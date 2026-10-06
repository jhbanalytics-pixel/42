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
  expect(html).toContain('Searching now');
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

// Restated 4 October 2026 for the ranked-list redesign: the rank is a numeral in
// its own column and a refresh reads as a short day or relative time, while the
// exact ISO value stays in the time element's dateTime attribute.
test('shows the rank when present and keeps the supplied value in dateTime', () => {
  const now = Date.parse('2026-10-04T03:50:00Z');
  const unranked = render({signals, market: 'ZA', now});
  const ranked = render({signals, market: 'NG', now});

  expect(unranked).toContain('<span class="sr-only">unranked</span>');
  expect(unranked).toContain('dateTime="2026-09-30"');
  expect(unranked).toContain('>30 Sep</time>');
  expect(unranked).not.toContain('T00:00:00');
  expect(ranked).toContain('<span class="sr-only">Rank </span>3');
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

test('orders by rank, live before daily, then term, and repeats tied ranks', () => {
  const now = Date.parse('2026-10-04T03:50:00Z');
  const row = (term, source, rank, refreshed_at) => ({term, market: 'ZA', source, rank, refreshed_at});
  const html = render({market: 'ZA', now, signals: [
    row('wales vs norway', 'google_bq', 3, '2026-10-01'),
    row('farmer', 'google_trending', null, '2026-10-04T03:30:00Z'),
    row('denmark vs portugal', 'google_bq', 1, '2026-10-01'),
    row('croatia vs england', 'google_trending', 1, '2026-10-04T00:45:59Z'),
    row('south africa green id end date', 'google_bq', 2, '2026-10-01'),
    row('eritrea vs south africa', 'google_bq', 1, '2026-10-01'),
  ]});
  const terms = [...html.matchAll(/class="searching-now__term">([^<]+)/g)].map((match) => match[1]);
  expect(terms).toEqual(['croatia vs england', 'denmark vs portugal', 'eritrea vs south africa', 'wales vs norway', 'south africa green id end date', 'farmer']);
  expect(html).toContain('Searching now<span class="searching-now__place"> · South Africa</span>');
  expect(html).toContain('Matches · 4');
  expect(html).toContain('Other searches · 2');
  expect([...html.matchAll(/<span class="sr-only">Rank <\/span>1</g)]).toHaveLength(3);
  expect([...html.matchAll(/searching-now__local/g)]).toHaveLength(2);
  expect(html.replace(/<[^>]+>/g, '')).toContain('Live trending, 20 min ago · Daily top terms, 1 Oct');
  expect([...html.matchAll(/searching-now__mark--fresh/g)]).toHaveLength(1);
  expect(html).not.toMatch(/[\u2013\u2014]/);
  expect(render({market: 'ZA', now, nameMarket: false, signals: [row('farmer', 'google_bq', 1, '2026-10-01')]})).not.toContain('searching-now__place');
});

test('accepts the Google Trends daily feed as a daily source and still drops unknown sources', () => {
  const now = Date.parse('2026-10-04T03:50:00Z');
  const feed = {term: 'lotto results', market: 'ZA', source: 'google_rss', rank: 2, refreshed_at: '2026-10-03'};
  const html = render({market: 'ZA', now, signals: [feed]});
  expect(html).toContain('lotto results');
  expect(html).toContain('title="Google Trends daily feed"');
  expect(html).toContain('data-search-source="daily"');
  expect(html).not.toContain('searching-now__mark--live');
  expect(html.replace(/<[^>]+>/g, '')).toContain('Daily feed, 3 Oct');
  expect(render({market: 'ZA', now, signals: [{...feed, source: 'google_news'}]})).toBe('');
});

// Added 4 October 2026: refreshed_at is now a plain day (the SAST day of the
// fetch), so the fresh mark compares days: a live row dated today in SAST is
// fresh. The row keeps exactly the five contract keys.
test('marks a live row fresh when its plain refresh day is today in South African time', () => {
  const live = (refreshed_at) => ({term: 'springboks', market: 'ZA', source: 'google_trending', rank: 1, refreshed_at});
  const morning = Date.parse('2026-10-04T03:50:00Z');
  expect(isFresh(live('2026-10-04'), morning)).toBe(true);
  expect(isFresh(live('2026-10-03'), morning)).toBe(false);
  expect(isFresh(live('2026-10-05'), morning)).toBe(false);
  // 22:30 UTC on 4 Oct is 00:30 SAST on 5 Oct: the SAST day decides, not the UTC one.
  const pastMidnight = Date.parse('2026-10-04T22:30:00Z');
  expect(isFresh(live('2026-10-05'), pastMidnight)).toBe(true);
  expect(isFresh(live('2026-10-04'), pastMidnight)).toBe(false);
  // 21:59 UTC is still 23:59 SAST on the same day.
  expect(isFresh(live('2026-10-04'), Date.parse('2026-10-04T21:59:59Z'))).toBe(true);
  // Daily sources never carry the red, even when dated today.
  expect(isFresh({...live('2026-10-04'), source: 'google_bq'}, morning)).toBe(false);
  expect(isFresh({...live('2026-10-04'), source: 'google_rss'}, morning)).toBe(false);

  const html = render({market: 'ZA', now: morning, signals: [
    live('2026-10-04'),
    {...live('2026-10-03'), term: 'load shedding', rank: 2},
    {...live('2026-10-04'), term: 'lotto results', source: 'google_rss', rank: 3},
  ]});
  expect([...html.matchAll(/searching-now__mark--fresh/g)]).toHaveLength(1);
  expect(html).toMatch(/searching-now__mark--live searching-now__mark--fresh"[^>]*><\/span>live<span class="sr-only">[^<]*<\/span><\/span><time dateTime="2026-10-04"/);
  expect(Object.keys(live('2026-10-04')).sort().join(',')).toBe('market,rank,refreshed_at,source,term');
  expect(render({market: 'ZA', now: morning, signals: [{...live('2026-10-04'), fresh: true}]})).toBe('');
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
