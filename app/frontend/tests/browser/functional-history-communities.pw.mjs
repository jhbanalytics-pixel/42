import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';

const ORIGIN = 'http://127.0.0.1:4199';
const FIXTURE_PASSCODE = 'functional-history-fixture-only';
const FIXED_DATE = '2026-09-30';
const readFixture = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));
const today = readFixture('../../src/ui/__tests__/fixtures/today42.json');
const history = readFixture('../../src/ui/__tests__/fixtures/history42.json');
const savedAsk = readFixture('../../src/ui/__tests__/fixtures/ask42_complete.json');
const people = readFixture('../../src/ui/__tests__/fixtures/people42.json');

function asksWithSavedAnswer(){
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

async function installFixtureBoundary(page, options = {}){
  const state = {
    api: [],
    unexpectedApi: [],
    blockedWrites: [],
    modelPosts: [],
    eventStreams: [],
    external: [],
    badCredentials: [],
    communityReads: [],
    releaseInitialCommunity: null,
  };
  const askPage = options.savedAsk ? asksWithSavedAnswer() : history.asks;
  await page.addInitScript((passcode) => {
    localStorage.setItem('pulse_passcode', passcode);
    localStorage.setItem('pulse-region', 'NG');
  }, FIXTURE_PASSCODE);
  await page.routeWebSocket('**/*', (webSocket) => webSocket.close());
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
    if (url.pathname === '/api/today' && method === 'GET'){
      await respond(route, entry, 200, today);
      return;
    }
    if (url.pathname === '/api/alerts' && method === 'GET'){
      await respond(route, entry, 200, {date: FIXED_DATE, alerts: []});
      return;
    }
    if (url.pathname === '/api/history/asks' && method === 'GET'){
      await respond(route, entry, 200, askPage);
      return;
    }
    if (url.pathname === '/api/history/briefs' && method === 'GET'){
      await respond(route, entry, 200, history.briefs);
      return;
    }
    if (url.pathname === '/api/ask' && method === 'POST'){
      state.modelPosts.push(method + ' ' + url.pathname);
      state.blockedWrites.push(method + ' ' + url.pathname);
      await respond(route, entry, 409, {error: 'fixture_write_blocked', message: 'Ask writes are blocked in this fixture.'});
      return;
    }
    if (/^\/api\/ask\/[^/]+\/events$/.test(url.pathname)){
      state.eventStreams.push(method + ' ' + url.pathname);
      await respond(route, entry, 409, {error: 'fixture_stream_blocked', message: 'Ask event streams are blocked in this fixture.'});
      return;
    }
    if (url.pathname === '/api/ask/' + encodeURIComponent(savedAsk.ask_id) && method === 'GET'){
      await respond(route, entry, 200, savedAsk);
      return;
    }
    if (url.pathname === '/api/communities' && method === 'GET'){
      state.communityReads.push(entry);
      if (options.holdCommunity && state.communityReads.length === 1){
        await new Promise((resolve) => { state.releaseInitialCommunity = resolve; });
        try {
          await respond(route, entry, 503, {error: 'unavailable', message: 'Communities could not be loaded.'});
        } catch {
          entry.cancelled = true;
        }
        return;
      }
      if (url.searchParams.get('market') === 'NG'){
        await respond(route, entry, 200, people.communities);
        return;
      }
      if (options.everyCommunityMarket && ['ZA', 'KE'].includes(url.searchParams.get('market'))){
        await respond(route, entry, 200, {...people.communities, market: url.searchParams.get('market')});
        return;
      }
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
    if (method !== 'GET') state.blockedWrites.push(method + ' ' + url.pathname);
    await respond(route, entry, 404, {error: 'fixture_not_found', message: 'No local fixture matched.'});
  });
  return state;
}

async function expectFixtureBoundary(state){
  expect(state.unexpectedApi).toEqual([]);
  expect(state.badCredentials).toEqual([]);
  expect(state.blockedWrites).toEqual([]);
  expect(state.eventStreams).toEqual([]);
  expect(state.external.every((request) => request.action === 'aborted')).toBe(true);
}

test('History Briefs opens a dated Today and browser Back restores the Briefs tab', async ({page}) => {
  const state = await installFixtureBoundary(page);
  await page.goto('/#/history');
  const main = page.locator('#main-content');
  await expect(main.getByRole('heading', {level: 1, name: 'History', exact: true})).toBeVisible();
  await main.getByRole('tab', {name: 'Briefs', exact: true}).click();

  const brief = main.locator('.hi42-row-title[href="#/today?date=' + FIXED_DATE + '"]');
  await expect(brief).toBeVisible();
  await brief.click();
  await expect(page).toHaveURL(new RegExp('#/today\\?date=' + FIXED_DATE + '$'));
  await expect(page.locator('[data-today-date-context]')).toContainText('30 September 2026');
  expect(state.api.filter((request) => request.path === '/api/today').map((request) => request.search))
    .toContain('?date=' + FIXED_DATE);

  await page.goBack();
  await expect(page).toHaveURL(/#\/history\?tab=briefs$/);
  const briefsTab = page.locator('#main-content').getByRole('tab', {name: 'Briefs', exact: true});
  await expect(briefsTab).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#main-content [data-section="briefs"]')).toBeVisible();
  await expectFixtureBoundary(state);
});

test('History opens a saved Ask by GET without posting a new Ask or opening events', async ({page}) => {
  const state = await installFixtureBoundary(page, {savedAsk: true});
  await page.goto('/#/history');
  const main = page.locator('#main-content');
  const row = main.locator('.hi42-row').filter({hasText: savedAsk.question});
  const readAnswer = row.getByRole('link', {name: 'Read answer', exact: true});
  await expect(readAnswer).toHaveAttribute('href', '#/ask?follow=' + encodeURIComponent(savedAsk.ask_id));
  await readAnswer.click();
  await expect(page).toHaveURL(new RegExp('#/ask\\?follow=' + savedAsk.ask_id + '$'));

  const answer = page.locator('#main-content').getByRole('article').first();
  await expect(answer.getByRole('heading', {level: 2, name: savedAsk.question, exact: true})).toBeVisible();
  await expect(answer).toContainText(savedAsk.answer.short_answer);
  expect(state.api.filter((request) => request.path === '/api/ask/' + savedAsk.ask_id))
    .toEqual([expect.objectContaining({method: 'GET', status: 200})]);
  expect(state.modelPosts).toEqual([]);
  expect(state.eventStreams).toEqual([]);
  await expectFixtureBoundary(state);
});

test('Communities shows a retry after a pending read times out, then loads its fixture', async ({page}) => {
  await page.clock.install({time: new Date('2026-10-01T12:00:00+02:00')});
  const state = await installFixtureBoundary(page, {holdCommunity: true});
  try {
    await page.goto('/#/communities?market=NG');
    await expect(page.getByRole('status').filter({hasText: 'Loading the communities'})).toBeVisible();
    await page.clock.fastForward(30001);
    await expect(page.getByRole('alert')).toBeVisible();
    const retry = page.getByRole('button', {name: 'Try again', exact: true});
    await expect(retry).toBeEnabled();
    await retry.click();

    await expect(page.getByRole('heading', {level: 1, name: 'Communities in Nigeria', exact: true})).toBeVisible();
    const first = people.communities.communities[0];
    await expect(page.locator('[data-community="' + first.community_id + '"]'))
      .toContainText(first.label);
    expect(state.communityReads.map((request) => request.search)).toEqual(['?market=NG', '?market=NG']);
    await expectFixtureBoundary(state);
  } finally {
    state.releaseInitialCommunity?.();
  }
});

/* Albert, 5 October 2026: picking another country left Communities on
   South Africa, with the header reading All markets. Communities reads one
   market, so the header shows the page's market, and a market picked in
   the header moves the page, after an in-page tab as well as before one. */
test('the header market picker moves Communities and shows the market on the page', async ({page}) => {
  const state = await installFixtureBoundary(page, {everyCommunityMarket: true});
  await page.addInitScript(() => localStorage.setItem('pulse-region', 'ALL'));
  await page.goto('/#/communities');
  const heading = page.locator('#main-content').getByRole('heading', {level: 1});
  const picker = page.locator('[data-continuity-market-scope]');
  const pick = async (code) => {
    await picker.click();
    await page.locator('#instrument-r0-market-scope [data-market="' + code + '"]').click();
  };
  await expect(heading).toHaveText('Communities in South Africa');
  await expect(picker).toHaveText('South Africa');

  await pick('NG');
  await expect(heading).toHaveText('Communities in Nigeria');
  await expect(picker).toHaveText('Nigeria');
  await expect(page).toHaveURL(/#\/communities\?market=NG$/);

  await pick('KE');
  await expect(heading).toHaveText('Communities in Kenya');
  await expect(picker).toHaveText('Kenya');

  await page.locator('#main-content').getByRole('navigation', {name: 'Market'}).getByRole('link', {name: 'South Africa', exact: true}).click();
  await expect(heading).toHaveText('Communities in South Africa');
  await expect(picker).toHaveText('South Africa');

  await pick('NG');
  await expect(heading).toHaveText('Communities in Nigeria');
  expect(state.communityReads.at(-1).search).toBe('?market=NG');
  await expectFixtureBoundary(state);
});
