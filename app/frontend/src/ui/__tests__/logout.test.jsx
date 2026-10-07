/* Log out (Albert, 6 October 2026: "a logout function to log out easily and
   safely"). The passcode is the only credential the browser holds and the
   live API keeps no session for it, so logging out forgets the stored
   passcode, the saved answers and every cached read, and returns to the
   gate. Theme and market stay: they are preferences, not access. The
   control sits at the foot of the wide rail and in the phone More sheet,
   and only where a passcode is what lets the reader in. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {PASS_KEY, clearCache} = await import('../../api.js');
const {default: App} = await import('../../App.jsx');
const {Rail42} = await import('../../rail42.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

function health(authMode, passcode){
  const body = {ok: true, service: 'f42-api', version: 'fixture', time: '2026-10-06T10:00:00+02:00', checks: {auth: 'ok'}, passcode};
  if (authMode !== undefined) body.auth_mode = authMode;
  return reply(200, body);
}

function serve(healthResponse){
  globalThis.fetch = async (url, init = {}) => {
    calls.push({url: String(url), init});
    if (String(url) === '/api/health') return healthResponse;
    if (String(url) === '/api/auth/verify') return reply(200, {ok: true});
    if (String(url).startsWith('/api/coverage')) return reply(200, {});
    return reply(404, {error: 'not_found'});
  };
}

function mountApp(){
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  flushSync(() => root.render(<App />));
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitUntil(check, timeout = 2000){
  const deadline = Date.now() + timeout;
  while (!check() && Date.now() < deadline) await pause(10);
}
const passcodeForm = () => host.querySelector('input[type="password"]');
const logOut = () => [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Log out') || null;
const moreButton = () => [...host.querySelectorAll('button')].find((b) => b.textContent.trim().startsWith('More'));
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));

beforeEach(() => {
  calls = [];
  window.happyDOM.setViewport({width: 1280, height: 900});
  localStorage.clear();
  window.location.hash = '#/coverage';
  clearCache();
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  localStorage.clear();
  globalThis.fetch = realFetch;
  clearCache();
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

test('Log out forgets the passcode and saved answers, keeps preferences, and returns to the gate', async () => {
  localStorage.setItem(PASS_KEY, 'synthetic-fixture-passcode');
  localStorage.setItem('pulse-briefs-ver', '2');
  localStorage.setItem('pulse-briefs', JSON.stringify([{id: 'fixture-brief', ts: 1}]));
  localStorage.setItem('pulse-chat', JSON.stringify([{q: 'fixture question'}]));
  localStorage.setItem('oi-theme', 'midnight');
  localStorage.setItem('pulse-region', 'NG');
  serve(health('passcode', true));
  mountApp();
  await waitUntil(() => Boolean(logOut()));

  const button = logOut();
  expect(button).not.toBeNull();
  expect(button.getAttribute('type')).toBe('button');
  expect(button.closest('nav[aria-label="Main"]')).not.toBeNull();
  expect(Boolean(passcodeForm())).toBe(false);

  click(button);
  await waitUntil(() => Boolean(passcodeForm()));

  expect(passcodeForm()).not.toBeNull();
  expect(host.querySelector('#gate-status').textContent).toContain('Protected Ogilvy team access.');
  expect(localStorage.getItem(PASS_KEY)).toBeNull();
  expect(localStorage.getItem('pulse-briefs')).toBeNull();
  expect(localStorage.getItem('pulse-chat')).toBeNull();
  expect(localStorage.getItem('oi-theme')).toBe('midnight');
  expect(localStorage.getItem('pulse-region')).toBe('NG');

  /* Nothing protected is read on the way out, and nothing after it carries
     the forgotten passcode. */
  const after = calls.length;
  await pause(50);
  expect(calls.slice(after).some((call) => call.init.headers?.['X-Passcode'] === 'synthetic-fixture-passcode')).toBe(false);
});

/* The older chat page saves its threads as it unmounts, which is after the
   click removed them (review of 067a2cb2). A write that lands between the
   click and the gate is removed again once the gate is up. */
test('answers a page saves while it closes are removed once the gate is up', async () => {
  localStorage.setItem(PASS_KEY, 'synthetic-fixture-passcode');
  serve(health('passcode', true));
  mountApp();
  await waitUntil(() => Boolean(logOut()));

  const late = () => localStorage.setItem('pulse-chat', JSON.stringify([{q: 'saved while closing'}]));
  document.addEventListener('click', late);
  try {
    click(logOut());
  } finally {
    document.removeEventListener('click', late);
  }
  await waitUntil(() => Boolean(passcodeForm()));
  await pause(20);

  expect(passcodeForm()).not.toBeNull();
  expect(localStorage.getItem('pulse-chat')).toBeNull();
  expect(localStorage.getItem(PASS_KEY)).toBeNull();
});

test('signing back in after Log out opens the desk again with the new passcode', async () => {
  localStorage.setItem(PASS_KEY, 'synthetic-fixture-passcode');
  serve(health('passcode', true));
  mountApp();
  await waitUntil(() => Boolean(logOut()));
  click(logOut());
  await waitUntil(() => Boolean(passcodeForm()));

  const input = passcodeForm();
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'synthetic-second-passcode');
  flushSync(() => input.dispatchEvent(new Event('input', {bubbles: true})));
  flushSync(() => input.closest('form').dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  await waitUntil(() => !passcodeForm() && Boolean(logOut()));

  expect(localStorage.getItem(PASS_KEY)).toBe('synthetic-second-passcode');
  expect(Boolean(passcodeForm())).toBe(false);
  expect(logOut()).not.toBeNull();
});

test('a Log out in another tab closes this tab too', async () => {
  localStorage.setItem(PASS_KEY, 'synthetic-fixture-passcode');
  serve(health('passcode', true));
  mountApp();
  await waitUntil(() => Boolean(logOut()));

  localStorage.setItem('pulse-chat', '[]');
  localStorage.removeItem(PASS_KEY);
  /* The storage listener registers in an effect after the button renders, so
     under load the first event can come before it. The other tab's event is
     sent again on each wait until the gate shows, within the same deadline,
     and the stored chat is cleared by an effect once the gate has rendered. */
  const otherTabLogsOut = () => window.dispatchEvent(new StorageEvent('storage', {key: PASS_KEY, oldValue: 'synthetic-fixture-passcode', newValue: null}));
  await waitUntil(() => {
    if (!passcodeForm()){ otherTabLogsOut(); return false; }
    return localStorage.getItem('pulse-chat') === null;
  });

  expect(passcodeForm()).not.toBeNull();
  expect(localStorage.getItem('pulse-chat')).toBeNull();
});

test('IAP read-only access has no Log out, since no passcode let the reader in', async () => {
  serve(health('iap_readonly', false));
  mountApp();
  await waitUntil(() => Boolean(moreButton()));
  await pause(20);

  expect(host.textContent).toContain('Read-only access');
  expect(logOut()).toBeNull();
});

test('a desk with no passcode gate has no Log out', async () => {
  serve(health(undefined, false));
  mountApp();
  await waitUntil(() => Boolean(moreButton()));
  await pause(20);

  expect(logOut()).toBeNull();
});

test('at phone width Log out sits in the More sheet after the theme', () => {
  window.happyDOM.setViewport({width: 390, height: 844});
  let signedOut = 0;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  flushSync(() => root.render(
    <Rail42 only="compact" route="pulse" theme="daylight" onThemeChange={() => {}} onSignOut={() => { signedOut += 1; }} />,
  ));

  const sheet = host.querySelector('.rail42-sheet');
  expect(sheet.hidden).toBe(true);
  click(moreButton());
  expect(sheet.hidden).toBe(false);
  const button = logOut();
  expect(sheet.contains(button)).toBe(true);
  const order = [...sheet.querySelectorAll('select, button')];
  expect(order.indexOf(button)).toBeGreaterThan(order.indexOf(sheet.querySelector('select')));
  click(button);
  expect(signedOut).toBe(1);
});

test('the rail shows no Log out when the host gives it nothing to call', () => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  flushSync(() => root.render(<Rail42 only="wide" route="pulse" theme="daylight" onThemeChange={() => {}} />));

  expect(logOut()).toBeNull();
});
