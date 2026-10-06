/* Quiet register, 23 Sept 2026: the questions offered under an empty Ask
   field. Rule 20 asks for suggestions drawn from this week's signals, and
   rule 16 for the reader's words rather than market codes. The console
   already reads the completed run's admitted signals from the desk, so when
   that read carries signals for the market in view, the offered questions are
   built from their names and nothing else; when it carries none, the general
   questions name the market in words. Nothing is invented either way. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, mock, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');

const realApi = {...(await import('../../api.js'))};
const realTransport = {...(await import('../../chatTransport.js'))};

const deskApi = {state: 'loading', data: null};
const submitted = [];

mock.module('../../api.js', () => ({
  ...realApi,
  useApi: (path) => [String(path).startsWith('/api/desk') ? deskApi : {state: 'loading'}, () => {}],
  apiGetFresh: () => new Promise(() => {}),
  apiPost: () => new Promise(() => {}),
}));

mock.module('../../chatTransport.js', () => ({
  ...realTransport,
  submitChat: (...args) => { submitted.push(args); return new Promise(() => {}); },
  pollChat: () => new Promise(() => {}),
}));

const {ChatPage} = await import('../../chat.jsx');

afterAll(() => {
  mock.module('../../api.js', () => realApi);
  mock.module('../../chatTransport.js', () => realTransport);
  GlobalRegistrator.unregister();
});

let host = null;
let root = null;

beforeEach(() => {
  submitted.length = 0;
  try { window.localStorage.clear(); } catch (_error) { /* nothing stored */ }
  window.location.hash = '#/console?work=ask';
  host = document.createElement('div');
  document.body.appendChild(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  window.location.hash = '#/console';
});

function open(region, desk){
  deskApi.state = desk ? 'ready' : 'loading';
  deskApi.data = desk;
  root = createRoot(host);
  flushSync(() => {
    root.render(<ChatPage region={region} session="test" onAuth={() => {}} initialThreadId={null} embedded />);
  });
}

function offered(){
  return [...host.querySelectorAll('.chat-starters')].map((list) => ({
    title: list.querySelector('.chat-starters-title').textContent.trim(),
    items: [...list.querySelectorAll('button')].map((button) => button.textContent.trim()),
  }));
}

const desk = {dynamic_discovery: {status: 'ready', signals: [
  {signal: {signal_name: 'Balcony mix', market: 'za'}},
  {signal: {signal_name: 'Thrift flip', market: 'za'}},
  {signal: {signal_name: 'Lagos night market', market: 'ng'}},
  {signal: {signal_name: 'Night market', market: 'za'}},
  {signal: {signal_name: 'Weekend repair', market: 'za'}},
]}};

test('an empty Ask offers questions built from the run signals for the market in view', () => {
  open('ZA', desk);
  expect(offered()).toEqual([{
    title: 'From the latest signals',
    items: [
      'What is driving Balcony mix?',
      'What is driving Thrift flip?',
      'What is driving Night market?',
      'What should we brief on from the latest available week?',
    ],
  }]);
  expect(host.textContent).not.toContain('What is moving in ZA?');
  expect(host.textContent).not.toContain('Lagos night market');
});

/* Quiet register, 23 Sept 2026: at e9bd14c the general starters sent on one
   click and the signal chips only filled the draft; the new signal starters
   took over the general list's send. A question spends allowance, so both
   lists now fill the question field and focus it, and nothing leaves until
   the reader presses Send. */
function field(){ return host.querySelector('textarea'); }
function sendButton(){ return [...host.querySelectorAll('button')].find((node) => node.getAttribute('aria-label') === 'Send'); }

test('a signal starter fills the question field and focuses it without sending', () => {
  open('ZA', desk);
  const first = [...host.querySelectorAll('.chat-starters button')][0];
  flushSync(() => { first.click(); });
  expect(submitted).toEqual([]);
  expect(field().value).toBe('What is driving Balcony mix?');
  expect(document.activeElement).toBe(field());
  flushSync(() => { sendButton().click(); });
  expect(submitted.length).toBe(1);
  expect(submitted[0][0]).toBe('What is driving Balcony mix?');
});

test('a general starter fills the question field and focuses it without sending', () => {
  open('ZA', {dynamic_discovery: {status: 'ready', signals: []}});
  const first = [...host.querySelectorAll('.chat-starters button')][0];
  flushSync(() => { first.click(); });
  expect(submitted).toEqual([]);
  expect(field().value).toBe('What is moving in South Africa?');
  expect(document.activeElement).toBe(field());
});

test('with no run signals the general questions name the market in words, never its code', () => {
  open('ZA', {dynamic_discovery: {status: 'ready', signals: []}});
  const lists = offered();
  expect(lists).toHaveLength(1);
  expect(lists[0].title).toBe('Try one of these');
  expect(lists[0].items[0]).toBe('What is moving in South Africa?');
  expect(lists[0].items).toContain('What should we brief on from the latest available week?');
  expect(lists[0].items.join(' ')).not.toMatch(/\bZA\b/);
});

test('all markets names the three markets in words', () => {
  open('all', null);
  expect(offered()[0].items[0]).toBe('What is moving across South Africa, Nigeria and Kenya?');
});
