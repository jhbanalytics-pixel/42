# Verdict and recommended direction

## Brand

Use the name:

42  
Ogilvy Intelligence

The 42 mark behaves like an editorial folio number, not a startup logo. “Ogilvy Intelligence” carries ownership. Google is removed from visible copy, marks, colour, fonts, loading language, metadata, and exports.

Use the official Ogilvy wordmark asset only when it is available through the approved internal brand library. Do not redraw it from memory. Until then, set “Ogilvy Intelligence” as text.

## Recommended concept: The Red Thread

The interface is built around one visible red evidence thread. It connects:

Signal → Why now → Receipts → Precedent → Possible response → Brief

The thread is not decoration. Every intersection marks a real relationship: two sources agree, a signal crosses markets, history repeats, evidence contradicts, or a response cites a receipt.

The visual world is ink black, warm paper, white, and Ogilvy red. Semantic amber and green appear only inside evidence states. There are no user-selectable brand colours.

The system combines an editorial front page with a working evidence margin. It should feel like a sharp strategy team has marked up the morning’s cultural evidence together.

## Alternative concepts considered

### Evidence Press

A newspaper and research-journal system with strong folios, columns, marginalia, and source notes. It is credible and readable, but can feel too passive for an interactive product.

### Studio Wall

A structured working wall where signals, receipts, analogues, and responses occupy an intersection grid. It is expressive and collaborative, but risks clutter and weak mobile translation.

The Red Thread takes the editorial authority of Evidence Press and the relational intelligence of Studio Wall without copying either metaphor literally.

## Product architecture

The global navigation has five jobs:

1. Briefing: what changed, why now, proof, and possible response.
2. Discover: open signals and emerging clusters.
3. Compare: signals, markets, sources, and historical analogues.
4. Build: Intelligence Console, Research, and client brief.
5. Fieldwork: Source Lab, spend, source readiness, and method.

Historical Intelligence is not a separate destination by default. It appears where a precedent changes the decision and remains available as a deeper archive.

The first screen contains one lead decision path and a compact queue. It does not begin with engine totals, a generic search field, or static Try prompts.

## Three interface grammars

### Editorial lead

One signal occupies the primary canvas. It contains the signal, why now, decisive proof, contradiction state, historical precedent, and a possible response. The recommendation appears before secondary metrics.

### Evidence ledger

Receipts are compact rows with source, observed claim, market, closed window, independence, quality, and direct link. Ready, thin, contradictory, and unchecked are structural states, not coloured badges added after the fact.

### Comparison strip

Signals and options compare in aligned rows. The same field always occupies the same column. Charts are used only when a shape over time changes the decision.

No other card family is permitted without a design review.

## Design principles translated into rules

| Principle | Product rule |
|---|---|
| Rams, useful and honest | A claim cannot look complete when receipts are thin, contradictory, or unchecked. |
| Müller-Brockmann, grid and rhythm | The grid aligns the argument. Source, time, market, and response relationships share repeatable columns. |
| Vignelli, semantic discipline | A visual device has one meaning everywhere. Red means the evidence thread and primary action, not decoration. |
| Tufte, evidence density | Remove metric tiles that do not change a decision. Show direct observations and useful comparisons instead. |
| Wurman, information architecture | Organize by the strategist’s question, not by the engine’s modules. |
| Gestalt, continuity and proximity | The red thread joins evidence that belongs together. Spacing separates different claims. |
| Norman, affordance and feedback | Every interactive row signals what opens, what changes, and where focus will move. |
| Progressive disclosure | Lead with the decision, then receipts, then complete lineage. Never hide the evidence state. |
| WCAG | Keyboard, focus, contrast, reduced motion, reflow, zoom, and touch targets are release gates. |
| Ogilvy identity | Red, black, typography, and intersections are functional elements tied to cultural evidence. |

## Type

First choice is the approved Ogilvy Serif and Ogilvy Sans family from the internal brand source.

If those files are unavailable, use a local fallback stack and ship no external Google Fonts request. Serif is reserved for interpretation and signal titles. Sans is used for navigation and working text. Mono is restricted to provenance, dates, source IDs, and numeric alignment.

No more than eight text sizes are allowed. Component files contain no raw font sizes.

## Colour

Brand palette:

| Token | Use |
|---|---|
| Ink | Primary text, structural rules, dark canvas |
| Paper | Main reading canvas |
| White | High contrast field and reverse text |
| Ogilvy red | Evidence thread, active position, primary action |
| Amber | Thin or unchecked evidence |
| Green | Ready evidence only |

No gradients, glow, multicolour identity dots, route colour lanes, colour picker, or decorative opacity wash.

## Shape and surface

Default radius is 0. A 2 pixel radius is allowed for form controls where it improves focus rendering. Pills are reserved for a true binary or categorical state and never used as layout decoration.

There are two elevations: canvas and overlay. Borders organize data, not every component.

## Motion

One motion idea is allowed: the red thread resolves as evidence becomes available or as focus moves from claim to receipt. Duration stays between 120 and 220 milliseconds. Reduced motion removes the transition without removing meaning.

Delete boot animation, bouncing dots, blinking live markers, idle ticker movement, loader theatre, hover lift, and user-facing motion controls.

## Responsive rule

At 390 pixels, the first useful recommendation begins within 720 pixels. One proof cue and one movement cue remain visible. Navigation does not stay fixed across the content. Evidence drawers become full-screen reading sheets with deterministic focus return.

## Honest empty states

An empty surface says what is absent, which run and closed window were checked, why it is absent when known, and the next valid action. It never renders a wall of zero metrics or an animated scanner.

## Anti-slop acceptance test

The design fails if any answer is yes:

1. Could the screen belong to an unnamed AI startup after changing the logo?
2. Does the page use glass, aurora, glow, floating pills, or gradient atmosphere?
3. Is a rounded card the default answer to grouping?
4. Does a colour exist without a stable semantic meaning?
5. Does motion continue while the user is idle?
6. Does the screen begin with a generic chat or search box?
7. Does the product expose a model, vendor, table, score, or internal route before the strategist’s question requires it?
8. Does an unsupported audience claim appear more confident than unavailable?
9. Is an empty state pretending that zero is intelligence?
10. Are there more than three visual grammars on one screen?
11. Does mobile preserve desktop chrome at the expense of the recommendation?
12. Is any Google name, logo, colour system, font request, or product metaphor still visible?

