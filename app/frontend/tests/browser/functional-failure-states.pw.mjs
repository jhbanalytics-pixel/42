import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {lensRosterFixture, questionCoverageFixture} from './support/capability-harness.mjs';

const ORIGIN = 'http://127.0.0.1:4199';
const FIXTURE_PASSCODE = 'functional-failure-fixture-only';
const FIXED_DATE = '2026-09-30';
const NOT_FOUND = {error: 'not_found', message: 'No such API route.'};
const query = 'football & street culture';
const browseResult = {
  query,
  market: 'za',
  total_matches: 14,
  thin: false,
  broad: false,
  volume_series: Array.from({length: 30}, (_, index) => ({
    date: new Date(Date.UTC(2026, 7, 8 + index)).toISOString().slice(0, 10),
    n: index >= 23 ? 2 : 0,
  })),
  platform_split: [{platform: 'tiktok', n: 14}],
  market_split: [{market: 'za', n: 14}],
  trends: [],
  slang: [],
  creators: [{handle: 'local.player', mentions: 2}],
  overall_read: '',
  limited_voice: false,
  quotes: [
    {text: 'Street football brings the neighbours together.', platform: 'tiktok', market: 'za', engagement: 40, handle: 'local.player'},
    {text: 'The coach shares the training schedule.', platform: 'tiktok', market: 'za', engagement: 30, handle: 'local.coach'},
    {text: 'A nearby club lists its next friendly match.', platform: 'tiktok', market: 'za', engagement: 20, handle: 'local.club'},
  ],
};
const readFixture = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8'));
const today = readFixture('../../src/ui/__tests__/fixtures/today42.json');

async function fulfill(route, entry, status, body){
  entry.status = status;
  entry.responseBody = body;
  await route.fulfill({status, contentType: 'application/json; charset=utf-8', body: JSON.stringify(body)});
}

async function installFixtureBoundary(page, {market = 'ZA', replyFor = null} = {}){
  const state = {api: [], external: [], badCredentials: [], blockedWrites: []};
  await page.addInitScript(({passcode, region}) => {
    localStorage.setItem('pulse_passcode', passcode);
    localStorage.setItem('pulse-region', region);
  }, {passcode: FIXTURE_PASSCODE, region: market});
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

    if (replyFor){
      const reply = await replyFor({request, url, entry, state});
      if (reply){
        await fulfill(route, entry, reply.status, reply.json);
        return;
      }
    }

    const readFixtures = {
      '/api/desk': {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}},
      '/api/alerts': {date: FIXED_DATE, alerts: []},
      '/api/research/recent': {artifacts: []},
      '/api/v2/investigations/list': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false},
      '/api/v2/investigations/scopes': {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null},
      '/api/investigations': {investigations: []},
      '/api/schedules': {schedules: []},
      '/api/watches': {watches: []},
      '/api/chat/coverage': questionCoverageFixture(),
      '/api/chat/lenses': lensRosterFixture(),
    };
    if (method === 'GET' && Object.hasOwn(readFixtures, url.pathname)){
      await fulfill(route, entry, 200, readFixtures[url.pathname]);
      return;
    }
    if (method !== 'GET') state.blockedWrites.push(method + ' ' + url.pathname);
    await fulfill(route, entry, method === 'GET' ? 404 : 409, {error: 'fixture_not_found', message: 'No local fixture matched.'});
  });
  return state;
}

async function expectFixtureBoundary(state){
  expect(state.badCredentials).toEqual([]);
  expect(state.blockedWrites).toEqual([]);
  expect(state.external.every((request) => request.action === 'aborted')).toBe(true);
}

/* Page port, 3 October 2026: Browse, Listen and Network read the desk, the
   archive search and the mentions feed, which f42-api does not serve, so
   they left the More menu and their links now land on the 42 page for the
   same job (src/legacyRoutes.js): #/browse opens Discover, #/listen opens
   Seed path and #/network opens Communities. No route reaches the older
   pages' failure states any more, so these tests hold the failure states of
   the pages they land on, behind the same fixture boundary: a missing route
   is named as missing, a transient failure retries to the returned fixture,
   nothing stays busy, and no older read is made. */
const OLDER_READS = ['/api/desk', '/api/voices', '/api/ask', '/api/intel/mentions'];
const discoverPage = () => {
  const za = today.markets.find((entry) => entry.market === 'ZA');
  return {date: FIXED_DATE, market: 'ZA', items: structuredClone(za.cards), next_cursor: null, held_back: {count: 0, items: []}, filters: {}};
};
const olderReads = (state) => state.api.filter((entry) => OLDER_READS.includes(entry.path));

/* Was: Browse explains a missing API route as not available yet. */
test('Browse opens Discover, which names a missing API route and offers a retry', async ({page}) => {
  const state = await installFixtureBoundary(page, {
    market: 'ZA',
    replyFor: ({request, url}) => url.pathname.startsWith('/api/discover') && request.method() === 'GET'
      ? {status: 404, json: NOT_FOUND}
      : null,
  });
  await page.goto('/#/browse');
  await expect(page).toHaveURL(/#\/explore$/);

  const main = page.locator('#main-content');
  await expect(main.getByRole('heading', {name: 'Discover could not load', exact: true})).toBeVisible();
  await expect(main.locator('.d42-feed').getByRole('alert')).toHaveText(NOT_FOUND.message);
  await expect(main.getByRole('button', {name: 'Try again', exact: true})).toBeVisible();
  await expect(page.getByRole('textbox', {name: 'Search the post archive'})).toHaveCount(0);
  expect(state.api.filter((entry) => entry.path === '/api/discover'))
    .toEqual([expect.objectContaining({method: 'GET', search: '?market=ZA&sort=order&limit=50', status: 404, responseBody: NOT_FOUND})]);
  expect(olderReads(state)).toEqual([]);
  await expectFixtureBoundary(state);
});

/* Was: Browse retries a transient failure and renders the returned fixture. */
test('Browse opens Discover, which retries a transient failure and renders the returned fixture', async ({page}) => {
  let reads = 0;
  const payload = discoverPage();
  const state = await installFixtureBoundary(page, {
    market: 'ZA',
    replyFor: ({request, url}) => {
      if (request.method() !== 'GET') return null;
      if (url.pathname === '/api/discover/radar') return {status: 200, json: {market: 'ZA', items: []}};
      if (url.pathname !== '/api/discover') return null;
      reads++;
      return reads === 1
        ? {status: 503, json: {error: 'unavailable', message: 'Discover read unavailable'}}
        : {status: 200, json: payload};
    },
  });
  await page.goto('/#/browse');
  await expect(page).toHaveURL(/#\/explore$/);
  const main = page.locator('#main-content');
  await expect(main.locator('.d42-feed').getByRole('alert')).toHaveText('Discover read unavailable');
  await main.getByRole('button', {name: 'Try again', exact: true}).click();
  await expect(main.getByRole('heading', {name: 'Trends', exact: true})).toBeVisible();
  await expect(main.locator('.d42-feed')).toContainText(payload.items[0].title);
  await expect(main.locator('.d42-feed').getByRole('alert')).toHaveCount(0);
  expect(reads).toBe(2);
  expect(olderReads(state)).toEqual([]);
  await expectFixtureBoundary(state);
});

/* Was: Listen settles to its unavailable state when the mentions endpoint is missing. */
test('Listen opens Seed path, which settles to its untraced state when its Discover read is missing', async ({page}) => {
  const state = await installFixtureBoundary(page, {
    market: 'ZA',
    replyFor: ({request, url}) => (url.pathname === '/api/discover' || url.pathname === '/api/intel/mentions') && request.method() === 'GET'
      ? {status: 404, json: NOT_FOUND}
      : null,
  });
  await page.goto('/#/listen');
  await expect(page).toHaveURL(/#\/seedpath(\?region=za)?$/);

  const main = page.locator('#main-content');
  await expect(main.getByText('Nothing traced yet', {exact: true})).toBeVisible();
  await expect(main.locator('[aria-busy="true"]')).toHaveCount(0);
  await expect(main.locator('.sp-try')).toHaveCount(0);
  await expect(main.getByText('Listen is not available yet.')).toHaveCount(0);
  await expect.poll(() => state.api.filter((entry) => entry.path === '/api/discover').length).toBe(1);
  expect(state.api.filter((entry) => entry.path === '/api/discover'))
    .toEqual([expect.objectContaining({method: 'GET', search: '?market=ZA&limit=12', status: 404, responseBody: NOT_FOUND})]);
  expect(state.api.filter((entry) => entry.path === '/api/seed-path')).toEqual([]);
  expect(olderReads(state)).toEqual([]);
  await expectFixtureBoundary(state);
});

/* Was: Network settles to an unavailable state when desk and voices endpoints are missing. */
test('Network opens Communities, which settles to its not-found state when its endpoint is missing', async ({page}) => {
  const state = await installFixtureBoundary(page, {
    market: 'ZA',
    replyFor: ({request, url}) => ['/api/communities', '/api/desk', '/api/voices'].includes(url.pathname) && request.method() === 'GET'
      ? {status: 404, json: NOT_FOUND}
      : null,
  });
  await page.goto('/#/network');
  await expect(page).toHaveURL(/#\/communities$/);

  const main = page.locator('#main-content');
  await expect(main.getByRole('heading', {level: 1, name: 'Communities not found', exact: true})).toBeVisible();
  await expect(main.getByText(NOT_FOUND.message, {exact: true})).toBeVisible();
  await expect(main.locator('[aria-busy="true"]')).toHaveCount(0);
  await expect(main.getByRole('button', {name: /retry|try again/i})).toHaveCount(0);
  const reads = state.api.filter((entry) => !['/api/health', '/api/auth/verify'].includes(entry.path));
  expect([...new Set(reads.map((entry) => entry.path))]).toEqual(['/api/communities']);
  expect(reads.every((entry) => entry.method === 'GET' && entry.search === '?market=ZA' && entry.status === 404 && entry.responseBody.error === 'not_found'))
    .toBe(true);
  expect(olderReads(state)).toEqual([]);
  await expectFixtureBoundary(state);
});

test('Today keeps late-run banners on their market when the selected market changes', async ({page}) => {
  const payload = structuredClone(today);
  const za = payload.markets.find((entry) => entry.market === 'ZA');
  const ng = payload.markets.find((entry) => entry.market === 'NG');
  za.banners = [...(za.banners || []), {kind: 'late_run', text: 'Late run: checked at 18:47'}];
  ng.banners = [...(ng.banners || []), {kind: 'late_run', text: 'Late run: checked at 17:12'}];
  const state = await installFixtureBoundary(page, {
    market: 'ZA',
    replyFor: ({request, url}) => url.pathname === '/api/today' && request.method() === 'GET'
      ? {status: 200, json: payload}
      : null,
  });
  await page.goto('/#/today?date=' + FIXED_DATE);
  const main = page.locator('#main-content');
  const zaBanner = main.locator('[data-market="ZA"] .t42-banner-late_run');
  await expect(zaBanner).toHaveCount(1);
  await expect(zaBanner).toHaveText('Late run: checked at 18:47');
  await main.getByRole('tab', {name: 'Nigeria', exact: true}).click();
  const ngBanner = main.locator('[data-market="NG"] .t42-banner-late_run');
  await expect(ngBanner).toHaveCount(1);
  await expect(ngBanner).toHaveText('Late run: checked at 17:12');
  await expect(main.locator('[data-market="ZA"] .t42-banner-late_run')).toHaveCount(0);
  await main.getByRole('tab', {name: 'All', exact: true}).click();
  const allZaBanner = main.locator('[data-market="ZA"] .t42-banner-late_run');
  await expect(allZaBanner).toHaveCount(1);
  await expect(allZaBanner).toHaveText('Late run: checked at 18:47');
  await expect(main.locator('[data-market="NG"] .t42-banner-late_run')).toHaveCount(1);
  await expect(main.locator('[data-market="NG"] .t42-banner-late_run')).toHaveText('Late run: checked at 17:12');
  expect(state.api.filter((entry) => entry.path === '/api/today'))
    .toEqual([expect.objectContaining({method: 'GET', search: '?date=' + FIXED_DATE, status: 200})]);
  await expectFixtureBoundary(state);
});
