/* Visual QA, 5 October 2026: reader words for units, board titles and the
   record ids a gap's search can name. */
import {expect, test} from 'bun:test';
import {boardTitle, plainSearched, plainUnit, unitFor} from '../../readerUnits.js';

test('a count of one takes the singular of its leading noun only', () => {
  expect(unitFor(1, 'posts first seen in 7 days')).toBe('post first seen in 7 days');
  expect(unitFor(1, 'posts a day')).toBe('post a day');
  expect(unitFor(2, 'posts a day')).toBe('posts a day');
  expect(unitFor(0, 'posts')).toBe('posts');
  expect(unitFor(1, 'Creators')).toBe('Creator');
  expect(unitFor(1, 'share of posts')).toBe('share of posts');
  expect(unitFor(1, '')).toBe('');
});

test('a warehouse unit reads as words', () => {
  expect(plainUnit('located_posts')).toBe('posts with a known location');
  expect(plainUnit('located_creators')).toBe('creators with a known location');
  expect(plainUnit('reach_per_post')).toBe('reach per post');
  expect(plainUnit('posts tagged #fixture_za_step')).toBe('posts tagged #fixture_za_step');
});

test('a board title loses its <br> tags and keeps its parts', () => {
  expect(boardTitle('<br>Gratitude<br>Asake')).toBe('Gratitude · Asake');
  expect(boardTitle('TEA<BR />Rema')).toBe('TEA · Rema');
  expect(boardTitle('#amapiano')).toBe('#amapiano');
  expect(boardTitle('<br><br>')).toBe('');
});

test('record ids in a gap search become a count', () => {
  expect(plainSearched('cited posts obs1_7bfd7de8e71252031cc34ad053aee005, obs1_66faeb419bbd118deb6bbaecfed8f213, obs1_43aa2bbce7f6b8f603323811e6f31a95')).toBe('3 cited posts');
  expect(plainSearched('cited posts obs1_7c5bbc4782cb6b3b0567d5670c39d603')).toBe('1 cited post');
  expect(plainSearched('the what to watch next text')).toBe('the what to watch next text');
});

/* Design audit item 10: a dossier gap printed the source key "instagram/hashtag". The route reads as its
   platform and a search term after it as "for <term>", the way the Ask page says it. */
test('a source route in a gap search reads as its platform', () => {
  expect(plainSearched('instagram/hashtag #fixture, 21 to 27 September: 0 found')).toBe('Instagram for #fixture, 21 to 27 September: 0 found');
  expect(plainSearched('tiktok/search/videos amapiano: 3 found')).toBe('TikTok for amapiano: 3 found');
  expect(plainSearched('x/search')).toBe('X');
});

/* Review of the route words: only a single search term right before a comma,
   a colon or the end is read as "for <term>". A sentence after the route
   keeps its own words, every route in the line is named, and a word that is
   not a platform before a slash is left alone. */
test('a source route never swallows the sentence after it', () => {
  expect(plainSearched('reddit/search/v2 and x/search, 21 to 28 September')).toBe('Reddit and X, 21 to 28 September');
  expect(plainSearched('tiktok/search for amapiano returned rate_limited')).toBe('TikTok for amapiano returned rate_limited');
  expect(plainSearched('reddit/search/v2 at 2026-09-24T12:30:00Z')).toBe('Reddit at 2026-09-24T12:30:00Z');
  expect(plainSearched('tiktok/video/screen-text')).toBe('TikTok');
  expect(plainSearched('instagram/search, 21 to 28 September')).toBe('Instagram, 21 to 28 September');
  expect(plainSearched('instagram/hashtag #fixture, Kenya, 21 to 27 September')).toBe('Instagram for #fixture, Kenya, 21 to 27 September');
  expect(plainSearched('24/7 coverage and/or replies')).toBe('24/7 coverage and/or replies');
});

/* Re-review N4: a search term of several words still reads as "for" that term. */
test('a search term of several words reads as for that term', () => {
  expect(plainSearched('instagram/hashtag sunday best, 21 to 27 September')).toBe('Instagram for sunday best, 21 to 27 September');
  expect(plainSearched('tiktok/search amapiano dance challenge: 0 found')).toBe('TikTok for amapiano dance challenge: 0 found');
  expect(plainSearched('x/search kota in soweto')).toBe('X for kota in soweto');
});

/* Review 3 N9 and N11: the reviewer's 22 route inputs, plus one row for each
   guard. A status sentence after a route stays a sentence ("TikTok came back
   empty"), a term ends at "and", "or", a full stop, a semicolon or the next
   route so every route keeps its own "for", a term runs to six words at most,
   and a slash after a word that is not a platform is left alone. */
const ROUTE_ROWS = [
  ['instagram/hashtag sunday best, 21 to 27 September', 'Instagram for sunday best, 21 to 27 September'],
  ['tiktok/search amapiano dance: 3 found', 'TikTok for amapiano dance: 3 found'],
  ['youtube/search "amapiano dance", Kenya', 'YouTube for "amapiano dance", Kenya'],
  ['x/search kota in soweto', 'X for kota in soweto'],
  ['tiktok/search returned nothing', 'TikTok returned nothing'],
  ['tiktok/search for amapiano returned rate_limited', 'TikTok for amapiano returned rate_limited'],
  ['reddit/search/v2 and x/search, 21 to 28 September', 'Reddit and X, 21 to 28 September'],
  ['tiktok/search came back empty', 'TikTok came back empty'],
  ['youtube/search was rate limited', 'YouTube was rate limited'],
  ['x/search gave no posts', 'X gave no posts'],
  ['instagram/hashtag found nothing', 'Instagram found nothing'],
  ['reddit/search timed out', 'Reddit timed out'],
  ['tiktok/search not run', 'TikTok not run'],
  ['x/search is not switched on', 'X is not switched on'],
  ["tiktok/search 'amapiano' and found 0", "TikTok for 'amapiano' and found 0"],
  ['tiktok/search amapiano and youtube/search kota', 'TikTok for amapiano and YouTube for kota'],
  ['instagram/hashtag #kota; tiktok/search kota', 'Instagram for #kota; TikTok for kota'],
  ['tiktok/search amapiano. No posts in the window', 'TikTok for amapiano. No posts in the window'],
  ['x/search skipped', 'X skipped'],
  ['tiktok/search kota, x/search kota, 21 to 27 September', 'TikTok for kota, X for kota, 21 to 27 September'],
  ['instagram/hashtag #fixture to 27 September', 'Instagram for #fixture to 27 September'],
  ['youtube/search amapiano dance challenge 2026 remix', 'YouTube for amapiano dance challenge 2026 remix'],
  ['youtube/search one two three four five six seven', 'YouTube one two three four five six seven'],
  ['tiktok/search kota x/search kota', 'TikTok for kota X for kota'],
  ['abc/def then tiktok/search kota', 'abc/def then TikTok for kota'],
  ['tiktok/search and x/search', 'TikTok and X'],
  ['tiktok/search in Kenya, 3 found', 'TikTok in Kenya, 3 found'],
  ['instagram/search @sipho.skits.07: 2 found', 'Instagram for @sipho.skits.07: 2 found'],
];

test('every route row reads as the reviewer read it, sentence or term', () => {
  const wrong = ROUTE_ROWS.map(([raw, words]) => [raw, plainSearched(raw), words]).filter(([, got, words]) => got !== words);
  expect(wrong).toEqual([]);
});
