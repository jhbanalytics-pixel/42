/* Ask, a ranked briefing (wave 8): "what are people posting about most" is answered as a lead line, then one block per
   story, ranked, with its own figures, when it peaked or whether it is still rising, why it is moving, one or two real
   posts and a line for a brand marked as interpretation. The record is the reader view the API serves
   (core/agent/answer_view.py) of a replayed answer in the ranked shape; its posts and handles are invented. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import rankedRecord from './fixtures/ask42_ranked_briefing.json';
import repeatRecord from './fixtures/ask42_comments_repeat.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {AskPage} = await import('../../ask42.jsx');

const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/[  ]/g, ' ').replace(/\s+/g, ' ').trim();

let host = null;
let root = null;

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  window.history.replaceState(null, '', '#/');
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
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

function serve(record){
  const stub = async (url) => {
    const ok = new RegExp('^/api/ask/' + record.ask_id + '$').test(String(url));
    return {ok, status: ok ? 200 : 404, headers: {get: () => null}, json: async () => (ok ? record : {error: 'not_found'}), text: async () => ''};
  };
  globalThis.fetch = stub;
  window.fetch = stub;
}

async function until(check, label){
  const started = Date.now();
  for (;;){
    let ok = false;
    try { ok = Boolean(check()); } catch (_error){ ok = false; }
    if (ok) return;
    if (Date.now() - started > 4000) throw new Error('timed out waiting for ' + label);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
  }
}

async function open(record){
  serve(record);
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{follow: record.ask_id}} />));
  await until(() => host.querySelector('.ask42-answer'), 'the answer');
  return record;
}

const all = (selector) => [...host.querySelectorAll(selector)];
const words = (node) => plain(node.textContent);

test('the answer opens with the one line lead and then one block per story in rank order', async () => {
  await open(clone(rankedRecord));
  expect(words(host.querySelector('.ask42-short'))).toBe(rankedRecord.answer.short_answer);
  const stories = all('.ask42-story');
  expect(stories.length).toBe(3);
  expect(stories.map((s) => words(s.querySelector('.ask42-story-name')))).toEqual(['Soweto unrest', 'Pirates vs Sundowns cup final', 'Forbes and Motsoane murder trial']);
  expect(stories.map((s) => words(s.querySelector('.ask42-story-rank')))).toEqual(['1', '2', '3']);
  expect(host.querySelector('.ask42-stories').tagName).toBe('OL');
});

test('the figures sit inside each story, each with its unit and the window, never bare', async () => {
  await open(clone(rankedRecord));
  const [first, , third] = all('.ask42-story');
  expect(all('.ask42-story-figure').length).toBe(9);
  expect(all('.ask42-story-figure').every((node) => /^[\d,]+ [a-z]+$/.test(words(node)))).toBe(true);
  expect([...first.querySelectorAll('.ask42-story-figure')].map(words)).toEqual(['640 posts', '301 creators', '4 platforms']);
  expect([...third.querySelectorAll('.ask42-story-figure')].map(words)).toEqual(['288 posts', '146 creators', '3 platforms']);
  expect(words(first.querySelector('.ask42-story-window'))).toBe('5 to 11 October 2026');
});

test('the usual numbers section is replaced by the figures in the stories', async () => {
  await open(clone(rankedRecord));
  expect(host.querySelector('.ask42-figure-tiles')).toBeNull();
  expect(host.querySelector('#ask42-figures-title')).toBeNull();
  expect(host.querySelector('[data-ask-source-chart]')).not.toBeNull();  // the platforms read stay in the rail
});

test('a story carries its peak, its rising line and its reason as lines under the figures, and a story with none shows none', async () => {
  await open(clone(rankedRecord));
  const [unrest, cup, trial] = all('.ask42-story');
  expect([...unrest.querySelectorAll('.ask42-story-line')].map(words)).toEqual([
    'The biggest story of the week, with roadblocks, burning tyres and stopped taxis on the main routes, in 640 posts from 301 creators on 4 platforms.',
    'Posts peaked on 8 Oct at 152 in a day and had fallen to 60 by 11 Oct.',
    'Residents say it began after a fight over taxi routes and spread to the roadblocks overnight.']);
  expect(words(cup.querySelectorAll('.ask42-story-line')[1])).toBe('Posts about it rose from 38 on the first day of the week to 142 on the last, so it is still rising.');
  expect(trial.querySelectorAll('.ask42-story-line').length).toBe(1);
});

test('each story shows at most two real posts, and a post shows its words', async () => {
  await open(clone(rankedRecord));
  for (const story of all('.ask42-story')){
    const tiles = story.querySelectorAll('.ask42-tile');
    expect(tiles.length).toBeGreaterThan(0);
    expect(tiles.length).toBeLessThanOrEqual(2);
  }
  const first = all('.ask42-story')[0];
  expect(words(first)).toContain('burning tyres at the intersection');
  expect(first.querySelector('.ask42-tile-open').getAttribute('href')).toMatch(/^https:\/\//);
});

test('a so-what line is under its story and says it is interpretation', async () => {
  await open(clone(rankedRecord));
  const sowhats = all('.ask42-story-sowhat');
  expect(sowhats.length).toBe(3);
  expect(words(sowhats[2])).toContain('A brand should stay clear of the trial');
  expect(words(sowhats[0])).toMatch(/interpretation/i);
  expect(host.querySelector('#ask42-sowhat-title')).toBeNull();  // every line found its story
});

test('a claim that names no story stays as a finding under "Across the stories"', async () => {
  await open(clone(rankedRecord));
  const across = host.querySelector('.ask42-across');
  expect(words(across.querySelector('h3'))).toBe('Across the stories');
  expect(words(across)).toContain('The unrest and the trial are followed mostly on X and YouTube');
  expect(across.querySelectorAll('.ask42-finding').length).toBe(1);
});

test('the caption says what ranks the stories when the pinned figures confirm it', async () => {
  await open(clone(rankedRecord));
  expect(words(host.querySelector('.ask42-stories-caption'))).toBe('Ranked by creators, then posts, 5 to 11 October 2026');
});

test('an unconfirmed order shows no rank and says so', async () => {
  const record = clone(rankedRecord);
  record.run.briefing.ordered = false;
  await open(record);
  expect(all('.ask42-story-rank').length).toBe(0);
  expect(words(host.querySelector('.ask42-stories-caption'))).toBe('In the order 42 listed them; the counts do not confirm a ranking');
});

test('a story the answer does not bear out is not shown, and the ranks follow the stories that are', async () => {
  const record = clone(rankedRecord);
  record.answer.claims = record.answer.claims.filter((c) => c.id !== 'c7');
  record.answer.short_answer = 'Two stories led South Africa this week.';
  record.answer.so_what = record.answer.so_what.filter((s) => !s.claim_ids.includes('c7'));
  record.run.briefing.stories = record.run.briefing.stories.filter((s) => s.rank !== 3);
  await open(record);
  expect(all('.ask42-story').length).toBe(2);
  expect(host.textContent).not.toContain('Forbes');
});

test('with fewer than two stories the usual layout comes back', async () => {
  const record = clone(rankedRecord);
  delete record.run.briefing;
  await open(record);
  expect(host.querySelector('.ask42-stories')).toBeNull();
  expect(all('.ask42-finding').length).toBe(record.answer.claims.length);
  expect(host.querySelector('.ask42-figure-tiles')).not.toBeNull();
});

test('an answer in the old shape reads as before', async () => {
  await open(clone(repeatRecord));
  expect(host.querySelector('.ask42-stories')).toBeNull();
  expect(host.querySelector('.ask42-figure-tiles')).not.toBeNull();
});

test('a briefing that points at words the claims do not hold shows nothing of its own', async () => {
  const record = clone(rankedRecord);
  record.run.briefing.stories[0].name = 'Forged headline';
  record.run.briefing.stories[1].claim_ids = ['c4', 'c5', 'c6', 'c1'];
  await open(record);
  expect(host.textContent).not.toContain('Forged headline');
  expect(all('.ask42-story-name').map(words)).toEqual(['Pirates vs Sundowns cup final', 'Forbes and Motsoane murder trial']);
});
