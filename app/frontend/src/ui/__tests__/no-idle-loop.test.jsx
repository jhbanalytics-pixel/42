/* The motion law forbids the idle loop. Nothing in the host stylesheet may
   animate forever, and the loading mark is a static 42 lockup in one colour
   rather than a five-bar equaliser reading in four accent colours. */
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import React from 'react';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {PASS_KEY, clearCache} = await import('../../api.js');
const {default: App} = await import('../../App.jsx');

const appCss = readFileSync(fileURLToPath(new URL('../../app.css', import.meta.url)), 'utf8');
const root = postcss.parse(appCss);
const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let appRoot = null;
let priorHash = '';

const reply = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

function serve(deskResponse = reply(404, {})){
  globalThis.fetch = async (url, init = {}) => {
    calls.push({url: String(url), init});
    if (String(url) === '/api/health'){
      return reply(200, {ok: true, service: 'f42-api', auth_mode: 'passcode', passcode: true, checks: {auth: 'ok'}});
    }
    if (String(url) === '/api/auth/verify') return reply(200, {ok: true});
    if (String(url).startsWith('/api/desk')) return deskResponse;
    return reply(404, {error: 'not_found'});
  };
}

function mount(){
  host = document.createElement('div');
  document.body.appendChild(host);
  appRoot = createRoot(host);
  flushSync(() => appRoot.render(<App />));
}

async function settle(){
  for (let i = 0; i < 16; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

const text = () => host.textContent.replace(/\s+/g, ' ').trim();
const deskCalls = () => calls.filter((call) => call.url.startsWith('/api/desk'));

beforeEach(() => {
  calls = [];
  priorHash = window.location.hash;
  localStorage.removeItem(PASS_KEY);
  localStorage.removeItem('pulse-checked-at');
  window.location.hash = '#/method';
  clearCache();
});

afterEach(() => {
  if (appRoot) flushSync(() => appRoot.unmount());
  appRoot = null;
  if (host) host.remove();
  host = null;
  window.location.hash = priorHash;
  globalThis.fetch = realFetch;
  localStorage.removeItem(PASS_KEY);
  localStorage.removeItem('pulse-checked-at');
  clearCache();
});

afterAll(() => { GlobalRegistrator.unregister(); });

function declarations(matcher){
  const found = [];
  root.walkDecls((decl) => {
    if (matcher(decl)) found.push({selector: decl.parent.selector || decl.parent.name, prop: decl.prop, value: decl.value});
  });
  return found;
}

test('no rule in app.css animates infinitely', () => {
  const infinite = declarations((decl) => (
    /^animation(-iteration-count)?$/.test(decl.prop) && /\binfinite\b/.test(decl.value)
  ));
  expect(infinite).toEqual([]);
});

test('the equaliser and its keyframes are gone', () => {
  const bars = [];
  root.walkRules((rule) => { if (/\.bars\b/.test(rule.selector)) bars.push(rule.selector); });
  const eq = [];
  root.walkAtRules('keyframes', (rule) => { if (rule.params === 'eq' || rule.params === 'ring') eq.push(rule.params); });
  expect(bars).toEqual([]);
  expect(eq).toEqual([]);
});

/* Round 4, task 23. The chunk wait shimmered a skeleton card and the Network
   read pulsed a loader label for the length of the request. Both now render
   the package loading frame with a static reserved band, and the workspace
   sheet's reduced-motion block zeroes its durations outright rather than
   leaving a microsecond transition on every element. */
test('the Network read renders the package loading frame with a reserved band, not a pulsing loader', async () => {
  const React = (await import('react')).default;
  const {renderToStaticMarkup} = await import('react-dom/server');
  const {NetworkGraph} = await import('../../network.jsx');
  const markup = renderToStaticMarkup(React.createElement(NetworkGraph, {region: 'ZA', session: 0}));
  expect(markup).toContain('data-state="loading"');
  expect(markup).not.toContain('state-view__reserved');
  expect(markup).not.toContain('intel-wait');
  expect(markup).not.toContain('iw-lab');
  expect(markup).not.toContain('signal-mark');
});

test('the chunk wait is the package loading frame, not a skeleton', () => {
  const app = readFileSync(fileURLToPath(new URL('../../App.jsx', import.meta.url)), 'utf8');
  const routeWait = app.match(/function RouteWait\(\)\{[\s\S]*?\n\}/)[0];
  expect(routeWait).toContain('StateView');
  expect(routeWait).toContain('routeStateView(ROUTE_WAIT)');
  expect(routeWait).not.toContain('Skeleton');
  expect(app).toMatch(/const ROUTE_WAIT = Object\.freeze\(\{[\s\S]*?state: 'loading'[\s\S]*?reservedRows: 3/);
});

test('desk failure copy keeps the source status and does not animate', () => {
  const app = readFileSync(fileURLToPath(new URL('../../App.jsx', import.meta.url)), 'utf8');
  expect(app).not.toMatch(/DeskWait|desk-wait|dSkel|desk-err|Skeleton/);
  expect(app).toMatch(/desk\.state === 'error' && route !== 'pulse' && route !== 'browse' && !STANDALONE\.has\(route\) && <DeskError code=\{desk\.code\} message=\{desk\.message\} onRetry=\{retryDesk\} \/>/);
  const deskError = app.match(/function DeskError\(\{code, message, onRetry\}\)\{[\s\S]*?\n\}/)?.[0] || '';
  expect(deskError).toMatch(/\n\s+code,\n/);
  expect(deskError).toContain('routeStateView(');
  expect(deskError).toContain("state: 'error'");
  expect(deskError).toMatch(/actions: \[\{id: 'retry-desk', label: 'Try again', onClick: onRetry\}\]/);
  for (const sheet of ['styles/boot.css', 'app.css']){
    const css = readFileSync(fileURLToPath(new URL(`../../${sheet}`, import.meta.url)), 'utf8');
    expect(css, `${sheet} keeps a desk skeleton or desk error rule`).not.toMatch(/\.desk-wait|\.desk-err/);
  }
});

test('Method stays behind auth and renders without requesting the desk', async () => {
  serve();
  mount();
  await settle();

  const passcode = host.querySelector('input[type="password"]');
  expect(passcode).not.toBeNull();
  /* Shell consistency, 2 October 2026: Method is titled with the menu's noun,
     and its old headline now leads the sentence under it. */
  expect(text()).not.toContain('How 42 reads the signal');
  expect(deskCalls()).toHaveLength(0);

  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(passcode, 'synthetic-fixture-passcode');
  flushSync(() => passcode.dispatchEvent(new Event('input', {bubbles: true})));
  flushSync(() => passcode.closest('form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settle();

  let methodHeading = null;
  for (let i = 0; i < 100 && !methodHeading; i++){
    methodHeading = [...host.querySelectorAll('h1, h2, h3, h4, h5, h6')]
      .find((heading) => heading.textContent.trim() === 'Method') || null;
    if (!methodHeading) await new Promise((resolve) => setTimeout(resolve, 10));
  }

  expect(methodHeading?.textContent).toBe('Method');
  expect(text()).toContain('How 42 reads the signal');
  expect(deskCalls()).toHaveLength(0);
  expect(calls.filter((call) => call.url === '/api/auth/verify')).toHaveLength(1);
});

/* Page port, 3 October 2026: Browse read the desk, which f42-api does not
   serve, so its link now opens Discover, which reads its own data. */
test('the Browse link opens Discover when the active API has no desk route', async () => {
  window.location.hash = '#/browse';
  localStorage.setItem(PASS_KEY, 'synthetic-fixture-passcode');
  serve(reply(404, {error: 'not_found', message: 'No such API route.'}));
  mount();
  await settle();

  expect(window.location.hash).toBe('#/explore');
  expect(calls.some((call) => call.url.startsWith('/api/discover?'))).toBe(true);
  expect(text()).not.toContain('The desk could not load');
  expect(text()).not.toContain('completed_observation');
  expect(deskCalls()).toHaveLength(0);
});

test('Pulse reads its own data with the passcode and skips the legacy desk', async () => {
  const passcode = 'synthetic-fixture-passcode';
  window.location.hash = '#/pulse';
  localStorage.setItem(PASS_KEY, passcode);
  serve();
  mount();
  await settle();

  const ownReads = calls.filter((call) => call.url.startsWith('/api/today'));
  expect(ownReads).toHaveLength(1);
  expect(ownReads[0].init.headers?.['X-Passcode']).toBe(passcode);
  expect(deskCalls()).toHaveLength(0);
});

test('Discover reads its own data with the passcode and skips the legacy desk', async () => {
  const passcode = 'synthetic-fixture-passcode';
  window.location.hash = '#/explore';
  localStorage.setItem(PASS_KEY, passcode);
  serve();
  mount();
  await settle();

  const ownReads = calls.filter((call) => call.url.startsWith('/api/discover?'));
  expect(ownReads).toHaveLength(1);
  expect(ownReads[0].init.headers?.['X-Passcode']).toBe(passcode);
  expect(deskCalls()).toHaveLength(0);
});

for (const [route, ownRead] of [['pulse', '/api/today'], ['explore', '/api/discover?']]){
  test(`${route} drops the old desk age after its own read takes over`, async () => {
    const passcode = 'synthetic-fixture-passcode';
    const oldDeskAge = 40 * 24 * 60 * 60 * 1000;
    /* Page port, 3 October 2026: no route reads the desk now (Browse's
       link opens Discover), so a stored desk age is never shown at all,
       on the way in or after the move. */
    window.location.hash = '#/browse';
    localStorage.setItem(PASS_KEY, passcode);
    localStorage.setItem('pulse-checked-at', String(Date.now() - oldDeskAge));
    serve(reply(200, {
      topics: [],
      lexicon: [],
      dynamic_discovery: {contract_version: 'desk_dynamic_signal_v2', status: 'no_discovery'},
      freshness: {status: 'amber', age_hours: 960},
    }));
    mount();
    await settle();

    expect(text()).not.toContain('40d ago');
    expect(deskCalls()).toHaveLength(0);

    window.location.hash = '#/' + route;
    window.dispatchEvent(new Event('hashchange'));
    await settle();

    const ownReads = calls.filter((call) => call.url.startsWith(ownRead));
    expect(ownReads).toHaveLength(1);
    expect(ownReads[0].init.headers?.['X-Passcode']).toBe(passcode);
    expect(text()).not.toContain('40d ago');
    expect(deskCalls()).toHaveLength(0);
    expect(calls.some((call) => String(call.init.method || 'GET').toUpperCase() === 'POST')).toBe(false);
  });
}

test('Coverage does not inherit a stored legacy desk age', async () => {
  window.location.hash = '#/coverage';
  localStorage.setItem(PASS_KEY, 'synthetic-fixture-passcode');
  localStorage.setItem('pulse-checked-at', String(Date.now() - 40 * 24 * 60 * 60 * 1000));
  serve();
  mount();
  await settle();

  expect(calls.some((call) => call.url.startsWith('/api/coverage'))).toBe(true);
  expect(text()).not.toContain('40d ago');
});

test('the workspace sheet zeroes its durations under reduced motion, no microsecond transitions', () => {
  const workspaces = postcss.parse(readFileSync(fileURLToPath(new URL('../../styles/workspaces.css', import.meta.url)), 'utf8'));
  const findings = [];
  workspaces.walkAtRules('media', (media) => {
    if (!/prefers-reduced-motion/.test(media.params)) return;
    media.walkDecls(/^(?:transition|animation)-duration$/, (declaration) => {
      if (!/^0m?s$/.test(declaration.value.trim())) findings.push(`${declaration.parent.selector}: ${declaration.prop}: ${declaration.value}`);
    });
  });
  expect(findings).toEqual([]);
});

test('the loading mark is a static 42 lockup in ink', () => {
  const marks = [];
  root.walkRules((rule) => { if (rule.selector === '.signal-mark') marks.push(rule); });
  expect(marks.length).toBe(1);
  const decls = Object.fromEntries(marks[0].nodes.filter((n) => n.type === 'decl').map((d) => [d.prop, d.value]));
  expect(decls.color).toBe('var(--ink)');
  expect(decls.animation).toBeUndefined();
  expect(decls.transition).toBeUndefined();
  expect(decls.background).toBeUndefined();
});
