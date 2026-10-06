import {afterEach, expect, test} from 'bun:test';
import {deliveryReport, pollChat, reportDelivery, servedAssetVersion} from '../../chatTransport.js';

const nativeFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = nativeFetch; });

const REQUEST_ID = '00000000-0000-4000-8000-000000000001';
const DIGEST = 'a'.repeat(64);
const terminal = {answer: 'Stored answer.', sources: [], intelligence: {status: 'complete'}, lifecycle: {request_id: REQUEST_ID, state: 'complete'}};

function served(body, digest){
  return {ok: true, status: 200, headers: {get: name => name === 'X-Question-Response-Digest' ? digest : null}, json: async () => body};
}

async function polled(digest){
  globalThis.fetch = async () => served(structuredClone(terminal), digest);
  return pollChat({id: 'chat_job', deadlineAt: Date.now() + 60000});
}

test('a rendered answer is reported with exactly the five named fields and no content', async () => {
  const response = await polled(DIGEST);
  const posts = [];
  globalThis.fetch = async (path, options) => { posts.push({path, options}); return served({delivery: 'rendered'}, null); };
  expect(await reportDelivery(response)).toBe(true);
  expect(posts).toHaveLength(1);
  expect(posts[0].path).toBe('/api/internal/v2/question-delivery');
  expect(posts[0].options.method).toBe('POST');
  const body = JSON.parse(posts[0].options.body);
  expect(Object.keys(body).sort()).toEqual(['asset_version', 'client_event_at', 'event_type', 'request_id', 'response_digest']);
  expect(body.request_id).toBe(REQUEST_ID);
  expect(body.response_digest).toBe(DIGEST);
  expect(body.event_type).toBe('rendered');
  expect(body.client_event_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  expect(body.asset_version).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/);
  expect(posts[0].options.body).not.toContain('Stored answer.');
});

test('no report is sent when the status read named no digest', async () => {
  const response = await polled(null);
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; return served({}, null); };
  expect(deliveryReport(response, 'rendered')).toBeNull();
  expect(await reportDelivery(response)).toBe(false);
  expect(calls).toBe(0);
});

test('a malformed digest or request id is never reported', async () => {
  expect(deliveryReport(await polled('not-a-digest'), 'rendered')).toBeNull();
  const response = await polled(DIGEST);
  expect(deliveryReport(response, 'healthy')).toBeNull();
  expect(deliveryReport({...response}, 'rendered')).toBeNull();
});

test('a failed report is swallowed because delivery telemetry is advisory', async () => {
  const response = await polled(DIGEST);
  globalThis.fetch = async () => { throw new TypeError('network failed'); };
  expect(await reportDelivery(response)).toBe(false);
});

test('the served asset version fits the server grammar', () => {
  expect(servedAssetVersion()).toMatch(/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/);
  expect(servedAssetVersion('https://example.test/assets/index-AbC_12.js?x=1')).toBe('index-AbC_12.js');
  expect(servedAssetVersion('not a url')).toBe('unversioned');
});
