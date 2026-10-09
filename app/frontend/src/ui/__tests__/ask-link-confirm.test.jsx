/* Wave 8 N45 (R0300, R0341, R0365): an "Ask about this" link never starts a
   paid ask by being opened. The address it carries is a draft, so a new tab, a
   copied link or a reload only fills in the question; the cost confirm is the
   one way to the live ask, and the draft keeps the card it came from. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import completeRecord from './fixtures/ask42_complete.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {AskAboutThis} = await import('../AskAboutThis.jsx');
const {AskPage} = await import('../../ask42.jsx');
const {legacyHashTarget} = await import('../../legacyRoutes.js');
const {parseAskQuery} = await import('../../App.jsx');
const {grantAsk, consumeAsk} = await import('../../askConsent.js');

const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/\s+/g, ' ').trim();

let host = null;
let root = null;
let calls = [];

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  consumeAsk({});
  window.history.replaceState(null, '', '#/');
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

const json = (status, body) => ({ok: status < 400, status, headers: {get: () => null}, json: async () => body, text: async () => JSON.stringify(body)});

function serve(record){
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({path, method, body: init.body ? JSON.parse(init.body) : null});
    if (method === 'POST' && path === '/api/ask') return json(202, {ask_id: record.ask_id, status: 'running'});
    if (path.endsWith('/events')){
      const encoder = new TextEncoder();
      let sent = false;
      return {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: {getReader: () => ({
        read: async () => { if (sent) return {value: undefined, done: true}; sent = true; return {value: encoder.encode('id: 1\nevent: done\ndata: {"seq":1,"status":"complete"}\n\n'), done: false}; },
        cancel: async () => {}, releaseLock(){},
      })}};
    }
    return json(200, record);
  };
  globalThis.fetch = stub;
  window.fetch = stub;
}

async function until(check, label){
  const started = Date.now();
  for (;;){
    let ok = false;
    try { ok = Boolean(check()); } catch (_error){ ok = false; }
    if (ok) return;
    if (Date.now() - started > 4000) throw new Error('timed out waiting for ' + label);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  }
}

const LIVE = '#/ask?q=What%20is%20behind%20this%3F&market=ZA&item=it_1&date=2026-10-01';

test('the Ask about this address is a draft and confirming goes to the live ask with the same card', async () => {
  await act(async () => root.render(<AskAboutThis href={LIVE} className="x" question="What is behind this?" />));
  const link = host.querySelector('a');
  const params = new URLSearchParams(link.getAttribute('href').split('?')[1]);
  expect(params.get('draft')).toBe('1');
  expect(params.get('q')).toBe('What is behind this?');
  expect(params.get('item')).toBe('it_1');
  expect(params.get('market')).toBe('ZA');
  expect(params.get('date')).toBe('2026-10-01');
  await act(async () => { link.click(); });
  await until(() => host.querySelector('[role="dialog"]'), 'the confirm');
  const confirm = [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === 'Ask');
  await act(async () => { confirm.click(); });
  expect(window.location.hash).toBe(LIVE);
  expect(parseAskQuery(window.location.hash).draft).toBe(true);
  expect(consumeAsk(parseAskQuery(window.location.hash))).toBe(true);
});

test('a draft from a card fills the question and starts nothing, then Ask posts the card it came from', async () => {
  const record = clone(completeRecord);
  serve(record);
  const query = {q: 'What is behind this?', market: 'ZA', item: 'it_1', date: '2026-10-01', draft: true};
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={query} />));
  expect(host.querySelector('textarea').value).toBe('What is behind this?');
  expect(calls.filter((call) => call.method === 'POST')).toEqual([]);
  await act(async () => { [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === 'Ask').click(); });
  await until(() => calls.some((call) => call.method === 'POST' && call.path === '/api/ask'), 'the ask');
  const posted = calls.find((call) => call.method === 'POST' && call.path === '/api/ask');
  expect(posted.body).toMatchObject({question: 'What is behind this?', market: 'ZA', from_card: {item_id: 'it_1', market: 'ZA', date: '2026-10-01'}});
});

test('an older Console address that carries a question is a draft too', () => {
  expect(legacyHashTarget('#/console?work=ask&q=What%20changed%3F&market=NG')).toBe('#/ask?q=What+changed%3F&market=NG&draft=1');
  expect(legacyHashTarget('#/console?work=ask&q=What%20changed%3F&draft=1')).toBe('#/ask?q=What+changed%3F&draft=1');
  expect(legacyHashTarget('#/console?work=ask')).toBe('#/ask');
});

/* L7-5: any address that carries a question is a draft, with or without
   draft=1 or live=1. Only the cost confirm lets one start, by granting a
   one-shot token held in memory for that question, market, item and date. */
const OLD = '#/ask?q=What%20is%20behind%20this%3F&market=ZA&item=it_1&date=2026-10-01';
const posts = () => calls.filter((call) => call.method === 'POST');

async function mountAddress(hash){
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={parseAskQuery(hash)} />));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
}

test('an old ?q= link renders the draft and makes no request to the ask endpoint', async () => {
  serve(clone(completeRecord));
  expect(parseAskQuery(OLD).draft).toBe(true);
  await mountAddress(OLD);
  expect(host.querySelector('textarea').value).toBe('What is behind this?');
  expect(calls.filter((call) => call.path.includes('/api/ask'))).toEqual([]);
  await act(async () => { [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === 'Ask').click(); });
  await until(() => posts().length === 1, 'the ask');
  expect(posts()[0].body).toMatchObject({question: 'What is behind this?', market: 'ZA', from_card: {item_id: 'it_1', market: 'ZA', date: '2026-10-01'}});
});

test('a crafted live=1 or draft=0 address makes no POST and shows the draft', async () => {
  serve(clone(completeRecord));
  for (const extra of ['&live=1', '&live=true&draft=0', '&draft=1&live=1']){
    calls = [];
    expect(parseAskQuery(OLD + extra).draft).toBe(true);
    await mountAddress(OLD + extra);
    expect(host.querySelector('textarea').value).toBe('What is behind this?');
    expect(posts()).toEqual([]);
    await act(async () => root.render(null));
  }
});

test('a granted token starts one ask for its own question, market, item and date, then is spent', async () => {
  serve(clone(completeRecord));
  grantAsk(parseAskQuery(OLD));
  await mountAddress(OLD);
  await until(() => posts().length === 1, 'the confirmed ask');
  expect(posts()[0].body).toMatchObject({question: 'What is behind this?', market: 'ZA', from_card: {item_id: 'it_1'}});
  await act(async () => root.render(null));
  calls = [];
  await mountAddress(OLD);
  expect(posts()).toEqual([]);
  expect(host.querySelector('textarea').value).toBe('What is behind this?');
});

test('a token for another question, market, item or date does not start this one', async () => {
  serve(clone(completeRecord));
  for (const other of [
    OLD.replace('behind', 'under'),
    OLD.replace('market=ZA', 'market=NG'),
    OLD.replace('item=it_1', 'item=it_2'),
    OLD.replace('2026-10-01', '2026-10-02'),
    OLD.replace('&item=it_1', ''),
  ]){
    calls = [];
    grantAsk(parseAskQuery(other));
    await mountAddress(OLD);
    expect(posts()).toEqual([]);
    await act(async () => root.render(null));
    consumeAsk(parseAskQuery(other));
  }
});

test('a token that was not used within thirty seconds is stale', async () => {
  serve(clone(completeRecord));
  const now = Date.now;
  try {
    grantAsk(parseAskQuery(OLD));
    Date.now = () => now() + 31000;
    await mountAddress(OLD);
    expect(posts()).toEqual([]);
  } finally { Date.now = now; }
});

test('a failed start is not posted again by a reload, back or restore of the same address', async () => {
  const failing = async (url, init = {}) => {
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({path: String(url), method, body: init.body ? JSON.parse(init.body) : null});
    return json(503, {error: 'unavailable'});
  };
  globalThis.fetch = failing;
  window.fetch = failing;
  grantAsk(parseAskQuery(OLD));
  window.location.hash = OLD;
  await mountAddress(OLD);
  await until(() => posts().length === 1, 'the failed start');
  await act(async () => root.render(null));
  await mountAddress(OLD);
  await mountAddress(window.location.hash);
  expect(posts()).toHaveLength(1);
});
