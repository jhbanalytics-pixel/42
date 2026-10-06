/* Design audit, 2 October 2026. Seeds states: a failed or empty read shows a
   short title and one plain sentence with Try again, and no strength legend,
   because a legend for cards that are not on screen is noise. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {clearCache} = await import('../../api.js');
const {SeedsPage} = await import('../../seeds.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;

const reply = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const settle = async () => { for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setTimeout(resolve, 0)); };

async function mount(answer){
  globalThis.fetch = async () => answer();
  flushSync(() => root.render(<SeedsPage session={0} onAuth={() => {}} />));
  await settle();
}

beforeEach(() => {
  clearCache();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
  globalThis.fetch = realFetch;
  clearCache();
});

afterAll(() => GlobalRegistrator.unregister());

test('a failed seed read says Seeds could not load, Try again in a moment, and shows no legend', async () => {
  await mount(() => reply(503, {error: 'upstream_unavailable', message: 'No.'}));
  expect(host.textContent).toContain('Seeds could not load');
  expect(host.textContent).toContain('Try again in a moment.');
  expect(host.textContent).not.toContain('This does not establish');
  expect(host.textContent).not.toContain('Seeds unavailable');
  expect([...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again')).toBe(true);
  expect(host.querySelector('.seed-key') !== null, 'no strength legend').toBe(false);
  expect(host.textContent).not.toContain('Strength, as our editors judge it');
});

test('an incomplete seed read reads as the same failure, with no legend', async () => {
  await mount(() => reply(200, {date: null, seeds: null}));
  expect(host.textContent).toContain('Seeds could not load');
  expect(host.querySelector('.seed-key') !== null, 'no strength legend').toBe(false);
});

/* Page port, 3 October 2026: an empty read is now 42's own empty queue
   (GET /api/seeds, status empty), which says so in its own words. */
test('an empty seed read shows no legend', async () => {
  await mount(() => reply(200, {market: 'ZA', market_name: 'South Africa', status: 'empty', message: 'Nothing is queued for South Africa yet.', query_ids: ['q_seeds_queue', 'q_seeds_results'], queue: null, results: null, lanes_about: [], notes: []}));
  expect(host.textContent).toContain('Nothing queued for South Africa yet');
  expect(host.querySelector('.seed-key') !== null, 'no strength legend').toBe(false);
});

/* Page data audit, 3 October 2026: f42-api has no /api/seeds, so its 404 is
   the app saying the route does not exist, not a read that may answer later. */
test('a service without the seeds route says Seeds is not in this version and offers Today, not Try again', async () => {
  await mount(() => reply(404, {error: 'not_found', message: 'No such API route.'}));
  expect(host.textContent).toContain('Seeds is not available yet');
  expect(host.textContent).toContain('Seeds are not served in this version, so no seed data was read.');
  expect(host.textContent).not.toContain('Seeds could not load');
  expect([...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again')).toBe(false);
  const today = [...host.querySelectorAll('a')].find((a) => a.textContent === 'Open Today');
  expect(today && today.getAttribute('href')).toBe('#/pulse');
  expect(host.querySelector('.seed-key') !== null, 'no strength legend').toBe(false);
});

test('any other 404 is still a failed read with Try again', async () => {
  await mount(() => reply(404, {error: 'not_found', message: 'Nothing here.'}));
  expect(host.textContent).toContain('Seeds could not load');
  expect([...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again')).toBe(true);
});
