# Lead-time evaluation

## What the metric means

"The engine detects cultural moves early" is a claim, not a number. This harness
turns it into one. For each entry in the standing ground-truth watchlist
(`configs/watchlist_ground_truth.yaml`) it checks BigQuery for the earliest date
the move was ingested (`enriched_content`) and the earliest date it was briefed
(`trend_analysis`), against the date the move became mainstream-obvious
(`event_date`). Each entry lands in one of three classes: SURFACED (briefed),
INGESTED_NOT_RANKED (ingested but never made it into a brief), or NEVER_INGESTED.
Brief lead days is `event_date - first_briefed`, positive when the brief landed
before the move went mainstream. The summary reports the surfaced rate, the
ingested-not-ranked and never-ingested counts, and the median brief lead days
over surfaced entries.

Run it with `python scripts/leadtime_eval.py --window-days 30`. It is read-only,
makes no writes, calls no Gemini, and always exits 0: it is an observability
tool, not a gate.

## How to add a watchlist entry

Budget one analyst hour per fortnight. Add an entry to
`configs/watchlist_ground_truth.yaml` with an id (slug), market, one-line title,
event_date (the date the move became mainstream-obvious), match_terms (lowercase
substrings that would identify the move in engine text), added (today's date),
and source (a one-line receipt, e.g. "news headline" or "team knowledge, 3 Jul
standup"). Keep match_terms specific enough to avoid false positives against
unrelated content, e.g. `is it love` rather than just `love`.

## How this gets consumed

Run manually as part of a weekly ritual, or wire into morning-check as a
read-only report block alongside the reconcile shadow digest. Watch the
surfaced rate and median lead days trend over time; a rising
never-ingested count is a source-coverage gap, a rising ingested-not-ranked
count is a scoring or ranking gap.

## The honesty rule

Every entry must be added from knowledge outside the engine: news, team
conversation, culture. Never add an entry because the engine already surfaced
it. An entry sourced from the engine's own output measures nothing; it just
confirms the engine agrees with itself. The metric is only honest if the
ground truth is independent of the system being measured.
