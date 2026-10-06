/* Synthetic paging fixtures use the producer's nested catalogue envelope.
   Snapshot time does not establish a content observation window. */
const CHECKED_AT = '2026-08-30T06:00:00Z';

function hex(index){
  return index.toString(16).padStart(64, '0');
}

export function measuredSource(index){
  return {
    endpoint_id: `endpoint_${hex(index)}`, vendor: 'socialcrawl', platform: 'reddit', source_family: 'socialcrawl',
    route_path: `/v1/reddit/search-${index}`, route_role: 'evidence', status: 'active', market: 'za',
    official_capability: `Public posts, route ${index}`,
    official_credits: 2,
    calls: 2, rows: 100 + index, integrity: 0.9, geo_precision: 1,
    last_checked_at: CHECKED_AT, last_success_at: '2026-08-30T06:10:00Z',
    downstream_consumers: ['evidence_receipts'], quality_state: 'qualifying',
    kill_test_state: 'passed', kill_test_result: null, blocking_reason: null,
  };
}

export function catalogueSource(index){
  return {
    endpoint_id: `endpoint_${hex(100000 + index)}`, vendor: 'socialcrawl', platform: 'tiktok', resource: 'profile',
    http_method: 'GET', route_path: `/v1/tiktok/profile-${index}`, route_role: 'inventory', source_family: 'socialcrawl',
    market: null, status: 'inventory_only', official_capability: `Catalogue route ${index}.`, official_parameters: ['handle'],
    official_credits: 1, official_credits_label: '1 credit', official_archetype: 'lookup',
    catalog_paginated: false, catalog_metered: true, cache_ttl_seconds: 3600, delivery: null,
    docs_url: 'https://socialcrawl.dev/docs',
    calls: null, credits: null, rows: null, integrity: null, geo_precision: null, unique_lift: null,
    last_success_at: null, downstream_consumers: [],
    blocking_reason: null, kill_test_result: null, review_date: null,
    quality_state: 'unmeasured', cost_state: 'catalog_only', yield_state: 'unmeasured', kill_test_state: 'not_run',
    last_checked_at: CHECKED_AT,
  };
}

export function sourceLabInventory({measured = 60, catalogue = 463} = {}){
  const total = measured + catalogue;
  const sources = [];
  let measuredLeft = measured;
  let catalogueLeft = catalogue;
  for (let index = 0; index < total; index += 1){
    /* One measured row in every nine until they run out, so the measured set
       is spread through the list rather than stacked at the top. */
    const takeMeasured = measuredLeft > 0 && (catalogueLeft === 0 || index % 9 === 0);
    if (takeMeasured){ sources.push(measuredSource(measured - measuredLeft + 1)); measuredLeft -= 1; }
    else { sources.push(catalogueSource(catalogue - catalogueLeft + 1)); catalogueLeft -= 1; }
  }
  return {
    contract_version: '2.2.0', resource_version: 'source_lab_inventory_v2',
    catalog: {catalog_digest: 'c'.repeat(64), checked_at: CHECKED_AT, completeness_state: 'verified_snapshot', route_count: total},
    run_id: 'source_lab_2026-08-30', sources,
  };
}
