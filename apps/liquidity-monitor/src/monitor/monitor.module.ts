import { Module } from '@nestjs/common';
import { MonitorService } from './monitor.service';
import { SecurityModule } from '../security/security.module';
import { TelegramModule } from '../telegram/telegram.module';
import { TradingModule } from '../trading/trading.module';

@Module({
  imports: [SecurityModule, TelegramModule, TradingModule],
  providers: [MonitorService],
})
export class MonitorModule {}
