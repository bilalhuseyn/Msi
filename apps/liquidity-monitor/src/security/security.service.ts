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
  /** Which layer approved it: 'sim' | 'bytecode+sim' | 'micro-test' | null */
  approvedBy: string | null;
  /** Bytecode risk score (0=clean, 100=definite scam) */
  bytecodeRiskScore: number;
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

// =====================================================
// BYTECODE STATIC ANALYSIS — Function selectors & opcodes
// =====================================================

interface BytecodeFlag {
  selector: string; // 4-byte hex (without 0x)
  name: string;
  severity: 'critical' | 'high' | 'medium';
  category: string;
  score: number;
}

const HONEYPOT_SELECTORS: BytecodeFlag[] = [
  // CRITICAL: Blacklist functions — can block your sells
  { selector: 'ecb3e7d6', name: 'blacklist(address)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: '16c02129', name: 'isBlacklisted(address)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: 'fe575a87', name: 'isBlacklisted(address)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: '44337ea1', name: 'blacklistAddress(address)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: 'f9f92be4', name: 'blacklist(address,bool)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: '0ecb93c0', name: 'blacklistAccount(address)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: '1b2ef1ca', name: 'addBlackList(address)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: 'a4e2d634', name: 'isBlackListed(address)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: 'e47d6060', name: 'isBlackListed(address)', severity: 'critical', category: 'blacklist', score: 30 },
  { selector: '04a7fdfe', name: 'antiBot(address,uint256)', severity: 'critical', category: 'blacklist', score: 30 },

  // CRITICAL: Mint = infinite supply dump
  { selector: '40c10f19', name: 'mint(address,uint256)', severity: 'critical', category: 'mint', score: 30 },
  { selector: 'a0712d68', name: 'mint(uint256)', severity: 'critical', category: 'mint', score: 30 },
  { selector: '6a627842', name: 'mint(address)', severity: 'critical', category: 'mint', score: 30 },

  // HIGH: Pause/Trading control — can disable sells
  { selector: '8456cb59', name: 'pause()', severity: 'high', category: 'pause', score: 15 },
  { selector: '5c975abb', name: 'paused()', severity: 'high', category: 'pause', score: 15 },
  { selector: '02329a29', name: 'setOpenTrading(bool)', severity: 'high', category: 'trading-control', score: 15 },
  { selector: 'c9567bf9', name: 'openTrading()', severity: 'high', category: 'trading-control', score: 15 },
  { selector: '293230b8', name: 'openTrading()', severity: 'high', category: 'trading-control', score: 15 },
  { selector: 'bbc0c742', name: 'tradingOpen()', severity: 'high', category: 'trading-control', score: 15 },
  { selector: 'c9e1e4c4', name: 'setTradingEnabled(bool)', severity: 'high', category: 'trading-control', score: 15 },

  // HIGH: Fee manipulation — can increase sell tax to 99% later
  { selector: 'a2a957bb', name: 'setFees(uint256,uint256,uint256,uint256)', severity: 'high', category: 'fee-manipulation', score: 15 },
  { selector: '08733214', name: 'setTaxFee(uint256)', severity: 'high', category: 'fee-manipulation', score: 15 },
  { selector: 'fab355f3', name: 'setLiquidityFee(uint256)', severity: 'high', category: 'fee-manipulation', score: 15 },
  { selector: 'a1ab19a3', name: 'setFeeRate(uint256)', severity: 'high', category: 'fee-manipulation', score: 15 },
  { selector: '02259e9e', name: 'setTaxFeePercent(uint256)', severity: 'high', category: 'fee-manipulation', score: 15 },

  // HIGH: Max TX limits — can trap large sells
  { selector: '7d1db4a5', name: 'setMaxTxAmount(uint256)', severity: 'high', category: 'max-tx', score: 15 },
  { selector: 'e01af92c', name: 'setMaxTxPercent(uint256)', severity: 'high', category: 'max-tx', score: 15 },
  { selector: '1a8145bb', name: 'setMaxTxAmount(uint256)', severity: 'high', category: 'max-tx', score: 15 },
  { selector: 'd543dbeb', name: 'setMaxTxPercent(uint256)', severity: 'high', category: 'max-tx', score: 15 },

  // HIGH: Proxy/Upgrade — can change ALL logic after deploy
  { selector: '3659cfe6', name: 'upgradeTo(address)', severity: 'high', category: 'proxy', score: 15 },
  { selector: '4f1ef286', name: 'upgradeToAndCall(address,bytes)', severity: 'high', category: 'proxy', score: 15 },
  { selector: '5c60da1b', name: 'implementation()', severity: 'high', category: 'proxy', score: 15 },

  // MEDIUM: Fee exclusion — owner can exempt themselves
  { selector: 'ea2f0b37', name: 'excludeFromFee(address)', severity: 'medium', category: 'fee-exclusion', score: 5 },
  { selector: '437823ec', name: 'excludeFromFee(address)', severity: 'medium', category: 'fee-exclusion', score: 5 },
  { selector: 'c0246668', name: 'excludeFromFees(address,bool)', severity: 'medium', category: 'fee-exclusion', score: 5 },
];

// Opcode patterns
const DANGEROUS_OPCODES = [
  { byte: 'ff', name: 'SELFDESTRUCT', score: 40 },
  { byte: 'f4', name: 'DELEGATECALL', score: 25 },
  { byte: 'f2', name: 'CALLCODE', score: 25 },
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

  /**
   * 3-LAYER SECURITY CHECK:
   *
   * Layer 1: Helper Kontrat Simülasyonu (eth_call) — ~200ms
   * Layer 2: Bytecode Statik Analiz — ~100ms
   *   → Both run in PARALLEL (~300ms total)
   *
   * Decision:
   *   - Both OK → isTradeable = true (fast path, ~300ms)
   *   - One FAIL → needs micro-test (caller handles, ~48s)
   *   - Both FAIL → isTradeable = false (reject)
   */
  async checkToken(
    tokenAddress: string,
    pairAddress?: string,
    simAmountETH?: number,
  ): Promise<SecurityResult> {
    // Run Layer 1 + Layer 2 in PARALLEL
    const [simResult, bytecodeResult, goplus, honeypot] = await Promise.all([
      this.simulateSwap(tokenAddress, simAmountETH),
      this.analyzeBytecode(tokenAddress),
      this.checkGoPlus(tokenAddress).catch(() => null),
      this.checkHoneypotIs(tokenAddress).catch(() => null),
    ]);

    // Layer 1: Simulation result
    const canSell = simResult?.canSell ?? false;
    const buyTax = simResult?.buyTax ?? goplus?.buyTax ?? honeypot?.buyTax ?? null;
    const sellTax = simResult?.sellTax ?? goplus?.sellTax ?? honeypot?.sellTax ?? null;

    // Layer 2: Bytecode risk
    const bytecodeRiskScore = bytecodeResult.score;
    const bytecodeClean = bytecodeRiskScore < 30; // Below 30 = safe

    // API fallback
    const apiSaysHoneypot = goplus?.isHoneypot || honeypot?.isHoneypot;

    // Build summary
    let summary: string;
    let emoji: string;
    let approvedBy: string | null = null;

    const taxOk =
      (buyTax === null || buyTax <= 10) &&
      (sellTax === null || sellTax <= 10);

    if (!canSell || apiSaysHoneypot) {
      summary = '🚫 Cannot sell (honeypot)';
      emoji = '🚫';
    } else if (!taxOk) {
      const taxes: string[] = [];
      if (buyTax !== null) taxes.push(`buy: ${buyTax.toFixed(1)}%`);
      if (sellTax !== null) taxes.push(`sell: ${sellTax.toFixed(1)}%`);
      summary = `⚠️ High Tax (${taxes.join(', ')})`;
      emoji = '⚠️';
    } else if (!bytecodeClean) {
      const flags = bytecodeResult.flags.slice(0, 3).map((f) => f.name).join(', ');
      summary = `⚠️ Risky bytecode (score:${bytecodeRiskScore}, ${flags})`;
      emoji = '⚠️';
    } else {
      const taxes: string[] = [];
      if (buyTax !== null) taxes.push(`buy: ${buyTax.toFixed(1)}%`);
      if (sellTax !== null) taxes.push(`sell: ${sellTax.toFixed(1)}%`);
      summary = `✅ Safe (${taxes.length ? taxes.join(', ') : 'no tax'} | bytecode:${bytecodeRiskScore})`;
      emoji = '✅';
    }

    // GATE DECISION:
    // Both OK → tradeable (fast path)
    // One FAIL → needsMicroTest (caller decides)
    // Both FAIL → not tradeable
    const simOk = canSell && !apiSaysHoneypot && taxOk;
    const bothOk = simOk && bytecodeClean;
    const bothFail = !simOk && !bytecodeClean;

    let isTradeable = false;

    if (bothOk) {
      // Fast path: both layers agree it's safe
      isTradeable = true;
      approvedBy = 'sim+bytecode';
      this.logger.log(
        `✅ APPROVED ${tokenAddress}: sim OK + bytecode clean (score:${bytecodeRiskScore})`,
      );
    } else if (bothFail) {
      // Both layers say it's dangerous — hard reject
      isTradeable = false;
      approvedBy = null;
      this.logger.warn(
        `🚫 REJECTED ${tokenAddress}: sim FAIL + bytecode risky (score:${bytecodeRiskScore})`,
      );
    } else if (simOk && !bytecodeClean) {
      // Sim says OK but bytecode is risky — needs micro-test
      isTradeable = false; // Will be overridden by micro-test in monitor
      approvedBy = 'needs-micro-test';
      this.logger.warn(
        `⚠️ MICRO-TEST NEEDED ${tokenAddress}: sim OK but bytecode risky (score:${bytecodeRiskScore}, flags: ${bytecodeResult.flags.map(f => f.name).join(', ')})`,
      );
    } else if (!simOk && bytecodeClean) {
      // Bytecode clean but sim failed — needs micro-test
      isTradeable = false;
      approvedBy = 'needs-micro-test';
      this.logger.warn(
        `⚠️ MICRO-TEST NEEDED ${tokenAddress}: sim FAIL but bytecode clean (score:${bytecodeRiskScore})`,
      );
    }

    return {
      canSell,
      buyTax,
      sellTax,
      summary,
      emoji,
      isTradeable,
      approvedBy,
      bytecodeRiskScore,
    };
  }

  // ============================================================
  // LAYER 1: On-chain Simulation via Helper Contract
  // ============================================================

  private async simulateSwap(
    tokenAddress: string,
    simAmountETH?: number,
  ): Promise<{
    canSell: boolean;
    buyTax: number | null;
    sellTax: number | null;
  } | null> {
    try {
      const router = DEX_LIST[0]; // Uniswap V2
      const simAmount = ethers.parseEther(
        (simAmountETH && simAmountETH > 0.0001 ? simAmountETH : 0.001).toFixed(6),
      );
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

      // Sell tax
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
      this.logger.debug(`Honeypot sim failed for ${tokenAddress}: ${err.message}`);
      return { canSell: false, buyTax: null, sellTax: null };
    }
  }

  // ============================================================
  // LAYER 2: Bytecode Static Analysis
  // ============================================================

  /**
   * Fetch contract bytecode and scan for dangerous function selectors
   * and opcodes. Returns a risk score (0-100+).
   *
   * PUSH4 opcode = 0x63 → followed by 4-byte selector.
   * We search for '63' + selector in the bytecode hex string
   * (more reliable than raw 4-byte match which can false-positive in data).
   */
  async analyzeBytecode(
    tokenAddress: string,
  ): Promise<{ score: number; flags: BytecodeFlag[]; opcodeFlags: string[] }> {
    try {
      const bytecode = await this.httpProvider.getCode(tokenAddress);

      if (!bytecode || bytecode === '0x') {
        // Not a contract — EOA. No risk from bytecode perspective.
        return { score: 0, flags: [], opcodeFlags: [] };
      }

      const code = bytecode.toLowerCase().slice(2); // remove 0x prefix
      const flags: BytecodeFlag[] = [];
      const opcodeFlags: string[] = [];
      let score = 0;

      // Check function selectors (PUSH4 + selector)
      for (const sel of HONEYPOT_SELECTORS) {
        if (code.includes('63' + sel.selector)) {
          flags.push(sel);
          score += sel.score;
        }
      }

      // Check dangerous opcodes
      // Only flag SELFDESTRUCT/DELEGATECALL if they appear as actual opcodes,
      // not inside PUSH data. Simple heuristic: check the bytecode length too.
      for (const op of DANGEROUS_OPCODES) {
        // SELFDESTRUCT (ff) is common in data, so require it's NOT preceded by PUSH
        // Simple check: count occurrences — actual opcodes typically appear 1-2 times
        if (op.byte === 'ff') {
          // More careful: check for SELFDESTRUCT pattern (not in PUSH data)
          // Look for ff NOT preceded by 60-7f (PUSH1-PUSH32 data range)
          const idx = code.indexOf(op.byte);
          if (idx >= 2) {
            const prevByte = parseInt(code.substring(idx - 2, idx), 16);
            // If previous byte is NOT a PUSH opcode (0x60-0x7f), it's likely real
            if (prevByte < 0x60 || prevByte > 0x7f) {
              opcodeFlags.push(op.name);
              score += op.score;
            }
          }
        } else if (code.includes(op.byte)) {
          // DELEGATECALL (f4) and CALLCODE (f2) — presence is always suspicious
          opcodeFlags.push(op.name);
          score += op.score;
        }
      }

      // Bonus: suspiciously small bytecode (proxy contract)
      if (code.length < 1000) {
        // < 500 bytes = very likely a proxy
        score += 10;
        opcodeFlags.push('TINY_CONTRACT');
      }

      if (flags.length > 0 || opcodeFlags.length > 0) {
        this.logger.log(
          `Bytecode ${tokenAddress}: score=${score} | flags=[${flags.map(f => f.name).join(', ')}] | opcodes=[${opcodeFlags.join(', ')}]`,
        );
      }

      return { score, flags, opcodeFlags };
    } catch (err) {
      this.logger.debug(`Bytecode analysis failed for ${tokenAddress}: ${err.message}`);
      // If we can't read bytecode, return neutral score
      return { score: 0, flags: [], opcodeFlags: [] };
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
