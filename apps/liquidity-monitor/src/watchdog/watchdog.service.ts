import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { TelegramService } from '../telegram/telegram.service';
import { SecurityService } from '../security/security.service';

// ============================================================
// WATCHDOG SERVICE — Self-monitoring guardian layer
//
// 7 independent monitors, all in-memory, near-zero overhead:
//   1. Trade Lifecycle   — every trade MUST close within deadline
//   2. Connection Health  — block listener + mempool guard alive?
//   3. Wallet & Balance   — unexpected balance drops, low balance
//   4. Security Gate Stats — honeypot rate, filter effectiveness
//   5. Sell Queue Health   — queue stuck, backlog growing
//   6. Performance Dashboard — periodic P&L summary
//   7. Memory & Process    — cooldown map leak, disk errors
// ============================================================

interface TradeSnapshot {
  tokenSymbol: string;
  tokenAddress: string;
  chainId: string;
  buyTimestamp: number;
  ethSpent: number;
  sellStarted: boolean;
  sellStartedAt: number | null;
  lastAlertedAt: number; // prevent alert spam
}

interface ChainHealth {
  lastBlockTime: number;       // timestamp of last processed block
  lastBlockNumber: number;
  wsDisconnects: number;
  mempoolGuardsActive: number;
  alertedDisconnect: boolean;
}

interface WalletSnapshot {
  chainId: string;
  chainName: string;
  nativeSymbol: string;
  balance: number;
  lastChecked: number;
  previousBalance: number;
  lowBalanceAlerted: boolean;
}

interface SecurityStats {
  totalChecked: number;
  rejected: number;
  needsMicroTest: number;
  approved: number;
  microTestPassed: number;
  microTestFailed: number;
  postBuyHoneypots: number;    // passed micro but can't sell after buy
  sellFailures: number;
  lastResetTime: number;
}

interface SellQueueStats {
  chainId: string;
  queueLength: number;
  queueRunning: boolean;
  queueStartedAt: number | null;
  lastAlerted: number;
}

interface PerformanceWindow {
  trades: number;
  wins: number;
  losses: number;
  totalPnlNative: number;
  totalPnlUSD: number;
  honeypotLosses: number;      // trades with -100% loss
  avgHoldTimeMs: number;
  startTime: number;
}

@Injectable()
export class WatchdogService implements OnModuleInit {
  private readonly logger = new Logger('Watchdog');

  // ① Trade Lifecycle tracking
  private readonly tradeSnapshots = new Map<string, TradeSnapshot>();

  // ② Connection Health per chain
  private readonly chainHealth = new Map<string, ChainHealth>();

  // ③ Wallet & Balance
  private readonly walletSnapshots = new Map<string, WalletSnapshot>();

  // ④ Security Gate Stats
  private readonly securityStats: SecurityStats = {
    totalChecked: 0, rejected: 0, needsMicroTest: 0, approved: 0,
    microTestPassed: 0, microTestFailed: 0, postBuyHoneypots: 0,
    sellFailures: 0, lastResetTime: Date.now(),
  };

  // ⑤ Sell Queue Stats per chain
  private readonly sellQueueStats = new Map<string, SellQueueStats>();

  // ⑥ Performance Window (rolling)
  private readonly perfWindow: PerformanceWindow = {
    trades: 0, wins: 0, losses: 0, totalPnlNative: 0, totalPnlUSD: 0,
    honeypotLosses: 0, avgHoldTimeMs: 0, startTime: Date.now(),
  };

  // ⑦ Memory tracking
  private cooldownMapSizes = new Map<string, number>();

  // Config
  private readonly TRADE_DEADLINE_ALERT_MS = 5 * 60 * 1000;  // Alert at 5 min (before 7min hold-time sell)
  private readonly SELL_STUCK_ALERT_MS = 2 * 60 * 1000;       // Alert if sell stuck 2 min
  private readonly BLOCK_GAP_ALERT_MS = 60 * 1000;            // Alert if no block for 60s
  private readonly LOW_BALANCE_ETH = 0.001;                    // Alert threshold
  private readonly LOW_BALANCE_BNB = 0.003;
  private readonly QUEUE_BACKLOG_ALERT = 3;                    // Alert if queue > 3
  private readonly PERFORMANCE_INTERVAL_MS = 2 * 60 * 60 * 1000; // Dashboard every 2h
  private readonly CHECK_INTERVAL_MS = 15 * 1000;             // Main loop every 15s
  private readonly HONEYPOT_RATE_ALERT = 30;                   // Alert if >30% honeypots

  constructor(
    private readonly telegram: TelegramService,
    private readonly security: SecurityService,
  ) {}

  onModuleInit() {
    // Main watchdog loop
    setInterval(() => this.runChecks(), this.CHECK_INTERVAL_MS);

    // Performance dashboard
    setInterval(() => this.sendPerformanceDashboard(), this.PERFORMANCE_INTERVAL_MS);

    this.logger.log('Watchdog active — monitoring all subsystems');
  }

  // ============================================================
  // PUBLIC API — Called by other services to feed data
  // ============================================================

  /** Called by TradingService when a trade is opened */
  onTradeOpened(chainId: string, tokenAddress: string, tokenSymbol: string, ethSpent: number) {
    const key = `${chainId}:${tokenAddress.toLowerCase()}`;
    this.tradeSnapshots.set(key, {
      tokenSymbol, tokenAddress, chainId,
      buyTimestamp: Date.now(), ethSpent,
      sellStarted: false, sellStartedAt: null,
      lastAlertedAt: 0,
    });
  }

  /** Called by TradingService when sell starts */
  onSellStarted(chainId: string, tokenAddress: string) {
    const key = `${chainId}:${tokenAddress.toLowerCase()}`;
    const snap = this.tradeSnapshots.get(key);
    if (snap) {
      snap.sellStarted = true;
      snap.sellStartedAt = Date.now();
    }
  }

  /** Called by TradingService when trade closes (win or loss) */
  onTradeClosed(chainId: string, tokenAddress: string, pnlNative: number, pnlUSD: number, holdTimeMs: number) {
    const key = `${chainId}:${tokenAddress.toLowerCase()}`;
    this.tradeSnapshots.delete(key);

    // Update performance
    this.perfWindow.trades++;
    this.perfWindow.totalPnlNative += pnlNative;
    this.perfWindow.totalPnlUSD += pnlUSD;
    if (pnlNative >= 0) {
      this.perfWindow.wins++;
    } else {
      this.perfWindow.losses++;
      if (pnlNative <= -0.99 * Math.abs(pnlNative + Math.abs(pnlNative))) {
        // ~100% loss = honeypot
      }
    }

    // Rolling average hold time
    const n = this.perfWindow.trades;
    this.perfWindow.avgHoldTimeMs = ((this.perfWindow.avgHoldTimeMs * (n - 1)) + holdTimeMs) / n;
  }

  /** Called by TradingService on honeypot (-100% loss) */
  onHoneypotLoss() {
    this.perfWindow.honeypotLosses++;
  }

  /** Called by MonitorService when a new block is processed */
  onBlockProcessed(chainId: string, blockNumber: number) {
    let h = this.chainHealth.get(chainId);
    if (!h) {
      h = { lastBlockTime: 0, lastBlockNumber: 0, wsDisconnects: 0, mempoolGuardsActive: 0, alertedDisconnect: false };
      this.chainHealth.set(chainId, h);
    }
    h.lastBlockTime = Date.now();
    h.lastBlockNumber = blockNumber;
    h.alertedDisconnect = false; // reset alert flag on new block
  }

  /** Called by MonitorService when WS disconnects */
  onWsDisconnect(chainId: string) {
    const h = this.chainHealth.get(chainId);
    if (h) h.wsDisconnects++;
  }

  /** Called by MonitorService when WS reconnects */
  onWsReconnect(chainId: string) {
    // no-op for now, disconnect counter stays for dashboard
  }

  /** Called by TradingService mempool guard connect/disconnect */
  onMempoolGuardChange(chainId: string, delta: number) {
    const h = this.chainHealth.get(chainId);
    if (h) h.mempoolGuardsActive += delta;
  }

  /** Called by SecurityService after each security check */
  onSecurityCheck(gate: 'approved' | 'needs-micro-test' | 'rejected') {
    this.securityStats.totalChecked++;
    if (gate === 'rejected') this.securityStats.rejected++;
    else if (gate === 'needs-micro-test') this.securityStats.needsMicroTest++;
    else this.securityStats.approved++;
  }

  /** Called by TradingService after micro-test */
  onMicroTestResult(passed: boolean) {
    if (passed) this.securityStats.microTestPassed++;
    else this.securityStats.microTestFailed++;
  }

  /** Called by TradingService when post-buy verification fails */
  onPostBuyHoneypot() {
    this.securityStats.postBuyHoneypots++;
  }

  /** Called by TradingService when sell TX fails */
  onSellFailure() {
    this.securityStats.sellFailures++;
  }

  /** Called by TradingService to report sell queue state */
  updateSellQueue(chainId: string, length: number, running: boolean) {
    let sq = this.sellQueueStats.get(chainId);
    if (!sq) {
      sq = { chainId, queueLength: 0, queueRunning: false, queueStartedAt: null, lastAlerted: 0 };
      this.sellQueueStats.set(chainId, sq);
    }
    sq.queueLength = length;
    sq.queueRunning = running;
    if (running && !sq.queueStartedAt) sq.queueStartedAt = Date.now();
    if (!running) sq.queueStartedAt = null;
  }

  /** Called by TradingService with current wallet balance */
  updateWalletBalance(chainId: string, chainName: string, nativeSymbol: string, balance: number) {
    let w = this.walletSnapshots.get(chainId);
    if (!w) {
      w = { chainId, chainName, nativeSymbol, balance, lastChecked: Date.now(), previousBalance: balance, lowBalanceAlerted: false };
      this.walletSnapshots.set(chainId, w);
    } else {
      w.previousBalance = w.balance;
      w.balance = balance;
      w.lastChecked = Date.now();
    }
  }

  /** Called by MonitorService to report cooldown map size */
  updateCooldownMapSize(chainId: string, size: number) {
    this.cooldownMapSizes.set(chainId, size);
  }

  // ============================================================
  // MAIN CHECK LOOP — runs every 15 seconds
  // ============================================================

  private async runChecks() {
    try {
      this.checkTradeLifecycle();
      this.checkConnectionHealth();
      this.checkWalletBalance();
      this.checkSecurityGates();
      this.checkSellQueue();
      this.checkMemory();
    } catch (err) {
      this.logger.error(`Watchdog check error: ${err.message}`);
    }
  }

  // ============================================================
  // ① TRADE LIFECYCLE — No trade can stay open forever
  // ============================================================

  private checkTradeLifecycle() {
    const now = Date.now();

    for (const [key, snap] of this.tradeSnapshots) {
      const elapsed = now - snap.buyTimestamp;
      const sinceLastAlert = now - snap.lastAlertedAt;

      // Safety: auto-clean stale snapshots (> 1 hour old = orphaned)
      if (elapsed > 60 * 60 * 1000) {
        this.logger.warn(`Cleaning orphaned trade snapshot: ${snap.tokenSymbol} (${Math.round(elapsed / 60000)}min old)`);
        this.tradeSnapshots.delete(key);
        continue;
      }

      // Alert at 8 min if trade still open (before 10min force-close)
      if (!snap.sellStarted && elapsed >= this.TRADE_DEADLINE_ALERT_MS && sinceLastAlert > 60000) {
        snap.lastAlertedAt = now;
        const minLeft = Math.round((7 * 60 * 1000 - elapsed) / 60000);
        this.alert(
          `⏰ TRADE DEADLINE`,
          `${snap.tokenSymbol} [${snap.chainId.toUpperCase()}] open for ${Math.round(elapsed / 60000)}min\n` +
          `Spent: ${snap.ethSpent.toFixed(4)}\n` +
          `Force-close in ~${Math.max(minLeft, 0)}min if no result`,
        );
      }

      // Alert if sell started but stuck for > 2 min (max once per 30 min)
      if (snap.sellStarted && snap.sellStartedAt) {
        const sellElapsed = now - snap.sellStartedAt;
        if (sellElapsed > this.SELL_STUCK_ALERT_MS && sinceLastAlert > 30 * 60 * 1000) {
          snap.lastAlertedAt = now;
          this.alert(
            `🔴 SELL STUCK`,
            `${snap.tokenSymbol} [${snap.chainId.toUpperCase()}] sell running for ${Math.round(sellElapsed / 60000)}min\n` +
            `May need intervention`,
          );
        }
      }
    }
  }

  // ============================================================
  // ② CONNECTION HEALTH — Block listener + WS alive?
  // ============================================================

  private checkConnectionHealth() {
    const now = Date.now();

    for (const [chainId, h] of this.chainHealth) {
      if (h.lastBlockTime === 0) continue; // not started yet

      const gap = now - h.lastBlockTime;
      if (gap > this.BLOCK_GAP_ALERT_MS && !h.alertedDisconnect) {
        h.alertedDisconnect = true;
        this.alert(
          `📡 BLOCK LISTENER GAP`,
          `${chainId.toUpperCase()} — No block for ${Math.round(gap / 1000)}s\n` +
          `Last block: #${h.lastBlockNumber}\n` +
          `WS disconnects: ${h.wsDisconnects}\n` +
          `Bot may be MISSING new tokens!`,
        );
      }
    }
  }

  // ============================================================
  // ③ WALLET & BALANCE — Low balance, unexpected drops
  // ============================================================

  private checkWalletBalance() {
    for (const [, w] of this.walletSnapshots) {
      const threshold = w.nativeSymbol === 'BNB' ? this.LOW_BALANCE_BNB : this.LOW_BALANCE_ETH;

      // Low balance alert (once per session)
      if (w.balance < threshold && !w.lowBalanceAlerted) {
        w.lowBalanceAlerted = true;
        this.alert(
          `💰 LOW BALANCE`,
          `${w.chainName} wallet: ${w.balance.toFixed(6)} ${w.nativeSymbol}\n` +
          `Threshold: ${threshold} ${w.nativeSymbol}\n` +
          `Bot cannot trade until funded!`,
        );
      }
      // Reset alert if balance recovers
      if (w.balance >= threshold * 2) {
        w.lowBalanceAlerted = false;
      }

      // Unexpected large drop (>50% of previous balance in a single check)
      if (w.previousBalance > 0 && w.balance < w.previousBalance * 0.5) {
        this.alert(
          `⚠️ BALANCE DROP`,
          `${w.chainName}: ${w.previousBalance.toFixed(6)} → ${w.balance.toFixed(6)} ${w.nativeSymbol}\n` +
          `Drop: ${((1 - w.balance / w.previousBalance) * 100).toFixed(1)}%\n` +
          `Check for unexpected transactions!`,
        );
      }
    }
  }

  // ============================================================
  // ④ SECURITY GATE EFFECTIVENESS — Are filters working?
  // ============================================================

  private checkSecurityGates() {
    const s = this.securityStats;
    if (s.totalChecked < 10) return; // need enough data

    // Honeypot rate: (micro-test fails + post-buy honeypots + sell failures) / total
    const honeypotCount = s.microTestFailed + s.postBuyHoneypots + s.sellFailures;
    const honeypotRate = (honeypotCount / s.totalChecked) * 100;

    // Check every 50 tokens
    if (s.totalChecked % 50 === 0) {
      if (honeypotRate > this.HONEYPOT_RATE_ALERT) {
        this.alert(
          `🛡️ HIGH HONEYPOT RATE`,
          `${honeypotRate.toFixed(1)}% of checked tokens are honeypots\n` +
          `Total: ${s.totalChecked} | Rejected: ${s.rejected} | Micro-fail: ${s.microTestFailed}\n` +
          `Post-buy HP: ${s.postBuyHoneypots} | Sell fail: ${s.sellFailures}\n` +
          `Security filters may need tightening`,
        );
      }

      // If approval rate is too high (>90%) — gates not filtering enough
      const approvalRate = ((s.approved + s.needsMicroTest) / s.totalChecked) * 100;
      if (approvalRate > 90) {
        this.alert(
          `🛡️ FILTER TOO PERMISSIVE`,
          `${approvalRate.toFixed(0)}% of tokens pass initial gate\n` +
          `Rejected only ${s.rejected} out of ${s.totalChecked}\n` +
          `Consider tightening bytecode thresholds`,
        );
      }
    }
  }

  // ============================================================
  // ⑤ SELL QUEUE HEALTH — Queue stuck or backlog?
  // ============================================================

  private checkSellQueue() {
    const now = Date.now();

    for (const [chainId, sq] of this.sellQueueStats) {
      // Backlog alert
      if (sq.queueLength >= this.QUEUE_BACKLOG_ALERT && now - sq.lastAlerted > 60000) {
        sq.lastAlerted = now;
        this.alert(
          `📦 SELL QUEUE BACKLOG`,
          `${chainId.toUpperCase()} — ${sq.queueLength} sells queued\n` +
          `Queue running: ${sq.queueRunning ? 'YES' : 'NO'}\n` +
          `Trades may be delayed!`,
        );
      }

      // Queue running too long (>3 min = stuck)
      if (sq.queueRunning && sq.queueStartedAt) {
        const runningMs = now - sq.queueStartedAt;
        if (runningMs > 3 * 60 * 1000 && now - sq.lastAlerted > 60000) {
          sq.lastAlerted = now;
          this.alert(
            `🔴 SELL QUEUE STUCK`,
            `${chainId.toUpperCase()} — Queue running for ${Math.round(runningMs / 1000)}s\n` +
            `${sq.queueLength} items pending\n` +
            `Possible TX confirmation hang`,
          );
        }
      }
    }
  }

  // ============================================================
  // ⑥ PERFORMANCE DASHBOARD — Periodic P&L summary
  // ============================================================

  private sendPerformanceDashboard() {
    const p = this.perfWindow;
    if (p.trades === 0) return; // nothing to report

    const winRate = p.trades > 0 ? (p.wins / p.trades * 100) : 0;
    const honeypotRate = p.trades > 0 ? (p.honeypotLosses / p.trades * 100) : 0;
    const elapsed = Date.now() - p.startTime;
    const hours = Math.round(elapsed / (60 * 60 * 1000));

    const lines = [
      `📊 <b>PERFORMANCE REPORT</b> (${hours}h window)`,
      ``,
      `Trades: ${p.trades} | Win: ${p.wins} | Loss: ${p.losses}`,
      `Win Rate: <b>${winRate.toFixed(1)}%</b>`,
      `Net P&L: ${p.totalPnlNative >= 0 ? '+' : ''}${p.totalPnlNative.toFixed(4)} ($${p.totalPnlUSD.toFixed(2)})`,
      `Honeypot losses: ${p.honeypotLosses} (${honeypotRate.toFixed(0)}%)`,
      `Avg hold: ${Math.round(p.avgHoldTimeMs / 1000)}s`,
      ``,
      `Active trades: ${this.tradeSnapshots.size}`,
    ];

    // Add per-chain health
    for (const [chainId, h] of this.chainHealth) {
      const gap = Date.now() - h.lastBlockTime;
      const status = gap < 30000 ? '🟢' : gap < 60000 ? '🟡' : '🔴';
      lines.push(`${status} ${chainId.toUpperCase()}: block #${h.lastBlockNumber} (${Math.round(gap / 1000)}s ago) | WS drops: ${h.wsDisconnects}`);
    }

    // Add wallet balances
    for (const [, w] of this.walletSnapshots) {
      lines.push(`💰 ${w.chainName}: ${w.balance.toFixed(4)} ${w.nativeSymbol}`);
    }

    // Security summary
    const s = this.securityStats;
    if (s.totalChecked > 0) {
      lines.push(``);
      lines.push(`🛡️ Security: ${s.totalChecked} checked | ${s.rejected} rejected | ${s.microTestFailed} micro-fail | ${s.postBuyHoneypots} post-buy HP`);
    }

    this.telegram.sendRawMessage(lines.join('\n')).catch(() => {});
    this.logger.log(`Performance dashboard sent: ${p.trades} trades, ${winRate.toFixed(1)}% win rate`);
  }

  // ============================================================
  // ⑦ MEMORY & PROCESS HEALTH
  // ============================================================

  private checkMemory() {
    for (const [chainId, size] of this.cooldownMapSizes) {
      if (size > 5000) {
        this.alert(
          `🧠 MEMORY WARNING`,
          `${chainId.toUpperCase()} cooldown map: ${size} entries\n` +
          `Possible memory leak — consider cleanup`,
        );
      }
    }
  }

  // ============================================================
  // ALERT — Send Telegram notification
  // ============================================================

  private alert(title: string, body: string) {
    const message = `🐕 <b>${title}</b>\n\n${body}`;
    this.telegram.sendRawMessage(message).catch((err) => {
      this.logger.error(`Watchdog alert failed: ${err.message}`);
    });
    this.logger.warn(`WATCHDOG: ${title} — ${body.replace(/\n/g, ' | ')}`);
  }
}
