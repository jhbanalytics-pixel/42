import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {intelligenceFixture} from '../../src/ui/__tests__/fixtures/general-intelligence.js';
import {sourceLabInventory} from '../../src/ui/__tests__/fixtures/source-lab-inventory.js';
import {lensRosterFixture, questionCoverageFixture} from './support/capability-harness.mjs';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/42-first-journey.json', import.meta.url), 'utf8'));
const historyFixture = JSON.parse(readFileSync(new URL('../../src/ui/__tests__/fixtures/history42.json', import.meta.url), 'utf8'));
const id = '00000000-0000-4000-8000-000000000001';
const requestHash = '#/console?work=ask&request=' + id;

/* The shell reads these on every route. A path outside the map is recorded
   and fails the test, so a 404 fixture reply can never pass as a render. */
function shellFixtures(){
  return {
    '/api/health': {passcode: true},
    '/api/auth/verify': {ok: true},
    '/api/desk': {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}},
    '/api/research/recent': {artifacts: []},
    '/api/v2/investigations/list': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false},
    '/api/v2/investigations/scopes': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null},
    '/api/chat/coverage': questionCoverageFixture(),
  };
}

function unconfirmedDetail(){
  return {contract_version: 'general_question_detail_v1', request_id: id, observed_state: 'unconfirmed', question: 'Exact retained question', history: [], selected_market: 'za', requested_window: null, response: null, window_from_plan: false, reserved_microusd: 100000, missing_work: ['execution_unconfirmed']};
}

function acceptedDetail(intelligence){
  return {contract_version: 'general_question_detail_v1', request_id: id, observed_state: intelligence.status, question: 'Exact retained question', history: [], selected_market: 'za', requested_window: null, response: {answer: 'Stored answer', sources: [], intelligence}, window_from_plan: true, reserved_microusd: 100000, missing_work: []};
}

async function setup(page, {question, extra = {}}){
  const requested = [], unexpected = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    localStorage.setItem('pulse-region', 'ZA');
  });
  const fixtures = {
    ...shellFixtures(),
    '/api/internal/v2/fieldwork/read': fixture.fieldwork,
    '/api/v2/source-lab': sourceLabInventory({measured: 2, catalogue: 3}),
    '/api/internal/v2/fieldwork/question': question,
    '/api/chat/lenses': lensRosterFixture(),
    ...extra,
  };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    requested.push(path);
    if (!(path in fixtures)){ unexpected.push(path); return route.fulfill({status: 404, json: {detail: 'Missing fixture'}}); }
    await route.fulfill({status: 200, json: fixtures[path]});
  });
  return {requested, unexpected, errors};
}

const focusedHeading = page => page.evaluate(() => {
  const node = document.activeElement;
  return {tag: node ? node.tagName : null, inWorkspace: Boolean(node && node.closest('#main-content'))};
});

/* Page port, 3 October 2026: a stored question link
   (#/console?work=ask&request=<id>) names an older record, and the older
   console that read it is gone from this version, so the link now opens
   History, where 42's own saved asks are listed (src/legacyRoutes.js). These
   two journeys hold that: the link lands on History, which reads its asks
   and lists the saved answers, cold and after a reload, and the older
   stored question endpoint is never read.
   Was: stored question survives cold reload. */
const ANSWERED_ASK = historyFixture.asks.asks.find((ask) => ['complete', 'partial'].includes(ask.answer_status)).question;
test('a stored question link opens History, which survives cold reload', async ({page}) => {
  const id = '00000000-0000-4000-8000-000000000001';
  const requested = [];
  await page.addInitScript(() => localStorage.setItem('pulse_passcode', 'browser-fixture-only'));
  const fixtures = {
    '/api/health': {passcode: true},
    '/api/auth/verify': {ok: true},
    '/api/desk': {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}},
    '/api/research/recent': {artifacts: []},
    '/api/v2/investigations/list': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false},
    '/api/v2/investigations/scopes': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null},
    '/api/chat/coverage': questionCoverageFixture(),
    '/api/internal/v2/fieldwork/question': {contract_version: 'general_question_detail_v1', request_id: id, observed_state: 'unconfirmed', question: 'Exact retained question', history: [], selected_market: 'za', requested_window: null, response: null, window_from_plan: false, reserved_microusd: 100000, missing_work: ['execution_unconfirmed']},
    '/api/history/asks': historyFixture.asks,
  };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    requested.push(path);
    await route.fulfill({status: path in fixtures ? 200 : 404,
                         json: fixtures[path] || {detail: 'Missing fixture'}});
  });
  await page.goto('/#/console?work=ask&request=' + id);
  await expect(page).toHaveURL(/#\/history$/);
  await expect(page.locator('#main-content h1')).toHaveText('History');
  await expect(page.getByText(ANSWERED_ASK, {exact: true})).toBeVisible();
  await page.reload();
  await expect(page).toHaveURL(/#\/history$/);
  await expect(page.locator('#main-content h1')).toHaveText('History');
  await expect(page.getByText(ANSWERED_ASK, {exact: true})).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Exact retained question', exact: true})).toHaveCount(0);
  expect(requested.filter(path => !(path in fixtures))).toEqual([]);
  expect(requested.filter(path => path === '/api/history/asks').length).toBeGreaterThanOrEqual(2);
  expect(requested).not.toContain('/api/internal/v2/fieldwork/question');
});

/* Quiet register, 23 Sept 2026: a saved answer on screen while the rail says
   "Recent questions: None yet." contradicted itself, and the page offered two
   ways back, one in its head and one in the answer. The rail now lists the
   open saved question as the current item, and the page head carries the one
   link back to Fieldwork. */
/* Page port, 3 October 2026: restated as above. The saved answers are
   History's asks list, each with its own way to the answer, and the page
   offers no older console rail or Back to Briefing.
   Was: a saved answer heads the recent questions and the page offers one way back. */
test('a stored question link opens History, which lists saved answers each with one way to read them', async ({page}) => {
  const id = '00000000-0000-4000-8000-000000000001';
  await page.addInitScript(() => localStorage.setItem('pulse_passcode', 'browser-fixture-only'));
  const fixtures = {
    '/api/health': {passcode: true},
    '/api/auth/verify': {ok: true},
    '/api/desk': {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}},
    '/api/research/recent': {artifacts: []},
    '/api/v2/investigations/list': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false},
    '/api/v2/investigations/scopes': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null},
    '/api/chat/coverage': questionCoverageFixture(),
    '/api/internal/v2/fieldwork/question': {contract_version: 'general_question_detail_v1', request_id: id, observed_state: 'unconfirmed', question: 'Exact retained question', history: [], selected_market: 'za', requested_window: null, response: null, window_from_plan: false, reserved_microusd: 100000, missing_work: ['execution_unconfirmed']},
    '/api/history/asks': historyFixture.asks,
  };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    await route.fulfill({status: path in fixtures ? 200 : 404, json: fixtures[path] || {detail: 'Missing fixture'}});
  });
  await page.goto('/#/console?work=ask&request=' + id);
  await expect(page).toHaveURL(/#\/history$/);
  await expect(page.getByRole('tab', {name: 'Asks', exact: true})).toHaveAttribute('aria-selected', 'true');
  const row = page.locator('#main-content li').filter({hasText: ANSWERED_ASK});
  await expect(row).toHaveCount(1);
  await expect(row.getByRole('link', {name: 'Read answer', exact: true})).toHaveCount(1);
  await expect(page.getByRole('navigation', {name: 'Workbench'})).toHaveCount(0);
  await expect(page.getByRole('heading', {name: 'Exact retained question', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Back to Briefing', exact: true})).toHaveCount(0);
});

test('Fieldwork Open question opens the stored request by keyboard and returns to a focused heading', async ({page}) => {
  const state = await setup(page, {question: unconfirmedDetail()});
  await page.goto('/#/fieldwork');
  const open = page.getByRole('link', {name: 'Open question', exact: true});
  await expect(open).toHaveAttribute('href', requestHash);
  await expect(page.getByRole('heading', {name: 'Retained question operation', exact: true})).toBeVisible();
  await open.focus();
  await expect(open).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(new RegExp('#/console\\?work=ask&request=' + id + '$'));
  await expect(page.getByRole('heading', {name: 'Exact retained question', exact: true})).toBeVisible();
  await expect(page.getByText('Execution unconfirmed. No validated terminal answer is available.', {exact: true})).toBeVisible();
  await expect.poll(() => focusedHeading(page)).toEqual({tag: expect.stringMatching(/^H[12]$/), inWorkspace: true});
  const back = page.getByRole('link', {name: 'Back to Fieldwork', exact: true});
  await expect(back).toHaveAttribute('href', '#/fieldwork');
  await back.focus();
  await expect(back).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/fieldwork$/);
  await expect(page.getByRole('heading', {name: 'Source and research operations', exact: true})).toBeFocused();
  await expect(page.getByRole('link', {name: 'Open question', exact: true})).toHaveAttribute('href', requestHash);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
  expect(state.requested.filter(path => path === '/api/internal/v2/fieldwork/question')).toHaveLength(1);
});

/* Ask redesign, 23 Sept 2026: the follow-up is asked from the "Ask a
   follow-up" field under the stored answer. Sending it opens the continued
   thread in the live console and asks the new question once, with the stored
   turns as its history; the stored question itself is never sent again. The
   stored request's state now sits in the Request and usage details
   disclosure. */
test('an accepted stored response renders its citations and asks a follow-up without resubmitting the stored question', async ({page}) => {
  const intelligence = intelligenceFixture();
  const state = await setup(page, {question: acceptedDetail(intelligence), extra: {'/api/chat/send': {job_id: 'job_follow_up'}, '/api/chat/status': {pending: true}}});
  const sends = [];
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/chat/send') sends.push(request.postDataJSON()); });
  await page.goto('/' + requestHash);
  await expect(page.getByRole('heading', {name: 'Exact retained question', exact: true})).toBeVisible();
  await page.getByText('Request and usage details', {exact: true}).click();
  await expect(page.getByText('Stored response: ' + intelligence.status + '.', {exact: true})).toBeVisible();
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toBeVisible();
  await expect(page.getByText('Stored answer', {exact: true})).toHaveCount(0);
  await page.getByRole('button', {name: /^Open R1:/}).click();
  await expect(page.getByRole('link', {name: 'View original source'})).toHaveAttribute('href', intelligence.receipts[0].url);
  await expect(page.getByText(intelligence.receipts[0].excerpt, {exact: true})).toBeVisible();
  await expect(page).toHaveURL(new RegExp('#/console\\?work=ask&request=' + id + '$'));
  expect(state.requested).not.toContain('/api/chat/send');
  await page.getByRole('textbox', {name: 'Ask a follow-up'}).fill('Which source came first?');
  await page.getByRole('button', {name: 'Send', exact: true}).click();
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect(page.getByText(intelligence.claims[0].text, {exact: true})).toBeVisible();
  await expect(page.getByText('Which source came first?', {exact: true})).toBeVisible();
  await expect(page.getByRole('textbox', {name: 'Ask a follow-up'})).toBeVisible();
  const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-chat')));
  expect(saved).toHaveLength(1);
  expect(saved[0].messages.find(turn => turn.intelligence)?.intelligence.request_id).toBe(id);
  await expect.poll(() => sends.length).toBe(1);
  expect(sends[0].message).toBe('Which source came first?');
  expect(sends[0].parent_request_id).toBe(id);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

/* A follow-up asked from a saved answer is sent once. Leaving Ask and coming
   back remounts the live console on the continued thread, and that remount
   must open the thread without asking its follow-up a second time. */
async function sendFollowUpFromSavedAnswer(page){
  const intelligence = intelligenceFixture();
  const state = await setup(page, {question: acceptedDetail(intelligence), extra: {'/api/chat/send': {job_id: 'job_follow_up'}, '/api/chat/status': {pending: true}}});
  const sends = [];
  page.on('request', request => { if (new URL(request.url()).pathname === '/api/chat/send') sends.push(request.postDataJSON()); });
  await page.goto('/' + requestHash);
  await page.getByRole('textbox', {name: 'Ask a follow-up'}).fill('Which source came first?');
  await page.getByRole('button', {name: 'Send', exact: true}).click();
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect(page.getByText('Which source came first?', {exact: true})).toBeVisible();
  await expect.poll(() => sends.length).toBe(1);
  return {state, sends};
}

async function expectOneFollowUpSend(page, {state, sends}){
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect(page.getByRole('textbox', {name: 'Ask a follow-up'})).toBeVisible();
  /* Give a repeated send time to leave the page before counting. */
  await page.waitForTimeout(1000);
  expect(sends).toHaveLength(1);
  expect(sends[0].message).toBe('Which source came first?');
  expect(sends[0].parent_request_id).toBe(id);
  await expect(page.getByRole('heading', {name: 'Which source came first?', exact: true})).toHaveCount(1);
  expect(state.errors).toEqual([]);
}

test('a follow-up from a saved answer is not sent again after switching to Build brief and back to Ask', async ({page}) => {
  const run = await sendFollowUpFromSavedAnswer(page);
  const rail = page.getByRole('navigation', {name: 'Workbench'});
  await rail.getByRole('button', {name: 'Build brief', exact: true}).click();
  await expect(page).toHaveURL(/#\/console\?work=brief/);
  await rail.getByRole('button', {name: 'Ask', exact: true}).click();
  await expectOneFollowUpSend(page, run);
});

test('a follow-up from a saved answer is not sent again after browser Back then Forward', async ({page}) => {
  const run = await sendFollowUpFromSavedAnswer(page);
  await page.goBack();
  await expect(page).toHaveURL(new RegExp('#/console\\?work=ask&request=' + id + '$'));
  await expect(page.getByRole('heading', {name: 'Exact retained question', exact: true})).toBeVisible();
  await page.goForward();
  await expectOneFollowUpSend(page, run);
});

/* Ask redesign, 23 Sept 2026: Send is the one primary action on the Ask
   composer. With a question in the field it is the red plane, on the first
   question and on the follow-up. With the field empty it is disabled and
   looks it: a filled neutral plane with the not-allowed cursor, never the red
   plane and never an outline like the Lens select beside it. */
const sendLook = send => send.evaluate(node => {
  const probe = document.createElement('span');
  probe.style.backgroundColor = 'var(--color-daylight-red-surface)';
  document.body.append(probe);
  const red = getComputedStyle(probe).backgroundColor;
  probe.remove();
  const style = getComputedStyle(node);
  return {background: style.backgroundColor, cursor: style.cursor, disabled: node.disabled, red};
});

async function expectSendStates(page, field){
  const send = page.getByRole('button', {name: 'Send', exact: true});
  await expect(field).toHaveValue('');
  await expect(send).toBeDisabled();
  const empty = await sendLook(send);
  expect(empty.red).not.toBe('rgba(0, 0, 0, 0)');
  expect(empty.background).not.toBe(empty.red);
  expect(empty.background).not.toBe('rgba(0, 0, 0, 0)');
  expect(empty.cursor).toBe('not-allowed');
  await field.fill('Which source came first?');
  await expect(send).toBeEnabled();
  const ready = await sendLook(send);
  expect(ready.background).toBe(ready.red);
  expect(ready.cursor).toBe('pointer');
}

test('Send is the red primary plane with a question and looks disabled when the field is empty, on both composers', async ({page}) => {
  const state = await setup(page, {question: acceptedDetail(intelligenceFixture())});
  await page.goto('/#/console?work=ask');
  await expectSendStates(page, page.getByRole('textbox', {name: 'Your question'}));
  await page.goto('/' + requestHash);
  await expectSendStates(page, page.getByRole('textbox', {name: 'Ask a follow-up'}));
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

/* The follow-up field reads as a follow-up: its example is not the first
   question's example again. */
test('the follow-up composer offers its own example, not the first question example', async ({page}) => {
  const state = await setup(page, {question: acceptedDetail(intelligenceFixture()), extra: {'/api/chat/send': {job_id: 'job_follow_up'}, '/api/chat/status': {pending: true}}});
  await page.goto('/#/console?work=ask');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toHaveAttribute('placeholder', 'For example: are repair tutorials rising in South Africa?');
  await page.goto('/' + requestHash);
  const stored = page.getByRole('textbox', {name: 'Ask a follow-up'});
  await expect(stored).toHaveAttribute('placeholder', 'For example: which platforms carry most of this?');
  await stored.fill('Which source came first?');
  await page.getByRole('button', {name: 'Send', exact: true}).click();
  await expect(page).toHaveURL(/#\/console\?work=ask$/);
  await expect(page.getByRole('button', {name: 'Stop waiting', exact: true})).toBeVisible();
  await expect(page.getByRole('textbox', {name: 'Ask a follow-up'})).toHaveAttribute('placeholder', 'Answering…');
  await page.getByRole('button', {name: 'Stop waiting', exact: true}).click();
  await expect(page.getByRole('textbox', {name: 'Ask a follow-up'})).toHaveAttribute('placeholder', 'For example: which platforms carry most of this?');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});
