import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {ComparePanel} from '../../compare.jsx';
import {ExplorePage} from '../../explore.jsx';
import {TodayPage} from '../../today.jsx';
import {DESK_AMBER_FRESHNESS, freshFixture} from './fixtures/instrument-payloads.js';
import {GREEN, signal, summaryFor} from '../../../tests/browser/fixtures/42-capability-routes/_desk.js';

/* Round 2 of the state matrix. Each case renders the route the review named
   and reads the frame it produces, so the repair cannot regress unread. */

const HELD = 'Held until the evidence authority is checked';

function refusedSignal(index){
  /* A ready summary with no receipts: the package validator refuses it and
     the adapter maps it onto the unchecked shape with a validated authority. */
  return signal(index, 'populated', {evidence_summary: {...summaryFor('populated'), receipts: []}});
}

describe('Compare names why an admitted signal is not placed', () => {
  test('a signal with no ribbon is captioned by that reason, not as an unchecked authority', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[signal(1), signal(2, 'populated', {ribbon_series: []})]} region="ZA" />);
    expect(markup).toContain('data-compare-state="unplaced"');
    expect(markup).toContain('ribbon_series_missing');
    expect(markup).not.toContain('evidence_authority_unchecked');
    expect(markup).toContain('1 with no evidence ribbon');
  });

  test('a refused summary is captioned by the validator token', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[signal(1), refusedSignal(2)]} region="ZA" />);
    expect(markup).toContain('evidence_summary_invalid');
    expect(markup).not.toContain(HELD);
  });

  test('only a set of unchecked authorities is a hold', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[signal(1, 'held'), signal(2, 'held')]} region="ZA" />);
    expect(markup).toContain('data-compare-state="held"');
    expect(markup).toContain(HELD);
    expect(markup).toContain('evidence_authority_unchecked');
  });

  test('an empty amber run is stale, dated through the shared run facts', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[]} region="ZA" freshness={freshFixture(DESK_AMBER_FRESHNESS)} />);
    expect(markup).toContain('data-compare-state="stale"');
    /* Round three, 24 Sept 2026: the no discovery body says the run finished
       without a signal strong enough to show, in a strategist's words rather
       than the engine's "admitted" and "dynamic". Same frame, same assertion. */
    expect(markup).not.toContain('The run finished without a signal strong enough to show.');
    const green = renderToStaticMarkup(<ComparePanel topics={[]} region="ZA" freshness={GREEN} />);
    expect(green).toContain('The run finished without a signal strong enough to show.');
  });
});

describe('A refused summary is a failed read, an unvalidated authority is a hold', () => {
  test('Briefing names the validator token as a failed read', () => {
    const markup = renderToStaticMarkup(<TodayPage topics={[refusedSignal(1)]} freshness={GREEN} deskDate="2026-09-12" />);
    expect(markup).toContain('evidence_summary_invalid');
    expect(markup).not.toContain(HELD);
  });

  test('Discover keeps the package cells for a refused summary', () => {
    const markup = renderToStaticMarkup(<ExplorePage topics={[refusedSignal(1)]} freshness={GREEN} region="ZA" />);
    expect(markup).toContain('discover-instrument');
    expect(markup).not.toContain(HELD);
  });

  test('both routes hold an unvalidated authority the engine declared', () => {
    const briefing = renderToStaticMarkup(<TodayPage topics={[signal(1, 'held')]} freshness={GREEN} deskDate="2026-09-12" />);
    const discover = renderToStaticMarkup(<ExplorePage topics={[signal(1, 'held')]} freshness={GREEN} region="ZA" />);
    expect(briefing).toContain(HELD);
    expect(discover).toContain(HELD);
    expect(discover).not.toContain('Open topic story');
  });
});
