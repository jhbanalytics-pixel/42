/* Coverage on the 42 API (contract.md section 10.4): what was collected,
   where, when, at what cost, and the gaps. The fixture is one day with a
   failed ZA series, a drifted NG series, nothing for KE, credits by job and
   lane, model spend under the cap, a failed detect run and no scorecard. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';
import coverageFixture from './fixtures/coverage42_day.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {CoveragePage42} = await import('../../coverage42.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});

function serve(answer){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init});
    return typeof answer === 'function' ? answer(url) : answer;
  };
}

beforeEach(() => {
  calls = [];
  localStorage.setItem('pulse_passcode', 'test-pass');
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  globalThis.fetch = realFetch;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };

async function mount(body = coverageFixture, props = {date: '2026-09-30'}){
  serve(reply(200, body));
  flushSync(() => root.render(<CoveragePage42 {...props} />));
  await settle();
}

const text = () => host.textContent.replace(/\s+/g, ' ');
const section = (name) => host.querySelector('[data-section="' + name + '"]');
const market = (code) => host.querySelector('[data-market="' + code + '"]');
const rowCells = (row) => [...row.querySelectorAll('th, td')].map((cell) => cell.textContent.replace(/\s+/g, ' ').trim());
const button = (label) => [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));

test('reads the coverage route for the date, or the latest day when none is given', async () => {
  await mount();
  flushSync(() => root.render(<CoveragePage42 />));
  await settle();
  expect(calls.map((c) => c.url)).toEqual(['/api/coverage?date=2026-09-30', '/api/coverage']);
  expect(calls[0].init.headers['X-Passcode']).toBe('test-pass');
});

test('shows a loading line while coverage is on its way', async () => {
  let release;
  serve(new Promise((resolve) => { release = resolve; }));
  flushSync(() => root.render(<CoveragePage42 date="2026-09-30" />));
  expect(host.querySelector('[aria-busy="true"]')).not.toBeNull();
  expect(text()).toContain('Loading coverage');
  release(reply(200, coverageFixture));
  await settle();
  expect(host.querySelector('[aria-busy="true"]')).toBeNull();
  expect(host.querySelector('h1').textContent).toBe('Coverage, 30 September 2026');
});

test('each market shows posts on a shared axis, located share, calls that worked against attempts and status in words', async () => {
  // Restated 2 October 2026: the per-market table now leads with posts drawn as bars on one axis and says status in words.
  await mount();
  const za = market('ZA');
  expect(za.querySelector('h2').textContent).toBe('South Africa');
  const head = rowCells(za.querySelector('thead tr'));
  expect(head).toEqual(['Source', 'Posts', '02 000', 'Located', 'Calls worked', 'Status']);
  // Restated 5 October 2026 (sources table tidy): rows sit in parts, each opened by its name and what it adds up to,
  // usable sources first, then by posts; a board whose posts carry no place says what it is instead of "Not measured".
  const rows = [...za.querySelectorAll('tbody tr:not(.cv42-group-row)')].map(rowCells);
  expect(rows).toEqual([
    ['YouTube trending board', '1 200', '', '26%', '12 of 12', 'Usable'],
    ['TikTok local feed', '500', '', '40%', '12 of 12', 'Usable'],
    ['TikTok hashtag board', '20', '', 'Market board', '1 of 6', 'Not usableCalls failed'],
  ]);
  expect(rowCells(za.querySelector('.cv42-group-row'))).toEqual(['Trending boards and feeds2 of 3 usable, 1 700 posts from usable sources']);
  // Restated after the design review (2 October 2026): the status word and its reason are two lines, the reason muted.
  expect(rowCells(market('NG').querySelector('tbody tr:not(.cv42-group-row)'))).toEqual(['Reddit rising and hot', '140', '', '10%', '8 of 8', 'Not usableFeed drifted from usual']);
  expect(market('NG').querySelector('.cv42-state-reason').textContent).toBe('Feed drifted from usual');
  expect(text()).toContain('For each series, the denominator is its distinct collected posts. The percentage shows how many have a location confidence of at least 70%, based on an external-region, home-market, or place-mention signal.');
  expect(text()).toContain('This is a post-level location signal, not proof of where people live or of general public or TikTok activity in the market.');
});

test('the bars share one axis across markets, scaled to a round top above the largest source', async () => {
  await mount();
  const widths = [...host.querySelectorAll('.cv42-bar')].map((bar) => Number(bar.style.getPropertyValue('--cv42-w')));
  // 1 200 posts is the day's largest source, so the axis tops out at the next round step, 2 000, for every market.
  // Restated 5 October 2026: the rows now read usable first, then by posts (YouTube, TikTok feed, hashtag board).
  expect(widths.slice(0, 3)).toEqual([1200 / 2000, 500 / 2000, 20 / 2000]);
  expect([...host.querySelectorAll('.cv42-axis')].map((axis) => axis.textContent.replace(/\s+/g, ' '))).toEqual(['02 000', '02 000']);
  expect(host.querySelector('.cv42-bar-col[aria-hidden="true"]')).not.toBeNull();
});

test('a market summary leads with the numbers the service sent, and says when location was not measured', async () => {
  const answer = clone(coverageFixture);
  answer.markets[0].summary = {posts: 1700, platforms: 2, located_share: 0.31, sources: 3, sources_usable: 2};
  answer.markets[1].summary = {posts: 1, platforms: 1, located_share: null, sources: 1, sources_usable: 0};
  await mount(answer);
  const stats = (code) => [...market(code).querySelectorAll('.cv42-stat')].map((li) => li.textContent.replace(/\s+/g, ' ').trim());
  expect(stats('ZA')).toEqual(['1 700 items collected', '2 platforms', '31% with a confident location', '2 of 3 sources usable']);
  expect(stats('NG')).toEqual(['1 item collected', '1 platform', 'Location not measured yet', '0 of 1 sources usable']);
  expect(market('ZA').querySelector('.cv42-stat-value').textContent).toBe('1\u00a0700');
});

test('a market with no usable source says items collected were not measured, not zero', async () => {
  const answer = clone(coverageFixture);
  answer.markets[1].summary = {posts: null, platforms: 0, located_share: null, sources: 2, sources_usable: 0};
  await mount(answer);
  const stats = [...market('NG').querySelectorAll('.cv42-stat')].map((li) => li.textContent.replace(/\s+/g, ' ').trim());
  expect(stats[0]).toBe('Items collected: not measured');
});

test('zero attempts are described as no calls recorded, while successful calls may return zero items', async () => {
  // Restated 5 October 2026 (sources table tidy): a source with no call on the day leaves the table for one closed
  // line under it that names it and says the summary counts it as not usable; a call that worked but found nothing
  // stays in the table as usable with 0 posts.
  const answer = clone(coverageFixture);
  const [notAttempted, emptySuccess] = answer.markets[0].series;
  Object.assign(notAttempted, {calls: 0, calls_ok: 0, items: 0, valid: false, invalid_reason: 'calls', invalid_words: 'no calls recorded', located_share: null});
  Object.assign(emptySuccess, {calls: 1, calls_ok: 1, items: 0, valid: true, invalid_reason: null, invalid_words: null});
  await mount(answer);
  const rows = [...market('ZA').querySelectorAll('tbody tr:not(.cv42-group-row)')].map(rowCells);
  expect(rows).toEqual([
    ['YouTube trending board', '1 200', '', '26%', '12 of 12', 'Usable'],
    ['TikTok hashtag board', '0', '', 'Market board', '1 of 1', 'Usable'],
  ]);
  const fold = market('ZA').querySelector('details[data-not-collected]');
  expect(fold.open).toBe(false);
  expect(fold.querySelector('summary').textContent).toBe('Not collected on this day: 1 source');
  expect([...fold.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['TikTok local feed']);
  expect(fold.textContent).toContain('The summary above counts it as not usable.');
  expect(text()).not.toContain('Calls failed');
});

const tidyDay = () => {
  const answer = clone(coverageFixture);
  const row = (series_words, group, extra = {}) => ({
    platform: null, series: 'board_x', series_words, group, group_words: {charts: 'Charts and app stores', boards: 'Trending boards and feeds', posts: 'Posts from platforms and followed accounts', news: 'News feeds'}[group],
    calls: 1, calls_ok: 1, items: 50, valid: true, invalid_reason: null, invalid_words: null, located_share: null, located_words: null, ...extra,
  });
  answer.markets[0].series = [
    row('News feed, eNCA', 'news', {series: 'news_rss', items: 12}),
    row('Google Play top free apps', 'charts', {calls: 0, calls_ok: 0, items: 0, valid: false, invalid_reason: 'calls', invalid_words: 'no calls recorded', located_words: 'National chart'}),
    row('Apple Music chart, songs', 'charts', {series: 'board_apple_music', located_words: 'National chart'}),
    row('Apple Music chart, music videos', 'charts', {series: 'board_apple_music', located_words: 'National chart'}),
    row('Shazam national chart', 'charts', {items: 200, located_words: 'National chart'}),
    row('YouTube trending board, Music', 'boards', {series: 'board_youtube', located_share: 0.04}),
    row('YouTube trending board, all categories', 'boards', {series: 'board_youtube', located_share: 0}),
    row('X, accounts we follow', 'posts', {series: 'panel_x_hub', items: 80, valid: false, invalid_reason: 'calls', invalid_words: 'could not be read', calls: 6, calls_ok: 2}),
    row('TikTok search, top posts', 'posts', {series: 'search', items: 900, located_share: 0.7}),
    row('News feed, Business Day', 'news', {series: 'news_rss', calls: 0, calls_ok: 0, items: 0, valid: false, invalid_reason: 'calls', invalid_words: 'no calls recorded'}),
  ];
  answer.markets[0].summary = {posts: 1412, platforms: 3, located_share: 0.5, sources: 10, sources_usable: 7};
  return answer;
};

test('the sources table reads in parts, each with its usable count and posts, in a fixed order', async () => {
  await mount(tidyDay());
  const za = market('ZA');
  expect([...za.querySelectorAll('tbody')].map((t) => t.getAttribute('data-part'))).toEqual(['charts', 'boards', 'posts', 'news']);
  expect([...za.querySelectorAll('.cv42-group-row')].map(rowCells).map((c) => c[0])).toEqual([
    'Charts and app stores3 of 3 usable, 300 posts from usable sources',
    'Trending boards and feeds2 of 2 usable, 100 posts from usable sources',
    'Posts from platforms and followed accounts1 of 2 usable, 900 posts from usable sources',
    'News feeds1 of 1 usable, 12 posts from usable sources',
  ]);
  expect(za.querySelector('.cv42-group-row th').getAttribute('scope')).toBe('rowgroup');
  expect(za.querySelector('.cv42-group-row th').getAttribute('colspan')).toBe('6');
  // The summary keeps the service's counts: the two sources with no call still count as not usable.
  expect([...za.querySelectorAll('.cv42-stat')].map((li) => li.textContent.replace(/\s+/g, ' ').trim())[3]).toBe('7 of 10 sources usable');
});

test('inside a part the usable sources lead, then the most posts, then the name', async () => {
  await mount(tidyDay());
  const names = (part) => [...market('ZA').querySelectorAll('tbody[data-part="' + part + '"] tr:not(.cv42-group-row) th')].map((th) => th.textContent);
  expect(names('charts')).toEqual(['Shazam national chart', 'Apple Music chart, music videos', 'Apple Music chart, songs']);
  expect(names('boards')).toEqual(['YouTube trending board, all categories', 'YouTube trending board, Music']);
  expect(names('posts')).toEqual(['TikTok search, top posts', 'X, accounts we follow']);
  expect(names('news')).toEqual(['News feed, eNCA']);
});

test('sources with no call fold into one closed line per market that expands to their names', async () => {
  await mount(tidyDay());
  const za = market('ZA');
  const fold = za.querySelector('details[data-not-collected]');
  expect(fold.querySelector('summary').textContent).toBe('Not collected on this day: 2 sources');
  expect(fold.open).toBe(false);
  expect([...fold.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['Google Play top free apps', 'News feed, Business Day']);
  expect(fold.textContent).toContain('No calls were recorded for these sources on this day. The summary above counts them as not usable.');
  expect(za.querySelector('table').textContent).not.toContain('Google Play top free apps');
  expect(za.querySelector('table').textContent).not.toContain('No calls recorded');
  // The fold sits under the table, inside the market.
  expect(za.querySelector('table').compareDocumentPosition(fold) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(market('NG').querySelector('details[data-not-collected]')).toBeNull();
});

test('a chart or board with no place to measure says what it is; anything else stays not measured', async () => {
  await mount(tidyDay());
  const located = (name) => {
    const th = [...market('ZA').querySelectorAll('tbody th[scope="row"]')].find((cell) => cell.textContent === name);
    return th.parentElement.querySelector('td[data-label="Located"]').textContent;
  };
  expect(located('Apple Music chart, songs')).toBe('National chart');
  expect(located('YouTube trending board, Music')).toBe('4%');
  expect(located('YouTube trending board, all categories')).toBe('0%');
  expect(located('News feed, eNCA')).toBe('Not measured');
});

test('every source row has its own key, so two rows of one series both render', async () => {
  const answer = tidyDay();
  const errors = [];
  const realError = console.error;
  console.error = (...args) => { errors.push(args.join(' ')); };
  try {
    await mount(answer);
  } finally {
    console.error = realError;
  }
  expect(errors.filter((e) => /same key|unique "key"/i.test(e))).toEqual([]);
  const rows = market('ZA').querySelectorAll('tbody tr:not(.cv42-group-row)');
  expect(rows.length).toBe(8);
});

test('a market with every source uncollected shows the fold and no empty table', async () => {
  const answer = tidyDay();
  answer.markets[0].series = answer.markets[0].series.filter((r) => r.calls === 0);
  await mount(answer);
  const za = market('ZA');
  expect(za.querySelector('table')).toBeNull();
  expect(za.querySelector('details[data-not-collected] summary').textContent).toBe('Not collected on this day: 2 sources');
  expect(za.textContent).not.toContain('Nothing was collected for South Africa');
});

test('the parts and the fold stack on a phone: the part row spans the row and the fold is styled like the method line', () => {
  const css = readFileSync(new URL('../../styles/coverage42.css', import.meta.url), 'utf8');
  const phone = css.split('@media (max-width: 639px) {')[1] || '';
  expect(phone).toContain('.cv42-stack.cv42-sources-table tr.cv42-group-row { display: block;');
  expect(css).toContain('.cv42-method > summary, .cv42-idle > summary {');
});

test('coverage explains when per-source last-success timestamps are unavailable', async () => {
  await mount();
  expect(text()).toContain('Last successful collection times are unavailable for individual sources.');
});

test('a market with nothing collected says so', async () => {
  await mount();
  const ke = market('KE');
  expect(ke.querySelector('table')).toBeNull();
  expect(ke.textContent).toContain('Nothing was collected for Kenya on this day');
});

test('credits by market, job and lane are charged Figures with a total', async () => {
  await mount();
  const credits = section('credits');
  // Restated 2 October 2026: "Lane" is pipeline jargon; the column now reads "Searched as".
  /* Charts, 3 October 2026: the credits chart above the table carries its own
     hidden table and a query id on each bar, so these reads are scoped to the
     credits table itself; the table and its figures are unchanged. */
  const table = credits.querySelector('table.cv42-credits');
  expect(rowCells(table.querySelector('thead tr'))).toEqual(['Market', 'Job', 'Searched as', 'Credits charged']);
  const rows = [...table.querySelectorAll('tbody tr')].map(rowCells);
  expect(rows).toEqual([
    ['South Africa', 'Collect', 'Feeds and boards', '310'],
    ['South Africa', 'Collect', 'Expansion searches', '120'],
    ['No market', 'Ask', 'Ask live calls', '32'],
  ]);
  expect(credits.querySelector('[data-query-id="q_credits_total"]').textContent).toBe('462');
  expect(credits.textContent).toContain('462 credits charged');
  expect(credits.querySelectorAll('.cv42-fig[data-query-id]')).toHaveLength(4);
});

test('model spend is shown against the daily cap, stage by stage', async () => {
  await mount();
  const spend = section('model-spend');
  expect(spend.textContent.replace(/\s+/g, ' ')).toContain('USD 3.40 of the USD 20.00 daily cap (17%)');
  expect(spend.querySelector('[data-query-id="q_model_usd"]')).not.toBeNull();
  const stages = [...spend.querySelectorAll('li')].map((li) => li.textContent.replace(/\s+/g, ' ').trim());
  expect(stages).toEqual(['Detect: USD 0.40', 'Brief: USD 3.00']);
  expect(spend.querySelector('[data-query-id="q_model_usd_brief"]')).not.toBeNull();
});

test('model spend over the cap says so, and missing spend is said plainly', async () => {
  const over = clone(coverageFixture);
  over.model_spend.usd.value = 23.5;
  await mount(over);
  expect(section('model-spend').textContent).toContain('over the cap');
  flushSync(() => root.unmount());
  root = createRoot(host);
  const none = clone(coverageFixture);
  none.model_spend = {usd: null, cap_usd: 20.0, stages: [], text: 'Model spend is not recorded per run yet'};
  await mount(none);
  expect(section('model-spend').textContent).toContain('Model spend is not recorded per run yet');
  expect(section('model-spend').querySelector('[data-query-id]')).toBeNull();
});

test('the runs of the day are listed by stage and status in words, with the error', async () => {
  // Restated after the design review (2 October 2026): the Note column is gone; a note is its own row under its run, only when there is one.
  await mount();
  const rows = [...section('runs').querySelectorAll('tbody tr')].map(rowCells);
  expect(rows).toEqual([
    ['Collect', 'Finished', '00:30 to 02:11'],
    ['Detect', 'Failed', '03:00 to 03:04'],
    ['Detection query timed out'],
    ['Brief', 'Finished', '06:00 to 06:14'],
  ]);
  // Restated after the second design review (2 October 2026): every run shares UTC+02:00, so the zone is said once under the table.
  expect(section('runs').querySelector('.cv42-zone-note').textContent).toBe('Times are South African time, UTC+02:00.');
  expect(section('runs').querySelectorAll('.cv42-note-row')).toHaveLength(1);
  expect(section('runs').querySelector('thead').textContent).not.toContain('Note');
});

test('reported running rows stay alongside terminal run records', async () => {
  const recorded = clone(coverageFixture);
  recorded.runs.push({
    run_id: 'r_recorded_running',
    stage: 'scorecard',
    status: 'running',
    started_at: '2026-09-30T07:30:00+02:00',
    finished_at: null,
    error: null,
  });
  await mount(recorded);
  const runs = section('runs');
  const rows = [...runs.querySelectorAll('tbody tr')].map(rowCells);
  // Restated after the design review (2 October 2026): a note is its own row, so four runs and one note make five rows.
  expect(rows.length).toBe(5);
  expect(rows.slice(0, 4)).toEqual([
    ['Collect', 'Finished', '00:30 to 02:11'],
    ['Detect', 'Failed', '03:00 to 03:04'],
    ['Detection query timed out'],
    ['Brief', 'Finished', '06:00 to 06:14'],
  ]);
  // Restated after the second design review (2 October 2026): the shared zone is said once under the table.
  expect(rows[4]).toEqual(['Scorecard', 'Reported running', 'from 07:30']);
  expect(text()).toContain('Run statuses are recorded by the pipeline; a running record does not confirm the job is still active.');
});

test('run times keep UTC offsets and mark naive timestamps as timezone unknown', async () => {
  const timed = clone(coverageFixture);
  timed.runs = [
    {run_id: 'r_utc', stage: 'collect', status: 'ok', started_at: '2026-09-30T00:30:00Z', finished_at: '2026-09-30T02:11:03Z', error: null},
    {run_id: 'r_naive', stage: 'detect', status: 'ok', started_at: '2026-09-30T03:00:00', finished_at: '2026-09-30T03:04:10', error: null},
  ];
  await mount(timed);
  const rows = [...section('runs').querySelectorAll('tbody tr')].map(rowCells);
  expect(rows).toEqual([
    ['Collect', 'Finished', '00:30 to 02:11 UTC'],
    ['Detect', 'Finished', '03:00 to 03:04 timezone unknown'],
  ]);
});

test('an understand run that finished with failed writes reads as degraded and says what failed', async () => {
  const degraded = clone(coverageFixture);
  degraded.runs.splice(1, 0, {
    run_id: 'r_understand', stage: 'understand', status: 'ok',
    started_at: '2026-09-30T02:15:00+02:00', finished_at: '2026-09-30T02:50:00+02:00', error: null,
    degraded: ['Enrichment', 'Clustering for Nigeria'],
  });
  await mount(degraded);
  const rows = [...section('runs').querySelectorAll('tbody tr')].map(rowCells);
  expect(rows.slice(0, 3)).toEqual([
    ['Collect', 'Finished', '00:30 to 02:11'],
    ['Understand', 'Finished, degraded', '02:15 to 02:50'],
    ['Did not complete: Enrichment; Clustering for Nigeria'],
  ]);
  expect(section('runs').querySelector('[data-degraded="true"]')).not.toBeNull();
});

test('a run error that is an engine code is written in words', async () => {
  const coded = clone(coverageFixture);
  coded.runs[1].error = 'agent_unavailable';
  await mount(coded);
  expect(rowCells(section('runs').querySelectorAll('tbody tr')[2])).toEqual(['Agent unavailable']);
});

test('no model spend block at all still says spend is not recorded', async () => {
  const none = clone(coverageFixture);
  none.model_spend = null;
  await mount(none);
  expect(section('model-spend').textContent).toContain('Model spend is not recorded per run yet');
});

// Restated 4 October 2026: the service now names when the learn job writes the scorecard (core/api/coverage.py NO_SCORECARD).
test('the service wait line for the scorecard is shown as sent', async () => {
  const waiting = clone(coverageFixture);
  waiting.scorecard_text = 'No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, for the week before';
  await mount(waiting);
  expect(section('scorecard').textContent.trim().endsWith('No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, for the week before.')).toBe(true);
});

test('no runs is said plainly', async () => {
  const empty = clone(coverageFixture);
  empty.runs = [];
  await mount(empty);
  expect(section('runs').textContent).toContain('No runs recorded for this day');
});

test('the not-seen list is shown as the service sends it', async () => {
  await mount();
  const items = [...section('not-seen').querySelectorAll('li')].map((li) => li.textContent);
  expect(items).toEqual(coverageFixture.not_seen);
  expect(section('not-seen').querySelector('h2').textContent).toBe('What 42 does not see');
});

test('without a service wait line the page says the same words as the service', async () => {
  const silent = clone(coverageFixture);
  delete silent.scorecard_text;
  await mount(silent);
  expect(section('scorecard').textContent).toContain('No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, for the week before.');
});

test('no scorecard yet shows the wait line', async () => {
  await mount();
  expect(section('scorecard').textContent).toContain('No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, for the week before');
  expect(section('scorecard').querySelector('table')).toBeNull();
});

test('a scorecard shows each measure per market as a Figure for its week, shares as percentages', async () => {
  const withCard = clone(coverageFixture);
  const fig = (value, unit) => ({value, unit, query_id: 'q_scorecard_x', run_id: 'r_learn_20260921_01', result_hash: 'sha256:' + unit});
  const za = {
    market: 'ZA', reasons: {},
    time_to_detect: fig(2.0, 'days'),
    lead_time: fig(1.5, 'days'),
    precision: fig(0.75, 'share of reviewed top trends marked real'),
    recall: fig(0.62, 'share of moments surfaced within 24 hours'),
    breadth: null,
    cost_per_confirmed_trend: fig(48.5, 'credits per confirmed trend'),
  };
  const ke = {...za, market: 'KE', lead_time: fig(null, 'days'), reasons: {lead_time: 'nothing to measure in this market this week'}};
  withCard.scorecard = {week: '2026-09-21', week_end: '2026-09-27', markets: [za, ke]};
  withCard.scorecard_text = null;
  await mount(withCard);
  const card = section('scorecard');
  // Restated after the second design review (2 October 2026): a week inside one month reads "21 to 27 September 2026".
  expect(card.textContent).toContain('Week of 21 to 27 September 2026');
  expect([...card.querySelectorAll('thead th')].map((th) => th.textContent)).toEqual(['Measure', 'South Africa', 'Kenya']);
  // Restated 2 October 2026: numbers lead in each cell and what a measure means sits once, muted, under its name.
  const rows = [...card.querySelectorAll('tbody tr')].map(rowCells);
  expect(rows).toEqual([
    ['Time to detectDays', '2 days', '2 days'],
    ['Lead timeDays', '1.5 days', 'Not measured yet1'],
    ['PrecisionShare of reviewed top trends marked real', '75%', '75%'],
    ['RecallShare of moments surfaced within 24 hours', '62%', '62%'],
    ['Breadth', 'Not measured yet', 'Not measured yet'],
    ['Cost per confirmed trend', '48.5 credits', '48.5 credits'],
  ]);
  expect([...card.querySelectorAll('.cv42-measure-unit')].map((el) => el.textContent)).toEqual(['Days', 'Days', 'Share of reviewed top trends marked real', 'Share of moments surfaced within 24 hours']);
  expect([...card.querySelectorAll('.cv42-unmeasured')]).toHaveLength(3);
  // Restated after the design review (2 October 2026): a reason is a numbered note under the table, so the cell stays one line.
  expect([...card.querySelectorAll('.cv42-notes li')].map((li) => li.textContent)).toEqual(['Kenya, lead time: nothing to measure in this market this week.']);
  expect(card.querySelector('.cv42-note-mark a').getAttribute('href')).toBe('#' + card.querySelector('.cv42-notes li').id);
  expect(card.querySelectorAll('[data-query-id="q_scorecard_x"]')).toHaveLength(9);
  expect(card.textContent).not.toContain('No weekly scorecard yet');
});

test('a failed read shows the message and Try again reads again', async () => {
  let n = 0;
  serve(() => (++n === 1 ? reply(500, {error: 'internal', message: 'Coverage could not be read.'}) : reply(200, coverageFixture)));
  flushSync(() => root.render(<CoveragePage42 date="2026-09-30" />));
  await settle();
  expect(host.querySelector('[role="alert"]').textContent).toBe('Coverage could not be read.');
  click(button('Try again'));
  await settle();
  expect(calls).toHaveLength(2);
  expect(market('ZA')).not.toBeNull();
});

test('a 401 hands over to the passcode screen', async () => {
  let asked = 0;
  serve(reply(401, {error: 'unauthorized', message: 'Missing or wrong passcode.'}));
  flushSync(() => root.render(<CoveragePage42 date="2026-09-30" onAuth={() => { asked += 1; }} />));
  await settle();
  expect(asked).toBe(1);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});

test('the data leads: the method notes sit closed in one disclosure under the heading, not as paragraphs above the tables', async () => {
  await mount();
  const page = host.querySelector('.cv42');
  const method = page.querySelector('details[data-section="method"]');
  expect(method).not.toBeNull();
  expect(method.open).toBe(false);
  expect(method.querySelector('summary').textContent).toBe('How to read these numbers');
  expect(method.textContent).toContain('Successful calls are shown against total attempts.');
  expect(method.textContent).toContain('Last successful collection times are unavailable for individual sources.');
  expect(method.textContent).toContain('Run statuses are recorded by the pipeline');
  const children = [...page.children];
  expect(children.filter((node) => node.tagName === 'P')).toHaveLength(0);
  expect(children.indexOf(method)).toBe(1);
  expect(children[2].getAttribute('data-market')).not.toBeNull();
});

test('no raw engine codes reach the page', async () => {
  await mount();
  expect(text()).not.toMatch(/\b[a-z]+_[a-z_]+\b/);
  expect(text()).not.toMatch(/gen ?z|millennial|youth|generation/i);
});

test('the stylesheet assigns page and section roles and uses no red', () => {
  const css = readFileSync(new URL('../../styles/coverage42.css', import.meta.url), 'utf8');
  const page = css.split('.cv42-heading {')[1]?.split('}')[0] || '';
  const section = css.split('.cv42-part-title {')[1]?.split('}')[0] || '';
  expect(page).toContain('font-size: var(--type-page-title)');
  expect(page).toContain('line-height: var(--leading-page-title)');
  expect(section).toContain('font-size: var(--type-section-title)');
  expect(section).toContain('line-height: var(--leading-section-title)');
  /* The accent is Ogilvy red; the page has no primary action, so only the focus ring may use it. */
  const painted = css.split('\n').filter((line) => !/outline/.test(line)).join('\n');
  expect(painted).not.toMatch(/--accent|#e4002b|\bred\b/i);
});

test('the scorecard week reads from its start to its end, and from its start alone when no end is sent', async () => {
  const withCard = clone(coverageFixture);
  withCard.scorecard = {week: '2026-09-21', week_end: '2026-09-27', markets: []};
  withCard.scorecard_text = null;
  await mount(withCard);
  // Restated after the second design review (2 October 2026): a week inside one month names the month once.
  expect(section('scorecard').querySelector('p').textContent).toBe('Week of 21 to 27 September 2026');
  flushSync(() => root.unmount());
  root = createRoot(host);
  withCard.scorecard = {week: '2026-09-21', markets: []};
  await mount(withCard);
  expect(section('scorecard').querySelector('p').textContent).toBe('Week of 21 September 2026');
});

/* ---------- moving between days (2 October 2026) ---------- */

const emptyDay = (extra = {}) => ({
  ...clone(coverageFixture),
  date: '2026-10-02', today: '2026-10-02', latest_day_with_data: '2026-09-30', collection: 'not_recorded',
  markets: coverageFixture.markets.map((m) => ({...m, series: [], summary: null})),
  credits: {total: null, rows: []}, runs: [], ...extra,
});

test('the day picker links the previous and next day by hash, and stops the next day at today', async () => {
  await mount({...clone(coverageFixture), today: '2026-10-02'});
  const nav = host.querySelector('nav[aria-label="Choose a day"]');
  const links = [...nav.querySelectorAll('a')].map((a) => [a.textContent.replace(/\s+/g, ' ').trim(), a.getAttribute('href')]);
  expect(links).toEqual([['‹ Previous day', '#/coverage?date=2026-09-29'], ['Next day ›', '#/coverage?date=2026-10-01']]);
  expect(nav.querySelector('input[type="date"]').value).toBe('2026-09-30');
  expect(nav.querySelector('input[type="date"]').getAttribute('max')).toBe('2026-10-02');
  flushSync(() => root.unmount());
  root = createRoot(host);
  await mount(emptyDay(), {date: '2026-10-02'});
  const next = [...host.querySelectorAll('nav .cv42-day-step')].pop();
  expect(next.tagName).toBe('SPAN');
  expect(next.getAttribute('aria-disabled')).toBe('true');
});

test('without a date prop the page reads the day from the hash and reads again when the hash changes', async () => {
  window.location.hash = '#/coverage?date=2026-09-29';
  serve(reply(200, coverageFixture));
  flushSync(() => root.render(<CoveragePage42 />));
  await settle();
  window.location.hash = '#/coverage?date=2026-09-28';
  window.dispatchEvent(new HashChangeEvent('hashchange'));
  await settle();
  window.location.hash = '';
  expect(calls.map((c) => c.url)).toEqual(['/api/coverage?date=2026-09-29', '/api/coverage?date=2026-09-28']);
});

test('choosing a day in the date field moves the hash to that day', async () => {
  await mount();
  const input = host.querySelector('input[type="date"]');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  flushSync(() => { setter.call(input, '2026-09-21'); input.dispatchEvent(new Event('input', {bubbles: true})); });
  expect(window.location.hash).toBe('#/coverage?date=2026-09-21');
  window.location.hash = '';
});

test('a day with no source says collection is not recorded yet and offers the latest day with data, once', async () => {
  await mount(emptyDay(), {date: '2026-10-02'});
  const notice = section('day-notice');
  expect(notice.getAttribute('role')).toBe('status');
  expect(notice.textContent).toContain('No collection recorded for today yet.');
  const back = notice.querySelector('a');
  expect(back.textContent).toBe('Open 30 September 2026');
  expect(back.getAttribute('href')).toBe('#/coverage?date=2026-09-30');
  expect(host.querySelector('[data-market]')).toBeNull();
  expect(text()).not.toContain('Nothing was collected for');
});

test.each([
  ['running', 'Collection is reported running for this day.'],
  ['failed', 'Collection ran on this day and failed, so no source was recorded.'],
  ['empty', 'Collection finished on this day but recorded no source.'],
  ['not_recorded', 'No collection recorded for this day yet.'],
])('an empty day in state %s says so in words', async (state, words) => {
  await mount(emptyDay({date: '2026-09-28', collection: state}), {date: '2026-09-28'});
  expect(section('day-notice').textContent).toContain(words);
  expect(section('day-notice').getAttribute('data-state')).toBe(state);
});

test('when no day has data the notice says what fills it, with no link', async () => {
  await mount(emptyDay({latest_day_with_data: null}), {date: '2026-10-02'});
  expect(section('day-notice').textContent).toContain('No day has collected sources yet. Coverage fills after the first collection run.');
  expect(section('day-notice').querySelector('a')).toBeNull();
});

test('the sources part links to Fieldwork and says nothing more about sources', async () => {
  await mount();
  const link = section('sources').querySelector('a');
  expect(link.textContent).toBe('See every source in Fieldwork');
  expect(link.getAttribute('href')).toBe('#/fieldwork');
  expect(section('sources').textContent).toBe('See every source in Fieldwork');
});

test('the coverage date hash reads only a real calendar day', async () => {
  const {parseCoverageDate, coverageHash} = await import('../../router.js');
  expect(parseCoverageDate('#/coverage?date=2026-09-30')).toBe('2026-09-30');
  expect(parseCoverageDate('#/coverage')).toBeNull();
  expect(parseCoverageDate('#/coverage?date=2026-02-30')).toBeNull();
  expect(parseCoverageDate('#/coverage?date=30-09-2026')).toBeNull();
  expect(coverageHash('2026-09-30')).toBe('#/coverage?date=2026-09-30');
  expect(coverageHash(null)).toBe('#/coverage');
});

test('the day field shows the day in words and keeps the native field, named, for picking', async () => {
  await mount();
  const field = host.querySelector('.cv42-day-field');
  expect(field.querySelector('.cv42-day-full').textContent).toBe('30 September 2026');
  expect(field.querySelector('.cv42-day-short').textContent).toBe('30 Sept 2026');
  const input = field.querySelector('input[type="date"]');
  expect(input.getAttribute('aria-label')).toBe('Choose a day, showing 30 September 2026');
  expect([...host.querySelectorAll('.cv42-day-step')].map((a) => a.getAttribute('aria-label'))).toEqual(['Previous day, 29 September 2026', 'Next day, 1 October 2026']);
});

test('every phone-stacked figure carries its column name as a label', async () => {
  await mount();
  const cells = [...host.querySelectorAll('.cv42-stack td:not(.cv42-bar-col):not(.cv42-state)')].filter((td) => !td.closest('.cv42-note-row'));
  expect(cells.length).toBeGreaterThan(0);
  expect(cells.filter((td) => !td.getAttribute('data-label'))).toEqual([]);
});

test('a week across two months keeps each date whole', async () => {
  const withCard = clone(coverageFixture);
  withCard.scorecard = {week: '2026-09-28', week_end: '2026-10-04', markets: []};
  withCard.scorecard_text = null;
  await mount(withCard);
  const line = section('scorecard').querySelector('p');
  expect(line.textContent).toBe('Week of 28 September 2026 to 4 October 2026');
  expect([...line.querySelectorAll('.cv42-nowrap')].map((el) => el.textContent)).toEqual(['28 September 2026', '4 October 2026']);
});

test('runs with different zones keep the zone on each time and add no shared note', async () => {
  const mixed = clone(coverageFixture);
  mixed.runs = [
    {run_id: 'a', stage: 'collect', status: 'ok', started_at: '2026-09-30T00:30:00Z', finished_at: '2026-09-30T02:11:03Z', error: null},
    {run_id: 'b', stage: 'detect', status: 'ok', started_at: '2026-09-30T03:00:00+02:00', finished_at: '2026-09-30T03:04:10+02:00', error: null},
  ];
  await mount(mixed);
  expect([...section('runs').querySelectorAll('tbody tr')].map(rowCells).map((r) => r[2])).toEqual(['00:30 to 02:11 UTC', '03:00 to 03:04 UTC+02:00']);
  expect(section('runs').querySelector('.cv42-zone-note')).toBeNull();
});

/* Charts, 3 October 2026: credits are drawn per job as a bar list with each
   bar keeping its query id, and model spend as a bullet bar against the cap.
   The tables and lines stay. */
test('credits by job are drawn as bars and model spend as a bullet against its cap', async () => {
  await mount();
  const credits = section('credits');
  const bars = credits.querySelector('[data-cv42-credits-chart]');
  expect(bars).not.toBeNull();
  expect(bars.querySelector('.ch42-title').textContent).toBe('Credits by job');
  const labels = [...bars.querySelectorAll('.ch42-bar-label')].map((node) => node.textContent);
  expect(labels).toEqual(['South Africa · Collect · Feeds and boards', 'South Africa · Collect · Expansion searches', 'No market · Ask · Ask live calls']);
  expect([...bars.querySelectorAll('.ch42-bar-fill')].map((node) => node.getAttribute('data-query-id'))).toEqual(['q_credits_za_feed', 'q_credits_za_exp', 'q_credits_ask_live']);
  expect(credits.querySelector('table')).not.toBeNull();
  const spend = section('model-spend');
  const bullet = spend.querySelector('[data-cv42-spend-chart]');
  expect(bullet).not.toBeNull();
  // The sentence above names it, so the bar carries no second title.
  expect(bullet.querySelector('.ch42-title')).toBeNull();
  expect(bullet.querySelector('.ch42-bullet-figure').textContent.replace(/\s+/g, ' ')).toBe('USD 3.40 of USD 20.00');
  expect(spend.textContent.replace(/\s+/g, ' ')).toContain('USD 3.40 of the USD 20.00 daily cap (17%)');
});

test('no credits rows and no measured spend draw no charts', async () => {
  const none = clone(coverageFixture);
  none.credits = {total: null, rows: []};
  none.model_spend = {usd: null, cap_usd: 20.0, stages: [], text: 'Model spend is not recorded per run yet'};
  await mount(none);
  expect(host.querySelector('.ch42')).toBeNull();
});

// Night Desk, 4 October 2026: every scorecard figure gets a meter on its row's
// scale, so markets compare at a glance (Tufte: every number gets a picture).
const scoreFigure = (value, unit) => ({value, unit, query_id: 'q_' + unit.split(' ')[0]});
const scoredWeek = {
  week: '2026-09-21', week_end: '2026-09-27',
  markets: [['ZA', 2, 0.6], ['NG', 3, 0.7], ['KE', 4, null]].map(([market, days, share]) => ({
    market, reasons: share === null ? {precision: 'Nothing reviewed this week'} : {},
    time_to_detect: scoreFigure(days, 'days from first unbiased sighting to first Emerging or Rising'),
    ...(share === null ? {} : {precision: scoreFigure(share, 'share of reviewed top trends marked real')}),
  })),
};

test('each scorecard figure carries a meter scaled within its row, shares on a 0 to 100 scale', async () => {
  await mount({...JSON.parse(JSON.stringify(coverageFixture)), scorecard: scoredWeek});
  const rows = [...section('scorecard').querySelectorAll('tbody tr')];
  const precision = rows.find((row) => row.textContent.includes('Precision'));
  const shares = [...precision.querySelectorAll('.cv42-score-meter')].map((node) => node.style.getPropertyValue('--v'));
  const values = [...precision.querySelectorAll('.cv42-score-cell:not(.cv42-unmeasured) .cv42-score-value')].map((node) => Number.parseFloat(node.textContent) / 100);
  expect(shares.length).toBe(values.length);
  shares.forEach((share, i) => expect(Number(share)).toBeCloseTo(values[i], 3));
  const detect = rows.find((row) => row.textContent.includes('Time to detect'));
  const lengths = [...detect.querySelectorAll('.cv42-score-meter')].map((node) => Number(node.style.getPropertyValue('--v')));
  expect(Math.max(...lengths)).toBe(1);
  for (const meter of section('scorecard').querySelectorAll('.cv42-score-meter')) expect(meter.getAttribute('aria-hidden')).toBe('true');
  for (const cell of section('scorecard').querySelectorAll('.cv42-unmeasured')) expect(cell.querySelector('.cv42-score-meter')).toBeNull();
});

/* Visual QA, 5 October 2026 (CV01): Kenya's cost per confirmed trend printed
   225.83333333333334 credits and ran off the table. */
test('a scorecard figure keeps one decimal and whole numbers whole', async () => {
  const fig = (value, unit) => ({value, unit, query_id: 'q_scorecard', run_id: 'r_learn', result_hash: 'sha256:' + unit});
  const row = (market, cost) => ({market, reasons: {}, time_to_detect: fig(2, 'days'), lead_time: null, precision: null, recall: null, breadth: null,
    cost_per_confirmed_trend: fig(cost, 'credits per confirmed trend')});
  const body = {...clone(coverageFixture), scorecard: {week: '2026-09-28', week_end: '2026-10-04', markets: [row('ZA', 326.6), row('NG', 365.75), row('KE', 225.83333333333334)]}, scorecard_text: null};
  await mount(body);
  const card = section('scorecard').textContent;
  expect(card).toContain('225.8 credits');
  expect(card).toContain('365.8 credits');
  expect(card).toContain('326.6 credits');
  expect(card).toContain('2 days');
  expect(card).not.toContain('225.833');
});

test('a scorecard figure that rounds to one takes the singular', async () => {
  const fig = (value, unit) => ({value, unit, query_id: 'q_scorecard', run_id: 'r_learn', result_hash: 'sha256:' + unit});
  const row = (market, days) => ({market, reasons: {}, time_to_detect: fig(days, 'days'), lead_time: null, precision: null, recall: null, breadth: null, cost_per_confirmed_trend: null});
  const body = {...clone(coverageFixture), scorecard: {week: '2026-09-28', week_end: '2026-10-04', markets: [row('ZA', 0.96), row('NG', 3)]}, scorecard_text: null};
  await mount(body);
  expect(section('scorecard').textContent).toContain('1 day');
  expect(section('scorecard').textContent).not.toContain('1 days');
});
