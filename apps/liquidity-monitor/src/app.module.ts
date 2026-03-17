import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import configuration from './config/configuration';
import { MonitorModule } from './monitor/monitor.module';

@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      load: [configuration],
      envFilePath: [
        'apps/liquidity-monitor/.env',
        '.env',
      ],
    }),
    MonitorModule,
  ],
})
export class AppModule {}
