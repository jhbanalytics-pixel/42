# Fable spec for a 10/10 (round 2 review of gamma-v3.html, score 8.0)

Owner requirements (Albert, verbatim intent): "really active animations, smooth transitioning with motion, proper design principles, I need to be like wow this is insane"; "3D animation loading effects where applicable, really wow loading effects, like when Ask is busy"; loves these hover effects from https://prismic.io/blog/css-hover-effects (#11 profile card hover, #16 social icons hover, #19 isometric social tiles, #20 text and gradient icon hover, #21 creative menu hover, #24 wavy menu line, #25 desktop menu animation with vertical MENU, #38 cubes hover) and wants a superset or enhanced best versions; hates text wrapping into a narrow column beside empty space ("cut off sentences").

## Why not 10 now
- Detection failed morning at 390: scrollWidth 404 (top bar too wide).
- Phone post tray (z 25) covers dock (z 20); its actions below the fold; no handle, no scrim.
- 7 blocks end in one-word orphans; Compare and item serif sublines wrap into 40ch beside empty space; phone h1 breaks at hyphen ("Gogo side-/eye").
- Run card on phone: four .runsteps overflow and collide.
- Palette buttons shrink-wrap (display:flex on button does not stretch).
- No press states; nav hover and current nearly identical.
- Readings strip has no semantics; each Today card button name ~300 chars; failed morning shows a bare "—".
- Font fallback: when Geist Mono is late, rail labels wrap and the bar jumps.
- Motion: one fade and a bar grow; nothing transitions between views.
- Specimen frames are empty dark boxes.

## Global
1. Type tokens: h1 clamp(2.25rem,4.2vw,3.5rem)/1.02 600 -.035em; h2 18/1.3 600; body 15/1.55; small 13/1.45; eyebrow 12/1.3 500 +.08em; mono reading clamp(2rem,3.4vw,2.75rem)/1 500 -.04em; serif subline clamp(1.375rem,2.1vw,1.75rem)/1.28. Spacing scale 4/8/12/16/24/32/48/64 only (replace 10,14,18,22,26 in .card,.record,.runcard,.tray,.verdict).
2. Measure and rag: p,.why,.note,.b,.r,.q,.gaps span,.tx span,.ghost{text-wrap:pretty}; h1,h2,.ttl,.serif{text-wrap:balance}; h1{hyphens:manual;overflow-wrap:normal}; wrap hyphenated compounds in item titles in <span class="nb"> (white-space:nowrap). Measures: body 60-70ch (.why 62ch; .issue p,.ghost,.gaps span,.claims .tx span 68ch); .note 60ch; serif max-width:min(44ch,100%); single-column heroes (Item, Compare, Ask, Loading, Dossier, Concepts) .hero{max-width:820px} so the serif sits in a measured column not beside a void. Ask answer serif 52ch. Loading .ghost 13.5px Geist (not mono) 64ch. Acceptance: no last line under 25% of block width unless one line.
3. States: hover 140ms; press transform:translateY(1px)+background:var(--bg-2) 80ms; selected uses signal; focus-visible 2px signal offset 3. :active on .btn,.seg button,.nav button,.spec,.card,.ev button,.pal button. Nav hover = text to --ink + left 2px --line-2 rule; current = left 2px --signal rule, number in signal, no border box.
4. Font fallback: @font-face{font-family:"Geist Mono Fallback";src:local("Menlo"),local("DejaVu Sans Mono");size-adjust:92%;ascent-override:95%} in --mono; .step .lbl,.proto label{white-space:nowrap}; .topin{height:56px;overflow:clip}.
5. Crosshair corners: all four or none.
6. Demote prototype chrome: .proto loses dashed amber border; plain select with 12px mono label SCENARIO in --ink-3. Amber for data issues only.
7. Failed readings: replace "—" with <span class="v dim">Not judged</span> (ui 15px).
8. Readings strip: <dl class="readings"> with <div class="rd"><dt class="eyebrow">..</dt><dd class="v">..</dd><dd class="b">..</dd></div>.
9. Today cards: <article class="card"> with <h3><button class="ttl" data-open> as the only name-bearing control (whole card still clickable via JS); tape and meter role=img with short labels; long tape sentence via aria-describedby.

## Motion system (all compositor-only; all off under prefers-reduced-motion with identical final states; roll prints final number)
10. Tokens: --e-out cubic-bezier(.22,1,.36,1); --e-digit cubic-bezier(.34,1.45,.64,1); --e-in-out cubic-bezier(.65,0,.35,1); --t-fast 140ms; --t-mid 280ms; --t-view 360ms; --t-data 520ms. Nothing over 600ms except ambient loops.
11. View transitions: wrap render() in document.startViewTransition when available. view-transition-name on hero h1 (hero-title), the tapped card's tape (tape-<id>) morphing into the item page big tape, the confidence chip, and the market segment selected background (seg-thumb, a pseudo element sliding between tabs). ::view-transition-old(root) fade-out 180ms --e-in-out; ::view-transition-new(root) fade-up 360ms --e-out (translateY 8px). Group morphs 360ms --e-out. Fallback: .in class.
12. Staggered entrances: .col > * animation in 280ms --e-out both, delay calc(var(--i)*40ms), cap 8. Watching cards stagger 60ms.
13. Data-driven: tape bars grow bottom-up left to right delay i*14ms (392ms sweep), today's bar last with a lime flash to --signal then settle; NO DATA label fades in after hatch (200ms). Numerals roll with --e-digit 420ms. Meter segments fill left to right 60ms apart; first empty segment pulses once (opacity .4 to 1, 600ms). Day clock: cells fill 1..N 35ms stagger, ring draws around today last (box-shadow 0 to 2px, 200ms). Loading pipeline: step dots fill in order; .pipe .run gets a 1px scanning line on its bottom edge (linear-gradient(90deg,transparent,var(--signal),transparent) 0 100%/40% 1px no-repeat, scan 1.6s linear infinite).
14. Hover: card title shifts 2px right and left signal rule draws top to bottom (scaleY 0 to 1, 180ms); spec cards lift translateY(-2px) with box-shadow 0 8px 24px -16px rgba(0,0,0,.6); readings numerals --ink to --signal on cell hover; .btn.pri lime underline grows from left (background-size 0 to 100%, 200ms).
15. Cursor-aware light on frames: .frame --mx/--my updated on pointermove (rAF-throttled, pointer:fine only); a .glow child with radial-gradient(260px circle at var(--mx) var(--my), color-mix(in srgb,var(--signal) 9%,transparent), transparent 70%), opacity 0 to 1 over 200ms on hover; pointer-events none; never behind the tape (tape on solid --panel). Skip on touch and reduced motion.
16. Scroll reveals only under @supports(animation-timeline:view()): .sec{animation:reveal linear both;animation-timeline:view();animation-range:entry 0% entry 35%} opacity .6 to 1, translateY 6px to 0. @media print{*{animation:none!important}}.
17. Ambient: freshness LED breathes (opacity .7 to 1, 3s); hero crosshair "+" marks drift 1px (6s). No particles.
18. Perf: only transform, opacity, background-position, small box-shadow; will-change only during transitions; content-visibility:auto on below-fold sections; no frame over 16.7ms in a view transition at 4x CPU throttle.

## 3D working states: one hand-written canvas 2D isometric lattice (no library, < 6 kB, aria-hidden canvas + role=img wrapper with text twin, static final frame under reduced motion, rAF paused by IntersectionObserver and visibilitychange)
19. 3x3x3 lattice of 27 cubes (edge 18px desktop, 12px phone), 3 shaded faces per cube (top --panel, left --bg-2, right --raise, edges --line-2 1px), fixed iso camera (35.264°/45°), slow yaw 0.08 rad/s (fake via projection). Three columns tinted by market with 12px mono labels ZA NG KE. Posts are 3px dots flying in along iso axes (320ms --e-out) landing on a face; a checked cube flashes --signal 240ms; failed check shows a --bad cross; uncollected lane cubes render hatched.
20. Ask running: step 01 lights the lattice face by face; step 02 pulls 41 dots over 1.8s; step 03 flashes 6 checked cubes 120ms apart; step 04 draws a 1px lime outline around the lattice (380ms). Log rows animate in from the lattice's right edge. Canvas left of the log at 220x220 desktop, above it at 160px on phone, never over the answer; it settles to its final frame and stays.
21. Loading morning: same lattice at 160px inside .pipe .run area, cubes filling column by column as Detect progresses (fake 0 to 90% over 20s then hold). Ghost text stays readable beside it.
22. No 3D on navigation.
23. Acceptance: paused off-screen; reduced motion shows one static final frame; role=img + text twin; ≤4ms main thread per frame throttled.

## Prismic effects mapped
24. #19 isometric slabs: market switcher buttons are 3px extruded slabs (box-shadow:3px 3px 0 var(--line-2)); hover translate(-1px,-1px) shadow 4px; selected pressed flat translate(2px,2px) shadow 1px + lime underline; 160ms --e-out; focus same lift; no skew.
25. #38 cube cluster: the lattice above, with pointer parallax ±4° yaw on desktop (lerp 0.08), no hue-rotate.
26. #11 profile card slide: Today card hover reveals a line under the title ("Next check Tomorrow 05:15 · 3 posts today") sliding up 10px / fading 200ms; facts also stay in the right column for keyboard users.
27. #16 icon fill: the three item actions get 16px inline SVG icons (dossier, question, compare); on hover the icon's circle fills bottom-up (::before translateY 100% to 0, 200ms). No 360 flip.
28. #20 text and gradient icon: phone dock tapping expands the label; desktop nav hover shows a soft lime glow (blur 8px, opacity .25) behind the current number only.
29. #21 creative menu: palette rows get ::before border lines scaling in from left on hover/focus (scaleX 0 to 1, 180ms) and a background sweep to --bg-2; .pal button{width:100%}.
30. #24 wavy line: current-page indicator in nav and dock is a 2px SVG sine line (data URI) with background-position animating 1.2s linear infinite; static under reduced motion. Replaces the straight dock rule.
31. #25 vertical MENU: at 960 to 1240px collapse the nav to a 48px rail with rotated "MENU" label; hover or focus slides the full 184px nav out (translateX 240ms), items staggered 30ms. Below 960 the dock stays.

## Today
32. Failed morning at 390: fresh chip "STOPPED 05:02"; .fresh{max-width:40vw;overflow:hidden;text-overflow:ellipsis}; .proto select{max-width:96px} under 640. scrollWidth 390 in all five mornings.
33. .runsteps ≤640: 2x2 grid, min-width:0, overflow-wrap:anywhere.
34. Replace "Checks: trust gate, one for every screen." with "Every screen passed its trust check." (or drop; in failed morning say what did not run).
35. Legend: "collected, not seen" only.
36. Two-column hero: serif 40ch fine there; .herogrid gap 48px; align run card first baseline with the eyebrow.

## Item
37. Tape scale label collides with dotted top rule: lift 4px with a left tick; add a right tick at 10 (half).
38. Specimen frames: 1px mono timeline bar across the bottom third (ticks every 10s, played portion --ink-2) and platform mark top-left; scanlines 4%.
39. Verdict: add day fraction under the word ("Day 10 of 14", 13px mono) as a text twin of the clock.

## Ask
40. Input becomes <textarea rows=1> auto-grow (field-sizing:content + JS fallback); never clips on phone.
41. Answer serif 52ch; claim rows 150px 1fr; "+2" button aria-label "2 more posts".

## Compare
42. ZA real-zero panel draws the same 28-cell tape (hatch 9 days, flat zero baseline 19 days) instead of a box; Kenya panel draws the tape at 30% opacity under the hatch with the not-comparable label.
43. Single-column hero, serif 44ch, .hero max-width 820px.

## Dossier
44. Empty state: 3-row ghost list (dotted frames, no text) under the sentence. Checkbox 22px with 44px label hit area.
45. Approve disabled until ticks complete, and says why ("Approve · 2 ticks needed").

## Palette, tray, toast
46. .pal button width 100%; number column right-aligned 12px; hint row "↑↓ move · Enter open · Esc close" (arrow keys must work).
47. Phone tray: z-index 30, padding-bottom calc(74px + safe area), 36x4 handle, scrim (40% --bg), actions sticky bottom on --raise.
48. Toast: top-right under the bar on desktop, bottom on phone; Undo button 44px.

## Light theme
49. Light tapes: .tc.last .bar{outline:1px solid var(--signal)}.

## Remove
- Dashed amber .proto box. Grain in light to 3%. Two-of-four crosshairs. In loading morning drop the dot pulse (keep scan line and lattice). Concepts C SVG text 12px.

## Acceptance checklist for 10
1. scrollWidth===390 in 5 mornings x 3 markets, every view, both themes, with and without tray.
2. Google Fonts blocked: top bar stays 56px, no label wraps, CLS ≤0.05.
3. Every text block: last line ≥25% width or single line; no h1 breaks at hyphen; measures as specified; zero orphans at 1440, 1280, 390.
4. :active exists on every button, a, [role=tab]; hover, press, selected, focus distinct.
5. A11y tree: readings is a dl; each card exposes one button with ≤80 char name; tape and meter role=img with text; no "—" without name; dialog traps and returns focus; Escape order palette then tray.
6. Contrast: text ≥4.5, UI boundaries ≥3.0 on bg, panel, raise, warn-bg, both themes.
7. 24px minimum targets including checkboxes.
8. Phone tray: actions visible without scrolling at 844px; dock not reachable under sheet; focus returns.
9. Motion: getAnimations() shows stagger, tape sweep, roll, meter fill, clock tick with stated durations; reduce = zero animations and identical final states.
10. View transition Today to Item: startViewTransition called, tape-<id> both sides, frames ≤16.7ms at 4x throttle.
11. Canvas lattice in Ask running and loading morning, role=img with text twin, paused off-screen, static under reduce, ≤4ms/frame throttled.
12. Each of the eight prismic effects has a keyboard-focus twin and reduced-motion state.
13. Honesty rules hold in all states; illustrative notice on every view; no em dash standing in for a value.
14. Light theme parity screenshots for every state; no lime on paper without a 1px signal edge.
