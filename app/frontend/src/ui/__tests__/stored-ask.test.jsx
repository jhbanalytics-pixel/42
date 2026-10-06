/* Quiet register, 23 Sept 2026: the console lists the saved answer it is
   showing among its recent questions. When the read of a stored request
   fails, the console no longer holds a question it could not read, so the
   stored view clears what it reported rather than leaving the last one in
   the rail. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, mock, test} from 'bun:test';
import React from 'react';
import {intelligenceFixture} from './fixtures/general-intelligence.js';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');

const realApi = {...(await import('../../api.js'))};

const A = '00000000-0000-4000-8000-000000000001';
const B = '00000000-0000-4000-8000-000000000002';

const detail = (id) => ({contract_version: 'general_question_detail_v1', request_id: id, observed_state: 'partial', question: 'Are repair tutorials rising?', history: [], selected_market: 'za', requested_window: null, response: {answer: 'Stored answer', sources: [], intelligence: {...intelligenceFixture(), request_id: id}}, window_from_plan: true, reserved_microusd: 100000, missing_work: []});

mock.module('../../api.js', () => ({
  ...realApi,
  useApi: () => [{state: 'loading'}, () => {}],
  apiGetFresh: (path) => {
    const id = new URL(path, 'https://example.test').searchParams.get('request_id');
    if (id === A) return Promise.resolve(detail(A));
    return Promise.reject(Object.assign(new Error('Not found'), {status: 404, code: 'request_not_found'}));
  },
}));

const {ChatPage} = await import('../../chat.jsx');

afterAll(() => {
  mock.module('../../api.js', () => realApi);
  GlobalRegistrator.unregister();
});

let host = null;
let root = null;

beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
});

const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

test('a stored request that cannot be read clears the saved question the console was listing', async () => {
  const reported = [];
  const onStoredQuestion = (value) => reported.push(value);
  flushSync(() => { root.render(<ChatPage requestId={A} session="test" onAuth={() => {}} embedded onStoredQuestion={onStoredQuestion} />); });
  await settle();
  expect(reported.at(-1)).toMatchObject({requestId: A, question: 'Are repair tutorials rising?'});

  flushSync(() => { root.render(<ChatPage requestId={B} session="test" onAuth={() => {}} embedded onStoredQuestion={onStoredQuestion} />); });
  await settle();
  expect(host.querySelector('[role="alert"]')).toBeTruthy();
  expect(reported.at(-1)).toBeNull();
});
