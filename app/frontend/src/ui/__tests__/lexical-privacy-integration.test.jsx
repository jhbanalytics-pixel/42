import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import producer from './fixtures/lexical-privacy-producer.json';

GlobalRegistrator.register();
const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {clearCache} = await import('../../api.js');
const {LexiconPage} = await import('../../lexicon.jsx');
const {SeedPathPage, validSeedPathPayload} = await import('../../seedpath.jsx');
const realFetch = globalThis.fetch;
let host, root;
beforeEach(() => {
  clearCache();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  flushSync(() => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
  clearCache();
});
afterAll(() => GlobalRegistrator.unregister());
async function mount(body, page) {
  globalThis.fetch = async () => new Response(JSON.stringify(body), {status: 200, headers: {'Content-Type': 'application/json'}});
  flushSync(() => root.render(page));
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}
test('the actual withheld Lexicon response never presents recording absence', async () => {
  await mount(producer.lexicon_withheld, <LexiconPage region="ZA" session={0} onAuth={() => {}} />);
  expect(host.querySelector('[data-state-frame="empty"]')).not.toBeNull();
  expect(host.textContent).toContain('No words or hashtags available to show');
  expect(host.textContent).toContain(producer.lexicon_withheld.message);
  expect(host.textContent).not.toContain('No words or hashtags recorded here yet');
  expect(host.textContent).not.toContain('hiddenfixture');
});
test('the actual masked Seed path response renders its unavailable reason', async () => {
  expect(validSeedPathPayload(producer.seedpath_withheld, 'hiddenfixture', 'za')).toBe(true);
  await mount(producer.seedpath_withheld, <SeedPathPage initialKeyword="hiddenfixture" initialMarket="ZA" session={0} onAuth={() => {}} />);
  expect(host.textContent).toContain('People view unavailable.');
  expect(host.textContent).not.toContain('incomplete or was for another term');
  expect([...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again')).toBe(false);
  expect(host.querySelector('.sp-result')).toBeNull();
});
test('masked phrases are accepted only as an empty privacy unavailable result in the requested market', () => {
  const body = producer.seedpath_phrase_withheld;
  expect(validSeedPathPayload(body, 'dance hiddenfixture', 'za')).toBe(true);
  for (const bad of [{...body, market: 'NG'}, {...body, status: 'ok'}, {...body, message: 'Other wait'}, {...body, keyword: 'different'}, {...body, topics: [{item_id: 'not-empty'}]}]) {
    expect(validSeedPathPayload(bad, 'dance hiddenfixture', 'za')).toBe(false);
  }
});
test('successful producer answers still require the exact term and market', () => {
  const body = producer.seedpath_success;
  expect(validSeedPathPayload(body, 'amapiano', 'za')).toBe(true);
  expect(validSeedPathPayload(body, 'different', 'za')).toBe(false);
  expect(validSeedPathPayload(body, 'amapiano', 'ng')).toBe(false);
});
