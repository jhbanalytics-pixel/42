/* Page port, 3 October 2026: Lexicon reads GET /api/lexicon?market=XX
   (core/api/contract.md section 20), the words and hashtags 42 records in one
   market, and sends each one to Seed path for its posts. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {clearCache} = await import('../../api.js');
const {LexiconPage} = await import('../../lexicon.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;
let calls = [];

const reply = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const settle = async () => { for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setTimeout(resolve, 0)); };
const fig = (value, unit) => ({value, unit, query_id: 'q_lexicon', run_id: 'r1', result_hash: 'sha256:x'});

const term = (id, label, kind, posts, week, prior, change, extra = {}) => ({
  item_id: id, label, kind, kind_word: kind === 'hashtag' ? 'Hashtag' : 'Word or phrase',
  seed_term: label.replace(/^#/, '').toLowerCase(), topic_href: '#/t/' + id + '?market=ZA', first_seen: '2026-08-28',
  posts: fig(posts, 'posts in 28 days'), week_posts: fig(week, 'posts in the last 7 days'),
  prior_week_posts: fig(prior, 'posts in the 7 days before'), change, ...extra,
});

const PAYLOAD = {
  market: 'ZA', market_name: 'South Africa', status: 'ok', message: null, query_id: 'q_lexicon', run_id: 'r1',
  window: {from: '2026-09-03', to: '2026-09-30', days: 28, week: {from: '2026-09-24', to: '2026-09-30'}, prior_week: {from: '2026-09-17', to: '2026-09-23'}},
  kinds: ['meme', 'hashtag'], truncated: false, notes: ['First seen is the day 42 first recorded the item in any market.'],
  terms: [
    term('a1', '#Amapiano', 'hashtag', 1240, 400, 320, {percent: 25, reason: null, text: 'Up 25% on the week before'}),
    term('b2', 'sharp sharp', 'meme', 24, 12, 8, {percent: 50, reason: null, text: 'Up 50% on the week before'}),
    term('c3', 'eish wena', 'meme', 3, 3, 0, {percent: null, reason: 'no_earlier_posts', text: 'No posts in the week before, so there is no change to work out.'}),
  ],
};

async function mount(answer, props = {}){
  calls = [];
  globalThis.fetch = async (url) => { calls.push(String(url)); return answer(String(url)); };
  flushSync(() => root.render(<LexiconPage region="ZA" session={0} onAuth={() => {}} {...props} />));
  await settle();
}

const links = () => [...host.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')]);

beforeEach(() => {
  clearCache();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
  globalThis.fetch = realFetch;
  clearCache();
});

afterAll(() => GlobalRegistrator.unregister());

test('the page reads the market lexicon and lists its terms, most posts first, with real counts', async () => {
  await mount(() => reply(200, PAYLOAD));
  expect(calls).toEqual(['/api/lexicon?market=ZA']);
  const names = [...host.querySelectorAll('.lex-term-name')].map((n) => n.textContent);
  expect(names).toEqual(['#Amapiano', 'sharp sharp', 'eish wena']);
  const first = host.querySelector('.lex-term-card');
  expect(first.textContent).toContain('1\u00a0240 posts');
  expect(first.textContent).toContain('Hashtag');
  expect(first.textContent).toContain('▲ 25%');
  expect(host.textContent).toContain('South Africa');
  expect(host.textContent).toContain('3 September 2026 to 30 September 2026');
  expect(host.querySelector('[data-query-id="q_lexicon"]') !== null).toBe(true);
  expect(host.querySelector('.lex-decode')).toBeNull();
});

test('a term with no earlier week shows no percentage', async () => {
  await mount(() => reply(200, PAYLOAD));
  const card = [...host.querySelectorAll('.lex-term-card')].find((c) => c.textContent.includes('eish wena'));
  expect(card.textContent).not.toMatch(/%/);
  expect(card.textContent).toContain('New this week');
});

/* Review, 3 October 2026: a quiet week before does not make a term new when
   it had posts earlier in the window. */
test('a term with older posts and a quiet week before is not called new', async () => {
  const older = {...PAYLOAD, terms: [term('d4', 'older word', 'meme', 10, 3, 0, {percent: null, reason: 'no_earlier_posts', text: 'No posts in the week before, so there is no change to work out.'})]};
  await mount(() => reply(200, older));
  const card = host.querySelector('.lex-term-card');
  expect(card.textContent).toContain('older word');
  expect(card.textContent).not.toContain('New this week');
});

test('a picked term shows its counts, its definition and links to its posts and topic', async () => {
  await mount(() => reply(200, PAYLOAD), {term: 'amapiano'});
  const panel = host.querySelector('.lex-decode');
  expect(panel !== null).toBe(true);
  expect(panel.querySelector('h2').textContent).toBe('#Amapiano');
  expect(panel.textContent).toContain('South Africa · last 28 days');
  expect(panel.textContent).toContain('1\u00a0240 posts in the last 28 days');
  expect(panel.textContent).toContain('400 in the last 7 days, 320 in the 7 days before');
  expect(panel.textContent).toContain('South African house genre built on the log drum');
  expect(panel.textContent).toContain('First recorded by 42 on 28 August 2026');
  expect(links()).toContainEqual(['See the posts', '#/seedpath/amapiano?region=za']);
  expect(links()).toContainEqual(['Open its topic page', '#/t/a1?market=ZA']);
  expect(links().some(([, href]) => String(href).startsWith('#/listen'))).toBe(false);
  const card = [...host.querySelectorAll('.lex-term-card')].find((c) => c.textContent.includes('#Amapiano'));
  expect(card.getAttribute('aria-pressed')).toBe('true');
});

test('a term without a written definition says so plainly', async () => {
  await mount(() => reply(200, PAYLOAD), {term: 'sharp sharp'});
  const panel = host.querySelector('.lex-decode');
  expect(panel.textContent).toContain('42 has no written definition for this term yet.');
  expect(links()).toContainEqual(['See the posts', '#/seedpath/sharp%20sharp?region=za']);
});

test('a term the list does not hold still opens Seed path and says it is not listed', async () => {
  await mount(() => reply(200, PAYLOAD), {term: 'wahala'});
  const panel = host.querySelector('.lex-decode');
  expect(panel.textContent).toContain('is not among the words and hashtags listed for South Africa');
  expect(links()).toContainEqual(['See the posts', '#/seedpath/wahala?region=za']);
});

test('an empty market says so in the producer\'s words and offers Discover', async () => {
  await mount(() => reply(200, {...PAYLOAD, status: 'empty', terms: [], notes: [], message: '42 has not recorded posts for any word or hashtag in Kenya in the last 28 days.'}), {region: 'KE'});
  expect(calls).toEqual(['/api/lexicon?market=KE']);
  expect(host.querySelector('[data-state-frame="empty"]') !== null).toBe(true);
  expect(host.textContent).toContain('42 has not recorded posts for any word or hashtag in Kenya in the last 28 days.');
  expect([...host.querySelectorAll('button')].some((b) => b.textContent === 'Open Discover')).toBe(true);
});

test('a lexicon with no daily count yet is not ready, not an error', async () => {
  await mount(() => reply(409, {error: 'not_ready', message: "The lexicon fills after 42's first daily count of posts has finished."}));
  expect(host.querySelector('[data-state-frame="not-ready"]') !== null).toBe(true);
  expect(host.textContent).toContain('The lexicon is not ready yet');
  expect(host.textContent).not.toContain('could not be read');
});

test('a lexicon whose views do not exist yet is not ready, in the producer\'s words', async () => {
  await mount(() => reply(200, {...PAYLOAD, status: 'not_ready', terms: [], notes: [], message: 'The lexicon fills once 42 has counted posts.'}));
  expect(host.querySelector('[data-state-frame="not-ready"]') !== null).toBe(true);
  expect(host.textContent).toContain('The lexicon fills once 42 has counted posts.');
});

test('all markets is not a market: the page asks for one and reads nothing', async () => {
  await mount(() => reply(200, PAYLOAD), {region: 'ALL'});
  expect(calls).toEqual([]);
  expect(host.textContent).toContain('Choose one market');
  const za = [...host.querySelectorAll('.lex-markets button')].find((b) => b.textContent === 'South Africa');
  window.location.hash = '#/lexicon?region=all';
  za.click();
  expect(window.location.hash).toBe('#/lexicon?region=za');
});

test('the page has at most one filled action and no serif heading', async () => {
  await mount(() => reply(200, PAYLOAD), {term: 'amapiano'});
  expect(host.querySelectorAll('.legacy-action--primary').length <= 1).toBe(true);
  expect(host.innerHTML).not.toContain('var(--serif)');
});

/* Charts, 3 October 2026: this week against last week per term, one row per
   listed term in the page's order, the largest rise marked as focus. */
test('a dumbbell shows this week against last week for each listed term', async () => {
  await mount(() => reply(200, PAYLOAD));
  const chart = host.querySelector('.ch42-dumbbell');
  expect(chart !== null).toBe(true);
  expect(chart.textContent).toContain('This week against last week');
  expect(chart.textContent).toContain('Last week');
  expect(chart.textContent).toContain('This week');
  const labels = [...chart.querySelectorAll('ol .ch42-bar-label')].map((n) => n.textContent);
  expect(labels).toEqual(['#Amapiano', 'sharp sharp', 'eish wena']);
  const values = [...chart.querySelectorAll('ol .ch42-bar-value')].map((n) => n.textContent);
  expect(values).toEqual(['320 to 400', '8 to 12', '0 to 3']);
});

test('no dumbbell while 42 has under two full weeks to compare', async () => {
  const warming = {percent: null, reason: 'warming_up', text: '42 has not collected for two full weeks yet, so there is no earlier week to compare with.'};
  await mount(() => reply(200, {...PAYLOAD, terms: PAYLOAD.terms.map((t) => ({...t, change: warming}))}));
  expect(host.querySelector('.ch42-dumbbell')).toBeNull();
  expect(host.querySelectorAll('.lex-grid > *').length).toBeGreaterThan(0);
});

test('the dumbbell lists at most the first 12 terms', async () => {
  const many = Array.from({length: 15}, (_, i) => term('id' + i, 'word ' + i, 'meme', 100 - i, 5, 2, {percent: 150, reason: null, text: 'Up'}));
  await mount(() => reply(200, {...PAYLOAD, terms: many}));
  const labels = [...host.querySelectorAll('.ch42-dumbbell ol .ch42-bar-label')].map((n) => n.textContent);
  expect(labels).toEqual(many.slice(0, 12).map((t) => t.label));
});
