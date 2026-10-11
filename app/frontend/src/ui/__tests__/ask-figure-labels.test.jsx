/* Ask, "The numbers" panel: every tile says what it counts. Albert saw bare
   tiles ("179 posts", "118 creators") with no subject on the live page. A
   tile now joins the posts and the creators of one claim and one query in one
   sentence and names the claim they count. It carries no market or window of
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

test('the posts and the creators of one claim and one query are one tile, in one sentence', async () => {
  await open();
  expect(lines()).toEqual(['179 posts by 118 creators', '104 posts by 31 creators', '18 posts by 2 creators', '6 posts by 6 creators']);
  expect(all('.ask42-figure-tile').length).toBe(4);
});

test('each tile names the claim it counts and carries no market or window of its own', async () => {
  await open();
  const claims = clone(record).answer.claims;
  all('.ask42-figure-tile').forEach((tile, index) => {
    const about = tile.querySelector('.ask42-figure-claim');
    expect(about).not.toBeNull();
    expect(claims[index].text.startsWith(words(about).replace(/…$/, ''))).toBe(true);
    expect(about.getAttribute('title')).toBe(claims[index].text);
    expect(tile.querySelector('.ask42-figure-scope')).toBeNull();
    expect(tile.getAttribute('aria-label')).not.toMatch(/Read in|October|South Africa\./);
  });
});

test('one caption under the heading says the figures are counted in the store, not only in the cited posts', async () => {
  await open();
  expect(all('.ask42-figure-caption').length).toBe(1);
  expect(words(host.querySelector('.ask42-figure-caption'))).toBe('Counted in the store, not only in the posts cited above');
});

test('the accessible name of a tile is the whole sentence', async () => {
  await open();
  const claims = clone(record).answer.claims;
  const names = all('.ask42-figure-tile').map((tile) => tile.getAttribute('aria-label'));
  expect(names[0]).toBe('179 posts by 118 creators. Finding: ' + claims[0].text);
  expect(names[3]).toBe('6 posts by 6 creators. Finding: ' + claims[3].text);
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
  expect(lines()).toEqual(['179 posts by 118 creators', '31 creators', '18 posts by 2 creators', '6 posts by 6 creators']);
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
  expect(lines().slice(0, 3)).toEqual(['1 post by 1 creator', '59 posts with a known location', '4 creators']);
});

test('a claim with no words of its own still says what its figure belongs to', async () => {
  const body = clone(record);
  body.answer.claims[3].text = '   ';
  await open(body);
  const tile = all('.ask42-figure-tile')[3];
  expect(words(tile.querySelector('.ask42-figure-claim'))).toBe('Counted for a finding with no words of its own.');
  expect(tile.getAttribute('aria-label')).toBe('6 posts by 6 creators. Counted for a finding with no words of its own.');
});

test('a long claim is cut at a word to one short line and the whole claim stays in the title', async () => {
  const body = clone(record);
  const long = 'Posts about the Soweto unrest anniversary led the week across TikTok and X, with creators in Gauteng driving most of the volume and a second wave from the Western Cape later in the week. A second sentence follows.';
  body.answer.claims[0].text = long;
  await open(body);
  const about = all('.ask42-figure-tile')[0].querySelector('.ask42-figure-claim');
  expect(words(about).length).toBeLessThanOrEqual(91);
  expect(words(about).endsWith('…')).toBe(true);
  expect(long.startsWith(words(about).slice(0, -1).trim())).toBe(true);
  expect(about.getAttribute('title')).toBe(long);
});

test('a claim that holds an abbreviation is never cut down to the abbreviation', () => {
  expect(evidence.topicWords('Mr. Mbeki trended on X. Then the posts spread.')).toBe('Mr. Mbeki trended on X. Then the posts spread.');
  expect(evidence.topicWords('Use of #SA vs. #ZA was split.')).toBe('Use of #SA vs. #ZA was split.');
  expect(evidence.topicWords('U.S. creators posted 40 posts about it.')).toBe('U.S. creators posted 40 posts about it.');
  const long = 'Mr. Mbeki was named in a long run of posts on X vs. posts on TikTok, and the creators who named him differed by platform.';
  const cut = evidence.topicWords(long);
  expect(cut.endsWith('…')).toBe(true);
  expect(cut.length).toBeLessThanOrEqual(90);
  expect(cut.startsWith('Mr. Mbeki was named')).toBe(true);
  expect(cut).not.toMatch(/\s…$/);
  const tiles = evidence.figureTiles([{id: 'a', text: 'Mr. Mbeki trended on X. Then it faded.', numbers: [posts(40, 'q1'), creators(12, 'q1')]}]);
  expect(tiles.map((tile) => tile.about)).toEqual(['Mr. Mbeki trended on X. Then it faded.']);
});

test('figureTiles joins posts and creators only from one claim, one query, with creators no more than posts', () => {
  const claims = [
    {id: 'a', text: 'First claim.', numbers: [posts(5, 'q1'), creators(3, 'q1')]},
    {id: 'b', text: 'Second claim.', numbers: [creators(7, 'q3')]},
    {id: 'c', text: 'Third claim.', numbers: [posts(2, 'q4'), posts(9, 'q5')]},
  ];
  const tiles = evidence.figureTiles(claims);
  expect(tiles.map((tile) => tile.line)).toEqual(['5 posts by 3 creators', '7 creators', '2 posts', '9 posts']);
  expect(tiles[0].queryIds).toEqual(['q1', 'q1']);
  expect(tiles.every((tile) => tile.about.length > 0 && tile.sentence.includes(tile.line))).toBe(true);
  expect(evidence.figureTiles([])).toEqual([]);
});

test('posts from one query and creators from another in one claim stay two tiles', () => {
  const tiles = evidence.figureTiles([{id: 'a', text: 'Load shedding drew 18 posts, and 2 creators posted about Eskom.', numbers: [posts(18, 'q_ls'), creators(2, 'q_eskom')]}]);
  expect(tiles.map((tile) => tile.line)).toEqual(['18 posts', '2 creators']);
});

test('figures with no query id are not joined', () => {
  const tiles = evidence.figureTiles([
    {id: 'a', text: 'A claim.', numbers: [posts(18, ''), creators(2, '')]},
    {id: 'b', text: 'B claim.', numbers: [{value: 9, unit: 'posts'}, {value: 3, unit: 'creators'}]},
  ]);
  expect(tiles.map((tile) => tile.line)).toEqual(['18 posts', '2 creators', '9 posts', '3 creators']);
});

test('more creators than posts is not a pair', () => {
  const tiles = evidence.figureTiles([{id: 'a', text: 'A claim.', numbers: [posts(3, 'q1'), creators(30, 'q1')]}]);
  expect(tiles.map((tile) => tile.line)).toEqual(['3 posts', '30 creators']);
});

test('a tie between claims leaves no false pair: the repeated count drops out and the leftovers stay apart', () => {
  const first = {id: 'a', text: 'Eish was used by 9 creators in 13 posts.', numbers: [posts(13, 'q_a'), creators(9, 'q_a')]};
  const second = {id: 'b', text: 'Run away was used by 11 creators in 13 posts, against 4 posts in the 7-day period before.', numbers: [creators(11, 'q_now'), posts(13, 'q_now'), posts(4, 'q_before')]};
  const tied = evidence.figureTiles([first, second]).map((tile) => tile.line);
  expect(tied).toEqual(['13 posts by 9 creators', '11 creators', '4 posts']);
  expect(tied).not.toContain('4 posts by 11 creators');
});

test('a claim with three numbers joins the posts and the creators of one query and keeps the third apart', () => {
  const tiles = evidence.figureTiles([{id: 'a', text: 'A claim with a view count.', numbers: [posts(10, 'q1'), {value: 500, unit: 'views', query_id: 'q1'}, creators(4, 'q1')]}]);
  expect(tiles.map((tile) => tile.line)).toEqual(['10 posts by 4 creators', '500 views']);
});

test('two posts figures in one claim are not joined with its creators', () => {
  const tiles = evidence.figureTiles([{id: 'a', text: 'A claim.', numbers: [posts(10, 'q1'), posts(4, 'q1'), creators(3, 'q1')]}]);
  expect(tiles.map((tile) => tile.line)).toEqual(['10 posts', '4 posts', '3 creators']);
});

test('only the first eight figures are shown, so five pairs leave four tiles', () => {
  const claims = [1, 2, 3, 4, 5].map((n) => ({id: 'c' + n, text: 'Claim ' + n + '.', numbers: [posts(100 + n, 'q' + n), creators(10 + n, 'q' + n)]}));
  expect(evidence.figureTiles(claims).map((tile) => tile.line)).toEqual(['101 posts by 11 creators', '102 posts by 12 creators', '103 posts by 13 creators', '104 posts by 14 creators']);
  expect(evidence.distinctFigures(claims).length).toBe(8);
  expect(evidence.figureTiles(claims, 10).length).toBe(5);
});
