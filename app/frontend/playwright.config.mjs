import {defineConfig} from '@playwright/test';
import {mkdtempSync, readFileSync, readdirSync} from 'node:fs';
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

/* A spec with a config of its own (playwright.<name>.config.mjs) pins that
   config's origin and refuses every other one, so the aggregate run at 4176
   leaves it out; run it under its own config. */
export function dedicatedSpecs(directory = new URL('.', import.meta.url)){
  return readdirSync(directory)
    .filter((name) => /^playwright\..+\.config\.mjs$/.test(name))
    .flatMap((name) => [...readFileSync(new URL(name, directory), 'utf8').matchAll(/testMatch:\s*'([^']+)'/g)].map((match) => match[1]));
}

export default defineConfig({
  testDir: './tests/browser',
  testMatch: '**/*.pw.mjs',
  testIgnore: dedicatedSpecs(),
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
