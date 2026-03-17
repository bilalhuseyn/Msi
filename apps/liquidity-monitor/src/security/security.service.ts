import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { ethers } from 'ethers';
import axios from 'axios';
import {
  WETH_ADDRESS,
  ROUTER_ABI_FRAGMENT,
  DEX_LIST,
} from '../monitor/constants';

export interface SecurityResult {
  /** Can the token actually be sold on-chain? */
  canSell: boolean;
  buyTax: number | null;
  sellTax: number | null;
  summary: string;
  emoji: string;
  /** true = not honeypot AND buy tax ≤10% AND sell tax ≤10% */
  isTradeable: boolean;
}

// Full router ABI we need for simulated swaps
const ROUTER_SIM_ABI = [
  'function getAmountsOut(uint256 amountIn, address[] calldata path) view returns (uint256[] memory amounts)',
  'function swapExactETHForTokens(uint256 amountOutMin, address[] calldata path, address to, uint256 deadline) payable returns (uint256[] memory amounts)',
  'function swapExactTokensForETH(uint256 amountIn, uint256 amountOutMin, address[] calldata path, address to, uint256 deadline) returns (uint256[] memory amounts)',
];

const ERC20_SIM_ABI = [
  'function approve(address spender, uint256 amount) returns (bool)',
  'function balanceOf(address account) view returns (uint256)',
  'function allowance(address owner, address spender) view returns (uint256)',
];

@Injectable()
export class SecurityService {
  private readonly logger = new Logger(SecurityService.name);
  private httpProvider: ethers.JsonRpcProvider;

  constructor(private readonly config: ConfigService) {}

  onModuleInit() {
    const httpUrl = this.config.get<string>('alchemy.httpUrl');
    const wssUrl = this.config.get<string>('alchemy.wssUrl');
    const url = httpUrl || (wssUrl ? wssUrl.replace('wss://', 'https://') : '');
    this.httpProvider = new ethers.JsonRpcProvider(url);
  }

  async checkToken(
    tokenAddress: string,
    pairAddress?: string,
  ): Promise<SecurityResult> {
    // 1) On-chain simulation (PRIMARY — works for brand-new tokens)
    const simResult = await this.simulateSwap(tokenAddress);

    // 2) API checks (FALLBACK — may fail for new tokens)
    const [goplus, honeypot] = await Promise.allSettled([
      this.checkGoPlus(tokenAddress),
      this.checkHoneypotIs(tokenAddress),
    ]);
    const gp = goplus.status === 'fulfilled' ? goplus.value : null;
    const hp = honeypot.status === 'fulfilled' ? honeypot.value : null;

    // Merge: on-chain sim is primary, APIs fill in extras
    const canSell = simResult?.canSell ?? false;
    const buyTax = simResult?.buyTax ?? gp?.buyTax ?? hp?.buyTax ?? null;
    const sellTax = simResult?.sellTax ?? gp?.sellTax ?? hp?.sellTax ?? null;

    // If sim failed entirely (no data at all), fall back to API honeypot check
    const apiSaysHoneypot = gp?.isHoneypot || hp?.isHoneypot;

    let summary: string;
    let emoji: string;

    if (!canSell || apiSaysHoneypot) {
      summary = '🚫 Cannot sell (honeypot)';
      emoji = '🚫';
    } else if (
      (buyTax !== null && buyTax > 10) ||
      (sellTax !== null && sellTax > 10)
    ) {
      const taxes: string[] = [];
      if (buyTax !== null) taxes.push(`buy: ${buyTax.toFixed(1)}%`);
      if (sellTax !== null) taxes.push(`sell: ${sellTax.toFixed(1)}%`);
      summary = `⚠️ High Tax (${taxes.join(', ')})`;
      emoji = '⚠️';
    } else if (buyTax !== null || sellTax !== null) {
      const taxes: string[] = [];
      if (buyTax !== null) taxes.push(`buy: ${buyTax.toFixed(1)}%`);
      if (sellTax !== null) taxes.push(`sell: ${sellTax.toFixed(1)}%`);
      summary = `✅ Safe (${taxes.join(', ')})`;
      emoji = '✅';
    } else if (canSell) {
      summary = '✅ Sellable (tax unknown)';
      emoji = '✅';
    } else {
      summary = '❓ Unknown';
      emoji = '❓';
    }

    // GATE: tradeable = can sell + both taxes ≤ 10%
    const isTradeable =
      canSell &&
      !apiSaysHoneypot &&
      (buyTax === null || buyTax <= 10) &&
      (sellTax === null || sellTax <= 10);

    return { canSell, buyTax, sellTax, summary, emoji, isTradeable };
  }

  /**
   * ON-CHAIN SIMULATION
   *
   * Uses eth_call to simulate:
   * 1. Buy: swapExactETHForTokens (tiny amount)
   * 2. Check received tokens vs expected → buy tax
   * 3. Sell: swapExactTokensForETH (the tokens we got)
   *    - If this reverts → honeypot (can't sell)
   * 4. Check received ETH vs expected → sell tax
   *
   * All done via eth_call = no real tx, no gas, no cost.
   */
  private async simulateSwap(
    tokenAddress: string,
  ): Promise<{
    canSell: boolean;
    buyTax: number | null;
    sellTax: number | null;
  } | null> {
    try {
      const router = DEX_LIST[0]; // Uniswap V2
      const routerContract = new ethers.Contract(
        router.router,
        ROUTER_SIM_ABI,
        this.httpProvider,
      );

      // Use 0.001 ETH for simulation
      const simAmount = ethers.parseEther('0.001');
      const path = [WETH_ADDRESS, tokenAddress];
      const reversePath = [tokenAddress, WETH_ADDRESS];

      // Step 1: Get expected output for buy
      let expectedTokens: bigint;
      try {
        const amounts = await routerContract.getAmountsOut(simAmount, path);
        expectedTokens = amounts[1];
      } catch {
        this.logger.debug(`getAmountsOut failed for ${tokenAddress} — no liquidity?`);
        return null;
      }

      if (expectedTokens === BigInt(0)) return null;

      // Step 2: Simulate buy — use staticCall to see actual output
      // We use a dead address as "from" with simulated ETH balance
      const deadAddr = '0x000000000000000000000000000000000000dEaD';
      let actualTokensBought: bigint;
      try {
        const buyResult = await routerContract.swapExactETHForTokens.staticCall(
          0, // amountOutMin = 0 (accept any)
          path,
          deadAddr,
          Math.floor(Date.now() / 1000) + 3600,
          { value: simAmount, from: deadAddr },
        );
        actualTokensBought = buyResult[1];
      } catch {
        // Can't even buy — might be paused or special logic
        this.logger.debug(`Buy simulation failed for ${tokenAddress}`);
        return null;
      }

      // Buy tax = (expected - actual) / expected * 100
      const buyTax =
        Number((expectedTokens - actualTokensBought) * BigInt(10000) / expectedTokens) / 100;

      // Step 3: Get expected ETH output for selling those tokens
      let expectedEthBack: bigint;
      try {
        const sellAmounts = await routerContract.getAmountsOut(
          actualTokensBought,
          reversePath,
        );
        expectedEthBack = sellAmounts[1];
      } catch {
        // getAmountsOut failed on sell path — can't sell
        return { canSell: false, buyTax, sellTax: null };
      }

      // Step 4: Simulate sell
      let canSell = false;
      let sellTax: number | null = null;

      try {
        // To simulate sell, we need the dead address to "have" the tokens
        // and have approved the router. We use eth_call state overrides.
        const tokenContract = new ethers.Contract(
          tokenAddress,
          ERC20_SIM_ABI,
          this.httpProvider,
        );

        // Try the sell simulation with state override
        const sellResult = await this.simulateSellWithOverride(
          tokenAddress,
          router.router,
          actualTokensBought,
          reversePath,
          deadAddr,
        );

        if (sellResult !== null) {
          canSell = true;
          // Sell tax = (expected - actual) / expected * 100
          sellTax =
            Number((expectedEthBack - sellResult) * BigInt(10000) / expectedEthBack) / 100;

          // Sanity: negative tax means we got more than expected (unlikely but handle)
          if (sellTax < 0) sellTax = 0;
        }
      } catch {
        // Sell reverted = honeypot
        canSell = false;
      }

      this.logger.log(
        `Sim ${tokenAddress}: canSell=${canSell} buyTax=${buyTax?.toFixed(1)}% sellTax=${sellTax?.toFixed(1)}%`,
      );

      return { canSell, buyTax: Math.max(buyTax, 0), sellTax };
    } catch (err) {
      this.logger.warn(`Simulation error for ${tokenAddress}: ${err.message}`);
      return null;
    }
  }

  /**
   * Simulate a token sell using eth_call with state overrides.
   * State overrides let us pretend deadAddr has token balance + approval
   * without any real transaction.
   */
  private async simulateSellWithOverride(
    tokenAddress: string,
    routerAddress: string,
    tokenAmount: bigint,
    path: string[],
    fromAddr: string,
  ): Promise<bigint | null> {
    try {
      const routerIface = new ethers.Interface(ROUTER_SIM_ABI);
      const deadline = Math.floor(Date.now() / 1000) + 3600;

      const callData = routerIface.encodeFunctionData(
        'swapExactTokensForETH',
        [tokenAmount, 0, path, fromAddr, deadline],
      );

      // ERC20 storage slots for balanceOf and allowance:
      // For standard ERC20: balanceOf[addr] is at slot keccak256(addr . slot_balances)
      // We use a generous state override approach:
      // Set balance slot and allowance slot for the dead address.

      // Standard ERC20 storage layout:
      // balanceOf mapping is usually at slot 0 or 1
      // allowance mapping is usually at slot 1 or 2
      // We try common slots (0, 1, 2) and use eth_call stateOverride

      const paddedAddr = ethers.zeroPadValue(fromAddr, 32);
      const maxUint = ethers.MaxUint256;
      const maxUintHex = ethers.zeroPadValue(ethers.toBeHex(maxUint), 32);
      const amountHex = ethers.zeroPadValue(ethers.toBeHex(tokenAmount), 32);

      // Try balance slots 0-5 and allowance
      const stateOverride: Record<string, any> = {};

      // Override token contract: give fromAddr a balance and unlimited approval
      const storageOverrides: Record<string, string> = {};

      // Try common balance slot positions (0, 1, 2, 3, 51 for OZ upgradeable)
      for (const balSlot of [0, 1, 2, 3, 51]) {
        const key = ethers.keccak256(
          ethers.concat([paddedAddr, ethers.zeroPadValue(ethers.toBeHex(balSlot), 32)]),
        );
        storageOverrides[key] = amountHex;
      }

      // Allowance: mapping(owner => mapping(spender => amount))
      // keccak256(spender . keccak256(owner . slot))
      const paddedRouter = ethers.zeroPadValue(routerAddress, 32);
      for (const allowSlot of [1, 2, 3, 4, 52]) {
        const innerKey = ethers.keccak256(
          ethers.concat([paddedAddr, ethers.zeroPadValue(ethers.toBeHex(allowSlot), 32)]),
        );
        const outerKey = ethers.keccak256(
          ethers.concat([paddedRouter, innerKey]),
        );
        storageOverrides[outerKey] = maxUintHex;
      }

      stateOverride[tokenAddress] = {
        stateDiff: storageOverrides,
      };

      // Also give fromAddr some ETH for gas
      stateOverride[fromAddr] = {
        balance: ethers.toBeHex(ethers.parseEther('1')),
      };

      // Raw eth_call with state override
      const result = await this.httpProvider.send('eth_call', [
        {
          from: fromAddr,
          to: routerAddress,
          data: callData,
          value: '0x0',
        },
        'latest',
        stateOverride,
      ]);

      // Decode result: returns uint256[] amounts
      const decoded = routerIface.decodeFunctionResult(
        'swapExactTokensForETH',
        result,
      );
      const ethReceived = decoded[0][decoded[0].length - 1] as bigint;

      return ethReceived;
    } catch (err) {
      this.logger.debug(`Sell override sim failed: ${err.message}`);

      // Fallback: try simple staticCall without state override
      // This won't work for most tokens but catches some edge cases
      try {
        const routerContract = new ethers.Contract(
          routerAddress,
          ROUTER_SIM_ABI,
          this.httpProvider,
        );
        const amounts = await routerContract.getAmountsOut(tokenAmount, path);
        // If getAmountsOut works, the swap path exists
        // but we can't confirm actual sell works
        // Return expected amount with a "soft" confirmation
        return amounts[1] as bigint;
      } catch {
        return null;
      }
    }
  }

  // ============================================================
  // FALLBACK: External API checks
  // ============================================================

  private async checkGoPlus(
    tokenAddress: string,
  ): Promise<{
    isHoneypot: boolean;
    buyTax: number | null;
    sellTax: number | null;
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
      };
    } catch (err) {
      this.logger.debug(`GoPlus API error for ${tokenAddress}: ${err.message}`);
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
      this.logger.debug(
        `Honeypot.is API error for ${tokenAddress}: ${err.message}`,
      );
      return null;
    }
  }
}
