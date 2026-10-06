# The 42 app, page by page

Every page in the app, what it shows and what it reads. [Back to the README](../../README.md).

The app is a React single-page app (`app/frontend/`) with hash routes, served by the `f42-api` service. The menu lives in `app/frontend/src/rail42.jsx`; below 1024 pixels it becomes a bottom bar with Today, Ask, Discover, Alerts and More. The market picker in the header (South Africa, Nigeria, Kenya) applies across pages. Themes are Light, Dark and Match system.

## Start here

### Today
The morning brief for each market: up to five cards, plus up to five more under "more". Each card carries a title, its state word, a cited explanation, the claims behind it with their labels, a count line, a sparkline against its expected band, thumbnails of the posts, and a ready question for Ask. Under the cards: every topic held back with the rule and reason, what dropped since yesterday, moments in the next 14 days, what is on the platforms' own boards, the day's coverage and today's alerts. During the first 14 days a "Warming up: day N of 14" line explains why there are no growth claims yet. From any card you can watch it or send feedback.

Reads `/api/today`, `/api/trends/{id}`, `/api/alerts`, `/api/investigations`, `/api/schedules`; writes `/api/watches`, `/api/feedback`.

### Ask
A question in plain words, answered with cited claims while the research streams in. See [the README](../../README.md#ask) and [the trust guide](trust.md) for how answers are checked.

`POST /api/ask` starts a run; `GET /api/ask/{id}/events` streams `step`, `evidence`, `claim` and `done` events (with a 2-second poll if the stream drops); `POST /api/ask/{id}/stop` stops it; `GET /api/ask/{id}/export?format=html` exports it. Starter questions come from the live trends in `/api/discover`. Forms for a brand lens, a creative context pack and creator fit sit beside the question box and appear when deep reads are switched on.

### Discover
Every live trend, by market, with tabs for hashtags, memes and topics and filters for state and platform, sorted by worth attention. Radar plots growth against reach once there are 14 days of data, and shows reach only before that. Every row has Ask about this, Watch and Compare with. Held-back items are listed with their reason.

Reads `/api/discover`, `/api/discover/radar`.

## Your work

### Alerts
Today's alerts and your team's watches. Watch a hashtag, sound, creator or brand in a market, and choose when to hear: when it starts rising, when growth passes a multiple, when reach passes a number of creators, when a creator breaks out, or when the tone flips. Watches can be paused and resumed. The daily alert email goes out at 06:45 SAST.

Reads `/api/alerts`, `/api/watches`; writes `/api/watches`, `/pause`, `/resume`.

### Investigations
Deep research for a bigger question. Draft a plan, edit it, start it, follow the live log, stop it, and turn the result into a dossier.

`/api/investigations` with `PUT /plan`, `/start`, `/stop` and a live `/events` stream.

### Dossiers
Client-ready packs of claims. Keep claims from answers and investigations, order and tick them, freeze a version, export it as HTML or PDF, and share a link to that frozen version.

`/api/dossiers`, `/ticks`, `/freeze`, `/versions/{v}`, `/versions/{v}/export`.

### History
Past questions, past briefs and saved findings, with search, and each item's earlier waves so you can see whether something has happened before.

`/api/history/asks`, `/briefs`, `/findings`, `/search`, `/items/{id}`.

## Dig deeper (under More)

| Page | What it shows | Reads |
|---|---|---|
| Compare | Two to five topics, brands, markets, platforms or creators side by side | `/api/compare`, `/api/discover` |
| Lexicon | The words and hashtags 42 is recording per market | `/api/lexicon` |
| Communities | Communities defined by shared interest, language and interaction, and one community in detail | `/api/communities`, `/api/communities/{id}` |
| Seed path | One word traced across platforms | `/api/seed-path` |
| Seeds | What 42 will search for next and where each seed came from | `/api/seeds` |

## How 42 works (under More)

| Page | What it shows | Reads |
|---|---|---|
| Coverage | Collection by day, market and platform, credits charged, model spend against its daily cap, runs of the day, the weekly scorecard, and what 42 does not see | `/api/coverage` |
| Fieldwork | The source roster and how each source performed | `/api/fieldwork` |
| Method | How 42 decides, in plain language | static |
| Schedules | Questions re-asked on a cadence, with pause and resume | `/api/schedules` |
| Skins | Client lenses over the same engine: a client Today, a report and an archive | `/api/skins` and its sub-routes |
| Hidden people | People suppressed from every view on request | `/api/suppressions` |
| All pages | A map of the menu | static |

## Off the menu

- **Topic page** (`#/t/<id>`): one item's series, posts, spread and history. Clicking a jump in its chart asks 42 why it happened (`/api/topics/{id}`, `POST /api/spikes`).
- **Creator page** (`#/creators/<id>`): one creator's page (`/api/creators/{id}`).
- Older links from the previous app redirect to their new pages (`app/frontend/src/legacyRoutes.js`).

## Stack

React 18 and Vite 6, built with Bun from a committed lockfile. Unit tests run under `bun test` with happy-dom, journeys under Playwright. The Ogilvy Intelligence design system ships as a vendored package; styles are plain CSS with design tokens. Fonts are Newsreader and Recursive. Charts use Chart.js.
