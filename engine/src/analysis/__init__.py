"""Analysis layer (Phase 2): Vertex AI Gemini trend synthesis + activation briefs.

Reads scored topics from BigQuery `trend_scores`, samples enriched content
and creator data, generates structured per-topic briefs via Vertex AI
Gemini, and writes results to `trend_analysis` and
`creator_briefs`. Output flows into the daily email digest in the shape
of Jo's PDF mock (description, activation idea, key metrics, social
references, top creators).

Components:
- ``gemini_client.GeminiClient`` is a thin wrapper around the
  ``google-genai`` SDK targeting Vertex AI on the project's GCP account.
  Returns parsed JSON + token counts for cost tracking.
- ``prompts.trend_brief.build_brief_prompt`` constructs the prompt
  string + response schema for a single (market, topic_group) brief.

Live API calls hit the model named by ``GEMINI_MODEL``, currently
gemini-3.5-flash on the global endpoint; ``gemini-2.5-flash`` is only the
code fallback in ``gemini_client`` and the pin on the reconcile resolver.
Cost envelope is roughly $16 per month at our scale on 3.5-flash, whose
output tokens include billed thinking; it was about $5 on 2.5-flash. All
prompts and responses are deterministic-shaped JSON so the email
renderer and the BQ writer can rely on a fixed schema.
"""
