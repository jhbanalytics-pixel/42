/* Lighthouse over the local production build. Serves web/dist as plain static
   files, so no passcode, no API and no deployed revision are involved: the
   measured page is the first paint every visitor gets. Runs mobile and desktop
   and prints the four category scores plus LCP and CLS. */
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {existsSync, mkdtempSync, readFileSync, statSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {extname, join, normalize, resolve, sep} from 'node:path';
import {fileURLToPath} from 'node:url';

const repositoryRoot = fileURLToPath(new URL('..', import.meta.url));
const productionRoot = resolve(repositoryRoot, 'web', 'dist');
const CONTENT_TYPES = {
  '.css': 'text/css',
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.json': 'application/json',
  '.woff2': 'font/woff2',
};
const chromeCandidates = [
  process.env.CHROME_PATH,
  String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
  String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);
const chrome = chromeCandidates.find((candidate) => existsSync(candidate));
if (!chrome) {
  console.error('FAIL: Chrome or Chromium is required. Set CHROME_PATH to the browser binary.');
  process.exit(1);
}
const port = 34000 + (process.pid % 1000);
/* Pinned so a Lighthouse release cannot move the recorded scores under us. */
const LIGHTHOUSE_VERSION = 'lighthouse@12.8.2';
const wait = (milliseconds) => new Promise((settle) => setTimeout(settle, milliseconds));

function run(command, args, options){
  return new Promise((settle, fail) => {
    const child = spawn(command, args, {...options, windowsHide: true, shell: process.platform === 'win32'});
    let out = '';
    let err = '';
    child.stdout.on('data', (chunk) => { out += chunk; });
    child.stderr.on('data', (chunk) => { err += chunk; });
    child.on('error', fail);
    /* chrome-launcher fails to remove its own temporary profile on Windows and
       exits nonzero after the report is already written, so the report file is
       the success test, not the exit code. */
    child.on('close', (code) => settle({code, out, err}));
  });
}

async function audit(formFactor, reportPath){
  await run('npx', [
    '-y', LIGHTHOUSE_VERSION, `http://127.0.0.1:${port}/`,
    '--output=json', `--output-path=${reportPath}`, '--quiet',
    ...(formFactor === 'desktop' ? ['--preset=desktop'] : []),
    '--chrome-flags=--headless=new --disable-gpu --no-first-run',
  ], {env: {...process.env, CHROME_PATH: chrome}});
  let report;
  try {
    report = JSON.parse(readFileSync(reportPath, 'utf8'));
  } catch {
    throw new Error(`Lighthouse wrote no ${formFactor} report`);
  }
  const score = (key) => Math.round((report.categories[key]?.score ?? 0) * 100);
  return {
    formFactor,
    performance: score('performance'),
    accessibility: score('accessibility'),
    bestPractices: score('best-practices'),
    seo: score('seo'),
    lcp: report.audits['largest-contentful-paint']?.displayValue ?? null,
    cls: report.audits['cumulative-layout-shift']?.numericValue ?? null,
    /* Naming what moved, so a CLS regression points at an element instead of a
       number. Lighthouse orders these by contribution. */
    shifted: (report.audits['layout-shift-elements']?.details?.items ?? [])
      .slice(0, 4)
      .map((item) => ({node: item.node?.selector ?? '', score: item.score})),
  };
}

const directory = mkdtempSync(join(tmpdir(), 'lp-lighthouse-'));
/* The desk is behind a passcode. Lighthouse gets a local-only value and a
   local desk payload so it measures the signed-in first paint rather than the
   gate; nothing here reads or carries the staging passcode. Unlike the Gate C
   harness this server never forces a reload, so LCP is the real first load. */
const preload = '<script>try{localStorage.setItem("pulse_passcode","lighthouse-local-only")}catch(e){}</script>';
const deskPayload = {
  topics: [],
  updated: '2026-09-03',
  freshness: {status: 'green', age_hours: 1},
  dynamic_discovery: {
    contract_version: 'desk_dynamic_signal_v2', status: 'no_discovery', requested_market: 'za',
    run: {run_id: 'lighthouse-local'}, signals: [], error: null,
  },
};

function apiBody(pathname){
  if (pathname === '/api/health') return {passcode: true};
  if (pathname === '/api/auth/verify') return {ok: true};
  if (pathname.startsWith('/api/desk')) return deskPayload;
  return {};
}

const server = createServer((request, response) => {
  const pathname = new URL(request.url, `http://127.0.0.1:${port}`).pathname;
  if (pathname.startsWith('/api/')){
    response.writeHead(200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
    response.end(JSON.stringify(apiBody(pathname)));
    return;
  }
  const relative = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '');
  const path = normalize(join(productionRoot, relative));
  /* Prefix alone would also admit a sibling directory whose name merely starts
     with the root's, so the separator has to be part of the match. */
  if (path !== productionRoot && !path.startsWith(`${productionRoot}${sep}`)){
    response.writeHead(403);
    response.end();
    return;
  }
  try {
    if (!statSync(path).isFile()) throw new Error('not a file');
    let body = readFileSync(path);
    if (relative === 'index.html') body = Buffer.from(String(body).replace('<script type="module"', `${preload}<script type="module"`));
    response.writeHead(200, {'Content-Type': CONTENT_TYPES[extname(path)] || 'application/octet-stream'});
    response.end(body);
  } catch { response.writeHead(404); response.end('not found'); }
});
try {
  await new Promise((settle) => server.listen(port, '127.0.0.1', settle));
  const results = [];
  for (const formFactor of ['mobile', 'desktop']){
    results.push(await audit(formFactor, join(directory, `${formFactor}.json`)));
  }
  console.log(JSON.stringify(results, null, 2));
} finally {
  server.close();
  rmSync(directory, {recursive: true, force: true});
}
