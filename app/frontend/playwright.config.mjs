import {defineConfig} from '@playwright/test';
import {mkdtempSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {isAbsolute, join} from 'node:path';
import {resolveChrome} from './src/ui/__tests__/task4-proof-helpers.js';

const chrome = resolveChrome();
if (!chrome) throw new Error('Chrome is required for browser journey tests');

export function configureCertificationEvidenceDirectory(environment = process.env, createDirectory = mkdtempSync, temporaryDirectory = tmpdir()){
  const configured = environment.CERTIFICATION_EVIDENCE_DIR;
  if (configured !== undefined){
    if (typeof configured !== 'string' || !configured || !isAbsolute(configured)) throw new Error('CERTIFICATION_EVIDENCE_DIR must be absolute');
    return configured;
  }
  const fresh = createDirectory(join(temporaryDirectory, 'frontend-certification-'));
  environment.CERTIFICATION_EVIDENCE_DIR = fresh;
  return fresh;
}

configureCertificationEvidenceDirectory();

export default defineConfig({
  testDir: './tests/browser',
  testMatch: '**/*.pw.mjs',
  workers: 1,
  fullyParallel: false,
  timeout: 30000,
  reporter: [['line'], ['./tests/browser/support/capability-states-reporter.mjs'], ['./tests/browser/support/certification-reporter.mjs']],
  outputDir: './test-results/browser',
  use: {
    baseURL: 'http://127.0.0.1:4176',
    headless: true,
    viewport: {width: 1280, height: 900},
    launchOptions: {executablePath: chrome},
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 4176 --strictPort',
    url: 'http://127.0.0.1:4176',
    reuseExistingServer: false,
    timeout: 30000,
  },
});
