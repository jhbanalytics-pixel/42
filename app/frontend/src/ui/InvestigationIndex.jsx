/* The dossier landing: recent investigations, as rows.

   This is the door. Until it existed the Evidence Room, the Historical
   workspace and the Client Read all read an investigation by identity that no
   screen could produce, so the last third of the journey had no way in.

   Rows and rules, in the same editorial idiom as the Client Read. Each row
   opens one investigation, and where an artifact already exists it offers the
   client read directly, because that is the end a strategist is walking
   towards.

   Empty and unavailable are different sentences here, deliberately. Empty is a
   fact about the estate. Unavailable is a fact about the read. */

import '../styles/console.css';
import {buildIndexState} from '../investigationIndex.js';

const STATUS_LABEL = Object.freeze({
  plan_ready: 'Plan ready',
  in_review: 'In review',
  approved: 'Approved',
  blocked: 'Blocked',
});

function investigationHref(item){
  const base = `#/console?work=brief&investigation=${encodeURIComponent(item.investigation_id)}`;
  /* The canonical artifact hash carries both identities. An artifact without
     its investigation is workspace_request_invalid, so it is never linked
     alone. */
  return item.candidate_artifact_id
    ? `${base}&artifact=${encodeURIComponent(item.candidate_artifact_id)}`
    : base;
}

function shortDate(value){
  if (typeof value !== 'string' || !value) return 'Date unavailable';
  const at = new Date(value);
  return Number.isNaN(at.getTime())
    ? 'Date unavailable'
    : at.toISOString().slice(0, 10);
}

function InvestigationRow({item}){
  return (
    <article className="investigation-row" data-readiness={item.readiness_state || 'unknown'}>
      <p className="investigation-row__meta">
        {(item.market_labels || []).join(' · ') || 'Markets unavailable'}
        {' · '}
        {shortDate(item.created_at)}
        {' · '}
        {STATUS_LABEL[item.status] || item.status || 'Status unavailable'}
      </p>
      <h2 className="investigation-row__question">
        <a href={investigationHref(item)}>
          {item.decision_question || 'This investigation states no question.'}
        </a>
      </h2>
      {/* The identity is shown because a strategist sharing this row needs the
          thing that identifies it, not a prettier name for it. */}
      <p className="investigation-row__identity">{item.investigation_id}</p>
      {item.candidate_artifact_id ? (
        <p className="investigation-row__artifact">Client read prepared</p>
      ) : null}
    </article>
  );
}

export function InvestigationIndex({payload = null, loading = false, error = null}){
  const view = buildIndexState({payload, loading, error});

  return (
    <div className="page investigation-index" data-index-state={view.state}>
      <header className="investigation-folio">
        <p className="investigation-folio__kicker">Intelligence Console</p>
        <h1 className="investigation-folio__title">Recent investigations</h1>
      </header>

      {view.state === 'loading' ? (
        <p className="investigation-state" aria-busy="true">Reading recent investigations.</p>
      ) : null}

      {view.state === 'error' ? (
        <p className="investigation-state" role="alert">
          Recent investigations are unavailable. Nothing is known about this estate either way.
        </p>
      ) : null}

      {view.state === 'unsupported_contract' ? (
        <p className="investigation-state" role="alert">
          This desk cannot read the version these investigations are stored in, so none is shown.
        </p>
      ) : null}

      {/* Empty is its own sentence. It says the estate is empty, which is a
          claim only worth making once the read came back. */}
      {view.state === 'empty' ? (
        <p className="investigation-state">
          No investigations have been framed in this scope yet.
        </p>
      ) : null}

      {view.state === 'ready' ? (
        <div className="investigation-rows">
          {view.investigations.map((item) => (
            <InvestigationRow item={item} key={item.investigation_id} />
          ))}
        </div>
      ) : null}

      {view.truncated ? (
        <p className="investigation-state">
          Showing the most recent {view.investigations.length} of {view.totalCount}.
        </p>
      ) : null}
    </div>
  );
}

export default InvestigationIndex;
