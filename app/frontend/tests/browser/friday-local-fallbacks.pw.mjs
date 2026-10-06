import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';

const ORIGIN = 'http://127.0.0.1:4199';
const YESTERDAY = '2026-09-30';
const FIXTURE_PASSCODE = 'friday-fixture-only';
const readFixture = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));
const today = readFixture('../../src/ui/__tests__/fixtures/today42.json');
const savedAsk = readFixture('../../src/ui/__tests__/fixtures/ask42_complete.json');

function historyAsks(){
  return {
    asks: [{
      ask_id: savedAsk.ask_id,
      question: savedAsk.question,
      at: savedAsk.created_at,
      status: savedAsk.status,
      answer_status: savedAsk.answer.status,
    }],
    next_before: null,
  };
}

async function respond(route, entry, status, body){
  entry.status = status;
  await route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });
}

async function installFixtureBoundary(page){
  const state = {api: [], unexpectedApi: [], blockedWrites: [], eventStreams: [], external: [], badCredentials: []};
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
    if (url.pathname === '/api/today' && method === 'GET' && url.searchParams.get('date') === YESTERDAY){
      await respond(route, entry, 200, today);
      return;
    }
    if (url.pathname === '/api/alerts' && method === 'GET'){
      await respond(route, entry, 200, {date: YESTERDAY, alerts: []});
      return;
    }
    if (url.pathname === '/api/history/asks' && method === 'GET'){
      await respond(route, entry, 200, historyAsks());
      return;
    }
    if (url.pathname === '/api/history/briefs' && method === 'GET'){
      await respond(route, entry, 200, {
        dates: [{date: YESTERDAY, markets: [{market: 'ZA', status: 'published'}]}],
        next_before: null,
      });
      return;
    }
    if (url.pathname === '/api/ask' && method === 'POST'){
      state.blockedWrites.push('POST /api/ask');
      await respond(route, entry, 409, {error: 'fixture_write_blocked', message: 'Ask writes are blocked in this fixture journey.'});
      return;
    }
    if (/^\/api\/ask\/[^/]+\/events$/.test(url.pathname)){
      state.eventStreams.push(method + ' ' + url.pathname);
      await respond(route, entry, 409, {error: 'fixture_stream_blocked', message: 'Ask event streams are blocked in this fixture journey.'});
      return;
    }
    if (url.pathname === '/api/ask/' + encodeURIComponent(savedAsk.ask_id) && method === 'GET'){
      await respond(route, entry, 200, savedAsk);
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

    state.unexpectedApi.push(method + ' ' + url.pathname + url.search);
    await respond(route, entry, 404, {error: 'fixture_not_found', message: 'No local API fixture matched.'});
  });
  return state;
}

async function expectBoundaryClean(state){
  expect(state.unexpectedApi).toEqual([]);
  expect(state.blockedWrites).toEqual([]);
  expect(state.eventStreams).toEqual([]);
  expect(state.badCredentials).toEqual([]);
  expect(state.external.every((request) => request.action === 'aborted')).toBe(true);
  expect(state.api.filter((request) => request.method !== 'GET' && request.path !== '/api/auth/verify')).toEqual([]);
}

async function expectFits(page, locator){
  const box = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      left: rect.left,
      right: rect.right,
      width: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    };
  });
  expect(box.scrollWidth).toBeLessThanOrEqual(box.width + 1);
  expect(box.left).toBeGreaterThanOrEqual(-1);
  expect(box.right).toBeLessThanOrEqual(box.width + 1);
}

async function openHistory(page, width){
  if (width === 390){
    const more = page.locator('.rail42--bar .rail42-more-button');
    await expect(more).toBeVisible();
    await more.click();
    const sheet = page.locator('.rail42--bar .rail42-sheet');
    await expect(sheet).toBeVisible();
    await sheet.locator('.rail42-link[href="#/history"]').click();
  } else {
    await page.getByRole('link', {name: 'History', exact: true}).first().click();
  }
  await expect(page).toHaveURL(/#\/history$/);
  await expect(page.locator('#main-content').getByRole('heading', {level: 1, name: 'History', exact: true})).toBeVisible();
}

for (const width of [390, 1280]){
  test.describe(`${width}px fixture journeys`, () => {
    test.use({viewport: {width, height: 900}, serviceWorkers: 'block'});

    test('Today explains its empty Kenya market and links to History', async ({page}) => {
      const state = await installFixtureBoundary(page);
      await page.goto('/#/today?date=' + YESTERDAY);
      const main = page.locator('#main-content');
      await expect(main.locator('[data-today-loaded]')).toBeVisible();
      await main.getByRole('tab', {name: 'Kenya', exact: true}).click();

      const kenya = main.locator('.t42-market[data-market="KE"]');
      /* Demo polish, 2 October 2026: Today now states the empty market in one
         line with the held count, names the Kenya data issue in its banner,
         and opens the held list (renamed Held back) by default, so the
         keyboard check closes and reopens it before reading the held reason. */
      const reason = kenya.locator('[data-empty-market-reason]');
      await expect(reason).toBeVisible();
      await expect(reason).toHaveText('No trend cleared our checks in Kenya today. 1 is held back; see why below.');
      await expect(kenya.locator('.t42-banner-data_issue')).toHaveText('Data issue: collection or detection failed for Kenya');

      const held = kenya.locator('details[data-section="held-for-evidence"]');
      const summary = held.locator('summary');
      await expect(summary).toHaveText('Held back');
      await expect(held).toHaveAttribute('open', '');
      await summary.focus();
      await page.keyboard.press('Enter');
      await expect(held).not.toHaveAttribute('open', '');
      await page.keyboard.press('Enter');
      await expect(held).toHaveAttribute('open', '');
      await expect(held).toContainText('#fixture_ke_match');
      await expect(held.locator('[data-held-reason]')).toHaveText('Data issue on TikTok in the last 3 days');
      await expect(held.locator('[data-held-reason]')).toBeVisible();

      const historyLink = kenya.getByRole('link', {name: 'Choose a past brief in History', exact: true});
      await expect(historyLink).toHaveAttribute('href', '#/history');
      await expectFits(page, historyLink);
      await historyLink.focus();
      await expect(historyLink).toBeFocused();
      await page.keyboard.press('Enter');
      await expect(page.locator('#main-content').getByRole('heading', {level: 1, name: 'History', exact: true})).toBeVisible();
      await expectBoundaryClean(state);
    });

    test('History opens a saved answer by GET without starting an Ask or event stream', async ({page}) => {
      const state = await installFixtureBoundary(page);
      await page.goto('/#/today?date=' + YESTERDAY);
      await expect(page.locator('#main-content').locator('[data-today-loaded]')).toBeVisible();
      await openHistory(page, width);

      const main = page.locator('#main-content');
      const row = main.locator('.hi42-row').filter({hasText: savedAsk.question});
      const readAnswer = row.getByRole('link', {name: 'Read answer', exact: true});
      await expect(readAnswer).toHaveAttribute('href', '#/ask?follow=' + encodeURIComponent(savedAsk.ask_id));
      await expectFits(page, readAnswer);
      await readAnswer.click();
      await expect(page).toHaveURL(new RegExp('#/ask\\?follow=' + savedAsk.ask_id + '$'));

      const answer = page.locator('#main-content').getByRole('article').first();
      await expect(answer.getByRole('heading', {level: 2, name: savedAsk.question, exact: true})).toBeVisible();
      await expect(answer).toContainText(savedAsk.answer.short_answer);
      const source = answer.getByRole('button', {name: /^Source: /}).first();
      await expect(source).toBeVisible();
      await expectFits(page, source);
      const sourceLabel = await source.getAttribute('aria-label') || '';
      const selectedEvidence = savedAsk.answer.evidence.find((item) => item.handle && sourceLabel.includes(item.handle));
      expect(selectedEvidence).toBeTruthy();
      await source.focus();
      await page.keyboard.press('Enter');
      const sourcePanel = page.locator('#main-content aside.ask42-source');
      await expect(sourcePanel.locator('.ask42-source-text')).toContainText(selectedEvidence.text);
      await expect(sourcePanel.getByRole('link', {name: 'Open the post', exact: true})).toHaveAttribute('href', selectedEvidence.url);

      const detailReads = state.api.filter((request) => request.path === '/api/ask/' + savedAsk.ask_id);
      expect(detailReads).toEqual([expect.objectContaining({method: 'GET', status: 200})]);
      expect(state.api.filter((request) => request.path === '/api/ask' && request.method === 'POST')).toEqual([]);
      expect(state.api.filter((request) => request.path === '/api/ask/' + savedAsk.ask_id + '/events')).toEqual([]);
      await expectBoundaryClean(state);
    });

    test('History opens the 30 September brief at its exact date', async ({page}) => {
      const state = await installFixtureBoundary(page);
      await page.goto('/#/history');
      await expect(page.locator('#main-content').getByRole('heading', {level: 1, name: 'History', exact: true})).toBeVisible();
      await page.locator('#main-content').getByRole('tab', {name: 'Briefs', exact: true}).click();

      const brief = page.locator('#main-content').locator('.hi42-row-title[href="#/today?date=2026-09-30"]');
      await expect(brief).toBeVisible();
      await expect(brief).toContainText('30 September 2026');
      await expectFits(page, brief);
      await brief.click();

      await expect(page).toHaveURL(/#\/today\?date=2026-09-30$/);
      await expect(page.locator('[data-today-date-context]')).toContainText('30 September 2026');
      expect(state.api.filter((request) => request.path === '/api/today').map((request) => ({method: request.method, search: request.search})))
        .toEqual([{method: 'GET', search: '?date=2026-09-30'}]);
      await expectBoundaryClean(state);
    });
  });
}
