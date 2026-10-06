/* Colour contrast on the passcode gate, measured on the production build.

   The gate is the one route a first-time visitor sees before any of the
   product loads, and Lighthouse audits it as a page in its own right. This
   harness does what Lighthouse does, on the same artefact: it builds the app,
   serves the built files, opens the gate in headless Chrome under each theme,
   and reads the painted foreground and the composited background of every
   visible text node from the browser's own computed styles.

   The backgrounds are measured, not assumed. Lighthouse reported the gate's
   hero field as paper and its access column as ink, which is the opposite of
   what the source reads at a glance, so the harness walks each text node's
   ancestor chain and composites every translucent layer it finds until it
   reaches an opaque one. What it reports is what the pixel would be.

   Both themes are checked because the gate resolves its planes through the
   package palette, and a token whose meaning flips between midnight and
   daylight fails in exactly one of them. */
import {afterAll, expect, test} from 'bun:test';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {existsSync, mkdtempSync, readFileSync, rmSync, statSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {extname, join, normalize, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

function resolveChrome(){
  const candidates = [
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);
  return candidates.find((candidate) => existsSync(candidate)) || null;
}

const CHROME = resolveChrome();
const FRONTEND = fileURLToPath(new URL('../../../', import.meta.url));

/* One build serves every theme in the file, and it is removed again so the
   tests that follow do not run against a machine still writing a second copy
   of the bundle to disk. */
let builtRoot = null;
let builtDirectory = null;
afterAll(() => {
  if (builtRoot) rmSync(builtRoot, {recursive: true, force: true});
});
async function buildOnce(){
  if (builtDirectory) return builtDirectory;
  builtRoot = mkdtempSync(join(tmpdir(), 'lp-gate-contrast-build-'));
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

/* Runs inside the page. Keep it dependency free: it is injected as source.
   The probe waits for the surface's own root, then for the reads the surface
   makes on mount to settle, so a loading sentence and its replacement are
   not both measured. */
function probe(rootSelector){
  return `<script>
(async function(){
  var deadline = Date.now() + 10000;
  while (Date.now() < deadline && !document.querySelector(${JSON.stringify(rootSelector)})){
    await new Promise(function(r){ setTimeout(r, 50); });
  }
  await new Promise(function(r){ setTimeout(r, 600); });
  var canvas = document.createElement('canvas');
  canvas.width = 1; canvas.height = 1;
  var context = canvas.getContext('2d', {willReadFrequently: true});
  /* Chrome answers computed colours in several notations, color-mix() among
     them. The 2d context normalises all of them to premultiplied pixels, so
     one paint and one read gives the same value for every notation. */
  function parse(value){
    if (!value || value === 'transparent' || value === 'none') return {r: 0, g: 0, b: 0, a: 0};
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = '#000';
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    var data = context.getImageData(0, 0, 1, 1).data;
    var alpha = data[3] / 255;
    if (alpha === 0) return {r: 0, g: 0, b: 0, a: 0};
    return {r: data[0], g: data[1], b: data[2], a: alpha};
  }
  function over(front, back){
    var a = front.a;
    return {r: front.r * a + back.r * (1 - a), g: front.g * a + back.g * (1 - a), b: front.b * a + back.b * (1 - a), a: 1};
  }
  function luminance(colour){
    function channel(v){ v = v / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
    return 0.2126 * channel(colour.r) + 0.7152 * channel(colour.g) + 0.0722 * channel(colour.b);
  }
  function ratio(a, b){
    var la = luminance(a), lb = luminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  function hex(colour){
    return '#' + [colour.r, colour.g, colour.b].map(function(v){ return Math.round(v).toString(16).padStart(2, '0'); }).join('');
  }
  function describe(element){
    var tag = element.tagName.toLowerCase();
    if (element.id) return tag + '#' + element.id;
    var name = typeof element.className === 'string' ? element.className.trim() : '';
    return name ? tag + '.' + name.split(/\\s+/).join('.') : tag;
  }
  /* Composite every painted layer from the element outwards until an opaque
     one is reached, then paint that stack back down onto white. */
  function background(element){
    var stack = [];
    var node = element;
    while (node){
      var style = getComputedStyle(node);
      var colour = parse(style.backgroundColor);
      if (colour.a > 0) stack.push({colour: colour, from: describe(node)});
      if (colour.a === 1) break;
      node = node.parentElement;
    }
    var painted = {r: 255, g: 255, b: 255, a: 1};
    for (var i = stack.length - 1; i >= 0; i -= 1) painted = over(stack[i].colour, painted);
    return {colour: painted, chain: stack.map(function(layer){ return layer.from + ' ' + hex(layer.colour); })};
  }
  var rows = [];
  var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var node;
  while ((node = walker.nextNode())){
    if (!node.nodeValue || !node.nodeValue.trim()) continue;
    var element = node.parentElement;
    if (!element) continue;
    var style = getComputedStyle(element);
    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
    var box = element.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;
    var back = background(element);
    var foreground = parse(style.color);
    if (foreground.a < 1) foreground = over(foreground, back.colour);
    var size = parseFloat(style.fontSize);
    var weight = parseInt(style.fontWeight, 10) || 400;
    var need = 4.5;
    var measured = ratio(foreground, back.colour);
    rows.push({
      node: describe(element), text: node.nodeValue.trim().slice(0, 48),
      inside: !!element.closest(${JSON.stringify(rootSelector)}),
      foreground: hex(foreground), background: hex(back.colour), chain: back.chain,
      size: size, weight: weight, need: need,
      ratio: Math.round(measured * 100) / 100, pass: measured >= need,
    });
  }
  var result = document.createElement('pre');
  result.id = 'gate-contrast-result';
  result.textContent = btoa(unescape(encodeURIComponent(JSON.stringify({
    theme: document.documentElement.getAttribute('data-dir'), rows: rows,
  }))));
  document.body.appendChild(result);
})();
</script>`;
}

const TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.json': 'application/json'};

/* One built page, one theme, one route, measured in headless Chrome. The
   gate is held up by passcode: true from /api/health; the product routes are
   let through by passcode: false, and every other read answers from the
   api map or is refused, so a route renders its own unavailable sentences
   rather than a network error. */
async function measure({theme, hash = '', root: rootSelector, api, width = 1440, height = 1000}){
  const root = resolve(await buildOnce());
  const preload = `<script>localStorage.setItem('oi-theme', ${JSON.stringify(theme)});</script>`;
  const PROBE = probe(rootSelector);
  const server = createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (url.pathname.startsWith('/api/')){
      const answer = api[url.pathname];
      if (answer === undefined){
        response.writeHead(404, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
        response.end(JSON.stringify({error: 'not_found'}));
        return;
      }
      response.writeHead(200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
      response.end(JSON.stringify(answer));
      return;
    }
    const relative = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '');
    const path = normalize(join(root, relative));
    if (!path.startsWith(root)){ response.writeHead(403); response.end(); return; }
    try {
      if (!statSync(path).isFile()) throw new Error('not a file');
      let body = readFileSync(path);
      if (relative === 'index.html'){
        body = Buffer.from(String(body)
          .replace('<script type="module"', preload + '<script type="module"')
          .replace('</body>', PROBE + '</body>'));
      }
      response.writeHead(200, {'Content-Type': TYPES[extname(path)] || 'application/octet-stream', 'Cache-Control': 'no-store'});
      response.end(body);
    } catch { response.writeHead(404); response.end('not found'); }
  });
  await new Promise((done) => server.listen(0, '127.0.0.1', done));
  const port = server.address().port;
  const directory = mkdtempSync(join(tmpdir(), 'lp-gate-contrast-'));
  try {
    const run = await new Promise((done, fail) => {
      const child = spawn(CHROME, [
        '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
        '--hide-scrollbars', '--force-prefers-reduced-motion',
        `--window-size=${width},${height}`, '--virtual-time-budget=12000',
        `--user-data-dir=${join(directory, 'profile')}`,
        '--dump-dom', `http://127.0.0.1:${port}/${hash}`,
      ], {windowsHide: true});
      let stdout = '';
      let stderr = '';
      child.stdout.on('data', (chunk) => { stdout += chunk; });
      child.stderr.on('data', (chunk) => { stderr += chunk; });
      child.on('error', fail);
      child.on('close', () => done({stdout, stderr}));
      setTimeout(() => { try { child.kill(); } catch { /* already gone */ } }, 90000);
    });
    const encoded = run.stdout.match(/<pre id="gate-contrast-result">([^<]+)<\/pre>/)?.[1];
    if (!encoded) throw new Error(`the gate contrast probe produced no measurement block: ${run.stderr.slice(0, 400)}`);
    return JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
  } finally {
    server.close();
    rmSync(directory, {recursive: true, force: true});
  }
}

function report(rows){
  return rows.map((row) => `${row.ratio.toFixed(2)}:1 needs ${row.need}:1 | ${row.foreground} on ${row.background}`
    + ` | ${row.node} "${row.text}" (${row.size}px/${row.weight}) [${row.chain.join(' over ')}]`);
}

/* passcode: true is what holds the gate up instead of the product. */
const measureGate = (theme) => measure({theme, root: '.gate-v4-access', api: {'/api/health': {passcode: true}}});

for (const theme of ['midnight', 'daylight']){
  test.skipIf(!CHROME)(`the built gate meets WCAG AA on every text node in ${theme}`, async () => {
    const measured = await measureGate(theme);
    expect(measured.theme).toBe(theme);
    /* A gate that rendered nothing would pass an empty check, so hold the
       node count as well as the ratios. The gate carries the masthead, the
       hero, the three principles and the access column. */
    /* Restated, design audit 2 October 2026: the mast market codes and
       Internal tag (4 nodes), the footer word list (4 nodes) and the 01, 02,
       03 folios (3 nodes) were removed as false affordances and a template
       idiom, taking the gate from 30 text nodes to 19: mast 2, hero 4,
       principles 6, access 7. Was: toBeGreaterThanOrEqual(28). */
    expect(measured.rows.length).toBeGreaterThanOrEqual(19);
    expect(report(measured.rows.filter((row) => !row.pass))).toEqual([]);
  }, 180000);
}

/* The console route. The workbench is host chrome around the package
   surfaces: the topbar, the rail with its tabs, actions and recents, the flow
   strip with the market scope, and the landing with its two paths and the
   investigation index. The audit measured ninety-two failing text runs here,
   most of them faint or the plane red painted as text and one label painted
   in its own background colour. Every text node inside the workbench is held
   to AA in both themes and at both the desk and the phone width.

   The reads the landing makes on mount answer as an empty estate, so the
   recents rail, the investigation index and the flow strip all render their
   settled sentences rather than a loading line. */
const CONSOLE_API = {
  '/api/health': {passcode: false},
  '/api/research/recent': [],
};

/* The package's own skip link paints paper on the text red in midnight. That
   is audit item 1, a package alias, and it sits outside the workbench; it is
   named here so a second shell failure still fails. */
const OUTSIDE_THE_WORKBENCH = new Set(['a.instrument-skip-link']);

for (const theme of ['midnight', 'daylight']){
  for (const [label, width, height, atLeast] of [['desk', 1440, 1000, 24], ['phone', 390, 844, 20]]){
    test.skipIf(!CHROME)(`the console workbench meets WCAG AA on every text node in ${theme} at the ${label} width`, async () => {
      const measured = await measure({theme, hash: '#/console', root: '.workbench', api: CONSOLE_API, width, height});
      expect(measured.theme).toBe(theme);
      const inside = measured.rows.filter((row) => row.inside);
      /* The topbar, the rail head, two tabs, two actions, two rail sections
         with their sentences, the flow strip, the hero and two path cards.
         The phone width folds the rail into the mobile switch, so it carries
         fewer nodes; a page that rendered nothing would still fail. */
      expect(inside.length).toBeGreaterThanOrEqual(atLeast);
      expect(report(inside.filter((row) => !row.pass))).toEqual([]);
      const shell = measured.rows.filter((row) => !row.inside && !row.pass && !OUTSIDE_THE_WORKBENCH.has(row.node));
      expect(report(shell)).toEqual([]);
    }, 180000);
  }
}
