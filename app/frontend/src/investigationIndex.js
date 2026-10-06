/* The investigation entry point, client side.

   Two surfaces sit on the two approved reads: a landing that lists what
   exists, and a framing surface that creates one. Neither invents anything.

   The rule that shapes both is section 7. With no released run the framing
   surface refuses to create and says why. It does not disable a button and
   leave the request assemblable: the request itself cannot be built, because
   the creation call validates run_id as non-empty text only, so an invented
   identity would pass the server's check and leave an investigation resting on
   a run that was never made. */

const CONTRACT_VERSION = 'intelligence_dossier_v1';
const INDEX_VERSION = 'investigation_index_v1';
const SCOPE_VERSION = 'investigation_scope_index_v1';

export const INDEX_STATES = Object.freeze([
  'loading',
  'ready',
  'empty',
  'error',
  'unsupported_contract',
]);

export const FRAMING_STATES = Object.freeze([
  'loading',
  'ready',
  'no_released_run',
  'error',
  'unsupported_contract',
]);

const NO_RUN_REASON =
  'No released run is available for this scope, so an investigation cannot be framed against one yet.';

function supported(payload, version){
  return Boolean(payload) && payload.contract_version === CONTRACT_VERSION
    && payload.resource_version === version;
}

export function buildIndexState({payload, loading = false, error = null} = {}){
  if (loading) return {state: 'loading', investigations: [], totalCount: 0, truncated: false};
  if (error) return {state: 'error', investigations: [], totalCount: 0, truncated: false};
  /* Absent is not empty. Empty says there are no investigations, which is a
     fact about the estate. Absent says we did not find out. */
  if (!payload || typeof payload !== 'object'){
    return {state: 'error', investigations: [], totalCount: 0, truncated: false};
  }
  if (!supported(payload, INDEX_VERSION)){
    return {state: 'unsupported_contract', investigations: [], totalCount: 0, truncated: false};
  }
  if (!Array.isArray(payload.investigations)){
    return {state: 'error', investigations: [], totalCount: 0, truncated: false};
  }
  /* A row with no identity cannot be opened, so offering it would be offering
     a door with no room behind it. */
  const investigations = payload.investigations.filter(
    (item) => item && typeof item.investigation_id === 'string' && item.investigation_id,
  );
  return {
    state: investigations.length ? 'ready' : 'empty',
    investigations,
    totalCount: Number.isInteger(payload.total_count) ? payload.total_count : investigations.length,
    truncated: payload.truncated === true,
  };
}

export function buildFramingState({payload, loading = false, error = null} = {}){
  const nothing = {state: 'error', scope: null, canCreate: false, reason: null};
  if (loading) return {...nothing, state: 'loading'};
  if (error) return nothing;
  if (!payload || typeof payload !== 'object') return nothing;
  if (!supported(payload, SCOPE_VERSION)) return {...nothing, state: 'unsupported_contract'};
  const scopes = Array.isArray(payload.scopes) ? payload.scopes : [];
  const scope = scopes.find((item) => item.client_scope_id === payload.default_scope_id)
    || scopes[0]
    || null;
  /* No scope is unavailable, never a default invented here. The server owns
     which scopes exist and this surface may not decide one for it. */
  if (!scope) return nothing;
  if (!scope.latest_run_id){
    return {state: 'no_released_run', scope, canCreate: false, reason: NO_RUN_REASON};
  }
  return {state: 'ready', scope, canCreate: true, reason: null};
}

export function creationRequestFrom(view, {decisionQuestion, markets, timeHorizonDays} = {}){
  /* Refused here, not merely disabled on screen. A disabled control still
     leaves a request that something else could assemble and send. */
  if (!view || !view.canCreate || !view.scope) throw new Error(NO_RUN_REASON);
  const scope = view.scope;
  /* Checked again here, independently of the state machine. The two guards
     must fail separately: if one is removed the other still refuses, because a
     run identity invented to satisfy a required field is the exact failure the
     answer to section 7 forbids. */
  const run = typeof scope.latest_run_id === 'string' ? scope.latest_run_id.trim() : '';
  if (!run) throw new Error(NO_RUN_REASON);
  const question = typeof decisionQuestion === 'string' ? decisionQuestion.trim() : '';
  if (!question) throw new Error('A decision question is required.');
  const chosen = Array.isArray(markets) ? markets.map((m) => String(m).toLowerCase()) : [];
  if (!chosen.length) throw new Error('At least one market is required.');
  const allowed = new Set(scope.market_scope || []);
  const stray = chosen.filter((market) => !allowed.has(market));
  if (stray.length) throw new Error(`Market outside this scope: ${stray.join(', ')}`);
  const horizon = Number(timeHorizonDays);
  if (!Number.isInteger(horizon) || horizon <= 0) throw new Error('A time horizon is required.');
  /* Every scope field is echoed exactly as the server gave it. The creation
     request refuses unless brand and theme match the configured values, and
     this surface is not the place to guess what they are. */
  return {
    client_scope_id: scope.client_scope_id,
    market_scope: chosen,
    brand_config_id: scope.brand_config_id ?? null,
    audience_lens_ids: [...(scope.audience_lens_ids || [])],
    theme_id: scope.theme_id ?? null,
    run_id: run,
    contract_version: CONTRACT_VERSION,
    decision_question: question,
    time_horizon_days: horizon,
  };
}

export async function createInvestigationFrom(view, input, post){
  const request = creationRequestFrom(view, input);
  const response = await post('/api/v2/investigations', request);
  const sameMembers = (left, right) => Array.isArray(left) && Array.isArray(right)
    && JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
  const valid = response && /^inv_[a-zA-Z0-9_-]+$/.test(response.investigation_id || '')
    && response.status === 'plan_ready'
    && ['contract_version', 'client_scope_id', 'brand_config_id', 'theme_id', 'run_id']
      .every((field) => response[field] === request[field])
    && sameMembers(response.market_scope, request.market_scope)
    && sameMembers(response.audience_lens_ids, request.audience_lens_ids);
  if (!valid) throw new Error('The framed investigation response could not be verified.');
  return response;
}
