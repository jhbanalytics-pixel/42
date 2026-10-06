/* Demo polish, 2 October 2026. The 42 API in core/api serves no /api/desk, so
   a page that does not read the legacy desk must not ask for it: the request
   only logs a 404 in the reader's console. Browse and a released signal story
   still read the desk, so they keep the request. Since 3 October neither
   does (see the last two tests). */
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {PASS_KEY, clearCache} = await import('../../api.js');
const {default: App} = await import('../../App.jsx');

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

function serve(){
  globalThis.fetch = async (url, init = {}) => {
    calls.push({url: String(url), init});
    if (String(url) === '/api/health'){
      return reply(200, {ok: true, service: 'f42-api', auth_mode: 'passcode', passcode: true, checks: {auth: 'ok'}});
    }
    if (String(url) === '/api/auth/verify') return reply(200, {ok: true});
    return reply(404, {error: 'not_found'});
  };
}

async function settle(){
  for (let i = 0; i < 16; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

async function visit(hash){
  window.location.hash = hash;
  calls = [];
  clearCache();
  serve();
  host = document.createElement('div');
  document.body.appendChild(host);
  appRoot = createRoot(host);
  flushSync(() => appRoot.render(<App />));
  await settle();
  return calls.filter((call) => call.url.startsWith('/api/desk')).length;
}

beforeEach(() => {
  priorHash = window.location.hash;
  localStorage.setItem(PASS_KEY, 'synthetic-fixture-passcode');
});

afterEach(() => {
  if (appRoot) flushSync(() => appRoot.unmount());
  appRoot = null;
  if (host) host.remove();
  host = null;
  window.location.hash = priorHash;
  globalThis.fetch = realFetch;
  localStorage.removeItem(PASS_KEY);
  clearCache();
});

afterAll(() => { GlobalRegistrator.unregister(); });

const DESKLESS = ['#/ask', '#/compare', '#/history', '#/alerts', '#/dossiers', '#/schedules', '#/investigations', '#/skins', '#/map'];

for (const hash of DESKLESS){
  test(`${hash} does not request the legacy desk`, async () => {
    expect(await visit(hash)).toBe(0);
  });
}

/* Page port, 3 October 2026: Browse's link now opens Discover and an older
   released signal link says it is from an older version, so neither asks for
   the desk any more. */
test('the Browse link opens Discover and does not request the legacy desk', async () => {
  expect(await visit('#/browse')).toBe(0);
  expect(window.location.hash).toBe('#/explore');
});

test('an older released signal link does not request the legacy desk', async () => {
  expect(await visit('#/topic/sig_' + 'a'.repeat(64))).toBe(0);
  expect(host.querySelector('h1')?.textContent).toBe('This topic link is from an older version');
});
