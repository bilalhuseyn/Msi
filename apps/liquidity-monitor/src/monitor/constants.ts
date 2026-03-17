// ============================================================
// Known DEX Router addresses on Ethereum Mainnet
// ============================================================

export const WETH_ADDRESS = '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2';

export interface DexInfo {
  name: string;
  router: string;
  factory: string;
}

export const DEX_LIST: DexInfo[] = [
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
];

// All known router addresses (lowercased for comparison)
export const ROUTER_ADDRESSES = new Set(
  DEX_LIST.map((d) => d.router.toLowerCase()),
);

// All known factory addresses (lowercased for comparison)
export const FACTORY_ADDRESSES = new Set(
  DEX_LIST.map((d) => d.factory.toLowerCase()),
);

// Map from router address (lowercase) to DEX info
export const ROUTER_TO_DEX = new Map<string, DexInfo>(
  DEX_LIST.map((d) => [d.router.toLowerCase(), d]),
);

// Map from factory address (lowercase) to DEX info
export const FACTORY_TO_DEX = new Map<string, DexInfo>(
  DEX_LIST.map((d) => [d.factory.toLowerCase(), d]),
);

// ============================================================
// Function selectors (first 4 bytes of keccak256)
// ============================================================

// addLiquidityETH(address,uint256,uint256,uint256,address,uint256)
export const ADD_LIQUIDITY_ETH_SELECTOR = '0xf305d719';

// ============================================================
// Event signatures
// ============================================================

// PairCreated(address indexed token0, address indexed token1, address pair, uint256)
export const PAIR_CREATED_TOPIC =
  '0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9';

// ============================================================
// Minimal ABIs
// ============================================================

export const ROUTER_ABI_FRAGMENT = [
  'function addLiquidityETH(address token, uint256 amountTokenDesired, uint256 amountTokenMin, uint256 amountETHMin, address to, uint256 deadline) payable returns (uint256 amountToken, uint256 amountETH, uint256 liquidity)',
];

export const FACTORY_ABI_FRAGMENT = [
  'event PairCreated(address indexed token0, address indexed token1, address pair, uint256)',
  'function getPair(address tokenA, address tokenB) view returns (address pair)',
];

export const ERC20_ABI_FRAGMENT = [
  'function name() view returns (string)',
  'function symbol() view returns (string)',
  'function decimals() view returns (uint8)',
  'function totalSupply() view returns (uint256)',
];

export const PAIR_ABI_FRAGMENT = [
  'function getReserves() view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)',
  'function token0() view returns (address)',
  'function token1() view returns (address)',
];
