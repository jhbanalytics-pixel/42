# PULSE Intelligence: Next Level Direction

3 Jul 2026. Output of the intelligence-next-level audit (phases A-C receipts in docs/intelligence-next-level-notes.md). This is the D-phase deliverable: core readiness, the relevance law, the daily accuracy checks, the open possibilities, and the 30-day programme.

The audit's one-line verdict: the math is honest and the pipes deliver, but the selection layer decides what humans see and it favours the already-loud. Every quality failure found this fortnight (0% seed precision, 50% cultural miss rate, 83% corroborated-rate) is a selection, gating, or calibration decision sitting on top of data the engine already holds.

## D1. Intelligence Core readiness

| Component | Exists | Live | Gating render | Cost | Effect on FP/FN/TR |
|---|---|---|---|---|---|
| Corroboration scorer (corroboration.py) | yes | computing + persisting every run | shadow, nothing reads the columns | $0 (pure python) | TR guard once fixed; today it would over-award: 83% of topic-days corroborated, news-default leak inflates factual families |
| Event ledger + reconcile | yes | LIVE shadow in prod cron (RECONCILE_ENABLED=true since 27 Jun) | renders nothing; operator CLI digest only | ~+$8/mo, $15 ceiling | TR core. Health UNVERIFIED this audit; the promotion runbook's ~10-clean-day window has not been evidenced with daily digest reads |
| Grounding verifier (grounding_verifier.py) | stub only | dark, returns empty receipts | n/a | $0 now; Vertex spend to build | TR: per-claim receipts. Blocked on build + cost approval |
| relevance.py | does not exist | n/a | n/a | n/a | The D2 law below is its spec |
| claim_receipts | does not exist (no module, no table) | n/a | n/a | n/a | The defend-the-number-with-receipts-open store; pairs with the grounding verifier |

Readiness verdict: NOT ready to promote to rendered labels. Two blockers, both cheap: the corroboration family map must stop defaulting unknown sources to news (one line plus a test), and the corroborated floor must be recalibrated against the observed distribution so the tier discriminates (a label that fires on 83% of rows is decoration). Reconcile shadow needs the runbook's clean window actually read, daily, before any promotion decision. Nothing here needs new money.

## D2. The relevance law

Three verbs, three different evidentiary bars. Loudness is not defensibility. No blended mush score anywhere.

ASSERT. A factual claim rendered as prose in a brief or on a card. Requires at least one factual-family receipt (news, search, youtube, music) resolvable to a real source, with a date. A claim supported only by social chatter is rendered AS social chatter ("TikTok volume says X") and never as fact. Stale-contradicted claims follow the reconcile law once promoted.

RANK. Position on the board, in the email order, in the ticker. trend_score may rank anything ingested; ranking is a claim about attention, not truth. But tier LABELS that imply trust (Key, corroborated, confidence chips) require the corroboration tier to be above social-only. A loud social-only topic can top the board; it cannot wear a trust label.

PROPOSE. A seed candidate offered for taxonomy or watchlist. Requires cultural signal, not volume: hot-topic co-occurrence above zero, or slang-type provenance, or a genz floor. Frequency and platform mix alone can never propose (this fortnight they proposed "customer"). The committed fix enforces the eligibility half; the co_occur revival below completes it.

## D3. Accuracy daily checks

1. Composite recompute: exists (watchdog composite_integrity). Keep.
2. Corroboration law: social-only-corroborated stays a hard fail (exists). Add: corroborated-rate band alert (over 60% of topic-days corroborated = over-award FAIL) and unknown-source-family counter (any row whose source string is not in the family map = FAIL, catches the news-default leak forever).
3. Reconcile mix: read reconcile_shadow_digest every cron day; any stale correction that inverts a true claim resets the promotion window (runbook rule, now actually scheduled).
4. Seed precision@K: weekly human grading in LP (approve/reject already persists via rejection memory); track precision@10 per market; target at or above 30% within 30 days of the fix landing.
5. FN miss rate on a standing watchlist: productize the B5 method. Keep a rolling 10-15 move ground-truth list per fortnight (one analyst hour), classify surfaced / ingested-not-ranked / never-ingested, alert under 60% surfaced.

## D4. Open possibilities

Data spine.

1. Google Trends trending RSS connector (geo=ZA/NG/KE). Verified live 3 Jul. Failure mode: Google kills or reshapes the feed. Cost: free, days of work. Dependency: none, mirrors the RSS connector. Kill criteria: feed dead or duplicate of BigQuery signal in a 14-day A/B.
2. Wikipedia per-country pageviews (top by country, CC BY-SA, no auth). Failure mode: attention skews non-Gen-Z. Cost: free, days. Dependency: none. Kill: under 10% of top articles ever map to taxonomy topics in 14 days.
3. Bluesky Jetstream consumer. The only open social firehose; free social family, hedge against paid vendor lock-in. Failure mode: SSA volume too thin to matter. Cost: free, ~a week (streaming consumer). Dependency: infra for a long-lived listener. Kill: under N relevant SSA posts/day after 14 days.
4. SocialCrawl eval channel (#226, already merged) graduation decision folds into the same 14-day read.

Trust spine.

5. weights_version / config_hash stamped on every scored row. Failure mode: none meaningful. Cost: hours. Kill: n/a, pure win.
6. Persist the full seed_fit (including distinctiveness) plus a written rationale string per candidate. Cost: hours. Effect: the unaccountable quarter becomes accountable, LP can show why.
7. Corroboration hardening: unknown source family defaults to "other" (never news), floor recalibrated to the observed distribution, the two corroboration laws (corroboration.py vs reconcile.py) unified into one config block. Cost: days. Kill: n/a.
8. claim_receipts table + grounding verifier build-out. Every asserted claim carries resolvable receipts; the D2 ASSERT bar becomes enforceable code. Cost: Vertex spend (needs the B2 cost approval) plus ~a week. Failure mode: receipt resolution slows Phase 2. Kill: receipt-check latency breaks the cron window.

Discovery loop.

9. co_occur revival. Diagnose why it is zero on 100% of candidates: HOT_SEED_SCORE=0.5 gate too high for observed seed_scores, or seed_graph rows carrying empty topic_groups. Cost: days (calibration, no schema change). Kill: if co_occur stays under 10% non-zero after calibration, the component is misdesigned; redesign instead of tune.
10. Bigram / entity-aware term extraction. Unigrams produced "save" and a lowercased channel ID; the cultural unit is "mopepe challenge", not "challenge". Cost: ~a week. Failure mode: term explosion (cap exists at 5000/market). Kill: junk share of proposals does not drop vs the stoplist-only baseline.
11. Sub-topic micro-brief slot. The FN killer: the six missed moves all dissolved into broad buckets. Give small-N but culturally distinct clusters (14-17 rows) one rendered line each below the main briefs, fed from seed_graph novelty, no taxonomy change needed. Cost: prompt + render work, ~a week. Failure mode: noise creep into the email. Kill: micro-slot precision under 50% over 14 days.

Analyst loop.

12. LP approve/reject writes back to seed_candidates status (buttons exist visually; the loop currently consumes nothing: 23/23 rows still pending). Closes the human half of the discovery loop and feeds precision@K. Cost: days (LP is a separate repo; needs its own change).

Product shape.

13. "What you are not seeing" panel: the standing FN watchlist verdicts plus the named WhatsApp/Facebook blind spot, rendered honestly. Turns the audit's one-pager into a permanent product surface. Cost: render work after D3 item 5 exists.

Do-next (max 3): 7 (corroboration hardening) because it unblocks Core promotion and kills the biggest latent TR; 9 (co_occur revival) because PROPOSE has no relevance anchor without it and the committed fix only closes the junk half; 11 (micro-brief slot) because it converts the 50% FN rate directly into visible product value from data already paid for. 1 and 2 (free pipes) ride behind these as W3+ capacity allows; 5 and 6 are hour-scale and land inside W1.

## D5. 30-day programme

Week 1, measure and honesty. Merge PR #228. Stamp config_hash + weights_version (item 5). Persist distinctiveness + rationale (item 6). Corroboration news-default fix + unknown-family watchdog check. Start daily reconcile digest reads and the standing FN watchlist. Nothing renders differently yet; everything becomes measurable.

Week 2, one formula/gate fix. co_occur revival (item 9): calibrate HOT_SEED_SCORE against observed seed_score distribution, verify seed_graph topic_groups density, re-run the B2 grading on a week of post-fix candidates. Target: precision@10 at or above 30% and co_occur non-zero on at least half of proposals.

Week 3, core promotion or deepen shadow. Recalibrate the corroborated floor on the leak-fixed distribution. If the runbook window is clean across the daily digest reads, promote Phase 1 (labels render, per docs/intel-core-activation-runbook.md); if not, stay shadow and say why in the notes. Free-pipe pilot (Google Trends RSS) starts if capacity allows.

Week 4, surface truth in LP (handoff to the visual programme, not this one). Rationale strings and safety_flags on cards, the distinctiveness bar, a one-line seed-vs-trend explainer, the "what you are not seeing" panel fed by the watchlist. LP is a separate repo with its own review path.

Non-goals for the 30 days: no new paid vendors, no WhatsApp/Facebook workaround (name the blind spot instead), no LP visual redesign inside this programme, no blended trust score, no second full-pipeline runs for testing.

## Verification hooks

Each week closes with evidence, not intention: W1 = watchdog shows the two new checks passing and the digest read log exists; W2 = re-graded candidate table in the notes; W3 = promotion decision recorded with the window receipts; W4 = LP PR links. The audit notes file stays the ledger of record.
