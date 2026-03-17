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

    const message = [
      `🟢 <b>NEW LIQUIDITY ADDED</b>`,
      ``,
      `<b>Token:</b> ${this.escapeHtml(alert.tokenName)} ($${this.escapeHtml(alert.tokenSymbol)})`,
      `<b>Address:</b> <code>${alert.tokenAddress}</code>`,
      `<b>ETH Added:</b> ${alert.ethAmount} ETH`,
      `<b>DEX:</b> ${this.escapeHtml(alert.dexName)}`,
      `<b>Type:</b> ${tokenType}`,
      `<b>Security:</b> ${alert.securitySummary}`,
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
