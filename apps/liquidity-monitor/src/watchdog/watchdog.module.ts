import { Module } from '@nestjs/common';
import { WatchdogService } from './watchdog.service';
import { TelegramModule } from '../telegram/telegram.module';
import { SecurityModule } from '../security/security.module';

@Module({
  imports: [TelegramModule, SecurityModule],
  providers: [WatchdogService],
  exports: [WatchdogService],
})
export class WatchdogModule {}
