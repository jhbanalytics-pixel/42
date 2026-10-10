/* Whole-diff rulings W3-5 (frontend half). Today's held list says why a topic
   was held once per group, and the group is the reason code: two topics held
   as not local, whatever their counts, are one group. The help sentence for
   rule G1 is about invalid days; a topic whose evidence could not be read has
   no invalid day, so it carries no such sentence even though the gate holds
   it under G1. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';
import trendFixture from './fixtures/trend42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TodayPage42} = await import('../../today42.jsx');

const realFetch = globalThis.fetch;
const realNow = Date.now;
const FIXTURE_DAY_NOW = Date.parse('2026-09-30T08:00:00Z');
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const G1_HELP = 'At least one of the last three days had invalid data on the main platform';

beforeEach(() => {
  localStorage.setItem('pulse_passcode', 'test-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  Date.now = realNow;
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = realFetch;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

async function mountKenya(items){
  const today = clone(todayFixture);
  const kenya = today.markets.find((market) => market.market === 'KE');
  const base = kenya.held_back.items[0];
  kenya.held_back.items = items.map((fields, index) => ({...clone(base), item_id: 'held-' + index, title: 'Held topic ' + index, held_detail: undefined, failed_reason: undefined, ...fields}));
  kenya.held_back.count = items.length;
  Date.now = () => FIXTURE_DAY_NOW;
  globalThis.fetch = async (url) => {
    if (String(url).startsWith('/api/today')) return reply(200, today);
    if (String(url).startsWith('/api/trends/')) return reply(200, trendFixture);
    return reply(404, {error: 'not_found', message: 'No route'});
  };
  flushSync(() => root.render(<TodayPage42 region="KE" date="2026-09-30" />));
  await settle();
  const details = host.querySelector('[data-market="KE"] details[data-section="held-for-evidence"]');
  return {details, groups: [...details.querySelectorAll('[data-held-group]')]};
}

const heading = (group) => group.querySelector('[data-held-group-reason]').textContent;

test('F6 a topic held because its evidence could not be read carries no invalid-day help, though the gate holds it under G1', async () => {
  const unread = {rule: 'G1', reason: 'data_issue', reason_text: 'We could not read the evidence for this trend', reason_raw: 'Evidence could not be read'};
  const invalid = {rule: 'G1', reason: 'data_issue', reason_text: 'Not enough clean data on the main platform', reason_raw: 'Data issue: 2 of the last 3 market-days invalid on the main platform'};
  const { groups } = await mountKenya([unread, invalid]);

  const unreadGroup = groups.find((group) => heading(group).startsWith('We could not read the evidence'));
  const invalidGroup = groups.find((group) => heading(group).startsWith('Not enough clean data'));
  expect(unreadGroup).toBeDefined();
  expect(unreadGroup.textContent).not.toContain(G1_HELP);
  expect(unreadGroup.textContent).not.toContain('invalid data');
  /* The positive control: the help is still said for a hold that is about invalid days. */
  expect(invalidGroup.textContent).toContain(G1_HELP);
});

test('F6 a hold that carries only the gate words, with no reason_raw, skips the help too', async () => {
  const { groups } = await mountKenya([{rule: 'G1', reason: 'data_issue', reason_text: 'Evidence could not be read', reason_raw: undefined}]);
  expect(groups).toHaveLength(1);
  expect(groups[0].textContent).not.toContain(G1_HELP);
});

test('F7 two not-local holds with different counts are one group, and each topic keeps its own counts', async () => {
  const first = {rule: 'G6', reason: 'not_local', reason_text: 'Too few of its posts were in this market (3 of 40 with a known location)'};
  const second = {rule: 'G6', reason: 'not_local', reason_text: 'Too few of its posts were in this market (7 of 20 with a known location)'};
  const { details, groups } = await mountKenya([first, second]);

  expect(groups).toHaveLength(1);
  expect(groups[0].querySelectorAll('li[data-held-item-id]')).toHaveLength(2);
  expect(heading(groups[0])).toContain('Too few of its posts were in this market');
  expect(heading(groups[0])).toContain('2 topics');
  expect(heading(groups[0])).not.toContain('3 of 40');
  const own = [...groups[0].querySelectorAll('li[data-held-item-id]')].map((item) => item.textContent);
  expect(own[0]).toContain('3 of 40 with a known location');
  expect(own[1]).toContain('7 of 20 with a known location');
  /* The chart of reasons counts the same group once. */
  expect(details.querySelectorAll('[data-held-reasons] .ch42-key-label')).toHaveLength(1);
});

test('F7 a single not-local topic still reads its own counts in the group heading', async () => {
  const { groups } = await mountKenya([{rule: 'G6', reason: 'not_local', reason_text: 'Too few of its posts were in this market (3 of 40 with a known location)'}]);
  expect(groups).toHaveLength(1);
  expect(heading(groups[0])).toContain('3 of 40 with a known location');
});

test('F7 different reason codes stay in different groups even when the words are alike', async () => {
  const notLocal = {rule: 'G6', reason: 'not_local', reason_text: 'Held for the market (3 of 40)'};
  const other = {rule: 'G5', reason: 'paid_led', reason_text: 'Held for the market (3 of 40)'};
  const { groups } = await mountKenya([notLocal, other]);
  expect(groups).toHaveLength(2);
});

test('F7 a hold whose words differ beyond the counts stays its own group', async () => {
  const placed = {rule: 'G6', reason: 'not_local', reason_text: 'Too few of its posts were in this market (3 of 40 with a known location)'};
  const global = {rule: 'G6', reason: 'not_local', reason_text: 'Mostly posted outside this market (3 of 40 posts local)'};
  const { groups } = await mountKenya([placed, global]);
  expect(groups).toHaveLength(2);
});
