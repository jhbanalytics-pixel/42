import {expect, test} from 'bun:test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

import {DEFERRED_SELECTOR_ROOTS} from '../../instrumentStylesheetSplit.mjs';

/* The stylesheet split leaves the route surfaces to a deferred half that lands
   after first paint. Gate C waits for data-product-styles to read ready before
   it measures, so the window between first contentful paint and that moment
   was never observed by any gate. This test holds the deferred stylesheets
   back by a fixed delay, records both ends of the window from the marks the
   loader writes, and records what a deferred family looked like inside it. */

/* Every stylesheet other than the render-blocking entry is held for this long
   before Chrome is allowed to fetch it. The window is measured against a
   throttle rather than against the local server's speed, so the number below
   means the same thing on every machine. */
const DEFERRED_HOLD_MS = 1000;

/* Pinned from the first measured runs: five routes at 1280 wide measured a
   window of 903 to 1,023 milliseconds from first contentful paint to the ready
   mark over a 1,000 millisecond hold, with the deferral itself, loading mark to
   ready mark, at 1,009 to 1,041 milliseconds. The loader's own cost above the
   hold was under 60 milliseconds, and first paint lands after the deferral
   begins, which is why the window can read a little under the hold. The budget
   grants the hold plus 250 milliseconds, four times the largest overhead seen,
   so noise on a loaded machine passes and a loader that starts late, retries,
   or waits on something other than the stylesheets fails. */
const WINDOW_BUDGET_MS = DEFERRED_HOLD_MS + 250;

const WIDTH = 1280;
const ROUTES = [
  {surface: 'briefing', route: 'pulse'},
  {surface: 'discover', route: 'explore'},
  {surface: 'compare', route: 'compare'},
  {surface: 'build', route: 'console?work=brief&investigation=inv_gate_c&artifact=ra_gate_c'},
  {surface: 'fieldwork', route: 'fieldwork'},
];

function chromePath(){
  return [
    process.env.CHROME_PATH,
    process.env.CHROME_BIN,
    String.raw`C:\Program Files\Google\Chrome\Application\chrome.exe`,
    String.raw`C:\Program Files (x86)\Google\Chrome\Application\chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].find((candidate) => candidate && existsSync(candidate));
}

const wait = (milliseconds) => new Promise((resolveWait) => setTimeout(resolveWait, milliseconds));

async function chromePort(profile){
  for (let attempt = 0; attempt < 200; attempt += 1){
    try {
      const port = Number(readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split(/\r?\n/, 1)[0]);
      if (port > 0) return port;
    } catch {}
    await wait(50);
  }
  throw new Error('Paint window Chrome debugging port unavailable');
}

async function browserSocket(port){
  for (let attempt = 0; attempt < 200; attempt += 1){
    try {
      const version = await fetch(`http://127.0.0.1:${port}/json/version`).then((response) => response.json());
      if (version?.webSocketDebuggerUrl) return version.webSocketDebuggerUrl;
    } catch {}
    await wait(50);
  }
  throw new Error('Paint window Chrome browser endpoint unavailable');
}

/* The same flat-session client Gate C drives in instrument-geometry.test.jsx:
   the browser endpoint, a page target this harness owns, and commands routed
   by session id, so Chrome's own startup tab bookkeeping cannot close the
   target under the measurement. */
function cdpConnection(url){
  return new Promise((resolveConnection, rejectConnection) => {
    const socket = new WebSocket(url);
    const pending = new Map();
    const listeners = new Set();
    let id = 0;
    let failure = null;
    const failAll = (error) => {
      failure = error;
      for (const command of pending.values()) command.rejectCommand(error);
      pending.clear();
    };
    socket.addEventListener('open', () => resolveConnection({
      command(method, params = {}, sessionId){
        if (failure) return Promise.reject(failure);
        const commandId = ++id;
        return new Promise((resolveCommand, rejectCommand) => {
          pending.set(commandId, {resolveCommand, rejectCommand});
          socket.send(JSON.stringify({id: commandId, method, params, ...(sessionId ? {sessionId} : {})}));
        });
      },
      onEvent(listener){ listeners.add(listener); },
      fail(error){ failAll(error); },
      close(){ socket.close(); },
    }), {once: true});
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (!message.id){
        for (const listener of listeners) listener(message);
        return;
      }
      if (!pending.has(message.id)) return;
      const command = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) command.rejectCommand(new Error(message.error.message));
      else command.resolveCommand(message.result);
    });
    socket.addEventListener('error', () => {
      const error = new Error('Paint window Chrome socket failed');
      failAll(error);
      rejectConnection(error);
    }, {once: true});
    socket.addEventListener('close', () => failAll(new Error('Paint window Chrome socket closed before the routes finished')), {once: true});
  });
}

async function pageClient(port, url){
  const connection = await cdpConnection(await browserSocket(port));
  const {targetId} = await connection.command('Target.createTarget', {url});
  const {sessionId} = await connection.command('Target.attachToTarget', {targetId, flatten: true});
  connection.onEvent((message) => {
    if (message.method === 'Target.detachedFromTarget' && message.params?.sessionId === sessionId){
      connection.fail(new Error(`Paint window page target detached mid-run: ${message.params.reason || 'unknown reason'}`));
    }
    if (message.method === 'Inspector.targetCrashed' && message.sessionId === sessionId){
      connection.fail(new Error('Paint window page target crashed mid-run'));
    }
  });
  await connection.command('Inspector.enable', {}, sessionId);
  return {
    command(method, params = {}){ return connection.command(method, params, sessionId); },
    onEvent(listener){
      connection.onEvent((message) => { if (message.sessionId === sessionId) listener(message); });
    },
    async dispose(){
      try { await connection.command('Target.closeTarget', {targetId}); } catch {}
      try { await connection.command('Browser.close'); } catch {}
      connection.close();
    },
  };
}

/* The served index reloads itself once per tab to install the gate recorder,
   so the document that is measured is the one after that reload. */
async function settleDocument(client){
  const deadline = Date.now() + 30000;
  let lastState = 'no document';
  while (Date.now() < deadline){
    try {
      const settled = await client.command('Runtime.evaluate', {
        returnByValue: true,
        expression: `JSON.stringify({state: document.readyState, reloaded: sessionStorage.getItem('gate-c-hard-reloaded') === '1'})`,
      });
      const value = JSON.parse(settled.result?.value || '{}');
      lastState = `readyState ${value.state}, one-time reload ${value.reloaded ? 'done' : 'pending'}`;
      if (value.state === 'complete' && value.reloaded) return;
    } catch (error) {
      if (/detached|crashed|socket closed/i.test(error.message)) throw error;
      lastState = `evaluate rejected with ${error.message}`;
    }
    await wait(50);
  }
  throw new Error(`Paint window document never settled within 30s: ${lastState}`);
}

/* Installed before any page script runs on every document. It records each
   data-product-styles transition against the document clock, and while the
   attribute reads loading it samples every deferred family in the DOM: an
   element from a deferred family with a box on screen before the deferred
   half has landed is a route surface painting unstyled. */
function observerScript(){
  return `(() => {
    const roots = ${JSON.stringify(DEFERRED_SELECTOR_ROOTS)};
    const selector = roots.map((root) => '.' + root).join(',');
    const record = {transitions: [], unstyled: [], skipped: [], samples: 0};
    window.__paintWindow = record;
    const state = () => document.documentElement.getAttribute('data-product-styles');
    const settled = (value) => value === 'ready' || value === 'failed';
    const family = (element) => roots.find((root) => element.classList.contains(root)) || element.className;
    /* A family counts as styled once any applied stylesheet declares a rule
       for it. The set is rebuilt whenever a sheet lands, so a surface whose
       sheet arrived ahead of the rest of the deferred set is not misread. */
    let styledCount = -1;
    let styled = new Set();
    const styledFamilies = () => {
      if (document.styleSheets.length === styledCount) return styled;
      styledCount = document.styleSheets.length;
      styled = new Set();
      for (const sheet of document.styleSheets){
        let rules = [];
        try { rules = sheet.cssRules; } catch { continue; }
        const walk = (list) => {
          for (const rule of list){
            if (rule.selectorText){
              for (const root of roots) if (rule.selectorText.includes('.' + root)) styled.add(root);
            }
            if (rule.cssRules) walk(rule.cssRules);
          }
        };
        walk(rules);
      }
      return styled;
    };
    /* A box is not a paint. The contents of a closed details are skipped:
       they never paint and never hit-test, but a rect query lays them out on
       demand and answers with a real size, so the rect on its own reads the
       ribbon data table as on screen while its disclosure is shut. The
       browser's own visibility answer separates the two. An unstyled surface
       still fails, because a surface with no rules is visible by default; the
       skipped ones are kept rather than dropped so the window still records
       what was in the DOM behind a disclosure. */
    const painted = (element) => (
      typeof element.checkVisibility !== 'function'
        || element.checkVisibility({checkOpacity: false, checkVisibilityCSS: true, contentVisibilityAuto: true})
    );
    const sample = () => {
      record.samples += 1;
      const present = styledFamilies();
      for (const element of document.querySelectorAll(selector)){
        const name = family(element);
        if (present.has(name)) continue;
        const rect = element.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const entry = {at: performance.now(), family: name, width: rect.width, height: rect.height, sheets: styledCount};
        if (painted(element)) record.unstyled.push(entry);
        else record.skipped.push(entry);
      }
    };
    let last = null;
    const note = () => {
      const value = state();
      if (value === last) return;
      last = value;
      record.transitions.push({state: value, at: performance.now()});
    };
    /* This runs before the parser has produced the html element, so the
       attribute observer waits for it. */
    const attach = () => {
      const observer = new MutationObserver(note);
      observer.observe(document.documentElement, {attributes: true, attributeFilter: ['data-product-styles']});
      const timer = setInterval(() => {
        note();
        const value = state();
        if (settled(value)){
          clearInterval(timer);
          observer.disconnect();
          return;
        }
        if (value === 'loading') sample();
      }, 16);
    };
    if (document.documentElement){
      attach();
      return;
    }
    const rootWatch = new MutationObserver(() => {
      if (!document.documentElement) return;
      rootWatch.disconnect();
      attach();
    });
    rootWatch.observe(document, {childList: true});
  })();`;
}

function routeExpression(surface){
  return `(async()=>{
const wait=(ms)=>new Promise(r=>setTimeout(r,ms));
const state=()=>document.documentElement.getAttribute('data-product-styles');
const settled=(s)=>s==='ready'||s==='failed';
if(!settled(state())){
await new Promise((done)=>{
const observer=new MutationObserver(()=>{if(settled(state())){observer.disconnect();clearTimeout(timer);done()}});
observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-product-styles']});
const timer=setTimeout(()=>{observer.disconnect();done()},30000);
if(settled(state())){observer.disconnect();clearTimeout(timer);done()}
})}
await wait(50);
const paint=performance.getEntriesByType('paint').find((entry)=>entry.name==='first-contentful-paint');
const mark=(name)=>{const entries=performance.getEntriesByName(name,'mark');return entries.length?entries[0].startTime:null};
const record=window.__paintWindow||{transitions:[],unstyled:[],skipped:[],samples:0};
return{surface:${JSON.stringify(surface)},state:state(),firstContentfulPaint:paint?paint.startTime:null,loadingMark:mark('product-styles:loading'),readyMark:mark('product-styles:ready'),transitions:record.transitions,unstyled:record.unstyled,skipped:record.skipped,samples:record.samples,styleSheets:document.styleSheets.length}})()`;
}

async function measuredRoutes(){
  const chrome = chromePath();
  if (!chrome) throw new Error('Chrome executable is required for the paint window');
  const root = mkdtempSync(join(tmpdir(), 'lp-instrument-paint-window-'));
  const frontendRoot = fileURLToPath(new URL('../../../', import.meta.url));
  const repositoryRoot = fileURLToPath(new URL('../../../../', import.meta.url));
  const serverScript = join(frontendRoot, 'scripts', 'gate-c-local-server.mjs');
  const productionRoot = join(repositoryRoot, 'web', 'dist');
  const index = readFileSync(join(productionRoot, 'index.html'), 'utf8');
  const entryHref = index.match(/href="([^"]+\.css)"/)?.[1];
  if (!entryHref) throw new Error('The production build links no entry stylesheet');
  /* A build older than the split would measure a previous loader. The entry
     chunk of a current build names the route surfaces stylesheet in its
     dependency table. */
  const entryScript = index.match(/<script[^>]+src="([^"]+\.js)"/)?.[1];
  if (!entryScript) throw new Error('The production build links no entry chunk');
  if (!readFileSync(join(productionRoot, entryScript.replace(/^\//, '')), 'utf8').includes('instrument-route-surfaces')){
    throw new Error('web/dist is a build from before the stylesheet split; run bun run build');
  }
  const port = 33000 + (process.pid % 1000);
  const server = spawn(process.execPath, [serverScript, productionRoot, String(port)], {
    cwd: repositoryRoot, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  const serverErrors = [];
  server.stderr.on('data', (chunk) => serverErrors.push(String(chunk)));
  const profile = join(root, 'chrome-profile');
  const chromeProcess = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
  ], {stdio: 'ignore', windowsHide: true});
  let client;
  const held = [];
  try {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline){
      if (server.exitCode !== null) throw new Error(`Paint window server exited ${server.exitCode}: ${serverErrors.join('')}`);
      try { if ((await fetch(`http://127.0.0.1:${port}/`)).ok) break; } catch {}
      await wait(50);
    }
    client = await pageClient(await chromePort(profile), 'about:blank');
    await client.command('Page.enable');
    await client.command('Page.addScriptToEvaluateOnNewDocument', {source: observerScript()});
    /* Every stylesheet apart from the render-blocking entry is paused at the
       request stage and released after the hold. The entry sheet goes through
       untouched, so first paint is styled by the critical half as in
       production and only the deferred half is late. */
    client.onEvent((message) => {
      if (message.method !== 'Fetch.requestPaused') return;
      const {requestId, request} = message.params;
      const release = () => client.command('Fetch.continueRequest', {requestId}).catch(() => {});
      if (new URL(request.url).pathname === entryHref){
        release();
        return;
      }
      held.push(request.url);
      setTimeout(release, DEFERRED_HOLD_MS);
    });
    await client.command('Fetch.enable', {patterns: [{urlPattern: '*.css', requestStage: 'Request'}]});
    await client.command('Emulation.setDeviceMetricsOverride', {width: WIDTH, height: 1600, deviceScaleFactor: 1, mobile: false});
    await client.command('Page.navigate', {url: `http://127.0.0.1:${port}/`});
    await settleDocument(client);
    await client.command('Runtime.evaluate', {expression: `localStorage.setItem('pulse_passcode','gate-c-local-only')`});
    const results = [];
    for (const {surface, route} of ROUTES){
      await client.command('Page.navigate', {url: `http://127.0.0.1:${port}/?paint-window=${surface}#/${route}`});
      await settleDocument(client);
      const evaluated = await client.command('Runtime.evaluate', {
        awaitPromise: true, returnByValue: true, expression: routeExpression(surface),
      });
      if (evaluated.exceptionDetails) throw new Error(evaluated.exceptionDetails.exception?.description || evaluated.exceptionDetails.text);
      results.push(evaluated.result.value);
    }
    return {results, held};
  } finally {
    if (client) await client.dispose();
    if (chromeProcess.exitCode === null){ chromeProcess.kill(); await Promise.race([once(chromeProcess, 'exit'), wait(5000)]); }
    if (server.exitCode === null) server.kill();
    /* Chrome releases its profile a moment after it exits, so the removal is
       retried rather than reported as the test's own failure. */
    for (let attempt = 0; attempt < 20 && existsSync(root); attempt += 1){
      try { rmSync(root, {recursive: true, force: true}); } catch { await wait(250); }
    }
  }
}

/* bun run test does not build web/dist. Without a build there is nothing to
   measure, and the reason is named rather than failing on a missing file. */
const DIST_PRESENT = existsSync(join(fileURLToPath(new URL('../../../../', import.meta.url)), 'web', 'dist', 'index.html'));

test.skipIf(!DIST_PRESENT)('the window between first contentful paint and the deferred stylesheets landing is measured on every route and stays inside its budget (skipped when web/dist is absent: run bun run build first)', async () => {
  const {results, held} = await measuredRoutes();
  if (process.env.PAINT_WINDOW_RESULTS_PATH) writeFileSync(process.env.PAINT_WINDOW_RESULTS_PATH, `${JSON.stringify({held, results}, null, 2)}\n`, 'utf8');
  expect(results).toHaveLength(ROUTES.length);
  /* The hold has to have bitten, or the window measured is the local server's
     speed rather than the deferral. */
  expect(held.length, `held stylesheet requests: ${held.join(', ')}`).toBeGreaterThan(0);
  for (const result of results){
    const label = `${result.surface}: ${JSON.stringify({...result, unstyled: result.unstyled.slice(0, 5)})}`;
    expect(result.state, label).toBe('ready');
    expect(result.firstContentfulPaint, `${result.surface} first contentful paint`).not.toBeNull();
    /* The loader marks both ends of the window on the document clock, the
       moment it began deferring and the moment the deferred half applied, so
       the window is read from the loader rather than inferred from the DOM. */
    expect(result.loadingMark, `${result.surface} loading mark missing: ${label}`).not.toBeNull();
    expect(result.readyMark, `${result.surface} ready mark missing: ${label}`).not.toBeNull();
    expect(result.transitions.map((transition) => transition.state), `${result.surface} transitions`).toEqual(['loading', 'ready']);
    /* The deferral itself, loading mark to ready mark, has to carry the whole
       hold, or the throttle never reached the deferred set and the window
       below is the local server's speed rather than the deferral. */
    const deferral = result.readyMark - result.loadingMark;
    expect(deferral, `${result.surface} deferral ${deferral.toFixed(1)}ms is under the ${DEFERRED_HOLD_MS}ms hold`).toBeGreaterThanOrEqual(DEFERRED_HOLD_MS);
    const window = result.readyMark - result.firstContentfulPaint;
    expect(window, `${result.surface} window ${window.toFixed(1)}ms over budget ${WINDOW_BUDGET_MS}ms`).toBeLessThanOrEqual(WINDOW_BUDGET_MS);
    expect(result.samples, `${result.surface} samples inside the window`).toBeGreaterThan(0);
    /* A deferred family the browser was painting while the attribute read
       loading painted before its stylesheet existed. The skipped list rides
       along in the label: it holds the deferred elements that had a box but
       were behind a closed disclosure, which is not a fault and is worth
       seeing when one of these fails. */
    expect(
      result.unstyled,
      `${result.surface} route surfaces painted unstyled inside the window: ${JSON.stringify(result.unstyled.slice(0, 5))}; skipped behind a disclosure: ${JSON.stringify((result.skipped || []).slice(0, 3))}`,
    ).toEqual([]);
  }
}, 180000);
