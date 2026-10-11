/* Ask, "The numbers" panel: every tile says what it counts. Albert saw bare
   tiles ("179 posts", "118 creators") with no subject on the live page. A
   tile now puts the posts and the accounts posting of one claim and one query
   side by side and names the claim they count. A closed lone tile shows the
   sentence that holds its own number. It carries no market or window of
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
const {FigureTiles} = await import('../AskBriefing.jsx');
const {flushSync} = await import('react-dom');

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
function stubLayout(heights){
  const proto = window.HTMLElement.prototype;
  const claim = (node) => node.classList && node.classList.contains('ask42-figure-claim');
  const saved = ['scrollHeight', 'clientHeight'].map((name) => [name, Object.getOwnPropertyDescriptor(proto, name)]);
  Object.defineProperty(proto, 'scrollHeight', {configurable: true, get(){ return claim(this) ? heights.scroll(this) : 0; }});
  Object.defineProperty(proto, 'clientHeight', {configurable: true, get(){ return claim(this) ? heights.client(this) : 0; }});
  return () => {
    for (const [name, descriptor] of saved){
      if (descriptor) Object.defineProperty(proto, name, descriptor);
      else delete proto[name];
    }
  };
}

const resize = async () => { await act(async () => { window.dispatchEvent(new window.Event('resize')); }); };

test('a clipped claim shows its whole text, and its toggle opens and closes it for touch and keyboard users', async () => {
  const restore = stubLayout({scroll: () => 90, client: () => 60});
  try {
    const body = clone(record);
    const long = 'Posts about the Soweto unrest anniversary led the week across TikTok and X, with creators in Gauteng driving most of the volume and a second wave from the Western Cape later in the week. A second sentence follows.';
    body.answer.claims[0].text = long;
    await open(body);
    const tile = all('.ask42-figure-tile')[0];
    expect(words(tile.querySelector('.ask42-figure-claim'))).toBe(long);
    expect(tile.querySelector('.ask42-figure-claim').getAttribute('title')).toBeNull();
    const toggle = tile.querySelector('button.ask42-figure-more');
    expect(toggle).not.toBeNull();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(tile.querySelector('.ask42-figure-claim').getAttribute('data-open')).toBeNull();
    await act(async () => toggle.click());
    expect(tile.querySelector('button.ask42-figure-more').getAttribute('aria-expanded')).toBe('true');
    expect(tile.querySelector('.ask42-figure-claim').getAttribute('data-open')).toBe('');
    expect(words(tile.querySelector('button.ask42-figure-more'))).toBe('Show less');
    await act(async () => tile.querySelector('button.ask42-figure-more').click());
    expect(tile.querySelector('.ask42-figure-claim').getAttribute('data-open')).toBeNull();
    expect(words(tile.querySelector('button.ask42-figure-more'))).toBe('Show all');
  } finally { restore(); }
});

test('a claim that fits its box has no toggle, however many characters it has', async () => {
  const restore = stubLayout({scroll: () => 60, client: () => 60});
  try {
    const body = clone(record);
    body.answer.claims[0].text = 'A claim that runs to a great many characters but which the rail still holds in its three lines, a long way past a hundred and ten.';
    await open(body);
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(0);
  } finally { restore(); }
});

test('the toggle follows the layout: only the claims whose text is taller than their box have one', async () => {
  const restore = stubLayout({scroll: (node) => (node.textContent.length > 100 ? 90 : 40), client: () => 60});
  try {
    await open();
    const tiles = all('.ask42-figure-tile');
    const long = clone(record).answer.claims.map((claim) => claim.text.length > 100);
    expect(long).toEqual([true, false, false, false]);
    tiles.forEach((tile, index) => expect(Boolean(tile.querySelector('button.ask42-figure-more'))).toBe(long[index]));
  } finally { restore(); }
});

test('the toggle is measured again when the window is resized', async () => {
  let clipped = false;
  const restore = stubLayout({scroll: () => (clipped ? 90 : 60), client: () => 60});
  try {
    await open();
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(0);
    clipped = true;
    await resize();
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(4);
    clipped = false;
    await resize();
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(0);
  } finally { restore(); }
});

test('an open claim keeps its Show less button when a resize makes it fit', async () => {
  let clipped = true;
  const restore = stubLayout({scroll: () => (clipped ? 90 : 60), client: () => 60});
  try {
    await open();
    await act(async () => all('.ask42-figure-tile button.ask42-figure-more')[0].click());
    clipped = false;
    await resize();
    const buttons = all('.ask42-figure-tile button.ask42-figure-more');
    expect(buttons.length).toBe(1);
    expect(words(buttons[0])).toBe('Show less');
  } finally { restore(); }
});

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

/* ---- a lone figure's own number stays in view while the claim is closed ---- */

const LONG_FIRST = 'Posts about the Soweto unrest anniversary led the week across TikTok and X, with creators in Gauteng driving most of the volume and a second wave from the Western Cape later in the week.';
const lone = (text, ...numbers) => [{id: 'a', text, numbers}];

test('the sentence that holds a lone figure comes first, whole, when the claim is closed', () => {
  const text = LONG_FIRST + ' Load shedding was steady all week. Run away drew 4 posts in the 7-day period before.';
  const [tile] = evidence.figureTiles(lone(text, posts(4, 'q1')));
  expect(tile.about).toBe('…Run away drew 4 posts in the 7-day period before.');
  expect(tile.about).toContain('4 posts');
  expect(tile.trimmed).toBe(true);
  expect(tile.full).toBe(text);
  expect(tile.sentence).toBe('4 posts. Finding: ' + text);
});

test('a figure in the first sentence, early enough to be seen, leaves the claim whole', () => {
  const text = 'Load shedding drew 18 posts across the week. ' + LONG_FIRST;
  const [tile] = evidence.figureTiles(lone(text, posts(18, 'q1')));
  expect(tile.about).toBe(text);
  expect(tile.trimmed).toBe(false);
});

test('a figure deep in one long sentence starts the excerpt at its clause, at a word', () => {
  const text = 'Run away was used by 41 creators across TikTok and Instagram and a long list of other places in 63 posts over the whole week, against 4 posts in the 7-day period before.';
  const [tile] = evidence.figureTiles(lone(text, posts(4, 'q1')));
  expect(tile.about).toBe('…against 4 posts in the 7-day period before.');
  const plainText = 'A long run of words with no commas at all that goes on and on and on and on and on and on and on until it reaches 4 posts in the end of the sentence.';
  const [late] = evidence.figureTiles(lone(plainText, posts(4, 'q1')));
  expect(late.about.startsWith('…')).toBe(true);
  expect(late.about).toContain('4 posts');
  const rest = late.about.slice(1);
  const start = plainText.indexOf(rest);
  expect(start).toBeGreaterThan(0);
  expect(plainText[start - 1]).toBe(' ');
  expect(rest[0]).not.toBe(' ');
});

test('a number is matched whole: 4 is not found inside 41 or 14, and 1,200 is found for 1200', () => {
  const [a] = evidence.figureTiles(lone(LONG_FIRST + ' Then 41 creators and 14 posts agreed. Later 4 posts did.', posts(4, 'q1')));
  expect(a.about).toBe('…Later 4 posts did.');
  const [b] = evidence.figureTiles(lone(LONG_FIRST + ' TikTok carried the most posts, 1,200 of them.', posts(1200, 'q1')));
  expect(b.about).toBe('…TikTok carried the most posts, 1,200 of them.');
  const whole = LONG_FIRST + ' Then 40 posts came. Nothing about the count here.';
  const [c] = evidence.figureTiles(lone(whole, posts(4, 'q1')));
  expect(c.trimmed).toBe(false);
  expect(c.about).toBe(whole);
});

test('an abbreviation does not end a sentence when the excerpt is taken', () => {
  const [tile] = evidence.figureTiles(lone(LONG_FIRST + ' Mr. Mbeki trended on X vs. #ZA with 412 posts in March 2025.', posts(412, 'q1')));
  expect(tile.about).toBe('…Mr. Mbeki trended on X vs. #ZA with 412 posts in March 2025.');
});

test('a closed lone tile shows its own number and Show all opens the whole claim', async () => {
  const restore = stubLayout({scroll: () => 60, client: () => 60});
  try {
    const body = clone(record);
    const text = LONG_FIRST + ' Load shedding was steady all week. Run away drew 4 posts in the 7-day period before.';
    body.answer.claims[3].text = text;
    body.answer.claims[3].numbers = [{...body.answer.claims[3].numbers[0], value: 4, unit: 'posts'}];
    await open(body);
    const tile = all('.ask42-figure-tile')[3];
    expect(words(tile.querySelector('.ask42-figure-claim'))).toBe('…Run away drew 4 posts in the 7-day period before.');
    const toggle = tile.querySelector('button.ask42-figure-more');
    expect(toggle).not.toBeNull();
    await act(async () => toggle.click());
    expect(words(tile.querySelector('.ask42-figure-claim'))).toBe(text);
    await act(async () => tile.querySelector('button.ask42-figure-more').click());
    expect(words(tile.querySelector('.ask42-figure-claim'))).toBe('…Run away drew 4 posts in the 7-day period before.');
    expect(tile.getAttribute('aria-label')).toBe('4 posts. Finding: ' + text);
  } finally { restore(); }
});

test('a joined tile keeps its whole claim, and only a joined tile is marked for the smaller numerals', async () => {
  const restore = stubLayout({scroll: () => 60, client: () => 60});
  try {
    const body = clone(record);
    body.answer.claims[1].numbers = [{...body.answer.claims[1].numbers[0]}];
    await open(body);
    const tiles = all('.ask42-figure-tile');
    expect(tiles.map((tile) => tile.hasAttribute('data-joined'))).toEqual([true, false, true, true]);
    expect(words(tiles[0].querySelector('.ask42-figure-claim'))).toBe(clone(record).answer.claims[0].text);
  } finally { restore(); }
});

/* ---- the Show all toggle ---- */

test('the toggle is on the first frame, measured before the page paints', () => {
  const restore = stubLayout({scroll: () => 90, client: () => 60});
  globalThis.IS_REACT_ACT_ENVIRONMENT = false;
  try {
    const tiles = evidence.figureTiles(lone('A claim that is clipped.', posts(5, 'q1')));
    flushSync(() => root.render(<FigureTiles tiles={tiles} />));
    expect(host.querySelector('button.ask42-figure-more')).not.toBeNull();
  } finally {
    restore();
    flushSync(() => root.render(null));
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  }
});

test('the layout stub gives back what it replaced', () => {
  const proto = window.HTMLElement.prototype;
  const before = ['scrollHeight', 'clientHeight'].map((name) => Object.getOwnPropertyDescriptor(proto, name));
  stubLayout({scroll: () => 1, client: () => 1})();
  expect(['scrollHeight', 'clientHeight'].map((name) => Object.getOwnPropertyDescriptor(proto, name))).toEqual(before);
  expect(typeof document.createElement('div').clientHeight).toBe('number');
});

test('a one pixel overhang is not clipping and two pixels are', async () => {
  let overhang = 1;
  const restore = stubLayout({scroll: () => 60 + overhang, client: () => 60});
  try {
    await open();
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(0);
    overhang = 2;
    await resize();
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(4);
  } finally { restore(); }
});

test('while a claim is open a resize does not measure it, because the open box is not the clamped box', async () => {
  const restore = stubLayout({scroll: (node) => (node.hasAttribute('data-open') ? 60 : 90), client: () => 60});
  try {
    await open();
    await act(async () => all('.ask42-figure-tile button.ask42-figure-more')[0].click());
    await resize();
    const showLess = all('.ask42-figure-tile button.ask42-figure-more')[0];
    expect(words(showLess)).toBe('Show less');
    await act(async () => showLess.click());
    const buttons = all('.ask42-figure-tile button.ask42-figure-more');
    expect(buttons.length).toBe(4);
    expect(words(buttons[0])).toBe('Show all');
    expect(buttons[0]).toBe(showLess);
  } finally { restore(); }
});

test('closing a claim measures it again, so a claim that now fits loses its button', async () => {
  let clipped = true;
  const restore = stubLayout({scroll: () => (clipped ? 90 : 60), client: () => 60});
  try {
    await open();
    await act(async () => all('.ask42-figure-tile button.ask42-figure-more')[0].click());
    clipped = false;
    await act(async () => all('.ask42-figure-tile button.ask42-figure-more')[0].click());
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(3);
  } finally { restore(); }
});

test('focus stays on the toggle after Show less when a resize made the claim fit', async () => {
  let clipped = true;
  const restore = stubLayout({scroll: () => (clipped ? 90 : 60), client: () => 60});
  try {
    await open();
    const toggle = all('.ask42-figure-tile button.ask42-figure-more')[0];
    await act(async () => { toggle.focus(); });
    await act(async () => { toggle.click(); });
    clipped = false;
    await resize();
    const less = all('.ask42-figure-tile button.ask42-figure-more')[0];
    expect(words(less)).toBe('Show less');
    expect(document.activeElement).toBe(less);
    await act(async () => less.click());
    const kept = all('.ask42-figure-tile button.ask42-figure-more')[0];
    expect(kept).toBe(less);
    expect(document.activeElement).toBe(kept);
    await act(async () => { kept.blur(); });
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(0);
  } finally { restore(); }
});

test('the toggle is measured again when the claim box changes size, and the watcher is let go on unmount', async () => {
  const watchers = [];
  const RealObserver = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class {
    constructor(callback){ this.callback = callback; this.gone = false; watchers.push(this); }
    observe(){}
    disconnect(){ this.gone = true; }
  };
  let clipped = false;
  const restore = stubLayout({scroll: () => (clipped ? 90 : 60), client: () => 60});
  try {
    await open();
    expect(watchers.length).toBeGreaterThan(0);
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(0);
    clipped = true;
    await act(async () => { for (const watcher of watchers.filter((item) => !item.gone)) watcher.callback(); });
    expect(all('.ask42-figure-tile button.ask42-figure-more').length).toBe(4);
    await act(async () => root.unmount());
    expect(watchers.every((watcher) => watcher.gone)).toBe(true);
    root = createRoot(host);
  } finally {
    restore();
    if (RealObserver) globalThis.ResizeObserver = RealObserver; else delete globalThis.ResizeObserver;
  }
});

test('the sentence chosen is the one where the number stands beside the figure\'s own noun', () => {
  const [tile] = evidence.figureTiles(lone(LONG_FIRST + ' Load shedding hit stage 4 for a day. Run away drew 4 posts in the 7-day period before.', posts(4, 'q1')));
  expect(tile.about).toBe('…Run away drew 4 posts in the 7-day period before.');
});
