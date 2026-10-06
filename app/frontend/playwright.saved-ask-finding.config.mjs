import {defineConfig} from '@playwright/test';
import {resolveChrome} from './src/ui/__tests__/task4-proof-helpers.js';

const chrome = resolveChrome();
if (!chrome) throw new Error('Chrome is required for the saved Ask to Finding fixture');

export default defineConfig({
  testDir: './tests/browser',
  testMatch: 'saved-ask-finding.pw.mjs',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60000,
  reporter: 'line',
  outputDir: '../../test-results/l4/saved-ask-finding-playwright',
  use: {
    baseURL: 'http://127.0.0.1:4199',
    headless: true,
    viewport: {width: 1280, height: 900},
    deviceScaleFactor: 1,
    locale: 'en-ZA',
    timezoneId: 'Africa/Johannesburg',
    launchOptions: {executablePath: chrome},
    trace: 'off',
    screenshot: 'off',
    video: 'off',
    serviceWorkers: 'block',
  },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 4199 --strictPort',
    url: 'http://127.0.0.1:4199',
    reuseExistingServer: false,
    timeout: 30000,
  },
});
