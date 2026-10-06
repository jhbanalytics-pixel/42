/* PULSE ui · MomentumPill: state pill for a trend's momentum. Maps to the four
   momentum tokens already in tokens.css (--rising / --building / --steady /
   --cooling). No new color tokens; these four exist. */

const STATES = {
  rising:   {label: 'Rising',   color: 'var(--rising)'},
  building: {label: 'Building', color: 'var(--building)'},
  steady:   {label: 'Steady',   color: 'var(--muted)'},
  cooling:  {label: 'Cooling',  color: 'var(--cooling)'},
  /* An absent momentum is not a steady one. Reading unknown as steady stated a
     measurement nothing made, and contradicted EvidenceReadiness, which reads
     its own unknown as unchecked. */
  unmeasured: {label: 'Unmeasured', color: 'var(--muted)'},
};

export function MomentumPill({state}){
  const meta = STATES[state] || STATES.unmeasured;
  return (
    <span className="ui-momentum-pill" style={{'--pill-color': meta.color}}>
      <span className="ui-momentum-dot" aria-hidden="true" />
      {meta.label}
    </span>
  );
}
