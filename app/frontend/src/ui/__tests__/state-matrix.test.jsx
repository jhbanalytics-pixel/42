import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {RedThreadBriefing} from '../RedThreadBriefing.jsx';
import {ExplorePage} from '../../explore.jsx';
import {ComparePanel} from '../../compare.jsx';
import {ComparisonStrip} from '../../compareStrips.jsx';
import {READY_SUMMARY} from './fixtures/instrument-payloads.js';

/* Batch G: the state matrix, executed rather than asserted in a document.

   Every job must be able to say what is absent, which run and window were
   checked where that is known, and what to do next. A surface that renders a
   plausible screen for an unknown state is the failure this matrix exists to
   catch, so each case is rendered and read, not merely listed. */

const SIG = 'sig_' + 'a'.repeat(64);

const signalRow = (over = {}) => ({
  contract_version: 'desk_dynamic_signal_v2',
  signal_id: SIG,
  discovery_mode: 'phrase',
  evidence_state: 'ready',
  label: 'Street football owns the evening',
  why_now: 'Clips moved into a shared format.',
  possible_response: 'Brief one local pitch.',
  market: 'za',
  receipts: [{id: 'r1', url: 'https://example.test/a', platform: 'TikTok'}],
  observation_start: '2026-08-01T00:00:00Z',
  observation_end: '2026-08-26T00:00:00Z',
  observation_method: 'closed completed-run observation',
  qualities: {
    velocity: 'moderate',
    novelty: 'moderate',
    breadth: 'moderate',
    independence: 'moderate',
    history: 'unmeasured',
    geo_confidence: 'moderate',
    topic_tags: [],
  },
  ...over,
});

const explicitUncheckedSignal = () => signalRow({
  evidence_state: 'unchecked',
  evidence_summary: {
    ...structuredClone(READY_SUMMARY),
    state: 'unchecked',
    receipts: [],
    independence: {status: 'unvalidated', familyCount: 0, groupingAuthority: null},
    direction: {status: 'unknown', supportingReceiptIds: [], opposingReceiptIds: []},
    limitations: ['Server verification is pending.'],
  },
  ribbon_series: [],
});

const stripText = (markup) => markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();

describe('Briefing states', () => {
  const cases = [
    ['loading', {topics: [], loading: true}, /preparing|checking/i],
    ['error', {topics: [], error: {message: 'The completed run could not be read.'}}, /unavailable/i],
    ['no discovery', {topics: [], freshness: {status: 'green'}}, /no signals were discovered/i],
    ['stale', {topics: [], freshness: {status: 'amber', stamp_utc: '2026-08-27T06:30:00Z'}}, /withholding|held/i],
  ];

  for (const [name, props, expected] of cases){
    test(`${name} states what is absent instead of rendering a decision`, () => {
      const markup = renderToStaticMarkup(<RedThreadBriefing {...props} />);
      const text = stripText(markup);
      expect(text).toMatch(expected);
      /* No state but ready may present a recommendation. */
      expect(text).not.toContain('Brief one local pitch');
    });
  }

  test('ready renders the decision path and nothing that contradicts it', () => {
    const markup = renderToStaticMarkup(
      <RedThreadBriefing topics={[signalRow()]} freshness={{status: 'green'}} />,
    );
    const text = stripText(markup);
    expect(text).toContain('Brief one local pitch');
    expect(text).not.toMatch(/no signals were discovered/i);
  });
});

describe('Discover states', () => {
  const cases = [
    ['loading', {topics: [], loading: true}, /reading the completed run/i],
    ['error', {topics: [], error: 'source unreadable'}, /could not read the run/i],
    ['no discovery', {topics: [], freshness: {status: 'green'}}, /no signals this run/i],
    ['stale', {topics: [], freshness: {status: 'amber', stamp_utc: '2026-08-27T06:30:00Z'}}, /held until the next run/i],
  ];

  for (const [name, props, expected] of cases){
    test(`${name} names the absence in the strategist's language`, () => {
      const markup = renderToStaticMarkup(<ExplorePage region="ZA" {...props} />);
      expect(stripText(markup)).toMatch(expected);
    });
  }

  test('the normal ready branch exposes the admitted count even when authority is unavailable', () => {
    const markup = renderToStaticMarkup(
      <ExplorePage topics={[signalRow()]} freshness={{status: 'green'}} region="ZA" />,
    );
    /* Quiet register, 23 Sept 2026: the count says what the reader sees, in plain
       words; "admitted" is the engine's word for a signal that passed its gate. */
    expect(stripText(markup)).toContain('1 of 1 signals shown');
    expect(stripText(markup)).not.toContain('admitted signals');
  });
});

describe('Compare states', () => {
  test('error, loading and insufficient each say which one they are', () => {
    const error = renderToStaticMarkup(<ComparePanel topics={[]} error={{message: 'unreadable'}} />);
    expect(error).toContain('data-compare-state="error"');

    const loading = renderToStaticMarkup(<ComparePanel topics={[]} loading />);
    expect(loading).toContain('data-compare-state="loading"');

    /* The count is what the run admitted, which is the count Compare alone
       holds. A run that admitted one signal it cannot place is not a run that
       admitted nothing. */
    const one = renderToStaticMarkup(<ComparePanel topics={[signalRow()]} />);
    expect(one).toContain('data-compare-state="insufficient"');
    expect(stripText(one)).toMatch(/admitted 1 signal\. Comparison needs two\./);
    /* Round three, 24 Sept 2026: the no discovery body says the run finished
       without a signal strong enough to show, in a strategist's words rather
       than the engine's "admitted" and "dynamic". Same frame, same assertion. */
    expect(stripText(one)).not.toMatch(/finished without a signal strong enough to show/i);

    const none = renderToStaticMarkup(<ComparePanel topics={[]} />);
    expect(stripText(none)).toMatch(/finished without a signal strong enough to show/i);
  });

  test('thin and contradictory readiness survive into the comparison', () => {
    const rows = [
      {signalId: 'a', title: 'A', readiness: 'thin', receiptCount: 1, market: 'za', qualities: {}},
      {signalId: 'b', title: 'B', readiness: 'contradictory', receiptCount: 2, market: 'ng', qualities: {}},
    ];
    const text = stripText(renderToStaticMarkup(<ComparisonStrip signals={rows} />));
    expect(text).toContain('thin');
    expect(text).toContain('contradictory');
  });
});

describe('Evidence readiness is never upgraded by rendering', () => {
  test('a legacy row without explicit authority is visibly unavailable', () => {
    const markup = renderToStaticMarkup(
      <ExplorePage topics={[signalRow({receipts: [], evidence_state: 'ready'})]} freshness={{status: 'green'}} region="ZA" />,
    );
    expect(stripText(markup)).toContain('Unavailable');
    expect(markup).not.toContain('data-evidence-state="ready"');
    expect(markup).not.toContain('data-evidence-state="unchecked"');
    expect(markup).not.toContain('Evidence state: unchecked');
  });

  test('an explicit valid unchecked authority renders as unchecked', () => {
    const markup = renderToStaticMarkup(
      <ExplorePage topics={[explicitUncheckedSignal()]} freshness={{status: 'green'}} region="ZA" />,
    );
    /* Round 9 removed the host lead from Discover, so the unchecked authority
       is carried by the package specimen: the candidate row's state and its
       provenance meta, never an upgrade to ready. */
    expect(markup).toContain('data-state="unchecked"');
    expect(markup).not.toContain('data-evidence-state="ready"');
    expect(stripText(markup)).toContain('unchecked');
    expect(stripText(markup)).not.toContain('Select a valid candidate');
  });
});
