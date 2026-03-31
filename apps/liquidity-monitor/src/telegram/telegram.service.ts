import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { Telegraf } from 'telegraf';
import { ChainConfig } from '../config/chains';

export interface LiquidityAlert {
  chainConfig: ChainConfig;
  tokenName: string;
  tokenSymbol: string;
  tokenAddress: string;
  pairAddress: string;
  nativeAmount: string;
  dexName: string;
  isNewToken: boolean;
  securitySummary: string;
  securityEmoji: string;
  buyTax: number | null;
  sellTax: number | null;
  poolNativeReserve: number | null;
  nativePriceUSD?: number;
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

    const chain = alert.chainConfig;
    const tokenType = alert.isNewToken ? '🆕 New Token' : '📈 Existing';
    const dexScreener = `https://dexscreener.com/${chain.dexScreenerSlug}/${alert.pairAddress}`;
    const dexTools = `https://www.dextools.io/app/${chain.dexToolsSlug}/pair-explorer/${alert.pairAddress}`;
    const explorerToken = `${chain.explorerUrl}/token/${alert.tokenAddress}`;

    // USD helper
    const usd = (amount: number) => {
      if (alert.nativePriceUSD && alert.nativePriceUSD > 0) {
        return ` ($${(amount * alert.nativePriceUSD).toFixed(0)})`;
      }
      return '';
    };

    // Pool liquidity info
    const poolLine = alert.poolNativeReserve !== null
      ? `<b>Pool Liquidity:</b> ${alert.poolNativeReserve.toFixed(2)} ${chain.nativeSymbol}${usd(alert.poolNativeReserve)}`
      : `<b>Pool Liquidity:</b> Unknown`;

    // Tax lines
    const buyTaxStr = alert.buyTax !== null ? `${alert.buyTax.toFixed(1)}%` : 'N/A';
    const sellTaxStr = alert.sellTax !== null ? `${alert.sellTax.toFixed(1)}%` : 'N/A';
    const taxLine = `<b>Tax:</b> Buy ${buyTaxStr} | Sell ${sellTaxStr}`;

    // Hybrid lot selling plan
    const lotPlan = this.calculateLotPlan(alert.poolNativeReserve, chain.nativeSymbol);

    const message = [
      `🟢 <b>NEW LIQUIDITY ADDED</b> [${chain.name}]`,
      `<b>${this.escapeHtml(alert.tokenName)}</b> ($${this.escapeHtml(alert.tokenSymbol)})`,
      `<b>Security:</b> ${alert.securitySummary}`,
      ``,
      `📊 <a href="${dexScreener}">DexScreener</a>`,
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
  private calculateLotPlan(poolNativeReserve: number | null, nativeSymbol: string): string {
    if (!poolNativeReserve || poolNativeReserve <= 0) {
      return `  Unknown pool — use 5 equal lots, 30s apart`;
    }

    // Max sell per lot = 3% of pool
    const maxPerLot = poolNativeReserve * 0.03;

    const lines: string[] = [];
    lines.push(`  Max/lot: ${maxPerLot.toFixed(3)} ${nativeSymbol}`);
    lines.push(`  Lot 1-2: ⚡ Instant (recover capital)`);
    lines.push(`  Lot 3+: 🐢 30s apart (arb recovery)`);
    lines.push(`  ⛔ Stop if price drops >20%`);

    return lines.join('\n');
  }

  async sendTradeNotification(
    title: string,
    tokenSymbol: string,
    tokenAddress: string,
    nativeAmount: number,
    txHash: string,
    details: string,
    chain?: ChainConfig,
  ): Promise<void> {
    if (!this.bot) return;

    const explorerUrl = chain?.explorerUrl || 'https://etherscan.io';
    const explorerName = chain?.explorerName || 'Etherscan';
    const nativeSymbol = chain?.nativeSymbol || 'ETH';
    const chainLabel = chain ? ` [${chain.name}]` : '';

    const txLink = `${explorerUrl}/tx/${txHash}`;
    const message = [
      `<b>${title}</b>${chainLabel}`,
      ``,
      `<b>Token:</b> $${this.escapeHtml(tokenSymbol)}`,
      `<b>Address:</b> <code>${tokenAddress}</code>`,
      `<b>Amount:</b> ${nativeAmount.toFixed(4)} ${nativeSymbol}`,
      ``,
      this.escapeHtml(details),
      ``,
      `<a href="${txLink}">View on ${explorerName}</a>`,
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

  async sendStartupMessage(chainNames?: string[]): Promise<void> {
    if (!this.bot) return;

    const chainsLine = chainNames && chainNames.length > 0
      ? `Chains: ${chainNames.join(', ')}`
      : 'Listening for addLiquidityETH transactions...';

    try {
      await this.bot.telegram.sendMessage(
        this.chatId,
        `🟢 <b>Liquidity Monitor Started</b>\n${chainsLine}`,
        { parse_mode: 'HTML' },
      );
    } catch (err) {
      this.logger.error(`Failed to send startup message: ${err.message}`);
    }
  }

  async sendRawMessage(html: string): Promise<void> {
    if (!this.bot) return;
    try {
      await this.bot.telegram.sendMessage(this.chatId, html, {
        parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      });
    } catch (err) {
      this.logger.error(`Failed to send raw message: ${err.message}`);
    }
  }

  private escapeHtml(text: string): string {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }
}
