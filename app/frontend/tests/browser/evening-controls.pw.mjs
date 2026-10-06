import {expect, test} from '@playwright/test';

const ORIGIN = 'http://127.0.0.1:4199';
const FIXTURE_PASSCODE = 'evening-controls-fixture-only';
const CURSOR = '2026-09-30T08:00:00+02:00';

const FILTER_PAGE = {
  asks: [
    {ask_id: 'older-partial', question: 'The older partial answer', at: '2026-09-29T09:00:00+02:00', status: 'ok', answer_status: 'partial'},
    {ask_id: 'missing-answer', question: 'The answer status is missing', at: '2026-09-28T09:00:00+02:00', status: 'complete', answer_status: null},
    {ask_id: 'failed-complete', question: 'The failed run has a complete answer', at: '2026-10-01T08:00:00+02:00', status: 'failed', answer_status: 'complete'},
    {ask_id: 'newest-probe', question: 'The newest probe question has a complete answer', at: '2026-10-02T09:00:00+02:00', status: 'complete', answer_status: 'complete'},
    {ask_id: 'insufficient-answer', question: 'The insufficient answer', at: '2026-09-27T09:00:00+02:00', status: 'complete', answer_status: 'insufficient'},
    {ask_id: 'middle-complete', question: 'The middle complete answer', at: '2026-09-30T09:00:00+02:00', status: 'ok', answer_status: 'complete'},
    {ask_id: 'failed-no-answer', question: 'The failed run has no answer', at: '2026-09-26T09:00:00+02:00', status: 'failed', answer_status: null},
  ],
  next_before: null,
};

const PAGED_FIRST = {
  asks: [
    {ask_id: 'first-page', question: 'The first page complete answer', at: '2026-10-02T09:00:00+02:00', status: 'complete', answer_status: 'complete'},
  ],
  next_before: CURSOR,
};

const PAGED_OLDER = {
  asks: [
    {ask_id: 'older-partial', question: 'The older page partial answer', at: '2026-09-29T11:00:00+02:00', status: 'ok', answer_status: 'partial'},
    {ask_id: 'older-failed', question: 'The older page failed answer', at: '2026-09-29T08:00:00+02:00', status: 'failed', answer_status: null},
    {ask_id: 'older-insufficient', question: 'The older page insufficient answer', at: '2026-09-29T10:00:00+02:00', status: 'complete', answer_status: 'insufficient'},
    {ask_id: 'older-complete', question: 'The older page complete answer', at: '2026-09-29T12:00:00+02:00', status: 'complete', answer_status: 'complete'},
  ],
  next_before: null,
};

async function respond(route, entry, status, body){
  entry.status = status;
  await route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });
}

async function installFixtureBoundary(page, firstPage = FILTER_PAGE, olderPage = null){
  const state = {api: [], historyReads: [], unexpectedApi: [], blockedWrites: [], external: [], badCredentials: []};
  await page.addInitScript((passcode) => {
    localStorage.setItem('pulse_passcode', passcode);
    localStorage.setItem('pulse-region', 'ZA');
  }, FIXTURE_PASSCODE);
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== ORIGIN){
      state.external.push({url: url.origin + url.pathname, action: 'aborted'});
      await route.abort('blockedbyclient');
      return;
    }
    if (!url.pathname.startsWith('/api/')){
      await route.continue();
      return;
    }

    const method = request.method().toUpperCase();
    const entry = {method, path: url.pathname, search: url.search, status: null};
    state.api.push(entry);
    if (!['/api/health', '/api/auth/verify'].includes(url.pathname)
      && !request.headers()['x-passcode']) state.badCredentials.push(method + ' ' + url.pathname);

    if (url.pathname === '/api/health' && method === 'GET'){
      await respond(route, entry, 200, {ok: true, passcode: true, auth_mode: 'passcode', checks: {auth: 'ok'}});
      return;
    }
    if (url.pathname === '/api/auth/verify' && method === 'POST'){
      let body = null;
      try { body = request.postDataJSON(); } catch { body = null; }
      if (body?.passcode !== FIXTURE_PASSCODE) state.badCredentials.push('POST /api/auth/verify');
      await respond(route, entry, 200, {ok: true});
      return;
    }
    if (url.pathname === '/api/history/asks' && method === 'GET'){
      state.historyReads.push({search: url.search, before: url.searchParams.get('before')});
      const before = url.searchParams.get('before');
      if (before === null){
        await respond(route, entry, 200, firstPage);
        return;
      }
      if (before === CURSOR && olderPage){
        await respond(route, entry, 200, olderPage);
        return;
      }
    }
    if (url.pathname === '/api/history/briefs' && method === 'GET'){
      await respond(route, entry, 200, {dates: [], next_before: null});
      return;
    }

    const readFixtures = {
      '/api/desk': {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}},
      '/api/research/recent': {artifacts: []},
      '/api/v2/investigations/list': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false},
      '/api/v2/investigations/scopes': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null},
      '/api/investigations': {investigations: []},
      '/api/schedules': {schedules: []},
      '/api/watches': {watches: []},
      '/api/chat/coverage': {contract_version: 'general_question_coverage_v1', state: 'covered', window: {start: '2026-08-25', end: '2026-09-07'}, cutoff_date: '2026-09-07'},
    };
    if (method === 'GET' && Object.hasOwn(readFixtures, url.pathname)){
      await respond(route, entry, 200, readFixtures[url.pathname]);
      return;
    }

    if (method !== 'GET') state.blockedWrites.push(method + ' ' + url.pathname);
    state.unexpectedApi.push(method + ' ' + url.pathname + url.search);
    await respond(route, entry, 404, {error: 'fixture_not_found', message: 'No local API fixture matched.'});
  });
  return state;
}

async function expectBoundaryClean(state){
  expect(state.unexpectedApi).toEqual([]);
  expect(state.blockedWrites).toEqual([]);
  expect(state.badCredentials).toEqual([]);
  expect(state.external.every((request) => request.action === 'aborted')).toBe(true);
  expect(state.api.filter((request) => request.method !== 'GET' && request.path !== '/api/auth/verify')).toEqual([]);
}

function askRows(page){
  return page.locator('#main-content [data-section="asks"] .hi42-row');
}

function askQuestions(rows){
  return rows.locator('[data-question]').allTextContents();
}

test('desktop More opens its controlled panel and Escape closes it to the trigger', async ({page}) => {
  const state = await installFixtureBoundary(page);
  await page.goto('/#/history');

  const rail = page.locator('nav.rail42[data-rail-layout="rail"]');
  const more = rail.locator('.rail42-more-button');
  const panel = rail.locator('.rail42-sheet');
  const chevron = more.locator('.rail42-more-chevron');
  await expect(rail).toBeVisible();
  await expect(more).toHaveAttribute('aria-expanded', 'false');
  await expect(panel).toBeHidden();
  expect(await more.getAttribute('aria-controls')).toBe(await panel.getAttribute('id'));
  await expect(chevron).toBeVisible();
  await expect(chevron).toHaveAttribute('aria-hidden', 'true');
  expect(await chevron.evaluate((element) => element.tagName.toLowerCase())).toBe('span');

  await more.click();
  await expect(more).toHaveAttribute('aria-expanded', 'true');
  await expect(panel).toBeVisible();
  await expect(more).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(more).toHaveAttribute('aria-expanded', 'false');
  await expect(panel).toBeHidden();
  await expect(more).toBeFocused();
  await expectBoundaryClean(state);
});

test('History filters by answer status, sorts newest first, and restores the full list without another read', async ({page}) => {
  const state = await installFixtureBoundary(page);
  await page.goto('/#/history');

  const asks = page.locator('#main-content [data-section="asks"]');
  const rows = asks.locator('.hi42-row');
  await expect(rows).toHaveCount(4);
  expect(await askQuestions(rows)).toEqual([
    'The newest probe question has a complete answer',
    'The failed run has a complete answer',
    'The middle complete answer',
    'The older partial answer',
  ]);
  const completeAnswer = rows.filter({hasText: 'The newest probe question'}).getByRole('link', {name: 'Read answer', exact: true});
  await expect(completeAnswer).toHaveAttribute('href', '#/ask?follow=newest-probe');

  const showAll = asks.getByRole('button', {name: 'Show all', exact: true});
  await expect(showAll).toHaveAttribute('aria-pressed', 'false');
  await showAll.click();
  await expect(showAll).toHaveAttribute('aria-pressed', 'true');
  await expect(rows).toHaveCount(7);
  await expect(rows.filter({hasText: 'The insufficient answer'})).toBeVisible();
  await expect(rows.filter({hasText: 'The answer status is missing'})).toBeVisible();
  await expect(rows.filter({hasText: 'The failed run has no answer'})).toBeVisible();

  await showAll.click();
  await expect(showAll).toHaveAttribute('aria-pressed', 'false');
  await expect(rows).toHaveCount(4);
  expect(await askQuestions(rows)).toEqual([
    'The newest probe question has a complete answer',
    'The failed run has a complete answer',
    'The middle complete answer',
    'The older partial answer',
  ]);
  expect(state.historyReads).toHaveLength(1);
  await expectBoundaryClean(state);
});

test('History loads a mixed older page only on request and keeps its cursor without writes', async ({page}) => {
  const state = await installFixtureBoundary(page, PAGED_FIRST, PAGED_OLDER);
  await page.goto('/#/history');

  const asks = page.locator('#main-content [data-section="asks"]');
  const rows = asks.locator('.hi42-row');
  await expect(rows).toHaveCount(1);
  const older = asks.getByRole('button', {name: 'Show older', exact: true});
  await expect(older).toBeVisible();
  expect(state.historyReads).toHaveLength(1);
  expect(state.historyReads[0].before).toBeNull();

  await older.click();
  await expect(rows).toHaveCount(3);
  expect(await askQuestions(rows)).toEqual([
    'The first page complete answer',
    'The older page complete answer',
    'The older page partial answer',
  ]);
  expect(state.historyReads).toHaveLength(2);
  expect(state.historyReads[1].before).toBe(CURSOR);

  const showAll = asks.getByRole('button', {name: 'Show all', exact: true});
  await showAll.click();
  await expect(rows).toHaveCount(5);
  await expect(rows.filter({hasText: 'The older page insufficient answer'})).toBeVisible();
  await expect(rows.filter({hasText: 'The older page failed answer'})).toBeVisible();
  await showAll.click();
  await expect(rows).toHaveCount(3);
  expect(state.historyReads).toHaveLength(2);
  await expectBoundaryClean(state);
});
