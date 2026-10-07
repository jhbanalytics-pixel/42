"""Comparable checked sound and hashtag items, referenced by their existing number pins."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

from core.agent.context import STORE_TOOL, RunContext, result_hash
from core.agent.tools.warehouse import PROFILE_SOURCE
from core.agent.writer import MAX_CLAIMS

UNRANKED = "The checked findings do not share a verified basis for ranking, so they are shown without a ranked list."
MIXED = ("This list shows each comparable item once. Checked findings with different ways of counting or an "
         "uncertain item match remain below.")
_QUESTION = re.compile(r"^\s*(?:which|what)\s+(?:(?:tiktok|instagram|youtube|facebook|threads|reddit|x|new|rising|"
                       r"trending|original|untitled|top|popular|most\s+used)\s+)*(sounds?|audio|hashtags?)\b"
                       r"(?=\s*(?:[?.!,;:]|$)|\s+(?:are|is|was|were|have|has|had|do|does|did|can|could|will|would|"
                       r"should|on|in|from|across|during|since|between|over|this|last|today|yesterday|rose|grew|fell|"
                       r"gained|spread|spreading|rising|trending|popular|used)\b)", re.I)
_PRIOR_UNITS = {"posts", "posts in previous week", "posts in the previous week", "posts in preceding week"}


def _hashtag_in(text, title):
    def continuation(char):
        return bool(char) and (char.isalnum() or char in "_#" or unicodedata.category(char).startswith("M"))

    return any(not continuation(text[match.start() - 1] if match.start() else "")
               and not continuation(text[match.end()] if match.end() < len(text) else "")
               for match in re.finditer(re.escape(title), text, re.I | re.ASCII))


def _day(value):
    if type(value) is date:
        return value
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _platforms(params):
    return {"x" if value == "twitter" else value for key, value in params.items() if key.startswith("platform_")}


def _authoritative(query):
    rows = query.get("rows")
    return (query.get("tool") == STORE_TOOL and isinstance(rows, list)
            and query.get("result_hash") == result_hash(rows))


def _pin(number, query_id, query, ctx, column, row):
    value = number.get("value")
    return (type(value) in (int, float) and value >= 0 and value == row.get(column)
            and number.get("query_id") == query_id and number.get("run_id") == ctx.run_id
            and number.get("result_hash") == query.get("result_hash"))


def _current_pair(claim, query_id, query, ctx, row):
    numbers = claim.get("numbers") or []
    pair = []
    for column in ("creators", "posts"):
        matches = [i for i, number in enumerate(numbers)
                   if str(number.get("unit") or "").strip().lower() == column
                   and _pin(number, query_id, query, ctx, column, row)]
        if len(matches) != 1:
            return None
        pair.append(matches[0])
    return pair


def _prior_index(claim, row, query, ctx, kind):
    start, end = ctx.window_start, ctx.window_end
    if (end - start).days != 6:
        return None
    params = query["params"]
    matches = []
    for i, number in enumerate(claim.get("numbers") or []):
        if str(number.get("unit") or "").strip().lower() not in _PRIOR_UNITS:
            continue
        qid = number.get("query_id")
        previous = ctx.queries.get(qid) or {}
        p = previous.get("params") or {}
        if (not _authoritative(previous) or p.get("market") != params.get("market")
                or p.get("profile_source") != params.get("profile_source")
                or _platforms(p) != _platforms(params)
                or _day(p.get("since")) != start - timedelta(days=7)
                or _day(p.get("until")) != start - timedelta(days=1)):
            continue
        field = "sound_id" if kind == "sounds" else "hashtag"
        held = [r for r in previous["rows"] if r.get("kind") == ("sound" if kind == "sounds" else "hashtag")
                and r.get(field) == row[field] and r.get("platform") == row["platform"]
                and _pin(number, qid, previous, ctx, "posts", r)]
        if len(held) == 1:
            matches.append(i)
    return matches[0] if len(matches) == 1 else None


def ranked_list(question: str, answer: dict, ctx: RunContext, *, masked: bool = False) -> tuple[dict | None, list[str]]:
    subject = _QUESTION.search(question or "")
    if (masked or not subject or answer.get("status") not in ("complete", "partial")
            or not answer.get("claims")):
        return None, []
    kind = "hashtags" if subject[1].lower().startswith("hashtag") else "sounds"
    field = "hashtag" if kind == "hashtags" else "sound_id"
    candidates = []
    for qid, query in ctx.queries.items():
        p = query.get("params") or {}
        platforms = _platforms(p)
        if (not _authoritative(query) or len(platforms) != 1
                or p.get("market") != ctx.market or p.get("profile_source") != PROFILE_SOURCE
                or ctx.window_start is None or ctx.window_end is None
                or _day(p.get("since")) != ctx.window_start or _day(p.get("until")) != ctx.window_end):
            continue
        platform = next(iter(platforms))
        rows = [row for row in query["rows"] if row.get("platform") == platform and row.get(field)
                and (kind != "hashtags" or isinstance(row[field], str))
                and type(row.get("creators")) in (int, float) and type(row.get("posts")) in (int, float)]
        if rows:
            candidates.append((qid, query, platform, rows))
    if len(candidates) != 1:
        return None, [UNRANKED]
    qid, query, platform, rows = candidates[0]
    evidence = {r.get("id"): r for r in answer.get("evidence") or []}
    by_row = {}
    for claim in (answer.get("claims") or [])[:MAX_CLAIMS]:
        cited = set(claim.get("evidence_ids") or []) & set(evidence)
        matches = []
        for position, row in enumerate(rows):
            samples = cited & set(row.get("sample_post_ids") or [])
            pair = _current_pair(claim, qid, query, ctx, row)
            verified_tag = True
            if kind == "hashtags":
                title = row[field] if row[field].startswith("#") else "#" + row[field]
                verified_tag = (len(title) > 1 and not any(char.isspace() for char in title)
                                and _hashtag_in(str(claim.get("text") or ""), title))
            if samples and pair is not None and verified_tag:
                matches.append((position, row, samples, pair))
        if len(matches) != 1:
            continue
        position, row, samples, pair = matches[0]
        if position in by_row:
            continue
        if kind == "hashtags":
            title = row[field] if row[field].startswith("#") else "#" + row[field]
            handle = None
        else:
            title = row.get("sound_title")
            if (not isinstance(title, str) or not title.strip() or title == row["sound_id"]
                    or re.match(r"^original\s+(?:sound|audio)\b", title.strip(), re.I)
                    or not re.search(r"(?<!\w)" + re.escape(title) + r"(?!\w)", str(claim.get("text") or ""), re.I)):
                title = None
            first = str(row.get("earliest_creator") or "").lstrip("@").casefold()
            handle = next((str(evidence[eid].get("handle") or "").lstrip("@") for eid in sorted(samples)
                           if first and str(evidence[eid].get("handle") or "").lstrip("@").casefold() == first), None)
        by_row[position] = {"claim_id": claim["id"], "title": title, "usage_handle": handle,
                            "creators_index": pair[0], "posts_index": pair[1],
                            "previous_posts_index": _prior_index(claim, row, query, ctx, kind),
                            "tied_with_previous": False}
    if not by_row:
        return None, [UNRANKED]
    items, last = [], None
    for position, item in sorted(by_row.items()):
        counts = (rows[position]["creators"], rows[position]["posts"])
        item["tied_with_previous"] = counts == last
        items.append(item)
        last = counts
    previous = None
    if any(item["previous_posts_index"] is not None for item in items):
        previous = {"from": (ctx.window_start - timedelta(days=7)).isoformat(),
                    "to": (ctx.window_start - timedelta(days=1)).isoformat()}
    return {"kind": kind, "scope": "retained_whole_store_claims", "order": "creators_desc_posts_desc",
            "query_id": qid, "market": ctx.market, "platform": platform,
            "measure_columns": {"creators": "creators", "posts": "posts"},
            "window": {"from": ctx.window_start.isoformat(), "to": ctx.window_end.isoformat()},
            "previous_window": previous, "items": items}, [MIXED] if len(items) != len(answer["claims"]) else []
