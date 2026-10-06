/* Desk payloads for the three desk routes, in the envelope /api/desk sends:
   each discovered signal is wrapped as {signal: {...}} and the run facts
   repeat on every row. The evidence, ribbon and audience members reuse the
   validated instrument payload fixtures rather than restating them. */
import {
  DESK_AMBER_FRESHNESS, MEASURED_AUDIENCE, NO_DISCOVERY, READY_RIBBON, READY_SUMMARY, freshFixture,
} from '../../../../src/ui/__tests__/fixtures/instrument-payloads.js';

export const RUN = Object.freeze({
  run_id: 'run_20260912_dynamic_apply_v2_r1', signal_date: '2026-09-12', market_scope: ['za'],
  observation_start: '2026-08-18', observation_end: '2026-08-24',
  observation_method: 'dynamic_source_copy_apply_v1', closed_at: '2026-08-25T06:30:00Z',
});
export const GREEN = Object.freeze({stamp_utc: '2026-09-12T06:30:00Z', age_hours: 2, status: 'green'});
export const THIN_FLOOR = 'One qualifying source family supports this signal. The evidence floor is two independent families.';
export const DISAGREEMENT = 'Forum source and Video source disagree on the direction of repair routines over the window.';
export const UNCHECKED_REASON = 'Source independence has not been checked by the engine.';
const NAMES = ['Repair routine', 'Balcony mix', 'Thrift flip', 'Night market'];

function hex64(index){ return index.toString(16).padStart(64, '0'); }

export function receipt(index){
  return {
    evidence_id: 'ev_' + hex64(index), url: 'https://evidence.invalid/post-' + index, platform: 'reddit',
    source_family: 'forum', author_label: 'maker_' + index, excerpt: 'Repair tutorial ' + index + ' shared with the community.',
    metric_label: null, published_at: '2026-08-2' + (index % 4) + 'T09:00:00Z',
  };
}

export function summaryFor(state){
  const summary = freshFixture(READY_SUMMARY);
  if (state === 'thin'){
    summary.state = 'thin';
    summary.receipts = [summary.receipts[0]];
    summary.independence = {...summary.independence, familyCount: 1};
    summary.direction = {status: 'agree', supportingReceiptIds: ['receipt-forum'], opposingReceiptIds: []};
    summary.limitations = [THIN_FLOOR];
  }
  if (state === 'contradictory'){
    summary.state = 'contradictory';
    summary.receipts[1].direction = 'opposing';
    summary.direction = {status: 'mixed', supportingReceiptIds: ['receipt-forum'], opposingReceiptIds: ['receipt-video']};
    summary.limitations = [DISAGREEMENT];
  }
  if (state === 'held'){
    summary.state = 'unchecked';
    /* The family count stays what the receipts show; only the authority over
       the grouping is unvalidated, which is the hold. */
    summary.independence = {status: 'unvalidated', familyCount: 2, groupingAuthority: null};
    summary.direction = {status: 'agree', supportingReceiptIds: ['receipt-forum', 'receipt-video'], opposingReceiptIds: []};
    summary.limitations = [UNCHECKED_REASON];
  }
  return summary;
}

export function ribbonFor(state){
  const series = freshFixture(READY_RIBBON);
  if (state === 'thin') return [series[0]];
  if (state === 'contradictory'){
    series[1].direction = 'opposing';
    series[1].points = [{at: '2026-08-18T09:00:00Z', value: 0.7}, {at: '2026-08-24T09:00:00Z', value: 0.2}];
    series[1].receiptAnchors = [{receiptId: 'receipt-video', at: '2026-08-24T09:00:00Z', value: 0.2}];
  }
  return series;
}

export function signal(index, state = 'populated', over = {}){
  const evidenceState = state === 'held' ? 'unchecked' : state === 'populated' ? 'ready' : state;
  return {signal: {
    contract_version: 'desk_dynamic_signal_v2', run_id: RUN.run_id, signal_id: 'sig_' + hex64(index),
    signal_date: RUN.signal_date, market: 'za', signal_name: NAMES[index % 4] + (index > 3 ? ' ' + index : ''),
    discovery_mode: 'phrase', evidence_state: evidenceState,
    why_now: 'Two independent families moved against baseline this window.',
    possible_response: 'Show the repair sequence and credit the makers.',
    rival: 'A short-lived promotion could explain the movement.',
    contradiction: 'Video growth slowed on the final day.',
    precedent: {match: 'Earlier repair cultures', difference: 'Current distribution is creator-led.'},
    transfer_limit: 'The precedent predates short-form video.',
    cultural_tension: 'Replacement speed conflicts with repair pride.',
    what_would_change_my_mind: 'A closed rerun with no independent agreement.',
    observation_start: RUN.observation_start, observation_end: RUN.observation_end, observation_method: RUN.observation_method,
    receipts: [receipt(index * 2), receipt(index * 2 + 1)],
    qualities: {velocity: 'moderate', novelty: 'moderate', breadth: 'moderate', independence: 'high', history: 'unmeasured', geo_confidence: 'high', topic_tags: []},
    evidence_summary: summaryFor(state), ribbon_series: ribbonFor(state), audience_basis: freshFixture(MEASURED_AUDIENCE),
    ...over,
  }};
}

export function desk({signals = [], freshness = GREEN, status = 'ready'} = {}){
  return {
    updated: RUN.signal_date, topics: [], lexicon: [], freshness,
    dynamic_discovery: {contract_version: 'desk_dynamic_signal_v2', status, requested_market: 'za', run: {...RUN}, signals, error: null},
  };
}

export function noDiscoveryDesk(freshness = GREEN){
  return {updated: RUN.signal_date, topics: [], lexicon: [], freshness, dynamic_discovery: freshFixture(NO_DISCOVERY)};
}

/* The desk read fails with a coded detail, the shape apiFailureDetails reads a code from. */
export const DESK_FAILURE = Object.freeze({__status: 500, body: {detail: {code: 'desk_read_failed', message: 'The completed run store did not answer.'}}});

export const DESK_STATES = Object.freeze({
  populated: () => desk({signals: [signal(1), signal(2), signal(3)]}),
  loading: () => ({__hold: true}),
  empty: () => noDiscoveryDesk(),
  thin: () => desk({signals: [signal(1, 'thin'), signal(2, 'thin')]}),
  stale: () => desk({signals: [], freshness: freshFixture(DESK_AMBER_FRESHNESS)}),
  contradictory: () => desk({signals: [signal(1, 'contradictory'), signal(2, 'contradictory')]}),
  failed: () => DESK_FAILURE,
  held: () => desk({signals: [signal(1, 'held'), signal(2, 'held')]}),
});
