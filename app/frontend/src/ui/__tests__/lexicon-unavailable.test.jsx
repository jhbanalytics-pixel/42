/* Page data audit, 3 October 2026: f42-api serves no /api/desk, so the
   lexicon's read ends in the app's "No such API route." 404 there. The page
   says it is not in this version and offers Today rather than Try again; any
   other failed read still offers Try again. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {clearCache} = await import('../../api.js');
const {LexiconPage} = await import('../../lexicon.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;

const reply = (status, body) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const settle = async () => { for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setTimeout(resolve, 0)); };

async function mount(answer){
  globalThis.fetch = async () => answer();
  flushSync(() => root.render(<LexiconPage region="ZA" session={0} onAuth={() => {}} />));
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

test('a service without the desk route says the lexicon is not in this version and offers Today', async () => {
  await mount(() => reply(404, {error: 'not_found', message: 'No such API route.'}));
  expect(host.querySelector('[data-state-frame="unavailable"]') !== null).toBe(true);
  expect(host.textContent).toContain('The lexicon is not available yet');
  expect(host.textContent).toContain('The lexicon is not served in this version, so no term counts were read.');
  expect(host.textContent).not.toContain('The lexicon could not be read');
  expect([...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again')).toBe(false);
  const today = [...host.querySelectorAll('button')].find((b) => b.textContent === 'Open Today');
  expect(Boolean(today)).toBe(true);
  expect(host.textContent).not.toContain('This action is not available.');
  window.location.hash = '#/lexicon';
  today.click();
  expect(window.location.hash).toBe('#/pulse');
});

test('any other failed desk read is still the error frame with Try again', async () => {
  await mount(() => reply(503, {error: 'upstream_unavailable', message: 'No.'}));
  expect(host.querySelector('[data-state-frame="error"]') !== null).toBe(true);
  expect(host.textContent).toContain('The lexicon could not be read');
  expect(host.textContent).not.toContain('not available yet');
  expect([...host.querySelectorAll('button')].some((b) => b.textContent === 'Try again')).toBe(true);
});

/* The state frame takes a link only as a path (/x) or an https URL, so a hash
   link was drawn as "This action is not available." Open Discover on an empty
   read is a button that moves the hash instead. */
test('an empty read offers a working Open Discover', async () => {
  await mount(() => reply(200, {lexicon: [], freshness: null}));
  expect(host.querySelector('[data-state-frame="empty"]') !== null).toBe(true);
  expect(host.textContent).not.toContain('This action is not available.');
  const discover = [...host.querySelectorAll('button')].find((b) => b.textContent === 'Open Discover');
  expect(Boolean(discover)).toBe(true);
  window.location.hash = '#/lexicon';
  discover.click();
  expect(window.location.hash).toBe('#/explore');
});
