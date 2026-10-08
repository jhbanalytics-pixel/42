/* Wave 8 N46: the market an Ask sends is the reader's choice. A market the
   question names wins over the page's default region, a deliberate pick wins
   over both, and a typed follow-up sends the Market select, not the parent
   answer's market. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import {readFileSync} from 'node:fs';
import completeRecord from './fixtures/ask42_complete.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {AskPage} = await import('../../ask42.jsx');
const {namedMarkets, marketToSend} = await import('../../askMarkets.js');

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

/* Every POST /api/ask is accepted with the next record; reading a record
   returns it already complete, and its event stream ends at once. */
function serve(records){
  const byId = new Map(records.map((record) => [record.ask_id, record]));
  let next = 0;
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({path, method, body: init.body ? JSON.parse(init.body) : null});
    if (method === 'POST' && path === '/api/ask'){
      const id = records[Math.min(next, records.length - 1)].ask_id;
      next += 1;
      return json(202, {ask_id: id, status: 'running', events_url: `/api/ask/${id}/events`, url: `/api/ask/${id}`});
    }
    const match = /^\/api\/ask\/([^/?]+)(\/[a-z]+)?/.exec(path);
    if (match && byId.has(match[1])){
      if (match[2] === '/events'){
        const encoder = new TextEncoder();
        const done = `id: 1\nevent: done\ndata: ${JSON.stringify({seq: 1, status: 'complete'})}\n\n`;
        let sent = false;
        return {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: {getReader: () => ({
          read: async () => { if (sent) return {value: undefined, done: true}; sent = true; return {value: encoder.encode(done), done: false}; },
          cancel: async () => {}, releaseLock(){},
        })}};
      }
      return json(200, byId.get(match[1]));
    }
    return json(404, {error: 'not_found', message: 'No such question.'});
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

const button = (label) => [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === label);
const posts = () => calls.filter((call) => call.method === 'POST' && call.path === '/api/ask');

function setValue(element, value){
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : element.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  element.dispatchEvent(new window.Event(element.tagName === 'SELECT' ? 'change' : 'input', {bubbles: true}));
}

async function render(props){
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{}} {...props} />));
}

async function typeAndAsk(question){
  await act(async () => setValue(host.querySelector('textarea'), question));
  await act(async () => { button('Ask').click(); });
  await until(() => posts().length > 0, 'the ask to start');
}

test('a market the question names is sent, not the region the page started on', async () => {
  serve([clone(completeRecord)]);
  await render({region: 'ZA'});
  await typeAndAsk('How are people in Nigeria talking about food prices right now?');
  expect(posts()[0].body.market).toBe('NG');
});

test('a question that names two markets leaves the market to the question', async () => {
  serve([clone(completeRecord)]);
  await render({region: 'ZA'});
  await typeAndAsk('Is amapiano bigger in South Africa or Nigeria this week?');
  expect(posts()[0].body.market).toBe(null);
});

test('a question that names no market sends the market the select shows', async () => {
  serve([clone(completeRecord)]);
  await render({region: 'KE'});
  await typeAndAsk('Which sounds are rising on TikTok this week?');
  expect(posts()[0].body.market).toBe('KE');
});

test('a market the reader picked in the select wins over a market the question names', async () => {
  serve([clone(completeRecord)]);
  await render({region: 'ZA'});
  await act(async () => setValue(host.querySelector('#ask42-market'), 'ZA'));
  await typeAndAsk('How are people in Nigeria talking about food prices right now?');
  expect(posts()[0].body.market).toBe('ZA');
});

test('the select shows the market that was sent', async () => {
  serve([clone(completeRecord)]);
  await render({region: 'ZA'});
  await typeAndAsk('How are people in Nigeria talking about food prices right now?');
  expect(host.querySelector('#ask42-market').value).toBe('NG');
});

async function openFollowUp(first){
  await render({query: {follow: first.ask_id}});
  await until(() => button('Ask a follow-up'), 'the reopened answer');
  await act(async () => { button('Ask a follow-up').click(); });
}

async function submitFollowUp(question){
  await act(async () => setValue(host.querySelector('textarea'), question));
  await act(async () => { host.querySelector('form.ask42-form').dispatchEvent(new window.Event('submit', {bubbles: true, cancelable: true})); });
  await until(() => posts().length > 0, 'the follow-up ask');
}

test('a typed follow-up sends the Market select, not the parent answer market', async () => {
  const first = clone(completeRecord);
  first.market = 'ZA';
  serve([first, {...clone(completeRecord), ask_id: 'a_20260928_0000000f', parent_id: first.ask_id}]);
  await openFollowUp(first);
  await act(async () => setValue(host.querySelector('#ask42-market'), 'NG'));
  await submitFollowUp('Is it moving in the capital too?');
  expect(posts()[0].body).toMatchObject({market: 'NG', parent_id: first.ask_id});
});

test('a follow-up with the select on From the question sends no market', async () => {
  const first = clone(completeRecord);
  first.market = 'ZA';
  serve([first, {...clone(completeRecord), ask_id: 'a_20260928_0000000f', parent_id: first.ask_id}]);
  await openFollowUp(first);
  await act(async () => setValue(host.querySelector('#ask42-market'), ''));
  await submitFollowUp('Is it moving in the capital too?');
  expect(posts()[0].body.market).toBe(null);
});

test('a follow-up that names another market sends that market while the select still shows the parent market', async () => {
  const first = clone(completeRecord);
  first.market = 'ZA';
  serve([first, {...clone(completeRecord), ask_id: 'a_20260928_0000000f', parent_id: first.ask_id}]);
  await openFollowUp(first);
  expect(host.querySelector('#ask42-market').value).toBe('ZA');
  await submitFollowUp('How is this playing in Nigeria?');
  expect(posts()[0].body).toMatchObject({market: 'NG', parent_id: first.ask_id});
});

test('a follow-up with no market named keeps the parent market', async () => {
  const first = clone(completeRecord);
  first.market = 'KE';
  serve([first, {...clone(completeRecord), ask_id: 'a_20260928_0000000f', parent_id: first.ask_id}]);
  await openFollowUp(first);
  await submitFollowUp('Which creators are driving it?');
  expect(posts()[0].body.market).toBe('KE');
});

test('a link with no market that names one in the question starts that market', async () => {
  serve([clone(completeRecord)]);
  window.history.replaceState(null, '', '#/ask');
  await render({region: 'ZA', query: {q: 'What are people in Kenya saying about fuel prices?'}});
  await until(() => posts().length > 0, 'the link ask');
  expect(posts()[0].body.market).toBe('KE');
});

test('namedMarkets and marketToSend', () => {
  expect(namedMarkets('Compare Lagos with Nairobi')).toEqual(['NG', 'KE']);
  expect(namedMarkets('what is SA saying')).toEqual(['ZA']);
  expect(namedMarkets('sa lot of noise')).toEqual([]);
  expect(marketToSend({question: 'Naija food prices', selected: 'ZA', picked: false})).toBe('NG');
  expect(marketToSend({question: 'Naija food prices', selected: 'ZA', picked: true})).toBe('ZA');
  expect(marketToSend({question: 'Kenya or Nigeria', selected: 'ZA', picked: false})).toBe('');
  expect(marketToSend({question: 'food prices', selected: 'ZA', picked: false})).toBe('ZA');
  expect(marketToSend({question: 'food prices', selected: '', picked: false})).toBe('');
});

/* The place words are the agent's: core/agent/ask.py _MARKET_PATTERNS and _SA.
   Reading that file keeps the two lists from drifting. */
test('the market words are the ones the agent reads', () => {
  const python = readFileSync(new URL('../../../../../core/agent/ask.py', import.meta.url), 'utf8');
  const block = /_MARKET_PATTERNS = \{([\s\S]*?)\n\}/.exec(python)[1];
  const fromAgent = {};
  for (const [, market, words] of block.matchAll(/"(\w\w)": re\.compile\(r"\\b\(\?:([^)]*)\)"/g)) fromAgent[market] = words.split('|');
  expect(Object.keys(fromAgent)).toEqual(['ZA', 'NG', 'KE']);
  for (const market of Object.keys(fromAgent)){
    for (const word of fromAgent[market]) expect(namedMarkets('about ' + word + ' today')).toEqual([market]);
  }
  expect(/_SA = re\.compile\(r"\\bSA\\b"\)/.test(python)).toBe(true);
});
