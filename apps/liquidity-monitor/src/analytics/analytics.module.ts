import { Module } from '@nestjs/common';
import { TradeLoggerService } from './trade-logger.service';
import { PatternAnalyzerService } from './pattern-analyzer.service';
import { TelegramModule } from '../telegram/telegram.module';

@Module({
  imports: [TelegramModule],
  providers: [TradeLoggerService, PatternAnalyzerService],
  exports: [TradeLoggerService, PatternAnalyzerService],
})
export class AnalyticsModule {}
