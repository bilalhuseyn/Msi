import { Module } from '@nestjs/common';
import { TradingService } from './trading.service';
import { TelegramModule } from '../telegram/telegram.module';
import { AnalyticsModule } from '../analytics/analytics.module';
import { SecurityModule } from '../security/security.module';
import { WatchdogModule } from '../watchdog/watchdog.module';

@Module({
  imports: [TelegramModule, AnalyticsModule, SecurityModule, WatchdogModule],
  providers: [TradingService],
  exports: [TradingService],
})
export class TradingModule {}
