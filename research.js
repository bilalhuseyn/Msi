const { ethers } = require("ethers");
const p = new ethers.JsonRpcProvider("https://eth-mainnet.g.alchemy.com/v2/AF_M6_8PvYX3QbuzZIViP");

async function main() {
  console.log("=== ETHEREUM TRADING BOT - FEASIBILITY RESEARCH ===\n");

  const ethPrice = 1900;

  // 1. Gas
  console.log("=== 1. GAS FEE ANALIZI ===\n");
  const feeData = await p.getFeeData();
  const gasPrice = feeData.gasPrice;
  console.log("Base Fee:     " + ethers.formatUnits(gasPrice, "gwei") + " gwei");
  console.log("Max Fee:      " + ethers.formatUnits(feeData.maxFeePerGas, "gwei") + " gwei");
  console.log("Priority Fee: " + ethers.formatUnits(feeData.maxPriorityFeePerGas, "gwei") + " gwei");

  const ops = [
    { name: "approve()", gas: 46000 },
    { name: "swapExactETHForTokens (BUY)", gas: 150000 },
    { name: "swapExactTokensForETH (SELL x1)", gas: 170000 },
    { name: "Full round-trip (approve+buy+sell)", gas: 366000 },
    { name: "5-lot sell (approve+buy+5x sell)", gas: 1046000 },
    { name: "10-lot sell (approve+buy+10x sell)", gas: 1896000 },
  ];

  console.log("\nGas Costs at Current Prices:");
  for (const op of ops) {
    const costWei = BigInt(op.gas) * gasPrice;
    const costETH = parseFloat(ethers.formatEther(costWei));
    console.log("  " + op.name.padEnd(45) + " " + op.gas.toLocaleString().padStart(10) + " gas | " + costETH.toFixed(5) + " ETH | $" + (costETH * ethPrice).toFixed(2));
  }

  console.log("\nRound-trip cost at different gas levels (366k gas):");
  for (const gp of [5, 10, 20, 50, 100]) {
    const cost = 366000 * gp / 1e9;
    console.log("  " + (gp + " gwei").padEnd(10) + " -> " + cost.toFixed(5) + " ETH ($" + (cost * ethPrice).toFixed(2) + ")");
  }

  // 2. Timing
  console.log("\n=== 2. HIZ & TIMING ===\n");
  const bn = await p.getBlockNumber();
  const blocks = [];
  for (let i = 0; i < 10; i++) blocks.push(await p.getBlock(bn - i));
  const times = [];
  for (let i = 0; i < 9; i++) times.push(blocks[i].timestamp - blocks[i+1].timestamp);
  console.log("Block: " + bn + " | Avg block time: " + (times.reduce((a,b)=>a+b)/9).toFixed(1) + "s");
  console.log("\nBuy latency:  ~15s (detect + sim + 1 block)");
  console.log("Sell (5 lot): ~150s (lot1-2 instant + lot3-5 30s gap)");

  // 3. Position sizing
  console.log("\n=== 3. POZISYON vs SLIPPAGE ===\n");
  const balances = [0.05, 0.1, 0.25, 0.5, 1.0];
  const pools = [1, 2, 5, 10];

  let header = "Position".padEnd(12);
  pools.forEach(pl => header += (pl + " ETH").padStart(10));
  console.log("Slippage impact (buy only):");
  console.log(header);

  for (const b of balances) {
    let line = ("$" + (b*ethPrice).toFixed(0)).padEnd(12);
    for (const pl of pools) {
      const impact = (b / (pl + b)) * 100;
      line += (impact.toFixed(1) + "%").padStart(10);
    }
    console.log(line);
  }

  // 4. Break-even
  console.log("\n=== 4. BREAK-EVEN ANALIZI ===\n");
  const gasCost5lot = parseFloat(ethers.formatEther(BigInt(1046000) * gasPrice));
  const gasCostSimple = parseFloat(ethers.formatEther(BigInt(366000) * gasPrice));
  console.log("Fixed overhead per trade:");
  console.log("  DEX swap fee:   0.3% x2 = 0.6%");
  console.log("  Buy tax (avg):  ~2%");
  console.log("  Sell tax (avg): ~2%");
  console.log("  Slippage (est): ~2%");
  console.log("  --------------------------");
  console.log("  Subtotal:       ~6.6%");
  console.log("\nGas: simple=" + gasCostSimple.toFixed(5) + " ETH ($" + (gasCostSimple*ethPrice).toFixed(2) + ") | 5-lot=" + gasCost5lot.toFixed(5) + " ETH ($" + (gasCost5lot*ethPrice).toFixed(2) + ")\n");

  console.log("Break-even by position size (5-lot strategy):");
  for (const b of balances) {
    const gasPct = (gasCost5lot / b) * 100;
    const total = 6.6 + gasPct;
    console.log("  $" + (b*ethPrice).toFixed(0).padEnd(6) + " -> gas " + gasPct.toFixed(1) + "% + fees 6.6% = need +" + total.toFixed(1) + "% to profit");
  }

  // 5. Recommended strategy
  console.log("\n=== 5. ONERILEN STRATEJI ===\n");
  console.log("WALLET:");
  console.log("  - Yeni hot wallet (private key .env'de)");
  console.log("  - ASLA ana cuzdan private key'i kullanma");
  console.log("  - Sadece trading bakiyesi tut");
  console.log("");
  console.log("MEV KORUMASI:");
  console.log("  - Flashbots Protect RPC: https://rpc.flashbots.net");
  console.log("  - UCRETSIZ, tx mempool'a dusmez");
  console.log("  - Sandwich attack IMKANSIZ olur");
  console.log("  - Alternatif: https://rpc.mevblocker.io");
  console.log("");
  console.log("BUY:");
  console.log("  1. Liq tespit + security OK + gas < 50 gwei");
  console.log("  2. Position = min(balance * 50%, pool_eth * 3%)");
  console.log("  3. Slippage max 5%");
  console.log("  4. Flashbots ile gonder");
  console.log("");
  console.log("SELL (Hybrid Lot):");
  console.log("  1. 10dk bekle");
  console.log("  2. approve(router, MAX_UINT)");
  console.log("  3. Lot size = pool_eth * 3%");
  console.log("  4. Lot 1-2: aninda (anaparayi kurtar)");
  console.log("  5. Lot 3+: 30s arayla (arb recovery)");
  console.log("  6. STOP-LOSS: -30% ise instant dump");
  console.log("  7. TAKE-PROFIT: +500% ise 50% hemen sat");
  console.log("");
  console.log("RISK:");
  console.log("  - Max 1 aktif trade");
  console.log("  - Gunluk max kayip: %30");
  console.log("  - Trade basi: bakiyenin max %50");
  console.log("  - Gas > 50 gwei -> dur");
  console.log("  - 3 ust uste kayip -> 1 saat mola");
}

main().catch(console.error);
