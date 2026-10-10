/* The typed summary state on the pages (C1 v2 sections 2.6, 6.1 and 8.5, tests
   F-01 to F-07, with the v2.1 amendment). The page reads answer_meta and
   trusts only a verified one; anything else says the summary is not
   available and claims no check failed. The words are the export's words:
   answer_meta_states.json holds, for each producer state, the wire value the
   API sends and the Short answer and status lines that
   GET /api/ask/{id}/export printed for the same record, so the page is held
   to the export and not to itself. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import {existsSync, readFileSync} from 'node:fs';
import completeRecord from './fixtures/ask42_complete.json';
import partialRecord from './fixtures/ask42_partial.json';
import states from './fixtures/answer_meta_states.json';
import words from './fixtures/answer_state_words.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const meta = await import('../../answerMeta.js');
const {AskPage} = await import('../../ask42.jsx');
const {InvestigationPage} = await import('../../investigations42.jsx');
const {DossierPage, SharedDossier} = await import('../../dossiers42.jsx');

const CEILING = 4000;
const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
const json = (status, body) => ({ok: status < 400, status, headers: {get: () => null}, json: async () => body, text: async () => JSON.stringify(body)});

let host = null;
let root = null;
let calls = [];

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  window.history.replaceState(null, '', '#/');
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

async function until(check, label){
  const started = Date.now();
  for (;;){
    let ok = false;
    try { ok = Boolean(check()); } catch (_error){ ok = false; }
    if (ok) return;
    if (Date.now() - started > CEILING) throw new Error('timed out waiting for ' + label);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  }
}

/* The record the API sent for a case, built on the fixture the case was made from. */
function recordOf(entry){
  const record = clone(entry.base === 'partial' ? partialRecord : completeRecord);
  record.status = entry.record_status;
  const answer = record.answer;
  answer.status = entry.answer.status;
  answer.short_answer = entry.answer.short_answer;
  answer.gaps = clone(entry.answer.gaps);
  if (entry.answer.claims === 0){
    answer.claims = [];
    answer.evidence = [];
    answer.so_what = [];
    answer.watch_next = [];
  }
  if (entry.answer_meta !== undefined) record.answer_meta = clone(entry.answer_meta);
  return record;
}

/* A fake f42-api that holds records, for the follow route and the live ask. */
function serve(records, options = {}){
  const byId = new Map(records.map((record) => [record.ask_id, record]));
  const order = records.map((record) => record.ask_id);
  const queue = [];
  let waiting = null;
  let closed = false;
  const encoder = new TextEncoder();
  const settle = (result) => { const resolve = waiting; waiting = null; resolve(result); };
  const stream = {
    push(text){
      const chunk = {value: encoder.encode(text), done: false};
      if (waiting) settle(chunk); else queue.push(chunk);
    },
    close(){ closed = true; if (waiting) settle({value: undefined, done: true}); },
    body: {
      getReader(){
        return {
          read(){
            if (queue.length) return Promise.resolve(queue.shift());
            if (closed) return Promise.resolve({value: undefined, done: true});
            return new Promise((resolve) => { waiting = resolve; });
          },
          cancel(){ closed = true; if (waiting) settle({value: undefined, done: true}); return Promise.resolve(); },
          releaseLock(){},
        };
      },
    },
  };
  let next = 0;
  const stub = async (url, init = {}) => {
    const path = String(url);
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({path, method, body: init.body ? JSON.parse(init.body) : null});
    if (method === 'POST' && path === '/api/ask'){
      const id = order[Math.min(next, order.length - 1)];
      next += 1;
      return json(202, {ask_id: id, status: 'running', events_url: `/api/ask/${id}/events`, url: `/api/ask/${id}`});
    }
    const match = /^\/api\/ask\/([^/?]+)(\/[a-z]+)?(\?.*)?$/.exec(path);
    if (!match || !byId.has(match[1])) return json(404, {error: 'not_found', message: 'No such question.'});
    if (match[2] === '/events'){
      if (options.noStream) throw new TypeError('network dropped');
      return {ok: true, status: 200, headers: {get: () => 'text/event-stream'}, body: stream.body};
    }
    if (!match[2]) return json(200, byId.get(match[1]));
    return json(404, {error: 'not_found', message: 'No such route.'});
  };
  globalThis.fetch = stub;
  window.fetch = stub;
  return stream;
}

const frame = (seq, event, data) => `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

async function showFollowed(record){
  serve([record]);
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{follow: record.ask_id}} />));
  await until(() => host.querySelector('.ask42-answer'), 'the answer');
}

const WIRE = (state, over = {}) => ({
  check: 'verified', v: 1,
  execution: {state: 'completed', stop_reason: null},
  summary: {state, removals: [], rewrite: 'not_attempted'},
  ...over,
});
const REMOVED_K6 = WIRE('removed', {summary: {state: 'removed', removals: [{stage: 'first_check', cause: 'K6'}], rewrite: 'not_attempted'}});

const NEUTRAL = 'The one-line summary is not available for this answer.';

/* F-01: the module */

test('F-01 the neutral result for everything that is not a verified wire value', () => {
  const neutral = {kind: 'neutral', sentence: NEUTRAL};
  const bad = [
    undefined, null, 'x', 7, [], {},
    {check: 'legacy_unknown'},
    {check: 'unverified', problem: 'digest'},
    {check: 'verified'},
    WIRE('shown', {check: 'Verified'}),
    WIRE('shown', {v: 2}),
    WIRE('shown', {v: true}),
    WIRE('shown', {v: '1'}),
    WIRE('shown', {extra: 1}),
    WIRE('shown', {digest: 'sha256:' + '0'.repeat(64)}),
    {...WIRE('shown'), ask_id: 'a_1', check_run_id: 'r_1', bound: {answer_status: 'complete', summary_blank: false, claims: 2}, digest: 'sha256:' + 'a'.repeat(64)},
    {v: 1, ask_id: 'a_1', check_run_id: 'r_1', execution: {state: 'completed', stop_reason: null}, summary: {state: 'removed', removals: [{stage: 'first_check', cause: 'K6'}], rewrite: 'not_attempted'}, bound: {}, digest: 'sha256:x'},
    WIRE('shown', {execution: {state: 'sleeping', stop_reason: null}}),
    WIRE('shown', {execution: {state: 'completed', stop_reason: 'budget_full'}}),
    WIRE('shown', {execution: {state: 'stopped_on_budget', stop_reason: null}}),
    WIRE('shown', {execution: {state: 'stopped_on_budget', stop_reason: 'cosmic'}}),
    WIRE('shown', {execution: {state: 'completed'}}),
    WIRE('shown', {execution: {state: 'completed', stop_reason: null, extra: 1}}),
    WIRE('rumoured'),
    WIRE('shown', {summary: {state: 'shown', removals: [], rewrite: 'sometimes'}}),
    WIRE('shown', {summary: {state: 'shown', removals: [], rewrite: 'not_attempted', extra: 1}}),
    WIRE('shown', {summary: {state: 'shown', removals: 'none', rewrite: 'not_attempted'}}),
    WIRE('removed', {summary: {state: 'removed', removals: [{stage: 'first_check', cause: 'K66'}], rewrite: 'not_attempted'}}),
    WIRE('removed', {summary: {state: 'removed', removals: [{stage: 'sixth_check', cause: 'K6'}], rewrite: 'not_attempted'}}),
    WIRE('removed', {summary: {state: 'removed', removals: [{stage: 'first_check', cause: 'K6', extra: 1}], rewrite: 'not_attempted'}}),
    WIRE('removed', {summary: {state: 'removed', removals: ['K6'], rewrite: 'not_attempted'}}),
  ];
  for (const value of bad){
    expect(meta.summaryNotice({answer_meta: value})).toEqual(neutral);
    expect(meta.statusWords({answer_meta: value})).toBeNull();
  }
  expect(meta.summaryNotice(null)).toEqual(neutral);
  expect(meta.summaryNotice(undefined)).toEqual(neutral);
  expect(meta.summaryNotice({})).toEqual(neutral);
  expect(meta.summaryNotice('record')).toEqual(neutral);
});

test('F-01 each verified summary state gives its kind and the export sentence', () => {
  const sentence = (summary) => meta.summaryNotice({answer_meta: WIRE(summary.state, {summary})});
  expect(sentence({state: 'shown', removals: [], rewrite: 'not_attempted'})).toEqual({kind: 'shown', sentence: ''});
  expect(sentence({state: 'fixed_text', removals: [], rewrite: 'not_attempted'})).toEqual({kind: 'fixed_text', sentence: ''});
  expect(sentence({state: 'shown_rewritten', removals: [{stage: 'support_check', cause: 'claim_cut'}], rewrite: 'kept'}))
    .toEqual({kind: 'shown_rewritten', sentence: 'This summary was rewritten once from the findings that passed.'});
  expect(sentence({state: 'blank_unexplained', removals: [], rewrite: 'not_attempted'}))
    .toEqual({kind: 'blank_unexplained', sentence: 'No one-line summary was written for this answer.'});
  expect(sentence({state: 'no_answer', removals: [], rewrite: 'not_attempted'}))
    .toEqual({kind: 'no_answer', sentence: 'There is no answer: the run did not finish.'});
  const removed = (stage, cause, rewrite = 'not_attempted') => sentence({state: 'removed', removals: [{stage, cause}], rewrite});
  expect(removed('first_check', 'K2').sentence).toBe('The one-line summary was removed because it used a figure that no checked finding holds.');
  expect(removed('recheck', 'K3').sentence).toBe("The one-line summary was removed because it named a place no cited post is located in, or relied on a post outside the question's window or market.");
  expect(removed('field_check', 'K3').sentence).toBe("The one-line summary was removed because it named a place no cited post is located in, or relied on a post outside the question's window or market.");
  expect(removed('first_check', 'K6').sentence).toBe('The one-line summary was removed because it used a term or source the trust rules do not allow.');
  expect(removed('first_check', 'K8').sentence).toBe('The one-line summary was removed because it quoted words that are not in the posts it cites.');
  expect(removed('field_check', 'K9').sentence).toBe('The one-line summary was removed because it made a forecast, and forecasts stay held until they beat a simple no-change forecast.');
  expect(removed('support_check', 'claim_cut').sentence).toBe('The one-line summary was removed after a claim it may have rested on did not pass its checks.');
  expect(removed('critic', 'claim_cut').sentence).toBe('The one-line summary was removed after a claim it may have rested on did not pass its checks.');
  expect(removed('support_check', 'claim_narrowed').sentence).toBe('The one-line summary was removed after a claim it may have rested on was narrowed.');
  expect(removed('field_check', 'K6').sentence).toBe('The one-line summary was removed because the text check found it describes people in a way the trust rules do not allow.');
  expect(removed('field_check', 'field_unchecked').sentence).toBe('The one-line summary was too long to check with the posts it rests on, so it was left out.');
  expect(removed('first_check', 'unattributed').sentence).toBe('The one-line summary was removed by the checks.');
  expect(removed('recheck', 'K6', 'removed_after_check').sentence)
    .toBe('The one-line summary was removed because it used a term or source the trust rules do not allow. A rewritten summary did not pass the checks either.');
  /* the first removal speaks; a removal list with no entry states nothing */
  expect(sentence({state: 'removed', removals: [{stage: 'first_check', cause: 'K2'}, {stage: 'recheck', cause: 'K9'}], rewrite: 'not_attempted'}).sentence)
    .toBe('The one-line summary was removed because it used a figure that no checked finding holds.');
  expect(sentence({state: 'removed', removals: [], rewrite: 'not_attempted'})).toEqual({kind: 'neutral', sentence: NEUTRAL});
});

test('F-01 a summary that is missing is explained only by a state that explains a missing summary', () => {
  const notice = (state, removals = [], rewrite = 'not_attempted') => meta.summaryNotice({answer_meta: WIRE(state, {summary: {state, removals, rewrite}})});
  expect(meta.missingSummarySentence(notice('shown_rewritten', [{stage: 'support_check', cause: 'claim_cut'}], 'kept'))).toBe(NEUTRAL);
  expect(meta.missingSummarySentence(notice('shown'))).toBe(NEUTRAL);
  expect(meta.missingSummarySentence(notice('fixed_text'))).toBe(NEUTRAL);
  expect(meta.missingSummarySentence(notice('blank_unexplained'))).toBe('No one-line summary was written for this answer.');
  expect(meta.missingSummarySentence(notice('no_answer'))).toBe('There is no answer: the run did not finish.');
  expect(meta.missingSummarySentence(null)).toBe(NEUTRAL);
  expect(meta.missingSummarySentence({kind: 'neutral', sentence: NEUTRAL})).toBe(NEUTRAL);
});

test('F-01 the status words of a verified run, and only of a verified run', () => {
  const run = (state, stop_reason) => ({answer_meta: WIRE('fixed_text', {execution: {state, stop_reason}})});
  expect(meta.statusWords(run('stopped_on_budget', 'budget_full'))).toBe("Stopped at this question's model budget, not for lack of evidence");
  expect(meta.statusWords(run('stopped_on_budget', 'model_call_unverified'))).toBe('Stopped because a model call failed or did not report what it cost, not for lack of evidence');
  expect(meta.statusWords(run('stopped_on_budget', 'price_unreadable'))).toBe('Stopped because the cost of a model call could not be worked out, not for lack of evidence');
  expect(meta.statusWords(run('stopped_on_request', null))).toBe('Stopped before an answer was written');
  expect(meta.statusWords(run('refused_budget_spent', null))).toBe('Not researched: the model budget for today is spent');
  expect(meta.statusWords(run('completed', null))).toBeNull();
  expect(meta.statusWords(run('failed', null))).toBeNull();
});

test('F-01 the Stopped early line is shown unless the run is verified as completed', () => {
  const stopped = (answer_meta) => meta.stoppedEarly({status: 'stopped', answer_meta});
  expect(stopped(WIRE('shown'))).toBe(false);
  expect(stopped(undefined)).toBe(true);
  expect(stopped({check: 'legacy_unknown'})).toBe(true);
  expect(stopped({check: 'unverified', problem: 'digest'})).toBe(true);
  expect(stopped(WIRE('fixed_text', {execution: {state: 'stopped_on_request', stop_reason: null}}))).toBe(true);
  expect(stopped({...WIRE('shown'), extra: 1})).toBe(true);
  expect(meta.stoppedEarly({status: 'complete', answer_meta: WIRE('shown')})).toBe(false);
  expect(meta.stoppedEarly({status: 'complete'})).toBe(false);
});

/* F-02, F-04: the Ask page, held to the export */

const STATUS_LINES = [
  "Stopped at this question's model budget, not for lack of evidence",
  'Stopped because a model call failed or did not report what it cost, not for lack of evidence',
  'Stopped because the cost of a model call could not be worked out, not for lack of evidence',
  'Stopped before an answer was written',
  'Not researched: the model budget for today is spent',
];
const CODES = /\b(K[0-9]|first_check|support_check|recheck|field_check|claim_cut|claim_narrowed|field_unchecked|unattributed|blank_unexplained|legacy_unknown|shown_rewritten|fixed_text|removed_after_check)\b/;

test('F-02 the fixture holds every producer state and the shapes around them', () => {
  const names = states.map((entry) => entry.name);
  for (const id of ['F01', 'F02', 'F03', 'F04', 'F06', 'F07', 'F08', 'F10', 'F11', 'F12a', 'F12b', 'F12c', 'F13', 'F14', 'F15a', 'F15b', 'F16', 'F17', 'F18', 'F19', 'F20', 'stopped-on-request', 'planted-wire-on-blank']){
    expect(names).toContain(id);
  }
  expect(states.length).toBe(31);
});

for (const entry of states){
  test(`F-02 the Ask page says what the export says: ${entry.name}`, async () => {
    const record = recordOf(entry);
    await showFollowed(record);
    const page = host.querySelector('.ask42-answer');
    const short = host.querySelector('.ask42-short');
    const exported = entry.export.short;
    expect(plain(short.textContent)).toBe(exported[0][1]);
    const note = host.querySelector('.ask42-short-note');
    if (exported.length > 1){
      expect(note).not.toBeNull();
      expect(plain(note.textContent)).toBe(exported[1][1]);
    } else {
      expect(note).toBeNull();
    }
    const statuses = [...page.querySelectorAll('.ask42-status')].map((node) => plain(node.textContent));
    for (const line of entry.export.review.filter((words) => STATUS_LINES.includes(words))){
      expect(statuses).toContain(line);
    }
    for (const line of STATUS_LINES.filter((words) => !entry.export.review.includes(words))){
      expect(statuses).not.toContain(line);
    }
    const early = entry.export.review.some((words) => words.startsWith('Stopped before the end'));
    expect(statuses.some((words) => words.startsWith('Stopped early'))).toBe(early);
    expect(plain(page.textContent)).not.toContain('The one-line summary did not pass the checks.');
    expect(plain(page.textContent)).not.toMatch(CODES);
  });
}

test('F-02 the claims list still renders for a partial answer with surviving claims', async () => {
  const entry = states.find((item) => item.name === 'F10');
  await showFollowed(recordOf(entry));
  expect(host.querySelectorAll('.ask42-claim')).toHaveLength(entry.answer.claims);
  expect(plain(host.querySelector('.ask42-short').textContent)).toBe(entry.export.short[0][1]);
});

test('F-02 a blank summary that the state does not explain never says a check failed', async () => {
  for (const name of ['F18', 'F19', 'planted-wire-on-blank', 'blank-legacy-partial-base']){
    const entry = states.find((item) => item.name === name);
    await showFollowed(recordOf(entry));
    const words = plain(host.querySelector('.ask42-short').textContent);
    expect(words).not.toMatch(/did not pass the checks/);
    expect(words).not.toMatch(/failed/);
    await act(async () => root.render(null));
  }
});

/* F-03: the stream carries no state, the record does */

async function askLive(record, options){
  const stream = serve([record], options);
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{}} />));
  const box = host.querySelector('textarea');
  const proto = window.HTMLTextAreaElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(box, 'Which sounds are rising on TikTok in South Africa this week?');
    box.dispatchEvent(new window.Event('input', {bubbles: true}));
  });
  const ask = [...host.querySelectorAll('button')].find((node) => plain(node.textContent) === 'Ask');
  await act(async () => { ask.click(); });
  await until(() => calls.some((call) => call.path === '/api/ask'), 'the ask to start');
  return stream;
}

const FORGED = WIRE('removed', {summary: {state: 'removed', removals: [{stage: 'first_check', cause: 'K2'}], rewrite: 'not_attempted'}});

test('F-03 a done payload that carries answer_meta is ignored, and the stream and the polling fallback render the same', async () => {
  const entry = states.find((item) => item.name === 'F19');
  const record = recordOf(entry);
  const followed = await (async () => { await showFollowed(record); return plain(host.querySelector('.ask42-short').textContent); })();
  await act(async () => root.render(null));
  const stream = await askLive(record);
  for (const step of record.steps) stream.push(frame(step.seq, 'step', step));
  stream.push(frame(99, 'done', {seq: 99, status: record.status, url: `/api/ask/${record.ask_id}`, answer_meta: FORGED}));
  stream.close();
  await until(() => host.querySelector('.ask42-short'), 'the live answer');
  const live = plain(host.querySelector('.ask42-short').textContent);
  expect(live).not.toContain('figure that no checked finding holds');
  expect(live).toContain(NEUTRAL);
  expect(live).toBe(followed);
  await act(async () => root.render(null));
  await askLive(record, {noStream: true});
  await until(() => host.querySelector('.ask42-short'), 'the polled answer');
  expect(plain(host.querySelector('.ask42-short').textContent)).toBe(followed);
});

test('F-04 a saved answer opened by its address reads the same as the live one for a verified removal', async () => {
  const entry = states.find((item) => item.name === 'F14');
  const record = recordOf(entry);
  await showFollowed(record);
  const followed = plain(host.querySelector('.ask42-short').textContent);
  expect(followed).toBe(entry.export.short[0][1]);
  await act(async () => root.render(null));
  const stream = await askLive(record);
  for (const step of record.steps) stream.push(frame(step.seq, 'step', step));
  stream.push(frame(99, 'done', {seq: 99, status: record.status, url: `/api/ask/${record.ask_id}`}));
  stream.close();
  await until(() => host.querySelector('.ask42-short'), 'the live answer');
  expect(plain(host.querySelector('.ask42-short').textContent)).toBe(followed);
});

/* F-05: the investigation view and the dossier views */

function investigationBody(record){
  return {
    investigation_id: 'i_0123456789ab', version: 3, created_at: '2026-09-29T08:00:00+02:00', updated_at: '2026-09-29T08:20:00+02:00',
    who: 'passcode', status: 'complete', question: record.question, market: 'ZA', plan: {sub_questions: [], researchers: 1, gap_round: false, max_credits: 100, max_model_usd: 1},
    estimate: {credits: 1, model_usd: 1, minutes: 1}, ask_id: record.ask_id, run_id: 'r_plan_1', record: {...record, investigation_id: 'i_0123456789ab'},
  };
}

for (const name of ['F10', 'F14', 'F18', 'F19', 'F02', 'F01', 'planted-wire-on-blank', 'blank-legacy-no-claims']){
  test(`F-05 the investigation view says what the export says: ${name}`, async () => {
    const entry = states.find((item) => item.name === name);
    const record = recordOf(entry);
    const body = investigationBody(record);
    serve([]);
    const stub = async (url) => (String(url) === '/api/investigations/i_0123456789ab' ? json(200, body) : json(404, {error: 'not_found', message: 'No.'}));
    globalThis.fetch = stub;
    window.fetch = stub;
    await act(async () => root.render(<InvestigationPage investigationId="i_0123456789ab" />));
    await until(() => host.querySelector('.ask42-short'), 'the investigation answer');
    expect(plain(host.querySelector('.ask42-short').textContent)).toBe(entry.export.short[0][1]);
    const note = host.querySelector('.ask42-short-note');
    if (entry.export.short.length > 1) expect(plain(note.textContent)).toBe(entry.export.short[1][1]);
    else expect(note).toBeNull();
  });
}

test('F-05 the investigation view shows the run-ending words of a verified stop', async () => {
  const entry = states.find((item) => item.name === 'F06');
  const body = investigationBody(recordOf(entry));
  const stub = async () => json(200, body);
  globalThis.fetch = stub;
  window.fetch = stub;
  await act(async () => root.render(<InvestigationPage investigationId="i_0123456789ab" />));
  await until(() => host.querySelector('.ask42-short'), 'the investigation answer');
  const statuses = [...host.querySelectorAll('.ask42-status')].map((node) => plain(node.textContent));
  expect(statuses).toContain("Stopped at this question's model budget, not for lack of evidence");
});

const C = completeRecord.answer;
function dossierView(over = {}, surface = 'draft'){
  const claims = C.claims.map(({id, ...rest}) => ({claim_id: id, evidence_ids: [], quotes: [], numbers: [], ...clone(rest), kept: true, note: null}));
  return {
    dossier_id: 'd_fixture01', version: surface === 'draft' ? 1 : 2, state: surface === 'draft' ? 'draft' : 'frozen', created_at: '2026-09-28T06:12:00+02:00', who: 'passcode',
    title: completeRecord.question, source: {ask_id: completeRecord.ask_id}, source_ask_id: completeRecord.ask_id, question: completeRecord.question,
    market: 'ZA', as_of: C.as_of, answer_status: 'partial', summary: null, claims, evidence: clone(C.evidence), gaps: [], left_out: [],
    content_hash: 'sha256:' + 'a'.repeat(64), ticks: {}, needs_tick: [], reviews: surface === 'draft' ? undefined : {},
    body_v: 2, source_answer_meta: REMOVED_K6, summary_state: 'removed', ...over,
  };
}

async function openDossier(view, surface){
  const path = surface === 'shared' ? '/api/dossiers/d_fixture01/versions/2' : '/api/dossiers/d_fixture01';
  const stub = async (url) => (String(url) === path ? json(200, view) : json(404, {error: 'not_found', message: 'No.'}));
  globalThis.fetch = stub;
  window.fetch = stub;
  await act(async () => root.render(surface === 'shared'
    ? <SharedDossier dossierId="d_fixture01" version="2" onAuth={() => {}} />
    : <DossierPage dossierId="d_fixture01" onAuth={() => {}} />));
  await until(() => host.querySelector('.dossiers42-summary'), 'the dossier summary');
  return plain(host.querySelector('.dossiers42-summary').textContent);
}

const DOSSIER_CASES = [
  ['a verified removal', {}, 'The one-line summary was removed because it used a term or source the trust rules do not allow.'],
  ['an unexplained blank', {source_answer_meta: WIRE('blank_unexplained'), summary_state: 'blank_unexplained'}, 'No one-line summary was written for this answer.'],
  ['a selection that leaves a claim out', {claims: dossierView().claims.map((claim, index) => ({...claim, kept: index === 0})), summary_state: 'omitted_by_selection'}, 'The one-line summary is left out because not every finding is kept.'],
  ['a body from before the state existed', {body_v: undefined, source_answer_meta: undefined, summary_state: 'legacy_unknown'}, NEUTRAL],
  ['a state that did not verify', {source_answer_meta: {check: 'unverified', problem: 'digest'}, summary_state: 'legacy_unknown'}, NEUTRAL],
  ['a first version body that carries a verified state', {body_v: undefined, summary_state: 'legacy_unknown'}, NEUTRAL],
  ['a body of a version this page does not know', {body_v: 3, summary_state: 'legacy_unknown'}, NEUTRAL],
  ['a removal the blank summary does not fit', {summary: 'Text the state says was removed.'}, 'Text the state says was removed.'],
  ['a shown state with no summary', {source_answer_meta: WIRE('shown'), summary_state: 'shown'}, NEUTRAL],
  ['a wire value with an extra key', {source_answer_meta: {...REMOVED_K6, digest: 'sha256:x'}}, NEUTRAL],
  ['a misleading gap beside a verified state', {gaps: [{what: 'One-line summary removed: because the gap says so', searched: 'the short answer text', why: 'partial'}], source_answer_meta: WIRE('blank_unexplained'), summary_state: 'blank_unexplained'}, 'No one-line summary was written for this answer.'],
];

for (const surface of ['draft', 'frozen', 'shared']){
  for (const [label, over, expected] of DOSSIER_CASES){
    test(`F-05 a ${surface} dossier states the summary outcome the export states: ${label}`, async () => {
      const view = dossierView(over, surface);
      for (const key of Object.keys(view)) if (view[key] === undefined) delete view[key];
      expect(await openDossier(view, surface)).toBe(expected);
    });
  }
}

test('F-05 a dossier that holds a summary is never explained by a state that says it has none', () => {
  const view = {...dossierView(), summary: 'Some words that are held.'};
  expect(meta.dossierSummaryWords(view)).toBe(NEUTRAL);
  expect(meta.dossierSummaryWords({...view, summary: ''})).toBe('The one-line summary was removed because it used a term or source the trust rules do not allow.');
  expect(meta.dossierSummaryWords(null)).toBe(NEUTRAL);
  expect(meta.dossierSummaryWords({})).toBe(NEUTRAL);
});

/* F-06: the words are the producer's words */

function pythonTable(source, name){
  const start = source.indexOf(name + ' = {');
  if (start === -1) throw new Error(name + ' not found');
  let depth = 0;
  let at = source.indexOf('{', start);
  const begin = at;
  let end = -1;
  for (; at < source.length; at += 1){
    const ch = source[at];
    if (ch === '"'){
      at += 1;
      while (source[at] !== '"'){ if (source[at] === '\\') at += 1; at += 1; }
      continue;
    }
    if (ch === '{' || ch === '(') depth += 1;
    if (ch === '}' || ch === ')') depth -= 1;
    if (depth === 0){ end = at; break; }
  }
  const body = source.slice(begin + 1, end);
  const entries = [];
  let level = 0;
  let from = 0;
  for (let i = 0; i < body.length; i += 1){
    const ch = body[i];
    if (ch === '"'){ i += 1; while (body[i] !== '"'){ if (body[i] === '\\') i += 1; i += 1; } continue; }
    if (ch === '(') level += 1;
    if (ch === ')') level -= 1;
    if (ch === ',' && level === 0){ entries.push(body.slice(from, i)); from = i + 1; }
  }
  if (body.slice(from).trim()) entries.push(body.slice(from));
  const strings = (text) => [...text.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1].replace(/\\"/g, '"').replace(/\\'/g, "'"));
  const table = {};
  for (const entry of entries){
    let level2 = 0;
    let colon = -1;
    for (let i = 0; i < entry.length; i += 1){
      const ch = entry[i];
      if (ch === '"'){ i += 1; while (entry[i] !== '"'){ if (entry[i] === '\\') i += 1; i += 1; } continue; }
      if (ch === '(') level2 += 1;
      if (ch === ')') level2 -= 1;
      if (ch === ':' && level2 === 0){ colon = i; break; }
    }
    const keyText = entry.slice(0, colon);
    const key = /^\s*\(/.test(keyText)
      ? strings(keyText).join('|') + (/None/.test(keyText) ? '|' : '')
      : strings(keyText)[0];
    table[key] = strings(entry.slice(colon + 1)).join('');
  }
  return table;
}

const WORDS = words;
const stateFile = new URL('../../../../../core/agent/answer_state.py', import.meta.url);
const summaryFile = new URL('../../../../../core/api/summary_state.py', import.meta.url);
/* A checkout that holds the producer holds the API module with it. The parity
   tests below read both; a checkout with neither is a lane branch cut before
   the services landed, and one with only one of them is broken. */
const producerHere = existsSync(stateFile);
const bothOrNeither = () => expect(existsSync(summaryFile)).toBe(producerHere);

test('F-06 the reader sentences equal the producer table, key by key', () => {
  expect(Object.keys(WORDS.reader_sentences).length).toBe(15);
  expect(meta.READER_SENTENCES).toEqual(WORDS.reader_sentences);
  expect(meta.NEUTRAL).toBe(WORDS.neutral);
  expect(meta.OMITTED).toBe(WORDS.omitted);
  expect(meta.NEUTRAL).toBe(NEUTRAL);
});

test('F-06 the status words equal the producer table, key by key', () => {
  expect(Object.keys(WORDS.status_words).length).toBe(5);
  expect(meta.STATUS_WORDS).toEqual(WORDS.status_words);
});

test('F-06 the closed enums equal the producer enums', () => {
  expect(meta.ENUMS).toEqual(WORDS.enums);
});

test('F-06 the pinned tables equal the producer source in this checkout', () => {
  bothOrNeither();
  if (!producerHere) return;
  const source = readFileSync(stateFile, 'utf8');
  expect(pythonTable(source, 'READER_SENTENCES')).toEqual(WORDS.reader_sentences);
  expect(pythonTable(source, 'STATUS_WORDS')).toEqual(WORDS.status_words);
  const tuple = (name) => {
    const found = new RegExp(name + ' = \\(([^)]*)\\)').exec(source);
    return [...found[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  };
  expect(WORDS.enums.execution).toEqual(tuple('EXECUTION_STATES'));
  expect(WORDS.enums.stopReasons).toEqual(tuple('STOP_REASONS'));
  expect(WORDS.enums.summary).toEqual(tuple('SUMMARY_STATES'));
  expect(WORDS.enums.stages).toEqual(tuple('REMOVAL_STAGES'));
  expect(WORDS.enums.causes).toEqual(tuple('REMOVAL_CAUSES'));
  expect(WORDS.enums.rewrites).toEqual(tuple('REWRITE_OUTCOMES'));
  const summary = readFileSync(summaryFile, 'utf8');
  expect(summary).toContain(`NEUTRAL = "${WORDS.neutral}"`);
  expect(summary).toContain(`OMITTED = "${WORDS.omitted}"`);
});

/* F-07: records from an older API */

for (const [label, value] of [['key absent', undefined], ['an empty object', {}], ['a string', 'x'], ['a number', 5], ['null', null], ['a list', []]]){
  test(`F-07 a record with answer_meta as ${label} reads as not available and does not throw`, async () => {
    const entry = states.find((item) => item.name === 'F19');
    const record = recordOf(entry);
    if (value === undefined) delete record.answer_meta; else record.answer_meta = value;
    await showFollowed(record);
    expect(plain(host.querySelector('.ask42-short').textContent)).toBe(entry.export.short[0][1]);
    expect(plain(host.querySelector('.ask42-short').textContent)).toContain(NEUTRAL);
  });
}
