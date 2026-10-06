/* A route chunk that fails to load, as after a deploy while a tab is open,
   keeps the shell: the boundary reloads the page once, and on a second
   failure soon after it shows the error with Reload and Return to Today
   instead of unmounting the whole app (live audit F05, 4 October 2026). */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React, {lazy, Suspense} from 'react';

GlobalRegistrator.register();
afterAll(() => GlobalRegistrator.unregister());

const {createRoot} = await import('react-dom/client');
const {RouteErrorBoundary, RELOAD_KEY, isChunkLoadError, reloadOnceForChunk} = await import('../../routeError.jsx');

const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
const CHUNK = new TypeError('Failed to fetch dynamically imported module: https://x/assets/ask42-C_hOnTTK.js');
let host = null;
let root = null;
const realError = console.error;

beforeEach(() => {
  window.sessionStorage.clear();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  console.error = () => {};
});

afterEach(() => {
  root.unmount();
  host.remove();
  console.error = realError;
});

function failingRoute(error){
  const Page = lazy(() => Promise.reject(error));
  return <Suspense fallback={<p>waiting</p>}><Page /></Suspense>;
}

test('chunk load errors are recognised and other errors are not', () => {
  expect(isChunkLoadError(CHUNK)).toBe(true);
  expect(isChunkLoadError(new Error('Importing a module script failed.'))).toBe(true);
  expect(isChunkLoadError(new Error('x is undefined'))).toBe(false);
});

test('a failed route chunk reloads the page once and keeps the surrounding shell', async () => {
  let reloads = 0;
  root.render(<main><nav>rail</nav><RouteErrorBoundary reload={() => { reloads += 1; }}>{failingRoute(CHUNK)}</RouteErrorBoundary></main>);
  await settle();
  expect(reloads).toBe(1);
  expect(host.textContent).toContain('rail');
  expect(host.textContent).toContain('reloading');
  expect(Number(window.sessionStorage.getItem(RELOAD_KEY))).toBeGreaterThan(0);
});

test('a second chunk failure soon after shows the error with Reload and Return to Today, not a loop', async () => {
  window.sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  let reloads = 0;
  root.render(<main><nav>rail</nav><RouteErrorBoundary reload={() => { reloads += 1; }}>{failingRoute(CHUNK)}</RouteErrorBoundary></main>);
  await settle();
  expect(reloads).toBe(0);
  expect(host.textContent).toContain('rail');
  expect(host.textContent).toContain('This page could not load');
  const labels = [...host.querySelectorAll('button')].map((b) => b.textContent.trim());
  expect(labels).toContain('Reload');
  expect(labels).toContain('Return to Today');
});

test('a render error that is not a chunk failure never reloads by itself', async () => {
  let reloads = 0;
  function Broken(){ throw new Error('x is undefined'); }
  root.render(<main><nav>rail</nav><RouteErrorBoundary reload={() => { reloads += 1; }}><Broken /></RouteErrorBoundary></main>);
  await settle();
  expect(reloads).toBe(0);
  expect(host.textContent).toContain('This page could not load');
  expect(host.textContent).toContain('rail');
});

test('the once-only guard holds across calls within thirty seconds', () => {
  let reloads = 0;
  expect(reloadOnceForChunk(100000, () => { reloads += 1; })).toBe(true);
  expect(reloadOnceForChunk(110000, () => { reloads += 1; })).toBe(false);
  expect(reloadOnceForChunk(140001, () => { reloads += 1; })).toBe(true);
  expect(reloads).toBe(2);
});

test('every route renders inside the boundary, so the rail survives a failed route', async () => {
  const source = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
  const layer = source.slice(source.indexOf('function RouteLayer('), source.indexOf('const DESK_OPTIONAL'));
  expect(layer).toContain('<RouteErrorBoundary>{children}</RouteErrorBoundary>');
});
