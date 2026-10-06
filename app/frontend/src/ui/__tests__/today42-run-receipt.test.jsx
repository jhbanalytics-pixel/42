/* The Today header's run receipt (contract.md section 4, run_receipt): one
   plain line built only from the counts f42-api returns, and no line at all
   when the field is missing or any count in it is not a whole number. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TodayPage42} = await import('../../today42.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const receiptLine = () => host.querySelector('[data-today-run-receipt]');
const RECEIPT = {posts: 2536, markets: ['South Africa', 'Nigeria', 'Kenya'], shown: 0, held: 27, collect_run_id: 'collect-x'};

beforeEach(() => {
  localStorage.setItem('pulse_passcode', 'test-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = realFetch;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

async function mount(receipt){
  const today = clone(todayFixture);
  if (receipt === undefined) delete today.run_receipt;
  else today.run_receipt = receipt;
  globalThis.fetch = async (url) => (String(url).startsWith('/api/today')
    ? reply(200, today) : reply(404, {error: 'not_found', message: 'No route'}));
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  await settle();
}

test('the header says what this run read, showed and held, with thousands separators', async () => {
  await mount(RECEIPT);
  const line = receiptLine();
  expect(line).not.toBeNull();
  expect(line.closest('header.t42-head')).not.toBeNull();
  expect(line.textContent).toBe('42 read 2,536 posts for South Africa, Nigeria and Kenya, showed 0 trends and held 27 back with reasons.');
});

test('the line uses singular words for one and says none are held when none are', async () => {
  await mount({...RECEIPT, posts: 1, shown: 1, held: 0, markets: ['Kenya']});
  expect(receiptLine().textContent).toBe('42 read 1 post for Kenya, showed 1 trend and held none back.');
  resetRoot();
  await mount({...RECEIPT, posts: 1234567, shown: 12, held: 1, markets: ['South Africa', 'Nigeria']});
  expect(receiptLine().textContent).toBe('42 read 1,234,567 posts for South Africa and Nigeria, showed 12 trends and held 1 back with reasons.');
});

test('no line without a run receipt', async () => {
  await mount(undefined);
  expect(receiptLine()).toBeNull();
});

test('no line when any count is missing or not a whole number, so nothing is guessed', async () => {
  const bad = [
    null, 'x', [], {...RECEIPT, posts: undefined}, {...RECEIPT, posts: '2536'}, {...RECEIPT, posts: -1},
    {...RECEIPT, posts: 2.5}, {...RECEIPT, shown: null}, {...RECEIPT, held: true}, {...RECEIPT, markets: []},
    {...RECEIPT, markets: ['South Africa', '']}, {...RECEIPT, markets: 'South Africa'},
  ];
  for (const receipt of bad){
    resetRoot();
    await mount(receipt);
    expect(receiptLine()).toBeNull();
  }
});

function resetRoot(){
  flushSync(() => root.unmount());
  root = createRoot(host);
}
