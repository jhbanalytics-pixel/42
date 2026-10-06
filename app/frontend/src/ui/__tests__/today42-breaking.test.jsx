/* Breaking on Today (contract.md section 4, breaking): a small strip above
   the morning cards, apart from them, only when a shown market has a
   Breaking line; nothing at all, not even its heading, when none has. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TodayPage42} = await import('../../today42.jsx');
const {topicHref} = await import('../TrendCard.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const strip = () => host.querySelector('[data-section="breaking"]');
const HEADING = 'Breaking in the last few hours';
const ZA_ID = 'a'.repeat(64);
const NG_ID = 'b'.repeat(64);
const ZA_LINE = {item_id: ZA_ID, market: 'ZA', market_label: 'South Africa', kind: 'hashtag', title: '#fixture_za_step',
  time_text: '14:00', ago_text: '2 hours ago', note: null};
const NG_LINE = {item_id: NG_ID, market: 'NG', market_label: 'Nigeria', kind: 'hashtag', title: '#fixture_ng_owambe',
  time_text: '12:00', ago_text: '4 hours ago', note: 'New, no prior posts'};

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

async function mount(breaking, region = 'ZA'){
  const today = clone(todayFixture);
  for (const market of today.markets){
    if (breaking === undefined) delete market.breaking;
    else market.breaking = breaking[market.market] || [];
  }
  globalThis.fetch = async (url) => (String(url).startsWith('/api/today')
    ? reply(200, today) : reply(404, {error: 'not_found', message: 'No route'}));
  flushSync(() => root.render(<TodayPage42 region={region} />));
  await settle();
}

test('nothing is shown, not even the heading, when there is no Breaking field', async () => {
  await mount(undefined);
  expect(host.querySelector('[data-today-loaded]')).not.toBeNull();
  expect(strip()).toBeNull();
  expect(host.textContent).not.toContain(HEADING);
});

test('nothing is shown when every Breaking list is empty', async () => {
  await mount({});
  expect(strip()).toBeNull();
  expect(host.textContent).not.toContain(HEADING);
});

test('nothing is shown on a market tab whose list is empty, even when another market has lines', async () => {
  await mount({NG: [NG_LINE]}, 'ZA');
  expect(strip()).toBeNull();
});

test('the strip sits above the cards with its heading and one plain linked line per item', async () => {
  await mount({ZA: [ZA_LINE], NG: [NG_LINE]}, 'ZA');
  const section = strip();
  expect(section).not.toBeNull();
  expect(section.querySelector('h2').textContent).toBe(HEADING);
  const rows = [...section.querySelectorAll('li')];
  expect(rows).toHaveLength(1);
  expect(rows[0].textContent).toBe('#fixture_za_step · South Africa · 2 hours ago');
  const link = rows[0].querySelector('a');
  expect(link.getAttribute('href')).toBe(topicHref(ZA_ID, 'ZA'));
  expect(link.textContent).toBe('#fixture_za_step');
  expect(section.textContent).not.toContain(ZA_ID);
  // Above the morning cards and apart from them.
  const cards = host.querySelector('.t42-cards, .t42-today-market');
  expect(cards).not.toBeNull();
  expect(section.compareDocumentPosition(cards) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(section.closest('.t42-today-market')).toBeNull();
  expect(section.closest('.t42-cards')).toBeNull();
});

test('All lists every market, and a new item says so instead of a ratio', async () => {
  await mount({ZA: [ZA_LINE], NG: [NG_LINE]}, 'ALL');
  const rows = [...strip().querySelectorAll('li')];
  expect(rows.map((row) => row.textContent)).toEqual([
    '#fixture_za_step · South Africa · 2 hours ago',
    '#fixture_ng_owambe · Nigeria · 4 hours ago · New, no prior posts',
  ]);
  expect(rows[1].querySelector('a').getAttribute('href')).toBe(topicHref(NG_ID, 'NG'));
});

test('a line without a usable title or item is left out, and with none left the strip is not shown', async () => {
  await mount({ZA: [{...ZA_LINE, title: ''}, {...ZA_LINE, item_id: ''}, null]}, 'ZA');
  expect(strip()).toBeNull();
});
