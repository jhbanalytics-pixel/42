import {expect, test} from 'bun:test';
import {saveThreads, loadThreads} from '../../chat.jsx';

test('history trimming retains every unresolved admitted request', () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  let saved;
  Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: {setItem(_key, value){ saved = value; }, getItem(){ return saved; }}});
  try {
    const threads = Array.from({length: 25}, (_, index) => ({id: 't' + index, ts: index, messages: [{role: 'assistant', turnId: 'a' + index, content: '', pending: true, job: {id: 'job' + index, deadlineAt: 180000}}]}));
    expect(saveThreads(threads)).toBe(true);
    const loaded = loadThreads();
    expect(loaded).toHaveLength(25);
    expect(loaded.find(thread => thread.id === 't0').messages[0]).toMatchObject({job: {id: 'job0'}, pending: false, statusUncertain: true});
  } finally {
    if (original) Object.defineProperty(globalThis, 'localStorage', original); else delete globalThis.localStorage;
  }
});
