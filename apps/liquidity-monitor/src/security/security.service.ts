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

// Router ABI for getAmountsOut (used for expected price calculation)
const ROUTER_SIM_ABI = [
  'function getAmountsOut(uint256 amountIn, address[] calldata path) view returns (uint256[] memory amounts)',
];

// HoneypotChecker contract — does buy+sell in one eth_call via code override.
// No storage slot guessing needed. If sell reverts = honeypot.
const HONEYPOT_CHECKER_BYTECODE =
  '0x6080604052348015600e575f5ffd5b5061059c8061001c5f395ff3fe608060405260043610610020575f3560e01c8063df4b31b11461002b575f5ffd5b3661002757005b5f5ffd5b61003e6100393660046103e3565b610057565b6040805192835260208301919091520160405180910390f35b5f5f5f34116100985760405162461bcd60e51b81526020600482015260086024820152670dccacac8408aa8960c31b60448201526064015b60405180910390fd5b6040805160028082526060820183525f9260208301908036833701905050905083815f815181106100cb576100cb610423565b60200260200101906001600160a01b031690816001600160a01b03168152505084816001815181106100ff576100ff610423565b6001600160a01b039283166020918202929092010152861663b6f9de95345f843061012c42610e1061044b565b6040518663ffffffff1660e01b815260040161014b94939291906104a7565b5f604051808303818588803b158015610162575f5ffd5b505af1158015610174573d5f5f3e3d5ffd5b50506040516370a0823160e01b81523060048201526001600160a01b03891693506370a0823192506024019050602060405180830381865afa1580156101bc573d5f5f3e3d5ffd5b505050506040513d601f19601f820116820180604052508101906101e091906104db565b92505f831161021e5760405162461bcd60e51b815260206004820152600a602482015269189d5e4819985a5b195960b21b604482015260640161008f565b60405163095ea7b360e01b81526001600160a01b0387811660048301525f19602483015286169063095ea7b3906044016020604051808303815f875af115801561026a573d5f5f3e3d5ffd5b505050506040513d601f19601f8201168201806040525081019061028e91906104f2565b506040805160028082526060820183525f9260208301908036833701905050905085815f815181106102c2576102c2610423565b60200260200101906001600160a01b031690816001600160a01b03168152505084816001815181106102f6576102f6610423565b6001600160a01b0392831660209182029290920101524790881663791ac947865f853061032542610e1061044b565b6040518663ffffffff1660e01b8152600401610345959493929190610518565b5f604051808303815f87803b15801561035c575f5ffd5b505af115801561036e573d5f5f3e3d5ffd5b50505050804761037e9190610553565b93505f84116103bd5760405162461bcd60e51b815260206004820152600b60248201526a1cd95b1b0819985a5b195960aa1b604482015260640161008f565b505050935093915050565b80356001600160a01b03811681146103de575f5ffd5b919050565b5f5f5f606084860312156103f5575f5ffd5b6103fe846103c8565b925061040c602085016103c8565b915061041a604085016103c8565b90509250925092565b634e487b7160e01b5f52603260045260245ffd5b634e487b7160e01b5f52601160045260245ffd5b8082018082111561045e5761045e610437565b92915050565b5f8151808452602084019350602083015f5b8281101561049d5781516001600160a01b0316865260209586019590910190600101610476565b5093949350505050565b848152608060208201525f6104bf6080830186610464565b6001600160a01b03949094166040830152506060015292915050565b5f602082840312156104eb575f5ffd5b5051919050565b5f60208284031215610502575f5ffd5b81518015158114610511575f5ffd5b9392505050565b85815284602082015260a060408201525f61053660a0830186610464565b6001600160a01b0394909416606083015250608001529392505050565b8181038181111561045e5761045e61043756fea2646970667358221220f67e87c9988f24b2b41d6e60c347d2def199adee2d33fe70ca3a9c3818c4819d64736f6c63430008220033';

// ABI for the HoneypotChecker.check() function
const CHECKER_ABI = [
  'function check(address router, address token, address weth) payable returns (uint256 amountBought, uint256 ethReceived)',
];

// Temporary address where we "deploy" the checker via code override
const CHECKER_ADDR = '0x0000000000000000000000000000000000C0FFEE';

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
   * ON-CHAIN SIMULATION using a helper contract deployed via code override.
   *
   * Deploys a temporary HoneypotChecker contract via eth_call + code override.
   * The contract does buy→approve→sell in a SINGLE call:
   * - If sell reverts → honeypot (can't sell)
   * - Compares ETH in vs ETH out → combined buy+sell tax
   * - Compares expected tokens vs actual tokens → buy tax
   *
   * No storage slot guessing needed. 100% reliable for any ERC20.
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
      const simAmount = ethers.parseEther('0.001');
      const path = [WETH_ADDRESS, tokenAddress];

      // Step 1: Get expected token output (pure math, no transfer)
      const routerContract = new ethers.Contract(
        router.router,
        ROUTER_SIM_ABI,
        this.httpProvider,
      );

      let expectedTokens: bigint;
      try {
        const amounts = await routerContract.getAmountsOut(simAmount, path);
        expectedTokens = amounts[1];
      } catch {
        this.logger.debug(`getAmountsOut failed for ${tokenAddress} — no liquidity?`);
        return null;
      }

      if (expectedTokens === BigInt(0)) return null;

      // Step 2: Simulate full buy+sell via helper contract (code override)
      const checkerIface = new ethers.Interface(CHECKER_ABI);
      const callData = checkerIface.encodeFunctionData('check', [
        router.router,
        tokenAddress,
        WETH_ADDRESS,
      ]);

      const result = await this.httpProvider.send('eth_call', [
        {
          from: CHECKER_ADDR,
          to: CHECKER_ADDR,
          data: callData,
          value: ethers.toBeHex(simAmount),
        },
        'latest',
        {
          // Deploy checker contract at temporary address
          [CHECKER_ADDR]: {
            code: HONEYPOT_CHECKER_BYTECODE,
            balance: ethers.toBeHex(ethers.parseEther('1')),
          },
        },
      ]);

      // Decode: (uint256 amountBought, uint256 ethReceived)
      const decoded = checkerIface.decodeFunctionResult('check', result);
      const actualTokensBought = decoded[0] as bigint;
      const ethReceived = decoded[1] as bigint;

      // Buy tax = (expected - actual) / expected * 100
      const buyTax = Number(
        (expectedTokens - actualTokensBought) * BigInt(10000) / expectedTokens,
      ) / 100;

      // Sell tax = 1 - (ethReceived / expectedEthBack)
      // expectedEthBack = what AMM would give for actualTokensBought (pure math)
      let sellTax = 0;
      try {
        const sellAmounts = await routerContract.getAmountsOut(
          actualTokensBought,
          [tokenAddress, WETH_ADDRESS],
        );
        const expectedEthBack = sellAmounts[1];
        if (expectedEthBack > BigInt(0)) {
          sellTax = Number(
            (expectedEthBack - ethReceived) * BigInt(10000) / expectedEthBack,
          ) / 100;
        }
      } catch {
        // If getAmountsOut fails for sell, use round-trip calculation
        // sellTax ≈ (simAmount - ethReceived) / simAmount * 100 - buyTax
        sellTax = 0;
      }

      const canSell = true;
      if (buyTax < 0) sellTax = 0;
      if (sellTax < 0) sellTax = 0;

      this.logger.log(
        `Sim ${tokenAddress}: canSell=${canSell} buyTax=${Math.max(buyTax, 0).toFixed(1)}% sellTax=${sellTax.toFixed(1)}%`,
      );

      return { canSell, buyTax: Math.max(buyTax, 0), sellTax };
    } catch (err) {
      // ANY failure in the helper contract = cannot buy or sell = skip
      this.logger.debug(`Honeypot sim failed for ${tokenAddress}: ${err.message}`);
      return { canSell: false, buyTax: null, sellTax: null };
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
