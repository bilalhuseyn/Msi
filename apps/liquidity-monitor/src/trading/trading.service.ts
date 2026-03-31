import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { ethers } from 'ethers';
import * as fs from 'fs';
import * as path from 'path';
import * as WebSocket from 'ws';
import { PAIR_ABI_FRAGMENT } from '../monitor/constants';
import { ChainId, ChainConfig, CHAIN_CONFIGS } from '../config/chains';
import { TelegramService } from '../telegram/telegram.service';
import { TradeLoggerService } from '../analytics/trade-logger.service';
import { SecurityService } from '../security/security.service';
import { WatchdogService } from '../watchdog/watchdog.service';

interface ActiveTrade {
  chainId: ChainId;
  tokenAddress: string;
  tokenCreator: string | null;
  pairAddress: string;
  routerAddress: string;
  tokenSymbol: string;
  tokenDecimals: number;
  entryPriceETH: number;
  tokensBought: bigint;
  ethSpent: number;
  buyTxHash: string;
  buyTimestamp: number;
  sellStarted: boolean;
  lotsSold: number;
  totalEthReceived: number;
  mempoolUnsub?: () => void;  // cleanup for mempool listener
  // Pre-signed emergency sell state
  preSignedSellData?: string;       // Pre-encoded swap calldata
  cachedNonce?: number;             // Cached nonce for instant signing
  cachedTokenBalance?: bigint;      // Cached token balance
  sellRetryCount: number;
  sellStartedAt: number | null;
  // Analytics metadata
  liquidityNative: number;
  bytecodeScore: number;
  hasNonLatinName: boolean;
  microTestPassed: boolean;
  microTestCostNative: number;
  securityGate: 'approved' | 'needs-micro-test';
  deployer: string | null;
}

export interface TradeOpportunity {
  tokenAddress: string;
  tokenSymbol: string;
  pairAddress: string;
  routerAddress: string;
  poolEthReserve: number;
  buyTax: number;
  sellTax: number;
  // Metadata for trade logger
  deployer?: string | null;
  bytecodeScore?: number;
  hasNonLatinName?: boolean;
  microTestPassed?: boolean;
  microTestCostNative?: number;
  securityGate?: 'approved' | 'needs-micro-test';
}

export interface MicroTestResult {
  success: boolean;
  costETH: number;
  reason?: string;
}

const MAX_CONCURRENT_TRADES = 3;
const MAX_SELL_RETRIES = 3;
const SELL_STUCK_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
const HARD_DEADLINE_MS = 10 * 60 * 1000; // 10 minutes — trade MUST close after this
const TX_WAIT_TIMEOUT_MS = 90 * 1000; // 90 seconds max for tx.wait

// Per-chain trading state
interface ChainTradingState {
  chainId: ChainId;
  chainConfig: ChainConfig;
  provider: ethers.JsonRpcProvider;
  wsProvider: ethers.WebSocketProvider | null;
  wallet: ethers.Wallet;
  enabled: boolean;
  activeTrades: ActiveTrade[];
  lastKnownBalance: number;
  dailyStartBalance: number;
  consecutiveLosses: number;
  pausedUntil: number;
  sellQueue: Array<() => Promise<void>>;
  sellQueueRunning: boolean;
}

const ROUTER_TRADE_ABI = [
  'function swapExactETHForTokens(uint256 amountOutMin, address[] calldata path, address to, uint256 deadline) payable returns (uint256[] memory amounts)',
  'function swapExactETHForTokensSupportingFeeOnTransferTokens(uint256 amountOutMin, address[] calldata path, address to, uint256 deadline) payable',
  'function swapExactTokensForETH(uint256 amountIn, uint256 amountOutMin, address[] calldata path, address to, uint256 deadline) returns (uint256[] memory amounts)',
  'function swapExactTokensForETHSupportingFeeOnTransferTokens(uint256 amountIn, uint256 amountOutMin, address[] calldata path, address to, uint256 deadline)',
  'function getAmountsOut(uint256 amountIn, address[] calldata path) view returns (uint256[] memory amounts)',
];

const ERC20_TRADE_ABI = [
  'function approve(address spender, uint256 amount) returns (bool)',
  'function balanceOf(address account) view returns (uint256)',
  'function allowance(address owner, address spender) view returns (uint256)',
  'function decimals() view returns (uint8)',
];

// CRITICAL: Only removeLiquidity selectors — triggers emergency sell
const REMOVE_LIQUIDITY_SELECTORS = [
  'baa2abde', // removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)
  'ded9382a', // removeLiquidityETH(address,uint256,uint256,uint256,address,uint256)
  '02751cec', // removeLiquidityETH(address,uint256,uint256,uint256,address,uint256) variant
  'af2979eb', // removeLiquidityETHSupportingFeeOnTransferTokens(...)
  '5b0d5984', // removeLiquidityETHWithPermit(...)
  '5c11d795', // removeLiquidityETHWithPermitSupportingFeeOnTransferTokens(...)
];

// Multi-RPC broadcast endpoints (verified free public RPCs)
const BROADCAST_RPCS: Record<ChainId, string[]> = {
  eth: [
    'https://rpc.flashbots.net',               // Flashbots Protect (free, private mempool)
    'https://eth.llamarpc.com',                // LlamaNodes (free)
    'https://ethereum-rpc.publicnode.com',     // PublicNode (free)
    'https://rpc.mevblocker.io',              // MEV Blocker (free, MEV protected)
  ],
  bsc: [
    'https://bsc-dataseed1.binance.org',      // Binance official (free)
    'https://bsc-dataseed2.binance.org',      // Binance official (free)
    'https://bsc-dataseed3.binance.org',      // Binance official (free)
  ],
  base: [
    'https://mainnet.base.org',               // Base official (free)
    'https://base-rpc.publicnode.com',        // PublicNode (free)
  ],
};

@Injectable()
export class TradingService implements OnModuleInit {
  private readonly logger = new Logger(TradingService.name);
  private readonly chainStates = new Map<ChainId, ChainTradingState>();

  // Native token USD prices (cached, updated every 5min)
  private nativePricesUSD: Record<string, number> = { ETH: 0, BNB: 0 };
  private priceLastUpdated = 0;

  // Trade state persistence
  private readonly TRADES_FILE = path.join(process.cwd(), 'active-trades.json');

  // Shared config
  private positionPct: number;
  private maxPoolPct: number;
  private slippagePct: number;
  private holdTimeMs: number;
  private stopLossPct: number;
  private maxDailyLossPct: number;
  private maxConsecutiveLosses: number;

  constructor(
    private readonly config: ConfigService,
    private readonly telegram: TelegramService,
    private readonly tradeLogger: TradeLoggerService,
    private readonly security: SecurityService,
    private readonly watchdog: WatchdogService,
  ) {}

  async onModuleInit() {
    this.positionPct = this.config.get<number>('trading.positionPct') || 50;
    this.maxPoolPct = this.config.get<number>('trading.maxPoolPct') || 3;
    this.slippagePct = this.config.get<number>('trading.slippagePct') || 5;
    this.holdTimeMs = this.config.get<number>('trading.holdTimeMs') || 5 * 60 * 1000;
    this.stopLossPct = this.config.get<number>('trading.stopLossPct') || 7;
    this.maxDailyLossPct = this.config.get<number>('trading.maxDailyLossPct') || 30;
    this.maxConsecutiveLosses = this.config.get<number>('trading.maxConsecutiveLosses') || 3;

    const enabledChains: string[] = this.config.get('enabledChains') || ['eth'];

    for (const chainIdStr of enabledChains) {
      const chainId = chainIdStr as ChainId;
      const chainConfig = CHAIN_CONFIGS[chainId];
      if (!chainConfig) continue;

      const { privateKey, rpcUrl, wssUrl } = this.getChainTradingConfig(chainConfig);
      if (!privateKey || !rpcUrl) {
        this.logger.warn(`[${chainConfig.name}] No private key or RPC — trading disabled`);
        continue;
      }

      try {
        const provider = new ethers.JsonRpcProvider(rpcUrl);
        const wallet = new ethers.Wallet(privateKey, provider);
        const balance = await provider.getBalance(wallet.address);
        const balNative = parseFloat(ethers.formatEther(balance));

        // WebSocket provider for mempool monitoring
        let wsProvider: ethers.WebSocketProvider | null = null;
        if (wssUrl) {
          try {
            wsProvider = new ethers.WebSocketProvider(wssUrl);
          } catch (err) {
            this.logger.warn(`[${chainConfig.name}] WSS for mempool monitoring failed: ${err.message}`);
          }
        }

        const state: ChainTradingState = {
          chainId,
          chainConfig,
          provider,
          wsProvider,
          wallet,
          enabled: true,
          activeTrades: [],
          lastKnownBalance: balNative,
          dailyStartBalance: balNative,
          consecutiveLosses: 0,
          pausedUntil: 0,
          sellQueue: [],
          sellQueueRunning: false,
        };

        this.chainStates.set(chainId, state);
        this.startStopLossMonitor(state);

        // Watchdog: initial wallet balance
        this.watchdog.updateWalletBalance(chainId, chainConfig.name, chainConfig.nativeSymbol, balNative);

        this.logger.log(
          `[${chainConfig.name}] Trading enabled | Wallet: ${wallet.address} | Balance: ${balNative.toFixed(4)} ${chainConfig.nativeSymbol} | Mempool: ${wsProvider ? 'YES' : 'NO'}`,
        );
      } catch (err) {
        this.logger.error(`[${chainConfig.name}] Trading init failed: ${err.message}`);
      }
    }

    if (this.chainStates.size === 0) {
      this.logger.warn('No chains configured for trading (notification-only mode).');
    }

    // Start USD price cache (immediate + every 5min)
    this.updateNativePrices();
    setInterval(() => this.updateNativePrices(), 5 * 60 * 1000);

    // Restore persisted trades from previous run
    await this.restoreTrades();
  }

  isReady(chainId?: ChainId): boolean {
    if (!chainId) {
      for (const s of this.chainStates.values()) {
        if (s.enabled && s.activeTrades.length < MAX_CONCURRENT_TRADES && Date.now() >= s.pausedUntil) return true;
      }
      return false;
    }
    const s = this.chainStates.get(chainId);
    if (!s || !s.enabled) return false;
    if (s.activeTrades.length >= MAX_CONCURRENT_TRADES) return false;
    if (Date.now() < s.pausedUntil) return false;
    return true;
  }

  estimatePosition(poolEthReserve: number, chainId?: ChainId): number {
    const s = chainId ? this.chainStates.get(chainId) : this.chainStates.values().next().value;
    if (!s || !s.enabled) return 0;
    const posFromBalance = s.lastKnownBalance * (this.positionPct / 100);
    const posFromPool = poolEthReserve * (this.maxPoolPct / 100);
    return Math.min(posFromBalance, posFromPool);
  }

  // ============================================================
  // MICRO BUY+SELL TEST — Real TX proof of sellability ($0.04)
  // ============================================================

  async executeMicroTest(
    opp: Omit<TradeOpportunity, 'buyTax' | 'sellTax'>,
    chainId?: ChainId,
  ): Promise<MicroTestResult> {
    const s = this.getState(chainId);
    if (!s) return { success: false, costETH: 0, reason: 'Trading not enabled' };

    const wrappedNative = s.chainConfig.wrappedNative;
    const tag = `[${s.chainConfig.name}]`;

    // $0.04 micro test amounts per chain
    // ETH: ~$2000/ETH → 0.000015 ETH ≈ $0.03
    // BSC: ~$630/BNB  → 0.000048 BNB ≈ $0.03
    // Base: ~$2000/ETH → 0.000015 ETH ≈ $0.03
    const microAmounts: Record<string, string> = {
      eth: '0.000015',
      bsc: '0.000048',
      base: '0.000015',
    };
    const microAmount = ethers.parseEther(microAmounts[s.chainConfig.id] || '0.00002');
    const balanceBefore = await s.provider.getBalance(s.wallet.address);

    try {
      const feeData = await s.provider.getFeeData();
      const routerTx = new ethers.Contract(opp.routerAddress, ROUTER_TRADE_ABI, s.wallet);
      const deadline = Math.floor(Date.now() / 1000) + 300;

      // STEP 1: Micro buy
      this.logger.log(`${tag} Micro-test BUY: ${ethers.formatEther(microAmount)} ${s.chainConfig.nativeSymbol} (~$0.04)`);
      const buyTx = await routerTx.swapExactETHForTokensSupportingFeeOnTransferTokens(
        0, [wrappedNative, opp.tokenAddress], s.wallet.address, deadline,
        { value: microAmount, gasLimit: 300000, maxFeePerGas: feeData.maxFeePerGas, maxPriorityFeePerGas: feeData.maxPriorityFeePerGas },
      );
      const buyReceipt = await buyTx.wait(1);
      if (!buyReceipt || buyReceipt.status === 0) {
        return { success: false, costETH: 0.0001, reason: 'Micro buy reverted' };
      }

      const tokenContract = new ethers.Contract(opp.tokenAddress, ERC20_TRADE_ABI, s.provider);
      const tokenBalance = await tokenContract.balanceOf(s.wallet.address);
      if (tokenBalance <= BigInt(0)) {
        return { success: false, costETH: 0.0001, reason: 'Received 0 tokens' };
      }

      // STEP 2: Approve
      const tokenTx = new ethers.Contract(opp.tokenAddress, ERC20_TRADE_ABI, s.wallet);
      const approveTx = await tokenTx.approve(opp.routerAddress, ethers.MaxUint256, { gasLimit: 100000 });
      await approveTx.wait(1);

      // STEP 3: Micro sell — THE PROOF
      this.logger.log(`${tag} Micro-test SELL: ${opp.tokenSymbol}`);
      const sellTx = await routerTx.swapExactTokensForETHSupportingFeeOnTransferTokens(
        tokenBalance, 0, [opp.tokenAddress, wrappedNative], s.wallet.address, deadline,
        { gasLimit: 300000 },
      );
      const sellReceipt = await sellTx.wait(1);
      if (!sellReceipt || sellReceipt.status === 0) {
        return { success: false, costETH: 0.0002, reason: 'Micro sell reverted — HONEYPOT CONFIRMED' };
      }

      const balanceAfter = await s.provider.getBalance(s.wallet.address);
      const costETH = parseFloat(ethers.formatEther(balanceBefore - balanceAfter));
      this.logger.log(`${tag} Micro-test PASSED: cost ${costETH.toFixed(5)} ${s.chainConfig.nativeSymbol}`);
      return { success: true, costETH: Math.max(costETH, 0) };
    } catch (err) {
      const balanceAfter = await s.provider.getBalance(s.wallet.address);
      const costETH = parseFloat(ethers.formatEther(balanceBefore - balanceAfter));
      return { success: false, costETH: Math.max(costETH, 0), reason: err.message?.slice(0, 100) };
    }
  }

  // ============================================================
  // Buy execution + post-purchase mempool guard
  // ============================================================

  async executeBuy(opp: TradeOpportunity, chainId?: ChainId): Promise<boolean> {
    const s = this.getState(chainId);
    if (!s || !this.isReady(chainId)) {
      const reason = !s ? 'no chain state' : !s.enabled ? 'chain disabled' :
        s.activeTrades.length >= MAX_CONCURRENT_TRADES ? `max trades (${MAX_CONCURRENT_TRADES})` :
        Date.now() < s.pausedUntil ? `paused ${Math.round((s.pausedUntil - Date.now()) / 1000)}s` : 'unknown';
      this.logger.warn(`[${s?.chainConfig?.name ?? chainId}] Buy skipped for ${opp.tokenSymbol}: ${reason}`);
      return false;
    }

    // DUPLICATE BUY GUARD: Check if we already have an active trade for this token
    const tokenLower = opp.tokenAddress.toLowerCase();
    const alreadyActive = s.activeTrades.some(t => t.tokenAddress.toLowerCase() === tokenLower);
    if (alreadyActive) {
      this.logger.warn(`[${s.chainConfig.name}] DUPLICATE BUY BLOCKED: ${opp.tokenSymbol} already in active trades`);
      return false;
    }

    const wrappedNative = s.chainConfig.wrappedNative;
    const tag = `[${s.chainConfig.name}]`;

    try {
      const feeData = await s.provider.getFeeData();
      const gasGwei = parseFloat(ethers.formatUnits(feeData.gasPrice, 'gwei'));
      const maxGas = this.getMaxGas(s.chainConfig);
      if (gasGwei > maxGas) {
        this.logger.warn(`${tag} Gas too high: ${gasGwei.toFixed(1)} > ${maxGas}`);
        return false;
      }

      const currentBalance = await this.getBalanceNative(s);
      s.lastKnownBalance = currentBalance;

      const dailyLossPct = ((s.dailyStartBalance - currentBalance) / s.dailyStartBalance) * 100;
      if (dailyLossPct >= this.maxDailyLossPct) {
        this.logger.warn(`${tag} Daily loss limit hit: ${dailyLossPct.toFixed(1)}%`);
        return false;
      }

      const posFromBalance = currentBalance * (this.positionPct / 100);
      const posFromPool = opp.poolEthReserve * (this.maxPoolPct / 100);
      let positionETH = Math.min(posFromBalance, posFromPool);

      // Test mode: cap BSC and Base buys to ~$0.10
      // Test mode: cap buys to ~$0.08
      const testCaps: Record<string, number> = { bsc: 0.000127, base: 0.00004 };
      const cap = testCaps[s.chainConfig.id];
      if (cap && positionETH > cap) {
        this.logger.log(`${tag} Test mode: capping position from ${positionETH.toFixed(6)} to ${cap} ${s.chainConfig.nativeSymbol} (~$0.08)`);
        positionETH = cap;
      }

      if (positionETH <= 0) {
        this.logger.warn(`${tag} Position size 0 for ${opp.tokenSymbol} — balance: ${currentBalance.toFixed(6)} | poolReserve: ${opp.poolEthReserve.toFixed(2)}`);
        return false;
      }

      this.logger.log(`${tag} BUY signal: ${opp.tokenSymbol} | pos=${positionETH.toFixed(4)} ${s.chainConfig.nativeSymbol} | pool=${opp.poolEthReserve.toFixed(2)} | gas=${gasGwei.toFixed(1)}`);

      // Record token balance BEFORE buy and read decimals
      const tokenContractRead = new ethers.Contract(opp.tokenAddress, ERC20_TRADE_ABI, s.provider);
      const [tokenBalanceBefore, tokenDecimals] = await Promise.all([
        tokenContractRead.balanceOf(s.wallet.address),
        tokenContractRead.decimals().catch(() => 18),
      ]);
      const decimals = Number(tokenDecimals);

      const routerRead = new ethers.Contract(opp.routerAddress, ROUTER_TRADE_ABI, s.provider);
      const path = [wrappedNative, opp.tokenAddress];
      const amountIn = ethers.parseEther(positionETH.toFixed(6));
      const amounts = await routerRead.getAmountsOut(amountIn, path);
      const minOut = (amounts[1] * BigInt(100 - this.slippagePct)) / BigInt(100);

      const routerTx = new ethers.Contract(opp.routerAddress, ROUTER_TRADE_ABI, s.wallet);
      const deadline = Math.floor(Date.now() / 1000) + 300;

      // PRE-BUY SIMULATION: eth_call to catch whitelist honeypots BEFORE spending gas
      // This prevents the 14+ buy-failed reverts we saw (TAD-type honeypots)
      try {
        const routerIface = new ethers.Interface(ROUTER_TRADE_ABI);
        const buySimData = routerIface.encodeFunctionData(
          'swapExactETHForTokensSupportingFeeOnTransferTokens',
          [minOut, path, s.wallet.address, deadline],
        );
        await s.provider.call({
          from: s.wallet.address,
          to: opp.routerAddress,
          data: buySimData,
          value: amountIn,
        });
        this.logger.log(`${tag} Pre-buy sim OK: ${opp.tokenSymbol} buy would succeed`);
      } catch (err) {
        this.logger.warn(`${tag} PRE-BUY SIM FAILED: ${opp.tokenSymbol} — buy would revert: ${err.message?.slice(0, 120)}`);
        this.tradeLogger.logTrade({
          tokenAddress: opp.tokenAddress, tokenSymbol: opp.tokenSymbol, chain: s.chainConfig.id,
          pairAddress: opp.pairAddress, deployer: opp.deployer ?? null,
          liquidityNative: opp.poolEthReserve, bytecodeScore: opp.bytecodeScore ?? 0,
          hasNonLatinName: opp.hasNonLatinName ?? false, microTestPassed: opp.microTestPassed ?? true,
          microTestCostNative: opp.microTestCostNative ?? 0, securityGate: opp.securityGate ?? 'approved',
          result: 'buy-failed', pnlPct: 0, pnlNative: 0, pnlUSD: 0,
          ethSpent: 0, ethReceived: 0, lotsSold: 0, holdTimeMs: 0,
          timestamp: Date.now(), buyTxHash: '', failReason: `pre-buy-sim: ${err.message?.slice(0, 80)}`,
        });
        return false;
      }

      // Normal priority for buys — we already waited 5min observation, no need to rush
      const tx = await routerTx.swapExactETHForTokensSupportingFeeOnTransferTokens(
        minOut, path, s.wallet.address, deadline,
        { value: amountIn, gasLimit: 300000, maxFeePerGas: feeData.maxFeePerGas, maxPriorityFeePerGas: feeData.maxPriorityFeePerGas },
      );

      this.logger.log(`${tag} Buy TX sent: ${tx.hash}`);
      const receipt = await tx.wait(1);
      if (!receipt || receipt.status === 0) {
        this.logger.error(`${tag} Buy TX failed (reverted)`);
        const gasCost = receipt ? parseFloat(ethers.formatEther(receipt.gasUsed * receipt.gasPrice)) : 0;
        this.tradeLogger.logTrade({
          tokenAddress: opp.tokenAddress, tokenSymbol: opp.tokenSymbol, chain: s.chainConfig.id,
          pairAddress: opp.pairAddress, deployer: opp.deployer ?? null,
          liquidityNative: opp.poolEthReserve, bytecodeScore: opp.bytecodeScore ?? 0,
          hasNonLatinName: opp.hasNonLatinName ?? false, microTestPassed: opp.microTestPassed ?? true,
          microTestCostNative: opp.microTestCostNative ?? 0, securityGate: opp.securityGate ?? 'approved',
          result: 'buy-failed', pnlPct: 0, pnlNative: -gasCost, pnlUSD: 0,
          ethSpent: positionETH, ethReceived: 0, lotsSold: 0, holdTimeMs: 0,
          timestamp: Date.now(), buyTxHash: tx.hash, failReason: 'tx-reverted',
        });
        s.pausedUntil = Date.now() + 5 * 60 * 1000;
        return false;
      }

      const tokenBalanceAfter = await tokenContractRead.balanceOf(s.wallet.address);
      // Only count tokens gained from THIS buy, not micro-test residuals
      const tokensBought = tokenBalanceAfter - tokenBalanceBefore;
      const tokenBalance = tokensBought > BigInt(0) ? tokensBought : tokenBalanceAfter;
      const entryPrice = this.calculatePrice(positionETH, tokenBalance, decimals);

      this.logger.debug(`${tag} Price calc: ${positionETH} ${s.chainConfig.nativeSymbol} / ${ethers.formatUnits(tokenBalance, decimals)} tokens (${decimals}d) = ${entryPrice}`);

      const trade: ActiveTrade = {
        chainId: s.chainId,
        tokenAddress: opp.tokenAddress,
        tokenCreator: null,
        pairAddress: opp.pairAddress,
        routerAddress: opp.routerAddress,
        tokenSymbol: opp.tokenSymbol,
        tokenDecimals: decimals,
        entryPriceETH: entryPrice,
        tokensBought: tokenBalance,
        ethSpent: positionETH,
        buyTxHash: tx.hash,
        buyTimestamp: Date.now(),
        sellStarted: false,
        lotsSold: 0,
        totalEthReceived: 0,
        sellRetryCount: 0,
        sellStartedAt: null,
        // Analytics metadata
        liquidityNative: opp.poolEthReserve,
        bytecodeScore: opp.bytecodeScore ?? 0,
        hasNonLatinName: opp.hasNonLatinName ?? false,
        microTestPassed: opp.microTestPassed ?? true,
        microTestCostNative: opp.microTestCostNative ?? 0,
        securityGate: opp.securityGate ?? 'approved',
        deployer: opp.deployer ?? null,
      };
      s.activeTrades.push(trade);
      this.persistTrades();

      this.logger.log(`${tag} BUY OK: ${opp.tokenSymbol} | ${positionETH.toFixed(4)} ${s.chainConfig.nativeSymbol}`);

      // Watchdog: trade opened
      this.watchdog.onTradeOpened(s.chainId, opp.tokenAddress, opp.tokenSymbol, positionETH);
      this.watchdog.updateWalletBalance(s.chainId, s.chainConfig.name, s.chainConfig.nativeSymbol, s.lastKnownBalance);

      const testLabel = this.isTestMode(s.chainId) ? ' [TEST]' : '';
      await this.telegram.sendTradeNotification(
        `🟢 BUY EXECUTED${testLabel}`, opp.tokenSymbol, opp.tokenAddress, positionETH, tx.hash,
        `Entry: ${this.formatWithUSD(positionETH, s.chainConfig.nativeSymbol)}\nGas: ${gasGwei.toFixed(1)} gwei`,
        s.chainConfig,
      );

      // ④ PRE-APPROVE for instant sells (no approve delay during emergency)
      try {
        const tokenForApproval = new ethers.Contract(opp.tokenAddress, ERC20_TRADE_ABI, s.wallet);
        const approveTx = await tokenForApproval.approve(opp.routerAddress, ethers.MaxUint256, { gasLimit: 100000 });
        await approveTx.wait(1);
        this.logger.log(`${tag} Pre-approved ${opp.tokenSymbol} for router`);
      } catch (err) {
        this.logger.warn(`${tag} Pre-approve failed (will approve at sell time): ${err.message}`);
      }

      // ⑤ POST-BUY VERIFICATION: eth_call simulation with FULL token amount
      // This catches amount-dependent honeypots that allow micro sells but block real ones
      const canSell = await this.verifyPostBuySellability(s, trade);
      if (!canSell) {
        this.logger.warn(`${tag} POST-BUY CHECK FAILED: ${opp.tokenSymbol} sell simulation reverted — HONEYPOT`);
        this.security.recordSellFailure(opp.tokenAddress);
        this.watchdog.onPostBuyHoneypot();
        await this.telegram.sendTradeNotification(
          '🚫 HONEYPOT DETECTED POST-BUY', opp.tokenSymbol, opp.tokenAddress, positionETH, tx.hash,
          `Sell simulation reverted with full amount.\nTokens stuck — skipping sell to save gas.\nLoss: ${this.formatWithUSD(positionETH, s.chainConfig.nativeSymbol)}`,
          s.chainConfig,
        );
        // DON'T attempt to sell — if eth_call reverts, real TX will also revert
        // and we'd just waste gas. Close the trade as a loss.
        this.watchdog.onTradeClosed(s.chainId, trade.tokenAddress, -positionETH, -positionETH * this.getNativePriceUSD(s.chainConfig.nativeSymbol), 0);
        this.removeTrade(s, trade);
        return false;
      }

      // ⑥ POST-PURCHASE: Prepare pre-signed sell + start mempool guard + hold timer
      await this.prepareEmergencySell(s, trade);
      this.startMempoolGuard(s, trade);
      this.scheduleSell(s, trade);

      // ⑦ ASYNC GOPLUS CHECK: Cross-check with GoPlus API during hold period
      // Non-blocking — runs in background, triggers emergency sell if flagged
      this.runGoPlusGuard(s, trade).catch((err) => {
        this.logger.debug(`${tag} GoPlus guard error: ${err.message}`);
      });

      return true;
    } catch (err) {
      this.logger.error(`${tag} Buy failed: ${err.message}`);
      this.tradeLogger.logTrade({
        tokenAddress: opp.tokenAddress, tokenSymbol: opp.tokenSymbol, chain: s.chainConfig.id,
        pairAddress: opp.pairAddress, deployer: opp.deployer ?? null,
        liquidityNative: opp.poolEthReserve, bytecodeScore: opp.bytecodeScore ?? 0,
        hasNonLatinName: opp.hasNonLatinName ?? false, microTestPassed: opp.microTestPassed ?? true,
        microTestCostNative: opp.microTestCostNative ?? 0, securityGate: opp.securityGate ?? 'approved',
        result: 'buy-failed', pnlPct: 0, pnlNative: 0, pnlUSD: 0,
        ethSpent: 0, ethReceived: 0, lotsSold: 0, holdTimeMs: 0,
        timestamp: Date.now(), buyTxHash: '', failReason: err.message?.slice(0, 120) || 'unknown',
      });
      s.pausedUntil = Date.now() + 5 * 60 * 1000;
      return false;
    }
  }

  // ============================================================
  // POST-PURCHASE: Mempool monitoring (frontrun the rug)
  //
  // Watches pending TXs targeting the token contract.
  // If owner calls blacklist/setFee/pause → emergency sell BEFORE
  // the owner's TX gets confirmed.
  // ============================================================

  // ============================================================
  // GOPLUS ASYNC GUARD: Background honeypot cross-check during hold
  // If GoPlus flags the token, trigger immediate sell
  // ============================================================

  private async runGoPlusGuard(s: ChainTradingState, trade: ActiveTrade): Promise<void> {
    const tag = `[${s.chainConfig.name}]`;
    // Wait 10 seconds for GoPlus to index the token
    await new Promise((r) => setTimeout(r, 10_000));

    // Check if trade is still active
    if (!s.activeTrades.includes(trade)) return;

    const result = await this.security.checkGoPlus(trade.tokenAddress, s.chainConfig.id);

    // Check if trade is still active after async call
    if (!s.activeTrades.includes(trade)) return;

    if (result.isHoneypot || result.cannotSellAll || (result.sellTax !== null && result.sellTax > 30)) {
      this.logger.warn(`${tag} ⚠️ GOPLUS ALERT for ${trade.tokenSymbol}: ${result.summary} — triggering emergency sell`);
      await this.telegram.sendRawMessage(
        `⚠️ <b>GoPlus Alert: ${trade.tokenSymbol}</b>\n${result.summary}\nTriggering early sell...`,
      );
      // Trigger immediate sell instead of waiting for hold timer
      this.enqueueSell(s, () => this.executeLotSell(s, trade), true);
    } else {
      this.logger.log(`${tag} GoPlus OK for ${trade.tokenSymbol}: ${result.summary}`);
    }
  }

  // ============================================================
  // PRE-SIGNED TX: Prepare emergency sell immediately after buy
  // Eliminates: getBalance, getAllowance, getFeeData, encode during emergency
  // ============================================================

  private async prepareEmergencySell(s: ChainTradingState, trade: ActiveTrade): Promise<void> {
    const tag = `[${s.chainConfig.name}]`;
    try {
      const routerInterface = new ethers.Interface(ROUTER_TRADE_ABI);
      const tokenBalance = trade.tokensBought;
      const swapPath = [trade.tokenAddress, s.chainConfig.wrappedNative];
      const deadline = Math.floor(Date.now() / 1000) + 86400; // 24h far-future deadline

      // Pre-encode the swap calldata
      trade.preSignedSellData = routerInterface.encodeFunctionData(
        'swapExactTokensForETHSupportingFeeOnTransferTokens',
        [tokenBalance, BigInt(0), swapPath, s.wallet.address, deadline],
      );

      // Cache nonce (will be current pending nonce)
      trade.cachedNonce = await s.provider.getTransactionCount(s.wallet.address, 'pending');
      trade.cachedTokenBalance = tokenBalance;

      this.logger.log(`${tag} Pre-signed sell READY for ${trade.tokenSymbol} (nonce: ${trade.cachedNonce})`);
    } catch (err) {
      this.logger.warn(`${tag} Pre-sign failed for ${trade.tokenSymbol}: ${err.message}`);
    }
  }

  // ============================================================
  // RAW WEBSOCKET MEMPOOL GUARD (replaces ethers.js listener)
  //
  // Uses raw WebSocket with `newPendingTransactions(true)` to get
  // FULL TX objects — eliminates getTransaction() roundtrip entirely.
  //
  // 5-layer filter:
  //   1. TX.to === router address?
  //   2. TX.data has removeLiquidity selector?
  //   3. TX.data contains our token address?
  //   4. TX.from === token owner/deployer?
  //   5. All passed → EMERGENCY SELL
  // ============================================================

  private startMempoolGuard(s: ChainTradingState, trade: ActiveTrade) {
    const { wssUrl } = this.getChainTradingConfig(s.chainConfig);
    if (!wssUrl) {
      this.logger.warn(`[${s.chainConfig.name}] No WSS URL — mempool monitoring disabled`);
      return;
    }

    const tag = `[${s.chainConfig.name}]`;
    const tokenLower = trade.tokenAddress.toLowerCase();
    const tokenLowerNoPrefix = tokenLower.slice(2);
    const routerLower = trade.routerAddress.toLowerCase();
    const ownerLower = trade.tokenCreator?.toLowerCase() || null;

    this.logger.log(`${tag} Mempool guard ACTIVE for ${trade.tokenSymbol} (raw WS, full TX mode)`);
    if (ownerLower) {
      this.logger.log(`${tag}   Owner filter: ${ownerLower}`);
    }

    let emergencyTriggered = false;
    let ws: WebSocket | null = null;
    let subId: string | null = null;

    const connectWs = () => {
      ws = new WebSocket(wssUrl);

      ws.on('open', () => {
        // Subscribe to full pending transactions (true = full TX objects)
        ws!.send(JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'eth_subscribe',
          params: ['newPendingTransactions', true],
        }));
        this.logger.log(`${tag} Raw WS connected for ${trade.tokenSymbol}`);
      });

      ws.on('message', (data: Buffer) => {
        if (emergencyTriggered || trade.sellStarted) return;

        try {
          const msg = JSON.parse(data.toString());

          // Store subscription ID
          if (msg.id === 1 && msg.result) {
            subId = msg.result;
            return;
          }

          // Process pending TX
          const tx = msg.params?.result;
          if (!tx || !tx.to) return;

          const toLower = (tx.to as string).toLowerCase();

          // FILTER 1: Only router calls (removeLiquidity goes through router)
          if (toLower !== routerLower) return;

          // FILTER 2: Is it a removeLiquidity selector?
          const selector = tx.input?.slice(2, 10) || tx.data?.slice(2, 10);
          if (!selector || !REMOVE_LIQUIDITY_SELECTORS.includes(selector)) return;

          // FILTER 3: Does TX data contain our token address?
          const txData = (tx.input || tx.data || '').toLowerCase();
          if (!txData.includes(tokenLowerNoPrefix)) return;

          // FILTER 4: Is it from the token owner/deployer?
          const fromLower = (tx.from as string).toLowerCase();
          if (ownerLower && fromLower !== ownerLower) return;

          // ALL FILTERS PASSED — EMERGENCY SELL
          if (emergencyTriggered) return;
          emergencyTriggered = true;

          const ownerGasPrice = BigInt(tx.gasPrice || tx.maxFeePerGas || '0');

          this.logger.warn(`${tag} 🚨 MEMPOOL: removeLiquidity detected for ${trade.tokenSymbol}!`);
          this.logger.warn(`${tag}   From: ${tx.from} | Selector: 0x${selector} | Owner gas: ${ethers.formatUnits(ownerGasPrice, 'gwei')} gwei`);

          if (!trade.sellStarted) {
            // Fire emergency sell with owner's gas info for dynamic pricing
            const sellPromise = this.emergencySell(s, trade, ownerGasPrice);

            // Notify Telegram in parallel (non-blocking)
            this.telegram.sendTradeNotification(
              '🚨 RUG DETECTED — EMERGENCY SELL',
              trade.tokenSymbol,
              trade.tokenAddress,
              0,
              tx.hash || 'pending',
              `removeLiquidity detected in mempool!\nOwner: ${tx.from}\nFrontrunning with 5x gas...`,
              s.chainConfig,
            ).catch(() => {});

            sellPromise.catch(() => {});
          }
        } catch {
          // Ignore parse errors on hot path
        }
      });

      ws.on('error', (err) => {
        this.logger.debug(`${tag} Mempool WS error: ${err.message}`);
      });

      ws.on('close', () => {
        if (!emergencyTriggered && !trade.sellStarted) {
          this.logger.debug(`${tag} Mempool WS closed, reconnecting in 3s...`);
          setTimeout(connectWs, 3000);
        }
      });
    };

    connectWs();

    trade.mempoolUnsub = () => {
      emergencyTriggered = true; // prevent reconnect
      try {
        if (ws && ws.readyState === WebSocket.OPEN) {
          if (subId) {
            ws.send(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'eth_unsubscribe', params: [subId] }));
          }
          ws.close();
        }
      } catch {}
      ws = null;
      this.logger.log(`${tag} Mempool guard stopped for ${trade.tokenSymbol}`);
    };
  }

  /**
   * Ultra-fast emergency sell with pre-signed TX + dynamic gas + multi-RPC broadcast.
   *
   * Flow (2-5ms from detection to broadcast):
   *   1. Use pre-encoded sell calldata (no RPC calls needed)
   *   2. Calculate gas: MAX(ownerGas * 3, ownerGas + 2 wei)
   *   3. Sign TX locally (~1ms)
   *   4. Broadcast to multiple RPCs simultaneously (Promise.any)
   */
  private async emergencySell(
    s: ChainTradingState,
    trade: ActiveTrade,
    ownerGasPrice: bigint = BigInt(0),
  ): Promise<void> {
    const tag = `[${s.chainConfig.name}]`;
    const startTime = Date.now();
    trade.sellStarted = true;
    trade.sellStartedAt = Date.now();

    try {
      // === FAST PATH: Use pre-signed TX if available ===
      if (trade.preSignedSellData && trade.cachedNonce !== undefined) {
        this.logger.warn(`${tag} 🚀 FAST PATH: Using pre-signed sell for ${trade.tokenSymbol}`);

        // Dynamic gas: MAX(ownerGas * 5, ownerGas + 2 wei) — 5x to beat owner in same block
        let gasPrice: bigint;
        if (ownerGasPrice > BigInt(0)) {
          const boostedGas = ownerGasPrice * BigInt(5);
          const plusTwoWei = ownerGasPrice + BigInt(2);
          gasPrice = boostedGas > plusTwoWei ? boostedGas : plusTwoWei;
        } else {
          // Fallback: fetch current gas and use 5x
          const feeData = await s.provider.getFeeData();
          gasPrice = (feeData.gasPrice || BigInt(5000000000)) * BigInt(5);
        }

        // Build TX object with pre-encoded data
        const txRequest: ethers.TransactionRequest = {
          to: trade.routerAddress,
          data: trade.preSignedSellData,
          nonce: trade.cachedNonce,
          gasLimit: 300000,
          chainId: s.chainConfig.chainIdNum,
          type: 0, // legacy TX for maximum compatibility + speed
          gasPrice,
        };

        // Sign locally (~1ms)
        const signedTx = await s.wallet.signTransaction(txRequest);
        const elapsed = Date.now() - startTime;
        this.logger.warn(`${tag} 🚀 Signed in ${elapsed}ms | Gas: ${ethers.formatUnits(gasPrice, 'gwei')} gwei`);

        // Broadcast to multiple RPCs simultaneously
        const txHash = await this.broadcastToMultipleRPCs(signedTx, s.chainConfig.id, s);
        this.logger.warn(`${tag} 🚨 Emergency sell broadcast in ${Date.now() - startTime}ms | TX: ${txHash}`);

        // Wait for confirmation (non-blocking for the fast path metric)
        try {
          const receipt = await s.provider.waitForTransaction(txHash, 1, 30000);
          if (receipt && receipt.status === 1) {
            const balAfter = await this.getBalanceNative(s);
            trade.totalEthReceived = Math.max(balAfter - s.lastKnownBalance + trade.ethSpent, 0);
            trade.lotsSold = 1;
            this.logger.warn(`${tag} ✅ Emergency sell CONFIRMED for ${trade.tokenSymbol} in ${Date.now() - startTime}ms`);
          } else {
            this.logger.error(`${tag} Emergency sell TX FAILED — falling back to slow path`);
            trade.sellStarted = false;
            await this.emergencySellSlowPath(s, trade);
            return;
          }
        } catch (waitErr) {
          this.logger.warn(`${tag} TX confirmation timeout — may still confirm. Hash: ${txHash}`);
        }

      } else {
        // === SLOW PATH: No pre-signed TX, do it the old way ===
        this.logger.warn(`${tag} ⚠️ SLOW PATH: No pre-signed TX for ${trade.tokenSymbol}`);
        await this.emergencySellSlowPath(s, trade);
        return;
      }

      // Log P&L
      const pnl = trade.totalEthReceived - trade.ethSpent;
      const pnlPct = trade.ethSpent > 0 ? (pnl / trade.ethSpent) * 100 : -100;
      this.logTradeRecord(trade, s, pnl, pnlPct);
      this.removeTrade(s, trade);

    } catch (err) {
      this.logger.error(`${tag} Emergency sell error: ${err.message}`);
      trade.sellStarted = false;
      try {
        await this.emergencySellSlowPath(s, trade);
      } catch (e2) {
        this.logger.error(`${tag} Slow path also failed: ${e2.message}`);
        this.removeTrade(s, trade);
      }
    }
  }

  /**
   * Slow-path emergency sell: fetches everything from chain.
   * Used as fallback when pre-signed TX is not available or fails.
   */
  private async emergencySellSlowPath(s: ChainTradingState, trade: ActiveTrade): Promise<void> {
    const tag = `[${s.chainConfig.name}]`;
    trade.sellStarted = true;
    trade.sellStartedAt = Date.now();

    try {
      const tokenBalance = await this.getTokenBalance(trade.tokenAddress, s);
      if (tokenBalance <= BigInt(0)) {
        this.logger.warn(`${tag} Emergency sell: no tokens to sell`);
        this.removeTrade(s, trade);
        return;
      }

      // Check allowance — should be pre-approved, but verify
      const tokenContract = new ethers.Contract(trade.tokenAddress, ERC20_TRADE_ABI, s.wallet);
      const currentAllowance = await tokenContract.allowance(s.wallet.address, trade.routerAddress);
      if (currentAllowance < tokenBalance) {
        const feeData = await s.provider.getFeeData();
        const approveTx = await tokenContract.approve(trade.routerAddress, ethers.MaxUint256, {
          gasLimit: 100000,
          gasPrice: feeData.gasPrice ? feeData.gasPrice * BigInt(3) : undefined,
        });
        await approveTx.wait(1);
      }

      const feeData = await s.provider.getFeeData();
      const gasPrice = (feeData.gasPrice || BigInt(5000000000)) * BigInt(3);
      const router = new ethers.Contract(trade.routerAddress, ROUTER_TRADE_ABI, s.wallet);
      const swapPath = [trade.tokenAddress, s.chainConfig.wrappedNative];
      const deadline = Math.floor(Date.now() / 1000) + 60;

      const sellTx = await router.swapExactTokensForETHSupportingFeeOnTransferTokens(
        tokenBalance, BigInt(0), swapPath, s.wallet.address, deadline,
        { gasLimit: 300000, gasPrice },
      );

      this.logger.warn(`${tag} 🚨 Slow-path emergency sell TX: ${sellTx.hash}`);
      const receipt = await this.waitForTx(sellTx);

      if (receipt && receipt.status === 1) {
        const balAfter = await this.getBalanceNative(s);
        trade.totalEthReceived = Math.max(balAfter - s.lastKnownBalance + trade.ethSpent, 0);
        trade.lotsSold = 1;
        this.logger.warn(`${tag} ✅ Slow-path sell SUCCESS for ${trade.tokenSymbol}`);
      } else {
        this.logger.error(`${tag} Slow-path sell TX FAILED for ${trade.tokenSymbol}`);
      }

      const pnl = trade.totalEthReceived - trade.ethSpent;
      const pnlPct = trade.ethSpent > 0 ? (pnl / trade.ethSpent) * 100 : -100;
      this.logTradeRecord(trade, s, pnl, pnlPct);
      this.removeTrade(s, trade);

    } catch (err) {
      this.logger.error(`${tag} Slow-path sell error: ${err.message}`);
      try {
        await this.enqueueSell(s, () => this.executeLotSell(s, trade));
      } catch (e2) {
        this.logger.error(`${tag} Fallback lot sell also failed: ${e2.message}`);
        this.removeTrade(s, trade);
      }
    }
  }

  /**
   * Broadcast signed TX to multiple RPC endpoints simultaneously.
   * Returns the TX hash as soon as ANY endpoint accepts it.
   */
  private async broadcastToMultipleRPCs(
    signedTx: string,
    chainId: ChainId,
    s: ChainTradingState,
  ): Promise<string> {
    const tag = `[${s.chainConfig.name}]`;
    const endpoints = [
      ...(BROADCAST_RPCS[chainId] || []),
    ];

    // Also add user's own RPC as primary
    const { rpcUrl } = this.getChainTradingConfig(s.chainConfig);
    if (rpcUrl) endpoints.unshift(rpcUrl);

    const payload = JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'eth_sendRawTransaction',
      params: [signedTx],
    });

    this.logger.log(`${tag} Broadcasting to ${endpoints.length} RPCs...`);

    const requests = endpoints.map(async (url) => {
      try {
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: payload,
          signal: AbortSignal.timeout(5000),
        });
        const json = await res.json() as any;
        if (json.error) throw new Error(json.error.message);
        return json.result as string; // TX hash
      } catch (err) {
        this.logger.debug(`${tag} Broadcast to ${url} failed: ${err.message}`);
        throw err;
      }
    });

    // Return as soon as ANY RPC accepts the TX (es2017-compatible Promise.any polyfill)
    return new Promise<string>((resolve, reject) => {
      let errors = 0;
      const total = requests.length;
      for (const req of requests) {
        req.then(resolve).catch(() => { errors++; if (errors >= total) reject(new Error('All RPCs failed')); });
      }
    });
  }

  // ============================================================
  // Lot sell
  // ============================================================

  private scheduleSell(s: ChainTradingState, trade: ActiveTrade) {
    setTimeout(async () => {
      if (!trade.sellStarted) {
        await this.enqueueSell(s, () => this.executeLotSell(s, trade));
      }
    }, this.holdTimeMs);
  }

  private async executeLotSell(s: ChainTradingState, trade: ActiveTrade) {
    if (!trade) return;

    const tag = `[${s.chainConfig.name}]`;
    const wrappedNative = s.chainConfig.wrappedNative;
    trade.sellStarted = true;
    trade.sellStartedAt = Date.now();

    // Watchdog: sell started
    this.watchdog.onSellStarted(s.chainId, trade.tokenAddress);

    // Stop mempool monitoring
    if (trade.mempoolUnsub) {
      trade.mempoolUnsub();
    }

    this.logger.log(`${tag} Starting lot sell for ${trade.tokenSymbol}...`);

    try {
      const tokenContract = new ethers.Contract(trade.tokenAddress, ERC20_TRADE_ABI, s.wallet);
      const currentAllowance = await tokenContract.allowance(s.wallet.address, trade.routerAddress);
      if (currentAllowance < trade.tokensBought) {
        const approveTx = await tokenContract.approve(trade.routerAddress, ethers.MaxUint256, { gasLimit: 100000 });
        await this.waitForTx(approveTx);
        this.logger.log(`${tag} Token approved`);
      }

      const poolInfo = await this.getPoolReserves(trade.pairAddress, s);
      if (!poolInfo) {
        await this.sellTokens(trade, trade.tokensBought, 'emergency-dump', s);
        this.removeTrade(s, trade);
        return;
      }

      const maxLotTokens = (poolInfo.tokenReserve * BigInt(Math.floor(this.maxPoolPct * 100))) / BigInt(10000);
      const remainingTokens = await this.getTokenBalance(trade.tokenAddress, s);
      const lotCount = Math.max(Math.ceil(Number(remainingTokens) / Number(maxLotTokens)), 1);

      this.logger.log(`${tag} Sell plan: ${lotCount} lots`);

      for (let i = 0; i < lotCount; i++) {
        const tokensLeft = await this.getTokenBalance(trade.tokenAddress, s);
        if (tokensLeft <= BigInt(0)) break;

        const lotSize = i === lotCount - 1 ? tokensLeft : maxLotTokens < tokensLeft ? maxLotTokens : tokensLeft;

        const currentPrice = await this.getCurrentPrice(trade.pairAddress, trade.tokenAddress, s, trade.tokenDecimals);
        if (currentPrice > 0) {
          const priceDrop = ((trade.entryPriceETH - currentPrice) / trade.entryPriceETH) * 100;
          if (priceDrop >= this.stopLossPct) {
            this.logger.warn(`${tag} STOP-LOSS: -${priceDrop.toFixed(1)}%`);
            await this.sellTokens(trade, tokensLeft, 'stop-loss', s);
            break;
          }
        }

        const ethReceived = await this.sellTokens(trade, lotSize, `lot-${i + 1}`, s);
        trade.lotsSold++;
        trade.totalEthReceived += ethReceived;

        if (i >= 1 && i < lotCount - 1) {
          await this.sleep(30000);
        }
      }

      // P&L
      const pnl = trade.totalEthReceived - trade.ethSpent;
      const pnlPct = (pnl / trade.ethSpent) * 100;
      const isWin = pnl >= 0;

      if (!isWin) {
        s.consecutiveLosses++;
        if (s.consecutiveLosses >= this.maxConsecutiveLosses) {
          s.pausedUntil = Date.now() + 60 * 60 * 1000;
          this.logger.warn(`${tag} ${this.maxConsecutiveLosses} consecutive losses — pausing 1h`);
        }
      } else {
        s.consecutiveLosses = 0;
      }

      this.logger.log(`${tag} CLOSED: ${trade.tokenSymbol} | ${isWin ? 'WIN' : 'LOSS'} | ${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)} ${s.chainConfig.nativeSymbol} (${pnlPct.toFixed(1)}%)`);

      const ns = s.chainConfig.nativeSymbol;
      const testLabel2 = this.isTestMode(s.chainId) ? ' [TEST]' : '';
      await this.telegram.sendTradeNotification(
        `${isWin ? '✅ TRADE WON' : '❌ TRADE LOST'}${testLabel2}`,
        trade.tokenSymbol, trade.tokenAddress, trade.totalEthReceived, trade.buyTxHash,
        `Spent: ${this.formatWithUSD(trade.ethSpent, ns)}\nReceived: ${this.formatWithUSD(trade.totalEthReceived, ns)}\nP&L: ${pnl >= 0 ? '+' : '-'}${this.formatWithUSD(Math.abs(pnl), ns)} (${pnlPct.toFixed(1)}%)\nLots: ${trade.lotsSold}`,
        s.chainConfig,
      );

      // Log trade for pattern analysis
      this.logTradeRecord(trade, s, pnl, pnlPct);

      // Watchdog: trade closed
      const holdTime = Date.now() - trade.buyTimestamp;
      const nativePrice = this.getNativePriceUSD(s.chainConfig.nativeSymbol);
      this.watchdog.onTradeClosed(s.chainId, trade.tokenAddress, pnl, pnl * nativePrice, holdTime);
      if (pnlPct <= -95) this.watchdog.onHoneypotLoss();

      this.removeTrade(s, trade);
    } catch (err) {
      this.logger.error(`${tag} Lot sell error: ${err.message}`);
      try {
        const remaining = await this.getTokenBalance(trade.tokenAddress, s);
        if (remaining > BigInt(0)) await this.sellTokens(trade, remaining, 'emergency', s);
      } catch (e2) {
        this.logger.error(`${tag} Emergency dump failed: ${e2.message}`);
      }

      // Log failed trade too
      const emergencyPnl = trade.totalEthReceived - trade.ethSpent;
      const emergencyPnlPct = trade.ethSpent > 0 ? (emergencyPnl / trade.ethSpent) * 100 : -100;
      this.logTradeRecord(trade, s, emergencyPnl, emergencyPnlPct);

      this.removeTrade(s, trade);
    }
  }

  private async sellTokens(
    trade: ActiveTrade, tokenAmount: bigint, label: string, s: ChainTradingState,
  ): Promise<number> {
    const tag = `[${s.chainConfig.name}]`;
    const wrappedNative = s.chainConfig.wrappedNative;
    const router = new ethers.Contract(trade.routerAddress, ROUTER_TRADE_ABI, s.wallet);
    const path = [trade.tokenAddress, wrappedNative];
    const deadline = Math.floor(Date.now() / 1000) + 300;

    let minOut = BigInt(0);
    try {
      const routerRead = new ethers.Contract(trade.routerAddress, ROUTER_TRADE_ABI, s.provider);
      const amounts = await routerRead.getAmountsOut(tokenAmount, path);
      minOut = (amounts[1] * BigInt(100 - this.slippagePct)) / BigInt(100);
    } catch {}

    const balBefore = await s.provider.getBalance(s.wallet.address);
    const tx = await router.swapExactTokensForETHSupportingFeeOnTransferTokens(
      tokenAmount, minOut, path, s.wallet.address, deadline, { gasLimit: 300000 },
    );

    let receipt: any = null;
    try {
      receipt = await this.waitForTx(tx);
    } catch (e) {
      // Timeout — TX may still confirm. Check balance to detect success.
      this.logger.warn(`${tag} ${label} sell TX timeout (hash: ${tx.hash}) — checking balance...`);
      await new Promise((r) => setTimeout(r, 15000)); // wait 15s more
      const balCheck = await s.provider.getBalance(s.wallet.address);
      const balDiff = parseFloat(ethers.formatEther(balCheck - balBefore));
      if (balDiff > 0) {
        this.logger.log(`${tag} ${label} sell confirmed via balance check: +${balDiff.toFixed(6)} ETH`);
        return balDiff;
      }
      this.logger.warn(`${tag} ${label} sell truly failed — no balance change`);
      this.security.recordSellFailure(trade.tokenAddress);
      this.watchdog.onSellFailure();
      return 0;
    }

    if (!receipt || receipt.status === 0) {
      this.security.recordSellFailure(trade.tokenAddress);
      this.watchdog.onSellFailure();
      return 0;
    }

    const balAfter = await s.provider.getBalance(s.wallet.address);
    const ethReceived = parseFloat(ethers.formatEther(balAfter - balBefore));
    const gasCost = receipt.gasUsed * receipt.gasPrice;
    return Math.max(ethReceived + parseFloat(ethers.formatEther(gasCost)), 0);
  }

  // ============================================================
  // Stop-loss monitor (per-chain)
  // ============================================================

  private startStopLossMonitor(s: ChainTradingState) {
    const interval = s.chainConfig.tradingConfig.stopLossMonitorIntervalMs;
    setInterval(async () => {
      for (const trade of [...s.activeTrades]) {
        const elapsed = Date.now() - trade.buyTimestamp;

        // ① HARD DEADLINE: force-close trade after 10 minutes no matter what
        if (elapsed >= HARD_DEADLINE_MS && !trade.sellStarted) {
          this.logger.warn(`[${s.chainConfig.name}] HARD DEADLINE: ${trade.tokenSymbol} open for ${Math.round(elapsed / 60000)}min — force closing`);
          await this.forceCloseTrade(s, trade, 'hard deadline (10min)');
          continue;
        }

        // ② Stuck sell cleanup: sell started but not finishing
        if (trade.sellStarted && trade.sellStartedAt) {
          const stuckMs = Date.now() - trade.sellStartedAt;
          if (stuckMs > SELL_STUCK_TIMEOUT_MS) {
            trade.sellRetryCount = (trade.sellRetryCount || 0) + 1;
            if (trade.sellRetryCount >= MAX_SELL_RETRIES) {
              this.logger.warn(`[${s.chainConfig.name}] Abandoning stuck trade ${trade.tokenSymbol} after ${MAX_SELL_RETRIES} retries`);
              await this.forceCloseTrade(s, trade, `stuck after ${MAX_SELL_RETRIES} sell retries`);
            } else {
              this.logger.warn(`[${s.chainConfig.name}] Resetting stuck trade ${trade.tokenSymbol} (retry ${trade.sellRetryCount}/${MAX_SELL_RETRIES})`);
              trade.sellStarted = false;
              trade.sellStartedAt = null;
              this.persistTrades();
            }
          }
          continue;
        }

        // Also force-close if sellStarted but stuckAt missing (shouldn't happen but safety)
        if (trade.sellStarted && elapsed >= HARD_DEADLINE_MS + SELL_STUCK_TIMEOUT_MS) {
          await this.forceCloseTrade(s, trade, 'stuck sell without timestamp');
          continue;
        }

        if (trade.sellStarted) continue;

        // ③ Grace period: don't check stop-loss in the first 60 seconds after buy
        if (elapsed < 60000) continue;

        try {
          const currentPrice = await this.getCurrentPrice(trade.pairAddress, trade.tokenAddress, s, trade.tokenDecimals);
          if (currentPrice <= 0 || trade.entryPriceETH <= 0) continue;
          const changePct = ((currentPrice - trade.entryPriceETH) / trade.entryPriceETH) * 100;
          this.logger.debug(`[${s.chainConfig.name}] ${trade.tokenSymbol} price: entry=${trade.entryPriceETH.toExponential(3)} cur=${currentPrice.toExponential(3)} change=${changePct.toFixed(1)}%`);

          if (changePct <= -this.stopLossPct) {
            this.logger.warn(`[${s.chainConfig.name}] STOP-LOSS HIT: ${trade.tokenSymbol} at ${changePct.toFixed(1)}%`);
            // Mark sellStarted BEFORE enqueuing to prevent duplicate enqueue on next interval
            trade.sellStarted = true;
            trade.sellStartedAt = Date.now();
            this.persistTrades();
            this.enqueueSell(s, () => this.executeLotSell(s, trade));
          }
        } catch {}
      }
    }, interval);
  }

  // ============================================================
  // Helpers
  // ============================================================

  // ============================================================
  // POST-BUY SELL VERIFICATION
  // ============================================================

  /**
   * Post-buy sell verification using eth_call to simulate the ACTUAL swap TX.
   * Unlike getAmountsOut (which is just math), this executes the full
   * token transfer logic including any hidden restrictions.
   *
   * Tests with FULL token balance to catch amount-dependent honeypots.
   */
  private async verifyPostBuySellability(s: ChainTradingState, trade: ActiveTrade): Promise<boolean> {
    const tag = `[${s.chainConfig.name}]`;
    try {
      const tokenBalance = trade.tokensBought;
      if (tokenBalance <= BigInt(0)) return true;

      const routerIface = new ethers.Interface(ROUTER_TRADE_ABI);
      const swapPath = [trade.tokenAddress, s.chainConfig.wrappedNative];
      const deadline = Math.floor(Date.now() / 1000) + 300;

      // Encode the FULL sell TX — same as we'd send on-chain
      const sellData = routerIface.encodeFunctionData(
        'swapExactTokensForETHSupportingFeeOnTransferTokens',
        [tokenBalance, BigInt(0), swapPath, s.wallet.address, deadline],
      );

      // eth_call simulates WITHOUT spending gas or broadcasting
      await s.provider.call({
        from: s.wallet.address,
        to: trade.routerAddress,
        data: sellData,
      });

      // If we reach here, eth_call didn't revert → sell would work
      this.logger.log(`${tag} Post-buy verify OK: ${trade.tokenSymbol} sell simulation passed (full amount)`);
      return true;
    } catch (err) {
      this.logger.warn(`${tag} Post-buy verify FAILED: ${trade.tokenSymbol} sell simulation reverted: ${err.message?.slice(0, 120)}`);
      return false;
    }
  }

  // ============================================================
  // SELL QUEUE: Serializes sells per chain to prevent nonce conflicts
  // ============================================================

  private async enqueueSell(s: ChainTradingState, sellFn: () => Promise<void>, priority = false): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const task = async () => {
        try { await sellFn(); resolve(); } catch (err) { reject(err); }
      };
      if (priority) {
        s.sellQueue.unshift(task); // Emergency sells go to front
      } else {
        s.sellQueue.push(task);
      }
      this.processSellQueue(s);
    });
  }

  private async processSellQueue(s: ChainTradingState): Promise<void> {
    if (s.sellQueueRunning) return;
    s.sellQueueRunning = true;
    this.watchdog.updateSellQueue(s.chainId, s.sellQueue.length, true);
    while (s.sellQueue.length > 0) {
      const fn = s.sellQueue.shift()!;
      this.watchdog.updateSellQueue(s.chainId, s.sellQueue.length, true);
      try {
        await fn();
      } catch (err) {
        this.logger.error(`[${s.chainConfig.name}] Sell queue task failed: ${err.message}`);
      }
    }
    s.sellQueueRunning = false;
    this.watchdog.updateSellQueue(s.chainId, 0, false);
  }

  private logTradeRecord(trade: ActiveTrade, s: ChainTradingState, pnl: number, pnlPct: number): void {
    const nativePrice = this.getNativePriceUSD(s.chainConfig.nativeSymbol);
    this.tradeLogger.logTrade({
      tokenAddress: trade.tokenAddress,
      tokenSymbol: trade.tokenSymbol,
      chain: s.chainId,
      pairAddress: trade.pairAddress,
      deployer: trade.deployer,
      liquidityNative: trade.liquidityNative,
      bytecodeScore: trade.bytecodeScore,
      hasNonLatinName: trade.hasNonLatinName,
      microTestPassed: trade.microTestPassed,
      microTestCostNative: trade.microTestCostNative,
      securityGate: trade.securityGate,
      result: pnl >= 0 ? 'win' : 'loss',
      pnlPct,
      pnlNative: pnl,
      pnlUSD: pnl * nativePrice,
      ethSpent: trade.ethSpent,
      ethReceived: trade.totalEthReceived,
      lotsSold: trade.lotsSold,
      holdTimeMs: Date.now() - trade.buyTimestamp,
      timestamp: Date.now(),
      buyTxHash: trade.buyTxHash,
    });
  }

  private removeTrade(s: ChainTradingState, trade: ActiveTrade) {
    const idx = s.activeTrades.indexOf(trade);
    if (idx !== -1) s.activeTrades.splice(idx, 1);
    this.persistTrades();
  }

  /** tx.wait with a hard timeout — prevents sell queue from hanging forever */
  private async waitForTx(tx: any, confirmations = 1): Promise<any> {
    return Promise.race([
      tx.wait(confirmations),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error('TX confirmation timeout')), TX_WAIT_TIMEOUT_MS),
      ),
    ]);
  }

  /** Force-close a trade: log as LOST, notify Telegram, remove from active trades */
  private async forceCloseTrade(s: ChainTradingState, trade: ActiveTrade, reason: string): Promise<void> {
    const tag = `[${s.chainConfig.name}]`;

    // Prevent double-close: if lot-sell already started, let it finish
    if (trade.sellStarted) {
      this.logger.debug(`${tag} Skipping force-close for ${trade.tokenSymbol} — sell already in progress`);
      return;
    }
    trade.sellStarted = true;

    this.logger.warn(`${tag} FORCE-CLOSING ${trade.tokenSymbol}: ${reason}`);

    // Try one last sell attempt (non-blocking, best effort)
    let sellOk = false;
    try {
      const remaining = await this.getTokenBalance(trade.tokenAddress, s);
      if (remaining > BigInt(0)) {
        const received = await this.sellTokens(trade, remaining, 'force-close', s);
        if (received > 0) {
          trade.lotsSold++;
          trade.totalEthReceived += received;
          sellOk = true;
        }
      }
    } catch (e) {
      this.logger.error(`${tag} Force-close sell failed: ${e.message}`);
    }

    const pnl = trade.totalEthReceived - trade.ethSpent;
    const pnlPct = trade.ethSpent > 0 ? (pnl / trade.ethSpent) * 100 : -100;
    const isWin = pnl >= 0;

    const ns = s.chainConfig.nativeSymbol;
    const testLabel = this.isTestMode(s.chainId) ? ' [TEST]' : '';
    await this.telegram.sendTradeNotification(
      `${isWin ? '✅ TRADE WON' : '❌ TRADE LOST'}${testLabel} (${reason})`,
      trade.tokenSymbol, trade.tokenAddress, trade.totalEthReceived, trade.buyTxHash,
      `Spent: ${this.formatWithUSD(trade.ethSpent, ns)}\nReceived: ${this.formatWithUSD(trade.totalEthReceived, ns)}\nP&L: ${pnl >= 0 ? '+' : '-'}${this.formatWithUSD(Math.abs(pnl), ns)} (${pnlPct.toFixed(1)}%)\nLots: ${trade.lotsSold}`,
      s.chainConfig,
    );

    this.logTradeRecord(trade, s, pnl, pnlPct);

    // Watchdog: force-closed trade
    const holdTime = Date.now() - trade.buyTimestamp;
    const nativePrice = this.getNativePriceUSD(s.chainConfig.nativeSymbol);
    this.watchdog.onTradeClosed(s.chainId, trade.tokenAddress, pnl, pnl * nativePrice, holdTime);
    if (pnlPct <= -95) this.watchdog.onHoneypotLoss();

    if (trade.mempoolUnsub) trade.mempoolUnsub();
    this.removeTrade(s, trade);
  }

  private getState(chainId?: ChainId): ChainTradingState | null {
    if (chainId) return this.chainStates.get(chainId) || null;
    const first = this.chainStates.values().next().value;
    return first || null;
  }

  private async getBalanceNative(s: ChainTradingState): Promise<number> {
    const bal = await s.provider.getBalance(s.wallet.address);
    return parseFloat(ethers.formatEther(bal));
  }

  private async getTokenBalance(tokenAddress: string, s: ChainTradingState): Promise<bigint> {
    const token = new ethers.Contract(tokenAddress, ERC20_TRADE_ABI, s.provider);
    return token.balanceOf(s.wallet.address);
  }

  private async getPoolReserves(
    pairAddress: string, s: ChainTradingState,
  ): Promise<{ ethReserve: bigint; tokenReserve: bigint } | null> {
    try {
      const pair = new ethers.Contract(pairAddress, PAIR_ABI_FRAGMENT, s.provider);
      const [reserves, token0] = await Promise.all([pair.getReserves(), pair.token0()]);
      const isWrapped0 = token0.toLowerCase() === s.chainConfig.wrappedNative.toLowerCase();
      return {
        ethReserve: isWrapped0 ? reserves[0] : reserves[1],
        tokenReserve: isWrapped0 ? reserves[1] : reserves[0],
      };
    } catch { return null; }
  }

  private async getCurrentPrice(
    pairAddress: string, tokenAddress: string, s: ChainTradingState, tokenDecimals = 18,
  ): Promise<number> {
    try {
      const pool = await this.getPoolReserves(pairAddress, s);
      if (!pool || pool.tokenReserve === BigInt(0)) return 0;
      return parseFloat(ethers.formatEther(pool.ethReserve)) / parseFloat(ethers.formatUnits(pool.tokenReserve, tokenDecimals));
    } catch { return 0; }
  }

  private calculatePrice(ethAmount: number, tokenAmount: bigint, tokenDecimals = 18): number {
    const tokens = parseFloat(ethers.formatUnits(tokenAmount, tokenDecimals));
    return tokens > 0 ? ethAmount / tokens : 0;
  }

  private getMaxGas(chain: ChainConfig): number {
    return this.config.get<number>('trading.maxGasGwei') || chain.tradingConfig.maxGasGweiDefault;
  }

  private getChainTradingConfig(chain: ChainConfig): { privateKey: string; rpcUrl: string; wssUrl: string; maxGasGwei: number } {
    if (chain.id === 'eth') {
      const httpUrl = this.config.get<string>('alchemy.httpUrl');
      const wssUrl = this.config.get<string>('alchemy.wssUrl');
      return {
        privateKey: this.config.get<string>('trading.privateKey') || '',
        rpcUrl: httpUrl || (wssUrl ? wssUrl.replace('wss://', 'https://') : ''),
        wssUrl: wssUrl || '',
        maxGasGwei: this.config.get<number>('trading.maxGasGwei') || chain.tradingConfig.maxGasGweiDefault,
      };
    }
    return {
      privateKey: this.config.get<string>(`${chain.id}.privateKey`) || '',
      rpcUrl: this.config.get<string>(`${chain.id}.httpUrl`) || '',
      wssUrl: this.config.get<string>(`${chain.id}.wssUrl`) || '',
      maxGasGwei: chain.tradingConfig.maxGasGweiDefault,
    };
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ============================================================
  // Native token USD price (Binance, cached 5min)
  // ============================================================

  private async updateNativePrices(): Promise<void> {
    try {
      const res = await fetch('https://api.binance.com/api/v3/ticker/price?symbols=["ETHUSDT","BNBUSDT"]');
      const data = await res.json() as Array<{ symbol: string; price: string }>;
      for (const item of data) {
        if (item.symbol === 'ETHUSDT') this.nativePricesUSD.ETH = parseFloat(item.price);
        if (item.symbol === 'BNBUSDT') this.nativePricesUSD.BNB = parseFloat(item.price);
      }
      this.priceLastUpdated = Date.now();
      this.logger.debug(`USD prices updated: ETH=$${this.nativePricesUSD.ETH.toFixed(0)} BNB=$${this.nativePricesUSD.BNB.toFixed(0)}`);
    } catch (err) {
      this.logger.warn(`Failed to fetch USD prices: ${err.message}`);
    }
  }

  // ============================================================
  // Trade state persistence — survive restarts
  // ============================================================

  private persistTrades(): void {
    try {
      const allTrades: Array<any> = [];
      for (const s of this.chainStates.values()) {
        for (const t of s.activeTrades) {
          allTrades.push({
            chainId: t.chainId,
            tokenAddress: t.tokenAddress,
            tokenCreator: t.tokenCreator,
            pairAddress: t.pairAddress,
            routerAddress: t.routerAddress,
            tokenSymbol: t.tokenSymbol,
            tokenDecimals: t.tokenDecimals,
            entryPriceETH: t.entryPriceETH,
            tokensBought: t.tokensBought.toString(),
            ethSpent: t.ethSpent,
            buyTxHash: t.buyTxHash,
            buyTimestamp: t.buyTimestamp,
            sellStarted: t.sellStarted,
            lotsSold: t.lotsSold,
            totalEthReceived: t.totalEthReceived,
            liquidityNative: t.liquidityNative,
            bytecodeScore: t.bytecodeScore,
            hasNonLatinName: t.hasNonLatinName,
            microTestPassed: t.microTestPassed,
            microTestCostNative: t.microTestCostNative,
            securityGate: t.securityGate,
            deployer: t.deployer,
            sellRetryCount: t.sellRetryCount,
            sellStartedAt: t.sellStartedAt,
          });
        }
      }
      fs.writeFileSync(this.TRADES_FILE, JSON.stringify(allTrades, null, 2));
    } catch (err) {
      this.logger.warn(`Failed to persist trades: ${err.message}`);
    }
  }

  private async restoreTrades(): Promise<void> {
    try {
      if (!fs.existsSync(this.TRADES_FILE)) return;
      const raw = fs.readFileSync(this.TRADES_FILE, 'utf-8');
      const trades: Array<any> = JSON.parse(raw);
      if (!trades.length) return;

      this.logger.log(`Restoring ${trades.length} active trade(s) from disk...`);

      for (const t of trades) {
        const s = this.chainStates.get(t.chainId as ChainId);
        if (!s || !s.enabled) continue;
        // Reset stuck trades instead of skipping
        if (t.sellStarted) {
          const retries = t.sellRetryCount || 0;
          if (retries >= MAX_SELL_RETRIES) {
            this.logger.warn(`[${s.chainConfig.name}] Dropping stuck trade ${t.tokenSymbol} (max retries)`);
            continue;
          }
          this.logger.warn(`[${s.chainConfig.name}] Resetting stuck trade ${t.tokenSymbol} for retry (${retries + 1}/${MAX_SELL_RETRIES})`);
          t.sellStarted = false;
          t.sellRetryCount = retries + 1;
        }

        // Check if we still hold tokens
        const tokenBalance = await this.getTokenBalance(t.tokenAddress, s);
        if (tokenBalance <= BigInt(0)) {
          this.logger.log(`[${s.chainConfig.name}] Restored trade ${t.tokenSymbol} — no tokens left, skipping`);
          continue;
        }

        const trade: ActiveTrade = {
          chainId: t.chainId,
          tokenAddress: t.tokenAddress,
          tokenCreator: t.tokenCreator,
          pairAddress: t.pairAddress,
          routerAddress: t.routerAddress,
          tokenSymbol: t.tokenSymbol,
          tokenDecimals: t.tokenDecimals,
          entryPriceETH: t.entryPriceETH,
          tokensBought: BigInt(t.tokensBought),
          ethSpent: t.ethSpent,
          buyTxHash: t.buyTxHash,
          buyTimestamp: t.buyTimestamp,
          sellStarted: false,
          lotsSold: t.lotsSold,
          totalEthReceived: t.totalEthReceived,
          liquidityNative: t.liquidityNative ?? 0,
          bytecodeScore: t.bytecodeScore ?? 0,
          hasNonLatinName: t.hasNonLatinName ?? false,
          microTestPassed: t.microTestPassed ?? true,
          microTestCostNative: t.microTestCostNative ?? 0,
          securityGate: t.securityGate ?? 'approved',
          deployer: t.deployer ?? null,
          sellRetryCount: t.sellRetryCount ?? 0,
          sellStartedAt: null, // reset on restore
        };

        s.activeTrades.push(trade);

        // Calculate remaining hold time
        const elapsed = Date.now() - trade.buyTimestamp;

        // If past hard deadline, force close immediately
        if (elapsed >= HARD_DEADLINE_MS) {
          this.logger.warn(`[${s.chainConfig.name}] Restored trade ${t.tokenSymbol} past deadline (${Math.round(elapsed / 60000)}min) — force closing`);
          await this.forceCloseTrade(s, trade, 'past deadline on restore');
          continue;
        }

        const remainingMs = Math.max(this.holdTimeMs - elapsed, 5000); // at least 5s

        this.logger.log(`[${s.chainConfig.name}] Restored: ${t.tokenSymbol} | hold remaining: ${Math.round(remainingMs / 1000)}s | tokens: ${ethers.formatUnits(tokenBalance, t.tokenDecimals)}`);

        // Re-start: prepare pre-signed sell + mempool guard + schedule sell
        await this.prepareEmergencySell(s, trade);
        this.startMempoolGuard(s, trade);
        setTimeout(async () => {
          if (!trade.sellStarted) {
            trade.sellStarted = true;
            trade.sellStartedAt = Date.now();
            this.persistTrades();
            await this.enqueueSell(s, () => this.executeLotSell(s, trade));
          }
        }, remainingMs);
      }

      // Clean up the file after restore
      fs.unlinkSync(this.TRADES_FILE);
    } catch (err) {
      this.logger.warn(`Failed to restore trades: ${err.message}`);
    }
  }

  /** Get cached USD price for a native symbol */
  getNativePriceUSD(nativeSymbol: string): number {
    return nativeSymbol === 'BNB' ? this.nativePricesUSD.BNB : this.nativePricesUSD.ETH;
  }

  /** Format native amount with USD equivalent: "0.0002 BNB ($0.13)" */
  formatWithUSD(amount: number, nativeSymbol: string): string {
    const usdPrice = nativeSymbol === 'BNB' ? this.nativePricesUSD.BNB : this.nativePricesUSD.ETH;
    const usd = amount * usdPrice;
    if (usdPrice > 0) {
      return `${amount.toFixed(4)} ${nativeSymbol} ($${usd.toFixed(2)})`;
    }
    return `${amount.toFixed(4)} ${nativeSymbol}`;
  }

  /** Check if we're in test mode for this chain */
  private isTestMode(chainId: ChainId): boolean {
    const testCaps: Record<string, number> = { bsc: 0.000127, base: 0.00004 };
    return !!testCaps[chainId];
  }
}
