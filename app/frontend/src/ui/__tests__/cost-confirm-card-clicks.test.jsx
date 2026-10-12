/* Review of the item 1 fix: once the credit confirm rendered on the route
   layer, a click inside it still bubbled through React to the Today card
   that opened it, and the card's tap to open read the click as a tap on the
   card. Cancel and Ask both went to the topic page, and Ask never started
   an ask. A click or key inside the confirm stays inside it, and the page
   behind does not scroll while it is open. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';
import trendFixture from './fixtures/trend42.json';
import completeRecord from './fixtures/ask42_complete.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {TodayPage42} = await import('../../today42.jsx');
const {AskPage} = await import('../../ask42.jsx');
const {parseAskQuery} = await import('../../App.jsx');

const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const realNow = Date.now;
const plain = (text) => String(text || '').replace(/\s+/g, ' ').trim();
const json = (status, body) => ({ok: status < 400, status, headers: {get: () => null}, json: async () => body, text: async () => JSON.stringify(body)});

let layer = null;
let host = null;
let root = null;
let calls = [];

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  Date.now = () => Date.parse('2026-09-30T08:00:00Z');
  calls = [];
  window.history.replaceState(null, '', '#/pulse');
  localStorage.setItem('pulse_passcode', 'test-pass');
  layer = document.createElement('div');
  layer.id = 'main-content';
  host = document.createElement('div');
  layer.appendChild(host);
  document.body.appendChild(layer);
  root = createRoot(host);
  serve();
});

afterEach(async () => {
  await act(async () => root.unmount());
  layer.remove();
  Date.now = realNow;
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
  document.body.style.overflow = '';
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

function serve(){
  const record = JSON.parse(JSON.stringify(completeRecord));
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({path, method});
    if (path.startsWith('/api/today')) return json(200, todayFixture);
    if (path.startsWith('/api/trends/')) return json(200, trendFixture);
    if (method === 'POST' && path === '/api/ask') return json(202, {ask_id: record.ask_id, status: 'running'});
    if (path.endsWith('/events')){
      const encoder = new TextEncoder();
      let sent = false;
      return {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: {getReader: () => ({
        read: async () => { if (sent) return {value: undefined, done: true}; sent = true; return {value: encoder.encode('id: 1\nevent: done\ndata: {"seq":1,"status":"complete"}\n\n'), done: false}; },
        cancel: async () => {}, releaseLock(){},
      })}};
    }
    if (path.startsWith('/api/ask/')) return json(200, record);
    return json(404, {error: 'not_found', message: 'No route'});
  };
  globalThis.fetch = stub;
  window.fetch = stub;
}

async function until(check, label){
  const started = Date.now === realNow ? Date.now() : realNow();
  for (;;){
    let ok = false;
    try { ok = Boolean(check()); } catch (_error){ ok = false; }
    if (ok) return;
    if (realNow() - started > 4000) throw new Error('timed out waiting for ' + label);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  }
}

const dialog = () => document.querySelector('.w42-dialog');
const button = (scope, words) => [...scope.querySelectorAll('button')].find((node) => plain(node.textContent) === words);
const asks = () => calls.filter((call) => call.method === 'POST' && call.path === '/api/ask');

async function mountToday(){
  await act(async () => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await until(() => host.querySelector('.sc-card'), 'the Today cards');
}

const OPENERS = {
  'a Today card': () => host.querySelector('.sc-card'),
  'the lead panel': () => host.querySelector('.t42-lead-panel'),
};

async function open(scope){
  const link = [...scope().querySelectorAll('a')].find((node) => plain(node.textContent) === 'Ask about this');
  link.focus();
  await act(async () => { link.click(); });
  await until(() => dialog(), 'the confirm');
  return link;
}

for (const [where, scope] of Object.entries(OPENERS)){
  test(`Cancel and keys in the confirm from ${where} stay in the confirm`, async () => {
    await mountToday();
    expect(Boolean(scope())).toBe(true);
    await open(scope);
    const lockedWhileOpen = document.body.style.overflow;
    const cancel = button(dialog(), 'Cancel');
    await act(async () => {
      cancel.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      cancel.click();
    });
    expect(dialog()).toBeNull();
    expect(window.location.hash).toBe('#/pulse');
    expect(lockedWhileOpen).toBe('hidden');
    expect(document.body.style.overflow).toBe('');
    await open(scope);
    await act(async () => { dialog().querySelector('h2').click(); });
    expect(window.location.hash).toBe('#/pulse');
    expect(dialog()).not.toBeNull();
    expect(asks()).toHaveLength(0);
  });

  test(`Ask in the confirm from ${where} goes to the Ask page and starts exactly one ask`, async () => {
    await mountToday();
    await open(scope);
    await act(async () => { button(dialog(), 'Ask').click(); });
    expect(window.location.hash.startsWith('#/ask?')).toBe(true);
    expect(window.location.hash).not.toContain('#/t/');
    expect(document.body.style.overflow).toBe('');
    const query = parseAskQuery(window.location.hash);
    await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={query} />));
    await until(() => asks().length === 1, 'the ask');
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)); });
    expect(asks()).toHaveLength(1);
  });
}

test('on the topic page the card, footer and day confirms open on the route layer and Cancel stays put', async () => {
  const topicFixture = (await import('./fixtures/topic42_za.json')).default;
  const {TopicPage42} = await import('../../topic42.jsx');
  const item = topicFixture.card.item_id;
  const stub = async (url, init = {}) => {
    calls.push({path: String(url), method: String(init.method || 'GET').toUpperCase()});
    return json(200, topicFixture);
  };
  globalThis.fetch = stub;
  window.fetch = stub;
  window.history.replaceState(null, '', '#/t/' + item);
  await act(async () => root.render(<TopicPage42 itemId={item} market="ZA" />));
  await until(() => host.querySelector('button[data-day]'), 'the topic page');
  const openers = [
    ...[...host.querySelectorAll('a')].filter((node) => plain(node.textContent) === 'Ask about this'),
    host.querySelector('button[data-day]'),
  ];
  expect(openers).toHaveLength(3);
  for (const opener of openers){
    await act(async () => { opener.dispatchEvent(new window.MouseEvent('click', {bubbles: true, cancelable: true})); });
    await until(() => dialog(), 'the confirm');
    expect(dialog().parentElement.parentElement.id).toBe('main-content');
    await act(async () => { button(dialog(), 'Cancel').click(); });
    expect(dialog()).toBeNull();
    expect(window.location.hash).toBe('#/t/' + item);
  }
  expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0);
});
