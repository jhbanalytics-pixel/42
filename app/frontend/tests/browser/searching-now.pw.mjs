import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';

const ORIGIN = 'http://127.0.0.1:4199';
const FIXTURE_PASSCODE = 'searching-now-fixture-only';
const today = JSON.parse(readFileSync(new URL('../../src/ui/__tests__/fixtures/today42.json', import.meta.url), 'utf8'));
const signals = JSON.parse(readFileSync(new URL('../../src/ui/__tests__/fixtures/searching-now.json', import.meta.url), 'utf8'));
const FIXTURE_FIELDS = ['market', 'rank', 'refreshed_at', 'source', 'term'];
const MARKETS = ['ZA', 'NG', 'KE'];
const MARKET_LABELS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya', ALL: 'All'};

expect(Array.isArray(signals)).toBe(true);
for (const signal of signals){
  expect(Object.keys(signal).sort()).toEqual(FIXTURE_FIELDS);
}
expect(signals.some((signal) => signal.rank === null)).toBe(true);
expect(signals.some((signal) => Number.isInteger(signal.rank) && signal.rank > 0)).toBe(true);
expect(signals.some((signal) => /^\d{4}-\d{2}-\d{2}$/.test(signal.refreshed_at))).toBe(true);
expect(signals.some((signal) => /^\d{4}-\d{2}-\d{2}T/.test(signal.refreshed_at))).toBe(true);

async function respond(route, entry, status, body){
  entry.status = status;
  await route.fulfill({
    status,
    contentType: 'application/json; charset=utf-8',
    body: JSON.stringify(body),
  });
}

async function installFixtureBoundary(page, {emptySearch = false} = {}){
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
    if (url.pathname !== '/api/health'
      && request.headers()['x-passcode'] !== FIXTURE_PASSCODE) state.badCredentials.push(method + ' ' + url.pathname);

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
      await respond(route, entry, 200, {...today, searching_now: emptySearch ? [] : signals});
      return;
    }
    if (url.pathname === '/api/alerts' && method === 'GET'){
      await respond(route, entry, 200, {date: today.date, alerts: []});
      return;
    }
    if (url.pathname === '/api/investigations' && method === 'GET'){
      await respond(route, entry, 200, {investigations: []});
      return;
    }
    if (url.pathname === '/api/schedules' && method === 'GET'){
      await respond(route, entry, 200, {schedules: []});
      return;
    }
    if (url.pathname === '/api/discover' && method === 'GET'){
      await respond(route, entry, 200, {
        date: today.date,
        items: [],
        next_cursor: null,
        filters: {kinds: [], states: [], platforms: []},
        searching_now: emptySearch ? [] : signals,
      });
      return;
    }
    if (url.pathname === '/api/discover/radar' && method === 'GET'){
      await respond(route, entry, 200, {date: today.date, market: url.searchParams.get('market') || 'ZA', points: [], held_back_count: 0, note: null});
      return;
    }
    if (method === 'POST' && url.pathname === '/api/ask') state.blockedWrites.push(method + ' ' + url.pathname);
    if (/^\/api\/ask\/[^/]+\/events$/.test(url.pathname)) state.eventStreams.push(method + ' ' + url.pathname);
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
  expect(state.api.some((request) => request.path === '/api/searching-now')).toBe(false);
}

async function expectFits(page, locator){
  const bounds = await locator.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {left: rect.left, right: rect.right, width: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth};
  });
  expect(bounds.scrollWidth).toBeLessThanOrEqual(bounds.width + 1);
  expect(bounds.left).toBeGreaterThanOrEqual(-1);
  expect(bounds.right).toBeLessThanOrEqual(bounds.width + 1);
}

async function expectSearchingNow(page, selectedMarket){
  const main = page.locator('#main-content');
  const visibleMarkets = selectedMarket === 'ALL' ? MARKETS : [selectedMarket];
  const expected = signals.filter((signal) => visibleMarkets.includes(signal.market));
  const groups = main.locator('[data-section="searching-now"] [data-search-market]');
  await expect.poll(async () => (await groups.evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-search-market')))).sort())
    .toEqual(visibleMarkets.slice().sort());

  await expect.poll(async () => (
    await main.locator('[data-section="searching-now"] .searching-now__term').allTextContents()
  ).sort()).toEqual(expected.map((signal) => signal.term).sort());

  const strips = main.locator('[data-section="searching-now"]');
  for (let index = 0; index < await strips.count(); index++){
    const strip = strips.nth(index);
    await expect(strip.getByRole('heading', {name: 'Trending on Google', exact: true})).toBeVisible();
    await expect(strip.locator('.searching-now__caption')).toHaveText('Google search interest, not posts');
    await expect(strip.locator('[data-card]')).toHaveCount(0);
    await expect(strip.locator('a')).toHaveCount(0);
    const stripText = await strip.innerText();
    expect(stripText).not.toMatch(/\b\d[\d,]*\s+posts?\b/i);
    expect(stripText).not.toMatch(/evidence/i);
    await expectFits(page, strip);
  }

  for (const signal of expected){
    const group = main.locator('[data-section="searching-now"] [data-search-market="' + signal.market + '"]');
    const item = main.locator('[data-search-market="' + signal.market + '"] .searching-now__item').filter({hasText: signal.term});
    await expect(group).toHaveCount(1);
    await expect(item).toHaveCount(1);
    await expect(item.locator('.searching-now__term')).toHaveText(signal.term);
    await expect(item.locator('time')).toHaveAttribute('datetime', signal.refreshed_at);
    await expect(item.locator('time')).toHaveText(signal.refreshed_at);
    if (signal.rank === null){
      await expect(item).not.toContainText(/Reported rank\s+\d+/i);
    } else {
      await expect(item).toContainText('Reported rank ' + signal.rank);
      await expect(item).not.toContainText(/national/i);
    }
  }
}

async function todayCardIdentities(locator){
  return locator.evaluateAll((cards) => cards.map((card) => (
    card.getAttribute('data-item-id')
    || card.querySelector('a[href*="#/topic/"]')?.getAttribute('href')
    || card.querySelector('h3')?.textContent?.trim()
  )).filter(Boolean));
}

function discoverRequestCount(state, market){
  const apiMarket = market === 'ALL' ? 'all' : market;
  return state.api.filter((request) => request.path === '/api/discover'
    && new URLSearchParams(request.search).get('market') === apiMarket
    && request.status === 200).length;
}

for (const view of ['today', 'discover']){
  for (const width of [390, 1280]){
    test(view + ' search interest stays market scoped at ' + width + 'px', async ({page}) => {
      await page.setViewportSize({width, height: 900});
      const state = await installFixtureBoundary(page);
      const pageErrors = [];
      page.on('pageerror', (error) => pageErrors.push(error.message));
      if (view === 'today') await page.goto('/#/today?date=' + encodeURIComponent(today.date));
      else await page.goto('/#/explore');

      const main = page.locator('#main-content');
      if (view === 'today') await expect(main.locator('[data-today-loaded]')).toBeVisible();
      else {
        await expect(main.getByRole('heading', {level: 1, name: 'Discover', exact: true})).toBeVisible();
        await expect(main.locator('#d42-feed')).toContainText('No trends have cleared the checks');
        await expect(main.locator('#d42-feed .t42-cards [data-card]')).toHaveCount(0);
      }

      const initialZaCards = view === 'today' ? await todayCardIdentities(main.locator('#t42-cards-ZA [data-card]')) : null;
      const initialZaCardCount = view === 'today' ? initialZaCards.length : null;
      if (view === 'today'){
        expect(initialZaCardCount).toBeGreaterThan(0);
        expect(new Set(initialZaCards).size).toBe(initialZaCards.length);
      }
      let activeMarket = 'ZA';

      for (const market of ['ZA', 'NG', 'KE', 'ALL']){
        const tab = main.getByRole('tab', {name: MARKET_LABELS[market], exact: true});
        const previousDiscoverReads = discoverRequestCount(state, market);
        await tab.click();
        await expect(tab).toHaveAttribute('aria-selected', 'true');
        if (view === 'discover' && market !== activeMarket){
          await expect.poll(() => discoverRequestCount(state, market)).toBeGreaterThan(previousDiscoverReads);
          await expect(main.locator('#d42-feed')).toContainText('No trends have cleared the checks');
          await expect(main.locator('#d42-feed .t42-cards [data-card]')).toHaveCount(0);
        }
        await expectSearchingNow(page, market);
        if (view === 'today' && market === 'KE'){
          await expect(main.locator('.t42-market[data-market="KE"] #t42-cards-KE [data-card]')).toHaveCount(0);
        }
        activeMarket = market;
      }

      if (view === 'today'){
        const zaTab = main.getByRole('tab', {name: MARKET_LABELS.ZA, exact: true});
        await zaTab.click();
        await expect(zaTab).toHaveAttribute('aria-selected', 'true');
        await expectSearchingNow(page, 'ZA');
        const revisitedZaCards = await todayCardIdentities(main.locator('#t42-cards-ZA [data-card]'));
        expect(revisitedZaCards.length).toBe(initialZaCardCount);
        expect(revisitedZaCards).toEqual(initialZaCards);
      }
      if (view === 'discover'){
        await expect(main.locator('#d42-feed')).toContainText('No trends have cleared the checks');
        await expect(main.locator('#d42-feed .t42-cards [data-card]')).toHaveCount(0);
      }
      await expectBoundaryClean(state);
      expect(pageErrors).toEqual([]);
    });
  }
}

for (const view of ['today', 'discover']){
  test(view + ' hides the strip when the payload is empty', async ({page}) => {
    const state = await installFixtureBoundary(page, {emptySearch: true});
    const pageErrors = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    if (view === 'today') await page.goto('/#/today?date=' + encodeURIComponent(today.date));
    else await page.goto('/#/explore');

    const main = page.locator('#main-content');
    if (view === 'today') await expect(main.locator('[data-today-loaded]')).toBeVisible();
    else {
      await expect(main.getByRole('heading', {level: 1, name: 'Discover', exact: true})).toBeVisible();
      await expect(main.locator('#d42-feed')).toContainText('No trends have cleared the checks');
      await expect(main.locator('#d42-feed .t42-cards [data-card]')).toHaveCount(0);
    }
    await expect(main.locator('[data-section="searching-now"]')).toHaveCount(0);
    await expectBoundaryClean(state);
    expect(pageErrors).toEqual([]);
  });
}
