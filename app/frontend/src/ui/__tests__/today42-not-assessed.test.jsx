import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import fixture from './fixtures/today42.json';

GlobalRegistrator.register();
const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TodayPage42} = await import('../../today42.jsx');
const realFetch = globalThis.fetch;
let host, root;

beforeEach(() => {
  localStorage.setItem('pulse_passcode', 'test-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  flushSync(() => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
});
afterAll(() => GlobalRegistrator.unregister());

async function mount(audit){
  const body = JSON.parse(JSON.stringify(fixture));
  body.markets.find((market) => market.market === 'ZA').not_assessed = audit;
  globalThis.fetch = async (url) => ({ok: true, status: 200, json: async () =>
    String(url).startsWith('/api/today') ? body : {items: []}});
  flushSync(() => root.render(<TodayPage42 region="ZA" date="2026-09-30" />));
  for (let n = 0; n < 8; n++) await new Promise((resolve) => setTimeout(resolve, 0));
  return host.querySelector('[data-market="ZA"]');
}

test('unassessed topics have a separate disclosure saying checks did not run', async () => {
  const market = await mount({detect_run_id: 'detect-fixture', pool_limit: 90, judged_limit: 10, count: 1,
    items: [{item_id: 'outside', title: 'Outside pool topic', status: 'not_assessed',
      reason: 'outside_candidate_pool', reason_text: 'Outside the morning candidate pool; checks did not run.',
      sql_rank: 149, pool_rank: null, market_scope: 'global'}]});
  const section = market.querySelector('[data-not-assessed]');
  expect(section).not.toBeNull();
  expect(section.tagName).toBe('DETAILS');
  expect(section.textContent).toContain('Not assessed');
  expect(section.textContent).toContain('Outside pool topic');
  expect(section.textContent).toContain('checks did not run');
  expect(section.querySelectorAll('[data-held-row], [data-card]').length).toBe(0);
});

test('legacy missing audit remains unavailable instead of reporting zero', async () => {
  const market = await mount(null);
  const section = market.querySelector('[data-not-assessed]');
  expect(section).not.toBeNull();
  expect(section.textContent).toContain('Selection audit unavailable for this brief.');
  expect(section.textContent).not.toContain('No topics were left unassessed.');
});

test('a complete empty audit explicitly reports no omissions', async () => {
  const market = await mount({detect_run_id: 'detect-fixture', pool_limit: 90, judged_limit: 10, count: 0, items: []});
  expect(market.querySelector('[data-not-assessed]').textContent).toContain('No topics were left unassessed.');
});
