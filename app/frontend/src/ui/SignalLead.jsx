/* The editorial lead.

   Readiness is derived, never asserted. The component took a bare `readiness`
   prop and rendered whatever it was handed, so a caller could pass "ready"
   with no receipts and the system drew a confident green claim with nothing
   behind it. The rule the product rests on was enforced in the API and in
   admission, and nowhere here. Now the receipts decide, and a handed state can
   only demote: a caller may say a signal is weaker than its evidence looks,
   never stronger.

   Reading order follows from the same rule. The strategist's first question is
   whether a signal can be used, so the verdict and the family proof line sit
   above the title rather than in the footer, where they arrived four seconds
   after a confident serif headline. And a response renders only when the
   evidence is ready: a recommendation resting on thin or contradictory
   evidence is the exact thing this system exists to refuse. */

import {EvidenceReadiness} from './EvidenceReadiness.jsx';
import {deriveReadiness, proofLine} from '../readiness.js';

function DecisionSection({label, children}){
  return (
    <section className="dossier-section">
      <h3 className="dossier-section__label">{label}</h3>
      <p className="dossier-section__body">{children || 'Not provided.'}</p>
    </section>
  );
}

export function SignalLead({
  eyebrow,
  title = 'Untitled signal',
  movement,
  whyNow,
  proof,
  precedent,
  response,
  evidence,
  readiness,
  onEvidence,
  evidenceLabel = 'Inspect evidence',
}){
  const state = deriveReadiness(evidence, readiness);
  /* The proof line states families, not a receipt count. Three receipts from
     one creator family are not three independent agreements, and a count
     smuggled that certainty in. A caller may still pass its own prose. */
  const proofBody = proof || proofLine(evidence);
  const match = precedent && precedent.match;
  const difference = precedent && precedent.difference;

  return (
    <article className="dossier-lead" aria-label={title}>
      <header className="dossier-lead__header">
        <div className="dossier-lead__verdict">
          <EvidenceReadiness state={state} />
          <p className="dossier-lead__proof-line">{proofLine(evidence)}</p>
        </div>
        {eyebrow && <div className="dossier-eyebrow">{eyebrow}</div>}
        <h2 className="dossier-lead__title">{title}</h2>
        {movement && (
          <p className="dossier-movement">
            <span className="dossier-movement__label">Movement</span>
            <span>{movement}</span>
          </p>
        )}
      </header>
      <div className="dossier-lead__path">
        <DecisionSection label="Why now">{whyNow}</DecisionSection>
        <DecisionSection label="Proof">{proofBody}</DecisionSection>
        {(match || difference) && (
          <section className="dossier-section dossier-section--precedent">
            <h3 className="dossier-section__label">Precedent</h3>
            {match && <p className="dossier-section__body">{match}</p>}
            {difference && <p className="dossier-section__body dossier-section__body--difference">{difference}</p>}
          </section>
        )}
        {state === 'ready'
          ? <DecisionSection label="Possible response">{response}</DecisionSection>
          : (
            <section className="dossier-section dossier-section--withheld">
              <h3 className="dossier-section__label">Possible response</h3>
              <p className="dossier-section__body">
                Withheld until the evidence is ready. {proofLine(evidence)}
              </p>
            </section>
          )}
      </div>
      <footer className="dossier-lead__footer">
        {onEvidence && (
          <button type="button" className="dossier-action" onClick={onEvidence}>
            {evidenceLabel}
          </button>
        )}
      </footer>
    </article>
  );
}
