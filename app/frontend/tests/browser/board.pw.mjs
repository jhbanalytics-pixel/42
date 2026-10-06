import {expect, test} from '@playwright/test';
import {expectDeskPageLanding, storedState, watchApi} from './support/older-links.mjs';

const topics = [{id: 'music_amapiano', topic: 'Amapiano', region: 'ZA', momentum: 'rising', series: [100, 150, 200]}];
const creators = [{handle: 'dbn.gogo.fanpage', market: 'ZA', platform: 'tiktok', reach: 1200, posts: 6, series: [600, 900, 1200]}];

async function setup(page, {width = 390, theme = 'midnight', topicTitle = 'Amapiano'} = {}){
  await page.setViewportSize({width, height: 960});
  await page.addInitScript(theme => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    localStorage.setItem('pulse-region', 'ZA');
    localStorage.setItem('oi-theme', theme);
    if (!sessionStorage.getItem('board-fixture-seeded')){
      localStorage.setItem('pulse-watch', JSON.stringify([{id: 'music_amapiano', market: 'za'}]));
      localStorage.setItem('pulse-crm', JSON.stringify([{name: 'dbn.gogo.fanpage'}]));
      localStorage.setItem('pulse-lastlook', JSON.stringify({'topic-pin:za:music_amapiano': {v: 100, at: 1788600000000}, 'voice:dbn.gogo.fanpage': {v: 600, at: 1788600000000}, unrelated: {v: 7, at: 1788600000000}}));
      sessionStorage.setItem('board-fixture-seeded', '1');
    }
  }, theme);
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (route.request().method() !== 'GET') return route.fulfill({status: 501, json: {detail: 'Unexpected method'}});
    if (url.pathname === '/api/health') return route.fulfill({json: {passcode: true}});
    if (url.pathname === '/api/desk' && ['za', 'all'].includes(url.searchParams.get('region'))) return route.fulfill({json: {topics: topics.map(topic => ({...topic, topic: topicTitle})), freshness: {status: 'green', age_hours: 1}}});
    if (url.pathname === '/api/voices' && url.searchParams.get('region') === 'all') return route.fulfill({json: {creators}});
    return route.fulfill({status: 501, json: {detail: 'Missing fixture'}});
  });
  const reads = watchApi(page);
  await page.goto('/#/method');
  const stored = await storedState(page);
  const heading = await expectDeskPageLanding(page, 'board', reads);
  expect(await storedState(page)).toEqual(stored);
  await page.waitForFunction(() => document.documentElement.dataset.productStyles === 'ready');
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => Promise.all(document.getAnimations().filter(a => Number.isFinite(a.effect.getTiming().iterations)).map(a => a.finished.catch(() => {}))));
  return {reads, stored, heading};
}

/* Page port, 3 October 2026: Board read the desk API, which f42-api does not
   serve, so it left the More menu and #/board now opens Alerts, where the
   watches a reader sets live. These journeys drove the Board page itself,
   which no route reaches any more, so each now holds the landing: #/board is
   rewritten to #/alerts and stays there across a reload, Alerts renders in
   the stored theme without sideways scroll at each width, no page links to
   #/board, no desk read (/api/desk, /api/voices) is made, and the pins,
   tracked handles and last looks the reader had stored are left as they
   were. The old titles are kept in the comment above each test. */
for (const width of [390, 1024, 1440]) for (const theme of ['daylight', 'midnight']){
  /* Was: Board identifiers and controls remain readable at ${width} ${theme}. */
  test(`a Board link opens Alerts, readable at ${width} ${theme}, with no desk read`, async ({page}) => {
    const state = await setup(page, {width, theme});
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-dir'))).toBe(theme);
    const lines = await state.heading.evaluate(node => {const range = document.createRange(); range.selectNodeContents(node); return new Set([...range.getClientRects()].filter(box => box.width > 0).map(box => Math.round(box.top))).size;});
    expect(lines).toBe(1);
    const box = await state.heading.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.locator('a[href^="#/board"]')).toHaveCount(0);
    await page.screenshot({path: test.info().outputPath('board.png'), fullPage: true});
    expect(state.reads.filter(entry => / \/api\/(desk|voices)(\?|\/|$)/.test(entry))).toEqual([]);
  });
}

/* Was: a long found-topic word retains a visible title and removal control. */
test('a Board link opens Alerts and leaves a long pinned topic stored as it was', async ({page}) => {
  const topicTitle = 'MicroentrepreneurshipCommunities';
  const state = await setup(page, {topicTitle});
  await expect(page.getByText(topicTitle)).toHaveCount(0);
  await expect(page.locator('.board-card')).toHaveCount(0);
  expect(await storedState(page)).toEqual(state.stored);
  expect(JSON.parse(state.stored['pulse-watch'])).toEqual([{id: 'music_amapiano', market: 'za'}]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

/* Was: Board stores unchanged source values and removes only the selected item. */
test('a Board link opens Alerts without rewriting or removing any stored pin or last look', async ({page}) => {
  const state = await setup(page);
  const stored = () => page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook')));
  const initial = await stored();
  expect(initial['topic-pin:za:music_amapiano']).toEqual({v: 100, at: 1788600000000});
  expect(initial['voice:dbn.gogo.fanpage']).toEqual({v: 600, at: 1788600000000});
  expect(initial.unrelated).toEqual({v: 7, at: 1788600000000});
  await page.reload();
  await expect(page).toHaveURL(/#\/alerts$/);
  await expect(page.getByRole('button', {name: 'Unpin', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Untrack', exact: true})).toHaveCount(0);
  expect(await storedState(page)).toEqual(state.stored);
  expect(JSON.parse(state.stored['pulse-crm'])).toEqual([{name: 'dbn.gogo.fanpage'}]);
  expect(state.reads.filter(entry => / \/api\/(desk|voices)(\?|\/|$)/.test(entry))).toEqual([]);
});
