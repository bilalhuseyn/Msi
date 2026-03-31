import { Injectable, Logger } from '@nestjs/common';
import * as fs from 'fs';
import * as path from 'path';
import { ChainId } from '../config/chains';

export interface TradeRecord {
  // Identity
  tokenAddress: string;
  tokenSymbol: string;
  chain: ChainId;
  pairAddress: string;
  deployer: string | null;

  // Token characteristics
  liquidityNative: number;
  bytecodeScore: number;
  hasNonLatinName: boolean;
  microTestPassed: boolean;
  microTestCostNative: number;
  securityGate: 'approved' | 'needs-micro-test';

  // Trade result
  result: 'win' | 'loss' | 'buy-failed';
  pnlPct: number;
  pnlNative: number;
  pnlUSD: number;
  ethSpent: number;
  ethReceived: number;
  lotsSold: number;
  holdTimeMs: number;
  failReason?: string;

  // Meta
  timestamp: number;
  buyTxHash: string;
}

export interface SkippedTokenRecord {
  tokenAddress: string;
  tokenSymbol: string;
  chain: ChainId;
  liquidityNative: number;
  reason: 'micro-test-failed' | 'rejected' | 'non-latin' | 'below-min-liq' | 'wallet-not-ready' | 'liq-removed-during-observation' | 'no-sells-observed';
  bytecodeScore: number;
  timestamp: number;
}

@Injectable()
export class TradeLoggerService {
  private readonly logger = new Logger('TradeLogger');
  private readonly HISTORY_FILE = path.join(process.cwd(), 'trade-history.json');
  private readonly SKIPPED_FILE = path.join(process.cwd(), 'skipped-tokens.json');

  private tradeHistory: TradeRecord[] = [];
  private skippedTokens: SkippedTokenRecord[] = [];

  constructor() {
    this.loadHistory();
  }

  logTrade(record: TradeRecord): void {
    this.tradeHistory.push(record);
    this.saveHistory();
    this.logger.log(
      `[${record.chain}] Trade logged: ${record.tokenSymbol} ${record.result} ${record.pnlPct.toFixed(1)}% ($${record.pnlUSD.toFixed(2)})`,
    );
  }

  logSkipped(record: SkippedTokenRecord): void {
    this.skippedTokens.push(record);
    // Keep only last 500 skipped tokens
    if (this.skippedTokens.length > 500) {
      this.skippedTokens = this.skippedTokens.slice(-500);
    }
    this.saveSkipped();
  }

  getHistory(): TradeRecord[] {
    return this.tradeHistory;
  }

  getRecentHistory(hours = 24): TradeRecord[] {
    const cutoff = Date.now() - hours * 60 * 60 * 1000;
    return this.tradeHistory.filter((t) => t.timestamp >= cutoff);
  }

  getSkippedTokens(): SkippedTokenRecord[] {
    return this.skippedTokens;
  }

  private loadHistory(): void {
    try {
      if (fs.existsSync(this.HISTORY_FILE)) {
        const raw = fs.readFileSync(this.HISTORY_FILE, 'utf-8');
        this.tradeHistory = JSON.parse(raw);
        this.logger.log(`Loaded ${this.tradeHistory.length} trade records from disk`);
      }
    } catch (err) {
      this.logger.warn(`Failed to load trade history: ${err.message}`);
    }
    try {
      if (fs.existsSync(this.SKIPPED_FILE)) {
        const raw = fs.readFileSync(this.SKIPPED_FILE, 'utf-8');
        this.skippedTokens = JSON.parse(raw);
      }
    } catch {}
  }

  private saveHistory(): void {
    try {
      fs.writeFileSync(this.HISTORY_FILE, JSON.stringify(this.tradeHistory, null, 2));
    } catch (err) {
      this.logger.warn(`Failed to save trade history: ${err.message}`);
    }
  }

  private saveSkipped(): void {
    try {
      fs.writeFileSync(this.SKIPPED_FILE, JSON.stringify(this.skippedTokens, null, 2));
    } catch {}
  }
}
