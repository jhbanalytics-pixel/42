/* The three U01 journey steps the 23 Sept sweep did not show working: an
   insufficient evidence reply, the Briefing deep link, and a question outside
   the covered window. Each reply is built from the accepted response fixtures
   and shaped as the engine publishes it (general_question_execution.py and
   general_question_result.py), and every API path outside the fixture map is
   recorded and must stay empty, so a 404 fixture reply never passes as a
   render. */
import {expect, test} from '@playwright/test';
import {intelligenceFixture} from '../../src/ui/__tests__/fixtures/general-intelligence.js';
import {DESK_STATES, RUN} from './fixtures/42-capability-routes/_desk.js';
import {installFixtures, lensRosterFixture, questionCoverageFixture, shellFixtures} from './support/capability-harness.mjs';

const REQUEST = '00000000-0000-4000-8000-000000000001';
const QUESTION = 'Are repair tutorials rising in South Africa?';

/* The reply the worker publishes when retrieval succeeded and matched
   nothing: unavailable, no snapshot, the engine's one limitation, and the two
   codes it writes for an empty match, inside the covered window. */
function insufficientReply(){
  const intelligence = intelligenceFixture();
  Object.assign(intelligence, {status: 'unavailable', as_of: '2026-09-08T10:00:00Z', window: {start: '2026-08-25', end: '2026-09-07', closed: true}, snapshot_id: null, sections: [], claims: [], receipts: [], readings: [], limitations: ['No completed answer is available.'], missing_work: ['evidence_insufficient', 'no_matching_evidence'], review_required: false, ready_for_downstream: false});
  return {answer: 'No admissible evidence matched this request. The requested answer cannot be established from this read.', sources: [], error: true, reason: 'evidence_insufficient', intelligence};
}

/* The reply for a question whose resolved window runs past the covered
   dates: the snapshot builder refuses with coverage_incomplete, which the
   worker publishes behind the generic retrieval code. */
function outOfWindowReply(){
  const intelligence = intelligenceFixture();
  Object.assign(intelligence, {status: 'unavailable', as_of: '2026-09-23T10:00:00Z', window: {start: '2026-09-14', end: '2026-09-20', closed: true}, snapshot_id: null, sections: [], claims: [], receipts: [], readings: [], limitations: ['No completed answer is available.'], missing_work: ['retrieval_incomplete', 'coverage_incomplete'], review_required: false, ready_for_downstream: false});
  return {answer: 'I could not produce a supported answer for this request.', sources: [], error: true, reason: 'retrieval_incomplete', intelligence};
}

function storedDetail(reply, question){
  return {contract_version: 'general_question_detail_v1', request_id: REQUEST, observed_state: reply.intelligence.status, question, history: [], selected_market: 'za', requested_window: null, response: reply, window_from_plan: true, reserved_microusd: 100000, missing_work: []};
}

function askFixtures(reply, question){
  return {
    ...shellFixtures(),
    '/api/chat/coverage': questionCoverageFixture(),
    '/api/chat/lenses': lensRosterFixture(),
    '/api/chat/send': {job_id: 'job_journey_proof'},
    '/api/chat/status': reply,
    '/api/internal/v2/fieldwork/question': storedDetail(reply, question),
  };
}

async function askLive(page, question){
  await page.goto('/#/console?work=ask');
  const field = page.getByRole('textbox', {name: 'Your question'});
  await field.fill(question);
  await page.getByRole('button', {name: 'Send', exact: true}).click();
}

const focusIn = page => page.evaluate(() => {
  const node = document.activeElement;
  return {tag: node ? node.tagName : null, inWorkspace: Boolean(node && node.closest('#main-content'))};
});

test('an insufficient evidence reply reads as a plain answer with its limits and a way on, and survives reload', async ({page}) => {
  const reply = insufficientReply();
  const state = await installFixtures(page, askFixtures(reply, QUESTION));
  await askLive(page, QUESTION);
  const answer = page.locator('.general-reply[data-intelligence-state="unavailable"]');
  for (const round of ['live', 'reloaded']){
    await expect(page.getByRole('heading', {name: QUESTION, exact: true})).toBeVisible();
    await expect(answer.getByText('Not enough evidence', {exact: true})).toBeVisible();
    await expect(answer.getByText('There is not enough evidence to answer this question. Nothing in the records for these dates supports an answer, so none was generated.', {exact: true})).toBeVisible();
    await expect(answer).toContainText('Evidence from 25 Aug to 7 Sept 2026');
    await expect(answer.getByRole('heading', {name: 'Limits of this read', exact: true})).toBeVisible();
    await expect(answer.getByText('No completed answer is available.', {exact: true})).toBeVisible();
    await expect(answer.getByText('No admissible evidence matched this request. The requested answer cannot be established from this read.', {exact: true})).toBeVisible();
    for (const wrong of ['could not be completed from the admitted evidence and execution results', 'Reply unavailable', 'retrieval did not complete', 'evidence_insufficient', 'no_matching_evidence']) await expect(answer).not.toContainText(wrong);
    if (round === 'live'){
      /* Live, the follow-up field under the reply takes the next question. */
      const field = page.getByRole('textbox', {name: 'Ask a follow-up'});
      await field.fill('Which platforms carry repair talk?');
      await expect(page.getByRole('button', {name: 'Send', exact: true})).toBeEnabled();
      await field.fill('');
      await expect(page).toHaveURL(new RegExp('#/console\\?work=ask&request=' + REQUEST + '$'));
      await page.reload();
    }
  }
  /* Reopened from its address, the saved reply has no answer to follow up,
     and New question opens an empty question field by keyboard. */
  const fresh = page.getByRole('navigation', {name: 'Workbench'}).getByRole('button', {name: 'New question'});
  await fresh.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect(page.getByRole('textbox', {name: 'Your question'})).toHaveValue('');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
  expect(state.requested.filter(path => path === '/api/chat/send')).toHaveLength(1);
});

test('a question outside the covered window is refused naming the window with a way on, live and after reload', async ({page}) => {
  const question = 'What happened in South Africa between 14 and 20 September 2026?';
  const rephrase = 'What stood out in South Africa in the latest available week?';
  const state = await installFixtures(page, askFixtures(outOfWindowReply(), question));
  await page.goto('/#/console?work=ask');
  await expect(page.getByText('Covers South Africa, 25 Aug to 7 Sept 2026.', {exact: true})).toBeVisible();
  await askLive(page, question);
  for (const round of ['live', 'reloaded']){
    await expect(page.getByRole('heading', {name: question, exact: true})).toBeVisible();
    const note = page.getByRole('note');
    await expect(note.getByText('This question falls outside the dates 42 can answer. No answer was generated.', {exact: true})).toBeVisible();
    await expect(note.getByText('Ask covers 25 Aug to 7 Sept 2026.', {exact: true})).toBeVisible();
    for (const wrong of ['The evidence retrieval did not complete', 'could not be completed from the admitted evidence']) await expect(page.locator('#main-content')).not.toContainText(wrong);
    const way = note.getByRole('button', {name: 'Ask about the latest available week', exact: true});
    await way.focus();
    await expect(way).toBeFocused();
    await page.keyboard.press('Enter');
    if (round === 'live'){
      await expect(page.getByRole('textbox', {name: 'Ask a follow-up'})).toHaveValue(rephrase);
      await page.getByRole('textbox', {name: 'Ask a follow-up'}).fill('');
      await expect(page).toHaveURL(new RegExp('#/console\\?work=ask&request=' + REQUEST + '$'));
      await page.reload();
    }
  }
  /* Reopened from its address, the rephrase opens as a fresh question,
     filled in and not sent. */
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect(page.getByRole('textbox', {name: 'Your question'})).toHaveValue(rephrase);
  /* The rephrase fills the field once: leaving Ask and coming back opens an
     empty question. */
  await page.goto('/#/console');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toHaveCount(0);
  await page.goto('/#/console?work=ask');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toHaveValue('');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
  expect(state.requested.filter(path => path === '/api/chat/send')).toHaveLength(1);
});

const LEAD = 'Balcony mix';
const LEAD_ID = DESK_STATES.populated().dynamic_discovery.signals[0].signal.signal_id;

function briefingFixtures(deskState = 'populated', extra = {}){
  return {...shellFixtures(), '/api/desk': DESK_STATES[deskState](), ...extra};
}

test('a copied #/pulse link opens the Briefing on its lead and survives reload', async ({page}) => {
  const state = await installFixtures(page, briefingFixtures());
  await page.goto('/#/pulse');
  for (const round of ['cold', 'reloaded']){
    await expect(page).toHaveURL(/#\/pulse$/);
    await expect(page.getByRole('heading', {name: LEAD, exact: true})).toBeVisible();
    await expect(page.locator('#main-content')).toContainText('Run 12 Sept 2026.');
    await expect(page.locator('[data-run-date="' + RUN.signal_date + '"]')).toHaveCount(1);
    await expect(page.getByRole('link', {name: 'Open ' + LEAD, exact: true})).toHaveAttribute('href', '#/topic/' + LEAD_ID);
    if (round === 'cold') await page.reload();
  }
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

/* The lead's released signal. The topic route reads it from the same
   released run the Briefing renders, so no /api/topic fixture is installed:
   any read of it is an unexpected path and fails the case. */
const LEAD_SIGNAL = DESK_STATES.populated().dynamic_discovery.signals[0].signal;

async function expectLeadStory(page){
  const main = page.locator('#main-content');
  await expect(main.getByRole('heading', {level: 1, name: LEAD_SIGNAL.signal_name, exact: true})).toBeVisible();
  await expect(main.locator('[data-signal-id="' + LEAD_ID + '"][data-run-date="' + RUN.signal_date + '"]')).toHaveCount(1);
  await expect(main.getByText(LEAD_SIGNAL.why_now, {exact: true})).toBeVisible();
  for (const wrong of ['The topic could not load', 'No signal for this topic', 'This signal is not in the released run']) await expect(main).not.toContainText(wrong);
}

test('the lead action opens its topic route by keyboard, lands focus on a heading, and Back returns focus to the Briefing', async ({page}) => {
  const state = await installFixtures(page, briefingFixtures());
  await page.goto('/#/pulse');
  const open = page.getByRole('link', {name: 'Open ' + LEAD, exact: true});
  await open.focus();
  await expect(open).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(new RegExp('#/topic/' + LEAD_ID + '$'));
  await expect.poll(() => focusIn(page)).toEqual({tag: expect.stringMatching(/^H[12]$/), inWorkspace: true});
  await expect(page.getByRole('link', {name: 'Discover', exact: true}).first()).toBeVisible();
  /* The route shows the lead's released signal story, so a blank route
     cannot pass. */
  await expectLeadStory(page);
  await page.goBack();
  await expect(page).toHaveURL(/#\/pulse$/);
  await expect(page.getByRole('heading', {name: LEAD, exact: true})).toBeFocused();
  expect(state.requested.filter((path) => path.startsWith('/api/topic'))).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

/* The lead action opens #/topic/<signal id>, which reads the lead from the
   released run, so the reader meets the lead's story rather than "The topic
   could not load". The story survives reload. */
test('the lead action opens a page that shows the lead signal', async ({page}) => {
  const state = await installFixtures(page, briefingFixtures());
  await page.goto('/#/pulse');
  await page.getByRole('link', {name: 'Open ' + LEAD, exact: true}).click();
  await expect(page).toHaveURL(new RegExp('#/topic/' + LEAD_ID + '$'));
  await expectLeadStory(page);
  await page.reload();
  await expect(page).toHaveURL(new RegExp('#/topic/' + LEAD_ID + '$'));
  await expectLeadStory(page);
  expect(state.requested.filter((path) => path.startsWith('/api/topic'))).toEqual([]);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('an empty Briefing opens Discover by keyboard, and both links survive reload', async ({page}) => {
  const state = await installFixtures(page, briefingFixtures('empty'));
  await page.goto('/#/pulse');
  await expect(page.getByText('No signals this run', {exact: false}).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText('No signals this run', {exact: false}).first()).toBeVisible();
  const discover = page.locator('#main-content').getByRole('button', {name: 'Open Discover', exact: true});
  await discover.focus();
  await expect(discover).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/explore$/);
  await expect.poll(() => focusIn(page)).toEqual({tag: expect.stringMatching(/^H[12]$/), inWorkspace: true});
  await page.reload();
  await expect(page).toHaveURL(/#\/explore$/);
  await expect(page.getByText('No signals this run', {exact: false}).first()).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/#\/pulse$/);
  await expect(page.getByText('No signals this run', {exact: false}).first()).toBeVisible();
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});
