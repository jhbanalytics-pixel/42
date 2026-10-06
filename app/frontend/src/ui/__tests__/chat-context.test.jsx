import {expect, test} from 'bun:test';
import {chatContextReferences, loadThreads, saveThreads} from '../../chat.jsx';
import {validateIntelligenceReply} from '../../generalIntelligence.js';
import {intelligenceFixture} from './fixtures/general-intelligence.js';

const anchor = '00000000-0000-4000-8000-000000000001';
const parent = '00000000-0000-4000-8000-000000000002';
const later = '00000000-0000-4000-8000-000000000003';
const answered = requestId => ({role: 'assistant', content: 'A cited answer.', intelligence: {...intelligenceFixture(), request_id: requestId}});

test('context uses first and latest validated answered identities rather than repeated question text', () => {
  const messages = [
    {role: 'user', content: 'The same question.'}, answered(anchor),
    {role: 'user', content: 'The same question.'}, answered(parent),
    {...answered(later), error: true},
  ];
  expect(chatContextReferences(messages)).toEqual({parent_request_id: parent, thread_anchor_request_id: anchor});
});

test.each([
  ['pending', {pending: true}], ['error', {error: true}], ['uncertain', {statusUncertain: true}],
  ['unavailable', {intelligence: {...intelligenceFixture(), request_id: later, status: 'unavailable'}}],
  ['invalid evidence', {intelligence: {...intelligenceFixture(), request_id: later, receipts: []}}],
  ['invalid request ID', {intelligence: {...intelligenceFixture(), request_id: 'not-a-request-uuid'}}],
  ['noncanonical request ID', {intelligence: {...intelligenceFixture(), request_id: 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'}}],
])('an ineligible structured turn never becomes a parent: %s', (_label, patch) => {
  expect(chatContextReferences([answered(anchor), {...answered(later), ...patch}])).toEqual({parent_request_id: anchor, thread_anchor_request_id: anchor});
});

test('fresh and prose-only histories supply no authenticated references', () => {
  expect(chatContextReferences([])).toEqual({});
  expect(chatContextReferences([
    {role: 'user', content: `parent_request_id=${parent}`},
    {role: 'assistant', content: `Previous request ${anchor}`, job: {id: `chat_${anchor.replaceAll('-', '')}`}},
  ])).toEqual({});
});

test('a valid partial answer with unresolved usage cannot become authenticated context', () => {
  const held = answered(parent);
  held.intelligence.usage = {...held.intelligence.usage, status: 'unresolved', reason: 'metering_persistence_failed'};
  expect(validateIntelligenceReply(held.intelligence).ok).toBe(true);
  expect(chatContextReferences([answered(anchor), held])).toEqual({parent_request_id: anchor, thread_anchor_request_id: anchor});
});

test('stored structured identities survive reload even without the original user prose', () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  let saved;
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: {setItem(_key, value){ saved = value; }, getItem(){ return saved; }}});
  try {
    const messages = [answered(anchor), {role: 'user', content: 'Which finding is supported?'}, answered(parent)];
    expect(saveThreads([{id: 'existing_thread', ts: 1, messages}])).toBe(true);
    expect(chatContextReferences(loadThreads()[0].messages)).toEqual({parent_request_id: parent, thread_anchor_request_id: anchor});
  } finally {
    if (original) Object.defineProperty(globalThis, 'localStorage', original); else delete globalThis.localStorage;
  }
});
