
const STATES = {
  ready: {
    label: 'Ready',
    detail: 'Supporting receipts meet the evidence standard',
    mark: '✓',
  },
  thin: {
    label: 'Thin',
    detail: 'Some support exists, but important coverage is missing',
    mark: '△',
  },
  contradictory: {
    label: 'Contradictory',
    detail: 'Credible sources disagree',
    mark: '⇄',
  },
  unchecked: {
    label: 'Unchecked',
    detail: 'Evidence has not been reviewed',
    mark: '?',
  },
};

export function EvidenceReadiness({state = 'unchecked'}){
  const normalizedState = STATES[state] ? state : 'unchecked';
  const meta = STATES[normalizedState];
  return (
    <span
      className="evidence-readiness"
      data-state={normalizedState}
      role="status"
      aria-label={`Evidence readiness: ${meta.label}. ${meta.detail}`}
    >
      <span className="evidence-readiness__mark" aria-hidden="true">{meta.mark}</span>
      <span>{meta.label}</span>
    </span>
  );
}
