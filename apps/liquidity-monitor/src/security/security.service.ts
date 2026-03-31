import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { ethers } from 'ethers';
import { ChainConfig, CHAIN_CONFIGS } from '../config/chains';

export interface SecurityResult {
  /** Can the token actually be sold on-chain? */
  canSell: boolean;
  buyTax: number | null;
  sellTax: number | null;
  summary: string;
  emoji: string;
  /**
   * Gate decision:
   *  'approved'       → fast gate passed, proceed to micro-test
   *  'needs-micro-test' → one layer failed, still worth micro-testing
   *  'rejected'       → both layers failed, do not trade
   */
  gate: 'approved' | 'needs-micro-test' | 'rejected';
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
  severity: 'critical' | 'high' | 'medium' | 'positive';
  category: string;
  score: number; // positive = risky, negative = safer
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

  // POSITIVE: Ownership renounced — risk-REDUCING signal
  { selector: '715018a6', name: 'renounceOwnership()', severity: 'positive', category: 'ownership-renounced', score: -15 },
];

// Opcode patterns
const DANGEROUS_OPCODES = [
  { byte: 'ff', name: 'SELFDESTRUCT', score: 40 },
  { byte: 'f4', name: 'DELEGATECALL', score: 25 },
  { byte: 'f2', name: 'CALLCODE', score: 25 },
  { byte: '32', name: 'ORIGIN', score: 15 },  // tx.origin usage — potential anti-DEX sell
];

@Injectable()
export class SecurityService {
  private readonly logger = new Logger(SecurityService.name);
  /** Default ETH provider (backward compat) */
  private defaultProvider: ethers.JsonRpcProvider;
  /** Known scam deployer addresses */
  private readonly scamDeployers = new Set<string>();
  /** Tokens that failed to sell (honeypots discovered post-buy) */
  private readonly sellFailureCount = new Map<string, number>();
  private readonly blacklistedTokens = new Set<string>();

  constructor(private readonly config: ConfigService) {}

  onModuleInit() {
    const httpUrl = this.config.get<string>('alchemy.httpUrl');
    const wssUrl = this.config.get<string>('alchemy.wssUrl');
    const url = httpUrl || (wssUrl ? wssUrl.replace('wss://', 'https://') : '');
    this.defaultProvider = new ethers.JsonRpcProvider(url);
  }

  /**
   * 2-LAYER FAST GATE:
   *
   * Layer 1: Helper Contract Simulation (eth_call) — ~200ms
   * Layer 2: Bytecode Static Analysis — ~100ms
   *   → Both run in PARALLEL (~300ms total)
   *
   * Decision:
   *   - Both OK → gate = 'approved' → proceed to micro-test
   *   - One FAIL → gate = 'needs-micro-test' → still worth testing
   *   - Both FAIL → gate = 'rejected' → do not trade
   *
   * NO external APIs in the fast gate. Honeypot.is is only used
   * as a secondary check when our sim is unavailable.
   */
  async checkToken(
    tokenAddress: string,
    pairAddress?: string,
    simAmountETH?: number,
    chainConfig?: ChainConfig,
    provider?: ethers.JsonRpcProvider,
  ): Promise<SecurityResult> {
    const chain = chainConfig || CHAIN_CONFIGS.eth;
    const prov = provider || this.defaultProvider;

    // Run Layer 1 + Layer 2 + Deployer Check in PARALLEL
    const [simResult, bytecodeResult, deployerSafe] = await Promise.all([
      this.simulateSwap(tokenAddress, simAmountETH, chain, prov),
      this.analyzeBytecode(tokenAddress, prov),
      this.checkDeployerHistory(tokenAddress, prov),
    ]);

    // Layer 1: Simulation result
    const simRan = simResult !== null && simResult.canSell !== undefined;
    const simCanSell = simResult?.canSell ?? false;
    const simUnavailable = !simRan || (simResult?.buyTax === null && simResult?.sellTax === null && !simCanSell);

    const buyTax = simResult?.buyTax ?? null;
    const sellTax = simResult?.sellTax ?? null;

    // Layer 2: Bytecode risk
    const bytecodeRiskScore = bytecodeResult.score;
    const bytecodeClean = bytecodeRiskScore < 20;

    // Tax check
    const taxOk =
      (buyTax === null || buyTax <= 10) &&
      (sellTax === null || sellTax <= 10);

    // Deployer blacklisted?
    if (!deployerSafe) {
      this.logger.warn(`REJECTED ${tokenAddress}: deployer is a known scammer`);
      return {
        canSell: false,
        buyTax,
        sellTax,
        summary: '🚫 Known scam deployer',
        emoji: '🚫',
        gate: 'rejected',
        bytecodeRiskScore,
      };
    }

    // Determine canSell from our own simulation only
    let canSell: boolean;
    if (simCanSell) {
      canSell = true;
    } else if (simUnavailable) {
      // Sim couldn't run (RPC limitation) — don't assume safe or unsafe
      canSell = false; // will go to micro-test
    } else {
      canSell = false; // Sim explicitly failed
    }

    // Build summary
    let summary: string;
    let emoji: string;

    if (!canSell && !simUnavailable) {
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
      summary = `⚠️ Risky bytecode (score:${bytecodeRiskScore}, ${flags || bytecodeResult.opcodeFlags.join(', ')})`;
      emoji = '⚠️';
    } else {
      const taxes: string[] = [];
      if (buyTax !== null) taxes.push(`buy: ${buyTax.toFixed(1)}%`);
      if (sellTax !== null) taxes.push(`sell: ${sellTax.toFixed(1)}%`);
      summary = `✅ Fast gate OK (${taxes.length ? taxes.join(', ') : 'no tax'} | bytecode:${bytecodeRiskScore})`;
      emoji = '✅';
    }

    // GATE DECISION:
    // sim OK + bytecode clean → approved (proceed to micro-test as proof)
    // one fail or sim unavailable → needs-micro-test
    // sim explicitly failed + bytecode risky → rejected
    const simOk = canSell && taxOk;
    let gate: 'approved' | 'needs-micro-test' | 'rejected';

    if (simOk && bytecodeClean) {
      gate = 'approved';
      this.logger.log(
        `✅ FAST GATE OK ${tokenAddress}: sim safe + bytecode clean (score:${bytecodeRiskScore}) → micro-test next`,
      );
    } else if (!canSell && !simUnavailable && !bytecodeClean) {
      // Both explicitly failed — hard reject
      gate = 'rejected';
      this.logger.warn(
        `🚫 REJECTED ${tokenAddress}: sim FAIL + bytecode risky (score:${bytecodeRiskScore})`,
      );
    } else {
      // One failed or sim unavailable — let micro-test decide
      gate = 'needs-micro-test';
      this.logger.warn(
        `⚠️ MICRO-TEST NEEDED ${tokenAddress}: simOk=${simOk} bytecodeClean=${bytecodeClean} simUnavailable=${simUnavailable} (score:${bytecodeRiskScore})`,
      );
    }

    return {
      canSell,
      buyTax,
      sellTax,
      summary,
      emoji,
      gate,
      bytecodeRiskScore,
    };
  }

  // ============================================================
  // Deployer History Check
  // ============================================================

  /**
   * Check if the token's deployer has previously deployed known scam tokens.
   * Returns false if deployer is blacklisted.
   */
  private async checkDeployerHistory(
    tokenAddress: string,
    provider: ethers.JsonRpcProvider,
  ): Promise<boolean> {
    try {
      // Get creation TX to find deployer
      // We use getCode to verify it's a contract, then check nonce pattern
      // For a comprehensive check we'd need the creation TX, but that requires
      // archive node or explorer API. For now, we maintain a local blacklist
      // that gets populated as we discover scam tokens.
      // Future: integrate Etherscan/BscScan API for deployer lookup
      return !this.scamDeployers.has(tokenAddress.toLowerCase());
    } catch {
      return true; // Don't block on error
    }
  }

  /**
   * Add a deployer address to the scam blacklist.
   * Called when a trade results in a honeypot (sell fails).
   */
  addScamDeployer(deployerAddress: string) {
    this.scamDeployers.add(deployerAddress.toLowerCase());
    this.logger.warn(`Added scam deployer to blacklist: ${deployerAddress}`);
  }

  /**
   * Add a token address to scam list (when we discover a honeypot after buying).
   * In the future this will also resolve and blacklist the deployer.
   */
  addScamToken(tokenAddress: string) {
    this.blacklistedTokens.add(tokenAddress.toLowerCase());
    this.logger.warn(`Scam token blacklisted: ${tokenAddress}`);
  }

  /** Record a sell failure. After 2 failures, auto-blacklist the token. */
  recordSellFailure(tokenAddress: string): boolean {
    const addr = tokenAddress.toLowerCase();
    const count = (this.sellFailureCount.get(addr) || 0) + 1;
    this.sellFailureCount.set(addr, count);
    if (count >= 2) {
      this.blacklistedTokens.add(addr);
      this.logger.warn(`Auto-blacklisted honeypot after ${count} sell failures: ${addr}`);
      return true;
    }
    return false;
  }

  /** Check if a token is blacklisted (known honeypot). */
  isBlacklisted(tokenAddress: string): boolean {
    return this.blacklistedTokens.has(tokenAddress.toLowerCase());
  }

  // ============================================================
  // LAYER 1: On-chain Simulation via Helper Contract
  // ============================================================

  private async simulateSwap(
    tokenAddress: string,
    simAmountETH?: number,
    chain?: ChainConfig,
    provider?: ethers.JsonRpcProvider,
  ): Promise<{
    canSell: boolean;
    buyTax: number | null;
    sellTax: number | null;
  } | null> {
    try {
      const c = chain || CHAIN_CONFIGS.eth;
      const prov = provider || this.defaultProvider;
      const router = c.dexList[0];
      const wrappedNative = c.wrappedNative;
      const simAmount = ethers.parseEther(
        (simAmountETH && simAmountETH > 0.0001 ? simAmountETH : 0.001).toFixed(6),
      );
      const path = [wrappedNative, tokenAddress];

      // Step 1: Get expected token output (pure math, no transfer)
      const routerContract = new ethers.Contract(
        router.router,
        ROUTER_SIM_ABI,
        prov,
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
        wrappedNative,
      ]);

      const result = await prov.send('eth_call', [
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
          [tokenAddress, wrappedNative],
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
  // GOPLUS API: Async honeypot cross-check (free, no API key)
  // ============================================================

  /**
   * GoPlus Security API — free async honeypot check.
   * Returns risk flags: is_honeypot, sell_tax, buy_tax, is_blacklisted, etc.
   * Used as post-buy async guard during hold period.
   *
   * Chain IDs: eth=1, bsc=56, base=8453
   */
  async checkGoPlus(
    tokenAddress: string,
    chainId: string,
  ): Promise<{
    isHoneypot: boolean;
    sellTax: number | null;
    buyTax: number | null;
    isBlacklisted: boolean;
    cannotSellAll: boolean;
    hasProxy: boolean;
    ownerCanChangeBalance: boolean;
    summary: string;
  }> {
    const chainMap: Record<string, string> = { eth: '1', bsc: '56', base: '8453' };
    const gpChainId = chainMap[chainId] || '1';
    const url = `https://api.gopluslabs.com/api/v1/token_security/${gpChainId}?contract_addresses=${tokenAddress}`;

    const defaultResult = {
      isHoneypot: false, sellTax: null as number | null, buyTax: null as number | null,
      isBlacklisted: false, cannotSellAll: false, hasProxy: false,
      ownerCanChangeBalance: false, summary: 'GoPlus: unavailable',
    };

    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 8000);

      const resp = await fetch(url, { signal: controller.signal });
      clearTimeout(timeout);

      if (!resp.ok) return defaultResult;

      const data = await resp.json();
      const tokenData = data?.result?.[tokenAddress.toLowerCase()];
      if (!tokenData) return { ...defaultResult, summary: 'GoPlus: token not found' };

      const isHoneypot = tokenData.is_honeypot === '1';
      const sellTax = tokenData.sell_tax ? parseFloat(tokenData.sell_tax) * 100 : null;
      const buyTax = tokenData.buy_tax ? parseFloat(tokenData.buy_tax) * 100 : null;
      const isBlacklisted = tokenData.is_blacklisted === '1';
      const cannotSellAll = tokenData.cannot_sell_all === '1';
      const hasProxy = tokenData.is_proxy === '1';
      const ownerCanChangeBalance = tokenData.owner_change_balance === '1';

      const flags: string[] = [];
      if (isHoneypot) flags.push('HONEYPOT');
      if (isBlacklisted) flags.push('BLACKLIST');
      if (cannotSellAll) flags.push('CANT_SELL_ALL');
      if (hasProxy) flags.push('PROXY');
      if (ownerCanChangeBalance) flags.push('OWNER_CHANGE_BAL');
      if (sellTax !== null && sellTax > 10) flags.push(`sellTax:${sellTax.toFixed(0)}%`);
      if (buyTax !== null && buyTax > 10) flags.push(`buyTax:${buyTax.toFixed(0)}%`);

      const summary = flags.length > 0
        ? `GoPlus: ⚠️ ${flags.join(', ')}`
        : `GoPlus: ✅ clean (sellTax:${sellTax?.toFixed(1) ?? '?'}% buyTax:${buyTax?.toFixed(1) ?? '?'}%)`;

      this.logger.log(`GoPlus ${tokenAddress}: ${summary}`);

      return { isHoneypot, sellTax, buyTax, isBlacklisted, cannotSellAll, hasProxy, ownerCanChangeBalance, summary };
    } catch (err) {
      this.logger.debug(`GoPlus API error for ${tokenAddress}: ${err.message}`);
      return defaultResult;
    }
  }

  // ============================================================
  // LAYER 2: Bytecode Static Analysis
  // ============================================================

  /**
   * Fetch contract bytecode and scan for dangerous function selectors
   * and opcodes. Returns a risk score (0-100+).
   *
   * PUSH4 opcode = 0x63 -> followed by 4-byte selector.
   * We search for '63' + selector in the bytecode hex string.
   *
   * Also checks:
   *  - ORIGIN opcode (0x32) for tx.origin anti-DEX patterns
   *  - renounceOwnership() as a risk-REDUCING signal (-15 score)
   */
  async analyzeBytecode(
    tokenAddress: string,
    provider?: ethers.JsonRpcProvider,
  ): Promise<{ score: number; flags: BytecodeFlag[]; opcodeFlags: string[] }> {
    try {
      const prov = provider || this.defaultProvider;
      const bytecode = await prov.getCode(tokenAddress);

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

      // Check dangerous opcodes — only flag as actual opcodes, not PUSH data
      for (const op of DANGEROUS_OPCODES) {
        let found = false;
        let i = 0;
        while (i < code.length - 1) {
          const byteHex = code.substring(i, i + 2);
          const byteVal = parseInt(byteHex, 16);
          // PUSH1..PUSH32 (0x60..0x7f) — skip the pushed data
          if (byteVal >= 0x60 && byteVal <= 0x7f) {
            const pushBytes = byteVal - 0x5f;
            i += 2 + pushBytes * 2;
            continue;
          }
          if (byteHex === op.byte) {
            found = true;
            break;
          }
          i += 2;
        }
        if (found) {
          opcodeFlags.push(op.name);
          score += op.score;
        }
      }

      // Bonus: suspiciously small bytecode (proxy contract)
      if (code.length < 1000) {
        score += 10;
        opcodeFlags.push('TINY_CONTRACT');
      }

      // Ensure score doesn't go below 0 (from renounceOwnership reduction)
      score = Math.max(score, 0);

      if (flags.length > 0 || opcodeFlags.length > 0) {
        this.logger.log(
          `Bytecode ${tokenAddress}: score=${score} | flags=[${flags.map(f => f.name).join(', ')}] | opcodes=[${opcodeFlags.join(', ')}]`,
        );
      }

      return { score, flags, opcodeFlags };
    } catch (err) {
      this.logger.debug(`Bytecode analysis failed for ${tokenAddress}: ${err.message}`);
      return { score: 0, flags: [], opcodeFlags: [] };
    }
  }
}
