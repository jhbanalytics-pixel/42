import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {PASS_KEY, clearCache} = await import('../../api.js');
const {ListenPage} = await import('../../listen.jsx');
const {NetworkGraph} = await import('../../network.jsx');
const {act} = React;
const previousFetch = globalThis.fetch;
const previousWindowFetch = window.fetch;
const previousSetTimeout = globalThis.setTimeout;
const previousClearTimeout = globalThis.clearTimeout;
const previousWindowSetTimeout = window.setTimeout;
const previousWindowClearTimeout = window.clearTimeout;
const previousActEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
let calls = [];
let fetchAnswer = null;
let host = null;
let root = null;
let clockRestore = null;

function response(status, body){
  return new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
}

function serve(answer){
  fetchAnswer = answer;
  const stub = async (url, init = {}) => {
    const call = {url: String(url), init};
    calls.push(call);
    return fetchAnswer(call, calls.length);
  };
  globalThis.fetch = stub;
  window.fetch = stub;
}

function controlDeadlines(){
  const deadlines = new Map();
  let nextId = 0;
  const fakeSetTimeout = (callback, delay, ...args) => {
    if (Number(delay) !== 30000) return previousSetTimeout(callback, delay, ...args);
    const id = ++nextId;
    deadlines.set(id, {callback, args});
    return id;
  };
  const fakeClearTimeout = (id) => {
    if (deadlines.delete(id)) return;
    previousClearTimeout(id);
  };
  globalThis.setTimeout = fakeSetTimeout;
  globalThis.clearTimeout = fakeClearTimeout;
  window.setTimeout = fakeSetTimeout;
  window.clearTimeout = fakeClearTimeout;
  clockRestore = () => {
    globalThis.setTimeout = previousSetTimeout;
    globalThis.clearTimeout = previousClearTimeout;
    window.setTimeout = previousWindowSetTimeout;
    window.clearTimeout = previousWindowClearTimeout;
    clockRestore = null;
  };
  return {
    size: () => deadlines.size,
    async fireAll(){
      const pending = [...deadlines.values()];
      deadlines.clear();
      await act(async () => { pending.forEach(({callback, args}) => callback(...args)); });
      await settle();
    },
  };
}

async function settle(){
  for (let i = 0; i < 8; i += 1){
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
}

async function mountListen(onAuth = () => {}){
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<ListenPage region="ZA" setRegion={() => {}} session={0} onAuth={onAuth} />));
  await settle();
}

async function mountNetwork(onAuth = () => {}){
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<NetworkGraph region="ZA" setRegion={() => {}} session={0} onAuth={onAuth} />));
  await settle();
}

const retryButton = () => [...host.querySelectorAll('button')].find((button) => button.textContent.trim() === 'Try again');

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  clearCache();
  localStorage.setItem(PASS_KEY, 'fixture-pass');
  host = null;
  root = null;
  serve(() => response(404, {error: 'not_found', message: 'No such API route.'}));
});

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
  if (clockRestore) clockRestore();
  globalThis.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
  localStorage.removeItem(PASS_KEY);
  clearCache();
  globalThis.fetch = previousFetch;
  window.fetch = previousWindowFetch;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

test('Listen renders a missing API route as unavailable and keeps route detail closed', async () => {
  let authCalls = 0;
  await mountListen(() => { authCalls += 1; });

  expect(calls.map((call) => call.url)).toEqual(['/api/intel/mentions?market=za']);
  expect(calls[0].init.headers['X-Passcode']).toBe('fixture-pass');
  expect(authCalls).toBe(0);
  expect(host.querySelector('.intel-down .id-head')?.textContent).toBe('Listen is not available yet.');
  expect(host.querySelector('.intel-down > .id-msg')?.textContent).toBe('The mention feed is not available in this version. No mention data was read.');
  expect(host.querySelector('.workspace-reason')?.open).toBe(false);
  expect(host.querySelector('.workspace-reason')?.textContent).toContain('http_404');
  expect(host.querySelector('.workspace-reason')?.textContent).toContain('No such API route.');
  expect(retryButton()).toBeUndefined();
});

test('Network renders a missing API route as unavailable and keeps route detail closed', async () => {
  await mountNetwork();

  expect(calls.map((call) => call.url).sort()).toEqual(['/api/desk?region=za', '/api/voices?region=za']);
  expect(host.querySelector('.network-page')?.getAttribute('data-network-state')).toBe('unavailable');
  expect(host.querySelector('.intel-down .id-head')?.textContent).toBe('Network is not available yet.');
  expect(host.querySelector('.intel-down > .id-msg')?.textContent).toBe('Network data is not available in this version. No relationships were read.');
  expect(host.querySelector('.workspace-reason')?.open).toBe(false);
  expect(host.querySelector('.workspace-reason')?.textContent).toContain('http_404');
  expect(host.querySelector('.workspace-reason')?.textContent).toContain('No such API route.');
  expect(retryButton()).toBeUndefined();
});

test('a record 404 stays a read failure instead of claiming the Listen API is absent', async () => {
  serve(() => response(404, {error: 'not_found', message: 'No mention records were returned for this market.'}));
  await mountListen();

  expect(host.querySelector('.intel-down .id-head')?.textContent).not.toBe('Listen is not available yet.');
  expect(host.querySelector('.intel-down > .id-msg')?.textContent).toContain('No mention records were returned for this market.');
  expect(retryButton()).toBeDefined();
});

test('Listen times out a pending read, ignores its late response and retries only on click', async () => {
  const pending = [];
  serve(() => new Promise((resolve) => pending.push(resolve)));
  const clock = controlDeadlines();
  await mountListen();

  expect(calls).toHaveLength(1);
  expect(clock.size()).toBe(1);
  await clock.fireAll();
  expect(host.querySelector('.intel-down .id-head')?.textContent).toBe('The mention feed timed out.');
  expect(host.querySelector('.intel-down > .id-msg')?.textContent).toBe('The feed did not answer within 30 seconds. No mention data was read.');
  expect(host.querySelector('.workspace-reason')?.textContent).toContain('request_timeout');

  pending[0](response(200, {results: [{title: 'Late mention'}]}));
  await settle();
  expect(host.textContent).not.toContain('Late mention');
  expect(host.querySelector('.intel-down .id-head')?.textContent).toBe('The mention feed timed out.');
  expect(calls).toHaveLength(1);

  serve(() => response(503, {error: 'upstream_unavailable', message: 'The mention source did not answer.'}));
  await act(async () => retryButton()?.click());
  await settle();
  expect(calls).toHaveLength(2);
  expect(host.querySelector('.intel-down > .id-msg')?.textContent).toContain('The mention source did not answer.');
  expect(retryButton()).toBeDefined();
  await settle();
  expect(calls).toHaveLength(2);
});

test('Network bounds both pending reads, ignores late responses and retries only on click', async () => {
  const pending = [];
  serve(() => new Promise((resolve) => pending.push(resolve)));
  const clock = controlDeadlines();
  await mountNetwork();

  expect(calls).toHaveLength(2);
  expect(clock.size()).toBe(2);
  await clock.fireAll();
  expect(host.querySelector('.intel-down .id-head')?.textContent).toBe('The network read timed out.');
  expect(host.querySelector('.intel-down > .id-msg')?.textContent).toBe('The reads did not answer within 30 seconds. No relationships were drawn.');
  expect(host.querySelector('.workspace-reason')?.textContent).toContain('request_timeout');

  pending[0](response(200, {topics: [], bridges: [], freshness: {status: 'green'}}));
  pending[1](response(200, {creators: []}));
  await settle();
  expect(host.querySelector('.intel-down .id-head')?.textContent).toBe('The network read timed out.');
  expect(calls).toHaveLength(2);

  serve(() => response(503, {error: 'upstream_unavailable', message: 'The network source did not answer.'}));
  await act(async () => retryButton()?.click());
  await settle();
  expect(calls).toHaveLength(4);
  expect(host.querySelector('.intel-down > .id-head')?.textContent).toContain('The network source did not answer.');
  expect(retryButton()).toBeDefined();
  await settle();
  expect(calls).toHaveLength(4);
});

/* Design audit, 2 October 2026: a control that can do nothing is not shown
   (NN/g on empty states; Nielsen heuristic 8, aesthetic and minimalist
   design). When the feed is unavailable or the read failed there are no posts
   to filter, so the filter box and the sentiment tabs go. The market tabs
   stay, because the market is the page's context and a way out. */
const listenControls = () => ({
  market: [...host.querySelectorAll('.listen-filter-label')].some((label) => label.textContent === 'Market'),
  search: host.querySelector('.listen-search') !== null,
  sentiment: [...host.querySelectorAll('.listen-filter-label')].some((label) => label.textContent === 'Sentiment'),
});

test('Listen hides the filter box and sentiment tabs when the feed is unavailable or the read failed', async () => {
  await mountListen();
  expect(host.querySelector('.intel-down .id-head')?.textContent).toBe('Listen is not available yet.');
  expect(listenControls()).toEqual({market: true, search: false, sentiment: false});
  await act(async () => root.unmount());
  host.remove();
  root = null;
  clearCache();

  serve(() => response(503, {error: 'upstream_unavailable', message: 'The mention source did not answer.'}));
  await mountListen();
  expect(host.querySelector('.intel-down')).not.toBeNull();
  expect(listenControls()).toEqual({market: true, search: false, sentiment: false});
});

test('Listen keeps every control while loading and once the feed is ready', async () => {
  const pending = [];
  serve(() => new Promise((resolve) => pending.push(resolve)));
  await mountListen();
  expect(listenControls()).toEqual({market: true, search: true, sentiment: true});
  pending[0](response(200, {results: [{title: 'A mention', sentiment: 'positive', url: 'https://example.com/a'}]}));
  await settle();
  expect(host.textContent).toContain('A mention');
  expect(listenControls()).toEqual({market: true, search: true, sentiment: true});
});

test('a chosen sentiment whose read fails keeps the sentiment row so the reader can go back to All', async () => {
  let calls = 0;
  serve(() => { calls += 1; return calls === 1 ? response(200, {results: [{title: 'A mention', sentiment: 'positive', url: 'https://example.com/a'}]}) : response(503, {error: 'upstream_unavailable', message: 'The mention source did not answer.'}); });
  await mountListen();
  await settle();
  const negative = [...host.querySelectorAll('.listen-filter-chip')].find((b) => b.textContent === 'Negative');
  await act(async () => negative.click());
  await settle();
  expect(host.querySelector('.intel-down') !== null).toBe(true);
  expect(listenControls()).toEqual({market: true, search: false, sentiment: true});
});
