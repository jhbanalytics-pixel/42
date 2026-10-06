/* Wide tables scroll inside their own box, measured in a real browser.

   From 1024 px the shell clips sideways overflow (rail42.css), so a table
   wider than its column would be cut with no way to reach the rest. Every
   table on Coverage and the Compare "Side by side" table sits in a named,
   focusable region that scrolls sideways, the page itself never scrolls
   sideways, and a keyboard reader can scroll the region with the arrow keys.
   Discover and History hold no tables. */
import {afterAll, expect, test} from 'bun:test';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync} from 'node:fs';
import {extname, join, normalize, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import coverageFixture from './fixtures/coverage42_day.json';
import compareFixture from './fixtures/compare42_items.json';

function resolveChrome(){
  return [
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean).find((candidate) => existsSync(candidate)) || null;
}

const CHROME = resolveChrome();
const FRONTEND = fileURLToPath(new URL('../../../', import.meta.url));
const REPO_ROOT = resolve(FRONTEND, '..', '..');
const TEST_RESULTS = join(REPO_ROOT, 'test-results', 'l4');
function testTempDirectory(prefix){
  mkdirSync(TEST_RESULTS, {recursive: true});
  return mkdtempSync(join(TEST_RESULTS, prefix));
}

const wait = (milliseconds) => new Promise((done) => setTimeout(done, milliseconds));

let builtRoot = null;
let builtDirectory = null;
afterAll(() => {
  if (builtRoot) rmSync(builtRoot, {recursive: true, force: true});
});
async function buildOnce(){
  if (builtDirectory) return builtDirectory;
  builtRoot = testTempDirectory('wide-tables-build-');
  const outDir = join(builtRoot, 'dist');
  const vite = join(FRONTEND, 'node_modules', '.bin', process.platform === 'win32' ? 'vite.exe' : 'vite');
  const run = await new Promise((done, fail) => {
    const child = spawn(vite, ['build', '--outDir', outDir, '--emptyOutDir'], {cwd: FRONTEND, windowsHide: true});
    let stderr = '';
    child.stderr.on('data', (chunk) => { stderr += chunk; });
    child.stdout.on('data', () => {});
    child.on('error', fail);
    child.on('close', (code) => done({code, stderr}));
  });
  if (run.code !== 0) throw new Error(`vite build exited ${run.code}: ${run.stderr.slice(0, 600)}`);
  builtDirectory = outDir;
  return outDir;
}

/* Coverage with a scorecard, so all four of its tables are on the page. */
const fig = (value, unit) => ({value, unit, query_id: 'q_scorecard_x', run_id: 'r_learn_20260921_01', result_hash: 'sha256:' + unit});
const card = {
  market: 'ZA', reasons: {},
  time_to_detect: fig(2.0, 'days'), lead_time: fig(1.5, 'days'),
  precision: fig(0.75, 'share of reviewed top trends marked real'),
  recall: fig(0.62, 'share of moments surfaced within 24 hours'),
  breadth: null, cost_per_confirmed_trend: fig(48.5, 'credits per confirmed trend'),
};
const coverage = {...structuredClone(coverageFixture), scorecard: {week: '2026-09-21', week_end: '2026-09-27', markets: [card, {...card, market: 'KE'}]}, scorecard_text: null};
const DISCOVER = {
  date: '2026-09-30', market: 'ZA', run_id: 'r_d1', next_cursor: null,
  items: [{item_id: 'i_step', market: 'ZA', title: '#fixture_za_step'}, {item_id: 'i_cola', market: 'ZA', title: 'Fixture Cola'}, {item_id: 'i_ring', market: 'ZA', title: '#fixture_za_ring'}],
  held_back: {count: 0, items: []},
  filters: {kinds: [], states: [], platforms: []},
};

function api(pathname){
  if (pathname === '/api/health') return {passcode: false};
  if (pathname.startsWith('/api/coverage')) return coverage;
  if (pathname.startsWith('/api/compare')) return compareFixture;
  if (pathname.startsWith('/api/discover')) return DISCOVER;
  return {};
}

const TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.json': 'application/json'};

async function serve(){
  const root = resolve(await buildOnce());
  const server = createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname.startsWith('/api/')){
      response.writeHead(200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
      response.end(JSON.stringify(api(url.pathname)));
      return;
    }
    const relativePath = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '');
    const path = normalize(join(root, relativePath));
    if (!path.startsWith(root)){ response.writeHead(403); response.end(); return; }
    try {
      if (!statSync(path).isFile()) throw new Error('not a file');
      response.writeHead(200, {'Content-Type': TYPES[extname(path)] || 'application/octet-stream', 'Cache-Control': 'no-store'});
      response.end(readFileSync(path));
    } catch { response.writeHead(404); response.end('not found'); }
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  return {server, port: server.address().port};
}

async function page(profile, url){
  let port = 0;
  for (let attempt = 0; attempt < 200 && !port; attempt += 1){
    try { port = Number(readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split(/\r?\n/, 1)[0]); } catch {}
    if (!port) await wait(50);
  }
  if (!port) throw new Error('the wide table harness found no debugging port');
  let endpoint = null;
  for (let attempt = 0; attempt < 200 && !endpoint; attempt += 1){
    try { endpoint = (await fetch(`http://127.0.0.1:${port}/json/version`).then((r) => r.json())).webSocketDebuggerUrl; } catch {}
    if (!endpoint) await wait(50);
  }
  const socket = new WebSocket(endpoint);
  await new Promise((done, fail) => { socket.addEventListener('open', done, {once: true}); socket.addEventListener('error', fail, {once: true}); });
  const pending = new Map();
  let id = 0;
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const {done, fail} = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) fail(new Error(message.error.message)); else done(message.result);
  });
  const command = (method, params = {}, sessionId) => new Promise((done, fail) => {
    const commandId = ++id;
    pending.set(commandId, {done, fail});
    socket.send(JSON.stringify({id: commandId, method, params, ...(sessionId ? {sessionId} : {})}));
  });
  const {targetId} = await command('Target.createTarget', {url});
  const {sessionId} = await command('Target.attachToTarget', {targetId, flatten: true});
  return {
    async evaluate(expression){
      const result = await command('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true}, sessionId);
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
      return result.result.value;
    },
    async press(key, code, keyCode){
      await command('Input.dispatchKeyEvent', {type: 'keyDown', key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode}, sessionId);
      await command('Input.dispatchKeyEvent', {type: 'keyUp', key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode}, sessionId);
    },
    async viewport(width, height){
      await command('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false}, sessionId);
    },
    async close(){
      try { await command('Browser.close'); } catch {}
      socket.close();
    },
  };
}

async function onTheApp(hash, ready, drive){
  const {server, port} = await serve();
  const directory = testTempDirectory('wide-tables-browser-');
  const profile = join(directory, 'profile');
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--force-prefers-reduced-motion', '--window-size=1440,1200',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
  ], {stdio: 'ignore', windowsHide: true});
  let client = null;
  try {
    client = await page(profile, `http://127.0.0.1:${port}/${hash}`);
    const deadline = Date.now() + 15000;
    let landed = false;
    while (Date.now() < deadline && !landed){
      try { landed = await client.evaluate(ready); } catch {}
      if (!landed) await wait(100);
    }
    if (!landed) throw new Error(`${hash} never showed its tables`);
    await wait(300);
    return await drive(client);
  } finally {
    if (client) await client.close();
    try { chrome.kill(); } catch {}
    server.close();
    await wait(200);
    try { rmSync(directory, {recursive: true, force: true, maxRetries: 5, retryDelay: 300}); } catch {}
  }
}

const NATURAL_FIRST_COLUMN = String.raw`(function(){
  var probe = document.createElement('span');
  probe.style.position = 'fixed';
  probe.style.left = '-10000px';
  probe.style.top = '0';
  probe.style.visibility = 'hidden';
  probe.style.whiteSpace = 'nowrap';
  document.body.appendChild(probe);
  return Array.from(document.querySelectorAll('.cv42 table tr'))
    .map(function(row){ return row.cells[0]; })
    .filter(Boolean)
    .map(function(cell){
      var style = getComputedStyle(cell);
      probe.style.fontFamily = style.fontFamily;
      probe.style.fontSize = style.fontSize;
      probe.style.fontWeight = style.fontWeight;
      probe.style.fontStyle = style.fontStyle;
      probe.style.fontStretch = style.fontStretch;
      probe.style.letterSpacing = style.letterSpacing;
      var longestWordWidth = String(cell.textContent || '').trim().split(/\s+/).reduce(function(widest, word){
        probe.textContent = word;
        return Math.max(widest, probe.getBoundingClientRect().width);
      }, 0);
      var rect = cell.getBoundingClientRect();
      var contentWidth = rect.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      return {label: String(cell.textContent || '').trim().replace(/\s+/g, ' '), contentWidth: contentWidth, longestWordWidth: longestWordWidth};
    });
})()`;

/* Makes the table at the given index wider than the workspace, then reads
   the page, the table's box and the box's colours and type size, with
   nothing focused so the focus ring is not counted as red. */
const WIDEN = (selector, index) => `(function(){
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  document.querySelectorAll('table[data-widened]').forEach(function(t){ t.style.minWidth = ''; t.removeAttribute('data-widened'); });
  var table = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
  var column = document.getElementById('main-content').getBoundingClientRect().width;
  table.style.minWidth = Math.ceil(column + 400) + 'px';
  table.setAttribute('data-widened', '');
  var box = table.parentElement;
  var rect = box.getBoundingClientRect();
  var probe = document.createElement('span');
  probe.style.color = 'var(--accent)';
  box.appendChild(probe);
  var accent = getComputedStyle(probe).color;
  probe.remove();
  var style = getComputedStyle(box);
  var red = ['color', 'backgroundColor', 'borderTopColor', 'borderRightColor', 'borderBottomColor', 'borderLeftColor', 'boxShadow', 'outlineColor'].filter(function(key){
    return key === 'outlineColor' ? style.outlineStyle !== 'none' && style.outlineColor === accent : String(style[key]).indexOf(accent) >= 0;
  });
  return {
    pageScrollWidth: document.documentElement.scrollWidth,
    innerWidth: innerWidth,
    workspaceOverflow: document.getElementById('main-content').scrollWidth - document.getElementById('main-content').clientWidth,
    overflowX: style.overflowX,
    role: box.getAttribute('role'),
    label: box.getAttribute('aria-label') || '',
    tabindex: box.getAttribute('tabindex'),
    scrollWidth: box.scrollWidth,
    clientWidth: box.clientWidth,
    left: rect.left,
    right: rect.right,
    fontSize: style.fontSize,
    parentFontSize: getComputedStyle(box.parentElement).fontSize,
    red: red,
  };
})()`;

/* Focuses the box and presses the right arrow: the box scrolls, the page does not. */
async function arrowScroll(client, selector, index){
  await client.evaluate(`(function(){ var box = document.querySelectorAll(${JSON.stringify(selector)})[${index}].parentElement; box.scrollLeft = 0; box.focus(); })()`);
  const readPosition = () => client.evaluate(`(function(){
    var box = document.querySelectorAll(${JSON.stringify(selector)})[${index}].parentElement;
    return {left: box.scrollLeft, max: box.scrollWidth - box.clientWidth};
  })()`);
  let position = await readPosition();
  for (let i = 0; i < 100 && position.left < position.max - 1; i += 1){
    await client.press('ArrowRight', 'ArrowRight', 39);
    await wait(20);
    let next = await readPosition();
    if (next.left <= position.left){
      await wait(100);
      next = await readPosition();
      if (next.left <= position.left) break;
    }
    position = next;
  }
  await wait(200);
  return client.evaluate(`(function(){
    var box = document.querySelectorAll(${JSON.stringify(selector)})[${index}].parentElement;
    return {
      focused: document.activeElement === box,
      scrollLeft: box.scrollLeft,
      reachedEnd: box.scrollLeft >= box.scrollWidth - box.clientWidth - 1,
      pageScrollX: scrollX,
    };
  })()`);
}

async function measure(client, selector, count){
  const result = [];
  for (const [width, height] of [[390, 844], [1024, 900], [1440, 1000]]){
    await client.viewport(width, height);
    await wait(300);
    for (let index = 0; index < count; index += 1){
      const read = await client.evaluate(WIDEN(selector, index));
      const keys = await arrowScroll(client, selector, index);
      result.push({width, index, ...read, ...keys});
    }
  }
  return result;
}

function failuresOf(reads){
  const failures = [];
  for (const r of reads){
    const at = `${r.width}px table ${r.index}`;
    if (r.pageScrollWidth > r.innerWidth) failures.push(`${at}: page is ${r.pageScrollWidth} wide in a ${r.innerWidth} window`);
    if (r.workspaceOverflow > 0) failures.push(`${at}: the workspace is ${r.workspaceOverflow}px wider than its column`);
    if (r.overflowX !== 'auto' && r.overflowX !== 'scroll') failures.push(`${at}: box overflow-x is ${r.overflowX}`);
    if (r.role !== 'region') failures.push(`${at}: box role is ${r.role}`);
    if (!r.label.trim()) failures.push(`${at}: box has no name`);
    if (r.tabindex !== '0') failures.push(`${at}: box tabindex is ${r.tabindex}`);
    if (!(r.scrollWidth > r.clientWidth)) failures.push(`${at}: box does not scroll (${r.scrollWidth} in ${r.clientWidth})`);
    if (r.left < 0 || r.right > r.innerWidth) failures.push(`${at}: box runs off screen (${r.left} to ${r.right})`);
    if (r.fontSize !== r.parentFontSize) failures.push(`${at}: box sets its own type size ${r.fontSize}`);
    if (r.red.length) failures.push(`${at}: box is red on ${r.red.join(', ')}`);
    if (!r.focused) failures.push(`${at}: box took no focus`);
    if (!(r.scrollLeft > 0)) failures.push(`${at}: arrow keys did not scroll the box`);
    if (!r.reachedEnd) failures.push(`${at}: ArrowRight stopped at ${r.scrollLeft}px before ${r.scrollWidth - r.clientWidth}px`);
    if (r.pageScrollX !== 0) failures.push(`${at}: arrow keys scrolled the page`);
  }
  return failures;
}

test.skipIf(!CHROME)('every Coverage table scrolls inside its own named box at 390, 1024, and 1440, never the page', async () => {
  const result = await onTheApp('#/coverage', `document.querySelectorAll('.cv42 table').length === 5`, async (client) => {
    await client.viewport(390, 844);
    await wait(300);
    const natural = await client.evaluate(NATURAL_FIRST_COLUMN);
    const reads = await measure(client, '.cv42 table', 5);
    return {natural, reads};
  });
  expect(result.natural.length).toBeGreaterThan(0);
  expect(result.natural.filter((r) => r.contentWidth + 1 < r.longestWordWidth)).toEqual([]);
  const reads = result.reads;
  expect(reads.length).toBe(15);
  expect(failuresOf(reads)).toEqual([]);
  expect(reads.filter((r) => r.width === 1440).map((r) => r.label)).toEqual([
    'South Africa series table', 'Nigeria series table', 'Credits table', 'Runs of the day table', 'Weekly scorecard table',
  ]);
}, 180000);

test.skipIf(!CHROME)('the Compare side by side table scrolls inside its own named box at 390, 1024, and 1440, never the page', async () => {
  const reads = await onTheApp('#/compare?mode=items&items=i_step,i_cola,i_ring&market=ZA&days=7', `!!document.querySelector('.c42 table')`, (client) => measure(client, '.c42 table', 1));
  expect(reads.length).toBe(3);
  expect(failuresOf(reads)).toEqual([]);
  expect(reads[0].label).toBe('Side by side table');
}, 180000);
