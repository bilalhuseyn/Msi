import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { ethers } from 'ethers';
import {
  WETH_ADDRESS,
  PAIR_ABI_FRAGMENT,
  DEX_LIST,
} from '../monitor/constants';
import { TelegramService } from '../telegram/telegram.service';

/**
 * Active trade being managed by the bot.
 */
interface ActiveTrade {
  tokenAddress: string;
  pairAddress: string;
  routerAddress: string;
  tokenSymbol: string;
  entryPriceETH: number; // price of token in ETH at buy time
  tokensBought: bigint; // raw token amount held
  ethSpent: number; // ETH we paid
  buyTxHash: string;
  buyTimestamp: number;
  sellStarted: boolean;
  lotsSold: number;
  totalEthReceived: number;
}

/**
 * Params passed from MonitorService when a trade opportunity is found.
 */
export interface TradeOpportunity {
  tokenAddress: string;
  tokenSymbol: string;
  pairAddress: string;
  routerAddress: string;
  poolEthReserve: number;
  buyTax: number;
  sellTax: number;
}

// Uniswap V2 Router ABI for trading
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

  // Providers
  private readProvider: ethers.JsonRpcProvider;
  private wallet: ethers.Wallet;

  // State
  private activeTrade: ActiveTrade | null = null;
  private tradingEnabled = false;
  private dailyStartBalance = 0;
  private dailyLoss = 0;
  private consecutiveLosses = 0;
  private pausedUntil = 0;

  // Config
  private positionPct: number; // % of balance to use per trade
  private maxPoolPct: number; // max % of pool to buy
  private slippagePct: number;
  private maxGasGwei: number;
  private holdTimeMs: number;
  private stopLossPct: number;
  private maxDailyLossPct: number;
  private maxConsecutiveLosses: number;

  constructor(
    private readonly config: ConfigService,
    private readonly telegram: TelegramService,
  ) {}

  async onModuleInit() {
    const privateKey = this.config.get<string>('trading.privateKey');
    if (!privateKey) {
      this.logger.warn('TRADING_PRIVATE_KEY not set. Trading disabled (notification-only mode).');
      return;
    }

    // Config
    this.positionPct = this.config.get<number>('trading.positionPct') || 50;
    this.maxPoolPct = this.config.get<number>('trading.maxPoolPct') || 3;
    this.slippagePct = this.config.get<number>('trading.slippagePct') || 5;
    this.maxGasGwei = this.config.get<number>('trading.maxGasGwei') || 50;
    this.holdTimeMs = this.config.get<number>('trading.holdTimeMs') || 10 * 60 * 1000;
    this.stopLossPct = this.config.get<number>('trading.stopLossPct') || 15;
    this.maxDailyLossPct = this.config.get<number>('trading.maxDailyLossPct') || 30;
    this.maxConsecutiveLosses = this.config.get<number>('trading.maxConsecutiveLosses') || 3;

    // Read provider (Alchemy)
    const httpUrl = this.config.get<string>('alchemy.httpUrl');
    const wssUrl = this.config.get<string>('alchemy.wssUrl');
    const rpcUrl = httpUrl || (wssUrl ? wssUrl.replace('wss://', 'https://') : '');
    this.readProvider = new ethers.JsonRpcProvider(rpcUrl);

    // Use Alchemy RPC for sending transactions
    // Flashbots Protect drops small txs silently; Alchemy is reliable for all sizes
    this.wallet = new ethers.Wallet(privateKey, this.readProvider);

    const balance = await this.readProvider.getBalance(this.wallet.address);
    const balETH = parseFloat(ethers.formatEther(balance));
    this.dailyStartBalance = balETH;

    this.tradingEnabled = true;
    this.logger.log(
      `Trading enabled | Wallet: ${this.wallet.address} | Balance: ${balETH.toFixed(4)} ETH`,
    );

    // Start stop-loss monitor loop
    this.startStopLossMonitor();
  }

  /** Is the bot ready to take a new trade? */
  isReady(): boolean {
    if (!this.tradingEnabled) return false;
    if (this.activeTrade) return false;
    if (Date.now() < this.pausedUntil) return false;
    return true;
  }

  /**
   * Execute a buy when a valid opportunity is detected.
   * Called by MonitorService after all checks pass.
   */
  async executeBuy(opp: TradeOpportunity): Promise<boolean> {
    if (!this.isReady()) {
      this.logger.debug('Trade skipped: bot not ready');
      return false;
    }

    try {
      // 1. Gas check
      const feeData = await this.readProvider.getFeeData();
      const gasGwei = parseFloat(ethers.formatUnits(feeData.gasPrice, 'gwei'));
      if (gasGwei > this.maxGasGwei) {
        this.logger.warn(`Gas too high: ${gasGwei.toFixed(1)} gwei > ${this.maxGasGwei} limit`);
        return false;
      }

      // 2. Check daily loss limit
      const currentBalance = await this.getBalanceETH();
      const dailyLossPct = ((this.dailyStartBalance - currentBalance) / this.dailyStartBalance) * 100;
      if (dailyLossPct >= this.maxDailyLossPct) {
        this.logger.warn(`Daily loss limit hit: ${dailyLossPct.toFixed(1)}% >= ${this.maxDailyLossPct}%`);
        return false;
      }

      // 3. Calculate position size
      const posFromBalance = currentBalance * (this.positionPct / 100);
      const posFromPool = opp.poolEthReserve * (this.maxPoolPct / 100);
      const positionETH = Math.min(posFromBalance, posFromPool);

      if (positionETH <= 0) {
        this.logger.warn(`No balance available for trading`);
        return false;
      }

      this.logger.log(
        `BUY signal: ${opp.tokenSymbol} | pos=${positionETH.toFixed(4)} ETH | pool=${opp.poolEthReserve.toFixed(2)} ETH | gas=${gasGwei.toFixed(1)} gwei`,
      );

      // 4. Calculate amountOutMin with slippage
      const routerRead = new ethers.Contract(opp.routerAddress, ROUTER_TRADE_ABI, this.readProvider);
      const path = [WETH_ADDRESS, opp.tokenAddress];
      const amountIn = ethers.parseEther(positionETH.toFixed(6));

      const amounts = await routerRead.getAmountsOut(amountIn, path);
      const expectedOut = amounts[1];
      const minOut = (expectedOut * BigInt(100 - this.slippagePct)) / BigInt(100);

      // 5. Send buy TX via Flashbots
      const routerTx = new ethers.Contract(opp.routerAddress, ROUTER_TRADE_ABI, this.wallet);
      const deadline = Math.floor(Date.now() / 1000) + 300; // 5 min

      const tx = await routerTx.swapExactETHForTokensSupportingFeeOnTransferTokens(
        minOut,
        path,
        this.wallet.address,
        deadline,
        {
          value: amountIn,
          gasLimit: 300000,
          maxFeePerGas: feeData.maxFeePerGas,
          maxPriorityFeePerGas: feeData.maxPriorityFeePerGas,
        },
      );

      this.logger.log(`Buy TX sent: ${tx.hash}`);

      // Wait for confirmation
      const receipt = await tx.wait(1);
      if (!receipt || receipt.status === 0) {
        this.logger.error(`Buy TX failed: ${tx.hash}`);
        this.pausedUntil = Date.now() + 5 * 60 * 1000; // 5 min cooldown
        return false;
      }

      // 6. Check actual token balance received
      const tokenContract = new ethers.Contract(opp.tokenAddress, ERC20_TRADE_ABI, this.readProvider);
      const tokenBalance = await tokenContract.balanceOf(this.wallet.address);

      // Get entry price
      const entryPrice = this.calculatePrice(positionETH, tokenBalance);

      // 7. Set active trade
      this.activeTrade = {
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

      this.logger.log(
        `BUY OK: ${opp.tokenSymbol} | ${positionETH.toFixed(4)} ETH -> ${ethers.formatUnits(tokenBalance, 18).slice(0, 12)} tokens`,
      );

      // Notify Telegram
      await this.telegram.sendTradeNotification(
        '🟢 BUY EXECUTED',
        opp.tokenSymbol,
        opp.tokenAddress,
        positionETH,
        tx.hash,
        `Entry: ${positionETH.toFixed(4)} ETH\nGas: ${gasGwei.toFixed(1)} gwei\nHolding for ${this.holdTimeMs / 60000} min...`,
      );

      // 8. Schedule sell after hold period
      this.scheduleSell();

      return true;
    } catch (err) {
      this.logger.error(`Buy execution failed: ${err.message}`);
      this.pausedUntil = Date.now() + 5 * 60 * 1000;
      return false;
    }
  }

  /**
   * Schedule the sell after hold period.
   */
  private scheduleSell() {
    setTimeout(async () => {
      if (this.activeTrade && !this.activeTrade.sellStarted) {
        await this.executeLotSell();
      }
    }, this.holdTimeMs);
  }

  /**
   * Hybrid Lot Sell Strategy:
   * - Lot size = max 3% of pool reserves
   * - Lot 1-2: immediate (recover principal)
   * - Lot 3+: 30s apart (let arbers rebalance)
   * - If price drops >15% from entry during sell → dump remaining
   */
  private async executeLotSell() {
    const trade = this.activeTrade;
    if (!trade) return;

    trade.sellStarted = true;
    this.logger.log(`Starting lot sell for ${trade.tokenSymbol}...`);

    try {
      // 1. Approve router to spend our tokens (once, max amount)
      const tokenContract = new ethers.Contract(trade.tokenAddress, ERC20_TRADE_ABI, this.wallet);
      const currentAllowance = await tokenContract.allowance(this.wallet.address, trade.routerAddress);

      if (currentAllowance < trade.tokensBought) {
        const approveTx = await tokenContract.approve(
          trade.routerAddress,
          ethers.MaxUint256,
          { gasLimit: 100000 },
        );
        await approveTx.wait(1);
        this.logger.log('Token approved for router');
      }

      // 2. Calculate lot sizes based on pool
      const poolInfo = await this.getPoolReserves(trade.pairAddress);
      if (!poolInfo) {
        this.logger.error('Cannot read pool — dumping all at once');
        await this.sellTokens(trade, trade.tokensBought, 'emergency-dump');
        return;
      }

      const maxLotTokens = this.calculateMaxLotTokens(
        poolInfo.tokenReserve,
        this.maxPoolPct,
      );

      // Split into lots
      const remainingTokens = await this.getTokenBalance(trade.tokenAddress);
      const totalLots = Math.ceil(
        Number(remainingTokens) / Number(maxLotTokens),
      );
      const lotCount = Math.max(totalLots, 1);

      this.logger.log(
        `Sell plan: ${lotCount} lots | max/lot: ${ethers.formatUnits(maxLotTokens, 18).slice(0, 10)} tokens`,
      );

      // 3. Execute lots
      for (let i = 0; i < lotCount; i++) {
        const tokensLeft = await this.getTokenBalance(trade.tokenAddress);
        if (tokensLeft <= BigInt(0)) break;

        // Last lot = sell everything remaining
        const lotSize = i === lotCount - 1 ? tokensLeft : maxLotTokens < tokensLeft ? maxLotTokens : tokensLeft;

        // Check stop-loss before each lot
        const currentPrice = await this.getCurrentPrice(trade.pairAddress, trade.tokenAddress);
        if (currentPrice > 0) {
          const priceDrop = ((trade.entryPriceETH - currentPrice) / trade.entryPriceETH) * 100;
          if (priceDrop >= this.stopLossPct) {
            this.logger.warn(`STOP-LOSS triggered: -${priceDrop.toFixed(1)}% — dumping remaining`);
            await this.sellTokens(trade, tokensLeft, `stop-loss-lot${i + 1}`);
            break;
          }
        }

        // Execute lot
        const ethReceived = await this.sellTokens(trade, lotSize, `lot-${i + 1}`);
        trade.lotsSold++;
        trade.totalEthReceived += ethReceived;

        this.logger.log(
          `Lot ${i + 1}/${lotCount}: +${ethReceived.toFixed(4)} ETH | Total: ${trade.totalEthReceived.toFixed(4)} ETH`,
        );

        // Lot 1-2: immediate, Lot 3+: wait 30s
        if (i >= 1 && i < lotCount - 1) {
          this.logger.debug(`Waiting 30s before lot ${i + 2}...`);
          await this.sleep(30000);
        }
      }

      // 4. Final P&L
      const pnl = trade.totalEthReceived - trade.ethSpent;
      const pnlPct = (pnl / trade.ethSpent) * 100;
      const isWin = pnl >= 0;

      if (!isWin) {
        this.consecutiveLosses++;
        this.dailyLoss += Math.abs(pnl);
        if (this.consecutiveLosses >= this.maxConsecutiveLosses) {
          this.pausedUntil = Date.now() + 60 * 60 * 1000; // 1 hour pause
          this.logger.warn(`${this.maxConsecutiveLosses} consecutive losses — pausing 1 hour`);
        }
      } else {
        this.consecutiveLosses = 0;
      }

      this.logger.log(
        `TRADE CLOSED: ${trade.tokenSymbol} | ${isWin ? 'WIN' : 'LOSS'} | ${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)} ETH (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%) | ${trade.lotsSold} lots`,
      );

      // Telegram summary
      await this.telegram.sendTradeNotification(
        isWin ? '✅ TRADE WON' : '❌ TRADE LOST',
        trade.tokenSymbol,
        trade.tokenAddress,
        trade.totalEthReceived,
        trade.buyTxHash,
        [
          `Spent: ${trade.ethSpent.toFixed(4)} ETH`,
          `Received: ${trade.totalEthReceived.toFixed(4)} ETH`,
          `P&L: ${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)} ETH (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%)`,
          `Lots: ${trade.lotsSold}`,
          isWin ? '' : `Consecutive losses: ${this.consecutiveLosses}`,
        ].filter(Boolean).join('\n'),
      );

      // Clear active trade
      this.activeTrade = null;
    } catch (err) {
      this.logger.error(`Lot sell error: ${err.message}`);
      // Try emergency dump
      try {
        const remaining = await this.getTokenBalance(trade.tokenAddress);
        if (remaining > BigInt(0)) {
          await this.sellTokens(trade, remaining, 'emergency-dump');
        }
      } catch (e2) {
        this.logger.error(`Emergency dump also failed: ${e2.message}`);
      }
      this.activeTrade = null;
    }
  }

  /**
   * Sell a specific amount of tokens for ETH.
   */
  private async sellTokens(
    trade: ActiveTrade,
    tokenAmount: bigint,
    label: string,
  ): Promise<number> {
    const router = new ethers.Contract(trade.routerAddress, ROUTER_TRADE_ABI, this.wallet);
    const path = [trade.tokenAddress, WETH_ADDRESS];
    const deadline = Math.floor(Date.now() / 1000) + 300;

    // Get expected output
    const routerRead = new ethers.Contract(trade.routerAddress, ROUTER_TRADE_ABI, this.readProvider);
    let minOut = BigInt(0);
    try {
      const amounts = await routerRead.getAmountsOut(tokenAmount, path);
      minOut = (amounts[1] * BigInt(100 - this.slippagePct)) / BigInt(100);
    } catch {
      // If getAmountsOut fails, sell with 0 minOut (emergency)
      this.logger.warn(`getAmountsOut failed for ${label} — selling with 0 minOut`);
    }

    const balanceBefore = await this.readProvider.getBalance(this.wallet.address);

    const tx = await router.swapExactTokensForETHSupportingFeeOnTransferTokens(
      tokenAmount,
      minOut,
      path,
      this.wallet.address,
      deadline,
      { gasLimit: 300000 },
    );

    const receipt = await tx.wait(1);
    if (!receipt || receipt.status === 0) {
      this.logger.error(`Sell TX failed (${label}): ${tx.hash}`);
      return 0;
    }

    const balanceAfter = await this.readProvider.getBalance(this.wallet.address);
    const ethReceived = parseFloat(
      ethers.formatEther(balanceAfter - balanceBefore),
    );

    // Gas cost correction (we paid gas too)
    const gasCost = receipt.gasUsed * receipt.gasPrice;
    const netReceived = ethReceived + parseFloat(ethers.formatEther(gasCost));

    this.logger.log(`Sell ${label}: +${netReceived.toFixed(4)} ETH | TX: ${tx.hash}`);
    return Math.max(netReceived, 0);
  }

  /**
   * Stop-loss monitor — checks price every 15s while holding.
   */
  private startStopLossMonitor() {
    setInterval(async () => {
      const trade = this.activeTrade;
      if (!trade || trade.sellStarted) return;

      try {
        const currentPrice = await this.getCurrentPrice(trade.pairAddress, trade.tokenAddress);
        if (currentPrice <= 0 || trade.entryPriceETH <= 0) return;

        const changePct = ((currentPrice - trade.entryPriceETH) / trade.entryPriceETH) * 100;

        // Stop-loss: dump everything immediately
        if (changePct <= -this.stopLossPct) {
          this.logger.warn(
            `STOP-LOSS HIT: ${trade.tokenSymbol} at ${changePct.toFixed(1)}% → emergency sell`,
          );
          await this.executeLotSell();
        }
      } catch (err) {
        this.logger.debug(`SL monitor error: ${err.message}`);
      }
    }, 15000); // Check every 15s
  }

  // ================================================================
  // Helper methods
  // ================================================================

  private async getBalanceETH(): Promise<number> {
    const bal = await this.readProvider.getBalance(this.wallet.address);
    return parseFloat(ethers.formatEther(bal));
  }

  private async getTokenBalance(tokenAddress: string): Promise<bigint> {
    const token = new ethers.Contract(tokenAddress, ERC20_TRADE_ABI, this.readProvider);
    return token.balanceOf(this.wallet.address);
  }

  private async getPoolReserves(
    pairAddress: string,
  ): Promise<{ ethReserve: bigint; tokenReserve: bigint } | null> {
    try {
      const pair = new ethers.Contract(pairAddress, PAIR_ABI_FRAGMENT, this.readProvider);
      const [reserves, token0] = await Promise.all([
        pair.getReserves(),
        pair.token0(),
      ]);
      const isWeth0 = token0.toLowerCase() === WETH_ADDRESS.toLowerCase();
      return {
        ethReserve: isWeth0 ? reserves[0] : reserves[1],
        tokenReserve: isWeth0 ? reserves[1] : reserves[0],
      };
    } catch {
      return null;
    }
  }

  private calculateMaxLotTokens(tokenReserve: bigint, maxPct: number): bigint {
    return (tokenReserve * BigInt(Math.floor(maxPct * 100))) / BigInt(10000);
  }

  private async getCurrentPrice(
    pairAddress: string,
    tokenAddress: string,
  ): Promise<number> {
    try {
      const pool = await this.getPoolReserves(pairAddress);
      if (!pool || pool.tokenReserve === BigInt(0)) return 0;
      return (
        parseFloat(ethers.formatEther(pool.ethReserve)) /
        parseFloat(ethers.formatEther(pool.tokenReserve))
      );
    } catch {
      return 0;
    }
  }

  private calculatePrice(ethAmount: number, tokenAmount: bigint): number {
    const tokens = parseFloat(ethers.formatEther(tokenAmount));
    return tokens > 0 ? ethAmount / tokens : 0;
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
