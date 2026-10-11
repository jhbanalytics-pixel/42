/* Design audit item 12, Release C: Today and Discover say "accounts posting",
   but Topic said "31 creators in 3 days" for the same figure, as did the
   cards on a Creator and a Community page and the Alerts reach rule. Every
   page that draws a trend card now says the measure the one way, and the
   reach rule and a fired alert's reason say accounts too. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import topicFixture from './fixtures/topic42_za.json';
import peopleFixture from './fixtures/people42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {renderToStaticMarkup} = await import('react-dom/server');
const {TopicPage42} = await import('../../topic42.jsx');
const {CreatorPage42, CommunityPage42} = await import('../../people42.jsx');
const {AlertsPage42} = await import('../../alerts42.jsx');
const {RuleChoice, ruleWords, ruleFrom, FIRST_CHOICE} = await import('../WatchDialog.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const text = () => host.textContent.replace(/\s+/g, ' ');
const RETIRED = /\d[\d\s]*\s+creators?\s+in\s+\d+\s+days?/i;

function serve(routes){
  globalThis.fetch = async (url) => {
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

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

async function mount(element, body){
  serve([['/api/', reply(200, body)]]);
  flushSync(() => root.render(element));
  await settle();
}

test('the topic page says accounts posting for the 3 day count', async () => {
  expect(JSON.stringify(topicFixture)).toMatch(/creators in 3 days/);
  await mount(<TopicPage42 itemId={topicFixture.card.item_id} market="ZA" />, topicFixture);
  expect(text()).toContain('accounts posting, last 3 days');
  expect(text()).not.toMatch(RETIRED);
});

test('the trend cards on a creator page say accounts posting', async () => {
  expect(JSON.stringify(peopleFixture.creator)).toMatch(/creators in 3 days/);
  await mount(<CreatorPage42 creatorId="c_ng_macro" market="NG" />, peopleFixture.creator);
  expect(text()).toContain('accounts posting, last 3 days');
  expect(text()).not.toMatch(RETIRED);
});

test('the trend cards on a community page say accounts posting', async () => {
  expect(JSON.stringify(peopleFixture.community)).toMatch(/creators in 3 days/);
  await mount(<CommunityPage42 communityId={peopleFixture.community.community.community_id} market="NG" />, peopleFixture.community);
  expect(text()).toContain('accounts posting, last 3 days');
  expect(text()).not.toMatch(RETIRED);
});

test('the reach rule counts accounts posting', () => {
  expect(ruleWords({reach_over: 50})).toBe('When reach passes 50 accounts posting in 3 days');
  expect(ruleFrom({...FIRST_CHOICE, kind: 'reach', reach: '0'}).error).toBe('Reach must be a whole number of accounts posting above 0.');
  const html = renderToStaticMarkup(<RuleChoice choice={{...FIRST_CHOICE, kind: 'reach', reach: '50'}} onChange={() => {}} />);
  const form = new window.DOMParser().parseFromString(html, 'text/html');
  const reach = form.querySelector('input[name="reach"]');
  expect(reach.getAttribute('aria-label')).toBe('Reach, accounts posting in 3 days');
  expect(reach.nextSibling.textContent.trim()).toBe('accounts posting in 3 days');
  expect(form.body.textContent).not.toMatch(/creators/);
});

test('a fired alert says its reach in accounts', async () => {
  const watch = {watch_id: 'w_r', created_at: '2026-09-29T08:00:00+02:00', who: 'passcode', target: {kind: 'brand', value: 'Castle Lager'}, market: 'NG', rule: {reach_over: 50}, label: 'Castle Lager', status: 'active'};
  const alerts = {date: '2026-09-30', alerts: [
    {watch_id: 'w_r', label: 'Castle Lager', item_id: 'item_castle', market: 'NG', fired_because: 'Reach passed 50 creators in 3 days', card: {item_id: 'item_castle'}, since: '2026-09-29'},
  ]};
  serve([['/api/watches', reply(200, {watches: [watch]})], ['/api/alerts', reply(200, alerts)]]);
  flushSync(() => root.render(<AlertsPage42 />));
  await settle();
  /* One phrasing per rule: the reason an alert fired says its count as the rule does. */
  expect(text()).toContain('Reach passed 50 accounts posting in 3 days');
  expect(text()).toContain('When reach passes 50 accounts posting in 3 days');
  expect(text()).not.toMatch(/creators in 3 days|50 creators/);
});

/* Review of item 12: Compare still said "Distinct creators in the window" and
   "5 creators in 7 days", and Method said Radar counts creators. */
test('Compare says accounts posting for its creator counts', async () => {
  const itemsFixture = (await import('./fixtures/compare42_items.json')).default;
  const {Compare42} = await import('../../compare42.jsx');
  expect(JSON.stringify(itemsFixture)).toMatch(/creators in 7 days/);
  serve([['/api/compare', reply(200, itemsFixture)], ['/api/discover', reply(200, {items: [], filters: {}})]]);
  flushSync(() => root.render(<Compare42 search="mode=items&items=i_step,i_cola,i_ring&market=ZA&days=7" region="ZA" />));
  await settle();
  const table = host.querySelector('[data-section="table"]');
  expect(table.textContent).toContain('Accounts posting in the window');
  expect(table.textContent).toContain('5 accounts posting, last 7 days');
  expect(table.textContent).toContain('31 accounts posting, last 3 days');
  expect(table.textContent).not.toMatch(/creators/i);
});

test('Method says Radar counts the accounts posting about a topic', async () => {
  const {MethodPage} = await import('../../views.jsx');
  flushSync(() => root.render(<MethodPage />));
  await settle();
  expect(text()).toContain('Radar ranks topics by how many different accounts post about them.');
  expect(text()).not.toContain('different creators');
});
