/* Focus after a hash move, measured in a real browser on every route.

   Round 4, task 23. The route effect moved focus to the first heading it
   could find, or to the workspace container when the heading was still
   behind a lazy chunk or a fetch, and on most routes it was. A keyboard
   reader then got a two pixel red ring around the whole workspace and no
   announcement. The ruling: after a hash move focus lands on the workspace
   heading, the package frame title or the route's own heading, on every
   route, and never on the container. The cold load is held here too: one
   health probe, one Today read, and no shared desk read, counted at the server. */
import {afterAll, expect, test} from 'bun:test';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {existsSync, mkdtempSync, readFileSync, rmSync, statSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {extname, join, normalize, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import {ROOT_SIGNAL} from './fixtures/instrument-payloads.js';
import {sourceLabInventory} from './fixtures/source-lab-inventory.js';
import people from './fixtures/people42.json';
import history from './fixtures/history42.json';

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
const wait = (milliseconds) => new Promise((done) => setTimeout(done, milliseconds));

let builtRoot = null;
let builtDirectory = null;
afterAll(() => {
  if (builtRoot) rmSync(builtRoot, {recursive: true, force: true});
});
async function buildOnce(){
  if (builtDirectory) return builtDirectory;
  builtRoot = mkdtempSync(join(tmpdir(), 'lp-route-focus-build-'));
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

/* The desk the routes read: two admitted signals and a checked stamp, the
   same shape the gate server answers with. Fieldwork reads its fixture and
   the inventory fixture so its instrument mounts. */
const admission = {
  discovery_mode: 'phrase', evidence_state: 'ready', market: 'za',
  qualities: {velocity: 'moderate', novelty: 'high', breadth: 'moderate', independence: 'high', history: 'low', geo_confidence: 'moderate', topic_tags: ['repair']},
  receipts: [{id: 'local-receipt', url: 'https://evidence.invalid/local', platform: 'search', snippet: 'Local deterministic fixture'}],
  observation_start: '2026-08-18', observation_end: '2026-08-24', observation_method: 'closed_local_fixture',
};
const firstSignal = {...structuredClone(ROOT_SIGNAL), ...admission, signal_id: `sig_${'a'.repeat(64)}`, signal_name: ROOT_SIGNAL.label};
const secondSignal = {...structuredClone(ROOT_SIGNAL), ...admission, signal_id: `sig_${'b'.repeat(64)}`, label: 'Shared repair knowledge', signal_name: 'Shared repair knowledge'};
const desk = {
  topics: [], updated: '2026-08-25', freshness: {status: 'green', age_hours: 21},
  dynamic_discovery: {
    contract_version: 'desk_dynamic_signal_v2', status: 'ready', requested_market: 'za', run: {run_id: 'local-run'},
    signals: [{contract_version: 'desk_dynamic_signal_v2', signal: firstSignal}, {contract_version: 'desk_dynamic_signal_v2', signal: secondSignal}],
    error: null,
  },
};
/* One drafted investigation, in the shape f42-agent answers with. */
const INVESTIGATION = {
  investigation_id: 'i_fixture01', version: 1, created_at: '2026-09-29T08:00:00+02:00', updated_at: '2026-09-29T08:00:00+02:00',
  who: 'passcode', status: 'draft', question: 'What is behind #fixture in South Africa?', market: 'ZA',
  plan: {sub_questions: [{id: 'q1', text: 'Where did it start?', platforms: ['tiktok'], credits: 120}], researchers: 3, gap_round: true, max_credits: 400, max_model_usd: 3},
  estimate: {credits: 120, model_usd: 0.5, minutes: 2}, ask_id: null, run_id: 'r_plan_1', record: null,
};
/* One skin and its narrowed Today, in the shapes f42-api answers with. */
const SKIN = {
  skin_id: 'sk_fixture01', skin_key: 'fixture', name: 'Fixture skin', markets: ['ZA'], terms: ['fixture'], hashtags: [],
  accounts: [], watch_ids: [], template: 'weekly_report', status: 'active',
};
const SKIN_TODAY = {date: '2026-09-30', markets: [], skin: {text: 'Not in this skin: Nigeria and Kenya'}, alerts: {alerts: [], waiting: []}};
const fieldwork = JSON.parse(readFileSync(new URL('../../../../tests/fixtures/workspaces/fieldwork_workspace_v1.json', import.meta.url), 'utf8')).ready;

function api(pathname){
  if (pathname === '/api/health') return {passcode: false};
  if (pathname.startsWith('/api/desk')) return desk;
  if (pathname.startsWith('/api/voices')) return {creators: [], market_label: 'South Africa'};
  if (pathname.startsWith('/api/internal/v2/fieldwork/read')) return fieldwork;
  if (pathname.startsWith('/api/v2/source-lab')) return sourceLabInventory();
  if (pathname.startsWith('/api/research/recent')) return {artifacts: []};
  if (pathname.startsWith('/api/v2/investigations/list')) return {investigations: []};
  if (pathname.startsWith('/api/v2/investigations/scopes')) return {scopes: []};
  if (pathname.startsWith('/api/creators/')) return people.creator;
  if (pathname === '/api/communities') return people.communities;
  if (pathname.startsWith('/api/communities/')) return people.community;
  if (pathname.startsWith('/api/history/items/')) return history.item;
  if (pathname.startsWith('/api/history/asks')) return history.asks;
  if (pathname === '/api/investigations') return {investigations: [INVESTIGATION]};
  if (pathname.startsWith('/api/investigations/')) return INVESTIGATION;
  if (pathname === '/api/schedules') return {schedules: []};
  if (pathname === '/api/skins') return {skins: [SKIN]};
  if (pathname === '/api/skins/sk_fixture01/today') return SKIN_TODAY;
  if (pathname === '/api/skins/sk_fixture01') return SKIN;
  return {};
}

const TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.json': 'application/json'};

/* Serves the build, counts every API hit by path, and holds the desk read
   for a moment so the loading frame is on screen when the hash moves. */
async function serve(){
  const root = resolve(await buildOnce());
  const hits = {};
  const server = createServer(async (request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname.startsWith('/api/')){
      hits[url.pathname] = (hits[url.pathname] || 0) + 1;
      if (url.pathname.startsWith('/api/desk') || url.pathname.startsWith('/api/voices')) await wait(600);
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
  return {server, port: server.address().port, hits};
}

async function page(profile, url){
  let port = 0;
  for (let attempt = 0; attempt < 200 && !port; attempt += 1){
    try { port = Number(readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split(/\r?\n/, 1)[0]); } catch {}
    if (!port) await wait(50);
  }
  if (!port) throw new Error('the route focus harness found no debugging port');
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
    /* Headless Chrome on Windows clamps a narrow window, so a phone width
       is set on the page's viewport instead. */
    async viewport(width, height){
      await command('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false}, sessionId);
    },
    async close(){
      try { await command('Browser.close'); } catch {}
      socket.close();
    },
  };
}

/* The element holding focus: tag, classes, whether it is the workspace
   container, and whether it sits inside the workspace. */
const FOCUS = `(function(){
  var node = document.activeElement;
  if (!node || node === document.body) return {tag: 'body', heading: false, container: false, inWorkspace: false, text: ''};
  var name = typeof node.className === 'string' && node.className.trim() ? '.' + node.className.trim().split(/\\s+/).join('.') : '';
  return {
    tag: node.tagName.toLowerCase() + (node.id ? '#' + node.id : '') + name,
    heading: /^h[12]$/i.test(node.tagName),
    container: node.id === 'main-content',
    inWorkspace: !!node.closest('#main-content'),
    text: (node.textContent || '').trim().slice(0, 40),
  };
})()`;

/* The walk opens on Briefing, so Briefing is the last move rather than a
   hash that does not change. */
const ROUTES = [
  'browse', 'method', 'explore', 'console', 'research', 'seeds', 'seedpath',
  'topic/sig_' + 'a'.repeat(64), 'lexicon', 'map', 'board', 'compare', 'listen',
  'fieldwork', 'historical', 'source-lab', 'network', 'alerts',
  'dossiers', 'dossiers/d_fixture01', 'd/d_fixture01/2',
  'creators/c_ng_macro?market=NG', 'communities?market=NG', 'communities/' + people.community.community.community_id + '?market=NG',
  'history', 'history/items/' + history.item.item_id + '?market=ZA',
  'investigations', 'investigations/i_fixture01', 'schedules', 'skins', 'skins/sk_fixture01', 'pulse',
];

/* The creator, community, History, Investigations, Schedules and Skins routes land on their own heading,
   not on a page they fell back to. */
const OWN_HEADINGS = {
  'creators/c_ng_macro': '@fixture_ng_macro',
  communities: 'Communities in Nigeria',
  ['communities/' + people.community.community.community_id]: people.community.community.label.slice(0, 40),
  history: 'History',
  ['history/items/' + history.item.item_id]: history.item.label,
  investigations: 'Investigations',
  'investigations/i_fixture01': 'What is behind #fixture in South Africa?',
  /* Shell consistency, 2 October 2026: the title is the menu's noun. */
  schedules: 'Schedules',
  skins: 'Skins',
  'skins/sk_fixture01': 'Fixture skin',
};

async function onTheApp(drive){
  const {server, port, hits} = await serve();
  const directory = mkdtempSync(join(tmpdir(), 'lp-route-focus-'));
  const profile = join(directory, 'profile');
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--force-prefers-reduced-motion', '--window-size=1280,1600',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
  ], {stdio: 'ignore', windowsHide: true});
  let client = null;
  try {
    client = await page(profile, `http://127.0.0.1:${port}/#/pulse`);
    const deadline = Date.now() + 15000;
    let landed = false;
    while (Date.now() < deadline && !landed){
      try { landed = await client.evaluate(`!!document.querySelector('[data-today-loaded]') && !!document.querySelector('.instrument-job-link')`); } catch {}
      if (!landed) await wait(100);
    }
    if (!landed) throw new Error('Today never mounted inside the shell');
    await wait(500);
    return await drive(client, hits);
  } finally {
    if (client) await client.close();
    try { chrome.kill(); } catch {}
    server.close();
    await wait(200);
    /* Chrome can still hold its profile for a moment after it is told to
       close, and the profile is a temporary directory, so a late lock is
       retried and then left for the system to sweep rather than failing
       the measurement it did not take part in. */
    try { rmSync(directory, {recursive: true, force: true, maxRetries: 5, retryDelay: 300}); } catch {}
  }
}

test.skipIf(!CHROME)('a cold Today load probes health once, reads Today once and makes no desk read without stealing focus', async () => {
  const {hits, focus} = await onTheApp(async (client, counted) => ({hits: {...counted}, focus: await client.evaluate(FOCUS)}));
  expect(hits['/api/health']).toBe(1);
  expect(hits['/api/today']).toBe(1);
  expect(hits['/api/desk'] || 0).toBe(0);
  expect(focus.tag).toBe('body');
}, 180000);

test.skipIf(!CHROME)('after a hash move focus lands on the workspace heading on every route, never on the container', async () => {
  const landings = await onTheApp(async (client) => {
    const result = {};
    for (const route of ROUTES){
      await client.evaluate(`document.body.focus(); location.hash = ${JSON.stringify('#/' + route)};`);
      const seen = new Set();
      const deadline = Date.now() + 6000;
      let settled = null;
      while (Date.now() < deadline){
        const now = await client.evaluate(FOCUS);
        seen.add(now.tag);
        /* The route's own heading, once the chunk and the reads have landed,
           is where the walk must end; the loading frame title on the way is
           a heading too, so the poll keeps going until the route settles. */
        if (now.heading && now.inWorkspace && !/state-view__title/.test(now.tag)){ settled = now; break; }
        await wait(50);
      }
      if (!settled){ await wait(300); settled = await client.evaluate(FOCUS); }
      result[route] = {...settled, seen: [...seen]};
    }
    return result;
  });
  const failures = [];
  for (const [route, landing] of Object.entries(landings)){
    if (!landing.heading || !landing.inWorkspace) failures.push(`${route}: focus settled on ${landing.tag}`);
    if (landing.seen.some((tag) => /^div#main-content/.test(tag))) failures.push(`${route}: focus passed through the container`);
    const heading = OWN_HEADINGS[route.split('?')[0]];
    if (heading !== undefined && landing.text !== heading) failures.push(`${route}: focus settled on "${landing.text}", not its own heading`);
  }
  expect(failures).toEqual([]);
}, 240000);

/* Demo polish, 2 October 2026 (QA item 12). The route moves focus to the page
   heading so a screen reader lands on it. That focus is the app's, not the
   reader's (tabindex -1), so the heading paints no ring, after a hash move or
   after a reload or a pasted link alike. Real controls keep their rings (the
   rail test below). */
const HEADING_RING = `(function(){
  var node = document.activeElement;
  if (!node || !/^h[12]$/i.test(node.tagName) || !node.closest('#main-content')) return null;
  var style = getComputedStyle(node);
  return {text: node.textContent.trim().slice(0, 40), tabindex: node.getAttribute('tabindex'), outline: style.outlineStyle, width: style.outlineWidth, shadow: style.boxShadow};
})()`;

async function headingRing(client){
  const deadline = Date.now() + 6000;
  while (Date.now() < deadline){
    const now = await client.evaluate(HEADING_RING);
    if (now && !/could not load/i.test(now.text)) return now;
    await wait(50);
  }
  return client.evaluate(HEADING_RING);
}

test.skipIf(!CHROME)('the page heading the route focuses paints no ring, after a hash move or a reload', async () => {
  const rings = await onTheApp(async (client) => {
    const result = {};
    for (const route of ['history', 'compare', 'explore', 'ask']){
      await client.evaluate(`document.body.focus(); location.hash = ${JSON.stringify('#/' + route)};`);
      await wait(150);
      result[route] = await headingRing(client);
    }
    /* A cold load leaves focus where the browser put it (the first test);
       if a heading does hold it after a reload, it paints no ring either. */
    await client.evaluate(`location.hash = '#/history'; location.reload();`);
    await wait(2500);
    const reloaded = await client.evaluate(HEADING_RING);
    if (reloaded) result['history after reload'] = reloaded;
    return result;
  });
  const failures = [];
  for (const [route, ring] of Object.entries(rings)){
    if (!ring){ failures.push(`${route}: focus did not land on the page heading`); continue; }
    if (ring.tabindex !== '-1') failures.push(`${route}: heading tabindex is ${ring.tabindex}`);
    if (ring.outline !== 'none') failures.push(`${route}: "${ring.text}" paints a ${ring.width} ${ring.outline} ring`);
  }
  expect(failures).toEqual([]);
}, 180000);

/* The 42 rail (EXPERIENCE.md, Navigation) in a real browser. It sits left of
   the workspace at desktop width, Tab walks it, a link moved to by keyboard
   lands focus on the page heading and marks the page, and nothing on it is
   red except the focus ring. */
async function railReady(client){
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline){
    if (await client.evaluate(`!!document.querySelector('nav[aria-label="Main"] a')`)) return;
    await wait(100);
  }
  throw new Error('the rail never mounted');
}

const RAIL_COLOURS = `(function(){
  var probe = document.createElement('span');
  probe.style.color = 'var(--accent)';
  document.querySelector('nav[aria-label="Main"]').appendChild(probe);
  var accent = getComputedStyle(probe).color;
  probe.style.color = 'var(--ink)';
  var ink = getComputedStyle(probe).color;
  probe.remove();
  var current = getComputedStyle(document.querySelector('nav[aria-label="Main"] a[aria-current="page"]'));
  var found = [];
  var nodes = [document.querySelector('nav[aria-label="Main"]')].concat([].slice.call(document.querySelectorAll('nav[aria-label="Main"] *')));
  nodes.forEach(function(node){
    var style = getComputedStyle(node);
    ['color', 'backgroundColor', 'borderTopColor', 'borderRightColor', 'borderBottomColor', 'borderLeftColor', 'boxShadow', 'textDecorationColor'].forEach(function(key){
      if (String(style[key]).indexOf(accent) >= 0) found.push(node.tagName.toLowerCase() + ' ' + (node.textContent || '').trim().slice(0, 20) + ' ' + key);
    });
  });
  return {accent: accent, ink: ink, found: found, marker: {colour: current.borderInlineStartColor, width: current.borderInlineStartWidth}};
})()`;

test.skipIf(!CHROME)('the rail sits beside the workspace, Tab walks it, a keyboard move lands on the page heading and marks the page, and only the focus ring is red', async () => {
  const result = await onTheApp(async (client) => {
    await railReady(client);
    const place = await client.evaluate(`(function(){
      var rail = document.querySelector('nav[aria-label="Main"]').getBoundingClientRect();
      var main = document.getElementById('main-content').getBoundingClientRect();
      return {railLeft: rail.left, railRight: rail.right, railWidth: rail.width, mainLeft: main.left, overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
    })()`);
    await client.evaluate(`document.activeElement && document.activeElement.blur && document.activeElement.blur()`);
    const colours = await client.evaluate(RAIL_COLOURS);
    await client.evaluate(`document.querySelector('nav[aria-label="Main"] a[href="#/alerts"]').focus()`);
    await client.press('Tab', 'Tab', 9);
    await wait(80);
    const tabbed = await client.evaluate(`(function(){
      var node = document.activeElement;
      var style = getComputedStyle(node);
      return {text: node.textContent.trim(), inRail: !!node.closest('nav[aria-label="Main"]'), visible: node.matches(':focus-visible'), outline: style.outlineStyle, width: style.outlineWidth, colour: style.outlineColor};
    })()`);
    await client.press('Enter', 'Enter', 13);
    const deadline = Date.now() + 6000;
    let landed = null;
    while (Date.now() < deadline){
      const now = await client.evaluate(FOCUS);
      if (now.heading && now.inWorkspace && !/state-view__title/.test(now.tag)){ landed = now; break; }
      await wait(50);
    }
    const marked = await client.evaluate(`[].map.call(document.querySelectorAll('nav[aria-label="Main"] a[aria-current="page"]'), function(a){ return a.textContent.trim(); })`);
    const hash = await client.evaluate('location.hash');
    return {place, colours, tabbed, landed, marked, hash};
  });
  expect(result.place.railWidth).toBeGreaterThan(0);
  expect(result.place.railRight).toBeLessThanOrEqual(result.place.mainLeft + 1);
  expect(result.place.overflow).toBeLessThanOrEqual(0);
  expect(result.colours.found).toEqual([]);
  expect(result.colours.marker.colour).toBe(result.colours.ink);
  expect(parseFloat(result.colours.marker.width)).toBeGreaterThanOrEqual(3);
  expect(result.tabbed.text).toBe('Investigations');
  expect(result.tabbed.inRail).toBe(true);
  expect(result.tabbed.visible).toBe(true);
  expect(result.tabbed.outline).toBe('solid');
  expect(parseFloat(result.tabbed.width)).toBeGreaterThanOrEqual(2);
  expect(result.tabbed.colour).toBe(result.colours.accent);
  expect(result.hash).toBe('#/investigations');
  expect(result.landed && result.landed.text).toBe('Investigations');
  expect(result.marked).toEqual(['Investigations']);
}, 180000);

test.skipIf(!CHROME)('at phone width the rail is a bottom bar with no sideways scroll, and More opens a sheet inside the screen', async () => {
  const result = await onTheApp(async (client) => {
    await client.viewport(390, 844);
    await wait(500);
    await railReady(client);
    const measure = `(function(){
      var nav = document.querySelector('nav[aria-label="Main"]');
      var rect = nav.getBoundingClientRect();
      var more = [].find.call(nav.querySelectorAll('button'), function(b){ return b.textContent.trim().indexOf('More') === 0; });
      var sheet = document.getElementById(more.getAttribute('aria-controls'));
      var sheetRect = sheet.getBoundingClientRect();
      return {
        layout: nav.getAttribute('data-rail-layout'), position: getComputedStyle(nav).position,
        top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
        navOverflow: nav.scrollWidth - nav.clientWidth,
        pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        innerWidth: innerWidth, innerHeight: innerHeight,
        expanded: more.getAttribute('aria-expanded'), sheetHidden: sheet.hidden,
        sheet: {top: sheetRect.top, bottom: sheetRect.bottom, left: sheetRect.left, right: sheetRect.right, overflow: sheet.scrollWidth - sheet.clientWidth},
      };
    })()`;
    const closed = await client.evaluate(measure);
    await client.evaluate(`[].find.call(document.querySelectorAll('nav[aria-label="Main"] button'), function(b){ return b.textContent.trim().indexOf('More') === 0; }).click()`);
    await wait(150);
    const open = await client.evaluate(measure);
    return {closed, open};
  });
  const {closed, open} = result;
  expect(closed.layout).toBe('bar');
  expect(closed.position).toBe('fixed');
  expect(closed.innerWidth).toBe(390);
  expect(Math.abs(closed.bottom - closed.innerHeight)).toBeLessThanOrEqual(1);
  expect(closed.left).toBeGreaterThanOrEqual(0);
  expect(closed.right).toBeLessThanOrEqual(closed.innerWidth);
  expect(closed.navOverflow).toBeLessThanOrEqual(0);
  expect(closed.pageOverflow).toBeLessThanOrEqual(0);
  expect(closed.sheetHidden).toBe(true);
  expect(open.expanded).toBe('true');
  expect(open.sheetHidden).toBe(false);
  expect(open.sheet.left).toBeGreaterThanOrEqual(0);
  expect(open.sheet.right).toBeLessThanOrEqual(open.innerWidth);
  expect(open.sheet.top).toBeGreaterThanOrEqual(0);
  expect(open.sheet.bottom).toBeLessThanOrEqual(open.top + 1);
  expect(open.sheet.overflow).toBeLessThanOrEqual(0);
  expect(open.pageOverflow).toBeLessThanOrEqual(0);
}, 180000);
