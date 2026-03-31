const { ethers } = require('ethers');

const provider = new ethers.JsonRpcProvider('https://eth-mainnet.g.alchemy.com/v2/AF_M6_8PvYX3QbuzZIViP');

const UNISWAP_V2_ROUTER = '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D'.toLowerCase();
const WETH = '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2';
const ADD_LIQ_ETH = '0xf305d719';
const UNI_V2_FACTORY = '0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f';

const FACTORY_ABI = [
  'function getPair(address tokenA, address tokenB) view returns (address pair)',
];
const PAIR_ABI = [
  'function getReserves() view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)',
  'function token0() view returns (address)',
  'function token1() view returns (address)',
];
const ROUTER_ABI = [
  'function addLiquidityETH(address token, uint256 amountTokenDesired, uint256 amountTokenMin, uint256 amountETHMin, address to, uint256 deadline) payable returns (uint256 amountToken, uint256 amountETH, uint256 liquidity)',
];

const factory = new ethers.Contract(UNI_V2_FACTORY, FACTORY_ABI, provider);
const iface = new ethers.Interface(ROUTER_ABI);

const BLOCKS_10_MIN = 50;

async function getTokenPriceInETH(pairAddress, tokenAddress, blockNumber) {
  try {
    const pair = new ethers.Contract(pairAddress, PAIR_ABI, provider);
    const [reserves, token0] = await Promise.all([
      pair.getReserves({ blockTag: blockNumber }),
      pair.token0({ blockTag: blockNumber }),
    ]);

    const reserve0 = parseFloat(ethers.formatEther(reserves[0]));
    const reserve1 = parseFloat(ethers.formatEther(reserves[1]));

    if (reserve0 === 0 || reserve1 === 0) return null;

    if (token0.toLowerCase() === WETH.toLowerCase()) {
      return reserve0 / reserve1;
    } else {
      return reserve1 / reserve0;
    }
  } catch (e) {
    return null;
  }
}

async function main() {
  const latestBlock = await provider.getBlockNumber();
  const DAYS = 7;
  const totalBlocks = DAYS * 7200;
  const startBlock = latestBlock - totalBlocks;
  const SAMPLE_STEP = 5;

  console.log('=== BACKTEST: Buy on addLiquidityETH, sell after 10 min ===');
  console.log('Period: last ' + DAYS + ' days');
  console.log('Blocks: ' + startBlock + ' to ' + latestBlock);
  console.log('Sample: every ' + SAMPLE_STEP + 'th block');
  console.log('Strategy: Buy token immediately, sell after 10 min (~50 blocks)');
  console.log('Starting capital: $100, 100% allocation, compound');
  console.log('Min ETH filter: >= 1 ETH');
  console.log('');

  const trades = [];
  let scanned = 0;

  for (let b = startBlock; b <= latestBlock - BLOCKS_10_MIN; b += SAMPLE_STEP) {
    try {
      const block = await provider.getBlock(b, true);
      if (!block || !block.prefetchedTransactions) continue;
      scanned++;

      if (scanned % 100 === 0) {
        console.log('  Progress: ' + scanned + ' blocks scanned, ' + trades.length + ' trades found...');
      }

      for (const tx of block.prefetchedTransactions) {
        if (!tx.to || !tx.data) continue;
        if (tx.to.toLowerCase() !== UNISWAP_V2_ROUTER) continue;
        if (!tx.data.startsWith(ADD_LIQ_ETH)) continue;

        const ethAmount = parseFloat(ethers.formatEther(tx.value));
        if (ethAmount < 1) continue;

        let tokenAddress;
        try {
          const decoded = iface.decodeFunctionData('addLiquidityETH', tx.data);
          tokenAddress = decoded[0];
        } catch { continue; }

        if (tokenAddress.toLowerCase() === WETH.toLowerCase()) continue;

        let pairAddress;
        try {
          pairAddress = await factory.getPair(tokenAddress, WETH, { blockTag: b });
          if (!pairAddress || pairAddress === ethers.ZeroAddress) continue;
        } catch { continue; }

        const buyBlock = b + 1;
        const sellBlock = b + BLOCKS_10_MIN;

        const buyPrice = await getTokenPriceInETH(pairAddress, tokenAddress, buyBlock);
        const sellPrice = await getTokenPriceInETH(pairAddress, tokenAddress, sellBlock);

        if (!buyPrice || !sellPrice || buyPrice === 0) continue;

        const returnPct = ((sellPrice - buyPrice) / buyPrice) * 100;

        trades.push({
          block: b,
          token: tokenAddress,
          ethAdded: ethAmount.toFixed(2),
          buyPrice,
          sellPrice,
          returnPct,
          txHash: tx.hash,
        });

        const emoji = returnPct >= 0 ? '+++' : '---';
        console.log(
          emoji +
          ' Block ' + b +
          ' | ' + ethAmount.toFixed(1) + ' ETH liq' +
          ' | Return: ' + returnPct.toFixed(2) + '%' +
          ' | Token: ' + tokenAddress.slice(0, 10) + '...'
        );
      }
    } catch (e) {
      // skip
    }
  }

  console.log('');
  console.log('=== RESULTS ===');
  console.log('Blocks scanned: ' + scanned);
  console.log('Qualifying trades found: ' + trades.length);

  if (trades.length === 0) {
    console.log('No qualifying trades found in sampled blocks.');
    return;
  }

  let balance = 100;
  let wins = 0;
  let losses = 0;
  let totalReturn = 0;

  console.log('');
  console.log('--- Trade Log (Compound) ---');
  for (const t of trades) {
    const oldBalance = balance;
    balance = balance * (1 + t.returnPct / 100);
    if (balance < oldBalance * 0.05) balance = oldBalance * 0.05;

    totalReturn += t.returnPct;
    if (t.returnPct >= 0) wins++;
    else losses++;

    console.log(
      '  $' + oldBalance.toFixed(2) + ' -> $' + balance.toFixed(2) +
      ' (' + (t.returnPct >= 0 ? '+' : '') + t.returnPct.toFixed(2) + '%)' +
      ' | ' + t.ethAdded + ' ETH liq | Block ' + t.block
    );
  }

  console.log('');
  console.log('--- FINAL SUMMARY ---');
  console.log('Total trades: ' + trades.length);
  console.log('Wins: ' + wins + ' | Losses: ' + losses);
  console.log('Win rate: ' + ((wins / trades.length) * 100).toFixed(1) + '%');
  console.log('Avg return per trade: ' + (totalReturn / trades.length).toFixed(2) + '%');
  console.log('Best trade: ' + Math.max(...trades.map(t => t.returnPct)).toFixed(2) + '%');
  console.log('Worst trade: ' + Math.min(...trades.map(t => t.returnPct)).toFixed(2) + '%');
  console.log('');
  console.log('Starting balance: $100.00');
  console.log('Final balance: $' + balance.toFixed(2));
  console.log('Total P&L: ' + (balance >= 100 ? '+' : '') + '$' + (balance - 100).toFixed(2));
  console.log('Total ROI: ' + (balance >= 100 ? '+' : '') + ((balance - 100)).toFixed(2) + '%');
  console.log('');
  console.log('NOTE: Sampled every ' + SAMPLE_STEP + 'th block. Estimated real trades: ~' + (trades.length * SAMPLE_STEP));
}

main().catch(console.error);
