import {afterEach, expect, test} from 'bun:test';
import {FOLLOW_UP_CONTEXT_REFUSED, POLICY_REVIEW_LAPSED, SERVICE_TEMPORARY, SUBMISSION_UNCERTAIN, pollChat, submitChat, terminalChatTurn} from '../../chatTransport.js';

const nativeFetch = globalThis.fetch;
const nativeNow = Date.now;
const nativeTimeout = globalThis.setTimeout;
const nativeClearTimeout = globalThis.clearTimeout;
afterEach(() => { globalThis.fetch = nativeFetch; Date.now = nativeNow; globalThis.setTimeout = nativeTimeout; globalThis.clearTimeout = nativeClearTimeout; });
const response = body => ({ok: true, status: 200, json: async () => body});

test('a follow-up posts explicit parent and anchor identities without client evidence authority', async () => {
  const history = [{role: 'user', content: 'An earlier question.'}];
  const references = {
    parent_request_id: '00000000-0000-4000-8000-000000000002',
    thread_anchor_request_id: '00000000-0000-4000-8000-000000000001',
    market: 'ng', evidence: ['untrusted_source'], context_digest: 'untrusted_digest',
  };
  let posted;
  globalThis.fetch = async (_path, options) => { posted = JSON.parse(options.body); return response({job_id: 'accepted_job'}); };
  await submitChat('Follow up', history, 'za', undefined, references);
  expect(posted).toEqual({
    message: 'Follow up', history, market: 'za',
    parent_request_id: references.parent_request_id,
    thread_anchor_request_id: references.thread_anchor_request_id,
  });
  expect(history).toEqual([{role: 'user', content: 'An earlier question.'}]);
});

test.each([undefined, {}, {parent_request_id: null, thread_anchor_request_id: null}])('a fresh question retains the exact legacy body with references %j', async references => {
  let posted;
  globalThis.fetch = async (_path, options) => { posted = JSON.parse(options.body); return response({job_id: 'accepted_job'}); };
  await submitChat('New topic', [], 'ke', undefined, references);
  expect(posted).toEqual({message: 'New topic', history: [], market: 'ke'});
});

test('an explicit parent without an anchor preserves the nullable anchor field', async () => {
  let posted;
  globalThis.fetch = async (_path, options) => { posted = JSON.parse(options.body); return response({job_id: 'accepted_job'}); };
  await submitChat('Follow up', [], 'za', undefined, {parent_request_id: '00000000-0000-4000-8000-000000000002'});
  expect(posted.parent_request_id).toBe('00000000-0000-4000-8000-000000000002');
  expect(posted.thread_anchor_request_id).toBeNull();
});

test('an ambiguous submission failure is not retried', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new TypeError('network failed'); };
  await expect(submitChat('Question', [], 'za')).rejects.toMatchObject({code: 'submission_uncertain'});
  expect(calls).toBe(1);
});

test('an accepted request keeps the full four-minute foreground window', async () => {
  Date.now = () => 1000;
  globalThis.fetch = async () => response({job_id: 'accepted_job'});
  await expect(submitChat('Question', [], 'za')).resolves.toEqual({id: 'accepted_job', deadlineAt: 241000});
});

test('polling retains a job past the old ninety-second cutoff', async () => {
  let now = 0;
  Date.now = () => now;
  globalThis.setTimeout = (callback, delay, ...args) => nativeTimeout(callback, delay === 1500 ? 0 : delay, ...args);
  const paths = [];
  globalThis.fetch = async path => {
    paths.push(path);
    now += 55000;
    return response(paths.length < 3 ? {pending: true} : {answer: 'Completed after ninety seconds.', sources: []});
  };
  const result = await pollChat({id: 'existing_job', deadlineAt: 180000});
  expect(result.answer).toBe('Completed after ninety seconds.');
  expect(paths).toHaveLength(3);
  expect(paths.every(path => path.startsWith('/api/chat/status?job_id=existing_job&'))).toBe(true);
});

test('an expired wait checks its existing job once without resetting the deadline', async () => {
  const job = {id: 'expired_job', deadlineAt: 1};
  let calls = 0;
  globalThis.fetch = async path => { calls += 1; expect(path).toContain('job_id=expired_job'); return response({pending: true}); };
  await expect(pollChat(job)).rejects.toMatchObject({code: 'reply_uncertain', job});
  expect(calls).toBe(1);
  expect(job.deadlineAt).toBe(1);
});

test('typed terminal failures preserve intelligence and unresolved usage', async () => {
  const terminal = {error: true, reason: 'metering_persistence_failed', intelligence: {status: 'unavailable', usage: {status: 'unresolved', model_calls: null, reservation_ids: ['reservation_1']}}};
  globalThis.fetch = async () => response(terminal);
  const result = terminalChatTurn(await pollChat({id: 'existing_job', deadlineAt: Date.now() + 180000}));
  expect(result.error).toBe(true);
  expect(result.reason).toBe('metering_persistence_failed');
  expect(result.intelligence).toEqual(terminal.intelligence);
  expect(result.intelligence.usage.model_calls).toBeNull();
});

test('aborting before submission starts no request', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; return response({job_id: 'unexpected'}); };
  const controller = new AbortController(); controller.abort();
  await expect(submitChat('Question', [], 'za', controller.signal)).rejects.toMatchObject({aborted: true});
  expect(calls).toBe(0);
});

function statusClock(){
  let now = 1000;
  let id = 0;
  const timers = new Map();
  Date.now = () => now;
  globalThis.setTimeout = (callback, delay) => { timers.set(++id, {at: now + delay, callback}); return id; };
  globalThis.clearTimeout = timer => timers.delete(timer);
  return async milliseconds => {
    const end = now + milliseconds;
    while (true){
      const next = [...timers.entries()].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      now = next[1].at;
      timers.delete(next[0]);
      next[1].callback();
      for (let turn = 0; turn < 12; turn += 1) await Promise.resolve();
    }
    now = end;
    for (let turn = 0; turn < 12; turn += 1) await Promise.resolve();
  };
}

test.each([241000, 1])('a complete status at sixteen seconds reaches the existing job with deadline %s', async deadlineAt => {
  const advance = statusClock();
  const calls = [];
  const terminal = {answer: 'Retained completed answer.', sources: [{id: 'evidence_1'}]};
  globalThis.fetch = (path, options) => new Promise((resolve, reject) => {
    calls.push({path, method: options.method});
    const timer = setTimeout(() => resolve(response(terminal)), 16000);
    options.signal.addEventListener('abort', () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); }, {once: true});
  });
  const job = {id: 'retained_job', deadlineAt};
  let outcome;
  pollChat(job).then(value => { outcome = value; }, error => { outcome = error; });
  await advance(16000);
  expect(outcome).toEqual(terminal);
  expect(job.deadlineAt).toBe(deadlineAt);
  expect(calls).toEqual([{path: '/api/chat/status?job_id=retained_job&_=1000', method: undefined}]);
});

test('a slow status read still stops at the existing foreground deadline', async () => {
  const advance = statusClock();
  let aborted = false;
  globalThis.fetch = (_path, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('Aborted', 'AbortError')); }, {once: true});
  });
  const job = {id: 'retained_job', deadlineAt: 11000};
  let outcome;
  pollChat(job).catch(error => { outcome = error; });
  await advance(10000);
  expect(aborted).toBe(true);
  expect(outcome).toMatchObject({code: 'reply_uncertain', job});
});

test('a named idempotency key travels with the question so a retry joins the same request', async () => {
  let posted;
  globalThis.fetch = async (_path, options) => { posted = JSON.parse(options.body); return response({job_id: 'accepted_job'}); };
  await submitChat('Question', [], 'za', undefined, {}, '', 'ask-00000000-0000-4000-8000-00000000000a');
  expect(posted).toEqual({message: 'Question', history: [], market: 'za', idempotency_key: 'ask-00000000-0000-4000-8000-00000000000a'});
});

test('a 503 on submission reads as a temporary service problem that is safe to retry', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; return {ok: false, status: 503, headers: {get: () => null}, url: '/api/chat/send', json: async () => ({detail: 'General question admission is unavailable'})}; };
  const failure = await submitChat('Question', [], 'za', undefined, {}, '', 'ask-00000000-0000-4000-8000-00000000000a').catch(error => error);
  expect(failure).toMatchObject({code: 'submission_uncertain', status: 503});
  expect(failure.message).toBe('The question service had a temporary problem, so this question may not have started. Retry sends it again as the same request, so it cannot start a second one.');
  expect(calls).toBe(1);
});

test('a connection that ends before submission is confirmed says the question may have started and never resends by itself', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new TypeError('network failed'); };
  const failure = await submitChat('Question', [], 'za', undefined, {}, '', 'ask-00000000-0000-4000-8000-00000000000a').catch(error => error);
  expect(failure.code).toBe('submission_uncertain');
  expect(failure.message).toBe('The connection ended before the service confirmed this question, so it may already have started. Retry sends it again as the same request, so it cannot start a second one.');
  expect(calls).toBe(1);
});

test('a lapsed pricing review says the question was not admitted and nothing was written', async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; return {ok: false, status: 503, json: async () => ({detail: {code: 'policy_review_lapsed', message: 'server text is not shown'}})}; };
  const refused = submitChat('Question', [], 'za');
  await expect(refused).rejects.toMatchObject({code: 'policy_review_lapsed', status: 503, message: POLICY_REVIEW_LAPSED});
  await refused.catch(error => {
    expect(error.message).not.toBe(SUBMISSION_UNCERTAIN);
    expect(error.message).toContain('not admitted');
    expect(error.message).toContain('Nothing was written');
    expect(error.message).toContain('pricing review');
  });
  expect(calls).toBe(1);
});

/* Quiet register, 23 Sept 2026: the Ask redesign (later the same day) words a
   503 on submission as a temporary service problem with a safe Retry instead
   of the old SUBMISSION_UNCERTAIN line. The check stays what core meant: an
   admission failure without the lapsed review code is still uncertain and is
   never read as a lapsed pricing review. */
test('an unavailable admission without the lapsed review code stays uncertain', async () => {
  globalThis.fetch = async () => ({ok: false, status: 503, json: async () => ({detail: 'General question admission is unavailable'})});
  await expect(submitChat('Question', [], 'za')).rejects.toMatchObject({code: 'submission_uncertain', message: SERVICE_TEMPORARY});
});

/* A follow-up whose earlier answer the service will not take as context, for
   example one asked under another client lens, is refused at admission. The
   reader is told that in words, never shown the service's code. */
test.each([
  [409, 'parent_context_invalid'],
  [409, 'parent_context_unavailable'],
  [409, 'parent_context_conflict'],
  [409, 'parent_alias_invalid'],
  [400, 'parent_reference_invalid'],
])('a refused follow-up context (%i %s) reads in plain words', async (status, code) => {
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; return {ok: false, status, json: async () => ({detail: code})}; };
  const refused = submitChat('And in Kenya?', [], 'za', undefined, {parent_request_id: '00000000-0000-4000-8000-000000000001', thread_anchor_request_id: '00000000-0000-4000-8000-000000000001'});
  await expect(refused).rejects.toMatchObject({code, status, message: FOLLOW_UP_CONTEXT_REFUSED});
  expect(FOLLOW_UP_CONTEXT_REFUSED).not.toMatch(/_/);
  expect(FOLLOW_UP_CONTEXT_REFUSED).toContain('nothing was started');
  expect(calls).toBe(1);
});
