/* Ask, "The numbers" panel: every tile says what it counts. Albert saw bare
   tiles ("179 posts", "118 creators") with no subject on the live page. A
   tile now puts the posts and the accounts posting of one claim and one query
   side by side and names the claim they count, whole. The claim is never cut,
   clamped or hidden behind a toggle. It carries no market or window of
   its own, because a stored figure does not record one. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import record from './fixtures/ask42_numbers_labels.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {AskPage} = await import('../../ask42.jsx');
const evidence = await import('../../askEvidence.js');

const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/[  ]/g, ' ').replace(/\s+/g, ' ').trim();

let host = null;
let root = null;

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  window.history.replaceState(null, '', '#/');
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

function serve(body){
  const stub = async (url) => {
    const ok = new RegExp('^/api/ask/' + body.ask_id + '$').test(String(url));
    return {ok, status: ok ? 200 : 404, headers: {get: () => null}, json: async () => (ok ? body : {error: 'not_found'}), text: async () => ''};
  };
  globalThis.fetch = stub;
  window.fetch = stub;
}

async function until(check, label){
  const started = Date.now();
  for (;;){
    let ok = false;
    try { ok = Boolean(check()); } catch (_error){ ok = false; }
    if (ok) return;
    if (Date.now() - started > 4000) throw new Error('timed out waiting for ' + label);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  }
}

async function open(body = clone(record)){
  serve(body);
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{follow: body.ask_id}} />));
  await until(() => host.querySelector('.ask42-answer'), 'the answer');
  return body;
}

const all = (selector) => [...host.querySelectorAll(selector)];
const words = (node) => plain(node.textContent);
const lines = () => all('.ask42-figure-tile .ask42-figure-line').map(words);
const posts = (value, query_id) => ({value, unit: 'posts', query_id});
const creators = (value, query_id) => ({value, unit: 'creators', query_id});

test('the fixture has eight figures in four claims under the header counts of the reported page', async () => {
  await open();
  expect(words(host.querySelector('.ask42-scope'))).toContain('Answer based on 8 posts by 7 creators · 296 posts read on 5 platforms · 3 444 posts in the store');
  const numbers = clone(record).answer.claims.flatMap((claim) => claim.numbers);
  expect(numbers.length).toBe(8);
  for (const number of numbers) expect(number.run_id).toBe(clone(record).run.run_id);
});

test('the posts and the accounts posting of one claim and one query are side by side in one tile, with no "by" between them', async () => {
  await open();
  expect(lines()).toEqual(['179 posts · 118 accounts posting', '104 posts · 31 accounts posting', '18 posts · 2 accounts posting', '6 posts · 6 accounts posting']);
  expect(all('.ask42-figure-tile').length).toBe(4);
});

test('each tile names the claim it counts and carries no market or window of its own', async () => {
  await open();
  const claims = clone(record).answer.claims;
  all('.ask42-figure-tile').forEach((tile, index) => {
    const about = tile.querySelector('.ask42-figure-claim');
    expect(about).not.toBeNull();
    expect(words(about)).toBe(claims[index].text);
    expect(about.getAttribute('title')).toBeNull();
    expect(tile.querySelector('.ask42-figure-scope')).toBeNull();
    expect(tile.getAttribute('aria-label')).not.toMatch(/Read in|October|South Africa\./);
  });
});

test('one caption under the heading says the figures are counted in the store, not only in the cited posts', async () => {
  await open();
  expect(all('.ask42-figure-caption').length).toBe(1);
  expect(words(host.querySelector('.ask42-figure-caption'))).toBe('Counted in the store, not only in the posts this answer cites');
});

test('the accessible name of a tile is the whole sentence', async () => {
  await open();
  const claims = clone(record).answer.claims;
  const names = all('.ask42-figure-tile').map((tile) => tile.getAttribute('aria-label'));
  expect(names[0]).toBe('179 posts and 118 accounts posting. Finding: ' + claims[0].text);
  expect(names[3]).toBe('6 posts and 6 accounts posting. Finding: ' + claims[3].text);
  for (const name of names) expect(name).not.toMatch(/^\d[\d ]* (posts|creators)\.\s*$/);
});

test('the values stay exactly as pinned and each query id still rides on its tile', async () => {
  await open();
  const pinned = clone(record).answer.claims.flatMap((claim) => claim.numbers);
  const shown = all('.ask42-figure-tile .ask42-figure-value').map(words);
  expect(shown).toEqual(pinned.map((number) => evidence.distinctFigures([{numbers: [number]}])[0].value));
  const ids = new Set(pinned.map((number) => number.query_id));
  for (const id of ids) expect(host.querySelector('.ask42-figure-tile[data-query-id="' + id + '"]')).not.toBeNull();
  for (const id of ids) expect(words(host.querySelector('.ask42-answer')).includes(id)).toBe(false);
});

test('a figure repeated by a second claim stays once, and a lone figure still names its claim', async () => {
  const body = clone(record);
  body.answer.claims[1].numbers = [{...body.answer.claims[0].numbers[0]}, {...body.answer.claims[1].numbers[1]}];
  await open(body);
  expect(lines()).toEqual(['179 posts · 118 accounts posting', '31 accounts posting', '18 posts · 2 accounts posting', '6 posts · 6 accounts posting']);
  const lone = all('.ask42-figure-tile')[1];
  expect(words(lone.querySelector('.ask42-figure-claim'))).toContain('Springboks');
});

test('a count of one reads in the singular and a unit in words is not paired with the wrong noun', async () => {
  const body = clone(record);
  body.answer.claims[0].numbers = [
    {...body.answer.claims[0].numbers[0], value: 1, unit: 'posts'},
    {...body.answer.claims[0].numbers[1], value: 1, unit: 'creators'},
  ];
  body.answer.claims[1].numbers = [
    {...body.answer.claims[1].numbers[0], value: 59, unit: 'located_posts'},
    {...body.answer.claims[1].numbers[1], value: 4, unit: 'creators'},
  ];
  await open(body);
  expect(lines().slice(0, 3)).toEqual(['1 post · 1 account posting', '59 posts with a known location', '4 accounts posting']);
});

test('a claim with no words of its own still says what its figure belongs to', async () => {
  const body = clone(record);
  body.answer.claims[3].text = '   ';
  await open(body);
  const tile = all('.ask42-figure-tile')[3];
  expect(words(tile.querySelector('.ask42-figure-claim'))).toBe('Counted for a finding with no words of its own.');
  expect(tile.getAttribute('aria-label')).toBe('6 posts and 6 accounts posting. Counted for a finding with no words of its own.');
});

/* happy-dom does no layout, so the clamp is stubbed: a claim element reports
   the height of its text and the height of the box that holds it. */
test('a claim that holds an abbreviation is shown whole, never cut down to the abbreviation', () => {
  const claim = (text) => [{id: 'a', text, numbers: [posts(40, 'q1'), creators(12, 'q1')]}];
  for (const text of ['Mr. Mbeki trended on X. Then the posts spread.', 'Use of #SA vs. #ZA was split.', 'U.S. creators posted 40 posts about it.']){
    expect(evidence.figureTiles(claim(text)).map((tile) => tile.about)).toEqual([text]);
  }
  const long = 'Mr. Mbeki was named in a long run of posts on X vs. posts on TikTok, and the creators who named him differed by platform, week on week, for the whole month.';
  expect(evidence.figureTiles(claim(long)).map((tile) => tile.about)).toEqual([long]);
  expect(evidence.topicWords).toBeUndefined();
});

test('an answer with no figures shows no empty heading or caption', async () => {
  const body = clone(record);
  for (const claim of body.answer.claims) claim.numbers = [];
  await open(body);
  expect(host.querySelector('#ask42-figures-title')).toBeNull();
  expect(host.querySelector('.ask42-figure-caption')).toBeNull();
});

test('figureTiles puts posts and accounts posting side by side only from one claim, one query, with creators no more than posts', () => {
  const claims = [
    {id: 'a', text: 'First claim.', numbers: [posts(5, 'q1'), creators(3, 'q1')]},
    {id: 'b', text: 'Second claim.', numbers: [creators(7, 'q3')]},
    {id: 'c', text: 'Third claim.', numbers: [posts(2, 'q4'), posts(9, 'q5')]},
  ];
  const tiles = evidence.figureTiles(claims);
  expect(tiles.map((tile) => tile.line)).toEqual(['5 posts · 3 accounts posting', '7 accounts posting', '2 posts', '9 posts']);
  expect(tiles[0].queryId).toBe('q1');
  expect(tiles.every((tile) => tile.about.length > 0 && tile.sentence.startsWith(tile.line.replace(' · ', ' and ')))).toBe(true);
  expect(evidence.figureTiles([])).toEqual([]);
});

test('posts from one query and creators from another in one claim stay two tiles', () => {
  const tiles = evidence.figureTiles([{id: 'a', text: 'Load shedding drew 18 posts, and 2 creators posted about Eskom.', numbers: [posts(18, 'q_ls'), creators(2, 'q_eskom')]}]);
  expect(tiles.map((tile) => tile.line)).toEqual(['18 posts', '2 accounts posting']);
});

test('figures with no query id are not joined', () => {
  const tiles = evidence.figureTiles([
    {id: 'a', text: 'A claim.', numbers: [posts(18, ''), creators(2, '')]},
    {id: 'b', text: 'B claim.', numbers: [{value: 9, unit: 'posts'}, {value: 3, unit: 'creators'}]},
  ]);
  expect(tiles.map((tile) => tile.line)).toEqual(['18 posts', '2 accounts posting', '9 posts', '3 accounts posting']);
});

test('more creators than posts is not a pair', () => {
  const tiles = evidence.figureTiles([{id: 'a', text: 'A claim.', numbers: [posts(3, 'q1'), creators(30, 'q1')]}]);
  expect(tiles.map((tile) => tile.line)).toEqual(['3 posts', '30 accounts posting']);
});

test('a tie between claims leaves no false pair: the repeated count drops out and the leftovers stay apart', () => {
  const first = {id: 'a', text: 'Eish was used by 9 creators in 13 posts.', numbers: [posts(13, 'q_a'), creators(9, 'q_a')]};
  const second = {id: 'b', text: 'Run away was used by 11 creators in 13 posts, against 4 posts in the 7-day period before.', numbers: [creators(11, 'q_now'), posts(13, 'q_now'), posts(4, 'q_before')]};
  const tied = evidence.figureTiles([first, second]).map((tile) => tile.line);
  expect(tied).toEqual(['13 posts · 9 accounts posting', '11 accounts posting', '4 posts']);
  expect(tied).not.toContain('4 posts · 11 accounts posting');
  expect(tied.join(' ')).not.toContain(' by ');
});

test('a claim with three numbers joins the posts and the creators of one query and keeps the third apart', () => {
  const tiles = evidence.figureTiles([{id: 'a', text: 'A claim with a view count.', numbers: [posts(10, 'q1'), {value: 500, unit: 'views', query_id: 'q1'}, creators(4, 'q1')]}]);
  expect(tiles.map((tile) => tile.line)).toEqual(['10 posts · 4 accounts posting', '500 views']);
});

test('two posts figures in one claim are not joined with its creators', () => {
  const tiles = evidence.figureTiles([{id: 'a', text: 'A claim.', numbers: [posts(10, 'q1'), posts(4, 'q1'), creators(3, 'q1')]}]);
  expect(tiles.map((tile) => tile.line)).toEqual(['10 posts', '4 posts', '3 accounts posting']);
});

test('only the first eight figures are shown, so five pairs leave four tiles', () => {
  const claims = [1, 2, 3, 4, 5].map((n) => ({id: 'c' + n, text: 'Claim ' + n + '.', numbers: [posts(100 + n, 'q' + n), creators(10 + n, 'q' + n)]}));
  expect(evidence.figureTiles(claims).map((tile) => tile.line)).toEqual(['101 posts · 11 accounts posting', '102 posts · 12 accounts posting', '103 posts · 13 accounts posting', '104 posts · 14 accounts posting']);
  expect(evidence.distinctFigures(claims).length).toBe(8);
  expect(evidence.figureTiles(claims, 10).length).toBe(5);
});

test('a claim with a second creators figure never pairs its posts with the creators of another query', () => {
  const first = {id: 'a', text: 'First.', numbers: [posts(20, 'q_a'), creators(11, 'q_a')]};
  const second = {id: 'b', text: 'Second.', numbers: [creators(11, 'q_now'), posts(13, 'q_now'), creators(3, 'q_before')]};
  expect(evidence.figureTiles([first, second]).map((tile) => tile.line)).toEqual(['20 posts · 11 accounts posting', '13 posts', '3 accounts posting']);
});

test('a side by side claim whose partner repeats an earlier claim leaves the other figure alone, without error', () => {
  const first = {id: 'a', text: 'First.', numbers: [posts(13, 'q_a'), creators(9, 'q_a')]};
  const second = {id: 'b', text: 'Second.', numbers: [posts(13, 'q_b'), creators(11, 'q_b')]};
  expect(evidence.figureTiles([first, second]).map((tile) => tile.line)).toEqual(['13 posts · 9 accounts posting', '11 accounts posting']);
  const third = {id: 'c', text: 'Third.', numbers: [posts(30, 'q_c'), creators(9, 'q_c')]};
  expect(evidence.figureTiles([first, third]).map((tile) => tile.line)).toEqual(['13 posts · 9 accounts posting', '30 posts']);
});

test('a joined creators count in the singular reads "1 account posting"', () => {
  expect(evidence.figureTiles([{id: 'a', text: 'One.', numbers: [posts(1, 'q1'), creators(1, 'q1')]}]).map((tile) => tile.line)).toEqual(['1 post · 1 account posting']);
});

/* Every tile shows its whole claim. */

const WHOLE_CLAIMS = [
  ['#gqomchallenge, the dance hashtag started by Durban creators, was used by 118 creators across TikTok, Instagram and YouTube in 179 posts, against 57 posts in the 7-day period before.', 179],
  ['Amapiano dance clips from Soweto and Durban creators, including the #gqomchallenge and the Run away sound, spread across TikTok, Instagram and YouTube in 88 posts, against 12 posts in the 7-day period before.', 88],
  ['South African football, including the Premier Soccer League and the Bafana Bafana qualifier against Egypt, appeared in 240 posts by 96 creators, against 310 posts in the 7 days before.', 240],
  ['Load shedding complaints about Eskom stage 4 cuts across Gauteng and the Western Cape peaked at 412 posts on 12 March 2025, the highest day in the stored history.', 412],
  ['Eskom drew complaints across Gauteng. It had 9 posts in the 7-day period before.', 9],
  ['Posts about the Soweto unrest anniversary led the week across TikTok and X, with creators in Gauteng driving most of the volume and a second wave from the Western Cape later in the week. Load shedding was steady all week. Run away drew 4 posts in the 7-day period before.', 4],
  ['Jacob G. Zuma and Mr. Mbeki trended on X vs. #ZA with 1 200 posts this week. The week before had 200 posts.', 200],
];

test('a lone figure shows its whole claim, whatever the claim says and wherever its number is', () => {
  for (const [text, value] of WHOLE_CLAIMS){
    const [tile] = evidence.figureTiles([{id: 'a', text, numbers: [posts(value, 'q1')]}]);
    expect(tile.about).toBe(text);
    expect(tile.full).toBe(text);
    expect(tile.sentence).toBe(value + ' posts. Finding: ' + text);
  }
});

test('a joined tile shows its whole claim too', () => {
  const text = WHOLE_CLAIMS[0][0];
  const [tile] = evidence.figureTiles([{id: 'a', text, numbers: [posts(179, 'q1'), creators(118, 'q1')]}]);
  expect(tile.joined).toBe(true);
  expect(tile.about).toBe(text);
});

test('the page shows each claim whole under its figures, with no toggle and no cut', async () => {
  const body = clone(record);
  body.answer.claims.forEach((claim, index) => {
    claim.text = WHOLE_CLAIMS[index][0];
    claim.numbers = [{...claim.numbers[0], value: WHOLE_CLAIMS[index][1], unit: 'posts'}];
  });
  await open(body);
  const tiles = all('.ask42-figure-tile');
  expect(tiles.length).toBe(4);
  tiles.forEach((tile, index) => {
    const claim = tile.querySelector('.ask42-figure-claim');
    expect(words(claim)).toBe(WHOLE_CLAIMS[index][0]);
    expect(claim.getAttribute('title')).toBeNull();
    expect(claim.hasAttribute('data-open')).toBe(false);
  });
  expect(all('.ask42-figure-tile button').length).toBe(0);
  expect(all('.ask42-figure-more').length).toBe(0);
});

test('only a joined tile is marked for the smaller numerals', async () => {
  const body = clone(record);
  body.answer.claims[1].numbers = [{...body.answer.claims[1].numbers[0]}];
  await open(body);
  expect(all('.ask42-figure-tile').map((tile) => tile.hasAttribute('data-joined'))).toEqual([true, false, true, true]);
});
