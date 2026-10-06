# TEV2 recommendation: topic dominance + a real social sentiment signal

Author: Listening Post data-trust pass, 18 June 2026.
Audience: Trends Engine V2 owners.
Status: recommendation only. No TEV2 code changed. The Listening Post side fixes
described at the end are already shipped and mitigate the symptoms read-side.

## Why this exists

PULSE reads TEV2 output read-only, so two source-side issues surfaced as
"the data is wrong" in the dashboard. Both were vetted against live BigQuery
(`ogilvy-trends-v2.trends_v2_dev`) on 18 June. The Listening Post now works
around them, but the durable fix is in TEV2.

## Issue 1: topic classification has no dominance arbitration

`src/enrichment/topic_classifier.py` (around lines 717 to 736) appends every
keyword-matched topic family to a post and returns `sorted(matched)`. There is
no notion of a primary topic and no arbitration, so a post that mentions one
stray keyword from another family is tagged into that family as if it were
equally about it. Because the list is sorted alphabetically, array position
carries no meaning either.

Evidence (live, 14-day window): 113 posts are tagged both a `politics_*` and a
`music_*` topic. Concrete cases include a Red Bull Symphonic amapiano YouTube
video and a "Rocking the Daisies" festival post both landing in
`politics_crises`, and GDELT theme-code rows ("NATURAL_DISASTER_WILDFIRE,272...")
classified into multiple unrelated families.

Recommendation: give the classifier a primary-topic rule. Options, cheapest
first:
1. Score each matched family by match strength (number of distinct keyword hits,
   weighted by family-specific anchor terms) and keep only families within a
   margin of the top family, or cap at the top two. Emit the families in
   descending score so position 0 is the real primary.
2. Add a per-post `topic_primary` column and a per-family confidence, so any
   downstream consumer can gate on dominance rather than guess.
3. Exclude GDELT theme-code rows from classification entirely (they are not
   posts), or route them to a `news_theme` sink that never enters
   `topic_groups`.

Until then, PULSE down-weights diffuse multi-tag posts (a post tagged with more
topics counts less for each) so the amapiano clip no longer crashes the politics
wall, and excludes GDELT/aggregate rows from the voice surfaces.

## Issue 2: tone_score is news tone, near-blind to social

`tone_score` is the GDELT AvgTone normalised to 0 to 1 in
`src/ingestion/enrichment.py` (`(avg_tone + 100) / 200`, 0.5 neutral). Of rows
carrying any tone signal in 14 days: news 72,973, web 6,418, youtube 5,515,
TikTok 2,017 of 27,575, Reddit 1,537 of 27,771, and Instagram, Threads, Twitter,
Facebook and Apple Music exactly zero. So the topic-level tone is essentially the
tone of news coverage, which sits near neutral by journalistic style. The
xenophobic Gen-Z social posts carry no tone at all, which is why a politics topic
full of xenophobia read as positive.

Two separate defects compounded it: the 0 to 1 scale was being read as a signed
-1 to 1 scale in PULSE (every topic showed positive), and even once recentered
the number still measures media tone, not social sentiment.

Recommendation: stand up a real per-social-post sentiment signal. The post text
is already stored, so a lightweight per-row sentiment classification (a small
model pass or a vendor sentiment field) on the social platforms would give a
genuine audience signal. Keep the GDELT tone as a distinct "media tone" field;
do not blend the two. Brand24 `b24_sentiment_trajectory` is the only social-
derived signal present today and should be treated as the interim social mood.

Until then, PULSE recenters the media tone correctly, relabels it "Media tone
(news)", shows Brand24 social mood beside it, and shows no tone pill at all for a
topic with zero GDELT coverage rather than a false neutral.

## What PULSE already does read-side (shipped 18 June)

Relevance ranking now uses a composite score (topic focus, the engine's
genz / regional / slang signals, log-compressed engagement, recency, platform
credibility) instead of raw engagement, so real voices surface and aggregator and
news rows are pushed down. Share of voice, mention counts and reach are voice-
only and deduped. Matching is word-boundary, so a query no longer hits a GDELT
theme code. These mitigate the symptoms but do not replace the source fixes
above.
