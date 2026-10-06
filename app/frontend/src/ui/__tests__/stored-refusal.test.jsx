/* A question refused because its dates fall outside the covered window reads
   the same when it is reopened from its address as it did live: the stored
   view reads the covered dates, names them, and offers the latest available
   week as a fresh question, since a refused reply has no answer to follow up. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, mock, test} from 'bun:test';
import React from 'react';
import {intelligenceFixture} from './fixtures/general-intelligence.js';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');

const realApi = {...(await import('../../api.js'))};

const A = '00000000-0000-4000-8000-000000000001';
const COVERAGE = {contract_version: 'general_question_coverage_v1', state: 'covered', window: {start: '2026-08-25', end: '2026-09-07'}, cutoff_date: '2026-09-07'};

function refused(){
  const intelligence = intelligenceFixture();
  Object.assign(intelligence, {status: 'unavailable', as_of: '2026-09-23T10:00:00Z', window: {start: '2026-09-14', end: '2026-09-20', closed: true}, snapshot_id: null, sections: [], claims: [], receipts: [], readings: [], limitations: ['No completed answer is available.'], missing_work: ['retrieval_incomplete', 'coverage_incomplete'], review_required: false, ready_for_downstream: false});
  return {contract_version: 'general_question_detail_v1', request_id: A, observed_state: 'unavailable', question: 'What happened between 14 and 20 September 2026?', history: [], selected_market: 'za', requested_window: null, response: {answer: 'I could not produce a supported answer for this request.', sources: [], error: true, reason: 'retrieval_incomplete', intelligence}, window_from_plan: true, reserved_microusd: 100000, missing_work: []};
}

const coverageRead = {state: 'ready', data: COVERAGE};
const coverageRetries = [];

mock.module('../../api.js', () => ({
  ...realApi,
  useApi: (path) => [path === '/api/chat/coverage' ? {...coverageRead} : {state: 'loading'}, () => { if (path === '/api/chat/coverage') coverageRetries.push(path); }],
  apiGetFresh: () => Promise.resolve(refused()),
}));

const {ChatPage} = await import('../../chat.jsx');

afterAll(() => {
  mock.module('../../api.js', () => realApi);
  GlobalRegistrator.unregister();
});

let host = null;
let root = null;

beforeEach(() => {
  Object.assign(coverageRead, {state: 'ready', data: COVERAGE});
  delete coverageRead.message;
  coverageRetries.length = 0;
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

test('a stored refusal outside the covered dates names them and offers the latest available week as a new question', async () => {
  const asked = [];
  flushSync(() => { root.render(<ChatPage requestId={A} session="test" onAuth={() => {}} embedded onNewQuestion={(market, question) => asked.push([market, question])} />); });
  await settle();
  const shown = host.textContent;
  expect(shown).toContain('This question falls outside the dates 42 can answer. No answer was generated.');
  expect(shown).toContain('Ask covers 25 Aug to 7 Sept 2026.');
  expect(shown).not.toContain('The evidence retrieval did not complete');
  expect(shown).not.toContain('could not be completed from the admitted evidence');
  const way = [...host.querySelectorAll('button')].find((button) => button.textContent === 'Ask about the latest available week');
  expect(way).toBeTruthy();
  flushSync(() => way.click());
  expect(asked).toEqual([['ZA', 'What stood out in South Africa in the latest available week?']]);
});

/* When the covered dates cannot be read, the page cannot tell whether the
   refusal is about dates, so it says the dates could not be read and offers to
   read them again, never the outage line or the engine's retrieval wording. */
test('a stored refusal whose covered dates could not be read says so and offers to read them again', async () => {
  Object.assign(coverageRead, {state: 'error', data: undefined, message: 'Service unavailable'});
  flushSync(() => { root.render(<ChatPage requestId={A} session="test" onAuth={() => {}} embedded onNewQuestion={() => {}} />); });
  await settle();
  const shown = host.textContent;
  expect(shown).toContain('The dates 42 can answer could not be read, so this page cannot say whether the question falls outside them. No answer was generated.');
  expect(shown).not.toContain('could not be completed from the admitted evidence');
  expect(shown).not.toContain('The evidence retrieval did not complete');
  expect(shown).not.toContain('Ask about the latest available week');
  const retry = [...host.querySelectorAll('button')].find((button) => button.textContent === 'Read the covered dates again');
  expect(retry).toBeTruthy();
  flushSync(() => retry.click());
  expect(coverageRetries).toEqual(['/api/chat/coverage']);
});

/* A re-read that is still loading after a failed one keeps the last settled
   answer on screen, so the note does not flash to the outage line and back. */
test('the unread dates note stays while the covered dates are read again', async () => {
  Object.assign(coverageRead, {state: 'error', data: undefined, message: 'Service unavailable'});
  const render = () => flushSync(() => { root.render(<ChatPage requestId={A} session="test" onAuth={() => {}} embedded onNewQuestion={() => {}} />); });
  render();
  await settle();
  Object.assign(coverageRead, {state: 'loading', data: undefined});
  render();
  await settle();
  const shown = host.textContent;
  expect(shown).toContain('The dates 42 can answer could not be read, so this page cannot say whether the question falls outside them. No answer was generated.');
  expect(shown).not.toContain('could not be completed from the admitted evidence');
  expect(shown).not.toContain('The evidence retrieval did not complete');
});
