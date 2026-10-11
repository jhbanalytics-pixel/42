/* Discover's filters apply the moment they change, and Radar follows the
   same ones as the list (wave 8). Every change reads the list and Radar
   again for the newest choice only, keeps the earlier rows on screen,
   dimmed, until the new ones arrive, and lives in the #/explore query. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import todayFixture from './fixtures/today42.json';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {fetchDiscover, fetchRadar} = await import('../../api42.js');
const {Discover42, parseDiscoverQuery, discoverHash} = await import('../../discover42.jsx');

const realFetch = globalThis.fetch;
let calls = [];
let host = null;
let root = null;

const clone = (value) => JSON.parse(JSON.stringify(value));
const reply = (status, body) => ({ok: status >= 200 && status < 300, status, json: async () => body});
const figure = (value, unit) => ({value, unit, query_id: 'q_' + unit.replace(/\W+/g, '_'), run_id: 'r_d1', result_hash: 'sha256:d1'});
const za = todayFixture.markets[0];

function grown(card, order, extra = {}){
  return {
    ...clone(card), tag: null, order, date: '2026-10-20',
    explanation_status: card.explained ? 'explained' : 'failed_checks',
    lifecycle: null, novelty: 'new', diffusion: 'bottom_up', origin: null,
    spread_line: null, reach: figure(10 + order, 'creators in 3 days'), growth: figure(1.5, 'times its usual level'),
    watch_id: null, ...extra,
  };
}

const filters = {
  kinds: ['hashtag', 'sound', 'topic', 'creator'],
  states: ['emerging', 'rising', 'spike'],
  platforms: ['tiktok', 'youtube', 'x', 'reddit'],
};
const pageWith = (titles) => ({
  date: '2026-10-20', market: 'ZA', run_id: 'r_d1',
  items: titles.map((title, i) => grown({...za.cards[0], item_id: 'it_' + title, title}, i + 1)),
  next_cursor: null, held_back: {count: 0, items: []}, filters,
});
const measures = (creators, posts, extra = {}) => ({
  posts7: figure(posts, 'posts in 7 days'), creators7: figure(creators, 'creators in 7 days'),
  measured_posts7: figure(posts, 'posts in 7 days'), measured_creators7: figure(creators, 'creators in 7 days'),
  located7: figure(4, 'posts with a known place in 7 days'), local7: figure(4, 'posts local to the market in 7 days'),
  local_share7: figure(1, 'share of located posts local to the market'), platforms: ['youtube', 'x'], ...extra,
});
const radarWith = (labels, extra = {}) => ({
  date: '2026-10-20', market: 'ZA', held_back_count: 0,
  note: 'Growth needs 14 days of data; showing reach only',
  window: {from: '2026-10-14', to: '2026-10-20', days: 7, platforms: [{platform: 'youtube', days: 7, days_ok: 6}]},
  points: labels.map((label, i) => ({item_id: 'rd_' + label, label, kind: extra.kind || 'creator', state: 'emerging', flag: null,
    growth: null, reach: figure(5, 'creators in 3 days'), measures: measures(20 - i, 40 - i)})),
  ...extra,
});

function serve(routes){
  globalThis.fetch = async (url, init) => {
    calls.push({url: String(url), init});
    for (const [prefix, answer] of routes){
      if (String(url).startsWith(prefix)) return typeof answer === 'function' ? answer(String(url)) : answer;
    }
    return reply(404, {error: 'not_found', message: 'No route'});
  };
}

beforeEach(() => {
  calls = [];
  localStorage.setItem('pulse_passcode', 'test-pass');
  window.history.replaceState(null, '', '#');
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
  window.history.replaceState(null, '', '#');
});
afterAll(() => { GlobalRegistrator.unregister(); });

const settle = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setTimeout(resolve, 0)); };
async function mount(routes, props = {}){
  serve(routes);
  flushSync(() => root.render(<Discover42 region="ZA" {...props} />));
  await settle();
}
const text = () => host.textContent.replace(/\s+/g, ' ');
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const chip = (group, value) => host.querySelector('button[data-filter="' + group + '"][data-value="' + value + '"]');
const tab = (list, label) => [...host.querySelectorAll('[role="tablist"][aria-label="' + list + '"] [role="tab"]')].find((t) => t.textContent.trim() === label);
const discoverCalls = () => calls.map((c) => c.url).filter((u) => u.startsWith('/api/discover?'));
const radarCalls = () => calls.map((c) => c.url).filter((u) => u.startsWith('/api/discover/radar'));
const titles = () => [...host.querySelectorAll('.d42-feed [data-card] h3')].map((h) => h.textContent);
const radarRows = () => [...host.querySelectorAll('[data-section="radar"] tbody th a')].map((a) => a.textContent);
const pressed = (el) => el.getAttribute('aria-pressed');

const base = (page = pageWith(['#alpha', '#beta']), radar = radarWith(['Ayanda', 'Bheki'])) => [
  ['/api/discover/radar', reply(200, radar)],
  ['/api/discover', reply(200, page)],
];

test('the API paths carry several states and platforms as repeated keys and leave empty ones off', async () => {
  serve([['/api/', reply(200, {})]]);
  await fetchDiscover({market: 'ZA', kind: 'creator', state: ['emerging', 'rising'], platform: ['youtube', 'x'], sort: 'order', limit: 50});
  await fetchDiscover({market: 'ZA', state: [], platform: [], sort: 'order', limit: 50});
  await fetchRadar('ZA', 'creator', {state: ['emerging'], platform: ['youtube', 'x']});
  await fetchRadar('ZA', '');
  expect(calls.map((c) => c.url)).toEqual([
    '/api/discover?market=ZA&kind=creator&state=emerging&state=rising&platform=youtube&platform=x&sort=order&limit=50',
    '/api/discover?market=ZA&sort=order&limit=50',
    '/api/discover/radar?market=ZA&kind=creator&state=emerging&platform=youtube&platform=x',
    '/api/discover/radar?market=ZA',
  ]);
});

test('every kind, state, platform and sort change reads the list and Radar again, and Radar carries all but sort', async () => {
  await mount(base());
  expect(discoverCalls()).toHaveLength(1);
  expect(radarCalls()).toHaveLength(1);

  click(tab('Kinds', 'Creators'));
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&kind=creator&sort=order&limit=50');
  expect(radarCalls().at(-1)).toBe('/api/discover/radar?market=ZA&kind=creator');

  click(chip('state', 'emerging'));
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&kind=creator&state=emerging&sort=order&limit=50');
  expect(radarCalls().at(-1)).toBe('/api/discover/radar?market=ZA&kind=creator&state=emerging');

  click(chip('platform', 'tiktok'));
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&kind=creator&state=emerging&platform=tiktok&sort=order&limit=50');
  expect(radarCalls().at(-1)).toBe('/api/discover/radar?market=ZA&kind=creator&state=emerging&platform=tiktok');

  const radarBefore = radarCalls().length;
  const sort = host.querySelector('select[name="sort"]');
  flushSync(() => { sort.value = 'reach'; sort.dispatchEvent(new Event('change', {bubbles: true})); });
  await settle();
  expect(discoverCalls().at(-1)).toContain('sort=reach');
  /* Radar ranks by creators in 7 days whatever the sort, so sort alone does not read it again. */
  expect(radarCalls()).toHaveLength(radarBefore);
});

test('platforms and states are multi-select chips, and All platforms clears the others', async () => {
  await mount(base());
  expect(pressed(chip('platform', ''))).toBe('true');
  click(chip('platform', 'youtube'));
  click(chip('platform', 'x'));
  await settle();
  expect(pressed(chip('platform', 'youtube'))).toBe('true');
  expect(pressed(chip('platform', 'x'))).toBe('true');
  expect(pressed(chip('platform', ''))).toBe('false');
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&platform=youtube&platform=x&sort=order&limit=50');
  expect(radarCalls().at(-1)).toBe('/api/discover/radar?market=ZA&platform=youtube&platform=x');

  click(chip('platform', 'youtube'));
  await settle();
  expect(pressed(chip('platform', 'youtube'))).toBe('false');
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&platform=x&sort=order&limit=50');

  click(chip('state', 'emerging'));
  click(chip('state', 'rising'));
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&state=emerging&state=rising&platform=x&sort=order&limit=50');

  click(chip('platform', ''));
  await settle();
  expect(pressed(chip('platform', ''))).toBe('true');
  expect(pressed(chip('platform', 'x'))).toBe('false');
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&state=emerging&state=rising&sort=order&limit=50');
});

test('the rows change with the filter: the list and Radar show what the newest choice returned', async () => {
  const answer = (url) => {
    if (url.includes('platform=youtube')) return {page: pageWith(['#only_youtube']), radar: radarWith(['Youtube creator'])};
    if (url.includes('kind=creator')) return {page: pageWith(['#creators_a', '#creators_b']), radar: radarWith(['Creator A', 'Creator B'])};
    return {page: pageWith(['#alpha', '#beta']), radar: radarWith(['Ayanda', 'Bheki'])};
  };
  await mount([
    ['/api/discover/radar', (url) => reply(200, answer(url).radar)],
    ['/api/discover', (url) => reply(200, answer(url).page)],
  ]);
  expect(titles()).toEqual(['#alpha', '#beta']);
  expect(radarRows()).toEqual(['Ayanda', 'Bheki']);
  click(tab('Kinds', 'Creators'));
  await settle();
  expect(titles()).toEqual(['#creators_a', '#creators_b']);
  expect(radarRows()).toEqual(['Creator A', 'Creator B']);
  click(chip('platform', 'youtube'));
  await settle();
  expect(titles()).toEqual(['#only_youtube']);
  expect(radarRows()).toEqual(['Youtube creator']);
});

test('while a change loads the earlier rows stay, dimmed and marked busy, and a status says so', async () => {
  let release;
  let releaseRadar;
  await mount([
    ['/api/discover/radar', (url) => (url.includes('platform=youtube') ? new Promise((resolve) => { releaseRadar = resolve; }) : reply(200, radarWith(['Ayanda', 'Bheki'])))],
    ['/api/discover', (url) => (url.includes('platform=youtube') ? new Promise((resolve) => { release = resolve; }) : reply(200, pageWith(['#alpha', '#beta'])))],
  ]);
  click(chip('platform', 'youtube'));
  await settle();
  expect(titles()).toEqual(['#alpha', '#beta']);
  const feed = host.querySelector('.d42-feed');
  expect(feed.getAttribute('aria-busy')).toBe('true');
  expect(feed.classList.contains('d42-dim')).toBe(true);
  expect(host.querySelector('[data-feed-status]').textContent).toContain('Updating');
  expect(radarRows().length).toBe(2);
  expect(host.querySelector('[data-section="radar"]').classList.contains('d42-dim')).toBe(true);
  /* No Load more or Try again while it loads. */
  release(reply(200, pageWith(['#fresh'])));
  releaseRadar(reply(200, radarWith(['Fresh radar'])));
  await settle();
  expect(titles()).toEqual(['#fresh']);
  expect(radarRows()).toEqual(['Fresh radar']);
  expect(host.querySelector('[data-section="radar"]').classList.contains('d42-dim')).toBe(false);
  expect(host.querySelector('.d42-feed').getAttribute('aria-busy')).toBeNull();
  expect(host.querySelector('.d42-feed').classList.contains('d42-dim')).toBe(false);
});

test('a late answer to an older choice cannot replace the newest one, in the list or in Radar', async () => {
  const slow = {};
  await mount([
    ['/api/discover/radar', (url) => {
      if (url.includes('state=emerging') && !url.includes('platform=')) return new Promise((resolve) => { slow.radar = resolve; });
      return reply(200, url.includes('platform=x') ? radarWith(['Fresh radar']) : radarWith(['Ayanda']));
    }],
    ['/api/discover', (url) => {
      if (url.includes('state=emerging') && !url.includes('platform=')) return new Promise((resolve) => { slow.feed = resolve; });
      return reply(200, url.includes('platform=x') ? pageWith(['#fresh']) : pageWith(['#alpha']));
    }],
  ]);
  click(chip('state', 'emerging'));
  await settle();
  const older = calls.filter((c) => c.url.includes('state=emerging') && !c.url.includes('platform='));
  expect(older).toHaveLength(2);
  click(chip('platform', 'x'));
  await settle();
  expect(older.every((c) => c.init.signal.aborted)).toBe(true);
  expect(titles()).toEqual(['#fresh']);
  expect(radarRows()).toEqual(['Fresh radar']);
  slow.feed(reply(200, pageWith(['#stale'])));
  slow.radar(reply(200, radarWith(['Stale radar'])));
  await settle();
  expect(titles()).toEqual(['#fresh']);
  expect(radarRows()).toEqual(['Fresh radar']);
});

test('the choice lives in the query with repeated platform keys, and reload or Back reads it again', async () => {
  window.history.replaceState(null, '', '#/explore?kind=creator&state=emerging&state=rising&platform=youtube&platform=x&sort=reach');
  const entries = window.history.length;
  await mount(base());
  expect(tab('Kinds', 'Creators').getAttribute('aria-selected')).toBe('true');
  expect(pressed(chip('platform', 'youtube'))).toBe('true');
  expect(pressed(chip('platform', 'x'))).toBe('true');
  expect(pressed(chip('state', 'emerging'))).toBe('true');
  expect(pressed(chip('state', 'rising'))).toBe('true');
  expect(host.querySelector('select[name="sort"]').value).toBe('reach');
  expect(discoverCalls()[0]).toBe('/api/discover?market=ZA&kind=creator&state=emerging&state=rising&platform=youtube&platform=x&sort=reach&limit=50');
  expect(radarCalls()[0]).toBe('/api/discover/radar?market=ZA&kind=creator&state=emerging&state=rising&platform=youtube&platform=x');

  click(chip('platform', 'reddit'));
  await settle();
  expect(window.location.hash).toBe('#/explore?kind=creator&state=emerging&state=rising&platform=youtube&platform=x&platform=reddit&sort=reach');
  click(chip('platform', ''));
  click(chip('state', ''));
  await settle();
  expect(window.location.hash).toBe('#/explore?kind=creator&sort=reach');
  expect(window.history.length).toBe(entries);

  /* A comma list, as an older link may carry it, reads the same way. */
  expect(parseDiscoverQuery('#/explore?platform=youtube,x&state=rising')).toEqual({kind: '', state: ['rising'], platform: ['youtube', 'x'], sort: 'order'});
  expect(discoverHash({kind: '', state: [], platform: ['youtube', 'x'], sort: 'order'})).toBe('#/explore?platform=youtube&platform=x');
});

test('the summary line counts the results and names every choice, and Clear filters undoes them', async () => {
  window.history.replaceState(null, '', '#/explore?kind=creator&state=emerging&platform=youtube&platform=x');
  await mount(base());
  const summary = host.querySelector('[data-filter-summary]');
  expect(summary.textContent).toContain('2 creators, South Africa, YouTube and X, Emerging');
  const clear = [...summary.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Clear filters');
  expect(clear).toBeDefined();
  click(clear);
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&sort=order&limit=50');
  expect(host.querySelector('[data-filter-summary]').textContent).toContain('2 results, South Africa');
  expect(host.querySelector('[data-filter-summary]').textContent).not.toContain('Creators');
  expect([...host.querySelectorAll('[data-filter-summary] button')].some((b) => b.textContent.trim() === 'Clear filters')).toBe(false);
  expect(tab('Kinds', 'All kinds').getAttribute('aria-selected')).toBe('true');
  expect(pressed(chip('platform', ''))).toBe('true');
});

test('a filter with no rows says so in plain words and offers Clear filters', async () => {
  const none = {...pageWith([]), held_back: {count: 0, items: []}};
  window.history.replaceState(null, '', '#/explore?kind=creator&state=emerging&platform=tiktok');
  await mount([
    ['/api/discover/radar', reply(200, {...radarWith([]), points: []})],
    ['/api/discover', reply(200, none)],
  ]);
  const feed = host.querySelector('.d42-feed');
  expect(feed.textContent).toContain('No TikTok creators that are emerging match these filters.');
  expect(host.querySelector('[data-filter-summary]').textContent).toContain('0 creators, South Africa, TikTok, Emerging');
  click([...feed.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Clear filters'));
  await settle();
  expect(discoverCalls().at(-1)).toBe('/api/discover?market=ZA&sort=order&limit=50');
});

test('the market is a segmented control and the filters sit behind a Filters button on a phone', async () => {
  await mount(base());
  const labels = [...host.querySelectorAll('[role="tablist"][aria-label="Markets"] [role="tab"]')].map((t) => t.textContent.trim());
  expect(labels).toEqual(['South Africa', 'Nigeria', 'Kenya', 'All']);
  expect(host.querySelector('[role="tablist"][aria-label="Markets"]').classList.contains('d42-segment')).toBe(true);
  const open = host.querySelector('button.d42-filters-open');
  expect(open.getAttribute('aria-expanded')).toBe('false');
  expect(open.getAttribute('aria-controls')).toBe('d42-filter-groups');
  click(chip('platform', 'youtube'));
  await settle();
  expect(host.querySelector('button.d42-filters-open').textContent).toContain('Filters');
  expect(host.querySelector('button.d42-filters-open .d42-filters-count').textContent).toBe('1');
  click(host.querySelector('button.d42-filters-open'));
  expect(host.querySelector('button.d42-filters-open').getAttribute('aria-expanded')).toBe('true');
  expect(host.querySelector('#d42-filter-groups').getAttribute('data-open')).toBe('true');
  click([...host.querySelectorAll('#d42-filter-groups button')].find((b) => b.textContent.trim() === 'Show results'));
  expect(host.querySelector('button.d42-filters-open').getAttribute('aria-expanded')).toBe('false');
});

test('platform chips carry the platform mark and its name, and a platform without a mark keeps its letter', async () => {
  await mount(base());
  const youtube = chip('platform', 'youtube');
  expect(youtube.textContent).toContain('YouTube');
  expect(youtube.querySelector('svg[data-logo="youtube"]')).not.toBeNull();
  expect(chip('platform', 'x').querySelector('.pl-monogram')).not.toBeNull();
});

test('Radar rows show platform marks with names, a Local bar with its words, one tidy info line and a sticky header', async () => {
  const radar = radarWith(['Ayanda'], {points: [{item_id: 'rd_1', label: 'Ayanda', kind: 'creator', state: 'emerging', flag: null, growth: null,
    reach: figure(5, 'creators in 3 days'), measures: measures(12, 40, {platforms: ['youtube', 'twitter', 'tiktok']})}]});
  await mount(base(undefined, radar));
  const section = host.querySelector('[data-section="radar"]');
  const marks = [...section.querySelectorAll('td[data-col="platforms"] [role="img"]')];
  expect(marks.map((m) => m.getAttribute('aria-label'))).toEqual(['YouTube', 'X', 'TikTok']);
  expect(section.querySelector('td[data-col="platforms"] svg[data-logo="youtube"]')).not.toBeNull();
  const place = section.querySelector('td[data-col="place"]');
  expect(place.textContent.replace(/\s+/g, ' ').trim()).toBe('100% of 4');
  expect(place.querySelector('.d42-local-bar > span').style.width).toBe('100%');
  const info = section.querySelectorAll('.d42-info');
  expect(info).toHaveLength(1);
  expect(info[0].querySelector('svg.d42-info-icon')).not.toBeNull();
  expect(info[0].textContent).toContain('Growth needs 14 days of data; showing reach only');
  expect(info[0].querySelector('[data-window-short]').textContent).toBe('Short collection days: YouTube 6 of 7 days');
  expect(section.querySelectorAll('.d42-note')).toHaveLength(0);
  expect(section.querySelector('.d42-table-wrap').getAttribute('data-sticky')).toBe('header');
});

test('the table header says Accounts posting, last 7 days, with its help as a tooltip and an accessible description, and no second column counts accounts', async () => {
  await mount(base());
  const head = host.querySelector('[data-section="radar"] thead th.d42-creators-head');
  const help = 'Different accounts that posted about this in the last 7 days. One account counts once, however many times it posted.';
  expect(head.childNodes[0].textContent.trim()).toBe('Accounts posting');
  expect(head.querySelector('.d42-sub').textContent).toBe('last 7 days');
  expect(head.getAttribute('title')).toBe(help);
  const described = host.querySelector('#' + head.getAttribute('aria-describedby'));
  expect(described.textContent).toBe(help);
  const posts = host.querySelector('[data-section="radar"] thead th[data-col="posts"]');
  expect(posts.childNodes[0].textContent.trim()).toBe('Posts');
  expect(posts.querySelector('.d42-sub').textContent).toBe('last 7 days');
  const names = [...host.querySelectorAll('[data-section="radar"] thead th')].map((th) => th.textContent);
  expect(names.filter((n) => /creators|accounts/i.test(n.replace(help, '')))).toHaveLength(1);
  expect(host.querySelector('[data-section="radar"]').textContent).not.toMatch(/Creators in 7 days/);
  expect(host.querySelector('[data-strip-caption]').textContent).toContain('sorted by accounts posting in the last 7 days');
});

const KINDS = [
  ['', 'Trend', 'trend', 'Different accounts that posted about this in the last 7 days.'],
  ['creator', 'Creator', 'creator', 'Different accounts that posted about this creator in the last 7 days.'],
  ['hashtag', 'Hashtag', 'hashtag', 'Different accounts that used this hashtag in the last 7 days.'],
  ['sound', 'Sound', 'sound', 'Different accounts that used this sound in the last 7 days.'],
  ['topic', 'Topic', 'topic', 'Different accounts that posted about this topic in the last 7 days.'],
  ['brand', 'Brand', 'brand', 'Different accounts that posted about this brand in the last 7 days.'],
];

for (const [kind, head, noun, help] of KINDS){
  test('the first column, the legend, the help text and the summary follow the Kind filter: ' + (kind || 'all kinds'), async () => {
    window.history.replaceState(null, '', kind ? '#/explore?kind=' + kind : '#/explore');
    const radar = radarWith(['Ayanda', 'Bheki'], {kind: kind || 'hashtag'});
    radar.points[1].kind = kind || 'creator';
    await mount(base(pageWith(['#alpha', '#beta']), radar));
    const section = host.querySelector('[data-section="radar"]');
    expect(section.querySelector('thead th').textContent).toBe(head);
    expect(section.querySelector('[data-strip-caption]').textContent).toStartWith('Each row is a ' + noun + ', sorted by accounts posting in the last 7 days');
    const accounts = section.querySelector('thead th.d42-creators-head');
    expect(accounts.getAttribute('title')).toStartWith(help);
    expect(host.querySelector('#d42-accounts-help').textContent).toStartWith(help);
    expect(accounts.getAttribute('title')).toEndWith('One account counts once, however many times it posted.');
    const tags = [...section.querySelectorAll('tbody .d42-kind-tag')].map((t) => t.textContent);
    if (kind) expect(tags).toEqual([]);
    else expect(tags).toEqual(['Hashtag', 'Creator']);
    const summary = host.querySelector('[data-filter-summary]').textContent;
    expect(summary).toStartWith(kind ? '2 ' + (kind === 'brand' ? 'brands' : kind + 's') + ', South Africa' : '2 results, South Africa');
  });
}

/* Wave 8, round 3: one count of accounts for a window on a row, said the same way in Radar and Trends. */
const pageWithCards = (cards) => ({...pageWith([]), items: cards});
const trendCard = (title, order, extra) => grown({...za.cards[0], item_id: 'tc_' + order, title}, order, extra);
const trendText = (title) => [...host.querySelectorAll('.d42-feed [data-card]')].find((c) => c.querySelector('h3').textContent === title).textContent.replace(/\s+/g, ' ');

test('a Trends row says its accounts once: the stored count line gives way to the measured figure, in the same words as Radar', async () => {
  const card = trendCard('The Joburg Drift Report', 1, {count_line: '31 creators, 3 days', reach: figure(3, 'creators in 3 days'),
    numbers: [figure(3, 'creators in 3 days')], reach7: figure(4, 'creators in 7 days')});
  await mount(base(pageWithCards([card])));
  const row = trendText('The Joburg Drift Report');
  expect(row).toContain('3 accounts posting, last 3 days');
  expect(row).toContain('4 accounts posting, last 7 days, every source');
  expect(row).not.toContain('31');
  expect(row).not.toMatch(/creators? in \d days|\d creators/);
  expect(row.match(/accounts? posting, last 3 days/g)).toHaveLength(1);
});

test('a stored count line that also counts posts keeps the posts and takes the measured accounts', async () => {
  const card = trendCard('Two part line', 1, {count_line: '12 creators and 20 posts in 3 days', reach: figure(3, 'creators in 3 days'),
    numbers: [figure(3, 'creators in 3 days'), figure(20, 'posts in 3 days')]});
  await mount(base(pageWithCards([card])));
  const row = trendText('Two part line');
  expect(row).toContain('3 accounts posting and 20 posts, last 3 days');
  expect(row).not.toContain('12');
});

test('one account reads in the singular, and a card with no reach figure keeps its own line', async () => {
  const one = trendCard('Single account', 1, {count_line: '1 creator, 3 days', reach: figure(1, 'creators in 3 days'), numbers: [figure(1, 'creators in 3 days')]});
  const none = trendCard('No reach', 2, {count_line: '5 creators, 3 days', reach: null, numbers: []});
  await mount(base(pageWithCards([one, none])));
  expect(trendText('Single account')).toContain('1 account posting, last 3 days');
  expect(trendText('Single account')).not.toContain('accounts posting, last 3 days');
  expect(trendText('No reach')).toContain('5 creators, 3 days');
});

test('Sort by sits on the Trends section and orders Trends only; Radar says so and keeps its own order', async () => {
  await mount(base());
  const sort = host.querySelector('select[name="sort"]');
  expect(sort.closest('.d42-feed')).not.toBeNull();
  expect(host.querySelector('[data-section="radar"]').contains(sort)).toBe(false);
  expect(host.querySelector('.d42-groups').contains(sort)).toBe(false);
  const caption = host.querySelector('[data-strip-caption]').textContent;
  expect(caption).toContain('Sort by below orders Trends, not this table.');
  const before = radarCalls().length;
  flushSync(() => { sort.value = 'new'; sort.dispatchEvent(new Event('change', {bubbles: true})); });
  await settle();
  expect(discoverCalls().at(-1)).toContain('sort=new');
  expect(radarCalls()).toHaveLength(before);
});

test('while Radar reads again for a new kind, the dimmed table keeps the words of the rows it still shows', async () => {
  let release;
  await mount([
    ['/api/discover/radar', (url) => (url.includes('kind=creator') ? new Promise((resolve) => { release = resolve; }) : reply(200, radarWith(['Ayanda', 'Bheki'], {kind: 'hashtag'})))],
    ['/api/discover', reply(200, pageWith(['#alpha']))],
  ]);
  const section = () => host.querySelector('[data-section="radar"]');
  expect(section().querySelector('thead th').textContent).toBe('Trend');
  click(tab('Kinds', 'Creators'));
  await settle();
  expect(section().classList.contains('d42-dim')).toBe(true);
  expect(section().querySelector('thead th').textContent).toBe('Trend');
  expect(section().querySelector('[data-strip-caption]').textContent).toStartWith('Each row is a trend');
  release(reply(200, radarWith(['Ayanda'], {kind: 'creator'})));
  await settle();
  expect(section().querySelector('thead th').textContent).toBe('Creator');
  expect(section().querySelector('[data-strip-caption]').textContent).toStartWith('Each row is a creator');
});
