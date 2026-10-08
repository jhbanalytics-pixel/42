"""Checked platform activity, topic snapshots and creator identities for list questions."""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from core.agent.context import STORE_TOOL, result_hash
from core.agent.ranked import _authoritative, _day, _hashtag_in, _pin
from core.agent.tools.warehouse import PROFILE_SOURCE
from core.agent.writer import MAX_CLAIMS

PLATFORMS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube", "facebook": "Facebook",
             "reddit": "Reddit", "threads": "Threads", "x": "X"}
SUBSET = "Some checked findings could not be placed in this list. They remain below with their sources."
UNAVAILABLE = "A comparable list was not recorded for these checked findings. The findings and their sources remain below."
_QUESTION = re.compile(r"^\s*(?:which|what)\s+(?:(?:top|rising|new|popular|active|busy|busiest|trending|"
                       r"south\s+african|nigerian|kenyan|tiktok|instagram|youtube|facebook|reddit|threads|x)\s+)*"
                       r"(platforms?|topics?|creators?)\b(?=\s*(?:[?.!,;:]|$)|\s+(?:are|is|were|have|has|do|does|"
                       r"on|in|from|across|during|this|last|today|rose|grew|fell|posting|post|use|used)\b)", re.I | re.ASCII)
_PRIOR_UNITS = {"posts", "posts3", "posts in previous week", "posts in the previous week",
                "posts in the same three days last week"}


def _platform(value):
    return "x" if value == "twitter" else value if value in PLATFORMS else None


def _creator_name(text, handle):
    if not handle or any(char.isspace() or char in "@#" for char in handle):
        return None

    def part(char):
        return bool(char) and (char.isalnum() or char in "_-" or unicodedata.category(char).startswith("M"))

    for match in re.finditer(r"@?" + re.escape(handle), text, re.I | re.ASCII):
        before = text[match.start() - 1] if match.start() else ""
        after = text[match.end():].lstrip(".")[:1]
        if part(before) or (before and before in ".@#") or part(after) or (after and after in "@#"):
            continue
        return ("@" if match[0].startswith("@") else "") + handle
    return None


def _query(ctx, qid, tool=None):
    query = ctx.queries.get(qid) or {}
    rows = query.get("rows")
    if (isinstance(rows, list) and query.get("result_hash") == result_hash(rows)
            and (tool is None or query.get("tool") == tool)):
        return query
    return None


def _pair(claim, qid, query, ctx, row, columns):
    pair = []
    for noun, column in zip(("creators", "posts"), columns):
        if type(row.get(column)) is not int or row[column] < 0:
            return None
        held = [i for i, number in enumerate(claim.get("numbers") or []) if isinstance(number, dict)
                and str(number.get("unit") or "").strip().lower() in {noun, column}
                and _pin(number, qid, query, ctx, column, row)]
        if len(held) != 1:
            return None
        pair.append(held[0])
    return pair


def _group(kind, qids, market, start, end):
    creators = kind == "creators"
    return {"kind": kind, "scope": {"platforms": "retained_store_platform_claims",
                                   "topics": "retained_detection_snapshot_claims",
                                   "creators": "retained_creator_identities"}[kind],
            "order": None if creators else "posts_desc_creators_desc" if kind == "platforms" else "creators_desc_posts_desc",
            "query_ids": list(qids), "market": market, "window": {"from": start.isoformat(), "to": end.isoformat()},
            "measure_columns": {"creators": None if creators else "creators3" if kind == "topics" else "creators",
                                "posts": None if creators else "posts3" if kind == "topics" else "posts"},
            "previous_window": None, "previous_basis": None,
            "unavailable": {"creators": "not_recorded" if creators else None,
                            "posts": "not_recorded" if creators else None, "previous_week": "not_recorded"}, "items": []}


def _item(claim, identity, name, platform, cited, pair=None, prior=None):
    return {"claim_id": claim["id"], "entity_id": identity, "name": name, "platform": platform,
            "evidence_ids": sorted(cited), "creators_index": pair[0] if pair else None,
            "posts_index": pair[1] if pair else None, "previous_posts_index": prior,
            "tied_with_previous": False if pair else None}


def _topic_prior(claim, row, current_qid, ctx):
    day = _day(row["metric_date"]) - timedelta(days=7)
    matches = []
    for index, number in enumerate(claim.get("numbers") or []):
        if not isinstance(number, dict) or str(number.get("unit") or "").strip().lower() not in _PRIOR_UNITS:
            continue
        qid = number.get("query_id")
        query = _query(ctx, qid, "rising_topics")
        if not query or qid == current_qid:
            continue
        params = query.get("params") or {}
        since, until = _day(params.get("since")), _day(params.get("date"))
        if not since or not until or not since <= day <= until or params.get("market") not in (None, row["market"]):
            continue
        held = [previous for previous in query["rows"] if isinstance(previous, dict)
                and (previous.get("item_id"), previous.get("market"), previous.get("kind"))
                == (row["item_id"], row["market"], "topic") and _day(previous.get("metric_date")) == day
                and type(previous.get("posts3")) is int and _pin(number, qid, query, ctx, "posts3", previous)]
        if len(held) == 1:
            matches.append((index, qid))
    return matches[0] if len(matches) == 1 else (None, None)


def _measured(kind, claims, evidence, ctx):
    candidates = []
    for qid in ctx.queries:
        query = _query(ctx, qid, STORE_TOOL if kind == "platforms" else "rising_topics")
        if not query:
            continue
        params = query.get("params") or {}
        if kind == "platforms":
            if (not _authoritative(query) or params.get("market") != ctx.market
                    or params.get("profile_source") != PROFILE_SOURCE
                    or _day(params.get("since")) != ctx.window_start or _day(params.get("until")) != ctx.window_end):
                continue
            rows = [row for row in query["rows"] if isinstance(row, dict) and _platform(row.get("platform"))
                    and not any(key in row for key in ("sound_id", "hashtag", "kind", "item_id"))]
            aliases = {}
            for row in rows:
                aliases.setdefault(_platform(row["platform"]), set()).add(row["platform"])
            rows = [row for row in rows if len(aliases[_platform(row["platform"])]) == 1]
        else:
            since, until = _day(params.get("since")), _day(params.get("date"))
            if not since or not until:
                continue
            rows = [row for row in query["rows"] if isinstance(row, dict) and row.get("kind") == "topic"
                    and row.get("market") in ("ZA", "NG", "KE") and row.get("item_id")
                    and (ctx.market is None or row["market"] == ctx.market)
                    and (params.get("market") is None or row["market"] == params["market"])
                    and _day(row.get("metric_date")) is not None
                    and since <= _day(row["metric_date"]) <= until
                    and ctx.window_start <= _day(row["metric_date"]) <= ctx.window_end]
        if rows:
            candidates.append((qid, query, rows))
    if kind == "platforms" and len(candidates) != 1:
        return []
    grouped, seen = {}, set()
    for claim in claims:
        cited = set(claim.get("evidence_ids") or []) & evidence.keys()
        matched = []
        for qid, query, rows in candidates:
            for row in rows:
                platform = _platform(row.get("platform")) if kind == "platforms" else None
                name = PLATFORMS[platform] if platform else row.get("label")
                pair = _pair(claim, qid, query, ctx, row, ("creators", "posts") if platform else ("creators3", "posts3"))
                sources = {eid for eid in cited if kind == "topics" or _platform(evidence[eid].get("platform")) == platform}
                if (pair and sources and isinstance(name, str) and name.strip()
                        and _hashtag_in(str(claim.get("text") or ""), name)):
                    matched.append((qid, row, name, platform, pair, sources))
        if len(matched) != 1:
            continue
        qid, row, name, platform, pair, sources = matched[0]
        day = ctx.window_end if platform else _day(row["metric_date"])
        market = ctx.market if platform else row["market"]
        identity = platform if platform else str(row["item_id"])
        key = (qid, market, day)
        unique = (kind, market, day, identity)
        if unique in seen:
            continue
        seen.add(unique)
        group = grouped.setdefault(key, _group(kind, [qid], market, ctx.window_start if platform else day - timedelta(days=2), day))
        prior, prior_qid = (None, None) if platform else _topic_prior(claim, row, qid, ctx)
        if prior is not None:
            group["previous_window"] = {"from": (day - timedelta(days=9)).isoformat(), "to": (day - timedelta(days=7)).isoformat()}
            group["previous_basis"] = "same_three_day_period_previous_week"
            group["unavailable"]["previous_week"] = None
            if prior_qid not in group["query_ids"]:
                group["query_ids"].append(prior_qid)
        group["items"].append(_item(claim, identity, name, platform, sources, pair, prior))
    by_claim = {claim["id"]: claim for claim in claims}
    for group in grouped.values():
        indices = ("posts_index", "creators_index") if kind == "platforms" else ("creators_index", "posts_index")
        def counts(item):
            numbers = by_claim[item["claim_id"]]["numbers"]
            return tuple(numbers[item[index]]["value"] for index in indices)
        group["items"].sort(key=lambda item: (*(-value for value in counts(item)), item["entity_id"]))
        previous = None
        for item in group["items"]:
            current = counts(item)
            item["tied_with_previous"] = current == previous
            previous = current
    return sorted(grouped.values(), key=lambda group: (group["market"] or "", group["window"]["to"]), reverse=True)


def _creators(claims, evidence, ctx, discovered):
    if not isinstance(discovered, dict):
        return []
    identity_qid, posts_qid = discovered.get("creator_query_id"), discovered.get("posts_query_id")
    identities, posts = _query(ctx, identity_qid), _query(ctx, posts_qid)
    window = discovered.get("window") or {}
    if (not identities or not posts or identities["rows"] != discovered.get("creator_candidates")
            or _day(window.get("since")) != ctx.window_start or _day(window.get("until")) != ctx.window_end):
        return []
    market = discovered.get("discovery_market")
    if market not in (None, "ZA", "NG", "KE"):
        return []
    group = _group("creators", [identity_qid, posts_qid], market, ctx.window_start, ctx.window_end)
    loaded = {record.get("id") for record in discovered.get("evidence") or [] if isinstance(record, dict)}
    seen = set()
    for claim in claims:
        cited = set(claim.get("evidence_ids") or []) & evidence.keys() & loaded
        for creator in identities["rows"]:
            if not isinstance(creator, dict):
                continue
            platform = _platform(creator.get("platform"))
            handle = str(creator.get("handle") or "").lstrip("@")
            text = str(claim.get("text") or "")
            name = _creator_name(text, handle)
            if not platform or not handle or handle.isdecimal() or name is None:
                continue
            sources = {str(post.get("post_id")) for post in posts["rows"] if isinstance(post, dict)
                       and post.get("creator_id") == creator.get("creator_id") and post.get("platform") == creator.get("platform")
                       and str(post.get("post_id")) in cited and _platform(evidence[str(post["post_id"])].get("platform")) == platform
                       and str(evidence[str(post["post_id"])].get("handle") or "").lstrip("@") == handle}
            identity = platform + ":" + str(creator["creator_id"])
            if sources and identity not in seen:
                group["items"].append(_item(claim, identity, name, platform, sources))
                seen.add(identity)
    return [group] if group["items"] else []


def entity_lists(question, answer, ctx, *, creator_discovery=None, masked=False):
    subject = _QUESTION.search(question or "")
    if (masked or not subject or answer.get("status") not in ("complete", "partial")
            or not ctx.window_start or not ctx.window_end or ctx.window_start > ctx.window_end):
        return [], []
    kind = subject[1].lower().rstrip("s") + "s"
    claims = [claim for claim in answer.get("claims") or [] if isinstance(claim, dict) and isinstance(claim.get("id"), str)]
    evidence = {record["id"]: record for record in answer.get("evidence") or []
                if isinstance(record, dict) and isinstance(record.get("id"), str)}
    if not claims:
        return [], []
    groups = _creators(claims, evidence, ctx, creator_discovery) if kind == "creators" else _measured(kind, claims, evidence, ctx)
    truncated = sum(len(group["items"]) for group in groups) > MAX_CLAIMS
    remaining = MAX_CLAIMS
    for group in groups:
        group["items"] = group["items"][:remaining]
        remaining -= len(group["items"])
    groups = [group for group in groups if group["items"]]
    retained = {item["claim_id"] for group in groups for item in group["items"]}
    notices = [] if len(retained) == len(claims) and not truncated else [SUBSET] if retained else [UNAVAILABLE]
    return groups, notices
