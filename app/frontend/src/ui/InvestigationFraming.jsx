/* Framing a new investigation.

   The other half of the door. It collects only what the fixed creation request
   requires, and it echoes the scope identity the server gave it rather than
   guessing brand or theme, because the creation call refuses unless those
   match the configured values exactly and the browser is not where that is
   known.

   Section 7 lands here, on screen. With no released run this surface does not
   render a disabled form: it renders no form at all, and says why. A disabled
   control still describes an action that is available in principle, and the
   whole point is that it is not. Nothing starts on load, on typing, or on
   anything but a person choosing to submit. */

import '../styles/console.css';
import {useRef, useState} from 'react';
import {buildFramingState} from '../investigationIndex.js';

export function InvestigationFraming({payload = null, loading = false, error = null, onFrame}){
  const view = buildFramingState({payload, loading, error});
  const scope = view.scope;
  const inFlight = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(null);

  return (
    <div className="page investigation-framing" data-framing-state={view.state}>
      {/* Quiet register, 23 Sept 2026: the page head names the page "Build
          brief" as its one h1, so the framing is a section under it. The
          uppercase "Intelligence Console" eyebrow repeated the route and is
          gone (rule 5 and sign 2). */}
      <header className="investigation-folio">
        <h2 className="investigation-folio__title">Frame an investigation</h2>
      </header>

      {view.state === 'loading' ? (
        <p className="investigation-state" aria-busy="true">Reading the available scope.</p>
      ) : null}

      {view.state === 'error' ? (
        <p className="investigation-state" role="alert">
          The investigation scope is unavailable, so nothing can be framed against it.
        </p>
      ) : null}

      {view.state === 'unsupported_contract' ? (
        <p className="investigation-state" role="alert">
          This desk cannot read the version this scope is stored in.
        </p>
      ) : null}

      {/* No form, not a disabled one. A disabled control still says the action
          exists and is merely unavailable now; this says the ground it would
          stand on does not exist yet. */}
      {view.state === 'no_released_run' ? (
        <section className="investigation-state" role="alert">
          <p>{view.reason}</p>
          <p>
            An investigation is framed against a completed run. Until one is released there is
            nothing to frame against, and creating one anyway would rest it on a run nobody made.
          </p>
        </section>
      ) : null}

      {view.state === 'ready' && scope ? (
        <form
          className="investigation-frame"
          onSubmit={async (event) => {
            event.preventDefault();
            if (!onFrame || inFlight.current) return;
            const data = new FormData(event.currentTarget);
            inFlight.current = true;
            setSubmitting(true);
            setSubmitError(null);
            try {
              await onFrame({
                decisionQuestion: String(data.get('decision_question') || ''),
                markets: data.getAll('market').map(String),
                timeHorizonDays: Number(data.get('time_horizon_days')),
              });
            } catch (error){
              setSubmitError(error?.message || 'The investigation could not be framed. Try again.');
            } finally {
              inFlight.current = false;
              setSubmitting(false);
            }
          }}
        >
          <label className="investigation-field">
            <span className="investigation-field__label">What decision does this inform?</span>
            <textarea name="decision_question" rows={3} required />
          </label>

          <fieldset className="investigation-field">
            <legend className="investigation-field__label">Markets</legend>
            {/* Exactly the markets this scope allows. The label is for the
                reader and the value is the identifier the request needs. */}
            {(scope.market_scope || []).map((market, index) => (
              <label className="investigation-market" key={market}>
                <input type="checkbox" name="market" value={market} />
                <span>{(scope.market_labels || [])[index] || market}</span>
              </label>
            ))}
          </fieldset>

          <label className="investigation-field">
            <span className="investigation-field__label">Time horizon in days</span>
            <input type="number" name="time_horizon_days" min="1" defaultValue="30" required />
          </label>

          <p className="investigation-frame__note">
            Framing records the question and its scope. It starts no research or model call.
          </p>

          {submitError ? <p role="alert" className="investigation-state">{submitError}</p> : null}
          {!onFrame ? <p className="investigation-state">Framing is unavailable in this workspace.</p> : null}
          <button type="submit" className="investigation-frame__submit" disabled={!onFrame || submitting} aria-busy={submitting ? 'true' : undefined}>{submitting ? 'Framing investigation' : 'Frame this investigation'}</button>
        </form>
      ) : null}
    </div>
  );
}

export default InvestigationFraming;
