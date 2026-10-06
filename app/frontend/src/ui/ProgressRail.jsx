/* PULSE ui · ProgressRail: determinate progress for a real backend job (research
   brief, behaviour scan, consolidated batch). The bar width comes from the
   caller-supplied percent, derived from the status-to-milestone map in
   v3-design-system.md section 4 (starting 5, gathering_seeds 25, gathering_bq
   50, synthesizing 90, completed/completed_degraded 100).
   The component NEVER computes a percent from a timer, so the honesty rule
   stays enforceable in one place per caller.

   steps defaults to the existing PROGRESS_STEPS from researchLib; do not pass a
   redefined shape. The step list marks each step done / current / pending from
   progressStepIndex(status). completed_degraded renders at 100% with a distinct
   "ready, degraded" badge rather than a different percentage.

   Forward-only clamp lives here, once, for every consumer: a high-water mark
   ref tracks the highest percent seen for the current job, so a status read
   that resolves to a lower percent than one already shown never regresses the
   bar. The reset must be visible on the same render as the status flip, so it
   happens during render, guarded by a previous-status ref (idempotent under
   StrictMode double-render). The caller moving to a new job signals it by
   transitioning status to 'starting' or empty. Batch rows keyed by id also
   get each row its own ref from scratch. */
import {useRef} from 'react';
import {PROGRESS_STEPS, progressStepIndex} from '../researchLib.jsx';

export function ProgressRail({status, steps, percent, label, focus = false}){
  const list = Array.isArray(steps) ? steps : PROGRESS_STEPS;
  const active = progressStepIndex(status);
  const done = status === 'completed' || status === 'completed_degraded';
  const degraded = status === 'completed_degraded';
  const pct = Math.max(0, Math.min(100, Number(percent) || 0));

  const peakRef = useRef(0);
  const prevStatusRef = useRef(status);
  if (prevStatusRef.current !== status){
    prevStatusRef.current = status;
    if (status === 'starting' || !status) peakRef.current = 0;
  }
  if (pct > peakRef.current) peakRef.current = pct;
  const shown = peakRef.current;

  return (
    <div className={'ui-progress-rail' + (focus ? ' ui-progress-rail--focus' : '')} role={focus ? 'dialog' : 'status'} aria-label={focus ? 'Generating brief' : undefined} aria-live={focus ? undefined : 'polite'}>
      {focus && <div className="ui-progress-rail-dim" aria-hidden="true" />}
      <div className="ui-progress-rail-head">
        {label && <span className="ui-progress-rail-label">{label}</span>}
        <span className="ui-progress-rail-pct">
          {degraded && <span className="ui-progress-rail-badge">Ready · degraded</span>}
          <span className="ui-progress-rail-num" aria-live="polite">{Math.round(shown)}%</span>
        </span>
      </div>
      <div
        className="ui-progress-rail-track"
        role="progressbar"
        aria-valuenow={Math.round(shown)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={'ui-progress-rail-fill' + (degraded ? ' is-degraded' : '')}
          style={{width: shown + '%'}}
        />
      </div>
      <ol className="ui-progress-rail-steps">
        {list.map((step, i) => {
          const isDone = done || active > i;
          const isCurrent = !done && active === i;
          return (
            <li
              key={step.key}
              className="ui-progress-rail-step"
              data-state={isDone ? 'done' : isCurrent ? 'current' : 'pending'}
            >
              <span className="ui-progress-rail-marker" aria-hidden="true" />
              <span className="ui-progress-rail-step-label">{step.label}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
