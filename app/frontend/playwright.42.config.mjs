/* The BUILD.md 1.15 journey. Locally it starts f42-agent and f42-api on the
   fixtures from the worktree root and serves the built app from app/web/dist,
   so run `bun run build` first. With F42_BASE_URL set it starts nothing and
   runs against that URL with the passcode in F42_SMOKE_PASSCODE. */
import {defineConfig} from '@playwright/test';
import {fileURLToPath} from 'node:url';
import {resolveChrome} from './src/ui/__tests__/task4-proof-helpers.js';

const chrome = resolveChrome();
if (!chrome) throw new Error('Chrome is required for the 42 journey');

const root = fileURLToPath(new URL('../..', import.meta.url));
const remote = process.env.F42_BASE_URL || '';
export const LOCAL_PASSCODE = 'journey-passcode';

const agent = {
  command: 'py -3.13 -m uvicorn core.api.agent_app:app --host 127.0.0.1 --port 4181',
  cwd: root,
  url: 'http://127.0.0.1:4181/health',
  env: {F42_AGENT: 'fixture', F42_DATA: 'fixtures', F42_FIXTURE_DELAY: '0.3'},
  reuseExistingServer: false,
  timeout: 60000,
};

const api = {
  command: 'py -3.13 -m uvicorn core.api.app:app --host 127.0.0.1 --port 4180',
  cwd: root,
  url: 'http://127.0.0.1:4180/api/health',
  env: {F42_DATA: 'fixtures', AGENT_URL: 'http://127.0.0.1:4181', UI_PASSCODE: LOCAL_PASSCODE, WEB_DIST: 'app/web/dist'},
  reuseExistingServer: false,
  timeout: 60000,
};

export default defineConfig({
  testDir: './tests/browser',
  testMatch: '42-full-journey.pw.mjs',
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60000,
  reporter: [['line']],
  outputDir: './test-results/42-full-journey',
  use: {
    baseURL: remote || 'http://127.0.0.1:4180',
    headless: true,
    viewport: {width: 1280, height: 900},
    launchOptions: {executablePath: chrome},
    acceptDownloads: true,
    // A staging trace would hold the passcode header, so traces stay local.
    trace: remote ? 'off' : 'retain-on-failure',
  },
  webServer: remote ? undefined : [agent, api],
});
