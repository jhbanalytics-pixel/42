# 42 product blueprint

> A complete build spec for a fresh session. Read this top to bottom before touching anything. The product is approved as specified here; do not re-litigate the concept, but flag anything the live data contradicts.
>
> READ SECTION 9 (owner amendments, 10 June) BEFORE building. It supersedes parts of sections 2 and 4.

## 0. What this is, in three sentences

42 is a trend-identification instrument on the Ogilvy Trends Engine. A user searches any topic (FIFA, load shedding, amapiano) and the product names the clear, usable cultural trends inside that topic for Sub-Saharan Africa (ZA, NG, KE), each with momentum, evidence, and a "how to use it" line, backed by actual public voice as proof. It is internal and external facing, carries no client branding, and runs on an all-Google stack (BigQuery, Vertex Gemini, Cloud Run).

It is NOT a copy of the Vodacom Culture Desk. Culture Desk is a prescriptive weekly desk for one brand (one call, brand-safety gate, sign-off). 42 is an exploratory instrument for anyone: search anything, get the trends inside it. Different job, different surfaces, different identity.

## 1. Non-negotiable ground rules

1. No Vodacom anywhere: no name, no red, no speechmark, no references.
2. No authorship-tool traces in any tracked file, commit message, comment, or filename. Gemini, Vertex, Nano Banana, Lyria are product technologies and fine to name.
3. The engine source is imported under `engine/` and remains a separate runtime boundary. This product consumes its BigQuery data; it does not modify engine code at runtime. All reads go to BigQuery directly.
4. No external AI services. All model calls are Vertex AI (Gemini) inside the GCP perimeter. No PII: creator handles that look personal or junk are masked.
5. Never fabricate. Every trend, quote, number, and slang term rendered must trace to retrieved data. The model summarizes evidence; it does not invent. Thin data renders as an honest thin-signal state, never padded.
6. Writing style in all copy and docs: no em dashes, no en dashes, no hype words (leverage, seamless, robust, game-changing). Sharp, plain, confident.

## 2. The product

### 2.1 Core loop

Search a topic, get: (1) the clear trends inside it, ranked; (2) the proof (real public voice, slang, creators, volume); (3) the move (an actionable angle per trend). Repeat searches are instant via cache. The home page never sits empty: it leads with the clearest trends moving this week.

### 2.2 Surfaces (a structured app, four views in one shell)

App shell: top bar with the product lockup, nav (Ask, Browse, Method), a market switch (ZA, NG, KE, All), theme toggle, freshness stamp driven by real engine freshness (green under 3h, amber "Updated Nh ago" beyond; never a hardcoded "Live").

1. **Ask** (home, the hero surface)
   - A large search box: placeholder like "Search any topic. FIFA, load shedding, matric dance..."
   - Under it, "Moving this week": 6-9 chips/cards of the strongest current topics from the engine's curated taxonomy (pre-computed, instant), each clickable straight into an Answer page.
   - Recent searches (localStorage), one row, dismissible.

2. **Answer** (the payload, one structured page per query)
   Order matters; this is the product's argument:
   - **The trends inside this topic**: 2-4 ranked trend cards. Each card: a sharply named trend statement in plain language; a momentum chip (Rising, Building, Cooling, Steady) computed from the daily volume series, never claimed by the model; evidence count ("backed by N posts across M platforms"); where it lives (platform glyphs + market split); and one **"How to use it"** line, the angle a planner lifts straight into a brief, written non-branded (no client names).
   - **The voice**: a wall of 6-12 real quotes from the retrieved items, each with a platform glyph, market tag, and engagement count. Foreign-language junk filtered out. Handles masked when anonymous-looking ("TikTok creator"). If fewer than 3 clean quotes survive, show what exists with a "limited voice sample" note.
   - **The slang it is carried in**: term + plain-English meaning, only terms actually present in the retrieved items (the engine tags slang; surface what matched, do not let the model invent glossary entries).
   - **Who is driving it**: top creators from the retrieved set (masked where anon), with mention counts.
   - **The pulse**: a 30-day daily-volume sparkline from the real series, a platform split bar, a per-market split. Numbers formatted humanely (1.2M not 1200000).
   - **Thin-signal state**: if matches are under a floor (start at 12 items), the page says "No clear trend yet on this topic. Here is the early noise." and shows whatever quotes/volume exist. Never render an invented trend.
   - A **printable one-pager** route per topic (`/card/{query}`), server-rendered, print-clean, with the product lockup, the trend cards, top quotes, and a generated-on stamp. This is the forwardable artifact; a button on the Answer page opens it.

3. **Browse**
   - The engine's curated topic groups per market as rails (instant, pre-computed from the engine's view), each row: topic, momentum, one-line read, click-through to its Answer page (run the same pipeline with the topic name as the query, which keeps one rendering path).

4. **Method**
   - Honest how-it-works: eight live sources, the engine, retrieval then synthesis, what is measured vs what is summarized, the honesty rails. Plain language, no inflation. State plainly that momentum is computed from observed volume and the synthesis only restates retrieved evidence.

### 2.3 What it must never do

- No forecasting claims (the engine's forecast lost a backtest and is deliberately out of products).
- No brand-safety verdicts (that is Culture Desk's wedge; this product stays a neutral instrument).
- No fake interactivity: every button does something real or does not exist. No "coming soon" toasts unless labelled exactly that.

## 3. Architecture

### 3.1 Shape

One Cloud Run service, FastAPI, co-hosting a single-file UI, the proven pattern:

```
listening-post/
├── src/api/main.py          # FastAPI: routes, passcode gate, rate limit, no-cache headers
├── src/api/bq.py            # BigQuery retrieval: search, aggregate, curated rails
├── src/api/synth.py         # Vertex Gemini synthesis + the prompt contract + cache
├── src/api/card.py          # server-rendered printable one-pager
├── web/index.html           # single-file UI (React 18 UMD + Babel-standalone inline)
├── scripts/check_jsx.mjs    # compiles the inline JSX via Babel; blocks blank-screen typos
├── scripts/check_contrast.mjs # WCAG AA gate over the token pairs, both themes
├── scripts/smoke_test.sh    # curl the deployed routes
├── tests/test_api.py        # pytest, BQ + Vertex mocked
├── infra/cloud-run/deploy.sh
├── infra/env/env-template   # GCP_PROJECT, BQ_DATASET, UI_PASSCODE, CACHE_TTL...
├── Procfile                 # uvicorn entrypoint (Buildpacks, no Dockerfile)
├── .python-version          # 3.13
└── requirements.txt
```

### 3.2 Hosting

- GCP project: `ogilvy-trends-v2` (the engine's own project; the data lives there, IAM is trivial, and it keeps this product out of any client project).
- Service name: `listening-post`, region `us-central1`, Buildpacks source deploy, `--allow-unauthenticated`, min-instances 0, max 2, 512Mi, 30s timeout.
- The Cloud Run runtime service account needs `roles/bigquery.dataViewer` + `roles/bigquery.jobUser` on `ogilvy-trends-v2` (likely already held by the default compute SA in-project; verify, do not assume) plus `roles/aiplatform.user` for Vertex calls.
- The `/` route MUST send `Cache-Control: no-cache, no-store, must-revalidate` (hard lesson: a browser once served a stale cached build of a sibling product mid-iteration).

### 3.3 Data path, step by step

**STEP ZERO, MANDATORY: live-probe the schema before wiring anything.** Run `SELECT table_name FROM ogilvy-trends-v2.trends_v2_dev.INFORMATION_SCHEMA.TABLES` and inspect the columns of the content-level tables. Known to exist: the `v_trend_briefs` view (curated, one row per market+topic+day: tier, scores, headline, summary, platforms, creators, slang, social refs). There are content-level tables underneath (raw/enriched items with text, platform, market, engagement, timestamps, slang tags); confirm their real names and shapes by probing, never by guessing. The standing rule from this codebase family: trust the live INFORMATION_SCHEMA, not memory or docs.

**Retrieval (`/api/ask?q=...&market=...`):**
1. Normalize the query (lowercase, trim; map a few obvious synonyms like soccer -> football at the query layer only).
2. Parameterized BigQuery over the enriched content, last 30 days, optional market filter: match the query against text + slang tags + topic labels (use BigQuery `SEARCH()` if the table supports it, else `REGEXP_CONTAINS(LOWER(...))`; always parameterized, never string-built SQL).
3. Aggregate in one or two queries: total matches, daily volume series (30 points), platform counts, market counts, top creators by mentions, top ~40 items by engagement (id, text, platform, market, engagement, timestamp) as the evidence pool.
4. Clean the evidence pool in Python: language filter (drop high-confidence non-SSA-language items: Portuguese/Spanish markers, off-market hashtags, pt/es diacritics), strip URLs, mask anon-looking handles (numeric, very long, deleted/bot patterns), dedupe near-identical texts.

**Synthesis (`synth.py`):**
5. One Vertex Gemini call (Gemini 2.5 Flash, the engine's standard) with the cleaned evidence pool + the aggregates. The prompt contract (write it verbatim into the code):
   - You are given N real posts and aggregate stats about "{query}" from ZA/NG/KE sources over the last 30 days.
   - Cluster the evidence into 2-4 distinct trends. For each: a one-sentence trend statement in plain sharp English; 2-3 indices of the posts that evidence it; one non-branded "how to use it" line for a marketing planner; the dominant platforms and markets from the stats.
   - Also return: up to 6 slang terms FROM THE POSTS with plain meanings, and a two-sentence overall read.
   - Hard rules: never invent a post, statistic, or slang term; only reference provided evidence; if the evidence does not support 2 clear trends, return fewer or none and say why in one sentence; momentum is not yours to claim.
   - Return strict JSON (define the schema in code; validate; retry once on parse failure).
6. Momentum is computed in Python from the volume series (compare the last 7-day mean to the prior 14-day mean: Rising > +25%, Cooling < -25%, else Steady; Building when low base but accelerating). Attach to each trend card. The model never sets momentum.
7. Cache the full Answer payload keyed by (normalized query, market) with a TTL of 24h, in-process dict first (the engine refreshes daily, so this is honest); a small BQ cache table is optional later, not v1.
8. Thin-signal: if cleaned matches < 12, skip synthesis of trends, return the thin-signal payload (stats + whatever quotes survive).

**Curated rails (`/api/rails?market=...`):** read `v_trend_briefs` (latest day, per market), return topic, headline/summary, tier, velocity-derived momentum, for Ask's "Moving this week" and the Browse view. Pre-existing data, no synthesis, instant.

### 3.4 Auth, safety, cost

- `UI_PASSCODE` env var gates `/api/*` only (constant-time compare on an `X-Passcode` header; the UI prompts on 401 and stores it in localStorage). The page routes and `/card/*` stay open and forwardable. Suggested passcode for launch: pick fresh, do not reuse other products' codes.
- Rate-limit `/api/ask` (e.g. 30 requests / 10s window in-process) because each cold search costs a Gemini call.
- Cost envelope: BigQuery reads are small (single-digit GB scans; consider a date-partitioned filter ALWAYS in the WHERE). Gemini Flash per fresh search is cents; the 24h cache makes repeats free. Cloud Run scale-to-zero is ~$0 idle. State this in Method honestly if asked.
- KNOWN OPS GOTCHA from the sibling product: a `--set-env-vars` deploy wipes env vars set later by `gcloud run services update`. Either put UI_PASSCODE in the deploy script's env composition from `.env`, or re-apply after every deploy. Bake it into deploy.sh properly from day one.

## 4. Identity (deliberately distinct)

- **Name**: 42. Lockup: "42" + a small "Ogilvy Intelligence" credit.
- **Feel**: a precision instrument, radar-room calm. NOT an editorial magazine (that is Culture Desk's identity; do not reuse Fraunces, do not use any red as accent).
- **Theme**: dark-default, light toggle. Near-black `#0E0F12`-family surfaces, high-contrast off-white ink.
- **Type**: Space Grotesk for display, Inter for body, JetBrains Mono for data/numbers (Google Fonts).
- **Accent**: signal green family (e.g. `#19D89A` on dark; darken appropriately on light). All token pairs must pass WCAG AA 4.5:1 text / 3:1 decoration in BOTH themes; the contrast gate enforces this, tune the palette to the gate, not the other way.
- **Design system from day one** (lesson learned: retrofitting these is what removes the "generated" look): a spacing scale (4/8/12/16/24/32/48 as CSS variables), a type scale (~9 sizes max), 3 radii, 2 elevation levels, one component per concept (one momentum chip, one platform glyph set, one card). Full platform names with simple monochrome glyphs (not two-letter codes, not trademark logos).
- **Motion**: minimal, reduced-motion safe (never gate visibility on an animation).
- A loader with the product name, a first-run guide overlay (4 short steps: Search, Read the trends, Check the proof, Take the move), Escape-closable, focus-managed.

## 5. Honesty rails (product behavior, verbatim requirements)

1. Every number on screen comes from a query result. No model-authored numbers.
2. Momentum chips derive from the volume math in 3.3 step 6 only.
3. Quotes render verbatim from retrieved items (truncated on word boundaries, never mid-word), with platform + market + engagement.
4. The freshness stamp reads real engine freshness; amber when stale, never a fake "Live".
5. Thin-signal state as specified; an empty market or failed fetch shows a designed empty/error state, never a crash or blank (guard every lead/index dereference).
6. The model's JSON is validated; a malformed response degrades to the thin-signal layout with stats only.
7. No sentence on any surface promises prediction.

## 6. Build order (do it in this order)

1. **Probe**: BigQuery INFORMATION_SCHEMA + sample rows; write `docs/schema-notes.md` with the confirmed table/column names the code will use.
2. **API skeleton**: FastAPI app, health route, passcode gate, rate limit, no-cache static serve of a placeholder index.
3. **Retrieval**: `bq.py` search + aggregates against the probed schema; prove it from the CLI with 3 real queries (one rich like "amapiano", one medium like "fifa", one thin like "quantum computing") and record the match counts.
4. **Synthesis**: `synth.py` with the prompt contract + JSON validation + cache; prove the three queries end-to-end, confirm the thin one returns thin-signal.
5. **UI**: the shell + Ask + Answer against the live API, dark theme first; then Browse + Method; then the `/card` one-pager.
6. **Gates**: check_jsx + check_contrast + pytest green; a dash scan (`grep` for em/en dashes) over UI strings and docs.
7. **Deploy**: deploy.sh to `ogilvy-trends-v2`, verify health, passcode gate, all three markets, a cold fresh-browser walk (the passcode prompt must be the first-run experience, not an error).
8. **Verify rendered**: drive the deployed URL headless (Playwright MCP if available: navigate with a cache-bust query param, set the passcode in localStorage, evaluate/screenshot; the inline-Babel app cannot be curl-verified).
9. **Adversarial QA pass**: run a multi-lens design/honesty review against the live build and fix the P0s. Do not chase zero forever; the residual after one or two rounds is judgment calls.

## 7. Definition of done

A first-time user opens the URL cold on desktop or phone, passes the passcode, lands on Ask with this week's movers visible, searches "fifa", and within ~8 seconds gets named trends with momentum, real quotes, slang, creators, and the pulse; clicks one trend's "open the card" and gets a clean printable one-pager; searches something obscure and gets the honest thin-signal page. Zero console errors, all gates green, the live revision matches main, nothing on screen is fake, stale, or unexplained, and the whole thing reads as a designed instrument, not a template.

## 8. Context a fresh session needs (read-only background)

- The engine: `engine/` (Python 3.13, GCP project `ogilvy-trends-v2`, dataset `trends_v2_dev`, daily cron ~00:30-03:30 UTC, 8 connectors, 24 curated topic groups across ZA/NG/KE, slang tagging, multilingual embeddings). Engine source is read-only for this build.
- Python: use 3.13 explicitly (`py -3.13`). Prefer `bun install` for node dev deps (`@babel/standalone` for the JSX gate). Windows + bash environment; beware heredocs eating backslashes when editing files containing `\uXXXX` literals (use scripted edits from a written .py file).
- Compliance: WPP rules, client/cultural data stays in the GCP perimeter, Vertex only, no external AI APIs, no PII in outputs.

## 9. Owner amendments (10 June, supersede the sections they name)

The owner set four decisions that reshape the product's positioning. Where these conflict with sections 2 and 4 above, section 9 wins.

### 9.1 Relationship to the daily email: parallel run, not replacement (amends 0 and 2)

The PULSE email keeps sending unchanged. This product launches beside it as the standing destination for the same intelligence. No email code changes, no lean-email switch, no engine modification; the switch decision comes later from real usage. Long-term intent (context, not v1 scope): the engine already carries a lean inbox renderer (`render_inbox_summary`) built for a hosted full-read URL, so if the parallel run proves the app, the email can later become a short note linking into it.

### 9.2 Identity: PULSE continuity, Google x Ogilvy register (supersedes section 4)

This is the PULSE mailer grown into 42, not a new neutral instrument. Carry the mailer's identity family: the dark editorial masthead, the serif display voice, the vermillion accent family, mono provenance type, cream body surfaces. Tokens come from the mailer's render layer (`Trends Engine V2/src/alerts/email_render/_table.py` palette + brand config) as the reference, adapted for the web (real fonts allowed; do not use Fraunces, which belongs to the sibling product). The signal-green accent and Space Grotesk direction in section 4 are dropped. The lockup is 42 with Ogilvy Intelligence, in the same register as the mailer masthead. Infrastructure names (`listening-post` service, repo folder) stay as specified. Everything else in section 4 holds: the design-system discipline (spacing scale, type scale, radii, one component per concept), WCAG AA gates in both themes, reduced-motion safety, the first-run guide.

### 9.3 Surfaces: Today leads (amends 2.2)

A fifth view, Today, is the home surface. It renders the day's full briefing, the same intelligence the email carries:

- The day's verdict and through-line from `daily_summary`.
- The ranked briefs for the day from `trend_analysis` (headline, synthesis, cultural context, status tag, platforms, creators, social refs, visual anchor, the Nano Banana and Lyria prompts).
- Each brief's render bundle from `trend_analysis.render_payload` (JSON string): the display dict (state badge, phase, window, in-market share, Seen-on channel weights, confidence, search read) plus the conversation fields (`comment_sentiment`, `comment_themes`, `driving_hashtags`). Parse it; absent fields drop their element, the binding rule. This column exists precisely so a BigQuery reader renders what the email rendered.
- Freshness from the engine's `pipeline_runs` (the day's success row), feeding the stamp in the top bar.
- The Ask search box sits on top of Today, so exploration is one keystroke from the ritual. Ask's "Moving this week" rails fold into Today.

Nav becomes: Today (home), Ask results (the Answer page), Browse, Method. The Answer page, thin-signal state, printable card, and everything else in 2.2 hold unchanged.

### 9.4 Access and rollout (amends 3.4)

Passcode launch to the internal team first (the owner, the data director, the analyst, the client-lead circle). The client and the wider exec list get the URL after the product proves itself beside the email. The passcode gate, rate limit, and open `/card/*` routes stay as specced.

### 9.5 Build timing

The engine has a client session the day after these amendments; the engine repo is under a change freeze until that session lands. This product is a separate folder and touches nothing in the engine, so step 1 of section 6 (the schema probe) may run immediately; the full build starts after the session. The probe must also confirm `trend_analysis.render_payload`, `daily_summary`, and `pipeline_runs` shapes for the Today view, alongside the content-level tables for Ask.
