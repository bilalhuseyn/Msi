import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { ethers } from 'ethers';
import { PAIR_ABI_FRAGMENT } from '../monitor/constants';
import { ChainId, ChainConfig, CHAIN_CONFIGS } from '../config/chains';
import { TelegramService } from '../telegram/telegram.service';

interface ActiveTrade {
  chainId: ChainId;
  tokenAddress: string;
  pairAddress: string;
  routerAddress: string;
  tokenSymbol: string;
  entryPriceETH: number;
  tokensBought: bigint;
  ethSpent: number;
  buyTxHash: string;
  buyTimestamp: number;
  sellStarted: boolean;
  lotsSold: number;
  totalEthReceived: number;
}

export interface TradeOpportunity {
  tokenAddress: string;
  tokenSymbol: string;
  pairAddress: string;
  routerAddress: string;
  poolEthReserve: number;
  buyTax: number;
  sellTax: number;
}

export interface MicroTestResult {
  success: boolean;
  costETH: number;
  reason?: string;
}

// Per-chain trading state
interface ChainTradingState {
  chainId: ChainId;
  chainConfig: ChainConfig;
  provider: ethers.JsonRpcProvider;
  wallet: ethers.Wallet;
  enabled: boolean;
  activeTrade: ActiveTrade | null;
  lastKnownBalance: number;
  dailyStartBalance: number;
  consecutiveLosses: number;
  pausedUntil: number;
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
];

@Injectable()
export class TradingService implements OnModuleInit {
  private readonly logger = new Logger(TradingService.name);
  private readonly chainStates = new Map<ChainId, ChainTradingState>();

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
  ) {}

  async onModuleInit() {
    this.positionPct = this.config.get<number>('trading.positionPct') || 50;
    this.maxPoolPct = this.config.get<number>('trading.maxPoolPct') || 3;
    this.slippagePct = this.config.get<number>('trading.slippagePct') || 5;
    this.holdTimeMs = this.config.get<number>('trading.holdTimeMs') || 10 * 60 * 1000;
    this.stopLossPct = this.config.get<number>('trading.stopLossPct') || 15;
    this.maxDailyLossPct = this.config.get<number>('trading.maxDailyLossPct') || 30;
    this.maxConsecutiveLosses = this.config.get<number>('trading.maxConsecutiveLosses') || 3;

    const enabledChains: string[] = this.config.get('enabledChains') || ['eth'];

    for (const chainIdStr of enabledChains) {
      const chainId = chainIdStr as ChainId;
      const chainConfig = CHAIN_CONFIGS[chainId];
      if (!chainConfig) continue;

      const { privateKey, rpcUrl, maxGasGwei } = this.getChainTradingConfig(chainConfig);
      if (!privateKey || !rpcUrl) {
        this.logger.warn(`[${chainConfig.name}] No private key or RPC — trading disabled`);
        continue;
      }

      try {
        const provider = new ethers.JsonRpcProvider(rpcUrl);
        const wallet = new ethers.Wallet(privateKey, provider);
        const balance = await provider.getBalance(wallet.address);
        const balNative = parseFloat(ethers.formatEther(balance));

        const state: ChainTradingState = {
          chainId,
          chainConfig,
          provider,
          wallet,
          enabled: true,
          activeTrade: null,
          lastKnownBalance: balNative,
          dailyStartBalance: balNative,
          consecutiveLosses: 0,
          pausedUntil: 0,
        };

        this.chainStates.set(chainId, state);
        this.startStopLossMonitor(state);

        this.logger.log(
          `[${chainConfig.name}] Trading enabled | Wallet: ${wallet.address} | Balance: ${balNative.toFixed(4)} ${chainConfig.nativeSymbol}`,
        );
      } catch (err) {
        this.logger.error(`[${chainConfig.name}] Trading init failed: ${err.message}`);
      }
    }

    if (this.chainStates.size === 0) {
      this.logger.warn('No chains configured for trading (notification-only mode).');
    }
  }

  isReady(chainId?: ChainId): boolean {
    if (!chainId) {
      // Any chain ready?
      for (const s of this.chainStates.values()) {
        if (s.enabled && !s.activeTrade && Date.now() >= s.pausedUntil) return true;
      }
      return false;
    }
    const s = this.chainStates.get(chainId);
    if (!s || !s.enabled) return false;
    if (s.activeTrade) return false;
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
  // LAYER 3: Micro buy+sell test
  // ============================================================

  async executeMicroTest(
    opp: Omit<TradeOpportunity, 'buyTax' | 'sellTax'>,
    chainId?: ChainId,
  ): Promise<MicroTestResult> {
    const s = this.getState(chainId);
    if (!s) return { success: false, costETH: 0, reason: 'Trading not enabled' };

    const wrappedNative = s.chainConfig.wrappedNative;
    const microAmount = ethers.parseEther('0.00005');
    const balanceBefore = await s.provider.getBalance(s.wallet.address);

    try {
      const feeData = await s.provider.getFeeData();
      const routerTx = new ethers.Contract(opp.routerAddress, ROUTER_TRADE_ABI, s.wallet);
      const deadline = Math.floor(Date.now() / 1000) + 300;

      // STEP 1: Micro buy
      this.logger.log(`[${s.chainConfig.name}] Micro-test BUY: ${ethers.formatEther(microAmount)} ${s.chainConfig.nativeSymbol}`);
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

      // STEP 3: Micro sell
      this.logger.log(`[${s.chainConfig.name}] Micro-test SELL: ${opp.tokenSymbol}`);
      const sellTx = await routerTx.swapExactTokensForETHSupportingFeeOnTransferTokens(
        tokenBalance, 0, [opp.tokenAddress, wrappedNative], s.wallet.address, deadline,
        { gasLimit: 300000 },
      );
      const sellReceipt = await sellTx.wait(1);
      if (!sellReceipt || sellReceipt.status === 0) {
        return { success: false, costETH: 0.0002, reason: 'Micro sell reverted — HONEYPOT' };
      }

      const balanceAfter = await s.provider.getBalance(s.wallet.address);
      const costETH = parseFloat(ethers.formatEther(balanceBefore - balanceAfter));
      return { success: true, costETH: Math.max(costETH, 0) };
    } catch (err) {
      const balanceAfter = await s.provider.getBalance(s.wallet.address);
      const costETH = parseFloat(ethers.formatEther(balanceBefore - balanceAfter));
      return { success: false, costETH: Math.max(costETH, 0), reason: err.message?.slice(0, 100) };
    }
  }

  // ============================================================
  // Buy execution
  // ============================================================

  async executeBuy(opp: TradeOpportunity, chainId?: ChainId): Promise<boolean> {
    const s = this.getState(chainId);
    if (!s || !this.isReady(chainId)) return false;

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
      const positionETH = Math.min(posFromBalance, posFromPool);
      if (positionETH <= 0) return false;

      this.logger.log(`${tag} BUY signal: ${opp.tokenSymbol} | pos=${positionETH.toFixed(4)} ${s.chainConfig.nativeSymbol} | pool=${opp.poolEthReserve.toFixed(2)} | gas=${gasGwei.toFixed(1)}`);

      const routerRead = new ethers.Contract(opp.routerAddress, ROUTER_TRADE_ABI, s.provider);
      const path = [wrappedNative, opp.tokenAddress];
      const amountIn = ethers.parseEther(positionETH.toFixed(6));
      const amounts = await routerRead.getAmountsOut(amountIn, path);
      const minOut = (amounts[1] * BigInt(100 - this.slippagePct)) / BigInt(100);

      const routerTx = new ethers.Contract(opp.routerAddress, ROUTER_TRADE_ABI, s.wallet);
      const deadline = Math.floor(Date.now() / 1000) + 300;

      const tx = await routerTx.swapExactETHForTokensSupportingFeeOnTransferTokens(
        minOut, path, s.wallet.address, deadline,
        { value: amountIn, gasLimit: 300000, maxFeePerGas: feeData.maxFeePerGas, maxPriorityFeePerGas: feeData.maxPriorityFeePerGas },
      );

      this.logger.log(`${tag} Buy TX sent: ${tx.hash}`);
      const receipt = await tx.wait(1);
      if (!receipt || receipt.status === 0) {
        this.logger.error(`${tag} Buy TX failed`);
        s.pausedUntil = Date.now() + 5 * 60 * 1000;
        return false;
      }

      const tokenContract = new ethers.Contract(opp.tokenAddress, ERC20_TRADE_ABI, s.provider);
      const tokenBalance = await tokenContract.balanceOf(s.wallet.address);
      const entryPrice = this.calculatePrice(positionETH, tokenBalance);

      s.activeTrade = {
        chainId: s.chainId,
        tokenAddress: opp.tokenAddress,
        pairAddress: opp.pairAddress,
        routerAddress: opp.routerAddress,
        tokenSymbol: opp.tokenSymbol,
        entryPriceETH: entryPrice,
        tokensBought: tokenBalance,
        ethSpent: positionETH,
        buyTxHash: tx.hash,
        buyTimestamp: Date.now(),
        sellStarted: false,
        lotsSold: 0,
        totalEthReceived: 0,
      };

      this.logger.log(`${tag} BUY OK: ${opp.tokenSymbol} | ${positionETH.toFixed(4)} ${s.chainConfig.nativeSymbol}`);

      await this.telegram.sendTradeNotification(
        '🟢 BUY EXECUTED', opp.tokenSymbol, opp.tokenAddress, positionETH, tx.hash,
        `Entry: ${positionETH.toFixed(4)} ${s.chainConfig.nativeSymbol}\nGas: ${gasGwei.toFixed(1)} gwei`,
        s.chainConfig,
      );

      this.scheduleSell(s);
      return true;
    } catch (err) {
      this.logger.error(`${tag} Buy failed: ${err.message}`);
      s.pausedUntil = Date.now() + 5 * 60 * 1000;
      return false;
    }
  }

  // ============================================================
  // Lot sell
  // ============================================================

  private scheduleSell(s: ChainTradingState) {
    setTimeout(async () => {
      if (s.activeTrade && !s.activeTrade.sellStarted) {
        await this.executeLotSell(s);
      }
    }, this.holdTimeMs);
  }

  private async executeLotSell(s: ChainTradingState) {
    const trade = s.activeTrade;
    if (!trade) return;

    const tag = `[${s.chainConfig.name}]`;
    const wrappedNative = s.chainConfig.wrappedNative;
    trade.sellStarted = true;
    this.logger.log(`${tag} Starting lot sell for ${trade.tokenSymbol}...`);

    try {
      const tokenContract = new ethers.Contract(trade.tokenAddress, ERC20_TRADE_ABI, s.wallet);
      const currentAllowance = await tokenContract.allowance(s.wallet.address, trade.routerAddress);
      if (currentAllowance < trade.tokensBought) {
        const approveTx = await tokenContract.approve(trade.routerAddress, ethers.MaxUint256, { gasLimit: 100000 });
        await approveTx.wait(1);
        this.logger.log(`${tag} Token approved`);
      }

      const poolInfo = await this.getPoolReserves(trade.pairAddress, s);
      if (!poolInfo) {
        await this.sellTokens(trade, trade.tokensBought, 'emergency-dump', s);
        s.activeTrade = null;
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

        const currentPrice = await this.getCurrentPrice(trade.pairAddress, trade.tokenAddress, s);
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

      await this.telegram.sendTradeNotification(
        isWin ? '✅ TRADE WON' : '❌ TRADE LOST',
        trade.tokenSymbol, trade.tokenAddress, trade.totalEthReceived, trade.buyTxHash,
        `Spent: ${trade.ethSpent.toFixed(4)} ${s.chainConfig.nativeSymbol}\nReceived: ${trade.totalEthReceived.toFixed(4)} ${s.chainConfig.nativeSymbol}\nP&L: ${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)} (${pnlPct.toFixed(1)}%)\nLots: ${trade.lotsSold}`,
        s.chainConfig,
      );

      s.activeTrade = null;
    } catch (err) {
      this.logger.error(`${tag} Lot sell error: ${err.message}`);
      try {
        const remaining = await this.getTokenBalance(trade.tokenAddress, s);
        if (remaining > BigInt(0)) await this.sellTokens(trade, remaining, 'emergency', s);
      } catch (e2) {
        this.logger.error(`${tag} Emergency dump failed: ${e2.message}`);
      }
      s.activeTrade = null;
    }
  }

  private async sellTokens(
    trade: ActiveTrade, tokenAmount: bigint, label: string, s: ChainTradingState,
  ): Promise<number> {
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
    const receipt = await tx.wait(1);
    if (!receipt || receipt.status === 0) return 0;

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
      const trade = s.activeTrade;
      if (!trade || trade.sellStarted) return;
      try {
        const currentPrice = await this.getCurrentPrice(trade.pairAddress, trade.tokenAddress, s);
        if (currentPrice <= 0 || trade.entryPriceETH <= 0) return;
        const changePct = ((currentPrice - trade.entryPriceETH) / trade.entryPriceETH) * 100;
        if (changePct <= -this.stopLossPct) {
          this.logger.warn(`[${s.chainConfig.name}] STOP-LOSS HIT: ${trade.tokenSymbol} at ${changePct.toFixed(1)}%`);
          await this.executeLotSell(s);
        }
      } catch {}
    }, interval);
  }

  // ============================================================
  // Helpers
  // ============================================================

  private getState(chainId?: ChainId): ChainTradingState | null {
    if (chainId) return this.chainStates.get(chainId) || null;
    // Default to first enabled chain
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
    pairAddress: string, tokenAddress: string, s: ChainTradingState,
  ): Promise<number> {
    try {
      const pool = await this.getPoolReserves(pairAddress, s);
      if (!pool || pool.tokenReserve === BigInt(0)) return 0;
      return parseFloat(ethers.formatEther(pool.ethReserve)) / parseFloat(ethers.formatEther(pool.tokenReserve));
    } catch { return 0; }
  }

  private calculatePrice(ethAmount: number, tokenAmount: bigint): number {
    const tokens = parseFloat(ethers.formatEther(tokenAmount));
    return tokens > 0 ? ethAmount / tokens : 0;
  }

  private getMaxGas(chain: ChainConfig): number {
    // Use per-chain default if not overridden globally
    return this.config.get<number>('trading.maxGasGwei') || chain.tradingConfig.maxGasGweiDefault;
  }

  private getChainTradingConfig(chain: ChainConfig): { privateKey: string; rpcUrl: string; maxGasGwei: number } {
    if (chain.id === 'eth') {
      const httpUrl = this.config.get<string>('alchemy.httpUrl');
      const wssUrl = this.config.get<string>('alchemy.wssUrl');
      return {
        privateKey: this.config.get<string>('trading.privateKey') || '',
        rpcUrl: httpUrl || (wssUrl ? wssUrl.replace('wss://', 'https://') : ''),
        maxGasGwei: this.config.get<number>('trading.maxGasGwei') || chain.tradingConfig.maxGasGweiDefault,
      };
    }
    return {
      privateKey: this.config.get<string>(`${chain.id}.privateKey`) || '',
      rpcUrl: this.config.get<string>(`${chain.id}.httpUrl`) || '',
      maxGasGwei: chain.tradingConfig.maxGasGweiDefault,
    };
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
