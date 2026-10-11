/* Ask, "The numbers" panel: every tile says what it counts. Albert saw bare
   tiles ("179 posts", "118 creators") with no subject on the live page. A
   tile now pairs the posts and the creators of one claim in one sentence and
   names the claim they count and the window they were read in. */
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

test('the fixture is the page Albert saw: eight figures, four claims, the same header', async () => {
  await open();
  expect(words(host.querySelector('.ask42-scope'))).toContain('Answer based on 8 posts by 7 creators · 296 posts read on 5 platforms · 3 444 posts in the store');
  expect(clone(record).answer.claims.flatMap((claim) => claim.numbers).length).toBe(8);
});

test('the posts and the creators of one claim are one tile, in one sentence', async () => {
  await open();
  expect(lines()).toEqual(['179 posts by 118 creators', '104 posts by 31 creators', '18 posts by 2 creators', '6 posts by 6 creators']);
  expect(all('.ask42-figure-tile').length).toBe(4);
});

test('each tile names the claim it counts and the window and market it was read in', async () => {
  await open();
  const claims = clone(record).answer.claims;
  all('.ask42-figure-tile').forEach((tile, index) => {
    const about = tile.querySelector('.ask42-figure-about');
    expect(about).not.toBeNull();
    expect(claims[index].text.startsWith(words(about).replace(/…$/, ''))).toBe(true);
    expect(about.getAttribute('title')).toBe(claims[index].text);
    expect(words(tile.querySelector('.ask42-figure-scope'))).toBe('South Africa, 5 to 11 October 2026');
  });
});

test('the accessible name of a tile is the whole sentence', async () => {
  await open();
  const claims = clone(record).answer.claims;
  const names = all('.ask42-figure-tile').map((tile) => tile.getAttribute('aria-label'));
  expect(names[0]).toBe('179 posts by 118 creators. Finding: ' + claims[0].text + ' Read in South Africa, 5 to 11 October 2026.');
  expect(names[3]).toBe('6 posts by 6 creators. Finding: ' + claims[3].text + ' Read in South Africa, 5 to 11 October 2026.');
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
  expect(words(lone.querySelector('.ask42-figure-about'))).toContain('Springboks');
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

test('a claim with no words of its own still says where its figure came from', async () => {
  const body = clone(record);
  body.answer.claims[3].text = '   ';
  await open(body);
  const tile = all('.ask42-figure-tile')[3];
  expect(words(tile.querySelector('.ask42-figure-about'))).toBe('Counted by a search this answer ran, not tied to a named finding.');
  expect(tile.getAttribute('aria-label')).toContain('Counted by a search this answer ran');
});

test('a long claim shows its first sentence in one short line and keeps the whole claim in the title', async () => {
  const body = clone(record);
  const long = 'Posts about the Soweto unrest anniversary led the week across TikTok and X, with creators in Gauteng driving most of the volume and a second wave from the Western Cape later in the week. A second sentence follows.';
  body.answer.claims[0].text = long;
  await open(body);
  const about = all('.ask42-figure-tile')[0].querySelector('.ask42-figure-about');
  expect(words(about).length).toBeLessThanOrEqual(91);
  expect(words(about).endsWith('…')).toBe(true);
  expect(about.getAttribute('title')).toBe(long);
});

test('no market and no window leaves the scope line out and nothing bare behind', async () => {
  const body = clone(record);
  body.market = '';
  delete body.run.window;
  await open(body);
  const tile = all('.ask42-figure-tile')[0];
  expect(tile.querySelector('.ask42-figure-scope')).toBeNull();
  expect(tile.querySelector('.ask42-figure-about')).not.toBeNull();
});

test('figureTiles pairs only a posts figure and a creators figure of the same claim', () => {
  const claims = [
    {id: 'a', text: 'First claim.', numbers: [{value: 5, unit: 'posts', query_id: 'q1'}, {value: 3, unit: 'creators', query_id: 'q2'}]},
    {id: 'b', text: 'Second claim.', numbers: [{value: 7, unit: 'creators', query_id: 'q3'}]},
    {id: 'c', text: 'Third claim.', numbers: [{value: 2, unit: 'posts', query_id: 'q4'}, {value: 9, unit: 'posts', query_id: 'q5'}]},
  ];
  const tiles = evidence.figureTiles(claims, 'Kenya, 1 to 7 October 2026');
  expect(tiles.map((tile) => tile.line)).toEqual(['5 posts by 3 creators', '7 creators', '2 posts', '9 posts']);
  expect(tiles[0].queryIds).toEqual(['q1', 'q2']);
  expect(tiles.every((tile) => tile.about.length > 0 && tile.sentence.includes(tile.line))).toBe(true);
  expect(evidence.figureTiles([], 'x')).toEqual([]);
});
