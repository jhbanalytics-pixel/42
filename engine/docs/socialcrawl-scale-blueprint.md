# SocialCrawl scale blueprint

The path from "we replaced a vendor" to "the engine reads any audience, not just Gen Z".

Written 24 July 2026, the day SocialCrawl went live. Every number here is
measured, not projected, and the source is named. Revise it when the numbers
move, do not let it rot into aspiration.

## The goal

Every market currently hardcodes Gen Z. Topic anchors, the genz_score signal,
and the creator watchlists are all built around one audience, which is why the
BSA brief prices "broaden the audience" as a ground-up rebuild at R40k to R55k.

That constraint exists because EnsembleData could not tell us who was actually
watching. SocialCrawl can, for 5 credits a creator. Everything in this document
is a step toward the point where audience becomes a config value per client
instead of a rebuild.

The vendor switch is not the goal. It is the thing that makes the goal possible.

## Where we stand, 24 July 2026

SocialCrawl is live as of commit `3db9a05`, deployed to all four Cloud Run jobs.
Tonight's cron is the first run of the six-phase connector.

EnsembleData was cancelled on 23 July but is still serving: 5,886 rows/day for
the last fortnight, and it ran again this morning. It dies when its billing
period closes, and nobody has told us exactly when that is.

That overlap is an asset with an expiry date. Both vendors on the same pipeline,
the same markets, the same day, is a controlled comparison that cannot be
reconstructed once the old account goes dark. `scripts/vendor_scorecard.py`
captures it daily.

Credits: 19,607 of 20,000. Modelled burn 290/day. Runway 67 days.

## The measure

Outperforming EnsembleData is not a volume question. It is a quality one: rich,
accurate, trustworthy rows. Volume is scaled only once quality is stable, and
never before.

That is not a slogan, it is now enforced in two places. The rollout ladder is
ordered quality first, so the three stages that buy accuracy and richness open
before the three that buy rows. And the ladder carries a `min_row_integrity_pct`
floor of 90: a stage that drops the share of usable rows below it is rolled
back, however many rows it added. Volume cannot buy its way past quality.

The first run of the scorecard already justifies the stance. EnsembleData is
producing 7,683 rows a day of which **71.4% are usable**, meaning roughly 2,200
rows a day carry no parseable date, no resolvable author or no url. They count
toward volume and give a brief nothing to cite. SocialCrawl, still on the old
eval config, is at **87.5%**.

Seven measures, from `scripts/vendor_scorecard.py`:

| Measure | Why it is in the list |
|---|---|
| Rows per day | The number Jo will ask about first |
| Topic coverage | Distinct topic_groups the source actually feeds |
| Freshness | Median age of content at collection, EnsembleData's weakest point |
| Geo precision | Share of rows with a verified country, not one inferred from slang |
| Corroboration | Share of topics confirmed on a second platform |
| Row integrity | Share of rows that are actually usable at all |
| Cost per 1,000 rows | The commercial line |

We expect to lose on rows and win on the rest. A scorecard that wins on
everything is a bug, not a result.

Row integrity is the one to lead with. It converts the volume argument into a
quality one honestly: EnsembleData's 7,683 rows are really 5,486 usable rows,
and the gap we have to close is that number, not the headline.

One artifact to fix before quoting the corroboration number: EnsembleData reads
100% because nine query groups across five platforms means nearly every topic
lands on two or more. That is a property of its fan-out, not evidence of
quality. The measure needs a minimum-rows floor.

## The six phases

Each phase has an entry gate and an exit criterion. Do not start one until the
previous exit is met. The gates exist so a bad week cannot compound.

### Phase 1, replace. DONE

SocialCrawl carries every surface EnsembleData did, plus per-country trending,
Google News and verified creator geo. Six phases, credit-bounded twice.

Exit met: PR #285 merged, deployed, 2,015 tests green.

### Phase 2, stabilise. IN PROGRESS

Three consecutive clean crons with no connector-zero alert and burn inside 25%
of the 290/day model. Re-baseline `configs/engine_capacity.yaml` off real
medians rather than the provisional 3,000 rows/day estimate.

Nothing else changes during this phase. The point is a trustworthy baseline.

Exit: three clean crons banked, capacity re-baselined, scorecard showing a
stable SocialCrawl column.

### Phase 3, match. NEXT

Close the volume gap. The ladder in `configs/socialcrawl_rollout.yaml` opens quality first, one stage
a day, gated by `scripts/socialcrawl_rollout.py`.

Order: verified creator country across the full watchlist, then TikTok comment
threads, then weekly audience demographics, and only then the three pagination
stages that add rows. The first stage needs no connector work at all, so it can
fire the moment three clean crons are banked. The comment and audience stages
need connector surfaces that do not exist yet; pagination is a day of work and
unlocks about 1,900 rows a day for 114 credits, but it deliberately waits.

Exit is a QUALITY bar, not a volume one. SocialCrawl must hold row integrity at
or above 90%, beat EnsembleData on freshness, geo precision and topic coverage,
and only then is the volume gap worth closing. Rows per day is explicitly not an
exit criterion for this phase; if quality holds and rows stay low, that is an
acceptable state to sit in while the ladder works.

### Phase 4, replace Brand24

Detailed below. This is the phase with real engineering risk, and it is also the
one that funds everything after it.

Exit: Brand24 flag false, every dependent surface rendering from SocialCrawl,
one clean week.

### Phase 5, fund the scale

Brand24 is a monthly subscription. SocialCrawl is prepaid credits. Dropping the
former releases recurring spend that buys the latter.

This reframes the conversation with Jo from "give me more budget" to "let me
move budget I am already spending", which is a much easier yes, and it is true.

Bring four weeks of scorecard, not a promise. The Brand24 invoice figure is the
number the whole argument rests on and it is not recorded anywhere in this
repo, so it has to come from the finance side.

Exit: credit top-up approved, or an explicit decision to hold at current scale.

### Phase 6, past Gen Z

The demographics lane. `tiktok/user/audience` at 5 credits a creator, 130
credits to profile the whole tier_1 watchlist, run weekly because audience mix
does not move daily.

With measured audience data per creator, the engine can score a topic against
any audience definition rather than the single hardcoded one. Gen Z becomes a
config value. The BSA upgrade stops being a rebuild.

Exit: a market scored against a non-Gen-Z audience, end to end, in a real brief.

## The Brand24 replacement map

Brand24 produces 365 rows a day and is read by twelve modules. Your own flip
discipline says no reader means dead ingestion; the inverse bites here, because
killing ingestion with twelve live readers thins or breaks seven surfaces. This
is not a flag flip and it must not be treated as one.

What actually depends on it, and what replaces it:

| Reader | What it consumes | Replacement |
|---|---|---|
| `email_render/card.py` | 7-day sentiment trajectory chip, market level | `content_analysis/sentiment`, 20cr, daily per market |
| `analysis/generate_briefs.py` | Per-project sentiment label from `brand24_mention_sentiment` | Same source as above, aggregated per topic |
| `analysis/generate_daily_summary.py` | `_fetch_brand24_insights`, ai-insights per market | See the note below. This one is a defect removal |
| `email_render/heatmap.py` | brand24 as one platform family | Cosmetic. One fewer family, no code change needed |
| `analysis/seed_graph.py` | `BRAND24_SYNTHETIC` filter on link/author/aggregate rows | Becomes dead code, harmless, delete later |
| `enrichment/topic_classifier.py` | Brand24 label mapping, classifier layer 1 | Self-contained: it only classifies Brand24's own rows, so it dies with them |
| `analysis/event_ledger.py` | Read modelled on the insights fetch | Repoint to the content_analysis rows |

The daily-summary dependency deserves its own note. Brand24's ai-insights
endpoint returns a weekly retrospective regardless of the window requested. That
is what put a stale football result in THE READ three times in June, and it was
patched by removing the card rather than fixing the source, because the source
cannot be fixed. Replacing Brand24 removes a known defect rather than losing a
feature.

Two other documented Brand24 weaknesses that this phase resolves: the SSA
geo-filtering limits Delysia confirmed during MVP testing, and the 100,000
mentions per month cap that has never been the binding constraint but has always
been a ceiling.

Sequence for this phase, in order, no shortcuts:

1. Ship the `content_analysis` surfaces dark, alongside a live Brand24.
2. Run both for a week. Compare sentiment labels head to head on the same topics.
3. Repoint each reader, one per day, with the previous one verified.
4. Only then set `brand24.enabled: false`.
5. Delete the dead classifier layer and seed_graph filter a week later.

Cost: three `content_analysis` calls per market per day is 60 credits, against
365 Brand24 rows a day replaced by a richer six-axis sentiment read.

## Budget model

The rollout gate derives its ceiling from the balance rather than trusting a
number anyone picked:

```
allowed_daily = balance / 45
```

At 19,607 credits that is 435 a day, against a 290 a day model, so 145 a day of
headroom. As the balance falls the allowed rate falls with it and the ramp stops
on its own.

Four lanes, each at its natural cadence, is what full exploitation looks like:

| Lane | Cadence | Credits/day |
|---|---|---|
| Ingestion | Daily | 380 |
| Enrichment (geo, audience) | Weekly | 21 |
| Intelligence (Prism, content analysis) | Monthly | 15 |
| Confirmation (`search/everywhere` on a spike) | Event-driven | 40 |

That totals roughly 456 a day against a 435 ceiling. Full exploitation costs
slightly more than the current balance sustains, which is the honest answer to
"squeeze everything out of it": the constraint is the money, not the endpoint
list. The ladder walks up to the edge and stops, which is exactly what it was
asked to do.

## What could go wrong

**The overlap window closes early.** EnsembleData could stop serving any day. If
it does before Phase 3, we lose the head-to-head and the scorecard becomes a
before-and-after instead of a comparison. Weaker, still usable. Run the
scorecard daily until the EnsembleData column goes to zero.

**Volume never recovers.** If pagination underdelivers and we settle well below
the EnsembleData baseline, the brief thins permanently. Mitigation is that we
told Jo up front, so it is a known trade rather than a surprise, and the
scorecard shows what we bought with the rows we lost.

**Brand24 removal breaks a surface nobody mapped.** Twelve readers is what grep
found; a dynamic reference would not show up. Mitigation is the one-reader-a-day
sequence, not a big-bang flag flip.

**The credits run out before the value is proven.** The 45-day runway floor
exists for this. If the gate starts refusing every stage, that is the signal to
stop building and start the Phase 5 conversation.

## Cadence

Daily: `morning-check`, which now carries the five SocialCrawl watch items.
Weekly: read the scorecard, review the rollout proposal, decide.
Monthly: re-baseline capacity, review what the endpoint catalog says we still
have not shipped.

## Update, 25 August 2026: the widen is funded and deliberate

Albert's instruction on 25 August, recorded here so the trigger is written down
rather than remembered: when the credit top-up lands, open the engine up. Push
volume and depth on SocialCrawl, and make the Gen Z cut one searchable
subsection of a wider data set instead of the whole product. That is the goal
at the top of this document, now with money behind it and a date attached.

**Trigger: the 500,000 credit block clearing.** Nothing in this section starts
before the credits are in the account. Verify with
`GET /v1/credits/balance`, not with an email confirmation.

### What changed since 24 July

Brand24 is gone. It was retired in `configs/sources.yaml` on 21 August, kept
serving to the 24th, and 25 August is the first zero day. It was giving 1,504
rows a day over the seven closed days to the 23rd.

Nothing replaced it. Measured `pipeline_runs.socialcrawl_credits` is flat at 382
a day for the fourteen days to 25 August, min 381, max 383, and that includes
the first Brand24-free run. The engine did not backfill the gap, it absorbed the
loss. Total rows held up on the 25th only because GDELT swung high that day
(4,352 against 2,611 the day before), which is daily variance, not recovery.

Measured rate is 91.0 credits per thousand SocialCrawl rows over the same
fourteen days. Annual run rate at the current configuration is 139,466 credits.
Both figures are re-derived from `pipeline_runs`, per the standing rule that
every burn number written down here gets overtaken.

### The pack, and why 500,000

Oscar's bulk rates of 25 August: £399 for 250,000 (£0.0016 each), £749 for
500,000 (£0.0015), £1,299 for 1,000,000 (£0.0013). Cover against our measured
rate plus a weekly hygiene pass of 24,180 credits a year:

| Scenario | Credits/yr | £399 | £749 | £1,299 |
|---|---|---|---|---|
| today, no widen | 163,646 | 18.3 mo | 36.7 mo | 73.3 mo |
| 1.5x widen | 233,380 | 12.9 mo | 25.7 mo | 51.4 mo |
| 2x widen | 303,113 | 9.9 mo | 19.8 mo | 39.6 mo |
| 3x widen | 442,580 | 6.8 mo | 13.6 mo | 27.1 mo |

500,000 was taken because the widen is the plan, not a contingency. It holds
about 20 months at double the current volume, which is the scenario this section
describes. Stepping to it costs £49 more than two 250,000 packs, so the choice
is about not renegotiating mid-quarter rather than about the rate.

At 3x the £749 block is 13.6 months, which is the point where the million-credit
rate stops being a nice-to-have. That is the number to watch once the widen is
live: if measured burn passes roughly 900 a day, start the next conversation.

### Order of work once credits land

1. Re-baseline. Run a week at the new configuration before quoting any burn
   figure to the vendor or to Jo. The 190,000 a year quoted to Oscar on 24
   August was a forecast of exactly this widen and it was wrong at the time,
   which is what a projection quoted as a measurement always is.
2. Widen the existing phases before adding new ones. NG creator coverage is
   currently tier_1 and tier_2 only; `configs/creators/ng.yaml` holds 95, 144
   and 5 handles in tier_3 across TikTok, Instagram and Threads that the engine
   has never called.
3. Clear the dead handles first. 23 distinct handles fail every morning across
   29 handle and surface pairs. They cost nothing on a hygiene pass but they are
   coverage we think we have and do not. The weekly pass at 5 credits a handle
   is 24,180 a year for our 93.
4. Audience as a config value. This is the goal in "The goal" above and it is
   the item the widen exists to fund. Gen Z becomes one selectable cut rather
   than the hardcoded assumption in the topic anchors, the genz_score signal and
   the creator watchlists.

### The cost ceiling is the real constraint, not the credits

GCP month-to-date on 25 August is $44.83 with a month-end projection of $57.69
from 24 closed days, against a $60 budget cap. That is 96.2% of the cap before
any widen. SocialCrawl credits are a separate, already-bought line, so the
widen does not touch that number directly, but more rows means more enrichment,
more embedding calls and more Vertex spend downstream. Raise the cap
deliberately before the widen rather than discovering it mid-month.
