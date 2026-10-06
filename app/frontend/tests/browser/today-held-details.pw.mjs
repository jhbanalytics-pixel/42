import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';

const ORIGIN = 'http://127.0.0.1:4199';
const FIXTURE_PASSCODE = 'today-held-details-fixture-only';
const FIXED_DATE = '2026-09-30';
const FIXED_RUN_ID = 'r_20260930_held_fixture';
const HOSTILE_TEXT = '<img src=x onerror="window.__heldEvidenceInjected=true"> & <script>window.__heldEvidenceInjected=true</script> literal post text';
const readFixture = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));

function makeTodayFixture(){
  const data = readFixture('../../src/ui/__tests__/fixtures/today42.json');
  const za = data.markets.find((market) => market.market === 'ZA');
  const populated = za.held_back.items[0];
  populated.failed_reason = 'Critic: a simpler explanation was not ruled out: a paid campaign';
  populated.numbers = [
    {
      value: 0,
      unit: 'posts in 3 days',
      query_id: 'q_held_posts_zero',
      run_id: FIXED_RUN_ID,
      result_hash: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    },
    {
      value: null,
      unit: 'creators in 3 days',
      query_id: 'q_held_creators_missing',
      run_id: FIXED_RUN_ID,
      result_hash: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    },
  ];
  populated.evidence[0] = {
    ...populated.evidence[0],
    url: 'https://example.invalid/held/za-post',
    text: HOSTILE_TEXT,
  };

  const ke = data.markets.find((market) => market.market === 'KE');
  const withoutNumbers = ke.held_back.items[0];
  delete withoutNumbers.numbers;
  withoutNumbers.failed_reason = 'Critic: a simpler explanation was not ruled out: a collection artefact';
  withoutNumbers.evidence[0] = {
    ...withoutNumbers.evidence[0],
    url: 'https://example.invalid/held/ke-post',
    text: HOSTILE_TEXT,
  };

  return {data, populated, withoutNumbers};
}

const {data: today, populated, withoutNumbers} = makeTodayFixture();

async function fulfill(route, entry, status, body){
  entry.status = status;
  entry.responseBody = body;
  await route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });
}

async function installFixtureBoundary(page){
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
    window.__heldEvidenceInjected = false;
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

    const readFixtures = {
      '/api/alerts': {date: FIXED_DATE, alerts: []},
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

async function expectStoredEvidence(container, item){
  const posts = container.locator('[data-evidence-id]');
  await expect(posts).toHaveCount(item.evidence.length);
  for (const evidence of item.evidence){
    const post = container.locator('[data-evidence-id="' + evidence.id + '"]');
    await expect(post).toContainText(evidence.text);
    if (evidence.url) await expect(post.locator('a[href="' + evidence.url + '"]')).toBeVisible();
  }
}

async function expectFixtureBoundary(state){
  expect(state.unexpectedApi).toEqual([]);
  expect(state.badCredentials).toEqual([]);
  expect(state.blockedWrites).toEqual([]);
  expect(state.modelPosts).toEqual([]);
  expect(state.eventStreams).toEqual([]);
  expect(state.webSockets).toEqual([]);
  expect(state.external.every((request) => request.action === 'aborted')).toBe(true);
  expect(state.api.filter((request) => request.method !== 'GET' && request.path !== '/api/auth/verify')).toEqual([]);
  expect(state.api.filter((request) => request.path === '/api/today'))
    .toEqual([expect.objectContaining({method: 'GET', search: '?date=' + FIXED_DATE, status: 200, responseBody: today})]);
  expect(state.api.some((request) => request.path.startsWith('/api/trends/'))).toBe(false);
}

test('Today held details use stored fields and source links without another read or write', async ({page}) => {
  const state = await installFixtureBoundary(page);
  await page.goto('/#/today?date=' + FIXED_DATE);
  const main = page.locator('#main-content');
  await expect(main.locator('[data-today-loaded]')).toBeVisible();

  const za = main.locator('.t42-market[data-market="ZA"]');
  const heldBack = za.locator('section[data-section="held-back"]');
  const heldButton = heldBack.getByRole('button', {name: populated.title, exact: true});
  await expect(heldButton).toHaveAttribute('aria-expanded', 'false');
  await heldButton.click();
  await expect(heldButton).toHaveAttribute('aria-expanded', 'true');
  const populatedDetail = heldBack.locator('#t42-held-' + populated.item_id);
  await expect(populatedDetail).toBeVisible();
  // The row names the trend and its reason once; the opened part holds the check detail, figures and posts.
  const populatedRow = heldBack.locator('ul.t42-rows > li').filter({has: page.getByRole('button', {name: populated.title, exact: true})});
  await expect(populatedRow.locator('[data-held-row-reason]')).toHaveText(populated.reason_text);
  await expect(populatedDetail).not.toContainText(populated.title);
  await expect(populatedDetail).toContainText(populated.failed_reason);
  await expectStoredEvidence(populatedDetail, populated);

  const numbers = await populatedDetail.locator('[data-held-number]').evaluateAll((rows) => rows.map((row) => {
    const source = row.matches('[data-query-id]') ? row : row.querySelector('[data-query-id]');
    return {text: row.textContent, queryId: source && source.getAttribute('data-query-id')};
  }));
  expect(numbers).toHaveLength(2);
  expect(numbers[0]).toEqual(expect.objectContaining({queryId: 'q_held_posts_zero', text: expect.stringContaining('0')}));
  expect(numbers[0].text).toContain('posts in 3 days');
  expect(numbers[1]).toEqual(expect.objectContaining({queryId: 'q_held_creators_missing'}));
  expect(numbers[1].text).toContain('creators in 3 days');
  expect(numbers[1].text).not.toMatch(/\b0\b/);
  expect(numbers[1].text).toMatch(/unrecorded|not recorded|not measured/i);

  const hostilePost = populatedDetail.locator('.t42-post-text').filter({hasText: HOSTILE_TEXT});
  await expect(hostilePost).toHaveCount(1);
  await expect(hostilePost).toHaveText(HOSTILE_TEXT);
  await expect(populatedDetail.locator('.t42-post-text img, .t42-post-text script')).toHaveCount(0);
  expect(await page.evaluate(() => window.__heldEvidenceInjected)).toBe(false);
  const zaSource = populatedDetail.locator('a[href="' + populated.evidence[0].url + '"]');
  await openBlockedSource(page, state, zaSource, populated.evidence[0].url);

  await main.getByRole('tab', {name: 'Kenya', exact: true}).click();
  const ke = main.locator('.t42-market[data-market="KE"]');
  const heldForEvidence = ke.locator('details[data-section="held-for-evidence"]');
  await expect(heldForEvidence).toBeVisible();
  await expect(heldForEvidence).toHaveAttribute('open', '');
  const emptyMarketDetail = heldForEvidence.locator('[data-held-item-id="' + withoutNumbers.item_id + '"]');
  await expect(emptyMarketDetail).toBeVisible();
  await expect(emptyMarketDetail).toContainText(withoutNumbers.title);
  await expect(emptyMarketDetail).toContainText(withoutNumbers.reason_text);
  await expect(emptyMarketDetail).toContainText(withoutNumbers.failed_reason);
  await expect(emptyMarketDetail).not.toContainText('Trend numbers were not stored');
  await expect(emptyMarketDetail.locator('[data-held-number]')).toHaveCount(0);
  await expectStoredEvidence(emptyMarketDetail, withoutNumbers);
  const emptyHostilePost = emptyMarketDetail.locator('.t42-post-text').filter({hasText: HOSTILE_TEXT});
  await expect(emptyHostilePost).toHaveCount(1);
  await expect(emptyHostilePost).toHaveText(HOSTILE_TEXT);
  await expect(emptyMarketDetail.locator('.t42-post-text img, .t42-post-text script')).toHaveCount(0);
  expect(await page.evaluate(() => window.__heldEvidenceInjected)).toBe(false);
  const keSource = emptyMarketDetail.locator('a[href="' + withoutNumbers.evidence[0].url + '"]');
  await openBlockedSource(page, state, keSource, withoutNumbers.evidence[0].url);

  await expectFixtureBoundary(state);
});
