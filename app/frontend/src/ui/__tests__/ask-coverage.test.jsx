import {expect, test} from 'bun:test';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import {GeneralIntelligence} from '../GeneralIntelligence.jsx';
import {askCoverage, latestWeekQuestion, outsideCoverage} from '../../askPresentation.js';
import {intelligenceFixture} from './fixtures/general-intelligence.js';
import {storedReplyFixture} from './fixtures/stored-reply-60a5fd02.js';

const served = {contract_version: 'general_question_coverage_v1', state: 'covered', window: {start: '2026-08-25', end: '2026-09-07'}, cutoff_date: '2026-09-07'};
const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const text = (html) => html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ');

function refusal(){
  const source = intelligenceFixture();
  Object.assign(source, {status: 'unavailable', as_of: '2026-09-23T10:00:00Z', window: {start: '2026-09-14', end: '2026-09-20', closed: true}, snapshot_id: null, sections: [], claims: [], receipts: [], readings: []});
  source.limitations = ['No completed answer is available.'];
  source.missing_work = ['retrieval_incomplete', 'coverage_incomplete'];
  return source;
}

test('the served coverage is read only from a covered answer the page can parse', () => {
  expect(askCoverage(served)).toEqual({start: '2026-08-25', end: '2026-09-07'});
  expect(askCoverage({...served, state: 'unavailable', window: null, cutoff_date: null})).toBeNull();
  expect(askCoverage({...served, contract_version: 'general_question_coverage_v2'})).toBeNull();
  expect(askCoverage({...served, window: {start: '2026-09-08', end: '2026-09-07'}})).toBeNull();
  expect(askCoverage({...served, window: {start: '2026-02-30', end: '2026-09-07'}})).toBeNull();
  expect(askCoverage({...served, cutoff_date: '2026-09-08'})).toBeNull();
  expect(askCoverage(null)).toBeNull();
});

test('a refusal is about dates only when the resolved window runs outside the covered dates', () => {
  const coverage = askCoverage(served);
  expect(outsideCoverage(refusal(), coverage)).toBe(true);
  expect(outsideCoverage(refusal(), null)).toBe(false);
  const inside = refusal(); inside.window = {start: '2026-08-25', end: '2026-09-07', closed: true};
  expect(outsideCoverage(inside, coverage)).toBe(false);
  const earlier = refusal(); earlier.window = {start: '2026-08-20', end: '2026-09-01', closed: true};
  expect(outsideCoverage(earlier, coverage)).toBe(true);
  const edge = refusal(); edge.window = {start: '2026-08-25', end: '2026-09-01', closed: true};
  expect(outsideCoverage(edge, coverage)).toBe(false);
  const other = refusal(); other.missing_work = ['retrieval_incomplete'];
  expect(outsideCoverage(other, coverage)).toBe(false);
});

test('a coverage refusal names the covered dates and offers the latest available week', () => {
  const source = refusal();
  const before = JSON.stringify(source);
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source, coverage: askCoverage(served), onRephrase: () => {}}));
  const shown = text(html);
  expect(shown).toContain('This question falls outside the dates 42 can answer.');
  expect(shown).toContain('Ask covers 25 Aug to 7 Sept 2026.');
  expect(shown).toContain('Ask about the latest available week');
  expect(shown).not.toContain('The evidence retrieval did not complete');
  expect(shown).not.toContain('This request could not be completed from the admitted evidence');
  expect(shown).toContain('The available evidence does not cover the requested dates or scope.');
  expect(shown).toContain('No completed answer is available.');
  expect(JSON.stringify(source)).toBe(before);
});

test('without readable coverage the refusal keeps the engine wording and names no dates', () => {
  const html = text(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: refusal(), coverage: null})));
  expect(html).toContain('The evidence retrieval did not complete.');
  expect(html).not.toContain('Ask covers');
});

test('the rephrase names the reply markets and never a date', () => {
  expect(latestWeekQuestion(['za'])).toBe('What stood out in South Africa in the latest available week?');
  expect(latestWeekQuestion(['za', 'ng', 'ke'])).toBe('What stood out across South Africa, Nigeria and Kenya in the latest available week?');
  expect(latestWeekQuestion([])).toBe('What stood out in the latest available week?');
});

/* Ask redesign, 23 Sept 2026: the question heads the answer as the h2, so the
   answer's own sections are h3, and dates read without a leading zero. */
function limits(html){
  const section = html.split('<h3>Limits of this read</h3>')[1].split('</section>')[0];
  return [...section.matchAll(/<p>([^<]*)<\/p>/g)].map((match) => match[1].replace(/&#x27;/g, "'"));
}

test('a first question hides the parent continuity line and each limit shows once', () => {
  const source = storedReplyFixture();
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source, firstQuestion: true}));
  const listed = limits(html);
  expect(listed.some((line) => line.startsWith('Authenticated parent continuity'))).toBe(false);
  expect(listed.some((line) => line.startsWith('Coverage counts name their units'))).toBe(false);
  expect(listed.some((line) => line.startsWith('Cited evidence names its units: 24 collected records'))).toBe(true);
  expect(listed).not.toContain('This evidence does not establish population-level rates or market-wide trends.');
  expect(listed).not.toContain('Source record only; prevalence and representativeness are not measured.');
  expect(new Set(listed).size).toBe(listed.length);
  const page = text(html);
  for (const line of source.limitations){
    if (line.startsWith('Authenticated parent continuity') || line.startsWith('Coverage counts name their units')) continue;
    expect(page).toContain(line);
  }
});

test('a follow-up keeps the parent continuity line', () => {
  const listed = limits(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: storedReplyFixture()})));
  expect(listed).toContain('Authenticated parent continuity was not supplied; prior conversation prose is context only.');
});

test('differing admitted and cited counts both stay', () => {
  const source = storedReplyFixture();
  source.limitations = source.limitations.map((line) => line.startsWith('Coverage counts') ? line.replace('24 collected records', '30 collected records') : line);
  const listed = limits(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source, firstQuestion: true})));
  expect(listed.some((line) => line.startsWith('Coverage counts name their units: 30 collected records'))).toBe(true);
  expect(listed.some((line) => line.startsWith('Cited evidence names its units'))).toBe(true);
});

test('the Ask page reads its dates from the coverage route and suggests no calendar week', () => {
  const chat = read('../../chat.jsx');
  expect(chat).toContain('COVERAGE_PATH');
  expect(chat).not.toContain('this week?');
  expect(chat).not.toMatch(/20\d\d-\d\d-\d\d/);
});

test('only the reply to the first question of a thread counts as a first question', async () => {
  const {answersFirstQuestion} = await import('../../chat.jsx');
  const thread = [{role: 'user', content: 'First'}, {role: 'assistant', content: 'One'}, {role: 'user', content: 'Second'}, {role: 'assistant', content: 'Two'}];
  expect(answersFirstQuestion(thread, 1)).toBe(true);
  expect(answersFirstQuestion(thread, 3)).toBe(false);
  const stored = [{role: 'user', content: 'Earlier'}, {role: 'assistant', content: 'Earlier reply'}, {role: 'user', content: 'Stored'}, {role: 'assistant', content: 'Stored reply'}];
  expect(answersFirstQuestion(stored, 3)).toBe(false);
  expect(answersFirstQuestion([{role: 'user', content: 'Stored'}, {role: 'assistant', content: 'Reply'}], 1)).toBe(true);
});

test('a stored reply written before any plan names no observation window', () => {
  const source = refusal();
  Object.assign(source, {window: {start: '2026-09-09', end: '2026-09-22', closed: true}, missing_work: ['model_timeout']});
  /* Round three, 24 Sept 2026: the redesign names the answer's window in its
     meta line as the dates its evidence comes from, in readable dates, and an
     unresolved one as "Window unresolved", so the words are restated to that
     line while the behaviour stays: no plan, no named window.
     Round three, 24 Sept 2026: "Window" and "unresolved" are system words
     beside a page head that says "Covers" and a meta line that says
     "Evidence from", so an unsettled window reads "Dates not settled". */
  const unplanned = text(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source, windowFromPlan: false})));
  expect(unplanned).toContain('Dates not settled');
  expect(unplanned).not.toContain('Window unresolved');
  expect(unplanned).not.toContain('Evidence from');
  expect(unplanned).not.toContain('9 to 22 Sept 2026');
  expect(unplanned).not.toContain('Observation window: closed');
  const planned = text(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source})));
  expect(planned).toContain('Evidence from 9 to 22 Sept 2026');
  expect(planned).not.toContain('Dates not settled');
});
