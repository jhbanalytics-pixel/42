/* Stored question detail payloads (general_question_detail_v1) wrapping the
   general_cultural_question_v1 reply the existing fixture builder produces. */
import {intelligenceFixture} from '../../../../src/ui/__tests__/fixtures/general-intelligence.js';

export const REQUEST_ID = '00000000-0000-4000-8000-000000000001';
export const ROUTE = '#/console?work=ask&request=' + REQUEST_ID;
export const ENDPOINT = '/api/internal/v2/fieldwork/question';
export const THIN_FLOOR = 'One source record supports this claim. The evidence floor is two independent records.';
export const DISAGREEMENT = 'Public music and repair post (R1) and Local news record (R2) disagree on whether repair tutorials are rising.';
export const QUESTION_FAILURE = Object.freeze({__status: 500, body: {detail: {code: 'question_store_unavailable', message: 'The stored question could not be read from the store.'}}});

export function detail(observedState, intelligence){
  return {
    contract_version: 'general_question_detail_v1', request_id: REQUEST_ID, observed_state: observedState,
    question: 'Are repair tutorials rising in the sampled posts?', history: [], selected_market: 'za',
    requested_window: {start: '2026-08-23', end: '2026-09-05'},
    response: intelligence ? {answer: 'Stored answer', sources: [], intelligence} : null, window_from_plan: !!intelligence,
    reserved_microusd: 100000, missing_work: intelligence ? [] : ['execution_unconfirmed'],
  };
}

function newsReceipt(){
  return {
    receipt_id: 'receipt_2', citation_label: 'R2', kind: 'content', snapshot_id: 'snapshot_1', market: 'za',
    source_label: 'Local news record', source_family: 'news', platform: 'news', author: null,
    url: 'https://example.test/news_2', source_row_id: 'news_2', published_at: '2026-09-02T13:46:00Z',
    collected_at: '2026-09-04T10:00:00Z', excerpt: 'Repair tutorials fell out of the weekend listings.',
    reading_ids: [], limitations: [], content_digest: 'c'.repeat(64),
  };
}

export function reply(state){
  const value = intelligenceFixture();
  if (state === 'populated'){ value.status = 'complete'; value.ready_for_downstream = true; }
  if (state === 'empty'){
    Object.assign(value, {status: 'unavailable', sections: [], claims: [], receipts: [], readings: [], limitations: [], missing_work: ['no_matching_evidence'], ready_for_downstream: false});
  }
  if (state === 'thin'){ value.claims[0].limitations = [THIN_FLOOR]; value.limitations = [THIN_FLOOR]; }
  if (state === 'contradictory'){
    value.receipts.push(newsReceipt());
    value.claims.push({claim_id: 'claim_2', text: 'Repair tutorials fell out of the weekend listings.', kind: 'observation', receipt_ids: ['receipt_2'], reading_ids: [], parent_claim_ids: [], support_state: 'source_record', limitations: [DISAGREEMENT], falsifier: null});
    value.sections[0].claim_ids.push('claim_2');
    value.limitations = [DISAGREEMENT];
  }
  if (state === 'held'){
    value.status = 'partial';
    value.ready_for_downstream = false;
    value.usage = {...value.usage, status: 'unresolved', reason: 'metering_persistence_failed'};
  }
  return value;
}

export const QUESTION_STATES = Object.freeze({
  populated: () => detail('complete', reply('populated')),
  loading: () => ({__hold: true}),
  empty: () => detail('unavailable', reply('empty')),
  thin: () => detail('partial', reply('thin')),
  contradictory: () => detail('partial', reply('contradictory')),
  failed: () => QUESTION_FAILURE,
  held: () => detail('held', reply('held')),
});
