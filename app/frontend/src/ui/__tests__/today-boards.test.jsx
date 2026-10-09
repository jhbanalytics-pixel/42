/* The Today boards section: one card per chart, the counting contract of the
   wave 8 plan (section 4), and the states a chart can be in. Rendered into a
   real DOM so disclosure, focus and the accessible text can be read. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {TodayBoards, chartCounts} = await import('../TodayBoards.jsx');
const {PlatformLogo, logoKey, LOGO_KEYS} = await import('../PlatformLogo.jsx');

let host = null;
let root = null;

beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
});
afterAll(() => {
  GlobalRegistrator.unregister();
});

const show = (props) => flushSync(() => root.render(<TodayBoards {...props} />));
const css = (name) => readFileSync(new URL(`../../styles/${name}`, import.meta.url), 'utf8');

const FAKE_CHANNEL = 'uc' + 'a1b2c3d4e5f6g7h8i9j0k_';
const entry = (rank, title, item_id) => ({rank, title, item_id});
const board = (platform, list, entries, extra = {}) => ({platform, list, entries, left_out: 0, left_out_reason: null, ...extra});

/* The required fixture. ZA: "shared" sits at rank 2 in the Apple Music chart
   and rank 17 in the Spotify chart; rank 5 is tied; "dup" appears twice in
   one chart; one row has a channel id for a title, one a null rank, one no
   item id; one chart is empty. NG carries the same "shared" id. */
const filler = (from, to, prefix) => {
  const rows = [];
  for (let rank = from; rank <= to; rank++) rows.push(entry(rank, `${prefix} ${rank} by Artist ${rank}`, `${prefix}-${rank}`));
  return rows;
};

function zaBoards(){
  return [
    board('tiktok', 'Hashtag board, 7 days', [entry(1, '#fixture_one', 'tt1'), entry(2, '#made by me', 'tt2'), entry(3, '#fixture_three', 'dup'), entry(9, '#fixture_three', 'dup')]),
    board('apple_music', 'Top 100: South Africa', [
      entry(1, 'Opener by Lead Artist', 'am1'),
      entry(2, 'Shared Song by Shared Artist', 'shared'),
      entry(3, 'Third Song by Third Artist', 'am3'),
      entry(5, 'Tie One by Tie Artist', 'tie1'),
      entry(5, 'Tie Two by Other Artist', 'tie2'),
      entry(6, 'Six Song by Six Artist', 'am6'),
      entry(7, 'Seven Song by Seven Artist', 'am7'),
      entry(null, 'No Rank Song by Quiet Artist', 'norank'),
      entry(8, 'Echo by Echo Artist', 'dup'),
      entry(10, 'Untracked Song by Anon Artist', null),
    ]),
    board('spotify', 'Daily top songs', [...filler(1, 16, 'Spot'), entry(17, 'Shared Song by Shared Artist', 'shared'), entry(18, FAKE_CHANNEL, 'hidden-in-spotify')], {left_out: 2, left_out_reason: 'No readable name'}),
    board('app_store', 'Top free apps (iPhone)', [entry(1, 'Fixture App', 'app1')]),
    board('youtube', 'Trending videos, today', []),
  ];
}

const rowsOf = (card) => [...card.querySelectorAll('.tb-row')];
const cardFor = (text) => [...host.querySelectorAll('[data-board-card]')].find((c) => c.querySelector('.tb-card-title').textContent.includes(text));
const title = (row) => row.querySelector('.tb-title').textContent;
const multi = (row) => { const m = row.querySelector('.tb-multi'); return m ? m.textContent : null; };
const expand = (card) => { const b = card.querySelector('button.tb-more'); if (b && b.getAttribute('aria-expanded') === 'false') flushSync(() => b.click()); };
const rowFor = (card, name) => rowsOf(card).find((row) => title(row) === name);

/* ---- grouping and logos ---- */

test('one card per chart, grouped as music charts, apps and social boards in a fixed order', () => {
  show({boards: zaBoards()});
  const groups = [...host.querySelectorAll('[data-board-group]')];
  expect(groups.map((g) => g.getAttribute('data-board-group'))).toEqual(['music', 'apps', 'social']);
  expect(groups.map((g) => g.querySelector('.tb-group-title').textContent.replace(/\d+$/, '').trim())).toEqual(['Music charts', 'Apps', 'Social boards']);
  const names = (g) => [...g.querySelectorAll('[data-board-card]')].map((c) => c.querySelector('.tb-card-title').textContent);
  expect(names(groups[0])).toEqual(['Apple Music Top 100: South Africa', 'Spotify Daily top songs']);
  expect(names(groups[1])).toEqual(['App Store Top free apps (iPhone)']);
  expect(names(groups[2])).toEqual(['TikTok Hashtag board, 7 days', 'YouTube Trending videos, today']);
  expect(host.querySelectorAll('[data-board-card]')).toHaveLength(5);
});

test('every chart header carries a decorative logo beside its text label', () => {
  show({boards: zaBoards()});
  for (const card of host.querySelectorAll('[data-board-card]')){
    const logo = card.querySelector('.tb-card-head .pl-logo');
    expect(logo).not.toBeNull();
    expect(logo.getAttribute('aria-hidden')).toBe('true');
    expect(logo.getAttribute('focusable')).toBe('false');
    expect(logo.getAttribute('role')).toBeNull();
    expect(logo.querySelector('title')).toBeNull();
    expect(card.querySelector('.tb-platform').textContent.trim().length).toBeGreaterThan(0);
  }
  expect(host.querySelectorAll('img')).toHaveLength(0);
});

test('the named platforms each have a vendored mark and an unknown one falls back to a text monogram', () => {
  for (const platform of ['apple_music', 'spotify', 'shazam', 'app_store', 'google_play', 'tiktok', 'youtube', 'google', 'reddit']) expect(LOGO_KEYS).toContain(platform);
  /* simple-icons has no Boomplay or Nairaland mark, so they stay text monograms. */
  for (const platform of ['boomplay', 'nairaland']){
    expect(LOGO_KEYS).not.toContain(platform);
    expect(logoKey(platform)).toBeNull();
  }
  flushSync(() => root.render(<PlatformLogo platform="boomplay" />));
  expect(host.querySelector('.pl-monogram').textContent).toBe('B');
  expect(host.querySelector('svg')).toBeNull();
  expect(logoKey('twitter')).toBeNull();
  expect(logoKey('app_store_iphone')).toBe('app_store');
  expect(logoKey('kworb_spotify')).toBe('spotify');
  expect(logoKey('Apple Music')).toBe('apple_music');
  expect(logoKey('some_feed')).toBeNull();
  flushSync(() => root.render(<PlatformLogo platform="some_feed" />));
  const mono = host.querySelector('.pl-monogram');
  expect(mono.getAttribute('aria-hidden')).toBe('true');
  expect(mono.textContent).toBe('S');
  expect(host.querySelector('svg')).toBeNull();
  flushSync(() => root.render(<PlatformLogo platform="spotify" />));
  expect(host.querySelector('svg.pl-logo path')).not.toBeNull();
  expect(host.querySelector('svg.pl-logo').getAttribute('aria-hidden')).toBe('true');
  flushSync(() => root.render(<PlatformLogo platform={undefined} />));
  expect(host.querySelector('.pl-monogram').textContent).toBe('?');
});

/* The official marks are vendored from simple-icons 13.21.0 (CC0). The expected
   digests were computed from that package's own files, so they pin the path
   data to the source and are not read from the component under test. */
const PINNED = {
  apple_music: '768eac82877086f3',
  spotify: 'f8560b9a4343d3ef',
  shazam: '24b37391f3ddff43',
  app_store: 'c94c142369043de8',
  google_play: 'f0fb1eb0282f2499',
  tiktok: '4ad895b5783ca940',
  youtube: '8142fbc0e697bc1b',
  google: '1418294b312c0653',
  reddit: '56447b3636ee5a28',
};

test('each official mark is the pinned simple-icons path, one colour, decorative, with its source named', () => {
  expect([...LOGO_KEYS].sort()).toEqual(Object.keys(PINNED).sort());
  for (const [key, digest] of Object.entries(PINNED)){
    flushSync(() => root.render(<PlatformLogo platform={key} />));
    const svg = host.querySelector('svg.pl-logo');
    expect(svg.getAttribute('viewBox')).toBe('0 0 24 24');
    expect(svg.getAttribute('aria-hidden')).toBe('true');
    const paths = svg.querySelectorAll('path');
    expect(paths).toHaveLength(1);
    expect(svg.querySelectorAll('*')).toHaveLength(1);
    expect(paths[0].getAttribute('fill')).toBeNull();
    expect(paths[0].getAttribute('stroke')).toBeNull();
    expect(createHash('sha256').update(paths[0].getAttribute('d')).digest('hex').slice(0, 16), key).toBe(digest);
  }
  const source = readFileSync(new URL('../PlatformLogo.jsx', import.meta.url), 'utf8');
  expect(source).toContain('simple-icons@13.21.0');
  expect(source).toContain('CC0');
  expect(source).toContain('trademarks of their owners');
  const logoCss = css('platform-logo.css');
  expect(logoCss).toMatch(/\.pl-logo\s*\{[^}]*fill:\s*currentColor/);
  expect(logoCss).not.toMatch(/stroke:\s*currentColor/);
});

test('a logo never loads anything from the network and the stylesheet uses theme tokens only', () => {
  show({boards: zaBoards()});
  expect(host.innerHTML).not.toMatch(/\b(?:href|src)=|<image|<img|xlink:href|url\(/i);
  for (const file of ['today-boards.css', 'platform-logo.css']){
    const text = css(file);
    expect(text).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(text).not.toMatch(/\b(?:rgb|rgba|hsl|hsla|oklch)\(/i);
    expect(text).toMatch(/var\(--/);
  }
});

/* ---- rows: ranks, ties, artists ---- */

test('ranks stay on their own chart, ties show =, and a null rank is called unranked', () => {
  show({boards: zaBoards()});
  const card = cardFor('Apple Music');
  expand(card);
  const badge = (name) => rowFor(card, name).querySelector('.tb-rank').textContent;
  expect(badge('Opener')).toBe('1');
  expect(badge('Tie One')).toBe('=5');
  expect(badge('Tie Two')).toBe('=5');
  expect(badge('Six Song')).toBe('6');
  const unranked = rowFor(card, 'No Rank Song');
  expect(unranked.querySelector('.tb-rank').textContent).toBe('-');
  expect(unranked.querySelector('.sr-only').textContent).toMatch(/Unranked/);
  expect(rowFor(card, 'Tie One').querySelector('.sr-only').textContent).toMatch(/Tied rank 5/);
  expect(rowFor(card, 'Opener').querySelector('.sr-only').textContent).toMatch(/Rank 1/);
  expect(host.textContent).not.toMatch(/overall|combined rank|across platforms rank/i);
});

test('rows keep source order and a stable identity of platform, list, rank and item id or title', () => {
  show({boards: zaBoards()});
  const card = cardFor('Apple Music');
  expand(card);
  const ids = rowsOf(card).map((row) => row.getAttribute('data-row-id'));
  expect(ids).toEqual([
    'apple_music|Top 100: South Africa|1|am1',
    'apple_music|Top 100: South Africa|2|shared',
    'apple_music|Top 100: South Africa|3|am3',
    'apple_music|Top 100: South Africa|5|tie1',
    'apple_music|Top 100: South Africa|5|tie2',
    'apple_music|Top 100: South Africa|6|am6',
    'apple_music|Top 100: South Africa|7|am7',
    'apple_music|Top 100: South Africa||norank',
    'apple_music|Top 100: South Africa|8|dup',
    'apple_music|Top 100: South Africa|10|Untracked Song by Anon Artist',
  ]);
});

test('a music title that names its artist splits onto two lines; other boards keep the whole title', () => {
  show({boards: zaBoards()});
  const apple = cardFor('Apple Music');
  const opener = rowFor(apple, 'Opener');
  expect(opener.querySelector('.tb-artists').textContent).toBe('Lead Artist');
  const social = cardFor('TikTok');
  const hashtag = rowsOf(social)[1];
  expect(title(hashtag)).toBe('#made by me');
  expect(hashtag.querySelector('.tb-artists')).toBeNull();
});

test('source line breaks read as the song then the artist on a music chart', () => {
  show({boards: [board('apple_music', 'Top 100', [entry(19, '<br>Gratitude<br>Asake', 'a'), entry(22, 'IMALI<br/>Fireboy DML, JAZZWRLD & Thukuthela', 'b'), entry(24, '<br><br>', 'c')], {})]});
  const rows = rowsOf(cardFor('Apple Music'));
  expect(rows.map(title)).toEqual(['Gratitude', 'IMALI']);
  expect(rows.map((r) => r.querySelector('.tb-artists').textContent)).toEqual(['Asake', 'Fireboy DML, JAZZWRLD & Thukuthela']);
  expect(host.textContent).not.toContain('<br');
  expect(cardFor('Apple Music').querySelector('.tb-left-out').textContent).toBe('1 left out: No readable name');
});

test('an unreadable title is left out and counted, with the reason kept', () => {
  show({boards: zaBoards()});
  const card = cardFor('Spotify');
  expect(host.textContent).not.toMatch(/uc[a-z0-9_-]{22}/i);
  expect(card.querySelector('.tb-left-out').textContent).toBe('3 left out: No readable name');
  expect(card.querySelector('.tb-left-out').closest('[data-board-card]')).toBe(card);
});

/* ---- top five and the disclosure ---- */

test('a long chart shows five rows and a named, keyboard-reachable show all control', () => {
  show({boards: zaBoards()});
  const card = cardFor('Spotify');
  expect(rowsOf(card)).toHaveLength(5);
  const button = card.querySelector('button.tb-more');
  expect(button.tagName).toBe('BUTTON');
  expect(button.getAttribute('type')).toBe('button');
  expect(button.getAttribute('tabindex')).not.toBe('-1');
  expect(button.textContent).toBe('Show all 17');
  expect(button.getAttribute('aria-expanded')).toBe('false');
  expect(button.getAttribute('aria-label')).toMatch(/^Show all 17 .*Spotify/);
  const controlled = document.getElementById(button.getAttribute('aria-controls'));
  expect(controlled).not.toBeNull();
  expect(card.contains(controlled)).toBe(true);
  button.focus();
  expect(document.activeElement).toBe(button);
  flushSync(() => button.click());
  expect(rowsOf(card)).toHaveLength(17);
  expect(button.getAttribute('aria-expanded')).toBe('true');
  expect(button.textContent).toBe('Show fewer');
  expect(button.getAttribute('aria-label')).toMatch(/^Show fewer .*Spotify/);
  expect(document.activeElement).toBe(button);
  flushSync(() => button.click());
  expect(rowsOf(card)).toHaveLength(5);
  expect(button.getAttribute('aria-expanded')).toBe('false');
});

test('a chart of five rows or fewer has no disclosure and one with six counts only readable rows', () => {
  show({boards: [board('spotify', 'Daily', [...filler(1, 5, 'A')]), board('shazam', 'Top', [...filler(1, 5, 'B'), entry(6, FAKE_CHANNEL, 'x')])]});
  expect(host.querySelectorAll('button.tb-more')).toHaveLength(0);
  expect(host.querySelectorAll('.tb-row')).toHaveLength(10);
});

/* ---- the counting contract ---- */

test('one item at ranks 2 and 17 in two charts is marked on both as on 2 charts', () => {
  show({boards: zaBoards()});
  const apple = cardFor('Apple Music');
  const spotify = cardFor('Spotify');
  expand(spotify);
  const inApple = rowFor(apple, 'Shared Song');
  const inSpotify = rowFor(cardFor('Spotify'), 'Shared Song');
  expect(multi(inApple)).toBe('On 2 charts');
  expect(multi(inSpotify)).toBe('On 2 charts');
  expect(inApple.querySelector('.tb-rank').textContent).toBe('2');
  expect(inSpotify.querySelector('.tb-rank').textContent).toBe('17');
  expect(inApple.querySelector('.tb-also').textContent).toContain('Spotify');
  expect(inApple.querySelector('.tb-also').textContent).not.toContain('Apple Music');
});

test('a duplicate inside one chart is deduplicated before counting', () => {
  const boards = zaBoards();
  const counts = chartCounts(boards);
  expect(counts.get('dup')).toBe(2);
  expect(counts.get('shared')).toBe(2);
  expect(counts.get('tt1')).toBe(1);
  show({boards});
  const tiktok = cardFor('TikTok');
  expect(rowsOf(tiktok).filter((r) => r.getAttribute('data-row-id').endsWith('|dup'))).toHaveLength(2);
  show({boards: [board('tiktok', 'Only', [entry(1, '#a', 'solo'), entry(2, '#a', 'solo'), entry(3, '#a', 'solo')])]});
  expect(host.querySelectorAll('.tb-multi')).toHaveLength(0);
});

test('the same chart named twice in a payload counts once', () => {
  const twice = [board('tiktok', 'Same', [entry(1, '#a', 'x')]), board('tiktok', 'Same', [entry(1, '#a', 'x')])];
  expect(chartCounts(twice).get('x')).toBe(1);
});

test('the count is taken before any filter, so an unreadable or unranked sighting still counts', () => {
  const counts = chartCounts(zaBoards());
  expect(counts.get('hidden-in-spotify')).toBe(1);
  const boards = [
    board('apple_music', 'A', [entry(1, 'Song by Someone', 'k')]),
    board('spotify', 'B', [entry(null, 'Song by Someone', 'k')]),
    board('shazam', 'C', [entry(3, FAKE_CHANNEL, 'k')]),
  ];
  expect(chartCounts(boards).get('k')).toBe(3);
  show({boards});
  expect(multi(rowsOf(cardFor('Apple Music'))[0])).toBe('On 3 charts');
  expect(multi(rowsOf(cardFor('Spotify'))[0])).toBe('On 3 charts');
});

test('a chart is identified by platform and list together: two lists on one platform are two charts', () => {
  const sameShop = [
    board('apple_music', 'Top 100: South Africa', [entry(4, 'Both by Someone', 'both')]),
    board('apple_music', 'Top 100: Nigeria', [entry(9, 'Both by Someone', 'both')]),
  ];
  expect(chartCounts(sameShop).get('both')).toBe(2);
  const sameName = [
    board('spotify', 'Daily top', [entry(1, 'Both by Someone', 'both')]),
    board('shazam', 'Daily top', [entry(1, 'Both by Someone', 'both')]),
  ];
  expect(chartCounts(sameName).get('both')).toBe(2);
  const one = [board('spotify', 'Daily top', [entry(1, 'Both by Someone', 'both')]), board('spotify', ' Daily top ', [entry(2, 'Both by Someone', 'both')])];
  expect(chartCounts(one).get('both')).toBe(1);
  show({boards: sameShop});
  for (const card of host.querySelectorAll('[data-board-card]')) expect(multi(rowsOf(card)[0])).toBe('On 2 charts');
  expect(host.querySelector('[data-board-card] .tb-also').textContent).toMatch(/Also on Apple Music Top 100: (Nigeria|South Africa)/);
  show({boards: sameName});
  for (const card of host.querySelectorAll('[data-board-card]')) expect(multi(rowsOf(card)[0])).toBe('On 2 charts');
});

test('a rank of zero, below zero, text or NaN is unranked, never shown as a rank', () => {
  show({boards: [board('apple_music', 'Odd ranks', [
    entry(0, 'Zero by Artist', 'z'),
    entry(-3, 'Negative by Artist', 'n'),
    entry(NaN, 'Nan by Artist', 'x'),
    entry('4', 'Text by Artist', 't'),
    entry(1, 'Real by Artist', 'r'),
  ])]});
  const rows = rowsOf(cardFor('Apple Music'));
  expect(rows.map((row) => row.querySelector('.tb-rank').textContent)).toEqual(['-', '-', '-', '-', '1']);
  expect(rows.slice(0, 4).every((row) => /Unranked/.test(row.querySelector('.sr-only').textContent))).toBe(true);
  expect(rows[4].querySelector('.sr-only').textContent).toMatch(/Rank 1\./);
  expect(rows.map((row) => row.getAttribute('data-row-id').split('|')[2])).toEqual(['', '', '', '', '1']);
});

test('an item without an item id never gets a marker, even when its title repeats', () => {
  const boards = [
    board('apple_music', 'A', [entry(1, 'Same Title by Same Artist', null)]),
    board('spotify', 'B', [entry(1, 'Same Title by Same Artist', undefined), entry(2, 'Same Title by Same Artist', '')]),
  ];
  const counts = chartCounts(boards);
  expect(counts.size).toBe(0);
  show({boards});
  expect(host.querySelectorAll('.tb-multi')).toHaveLength(0);
  const bad = [board('apple_music', 'A', [entry(1, 'T by A', 42)]), board('spotify', 'B', [entry(1, 'T by A', 42)])];
  expect(chartCounts(bad).size).toBe(0);
});

test('the same item id in another market is separate: counted per market, never merged', () => {
  const za = zaBoards();
  const ng = [board('apple_music', 'Top 100: Nigeria', [entry(4, 'Shared Song by Shared Artist', 'shared')])];
  expect(chartCounts(za).get('shared')).toBe(2);
  expect(chartCounts(ng).get('shared')).toBe(1);
  show({groups: [{market: 'ZA', label: 'South Africa', boards: za}, {market: 'NG', label: 'Nigeria', boards: ng}]});
  const nigeria = host.querySelector('[data-board-market="NG"]');
  expect(nigeria.querySelectorAll('.tb-multi')).toHaveLength(0);
  const southAfrica = host.querySelector('[data-board-market="ZA"]');
  const apple = [...southAfrica.querySelectorAll('[data-board-card]')].find((c) => c.textContent.includes('Apple Music'));
  expect(multi(rowFor(apple, 'Shared Song'))).toBe('On 2 charts');
});

/* ---- All: charts grouped by market ---- */

test('All groups charts by market, each with a badge, in the order given', () => {
  show({groups: [
    {market: 'ZA', label: 'South Africa', boards: [board('tiktok', 'Hashtag board', [entry(1, '#za', 'z')])]},
    {market: 'NG', label: 'Nigeria', boards: [board('youtube', 'Trending', [entry(1, '#ng', 'n')])]},
    {market: 'KE', label: 'Kenya', boards: []},
  ]});
  const markets = [...host.querySelectorAll('[data-board-market]')];
  expect(markets.map((m) => m.getAttribute('data-board-market'))).toEqual(['ZA', 'NG', 'KE']);
  expect(markets.map((m) => m.querySelector('.tb-market-badge').textContent)).toEqual(['ZA', 'NG', 'KE']);
  expect(markets[0].querySelector('.tb-market-name').textContent).toBe('South Africa');
  expect(markets[0].getAttribute('role')).toBe('group');
  expect(markets[0].getAttribute('aria-labelledby')).toBe(markets[0].querySelector('.tb-market-name').id);
  expect(markets[2].textContent).toContain('No platform lists were read today.');
  expect(host.querySelectorAll('[data-section="boards"]')).toHaveLength(1);
});

/* ---- the brief day ---- */

const PAST = 'on 30 September 2026';
const pastBoards = () => [
  board('youtube', 'Empty list', []),
  {platform: 'tiktok', list: 'Missing list', left_out: 0, left_out_reason: null},
  board('apple_music', 'Unreadable list', [entry(1, FAKE_CHANNEL, 'i1')], {left_out: 1, left_out_reason: 'No readable name'}),
  board('spotify', 'Daily top songs', [entry(1, 'Song by Someone', 's1')]),
];

test('a past brief words every boards line with its own day and never says today', () => {
  show({boards: pastBoards(), day: PAST});
  const section = host.querySelector('[data-section="boards"]');
  expect(section.querySelector('h3.t42-section-title').textContent).toBe('On the boards on 30 September 2026');
  expect(section.querySelector('.tb-caption').textContent).toBe('Best rank on 30 September 2026');
  expect(cardFor('Empty list').textContent).toContain('Nothing was on this list on 30 September 2026.');
  expect(cardFor('Missing list').textContent).toContain('This list did not come through on 30 September 2026.');
  expect(section.querySelector('.tb-note').textContent).toContain('same market on 30 September 2026');
  expect(section.textContent).not.toMatch(/\btoday\b/i);
  show({boards: [], day: PAST});
  expect(host.querySelector('[data-section="boards"]').textContent).toContain('No platform lists were read on 30 September 2026.');
  expect(host.querySelector('[data-section="boards"]').textContent).not.toMatch(/\btoday\b/i);
});

test('with no day given the boards say today, and the note says what "On N charts" counts', () => {
  show({boards: pastBoards()});
  const section = host.querySelector('[data-section="boards"]');
  expect(section.querySelector('h3.t42-section-title').textContent).toBe('On the boards today');
  expect(section.querySelector('.tb-caption').textContent).toBe('Best rank today');
  expect(section.querySelector('.tb-note').textContent).toBe('Each card is one platform\'s own list, and a rank belongs to that list only. "On N charts" counts the lists in the same market today.');
  show({boards: pastBoards(), day: ''});
  expect(host.querySelector('[data-section="boards"] h3').textContent).toBe('On the boards today');
});

test('the server reason is said once on a past brief and worded by the brief day', () => {
  /* The server's shape for a chart whose every entry is an id: no entries, a count and the reason. */
  show({boards: [board('youtube', 'YouTube trending board', [], {left_out: 3, left_out_reason: SERVER_REASON})], day: PAST});
  const card = host.querySelector('[data-board-card]');
  expect(card.getAttribute('data-chart-state')).toBe('unreadable');
  expect(occurrences(card.textContent, 'readable name')).toBe(1);
  expect(card.querySelector('.tb-left-out').textContent).toBe("3 left out: None of this list's entries had a readable name on 30 September 2026");
  expect(card.textContent).not.toMatch(/today/i);
  show({boards: [board('youtube', 'YouTube trending board', [], {left_out: 3, left_out_reason: SERVER_REASON})]});
  expect(host.querySelector('.tb-left-out').textContent).toBe('3 left out: ' + SERVER_REASON);
});

test('a reason that is not the state sentence is kept as the server sent it, on any day', () => {
  show({boards: [board('spotify', 'Daily', [entry(1, 'Song by Someone', 's'), entry(2, FAKE_CHANNEL, 'h')], {left_out: 1, left_out_reason: 'Some other reason'}), board('youtube', 'Plain', [entry(1, 'A', 'a'), entry(2, FAKE_CHANNEL, 'b')], {left_out: 0, left_out_reason: null})], day: PAST});
  expect(cardFor('Spotify').querySelector('.tb-left-out').textContent).toBe('2 left out: Some other reason');
  expect(cardFor('YouTube').querySelector('.tb-left-out').textContent).toBe('1 left out: No readable name');
});

/* ---- All: every market from one payload ---- */

test('All renders each market of a Today payload under one boards section, apart and counted apart', () => {
  const markets = ['ZA', 'NG', 'KE'].map((market, at) => ({
    market, label: ['South Africa', 'Nigeria', 'Kenya'][at], status: 'ok', cards: [],
    boards: at === 2 ? undefined : [
      board('apple_music', 'Top 100: ' + market, [entry(1, 'Shared Song by Shared Artist', 'shared')]),
      board('spotify', 'Daily top songs', [entry(2, 'Shared Song by Shared Artist', 'shared')]),
    ],
  }));
  const groups = markets.map((m) => ({market: m.market, label: m.label, boards: m.boards}));
  show({groups, day: PAST});
  expect(host.querySelectorAll('[data-section="boards"]')).toHaveLength(1);
  const blocks = [...host.querySelectorAll('[data-board-market]')];
  expect(blocks.map((b) => b.getAttribute('data-board-market'))).toEqual(['ZA', 'NG', 'KE']);
  expect(blocks.map((b) => b.querySelector('.tb-market-badge').textContent)).toEqual(['ZA', 'NG', 'KE']);
  for (const block of blocks.slice(0, 2)){
    expect(block.querySelectorAll('[data-board-card]')).toHaveLength(2);
    expect([...block.querySelectorAll('.tb-multi')].map((m) => m.textContent)).toEqual(['On 2 charts', 'On 2 charts']);
  }
  expect(blocks[2].textContent).toContain('No platform lists were read on 30 September 2026.');
  const levels = (selector) => [...new Set([...host.querySelectorAll(selector)].map((el) => el.tagName))];
  expect(levels('.tb-market-head')).toEqual(['H4']);
  expect(levels('.tb-group-title')).toEqual(['H5']);
  expect(levels('.tb-card-title')).toEqual(['H6']);
  expect(host.querySelector('.tb-note').textContent).toContain('same market on 30 September 2026');
  const captions = [...host.querySelectorAll('.tb-caption')].map((c) => c.textContent);
  expect(captions).toHaveLength(4);
  expect(new Set(captions)).toEqual(new Set(['Best rank on 30 September 2026']));
  const ids = [...host.querySelectorAll('[id]')].map((el) => el.id);
  expect(new Set(ids).size).toBe(ids.length);
  expect(host.textContent).not.toMatch(/\btoday\b/i);
});

test('All with every market empty says so under each badge, and groups win over boards', () => {
  show({groups: [{market: 'ZA', label: 'South Africa', boards: []}, {market: 'NG', label: 'Nigeria'}], boards: [board('spotify', 'Ignored', [entry(1, 'A by B', 'q')])]});
  expect(host.querySelectorAll('[data-board-market]')).toHaveLength(2);
  expect(host.querySelectorAll('[data-board-card]')).toHaveLength(0);
  expect(host.textContent).not.toContain('Ignored');
  expect(host.querySelector('.tb-note')).toBeNull();
  expect(host.querySelectorAll('.t42-line-text')).toHaveLength(2);
});

/* ---- states ---- */

test('empty, missing and unreadable charts each get their own visible state', () => {
  show({boards: [
    board('youtube', 'Empty list', []),
    {platform: 'tiktok', list: 'Missing list', left_out: 0, left_out_reason: null},
    board('apple_music', 'Unreadable list', [entry(1, FAKE_CHANNEL, 'i1'), entry(2, '<br>', 'i2')], {left_out: 1, left_out_reason: 'No readable name'}),
    null,
    'not a chart',
  ]});
  const state = (text) => cardFor(text).getAttribute('data-chart-state');
  expect(state('Empty list')).toBe('empty');
  expect(state('Missing list')).toBe('missing');
  expect(state('Unreadable list')).toBe('unreadable');
  expect(cardFor('Empty list').textContent).toContain('Nothing was on this list today.');
  expect(cardFor('Missing list').textContent).toContain('This list did not come through today.');
  expect(cardFor('Unreadable list').textContent).toContain("None of this list's entries had a readable name today.");
  expect(cardFor('Unreadable list').querySelector('.tb-left-out').textContent).toBe('3 left out: No readable name');
  expect(host.querySelectorAll('[data-chart-state="invalid"]')).toHaveLength(2);
  expect(host.textContent).toContain('This list could not be read.');
  for (const card of host.querySelectorAll('[data-board-card]')) expect(card.querySelector('ul.tb-rows')).toBeNull();
});

test('a chart whose only entries are ids shows only its count and reason', () => {
  show({boards: [board('youtube', 'YouTube trending board', [], {left_out: 10, left_out_reason: 'No readable name'})]});
  const card = host.querySelector('[data-board-card]');
  expect(card.getAttribute('data-chart-state')).toBe('unreadable');
  expect(card.querySelector('.tb-left-out').textContent).toBe('10 left out: No readable name');
  expect(card.querySelector('ul.tb-rows')).toBeNull();
});

/* core/api/today.py NO_NAMES_READ, as the server sends it for a chart whose entries are all ids. */
const SERVER_REASON = "None of this list's entries had a readable name today";
const occurrences = (text, needle) => text.split(needle).length - 1;

test('an unreadable chart says the server reason once, with the count beside it', () => {
  show({boards: [board('youtube', 'YouTube trending board', [entry(1, FAKE_CHANNEL, 'i1'), entry(2, '<br>', 'i2')], {left_out: 0, left_out_reason: SERVER_REASON})]});
  const card = host.querySelector('[data-board-card]');
  expect(card.getAttribute('data-chart-state')).toBe('unreadable');
  expect(occurrences(card.textContent, 'readable name')).toBe(1);
  expect(occurrences(card.textContent, SERVER_REASON)).toBe(1);
  expect(card.querySelector('.tb-left-out').textContent).toBe('2 left out: ' + SERVER_REASON);
  /* Same sentence with the state line's own full stop still counts as the same sentence. */
  show({boards: [board('youtube', 'YouTube trending board', [entry(1, FAKE_CHANNEL, 'i1')], {left_out_reason: SERVER_REASON + '.'})]});
  expect(occurrences(host.querySelector('[data-board-card]').textContent, 'readable name')).toBe(1);
});

test('no boards at all says so, and a payload that is not a list does too', () => {
  show({boards: []});
  expect(host.querySelector('[data-section="boards"]').textContent).toContain('No platform lists were read today.');
  show({boards: undefined});
  expect(host.querySelector('[data-section="boards"]').textContent).toContain('No platform lists were read today.');
  show({boards: {not: 'a list'}});
  expect(host.querySelector('[data-section="boards"] ul')).toBeNull();
  expect(host.querySelector('[data-section="boards"]').textContent).toContain('No platform lists were read today.');
});

/* ---- accessibility ---- */

test('every card is a named region and every row announces its rank and its chart', () => {
  show({boards: zaBoards()});
  for (const card of host.querySelectorAll('[data-board-card]')){
    const heading = card.querySelector('.tb-card-title');
    expect(heading.tagName).toBe('H5');
    expect(card.getAttribute('aria-labelledby')).toBe(heading.id);
    expect(card.tagName).toBe('SECTION');
  }
  const spotify = cardFor('Spotify');
  const spoken = rowsOf(spotify)[0].textContent;
  expect(spoken).toMatch(/Rank 1/);
  expect(spoken).toMatch(/Spotify/);
  expect(spoken).toMatch(/Daily top songs/);
  const section = host.querySelector('[data-section="boards"]');
  expect(section.querySelector('h3.t42-section-title').textContent).toBe('On the boards today');
  const list = spotify.querySelector('ul.tb-rows');
  expect(list.tagName).toBe('UL');
  expect(spotify.querySelector('ol')).toBeNull();
});

test('ids are unique across cards, groups and markets', () => {
  show({groups: [{market: 'ZA', label: 'South Africa', boards: zaBoards()}, {market: 'NG', label: 'Nigeria', boards: zaBoards()}]});
  const ids = [...host.querySelectorAll('[id]')].map((el) => el.id);
  expect(new Set(ids).size).toBe(ids.length);
});

test('the stylesheet gives the disclosure a visible focus ring, wraps long text and reflows at phone width', () => {
  const text = css('today-boards.css');
  expect(text).toMatch(/\.tb-more:focus-visible\s*\{[^}]*outline:/);
  expect(text).toMatch(/\.tb-row[\s\S]*?overflow-wrap:\s*anywhere/);
  expect(text).toMatch(/\.tb-grid\s*\{[^}]*grid-template-columns:\s*repeat\(auto-fill,\s*minmax\(min\(100%,/);
  expect(text).toMatch(/\.tb-more\s*\{[^}]*min-block-size:\s*var\(--target-min\)/);
  expect(text).not.toMatch(/(?<![-\w])(?:width|min-width):\s*\d{3,}px/);
  expect(text).not.toMatch(/font-size:\s*\d+px/);
  expect(text).not.toMatch(new RegExp('[' + String.fromCharCode(0x2013, 0x2014) + ']'));
});

/* The platform word, said once (N5) */

/* The server names every list in its own words and those words start with the
   platform ("Spotify daily chart", "Apple Music chart", "YouTube trending board"). */
const serverBoards = () => [
  board('spotify', 'Spotify daily chart', [entry(1, 'Shared Song by Shared Artist', 'shared'), entry(2, 'Only Here by Someone', 'only')]),
  board('apple_music', 'Apple Music chart', [entry(1, 'Shared Song by Shared Artist', 'shared')]),
  board('youtube', 'YouTube trending board', [entry(1, 'A video', 'v1')]),
  board('tiktok', 'Culture accounts we follow', [entry(1, '#fixture', 't1')]),
];

test('a list the server names with its platform is not said with the platform twice', () => {
  show({boards: serverBoards()});
  const heads = [...host.querySelectorAll('.tb-card-title')].map((node) => node.textContent);
  expect(heads).toEqual(['Spotify daily chart', 'Apple Music chart', 'YouTube trending board', 'TikTok Culture accounts we follow']);
  for (const card of host.querySelectorAll('[data-board-card]')) expect(card.textContent).not.toMatch(/(Spotify|Apple Music|YouTube) \1/);
});

test('the hidden chart name on each row and the Also on names say the platform once', () => {
  show({boards: serverBoards()});
  const spotifyRow = rowsOf(cardFor('Spotify'))[0];
  expect(spotifyRow.querySelector('.sr-only:last-child').textContent).toBe('. Spotify daily chart');
  const text = (node) => node.textContent.replace(/\s+/g, ' ');
  expect(text(spotifyRow)).not.toMatch(/Spotify Spotify/);
  expect(spotifyRow.querySelector('.tb-also').textContent).toBe('Also on Apple Music chart');
  const appleRow = rowsOf(cardFor('Apple Music'))[0];
  expect(appleRow.querySelector('.tb-also').textContent).toBe('Also on Spotify daily chart');
});

test('a list name that only begins with the same letters as the platform keeps the platform word', () => {
  show({boards: [board('x', 'Xylophone trends', [entry(1, 'a', 'a')])]});
  expect(host.querySelector('.tb-card-title').textContent).toBe('X Xylophone trends');
});

/* The Also on names are text on the page (7b-6) */

test('the Also on names are visible text beside the count, not only a hover title', () => {
  show({boards: serverBoards()});
  const row = rowsOf(cardFor('Spotify'))[0];
  const also = row.querySelector('.tb-also');
  expect(also.textContent).toBe('Also on Apple Music chart');
  expect(also.hasAttribute('hidden')).toBe(false);
  expect(host.querySelector('.tb-multi').hasAttribute('title')).toBe(false);
  expect(also.closest('[aria-hidden="true"]')).toBeNull();
  expect(rowsOf(cardFor('Spotify'))[1].querySelector('.tb-also')).toBeNull();
  expect(host.querySelector('[data-board-card] .tb-also')).not.toBeNull();
});

test('a row on three charts names both of the other two', () => {
  show({boards: [
    board('spotify', 'Daily top songs', [entry(1, 'Hit by Star', 'hit')]),
    board('apple_music', 'Top 100: South Africa', [entry(3, 'Hit by Star', 'hit')]),
    board('shazam', 'Shazam national chart', [entry(9, 'Hit by Star', 'hit')]),
  ]});
  const row = rowsOf(cardFor('Spotify'))[0];
  expect(row.querySelector('.tb-multi').textContent).toBe('On 3 charts');
  expect(row.querySelector('.tb-also').textContent).toBe('Also on Apple Music Top 100: South Africa; Shazam national chart');
});

/* The All view hands every card the brief day (N3, mutant B14) */

test('every card caption in All reads the brief day, with nothing missing after it', () => {
  show({groups: [
    {market: 'ZA', label: 'South Africa', boards: [board('spotify', 'Spotify daily chart', [entry(1, 'A by B', 'a')])]},
    {market: 'NG', label: 'Nigeria', boards: [board('youtube', 'YouTube trending board', [entry(1, 'V', 'v')])]},
  ], day: PAST});
  const captions = [...host.querySelectorAll('.tb-caption')].map((c) => c.textContent);
  expect(captions).toEqual(['Best rank on 30 September 2026', 'Best rank on 30 September 2026']);
});

test('the Also on line is shown by the stylesheet, not hidden or clipped away', () => {
  const sheet = css('today-boards.css');
  const rule = sheet.match(/\.tb-also\s*\{([^}]*)\}/);
  expect(rule).not.toBeNull();
  expect(rule[1]).not.toMatch(/display\s*:\s*none|visibility\s*:\s*hidden|clip|position\s*:\s*absolute|opacity\s*:\s*0|font-size\s*:\s*0|height\s*:\s*0|overflow\s*:\s*hidden/);
  expect(sheet).not.toMatch(/\.tb-also[^{]*\{[^}]*display\s*:\s*none/);
});
