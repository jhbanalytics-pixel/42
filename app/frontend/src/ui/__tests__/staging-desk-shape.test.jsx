/* Round 10 of the 42 in Black programme, task 45: the two reasons no
   instrument rendered on staging at revision 00125-vrf.

   P1a, the envelope. The desk sends each discovered signal as
   `{signal: {contract_version: "desk_dynamic_signal_v2", ...}}`. The wrapper
   carries no version of its own. `versionedSignal` demanded the version on the
   root before it would read the nested signal, so it refused every live row
   and Briefing, the Discover candidate list, Compare and the shell rail
   summary all read unavailable. Discover's own admission reads the same row
   through `sourceField`, which falls through to `row.signal`, so the two
   readers disagreed about the same payload.

   P1b, the ordering. The route state builders tested the desk-wide freshness
   stamp before they looked at the run, so a released run with
   `dynamic_discovery.status: ready` never reached the decision while the
   ingest stamp sat amber. Readiness is the released run's own contract; the
   freshness stamp measures ingest and the shell already shows it. The held
   card stays for the branches where discovery itself is not ready.

   The payload here is the row staging actually served, captured from
   `/api/desk?region=za` on 4 September 2026. */
import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {buildBriefingState} from '../../briefingContract.js';
import {ComparePanel} from '../../compare.jsx';
import {ExplorePage} from '../../explore.jsx';
import {admitExploreSignals, buildExploreState} from '../../explore.jsx';
import {adaptSignalToInstrumentModel, railSummaryForTopics} from '../../instrumentAdapters.js';
import {TodayPage} from '../../today.jsx';
import {
  DESK_AMBER_FRESHNESS,
  DESK_WRAPPED_SIGNAL,
  ROOT_SIGNAL,
  freshFixture,
} from './fixtures/instrument-payloads.js';

const UNAVAILABLE = {state: 'unavailable', error: 'instrument_payload_unavailable'};

const deskRow = () => freshFixture(DESK_WRAPPED_SIGNAL);
const amber = () => freshFixture(DESK_AMBER_FRESHNESS);
const text = (markup) => markup.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();

describe('the adapter reads the envelope the desk sends', () => {
  test('a wrapper whose nested signal carries the version is read through', () => {
    const model = adaptSignalToInstrumentModel(deskRow());
    expect(model.state).not.toBe('unavailable');
    expect(model.id).toBe(DESK_WRAPPED_SIGNAL.signal.signal_id);
    expect(model.title).toBe('johannesburg');
  });

  test('a root that carries the version is still read', () => {
    const model = adaptSignalToInstrumentModel(freshFixture(ROOT_SIGNAL));
    expect(model.state).not.toBe('unavailable');
    expect(model.id).toBe('sig_root');
  });

  test('a wrapper that carries the version on both levels is still read', () => {
    const model = adaptSignalToInstrumentModel({
      contract_version: 'desk_dynamic_signal_v2',
      signal: freshFixture(ROOT_SIGNAL),
    });
    expect(model.state).not.toBe('unavailable');
    expect(model.id).toBe('sig_root');
  });

  test('a wrapper with the version on neither level is refused', () => {
    const row = deskRow();
    delete row.signal.contract_version;
    expect(adaptSignalToInstrumentModel(row)).toEqual(UNAVAILABLE);
  });

  test('a wrapper whose nested signal states another contract is refused', () => {
    const row = deskRow();
    row.contract_version = 'desk_dynamic_signal_v2';
    row.signal.contract_version = 'desk_dynamic_signal_v1';
    expect(adaptSignalToInstrumentModel(row)).toEqual(UNAVAILABLE);
  });

  test('the shell does not label the admitted live row ready without measured evidence', () => {
    expect(adaptSignalToInstrumentModel(deskRow()).evidenceSummary.state).toBe('unchecked');
    expect(railSummaryForTopics([deskRow()])).toMatchObject({ready: 0, thin: 0, contradictory: 0});
  });
});

describe('a released run outranks the ingest freshness stamp', () => {
  test('Discover is ready while the desk stamp is amber', () => {
    expect(buildExploreState({topics: [deskRow()], freshness: amber()}).state).toBe('ready');
  });

  test('Briefing reaches the run while the desk stamp is amber', () => {
    expect(buildBriefingState({topics: [deskRow()], freshness: amber()}).state).toBe('ready');
  });

  test('an amber stamp still dates the hold when discovery admitted nothing', () => {
    const held = buildExploreState({topics: [], freshness: amber()});
    expect(held).toMatchObject({state: 'stale', checkedAt: DESK_AMBER_FRESHNESS.stamp_utc});
    expect(buildBriefingState({topics: [], freshness: amber()}))
      .toMatchObject({state: 'stale', checkedAt: DESK_AMBER_FRESHNESS.stamp_utc});
  });

  test('a run that admitted nothing under a green stamp is still no discovery', () => {
    expect(buildExploreState({topics: [], freshness: {status: 'green'}}).state).toBe('no_discovery');
    expect(buildBriefingState({topics: [], freshness: {status: 'green'}}).state).toBe('no_discovery');
  });

  test('loading and error still outrank both', () => {
    expect(buildExploreState({topics: [deskRow()], freshness: amber(), loading: true}).state).toBe('loading');
    expect(buildExploreState({topics: [deskRow()], freshness: amber(), error: {code: 'x'}}).state).toBe('error');
    expect(buildBriefingState({topics: [deskRow()], freshness: amber(), loading: true}).state).toBe('loading');
    expect(buildBriefingState({topics: [deskRow()], freshness: amber(), error: {code: 'x'}}).state).toBe('error');
  });
});

describe('the routes against the payload staging served', () => {
  /* The live signal_name is `johannesburg`, one token and no whitespace.
     Package 2.0.12 rules that a title is a proposition only when it is a
     written line, so the instrument mounts and admits the row while
     withholding the identifier rather than printing it as a heading. The
     route still reaches ready and still renders the package instrument,
     which is what this test is for; the withheld identifier is the ruling,
     not a fault, and the assertion moves onto it. */
  test('Discover renders the instrument, not a held card', () => {
    const markup = renderToStaticMarkup(
      <ExplorePage topics={[deskRow()]} freshness={amber()} region="ZA" />,
    );
    expect(markup).toContain('data-explore-state="ready"');
    expect(markup).toContain('discover-instrument');
    expect(markup).toContain('data-candidate-count="1"');
    expect(markup).not.toContain('johannesburg');
    expect(markup).not.toContain('data-state="stale"');
    /* Quiet register, 23 Sept 2026: the count says what the reader sees, in plain
       words; "admitted" is the engine's word for a signal that passed its gate. */
    expect(text(markup)).toContain('1 of 1 signals shown');
    expect(text(markup)).not.toContain('admitted signals');
  });

  test('Compare stops saying the run admitted nothing when it admitted one', () => {
    const markup = renderToStaticMarkup(<ComparePanel topics={[deskRow()]} region="ZA" />);
    const surface = text(markup);
    expect(admitExploreSignals([deskRow()]).signals).toHaveLength(1);
    /* Round three, 24 Sept 2026: the no discovery body says the run finished
       without a signal strong enough to show, in a strategist's words rather
       than the engine's "admitted" and "dynamic". Same frame, same assertion. */
    expect(surface).not.toContain('The run finished without a signal strong enough to show.');
    expect(surface).toContain('Not enough to compare');
    expect(surface).toContain('The completed run admitted 1 signal. Comparison needs two.');
  });

  test('Compare still reads no discovery when the run admitted nothing', () => {
    const surface = text(renderToStaticMarkup(<ComparePanel topics={[]} />));
    /* Round three, 24 Sept 2026: the same no discovery body, restated to the
       new sentence above. */
    expect(surface).toContain('The run finished without a signal strong enough to show.');
  });

  /* The desk sends no closed evidence summary with a signal, so the briefing
     still withholds. That is the payload, not the envelope: the route now
     reaches the signal and names the field it lacks instead of dating a hold
     against the ingest stamp. */
  test('Briefing reaches the signal and names the missing authority', () => {
    const markup = renderToStaticMarkup(
      <TodayPage topics={[deskRow()]} freshness={amber()} deskDate="2026-09-03" />,
    );
    expect(markup).toContain('data-briefing-state="unavailable"');
    expect(markup).not.toContain('data-briefing-state="stale"');
    expect(text(markup)).toContain('Nothing to show for this signal');
  });
});
