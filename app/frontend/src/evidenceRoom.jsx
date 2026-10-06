import {useEffect, useState} from 'react';
import {FAILURE_ORIGIN_LABEL, apiPost, failureCode, failureOrigin, readerFigure} from './api.js';
import {readerWord, workspaceGapLabel} from './model.js';
import './styles/workspaces.css';
import './styles/console.css';

function claimState(claims){
  if (!claims.length) return 'empty';
  for (const state of ['contradictory', 'thin', 'unchecked']) if (claims.some((claim) => claim.evidence_state === state)) return state;
  return 'ready';
}

export function resolveEvidenceRoomView(status, claimResource, decisionResource, artifactResource){
  /* Empty is a fact about the investigation: it has been opened and nothing
     has been claimed yet. A claims resource that arrives without its list is a
     fact about the read instead, so it is an error. Collapsing the two would
     state something about the investigation that was never established. */
  /* An unsupported version is named as such rather than reduced to the
     generic unavailable state, because the reader can do something about a
     version they are told about and nothing about a bare code. */
  if (status && status.contract_version_unsupported) return {state: 'unsupported_contract', status, claims: [], error: {code: 'contract_version_unsupported'}, decision: null, artifact: null};
  const readable = Array.isArray(claimResource && claimResource.claims);
  const claims = readable ? claimResource.claims : [];
  if (!readable){
    return {
      state: 'error',
      status,
      claims: [],
      /* The read answered, so no producer reported a failure: this page found
         the list missing from what it returned, and says so as its own. */
      error: {code: 'claim_list_missing', origin: 'derived'},
      decision: null,
      artifact: null,
    };
  }
  return {
    state: claimState(claims),
    status,
    claims,
    decision: decisionResource && decisionResource.decision,
    artifact: artifactResource && artifactResource.artifact,
  };
}

/* Whose code a failure frame prints: the origin the failure was recorded
   with where there is one, and otherwise what the code itself says. */
function FailureCode({failure}){
  const code = failure && typeof failure === 'object' ? failure.code : failure;
  const origin = (failure && typeof failure === 'object' && failure.origin) || failureOrigin(code);
  return <p>{FAILURE_ORIGIN_LABEL[origin]} <code>{code || 'unstated'}</code></p>;
}

export function EvidenceRoomView({state, status, claims=[], decision, artifact, artifactError, error, onRetry}){
  if (state === 'loading') return <section className="workspace-page workspace-loading" aria-busy="true"><h1>Evidence Room</h1><p>Loading the investigation plan, claims and proof.</p><div className="workspace-skeleton" /></section>;
  if (state === 'stale') return <section className="workspace-page workspace-error" role="alert"><h1>Evidence Room</h1><p>The Evidence Room snapshot is stale. No previous content is shown.</p>{onRetry && <button type="button" onClick={onRetry}>Try again</button>}</section>;
  if (state === 'auth') return <section className="workspace-page workspace-error" role="alert"><h1>Evidence Room</h1><p>Authentication is required before this investigation can be read.</p></section>;
  if (state === 'unsupported_contract') return <section className="workspace-page workspace-error" role="alert"><h1>Evidence Room</h1><p>This investigation is stored in a version this desk cannot read. Nothing from it is shown, because a partial reading of an unknown version would be worse than none.</p>{onRetry && <button type="button" onClick={onRetry}>Try again</button>}</section>;
  if (state === 'error' || !status) return <section className="workspace-page workspace-error" role="alert"><h1>Evidence Room</h1><p>The investigation is unavailable.</p>{error && <details className="workspace-reason"><summary>Details</summary><FailureCode failure={error} /></details>}{onRetry && <button type="button" onClick={onRetry}>Try again</button>}</section>;
  const plan = status.research_plan || {};
  const artifactView = artifactError ? <div><p role="alert">{artifactError.code === 'workspace_snapshot_stale' ? 'Requested artifact is stale.' : artifactError.code === 'contract_version_unsupported' ? 'Requested artifact uses an unsupported version.' : 'Requested artifact is unavailable.'}</p><details><summary>Artifact read details</summary><FailureCode failure={artifactError} /></details>{onRetry && <button type="button" onClick={onRetry}>Retry read</button>}</div> : artifact ? (artifact.title ? <p>{artifact.title}</p> : <><p>This artifact has no title.</p><details className="workspace-reference"><summary>Artifact reference</summary><p>{artifact.artifact_id}</p></details></>) : <p>No artifact exists for this investigation.</p>;
  /* Quiet register, 23 Sept 2026: no eyebrow over the title, headings in
     plain words, source families written as words, and every id a reader
     would quote waits under a disclosure in mono. The one exception is the
     evidence that challenges a claim: the contradiction is surfaced, not
     made available, so its ids stay on the claim's face, set in mono. */
  return <section className="workspace-page evidence-room" data-evidence-state={state}><header className="workspace-lead"><h1>Evidence Room</h1><span className="workspace-state">{workspaceGapLabel(state)}</span></header><details className="workspace-reference"><summary>Investigation reference</summary><p>{status.investigation_id}</p></details><div className="evidence-room-grid"><aside><h2>Research plan</h2><ol>{(plan.questions || []).map((question) => <li key={question}>{question}</li>)}</ol><h3>Required sources</h3><p>{(plan.required_source_families || []).map(readerWord).join(' \u00b7 ') || 'Unavailable'}</p><h3>Still missing</h3>{(status.missing_work || []).length ? <ul>{status.missing_work.map((item) => <li key={item}>{workspaceGapLabel(item)}</li>)}</ul> : <p>None recorded.</p>}</aside><main><h2>Claims</h2>{claims.length === 0 ? <p>No claims gathered yet.</p> : <ol className="workspace-rows">{claims.map((claim) => { const opposing = claim.opposing_evidence_ids || []; return <li key={claim.claim_id} className="workspace-row"><span className="workspace-state">{workspaceGapLabel(claim.evidence_state)}</span><h3>{claim.claim_text}</h3><p>{claim.confidence == null ? 'Confidence not stated' : `Confidence ${readerFigure(claim.confidence)}`}</p>{opposing.length > 0 && <p className="workspace-challenge">Challenge evidence: <code>{opposing.join(', ')}</code></p>}<details><summary>Open evidence</summary><p>Support evidence: {(claim.supporting_evidence_ids || []).length ? <code>{claim.supporting_evidence_ids.join(', ')}</code> : 'None'}</p><p>Missing evidence: {(claim.missing_evidence || []).join(', ') || 'None'}</p></details></li>; })}</ol>}</main><aside><h2>Decision</h2>{decision ? <p>{workspaceGapLabel(decision.state)}</p> : claims.some((claim) => claim.human_review_required) ? <p>Held for human review: no decision has been recorded and {claims.filter((claim) => claim.human_review_required).length} of {claims.length} claims require it.</p> : <p>No decision resource exists.</p>}<h2>Artifact</h2>{artifactView}</aside></div></section>;
}

const EXACT_VERSION = /^[0-9a-f]{64}$/;

/* The artifact read the server answers takes the exact version the link
   carries and nothing else. A link without one is refused here rather than
   sent, because the server can only refuse it. */
export function evidenceRoomArtifactRead(investigationId, artifactId, artifactVersion){
  if (!artifactId) return {path: null, error: null};
  if (typeof artifactVersion !== 'string' || !EXACT_VERSION.test(artifactVersion)) return {path: null, error: {code: 'workspace_request_invalid', origin: 'derived'}};
  return {path: `/api/v2/investigations/${encodeURIComponent(investigationId)}/artifacts/${encodeURIComponent(artifactId)}/read?artifact_version=${artifactVersion}`};
}

/* The Evidence Room owns the one read of the artifact version its link names.
   The Client Read panel needs the same exact read, so the room hands the
   outcome on through onArtifactRead rather than the panel reading it again:
   one read per version, one alert when it fails, one re-read on Retry. */
export function EvidenceRoom({routeState, onAuth, onArtifactRead}){
  const investigationId = routeState && routeState.investigationId;
  const artifactId = routeState && routeState.artifactId;
  const artifactVersion = routeState && routeState.artifactVersion;
  const [view, setView] = useState({state: 'loading'});
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!investigationId) { setView({state: 'error', error: {code: 'workspace_request_invalid', origin: 'derived'}}); return; }
    let live = true;
    setView({state: 'loading'});
    const base = `/api/v2/investigations/${encodeURIComponent(investigationId)}`;
    const read = evidenceRoomArtifactRead(investigationId, artifactId, artifactVersion);
    const key = `${investigationId}:${artifactId}:${artifactVersion}`;
    const hand = (outcome) => { if (live && read.path && onArtifactRead) onArtifactRead({key, ...outcome}); };
    hand({state: 'loading'});
    const artifactRead = read.path ? apiPost(read.path).then(value => ({value})).catch(error => { if (error && error.auth) throw error; return {error: {code: failureCode(error)}}; }) : Promise.resolve(read.error ? {value: null, error: read.error} : {value: null});
    Promise.all([apiPost(base + '/status'), apiPost(base + '/claims/read'), apiPost(base + '/decision/read'), artifactRead])
      .then(([status, claimResource, decisionResource, artifactResult]) => {
        if (!live) return;
        hand(artifactResult.error ? {state: 'failed'} : {state: 'ready', value: artifactResult.value});
        setView({...resolveEvidenceRoomView(status, claimResource, decisionResource, artifactResult.value), artifactError: artifactResult.error || null});
      })
      .catch((error) => {
        if (!live) return;
        /* The room says what failed, once; the panel shows nothing more. */
        hand({state: 'failed'});
        if (error && error.auth) { onAuth && onAuth(); setView({state: 'auth'}); } else setView({state: error && error.code === 'workspace_snapshot_stale' ? 'stale' : 'error', error: {code: failureCode(error)}});
      });
    return () => { live = false; };
  }, [investigationId, artifactId, artifactVersion, retry, onAuth, onArtifactRead]);
  return <EvidenceRoomView {...view} onRetry={() => setRetry((value) => value + 1)} />;
}
