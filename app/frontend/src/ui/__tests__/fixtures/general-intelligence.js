export function intelligenceFixture(){
  return {
    contract_version: 'general_cultural_question_v1', request_id: '00000000-0000-4000-8000-000000000001', request_digest: 'a'.repeat(64),
    status: 'partial', resolved_scope: {client_scope_id: 'ogilvy_default', market_scope: ['za'], brand_config_id: null, audience_lens_ids: [], theme_id: null},
    window: {start: '2026-08-23', end: '2026-09-05', closed: true}, as_of: '2026-09-06T10:00:00Z', snapshot_id: 'snapshot_1',
    sections: [{kind: 'answer', claim_ids: ['claim_1']}],
    claims: [{claim_id: 'claim_1', text: 'Repair tutorials recur in the sampled posts.', kind: 'observation', receipt_ids: ['receipt_1'], reading_ids: [], parent_claim_ids: [], support_state: 'source_record', limitations: ['This is a bounded sample.'], falsifier: null}],
    receipts: [{receipt_id: 'receipt_1', citation_label: 'R1', kind: 'content', snapshot_id: 'snapshot_1', market: 'za', source_label: 'Public music and repair post', source_family: 'social', platform: 'instagram', author: 'maker', url: 'https://example.test/post_1', source_row_id: 'post_1', published_at: '2026-09-03T10:00:00Z', collected_at: '2026-09-04T10:00:00Z', excerpt: 'A repair tutorial shared with the community.', reading_ids: [], limitations: [], content_digest: 'b'.repeat(64)}],
    readings: [], limitations: ['Coverage is limited to the sampled records.'], missing_work: ['Compare with further independent sources.'], clarification: null,
    review_required: false, ready_for_downstream: false,
    usage: {status: 'resolved', model_calls: 1, input_tokens: 200, output_tokens: 100, usage_receipt_ids: ['usage_1'], call_receipt_ids: ['call_1'], reservation_ids: ['reservation_1'], reserved_cost_usd: '0.000500', reason: null},
  };
}
