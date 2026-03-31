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
import { TradeLoggerService } from '../analytics/trade-logger.service';
import { PatternAnalyzerService } from '../analytics/pattern-analyzer.service';
import { WatchdogService } from '../watchdog/watchdog.service';

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
    private readonly tradeLogger: TradeLoggerService,
    private readonly patternAnalyzer: PatternAnalyzerService,
    private readonly watchdog: WatchdogService,
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

    // Start pattern analyzer for trade learning
    this.patternAnalyzer.start();
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
        this.watchdog.onWsDisconnect(chainConfig.id);
        setTimeout(() => this.connectChain(state, wssUrl), 5000);
      });
    }

    // Watchdog: WS reconnected
    this.watchdog.onWsReconnect(chainConfig.id);
    this.logger.log(`${tag} WebSocket connected. Listening for blocks...`);
  }

  // ============================================================
  // Block processing (per-chain)
  // ============================================================

  private async processBlock(blockNumber: number, state: ChainState) {
    // Watchdog: block processed
    this.watchdog.onBlockProcessed(state.chainConfig.id, blockNumber);

    const hexBlock = '0x' + blockNumber.toString(16);
    const rawBlock: any = await state.httpProvider.send('eth_getBlockByNumber', [hexBlock, true]);
    if (!rawBlock?.transactions?.length) return;

    const timestamp = parseInt(rawBlock.timestamp, 16);

    for (const tx of rawBlock.transactions) {
      if (!tx.to || !tx.input) continue;

      const toAddr = tx.to.toLowerCase();
      if (
        state.constants.routerAddresses.has(toAddr) &&
        tx.input.startsWith(ADD_LIQUIDITY_ETH_SELECTOR)
      ) {
        const txResponse = await state.httpProvider.getTransaction(tx.hash);
        if (txResponse) {
          await this.handleAddLiquidityETH(txResponse, timestamp, state);
        }
      }
    }

    // Periodically clean stale pair creation entries
    if (blockNumber % 100 === 0) {
      const now = Math.floor(Date.now() / 1000);
      for (const [pair, ts] of state.pairCreationTime) {
        if (now - ts > this.newTokenMaxAge * 2) {
          state.pairCreationTime.delete(pair);
        }
      }
    }
  }

  // ============================================================
  // addLiquidityETH handler — NEW SECURITY FLOW
  //
  //  ① FAST GATE (300ms): eth_call sim + bytecode analysis
  //     → rejected? stop
  //     → approved or needs-micro-test? continue
  //
  //  ② PROOF GATE (24-48s): Real micro buy+sell ($0.04)
  //     → sell failed? stop (confirmed honeypot)
  //     → sell OK? continue (proven sellable)
  //
  //  ③ TRADE: Full position buy
  //
  //  ④ POST-PURCHASE GUARD: Mempool monitoring (10 min hold)
  //     → Owner calls setFee/blacklist/pause? → frontrun sell
  //     → Timer expires? → normal lot sell
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

      // Only process new tokens (pair age ≤ newTokenMaxAge)
      const isNew = await this.isPairNew(tokenAddress, tx.to, blockTimestamp, state);
      if (!isNew) {
        return;
      }

      // Apply per-chain minimum liquidity threshold
      if (ethAmountNum < chainConfig.minNativeNewToken) {
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

      // Get token info + pair + pool info + USD price
      const { name, symbol } = await this.getTokenInfo(tokenAddress, state);

      // Skip tokens with emoji or non-Latin characters (CJK, Arabic, Cyrillic, etc.)
      // Only allow: ASCII letters, digits, common symbols ($, ., -, _, space)
      const nonLatinRegex = /[^\x20-\x7E]/;
      if (nonLatinRegex.test(symbol) || nonLatinRegex.test(name)) {
        this.logger.log(`${tag} Skipping non-Latin token: ${symbol} (${name})`);
        this.tradeLogger.logSkipped({
          tokenAddress, tokenSymbol: symbol, chain: chainConfig.id,
          liquidityNative: ethAmountNum, reason: 'non-latin', bytecodeScore: 0, timestamp: Date.now(),
        });
        return;
      }

      const pairAddress = await this.getPairAddress(tokenAddress, tx.to, state);
      let poolInfo = await this.getPoolInfo(pairAddress, state);
      const nativePriceUSD = this.trading.getNativePriceUSD(chainConfig.nativeSymbol);

      // ⓪ OBSERVATION WINDOW — Watch pair activity for 5 min before buying
      // Goal: See if OTHER people can sell. If sells exist → token is sellable.
      // This catches honeypots for FREE (no gas spent).
      let sellCount = 0;
      let buyCount = 0;
      if (pairAddress) {
        const OBSERVE_MS = 5 * 60 * 1000; // 5 minutes
        const OBSERVE_CHECK_INTERVAL = 30 * 1000; // check every 30s
        const MIN_SELLS_REQUIRED = 1; // at least 1 sell proves sellability
        const MIN_SELL_ETH = ethers.parseEther('0.00015'); // ~$0.30 min sell size (filters fake micro-sells)

        this.logger.log(`${tag} 👀 Observing ${symbol} for 5min (watching for sells)...`);

        const observeStart = Date.now();
        let lastCheckedBlock = await state.httpProvider.getBlockNumber();

        while (Date.now() - observeStart < OBSERVE_MS) {
          await new Promise((r) => setTimeout(r, OBSERVE_CHECK_INTERVAL));

          // Check if liquidity still exists
          const poolNow = await this.getPoolInfo(pairAddress, state);
          if (!poolNow || poolNow.ethReserve < chainConfig.minNativeNewToken * 0.3) {
            this.logger.warn(`${tag} ❌ Liquidity REMOVED during observation for ${symbol}`);
            this.tradeLogger.logSkipped({
              tokenAddress, tokenSymbol: symbol, chain: chainConfig.id,
              liquidityNative: ethAmountNum, reason: 'liq-removed-during-observation', bytecodeScore: 0, timestamp: Date.now(),
            });
            return;
          }

          // Count Swap events on the pair to detect buys and sells
          try {
            const currentBlock = await state.httpProvider.getBlockNumber();
            if (currentBlock <= lastCheckedBlock) continue;

            const pair = new ethers.Contract(pairAddress, PAIR_ABI_FRAGMENT, state.httpProvider);
            const token0 = await pair.token0();
            const isToken0Native = token0.toLowerCase() === constants.wrappedNative.toLowerCase();

            const swapFilter = pair.filters.Swap();
            const events = await pair.queryFilter(swapFilter, lastCheckedBlock + 1, currentBlock);
            lastCheckedBlock = currentBlock;

            for (const event of events) {
              const args = (event as any).args;
              if (!args) continue;
              // A "sell" = token goes IN, native comes OUT
              // If token is token0: sell means amount0In > 0 && amount1Out > 0
              // If token is token1: sell means amount1In > 0 && amount0Out > 0
              if (isToken0Native) {
                // token is token1, native is token0
                // sell = token1 in, native(token0) out
                if (args.amount1In > BigInt(0) && args.amount0Out >= MIN_SELL_ETH) sellCount++;
                if (args.amount0In > BigInt(0) && args.amount1Out > BigInt(0)) buyCount++;
              } else {
                // token is token0, native is token1
                // sell = token0 in, native(token1) out
                if (args.amount0In > BigInt(0) && args.amount1Out >= MIN_SELL_ETH) sellCount++;
                if (args.amount1In > BigInt(0) && args.amount0Out > BigInt(0)) buyCount++;
              }
            }
          } catch (err) {
            this.logger.debug(`${tag} Swap event query error: ${err.message}`);
          }

          const elapsed = Math.round((Date.now() - observeStart) / 1000);
          this.logger.debug(`${tag} ${symbol} observation: ${elapsed}s | buys=${buyCount} sells=${sellCount}`);

          // Early exit: if we see enough sells, no need to wait full 5 min
          if (sellCount >= MIN_SELLS_REQUIRED) {
            this.logger.log(`${tag} ✅ ${symbol} has ${sellCount} sell(s) confirmed after ${elapsed}s — token is sellable`);
            break;
          }
        }

        // Re-read pool info after observation
        poolInfo = await this.getPoolInfo(pairAddress, state);
        if (!poolInfo || poolInfo.ethReserve < chainConfig.minNativeNewToken * 0.3) {
          this.logger.warn(`${tag} ❌ Liquidity gone after observation for ${symbol}`);
          return;
        }

        // DECISION: Did we see any sells?
        if (sellCount === 0) {
          this.logger.warn(`${tag} ❌ NO SELLS observed for ${symbol} in 5min (buys=${buyCount}) — likely honeypot, skipping`);
          this.tradeLogger.logSkipped({
            tokenAddress, tokenSymbol: symbol, chain: chainConfig.id,
            liquidityNative: ethAmountNum, reason: 'no-sells-observed', bytecodeScore: 0, timestamp: Date.now(),
          });
          await this.telegram.sendLiquidityAlert({
            chainConfig,
            tokenName: name,
            tokenSymbol: symbol,
            tokenAddress,
            pairAddress: pairAddress || tokenAddress,
            nativeAmount: ethAmountNum.toFixed(2),
            dexName: dex?.name ?? 'Unknown DEX',
            isNewToken: true,
            securitySummary: `🚫 No sells in 5min observation (buys=${buyCount}, sells=0) — likely honeypot`,
            securityEmoji: '🚫',
            buyTax: null,
            sellTax: null,
            poolNativeReserve: poolInfo?.ethReserve ?? null,
            nativePriceUSD,
          });
          return;
        }

        this.logger.log(`${tag} 👀 Observation complete: ${symbol} | buys=${buyCount} sells=${sellCount} | liq=${poolInfo.ethReserve.toFixed(2)} ${chainConfig.nativeSymbol}`);
      }

      // Check if token is already blacklisted (known honeypot)
      if (this.security.isBlacklisted(tokenAddress)) {
        this.logger.warn(`${tag} BLACKLISTED honeypot: ${tokenAddress}`);
        return;
      }

      // Estimate position for realistic security simulation
      const estimatedPosition = this.trading.estimatePosition(
        poolInfo?.ethReserve ?? 0,
        chainConfig.id,
      );

      // ① FAST GATE — sim + bytecode (300ms, parallel)
      const secResult = await this.security.checkToken(
        tokenAddress,
        pairAddress ?? undefined,
        estimatedPosition > 0 ? estimatedPosition : undefined,
        chainConfig,
        state.httpProvider,
      );

      // Set cooldown
      state.cooldownMap.set(cooldownKey, Date.now());

      // Watchdog: security check result + cooldown map size
      this.watchdog.onSecurityCheck(secResult.gate as 'approved' | 'needs-micro-test' | 'rejected');
      this.watchdog.updateCooldownMapSize(chainConfig.id, state.cooldownMap.size);

      // GATE DECISION
      if (secResult.gate === 'rejected') {
        this.logger.warn(`${tag} BLOCKED ${tokenAddress}: ${secResult.summary}`);
        this.tradeLogger.logSkipped({
          tokenAddress, tokenSymbol: symbol, chain: chainConfig.id,
          liquidityNative: ethAmountNum, reason: 'rejected', bytecodeScore: secResult.bytecodeRiskScore, timestamp: Date.now(),
        });

        await this.telegram.sendLiquidityAlert({
          chainConfig,
          tokenName: name,
          tokenSymbol: symbol,
          tokenAddress,
          pairAddress: pairAddress || tokenAddress,
          nativeAmount: ethAmountNum.toFixed(2),
          dexName: dex?.name ?? 'Unknown DEX',
          isNewToken: true,
          securitySummary: secResult.summary,
          securityEmoji: secResult.emoji,
          buyTax: secResult.buyTax,
          sellTax: secResult.sellTax,
          poolNativeReserve: poolInfo?.ethReserve ?? null,
          nativePriceUSD,
        });
        return;
      }

      // ② TRADE GATE — wallet ready?
      if (!this.trading.isReady(chainConfig.id) || !pairAddress) {
        this.logger.warn(`${tag} Trading not ready or no pair — notification only`);
        this.tradeLogger.logSkipped({
          tokenAddress, tokenSymbol: symbol, chain: chainConfig.id,
          liquidityNative: ethAmountNum, reason: 'wallet-not-ready', bytecodeScore: secResult.bytecodeRiskScore, timestamp: Date.now(),
        });

        await this.telegram.sendLiquidityAlert({
          chainConfig,
          tokenName: name,
          tokenSymbol: symbol,
          tokenAddress,
          pairAddress: pairAddress || tokenAddress,
          nativeAmount: ethAmountNum.toFixed(2),
          dexName: dex?.name ?? 'Unknown DEX',
          isNewToken: true,
          securitySummary: `${secResult.summary} (not traded — wallet not ready)`,
          securityEmoji: secResult.emoji,
          buyTax: secResult.buyTax,
          sellTax: secResult.sellTax,
          poolNativeReserve: poolInfo?.ethReserve ?? null,
          nativePriceUSD,
        });
        return;
      }

      // ③ MICRO-TEST — Real on-chain buy+sell proof ($0.04)
      // Catches amount-dependent honeypots AND whitelist honeypots that
      // pass observation (others can sell, but WE can't buy/sell)
      let microTestPassed = false;
      let microTestCost = 0;

      if (secResult.gate === 'approved') {
        // Fast gate approved — micro-test as final proof
        this.logger.log(`${tag} Running micro-test for ${symbol} (fast gate approved)...`);
      } else {
        // needs-micro-test — one security layer failed, micro-test decides
        this.logger.log(`${tag} Running micro-test for ${symbol} (gate needs proof)...`);
      }

      const microResult = await this.trading.executeMicroTest({
        tokenAddress,
        tokenSymbol: symbol,
        pairAddress,
        routerAddress: dex?.router ?? '',
        poolEthReserve: poolInfo?.ethReserve ?? 0,
      }, chainConfig.id);

      microTestPassed = microResult.success;
      microTestCost = microResult.costETH;

      if (!microTestPassed) {
        this.logger.warn(`${tag} ❌ MICRO-TEST FAILED for ${symbol}: ${microResult.reason}`);
        this.tradeLogger.logSkipped({
          tokenAddress, tokenSymbol: symbol, chain: chainConfig.id,
          liquidityNative: ethAmountNum, reason: 'micro-test-failed',
          bytecodeScore: secResult.bytecodeRiskScore, timestamp: Date.now(),
        });
        await this.telegram.sendLiquidityAlert({
          chainConfig,
          tokenName: name,
          tokenSymbol: symbol,
          tokenAddress,
          pairAddress,
          nativeAmount: ethAmountNum.toFixed(2),
          dexName: dex?.name ?? 'Unknown DEX',
          isNewToken: true,
          securitySummary: `🚫 Micro-test FAILED: ${microResult.reason?.slice(0, 60)} | cost: ${microTestCost.toFixed(5)} ${chainConfig.nativeSymbol}`,
          securityEmoji: '🚫',
          buyTax: secResult.buyTax,
          sellTax: secResult.sellTax,
          poolNativeReserve: poolInfo?.ethReserve ?? null,
          nativePriceUSD,
        });
        return;
      }

      this.logger.log(`${tag} ✅ MICRO-TEST PASSED for ${symbol} | cost: ${microTestCost.toFixed(5)} ${chainConfig.nativeSymbol}`);

      // ④ TRADE — Observation + security + micro-test all passed → full position buy
      const finalSummary = `✅ Observation: ${sellCount} sell(s) | Micro-test: PASSED (${microTestCost.toFixed(5)}) | bytecode:${secResult.bytecodeRiskScore}`;

      await this.telegram.sendLiquidityAlert({
        chainConfig,
        tokenName: name,
        tokenSymbol: symbol,
        tokenAddress,
        pairAddress,
        nativeAmount: ethAmountNum.toFixed(2),
        dexName: dex?.name ?? 'Unknown DEX',
        isNewToken: true,
        securitySummary: finalSummary,
        securityEmoji: '✅',
        buyTax: secResult.buyTax,
        sellTax: secResult.sellTax,
        poolNativeReserve: poolInfo?.ethReserve ?? null,
      });

      // Execute full buy + start mempool monitoring
      this.trading.executeBuy({
        tokenAddress,
        tokenSymbol: symbol,
        pairAddress,
        routerAddress: dex?.router ?? '',
        poolEthReserve: poolInfo?.ethReserve ?? 0,
        buyTax: secResult.buyTax ?? 0,
        sellTax: secResult.sellTax ?? 0,
        bytecodeScore: secResult.bytecodeRiskScore,
        microTestPassed: true,
        microTestCostNative: microTestCost,
        securityGate: secResult.gate as 'approved' | 'needs-micro-test',
        hasNonLatinName: false,
      }, chainConfig.id).catch((err) => {
        this.logger.error(`${tag} Auto-trade failed: ${err.message}`);
      });
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

    // PairCreated event may not have fired yet (race condition: pair creation
    // and addLiquidity often land in the same block/TX). If the addLiquidityETH
    // TX itself is very recent, treat the pair as new and register it.
    const nowSec = Math.floor(Date.now() / 1000);
    if (nowSec - currentTimestamp < this.newTokenMaxAge) {
      this.logger.debug(
        `[${state.chainConfig.name}] Pair ${pairLower} not in PairCreated map — registering from addLiq block (age=${nowSec - currentTimestamp}s)`,
      );
      state.pairCreationTime.set(pairLower, currentTimestamp);
      return true;
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
