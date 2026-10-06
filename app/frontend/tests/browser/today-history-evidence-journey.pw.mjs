import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';

const FIXTURE_PASSCODE = 'today-history-evidence-fixture-only';
const FIXED_DATE = '2026-09-30';
const readFixture = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));
const today = readFixture('../../src/ui/__tests__/fixtures/today42.json');
const trend = readFixture('../../src/ui/__tests__/fixtures/trend42.json');
const topic = readFixture('../../src/ui/__tests__/fixtures/topic42_za.json');
const history = readFixture('../../src/ui/__tests__/fixtures/history42.json');
const card = today.markets[0].cards[0];
const heldItem = today.markets[0].held_back.items[0];
const savedFinding = history.findings.findings.find((entry) => entry.finding_id === 'f_step_1');

function allowedOrigin(baseURL){
  const url = new URL(baseURL);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1'){
    throw new Error('The Today fixture journey needs an http://127.0.0.1 Playwright baseURL.');
  }
  return url.origin;
}

async function fulfill(route, entry, status, body){
  entry.status = status;
  entry.responseBody = body;
  await route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });
}

async function installFixtureBoundary(page, baseURL){
  const origin = allowedOrigin(baseURL);
  const state = {
    api: [],
    external: [],
    unexpectedApi: [],
    blockedWrites: [],
    modelPosts: [],
    eventStreams: [],
    badCredentials: [],
  };
  const context = page.context();
  await page.addInitScript(({passcode}) => {
    localStorage.setItem('pulse_passcode', passcode);
    localStorage.setItem('pulse-region', 'ZA');
  }, {passcode: FIXTURE_PASSCODE});
  await context.routeWebSocket('**/*', (webSocket) => webSocket.close());
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== origin){
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
      await fulfill(route, entry, 200, {ok: true, passcode: true, auth_mode: 'passcode', checks: {auth: 'ok'}});
      return;
    }
    if (url.pathname === '/api/auth/verify' && method === 'POST'){
      let body = null;
      try { body = request.postDataJSON(); } catch { body = null; }
      if (body?.passcode !== FIXTURE_PASSCODE) state.badCredentials.push('POST /api/auth/verify');
      await fulfill(route, entry, 200, {ok: true});
      return;
    }
    if (url.pathname === '/api/ask' && method === 'POST'){
      state.modelPosts.push(method + ' ' + url.pathname);
      state.blockedWrites.push(method + ' ' + url.pathname);
      await fulfill(route, entry, 409, {error: 'fixture_write_blocked', message: 'Ask writes are blocked in this fixture.'});
      return;
    }
    if (/^\/api\/ask\/[^/]+\/events$/.test(url.pathname)){
      state.eventStreams.push(method + ' ' + url.pathname);
      await fulfill(route, entry, 409, {error: 'fixture_stream_blocked', message: 'Ask event streams are blocked in this fixture.'});
      return;
    }
    if (method !== 'GET'){
      state.blockedWrites.push(method + ' ' + url.pathname);
      await fulfill(route, entry, 409, {error: 'fixture_write_blocked', message: 'Writes are blocked in this fixture.'});
      return;
    }
    if (url.pathname === '/api/today'){
      await fulfill(route, entry, 200, today);
      return;
    }
    if (url.pathname === '/api/alerts'){
      await fulfill(route, entry, 200, {date: FIXED_DATE, alerts: []});
      return;
    }
    if (url.pathname === '/api/trends/' + encodeURIComponent(card.item_id)){
      await fulfill(route, entry, 200, trend);
      return;
    }
    if (url.pathname === '/api/topics/' + encodeURIComponent(topic.card.item_id)){
      await fulfill(route, entry, 200, topic);
      return;
    }
    if (url.pathname === '/api/history/asks'){
      await fulfill(route, entry, 200, history.asks);
      return;
    }
    if (url.pathname === '/api/history/briefs'){
      await fulfill(route, entry, 200, history.briefs);
      return;
    }
    if (url.pathname === '/api/history/findings'){
      await fulfill(route, entry, 200, history.findings);
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
    if (Object.hasOwn(readFixtures, url.pathname)){
      await fulfill(route, entry, 200, readFixtures[url.pathname]);
      return;
    }
    state.unexpectedApi.push(method + ' ' + url.pathname + url.search);
    await fulfill(route, entry, 404, {error: 'fixture_not_found', message: 'No local fixture matched.'});
  });
  return state;
}

async function openBlockedSource(page, state, link, href){
  await expect(link).toHaveAttribute('href', href);
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', /noopener.*noreferrer/);
  const destination = new URL(href);
  const expectedRequest = destination.origin + destination.pathname;
  const popupPromise = page.waitForEvent('popup');
  await link.click();
  const popup = await popupPromise;
  await expect.poll(() => state.external.some((request) => request.url === expectedRequest && request.action === 'aborted'))
    .toBe(true);
  await popup.close();
}

async function expectFixtureBoundary(state){
  expect(state.unexpectedApi).toEqual([]);
  expect(state.badCredentials).toEqual([]);
  expect(state.blockedWrites).toEqual([]);
  expect(state.modelPosts).toEqual([]);
  expect(state.eventStreams).toEqual([]);
  expect(state.external.every((request) => request.action === 'aborted')).toBe(true);
  expect(state.api.filter((request) => request.method !== 'GET' && request.path !== '/api/auth/verify')).toEqual([]);
}

test('Today cited posts and held evidence lead to the saved History finding and its source', async ({page, baseURL}) => {
  const state = await installFixtureBoundary(page, baseURL);
  await page.goto('/#/today?date=' + FIXED_DATE);
  const main = page.locator('#main-content');
  await expect(main.locator('[data-today-loaded]')).toBeVisible();

  const za = main.locator('.t42-market[data-market="ZA"]');
  const cardView = za.locator('[data-card]').filter({hasText: card.title}).first();
  const postsButton = cardView.getByRole('button', {name: 'Posts', exact: true});
  await postsButton.click();
  const posts = cardView.locator('.t42-posts');
  await expect(posts.locator('[data-evidence-id="' + trend.evidence[0].id + '"]'))
    .toContainText(trend.evidence[0].text);
  const citedPost = posts.getByRole('link', {name: 'Open the post', exact: true}).first();
  await openBlockedSource(page, state, citedPost, trend.evidence[0].url);

  expect(topic.card.item_id).toBe(card.item_id);
  const topicHash = '#/t/' + encodeURIComponent(card.item_id) + '?market=ZA';
  const titleLink = cardView.getByRole('link', {name: card.title, exact: true});
  await expect(titleLink).toHaveAttribute('href', topicHash);
  await titleLink.click();
  await expect(page).toHaveURL(new RegExp('#/t/' + card.item_id + '\\?market=ZA$'));
  const topicMain = page.locator('#main-content');
  await expect(topicMain.getByRole('heading', {level: 1, name: topic.card.title, exact: true})).toBeVisible();
  const topicEvidence = topicMain.locator('[data-section="evidence"]');
  const topicSource = topicEvidence.getByRole('link', {name: 'Open the post', exact: true}).first();
  await openBlockedSource(page, state, topicSource, topic.evidence[0].url);

  await page.goBack();
  await expect(page).toHaveURL(/#\/today\?date=2026-09-30$/);
  await expect(main.locator('[data-today-loaded]')).toBeVisible();

  const heldBack = za.locator('section[data-section="held-back"]');
  const heldButton = heldBack.getByRole('button', {name: heldItem.title, exact: true});
  const heldPanelId = 't42-held-' + heldItem.item_id;
  await expect(heldButton).toHaveAttribute('aria-expanded', 'false');
  await expect(heldButton).toHaveAttribute('aria-controls', heldPanelId);
  await heldButton.click();
  await expect(heldButton).toHaveAttribute('aria-expanded', 'true');
  const heldDetail = heldBack.locator('#' + heldPanelId);
  await expect(heldDetail).toBeVisible();
  await expect(heldDetail).toContainText(heldItem.reason_text);
  const heldPost = heldDetail.locator('a[href="' + heldItem.evidence[0].url + '"]');
  await openBlockedSource(page, state, heldPost, heldItem.evidence[0].url);

  await page.getByRole('link', {name: 'History', exact: true}).first().click();
  await expect(page).toHaveURL(/#\/history(?:\?[^#]*)?$/);
  const historyMain = page.locator('#main-content');
  await expect(historyMain.getByRole('heading', {level: 1, name: 'History', exact: true})).toBeVisible();
  await historyMain.getByRole('tab', {name: 'Findings', exact: true}).click();

  const finding = historyMain.locator('[data-finding="' + savedFinding.finding_id + '"]');
  await expect(finding.locator('.hi42-row-title')).toHaveText(savedFinding.question);
  await expect(finding.locator('.hi42-answer')).toHaveText(savedFinding.answer);
  await expect(finding).toContainText(savedFinding.claims[0].text);
  await expect(finding.locator('[data-evidence="' + savedFinding.evidence[0].id + '"]'))
    .toContainText(savedFinding.evidence[0].text);
  const findingSource = finding.locator('a[href="' + savedFinding.evidence[0].url + '"]');
  await openBlockedSource(page, state, findingSource, savedFinding.evidence[0].url);

  expect(state.api.filter((request) => request.path === '/api/today'))
    .toEqual([
      expect.objectContaining({method: 'GET', search: '?date=' + FIXED_DATE, status: 200, responseBody: today}),
      expect.objectContaining({method: 'GET', search: '?date=' + FIXED_DATE, status: 200, responseBody: today}),
    ]);
  expect(state.api.filter((request) => request.path === '/api/trends/' + card.item_id))
    .toEqual([expect.objectContaining({method: 'GET', search: '?market=ZA&date=' + FIXED_DATE, status: 200})]);
  expect(state.api.filter((request) => request.path === '/api/topics/' + topic.card.item_id))
    .toEqual([expect.objectContaining({method: 'GET', search: '?market=ZA', status: 200, responseBody: topic})]);
  expect(state.api.filter((request) => request.path === '/api/history/findings'))
    .toEqual([expect.objectContaining({method: 'GET', status: 200, responseBody: history.findings})]);
  await expectFixtureBoundary(state);
});
