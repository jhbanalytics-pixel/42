/* A conversation keeps the client lens its first answer was asked under.

   The stored view reads that lens from the stored request, shows it, and
   continues the conversation under it. The live console shows the lens of the
   conversation on screen and refuses, in plain words and without sending
   anything, a follow-up the page would ask under a different lens, because the
   service would refuse to use an answer from another lens as its context. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, mock, test} from 'bun:test';
import React from 'react';
import {intelligenceFixture} from './fixtures/general-intelligence.js';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;

const realApi = {...(await import('../../api.js'))};

const LENS = 'bsa_pulse_lens';
const DIGEST = 'e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf';
const BINDING = {lens_binding_version: 'client_lens_binding_v1', client_lens_id: LENS, configuration_digest: DIGEST};
const ROSTER = {
  contract_version: 'client_lens_roster_v1',
  default_client_lens_id: null,
  client_scope_id: 'bsa_pulse',
  lenses: [{client_lens_id: LENS, label: 'Brand South Africa Pulse', configuration_digest: DIGEST}],
};
const GENERAL_ID = '00000000-0000-4000-8000-00000000000a';
const LENSED_ID = '00000000-0000-4000-8000-00000000000b';

const detail = (id, lens) => ({
  ...(lens ? {client_lens: lens} : {}),
  contract_version: 'general_question_detail_v1', request_id: id, observed_state: 'partial',
  question: 'Are repair tutorials rising?', history: [], selected_market: 'za', requested_window: null,
  response: {answer: 'Stored answer', sources: [], intelligence: {...intelligenceFixture(), request_id: id}},
  window_from_plan: true, reserved_microusd: 100000, missing_work: [],
});

let posts = [];
let rosterReads = [];
mock.module('../../api.js', () => ({
  ...realApi,
  useApi: (path, _key, _auth, enabled) => {
    if (path === '/api/chat/lenses') rosterReads.push(enabled !== false);
    return [path === '/api/chat/lenses' ? (enabled === false ? {state: 'idle'} : {state: 'ready', data: ROSTER}) : {state: 'loading'}, () => {}];
  },
  apiGetFresh: (path) => {
    const id = new URL(path, 'https://example.test').searchParams.get('request_id');
    if (id === GENERAL_ID) return Promise.resolve(detail(GENERAL_ID, null));
    if (id === LENSED_ID) return Promise.resolve(detail(LENSED_ID, BINDING));
    return new Promise(() => {});
  },
  apiPost: (path, body) => { posts.push({path, body}); return new Promise(() => {}); },
}));

const {ChatPage, validateQuestionDetail, chatThreadLensId} = await import('../../chat.jsx');

afterAll(() => {
  mock.module('../../api.js', () => realApi);
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

let host = null;
let root = null;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  posts = [];
  rosterReads = [];
  localStorage.clear();
  window.location.hash = '#/console?work=ask';
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  localStorage.clear();
});

const settle = async () => { await act(async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0)); }); };

async function type(label, text){
  const found = [...host.querySelectorAll('label')].find((node) => node.textContent === label);
  if (!found) throw new Error('no field labelled ' + label);
  const field = document.getElementById(found.htmlFor);
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(field, text);
    field.dispatchEvent(new window.Event('input', {bubbles: true}));
  });
}

const sendButton = () => [...host.querySelectorAll('button')].find((node) => node.getAttribute('aria-label') === 'Send' || node.textContent === 'Send');

test('a stored detail may carry the lens binding of its request, and only a well formed one', () => {
  expect(validateQuestionDetail(detail(LENSED_ID, BINDING), LENSED_ID)).toBe(true);
  expect(validateQuestionDetail(detail(GENERAL_ID, null), GENERAL_ID)).toBe(true);
  for (const lens of [null, {}, LENS, {...BINDING, extra: 'x'}, {...BINDING, client_lens_id: ''}, {...BINDING, configuration_digest: 'E'.repeat(64)}, {...BINDING, lens_binding_version: 'client_lens_binding_v0'}]){
    expect(validateQuestionDetail({...detail(LENSED_ID, null), client_lens: lens}, LENSED_ID)).toBe(false);
  }
});

test('a reopened answer shows the lens its own request was asked under, not the one the page names', async () => {
  window.location.hash = `#/console?work=ask&request=${LENSED_ID}`;
  await act(async () => root.render(<ChatPage requestId={LENSED_ID} region="ZA" session="test" onAuth={() => {}} embedded />));
  await settle();
  const shown = host.querySelector('[data-thread-lens]');
  expect(shown.getAttribute('data-thread-lens')).toBe(LENS);
  expect(shown.textContent).toBe('Client lens: Brand South Africa Pulse');

  await act(async () => root.render(<ChatPage requestId={GENERAL_ID} region="ZA" session="test" onAuth={() => {}} embedded />));
  await settle();
  const general = host.querySelector('[data-thread-lens]');
  expect(general.getAttribute('data-thread-lens')).toBe('general');
  expect(general.textContent).toBe('Client lens: None');
});

test('a follow-up from a reopened answer continues under the lens of that answer', async () => {
  for (const [id, lens, expected] of [[LENSED_ID, BINDING, LENS], [GENERAL_ID, null, '']]){
    const continued = [];
    await act(async () => root.render(<ChatPage key={id} requestId={id} region="ZA" session="test" onAuth={() => {}} embedded onContinueQuestion={(...args) => continued.push(args)} />));
    await settle();
    await type('Ask a follow-up', 'And in Kenya?');
    await act(async () => sendButton().click());
    expect(continued).toHaveLength(1);
    const [threadId, market, question, lensId] = continued[0];
    expect([market, question, lensId]).toEqual(['ZA', 'And in Kenya?', expected]);
    const thread = JSON.parse(localStorage.getItem('pulse-chat')).find((item) => item.id === threadId);
    expect(chatThreadLensId(thread.messages)).toBe(expected);
    expect(lens === null || thread.messages.at(-1).clientLensId === LENS).toBe(true);
  }
  expect(posts).toEqual([]);
});

function savedThread(lensId){
  const intelligence = {...intelligenceFixture(), request_id: lensId ? LENSED_ID : GENERAL_ID};
  return {id: 'thread-1', ts: 1, messages: [
    {role: 'user', content: 'Are repair tutorials rising?'},
    {role: 'assistant', turnId: 'turn-1', content: 'Stored answer', sources: [], intelligence, clientLensId: lensId},
  ]};
}

test('the live console names the lens of the conversation on screen', async () => {
  localStorage.setItem('pulse-chat', JSON.stringify([savedThread(LENS)]));
  window.location.hash = `#/console?work=ask&lens=${LENS}`;
  await act(async () => root.render(<ChatPage region="ZA" session="test" onAuth={() => {}} embedded initialThreadId="thread-1" />));
  await settle();
  const shown = host.querySelector('[data-thread-lens]');
  expect(shown.getAttribute('data-thread-lens')).toBe(LENS);
  expect(shown.textContent).toBe('Client lens: Brand South Africa Pulse');
  expect(host.querySelector('[data-thread-lens-crossing]')).toBeNull();
});

test('a follow-up under the conversation lens is sent with its parent', async () => {
  localStorage.setItem('pulse-chat', JSON.stringify([savedThread(LENS)]));
  window.location.hash = `#/console?work=ask&lens=${LENS}`;
  await act(async () => root.render(<ChatPage region="ZA" session="test" onAuth={() => {}} embedded initialThreadId="thread-1" />));
  await settle();
  await type('Ask a follow-up', 'And in Kenya?');
  await act(async () => sendButton().click());
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({parent_request_id: LENSED_ID, thread_anchor_request_id: LENSED_ID, client_lens_id: LENS});
});

test('a follow-up under another lens is refused in plain words and nothing is sent', async () => {
  for (const [threadLens, pageHash, words] of [
    [LENS, '#/console?work=ask', /asked under the client lens Brand South Africa Pulse/],
    [undefined, `#/console?work=ask&lens=${LENS}`, /asked with no client lens/],
  ]){
    posts = [];
    localStorage.setItem('pulse-chat', JSON.stringify([savedThread(threadLens)]));
    window.location.hash = pageHash;
    await act(async () => root.render(<ChatPage key={pageHash} region="ZA" session="test" onAuth={() => {}} embedded initialThreadId="thread-1" />));
    await settle();
    const refusal = host.querySelector('[data-thread-lens-crossing]');
    expect(refusal).toBeTruthy();
    expect(refusal.getAttribute('role')).toBe('alert');
    expect(refusal.textContent).toMatch(words);
    expect(refusal.textContent).not.toMatch(/_/);
    await type('Ask a follow-up', 'And in Kenya?');
    expect(sendButton().disabled).toBe(true);
    await act(async () => sendButton().click());
    expect(posts).toEqual([]);
  }
});

test('a reopened general 42 answer names no lens and reads no lens roster', async () => {
  await act(async () => root.render(<ChatPage requestId={GENERAL_ID} region="ZA" session="test" onAuth={() => {}} embedded />));
  await settle();
  expect(host.querySelector('[data-thread-lens]').textContent).toBe('Client lens: None');
  expect(rosterReads.length).toBeGreaterThan(0);
  expect(rosterReads.every((enabled) => enabled === false)).toBe(true);
});
