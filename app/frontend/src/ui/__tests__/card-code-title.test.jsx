/* Albert, 12 October 2026, "code titles": when the title check drops the
   writer's title, a card shows a plain title built in code from its cluster
   label, not the raw label. On 12 October ZA card 03 showed "bafanabafana,
   mofokeng, relebohile". The cases are the services' own
   (core/api/tests/fixtures/code_titles.json), so the app and the digest
   build the same title. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';
import todayFixture from './fixtures/today42.json';
import topicFixture from './fixtures/topic42_za.json';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {codeTitle, codeCountLine} = await import('../../codeTitle.js');
const {TrendCard, codedTitle} = await import('../TrendCard.jsx');
const {StoryCard, cardWords} = await import('../StoryCard.jsx');
const {TopicPage42} = await import('../../topic42.jsx');

const CASES = JSON.parse(readFileSync(new URL('../../../../../core/api/tests/fixtures/code_titles.json', import.meta.url), 'utf8')).cases;
const clone = (value) => JSON.parse(JSON.stringify(value));
const LABEL = 'bafanabafana, mofokeng, relebohile';
const CODED = 'Posts about Bafana Bafana, Mofokeng and Relebohile';
const COUNT = '5 creators and 11 posts in 3 days (' + LABEL + ' only)';
const explained = todayFixture.markets.flatMap((m) => m.cards).find((card) => card.explained === true && card.kind === 'hashtag');
/* ZA card 03 as Today returned it on 12 October: explained, its written title dropped by the title check. */
const za03 = () => ({...clone(explained), kind: 'topic', title: LABEL, title_written: null, count_line: COUNT});

let host = null;
let root = null;
const realFetch = globalThis.fetch;

beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  flushSync(() => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
});

for (const c of CASES){
  test('the code title: ' + c.name, () => {
    expect(codeTitle(c.card, c.card.title)).toBe(c.title);
    const shown = {...c.card, title_written: null};
    if ('count_line' in c) expect(codeCountLine(shown, c.card.title, c.card.count_line, codedTitle(shown, c.card.title))).toBe(c.count_line);
  });
}

test('a code title shows only on a card from a brief that writes titles and shows none', () => {
  expect(codedTitle(za03(), LABEL)).toBe(CODED);
  const old = za03();
  delete old.title_written;
  expect(codedTitle(old, LABEL)).toBeNull();
  expect(codedTitle({...za03(), title_written: 'MTN8 final'}, LABEL)).toBeNull();
  // A written title that is not shown (the card is not explained, or it repeats the label) leaves the code title.
  expect(codedTitle({...za03(), explained: false, title_written: 'MTN8 final'}, LABEL)).toBe(CODED);
  expect(codedTitle({...za03(), kind: 'hashtag', title: '#Shaya_Step', title_written: 'shaya step'}, '#Shaya_Step')).toBe('Posts about #Shaya_Step');
  expect(codeCountLine(old, LABEL, COUNT, codedTitle(old, LABEL))).toBe(COUNT);
  expect(codeCountLine(za03(), LABEL, COUNT, codedTitle(za03(), LABEL))).toBe('5 creators and 11 posts in 3 days (Bafana Bafana, Mofokeng and Relebohile only)');
});

function renderCard(card, props = {}){
  flushSync(() => root.render(<ol><TrendCard card={card} market="ZA" date="2026-10-12" linkTopic {...props} /></ol>));
  const node = host.querySelector('[data-card]');
  const label = node.querySelector('[data-card-label]');
  return {node, heading: node.querySelector('h3').textContent, label: label ? label.textContent : null};
}

test('a Discover card with no written title leads with its code title, and the raw label is not shown', () => {
  const got = renderCard(za03());
  expect(got.heading).toBe(CODED);
  expect(got.label).toBeNull();
  expect(got.node.textContent).not.toContain(LABEL);
  expect(got.node.textContent).toContain('(Bafana Bafana, Mofokeng and Relebohile only)');
});

test('the Watch button names the watch by the code title', () => {
  let watched = null;
  renderCard(za03(), {onWatch: (card) => { watched = card; }});
  const button = [...host.querySelectorAll('button')].find((b) => b.textContent === 'Watch');
  flushSync(() => button.dispatchEvent(new MouseEvent('click', {bubbles: true})));
  expect(watched.title).toBe(CODED);
});

test('a card from a brief written before titles, and a creator card, read as before', () => {
  const old = za03();
  delete old.title_written;
  expect(renderCard(old).heading).toBe(LABEL);
  expect(renderCard({...za03(), kind: 'creator', title: 'Thabo Dance'}).heading).toBe('Thabo Dance');
  expect(renderCard({...za03(), title: '@***, dance'}).heading).toBe('@***, dance');
});

test('Today\'s story card leads with the code title', () => {
  flushSync(() => root.render(<ol><StoryCard card={za03()} index={2} market="ZA" date="2026-10-12" todaySpecificity={null} /></ol>));
  const node = host.querySelector('[data-card]');
  expect(node.querySelector('h3').textContent).toBe(CODED);
  expect(node.querySelector('[data-card-label]')).toBeNull();
  expect(node.textContent).not.toContain(LABEL);
  const words = cardWords(za03(), 'ZA', '2026-10-12');
  expect(words.title).toBe(CODED);
  expect(words.coded).toBe(CODED);
});

test('the trend page heads the topic with its code title', async () => {
  const body = clone(topicFixture);
  body.card = {...body.card, kind: 'topic', title: LABEL, title_written: null, explained: false};
  localStorage.setItem('pulse_passcode', 'test-pass');
  globalThis.fetch = async () => ({ok: true, status: 200, json: async () => body});
  flushSync(() => root.render(<TopicPage42 itemId={body.card.item_id} market="ZA" />));
  for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0));
  expect(host.querySelector('h1').textContent).toBe(CODED);
  expect(host.querySelector('.tp42-sub').textContent).not.toContain(LABEL);
});
