/* Tester report, 6 October 2026: a Today card titled "northeast governors,
   northeast, governors" was about Independence Day reflections. A card whose
   brief wrote a checked title (contract.md section 4, title_written) leads
   with it and keeps the cluster label as smaller text; any other card reads
   as before. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TrendCard, writtenTitle} = await import('../TrendCard.jsx');

const clone = (value) => JSON.parse(JSON.stringify(value));
const explained = todayFixture.markets.flatMap((m) => m.cards).find((card) => card.explained === true);
const LABEL = 'northeast governors, northeast, governors';
const WRITTEN = 'Independence Day reflections';

let host = null;
let root = null;

beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  flushSync(() => root.unmount());
  host.remove();
});

function render(card){
  flushSync(() => root.render(<ol><TrendCard card={card} market={card.market || 'NG'} date="2026-10-06" linkTopic /></ol>));
  const node = host.querySelector('[data-card]');
  const label = node.querySelector('[data-card-label]');
  return {heading: node.querySelector('h3').textContent, label: label ? label.textContent : null};
}

test('a written title leads the card and the cluster label sits beside it', () => {
  expect(explained).toBeDefined();
  const got = render({...clone(explained), title: LABEL, title_written: WRITTEN});
  expect(got).toEqual({heading: WRITTEN, label: LABEL});
  expect(host.querySelector('h3 a.tc-title-link').textContent).toBe(WRITTEN);
});

test('a card from a brief without a written title reads as before', () => {
  const card = {...clone(explained), title: LABEL};
  delete card.title_written;
  expect(render(card)).toEqual({heading: LABEL, label: null});
  expect(render({...card, title_written: null})).toEqual({heading: LABEL, label: null});
});

test('a written title shows only on an explained card and never repeats its label', () => {
  const held = {...clone(explained), title: LABEL, title_written: WRITTEN, explained: false, explanation: null, claims: []};
  expect(render(held)).toEqual({heading: LABEL, label: null});
  expect(render({...clone(explained), title: '#Shaya_Step', title_written: 'shaya step'})).toEqual({heading: '#Shaya_Step', label: null});
  expect(writtenTitle({explained: true, title_written: '   '}, LABEL)).toBeNull();
  expect(writtenTitle({explained: true, title_written: 42}, LABEL)).toBeNull();
});

/* Wave 8 T6 (R0235, R0267): the Ask link and the Watch label name the topic as
   the card does. A question or a watch built from the raw cluster label would
   say "northeast governors, northeast, governors" under a card headed
   "Independence Day reflections". */
const ASK = 'What is behind ' + LABEL + ' in Nigeria this week?';

function renderWith(card, props = {}){
  flushSync(() => root.render(<ol><TrendCard card={card} market={card.market || 'NG'} date="2026-10-06" linkTopic {...props} /></ol>));
  return host.querySelector('[data-card]');
}

test('the Ask link asks about the written title when the card shows one', () => {
  const node = renderWith({...clone(explained), title: LABEL, title_written: WRITTEN, ask: ASK});
  const link = [...node.querySelectorAll('a')].find((a) => a.textContent === 'Ask about this');
  const q = new URLSearchParams(link.getAttribute('href').split('?')[1]).get('q');
  expect(q).toBe('What is behind ' + WRITTEN + ' in Nigeria this week?');
});

test('the Ask link keeps the server question when the card has no written title or the question does not hold the label', () => {
  const plain = {...clone(explained), title: LABEL, ask: ASK};
  delete plain.title_written;
  const read = (card) => new URLSearchParams([...renderWith(card).querySelectorAll('a')].find((a) => a.textContent === 'Ask about this').getAttribute('href').split('?')[1]).get('q');
  expect(read(plain)).toBe(ASK);
  expect(read({...plain, title_written: WRITTEN, ask: 'Which sounds are rising?'})).toBe('Which sounds are rising?');
  expect(read({...plain, title_written: WRITTEN, ask: undefined})).toBe(WRITTEN);
});

test('Watch is opened for the written title', () => {
  const seen = [];
  const node = renderWith({...clone(explained), title: LABEL, title_written: WRITTEN}, {onWatch: (card) => seen.push(card.title)});
  const watch = [...node.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Watch');
  flushSync(() => watch.dispatchEvent(new MouseEvent('click', {bubbles: true})));
  expect(seen).toEqual([WRITTEN]);
});

/* Wave 8 N10 (frontend part): a topic that has only its neutral birth label is
   not given that label as a name. */
test('a neutral Topic label is not shown as the topic name', () => {
  const node = renderWith({...clone(explained), title: 'Topic 4558a7fe', ask: undefined});
  expect(node.querySelector('h3').textContent).toBe('A topic not yet named');
  expect(node.querySelector('[data-card-label]').textContent).toBe('Topic 4558a7fe');
});

test('a neutral label is replaced by a written title when there is one', () => {
  const node = renderWith({...clone(explained), title: 'Topic 4558a7fe', title_written: WRITTEN});
  expect(node.querySelector('h3').textContent).toBe(WRITTEN);
});
