"""Write a completed Ask's research inputs in the record shape consumed by blind_test."""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def record_from_ask(ctx, *, id, question, markets, run, gaps, failed_sources, note) -> dict:
    """Build one frozen research record without retaining the RunContext or its runtime state."""
    from core.agent.checks import SOURCE_STEPS

    run = run or {}
    window = run.get("window") or {}
    start = window.get("from", ctx.window_start)
    end = window.get("to", ctx.window_end)
    if start is None or end is None:
        raise ValueError("Ask record needs a research window")
    source_events = [
        {key: copy.deepcopy(event[key]) for key in ("step", "route", "status") if key in event}
        for event in ctx.events if event.get("step") in SOURCE_STEPS
    ]
    source_calls = [
        {key: copy.deepcopy(call[key]) for key in ("route", "params", "status") if key in call}
        for call in ctx.sc_calls
    ]

    return {
        "id": id,
        "question": question,
        "markets": list(markets or []),
        "window": {"from": _iso(start), "to": _iso(end)},
        "as_of": _iso(ctx.as_of),
        "tier": run.get("tier") or ctx.tier,
        "run_id": run.get("run_id") or ctx.run_id,
        "evidence": copy.deepcopy(list(ctx.evidence.values())),
        "queries": copy.deepcopy(ctx.queries),
        "gaps": copy.deepcopy(gaps or []),
        "failed_sources": copy.deepcopy(failed_sources or []),
        "note": note or "",
        "gate_controls": {
            "forecasts": copy.deepcopy(ctx.forecasts),
            "native_statuses": copy.deepcopy(ctx.native_statuses),
            "native_languages": copy.deepcopy(ctx.native_languages),
            "native_review_loaded": bool(ctx.native_review_loaded),
            "native_review_available": bool(ctx.native_review_available),
            "native_language_attempted": sorted(str(value) for value in ctx.native_language_attempted),
            "source_events": source_events,
            "sc_calls": source_calls,
        },
    }


def record_from_saved_attempt(attempt: dict) -> dict:
    """Adapt an after-answer capture to the frozen replay shape without implying a writer-input snapshot."""
    if not isinstance(attempt, dict):
        raise ValueError("saved Ask attempt must be an object")
    run = attempt.get("run")
    saved = attempt.get("ctx")
    if not isinstance(run, dict) or not isinstance(saved, dict):
        raise ValueError("saved Ask attempt needs run and ctx objects")

    run_id = attempt.get("run_id")
    if not isinstance(run_id, str) or not run_id or run_id != run.get("run_id") or run_id != saved.get("run_id"):
        raise ValueError("saved Ask run identity does not match")
    tier = run.get("tier")
    if not isinstance(tier, str) or not tier or tier != saved.get("tier"):
        raise ValueError("saved Ask tier does not match")

    record_id, question, markets = attempt.get("id"), attempt.get("question"), attempt.get("markets")
    if not isinstance(record_id, str) or not record_id or not isinstance(question, str) or not question:
        raise ValueError("saved Ask attempt needs an id and question")
    if not isinstance(markets, list) or any(not isinstance(market, str) or not market for market in markets):
        raise ValueError("saved Ask markets must be a list of names")
    effective_market = saved.get("market")
    if effective_market is not None:
        if not isinstance(effective_market, str) or effective_market not in markets:
            raise ValueError("saved Ask context market is outside its declared markets")
        replay_markets = [effective_market]
    elif len(markets) == 1:
        raise ValueError("saved Ask single market does not match its context")
    else:
        replay_markets = copy.deepcopy(markets)

    window = run.get("window")
    if not isinstance(window, dict) or not isinstance(saved.get("window_start"), str) \
            or not isinstance(saved.get("window_end"), str):
        raise ValueError("saved Ask attempt needs a complete research window")
    try:
        start = date.fromisoformat(window["from"])
        end = date.fromisoformat(window["to"])
        ctx_start = date.fromisoformat(saved["window_start"])
        ctx_end = date.fromisoformat(saved["window_end"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("saved Ask research window is invalid") from None
    if start != ctx_start or end != ctx_end or start > end:
        raise ValueError("saved Ask run and context windows do not match")

    as_of = saved.get("as_of")
    if not isinstance(as_of, str):
        raise ValueError("saved Ask context needs an as_of timestamp")
    try:
        parsed_as_of = datetime.fromisoformat(as_of)
    except ValueError:
        raise ValueError("saved Ask as_of timestamp is invalid") from None
    if not run_id.startswith(f"r_{parsed_as_of:%Y%m%d_%H%M%S}_"):
        raise ValueError("saved Ask run id and as_of timestamp do not match")

    evidence = saved.get("evidence")
    if not isinstance(evidence, dict) or any(
            not isinstance(evidence_id, str) or not isinstance(item, dict) or item.get("id") != evidence_id
            for evidence_id, item in evidence.items()):
        raise ValueError("saved Ask evidence ids do not match their records")

    queries = saved.get("queries")
    query_hashes = attempt.get("query_hashes")
    if not isinstance(queries, dict) or not isinstance(query_hashes, dict) or set(queries) != set(query_hashes):
        raise ValueError("saved Ask query hashes do not match the query ids")
    from core.agent.context import result_hash

    def restore_jsonified_datetimes(value):
        if isinstance(value, dict):
            return {key: restore_jsonified_datetimes(item) for key, item in value.items()}
        if isinstance(value, list):
            return [restore_jsonified_datetimes(item) for item in value]
        if isinstance(value, str) and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", value):
            try:
                return str(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                return value
        return value

    replay_queries = copy.deepcopy(queries)
    hash_recovery = []
    for query_id, query in queries.items():
        if not isinstance(query, dict) or "rows" not in query or not isinstance(query.get("result_hash"), str):
            raise ValueError(f"saved Ask query {query_id} is incomplete")
        stored_hash = query["result_hash"]
        if query_hashes[query_id] != stored_hash:
            raise ValueError(f"saved Ask query hash index does not match {query_id}")
        digest = result_hash(query["rows"])
        if digest == stored_hash:
            continue
        restored_rows = restore_jsonified_datetimes(query["rows"])
        if result_hash(restored_rows) != stored_hash:
            raise ValueError(f"saved Ask query hash does not match {query_id}")
        replay_queries[query_id]["rows"] = restored_rows
        hash_recovery.append({"query_id": query_id, "method": "iso_datetime_to_datetime_string"})

    source_status = run.get("source_status")
    if not isinstance(source_status, list):
        raise ValueError("saved Ask run needs captured source statuses")
    list_fields = ("events", "sc_calls", "native_language_attempted")
    for field in list_fields:
        if not isinstance(saved.get(field), list):
            raise ValueError(f"saved Ask context needs {field}")
    for field in ("events", "sc_calls"):
        if any(not isinstance(item, dict) for item in saved[field]):
            raise ValueError(f"saved Ask context {field} must contain objects")
    for field in ("forecasts", "native_statuses", "native_languages"):
        if not isinstance(saved.get(field), dict):
            raise ValueError(f"saved Ask context needs {field}")
    for field in ("native_review_loaded", "native_review_available"):
        if not isinstance(saved.get(field), bool):
            raise ValueError(f"saved Ask context needs boolean {field}")

    ctx = SimpleNamespace(
        run_id=run_id,
        tier=tier,
        as_of=as_of,
        window_start=saved["window_start"],
        window_end=saved["window_end"],
        evidence=evidence,
        queries=replay_queries,
        forecasts=saved["forecasts"],
        native_statuses=saved["native_statuses"],
        native_languages=saved["native_languages"],
        native_review_loaded=saved["native_review_loaded"],
        native_review_available=saved["native_review_available"],
        native_language_attempted=saved["native_language_attempted"],
        events=saved["events"],
        sc_calls=saved["sc_calls"],
    )
    record = record_from_ask(
        ctx,
        id=record_id,
        question=question,
        markets=replay_markets,
        run={"run_id": run_id, "tier": tier, "window": window},
        gaps=[],
        failed_sources=source_status,
        note="",
    )
    record["replay_metadata"] = {
        "snapshot_phase": "after_answer",
        "research_note_available": False,
        "declared_markets": copy.deepcopy(markets),
        "query_hash_recovery": hash_recovery,
    }
    return record


def write_record(directory, record: dict) -> Path:
    """Create one JSON record exclusively; an existing record is never replaced."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(record.get("id") or "")).strip("_")
    if not safe_id:
        raise ValueError("record id has no safe filename characters")
    path = directory / f"{safe_id}.json"
    payload = json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    return path
