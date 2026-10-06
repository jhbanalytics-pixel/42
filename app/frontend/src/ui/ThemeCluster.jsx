import React from 'react';
import {MomentumPill} from './MomentumPill.jsx';
import {EvidenceReadiness} from './EvidenceReadiness.jsx';
import {deriveReadiness} from '../readiness.js';
const REL = {shares: 'oi-cluster__rel--shares', against: 'oi-cluster__rel--against', tension: 'oi-cluster__rel--against', anchor: ''};
// Signals as peers. No ranking, no truncation: the relationships are the content and contradiction is content, not failure.
export function ThemeCluster({eyebrow, title, summary, signals = [], held, onOpen}) {
  return (
    <section className="oi-cluster">
      {eyebrow ? <p className="dossier-eyebrow oi-cluster__eyebrow">{eyebrow}</p> : null}
      {title ? <h3 className="oi-display oi-cluster__title">{title}</h3> : null}
      {summary ? <p className="oi-body oi-cluster__summary">{summary}</p> : null}
      {signals.map((s, i) => (
        <div key={s.id || i} className="oi-cluster__signal">
          <p className="oi-body oi-cluster__name">{s.title}</p>
          <span className="oi-cluster__pill"><MomentumPill state={s.momentum} /></span>
          <span className="oi-cluster__pill"><EvidenceReadiness state={deriveReadiness(s.receipts, s.readiness)} /></span>
          {s.relation ? <p className={'oi-provenance oi-cluster__rel ' + (REL[s.relationKind] || '')}>{s.relation}</p> : null}
          {onOpen ? <button type="button" className="oi-cluster__open oi-provenance" onClick={() => onOpen(s)}>Receipts</button> : null}
        </div>
      ))}
      {held ? (
        <div className="oi-cluster__held">
          <p className="oi-metadata oi-cluster__held-label">HELD IN THIS THEME</p>
          <p className="oi-body oi-cluster__held-body">{held}</p>
        </div>
      ) : null}
    </section>
  );
}
export default ThemeCluster;
