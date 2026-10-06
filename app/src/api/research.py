"""Research brief evidence orchestrator (persona-driven, seeds-first)."""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from . import bq, persona_registry, synth

logger = logging.getLogger("listening_post.research")

THIN_FLOOR = persona_registry.THIN_FLOOR
JOB_TIMEOUT_SEC = 90.0
_GATHER_WORKERS = 8
_VOICE_POSTS_PER_TOPIC = 8
_VOICE_COMMENTS_PER_TOPIC = 5

# The staging profile lets only the reviewed general question engine call the
# model, so the brief writer is off there by contract. Readers get these words,
# never the refusal code the synthesis guard raises.
BRIEF_WRITING_UNAVAILABLE = "brief_writing_unavailable"
BRIEF_WRITING_UNAVAILABLE_MESSAGE = (
    "Cited briefs cannot be written on staging. Staging lets only Ask use the "
    "model, so the brief writer is switched off here. Ask your question in Ask "
    "to get a cited answer you can download as HTML or PDF."
)
BEHAVIOUR_SCAN_OFF = "behaviour_scan_off"
BEHAVIOUR_SCAN_OFF_MESSAGE = (
    "The behaviour scan is switched off on this server. Pick topics manually "
    "to choose what the brief covers."
)
RESEARCH_FAILED_MESSAGE = "Research generation failed."


def brief_writing_available() -> bool:
    return not synth.legacy_generation_refused()


def brief_via_question() -> bool:
    """True where a cited brief comes from the reviewed question engine.

    The Build brief page then submits one question through Ask's start path,
    under the same reservation, pricing policy and passcode as Ask."""
    return synth.legacy_generation_refused()


def research_failure(exc: BaseException, default: str = RESEARCH_FAILED_MESSAGE) -> dict:
    """The reader facing failure for an exception raised inside a research job.

    Exception text is internal: it can carry refusal codes, paths or vendor
    detail. Only a persona that cannot be resolved has a reason written for a
    reader, and the staging refusal has its own plain words."""
    if isinstance(exc, RuntimeError) and str(exc) == synth.LEGACY_GENERATION_REFUSED:
        return {
            "code": BRIEF_WRITING_UNAVAILABLE,
            "message": BRIEF_WRITING_UNAVAILABLE_MESSAGE,
        }
    if isinstance(exc, PersonaResolveError) and str(exc):
        return {"message": str(exc)}
    return {"message": default}


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 120) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return max(lo, min(int(raw), hi))
    except ValueError:
        return default


def _post_gather_limit(mks: list[str]) -> int:
    n = max(1, len(mks))
    return min(120, 80 + (n - 1) * 20)


def _ref_dedupe_key(ref: dict) -> str:
    rid = str(ref.get("id") or ref.get("content_id") or "").strip()
    if rid:
        return f"id:{rid}"
    url = str(ref.get("url") or "").strip().lower()
    if url.startswith(("http://", "https://")):
        return f"url:{url}"
    return (
        f"fallback:{ref.get('ref_type')}|{ref.get('market')}|{ref.get('query_group')}|"
        f"{ref.get('author_handle')}|{str(ref.get('text') or '')[:160]}"
    )

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None


class PersonaResolveError(ValueError):
    pass


def _require_persona(persona_id: str) -> dict:
    persona = persona_registry.get_persona(persona_id)
    if not persona:
        raise PersonaResolveError(f"Unknown or disabled persona: {persona_id}")
    return persona


def _resolve_product_frame(product_frame: str | None, persona: dict) -> str:
    selected = persona.get("product_frame") if product_frame is None else product_frame
    return str(selected or "").strip()


def _deadline(start: float) -> bool:
    return (time.monotonic() - start) >= JOB_TIMEOUT_SEC


_batch_progress_meta: dict = {}


def _emit_progress(progress_key: tuple | None, status: str, phase: str = "gathering") -> None:
    if not progress_key:
        return
    payload: dict = {"pending": True, "phase": phase, "status": status}
    meta = _batch_progress_meta.get(progress_key)
    if meta:
        payload.update(meta)
    synth.cache_set(
        progress_key,
        payload,
        ttl=600 if meta else 120,
    )


def _timed(label: str, start: float, t0: float) -> None:
    logger.info("research %s %.2fs (elapsed %.2fs)", label, time.monotonic() - t0, time.monotonic() - start)


def _topic_matches_anchor(name: str, anchors: list) -> bool:
    blob = (name or "").lower()
    if fuzz is not None:
        return any(fuzz.partial_ratio(a, blob) >= 75 for a in anchors if a)
    return any(a in blob for a in anchors if a)


def _assign_ref_ids(refs: list) -> list:
    out = []
    idx = 0
    for r in refs:
        if not persona_registry.ref_has_substance(r):
            continue
        idx += 1
        row = dict(r)
        row["ref_id"] = f"ref_{idx}"
        out.append(row)
    return out


def _seed_candidates(seeds: list, persona: dict) -> list[dict]:
    out = []
    persona_tags = {str(t).lower() for t in (persona.get("seed_tags") or [])}
    for s in seeds:
        markets = [str(m).lower() for m in (s.get("markets") or [])]
        blob = " ".join(
            [
                str(s.get("behaviour") or ""),
                str(s.get("the_shift") or ""),
                str(s.get("evidence") or ""),
            ]
        ).lower()
        tag_hit = bool(persona_tags) and any(t in blob for t in persona_tags)
        for mk in markets or ["all"]:
            out.append(
                {
                    "ref_type": "seed",
                    "market": mk,
                    "query_group": "",
                    "behaviour": str(s.get("behaviour") or ""),
                    "the_shift": str(s.get("the_shift") or ""),
                    "text": bq.truncate_words(
                        " ".join(
                            [
                                str(s.get("behaviour") or ""),
                                str(s.get("the_shift") or ""),
                            ]
                        ),
                        220,
                    ),
                    "seed_tags": list(persona_tags) if tag_hit else [],
                    "evidence_tier": 1,
                    "bq_trace": {"table": "seed_insights", "trend_date": str(s.get("trend_date") or "")},
                }
            )
    return out


def _brief_candidates(rows: list, persona: dict) -> list[dict]:
    out = []
    for r in rows:
        qg = str(r.get("query_group") or "")
        headline = str(r.get("headline") or "")
        synthesis = str(r.get("trend_synthesis") or "")
        cultural = str(r.get("cultural_context") or "")
        synth_text = " ".join([headline, synthesis, cultural])
        out.append(
            {
                "ref_type": "brief",
                "market": str(r.get("market") or "").lower(),
                "query_group": qg,
                "headline": headline,
                "trend_synthesis": synthesis,
                "cultural_context": cultural,
                "text": bq.truncate_words(synth_text, 280),
                "trend_score": float(r.get("trend_score") or 0),
                "velocity_score": float(r.get("velocity_score") or 0),
                "search_velocity_score": float(r.get("search_velocity_score") or 0),
                "item_count": int(r.get("item_count") or 0),
                "evidence_tier": 1,
                "bq_trace": {"table": "trend_analysis", "row_key": qg},
            }
        )
    return out


def _post_metrics(row: dict) -> dict:
    """Per-post performance metrics from enriched_content, only real values.

    likes / shares / views / comments are the granular counters the connectors
    persist; engagement_total is their sum. Zero or missing counters are
    dropped so a Reddit post (no views/shares) shows only what it has."""
    out: dict = {}
    for key in ("likes", "shares", "views", "comments"):
        try:
            val = int(float(row.get(key) or 0))
        except (TypeError, ValueError):
            val = 0
        if val > 0:
            out[key] = val
    try:
        eng = int(float(row.get("engagement_total") or row.get("engagement") or 0))
    except (TypeError, ValueError):
        eng = 0
    if eng > 0:
        out["engagement_total"] = eng
    return out


def _posts_candidates(items: list, trend_date) -> list[dict]:
    out = []
    for it in items:
        text = str(it.get("text") or "")
        groups = it.get("topic_groups") or []
        topic_group = str(it.get("topic_group") or (groups[0] if groups else ""))
        vk = str(it.get("voice_kind") or bq._voice_kind(it.get("content_type")) or "post")
        thread_comments = it.get("thread_comments") if isinstance(it.get("thread_comments"), list) else []
        ref: dict = {
            "id": str(it.get("id") or ""),
            "ref_type": "comment" if vk == "comment" else "post",
            "market": str(it.get("market") or "").lower(),
            "query_group": topic_group,
            "platform": str(it.get("platform") or ""),
            "text": bq.truncate_words(text, 220),
            "url": str(it.get("url") or ""),
                "engagement_total": int(it.get("engagement_total") or 0),
                "metrics": _post_metrics(it),
                "author_handle": str(it.get("author_handle") or ""),
                "content_type": str(it.get("content_type") or ""),
                "voice_kind": vk,
                "evidence_tier": 2,
                "receipt": {
                "platform": it.get("platform"),
                "handle": it.get("author_handle"),
                "published_at": it.get("published_at"),
            },
            "bq_trace": {"table": "enriched_content", "trend_date": str(trend_date or "")},
        }
        if vk != "comment" and thread_comments:
            ref["thread_comments"] = thread_comments
            ref["thread_size"] = len(thread_comments)
        out.append(ref)
    return out


def _voice_rows_with_threads(voice_rows: list, *, per_post: int = 10) -> list:
    """Attach BQ comment threads to posts; keep orphan flat comments as separate rows."""
    posts = [
        r
        for r in (voice_rows or [])
        if (r.get("voice_kind") or bq._voice_kind(r.get("content_type"))) != "comment"
    ]
    flat_comments = [
        r for r in (voice_rows or []) if (r.get("voice_kind") or "") == "comment"
    ]
    threads = bq.fetch_comment_threads_for_posts(posts, per_post=per_post)

    attached_ids: set[str] = set()
    for post in posts:
        pid = str(post.get("id") or "")
        thread = threads.get(pid) or []
        post["thread_comments"] = thread
        for row in thread:
            handle = str(row.get("handle") or "")
            text_key = str(row.get("text") or "")[:120]
            attached_ids.add(f"{handle}|{text_key}")

    orphans: list = []
    for comment in flat_comments:
        cid = str(comment.get("id") or "").strip()
        if cid and any(
            str(c.get("id") or "") == cid
            for thread in threads.values()
            for c in (thread or [])
        ):
            continue
        handle = str(comment.get("author_handle") or comment.get("handle") or "")
        text_key = str(comment.get("text") or "")[:120]
        if f"{handle}|{text_key}" in attached_ids:
            continue
        orphans.append(comment)

    return posts + orphans


def _merge_voice_rows(*row_sets: list) -> list:
    merged: list = []
    seen: set[str] = set()
    for rows in row_sets:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            key = str(row.get("id") or row.get("url") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            merged.append(row)
    return merged


def _trends_candidates(rows: list) -> list[dict]:
    out = []
    for r in rows:
        qg = str(r.get("query_group") or "")
        sv = float(r.get("search_velocity_score") or 0)
        out.append(
            {
                "ref_type": "google_trends_rising",
                "market": str(r.get("market") or "").lower(),
                "query_group": qg,
                "headline": f"Rising search: {bq.topic_label(qg)}",
                "text": f"Search velocity {sv:.2f} on {bq.topic_label(qg)}",
                "search_velocity_score": sv,
                "trend_score": float(r.get("trend_score") or 0),
                "evidence_tier": 1,
            }
        )
    return out


def _lexicon_candidates(rows: list, persona: dict) -> list[dict]:
    anchors = persona.get("anchor_tokens") or []
    out = []
    for r in rows:
        term = str(r.get("term") or r.get("slang") or "")
        if not any(a in term.lower() for a in anchors):
            continue
        n = int(r.get("n") or 0)
        detail_lines = [f'Slang term: "{term}"']
        if n:
            detail_lines.append(f"{n} occurrences in corpus")
        metrics = {"occurrences": n} if n else {}
        text = f'"{term}"' + (f" ({n} uses)" if n else "")
        out.append(
            {
                "ref_type": "lexicon",
                "market": str(r.get("market") or "").lower(),
                "term": term,
                "text": text,
                "detail_lines": detail_lines,
                "metrics": metrics,
            }
        )
    return out


def _parallel_gather(
    persona: dict,
    mks: list[str],
    td,
    start: float,
    degraded: list,
    progress_key: tuple | None,
) -> list[dict]:
    """Fetch seeds, digest, briefs, trends, posts and lexicon in parallel.

    Tier 1 (engine signal): brief rows, seed behaviours, digest through_line, rising search.
    Tier 2 (voice proof): posts linked to focus topics as illustrative receipts.
    """
    qgroups = persona.get("query_groups") or []
    anchors = persona.get("anchor_tokens") or []
    post_limit = _post_gather_limit(mks)
    per_posts = _env_int("RESEARCH_VOICE_POSTS_PER_TOPIC", _VOICE_POSTS_PER_TOPIC, lo=1, hi=12)
    per_comments = _env_int("RESEARCH_VOICE_COMMENTS_PER_TOPIC", _VOICE_COMMENTS_PER_TOPIC, lo=1, hi=10)
    candidates: list[dict] = []

    _emit_progress(progress_key, "gathering_bq")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=_GATHER_WORKERS) as pool:
        fut_seeds = pool.submit(bq.fetch_research_seeds, mks, trend_date=td)
        fut_digest = pool.submit(bq.fetch_research_digest, trend_date=td)
        fut_briefs = pool.submit(bq.fetch_topic_briefs_for_markets, qgroups, mks, td)
        fut_trends = pool.submit(bq.fetch_rising_search_terms_for_markets, mks, anchors, td)
        fut_voice = pool.submit(
            bq.fetch_posts_and_comments_for_topics,
            mks,
            qgroups,
            per_topic_posts=per_posts,
            per_topic_comments=per_comments,
        )
        fut_posts = pool.submit(bq.fetch_research_posts, mks, qgroups, limit=post_limit)
        fut_lex = pool.submit(bq.fetch_lexicon_rows_for_markets, mks)

        seeds = fut_seeds.result()
        digest = fut_digest.result()
        brief_rows = fut_briefs.result()
        trend_rows = fut_trends.result()
        voice_rows = fut_voice.result()
        post_rows = fut_posts.result()
        lex_rows = fut_lex.result()
        per_thread = _env_int("RESEARCH_THREAD_COMMENTS_PER_POST", 10, lo=1, hi=10)
        voice_rows = _voice_rows_with_threads(voice_rows, per_post=per_thread)

        _timed("bq_batch", start, t0)
        candidates.extend(_seed_candidates(seeds, persona))

        if digest:
            candidates.append(
                {
                    "ref_type": "digest",
                    "market": "all",
                    "text": bq.truncate_words(
                        str(digest.get("through_line") or digest.get("summary_text") or ""), 220
                    ),
                    "headline": str(digest.get("through_line") or ""),
                    "note": str(digest.get("seed_recommend") or ""),
                    "metrics": {"seed_recommend": digest.get("seed_recommend")},
                    "evidence_tier": 1,
                    "bq_trace": {"table": "daily_summary", "trend_date": str(td or "")},
                }
            )

        candidates.extend(_brief_candidates(brief_rows, persona))
        candidates.extend(_trends_candidates(trend_rows))
        candidates.extend(_posts_candidates(_merge_voice_rows(voice_rows, post_rows), td))
        candidates.extend(_lexicon_candidates(lex_rows, persona))

        if _deadline(start):
            degraded.append("job_timeout")
            return candidates

    return candidates


def focus_payload_for_behaviour(behaviour: dict) -> tuple[list[str], str]:
    """Build focus_query_groups and focus_note for one approved behaviour row."""
    qg = str(behaviour.get("query_group") or "").strip()
    groups = [qg] if qg else []
    base = str(behaviour.get("behaviour") or behaviour.get("signal_topic") or "").strip()
    note = str(behaviour.get("note") or "").strip()
    if base and note:
        focus_note = f"{base} [note: {note}]"
    else:
        focus_note = base or note
    return groups, focus_note


def behaviour_brief_label(behaviour: dict, index: int = 0) -> str:
    mk = str(behaviour.get("market") or "").upper()
    topic = str(behaviour.get("signal_topic") or behaviour.get("behaviour") or f"Brief {index + 1}")
    return f"{mk} · {topic}" if mk else topic


def _refs_from_scan_examples(
    examples: list | None,
    market: str,
    query_group: str,
) -> list[dict]:
    """Turn behaviour-scan proof rows into gather candidates."""
    out: list[dict] = []
    mk = str(market or "").lower()
    qg = str(query_group or "").strip()
    for ex in (examples or [])[:3]:
        if not isinstance(ex, dict):
            continue
        text = str(ex.get("text") or "").strip()
        if not text:
            continue
        vk = str(ex.get("voice_kind") or bq._voice_kind(ex.get("content_type")) or "post")
        out.append(
            {
                "ref_type": "comment" if vk == "comment" else "post",
                "market": mk,
                "query_group": qg,
                "text": text,
                "url": str(ex.get("url") or ""),
                "platform": str(ex.get("platform") or ""),
                "author_handle": str(ex.get("handle") or ex.get("author_handle") or ""),
                "handle": str(ex.get("handle") or ex.get("author_handle") or ""),
                "content_type": str(ex.get("content_type") or ""),
                "voice_kind": vk,
                "relevance_score": 0.88,
                "source": "behaviour_scan",
            }
        )
    return out


def build_research_evidence(
    persona_id: str,
    markets: list[str] | None = None,
    product_frame: str | None = None,
    *,
    trend_date=None,
    progress_key: tuple | None = None,
    focus_query_groups: list[str] | None = None,
    focus_note: str = "",
    focus_examples: list | None = None,
    consolidated: bool = False,
) -> dict[str, Any]:
    start = time.monotonic()
    persona = _require_persona(persona_id)
    focus_groups = [str(g).strip() for g in (focus_query_groups or []) if str(g).strip()]
    if focus_groups:
        # Scope the gather to the approved behaviours (behaviour scan step). The
        # persona still supplies anchor tokens and lane rules; only the topic
        # allowlist narrows so the brief centres on what was signed off.
        persona = {**persona, "query_groups": focus_groups}
    mks = [m.lower() for m in (markets or persona.get("default_markets") or []) if m]
    if not mks:
        mks = list(persona.get("default_markets") or [])
    degraded: list[str] = []
    td = trend_date or bq.latest_trend_date()

    _emit_progress(progress_key, "gathering_seeds")
    candidates = _parallel_gather(persona, mks, td, start, degraded, progress_key)
    if focus_groups and focus_examples:
        scan_mk = mks[0] if len(mks) == 1 else ""
        candidates = _refs_from_scan_examples(focus_examples, scan_mk, focus_groups[0]) + candidates

    refs = _assign_ref_ids(
        persona_registry.rank_and_filter(candidates, persona, consolidated=consolidated)
    )
    gate = persona_registry.quality_gate(refs, persona, focus_scoped=bool(focus_groups))
    if td is not None and hasattr(td, "isoformat"):
        trend_date_str = td.isoformat()
    elif td:
        trend_date_str = str(td)
    else:
        fallback = bq.latest_trend_date()
        trend_date_str = fallback.isoformat() if fallback is not None and hasattr(fallback, "isoformat") else str(fallback or "")

    logger.info(
        "research evidence persona=%s markets=%s refs=%d elapsed=%.2fs degraded=%s",
        persona_id,
        mks,
        len(refs),
        time.monotonic() - start,
        degraded or None,
    )

    return {
        "persona_id": persona.get("id") or persona_id,
        "persona_label": persona.get("label") or persona_id,
        "markets": mks,
        "product_frame": _resolve_product_frame(product_frame, persona),
        "trend_date": trend_date_str,
        "query_groups": list(persona.get("query_groups") or []),
        "focus_query_groups": focus_groups,
        "focus_note": (focus_note or "").strip(),
        "refs": refs,
        "signal_quality": gate.get("level"),
        "quality_gate": gate,
        "quality": gate,
        "degraded_reasons": degraded,
        "ref_count": len(refs),
    }


def build_research_doc(evidence: dict[str, Any]) -> dict[str, Any] | None:
    refs = evidence.get("refs") or []
    gate = evidence.get("quality_gate") or {}
    level = gate.get("level") or "thin"

    if level == "thin":
        thin = synth.build_thin_research_doc(evidence)
        return thin

    if not gate.get("allow_inference"):
        return synth.build_thin_research_doc(evidence)

    doc_json = synth.synthesize_research(evidence)
    if doc_json is None:
        return None

    if not gate.get("allow_positioning"):
        doc_json.pop("inference", None)
    if not gate.get("allow_activation"):
        doc_json.pop("activation", None)

    doc_json["_refs"] = refs
    markdown = synth.render_research_markdown(doc_json)
    return {
        "json": doc_json,
        "markdown": markdown,
        "sources": refs,
        "signal_quality": level,
    }


def run_research_job(
    persona_id: str,
    markets: list[str] | None,
    product_frame: str | None,
    *,
    progress_key: tuple | None = None,
    focus_query_groups: list[str] | None = None,
    focus_note: str = "",
    focus_examples: list | None = None,
) -> dict:
    evidence = build_research_evidence(
        persona_id,
        markets,
        product_frame,
        progress_key=progress_key,
        focus_query_groups=focus_query_groups,
        focus_note=focus_note,
        focus_examples=focus_examples,
    )
    _emit_progress(progress_key, "synthesizing", phase="synthesizing")
    doc = build_research_doc(evidence)
    status = "completed"
    if evidence.get("degraded_reasons"):
        status = "completed_degraded"
    if doc is None:
        if not brief_writing_available():
            return {
                "status": "failed",
                "error": True,
                "code": BRIEF_WRITING_UNAVAILABLE,
                "message": BRIEF_WRITING_UNAVAILABLE_MESSAGE,
            }
        return {
            "status": "failed",
            "error": True,
            "message": "Synthesis unavailable for this evidence set.",
        }
    return {
        "status": status,
        "degraded_reason": ", ".join(evidence.get("degraded_reasons") or []) or None,
        "doc": doc,
        "evidence_graph": evidence,
        "signal_quality": evidence.get("signal_quality"),
        "artifact_id": None,
    }


def persist_job_result(result: dict, parent_artifact_id: str | None = None) -> str:
    evidence = result.get("evidence_graph") or {}
    doc = result.get("doc") or {}
    row = {
        "created_at": datetime.now(UTC).isoformat(),
        "persona_id": evidence.get("persona_id"),
        "persona_label": evidence.get("persona_label"),
        "markets": evidence.get("markets"),
        "product_frame": evidence.get("product_frame"),
        "trend_date": evidence.get("trend_date"),
        "status": result.get("status"),
        "quality": evidence.get("quality") or {"level": evidence.get("signal_quality")},
        "signal_quality": result.get("signal_quality"),
        "evidence": evidence.get("refs") or [],
        "synthesis": doc.get("json") or {},
        "markdown": doc.get("markdown") or "",
        "doc_json": doc.get("json"),
        "doc_markdown": doc.get("markdown"),
        "evidence_graph": evidence,
        "degraded_reason": result.get("degraded_reason") or "",
        "parent_artifact_id": parent_artifact_id,
    }
    aid = bq.insert_research_artifact(row)
    result["artifact_id"] = aid
    return aid


def refine_research_artifact(artifact: dict, instruction: str) -> dict | None:
    refined = synth.refine_research_prose(artifact, instruction)
    if refined is None:
        return None
    refined["_refs"] = (artifact.get("evidence_graph") or {}).get("refs") or []
    out = dict(artifact)
    md = synth.render_research_markdown(refined)
    out["doc_json"] = refined
    out["doc_markdown"] = md
    out["synthesis"] = refined
    out["markdown"] = md
    out["refined"] = int(artifact.get("refined") or 0) + 1
    out["parent_artifact_id"] = artifact.get("artifact_id")
    return out


def _refs_from_voice_rows(
    rows: list | None,
    market: str,
    query_group: str,
) -> list[dict]:
    """42 phase R2b voice rows as evidence refs for consolidated docs and packs."""
    out: list[dict] = []
    mk = str(market or "").lower()
    qg = str(query_group or "").strip()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("title") or "").strip()
        if not text:
            continue
        vk = str(row.get("voice_kind") or bq._voice_kind(row.get("content_type")) or "post")
        ref: dict = {
            "id": str(row.get("id") or ""),
            "ref_type": "comment" if vk == "comment" else "post",
            "market": mk or str(row.get("market") or "").lower(),
            "query_group": qg or str(row.get("topic_group") or ""),
            "text": bq.truncate_words(text, 220),
            "url": str(row.get("url") or ""),
            "platform": str(row.get("platform") or ""),
            "author_handle": str(row.get("author_handle") or row.get("handle") or ""),
            "engagement_total": int(row.get("engagement_total") or row.get("engagement") or 0),
            "metrics": _post_metrics(row),
            "content_type": str(row.get("content_type") or ""),
            "voice_kind": vk,
            "evidence_tier": 2,
            "source": "voice_fetch",
        }
        thread_comments = row.get("thread_comments") if isinstance(row.get("thread_comments"), list) else []
        if vk != "comment" and thread_comments:
            ref["thread_comments"] = thread_comments
            ref["thread_size"] = len(thread_comments)
        out.append(ref)
    return out


def _topic_ref_key(ref: dict) -> str:
    return str(ref.get("query_group") or "").strip().lower()


def _order_section_refs(
    engine_refs: list,
    voice_refs: list,
    query_group: str,
    market: str,
) -> list[dict]:
    """Per behaviour: brief row + seed match + search + voice sample, not random posts."""
    qg = query_group.strip().lower()
    mk = market.strip().lower()
    briefs = [
        r
        for r in engine_refs
        if r.get("ref_type") == "brief" and (_topic_ref_key(r) == qg or str(r.get("market") or "").lower() == mk)
    ]
    digests = [r for r in engine_refs if r.get("ref_type") == "digest"]
    seeds = [r for r in engine_refs if r.get("ref_type") == "seed"]
    searches = [
        r
        for r in engine_refs
        if r.get("ref_type") == "google_trends_rising" and (_topic_ref_key(r) == qg or not qg)
    ]
    lexicon = [r for r in engine_refs if r.get("ref_type") == "lexicon"]
    voice = list(voice_refs or [])
    voice_posts = [r for r in voice if str(r.get("ref_type") or "") == "post"]
    voice_orphans = [r for r in voice if str(r.get("ref_type") or "") == "comment"]
    voice_posts.sort(
        key=lambda r: (
            -(len(r.get("thread_comments") or [])),
            -(int(r.get("engagement_total") or 0)),
        )
    )
    voice = voice_posts + voice_orphans

    ordered: list[dict] = []
    seen: set[str] = set()

    def _add(row: dict) -> None:
        key = _ref_dedupe_key(row)
        if key in seen:
            return
        seen.add(key)
        ordered.append(row)

    for row in briefs[:8]:
        _add(row)
    for row in digests[:1]:
        _add(row)
    for row in seeds[:5]:
        _add(row)
    for row in searches[:3]:
        _add(row)
    for row in lexicon[:2]:
        _add(row)
    for row in voice[:15]:
        _add(row)
    for row in engine_refs:
        _add(row)
    for row in voice:
        _add(row)
    return ordered


def _merge_section_refs(evidence_refs: list, voice_refs: list) -> list[dict]:
    """Combine gather refs with voice rows; dedupe by BQ id or URL, not text prefix."""
    merged = list(evidence_refs or [])
    seen = {_ref_dedupe_key(r) for r in merged}
    for r in voice_refs:
        key = _ref_dedupe_key(r)
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)
    return merged


def build_consolidated_evidence(
    persona_id: str,
    behaviours: list[dict],
    markets: list[str] | None = None,
    product_frame: str | None = None,
    *,
    trend_date=None,
    progress_key: tuple | None = None,
    seo_notes: str = "",
) -> dict[str, Any]:
    """Gather scoped evidence per approved behaviour for one consolidated doc."""
    start = time.monotonic()
    persona = _require_persona(persona_id)
    rows = [b for b in (behaviours or []) if isinstance(b, dict)][:12]
    if not rows:
        raise PersonaResolveError("At least one approved behaviour is required")

    mks = [m.lower() for m in (markets or []) if m]
    if not mks:
        mks = sorted({str(b.get("market") or "").lower() for b in rows if b.get("market")})
    if not mks:
        mks = list(persona.get("default_markets") or [])

    frame = _resolve_product_frame(product_frame, persona)
    degraded: list[str] = []
    behaviour_sections: list[dict] = []
    all_refs: list[dict] = []

    for i, behaviour in enumerate(rows):
        _emit_progress(progress_key, f"gathering_behaviour_{i + 1}")
        groups, note = focus_payload_for_behaviour(behaviour)
        mk = str(behaviour.get("market") or "").lower()
        qg = str(behaviour.get("query_group") or "").strip()
        scan_mks = [mk] if mk in ("za", "ng", "ke") else mks
        examples = behaviour.get("examples") if isinstance(behaviour.get("examples"), list) else []

        try:
            ev = build_research_evidence(
                persona_id,
                scan_mks,
                frame,
                trend_date=trend_date,
                focus_query_groups=groups or None,
                focus_note=note,
                focus_examples=examples or None,
                consolidated=True,
            )
        except Exception:
            logger.exception("consolidated evidence failed behaviour=%s", behaviour.get("id"))
            degraded.append(f"behaviour_{i + 1}")
            ev = {"refs": [], "quality_gate": {"level": "thin"}, "degraded_reasons": ["gather_failed"]}

        voice_rows = (
            bq.fetch_posts_and_comments_for_topics(
                scan_mks,
                [qg] if qg else groups,
                per_topic_posts=_env_int("RESEARCH_VOICE_POSTS_PER_TOPIC", _VOICE_POSTS_PER_TOPIC, lo=1, hi=12),
                per_topic_comments=_env_int("RESEARCH_VOICE_COMMENTS_PER_TOPIC", _VOICE_COMMENTS_PER_TOPIC, lo=1, hi=10),
            )
            if (qg or groups)
            else []
        )
        per_thread = _env_int("RESEARCH_THREAD_COMMENTS_PER_POST", 10, lo=1, hi=10)
        voice_rows = _voice_rows_with_threads(voice_rows, per_post=per_thread)
        voice_posts = [r for r in voice_rows if (r.get("voice_kind") or "post") != "comment"]
        voice_comments = [r for r in voice_rows if (r.get("voice_kind") or "") == "comment"]
        voice_refs = _refs_from_voice_rows(voice_rows, mk, qg)
        engine_refs = [r for r in (ev.get("refs") or []) if r.get("evidence_tier") != 2]
        section_refs = _order_section_refs(engine_refs, voice_refs, qg, mk)
        if not section_refs:
            section_refs = _merge_section_refs(ev.get("refs") or [], voice_refs)
        ref_start = len(all_refs) + 1
        all_refs.extend(section_refs)
        ref_end = len(all_refs)

        metric = behaviour.get("metric") if isinstance(behaviour.get("metric"), dict) else {}
        behaviour_sections.append(
            {
                "behaviour_id": behaviour.get("id") or f"{mk}:{qg}",
                "market": mk,
                "signal_topic": behaviour.get("signal_topic") or behaviour_brief_label(behaviour, i),
                "behaviour": behaviour.get("behaviour") or "",
                "query_group": qg,
                "note": behaviour.get("note") or "",
                "metric": metric,
                "examples": examples,
                "voice_posts": voice_posts,
                "voice_comments": voice_comments,
                "refs": section_refs,
                "ref_start": ref_start,
                "ref_end": ref_end,
                "signal_quality": ev.get("signal_quality"),
                "quality_gate": ev.get("quality_gate") or {},
            }
        )
        degraded.extend(ev.get("degraded_reasons") or [])

    all_refs = _assign_ref_ids(all_refs)
    offset = 0
    for section in behaviour_sections:
        n = len(section.get("refs") or [])
        section["ref_start"] = offset + 1
        section["ref_end"] = offset + n
        offset += n

    td = trend_date or bq.latest_trend_date()
    if td is not None and hasattr(td, "isoformat"):
        trend_date_str = td.isoformat()
    else:
        trend_date_str = str(td or "")

    logger.info(
        "consolidated evidence persona=%s behaviours=%d refs=%d elapsed=%.2fs",
        persona_id,
        len(behaviour_sections),
        len(all_refs),
        time.monotonic() - start,
    )

    return {
        "consolidated": True,
        "persona_id": persona.get("id") or persona_id,
        "persona_label": persona.get("label") or persona_id,
        "markets": mks,
        "product_frame": frame,
        "trend_date": trend_date_str,
        "behaviour_sections": behaviour_sections,
        "behaviour_count": len(behaviour_sections),
        "refs": all_refs,
        "seo_notes": (seo_notes or "").strip(),
        "degraded_reasons": degraded,
        "ref_count": len(all_refs),
    }


def build_consolidated_doc(evidence: dict[str, Any]) -> dict[str, Any] | None:
    refs = evidence.get("refs") or []
    doc_json = synth.synthesize_consolidated_research(evidence)
    if doc_json is None:
        thin = synth.build_thin_consolidated_doc(evidence)
        return thin
    doc_json["_refs"] = refs
    doc_json["consolidated"] = True
    markdown = synth.render_consolidated_markdown(doc_json, evidence)
    return {
        "json": doc_json,
        "markdown": markdown,
        "sources": refs,
        "consolidated": True,
    }


def run_consolidated_research_job(
    persona_id: str,
    behaviours: list[dict],
    markets: list[str] | None,
    product_frame: str | None,
    *,
    progress_key: tuple | None = None,
    seo_notes: str = "",
) -> dict:
    evidence = build_consolidated_evidence(
        persona_id,
        behaviours,
        markets,
        product_frame,
        progress_key=progress_key,
        seo_notes=seo_notes,
    )
    _emit_progress(progress_key, "synthesizing", phase="synthesizing")
    doc = build_consolidated_doc(evidence)
    status = "completed"
    if evidence.get("degraded_reasons"):
        status = "completed_degraded"
    if doc is None:
        return {
            "status": "failed",
            "error": True,
            "message": "Consolidated synthesis unavailable for this evidence set.",
        }
    return {
        "status": status,
        "degraded_reason": ", ".join(evidence.get("degraded_reasons") or []) or None,
        "doc": doc,
        "evidence_graph": evidence,
        "signal_quality": "consolidated",
        "artifact_id": None,
        "consolidated": True,
    }


def persist_consolidated_result(result: dict) -> str:
    evidence = result.get("evidence_graph") or {}
    doc = result.get("doc") or {}
    row = {
        "created_at": datetime.now(UTC).isoformat(),
        "persona_id": evidence.get("persona_id"),
        "persona_label": evidence.get("persona_label"),
        "markets": evidence.get("markets"),
        "product_frame": evidence.get("product_frame"),
        "trend_date": evidence.get("trend_date"),
        "status": result.get("status"),
        "quality": {"level": "consolidated"},
        "signal_quality": result.get("signal_quality"),
        "evidence": evidence.get("refs") or [],
        "synthesis": doc.get("json") or {},
        "markdown": doc.get("markdown") or "",
        "doc_json": doc.get("json"),
        "doc_markdown": doc.get("markdown"),
        "evidence_graph": evidence,
        "degraded_reason": result.get("degraded_reason") or "",
        "consolidated": True,
    }
    aid = bq.insert_research_artifact(row)
    result["artifact_id"] = aid
    return aid
