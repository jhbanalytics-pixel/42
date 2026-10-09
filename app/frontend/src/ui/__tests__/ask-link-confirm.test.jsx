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
const CONFIRMED = LIVE + '&live=1';

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
  expect(window.location.hash).toBe(CONFIRMED);
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

/* L7-5: a link made before drafts existed carries no draft=1. Opening it fills
   in the question and spends nothing; only the confirm's own address (live=1)
   starts the ask. */
const {parseAskQuery} = await import('../../App.jsx');
const OLD = '#/ask?q=What%20is%20behind%20this%3F&market=ZA&item=it_1&date=2026-10-01';

test('an old #/ask?q= link with no draft=1 is read as a draft, and only live=1 is not', () => {
  expect(parseAskQuery(OLD).draft).toBe(true);
  expect(parseAskQuery('#/ask?q=football').draft).toBe(true);
  expect(parseAskQuery(OLD + '&live=1').draft).toBeUndefined();
  for (const value of ['true', '0', '01', '']) expect(parseAskQuery(OLD + '&live=' + value).draft).toBe(true);
  expect(parseAskQuery(OLD + '&draft=1&live=1').draft).toBe(true);
  expect(parseAskQuery('#/ask').draft).toBeUndefined();
  expect(parseAskQuery('#/ask?follow=a_1').draft).toBeUndefined();
});

test('an old ?q= link renders the draft and makes no request to the ask endpoint', async () => {
  const record = clone(completeRecord);
  serve(record);
  const query = parseAskQuery(OLD);
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={query} />));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 30)); });
  expect(host.querySelector('textarea').value).toBe('What is behind this?');
  expect(calls.filter((call) => call.path.includes('/api/ask'))).toEqual([]);
  expect(calls.filter((call) => call.method === 'POST')).toEqual([]);
  await act(async () => { [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === 'Ask').click(); });
  await until(() => calls.some((call) => call.method === 'POST' && call.path === '/api/ask'), 'the ask');
  const posted = calls.find((call) => call.method === 'POST' && call.path === '/api/ask');
  expect(posted.body).toMatchObject({question: 'What is behind this?', market: 'ZA', from_card: {item_id: 'it_1', market: 'ZA', date: '2026-10-01'}});
});

test('the live address the confirm goes to starts the ask', async () => {
  const record = clone(completeRecord);
  serve(record);
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={parseAskQuery(OLD + '&live=1')} />));
  await until(() => calls.some((call) => call.method === 'POST' && call.path === '/api/ask'), 'the ask');
  const posted = calls.find((call) => call.method === 'POST' && call.path === '/api/ask');
  expect(posted.body).toMatchObject({question: 'What is behind this?', market: 'ZA', from_card: {item_id: 'it_1'}});
});
