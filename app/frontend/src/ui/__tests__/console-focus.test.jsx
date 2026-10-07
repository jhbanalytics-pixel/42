/* Build focus on the production page. A fresh load leaves the rail first
   in the Tab order. Keyboard controls retain their focus rings, while a
   draft sent from Build takes focus to the Ask heading without a ring. */
import {afterAll, expect, test} from 'bun:test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {createServer} from 'node:http';
import {existsSync, mkdtempSync, readFileSync, rmSync, statSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {extname, join, normalize, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

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
  builtRoot = mkdtempSync(join(tmpdir(), 'lp-console-focus-build-'));
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

const TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.json': 'application/json'};
const API = {
  '/api/health': {passcode: false},
  '/api/history/asks': {asks: [{ask_id: 'a_focus_fixture', question: 'Console focus fixture answer', status: 'complete', answer_status: 'complete', market: 'ZA', at: '2026-10-06T08:00:00Z'}]},
  '/api/history/briefs': {dates: []},
  '/api/investigations': {investigations: []},
  '/api/dossiers': {dossiers: []},
  '/api/schedules': {schedules: []},
  '/api/discover': {items: []},
};

/* Serves the build with the theme preset and the landing's reads answered as
   an empty estate, the same way the contrast measurement does. */
async function serve(theme){
  const root = resolve(await buildOnce());
  const preload = `<script>localStorage.setItem('oi-theme', ${JSON.stringify(theme)});</script>`;
  const server = createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname.startsWith('/api/')){
      const answer = API[url.pathname];
      response.writeHead(answer === undefined ? 404 : 200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
      response.end(JSON.stringify(answer === undefined ? {error: 'not_found'} : answer));
      return;
    }
    const relativePath = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '');
    const path = normalize(join(root, relativePath));
    if (!path.startsWith(root)){ response.writeHead(403); response.end(); return; }
    try {
      if (!statSync(path).isFile()) throw new Error('not a file');
      let body = readFileSync(path);
      if (relativePath === 'index.html') body = Buffer.from(String(body).replace('<script type="module"', preload + '<script type="module"'));
      response.writeHead(200, {'Content-Type': TYPES[extname(path)] || 'application/octet-stream', 'Cache-Control': 'no-store'});
      response.end(body);
    } catch { response.writeHead(404); response.end('not found'); }
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  return {server, port: server.address().port};
}

/* A flat debugging session on a page target of our own, so the tab Chrome
   opens at startup cannot take the session with it when it is discarded. */
async function page(profile, url){
  let port = 0;
  for (let attempt = 0; attempt < 200 && !port; attempt += 1){
    try { port = Number(readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split(/\r?\n/, 1)[0]); } catch {}
    if (!port) await wait(50);
  }
  if (!port) throw new Error('the console focus harness found no debugging port');
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
    async press(key, code, keyCode, text){
      await command('Input.dispatchKeyEvent', {type: 'keyDown', key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode, ...(text ? {text} : {})}, sessionId);
      await command('Input.dispatchKeyEvent', {type: 'keyUp', key, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode}, sessionId);
    },
    /* With scrollbars shown the window's frame eats into the page, so the
       page is given the exact width a reader's viewport has. */
    async viewport(width, height){
      await command('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false}, sessionId);
    },
    async close(){
      try { await command('Browser.close'); } catch {}
      socket.close();
    },
  };
}

/* What the page says about its focus: which element holds it, whether the
   browser considers that focus visible, the ring it paints, and whether it
   sits inside the workbench. */
const FOCUS_STATE = `(function(){
  var node = document.activeElement;
  if (!node || node === document.body) return {tag: 'body', id: '', visible: false, outline: 'none', width: '0px', text: '', inWorkbench: false};
  var style = getComputedStyle(node);
  var name = typeof node.className === 'string' && node.className.trim() ? '.' + node.className.trim().split(/\\s+/).join('.') : '';
  return {
    tag: node.tagName.toLowerCase() + name,
    id: node.id,
    tabindex: node.getAttribute('tabindex'),
    visible: node.matches(':focus-visible'),
    outline: style.outlineStyle,
    width: style.outlineWidth,
    colour: style.outlineColor,
    text: (node.textContent || '').trim().slice(0, 40),
    inWorkbench: !!node.closest('#instrument-workspace'),
  };
})()`;

async function onTheConsole(theme, drive, width = 1440, {scrollbars = false, height = 1000, hash = '#/console', surface = '.b42'} = {}){
  const {server, port} = await serve(theme);
  const directory = mkdtempSync(join(tmpdir(), 'lp-console-focus-'));
  const profile = join(directory, 'profile');
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    ...(scrollbars ? [] : ['--hide-scrollbars']), '--force-prefers-reduced-motion', `--window-size=${width},${height}`,
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
  ], {stdio: 'ignore', windowsHide: true});
  let client = null;
  try {
    client = await page(profile, `http://127.0.0.1:${port}/${hash}`);
    if (scrollbars) await client.viewport(width, height);
    const deadline = Date.now() + 15000;
    let landed = false;
    while (Date.now() < deadline && !landed){
      try { landed = await client.evaluate(`!!document.querySelector(${JSON.stringify(surface + ' h1')}) && !!document.querySelector('nav.rail42') && (${JSON.stringify(surface)} !== '.b42' || document.body.textContent.includes('Console focus fixture answer'))`); } catch {}
      if (!landed) await wait(100);
    }
    if (!landed) throw new Error('the console never mounted inside the shell beside the 42 rail: ' + await client.evaluate(`JSON.stringify({hash: location.hash, text: document.body.innerText.slice(0, 1200)})`));
    await wait(300);
    return await drive(client);
  } finally {
    if (client) await client.close();
    try { chrome.kill(); } catch {}
    server.close();
    /* Chrome releases its profile a moment after the kill; on Windows an
       early remove fails with EBUSY, so the exit is awaited and the remove
       retried rather than failing a measurement that already passed. */
    await Promise.race([once(chrome, 'exit'), wait(5000)]);
    for (let attempt = 0; attempt < 20 && existsSync(directory); attempt += 1){
      try { rmSync(directory, {recursive: true, force: true}); } catch { await wait(250); }
    }
  }
}

/* Round 3, Task 18. The workbench moved focus to its stage heading on
   mount, so a reader who pressed Tab on a fresh console load walked the
   two action cards and the recent investigation links, fell off the end of
   the document onto body, and only then reached the skip link. The ruling:
   a fresh load steals no focus, so the shell chrome comes first, and the
   heading takes focus only when the reader moves inside the workbench. The
   stops at 1440 are the skip link, the masthead, the market control, the
   theme control, the five jobs, then the workbench's own controls.

   Round 4, task 25. The skip link at stop one is the keyboard path to the
   workspace: a reader who does not want the rail presses Enter there and
   lands past every link below.

   Round 5, task 32. The rail carries the five jobs and nothing else, so the
   eleven utility links task 25 put after the jobs are gone and the walk is
   nine stops before the workbench.

   The current rail comes first: its skip link, seven pages, closed More
   and theme, then the shell's masthead, market and theme controls. Build's
   forms follow. The shell's hidden jobs take no stop. */
const RAIL_TOP_LABELS = ['Today', 'Ask', 'Discover', 'Alerts', 'Investigations', 'Dossiers', 'History'];
const RAIL_MORE_LABELS = ['Compare', 'Lexicon', 'Communities', 'Seed path', 'Seeds', 'Coverage', 'Fieldwork', 'Method', 'Schedules', 'Skins', 'Hidden people', 'All pages'];
const railLink = (label) => [/^a\.rail42-link$/, label];
const SHELL_STOPS = [
  [/^a\.instrument-skip-link$/, 'Skip to workspace'],
  ...RAIL_TOP_LABELS.map(railLink),
  [/^button\.rail42-more-button$/, 'More'],
  [/^select$/, null],
  [/^a\.instrument-masthead/, null],
  [/^button\.instrument-market-trigger/, null],
  [/^button\.instrument-theme-control/, null],
];
const WALK = SHELL_STOPS.length + 3;

for (const theme of ['midnight', 'daylight']){
  test.skipIf(!CHROME)(`${WALK} Tab stops from a fresh Build load walk the 42 rail and shell chrome, then the workspace, and never land on body in ${theme}`, async () => {
    const stops = await onTheConsole(theme, async (client) => {
      const walk = [await client.evaluate(FOCUS_STATE)];
      for (let index = 0; index < WALK; index += 1){
        await client.press('Tab', 'Tab', 9);
        await wait(80);
        walk.push(await client.evaluate(FOCUS_STATE));
      }
      return walk;
    });
    expect(stops[0].inWorkbench, `a fresh load put focus on ${stops[0].tag}`).toBe(false);
    const tags = stops.slice(1).map((stop) => stop.tag);
    for (const [index, [pattern, text]] of SHELL_STOPS.entries()){
      expect(tags[index], `stop ${index + 1} is ${tags[index]}`).toMatch(pattern);
      if (text !== null) expect(stops[index + 1].text, `stop ${index + 1} reads ${stops[index + 1].text}`).toBe(text);
    }
    expect(tags.filter((tag) => /instrument-job-link|instrument-menu/.test(tag))).toEqual([]);
    for (const index of [SHELL_STOPS.length, SHELL_STOPS.length + 1, SHELL_STOPS.length + 2]){
      expect(stops[index + 1].inWorkbench, `stop ${index + 1} is ${tags[index]}`).toBe(true);
    }
    expect(stops.slice(SHELL_STOPS.length + 1).map((stop) => stop.id)).toEqual([
      'b42-ask-question', 'b42-ask-market', 'b42-inv-question',
    ]);
    expect(tags.filter((tag) => tag === 'body')).toEqual([]);
    expect(new Set(stops.slice(1, 6).map((stop) => stop.text)).size).toBe(5);
    expect(stops[1].visible).toBe(true);
    expect(stops[1].outline).toBe('solid');
    expect(parseFloat(stops[1].width)).toBeGreaterThanOrEqual(2);
  }, 180000);

  test.skipIf(!CHROME)(`submitting a draft from Build takes keyboard focus to the Ask heading without a ring in ${theme}`, async () => {
    const state = await onTheConsole(theme, async (client) => {
      for (let index = 0; index <= SHELL_STOPS.length; index += 1){
        await client.press('Tab', 'Tab', 9);
      }
      expect((await client.evaluate(FOCUS_STATE)).id).toBe('b42-ask-question');
      await client.evaluate(`(function(){
        const field = document.getElementById('b42-ask-question');
        Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(field, 'What changed in this fixture?');
        field.dispatchEvent(new Event('input', {bubbles: true}));
      })()`);
      await wait(50);
      await client.press('Tab', 'Tab', 9);
      expect((await client.evaluate(FOCUS_STATE)).id).toBe('b42-ask-market');
      await client.press('Tab', 'Tab', 9);
      const submit = await client.evaluate(FOCUS_STATE);
      expect(submit.tag).toMatch(/^button\./);
      expect(submit.text).toBe('Ask');
      await client.press('Enter', 'Enter', 13, '\r');
      const deadline = Date.now() + 5000;
      let landed = false;
      while (Date.now() < deadline && !landed){
        landed = await client.evaluate(`(function(){var n=document.activeElement;return !!n && /^h[12]$/i.test(n.tagName) && n.textContent.trim() === 'Ask' && !!n.closest('.ask42');})()`);
        if (!landed) await wait(50);
      }
      expect(await client.evaluate(`location.hash.startsWith('#/ask?') && new URLSearchParams(location.hash.split('?')[1]).get('draft') === '1'`)).toBe(true);
      return client.evaluate(FOCUS_STATE);
    });
    expect(state.tag).toMatch(/^h[12]/);
    expect(state.inWorkbench).toBe(true);
    expect(state.tabindex).toBe('-1');
    /* The ruling is about the paint, not the browser's opinion of the focus:
       whether or not Chrome calls this focus visible, nothing is drawn. */
    expect(state.outline === 'none' || state.width === '0px', `heading paints ${state.width} ${state.outline} ${state.colour}`).toBe(true);
  }, 180000);
}

/* The 42 rail is the only navigation (EXPERIENCE.md, Navigation). The shell
   still renders its five jobs, so this measures what the reader gets at the
   rail width, the phone bar width above the shell's own phone break, and the
   phone width: the job rail and its Menu button take no box, Today, Discover
   and Compare each show once and Briefing not at all, and the workspace
   starts at the shell's own edge rather than past an empty column. */
const NAVIGATION_STATE = `(function(){
  var shown = function(node){ return !!node && getComputedStyle(node).display !== 'none' && node.getClientRects().length > 0; };
  var links = function(label){ return [].slice.call(document.querySelectorAll('a')).filter(function(a){ return a.textContent.trim() === label && shown(a); }).length; };
  var shell = document.querySelector('.instrument-shell').getBoundingClientRect();
  var main = document.getElementById('instrument-workspace').getBoundingClientRect();
  var skips = [].slice.call(document.querySelectorAll('a[href="#instrument-workspace"]')).filter(shown);
  var rail = document.querySelector('nav.rail42');
  return {
    jobRail: shown(document.querySelector('.instrument-job-rail')),
    jobLinks: [].slice.call(document.querySelectorAll('.instrument-job-link')).filter(shown).length,
    menu: shown(document.querySelector('.instrument-menu')),
    counts: {Today: links('Today'), Ask: links('Ask'), Discover: links('Discover'), Alerts: links('Alerts'), Compare: links('Compare'), Briefing: links('Briefing'), Build: links('Build'), Fieldwork: links('Fieldwork')},
    workspaceInset: Math.round(main.left - shell.left),
    skips: skips.length,
    skipInShell: skips.map(function(a){ return !!a.closest('.oi-product'); }),
    layout: rail.getAttribute('data-rail-layout'),
    railBeforeShell: !!(rail.compareDocumentPosition(document.querySelector('.oi-product')) & Node.DOCUMENT_POSITION_FOLLOWING),
  };
})()`;

for (const [width, layout] of [[1440, 'rail'], [900, 'bar'], [390, 'bar']]){
  test.skipIf(!CHROME)(`at ${width} the shell's five jobs and its Menu are hidden, each rail page shows once, and the workspace takes the shell's full width`, async () => {
    const state = await onTheConsole('daylight', (client) => client.evaluate(NAVIGATION_STATE), width);
    expect(state.layout).toBe(layout);
    expect(state.jobRail).toBe(false);
    expect(state.jobLinks).toBe(0);
    expect(state.menu).toBe(false);
    expect([state.counts.Today, state.counts.Ask, state.counts.Discover, state.counts.Alerts, state.counts.Briefing]).toEqual([1, 1, 1, 1, 0]);
    expect([state.counts.Compare, state.counts.Build, state.counts.Fieldwork]).toEqual([0, 0, 0]);
    expect(state.workspaceInset).toBe(0);
    /* One skip link at every width: the rail's own, ahead of the rail and
       the shell, where the rail is the left column; the shell's own where
       the rail is the phone bar at the foot. */
    expect(state.skips).toBe(1);
    expect(state.skipInShell).toEqual([layout !== 'rail']);
    if (layout === 'rail'){
      expect(state.railBeforeShell).toBe(true);
    }
  }, 180000);
}

/* The skip link is the first stop and the keyboard path past the rail: Enter
   on it lands focus on the workspace, and the reader stays on the console. */
test.skipIf(!CHROME)('Enter on the skip link at the rail width lands focus on the workspace and keeps the console route', async () => {
  const state = await onTheConsole('daylight', async (client) => {
    await client.press('Tab', 'Tab', 9);
    await wait(80);
    const first = await client.evaluate(FOCUS_STATE);
    await client.press('Enter', 'Enter', 13, '\r');
    await wait(300);
    const after = await client.evaluate(`({id: document.activeElement && document.activeElement.id, hash: window.location.hash, stage: !!document.querySelector('.b42 h1')})`);
    return {first, after};
  });
  expect(state.first.tag).toBe('a.instrument-skip-link');
  expect(state.first.text).toBe('Skip to workspace');
  expect(state.after).toEqual({id: 'instrument-workspace', hash: '#/console', stage: true});
}, 180000);

/* Staging showed the rail's labels cut at the left edge ("oday", "sk",
   "iscover", "heme"). The body clips sideways overflow with overflow-x
   hidden, which still lets a script, a focus move or find in page scroll
   the viewport sideways, and the sticky rail went with it whenever a page
   was wider than the screen (#/coverage's table is). The rail's own theme
   select was also one pixel wider than the rail once the rail's scrollbar
   showed, so the rail itself scrolled sideways. This loads the console with
   real scrollbars and More open, puts something wider than the screen in
   the workspace, moves focus to its far end, and then measures every rail
   label's text against the viewport and every clipping box above it. */
const RAIL_LABELS_STATE = `(function(){
  var shown = function(node){ return !!node && getComputedStyle(node).display !== 'none' && node.getClientRects().length > 0; };
  var rail = document.querySelector('nav.rail42');
  var nodes = [].slice.call(rail.querySelectorAll('a.rail42-link, button.rail42-more-button, .rail42-theme label')).filter(shown);
  var labels = nodes.map(function(node){
    var range = document.createRange();
    range.selectNodeContents(node);
    var text = range.getBoundingClientRect();
    var left = 0, right = document.documentElement.clientWidth;
    for (var p = node.parentElement; p; p = p.parentElement){
      if (p === document.body || p === document.documentElement) continue;
      if (getComputedStyle(p).overflowX === 'visible') continue;
      var box = p.getBoundingClientRect();
      left = Math.max(left, box.left + p.clientLeft);
      right = Math.min(right, box.left + p.clientLeft + p.clientWidth);
    }
    return {label: node.textContent.trim(), textLeft: Math.round(text.left * 10) / 10, textRight: Math.round(text.right * 10) / 10, left: left, right: right, cut: node.scrollWidth > node.clientWidth};
  });
  var main = [].slice.call(document.querySelectorAll('nav')).filter(shown).filter(function(nav){
    var texts = [].slice.call(nav.querySelectorAll('a')).filter(shown).map(function(a){ return a.textContent.trim(); });
    return ['Today', 'Ask', 'Discover'].every(function(label){ return texts.indexOf(label) >= 0; });
  });
  return {
    scrollX: window.scrollX,
    railScroll: {left: rail.scrollLeft, width: rail.scrollWidth, client: rail.clientWidth},
    labels: labels,
    navigations: main.map(function(nav){ return nav.className; }),
  };
})()`;

for (const width of [1440, 1024]){
  test.skipIf(!CHROME)(`at ${width} every rail label shows whole, even when the page is wider than the screen, and one navigation holds Today, Discover and Compare`, async () => {
    const state = await onTheConsole('daylight', async (client) => {
      await client.evaluate(`(function(){
        document.querySelector('.rail42-more-button').click();
        var wide = document.createElement('div');
        wide.style.cssText = 'width: 2400px; height: 48px;';
        var far = document.createElement('button');
        far.type = 'button';
        far.textContent = 'Far end';
        far.style.cssText = 'margin-left: 2300px;';
        wide.appendChild(far);
        document.getElementById('instrument-workspace').appendChild(wide);
        far.focus();
        window.scrollTo(400, window.scrollY);
        var rail = document.querySelector('nav.rail42');
        rail.scrollLeft = 400;
      })()`);
      await wait(300);
      return {...await client.evaluate(RAIL_LABELS_STATE), inner: await client.evaluate('innerWidth')};
    }, width, {scrollbars: true, height: 800});
    expect(state.inner).toBe(width);
    expect(state.navigations).toEqual(['rail42']);
    expect(state.labels.map((item) => item.label)).toEqual([...RAIL_TOP_LABELS, 'More', ...RAIL_MORE_LABELS, 'Theme']);
    expect(state.scrollX).toBe(0);
    expect(state.railScroll.left).toBe(0);
    expect(state.railScroll.width).toBeLessThanOrEqual(state.railScroll.client);
    for (const item of state.labels){
      expect(item.textLeft, `${item.label} starts at ${item.textLeft}`).toBeGreaterThanOrEqual(Math.max(0, item.left));
      expect(item.textRight, `${item.label} ends at ${item.textRight} past ${item.right}`).toBeLessThanOrEqual(item.right);
      expect(item.cut, `${item.label} is wider than its box`).toBe(false);
    }
  }, 180000);
}
