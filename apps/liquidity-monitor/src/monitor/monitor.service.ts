import { Injectable, Logger, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { ethers } from 'ethers';
import {
  WETH_ADDRESS,
  ROUTER_ADDRESSES,
  ROUTER_TO_DEX,
  FACTORY_TO_DEX,
  ADD_LIQUIDITY_ETH_SELECTOR,
  PAIR_CREATED_TOPIC,
  ROUTER_ABI_FRAGMENT,
  ERC20_ABI_FRAGMENT,
  FACTORY_ABI_FRAGMENT,
  PAIR_ABI_FRAGMENT,
  DEX_LIST,
} from './constants';
import { SecurityService } from '../security/security.service';
import { TelegramService } from '../telegram/telegram.service';
import { TradingService } from '../trading/trading.service';

@Injectable()
export class MonitorService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(MonitorService.name);
  private wsProvider: ethers.WebSocketProvider;
  private httpProvider: ethers.JsonRpcProvider;

  // token address (lowercase) → last notification timestamp
  private readonly cooldownMap = new Map<string, number>();

  // pair address (lowercase) → creation timestamp (block timestamp)
  private readonly pairCreationTime = new Map<string, number>();

  private cooldownMs: number;
  private minEthNew: number;
  private minEthExisting: number;
  private newTokenMaxAge: number;

  constructor(
    private readonly config: ConfigService,
    private readonly security: SecurityService,
    private readonly telegram: TelegramService,
    private readonly trading: TradingService,
  ) {}

  async onModuleInit() {
    this.cooldownMs = this.config.get<number>('monitor.cooldownMs');
    this.minEthNew = this.config.get<number>('monitor.minEthNewToken');
    this.minEthExisting = this.config.get<number>('monitor.minEthExistingToken');
    this.newTokenMaxAge = this.config.get<number>('monitor.newTokenMaxAge');

    const wssUrl = this.config.get<string>('alchemy.wssUrl');
    const httpUrl = this.config.get<string>('alchemy.httpUrl');

    if (!wssUrl) {
      this.logger.error('ALCHEMY_WSS_URL not configured. Exiting.');
      return;
    }

    this.httpProvider = new ethers.JsonRpcProvider(
      httpUrl || wssUrl.replace('wss://', 'https://').replace('/v2/', '/v2/'),
    );

    await this.connectWebSocket(wssUrl);
    await this.telegram.sendStartupMessage();
  }

  async onModuleDestroy() {
    if (this.wsProvider) {
      await this.wsProvider.destroy();
    }
  }

  private async connectWebSocket(wssUrl: string) {
    this.logger.log('Connecting to Alchemy WebSocket...');

    this.wsProvider = new ethers.WebSocketProvider(wssUrl);

    // Listen for new blocks
    this.wsProvider.on('block', async (blockNumber: number) => {
      try {
        await this.processBlock(blockNumber);
      } catch (err) {
        this.logger.error(`Error processing block ${blockNumber}: ${err.message}`);
      }
    });

    // Subscribe to PairCreated events from all known factories
    for (const dex of DEX_LIST) {
      const factory = new ethers.Contract(
        dex.factory,
        FACTORY_ABI_FRAGMENT,
        this.wsProvider,
      );

      factory.on('PairCreated', (token0, token1, pairAddress) => {
        const pair = pairAddress.toLowerCase();
        this.pairCreationTime.set(pair, Math.floor(Date.now() / 1000));
        this.logger.log(
          `New pair created on ${dex.name}: ${pair} (${token0} / ${token1})`,
        );
      });
    }

    // Handle WebSocket disconnection with auto-reconnect
    const ws = this.wsProvider.websocket as any;
    if (ws && typeof ws.on === 'function') {
      ws.on('close', () => {
        this.logger.warn('WebSocket disconnected. Reconnecting in 5s...');
        setTimeout(() => this.connectWebSocket(wssUrl), 5000);
      });
    }

    this.logger.log('WebSocket connected. Listening for blocks...');
  }

  private async processBlock(blockNumber: number) {
    const block = await this.httpProvider.getBlock(blockNumber, true);
    if (!block || !block.prefetchedTransactions) return;

    for (const tx of block.prefetchedTransactions) {
      if (!tx.to || !tx.data) continue;

      const toAddr = tx.to.toLowerCase();

      // Check if this is a call to a known router with addLiquidityETH
      if (
        ROUTER_ADDRESSES.has(toAddr) &&
        tx.data.startsWith(ADD_LIQUIDITY_ETH_SELECTOR)
      ) {
        await this.handleAddLiquidityETH(tx, block.timestamp);
      }
    }
  }

  private async handleAddLiquidityETH(
    tx: ethers.TransactionResponse,
    blockTimestamp: number,
  ) {
    try {
      const iface = new ethers.Interface(ROUTER_ABI_FRAGMENT);
      const decoded = iface.decodeFunctionData('addLiquidityETH', tx.data);
      const tokenAddress: string = decoded[0]; // first param is token address
      const ethAmount = ethers.formatEther(tx.value);
      const ethAmountNum = parseFloat(ethAmount);

      const tokenLower = tokenAddress.toLowerCase();
      const dex = ROUTER_TO_DEX.get(tx.to.toLowerCase());

      // Skip WETH or zero-value
      if (tokenLower === WETH_ADDRESS.toLowerCase() || ethAmountNum === 0) {
        return;
      }

      // Determine if pair is new
      const isNew = await this.isPairNew(tokenAddress, tx.to, blockTimestamp);

      // Apply ETH threshold
      const minEth = isNew ? this.minEthNew : this.minEthExisting;
      if (ethAmountNum < minEth) {
        return;
      }

      // Check cooldown
      if (this.isOnCooldown(tokenLower)) {
        this.logger.debug(
          `Skipping ${tokenLower} — on cooldown`,
        );
        return;
      }

      this.logger.log(
        `addLiquidityETH detected: ${ethAmount} ETH for token ${tokenAddress} on ${dex?.name ?? 'Unknown DEX'}`,
      );

      // Security check FIRST — gate: must be tradeable
      const secResult = await this.security.checkToken(tokenAddress);

      if (!secResult.isTradeable) {
        this.logger.warn(
          `BLOCKED ${tokenAddress}: ${secResult.summary} — not tradeable`,
        );
        return;
      }

      // Get token info
      const { name, symbol } = await this.getTokenInfo(tokenAddress);

      // Get pair address
      const pairAddress = await this.getPairAddress(tokenAddress, tx.to);

      // Get pool reserves for liquidity info + lot calculation
      const poolInfo = await this.getPoolInfo(pairAddress);

      // Set cooldown
      this.cooldownMap.set(tokenLower, Date.now());

      // Send notification with pool info and lot plan
      await this.telegram.sendLiquidityAlert({
        tokenName: name,
        tokenSymbol: symbol,
        tokenAddress,
        pairAddress: pairAddress || tokenAddress,
        ethAmount: ethAmountNum.toFixed(2),
        dexName: dex?.name ?? 'Unknown DEX',
        isNewToken: isNew,
        securitySummary: secResult.summary,
        securityEmoji: secResult.emoji,
        buyTax: secResult.buyTax,
        sellTax: secResult.sellTax,
        poolEthReserve: poolInfo?.ethReserve ?? null,
      });

      // Auto-trade disabled — notification-only mode
      // if (this.trading.isReady() && pairAddress && poolInfo) {
      //   this.trading.executeBuy({ ... }).catch(...);
      // }
    } catch (err) {
      this.logger.error(`Error handling addLiquidityETH tx ${tx.hash}: ${err.message}`);
    }
  }

  private async isPairNew(
    tokenAddress: string,
    routerAddress: string,
    currentTimestamp: number,
  ): Promise<boolean> {
    const pairAddress = await this.getPairAddress(tokenAddress, routerAddress);
    if (!pairAddress) return true; // If we can't find the pair, assume new

    const pairLower = pairAddress.toLowerCase();

    // Check our local cache first
    const createdAt = this.pairCreationTime.get(pairLower);
    if (createdAt) {
      return currentTimestamp - createdAt < this.newTokenMaxAge;
    }

    // If not in cache, try to get creation block from the pair contract
    // For simplicity, if we haven't seen it created, assume it's existing
    return false;
  }

  private async getPairAddress(
    tokenAddress: string,
    routerAddress: string,
  ): Promise<string | null> {
    try {
      const dex = ROUTER_TO_DEX.get(routerAddress.toLowerCase());
      if (!dex) return null;

      const factory = new ethers.Contract(
        dex.factory,
        FACTORY_ABI_FRAGMENT,
        this.httpProvider,
      );

      const pair = await factory.getPair(tokenAddress, WETH_ADDRESS);
      if (pair === ethers.ZeroAddress) return null;
      return pair;
    } catch {
      return null;
    }
  }

  private async getTokenInfo(
    tokenAddress: string,
  ): Promise<{ name: string; symbol: string }> {
    try {
      const token = new ethers.Contract(
        tokenAddress,
        ERC20_ABI_FRAGMENT,
        this.httpProvider,
      );
      const [name, symbol] = await Promise.all([
        token.name().catch(() => 'Unknown'),
        token.symbol().catch(() => '???'),
      ]);
      return { name, symbol };
    } catch {
      return { name: 'Unknown', symbol: '???' };
    }
  }

  private isOnCooldown(tokenAddress: string): boolean {
    const lastSent = this.cooldownMap.get(tokenAddress);
    if (!lastSent) return false;
    return Date.now() - lastSent < this.cooldownMs;
  }

  /**
   * Reads on-chain reserves from a Uniswap V2 pair to determine
   * ETH liquidity depth (used for lot-sell planning).
   */
  private async getPoolInfo(
    pairAddress: string | null,
  ): Promise<{ ethReserve: number; tokenReserve: number } | null> {
    if (!pairAddress) return null;

    try {
      const pair = new ethers.Contract(
        pairAddress,
        PAIR_ABI_FRAGMENT,
        this.httpProvider,
      );

      const [reserves, token0] = await Promise.all([
        pair.getReserves(),
        pair.token0(),
      ]);

      const r0 = parseFloat(ethers.formatEther(reserves[0]));
      const r1 = parseFloat(ethers.formatEther(reserves[1]));
      const isWeth0 =
        token0.toLowerCase() === WETH_ADDRESS.toLowerCase();

      return {
        ethReserve: isWeth0 ? r0 : r1,
        tokenReserve: isWeth0 ? r1 : r0,
      };
    } catch (err) {
      this.logger.warn(`Failed to read pool reserves: ${err.message}`);
      return null;
    }
  }
}
