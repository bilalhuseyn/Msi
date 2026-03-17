import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module';
import { Logger } from '@nestjs/common';

async function bootstrap() {
  const logger = new Logger('LiquidityMonitor');
  const app = await NestFactory.createApplicationContext(AppModule);

  logger.log('Liquidity Monitor is running');

  // Graceful shutdown
  const shutdown = async () => {
    logger.log('Shutting down...');
    await app.close();
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

bootstrap();
