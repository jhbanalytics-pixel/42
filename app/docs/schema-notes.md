# Schema notes, probed live 10 June 2026

Probed against `ogilvy-trends-v2.trends_v2_dev` INFORMATION_SCHEMA plus live counts. These are the confirmed names and shapes the code uses. Re-probe before doubting them; never guess.

## Tables this product reads (read-only)

### enriched_content (the Ask retrieval table)

202,755 rows over the trailing 30 days (2026-05-12 to 2026-06-10). Columns the product uses:

- `id` STRING, `platform` STRING, `market` STRING (za/ng/ke), `content_type` STRING
- `title` STRING, `text` STRING, `hashtags` STRING, `url` STRING
- `author_handle` STRING (mask anon-looking; numeric ids exist from IG)
- `published_at` TIMESTAMP, `collected_at` TIMESTAMP (filter on `collected_at`, always with a 30-day window in the WHERE)
- `views/likes/comments/shares/engagement_total` FLOAT64
- `slang_terms` STRING (delimited string, NOT an array; split before surfacing)
- `topic_groups` ARRAY<STRING> (the engine's curated classification)
- `genz_score`, `slang_score`, `regional_score` FLOAT64

Search pattern (proven): `REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(title,''),' ',IFNULL(text,''),' ',IFNULL(hashtags,''))), @term)` OR a match inside `UNNEST(topic_groups)`. Parameterized always.

Probe counts (30d): amapiano 5,922 (rich), fifa 628 (medium), quantum computing 7 (thin-signal case).

### trend_analysis (the Today briefs)

One row per (trend_date, market, query_group). Columns: `headline`, `trend_synthesis`, `cultural_context`, `status_tag`, `platforms` ARRAY, `sentiment_summary`, `visual_anchor`, `nano_banana_prompt`, `lyria_prompt`, `top_creators` ARRAY (strings "@handle | platform | N mentions"), `social_refs` ARRAY, `platform_counts` ARRAY (strings "tiktok | 40 items"), `b24_sentiment_trajectory`, `trend_score`, `campaign_angles` ARRAY, `risk_flags` ARRAY, and `render_payload` STRING.

`render_payload` is a JSON string: `{display: {state: {badge, direction}, phase, window, in_market_pct, channels: [[platform, weight]...], confidence, search}, comment_sentiment: str, comment_themes: [str], driving_hashtags: [{tag, share_pct, mood}]}`. Absent keys mean the field did not exist that day; drop the element. Populated from 2026-06-10 onward (backfilled for 06-10).

### daily_summary (the Today verdict)

`trend_date` DATE, `summary_text`, `through_line`, `call_to_action`, `key_topics` ARRAY, `rising_topics` ARRAY, `generated_at` TIMESTAMP. Take the latest `generated_at` row per date with non-empty `summary_text`.

### v_trend_briefs (the rails / Browse)

The curated view, one row per market+topic+day: `trend_date`, `market`, `market_label`, `topic_group`, `trend_score`, `tier` (Trending/Emerging/Monitoring), `status_tag`, `headline`, `description_rationale`, `velocity_score`, `rank_in_market`, plus joined convenience columns. Latest day per market for the rails.

### pipeline_runs (freshness)

`started_at`, `finished_at`, `status`, `market`, `email_status`, per-connector row counts. Freshness = the latest success row's `finished_at` for the day. NOTE: a run that dies mid-flight writes NO row (the 10 June timeout incident), so absence of today's row with data present in enriched_content means a degraded-but-data-bearing day; fall back to MAX(collected_at) from enriched_content for the stamp.

## Identity tokens (from the PULSE mailer render layer, the visual source of truth)

ink `#0d0d0f`, paper `#f4efe4`, paper2 `#ece4d4`, vermillion `#ec3a1e`, vermillion_ink `#b62a12`, pos `#2f9e6b`, neu `#b8902a`, neg `#d24b3a`, ink2 `#3a352b`, on_ink `#f4efe4`, on_ink_soft `#ddd3bd`, on_ink_mute `#8c826c`, on_ink_dim `#998e76`. The email uses web-safe stacks; the web app upgrades to real fonts (a serif display that is NOT Fraunces, a clean body sans, a data mono), same register: dark editorial masthead, cream body, vermillion accent.

## Gotchas

- The bq CLI on this box is unreliable; the Python SDK and the REST path work. The product uses the SDK.
- `slang_terms` is a STRING, not an array.
- Engagement floats can be NULL; IFNULL before math.
- Every query carries the 30-day `collected_at` filter (cost control) and parameterized inputs (never string-built SQL).
