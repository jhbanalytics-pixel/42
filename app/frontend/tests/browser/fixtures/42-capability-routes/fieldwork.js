/* fieldwork_workspace_v2 payloads and the Source Lab projection the route
   reads beside them. Every research row is unchecked, because the producer
   refuses any other readiness. */
import {sourceLabInventory} from '../../../../src/ui/__tests__/fixtures/source-lab-inventory.js';
import {REQUEST_ID} from './_question.js';

const READ = '/api/internal/v2/fieldwork/read';
const SOURCE_LAB = '/api/v2/source-lab';

export function counts(over = {}){
  return {planned: 0, running: 0, unconfirmed: 0, complete: 0, partial: 0, needs_clarification: 0, refused: 0, held: 0, killed: 0, unavailable: 0, ...over};
}

export function row(over = {}){
  return {
    operation_id: 'gq_00000000000000000000000000000001', kind: 'research', operation_type: 'general_question',
    entity_id: REQUEST_ID, title: 'Retained question operation', status: 'complete', market_scope: ['za'],
    evidence_readiness: 'unchecked', updated_at: '2026-09-12T04:00:00Z', gaps: [],
    next_operation: {action: 'open_question_request', entity_id: REQUEST_ID, label: 'Open question', available: true, reason: null},
    ...over,
  };
}

export function freshness(state){
  return {
    state, source_lab_checked_at: '2026-08-30T06:00:00Z', investigations_updated_at: '2026-09-12T04:00:00Z',
    oldest_operation_updated_at: state === 'stale' ? '2026-08-20T04:00:00Z' : '2026-09-12T04:00:00Z',
    reasons: state === 'stale' ? ['oldest_operation_outside_window'] : [],
  };
}

export function budget(state, over = {}){
  return {state, funding_lane: null, credit_ceiling: null, credits_reserved: null, credits_used: null, credits_remaining: null, reserve_floor: null, observed_at: null, reasons: [], ...over};
}

export function payload({workspace_state = 'ready', rows = [], fresh = freshness('current'), lane = budget('not_required')} = {}){
  const byStatus = counts();
  rows.forEach((item) => { byStatus[item.status] += 1; });
  return {
    contract_version: 'fieldwork_workspace_v2', workspace_state, source_operations: [], research_operations: rows,
    research_operation_count: rows.length, research_operations_truncated: false,
    operation_summary: {count_state: 'complete', total: rows.length, known_total: rows.length, known_by_status: byStatus},
    operation_coverage: {}, budget_summary: lane, freshness: fresh, unavailable_inputs: [],
  };
}

/* Measured routes carry their observation counts, so the snapshot has a
   collected volume to state. The builder is reused as it is and decorated. */
export function measuredInventory(){
  const inventory = sourceLabInventory({measured: 2, catalogue: 3});
  return {...inventory, sources: inventory.sources.map((source) => (source.status === 'active' ? {...source, yield_state: 'measured'} : source))};
}

export const HELD_GAP = 'Held: the ogilvy_funded lane is exhausted for this window; resuming is a separate approval.';

export default {
  capability_id: 'fieldwork',
  route: '#/fieldwork',
  fixtures: (state) => ({
    [SOURCE_LAB]: measuredInventory(),
    [READ]: {
      populated: () => payload({rows: [row()]}),
      loading: () => ({__hold: true}),
      empty: () => payload({workspace_state: 'empty'}),
      stale: () => payload({rows: [row()], fresh: freshness('stale')}),
      failed: () => ({__status: 500, body: {detail: {code: 'fieldwork_read_failed', message: 'The fieldwork projection could not be read.'}}}),
      held: () => payload({
        workspace_state: 'budget_exhausted',
        lane: budget('exhausted', {funding_lane: 'ogilvy_funded', credit_ceiling: 25000, credits_reserved: 0, credits_used: 25000, credits_remaining: 0, reserve_floor: 225000, observed_at: '2026-09-12T04:00:00Z'}),
        rows: [row({status: 'held', gaps: [HELD_GAP], next_operation: {action: 'open_question_request', entity_id: REQUEST_ID, label: 'Open question', available: false, reason: 'Held until the funded lane is approved.'}})],
      }),
    }[state](),
  }),
  gaps: {
    thin: 'fieldwork_workspace_v2 projects every research row as unchecked; the producer refuses a thin readiness, so no thin state can be produced.',
    contradictory: 'fieldwork_workspace_v2 projects every research row as unchecked; the producer refuses a contradictory readiness, so no contradictory state can be produced.',
  },
};
