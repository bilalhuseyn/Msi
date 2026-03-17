import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { Telegraf } from 'telegraf';

export interface LiquidityAlert {
  tokenName: string;
  tokenSymbol: string;
  tokenAddress: string;
  pairAddress: string;
  ethAmount: string;
  dexName: string;
  isNewToken: boolean;
  securitySummary: string;
  securityEmoji: string;
  buyTax: number | null;
  sellTax: number | null;
  poolEthReserve: number | null;
}

@Injectable()
export class TelegramService implements OnModuleInit {
  private readonly logger = new Logger(TelegramService.name);
  private bot: Telegraf;
  private chatId: string;

  constructor(private readonly config: ConfigService) {}

  onModuleInit() {
    const token = this.config.get<string>('telegram.botToken');
    this.chatId = this.config.get<string>('telegram.chatId');

    if (!token || !this.chatId) {
      this.logger.warn(
        'Telegram bot token or chat ID not configured. Notifications disabled.',
      );
      return;
    }

    this.bot = new Telegraf(token);
    this.bot.launch().catch((err) => {
      this.logger.error(`Telegraf launch error: ${err.message}`);
    });

    this.logger.log('Telegram bot started');
  }

  async sendLiquidityAlert(alert: LiquidityAlert): Promise<void> {
    if (!this.bot) return;

    const tokenType = alert.isNewToken ? '🆕 New Token' : '📈 Existing';
    const dexScreener = `https://dexscreener.com/ethereum/${alert.pairAddress}`;
    const dexTools = `https://www.dextools.io/app/ether/pair-explorer/${alert.pairAddress}`;

    // Pool liquidity info
    const poolLine = alert.poolEthReserve !== null
      ? `<b>Pool Liquidity:</b> ${alert.poolEthReserve.toFixed(2)} ETH`
      : `<b>Pool Liquidity:</b> Unknown`;

    // Tax lines
    const buyTaxStr = alert.buyTax !== null ? `${alert.buyTax.toFixed(1)}%` : 'N/A';
    const sellTaxStr = alert.sellTax !== null ? `${alert.sellTax.toFixed(1)}%` : 'N/A';
    const taxLine = `<b>Tax:</b> Buy ${buyTaxStr} | Sell ${sellTaxStr}`;

    // Hybrid lot selling plan
    const lotPlan = this.calculateLotPlan(alert.poolEthReserve);

    const message = [
      `🟢 <b>NEW LIQUIDITY ADDED</b>`,
      ``,
      `<b>Token:</b> ${this.escapeHtml(alert.tokenName)} ($${this.escapeHtml(alert.tokenSymbol)})`,
      `<b>Address:</b> <code>${alert.tokenAddress}</code>`,
      `<b>ETH Added:</b> ${alert.ethAmount} ETH`,
      `<b>DEX:</b> ${this.escapeHtml(alert.dexName)}`,
      `<b>Type:</b> ${tokenType}`,
      poolLine,
      taxLine,
      `<b>Security:</b> ${alert.securitySummary}`,
      ``,
      `📋 <b>Lot Sell Plan:</b>`,
      lotPlan,
      ``,
      `📊 <a href="${dexScreener}">DexScreener</a> | <a href="${dexTools}">DexTools</a>`,
    ].join('\n');

    try {
      await this.bot.telegram.sendMessage(this.chatId, message, {
        parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      });
    } catch (err) {
      this.logger.error(`Failed to send Telegram message: ${err.message}`);
    }
  }

  /**
   * Hybrid lot selling plan:
   * - Max 3% of pool per lot to keep slippage < 3%
   * - First 2 lots: fast (immediate, recover principal)
   * - Remaining lots: slow (2-3 blocks / ~30s apart, let arbers rebalance)
   * - Stop-loss: if price drops >20% mid-sell, abort remaining lots
   */
  private calculateLotPlan(poolEthReserve: number | null): string {
    if (!poolEthReserve || poolEthReserve <= 0) {
      return `  Unknown pool — use 5 equal lots, 30s apart`;
    }

    // Max sell per lot = 3% of pool ETH
    const maxPerLot = poolEthReserve * 0.03;
    const maxPerLotUsd = (maxPerLot * 1900).toFixed(0);

    // Calculate lot count for various position sizes
    // We show a general plan since we don't know entry size yet
    const lines: string[] = [];
    lines.push(`  Max/lot: ${maxPerLot.toFixed(3)} ETH (~$${maxPerLotUsd})`);
    lines.push(`  Lot 1-2: ⚡ Instant (recover capital)`);
    lines.push(`  Lot 3+: 🐢 30s apart (arb recovery)`);
    lines.push(`  ⛔ Stop if price drops >20%`);

    return lines.join('\n');
  }

  async sendTradeNotification(
    title: string,
    tokenSymbol: string,
    tokenAddress: string,
    ethAmount: number,
    txHash: string,
    details: string,
  ): Promise<void> {
    if (!this.bot) return;

    const etherscanTx = `https://etherscan.io/tx/${txHash}`;
    const message = [
      `<b>${title}</b>`,
      ``,
      `<b>Token:</b> $${this.escapeHtml(tokenSymbol)}`,
      `<b>Address:</b> <code>${tokenAddress}</code>`,
      `<b>Amount:</b> ${ethAmount.toFixed(4)} ETH`,
      ``,
      this.escapeHtml(details),
      ``,
      `<a href="${etherscanTx}">View on Etherscan</a>`,
    ].join('\n');

    try {
      await this.bot.telegram.sendMessage(this.chatId, message, {
        parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      });
    } catch (err) {
      this.logger.error(`Failed to send trade notification: ${err.message}`);
    }
  }

  async sendStartupMessage(): Promise<void> {
    if (!this.bot) return;

    try {
      await this.bot.telegram.sendMessage(
        this.chatId,
        '🟢 <b>Liquidity Monitor Started</b>\nListening for addLiquidityETH transactions...',
        { parse_mode: 'HTML' },
      );
    } catch (err) {
      this.logger.error(`Failed to send startup message: ${err.message}`);
    }
  }

  private escapeHtml(text: string): string {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }
}
