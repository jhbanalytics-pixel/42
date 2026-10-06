/* History on the 42 API (task 3.3, screen half; contract.md section 12.4;
   EXPERIENCE.md, History: past asks, briefs, findings and analogues). The
   fixture is what core/api/history.py builds over its fixture store: a
   South African hashtag four days into its wave with three analogues, a
   Nigerian item before 42's cultural map (no analogues, the words
   instead), a search for heritage, two pages of asks and of brief dates,
   and two saved findings before L3 sets their status. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import fixture from './fixtures/history42.json';
import topicFixture from './fixtures/topic42_za.json';
import todayFixture from './fixtures/today42.json';
import completeAskFixture from './fixtures/ask42_complete.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const api = await import('../../api42.js');
const {HistoryPage42, HistoryItemPage42} = await import('../../history42.jsx');
const {TopicPage42} = await import('../../topic42.jsx');
const {TodayPage42} = await import('../../today42.jsx');
const {AskPage} = await import('../../ask42.jsx');
const {parseAskQuery, resolveHostRoute, todayDateFromHash} = await import('../../App.jsx');

const STEP = fixture.item.item_id;
const HER = fixture.search.items[0].item_id;
const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const text = (scope = host) => scope.textContent.replace(/ /g, ' ').replace(/\s+/g, ' ');
const hrefs = (scope = host) => [...scope.querySelectorAll('a')].map((a) => a.getAttribute('href'));
const button = (label) => [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const tab = (label) => [...host.querySelectorAll('[role="tab"]')].find((b) => b.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const section = (name) => host.querySelector('[data-section="' + name + '"]');
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

/* Every test answers fetch from a route table: the first matching prefix. */
function serve(routes){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init: init || {}});
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer(String(url)) : answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

const ROUTES = [
  ['/api/history/asks?limit=20&before=', reply(200, fixture.asks_older)],
  ['/api/history/asks', reply(200, fixture.asks)],
  ['/api/history/briefs?limit=30&before=', reply(200, fixture.briefs_older)],
  ['/api/history/briefs', reply(200, fixture.briefs)],
  ['/api/history/findings', reply(200, fixture.findings)],
  ['/api/history/search', reply(200, fixture.search)],
];

beforeEach(async () => {
  window.history.replaceState(window.history.state, '', '#/history');
  await settle();
  calls = [];
  localStorage.setItem('pulse_passcode', 'test-pass');
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

async function mount(element, routes = ROUTES){
  serve(routes);
  flushSync(() => root.render(element));
  await settle();
}

/* The reads and the routes. */

test('the fetch functions ask the paths contract section 12.4 names, with the passcode', async () => {
  serve([['/api/', reply(200, {})]]);
  await api.fetchHistoryItem(STEP, 'ZA');
  await api.searchHistory('heritage', null);
  await api.searchHistory('fixture za', 'ZA');
  await api.fetchHistoryAsks({limit: 20});
  await api.fetchHistoryAsks({limit: 20, before: '2026-09-30T08:00:00+02:00'});
  await api.fetchHistoryBriefs({limit: 30, before: '2026-09-30'});
  await api.fetchHistoryFindings({});
  await api.fetchHistoryFindings({itemId: STEP});
  expect(calls.map((c) => c.url)).toEqual([
    '/api/history/items/' + STEP + '?market=ZA',
    '/api/history/search?q=heritage',
    '/api/history/search?q=fixture+za&market=ZA',
    '/api/history/asks?limit=20',
    '/api/history/asks?limit=20&before=2026-09-30T08%3A00%3A00%2B02%3A00',
    '/api/history/briefs?limit=30&before=2026-09-30',
    '/api/history/findings',
    '/api/history/findings?item_id=' + STEP,
  ]);
  expect(calls.every((c) => c.init.headers['X-Passcode'] === 'test-pass')).toBe(true);
});

test('the routes: #/history and #/history/items/<id>; a brief date opens Today on that date', () => {
  expect(resolveHostRoute('history', '')).toBe('history');
  expect(resolveHostRoute('history', 'items')).toBe('history-item');
  expect(todayDateFromHash('#/today?date=2026-09-29')).toBe('2026-09-29');
  expect(todayDateFromHash('#/pulse?date=2026-09-29')).toBe('2026-09-29');
  expect(todayDateFromHash('#/today')).toBeNull();
  expect(todayDateFromHash('#/today?date=yesterday')).toBeNull();
  expect(resolveHostRoute('today', '')).toBe('pulse');
});

/* The History page. */

/* Albert, 4 October 2026: with Nigeria picked in the header, History still
   listed South Africa's asks. Every tab now asks for the picked market. */
test('History asks every list for the market picked in the header, and says which', async () => {
  await mount(<HistoryPage42 region="NG" />);
  expect(calls.map((c) => c.url)).toContain('/api/history/asks?limit=20&market=NG');
  expect(text()).toContain('Showing Nigeria only. Pick All markets at the top to see every market.');
  click(tab('Briefs'));
  await settle();
  expect(calls.map((c) => c.url)).toContain('/api/history/briefs?limit=30&market=NG');
  click(tab('Findings'));
  await settle();
  expect(calls.map((c) => c.url)).toContain('/api/history/findings?market=NG');
});

test('History with All markets picked asks for every market and names each ask\'s market', async () => {
  const asks = clone(fixture.asks);
  asks.asks = asks.asks.map((a) => ({...a, market: 'KE'}));
  await mount(<HistoryPage42 region="ALL" />, [['/api/history/asks', reply(200, asks)], ...ROUTES]);
  expect(calls[0].url).toBe('/api/history/asks?limit=20');
  expect(text()).toContain('Showing every market.');
  expect(host.querySelector('[data-ask-market]').textContent).toBe('Kenya');
});

test('History opens on answered Asks; Show all and Show older use only loaded pages', async () => {
  await mount(<HistoryPage42 />);
  expect(host.querySelector('h1').textContent).toBe('History');
  expect([...host.querySelectorAll('[role="tab"]')].map((t) => t.textContent)).toEqual(['Asks', 'Briefs', 'Findings', 'Search items']);
  expect(tab('Asks').getAttribute('aria-selected')).toBe('true');
  expect(calls.map((c) => c.url)).toEqual(['/api/history/asks?limit=20']);
  const rows = () => [...section('asks').querySelectorAll('li')];
  const answered = fixture.asks.asks.filter((ask) => ['complete', 'partial'].includes(ask.answer_status));
  expect(rows().map((r) => r.querySelector('[data-question]').textContent)).toEqual(answered.map((a) => a.question));
  expect(text(rows()[0])).toContain('Partly answered');
  expect(text(rows()[0])).toContain('30 September 2026');
  expect(hrefs(rows()[0])).toEqual(['#/ask?follow=a_20260930_h2']);
  expect(text(rows()[0].querySelector('a'))).toBe('Read answer');
  expect(button('Show all').getAttribute('aria-pressed')).toBe('false');
  click(button('Show all'));
  expect(rows().map((r) => r.querySelector('[data-question]').textContent)).toEqual(fixture.asks.asks.map((a) => a.question));
  expect(button('Show all').getAttribute('aria-pressed')).toBe('true');
  click(button('Show all'));
  expect(rows().map((r) => r.querySelector('[data-question]').textContent)).toEqual(answered.map((a) => a.question));
  expect(calls.map((c) => c.url)).toEqual(['/api/history/asks?limit=20']);
  click(button('Show older'));
  await settle();
  expect(calls[1].url).toBe('/api/history/asks?limit=20&before=' + encodeURIComponent(fixture.asks.next_before));
  const answeredAcrossPages = [...fixture.asks.asks, ...fixture.asks_older.asks]
    .filter((ask) => ['complete', 'partial'].includes(ask.answer_status));
  expect(rows().map((r) => r.querySelector('[data-question]').textContent)).toEqual(answeredAcrossPages.map((a) => a.question));
  expect(text(rows()[1])).toContain('Answered');
  expect(Boolean(button('Show older'))).toBe(false);
});

test('History list reads announce status outside busy content and clear busy on settlement', async () => {
  let finishAsks;
  let finishOlder;
  let finishBriefs;
  let finishFindings;
  const region = (name) => section(name).querySelector('.hi42-list-region');
  const status = (name) => section(name).querySelector('[role=status]');

  await mount(<HistoryPage42 />, [
    ['/api/history/asks?limit=20&before=', () => new Promise((resolve) => { finishOlder = resolve; })],
    ['/api/history/asks?limit=20', () => new Promise((resolve) => { finishAsks = resolve; })],
    ['/api/history/briefs?limit=30&before=', reply(200, fixture.briefs_older)],
    ['/api/history/briefs', () => new Promise((resolve) => { finishBriefs = resolve; })],
    ['/api/history/findings', () => new Promise((resolve) => { finishFindings = resolve; })],
    ...ROUTES,
  ]);
  expect(region('asks').getAttribute('aria-busy')).toBe('true');
  expect(status('asks').textContent).toBe('Reading past asks');
  expect(status('asks').closest('.hi42-list-region')).toBeNull();

  finishAsks(reply(200, fixture.asks));
  await settle();
  expect(region('asks').getAttribute('aria-busy')).toBe('false');

  click(button('Show older'));
  expect(region('asks').getAttribute('aria-busy')).toBe('true');
  expect(status('asks').textContent).toBe('Reading more past asks');
  expect(status('asks').closest('.hi42-list-region')).toBeNull();
  expect(Boolean(button('Show all'))).toBe(true);
  finishOlder(reply(200, fixture.asks_older));
  await settle();
  expect(region('asks').getAttribute('aria-busy')).toBe('false');

  click(tab('Briefs'));
  await settle();
  expect(region('briefs').getAttribute('aria-busy')).toBe('true');
  expect(status('briefs').textContent).toBe('Reading published briefs');
  expect(status('briefs').closest('.hi42-list-region')).toBeNull();
  finishBriefs(reply(200, fixture.briefs));
  await settle();
  expect(region('briefs').getAttribute('aria-busy')).toBe('false');

  click(tab('Findings'));
  await settle();
  expect(region('findings').getAttribute('aria-busy')).toBe('true');
  expect(status('findings').textContent).toBe('Reading saved findings');
  expect(status('findings').closest('.hi42-list-region')).toBeNull();
  finishFindings(reply(200, fixture.findings));
  await settle();
  expect(region('findings').getAttribute('aria-busy')).toBe('false');
  // UI notes, 2 Oct 2026: the server's "not checked yet" note reads as what a saved finding is.
  expect(text(section('findings'))).toContain('Saved findings show what was true when they were saved.');
  expect(text(section('findings'))).not.toContain('Whether a finding still holds is not checked yet');
});
test('History tab links and browser navigation restore the selected tab', async () => {
  const previousHash = window.location.hash;
  try {
    window.history.replaceState(window.history.state, '', '#/history?scope=all&tab=briefs');
    await mount(<HistoryPage42 />);
    expect(tab('Briefs').getAttribute('aria-selected')).toBe('true');
    expect(Boolean(section('briefs'))).toBe(true);

    click(tab('Findings'));
    expect(window.location.hash).toBe('#/history?scope=all&tab=findings');
    expect(tab('Findings').getAttribute('aria-selected')).toBe('true');
    click(tab('Briefs'));
    expect(window.location.hash).toBe('#/history?scope=all&tab=briefs');

    window.location.hash = '#/history?scope=all&tab=asks';
    await settle();
    expect(tab('Asks').getAttribute('aria-selected')).toBe('true');
    const traverse = async (direction) => {
      const changed = new Promise((resolve) => window.addEventListener('hashchange', resolve, {once: true}));
      window.history[direction]();
      await changed;
      await settle();
    };
    await traverse('back');
    expect(window.location.hash).toBe('#/history?scope=all&tab=briefs');
    expect(tab('Briefs').getAttribute('aria-selected')).toBe('true');

    window.location.hash = hrefs(section('briefs'))[0];
    await settle();
    expect(window.location.hash).toBe('#/today?date=2026-09-30');
    await traverse('back');
    expect(window.location.hash).toBe('#/history?scope=all&tab=briefs');
    expect(tab('Briefs').getAttribute('aria-selected')).toBe('true');
    await traverse('forward');
    expect(window.location.hash).toBe('#/today?date=2026-09-30');
    await traverse('back');
    expect(tab('Briefs').getAttribute('aria-selected')).toBe('true');
  } finally {
    window.history.replaceState(window.history.state, '', previousHash || '#/');
  }
});

test('Asks filter by answer_status, sort stably by valid timestamps, and Show all reveals every loaded row', async () => {
  const asks = [
    {ask_id: 'tie-partial-first', at: '2026-09-30T08:00:00Z', status: 'complete', answer_status: 'partial', question: 'First tied answer'},
    {ask_id: 'unknown-complete', at: 'not-a-date', status: 'complete', answer_status: 'complete', question: 'Answer with an unknown date'},
    {ask_id: 'probe-complete', at: '2026-09-30T09:00:00Z', status: 'ok', answer_status: 'complete', question: 'Probe: what is the result?'},
    {ask_id: 'insufficient', at: '2026-09-30T11:00:00Z', status: 'complete', answer_status: 'insufficient', question: 'An insufficient answer'},
    {ask_id: 'failed-complete', at: '2026-09-30T10:00:00Z', status: 'failed', answer_status: 'complete', question: 'Stored answer despite failed run'},
    {ask_id: 'tie-complete-second', at: '2026-09-30T08:00:00Z', status: 'complete', answer_status: 'complete', question: 'Second tied answer'},
    {ask_id: 'queued', at: null, status: 'queued', answer_status: null, question: 'No answer yet'},
  ];
  await mount(<HistoryPage42 />, [['/api/history/asks', reply(200, {asks, next_before: null})], ...ROUTES]);
  const questions = () => [...section('asks').querySelectorAll('[data-question]')].map((row) => row.textContent);
  expect(questions()).toEqual([
    'Stored answer despite failed run',
    'Probe: what is the result?',
    'First tied answer',
    'Second tied answer',
    'Answer with an unknown date',
  ]);
  expect(hrefs(section('asks'))).toEqual([
    '#/ask?follow=probe-complete',
    '#/ask?follow=tie-partial-first',
    '#/ask?follow=tie-complete-second',
    '#/ask?follow=unknown-complete',
  ]);
  expect(button('Show all').getAttribute('aria-pressed')).toBe('false');

  click(button('Show all'));
  expect(questions()).toEqual([
    'An insufficient answer',
    'Stored answer despite failed run',
    'Probe: what is the result?',
    'First tied answer',
    'Second tied answer',
    'Answer with an unknown date',
    'No answer yet',
  ]);
  expect(button('Show all').getAttribute('aria-pressed')).toBe('true');
  click(button('Show all'));
  expect(questions()).toEqual([
    'Stored answer despite failed run',
    'Probe: what is the result?',
    'First tied answer',
    'Second tied answer',
    'Answer with an unknown date',
  ]);
  expect(button('Show all').getAttribute('aria-pressed')).toBe('false');
  expect(calls.map((call) => call.url)).toEqual(['/api/history/asks?limit=20']);
  expect(calls.every((call) => String(call.init.method || 'GET').toUpperCase() === 'GET')).toBe(true);
});

test('an unanswered loaded page says so while keeping Show all and Show older available', async () => {
  const cursor = '2026-09-30T00:00:00Z';
  const insufficient = {ask_id: 'insufficient', at: '2026-09-30T09:00:00Z', status: 'complete', answer_status: 'insufficient', question: 'An insufficient answer'};
  const partial = {ask_id: 'partial-older', at: '2026-09-29T09:00:00Z', status: 'complete', answer_status: 'partial', question: 'An older partial answer'};
  const olderUrl = '/api/history/asks?limit=20&before=' + encodeURIComponent(cursor);
  await mount(<HistoryPage42 />, [
    ['/api/history/asks?limit=20&before=', reply(200, {asks: [partial], next_before: null})],
    ['/api/history/asks', reply(200, {asks: [insufficient], next_before: cursor})],
    ...ROUTES,
  ]);
  expect(text(section('asks'))).toContain('No complete or partial answers in this page');
  expect(Boolean(button('Show all'))).toBe(true);
  expect(Boolean(button('Show older'))).toBe(true);
  click(button('Show all'));
  expect([...section('asks').querySelectorAll('[data-question]')].map((row) => row.textContent)).toEqual(['An insufficient answer']);
  click(button('Show all'));
  expect(text(section('asks'))).toContain('No complete or partial answers in this page');
  expect(calls.map((call) => call.url)).toEqual(['/api/history/asks?limit=20']);

  click(button('Show older'));
  await settle();
  expect([...section('asks').querySelectorAll('[data-question]')].map((row) => row.textContent)).toEqual(['An older partial answer']);
  expect(text(section('asks'))).not.toContain('No complete or partial answers in this page');
  expect(calls.map((call) => call.url)).toEqual(['/api/history/asks?limit=20', olderUrl]);
});

test('Asks can retry a failed first read and recover with GET only', async () => {
  let reads = 0;
  await mount(<HistoryPage42 />, [['/api/history/asks?limit=20', () => {
    reads += 1;
    return reads === 1
      ? reply(503, {error: 'unavailable', message: 'The ask history is unavailable'})
      : reply(200, fixture.asks);
  }], ...ROUTES]);
  expect(section('asks').querySelector('[role="alert"]')?.textContent).toContain('The ask history is unavailable');
  click(button('Try again'));
  expect(button('Try again')?.disabled).toBe(true);
  await settle();

  expect([...section('asks').querySelectorAll('[data-question]')].map((row) => row.textContent)).toEqual(
    fixture.asks.asks.filter((ask) => ['complete', 'partial'].includes(ask.answer_status)).map((ask) => ask.question));
  expect(calls.map((call) => call.url)).toEqual(['/api/history/asks?limit=20', '/api/history/asks?limit=20']);
  expect(calls.every((call) => String(call.init.method || 'GET').toUpperCase() === 'GET')).toBe(true);
});

test('Asks retry a failed older page at the same cursor and append its rows once', async () => {
  let olderReads = 0;
  let finishRetry;
  const cursorUrl = '/api/history/asks?limit=20&before=' + encodeURIComponent(fixture.asks.next_before);
  await mount(<HistoryPage42 />, [['/api/history/asks?limit=20&before=', () => {
    olderReads += 1;
    return olderReads === 1
      ? reply(503, {error: 'unavailable', message: 'Older asks could not load'})
      : new Promise((resolve) => { finishRetry = resolve; });
  }], ...ROUTES]);
  click(button('Show older'));
  await settle();
  const rows = () => [...section('asks').querySelectorAll('li')].map((row) => ({
    question: row.querySelector('[data-question]').textContent,
    details: row.querySelector('.hi42-muted').textContent,
  }));
  const answered = fixture.asks.asks.filter((ask) => ['complete', 'partial'].includes(ask.answer_status));
  expect(rows().map((row) => row.question)).toEqual(answered.map((ask) => ask.question));
  expect(rows()).toHaveLength(answered.length);
  expect(section('asks').querySelector('[role="alert"]')?.textContent).toContain('Older asks could not load');
  expect(Boolean(button('Show older'))).toBe(false);

  click(button('Try again'));
  expect(button('Try again')?.disabled).toBe(true);
  expect(calls.map((call) => call.url)).toEqual(['/api/history/asks?limit=20', cursorUrl, cursorUrl]);
  finishRetry(reply(200, fixture.asks_older));
  await settle();

  const expected = [...fixture.asks.asks, ...fixture.asks_older.asks]
    .filter((ask) => ['complete', 'partial'].includes(ask.answer_status)).map((ask) => ask.question);
  const expectedRows = [
    {question: expected[0], details: '30 September 2026, 08:00 · Partly answered'},
    {question: expected[1], details: '29 September 2026, 10:00 · Answered'},
  ];
  expect(rows()).toEqual(expectedRows);
  expect(rows()).toHaveLength(expectedRows.length);
  expect(Boolean(button('Try again'))).toBe(false);
  expect(Boolean(button('Show older'))).toBe(false);
});

test('Ask rows keep their SAST time when the API writes the stamp with a space, as staging does', async () => {
  /* Demo polish, 2 October 2026: core/api/history.py sends str(asked_at),
     "2026-10-01 18:22:05.123456+00:00", so the time fell off every row. */
  const asks = {asks: [
    {ask_id: 'spaced', question: 'Spaced stamp', at: '2026-10-01 18:22:05.123456+00:00', status: 'complete', answer_status: 'complete'},
    {ask_id: 'late-utc', question: 'Late UTC stamp', at: '2026-09-30T22:30:00Z', status: 'complete', answer_status: 'complete'},
    {ask_id: 'sast', question: 'SAST stamp', at: '2026-09-29T10:00:00+02:00', status: 'complete', answer_status: 'partial'},
  ], next_before: null};
  await mount(<HistoryPage42 />, [['/api/history/asks', reply(200, asks)], ...ROUTES]);
  const details = [...section('asks').querySelectorAll('li')].map((row) => row.querySelector('.hi42-muted').textContent);
  expect(details).toEqual([
    '1 October 2026, 20:22 · Answered',
    '1 October 2026, 00:30 · Answered',
    '29 September 2026, 10:00 · Partly answered',
  ]);
});

test('each ask row carries its time as a time element and its status as a marked word, reading as one line', async () => {
  const asks = {asks: [
    {ask_id: 'a1', question: 'First', at: '2026-09-29T10:00:00+02:00', status: 'complete', answer_status: 'complete'},
    {ask_id: 'a2', question: 'Second', at: '2026-09-28T10:00:00+02:00', status: 'complete', answer_status: 'partial'},
    {ask_id: 'a3', question: 'Third', at: '2026-09-27T10:00:00+02:00', status: 'failed', answer_status: null},
  ], next_before: null};
  await mount(<HistoryPage42 />, [['/api/history/asks', reply(200, asks)], ...ROUTES]);
  click(button('Show all'));
  const rows = [...section('asks').querySelectorAll('li')];
  expect(rows.map((row) => row.querySelector('.hi42-muted time').getAttribute('datetime'))).toEqual(['2026-09-29T10:00:00+02:00', '2026-09-28T10:00:00+02:00', '2026-09-27T10:00:00+02:00']);
  expect(rows.map((row) => row.querySelector('.hi42-state').dataset.status)).toEqual(['answered', 'partial', 'failed']);
  expect(rows.map((row) => row.querySelector('.hi42-muted').textContent)).toEqual([
    '29 September 2026, 10:00 · Answered',
    '28 September 2026, 10:00 · Partly answered',
    '27 September 2026, 10:00 · Did not finish',
  ]);
});

test('no asks yet says so and offers Ask as the one next step', async () => {
  await mount(<HistoryPage42 />, [['/api/history/asks', reply(200, {asks: [], next_before: null})], ...ROUTES]);
  expect(text(section('asks'))).toContain('No questions have been asked yet.');
  const link = section('asks').querySelector('a[href="#/ask"]');
  expect(link?.textContent).toBe('Ask a question');
});

test('an Ask history 401 hands over to passcode entry without retry', async () => {
  let authCalls = 0;
  await mount(<HistoryPage42 onAuth={() => { authCalls += 1; }} />, [['/api/history/asks?limit=20', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})], ...ROUTES]);
  expect(authCalls).toBe(1);
  expect(section('asks').querySelector('[role="alert"]')?.textContent).toBe('Enter the passcode to read past asks.');
  expect(Boolean(button('Try again'))).toBe(false);
});

test('switching away aborts the obsolete Ask history read', async () => {
  let finishRead;
  await mount(<HistoryPage42 />, [['/api/history/asks?limit=20', () => new Promise((resolve) => { finishRead = resolve; })], ...ROUTES]);
  const signal = calls[0].init.signal;
  expect(signal.aborted).toBe(false);

  click(tab('Briefs'));
  await settle();
  expect(signal.aborted).toBe(true);
  finishRead(reply(200, fixture.asks));
  await settle();
  expect(Boolean(section('asks'))).toBe(false);
  expect(text(section('briefs'))).toContain('30 September 2026');
});

test('a saved answer opened from History follows its stored ask ID with GET only', async () => {
  await mount(<HistoryPage42 />);
  const saved = fixture.asks.asks[1];
  const link = hrefs(section('asks'))[0];
  expect(link).toBe('#/ask?follow=' + saved.ask_id);
  expect(resolveHostRoute('ask', '')).toBe('ask');
  const query = parseAskQuery(link);
  expect(query.follow).toBe(saved.ask_id);

  flushSync(() => root.unmount());
  root = createRoot(host);
  calls = [];
  const record = {...clone(completeAskFixture), ask_id: saved.ask_id, question: saved.question};
  serve([['/api/ask/', reply(200, record)]]);
  flushSync(() => root.render(<AskPage region="ZA" query={query} />));
  await settle();

  expect(calls.map((call) => call.url)).toEqual(['/api/ask/' + saved.ask_id]);
  expect(calls.every((call) => String(call.init.method || 'GET').toUpperCase() === 'GET')).toBe(true);
  expect(text()).toContain(completeAskFixture.answer.short_answer);
});

test('Briefs list each published date newest first with every market, and open Today for that date', async () => {
  await mount(<HistoryPage42 />);
  click(tab('Briefs'));
  await settle();
  expect(tab('Briefs').getAttribute('aria-selected')).toBe('true');
  expect(calls.map((c) => c.url)).toContain('/api/history/briefs?limit=30');
  expect(hrefs(section('briefs'))).toEqual(['#/today?date=2026-09-30']);
  const first = text(section('briefs').querySelector('li'));
  expect(first).toContain('30 September 2026');
  /* Demo polish, 2 October 2026: markets that share a status now read as
     one group, so the row says it once instead of once per market. */
  expect(first).toContain('South Africa and Nigeria published; Kenya data issue');
  click(button('Show older'));
  await settle();
  expect(calls.at(-1).url).toBe('/api/history/briefs?limit=30&before=2026-09-30');
  expect(hrefs(section('briefs'))).toEqual(['#/today?date=2026-09-30', '#/today?date=2026-09-29']);
  expect(button('Show older')).toBeUndefined();
});

test('each brief row says what Today showed and held per market, a brief that held every item included', async () => {
  /* Staging, 1 to 3 October 2026: every brief held every item. History still
     lists the date and says how many items each market held back. */
  await mount(<HistoryPage42 />);
  click(tab('Briefs'));
  await settle();
  const counts = section('briefs').querySelector('li [data-counts]');
  expect(text(counts)).toBe('South Africa 1 trend shown, 8 held back; Nigeria 1 trend shown, 5 held back; Kenya 0 trends shown, 1 held back');
  click(button('Show older'));
  await settle();
  const older = [...section('briefs').querySelectorAll('li [data-counts]')].map((row) => text(row));
  expect(older[1]).toBe('South Africa 0 trends shown, 7 held back; Nigeria 0 trends shown, 5 held back');
});

test('a brief date where every market is partial says so once, in plain words', async () => {
  /* Demo polish, 2 October 2026: staging read "South Africa partial,
     Nigeria partial, Kenya partial". */
  await mount(<HistoryPage42 />, [['/api/history/briefs', reply(200, {
    dates: [{date: '2026-10-01', markets: [
      {market: 'ZA', status: 'partial'}, {market: 'NG', status: 'partial'}, {market: 'KE', status: 'partial'},
    ]}, {date: '2026-09-30', markets: [
      {market: 'ZA', status: 'published'}, {market: 'KE', status: 'partial'}, {market: 'NG', status: 'not_ready'},
    ]}],
    next_before: null,
  })], ...ROUTES]);
  click(tab('Briefs'));
  await settle();
  const rows = [...section('briefs').querySelectorAll('li .hi42-muted')].map((row) => text(row));
  expect(rows).toEqual([
    'South Africa, Nigeria and Kenya published, some trends not yet explained',
    'South Africa published; Kenya published, some trends not yet explained; Nigeria not ready',
  ]);
});

test('a brief opened from History keeps its date through the Today route', async () => {
  await mount(<HistoryPage42 />, [['/api/history/briefs', reply(200, {
    dates: [{date: '2026-09-30', markets: [{market: 'ZA', status: 'published'}]}],
  })], ...ROUTES]);
  click(tab('Briefs'));
  await settle();

  const historyHref = hrefs(section('briefs'))[0];
  expect(historyHref).toBe('#/today?date=2026-09-30');
  expect(resolveHostRoute('today', '')).toBe('pulse');
  const date = todayDateFromHash(historyHref);
  expect(date).toBe('2026-09-30');

  flushSync(() => root.unmount());
  root = createRoot(host);
  calls = [];
  serve([['/api/today', reply(200, todayFixture)]]);
  flushSync(() => root.render(<TodayPage42 region="ZA" date={date} />));
  await settle();

  expect(calls.map((call) => call.url)).toEqual(['/api/today?date=2026-09-30']);
  expect(host.querySelector('[data-today-date-context]')?.textContent).toBe('Brief for 30 September 2026');
  expect(host.querySelector('[data-card] h3')?.textContent).toBe(todayFixture.markets[0].cards[0].title);
});

test('Briefs state clearly when reading, when unavailable, and when none are published', async () => {
  let finishRead;
  await mount(<HistoryPage42 />, [['/api/history/briefs', () => new Promise((resolve) => { finishRead = resolve; })], ...ROUTES]);
  click(tab('Briefs'));
  await settle();
  expect(section('briefs').querySelector('[role="status"]')?.textContent).toBe('Reading published briefs');
  expect(section('briefs').querySelector('[aria-busy]')?.getAttribute('aria-busy')).toBe('true');
  expect(section('briefs').querySelector('[role="status"]')?.closest('.hi42-list-region')).toBeNull();

  finishRead(reply(200, {dates: []}));
  await settle();
  expect(text(section('briefs'))).toContain('No brief has been published yet.');
  expect(section('briefs').querySelector('[role=status]')?.textContent).toBe('No brief has been published yet.');
  expect(section('briefs').querySelector('[aria-busy]')?.getAttribute('aria-busy')).toBe('false');
  expect(hrefs(section('briefs'))).toEqual([]);

  flushSync(() => root.unmount());
  root = createRoot(host);
  calls = [];
  let briefReads = 0;
  await mount(<HistoryPage42 />, [['/api/history/briefs', () => {
    briefReads += 1;
    return briefReads === 1
      ? reply(503, {error: 'unavailable', message: 'The brief archive is unavailable'})
      : reply(200, {dates: []});
  }], ...ROUTES]);
  click(tab('Briefs'));
  await settle();
  expect(section('briefs').querySelector('[role="alert"]')?.textContent).toContain('The brief archive is unavailable');
  expect(section('briefs').querySelector('[aria-busy]')?.getAttribute('aria-busy')).toBe('false');
  expect(section('briefs').querySelector('[role="alert"]')?.closest('.hi42-list-region')).toBeNull();
  click(button('Try again'));
  expect(button('Try again')?.disabled).toBe(true);
  await settle();
  expect(text(section('briefs'))).toContain('No brief has been published yet.');
  expect(hrefs(section('briefs'))).toEqual([]);
  const briefCalls = calls.filter((call) => call.url.startsWith('/api/history/briefs'));
  expect(briefCalls.map((call) => call.url)).toEqual(['/api/history/briefs?limit=30', '/api/history/briefs?limit=30']);
  expect(briefCalls.every((call) => String(call.init.method || 'GET').toUpperCase() === 'GET')).toBe(true);
});

test('Findings show each saved question, answer and claims, with the words about whether they still hold', async () => {
  await mount(<HistoryPage42 />);
  click(tab('Findings'));
  await settle();
  expect(calls.map((c) => c.url)).toContain('/api/history/findings');
  // UI notes, 2 Oct 2026: the server's "not checked yet" note reads as what a saved finding is.
  expect(text(section('findings'))).toContain('Saved findings show what was true when they were saved.');
  expect(text(section('findings'))).not.toContain('Whether a finding still holds is not checked yet');
  const rows = [...section('findings').querySelectorAll('[data-finding]')];
  expect(rows.map((r) => r.getAttribute('data-finding'))).toEqual(['f_her_1', 'f_step_1']);
  expect(text(rows[1])).toContain('What is behind #fixture_za_step?');
  expect(text(rows[1])).toContain('A dance step from Soweto.');
  expect(text(rows[1])).toContain('It started on TikTok in Soweto.');
  expect(text(rows[1])).toContain('Supported');
  expect(rows[1].querySelectorAll('[data-evidence]').length).toBe(2);
});

test('a saved Finding keeps its version, context and caveats readable as text', async () => {
  const body = clone(fixture.findings);
  const finding = body.findings[1];
  finding.valid_from = '2026-09-29T10:00:40+02:00';
  finding.valid_to = '2026-09-30T10:00:40+02:00';
  finding.saved_context = {
    market: 'ZA',
    window: {from: '2026-09-28', to: '2026-09-30'},
    context: {topic: '<script>alert("x")</script>', terms: ['first term', 'second term']},
    notices: ['Only source-visible posts are included.'],
    gaps: [{source: 'TikTok', reason: 'History ended before the window.'}],
    source_status: [{source: 'Instagram', status: 'partial'}],
    extra_note: 'Additional saved context stays visible.',
  };
  finding.evidence[0].market = 'ZA';
  finding.evidence[0].source_market = 'KE';
  await mount(<HistoryPage42 />, [['/api/history/findings', reply(200, body)], ...ROUTES]);
  click(tab('Findings'));
  await settle();

  const row = section('findings').querySelector('[data-finding="f_step_1"]');
  const rowText = text(row);
  for (const value of [
    'Valid from 29 September 2026, 10:00',
    'Valid to 30 September 2026, 10:00',
    'South Africa (ZA)',
    '28 September 2026',
    '30 September 2026',
    '<script>alert("x")</script>',
    'first term',
    'second term',
    'Only source-visible posts are included.',
    'History ended before the window.',
    'partial',
    'Additional saved context stays visible.',
    'Recorded source market: Kenya',
  ]) expect(rowText).toContain(value);
  expect(row.querySelector('script')).toBeNull();
  expect(rowText).not.toContain('Market assumed');
});

/* Demo run, 2 Oct 2026: the saved context (schema version, raw ids) sits
   behind a closed "How this was checked" toggle, with one plain line shown. */
test('a saved Finding shows one context line and keeps the rest behind a closed toggle', async () => {
  const body = clone(fixture.findings);
  body.findings[1].saved_context = {schema_version: 'saved_finding_context_v1', source_run_id: 'run_fixture_1', market: 'ZA', window: {from: '2026-09-21', to: '2026-09-27'}};
  await mount(<HistoryPage42 />, [['/api/history/findings', reply(200, body)], ...ROUTES]);
  click(tab('Findings'));
  await settle();

  const row = section('findings').querySelector('[data-finding="f_step_1"]');
  const toggle = row.querySelector('details[data-saved-context]');
  expect(toggle).not.toBeNull();
  expect(toggle.open).toBe(false);
  expect(text(toggle.querySelector('summary'))).toBe('How this was checked');
  expect(text(row.querySelector('[data-saved-context-line]'))).toBe('Asked about South Africa, 21 to 27 September 2026');
  expect(text(toggle)).toContain('run_fixture_1');
  const shown = row.cloneNode(true);
  shown.querySelector('details').remove();
  expect(text(shown)).not.toContain('Saved context');
  expect(text(shown)).not.toContain('saved_finding_context_v1');
  expect(text(shown)).not.toContain('run_fixture_1');
});

test('a malformed Finding shows why its saved answer is unavailable', async () => {
  const body = clone(fixture.findings);
  body.findings = [{
    ...body.findings[0],
    answer: null,
    claims: [],
    evidence: [],
    unavailable_reason: 'The saved answer could not be read.',
  }];
  await mount(<HistoryPage42 />, [['/api/history/findings', reply(200, body)], ...ROUTES]);
  click(tab('Findings'));
  await settle();

  const row = section('findings').querySelector('[data-finding="f_her_1"]');
  expect(text(row)).toContain('The saved answer could not be read.');
  expect(row.querySelector('.hi42-answer')).toBeNull();
  expect(row.querySelector('[data-evidence]')).toBeNull();
  expect(row.querySelector('.hi42-claims')).toBeNull();
});

test('a finding keeps its evidence flags and shows each as words', async () => {
  const body = clone(fixture.findings);
  body.findings[1].evidence[0].flags = ['paid', 'near_duplicate'];
  await mount(<HistoryPage42 />, [['/api/history/findings', reply(200, body)], ...ROUTES]);
  click(tab('Findings'));
  await settle();
  const post = section('findings').querySelector('[data-evidence="p01"]');
  expect(text(post)).toContain('Paid');
  expect(text(post)).toContain('Near duplicate');
});

test('no findings table yet says so', async () => {
  await mount(<HistoryPage42 />, [['/api/history/findings', reply(200, {findings: null, note: 'Whether a finding still holds is not checked yet'})], ...ROUTES]);
  click(tab('Findings'));
  await settle();
  expect(text(section('findings'))).toContain('No findings are saved yet');
});

test('a failed Findings read does not show a retry without a retry action', async () => {
  await mount(<HistoryPage42 />, [['/api/history/findings', reply(503, {error: 'unavailable', message: 'Findings are unavailable'})], ...ROUTES]);
  click(tab('Findings'));
  await settle();
  expect(text(section('findings'))).toContain('Findings are unavailable');
  expect(section('findings').querySelector('[aria-busy]')?.getAttribute('aria-busy')).toBe('false');
  expect(section('findings').querySelector('[role="alert"]')?.closest('.hi42-list-region')).toBeNull();
  expect(Boolean(button('Try again'))).toBe(false);
});

test('Search items finds items by label or alias and links each to its history', async () => {
  await mount(<HistoryPage42 />);
  click(tab('Search items'));
  await settle();
  const input = section('search').querySelector('input');
  flushSync(() => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, 'heritage');
    input.dispatchEvent(new Event('input', {bubbles: true}));
  });
  flushSync(() => section('search').querySelector('form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settle();
  expect(calls.at(-1).url).toBe('/api/history/search?q=heritage');
  const row = section('search').querySelector('[data-item="' + HER + '"]');
  expect(hrefs(row)).toEqual(['#/history/items/' + HER + '?market=ZA']);
  expect(text(row)).toContain('#fixture_za_heritage');
  expect(text(row)).toContain('#heritagefixture');
  expect(text(row)).toContain('Peaked 29 September 2026');
  expect(row.querySelectorAll('[data-query-id="q_item_waves"]').length).toBe(2);
});

test('changing the market without searching again leaves the shown results linked to the market they were read for', async () => {
  await mount(<HistoryPage42 />);
  click(tab('Search items'));
  await settle();
  const form = section('search').querySelector('form');
  const input = form.querySelector('input');
  const select = form.querySelector('select');
  const pick = (value) => flushSync(() => {
    select.value = value;
    select.dispatchEvent(new Event('change', {bubbles: true}));
  });
  flushSync(() => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'heritage');
    input.dispatchEvent(new Event('input', {bubbles: true}));
  });
  pick('ZA');
  flushSync(() => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settle();
  expect(calls.at(-1).url).toBe('/api/history/search?q=heritage&market=ZA');
  const row = () => section('search').querySelector('[data-item="' + HER + '"]');
  expect(hrefs(row())).toEqual(['#/history/items/' + HER + '?market=ZA']);
  pick('NG');
  await settle();
  expect(hrefs(row())).toEqual(['#/history/items/' + HER + '?market=ZA']);
});

test('a search the API refuses shows its words', async () => {
  await mount(<HistoryPage42 />, [['/api/history/search', reply(400, {error: 'bad_request', message: 'q must be 2 to 100 characters.'})], ...ROUTES]);
  click(tab('Search items'));
  await settle();
  flushSync(() => section('search').querySelector('form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await settle();
  expect(text(section('search'))).toContain('q must be 2 to 100 characters.');
});

/* One item's history. */

test('an item’s history: its waves as Figures, recurrences and analogues aligned on its current day', async () => {
  await mount(<HistoryItemPage42 itemId={STEP} market="ZA" />, [['/api/history/items/', reply(200, fixture.item)]]);
  expect(calls.map((c) => c.url)).toEqual(['/api/history/items/' + STEP + '?market=ZA']);
  expect(host.querySelector('h1').textContent).toBe('#fixture_za_step');
  expect(text()).toContain('Day 4 of its current wave');
  const wave = section('waves').querySelector('li');
  expect(text(wave)).toContain('Peaked 30 September 2026');
  expect(wave.querySelector('[data-query-id="q_item_waves"]').textContent).toBe('12');
  expect(wave.querySelector('[data-query-id="q_item_days"]').textContent).toBe('2');
  expect(text(wave)).toContain('still going');
  expect(section('recurrences').querySelector('[data-query-id="q_item_waves"]').textContent).toBe('0');
  const analogues = [...section('analogues').querySelectorAll('[data-analogue]')];
  expect(analogues.map((a) => a.getAttribute('data-analogue'))).toEqual(fixture.item.analogues.map((a) => a.item_id));
  expect(hrefs(analogues[0])).toEqual(['#/history/items/' + HER + '?market=ZA']);
  expect(text(analogues[0])).toContain('peaked 10 days later');
  expect(text(analogues[0])).toContain('240 posts on the peak day');
  expect(text(analogues[0])).toContain('fell below half its peak 13 days later');
  expect(text(analogues[2])).toContain('had peaked 4 days earlier');
  expect(text(analogues[2])).toContain('fade not measured');
  expect(analogues[0].querySelectorAll('[data-query-id="q_analogues"]').length).toBe(3);
  expect(hrefs()).toContain('#/t/' + STEP + '?market=ZA');
});

test('before 42’s cultural map: the words instead of analogues, and a market with no waves says so', async () => {
  await mount(<HistoryItemPage42 itemId={fixture.item_no_map.item_id} market="NG" />, [['/api/history/items/', reply(200, fixture.item_no_map)]]);
  expect(text(section('analogues'))).toContain("Analogues need 42's cultural map");
  expect(section('analogues').querySelector('[data-analogue]')).toBeNull();
  expect(text(section('waves'))).toContain('No waves in Nigeria yet');
});

test('waves not measured yet say so', async () => {
  const body = clone(fixture.item);
  body.waves = null;
  body.recurrences = null;
  await mount(<HistoryItemPage42 itemId={STEP} market="ZA" />, [['/api/history/items/', reply(200, body)]]);
  expect(text(section('waves'))).toContain('Waves are not measured yet');
  expect(host.querySelector('[data-section="recurrences"]')).toBeNull();
  expect(host.querySelector('[data-wave-timeline]')).toBeNull();
});

/* Charts, 3 October 2026: the item's waves and its analogues' waves on one
   time axis, drawn above the written Figures. */

test('an item’s waves and its analogues’ waves share one timeline, the current wave in focus', async () => {
  await mount(<HistoryItemPage42 itemId={STEP} market="ZA" />, [['/api/history/items/', reply(200, fixture.item)]]);
  const chart = host.querySelector('[data-wave-timeline]');
  expect(chart).not.toBeNull();
  expect(chart.querySelector('.ch42-title').textContent).toBe('Each wave, from first rise to fade');
  expect(text(chart.querySelector('.ch42-caption'))).toBe('Waves from 10 September 2025 to 30 September 2026, posts on each peak day');
  const rows = [...chart.querySelectorAll('.ch42-bar-row')];
  expect(rows.map((r) => r.querySelector('.ch42-bar-label').textContent)).toEqual(
    ['September 2026', '#fixture_za_heritage', '#fixture_za_board', 'fixture za rising topic']);
  expect(rows[0].classList.contains('is-focus')).toBe(true);
  expect(chart.querySelectorAll('.is-focus')).toHaveLength(1);
  expect(rows.map((r) => r.querySelector('.ch42-bar-value').textContent)).toEqual(
    ['12 posts at peak', '240 posts at peak', '80 posts at peak', '5 posts at peak']);
  // The current wave is not complete, so it draws a rise and no fade.
  expect(rows[0].querySelector('.ch42-tl-fade')).toBeNull();
  expect(rows[1].querySelector('.ch42-tl-fade')).not.toBeNull();
  expect([...chart.querySelectorAll('table td')].map((td) => td.textContent)).toContain('still running');
  // The chart sits above the written waves.
  expect(chart.compareDocumentPosition(section('waves')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test('an item with no waves draws no timeline', async () => {
  await mount(<HistoryItemPage42 itemId={fixture.item_no_map.item_id} market="NG" />, [['/api/history/items/', reply(200, fixture.item_no_map)]]);
  expect(host.querySelector('[data-wave-timeline]')).toBeNull();
});

test('an item not in the map shows the API’s words', async () => {
  await mount(<HistoryItemPage42 itemId={'0'.repeat(64)} market="ZA" />, [['/api/history/items/', reply(404, {error: 'not_found', message: 'That trend is not tracked by 42.'})]]);
  expect(host.querySelector('h1').textContent).toBe('Item not found');
  expect(text()).toContain('That trend is not tracked by 42.');
});

/* The topic page links to the item's history. */

test('a topic page links to the History of this item', async () => {
  serve([['/api/topics/', reply(200, topicFixture)]]);
  flushSync(() => root.render(<TopicPage42 itemId={topicFixture.card.item_id} market="ZA" />));
  await settle();
  const link = [...host.querySelectorAll('a')].find((a) => a.textContent === 'History of this item');
  expect(link.getAttribute('href')).toBe('#/history/items/' + encodeURIComponent(topicFixture.card.item_id) + '?market=ZA');
});
