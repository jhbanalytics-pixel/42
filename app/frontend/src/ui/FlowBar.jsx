/* PULSE ui · FlowBar: the pinned console flow strip. Shows the current step
   of a named flow, market scope toggles, and one primary CTA slot so the
   console's step, scope, and outcome pin to the viewport instead of scrolling
   away. Renders nothing without a real steps array (DATA_SPEC): a flow with
   no steps has no flow to show.

   status idle|active|busy|done drives role="status" aria-live="polite" so a
   step change or busy transition is announced without a live region on every
   child. The CTA is a real button; while busy it is natively disabled so a
   click can never double-fire the running action, with aria-disabled
   mirrored for AT that announces the attribute rather than the state. A
   guard-blocked CTA still renders (disabled, not omitted) with an optional
   cta.title surfaced as the button's title attribute so the guard reason is
   named rather than silently hidden.
   Scope chips are true toggles (aria-pressed), not a
   radio group, since the caller decides whether more than one can be on. */

export function FlowBar({step, steps, scope, status = 'idle', cta}){
  const list = Array.isArray(steps) ? steps : [];
  if (list.length === 0) return null;
  const busy = status === 'busy';
  const activeIdx = list.findIndex((x) => x.key === step);

  return (
    <div className="ui-flowbar" role="status" aria-live="polite">
      <div className="ui-flowbar-inner">
        <ol className="ui-flowbar-steps">
          {list.map((s, i) => {
            const isCurrent = s.key === step;
            const isDone = activeIdx > i;
            return (
              <li
                key={s.key}
                className="ui-flowbar-step"
                data-state={isDone ? 'done' : isCurrent ? 'current' : 'pending'}
              >
                <span className="ui-progress-rail-marker" aria-hidden="true" />
                <span>{s.label}</span>
              </li>
            );
          })}
        </ol>
        {Array.isArray(scope) && scope.length > 0 && (
          <div className="ui-flowbar-scope" role="group" aria-label="Market scope">
            {scope.map((s) => (
              <button
                key={s.code}
                type="button"
                className="ui-flowbar-chip"
                aria-pressed={!!s.on}
                onClick={s.onToggle}
              >
                {s.code}
              </button>
            ))}
          </div>
        )}
        {cta && cta.label && (
          <button
            type="button"
            className="ui-flowbar-cta"
            disabled={busy || !!cta.disabled}
            aria-disabled={busy || !!cta.disabled}
            title={cta.title}
            onClick={cta.onClick}
          >
            {cta.label}
          </button>
        )}
      </div>
    </div>
  );
}
