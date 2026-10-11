/* The ranked briefing, read back by the page. core/agent/briefing.py works the groups out of the claims on every read
   and puts ids and indexes in run.briefing; the page does not trust it. It takes a story's name from the claim's own
   words, a figure from the claim's own number and a so-what line from the answer, and it drops anything the answer
   does not bear out. Fewer than two stories left means the usual layout. */
import {expect, test} from 'bun:test';
import {storyView} from '../../askStories.js';

const pin = {run_id: 'r_1', result_hash: 'sha256:' + 'a'.repeat(64)};
const number = (value, unit, query = 'q_1') => ({value, unit, query_id: query, ...pin});
const claim = (id, text, extra = {}) => ({id, text, label: 'corroborated', kind: 'observation', evidence_ids: ['p1'], quotes: [], numbers: [], ...extra});

function record(overrides = {}){
  const claims = [
    claim('c1', 'Soweto unrest: the biggest story of the week, in 640 posts from 301 creators on 4 platforms.', {numbers: [number(640, 'posts'), number(301, 'creators'), number(4, 'platforms')]}),
    claim('c2', 'Soweto unrest: posts peaked on 8 Oct at 152 in a day.', {numbers: [number(152, 'posts', 'q_day')]}),
    claim('c3', 'Cup final: the match fans are counting down to, in 512 posts from 233 creators on 1 platform.', {numbers: [number(512, 'posts', 'q_2'), number(233, 'creators', 'q_2'), number(1, 'platforms', 'q_2')]}),
    claim('c4', 'Everything here is on TikTok.'),
  ];
  const briefing = {
    kind: 'ranked_stories', ordered: true, other_claim_ids: ['c4'],
    stories: [
      {rank: 1, name: 'Soweto unrest', claim_ids: ['c1', 'c2'], size: {claim_id: 'c1', figures: [{unit: 'posts', index: 0}, {unit: 'creators', index: 1}, {unit: 'platforms', index: 2}]}, so_what: [0]},
      {rank: 2, name: 'Cup final', claim_ids: ['c3'], size: {claim_id: 'c3', figures: [{unit: 'posts', index: 0}, {unit: 'creators', index: 1}, {unit: 'platforms', index: 2}]}, so_what: [1]},
    ],
  };
  const base = {
    answer: {status: 'complete', short_answer: 'Two stories led.', claims, evidence: [],
      so_what: [{text: 'Pause posts near the routes.', claim_ids: ['c1', 'c2']}, {text: 'Plan for the whistle.', claim_ids: ['c3']}], watch_next: [], gaps: []},
    run: {briefing},
  };
  return {...base, ...overrides};
}

test('a record with two stories reads as a ranked briefing', () => {
  const view = storyView(record());
  expect(view.ordered).toBe(true);
  expect(view.stories.map((s) => [s.rank, s.name, s.lines.map((l) => l.claim.id)])).toEqual([[1, 'Soweto unrest', ['c1', 'c2']], [2, 'Cup final', ['c3']]]);
  expect(view.others.map((c) => c.id)).toEqual(['c4']);
  expect(view.restSoWhat).toEqual([]);
});

test('a figure is the claim\'s own number, with its unit, in the reader\'s words', () => {
  const [first, second] = storyView(record()).stories;
  expect(first.figures.map((f) => f.value + ' ' + f.unit)).toEqual(['640 posts', '301 creators', '4 platforms']);
  expect(first.figures.every((f) => f.queryId === 'q_1')).toBe(true);
  expect(second.figures.map((f) => f.value + ' ' + f.unit)).toEqual(['512 posts', '233 creators', '1 platform']);
});

test('the story line is the claim after its name, capitalised, and nothing else is changed', () => {
  const [first] = storyView(record()).stories;
  expect(first.lines[0].body).toBe('The biggest story of the week, in 640 posts from 301 creators on 4 platforms.');
  expect(first.lines[1].body).toBe('Posts peaked on 8 Oct at 152 in a day.');
});

test('a so-what line joins its story and is named by index', () => {
  const [first, second] = storyView(record()).stories;
  expect(first.soWhat).toEqual([{index: 0, text: 'Pause posts near the routes.'}]);
  expect(second.soWhat).toEqual([{index: 1, text: 'Plan for the whistle.'}]);
});

test('a record with no briefing, or an unordered briefing with one story, is the usual layout', () => {
  expect(storyView({answer: record().answer, run: {}})).toBeNull();
  expect(storyView({answer: record().answer})).toBeNull();
  expect(storyView(null)).toBeNull();
  const one = record();
  one.run.briefing.stories = one.run.briefing.stories.slice(0, 1);
  expect(storyView(one)).toBeNull();
});

test('a briefing of another kind is not read', () => {
  const other = record();
  other.run.briefing.kind = 'something_else';
  expect(storyView(other)).toBeNull();
});

test('the ranks come from the page, not the briefing: an unordered briefing shows no rank', () => {
  const unordered = record();
  unordered.run.briefing.ordered = false;
  const view = storyView(unordered);
  expect(view.ordered).toBe(false);
  expect(view.stories.map((s) => s.rank)).toEqual([null, null]);
});

test('a claim whose own words do not start with the story name is not part of the story', () => {
  const forged = record();
  forged.answer.claims[1].text = 'Posts peaked on 8 Oct at 152 in a day.';
  const view = storyView(forged);
  expect(view.stories[0].lines.map((l) => l.claim.id)).toEqual(['c1']);
  expect(view.others.map((c) => c.id)).toEqual(['c2', 'c4']);
});

test('a story with no claim left is dropped, and so is the briefing when one story remains', () => {
  const forged = record();
  forged.run.briefing.stories[1].claim_ids = ['c99'];
  expect(storyView(forged)).toBeNull();
});

test('a claim the briefing names twice, or in no story of the answer, is not shown twice', () => {
  const twice = record();
  twice.run.briefing.stories[1].claim_ids = ['c3', 'c1'];
  const view = storyView(twice);
  expect(view.stories[0].lines.map((l) => l.claim.id)).toEqual(['c1', 'c2']);
  expect(view.stories[1].lines.map((l) => l.claim.id)).toEqual(['c3']);
});

test('a figure whose index or unit the claim does not bear out is left off', () => {
  const forged = record();
  forged.run.briefing.stories[0].size.figures = [{unit: 'posts', index: 0}, {unit: 'creators', index: 7}, {unit: 'platforms', index: 1}];
  expect(storyView(forged).stories[0].figures.map((f) => f.unit)).toEqual(['posts']);
});

test('a figure that is not a pinned number is left off', () => {
  const forged = record();
  forged.answer.claims[0].numbers[0] = {value: '640', unit: 'posts', query_id: 'q_1'};
  expect(storyView(forged).stories[0].figures.map((f) => f.unit)).toEqual(['creators', 'platforms']);
  forged.answer.claims[0].numbers[1] = {value: 301, unit: 'creators', query_id: ''};
  expect(storyView(forged).stories[0].figures.map((f) => f.unit)).toEqual(['platforms']);
});

test('a size claim that is not one of the story\'s own claims gives no figures', () => {
  const forged = record();
  forged.run.briefing.stories[0].size.claim_id = 'c3';
  expect(storyView(forged).stories[0].figures).toEqual([]);
});

test('a so-what line that names a claim outside the story, or no claim, stays in the general list', () => {
  const forged = record();
  forged.answer.so_what[0].claim_ids = ['c1', 'c3'];
  forged.run.briefing.stories[0].so_what = [0, 5];
  const view = storyView(forged);
  expect(view.stories[0].soWhat).toEqual([]);
  expect(view.restSoWhat).toEqual([{index: 0, text: 'Pause posts near the routes.'}]);
});

test('a so-what line the briefing does not attach stays in the general list', () => {
  const loose = record();
  loose.run.briefing.stories[1].so_what = [];
  expect(storyView(loose).restSoWhat).toEqual([{index: 1, text: 'Plan for the whistle.'}]);
});

test('the claims that are in no story are the answer\'s own, in its order', () => {
  const extra = record();
  extra.answer.claims.push(claim('c5', 'A second loose claim.'));
  expect(storyView(extra).others.map((c) => c.id)).toEqual(['c4', 'c5']);
});

test('the input is not changed', () => {
  const input = record();
  const before = JSON.stringify(input);
  storyView(input);
  expect(JSON.stringify(input)).toBe(before);
});
