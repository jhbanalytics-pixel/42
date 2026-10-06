-- Seed intelligence: the hidden seedable behaviours mined from the day's data.
--
-- A second cross-topic Gemini pass reads all of the day's briefs and surfaces
-- three to five cultural BEHAVIOURS (not topics) worth seeding a Nanobanana or
-- Lyria activation around before they become obvious trends. One row per
-- behaviour, a few rows per day. Persisted so the tool and the chat read the
-- same insights, each traceable to its evidence topics.
--
-- Created by scripts/migrations/create_seed_insights_table.py.
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.seed_insights` (
  insight_id STRING NOT NULL,
  trend_date DATE NOT NULL,
  rank INT64,
  -- The hidden behaviour (a cultural move, not a topic).
  behaviour STRING,
  -- The non-obvious shift the data reveals.
  the_shift STRING,
  -- Proof points: 'market/topic_group - what it reveals', the reader clicks
  -- these through to the topic stories.
  evidence ARRAY<STRING>,
  -- Why it is whitespace, the edge.
  why_hidden STRING,
  -- Why now + the window to seed before it peaks.
  timing STRING,
  markets ARRAY<STRING>,
  -- The so-what for a brand.
  brand_opportunity STRING,
  -- The activation: Nanobanana or Lyria + the angle + a paste-ready prompt.
  activation_tool STRING,
  activation_angle STRING,
  activation_prompt STRING,
  -- emerging | building | strong.
  signal_strength STRING,
  gemini_model STRING,
  prompt_tokens INT64,
  completion_tokens INT64,
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY trend_date
OPTIONS (
  description = 'Daily hidden seedable behaviours mined from the briefs by a cross-topic Gemini pass.'
);
