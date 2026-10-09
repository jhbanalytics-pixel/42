"""The morning brief Cloud Run job (BUILD.md 1.12 and 1.13, ENGINE.md sections 1 and 3, TRUST.md section 2).

A fixed pipeline, not agent runs. Per market: the top 10 candidates of the current detect run by worth_raw, named
items first, with a candidate held for invalid data days (G1) not taking one of the 10 slots, so the next ranked
rows of the top 90 are judged in its place (_candidates), an SQL evidence pack each, the publish gate (core/trust/gate.py, plus G4 for items detect marked
likely coordinated, which gate_card does not read), then one card per set of posts: a Today-bound candidate with
fewer than 3 posts not already on a higher card merges into the card it shares most posts with, listed there under
also and recorded in the counts, before any confirm or model spend, one confirm search each for the Today-bound ones inside the confirm share, then
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
from core.api.today import without_hidden
from core.brief import pack_order
from core.brief.evidence import OFFSETS, SuppressionUnreadable, build_pack, read_hidden
from core.brief.explain import CHECK_INCOMPLETE, STANDINGS, TITLE_RULE, explain_trend
from core.brief.locality_audit import build_locality_audit
from core.brief.market_scope import read_market_scope
from core.brief.payload import (MODEL_BUSY, MODEL_REFUSED, NOT_ASSESSED_REASONS, NOT_REACHED_TEXT, _worth, brief_row,
                               build_market_payload)
from core.brief.specificity import MIN_EVIDENCE, assess_specificity, counted_local_posts, showable_posts
from core.collect import chain as collect_chain
from core.config.caps import model_daily_usd
from core.detect import sqlrun
from core.detect.job import PROJECT, RULE_VERSION
from core.detect.sqlrun import AGENT, CORE
from core.llm.gemini import GeminiModel
from core.trust import locality, retained
from core.trust.gate import Decision, gate_card, market_banner
from core.trust.locality import V2_BASIS, locality_block, read_locality, row_from_prefixed, scope_basis

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
FAILED_CHECKS = {"breach", "failed_checks", "too_few_claims", "check_incomplete"}
_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)
QUERIES = {_NAME.search(s).group(1): s for s in sqlrun.split(SQL.read_text(encoding="utf-8"))}

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
}
SENTENCE_WORDS = {
    "K2": "the explanation had a number not pinned to its query",
    "K3": "the explanation named a place no cited post is located in",
    "K4": "the explanation sentence was not supported by its posts",
    "K6": "the explanation used a banned term",
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
OTHER_WORDS = {"K5": "the label was lowered to what the evidence allows",
               "K10": "the status was lowered as claims were cut",
               # The writer's title (core/brief/explain.py): a cut only drops it, so the card keeps its label.
               "title": "the written title did not pass, so the card keeps its label"}
NOT_PASSED = "did not pass"
TOO_FEW = "Claim checks: fewer than 2 claims passed"
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
_CRITIC = re.compile(r"^critic: simplest non-cultural explanation: (.*?); (?:(?:not )?ruled out|(?:news|event)-driven "
                     r"with local reaction): ", re.S)
_CRITIC_PARTS = re.compile(r"^critic: simplest non-cultural explanation: .*?; (not ruled out|ruled out|news-driven with "
                           r"local reaction|event-driven with local reaction): ", re.S)
# A critic pass on a news event that local creators react to (Albert, 2 Oct); never worded as ruled out.
NEWS_PASS = "Critic: news-driven, local creators react in their own words"
# The same pass on a scheduled event: a release, match, holiday or scheduled cultural moment (Albert, 3 Oct).
EVENT_PASS = "Critic: event-driven, local creators react in their own words"
# The standings of a news or scheduled event that passed on local reaction; a cut there failed the why-now only.
REACTION_STANDINGS = ("news-driven with local reaction", "event-driven with local reaction")

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
    return bool(_HASH_ID.fullmatch(value) or _CHANNEL_ID.fullmatch(value) or _PLATFORM_ID.fullmatch(value))


def readable_title(label, key):
    """The label, else a canonical key that is a name rather than an id, else None."""
    for value in (label, key):
        if value and not _id_like(value):
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
    return decision


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


def _ranked(rows):
    """At most POOL rows in the query's order, rows without a readable title (card_title) after every named row,
    so a platform id never takes a slot from a named item. Unnamed rows still fill slots named rows leave empty, and
    are held there as having no readable name."""
    named = [r for r in rows if card_title(r) is not None]
    return (named + [r for r in rows if card_title(r) is None])[:POOL]


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
    judged = sum(not _held_g1(c) for c in prepared)
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
    except Exception:
        print(f"brief {d.isoformat()}: locality_view_read_failed", file=sys.stderr)
        receipt.clear()
        held = locality.LOCALITY_AUTHORITY == "v2"
        if failed is not None:
            failed[market] = "held" if held else "read_without_view"
        if held:
            return []
        return _query(client, "candidates_without_locality", params, core, agent, receipt=receipt)


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

    Each market judges up to CANDIDATES candidates that G1 does not hold. A candidate G1 holds stays in the list,
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
        pools[m] = [{k: v for k, v in row.items() if k not in (
            "_selection_sql_rank", "_selection_market_scope", "_selection_snapshot")} for row in rows]
    creator_names(client, [r for m in MARKETS for r in pools[m]], hidden, core, agent)
    ranked = {m: _ranked(pools[m]) for m in MARKETS}
    by_market = {m: [] for m in MARKETS}
    taken = dict.fromkeys(MARKETS, 0)
    cutoff, observed = None, None
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
            rows = []
            for m in MARKETS:
                need = CANDIDATES - sum(1 for c in by_market[m] if not _held_g1(c))
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
            for cand in cands:
                cand["decision"] = _gate(cand, None)
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


def _merge(by_market):
    """One card per set of posts. Per market, the Today-bound candidates in rank order: one with fewer than
    MIN_EVIDENCE posts not already on a higher kept card is not a card of its own, and merges into the kept card it
    shares most posts with (the higher one on a tie). Held candidates keep their hold and claim no posts. Merged
    candidates leave by_market, so they are neither confirmed, explained, shown nor held. Returns
    [{market, into, item_id}] in rank order."""
    merged = []
    for m in MARKETS:
        kept, claimed, gone = [], set(), set()
        for cand in sorted((c for c in by_market[m] if _for_today(c)), key=lambda c: _worth(c["row"])):
            posts = cand["posts"]
            best = max(((len(posts & k["posts"]), -i) for i, k in enumerate(kept)), default=(0, 0))
            if len(posts - claimed) < MIN_EVIDENCE and best[0] > 0:
                into = kept[-best[1]]
                _absorb(into, cand)
                claimed |= posts
                merged.append({"market": m, "into": into["row"]["item_id"], "item_id": cand["row"]["item_id"]})
                gone.add(id(cand))
            else:
                cand["also"] = []
                kept.append(cand)
                claimed |= posts
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
    can name only the four standings or None when unreadable."""
    standing, local = chk.get("standing"), chk.get("local_why_now")
    if standing in STANDINGS and isinstance(local, bool):
        return standing, local
    detail = str(chk.get("detail") or "")
    m = _CRITIC_PARTS.match(detail)
    return (m.group(1) if m else None), detail.endswith("local why-now checked")


def _wording(chk, rested=False):
    """The fixed wording for one check row. rested: the row cuts a claim the explanation sentence rests on."""
    rule, verdict = chk["rule"], chk["verdict"]
    if chk.get("detail") == CHECK_INCOMPLETE:
        return f"{'Critic' if rule == 'critic' else RULE_NAMES.get(rule, 'Claim checks')}: {CHECK_INCOMPLETE}"
    if rule == "critic":
        menu = _menu(chk.get("detail"))
        standing, local = _critic_parts(chk)
        news = standing == "news-driven with local reaction"
        event = standing == "event-driven with local reaction"
        if verdict == "pass":
            head = NEWS_PASS if news else EVENT_PASS if event else "Critic: the simpler explanation was ruled out"
            return f"{head}: {menu}" if menu else head
        # A cut names the part that failed: the simpler explanation, the local why-now, or both.
        ruled_out = standing == "ruled out" or standing in REACTION_STANDINGS
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
    words = (SENTENCE_WORDS.get(rule) if chk.get("claim_id") is None else None) or CLAIM_WORDS.get(rule, NOT_PASSED)
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
        if standing == "not ruled out":
            return "critic_rival_not_ruled_out" if local else "critic_rival_and_why_now"
        if standing is not None and not local:
            return "critic_why_now_not_shown"
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
                 lambda c: c["claim_id"] is None):
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


def _explain_one(cand, *, model, spent_before, d, model_call_guard, retry_guard=None):
    start, end = _window(d, cand["market"])
    row = cand["row"]
    drafts = _Drafts(model)
    try:
        result = explain_trend(row, cand["pack"], model=drafts, spent_today_usd=spent_before, window_start=start,
                               window_end=end, market=cand["market"], rerun=cand["rerun"],
                               model_call_guard=model_call_guard, second_draft=True, retry_guard=retry_guard)
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


def _explain_all(tasks, *, model, base_usd, spend, clock, chain, d, workers, started=None, sleep=None, busy=None):
    """Run explanations in task order, one at a time. No new one starts past the deadline or the time limit
    (_out_of_time from started, stopped as at the deadline, with limit task_timeout in the stop), outside the
    current SAST accounting day, or over the model cap, or once the model breaker has tripped. A busy model is
    waited out call by call (_Breaker, BUSY_RETRIES and the settings beside it); no wait ends past the deadline, the
    time limit or the day. sleep (default time.sleep) does every wait. spend["usd"] grows as explanations finish.
    A Today-bound candidate the busy model left unexplained gets cand["busy_reason"], the fixed wording its held item
    shows as failed_reason: the one whose own call ran out of tries, and, once the breaker tripped on refusals or the
    deadline stopped a run that had waited on the model, each one not started. busy, when given, gets the run's
    refusals, waits and give-ups. Returns
    ({id(cand): result}, the breaker's error or None, the deadline stop or None)."""
    results, running, capped = {}, {}, False
    explanation_stop = None
    reserved_before = spend.get("model_reserved_usd", 0.0)  # an earlier pass's reservation stays in the total

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

    def explain(cand):
        gave_up = breaker.gave_up
        result = _explain_one(cand, model=breaker, spent_before=base_usd + spend["usd"], d=d,
                              model_call_guard=lambda: d == clock().astimezone(SAST).date(),
                              retry_guard=lambda: _call_allowance(clock(), started, d, chain) > 0)
        if breaker.gave_up > gave_up and result["reason"] == "model_error":
            cand["busy_reason"] = MODEL_BUSY if breaker.busy == "deadline" else MODEL_REFUSED
        return result

    def collect(done):
        nonlocal capped
        for fut in done:
            cand = running.pop(fut)
            result = fut.result()
            results[id(cand)] = result
            spend["usd"] += result["usage_usd"]
            reserved = sum(getattr(exc, "reserved_usd", 0.0) for exc in breaker.uncertain_calls)
            if reserved:
                spend["model_reserved_usd"] = reserved_before + reserved
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
    return (bool(cand.get("floor_held")) and not cand.get("error")
            and cand["row"].get("market_scope") == "market")


def _in_rank_order(by_market):
    """Each market's Today-bound candidates interleaved by rank, so every market's top trends go first."""
    lists = [[c for c in by_market[m] if _for_today(c)] for m in MARKETS]
    return [lst[i] for i in range(max(map(len, lists), default=0)) for lst in lists if i < len(lst)]


def _payload_candidate(cand, result, specificity=None):
    row, pack = cand["row"], cand["pack"]
    reason = result.get("reason") if isinstance(result, dict) else None
    explained = (isinstance(result, dict) and "reason" in result and reason is None
                 and result.get("numbers_only") is False)
    status = "not_run" if not isinstance(result, dict) or "reason" not in result else (
        "explained" if explained else "failed_checks" if reason in FAILED_CHECKS else "not_run")
    return {
        **row, "decision": cand["decision"], "explanation_status": status, "numbers": pack["numbers"],
        "evidence": pack["evidence"], "sparkline": cand["sparkline"], "held_reason": cand.get("held_reason"),
        "held_reason_detail": cand.get("held_reason_detail"), "held_reason_audit": cand.get("held_reason_audit"),
        "failed_reason": failed_reason(result) if status == "failed_checks" else (
            cand.get("busy_reason") if status == "not_run" else None),
        "explanation": result.get("explanation") if explained else None,
        "title_written": result.get("title_written") if explained else None,
        "explanation_claim_ids": result.get("explanation_claim_ids") if explained else [],
        "claims": result.get("claims") if explained else [], "also": cand.get("also") or [],
        "specificity": specificity, "news_driven": explained and result.get("news_driven") is True,
        # The critic's own answer, kept for audit only: the payload stores it apart from cards and held items.
        "critic": result.get("critic") if isinstance(result, dict) else None,
    }


def _market_payload(market, d, cands, results, *, banners, moments_, boards_, issues, selection_audit=None):
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
    if market_banner(data):
        n = sum(1 for x in data if x.rule == "G1")
        banners = banners + [{"kind": "data_issue", "text": f"Data issue: {n} of {len(local_cands)} candidates held for "
                                                            "invalid collection days or unreadable evidence"}]
    audit = selection_audit or {}
    payload = build_market_payload(market, d, items, moments=moments_, boards=boards_, banners=banners,
        issues=issues, not_assessed=audit.get("not_assessed"), selection_receipt=audit.get("selection_receipt"))
    top = payload["cards"][0] if payload["cards"] else None
    if top and top["explained"]:
        headline = {"text": top["explanation"], "item_id": top["item_id"], "claim_ids": top["explanation_claim_ids"]}
        payload = build_market_payload(market, d, items, moments=moments_, boards=boards_, banners=banners,
            headline=headline, issues=issues, not_assessed=audit.get("not_assessed"),
            selection_receipt=audit.get("selection_receipt"))
    inputs = audit.get("locality_audit_inputs")
    if inputs is not None:
        shown = {i["item_id"] for part in ("cards", "more") for i in payload[part]}
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
    Today-bound merges into a kept card when it adds fewer than MIN_EVIDENCE new posts, as _merge does; one whose
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
            new["decision"] = _gate(new, None)
            by_market[m][i] = new
            grown.append({"market": m, "item_id": cand["row"]["item_id"],
                          "posts_before": len(cand["pack"]["evidence"]), "posts_after": len(new["pack"]["evidence"]),
                          "held": not _for_today(new)})
        kept = [c for c in by_market[m] if _for_today(c) and "also" in c]
        claimed = set().union(*(c["posts"] for c in kept))
        for cand in sorted((c for c in by_market[m] if _for_today(c) and "also" not in c),
                           key=lambda c: _worth(c["row"])):
            posts = cand["posts"]
            best = max(((len(posts & k["posts"]), -j) for j, k in enumerate(kept)), default=(0, 0))
            if len(posts - claimed) < MIN_EVIDENCE and best[0] > 0:
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
        if selection_audits.get(m, {}).get("candidate_read") == "held":
            banners.append({"kind": "data_issue", "text": "Data issue: today's candidates could not be read"})
        boards_, unnamed = boards(client, d, m, core, agent, hidden=hidden)
        payload = _market_payload(m, d, cands, results, banners=banners, moments_=calendar[m],
                                  boards_=boards_, issues=_unnamed_issue(unnamed), selection_audit=selection_audits.get(m))
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
    return {"markets": len(MARKETS), "cards": 0, "held": 0, "credits": 0, "model_usd": 0.0}


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
                      "model_usd": round(spend["usd"], 6)}
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
GEMINI_TIMEOUT_S = 60


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
