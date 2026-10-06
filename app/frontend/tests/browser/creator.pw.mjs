import {expect, test} from '@playwright/test';
import {deskReads, expectDeskPageLanding, expectOlderLink, serveLandingReads, watchApi} from './support/older-links.mjs';

const profile = (handle) => ({
  handle, market: 'za', market_label: 'South Africa', markets: ['za', 'ng'],
  platform: 'tiktok', platforms: ['tiktok', 'instagram'], reach: 1200, posts: 2,
  total_engagement: 1200, avg_engagement: 600, top_engagement: 1200,
  rank: 2, rank_total: 30, topics: [{id: 'music_amapiano', label: 'Amapiano'}],
  read: 'Unsupported potential audience narrative', angle: 'Unsupported audience assertion',
  reach_series: [{date: '2026-09-04', reach: 0}, {date: '2026-09-05', reach: 1200}],
  wall_count: 1, wall: [{id: 'post_1', text: `${handle} shares a source-backed music observation.`,
    platform: 'TikTok', market: 'za', engagement: 0, age: '1d',
    published_at: '2026-09-05T10:00:00Z', collected_at: '2026-09-05T11:00:00Z',
    url: 'https://example.test/post_1', topic: 'music_amapiano', topic_label: 'Amapiano'}],
});

async function setup(page, {failFirst = false, mismatch = false, mismatchFirst = false} = {}){
  const unknown = [], reads = [], api = watchApi(page);
  await page.addInitScript(() => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    localStorage.setItem('pulse-region', 'ZA');
  });
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    let json;
    if (path === '/api/health') json = {passcode: true};
    else if (path === '/api/auth/verify') json = {ok: true};
    else if (path === '/api/desk') json = {topics: [{id: 'music_amapiano', region: 'ZA', topic: 'Amapiano', score: .9, creators_list: ['@maker']}], lexicon: [], freshness: {status: 'green', age_hours: 1}};
    else if (path === '/api/voices') json = {region: 'za', market_label: 'South Africa', creators: [{handle: 'maker', market: 'ZA', platform: 'tiktok', reach: 1200, posts: 2}]};
    else if (path.startsWith('/api/creator/')) {
      reads.push(path);
      if (failFirst && reads.length === 1) return route.fulfill({status: 503, json: {detail: 'Unavailable'}});
      json = profile(mismatch || (mismatchFirst && reads.length === 1) ? 'another_handle' : decodeURIComponent(path.slice('/api/creator/'.length)));
    } else { unknown.push(path); return route.fulfill({status: 404, json: {detail: 'Missing fixture'}}); }
    await route.fulfill({status: 200, json});
  });
  await serveLandingReads(page);
  return {reads, unknown, api};
}

/* Page port, 3 October 2026: the older creator page read /api/creator, which
   f42-api does not serve, so #/creator/<handle> now says the link is from an
   older version and offers Communities and Discover (src/olderLink42.jsx);
   42's own creator pages are #/creators/<id>. Network, which linked here,
   now opens Communities. No route reaches the old profile, so these journeys
   hold the older link instead: the notice and its two ways on, the same after
   a reload or a hash change, no Track control, no profile read, no desk read,
   and the tracked handles the reader had stored left exactly as they were.
   The old title is kept in the comment above each test. */
const noProfileReads = state => {
  expect(state.reads).toEqual([]);
  expect(deskReads(state.api)).toEqual([]);
};

/* Was: Network opens the selected creator and reload preserves source-backed profile. */
test('Network opens Communities, an older creator link says it is older, and reload keeps that with no profile read', async ({page}) => {
  const state = await setup(page);
  await expectDeskPageLanding(page, 'network', state.api);
  await expect(page.locator('.net-node--creator')).toHaveCount(0);
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await expect(page.getByRole('link', {name: 'View original post'})).toHaveCount(0);
  await expect(page.getByText('Unsupported audience assertion')).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('heading', {level: 1, name: 'This creator link is from an older version'})).toBeVisible();
  noProfileReads(state);
  expect(state.unknown).toEqual([]);
});

/* Was: creator tracking persists and can be removed. */
test('an older creator link offers no Track control and leaves stored tracking as it was', async ({page}) => {
  const state = await setup(page);
  await page.addInitScript(() => { if (!sessionStorage.getItem('crm-seeded')){ localStorage.setItem('pulse-crm', JSON.stringify([{name: 'maker'}])); sessionStorage.setItem('crm-seeded', '1'); } });
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await expect(page.getByRole('button', {name: 'Track this handle'})).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('button', {name: 'Tracking handle'})).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-crm')))).toEqual([{name: 'maker'}]);
  noProfileReads(state);
});

/* Was: creator provenance and separated handle retain their intended typography. */
test('an older creator link with a dotted handle keeps its notice on screen at 390 and never prints the handle', async ({page}) => {
  const state = await setup(page);
  await page.setViewportSize({width: 390, height: 1000});
  await expectOlderLink(page, 'creator', '#/creator/dbn.gogo.fanpage', state.api);
  const heading = page.getByRole('heading', {level: 1, name: 'This creator link is from an older version'});
  await page.evaluate(() => document.fonts.ready);
  const box = await heading.boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
  await expect(page.getByText('dbn.gogo.fanpage')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  noProfileReads(state);
});

/* Was: profile errors retry without falsely claiming no records. */
test('with a failing profile read waiting, an older creator link reads nothing and offers no retry', async ({page}) => {
  const state = await setup(page, {failFirst: true});
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await expect(page.getByText('The profile could not load', {exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Try again'})).toHaveCount(0);
  noProfileReads(state);
});

/* Was: mismatched profile identity is withheld. */
test('with a mismatched profile waiting, an older creator link shows no profile at all', async ({page}) => {
  const state = await setup(page, {mismatch: true});
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await expect(page.getByRole('heading', {name: '@another_handle'})).toHaveCount(0);
  await expect(page.getByRole('heading', {name: '@maker'})).toHaveCount(0);
  noProfileReads(state);
});

/* Was: a second handle replaces the first profile without stale content. */
test('a second older creator link keeps the same notice and no handle content', async ({page}) => {
  const state = await setup(page);
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await page.evaluate(() => { location.hash = '#/creator/another'; });
  await expect(page).toHaveURL(/#\/creator\/another$/);
  await expect(page.getByRole('heading', {level: 1, name: 'This creator link is from an older version'})).toBeVisible();
  await expect(page.getByRole('heading', {name: '@another', exact: true})).toHaveCount(0);
  await expect(page.getByText('maker shares a source-backed music observation.')).toHaveCount(0);
  noProfileReads(state);
});

/* Was: a rejected successful response can be refreshed. */
test('with a rejected profile waiting, an older creator link reads nothing and offers no refresh', async ({page}) => {
  const state = await setup(page, {mismatchFirst: true});
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await expect(page.getByText('The profile could not be verified', {exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Try again'})).toHaveCount(0);
  noProfileReads(state);
});

/* Was: mixed-case tracking stays compatible with My board. */
test('mixed-case tracking stays stored through an older creator link and the Board landing on Alerts', async ({page}) => {
  const state = await setup(page);
  await page.addInitScript(() => localStorage.setItem('pulse-crm', JSON.stringify([{name: 'Maker'}])));
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await expect(page.getByRole('link', {name: 'My board', exact: true})).toHaveCount(0);
  await expectDeskPageLanding(page, 'board', state.api);
  await expect(page.locator('.board-card')).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-crm')))).toEqual([{name: 'Maker'}]);
  noProfileReads(state);
});

/* Was: creator layout and controls at ${width} ${theme}. */
for (const width of [390, 1024, 1440]) for (const theme of ['daylight', 'midnight']) {
  test(`an older creator link's notice and ways on at ${width} ${theme}`, async ({page}) => {
    const state = await setup(page);
    await page.addInitScript(value => localStorage.setItem('oi-theme', value), theme);
    await page.setViewportSize({width, height: 960});
    await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-dir'))).toBe(theme);
    await page.evaluate(() => document.fonts.ready);
    await page.evaluate(() => Promise.all(document.getAnimations().filter(a => Number.isFinite(a.effect.getTiming().iterations)).map(a => a.finished.catch(() => {}))));
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const link = page.getByRole('link', {name: 'Open Communities', exact: true});
    await link.focus();
    expect(await link.evaluate(node => getComputedStyle(node).outlineStyle)).not.toBe('none');
    const before = await link.boundingBox();
    await link.hover();
    expect(await link.boundingBox()).toEqual(before);
    expect(before.x + before.width).toBeLessThanOrEqual(width);
    await page.screenshot({path: test.info().outputPath('creator.png'), fullPage: true});
    noProfileReads(state);
  });
}
