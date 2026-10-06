/* The dossier working view: the internal side of an investigation, where a
   reviewer inspects what was found, chooses what goes to the client, reviews
   each claim and each link on its own, and approves one exact exported
   version.

   Everything here is internal. Pending and rejected material is shown and
   labelled as internal; what a client may read is still decided only by the
   Client Read projection. Every step is bound to the exact dossier version on
   screen, so a step taken against a version that has since changed is
   refused by the server and the page asks for a fresh approval. */
import {useEffect, useMemo, useRef, useState} from 'react';
import {apiPost} from '../api.js';
import {beginReviewerSignIn, createReviewClient, finishReviewerSignIn, reviewSignInClientId, reviewSignInProvider} from '../dossierResource.js';
import {
  SUPPORT_VERDICTS,
  artifactPrepareCommand,
  buildDossierWorkingModel,
  claimReviewCommand,
  reviewCommand,
  reviewIdempotencyKey,
  reviewRefusal,
  selectionCommand,
  signInRefusal,
} from '../intelligenceDossierContract.js';
import '../styles/workspaces.css';

/* One default for every render. A default object built per render would be a
   new dependency each time, and the reads keyed on it would run forever. */
const DEFAULT_API = Object.freeze({post: apiPost});

const KIND_WORDS = Object.freeze({
  observation: 'Observation',
  interpretation: 'Interpretation',
  recommendation: 'Recommendation',
  limitation: 'Limitation',
  abstention: 'Abstention',
});

const STATUS_WORDS = Object.freeze({
  pending: 'Awaiting review',
  approved: 'Approved',
  rejected: 'Rejected',
});

const EVIDENCE_WORDS = Object.freeze({
  ready: 'Evidence ready',
  thin: 'Evidence thin',
  contradictory: 'Evidence contradicts it',
  unchecked: 'Evidence not yet checked',
  withheld: 'Evidence withheld',
});

const ARTIFACT_WORDS = Object.freeze({
  draft: 'Draft',
  pending_review: 'Needs approval',
  approved: 'Approved',
  rejected: 'Rejected',
});

const VERDICT_WORDS = Object.freeze({
  supported: 'Supported',
  partial: 'Partly supported',
  contradictory: 'Contradicted',
  unsupported: 'Unsupported',
});

function Refusal({notice}){
  if (!notice) return null;
  if (notice.tone === 'done') return <p className="dossier-review__notice" role="status">{notice.words}</p>;
  return (
    <div className="dossier-review__notice" role="alert">
      <p>{notice.words}</p>
      {notice.code ? <details className="workspace-reason"><summary>Details</summary><p>Reason the service gave: <code>{notice.code}</code></p></details> : null}
    </div>
  );
}

function downloadText(name, text, type){
  try {
    const url = URL.createObjectURL(new Blob([text], {type}));
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (_error){
    /* A browser without object URLs keeps the exported text on screen. */
  }
}

function downloadBase64(name, encoded, type){
  try {
    const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));
    downloadText(name, bytes, type);
  } catch (_error){
    /* An unreadable export is not offered as a file. */
  }
}

/* Reviewer sign in, offered in place of anything that needs the session. When
   the page names no identity provider client, sign in is not set up here, and
   the panel says so instead of offering a control that cannot work.

   The provider's own button is the sign in control. Its automatic prompt can
   be held back for weeks after one dismissal and some browsers never show
   it, so the prompt is only an extra and the button is always rendered. The
   button is set up with the nonce this login was issued, and its callback
   completes the same login. A login outlives its nonce by design only for a
   few minutes, so a fresh one is started shortly before it expires. */
const DEFAULT_SIGNIN_FLOW = Object.freeze({post: apiPost, provider: reviewSignInProvider, clientId: reviewSignInClientId});

function ReviewerSignIn({configured, lead, flow = DEFAULT_SIGNIN_FLOW}){
  const container = useRef(null);
  const prompted = useRef(false);
  const [view, setView] = useState({phase: 'starting', problem: null});
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!configured) return undefined;
    let live = true;
    const timers = [];
    const pollMs = flow.pollMs || 200;
    const pollLimit = flow.pollLimit || 50;
    /* Whatever an earlier run set up is taken down before this one starts
       and when it ends: the provider's pending sign in is cancelled and its
       button is removed, so an old nonce never stays on screen. */
    const reset = () => {
      const provider = flow.provider();
      try {
        if (provider && typeof provider.cancel === 'function') provider.cancel();
      } catch (_error){
        /* Nothing to cancel. */
      }
      if (container.current) while (container.current.firstChild) container.current.removeChild(container.current.firstChild);
    };
    const fail = (error) => { if (live) setView({phase: 'failed', problem: signInRefusal(error).words}); };
    reset();
    setView({phase: 'starting', problem: null});
    beginReviewerSignIn({post: flow.post}).then((login) => {
      if (!live) return;
      /* A fresh login a minute before this one's nonce lapses. */
      const lifetime = Number(login.expires_in);
      if (Number.isFinite(lifetime) && lifetime > 0){
        const refreshIn = lifetime > 60 ? lifetime - 60 : lifetime / 2;
        timers.push(setTimeout(() => { if (live) setAttempt((value) => value + 1); }, refreshIn * 1000));
      }
      let tries = 0;
      const mount = () => {
        if (!live) return;
        const provider = flow.provider();
        const clientId = flow.clientId();
        if (!provider || !clientId || !container.current){
          tries += 1;
          if (tries > pollLimit){ fail(new Error('The sign in service did not load. Reload the page and try again.')); return; }
          timers.push(setTimeout(mount, pollMs));
          return;
        }
        provider.initialize({
          client_id: clientId,
          nonce: login.nonce,
          callback: (response) => {
            if (!live) return;
            if (!response || !response.credential){ fail(new Error('The sign in was not completed.')); return; }
            finishReviewerSignIn({credential: response.credential, login, post: flow.post}).catch(fail);
          },
        });
        provider.renderButton(container.current, {type: 'standard', text: 'signin_with'});
        setView({phase: 'ready', problem: null});
        /* The prompt is an extra, offered once; a refresh does not repeat it. */
        if (!prompted.current && typeof provider.prompt === 'function'){
          prompted.current = true;
          try {
            provider.prompt();
          } catch (_error){
            /* The button stands without it. */
          }
        }
      };
      mount();
    }).catch(fail);
    return () => {
      live = false;
      timers.forEach((timer) => clearTimeout(timer));
      reset();
    };
  }, [configured, attempt, flow]);

  if (!configured){
    return <div className="dossier-review__signin"><p>{lead} Reviewer sign in is not set up on this desk yet.</p></div>;
  }
  return (
    <div className="dossier-review__signin">
      <p>{lead}</p>
      {view.phase === 'starting' ? <p>Preparing reviewer sign in.</p> : null}
      <div className="dossier-review__signin-button" data-review-signin-button="" ref={container} />
      {view.problem ? (
        <div role="alert">
          <p>{view.problem}</p>
          <button type="button" className="legacy-action" onClick={() => setAttempt((value) => value + 1)}>Start sign in again</button>
        </div>
      ) : null}
    </div>
  );
}

function ClaimReviewForm({claim, disabled, onSubmit}){
  const [verdict, setVerdict] = useState('');
  const [receipts, setReceipts] = useState([]);
  const [note, setNote] = useState('');
  const [problem, setProblem] = useState('');
  const toggle = (id) => setReceipts((list) => (list.includes(id) ? list.filter((item) => item !== id) : [...list, id]));
  const submit = (action) => {
    setProblem('');
    Promise.resolve().then(() => onSubmit({claim, action, verdict, receipts, note})).catch((error) => setProblem(error && error.message ? error.message : 'This review could not be sent.'));
  };
  return (
    <fieldset className="dossier-review__form" data-review-claim={claim.claimId}>
      <legend>Review this claim</legend>
      <label>
        Verdict
        <select value={verdict} onChange={(event) => setVerdict(event.target.value)} disabled={disabled}>
          <option value="">Choose a verdict</option>
          {SUPPORT_VERDICTS.map((value) => <option key={value} value={value}>{VERDICT_WORDS[value]}</option>)}
        </select>
      </label>
      <div role="group" aria-label="Receipts the verdict rests on">
        {claim.citations.length ? claim.citations.map((id) => (
          <label key={id} className="dossier-review__receipt">
            <input type="checkbox" value={id} checked={receipts.includes(id)} onChange={() => toggle(id)} disabled={disabled} />
            <code>{id}</code>
          </label>
        )) : <p>This claim cites no receipt, so it cannot be approved as supported.</p>}
      </div>
      <label>
        Note
        <textarea value={note} onChange={(event) => setNote(event.target.value)} disabled={disabled} rows={2} />
      </label>
      {problem ? <p className="dossier-review__problem" role="alert">{problem}</p> : null}
      <div className="dossier-review__actions">
        <button type="button" className="legacy-action legacy-action--primary" disabled={disabled} onClick={() => submit('approve')}>Approve claim</button>
        <button type="button" className="legacy-action" disabled={disabled} onClick={() => submit('reject')}>Reject claim</button>
      </div>
    </fieldset>
  );
}

function ClaimRow({claim, selectable, selected, locked, onToggle, revealed, onReveal, reviewing, onReview}){
  return (
    <li className="workspace-row dossier-review__claim" data-claim-kind={claim.kind}>
      <p className="workspace-state">
        {KIND_WORDS[claim.kind] || 'Claim'} · {STATUS_WORDS[claim.status] || 'State not stated'} · {EVIDENCE_WORDS[claim.evidenceState] || 'Evidence state not stated'}
      </p>
      <h3>{claim.text || 'This claim has no text.'}</h3>
      {claim.internalReason ? <p className="dossier-review__internal">{claim.internalReason}</p> : null}
      {selectable ? (
        <label className="dossier-review__select">
          <input type="checkbox" data-claim={claim.claimId} checked={selected} disabled={locked} onChange={() => onToggle(claim.claimId)} />
          {locked ? 'Kept in the selection: a limitation always travels with the findings' : 'Select for the client'}
        </label>
      ) : null}
      <details className="workspace-reference">
        <summary>Receipts</summary>
        <p>{claim.citations.length ? claim.citations.map((id) => <code key={id}>{id} </code>) : 'None cited.'}</p>
      </details>
      {claim.contradicting.length ? (
        revealed
          ? <p className="workspace-challenge">Contradicting evidence: <code>{claim.contradicting.join(', ')}</code></p>
          : <button type="button" className="legacy-action" onClick={() => onReveal(claim.claimId)}>Show contradicting evidence</button>
      ) : null}
      {reviewing ? onReview(claim) : null}
    </li>
  );
}

export function DossierReview({
  investigationId,
  resource,
  api = DEFAULT_API,
  reviewSession = null,
  signInConfigured = true,
  onVersionChange,
  onReloadLatest,
  onOpenClientRead,
  signInFlow,
  onAuth,
}){
  const dossier = resource && resource.status === 'ready' ? resource.dossier : null;
  const model = useMemo(() => (dossier ? buildDossierWorkingModel(dossier) : null), [dossier]);
  const client = useMemo(() => createReviewClient({post: api.post, investigationId, csrfToken: reviewSession && reviewSession.csrfToken}), [api.post, investigationId, reviewSession]); // eslint-disable-line react-hooks/exhaustive-deps
  const [selection, setSelection] = useState([]);
  const [revealed, setRevealed] = useState([]);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [prepared, setPrepared] = useState([]);
  const [exports, setExports] = useState({});

  const version = model ? model.dossierVersion : null;
  useEffect(() => {
    if (!model) return;
    setSelection([...model.findings, ...model.limitations].filter((item) => item.selected).map((item) => item.claimId));
    setRevealed([]);
  }, [version]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!resource || resource.status === 'idle') return null;
  if (resource.status === 'signin'){
    return (
      <section className="workspace-page dossier-review" data-review-signin="">
        <h2>Dossier review</h2>
        <ReviewerSignIn configured={signInConfigured} flow={signInFlow} lead="Sign in as a reviewer to open this dossier. The working view is internal, so it is shown only to signed in reviewers." />
      </section>
    );
  }
  if (resource.status === 'loading') return <section className="workspace-page workspace-loading dossier-review" aria-busy="true"><h2>Dossier review</h2><p>Loading this dossier version.</p></section>;
  if (!model){
    const error = resource.error || {words: 'This dossier could not be read.'};
    return (
      <section className="workspace-page workspace-error dossier-review" role="alert">
        <h2>Dossier review</h2>
        <p>{error.words}</p>
        {error.code ? <details className="workspace-reason"><summary>Details</summary><p>Reason: <code>{error.code}</code></p></details> : null}
        {resource.reload ? <button type="button" className="legacy-action legacy-action--primary" onClick={resource.reload}>Try again</button> : null}
      </section>
    );
  }

  const canAct = Boolean(reviewSession && reviewSession.csrfToken) && !busy;
  /* Each step opens for the role the server requires of it; a reviewer who
     holds several roles has every one of those steps open. */
  const roles = new Set(reviewSession ? (Array.isArray(reviewSession.roles) ? reviewSession.roles : [reviewSession.role]) : []);
  const canEdit = canAct && roles.has('dossier_editor');
  const canClaims = canAct && roles.has('claim_approver');
  const canLinks = canAct && roles.has('relationship_approver');
  const canApprove = canAct && roles.has('client_read_approver');
  const refuse = (error, step) => {
    if (error && error.auth && onAuth) onAuth();
    const refusal = reviewRefusal(error, {step});
    setNotice({tone: 'refused', words: refusal.words, code: refusal.code && refusal.code !== 'passcode' ? refusal.code : null});
    /* Only a version conflict means the version on screen is out of date. */
    if (refusal.code === 'review_version_conflict'){
      if (onReloadLatest) onReloadLatest();
      else if (resource.reload) resource.reload();
    }
  };
  const run = async (work, step) => {
    setBusy(true);
    setNotice(null);
    try {
      await work();
    } catch (error){
      refuse(error, step);
    } finally {
      setBusy(false);
    }
  };
  const echoes = (result) => result && result.investigation_id === investigationId && typeof result.dossier_version === 'string';

  const saveSelection = () => run(async () => {
    const chosen = [...model.findings, ...model.limitations]
      .filter((item) => item.kind === 'limitation' || item.kind === 'abstention' || selection.includes(item.claimId))
      .map((item) => item.claimId);
    const result = await client.saveSelection(selectionCommand({dossierVersion: model.dossierVersion, selectedClaimIds: chosen}));
    if (!echoes(result)) throw new Error('The saved version could not be confirmed. Reload the dossier before carrying on.');
    setNotice({tone: 'done', words: 'Saved as a new version. Earlier approvals stay with the version they were given for.'});
    if (onVersionChange) onVersionChange(result.dossier_version);
  });

  const reviewClaim = ({claim, action, verdict, receipts, note}) => {
    /* The builder refuses in words before anything is sent; that refusal is
       the form's to show, so it is thrown back to the form. */
    const body = claimReviewCommand({dossierVersion: model.dossierVersion, claimId: claim.claimId, claimVersion: claim.version, action,
      expectedState: 'pending_review', idempotencyKey: reviewIdempotencyKey({resource: 'claim', resourceId: claim.claimId, action}),
      verdict, receiptIds: receipts, note});
    return run(async () => {
      const result = await client.review(body);
      if (!echoes(result)) throw new Error('The review could not be confirmed. Reload the dossier before carrying on.');
      setNotice({tone: 'done', words: 'Claim review recorded for this version.'});
      if (resource.reload) resource.reload();
    });
  };

  const reviewLink = (link, action) => run(async () => {
    const result = await client.review(reviewCommand({dossierVersion: model.dossierVersion, resource: 'relationship', resourceId: link.relationshipId,
      resourceVersion: link.version, action, expectedState: 'pending_review',
      idempotencyKey: reviewIdempotencyKey({resource: 'relationship', resourceId: link.relationshipId, action})}));
    if (!echoes(result)) throw new Error('The review could not be confirmed. Reload the dossier before carrying on.');
    setNotice({tone: 'done', words: 'Link review recorded for this version.'});
    if (resource.reload) resource.reload();
  });

  const prepare = () => run(async () => {
    const result = await client.prepare(artifactPrepareCommand({dossierVersion: model.dossierVersion}));
    if (!echoes(result) || result.dossier_version !== model.dossierVersion || typeof result.artifact_id !== 'string' || typeof result.artifact_version !== 'string'){
      throw new Error('The prepared artifact could not be confirmed. Reload the dossier before carrying on.');
    }
    const entry = {artifactId: result.artifact_id, artifactVersion: result.artifact_version, dossierVersion: result.dossier_version, state: result.state};
    setPrepared((list) => [...list.filter((item) => item.artifactId !== entry.artifactId), entry]);
    setNotice({tone: 'done', words: 'Internal artifact prepared. Inspect the exported text before approving it.'});
  }, 'prepare');

  const inspect = (artifact) => run(async () => {
    const result = await client.readArtifact({artifactId: artifact.artifactId, artifactVersion: artifact.artifactVersion});
    if (!result || result.investigation_id !== investigationId || result.artifact_id !== artifact.artifactId || result.artifact_version !== artifact.artifactVersion || typeof result.html !== 'string'){
      throw new Error('The exported text that came back is not the version prepared, so it is not shown.');
    }
    setExports((map) => ({...map, [artifact.artifactId]: {artifactVersion: artifact.artifactVersion, html: result.html, pdf: typeof result.pdf === 'string' ? result.pdf : null}}));
  });

  const decideArtifact = (artifact, action) => run(async () => {
    const result = await client.review(reviewCommand({dossierVersion: artifact.dossierVersion, resource: 'artifact', resourceId: artifact.artifactId,
      resourceVersion: artifact.artifactVersion, action, expectedState: 'pending_review',
      idempotencyKey: reviewIdempotencyKey({resource: 'artifact', resourceId: artifact.artifactId, action})}));
    if (!echoes(result)) throw new Error('The decision could not be confirmed. Reload the dossier before carrying on.');
    const state = typeof result.state === 'string' ? result.state : (action === 'approve' ? 'approved' : 'rejected');
    setPrepared((list) => [...list.filter((item) => item.artifactId !== artifact.artifactId), {...artifact, state}]);
    setNotice({tone: 'done', words: action === 'approve' ? 'This exact version is approved for the client.' : 'This version was rejected and stays internal.'});
    if (action !== 'approve' || state !== 'approved') return;
    /* The PDF is sealed with the artifact but served only once the version
       is approved, so the approved read is taken again to fetch it. If that
       read fails the approval still stands, and the Client Read offers it. */
    try {
      const approved = await client.readArtifact({artifactId: artifact.artifactId, artifactVersion: artifact.artifactVersion});
      if (approved && approved.investigation_id === investigationId && approved.artifact_id === artifact.artifactId
        && approved.artifact_version === artifact.artifactVersion && typeof approved.html === 'string'){
        setExports((map) => ({...map, [artifact.artifactId]: {artifactVersion: artifact.artifactVersion, html: approved.html, pdf: typeof approved.pdf === 'string' ? approved.pdf : null}}));
      }
    } catch (error){
      if (error && error.auth && onAuth) onAuth();
    }
  });

  const known = new Map();
  for (const item of [...model.artifacts.current, ...model.artifacts.historical, ...prepared]) known.set(item.artifactId, item);
  const current = [...known.values()].filter((item) => item.dossierVersion === model.dossierVersion);
  const historical = [...known.values()].filter((item) => item.dossierVersion !== model.dossierVersion);
  const toggle = (id) => setSelection((list) => (list.includes(id) ? list.filter((item) => item !== id) : [...list, id]));
  const reveal = (id) => setRevealed((list) => [...list, id]);
  const claimRows = (items, selectable) => items.map((claim) => (
    <ClaimRow key={claim.claimId} claim={claim} selectable={selectable}
      selected={claim.kind === 'limitation' || claim.kind === 'abstention' || selection.includes(claim.claimId)}
      locked={!canEdit || claim.kind === 'limitation' || claim.kind === 'abstention'} onToggle={toggle}
      revealed={revealed.includes(claim.claimId)} onReveal={reveal}
      reviewing={claim.status === 'pending'}
      onReview={(item) => <ClaimReviewForm claim={item} disabled={!canClaims || !item.version} onSubmit={reviewClaim} />} />
  ));

  return (
    <section className="workspace-page dossier-review" data-dossier-version={model.dossierVersion}>
      <header className="workspace-lead">
        <h2>Dossier review</h2>
        <span className="workspace-state">{model.clientReady ? 'Ready for client approval' : 'Not client ready'}</span>
      </header>
      <details className="workspace-reference"><summary>Dossier version</summary><p><code>{model.dossierVersion}</code></p></details>
      {!reviewSession ? (
        <ReviewerSignIn configured={signInConfigured} flow={signInFlow} lead="Sign in as a reviewer to select, review and export." />
      ) : null}
      <Refusal notice={notice} />
      {model.holds.length ? (
        <div className="dossier-review__holds">
          <h3>Held back from the client</h3>
          <ul>{model.holds.map((words) => <li key={words}>{words}</li>)}</ul>
          {model.blockingQuestions.length ? <><h4>Open questions</h4><ul>{model.blockingQuestions.map((text) => <li key={text}>{text}</li>)}</ul></> : null}
        </div>
      ) : null}

      <h3>Observations and interpretations</h3>
      {model.findings.length ? <ol className="workspace-rows">{claimRows(model.findings, true)}</ol> : <p>No findings in this version.</p>}
      <h3>Limitations</h3>
      {model.limitations.length ? <ol className="workspace-rows">{claimRows(model.limitations, true)}</ol> : <p>No limitations are recorded in this version.</p>}
      <div className="dossier-review__actions">
        <button type="button" className="legacy-action legacy-action--primary" disabled={!canEdit} onClick={saveSelection}>Save as a new version</button>
      </div>

      <h3>Links between claims</h3>
      {model.relationships.length ? (
        <ol className="workspace-rows">
          {model.relationships.map((link) => (
            <li key={link.relationshipId} className="workspace-row" data-review-relationship={link.relationshipId}>
              <p className="workspace-state">{STATUS_WORDS[link.status] || 'State not stated'}</p>
              <p>Links <code>{link.parentClaimId}</code> to <code>{link.claimId}</code></p>
              {link.status === 'pending' ? (
                <div className="dossier-review__actions">
                  <button type="button" className="legacy-action legacy-action--primary" disabled={!canLinks || !link.version} onClick={() => reviewLink(link, 'approve')}>Approve link</button>
                  <button type="button" className="legacy-action" disabled={!canLinks || !link.version} onClick={() => reviewLink(link, 'reject')}>Reject link</button>
                </div>
              ) : null}
            </li>
          ))}
        </ol>
      ) : <p>No links between claims in this version.</p>}

      <h3>Client artifact</h3>
      <div className="dossier-review__actions">
        <button type="button" className="legacy-action legacy-action--primary" disabled={!canEdit} onClick={prepare}>Prepare internal artifact</button>
      </div>
      <div data-artifacts="current">
        {current.length ? current.map((artifact) => {
          const exported = exports[artifact.artifactId];
          const inspected = Boolean(exported && exported.artifactVersion === artifact.artifactVersion);
          const approved = artifact.state === 'approved';
          return (
            <article key={artifact.artifactId} className="workspace-row dossier-review__artifact">
              <p className="workspace-state">{ARTIFACT_WORDS[artifact.state] || 'State not stated'}</p>
              <details className="workspace-reference"><summary>Artifact version</summary><p><code>{artifact.artifactVersion}</code></p></details>
              <div className="dossier-review__actions">
                <button type="button" className={inspected || approved ? 'legacy-action' : 'legacy-action legacy-action--primary'} disabled={busy} onClick={() => inspect(artifact)}>Inspect exported text</button>
                {!approved ? (
                  <>
                    <button type="button" className={inspected ? 'legacy-action legacy-action--primary' : 'legacy-action'} disabled={!canApprove || !inspected || !model.clientReady || artifact.state !== 'pending_review'} onClick={() => decideArtifact(artifact, 'approve')}>Approve this exact version</button>
                    <button type="button" className="legacy-action" disabled={!canApprove || artifact.state !== 'pending_review'} onClick={() => decideArtifact(artifact, 'reject')}>Reject this version</button>
                  </>
                ) : null}
              </div>
              {!inspected && !approved ? <p>Approval opens once the exported text of this exact version has been inspected.</p> : null}
              {inspected && !model.clientReady ? <p>Approval is held until everything above that holds it back is resolved.</p> : null}
              {inspected ? <pre className="dossier-review__export" data-export="html">{exported.html}</pre> : null}
              {approved ? (
                <div className="dossier-review__actions">
                  <button type="button" className="legacy-action legacy-action--primary" onClick={() => onOpenClientRead && onOpenClientRead({artifactId: artifact.artifactId, artifactVersion: artifact.artifactVersion})}>Open Client Read</button>
                  {inspected ? <button type="button" className="legacy-action" onClick={() => downloadText(`${artifact.artifactId}.html`, exported.html, 'text/html')}>Download HTML</button> : <p>Inspect the exported text to download it.</p>}
                  {inspected && exported.pdf
                    ? <button type="button" className="legacy-action" onClick={() => downloadBase64(`${artifact.artifactId}.pdf`, exported.pdf, 'application/pdf')}>Download PDF</button>
                    : <p>Open Client Read to download the PDF.</p>}
                </div>
              ) : null}
            </article>
          );
        }) : <p>This version needs approval: no artifact has been prepared and approved for it yet.</p>}
      </div>
      {historical.length ? (
        <div data-artifacts="historical">
          <h4>Earlier versions</h4>
          <ul>
            {historical.map((artifact) => (
              <li key={artifact.artifactId}>
                {artifact.state === 'approved'
                  ? 'Approved for an earlier version of this dossier. It stays readable exactly as it was approved, and its approval does not carry over to this version.'
                  : 'Prepared for an earlier version of this dossier. It stays internal.'}
                <details className="workspace-reference"><summary>Artifact version</summary><p><code>{artifact.artifactVersion}</code></p></details>
                {artifact.state === 'approved' && onOpenClientRead ? (
                  <button type="button" className="legacy-action" onClick={() => onOpenClientRead({artifactId: artifact.artifactId, artifactVersion: artifact.artifactVersion})}>Open the earlier Client Read</button>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

export default DossierReview;

/* The Client Read of one exact artifact version. The approved export is shown
   as the bytes that were approved, in a sandbox that runs nothing and loads
   nothing, because the client will read exactly this and nothing re-rendered
   from it. A version that is not approved is not shown at all. */
function clientReadView(result, investigationId, artifactId, artifactVersion){
  if (!result || result.investigation_id !== investigationId || result.artifact_id !== artifactId
    || result.artifact_version !== artifactVersion || typeof result.html !== 'string'){
    return {state: 'refused', words: 'The artifact that came back is not the version this link names, so it is not shown.'};
  }
  if (result.state !== 'approved') return {state: 'refused', words: 'This version has not been approved for the client, so it stays internal.'};
  return {state: 'ready', html: result.html, pdf: typeof result.pdf === 'string' ? result.pdf : null};
}

/* read is the outcome of a read someone else already made of this exact
   version (the Evidence Room, on the console). When it is given, the panel
   sends nothing and shows nothing for a failed read, because the owner of
   the read has already said so once and offers the one Retry. */
export function ClientReadExport({investigationId, artifactId, artifactVersion, api = DEFAULT_API, onAuth, read}){
  const shared = read !== undefined;
  const [fetched, setView] = useState({state: 'loading'});
  useEffect(() => {
    if (shared) return undefined;
    if (!investigationId || !artifactId || !artifactVersion){
      setView({state: 'refused', words: 'This link does not name an exact artifact version, so nothing is shown.'});
      return undefined;
    }
    let live = true;
    setView({state: 'loading'});
    createReviewClient({post: api.post, investigationId}).readArtifact({artifactId, artifactVersion})
      .then((result) => {
        if (!live) return;
        setView(clientReadView(result, investigationId, artifactId, artifactVersion));
      })
      .catch((error) => {
        if (!live) return;
        if (error && error.auth && onAuth) onAuth();
        const refusal = reviewRefusal(error);
        setView({state: 'refused', words: error && error.status === 404 ? 'This artifact version is not available to you.' : refusal.words, code: refusal.code});
      });
    return () => { live = false; };
  }, [shared, investigationId, artifactId, artifactVersion, api.post]); // eslint-disable-line react-hooks/exhaustive-deps

  const view = !shared ? fetched
    : !read || read.state === 'loading' ? {state: 'loading'}
      : read.state === 'ready' ? clientReadView(read.value, investigationId, artifactId, artifactVersion)
        : null;
  if (!view) return null;
  if (view.state === 'loading') return <section className="workspace-page workspace-loading" aria-busy="true"><h2>Client Read</h2><p>Loading this exact version.</p></section>;
  if (view.state !== 'ready'){
    return (
      <section className="workspace-page workspace-error" role="alert">
        <h2>Client Read</h2>
        <p>{view.words}</p>
        {view.code && view.code !== 'passcode' ? <details className="workspace-reason"><summary>Details</summary><p>Reason the service gave: <code>{view.code}</code></p></details> : null}
      </section>
    );
  }
  return (
    <section className="workspace-page dossier-client-read" data-artifact-version={artifactVersion}>
      <h2>Client Read</h2>
      <iframe className="dossier-client-read__frame" title="Client Read, approved version" sandbox="" srcDoc={view.html} />
      <div className="dossier-review__actions">
        <button type="button" className="legacy-action" onClick={() => downloadText(`${artifactId}.html`, view.html, 'text/html')}>Download HTML</button>
        {view.pdf
          ? <button type="button" className="legacy-action legacy-action--primary" onClick={() => downloadBase64(`${artifactId}.pdf`, view.pdf, 'application/pdf')}>Download PDF</button>
          : <p>The PDF for this exact version could not be read just now, so only the HTML can be downloaded.</p>}
      </div>
    </section>
  );
}
