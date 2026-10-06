import {useId} from 'react';

import {EvidenceReadiness} from './EvidenceReadiness.jsx';

export function SignalComparisonRow({
  signal = 'Untitled signal',
  comparison = 'Comparison unavailable.',
  movement,
  proof = 'Evidence not provided.',
  readiness = 'unchecked',
  onEvidence,
  evidenceLabel = 'Inspect evidence',
}){
  const titleId = useId();
  return (
    <div className="dossier-comparison" role="group" aria-labelledby={titleId}>
      <div className="dossier-comparison__signal">
        <h3 className="dossier-comparison__title" id={titleId}>{signal}</h3>
        {movement && <span className="dossier-comparison__movement">Movement: {movement}</span>}
      </div>
      <p className="dossier-comparison__summary">{comparison}</p>
      <p className="dossier-comparison__proof">
        <span className="dossier-comparison__proof-label">Proof</span><br />
        {proof}
      </p>
      <div className="dossier-comparison__actions">
        <EvidenceReadiness state={readiness} />
        {onEvidence && (
          <button type="button" className="dossier-action" onClick={onEvidence}>
            {evidenceLabel}
          </button>
        )}
      </div>
    </div>
  );
}
