/* Watches, alerts and card feedback on the 42 API (contract.md sections
   10.5 and 10.6). The Alerts screen lists the current watches with Pause and
   Resume, today's alerts and the waiting rules, and a form to watch a
   hashtag, sound, creator, brand or query. Watch on a card opens a small
   rule chooser; the rules whose data does not exist yet are shown and
   disabled. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import topicFixture from './fixtures/topic42_za.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {listWatches, createWatch, pauseWatch, resumeWatch, fetchAlerts, sendFeedback} = await import('../../api42.js');
const {AlertsPage42} = await import('../../alerts42.jsx');
const {ruleWords, ruleFrom, waitingWords, RuleChoice, FIRST_CHOICE, SHOW_UNBUILT_RULES} = await import('../WatchDialog.jsx');
const {TopicPage42} = await import('../../topic42.jsx');

const ITEM = topicFixture.card.item_id;
const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

const W1 = {watch_id: 'w_1', created_at: '2026-09-30T08:00:00+02:00', who: 'passcode', target: {kind: 'hashtag', value: '#amapiano'}, market: 'ZA', rule: {state_in: ['rising']}, label: '#amapiano', status: 'active'};
const W2 = {watch_id: 'w_2', created_at: '2026-09-29T08:00:00+02:00', who: 'passcode', target: {kind: 'brand', value: 'Castle Lager'}, market: 'all', rule: {reach_over: 50}, label: 'Castle Lager', status: 'paused'};
const W3 = {watch_id: 'w_3', created_at: '2026-09-28T08:00:00+01:00', who: 'passcode', target: {kind: 'creator', value: '@fixture.creator'}, market: 'NG', rule: {creator_surge: true}, label: '@fixture.creator', status: 'active'};
const W4 = {watch_id: 'w_4', created_at: '2026-09-27T08:00:00+02:00', who: 'passcode', target: {kind: 'item', item_id: ITEM}, market: 'ZA', rule: {ratio_over: 2.5}, label: '#fixture_za_step', status: 'active'};
const WATCHES = {watches: [W1, W2, W3, W4]};
const ALERTS = {date: '2026-09-30', alerts: [
  {watch_id: 'w_1', label: '#amapiano', item_id: 'item_ama', market: 'ZA', fired_because: 'Entered Rising', card: {item_id: 'item_ama', title: '#amapiano'}, since: '2026-09-30'},
  {watch_id: 'w_3', waiting: 'Waiting for creator views detection'},
]};

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
const choose = (select, value) => flushSync(() => {
  select.value = value;
  select.dispatchEvent(new Event('change', {bubbles: true}));
});
function typeInto(element, value){
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(element, value);
  flushSync(() => element.dispatchEvent(new window.Event('input', {bubbles: true})));
}
const option = (scope, words) => [...scope.querySelectorAll('label')].find((l) => l.textContent.includes(words)).querySelector('input');
const row = (id) => host.querySelector('[data-watch="' + id + '"]');
const posted = (path) => calls.filter((c) => c.url === path && c.init.method === 'POST');
const bodyOf = (call) => JSON.parse(call.init.body);

const standard = () => [
  ['/api/watches/w_1/pause', reply(200, {...W1, status: 'paused'})],
  ['/api/watches/w_2/resume', reply(200, {...W2, status: 'active'})],
  ['/api/watches', (url, init) => (init && init.method === 'POST'
    ? reply(201, {watch_id: 'w_new', created_at: '2026-09-30T09:00:00+02:00', who: 'passcode', ...JSON.parse(init.body), status: 'active'})
    : reply(200, WATCHES))],
  ['/api/alerts', reply(200, ALERTS)],
];

async function mount(routes = standard(), props = {}){
  serve(routes);
  flushSync(() => root.render(<AlertsPage42 {...props} />));
  await settle();
}

test('the watch, alert and feedback calls build the contract paths, methods and bodies', async () => {
  serve([['/api/', reply(200, {})]]);
  await listWatches();
  await createWatch({target: {kind: 'hashtag', value: '#amapiano'}, market: 'ZA', rule: {reach_over: 50}, label: '#amapiano'});
  await pauseWatch('w 1');
  await resumeWatch('w_1');
  await fetchAlerts('2026-09-30');
  await fetchAlerts();
  await sendFeedback({target: {kind: 'card', item_id: 'abc', market: 'ZA', date: '2026-09-30'}, value: 'real'});
  expect(calls.map((c) => [c.init.method || 'GET', c.url])).toEqual([
    ['GET', '/api/watches'],
    ['POST', '/api/watches'],
    ['POST', '/api/watches/w%201/pause'],
    ['POST', '/api/watches/w_1/resume'],
    ['GET', '/api/alerts?date=2026-09-30'],
    ['GET', '/api/alerts'],
    ['POST', '/api/feedback'],
  ]);
  expect(bodyOf(calls[1])).toEqual({target: {kind: 'hashtag', value: '#amapiano'}, market: 'ZA', rule: {reach_over: 50}, label: '#amapiano'});
  expect(bodyOf(calls[6])).toEqual({target: {kind: 'card', item_id: 'abc', market: 'ZA', date: '2026-09-30'}, value: 'real'});
  for (const call of calls) expect(call.init.headers['X-Passcode']).toBe('test-pass');
  expect(calls[1].init.headers['Content-Type']).toBe('application/json');
});

test('a failed write keeps the API code and status, and marks 401 as auth', async () => {
  serve([
    ['/api/watches', reply(400, {error: 'bad_request', message: 'market must be ZA, NG, KE or all.'})],
    ['/api/feedback', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})],
  ]);
  const bad = await createWatch({target: {kind: 'hashtag', value: 'x'}, market: 'XX', rule: {breakout: true}}).catch((error) => error);
  expect(bad.status).toBe(400);
  expect(bad.code).toBe('bad_request');
  expect(bad.message).toBe('market must be ZA, NG, KE or all.');
  const auth = await sendFeedback({target: {kind: 'card'}, value: 'real'}).catch((error) => error);
  expect(auth.auth).toBe(true);
});

test('every rule reads in words', () => {
  expect(ruleWords({state_in: ['rising']})).toBe('When it starts rising');
  expect(ruleWords({state_in: ['rising', 'peaking']})).toBe('When it enters Rising or Peaking');
  expect(ruleWords({state_in: ['new_to_42']})).toBe('When it enters New to 42');
  expect(ruleWords({ratio_over: 2.5})).toBe('When growth passes 2.5 times');
  expect(ruleWords({reach_over: 50})).toBe('When reach passes 50 creators');
  expect(ruleWords({breakout: true})).toBe('When a creator breaks out');
  expect(ruleWords({tone_flip: true})).toBe('When the tone flips');
  expect(ruleWords({creator_surge: true})).toBe("When the creator's views surge");
});

test('the Alerts screen reads the watches and the alerts, under one heading', async () => {
  await mount();
  expect(host.querySelector('h1').textContent).toBe('Alerts');
  expect(text()).toContain('Watch a topic, sound, creator or brand');
  expect(calls.map((c) => c.url).sort()).toEqual(['/api/alerts', '/api/watches']);
  expect(calls.every((c) => c.init.headers['X-Passcode'] === 'test-pass')).toBe(true);
});

test('each watch shows its target, market, rule in words and status, with Pause or Resume', async () => {
  await mount();
  expect(host.querySelectorAll('[data-watch]')).toHaveLength(4);
  const one = text(row('w_1'));
  for (const words of ['#amapiano', 'Hashtag', 'South Africa', 'When it starts rising', 'Active']) expect(one).toContain(words);
  expect(button(row('w_1'), 'Pause')).toBeDefined();
  const two = text(row('w_2'));
  for (const words of ['Castle Lager', 'Brand', 'All markets', 'When reach passes 50 creators', 'Paused']) expect(two).toContain(words);
  expect(button(row('w_2'), 'Resume')).toBeDefined();
  expect(text(row('w_3'))).toContain('Waiting for creator views detection');
  expect(text(row('w_4'))).toContain('When growth passes 2.5 times');
  expect(row('w_4').querySelector('a').getAttribute('href')).toBe('#/t/' + ITEM + '?market=ZA');
});

test('Pause and Resume post to the watch and show its new status', async () => {
  await mount();
  click(button(row('w_1'), 'Pause'));
  await settle();
  expect(posted('/api/watches/w_1/pause')).toHaveLength(1);
  expect(text(row('w_1'))).toContain('Paused');
  expect(button(row('w_1'), 'Resume')).toBeDefined();
  click(button(row('w_2'), 'Resume'));
  await settle();
  expect(posted('/api/watches/w_2/resume')).toHaveLength(1);
  expect(text(row('w_2'))).toContain('Active');
  expect(button(row('w_2'), 'Pause')).toBeDefined();
});

test('a pause that fails says so in words and keeps the watch as it was', async () => {
  await mount([['/api/watches/w_1/pause', reply(500, {error: 'internal', message: 'The watch could not be saved; try again.'})], ...standard().slice(1)]);
  click(button(row('w_1'), 'Pause'));
  await settle();
  expect(text(row('w_1'))).toContain('The watch could not be saved; try again.');
  expect(text(row('w_1'))).toContain('Active');
  expect(button(row('w_1'), 'Pause')).toBeDefined();
});

test("today's alerts link to their topic, and waiting rules are listed with their watch", async () => {
  await mount();
  const alerts = host.querySelector('[data-section="alerts"]');
  const link = [...alerts.querySelectorAll('a')].find((a) => a.textContent === '#amapiano');
  expect(link.getAttribute('href')).toBe('#/t/item_ama?market=ZA');
  expect(text(alerts)).toContain('Entered Rising');
  expect(text(alerts)).toContain('1 alert today');
  expect(text(alerts)).toContain('@fixture.creator');
  expect(text(alerts)).toContain('Waiting for creator views detection');
});

const HELD = {rule: 'G4', reason: 'likely_coordinated', reason_text: 'Likely coordinated: many new accounts posting the same words'};
const MIXED = {date: '2026-09-30', alerts: [
  {watch_id: 'w_4', label: '#fixture_za_step', item_id: ITEM, market: 'ZA', fired_because: 'Growth passed 2.5 times its baseline', card: {item_id: ITEM, title: '#fixture_za_step', held_back: HELD}, since: '2026-09-30'},
  {watch_id: 'w_1', waiting: 'Waiting for a second detect run to compare against'},
]};

test('an alert on a held-back item says Held back with its reason, never as a plain trend', async () => {
  await mount([...standard().slice(0, 3), ['/api/alerts', reply(200, MIXED)]]);
  const alerts = host.querySelector('[data-section="alerts"]');
  const held = alerts.querySelector('[data-held]');
  expect(held).not.toBeNull();
  expect(text(held)).toContain('#fixture_za_step');
  expect(text(held)).toContain('Held back: Likely coordinated: many new accounts posting the same words');
});

test('waiting entries are a quiet list with their own words, not alerts, and the watch row says so', async () => {
  await mount([...standard().slice(0, 3), ['/api/alerts', reply(200, MIXED)]]);
  const alerts = host.querySelector('[data-section="alerts"]');
  expect(text(alerts)).toContain('1 alert today');
  const waiting = alerts.querySelector('.a42-waiting');
  expect(text(waiting)).toContain('#amapiano');
  expect(text(waiting)).toContain('Waiting for a second detect run to compare against');
  expect(waiting.querySelector('a')).toBeNull();
  expect(text(row('w_1'))).toContain('Waiting for a second detect run to compare against');
});

test('no alerts today says so', async () => {
  await mount([...standard().slice(0, 3), ['/api/alerts', reply(200, {date: '2026-09-30', alerts: []})]]);
  expect(text(host.querySelector('[data-section="alerts"]'))).toContain('No alerts today.');
});

test('the add form watches a hashtag in a market with the chosen rule', async () => {
  await mount();
  const form = host.querySelector('[data-section="add"] form');
  choose(form.querySelector('select[name="kind"]'), 'sound');
  typeInto(form.querySelector('input[name="value"]'), '  pula groove  ');
  choose(form.querySelector('select[name="market"]'), 'KE');
  click(option(form, 'When reach passes'));
  typeInto(form.querySelector('input[name="reach"]'), '80');
  click(button(form, 'Watch'));
  await settle();
  const made = posted('/api/watches');
  expect(made).toHaveLength(1);
  expect(bodyOf(made[0])).toEqual({target: {kind: 'sound', value: 'pula groove'}, market: 'KE', rule: {reach_over: 80}, label: 'pula groove'});
  expect(host.querySelectorAll('[data-watch]')).toHaveLength(5);
  expect(text(row('w_new'))).toContain('When reach passes 80 creators');
  expect(text(form)).toContain('Watching pula groove');
  expect(form.querySelector('input[name="value"]').value).toBe('');
});

test('the add form asks for words before it posts, and a rejected watch says why', async () => {
  await mount([['/api/watches', (url, init) => (init && init.method === 'POST'
    ? reply(400, {error: 'bad_request', message: 'A hashtag target needs value, 1 to 200 characters.'})
    : reply(200, WATCHES))], ['/api/alerts', reply(200, ALERTS)]]);
  const form = host.querySelector('[data-section="add"] form');
  click(button(form, 'Watch'));
  await settle();
  expect(posted('/api/watches')).toHaveLength(0);
  expect(text(form)).toContain('Type the hashtag to watch.');
  typeInto(form.querySelector('input[name="value"]'), '#amapiano');
  click(button(form, 'Watch'));
  await settle();
  expect(posted('/api/watches')).toHaveLength(1);
  expect(text(form)).toContain('A hashtag target needs value, 1 to 200 characters.');
});

/* Demo polish, 2 October 2026: a rule with no data yet is not offered at
   all, so the demo shows only choices that work; such a rule would stay in
   code behind SHOW_UNBUILT_RULES (off) and the chooser's showUnbuilt. Every
   rule has its data now: breakout (breakout_signals), tone flip
   (v_item_tone_daily) and creator surge (detect's watch matches), so none is
   left behind the flag. A creator surge is offered only for a creator. */
const NEEDS = ['Needs breakout detection', 'Needs tone detection', 'Needs creator views detection'];

test('every rule is offered, the views surge only for a creator, and nothing is left behind the flag', async () => {
  await mount();
  const form = host.querySelector('[data-section="add"] form');
  expect(SHOW_UNBUILT_RULES).toBe(false);
  const values = () => [...form.querySelectorAll('input[type="radio"]')].map((r) => r.value);
  expect(values()).toEqual(['rising', 'ratio', 'reach', 'breakout', 'tone_flip']);
  for (const words of [...NEEDS, "When the creator's views surge"]) expect(text(form)).not.toContain(words);
  expect(option(form, 'When it starts rising').disabled).toBe(false);
  expect(option(form, 'When it starts rising').checked).toBe(true);
  choose(form.querySelector('select[name="kind"]'), 'creator');
  expect(values()).toEqual(['rising', 'ratio', 'reach', 'breakout', 'tone_flip', 'creator_surge']);
  expect(option(form, "When the creator's views surge").disabled).toBe(false);
  flushSync(() => root.render(<RuleChoice choice={FIRST_CHOICE} onChange={() => {}} showUnbuilt />));
  expect(host.querySelectorAll('input[type="radio"]')).toHaveLength(5);
  for (const words of NEEDS) expect(text(host)).not.toContain(words);
});

test('the tone flip and the creator surge are rules you can choose, and neither reads as waiting', () => {
  expect(ruleFrom({...FIRST_CHOICE, kind: 'tone_flip'})).toEqual({rule: {tone_flip: true}});
  expect(ruleFrom({...FIRST_CHOICE, kind: 'creator_surge'}, {creator: true})).toEqual({rule: {creator_surge: true}});
  expect(ruleFrom({...FIRST_CHOICE, kind: 'creator_surge'})).toEqual({error: 'A views surge needs a creator to watch.'});
  expect(ruleWords({tone_flip: true})).toBe('When the tone flips');
  expect(ruleWords({creator_surge: true})).toBe("When the creator's views surge");
  expect(waitingWords({tone_flip: true})).toBe(null);
  expect(waitingWords({creator_surge: true})).toBe(null);
});

test('choosing the tone flip or a creator surge on the Alerts screen sends that rule', async () => {
  await mount();
  const form = host.querySelector('[data-section="add"] form');
  typeInto(form.querySelector('input[name="value"]'), '#amapiano');
  click(option(form, 'When the tone flips'));
  click(button(form, 'Watch'));
  await settle();
  choose(form.querySelector('select[name="kind"]'), 'creator');
  typeInto(form.querySelector('input[name="value"]'), '@fixture.creator');
  click(option(form, "When the creator's views surge"));
  click(button(form, 'Watch'));
  await settle();
  const sent = posted('/api/watches').map(bodyOf);
  expect(sent.map((b) => [b.target.kind, b.rule])).toEqual([['hashtag', {tone_flip: true}], ['creator', {creator_surge: true}]]);
});

test('a creator surge chosen for a creator and then kept for another kind is refused in words', async () => {
  await mount();
  const form = host.querySelector('[data-section="add"] form');
  choose(form.querySelector('select[name="kind"]'), 'creator');
  click(option(form, "When the creator's views surge"));
  choose(form.querySelector('select[name="kind"]'), 'hashtag');
  typeInto(form.querySelector('input[name="value"]'), '#amapiano');
  click(button(form, 'Watch'));
  await settle();
  expect(posted('/api/watches')).toHaveLength(0);
  expect(text(form)).toContain('A views surge needs a creator to watch.');
});

test('a creator breakout is a rule you can choose, and it never reads as waiting', async () => {
  await mount();
  const form = host.querySelector('[data-section="add"] form');
  const breakout = option(form, 'When a creator breaks out');
  expect(breakout.disabled).toBe(false);
  expect(ruleFrom({...FIRST_CHOICE, kind: 'breakout'})).toEqual({rule: {breakout: true}});
  expect(ruleWords({breakout: true})).toBe('When a creator breaks out');
  expect(waitingWords({breakout: true})).toBe(null);
});

test('choosing breakout on the Alerts screen sends the breakout rule', async () => {
  await mount();
  const form = host.querySelector('[data-section="add"] form');
  typeInto(form.querySelector('input[name="value"]'), '#amapiano');
  click(option(form, 'When a creator breaks out'));
  click(button(form, 'Watch'));
  await settle();
  const sent = posted('/api/watches');
  expect(sent).toHaveLength(1);
  expect(bodyOf(sent[0]).rule).toEqual({breakout: true});
});

test('a growth figure that is not a number above zero is refused in words', async () => {
  await mount();
  const form = host.querySelector('[data-section="add"] form');
  typeInto(form.querySelector('input[name="value"]'), '#amapiano');
  click(option(form, 'When growth passes'));
  typeInto(form.querySelector('input[name="ratio"]'), '0');
  click(button(form, 'Watch'));
  await settle();
  expect(posted('/api/watches')).toHaveLength(0);
  expect(text(form)).toContain('Growth must be a number above 0.');
});

test('a 401 on the Alerts screen hands the reader to the passcode flow', async () => {
  let asked = 0;
  await mount([['/api/', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]], {onAuth: () => { asked += 1; }});
  expect(asked).toBeGreaterThanOrEqual(1);
  expect(host.querySelector('[data-watch]')).toBeNull();
});

test('watches that cannot load say so and offer Try again', async () => {
  let answer = reply(500, {error: 'internal', message: 'Something broke.'});
  await mount([['/api/watches', () => answer], ['/api/alerts', reply(200, ALERTS)]]);
  expect(text()).toContain('The watches could not load.');
  answer = reply(200, WATCHES);
  click(button(host.querySelector('[data-section="watches"]'), 'Try again'));
  await settle();
  expect(host.querySelectorAll('[data-watch]')).toHaveLength(4);
});

test('alerts that cannot load say so and offer Try again, like the watches', async () => {
  let answer = reply(500, {error: 'internal', message: 'Something broke.'});
  await mount([...standard().slice(0, 3), ['/api/alerts', () => answer]]);
  const alerts = host.querySelector('[data-section="alerts"]');
  expect(text(alerts)).toContain('The alerts could not load.');
  expect(alerts.querySelector('[role="alert"]')).not.toBeNull();
  answer = reply(200, ALERTS);
  click(button(alerts, 'Try again'));
  await settle();
  expect(text(host.querySelector('[data-section="alerts"]'))).toContain('1 alert today');
  expect(calls.filter((c) => c.url === '/api/alerts')).toHaveLength(2);
});

test('a waiting entry uses its own label first, then the watch list, then A watch', async () => {
  const waiting = {date: '2026-09-30', alerts: [
    {watch_id: 'w_1', label: 'Own words for amapiano', waiting: 'Waiting for tone detection'},
    {watch_id: 'w_3', waiting: 'Waiting for tone detection'},
    {watch_id: 'w_gone', waiting: 'Waiting for creator views detection'},
  ]};
  await mount([...standard().slice(0, 3), ['/api/alerts', reply(200, waiting)]]);
  const rows = [...host.querySelectorAll('.a42-waiting li')].map((li) => text(li));
  expect(rows).toEqual([
    'Own words for amapiano: Waiting for tone detection',
    '@fixture.creator: Waiting for tone detection',
    'A watch: Waiting for creator views detection',
  ]);
});

test('a 401 from both reads hands the reader to the passcode flow once', async () => {
  let asked = 0;
  await mount([['/api/', reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'})]], {onAuth: () => { asked += 1; }});
  expect(calls.map((c) => c.url).sort()).toEqual(['/api/alerts', '/api/watches']);
  expect(asked).toBe(1);
});

/* Watch on a card: the topic page is the one page that always carries it. */
async function mountTopic(create){
  serve([['/api/topics/', reply(200, topicFixture)]]);
  flushSync(() => root.render(<TopicPage42 itemId={ITEM} market="ZA" onCreateWatch={create} />));
  await settle();
}

test('Watch on a card opens a rule chooser, and a new watch turns the button to Watching', async () => {
  const made = [];
  await mountTopic((body) => { made.push(body); return Promise.resolve({watch_id: 'w_9', ...body, status: 'active'}); });
  const card = host.querySelector('[data-card]');
  const watch = button(card, 'Watch');
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  watch.focus();
  click(watch);
  const dialog = host.querySelector('[role="dialog"]');
  expect(dialog).not.toBeNull();
  expect(dialog.getAttribute('aria-modal')).toBe('true');
  expect(text(dialog)).toContain(topicFixture.card.title);
  for (const words of ['When it starts rising', 'When growth passes', 'When reach passes', 'When a creator breaks out', 'When the tone flips']) expect(option(dialog, words).disabled).toBe(false);
  /* Demo polish, 2 October 2026: the chooser offers only the rules that
     work (see the add form test); this card is not a creator, so a views
     surge is not one of them. */
  expect(dialog.querySelectorAll('input[type="radio"]')).toHaveLength(5);
  for (const words of [...NEEDS, "When the creator's views surge"]) expect(text(dialog)).not.toContain(words);
  click(button(dialog, 'Watch'));
  await settle();
  expect(made).toEqual([{target: {kind: 'item', item_id: ITEM}, market: 'ZA', rule: {state_in: ['rising']}, label: topicFixture.card.title}]);
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  const after = button(card, 'Watching');
  expect(after).toBeDefined();
  expect(after.getAttribute('aria-pressed')).toBe('true');
  expect(document.activeElement).toBe(after);
});

test('the chooser sends the growth rule it was given', async () => {
  const made = [];
  await mountTopic((body) => { made.push(body); return Promise.resolve({watch_id: 'w_9', ...body}); });
  click(button(host.querySelector('[data-card]'), 'Watch'));
  const dialog = host.querySelector('[role="dialog"]');
  click(option(dialog, 'When growth passes'));
  typeInto(dialog.querySelector('input[name="ratio"]'), '3');
  click(button(dialog, 'Watch'));
  await settle();
  expect(made[0].rule).toEqual({ratio_over: 3});
});

test('Cancel and Escape close the chooser without a watch', async () => {
  const made = [];
  await mountTopic((body) => { made.push(body); return Promise.resolve(body); });
  const card = host.querySelector('[data-card]');
  click(button(card, 'Watch'));
  click(button(host.querySelector('[role="dialog"]'), 'Cancel'));
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  click(button(card, 'Watch'));
  flushSync(() => host.querySelector('[role="dialog"]').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(made).toHaveLength(0);
  expect(button(card, 'Watch')).toBeDefined();
});

test('the chooser keeps Tab and Shift+Tab inside itself, wrapping at both ends, and Escape still closes it', async () => {
  const made = [];
  await mountTopic((body) => { made.push(body); return Promise.resolve(body); });
  const card = host.querySelector('[data-card]');
  const watch = button(card, 'Watch');
  watch.focus();
  click(watch);
  const dialog = host.querySelector('[role="dialog"]');
  const first = option(dialog, 'When it starts rising');
  expect(document.activeElement).toBe(first);
  const tab = (shiftKey = false) => {
    const event = new KeyboardEvent('keydown', {key: 'Tab', shiftKey, bubbles: true, cancelable: true});
    flushSync(() => document.activeElement.dispatchEvent(event));
    return event.defaultPrevented;
  };
  expect(tab(true)).toBe(true);
  expect(document.activeElement).toBe(button(dialog, 'Cancel'));
  expect(tab()).toBe(true);
  expect(document.activeElement).toBe(first);
  expect(tab()).toBe(false);
  flushSync(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
  expect(host.querySelector('[role="dialog"]')).toBeNull();
  expect(made).toHaveLength(0);
  expect(document.activeElement).toBe(button(card, 'Watch'));
});

test('with a later rule chosen, the chooser wraps Tab to that radio, not the first one', async () => {
  await mountTopic((body) => Promise.resolve(body));
  click(button(host.querySelector('[data-card]'), 'Watch'));
  const dialog = host.querySelector('[role="dialog"]');
  const tab = (shiftKey = false) => {
    const event = new KeyboardEvent('keydown', {key: 'Tab', shiftKey, bubbles: true, cancelable: true});
    flushSync(() => document.activeElement.dispatchEvent(event));
    return event.defaultPrevented;
  };
  const growth = option(dialog, 'When growth passes');
  click(growth);
  expect(growth.checked).toBe(true);
  growth.focus();
  expect(tab(true)).toBe(true);
  expect(document.activeElement).toBe(button(dialog, 'Cancel'));
  expect(tab()).toBe(true);
  expect(document.activeElement).toBe(growth);
  const reach = option(dialog, 'When reach passes');
  click(reach);
  expect(reach.checked).toBe(true);
  reach.focus();
  expect(tab(true)).toBe(false);
  button(dialog, 'Cancel').focus();
  expect(tab()).toBe(true);
  expect(document.activeElement).toBe(dialog.querySelector('input[name="ratio"]'));
  expect(dialog.contains(document.activeElement)).toBe(true);
});

test('a watch that fails to save says so in words and leaves the card on Watch', async () => {
  const failure = Object.assign(new Error('The watch could not be saved; try again.'), {status: 500, code: 'internal'});
  await mountTopic(() => Promise.reject(failure));
  const card = host.querySelector('[data-card]');
  click(button(card, 'Watch'));
  click(button(host.querySelector('[role="dialog"]'), 'Watch'));
  await settle();
  const dialog = host.querySelector('[role="dialog"]');
  expect(dialog.querySelector('[role="alert"]').textContent).toContain('The watch could not be saved; try again.');
  expect(button(card, 'Watch')).toBeDefined();
});

test('a 401 while watching hands the reader to the passcode flow', async () => {
  let asked = 0;
  serve([['/api/topics/', reply(200, topicFixture)]]);
  const failure = Object.assign(new Error('Missing or wrong passcode.'), {status: 401, auth: true});
  flushSync(() => root.render(<TopicPage42 itemId={ITEM} market="ZA" onAuth={() => { asked += 1; }} onCreateWatch={() => Promise.reject(failure)} />));
  await settle();
  click(button(host.querySelector('[data-card]'), 'Watch'));
  click(button(host.querySelector('[role="dialog"]'), 'Watch'));
  await settle();
  expect(asked).toBe(1);
  expect(host.querySelector('[role="dialog"]')).toBeNull();
});

test('no watches says what to do next, and the add form groups its fields under labels of one size', async () => {
  await mount([['/api/watches', reply(200, {watches: []})], ['/api/alerts', reply(200, ALERTS)]]);
  /* Restated, layout pass 4 October 2026: the add form sits beside the list on a wide screen, so the line no longer says below. */
  expect(text(host.querySelector('[data-section="watches"]'))).toContain('No watches yet. Add one and 42 checks it each time the trends update.');
  const form = host.querySelector('[data-section="add"] form');
  expect(form.querySelector('.a42-fields').children).toHaveLength(3);
  for (const field of form.querySelectorAll('.a42-fields > .a42-field')) expect(field.firstElementChild.tagName).toBe('LABEL');
});

test('copy carries no generation words and the stylesheet keeps the four sizes and one red action', async () => {
  for (const file of ['../../alerts42.jsx', '../WatchDialog.jsx']){
    const source = await Bun.file(new URL(file, import.meta.url)).text();
    expect(source).not.toMatch(/gen ?z|millennial|youth|google trends|prompt pulse|nano banana/i);
  }
  const css = await Bun.file(new URL('../../styles/alerts42.css', import.meta.url)).text();
  const sizes = [...css.matchAll(/font-size:\s*([^;]+);/g)].map((m) => m[1].trim());
  expect(sizes.length).toBeGreaterThan(0);
  /* Restated, design audit 2 October 2026: the four sizes were 13, 16, 21 and
     32 px; they now sit on the package scale at 12, 16, 18 and 32 px, the
     same steps as the app type roles (Bringhurst, modular scale). */
  expect(sizes.every((size) => /^var\(--t42-(small|body|lead|head)\)$/.test(size) || /^(12|16|18|32)px$/.test(size))).toBe(true);
  const scale = {};
  css.replace(/\.w42-backdrop, \.f42-feedback \{([^}]*)\}/, (_, body) => {
    for (const m of body.matchAll(/(--t42-[a-z]+):\s*([^;]+);/g)) scale[m[1]] = m[2].trim();
  });
  expect(scale).toEqual({'--t42-small': '12px', '--t42-body': '16px', '--t42-lead': '18px', '--t42-head': '32px'});
  const red = css.split('}').filter((rule) => /var\(--accent\)/.test(rule) && !/focus-visible/.test(rule));
  expect(red.every((rule) => /\.w42-primary/.test(rule))).toBe(true);
});

/* Design critique, 2 October 2026: the subtitle and the empty watch list
   both said 42 checks each time the trends update. The page says it once. */
test('the subtitle and the empty watch list do not say the same thing twice', async () => {
  await mount([['/api/watches', reply(200, {watches: []})], ['/api/alerts', reply(200, ALERTS)]]);
  expect(text().split('each time').length - 1).toBe(1);
  expect(text(host.querySelector('header'))).toContain('Watch a topic, sound, creator or brand and get an alert when it moves.');
  const shared = [...host.querySelectorAll('[data-section="watches"] p')].find((p) => p.textContent === 'This list is shared with your team.');
  expect(shared.classList.contains('a42-note')).toBe(true);
});

/* Visual QA, 5 October 2026 (AL01): with Nigeria picked in the header, the
   Add a watch form started on South Africa. */
test('the watch form starts on the header market and follows it until the reader picks one', async () => {
  await mount(standard(), {region: 'NG'});
  const select = () => host.querySelector('select[name="market"]');
  expect(select().value).toBe('NG');
  flushSync(() => root.render(<AlertsPage42 region="KE" />));
  await settle();
  expect(select().value).toBe('KE');
  flushSync(() => root.render(<AlertsPage42 region="ALL" />));
  await settle();
  expect(select().value).toBe('all');
  const pick = select();
  Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set.call(pick, 'ZA');
  pick.dispatchEvent(new window.Event('change', {bubbles: true}));
  await settle();
  flushSync(() => root.render(<AlertsPage42 region="NG" />));
  await settle();
  expect(select().value).toBe('ZA');
});
