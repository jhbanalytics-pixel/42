"""The reader's view of a stored Ask answer (standard library only, so the api service can import it).

A stored answer is never changed. Every read builds this view from it: comments told apart from posts, one claim per
fact, gaps and follow-ups in plain words. Nothing here checks, widens or rewords a claim. A claim that survives is the
stored claim, byte for byte, so the K and G checks it passed still describe exactly what the reader sees.
"""

from __future__ import annotations

import copy
import re

LABEL_RANK = {"inferred": 0, "single_source": 1, "observed": 2, "corroborated": 3}  # as core.agent.checks.LABEL_RANK
PLATFORM_WORDS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube", "reddit": "Reddit", "x": "X",
                  "threads": "Threads", "bluesky": "Bluesky", "facebook": "Facebook", "linkedin": "LinkedIn",
                  "telegram": "Telegram", "news": "News", "music_chart": "Music chart", "wikipedia": "Wikipedia",
                  "web": "Web"}
_ROUTE = re.compile(r"^(" + "|".join(PLATFORM_WORDS) + r")/([a-z0-9_/]+)", re.I)
_ROUTE_ANYWHERE = re.compile(r"\b(" + "|".join(PLATFORM_WORDS) + r")/[a-z0-9_/]+", re.I)
_HANDLE_URL = re.compile(r"^https?://(?:www\.)?tiktok\.com/@([^/?#]+)/", re.I)
_STATUS_WHY = {"empty", "partial", "rate_limited", "auth_failed", "schema_drift", "not_in_replay"}
_QUIET = re.compile(r"\s+")


def is_comment(item) -> bool:
    """A comment record: its id carries the platform and the word comment (enrich_tools._comment_record)."""
    return isinstance(item, dict) and isinstance(item.get("id"), str) and "_comment_" in item["id"]


def comment_parent_handle(item) -> str | None:
    """The author of the post a comment sits under, read from the comment's link, which is the post's link. Only a
    TikTok link names its author; for any other platform the answer is None and the reader sees "a post"."""
    url = item.get("url") if isinstance(item, dict) else None
    match = _HANDLE_URL.match(url) if isinstance(url, str) else None
    return match.group(1) if match else None


def _norm(text) -> str:
    return _QUIET.sub(" ", str(text or "")).strip().lower()


def _fact_key(claim: dict, comment_ids: set):
    """What a claim rests on, as the words of the posts it quotes. Comments are not part of it: they sit under a post
    and are not the post. A claim that quotes no post has no key and is never merged."""
    if claim.get("kind") == "proposal":
        return None
    quotes = [q for q in claim.get("quotes") or [] if isinstance(q, dict) and q.get("evidence_id") not in comment_ids]
    texts = frozenset(_norm(q.get("text")) for q in quotes if _norm(q.get("text")))
    return (claim.get("kind"), texts) if texts else None


def _strength(claim: dict, index: int):
    posts = len(set(claim.get("evidence_ids") or []))
    numbers = len({(n.get("value"), n.get("unit")) for n in claim.get("numbers") or [] if isinstance(n, dict)})
    return (LABEL_RANK.get(claim.get("label"), -1), posts, numbers, len(str(claim.get("text") or "")), -index)


def merge_repeated_claims(answer: dict) -> dict:
    """Claims that quote the same post words are one fact said several times. The strongest stays (label, then the
    most posts, figures and words) and the rest go; a line that referred to a gone claim refers to the one that stays."""
    claims = [c for c in answer.get("claims") or [] if isinstance(c, dict)]
    comment_ids = {e.get("id") for e in answer.get("evidence") or [] if is_comment(e)}
    groups: dict = {}
    for index, claim in enumerate(claims):
        key = _fact_key(claim, comment_ids)
        if key is not None:
            groups.setdefault(key, []).append(index)
    drop, stays = set(), {}
    for members in groups.values():
        if len(members) < 2:
            continue
        best = max(members, key=lambda i: _strength(claims[i], i))
        for i in members:
            if i != best:
                drop.add(i)
                stays[claims[i].get("id")] = claims[best].get("id")
    if not drop:
        return answer
    kept = [c for i, c in enumerate(claims) if i not in drop]
    cited = {eid for c in kept for eid in c.get("evidence_ids") or []}
    out = {**answer, "claims": kept,
           "evidence": [e for e in answer.get("evidence") or [] if not isinstance(e, dict) or e.get("id") in cited]}
    for section in ("so_what", "watch_next"):
        lines = []
        for line in answer.get(section) or []:
            if isinstance(line, dict) and isinstance(line.get("claim_ids"), list):
                ids = list(dict.fromkeys(stays.get(cid, cid) for cid in line["claim_ids"]))
                line = {**line, "claim_ids": ids}
            lines.append(line)
        out[section] = lines
    return out


# What a stored gap says, in the words a reader uses. The code that wrote the gap used table, route and check names.
_FORECAST = "We left out a forecast because it has not yet beaten a simple no-change forecast"
_CUT = "We left out a finding that could not be tied to a post inside the question's dates and market"
_CUT_ANY = "We left out a finding that did not pass its checks"
_BACKGROUND = "We left out some background that depended on a finding that did not pass its checks"
_IMPLICATION = "We left out some takeaways that depended on a finding that did not pass its checks"
_BOTH = "We left out some background and takeaways that depended on a finding that did not pass its checks"
_NO_HANDLE = "We left out some posts because they had no author name to credit"


def route_words(route: str) -> tuple[str, str]:
    """(the gap sentence, the follow-up question) for a source route such as tiktok/post/transcript."""
    match = _ROUTE.match(str(route or "").strip())
    if not match:
        return "We could not read one of the sources this time", "What does the source show once it answers again?"
    platform = PLATFORM_WORDS[match.group(1).lower()]
    rest = match.group(2).lower()
    if rest.endswith("transcript"):
        return ("We could not read the video's spoken words this time",
                "What do the spoken words in these videos say?")
    if rest.endswith("comments"):
        return (f"We could not read the comments on a {platform} post this time",
                f"What are people saying in the {platform} comments?")
    return (f"We could not read {platform} posts this time", f"What does {platform} show once it answers again?")


def _gap(gap):
    if not isinstance(gap, dict):
        return gap
    what = str(gap.get("what") or "").strip()
    searched = str(gap.get("searched") or "").strip()
    why = str(gap.get("why") or "").strip()
    route = _ROUTE.match(searched)
    if route and not what.lower().startswith("live search budget"):
        sentence = route_words(route.group(0))[0]
        if what.lower().startswith("live ") and "partial" in what.lower():
            sentence = f"We only read part of the {PLATFORM_WORDS[route.group(1).lower()]} data this time"
        return {"what": sentence, "searched": "", "why": why if why in _STATUS_WHY else ""}
    low = what.lower()
    if low.startswith("a line on what to watch next was removed") or low.startswith("a line on watch next was removed"):
        return {"what": _FORECAST if "forecast" in low else "We left out a line on what to watch next",
                "searched": "", "why": ""}
    if re.match(r"^a claim was (cut|removed)\b", low) and re.search(r"stored evidence|_comment_", searched):
        return {"what": _CUT if "window" in low or "market" in low else _CUT_ANY, "searched": "", "why": ""}
    if low.startswith("background removed"):
        return {"what": _BACKGROUND, "searched": "", "why": ""}
    if low.startswith("an implication removed"):
        return {"what": _IMPLICATION, "searched": "", "why": ""}
    if low.startswith("some posts found could not be quoted"):
        return {"what": _NO_HANDLE, "searched": "", "why": ""}
    return gap


def plain_gaps(gaps) -> list:
    """The gaps a reader sees: plain words, a background line and a takeaway line said as one, each line once."""
    out, seen = [], set()
    for gap in gaps or []:
        shown = _gap(gap)
        key = tuple(shown.get(k) for k in ("what", "searched", "why")) if isinstance(shown, dict) else repr(shown)
        if key in seen:
            continue
        seen.add(key)
        out.append(shown)
    whats = [g.get("what") if isinstance(g, dict) else None for g in out]
    if _BACKGROUND in whats and _IMPLICATION in whats:
        first = min(whats.index(_BACKGROUND), whats.index(_IMPLICATION))
        out = [{"what": _BOTH, "searched": "", "why": ""} if i == first else g
               for i, g in enumerate(out) if g.get("what") not in (_BACKGROUND, _IMPLICATION) or i == first]
    return out


def _distinct(gaps) -> list:
    out, seen = [], set()
    for gap in gaps or []:
        key = repr(sorted(gap.items())) if isinstance(gap, dict) else repr(gap)
        if key not in seen:
            seen.add(key)
            out.append(gap)
    return out


def plain_followups(followups) -> list:
    """Follow-up questions without a source route in them: a route reads as what it reads, or the question goes."""
    out = []
    for text in followups or []:
        if not isinstance(text, str):
            continue
        found = _ROUTE_ANYWHERE.search(text)
        if found:
            if not re.match(r"^What does .+ show once \S+ answers again\?$", text):
                continue
            text = route_words(found.group(0))[1]
        if text not in out:
            out.append(text)
    return out


def present(record: dict) -> dict:
    """record as a reader sees it, built from a copy. Stable: present(present(x)) == present(x)."""
    if not isinstance(record, dict) or not isinstance(record.get("answer"), dict):
        return record
    shown = copy.deepcopy(record)
    answer = merge_repeated_claims(shown["answer"])
    if isinstance(answer.get("gaps"), list):
        run = shown.get("run") if isinstance(shown.get("run"), dict) else {}
        if "technical_gaps" not in run:
            run = {**run, "technical_gaps": _distinct(answer["gaps"])}
        shown["run"] = run
        answer = {**answer, "gaps": plain_gaps(answer["gaps"])}
    shown["answer"] = answer
    if isinstance(shown.get("run"), dict) and isinstance(shown["run"].get("followups"), list):
        shown["run"] = {**shown["run"], "followups": plain_followups(shown["run"]["followups"])}
    return shown
