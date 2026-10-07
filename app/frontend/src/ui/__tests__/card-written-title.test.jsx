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
