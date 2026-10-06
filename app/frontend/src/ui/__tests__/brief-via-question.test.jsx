/* Build cited brief on staging. The brief writer is closed there by contract,
   so the page asks 42's reviewed question engine for the brief through Ask's
   own start path: one question built from the reader's topic, market and
   framing, sent with Ask's transport, and the reader lands on that request,
   where the cited answer and its HTML and PDF download are the brief. Where
   the writer runs, the old flow is unchanged. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, mock, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const realApi = {...(await import('../../api.js'))};
const {intelligenceFixture} = await import('./fixtures/general-intelligence.js');
const {validateIntelligenceReply} = await import('../../generalIntelligence.js');
const {POLICY_REVIEW_LAPSED, SERVICE_TEMPORARY} = await import('../../chatTransport.js');

const OFF = 'Cited briefs cannot be written on staging. Staging lets only Ask use the model, so the brief writer is switched off here. Ask your question in Ask to get a cited answer you can download as HTML or PDF.';
const COVERAGE = {contract_version: 'general_question_coverage_v1', state: 'covered', window: {start: '2026-08-25', end: '2026-09-07'}, cutoff_date: '2026-09-07'};
const reads = {availability: null, send: null, status: null};
const posted = [];
const polled = [];

mock.module('../../api.js', () => ({
  ...realApi,
  apiGet: (path) => {
    if (path === '/api/research/availability') return reads.availability();
    if (path === '/api/chat/coverage') return Promise.resolve(COVERAGE);
    if (path === '/api/research/personas') return Promise.resolve({personas: [{id: 'audience_neutral', label: 'Audience neutral', markets: ['za'], query_groups: []}]});
    return new Promise(() => {});
  },
  apiGetFresh: (path) => {
    if (String(path).startsWith('/api/chat/status')){ polled.push(path); return reads.status(); }
    /* The scan keeps one read in flight across mounts, so it must settle
       here rather than hang into the next test file. */
    if (String(path).startsWith('/api/research/behaviours')) return Promise.reject(Object.assign(new Error('Service unavailable right now.'), {status: 503}));
    return new Promise(() => {});
  },
  apiPost: (path, body) => {
    posted.push([path, body]);
    if (path === '/api/chat/send') return reads.send(body);
    return new Promise(() => {});
  },
}));

const {ResearchPage} = await import('../../ResearchDocPanel.jsx');
const {briefQuestion, BRIEF_TOPIC_MAX, BRIEF_FRAMING_MAX} = await import('../../briefViaQuestion.jsx');

afterAll(() => {
  mock.module('../../api.js', () => realApi);
  GlobalRegistrator.unregister();
});

let host = null;
let root = null;

beforeEach(() => {
  posted.length = 0;
  polled.length = 0;
  window.location.hash = '#/console?work=brief';
  host = document.createElement('div');
  document.body.appendChild(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  host.remove();
});

const settle = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setTimeout(resolve, 0)); };

async function mount(props = {}){
  root = createRoot(host);
  flushSync(() => root.render(<ResearchPage embedded onAuth={() => {}} {...props} />));
  await settle();
}

const staging = () => Promise.resolve({
  brief_writing: {available: false, code: 'brief_writing_unavailable', message: OFF},
  behaviour_scan: {available: false, code: 'behaviour_scan_off', message: 'off'},
  brief_via_question: true,
});

function type(node, value){
  const proto = node.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : node.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, value);
  flushSync(() => node.dispatchEvent(new window.Event(node.tagName === 'SELECT' ? 'change' : 'input', {bubbles: true})));
}

const field = (label) => [...host.querySelectorAll('label')].find((node) => node.textContent.startsWith(label)).control
  || host.querySelector('#' + [...host.querySelectorAll('label')].find((node) => node.textContent.startsWith(label)).htmlFor);
const action = () => [...host.querySelectorAll('button')].find((node) => node.textContent === 'Ask 42 for a cited brief');

async function ask(){
  type(field('Topic'), 'Mobile data prices');
  type(field('Market'), 'ng');
  type(field('Framing'), 'a telecoms brand');
  flushSync(() => action().click());
  await settle();
}

test('staging offers one plain action that asks the question engine and opens the stored request', async () => {
  reads.availability = staging;
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => Promise.resolve({answer: 'Stored answer', sources: [], intelligence: intelligenceFixture()});
  await mount();
  expect(host.textContent).toContain("42's reviewed question engine");
  expect(host.textContent).toContain('25 Aug to 7 Sept 2026');
  expect(host.textContent).not.toContain('brief_writing_unavailable');
  expect(host.textContent).not.toContain('See the behaviours first');
  expect(action()).toBeTruthy();
  await ask();
  const sends = posted.filter(([path]) => path === '/api/chat/send');
  expect(sends).toHaveLength(1);
  const body = sends[0][1];
  expect(body.message).toBe('Write a cited brief on Mobile data prices in Nigeria. Framing: a telecoms brand.');
  expect(body.history).toEqual([]);
  expect(body.market).toBe('ng');
  expect(body.idempotency_key).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$/);
  expect(Object.keys(body).sort()).toEqual(['history', 'idempotency_key', 'market', 'message']);
  expect(polled[0]).toContain('job_id=job_brief');
  expect(posted.some(([path]) => path.startsWith('/api/research/'))).toBe(false);
  expect(window.location.hash).toBe('#/console?work=ask&request=' + intelligenceFixture().request_id);
});

test('the page shows the exact question before it is sent', async () => {
  reads.availability = staging;
  reads.send = () => new Promise(() => {});
  await mount();
  type(field('Topic'), 'Mobile data prices');
  expect(host.textContent).toContain('Write a cited brief on Mobile data prices in South Africa.');
  expect(action().disabled).toBe(false);
  type(field('Topic'), '   ');
  expect(action().disabled).toBe(true);
});

test('the question is bounded and carries only the reader\'s topic, market and framing', () => {
  expect(briefQuestion({topic: 'Mobile data', market: 'za', framing: ''})).toBe('Write a cited brief on Mobile data in South Africa.');
  expect(briefQuestion({topic: 'Mobile data', market: 'all', framing: ''})).toBe('Write a cited brief on Mobile data across South Africa, Nigeria and Kenya.');
  expect(briefQuestion({topic: '  Mobile\n\tdata\u0000 prices. ', market: 'ke', framing: ' banks\n'})).toBe('Write a cited brief on Mobile data prices in Kenya. Framing: banks.');
  expect(briefQuestion({topic: '', market: 'za', framing: 'banks'})).toBe('');
  expect(briefQuestion({topic: 'x', market: 'zz', framing: ''})).toBe('');
  const long = briefQuestion({topic: 't'.repeat(BRIEF_TOPIC_MAX + 50), market: 'za', framing: 'f'.repeat(BRIEF_FRAMING_MAX + 50)});
  expect(long).toBe('Write a cited brief on ' + 't'.repeat(BRIEF_TOPIC_MAX) + ' in South Africa. Framing: ' + 'f'.repeat(BRIEF_FRAMING_MAX) + '.');
  expect(BRIEF_TOPIC_MAX + BRIEF_FRAMING_MAX).toBeLessThanOrEqual(400);
});

test('a lapsed pricing review shows Ask\'s plain words, not the code', async () => {
  reads.availability = staging;
  reads.send = () => Promise.reject(Object.assign(new Error('raw'), {status: 503, code: 'policy_review_lapsed'}));
  await mount();
  await ask();
  expect(host.textContent).toContain(POLICY_REVIEW_LAPSED);
  expect(host.textContent).not.toContain('policy_review_lapsed');
  expect(window.location.hash).toBe('#/console?work=brief');
});

test('a service that cannot start the question shows Ask\'s plain words and resends as the same request', async () => {
  reads.availability = staging;
  reads.send = () => Promise.reject(Object.assign(new Error('Service unavailable right now.'), {status: 503}));
  await mount();
  await ask();
  expect(host.textContent).toContain(SERVICE_TEMPORARY);
  expect(host.textContent).not.toContain('submission_uncertain');
  const retry = [...host.querySelectorAll('button')].find((node) => node.textContent === 'Retry');
  flushSync(() => retry.click());
  await settle();
  const sends = posted.filter(([path]) => path === '/api/chat/send');
  expect(sends).toHaveLength(2);
  expect(sends[1][1]).toEqual(sends[0][1]);
});

const button = (label) => [...host.querySelectorAll('button')].find((node) => node.textContent === label);

test('the fields bound what a reader can type at the question\'s own limits', async () => {
  reads.availability = staging;
  await mount();
  expect(field('Topic').maxLength).toBe(BRIEF_TOPIC_MAX);
  expect(field('Framing').maxLength).toBe(BRIEF_FRAMING_MAX);
});

test('while a retry is on offer the question is locked and cannot be started again under a new key', async () => {
  reads.availability = staging;
  reads.send = () => Promise.reject(Object.assign(new Error('Service unavailable right now.'), {status: 503}));
  await mount();
  await ask();
  expect(button('Retry')).toBeTruthy();
  expect(field('Topic').disabled).toBe(true);
  expect(field('Market').disabled).toBe(true);
  expect(field('Framing').disabled).toBe(true);
  expect(!action() || action().disabled).toBe(true);
  expect(button('Start over')).toBeUndefined();
  expect(host.textContent).toContain('Write a cited brief on Mobile data prices in Nigeria. Framing: a telecoms brand.');
  const sent = posted.filter(([path]) => path === '/api/chat/send')[0][1];
  expect(sent.message).toBe('Write a cited brief on Mobile data prices in Nigeria. Framing: a telecoms brand.');
});

test('while an unconfirmed reply can be checked again the question stays locked', async () => {
  reads.availability = staging;
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => Promise.reject(Object.assign(new Error('forbidden'), {status: 403}));
  await mount();
  await ask();
  expect(button('Check again')).toBeTruthy();
  expect(field('Topic').disabled).toBe(true);
  expect(!action() || action().disabled).toBe(true);
  expect(button('Start over')).toBeUndefined();
});

/* The status route answers an unknown or foreign job with 503, never 404, so
   a 404 is version skew or a proxy and the job may still be running. */
function stillChecking(){
  expect(button('Check again')).toBeTruthy();
  expect(button('Retry')).toBeUndefined();
  expect(button('Start over')).toBeUndefined();
  expect(field('Topic').disabled).toBe(true);
  expect(field('Market').disabled).toBe(true);
  expect(field('Framing').disabled).toBe(true);
  expect(!action() || action().disabled).toBe(true);
  expect(host.textContent).not.toContain('reply_uncertain');
  expect(posted.filter(([path]) => path === '/api/chat/send')).toHaveLength(1);
}

test('a 404 on Check again still offers Check again, keeps the fields locked and does not resend', async () => {
  reads.availability = staging;
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => Promise.reject(Object.assign(new Error('forbidden'), {status: 403}));
  await mount();
  await ask();
  reads.status = () => Promise.reject(Object.assign(new Error('not found'), {status: 404}));
  flushSync(() => button('Check again').click());
  await settle();
  stillChecking();
  expect(polled.length).toBe(2);
});

test('a 404 on the first poll after Ask still offers Check again, keeps the fields locked and does not resend', async () => {
  reads.availability = staging;
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => Promise.reject(Object.assign(new Error('not found'), {status: 404}));
  await mount();
  await ask();
  stillChecking();
});

/* A focusable control outside the page, so a test can tell focus moved. */
function elsewhere(){
  let node = document.getElementById('brief-test-elsewhere');
  if (!node){ node = document.createElement('button'); node.id = 'brief-test-elsewhere'; document.body.appendChild(node); }
  return node;
}

test('Retry and Check again move focus to the status line', async () => {
  reads.availability = staging;
  reads.send = () => Promise.reject(Object.assign(new Error('Service unavailable right now.'), {status: 503}));
  await mount();
  await ask();
  elsewhere().focus();
  expect(document.activeElement).toBe(elsewhere());
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => new Promise(() => {});
  flushSync(() => button('Retry').click());
  await settle();
  const status = host.querySelector('[role="status"]');
  expect(document.activeElement).toBe(status);
  flushSync(() => root.unmount());
  root = null;
  reads.status = () => Promise.reject(Object.assign(new Error('forbidden'), {status: 403}));
  await mount();
  await ask();
  elsewhere().focus();
  expect(document.activeElement).toBe(elsewhere());
  reads.status = () => new Promise(() => {});
  flushSync(() => button('Check again').click());
  await settle();
  expect(document.activeElement).toBe(host.querySelector('[role="status"]'));
});

test('pressing Ask moves focus to the status line, and Start over moves it to Topic', async () => {
  const refused = {...intelligenceFixture(), status: 'refused', sections: [], ready_for_downstream: false, missing_work: ['coverage_incomplete']};
  reads.availability = staging;
  reads.send = () => new Promise(() => {});
  await mount();
  type(field('Topic'), 'Mobile data prices');
  flushSync(() => action().click());
  await settle();
  const status = host.querySelector('[role="status"]');
  expect(document.activeElement).toBe(status);
  expect(status.getAttribute('tabindex')).toBe('-1');
  flushSync(() => root.unmount());
  root = null;
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => Promise.resolve({answer: '', sources: [], intelligence: refused});
  await mount();
  await ask();
  flushSync(() => button('Start over').click());
  await settle();
  expect(document.activeElement).toBe(field('Topic'));
});

test('a settled refusal offers Start over, which unlocks the question for a new request', async () => {
  const refused = {...intelligenceFixture(), status: 'refused', sections: [], ready_for_downstream: false, missing_work: ['coverage_incomplete']};
  reads.availability = staging;
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => Promise.resolve({answer: '', sources: [], intelligence: refused});
  await mount();
  await ask();
  expect(field('Topic').disabled).toBe(true);
  expect(!action() || action().disabled).toBe(true);
  flushSync(() => button('Start over').click());
  expect(field('Topic').disabled).toBe(false);
  expect(field('Topic').value).toBe('Mobile data prices');
  expect(action().disabled).toBe(false);
  expect(host.textContent).not.toContain('The available evidence does not cover');
});

test('a start the service refused outright offers Start over and no Retry', async () => {
  reads.availability = staging;
  reads.send = () => Promise.reject(Object.assign(new Error('raw'), {status: 503, code: 'policy_review_lapsed'}));
  await mount();
  await ask();
  expect(button('Retry')).toBeUndefined();
  expect(button('Start over')).toBeTruthy();
});

test('every action on the page takes the 48px action style', async () => {
  const refused = {...intelligenceFixture(), status: 'refused', sections: [], ready_for_downstream: false, missing_work: ['coverage_incomplete']};
  reads.availability = staging;
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => Promise.resolve({answer: '', sources: [], intelligence: refused});
  await mount();
  expect(action().classList.contains('legacy-action')).toBe(true);
  await ask();
  const open = [...host.querySelectorAll('a')].find((node) => node.textContent === 'Open the request');
  expect(open.classList.contains('legacy-action')).toBe(true);
  expect(button('Start over').classList.contains('legacy-action')).toBe(true);
});

test('a question outside the covered window shows Ask\'s refusal words and links the stored request', async () => {
  const refused = {...intelligenceFixture(), status: 'refused', sections: [], ready_for_downstream: false, missing_work: ['coverage_incomplete']};
  expect(validateIntelligenceReply(refused).ok).toBe(true);
  reads.availability = staging;
  reads.send = () => Promise.resolve({job_id: 'job_brief'});
  reads.status = () => Promise.resolve({answer: '', sources: [], intelligence: refused});
  await mount();
  await ask();
  expect(host.textContent).toContain('The available evidence does not cover the requested dates or scope.');
  expect(host.textContent).not.toContain('coverage_incomplete');
  expect(window.location.hash).toBe('#/console?work=brief');
  const open = [...host.querySelectorAll('a')].find((node) => node.textContent === 'Open the request');
  expect(open.getAttribute('href')).toBe('#/console?work=ask&request=' + refused.request_id);
});

test('where briefs can be written the old flow still renders', async () => {
  reads.availability = () => Promise.resolve({
    brief_writing: {available: true, code: null, message: null},
    behaviour_scan: {available: true, code: null, message: null},
    brief_via_question: false,
  });
  await mount();
  expect(host.textContent).toContain('See the behaviours first');
  expect(action()).toBeUndefined();
});

test('a closed writer without the question route keeps the plain notice', async () => {
  reads.availability = () => Promise.resolve({
    brief_writing: {available: false, code: 'brief_writing_unavailable', message: OFF},
    behaviour_scan: {available: false, code: 'behaviour_scan_off', message: 'off'},
  });
  await mount({onBuildAsk: () => {}});
  expect(host.textContent).toContain('Cited briefs are not available here');
  expect(action()).toBeUndefined();
});
