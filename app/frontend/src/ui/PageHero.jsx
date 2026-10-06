/* PULSE ui · PageHero: the route header. This is the ONE place that implements
   the width rule from Global Constraint 6 so the Discover/Explorer dead-
   whitespace bug cannot recur route by route.

   The text column and any actions row share a flex parent. The text side gets
   min-width: 0 so it can shrink, and the sub-text fills up to min(72ch, 100%)
   of its OWN column rather than being pinned to a fixed narrow width against
   the full grid. eyebrow / title / sub / actions are all optional. Anything
   passed as children reads with the sentence it explains, under the sub in
   the title column (the Seeds strength key). */

export function PageHero({eyebrow, title, sub, actions, children}){
  return (
    <header className="ui-page-hero">
      <div className="ui-page-hero-text">
        {eyebrow && <div className="ui-page-hero-eyebrow eyebrow">{eyebrow}</div>}
        {title && <h1 className="ui-page-hero-title serif">{title}</h1>}
        {sub && <p className="ui-page-hero-sub">{sub}</p>}
        {children}
      </div>
      {actions && <div className="ui-page-hero-actions">{actions}</div>}
    </header>
  );
}
