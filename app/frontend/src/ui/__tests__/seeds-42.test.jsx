/* Page port, 3 October 2026. Seeds reads 42's own GET /api/seeds
   (core/api/contract.md section 19): what 42 will search for next in one
   market and what its last searches found, from seed_queue. These hold the
   page to that payload, its empty and not ready states, and the rule that
   no person's handle is ever shown. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {clearCache} = await import('../../api.js');
const {SeedsPage, validSeedsPayload} = await import('../../seeds.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;
let reads = [];

const fig = (value, unit, q = 'q_seeds_queue') => ({value, unit, query_id: q, run_id: null, result_hash: 'sha256:x'});
const lanes = [
  {lane: 'expansion', words: 'Following what is rising', seeds: fig(5, 'searches queued'), credits: fig(13, 'credits, estimated')},
  {lane: 'exploration', words: 'Trying something new', seeds: fig(1, 'searches queued'), credits: fig(5, 'credits, estimated')},
  {lane: 'anchor', words: 'Re-checking what worked', seeds: fig(1, 'searches queued'), credits: fig(1, 'credits, estimated')},
];
const queued = (label, lane, extra = {}) => ({lane, lane_words: {expansion: 'Following what is rising', exploration: 'Trying something new', anchor: 'Re-checking what worked'}[lane], kind: 'hashtag', label, item_id: null, href: null, platforms: ['TikTok'], priority: 1, credits: fig(1, 'credits, estimated'), ...extra});
const found = (label, posts, creators, extra = {}) => ({lane: 'expansion', lane_words: 'Following what is rising', kind: 'hashtag', label, item_id: null, href: null, platforms: ['TikTok'], posts: fig(posts, 'new posts found', 'q_seeds_results'), new_creators: fig(creators, 'new creators found', 'q_seeds_results'), ...extra});
const R = (v, u) => fig(v, u, 'q_seeds_results');
const PAYLOAD = {
  market: 'ZA', market_name: 'South Africa', status: 'ok', message: null, query_ids: ['q_seeds_queue', 'q_seeds_results'],
  lanes_about: [
    {lane: 'expansion', words: 'Following what is rising', about: 'Topics already rising in 42\'s counts, searched more widely.'},
    {lane: 'exploration', words: 'Trying something new', about: 'Topics from the middle of the ranking.'},
    {lane: 'anchor', words: 'Re-checking what worked', about: 'Searches that found posts last time, run again.'},
  ],
  queue: {
    seed_date: '2026-10-01', when: 'upcoming',
    seeds: [
      queued('Fixture Cola', 'expansion', {kind: 'brand', item_id: 'abc123', href: '#/t/abc123?market=ZA', platforms: ['X', 'Threads', 'Reddit'], credits: fig(5, 'credits, estimated')}),
      queued('fixture_za_anchor', 'anchor', {href: '#/seedpath/fixture_za_anchor?region=za'}),
      queued('fixture za other meme', 'exploration', {kind: 'meme', item_id: 'def456', href: '#/t/def456?market=ZA'}),
    ],
    lanes,
    creator_accounts: {seeds: fig(2, 'creator account searches queued'), credits: fig(2, 'credits, estimated')},
    seeds_total: fig(7, 'searches queued'), credits_total: fig(19, 'credits, estimated'), truncated: false,
  },
  results: {
    seed_date: '2026-09-30',
    seeds: [found('#fixture_za_heritage', 14, 3, {item_id: 'her', href: '#/t/her?market=ZA'}), found('#fixture_za_outage', 0, 0, {lane: 'exploration', lane_words: 'Trying something new'})],
    lanes: [{lane: 'expansion', words: 'Following what is rising', seeds: R(2, 'searches run'), posts: R(19, 'new posts found'), new_creators: R(4, 'new creators found')}],
    creator_accounts: {seeds: R(1, 'creator account searches run'), posts: R(5, 'new posts found'), new_creators: R(1, 'new creators found')},
    totals: {seeds: R(4, 'searches run'), posts: R(23, 'new posts found'), new_creators: R(4, 'new creators found')},
    truncated: false,
  },
  notes: [],
};

const reply = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const settle = async () => { for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setTimeout(resolve, 0)); };

async function mount(answer, props = {}){
  globalThis.fetch = async (path) => { reads.push(String(path)); return answer(String(path)); };
  flushSync(() => root.render(<SeedsPage session={0} onAuth={() => {}} {...props} />));
  await settle();
}

beforeEach(() => {
  clearCache();
  reads = [];
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

test('the page reads one market, from the app region, and South Africa when the region is all markets', async () => {
  await mount(() => reply(200, {...PAYLOAD, market: 'NG', market_name: 'Nigeria'}), {region: 'NG'});
  expect(reads).toEqual(['/api/seeds?market=NG']);
  flushSync(() => root.unmount());
  root = createRoot(host);
  clearCache();
  reads = [];
  await mount(() => reply(200, PAYLOAD), {region: 'ALL'});
  expect(reads).toEqual(['/api/seeds?market=ZA']);
});

test('the queue states the fact first: how many searches, when, and where they link', async () => {
  await mount(() => reply(200, PAYLOAD), {region: 'ZA'});
  const text = host.textContent;
  expect(text).toContain('42 will run 7 searches in South Africa on Thursday 1 October 2026');
  expect(text).toContain('Fixture Cola');
  expect(text).toContain('X, Threads, Reddit');
  expect(text).toContain('Following what is rising');
  expect(text).toContain('Trying something new');
  expect(text).toContain('Re-checking what worked');
  const cola = [...host.querySelectorAll('a')].find((a) => a.textContent === 'Fixture Cola');
  expect(cola.getAttribute('href')).toBe('#/t/abc123?market=ZA');
  const word = [...host.querySelectorAll('a')].find((a) => a.textContent === 'fixture_za_anchor');
  expect(word.getAttribute('href')).toBe('#/seedpath/fixture_za_anchor?region=za');
  const ask = [...host.querySelectorAll('a')].filter((a) => a.textContent === 'Ask about this');
  expect(ask.length).toBe(3);
  expect(ask[0].getAttribute('href')).toMatch(/^#\/ask\?q=[^&]+&market=ZA&draft=1$/);
  /* Review, 3 October 2026: the creator account searches are part of the
     7, so the line says so rather than reading as more searches. */
  expect(text).toContain('2 of these searches are of creator accounts');
  expect(text).not.toContain('Plus 2');
  expect(host.querySelector('[data-query-id="q_seeds_queue"]')).not.toBeNull();
});

/* Review, 3 October 2026: the latest queued day can already have run (the
   02:00 collection runs it before the next list is written) or be old when
   the morning run stopped, so only a day still to come says will run. */
test('a queue for today or an earlier day does not say will run', async () => {
  await mount(() => reply(200, {...PAYLOAD, queue: {...PAYLOAD.queue, when: 'today'}}), {region: 'ZA'});
  expect(host.textContent).toContain('42 queued 7 searches in South Africa for today, Thursday 1 October 2026');
  expect(host.textContent).not.toContain('will run');
  flushSync(() => root.unmount());
  root = createRoot(host);
  clearCache();
  await mount(() => reply(200, {...PAYLOAD, queue: {...PAYLOAD.queue, when: 'past'}}), {region: 'ZA'});
  expect(host.textContent).toContain('The last list 42 queued in South Africa was for Thursday 1 October 2026: 7 searches');
  expect(host.textContent).toContain('Nothing newer is queued yet');
  expect(host.textContent).not.toContain('will run');
});

test('the last results say what the searches found, by topic and in total', async () => {
  await mount(() => reply(200, PAYLOAD), {region: 'ZA'});
  const text = host.textContent;
  expect(text).toContain('The 4 searches of Wednesday 30 September 2026 found 23 new posts and 4 new creators');
  expect(text).toContain('#fixture_za_heritage');
  expect(text).toContain('14');
  expect(host.querySelector('[data-query-id="q_seeds_results"]')).not.toBeNull();
});

test('the page carries none of the old desk actions and no jargon', async () => {
  await mount(() => reply(200, PAYLOAD), {region: 'ZA'});
  const text = host.textContent;
  for (const gone of ['Generate research doc', 'Filter the Listen feed', 'Listen', 'placebo', 'Thompson', 'expansion share', 'Paste-ready prompt', 'brand play']) expect(text).not.toContain(gone);
  expect([...host.querySelectorAll('a')].some((a) => /#\/(listen|console)/.test(a.getAttribute('href') || ''))).toBe(false);
  expect(host.querySelectorAll('.legacy-action--primary').length).toBeLessThanOrEqual(1);
});

test('an empty queue says so plainly and why', async () => {
  const empty = {...PAYLOAD, market: 'KE', market_name: 'Kenya', status: 'empty', message: 'Nothing is queued for Kenya yet. 42\'s morning detect run writes the next day\'s searches.', queue: null, results: null};
  await mount(() => reply(200, empty), {region: 'KE'});
  expect(host.textContent).toContain('Nothing queued for Kenya yet');
  expect(host.textContent).toContain('morning detect run');
  expect(host.textContent).not.toContain('Seeds could not load');
});

test('not ready, from the payload or a 409, is a waiting state with no Try again', async () => {
  await mount(() => reply(200, {...PAYLOAD, status: 'not_ready', message: 'Seeds fills once 42 has written its first list of searches.', queue: null, results: null}), {region: 'ZA'});
  expect(host.textContent).toContain('Seeds is waiting for its first list');
  expect(host.textContent).toContain('Seeds fills once 42 has written its first list of searches.');
  flushSync(() => root.unmount());
  root = createRoot(host);
  clearCache();
  await mount(() => reply(409, {error: 'not_ready', message: 'Not ready yet.'}), {region: 'ZA'});
  expect(host.textContent).toContain('Seeds is waiting for its first list');
  expect([...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again')).toBe(false);
});

test('coming back to Seeds reads it again, so a waiting page shows the queue once it is written', async () => {
  await mount(() => reply(200, {...PAYLOAD, status: 'not_ready', message: 'Seeds fills once 42 has written its first list of searches.', queue: null, results: null}), {region: 'ZA'});
  expect(host.textContent).toContain('Seeds is waiting for its first list');
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount(() => reply(200, PAYLOAD), {region: 'ZA'});
  expect(reads).toEqual(['/api/seeds?market=ZA', '/api/seeds?market=ZA']);
  expect(host.textContent).not.toContain('Seeds is waiting for its first list');
  expect(host.textContent).toContain('Fixture Cola');
});

test('a seed that names a person is never shown, even if a payload carried one', async () => {
  const leaky = {...PAYLOAD, queue: {...PAYLOAD.queue, seeds: [...PAYLOAD.queue.seeds, queued('@fixture_someone', 'expansion'), queued('fixture_handle_two', 'expansion', {kind: 'creator'})]},
    results: {...PAYLOAD.results, seeds: [...PAYLOAD.results.seeds, found('fixture_handle_three', 3, 1, {kind: 'creator'})]}};
  await mount(() => reply(200, leaky), {region: 'ZA'});
  expect(host.textContent).toContain('Fixture Cola');
  expect(host.textContent).not.toContain('fixture_someone');
  expect(host.textContent).not.toContain('fixture_handle_two');
  expect(host.textContent).not.toContain('fixture_handle_three');
});

test('the payload check refuses another market and numbers that are not Figures', () => {
  expect(validSeedsPayload(PAYLOAD, 'ZA')).toBe(true);
  expect(validSeedsPayload(PAYLOAD, 'NG')).toBe(false);
  expect(validSeedsPayload({...PAYLOAD, queue: {...PAYLOAD.queue, seeds_total: 7}}, 'ZA')).toBe(false);
  expect(validSeedsPayload({...PAYLOAD, status: 'maybe'}, 'ZA')).toBe(false);
  expect(validSeedsPayload({...PAYLOAD, status: 'empty', message: '', queue: null, results: null}, 'ZA')).toBe(false);
});
