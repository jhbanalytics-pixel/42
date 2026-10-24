"""The morning brief Cloud Run job (BUILD.md 1.12 and 1.13, ENGINE.md sections 1 and 3, TRUST.md section 2).

A fixed pipeline, not agent runs. Per market: the top 10 candidates of the current detect run by worth_raw, named
items first, with a candidate held for invalid data days (G1) not taking one of the 10 slots, so the next ranked
rows of the top 90 are judged in its place (_candidates), an SQL evidence pack each, the publish gate (core/trust/gate.py, plus G4 for items detect marked
likely coordinated, which gate_card does not read), then one card per set of posts: a Today-bound candidate with
fewer than 3 posts not already on a higher card, or with half or more of its posts on one higher card, merges into the
card it shares most posts with, listed there under also and recorded in the counts, and does not take one of the 10
judged slots either (_fold_plan), before any confirm or model spend (and, after the explanations, the published cards
of one market that describe the same event are recorded in shadow, core/brief/same_event.py), one confirm search each for the
Today-bound ones inside the confirm share, then
for the ones held only by the post floors (fewer than 3 posts it can show or 2 local), with
what it found added to the pack facts as presence only, then one structured explanation each, 5 at a time in
rank order across the markets. Confirm results also go into posts and post_observations through lane L1's
core.collect.parse.ingest (not in replay mode; "unavailable" in the counts while this branch lacks it), counted
per market. A Today-bound candidate's pack is not read again after confirm, so those posts are citable for it only
from the next evidence read; a floor-held candidate whose search wrote posts has its pack read again and is gated
again with the same floors, so it can become a card in the same run (counts "regrown"). Seasonal items (G8) go to moments and are neither confirmed nor explained. The
confirm-share SocialCrawl client is built right after chain.begin; if it cannot be built, confirm is skipped
with a thin-coverage note and the brief goes on. No explanation starts once
chain.past_deadline is true, once the run is FINISH_MARGIN short of its task timeout (chain.TIMEOUTS), or once
the day's model spend reaches the current cap; whatever is unexplained then is held back with its reason
shown (G10). A model that refuses for capacity (429) is waited out call by call, with backoff, never past the deadline,
that time limit or the SAST day, and the breaker stops the run's calls only once it keeps refusing; an item the busy
model left unexplained says so in its failed_reason. A run the breaker stopped on 429 before 06:15 waits 10 minutes and
explains what is left once more (RESUME_WAIT_S, recorded as model_resume in the counts); a second stop is final. No G1 backfill round, and no market's confirm or regrow, starts
past the deadline or that time limit either. The gate runs again with each explanation's result, and one briefs row
per market (payload: the Market object of core/api/contract.md section 4) plus the claim_checks rows are appended.
Brief is the last stage of lane L1's chain, so nothing is started after it.

The suppression list (SETUP.md data protection) is read once before anything else, as f42-api reads it: a
suppressed creator's posts never enter a pack, and a label, title or text naming them is masked before the model
reads it and again on the stored payload. A list that cannot be read stops the brief with SuppressionUnreadable,
the run finishing failed with nothing written: a stored brief is kept for good, and one written with its authors
masked could no longer be matched to the list when Today reads it.

When the upstream stage is not ok: before 06:15 SAST the job exits 1 without writing, so a retry can still
publish a full brief; from 06:15 it publishes a data-issue row for every market and finishes the run.

A finished brief run is recorded ok, also when a market row is partial or data_issue: the row carries that
status, and v_briefs_current shows only briefs whose run is ok.

Entry point: python -m core.brief.job. Environment: RUN_DATE (the brief date, else today in SAST), FORCE_RERUN=1
and CLOUD_RUN_EXECUTION (both read by lane L1's chain.begin). Credentials are the job's own identity.
"""

import hashlib
import json
import math
import os
import random
import re
import sys
import threading
import traceback
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from time import sleep as _sleep

from core.brief import confirm as confirm_lane
from core.brief import gatectx
from core.api.store import creator_key
from core.api.today import _named_pattern, _today_card_fault, without_hidden
from core.brief import pack_order
from core.brief import pattern_formats
from core.brief.evidence import OFFSETS, SuppressionUnreadable, build_pack, read_hidden
from core.brief.explain import CHECK_INCOMPLETE, INCONSISTENT, STANDINGS, TITLE_RULE, explain_trend
from core.brief.locality_audit import build_locality_audit
from core.brief.market_scope import read_market_scope
from core.brief.payload import (MODEL_BUSY, MODEL_REFUSED, NOT_ASSESSED_REASONS, NOT_REACHED_TEXT, NOT_RUN_WORDS, _today_order, _worth,
                               SHOWN, brief_row, build_market_payload, preview_card)
from core.brief.same_event import near as same_event_near, reason as same_event_reason
from core.brief.same_event import fixture_anchor, fixture_reason
from core.brief.specificity import MIN_EVIDENCE, assess_specificity, counted_local_posts, showable_posts
from core.collect import chain as collect_chain
from core.config.caps import model_daily_usd
from core.detect import sqlrun
from core.detect.job import PROJECT, RULE_VERSION as DETECT_RULE_VERSION
from core.detect.sqlrun import AGENT, CORE
from core.llm.gemini import GeminiModel
from core.trust import locality, retained
from core.trust.gate import Decision, gate_card, market_banner
from core.trust.locality import V2_BASIS, locality_block, read_locality, row_from_prefixed, scope_basis

# Every decision in the tree that changes what a brief row says for the same inputs adds one token, so a reader can
# split on "+": the pack order (member first), W8-DEC-02 the G10 hold reason shown, W8-DEC-06 the G4b political list,
# W8-DEC-11 the G1 zero yield, W8-DEC-12 news posts outside the 2 local rule, W8-DEC-14 the retained check rows,
# W8-DEC-15 the one extra support or critic call, W8-DEC-16 unrelated authors for Corroborated, W8-DEC-17 the earned
# Local label, W8-DEC-03d the narrowed K6 score and date terms switched on, W8-DEC-06b ballot and voter political only
# beside a party, leader or election term, k6-b7-decade the K6 decade rule of closure review B (CB-4), k6-names the K6
# false hold exemptions for names, titles and identifiers, k6-n13-n15 the N13-T and N15 K6 age words, rule1-old-age the
# rule 1 words old man, old woman and elders, g1-gap a missing baseline day after history began read as invalid for G1,
# zero-count-hold a card with a measured zero creators or posts in its counted 3 days held, e-order-breadth Today's
# shown order by the card's creators in 3 days, then posts in 3 days, then worth (core/brief/payload.py _today_order),
# e-one-event-shadow the published cards of one market that describe the same event recorded in shadow
# (RELEASE-E-PLAN 8h, core/brief/same_event.py, _merge_events): it changes nothing in a brief row and marks the shadow
# era, so the live merge at E3 takes a token of its own, brief-504-retry a support or critic check the model service
# answered with 504 tried once more when a whole call fits before the cutoff; a failed retry keeps the first 504
# (core/brief/explain.py check_call), pattern-first-selection the pool orders candidates whose own posts show a sound or
# enriched format adopted by 3 or more creators on 2 or more days before ones the code sees as one creator, a calendar
# moment, mostly sponsored or mostly news, ahead of worth_raw (core/brief/sql/brief.sql candidates), fold-half-shared a
# Today-bound candidate with half or more of its posts on one higher card folds into it and takes no judged slot
# (_fold_plan), pack-formats-sound the support check, critic and writer see each post's sound id and enriched formats
# (core/brief/explain.py SUPPORT_FIELDS), k6-sound-label a claim or sentence naming one of the pack's sound letters is
# cut by code (core/brief/explain.py _sound_label_rows), prompt-formats-sound the support check and the critic are
# told what each post's formats and sound mean (core/brief/explain.py SUPPORT_SYSTEM, CRITIC_SYSTEM),
# critic-pattern-counts the critic gets a line counted by code of the sounds and formats 2 or more different creators
# share among the posts shown (core/brief/explain.py _shared_patterns), writer-pattern-lead the writer leads with the
# format, sound or practice the cited posts share and words a trend as news reaction only when that is all the posts
# show (core/brief/explain.py WRITER_SYSTEM), critic-inconsistent a critic answer critic_is_valid rejects is a cut
# with its own standing, wording and reason code, never stored as not ruled out (core/brief/explain.py INCONSISTENT),
# pattern-skip-talking-head the pattern line leaves talking_head out as it leaves other and news_clip out, since the
# prompts say it alone shows no adopted practice (core/brief/explain.py PATTERN_SKIP_FORMATS), writer-critic-agree the
# writer hears the critic's own adoption bar instead of resting on different creators, no sponsored flags and
# different days, cites every post that shows the pattern it leads with, and the writer, the critic and the second
# draft share one why-now definition (core/brief/explain.py WHY_NOW_DEFINITION, _writer_pattern_line), pack-video-fields
# the pack reads the video step's on-screen text and its note's format and sound (post_enrichment screen_text,
# video_notes) as model-only fields the support check and the critic see and the writer does not
# (core/brief/evidence.py _video_notes, core/brief/explain.py MEDIA_FIELDS), critic-rivals-line the critic gets a line
# written by code saying which simpler explanations detect's numbers show, do not show or could not assess, with the
# thresholds and the 10-post floor, beside the raw shares it already saw (core/brief/explain.py _rivals_line),
# author-step-note an observation K5 labels inferred only because none of its cited posts has an independent author
# reaches the support check with a label note and is judged as an observation, its label unchanged
# (core/trust/claims.py inferred_only_by_author_step, core/brief/explain.py AUTHOR_STEP_NOTE), pack-board-only-mark
# a pack post whose every sighting in the window was the YouTube trending board is marked board_only to the writer
# and the critic as a model-only field, with a line naming such posts; the pack order, the post's locality and the
# board's place in source_market are unchanged (core/brief/sql/evidence.sql s, core/brief/explain.py _board_only_line),
# creator-letters each pack post carries a code letter for its author, written from the post's creator id and never
# the id itself, so the writer, the support check and the critic see which posts one author made, and the counted
# pattern line counts a creator with no handle by that id (core/brief/explain.py _author, _creator_letters),
# data-issue-full-judgement a market that judged its full CANDIDATES keeps the status its judged cards make when
# over 30% of the candidates considered were held for data, the banner saying the judged ones were not affected;
# a market that judged fewer is data_issue as before (_market_payload, core/brief/payload.py build_market_payload),
# claim-floor-fault the code checks count a claim-count fault only below the 2 claims an explanation publishes on,
# not below the 3 the writer is asked for, so the one repair round is kept for a cut that holds the draft
# (core/brief/explain.py CLAIM_FLOOR, _faults), writer-no-earliest when no post is marked earliest_in_pack (a tie,
# or an unreadable time) a line written by code tells the writer so and to write no earliest-post claim, and the
# writer's ask names the mark as the condition for one (core/brief/explain.py WRITER_SYSTEM, _writer_user),
# fixture-reaction-code a primary fixture reaction held before publication keeps its G3 hold with its own wording
# and a hold code of its own on the held item instead of the generic Not confirmed (FIXTURE_REACTION,
# FIXTURE_REACTION_CODE, core/trust/retained.py REASON_CODES, core/api/held_words.py), pattern-video-formats the
# pool's adoption count reads the video step's format (post_enrichment video_notes) beside the enriched formats, with
# the same exclusions and the same 3 creators on 2 days; rank only (core/brief/sql/brief.sql pattern_video),
# writer-rivals-line the writer gets the critic's line of simpler explanations checked by code and is told to name the
# rival and show the pattern beyond it, writes no pattern claim when the posts only react to an event, and the critic's
# evaluate-only line is scoped to local_why_now (core/brief/explain.py WRITER_SYSTEM, CRITIC_SYSTEM, _writer_user),
# video-format-counted the pattern line counts each post's video_format beside its enriched formats, with the same
# exclusions, and the writer sees video_format (core/brief/explain.py _shared_patterns, _writer_user),
# pattern-local-adoption only local posts (the local_creators rule) count toward an adopted sound or format in the
# pool; rank only (core/brief/sql/brief.sql pattern_posts.is_local), generic-label-last a topic named only by
# platform-generic tag words ranks after the other named rows (_generic_label, _ranked), fold-rank-order folds within
# a round, and the regrow merge, run in pool rank order before worth so a pattern item is not folded into a broader
# card (_fold_plan, _regrow),
# critic-pattern-citable the critic's counted pattern line counts only the posts the writer may cite, as the writer's
# own line does (core/brief/explain.py _citable_pack, CRITIC_PATTERN_SCOPE), critic-detection-facts the critic gets the
# facts lines from detection the writer gets, with the calendar moment on each local post's day (core/brief/explain.py
# _critic_user), pack-lane-member-marks each pack post carries measured and market_member as model-only fields to the
# writer and the critic, with a critic line on weighing them against a collection or grouping artefact; the pack order
# and scope are unchanged (core/brief/evidence.py _record, core/brief/explain.py CRITIC_SYSTEM),
# critic-patterns-recorded the pattern line the critic read is kept beside code_rivals on the critic row and answer as a
# shadow record (core/brief/explain.py _critic_patterns), redraft-critic-rival the critic-informed second draft gets the
# simpler explanation the critic named beside its reason (core/brief/explain.py _redraft_user), critic-rival-flags the
# critic row carries the critic's own news_driven and scheduled_event flags, and the held wording names a news or
# scheduled event from them, not from the free text, while the dead news or event pass on local reaction is gone
# (core/brief/explain.py _critic_row, STANDINGS, _critic_menu), writer-crowd-handles the writer's Counts line says a
# crowd word is checked against the cited posts' handles only and how to word more creators than they show, the check
# unchanged (core/brief/explain.py _writer_user), sentence-numeral-resting a numeral in the explanation sentence must be
# pinned on a claim the sentence rests on, checked by code with K2's sentence reason code (core/brief/explain.py
# _resting_numeral_rows), board-line-feed-wording the board line words a board place as ranked on the market's YouTube
# trending board, the wording K3 passes, and the support check sees board_only and is told what it shows
# (core/brief/explain.py _board_only_line, _support_user, SUPPORT_SYSTEM), writer-numbered-names law 8 tells the writer
# how K2 reads a year and that a numeral inside a name such as Part 2 is no pinned number, K2 unchanged
# (core/brief/explain.py WRITER_SYSTEM), writer-rival-post-text the writer names a rival and an event date only as a
# cited post's own text states it, since the sentence's support check sees only the cited posts (core/brief/explain.py
# WRITER_SYSTEM), not-run-reason a Today-bound item whose explanation was started and not completed names the cause in
# failed_reason, still a G10 hold with the reader wording unchanged (core/brief/payload.py NOT_RUN_WORDS,
# _payload_candidate), pattern-line-not-pack-numbers the writer's pattern line says its creators and days are not pack
# numbers, as the Counts line does (core/brief/explain.py _writer_pattern_line), why-now-inferred-adoption with the
# critic's simpler explanation ruled out and no local why-now a cited post states, an explanation passes when code
# counts a format, sound or practice adopted by 3 or more independent local creators on 2 or more days among the posts
# the writer may cite, its why-now labelled inferred on the stored card and in the app; a rival left standing still
# holds (Albert, 22 Oct 2026; core/brief/quality.py, core/brief/explain.py adopted_pattern, core/brief/specificity.py),
# model-error-retry an explanation that ended in a model error is made once more while a whole model call still fits
# before the deadline and the time limit and the day's spend is below the cap, the card showing the second attempt and
# both billed (_retry_admits, _after_retry), adopted-first the explanations of pattern bucket 0 candidates of every
# market run before the rest (_in_rank_order), genre-formats-skipped a genre format (skit, tutorial, slideshow,
# meme_image, duet, stitch) counts toward no adopted format in the pattern bucket, the pattern line the writer and the
# critic read or the count behind an inferred why-now, from one list (core/brief/pattern_formats.py; 23 Oct 2026).
RULE_VERSION = (DETECT_RULE_VERSION + "+pack-member-first+w8-dec-02+w8-dec-06+w8-dec-11+w8-dec-12+w8-dec-14"
                "+w8-dec-15+w8-dec-16+w8-dec-17+w8-dec-03d+w8-dec-06b+k6-b7-decade+k6-names"
                "+k6-n13-n15+rule1-old-age+g1-gap+zero-count-hold+e-order-breadth+e-one-event-shadow"
                "+brief-504-retry+pattern-first-selection+fold-half-shared+pack-formats-sound+k6-sound-label"
                "+prompt-formats-sound+critic-pattern-counts+writer-pattern-lead"
                "+critic-inconsistent+pattern-skip-talking-head+writer-critic-agree+pack-video-fields"
                "+critic-rivals-line+author-step-note+pack-board-only-mark+creator-letters"
                "+data-issue-full-judgement+claim-floor-fault+writer-no-earliest+fixture-reaction-code"
                "+pattern-video-formats+writer-rivals-line+video-format-counted+pattern-local-adoption+generic-label-last"
                "+fold-rank-order"
                "+critic-pattern-citable+critic-detection-facts+pack-lane-member-marks+critic-patterns-recorded"
                "+redraft-critic-rival+critic-rival-flags"
                "+writer-crowd-handles+sentence-numeral-resting+board-line-feed-wording+writer-numbered-names+writer-rival-post-text+not-run-reason+pattern-line-not-pack-numbers"
                "+why-now-inferred-adoption+model-error-retry+adopted-first+genre-formats-skipped")
MARKETS = ("ZA", "NG", "KE")
WORKERS = 1
PACK_WORKERS = 8  # evidence packs and gate contexts; the BigQuery client is thread-safe
WARMUP_DAYS = 14
WINDOW_DAYS = 7
BOARD_ENTRIES = 10
CANDIDATES = 10  # per market: candidates judged that G1 does not hold
# per market: brief.sql candidates' top 90, the most candidates prepared and gated. Its LIMIT must equal POOL
# (test_brief_job pins the two). 90 leaves room for 10 judged when G1 holds most of the top: on 3 Oct staging held
# about 27 youtube-led items per market. Rows are prepared only as needed (_candidates), so judged work stays 10.
POOL = 90
INSERT_BATCH = 500
SAST = timezone(timedelta(hours=2), "SAST")
SQL = Path(__file__).parent / "sql" / "brief.sql"
BOARD_WORDS = {"board_tiktok_hashtag": "TikTok hashtag board", "board_youtube": "YouTube trending board",
               "board_apple_music": "Apple Music chart"}
NO_CONFIRM = {"kind": "thin_coverage",
              "text": "Cross-platform confirmation did not run today: cards rest on 42's own collection only"}
NO_COUNTED_DAYS = "No creator posted about it in the last 3 days"
PACK_MAX, PER_CREATOR = 12, 2  # core/brief/sql/evidence.sql: at most 12 posts in a pack and 2 per creator
# Leave this reserve for the boards, moments, claim_checks, briefs and chain.finish. Each model dispatch reads the
# remaining allowance to this cutoff and the daily deadline. HTTP phase timeouts are clamped again after auth;
# no later check or retry is admitted after the cutoff. The owned async request also cancels at its absolute elapsed
# deadline and acknowledges cleanup before returning. Final authentication-worker shutdown can still take longer.
FINISH_MARGIN = timedelta(minutes=8)
# A model that refuses a call for capacity (see _rate_limited: Vertex's 429 RESOURCE_EXHAUSTED on its shared capacity,
# on and off on 6 Oct 2026) is waited out, not given up on at once: the refused call is made again after
# BUSY_WAIT_S, doubling each time up to BUSY_WAIT_MAX_S, each wait up to BUSY_JITTER longer at random, at most
# BUSY_RETRIES times. The breaker trips once BUSY_TRIP_ITEMS explanations in a row ran out of tries, or as soon as
# the next wait would end past the deadline, the time limit or the SAST day. PACE_S is a gap before each explanation
# after the first (none by default). Each can be set through the environment variable of the same name with a BRIEF_
# prefix. With the defaults a refused call waits 150 to 190 s in all before it gives up, so a model that refuses
# every call trips the breaker after about 8 to 10 minutes.
BUSY_RETRIES = 4
BUSY_WAIT_S = 10.0
BUSY_WAIT_MAX_S = 80.0
BUSY_JITTER = 0.25
BUSY_TRIP_ITEMS = 3
PACE_S = 0.0
# A run the breaker stopped on 429 before 06:15 SAST waits RESUME_WAIT_S and explains what is left once more, with a new
# breaker, inside the model cap and the same deadline and time limit (W8-DEC-18, Albert, 9 Oct 2026). A second stop is
# final: there is no third attempt.
RESUME_WAIT_S = 600
UPSTREAM = {"kind": "data_issue",
            "text": "Data issue: the steps before the brief did not finish by 06:15, so no trends were checked today"}

# Keys that are ids, not names: a 64-hex item hash, or a YouTube channel id (uc plus 22 or more id characters).
_HASH_ID = re.compile(r"[0-9a-f]{64}", re.IGNORECASE)
_CHANNEL_ID = re.compile(r"uc[a-z0-9_-]{22,}", re.IGNORECASE)
# Platform ids, with or without a leading # or @: an all-digit post, sound or account id of 8 or more digits, a
# TikTok default handle (user plus 6 or more digits) and a Reddit fullname (t1_ to t6_ plus its base-36 id).
# A handle someone chose, such as virtual-mycologist13, is a name.
_PLATFORM_ID = re.compile(r"[#@]?(?:\d{8,}|user\d{6,}|t[1-6]_[a-z0-9]{4,})", re.IGNORECASE)
# A topic item's canonical key, "topic:" plus the cluster id it was born from (core/understand/cluster.py plan): an id,
# not a name. On 16 Oct 2026 "topic:20261007-za-037" and "topic:20261013-ng-092" took judged slots as if named.
_TOPIC_KEY = re.compile(r"topic:\d{8}-[a-z]{2}-\d{3,}", re.IGNORECASE)
FAILED_CHECKS = {"breach", "failed_checks", "too_few_claims", "check_incomplete"}
_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)
# The pattern bucket's format exclusions are written in from their one list (core/brief/pattern_formats.py).
QUERIES = {_NAME.search(s).group(1): s
           for s in sqlrun.split(pattern_formats.render(SQL.read_text(encoding="utf-8")))}
assert QUERIES["candidates"].count(f"NOT IN ({pattern_formats.sql_list()})") == 1

# The candidate statement reads the retained locality row through v_item_locality_current. If that read fails, the
# statement runs again with the view replaced by an empty one of the same columns: a row on the v1 basis then orders,
# scopes and gates exactly as before (it never reads the view), and a row on the v2 basis has no locality row, which
# is unreadable and held as a data issue (C4 v3 sections 8.4 and 10). That second read is for the shadow authority
# only (_candidate_rows). The failure is printed and recorded, never swallowed.
_LOCALITY_COLUMNS = (
    ("run_date", "DATE"), ("item_id", "STRING"), ("market", "STRING"), ("detect_run_id", "STRING"),
    ("metric_version", "STRING"), ("schema_version", "INT64"), ("population_posts", "INT64"),
    ("known_posts", "INT64"), ("local_posts", "INT64"), ("foreign_posts", "INT64"), ("unknown_posts", "INT64"),
    ("feed_only_posts", "INT64"), ("vetoed_feed_posts", "INT64"), ("breadth_creators", "INT64"),
    ("status", "STRING"), ("local_share", "FLOAT64"), ("population_digest", "STRING"),
    ("population_cutoff", "TIMESTAMP"), ("checked_status", "STRING"), ("checked_label", "STRING"))
_NO_LOCALITY = ("(SELECT " + ", ".join(f"CAST(NULL AS {kind}) {name}" for name, kind in _LOCALITY_COLUMNS)
                + " LIMIT 0) lo")
_LOCALITY_VIEW = "{core}.v_item_locality_current lo"
assert QUERIES["candidates"].count(_LOCALITY_VIEW) == 1
QUERIES["candidates_without_locality"] = QUERIES["candidates"].replace(_LOCALITY_VIEW, _NO_LOCALITY)

# a80's detect job re-creates v_item_market_scope on every run without market_news_posts7, so a brief on this code run while
# detect is still a80 (a mixed prefix of the jobs update or its restore) finds the column absent. The candidate statement
# then runs again with a typed NULL in its place, as core/api/store.py does for the same view: a NULL count is not below
# the market's posts, so the row ranks in the third tier for that run. The locality view is still read. This is its own
# reason (missing_view_column), never a locality failure, and under v2 it holds nothing. The statement serves only an error
# that says the column is missing; an error that merely names it (a resource or deadline fault) is a read fault like any other.
# If the null statement itself fails, that failure is caught and recorded as its own reason and the market is held with no
# candidates under either authority: the run goes on and the brief shows the data issue.
_NEWS_COLUMN = "ms.market_news_posts7"
_NULL_COLUMN = "CAST(NULL AS INT64)"
assert QUERIES["candidates"].count(_NEWS_COLUMN) == 4
QUERIES["candidates_without_news_column"] = QUERIES["candidates"].replace(_NEWS_COLUMN, _NULL_COLUMN)
QUERIES["candidates_without_locality_or_news_column"] = QUERIES["candidates_without_locality"].replace(_NEWS_COLUMN, _NULL_COLUMN)
MISSING_VIEW_COLUMN = "missing_view_column"
# Both faults at once, as live staging showed before detect re-created its views: no locality view and the old scope view.
BOTH_VIEW_FAULTS = "read_without_view+missing_view_column"
NULL_COLUMN_READ_FAILED = "null_column_statement_failed"
_MISSING_COLUMN_WORDS = ("Unrecognized name", "not found inside", "does not have a column named")
_NEWS_COLUMN_NAME = re.compile(r"(?<!\w)market_news_posts7(?!\w)")


def _names_news_column(error):
    """True when the error says market_news_posts7 is missing: it names the column and reads as one of the three missing
    column wordings (BigQuery's two and DuckDB's), not merely mentions the column."""
    text = str(error)
    return bool(_NEWS_COLUMN_NAME.search(text)) and any(words in text for words in _MISSING_COLUMN_WORDS)


def _null_column_rows(client, name, d, params, core, agent, receipt, failed, market):
    """Read the statement that carries a typed NULL for the missing column. If it fails, the failure is printed and recorded
    as NULL_COLUMN_READ_FAILED and the market has no candidates: held, whatever the locality authority."""
    try:
        return _query(client, name, params, core, agent, receipt=receipt)
    except Exception:
        receipt.clear()
        print(f"brief {d.isoformat()}: {NULL_COLUMN_READ_FAILED} market_news_posts7", file=sys.stderr)
        if failed is not None:
            failed[market] = NULL_COLUMN_READ_FAILED
        return []

# Which market the retained locality result gives a candidate on the v2 basis (C4 v3 section 11.2): the status of
# read_locality, never a v1 value. An unreadable result gives none, and the candidate is held as a data issue.
_V2_SCOPE = {"local": "market", "market_unconfirmed": "market", "not_local": "global"}

# Held reasons (TRUST.md section 2). claim_checks.reason and a card's failed_reason carry fixed wording per rule,
# never the check's detail: model text and the claim text inside code details can carry names, post text or an age
# reading that no word list catches. The critic's explanation is named only by the menu item its words match.
REASON_MAX = 300
HELD_MAX = 160
REPAIR = "before repair: "
RULE_NAMES = {"K1": "Quote check", "K2": "Number check", "K3": "Place check", "K4": "Support check",
              "K5": "Label check", "K6": "Banned term check", "K8": "Translation check", "K10": "Status check",
              "specificity": "Specificity check", "title": "Title check"}
CLAIM_WORDS = {
    "K1": "a quote or evidence id in a claim did not check out",
    "K2": "a claim had a number not pinned to its query",
    "K3": "a claim cited a post outside the window or market, or named a place no cited post is located in",
    "K4": "a claim was not supported by its posts",
    "K6": "a claim used a banned term or generated evidence",
    "K8": "a claim had an unmarked translation",
    "K9": "a claim stated what will happen, and forecasts are off",
}
SENTENCE_WORDS = {
    "K2": "the explanation had a number not pinned to its query",
    "K3": "the explanation named a place no cited post is located in",
    "K4": "the explanation sentence was not supported by its posts",
    "K6": "the explanation used a banned term",
    "K9": "the explanation stated what will happen, and forecasts are off",
    # core/brief/specificity.py: the sentence rests on 2 local posts and a quote copied exactly from one of them.
    "specificity": "the explanation did not rest on 2 local posts and a quote copied exactly from one of them",
}
# K3 names the part of the place check that failed, matched on core/trust/claims.py _k3 and place_fault details;
# the detail's post ids, times and place names are never copied. A detail matching none keeps the K3 wording above.
K3_PARTS = (
    (re.compile(r"outside the window$|has no readable posted_at$"), "cited a post outside the window"),
    (re.compile(r"is located in .+, not [A-Z]{2}$"), "cited a post located in another market"),
    (re.compile(r"but cites no record located there or from its feeds$"),
     "named a place no cited post is located in or comes from its feeds"),
    (re.compile(r"in feed wording but cites no record with source market [A-Z]{2}$"),
     "used a market's feed wording without a post from that market's feeds"),
    (re.compile(r"on source-market evidence without feed-scoped wording$|outside its feed-scoped phrase; source "
                r"evidence cannot support place or people claims$"),
     "described a place or people from posts only seen in that market's feeds"),
)
# A K6 cut for a pack sound letter (core/brief/explain.py _sound_label_rows) is a code label in reader text, not a
# banned term: matched on the code's own fixed detail phrase, never on copied text.
_CODE_LABEL = re.compile(r"^(?:short_answer: )?code label in reader text: ")
CODE_LABEL_NAME = "Code label check"
CODE_LABEL_WORDS = {"claim": "a claim named an internal sound label",
                    "sentence": "the explanation named an internal sound label"}
OTHER_WORDS = {"K5": "the label was lowered to what the evidence allows",
               "K10": "the status was lowered as claims were cut",
               # The writer's title (core/brief/explain.py): a cut only drops it, so the card keeps its label.
               "title": "the written title did not pass, so the card keeps its label"}
NOT_PASSED = "did not pass"
# explain.py's reject_overflow row: no support check ran, so the card does not say one failed.
OVERFLOW_WORDS = "the writer returned too many claims, so support was not checked"
TOO_FEW = "Claim checks: fewer than 2 claims passed"
# A primary fixture reaction (_publish_events): the explanation says creators reacted to a match or fixture, the
# critic read it as a scheduled event, and no cited post anchors the fixture and its date, so the card is held with
# its own wording and code instead of the generic "Not confirmed" (18 Oct 2026). The held_back.reason stays
# not_confirmed, the contract's enum; the code is a retained.REASON_CODES member the held item carries as
# held_reason_code, and core/api/held_words.py gives the wording its plain form.
FIXTURE_REACTION = "Not confirmed: a reaction to a match or fixture that no cited post dates or names"
FIXTURE_REACTION_CODE = "hold_fixture_reaction"
NO_REST = "Claim checks: the explanation did not rest on claims that passed"
CHECK_INCOMPLETE_WORDS = "Check did not complete"
# CRITIC_SYSTEM's menu of non-cultural explanations, in its order, each with the words that name it.
CRITIC_MENU = (
    ("a paid campaign", r"paid|sponsor\w*|campaign\w*|advert\w*|promot\w*|brand\s+push"),
    ("a platform feature change", r"platform\s+(?:features?|changes?|updates?)|features?\s+(?:changes?|updates?|launch)"
                                  r"|algorithm\w*"),
    ("a coordinated push", r"bots?|coordinated|inauthentic|astroturf\w*"),
    ("a news or scheduled event", r"news|scheduled|events?|fixtures?|holidays?|release"),
    ("a scraping or collection artefact", r"scrap\w*|collection|collected|collecting|collects|artefacts?|artifacts?|crawl\w*"),
    ("one viral post or creator", r"viral|(?:one|single)\s+(?:creator|post|account)"),
)
_MENU = tuple((words, re.compile(rf"\b(?:{pattern})\b", re.I)) for words, pattern in CRITIC_MENU)
_NEGATION = re.compile(r"\b(?:not|no|never|rather\s+than|instead\s+of|unlike|without)\b", re.I)
_CRITIC = re.compile(r"^critic: simplest non-cultural explanation: (.*?); (?:not )?ruled out: ", re.S)
_CRITIC_PARTS = re.compile(r"^critic: simplest non-cultural explanation: .*?; (not ruled out|ruled out|critic answer "
                           r"inconsistent): ", re.S)
# The CRITIC_MENU item the critic's own news_driven or scheduled_event flag names (explain._critic_row puts them on the
# row): set only when the simplest explanation is that event and no other applies (CRITIC_SYSTEM).
EVENT_MENU = "a news or scheduled event"
# A critic pass with no local why-now a post states, on code-counted local adoption (Albert, 22 Oct 2026).
WHY_NOW_INFERRED = "; why-now inferred from local adoption"
# A critic answer that contradicts itself (explain.INCONSISTENT) is a cut of its own, never worded as a simpler
# explanation left standing (18 Oct 2026).
CRITIC_INCONSISTENT = "Critic: the critic's answer was inconsistent"

def query_sql(name):
    """The text of a named statement for the locality authority in force (sqlrun.for_authority)."""
    return sqlrun.for_authority(QUERIES[name])


def _query(client, name, params, core, agent, receipt=None):
    if receipt is None:
        return sqlrun.query(client, query_sql(name), params, core=core, agent=agent)
    from google.cloud import bigquery

    sql = sqlrun.render(query_sql(name), core, agent)
    config = bigquery.QueryJobConfig(query_parameters=[sqlrun._param(k, v) for k, v in params.items()])
    query = client.query(sql, job_config=config)
    rows = [dict(row.items()) for row in query.result()]
    receipt.update(sql=sql, parameters={k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in params.items()},
                   job_id=getattr(query, "job_id", None),
                   result_sha256=hashlib.sha256(json.dumps(rows, sort_keys=True, default=str,
                                                          separators=(",", ":")).encode("utf-8")).hexdigest())
    return rows


def warmup_banner(client, d, core=CORE, agent=AGENT):
    first = _query(client, "first_collect", {"d": d}, core, agent)[0]["first_day"]
    if first is None:
        return None
    day = (d - first).days + 1
    return {"kind": "warming_up", "text": f"Warming up: day {day} of {WARMUP_DAYS}"} if day <= WARMUP_DAYS else None


def moments(client, d, market, core=CORE, agent=AGENT):
    return [{"date": r["moment_date"].isoformat(), "name": r["name"], "kind": r["kind"], "source": "calendar",
             "item_ids": list(r["item_ids"] or [])}
            for r in _query(client, "moments", {"d": d, "market": market}, core, agent)]


def _id_like(value):
    return bool(_HASH_ID.fullmatch(value) or _CHANNEL_ID.fullmatch(value) or _PLATFORM_ID.fullmatch(value)
                or _TOPIC_KEY.fullmatch(value))


def readable_title(label, key):
    """The label, else a canonical key that is a name rather than an id, else None."""
    for value in (label, key):
        if value and not _id_like(value) and _named_pattern(value):
            return value
    return None


def _raw_creator(row):
    """(platform, raw id) when a creator item's label is only the id its canonical key "{platform}:{handle}" was
    built from (core/detect/items.py), else None. The same rule as core/api/discover.py _raw_creator."""
    key = row.get("canonical_key")
    if (row.get("kind") or row.get("map_kind")) != "creator" or not isinstance(key, str) or ":" not in key:
        return None
    platform, raw = key.split(":", 1)
    label = str(row.get("label") or "").strip().lstrip("@").casefold()
    return (platform, raw) if not label or label in (raw, key.casefold()) else None


def creator_names(client, rows, hidden, core=CORE, agent=AGENT):
    """Set creator_name on each creator row whose label is only its id (_raw_creator): the creators row's display
    name, else its @handle when the handle is neither the id nor itself an id (the rule of core/api/discover.py
    _creator_names). One read for all the rows. A suppressed creator is never named: the read leaves out
    v_suppressed_creators, and hidden (what read_hidden returned) is checked again by id and creator_key. A failed
    read names nobody, so those rows keep the title readable_title gives them, or are held as nameless."""
    raw = [(r, _raw_creator(r)) for r in rows]
    keys = sorted({f"{p}:{i}".lower() for _, pair in raw if pair for p, i in [pair]})
    if not keys:
        return rows
    from google.cloud import bigquery

    param = bigquery.ArrayQueryParameter("keys", "STRUCT", [
        bigquery.StructQueryParameter(None, bigquery.ScalarQueryParameter("key", "STRING", k)) for k in keys])
    try:
        job = client.query(sqlrun.render(QUERIES["creator_names"], core, agent),
                           job_config=bigquery.QueryJobConfig(query_parameters=[param]))
        found = {str(c["key"]): c for c in (dict(row.items()) for row in job.result())}
    except Exception as e:  # noqa: BLE001 - names are optional; the brief must not fail on them
        print(f"brief: creator names read failed: {type(e).__name__}: {e}", file=sys.stderr)
        return rows
    hidden_keys, hidden_ids, _ = hidden
    for r, pair in raw:
        if not pair:
            continue
        platform, ident = pair
        c = found.get(f"{platform}:{ident}".lower())
        if not c or c.get("creator_id") in hidden_ids or creator_key(platform, c.get("handle") or ident) in hidden_keys:
            continue
        handle = str(c.get("handle") or "").strip().lstrip("@")
        name = str(c.get("display_name") or "").strip()
        if not name and handle and handle.casefold() != ident.casefold() and not _id_like(handle):
            name = "@" + handle
        if name:
            r["creator_name"] = name
    return rows


def card_title(row):
    """The title a candidate shows: readable_title, except that a creator whose label is only its id takes its
    creator_name (creator_names), and with none is unnamed when that id is not itself a readable handle."""
    title = readable_title(row.get("label"), row.get("canonical_key"))
    pair = _raw_creator(row)
    if pair is None:
        return title
    return row.get("creator_name") or (None if _id_like(pair[1]) else title)


def _unnamed_issue(n):
    return [f"{n} board {'entry' if n == 1 else 'entries'} without a name"] if n else []


def boards(client, d, market, core=CORE, agent=AGENT, hidden=None):
    """(boards, entries left out because they have no readable name). An entry is titled as a card is (card_title):
    a creator whose label is only the id its key was built from, such as a YouTube channel id, takes its creators
    name (creator_names, through hidden, what read_hidden returned) and with none is left out, so a board never
    shows "youtube:uc..." as a name. Without hidden no creator name is read and such an entry is left out."""
    out, unnamed = {}, 0
    rows = _query(client, "boards", {"d": d, "market": market}, core, agent)
    if hidden is not None:
        creator_names(client, rows, hidden, core, agent)
    for r in rows:
        title = card_title(r)
        if title is None:
            unnamed += 1
            continue
        board = out.setdefault((r["platform"], r["series"]), {
            "platform": r["platform"], "list": BOARD_WORDS.get(r["series"], r["series"].replace("_", " ")),
            "entries": []})
        entries = board["entries"]
        if len(entries) < BOARD_ENTRIES:
            rank = int(r["best_rank"]) if r["best_rank"] is not None else None
            entries.append({"rank": rank, "title": title, "item_id": r["item_id"]})
    return list(out.values()), unnamed


def spent_today(client, d, core=CORE, agent=AGENT):
    return float(_query(client, "spent_today", {"d": d}, core, agent)[0]["usd"] or 0)


def _window(d, market):
    tz = timezone(timedelta(hours=OFFSETS[market]))
    return (datetime.combine(d - timedelta(days=WINDOW_DAYS - 1), time(), tz),
            datetime.combine(d, time(23, 59, 59), tz))


def post_set(client, d, market, item_id, core=CORE, agent=AGENT):
    """The ids of every post the item's card could rest on over the pack's window (brief.sql post_set)."""
    start = _window(d, market)[0]
    params = {"item_id": item_id, "market": market, "d": d, "start": start,
              "end": start + timedelta(days=WINDOW_DAYS)}
    return {r["post_id"] for r in _query(client, "post_set", params, core, agent)}


def _coordinated():
    return Decision(publish=False, where="held_back", flag="Likely coordinated",
                    reason="Likely coordinated: detection found a coordinated posting pattern", rule="G4",
                    numbers_only=False)


def _held(reason_text, rule=None):
    return Decision(publish=False, where="held_back", flag=None, reason=reason_text, rule=rule, numbers_only=False)


def _floor_held_decision(cand, floor, text, evidence):
    """A hold on a post floor, with the stage at which the pack fell below it (pack_order.hold_detail) kept in
    held_reason_detail, and the cap that did it named in the text. The reason code stays not_confirmed.

    When the suppression mask took posts out of the pack after the query, the stage counts include posts a reader
    must not learn of, and a cap named in the text would place a loss the mask caused. The wording stays plain, the
    detail served is counted over the posts a reader can see (no stage counts, so the same as a hold that lost nothing
    to the mask and had that many posts), and the counts as the query left them are kept in held_reason_audit, which
    the payload stores apart from cards and held items (payload.py hold_audit) and no reader shows."""
    detail = pack_order.hold_detail(cand.get("stages"), floor, evidence, cand["market"])
    if pack_order.masked_after_ranking(cand.get("stages"), floor, evidence, cand["market"]):
        cand["held_reason_audit"] = detail
        cand["held_reason_detail"] = pack_order.hold_detail(None, floor, evidence, cand["market"])
        return _held(text)
    cand["held_reason_detail"] = detail
    return _held(pack_order.hold_text(text, detail))


def _gate(cand, passed):
    """gate_card plus the brief's own holds, each with its contract reason code in cand["held_reason"]: a
    platform-generic tag is not a trend (G2), and a card needs at least 3 posts 42 can show. Invalid data days
    (G1) are checked before the brief's own holds, so the data-issue count is never undercounted. A market scope
    that could not be read is held as unreadable evidence, not as global."""
    cand["held_reason"], cand["floor_held"] = None, False
    cand["held_reason_detail"], cand["held_reason_audit"] = None, None
    if cand.get("error"):
        cand["held_reason"] = "data_issue"
        return _held("Evidence could not be read")
    ctx = cand["ctx"]
    # G5 reads the larger of item_state's sponsored share and the evidence share of paid-post markers.
    card = {**cand["row"], "sponsored_share": max(cand["row"].get("sponsored_share") or 0,
                                                  ctx.get("sponsored_share") or 0)}
    decision = gate_card(card, {**ctx, "explanation_passed": passed})
    # G1 is an invalid day. The gate also answers G1 for a row whose locality row cannot be read; that one is held
    # below with the scope read failures, so that it takes a judged slot and no replacement is prepared for it.
    if decision.rule == "G1" and (not cand.get("scope_error") or any(ok is False for ok in ctx["valid_days"])):
        return decision
    if cand.get("scope_error"):
        cand["held_reason"] = "data_issue"
        return _held("Evidence could not be read")
    if cand["row"].get("map_status") == "generic":
        cand["held_reason"] = "not_confirmed"
        return _held("Platform-generic tag", "G2")
    if cand["row"].get("nameless"):
        cand["held_reason"] = "not_confirmed"
        return _held("No readable name")
    if ctx.get("paid_key"):
        return _held(f"Paid-led: #{ctx['paid_key']} marks paid posts", "G5b")
    if cand["row"].get("authenticity") == "likely_coordinated":
        return _coordinated()
    evidence = cand["pack"]["evidence"]
    local = counted_local_posts(evidence, cand["market"])
    showable = showable_posts(evidence, cand["market"])
    if decision.where == "today" and len(showable) < MIN_EVIDENCE:
        cand["held_reason"], cand["floor_held"] = "not_confirmed", True
        return _floor_held_decision(cand, "showable", "Fewer than 3 posts 42 can show", evidence)
    if decision.where == "today" and len(local) < 2:
        cand["held_reason"], cand["floor_held"] = "not_confirmed", True
        return _floor_held_decision(cand, "local", "Fewer than 2 supported local posts", evidence)
    # The floors above read the 7 day pack; the card's own counts are the last 3 days. A card whose counted days hold
    # no creator or no post would show "0 creators and 0 posts in 3 days" over examples older than those days. Only a
    # measured zero holds: a count detect did not give is not a zero. Not a floor hold: confirm finds posts, never
    # detect's counts, so a search cannot change the answer.
    if decision.where == "today" and _measured_zero(cand):
        cand["held_reason"] = "too_few_creators"
        return _held(NO_COUNTED_DAYS)
    return decision


def _measured_zero(cand):
    """True when the pack pinned a 0 for the creators or the posts of the card's counted 3 days."""
    return 0 in (_pinned(cand, "creators in 3 days"), _pinned(cand, "posts in 3 days"))


def _pinned(cand, unit):
    """The pack's pinned number with this unit, None when the pack has none."""
    return next((n.get("value") for n in cand["pack"].get("numbers") or [] if n.get("unit") == unit), None)


def _prepare(client, d, market, row, *, build_ctx, campaign_hashtags, political_terms, core, agent, hidden=None,
             calendar=()):
    """One candidate: its evidence pack and gate context. A pack or market scope that fails leaves the candidate to
    be held back with its reason (scope_error for the scope); a gate context that fails fails the run. A label or key that names a suppressed person is
    masked before anything reads it. hidden: what read_hidden returned, read here when not given. calendar: the
    market's moments (moments()), which the pack's day lines name on their dates."""
    hidden = read_hidden(client, core=core, agent=agent) if hidden is None else hidden
    row = without_hidden(row, hidden)
    row["kind"] = row.get("kind") or row.get("map_kind")
    title = card_title(row)
    row["title"], row["nameless"] = without_hidden(title or f"Unnamed {row['kind']}", hidden), title is None
    row["seen_platforms"] = list(row.get("seen_platforms") or [])
    scope = {"market_scope": "global", "market_posts7": None, "total_posts7": None, "market_share7": None}
    scope_error = False
    try:
        basis = read_market_scope(client, row, d, market, core=core, agent=agent)
        if isinstance(basis, dict):
            scope.update(basis)
    except Exception:
        scope_error = True
        print(f"brief {d.isoformat()}: market_scope_read_failed", file=sys.stderr)
    if scope.get("market_scope") != "market":
        scope["market_scope"] = "global"
    # The retained locality row the candidate statement joined, with the lrow_ prefix off; None when no verified row
    # exists. A row whose item_state says it was admitted under locality_v2.1 is read by that rule whatever this
    # job's own constant is (C4 v3 section 7.4): its scope comes from the checked status and the pack scope is kept
    # only as the observation pack_scope_v1. Any other row is read by the pack scope, as before, and the locality
    # row is stored beside it as an observation (section 10).
    record = row_from_prefixed(row) if row.get("lrow_metric_version") is not None else None
    if row.get("locality_basis") == V2_BASIS:
        derived = _V2_SCOPE.get(read_locality(record).status)
        row["pack_scope_v1"] = {k: scope.get(k) for k in ("market_scope", "market_posts7", "total_posts7",
                                                          "market_share7")}
        row.update({k: scope.get(k) for k in ("market_posts7", "total_posts7", "market_share7")})
        row["market_scope"], row["market_scope_basis"] = derived, V2_BASIS
        # A pack read that failed is an observation lost. A row that cannot be read has no scope and is held as
        # unreadable evidence, never as global (C4 v3 section 11.2): scope_error says so to _gate and the banner.
        scope_error = derived is None
    else:
        row.update(scope)
        if scope_basis(row.get("locality_basis")):
            row["market_scope_basis"] = scope_basis(row.get("locality_basis"))
    if record is not None or row.get("locality_basis") == V2_BASIS:
        row["locality_v2"] = locality_block(record)
    cand = {"row": row, "market": market, "sparkline": None, "rerun": None, "ctx": {}, "stages": {},
            "pack": {"evidence": [], "numbers": [], "facts": []}, "scope_error": scope_error}
    try:
        cand["pack"], cand["sparkline"], cand["rerun"] = build_pack(client, row, d, market, core=core, agent=agent,
                                                                    hidden=hidden, moments=calendar,
                                                                    stages=cand["stages"])
        cand["posts"] = (post_set(client, d, market, row["item_id"], core, agent)
                         | {e["id"] for e in cand["pack"]["evidence"]})
    except Exception as e:
        cand["error"] = f"{type(e).__name__}: {e}"
        return cand
    cand["ctx"] = build_ctx(client, row, d, market, cand["pack"]["evidence"], campaign_hashtags=campaign_hashtags,
                            political_terms=political_terms,
                            numbers=[n for n in cand["pack"]["numbers"] if "rival_field" not in n], core=core,
                            agent=agent)
    return cand


def _generic_label(row):
    """True for a topic whose label is made only of platform-generic tag words (core/detect/stoplist.yaml), such as
    "tiktok, viralvideo, viral" or "mombasatiktokers, kenyantiktok": the stoplist says those words name no topic."""
    from core.detect.items import is_generic

    words = [w for part in str(row.get("label") or "").split(",") for w in part.split()]
    return ((row.get("kind") or row.get("map_kind")) == "topic" and bool(words)
            and all(is_generic("hashtag", w) for w in words))


def _ranked(rows):
    """At most POOL rows in the query's order, rows without a readable title (card_title) after every named row,
    so a platform id never takes a slot from a named item, and a topic named only by platform-generic tag words
    (_generic_label) after the other named rows. Unnamed rows still fill slots named rows leave empty, and are held
    there as having no readable name."""
    named = [r for r in rows if card_title(r) is not None and not _generic_label(r)]
    generic = [r for r in rows if card_title(r) is not None and _generic_label(r)]
    return (named + generic + [r for r in rows if card_title(r) is None])[:POOL]


def _held_g1(cand):
    """Held by G1: an invalid baseline feed day on the item's main platform in d to d-2."""
    return cand["decision"].rule == "G1"


def _selection_snapshot(rows, receipt):
    if not rows or not receipt.get("job_id"):
        return None
    try:
        snapshot = json.loads(rows[0].get("_selection_snapshot") or "null")
        if not isinstance(snapshot, dict) or any(r.get("_selection_snapshot") for r in rows[1:]):
            return None
        total, count = snapshot["total_count"], snapshot["eligible_count"]
        items = snapshot.get("items") or []
        detect = snapshot["detect_run_id"]
        if (type(total) is not int or type(count) is not int or not 0 <= count <= total
                or len(rows) != min(POOL, total) or snapshot["detect_run_count"] != 1
                or not isinstance(detect, str) or not detect.strip() or len(items) != count
                or any(r.get("run_id") != detect or r.get("_selection_sql_rank") != n
                       for n, r in enumerate(rows, 1))):
            return None
        by_id, ranks = {}, []
        for item in items:
            rank, item_id = item["sql_rank"], item["item_id"]
            if (not isinstance(item_id, str) or not item_id.strip() or item_id in by_id
                    or any(item.get(key) is not None and not isinstance(item.get(key), str)
                           for key in ("label", "canonical_key", "map_kind"))
                    or type(rank) is not int or not 1 <= rank <= total
                    or item.get("market_scope") not in ("market", "global", None)):
                return None
            by_id[item_id] = item
            ranks.append(rank)
        if ranks != sorted(set(ranks)):
            return None
        for row in rows:
            item = by_id.get(row["item_id"])
            if row.get("eligible") is not True:
                if item is not None:
                    return None
                continue
            if item is None or any(item.get(key) != row.get(source) for key, source in (
                    ("sql_rank", "_selection_sql_rank"), ("market_scope", "_selection_market_scope"),
                    ("label", "label"), ("canonical_key", "canonical_key"), ("map_kind", "map_kind"))):
                return None
        return snapshot
    except (KeyError, TypeError, ValueError):
        return None


def _selection_result(snapshot, receipt, ranked, prepared, taken, cutoff, observed):
    judged = sum(not _held_g1(c) for c in prepared) - len(_fold_plan(prepared)[1])
    exit_reason = ("judged_limit_reached" if judged >= CANDIDATES else
                   "pool_exhausted" if taken >= len(ranked) else
                   "deadline_reached" if cutoff else "pool_exhausted")
    receipt = dict(receipt, preparation={"exit_reason": exit_reason,
        "cutoff_basis": cutoff if exit_reason == "deadline_reached" else None,
        "observed_at": observed.isoformat() if observed is not None else None, "taken": taken, "judged": judged})
    if snapshot is None:
        return {"not_assessed": None, "selection_receipt": receipt if receipt.get("job_id") else None}
    prepared_ids = {c["row"]["item_id"] for c in prepared}
    pool = {row["item_id"]: (n, row) for n, row in enumerate(ranked, 1)}
    basis = scope_basis(ranked[0].get("locality_basis") if ranked else None)   # one run, one rule
    items = []
    for item in snapshot.get("items") or []:
        item_id = item["item_id"]
        if item_id in prepared_ids:
            continue
        in_pool = pool.get(item_id)
        reason = "outside_candidate_pool" if item["sql_rank"] > POOL else exit_reason
        if reason not in NOT_ASSESSED_REASONS or (item["sql_rank"] <= POOL and in_pool is None):
            return {"not_assessed": None, "selection_receipt": receipt}
        row = in_pool[1] if in_pool else dict(item, kind=item.get("map_kind"))
        title = card_title(row) or f"Unnamed {item.get('map_kind') or 'topic'}"
        items.append({"item_id": item_id, "title": title, "status": "not_assessed", "reason": reason,
            "reason_text": NOT_ASSESSED_REASONS[reason], "sql_rank": item["sql_rank"],
            "pool_rank": in_pool[0] if in_pool else None, "market_scope": item.get("market_scope"),
            **({"market_scope_basis": basis} if basis else {})})
    return {"not_assessed": {"detect_run_id": snapshot["detect_run_id"], "pool_limit": POOL,
        "judged_limit": CANDIDATES, "count": len(items), "items": items}, "selection_receipt": receipt}


def _candidate_rows(client, d, market, core, agent, receipt, failed=None):
    """The market's candidate rows. If the read fails the failure is printed and recorded in failed, {market: action}.
    In shadow the statement runs once more without the locality view (see QUERIES), so a missing or failing view never
    changes a row on the v1 basis. Under v2 it does not: every row on the v2 basis would then have no locality row and
    be unreadable, which turns one transient error into a whole market held for evidence it never failed to read. The
    market is held instead, with no candidates, and the brief shows the data issue."""
    params = {"d": d, "market": market}
    try:
        return _query(client, "candidates", params, core, agent, receipt=receipt)
    except Exception as error:
        receipt.clear()
        if _names_news_column(error):
            print(f"brief {d.isoformat()}: {MISSING_VIEW_COLUMN} market_news_posts7", file=sys.stderr)
            if failed is not None:
                failed[market] = MISSING_VIEW_COLUMN
            return _null_column_rows(client, "candidates_without_news_column", d, params, core, agent, receipt, failed, market)
        print(f"brief {d.isoformat()}: locality_view_read_failed", file=sys.stderr)
        held = locality.LOCALITY_AUTHORITY == "v2"
        if failed is not None:
            failed[market] = "held" if held else "read_without_view"
        if held:
            return []
        try:
            return _query(client, "candidates_without_locality", params, core, agent, receipt=receipt)
        except Exception as second:
            if not _names_news_column(second):
                raise
            receipt.clear()
            print(f"brief {d.isoformat()}: {MISSING_VIEW_COLUMN} market_news_posts7", file=sys.stderr)
            if failed is not None:
                failed[market] = BOTH_VIEW_FAULTS
            return _null_column_rows(client, "candidates_without_locality_or_news_column", d, params, core, agent, receipt, failed,
                                     market)


def _locality_audit_inputs(client, d, market, rows, core, agent):
    """What the locality_audit block is built from (C4 v3 section 11.2): the not_local rows of the run, up to 50 with
    their total, and the count of eligible rows whose locality row is missing or unreadable. Read only when the
    pool holds a row admitted under locality_v2.1 (every row of a run is written under one rule, so the pool tells which
    rule the run used); None when there is nothing to audit or a read failed (the block is then left out, and the pool
    and the holds are unchanged)."""
    if not any(r.get("locality_basis") == V2_BASIS for r in rows):
        return None
    run_id = rows[0].get("run_id")
    try:
        return {"detect_run_id": run_id,
                "not_local": _query(client, "not_local_audit", {"d": d, "market": market}, core, agent),
                "unreadable_total": _query(client, "unreadable_audit", {"d": d, "market": market, "run_id": run_id},
                                           core, agent)[0]["unreadable_total"]}
    except Exception:
        print(f"brief {d.isoformat()}: locality_audit_read_failed", file=sys.stderr)
        return None


def _candidates(client, d, *, build_ctx, campaign_hashtags, political_terms, core, agent, hidden=None, chain=None,
                clock=None, started=None, calendar=None, selection_audits=None):
    """{market: candidates in rank order}, packs and contexts built PACK_WORKERS at a time across the markets.

    Each market judges up to CANDIDATES candidates that G1 does not hold and that do not fold into a higher card
    (_fold_plan); a folded candidate stays in the list and _merge absorbs it. A candidate G1 holds stays in the list,
    and so in held_back with its G1 reason, but does not take one of those slots: the next ranked rows are prepared
    and gated in rounds until CANDIDATES candidates not held by G1 have been judged or the market's POOL rows run
    out, so at most POOL candidates are prepared per market. G1 reads only the gate context, so it is decided here,
    before any confirm search or model call; G1 candidates are never confirmed or explained (_for_today).
    hidden: what read_hidden returned, read here when not given. With chain and clock, no round after the first
    starts past the deadline (_brief_deadline) or the time limit (_out_of_time from started); a market then judges
    fewer than CANDIDATES, and its G1 holds stay in held_back. Creator rows are named first (creator_names), in one
    read. calendar: {market: moments()} for the packs' day lines, none when not given."""
    calendar = calendar or {}
    hidden = read_hidden(client, core=core, agent=agent) if hidden is None else hidden
    pools, snapshots, receipts, unread = {}, {}, {}, {}
    for m in MARKETS:
        receipts[m] = {}
        rows = _candidate_rows(client, d, m, core, agent, receipts[m], unread)
        snapshots[m] = _selection_snapshot(rows, receipts[m])
        pools[m] = [{k: v for k, v in row.items() if not k.startswith("_selection_")} for row in rows]
    creator_names(client, [r for m in MARKETS for r in pools[m]], hidden, core, agent)
    ranked = {m: _ranked(pools[m]) for m in MARKETS}
    by_market = {m: [] for m in MARKETS}
    taken = dict.fromkeys(MARKETS, 0)
    cutoff, observed, rounds = None, None, 0
    with ThreadPoolExecutor(max_workers=PACK_WORKERS) as pool:
        first = True
        while True:
            if not first and clock is not None and chain is not None:
                now = clock()
                observed = now
                past_deadline = _brief_deadline(now, d, chain)[0]
                if past_deadline or _out_of_time(now, started):
                    cutoff = "deadline" if past_deadline else "task_timeout"
                    break
            first = False
            rounds += 1
            rows = []
            for m in MARKETS:
                # A candidate that folds into a higher card (_fold_plan) takes no judged slot either, so one story
                # told by several items leaves room for the next ranked rows.
                need = (CANDIDATES - sum(1 for c in by_market[m] if not _held_g1(c))
                        + len(_fold_plan(by_market[m])[1]))
                batch = ranked[m][taken[m]:taken[m] + max(need, 0)]
                taken[m] += len(batch)
                rows += [(m, row) for row in batch]
            if not rows:
                break
            cands = list(pool.map(lambda mr: _prepare(client, d, mr[0], mr[1], build_ctx=build_ctx,
                                                      campaign_hashtags=campaign_hashtags,
                                                      political_terms=political_terms[mr[0]], hidden=hidden,
                                                      calendar=calendar.get(mr[0], ()), core=core, agent=agent),
                                  rows))
            for cand, (m, row) in zip(cands, rows):
                cand["decision"] = _gate(cand, None)
                cand["_round"] = rounds
                # The pool position (pattern-first SQL order): within a round a fold keeps the higher-ranked row.
                cand["_rank"] = next(n for n, r in enumerate(ranked[m]) if r is row)
                by_market[cand["market"]].append(cand)
    if selection_audits is not None:
        for m in MARKETS:
            selection_audits[m] = _selection_result(snapshots[m], receipts[m], ranked[m], by_market[m],
                                                    taken[m], cutoff, observed)
            selection_audits[m]["locality_audit_inputs"] = _locality_audit_inputs(client, d, m, pools[m], core, agent)
            selection_audits[m]["candidate_read"] = unread.get(m)
    return by_market


def _absorb(into, cand):
    """Merge cand into the kept card: its title goes on the card's also list, and its pack posts join the card's
    pack while the pack has room, at most PER_CREATOR per handle. The card's numbers stay its own item's."""
    into["also"].append({"item_id": cand["row"]["item_id"], "title": cand["row"]["title"]})
    into["posts"].update(cand["posts"])
    evidence = into["pack"]["evidence"]
    have = {e["id"] for e in evidence}
    per = Counter(e.get("handle") for e in evidence)
    for e in cand["pack"]["evidence"]:
        if len(evidence) >= PACK_MAX:
            break
        if e["id"] in have or (e.get("handle") and per[e["handle"]] >= PER_CREATOR):
            continue
        evidence.append(e)
        have.add(e["id"])
        per[e.get("handle")] += 1


def _fold_plan(cands):
    """(kept, folds) for one market's candidates, without changing them. Round by round as _candidates prepared them
    (_round), in worth order within a round, a Today-bound candidate folds into the kept card it shares most posts
    with (the higher one on a tie) when it has fewer than MIN_EVIDENCE posts not already on a higher kept card, or
    when half or more of its posts are already on that one card: the same story told by another item, such as a
    match's topic and its club's hashtag (16 Oct 2026, NG: 8 of 10 judged slots on one round of football). A kept
    card's posts grow by what folds into it. folds is [(cand, into)]. Rounds first because each fold frees a judged
    slot for the next round, and rank order is pattern-first, so a later row can be worth more than an earlier one:
    in worth order alone it could take posts from an earlier fold and undo it, and the market would judge more than
    CANDIDATES. Rounds first, no decision rests on a later round's rows."""
    kept, kept_posts, claimed, folds = [], [], set(), []
    for cand in sorted((c for c in cands if _for_today(c)),
                       key=lambda c: (c.get("_round", 0), c.get("_rank", 0), _worth(c["row"]))):
        posts = cand.get("posts") or set()
        best = max(((len(posts & kp), -i) for i, kp in enumerate(kept_posts)), default=(0, 0))
        if best[0] > 0 and (len(posts - claimed) < MIN_EVIDENCE or best[0] * 2 >= len(posts)):
            folds.append((cand, kept[-best[1]]))
            kept_posts[-best[1]] = kept_posts[-best[1]] | posts
        else:
            kept.append(cand)
            kept_posts.append(set(posts))
        claimed |= posts
    return kept, folds


def _merge(by_market):
    """One card per set of posts. Per market, the Today-bound candidates round by round, in worth order within a
    round (_fold_plan): one that folds is not a card of its own and merges into its kept card. Held candidates keep
    their hold and claim no posts.
    Merged candidates leave by_market, so they are neither confirmed, explained, shown nor held. Returns
    [{market, into, item_id}] in rank order."""
    merged = []
    for m in MARKETS:
        kept, folds = _fold_plan(by_market[m])
        for cand in kept:
            cand["also"] = []
        gone = set()
        for cand, into in folds:
            _absorb(into, cand)
            merged.append({"market": m, "into": into["row"]["item_id"], "item_id": cand["row"]["item_id"]})
            gone.add(id(cand))
        by_market[m] = [c for c in by_market[m] if id(c) not in gone]
    return merged


class ModelUnavailable(Exception):
    """Raised instead of calling the model once the breaker has tripped."""


def _rate_limited(e):
    """An error class named RateLimitError (matched by class name through the MRO, so no SDK need be imported), a 429
    status_code or status, or Vertex's RESOURCE_EXHAUSTED in the text. A 429 elsewhere in the text, such as
    inside a request id, does not count."""
    if any(k.__name__ == "RateLimitError" for k in type(e).__mro__):
        return True
    if any(str(getattr(e, name, None)) == "429" for name in ("status_code", "status", "code")):
        return True
    return "RESOURCE_EXHAUSTED" in str(e)


class _Breaker:
    """Wraps the model for one run. Until one call has gone through, calls are made one at a time. A quota or
    rate-limit error (see _rate_limited) that billed nothing is waited out: the same call is made again after
    wait(attempt) seconds, at most retries times, while can_wait(seconds) says the wait ends in time. A call that runs
    out of tries raises the error; trip_after such calls in a row, with no call going through between them, or a wait
    that would not end in time, trip the breaker, and every later call raises ModelUnavailable without reaching the
    model, so a sustained outage costs a bounded number of calls, not a retry storm per card. A refusal books
    nothing: only the call that goes through returns usage. busy is "refused" or "deadline" once the breaker has
    tripped on refusals, else None. With the defaults the first refusal trips it, as before any waiting."""

    def __init__(self, model, *, retries=0, wait=None, can_wait=None, sleep=None, trip_after=1, call_allowance=None):
        self.model = model
        self.tripped = None
        self.busy = None
        self.retries = retries
        self.wait = wait or (lambda attempt: 0.0)
        self.can_wait = can_wait or (lambda seconds: True)
        self.sleep = sleep or _sleep
        self.trip_after = trip_after
        self.call_allowance = call_allowance
        self.uncertain_calls = []
        self.refusals = self.gave_up = self.streak = 0
        self.waited_s = 0.0
        self._proven = False
        self._first = threading.Lock()

    def complete_json(self, **kw):
        if not self._proven:
            with self._first:
                if not self._proven:
                    return self._call(kw, prove=True)
        return self._call(kw)

    def _call(self, kw, prove=False):
        attempt = 0
        while True:
            if self.tripped:
                raise ModelUnavailable(self.tripped)
            call_kw = kw
            if self.call_allowance is not None:
                allowance = self.call_allowance()
                if allowance < 0.001:
                    raise ModelUnavailable("brief model call deadline exhausted")
                if isinstance(self.model, GeminiModel):
                    timeout_s = min(GEMINI_TIMEOUT_S, self.model.timeout_s or GEMINI_TIMEOUT_S,
                                    kw.get("request_timeout_s") or GEMINI_TIMEOUT_S, allowance)
                    call_kw = {**kw, "request_timeout_s": timeout_s, "request_allowance": self.call_allowance}
            try:
                out = self.model.complete_json(**call_kw)
                break
            except Exception as e:
                if getattr(e, "reserve_model_estimate", False):
                    self.uncertain_calls.append(e)
                if getattr(e, "auth_unresolved", False):
                    self.tripped = "deadline owner retired after unresolved authentication"
                if getattr(e, "request_cleanup_failed", False):
                    self.tripped = "deadline owner retired after request cleanup failure"
                if not _rate_limited(e):
                    raise
                self.refusals += 1
                # A refusal that reports spend is not made again, so that spend is booked once, by the caller.
                again = attempt < self.retries and not getattr(e, "usd", 0.0)
                seconds = self.wait(attempt) if again else 0.0
                if again and self.can_wait(seconds):
                    self.sleep(seconds)
                    self.waited_s += seconds
                    attempt += 1
                    continue
                self.gave_up += 1
                self.streak += 1
                if again:
                    self.tripped, self.busy = f"{type(e).__name__}: {e}", "deadline"
                elif self.streak >= self.trip_after:
                    self.tripped, self.busy = f"{type(e).__name__}: {e}", "refused"
                raise
        self.streak = 0
        self._proven = self._proven or prove
        return out


def _setting(name, default, least):
    """BRIEF_<name> from the environment as a number of default's type, else default when it is unset, not a finite
    number or below least."""
    value = os.environ.get(f"BRIEF_{name}")
    if value is None:
        return default
    try:
        number = type(default)(value.strip())
    except ValueError:
        return default
    return number if math.isfinite(number) and number >= least else default


def _busy_wait(base, cap, jitter, rand=random.random):
    """Seconds to wait before try attempt + 1 of a refused call: base doubling per try up to cap, then up to jitter
    longer."""
    return lambda attempt: min(cap, base * 2 ** attempt) * (1 + jitter * rand())


def _menu(detail):
    """The CRITIC_MENU wording the critic's explanation matches first, or None. Only the menu wording is returned."""
    m = _CRITIC.match(str(detail or ""))
    if not m:
        return None
    text = m.group(1)
    hits = [words for words, rx in _MENU if rx.search(text)]
    # Only one unambiguous menu item is named; a contrast or negation ("not a paid campaign") could flip it.
    if len(hits) != 1 or _NEGATION.search(text):
        return None
    return hits[0]


def _critic_parts(chk):
    """(standing, local_why_now) of a critic row. explain._critic_row puts both on the row as structured keys, and
    they are read from there: the detail embeds the model's own explanation text, so a standing parsed out of it
    could be chosen by that text. A row without the keys (only a hand-built one) falls back to its detail, which
    can name only the standings or None when unreadable."""
    standing, local = chk.get("standing"), chk.get("local_why_now")
    if standing in STANDINGS and isinstance(local, bool):
        return standing, local
    detail = str(chk.get("detail") or "")
    m = _CRITIC_PARTS.match(detail)
    return (m.group(1) if m else None), detail.endswith("local why-now checked")


def _overflow(chk):
    detail = str(chk.get("detail") or "")
    return detail.startswith("writer returned") and detail.endswith("support checks were withheld")


def _code_label(chk):
    """True for explain.py's K6 cut of a pack sound letter, before or after the repair round."""
    detail = str(chk.get("detail") or "")
    detail = detail[len(REPAIR):] if detail.startswith(REPAIR) else detail
    return chk.get("rule") == "K6" and _CODE_LABEL.match(detail) is not None


def _critic_menu(chk):
    """The CRITIC_MENU item a critic row names. A row that carries the critic's own flags (explain._critic_row) names a
    news or scheduled event from them: when either is true it is that event, whatever the free text matches (22 Oct
    2026, KE stand-up comedy: scheduled_event true, its text matched release and promot, so no rival was named), and
    when both are false the free text cannot name it. Any other menu item still comes from the text (_menu), since the
    critic has no flag for it. A hand-built row without the flags reads its text alone."""
    news, event = chk.get("news_driven"), chk.get("scheduled_event")
    flagged = isinstance(news, bool) and isinstance(event, bool)
    if flagged and (news or event):
        return EVENT_MENU
    menu = _menu(chk.get("detail"))
    return None if flagged and menu == EVENT_MENU else menu


def _wording(chk, rested=False):
    """The fixed wording for one check row. rested: the row cuts a claim the explanation sentence rests on."""
    rule, verdict = chk["rule"], chk["verdict"]
    if chk.get("detail") == CHECK_INCOMPLETE:
        return f"{'Critic' if rule == 'critic' else RULE_NAMES.get(rule, 'Claim checks')}: {CHECK_INCOMPLETE}"
    if rule == "critic":
        menu = _critic_menu(chk)
        standing, local = _critic_parts(chk)
        if standing == INCONSISTENT:
            return CRITIC_INCONSISTENT
        if verdict == "pass":
            head = "Critic: the simpler explanation was ruled out"
            head = f"{head}: {menu}" if menu else head
            # Albert, 22 Oct 2026: no post states the local cause; code counted the adoption the why-now rests on.
            return f"{head}{WHY_NOW_INFERRED}" if chk.get("why_now_inferred") is True else head
        # A cut names the part that failed: the simpler explanation, the local why-now, or both.
        ruled_out = standing == "ruled out"
        if ruled_out and not local:
            return "Critic: local why-now not shown"
        head = "Critic: a simpler explanation was not ruled out"
        head = f"{head}: {menu}" if menu else head
        return head if local or ruled_out else f"{head}; local why-now not shown"
    name = RULE_NAMES.get(rule, "Claim checks")
    if verdict == "pass":
        return f"{name}: passed"
    if rule in OTHER_WORDS:
        return f"{name}: {OTHER_WORDS[rule]}"
    if _overflow(chk):
        return f"{name}: {OVERFLOW_WORDS}"
    words = (SENTENCE_WORDS.get(rule) if chk.get("claim_id") is None else None) or CLAIM_WORDS.get(rule, NOT_PASSED)
    if _code_label(chk):
        name, words = CODE_LABEL_NAME, CODE_LABEL_WORDS["sentence" if chk.get("claim_id") is None else "claim"]
    if rule == "K3":
        part = next((w for rx, w in K3_PARTS if rx.search(str(chk.get("detail") or "").strip())), None)
        if part is not None:
            words = f"{'the explanation' if chk.get('claim_id') is None else 'a claim'} {part}"
    if rested:
        words = words.replace("a claim", "a claim the explanation rests on", 1)
    return f"{name}: {words}"


def check_reason(chk):
    """claim_checks.reason for one check row: its rule's fixed wording, marked when the row is from before the
    repair round."""
    prefix = REPAIR if str(chk.get("detail") or "").startswith(REPAIR) else ""
    return (prefix + _wording(chk))[:REASON_MAX]


# core/trust/retained.py codes for the K3 part that failed, in K3_PARTS order.
K3_CODES = ("sentence_place_outside_window", "sentence_place_other_market", "sentence_place_unlocated",
            "sentence_place_feed_wording", "sentence_place_source_only")
SENTENCE_CODES = {"K1": "sentence_quote", "K2": "sentence_number_unpinned", "K6": "sentence_banned_term",
                  "K8": "sentence_translation", "K9": "sentence_future_assertion",
                  "specificity": "sentence_specificity"}
_SUPPORT_VERDICT = re.compile(r"^(?:explanation sentence )?support check (supported|partial|unsupported):")


def _reason_code(chk):
    """The retained.REASON_CODES member for a failed check, from its rule, scope, checker and verdict word. The
    check's detail is read for structure only (which fixed phrase it opens with); none of it is copied."""
    rule, detail = chk["rule"], str(chk.get("detail") or "")
    detail = detail[len(REPAIR):] if detail.startswith(REPAIR) else detail
    scope = "claim" if chk.get("claim_id") is not None else "sentence"
    if detail == CHECK_INCOMPLETE:
        return "check_incomplete"
    if rule == "K4":
        if detail.startswith("writer returned") and detail.endswith("support checks were withheld"):
            return "support_withheld_overflow"
        if detail.startswith("crowd wording:") or detail.startswith("short_answer: crowd wording:"):
            return f"support_{scope}_crowd_wording"
        m = _SUPPORT_VERDICT.match(detail)
        return f"support_{scope}_{m.group(1)}" if m and m.group(1) != "supported" else f"support_{scope}_other"
    if rule == "critic":
        standing, local = _critic_parts(chk)
        if standing == INCONSISTENT:
            return "critic_answer_inconsistent"
        if standing == "not ruled out":
            return "critic_rival_not_ruled_out" if local else "critic_rival_and_why_now"
        if standing is not None and not local:
            return "critic_why_now_not_shown"
        return "unclassified"
    if _code_label(chk):
        # No retained.REASON_CODES member names a code label yet; the catch-all is truer than a banned term.
        return "unclassified"
    if rule == "K3":
        for (rx, _), code in zip(K3_PARTS, K3_CODES):
            if rx.search(detail.strip()):
                return code
        return "sentence_place_other"
    return SENTENCE_CODES.get(rule, "unclassified")


def retained_columns(chk):
    """span_sha256 and reason_code for claim_checks (W8-DEC-14), on a failed support or sentence check only, else
    nothing. The code is worked out again from the row here, whatever the row carries. The digest cannot be
    recomputed because the span is never kept, so only its shape is checked: a value that is not 64 lowercase hex
    characters is dropped, which keeps text out of the column."""
    if not retained.is_retained(chk):
        return {}
    out = {"reason_code": _reason_code(chk)}
    if retained.is_digest(chk.get("span_sha256")):
        out["span_sha256"] = chk["span_sha256"]
    return out


def failed_reason(result):
    """A card's failed_reason once its explanation failed its checks: the fixed wording of the row that held it.
    That is, in order, a cut on a claim the sentence rests on, the sentence's support check, the place fault, the
    critic, then a fault in the sentence itself; rows from before the repair round did not hold the card."""
    if result.get("reason") == "too_few_claims":
        return TOO_FEW
    if result.get("reason") == "check_incomplete":
        return CHECK_INCOMPLETE_WORDS
    rests_on = set(result.get("rests_on") or [])
    # A title row never held a card: its cut only drops the written title.
    rows = [c for c in result.get("checks") or [] if not str(c.get("detail") or "").startswith(REPAIR)
            and c.get("rule") != TITLE_RULE]
    breaches = [c for c in rows if c["verdict"] == "breach"]
    if result.get("reason") == "breach" and breaches:
        # Any breach holds the whole card, rested on or not: name the breach itself.
        row = next((c for c in breaches if c["claim_id"] in rests_on), breaches[0])
        return _wording(row, rested=row["claim_id"] in rests_on)[:HELD_MAX]
    failed = breaches or [c for c in rows if c["verdict"] == "cut"]
    for held in (lambda c: c["claim_id"] in rests_on,
                 lambda c: c["claim_id"] is None and c["rule"] == "K4",
                 lambda c: c["claim_id"] is None and c["rule"] == "K3",
                 lambda c: c["rule"] == "critic",
                 lambda c: c["claim_id"] is None,
                 # judge() fails the whole explanation on any K9 cut, rested on or not.
                 lambda c: c["rule"] == "K9"):
        row = next((c for c in failed if held(c)), None)
        if row is not None:
            return _wording(row, rested=row["claim_id"] in rests_on)[:HELD_MAX]
    return NO_REST


class _Drafts:
    """Passes model calls through and keeps the explanation_claim_ids of the latest writer draft, which
    explain_trend does not return when the explanation fails."""

    def __init__(self, model):
        self.model = model
        self.rests_on = []

    def complete_json(self, **kw):
        out, usage = self.model.complete_json(**kw)
        if "explanation_claim_ids" in kw["schema"]["properties"] and isinstance(out, dict):
            self.rests_on = [x for x in out.get("explanation_claim_ids") or [] if isinstance(x, str)]
        return out, usage


def _explain_one(cand, *, model, spent_before, d, model_call_guard, retry_guard=None, retry_504_guard=None):
    start, end = _window(d, cand["market"])
    row = cand["row"]
    drafts = _Drafts(model)
    try:
        result = explain_trend(row, cand["pack"], model=drafts, spent_today_usd=spent_before, window_start=start,
                               window_end=end, market=cand["market"], rerun=cand["rerun"],
                               model_call_guard=model_call_guard, second_draft=True, retry_guard=retry_guard,
                               retry_504_guard=retry_504_guard)
    except Exception as exc:
        # One bad trend is held back with its reason; it never costs the other markets their brief.
        return {"explanation": None, "explanation_claim_ids": [], "claims": [], "numbers_only": True,
                "reason": "job_error", "usage_usd": 0.0, "checks": [], "error": f"{type(exc).__name__}: {exc}",
                "rests_on": []}
    return {**result, "rests_on": drafts.rests_on}


def _recovery_deadline(now, d):
    local = now.astimezone(SAST)
    value = os.environ.get("BRIEF_RECOVERY_UNTIL")
    if (os.environ.get("FORCE_RERUN") != "1" or os.environ.get("RUN_DATE") != local.date().isoformat()
            or d != local.date() or not isinstance(value, str)
            or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None):
        return None
    hour, minute = (int(part) for part in value.split(":"))
    return datetime.combine(local.date(), time(hour, minute), SAST)


def _brief_deadline(now, d, chain):
    recovery = _recovery_deadline(now, d)
    if recovery is not None:
        return now.astimezone(SAST) >= recovery, recovery
    return chain.past_deadline(now, d), None


def _out_of_time(now, started):
    """True once the run that started at started is FINISH_MARGIN short of the brief's task timeout."""
    return started is not None and now - started >= collect_chain.TIMEOUTS["brief"] - FINISH_MARGIN


def _call_allowance(now, started, d, chain):
    past, recovery = _brief_deadline(now, d, chain)
    if d != now.astimezone(SAST).date() or past or _out_of_time(now, started):
        return 0.0
    cutoffs = [recovery or datetime.combine(d, collect_chain.DEADLINE, SAST),
               datetime.combine(d + timedelta(days=1), time(), SAST)]
    if started is not None:
        cutoffs.append(started + collect_chain.TIMEOUTS["brief"] - FINISH_MARGIN)
    return max(0.0, (min(cutoffs) - now).total_seconds())


def _retry_admits(result, cand, breaker, spent_usd, now, started, d, chain):
    """True when an explanation that ended in a model error (a model call that failed or ran out of time, after any
    further attempt its check call made) gets one more whole attempt (23 Oct 2026: KE's top bucket 0 candidate and an
    NG candidate were held "Explanation not run" at 05:15 SAST, an hour before the deadline). The same limits as any
    explanation's start apply, with the 504 retry's floor: no refusal (a model that blocked the request or its reply is
    never asked again, W8-DEC-15), no busy model's doing (that is the breaker's and the resume's to wait out), a
    breaker that has not tripped, the same SAST day, the spend so far with this attempt's own below the day's cap,
    and a whole model call (GEMINI_TIMEOUT_S) still fitting before the deadline and the time limit
    (_call_allowance). Nothing is raised: the cap, the call timeout and the deadline are the ones every call meets."""
    if (result.get("reason") != "model_error" or result.get("model_refused") or cand.get("busy_reason")
            or breaker.tripped):
        return False
    if d != now.astimezone(SAST).date() or spent_usd + result.get("usage_usd", 0.0) >= model_daily_usd():
        return False
    return _call_allowance(now, started, d, chain) >= GEMINI_TIMEOUT_S


def _after_retry(first, second):
    """The second attempt's result, which is what the card shows and stores, with the first attempt's spend added:
    both attempts were billed, and a reservation either one kept stays reserved."""
    out = {**second, "usage_usd": first.get("usage_usd", 0.0) + second.get("usage_usd", 0.0)}
    reserved = first.get("model_reserved_usd", 0.0) + second.get("model_reserved_usd", 0.0)
    if reserved:
        out["model_reserved_usd"] = reserved
        out["usage_unknown"] = True
    return out


def _explain_all(tasks, *, model, base_usd, spend, clock, chain, d, workers, started=None, sleep=None, busy=None):
    """Run explanations in task order, one at a time. No new one starts past the deadline or the time limit
    (_out_of_time from started, stopped as at the deadline, with limit task_timeout in the stop), outside the
    current SAST accounting day, or over the model cap, or once the model breaker has tripped. A busy model is
    waited out call by call (_Breaker, BUSY_RETRIES and the settings beside it); no wait ends past the deadline, the
    time limit or the day. sleep (default time.sleep) does every wait. spend["usd"] grows as explanations finish.
    A Today-bound candidate the busy model left unexplained gets cand["busy_reason"], the fixed wording its held item
    shows as failed_reason: the one whose own call ran out of tries, and, once the breaker tripped on refusals or the
    deadline stopped a run that had waited on the model, each one not started. An explanation that ended in a model
    error is made once more, from the writer on, when _retry_admits allows it; the card shows the second attempt and
    both are billed (_after_retry), and spend["model_retry"] lists each retried candidate with its second reason.
    busy, when given, gets the run's refusals, waits and give-ups. Returns
    ({id(cand): result}, the breaker's error or None, the deadline stop or None)."""
    results, running, capped = {}, {}, False
    explanation_stop = None

    def can_wait(seconds):
        then = clock() + timedelta(seconds=seconds)
        return (d == then.astimezone(SAST).date() and not _brief_deadline(then, d, chain)[0]
                and not _out_of_time(then, started))

    breaker = _Breaker(model, retries=_setting("BUSY_RETRIES", BUSY_RETRIES, 0),
                       wait=_busy_wait(_setting("BUSY_WAIT_S", BUSY_WAIT_S, 0.0),
                                       _setting("BUSY_WAIT_MAX_S", BUSY_WAIT_MAX_S, 0.0),
                                       _setting("BUSY_JITTER", BUSY_JITTER, 0.0)),
                       can_wait=can_wait, sleep=sleep, trip_after=_setting("BUSY_TRIP_ITEMS", BUSY_TRIP_ITEMS, 1),
                       call_allowance=lambda: _call_allowance(clock(), started, d, chain))
    pace = _setting("PACE_S", PACE_S, 0.0)
    workers = 1

    def attempt(cand, spent_before):
        gave_up = breaker.gave_up
        result = _explain_one(cand, model=breaker, spent_before=spent_before, d=d,
                              model_call_guard=lambda: d == clock().astimezone(SAST).date(),
                              retry_guard=lambda: _call_allowance(clock(), started, d, chain) > 0,
                              retry_504_guard=lambda: _call_allowance(clock(), started, d, chain) >= GEMINI_TIMEOUT_S)
        if breaker.gave_up > gave_up and result["reason"] == "model_error":
            cand["busy_reason"] = MODEL_BUSY if breaker.busy == "deadline" else MODEL_REFUSED
        return result

    def explain(cand):
        result = attempt(cand, base_usd + spend["usd"])
        if not _retry_admits(result, cand, breaker, base_usd + spend["usd"], clock(), started, d, chain):
            return result
        print(f"brief: explanation of {cand['market']} {cand['row'].get('item_id')} tried once more after a model "
              f"error: {str(result.get('error'))[:300]}", file=sys.stderr)
        second = attempt(cand, base_usd + spend["usd"] + result["usage_usd"])
        spend.setdefault("model_retry", []).append({"market": cand["market"], "item_id": cand["row"].get("item_id"),
                                                    "reason": second["reason"]})
        return _after_retry(result, second)

    def collect(done):
        nonlocal capped
        for fut in done:
            cand = running.pop(fut)
            result = fut.result()
            results[id(cand)] = result
            spend["usd"] += result["usage_usd"]
            reserved = result.get("model_reserved_usd", 0.0)
            if reserved:
                spend["model_reserved_usd"] = spend.get("model_reserved_usd", 0.0) + reserved
            capped = capped or result["reason"] == "model_cap"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, cand in enumerate(tasks):
            while len(running) >= workers:
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                collect(done)
            if pace and index and not breaker.tripped:
                (sleep or _sleep)(pace)
            now = clock()
            if (capped or breaker.tripped or base_usd + spend["usd"] >= model_daily_usd()
                    or d != now.astimezone(SAST).date()):
                break
            past_deadline, _ = _brief_deadline(now, d, chain)
            out_of_time = _out_of_time(now, started)
            if past_deadline or out_of_time:
                explanation_stop = {"reason": "deadline", "attempted": len(results),
                    "skipped": [{"market": item["market"], "item_id": item["row"]["item_id"]}
                        for item in tasks[index:]]}
                if out_of_time and not past_deadline:
                    explanation_stop["limit"] = "task_timeout"
                break
            fut = pool.submit(explain, cand)
            running[fut] = cand
        collect(wait(list(running))[0])
    # Only a busy model's doing gets the wording: a cap, a day change or a slow run without refusals leaves it unset.
    left = (MODEL_REFUSED if breaker.busy == "refused" else MODEL_BUSY if breaker.busy == "deadline"
            or (explanation_stop is not None and breaker.refusals) else None)
    if left is not None:
        for cand in tasks:
            if id(cand) not in results:
                cand["busy_reason"] = left
    if busy is not None and breaker.refusals:
        busy.update({"refusals": breaker.refusals, "gave_up": breaker.gave_up,
                     "waited_s": round(breaker.waited_s, 1), "tripped": breaker.busy})
    return results, breaker.tripped, explanation_stop


def _for_today(cand):
    """Only published, current-market cards enter Today's confirmation and explanation paths."""
    return (cand["decision"].publish and cand["decision"].where == "today"
            and cand["row"].get("market_scope") == "market")


def _floor_held(cand):
    """A current-market candidate held only for too few posts it can show or too few local posts. Confirm searches
    it too (ENGINE.md section 3: one search on each top candidate), since the posts confirm finds are the only way
    its pack can grow; every other hold stands whatever a search finds, so those are never searched."""
    return (bool(cand.get("floor_held")) and not cand.get("error") and not _measured_zero(cand)
            and cand["row"].get("market_scope") == "market")


def _adopted(cand):
    """True for a candidate in the candidate statement's pattern bucket 0: a sound or a format its own local posts
    show adopted by 3 or more creators on 2 or more days (core/brief/sql/brief.sql _selection_pattern)."""
    return cand["row"].get("_selection_pattern") == 0


def _in_rank_order(by_market):
    """Each market's Today-bound candidates interleaved by rank, so every market's top trends go first: those in
    pattern bucket 0 (_adopted) of every market before the rest, each part interleaved by rank, so a model failure or
    a deadline late in the run never costs the candidates whose posts show adoption (23 Oct 2026: KE's top bucket 0
    candidate was held unexplained). Only the order of the explanations changes; which candidates are judged does
    not."""
    lists = [[c for c in by_market[m] if _for_today(c)] for m in MARKETS]

    def interleaved(parts):
        return [lst[i] for i in range(max(map(len, parts), default=0)) for lst in parts if i < len(lst)]

    return (interleaved([[c for c in lst if _adopted(c)] for lst in lists])
            + interleaved([[c for c in lst if not _adopted(c)] for lst in lists]))


def _payload_candidate(cand, result, specificity=None):
    row, pack = cand["row"], cand["pack"]
    reason = result.get("reason") if isinstance(result, dict) else None
    explained = (isinstance(result, dict) and "reason" in result and reason is None
                 and result.get("numbers_only") is False)
    status = "not_run" if not isinstance(result, dict) or "reason" not in result else (
        "explained" if explained else "failed_checks" if reason in FAILED_CHECKS else "not_run")
    # An explanation started and not completed names its cause (payload.NOT_RUN_WORDS) unless a busy model's wording
    # already does; the model's error text goes to the log only, never to the payload (22 Oct 2026).
    not_run = cand.get("busy_reason") or (NOT_RUN_WORDS.get(reason) if status == "not_run" else None)
    if status == "not_run" and reason in NOT_RUN_WORDS and isinstance(result, dict) and result.get("error"):
        print(f"brief: explanation not run for {row.get('item_id')}: {reason}: {str(result['error'])[:300]}",
              file=sys.stderr)
    return {
        **row, "decision": cand["decision"], "explanation_status": status, "numbers": pack["numbers"],
        "evidence": pack["evidence"], "sparkline": cand["sparkline"], "held_reason": cand.get("held_reason"),
        "held_reason_detail": cand.get("held_reason_detail"), "held_reason_audit": cand.get("held_reason_audit"),
        "failed_reason": failed_reason(result) if status == "failed_checks" else (
            not_run if status == "not_run" else None),
        "explanation": result.get("explanation") if explained else None,
        "title_written": result.get("title_written") if explained else None,
        "explanation_claim_ids": result.get("explanation_claim_ids") if explained else [],
        "claims": result.get("claims") if explained else [], "also": cand.get("also") or [],
        "specificity": specificity, "news_driven": explained and result.get("news_driven") is True,
        # The critic's own answer, kept for audit only: the payload stores it apart from cards and held items.
        "critic": result.get("critic") if isinstance(result, dict) else None,
        "_event_checked": explained and result.get("local_why_now_checked") is True,
    }


def _today_admits(item, market, d):
    """True when Today would admit the item as a card (core/api/today.py _today_card_fault), read on the card the
    payload builds for it alone."""
    return _today_card_fault(preview_card(item, market, d)) is None


def _published(item):
    decision = item["decision"]
    if isinstance(decision, dict):
        return decision.get("publish") is True and decision.get("where") == "today"
    return decision.publish is True and decision.where == "today"


def _merge_events(market, d, items, posts):
    """What one Today card per event would merge, in shadow (RELEASE-E-PLAN item 8h, W8: records and gates nothing
    until E3). items: the market's payload items after the explanation gate (G10); posts: {item_id: member posts},
    every post the item's card could rest on (post_set plus the pack). The cards judged are the published items the
    payload would show (payload.SHOWN in Today's order) that Today would admit. In Today's order, an item that is one
    event (same_event.reason) with a higher ranked kept card, and with every card already recorded into it, is
    recorded into the first such card; the lower ranked merges into the higher, as 8h says. Nothing on the items
    changes and nothing reads the record. Near pairs are every pair of judged cards, in Today's order, that passes
    every check but the dates (same_event.near). Returns {"judged": n, "merges": [{kept, item_id, shared_posts,
    shared_names, date}], "near": [{a, b, shared_posts, shared_names, dated}]}, with no name, title or post text."""
    def event(item):
        return {"explanation": item.get("explanation"), "posts": posts.get(item["item_id"]) or set()}

    # The payload's own shown cards: the top SHOWN by worth, in Today's order (payload _today_order), so the kept card
    # is the one Today shows higher.
    shown = sorted(sorted((i for i in items if _published(i)), key=_worth)[:SHOWN], key=_today_order)
    judged = [i for i in shown if _today_admits(i, market, d)]
    kept, taken, merges = [], {}, []
    for item in judged:
        found = None
        for k in kept:
            why = same_event_reason(event(k), event(item))
            if why is not None and all(same_event_reason(event(t), event(item)) for t in taken[k["item_id"]]):
                found = (k, why)
                break
        if found is None:
            kept.append(item)
            taken[item["item_id"]] = []
            continue
        k, why = found
        taken[k["item_id"]].append(item)
        merges.append({"kept": k["item_id"], "item_id": item["item_id"], **why})
    near = []
    for i, a in enumerate(judged):
        for b in judged[i + 1:]:
            why = same_event_near(event(a), event(b))
            if why is not None:
                near.append({"a": a["item_id"], "b": b["item_id"], **why})
    return {"judged": len(judged), "merges": merges, "near": near}


def _publish_events(market, d, items, posts):
    """Complete-link fixture groups after G10, preserving the representative's original authority."""
    eligible = sorted((i for i in items if _published(i) and i.get("market", market) == market), key=_worth)
    ranked = sorted(eligible[:SHOWN], key=_today_order) + sorted(eligible[SHOWN:], key=_today_order)
    admitted = [i for i in ranked if _today_admits(i, market, d)]
    anchors = {i["item_id"]: fixture_anchor(i.get("explanation"), market, d, checked=i.get("_event_checked"))
               for i in admitted}
    groups, merges, replacements, gone = [], [], {}, set()
    for item in admitted:
        critic = item.get("critic")
        text = item.get("explanation")
        if (anchors[item["item_id"]] is not None or item.get("_event_checked") is not True
                or not isinstance(critic, dict) or critic.get("scheduled_event") is not True
                or not isinstance(text, str)):
            continue
        parts = re.split(r"\blikely because\b", text, flags=re.I)
        if len(parts) != 2 or not re.search(r"\b(?:MTN8|Premier League)\b", text, re.I):
            continue
        team = r"(?-i:[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,4})"
        primary_fixture = re.fullmatch(
            r"^\s*(?:(?:South African|Nigerian|Kenyan|local)\s+)?(?:football\s+)?"
            r"(?:creators|fans|supporters)\s+reacted(?:\s+on\s+(?:TikTok|Twitter|Reddit|Instagram|YouTube|X))?"
            r"\s+to\s+(?:the\s+)?(?:"
            r"(?:MTN8|Premier League)\s+(?:football\s+)?(?:final|match|fixture)"
            r"|(?:matches?|fixtures?|final)\s+(?:involving|between|against)\s+" + team +
            r"(?:\s+(?:and|against|vs\.?|versus)\s+" + team + r")?)"
            r"(?:\s+on\s+(?:TikTok|Twitter|Reddit|Instagram|YouTube|X))?\s*,?\s*", parts[0], re.I)
        if primary_fixture:
            replacements[item["item_id"]] = {**item,
                "decision": Decision(False, "held_back", None, FIXTURE_REACTION, "G3", False),
                "held_reason": "not_confirmed", "held_reason_code": FIXTURE_REACTION_CODE}

    def matches(a, b):
        shared = len(set(posts.get(a["item_id"]) or ()) & set(posts.get(b["item_id"]) or ()))
        why = fixture_reason(anchors.get(a["item_id"]), anchors.get(b["item_id"]))
        return {**why, "shared_posts": shared} if why and shared >= MIN_EVIDENCE else None

    for item in admitted:
        if item["item_id"] in replacements:
            continue
        group = next((g for g in groups if all(matches(t, item) for t in g)), None)
        if group is None:
            groups.append([item])
            continue
        representative = group[0]
        key = representative["item_id"]
        kept = replacements.setdefault(key, {**representative, "also": list(representative.get("also") or [])})
        aliases = [{"item_id": item["item_id"], "title": item["title"]}, *(item.get("also") or [])]
        seen = {key, *(a["item_id"] for a in kept["also"])}
        for alias in aliases:
            if alias["item_id"] not in seen:
                kept["also"].append(dict(alias))
                seen.add(alias["item_id"])
        merges.append({"kept": key, "item_id": item["item_id"], **matches(representative, item)})
        gone.add(item["item_id"])
        group.append(item)
    possible = []
    for item in admitted:
        if item["item_id"] in gone or item["item_id"] in replacements or anchors.get(item["item_id"]) is not None:
            continue
        partial = fixture_anchor(item.get("explanation"), market, d, checked=item.get("_event_checked"),
                                 allow_undated=True)
        if partial is None or partial["date"] is not None:
            continue
        for group in groups:
            kept = group[0]
            known = anchors.get(kept["item_id"])
            if known is None or not fixture_reason(known, partial, allow_undated=True):
                continue
            event = {"explanation": item.get("explanation"), "posts": posts.get(item["item_id"]) or set()}
            if not all(same_event_near(
                    {"explanation": t.get("explanation"), "posts": posts.get(t["item_id"]) or set()}, event)
                       or same_event_reason(
                    {"explanation": t.get("explanation"), "posts": posts.get(t["item_id"]) or set()}, event)
                       for t in group):
                continue
            fragment = {"status": "possible_duplicate", "reference_item_id": kept["item_id"],
                        "missing": ["event_date"]}
            replacements[item["item_id"]] = {**item,
                "decision": Decision(False, "held_back", None,
                    "Possible duplicate event: its event date is unknown; the dated card is shown separately", "G3", False),
                "held_reason": "not_confirmed", "event_fragment": fragment}
            possible.append({"item_id": item["item_id"], **fragment})
            break
    return ([replacements.get(i["item_id"], i) for i in items if i["item_id"] not in gone],
            {"judged": len(admitted), "anchored": sum(a is not None for a in anchors.values()), "merges": merges,
             "possible_fragments": possible})


def _market_payload(market, d, cands, results, *, banners, moments_, boards_, issues, selection_audit=None,
                    event_shadow=None):
    # A candidate whose market scope could not be read is not known to be global, so it stays, held for its
    # unreadable evidence.
    # A candidate admitted under locality_v2.1 stays whatever its scope: a not_local one is held by G6 and shown, which
    # is the silent drop of the pack scope no longer applying (C4 v3 section 11.3).
    local_cands = [c for c in cands if c["row"].get("market_scope") == "market" or c.get("scope_error")
                   or c["row"].get("locality_basis") == V2_BASIS]
    items = []
    for c in local_cands:
        result = results.get(id(c))
        decision = c["decision"]
        publish = decision.get("publish") if isinstance(decision, dict) else decision.publish
        where = decision.get("where") if isinstance(decision, dict) else decision.where
        numbers_only = decision.get("numbers_only") if isinstance(decision, dict) else decision.numbers_only
        specificity = None
        if where == "today":
            checked = result if isinstance(result, dict) else {}
            specificity = assess_specificity(
                explanation=checked.get("explanation"), claims=checked.get("claims"),
                explanation_claim_ids=checked.get("explanation_claim_ids"), evidence=c["pack"]["evidence"],
                market=market, local_why_now=checked.get("local_why_now_checked") is True,
                why_now_inferred=checked.get("why_now_inferred") is True,
            )
        item = _payload_candidate(c, result, specificity)
        if where == "today" and (item["explanation_status"] != "explained" or publish is not True
                                 or numbers_only is not False or specificity["status"] != "pass"):
            # An item the loop never reached has no failed check to report; a busy model's items keep the generic
            # wording, which their failed_reason qualifies. The hold is G10 either way.
            never_reached = item["explanation_status"] == "not_run" and not c.get("busy_reason") and result is None
            item["decision"] = _held(NOT_REACHED_TEXT if never_reached else "Explanation failed its checks", "G10")
            item["held_reason"] = "explanation_failed"
            if item["explanation_status"] == "explained":
                # The model explained it but the gate or the specificity check held it: report a failed check, not
                # an explanation (the enum in core/api/contract.md has no explained-and-held value).
                item["explanation_status"] = "failed_checks"
        items.append(item)
    # One card per event, in shadow, on the final decision: after G10, so a card held there takes no part. The
    # shadow records and gates nothing, so a failure in it is logged and recorded and never costs the brief.
    try:
        shadow = _merge_events(market, d, items, {c["row"]["item_id"]: c.get("posts") or set() for c in local_cands})
    except Exception as e:
        print(f"brief {d.isoformat()}: event_merge_shadow_failed {type(e).__name__}", file=sys.stderr)
        shadow = {"error": type(e).__name__}
    if event_shadow is not None:
        event_shadow[market] = shadow
    items, event_publication = _publish_events(
        market, d, items, {c["row"]["item_id"]: c.get("posts") or set() for c in local_cands})
    # An evidence read that failed is a data problem too, so it counts with G1 toward the over-30% banner. The
    # denominator is every current-market candidate considered: the G1 holds that _candidates backfilled past as well
    # as the candidates judged, so the banner still shows how much of what was looked at bad data blocked.
    data = []
    for c in local_cands:
        if c.get("error") or c.get("scope_error"):
            data.append(_held("", "G1"))
            continue
        decision = c["decision"]
        if isinstance(decision, dict):
            decision = Decision(*(decision.get(name) for name in
                                  ("publish", "where", "flag", "reason", "rule", "numbers_only")))
        data.append(decision)
    audit = selection_audit or {}
    # The candidates judged: the receipt's own count (non-G1 candidates less the ones that folded, _selection_result),
    # or, with no receipt, the non-G1 decisions here. When the market judged its full CANDIDATES, the G1 holds took
    # no judged slot from it, so the banner says so and the market's status stays what its judged cards make it
    # (18 Oct 2026, KE: 10 cards judged, status data_issue, Today read "incomplete" for a complete brief).
    judged = ((audit.get("selection_receipt") or {}).get("preparation") or {}).get("judged")
    if not isinstance(judged, int):
        judged = sum(1 for x in data if x.rule != "G1")
    full_judgement = judged >= CANDIDATES
    if market_banner(data):
        n = sum(1 for x in data if x.rule == "G1")
        text = f"Data issue: {n} of {len(local_cands)} candidates held for invalid collection days or unreadable evidence"
        if full_judgement:
            text += f"; the {judged} candidates judged were not affected, so the brief is complete"
        banners = banners + [{"kind": "data_issue", "text": text}]
    payload = build_market_payload(market, d, items, moments=moments_, boards=boards_, banners=banners,
        issues=issues, not_assessed=audit.get("not_assessed"), selection_receipt=audit.get("selection_receipt"),
        full_judgement=full_judgement)
    top = payload["cards"][0] if payload["cards"] else None
    if top and top["explained"]:
        headline = {"text": top["explanation"], "item_id": top["item_id"], "claim_ids": top["explanation_claim_ids"]}
        payload = build_market_payload(market, d, items, moments=moments_, boards=boards_, banners=banners,
            headline=headline, issues=issues, not_assessed=audit.get("not_assessed"),
            selection_receipt=audit.get("selection_receipt"), full_judgement=full_judgement)
    inputs = audit.get("locality_audit_inputs")
    if event_publication["merges"] or event_publication["possible_fragments"]:
        payload["event_publication"] = event_publication
    if inputs is not None:
        shown = {i["item_id"] for part in ("cards", "more") for i in payload[part]}
        shown |= {a["item_id"] for part in ("cards", "more") for i in payload[part] for a in i.get("also") or []}
        shown |= {i["item_id"] for i in payload["held_back"]["items"]}
        shown |= {i["item_id"] for i in (payload.get("not_assessed") or {}).get("items") or []}
        payload["locality_audit"] = build_locality_audit(inputs["detect_run_id"], inputs["not_local"],
                                                         inputs["unreadable_total"], represented_ids=shown)
    return payload


class InsertFailed(RuntimeError):
    """An append that stopped part way. written is the number of rows already in the table."""

    def __init__(self, message, written):
        super().__init__(message)
        self.written = written


def _insert(client, table, rows):
    """Append rows in batches and return how many were written. A failing batch raises InsertFailed carrying the
    count of rows the earlier batches wrote."""
    written = 0
    for i in range(0, len(rows), INSERT_BATCH):
        batch = json.loads(json.dumps(rows[i:i + INSERT_BATCH], default=str))
        try:
            errors = client.insert_rows_json(table, batch)
        except Exception as e:
            raise InsertFailed(f"append to {table} failed: {type(e).__name__}: {e}", written) from e
        if errors:
            raise InsertFailed(f"append to {table} failed: {errors}", written)
        written += len(batch)
    return written


def _briefs_rows(rows):
    return [{**r, "payload": json.dumps(r["payload"], ensure_ascii=False, default=str)} for r in rows]


def _open_sc(make_sc, run_id):
    """(client, None) or (None, why confirm is skipped). Any failure to build the client skips confirm; it never
    stops the brief."""
    if make_sc is None:
        return None, "no SocialCrawl client configured"
    try:
        return make_sc(run_id), None
    except Exception as e:
        return None, f"no SocialCrawl client ({type(e).__name__}: {e})"


def _open_ingest(client, sc):
    """(lane L1's ingest bound for the confirm lane, or None; its status), loaded once per run. No ingest in
    replay mode or without a BigQuery client; "unavailable" while this branch does not have
    core.collect.parse.ingest yet."""
    if client is None:
        return None, "no_client"
    if getattr(sc, "mode", None) == "replay":
        return None, "replay"
    ingest = confirm_lane.load_ingest()
    return ingest, "ok" if ingest is not None else "unavailable"


def _confirm(by_market, d, *, client, sc, confirm, ingest, ingested, spend, chain, clock, started=None):
    """Confirm the Today-bound candidates of each market inside one confirm share, then the candidates held only by
    the post floors (_floor_held), so the share goes to Today-bound ones first. Other held items are skipped: a
    search on them would spend credits without changing any decision, since presence never lifts a hold. Each
    item's platforms_found goes into its pack facts as a presence line, never as evidence, so it cannot count
    towards a corroboration label. When ingest is given, the confirm results also go into posts and
    post_observations; the evidence packs were built before confirm and are not read again in this run, so those
    posts become citable only from the next evidence read. ingested gets {market: {posts, observations, errors}}.
    No market and no search starts past the deadline or the time limit (_out_of_time from started), and a market
    stopped that way is not confirmed, so a slow vendor cannot push explanations past 06:15 or the task timeout; a confirm call that raises, or a market whose
    searches the lane stopped (its share used, or the client's cap_reached, balance_floor or insufficient_credits),
    stops confirming for the rest of the run, leaves that market unconfirmed and keeps the credits already recorded.
    Returns ({market: {item_id: platforms_found}} for the items found anywhere, the set of markets confirmed, and
    why confirming stopped or None)."""
    found_by_market, confirmed, why = {}, set(), {}

    def stop():
        now = clock()
        past_deadline, recovery = _brief_deadline(now, d, chain)
        if past_deadline:
            cutoff = recovery.strftime("%H:%M") if recovery is not None else "06:15"
            why["note"] = f"stopped at the {cutoff} deadline"
        elif _out_of_time(now, started):
            why["note"] = "stopped at the task time limit"
        return "note" in why

    for m in MARKETS:
        if stop():
            return found_by_market, confirmed, why["note"]
        cands = ([c for c in by_market[m] if _for_today(c)]
                 + [c for c in by_market[m] if not _for_today(c) and _floor_held(c)])
        if not cands:
            confirmed.add(m)
            continue
        try:
            found = confirm([c["row"] for c in cands], sc=sc, market=m, d=d, share_used=spend["credits"],
                            seen=None, client=client, ingest=ingest, clock=clock, stop=stop)
        except Exception as e:
            return found_by_market, confirmed, f"stopped in {m} ({type(e).__name__}: {e})"
        spend["credits"] += sum(r["credits"] for r in found.values())
        if ingest is not None:
            tally = ingested.setdefault(m, {"posts": 0, "observations": 0, "errors": 0})
            for item_id, r in found.items():
                for k in tally:
                    tally[k] += (r.get("ingest") or {}).get(k, 0)
                for call in r.get("calls") or []:
                    if call.get("ingest_error"):
                        print(f"brief {d.isoformat()}: ingest {m}:{item_id} {call['route']}: {call['ingest_error']}",
                              file=sys.stderr)
        for c in cands:
            c["confirm_posts"] = ((found.get(c["row"]["item_id"]) or {}).get("ingest") or {}).get("posts", 0)
            platforms = (found.get(c["row"]["item_id"]) or {}).get("platforms_found") or []
            if platforms:
                since = c["row"].get("first_seen") or d - timedelta(days=confirm_lane.SINCE_DAYS)
                c["pack"]["facts"].append(f"Also found by search since {since.isoformat()}: {', '.join(platforms)}")
                found_by_market.setdefault(m, {})[c["row"]["item_id"]] = list(platforms)
        stopped = [r for r in found.values() if r.get("status") == "stopped"]
        if stopped:
            if "note" in why:
                return found_by_market, confirmed, why["note"]
            halts = [call["status"] for r in stopped for call in r.get("calls") or []
                     if call.get("status") in confirm_lane.HALT]
            return found_by_market, confirmed, f"stopped in {m} ({halts[0] if halts else 'confirm share used'})"
        confirmed.add(m)
    return found_by_market, confirmed, None


def _regrow(client, d, by_market, *, chain, clock, build_ctx, campaign_hashtags, political_terms, core, agent,
            hidden=None, started=None, calendar=None):
    """Read the pack again for each floor-held candidate whose confirm search wrote posts, and gate it again with
    the same floors (MIN_EVIDENCE showable, 2 local), so posts confirm found can lift a hold in the run that found
    them, read through the same suppression list (hidden) as the first packs. Today-bound candidates keep the pack
    they were confirmed with. A rebuilt candidate that is now
    Today-bound merges into a kept card when it adds fewer than MIN_EVIDENCE new posts or half or more of its posts
    are on that card, as _merge does; one whose
    read fails keeps its hold. Past the deadline or the time limit (_out_of_time from started) nothing is read
    again. Returns [{market, item_id, posts_before, posts_after, held}] for the candidates read again, and the
    merges, in rank order."""
    grown, merged = [], []
    for m in MARKETS:
        now = clock()
        if _brief_deadline(now, d, chain)[0] or _out_of_time(now, started):
            break
        for i, cand in enumerate(list(by_market[m])):
            if not (_floor_held(cand) and cand.get("confirm_posts")):
                continue
            new = _prepare(client, d, m, cand["row"], build_ctx=build_ctx, campaign_hashtags=campaign_hashtags,
                           political_terms=political_terms[m], hidden=hidden, core=core, agent=agent,
                           calendar=(calendar or {}).get(m, ()))
            if new.get("error"):
                print(f"brief {d.isoformat()}: regrow {m}:{cand['row']['item_id']}: {new['error']}", file=sys.stderr)
                continue
            new["pack"]["facts"].extend(f for f in cand["pack"]["facts"] if f not in new["pack"]["facts"])
            new["_round"], new["_rank"] = cand.get("_round", 0), cand.get("_rank", 0)
            new["decision"] = _gate(new, None)
            by_market[m][i] = new
            grown.append({"market": m, "item_id": cand["row"]["item_id"],
                          "posts_before": len(cand["pack"]["evidence"]), "posts_after": len(new["pack"]["evidence"]),
                          "held": not _for_today(new)})
        kept = [c for c in by_market[m] if _for_today(c) and "also" in c]
        claimed = set().union(*(c["posts"] for c in kept))
        for cand in sorted((c for c in by_market[m] if _for_today(c) and "also" not in c),
                           key=lambda c: (c.get("_round", 0), c.get("_rank", 0), _worth(c["row"]))):
            posts = cand["posts"]
            best = max(((len(posts & k["posts"]), -j) for j, k in enumerate(kept)), default=(0, 0))
            if best[0] > 0 and (len(posts - claimed) < MIN_EVIDENCE or best[0] * 2 >= len(posts)):
                into = kept[-best[1]]
                _absorb(into, cand)
                merged.append({"market": m, "into": into["row"]["item_id"], "item_id": cand["row"]["item_id"]})
                by_market[m] = [c for c in by_market[m] if c is not cand]
            else:
                cand["also"] = []
                kept.append(cand)
            claimed |= posts
    return grown, merged


def _resume_after_429(tasks, results, busy, *, model, base_usd, spend, clock, chain, d, workers, started, sleep,
                      read_base):
    """DEC-18: when the breaker stopped the run on 429 before 06:15 SAST, wait RESUME_WAIT_S and explain once more
    what the 429s left, once. Only explanations the busy model left (no result, or a model_error that carries its
    busy_reason) are asked for again; a card already explained, or one that failed for another reason, is kept as it
    is. Nothing is resumed at or after 06:15, when the wait would end at or past the deadline or the time limit, or
    when the day's spend has reached the cap. The day's spend before this run's own is read again after the wait with
    read_base(), because other stages spend meanwhile; a failed read keeps the value read at the start. Returns
    (results, unavailable, explanation_stop, resume): the pass's own results and stops, and the record for the run
    counts, or None when no resume happened. results is changed in place."""
    stopped = busy.get("tripped")
    now = clock()
    if (stopped is None or now.astimezone(SAST) >= datetime.combine(d, collect_chain.DEADLINE, SAST)
            or base_usd + spend["usd"] >= model_daily_usd()):
        return None
    then = now + timedelta(seconds=RESUME_WAIT_S)
    if _brief_deadline(then, d, chain)[0] or _out_of_time(then, started):
        return None
    pending = [c for c in tasks if id(c) not in results
               or (results[id(c)]["reason"] == "model_error" and c.get("busy_reason"))]
    if not pending:
        return None
    (sleep or _sleep)(RESUME_WAIT_S)
    try:
        base_usd = read_base()
    except Exception as exc:
        print(f"brief {d.isoformat()}: the day's spend could not be read again after the wait "
              f"({type(exc).__name__}); the value read at the start stands", file=sys.stderr)
    if base_usd + spend["usd"] >= model_daily_usd():
        return None
    before = {id(c): c.pop("busy_reason", None) for c in pending}
    again = {}
    got, unavailable, stop = _explain_all(pending, model=model, base_usd=base_usd, spend=spend, clock=clock,
                                          chain=chain, d=d, workers=workers, started=started, sleep=sleep, busy=again)
    results.update(got)
    for c in pending:
        reached_without_wording = id(c) in got and got[id(c)]["reason"] == "model_error"
        if (id(c) not in got or reached_without_wording) and not c.get("busy_reason") and before[id(c)]:
            c["busy_reason"] = before[id(c)]
    busy.update({"refusals": busy["refusals"] + again.get("refusals", 0),
                 "gave_up": busy["gave_up"] + again.get("gave_up", 0),
                 "waited_s": round(busy["waited_s"] + again.get("waited_s", 0.0), 1),
                 "tripped": again.get("tripped")})
    return got, unavailable, stop, {"wait_s": RESUME_WAIT_S, "stopped_by": stopped, "retried": len(pending),
                                    "tripped_again": bool(unavailable)}


def _brief(client, d, run, *, chain, model, sc, sc_skipped, clock, build_ctx, confirm, campaign_hashtags,
           political_terms, workers, spend, core, agent, started=None, sleep=None):
    hidden = read_hidden(client, core=core, agent=agent)  # raises SuppressionUnreadable before anything is spent
    warm = warmup_banner(client, d, core, agent)
    base_usd = spent_today(client, d, core, agent)
    calendar = {m: moments(client, d, m, core, agent) for m in MARKETS}

    selection_audits = {}
    by_market = _candidates(client, d, build_ctx=build_ctx, campaign_hashtags=campaign_hashtags,
                            political_terms=political_terms, hidden=hidden, core=core, agent=agent, chain=chain,
                            clock=clock, started=started, calendar=calendar, selection_audits=selection_audits)
    merged = _merge(by_market)

    found, confirmed, note = {}, set(), sc_skipped and f"skipped: {sc_skipped}"
    ingest_status, ingested = None, {}
    if sc is not None:
        ingest, ingest_status = _open_ingest(client, sc)
        if ingest_status == "unavailable":
            print(f"brief {d.isoformat()}: ingest unavailable, confirm finds are not written to posts",
                  file=sys.stderr)
        found, confirmed, note = _confirm(by_market, d, client=client, sc=sc, confirm=confirm, ingest=ingest,
                                          ingested=ingested, spend=spend, chain=chain, clock=clock,
                                          started=started)
    if note:
        print(f"brief {d.isoformat()}: confirm {note}", file=sys.stderr)
    grown, late_merged = _regrow(client, d, by_market, chain=chain, clock=clock, build_ctx=build_ctx,
                                 campaign_hashtags=campaign_hashtags, political_terms=political_terms, hidden=hidden,
                                 core=core, agent=agent, started=started, calendar=calendar)
    merged += late_merged

    tasks = _in_rank_order(by_market)
    busy = {}
    results, unavailable, explanation_stop = _explain_all(tasks, model=model, base_usd=base_usd, spend=spend,
                                                           clock=clock, chain=chain, d=d, workers=workers,
                                                           started=started, sleep=sleep, busy=busy)
    resume = _resume_after_429(tasks, results, busy, model=model, base_usd=base_usd, spend=spend, clock=clock,
                               chain=chain, d=d, workers=workers, started=started, sleep=sleep,
                               read_base=lambda: spent_today(client, d, core, agent))
    if resume is not None:
        _, unavailable, explanation_stop, resume = resume
    from core.brief import title_purity

    title_receipts, receipt_omitted = title_purity.project_receipts(tasks, results)

    brief_rows, check_rows, cards, held = [], [], 0, 0
    event_shadow = {}
    published_at = clock()
    recovery = _recovery_deadline(published_at, d)
    late_banner = None
    if recovery is not None:
        late_banner = {"kind": "late_run",
            "text": f"Late run: checked at {published_at.astimezone(SAST).strftime('%H:%M')}"}
    for m in MARKETS:
        cands = by_market[m]
        for c in cands:
            result = results.get(id(c))
            if _for_today(c):
                c["decision"] = _gate(c, result is not None and result["reason"] is None)
            for chk in (result or {}).get("checks") or []:
                check_rows.append({"answer_or_brief_id": f"{run.run_id}:{m}:{c['row']['item_id']}",
                                   "claim_id": chk["claim_id"], "rule": chk["rule"], "verdict": chk["verdict"],
                                   "checker": chk["checker"], "run_id": run.run_id,
                                   "reason": check_reason(chk), **retained_columns(chk)})
        banners = [b for b in (warm, None if m in confirmed else NO_CONFIRM, late_banner) if b]
        if selection_audits.get(m, {}).get("candidate_read") in ("held", NULL_COLUMN_READ_FAILED):
            banners.append({"kind": "data_issue", "text": "Data issue: today's candidates could not be read"})
        boards_, unnamed = boards(client, d, m, core, agent, hidden=hidden)
        payload = _market_payload(m, d, cands, results, banners=banners, moments_=calendar[m],
                                  boards_=boards_, issues=_unnamed_issue(unnamed), selection_audit=selection_audits.get(m),
                                  event_shadow=event_shadow)
        # Titles, aliases and model text never name a suppressed person either, as Today reads them.
        selection_receipt = payload.pop("selection_receipt", None)
        payload = without_hidden(payload, hidden)
        payload["selection_receipt"] = selection_receipt
        cards += len(payload["cards"]) + len(payload["more"])
        held += payload["held_back"]["count"]
        brief_rows.append(brief_row(m, d, run.run_id, published_at, payload, RULE_VERSION))

    counts = {"markets": len(MARKETS), "cards": cards, "held": held, "credits": spend["credits"],
              "model_usd": round(spend["usd"], 6), "platforms_found": found, "merged": merged}
    if spend.get("model_reserved_usd"):
        counts["model_reserved_usd"] = round(spend["model_reserved_usd"], 6)
    if spend.get("model_retry"):
        counts["model_retry"] = spend["model_retry"]
    if title_receipts:
        counts["title_majority_receipts"] = title_receipts
    if receipt_omitted:
        counts["title_majority_receipts_omitted"] = receipt_omitted
    shadow = [{"market": m, "item_id": c["row"]["item_id"],
               "pack_scope": (c["row"].get("pack_scope_v1") or {}).get("market_scope") if c["row"].get(
                   "locality_basis") == V2_BASIS else c["row"].get("market_scope"),
               "v2_status": c["row"]["locality_v2"]["status"], "v2_label": c["row"]["locality_v2"]["label"]}
              for m in MARKETS for c in by_market[m] if c.get("decision") is not None and c["row"].get("locality_v2")]
    if shadow:
        counts["locality_shadow"] = shadow
    unread = {m: a["candidate_read"] for m, a in selection_audits.items() if a.get("candidate_read")}
    column_missing = [m for m, v in unread.items() if MISSING_VIEW_COLUMN in v]
    if column_missing:
        counts["view_column_missing"] = {"markets": column_missing, "column": "market_news_posts7", "action": "null_column"}
    null_failed = [m for m, v in unread.items() if v == NULL_COLUMN_READ_FAILED]
    if null_failed:
        counts["view_column_read_failed"] = {"markets": null_failed, "column": "market_news_posts7", "action": "held"}
    unread = {m: v for m, v in unread.items() if v not in (MISSING_VIEW_COLUMN, NULL_COLUMN_READ_FAILED)}
    if unread:
        counts["locality_view_failed"] = {"markets": list(unread), "action": "held" if "held" in unread.values()
                                          else "read_without_view"}
    reads = Counter(c["pack"].get("rival_read") for cands in by_market.values() for c in cands)
    if reads.keys() - {None}:
        counts["rival_reads"] = {k: reads.get(k, 0) for k in ("ok", "cutoff_missing", "failed")}
    if explanation_stop is not None:
        counts["explanation_stop"] = explanation_stop
    if busy:
        counts["model_busy"] = busy
    if resume is not None:
        counts["model_resume"] = resume
    pack_errors = {f"{c['market']}:{c['row']['item_id']}": c["error"] for m in MARKETS for c in by_market[m]
                   if c.get("error")}
    if pack_errors:
        counts["pack_errors"] = pack_errors
    if unavailable:
        counts["model"] = {"reason": "model_unavailable", "error": unavailable, "numbers_only": [
            c["row"]["item_id"] for c in tasks if (results.get(id(c)) or {}).get("reason", "not_started")]}
    if note:
        counts["confirm"] = note
    if grown:
        counts["regrown"] = grown
    if ingest_status is not None:
        counts["ingest"] = {"status": ingest_status, "by_market": ingested} if ingest_status == "ok" else {
            "status": ingest_status}
    if title_receipts and title_purity.wire_bytes(counts) > title_purity.COUNTS_WIRE_LIMIT:
        counts.pop("title_majority_receipts", None)
        counts.pop("title_majority_receipts_omitted", None)
        for brief in brief_rows:
            for card in brief["payload"]["cards"] + brief["payload"]["more"]:
                if card.get("kind") == "topic":
                    card["title_written"] = None
        for candidate in tasks:
            if "title_majority" in results.get(id(candidate), {}):
                check_rows.append({"answer_or_brief_id": f"{run.run_id}:{candidate['market']}:{candidate['row']['item_id']}",
                                   "claim_id": None, "rule": "title", "verdict": "cut", "checker": "code",
                                   "run_id": run.run_id, "reason": "Title check: receipt byte bound"})
    # Added after the size bound above, so the shadow's own bytes can never cost a topic card its written title.
    counts["event_merge_shadow"] = event_shadow
    _insert(client, f"{agent}.briefs", _briefs_rows(brief_rows))
    if check_rows:
        # The diagnostics are written after the briefs: a claim_checks table that has not had its W8-DEC-14
        # columns added yet costs the diagnostics, never a market's brief.
        try:
            _insert(client, f"{agent}.claim_checks", check_rows)
        except Exception as e:
            counts["claim_checks"] = {"status": "failed", "written": getattr(e, "written", 0),
                                      "rows": len(check_rows), "error": str(e)[:300]}
            print(f"brief {d.isoformat()}: claim_checks not written ({type(e).__name__})", file=sys.stderr)
    return counts


def _publish_data_issue(client, d, run, error, *, clock, core, agent):
    warm = warmup_banner(client, d, core, agent)
    banners = [b for b in (warm, UPSTREAM) if b]
    published_at = clock()
    rows = []
    for m in MARKETS:
        boards_, unnamed = boards(client, d, m, core, agent)
        payload = build_market_payload(m, d, [], moments=moments(client, d, m, core, agent), boards=boards_,
                                       banners=banners, issues=_unnamed_issue(unnamed))
        rows.append(brief_row(m, d, run.run_id, published_at, payload, RULE_VERSION))
    _insert(client, f"{agent}.briefs", _briefs_rows(rows))
    return {"markets": len(MARKETS), "cards": 0, "held": 0, "credits": 0, "model_usd": 0.0,
            "event_merge_shadow": {m: {"not_run": "data_issue"} for m in MARKETS}}


def run(client, d, *, chain, model, make_sc, clock, build_ctx=gatectx.build_ctx, confirm=confirm_lane.confirm,
        campaign_hashtags=None, political_terms=None, workers=WORKERS, core=CORE, agent=AGENT, sleep=None):
    """The brief for day d. Returns the counts it finished the run with. Raises chain.AlreadyDone, and
    chain.UpstreamNotReady before the deadline; any other failure after begin finishes the run failed and
    re-raises. make_sc(run_id) builds the confirm-share SocialCrawl client once the run has begun; None, or a
    factory that raises, skips confirm with a coverage note. political_terms: {market: [term]}; both lists
    default to core/brief's yaml files. The run's time limit (_out_of_time) counts from clock() here. sleep (default
    time.sleep) does the waits on a busy model (_explain_all)."""
    started = clock()
    if campaign_hashtags is None:
        campaign_hashtags = gatectx.load_campaign_hashtags()
    if political_terms is None:
        political_terms = {m: gatectx.load_political_terms(m) for m in MARKETS}

    error = None
    try:
        brief = chain.begin("brief", d)
    except chain.UpstreamNotReady as e:
        if not chain.past_deadline(clock(), d):
            raise
        brief, error = e.run, str(e)

    spend = {"usd": 0.0, "credits": 0}
    try:
        try:
            if error is not None:
                counts = _publish_data_issue(client, d, brief, error, clock=clock, core=core, agent=agent)
            else:
                sc, sc_skipped = _open_sc(make_sc, brief.run_id)
                counts = _brief(client, d, brief, chain=chain, model=model, sc=sc, sc_skipped=sc_skipped, clock=clock,
                                build_ctx=build_ctx, confirm=confirm, campaign_hashtags=campaign_hashtags,
                                political_terms=political_terms, workers=workers, spend=spend, core=core, agent=agent,
                                started=started, sleep=sleep)
        except Exception as e:
            counts = {"markets": 0, "cards": 0, "held": 0, "credits": spend["credits"],
                      "model_usd": round(spend["usd"], 6),
                      "event_merge_shadow": {m: {"not_run": "run_failed"} for m in MARKETS}}
            if spend.get("model_reserved_usd"):
                counts["model_reserved_usd"] = round(spend["model_reserved_usd"], 6)
            chain.finish(brief, "failed", counts, error=f"{type(e).__name__}: {e}")
            raise
        chain.finish(brief, "ok", counts, error=error)
        return counts
    finally:
        primary = sys.exc_info()[1]
        if isinstance(model, GeminiModel):
            try:
                model.close_deadline_calls()
            except Exception:
                if primary is None:
                    raise
                print("brief: deadline owner cleanup failed; primary error preserved", file=sys.stderr)


def live_socialcrawl(client, clock):
    """The factory main() hands to run: lane L1's SocialCrawlClient on the confirm share, live, writing its
    ledger and raw_responses rows through L1's BigQuery stores. Imported only when called."""
    def make(run_id):
        from core.collect.socialcrawl_client import SocialCrawlClient, requests_http
        from core.collect.stores import BigQueryLedgerStore, BigQueryRawStore

        return SocialCrawlClient(share="confirm", run_id=run_id, mode="live",
                                 ledger=BigQueryLedgerStore(client, PROJECT), raw=BigQueryRawStore(client, PROJECT),
                                 http=requests_http, clock=clock)
    return make


# Maximum HTTP phase timeout for one Gemini SDK attempt, clamped to the remaining allowance again at dispatch.
# An expired allowance blocks later dispatches; the awaited SDK request also has a cooperative elapsed deadline.
# Vertex receives it as the server's own deadline, so a call still thinking at this limit ends in 504
# DEADLINE_EXCEEDED. At 60 s every explanation of 24 Oct 2026 ended that way or in 500 INTERNAL (15 of 15 tried, all
# three markets), and the two candidates that ran out of time on 23 Oct ran out again: the candidates whose posts show
# an adopted sound or format, which the brief explains first since 2.7.0, carry the largest packs. Ask allows 120 s
# a call; the brief allows 180 s. The deadline, the task time limit and the day's model cap are unchanged.
GEMINI_TIMEOUT_S = 180


def brief_model():
    """The brief's structured-output client: GeminiModel with GEMINI_TIMEOUT_S."""
    from core.llm.provider import provider

    provider()
    return GeminiModel(project=PROJECT, timeout_s=GEMINI_TIMEOUT_S)


_AUTO = object()


def main(client=None, chain=None, model=None, make_sc=_AUTO, clock=None, **kw):
    """Exit code: 0 on a published brief or AlreadyDone, 1 on UpstreamNotReady before the deadline or any failure.
    Nothing here raises. kw passes through to run (build_ctx, confirm, campaign_hashtags, political_terms,
    workers, core, agent)."""
    try:
        if chain is None:
            from core.collect import chain
        if client is None:
            from google.cloud import bigquery
            client = bigquery.Client(project=PROJECT)
        if model is None:
            model = brief_model()
        clock = clock or (lambda: datetime.now(SAST))
        if make_sc is _AUTO:
            make_sc = live_socialcrawl(client, clock)
        d = chain.today(clock())
    except Exception:
        traceback.print_exc()
        return 1
    try:
        counts = run(client, d, chain=chain, model=model, make_sc=make_sc, clock=clock, **kw)
    except chain.AlreadyDone as e:
        print(f"brief {d.isoformat()}: nothing to do, {e}")
        return 0
    except chain.UpstreamNotReady as e:
        print(f"brief {d.isoformat()}: {e}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    print(json.dumps({"brief": d.isoformat(), "counts": counts}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
