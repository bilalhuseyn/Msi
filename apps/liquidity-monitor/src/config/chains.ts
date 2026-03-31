// ============================================================
// Multi-Chain Configuration Registry
// Supports: Ethereum, BSC, Base (all EVM-compatible)
// ============================================================

export type ChainId = 'eth' | 'bsc' | 'base';

export interface DexInfo {
  name: string;
  router: string;
  factory: string;
}

export interface ChainConfig {
  id: ChainId;
  name: string;
  chainIdNum: number;
  wrappedNative: string;        // WETH / WBNB address
  nativeSymbol: string;         // "ETH" / "BNB"
  nativeWrappedSymbol: string;  // "WETH" / "WBNB"
  blockTimeMs: number;
  explorerUrl: string;
  explorerName: string;
  dexScreenerSlug: string;
  dexToolsSlug: string;
  goPlusChainId: string;
  honeypotIsChainId: number;
  dexList: DexInfo[];
  minNativeNewToken: number;    // Minimum liquidity for new tokens (in native)
  liquidityWaitMs: number;      // Wait before trading to filter rug pulls (0 = no wait)
  rpcConfig: {
    wssUrlEnvKey: string;
    httpUrlEnvKey: string;
  };
  tradingConfig: {
    privateKeyEnvKey: string;
    maxGasGweiDefault: number;
    stopLossMonitorIntervalMs: number;
  };
}

// ============================================================
// Chain Definitions
// ============================================================

export const CHAIN_CONFIGS: Record<ChainId, ChainConfig> = {
  eth: {
    id: 'eth',
    name: 'Ethereum',
    chainIdNum: 1,
    wrappedNative: '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2',
    nativeSymbol: 'ETH',
    nativeWrappedSymbol: 'WETH',
    blockTimeMs: 12000,
    explorerUrl: 'https://etherscan.io',
    explorerName: 'Etherscan',
    dexScreenerSlug: 'ethereum',
    dexToolsSlug: 'ether',
    goPlusChainId: '1',
    honeypotIsChainId: 1,
    minNativeNewToken: 1, // 1 ETH (~$2,337)
    liquidityWaitMs: 0,  // ETH: no wait
    dexList: [
      {
        name: 'Uniswap V2',
        router: '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D',
        factory: '0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f',
      },
      {
        name: 'SushiSwap',
        router: '0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F',
        factory: '0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac',
      },
    ],
    rpcConfig: {
      wssUrlEnvKey: 'ALCHEMY_WSS_URL',
      httpUrlEnvKey: 'ALCHEMY_HTTP_URL',
    },
    tradingConfig: {
      privateKeyEnvKey: 'TRADING_PRIVATE_KEY',
      maxGasGweiDefault: 50,
      stopLossMonitorIntervalMs: 15000,
    },
  },

  bsc: {
    id: 'bsc',
    name: 'BSC',
    chainIdNum: 56,
    wrappedNative: '0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c',
    nativeSymbol: 'BNB',
    nativeWrappedSymbol: 'WBNB',
    blockTimeMs: 3000,
    explorerUrl: 'https://bscscan.com',
    explorerName: 'BscScan',
    dexScreenerSlug: 'bsc',
    dexToolsSlug: 'bnb',
    goPlusChainId: '56',
    honeypotIsChainId: 56,
    minNativeNewToken: 30, // 30 BNB (~$18,900) — filter low-liq rug pulls
    liquidityWaitMs: 3 * 60 * 1000, // BSC: 3 min wait — filter rug pulls
    dexList: [
      {
        name: 'PancakeSwap V2',
        router: '0x10ED43C718714eb63d5aA57B78B54704E256024E',
        factory: '0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73',
      },
    ],
    rpcConfig: {
      wssUrlEnvKey: 'BSC_WSS_URL',
      httpUrlEnvKey: 'BSC_HTTP_URL',
    },
    tradingConfig: {
      privateKeyEnvKey: 'BSC_PRIVATE_KEY',
      maxGasGweiDefault: 10,
      stopLossMonitorIntervalMs: 6000,
    },
  },

  base: {
    id: 'base',
    name: 'Base',
    chainIdNum: 8453,
    wrappedNative: '0x4200000000000000000000000000000000000006',
    nativeSymbol: 'ETH',
    nativeWrappedSymbol: 'WETH',
    blockTimeMs: 2000,
    explorerUrl: 'https://basescan.org',
    explorerName: 'BaseScan',
    dexScreenerSlug: 'base',
    dexToolsSlug: 'base',
    goPlusChainId: '8453',
    honeypotIsChainId: 8453,
    minNativeNewToken: 10, // 10 ETH (~$21,000) — filter noise, focus on quality
    liquidityWaitMs: 0,  // Base: no wait
    dexList: [
      {
        name: 'Uniswap V2',
        router: '0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24',
        factory: '0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6',
      },
    ],
    rpcConfig: {
      wssUrlEnvKey: 'BASE_WSS_URL',
      httpUrlEnvKey: 'BASE_HTTP_URL',
    },
    tradingConfig: {
      privateKeyEnvKey: 'BASE_PRIVATE_KEY',
      maxGasGweiDefault: 1,
      stopLossMonitorIntervalMs: 4000,
    },
  },
};

// ============================================================
// Helper: Build chain-specific lookup structures
// ============================================================

export interface ChainConstants {
  wrappedNative: string;
  dexList: DexInfo[];
  routerAddresses: Set<string>;
  routerToDex: Map<string, DexInfo>;
  factoryToDex: Map<string, DexInfo>;
}

export function getChainConstants(chainId: ChainId): ChainConstants {
  const chain = CHAIN_CONFIGS[chainId];
  return {
    wrappedNative: chain.wrappedNative,
    dexList: chain.dexList,
    routerAddresses: new Set(chain.dexList.map((d) => d.router.toLowerCase())),
    routerToDex: new Map(chain.dexList.map((d) => [d.router.toLowerCase(), d])),
    factoryToDex: new Map(chain.dexList.map((d) => [d.factory.toLowerCase(), d])),
  };
}

// ============================================================
// Chain context passed to services (provider + config)
// ============================================================

export interface ChainContext {
  chainConfig: ChainConfig;
  constants: ChainConstants;
}
