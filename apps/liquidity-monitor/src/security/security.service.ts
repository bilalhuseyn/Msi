import { Injectable, Logger } from '@nestjs/common';
import axios from 'axios';

export interface SecurityResult {
  isHoneypot: boolean;
  buyTax: number | null;
  sellTax: number | null;
  isOpenSource: boolean | null;
  isMintable: boolean | null;
  summary: string; // "Safe" | "Risky (tax: X%)" | "Honeypot" | "Unknown"
  emoji: string;
}

@Injectable()
export class SecurityService {
  private readonly logger = new Logger(SecurityService.name);

  async checkToken(tokenAddress: string): Promise<SecurityResult> {
    const [goplus, honeypot] = await Promise.allSettled([
      this.checkGoPlus(tokenAddress),
      this.checkHoneypotIs(tokenAddress),
    ]);

    const gp =
      goplus.status === 'fulfilled' ? goplus.value : null;
    const hp =
      honeypot.status === 'fulfilled' ? honeypot.value : null;

    // Merge results: GoPlus is primary, honeypot.is is secondary
    const isHoneypot = gp?.isHoneypot ?? hp?.isHoneypot ?? false;
    const buyTax = gp?.buyTax ?? hp?.buyTax ?? null;
    const sellTax = gp?.sellTax ?? hp?.sellTax ?? null;

    let summary: string;
    let emoji: string;

    if (isHoneypot) {
      summary = '🚫 Honeypot';
      emoji = '🚫';
    } else if (sellTax !== null && sellTax > 10) {
      summary = `⚠️ High Tax (sell: ${sellTax}%)`;
      emoji = '⚠️';
    } else if (sellTax !== null && sellTax > 0) {
      summary = `✅ Safe (sell tax: ${sellTax}%)`;
      emoji = '✅';
    } else if (gp || hp) {
      summary = '✅ Safe';
      emoji = '✅';
    } else {
      summary = '❓ Unknown (API error)';
      emoji = '❓';
    }

    return {
      isHoneypot,
      buyTax,
      sellTax,
      isOpenSource: gp?.isOpenSource ?? null,
      isMintable: gp?.isMintable ?? null,
      summary,
      emoji,
    };
  }

  private async checkGoPlus(
    tokenAddress: string,
  ): Promise<{
    isHoneypot: boolean;
    buyTax: number | null;
    sellTax: number | null;
    isOpenSource: boolean | null;
    isMintable: boolean | null;
  }> {
    try {
      const { data } = await axios.get(
        `https://api.gopluslabs.io/api/v1/token_security/1`,
        {
          params: { contract_addresses: tokenAddress },
          timeout: 10000,
        },
      );

      const info = data?.result?.[tokenAddress.toLowerCase()];
      if (!info) return null;

      return {
        isHoneypot: info.is_honeypot === '1',
        buyTax: info.buy_tax ? parseFloat(info.buy_tax) * 100 : null,
        sellTax: info.sell_tax ? parseFloat(info.sell_tax) * 100 : null,
        isOpenSource: info.is_open_source === '1',
        isMintable: info.is_mintable === '1',
      };
    } catch (err) {
      this.logger.warn(`GoPlus API error for ${tokenAddress}: ${err.message}`);
      return null;
    }
  }

  private async checkHoneypotIs(
    tokenAddress: string,
  ): Promise<{
    isHoneypot: boolean;
    buyTax: number | null;
    sellTax: number | null;
  }> {
    try {
      const { data } = await axios.get(
        `https://api.honeypot.is/v2/IsHoneypot`,
        {
          params: { address: tokenAddress, chainID: 1 },
          timeout: 10000,
        },
      );

      return {
        isHoneypot: data?.honeypotResult?.isHoneypot ?? false,
        buyTax: data?.simulationResult?.buyTax
          ? parseFloat(data.simulationResult.buyTax) * 100
          : null,
        sellTax: data?.simulationResult?.sellTax
          ? parseFloat(data.simulationResult.sellTax) * 100
          : null,
      };
    } catch (err) {
      this.logger.warn(
        `Honeypot.is API error for ${tokenAddress}: ${err.message}`,
      );
      return null;
    }
  }
}
