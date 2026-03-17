import { Module } from '@nestjs/common';
import { TradingService } from './trading.service';
import { TelegramModule } from '../telegram/telegram.module';

@Module({
  imports: [TelegramModule],
  providers: [TradingService],
  exports: [TradingService],
})
export class TradingModule {}
