/* The boundary between an internal investigation and a client-facing read.

   Two rules do most of the work here.

   Projection is allowlist based: material reaches a Client Read only when it
   is approved, ready, selected and cited. Everything else is excluded.

   Exclusion is not deletion. Every excluded resource is retained with the
   section it was excluded from and the reason why, because a dossier that
   quietly drops its own disagreement reads as consensus that never existed.
   A cleaner artifact must mean a narrower approved projection, not hidden
   uncertainty. */

/* The nine sections, in the only order they may appear. */
export const CLIENT_READ_SECTIONS = Object.freeze([
  'what_changed',
  'why_now',
  'evidence',
  'contradictions',
  'market_differences',
  'precedent',
  'cultural_tension',
  'possible_response',
  'what_would_change_my_mind',
]);

/* Claim kinds a client may see as a finding. A limitation is handled
   separately, because a limitation is the one place thin, contradictory or
   unchecked evidence is admissible: saying so is the point of it. */
const FINDING_KINDS = Object.freeze(new Set(['observation', 'interpretation', 'recommendation']));
const LIMITATION_KINDS = Object.freeze(new Set(['limitation', 'abstention']));

/* Never projected under any status. These are internal reasoning and internal
   machinery, not evidence about the world. */
const NEVER_PROJECTED_KINDS = Object.freeze(new Set([
  'model_proposal',
  'model_memory',
  'internal_assumption',
  'diffusion_shadow',
]));

const APPROVED = 'approved';
const READY = 'ready';

/* Why one evidence date may not pass the cutoff gate, or null. A value the
   gate cannot read must refuse, not pass: undated is unknown, a malformed
   cutoff is no boundary at all, and neither may read as open. Mirrors the
   server gate in intelligence_dossier.py exactly. */
function validCalendarDay(value){
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
  const stamp = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === value;
}

function evidenceDatingReason(value, cutoff){
  if (typeof value !== 'string' || !value){
    return 'evidence carries no publication date, so it cannot be checked against the cutoff';
  }
  const limit = typeof cutoff === 'string' && validCalendarDay(cutoff) ? Date.parse(`${cutoff}T00:00:00Z`) + 86400000 : NaN;
  if (!Number.isFinite(limit)){
    return 'investigation cutoff is unavailable, so evidence dating cannot be checked';
  }
  const shape = /^\d{4}-\d{2}-\d{2}(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?$/.exec(value);
  const valid = shape && validCalendarDay(value.slice(0, 10)) && (!shape[1] || (Number(shape[1]) < 24 && Number(shape[2]) < 60 && Number(shape[3] || 0) < 60));
  const utc = value.length === 10 ? `${value}T00:00:00Z` : /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`;
  const at = valid ? Date.parse(utc) : NaN;
  if (!Number.isFinite(at)){
    return 'evidence dating is unreadable, so it cannot be checked against the cutoff';
  }
  if (at >= limit) return 'evidence is dated after the investigation cutoff';
  return null;
}

function claimExclusionReason(item){
  if (typeof item.kind !== 'string') return 'claim kind is unrecognized';
  if (NEVER_PROJECTED_KINDS.has(item.kind)) return 'kind is never projected to a client';
  if (item.status !== APPROVED) return `claim status is ${['pending', 'rejected', 'superseded', 'disputed'].includes(item.status) ? item.status : 'unrecognized'}, not approved`;
  if (item.selected !== true) return 'claim is not selected for the artifact';
  if (typeof item.text !== 'string' || !item.text.trim()) return 'claim has no usable text';
  if (!Array.isArray(item.citations) || !item.citations.length || item.citations.some(id => typeof id !== 'string' || !id.trim())) return 'claim carries no citation';
  if (LIMITATION_KINDS.has(item.kind)) return null;
  if (!FINDING_KINDS.has(item.kind)) return 'kind is not a client claim kind';
  if (item.evidence_state !== READY){
    /* A finding on thin, contradictory or unchecked evidence is exactly the
       thing that must not be stated as a finding. It may still be said, but
       only as a limitation, in its own words. */
    return `evidence state is ${['thin', 'contradictory', 'unchecked', 'withheld'].includes(item.evidence_state) ? item.evidence_state : 'unrecognized'}, which is admissible only as a limitation`;
  }
  return null;
}

export function projectClientRead(source){
  const dossier = source || {};
  const cutoff = dossier.cutoff;
  const claims = [];
  const excluded = [];
  const {claimIds, evidenceIds, eligible} = dossierIdentities(dossier);

  for (const item of dossier.claims || []){
    const reason = claimExclusionReason(item) || (claimIds.get(item.claim_id) !== 1 ? 'claim identity is missing or ambiguous' : null);
    if (reason){
      excluded.push({
        claim_id: typeof item.claim_id === 'string' ? item.claim_id : null,
        kind: typeof item.kind === 'string' ? item.kind : null,
        status: typeof item.status === 'string' ? item.status : null,
        excluded_from: 'client_read',
        reason,
      });
      continue;
    }
    claims.push({
      claim_id: item.claim_id,
      kind: item.kind,
      text: item.text,
      citations: [...(item.citations || [])],
    });
  }

  const cited = new Set(claims.flatMap((item) => item.citations));
  const evidence = [];
  for (const item of dossier.evidence || []){
    if (!cited.has(item.evidence_id)) continue;
    if (item.client_citable !== true){
      excluded.push({
        evidence_id: item.evidence_id,
        excluded_from: 'client_read',
        reason: 'evidence is not client citable',
      });
      continue;
    }
    if (item.in_scope !== true){
      excluded.push({
        evidence_id: item.evidence_id,
        excluded_from: 'client_read',
        reason: 'evidence is outside the investigation scope',
      });
      continue;
    }
    const datingReason = evidenceDatingReason(item.published_at, cutoff);
    if (datingReason){
      excluded.push({
        evidence_id: item.evidence_id,
        excluded_from: 'client_read',
        reason: datingReason,
      });
      continue;
    }
    if (evidenceIds.get(item.evidence_id) !== 1){
      excluded.push({evidence_id: item.evidence_id, excluded_from: 'client_read', reason: 'evidence identity is missing or ambiguous'});
      continue;
    }
    evidence.push({
      evidence_id: item.evidence_id,
      published_at: item.published_at,
    });
  }

  const resolvedClaims = claims.filter(item => {
    if (citationsResolve(item, eligible)) return true;
    excluded.push({claim_id: item.claim_id, kind: item.kind, excluded_from: 'client_read', reason: 'claim citations do not resolve to eligible evidence'});
    return false;
  });
  const retained = new Set(resolvedClaims.flatMap(item => item.citations));

  /* Only the allowlisted shape leaves this function. Internal graph scores,
     model memory, digests and principals are not filtered out downstream: they
     are never placed in the projection to begin with. */
  return {
    investigation_id: dossier.investigation_id,
    sections: [...CLIENT_READ_SECTIONS],
    claims: resolvedClaims,
    evidence: evidence.filter(item => retained.has(item.evidence_id)),
    excluded,
  };
}

function dossierIdentities(dossier){
  const count = (items, key) => {
    const ids = new Map();
    for (const item of items || []) if (typeof item[key] === 'string' && item[key].trim()) ids.set(item[key], (ids.get(item[key]) || 0) + 1);
    return ids;
  };
  const claimIds = count(dossier.claims, 'claim_id');
  const evidenceIds = count(dossier.evidence, 'evidence_id');
  const eligible = new Set((dossier.evidence || []).filter(item => evidenceIds.get(item.evidence_id) === 1 && item.client_citable === true && item.in_scope === true && evidenceDatingReason(item.published_at, dossier.cutoff) === null).map(item => item.evidence_id));
  return {claimIds, evidenceIds, eligible};
}

function citationsResolve(claim, eligible){
  return Array.isArray(claim.citations) && claim.citations.length > 0
    && claim.citations.every(id => typeof id === 'string' && eligible.has(id))
    && new Set(claim.citations).size === claim.citations.length;
}

export function computeArtifactReadiness(source){
  const dossier = source || {};
  const failed = [];
  const {claimIds, eligible} = dossierIdentities(dossier);

  if (typeof dossier.concise_answer !== 'string' || !dossier.concise_answer.trim()) failed.push('concise_answer');
  if (dossier.decision !== APPROVED) failed.push('decision');

  const selected = (dossier.claims || []).filter(item => !NEVER_PROJECTED_KINDS.has(item.kind) && item.selected === true);
  if (!selected.length) failed.push('claim_not_cited');
  for (const item of selected){
    if (item.status !== APPROVED && !failed.includes('claim_not_approved')){
      failed.push('claim_not_approved');
    }
    if (!citationsResolve(item, eligible) && !failed.includes('claim_not_cited')){
      failed.push('claim_not_cited');
    }
    if (
      ((!FINDING_KINDS.has(item.kind) && !LIMITATION_KINDS.has(item.kind))
        || typeof item.text !== 'string' || !item.text.trim()
        || (FINDING_KINDS.has(item.kind) && item.evidence_state !== READY)
        || claimIds.get(item.claim_id) !== 1)
      && !failed.includes('claim_not_ready')
    ){
      failed.push('claim_not_ready');
    }
  }

  for (const item of dossier.relationships || []){
    if (item.status !== APPROVED && !failed.includes('relationship_not_approved')){
      failed.push('relationship_not_approved');
    }
  }

  for (const item of dossier.precedents || []){
    if (!validCalendarDay(item.occurred_on) || !validCalendarDay(dossier.cutoff) || item.occurred_on >= dossier.cutoff){
      if (!failed.includes('precedent_not_earlier_than_cutoff')){
        failed.push('precedent_not_earlier_than_cutoff');
      }
    }
    const stated = value => Array.isArray(value) && value.length > 0 && value.every(text => typeof text === 'string' && text.trim());
    const differences = stated(item.material_differences);
    const nontransferable = stated(item.nontransferable);
    if ((!differences || !nontransferable) && !failed.includes('precedent_missing_transfer_limits')){
      /* A precedent without its material differences and its nontransferable
         lessons is a resemblance, not a precedent, and resemblance is how a
         wrong lesson travels. */
      failed.push('precedent_missing_transfer_limits');
    }
  }

  /* Fail closed: a question that does not say blocking === false is not
     known safe, and unknown must not read as open. */
  if ((dossier.unanswered_questions || []).some((item) => item.blocking !== false)){
    failed.push('blocking_question');
  }

  if (!failed.length) return {state: 'client_ready', failed: []};
  /* Named, never softened. A blocked artifact is not one awaiting polish. */
  const approvalOnly = failed.every(
    (reason) => reason === 'decision' || reason === 'claim_not_approved'
      || reason === 'relationship_not_approved',
  );
  return {state: approvalOnly ? 'approval_required' : 'blocked', failed};
}

/* The review and export commands, built exactly as the server contract states
   them. Every builder refuses rather than sends when a field it needs is
   missing, because a command the server would refuse is still a command that
   left the page, and a refusal the reviewer can read here is better than one
   they have to decode from the server. */
export const DOSSIER_REVIEW_CONTRACT = 'dossier_review_v1';
export const DOSSIER_ARTIFACT_CONTRACT = 'dossier_artifact_v1';
export const SUPPORT_VERDICTS = Object.freeze(['supported', 'partial', 'contradictory', 'unsupported']);
const REVIEW_RESOURCES = Object.freeze(new Set(['claim', 'relationship', 'artifact']));
const REVIEW_ACTIONS = Object.freeze(new Set(['approve', 'reject', 'submit']));
const DIGEST = /^[0-9a-f]{64}$/;

function requiredText(value, what){
  if (typeof value !== 'string' || !value.trim()) throw new Error(`A ${what} is required.`);
  return value;
}

function requiredVersion(value, what){
  if (typeof value !== 'string' || !DIGEST.test(value)) throw new Error(`The exact ${what} is not known, so nothing was sent.`);
  return value;
}

export function selectionCommand({dossierVersion, selectedClaimIds} = {}){
  const ids = [];
  for (const id of selectedClaimIds || []){
    if (typeof id === 'string' && id.trim() && !ids.includes(id)) ids.push(id);
  }
  return {
    contract_version: DOSSIER_REVIEW_CONTRACT,
    expected_dossier_version: requiredVersion(dossierVersion, 'dossier version'),
    selected_claim_ids: ids,
  };
}

export function reviewCommand({dossierVersion, resource, resourceId, resourceVersion, action, expectedState, idempotencyKey, supportReview = null} = {}){
  if (!REVIEW_RESOURCES.has(resource)) throw new Error('This item cannot be reviewed.');
  if (!REVIEW_ACTIONS.has(action)) throw new Error('This review step is not one the desk knows.');
  /* A claim is reviewed with its support assessment and nothing else is. The
     server refuses either mistake, and so does this page. */
  if (resource === 'claim' && (supportReview === null || typeof supportReview !== 'object')) throw new Error('A claim review needs a verdict, receipts and a note.');
  if (resource !== 'claim' && supportReview !== null) throw new Error('Only a claim review carries a verdict.');
  return {
    contract_version: DOSSIER_REVIEW_CONTRACT,
    dossier_version: requiredVersion(dossierVersion, 'dossier version'),
    resource,
    resource_id: requiredText(resourceId, 'resource'),
    resource_version: requiredVersion(resourceVersion, 'version of this item'),
    action,
    expected_state: requiredText(expectedState, 'current state'),
    idempotency_key: requiredText(idempotencyKey, 'request key'),
    support_review: supportReview,
  };
}

export function claimReviewCommand({verdict, receiptIds, note, claimId, claimVersion, ...rest} = {}){
  if (!SUPPORT_VERDICTS.includes(verdict)) throw new Error('Choose a verdict: supported, partial, contradictory or unsupported.');
  const receipts = [];
  for (const id of receiptIds || []) if (typeof id === 'string' && id.trim() && !receipts.includes(id)) receipts.push(id);
  /* Support is shown by receipts. A rejection, or a verdict that nothing
     supports the claim, may rest on none, and the server accepts that. */
  if (!receipts.length && rest.action !== 'reject' && verdict !== 'unsupported') throw new Error('Choose at least one receipt the verdict rests on.');
  if (typeof note !== 'string' || !note.trim()) throw new Error('Write a note that says why.');
  return reviewCommand({...rest, resource: 'claim', resourceId: claimId, resourceVersion: claimVersion,
    supportReview: {verdict, receipt_ids: receipts, note: note.trim()}});
}

export function artifactPrepareCommand({dossierVersion} = {}){
  return {
    contract_version: DOSSIER_ARTIFACT_CONTRACT,
    dossier_version: requiredVersion(dossierVersion, 'dossier version'),
    format: 'html',
  };
}

/* One key per press. Should the same request reach the server twice, the
   server answers the second with the decision it already recorded instead of
   writing another. Printable ASCII within the server's length limit. */
export function reviewIdempotencyKey({resource, resourceId, action, nonce} = {}){
  const tail = typeof nonce === 'string' && nonce ? nonce : Math.random().toString(36).slice(2, 12);
  return `${resource}:${String(resourceId).slice(0, 80)}:${action}:${tail}`.replace(/[^\x21-\x7e]/g, '_').slice(0, 200);
}

/* Every refusal the review endpoints can give, in plain words. The code stays
   available to the page for a Details disclosure, never on its own. */
const REFUSAL_WORDS = Object.freeze({
  review_request_invalid: 'The desk could not send this step as it stands. Reload the dossier and try again.',
  review_role_required: 'Your reviewer role does not allow this step. Sign in as a reviewer who holds that role, or ask one to do it.',
  scope_invalid: 'This investigation is not available to you, so nothing was recorded.',
  review_version_conflict: 'The dossier changed after you opened it, so an earlier approval no longer applies. The latest version has been reloaded and needs a fresh approval.',
  review_authority_unavailable: 'Review is switched off on this desk right now, so nothing was recorded. Try again later.',
  review_storage_unavailable: 'The review store could not be reached, so nothing was recorded. Try again in a moment.',
  artifact_citation_missing: 'A selected finding has no usable citation a client could follow, so nothing was prepared. Fix the selection or its receipts first.',
  artifact_approval_required: 'This exact version is not approved for the client, so it cannot be opened or downloaded as a Client Read.',
  client_purpose_review_override_refused: 'The client purpose for this investigation is prohibited, and no role can override it. Nothing was prepared.',
  identity_assets_unavailable: 'The brand assets this export needs are not available, so nothing was prepared. Try again later.',
  workspace_unavailable: 'The workspace could not be read right now. Try again in a moment.',
  workspace_request_invalid: 'The desk sent a request the service could not read. Reload the page and try again.',
});

/* Words that depend on the step a refusal answers. */
const STEP_REFUSAL_WORDS = Object.freeze({
  prepare: Object.freeze({
    review_storage_unavailable: 'The export could not be created, so nothing was prepared. Try again in a moment.',
  }),
});

export function reviewRefusal(error, {step} = {}){
  const code = error && typeof error.code === 'string' ? error.code : null;
  if (error && error.auth) return {code: 'passcode', words: 'Your passcode session has ended. Enter the passcode again to carry on.'};
  const stepWords = step && STEP_REFUSAL_WORDS[step];
  if (code && stepWords && stepWords[code]) return {code, words: stepWords[code]};
  if (code && REFUSAL_WORDS[code]) return {code, words: REFUSAL_WORDS[code]};
  if (error && error.status === 403) return {code, words: 'This step is not open to you here, so nothing was recorded.'};
  if (error && error.status === 409) return {code, words: 'This step could not go through as things stand, so nothing was recorded. Reload the dossier to see where it is.'};
  if (error && error.status === 503) return {code, words: 'The service could not complete this step right now, so nothing was recorded. Try again in a moment.'};
  if (error && error.status) return {code, words: 'The step did not go through, and the service gave no reason this desk can describe. Nothing was recorded.'};
  /* Builders refuse in words already; those are shown as they are. */
  if (error && !code && typeof error.message === 'string' && /^[A-Z].*\.$/.test(error.message)) return {code: null, words: error.message};
  return {code, words: 'The desk could not be reached, so nothing was recorded. Check the connection and try again.'};
}

/* Refusals during reviewer sign in, in sign in words. The server's refusal
   code for a failed login is the generic request code, and the check that
   failed travels as its message, so the message picks the words. */
const SIGN_IN_REASON_WORDS = Object.freeze({
  login_expired: 'The sign in took too long and has lapsed. Start sign in again.',
  login_unknown: 'This sign in is no longer known to the desk, start sign in again.',
  login_mismatch: 'The sign in did not match the one this page started, so it was not accepted. Start sign in again.',
  login_replayed: 'That sign in was already used, so it was not accepted again. Start sign in again.',
  csrf_mismatch: 'The sign in could not be checked against this page, so it was not accepted. Reload the page and sign in again.',
  csrf_missing: 'The sign in could not be checked against this page, so it was not accepted. Reload the page and sign in again.',
  origin_mismatch: 'The sign in could not be checked against this page, so it was not accepted. Reload the page and sign in again.',
});

export const SIGN_IN_NOT_ENROLLED = 'This account is not enrolled as a reviewer yet. Ask the desk owner to add it, then sign in again.';
export const SIGN_IN_WRONG_DOMAIN = 'This account is not on the reviewer organisation domain, so it cannot sign in as a reviewer. Sign in with your organisation account.';

export function signInRefusal(error){
  if (error && error.auth) return reviewRefusal(error);
  const code = error && typeof error.code === 'string' ? error.code : null;
  const reason = error && typeof error.message === 'string' ? error.message : '';
  if (code === 'review_authority_unavailable') return {code, words: 'Reviewer sign in is switched off on this desk right now. Try again later.'};
  if (code === 'review_request_invalid'){
    return {code, words: SIGN_IN_REASON_WORDS[reason] || 'Google sign in could not be verified, so you are not signed in. Start sign in again.'};
  }
  if (code === 'review_role_required' || (error && error.status === 403)){
    return {code, words: reason === 'hosted_domain_mismatch' ? SIGN_IN_WRONG_DOMAIN : SIGN_IN_NOT_ENROLLED};
  }
  if (error && error.status === 503) return {code, words: 'Reviewer sign in is not available right now. Try again in a moment.'};
  if (error && error.status) return {code, words: 'The sign in did not go through, and the service gave no reason this desk can describe. Start sign in again.'};
  /* A sentence this page wrote itself is kept; anything else with no status
     never reached the desk. */
  if (error && !code && typeof error.message === 'string' && /^[A-Z].*\.$/.test(error.message)) return {code: null, words: error.message};
  return {code, words: 'The sign in could not reach the desk. Check the connection and start sign in again.'};
}

const WORKING_FINDING_KINDS = Object.freeze(['observation', 'interpretation', 'recommendation']);

function contradictingIds(claim){
  const ids = claim.contradicting_evidence_ids || claim.opposing_evidence_ids || claim.challenge_evidence_ids || [];
  return Array.isArray(ids) ? ids.filter((id) => typeof id === 'string' && id) : [];
}

function internalReason(claim){
  if (claim.status === 'rejected') return 'Internal only: rejected in review, withheld from the client.';
  if (claim.status !== 'approved') return 'Internal only: awaiting review, withheld from the client.';
  if (claim.selected !== true) return 'Internal only: not selected for the client.';
  return null;
}

/* The internal working view of one exact dossier version. Pending and rejected
   material stays here, labelled as internal; the Client Read is still only
   what projectClientRead admits. */
export function buildDossierWorkingModel(working){
  const source = working || {};
  const projection = source.projection || {};
  const versions = source.resource_versions || {};
  const claimVersions = versions.claim || {};
  const linkVersions = versions.relationship || {};
  const claims = (projection.claims || []).filter((item) => item && typeof item.claim_id === 'string').map((item) => ({
    claimId: item.claim_id,
    kind: item.kind,
    text: typeof item.text === 'string' ? item.text : '',
    status: item.status,
    selected: item.selected === true,
    evidenceState: item.evidence_state,
    citations: Array.isArray(item.citations) ? item.citations.filter((id) => typeof id === 'string') : [],
    contradicting: contradictingIds(item),
    version: typeof claimVersions[item.claim_id] === 'string' ? claimVersions[item.claim_id] : null,
    internalReason: internalReason(item),
  }));
  const relationships = (projection.relationships || []).filter((item) => item && typeof item.relationship_id === 'string').map((item) => ({
    relationshipId: item.relationship_id,
    parentClaimId: item.parent_claim_id,
    claimId: item.claim_id,
    status: item.status,
    version: typeof linkVersions[item.relationship_id] === 'string' ? linkVersions[item.relationship_id] : null,
  }));
  const readiness = computeArtifactReadiness(projection);
  const holds = [];
  const blocking = (projection.unanswered_questions || []).filter((item) => item && item.blocking !== false);
  if (blocking.length) holds.push(`${blocking.length === 1 ? 'An open question still blocks' : `${blocking.length} open questions still block`} this from going to the client.`);
  if (readiness.failed.includes('claim_not_cited')) holds.push('A selected finding has no citation a client could follow.');
  if (readiness.failed.includes('claim_not_ready')) holds.push('A selected finding rests on evidence that is not ready.');
  if (readiness.failed.includes('concise_answer')) holds.push('No answer has been approved yet.');
  if (readiness.failed.some((gate) => gate.startsWith('precedent_'))) holds.push('A precedent does not yet state its limits.');
  if (readiness.failed.includes('claim_not_approved')) holds.push('A selected claim is still awaiting review.');
  if (readiness.failed.includes('relationship_not_approved')) holds.push('A link between claims is still awaiting review.');
  const artifacts = {current: [], historical: []};
  for (const item of source.artifacts || []){
    if (!item || typeof item.artifact_id !== 'string') continue;
    const entry = {artifactId: item.artifact_id, artifactVersion: item.artifact_version, dossierVersion: item.dossier_version, state: item.state};
    (item.dossier_version === source.dossier_version ? artifacts.current : artifacts.historical).push(entry);
  }
  return {
    investigationId: source.investigation_id,
    dossierVersion: source.dossier_version,
    conciseAnswer: typeof projection.concise_answer === 'string' ? projection.concise_answer : null,
    findings: claims.filter((item) => WORKING_FINDING_KINDS.includes(item.kind)),
    limitations: claims.filter((item) => LIMITATION_KINDS.has(item.kind)),
    relationships,
    blockingQuestions: blocking.map((item) => item.question || item.text).filter((text) => typeof text === 'string' && text),
    holds,
    readiness,
    /* Ready for the client once the exact artifact is approved; the one gate
       left open here is that approval itself. */
    clientReady: holds.length === 0 && readiness.failed.every((gate) => gate === 'decision'),
    artifacts,
  };
}
