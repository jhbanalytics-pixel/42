import {afterAll, beforeAll, expect, test} from 'bun:test';
import {createServer} from 'node:http';
import {existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {extname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium} from '@playwright/test';
import {build} from 'vite';
import {resolveChrome} from './task4-proof-helpers.js';

const frontend = fileURLToPath(new URL('../../../', import.meta.url));
const evidence = process.env.LEGACY_CONSOLE_EVIDENCE_DIR || mkdtempSync(join(tmpdir(), 'console-alias-proof-'));
const mime = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2'};
let browser;
let server;
let base;
let consoleChunk;

beforeAll(async () => {
  const chrome = resolveChrome();
  if (!chrome) throw new Error('Chrome is required for console redirect network checks');
  const output = join(evidence, 'dist');
  await build({root: frontend, configLoader: 'native', logLevel: 'error', build: {outDir: output, emptyOutDir: true}});
  consoleChunk = readdirSync(join(output, 'assets')).find((name) => /^ConsoleWorkbench-.*\.js$/.test(name));
  if (!consoleChunk) throw new Error('The production console chunk is missing');
  server = createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname === '/favicon.ico') { response.writeHead(204); response.end(); return; }
    const path = join(output, url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname));
    if (!existsSync(path)) { response.writeHead(404); response.end(); return; }
    response.writeHead(200, {'Content-Type': mime[extname(path)] || 'application/octet-stream'});
    response.end(readFileSync(path));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({executablePath: chrome, headless: true});
}, 30000);

afterAll(async () => {
  if (browser) await browser.close();
  if (server) await new Promise((resolve) => server.close(resolve));
});

async function fixture(){
  const context = await browser.newContext();
  const page = await context.newPage();
  const requests = [];
  let phase = 'setup';
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== base) throw new Error('Unexpected external request: ' + url.origin);
    if (!url.pathname.startsWith('/api/')) { await route.continue(); return; }
    requests.push({phase, path: url.pathname + url.search, method: request.method()});
    const replies = {
      '/api/health': {passcode: false}, '/api/alerts': {alerts: []},
      '/api/investigations': {investigations: []}, '/api/dossiers': {dossiers: []},
      '/api/history/asks': {asks: []}, '/api/history/briefs': {dates: []},
      '/api/schedules': {schedules: []}, '/api/discover': {items: []},
    };
    const reply = replies[url.pathname];
    await route.fulfill({status: reply === undefined ? 404 : 200, contentType: 'application/json', body: JSON.stringify(reply === undefined ? {error: 'not_found'} : reply)});
  });
  return {context, page, requests, phase: (value) => { phase = value; }};
}

async function settled(page, hash, selector){
  await page.waitForFunction((expected) => location.hash === expected, hash, {timeout: 5000});
  await page.locator(selector).first().waitFor({state: 'visible'});
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(100);
}

test('cold console Ask aliases do not dispatch obsolete research reads', async () => {
  const results = [];
  for (const alias of ['console', 'chat', 'research']){
    const host = await fixture();
    try {
      host.phase('case');
      await host.page.goto(base + '/#/' + alias + '?work=ask');
      await settled(host.page, '#/ask', '.ask42-title');
      const obsolete = host.requests.filter((request) => request.path.startsWith('/api/research/'));
      results.push({alias, requests: host.requests, obsolete});
      expect(obsolete).toEqual([]);
    } finally { await host.context.close(); }
  }
  writeFileSync(join(evidence, 'cold.json'), JSON.stringify(results, null, 2));
}, 30000);

test('a warm console Ask hash redirects before the cached workbench can dispatch research reads', async () => {
  const host = await fixture();
  try {
    await host.page.goto(base + '/#/console');
    await settled(host.page, '#/console', '.b42');
    await host.page.evaluate((url) => import(url), base + '/assets/' + consoleChunk);
    await host.page.evaluate(() => { location.hash = '#/console?work=ask'; });
    await settled(host.page, '#/ask', '.ask42-title');
    await host.page.evaluate(() => { location.hash = '#/console'; });
    await settled(host.page, '#/console', '.b42');
    host.phase('case');
    await host.page.evaluate(() => { location.hash = '#/console?work=ask'; });
    await settled(host.page, '#/ask', '.ask42-title');
    const obsolete = host.requests.filter((request) => request.phase === 'case' && request.path.startsWith('/api/research/'));
    writeFileSync(join(evidence, 'warm.json'), JSON.stringify({requests: host.requests, obsolete}, null, 2));
    expect(obsolete).toEqual([]);
  } finally { await host.context.close(); }
}, 30000);

test('console Ask draft links retain their question and market after a warm hash transition', async () => {
  const host = await fixture();
  try {
    await host.page.goto(base + '/#/console');
    await settled(host.page, '#/console', '.b42');
    host.phase('case');
    await host.page.evaluate(() => { location.hash = '#/console?work=ask&q=F3%20fixture%20question&market=NG&draft=1'; });
    await settled(host.page, '#/ask?q=F3+fixture+question&market=NG&draft=1', '.ask42-title');
    expect(await host.page.locator('.ask42 textarea').first().inputValue()).toBe('F3 fixture question');
    expect(await host.page.locator('.ask42 select').first().inputValue()).toBe('NG');
    const writes = host.requests.filter((request) => request.phase === 'case' && !['GET', 'HEAD'].includes(request.method));
    expect(writes).toEqual([]);
    writeFileSync(join(evidence, 'draft.json'), JSON.stringify({hash: await host.page.evaluate(() => location.hash), requests: host.requests}, null, 2));
  } finally { await host.context.close(); }
}, 30000);
