/* DigestList: the desk aside's tappable topic rows (round 2 handoff).
   One row per topic, label verbatim, a hash link into /topic/<query>.
   When the chip carries a market, switch the desk region before navigating so
   cross-market digest topics resolve in the right slice. */
export function DigestList({topics, variant, setRegion}){
  const rows = (Array.isArray(topics) ? topics : []).filter((t) => t && t.query && t.label);
  if (!rows.length) return null;
  const openChip = (t) => {
    if (t.market && setRegion) {
      setRegion(String(t.market).toUpperCase());
    }
    window.location.hash = '#/topic/' + encodeURIComponent(t.query);
  };
  return (
    <div className="digest-list">
      {rows.map((t) => (
        <a key={(t.market || '') + ':' + t.query} href={'#/topic/' + encodeURIComponent(t.query)}
          onClick={(e) => { e.preventDefault(); openChip(t); }}
          className={'digest-list-row digest-list-row--' + (variant === 'rising' ? 'rising' : 'key')}>
          {variant === 'rising'
            ? <svg className="digest-list-marker" viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="var(--up)" strokeWidth="2.6" aria-hidden="true"><path d="M6 15l6-6 6 6" /></svg>
            : <span className="digest-list-bullet" aria-hidden="true" />}
          <span className="digest-list-label">{t.label}</span>
          <svg className="digest-list-chev" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="var(--faint)" strokeWidth="2" aria-hidden="true"><path d="M9 6l6 6-6 6" /></svg>
        </a>
      ))}
    </div>
  );
}
