import { Module } from '@nestjs/common';
import { MonitorService } from './monitor.service';
import { SecurityModule } from '../security/security.module';
import { TelegramModule } from '../telegram/telegram.module';
import { TradingModule } from '../trading/trading.module';
import { AnalyticsModule } from '../analytics/analytics.module';
import { WatchdogModule } from '../watchdog/watchdog.module';

@Module({
  imports: [SecurityModule, TelegramModule, TradingModule, AnalyticsModule, WatchdogModule],
  providers: [MonitorService],
})
export class MonitorModule {}
