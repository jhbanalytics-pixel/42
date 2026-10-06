import {expect, test} from '@playwright/test';
import {deskReads, expectDeskPageLanding, expectOlderLink, serveLandingReads, watchApi} from './support/older-links.mjs';

const topics = ['ZA', 'NG'].map(region => ({
  id: 'music_amapiano', region, topic: `Music in ${region}`, label: `Music · ${region}`,
  momentum: 'rising', score: .8, seed: null, delta: 0, sources: 2, creators: 1,
  sentiment: .2, velocity: 'moderate', age: '1d', why: 'A bounded fixture observation.',
  series: Array.from({length: 30}, (_, index) => .4 + index / 100),
  platforms: [['TikTok', 100]], creators_list: ['@maker'], voices: [],
  brief: {trend: `Observed music discussion in ${region}.`, relevance: 'A sampled cultural signal.', idea: {tool: 'Editorial response', text: 'Review the supplied sources.'}, prompt: {nano: '', lyria: ''}},
}));
const creators = ['ZA', 'NG'].map(market => ({handle: 'maker', market, platform: 'tiktok', reach: 100, posts: 2}));
const profile = {handle: 'maker', market: 'za', market_label: 'South Africa', markets: ['za', 'ng'], platform: 'tiktok', platforms: ['tiktok'], reach: 200, posts: 4, total_engagement: 200, avg_engagement: 50, top_engagement: 100, topics: [{id: 'music_amapiano', label: 'Music'}], reach_series: [], wall_count: 1, wall: [{id: 'post_1', text: 'A music source record.', platform: 'TikTok', market: 'za', engagement: 100, published_at: '2026-09-05T10:00:00Z', collected_at: '2026-09-05T11:00:00Z', url: 'https://example.test/music', topic: 'music_amapiano', topic_label: 'Music'}]};

async function setup(page, {invalidFirst = false, empty = false, late = false, recorded = false, recordedHandle = '@maker', initialMarket = 'ALL', wrongMarket = false, wrongRecordedMarket = false, emptyBrief = false} = {}){
  const reads = [], unknown = [], api = watchApi(page);
  let deskReadCount = 0;
  await page.addInitScript(market => { localStorage.setItem('pulse_passcode', 'browser-fixture-only'); if (!localStorage.getItem('pulse-region')) localStorage.setItem('pulse-region', market); }, initialMarket);
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), path = url.pathname;
    reads.push(path + url.search);
    const selected = wrongMarket ? 'NG' : url.searchParams.get('region')?.toUpperCase();
    let json;
    if (path === '/api/health') json = {passcode: true};
    else if (path === '/api/desk') {
      deskReadCount++;
      const rows = selected === 'ALL' ? topics : topics.filter(topic => topic.region === selected);
      json = invalidFirst && deskReadCount === 1 ? {error: 'Missing topic records'} : {topics: empty ? rows.map(topic => ({...topic, creators_list: []})) : late ? Array.from({length: 7}, (_, index) => ({...topics[0], id: 'unmatched' + index, score: 1, creators_list: []})).concat(rows) : rows, bridges: recorded ? [{h: recordedHandle, mk: wrongRecordedMarket ? 'NG' : 'ZA', from: 'Music', to: 'Food', note: 'A separately collected record.', eng: '200'}] : [], lexicon: [], freshness: {status: 'green', age_hours: 1}, updated: '2026-09-06'};
    } else if (path === '/api/voices') json = {region: selected, market_label: selected === 'ALL' ? 'All markets' : selected, creators: late ? Array.from({length: 12}, (_, index) => ({...creators[0], handle: 'unmatched' + index, reach: 1000})).concat(creators) : selected === 'ALL' ? creators : creators.filter(row => row.market === selected)};
    else if (path === '/api/topic/music_amapiano') { json = topics.find(topic => topic.region === selected); if (json && emptyBrief) json = {...json, has_brief: false, brief: {trend: '', relevance: '', idea: {tool: 'Editorial response', text: ''}, prompt: {nano: '', lyria: ''}}}; }
    else if (path === '/api/desk/opportunity/music_amapiano') json = {opportunity: `A bounded opportunity in ${selected}.`};
    else if (path === '/api/creator/maker') json = profile;
    else { unknown.push(path); return route.fulfill({status: 404, json: {detail: 'Missing fixture'}}); }
    if (!json) return route.fulfill({status: 404, json: {detail: 'Missing scoped fixture'}});
    await route.fulfill({status: 200, json});
  });
  await serveLandingReads(page);
  return {reads, unknown, api};
}

/* Page port, 3 October 2026: Network read the desk API (/api/desk,
   /api/voices), which f42-api does not serve, so it left the More menu and
   #/network now opens Communities, 42's own page for who drives which topics
   (src/legacyRoutes.js). The older topic and creator pages it linked to read
   /api/topic and /api/creator, and their links now say they are from an
   older version (src/olderLink42.jsx). No route reaches the Network graph, so
   these journeys hold the landing: #/network is rewritten to #/communities
   and stays there across a reload, Communities renders from its own
   /api/communities read in the stored market, no page links to #/network,
   and no desk, topic, opportunity or creator read is made. The old title is
   kept in the comment above each test. */
const communityReads = state => state.api.filter(entry => entry.startsWith('GET /api/communities?'));
const noDeskReads = state => {
  expect(deskReads(state.api)).toEqual([]);
  expect(state.reads.filter(path => path.startsWith('/api/desk') || path.startsWith('/api/voices') || path.startsWith('/api/topic/') || path.startsWith('/api/creator/'))).toEqual([]);
};

/* Was: Network retains late-ranked matches in the rendered graph. */
test('a Network link opens Communities from its own read, with no desk read', async ({page}) => {
  const state = await setup(page, {late: true});
  await expectDeskPageLanding(page, 'network', state.api);
  await expect(page.locator('.net-edge')).toHaveCount(0);
  expect(communityReads(state).length).toBeGreaterThanOrEqual(2);
  noDeskReads(state);
  expect(state.unknown).toEqual([]);
});

/* Was: a topic without brief content cannot open an export or request an opportunity. */
test('an older topic link cannot open an export or request an opportunity', async ({page}) => {
  const state = await setup(page, {initialMarket: 'ZA', emptyBrief: true});
  await expectOlderLink(page, 'topic', '#/topic/music_amapiano', state.api);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.getByText('Signal brief', {exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Copy brief', exact: true})).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'PDF', exact: true})).toHaveCount(0);
  expect(state.reads.some(path => path.startsWith('/api/desk/opportunity/'))).toBe(false);
  noDeskReads(state);
});

/* Was: Network retries an invalid successful envelope without calling it empty. */
test('with an invalid desk envelope waiting, a Network link opens Communities and never reads it', async ({page}) => {
  const state = await setup(page, {invalidFirst: true});
  await expectDeskPageLanding(page, 'network', state.api);
  await expect(page.getByText('The returned topic or voice records could not be verified.', {exact: true})).toHaveCount(0);
  await expect(page.locator('[data-network-coverage]')).toHaveCount(0);
  noDeskReads(state);
});

/* Was: Network withholds wrong-market ${...}. */
for (const wrongRecordedMarket of [false, true]) {
  test(`with wrong-market ${wrongRecordedMarket ? 'recorded links' : 'graph records'} waiting, a Network link opens Communities in the stored market`, async ({page}) => {
    const state = await setup(page, {initialMarket: 'ZA', wrongMarket: !wrongRecordedMarket, recorded: true, wrongRecordedMarket});
    const heading = await expectDeskPageLanding(page, 'network', state.api);
    await expect(heading).toHaveText('Communities in South Africa');
    expect(communityReads(state).every(entry => entry.endsWith('market=ZA'))).toBe(true);
    await expect(page.locator('.net-edge')).toHaveCount(0);
    await expect(page.locator('.net-recorded-links')).toHaveCount(0);
    noDeskReads(state);
  });
}

/* Was: Network empty state leaves the relationship window unknown. */
test('with an empty desk waiting, a Network link opens Communities without a Network empty state', async ({page}) => {
  const state = await setup(page, {empty: true});
  await expectDeskPageLanding(page, 'network', state.api);
  await expect(page.getByText('No matching links in the returned records', {exact: true})).toHaveCount(0);
  await expect(page.getByText('How these links are matched', {exact: true})).toHaveCount(0);
  noDeskReads(state);
});

/* Was: separately supplied fourteen-day records remain visible without becoming graph edges. */
test('a Network link opens Communities at 390, and an older creator link says it is older', async ({page}) => {
  const state = await setup(page, {empty: true, recorded: true});
  await page.setViewportSize({width: 390, height: 1000});
  await expectDeskPageLanding(page, 'network', state.api);
  await expect(page.locator('.net-edge')).toHaveCount(0);
  await expect(page.getByRole('heading', {name: 'Recorded links across scenes'})).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path: test.info().outputPath('recorded-links.png'), fullPage: true});
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await expect(page.getByRole('heading', {name: '@maker', exact: true})).toHaveCount(0);
  noDeskReads(state);
});

/* Was: long recorded handles remain inside their tablet cards. */
test('a Network link opens Communities inside a tablet viewport', async ({page}) => {
  const state = await setup(page, {recorded: true, recordedHandle: '@abcdefghijklmnopqrstuvwxyzabcdef'});
  await page.setViewportSize({width: 1024, height: 1000});
  await expectDeskPageLanding(page, 'network', state.api);
  await page.evaluate(() => document.fonts.ready);
  await expect(page.getByText('@abcdefghijklmnopqrstuvwxyzabcdef')).toHaveCount(0);
  expect(await page.locator('#main-content h1').evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  noDeskReads(state);
});

/* Was: Network topic actions retain their recorded market at ${width}. */
for (const width of [390, 1440]) {
  test(`a Network link opens Communities in the stored market at ${width}, and an older scoped topic link reads nothing`, async ({page}) => {
    const state = await setup(page, {initialMarket: 'NG'});
    await page.setViewportSize({width, height: 1000});
    const heading = await expectDeskPageLanding(page, 'network', state.api);
    await expect(heading).toHaveText('Communities in Nigeria');
    expect(communityReads(state).length).toBeGreaterThanOrEqual(2);
    expect(communityReads(state).every(entry => entry.endsWith('market=NG'))).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expectOlderLink(page, 'topic', '#/topic/music_amapiano?region=ng', state.api);
    await page.reload();
    await expect(page.getByRole('heading', {level: 1, name: 'This topic link is from an older version'})).toBeVisible();
    expect(state.reads.filter(path => path.startsWith('/api/topic/'))).toEqual([]);
    expect(state.reads.some(path => path.startsWith('/api/desk/opportunity/'))).toBe(false);
    expect(state.unknown).toEqual([]);
  });
}

/* Was: Network creator action opens the disclosed combined handle profile. */
test('an older creator link says it is older, survives reload and reads no profile', async ({page}) => {
  const state = await setup(page);
  await expectDeskPageLanding(page, 'network', state.api);
  await expectOlderLink(page, 'creator', '#/creator/maker', state.api);
  await expect(page.getByRole('link', {name: 'View original post'})).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('heading', {level: 1, name: 'This creator link is from an older version'})).toBeVisible();
  noDeskReads(state);
  expect(state.unknown).toEqual([]);
});

/* Was: Network identity layout at ${width} ${theme}. */
for (const width of [390, 1024, 1440]) for (const theme of ['daylight', 'midnight']) {
  test(`a Network link opens Communities laid out at ${width} ${theme}`, async ({page}) => {
    const state = await setup(page);
    await page.addInitScript(value => localStorage.setItem('oi-theme', value), theme);
    await page.setViewportSize({width, height: 1000});
    const heading = await expectDeskPageLanding(page, 'network', state.api);
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-dir'))).toBe(theme);
    await page.evaluate(() => document.fonts.ready);
    await page.evaluate(() => Promise.all(document.getAnimations().filter(animation => Number.isFinite(animation.effect.getTiming().iterations)).map(animation => animation.finished.catch(() => {}))));
    const box = await heading.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.locator('.net-node--topic, .net-node--creator, .net-topic-links')).toHaveCount(0);
    await page.screenshot({path: test.info().outputPath('network.png'), fullPage: true});
    noDeskReads(state);
  });
}
