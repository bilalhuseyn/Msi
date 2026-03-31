import { Injectable, Logger } from '@nestjs/common';
import { TradeLoggerService, TradeRecord } from './trade-logger.service';
import { TelegramService } from '../telegram/telegram.service';
import { ChainId } from '../config/chains';

export interface Recommendation {
  id: string;
  type: 'min-liquidity' | 'deployer-blacklist' | 'bytecode-threshold' | 'chain-disable' | 'hold-time' | 'general';
  chain?: ChainId;
  severity: 'info' | 'warning' | 'critical';
  title: string;
  detail: string;
  dataPoints: number;      // how many trades support this recommendation
  confidence: number;       // 0-100%
  suggestedAction: string;
  timestamp: number;
}

@Injectable()
export class PatternAnalyzerService {
  private readonly logger = new Logger('PatternAnalyzer');
  private lastAnalysisAt = 0;
  private readonly MIN_TRADES_FOR_ANALYSIS = 10;
  private analysisInterval: NodeJS.Timeout;

  constructor(
    private readonly tradeLogger: TradeLoggerService,
    private readonly telegram: TelegramService,
  ) {}

  start(): void {
    // Run analysis every 2 hours
    this.analysisInterval = setInterval(() => this.runAnalysis(), 2 * 60 * 60 * 1000);
    // Also run after 30 min on startup to catch early patterns
    setTimeout(() => this.runAnalysis(), 30 * 60 * 1000);
    this.logger.log('Pattern analyzer started (interval: 2h)');
  }

  async runAnalysis(): Promise<void> {
    const history = this.tradeLogger.getHistory();
    if (history.length < this.MIN_TRADES_FOR_ANALYSIS) {
      this.logger.debug(`Not enough trades for analysis (${history.length}/${this.MIN_TRADES_FOR_ANALYSIS})`);
      return;
    }

    // Only analyze trades since last analysis (or all if first time)
    const trades = this.lastAnalysisAt > 0
      ? history.filter((t) => t.timestamp > this.lastAnalysisAt)
      : history;

    if (trades.length < 5) return;

    this.logger.log(`Running pattern analysis on ${trades.length} trades (${history.length} total)...`);

    const recommendations: Recommendation[] = [];

    // Run all pattern detectors
    recommendations.push(...this.analyzeLiquidityPatterns(history));
    recommendations.push(...this.analyzeDeployerPatterns(history));
    recommendations.push(...this.analyzeBytecodePatterns(history));
    recommendations.push(...this.analyzeChainPerformance(history));
    recommendations.push(...this.analyzeHoldTimePatterns(history));
    recommendations.push(...this.analyzeHoneypotRate(history));

    this.lastAnalysisAt = Date.now();

    // Filter: only send recommendations with confidence > 60%
    const actionable = recommendations.filter((r) => r.confidence >= 60);

    if (actionable.length > 0) {
      await this.sendRecommendations(actionable, history.length);
    } else {
      this.logger.log('No actionable recommendations found');
    }
  }

  // ============================================================
  // Pattern Detectors
  // ============================================================

  private analyzeLiquidityPatterns(trades: TradeRecord[]): Recommendation[] {
    const recs: Recommendation[] = [];
    const chains: ChainId[] = ['eth', 'bsc', 'base'];

    for (const chain of chains) {
      const chainTrades = trades.filter((t) => t.chain === chain);
      if (chainTrades.length < 5) continue;

      // Group by liquidity ranges
      const ranges = [
        { label: '0-10', min: 0, max: 10 },
        { label: '10-20', min: 10, max: 20 },
        { label: '20-30', min: 20, max: 30 },
        { label: '30-50', min: 30, max: 50 },
        { label: '50+', min: 50, max: Infinity },
      ];

      for (const range of ranges) {
        const inRange = chainTrades.filter(
          (t) => t.liquidityNative >= range.min && t.liquidityNative < range.max,
        );
        if (inRange.length < 3) continue;

        const losses = inRange.filter((t) => t.result === 'loss');
        const lossRate = (losses.length / inRange.length) * 100;
        const totalLossUSD = losses.reduce((sum, t) => sum + Math.abs(t.pnlUSD), 0);

        if (lossRate >= 70) {
          recs.push({
            id: `liq-${chain}-${range.label}`,
            type: 'min-liquidity',
            chain,
            severity: lossRate >= 90 ? 'critical' : 'warning',
            title: `${chain.toUpperCase()}: ${range.label} native liq = ${lossRate.toFixed(0)}% loss rate`,
            detail: `${inRange.length} trades in ${range.label} range: ${losses.length} losses, ${inRange.length - losses.length} wins. Total loss: $${totalLossUSD.toFixed(2)}`,
            dataPoints: inRange.length,
            confidence: Math.min(lossRate, 95),
            suggestedAction: `Increase ${chain.toUpperCase()} minimum liquidity above ${range.max}`,
            timestamp: Date.now(),
          });
        }
      }
    }

    return recs;
  }

  private analyzeDeployerPatterns(trades: TradeRecord[]): Recommendation[] {
    const recs: Recommendation[] = [];

    // Group by deployer
    const deployerMap = new Map<string, TradeRecord[]>();
    for (const t of trades) {
      if (!t.deployer) continue;
      const key = t.deployer.toLowerCase();
      if (!deployerMap.has(key)) deployerMap.set(key, []);
      deployerMap.get(key)!.push(t);
    }

    for (const [deployer, deployerTrades] of deployerMap) {
      if (deployerTrades.length < 2) continue;
      const losses = deployerTrades.filter((t) => t.result === 'loss');
      const lossRate = (losses.length / deployerTrades.length) * 100;

      if (lossRate >= 80 && losses.length >= 2) {
        recs.push({
          id: `deployer-${deployer.slice(0, 10)}`,
          type: 'deployer-blacklist',
          severity: 'critical',
          title: `Repeat scam deployer: ${deployer.slice(0, 10)}...`,
          detail: `${deployerTrades.length} tokens deployed, ${losses.length} were honeypots (${lossRate.toFixed(0)}% loss rate). Tokens: ${deployerTrades.map((t) => t.tokenSymbol).join(', ')}`,
          dataPoints: deployerTrades.length,
          confidence: Math.min(80 + losses.length * 5, 98),
          suggestedAction: `Blacklist deployer ${deployer}`,
          timestamp: Date.now(),
        });
      }
    }

    return recs;
  }

  private analyzeBytecodePatterns(trades: TradeRecord[]): Recommendation[] {
    const recs: Recommendation[] = [];

    const highScore = trades.filter((t) => t.bytecodeScore >= 15);
    const lowScore = trades.filter((t) => t.bytecodeScore < 15);

    if (highScore.length >= 3) {
      const highLossRate = (highScore.filter((t) => t.result === 'loss').length / highScore.length) * 100;
      const lowLossRate = lowScore.length > 0
        ? (lowScore.filter((t) => t.result === 'loss').length / lowScore.length) * 100
        : 0;

      if (highLossRate > lowLossRate + 20) {
        recs.push({
          id: 'bytecode-threshold',
          type: 'bytecode-threshold',
          severity: 'warning',
          title: `High bytecode score (≥15) = ${highLossRate.toFixed(0)}% loss rate`,
          detail: `Score ≥15: ${highScore.length} trades, ${highLossRate.toFixed(0)}% loss. Score <15: ${lowScore.length} trades, ${lowLossRate.toFixed(0)}% loss. Difference: ${(highLossRate - lowLossRate).toFixed(0)}%`,
          dataPoints: highScore.length,
          confidence: Math.min(60 + highScore.length * 3, 90),
          suggestedAction: 'Lower bytecode rejection threshold from 50 to 30',
          timestamp: Date.now(),
        });
      }
    }

    return recs;
  }

  private analyzeChainPerformance(trades: TradeRecord[]): Recommendation[] {
    const recs: Recommendation[] = [];
    const chains: ChainId[] = ['eth', 'bsc', 'base'];

    for (const chain of chains) {
      const chainTrades = trades.filter((t) => t.chain === chain);
      if (chainTrades.length < 5) continue;

      const wins = chainTrades.filter((t) => t.result === 'win');
      const losses = chainTrades.filter((t) => t.result === 'loss');
      const winRate = (wins.length / chainTrades.length) * 100;
      const netPnlUSD = chainTrades.reduce((sum, t) => sum + t.pnlUSD, 0);
      const avgWinPct = wins.length > 0 ? wins.reduce((s, t) => s + t.pnlPct, 0) / wins.length : 0;
      const avgLossPct = losses.length > 0 ? losses.reduce((s, t) => s + t.pnlPct, 0) / losses.length : 0;
      const honeypots = losses.filter((t) => t.pnlPct <= -99 && t.lotsSold === 0);

      // Report chain summary
      recs.push({
        id: `chain-perf-${chain}`,
        type: 'general',
        chain,
        severity: netPnlUSD < -1 ? 'warning' : 'info',
        title: `${chain.toUpperCase()}: ${winRate.toFixed(0)}% win rate | Net: $${netPnlUSD.toFixed(2)}`,
        detail: `${chainTrades.length} trades: ${wins.length}W/${losses.length}L | Avg win: +${avgWinPct.toFixed(1)}% | Avg loss: ${avgLossPct.toFixed(1)}% | Honeypots: ${honeypots.length}`,
        dataPoints: chainTrades.length,
        confidence: 100,
        suggestedAction: netPnlUSD < -2 ? `Review ${chain.toUpperCase()} filters — consistently losing money` : 'No action needed',
        timestamp: Date.now(),
      });

      // If honeypot rate is very high
      if (honeypots.length >= 3 && (honeypots.length / chainTrades.length) * 100 >= 40) {
        recs.push({
          id: `honeypot-rate-${chain}`,
          type: 'general',
          chain,
          severity: 'critical',
          title: `${chain.toUpperCase()}: ${((honeypots.length / chainTrades.length) * 100).toFixed(0)}% honeypot rate`,
          detail: `${honeypots.length} out of ${chainTrades.length} trades were unsellable honeypots (-100%, 0 lots)`,
          dataPoints: honeypots.length,
          confidence: 90,
          suggestedAction: `Tighten ${chain.toUpperCase()} filters or increase liquidity minimum`,
          timestamp: Date.now(),
        });
      }
    }

    return recs;
  }

  private analyzeHoldTimePatterns(trades: TradeRecord[]): Recommendation[] {
    const recs: Recommendation[] = [];
    const wins = trades.filter((t) => t.result === 'win');

    if (wins.length < 5) return recs;

    // Check if most wins peak early
    const earlyWins = wins.filter((t) => t.holdTimeMs < 3 * 60 * 1000); // < 3min
    const lateWins = wins.filter((t) => t.holdTimeMs >= 3 * 60 * 1000);

    if (earlyWins.length > 0 && lateWins.length > 0) {
      const earlyAvgPnl = earlyWins.reduce((s, t) => s + t.pnlPct, 0) / earlyWins.length;
      const lateAvgPnl = lateWins.reduce((s, t) => s + t.pnlPct, 0) / lateWins.length;

      if (earlyAvgPnl > lateAvgPnl * 1.5 && earlyWins.length >= 3) {
        recs.push({
          id: 'hold-time-early',
          type: 'hold-time',
          severity: 'info',
          title: `Early sells more profitable: +${earlyAvgPnl.toFixed(1)}% vs +${lateAvgPnl.toFixed(1)}%`,
          detail: `Wins <3min avg: +${earlyAvgPnl.toFixed(1)}% (${earlyWins.length} trades). Wins ≥3min avg: +${lateAvgPnl.toFixed(1)}% (${lateWins.length} trades)`,
          dataPoints: wins.length,
          confidence: 65,
          suggestedAction: 'Consider reducing hold time to 3 minutes',
          timestamp: Date.now(),
        });
      }
    }

    return recs;
  }

  private analyzeHoneypotRate(trades: TradeRecord[]): Recommendation[] {
    const recs: Recommendation[] = [];

    // Analyze micro-test effectiveness
    const microTestPassed = trades.filter((t) => t.microTestPassed);
    if (microTestPassed.length < 5) return recs;

    const passedButLost100 = microTestPassed.filter((t) => t.result === 'loss' && t.pnlPct <= -99 && t.lotsSold === 0);
    const bypassRate = (passedButLost100.length / microTestPassed.length) * 100;

    if (bypassRate >= 20 && passedButLost100.length >= 3) {
      recs.push({
        id: 'micro-test-bypass',
        type: 'general',
        severity: 'critical',
        title: `Micro-test bypass rate: ${bypassRate.toFixed(0)}%`,
        detail: `${passedButLost100.length} tokens passed micro-test but turned out to be honeypots (delayed rug). Tokens: ${passedButLost100.slice(-5).map((t) => t.tokenSymbol).join(', ')}`,
        dataPoints: passedButLost100.length,
        confidence: 85,
        suggestedAction: 'Increase liquidity wait time or add post-buy liquidity monitoring',
        timestamp: Date.now(),
      });
    }

    return recs;
  }

  // ============================================================
  // Send recommendations to Telegram
  // ============================================================

  /** Escape HTML special chars so Telegram doesn't choke on < > & in dynamic text */
  private escHtml(text: string): string {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  private async sendRecommendations(recs: Recommendation[], totalTrades: number): Promise<void> {
    const critical = recs.filter((r) => r.severity === 'critical');
    const warnings = recs.filter((r) => r.severity === 'warning');
    const info = recs.filter((r) => r.severity === 'info');

    let msg = `📊 <strong>PATTERN ANALYSIS REPORT</strong>\n`;
    msg += `Based on <strong>${totalTrades}</strong> total trades\n\n`;

    if (critical.length > 0) {
      msg += `🔴 <strong>CRITICAL</strong>\n`;
      for (const r of critical) {
        msg += `• <strong>${this.escHtml(r.title)}</strong>\n  ${this.escHtml(r.detail)}\n  💡 <em>${this.escHtml(r.suggestedAction)}</em> (${r.confidence}% confidence)\n\n`;
      }
    }

    if (warnings.length > 0) {
      msg += `🟡 <strong>WARNINGS</strong>\n`;
      for (const r of warnings) {
        msg += `• <strong>${this.escHtml(r.title)}</strong>\n  ${this.escHtml(r.detail)}\n  💡 <em>${this.escHtml(r.suggestedAction)}</em> (${r.confidence}% confidence)\n\n`;
      }
    }

    if (info.length > 0) {
      msg += `🔵 <strong>INFO</strong>\n`;
      for (const r of info) {
        msg += `• <strong>${this.escHtml(r.title)}</strong>\n  ${this.escHtml(r.detail)}\n`;
      }
    }

    await this.telegram.sendRawMessage(msg);
    this.logger.log(`Sent ${recs.length} recommendations to Telegram`);
  }
}
