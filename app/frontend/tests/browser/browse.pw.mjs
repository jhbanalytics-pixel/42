import {expect, test} from '@playwright/test';
import {lensRosterFixture, questionCoverageFixture} from './support/capability-harness.mjs';
import {deskReads, watchApi} from './support/older-links.mjs';

const query = 'football & street culture';
const LOCAL_ORIGINS = new Set(['http://127.0.0.1:4176', 'http://127.0.0.1:4199']);
const baseResult = {
  query, market: 'za', total_matches: 14, thin: false, broad: false,
  volume_series: Array.from({length: 30}, (_, i) => ({date: new Date(Date.UTC(2026, 7, 8 + i)).toISOString().slice(0, 10), n: i >= 23 ? 2 : 0})),
  platform_split: [{platform: 'tiktok', n: 14}], market_split: [{market: 'za', n: 14}],
  trends: [], slang: [], creators: [{handle: 'local.player', mentions: 2}], overall_read: '', limited_voice: false,
  quotes: [
    {text: 'Street football brings the neighbours together.', platform: 'tiktok', market: 'za', engagement: 40, handle: 'local.player'},
    {text: 'The coach shares the training schedule.', platform: 'tiktok', market: 'za', engagement: 30, handle: 'local.coach'},
    {text: 'A nearby club lists its next friendly match.', platform: 'tiktok', market: 'za', engagement: 20, handle: 'local.club'},
  ],
};

async function setup(page, {thin = false, cached = false, failureStatus = 0, failurePayload = null, deskFailureStatus = 0, missingSearchRoute = false, holdSearch = false, discoverStatus = 200} = {}){
  const searches = [], passcodes = [], generations = [], unknown = [], discover = [], api = watchApi(page);
  let releaseSearch = null;
  const result = structuredClone(baseResult);
  if (thin){result.thin = true; result.total_matches = 3; result.volume_series = result.volume_series.map((row, i) => ({...row, n: i === 29 ? 3 : 0})); result.platform_split[0].n = 3; result.market_split[0].n = 3; result.overall_read = 'Only a small evidence sample was retrieved. Read these examples with that limit.';}
  if (cached){result.trends = [{statement: 'A saved reading of street football.', how_to_use: 'Review the supporting sources.', momentum: 'Steady', evidence_count: 3, platforms: ['tiktok'], markets: ['za']}]; result.overall_read = 'An existing interpretation of the retrieved conversation.';}
  await page.addInitScript(() => {localStorage.setItem('pulse_passcode', 'browser-fixture-only'); localStorage.setItem('pulse-region', 'ZA');});
  await page.routeWebSocket('**/*', webSocket => webSocket.close());
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (!LOCAL_ORIGINS.has(url.origin)) return route.abort('blockedbyclient');
    if (!url.pathname.startsWith('/api/')) return route.continue();
    let json;
    if (url.pathname === '/api/health') json = {passcode: true};
    else if (url.pathname === '/api/desk') {
      if (deskFailureStatus) return route.fulfill({status: deskFailureStatus, json: {error: 'not_found', message: 'No such API route.'}});
      json = {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}};
    }
    else if (url.pathname === '/api/ask' && request.method() === 'POST') {
      generations.push({method: request.method(), path: url.pathname});
      return route.fulfill({status: 501, json: {detail: 'No generation is permitted in this journey'}});
    }
    else if (url.pathname === '/api/ask' && request.method() === 'GET') {
      searches.push({query: url.searchParams.get('q'), market: url.searchParams.get('market')});
      passcodes.push(request.headers()['x-passcode']);
      if (holdSearch && searches.length === 1){
        await new Promise(resolve => { releaseSearch = resolve; });
        return route.fulfill({json: result}).catch(() => {});
      }
      if (missingSearchRoute) return route.fulfill({status: 404, json: {error: 'not_found', message: 'No such API route.'}});
      if (failureStatus && searches.length === 1) return route.fulfill({status: failureStatus, json: failurePayload || {detail: 'Archive read unavailable'}});
      json = result;
    }
    else if (url.pathname === '/api/discover' || url.pathname === '/api/discover/radar') {
      discover.push({path: url.pathname + url.search, passcode: request.headers()['x-passcode']});
      if (discoverStatus !== 200) return route.fulfill({status: discoverStatus, json: {error: 'unauthorized', message: 'Passcode required.'}});
      json = url.pathname === '/api/discover' ? {items: [], next_cursor: null, held_back: {count: 0, items: []}, filters: {}} : {market: url.searchParams.get('market'), items: []};
    }
    else if (url.pathname === '/api/research/recent') json = {artifacts: []};
    else if (url.pathname === '/api/v2/investigations/list') json = {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_index_v1', investigations: [], total_count: 0, truncated: false};
    else if (url.pathname === '/api/v2/investigations/scopes') json = {contract_version: 'intelligence_dossier_v1', resource_version: 'investigation_scope_index_v1', scopes: [], default_scope_id: null};
    else if (url.pathname === '/api/chat/lenses') json = lensRosterFixture();
    else if (url.pathname === '/api/chat/coverage') json = questionCoverageFixture();
    else if (url.pathname === '/api/chat/send') {generations.push(request.postDataJSON()); return route.fulfill({status: 501, json: {detail: 'No generation is permitted in this journey'}});}
    else {unknown.push(url.pathname); return route.fulfill({status: 501, json: {detail: 'Missing fixture'}});}
    return route.fulfill({json});
  });
  /* Page port, 3 October 2026: Browse read the desk and the archive search,
     which f42-api does not serve, so it left the More menu and #/browse now
     opens Discover (src/legacyRoutes.js). The search box is gone; the
     landing is held instead: the hash is rewritten to #/explore, Discover
     renders and reads its own /api/discover in the stored market with the
     passcode, and no desk read, archive search or generation is made. */
  await page.goto('/#/browse');
  await expect(page).toHaveURL(/#\/explore$/);
  if (discoverStatus === 200) await expect(page.locator('#main-content h1')).toHaveText('Discover');
  await expect.poll(() => discover.some(entry => entry.path.startsWith('/api/discover?'))).toBe(true);
  await expect(page.getByRole('textbox', {name: 'Search the post archive'})).toHaveCount(0);
  await expect(page.locator('a[href^="#/browse"]')).toHaveCount(0);
  return {searches, passcodes, generations, unknown, discover, api, releaseSearch: () => releaseSearch && releaseSearch()};
}

/* The landing's own checks: no archive search, no generation, no desk read,
   and every Discover read carried the stored market and the passcode. */
function expectNoBrowseReads(state){
  expect(state.searches).toEqual([]);
  expect(state.generations).toEqual([]);
  expect(deskReads(state.api)).toEqual([]);
  expect(state.discover.every(entry => /[?&]market=ZA(&|$)/.test(entry.path) && entry.passcode === 'browser-fixture-only')).toBe(true);
}

/* Was: retrieved evidence does not become trusted and Ask opens an unsubmitted draft. */
test('a Browse link opens Discover with no archive search, trust label or generation, and Ask stays an empty draft', async ({page}) => {
  const state = await setup(page);
  await expect(page.getByText('Broad enough to trust', {exact: true})).toHaveCount(0);
  await expect(page.getByText('Retrieved evidence', {exact: true})).toHaveCount(0);
  await expect(page.getByRole('link', {name: 'Ask about this search', exact: true})).toHaveCount(0);
  await page.goto('/#/ask');
  await expect(page.getByRole('textbox', {name: 'Your question'})).toHaveValue('');
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expectNoBrowseReads(state);
  expect(state.unknown).toEqual([]);
});

/* Was: a limited sample stays visibly limited without implying there is no trend. */
test('a Browse link with a thin archive opens Discover without a limited-evidence label or trend card', async ({page}) => {
  const state = await setup(page, {thin: true});
  await expect(page.getByText('Limited retrieved evidence', {exact: true})).toHaveCount(0);
  await expect(page.locator('.brw-trend-card')).toHaveCount(0);
  expectNoBrowseReads(state);
});

/* Was: a saved interpretation remains visible with its source-review limitation. */
test('a Browse link with a saved interpretation opens Discover without showing that interpretation', async ({page}) => {
  const state = await setup(page, {cached: true});
  await expect(page.getByText('A saved reading of street football.', {exact: true})).toHaveCount(0);
  await expect(page.getByText('Broad enough to trust', {exact: true})).toHaveCount(0);
  expectNoBrowseReads(state);
});

/* Was: the current missing API routes produce a clear unavailable state, not a false empty result. */
test('with the desk and archive routes missing, a Browse link opens Discover rather than an unavailable Browse', async ({page}) => {
  const state = await setup(page, {deskFailureStatus: 404, missingSearchRoute: true});
  await expect(page.getByRole('heading', {name: 'Browse', exact: true})).toHaveCount(0);
  await expect(page.getByText('Browse is not available yet', {exact: false})).toHaveCount(0);
  expect(state.passcodes).toEqual([]);
  expectNoBrowseReads(state);
  expect(state.unknown).toEqual([]);
});

/* Was: a different 404 stays a read failure instead of being mislabeled as a missing route. */
test('with an archive 404 waiting, a Browse link opens Discover and offers no archive retry', async ({page}) => {
  const state = await setup(page, {failureStatus: 404, failurePayload: {error: 'not_found', message: 'No such archive record.'}});
  await expect(page.getByRole('button', {name: 'Retry search', exact: true})).toHaveCount(0);
  await expect(page.getByText('could not be read', {exact: false})).toHaveCount(0);
  expectNoBrowseReads(state);
});

/* Was: a failed archive search has a manual retry that can recover without generation. */
test('with an archive failure waiting, a Browse link opens Discover and survives a reload without searching', async ({page}) => {
  const state = await setup(page, {failureStatus: 503});
  await page.reload();
  await expect(page).toHaveURL(/#\/explore$/);
  await expect(page.locator('#main-content h1')).toHaveText('Discover');
  await expect(page.getByRole('button', {name: 'Retry search', exact: true})).toHaveCount(0);
  expectNoBrowseReads(state);
});

/* Was: a search that never settles ends after 30 seconds with a manual retry. */
test('with an archive search that never settles, a Browse link opens Discover and starts no search in 30 seconds', async ({page}) => {
  await page.clock.install({time: new Date('2026-10-01T16:00:00.000Z')});
  const state = await setup(page, {holdSearch: true});
  try {
    await expect(page.getByText('Searching the archive', {exact: true})).toHaveCount(0);
    await page.clock.fastForward(30_000);
    await expect(page.getByText('taking too long', {exact: false})).toHaveCount(0);
    await expect(page.getByRole('button', {name: 'Retry search', exact: true})).toHaveCount(0);
    expectNoBrowseReads(state);
  } finally {
    state.releaseSearch();
  }
});

/* Was: an archive authentication failure reaches the existing passcode gate. */
test('a Browse link whose Discover read fails authentication reaches the existing passcode gate', async ({page}) => {
  const state = await setup(page, {discoverStatus: 401});
  await expect(page.locator('input[type="password"]')).toBeVisible();
  expect(state.searches).toEqual([]);
  expect(deskReads(state.api)).toEqual([]);
});
