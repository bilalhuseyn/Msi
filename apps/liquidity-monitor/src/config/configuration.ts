export default () => ({
  alchemy: {
    wssUrl: process.env.ALCHEMY_WSS_URL || '',
    httpUrl: process.env.ALCHEMY_HTTP_URL || '',
  },
  telegram: {
    botToken: process.env.TELEGRAM_BOT_TOKEN || '',
    chatId: process.env.TELEGRAM_CHAT_ID || '',
  },
  monitor: {
    // Minimum ETH for new tokens (pair age < 24h)
    minEthNewToken: parseFloat(process.env.MIN_ETH_NEW_TOKEN || '1'),
    // Minimum ETH for existing tokens
    minEthExistingToken: parseFloat(process.env.MIN_ETH_EXISTING_TOKEN || '5'),
    // Cooldown per token in ms (12 hours)
    cooldownMs: parseInt(process.env.COOLDOWN_MS || String(12 * 60 * 60 * 1000), 10),
    // Max pair age to be considered "new" (24 hours in seconds)
    newTokenMaxAge: parseInt(process.env.NEW_TOKEN_MAX_AGE || String(24 * 60 * 60), 10),
  },
});
