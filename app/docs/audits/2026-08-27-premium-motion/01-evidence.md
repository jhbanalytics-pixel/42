# Evidence

## Structural

The production ready view has one global shell, one signal, one evidence rail, one precedent section, one response section, and one comparison queue. It no longer contains the old chat hero, engine metrics, duplicate board, theme controls, motion controls, or footer route wall.

The decision sequence is understandable and honest. The remaining problem is character, not information architecture.

## Visual

The current production component uses a consistent black, paper, red and serif system, but the result is static and evenly weighted. The title, evidence, precedent and response appear as adjacent editorial blocks with no meaningful transition between them.

The first actual QA view stacked all seven states vertically. That was a verification artifact rather than the production route, but it amplified the visual repetition and made the work appear generated.

The current title has no premium hover treatment. The Red Thread is a static border rather than a visible relationship or progress instrument.

The installed system contains the official font family `OgilvyJBaskerville` in regular, italic, bold and bold italic variants. The product currently uses Georgia first and therefore leaves real Ogilvy character unused.

## Interaction

The current product motion contract has three short transitions, but the ready surface exposes little of their value. The page does not provide chapter navigation, sticky context, scroll progress, or a premium title interaction.

Native `scroll-behavior: smooth` is widely available and follows user-agent conventions. CSS scroll-driven animation exists but has limited availability, so it is unsuitable as the only mechanism. Progressive enhancement should use IntersectionObserver and transform only. Reduced motion must switch anchor navigation to auto and effectively remove transitions.

Sources:

1. https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/scroll-behavior
2. https://developer.mozilla.org/en-US/docs/Web/CSS/Guides/Scroll-driven_animations
3. https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/At-rules/@media/prefers-reduced-motion

## Ogilvy identity

Ogilvy’s own identity system moved toward classic black and red, typography, tartan, and intersections. The next refinement should use intersection as behavior, not add another graphic motif.

Sources:

1. https://www.ogilvy.com/ideas/how-ogilvy-485-created-agencys-updated-visual-identity-system
2. https://wearecollins.com/case-studies/ogilvy/

## Research rejected

The generic design database recommended motion-heavy pink and cyan, Google Fonts, parallax layers, and 300 to 400 millisecond hover effects. That is rejected as category slop.

Also rejected:

1. Scroll hijacking.
2. Multi-layer parallax.
3. Full-page fades.
4. Cursor-following decoration.
5. Scaling titles.
6. Glass, glow, blur or depth effects.
7. Motion on every section.
8. CSS scroll-driven animation as a required baseline.

