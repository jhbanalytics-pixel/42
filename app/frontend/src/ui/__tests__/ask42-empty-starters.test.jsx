/* The empty Ask page (Albert, 2 October 2026: a narrow column beside a dead
   area). With nothing asked yet it offers starting points: the questions for
   the trends 42 is following in the chosen market, and a few plain examples.
   Choosing one fills the question and asks nothing, since asking spends
   credits; the reader still presses Ask. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

GlobalRegistrator.register();
const {createRoot} = await import('react-dom/client');
const {act} = React;
const {AskPage} = await import('../../ask42.jsx');

const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
let host = null;
let root = null;
let calls = [];

afterAll(() => GlobalRegistrator.unregister());
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  calls = [];
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
});

const json = (body, status = 200) => ({ok: status < 400, status, headers: {get: () => null}, json: async () => body, text: async () => JSON.stringify(body)});
function serve(discover){
  const fake = async (url, options = {}) => {
    calls.push({url: String(url), method: options.method || 'GET'});
    if (String(url).startsWith('/api/discover')) return discover ? json(discover) : json({error: 'down'}, 503);
    return json({error: 'not found'}, 404);
  };
  globalThis.fetch = fake;
  window.fetch = fake;
}
const settle = async () => { for (let i = 0; i < 6; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };

const ITEMS = [
  {item_id: 'a', title: '#fixture_za_step', ask: 'What is behind #fixture_za_step in South Africa this week?', state_word: 'Emerging', market: 'ZA'},
  {item_id: 'b', title: '#fixture_za_heritage', ask: 'What is behind #fixture_za_heritage in South Africa this week?', state_word: 'Recurring', market: 'ZA'},
];

test('an empty Ask offers the questions for the trends 42 is following, and examples', async () => {
  serve({items: ITEMS});
  await act(async () => root.render(<AskPage region="ZA" query={{}} />));
  await settle();
  const starters = host.querySelector('[data-ask-starters]');
  expect(starters).not.toBeNull();
  const trendButtons = [...starters.querySelectorAll('[data-ask-starter="trend"]')];
  expect(trendButtons.map((b) => b.textContent)).toEqual([
    expect.stringContaining('What is behind #fixture_za_step in South Africa this week?'),
    expect.stringContaining('What is behind #fixture_za_heritage in South Africa this week?'),
  ]);
  expect(starters.querySelectorAll('[data-ask-starter="example"]').length).toBeGreaterThanOrEqual(3);
  expect(calls.some((c) => c.url.startsWith('/api/discover') && c.url.includes('market=ZA'))).toBe(true);
});

test('choosing a starter fills the question and asks nothing', async () => {
  serve({items: ITEMS});
  await act(async () => root.render(<AskPage region="ZA" query={{}} />));
  await settle();
  const button = host.querySelector('[data-ask-starter="trend"]');
  await act(async () => button.click());
  await settle();
  expect(host.querySelector('#ask42-question').value).toBe('What is behind #fixture_za_step in South Africa this week?');
  expect(calls.filter((c) => c.method === 'POST')).toEqual([]);
  expect(document.activeElement && document.activeElement.id).toBe('ask42-question');
});

test('when the trends cannot load the examples still show and no error is raised', async () => {
  serve(null);
  await act(async () => root.render(<AskPage region="ZA" query={{}} />));
  await settle();
  const starters = host.querySelector('[data-ask-starters]');
  expect(starters.querySelectorAll('[data-ask-starter="trend"]').length).toBe(0);
  expect(starters.querySelectorAll('[data-ask-starter="example"]').length).toBeGreaterThanOrEqual(3);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});

test('a reopened answer or a question from a link shows no starters', async () => {
  serve({items: ITEMS});
  await act(async () => root.render(<AskPage region="ZA" query={{q: 'What is behind it?', draft: '1'}} />));
  await settle();
  expect(host.querySelector('[data-ask-starters]')).toBeNull();
});

test('on a wide screen the composer and the starters stand side by side', () => {
  const css = readFileSync(fileURLToPath(new URL('../../styles/ask42.css', import.meta.url)), 'utf8');
  expect(css).toMatch(/@media \(min-width: 1024px\)\s*\{[^}]*\.ask42-start[^{]*\{[^}]*grid-template-columns/);
});
