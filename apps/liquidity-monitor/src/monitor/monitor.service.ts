import { Injectable, Logger, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { ethers } from 'ethers';
import {
  ADD_LIQUIDITY_ETH_SELECTOR,
  ROUTER_ABI_FRAGMENT,
  ERC20_ABI_FRAGMENT,
  FACTORY_ABI_FRAGMENT,
  PAIR_ABI_FRAGMENT,
} from './constants';
import {
  ChainId,
  ChainConfig,
  ChainConstants,
  CHAIN_CONFIGS,
  getChainConstants,
} from '../config/chains';
import { SecurityService } from '../security/security.service';
import { TelegramService } from '../telegram/telegram.service';
import { TradingService } from '../trading/trading.service';

/** Per-chain runtime state */
interface ChainState {
  chainConfig: ChainConfig;
  constants: ChainConstants;
  wsProvider: ethers.WebSocketProvider;
  httpProvider: ethers.JsonRpcProvider;
  cooldownMap: Map<string, number>;           // token address → last alert ts
  pairCreationTime: Map<string, number>;      // pair address → creation ts
}

@Injectable()
export class MonitorService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger('MonitorService');
  private readonly chains = new Map<ChainId, ChainState>();

  private cooldownMs: number;
  private newTokenMaxAge: number;

  constructor(
    private readonly config: ConfigService,
    private readonly security: SecurityService,
    private readonly telegram: TelegramService,
    private readonly trading: TradingService,
  ) {}

  async onModuleInit() {
    this.cooldownMs = this.config.get<number>('monitor.cooldownMs');
    this.newTokenMaxAge = this.config.get<number>('monitor.newTokenMaxAge');

    const enabledChains: string[] = this.config.get('enabledChains') || ['eth'];

    for (const chainIdStr of enabledChains) {
      const chainId = chainIdStr as ChainId;
      const chainConfig = CHAIN_CONFIGS[chainId];
      if (!chainConfig) {
        this.logger.warn(`Unknown chain "${chainIdStr}" in ENABLED_CHAINS — skipping`);
        continue;
      }

      const { wssUrl, httpUrl } = this.getChainRpcUrls(chainConfig);
      if (!wssUrl) {
        this.logger.warn(`[${chainConfig.name}] No WSS URL configured — skipping`);
        continue;
      }

      const httpProvider = new ethers.JsonRpcProvider(httpUrl || wssUrl.replace('wss://', 'https://'));
      const constants = getChainConstants(chainId);

      const state: ChainState = {
        chainConfig,
        constants,
        wsProvider: null as any, // set in connectChain
        httpProvider,
        cooldownMap: new Map(),
        pairCreationTime: new Map(),
      };

      this.chains.set(chainId, state);
      await this.connectChain(state, wssUrl);
    }

    const chainNames = Array.from(this.chains.values()).map((s) => s.chainConfig.name);
    await this.telegram.sendStartupMessage(chainNames);
  }

  async onModuleDestroy() {
    for (const state of this.chains.values()) {
      if (state.wsProvider) {
        await state.wsProvider.destroy().catch(() => {});
      }
    }
  }

  // ============================================================
  // Per-chain WebSocket setup
  // ============================================================

  private async connectChain(state: ChainState, wssUrl: string) {
    const { chainConfig, constants } = state;
    const tag = `[${chainConfig.name}]`;

    this.logger.log(`${tag} Connecting WebSocket...`);
    state.wsProvider = new ethers.WebSocketProvider(wssUrl);

    // Block listener
    state.wsProvider.on('block', async (blockNumber: number) => {
      try {
        await this.processBlock(blockNumber, state);
      } catch (err) {
        this.logger.error(`${tag} Block ${blockNumber} error: ${err.message}`);
      }
    });

    // PairCreated listeners
    for (const dex of chainConfig.dexList) {
      const factory = new ethers.Contract(dex.factory, FACTORY_ABI_FRAGMENT, state.wsProvider);
      factory.on('PairCreated', (token0, token1, pairAddress) => {
        const pair = pairAddress.toLowerCase();
        state.pairCreationTime.set(pair, Math.floor(Date.now() / 1000));
        this.logger.log(`${tag} New pair on ${dex.name}: ${pair} (${token0} / ${token1})`);
      });
    }

    // Auto-reconnect
    const ws = state.wsProvider.websocket as any;
    if (ws && typeof ws.on === 'function') {
      ws.on('close', () => {
        this.logger.warn(`${tag} WebSocket disconnected. Reconnecting in 5s...`);
        setTimeout(() => this.connectChain(state, wssUrl), 5000);
      });
    }

    this.logger.log(`${tag} WebSocket connected. Listening for blocks...`);
  }

  // ============================================================
  // Block processing (per-chain)
  // ============================================================

  private async processBlock(blockNumber: number, state: ChainState) {
    const block = await state.httpProvider.getBlock(blockNumber, true);
    if (!block || !block.prefetchedTransactions) return;

    for (const tx of block.prefetchedTransactions) {
      if (!tx.to || !tx.data) continue;

      const toAddr = tx.to.toLowerCase();
      if (
        state.constants.routerAddresses.has(toAddr) &&
        tx.data.startsWith(ADD_LIQUIDITY_ETH_SELECTOR)
      ) {
        await this.handleAddLiquidityETH(tx, block.timestamp, state);
      }
    }
  }

  // ============================================================
  // addLiquidityETH handler (chain-aware)
  // ============================================================

  private async handleAddLiquidityETH(
    tx: ethers.TransactionResponse,
    blockTimestamp: number,
    state: ChainState,
  ) {
    const { chainConfig, constants } = state;
    const tag = `[${chainConfig.name}]`;

    try {
      const iface = new ethers.Interface(ROUTER_ABI_FRAGMENT);
      const decoded = iface.decodeFunctionData('addLiquidityETH', tx.data);
      const tokenAddress: string = decoded[0];
      const ethAmount = ethers.formatEther(tx.value);
      const ethAmountNum = parseFloat(ethAmount);

      const tokenLower = tokenAddress.toLowerCase();
      const dex = constants.routerToDex.get(tx.to.toLowerCase());

      // Skip wrapped native or zero-value
      if (tokenLower === constants.wrappedNative.toLowerCase() || ethAmountNum === 0) {
        return;
      }

      // Determine if pair is new
      const isNew = await this.isPairNew(tokenAddress, tx.to, blockTimestamp, state);

      // Apply per-chain minimum liquidity threshold for new tokens
      if (isNew && ethAmountNum < chainConfig.minNativeNewToken) {
        return;
      }

      // Check cooldown (chain-prefixed)
      const cooldownKey = `${chainConfig.id}:${tokenLower}`;
      if (this.isOnCooldown(cooldownKey, state)) {
        return;
      }

      this.logger.log(
        `${tag} addLiquidityETH: ${ethAmount} ${chainConfig.nativeSymbol} for ${tokenAddress} on ${dex?.name ?? 'Unknown'}`,
      );

      // Get pair + pool info
      const pairAddress = await this.getPairAddress(tokenAddress, tx.to, state);
      const poolInfo = await this.getPoolInfo(pairAddress, state);

      // Estimate position for realistic security simulation
      const estimatedPosition = this.trading.estimatePosition(
        poolInfo?.ethReserve ?? 0,
        chainConfig.id,
      );

      // 3-LAYER SECURITY CHECK (chain-aware)
      const secResult = await this.security.checkToken(
        tokenAddress,
        pairAddress ?? undefined,
        estimatedPosition > 0 ? estimatedPosition : undefined,
        chainConfig,
        state.httpProvider,
      );

      // Get token info
      const { name, symbol } = await this.getTokenInfo(tokenAddress, state);

      let finalTradeable = secResult.isTradeable;
      let finalSummary = secResult.summary;
      let finalEmoji = secResult.emoji;

      // Layer 3: Micro-test if needed
      if (secResult.approvedBy === 'needs-micro-test') {
        if (this.trading.isReady(chainConfig.id) && pairAddress) {
          this.logger.log(`${tag} Starting micro-test for ${symbol}...`);
          const microResult = await this.trading.executeMicroTest({
            tokenAddress,
            tokenSymbol: symbol,
            pairAddress,
            routerAddress: dex?.router ?? '',
            poolEthReserve: poolInfo?.ethReserve ?? 0,
          }, chainConfig.id);

          if (microResult.success) {
            finalTradeable = true;
            finalSummary = `Micro-test passed (cost ${microResult.costETH.toFixed(5)} ${chainConfig.nativeSymbol}) | bytecode:${secResult.bytecodeRiskScore}`;
            finalEmoji = '✅';
          } else {
            finalTradeable = false;
            finalSummary = `Micro-test FAILED: ${microResult.reason}`;
            finalEmoji = '🚫';
          }
        } else {
          finalTradeable = false;
        }
      }

      if (!finalTradeable && secResult.approvedBy !== 'needs-micro-test') {
        this.logger.warn(`${tag} BLOCKED ${tokenAddress}: ${secResult.summary}`);
      }

      // Set cooldown
      state.cooldownMap.set(cooldownKey, Date.now());

      // Send Telegram notification (chain-aware)
      await this.telegram.sendLiquidityAlert({
        chainConfig,
        tokenName: name,
        tokenSymbol: symbol,
        tokenAddress,
        pairAddress: pairAddress || tokenAddress,
        nativeAmount: ethAmountNum.toFixed(2),
        dexName: dex?.name ?? 'Unknown DEX',
        isNewToken: isNew,
        securitySummary: finalSummary,
        securityEmoji: finalEmoji,
        buyTax: secResult.buyTax,
        sellTax: secResult.sellTax,
        poolNativeReserve: poolInfo?.ethReserve ?? null,
      });

      // Auto-trade if approved
      if (finalTradeable && this.trading.isReady(chainConfig.id) && pairAddress && poolInfo) {
        this.trading.executeBuy({
          tokenAddress,
          tokenSymbol: symbol,
          pairAddress,
          routerAddress: dex?.router ?? '',
          poolEthReserve: poolInfo.ethReserve,
          buyTax: secResult.buyTax ?? 0,
          sellTax: secResult.sellTax ?? 0,
        }, chainConfig.id).catch((err) => {
          this.logger.error(`${tag} Auto-trade failed: ${err.message}`);
        });
      }
    } catch (err) {
      this.logger.error(`${tag} Error handling tx ${tx.hash}: ${err.message}`);
    }
  }

  // ============================================================
  // Helpers (chain-aware)
  // ============================================================

  private async isPairNew(
    tokenAddress: string,
    routerAddress: string,
    currentTimestamp: number,
    state: ChainState,
  ): Promise<boolean> {
    const pairAddress = await this.getPairAddress(tokenAddress, routerAddress, state);
    if (!pairAddress) return true;

    const pairLower = pairAddress.toLowerCase();
    const createdAt = state.pairCreationTime.get(pairLower);
    if (createdAt) {
      return currentTimestamp - createdAt < this.newTokenMaxAge;
    }
    return false;
  }

  private async getPairAddress(
    tokenAddress: string,
    routerAddress: string,
    state: ChainState,
  ): Promise<string | null> {
    try {
      const dex = state.constants.routerToDex.get(routerAddress.toLowerCase());
      if (!dex) return null;

      const factory = new ethers.Contract(dex.factory, FACTORY_ABI_FRAGMENT, state.httpProvider);
      const pair = await factory.getPair(tokenAddress, state.constants.wrappedNative);
      if (pair === ethers.ZeroAddress) return null;
      return pair;
    } catch {
      return null;
    }
  }

  private async getTokenInfo(
    tokenAddress: string,
    state: ChainState,
  ): Promise<{ name: string; symbol: string }> {
    try {
      const token = new ethers.Contract(tokenAddress, ERC20_ABI_FRAGMENT, state.httpProvider);
      const [name, symbol] = await Promise.all([
        token.name().catch(() => 'Unknown'),
        token.symbol().catch(() => '???'),
      ]);
      return { name, symbol };
    } catch {
      return { name: 'Unknown', symbol: '???' };
    }
  }

  private isOnCooldown(cooldownKey: string, state: ChainState): boolean {
    const lastSent = state.cooldownMap.get(cooldownKey);
    if (!lastSent) return false;
    return Date.now() - lastSent < this.cooldownMs;
  }

  private async getPoolInfo(
    pairAddress: string | null,
    state: ChainState,
  ): Promise<{ ethReserve: number; tokenReserve: number } | null> {
    if (!pairAddress) return null;
    try {
      const pair = new ethers.Contract(pairAddress, PAIR_ABI_FRAGMENT, state.httpProvider);
      const [reserves, token0] = await Promise.all([
        pair.getReserves(),
        pair.token0(),
      ]);
      const r0 = parseFloat(ethers.formatEther(reserves[0]));
      const r1 = parseFloat(ethers.formatEther(reserves[1]));
      const isWrapped0 = token0.toLowerCase() === state.constants.wrappedNative.toLowerCase();
      return {
        ethReserve: isWrapped0 ? r0 : r1,
        tokenReserve: isWrapped0 ? r1 : r0,
      };
    } catch (err) {
      this.logger.warn(`Failed to read pool reserves: ${err.message}`);
      return null;
    }
  }

  // ============================================================
  // RPC URL resolution per chain
  // ============================================================

  private getChainRpcUrls(chain: ChainConfig): { wssUrl: string; httpUrl: string } {
    if (chain.id === 'eth') {
      return {
        wssUrl: this.config.get<string>('alchemy.wssUrl') || '',
        httpUrl: this.config.get<string>('alchemy.httpUrl') || '',
      };
    }
    return {
      wssUrl: this.config.get<string>(`${chain.id}.wssUrl`) || '',
      httpUrl: this.config.get<string>(`${chain.id}.httpUrl`) || '',
    };
  }
}
