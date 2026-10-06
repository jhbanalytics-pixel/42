/* The delivery report leaves only after React committed the answer it names,
   once per rendered turn, and says render_failed where the reply the page holds
   is withheld because it did not pass the client check. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {holdDelivery, pollChat} = await import('../../chatTransport.js');
const {useTurnDelivery} = await import('../../chat.jsx');

const REQUEST_ID = '00000000-0000-4000-8000-000000000001';
const DIGEST = 'b'.repeat(64);
const nativeFetch = globalThis.fetch;
let posts = [];
let host = null;
let root = null;

beforeEach(() => {
  posts = [];
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  flushSync(() => root.unmount());
  host.remove();
  globalThis.fetch = nativeFetch;
});

afterAll(() => { GlobalRegistrator.unregister(); });

async function served(body){
  globalThis.fetch = async () => ({ok: true, status: 200, headers: {get: name => name === 'X-Question-Response-Digest' ? DIGEST : null}, json: async () => body});
  const response = await pollChat({id: 'chat_job', deadlineAt: Date.now() + 60000});
  globalThis.fetch = async (path, options) => { posts.push({path, body: JSON.parse(options.body)}); return {ok: true, status: 202, json: async () => ({})}; };
  return response;
}

function Turn({turn}){
  useTurnDelivery(turn);
  return <p>{turn.pending ? 'waiting' : turn.content}</p>;
}

function render(turn){ flushSync(() => root.render(<Turn turn={turn} />)); }
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

test('holding a delivery sends nothing until the turn is committed', async () => {
  const response = await served({answer: 'Stored.', sources: [], lifecycle: {request_id: REQUEST_ID}});
  holdDelivery('turn-1', response);
  await settle();
  expect(posts).toEqual([]);
  render({role: 'assistant', turnId: 'turn-1', pending: true, content: ''});
  await settle();
  expect(posts).toEqual([]);
  render({role: 'assistant', turnId: 'turn-1', pending: false, content: 'Stored.'});
  await settle();
  expect(posts).toHaveLength(1);
  expect(posts[0].path).toBe('/api/internal/v2/question-delivery');
  expect(posts[0].body.event_type).toBe('rendered');
  expect(posts[0].body.response_digest).toBe(DIGEST);
  render({role: 'assistant', turnId: 'turn-1', pending: false, content: 'Stored.'});
  await settle();
  expect(posts).toHaveLength(1);
});

test('a structured reply the page withholds is reported as render_failed', async () => {
  const intelligence = {status: 'complete'};
  const response = await served({answer: 'Stored.', sources: [], intelligence, lifecycle: {request_id: REQUEST_ID}});
  holdDelivery('turn-2', response);
  render({role: 'assistant', turnId: 'turn-2', pending: false, content: 'Stored.', intelligence});
  await settle();
  expect(posts.map(post => post.body.event_type)).toEqual(['render_failed']);
});

test('a turn restored from storage with no held delivery reports nothing', async () => {
  render({role: 'assistant', turnId: 'turn-3', pending: false, content: 'Restored.'});
  await settle();
  expect(posts).toEqual([]);
});
