import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {PASS_KEY, clearCache} = await import('../../api.js');
const {default: App} = await import('../../App.jsx');

const realFetch = globalThis.fetch;
const desk = {
  topics: [],
  lexicon: [],
  freshness: {status: 'green', age_hours: 1},
  dynamic_discovery: {contract_version: 'desk_dynamic_signal_v2', status: 'no_discovery'},
};
let calls = [];
let host = null;
let root = null;

const reply = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

function health(authMode, passcode, authCheck = 'ok'){
  const checks = {bigquery: 'ok', agent: 'ok', today: 'ok'};
  if (authMode !== undefined) checks.auth = authCheck;
  const body = {
    ok: true,
    service: 'f42-api',
    version: 'fixture',
    time: '2026-09-29T10:00:00+02:00',
    checks,
    passcode,
  };
  if (authMode !== undefined) body.auth_mode = authMode;
  return reply(200, body);
}

function serve({
  healthResponse,
  verifyResponse = reply(200, {ok: true}),
  deskResponse = reply(200, desk),
  healthError = null,
  verifyError = null,
}){
  globalThis.fetch = async (url, init = {}) => {
    calls.push({url: String(url), init});
    if (String(url) === '/api/health'){
      if (healthError) throw healthError;
      return healthResponse;
    }
    if (String(url) === '/api/auth/verify'){
      if (verifyError) throw verifyError;
      return verifyResponse;
    }
    if (String(url).startsWith('/api/coverage')) return deskResponse;
    return reply(404, {error: 'not_found'});
  };
}

function mount(){
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  flushSync(() => root.render(<App />));
}

async function settle(){
  for (let i = 0; i < 16; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

const text = () => host.textContent.replace(/\s+/g, ' ').trim();
const passcodeForm = () => host.querySelector('input[type="password"]');
const verifyCalls = () => calls.filter((call) => call.url === '/api/auth/verify');
const readCalls = () => calls.filter((call) => call.url.startsWith('/api/coverage'));

beforeEach(() => {
  calls = [];
  localStorage.removeItem(PASS_KEY);
  /* Demo polish, 2 October 2026: these checks use the legacy desk as the
     protected read, and only Browse still asks for it now that every 42
     page fetches its own data, so they open Browse instead of Map.
     Page port, 3 October 2026: Browse's link now opens Discover and no page
     reads the desk, so Coverage and its /api/coverage read stand in as the
     protected read. The gate checks are unchanged. */
  window.location.hash = '#/coverage';
  clearCache();
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  localStorage.removeItem(PASS_KEY);
  globalThis.fetch = realFetch;
  clearCache();
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

test('IAP read-only access opens without a passcode form or credential write', async () => {
  localStorage.setItem(PASS_KEY, 'synthetic-stored-fixture-passcode');
  serve({healthResponse: health('iap_readonly', false)});
  mount();
  await settle();

  expect(text()).toContain('Read-only access');
  expect(Boolean(passcodeForm())).toBe(false);
  expect(verifyCalls()).toHaveLength(1);
  expect(verifyCalls()[0].init.method).toBe('POST');
  expect(verifyCalls()[0].init.body).toBe('{}');
  expect(verifyCalls()[0].init.headers?.['X-Passcode']).toBeUndefined();
  expect(localStorage.getItem(PASS_KEY)).toBe('synthetic-stored-fixture-passcode');
});

test('the health probe keeps the passcode form hidden until passcode mode is known', async () => {
  let resolveHealth;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({url: String(url), init});
    if (String(url) === '/api/health'){
      return new Promise((resolve) => { resolveHealth = resolve; });
    }
    return reply(404, {error: 'not_found'});
  };
  mount();
  await settle();

  expect(typeof resolveHealth).toBe('function');
  expect(Boolean(passcodeForm())).toBe(false);
  resolveHealth(health('passcode', true));
  await settle();

  expect(passcodeForm()).not.toBeNull();
  expect(verifyCalls()).toHaveLength(0);
});

test('IAP waits for signed identity before showing the shell or reading protected data', async () => {
  let resolveVerify;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({url: String(url), init});
    if (String(url) === '/api/health') return health('iap_readonly', false);
    if (String(url) === '/api/auth/verify'){
      return new Promise((resolve) => { resolveVerify = resolve; });
    }
    if (String(url).startsWith('/api/coverage')) return reply(200, desk);
    return reply(404, {error: 'not_found'});
  };
  mount();
  await settle();

  expect(typeof resolveVerify).toBe('function');
  expect(Boolean(passcodeForm())).toBe(false);
  expect(host.querySelector('h1')).toBeNull();
  expect(readCalls()).toHaveLength(0);
  expect(verifyCalls()).toHaveLength(1);
  expect(verifyCalls()[0].init.body).toBe('{}');
  expect(verifyCalls()[0].init.headers?.['X-Passcode']).toBeUndefined();

  resolveVerify(reply(200, {ok: true}));
  await settle();

  expect(text()).toContain('Read-only access');
  expect(host.querySelector('h1')?.textContent).toStartWith('Coverage');
  expect(readCalls()).toHaveLength(1);
});

test('an IAP protected-read 401 shows fixed sign-in recovery without a passcode fallback', async () => {
  localStorage.setItem(PASS_KEY, 'synthetic-stored-fixture-passcode');
  serve({
    healthResponse: health('iap_readonly', false),
    deskResponse: reply(401, {error: 'unauthorized', message: 'Synthetic fixture detail'}),
  });
  mount();
  await settle();

  expect(host.querySelector('h1')?.textContent).toBe('Sign-in unavailable');
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('Sign-in could not be confirmed');
  expect(text()).toContain('Reload');
  expect(text()).not.toContain('Synthetic fixture detail');
  expect(Boolean(passcodeForm())).toBe(false);
  expect(verifyCalls()).toHaveLength(1);
  expect(verifyCalls()[0].init.body).toBe('{}');
  expect(verifyCalls()[0].init.headers?.['X-Passcode']).toBeUndefined();
  expect(localStorage.getItem(PASS_KEY)).toBe('synthetic-stored-fixture-passcode');
});

test('passcode mode keeps the absent and stored credential paths', async () => {
  serve({healthResponse: health('passcode', true)});
  mount();
  await settle();
  expect(passcodeForm()).not.toBeNull();
  const input = passcodeForm();
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'synthetic-new-passcode');
  flushSync(() => input.dispatchEvent(new Event('input', {bubbles: true})));
  flushSync(() => input.closest('form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settle();
  expect(verifyCalls()).toHaveLength(1);
  expect(JSON.parse(verifyCalls()[0].init.body)).toEqual({passcode: 'synthetic-new-passcode'});
  expect(localStorage.getItem(PASS_KEY)).toBe('synthetic-new-passcode');
  expect(Boolean(passcodeForm())).toBe(false);

  flushSync(() => root.unmount());
  root = null;
  host.remove();
  host = null;
  localStorage.setItem(PASS_KEY, 'synthetic-fixture-passcode');
  calls = [];
  clearCache();
  mount();
  await settle();

  expect(Boolean(passcodeForm())).toBe(false);
  expect(calls.some((call) => call.init.headers?.['X-Passcode'] === 'synthetic-fixture-passcode')).toBe(true);
});

test('legacy health without auth_mode keeps both passcode gate semantics', async () => {
  const legacyHealth = health(undefined, true);
  delete legacyHealth.ok;
  serve({healthResponse: legacyHealth});
  mount();
  await settle();

  expect(passcodeForm()).not.toBeNull();

  flushSync(() => root.unmount());
  root = null;
  host.remove();
  host = null;
  calls = [];
  clearCache();
  serve({healthResponse: health(undefined, false)});
  mount();
  await settle();

  expect(Boolean(passcodeForm())).toBe(false);
  expect(calls.some((call) => call.url.startsWith('/api/coverage'))).toBe(true);
});

test('unavailable modes and failed health or identity probes show reload and stay closed', async () => {
  const cases = [
    {healthResponse: health('unavailable', false, 'not_configured')},
    {healthResponse: health('future_mode', true)},
    {healthResponse: {...health('unavailable', false, 'not_configured'), ok: false, status: 503}},
    {healthError: new TypeError('synthetic network failure')},
    {healthResponse: health('iap_readonly', false), verifyResponse: reply(401, {error: 'unauthorized', message: 'Synthetic fixture detail'})},
  ];

  for (const fixture of cases){
    serve(fixture);
    mount();
    await settle();
    expect(host.querySelector('h1')?.textContent).toBe('Sign-in unavailable');
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Sign-in could not be confirmed');
    expect(text()).toContain('Reload');
    expect(Boolean(passcodeForm())).toBe(false);
    expect(readCalls()).toHaveLength(0);
    expect(verifyCalls().length).toBe(fixture.verifyResponse ? 1 : 0);
    if (fixture.verifyResponse) expect(text()).not.toContain('Synthetic fixture detail');
    flushSync(() => root.unmount());
    root = null;
    host.remove();
    host = null;
    calls = [];
    clearCache();
  }
});
