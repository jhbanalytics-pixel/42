/* SeedishCard: the one shared card grammar for the Intelligence Loop
   (Discover -> Explorer -> Seeds). Serif headline, mono meta, accent
   evidence links, fit bars. Each stage keeps a distinct lane by structure
   (what sits in the card body), not by hue: the grammar itself never
   changes color per stage. Real payload fields only; a caller that has no
   value for a piece just omits that prop and the zone does not render. */
import './styles/loop.css';

export function LoopMeta({children}){
  return <div className="loop-meta">{children}</div>;
}

export function LoopStageChip({stage, active, onClick}){
  return (
    <button type="button" className={'loop-stage' + (active ? ' is-active' : '')} onClick={onClick}>
      {stage}
    </button>
  );
}

export function FitBars({fit}){
  const rows = Array.isArray(fit) ? fit : [];
  if (!rows.length) return null;
  return (
    <div className="loop-fitgrid">
      {rows.map((f) => (
        <div className="loop-fitrow" key={f.label}>
          <div className="loop-fitrow-head">
            <span className="loop-fitrow-label">{f.label}</span>
            <span className="loop-fitrow-value">{typeof f.value === 'number' ? f.value.toFixed(2) : f.value}</span>
          </div>
          <div className="loop-fitrow-track">
            <div className="loop-fitrow-fill" style={{width: (Math.max(0, Math.min(1, f.value || 0)) * 100) + '%'}} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function EvidenceLink({market, label, onClick}){
  return (
    <button type="button" className="loop-evidence" onClick={onClick}>
      {market && <span className="loop-evidence-market">{market}</span>}
      <span className="loop-evidence-label">{label}</span>
      <span className="loop-evidence-open">Open &rarr;</span>
    </button>
  );
}

/* SeedishCard: card shell shared by every stage. `kicker` carries the stage-
   specific mono meta row (market/lane/status/score for Discover, trace path
   for Explorer, SEED nn/strength for Seeds); `headline` is always the serif
   entity name; `body` is the stage's own structural content (why text, fit
   bars, evidence, activation block); `cta` is the stage's forward action. */
export function SeedishCard({kicker, headline, sub, body, cta, laneBorder}){
  return (
    <article className={'loop-card' + (laneBorder ? ' loop-card-accented' : '')}>
      {kicker && <div className="loop-card-kicker">{kicker}</div>}
      {headline && <h2 className="loop-card-headline">{headline}</h2>}
      {sub && <p className="loop-card-sub">{sub}</p>}
      {body}
      {cta && <div className="loop-card-cta">{cta}</div>}
    </article>
  );
}

export default SeedishCard;
