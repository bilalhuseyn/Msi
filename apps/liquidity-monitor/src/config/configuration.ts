export default () => ({
  // Which chains to enable (comma-separated: eth,bsc,base)
  enabledChains: (process.env.ENABLED_CHAINS || 'eth').split(',').map((s) => s.trim()),

  // === Per-chain RPC URLs ===
  // Ethereum (existing keys, backward compatible)
  alchemy: {
    wssUrl: process.env.ALCHEMY_WSS_URL || '',
    httpUrl: process.env.ALCHEMY_HTTP_URL || '',
  },
  // BSC
  bsc: {
    wssUrl: process.env.BSC_WSS_URL || '',
    httpUrl: process.env.BSC_HTTP_URL || '',
    privateKey: process.env.BSC_PRIVATE_KEY || '',
  },
  // Base
  base: {
    wssUrl: process.env.BASE_WSS_URL || '',
    httpUrl: process.env.BASE_HTTP_URL || '',
    privateKey: process.env.BASE_PRIVATE_KEY || '',
  },

  telegram: {
    botToken: process.env.TELEGRAM_BOT_TOKEN || '',
    chatId: process.env.TELEGRAM_CHAT_ID || '',
  },

  monitor: {
    // Cooldown per token in ms (12 hours)
    cooldownMs: parseInt(process.env.COOLDOWN_MS || String(12 * 60 * 60 * 1000), 10),
    // Max pair age to be considered "new" (24 hours in seconds)
    newTokenMaxAge: parseInt(process.env.NEW_TOKEN_MAX_AGE || String(24 * 60 * 60), 10),
  },

  trading: {
    privateKey: process.env.TRADING_PRIVATE_KEY || '',
    positionPct: parseFloat(process.env.TRADING_POSITION_PCT || '50'),
    maxPoolPct: parseFloat(process.env.TRADING_MAX_POOL_PCT || '3'),
    slippagePct: parseFloat(process.env.TRADING_SLIPPAGE_PCT || '5'),
    maxGasGwei: parseFloat(process.env.TRADING_MAX_GAS_GWEI || '50'),
    holdTimeMs: parseInt(process.env.TRADING_HOLD_TIME_MS || String(10 * 60 * 1000), 10),
    stopLossPct: parseFloat(process.env.TRADING_STOP_LOSS_PCT || '15'),
    maxDailyLossPct: parseFloat(process.env.TRADING_MAX_DAILY_LOSS_PCT || '30'),
    maxConsecutiveLosses: parseInt(process.env.TRADING_MAX_CONSECUTIVE_LOSSES || '3', 10),
  },
});
