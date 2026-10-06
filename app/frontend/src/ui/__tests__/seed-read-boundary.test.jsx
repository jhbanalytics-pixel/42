import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {validSeedsPayload, QueueTable, queueDateLine} from '../../seeds.jsx';
import {validSeedPathPayload, PlatformTimeline} from '../../seedpath.jsx';
import {buildScopedReadHash, parseWorkspaceHash} from '../../router.js';

/* Page port, 3 October 2026: Seeds reads 42's own search queue (GET
   /api/seeds, contract section 19), so the seed here is a queued search and
   the payload is that response. The checks keep their strength: an empty
   read must be stated, broken shapes are refused, an unknown way of picking
   a search stays unnamed, the row offers Ask and the date is in words. */
const qf = (value) => ({value, unit: 'credits, estimated', query_id: 'q_seeds_queue', run_id: null, result_hash: 'sha256:x'});
const seed = {lane: 'expansion', lane_words: 'Following what is rising', kind: 'topic', label: 'Repair as a shared ritual', item_id: null, href: null, platforms: ['TikTok'], priority: 1, credits: qf(1)};
const seedsBody = (queue) => ({market: 'KE', market_name: 'Kenya', status: queue ? 'ok' : 'empty', message: queue ? null : 'Nothing is queued for Kenya yet.', query_ids: ['q_seeds_queue', 'q_seeds_results'], lanes_about: [], notes: [], results: null, queue});
const queue = (seeds) => ({seed_date: '2026-10-01', seeds, lanes: [], creator_accounts: null, seeds_total: qf(seeds.length), credits_total: qf(seeds.length), truncated: false});
/* Seed path now reads 42's own GET /api/seed-path (contract section 17)
   instead of the old seed graph, so the payload and the trail are restated
   to that response: one Figure per number, one row per platform. */
const fig = (value, unit) => ({value, unit, query_id: 'q_seed_path', run_id: 'r1', result_hash: 'sha256:x'});
const day = (date, posts) => ({date, posts});
const platform = (name, first, before, points) => ({platform: name.toLowerCase(), name, first_seen: fig(first, 'first sighting day'), before_window: before, posts_all_time: fig(points.reduce((a, p) => a + p.posts, 0), 'posts'), posts_in_window: fig(points.reduce((a, p) => a + p.posts, 0), 'posts'), daily: {unit: 'matching posts a day', query_id: 'q_seed_path', run_id: 'r1', points}});
const path = {keyword: 'amapiano', market: 'KE', status: 'ok', message: null, query_id: 'q_seed_path', run_id: 'r1', window: {from: '2026-09-01', to: '2026-09-03', days: 3}, matched_posts: fig(4, 'posts'), platforms: [platform('TikTok', '2026-09-01', false, [day('2026-09-01', 3), day('2026-09-02', 0), day('2026-09-03', 0)]), platform('X', '2026-08-20', true, [day('2026-09-01', 0), day('2026-09-02', 0), day('2026-09-03', 1)])], side_words: [{word: 'piano', posts: fig(2, 'posts')}], side_words_min_posts: 2, topics: [], truncated: false, notes: []};

test('seed absence requires an explicit collection and an unknown lane stays unnamed', () => {
  expect(validSeedsPayload(seedsBody(null), 'KE')).toBe(true);
  expect(validSeedsPayload(seedsBody(queue([seed])), 'KE')).toBe(true);
  for (const bad of [{}, {...seedsBody(null), queue: undefined}, seedsBody({...queue([seed]), seeds: {}}), seedsBody(queue([null])), seedsBody(queue([{...seed, platforms: 'TikTok'}]))]) expect(validSeedsPayload(bad, 'KE')).toBe(false);
  for (const lane of ['', null, undefined, 'unsupported']){
    const html = renderToStaticMarkup(<QueueTable seeds={[{...seed, lane, lane_words: 'Following what is rising'}]} market="KE" />);
    expect(html).toContain('Other searches');
    expect(html).not.toContain('Following what is rising');
    expect(html).not.toContain('act now');
  }
});

test('Explorer validates request identity and complete empty read separately', () => {
  expect(validSeedPathPayload(path, ' Amapiano ', 'KE')).toBe(true);
  for (const bad of [{}, {...path, market: 'NG'}, {...path, keyword: 'football'}, {...path, platforms: null}, {...path, matched_posts: 4}, {...path, platforms: [null]}, {...path, platforms: []}]) expect(validSeedPathPayload(bad, 'amapiano', 'ke')).toBe(false);
  expect(validSeedPathPayload(path, '#amapiano', 'ke')).toBe(true);
  const empty = {...path, status: 'no_match', message: 'No post uses it.', platforms: [], side_words: [], matched_posts: null, window: null};
  expect(validSeedPathPayload(empty, 'amapiano', 'ke')).toBe(true);
  expect(validSeedPathPayload({...empty, message: ''}, 'amapiano', 'ke')).toBe(false);
  expect(validSeedPathPayload({...empty, status: 'maybe'}, 'amapiano', 'ke')).toBe(false);
});

test('Explorer draws one row per platform on one axis without constructing a platform observation', () => {
  const html = renderToStaticMarkup(<PlatformTimeline platforms={path.platforms} window={path.window} market="ke" />);
  expect((html.match(/data-platform-row=/g) || []).length).toBe(2);
  expect(html).toContain('First seen <span class="sp-fig" data-query-id="q_seed_path"');
  expect(html).toContain('1 September 2026');
  expect(html).toContain('Before these 28 days');
  expect(html).toContain('3 posts');
  expect(html).toContain('1 post<');
  expect((html.match(/class="sp-bar[^"]*"/g) || []).length).toBe(6);
  expect((html.match(/is-first/g) || []).length).toBe(1);
  expect(html).toContain('the tallest bar is 3 posts');
  expect(html).not.toContain('graph records');
});

for (const view of ['listen', 'seedpath', 'lexicon']) test(view + ' links preserve explicit market and refuse ambiguous scopes', () => {
  const hash = buildScopedReadHash(view, 'repair café', 'KE');
  expect(hash).toBe('#/' + view + '/repair%20caf%C3%A9?region=ke');
  expect(parseWorkspaceHash(hash).readRegion).toBe('KE');
  for (const scope of ['region=ng&region=ke', 'region=', 'region=unknown']) expect(parseWorkspaceHash('#/' + view + '/repair?' + scope).error).toBe('read_scope_invalid');
  expect(parseWorkspaceHash('#/' + view).error).toBe(null);
});

test('the queue date is written in words and says what it is', () => {
  expect(queueDateLine('2026-09-06')).toBe('Queued for Sunday 6 September 2026.');
  expect(queueDateLine(null)).toBe('The date of this queue was not recorded.');
  expect(queueDateLine('2026-09-06')).not.toContain('2026-09-06');
});

test('a queued search offers Ask about this, named as the menu names Ask, and no Console', () => {
  const html = renderToStaticMarkup(<QueueTable seeds={[seed]} market="KE" />);
  expect(html).toContain('Ask about this');
  expect(html).not.toMatch(/Console/);
});
