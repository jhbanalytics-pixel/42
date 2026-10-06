/* A stored refusal's rephrase opens the live console with the question filled
   in. The draft is never sent, and the console reports it used on first
   mount, so the workbench can drop it. That a later return to Ask never fills
   the field with it again is proved in 42-journey-proofs.pw.mjs. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, mock, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');

const realApi = {...(await import('../../api.js'))};
const realTransport = {...(await import('../../chatTransport.js'))};
const submitted = [];

mock.module('../../api.js', () => ({
  ...realApi,
  useApi: () => [{state: 'loading'}, () => {}],
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
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  window.location.hash = '#/console';
});

const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const REPHRASE = 'What stood out in South Africa in the latest available week?';

test('an initial draft fills the field, is never sent, and is reported used once', async () => {
  const used = [];
  const render = () => flushSync(() => { root.render(<ChatPage region="ZA" session="test" onAuth={() => {}} embedded initialThreadId={null} initialDraft={REPHRASE} onInitialDraftUsed={() => used.push(true)} />); });
  render();
  await settle();
  expect(host.querySelector('textarea').value).toBe(REPHRASE);
  render();
  await settle();
  expect(used).toHaveLength(1);
  expect(submitted).toEqual([]);
});
