/* Today and Discover say the count of accounts the same way, because both read
   a card through the one helper in ui/accounts.js. The six shapes below are the
   shapes the Opus review of the Today branch probed, where Discover's own copy
   of the rule gave a different line from Today on three of them. Each shape is
   drawn on the Today story card and on a Discover Trends row, and both must show
   the pinned words: the measured figure said as accounts posting, and no figure
   of a stored count line that this page did not measure. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {Discover42} = await import('../../discover42.jsx');
const {StoryCard} = await import('../StoryCard.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const figure = (value, unit) => ({value, unit, query_id: 'q_' + String(unit).replace(/\W+/g, '_'), run_id: 'r_p1', result_hash: 'sha256:p1'});
const za = todayFixture.markets[0];
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const squash = (el) => el.textContent.replace(/\s+/g, ' ');

beforeEach(() => {
  localStorage.setItem('pulse_passcode', 'test-pass');
  window.history.replaceState(null, '', '#');
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
  window.history.replaceState(null, '', '#');
});
afterAll(() => { GlobalRegistrator.unregister(); });

/* Stored figures use numbers no other part of a card carries, so a stored figure that
   survives into the page can be told from a measured one. */
const SHAPES = [
  {
    name: 'reach matches a stored line that counts other accounts',
    edit: {reach: figure(17, 'creators in 3 days'), numbers: [], count_line: '83 creators, 3 days'},
    has: ['17 accounts posting, last 3 days'],
    lacks: ['83 creators', '83 accounts'],
  },
  {
    name: 'the measured figure is only in the numbers',
    edit: {reach: null, numbers: [figure(17, 'creators in 3 days')], count_line: '83 creators, 3 days'},
    has: ['17 accounts posting, last 3 days'],
    lacks: ['83 creators', '83 accounts'],
  },
  {
    name: 'a stored line that counts other accounts in other words',
    edit: {reach: figure(17, 'creators in 3 days'), numbers: [], count_line: 'Across 83 creators, 3 days'},
    has: ['17 accounts posting, last 3 days'],
    lacks: ['83 creators', '83 accounts'],
  },
  {
    name: 'a stored line that also counts posts is never joined to the measured accounts',
    edit: {reach: figure(17, 'creators in 3 days'), numbers: [figure(40, 'posts in 3 days')], count_line: '61 creators and 47 posts in 3 days'},
    has: ['17 accounts posting, last 3 days', '40 posts in 3 days'],
    lacks: ['61 creators', '47 posts', '17 accounts posting and'],
  },
  {
    name: 'a 7 day stored count is never said over the 3 day window',
    edit: {reach: figure(12, 'creators in 3 days'), numbers: [], count_line: '83 creators in 7 days, 12 posts in 3 days'},
    has: ['12 accounts posting, last 3 days'],
    lacks: ['83 creators', '83 accounts', 'accounts posting in 7 days'],
  },
  {
    name: 'a measured value that arrives as a string',
    edit: {reach: figure('1', 'creators in 3 days'), numbers: [], count_line: '1 creator, 3 days'},
    has: ['1 account posting, last 3 days'],
    lacks: ['1 creator'],
  },
];

function todayRow(edit){
  const card = {...clone(za.cards[0]), item_id: 'p'.repeat(64), title: '#parity_shape', ...edit};
  flushSync(() => root.render(<ul><StoryCard card={card} market="ZA" date="2026-09-30" /></ul>));
  return squash(host.querySelector('[data-card]'));
}

async function discoverRow(edit){
  const card = {
    ...clone(za.cards[0]), item_id: 'it_parity', title: '#parity_shape', tag: null, order: 1, date: '2026-10-20',
    explanation_status: 'explained', lifecycle: null, novelty: 'new', diffusion: 'bottom_up', origin: null,
    spread_line: null, growth: figure(1.5, 'times its usual level'), watch_id: null, ...edit,
  };
  const page = {
    date: '2026-10-20', market: 'ZA', run_id: 'r_p1', items: [card], next_cursor: null, held_back: {count: 0, items: []},
    filters: {kinds: ['hashtag'], states: ['emerging'], platforms: ['tiktok']},
  };
  const radar = {date: '2026-10-20', market: 'ZA', held_back_count: 0, note: '', window: {from: '2026-10-14', to: '2026-10-20', days: 7, platforms: []}, points: []};
  globalThis.fetch = async (url) => {
    if (String(url).startsWith('/api/discover/radar')) return reply(200, radar);
    if (String(url).startsWith('/api/discover')) return reply(200, page);
    return reply(404, {error: 'not_found', message: 'No route'});
  };
  flushSync(() => root.render(<Discover42 region="ZA" />));
  await settle();
  return squash(host.querySelector('.d42-feed [data-card]'));
}

for (const shape of SHAPES){
  test('Today: ' + shape.name, () => {
    const row = todayRow(shape.edit);
    for (const words of shape.has) expect(row).toContain(words);
    for (const words of shape.lacks) expect(row).not.toContain(words);
  });

  test('Discover: ' + shape.name, async () => {
    const row = await discoverRow(shape.edit);
    for (const words of shape.has) expect(row).toContain(words);
    for (const words of shape.lacks) expect(row).not.toContain(words);
  });
}
