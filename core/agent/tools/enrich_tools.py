"""The agent's get_comments and get_transcript tools (AGENT.md, Tools and Effort tiers).

Both enrich a post already in ctx.evidence and spend credits through L1's client (sc_adapter), on the same checks as
socialcrawl_call: an allowed route, calls left, the quote against max_credits and the budget, then the agent_live
lane. Enrichment gets 20% of a tier's credits and at most 20 items. Scraped text goes back inside untrusted_content
fences; ctx keeps it raw so the writer can cite comments and the checks can match a quote against a transcript span.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from urllib.parse import urlparse

from core.agent.context import Refused, RunContext
from core.agent.tools.socialcrawl import (
    ITEM_STATUSES,
    SocialCrawlClient,
    _fence,
    check_route,
    iso_time,
    plain_handle,
    read_credits,
)

COMMENT_ROUTES = {
    "tiktok": "tiktok/post/comments",
    "instagram": "instagram/post/comments",
    "youtube": "youtube/video/comments",
    "reddit": "reddit/post/comments",
    "facebook": "facebook/post/comments",
    "twitter": "twitter/tweet/replies",
    "threads": "threads/post/comments",
}
TRANSCRIPT_ROUTES = {
    "tiktok": "tiktok/post/transcript",
    "instagram": "instagram/media/transcript",
    "youtube": "youtube/video/transcript",
    "twitter": "twitter/tweet/transcript",
    "reddit": "reddit/post/transcript",
    "facebook": "facebook/post/transcript",
}
PLATFORM_ALIASES = {"x": "twitter"}
ENRICH_SHARE = 0.2  # AGENT.md: 20% of a tier's credits for enrichment on the top 10 to 20 items only
MAX_ENRICHED = 20
MAX_COMMENTS = 50
PLAIN_ID = re.compile(r"[\w.:-]{1,128}", re.ASCII)  # a comment id kept as scraped; any other gets a sha256 of the item


def _parent(ctx: RunContext, evidence_id: str, routes: dict, what: str) -> tuple[dict, str, str]:
    record = ctx.evidence.get(evidence_id)
    if record is None:
        raise Refused(f"Evidence id {str(evidence_id)[:80]!r} was not seen in this run.")
    if record.get("parent_id"):
        raise Refused(f"{evidence_id} is a comment; {what} are fetched for posts only.")
    platform = str(record.get("platform") or "").lower()
    platform = PLATFORM_ALIASES.get(platform, platform)
    if platform not in routes:
        raise Refused(f"{platform or 'this platform'} has no {what} route. Platforms with one: {', '.join(routes)}.")
    if not record.get("url"):
        raise Refused(f"{evidence_id} has no URL, so its {what} cannot be fetched.")
    return record, routes[platform], record["url"]


def enrich_credits_left(ctx: RunContext) -> float:
    share = ENRICH_SHARE * ctx.budget["credits"] - ctx.enrich_credits_spent
    return max(0.0, min(ctx.credits_left(), share))


def _charged_call(ctx: RunContext, client: SocialCrawlClient, step: str, route: str, params: dict, max_credits: float,
                  parent_id: str) -> tuple[dict, dict]:
    """socialcrawl_call's budget path, with the enrichment share and item cap on top. As socialcrawl_call does, the
    call's event and its ctx.sc_calls entry are recorded straight after the client answers, before its credits or any
    item is read, so source_status stays aligned with the client's calls. An unreadable credits_charged is an error
    status that charges the quote. Returns the result, with that status, and the event, whose status the caller lowers
    if reading the items fails."""
    check_route(route, params)
    if parent_id not in ctx.enriched and len(ctx.enriched) >= MAX_ENRICHED:
        raise Refused(f"Enrichment is for the top {MAX_ENRICHED} items only, and {MAX_ENRICHED} items are enriched.")
    if ctx.calls_left() <= 0:
        raise Refused(f"no SocialCrawl calls left for this question ({ctx.budget['calls']} allowed)")
    quote = client.quote(route, params)
    if quote > max_credits:
        raise Refused(f"{route} costs {quote} credits, over max_credits {max_credits}")
    left = enrich_credits_left(ctx)
    if quote > left:
        raise Refused(f"{route} costs {quote} credits, over the {left} enrichment credits left in this question "
                      f"({ENRICH_SHARE:.0%} of the budget, within the {ctx.credits_left()} credits left)")

    result = client.call(route, params, lane="agent_live", run_id=ctx.run_id, max_credits=min(max_credits, left))
    status = result.get("status")
    ctx.calls_made += 1
    ctx.enriched.add(parent_id)
    ctx.emit(step, route=route, evidence_id=parent_id, status=status, credits=0.0)
    ctx.sc_calls.append({"route": route, "params": dict(params), "status": status})
    event = ctx.events[-1]
    charged = read_credits(result.get("credits_charged"))
    if charged is None:
        charged, status = float(quote), "error"
        _set_status(ctx, event, status)
    event["credits"] = charged
    ctx.credits_spent += charged
    ctx.enrich_credits_spent += charged
    return {**result, "status": status, "credits_charged": charged}, event


def _set_status(ctx: RunContext, event: dict, status: str) -> None:
    """Record a status found while reading the items on the call's event and its ctx.sc_calls entry."""
    event["status"] = status
    ctx.sc_calls[-1]["status"] = status


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _first(node: dict, *keys):
    return next((node[k] for k in keys if node.get(k) not in (None, "")), None)


def _comment_record(item: dict, parent: dict, platform: str, route: str) -> dict | None:
    """Only the comment's id, handle, text, time and likes. The author object never passes, so no follower,
    demographic or profile field reaches the agent. The vendor nests each row under "comment" (beside its labels and
    language); that inner row is read, and older flat rows are read as they are."""
    if isinstance(item.get("comment"), dict):
        item = item["comment"]
    text = _first(_dict(item.get("content")), "text") or _first(item, "text", "comment", "body")
    if not isinstance(text, str) or not text:
        return None
    native = _first(item, "comment_id", "id", "cid")
    if not isinstance(native, (str, int)) or isinstance(native, bool) or not PLAIN_ID.fullmatch(str(native)):
        canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
        native = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    author = item.get("author")
    handle = author if isinstance(author, str) else _first(_dict(author), "username", "handle", "unique_id")
    likes = _first(_dict(item.get("engagement")), "likes")
    likes = _first(item, "likes", "like_count", "digg_count") if likes is None else likes
    if not isinstance(likes, int) or isinstance(likes, bool) or likes < 0:
        likes = None
    url = item.get("url")
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
    except ValueError:  # "http://[bad" and other malformed hosts
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        url = parent.get("url")
    eid = f"{platform}_comment_{native}"
    return {
        "id": eid,
        "platform": platform,
        "handle": plain_handle(handle or _first(item, "handle", "username")),
        "url": url,
        "posted_at": iso_time(_first(item, "published_at", "created_at", "create_time")),
        "market": parent.get("market"),
        "text": text,
        "engagement": {"likes": likes} if likes is not None else {},
        "flags": ["market_assumed"],  # the market is the parent post's; a comment's own place is not observed
        "parent_id": parent["id"],
        "via": route,
    }


def get_comments(ctx: RunContext, client: SocialCrawlClient, evidence_id: str, limit: int = 20,
                 max_credits: float = 1.0) -> dict:
    parent, route, url = _parent(ctx, evidence_id, COMMENT_ROUTES, "comments")
    if isinstance(limit, float) and not math.isfinite(limit):
        raise Refused(f"limit must be a whole number from 1 to {MAX_COMMENTS}.")
    limit = max(1, min(int(limit), MAX_COMMENTS))
    params = {"url": url, "limit": limit} if route == "threads/post/comments" else {"url": url}
    result, event = _charged_call(ctx, client, "get_comments", route, params, float(max_credits), evidence_id)

    platform = route.split("/", 1)[0]
    status, reason = result.get("status"), str(result.get("reason") or "")
    items = (result.get("items") or []) if status in ITEM_STATUSES else []
    try:
        records = [_comment_record(_dict(item), parent, platform, route) for item in items]
    except Exception:
        _set_status(ctx, event, "error")
        raise
    dropped = records.count(None)
    event["dropped"] = dropped
    if items and dropped == len(items):
        status, reason = "schema_drift", f"{dropped} comments arrived and none had text in a known field"
        _set_status(ctx, event, status)
    elif status == "ok" and dropped * 2 > len(items):  # mostly unread, so source_gaps shows it
        status, reason = "partial", f"{dropped} of {len(items)} comments had no text in a known field"
        _set_status(ctx, event, status)
    evidence_ids, evidence = [], []
    for record in records:
        if record is None or record["id"] in evidence_ids:
            continue
        ctx.evidence[record["id"]] = record
        evidence_ids.append(record["id"])
        evidence.append({**record, "evidence_id": record["id"], "text": _fence(record["text"])})
        if len(evidence_ids) == limit:
            break
    return {
        "parent_id": evidence_id,
        "evidence_ids": evidence_ids,
        "evidence": evidence,
        "credits_spent": result["credits_charged"],
        "status": status,
        "reason": reason,
        "dropped": dropped,
    }


def _seconds(value) -> float | None:
    """A finite time in seconds; None for anything else, nan and inf included."""
    try:
        seconds = None if value is None or isinstance(value, bool) else float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds is not None and math.isfinite(seconds) else None


def _segments(items) -> list[dict]:
    segments = []
    for item in items or []:
        item = _dict(item)
        text = _first(item, "text")
        start = _seconds(_first(item, "start_s", "start", "offset"))
        if not isinstance(text, str) or not text or start is None:
            continue
        end = _seconds(_first(item, "end_s", "end"))
        if end is None and (duration := _seconds(item.get("duration"))) is not None:
            end = round(start + duration, 3)
        segments.append({"start_s": start, "end_s": end, "text": text})
    return segments


def get_transcript(ctx: RunContext, client: SocialCrawlClient, evidence_id: str) -> dict:
    if evidence_id in ctx.transcripts:
        hit = ctx.transcripts[evidence_id]
        segments, spent, status, reason, cached = hit["segments"], 0.0, hit["status"], "", True
    else:
        _, route, url = _parent(ctx, evidence_id, TRANSCRIPT_ROUTES, "transcripts")
        result, event = _charged_call(ctx, client, "get_transcript", route, {"url": url}, ctx.credits_left(),
                                      evidence_id)
        status, spent, cached = result.get("status"), result["credits_charged"], False
        reason = str(result.get("reason") or "")
        items = (result.get("items") or []) if status in ITEM_STATUSES else []
        try:
            segments = _segments(items)
        except Exception:
            _set_status(ctx, event, "error")
            raise
        if items and not segments:
            status, reason = "schema_drift", f"{len(items)} transcript items arrived and none had text and a start time"
            _set_status(ctx, event, status)
        if segments:  # only a transcript with segments is kept, so an empty or failed fetch can be retried
            ctx.transcripts[evidence_id] = {"status": status, "segments": segments}
    return {
        "evidence_id": evidence_id,
        "segments": [{**s, "text": _fence(s["text"])} for s in segments],
        "credits_spent": spent,
        "status": status,
        "reason": reason,
        "cached": cached,
    }


MAX_QUESTION_CHARS = 500
# The stored thumbnail, duration and transcript of a post the warehouse holds, read once per watch_video. The
# post_date bound keeps the scan to Stage 1's 400 days, as the understand job's reads do.
STORED_VIDEO_SQL = (
    "SELECT p.thumbnail_url, p.duration_s, p.transcript FROM `ogilvy-trends-v2.intelligence_42_core.posts` AS p "
    "WHERE p.post_id = @post_id AND p.post_date >= @since LIMIT 1")
STORED_VIDEO_MAX_BYTES = 2_000_000_000


# Whether the post's creator is on the suppression list, read through L1's v_suppressed_creators as the creator
# checks in core/agent/skills.py and warehouse.py read it: by the post's stored creator_id, and by its handle matched
# to creators rows the way the view matches handles (X as x, trimmed, lower case, no @ or u/). known is 0 when neither
# finds the creator, so the check cannot say and watch_video refuses.
SUPPRESSED_SQL = (
    "WITH ids AS ("
    "SELECT p.creator_id FROM `ogilvy-trends-v2.intelligence_42_core.posts` AS p "
    "WHERE p.post_id = @post_id AND p.post_date >= @since AND p.creator_id IS NOT NULL "
    "UNION DISTINCT "
    "SELECT c.creator_id FROM `ogilvy-trends-v2.intelligence_42_core.creators` AS c "
    "WHERE CONCAT(IF(LOWER(TRIM(c.platform)) = 'twitter', 'x', LOWER(TRIM(c.platform))), ':', "
    "LOWER(REGEXP_REPLACE(TRIM(c.handle), r'^@*(u/)?', ''))) = @key AND c.creator_id IS NOT NULL) "
    "SELECT COUNTIF(i.creator_id IN (SELECT s.creator_id FROM "
    "`ogilvy-trends-v2.intelligence_42_core.v_suppressed_creators` AS s)) AS suppressed, COUNT(*) AS known "
    "FROM ids AS i")
UNCHECKED = "Whether {eid}'s creator is on the suppression list cannot be checked, so the post is not watched."


def _creator_cleared(ctx: RunContext, warehouse, evidence_id: str, record: dict) -> None:
    """Refused unless the post's creator is known and not suppressed. A check that fails or finds no creator
    refuses: a post 42 cannot check is never sent to the model."""
    from datetime import timedelta

    platform = str(record.get("platform") or "").strip().lower()
    handle = re.sub(r"^@*(u/)?", "", str(record.get("handle") or "").strip()).lower()
    key = f"{'x' if platform == 'twitter' else platform}:{handle}"
    if warehouse is None:
        raise Refused(UNCHECKED.format(eid=evidence_id))
    try:
        rows = warehouse.run(SUPPRESSED_SQL, {"post_id": evidence_id, "key": key,
                                              "since": ctx.as_of.date() - timedelta(days=402)},
                             STORED_VIDEO_MAX_BYTES)
        row = dict(rows[0])
        suppressed, known = int(row["suppressed"] or 0), int(row["known"] or 0)
    except Exception:
        raise Refused(UNCHECKED.format(eid=evidence_id)) from None
    if suppressed:
        raise Refused(f"{evidence_id}'s creator is suppressed, so the post is not watched.")
    if not known:
        raise Refused(UNCHECKED.format(eid=evidence_id))


class _VideoCalls:
    """The SocialCrawl client video.read_clip expects (call(route, params) -> status, items, credits_charged), on the
    agent's enrichment path: a transcript is get_transcript's call, cached in ctx.transcripts and logged under its
    step; screen text is logged under watch_video. A call the enrichment share or item cap refuses comes back
    refused, and the clip is read without that text."""

    def __init__(self, ctx: RunContext, client: SocialCrawlClient, evidence_id: str):
        self.ctx, self.client, self.evidence_id = ctx, client, evidence_id

    def call(self, route: str, params: dict):
        from types import SimpleNamespace

        transcript = route in TRANSCRIPT_ROUTES.values()
        hit = self.ctx.transcripts.get(self.evidence_id) if transcript else None
        if hit:
            return SimpleNamespace(status=hit["status"], items=hit["segments"], credits_charged=0.0)
        step = "get_transcript" if transcript else "watch_video"
        try:
            result, event = _charged_call(self.ctx, self.client, step, route, params, self.ctx.credits_left(),
                                          self.evidence_id)
        except Refused as e:
            return SimpleNamespace(status="refused", items=[], credits_charged=0.0, reason=str(e))
        status = result.get("status")
        items = (result.get("items") or []) if status in ITEM_STATUSES else []
        if transcript:
            try:
                segments = _segments(items)
            except Exception:
                _set_status(self.ctx, event, "error")
                raise
            if items and not segments:
                status = "schema_drift"
                _set_status(self.ctx, event, status)
            if segments:
                self.ctx.transcripts[self.evidence_id] = {"status": status, "segments": segments}
            items = segments
        return SimpleNamespace(status=status, items=items, credits_charged=result["credits_charged"])


def _stored_video(ctx: RunContext, warehouse, evidence_id: str) -> dict:
    """The post's stored thumbnail_url, duration_s and transcript, or nothing when the warehouse cannot say."""
    if warehouse is None:
        return {}
    from datetime import timedelta

    try:
        rows = warehouse.run(STORED_VIDEO_SQL, {"post_id": evidence_id,
                                                "since": ctx.as_of.date() - timedelta(days=402)},
                             STORED_VIDEO_MAX_BYTES)
    except Exception:
        return {}
    row = dict(rows[0]) if rows else {}
    return {k: row.get(k) for k in ("thumbnail_url", "duration_s", "transcript") if row.get(k) not in (None, "")}


class _SpendingModel:
    """The model, with each call's spend booked on the question: reserved in its model budget before the call and
    settled after it when the question has one, added to ctx.model_usd_extra otherwise. Without one, a call whose
    reserve (its input bound and full output allowance at the model's price) is over what the context's own
    max_budget_usd leaves after the research model's spend and earlier tool spend is refused before it is sent. A
    billed failure counts. Under a budget a 429 refusal gives its reserve back and is waited out, and a 5xx books its
    reserve and is tried once more, each try on a fresh reserve."""

    def __init__(self, ctx: RunContext, model, input_bound: int):
        self.ctx, self.model, self.input_bound = ctx, model, input_bound

    def complete_json(self, **kwargs):
        import time

        from core.agent.gemini_research import RETRY_WAIT_S, busy_refusal, server_error, wait_busy
        from core.agent.model_budget import BudgetRefused

        budget = getattr(self.ctx, "model_budget", None)
        busy = server_failed = 0
        while True:
            reservation = None
            if budget is not None:
                try:
                    reservation = budget.reserve(kwargs["model"], self.input_bound, kwargs["max_tokens"])
                except BudgetRefused as e:
                    raise Refused(f"The question's model budget has no room to watch a video ({e}).") from None
            else:
                self._check_lane(kwargs["model"], kwargs["max_tokens"])
            try:
                result = self.model.complete_json(**kwargs)
            except Exception as exc:
                if reservation is not None and busy_refusal(exc):
                    # A 429 for capacity billed nothing: the reserve goes back and the read is tried again after a
                    # short wait, on a fresh reserve, as Ask's other guarded calls are (gemini_research.BUSY_WAITS_S).
                    budget.release(reservation)
                    if wait_busy(busy, lambda: False):
                        busy += 1
                        continue
                    raise
                if reservation is not None and not server_failed and server_error(exc):
                    # A 5xx books its full reserve and one more try runs on a fresh one; the HTTP client makes no
                    # retry of its own under a budget, since that retry would run outside the reserve.
                    budget.settle(reservation, None, stop_unknown=False)
                    server_failed += 1
                    time.sleep(RETRY_WAIT_S)
                    continue
                self._spend(budget, reservation, getattr(exc, "usage", None))
                raise
            self._spend(budget, reservation, result[1] if isinstance(result, tuple) and len(result) == 2 else None)
            return result

    def _check_lane(self, model: str, max_tokens: int) -> None:
        from core.llm.provider import price_for, reserve_output

        cap = self.ctx.budget.get("max_budget_usd")
        if cap is None:
            return
        left = cap - self.ctx.research_usd - self.ctx.model_usd_extra
        try:
            price = price_for(model)
            reserve = (self.input_bound * price["input"] + reserve_output(model, max_tokens) * price["output"]) / 1e6
        except Exception:  # a call that cannot be priced cannot be shown to fit, so it is not sent
            raise Refused("The model budget for this research cannot price a video read, so it is not sent.") from None
        if reserve > left:
            raise Refused(f"The model budget for this research has USD {max(left, 0.0):.4f} left, under the USD "
                          f"{reserve:.4f} a video read reserves.")

    def _spend(self, budget, reservation, usage) -> None:
        if reservation is not None:
            budget.settle(reservation, usage if isinstance(usage, dict) else None)
        elif isinstance(usage, dict) and isinstance(usage.get("usd"), (int, float)) and usage["usd"] > 0:
            self.ctx.model_usd_extra += float(usage["usd"])


def _fenced(value):
    if isinstance(value, list):
        return [_fence(v) for v in value]
    return _fence(value) if value else ""


def watch_video(ctx: RunContext, client: SocialCrawlClient, evidence_id: str, question: str, *, warehouse=None,
                model=None, fetch_bytes=None) -> dict:
    """Read one video post with the question in mind (core/understand/video.read_clip): a YouTube clip by its link,
    any other by its stored thumbnail, with the transcript and, on TikTok, the screen text. Their credits come from
    the enrichment share, and the post counts toward MAX_ENRICHED, as get_transcript's do. A post whose creator is
    suppressed, or cannot be checked, is refused before anything is spent (_creator_cleared). The model spend goes
    through the question's model budget when it has one and otherwise into ctx.model_usd_extra, only while the context's
    own max_budget_usd can still cover the read (_SpendingModel).
    Every text the model wrote goes back fenced, since it describes untrusted content."""
    from core.llm.provider import provider
    from core.understand import video

    record = ctx.evidence.get(evidence_id)
    if record is None:
        raise Refused(f"Evidence id {str(evidence_id)[:80]!r} was not seen in this run.")
    if record.get("parent_id"):
        raise Refused(f"{evidence_id} is a comment; watch_video reads video posts only.")
    platform = str(record.get("platform") or "").lower()
    platform = PLATFORM_ALIASES.get(platform, platform)
    if platform not in video.VIDEO_PLATFORMS:
        raise Refused(f"{evidence_id} is not a video post; watch_video reads TikTok, YouTube and Instagram posts.")
    if not record.get("url"):
        raise Refused(f"{evidence_id} has no URL, so it cannot be watched.")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise Refused(f"question must be one plain question of at most {MAX_QUESTION_CHARS} characters.")
    if provider() != "gemini":
        raise Refused("Video reading runs only on the gemini model family, and this question runs on another model.")
    from core.config import caps

    daily = caps.video_daily()
    if daily["clips"] <= 0 or daily["credits"] <= 0:  # either VIDEO_DAILY number at zero switches it off, Ask too
        raise Refused(f"Video reading is switched off: VIDEO_DAILY clips is {daily['clips']} and credits is "
                      f"{daily['credits']}, and both must be above 0.")
    if evidence_id not in ctx.enriched and len(ctx.enriched) >= MAX_ENRICHED:
        raise Refused(f"Enrichment is for the top {MAX_ENRICHED} items only, and {MAX_ENRICHED} items are enriched.")
    _creator_cleared(ctx, warehouse, evidence_id, record)

    post = {"post_id": evidence_id, "platform": platform, "url": record["url"], "text": record.get("text"),
            "market": record.get("market"), **_stored_video(ctx, warehouse, evidence_id)}
    hit = ctx.transcripts.get(evidence_id)
    if hit and not post.get("transcript"):
        post["transcript"] = "\n".join(f"[{s['start_s']:g}s] {s['text']}" for s in hit["segments"])
    if model is None:
        from core.llm.gemini import GeminiModel

        # Under a question's model budget the client makes no retry of its own: _SpendingModel retries on a fresh
        # reserve, so no attempt runs outside one.
        model = GeminiModel(retries=0 if getattr(ctx, "model_budget", None) is not None else 1)
    read = video.read_clip(post, _SpendingModel(ctx, model, video.est_tokens(post)),
                           _VideoCalls(ctx, client, evidence_id), question=question,
                           fetch_bytes=fetch_bytes or video.fetch_image)
    ctx.enriched.add(evidence_id)
    out = read["output"] or {}
    result = {"evidence_id": evidence_id, "status": "cap_reached" if read["stopped"] else "ok",
              "read_from": read["read_from"], "credits_spent": read["credits"], "observations": []}
    if read["stopped"]:
        return result
    screen = (read["row"] or {}).get("screen_text")
    result.update({"format": out.get("format"), "on_screen_text": _fenced(screen),
                   **{k: _fenced(out.get(k)) for k in ("hook", "setting", "people", "brands", "sound", "edit_style")},
                   "observations": [{"t_s": o["t_s"], "text": _fence(o["text"])}
                                    for o in out.get("observations") or []]})
    return result
