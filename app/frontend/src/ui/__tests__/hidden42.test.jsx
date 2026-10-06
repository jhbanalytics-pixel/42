/* Hiding a person from the app (contract.md section 16). A quiet "Hide this
   person" on a creator page and beside each named member of a community
   opens a dialog asking why and your name, with Hide as its one red action
   and Cancel taking first focus. Hide posts {creator_id, reason, who} to
   /api/suppressions and, on the 201, leaves for #/people/hidden. The list
   there shows who was hidden, why, by whom and when, and has no way to bring
   anyone back from the app. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import fixture from './fixtures/people42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const api = await import('../../api42.js');
const {CreatorPage42, CommunityPage42, HiddenPeoplePage42} = await import('../../people42.jsx');
const {resolveHostRoute} = await import('../../App.jsx');

const COMMUNITY = fixture.community.community.community_id;
const REASON = 'Asked to be left out of our reports';
const MATCHED_NONE = 'No one matches this now; they will be hidden if they appear.';
const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const text = (scope = host) => scope.textContent.replace(/ /g, ' ').replace(/\s+/g, ' ');
const button = (scope, label) => [...scope.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const dialog = () => host.querySelector('[role="dialog"]');
const field = (name) => dialog().querySelector('[name="' + name + '"]');
const posts = () => calls.filter((c) => c.url === '/api/suppressions' && c.init.method === 'POST');
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

function typeInto(element, value){
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  flushSync(() => element.dispatchEvent(new window.Event('input', {bubbles: true})));
}

function serve(routes){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init: init || {}});
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer(String(url), init || {}) : answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

async function mount(element, routes){
  serve(routes);
  flushSync(() => root.render(element));
  await settle();
}

const hidden = (overrides = {}) => ({
  suppression_id: 'sup_app_0123456789ab', status_at: '2026-09-29T10:00:00+02:00', status: 'suppressed',
  creator_id: 'c_ng_macro', platform: null, handle: null, reason: REASON, who: 'Thandi', matched: 1, ...overrides,
});

const creatorPage = (hide, props = {}) => mount(<CreatorPage42 creatorId="c_ng_macro" market="NG" {...props} />, [
  ['/api/creators/', reply(200, fixture.creator)],
  ['/api/suppressions', hide],
]);

async function fillAndHide(reason = REASON, who = 'Thandi'){
  typeInto(field('reason'), reason);
  typeInto(field('who'), who);
  click(button(dialog(), 'Hide'));
  await settle();
}

beforeEach(() => {
  calls = [];
  localStorage.setItem('pulse_passcode', 'test-pass');
  window.location.hash = '#/creators/c_ng_macro?market=NG';
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

/* The calls and the route. */

test('hidePerson posts the body to /api/suppressions and listHidden reads it, both with the passcode', async () => {
  serve([['/api/suppressions', reply(200, {suppressions: []})]]);
  await api.hidePerson({creator_id: 'c_ng_macro', reason: REASON, who: 'Thandi'});
  await api.listHidden();
  expect(calls.map((c) => [c.init.method || 'GET', c.url])).toEqual([['POST', '/api/suppressions'], ['GET', '/api/suppressions']]);
  expect(JSON.parse(calls[0].init.body)).toEqual({creator_id: 'c_ng_macro', reason: REASON, who: 'Thandi'});
  expect(calls.every((c) => c.init.headers['X-Passcode'] === 'test-pass')).toBe(true);
});

test('#/people/hidden is the Hidden people route', () => {
  expect(resolveHostRoute('people', 'hidden')).toBe('people-hidden');
});

/* The dialog on a creator page. */

test('a creator page offers a quiet Hide this person; the dialog asks why and your name, Cancel first, Hide the one red action', async () => {
  await creatorPage(reply(201, hidden()));
  const open = button(host.querySelector('[data-section="creator"]'), 'Hide this person');
  expect(open).toBeTruthy();
  expect(open.classList.contains('w42-primary')).toBe(false);
  expect(dialog()).toBeNull();
  click(open);
  expect(dialog()).not.toBeNull();
  expect(dialog().getAttribute('aria-modal')).toBe('true');
  expect(document.activeElement && document.activeElement.textContent).toBe('Cancel');
  expect([...dialog().querySelectorAll('.w42-primary')].map((b) => b.textContent)).toEqual(['Hide']);
  const reason = field('reason');
  const who = field('who');
  expect(reason.tagName).toBe('TEXTAREA');
  expect(reason.getAttribute('maxlength')).toBe('300');
  expect(who.getAttribute('maxlength')).toBe('60');
  expect(dialog().querySelector('label[for="' + reason.id + '"]').textContent).toContain('Why');
  expect(dialog().querySelector('label[for="' + who.id + '"]').textContent).toContain('Your name');
  expect(text(dialog())).toContain('No contact details');
  expect(text(dialog())).toContain('@fixture_ng_macro');
  click(button(dialog(), 'Cancel'));
  expect(dialog()).toBeNull();
  expect(posts()).toHaveLength(0);
});

test('Escape closes the dialog and sends nothing', async () => {
  await creatorPage(reply(201, hidden()));
  click(button(host, 'Hide this person'));
  flushSync(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
  expect(dialog()).toBeNull();
  expect(posts()).toHaveLength(0);
});

test('Tab and Shift+Tab stay inside the dialog, wrapping at both ends, with Cancel still first', async () => {
  await creatorPage(reply(201, hidden()));
  click(button(host, 'Hide this person'));
  expect(document.activeElement.textContent).toBe('Cancel');
  const tab = (shiftKey = false) => {
    const event = new KeyboardEvent('keydown', {key: 'Tab', shiftKey, bubbles: true, cancelable: true});
    flushSync(() => document.activeElement.dispatchEvent(event));
    return event.defaultPrevented;
  };
  expect(tab()).toBe(false);
  button(dialog(), 'Hide').focus();
  expect(tab()).toBe(true);
  expect(document.activeElement).toBe(field('reason'));
  expect(tab(true)).toBe(true);
  expect(document.activeElement).toBe(button(dialog(), 'Hide'));
  flushSync(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
  expect(dialog()).toBeNull();
});

test('Hide posts creator_id, reason and who, and on the 201 goes to #/people/hidden', async () => {
  await creatorPage(reply(201, hidden()));
  click(button(host, 'Hide this person'));
  await fillAndHide('  ' + REASON + '  ', ' Thandi ');
  expect(posts()).toHaveLength(1);
  expect(JSON.parse(posts()[0].init.body)).toEqual({creator_id: 'c_ng_macro', reason: REASON, who: 'Thandi'});
  expect(window.location.hash).toBe('#/people/hidden');
  expect(dialog()).toBeNull();
});

test('a why under 3 characters or a name under 2 is refused in plain words before anything is sent', async () => {
  await creatorPage(reply(201, hidden()));
  click(button(host, 'Hide this person'));
  await fillAndHide('no', 'Thandi');
  expect(dialog().querySelector('[role="alert"]').textContent).toBe('Say why in 3 to 300 characters.');
  await fillAndHide(REASON, 'T');
  expect(dialog().querySelector('[role="alert"]').textContent).toBe('Give your name in 2 to 60 characters.');
  expect(posts()).toHaveLength(0);
  expect(window.location.hash).toBe('#/creators/c_ng_macro?market=NG');
});

for (const [status, code, message] of [
  [400, 'bad_request', 'reason must not hold an email address or a phone number.'],
  [404, 'not_found', 'No creator with that id.'],
  [503, 'suppression_unavailable', 'Hiding people is not switched on yet.'],
]){
  test(`a ${status} shows the server's own words in the dialog and stays on the page`, async () => {
    await creatorPage(reply(status, {error: code, message}));
    click(button(host, 'Hide this person'));
    await fillAndHide();
    expect(posts()).toHaveLength(1);
    expect(dialog()).not.toBeNull();
    expect(dialog().querySelector('[role="alert"]').textContent).toBe(message);
    expect(window.location.hash).toBe('#/creators/c_ng_macro?market=NG');
  });
}

test('a 401 closes the dialog and hands over to the passcode screen', async () => {
  let asked = 0;
  await creatorPage(reply(401, {error: 'unauthorized', message: 'Passcode required'}), {onAuth: () => { asked += 1; }});
  click(button(host, 'Hide this person'));
  await fillAndHide();
  expect(asked).toBe(1);
  expect(dialog()).toBeNull();
  expect(window.location.hash).toBe('#/creators/c_ng_macro?market=NG');
});

test('matched 0 is not a failure: the list says no one matches now and they will be hidden if they appear, once', async () => {
  await creatorPage(reply(201, hidden({matched: 0})));
  click(button(host, 'Hide this person'));
  await fillAndHide();
  expect(window.location.hash).toBe('#/people/hidden');
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount(<HiddenPeoplePage42 />, [['/api/suppressions', reply(200, {suppressions: [hidden()]})]]);
  expect(text()).toContain(MATCHED_NONE);
  expect(host.querySelector('[role="alert"]')).toBeNull();
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount(<HiddenPeoplePage42 />, [['/api/suppressions', reply(200, {suppressions: [hidden()]})]]);
  expect(text()).not.toContain(MATCHED_NONE);
});

test('a match of one or more says nothing extra on the list', async () => {
  await creatorPage(reply(201, hidden({matched: 1})));
  click(button(host, 'Hide this person'));
  await fillAndHide();
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount(<HiddenPeoplePage42 />, [['/api/suppressions', reply(200, {suppressions: [hidden()]})]]);
  expect(text()).not.toContain(MATCHED_NONE);
});

/* Beside each named member of a community. */

test('each named member of a community has its own Hide this person, which hides that member', async () => {
  await mount(<CommunityPage42 communityId={COMMUNITY} market="NG" />, [
    ['/api/communities/', reply(200, fixture.community)],
    ['/api/suppressions', reply(201, hidden())],
  ]);
  const rows = [...host.querySelectorAll('[data-section="members"] li')];
  expect(rows).toHaveLength(2);
  const opens = rows.map((row) => button(row, 'Hide this person'));
  expect(opens.every(Boolean)).toBe(true);
  expect(opens.map((b) => b.getAttribute('aria-label'))).toEqual(['Hide this person, fixture_ng_mega', 'Hide this person, @fixture_ng_macro']);
  click(opens[1]);
  expect(text(dialog())).toContain('@fixture_ng_macro');
  expect(document.activeElement && document.activeElement.textContent).toBe('Cancel');
  await fillAndHide();
  expect(JSON.parse(posts()[0].init.body)).toEqual({creator_id: 'c_ng_macro', reason: REASON, who: 'Thandi'});
  expect(window.location.hash).toBe('#/people/hidden');
});

test('a community before the sensitive set names no one, so it offers no Hide', async () => {
  const body = fixture.community_before;
  await mount(<CommunityPage42 communityId={body.community.community_id} market="NG" />, [['/api/communities/', reply(200, body)]]);
  expect(button(host, 'Hide this person')).toBeUndefined();
});

/* The list at #/people/hidden. */

const LISTED = {suppressions: [
  hidden({suppression_id: 'sup_app_000000000001', status_at: '2026-09-28T09:00:00+02:00', creator_id: 'c_ng_macro', who: 'Thandi', reason: 'Older hide'}),
  hidden({suppression_id: 'sup_app_000000000002', status_at: '2026-09-29T11:30:00+02:00', creator_id: null, platform: 'tiktok', handle: '@someone_new', who: 'Jo', reason: 'Asked to be removed under POPIA'}),
]};

/* Demo polish, 2 October 2026: a row with no handle reads "A creator" rather
   than the internal creator id, and the way back names the 42 team rather
   than one colleague. The id still keys nothing on screen; the row is still
   there, in its place, with why, who and when. */
test('Hidden people lists each person newest first with platform and handle or A creator, why, who and when', async () => {
  await mount(<HiddenPeoplePage42 />, [['/api/suppressions', reply(200, LISTED)]]);
  expect(calls.map((c) => [c.init.method || 'GET', c.url])).toEqual([['GET', '/api/suppressions']]);
  expect(host.querySelector('h1').textContent).toBe('Hidden people');
  expect(text()).toContain('Ask the 42 team to bring someone back.');
  expect(text()).not.toContain('Albert');
  const rows = [...host.querySelectorAll('[data-suppression]')];
  expect(rows.map((r) => r.getAttribute('data-suppression'))).toEqual(['sup_app_000000000002', 'sup_app_000000000001']);
  expect(text(rows[0])).toContain('TikTok @someone_new');
  expect(text(rows[0])).toContain('Asked to be removed under POPIA');
  expect(text(rows[0])).toContain('hidden by Jo');
  expect(text(rows[0])).toContain('29 September 2026');
  expect(rows[1].querySelector('.pp42-row-title').textContent).toBe('A creator');
  expect(text(rows[1])).not.toContain('c_ng_macro');
  expect(text(rows[1])).toContain('hidden by Thandi');
  expect(text(rows[1])).toContain('28 September 2026');
});

test('the list has no Lift or remove control: no button, no form and no such words', async () => {
  await mount(<HiddenPeoplePage42 />, [['/api/suppressions', reply(200, LISTED)]]);
  expect(host.querySelectorAll('button')).toHaveLength(0);
  expect(host.querySelectorAll('input, select, textarea, form')).toHaveLength(0);
  expect(text().toLowerCase()).not.toMatch(/\blift\b|\bremove\b|\bunhide\b|\bundo\b/);
});

test('an empty list says no one is hidden', async () => {
  await mount(<HiddenPeoplePage42 />, [['/api/suppressions', reply(200, {suppressions: []})]]);
  expect(text()).toContain('No one is hidden.');
  expect(text()).toContain('To hide someone, open their creator page and choose Hide this person.');
  /* Demo polish, 2 October 2026: the way back names the 42 team, not one
     colleague. */
  expect(text()).toContain('Ask the 42 team to bring someone back.');
});

test('a 503 shows the server’s words, and a 401 hands over to the passcode screen', async () => {
  await mount(<HiddenPeoplePage42 />, [['/api/suppressions', reply(503, {error: 'people_unavailable', message: 'People view unavailable.'})]]);
  expect(host.querySelector('[role="alert"]').textContent).toBe('People view unavailable.');
  flushSync(() => root.unmount());
  root = createRoot(host);
  let asked = 0;
  await mount(<HiddenPeoplePage42 onAuth={() => { asked += 1; }} />, [['/api/suppressions', reply(401, {error: 'unauthorized', message: 'Passcode required'})]]);
  expect(asked).toBe(1);
});

test('the people source keeps plain words: no dashes in prose and nothing about age', async () => {
  const source = await Bun.file(new URL('../../people42.jsx', import.meta.url)).text();
  expect(source).not.toMatch(/—|–|gen ?z|millennial|youth|\bage\b/i);
});
