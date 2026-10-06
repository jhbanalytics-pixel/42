/* Page port, 3 October 2026: an older topic or creator link names a desk
   record that f42-api cannot read. The page says so in plain words and offers
   the 42 page that holds the same job, and it reads nothing. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {OlderLink42} = await import('../../olderLink42.jsx');

const realFetch = globalThis.fetch;
let host = null;
let root = null;
let calls = 0;

beforeEach(() => {
  calls = 0;
  globalThis.fetch = async () => { calls += 1; return new Response('{}'); };
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
});

afterAll(() => GlobalRegistrator.unregister());

test('an older topic link says it cannot open here and offers Discover', () => {
  flushSync(() => root.render(<OlderLink42 kind="topic" />));
  expect(host.querySelector('h1').textContent).toBe('This topic link is from an older version');
  const links = [...host.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')]);
  expect(links).toContainEqual(['Open Discover', '#/explore']);
  expect(calls).toBe(0);
});

test('an older creator link offers Communities', () => {
  flushSync(() => root.render(<OlderLink42 kind="creator" />));
  expect(host.querySelector('h1').textContent).toBe('This creator link is from an older version');
  const links = [...host.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')]);
  expect(links).toContainEqual(['Open Communities', '#/communities']);
  expect(calls).toBe(0);
});
