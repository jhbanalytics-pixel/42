import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {
  COMPARE_FIELDS,
  buildComparisonRows,
  ComparisonStrip,
} from '../../compareStrips.jsx';

/* Compare reads dynamic signals against each other. Every row states the same
   field in the same column, so a reader compares like with like rather than
   re-reading a card each time. */

const signal = (over = {}) => ({
  signalId: 'sig_' + 'a'.repeat(64),
  title: 'Street football owns the evening',
  discoveryMode: 'phrase',
  whyNow: 'Clips moved into a shared format.',
  readiness: 'ready',
  receiptCount: 3,
  market: 'za',
  qualities: {
    velocity: 'moderate',
    novelty: 'high',
    breadth: 'low',
    independence: 'moderate',
    history: 'unmeasured',
    geo_confidence: 'high',
  },
  topicTags: ['street_football'],
  ...over,
});

describe('Compare as aligned strips', () => {
  test('the field order is published once and every column follows it', () => {
    expect(COMPARE_FIELDS.map((field) => field.key)).toEqual([
      'market',
      'readiness',
      'why_now',
      'precedent',
      'receipts',
      'velocity',
      'novelty',
      'breadth',
      'independence',
      'history',
      'geo_confidence',
    ]);
  });

  test('two signals produce one row per field with a value for each side', () => {
    const rows = buildComparisonRows([
      signal(),
      signal({signalId: 'sig_' + 'b'.repeat(64), market: 'ng', readiness: 'thin', receiptCount: 1}),
    ]);

    expect(rows).toHaveLength(COMPARE_FIELDS.length);
    const readiness = rows.find((row) => row.key === 'readiness');
    expect(readiness.values).toEqual(['ready', 'thin']);
    const receipts = rows.find((row) => row.key === 'receipts');
    expect(receipts.values).toEqual(['3 receipts', '1 receipt']);
    const market = rows.find((row) => row.key === 'market');
    expect(market.values).toEqual(['ZA', 'NG']);
  });

  test('an unmeasured quality stays unmeasured on both sides of the comparison', () => {
    const rows = buildComparisonRows([signal(), signal({qualities: {}})]);
    const history = rows.find((row) => row.key === 'history');
    expect(history.values).toEqual(['unmeasured', 'unmeasured']);
    const novelty = rows.find((row) => row.key === 'novelty');
    expect(novelty.values).toEqual(['high', 'unmeasured']);
  });

  test('a difference is marked so a reader sees where the two part company', () => {
    const rows = buildComparisonRows([
      signal(),
      signal({signalId: 'sig_' + 'b'.repeat(64), readiness: 'thin'}),
    ]);
    expect(rows.find((row) => row.key === 'readiness').differs).toBe(true);
    expect(rows.find((row) => row.key === 'novelty').differs).toBe(false);
  });

  test('fewer than two signals compare nothing rather than comparing a signal with itself', () => {
    expect(buildComparisonRows([signal()])).toEqual([]);
    expect(buildComparisonRows([])).toEqual([]);
  });

  test('the strip renders a header per signal and one aligned row per field', () => {
    const markup = renderToStaticMarkup(
      <ComparisonStrip signals={[signal(), signal({signalId: 'sig_' + 'b'.repeat(64), title: 'Sunday cycling clubs', market: 'ng'})]} />,
    );

    expect(markup).toContain('compare-strip');
    expect(markup).toContain('Street football owns the evening');
    expect(markup).toContain('Sunday cycling clubs');
    expect(markup).toContain('Source independence');
    expect(markup.match(/compare-strip__row/g)).toHaveLength(COMPARE_FIELDS.length);
    /* No engine number reaches the reader through Compare either. */
    expect(markup).not.toMatch(/0\.\d/);
  });

  test('the strip states what is absent when there is nothing to compare', () => {
    const markup = renderToStaticMarkup(<ComparisonStrip signals={[signal()]} />);
    expect(markup).toContain('needs two signals');
    expect(markup).not.toContain('compare-strip__row');
  });
});
