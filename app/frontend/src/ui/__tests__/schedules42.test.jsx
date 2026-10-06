/* Scheduled questions and the spike ask on the 42 API (contract.md sections
   14.1 and 14.2). The Schedules screen lists each schedule with its last run
   and, when that run was skipped, why; Pause and Resume append a row; the
   form saves a question to re-ask on a cadence. Nothing is deleted: a paused
   schedule stays listed. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {listSchedules, createSchedule, pauseSchedule, resumeSchedule, askSpike} = await import('../../api42.js');
const {SchedulesPage42, runWords} = await import('../../schedules42.jsx');
const {resolveHostRoute, parseAskQuery} = await import('../../App.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

const S1 = {
  schedule_id: 's_000000000001', created_at: '2026-09-28T08:00:00+02:00', status_at: '2026-09-28T08:00:00+02:00', who: 'passcode',
  question: 'What are Lagos food creators posting this week?', market: 'NG', tier: 'T1', cadence: 'weekly_monday', deliver: ['in_app', 'channel'], status: 'active',
  last_run: {run_id: 'sched-2026-09-28-s_000000000001', date: '2026-09-28', status: 'complete', ask_id: 'a_20260928_00000001', outcome: 'complete'},
  skip_reason: null,
};
const S2 = {
  schedule_id: 's_000000000002', created_at: '2026-09-27T08:00:00+02:00', status_at: '2026-09-29T09:00:00+02:00', who: 'passcode',
  question: 'Which amapiano sounds are new in Nairobi?', market: 'KE', tier: 'T0', cadence: 'daily', deliver: ['in_app'], status: 'paused',
  last_run: {run_id: 'sched-2026-09-29-s_000000000002', date: '2026-09-29', status: 'skipped', ask_id: null, outcome: 'Scheduled share spent'},
  skip_reason: 'Scheduled share spent',
};
const S3 = {
  schedule_id: 's_000000000003', created_at: '2026-09-29T10:00:00+02:00', status_at: '2026-09-29T10:00:00+02:00', who: 'passcode',
  question: 'What is new in Durban dance challenges?', market: 'ZA', tier: 'T1', cadence: 'daily', deliver: ['in_app'], status: 'active',
  last_run: null, skip_reason: null,
};
const SCHEDULES = {schedules: [S1, S2, S3]};

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
const submit = (form) => flushSync(() => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
const choose = (select, value) => flushSync(() => {
  select.value = value;
  select.dispatchEvent(new Event('change', {bubbles: true}));
});
function typeInto(element, value){
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  flushSync(() => element.dispatchEvent(new window.Event('input', {bubbles: true})));
}
const labelled = (scope, words) => [...scope.querySelectorAll('label')].find((l) => l.textContent.trim().startsWith(words));
const inputOf = (scope, words) => {
  const label = labelled(scope, words);
  return label.querySelector('input') || scope.querySelector('#' + CSS.escape(label.getAttribute('for')));
};
const row = (id) => host.querySelector('[data-schedule="' + id + '"]');
const posted = (path) => calls.filter((c) => c.url === path && c.init.method === 'POST');
const bodyOf = (call) => JSON.parse(call.init.body);
const form = () => host.querySelector('[data-section="add"] form');

const standard = () => [
  ['/api/schedules/s_000000000001/pause', reply(200, {...S1, status: 'paused', last_run: undefined, skip_reason: undefined})],
  ['/api/schedules/s_000000000002/resume', reply(200, {...S2, status: 'active', last_run: undefined, skip_reason: undefined})],
  ['/api/schedules', (url, init) => (init && init.method === 'POST'
    ? reply(201, {schedule_id: 's_new000000001', created_at: '2026-09-30T09:00:00+02:00', status_at: '2026-09-30T09:00:00+02:00', who: 'passcode', ...JSON.parse(init.body), status: 'active'})
    : reply(200, SCHEDULES))],
];

async function mount(routes = standard(), props = {}){
  serve(routes);
  flushSync(() => root.render(<SchedulesPage42 {...props} />));
  await settle();
}

test('the schedule and spike calls build the contract paths, methods and bodies', async () => {
  serve([['/api/', reply(200, {})]]);
  await listSchedules();
  await createSchedule({question: 'What is new in amapiano?', market: 'ZA', tier: 'T1', cadence: 'weekly_monday', deliver: ['in_app']});
  await pauseSchedule('s 1');
  await resumeSchedule('s_1');
  await askSpike({item_id: 'item_a', market: 'ZA', date: '2026-09-25', series: 'feed_tiktok'});
  await askSpike({item_id: 'item_a', market: 'ZA', date: '2026-09-25'});
  expect(calls.map((c) => [c.init.method || 'GET', c.url])).toEqual([
    ['GET', '/api/schedules'],
    ['POST', '/api/schedules'],
    ['POST', '/api/schedules/s%201/pause'],
    ['POST', '/api/schedules/s_1/resume'],
    ['POST', '/api/spikes'],
    ['POST', '/api/spikes'],
  ]);
  expect(bodyOf(calls[1])).toEqual({question: 'What is new in amapiano?', market: 'ZA', tier: 'T1', cadence: 'weekly_monday', deliver: ['in_app']});
  expect(bodyOf(calls[4])).toEqual({item_id: 'item_a', market: 'ZA', date: '2026-09-25', series: 'feed_tiktok'});
  expect(bodyOf(calls[5])).toEqual({item_id: 'item_a', market: 'ZA', date: '2026-09-25'});
  for (const call of calls) expect(call.init.headers['X-Passcode']).toBe('test-pass');
});

test('the routes: #/schedules opens the Schedules screen, and #/ask?follow=<id> follows an ask already started', () => {
  expect(resolveHostRoute('schedules', '')).toBe('schedules');
  expect(parseAskQuery('#/ask?follow=a_20260928_00000001').follow).toBe('a_20260928_00000001');
  expect(parseAskQuery('#/ask?q=Hello').follow).toBeNull();
});

test('each schedule shows its question, market, tier and cadence in words, its status and its last run', async () => {
  await mount();
  expect(calls[0].url).toBe('/api/schedules');
  /* Shell consistency, 2 October 2026: the title is the menu's noun. */
  expect(host.querySelector('h1').textContent).toBe('Schedules');
  const first = row('s_000000000001');
  expect(text(first)).toContain('What are Lagos food creators posting this week?');
  expect(text(first)).toContain('Nigeria · Quick scan · Every Monday · In the app and Team channel (not sent yet)');
  expect(text(first)).toContain('Active');
  expect(text(first)).toContain('Last run 28 September 2026: Answered');
  const read = [...first.querySelectorAll('a')].find((a) => a.textContent === 'Read the answer');
  expect(read.getAttribute('href')).toBe('#/ask?follow=a_20260928_00000001');
  expect(button(first, 'Pause')).toBeDefined();

  const second = row('s_000000000002');
  expect(text(second)).toContain('Kenya · Lookup · Every day · In the app');
  expect(text(second)).toContain('Paused');
  expect(text(second)).toContain('Last run 29 September 2026: Skipped, Scheduled share spent');
  expect(second.querySelector('a')).toBeNull();
  expect(button(second, 'Resume')).toBeDefined();

  expect(text(row('s_000000000003'))).toContain('Not run yet');
});

test('the run words say how each answer ended, refused and thin answers as such', () => {
  expect(runWords({status: 'complete', outcome: 'complete'})).toBe('Answered');
  // A started ask with no answer recorded, or any status 42 does not know, is never called answered.
  expect(runWords({status: 'started', outcome: 'Ask started'})).toBe('Started, no answer recorded yet');
  expect(runWords({status: 'queued'})).toBe('No answer recorded');
  expect(runWords({})).toBe('No answer recorded');
  expect(runWords({status: 'complete', outcome: 'partial'})).toBe('Partial answer');
  expect(runWords({status: 'complete', outcome: 'insufficient_evidence'})).toBe('Not enough evidence');
  expect(runWords({status: 'complete', outcome: 'refused'})).toBe('Refused');
  expect(runWords({status: 'stopped', outcome: 'complete'})).toBe('Stopped early');
  expect(runWords({status: 'failed', outcome: 'agent_error'})).toBe('Failed');
  expect(runWords({status: 'running', outcome: 'running'})).toBe('Running');
  expect(runWords({status: 'skipped', outcome: 'Daily questions spent'})).toBe('Skipped, Daily questions spent');
});

test('a note says scheduled questions run at 07:00 and wait for their daily share', async () => {
  await mount();
  expect(text()).toContain('Scheduled questions run at 07:00');
  /* Demo polish, 2 October 2026: the second sentence no longer reads as a
     feature that does not run ("saved but not asked"). It still says that a
     saved schedule waits for its share of the daily questions, so the whole
     sentence is matched rather than the fragment "share of the day". */
  expect(text()).toContain('Scheduled questions run at 07:00 South Africa time (06:00 in Lagos, 08:00 in Nairobi). Saved schedules start running once their share of the daily questions is set.');
  expect(text()).not.toContain('saved but not asked');
});

test('Pause and Resume append a row through the API and the list shows the new status', async () => {
  await mount();
  click(button(row('s_000000000001'), 'Pause'));
  await settle();
  expect(posted('/api/schedules/s_000000000001/pause')).toHaveLength(1);
  expect(text(row('s_000000000001'))).toContain('Paused');
  expect(button(row('s_000000000001'), 'Resume')).toBeDefined();
  expect(text(row('s_000000000001'))).toContain('Last run 28 September 2026: Answered');
  click(button(row('s_000000000002'), 'Resume'));
  await settle();
  expect(posted('/api/schedules/s_000000000002/resume')).toHaveLength(1);
  expect(text(row('s_000000000002'))).toContain('Active');
});

test('a failed pause shows the API message on its row', async () => {
  await mount([['/api/schedules/s_000000000001/pause', reply(503, {error: 'agent_unavailable', message: 'The agent service is not reachable.'})], ...standard()]);
  click(button(row('s_000000000001'), 'Pause'));
  await settle();
  expect(row('s_000000000001').querySelector('[role="alert"]').textContent).toBe('The agent service is not reachable.');
});

test('the form saves a schedule with the tier, cadence and delivery in the contract codes, and lists it', async () => {
  await mount();
  const add = host.querySelector('[data-section="add"]');
  typeInto(add.querySelector('textarea'), 'How is gqom travelling in Nigeria?');
  choose(add.querySelector('select[name="market"]'), 'NG');
  flushSync(() => inputOf(add, 'Lookup').click());
  choose(add.querySelector('select[name="cadence"]'), 'daily');
  submit(form());
  await settle();
  const saved = posted('/api/schedules');
  expect(saved).toHaveLength(1);
  expect(bodyOf(saved[0])).toEqual({question: 'How is gqom travelling in Nigeria?', market: 'NG', tier: 'T0', cadence: 'daily', deliver: ['in_app']});
  expect(row('s_new000000001')).not.toBeNull();
  expect(text(row('s_new000000001'))).toContain('Not run yet');
  expect(add.querySelector('textarea').value).toBe('');
});

test('the form starts on a quick scan every Monday, in the app, and names the tiers in plain words', async () => {
  await mount();
  const add = host.querySelector('[data-section="add"]');
  expect(inputOf(add, 'Quick scan').checked).toBe(true);
  expect(inputOf(add, 'Lookup').checked).toBe(false);
  expect(add.querySelector('select[name="cadence"]').value).toBe('weekly_monday');
  expect([...add.querySelectorAll('select[name="cadence"] option')].map((o) => o.textContent)).toEqual(['Every Monday', 'Every day']);
  expect([...add.querySelectorAll('select[name="market"] option')].map((o) => o.value)).toEqual(['ZA', 'NG', 'KE']);
  expect(inputOf(add, 'In the app').checked).toBe(true);
  expect([...add.querySelectorAll('input[name="deliver"]')].map((i) => i.value)).toEqual(['in_app']);
  expect(text(add)).toContain('Sending answers to the team channel is not connected yet.');
  expect(add.querySelector('textarea').getAttribute('maxlength')).toBe('500');
});

test('a question under 3 characters, or no place to deliver, is stopped before any call', async () => {
  await mount();
  const add = host.querySelector('[data-section="add"]');
  typeInto(add.querySelector('textarea'), 'ab');
  submit(form());
  await settle();
  expect(add.querySelector('[role="alert"]').textContent).toBe('Type a question of 3 to 500 characters.');
  typeInto(add.querySelector('textarea'), 'What is new in amapiano?');
  flushSync(() => inputOf(add, 'In the app').click());
  submit(form());
  await settle();
  expect(add.querySelector('[role="alert"]').textContent).toBe('Choose at least one place for the answer.');
  expect(posted('/api/schedules')).toHaveLength(0);
});

test('a refused save shows the API message', async () => {
  await mount([['/api/schedules', (url, init) => (init && init.method === 'POST'
    ? reply(400, {error: 'bad_request', message: 'market must be ZA, NG or KE.'})
    : reply(200, SCHEDULES))]]);
  const add = host.querySelector('[data-section="add"]');
  typeInto(add.querySelector('textarea'), 'What is new in amapiano?');
  submit(form());
  await settle();
  expect(add.querySelector('[role="alert"]').textContent).toBe('market must be ZA, NG or KE.');
});

test('a question handed over from Ask opens the form filled in with its market', async () => {
  await mount(standard(), {search: 'q=' + encodeURIComponent('What is behind #fixture in South Africa this week?') + '&market=KE'});
  const add = host.querySelector('[data-section="add"]');
  expect(add.querySelector('textarea').value).toBe('What is behind #fixture in South Africa this week?');
  expect(add.querySelector('select[name="market"]').value).toBe('KE');
  expect(add.querySelector('select[name="cadence"]').value).toBe('weekly_monday');
});

test('the screen has one red action, the save', async () => {
  await mount();
  expect([...host.querySelectorAll('.w42-primary')].map((b) => b.textContent.trim())).toEqual(['Schedule']);
});

test('no schedules says so, a failed read offers Try again, and a 401 hands over to the passcode screen', async () => {
  await mount([['/api/schedules', reply(200, {schedules: []})]]);
  expect(text()).toContain('No scheduled questions yet.');
  expect(text()).toContain('No scheduled questions yet. Schedule one below, or choose Ask every Monday under an answer in Ask.');
  flushSync(() => root.unmount());
  root = createRoot(host);
  let n = 0;
  await mount([['/api/schedules', () => (++n === 1 ? reply(500, {error: 'internal', message: 'Broken'}) : reply(200, SCHEDULES))]]);
  expect(host.querySelector('[data-section="schedules"] [role="alert"]').textContent).toBe('The scheduled questions could not load.');
  click(button(host, 'Try again'));
  await settle();
  expect(row('s_000000000001')).not.toBeNull();
  flushSync(() => root.unmount());
  root = createRoot(host);
  let asked = 0;
  await mount([['/api/schedules', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]], {onAuth: () => { asked += 1; }});
  expect(asked).toBe(1);
});

test('the screen copy names no age group and no Google Trends', async () => {
  await mount();
  expect(text()).not.toMatch(/gen ?z|millennial|youth|generation|google trends/i);
});

test('a schedule saved before the list has loaded still shows once the list arrives', async () => {
  let release;
  let reads = 0;
  const made = {schedule_id: 's_early00000001', created_at: '2026-09-30T09:00:00+02:00', status_at: '2026-09-30T09:00:00+02:00', who: 'passcode',
    question: 'What is new in amapiano?', market: 'ZA', tier: 'T1', cadence: 'weekly_monday', deliver: ['in_app'], status: 'active'};
  await mount([['/api/schedules', (url, init) => {
    if (init && init.method === 'POST') return reply(201, made);
    reads += 1;
    return reads === 1
      ? new Promise((resolve) => { release = () => resolve(reply(200, SCHEDULES)); })
      : reply(200, {schedules: [{...made, last_run: null, skip_reason: null}, ...SCHEDULES.schedules]});
  }]]);
  const add = host.querySelector('[data-section="add"]');
  typeInto(add.querySelector('textarea'), 'What is new in amapiano?');
  submit(form());
  await settle();
  release();
  await settle();
  expect(row('s_early00000001')).not.toBeNull();
  expect(host.querySelectorAll('[data-schedule]')).toHaveLength(4);
});

/* Visual QA, 5 October 2026 (SC01): with Kenya picked in the header, the
   schedule form started on South Africa. A link's market still wins. */
test('the schedule form starts on the header market, and a market the link names wins', async () => {
  await mount(standard(), {region: 'KE'});
  const select = () => host.querySelector('select[name="market"]');
  expect(select().value).toBe('KE');
  flushSync(() => root.render(<SchedulesPage42 region="NG" />));
  await settle();
  expect(select().value).toBe('NG');
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount(standard(), {region: 'ALL'});
  expect(select().value).toBe('ZA');
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount(standard(), {region: 'KE', search: '?q=What%20is%20new&market=NG'});
  expect(select().value).toBe('NG');
  flushSync(() => root.render(<SchedulesPage42 region="ZA" search="?q=What%20is%20new&market=NG" />));
  await settle();
  expect(select().value).toBe('NG');
});
