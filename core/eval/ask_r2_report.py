from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence

QUESTION_IDS = ("NOW-01", "RISE-02", "WHY-03", "SPR-03", "CRE-01")
DEFAULT_MORNING_DIR = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-data")
FROZEN_MORNING_SHA256 = {
    "NOW-01": "d2993a0fa5d09b89b2ddf5d34dda9014278355c916b7535260a76ca07e352926",
    "RISE-02": "c40597369228462022100a34a9f715c7b7e235ebf0fe276f9665057963f3fb03",
    "WHY-03": "4673aade64d4345329bfbcfd1935e8f11712f6d5ef90b746eeb9f284956e3246",
    "SPR-03": "45547d5ec8df9d8b6adb372a52683677daa517ebe323170fd5c4deec1f5b7eb7",
    "CRE-01": "a2441d544e60eb27a4b37338fad5ad291777cd2eeb234bffe2824fd6aaad15dd",
}
_BT = chr(96)
_SECRET_KEY = re.compile(r"(?:api.?key|access.?token|refresh.?token|password|secret|authorization|private.?key)", re.I)
_SECRET_VALUE = re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|authorization)\b(\s*[:=]\s*)(['\"]?)[^,\s;'\"]+")
_WINDOWS_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:\\|\\\\)[^<>\"|;\r\n,]+?\.[A-Z0-9]{1,8}(?=$|[\s,:;)}\]])")
_UNIX_PATH = re.compile(r"(?i)(?<![A-Za-z0-9:/])/(?:home|users|private|tmp|var|opt|workspace|workspaces|mnt|c/Users)/[^<>\"|;\r\n,]+?\.[A-Z0-9]{1,8}(?=$|[\s,:;)}\]])")
_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,119}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$", re.I)


def load_morning_records(source_dir=DEFAULT_MORNING_DIR, expected_hashes=FROZEN_MORNING_SHA256):
    directory = Path(source_dir)
    if expected_hashes is not None and set(expected_hashes) != set(QUESTION_IDS):
        raise ValueError("expected morning hashes must cover the five question IDs")
    records, paths, hashes = {}, {}, {}
    for question_id in QUESTION_IDS:
        path = directory / f"{question_id}.json"
        try:
            payload = path.read_bytes()
        except OSError:
            raise ValueError(f"morning input unavailable for {question_id}") from None
        digest = hashlib.sha256(payload).hexdigest()
        if expected_hashes is not None and digest != expected_hashes[question_id]:
            raise ValueError(f"morning input hash mismatch for {question_id}")
        try:
            record = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError(f"morning input is not valid JSON for {question_id}") from None
        if not isinstance(record, dict) or record.get("id") != question_id:
            raise ValueError(f"morning input ID mismatch for {question_id}")
        records[question_id], paths[question_id], hashes[question_id] = record, str(path.resolve()), digest
    return records, {"source_paths": paths, "sha256": hashes}


def render_report(results, not_run, metadata, morning_records) -> str:
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a mapping")
    evaluation_label = _evaluation_label(metadata)
    _validate_r3_metadata(evaluation_label, metadata)
    morning = _records(morning_records, "morning")
    attempts_by_id = _results(results, evaluation_label)
    current = {question_id: attempts[-1][1] for question_id, attempts in attempts_by_id.items() if attempts}
    attempts = list(results)
    stopped = _ids(not_run, "not_run")
    if set(current) | stopped != set(QUESTION_IDS):
        raise ValueError("runner must account for each question exactly once")
    pending_attempts = _validate_not_run_attempts(metadata, attempts_by_id, stopped, evaluation_label)
    overlap = set(current) & stopped
    rise_keys = [key for key, _ in attempts_by_id["RISE-02"]]
    pending_rise = overlap == {"RISE-02"} and rise_keys == ["RISE-02"] and pending_attempts.get("RISE-02") == "RISE-02-retry-1"
    now_keys = [key for key, _ in attempts_by_id["NOW-01"]]
    pending_now = (evaluation_label == "R3" and overlap == {"NOW-01"} and now_keys == ["NOW-01"]
                   and pending_attempts.get("NOW-01") == "NOW-01-retry-1")
    if evaluation_label == "R2" and overlap and not pending_rise:
        raise ValueError("results and not_run may overlap only for the identified pending RISE-02 retry")
    if evaluation_label == "R3" and overlap and not pending_now:
        raise ValueError("results and not_run may overlap only for the identified pending NOW-01 retry")
    reasons = metadata.get("not_run_reasons", {})
    if not isinstance(reasons, Mapping):
        raise ValueError("not_run_reasons must be a mapping")
    for question_id in stopped:
        if not _safe_code(reasons.get(question_id)):
            raise ValueError(f"safe stop cause code missing for {question_id}")
    _validate_hashes(metadata)
    _validate_attempt_manifest(metadata, attempts)
    _validate_funding(metadata)

    out = [
        f"# Ask {evaluation_label} comparison", "",
        f"Each {evaluation_label} result is paired with its morning capture. Research windows and captured post counts are shown per question because dates and data may differ. This is not a controlled, isolated K4 benchmark, so a change cannot be attributed to code alone. Morning checker rows and reasons remain as captured.",
        *_coverage(metadata, current, evaluation_label),
    ]
    if evaluation_label == "R3":
        out += ["## Embedding coverage", *_embedding_coverage(metadata),
                "", "## Retrieval trace", *_retrieval_trace(attempts_by_id), ""]
    out += [f"| Question | Latest {evaluation_label} result | Prior {evaluation_label} attempts | Morning baseline |",
            "|---|---|---|---|"]
    for question_id in QUESTION_IDS:
        latest = current.get(question_id)
        latest_cell = (f"{evaluation_label} verdict: refused<br>NOT RUN<br>Cause: {_cell(_safe_code(reasons[question_id]))}"
                       if latest is None else _summary(latest, evaluation_label, evaluation_label))
        if question_id in pending_attempts:
            latest_cell += f"<br>Retry pending: {_cell(pending_attempts[question_id])}; cause: {_cell(_safe_code(reasons[question_id]))}"
        prior = attempts_by_id[question_id][:-1]
        out.append(f"| {question_id} | {latest_cell} | {_prior_summary(prior, evaluation_label)} | {_summary(morning[question_id], 'morning', evaluation_label)} |")
    out += ["", "## Spend accounting", *_spend(attempts, stopped, metadata, morning, evaluation_label),
            "", "## Source and health provenance", *_deployment(metadata, evaluation_label),
            "", "## Morning input integrity", *_integrity(metadata),
            "", "## Raw receipts",]
    for question_id in QUESTION_IDS:
        out += _receipts(question_id, attempts_by_id[question_id], morning[question_id], reasons,
                         evaluation_label, pending_attempts.get(question_id))
    return "\n".join(out).rstrip() + "\n"


def _evaluation_label(metadata):
    label = metadata.get("evaluation_label", "R2")
    if not isinstance(label, str) or label not in {"R2", "R3"}:
        raise ValueError("evaluation_label must be R2 or R3")
    return label


def _validate_r3_metadata(evaluation_label, metadata):
    if evaluation_label != "R3":
        return
    if metadata.get("funding") is not None or metadata.get("resume_provenance") is not None:
        raise ValueError("R3 does not allow R2 resume metadata")
    pending = metadata.get("not_run_attempts")
    if isinstance(pending, list):
        for item in pending:
            if not isinstance(item, Mapping):
                continue
            attempt_key, question_id = item.get("attempt_key"), item.get("id")
            if attempt_key not in (None, question_id) and not (
                    question_id == "NOW-01" and attempt_key == "NOW-01-retry-1"):
                raise ValueError("R3 does not allow an unauthorized pending retry")


def _embedding_coverage(metadata):
    receipt = metadata.get("embedding_coverage")
    if not isinstance(receipt, Mapping):
        return ["Status: unverified; no embedding coverage receipt was supplied.",
                "Full embedding coverage receipt:", _json("unverified")]
    latest_run = receipt.get("latest_embedding_run")
    latest_run = latest_run if isinstance(latest_run, Mapping) else {}
    latest_date = latest_run.get("run_date")
    lines = [
        f"Status: {_shown(receipt.get('status'))}.",
        f"Latest embedding run date: {_shown(latest_date)}.",
        f"Coverage window: {_shown(receipt.get('window_start_date'))} to {_shown(receipt.get('window_end_date'))}; days: {_shown(receipt.get('window_days'))}; basis: {_safe_text(receipt.get('window_basis'))}.",
        f"As of: {_shown(receipt.get('as_of_date'))}; current day partial: {_shown(receipt.get('current_day_partial'))}.",
        "", "| Market | Latest embedding run date | Embedded distinct posts in stated window | Eligible distinct posts in stated window |",
        "|---|---|---:|---:|",
    ]
    markets = receipt.get("markets")
    if isinstance(markets, Mapping) and markets:
        for market, counts in sorted(markets.items(), key=lambda item: str(item[0])):
            counts = counts if isinstance(counts, Mapping) else {}
            lines.append(
                f"| {_cell(_safe_value(market))} | {_cell(_shown(latest_date))} | "
                f"{_cell(_shown(counts.get('embedded_distinct_posts')))} | "
                f"{_cell(_shown(counts.get('eligible_distinct_posts')))} |"
            )
    else:
        lines.append("| unverified | unverified | unverified | unverified |")
    lines += ["", "Full embedding coverage receipt:", _json(receipt)]
    return lines


def _retrieval_trace(attempts_by_id):
    lines = [
        "| Question | Mode | Status | Refusal category | Query ID | Rows | Post IDs | Error type |",
        "|---|---|---|---|---|---:|---|---|",
    ]
    raw_blocks = []
    for question_id in QUESTION_IDS:
        events = []
        for _, record in attempts_by_id[question_id]:
            context = record.get("context")
            context = context if isinstance(context, Mapping) else {}
            rows = context.get("events")
            if isinstance(rows, list):
                events.extend(event for event in rows if isinstance(event, Mapping)
                              and event.get("step") == "retrieval_trace")
        seen_modes = set()
        for event in events:
            mode = _safe_code(event.get("mode")) or "unverified"
            if mode in {"keyword", "semantic"}:
                seen_modes.add(mode)
            status = _safe_code(event.get("status")) or "unverified"
            category = _safe_code(event.get("refusal_category")) or "unverified"
            query_id = _cell(_safe_value(event.get("query_id", "unverified")))
            count = _cell(_shown(event.get("row_count")))
            post_ids = json.dumps(_safe_value(event.get("post_ids", "unverified")),
                                  ensure_ascii=False, separators=(",", ":"))
            error_type = _safe_code(event.get("error_type")) or "unverified"
            lines.append(
                f"| {question_id} | {mode} | {status} | {category} | {query_id} | "
                f"{count} | {_cell(post_ids)} | {error_type} |"
            )
        for mode in ("keyword", "semantic"):
            if mode not in seen_modes:
                lines.append(f"| {question_id} | {mode} | unverified | unverified | unverified | unverified | unverified | unverified |")
        raw_blocks += [f"{question_id} raw retrieval_trace events:", _json(events if events else "unverified"), ""]
    return lines + [""] + raw_blocks


def _records(records, label):
    if not isinstance(records, Mapping) or set(records) != set(QUESTION_IDS):
        raise ValueError(f"{label} records must contain exactly the five question IDs")
    for question_id in QUESTION_IDS:
        if not isinstance(records[question_id], Mapping) or records[question_id].get("id") != question_id:
            raise ValueError(f"{label} record ID mismatch for {question_id}")
    return records


def _coverage(metadata, records, evaluation_label):
    scope = metadata.get("cost_scope")
    scope_text = _safe_text(scope)
    lowered = scope_text.lower()
    lines = [""]
    if "semantic" in lowered and "refus" in lowered:
        lines.append(f"Retrieval coverage differs: the morning baseline allowed semantic retrieval, while {evaluation_label} refuses semantic TVF retrieval and uses keyword retrieval. These rows cannot isolate K4 quality changes.")
    else:
        lines.append("Retrieval coverage: unverified unless shown in the per-question events below.")
    for question_id, record in records.items():
        context = record.get("context")
        events = context.get("events") if isinstance(context, Mapping) else None
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping) or event.get("step") != "r2_coverage_gap":
                continue
            reason = _safe_code(event.get("reason"))
            lines.append(f"{evaluation_label} {question_id} coverage gap: {reason or 'unverified'}.")
    return lines + [""]


def _results(results, evaluation_label="R2"):
    if isinstance(results, (str, bytes)) or not isinstance(results, Sequence):
        raise ValueError("results must be a sequence")
    indexed = {question_id: [] for question_id in QUESTION_IDS}
    attempt_keys, run_ids, prefixes = set(), set(), set()
    write_run_ids, readback_run_ids = set(), set()
    for record in results:
        if not isinstance(record, Mapping):
            raise ValueError("result must be a mapping")
        question_id = record.get("id")
        if question_id not in QUESTION_IDS:
            raise ValueError("result has an unexpected question ID")
        attempt_key = record.get("attempt_key", question_id)
        if not _safe_code(attempt_key):
            raise ValueError(f"attempt key is invalid for {question_id}")
        if evaluation_label == "R3" and attempt_key != question_id and not (
                question_id == "NOW-01" and attempt_key == "NOW-01-retry-1"):
            raise ValueError("R3 allows only NOW-01 retry-1 as a second attempt")
        if attempt_key in attempt_keys:
            raise ValueError("duplicate attempt key")
        attempt_keys.add(attempt_key)
        run = record.get("run") if isinstance(record.get("run"), Mapping) else {}
        direct_run_id, nested_run_id = record.get("run_id"), run.get("run_id")
        if direct_run_id and nested_run_id and direct_run_id != nested_run_id:
            raise ValueError(f"run ID mismatch for {question_id}")
        run_id = direct_run_id or nested_run_id
        if isinstance(run_id, str) and run_id:
            if run_id in run_ids:
                raise ValueError(f"duplicate {evaluation_label} run ID")
            run_ids.add(run_id)
        prefix = record.get("prefix")
        if isinstance(prefix, str) and prefix:
            if prefix in prefixes:
                raise ValueError("duplicate attempt prefix")
            prefixes.add(prefix)
        for field, seen_ids, label in (
            ("spend_writes", write_run_ids, "write"),
            ("spend_readback", readback_run_ids, "readback"),
        ):
            container = record.get(field)
            rows = container.get("rows") if isinstance(container, Mapping) else container
            if not isinstance(rows, list):
                continue
            for row in rows:
                row_id = row.get("run_id") if isinstance(row, Mapping) else None
                if isinstance(row_id, str) and row_id:
                    if row_id in seen_ids:
                        raise ValueError(f"duplicate {evaluation_label} spend {label} run ID")
                    seen_ids.add(row_id)
        indexed[question_id].append((attempt_key, record))

    for question_id, grouped in indexed.items():
        keys = [attempt_key for attempt_key, _ in grouped]
        if not grouped:
            continue
        if keys == [question_id]:
            continue
        if evaluation_label == "R3" and question_id == "NOW-01" and keys == ["NOW-01", "NOW-01-retry-1"]:
            retry = grouped[-1][1]
            if retry.get("prefix") != "l3-ask-20260930-r3-NOW-01-retry-1":
                raise ValueError("NOW-01 retry prefix does not match the approved R3 attempt")
            continue
        if question_id == "RISE-02" and keys == ["RISE-02", "RISE-02-retry-1"]:
            retry = grouped[-1][1]
            if retry.get("prefix") != "l3-ask-20260930-r2-RISE-02-retry-1":
                raise ValueError("RISE-02 retry prefix does not match the approved attempt")
            continue
        if evaluation_label == "R3" and question_id == "NOW-01" and keys == ["NOW-01-retry-1"]:
            raise ValueError("R3 NOW-01 retry requires its original attempt first")
        raise ValueError("only RISE-02 retry-1 is allowed as a second attempt")
    return indexed


def _validate_attempt_manifest(metadata, attempts):
    listed = metadata.get("attempts")
    if listed is None:
        return
    if not isinstance(listed, list):
        raise ValueError("metadata attempts must be a list")
    expected = {record.get("attempt_key", record.get("id")): record for record in attempts}
    seen = set()
    for item in listed:
        if not isinstance(item, Mapping):
            raise ValueError("metadata attempt must be a mapping")
        key = item.get("attempt_key", item.get("key"))
        if not _safe_code(key) or key in seen:
            raise ValueError("metadata has an invalid or duplicate attempt key")
        seen.add(key)
        if key not in expected:
            raise ValueError("metadata attempt list does not match results")
        record = expected[key]
        question_id = record.get("id")
        if item.get("id") != question_id or item.get("prefix") != record.get("prefix"):
            raise ValueError("metadata attempt identity does not match results")
        listed_cost = item.get("charged_usd", item.get("cost"))
        if item.get("outcome") != record.get("outcome") or _num(listed_cost) != _num(record.get("charged_usd")):
            raise ValueError("metadata attempt amount or outcome does not match results")
        if "stop_reason" in item and item.get("stop_reason") != record.get("stop_reason"):
            raise ValueError("metadata attempt stop reason does not match results")
    if seen != set(expected):
        raise ValueError("metadata attempt list omits a result")


def _validate_not_run_attempts(metadata, attempts_by_id, stopped, evaluation_label="R2"):
    listed = metadata.get("not_run_attempts")
    if listed is None:
        return {}
    if not isinstance(listed, list):
        raise ValueError("metadata not_run_attempts must be a list")
    result_keys = {key for grouped in attempts_by_id.values() for key, _ in grouped}
    pending = {}
    for item in listed:
        if not isinstance(item, Mapping):
            raise ValueError("not-run attempt must be a mapping")
        question_id, attempt_key = item.get("id"), item.get("attempt_key")
        if question_id not in QUESTION_IDS or question_id not in stopped or not _safe_code(attempt_key):
            raise ValueError("not-run attempt identity is invalid")
        if question_id in pending or attempt_key in result_keys:
            raise ValueError("duplicate or already returned not-run attempt")
        pending[question_id] = attempt_key
    for question_id, attempt_key in pending.items():
        if attempt_key.endswith("-retry-1"):
            keys = [key for key, _ in attempts_by_id[question_id]]
            allowed_rise = (evaluation_label == "R2" and question_id == "RISE-02"
                            and keys == ["RISE-02"] and attempt_key == "RISE-02-retry-1")
            allowed_now = (evaluation_label == "R3" and question_id == "NOW-01"
                           and keys == ["NOW-01"] and attempt_key == "NOW-01-retry-1")
            if not (allowed_rise or allowed_now):
                raise ValueError("only the identified evaluation retry may be pending")
    return pending


def _validate_funding(metadata):
    funding = metadata.get("funding")
    if not isinstance(funding, Mapping):
        return
    baseline = _num(funding.get("baseline_spend_usd", funding.get("baseline_usd")))
    additional = _num(funding.get("additional_cap_usd", funding.get("additional_usd")))
    combined = _num(funding.get("combined_total_cap_usd", funding.get("combined_usd")))
    if baseline is None or additional is None or combined is None or baseline + additional != combined:
        raise ValueError("funding ceiling values do not reconcile")
    if additional < 0:
        raise ValueError("additional funding cannot be negative")
    if additional > Decimal("8"):
        raise ValueError("additional funding exceeds the authorized USD 8 cap")
    cap = _num(metadata.get("cap_usd", metadata.get("session_cap_usd")))
    if cap is not None and combined > cap:
        raise ValueError("combined funding ceiling exceeds the original session cap")


def _ids(values, label):
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{label} must be a sequence")
    ids = set()
    for question_id in values:
        if question_id not in QUESTION_IDS or question_id in ids:
            raise ValueError(f"{label} has an unexpected or duplicate question ID")
        ids.add(question_id)
    return ids


def _summary(record, source, evaluation_label="R2"):
    answer = record.get("raw_answer")
    answer_status = answer.get("status") if isinstance(answer, Mapping) else None
    run = record.get("run") if isinstance(record.get("run"), Mapping) else {}
    bars = record.get("bars") if isinstance(record.get("bars"), Mapping) else {}
    window = run.get("window") if isinstance(run.get("window"), Mapping) else {}
    cited_count = _num(bars.get("cited"))
    accept = "PASS" if bars.get("pass") is True else "Below the five post bar" if bars.get("pass") is False and cited_count is not None and cited_count < 5 else "Below acceptance bar" if bars.get("pass") is False else "unverified"
    platforms = bars.get("platforms")
    platform_names = [str(x) for x in platforms if isinstance(x, str)] if isinstance(platforms, list) else None
    platform_text = f"{len(platform_names)} ({', '.join(platform_names)})" if platform_names else "0" if platform_names == [] else "unverified"
    model_usd = record.get("reported_model_usd") if source == "morning" else record.get("charged_usd")
    booked = _shown(record.get("booked_usd")) if source == "morning" else _booked(record)
    values = [
        f"Acceptance: {accept}", f"Outcome: {_shown(record.get('outcome'))}",
        f"Schema valid: {_shown(bars.get('schema_valid'))}",
        f"Answer status: {_shown(answer_status)}",
        f"Window: {_shown(window.get('from'))} to {_shown(window.get('to'))}; captured posts: {_shown(run.get('posts'))}",
        f"Cited in window / cited: {_shown(bars.get('cited_in_window'))} / {_shown(bars.get('cited'))}",
        f"Platforms: {platform_text}", f"Unknown IDs: {_shown(bars.get('unknown_ids'))}; age claims: {_shown(bars.get('age_claims'))}",
        f"Numbers reproduced / total: {_shown(bars.get('numbers_reproduced'))} / {_shown(bars.get('numbers'))}",
        (f"Tracked token priced USD: {_shown(model_usd)}" if source == "morning"
         else f"Runner-reported USD amount: {_money_value(model_usd)}"),
        f"Rounded booked USD: {booked}",
        f"Run ID: {_run_id(record)}",
    ]
    if source != "morning":
        values.insert(0, f"{evaluation_label} verdict: {_r2_verdict(record)}")
    error = _error(record)
    if error:
        values.insert(2, f"Error type and cause code: {error}")
    return "<br>".join(_cell(value) for value in values)


def _prior_summary(attempts, evaluation_label="R2"):
    if not attempts:
        return "none"
    summaries = []
    for attempt_key, record in attempts:
        values = [
            f"Attempt {_cell(attempt_key)}: {evaluation_label} verdict {_r2_verdict(record)}",
            f"runner-reported USD {_money_value(record.get('charged_usd'))}",
            f"rounded booked USD {_booked(record)}",
        ]
        error = _error(record)
        if error:
            values.append(f"error {error}")
        ceiling = _unknown_ceiling(record)
        if ceiling is not None:
            values.append(f"unknown-dispatch ceiling {_money(ceiling)}, not measured token usage")
        summaries.append("; ".join(values))
    return "<br>".join(summaries)


def _r2_verdict(record):
    answer = record.get("raw_answer")
    status = answer.get("status") if isinstance(answer, Mapping) else None
    status = status.strip().casefold() if isinstance(status, str) else ""
    outcome = record.get("outcome")
    outcome = outcome.strip().upper() if isinstance(outcome, str) else ""
    operational = outcome in {"OPERATIONAL STOP", "ERROR", "FAILED", "REFUSED"}
    operational = operational or any(record.get(key) for key in ("error_type", "research_error_type", "stop_reason"))
    if operational or not status or status in {"refused", "insufficient_evidence"}:
        return "refused"
    if status == "partial":
        return "partial"
    if status != "complete":
        return "refused"
    bars = record.get("bars") if isinstance(record.get("bars"), Mapping) else {}
    claim_readback = record.get("claim_readback")
    spend_readback = record.get("spend_readback")
    checks_match = (
        outcome == "PASS"
        and bars.get("schema_valid") is True
        and bars.get("pass") is True
        and isinstance(claim_readback, Mapping)
        and claim_readback.get("match") is True
        and isinstance(spend_readback, Mapping)
        and spend_readback.get("match") is True
    )
    return "full answer with local receipts" if checks_match else "partial"


def _run_id(record):
    direct = record.get("run_id")
    run = record.get("run")
    nested = run.get("run_id") if isinstance(run, Mapping) else None
    return "identity mismatch" if direct and nested and direct != nested else _shown(direct or nested)


def _error(record):
    parts = []
    for field in ("error_type", "research_error_type", "readback_error_type", "session_readback_error_type"):
        error_type = record.get(field)
        if (isinstance(error_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,99}", error_type)
                and error_type not in parts):
            parts.append(error_type)
    cause = _safe_code(record.get("stop_reason"))
    if cause:
        parts.append(cause)
    return ", ".join(parts)


def _booked(record):
    readback = record.get("spend_readback")
    if not isinstance(readback, Mapping) or readback.get("match") is not True or not isinstance(readback.get("rows"), list):
        return "unverified"
    rows = readback["rows"]
    writes = record.get("spend_writes")
    prefix, question_id = record.get("prefix"), record.get("id")
    if (not isinstance(prefix, str) or not prefix or question_id not in QUESTION_IDS
            or not isinstance(writes, list) or len(rows) != len(writes)):
        return "unverified"
    amounts = []
    for row in rows:
        if (not isinstance(row, Mapping) or not isinstance(row.get("run_id"), str)
                or not row["run_id"].startswith(prefix + "-spend-")
                or row.get("stage") != "understand_spend" or row.get("status") != "ok"
                or row.get("what") != f"staging_check_ask:{question_id}"):
            return "unverified"
        value = _num(row.get("booked_usd"))
        if value is None:
            return "unverified"
        amounts.append(value)
    for write in writes:
        if (not isinstance(write, Mapping) or not isinstance(write.get("run_id"), str)
                or not write["run_id"].startswith(prefix + "-spend-")
                or write.get("stage") != "understand_spend" or write.get("status") != "ok"
                or write.get("what") != f"staging_check_ask:{question_id}"):
            return "unverified"
    return _money(sum(amounts, Decimal(0)))


def _unknown_ceiling(record):
    rows = record.get("call_costs")
    if not isinstance(rows, list):
        return None
    values = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("status") != "unknown_charged_ceiling":
            continue
        amount = _num(row.get("reserved_usd"))
        if amount is None:
            return None
        values.append(amount)
    return sum(values, Decimal(0)) if values else None


def _call_cost_totals(records):
    known, ceilings, conservative_ceilings = [], [], []
    conservative_seen = False
    other = set()
    for record in records:
        rows = record.get("call_costs")
        if not isinstance(rows, list):
            other.add("call_costs unavailable for an attempt")
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                other.add("invalid call cost row")
                continue
            status = row.get("status")
            if status == "charged_known":
                amount = _num(row.get("charged_usd"))
                if amount is None:
                    other.add("charged_known amount unverified")
                else:
                    known.append(amount)
            elif status == "unknown_charged_ceiling":
                amount = _num(row.get("reserved_usd"))
                if amount is None:
                    other.add("unknown_charged_ceiling amount unverified")
                else:
                    ceilings.append(amount)
            elif status == "charged_conservative_ceiling":
                conservative_seen = True
                basis = row.get("charge_basis")
                reserved = _num(row.get("reserved_usd"))
                charged = _num(row.get("charged_usd"))
                if (basis not in {"conservative_embedding_ceiling", "bounded_usage_missing"}
                        or reserved is None or charged is None or reserved != charged):
                    other.add("conservative ceiling reservation unverified or unmatched")
                else:
                    conservative_ceilings.append(reserved)
            elif status == "refused_before_dispatch":
                charged = _num(row.get("charged_usd"))
                reserved = _num(row.get("reserved_usd"))
                if charged != 0 or reserved != 0:
                    other.add("refused_before_dispatch amount unverified or nonzero")
            else:
                other.add(_safe_text(status))
    known_total = sum(known, Decimal(0)) if known else None
    ceiling_total = sum(ceilings, Decimal(0)) if ceilings else None
    conservative_total = sum(conservative_ceilings, Decimal(0)) if conservative_ceilings else None
    return known_total, ceiling_total, conservative_total, conservative_seen, sorted(other)


def _query_bytes(record):
    context = record.get("context")
    queries = context.get("queries") if isinstance(context, Mapping) else None
    if not isinstance(queries, Mapping) or not queries:
        return None
    amounts = []
    for query in queries.values():
        if not isinstance(query, Mapping) or "bytes" not in query:
            return None
        amount = _num(query.get("bytes"))
        if amount is None:
            return None
        amounts.append(amount)
    return sum(amounts, Decimal(0))


def _spend(records, stopped, metadata, morning, evaluation_label="R2"):
    cap = _money_value(metadata.get("cap_usd", metadata.get("session_cap_usd")))
    values = [_num(record.get("charged_usd")) for record in records]
    complete = bool(records) and all(value is not None for value in values)
    sum_values = sum((value for value in values if value is not None), Decimal(0))
    aggregate = _num(metadata.get("charged_usd"))
    morning_model = [_num(record.get("reported_model_usd")) for record in morning.values()]
    morning_booked = [_num(record.get("booked_usd")) for record in morning.values()]
    lines = [f"Cost scope: {_safe_text(metadata.get('cost_scope'))}.",
             "Runner-reported amounts can include an unknown-dispatch ceiling. Only rows marked charged_known are counted as known call charges. Booked USD sums matched spend readback rows.",
             f"Session cap: {cap}."]
    continuation = metadata.get("r3_continuation")
    if evaluation_label == "R3" and isinstance(continuation, Mapping):
        initial_booked = _num(continuation.get("initial_booked_usd"))
        if initial_booked is not None:
            lines.append(f"Already booked before R3 continuation: {format(initial_booked, '.6f')} USD; reference only, not added separately from continuation metadata.")
        else:
            lines.append("Already booked before R3 continuation: unverified; no valid amount in continuation provenance.")
    lines.append(f"{evaluation_label} reported runner amount total: {_money(aggregate) if aggregate is not None else 'unverified' }.")
    if records and complete:
        match = "matches" if aggregate == sum_values else "does not match" if aggregate is not None else "not supplied"
        lines.append(f"Per-attempt reported amount sum: {_money(sum_values)}; aggregate {match}.")
    elif records:
        lines.append(f"Known per-attempt reported amount sum: {_money(sum_values)}; complete total unverified.")
    else:
        lines.append("Per-attempt reported amount sum: unverified.")
    known_call, ceiling_call, conservative_call, conservative_seen, other_statuses = _call_cost_totals(records)
    if known_call is not None:
        lines.append(f"Known-dispatch call charge subtotal: {_money(known_call)}.")
    else:
        lines.append("Known-dispatch call charge subtotal: unverified; no classified charged_known rows.")
    if ceiling_call is not None:
        lines.append(f"Unknown-dispatch ceiling: {_money(ceiling_call)}; not measured token usage.")
        lines.append("Fallback token bounds are not measured usage.")
    if conservative_call is not None:
        lines.append(f"Conservative reservation subtotal: {_money(conservative_call)}; unmeasured reserved amount.")
    elif conservative_seen:
        lines.append("Conservative reservation subtotal: unverified; raw cost rows are retained in the receipts.")
    if other_statuses:
        lines.append(f"Other call cost statuses are not treated as known charges: {', '.join(_cell(status) for status in other_statuses)}.")
    booked = [_num(_booked(record).removesuffix(" USD")) if _booked(record) != "unverified" else None
              for record in records]
    if records and all(value is not None for value in booked):
        lines.append(f"Rounded booked total from matched spend readbacks: {_money(sum(booked, Decimal(0)))}.")
    elif any(value is not None for value in booked):
        known = sum((value for value in booked if value is not None), Decimal(0))
        lines.append(f"Known rounded booked readbacks: {_money(known)}; total unverified.")
    else:
        lines.append("Rounded booked total: unverified; no complete matched spend readback set.")
    manifest = metadata.get("attempts")
    manifest = manifest if isinstance(manifest, list) else []
    manifest_by_key = {
        item.get("attempt_key", item.get("key")): item
        for item in manifest if isinstance(item, Mapping)
    }
    lines += ["", "Attempt ledger", "", "| Question | Attempt key | Outcome | Runner-reported USD | Session cap | Prefix |", "|---|---|---|---:|---:|---|"]
    for record in records:
        attempt_key = record.get("attempt_key", record.get("id"))
        attempt_meta = manifest_by_key.get(attempt_key, {})
        attempt_cap = attempt_meta.get("session_cap_usd", metadata.get("cap_usd", metadata.get("session_cap_usd")))
        prefix = record.get("prefix")
        prefix_text = _safe_value(prefix) if isinstance(prefix, str) and prefix else "unverified"
        lines.append(f"| {_cell(record.get('id'))} | {_cell(attempt_key)} | {_cell(_shown(record.get('outcome')))} | {_cell(_money_value(record.get('charged_usd')))} | {_cell(_money_value(attempt_cap))} | {_cell(prefix_text)} |")
    if all(value is not None for value in morning_model):
        morning_total = sum(morning_model, Decimal(0)).quantize(Decimal("0.0000000001"))
        lines.append(
            "Morning totals sum the five captured rows. The token priced total is rounded to 10 decimal places after summing; booked USD uses the stored per-run amounts rounded to 6 decimal places."
        )
        lines.append(f"Morning tracked token priced total: {_money(morning_total)}.")
    else:
        lines.append("Morning tracked token priced total: unverified.")
    if all(value is not None for value in morning_booked):
        lines.append(f"Morning rounded booked total: {_money(sum(morning_booked, Decimal(0)))}.")
    else:
        lines.append("Morning rounded booked total: unverified.")
    funding = metadata.get("funding")
    if isinstance(funding, Mapping):
        baseline = _num(funding.get("baseline_spend_usd", funding.get("baseline_usd")))
        additional = _num(funding.get("additional_cap_usd", funding.get("additional_usd")))
        combined = _num(funding.get("combined_total_cap_usd", funding.get("combined_usd")))
        if baseline is not None and additional is not None and combined is not None and baseline + additional == combined:
            lines.append(f"Combined retry ceiling: {_money(combined)} (baseline {_money(baseline)} plus additional {_money(additional)}).")
        else:
            lines.append("Combined retry ceiling: unverified; funding amounts do not reconcile.")
    session_booked = _num(metadata.get("session_booked_usd"))
    if metadata.get("session_booked_verified") is True and session_booked is not None:
        lines.append(f"{evaluation_label} session booked readback: {_money(session_booked)}.")
    else:
        lines.append(f"{evaluation_label} session booked readback: unverified; no verified session total supplied.")
    warehouse_values = [_query_bytes(record) for record in records]
    if records and all(value is not None for value in warehouse_values):
        lines.append(f"{evaluation_label} research-query snapshot billed bytes: {format(sum(warehouse_values, Decimal(0)), 'f')} bytes; preflight and readback queries are excluded, so this is not a total. USD cost is unverified.")
    elif any(value is not None for value in warehouse_values):
        known_bytes = sum((value for value in warehouse_values if value is not None), Decimal(0))
        lines.append(f"Known {evaluation_label} research-query snapshot bytes: {format(known_bytes, 'f')}; complete bytes total unverified and preflight and readback queries are excluded. USD cost is unverified.")
    else:
        lines.append(f"{evaluation_label} warehouse billed bytes: unverified; query snapshots contain no captured bytes. USD cost is unverified.")
    if stopped:
        lines.append(f"Not run: {len(stopped)} of five question slots.")
    lines.append("Vendor billing reconciliation: unproven.")
    return lines


def _deployment(metadata, evaluation_label="R2"):
    lines = ["Health at start:", _json(metadata.get("health_at_start", "unverified"))]
    if evaluation_label == "R3":
        lines += ["R3 clean source commit at start:", _json(metadata.get("source_commit", "unverified"))]
        lines += ["R3 continuation provenance:", _json(metadata.get("r3_continuation", "unverified"))]
    else:
        resume = metadata.get("resume_provenance")
        resume = resume if isinstance(resume, Mapping) else {}
        lines += ["Original source details from resume provenance:", _json(resume.get("original_source_details", "unverified")),
                  "Original source commit from resume provenance:", _json(resume.get("original_source_commit", "unverified"))]
    lines.append(f"Per-attempt source and health provenance is shown with each {evaluation_label} receipt; missing attempt values are unverified.")
    return lines


def _integrity(metadata):
    paths, hashes = metadata.get("morning_source_paths"), metadata.get("morning_sha256")
    paths = paths if isinstance(paths, Mapping) else {}
    hashes = hashes if isinstance(hashes, Mapping) else {}
    lines = ["| Question | Source path | SHA-256 |", "|---|---|---|"]
    for question_id in QUESTION_IDS:
        path = paths.get(question_id)
        path = path if isinstance(path, str) else "unverified"
        digest = hashes.get(question_id)
        if isinstance(digest, str) and digest.lower() != FROZEN_MORNING_SHA256[question_id]:
            raise ValueError(f"reported morning hash mismatch for {question_id}")
        digest = digest.lower() if isinstance(digest, str) and _SHA256.fullmatch(digest) else "unverified"
        lines.append(f"| {question_id} | {_cell(path)} | {_BT}{digest}{_BT} |")
    return lines


def _validate_hashes(metadata):
    hashes = metadata.get("morning_sha256")
    if hashes is None:
        return
    if not isinstance(hashes, Mapping) or set(hashes) != set(QUESTION_IDS):
        raise ValueError("morning_sha256 must cover the five question IDs")
    for question_id, expected in FROZEN_MORNING_SHA256.items():
        if str(hashes.get(question_id, "")).lower() != expected:
            raise ValueError(f"reported morning hash mismatch for {question_id}")


def _receipts(question_id, attempts, morning, reasons, evaluation_label="R2", pending_key=None):
    lines = [f"### {question_id}", ""]
    if not attempts:
        lines += [f"{evaluation_label}: NOT RUN, cause code {_BT}{_safe_code(reasons[question_id])}{_BT}.",
                  f"{evaluation_label} verdict: refused.",
                  f"No {evaluation_label} raw answer or run receipt exists because the question did not run.", ""]
    else:
        for attempt_key, record in attempts:
            label = evaluation_label if len(attempts) == 1 else f"{evaluation_label} attempt {attempt_key}"
            lines += _record_receipt(label, record, attempt_key, evaluation_label)
    if pending_key:
        lines += [f"{evaluation_label} attempt {pending_key}: NOT RUN, cause code {_BT}{_safe_code(reasons[question_id])}{_BT}.",
                  "No raw answer or run receipt exists for this pending attempt.", ""]
    lines += _record_receipt("Morning", morning, evaluation_label=evaluation_label)
    return lines


def _record_receipt(label, record, attempt_key=None, evaluation_label="R2"):
    answer = record.get("raw_answer")
    status = answer.get("status") if isinstance(answer, Mapping) else None
    run = record.get("run") if isinstance(record.get("run"), Mapping) else {}
    attempt_key = attempt_key or record.get("attempt_key", record.get("id"))
    prefix = record.get("prefix")
    prefix_text = _safe_value(prefix) if isinstance(prefix, str) and prefix else "unverified"
    lines = [
        f"{label} question: {_cell(_safe_value(record.get('question')))}",
        f"{label} attempt key: {_cell(_safe_value(attempt_key))}",
        f"{label} spend prefix: {_cell(prefix_text)}",
        f"{label} run ID: {_BT}{_cell(_run_id(record))}{_BT}",
        f"{label} outcome: {_cell(_shown(record.get('outcome')))}",
        f"{label} answer status: {_cell(_shown(status))}",
        f"{label} research window: {_cell(_window(run))}",
    ]
    if label == evaluation_label or label.startswith(f"{evaluation_label} "):
        lines.insert(3, f"{evaluation_label} verdict: {_r2_verdict(record)}")
    accounting_status = record.get("accounting_status")
    if accounting_status is not None:
        lines.append(f"{label} accounting status: {_cell(_safe_value(accounting_status))}")
    error = _error(record)
    if error:
        lines.append(f"{label} error type and cause code: {_cell(error)}")
    context = record.get("context")
    queries = context.get("queries") if isinstance(context, Mapping) else None
    if "warehouse_billed_bytes" in record or isinstance(queries, Mapping):
        query_bytes = _query_bytes(record)
        if query_bytes is None:
            lines.append(f"{label} warehouse billed bytes: unverified; query snapshots contain no captured bytes.")
        else:
            lines.append(f"{label} research-query snapshot bytes: {format(query_bytes, 'f')}; preflight and readback queries are excluded.")
    lines += ["", f"{label} query hashes", "", _json(record.get("query_hashes", "unverified")),
              "", f"{label} cited sources", "", _json(_cited(record)),
              "", f"{label} checker reason summary", ""]
    lines += _checker(record)
    lines += ["", f"{label} claim readback", "", _json(record.get("claim_readback", "unverified")),
              "", f"{label} spend writes", "", _json(record.get("spend_writes", "unverified")),
              "", f"{label} spend intents", "", _json(record.get("spend_intents", "unverified"))]
    readback = record.get("spend_readback")
    if readback is None:
        readback = {"reported_model_usd": record.get("reported_model_usd"), "booked_usd": record.get("booked_usd")}
    lines += ["", f"{label} spend readback", "", _json(readback)]
    costs = record.get("call_costs")
    if isinstance(costs, list):
        if _unknown_ceiling(record) is not None:
            lines += ["", f"{label} note: fallback token bounds on unknown-dispatch rows are not measured usage."]
        lines += ["", f"{label} model call cost rows", "", _json(_cost_rows(costs))]
    source = record.get("source_provenance")
    if source is not None:
        lines += ["", f"{label} source provenance", "", _json(source)]
    continuation = record.get("r3_continuation")
    if evaluation_label == "R3" and continuation is not None:
        lines += ["", f"{label} continuation provenance", "", _json(continuation)]
    lines += ["", f"{label} raw answer", "", "not returned" if answer is None else _json(answer), ""]
    return lines


def _window(run):
    window = run.get("window")
    if not isinstance(window, Mapping):
        return "unverified"
    return f"{_shown(window.get('from'))} to {_shown(window.get('to'))}; captured posts {_shown(run.get('posts'))}"


def _cited(record):
    if record.get("citations") is not None:
        return record["citations"]
    answer = record.get("raw_answer")
    return answer.get("evidence", "unavailable") if isinstance(answer, Mapping) else "unavailable"


def _checker(record):
    rows = record.get("claim_rows")
    if not isinstance(rows, list):
        return ["Checker rows: unavailable."]
    counts = Counter()
    for row in rows:
        if isinstance(row, Mapping):
            reason = _reason(row.get("reason"))
            counts[(_shown(row.get("checker")), _shown(row.get("rule")),
                    _shown(row.get("verdict")), reason)] += 1
    if not counts:
        return ["Checker rows: 0."]
    lines = ["| Checker | Rule | Verdict | Rows | Reason summary |", "|---|---|---|---:|---|"]
    for (checker, rule, verdict, reason), count in sorted(counts.items()):
        lines.append(f"| {_cell(checker)} | {_cell(rule)} | {_cell(verdict)} | {count} | {_cell(reason)} |")
    return lines


def _cost_rows(rows):
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _reason(value):
    if value is None or value == "":
        return "none"
    text = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else _sanitize(value)
    return text if len(text) <= 420 else text[:417] + "..."


def _safe_value(value):
    if isinstance(value, Mapping):
        return {str(key): "[redacted]" if _SECRET_KEY.search(str(key)) else _safe_value(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        return _sanitize(value)
    return value


def _sanitize(value):
    text = str(value)
    text = _SECRET_VALUE.sub(r"\1\2[redacted]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = _WINDOWS_PATH.sub("[path omitted]", text)
    text = _UNIX_PATH.sub("[path omitted]", text)
    return text.strip()


def _safe_text(value):
    return _sanitize(value) if isinstance(value, str) and value else "unverified"


def _safe_code(value):
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


def _shown(value):
    if value is None or value == "":
        return "unverified"
    if isinstance(value, bool):
        return str(value).lower()
    number = _num(value)
    return format(number, "f") if number is not None else _sanitize(value)


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _money(value):
    return f"{format(value, 'f')} USD"


def _money_value(value):
    number = _num(value)
    return _money(number) if number is not None else "unverified"


def _digest(value):
    return value if isinstance(value, str) and re.fullmatch(r"(?:sha256:)?[a-f0-9]{7,64}", value, re.I) else "unverified"


def _json(value):
    safe = _safe_value(value)
    body = json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True)
    longest = max((len(match.group(0)) for match in re.finditer(r"\x60+", body)), default=0)
    fence = _BT * max(3, longest + 1)
    return f"{fence}json\n{body}\n{fence}"


def _cell(value):
    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ").strip()
