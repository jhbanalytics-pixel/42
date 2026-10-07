/* Compare on the 42 API (contract.md section 11; EXPERIENCE.md, Compare):
   two to five things side by side in the same units over the same window.
   The fixture is three items in South Africa over 7 days: a linked hashtag,
   a brand matched by name in post text and a held-back hashtag, with days
   that had no usable collection kept as gaps. The URL holds the state, so a
   comparison can be shared. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import itemsFixture from './fixtures/compare42_items.json';
import refusedFixture from './fixtures/compare42_refused.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {fetchCompare} = await import('../../api42.js');
const {Compare42, parseCompareQuery} = await import('../../compare42.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

const DISCOVER = {
  date: '2026-09-30', market: 'ZA', run_id: 'r_d1', next_cursor: null,
  items: [
    {item_id: 'i_step', market: 'ZA', title: '#fixture_za_step'},
    {item_id: 'i_cola', market: 'ZA', title: 'Fixture Cola'},
    {item_id: 'i_ring', market: 'ZA', title: '#fixture_za_ring'},
    {item_id: 'i_four', market: 'ZA', title: '#fixture_four'},
    {item_id: 'i_five', market: 'ZA', title: '#fixture_five'},
    {item_id: 'i_six', market: 'ZA', title: '#fixture_six'},
  ],
  held_back: {count: 0, items: []},
  filters: {kinds: [], states: [], platforms: []},
};

/* Every test answers fetch from a route table: the first matching prefix. */
function serve(routes){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init: init || {}});
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer(String(url), init) : answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

beforeEach(() => {
  calls = [];
  localStorage.setItem('pulse_passcode', 'test-pass');
  window.history.replaceState(null, '', '#/compare');
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

const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const text = (scope = host) => scope.textContent.replace(/\s+/g, ' ');
const button = (scope, label) => [...scope.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const choice = (scope, words) => [...scope.querySelectorAll('label')].find((l) => l.textContent.trim() === words).querySelector('input');
const choose = (select, value) => flushSync(() => {
  select.value = value;
  select.dispatchEvent(new Event('change', {bubbles: true}));
});
function typeInto(element, value){
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(element, value);
  flushSync(() => element.dispatchEvent(new window.Event('input', {bubbles: true})));
}
const compareCalls = () => calls.filter((c) => c.url.startsWith('/api/compare')).map((c) => c.url);
const discoverCalls = () => calls.filter((c) => c.url.startsWith('/api/discover')).map((c) => c.url);
const section = (name) => host.querySelector('[data-section="' + name + '"]');

const standard = (body = itemsFixture) => [
  ['/api/compare', reply(200, body)],
  ['/api/discover', reply(200, DISCOVER)],
];

async function mount(search, routes = standard(), props = {}){
  serve(routes);
  flushSync(() => root.render(<Compare42 search={search} region="ZA" {...props} />));
  await settle();
}

const SHARED = 'mode=items&items=i_step,i_cola,i_ring&market=ZA&days=7';

test('fetchCompare builds the contract path for each mode, with the passcode', async () => {
  serve([['/api/', reply(200, {})]]);
  await fetchCompare({mode: 'items', items: ['i_step', 'i cola'], market: 'ZA', days: 28});
  await fetchCompare({mode: 'markets', items: ['i_step'], markets: ['ZA', 'NG', 'KE'], days: 14});
  await fetchCompare({mode: 'platforms', items: ['i_step'], market: 'KE', platforms: ['tiktok', 'x'], days: 7});
  expect(calls.map((c) => c.url)).toEqual([
    '/api/compare?mode=items&items=i_step,i%20cola&market=ZA&days=28',
    '/api/compare?mode=markets&items=i_step&markets=ZA,NG,KE&days=14',
    '/api/compare?mode=platforms&items=i_step&market=KE&platforms=tiktok,x&days=7',
  ]);
  for (const call of calls) expect(call.init.headers['X-Passcode']).toBe('test-pass');
});

test('a shared URL restores the mode, subjects, market and window, and reads the comparison once', async () => {
  expect(parseCompareQuery(SHARED, 'ZA')).toEqual({mode: 'items', items: ['i_step', 'i_cola', 'i_ring'], market: 'ZA', markets: ['ZA', 'NG', 'KE'], platforms: [], days: 7});
  expect(parseCompareQuery('mode=nonsense&items=a,a,b&market=xx&days=9', 'NG')).toEqual({mode: 'items', items: ['a', 'b'], market: 'NG', markets: ['ZA', 'NG', 'KE'], platforms: [], days: 28});
  expect(parseCompareQuery('mode=markets&items=a,b&markets=ke,za,zz', 'ZA')).toMatchObject({mode: 'markets', items: ['a'], markets: ['KE', 'ZA']});
  await mount(SHARED);
  expect(host.querySelector('h1').textContent).toBe('Compare');
  expect(compareCalls()).toEqual(['/api/compare?mode=items&items=i_step,i_cola,i_ring&market=ZA&days=7']);
  expect(choice(host, 'Things in one market').checked).toBe(true);
  expect(choice(host, '7 days').checked).toBe(true);
  expect(host.querySelector('select[name="market"]').value).toBe('ZA');
});

test('daily chart buttons name one post in the singular and keep unknown days unavailable', async () => {
  const data = clone(itemsFixture);
  data.series[0].points = [0, 1, 2, null, undefined].map((value, index) => ({date: '2026-10-0' + (index + 1), value}));
  await mount(SHARED, standard(data));
  const days = [...section('chart').querySelectorAll('[data-subject="s1"] .c42-day')];
  expect(days.map((day) => day.dataset.day)).toEqual(['2026-10-01', '2026-10-02', '2026-10-03']);
  expect(days.map((day) => day.getAttribute('aria-label'))).toEqual([
    '#fixture_za_step, 1 October 2026, 0 posts a day. Ask why this day jumped',
    '#fixture_za_step, 2 October 2026, 1 post a day. Ask why this day jumped',
    '#fixture_za_step, 3 October 2026, 2 posts a day. Ask why this day jumped',
  ]);
});

test('one small multiple per subject, all on the same scale, with days without collection kept as gaps', async () => {
  await mount(SHARED);
  const chart = section('chart');
  const multiples = [...chart.querySelectorAll('[data-subject]')];
  expect(multiples.map((m) => m.getAttribute('data-subject'))).toEqual(['s1', 's2', 's3']);
  const svgs = multiples.map((m) => m.querySelector('svg'));
  expect(svgs.map((s) => s.getAttribute('data-y-max'))).toEqual(['3', '3', '3']);
  expect(text(chart)).toContain('Same scale on every chart: 0 to 3 posts a day');
  expect(multiples.map((m) => m.querySelectorAll('.c42-gap').length)).toEqual([3, 4, 3]);
  /* s2 has a gap inside its run, so its line is drawn in two pieces */
  const pieces = (svg) => (svg.querySelector('.c42-line').getAttribute('d').match(/M/g) || []).length;
  expect(pieces(svgs[0])).toBe(1);
  expect(pieces(svgs[1])).toBe(2);
  expect(svgs[1].getAttribute('aria-label')).toContain('with days missing');
  expect(text(multiples[0])).toContain('#fixture_za_step');
});

test('the table puts each subject in a column and each metric in a row, in its own words', async () => {
  await mount(SHARED);
  const table = section('table').querySelector('table');
  const heads = [...table.querySelectorAll('thead th')].map((th) => text(th));
  expect(heads[1]).toContain('#fixture_za_step');
  expect(heads[2]).toContain('Fixture Cola');
  expect(heads[3]).toContain('#fixture_za_ring');
  const rows = [...table.querySelectorAll('tbody tr')];
  expect(rows.map((r) => text(r.querySelector('th')))).toEqual(itemsFixture.rows.map((r) => r.words));
  const cells = (metric) => [...rows[itemsFixture.rows.findIndex((r) => r.metric === metric)].querySelectorAll('td')].map((td) => text(td.querySelector('.c42-fig') || td));
  // Was '1 posts in 7 days': a count of one reads in the singular since 6 October 2026 (TrendCard figureWords).
  expect(cells('posts')).toEqual(['8 posts in 7 days', '2 posts in 7 days', '1 post in 7 days']);
  expect(cells('first_seen')).toEqual(['10 September 2026', '26 September 2026', 'Not seen']);
  expect(cells('state')).toEqual(['Emerging', 'Not measured', 'Spike']);
  expect(cells('growth')[0]).toBe('2.8 times its usual level');
});

test('every figure carries its query id, shown on focus', async () => {
  await mount(SHARED);
  const figs = [...section('table').querySelectorAll('.c42-fig')];
  expect(figs.length).toBe(21);
  for (const fig of figs){
    expect(fig.getAttribute('tabindex')).toBe('0');
    expect(fig.getAttribute('data-query-id')).toMatch(/^q_/);
    const tip = host.querySelector('#' + fig.getAttribute('aria-describedby'));
    expect(tip.getAttribute('role')).toBe('tooltip');
    expect(tip.textContent).toContain('Query ' + fig.getAttribute('data-query-id'));
  }
  const css = await Bun.file(new URL('../../styles/compare42.css', import.meta.url)).text();
  expect(css).toMatch(/\.c42-fig:focus[^{]*\+ \.c42-tip/);
});

test('a keyword match says so, and a held-back subject says Held back with its reason', async () => {
  await mount(SHARED);
  const heads = [...section('table').querySelectorAll('thead th')];
  expect(text(heads[2])).toContain('Matched by name in post text');
  expect(text(heads[1])).not.toContain('Matched by name in post text');
  expect(text(heads[3])).toContain('Held back: Likely coordinated: many new accounts posting the same words');
  expect(text(section('chart').querySelector('[data-subject="s3"]'))).toContain('Held back');
});

test('the notes are always shown, above the table', async () => {
  await mount(SHARED);
  const notes = section('notes');
  expect([...notes.querySelectorAll('li')].map((li) => li.textContent)).toEqual(itemsFixture.notes);
  expect(notes.compareDocumentPosition(section('table')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test('a comparison the bytes cap refused says so in words, with no table and no chart', async () => {
  await mount('mode=items&items=i_step,i_cola&market=ZA', standard(refusedFixture));
  expect(text()).toContain('This comparison needs more data than one question may read');
  expect(host.querySelector('table')).toBeNull();
  expect(section('chart')).toBeNull();
  expect(section('notes')).not.toBeNull();
});

test('the window chooser reads again with the new window and writes it to the URL', async () => {
  await mount(SHARED);
  click(choice(host, '14 days'));
  await settle();
  expect(compareCalls().at(-1)).toBe('/api/compare?mode=items&items=i_step,i_cola,i_ring&market=ZA&days=14');
  expect(window.location.hash).toBe('#/compare?mode=items&items=i_step,i_cola,i_ring&market=ZA&days=14');
});

test('one thing across markets keeps the first thing and compares it in the three markets', async () => {
  await mount(SHARED);
  click(choice(host, 'One thing across markets'));
  await settle();
  expect(compareCalls().at(-1)).toBe('/api/compare?mode=markets&items=i_step&markets=ZA,NG,KE&days=7');
  expect(window.location.hash).toBe('#/compare?mode=markets&items=i_step&markets=ZA,NG,KE&days=7');
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=all&limit=50');
  click(choice(host, 'Nigeria'));
  await settle();
  expect(compareCalls().at(-1)).toBe('/api/compare?mode=markets&items=i_step&markets=ZA,KE&days=7');
  const before = compareCalls().length;
  click(choice(host, 'Kenya'));
  await settle();
  expect(text(section('picker'))).toContain('Pick two or three markets.');
  expect(compareCalls().length).toBe(before);
  expect(host.querySelector('table')).toBeNull();
});

test('one thing across platforms waits for two platforms, then reads them in one market', async () => {
  const body = clone(itemsFixture);
  await mount('mode=platforms&items=i_step&market=KE&days=7', standard(body));
  expect(compareCalls()).toEqual([]);
  expect(text(section('picker'))).toContain('Pick two to five platforms.');
  click(choice(host, 'TikTok'));
  click(choice(host, 'X'));
  await settle();
  expect(compareCalls()).toEqual(['/api/compare?mode=platforms&items=i_step&market=KE&platforms=tiktok,x&days=7']);
  expect(window.location.hash).toBe('#/compare?mode=platforms&items=i_step&market=KE&platforms=tiktok,x&days=7');
});

test('the picker finds trends by name, adds them up to five, and removes them', async () => {
  await mount('mode=items&items=i_step&market=ZA&days=7');
  expect(discoverCalls()).toEqual(['/api/discover?market=ZA&limit=50']);
  expect(compareCalls()).toEqual([]);
  const picker = section('picker');
  expect(text(picker)).toContain('Add at least one more thing to compare.');
  expect(text(picker.querySelector('.c42-chosen'))).toContain('#fixture_za_step');
  typeInto(picker.querySelector('input[name="find"]'), 'cola');
  const options = [...picker.querySelectorAll('.c42-options button')].map((b) => b.textContent);
  expect(options).toEqual(['Add Fixture Cola']);
  click(button(picker, 'Add Fixture Cola'));
  await settle();
  expect(compareCalls()).toEqual(['/api/compare?mode=items&items=i_step,i_cola&market=ZA&days=7']);
  typeInto(picker.querySelector('input[name="find"]'), '');
  for (const title of ['#fixture_za_ring', '#fixture_four', '#fixture_five']) click(button(picker, 'Add ' + title));
  await settle();
  expect(picker.querySelectorAll('.c42-chosen li')).toHaveLength(5);
  expect(picker.querySelectorAll('.c42-options button')).toHaveLength(0);
  expect(text(picker)).toContain('Five is the most one comparison holds.');
  click(picker.querySelector('.c42-chosen button[aria-label="Remove Fixture Cola"]'));
  await settle();
  expect(compareCalls().at(-1)).toBe('/api/compare?mode=items&items=i_step,i_ring,i_four,i_five&market=ZA&days=7');
  expect(window.location.hash).toBe('#/compare?mode=items&items=i_step,i_ring,i_four,i_five&market=ZA&days=7');
});

/* Demo polish, 2 October 2026 (QA item 11): comparing a held-back trend is by
   design (contract.md section 11), so the picker offers it, but apart from
   the trends that cleared the gate and with its reason beside it, never as a
   plain Add button. One that arrives in a shared URL keeps its name. */
test('the picker lists held-back trends apart, each with its reason, and names one already chosen', async () => {
  const discover = {
    ...DISCOVER,
    held_back: {count: 3, items: [
      {item_id: 'i_bot', title: '#fixture_bot_push', reason: 'likely_coordinated', reason_text: 'Likely coordinated'},
      {item_id: 'i_paid', title: 'Fixture Sponsored', reason: 'paid_led', reason_text: 'Paid-led'},
      {item_id: 'i_bare', title: '#fixture_bare', reason: 'not_local'},
    ]},
  };
  await mount('mode=items&items=i_step,i_paid&market=ZA&days=7', [
    ['/api/compare', reply(200, itemsFixture)],
    ['/api/discover', reply(200, discover)],
  ]);
  const picker = section('picker');
  const admitted = [...picker.querySelectorAll('.c42-options:not([data-held-options]) button')].map((b) => b.textContent);
  expect(admitted).toEqual(['Add Fixture Cola', 'Add #fixture_za_ring', 'Add #fixture_four', 'Add #fixture_five', 'Add #fixture_six']);
  const held = picker.querySelector('[data-held-options]');
  expect(held).not.toBeNull();
  /* UI polish, 2 October 2026: restated. The held list sits under a plain heading. */
  expect(picker.querySelector('.c42-held-intro').textContent).toBe('Held back by checks');
  const rows = [...held.querySelectorAll('li')];
  expect(rows.map((li) => li.querySelector('button').textContent)).toEqual(['Add #fixture_bot_push', 'Add #fixture_bare']);
  expect(rows.map((li) => li.querySelector('.c42-held').textContent)).toEqual(['Held back: Likely coordinated', 'Held back: Not local']);
  /* Under the Held back by checks heading the words Held back are said to
     screen readers only, so the eye reads the reason alone. */
  for (const li of rows) expect(li.querySelector('.c42-held .sr-only')?.textContent).toBe('Held back: ');
  for (const li of rows){
    const button = li.querySelector('button');
    expect(host.querySelector('#' + button.getAttribute('aria-describedby')).textContent).toBe(li.querySelector('.c42-held').textContent);
  }
  typeInto(picker.querySelector('input[name="find"]'), 'bot');
  expect(picker.querySelectorAll('.c42-options:not([data-held-options]) button')).toHaveLength(0);
  expect([...picker.querySelectorAll('[data-held-options] button')].map((b) => b.textContent)).toEqual(['Add #fixture_bot_push']);
  click(button(picker, 'Add #fixture_bot_push'));
  await settle();
  expect(compareCalls().at(-1)).toBe('/api/compare?mode=items&items=i_step,i_paid,i_bot&market=ZA&days=7');
  expect(text(picker.querySelector('.c42-chosen'))).toContain('Fixture Sponsored');
});

test('the picker never says no trend matches while more trends wait, and Load more reads and offers them', async () => {
  const later = {...DISCOVER, next_cursor: null, items: [{item_id: 'i_late', market: 'ZA', title: '#fixture_late_find'}]};
  await mount('mode=items&items=i_step&market=ZA&days=7', [
    ['/api/compare', reply(200, itemsFixture)],
    ['/api/discover?market=ZA&limit=50&cursor=50', reply(200, later)],
    ['/api/discover', reply(200, {...DISCOVER, next_cursor: '50'})],
  ]);
  const picker = section('picker');
  typeInto(picker.querySelector('input[name="find"]'), 'late_find');
  expect(text(picker)).not.toContain('No trend matches those words.');
  click(button(picker, 'Load more trends'));
  await settle();
  expect(discoverCalls()).toEqual(['/api/discover?market=ZA&limit=50', '/api/discover?market=ZA&limit=50&cursor=50']);
  expect([...picker.querySelectorAll('.c42-options button')].map((b) => b.textContent)).toEqual(['Add #fixture_late_find']);
  expect(button(picker, 'Load more trends')).toBeUndefined();
  click(button(picker, 'Add #fixture_late_find'));
  await settle();
  expect(compareCalls().at(-1)).toBe('/api/compare?mode=items&items=i_step,i_late&market=ZA&days=7');
  typeInto(picker.querySelector('input[name="find"]'), 'nothing like this');
  expect(text(picker)).toContain('No trend matches those words.');
});

test('a changed market reads the picker list for that market', async () => {
  await mount(SHARED);
  choose(host.querySelector('select[name="market"]'), 'NG');
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=NG&limit=50');
  expect(compareCalls().at(-1)).toBe('/api/compare?mode=items&items=i_step,i_cola,i_ring&market=NG&days=7');
});

test('a 401 hands the reader to the passcode flow once, and shows no figures', async () => {
  let asked = 0;
  await mount(SHARED, [['/api/', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]], {onAuth: () => { asked += 1; }});
  expect(asked).toBe(1);
  expect(host.querySelector('table')).toBeNull();
});

test('a refused request shows the API words and offers Try again', async () => {
  let answer = reply(400, {error: 'bad_request', message: 'That trend is not tracked by 42. Pick another one.'});
  await mount(SHARED, [['/api/compare', () => answer], ['/api/discover', reply(200, DISCOVER)]]);
  expect(text(section('result'))).toContain('That trend is not tracked by 42. Pick another one.');
  answer = reply(200, itemsFixture);
  click(button(section('result'), 'Try again'));
  await settle();
  expect(host.querySelector('table')).not.toBeNull();
});

test('copy stays clear, headings use their type roles, and red stays on focus', async () => {
  const source = await Bun.file(new URL('../../compare42.jsx', import.meta.url)).text();
  expect(source).not.toMatch(/gen ?z|millennial|youth|google trends|nano banana|—|–/i);
  const css = await Bun.file(new URL('../../styles/compare42.css', import.meta.url)).text();
  const shared = await Bun.file(new URL('../../styles/today42.css', import.meta.url)).text();
  const page = shared.split('.t42-heading {')[1]?.split('}')[0] || '';
  const section = css.split('.c42-title {')[1]?.split('}')[0] || '';
  expect(page).toContain('font-size: var(--type-page-title)');
  expect(page).toContain('line-height: var(--leading-page-title)');
  expect(section).toContain('font-size: var(--type-section-title)');
  expect(section).toContain('line-height: var(--leading-section-title)');
  const red = css.split('}').filter((rule) => /var\(--accent\)/.test(rule) && !/focus-visible/.test(rule));
  expect(red).toEqual([]);
});

test('the compare route renders the new Compare screen from the URL', async () => {
  const app = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
  expect(app).toMatch(/route === 'compare' && <Compare42 /);
  expect(app).not.toMatch(/<ComparePanel/);
});

/* Contract section 14.1: a day on a Compare chart asks why it jumped, through
   the same confirm the topic page uses. A gap has no control. */
const dayButtons = (key) => [...section('chart').querySelector('[data-subject="' + key + '"]').querySelectorAll('button[data-day]')];
const dialog = () => host.querySelector('[role="dialog"]');
const spikeRoutes = (spike) => [['/api/spikes', spike], ...standard()];

test('each measured day on each small multiple is a button, and gaps have none', async () => {
  await mount(SHARED);
  expect(dayButtons('s1').map((b) => b.getAttribute('data-day'))).toEqual(['2026-09-27', '2026-09-28', '2026-09-29', '2026-09-30']);
  expect(dayButtons('s2').map((b) => b.getAttribute('data-day'))).toEqual(['2026-09-27', '2026-09-29', '2026-09-30']);
  expect(dayButtons('s3').map((b) => b.getAttribute('data-day'))).toEqual(['2026-09-27', '2026-09-28', '2026-09-29', '2026-09-30']);
  for (const b of dayButtons('s1')){
    expect(b.tagName).toBe('BUTTON');
    expect(b.getAttribute('type')).toBe('button');
  }
  expect(dayButtons('s1')[1].getAttribute('aria-label')).toBe('#fixture_za_step, 28 September 2026, 0 posts a day. Ask why this day jumped');
});

test('a day opens the spike confirm, Cancel first; Cancel posts nothing', async () => {
  await mount(SHARED, spikeRoutes(reply(202, {ask_id: 'a_20260930_0000spk2', status: 'running'})));
  click(dayButtons('s1')[1]);
  expect(dialog()).not.toBeNull();
  expect(dialog().textContent).toContain('Ask why this day jumped?');
  expect(dialog().textContent).toContain('A quick scan, up to 60 credits.');
  expect(dialog().textContent).toContain('#fixture_za_step, 28 September 2026');
  expect(document.activeElement && document.activeElement.textContent).toBe('Cancel');
  click(button(dialog(), 'Cancel'));
  expect(dialog()).toBeNull();
  expect(calls.filter((c) => c.url === '/api/spikes')).toHaveLength(0);
});

test('Ask posts the spike for that subject, market and day, then follows the ask', async () => {
  await mount(SHARED, spikeRoutes(reply(202, {ask_id: 'a_20260930_0000spk2', status: 'running'})));
  click(dayButtons('s2')[1]);
  click(button(dialog(), 'Ask'));
  await settle();
  const posts = calls.filter((c) => c.url === '/api/spikes');
  expect(posts).toHaveLength(1);
  expect(posts[0].init.method).toBe('POST');
  expect(JSON.parse(posts[0].init.body)).toEqual({item_id: 'i_cola', market: 'ZA', date: '2026-09-29'});
  expect(window.location.hash).toBe('#/ask?follow=a_20260930_0000spk2');
});

/* Design critique, 2 October 2026: an empty picker said "Add two to five
   things to compare." right under the subtitle that says two to five things
   side by side. The empty picker no longer repeats it; a picker that still
   needs something says what. */
test('an empty picker does not repeat the subtitle, and a picker with one thing says what it needs', async () => {
  await mount('');
  expect(text()).toContain('Two to five things side by side');
  expect(text()).not.toContain('Add two to five things to compare.');
  expect(host.querySelector('.c42-need').getAttribute('role')).toBe('status');
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount('mode=items&items=i_step&market=ZA&days=7');
  expect(host.querySelector('.c42-need').textContent).toBe('Add at least one more thing to compare.');
});
