/* readerWord writes a host token in the reader's words. It runs over platform
   keys and over research ref types alike, so a ref type must never pick up a
   platform's name from a loose match: "google_trends_rising" printed as
   "Google Search" was a false provenance line on the search card. */
import {describe, expect, test} from 'bun:test';
import {readerWord} from '../../model.js';
import {EMPTY_STATE_FACTS, readerPhrase} from '../../instrumentRouteModels.js';

describe('readerWord names only what the token is', () => {
  test('exact platform keys keep the product names', () => {
    expect(readerWord('tiktok')).toBe('TikTok');
    expect(readerWord('youtube')).toBe('YouTube');
    expect(readerWord('threads')).toBe('Threads');
    expect(readerWord('news')).toBe('News');
    expect(readerWord('x')).toBe('X');
    expect(readerWord('google_search')).toBe('Google Search');
  });

  test('research ref types read as their own kind', () => {
    expect(readerWord('google_trends_rising')).toBe('Google Trends');
  });

  test('a token that only contains a platform word is not that platform', () => {
    expect(readerWord('search_volume')).toBe('Search volume');
    expect(readerWord('comment_thread')).toBe('Comment thread');
    expect(readerWord('newsletter')).toBe('Newsletter');
  });

  test('unknown tokens fall back to sentence case with underscores as spaces', () => {
    expect(readerWord('brand_moment')).toBe('Brand moment');
    expect(readerWord('post')).toBe('Post');
  });

  test('host cased words and empty input are left as written', () => {
    expect(readerWord('BBC News')).toBe('BBC News');
    expect(readerWord('')).toBe('');
    expect(readerWord(null)).toBe('');
  });
});

/* readerPhrase writes the Listen filters into the empty state line, and it
   had the same loose platform match: a category of "newsletter" read as
   "From News". It holds to the same exact keys. */
describe('readerPhrase names only what the token is', () => {
  test('exact platform keys keep the product names', () => {
    expect(readerPhrase('tiktok')).toBe('TikTok');
    expect(readerPhrase('google_search')).toBe('Google Search');
    expect(readerPhrase('news')).toBe('News');
  });

  test('ref types and tokens that only contain a platform word are not that platform', () => {
    expect(readerPhrase('google_trends_rising')).toBe('Google Trends');
    expect(readerPhrase('search_volume')).toBe('Search volume');
    expect(readerPhrase('comment_thread')).toBe('Comment thread');
    expect(readerPhrase('newsletter')).toBe('Newsletter');
  });

  test('the empty state facts carry the plain words', () => {
    expect(EMPTY_STATE_FACTS.category('newsletter')).toBe('From Newsletter');
    expect(EMPTY_STATE_FACTS.category('search_volume')).toBe('From Search volume');
  });
});

/* The server sends platform keys the frontend glyph table does not carry, and
   names them in _PLATFORM_LABELS (app/src/api/bq.py and card.py). The exact
   key check must not lose those names: at fb925ba "twitter" read as "X". */
describe('server platform keys keep the server names', () => {
  const cases = [
    ['twitter', 'X'],
    ['rss', 'News'],
    ['gdelt', 'GDELT'],
    ['apple_music', 'Apple Music'],
    ['bigquery_trends', 'Search Trends'],
    ['search', 'Search'],
    ['web', 'Web'],
  ];
  test('readerWord names each key as the server does', () => {
    for (const [key, label] of cases) expect(readerWord(key)).toBe(label);
  });
  test('readerPhrase names each key as the server does', () => {
    for (const [key, label] of cases) expect(readerPhrase(key)).toBe(label);
  });
});
