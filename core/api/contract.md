# 42 API contract, Stage 1

Owner: lane L4. Version 3, 30 September 2026. Frozen for the Friday 2 October demo: a change goes through Albert as one line to L4, and L4 bumps the version here. Built from EXPERIENCE.md (Today, Ask), AGENT.md (answer, tools, live progress), DATA.md (tables and views) and TRUST.md (the gate). The answer shape is core/eval/rubric.md section 1 and is not repeated here; where this file and rubric.md differ, rubric.md wins.

Two services, one origin for the app:

| Service | Identity | Code | Job |
|---|---|---|---|
| f42-api | f42-web | core/api/app.py | Serves the built app, the passcode gate, Today and trends (read only), and forwards every /api/ask call to f42-agent |
| f42-agent | f42-agent | core/api/agent_app.py wrapping core.agent.run_ask (L3) | Runs Ask, streams its steps, writes the finished run to intelligence_42_agent.runs |

f42-api holds no write rights (f42-web has dataViewer only), so everything it serves is read from the v_*_current views or from f42-agent.

## 1. Conventions

- JSON everywhere, UTF-8, snake_case keys. Times are ISO 8601 with the market's offset (SAST +02:00, WAT +01:00, EAT +03:00); dates are YYYY-MM-DD in the market's local day.
- Markets: ZA, NG, KE. Labels: South Africa, Nigeria, Kenya.
- Every number shown as a finding carries `{value, unit, query_id, run_id, result_hash}` (rubric.md numbers). Display strings built from those numbers are for layout only.
- Errors: HTTP status plus `{"error": "<code>", "message": "<plain words>"}`. Codes: `unauthorized` 401, `gate_not_configured` 503, `auth_unavailable` 503, `read_only_pilot` 403, `rate_limited` 429, `daily_question_limit` 429, `not_found` 404, `bad_request` 400, `not_ready` 409 (Today not yet published for that date), `agent_unavailable` 502, `internal` 500, `people_unavailable` 503 ("People view unavailable.": the suppression view is missing or cannot be read, so the creator and community routes name no one). A finished Ask record's `error` may also be `model_unavailable` (the model is out of quota or not enabled: a 429, 403 or 404 raised by the model SDK, google.genai, on the error or its cause; a BigQuery or other 404, 403 or 429 stays `internal`), shown in plain words; any other failure stays `internal`.
- Nothing is dropped silently: an item the gate blocked appears in `held_back` with its reason, and a failed source appears in `gaps` or `issues`.

## 2. Access

Lifted from app/src/api/main.py (require_passcode, lines 274 to 286; the auth and ask rate limiters, lines 302 to 334 and 616 to 635).

- `F42_AUTH_MODE` selects the gate. When unset, it is `passcode`. The only other supported value is `iap_readonly`; an unknown value fails closed with 503 `gate_not_configured`.
- In passcode mode, every `/api` route except `GET /api/health` and `POST /api/auth/verify` requires `X-Passcode: <passcode>` or `Authorization: Bearer <passcode>`. The server compares either value in constant time with `UI_PASSCODE`, mounted from the secret of the same name. An unset passcode returns 503 `gate_not_configured`.
- In passcode mode, `POST /api/auth/verify` accepts `{"passcode": "..."}` and returns `{"ok": true}` or 401. Failed passcodes on verify or gated routes share a limit of 10 per IP per minute; over-limit requests receive 429 `rate_limited`. Correct passcodes do not count.
- In `iap_readonly`, set `IAP_AUDIENCE` to the exact nonempty Cloud Run signed header audience and `IAP_ALLOWED_EMAILS` to a nonempty comma separated list of exact email addresses. Wildcards and domain only entries make the configuration invalid. Missing or invalid IAP configuration fails protected requests with 503 `gate_not_configured`. `GET /api/health` reports `auth_mode: "unavailable"`, `passcode: false`, and `checks.auth: "not_configured"`; it does not return an audience or identity.
- The IAP mode accepts only `x-goog-iap-jwt-assertion`. It verifies the ES256 signature using the PEM map at `https://www.gstatic.com/iap/verify/public_key`, fetched with a five second timeout, then checks exact audience, issuer `https://cloud.google.com/iap`, time claims, and exact membership of the signed `email` claim in `IAP_ALLOWED_EMAILS`. Time checks allow 30 seconds of clock skew and reject a token lifetime over 660 seconds. A plain forwarded email header and `UI_PASSCODE` do not authenticate this mode. Missing or invalid assertions and unlisted emails return 401 `unauthorized`. Certificate retrieval or response failures return 503 `auth_unavailable` with the fixed message "Authentication is unavailable. Try again shortly." The assertion is not logged or sent elsewhere.
- A verified IAP identity may use protected `GET` and `HEAD` routes. Other protected methods return 403 `read_only_pilot` before route handlers, limiters, writes, or spend. `POST /api/auth/verify` is the exception: it validates the signed assertion and returns `{"ok": true}`; its passcode body is ignored in IAP mode. The reviewed GET handlers, including investigation readback and event polling, read existing records and do not start or finish an investigation or ask.
- The signed header checks follow Google's [IAP signed headers guide](https://docs.cloud.google.com/iap/docs/signed-headers-howto); Cloud Run setup constraints are in the [Cloud Run IAP guide](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run). The local review boundary is in [IAP-PILOT.md](IAP-PILOT.md). These references and local checks do not establish deployed IAP or effective IAM state.
- Ask is limited per client IP: 30 requests per 10 seconds, and `ASK_PER_IP_DAILY` live questions per day (default 100; the office shares one IP). `mode: replay` asks spend no credits and do not count toward the daily limit. Over either: 429. Counters are in memory per f42-api instance, which is acceptable for staging.

## 3. Health

`GET /api/health` returns

```json
{"ok": true, "service": "f42-api", "version": "<git sha>", "time": "2026-09-30T06:31:02+02:00",
 "model_daily_cap_usd": 80.0,
 "auth_mode": "passcode", "passcode": true,
 "checks": {"auth": "ok", "bigquery": "ok", "agent": "ok", "today": "published"}}
```

`model_daily_cap_usd` is the effective model spend ceiling in USD for the SAST date represented by `time`, read from the shared cap configuration. `MODEL_DAILY_USD` may lower the value but cannot raise it. This is a ceiling, not spend or remaining budget. If the shared cap or override cannot be read, health returns 500 `internal` rather than report an unverified cap. `checks.today` is `published`, `not_ready` or `data_issue` for today's SAST date. `auth_mode` is `passcode`, `iap_readonly` or `unavailable`. `passcode` is true only when the passcode mode is active and configured. `checks.auth` is `ok` for a configured mode and `not_configured` otherwise. `ok` is false when auth is not configured, BigQuery fails, or the agent cannot be reached; the body still returns with 200 so the smoke test can print which check failed. Health exposes no IAP audience or identity.

## 4. Today

`GET /api/today?date=YYYY-MM-DD` (date optional, default the latest published brief date). Always returns all three markets. The Today reader applies the admission rule below to each market by combining `cards` followed by `more` without reordering either list, preserves each original `rank`, places the first five admitted cards in `cards` and the rest in `more`, then builds All from admitted cards (top three per market).

When a market has no admitted cards, its status block gives a general check status and the number held back, including on All. Individual reasons appear in the separate native `Held back` disclosure, which starts open. It groups held items by their supplied reason and shows stored check detail beside item titles. Each item's `Posts and figures` disclosure opens its evidence and figures, including safe HTTP(S) source links. Empty markets use this one held list; populated markets keep their `Held back` list. This display state does not change any gate or admission rule.

```json
{
  "date": "2026-09-30",
  "status": "published",
  "published_at": "2026-09-30T06:14:40+02:00",
  "heading": "Taking off, 30 September 2026",
  "headline": {"text": "One serif sentence stating the biggest finding.", "market": "ZA", "item_id": "...", "claim_ids": ["c1"]},
  "warmup": {"active": true, "day": 2, "of": 14, "text": "Warming up: day 2 of 14"},
  "first_morning": false,
  "markets": [ <Market>, <Market>, <Market> ],
  "run_receipt": {"posts": 2536, "markets": ["South Africa", "Nigeria", "Kenya"], "shown": 0, "held": 27, "collect_run_id": "collect-..."}
}
```

`run_receipt` is optional. `posts` is the `posts` count the latest ok collect run for the date stored in intelligence_42_agent.runs; `shown` and `held` are the cards (with `more`) and `held_back.count` summed over the markets in this response. f42-api leaves the field out when there is no ok collect run, its stored `posts` is not a whole number, or a market has no brief row; it never estimates a count.

`status`: `published` (every market's brief passed on time), `partial` (at least one market published numbers and posts only, or one market is missing), `data_issue` (collection or detection failed; cards may be empty, banners say why). 409 `not_ready` only when no brief exists for the requested date.

`warmup.day` counts days since the first ok collect run (runs, stage collect); `active` while day is 14 or less.

Market:

```json
{
  "market": "ZA", "label": "South Africa", "status": "published",
  "banners": [{"kind": "warming_up|thin_coverage|data_issue", "text": "plain words"}],
  "cards": [ <Card> x up to 5, source order ],
  "more": [ <Card> remaining admitted cards after the first five, source order with original ranks ],
  "dropped": {"first_morning": false, "text": "First morning: nothing to compare yet",
              "items": [{"item_id": "...", "title": "...", "reason": "faded|held_back|reclassified_seasonal|not_confirmed", "reason_text": "Faded"}]},
  "held_back": {"count": 1, "text": "1 held back: data issue",
                "items": [{"item_id": "...", "title": "...", "rule": "G1", "reason": "data_issue", "reason_text": "Data issue on TikTok in the last 3 days", "evidence_ids": [], "evidence": [ <Evidence> ]}]},
  "moments": [{"date": "2026-10-01", "name": "...", "kind": "holiday|festival|fixture|release|seasonal_item", "source": "calendar", "item_ids": []}],
  "boards": [{"platform": "tiktok", "list": "Hashtag board, 7 days", "entries": [{"rank": 1, "title": "#...", "item_id": "..."}], "left_out": 0, "left_out_reason": null}],
  "breaking": [{"item_id": "...", "market": "ZA", "market_label": "South Africa", "kind": "hashtag", "title": "#...",
                "time_text": "14:00", "ago_text": "2 hours ago", "note": null}],
  "coverage": {"posts": 18420, "platforms": ["tiktok", "instagram", "youtube", "x", "reddit", "facebook"],
               "located_share": 0.41, "invalid_series": 1, "issues": ["TikTok hashtag board: calls failed"]}
}
```

`dropped.first_morning` is true when there is no earlier published brief for the market. `headline` is null when no market row carries one. Its `claim_ids` point at verified claims on the named card, so the headline is checked like any claim (TRUST.md K10); the Today reader shows it only when `headline.item_id` resolves to an admitted card. `held_back.count` is the number of entries in `held_back.items`, as returned by f42-api. A card's `news_driven` is true when a news event, or a scheduled event such as a release, match or holiday, accounts for the rise and local creators add their own reaction; the app labels it News-driven and its claims sit one confidence step lower. Each held item also carries the `numbers` and `count_line` its card would have shown (growth left out while untested) and `failed_reason`, the fixed check wording, when its explanation failed the claim checks, or the fixed wording "Model busy: not explained before the deadline" or "Model busy: the model kept refusing calls, so this was not explained" when a busy model (429) left it unexplained (null otherwise). `held_back.reason` is one of `data_issue` (G1), `likely_coordinated` (G4), `political_unconfirmed` (G4b), `paid_led` (G5, G5b), `not_local` (G6), `too_few_creators`, `not_confirmed`, `explanation_failed` (G10 keeps the card as numbers and posts; it is listed here only if the card was dropped). `coverage` comes from v_collection_health_current for the date: posts summed over series, platforms with valid rows, `located_share` weighted by items, `issues` one line per invalid series. Each board is its own group: f42-api leaves out entries whose title is empty or an id (a YouTube channel id, `uc` plus exactly 22 id characters in any case, or a 64-hex hash), adds them to any `left_out` the board carries with `left_out_reason` "No readable name" (null when none), and keeps each entry's `rank` as the brief's best rank today, so ties and gaps stay and the app marks a tie among the shown entries with "=".

`breaking` (added 2 October 2026) lists the market's items the hourly Breaking rule (core/detect/breaking.py) judged Breaking, read from intelligence_42_core.v_breaking_signals_current (rows of ok breaking runs only): each item once, at its latest hour that started in the last 6 hours, newest first. It is not a card and never passes or skips the trust gate; the app shows it as its own strip above the morning cards, headed "Breaking in the last few hours", and shows nothing at all when every list is empty, which is the normal state while the hourly pulse is off. `title` is the item's cultural_map label, or its kind in plain words ("Hashtag item", "Trend item") when the label is missing or an id; never an id. `time_text` is the end of the item's hour in SAST (HH:MM) and `ago_text` how long ago that was ("In the last hour", "1 hour ago", "3 hours ago"); no ISO time is meant for the screen. `note` is "New, no prior posts" when the item had no baseline posts (no ratio), else null; no ratio or count is returned, so nothing needs a query id. The suppression list applies (section 16): a creator item that names a suppressed creator is left out and a suppressed handle is masked in any title; while the list cannot be read, no creator item is listed and every @handle in a title is masked. Only a request for the latest brief or for today's SAST date reads Breaking; a past brief carries empty lists. When the view does not exist or the read fails, every list is empty, f42-api logs a warning and Today renders as before. A client skin keeps only the lines whose title matches its terms or hashtags.

Card (one shape on Today, trends and topic):

```json
{
  "item_id": "sha256...", "market": "ZA", "date": "2026-09-30", "rank": 1, "kind": "hashtag",
  "title": "#example",
  "title_written": "A few words on what the posts are about, or null",
  "tag": "first_time|moved_up|held_place|null",
  "state": "new_to_42", "state_word": "New to 42",
  "flag": "null|likely_coordinated|check_pattern|market_unconfirmed|not_assessed|data_issue",
  "flag_word": "Market unconfirmed",
  "explained": true,
  "explanation": "One sentence, any hedge on the why-now clause only.",
  "explanation_claim_ids": ["c1"],
  "specificity": {"status": "pass", "local_evidence_ids": ["tt_7431", "ig_2210"],
                   "quote": {"evidence_id": "tt_7431", "text": "<verbatim source words>"},
                   "why_now": "<same checked sentence as explanation>", "reason": null},
  "claims": [ <rubric.md claim> ],
  "count_line": "31 creators, 3 days, 2.8 times usual",
  "numbers": [{"value": 31, "unit": "creators in 3 days", "query_id": "q_...", "run_id": "r_...", "result_hash": "sha256:..."}],
  "sparkline": {"unit": "posts a day", "points": [{"date": "2026-09-24", "value": 4, "expected_low": 0, "expected_high": 6}]},
  "thumbnails": ["tt_7431", "ig_2210"],
  "evidence_ids": ["tt_7431", "ig_2210", "yt_88"],
  "evidence": [ <Evidence> ],
  "ask": "What is behind #example in South Africa this week?"
}
```

- `tag` is computed by f42-api from the previous published brief for the market (the latest brief_date before this one, cards and more together): absent from it gives `first_time`; a smaller rank number today than there gives `moved_up`; the same or a larger rank number gives `held_place`; null on the first published morning.
- `title_written` (added 6 October 2026, additive) is the brief writer's short title naming what the card's posts are about, in their own terms, from the same writer call as the explanation (core/brief/explain.py). It is set only on an explained card whose title passed the explanation sentence's code checks (banned terms, pinned numerals, no forecast, crowd words, places) and its own support check against the posts the sentence's claims cite; otherwise it is null. It never changes whether a card is explained, admitted or held. `title` stays the cluster label as before. Today shows `title_written` as the card's title with `title` as secondary text, and shows `title` alone when `title_written` is null or absent (briefs written before it, which f42-api returns without the field). f42-api sets it to null when the card is not explained or when a suppressed person's posts were left out of it (section 16), since the check read those posts.
- `state` codes and words as DATA.md section 3.7. At most one state, one confidence label (on claims) and one flag per card.
- `specificity` is additive on a Card: `status` is `pass|fail`; `local_evidence_ids` is a string array; `quote` is null or `{"evidence_id": "...", "text": "..."}`; `why_now` is a string or null; `reason` is null or one of `missing_explanation`, `unsupported_claim_reference`, `insufficient_local_evidence`, `quote_not_cited`, `quote_not_local`, `quote_too_short`, `quote_too_long`, `quote_not_verbatim`, `quote_not_word_bounded`, `missing_local_quote`, `local_why_now_not_checked`. Failure fields may be partial. Producer authority: `0222ba6ddfc605ef005a53647fcd7199966e03be`.
- Today admits a card only when `explained` is exactly `true`, `explanation` is a non-empty sentence, `specificity.status` is exactly `pass`, `specificity.reason` is null, and `specificity.why_now` exactly equals `explanation.strip()`. `explanation_claim_ids` must be non-empty and resolve to valid claims in `claims`; the union of those claims' `evidence_ids` must include every ID in `specificity.local_evidence_ids`. At least two distinct local IDs must resolve by `id` in `evidence`, and `specificity.quote` must resolve by `evidence_id` to one of them. The quote must exactly match an `{evidence_id, text}` entry in one of those claims' `quotes`, and that same claim's `evidence_ids` must include the quote's `evidence_id`. It must be verbatim in its resolved record, using non-empty `quote_text` or otherwise `text`, pass the word-boundary check, contain 2 to 25 words and be at most 160 characters. Resolve records by ID, never array position. The reader checks references and quote integrity against the supplied card; it does not call the critic or recompute locality, which remain producer decisions. The one exception is a card that a suppressed person's posts were left out of (section 16): every reader gets it through the same projection (core/api/today.py without_hidden), which drops that person's quotes and checks the card again on the posts that stay, by the brief's own code. A claim that also cites posts that stay is kept only if they still pass K1 and K3's place rule, and its label is lowered to what K5 allows on them (one step lower again on a news-driven card), never raised. The card is then held as an explanation that failed its checks, with a `failed_reason`, when the explanation rests on a dropped claim, fewer than 2 claims stand, a place the sentence names loses its support, or fewer than 3 posts can be shown. A claim left with no post that stays holds the card as "The posts behind it could not be read".
- Legacy cards, missing or malformed specificity, and any card failing an admission condition are hidden on Today. The admitted card renders the quote in its collapsed quote block and at least two attributed examples from its embedded `evidence`, including the quote source. These examples require no additional Posts fetch. Hide the card if the quote or fewer than two distinct examples cannot be resolved. The headline may name only an admitted card; if its `item_id` does not resolve to one, show no card headline.
- Reader filtering retains rejected current candidates in `held_back.items`, after the producer's explicit held entries. Each item keeps its supplied evidence and exclusion reason; missing reasons have neutral wording. Item IDs are deduplicated with the explicit producer entry taking precedence. The API returns `held_back.count` as `len(items)` and derives its summary from that list. Rejected candidates remain outside admitted cards. A held item may carry `held_detail`, one plain sentence from the stored intelligence_42_agent.claim_checks row that held it, left out when no stored row held it. When that read fails, every held item it covered carries `"check_detail": "unavailable"` instead, and the Today reader says the check detail could not be read just now rather than that it was not stored; `check_detail` is absent otherwise. `dropped` is API-derived history from raw previous and current brief IDs, so a current card hidden by Today admission is not classified as dropped. The specificity admission rule applies only to Today. Trends, topic, Core and Discover keep their existing inclusion and evidence behavior.
- Today creator titles containing an exact YouTube channel ID use a readable `cultural_map.label` only when the map record matches that same `item_id` and has `kind=creator`. The API batches those IDs in one existing map read; normal titles need no extra read. Missing, unreadable or ambiguous matches display `YouTube channel`. Evidence authors are not guessed from another post or item. The UI retains ordinary text escaping for supplied titles, reasons and excerpts.
- `explained` false means no written explanation: G10 fired (`explanation_status` `failed_checks`) or no model call happened (`not_run`, section 10.1); `explanation` is null, `claims` is empty, numbers and posts still show on non-Today read surfaces. Today hides that card.
- `sparkline.points` has a point per day with `value` null on days collection_health marked invalid or before the series started (gaps, never zeros). `expected_low` and `expected_high` are null in warm-up.
- `thumbnails` lists up to two evidence ids from `evidence`.

Evidence (one shape everywhere: Today cards, Ask answers, SSE events). It is the rubric.md evidence record plus media fields, all optional except the rubric ones:

```json
{
  "id": "tt_7431", "platform": "tiktok", "handle": "@example", "url": "https://...",
  "posted_at": "2026-09-26T19:40:00+02:00", "market": "ZA", "source_market": "ZA", "text": "caption or transcript line",
  "engagement": {"views": 184000, "likes": 9100, "comments": 300, "shares": 120}, "flags": [],
  "thumbnail_url": "https://...", "duration_s": 21.0,
  "transcript_span": {"start_s": 4.2, "end_s": 9.8, "text": "..."},
  "creator_tier": "micro"
}
```

`source_market` is optional and nullable; a non-null value is `ZA`, `NG` or `KE`. The current Ask selector may choose it from dated `source_sightings` in the Ask window, creator `home_market`, or a profile-sourced `geo_market`. Because these sources are mixed, it is selected source or profile context, not proof that the post originated in that market, that its creator lives there, or of national popularity. The answer-level `market`, `evidence.market` and `platform` do not supply or imply `source_market`. Show `Market assumed: Country` only when `flags` includes `market_assumed` and `source_market` is recognized.

`sponsor_checked` is optional on brief evidence: true when 42 has a paid-label reading for the post (the enrich model's marker or the vendor's label), false when it has none. It never says a post is paid; the `sponsored` flag does.

## 5. Trends

`GET /api/trends?market=ZA&date=YYYY-MM-DD` returns the confirmed candidates of that published brief, up to 10, as `{"date", "market", "cards": [<Card>]}` (the same cards as Today's `cards` plus `more`). Stage 2 adds `state`, `platform` and `kind` filters and every live trend from v_item_state_current.

`GET /api/trends/{item_id}?market=ZA&date=YYYY-MM-DD` returns one Card with every evidence record the brief holds for it (the posts view behind "open a trend's posts"). 404 if the item is not in that brief or its held-back list; a held-back item returns its card with `"held_back": {"rule", "reason", "reason_text"}` added.

## 6. Ask

### Start

`POST /api/ask`

```json
{"question": "What is behind #example in South Africa this week?",
 "market": "ZA", "parent_id": null, "tier": "T1", "mode": "live", "wait": false,
 "from_card": {"item_id": "...", "market": "ZA", "date": "2026-09-30"}}
```

- `question` required, 3 to 2,000 characters.
- `market` optional (ZA, NG, KE or null for the agent to read from the question).
- `parent_id` optional: the ask_id this follows up; the agent receives the parent's question and answer.
- `tier` optional `T0` or `T1` (Stage 1A); the agent picks when absent. `T3` always returns 400 here (an investigation runs T3, section 13). `T2` returns 400 unless f42-agent has `F42_T2_READY=1` (off by default) and the ask's `skill` is `brand-implication` or `context-pack` (section 15.2); any other T2 ask still returns 400. The T2 hold is reserved under the daily model cap before any research starts, as for every tier. `GET /health` on f42-agent and `GET /api/health` on f42-api carry `t2_ready` (true or false); f42-api reports true only when the agent answers its health check with `t2_ready: true`.
- `mode` `live` (default) or `replay` (cached SocialCrawl responses only; a cache miss becomes a gap with status `not_in_replay`; spends no live credits).
- `wait` false (default) returns 202 at once; true holds the request until the answer is final (the eval) and returns 200 with the full Ask record.
- `from_card` optional, passed to the agent as is (Ask about this on a Today card). Named apart from the answer's `context` string.
- `spike` optional (section 14.1) takes only `item_id` (an item id), `market` (ZA, NG or KE), `date` (YYYY-MM-DD) and an optional `series` (1 to 80 letters, digits or _), and any other key or a bad value gives 400.

202 body: `{"ask_id": "a_20260930_...", "status": "running", "events_url": "/api/ask/a_.../events", "url": "/api/ask/a_..."}`

### Read

`GET /api/ask/{ask_id}` returns the Ask record:

```json
{
  "ask_id": "a_...", "question": "...", "parent_id": null, "market": "ZA",
  "status": "running|complete|stopped|failed",
  "created_at": "...", "finished_at": null,
  "answer": <rubric.md section 1 answer, or null while running>,
  "run": {"run_id": "r_...", "tier": "T1", "mode": "live", "credits": 214, "tokens": {"input": 0, "output": 0},
          "seconds": 41, "model_usd": 0.0,
          "window": {"from": "2026-09-21", "to": "2026-09-27"}, "posts": 214, "platforms": 6,
          "source_status": [{"platform": "tiktok", "route": "tiktok/search", "status": "ok|empty|partial|rate_limited|auth_failed|schema_drift|not_in_replay", "items": 312}],
          "followups": ["three suggested follow-ups built from the gaps"],
          "notices": ["Live search budget for today is spent; this answer uses stored posts only"]},
  "steps": [ <Step> ],
  "error": null
}
```

`run.posts` and `run.platforms` count the posts the answer read (its evidence records). `run.store` (added 4 October 2026, optional) holds the whole-store counts Ask makes once before the writer: `{"posts", "creators", "located_posts", "located_creators", "platforms", "query_id"}`, summed over the platforms of one recorded query (`query_id`) that counts every stored post in the ask's market and window (located there, or seen in its feeds; `located_*` count the located posts), limited to the platforms the question names. The Ask meta line shows it as "1 108 posts by 852 creators on 1 platform in the store · 51 posts read"; without it the line reads as before. A failed ask does not keep it.

A cut claim's event may carry `evidence_ids: ["[unresolved]"]` (L3), a placeholder that matches no evidence record; the app never renders it as a chip or a post. `status` is the Ask record's state; `answer.status` is the answer's own (`complete`, `partial`, `insufficient_evidence`, `refused`). A failed run has `answer` null and `error` `{"error", "message"}`. A `model_unavailable` error also carries `status` (the 429, 403 or 404 the model SDK raised) and `sdk_module` (the module of that SDK error), so the stored run says why the model was unavailable; the SDK's own message text is never kept. Readers ignore keys they do not know. The eval reads `answer` from this body. An ask started with `spike` (section 14.1) keeps that object on its record as `spike`, held and stored alike, so Try again on it can go back to the day on its topic page.

### Live progress

`GET /api/ask/{ask_id}/events` is a server-sent event stream (text/event-stream). The stream replays every step so far, then follows live, then ends with `done`. A comment line `: keepalive` goes out every 15 seconds. `Last-Event-ID` resumes after that seq. The app opens it with fetch and reads the stream, because EventSource cannot send the X-Passcode header (the app sends it from localStorage `pulse_passcode`, app/frontend/src/api.js). If the stream drops, the app falls back to polling `GET /api/ask/{ask_id}` every 2 seconds. For a finished record the stream replays its stored steps and ends with `done`.

| event | data |
|---|---|
| `step` | Step: `{"seq": 3, "at": "...", "kind": "plan|search|found|transcribe|read|check|write|note", "text": "Reading TikTok posts tagged #amapiano, South Africa, 21 to 27 September: 312 found", "platform": "tiktok", "count": 312}` |
| `evidence` | `{"seq": 4, "evidence": <Evidence>}` as sources arrive (thumbnails gather) |
| `claim` | `{"seq": 9, "claim": <rubric.md claim>, "check": "checking|verified|downgraded|cut", "reason": null}` |
| `done` | `{"seq": 12, "status": "complete|stopped|failed", "url": "/api/ask/a_..."}` |

Every event carries `id: <seq>`. Steps are written in plain words with counts, as EXPERIENCE.md shows.

### Stop

`POST /api/ask/{ask_id}/stop` returns 202 `{"ask_id", "status": "stopping"}`. The agent stops cleanly and finishes with status `stopped` and whatever answer it had passed through the checks (possibly `insufficient_evidence`).

### Export

`GET /api/ask/{ask_id}/export?format=html` returns `text/html` with `Content-Disposition: attachment; filename="42-answer-<ask_id>.html"`, rendered by the lifted ask_export from the Ask record. Only a record with status complete or stopped and a non-null answer exports; otherwise 409. PDF follows in Stage 3 through the lifted pdf_exporter.

## 7. The agent seam (L3 implements, L4 wraps)

f42-agent (core/api/agent_app.py) calls one function from core/agent:

```python
def run_ask(request: dict, emit: Callable[[dict], None], should_stop: Callable[[], bool]) -> dict
```

- `request`: the POST /api/ask body with `ask_id` added, and `parent` (`{"question", "answer"}`) filled in when `parent_id` is set.
- `emit(event)`: called for each `step`, `evidence` and `claim` event, as the dicts in section 6 without `seq` (the wrapper numbers them). The event may carry an `"event"` key (`step`, `evidence` or `claim`) naming its type; the wrapper classifies by it, drops it from the stored data, and falls back to the keys present when it is absent.
- `should_stop()`: polled between steps; true after POST /stop.
- Returns `{"answer": <rubric.md answer>, "run": <the run object in section 6>, "query_receipts": {...}}`. `query_receipts` holds, per query_id, the query behind the answer's numbers: `{"purpose", "sql", "params", "result_hash", "row_count", "rows"}`, with at most 50 rows (`row_count` and `result_hash` cover them all). The wrapper keeps it only on the stored record in the runs row, so a reviewer can check a number; it is never in an Ask body, an event, or a record read back for the app. To keep the row under BigQuery's streamed row limit, receipts take at most 1 MB: the largest queries lose their rows first (`rows: []`, `rows_dropped: "size"`), and receipts still too large are stored as null; the record itself is always written. Raises only on an unrecoverable error; the wrapper records it as `failed`. When the exception carries the partial run object as a dict `exc.run` (spend already made, at least `model_usd` and `tokens`), the failed record keeps it as `run` and its runs row writes credits, seconds and the record from it, so a daily model spend check that sums `record.run.model_usd` counts failed asks too; otherwise `run` stays null.

The wrapper holds running asks in memory. For Stage 1, f42-agent runs with max instances 1, CPU always allocated (`--no-cpu-throttling`, so a `wait: false` ask keeps running after the 202) and a 3,600 second request timeout (SSE streams and `wait: true` evals outlive the 300 second default); f42-api runs with the same timeout and up to 3 instances, holding no state. f42-agent is deployed `--no-allow-unauthenticated`, so an ID token from f42-web is the only way in. On finish the wrapper appends the Ask record to intelligence_42_agent.runs (stage `ask`). f42-api forwards /api/ask* to f42-agent with an ID token and reads a finished record from runs when f42-agent no longer holds it. The wrapper imports `run_ask` from core.agent.ask (else core.agent); if neither exists, POST /api/ask returns 503 `agent_unavailable` and /health reports the agent as missing. A fixture agent that replays core/api/fixtures/ask_*.json runs only when `F42_AGENT=fixture` (local and tests, never staging). It holds at most 8 running asks (429 `rate_limited` beyond that) and evicts finished asks after an hour or past the newest 200; a follow-up whose parent was evicted reads it from runs.

Needed from L1 (via Albert): (a) runs rows for stage `ask` carry two JSON columns, `answer` and `record` (the full Ask record above), so a finished answer can be read back and exported; (b) bootstrap.py adds a secret `UI_PASSCODE` (Albert sets its value himself; the builder never sees it) and secretmanager.secretAccessor on that secret only for f42-web.

Needed from L3 (via Albert): core.agent exposes `run_ask` with the signature above, and its `run` object carries `window` (`{"from": "2026-09-21", "to": "2026-09-27"}`), `posts` and `platforms` counts for the Ask meta line (EXPERIENCE.md, Ask item 2).

## 8. What the API reads (from L2 and L1 tables)

Today and trends read one published brief per market and date from intelligence_42_agent.v_briefs_current (DATA.md section 3.1). The brief row carries the Market object of section 4, without `tag`, `dropped` and `coverage`, which f42-api computes, in one JSON column:

```
briefs: brief_date DATE, market STRING, run_id STRING, published_at TIMESTAMP,
        status STRING (published | partial | data_issue), payload JSON (the Market object), rule_version STRING
```

Needed from L2 (via Albert): write briefs with that `payload` shape. Cards inside it carry `item_id`, `rank`, `kind`, `title`, `state`, `flag`, `explained`, `explanation`, `claims`, `count_line`, `numbers`, `sparkline`, `thumbnails`, `evidence_ids`, `evidence`, `ask`; `explanation_claim_ids`, `held_back.items`, and a `headline` object on each market row (`{text, item_id, claim_ids}`, or null). f42-api shows the ZA row's headline, else NG's, else KE's. Paid-led items (G5, G5b) go to `held_back` with reason `paid_led`, never onto a card as a flag.

Also read: v_breaking_signals_current (Breaking, section 4), google_search_signals (Searching now, section 21), v_collection_health_current (coverage, thin-coverage and data-issue banners), calendar (moments in the next 14 days), runs (warm-up day, today's stage statuses for the data-issue banner and health). Every query runs with a 2 GB bytes-billed cap.

## 9. Fixtures

core/api/fixtures/ holds one published brief per market for two consecutive days (so tags and dropped have something to compare), one data_issue market, collection_health rows, calendar rows, and three Ask records (complete, partial with gaps, failed). Tests and the app's local dev mode run against them with `F42_DATA=fixtures`; staging runs with `F42_DATA=bigquery`.

## 10. Version 2: Stage 2 screens (added 29 September 2026)

Everything above stays. Stage 2 adds read routes on f42-api (read only, 2 GB bytes cap) and three small write routes that f42-api forwards to f42-agent, because f42-web holds no write rights and f42-agent already writes intelligence_42_agent. Nothing here uses an age lens. The worth-attention percentile never reaches the app, only the order it gives (DATA.md section 3.7). Nothing the gate removes disappears: it is listed with its reason (TRUST.md section 2, RULES.md rule 5).

Every count or ratio the app shows as a finding is a Figure: `{"value": 31, "unit": "creators in 3 days", "query_id": "q_...", "run_id": "r_...", "result_hash": "sha256:..."}`. Shapes below write `<Figure>` where one goes.

### 10.1 One card shape, grown for Stage 2

The Card of section 4 stays the only item shape on Today, trends, Discover and topic pages. Stage 2 adds these optional fields to it (null when their data does not exist yet, so the screen says so instead of hiding it):

- `explanation_status` (written by L2 on every brief card, L2 Needs item 12): `explained`, `not_run` (no model call happened: quota, deadline, or an item on Discover that is not in the brief; the app says "No explanation yet"), `failed_checks` (TRUST.md G10; the app says "Explanation held back: it did not pass the checks") or `held`. `explained` stays true only for `explained`.
- `lifecycle`: `{"step": 1 to 5, "word": "Rising", "rule": "Clearly up, at least twice its usual level, on 2 days"}` for Emerging, Rising, Peaking, Mainstream and Fading (the five-step marker, with the rule shown as FEATURES.md row 11 asks); null for the other states.
- `novelty`: `new`, `recurrence`, `variant` or `ongoing`, with `last_wave` (`{"peak_date", "peak_posts": <Figure>}`) when a recurrence.
- `diffusion`: `bottom_up`, `small_only` or `top_down`.
- `origin`: `native`, `news_led` or null until L2's task 2.5 measures it.
- `spread_line`: the sentence of EXPERIENCE.md ("First seen on TikTok 12 September, X 18 September, news 24 September; 38 creators; Gauteng to Western Cape"), or null until L2's task 2.3 writes spread.
- `reach`: <Figure>, creators over three days (item_state.creators3). This is the one reach used everywhere: Discover's reach sort and Radar's reach axis.
- `growth`: <Figure>, the main series ratio to its baseline (item_state.main_ratio), null in warm-up.
- `order`: the item's place in its market's list, from worth-attention (worth_pct, or worth_raw when the cohort is under 20 items, then creators3); 1 is first.
- `watch_id`: the active watch on this item, if any, so the card shows Watch or Watching.
- Flag words gain one: `paid_led` "Paid-led". On Today a paid-led item is only ever in held_back (section 4); on Discover it is listed under Held back as below.
- The share flag `young_accounts` (DATA.md section 3.7, account age, a bot signal) is always worded "Accounts under 30 days old", never anything that could read as a person's age.

### 10.2 Discover and Radar

`GET /api/discover?market=ZA&kind=&state=&platform=&sort=order|velocity|reach|new&limit=50&cursor=` reads v_item_state_current for the latest good detect run, joined to cultural_map (`valid_to IS NULL`) for the label.

```json
{"date": "2026-10-20", "market": "ZA", "run_id": "r_...",
 "items": [ <Card> ], "next_cursor": null,
 "held_back": {"count": 4, "items": [{"item_id": "...", "title": "...", "reason": "not_local|likely_coordinated|paid_led|data_issue", "reason_text": "...", "card": <Card>}]},
 "filters": {"kinds": [], "states": [], "platforms": []}}
```

Discover result cards add `market_scope`, a resolved string with exactly two values: `market` and `global`. The optional source is `intelligence_42_core.v_item_market_scope`, joined by exact `metric_date`, `item_id` and `market`, with the join filtered by date. When the dated scope source is present, it is authoritative: a null or invalid value, or a missing match, resolves to `global`, even if the stored brief says `market`. Only when the source is absent may an explicit, valid `market_scope` from the same date's brief supply the value; an absent or invalid fallback resolves to `global`. The response contract requires the resolved field to be `market` or `global`; any missing or invalid value encountered at this boundary is unproven and treated as `global`. The existing `market` field is search or collection context (`ZA`, `NG` or `KE`), not author location and not proof that a post came from that market's own feeds. In the Discover display, `global` is labelled `Global, seen in South Africa searches` for `ZA`, `Global, seen in Nigeria searches` for `NG`, or `Global, seen in Kenya searches` for `KE`.

Discover cards also expose nullable `market_posts7` (integer), `total_posts7` (integer) and `market_share7` (number). The counts describe distinct posts in the selected seven-day evidence pack, capped at two posts per creator and twelve posts total: `market_posts7` counts posts confidently located in the target market or with matching own-market source provenance, and `total_posts7` is the full pack. The display labels this `Market evidence`; it does not describe an author's physical location or population coverage. `market_share7` is the supplied ratio for that pack, not `local_share`, which is the known-geography ratio. A `global` result remains in Discover and may show a valid supplied `market_share7`; the basis does not change its scope or eligibility.

Each Discover page card also carries `reach7` (added 5 October 2026): the `creators7` Figure Radar's measures give (store.item_reach, every collection lane over the 7 days ending the run date), or null when the read gave nothing. The app shows it beside `reach`, the 3-day count from the boards and followed accounts the checks use; order, gating and `reach` are unchanged.

The same optional `v_item_market_scope` view supplies these fields from the exact `(metric_date, item_id, market)` row. When the view exists, a missing row returns null for all three fields, and a null or malformed field stays unknown without brief fallback. Only when the view is absent may f42-api use the three fields together from the matching card in a same-date brief (`brief_date` equal to `run_date`, matching market and item ID, from `payload.cards` or `payload.more`); fields are never mixed across sources or dates. Counts accept non-negative integers up to twelve. If one count is missing or invalid, retain any individually valid count and return a null share; if both counts are valid but `market_posts7` exceeds `total_posts7`, return null for both counts and the share. A share accepts a numeric integer or float in `[0, 1]`, not a boolean. With valid counts and a positive total, the supplied share must match the count ratio rounded to one decimal place in percentage units; if it matches, the API returns the supplied share without rounding, otherwise it returns null. A zero numerator with a positive total is measured as zero when the supplied share is valid; zero over zero has a null, unknown share. The source may be absent or unpopulated; this contract does not assert native availability. L2 owns producer admission to Today; this section defines no new L4 Today filtering and gives no DDL or deployment authorization.

`items` holds eligible items only (item_state.eligible). An item that is not eligible, or has sponsored_share 0.5 or more, goes to `held_back` with its card so it can be opened. The reason comes from L2's per-item gate result (v_item_gate_current: item_id, market, rule, reason) when it exists; until then f42-api derives it from item_state: authenticity likely_coordinated gives G4 likely_coordinated, geo_status not_local gives G6 not_local, sponsored_share 0.5 or more gives G5 paid_led, a cultural_map status other than active gives `not_active` ("Not tracked by 42 right now"), and anything else gives `held_by_gate` ("Held back by 42's checks"). G1 data_issue and G5b (the campaign hashtag list) appear only through L2's view. Sort by velocity uses the main series velocity, by reach the `reach` Figure, by new first_seen. `market=all` returns every market with `market` on each card. `GET /api/trends` (section 5) becomes this route with `sort=order` once Stage 2 ships.

`GET /api/discover/radar?market=ZA&kind=` returns `{"date", "market", "points": [{"item_id", "label", "kind", "state", "flag", "growth": <Figure or null>, "reach": <Figure>, "measures": <Measures or null>}], "held_back_count": 4, "note": null}`. Points are eligible items only; the held-back count shows beside the chart. In warm-up growth is null for every point and `note` reads "Growth needs 14 days of data; showing reach only", and, when the points carry `measures`, the strip ranks `measures.creators7` on one bar scale for points with 8 or more posts in 7 days (the G6 floor), lists the rest after it with their count in words and no bar, and says "Not measured" for a point whose measures are null; without measures it ranks `reach`. The response also carries `window`: `{"from", "to", "days": 7, "platforms": [{"platform", "days", "days_ok"}]}`, the days the measures cover and, per platform, how many of them had baseline health rows and how many of those were valid on every baseline series (null `platforms` when health cannot be read; null `window` on All).

`measures` counts the item's posts in the market over the 7 days ending the run date, each post dated by its first sighting in the market (a 28-day scan, as tvf_item_window and item_daily date posts) and counted once, every collection lane counted (legacy, placebo and agent_live aside): `posts7`, `creators7` (coord_score under 1, as creators3), `measured_posts7` and `measured_creators7` (rank and panel lanes only, which no search of ours steers), `views7` and `engagement7` with `views_posts7` and `engagement_posts7` (how many posts carried the value), `located7` (posts whose place is known: geo_confidence 0.7 or more from ext_region, home_market or place_mention, or sighted on one of the market's own feeds), `local7` (those located in the market or sighted on its own feeds, the v_item_market_scope rule), `local_share7` (local7 over located7, null when nothing is located) and `own_feed7`, each a <Figure> with query_id `q_item_reach`, plus `platforms`. An item the read found no post for gets its counts as 0 Figures and views and engagement null. `measures` is null on every point when the read is missing or fails, and on the All market. `measures` describes reach only: the gate, `reach`, `flag`, `market_posts7`, `total_posts7` and eligibility still read item_state and v_item_market_scope. A creator item whose map label is only its id is titled with its creators row's display name, else its @handle, else "<Platform> account, name not collected"; a suppressed creator is never named.

### 10.3 Topic pages

`GET /api/topics/{item_id}?market=ZA` returns `{"card": <Card>, "aliases": [], "history": [{"date", "state", "state_word"}], "series": [{"platform", "series", "series_words", "unit", "points": [ ...section 4 points... ]}], "spread": {"platforms": [{"platform", "first_seen"}], "tiers": {"nano": <Figure>, "micro": <Figure>, "mid": <Figure>, "macro": <Figure>, "mega": <Figure>}, "markets": [{"market", "first_seen", "posts": <Figure>}]} or null, "origin": {"origin", "market", "first_measured", "after_collection_began", "first_state_day", "lead_news_day", "lag_days"} or null, "news": [{"seed_date", "news_day", "news_day_from", "title", "matched_by", "collect_ran", "reached_state", "first_state_day", "first_measured", "lag_days"}] or null, "waves": [{"peak_date", "peak_posts": <Figure>}], "authenticity": {"flag", "flag_word", "signals": [{"signal", "words", "share": <Figure>}]}, "evidence": [ <Evidence> ]}`. `authenticity.flag` is null only when item_state stores a check that found nothing (`clear`); `not_assessed` when the sample was too thin to check; `not_stored`, with a null `flag_word`, when item_state holds no check for the item; otherwise the stored flag. The topic page says "No unusual patterns" only for null, and says the posts were not checked for `not_assessed` and `not_stored`. Card `flag` values are unchanged.

Waves come from L2's v_item_waves; evidence from v_item_evidence; series from tvf_item_timeseries (DATA.md section 7). A topic the gate held back returns with `card.held_back` set, as section 5 does.

Spread comes from L2's v_item_spread (BUILD.md task 2.3), one read of the item's rows on the run date in every market; it is null when the page's market has no row. `markets` lists only the markets the view has a row for, which are those with a measured post first seen in the 7 days ending the run date, ordered by `first_seen`: `first_seen` is that market's earliest platform sighting (null when the view names no platform) and `posts` the market's tier counts summed (null when a tier is missing), so posts with no creator tier are not in it. A market is where the posts were collected, not where a person lives.

`origin` comes from L2's v_item_origin row for the item and market (BUILD.md task 2.5), null when there is no row or no view. `first_measured` (the first measured social sighting, never a news outlet's post), `after_collection_began` (42 was already collecting there before that day, so the sighting is not older than 42's measurement) and `first_state_day` are measured. `origin` (`native` or `news_led`), `lead_news_day` and `lag_days` stay null until the seed queue records a news source and its date.

`news` comes from L2's v_news_followthrough: the news seeds in the market queued on or before the run date whose own item is this topic or that the news bridge matched to it, newest `seed_date` first, at most 20; null when there are none or no view. `title` is the view's `label` (the seed item's label, or its query text); `matched_by` is how this topic matched the story (`seed`, `hashtag_key`, `label` or `cluster_keyword`). `collect_ran`, `reached_state`, `first_state_day` and `first_measured` are the view's; `news_day`, `news_day_from` and `lag_days` stay null until the seed queue records a news source and its date. None of spread, origin or news carries a creator handle or id, and the page passes through the suppression list as a whole.

### 10.4 Coverage

`GET /api/coverage?date=` returns per market: series rows from collection_health (series words as on Today, calls, calls_ok, items, valid, invalid_reason, located_share); credits for the day by job and lane from credit_ledger (charged, never quoted); model spend for the day from runs (sum of the `model_usd` column over every stage: ask, understand, detect, brief, learn) against MODEL_DAILY_USD from SETUP.md; while any run of a stage that always records its spend (understand, understand_spend, detect, brief, ask, learn, investigation) has no `model_usd`, `model_spend.usd` is null, the recorded subtotal is `known_usd` and those runs are listed in `uncosted_run_ids`, so missing spend never reads as zero; the runs of the day by stage and status, where a run that finished ok but could not make some of its writes (the understand job's enrichment, or clustering for a market: counts.enrich_error, or an error under counts.cluster.<market>, on its latest runs row) also carries `degraded`, those writes in words ("Enrichment", "Clustering for Nigeria"), its status still ok since detect and the brief went ahead; the not-seen list from ENGINE.md section 5 as fixed copy; and `scorecard`, the latest week of intelligence_42_agent.engine_scorecard (one row per market, keyed by week_start): `{week, week_end, markets: [{market, time_to_detect, lead_time, precision, recall, breadth, cost_per_confirmed_trend, reasons}]}`, each measure the Figure L2 stored (trimmed to the five Figure keys; a measure without a value keeps L2's reason in `reasons`), or null with "No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, for the week before" (f42-learn-mon-0730).

Moving between days (added 2 October 2026): the response also carries `today` (the SAST date the read was made), `latest_day_with_data` (the newest day v_collection_health_current has any row for, or null), `collection` (what happened to collection on the requested day, read from the same rows: `collected` when sources were recorded, else from that day's collect runs `running`, `empty` when a collect run finished without recording a source, `failed`, or `not_recorded` when there is no collect run), and per market `summary` `{posts, platforms, located_share, sources, sources_usable}` counted as Today counts it (posts and platforms from usable sources, the located share weighted by posts), or null when the market recorded nothing that day.

Sources table (added 5 October 2026, display only): each series row's `series_words` is a name no other row of its market shares, the series words plus what tells the row apart (the Apple Music chart type, the TikTok board's period and industry, the YouTube category, the subreddit and sort, the publication of a news feed, the kind of search), and a number in protocol order where rows still read alike. Each row also carries `group` (charts, boards, posts, news, other) with its `group_words`, and `located_words` (for example "National chart" or "Market board") when `located_share` is null and the source is a chart or board, else null. The page folds rows with no calls under the table; `summary` counts are unchanged.

### 10.5 Watches and alerts

A watch follows an item, a hashtag, a sound, a creator, a brand or a free query, in one market or all.

- Targets: `{"kind": "item", "item_id": "..."}`; `{"kind": "hashtag|sound|creator", "value": "#amapiano"}`; `{"kind": "brand|query", "value": "..."}`. Hashtag, sound and creator targets resolve in SQL by exact match on cultural_map kind and canonical_key after the same normalising the engine uses (no model call). Brand and query targets resolve by keyword against cultural_map label and aliases in SQL at read time, and nightly by L2's detect job with semantic search (tvf_search_items) into intelligence_42_agent.watch_matches, its embedding spend written to that detect run's `model_usd` so it counts toward MODEL_DAILY_USD; the read route uses watch_matches when it has rows for the watch.
- Rules: `state_in` (enters one of these states), `ratio_over` (growth at or above), `reach_over` (reach at or above), `breakout` (a creator breakout on this item, from L2's task 2.13), `tone_flip` (tone on this item changes sign day over day, from task 2.1 tone), `creator_surge` (a watched creator posts three or more times their usual views). A rule whose data does not exist yet is accepted, never fires, and the watch shows "Waiting for breakout detection" (or tone, or creator views).
- `GET /api/watches`, `POST /api/watches` (201), `POST /api/watches/{watch_id}/pause`, `POST /api/watches/{watch_id}/resume`. Writes are forwarded to f42-agent, which appends a row; the current watch is the latest row per watch_id; nothing is deleted. Until IAP (Stage 3) the list is shared by everyone who has the passcode, and `who` is "passcode".
- `GET /api/alerts?date=` computes alerts when read from the latest good detect run: `{"date", "alerts": [{"watch_id", "label", "item_id", "market", "fired_because": "Entered Rising", "card": <Card>, "since": "2026-10-19"}]}`. Today shows the count and the list above the cards.
- The daily email digest (BUILD.md 2.9) is a job after the brief that sends the same list with the lifted email kit, through Gmail SMTP from jhb.analytics@gmail.com with an app password (Albert, 2 October): f42-digest runs core/api/digest.py --send at 06:45 SAST, at most once a day, and sends only while DIGEST_SEND_ENABLED is true.
- Watches also feed the collector's watchlist lane (DATA.md section 3.2): L1's collect job reads the current watches.

### 10.6 Feedback

`POST /api/feedback` returns 202 and is forwarded to f42-agent, which appends to intelligence_42_agent.feedback. Body `{"target": {"kind": "card", "item_id", "market", "date"} or {"kind": "claim", "answer_id" or "brief_id", "claim_id"}, "value": "real|not_real|useful|wrong", "reason": "optional words"}`. Stored as `who` "passcode" until IAP, `what` the JSON of target and value, `reason` the words, `at` the time. Taps never feed precision or trust numbers (TRUST.md section 6); they only tell reviewers where to look.

### 10.7 Needed from other lanes for version 2

- L1: runs gains `model_usd FLOAT64` (added column only), and every job writes its model spend there (ask rows are written by f42-agent from run.model_usd); tables intelligence_42_agent.watches (watch_id, created_at, status_at TIMESTAMP, who, target JSON, market, rule JSON, label, status active or paused; append-only, every row keeps the watch's first created_at and status_at is when that row's status was set; the current view takes the latest row per watch_id by COALESCE(status_at, created_at)) and intelligence_42_agent.watch_matches (watch_id, match_date, item_id, market, method, run_id; append-only); feedback gains `at TIMESTAMP` (added column only); the collect job reads current watches for the watchlist lane.
- L2: v_item_gate_current (item_id, market, rule, reason) with the gate result per item; model_usd on detect, brief, understand and learn runs rows; v_item_evidence and tvf_item_timeseries (DATA.md section 7); spread, tiers and origin per item (tasks 2.3 and 2.5); engine_scorecard, table and weekly rows (task 2.7); watch_matches rows for brand and query watches in the detect job; breakout and tone signals per item (tasks 2.13 and 2.1).
- SETUP.md calls f42-agent "Ask service only"; it now also appends watches and feedback rows, inside its existing dataEditor on intelligence_42_agent. No new grant.

## 11. Version 3: Compare (added 29 September 2026)

Compare puts two to five things side by side in the same units over the same window (EXPERIENCE.md, FEATURES.md row 19). Read only on f42-api, 2 GB bytes cap, every number a Figure (section 10). Nothing here uses an age lens.

`GET /api/compare?mode=items|markets|platforms&items=<item_id>,<item_id>&market=ZA&markets=ZA,NG,KE&platforms=tiktok,x&days=28`

- `mode=items`: two to five items (topics, hashtags, sounds, creators or brands) in one market.
- `mode=markets`: one item across two or three markets (the only three markets 42 covers).
- `mode=platforms`: one item in one market across two to five platforms.
- `days`: 7, 14 or 28 (default 28). The window ends on `window.to`, the metric_date of the latest good aggregate run, and every value in the response is read for that window or at that date, never "latest" separately.
- 400 when the subject count is outside the mode's range, or a subject is unknown.

### 11.1 Where the numbers come from

Posts, not item_daily: item_daily's `_any` rows carry platform `_all`, and its per-platform rows are split by lane class, so neither gives per-platform counts without double counting, and its daily `creators` cannot be summed into a window. Compare therefore counts in SQL from posts joined to the subject's posts, each post once:

- A subject's posts are found by post_items (item_id) for hashtags, sounds and creators, which the understand step links today. Brands and topics are found through post_items too (clustering links topic posts there with via 'cluster') and also by keyword: a whole-word, case-insensitive match of the item's label or any alias in posts.text or posts.transcript. The response says which way each subject was matched (`matched_by`: `linked` or `keyword`), and a keyword-matched subject carries the note "counts the posts 42 linked to it plus posts that name it; name matches may include unrelated uses".
- One market and day rule for every subject, however it was matched: a post counts in a market on the market-local day of its first post_observations sighting in that market, ignoring legacy and agent_live sightings (the lanes excluded from `_any`, DATA.md section 1). This is the same basis as item_daily's metric_date and the market Today and topic pages count in, so Compare's numbers line up with theirs. A post seen in two markets counts once in each. The platform is posts.platform.

Metrics per subject for the window, each a Figure over the rows it counts:

| metric | words | how |
|---|---|---|
| posts | Posts first seen in the window | COUNT(DISTINCT post_id) |
| creators | Distinct creators in the window | COUNT(DISTINCT creator_id) over the whole window, not a sum of days |
| engagement | Engagement (likes, comments, shares, as each platform counts them) | SUM(likes + comments + shares) from posts' latest reading over the posts that report any of the three; the words say platforms count differently. Null, not 0, when the subject has posts and none reports a counter; when only some do, a note says how many of its posts the sum counts |
| first_seen | First seen in 42's collection (all time, not clipped to the window) | MIN of the first-sighting day in that market over all of 42's collection |
| growth, reach, state | Growth against its own baseline; creators in the last 3 days; state | From item_state at window.to, only in `mode=items` and `mode=markets`, where the subject is an item and a market. `mode=platforms` omits these three rows, because item_state has no per-platform scope. |
| platforms | Platforms seen on | COUNT(DISTINCT platform), only in `mode=items` and `mode=markets` |

Series: `posts a day` per subject for each day of the window, by first-sighting day, which is also the collection day collection_health describes. A day is null when every collection_health row for the subject's market (and, in `mode=platforms`, that platform) is invalid for that day; a valid day with no matching posts is 0. Each post's first sighting per market is computed over all of post_observations (never only the window's partitions, which would make re-sightings look new), then the window keeps the posts whose first sighting falls inside it; posts is joined by post_id without a post_date filter. The query runs under the 2 GB cap; if the cap refuses it, the response says "This comparison needs more data than one question may read" instead of a partial answer.

### 11.2 Response

```json
{"mode": "items", "window": {"from": "2026-09-02", "to": "2026-09-29", "days": 28},
 "subjects": [{"key": "s1", "label": "#amapiano", "item_id": "...", "market": "ZA", "platform": null, "matched_by": "linked", "card": <Card or null>}],
 "series": [{"key": "s1", "unit": "posts a day", "points": [{"date": "2026-09-02", "value": 4}]}],
 "rows": [{"metric": "posts", "words": "Posts first seen in the window", "values": {"s1": <Figure>, "s2": <Figure>}}],
 "notes": ["Coverage differs: 5 of 6 South Africa series usable, 2 of 4 Kenya series usable over the window; read differences with care",
           "#example counts the posts 42 linked to it plus posts that name it; name matches may include unrelated uses"]}
```

`notes` always states unequal coverage across the compared markets or platforms (share of valid collection_health series per subject over the window), because unequal collection is never read as a difference in prevalence (rubric.md, honesty about gaps). A subject the gate holds back is compared like any other, with its card's `held_back` shown.

## 12. Version 4: creators, communities and history (added 29 September 2026)

Read only on f42-api, 2 GB bytes cap, every number a Figure. Communities are defined by shared interests and interaction only; nothing here infers age. Where trust and coverage conflict, 42 publishes less (TRUST.md), so the rules below keep named people apart from sensitive topics and from coordination or payment signals. Text search never calls a model from f42-api: it matches cultural_map labels and aliases in SQL.

### 12.1 Three rules for anything that names a person

1. Page threshold: a creator gets a page, and is named in a community's member list, only at tier macro or above (500,000 followers or more, creators.tier as it stands now), the tier SETUP.md already uses for copying clips. Everyone else appears only inside counts. There is no other exception. Albert may lower this to mid (Needs Albert).
2. No sensitive topics next to a name: items in the sensitive set (political items including TRUST.md G4b's election list, and religion, health, sex life, race or ethnicity, and crime) never appear on a creator page, in a creator's item list, or as the basis of a named community. The set is L2's to supply. A keyword list alone is a floor, not a complete set (L2, 29 September), so the set counts as known only when L2's view intelligence_42_core.v_sensitive_items_complete exists: the union of the keyword list (v_sensitive_items) and the understand job's model classification (task 2.1). v_sensitive_items on its own is still used to exclude items, but it does not switch item lists or named communities on. Until the complete view exists, creator pages show no item list and no community, with "Topics per creator appear once 42 can keep sensitive topics off named pages", and communities show counts only, never members.
3. No coordination or payment next to a name (TRUST.md K7): on creator pages and member lists, items that are held back in the latest detect run, or flagged likely_coordinated, check_pattern or paid-led in any good detect run of the last 28 days, are left out, and Evidence `flags` are dropped. creators.coord_score is never shown. An evidence post on a named page keeps its author only if that author is at page tier now (creators.tier as it stands, not the tier at posting) and not suppressed; an author 42 cannot resolve is not named.

42 also honours the suppression list (SETUP.md data protection) on every route that names a person, as soon as that list exists; whoever builds it (Needs Albert) gives f42-api a view to read.

### 12.2 Creator pages

`GET /api/creators/{creator_id}?market=ZA`. Below the page threshold: 404 with "42 shows creators of this size only in totals".

```json
{"creator": {"creator_id": "...", "platform": "tiktok", "handle": "@...", "tier": "macro", "followers": <Figure>, "home_market": "ZA", "profile_url": "https://www.tiktok.com/@..."},
 "recent_posts": [ <Evidence, flags dropped> ],
 "items": [{"card": <Card>, "posts": <Figure>}] or null,
 "formats": [{"format": "duet", "posts": <Figure>}] or null,
 "reach": {"median_views": <Figure>, "posts_28d": <Figure>},
 "community": {"label": "...", "community_id": "..."} or null}
```

- `recent_posts`: the creator's own posts first seen in the market in the last 28 days, newest first, up to 12, minus posts that belong to sensitive, held-back or flagged items. While the sensitive set is unknown, `recent_posts` is null and `recent_posts_note` carries "Recent posts appear once 42 can keep sensitive topics off named pages".
- `profile_url`: built from the handle by a fixed template per platform (tiktok.com/@h, instagram.com/h, youtube.com/@h, x.com/h, facebook.com/h, reddit.com/user/h, threads.net/@h), else null.
- `formats` is null until L2's enrichment (task 2.1) fills post_enrichment.formats.

### 12.3 Communities

`GET /api/communities?market=ZA` and `GET /api/communities/{community_id}`. Until L2's clusters exist (task 2.2), a community is a connected component of creators in one market over the last 28 days, where two creators are joined when they share three or more items that are not sensitive, not on a platform's own board, not platform-generic (L2's list of generic tags such as #fyp), and not held back, flagged likely_coordinated or check_pattern, or paid-led (rule 3); components under five creators are not shown. `community_id` is sha256 of the market and the sorted item_ids of its top five shared items, so it stays stable while its core interests do. `method` says `shared_items` or `clusters`. Interaction (replies and mentions between creators) joins the definition when 42 collects comments and mentions; until then the response says "Grouped by shared topics; interaction not yet measured".

Each community follows rules 1 to 3 throughout, its `label` and `top_items` included: `community_id`, `label` (its top shared items, never people), `creators` <Figure> (all members counted), `members` (page-tier members only, by rule 1, and only once rule 2's sensitive set exists), `top_items` [Card], `platforms`, `languages` (from post_enrichment.langs, null until 2.1), `example_posts` [Evidence, flags dropped] from page-tier members only.

### 12.4 History

- `GET /api/history/items/{item_id}?market=ZA`: the item's earlier waves (v_item_waves: peak date, peak posts, days above half its peak), recurrences, and analogues: up to five other items whose cultural_map centroid is nearest (VECTOR_SEARCH over stored centroids, no model call) with a completed wave. The two are aligned on days since first sighting; for each analogue the response gives what happened from the item's current day count onward (days to peak, peak posts, days to fade below half its peak), as Figures. Analogues are null until centroids exist (L2 tasks 1.8 and 2.2), with "Analogues need 42's cultural map".
- `GET /api/history/search?q=&market=`: items whose label or alias matches q (SQL), each with its waves.
- `GET /api/history/asks?limit=20&before=`: past Ask records (question, when, answer status, ask_id) from runs, newest first.
- `GET /api/history/briefs?limit=30&before=`: brief dates per market, newest first: every row of v_briefs_current whatever its status, so a brief that held every item is listed. Each market carries `status`, `published_at`, `cards` (Figure: the cards and more Today shows of that brief) and `held` (Figure: its `held_back.count` as Today counts it), both with query_id `q_history_briefs` and the brief's run id, counted from the stored payload as Today reads it.
- `GET /api/history/findings?item_id=&status=`: saved findings with their claims and evidence. Status values and who sets them are L3's (save_finding writes current, only for a claim the Ask's trust gate kept; recall_findings marks stale or contradicted); until L3 defines them, the route returns every finding with valid_to null and says "Whether a finding still holds is not checked yet".
- Dossiers join History with task 3.5.

### 12.5 Needed from other lanes for version 4

- L2: the sensitive set (cultural_map.sensitive or v_sensitive_items) covering political (with the G4b list), religion, health, sex life, race or ethnicity and crime items; the platform-generic tag list as a table or view; v_item_waves `above_half_days` if missing; centroids and a vector index (1.8, 2.2); post_enrichment.formats and langs (2.1); clusters and cluster_members (2.2).
- L3: the findings status values and their writer (current, stale, contradicted), and item_ids on each claim.

## 13. Version 5: investigations and dossiers (added 29 September 2026)

Every write in this section goes through f42-agent, as in section 10 (f42-web writes nothing). Every table named here is append-only; nothing is overwritten or deleted (the never-delete rule).

### 13.1 Investigations (task 3.4)

An investigation is a T3 run (AGENT.md effort tiers) with a plan shown first, an edit step, an estimate, a background run with the live log, and a notice when done (EXPERIENCE.md, Investigations).

Money comes first. Before drafting, re-planning or starting, f42-agent reads today's spend: SocialCrawl credits left under ASK_DAILY (credit_ledger) and model dollars left under MODEL_DAILY_USD (the sum of runs.model_usd over every stage today, section 10.4). Drafting needs at least the planner's own ceiling of USD 0.50 left, else 429 "Today's model budget is spent; try tomorrow or ask Albert to raise MODEL_DAILY_USD". A plan whose estimate is above either budget left is refused at start (409, with the words for whichever budget it breaks); Albert can raise ASK_DAILY or MODEL_DAILY_USD for a day (SETUP.md). The planner's own spend is written to its runs row (stage investigation, model_usd) so it counts.

- `POST /api/investigations` body `{"question", "market", "angles": optional}` returns 201 with a draft: `{"investigation_id": "i_...", "status": "draft", "plan": <Plan>, "estimate": {"credits": n, "model_usd": n, "minutes": n}, "budget_left": {"credits": n, "model_usd": n}}`. Drafting calls L3's `plan_investigation(request) -> {"plan", "estimate", "run"}`; it spends no SocialCrawl credits.
- Plan: `{"sub_questions": [{"id": "q1", "text": "...", "platforms": ["tiktok", "x"], "credits": 120}], "researchers": 5, "gap_round": true, "max_credits": n, "max_model_usd": n}`. `max_credits` and `max_model_usd` never exceed the budgets left when the plan was made.
- `PUT /api/investigations/{id}/plan` body `{"plan": <Plan>}` while draft: appends a new draft row with the edited plan and a fresh estimate from L3's `estimate_plan(plan)` (no model call).
- `POST /api/investigations/{id}/start`: re-checks both budgets, then f42-agent calls `run_ask` with `tier: "T3"`, `plan`, and ceilings set at that moment: `max_credits` and `max_model_usd` are each the lower of the plan's value and the budget left now, less the ceilings already granted to investigations still running (f42-agent keeps that reservation until each run ends) (section 7); run_ask must stop cleanly at either ceiling and report what it had. The run's record is an Ask record (section 6) with `investigation_id`. `GET /api/investigations/{id}/events`, `GET /api/investigations/{id}` and `POST /api/investigations/{id}/stop` work as Ask's do. `GET /api/investigations/{id}` on a draft also returns `budget_left`, read at that moment less the reservations as the draft and PUT read it, or null when today's spend cannot be read; other statuses leave it out.
- `GET /api/investigations?status=draft|running|complete|stopped|failed` lists them, newest first.
- Storage: intelligence_42_agent.investigations `(investigation_id, version, created_at, who, status, question, market, plan JSON, estimate JSON, ask_id, run_id)`, one row per draft, edit, start and finish; the current investigation is the latest row. Running ones also live in f42-agent's memory like asks; the table is the source of truth, so a restart or eviction loses nothing.
- Done notice: Today's alert strip shows investigations finished in the last 7 days; email follows with the digest once a mail route exists. The finished answer opens as a draft dossier (13.2) with one tap.

### 13.2 Dossiers (task 3.5)

A dossier assembles a finished answer or investigation into a reviewed, frozen, exportable document (EXPERIENCE.md, Dossiers; SALVAGE.md lifts intelligence_dossier, dossier_resolver, dossier_store storage only, dossier_review_store log only, dossier_producer from PR 96 and pdf_exporter, with the approval roles dropped).

The server owns every claim. A dossier's claims are always rebuilt by claim_id from the source record's stored answer (section 6), and only claims that passed the checks there (TRUST.md section 3: kept or downgraded, never cut) can appear. A client never sends claim text, labels, quotes, evidence or numbers; it can only choose which of the source claims to keep, their order, the title, and short notes, which are shown as the reviewer's notes and never as claims.

- `POST /api/dossiers` body `{"from": {"ask_id" or "investigation_id"}, "title"}` returns 201 with version 1 of a draft: summary (the short answer), the source's checked claims with their labels, quotes, evidence ids and Figures, the cited Evidence, gaps.
- `PUT /api/dossiers/{id}` body `{"keep": ["c1", "c3"], "order": ["c3", "c1"], "title", "notes": {"c1": "..."}}` appends a new draft version rebuilt from the source with that selection.
- `POST /api/dossiers/{id}/ticks` body `{"claim_id", "ticked": true|false, "note"}` appends a review row to intelligence_42_agent.dossier_reviews `(dossier_id, claim_id, ticked, note, who, at)`. Ticks belong to the claim_id, not the version, so they carry across edits; the latest row per claim_id counts. That is safe because a claim's content can never change inside a dossier (it is always the source record's), so a tick given on any version is a tick on the same claim.
- `POST /api/dossiers/{id}/freeze` appends a frozen version. Refused (409, naming the claims) while any kept Single source or Inferred claim has no current tick (TRUST.md: these need review), or while any kept claim fails its re-check against the stored source: label, evidence_ids, every quote and every Figure's result_hash must equal the source record's, every quote must still be found verbatim in its evidence text (TRUST.md K1), and every evidence id must resolve (K1). A frozen version is never changed; editing again starts a new draft version from it.
- The edit and the freeze each send `from_version`, the version they were made from, and when that is not the latest version they are refused with 409 `stale_version`, "This dossier changed since you opened it. Reload to see the latest version.", and nothing is appended, so a second tab never buries another tab's draft. A request without `from_version` behaves as before, so older clients still work.
- `GET /api/dossiers?limit=&before=`, `GET /api/dossiers/{id}` (latest version with its ticks), `GET /api/dossiers/{id}/versions/{n}`.
- Export: `GET /api/dossiers/{id}/versions/{n}/export?format=html|pdf`, frozen versions only (409 otherwise), with working citations: HTML from the lifted renderer, PDF from the lifted pdf_exporter running headless Chromium in f42-agent (the same image as f42-api carries Chromium), rendered in memory, never stored, and passed through by f42-api unchanged.
- Share: `#/d/<dossier_id>/<version>` opens frozen versions only, for anyone past the gate (the passcode now, IAP later); drafts are reached from the dossier list.
- Storage: intelligence_42_agent.dossier_versions `(dossier_id, version, created_at, who, state draft|frozen, body JSON, source_ask_id, content_hash)`.
- Copying cited clips for evidence that must survive deletion by the author (SETUP.md data protection) is deferred: it needs a write role on the media bucket, which no service holds; the dossier links to the posts meanwhile.
- History (section 12.4) lists dossiers.

### 13.3 Needed from other lanes for version 5

- L1: tables intelligence_42_agent.investigations, dossier_versions and dossier_reviews (append-only, as above); runs stage `investigation` for planner rows.
- L3: `plan_investigation(request) -> {"plan", "estimate", "run"}` staying under a USD 0.50 model ceiling of its own, `estimate_plan(plan) -> estimate` (no model call), and T3 in `run_ask` honouring `plan`, `max_credits` and `max_model_usd`. Until they land, drafts return 503 "Investigations need the T3 agent" (tests use a fixture planner).

## 14. Version 6: Stage 4 app rows (added 29 September 2026)

Spike explanation, scheduled questions and team-channel delivery (FEATURES.md rows 28 to 30). Money rules as in section 13: every live model or SocialCrawl call counts toward MODEL_DAILY_USD and ASK_DAILY and is written to runs.

### 14.1 Spike explanation

Clicking a day on any chart (card sparklines, topic series, Compare) asks why that day jumped. `POST /api/spikes` body `{"item_id", "market", "date", "series": optional}` is a shorthand for an ask: f42-api builds the question "What made <label> jump in <market name> on <date>?" and forwards `POST /api/ask` with `tier: "T1"`, `from_card: {item_id, market, date}` and `spike: {item_id, market, date, series}`; the response is the ask's 202. The agent answers with L3's spike-explain skill when present (AGENT.md skills), else a plain T1 answer. Everything else is Ask: the same record, events, checks, export and dossier path, and the same section 2 limits as `/api/ask` (30 requests per 10 seconds and ASK_PER_IP_DAILY per client IP). A T1 ask can spend up to 60 credits (AGENT.md), so the app asks for a confirm tap ("Ask why, up to 60 credits") before it posts. A day with no measured value (a gap) cannot be asked about: 400 "No measurement that day".

### 14.2 Scheduled questions

A schedule re-asks a saved question on a cadence and keeps every answer (FEATURES.md row 29).

- `POST /api/schedules` body `{"question" (3 to 500 characters), "market", "tier": "T0|T1", "cadence": "weekly_monday|daily", "deliver": ["in_app", "channel"]}` returns 201. Writes go through f42-agent to intelligence_42_agent.schedules `(schedule_id, created_at, status_at, who, question, market, tier, cadence, deliver JSON, status active|paused)`, append-only, current = latest row by the same order as v_watches_current; `POST /api/schedules/{id}/pause` and `/resume` append a row. `GET /api/schedules` lists them with each one's last run and, when the last due run was skipped, the skip reason.
- Money: scheduled asks spend a share of ASK_DAILY, SCHEDULED_DAILY in credits, which only Albert sets in the SETUP.md caps table and L1 mirrors in core/config/caps.yaml (the way the pulse share is held). L4 proposes 120 credits (two T1 asks, or twelve T0). Until that row exists, the job runs as a dry run and asks nothing. Scheduled asks call SocialCrawl under the existing `ask` share, so ASK_DAILY stays hard in core/collect/socialcrawl_client.py; their run_ids start `sched-`, and the scheduled share spent today is counted in credits: the larger of the credit_ledger sum over `job = 'ask'` rows whose run_id starts `sched-` and the credits on the stage ask runs rows with a `sched-` run_id (which always carry it; run_ask now passes the request's sched- run_id to the SocialCrawl client, so the ledger rows carry it too, and the runs rows still cover older days). Their model spend counts toward MODEL_DAILY_USD like every other ask. Before each ask the job appends a runs row (stage `scheduled`, status `started`, credits 0, finished_at null) with the same `sched-` run_id; a schedule that already has a stage ask row or a started row today is not asked again, so a retried or killed job never asks twice. If the started row cannot be written, the schedule is not asked and a failed row says so. The app shows a started run as "Started, no answer recorded yet", never as answered. The pre-check relies on run_ask stopping each ask at its tier's credit and max_budget_usd maximum (L3); an ask that overran would still stop inside ASK_DAILY, and the next pre-check would see it.
- Runs: a Cloud Run job f42-scheduled-asks (code core/api/scheduled.py, identity f42-agent, deployed with core/setup/deploy_jobs.py like the other jobs and started by the Scheduler entry f42-scheduled-asks-0700 in core/setup/schedule.py) runs at 07:00 SAST, after the 06:15 brief deadline. It asks each due schedule one at a time, oldest schedule first, through the same run_ask path as Ask, `mode: live`, tier as saved. Before each ask it reads spend again and asks only when the SCHEDULED_DAILY credits left, the ASK_DAILY credits left and the MODEL_DAILY_USD left each cover the saved tier's maximum (AGENT.md: T0 10 credits, T1 60 credits, and that tier's max_budget_usd). Otherwise the schedule is skipped, never silently: a runs row (stage `scheduled`) names the schedule and the reason ("Scheduled share spent", "Daily questions spent", "Model budget spent"), GET /api/schedules shows it, and the next due day tries again. Skipping replaces SETUP.md's T0 fallback on the fast model here on purpose: a scheduled answer at a lower tier than the one saved would read as the same question answered worse.
- Results: each run is an Ask record with `schedule_id`, listed in History and on the Alerts screen. The in-app notice on Today shows the question, the answer status and a link, as the section 13.1 done notice does; no answer text is lifted onto Today, and refused or insufficient_evidence answers show as such. Channel delivery is not wired (14.3): `deliver: ["channel"]` is validated and stored with the schedule, and nothing acts on it.

### 14.3 Slack or Teams delivery

A daily post to one team channel after the brief (FEATURES.md row 30), built as core/api/channel.py, which renders one message and posts it to an incoming-webhook URL held in Secret Manager as CHANNEL_WEBHOOK_URL (Slack or Teams form detected from the URL). It is not wired (3 October 2026): channel.py is a standalone CLI that no job runs, core/api/scheduled.py neither imports it nor reads CHANNEL_WEBHOOK_URL, f42-scheduled-asks does not mount that secret, and a schedule's `deliver: ["channel"]` is stored but never acted on, so nothing is posted outside 42. The owner does not want a webhook created. Wiring it later needs his go, the webhook value, the secret mounted on f42-scheduled-asks again and a call to channel.py from scheduled.py. The rules below hold for that day. A message already posted cannot be suppressed later, so the message carries only these fields and nothing else:

- Per market: the brief's state ("Published", "Published, some explanations held back" for a partial brief, "Published with a data issue" for a data_issue one, or "No brief today" when none was published by 07:00) and a link to Today.
- The headline, only when the item it is about is not creator-kind; otherwise "Today's brief is ready" and the link.
- Up to three cards per market: title, state word and count_line, each with a link into 42. A creator-kind card (DATA.md) goes out without its title, as "A creator is <state word> in <market name>" and the link.
- Alerts: an item-watch alert goes out as the item's card title (under the creator-kind rule above), the rule that fired and a link. Watch labels, brand names and search text are typed by users, so they stay out: a creator watch goes out as "A watched creator moved in <market name>", a brand or query watch as "A watched search moved in <market name>", each with the link.
- Scheduled answers: the answer status and a link only. The question text stays out, since a user can type a handle into it.

No handle, no "@", no post text, no evidence excerpt or quote, and no answer text ever reaches the channel; a unit test renders a fixture with a creator card, a creator watch and an answer that quotes a post, and asserts none of them appears; the fixture also holds a query watch labelled with an @handle. The webhook URL is itself a credential: it is never printed, logged, put in dry-run output or in error text (an HTTP client error is reported as its status code only). Without the secret, channel.py renders a dry run only.

### 14.4 Needed for version 6

- Albert: the SCHEDULED_DAILY share of ASK_DAILY in the SETUP.md caps table (L4 proposes 120 credits). The channel's incoming-webhook URL (secret CHANNEL_WEBHOOK_URL, secretAccessor for f42-agent only, bootstrap.py) is needed only if 14.3 is wired later; as of 3 October 2026 it is not wanted.
- L1: table intelligence_42_agent.schedules (append-only as above); SCHEDULED_DAILY mirrored in core/config/caps.yaml; the job f42-scheduled-asks in core/setup/deploy_jobs.py with the SocialCrawl key as its only secret (CHANNEL_WEBHOOK_URL is not mounted while 14.3 is not wired), and its Scheduler entry f42-scheduled-asks-0700 in core/setup/schedule.py, all owned by L1; runs stage `scheduled` for skip, started and failed rows (a started row has a null finished_at). Until then scheduled.py and channel.py run as dry runs and their tests run on fixtures.
- L3: scheduled asks spend ASK_DAILY, which L3 owns, inside the SCHEDULED_DAILY share; the spike-explain skill honouring `spike` (optional; plain T1 works without it).

## 15. Version 7: Stage 5 app rows (added 29 September 2026)

Client skins (FEATURES.md row 34, ENGINE.md section 8) and the app side of brand lens, creator brand-fit, and the creative context pack (rows 31, 32, 35), whose answers are L3's Ask skills (L3 progress, "FEATURES 31, 32, 35"). GenAI visibility (row 33) is not built: its route is on SOURCES.md's never-used list and the client refuses it (L3 Needs Albert 3). Nothing about a client changes the engine; money, the trust gate and the three rules for anything that names a person (section 12.1) apply unchanged. BUILD.md gives Stage 5 its own task table once Stage 3 is done; until then this section is L4's plan for its part.

### 15.1 Client skins

A skin is a saved lens over the same engine: a name, markets, a query set, tracked organisation accounts, watches and a report template. BSA is the first.

- Words: the skin name, terms, hashtags and the labels of linked watches pass rule 1 through L1's `core.collect.gdelt.blocked()` (the same check collect uses on seeds and hashtags; both images carry core/collect), which also refuses mixed-script text (f42-api checks mixed script itself too, so the refusal holds whichever copy of blocked() is merged), and rule 2 by refusing any phrase whose letters, after NFKC, with spaces, hyphens, underscores and dots removed, contain "googletrends" or "trendsgoogle"; a refused word gives 400 "Not accepted (rule 1, rule 2 or mixed script)", never the word list.
- Accounts: only organisation accounts on an approved list, core/api/skin_accounts.yaml `{skin_key: [{"platform", "handle", "org", "role": "client|competitor|partner"}]}` in L4's folder. Its entries come only from Albert: he commits them or gives them to L4, who enters them exactly as given and records the source in the commit message; the reviewer checks each change against that source. A handle not on the list for that skin is refused with 400 "Only approved organisation accounts". Nothing in 42 can tell a person's handle from a brand's, so the list is the only gate; a private person can never be tracked.
- `POST /api/skins` body `{"skin_key", "name" (3 to 80 characters), "markets": ["ZA", ...], "terms": [...] (at most 30, 2 to 60 characters), "hashtags": [...] (same), "accounts": [{"platform", "handle"}] (from the list), "watch_ids": [...], "template": "weekly_report"}` returns 201. Writes go through f42-agent to intelligence_42_agent.skins `(skin_id, skin_key, created_at, status_at, who, name, markets JSON, terms JSON, hashtags JSON, accounts JSON, watch_ids JSON, template, status active|archived)`, append-only, current = latest row by the same order as v_watches_current. `PUT /api/skins/{id}` appends a new row with the full body; `POST /api/skins/{id}/archive` appends an archived row. `GET /api/skins` lists current skins; `GET /api/skins/{id}` returns one.
- `GET /api/skins/{id}/today` is Today (section 5) narrowed to the skin: its markets, and the cards whose item label or hashtags match a term or hashtag of the skin at read time in SQL (no nightly matching job), plus the alerts of the skin's watches. Cards keep their state, count_line, evidence and held-back reasons; nothing is dropped silently.
- The weekly report (template `weekly_report`, ENGINE.md section 8) is an investigation (section 13.1) scoped to the skin, started by `POST /api/skins/{id}/report`, so its estimate, both budgets, the reservation, the confirm and Start are exactly Investigations'; on finish the dossier is created from the investigation as today (section 13.2). It covers the main conversations and trends in the lens, public mentions and tags of the client's own approved handles, the tracked accounts' public activity, and the coming week's moments from the calendar. Mentions and tags use `prism/mentions`, `instagram/tagged` and X search through the SocialCrawl client under the `ask` share only, inside the investigation's ceilings. A respond-or-amplify suggestion appears only as a claim with evidence ids that passed the gate, like any other claim; there is no flag outside a claim.
- People in a report: an author who is not an approved organisation account and not allowed a named page under section 12.1 is never named. In a skin report such evidence carries no `handle`, no author name and no `url` (post URLs on X, TikTok and others carry the handle); the app links it through 42's own source panel by evidence id instead, and any `@handle` in its excerpt or in a claim quote that is not an approved account is shown as `@` followed by a mask (the stored quote stays verbatim, so the K1 check and the section 13.2 freeze re-check still pass). The source panel opened by evidence id follows the same rule, so the name is not one click away. Mentions by such people are counted, not listed. Who may be named is stored with the dossier when it is made; on display anyone then on the suppression list is masked as well, so a later suppression takes effect at once. Masking runs over every string shown (claims, basis, falsifier, so what, watch next, gaps, steps, excerpts and quotes), covers whole handles and profile or post URLs in text, and the stored bodies stay verbatim. A fixture test on the report JSON, HTML and PDF asserts that no handle, name or URL of a sub-tier author and no unapproved `@handle` appears.
- Inbound and outbound community management (direct messages, the client's replies) is not visible from public data and is not shown (ENGINE.md section 8).
- App: a Skins screen (#/skins) lists skins with New skin; a skin page (#/skins/{id}) shows the narrowed Today, the skin's watches and alerts, and "Build weekly report" as its one red action, which opens the investigation plan with its estimate, with past reports as dossiers.

### 15.2 Brand lens, creator brand-fit and the context pack

These are Ask answers made by L3's skills; the app gives each a form that fills an ask, and shows the answer through Ask's own page, checks, export and dossier path.

- `POST /api/ask` accepts an optional `skill`, passed to run_ask, one of the names L3 lists in AGENT.md's skills (proposed: `brand-implication` for the brand lens and `creator-read` for brand-fit, both already listed, and a new `context-pack`); unknown values are refused with 400.
- Tiers and money: brand lens and context pack are T2 (up to 300 credits, AGENT.md), brand-fit is T1 (up to 60). Each form shows a confirm tap with its credit ceiling, as 14.1 does. Ask takes T2 for these two skills only while f42-agent's `F42_T2_READY` is on (section 6); the app reads `t2_ready` from `/api/health`, and while it is not true the brand lens and context pack forms are not shown and send nothing.
- Brand lens (row 31): a brand, up to four competitors and a market; the answer covers share of voice, tone, competitor content and what people say the brand is. Brands are organisations; people are never the subject.
- Creator brand-fit (row 32): a creator (from a creator page, so only creators section 12.1 allows) and a brand; the answer covers fit only: the topics, formats, tone and language of the creator's public posts against the brand. Topics exclude the sensitive set, and no topic is shown until v_sensitive_items_complete exists, as for creator pages (section 12.1 rule 2). The form is not offered where the creator page is not allowed. Safety flags and audience authenticity are not built: as written they would put sensitive topics, coordination or payment next to a name, which section 12.1, TRUST.md K7 and SETUP.md data protection forbid. They wait for Albert (15.3).
- Creative context pack (row 35): a brief (up to 2,000 characters) and a market; the answer covers tensions, formats, sounds, creators, and language to use and avoid, each with proof. Creators in it follow section 12.1. From the finished answer, "Add to dossier" is the red action, as in Ask.

### 15.3 Needed for version 7

- L1: table intelligence_42_agent.skins (append-only as above); `prism/mentions` and `instagram/tagged` added to the client's priced routes for the `ask` share after a probe in a Cloud Run job (X search is priced already).
- L3: the `skill` field in run_ask and the skill names (adding `context-pack` to AGENT.md edits a spec file, so it needs Albert's yes); T2 (row 1.17) for the brand lens and context pack; the weekly report's plan template for an investigation scoped to a skin; brand-fit that reports fit only.
- Albert: the approved organisation accounts for BSA (client, competitors, partners, with platform and handle) in core/api/skin_accounts.yaml or given to L4, plus BSA's terms and hashtags; and a decision on creator safety and audience authenticity for row 32. L4 recommends: only aggregate, non-accusing figures about a creator's audience (for example the share of comments from accounts with posts of their own), no safety categories touching religion, race or ethnicity, health, politics, sex life or crime, and nothing that attributes coordination or payment.

## 16. Version 8: hiding a person from the app (added 29 September 2026)

Albert, 29 September: staff add people to the suppression list from the app. L1 owns the list (intelligence_42_core.suppressions and the view v_suppressed_creators, core/schema/core.sql on full-42-l1), which is also the route for removal requests under POPIA, NDPA and the Kenya DPA (SETUP.md data protection). The app only adds to it and shows it; every people route reads the view, so a hidden person is gone from the next read. Reading the list stays fail closed (section 1, `people_unavailable`).

- `POST /api/suppressions` body `{"creator_id"}` or `{"platform", "handle"}`, plus `"reason"` (3 to 300 characters, plain words, never contact details: text holding an email address or a phone number is refused with 400) and `"who"` (the name the staff member gives, 2 to 60 characters; the passcode is shared, so it is a given name, not a verified user, until IAP). A creator_id 42 does not hold gives 404. A handle must be a bare handle (an optional leading @ or u/, no spaces, no slashes, no URL; anything else is 400), and platform is one of 42's platforms with twitter written as x; the response says how many creators rows it matches now (the view also matches creators that appear later). f42-agent appends one row `(suppression_id "sup_app_" + 12 hex, status_at now, status "suppressed", creator_id, platform, handle, reason, who)` with a parameterised DML INSERT and returns 201 with the row and `matched`. The app words `matched` 0 as "No one matches this now; they will be hidden if they appear", never as a failure.
- No lifting from the app. A suppression, including one made in the app, is lifted only by Albert outside the app, because the list also carries removal requests that must stay in force.
- `GET /api/suppressions`, served by f42-api reading intelligence_42_core.suppressions as f42-web, lists the current row per suppression_id (the view's order: status_at DESC, not-lifted first, then the row's JSON), newest first, with status, the given name, reason, status_at and the creator_id, platform and handle it names. It is behind the passcode like every route, for the same staff who already see creator pages, and it is the only route that shows a hidden person's handle; it is never part of a skin, an export or a dossier. When the table cannot be read it answers 503 `people_unavailable`.
- Access: L1's bootstrap.py grants f42-agent roles/bigquery.dataEditor on the table intelligence_42_core.suppressions (AGENT_TABLES in bootstrap.py on full-42-l1, which lists suppressions). That role could also delete or truncate; append only is enforced in code: the route issues only one parameterised INSERT, no other code path in f42-agent writes that table (a test pins it), and model-written SQL runs through L3's sql_query guard, which refuses DML and DDL. Until Albert runs bootstrap with that line, the insert is refused; the route then answers 503 `suppression_unavailable` "Hiding people is not switched on yet." and the app says so; a missing table gives the same answer. A BigQuery quota or rate refusal gives 503 `suppression_unavailable` "Hiding people is busy; try again in a minute." A failure before the insert is sent says nothing was saved; a failure after it was sent says "The hide may not have been saved; check the hidden list before trying again." (a retry at worst adds a second hide). A write that fails is never reported as done. `who` is screened for an email address or a phone number like `reason`.
- Straight away: the people routes read v_suppressed_creators on every request (store.py caches only whether the view exists), and a committed INSERT is visible to the next query, so the hidden person is gone from creator pages, communities, community members and skin reports on the next load. After a hide, the app leaves the creator page for the list.
- App: on a creator page and beside each named member of a community, a quiet "Hide this person" action opens a dialog asking why and your name, with Hide as its one red action and Cancel taking first focus. The list lives at #/people/hidden (under More), labelled with the name each person was given as, and it has no Lift.

## 17. Fieldwork: the source roster and its health (added 2 October 2026)

Coverage (10.4) is the daily scorecard of how much 42 saw. Fieldwork is the roster: every source the 02:00 collect job is set to read per market, and how each one did on one collection day.

`GET /api/fieldwork?date=YYYY-MM-DD` (gated; `date` optional, a bad date is 400). Without `date` it reads the latest day on or before today (SAST) that has collection_health rows (`latest_day`, looked up over the last 60 days), else today. Built by core/api/fieldwork.py `build_fieldwork(store, date)`:

- The roster comes from the repo's config, so it is real even on a day nothing ran: `core.collect.job.plan(day)` (platform feeds, trending boards, own subreddits, Facebook pages, X accounts, culture desk and the kept creator rotation from core/config/uefa_creators.yaml, searches, counter reads, evidence rechecks), `core.collect.local_sources.plan(day)` (charts, the Instagram gossip accounts, App Store, RSS) and `core.public_feeds.catalog.FEEDS` (news sites, charts and radio playlists), plus one Google Trends trending read per market (search interest only, never evidence).
- `markets`: ZA, NG, KE and GLOBAL, each `{market, label, counts, planned_credits, sources}`. A source is one market and collection_health series: `{key, series, group, name, members, detail, planned_calls, planned_credits, free, status, status_words, health}`. `group` is one of `groups` (platform, own, creators, charts, news, search, search_interest). `health` sums the day's collection_health rows for that market and series: `{status, calls, calls_ok, items (a Figure, q_fieldwork_health), located_share (one part only), located_range ([min, max] over several parts), parts, parts_usable, reasons}`, or null.
- `status`: delivered, partial, failed (reasons in Today's words), no_record (the day has health rows but none for this source), waiting (no health rows for the day at all: `health_state` "absent" and `health_note` says so), unavailable (the health view could not be read: `health_state` "unavailable"), or off (a planned route the client cannot price). Google Trends status comes from the collect run's `counts.search_signal_states`; without it, "Not reported by this day's collection".
- `off`: sources the config names but does not read, each `{market, name, why}` in plain words (retired scrapes and RSS feeds, unconfirmed public feeds, Google Play until priced, the X trends archive until legal approves, Google Trends explore and rising until after the demo, markets with no Google Trends table rows).
- `credits`: the collect share of credit_ledger for the day (`spent`, a Figure, q_fieldwork_credits, and by market), against `cap` (caps.yaml ENGINE_DAILY.collect) and `planned` (the plan's hold); `state` recorded, none or unavailable. Brief, ask and other spend stays on Coverage.
- `collect_run`: the day's collect run from runs (status, times, redacted error) or null. `next_collection`: the next 02:00 SAST start of f42-collect-0200 (core/setup/schedule.py).

## 18. Seed path (added 2 October 2026)

`GET /api/seed-path?keyword=<term>&market=ZA|NG|KE` traces one word, hashtag or slang term through the posts 42 collected in one market (core/api/seedpath.py, store `seed_path_posts`). The keyword is trimmed, a leading # dropped, inner spaces collapsed and lower-cased; it must be 2 to 60 characters with a letter or number, and market one of the three, else 400 `bad_request`.

- Matching: a whole-word, case-insensitive match of the term in posts.text or posts.transcript (the Compare rule, RE2 in BigQuery), or the term among posts.hashtags (case and a leading # ignored). Each post counts once, on the market-local day of its first sighting in the market over all of post_observations, legacy and agent_live sightings aside, as Compare counts. The term and the market travel only as query parameters; every query carries the 2 GB bytes-billed cap.
- Window: the 28 days ending on the latest good aggregate run. At most 5,000 matching posts inside the window are read (oldest first); `truncated` is then true and a note says so.
- `platforms`: one entry per platform, oldest first sighting first: `platform` (twitter written x), `name`, `first_seen` (Figure, a date, all time), `before_window`, `posts_all_time` and `posts_in_window` (Figures), and `daily` `{unit, query_id, run_id, points: [{date, posts}]}` with one point per day of the window (0 is "no matching post first sighted that day").
- `side_words`: up to 12 words or hashtags that appear in the most matching posts in the window, each in at least `side_words_min_posts` (2) posts, counted once per post; mentions, links, numbers, English stopwords, id-like tokens and the term itself are left out. `posts` is a Figure.
- `topics`: up to 8 of the market's items in the latest detect run linked (post_items) to the matching posts in the window, with the card title, kind, state word, `held_back`, `href` (`#/t/<item_id>?market=ZA`) and `posts` (Figure). Creator items are never listed.
- No handle, creator id, post id or post text is returned, so no person is named by account (section 12.1). Side words are words that recur in posts with mentions removed; a recurring word can still be a name.
- Every Figure has query_id `q_seed_path` and the aggregate run id. `status` is `ok`, `no_match` (no post in the market uses the term; empty lists and a plain `message`), `not_ready` (no aggregate run yet, or posts, post_observations or post_items do not exist yet; a plain `message`), or `too_large` (BigQuery refused the read at the cap; the message asks for a more specific term). None of these is an error.
- Fixtures: seedpath_posts, seedpath_post_observations and seedpath_post_items add posts only this read sees (the term "amapiano" in ZA and NG).

## 19. Seeds (added 3 October 2026)

`GET /api/seeds?market=ZA|NG|KE` says what 42 will search for next in one market and what its last searches found (core/api/seeds.py, store `seed_queue`). market is trimmed and upper-cased; anything but the three is 400 `bad_request`. The old desk's editorial seeds (behaviours, brand plays, activation prompts) had no 42 data behind them and are gone.

- Source: intelligence_42_core.seed_queue (DATA.md section 5), append-only, written by the detect step's seed loop (core/detect/seeds.py, tomorrow's seeds) and by the news harvests (core/collect/gdelt.py, core/collect/local_sources.py), and read by the collect job each morning. A queued row has `credits_estimate` set and both yield columns null; a yield row has a yield column set. One parameterised query reads seed dates from 60 days before to 14 days after today (SAST), at most 300 rows of each grain, under the 2 GB bytes-billed cap. While the table does not exist the answer is `not_ready`.
- `queue`: the latest seed_date with queued rows for the market, or null. `{seed_date, when, seeds, lanes, creator_accounts, seeds_total, credits_total, truncated}`. Each listed seed is `{lane, lane_words, kind, label, item_id, href, platforms, priority, credits}`: `label` is the cultural_map label where item_id is set, else the query; `platforms` are the platform words the template reads (search/multi reads X, Threads and Reddit for a brand or event, and Instagram, YouTube, Reddit, X, Threads and Facebook otherwise); `href` is `#/t/<item_id>?market=ZA` when item_id is set, else `#/seedpath/<term>?region=za`; `credits` is the estimate (a Figure). Listed in priority order. `lanes` gives per lane `{lane, words, seeds, credits}` (Figures), in the order expansion, exploration, anchor.
- `when` (added after review, 3 October 2026): `upcoming` when seed_date is after today (SAST), `today` when it is today (the 02:00 collection may already have run it), `past` when it is earlier (the morning run has not written a newer list). Only `upcoming` is worded as searches 42 will run.
- `results`: the latest seed_date that has yield rows for the market, or null. Per seed the MAX of `yield_posts` and of `yield_new_creators` (as read_trials reads them): each listed seed carries `posts` and `new_creators` (Figures, null where collect recorded none), most posts first; `lanes` and `totals` carry `{seeds, posts, new_creators}`, each null where no row in the group recorded a value (never 0 for nothing recorded).
- Lanes in reader words: expansion "Following what is rising", exploration "Trying something new", anchor "Re-checking what worked", any other "Other searches". `lanes_about` carries one plain sentence for each.
- The placebo lane is never read: not listed, not labelled and not in any count or total. It is TRUST.md A2's blind control, run on items 42 did not choose on merit; a page about what 42 chose to look at has no use for it, and showing it, even as a number, invites someone to act on those items and bias the base rate. Credit totals therefore leave out the control's few credits; Coverage shows the day's whole spend.
- People (section 12.1): this covers creator accounts. A news topic can carry a public figure's name, as topic labels do on every page, and is listed like any topic. A seed whose kind or mapped item is a creator, whose template is a profile route (tiktok/profile/videos, twitter/user/tweets, facebook/profile/posts, or any route naming a profile, user, account, channel or handle), or whose query or label holds an @handle, is never listed. It is counted in `creator_accounts` (`{seeds, credits}` in the queue, `{seeds, posts, new_creators}` in results) and in its lane and the totals. Its query and item id never enter a Figure's hash. A seed for a suppressed creator (v_suppressed_creators, or a current suppression by platform and handle) is not counted at all. While the suppression list does not exist nothing is dropped; creator seeds are counts either way.
- Figures: query_id `q_seeds_queue` or `q_seeds_results`. seed_queue rows carry no run id, so `run_id` is null and the hash over the rows read (seed date, market, lane, kind, template, credits and yields, plus item and query for a listed seed) is the trace.
- `status`: `ok`, `empty` (no row for the market in the window; `message` says 42 writes the next list each morning), or `not_ready`. None is an error. `notes` say when the queue is empty but results exist, and when a list was cut.
- Fixtures: seed_queue.json (ZA queued for 1 October with results of 30 September, NG queued only, KE empty), used only with `F42_DATA=fixtures`; every query text in it names "fixture".

## 20. Lexicon (added 3 October 2026)

`GET /api/lexicon?market=ZA|NG|KE` lists the words and hashtags 42 is recording in one market, most posts first (core/api/lexicon.py, store `lexicon_terms`). `market` must be one of the three (case ignored); anything else, `all` included, is 400 `bad_request`. Before any good aggregate run exists the route answers 409 `not_ready`.

- Items: open cultural_map rows (`valid_to IS NULL`) of kind `meme` (legacy slang lands there, core/detect/legacy.py) and `hashtag`, with status `active`. Generic hashtags (status `generic`, or on the stoplist by core.detect.items.is_generic), rejected rows and creator items are never listed. Topic items are left out on purpose: their labels are 42's names for a theme, not words people write.
- Counting: posts per day from v_item_daily_current as store.item_days counts them (per day the largest lane class total, `_any` rows aside, search_presence sightings on a placebo seed_queue day aside), in the market only. The window is the 28 days ending on the latest good aggregate run, as Seed path reads. One parameterised query with the 2 GB bytes-billed cap and `LIMIT`; at most 100 terms are returned (`truncated` true and a note when more were read).
- Top level: `market`, `market_name`, `status`, `message`, `query_id` (`q_lexicon`), `run_id` (the aggregate run), `window` `{from, to, days, week: {from, to}, prior_week: {from, to}}` (the week is the last 7 days of the window, the prior week the 7 before), `kinds` (`["meme", "hashtag"]`), `terms`, `truncated`, `notes` (plain sentences).
- Each term: `item_id`, `label` (the cultural_map label, else its canonical key, never an id), `kind`, `kind_word` ("Hashtag" or "Word or phrase"), `seed_term` (the label as Seed path matches it, or null when Seed path would refuse it), `topic_href` (`#/t/<item_id>?market=ZA`), `first_seen` (cultural_map first_seen: the day 42 first recorded the item in any market, or null), `posts`, `week_posts` and `prior_week_posts` (Figures), and `change` `{percent, reason, text}`. `percent` is the rounded change of `week_posts` on `prior_week_posts`; it is null with `reason` `warming_up` while 42's first ok collect run is later than the prior week's first day, and null with `reason` `no_earlier_posts` when the prior week has no posts. A percentage is never made up.
- `status`: `ok`, `empty` (no word or hashtag had posts in the window; a plain `message`) or `not_ready` (cultural_map or v_item_daily_current does not exist yet; a plain `message`). None of these is an error.
- Fixtures: lexicon_cultural_map, lexicon_item_days and lexicon_seed_queue are read only with F42_DATA=fixtures; every label in them carries the word "fixture". The BigQuery store never reads them.

## 21. Searching now (added 3 October 2026)

`/api/today` (section 4) and `/api/discover` (10.2) carry `searching_now`: the Google search interest rows the collect job appended to intelligence_42_core.google_search_signals, in the five-field shape the app's SearchingNow strip reads (docs/full-42/contracts/google-trends.md). Built by core/api/searching.py `searching_now(store, markets, as_of)`, store `search_signals(start, end, markets)`.

- Each row is exactly `{term, market, source, rank, refreshed_at}`: `term` trimmed and non-empty, `market` ZA, NG or KE, `source` google_bq, google_trending or google_rss (Google Trends daily RSS, from 4 October 2026), `rank` a positive integer or null. `refreshed_at` keeps the UTC fetch timestamp ending in Z for google_trending; google_bq and google_rss carry their refresh day (`YYYY-MM-DD`). A legacy google_trending day is accepted but cannot establish freshness within an hour. The app marks only a live timestamp whose age is at least zero and less than one hour as fresh, and updates its clock each minute. A row that breaks the shape is left out here, so the strip never has to reject one. kind, fetched_at, raw_payload, region, week and scores are never returned.
- Market: Today carries all three markets; Discover only its `market`, or all three for `all`. A client skin's Today keeps only the rows of the skin's markets.
- Date: the read covers the three SAST days (by fetched_at) ending on the as-of day. For Discover and for Today's latest or current date, that is today in SAST, so the strip shows the morning's search data even before the brief is published; a past Today date reads up to that date. Within the window each market and source shows only its newest fetch day, so an old run never mixes with a new one.
- Order and size: one row per term in a market (case ignored), at its best reported rank across sources and top and rising lists; ranked rows first by rank, then unranked, ties by term; at most 10 a market; markets in ZA, NG, KE order.
- Fixtures (added 4 October 2026): a term of the form "X vs Y" (vs, v or versus) between two national teams, neither of which is the row's market (South Africa, SA, RSA, Bafana or Banyana for ZA; Nigeria, Naija, Super Eagles or Super Falcons for NG; Kenya or Harambee Stars or Starlets for KE), is left out, so "croatia vs england" never fills a South African strip while "eritrea vs south africa" and club derbies such as "kaizer chiefs vs orlando pirates" stay. One fixture written several ways ("eritrea national football team vs south africa national soccer team standings", "south africa vs eritrea") is one row at its best rank. The cap of 10 applies after this, so the next terms fill the strip. Display only: the table, the store read and Ask are unchanged.
- The suppression list applies (section 16), read once with the page's own read: a term is left out when a suppressed person's handle, creator id or display name, read as words with case, accents, spaces, punctuation and underscores ignored, equals any run of whole words in the term written together (so "fixture hidden handle live" names fixture_hidden_handle, and "j cole tour" names J. Cole). While the list cannot be read, no term is sent at all.
- Never posts, counts or evidence: the rows are not cards, never pass or skip the trust gate and are never cited. Rank is what Google reported, not a national ranking claim.
- Fail soft: `searching_now` is always a list. When the table does not exist yet, holds nothing in the window, or the read fails, it is `[]`, f42-api logs a warning (exception type only) and Today and Discover are otherwise unchanged. The read carries the 2 GB bytes-billed cap and is bounded by the fetched_at partition.
- Fixture: core/api/fixtures/google_search_signals.json (ZA google_bq rows on 29 and 30 September, NG google_trending rows on 30 September and 1 October).
