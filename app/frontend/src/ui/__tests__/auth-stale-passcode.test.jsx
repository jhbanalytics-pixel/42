/* N40-B, the browser half (C5 v2 section 13): what the page does with a
   stored passcode the server no longer accepts. The tests C01 to C15 follow
   the contract's table; R1 pins one race the contract does not name.

   Fixture FX-AUTH: a fetch mock that records every call with its headers,
   /api/health answering passcode mode, /api/auth/verify and the gated reads
   answering per test, and a deferred promise that decides when a verify
   resolves. Synthetic keys only. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const apiModule = await import('../../api.js');
const {PASS_KEY, clearCache, apiGet, apiGetFresh, apiPost, credential} = apiModule;
const {getJson, streamInvestigation} = await import('../../api42.js');
const {startAsk, streamAsk} = await import('../../askTransport42.js');
const {downloadResearchHtml} = await import('../../researchLib.jsx');
const {AnswerExport} = await import('../AnswerExport.jsx');
const {default: App} = await import('../../App.jsx');

const REASON_KEY = 'pulse_auth_reason';
const STALE_KEY = 'synthetic-stale-fixture-key';
const GOOD_KEY = 'synthetic-good-fixture-key';
const STALE_COPY = 'Your saved access key is no longer valid. Enter the current key.';
const WRONG_COPY = 'Access not recognised. Check the key and try again.';
const CHECK_COPY = 'Sign-in could not be checked just now. Try again shortly.';
const NETWORK_RATE_COPY = 'Too many attempts from this network. Wait a minute, then try again.';
const SERVER_RATE_COPY = 'Too many passcode attempts. Wait a minute and try again.';
const SAVE_COPY = 'This browser would not save the key, so you will be asked again after a reload.';

const realFetch = globalThis.fetch;
const realStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;
let calls = [];
let host = null;
let root = null;
let extraRoots = [];
let timers = new Map();

const json = (status, body, headers = {}) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json', ...headers}});
const unauthorized = () => json(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'});
const healthPasscode = () => json(200, {
  ok: true, service: 'f42-api', version: 'fixture', time: '2026-10-09T10:00:00+02:00',
  checks: {bigquery: 'ok', agent: 'ok', today: 'ok', auth: 'ok'}, passcode: true, auth_mode: 'passcode',
});
const deskFixture = {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}, dynamic_discovery: {contract_version: 'desk_dynamic_signal_v2', status: 'no_discovery'}};
const never = () => new Promise(() => {});

/* Every call that is not the health probe or the verify is a gated read. */
const verifyCalls = () => calls.filter((call) => call.url === '/api/auth/verify');
const gatedCalls = () => calls.filter((call) => call.url !== '/api/health' && call.url !== '/api/auth/verify');

function serve({verify = () => json(200, {ok: true}), gated = () => json(200, deskFixture), health = healthPasscode} = {}){
  globalThis.fetch = async (url, init = {}) => {
    const path = String(url);
    calls.push({url: path, init});
    if (path === '/api/health') return health();
    if (path === '/api/auth/verify') return verify(init);
    return gated(path, init);
  };
}

function deferred(){
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

function mount(){
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  flushSync(() => root.render(<App />));
}

function unmount(){
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
}

async function settle(){
  for (let i = 0; i < 16; i++) await new Promise((resolve) => realSetTimeout(resolve, 0));
}

/* A page's code is loaded on demand, so a read it starts can follow the verify
   by more than a few ticks on a busy machine. */
async function until(check, limit = 6000){
  const start = Date.now();
  while (!check() && Date.now() - start < limit) await new Promise((resolve) => realSetTimeout(resolve, 10));
  await settle();
}

/* Stands in for advancing fake timers by ten seconds: every timer the page
   has pending is run once, and the test then looks for new requests. */
function runPendingTimers(){
  const pending = [...timers.values()];
  timers.clear();
  for (const {fn} of pending){ try { fn(); } catch (_error) { /* a timer that throws is not under test */ } }
}

const text = () => host.textContent.replace(/\s+/g, ' ').trim();
const passcodeInput = () => host.querySelector('input[type="password"]');
const alertText = () => (host.querySelector('[role="alert"]') || {textContent: ''}).textContent.replace(/\s+/g, ' ').trim();
const button = (label) => [...host.querySelectorAll('button')].find((node) => node.textContent.replace(/\s+/g, ' ').trim().startsWith(label)) || null;

function type(value){
  const input = passcodeInput();
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
  flushSync(() => input.dispatchEvent(new Event('input', {bubbles: true})));
}

async function submit(value){
  type(value);
  flushSync(() => passcodeInput().closest('form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settle();
}

async function press(label){
  flushSync(() => button(label).dispatchEvent(new MouseEvent('click', {bubbles: true})));
  await settle();
}

beforeEach(() => {
  calls = [];
  if (credential) credential.end();
  timers = new Map();
  globalThis.setTimeout = (fn, delay, ...args) => {
    const id = realSetTimeout(() => { timers.delete(id); fn(...args); }, delay);
    timers.set(id, {fn: () => fn(...args), delay});
    return id;
  };
  globalThis.clearTimeout = (id) => { timers.delete(id); realClearTimeout(id); };
  localStorage.clear();
  window.location.hash = '#/coverage';
  clearCache();
});

afterEach(() => {
  if (credential) credential.end();
  Object.defineProperty(globalThis, 'localStorage', realStorage);
  unmount();
  for (const [node, mountedRoot] of extraRoots){ flushSync(() => mountedRoot.unmount()); node.remove(); }
  extraRoots = [];
  localStorage.clear();
  globalThis.fetch = realFetch;
  globalThis.setTimeout = realSetTimeout;
  globalThis.clearTimeout = realClearTimeout;
  clearCache();
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

test('C01 a stale stored key: one verify, no gated read, the key removed, the stale copy, and nothing on a reload', async () => {
  localStorage.setItem(PASS_KEY, STALE_KEY);
  serve({verify: unauthorized});
  mount();
  await settle();

  expect(verifyCalls()).toHaveLength(1);
  expect(JSON.parse(verifyCalls()[0].init.body)).toEqual({passcode: STALE_KEY});
  expect(gatedCalls()).toHaveLength(0);
  expect(localStorage.getItem(PASS_KEY)).toBeNull();
  expect(localStorage.getItem(REASON_KEY)).toBe('stale');
  expect(passcodeInput()).not.toBeNull();
  expect(text()).toContain(STALE_COPY);

  unmount();
  calls = [];
  clearCache();
  mount();
  await settle();
  expect(verifyCalls()).toHaveLength(0);
  expect(gatedCalls()).toHaveLength(0);
  expect(passcodeInput()).not.toBeNull();
});

test('C02 a valid key: no gated read before the verify answers, and every one after it carries the key', async () => {
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  const gate = deferred();
  serve({verify: () => gate.promise});
  mount();
  await settle();

  expect(verifyCalls()).toHaveLength(1);
  expect(gatedCalls()).toHaveLength(0);
  expect(passcodeInput()).toBeNull();

  gate.resolve(json(200, {ok: true}));
  await until(() => gatedCalls().length > 0);

  expect(gatedCalls().length).toBeGreaterThan(0);
  for (const call of gatedCalls()) expect(call.init.headers['X-Passcode']).toBe(GOOD_KEY);
  expect(calls.indexOf(gatedCalls()[0])).toBeGreaterThan(calls.indexOf(verifyCalls()[0]));
  expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
});

test('C03 a valid key on Today: one verify, then the four gated reads once each', async () => {
  window.location.hash = '#/';
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  serve({gated: () => never()});
  mount();
  const count = (prefix) => gatedCalls().filter((call) => call.url.startsWith(prefix)).length;
  await until(() => count('/api/alerts') && count('/api/investigations') && count('/api/schedules') && count('/api/today'));

  expect(verifyCalls()).toHaveLength(1);
  expect(count('/api/alerts')).toBe(1);
  expect(count('/api/investigations')).toBe(1);
  expect(count('/api/schedules')).toBe(1);
  expect(count('/api/today')).toBe(1);
  const firstGated = calls.indexOf(gatedCalls()[0]);
  expect(firstGated).toBeGreaterThan(calls.indexOf(verifyCalls()[0]));
});

test('C04 verify 429: the key is kept, no gated read, the rate-limit copy, no automatic retry, one verify per press of Try again', async () => {
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  let answers = 0;
  const second = deferred();
  serve({verify: () => {
    answers += 1;
    return answers === 1 ? json(429, {error: 'rate_limited', message: SERVER_RATE_COPY}) : second.promise;
  }});
  mount();
  await settle();

  expect(verifyCalls()).toHaveLength(1);
  expect(gatedCalls()).toHaveLength(0);
  expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
  expect(text()).toContain(SERVER_RATE_COPY);
  expect(text()).not.toContain(WRONG_COPY);

  runPendingTimers();
  await settle();
  expect(verifyCalls()).toHaveLength(1);

  await press('Try again');
  expect(verifyCalls()).toHaveLength(2);
  expect(button('Try again').disabled).toBe(true);
  await press('Try again');
  expect(verifyCalls()).toHaveLength(2);

  second.resolve(json(200, {ok: true}));
  await until(() => gatedCalls().length > 0);
  expect(gatedCalls().length).toBeGreaterThan(0);
  expect(passcodeInput()).toBeNull();
});

test('C04 a 429 that is not the limiter words gets the network copy', async () => {
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  serve({verify: () => json(429, {detail: 'slow down'})});
  mount();
  await settle();
  expect(text()).toContain(NETWORK_RATE_COPY);
  expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
  expect(gatedCalls()).toHaveLength(0);
});

test('C05 verify 503, 500, a non-JSON body and a rejected fetch: key kept, could-not-check copy, no gated read', async () => {
  const cases = [
    () => json(503, {error: 'gate_not_configured', message: 'The sign-in gate is not configured.'}),
    () => json(500, {error: 'internal', message: 'Something broke.'}),
    () => new Response('<html>proxy page</html>', {status: 502, headers: {'Content-Type': 'text/html'}}),
    () => new Response('<html>captive portal</html>', {status: 200, headers: {'Content-Type': 'text/html'}}),
    () => { throw new TypeError('synthetic network failure'); },
  ];
  for (const verify of cases){
    calls = [];
    localStorage.setItem(PASS_KEY, GOOD_KEY);
    serve({verify});
    mount();
    await settle();
    expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
    expect(text()).toContain(CHECK_COPY);
    expect(text()).not.toContain(WRONG_COPY);
    expect(gatedCalls()).toHaveLength(0);
    expect(verifyCalls()).toHaveLength(1);
    unmount();
    clearCache();
  }
});

test('C06 a genuine 401 after sign-in removes the key, shows the gate, and every site then refuses to fetch', async () => {
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  serve({gated: () => unauthorized()});
  mount();
  await until(() => localStorage.getItem(PASS_KEY) === null);

  expect(verifyCalls()).toHaveLength(1);
  expect(localStorage.getItem(PASS_KEY)).toBeNull();
  expect(passcodeInput()).not.toBeNull();
  const before = calls.length;

  const sites = {
    apiGet: () => apiGet('/api/after-1'),
    apiGetFresh: () => apiGetFresh('/api/after-2'),
    apiPost: () => apiPost('/api/after-3', {}),
    api42: () => getJson('/api/after-4'),
    streamInvestigation: () => streamInvestigation('inv_after', () => {}),
    askTransport: () => startAsk({question: 'after', market: 'ZA'}),
    askStream: () => streamAsk('a_after_0001', () => {}),
    research: () => downloadResearchHtml('art_after_0001', {docJson: {title: 'x'}}),
  };
  for (const [name, call] of Object.entries(sites)){
    let error = null;
    try { await call(); } catch (caught) { error = caught; }
    expect(`${name}:${Boolean(error && error.auth)}`).toBe(`${name}:true`);
  }
  expect(calls.length).toBe(before);

  const exportHost = document.createElement('div');
  document.body.appendChild(exportHost);
  const exportRoot = createRoot(exportHost);
  extraRoots.push([exportHost, exportRoot]);
  flushSync(() => exportRoot.render(<AnswerExport requestId="req_after_0001" />));
  flushSync(() => [...exportHost.querySelectorAll('button')][0].dispatchEvent(new MouseEvent('click', {bubbles: true})));
  await settle();
  expect(calls.length).toBe(before);
});

test('C07 the Ask poll ends at its first 401 and adds no request later (green at the old code too, pinned)', async () => {
  let polls = 0;
  serve({gated: (path) => {
    if (path.endsWith('/events')) return json(500, {error: 'internal', message: 'no stream'});
    polls += 1;
    return unauthorized();
  }});
  let error = null;
  try { await streamAsk('a_poll_0001', () => {}, {pollMs: 50}); } catch (caught) { error = caught; }
  expect(error && error.auth).toBe(true);
  expect(polls).toBe(1);
  const settled = calls.length;
  runPendingTimers();
  await new Promise((resolve) => realSetTimeout(resolve, 200));
  expect(calls.length).toBe(settled);
});

test('C08 a storage event for a stale clear closes the desk and keeps the saved answers; a Log out event still removes them', async () => {
  for (const marker of ['stale', null]){
    calls = [];
    localStorage.clear();
    localStorage.setItem(PASS_KEY, GOOD_KEY);
    localStorage.setItem('pulse-briefs-ver', '2');
    localStorage.setItem('pulse-briefs', '[]');
    localStorage.setItem('pulse-chat', '{"threads":[]}');
    serve();
    mount();
    await settle();
    expect(passcodeInput()).toBeNull();
    const before = calls.length;

    if (marker) localStorage.setItem(REASON_KEY, marker);
    localStorage.removeItem(PASS_KEY);
    flushSync(() => window.dispatchEvent(new StorageEvent('storage', {key: PASS_KEY, oldValue: GOOD_KEY, newValue: null})));
    await settle();

    expect(passcodeInput()).not.toBeNull();
    expect(calls.length).toBe(before);
    if (marker){
      expect(localStorage.getItem('pulse-briefs')).toBe('[]');
      expect(localStorage.getItem('pulse-chat')).toBe('{"threads":[]}');
    } else {
      expect(localStorage.getItem('pulse-briefs')).toBeNull();
      expect(localStorage.getItem('pulse-chat')).toBeNull();
    }
    unmount();
    clearCache();
  }
});

test('C08 Log out removes any stale marker with the key', async () => {
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  localStorage.setItem(REASON_KEY, 'stale');
  serve();
  mount();
  await settle();
  const more = [...host.querySelectorAll('button')].find((node) => node.textContent.trim().startsWith('More'));
  if (more) flushSync(() => more.dispatchEvent(new MouseEvent('click', {bubbles: true})));
  flushSync(() => button('Log out').dispatchEvent(new MouseEvent('click', {bubbles: true})));
  await settle();
  expect(localStorage.getItem(PASS_KEY)).toBeNull();
  expect(localStorage.getItem(REASON_KEY)).toBeNull();
});

test('C09 two wrong submits in a row clear the field and change the alert both times', async () => {
  serve({verify: unauthorized});
  mount();
  await settle();

  await submit('synthetic-wrong-one');
  expect(passcodeInput().value).toBe('');
  const first = alertText();
  expect(first).toContain(WRONG_COPY);

  await submit('synthetic-wrong-two');
  expect(passcodeInput().value).toBe('');
  const second = alertText();
  expect(second).toContain(WRONG_COPY);
  expect(second).not.toBe(first);
});

test('C10 a 429, a 503 and a rejected fetch at the gate keep the field and never say the key was wrong', async () => {
  const cases = [
    [() => json(429, {error: 'rate_limited', message: SERVER_RATE_COPY}), SERVER_RATE_COPY],
    [() => json(503, {error: 'gate_not_configured', message: 'x'}), CHECK_COPY],
    [() => { throw new TypeError('synthetic network failure'); }, CHECK_COPY],
  ];
  for (const [verify, copy] of cases){
    serve({verify});
    mount();
    await settle();
    await submit('synthetic-typed-value');
    expect(passcodeInput().value).toBe('synthetic-typed-value');
    expect(text()).toContain(copy);
    expect(text()).not.toContain(WRONG_COPY);
    expect(localStorage.getItem(PASS_KEY)).toBeNull();
    unmount();
    calls = [];
  }
});

test('C11 a verified key that cannot be saved opens the desk, is sent from memory, and says the browser would not save it', async () => {
  serve();
  mount();
  await settle();
  const real = globalThis.localStorage;
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => real.getItem(key),
      removeItem: (key) => real.removeItem(key),
      setItem(key, value){
        if (key === PASS_KEY) throw new Error('storage blocked');
        return real.setItem(key, value);
      },
    },
  });
  await submit(GOOD_KEY);
  await until(() => gatedCalls().length > 0);

  expect(passcodeInput()).toBeNull();
  expect(text()).toContain(SAVE_COPY);
  expect(text()).not.toContain(WRONG_COPY);
  expect(gatedCalls().length).toBeGreaterThan(0);
  for (const call of gatedCalls()) expect(call.init.headers['X-Passcode']).toBe(GOOD_KEY);
  expect(localStorage.getItem(PASS_KEY)).toBeNull();
});

test('C13 nothing depends on a Retry-After header', async () => {
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  const answer = json(429, {error: 'rate_limited', message: SERVER_RATE_COPY});
  const headers = {get(){ throw new Error('the page read a header'); }};
  Object.defineProperty(answer, 'headers', {value: headers});
  serve({verify: () => answer});
  mount();
  await settle();
  expect(text()).toContain(SERVER_RATE_COPY);
  runPendingTimers();
  await settle();
  expect(verifyCalls()).toHaveLength(1);
});

test('C14 a 401 that is not the gate keeps the key and shows the page its own error', async () => {
  const bodies = [
    () => new Response('Unauthorized', {status: 401, headers: {'Content-Type': 'text/plain'}}),
    () => new Response('<html>iam</html>', {status: 401, headers: {'Content-Type': 'text/html'}}),
    () => json(401, {error: 'agent_unavailable', message: 'The agent did not answer.'}),
  ];
  for (const gated of bodies){
    calls = [];
    localStorage.setItem(PASS_KEY, GOOD_KEY);
    serve({gated});
    mount();
    await until(() => gatedCalls().length > 0);
    expect(verifyCalls()).toHaveLength(1);
    expect(gatedCalls().length).toBeGreaterThan(0);
    expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
    expect(passcodeInput()).toBeNull();
    expect(host.querySelector('h1')).not.toBeNull();
    unmount();
    clearCache();
  }
});

test('C15 a 401 delivered to a read whose page swallows errors still clears the key', async () => {
  window.location.hash = '#/';
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  serve({gated: (path) => (path.startsWith('/api/investigations') || path.startsWith('/api/schedules') ? unauthorized() : never())});
  mount();
  await until(() => localStorage.getItem(PASS_KEY) === null);

  expect(gatedCalls().some((call) => call.url.startsWith('/api/investigations'))).toBe(true);
  expect(localStorage.getItem(PASS_KEY)).toBeNull();
  expect(localStorage.getItem(REASON_KEY)).toBe('stale');
  expect(passcodeInput()).not.toBeNull();
});

test('R1 a late 401 for a key that has since been replaced does not remove the new key', async () => {
  credential.begin('passcode');
  localStorage.setItem(PASS_KEY, 'synthetic-old-key');
  serve({gated: () => {
    localStorage.setItem(PASS_KEY, 'synthetic-new-key');
    return unauthorized();
  }});
  let error = null;
  try { await apiGet('/api/late'); } catch (caught) { error = caught; }
  expect(error && error.auth).toBeFalsy();
  expect(localStorage.getItem(PASS_KEY)).toBe('synthetic-new-key');
  await apiPost('/api/next', {}).catch(() => {});
  expect(calls.at(-1).init.headers['X-Passcode']).toBe('synthetic-new-key');
});

test('C02 callers that ask while a verify is in flight share it', async () => {
  credential.begin('passcode');
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  const gate = deferred();
  serve({verify: () => gate.promise});
  const first = credential.verifyStored();
  const second = credential.verifyStored();
  gate.resolve(json(200, {ok: true}));
  expect(await first).toEqual({outcome: 'ok'});
  expect(await second).toEqual({outcome: 'ok'});
  expect(verifyCalls()).toHaveLength(1);
  await credential.verifyStored();
  expect(verifyCalls()).toHaveLength(2);
});

test('C14 at every site a 401 that is not the gate is no verdict on the key', async () => {
  credential.begin('passcode');
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  serve({gated: () => json(401, {error: 'agent_unavailable', message: 'The agent did not answer.'})});
  const sites = {
    apiGet: () => apiGet('/api/other-1'),
    apiGetFresh: () => apiGetFresh('/api/other-2'),
    apiPost: () => apiPost('/api/other-3', {}),
    api42: () => getJson('/api/other-4'),
    askTransport: () => startAsk({question: 'other', market: 'ZA'}),
    research: () => downloadResearchHtml('art_other_0001', {docJson: {title: 'x'}}),
  };
  for (const [name, call] of Object.entries(sites)){
    let error = null;
    try { await call(); } catch (caught) { error = caught; }
    expect(`${name}:${Boolean(error)}:${Boolean(error && error.auth)}`).toBe(`${name}:true:false`);
  }
  expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
  expect(localStorage.getItem(REASON_KEY)).toBeNull();
});

test('R2 the older wordings of the same refusal still name the gate, and a coded error that is not unauthorized does not', async () => {
  const named = [
    {detail: 'Passcode required'},
    {detail: {code: 'unauthorized'}},
  ];
  const unnamed = [
    {error: 'unauthorised', message: 'Passcode needed.'},
    {error: 'agent_unavailable', detail: 'Passcode required'},
    {error: 'agent_unavailable', message: 'Passcode required.'},
    {message: 'Passcode required.'},
  ];
  for (const [bodies, expected] of [[named, true], [unnamed, false]]){
    for (const body of bodies){
      localStorage.setItem(PASS_KEY, GOOD_KEY);
      credential.begin('passcode');
      serve({gated: () => json(401, body)});
      let error = null;
      try { await apiGet('/api/older-' + JSON.stringify(body).length); } catch (caught) { error = caught; }
      expect(JSON.stringify(body) + Boolean(error && error.auth)).toBe(JSON.stringify(body) + String(expected));
      expect(JSON.stringify(body) + (localStorage.getItem(PASS_KEY) === null)).toBe(JSON.stringify(body) + String(expected));
    }
  }
});

test('C08 a desk closed by another tab sends nothing more from this one', async () => {
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  serve();
  mount();
  await settle();
  localStorage.setItem(REASON_KEY, 'stale');
  localStorage.removeItem(PASS_KEY);
  flushSync(() => window.dispatchEvent(new StorageEvent('storage', {key: PASS_KEY, oldValue: GOOD_KEY, newValue: null})));
  await settle();
  const before = calls.length;
  let error = null;
  try { await apiGet('/api/after-close'); } catch (caught) { error = caught; }
  expect(error && error.auth).toBe(true);
  expect(calls.length).toBe(before);
});

/* A storage that records every write and remove, in order, and can refuse the
   marker write the way a full browser store does. */
function watchStorage({refuseMarker = false} = {}){
  const real = globalThis.localStorage;
  const ops = [];
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => real.getItem(key),
      removeItem(key){ ops.push('remove:' + key); return real.removeItem(key); },
      setItem(key, value){
        ops.push('set:' + key);
        if (refuseMarker && key === REASON_KEY) throw new Error('storage full');
        return real.setItem(key, value);
      },
    },
  });
  return ops;
}

test('AM07 a rejected key writes the stale marker first and removes the key second', async () => {
  credential.begin('passcode');
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  const ops = watchStorage();
  credential.reject();
  expect(ops.filter((op) => op === 'set:' + REASON_KEY || op === 'remove:' + PASS_KEY)).toEqual(['set:' + REASON_KEY, 'remove:' + PASS_KEY]);
  expect(localStorage.getItem(PASS_KEY)).toBeNull();
  expect(localStorage.getItem(REASON_KEY)).toBe('stale');
});

test('AM07 when the marker cannot be written the key stays in storage and the rejection is held in memory', async () => {
  credential.begin('passcode');
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  const ops = watchStorage({refuseMarker: true});
  serve();
  credential.reject();
  expect(ops).not.toContain('remove:' + PASS_KEY);
  expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
  let error = null;
  try { await apiGet('/api/after-full-storage'); } catch (caught) { error = caught; }
  expect(error && error.auth).toBe(true);
  expect(calls.length).toBe(0);
});

test('AM07 a full browser store does not turn a stale key into a Log out for other tabs', async () => {
  localStorage.setItem(PASS_KEY, GOOD_KEY);
  watchStorage({refuseMarker: true});
  serve({gated: () => unauthorized()});
  mount();
  await until(() => passcodeInput() !== null);
  expect(passcodeInput()).not.toBeNull();
  expect(text()).toContain(STALE_COPY);
  expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
});

test('AM09 storing a verified key removes a lingering stale marker', async () => {
  credential.begin('passcode');
  localStorage.setItem(REASON_KEY, 'stale');
  expect(credential.store(GOOD_KEY)).toBe(true);
  expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
  expect(localStorage.getItem(REASON_KEY)).toBeNull();
});

test('AM09 signing in at the gate removes a marker an older tab left behind', async () => {
  localStorage.setItem(REASON_KEY, 'stale');
  serve();
  mount();
  await settle();
  expect(passcodeInput()).not.toBeNull();
  await submit(GOOD_KEY);
  await until(() => localStorage.getItem(PASS_KEY) === GOOD_KEY);
  expect(localStorage.getItem(PASS_KEY)).toBe(GOOD_KEY);
  expect(localStorage.getItem(REASON_KEY)).toBeNull();
});

test('C08 Log out in one tab shows a second tab the normal gate even when an old stale marker lingers', async () => {
  const saved = () => {
    localStorage.clear();
    localStorage.setItem(PASS_KEY, GOOD_KEY);
    localStorage.setItem(REASON_KEY, 'stale');
    localStorage.setItem('pulse-briefs-ver', '2');
    localStorage.setItem('pulse-briefs', '[]');
    localStorage.setItem('pulse-chat', '{"threads":[]}');
  };
  /* Tab A: the real Log out click, and the removals it makes, in order. */
  saved();
  const ops = watchStorage();
  serve();
  mount();
  await settle();
  const more = [...host.querySelectorAll('button')].find((node) => node.textContent.trim().startsWith('More'));
  if (more) flushSync(() => more.dispatchEvent(new MouseEvent('click', {bubbles: true})));
  flushSync(() => button('Log out').dispatchEvent(new MouseEvent('click', {bubbles: true})));
  await settle();
  const removals = ops.filter((op) => op.startsWith('remove:')).map((op) => op.slice('remove:'.length));
  expect(removals).toContain(PASS_KEY);
  expect(removals).toContain(REASON_KEY);
  unmount();
  credential.end();
  clearCache();
  Object.defineProperty(globalThis, 'localStorage', realStorage);

  /* Tab B: a fresh page signed in, which sees tab A's removals one at a time.
     A browser sends a storage event only when a removal changed something. */
  saved();
  calls = [];
  serve();
  mount();
  await settle();
  expect(passcodeInput()).toBeNull();
  for (const key of removals){
    const present = localStorage.getItem(key) !== null;
    localStorage.removeItem(key);
    if (!present) continue;
    flushSync(() => window.dispatchEvent(new StorageEvent('storage', {key, oldValue: 'x', newValue: null})));
  }
  await settle();
  expect(passcodeInput()).not.toBeNull();
  expect(text()).not.toContain(STALE_COPY);
  expect(localStorage.getItem('pulse-briefs')).toBeNull();
  expect(localStorage.getItem('pulse-chat')).toBeNull();
});
