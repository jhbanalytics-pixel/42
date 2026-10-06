/* The source_lab.py 2.2.0 envelope: the inventory builder's catalog and
   sources, plus the funding row and the credit_budget_v2 reading the route
   verifies before it shows anything. */
import {sourceLabInventory} from '../../../../src/ui/__tests__/fixtures/source-lab-inventory.js';

const ENDPOINT = '/api/v2/source-lab';
const CHECKED_AT = '2026-08-30T06:00:00Z';
const LIMITATION = 'Enumeration is not enablement. A positive allowance does not authorize a source or a run.';

function lane(over = {}){
  return {
    credential_lane: 'ogilvy_funded', funding_account: 'ogilvy_albert', activation_stage: 1, opening_balance: '250100',
    current_balance: '250100', month_opening_balance: '250100', monthly_cap: '25000', monthly_ledger_debit: '0',
    monthly_balance_delta: '0', monthly_effective_spend: '0', monthly_remaining: '25000', reserve_floor: '225000',
    reserve_remaining: '25100', stage_cap: '100', run_allowance: '100', attribution_state: 'complete', kill_state: 'passed',
    ledger_through: CHECKED_AT, limitation: LIMITATION,
    ...over,
  };
}

function jhbLane(){
  return lane({
    credential_lane: 'jhb_core', funding_account: 'jhb_analytics', activation_stage: null, opening_balance: null, current_balance: null,
    month_opening_balance: null, monthly_cap: null, monthly_ledger_debit: null, monthly_balance_delta: null, monthly_effective_spend: null,
    monthly_remaining: null, reserve_floor: null, reserve_remaining: null, stage_cap: null, run_allowance: null,
    attribution_state: 'unavailable', kill_state: 'not_applicable', ledger_through: null,
    limitation: 'This credential remains operationally separate and has no funded-lane budget authority.',
  });
}

export function sourceLab({measured = 2, catalogue = 3} = {}){
  const base = sourceLabInventory({measured, catalogue});
  return {
    ...base,
    catalog: {...base.catalog, expectation_version: 'socialcrawl_catalog_2026_08', platform_count: 2},
    sources: base.sources.map((source) => (source.status === 'active' ? {...source, yield_state: 'measured'} : source)),
    funding: {
      balance: '250100', recent_deductions: '0', observed_at: CHECKED_AT, funding_math_status: 'unknown',
      funded_increase_observed: false, selected_top_up_credits: null, monthly_optional_credit_cap: 25000,
      monthly_optional_credits_used: null, optional_calls_enabled: false,
    },
    credit_budget: {
      contract_version: 'credit_budget_v2', state: 'projected', month_start: '2026-09-01', unit: 'vendor_credits',
      lanes: [lane(), jhbLane()], limitation: LIMITATION,
    },
  };
}

export default {
  capability_id: 'coverage_source_lab',
  route: '#/source-lab',
  fixtures: (state) => ({[ENDPOINT]: {
    populated: () => sourceLab(),
    loading: () => ({__hold: true}),
    empty: () => sourceLab({measured: 0, catalogue: 0}),
    failed: () => ({__status: 500, body: {detail: {code: 'catalog_unverified', message: 'The catalog snapshot could not be verified.'}}}),
  }[state]()}),
  gaps: {
    thin: 'source_lab.py 2.2.0 sets quality_state only as measured or unmeasured (source_lab.py:531) and carries no evidence floor, so no thin state can be produced.',
    stale: 'source_lab.py 2.2.0 states checked_at on the catalog but no freshness state or window, so the route cannot declare a snapshot stale with its age.',
    contradictory: 'source_lab.py 2.2.0 carries no claim two sources can disagree on; a catalog inconsistent with its list is a failed verification, not a contradiction.',
  },
};
