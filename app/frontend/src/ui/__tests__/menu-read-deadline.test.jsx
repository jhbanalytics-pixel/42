/* Audit W24-02 and W28-05: a menu page whose read never answers must not sit
   on its loading line. Seeds, Seed path and Lexicon give their read a
   deadline, then show their error state with Try again; a late answer does
   not replace that state, and Try again sends a fresh read. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {clearCache, MENU_READ_TIMEOUT_MS} = await import('../../api.js');
const {SeedsPage} = await import('../../seeds.jsx');
const {SeedPathPage} = await import('../../seedpath.jsx');
const {LexiconPage} = await import('../../lexicon.jsx');

const realFetch = globalThis.fetch;
const nativeSetTimeout = globalThis.setTimeout;
let host = null;
let root = null;
let reads = [];
let held = [];

const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => nativeSetTimeout(resolve, 0)); };
const text = () => host.textContent.replace(/\s+/g, ' ');
const button = (label) => [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === label);

beforeEach(() => {
  clearCache();
  reads = [];
  held = [];
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay === MENU_READ_TIMEOUT_MS && delay > 0){
      held.push(() => callback(...args));
      return nativeSetTimeout(() => {}, 0);
    }
    return nativeSetTimeout(callback, delay, ...args);
  };
});

afterEach(() => {
  globalThis.setTimeout = nativeSetTimeout;
  if (root) flushSync(() => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
  globalThis.fetch = realFetch;
  clearCache();
});

afterAll(() => GlobalRegistrator.unregister());

const pages = [
  ['Seeds', '/api/seeds?market=ZA', () => <SeedsPage session={0} onAuth={() => {}} region="ZA" />, 'Loading available seeds', 'Seeds could not load'],
  ['Seed path', '/api/seed-path?keyword=amapiano&market=ZA', () => <SeedPathPage session={0} onAuth={() => {}} initialKeyword="amapiano" initialMarket="za" />, 'Reading the posts 42 collected', 'Could not trace this term'],
  ['Lexicon', '/api/lexicon?market=ZA', () => <LexiconPage region="ZA" session={0} onAuth={() => {}} />, null, 'The lexicon could not be read'],
];

for (const [name, path, page, loading, failed] of pages){
  test(name + ' leaves a read that never answers for Try again at its deadline', async () => {
    const late = [];
    globalThis.fetch = async (url) => {
      reads.push(String(url));
      if (String(url) === path) return new Promise((resolve) => { late.push(resolve); });
      return {ok: true, status: 200, json: async () => ({items: []})};
    };
    flushSync(() => root.render(page()));
    await settle();
    if (loading) expect(text()).toContain(loading);
    expect(reads.filter((url) => url === path)).toHaveLength(1);
    expect(MENU_READ_TIMEOUT_MS).toBeGreaterThan(0);
    expect(held.length).toBeGreaterThan(0);
    flushSync(() => { for (const fire of held.splice(0)) fire(); });
    await settle();
    expect(text()).toContain(failed);
    expect(button('Try again')).toBeDefined();
    late[0]({ok: true, status: 200, json: async () => ({})});
    await settle();
    expect(text()).toContain(failed);
    flushSync(() => button('Try again').dispatchEvent(new MouseEvent('click', {bubbles: true})));
    await settle();
    expect(reads.filter((url) => url === path)).toHaveLength(2);
  });
}
