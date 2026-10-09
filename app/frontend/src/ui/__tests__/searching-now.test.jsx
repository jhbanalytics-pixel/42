import {expect, test} from 'bun:test';
import React from 'react';
import {readFileSync} from 'node:fs';
import {renderToStaticMarkup} from 'react-dom/server';
import {SearchingNow, freshnessWords, isFresh} from '../SearchingNow.jsx';

const signals = JSON.parse(readFileSync(new URL('./fixtures/searching-now.json', import.meta.url), 'utf8'));
const render = (props) => renderToStaticMarkup(<SearchingNow {...props} />);

test('renders the five-field search strip in stable market groups', () => {
  const html = render({signals, market: 'ALL'});

  expect(html).toContain('data-section="searching-now"');
  // Tester report, 5 October 2026: "Searching now" read as a page still loading.
  expect(html).toContain('Trending on Google');
  expect(html).not.toContain('Searching now');
  expect(html).toContain('Google search interest, not posts');
  expect(html).not.toContain('<article');
  expect([...html.matchAll(/data-search-market="([A-Z]{2})"/g)].map((match) => match[1]))
    .toEqual(['ZA', 'NG', 'KE']);
  expect(signals.every((signal) => Object.keys(signal).sort().join(',') === 'market,rank,refreshed_at,source,term'))
    .toBe(true);
});

test('keeps only the selected market', () => {
  const html = render({signals, market: 'ZA'});

  expect(html).toContain('fixture query ZA');
  expect(html).not.toContain('fixture query NG');
  expect(html).not.toContain('fixture query KE');
  expect([...html.matchAll(/data-search-market="([A-Z]{2})"/g)].map((match) => match[1]))
    .toEqual(['ZA']);
});

// Restated 4 October 2026 for the ranked-list redesign: a refresh reads as a
// short day or relative time, while the exact ISO value stays in the time
// element's dateTime attribute. Restated 6 October 2026 (tester report): the
// rank left its own numeral column, where Google's gaps (2, or 3, 6, 8, 11)
// read as missing rows, and is labelled as Google's after the freshness.
test('shows the rank when present and keeps the supplied value in dateTime', () => {
  const now = Date.parse('2026-10-04T03:50:00Z');
  const unranked = render({signals, market: 'ZA', now});
  const ranked = render({signals, market: 'NG', now});

  expect(unranked).toContain('<span class="sr-only">, unranked on Google</span>');
  expect(unranked).not.toContain('searching-now__rank');
  expect(unranked).toContain('dateTime="2026-09-30"');
  expect(unranked).toContain('>30 Sep</time>');
  expect(unranked).not.toContain('T00:00:00');
  expect(ranked).toContain('<span class="searching-now__rank" data-google-rank="3"><span class="sr-only">, </span>Google rank 3</span>');
  expect(ranked).toMatch(/searching-now__term">fixture query NG<\/span><span class="searching-now__meta">/);
  expect(ranked).toContain('dateTime="2026-09-30T12:34:56Z"');
  expect(ranked).toContain('>30 Sep</time>');
  expect(ranked).not.toContain('>2026-09-30T12:34:56Z<');
});

test('reads freshness against an injected now', () => {
  const now = Date.parse('2026-10-04T03:50:00Z');
  expect(freshnessWords('2026-10-04T03:49:30Z', now)).toBe('just now');
  expect(freshnessWords('2026-10-04T03:20:00Z', now)).toBe('30 min ago');
  expect(freshnessWords('2026-10-04T00:45:59Z', now)).toBe('3 h ago');
  expect(freshnessWords('2026-10-03T01:00:00Z', now)).toBe('yesterday');
  expect(freshnessWords('2026-10-02T20:00:00Z', now)).toBe('2 Oct');
  expect(freshnessWords('2026-10-01T09:00:00Z', now)).toBe('1 Oct');
  expect(freshnessWords('2026-10-01', now)).toBe('1 Oct');
  expect(freshnessWords('2025-12-31', now)).toBe('31 Dec 2025');
});

test('orders live rows by rank then term and repeats tied ranks', () => {
  const now = Date.parse('2026-10-04T03:50:00Z');
  const row = (term, source, rank, refreshed_at) => ({term, market: 'ZA', source, rank, refreshed_at});
  const html = render({market: 'ZA', now, signals: [
    row('wales vs norway', 'google_trending', 3, '2026-10-04T02:00:00Z'),
    row('farmer', 'google_trending', null, '2026-10-04T03:30:00Z'),
    row('denmark vs portugal', 'google_trending', 1, '2026-10-04T02:00:00Z'),
    row('croatia vs england', 'google_trending', 1, '2026-10-04T00:45:59Z'),
    row('south africa green id end date', 'google_trending', 2, '2026-10-04T01:00:00Z'),
    row('eritrea vs south africa', 'google_trending', 1, '2026-10-04T02:30:00Z'),
  ]});
  const terms = [...html.matchAll(/class="searching-now__term">([^<]+)/g)].map((match) => match[1]);
  expect(terms).toEqual(['croatia vs england', 'denmark vs portugal', 'eritrea vs south africa', 'wales vs norway', 'south africa green id end date', 'farmer']);
  expect(html).toContain('Trending on Google<span class="searching-now__place"> · South Africa</span>');
  expect(html).toContain('Matches · 4');
  expect(html).toContain('Other searches · 2');
  expect([...html.matchAll(/data-google-rank="1"><span class="sr-only">, <\/span>Google rank 1</g)]).toHaveLength(3);
  expect([...html.matchAll(/searching-now__local/g)]).toHaveLength(2);
  expect(html.replace(/<[^>]+>/g, '')).toContain('Live trending, 20 min ago');
  expect([...html.matchAll(/searching-now__mark--fresh/g)]).toHaveLength(1);
  expect(html).not.toMatch(new RegExp('[' + String.fromCharCode(0x2013, 0x2014) + ']'));
  expect(render({market: 'ZA', now, nameMarket: false, signals: [row('farmer', 'google_trending', 1, '2026-10-04T03:30:00Z')]})).not.toContain('searching-now__place');
});

// Wave 8 decision W8-DEC-04: the strip shows only Google's live trending list,
// as search interest and never as post evidence. The daily sources are not shown.
test('shows only live Google trending rows and drops the daily sources and unknown ones', () => {
  const now = Date.parse('2026-10-04T03:50:00Z');
  const live = {term: 'springboks', market: 'ZA', source: 'google_trending', rank: 1, refreshed_at: '2026-10-04T03:30:00Z'};
  const bq = {term: 'daily top term', market: 'ZA', source: 'google_bq', rank: 2, refreshed_at: '2026-10-03'};
  const rss = {term: 'lotto results', market: 'ZA', source: 'google_rss', rank: 3, refreshed_at: '2026-10-03'};
  const html = render({market: 'ZA', now, signals: [bq, rss, live]});
  expect(html).toContain('springboks');
  expect(html).not.toContain('daily top term');
  expect(html).not.toContain('lotto results');
  expect(html).not.toContain('data-search-source="daily"');
  expect(html).toContain('data-search-source="live"');
  expect(html).not.toContain('Google Trends daily feed');
  expect(html.replace(/<[^>]+>/g, '')).not.toMatch(/Daily top terms|Daily feed/);
  expect(html.replace(/<[^>]+>/g, '')).toContain('Live trending, 20 min ago');
  expect(html).toContain('Google search interest, not posts');
  expect(html).not.toMatch(/evidence/i);
  expect(html).toContain('data-logo="google"');
  expect(render({market: 'ZA', now, signals: [bq, rss]})).toBe('');
  expect(render({market: 'ALL', now, signals: [bq, rss]})).toBe('');
  expect(render({market: 'ZA', now, signals: [{...live, source: 'google_news'}]})).toBe('');
  const all = render({market: 'ALL', now, signals: [bq, {...live, market: 'NG', term: 'naija live'}, {...rss, market: 'KE'}]});
  expect([...all.matchAll(/data-search-market="([A-Z]{2})"/g)].map((match) => match[1])).toEqual(['NG']);
  expect(all).not.toContain('daily top term');
});

test('a plain refresh day cannot prove that a live row is less than an hour old', () => {
  const live = (refreshed_at) => ({term: 'springboks', market: 'ZA', source: 'google_trending', rank: 1, refreshed_at});
  const morning = Date.parse('2026-10-04T03:50:00Z');
  expect(isFresh(live('2026-10-04'), morning)).toBe(false);
  expect(isFresh(live('2026-10-03'), morning)).toBe(false);
  expect(isFresh(live('2026-10-05'), morning)).toBe(false);
  // A date alone remains insufficient at the SAST date boundary.
  const pastMidnight = Date.parse('2026-10-04T22:30:00Z');
  expect(isFresh(live('2026-10-05'), pastMidnight)).toBe(false);
  expect(isFresh(live('2026-10-04'), pastMidnight)).toBe(false);
  // A date alone also remains insufficient just before SAST midnight.
  expect(isFresh(live('2026-10-04'), Date.parse('2026-10-04T21:59:59Z'))).toBe(false);
  // Daily sources never carry the red, even when dated today.
  expect(isFresh({...live('2026-10-04'), source: 'google_bq'}, morning)).toBe(false);
  expect(isFresh({...live('2026-10-04'), source: 'google_rss'}, morning)).toBe(false);

  const html = render({market: 'ZA', now: morning, signals: [
    live('2026-10-04'),
    {...live('2026-10-03'), term: 'load shedding', rank: 2},
    {...live('2026-10-04'), term: 'lotto results', source: 'google_rss', rank: 3},
  ]});
  expect([...html.matchAll(/searching-now__mark--fresh/g)]).toHaveLength(0);
  expect(html).toContain('dateTime="2026-10-04"');
  expect(Object.keys(live('2026-10-04')).sort().join(',')).toBe('market,rank,refreshed_at,source,term');
  expect(render({market: 'ZA', now: morning, signals: [{...live('2026-10-04'), fresh: true}]})).toBe('');
});

test('only a live timestamp in the last hour earns a fresh mark across SAST midnight', () => {
  const now = Date.parse('2026-10-04T22:30:00Z');
  const live = (refreshed_at) => ({term: 'springboks', market: 'ZA', source: 'google_trending', rank: 1, refreshed_at});
  expect(isFresh(live('2026-10-04T21:30:01Z'), now)).toBe(true);
  expect(isFresh(live('2026-10-04T21:30:00Z'), now)).toBe(false);
  expect(isFresh(live('2026-10-04T22:30:01Z'), now)).toBe(false);
  expect(isFresh({...live('2026-10-04T22:29:00Z'), source: 'google_rss'}, now)).toBe(false);
});

test('renders terms as text', () => {
  const html = render({
    signals: [{...signals[0], term: '<img src=x onerror=alert(1)>'}],
    market: 'ZA',
  });

  expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
  expect(html).not.toContain('<img src=x');
});

test('hides absent, empty, and unusable signals without filling values', () => {
  const unusable = [
    null,
    {...signals[0], market: 'za'},
    {...signals[0], source: 'web'},
    {...signals[0], rank: 0},
    {...signals[0], refreshed_at: '2026-09-30T12:34:56+02:00'},
    {...signals[0], traffic_band: 'high'},
  ];

  expect(render({market: 'ZA'})).toBe('');
  expect(render({signals: [], market: 'ALL'})).toBe('');
  expect(render({signals: unusable, market: 'ALL'})).toBe('');
  expect(render({signals, market: 'all'})).toBe('');
});

// Tester report, 5 October 2026: the caption beside the heading read as a
// separate column. It sits under the heading and says what a rank is.
test('the caption sits under the heading and explains Google ranks', () => {
  const html = render({signals, market: 'NG'});
  // The caption keeps its exact words (scripts/staging_demo_check.py reads them); the rank note is its own line.
  expect(html).toMatch(/<h2 class="searching-now__heading">Trending on Google[^]*?<\/h2><p class="searching-now__caption">Google search interest, not posts<\/p><p class="searching-now__note">A rank is the term&#x27;s place on Google&#x27;s own list, so some numbers are skipped\.<\/p>/);
  expect(render({signals, market: 'ZA'})).not.toContain('searching-now__note');
  expect(html).not.toMatch(/[\u2013\u2014]/);
  const css = readFileSync(new URL('../../styles/searching-now.css', import.meta.url), 'utf8');
  const header = /\.searching-now__header \{[^}]*\}/.exec(css)[0];
  expect(header).toContain('grid-template-columns: minmax(0, 1fr);');
  expect(/\.searching-now__caption \{[^}]*\}/.exec(css)[0]).not.toContain('text-align: right');
});

// Wave 8: the heading carries Google's mark, vendored inline and decorative,
// beside the words; the heading and caption markup is otherwise unchanged.
test('the header carries a decorative Google logo beside the heading text', () => {
  const html = render({signals, market: 'NG'});
  const svg = /<svg[^>]*class="pl-logo[^"]*"[^>]*>/.exec(html);
  expect(svg).not.toBeNull();
  expect(svg[0]).toContain('aria-hidden="true"');
  expect(svg[0]).toContain('data-logo="google"');
  expect(html.indexOf('pl-logo')).toBeLessThan(html.indexOf('searching-now__heading'));
  expect(html.indexOf('<svg')).toBeGreaterThan(html.indexOf('searching-now__header'));
  expect(html).toContain('>Trending on Google');
  expect(html).not.toMatch(/<svg[^>]*>[^]*<title>/);
  expect(html).not.toMatch(/<image|<img/);
  const css = readFileSync(new URL('../../styles/searching-now.css', import.meta.url), 'utf8');
  expect(css).toMatch(/\.searching-now__logo\s*\{[^}]*position:\s*absolute/);
});
