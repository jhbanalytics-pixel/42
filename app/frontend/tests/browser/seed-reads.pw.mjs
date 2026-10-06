import {expect, test} from '@playwright/test';
import {readFileSync} from 'node:fs';
import {DESK_WRAPPED_SIGNAL} from '../../src/ui/__tests__/fixtures/instrument-payloads.js';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/seed-reads.json', import.meta.url), 'utf8'));
const copy = value => JSON.parse(JSON.stringify(value));

function lexiconPayload(rows, market){
  const fig = value => ({value, unit: 'posts', query_id: 'q_lexicon', run_id: 'r1', result_hash: 'sha256:x'});
  const terms = rows.filter(r => r.market === market).sort((a, b) => b.n - a.n).map(r => ({
    item_id: 'item_' + r.term, label: r.term, kind: 'meme', kind_word: 'Word or phrase', seed_term: r.term,
    topic_href: '#/t/item_' + r.term + '?market=' + market, first_seen: null, posts: fig(r.n), week_posts: fig(r.n),
    prior_week_posts: fig(0), change: {percent: null, reason: 'no_earlier_posts', text: 'No posts in the week before, so there is no change to work out.'},
  }));
  return {market, status: terms.length ? 'ok' : 'empty', message: terms.length ? null : 'No terms.', query_id: 'q_lexicon', run_id: 'r1',
    window: {from: '2026-09-03', to: '2026-09-30', days: 28, week: {from: '2026-09-24', to: '2026-09-30'}, prior_week: {from: '2026-09-17', to: '2026-09-23'}},
    kinds: ['meme', 'hashtag'], terms, truncated: false, notes: []};
}

/* Page port, 3 October 2026: Seeds reads 42's own GET /api/seeds?market=XX
   (core/api/contract.md section 19), built from seed_queue: the next
   searches 42 will run in one market and what its last searches found. The
   old editorial seed behaviours in fixture.seeds are not that payload, so
   the Seeds journeys serve this one, in the shape the page's unit tests
   use (src/ui/__tests__/seeds-42.test.jsx), for whichever market is asked.
   The journeys that already failed before the port (the desk summary,
   focus and malformed read cases) are left as they were. */
const SEEDS_MARKET_NAMES = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};
function seedsPayload(market){
  const mk = String(market || '').toUpperCase(), lower = mk.toLowerCase();
  const fig = (value, unit, q = 'q_seeds_queue') => ({value, unit, query_id: q, run_id: null, result_hash: 'sha256:x'});
  const R = (value, unit) => fig(value, unit, 'q_seeds_results');
  const words = {expansion: 'Following what is rising', exploration: 'Trying something new', anchor: 'Re-checking what worked'};
  const queued = (label, lane, extra = {}) => ({lane, lane_words: words[lane], kind: 'hashtag', label, item_id: null, href: null, platforms: ['TikTok'], priority: 1, credits: fig(1, 'credits, estimated'), ...extra});
  const found = (label, posts, creators, extra = {}) => ({lane: 'expansion', lane_words: words.expansion, kind: 'hashtag', label, item_id: null, href: null, platforms: ['TikTok'], posts: R(posts, 'new posts found'), new_creators: R(creators, 'new creators found'), ...extra});
  return {
    market: mk, market_name: SEEDS_MARKET_NAMES[mk] || mk, status: 'ok', message: null, query_ids: ['q_seeds_queue', 'q_seeds_results'],
    lanes_about: [
      {lane: 'expansion', words: words.expansion, about: 'Topics already rising in 42\'s counts, searched more widely.'},
      {lane: 'exploration', words: words.exploration, about: 'Topics from the middle of the ranking.'},
      {lane: 'anchor', words: words.anchor, about: 'Searches that found posts last time, run again.'},
    ],
    queue: {
      seed_date: '2026-10-01',
      seeds: [
        queued('Fixture Cola', 'expansion', {kind: 'brand', item_id: 'abc123', href: '#/t/abc123?market=' + mk, platforms: ['X', 'Threads', 'Reddit'], credits: fig(5, 'credits, estimated')}),
        queued('fixture_anchor', 'anchor', {href: '#/seedpath/fixture_anchor?region=' + lower}),
        queued('fixture other meme', 'exploration', {kind: 'meme', item_id: 'def456', href: '#/t/def456?market=' + mk}),
      ],
      lanes: [
        {lane: 'expansion', words: words.expansion, seeds: fig(5, 'searches queued'), credits: fig(13, 'credits, estimated')},
        {lane: 'exploration', words: words.exploration, seeds: fig(1, 'searches queued'), credits: fig(5, 'credits, estimated')},
        {lane: 'anchor', words: words.anchor, seeds: fig(1, 'searches queued'), credits: fig(1, 'credits, estimated')},
      ],
      creator_accounts: {seeds: fig(2, 'creator account searches queued'), credits: fig(2, 'credits, estimated')},
      seeds_total: fig(7, 'searches queued'), credits_total: fig(19, 'credits, estimated'), truncated: false,
    },
    results: {
      seed_date: '2026-09-30',
      seeds: [found('#fixture_heritage', 14, 3, {item_id: 'her', href: '#/t/her?market=' + mk}), found('#fixture_outage', 0, 0, {lane: 'exploration', lane_words: words.exploration})],
      lanes: [{lane: 'expansion', words: words.expansion, seeds: R(2, 'searches run'), posts: R(19, 'new posts found'), new_creators: R(4, 'new creators found')}],
      creator_accounts: {seeds: R(1, 'creator account searches run'), posts: R(5, 'new posts found'), new_creators: R(1, 'new creators found')},
      totals: {seeds: R(4, 'searches run'), posts: R(23, 'new posts found'), new_creators: R(4, 'new creators found')},
      truncated: false,
    },
    notes: [],
  };
}

async function setup(page, {seedReply, pathReply, deskReply, initialRegion = 'NG', lexicon = []} = {}){
  const reads = [], unexpected = [], errors = [];
  let seedCalls = 0, pathCalls = 0;
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(region => {
    localStorage.setItem('pulse_passcode', 'browser-fixture-only');
    if (!localStorage.getItem('pulse-region')) localStorage.setItem('pulse-region', region);
  }, initialRegion);
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url());
    reads.push(url.pathname + url.search);
    if (request.method() !== 'GET') {unexpected.push(request.method() + url.pathname); return route.fulfill({status: 501, json: {detail: 'Unexpected mutation'}});}
    if (url.pathname === '/api/health') return route.fulfill({json: {passcode: true}});
    if (url.pathname === '/api/seeds') return route.fulfill(seedReply ? await seedReply(++seedCalls, url) : {json: fixture.seeds});
    if (url.pathname === '/api/seed-path'){
      if (pathReply) return route.fulfill(await pathReply(++pathCalls, url));
      return route.fulfill({json: fixture.explorer});
    }
    if (url.pathname === '/api/discover') return route.fulfill({json: {items: [], next_cursor: null, held_back: {count: 0, items: []}, filters: {}}});
    if (url.pathname === '/api/desk') return route.fulfill(deskReply ? await deskReply() : {json: {topics: [], lexicon, freshness: {status: 'green', age_hours: 1}}});
    if (url.pathname === '/api/intel/mentions') return route.fulfill({json: {results: [], cursor: null, has_more: false}});
    /* Page port, 3 October 2026: Lexicon reads /api/lexicon?market=XX
       (contract section 20); the old desk rows are served in that shape. */
    if (url.pathname === '/api/lexicon') return route.fulfill({json: lexiconPayload(lexicon, url.searchParams.get('market'))});
    if (url.pathname === '/api/topic/music_amapiano') return route.fulfill({json: {id: 'music_amapiano', region: url.searchParams.get('region')?.toUpperCase(), topic: 'Amapiano in Kenya', score: .5, series: [3, 9], platforms: [], creators_list: [], voices: [], brief: null, has_brief: false}});
    unexpected.push(url.pathname + url.search);
    return route.fulfill({status: 501, json: {detail: 'Missing fixture'}});
  });
  return {reads, unexpected, errors};
}

for (const width of [767, 768, 1023, 1024]) test('desk summary arrival keeps the Seeds workspace stable at ' + width, async ({page}) => {
  await page.setViewportSize({width, height: 1000});
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  await setup(page, {initialRegion: 'ZA', deskReply: async () => {await gate; return {json: {topics: [], lexicon: [], freshness: {status: 'green', age_hours: 1}, dynamic_discovery: {contract_version: 'desk_dynamic_signal_v2', status: 'ready', requested_market: 'za', run: {run_id: DESK_WRAPPED_SIGNAL.signal.run_id}, signals: [DESK_WRAPPED_SIGNAL], error: null}}};}});
  await page.goto('/#/seeds');
  await page.getByRole('heading', {name: 'Repair as a shared ritual'}).waitFor();
  await page.waitForFunction(() => document.documentElement.dataset.productStyles === 'ready');
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => Promise.all(document.getAnimations().filter(a => Number.isFinite(a.effect.getTiming().iterations)).map(a => a.finished.catch(() => {}))));
  const before = await page.locator('.seeds .page-shell').boundingBox();
  release();
  await page.locator('.instrument-rail-summary').waitFor({state: 'attached'});
  const after = await page.locator('.seeds .page-shell').boundingBox();
  for (const axis of ['x', 'y', 'width', 'height']) expect(Math.abs(before[axis] - after[axis]), axis).toBeLessThanOrEqual(1);
});

for (const view of ['seeds', 'seedpath']) for (const width of [768, 1280]) test(view + ' keeps existing controls and focus stable as a read arrives at ' + width, async ({page}) => {
  await page.setViewportSize({width, height: 1000});
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  const reply = async () => {await gate; return {json: view === 'seeds' ? fixture.seeds : fixture.explorer};};
  const state = await setup(page, view === 'seeds' ? {seedReply: reply} : {pathReply: reply});
  await page.goto('/#/' + (view === 'seeds' ? 'seeds' : 'seedpath/amapiano?region=ke'));
  await page.getByText(view === 'seeds' ? 'Loading available seeds…' : 'Reading the posts 42 collected in Kenya', {exact: true}).waitFor();
  await page.waitForFunction(() => document.documentElement.dataset.productStyles === 'ready');
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => Promise.all(document.getAnimations().filter(a => Number.isFinite(a.effect.getTiming().iterations)).map(a => a.finished.catch(() => {}))));
  await expect(page.locator('.ui-next')).toHaveCount(0);
  const selectors = ['.ui-page-hero', '.loop-rail', ...(view === 'seedpath' ? ['.seedpath form'] : [])];
  const before = await Promise.all(selectors.map(selector => page.locator(selector).boundingBox()));
  await page.locator(view === 'seeds' ? '.loop-stage' : '.seed-field').first().focus();
  await page.evaluate(() => {window.readFocus = document.activeElement;});
  release();
  await page.locator(view === 'seeds' ? '.seeds section.reveal h2' : '[data-platform-row="2"]').waitFor();
  await expect(page.locator('.ui-next')).toBeVisible();
  const after = await Promise.all(selectors.map(selector => page.locator(selector).boundingBox()));
  for (let index = 0; index < selectors.length; index++) for (const axis of ['x', 'y', 'width', 'height']) expect(Math.abs(before[index][axis] - after[index][axis]), selectors[index] + ' ' + axis).toBeLessThanOrEqual(1);
  expect(await page.evaluate(() => document.activeElement === window.readFocus && window.readFocus.isConnected)).toBe(true);
  expect(state.errors).toEqual([]);
});

/* Page port, 3 October 2026: the old page's evidence links opened the older
   topic page and Listen, which read the desk API f42-api does not serve.
   Seeds now reads one market at a time from /api/seeds?market=, starting
   from the header's market; its market buttons move the header too, and each
   search links to the 42 topic page and an unsent Ask draft in that market.
   Was: Seeds evidence carries Kenya to Topic and Listen despite a Nigeria header. */
test('Seeds carries Kenya from its market button to the header, its reads, its topic and Ask links and a reload', async ({page}) => {
  const state = await setup(page, {seedReply: (call, url) => ({json: seedsPayload(url.searchParams.get('market'))})});
  await page.goto('/#/seeds');
  await expect(page.getByRole('heading', {name: 'Next searches'})).toBeVisible();
  await expect(page.getByText('42 will run 7 searches in Nigeria on Thursday 1 October 2026', {exact: false})).toBeVisible();
  await page.getByRole('group', {name: 'Market', exact: true}).getByRole('button', {name: 'Kenya', exact: true}).click();
  await expect(page.getByText('42 will run 7 searches in Kenya on Thursday 1 October 2026', {exact: false})).toBeVisible();
  await expect(page.getByRole('group', {name: 'Market', exact: true}).getByRole('button', {name: 'Kenya', exact: true})).toHaveAttribute('aria-pressed', 'true');
  /* Quiet register, 23 Sept 2026: package 2.0.21 writes the market scope
     on the trigger as the market's name rather than its code. */
  await expect(page.locator('.instrument-market-trigger')).toHaveText('Kenya');
  await expect(page.getByText('The 4 searches of Wednesday 30 September 2026 found 23 new posts and 4 new creators', {exact: false})).toBeVisible();
  await expect(page.getByText('2 searches of creator accounts', {exact: false})).toBeVisible();
  await expect(page.locator('[data-query-id="q_seeds_queue"]').first()).toBeVisible();
  await expect(page.getByRole('link', {name: 'Fixture Cola', exact: true})).toHaveAttribute('href', '#/t/abc123?market=KE');
  await expect(page.getByRole('link', {name: 'fixture_anchor', exact: true})).toHaveAttribute('href', '#/seedpath/fixture_anchor?region=ke');
  await expect(page.getByRole('link', {name: 'Ask about this', exact: true}).first()).toHaveAttribute('href', /^#\/ask\?q=[^&]+&market=KE&draft=1$/);
  await expect(page.getByRole('button', {name: 'Listen ↗', exact: true})).toHaveCount(0);
  await expect(page.locator('a[href^="#/listen"], a[href^="#/topic/"]')).toHaveCount(0);
  await page.reload();
  await expect(page.getByText('42 will run 7 searches in Kenya', {exact: false})).toBeVisible();
  await expect(page.locator('.instrument-market-trigger')).toHaveText('Kenya');
  expect(state.reads.filter(p => p.startsWith('/api/seeds'))).toEqual(['/api/seeds?market=NG', '/api/seeds?market=KE', '/api/seeds?market=KE']);
  await page.getByRole('link', {name: 'Fixture Cola', exact: true}).click();
  await expect(page).toHaveURL(/#\/t\/abc123\?market=KE$/);
  expect(state.reads.filter(p => p.startsWith('/api/topic/') || p.startsWith('/api/intel/mentions') || p.startsWith('/api/desk'))).toEqual([]);
  expect(state.unexpected.filter(p => !p.startsWith('/api/topics/abc123'))).toEqual([]); expect(state.errors).toEqual([]);
});

for (const invalid of [{}, {date: null, seeds: null}, {date: null, seeds: [{...fixture.seeds.seeds[0], evidence: [null]}]}]) test('malformed Seeds read is unavailable and retry recovers: ' + JSON.stringify(invalid).slice(0, 60), async ({page}) => {
  const state = await setup(page, {seedReply: call => ({json: call === 1 ? invalid : fixture.seeds})});
  await page.goto('/#/seeds');
  await expect(page.getByText('Seeds unavailable', {exact: true})).toBeVisible();
  await expect(page.getByText('No seed behaviours returned in this read.', {exact: true})).toHaveCount(0);
  await page.getByRole('button', {name: 'Try again', exact: true}).click();
  await expect(page.getByRole('heading', {name: 'Repair as a shared ritual'})).toBeVisible();
  expect(state.reads.filter(p => p === '/api/seeds')).toHaveLength(2);
  expect(state.errors).toEqual([]);
});

/* Page port, 3 October 2026: an empty read is the new payload's status
   empty, with its message, and no queue or results. */
test('valid empty Seeds does not imply a daily result', async ({page}) => {
  const state = await setup(page, {seedReply: (call, url) => ({json: {...seedsPayload(url.searchParams.get('market')), status: 'empty', message: 'Nothing is queued for Nigeria yet. 42\'s morning detect run writes the next day\'s searches.', queue: null, results: null}})});
  await page.goto('/#/seeds');
  await expect(page.getByText('Nothing queued for Nigeria yet', {exact: true})).toBeVisible();
  await expect(page.getByText('morning detect run', {exact: false})).toBeVisible();
  await expect(page.getByRole('heading', {name: 'Next searches'})).toHaveCount(0);
  await expect(page.getByText('Seeds could not load', {exact: true})).toHaveCount(0);
  expect(state.reads.filter(p => p.startsWith('/api/seeds'))).toEqual(['/api/seeds?market=NG']);
});

/* Page port, 3 October 2026: Listen read /api/intel/mentions, which
   f42-api does not serve, so it left the More menu and #/listen/<term> now
   opens Seed path for the same term and market (src/legacyRoutes.js). The
   journey holds that: the term and market ride over, an edited term
   survives a market change, a reload stays on Seed path, and no mentions
   read is made. Was: Listen preserves edited filters across market changes. */
test('a Listen link opens Seed path with its term and market, and an edited term survives a market change', async ({page}) => {
  const state = await setup(page);
  await page.goto('/#/listen/amapiano?region=ke');
  await expect(page).toHaveURL(/#\/seedpath\/amapiano\?region=ke$/);
  await expect(page.getByRole('textbox', {name: 'Keyword to trace'})).toHaveValue('amapiano');
  await expect(page.locator('[data-platform-row]')).toHaveCount(2);
  await page.reload();
  await expect(page).toHaveURL(/#\/seedpath\/amapiano\?region=ke$/);
  await expect(page.locator('[data-platform-row]')).toHaveCount(2);
  expect(state.reads.filter(p => p.startsWith('/api/seed-path'))).toEqual(['/api/seed-path?keyword=amapiano&market=KE', '/api/seed-path?keyword=amapiano&market=KE']);
  await page.getByRole('textbox', {name: 'Keyword to trace'}).fill('food');
  await page.getByRole('group', {name: 'Market', exact: true}).getByRole('button', {name: 'Nigeria', exact: true}).click();
  await expect(page.getByRole('textbox', {name: 'Keyword to trace'})).toHaveValue('food');
  await page.goto('/#/listen/piano?region=ng');
  await expect(page).toHaveURL(/#\/seedpath\/piano\?region=ng$/);
  await expect(page.getByRole('textbox', {name: 'Keyword to trace'})).toHaveValue('piano');
  await expect(page.locator('a[href^="#/listen"]')).toHaveCount(0);
  expect(state.reads.filter(p => p.startsWith('/api/intel/mentions'))).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('Lexicon in-page selection retains the recorded country in its portable link', async ({page}) => {
  await setup(page, {lexicon: [{term: 'piano', market: 'KE', n: 2}, {term: 'repair', market: 'KE', n: 3}]});
  await page.goto('/#/lexicon/piano?region=ke');
  await page.locator('.lex-term-card').filter({hasText: 'repair'}).click();
  await expect(page).toHaveURL(/#\/lexicon\/repair\?region=ke$/);
  await expect(page.locator('.lex-decode h2')).toHaveText('repair');
  /* Demo polish, 2 October 2026: the link names the page as the menu does. */
  await expect(page.getByRole('link', {name: 'Trace this term in Seed path'})).toHaveAttribute('href', '#/seedpath/repair?region=ke');
});

/* Seed path rebuild, 2 October 2026: the page reads 42's GET /api/seed-path
   (contract section 17), so the trace is restated to that response: one
   timeline row per platform with its first sighting and posts, side words
   that trace themselves, and topics that open the 42 topic page. The old
   graph's lexicon matches are not part of the new response. */
test('Explorer traces physical input, draws one row per platform and carries market to the side words and topics', async ({page}) => {
  const state = await setup(page);
  await page.goto('/#/seedpath');
  await expect(page.getByText('Nothing traced yet', {exact: true})).toBeVisible();
  expect(state.reads.filter(p => p.startsWith('/api/seed-path'))).toEqual([]);
  await page.getByRole('textbox', {name: 'Keyword to trace'}).fill('amapiano');
  /* Demo polish, 2 October 2026: the Seed path market buttons name the
     market in words, as the header does, so Kenya rather than KE. */
  await page.getByRole('group', {name: 'Market', exact: true}).getByRole('button', {name: 'Kenya', exact: true}).click();
  await expect(page.getByRole('textbox', {name: 'Keyword to trace'})).toHaveValue('amapiano');
  await page.getByRole('button', {name: 'Trace', exact: true}).click();
  await expect(page).toHaveURL(/#\/seedpath\/amapiano\?region=ke$/);
  await expect(page.locator('[data-platform-row]')).toHaveCount(2);
  await expect(page.locator('[data-platform-row="1"]')).toContainText('TikTok');
  await expect(page.locator('[data-platform-row="1"]')).toContainText('3 posts');
  /* First sightings are written as a reader says them, 1 September 2026. */
  await expect(page.locator('[data-platform-row="1"]')).toContainText('1 September 2026');
  await expect(page.locator('[data-platform-row="2"]')).toContainText('9 posts');
  await expect(page.locator('[data-platform-row="2"]')).toContainText('6 September 2026');
  await expect(page.locator('[data-platform-row="1"] [data-query-id="q_seed_path"]')).toHaveCount(2);
  await page.reload();
  await expect(page.locator('[data-platform-row]')).toHaveCount(2);
  expect(state.reads.filter(p => p.startsWith('/api/seed-path')).every(p => p === '/api/seed-path?keyword=amapiano&market=KE')).toBe(true);
  await expect(page.getByRole('link', {name: 'Amapiano in Kenya', exact: true})).toHaveAttribute('href', '#/t/music_amapiano?market=KE');
  await page.getByRole('button', {name: 'piano', exact: true}).click();
  await expect(page).toHaveURL(/#\/seedpath\/piano\?region=ke$/);
  expect(state.unexpected.filter(p => !p.startsWith('/api/seed-path?keyword=piano'))).toEqual([]); expect(state.errors).toEqual([]);
});

for (const invalid of [{}, {...fixture.explorer, market: 'NG'}, {...fixture.explorer, keyword: 'football'}]) test('Explorer refuses malformed or mismatched identity and retry recovers: ' + JSON.stringify(invalid).slice(0, 65), async ({page}) => {
  const state = await setup(page, {pathReply: call => ({json: call === 1 ? invalid : fixture.explorer})});
  await page.goto('/#/seedpath/amapiano?region=ke');
  await expect(page.getByText('Could not trace this term', {exact: true})).toBeVisible();
  await expect(page.locator('[data-platform-row]')).toHaveCount(0);
  await page.getByRole('button', {name: 'Try again', exact: true}).click();
  await expect(page.locator('[data-platform-row]')).toHaveCount(2);
  expect(state.reads.filter(p => p.startsWith('/api/seed-path'))).toHaveLength(2);
  expect(state.errors).toEqual([]);
});

test('All-market Explorer waits for an explicit country and retains the keyword', async ({page}) => {
  const state = await setup(page);
  await page.goto('/#/seedpath/amapiano?region=all');
  await expect(page.getByText('Choose one market', {exact: true})).toBeVisible();
  await expect(page.getByRole('button', {name: 'Trace', exact: true})).toBeDisabled();
  expect(state.reads.filter(p => p.startsWith('/api/seed-path'))).toEqual([]);
  /* Demo polish, 2 October 2026: the Seed path market buttons name the
     market in words, as the header does, so Kenya rather than KE. */
  await page.getByRole('group', {name: 'Market', exact: true}).getByRole('button', {name: 'Kenya', exact: true}).click();
  await expect(page.locator('[data-platform-row]')).toHaveCount(2);
  expect(state.reads.filter(p => p.startsWith('/api/seed-path'))).toEqual(['/api/seed-path?keyword=amapiano&market=KE']);
});

/* Page port, 3 October 2026: the lexicon reads /api/lexicon?market=, and a
   Listen link now opens Seed path, which keeps the malformed market and
   withholds its read in the same way. */
test('invalid lexicon market withholds the read', async ({page}) => {
  const state = await setup(page);
  await page.goto('/#/lexicon/piano?region=ng&region=ke');
  await expect(page.getByRole('heading', {name: 'Invalid market link'})).toBeVisible();
  expect(state.reads.filter(p => p === '/api/lexicon' || p.startsWith('/api/lexicon?') || p.startsWith('/api/lexicon/'))).toEqual([]);
});

test('invalid listen market opens Seed path and withholds the read', async ({page}) => {
  const state = await setup(page);
  await page.goto('/#/listen/piano?region=ng&region=ke');
  await expect(page).toHaveURL(/#\/seedpath\/piano\?region=ng&region=ke$/);
  expect(state.reads.filter(p => p.startsWith('/api/seed-path') || p.startsWith('/api/intel/mentions'))).toEqual([]);
});

for (const width of [390, 768, 1440]) test('Explorer records fit the viewport at ' + width, async ({page}) => {
  await page.setViewportSize({width, height: 1000});
  const state = await setup(page);
  await page.goto('/#/seedpath/amapiano?region=ke');
  await expect(page.locator('[data-platform-row]')).toHaveCount(2);
  await page.evaluate(() => document.fonts.ready);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(state.errors).toEqual([]);
});
