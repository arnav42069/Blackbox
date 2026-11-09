import { defineConfig } from '@playwright/test';
import { resolve } from 'node:path';
export default defineConfig({
  testDir:'../tests/e2e',fullyParallel:false,workers:1,
  use:{baseURL:'http://127.0.0.1:8876',headless:true,launchOptions:{executablePath:process.env.BLACKBOX_CHROMIUM || '/usr/bin/chromium',args:['--no-sandbox']},trace:'retain-on-failure'},
  webServer:{command:`${resolve('../.venv/bin/python')} ${resolve('../tests/e2e/server.py')}`,url:'http://127.0.0.1:8876/api/health',reuseExistingServer:false,timeout:20000},reporter:'list',
});
