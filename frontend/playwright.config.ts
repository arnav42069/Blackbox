import { defineConfig } from '@playwright/test';
import { resolve } from 'node:path';
const port=Number(process.env.BLACKBOX_E2E_PORT || 8876);
const report=process.env.BLACKBOX_QA_REPORT;
export default defineConfig({
  testDir:'../tests/e2e',fullyParallel:false,workers:1,
  outputDir:report ? resolve(report,'browser-artifacts') : '../test-results',
  use:{baseURL:`http://127.0.0.1:${port}`,headless:true,launchOptions:{executablePath:process.env.BLACKBOX_CHROMIUM || '/usr/bin/chromium'},
    // Traces/HAR contain pairing and bearer credentials. QA captures sanitized evidence instead.
    trace:report ? 'off' : 'retain-on-failure'},
  webServer:{command:`${process.env.BLACKBOX_QA_PYTHON || resolve('../.venv/bin/python')} ${resolve('../tests/e2e/server.py')}`,
    url:`http://127.0.0.1:${port}/api/health`,reuseExistingServer:false,timeout:20000,gracefulShutdown:{signal:'SIGTERM',timeout:5000}},reporter:'list',
});
