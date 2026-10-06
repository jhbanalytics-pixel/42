-- Trend briefs view, dashboard-ready for the Trend Engine PDF mock layout.
--
-- Powers the Looker page that mirrors Jo's example PDF (Trend / Country /
-- Platforms / Sentiment / Status table with Creative Concept and Data
-- Insights and Social References blocks below). Reshapes trend_analysis
-- rows so each one slots straight into the dashboard without per-card
-- string wrangling on the Looker side.
--
-- The pipe-delimited array fields (top_creators, social_refs,
-- platform_counts) are surfaced both as raw arrays AND as comma-joined
-- strings so a Looker calc field can choose the easier shape per panel
-- (table cell vs chip row vs inline list).
--
-- Filtered to the last 30 days to match v_market_comparison's window.
-- Dashboard panels should also filter to MAX(trend_date) per market when
-- they want today-only.

CREATE OR REPLACE VIEW `{project}.{dataset}.v_trend_briefs` AS
WITH analysis AS (
  SELECT
    trend_date,
    LOWER(market) AS market,
    query_group AS topic_group,
    trend_score,
    status_tag,
    trend_synthesis AS description_rationale,
    cultural_context AS activation_idea,
    visual_anchor,
    nano_banana_prompt,
    lyria_prompt,
    sentiment_summary,
    campaign_angles AS key_metrics,
    platforms,
    top_creators,
    social_refs,
    platform_counts,
    b24_sentiment_trajectory,
    headline,
    risk_flags,
    prompt_tokens,
    completion_tokens,
    gemini_model,
    analyzed_at
  FROM `{project}.{dataset}.trend_analysis`
  WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
),
scores AS (
  SELECT
    trend_date,
    LOWER(market) AS market,
    query_group AS topic_group,
    item_count,
    source_diversity,
    creator_spread,
    velocity_score,
    engagement_score,
    regional_score,
    genz_score,
    slang_score,
    watchlist_score,
    search_velocity_score,
    diversity_score,
    creator_score,
    tone_score
  FROM `{project}.{dataset}.trend_scores`
  WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
)
SELECT
  a.trend_date,
  a.market,
  CASE
    WHEN a.market = 'za' THEN 'South Africa'
    WHEN a.market = 'ng' THEN 'Nigeria'
    WHEN a.market = 'ke' THEN 'Kenya'
    ELSE UPPER(a.market)
  END AS market_label,
  a.topic_group,
  a.trend_score,
  a.status_tag,
  -- Tier band from scoring.yaml thresholds, recalibrated 28 May 2026
  -- (trending 0.45, emerging 0.30, monitoring 0.18). Same logic as the email
  -- render + the brief status_tag. The old 0.6/0.4/0.2 floors predated the
  -- recalibration and bucketed every live trend to Monitoring/Below (max
  -- observed trend_score ~0.36), so the dashboard never showed an
  -- Emerging/Trending moment.
  CASE
    WHEN a.trend_score >= 0.45 THEN 'Trending'
    WHEN a.trend_score >= 0.30 THEN 'Emerging'
    WHEN a.trend_score >= 0.18 THEN 'Monitoring'
    ELSE 'Below threshold'
  END AS tier,
  -- Sentiment polarity from the actual tone_score float (0..1 scale,
  -- 0=very negative, 1=very positive). Far more reliable than scanning
  -- the Gemini-generated sentiment_summary string for keywords (which
  -- false-positives on phrases like "Positive 80%, negative 5%").
  --
  -- A tone_score of exactly 0.0 means no GDELT signal contributed to
  -- this topic (e.g. pure-TikTok music topics have no news tone), not
  -- that the topic is maximally negative. Treat that as 'unknown' so
  -- the dashboard does not paint music_gengetone red.
  CASE
    WHEN s.tone_score IS NULL OR s.tone_score = 0.0 THEN 'unknown'
    WHEN s.tone_score < 0.4 THEN 'negative'
    WHEN s.tone_score > 0.6 THEN 'positive'
    ELSE 'neutral'
  END AS sentiment_polarity,
  s.tone_score,
  -- Narrative fields the email card and dashboard share.
  a.description_rationale,
  a.activation_idea,
  a.visual_anchor,
  a.nano_banana_prompt,
  a.lyria_prompt,
  a.sentiment_summary,
  -- Array fields kept native so Looker can iterate when needed.
  a.key_metrics,
  a.platforms,
  a.top_creators,
  a.social_refs,
  a.platform_counts,
  a.b24_sentiment_trajectory,
  -- Gemini headline + risk flags, additive for the downstream product.
  a.headline,
  a.risk_flags,
  -- Comma-joined renderings for table-cell display (the PDF mock's
  -- 'Primary Platforms' column reads 'TikTok, Instagram Reels').
  ARRAY_TO_STRING(IFNULL(a.platforms, []), ', ') AS platforms_joined,
  ARRAY_TO_STRING(IFNULL(a.key_metrics, []), ' | ') AS key_metrics_joined,
  ARRAY_TO_STRING(IFNULL(a.top_creators, []), ' | ') AS top_creators_joined,
  ARRAY_TO_STRING(IFNULL(a.social_refs, []), ' | ') AS social_refs_joined,
  ARRAY_TO_STRING(IFNULL(a.platform_counts, []), ' | ') AS platform_counts_joined,
  -- Volume + diversity signals from trend_scores so the dashboard can
  -- render reach metrics next to the brief without a separate JOIN.
  s.item_count,
  s.source_diversity,
  s.creator_spread,
  s.velocity_score,
  s.engagement_score,
  s.regional_score,
  s.genz_score,
  -- Additional scoring signals (additive): slang, creator-watchlist, search
  -- velocity, and the raw diversity + creator scores from trend_scores so the
  -- product can show the full per-signal breakdown behind the composite.
  s.slang_score,
  s.watchlist_score,
  s.search_velocity_score,
  s.diversity_score,
  s.creator_score,
  -- Cost ledger so the dashboard can show today's Vertex spend.
  a.prompt_tokens,
  a.completion_tokens,
  a.gemini_model,
  a.analyzed_at,
  -- Per-market rank by trend_score so the dashboard can render top-N
  -- without a window function on the Looker side.
  ROW_NUMBER() OVER (
    PARTITION BY a.trend_date, a.market
    ORDER BY a.trend_score DESC, a.topic_group ASC
  ) AS rank_in_market
FROM analysis a
LEFT JOIN scores s
  ON a.trend_date = s.trend_date
  AND a.market = s.market
  AND a.topic_group = s.topic_group
ORDER BY a.trend_date DESC, a.market, rank_in_market;
