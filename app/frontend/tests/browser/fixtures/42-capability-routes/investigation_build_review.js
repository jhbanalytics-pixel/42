/* The Evidence Room reads four resources for one investigation. Claims carry
   the readiness vocabulary; the decision resource holds approved or rejected
   (dossier_resolver.DECISION_STATES) and is absent while review is pending. */
export const INVESTIGATION_ID = 'inv_fixture';
export const ARTIFACT_ID = 'ra_fixture';
/* The server refuses an artifact read without its exact sealed version. */
export const ARTIFACT_VERSION = 'b'.repeat(64);
const BASE = '/api/v2/investigations/' + INVESTIGATION_ID;
export const THIN_GAP = 'second_family: the evidence floor is two independent families and one is present';
export const DISAGREEMENT_IDS = ['ev_2_news'];
export const ARTIFACT_TITLE = 'Weekend repair client read';

export function status(){
  return {
    contract_version: 'intelligence_dossier_v1', investigation_id: INVESTIGATION_ID,
    research_plan: {questions: ['What is changing in weekend repair?'], required_source_families: ['reddit', 'news'], known_gaps: []},
    missing_work: [],
  };
}

export function claim(evidenceState, over = {}){
  return {
    claim_id: 'clm_1', claim_text: 'Repair tutorials recur in the sampled posts.', evidence_state: evidenceState,
    supporting_evidence_ids: ['ev_1_reddit'], opposing_evidence_ids: evidenceState === 'contradictory' ? DISAGREEMENT_IDS : [],
    missing_evidence: evidenceState === 'thin' ? [THIN_GAP] : [], assumptions: [], confidence: null,
    confidence_reason: 'Unavailable', human_review_required: true, ...over,
  };
}

const FAILURE = {__status: 500, body: {detail: {code: 'investigation_store_unavailable', message: 'The investigation store did not answer.'}}};

export default {
  capability_id: 'investigation_build_review',
  route: '#/console?work=brief&investigation=' + INVESTIGATION_ID + '&artifact=' + ARTIFACT_ID + '&artifact_version=' + ARTIFACT_VERSION,
  fixtures: (state) => {
    const decision = state === 'held' ? {decision: null} : {decision: {state: 'approved'}};
    const claims = {
      populated: [claim('ready')], loading: [claim('ready')], empty: [], thin: [claim('thin')],
      contradictory: [claim('contradictory')], failed: [claim('ready')], held: [claim('ready')],
    }[state];
    return {
      [BASE + '/status']: state === 'failed' ? FAILURE : state === 'loading' ? {__hold: true} : status(),
      [BASE + '/claims/read']: {claims},
      [BASE + '/decision/read']: decision,
      [BASE + '/artifacts/' + ARTIFACT_ID + '/read']: {artifact: {artifact_id: ARTIFACT_ID, title: ARTIFACT_TITLE}},
    };
  },
  gaps: {
    stale: 'No producer under src/api emits workspace_snapshot_stale or a snapshot age for an investigation read, so the route cannot state a stale age.',
  },
};
