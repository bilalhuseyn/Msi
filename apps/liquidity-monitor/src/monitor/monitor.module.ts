import { Module } from '@nestjs/common';
import { MonitorService } from './monitor.service';
import { SecurityModule } from '../security/security.module';
import { TelegramModule } from '../telegram/telegram.module';

@Module({
  imports: [SecurityModule, TelegramModule],
  providers: [MonitorService],
})
export class MonitorModule {}
