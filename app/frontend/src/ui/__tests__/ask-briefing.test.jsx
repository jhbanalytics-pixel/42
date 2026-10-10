/* Ask as a briefing (wave 8): the answer a reader opens is the question, what
   is behind it, one confidence tag, what it rests on, the findings with their
   real posts, and the platforms read. The record is shaped like a live answer
   (three posts that share one hashtag line, four comments whose link is the
   parent video's, repeated gaps, a failed TikTok call, 44 steps that read as
   32 lines) with invented handles; it is the reader view the API serves
   (core/agent/answer_view.py), so claims that restated one fact are already
   one. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import repeatRecord from './fixtures/ask42_comments_repeat.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {AskPage} = await import('../../ask42.jsx');
const {ResearchLog, collapseSteps} = await import('../ResearchLog.jsx');
const evidence = await import('../../askEvidence.js');

const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const clone = (value) => JSON.parse(JSON.stringify(value));
const plain = (text) => String(text || '').replace(/[  ]/g, ' ').replace(/\s+/g, ' ').trim();
const INTERNAL = /[a-z]+\/[a-z]+\/[a-z]+|_comment_|stored evidence|Why: error|the what to watch next text|tiktok\/post/i;

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

async function open(record = clone(repeatRecord)){
  serve(record);
  await act(async () => root.render(<AskPage region="ZA" setRegion={() => {}} query={{follow: record.ask_id}} />));
  await until(() => host.querySelector('.ask42-answer'), 'the answer');
  return record;
}

const text = () => plain(host.textContent);
const all = (selector) => [...host.querySelectorAll(selector)];
const words = (node) => plain(node.textContent);

/* ---- 1. comments are comments ---- */

test('a comment is told from a post, names the post it sits under and never counts as a creator', () => {
  const comment = repeatRecord.answer.evidence.find((item) => item.id.includes('_comment_'));
  expect(evidence.isComment(comment)).toBe(true);
  expect(evidence.isComment(repeatRecord.answer.evidence[0])).toBe(false);
  expect(evidence.parentWords(comment)).toBe("@lerato_clips_22's post");
  expect(evidence.parentWords({id: 'instagram_comment_9', url: 'https://www.instagram.com/p/Cx1/'})).toBe('a post');
  expect(evidence.basisWords(repeatRecord.answer.evidence)).toBe('Answer based on 3 posts by 3 creators and 2 comments');
});

test('the post tiles hold the posts and the comments sit apart under What commenters said', async () => {
  await open();
  const cards = all('.ask42-finding');
  expect(cards.length).toBe(2);
  const tiles = all('.ask42-tile');
  expect(tiles.length).toBe(3);
  expect(tiles.map((tile) => words(tile.querySelector('.ask42-chip-handle')))).toEqual(['ngwenya.tales', 'lerato_clips_22', 'sipho.skits.07']);
  expect(text()).not.toContain('The posts');
  expect(all('.ask42-posts').length).toBe(0);
  const said = all('.ask42-commenters');
  expect(said.length).toBe(1);
  expect(words(said[0])).toContain('What commenters said');
  expect(words(said[0])).toContain("@lerato_clips_22's post");
  expect(words(said[0])).toContain('This clip is old news');
  expect(cards[1].contains(said[0])).toBe(true);
  expect(said[0].querySelector('a[href="https://www.tiktok.com/@lerato_clips_22/video/7701000000000000002"]')).not.toBeNull();
  for (const tile of tiles) expect(words(tile)).not.toContain('old news');
});

test('a comment source says it is a comment on the post, never a post by the commenter', async () => {
  await open();
  const chips = all('.ask42-commenters button[aria-label^="Source:"]');
  expect(chips.length).toBe(2);
  expect(chips[0].getAttribute('aria-label')).toContain("TikTok comment on @lerato_clips_22's post");
  expect(chips[0].getAttribute('aria-label')).not.toContain('post by pretty.zodwa');
  await act(async () => chips[0].click());
  await until(() => host.querySelector('.ask42-source-title'), 'the pinned comment');
  expect(words(host.querySelector('.ask42-source-title'))).toBe("TikTok comment on @lerato_clips_22's post");
});

/* ---- 2. one claim per fact (the API view), and the confidence tag ---- */

test('one confidence tag covers the answer and each card keeps its own label', async () => {
  await open();
  expect(all('.ask42-confidence-tag').map(words)).toEqual(['Single source']);
  expect(all('.ask42-finding .ask42-confidence').map((node) => words(node).replace(/^[^A-Za-z]+/, ''))).toEqual(['Single source', 'Single source']);
  const mixed = clone(repeatRecord);
  mixed.answer.claims[0].label = 'corroborated';
  expect(evidence.confidenceTag(mixed.answer.claims, {corroborated: {word: 'Corroborated'}, single_source: {word: 'Single source'}}).word).toBe('Mixed confidence');
  expect(evidence.confidenceTag([], {})).toBeNull();
});

/* ---- 3. each figure once ---- */

test('each distinct figure is one tile, however many claims state it', async () => {
  const record = clone(repeatRecord);
  record.answer.claims[1].numbers = clone(record.answer.claims[0].numbers).map((number) => ({...number, query_id: 'q_other'}));
  await open(record);
  const tiles = all('.ask42-figure-tile').map(words);
  expect(tiles).toEqual(['3 posts', '3 creators']);
  expect(evidence.distinctFigures(record.answer.claims).length).toBe(2);
});

/* ---- 4. one row per platform ---- */

test('the platforms read are one row each with their post counts, and a failed call is a note', async () => {
  await open();
  const rows = all('.ask42-platform-row').map((row) => words(row.querySelector('.ask42-platform-name')) + ' ' + words(row.querySelector('.ask42-platform-count')));
  expect(rows).toEqual(['TikTok 100', 'Instagram 40', 'YouTube 29']);
  expect(all('.ask42-platform-row .pl-logo, .ask42-platform-row .pl-monogram').length).toBe(3);
  expect(words(host.querySelector('.ask42-platform-notes'))).toBe('1 TikTok search failed');
});

/* ---- 5. nothing internal outside the technical details ---- */

test('the model usage banner and the internal lines live in Technical details', async () => {
  await open();
  expect(host.querySelector('.ask42-notices')).toBeNull();
  const technical = host.querySelector('.ask42-technical');
  expect(words(technical)).toContain('did not report what it cost');
  const outside = plain(clone_without(host, '.ask42-technical').textContent);
  expect(outside).not.toContain('did not report what it cost');
  expect(outside).not.toMatch(INTERNAL);
  expect(words(technical)).toContain('tiktok/post/transcript');
});

function clone_without(node, selector){
  const copy = node.cloneNode(true);
  copy.querySelectorAll(selector).forEach((part) => part.remove());
  return copy;
}

test('what we do not know shows at most three plain lines and the rest wait in Technical details', async () => {
  await open();
  const shown = all('.ask42-gaps li');
  expect(shown.length).toBe(3);
  expect(shown.map(words).join(' ')).not.toMatch(INTERNAL);
  expect(shown.map(words)).toContain('We left out some posts because they had no author name to credit');
  const rest = all('.ask42-technical .ask42-gaps-rest li').map(words);
  expect(rest).toContain("We could not read the video's spoken words this time");
  expect(new Set(shown.map(words)).size).toBe(3);
});

test('the follow-up questions name no source route', async () => {
  const record = clone(repeatRecord);
  await open(record);
  const chips = all('.ask42-next-question').map(words);
  expect(chips.length).toBe(2);
  expect(chips.join(' ')).not.toMatch(INTERNAL);
});

/* ---- 6. the scope line ---- */

test('the header says what the answer rests on before what was read', async () => {
  await open();
  expect(words(host.querySelector('.ask42-scope'))).toBe(
    'Answer based on 3 posts by 3 creators and 2 comments · 169 posts read on 8 platforms · 3 208 posts in the store for 4 to 10 October 2026');
  expect(words(host.querySelector('.ask42-meta'))).toBe('South Africa · posts from 4 to 10 October 2026');
});

/* ---- 7. the count and the list agree ---- */

test('the research summary counts the lines it lists', async () => {
  const record = await open();
  expect(record.steps.length).toBe(44);
  const log = host.querySelector('details.ask42-log');
  const count = Number(/· (\d+) steps?/.exec(words(log.querySelector('summary')))[1]);
  expect(count).toBe(log.querySelectorAll('ol > li').length);
  expect(count).toBe(32);
  expect(collapseSteps(record.steps).length).toBe(32);
});

/* ---- briefing order and the action bar ---- */

test('the sections come in the briefing order', async () => {
  await open();
  const order = ['.ask42-question', '.ask42-confidence-tag', '.ask42-scope', '.ask42-short', '.ask42-finding', '.ask42-figure-tile', '.ask42-platform-row', '.ask42-gaps', 'details.ask42-log', 'details.ask42-technical'];
  const nodes = order.map((selector) => host.querySelector(selector));
  nodes.forEach((node, index) => expect(node, order[index]).not.toBeNull());
  const position = (a, b) => Boolean(a.compareDocumentPosition(b) & window.Node.DOCUMENT_POSITION_FOLLOWING);
  for (let i = 0; i < nodes.length - 1; i += 1){
    if (['.ask42-figure-tile', '.ask42-platform-row'].includes(order[i]) && ['.ask42-platform-row', '.ask42-gaps'].includes(order[i + 1])) continue; // the rail sits beside the main column
    expect(position(nodes[i], nodes[i + 1]), order[i] + ' before ' + order[i + 1]).toBe(true);
  }
});

test('one action bar: Add to dossier leads, then follow-up, export and schedule, with saving last', async () => {
  await open();
  const bar = host.querySelector('.ask42-actions');
  expect(all('.ask42-actions button').map(words)).toEqual(['Add to dossier', 'Ask a follow-up', 'Export answer', 'Ask every Monday', 'Save checked claims to Findings']);
  expect(bar.querySelectorAll('.ask42-primary').length).toBe(1);
});

/* ---- the running view ---- */

const RUNNING_STEPS = Array.from({length: 44}, (_, index) => ({seq: index + 1, kind: index % 2 ? 'read' : 'search', text: 'Reading TikTok post ' + Math.floor(index / 2) + ' for its words', platform: 'tiktok', count: 1}));

test('while it runs the view shows the current step, the steps done and the clock, and folds the earlier steps', async () => {
  await act(async () => root.render(<ResearchLog steps={RUNNING_STEPS} running evidence={[]} claims={[]} market="ZA" />));
  expect(words(host.querySelector('.ask42-scan-now'))).toContain('Reading TikTok post 21 for its words');
  expect(words(host.querySelector('.ask42-scan-counts dd'))).toBe('43');
  expect(host.querySelector('.ask42-scan-clock')).not.toBeNull();
  const earlier = host.querySelector('details.ask42-scan-log');
  expect(earlier).not.toBeNull();
  expect(earlier.hasAttribute('open')).toBe(false);
  expect(words(earlier.querySelector('summary'))).toBe('Earlier steps · 21');
});

/* ---- lead review, 10 October: one label, and the rail at laptop width ---- */

test('a gap whose stored text already starts with a verb is not given a second "Searched"', async () => {
  await open();
  const rows = all('.ask42-gaps li').map(words);
  expect(rows.join(' ')).not.toMatch(/Searched (Searched|Inspected)/);
  expect(rows[0]).toContain('Inspected audio metadata associated with sound identifiers');
  expect(rows[0]).not.toContain('Searched Inspected');
  expect(rows[1]).toContain('Searched posts matching #funnyclip');
  expect(rows[1]).not.toContain('Searched Searched');
  const record = clone(repeatRecord);
  record.answer.gaps = [{what: 'No Instagram posts', searched: 'the posts found for this question', why: 'empty'}];
  await act(async () => root.unmount());
  root = createRoot(host);
  await open(record);
  expect(words(host.querySelector('.ask42-gaps li'))).toContain('Searched the posts found for this question');
});

test('the rail sits beside the findings from 880px of answer width and stacks below it', async () => {
  const {readFileSync} = await import('node:fs');
  const css = readFileSync(new URL('../../styles/askbrief42.css', import.meta.url), 'utf8');
  const rail = /@container \(min-width: (\d+)px\)\s*\{[^}]*\.ask42-answer-body \{[^}]*grid-template-areas: "lead rail" "rest rail"/.exec(css);
  expect(rail, 'a container rule that places the rail beside the lead').not.toBeNull();
  expect(Number(rail[1])).toBeLessThanOrEqual(880);
  expect(Number(rail[1])).toBeGreaterThan(820 - 175); // narrower than a tablet's answer column stays stacked
  expect(css).toMatch(/\.ask42-answer-body \{ grid-template-areas: "lead" "rail" "rest"; \}/);
});
