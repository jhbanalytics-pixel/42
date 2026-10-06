/* The brand lens, creator brand-fit and context pack under Ask
   (contract.md section 15.2). Each fills an ask with its skill and tier,
   asks for a confirm tap naming its credit ceiling with Cancel taking first
   focus, then follows the ask on #/ask?follow=. Unless /api/health says
   t2_ready is true, the brand lens and context pack are not shown and send
   nothing. Creator fit is offered only from a creator page 42 may show. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import fixture from './fixtures/people42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const api = await import('../../api42.js');
const {SkillForms, t2ReadyFrom} = await import('../../skills42.jsx');
const {CreatorPage42} = await import('../../people42.jsx');
const {parseAskQuery} = await import('../../App.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const STARTED = {ask_id: 'a_20260930_0000skl1', status: 'running', events_url: '/api/ask/a_20260930_0000skl1/events', url: '/api/ask/a_20260930_0000skl1'};

function serve(routes){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init: init || {}});
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer(String(url), init || {}) : answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

beforeEach(() => {
  calls = [];
  localStorage.setItem('pulse_passcode', 'test-pass');
  window.location.hash = '#/ask';
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

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const text = (scope = host) => scope.textContent.replace(/\s+/g, ' ');
const button = (scope, label) => [...scope.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const submit = (form) => flushSync(() => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
function typeInto(element, value){
  const proto = element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(element, value);
  flushSync(() => element.dispatchEvent(new window.Event('input', {bubbles: true})));
}
const choose = (select, value) => flushSync(() => {
  select.value = value;
  select.dispatchEvent(new Event('change', {bubbles: true}));
});
const skill = (name) => host.querySelector('[data-skill="' + name + '"]');
const field = (scope, name) => scope.querySelector('[name="' + name + '"]');
const dialog = () => host.querySelector('[role="dialog"]');
const asks = () => calls.filter((c) => c.url === '/api/ask' && c.init.method === 'POST');
const bodyOf = (call) => JSON.parse(call.init.body);

async function mount(props = {}, routes = [['/api/ask', reply(202, STARTED)]]){
  serve(routes);
  flushSync(() => root.render(<SkillForms market="ZA" {...props} />));
  await settle();
}

/* ---------------- the call ---------------- */

test('askQuestion posts /api/ask with the skill and tier when given, and without them when not', async () => {
  serve([['/api/ask', reply(202, STARTED)]]);
  await api.askQuestion({question: 'Brand lens on Fixture Cola in South Africa', market: 'ZA', tier: 'T2', skill: 'brand-implication'});
  await api.askQuestion({question: 'What is new in amapiano?', market: null});
  expect(calls.map((c) => [c.init.method, c.url])).toEqual([['POST', '/api/ask'], ['POST', '/api/ask']]);
  expect(bodyOf(calls[0])).toEqual({question: 'Brand lens on Fixture Cola in South Africa', market: 'ZA', tier: 'T2', skill: 'brand-implication'});
  expect(bodyOf(calls[1])).toEqual({question: 'What is new in amapiano?', market: null});
  expect(calls[0].init.headers['X-Passcode']).toBe('test-pass');
});

test('the Ask hash can carry the creator a fit is read for', () => {
  expect(parseAskQuery('#/ask?fit=c_ng_macro&market=NG').fit).toBe('c_ng_macro');
  expect(parseAskQuery('#/ask?q=Hello').fit).toBeNull();
});

/* ---------------- the T2 flag from the API ---------------- */

/* f42-agent opens T2 for these two skills only while F42_T2_READY is on, and
   /api/health says so as t2_ready. Anything but true keeps the forms hidden. */
test('the T2 flag is read from /api/health and is off unless it says true', () => {
  expect(t2ReadyFrom({t2_ready: true})).toBe(true);
  for (const health of [{t2_ready: false}, {t2_ready: 'true'}, {t2_ready: 1}, {}, null, undefined, 'ok']){
    expect(t2ReadyFrom(health)).toBe(false);
  }
});

test('with the flag on from /api/health, brand lens and context pack are offered', async () => {
  await mount({t2Ready: t2ReadyFrom({ok: true, t2_ready: true})});
  expect(skill('brand-lens')).not.toBeNull();
  expect(skill('context-pack')).not.toBeNull();
  expect(text()).toContain('Ask with a skill');
});

test('with the flag off from /api/health, brand lens and context pack are not offered', async () => {
  await mount({t2Ready: t2ReadyFrom({ok: true, t2_ready: false})});
  expect(skill('brand-lens')).toBeNull();
  expect(skill('context-pack')).toBeNull();
  expect(host.querySelector('.sk42-skills')).toBeNull();
  expect(asks()).toHaveLength(0);
});

/* ---------------- while Ask refuses T2 ---------------- */

/* QA, 2 Oct 2026, item 7: "Brand lens: Not ready yet" and "Context pack: Not
   ready yet" sat under every answer. A skill Ask cannot run is not shown, and
   with no skill to offer the whole section stays away. */
test('Ask refuses T2 until the API says otherwise, so brand lens and context pack are not shown and nothing can be sent', async () => {
  expect(t2ReadyFrom(undefined)).toBe(false);
  await mount();
  expect(skill('brand-lens')).toBeNull();
  expect(skill('context-pack')).toBeNull();
  expect(text()).not.toContain('Not ready yet');
  expect(host.querySelector('.sk42-skills')).toBeNull();
  expect(dialog()).toBeNull();
  expect(asks()).toHaveLength(0);
});

test('without a creator and with T2 refused, the skill section is not shown at all', async () => {
  await mount();
  expect(skill('creator-fit')).toBeNull();
  expect(text()).not.toContain('Ask with a skill');
  expect(text()).not.toContain('Creator fit opens from a creator page.');
});

test('once T2 is ready and with no creator, creator fit still says where it comes from', async () => {
  await mount({t2Ready: true});
  expect(skill('creator-fit')).toBeNull();
  expect(text()).toContain('Creator fit opens from a creator page.');
});

/* ---------------- the brand lens, once T2 is ready ---------------- */

test('the brand lens takes a brand, up to four competitors and a market', async () => {
  await mount({t2Ready: true});
  const form = skill('brand-lens');
  expect(field(form, 'brand')).not.toBeNull();
  expect(form.querySelectorAll('input[name="competitor"]')).toHaveLength(4);
  expect([...field(form, 'market').querySelectorAll('option')].map((o) => o.value)).toEqual(['ZA', 'NG', 'KE']);
  expect(text(form)).not.toContain('Not ready yet');
});

test('the brand lens asks for a confirm naming up to 300 credits, with Cancel first; Cancel sends nothing', async () => {
  await mount({t2Ready: true});
  const form = skill('brand-lens');
  typeInto(field(form, 'brand'), 'Fixture Cola');
  submit(form.querySelector('form'));
  expect(dialog()).not.toBeNull();
  expect(text(dialog())).toContain('up to 300 credits');
  expect([...dialog().querySelectorAll('.w42-primary')].map((b) => b.textContent)).toEqual(['Ask']);
  expect(document.activeElement && document.activeElement.textContent).toBe('Cancel');
  click(button(dialog(), 'Cancel'));
  expect(dialog()).toBeNull();
  expect(asks()).toHaveLength(0);
});

test('the skill confirm keeps Tab and Shift+Tab inside itself, wrapping at both ends, with Cancel still first', async () => {
  await mount({t2Ready: true});
  const form = skill('brand-lens');
  typeInto(field(form, 'brand'), 'Fixture Cola');
  submit(form.querySelector('form'));
  expect(document.activeElement.textContent).toBe('Cancel');
  const tab = (shiftKey = false) => {
    const event = new KeyboardEvent('keydown', {key: 'Tab', shiftKey, bubbles: true, cancelable: true});
    flushSync(() => document.activeElement.dispatchEvent(event));
    return event.defaultPrevented;
  };
  expect(tab(true)).toBe(true);
  expect(document.activeElement).toBe(button(dialog(), 'Ask'));
  expect(tab()).toBe(true);
  expect(document.activeElement).toBe(button(dialog(), 'Cancel'));
  expect(tab()).toBe(false);
  flushSync(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
  expect(dialog()).toBeNull();
  expect(asks()).toHaveLength(0);
});

test('closing the skill confirm puts focus back on the button that opened it', async () => {
  await mount({t2Ready: true});
  const form = skill('brand-lens');
  typeInto(field(form, 'brand'), 'Fixture Cola');
  const opener = button(form, 'Read the brand');
  opener.focus();
  submit(form.querySelector('form'));
  expect(document.activeElement.textContent).toBe('Cancel');
  click(button(dialog(), 'Cancel'));
  expect(dialog()).toBeNull();
  expect(document.activeElement).toBe(opener);
  opener.focus();
  submit(form.querySelector('form'));
  flushSync(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
  expect(dialog()).toBeNull();
  expect(document.activeElement).toBe(opener);
  expect(asks()).toHaveLength(0);
});

test('the brand lens posts its skill at T2 and follows the ask', async () => {
  await mount({t2Ready: true});
  const form = skill('brand-lens');
  typeInto(field(form, 'brand'), 'Fixture Cola');
  const competitors = form.querySelectorAll('input[name="competitor"]');
  typeInto(competitors[0], 'Rival Pop');
  typeInto(competitors[2], 'Other Fizz');
  choose(field(form, 'market'), 'NG');
  submit(form.querySelector('form'));
  click(button(dialog(), 'Ask'));
  await settle();
  expect(asks()).toHaveLength(1);
  const body = bodyOf(asks()[0]);
  expect(body.skill).toBe('brand-implication');
  expect(body.tier).toBe('T2');
  expect(body.market).toBe('NG');
  expect(body.question).toContain('Fixture Cola');
  expect(body.question).toContain('Rival Pop');
  expect(body.question).toContain('Other Fizz');
  expect(body.question).toContain('Nigeria');
  expect(window.location.hash).toBe('#/ask?follow=a_20260930_0000skl1');
});

test('a brand lens with no brand is stopped before any call', async () => {
  await mount({t2Ready: true});
  const form = skill('brand-lens');
  submit(form.querySelector('form'));
  expect(dialog()).toBeNull();
  expect(form.querySelector('[role="alert"]').textContent).toBe('Type the brand.');
  expect(asks()).toHaveLength(0);
});

test('a refused ask shows the server words in the confirm', async () => {
  const message = 'Tier T2 is not available until Stage 1B; use T0, T1 or leave it out.';
  await mount({t2Ready: true}, [['/api/ask', reply(400, {error: 'bad_request', message})]]);
  const form = skill('brand-lens');
  typeInto(field(form, 'brand'), 'Fixture Cola');
  submit(form.querySelector('form'));
  click(button(dialog(), 'Ask'));
  await settle();
  expect(dialog().querySelector('[role="alert"]').textContent).toBe(message);
  expect(window.location.hash).toBe('#/ask');
});

/* ---------------- the context pack, once T2 is ready ---------------- */

test('the context pack takes a brief of up to 2,000 characters and a market, and posts it at T2', async () => {
  await mount({t2Ready: true});
  const form = skill('context-pack');
  expect(field(form, 'brief').tagName).toBe('TEXTAREA');
  expect(field(form, 'brief').getAttribute('maxlength')).toBe('2000');
  typeInto(field(form, 'brief'), 'A winter campaign for a warm drink in Nairobi, told through matatu culture.');
  choose(field(form, 'market'), 'KE');
  submit(form.querySelector('form'));
  expect(text(dialog())).toContain('up to 300 credits');
  click(button(dialog(), 'Ask'));
  await settle();
  expect(bodyOf(asks()[0])).toEqual({
    question: 'A winter campaign for a warm drink in Nairobi, told through matatu culture.', market: 'KE', tier: 'T2', skill: 'context-pack',
  });
  expect(window.location.hash).toBe('#/ask?follow=a_20260930_0000skl1');
});

test('a context pack brief under 3 characters is stopped before any call', async () => {
  await mount({t2Ready: true});
  const form = skill('context-pack');
  typeInto(field(form, 'brief'), 'ab');
  submit(form.querySelector('form'));
  expect(form.querySelector('[role="alert"]').textContent).toBe('Type a brief of 3 to 2,000 characters.');
  expect(asks()).toHaveLength(0);
});

/* ---------------- creator fit ---------------- */

test('creator fit reads the creator page first and offers the form only when 42 may show that creator', async () => {
  await mount({fit: 'c_ng_macro', market: 'NG'}, [['/api/creators/', reply(200, fixture.creator)], ['/api/ask', reply(202, STARTED)]]);
  expect(calls[0].url).toBe('/api/creators/c_ng_macro?market=NG');
  const form = skill('creator-fit');
  expect(form).not.toBeNull();
  expect(text(form)).toContain('@fixture_ng_macro');
  expect(field(form, 'brand')).not.toBeNull();
});

test('creator fit asks for a confirm naming up to 60 credits and posts creator-read at T1', async () => {
  await mount({fit: 'c_ng_macro', market: 'NG'}, [['/api/creators/', reply(200, fixture.creator)], ['/api/ask', reply(202, STARTED)]]);
  const form = skill('creator-fit');
  typeInto(field(form, 'brand'), 'Fixture Cola');
  submit(form.querySelector('form'));
  expect(text(dialog())).toContain('up to 60 credits');
  expect(document.activeElement && document.activeElement.textContent).toBe('Cancel');
  click(button(dialog(), 'Ask'));
  await settle();
  const body = bodyOf(asks()[0]);
  expect(body.skill).toBe('creator-read');
  expect(body.tier).toBe('T1');
  expect(body.market).toBe('NG');
  expect(body.question).toContain('@fixture_ng_macro');
  expect(body.question).toContain('Fixture Cola');
  expect(window.location.hash).toBe('#/ask?follow=a_20260930_0000skl1');
});

test('creator fit is not offered for a creator 42 may not show', async () => {
  await mount({fit: 'c_ng_micro', market: 'NG'}, [['/api/creators/', reply(404, {error: 'not_found', message: 'This creator is not shown.'})]]);
  expect(skill('creator-fit')).toBeNull();
  expect(text()).toContain('Creator fit is offered only from a creator page 42 can show.');
  expect(asks()).toHaveLength(0);
});

test('an allowed creator page links to creator fit', async () => {
  serve([['/api/creators/', reply(200, fixture.creator)]]);
  flushSync(() => root.render(<CreatorPage42 creatorId="c_ng_macro" market="NG" />));
  await settle();
  const link = [...host.querySelectorAll('a')].find((a) => a.textContent.trim() === 'Creator fit');
  expect(link.getAttribute('href')).toBe('#/ask?fit=c_ng_macro&market=NG');
});

test('a creator page that is not shown offers no creator fit', async () => {
  serve([['/api/creators/', reply(404, {error: 'not_found', message: 'This creator is not shown.'})]]);
  flushSync(() => root.render(<CreatorPage42 creatorId="c_ng_micro" market="NG" />));
  await settle();
  expect([...host.querySelectorAll('a')].some((a) => a.textContent.trim() === 'Creator fit')).toBe(false);
});

test('the skill forms name no age group and no Google Trends', async () => {
  await mount({t2Ready: true, fit: 'c_ng_macro', market: 'NG'}, [['/api/creators/', reply(200, fixture.creator)]]);
  expect(text()).not.toMatch(/gen ?z|millennial|youth|generation|google trends/i);
});
