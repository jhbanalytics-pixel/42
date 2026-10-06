import {expect, test} from '@playwright/test';
import {expectDeskPageLanding, expectOlderLink, storedState, watchApi} from './support/older-links.mjs';
const id = 'sports_football';
const records = ['KE', 'NG'].map(region => ({id, region, topic: 'Football in ' + region, label: 'Football', momentum: 'steady', score: .5, series: [100, region === 'KE' ? 200 : 100], platforms: [], creators_list: [], voices: [], brief: null, has_brief: false}));

async function setup(page, {pins = [], priors = {}, reverse = false, invalid = false, failOnce = false} = {}){
  const reads = [], api = watchApi(page);
  let failed = false;
  await page.addInitScript(({pins, priors}) => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    if (!localStorage.getItem('pulse-region')) localStorage.setItem('pulse-region', 'NG');
    if (!localStorage.getItem('market-pins-seeded')){
      localStorage.setItem('pulse-watch', JSON.stringify(pins));
      localStorage.setItem('pulse-lastlook', JSON.stringify(priors));
      localStorage.setItem('market-pins-seeded', '1');
    }
  }, {pins, priors});
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()), market = url.searchParams.get('region')?.toUpperCase();
    reads.push(url.pathname + url.search);
    if (route.request().method() !== 'GET') return route.fulfill({status: 501, json: {detail: 'Unexpected method'}});
    if (url.pathname === '/api/health') return route.fulfill({json: {passcode: true}});
    if (url.pathname === '/api/desk'){
      if (market === 'ALL' && failOnce && !failed){failed = true; return route.fulfill({status: 503, json: {detail: 'Read unavailable'}});}
      const topics = market === 'ALL' ? reverse ? [...records].reverse() : records : records.filter(row => row.region === market);
      return route.fulfill({json: market === 'ALL' && invalid ? {error: 'Missing records'} : {topics, freshness: {status: 'green', age_hours: 1}}});
    }
    if (url.pathname === '/api/voices') return route.fulfill({json: {creators: []}});
    if (url.pathname === '/api/topic/' + id) return route.fulfill({json: records.find(row => row.region === market)});
    return route.fulfill({status: 501, json: {detail: 'Missing fixture'}});
  });
  return {reads, api};
}

/* Page port, 3 October 2026: the older topic page read /api/topic and Board
   read /api/desk, neither of which f42-api serves. #/board now opens Alerts,
   an older #/topic/<id> that names a 42 item (64 hex characters) opens the
   42 topic page #/t/<id> with its market, and any other older topic link says
   it is from an older version, reads nothing and offers no Watch control.
   No route reaches the old Board or topic pages, so these journeys hold that
   landing instead, with the same stores: the pins and last looks the reader
   had stored are never rewritten, cleared or given a fabricated reading by
   the landing, and no /api/topic or /api/desk read is made. The old title is
   kept in the comment above each test. */
const ITEM = 'ab'.repeat(32);
const deskOrTopic = state => state.reads.filter(path => path.startsWith('/api/topic/') || path.startsWith('/api/desk') || path.startsWith('/api/voices'));

/* Was: a topic pin retains its actual market through Board and reload. */
test('an older topic link offers no Watch and Board opens Alerts with the stored pin untouched', async ({page}) => {
  const state = await setup(page, {pins: [{id, market: 'ng'}]});
  await expectOlderLink(page, 'topic', '#/topic/' + id, state.api);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-watch')))).toEqual([{id, market: 'ng'}]);
  await expectDeskPageLanding(page, 'board', state.api);
  await page.reload();
  await expect(page).toHaveURL(/#\/alerts$/);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-watch')))).toEqual([{id, market: 'ng'}]);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: an explicitly scoped topic link controls the first fetch and header. */
test('an explicitly scoped older topic link naming a 42 item carries its market to the first 42 read', async ({page}) => {
  const state = await setup(page);
  await page.goto('/#/topic/' + ITEM + '?region=ke');
  await expect(page).toHaveURL(new RegExp('#/t/' + ITEM + '\\?market=KE$'));
  await expect.poll(() => state.reads.filter(path => path.startsWith('/api/topics/'))).toEqual(['/api/topics/' + ITEM + '?market=KE']);
  await page.reload();
  await expect(page).toHaveURL(new RegExp('#/t/' + ITEM + '\\?market=KE$'));
  await expect.poll(() => state.reads.filter(path => path.startsWith('/api/topics/')).length).toBe(2);
  expect(state.reads.filter(path => path.startsWith('/api/topics/')).every(path => path.endsWith('?market=KE'))).toBe(true);
  await expectOlderLink(page, 'topic', '#/topic/' + id + '?region=ke', state.api);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: opening a saved topic link in another tab retains its country. */
test('a saved older topic link opened in another tab says it is older and reads nothing', async ({page}) => {
  const first = await setup(page, {pins: [{id, market: 'ke'}]});
  await expectDeskPageLanding(page, 'board', first.api);
  await expect(page.locator('[data-topic-pin-market="ke"] a')).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-watch')))).toEqual([{id, market: 'ke'}]);
  const other = await page.context().newPage();
  const state = await setup(other);
  await expectOlderLink(other, 'topic', '#/topic/sports_football?region=ke', state.api);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: invalid explicit topic scope is refused: ${scope}. */
for (const scope of ['region=unknown', 'region=ng&region=ke', 'region=']) test(`an older topic link with an invalid scope is refused without a read: ${scope}`, async ({page}) => {
  const state = await setup(page);
  await expectOlderLink(page, 'topic', '#/topic/' + id + '?' + scope, state.api);
  await expect(page.getByText('Invalid topic link', {exact: true})).toHaveCount(0);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: same-topic market pins remain independent with reverse=${reverse}. */
for (const reverse of [false, true]) test(`same-topic market pins stay stored apart through the Board landing with reverse=${reverse}`, async ({page}) => {
  const pins = [{id, market: 'ke'}, {id, market: 'ng'}];
  const priors = {'topic-pin:ke:sports_football': {v: 100, at: 1788600000000}, 'topic-pin:ng:sports_football': {v: 100, at: 1788600000000}};
  const state = await setup(page, {reverse, pins, priors});
  await expectDeskPageLanding(page, 'board', state.api);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-watch')))).toEqual(pins);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook')))).toEqual(priors);
  await expect(page.locator('[data-topic-pin-market]')).toHaveCount(0);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: Watch keeps its displayed add intent when another tab has already saved the pin. */
test('an older topic link offers no Watch, so a pin another tab saved is left as it is', async ({page}) => {
  const state = await setup(page);
  await expectOlderLink(page, 'topic', '#/topic/' + id, state.api);
  await page.evaluate(id => localStorage.setItem('pulse-watch', JSON.stringify([{id, market: 'ng'}, {id, market: 'ke'}])), id);
  await page.reload();
  await expect(page.getByRole('heading', {level: 1, name: 'This topic link is from an older version'})).toBeVisible();
  await expect(page.getByRole('button', {name: /^Watch/})).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-watch')))).toEqual([{id, market: 'ng'}, {id, market: 'ke'}]);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: Topic unwatch clears only its scoped prior and a later pin starts fresh. */
test('an older scoped topic link leaves every scoped prior stored as it was', async ({page}) => {
  const priors = {'topic-pin:ng:sports_football': {v: 50, at: 1788600000000}, 'topic-pin:ke:sports_football': {v: 1000, at: 1788600000000}};
  const state = await setup(page, {pins: [{id, market: 'ng'}, {id, market: 'ke'}], priors});
  await expectOlderLink(page, 'topic', '#/topic/' + id + '?region=ng', state.api);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook')))).toEqual(priors);
  await expectDeskPageLanding(page, 'board', state.api);
  await expect(page.getByText('First look, no change yet', {exact: true})).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook')))).toEqual(priors);
});

/* Was: a failed prior cleanup offers recovery before another Watch action. */
test('an older topic link makes no history write, so a failing history store raises nothing', async ({page}) => {
  const state = await setup(page, {pins: [{id, market: 'ng'}], priors: {'topic-pin:ng:sports_football': {v: 50, at: 1788600000000}}});
  await page.goto('/#/method');
  await page.evaluate(() => {
    window.historyWrites = 0;
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value){if (key === 'pulse-lastlook'){window.historyWrites++; throw new Error('History write unavailable');} return original.call(this, key, value);};
  });
  await page.evaluate(() => { location.hash = '#/topic/sports_football?region=ng'; });
  await expect(page.getByRole('heading', {level: 1, name: 'This topic link is from an older version'})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Retry clearing previous reading', exact: true})).toHaveCount(0);
  expect(await page.evaluate(() => window.historyWrites)).toBe(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook'))['topic-pin:ng:sports_football'].v)).toBe(50);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: Board retries a failed scoped-prior deletion before dismissing its warning. */
test('a Board link opens Alerts without a history write, so a failing history store raises nothing', async ({page}) => {
  const state = await setup(page, {pins: [{id, market: 'ng'}], priors: {'topic-pin:ng:sports_football': {v: 50, at: 1788600000000}}});
  await page.goto('/#/method');
  await page.evaluate(() => {
    window.historyWrites = 0;
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value){if (key === 'pulse-lastlook'){window.historyWrites++; throw new Error('History write unavailable');} return original.call(this, key, value);};
  });
  await page.evaluate(() => { location.hash = '#/board'; });
  await expect(page).toHaveURL(/#\/alerts$/);
  await expect(page.locator('#main-content h1')).toHaveText('Alerts');
  await expect(page.getByRole('button', {name: 'Retry history', exact: true})).toHaveCount(0);
  expect(await page.evaluate(() => window.historyWrites)).toBe(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook'))['topic-pin:ng:sports_football'].v)).toBe(50);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: legacy pins require an explicit market and never use an unqualified prior. */
test('a Board link opens Alerts and leaves an unscoped legacy pin and its prior unchanged', async ({page}) => {
  const state = await setup(page, {pins: [id], priors: {'topic:sports_football': {v: 50, at: 1788600000000}}});
  await expectDeskPageLanding(page, 'board', state.api);
  await expect(page.getByRole('button', {name: 'Use Nigeria', exact: true})).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-watch')))).toEqual([id]);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook')))).toEqual({'topic:sports_football': {v: 50, at: 1788600000000}});
});

/* Was: a legacy pin can be removed without changing a scoped pin of the same topic. */
test('a Board link opens Alerts and leaves a legacy pin and a scoped pin of the same topic both stored', async ({page}) => {
  const pins = [id, {id, market: 'ng'}];
  const priors = {'topic:sports_football': {v: 50, at: 1788600000000}, 'topic-pin:ng:sports_football': {v: 100, at: 1788600000000}};
  const state = await setup(page, {pins, priors});
  await expectDeskPageLanding(page, 'board', state.api);
  const saved = await page.evaluate(() => ({pins: JSON.parse(localStorage.getItem('pulse-watch')), prior: JSON.parse(localStorage.getItem('pulse-lastlook'))}));
  expect(saved.pins).toEqual(pins);
  expect(saved.prior).toEqual(priors);
});

/* Was: failed Board reads preserve scoped pins and priors, invalid=${invalid}. */
for (const invalid of [false, true]) test(`a Board link makes no desk read to fail and preserves scoped pins and priors, invalid=${invalid}`, async ({page}) => {
  const state = await setup(page, {pins: [{id, market: 'ng'}], priors: {'topic-pin:ng:sports_football': {v: 50, at: 1788600000000}}, invalid, failOnce: !invalid});
  await expectDeskPageLanding(page, 'board', state.api);
  await expect(page.locator('[data-topic-pin-state]')).toHaveCount(0);
  await expect(page.getByRole('button', {name: 'Retry Board reads', exact: true})).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook'))['topic-pin:ng:sports_football'].v)).toBe(50);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-watch')))).toEqual([{id, market: 'ng'}]);
  expect(deskOrTopic(state)).toEqual([]);
});

/* Was: a zero prior is distinct from an absent previous reading. */
test('a Board link opens Alerts and keeps a zero prior stored as zero', async ({page}) => {
  const state = await setup(page, {pins: [{id, market: 'ng'}], priors: {'topic-pin:ng:sports_football': {v: 0, at: 1788600000000}}});
  await expectDeskPageLanding(page, 'board', state.api);
  await expect(page.getByText('% since last look')).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook'))['topic-pin:ng:sports_football'])).toEqual({v: 0, at: 1788600000000});
});

/* Was: unreadable history remains preserved instead of becoming a fabricated baseline. */
test('a Board link opens Alerts and leaves unreadable history exactly as stored', async ({page}) => {
  const state = await setup(page, {pins: [{id, market: 'ng'}]});
  await page.goto('/#/method');
  await page.evaluate(() => localStorage.setItem('pulse-lastlook', '[broken'));
  await expectDeskPageLanding(page, 'board', state.api);
  await expect(page.getByText('since last look', {exact: false})).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('pulse-lastlook'))).toBe('[broken');
});

/* Was: a tiny nonzero reading change stays nonzero. */
test('a Board link opens Alerts and keeps a tiny nonzero prior exactly as stored', async ({page}) => {
  const state = await setup(page, {pins: [{id, market: 'ng'}], priors: {'topic-pin:ng:sports_football': {v: 99.9999, at: 1788600000000}}});
  await expectDeskPageLanding(page, 'board', state.api);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook'))['topic-pin:ng:sports_football'].v)).toBe(99.9999);
});

/* Was: a finite but unusably small prior cannot produce an infinite percentage. */
test('a Board link opens Alerts with no Infinity and keeps an unusably small prior as stored', async ({page}) => {
  const state = await setup(page, {pins: [{id, market: 'ng'}], priors: {'topic-pin:ng:sports_football': {v: Number.MIN_VALUE, at: 1788600000000}}});
  await expectDeskPageLanding(page, 'board', state.api);
  await expect(page.getByText('Infinity')).toHaveCount(0);
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('pulse-lastlook'))['topic-pin:ng:sports_football'].v)).toBe(Number.MIN_VALUE);
});
