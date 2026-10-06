import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';

const ORIGIN = 'http://127.0.0.1:4199';
const FIXTURE_PASSCODE = 'today-screen-states-fixture-only';
const FIXED_DATE = '2026-10-02';
const SERVER_HEADING = 'Today, Friday 2 October 2026';
const HELD_DETAIL = 'The saved detail includes <img src=x onerror="window.__heldDetailInjected=true"> as plain text.';
const SOURCE_ISSUE = 'TikTok collection: 2 requests failed';
const readFixture = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));
const clone = (value) => JSON.parse(JSON.stringify(value));

function makeAllEmptyFixture(){
  const data = clone(readFixture('../../src/ui/__tests__/fixtures/today42.json'));
  data.date = FIXED_DATE;
  data.heading = SERVER_HEADING;
  data.status = 'data_issue';
  data.headline = null;
  for (const market of data.markets){
    market.cards = [];
    market.more = [];
    market.held_back.count = market.held_back.items.length;
  }

  const za = data.markets.find((market) => market.market === 'ZA');
  za.held_back.items[0].held_detail = HELD_DETAIL;

  const ke = data.markets.find((market) => market.market === 'KE');
  ke.status = 'data_issue';
  ke.banners = [
    {kind: 'data_issue', text: 'Data issue: collection or detection failed for Kenya'},
    {kind: 'thin_coverage', text: 'Thin coverage: 1 of 4 collection series usable today'},
  ];
  ke.coverage.issues = [SOURCE_ISSUE, 'Instagram search: no usable posts'];
  return data;
}

function makePartialFixture(){
  const data = clone(readFixture('../../src/ui/__tests__/fixtures/today42.json'));
  data.date = FIXED_DATE;
  data.heading = SERVER_HEADING;
  data.status = 'partial';
  for (const market of data.markets) market.status = 'published';
  return data;
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

async function installFixtureBoundary(page, today){
  const state = {
    api: [],
    external: [],
    unexpectedApi: [],
    blockedWrites: [],
    modelPosts: [],
    eventStreams: [],
    badCredentials: [],
    webSockets: [],
  };
  const context = page.context();
  await page.addInitScript(({passcode}) => {
    localStorage.setItem('pulse_passcode', passcode);
    localStorage.setItem('pulse-region', 'ZA');
    window.__heldDetailInjected = false;
  }, {passcode: FIXTURE_PASSCODE});
  await context.routeWebSocket('**/*', (webSocket) => {
    state.webSockets.push(webSocket.url());
    webSocket.close();
  });
  await context.route('**/*', async (route) => {
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
    const entry = {method, path: url.pathname, search: url.search, status: null, responseBody: null};
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
    if (/^\/api\/ask\/[^/]+\/events$/.test(url.pathname)){
      state.eventStreams.push(method + ' ' + url.pathname);
      await fulfill(route, entry, 409, {error: 'fixture_stream_blocked', message: 'Ask event streams are blocked in this fixture.'});
      return;
    }
    if (url.pathname === '/api/ask' && method !== 'GET'){
      state.modelPosts.push(method + ' ' + url.pathname);
      state.blockedWrites.push(method + ' ' + url.pathname);
      await fulfill(route, entry, 409, {error: 'fixture_write_blocked', message: 'Ask writes are blocked in this fixture.'});
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

    const readFixtures = {
      '/api/desk': {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}},
      '/api/investigations': {investigations: []},
      '/api/research/recent': {artifacts: []},
      '/api/schedules': {schedules: []},
      '/api/v2/investigations/list': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false},
      '/api/v2/investigations/scopes': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null},
      '/api/watches': {watches: []},
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

async function expectFixtureBoundary(state, today, expectedExternalUrls = []){
  expect(state.unexpectedApi).toEqual([]);
  expect(state.badCredentials).toEqual([]);
  expect(state.blockedWrites).toEqual([]);
  expect(state.modelPosts).toEqual([]);
  expect(state.eventStreams).toEqual([]);
  expect(state.webSockets).toEqual([]);
  const external = state.external.map(({url, action}) => ({url, action}))
    .sort((left, right) => left.url.localeCompare(right.url));
  const expectedExternal = expectedExternalUrls.map((url) => ({url, action: 'aborted'}))
    .sort((left, right) => left.url.localeCompare(right.url));
  expect(external).toEqual(expectedExternal);
  expect(state.api.filter((request) => request.method !== 'GET' && request.path !== '/api/auth/verify')).toEqual([]);
  expect(state.api.filter((request) => request.path === '/api/today'))
    .toEqual([expect.objectContaining({method: 'GET', search: '?date=' + FIXED_DATE, status: 200, responseBody: today})]);
  expect(state.api.filter((request) => request.path === '/api/alerts'))
    .toEqual([expect.objectContaining({method: 'GET', status: 200, responseBody: {date: FIXED_DATE, alerts: []}})]);
  expect(state.api.some((request) => request.path.startsWith('/api/trends/'))).toBe(false);
}

test('an all-empty Today brief keeps its server heading and shows stored holds and source issues', async ({page}) => {
  const today = makeAllEmptyFixture();
  const state = await installFixtureBoundary(page, today);
  await page.goto('/#/today?date=' + FIXED_DATE);
  const main = page.locator('#main-content');
  await expect(main.locator('h1[data-today-loaded]')).toHaveText(SERVER_HEADING);

  const historyNote = main.locator('[data-today-history-link]');
  await expect(historyNote).toHaveCount(1);
  const historyLink = historyNote.locator('a');
  await expect(historyLink).toHaveAttribute('href', '#/history');
  const historyFollowsHeader = await historyLink.evaluate((link) => {
    const page = link.closest('.t42');
    const children = [...page.children];
    const heading = children.findIndex((child) => child.matches('.t42-head'));
    const history = children.findIndex((child) => child.contains(link));
    return heading >= 0 && history === heading + 1;
  });
  expect(historyFollowsHeader).toBe(true);

  await main.getByRole('tab', {name: 'All', exact: true}).click();
  for (const marketData of today.markets){
    const market = main.locator('.t42-market[data-market="' + marketData.market + '"]');
    const count = marketData.held_back.items.length;
    const heldCount = count === 1 ? '1 is held back' : count + ' are held back';
    await expect(market.locator('[data-empty-market-reason]'))
      .toHaveText('No trend cleared our checks in ' + marketData.label + ' today. ' + heldCount + '; see why below.');
    const held = market.locator('details[data-section="held-for-evidence"]');
    await expect(held).toHaveAttribute('open', '');
    await expect(held.locator('[data-held-item-id]')).toHaveCount(count);
    for (const item of marketData.held_back.items){
      const row = held.locator('[data-held-item-id="' + item.item_id + '"]');
      await expect(row).toContainText(item.title);
      if (item.held_detail) await expect(row).toContainText(item.held_detail);
    }
  }

  const detailRow = main.locator('[data-held-item-id="' + today.markets[0].held_back.items[0].item_id + '"]');
  await expect(detailRow.locator('img, script')).toHaveCount(0);
  expect(await page.evaluate(() => window.__heldDetailInjected)).toBe(false);

  const ke = main.locator('.t42-market[data-market="KE"]');
  await expect(main.locator('[data-today-status="data_issue"]'))
    .toHaveText('Data issue: some data needed for this brief is incomplete.');
  await expect(ke.locator('.t42-banner-data_issue'))
    .toHaveText('Data issue: collection or detection failed for Kenya');
  await expect(ke.locator('[data-source-problem]')).toHaveText('Some sources were incomplete today');
  const sourceDetails = ke.locator('details[data-section="source-details"]');
  await expect(sourceDetails).toBeVisible();
  await expect(sourceDetails).not.toHaveAttribute('open', '');
  await expect(sourceDetails.locator('summary')).toHaveText('Source details');
  await expect(sourceDetails.getByText(SOURCE_ISSUE, {exact: true})).not.toBeVisible();
  await sourceDetails.locator('summary').click();
  await expect(sourceDetails.getByText(SOURCE_ISSUE, {exact: true})).toBeVisible();
  await expect(sourceDetails).toContainText('Thin coverage: 1 of 4 collection series usable today');
  await sourceDetails.locator('summary').click();
  await expect(sourceDetails).not.toHaveAttribute('open', '');

  await expect.poll(() => state.api.some((request) => request.path === '/api/alerts' && request.status === 200)).toBe(true);
  await expect(main.locator('[data-section="alerts"]')).toHaveCount(0);
  await expectFixtureBoundary(state, today);
});

test('a partial Today brief keeps its message and populated held list', async ({page}) => {
  const today = makePartialFixture();
  const state = await installFixtureBoundary(page, today);
  await page.goto('/#/today?date=' + FIXED_DATE);
  const main = page.locator('#main-content');
  await expect(main.locator('h1[data-today-loaded]')).toHaveText(SERVER_HEADING);
  await expect(main.locator('[data-today-status="partial"]'))
    .toHaveText('Some trends are shown without an explanation yet.');

  const za = main.locator('.t42-market[data-market="ZA"]');
  await expect(za.locator('[data-card]').first()).toBeVisible();
  const heldBack = za.locator('section[data-section="held-back"]');
  await expect(heldBack).toBeVisible();
  await expect(heldBack).toContainText(today.markets[0].held_back.text);
  for (const item of today.markets[0].held_back.items){
    await expect(heldBack.getByRole('button', {name: item.title, exact: true})).toBeVisible();
  }

  await expectFixtureBoundary(state, today, [
    'https://example.invalid/thumb/tt_za_006.jpg',
    'https://example.invalid/thumb/ig_za_007.jpg',
  ]);
});
