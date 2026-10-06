# Evidence

## Live staging

The passcode gate leads with four bouncing Google dots, the line “Google × Ogilvy · Signal desk”, a glow, a glass card, and a Google blue action. The first impression therefore belongs to Google, not Ogilvy.

The authenticated header repeats “Google × Ogilvy”, exposes four Google colour swatches, carries light and dark controls, and retains a multicolour route system. The product mark is a coloured “42.” rather than a coherent Ogilvy Intelligence lockup.

Today opens with a search field and static Try prompts before the recommendation. The same page then presents metric tiles, a lead card, ranked rows, market cards, controls, and a large footer route wall. The user must translate the hierarchy into a decision path.

At 390 pixels, navigation and content compete for the viewport, the fixed mobile bar consumes permanent space, and some search content clips. Empty data is represented by dashboard chrome and zero-like structures rather than an editorial explanation of what did not run.

## Repository proof

| Finding | Source |
|---|---|
| Google brand colours are a declared product feature | frontend/src/today.jsx:15 |
| Theme dock contains the four Google colour swatches | frontend/src/today.jsx:87 |
| Masthead says Google × Ogilvy | frontend/src/today.jsx:153 |
| Passcode gate declares a four-dot Google mark | frontend/src/passcode.jsx:1 |
| Passcode gate says Google × Ogilvy | frontend/src/passcode.jsx:30 |
| Gate animates the four Google colours | frontend/src/styles/gate.css:39 |
| App boot uses four dots and a coloured 42 mark | frontend/src/App.jsx:402 |
| Footer repeats Google × Ogilvy | frontend/src/App.jsx:456 |
| Footer exposes a route wall across three columns | frontend/src/App.jsx:101 |
| Four motion levels are persistent product controls | frontend/src/App.jsx:475 |
| Background glow is always rendered | frontend/src/App.jsx:398 |
| Static Try prompts are part of the command hero | frontend/src/today.jsx:211 |
| Google Fonts are loaded from an external Google domain | frontend/index.html:10 |
| Current CSS contains repeated glow, radius, pill, gradient, dot, and loader grammars | frontend/src/app.css and frontend/src/styles/gate.css |

## Ogilvy identity research

Ogilvy’s own identity case study describes a move from bright pink, yellow, and blue to classic black and red. It identifies Ogilvy red, black, typography, tartan, and intersections as the core system. The tartan is intended to connect heritage and future and to represent intersections across capabilities.

Source: https://www.ogilvy.com/ideas/how-ogilvy-485-created-agencys-updated-visual-identity-system

Ogilvy positions itself around culture-changing, value-driving ideas, Cultural Discovery, and Borderless Creativity. The product should therefore feel like a place where cultural evidence becomes an idea, not a monitoring console.

Source: https://www.ogilvy.com/about

Ogilvy Consulting describes its work through intelligent and imaginative answers, behavioral science, demand analytics, systemic thinking, and measurable outcomes. The interface needs to join analytical discipline with creative judgment.

Source: https://www.ogilvy.com/capabilities/consulting

## Research rejected

The generic design database recommendation was a blue and orange data-dense dashboard with Fira typography. That direction is rejected because it reproduces the category default.

Also rejected:

1. Brutalism and neobrutalism, because they read as fashionable youth branding rather than durable intelligence.
2. Futuristic HUD language, because it makes the system look speculative and hides ordinary evidence behind theatre.
3. Aurora gradients, glows, glass cards, and floating pills, because they are the current AI product cliché.
4. A literal scrapbook or detective dossier, because it turns provenance into costume.
5. A direct copy of the corporate Ogilvy website, because 42 needs a product-specific interaction model.

