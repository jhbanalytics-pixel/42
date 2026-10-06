/* The Hidden people page as a whole: it says in plain words why someone is
   hidden and what hiding does across 42, keeps its heading through loading
   and failure, lists each hide with platform, handle, why, who and when,
   newest first, and an empty list points to where a hide starts. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {HiddenPeoplePage42} = await import('../../people42.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;
let calls = 0;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const text = (scope = host) => scope.textContent.replace(/ /g, ' ').replace(/\s+/g, ' ');
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const row = (overrides = {}) => ({
  suppression_id: 'sup_app_0123456789ab', status_at: '2026-09-29T10:00:00+02:00', status: 'suppressed',
  creator_id: null, platform: 'tiktok', handle: '@someone_new', reason: 'Asked to be left out of our reports', who: 'Thandi', ...overrides,
});

async function mount(answer){
  globalThis.fetch = async () => { calls += 1; return typeof answer === 'function' ? answer() : answer; };
  flushSync(() => root.render(<HiddenPeoplePage42 />));
  await settle();
}

beforeEach(() => {
  calls = 0;
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

afterAll(() => GlobalRegistrator.unregister());

test('the page says why someone is hidden and what hiding does, and that hides only add', async () => {
  await mount(reply(200, {suppressions: []}));
  const intro = host.querySelector('[data-part="about"]');
  expect(intro).not.toBeNull();
  expect(text(intro)).toContain('asked to be left out');
  expect(text(intro)).toContain('creator pages, community member lists and client reports');
  expect(text(intro)).toContain('Hiding only adds to this list.');
  expect(text()).toContain('Ask the 42 team to bring someone back.');
});

test('while loading, the heading and the explanation stay and a status says the list is loading', async () => {
  globalThis.fetch = () => new Promise(() => {});
  flushSync(() => root.render(<HiddenPeoplePage42 />));
  await settle();
  expect(host.querySelector('h1').textContent).toBe('Hidden people');
  expect(host.querySelector('[data-part="about"]')).not.toBeNull();
  expect(host.querySelector('[aria-busy="true"]')).not.toBeNull();
  expect(host.querySelector('[role="status"]').textContent).toBe('Loading the list of hidden people');
});

test('a failure keeps the heading, shows the server words and Try again reads the list again', async () => {
  let n = 0;
  await mount(() => (n++ === 0
    ? reply(503, {error: 'people_unavailable', message: 'People pages are not available yet.'})
    : reply(200, {suppressions: [row()]})));
  expect(host.querySelector('h1').textContent).toBe('Hidden people');
  expect(host.querySelector('[role="alert"]').textContent).toBe('People pages are not available yet.');
  expect(text()).toContain('The list could not load');
  const retry = [...host.querySelectorAll('button')].find((b) => b.textContent === 'Try again');
  flushSync(() => retry.dispatchEvent(new MouseEvent('click', {bubbles: true})));
  await settle();
  expect(calls).toBe(2);
  expect(host.querySelectorAll('[data-suppression]')).toHaveLength(1);
});

test('rows show platform and handle as sent, why, who and when, newest first, under a count', async () => {
  await mount(reply(200, {suppressions: [
    row({suppression_id: 'sup_a', status_at: '2026-09-20T08:00:00+02:00', platform: 'x', handle: 'older_one', who: 'Jo'}),
    row({suppression_id: 'sup_b', status_at: '2026-09-30T08:00:00+02:00'}),
  ]}));
  expect(text(host.querySelector('[data-part="count"]'))).toBe('2 on the list, newest first.');
  const rows = [...host.querySelectorAll('[data-suppression]')];
  expect(rows.map((r) => r.getAttribute('data-suppression'))).toEqual(['sup_b', 'sup_a']);
  expect(rows[0].querySelector('.pp42-row-title').textContent).toBe('TikTok @someone_new');
  expect(rows[1].querySelector('.pp42-row-title').textContent).toBe('X older_one');
  expect(text(rows[0].querySelector('.hp42-reason'))).toBe('Asked to be left out of our reports');
  expect(text(rows[1])).toContain('hidden by Jo');
  expect(rows[1].querySelector('time').getAttribute('datetime')).toBe('2026-09-20T08:00:00+02:00');
  expect(text(rows[1].querySelector('time'))).toBe('20 September 2026');
});

test('the who and when line never breaks at the dot: the dot is held to the name and the date', async () => {
  await mount(reply(200, {suppressions: [row()]}));
  const meta = host.querySelector('[data-suppression] .hp42-meta').textContent;
  /* Restated in the design round: a nowrap span boundary is itself a break
     point in Chrome, so the line is one run with only non-breaking spaces. */
  expect(meta).toBe('hidden\u00a0by\u00a0Thandi\u00a0\u00b7\u00a029\u00a0September\u00a02026');
  expect(host.querySelector('[data-suppression] .hp42-dot').textContent).toBe('\u00a0\u00b7\u00a0');
});

test('a row the 42 team brought back says so instead of hidden by', async () => {
  await mount(reply(200, {suppressions: [row({status: 'lifted', who: 'Albert'})]}));
  const r = host.querySelector('[data-suppression]');
  expect(text(r)).toContain('Brought back');
  expect(text(r)).not.toContain('hidden by');
  expect(text(host.querySelector('[data-part="count"]'))).toBe('1 on the list, newest first.');
});

test('an empty list says no one is hidden and links to Communities, where creator pages open', async () => {
  await mount(reply(200, {suppressions: []}));
  expect(host.querySelectorAll('[data-suppression]')).toHaveLength(0);
  expect(host.querySelector('[data-part="count"]')).toBeNull();
  const empty = host.querySelector('.pp42-empty');
  expect(text(empty)).toContain('No one is hidden.');
  expect([...empty.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')])).toEqual([['Open Communities', '#/communities']]);
  expect(text(empty)).toContain('creator page');
});

/* Design audit, 2 October 2026: inverted pyramid. The state comes first and
   the policy paragraphs wait under a closed disclosure titled About hiding
   people, after the state, with every word kept. */
test('the state comes before the policy, which sits in an About hiding people disclosure', async () => {
  await mount(reply(200, {suppressions: []}));
  const about = host.querySelector('[data-part="about"]');
  expect(about.tagName).toBe('DETAILS');
  /* Restated, layout pass 4 October 2026: the disclosure now also holds where a hide applies and opens by default, so the page has one real block. */
  expect(about.open).toBe(true);
  expect(text(about)).toContain('Where a hide applies');
  expect(about.querySelector('summary').textContent).toBe('About hiding people');
  expect(text(about)).toContain('Staff hide someone who asked to be left out of 42, for example under POPIA or another privacy law, or who should not be named in our work.');
  expect(text(about)).toContain('From the next load they are gone from creator pages, community member lists and client reports, and anyone who later appears under the same handle is hidden too. Hiding only adds to this list. Ask the 42 team to bring someone back.');
  const empty = host.querySelector('.pp42-empty');
  expect(Boolean(empty.compareDocumentPosition(about) & Node.DOCUMENT_POSITION_FOLLOWING), 'the policy follows the state').toBe(true);
  expect(host.querySelector('header').textContent).toBe('Hidden people');
});

test('the page uses no inline styles', async () => {
  await mount(reply(200, {suppressions: [row()]}));
  expect(host.querySelectorAll('[style]')).toHaveLength(0);
});
