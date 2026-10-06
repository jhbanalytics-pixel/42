/* PULSE ui · Skeleton: one shape-matched loading placeholder. A page composes
   its own skeleton layout from repeated calls rather than importing five
   separate components. variant sets the base shape; width / height override
   dimensions; count repeats the block (for text lines or list rows). The
   shimmer honours the motion system and prefers-reduced-motion via ui.css.

   variant="hero" is a composed shape (title bar + sub bar + a railRows-sized
   block), not a VARIANT_DEFAULT entry, since it renders three pieces instead
   of one: titleWidth / subWidth / railRows size each piece. Used by any route
   whose loading state is a hero cover (topic dossier, desk top band). */

const VARIANT_DEFAULT = {
  text:   {width: '100%', height: '0.9em'},
  metric: {width: '96px', height: '38px'},
  card:   {width: '100%', height: '150px'},
  row:    {width: '100%', height: '56px'},
  chart:  {width: '100%', height: '240px'},
};

/* className is additive (appended to the base "ui-skeleton" / "ui-skeleton-hero"
   class), so callers can stamp the shared "dSkel" DOM-probe hook used by the
   V3 desk reveal (see design_handoff_pulse_v3, Pulse Desk acceptance: board
   rows and metric cells must expose a .dSkel node at first paint) without a
   new variant per call site. */
export function Skeleton({variant = 'text', width, height, count = 1, titleWidth = '60%', subWidth = '40ch', railRows = 3, className}){
  const cls = (base) => className ? base + ' ' + className : base;
  if (variant === 'hero'){
    const rows = Math.max(1, railRows | 0);
    return (
      <div className={cls('ui-skeleton-hero')} aria-hidden="true">
        <span className="ui-skeleton" data-variant="hero-title" style={{width: titleWidth, height: '46px'}} />
        <span className="ui-skeleton" data-variant="hero-sub" style={{width: subWidth, height: '0.9em'}} />
        <span className="ui-skeleton-group">
          {Array.from({length: rows}, (_, i) => (
            <span key={i} className="ui-skeleton" data-variant="hero-rail-row" style={{width: '100%', height: '20px'}} />
          ))}
        </span>
      </div>
    );
  }
  const base = VARIANT_DEFAULT[variant] || VARIANT_DEFAULT.text;
  const n = Math.max(1, count | 0);
  const items = [];
  for (let i = 0; i < n; i++){
    items.push(
      <span
        key={i}
        className={cls('ui-skeleton')}
        data-variant={variant}
        aria-hidden="true"
        style={{
          width: width != null ? width : base.width,
          height: height != null ? height : base.height,
        }}
      />,
    );
  }
  return n === 1
    ? items[0]
    : <span className="ui-skeleton-group">{items}</span>;
}
