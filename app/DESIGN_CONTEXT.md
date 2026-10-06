# 42 Design Context

Everything a designer needs to extend 42 into a full exploratory journey. Read
this, then study the attached `frontend/src` code and the screenshots. The goal is
to keep the exact look and turn five flat pages into a web you can travel through.

## What 42 is

A cultural intelligence tool for strategists and creatives. It reads the Trends
Engine, a daily pipeline that collects social posts across South Africa (za),
Nigeria (ng) and Kenya (ke) from TikTok, Instagram, Threads, YouTube, Reddit and
news. 42 turns that into a live desk: what is trending, who is driving it,
what people think, and a grounded chat. The audience is strategists and creatives,
not analysts. It must read like a sharp colleague, not a dashboard.

The interface is passcode-gated. Show only evidence supported by the response,
with its source, market and window. Distinguish observations from inference and
state missing or insufficient evidence explicitly. No audience lens is selected
by default. Explicit audience questions remain valid, but demographic claims need
approved measurement with source, window, sample size, method and confidence.
Language, slang, topic labels and creator selection cannot establish demographics
or population representativeness.

## The aesthetic (preserve it exactly)

A dark "signal desk". The full token set is in `frontend/src/tokens.css`. Do not
introduce new colors or fonts.

- Fonts: `Newsreader` (serif display, used for names and headings), `Hanken
  Grotesk` (sans body), `JetBrains Mono` (all data, labels, eyebrows, ranks).
- Two themes, switched on `<html data-dir>`: `midnight` (default, near-black warm
  canvas) and `daylight` (paper terminal). Both are oklch.
- Accent is a signal orange (`--accent`), used for bars, ranks, links, the active
  state. Greens and reds are reserved for up/down and positive/negative only.
- Motifs: `## SECTION` dividers in mono caps, thin hairlines, staggered reveal
  animations on load (a `--motion` multiplier, user-controllable off to lively),
  tabular-num data, hover states that surprise, a `SignalLoader` mark.
- Mobile-first is a hard requirement. The current build is weak on phones; fix that
  first, then scale up.

## Tech and current shape

- Frontend: React (Vite), no router library. "Routing" is a `route` string in
  `App.jsx` held in `localStorage` (`pulse-route`), so there are NO real URLs and
  nothing is deep-linkable today. That is a gap to close.
- State in `localStorage`: route, region (`pulse-region` ZA/NG/KE), theme
  (`pulse-dir2`), motion, saved briefs (`pulse-briefs`), tracked creators
  (`pulse-crm`), chat history (`pulse-chat`).
- Backend: FastAPI (`src/api`) on Cloud Run, reading BigQuery. JSON
  endpoints listed below. The frontend talks to it through `frontend/src/api.js`.

### Current pages (the five flat screens)

1. Today (`today.jsx`): masthead, a live ticker, the ranked trend board, growth
   metrics, an Ask box, saved briefs.
2. Listen (`listen.jsx`): the live mention feed of real ingested posts, one
   market at a time, filterable by sentiment and platform.
3. Ask / Intelligence Centre (`chat.jsx`): a grounded chat with the engine.
4. Browse (`views.jsx` BrowsePage): search the post archive.
5. Method (`views.jsx` MethodPage): how it works.

A topic already opens a detail view, but as a MODAL overlay (`TopicDetail` in
`views.jsx`, opened from the ticker and boards). Creators do not open anything. The
modal pattern is why it feels one-pagey: you glance and close, you never travel.

### Component vocabulary to reuse (`frontend/src/parts.jsx`)

`Icon`, `Gloss`, `MomentumTag`, `CountUp`, `Sentiment`, `SignalLoader`, and a real
chart kit: `Sparkline`, `MomentumChart`, `CompareChart`, `RadarChart`. Use these for
the trend-over-time and reach-over-time views rather than inventing new charts.

## The journey to design

Turn entities into places. Every topic and every creator becomes a page you can
open, that cross-links to the others, that has a shareable URL.

1. Topic detail page (elevate the existing modal to a real page). For a topic on the
   Share of Voice board, show: share of voice and how it moved over time, the top
   posts driving it, the top creators on it, the sentiment split, the driving
   hashtags and slang, and the live mention feed. Each creator links to that
   creator's page.

2. Creator profile page (new). For a creator on the Influence board, or any handle
   anywhere, show: handle, platform, total reach, posts in window, the topics they
   cover, a reach-over-time trend, a wall of their actual posts, a Track toggle
   (the CRM, already `localStorage` `pulse-crm`), and a link out to their real
   platform profile. Each topic links to that topic's page.

3. Cross-linking and routing. Topic pages list creators, creator pages list topics,
   so the user hops creator to topic to creator without dead ends. Add breadcrumbs,
   a back affordance, and real deep-linkable routes (for example `/topic/amapiano`,
   `/creator/emily.112056`) so a view can be shared and reopened. This means
   introducing real URL routing.

4. Wire the existing pages in. Handles and topics on Today, on the Intelligence
   boards, and inside chat answers all become clickable and route into these pages.

## The live data model (ground every screen in these real fields)

Markets are `za`, `ng`, `ke`. The Intelligence layer is per single market; the desk
also supports `all`.

- `GET /api/metrics` (cumulative engine totals for the Today header):
  `{ posts, briefs, trends, creators, platforms, markets }`. Always grows.

- `GET /api/desk?region=za|ng|ke|all` (the trend board):
  `{ topics: [ ... ], freshness, digest, bridges, lexicon }`. Each topic carries a
  label, a trend score, a momentum read (Rising, Building, Steady, Cooling), a one
  line why, and the platforms it lives on.
- `GET /api/desk/topic/{topic_id}` (topic detail data, already exists).

- `GET /api/intel/mentions?market=za&sentiment=&category=&cursor=` (the mention
  drill-through): `{ results: [ { date, title, content, host, category, sentiment } ],
  cursor, has_more }`.

- `GET /api/ask?q=food+brands&market=za` (Browse search):
  `{ query, market, total_matches, thin, broad, volume_series:[{date,n}],
     platform_split:[{platform,n}], market_split:[{market,n}],
     trends:[{statement,how_to_use,momentum,evidence_count,platforms,markets}],
     quotes:[{text,platform,market,engagement,handle,age,tags}],
     slang:[...], creators:[{handle,mentions}], overall_read }`.

- Chat (async): `POST /api/chat/send { message, history, market } -> { job_id }`,
  then poll `GET /api/chat/status?job_id= -> { answer, sources:[{tool,market}] }` or
  `{ pending: true }`. History is client-supplied each send; the server keeps none.

### What exists vs what is a new data need

- Topic detail data EXISTS (`/api/desk/topic/{id}` and the overview topic fields).
- Creator identity, reach, posts-in-window and topics EXIST (the author shape
  above). A creator's WALL OF POSTS and their REACH-OVER-TIME trend are NEW: they
  need a `GET /api/creator/{handle}?market=` endpoint. Design the page assuming it
  exists; mark those two panels as a new data need rather than inventing numbers.
- Share-of-voice OVER TIME for a topic is a likely new need too. Mark it the same
  way. Everything else on the topic page is already served.

## Reading the data correctly (the priority)

The single most important thing: every number must mean what it actually means, and
be used the right way. Misusing a metric is worse than a plain layout. Definitions:

- reach: the potential audience of the posts, summed. It runs to tens or hundreds
  of millions. It is the influence signal. A creator with high reach and few posts
  is still a top voice. Rank "who is driving" or "top voices" by REACH, never by
  post count.
- mentions / posts in window: a COUNT of matching posts. It is activity, not
  influence. A prolific low-reach account is active, not influential. Never present
  it as audience size.
- share_of_voice: a creator-or-topic's PERCENT of the board's voice. It sums to 100
  across the topics shown. It is a relative share, not an absolute volume. Do not
  label it as a count.
- sentiment: positive, negative and total mention COUNTS from the pipeline. It is a
  split, not a score; the rest is neutral. Compute positive percent as positive over
  total, do not invent a 0 to 100 mood number.
- momentum (Rising, Building, Steady, Cooling): derived from the OBSERVED volume
  trend. It is descriptive, never a prediction.
- `genz_score`: a historical compatibility field, never audience evidence. Do not
  display it as a demographic finding or use it to select, rank or frame evidence.
  Preserve historical schema and topic keys when reading stored records.
- topics on a creator: the cluster labels they post on, top two. They are what the
  creator covers, not a ranking of the creator.
- markets (za, ng, ke) are SEPARATE listening projects. Never blend them into one
  number unless the view is explicitly the "all" desk. A South Africa figure is not
  a pan-African figure.
- window: everything is observed over a trailing window, mostly 30 days, some
  surfaces 7. Always show the unit and the window next to a number, never a bare
  figure. Nothing here is a forecast; never imply one.
- sources: the completed run's admitted signals and the engine's own taxonomy
  topics are two different sources. Creators come from the engine's own pipeline,
  not a vendor follower count. Keep them straight; do not merge them into one list.

Common traps to avoid, every one of which has already bitten this tool:
- Ranking voices by mention count instead of reach (surfaces Reddit commenters over
  real creators).
- Showing share of voice as if it were a raw volume.
- Letting a foreign or off-region cluster onto a per-country board.
- Printing a number with no unit, no window, or no market.
- Inventing a figure to fill a panel the API does not serve. Mark it a new data
  need instead.

## Rules for the design

- React, matching the `parts.jsx` vocabulary. Real URL routing for the journey.
- Mobile-first, then desktop. Show both layouts.
- Reuse `tokens.css`. No new colors, no new fonts.
- Real data only. Where a panel needs data the API does not yet return, label it a
  new data need; never fabricate a figure.
- Keep it unmistakably 42. A stranger should not be able to tell where the old
  screens end and the new pages begin.

## Files attached for reference

`frontend/src/` (App.jsx, today.jsx, intel.jsx, chat.jsx, ask.jsx, views.jsx,
parts.jsx, api.js, model.js, passcode.jsx, app.css, tokens.css), `BLUEPRINT.md`,
and the two board screenshots.
