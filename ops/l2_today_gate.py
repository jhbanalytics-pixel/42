import argparse
import datetime as dt
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo
from core.brief.job import MIN_EVIDENCE
from core.brief.evidence import OFFSETS, WINDOW_DAYS
from core.brief.market_scope import QUERIES
from core.brief.specificity import local_posts, specificity_basis
from core.detect.sqlrun import AGENT, CORE
from core.trust.claims import located_market, source_market

DAY = dt.date(2026, 10, 1)
OCT2_DAY = dt.date(2026, 10, 2)
GATE_BASELINE_HEAD = "8c645ffae9c712db9b05b6a8387cdf194efde5fc"
PROJECT = "ogilvy-trends-v2"
BUILDER = "f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com"
SAST = ZoneInfo("Africa/Johannesburg")
OCT2_DEADLINE = dt.time(7, 0)
TOTAL_CAP, QUERY_CAP = 128 * 1024 * 1024, 64 * 1024 * 1024
MARKETS = {"ZA", "NG", "KE"}
PRIMARY_REPORT_PATH = Path(r"C:\Users\AlbertMeintjes\dev\42-inputs\L2-GATE-2026-10-01.md")
PRIMARY_RECEIPT_BASE = (Path(__file__).resolve().parents[1] / "test-results" / "l2"
    / "gate_after_brief_20261001.marker.json")
OCT1_ACCEPTANCE_REPORT_PATH = Path(r"C:\Users\AlbertMeintjes\dev\42-inputs\BRIEF-ACCEPT-2026-10-01.md")
OCT2_ACCEPTANCE_REPORT_PATH = Path(r"C:\Users\AlbertMeintjes\dev\42-inputs\MORNING-L2-2026-10-02.md")
OCT2_RECEIPT_BASE = (Path(__file__).resolve().parents[1] / "test-results" / "l2"
    / "morning_l2_20261002.marker.json")
URL_IN_TEXT = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)

class Stop(Exception):
    def __init__(self, reason, status="blocked"):
        super().__init__(reason)
        self.reason, self.status = reason, status

def _paths(day=DAY):
    if day == DAY:
        return OCT1_ACCEPTANCE_REPORT_PATH, PRIMARY_RECEIPT_BASE
    if day == OCT2_DAY:
        return OCT2_ACCEPTANCE_REPORT_PATH, OCT2_RECEIPT_BASE
    raise Stop("outside_sast_date")

def _validate_cli_paths(report_path, receipt_base, day=DAY):
    report_path, receipt_base = Path(report_path).resolve(), Path(receipt_base).resolve()
    expected_report, expected_receipt = _paths(day)
    if report_path != expected_report.resolve() or receipt_base != expected_receipt.resolve():
        raise Stop("output_paths_outside_allowlist")
    return report_path, receipt_base

def _report_heading(day):
    return f"### Recovered read only Today gate, {day.day} {day.strftime('%B %Y')}"

def _operator_sha256():
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()

def _run_paths(marker_path, run_id):
    key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    stem = marker_path.stem
    return (marker_path.with_name(f"{stem}.{key}.receipt.json"),
            marker_path.with_name(f"{stem}.{key}.claim"))

def _claim_run(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise Stop("run_claim_unavailable", "running") from exc
    return handle

def _release_run_claim(handle):
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()

def _completed_report_data(report_path, run_id, operator_hash, day=DAY):
    if not report_path.exists():
        return None
    heading = _report_heading(day)
    decoder = json.JSONDecoder()
    text = report_path.read_text(encoding="utf-8")
    for section in text.split(heading)[1:]:
        start = section.find("\n{")
        if start < 0:
            continue
        try:
            data, _ = decoder.raw_decode(section[start + 1:].lstrip())
        except ValueError:
            continue
        completion = data.get("completion") if isinstance(data, dict) else None
        if (isinstance(completion, dict) and completion.get("run_id") == run_id
                and data.get("status") == "completed" and data.get("operator_sha256") == operator_hash):
            return data
    return None

def _write_run_receipt(path, run_id, report_path, report_bytes, data, recovered=False, day=DAY):
    receipt = {
        "status": "completed", "date": day.isoformat(), "run_id": run_id,
        "report": str(report_path), "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "operator_sha256": data["operator_sha256"], "gate_baseline_head": GATE_BASELINE_HEAD,
        "query_job_ids": [x["job_id"] for x in data["queries"]], "billed": data["billed"],
        "finished": data["at"], "recovered_from_report": recovered,
    }
    with path.open("x", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)
    return receipt

def _guard(now, day=None):
    if now.tzinfo is None:
        raise Stop("timezone_required")
    now = now.astimezone(SAST)
    if now.date() not in {DAY, OCT2_DAY} or (day is not None and now.date() != day):
        raise Stop("outside_sast_date")
    if now.date() == OCT2_DAY and now.time().replace(tzinfo=None) >= OCT2_DEADLINE:
        raise Stop("outside_acceptance_deadline")
    return now

def _client():
    import google.auth
    from google.cloud import bigquery
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if getattr(credentials, "service_account_email", None) != BUILDER:
        raise Stop("wrong_adc_identity")
    return bigquery.Client(project=PROJECT, credentials=credentials), bigquery

def _read(client, bq, sql, params, kind, receipts, limit):
    cap = min(QUERY_CAP, limit, TOTAL_CAP - sum(x["billed"] for x in receipts))
    if cap <= 0:
        raise Stop("aggregate_read_budget_exhausted")
    opts = {"use_query_cache": False, "maximum_bytes_billed": cap, "query_parameters": params}
    dry = client.query(sql, job_config=bq.QueryJobConfig(dry_run=True, **opts))
    estimate = getattr(dry, "total_bytes_processed", None)
    if dry.statement_type != "SELECT" or isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0:
        raise Stop("select_dry_run_invalid")
    if estimate > cap:
        raise Stop("aggregate_read_budget_would_be_exceeded")
    job = client.query(sql, job_config=bq.QueryJobConfig(dry_run=False, **opts))
    if job.statement_type != "SELECT":
        raise Stop("non_select_detected")
    rows = [dict(row) for row in job.result(timeout=90)]
    processed, billed = job.total_bytes_processed, job.total_bytes_billed
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (processed, billed)):
        raise Stop("read_byte_accounting_unavailable")
    receipts.append({"kind": kind, "job_id": job.job_id, "estimate": estimate,
                     "processed": processed, "billed": billed, "cap": cap, "rows": len(rows)})
    if billed > cap or sum(x["billed"] for x in receipts) > TOTAL_CAP:
        raise Stop("aggregate_read_budget_exceeded")
    return rows

def _completion_sql():
    q = chr(96)
    return f"""WITH per_run AS (
SELECT run_id,run_date,stage,status,started_at,finished_at,error IS NOT NULL error_present
FROM {q}{AGENT}.runs{q} WHERE run_date=@d AND stage='brief'
QUALIFY ROW_NUMBER() OVER(PARTITION BY run_id ORDER BY COALESCE(finished_at,started_at) DESC,started_at DESC)=1
)
SELECT run_id,run_date,stage,status,started_at,finished_at,error_present FROM per_run
QUALIFY ROW_NUMBER() OVER(PARTITION BY stage,run_date ORDER BY started_at DESC,COALESCE(finished_at,started_at) DESC,run_id DESC)=1"""

def _current_sql():
    q = chr(96)
    return f"SELECT brief_date,market,run_id,published_at,status,payload,rule_version FROM {q}{AGENT}.v_briefs_current{q} WHERE brief_date=@d AND run_id=@rid ORDER BY market"

def _scope_sql(items, bq, day=DAY):
    q = chr(96)
    base = QUERIES["market_scope"].format(core=f"{q}{CORE}{q}", agent=f"{q}{AGENT}{q}").strip().rstrip(";")
    base = base.replace(f"{q}{CORE}{q}.", f"{q}{CORE}.")
    base = re.sub(q + "(" + re.escape(CORE) + r"\.[A-Za-z_]+)(?![A-Za-z_])", lambda m: q + m.group(1) + q, base)
    parts, params = [], []
    for i, (market, item) in enumerate(items):
        zone = dt.timezone(dt.timedelta(hours=OFFSETS[market]))
        start = dt.datetime.combine(day - dt.timedelta(days=WINDOW_DAYS - 1), dt.time(), zone)
        values = {"item_id": ("STRING", item["item_id"]), "market": ("STRING", market), "d": ("DATE", day),
                  "start": ("TIMESTAMP", start), "end": ("TIMESTAMP", start + dt.timedelta(days=WINDOW_DAYS))}
        sql = base
        for name, (kind, value) in values.items():
            sql = re.sub("@" + name + r"\b", "@" + name + "_" + str(i), sql)
            params.append(bq.ScalarQueryParameter(name + "_" + str(i), kind, value))
        parts.append(f"SELECT {i} card_index,scope.* FROM ({sql}) scope")
    return "\nUNION ALL\n".join(parts), params

def public_reasons(card, market):
    reasons, explanation, public = [], card.get("explanation"), card.get("specificity")
    if card.get("explained") is not True or card.get("explanation_status") != "explained":
        reasons.append("explanation_not_exactly_explained")
    if not isinstance(explanation, str) or not explanation.strip():
        reasons.append("explanation_missing")
    if not isinstance(public, dict):
        return reasons + ["specificity_missing_or_malformed"]
    if public.get("status") != "pass":
        reasons.append("specificity_status_not_pass")
    if public.get("reason") is not None:
        reasons.append("specificity_reason_present")
    if public.get("why_now") != explanation:
        reasons.append("specificity_why_now_mismatch")
    basis = specificity_basis(explanation=explanation, claims=card.get("claims"),
        explanation_claim_ids=card.get("explanation_claim_ids"), evidence=card.get("evidence"), market=market)
    if basis["reason"] is not None:
        reasons.append("specificity_basis_" + basis["reason"])
    if public != {"status": "pass" if basis["reason"] is None else "fail", **basis}:
        reasons.append("specificity_contract_mismatch")
    return list(dict.fromkeys(reasons))

def _evidence_view(record, market):
    located = located_market(record)
    sourced = source_market(record)
    basis = "located_and_source_market" if located == market and sourced == market else (
        "located" if located == market else "source_market_feed" if sourced == market else "not_local")
    link_check = _public_url_check(record.get("url"))
    result = {key: record.get(key) for key in (
        "id", "platform", "handle", "posted_at", "market", "source_market",
        "text", "quote_text", "flags")} | {"market_basis": basis}
    result["url"] = record.get("url") if link_check["status"] == "pass" else None
    redacted_fields = []
    for key in ("platform", "handle", "posted_at", "market", "source_market", "text", "quote_text", "flags"):
        result[key], redacted = _scrub_rejected_urls(result[key])
        if redacted:
            redacted_fields.append(key)
    result["redacted_fields"] = redacted_fields
    result["link_check"] = link_check
    return result

def _scrub_rejected_urls(value):
    if isinstance(value, dict):
        cleaned, changed = {}, False
        for key, item in value.items():
            safe_key, key_changed = _scrub_rejected_urls(key)
            safe_item, item_changed = _scrub_rejected_urls(item)
            if safe_key in cleaned:
                original_key, suffix = safe_key, 2
                while safe_key in cleaned:
                    safe_key = f"{original_key} [duplicate {suffix}]"
                    suffix += 1
                key_changed = True
            cleaned[safe_key] = safe_item
            changed = changed or key_changed or item_changed
        return cleaned, changed
    if isinstance(value, tuple):
        cleaned, changed = [], False
        for item in value:
            result, item_changed = _scrub_rejected_urls(item)
            cleaned.append(result)
            changed = changed or item_changed
        return tuple(cleaned), changed
    if isinstance(value, (list, tuple)):
        cleaned, changed = [], False
        for item in value:
            result, item_changed = _scrub_rejected_urls(item)
            cleaned.append(result)
            changed = changed or item_changed
        return cleaned, changed
    if not isinstance(value, str):
        return value, False
    changed = False
    def replace(match):
        nonlocal changed
        raw_url = match.group(0)
        candidate = raw_url.rstrip(".,;:!?)")
        suffix = raw_url[len(candidate):]
        if candidate and _public_url_check(candidate)["status"] != "pass":
            changed = True
            return "[rejected URL omitted]" + suffix
        return raw_url
    return URL_IN_TEXT.sub(replace, value), changed

def _public_url_check(value):
    reasons = []
    if not isinstance(value, str) or not value.strip():
        return {"status": "fail", "reasons": ["url_missing"]}
    if value != value.strip():
        reasons.append("url_whitespace")
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            reasons.append("url_scheme_not_http")
        if not parsed.hostname:
            reasons.append("url_host_missing")
        if parsed.username is not None or parsed.password is not None:
            reasons.append("url_embedded_credentials")
        try:
            parsed.port
        except ValueError:
            reasons.append("url_port_invalid")
        query_sources = [("query", parsed.query)]
        query_sources.extend(("fragment", part) for part in parsed.fragment.split("?") if "=" in part)
        for source, query in query_sources:
            for key, _ in parse_qsl(query, keep_blank_values=True):
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in {"key", "auth", "sig"} or any(token in normalized for token in (
                        "token", "jwt", "apikey", "secret", "password", "credential", "authorization",
                        "signature", "bearer")):
                    reasons.append("url_credential_fragment_key" if source == "fragment"
                        else "url_credential_query_key")
                    break
    except ValueError:
        reasons.append("url_invalid")
    reasons = list(dict.fromkeys(reasons))
    return {"status": "fail" if reasons else "pass", "reasons": reasons}

def published_evidence_audit(card, market):
    specificity = card.get("specificity")
    evidence = card.get("evidence")
    records = evidence if isinstance(evidence, list) else []
    by_id, duplicate_ids = {}, set()
    for record in records:
        if not isinstance(record, dict):
            continue
        evidence_id = record.get("id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            continue
        if evidence_id in by_id:
            duplicate_ids.add(evidence_id)
        else:
            by_id[evidence_id] = record
    local_by_id = {record["id"]: record for record in local_posts(records, market)}
    expected = specificity_basis(explanation=card.get("explanation"), claims=card.get("claims"),
        explanation_claim_ids=card.get("explanation_claim_ids"), evidence=records, market=market)
    contract_reasons = public_reasons(card, market)
    raw_requested = specificity.get("local_evidence_ids") if isinstance(specificity, dict) else None
    local_reasons, local_examples = [], []
    if not isinstance(raw_requested, list):
        local_reasons.append("local_evidence_ids_missing_or_malformed")
        local_examples.append({"evidence_id": None, "status": "fail",
            "reason": "local_evidence_ids_missing"})
        raw_requested = []
    requested = []
    for value in raw_requested:
        if not isinstance(value, str) or not value.strip():
            local_reasons.append("local_evidence_id_missing" if value is None
                else "local_evidence_id_invalid")
            local_examples.append({"evidence_id": value, "status": "fail",
                "reason": "evidence_id_missing" if value is None else "evidence_id_invalid"})
        else:
            requested.append(value)
    if len(set(requested)) != len(requested):
        local_reasons.append("local_evidence_id_duplicate")
    for evidence_id in requested:
        record = by_id.get(evidence_id)
        if evidence_id in duplicate_ids:
            local_reasons.append("local_evidence_id_ambiguous")
            local_examples.append({"evidence_id": evidence_id, "status": "fail",
                "reason": "duplicate_evidence_id"})
        elif record is None:
            local_reasons.append("local_evidence_id_dangling")
            local_examples.append({"evidence_id": evidence_id, "status": "fail",
                "reason": "evidence_missing"})
        elif evidence_id not in local_by_id:
            local_reasons.append("local_evidence_not_local")
            local_examples.append({"evidence_id": evidence_id, "status": "fail",
                "reason": "evidence_not_attributed_to_market",
                "evidence": _evidence_view(record, market)})
        else:
            linked = _evidence_view(record, market)
            if linked["link_check"]["status"] != "pass":
                local_reasons.append("local_evidence_url_invalid")
            if linked["redacted_fields"]:
                local_reasons.append("local_evidence_contains_rejected_url")
            valid_example = linked["link_check"]["status"] == "pass" and not linked["redacted_fields"]
            local_examples.append({"evidence_id": evidence_id,
                "status": "pass" if valid_example else "fail",
                "reason": None if valid_example else "source_url_invalid" if linked["link_check"]["status"] != "pass"
                    else "source_text_contains_rejected_url", "evidence": linked})
    if len([x for x in local_examples if x["status"] == "pass"]) < 2:
        local_reasons.append("fewer_than_two_joined_local_examples")
    local_reasons = list(dict.fromkeys(local_reasons))
    quote = specificity.get("quote") if isinstance(specificity, dict) else None
    quote_reasons = []
    quote_view = None
    if not isinstance(quote, dict):
        quote_reasons.append("quote_missing_or_malformed")
    else:
        evidence_id, quote_text = quote.get("evidence_id"), quote.get("text")
        record = by_id.get(evidence_id) if isinstance(evidence_id, str) else None
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            quote_reasons.append("quote_evidence_id_missing")
        elif evidence_id in duplicate_ids:
            quote_reasons.append("quote_evidence_id_ambiguous")
        elif record is None:
            quote_reasons.append("quote_evidence_dangling")
        elif evidence_id not in local_by_id:
            quote_reasons.append("quote_evidence_not_local")
        source_text = None if record is None else (
            record.get("quote_text") if isinstance(record.get("quote_text"), str)
            and record.get("quote_text").strip() else record.get("text"))
        if not isinstance(quote_text, str) or not isinstance(source_text, str) or quote_text not in source_text:
            quote_reasons.append("quote_not_verbatim")
        if expected.get("quote") != quote:
            quote_reasons.append("quote_not_in_claim_basis")
        quote_text_display, quote_text_redacted = _scrub_rejected_urls(quote_text)
        if quote_text_redacted:
            quote_reasons.append("quote_text_contains_rejected_url")
        if record is not None:
            linked = _evidence_view(record, market)
            if linked["link_check"]["status"] != "pass":
                quote_reasons.append("quote_source_url_invalid")
            if linked["redacted_fields"]:
                quote_reasons.append("quote_source_contains_rejected_url")
            quote_view = {"evidence_id": evidence_id, "text": quote_text_display,
                "status": "fail" if quote_reasons else "pass", "reasons": list(dict.fromkeys(quote_reasons)),
                "url": linked["url"], "link_check": linked["link_check"], "evidence": linked}
    quote_reasons = list(dict.fromkeys(quote_reasons))
    if quote_view is None:
        quote_text_display, quote_text_redacted = _scrub_rejected_urls(
            quote.get("text") if isinstance(quote, dict) else None)
        if quote_text_redacted:
            quote_reasons.append("quote_text_contains_rejected_url")
        quote_view = {"evidence_id": quote.get("evidence_id") if isinstance(quote, dict) else None,
            "text": quote_text_display,
            "status": "fail", "reasons": quote_reasons}
    explanation = card.get("explanation")
    public_why_now = specificity.get("why_now") if isinstance(specificity, dict) else None
    explanation_display, explanation_redacted = _scrub_rejected_urls(explanation)
    public_why_now_display, public_why_now_redacted = _scrub_rejected_urls(public_why_now)
    why_reasons = []
    if card.get("explained") is not True or card.get("explanation_status") != "explained":
        why_reasons.append("explanation_not_exactly_explained")
    if not isinstance(explanation, str) or not explanation.strip():
        why_reasons.append("explanation_missing")
    if public_why_now != explanation:
        why_reasons.append("specificity_why_now_mismatch")
    if explanation_redacted or public_why_now_redacted:
        why_reasons.append("why_now_contains_rejected_url")
    why_reasons = list(dict.fromkeys(why_reasons))
    floor_reasons = []
    if not isinstance(evidence, list) or any(not isinstance(record, dict) for record in records):
        floor_reasons.append("evidence_missing_or_malformed")
        showable, local = [], []
    else:
        local_ids = set(local_by_id)
        showable = [record for record in records
            if located_market(record) is None or record.get("id") in local_ids]
        local = list(local_by_id.values())
    if len(showable) < MIN_EVIDENCE:
        floor_reasons.append("fewer_than_three_showable_posts")
    if len(local) < 2:
        floor_reasons.append("fewer_than_two_local_posts")
    return {
        "specificity_status": specificity.get("status") if isinstance(specificity, dict) else None,
        "specificity_reasons": contract_reasons,
        "quote": quote_view,
        "local_examples": local_examples,
        "why_now": explanation_display,
        "public_why_now": public_why_now_display,
        "checks": {
            "specificity_contract": "pass" if not contract_reasons else "fail",
            "quote": "pass" if not quote_reasons else "fail",
            "local_examples": "pass" if not local_reasons else "fail",
            "why_now_contract": "pass" if not why_reasons else "fail",
            "critic_local_why_now_attestation": "unavailable_not_persisted",
            "producer_showable_post_floor": {
                "status": "pass" if not floor_reasons else "fail",
                "showable_posts": len(showable), "local_posts": len(local),
                "showable_evidence_ids": [record.get("id") for record in showable],
                "local_evidence_ids": [record["id"] for record in local],
                "reasons": floor_reasons,
                "three_day_main_platform_validity": "unavailable_not_persisted",
            },
        },
    }

def _render(r, day=DAY):
    data = {k: r[k] for k in ("completion", "snapshots", "results", "held_results", "counts",
        "market_counts", "full_publication_status", "gate_baseline_head", "operator_sha256", "queries", "billed")}
    data, _ = _scrub_rejected_urls(data)
    header, _ = _scrub_rejected_urls([_report_heading(day), "",
        f"State: {r['status']}. Reason: {r.get('reason') or 'none'}. Gate baseline head: {GATE_BASELINE_HEAD}.",
        f"Operator SHA-256: {r['operator_sha256']}.",
        f"Observed SAST: {r['at']}. Model calls: 0. Table writes: 0.",
        "Producer placement counts come directly from cards plus more and held_back.items. The independent current-scope results are separate.",
        "Seven day scope uses the current SQL, two posts per creator, twelve posts per item, and a strict majority. Snapshot time is separate.",
        "Held evidence counts persisted market-attributed posts. source_market means feed attribution, not physical location. Held payloads omit explanation, claims, and specificity, so claim support, quote, why now, and specificity verdicts remain unavailable.",
        "Post counts are posts, not creators. No specificity failure or missing source data is inferred from a held payload.",
        "Published-card quote and local-example details are joined to stored evidence. The producer 3-post and 2-local-post floor is reproduced from the payload. Separate three-day main-platform validity is unavailable because it is not persisted. This report does not claim full publication readiness.",
        ""])
    rendered, _ = _scrub_rejected_urls("\n".join([*header, json.dumps(data, indent=2, default=str), ""]))
    return rendered

def run_gate(now=None, report_path=None, marker_path=None, client_factory=None, day=DAY):
    if report_path is None or marker_path is None:
        return {"status": "blocked", "reason": "output_paths_required", "query_job_ids": []}
    now = _guard(now or dt.datetime.now(SAST), day)
    report_path, marker_path = Path(report_path), Path(marker_path)
    queries = []
    operator_hash = _operator_sha256()
    def pending(status, reason):
        return {"status": status, "reason": reason, "query_job_ids": [x["job_id"] for x in queries]}
    claim_handle = None
    receipt_path = None
    def release_claim():
        nonlocal claim_handle
        _release_run_claim(claim_handle)
        claim_handle = None
    try:
        client, bq = (client_factory or _client)()
        rows = _read(client, bq, _completion_sql(), [bq.ScalarQueryParameter("d", "DATE", day)],
                     "brief_terminal", queries, QUERY_CAP)
        if len(rows) != 1:
            return pending("not_ready", "latest_brief_missing")
        completion = rows[0]
        if (completion.get("stage") != "brief" or completion.get("run_date") != day
                or completion.get("status") != "ok" or completion.get("finished_at") is None
                or not completion.get("run_id")):
            return pending("not_ready", "latest_brief_not_complete")
        receipt_path, claim_path = _run_paths(marker_path, completion["run_id"])
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (receipt.get("status") != "completed" or receipt.get("run_id") != completion["run_id"]
                    or receipt.get("date") != day.isoformat()):
                raise Stop("run_receipt_conflict")
            return {"status": "already_used", "marker_status": "completed",
                    "receipt_path": str(receipt_path), "source_receipt_path": str(receipt_path),
                    "source_report_path": receipt.get("report"),
                    "source_report_sha256": receipt.get("report_sha256"),
                    "report_path": str(report_path)}
        brief_rows = _read(client, bq, _current_sql(), [
            bq.ScalarQueryParameter("d", "DATE", day),
            bq.ScalarQueryParameter("rid", "STRING", completion["run_id"])],
            "brief_rows_same_run", queries, QUERY_CAP)
        markets = [str(x.get("market") or "").strip().upper() for x in brief_rows]
        if any(x.get("run_id") != completion["run_id"] or x.get("brief_date") != day for x in brief_rows):
            release_claim()
            return pending("not_ready", "current_briefs_not_same_completed_run")
        if len(brief_rows) != 3 or len(set(markets)) != 3 or set(markets) != MARKETS:
            return pending("not_ready", "current_brief_markets_or_run_incomplete")
        claim_handle = _claim_run(claim_path)
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt.get("status") != "completed" or receipt.get("run_id") != completion["run_id"]:
                raise Stop("run_receipt_conflict")
            release_claim()
            return {"status": "already_used", "marker_status": "completed",
                    "receipt_path": str(receipt_path)}
        existing = _completed_report_data(report_path, completion["run_id"], operator_hash, day)
        if existing is not None:
            _write_run_receipt(receipt_path, completion["run_id"], report_path,
                report_path.read_bytes(), existing, recovered=True, day=day)
            release_claim()
            return {"status": "already_used", "marker_status": "completed",
                    "receipt_path": str(receipt_path), "recovered_receipt": True}
    except Stop as exc:
        release_claim()
        return pending(exc.status, exc.reason)
    except Exception:
        release_claim()
        return pending("blocked", "native_read_failed")
    snapshots, cards, held, seen, results, held_results = [], [], [], set(), [], []
    def record_report(status, reason=None):
        snapshots_by_market = {item["market"]: item for item in snapshots}
        unavailable_empty_markets = {
            market for market, item in snapshots_by_market.items()
            if item.get("payload_status") in {"partial", "data_issue"}
            and item.get("cards") == 0 and item.get("more") == 0 and item.get("held") == 0
        }
        d, hm, hr = Counter(x["result"] for x in results), Counter(), Counter()
        for market, item, _ in held:
            hm[market] += 1
            hr[str(item.get("reason") or item.get("rule") or "unknown")] += 1
        counts = {"cards": len(cards),
            "published_cards": sum(source == "cards" for _, _, source, _ in cards),
            "published_more": sum(source == "more" for _, _, source, _ in cards),
            "published_items": len(cards), "global": d["Global Discover"], "unmeasured": d["Unmeasured source population"],
            "g10": d["G10 hold"], "cross_market": d["Cross-market outside Today gate"],
            "majority_specificity_only": d["Majority and specificity pass, producer floor unproven"],
            "producer_floor_not_met": d["Producer evidence floor not met"],
            "held": len(held), "held_market": dict(hm), "held_reason": dict(hr)}
        counts["placement_counts_status"] = "unavailable_empty_partial_payload" if unavailable_empty_markets else "observed"
        if unavailable_empty_markets:
            for key in ("cards", "published_cards", "published_more", "published_items", "global",
                    "unmeasured", "g10", "cross_market", "majority_specificity_only",
                    "producer_floor_not_met", "held", "held_market", "held_reason"):
                counts[key] = None
        market_counts = {}
        result_names = {
            "global_discover": "Global Discover",
            "unmeasured": "Unmeasured source population",
            "g10_hold": "G10 hold",
            "majority_specificity_only": "Majority and specificity pass, producer floor unproven",
            "producer_floor_not_met": "Producer evidence floor not met",
            "cross_market_outside_today_gate": "Cross-market outside Today gate",
        }
        for market in sorted(MARKETS):
            market_results = [item for item in results if item["market"] == market]
            raw_public = [item for item in cards if item[0] == market]
            result_counts = {key: sum(item["result"] == value for item in market_results)
                for key, value in result_names.items()}
            classified_public = sum(result_counts.values())
            market_held = [item for item in held_results if item["market"] == market]
            raw_held = [item for item in held if item[0] == market]
            reason_counts = Counter(str(item["stored_hold"].get("reason") or
                item["stored_hold"].get("rule") or "unknown") for item in market_held)
            reason_text_counts = Counter(str(item["stored_hold"].get("reason_text") or "unknown")
                for item in market_held)
            top_reasons = [{"reason": key, "count": value} for key, value in
                sorted(reason_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]]
            snapshot = snapshots_by_market.get(market, {})
            unavailable_empty = market in unavailable_empty_markets
            placement_status = "unavailable_empty_partial_payload" if unavailable_empty else (
                "observed_partial_payload" if snapshot.get("payload_status") in {"partial", "data_issue"}
                else "observed")
            market_counts[market] = {
                "public": {
                    "cards": None if unavailable_empty else sum(source == "cards" for _, _, source, _ in raw_public),
                    "more": None if unavailable_empty else sum(source == "more" for _, _, source, _ in raw_public),
                    "raw_items": None if unavailable_empty else len(raw_public),
                    "classified_items": None if unavailable_empty else classified_public,
                    "result_counts": None if unavailable_empty else result_counts,
                    "reconciles": None if unavailable_empty else len(raw_public) == classified_public,
                },
                "stored_held": {
                    "raw_items": None if unavailable_empty else len(raw_held),
                    "classified_items": None if unavailable_empty else len(market_held),
                    "reason_total": None if unavailable_empty else sum(reason_counts.values()),
                    "reason_counts": None if unavailable_empty else dict(reason_counts),
                    "top_reason_totals": None if unavailable_empty else top_reasons,
                    "reconciles": None if unavailable_empty else len(raw_held) == len(market_held) == sum(reason_counts.values()),
                },
                "producer_placement": {
                    "status": placement_status, "payload_status": snapshot.get("payload_status"),
                    "published_cards": None if unavailable_empty else sum(source == "cards" for _, _, source, _ in raw_public),
                    "published_more": None if unavailable_empty else sum(source == "more" for _, _, source, _ in raw_public),
                    "published_items": None if unavailable_empty else len(raw_public),
                    "held_items": None if unavailable_empty else len(raw_held),
                    "held_reason_counts": None if unavailable_empty else dict(reason_counts),
                    "held_reason_text_counts": None if unavailable_empty else dict(reason_text_counts),
                    "reconciles": None if unavailable_empty else sum(source == "cards" for _, _, source, _ in raw_public)
                        + sum(source == "more" for _, _, source, _ in raw_public) == len(raw_public)
                        and len(raw_held) == sum(reason_counts.values()),
                },
            }
        report = {"status": status, "reason": reason, "at": now.isoformat(), "completion": completion,
            "snapshots": snapshots, "results": results, "held_results": held_results, "counts": counts,
            "market_counts": market_counts, "gate_baseline_head": GATE_BASELINE_HEAD,
            "full_publication_status": "unproven", "operator_sha256": operator_hash,
            "queries": queries, "billed": sum(x["billed"] for x in queries)}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        section = _render(report, day)
        prefix = ""
        if report_path.exists():
            with report_path.open("r", encoding="utf-8", newline="") as f:
                prefix = f.read()
        append_text = (("" if prefix.endswith("\n") else "\n") + "\n" if prefix else "") + section
        with report_path.open("a", encoding="utf-8", newline="") as f:
            f.write(append_text)
        report_bytes = report_path.read_bytes()
        if status == "completed":
            _write_run_receipt(receipt_path, completion["run_id"], report_path,
                report_bytes, report, day=day)
        return {"status": status, "reason": reason, "report_path": str(report_path),
                "receipt_path": str(receipt_path) if status == "completed" else None}
    def finish(status, reason=None):
        try:
            return record_report(status, reason)
        finally:
            release_claim()
    try:
        for row in brief_rows:
            payload = row["payload"]
            payload = json.loads(payload) if isinstance(payload, str) else payload
            if not isinstance(payload, dict):
                raise Stop("brief_payload_invalid")
            market = str(row.get("market") or "").strip().upper()
            if not market:
                raise Stop("brief_market_missing")
            arr, more = payload.get("cards") or [], payload.get("more") or []
            block = payload.get("held_back") or {}
            held_items = block.get("items", []) if isinstance(block, dict) else block
            if not isinstance(arr, list) or not isinstance(more, list) or not isinstance(held_items, list):
                raise Stop("brief_payload_shape_invalid")
            if any(not isinstance(item, dict) for item in held_items):
                raise Stop("held_item_invalid")
            banners = payload.get("banners")
            data_issue_banners = [x for x in banners if isinstance(x, dict) and x.get("kind") == "data_issue"] \
                if isinstance(banners, list) else []
            payload_status = payload.get("status")
            snapshots.append({"market": market, "run_id": row["run_id"], "published_at": row.get("published_at"),
                "status": row.get("status"), "payload_status": payload_status,
                "payload_partial_status": "partial" if payload_status == "partial" else
                    "not_partial" if isinstance(payload_status, str) else "unavailable",
                "data_issue_status": "present" if payload_status == "data_issue" or data_issue_banners else
                    "absent_from_payload" if isinstance(banners, list) else "unavailable",
                "data_issue_banners": data_issue_banners,
                "coverage_issues": (payload.get("coverage") or {}).get("issues")
                    if isinstance(payload.get("coverage"), dict) else None,
                "cards": len(arr), "more": len(more), "held": len(held_items)})
            for item in held_items:
                item_id = item.get("item_id")
                if not isinstance(item_id, str) or not item_id.strip():
                    raise Stop("held_item_invalid")
                held.append((market, item, row))
            for source, entries in (("cards", arr), ("more", more)):
                for card in entries:
                    item_id = card.get("item_id") if isinstance(card, dict) else None
                    if not isinstance(item_id, str) or not item_id.strip() or (market, item_id) in seen:
                        raise Stop("stored_card_invalid_or_duplicate")
                    seen.add((market, item_id))
                    cards.append((market, card, source, row))
        selected = [(m, c) for m, c, _, _ in cards if m in MARKETS]
        held_selected = [(m, item) for m, item, _ in held if m in MARKETS]
        scope_items = selected + held_selected
        if scope_items:
            sql, params = _scope_sql(scope_items, bq, day)
            scoped = _read(client, bq, sql, params, "cards_more_scope", queries, TOTAL_CAP)
            by_index = {x.get("card_index"): x for x in scoped}
            if len(scoped) != len(scope_items) or set(by_index) != set(range(len(scope_items))):
                raise Stop("scope_result_count_mismatch")
            for i, (market, card, source, row) in enumerate(x for x in cards if x[0] in MARKETS):
                scope = by_index[i]
                total, local = scope.get("total_posts7"), scope.get("market_posts7")
                if (isinstance(total, bool) or isinstance(local, bool) or not isinstance(total, int)
                        or not isinstance(local, int) or total < 0 or local < 0 or local > total or total > 12):
                    raise Stop("scope_counts_invalid")
                majority = total > 0 and local * 2 > total
                reasons = public_reasons(card, market) if majority else []
                producer_floor = "not_met_from_current_candidate_count" if total < MIN_EVIDENCE else "unproven_showability_set_not_rechecked"
                unmeasured = total == 0
                result = ("Unmeasured source population" if unmeasured else
                    "G10 hold" if majority and reasons else
                    "Producer evidence floor not met" if majority and total < MIN_EVIDENCE else
                    "Majority and specificity pass, producer floor unproven" if majority else "Global Discover")
                results.append({"market": market, "item_id": card["item_id"], "title": card.get("title"),
                    "stored_at": row.get("published_at"), "stored_scope": card.get("market_scope"),
                    "scope": "market" if majority else "unmeasured" if unmeasured else "global", "local": local, "total": total,
                    "share": local / total if total else None, "share_status": "measured" if total else "unmeasured",
                    "result": result,
                    "specificity": (card.get("specificity") or {}).get("status") if isinstance(card.get("specificity"), dict) else None,
                    "publication_evidence": published_evidence_audit(card, market),
                    "majority_status": "pass" if majority else "fail" if total else "unmeasured",
                    "producer_floor": producer_floor, "would_publish": "unproven",
                    "showability_evidence_needed": "the producer showable post set",
                    "basis_match": not reasons if majority else None, "reasons": reasons})
            for i, (market, item, row) in enumerate((x for x in held if x[0] in MARKETS), start=len(selected)):
                scope = by_index[i]
                total, local = scope.get("total_posts7"), scope.get("market_posts7")
                if (isinstance(total, bool) or isinstance(local, bool) or not isinstance(total, int)
                        or not isinstance(local, int) or total < 0 or local < 0 or local > total or total > 12):
                    raise Stop("scope_counts_invalid")
                attributed = []
                for post in local_posts(item.get("evidence"), market):
                    located = located_market(post) == market
                    sourced = source_market(post) == market
                    basis = "located_and_source_market" if located and sourced else "located" if located else "source_market_feed"
                    attributed.append({"evidence_id": post["id"], "basis": basis})
                held_results.append({
                    "market": market, "item_id": item["item_id"], "title": item.get("title"),
                    "stored_hold": {key: item.get(key) for key in ("rule", "reason", "reason_text")},
                    "stored_at": row.get("published_at"),
                    "current_scope": "market" if total > 0 and local * 2 > total else "global" if total > 0 else "unmeasured",
                    "local": local, "total": total, "share": local / total if total else None,
                    "share_status": "measured" if total else "unmeasured", "basis_unit": "posts",
                    "market_attributed_posts": attributed,
                    "producer_showability_verdict": "unavailable_held_payload_does_not_persist_full_showability_basis",
                     "showability_evidence_needed": "the producer showable post set",
                    "evidence_gaps": {
                        "claim_support_verdict": "unavailable_claims_not_persisted",
                        "quote_verdict": "unavailable_claims_not_persisted",
                        "why_now_verdict": "unavailable_explanation_not_persisted",
                        "specificity_verdict": "unavailable_claims_explanation_and_specificity_not_persisted",
                    },
                })
        results.extend({"market": m, "item_id": c["item_id"], "title": c.get("title"),
            "stored_at": row.get("published_at"), "stored_scope": c.get("market_scope"),
            "scope": "not evaluated", "local": None, "total": None, "share": None,
            "share_status": "unmeasured",
            "result": "Cross-market outside Today gate", "specificity": None, "basis_match": None, "reasons": []}
            for m, c, _, row in cards if m not in MARKETS)
        return finish("completed")
    except Stop as exc:
        return finish(exc.status, exc.reason)
    except Exception:
        return finish("blocked", "native_read_or_payload_failed")

def main(argv=None, now=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, choices=(DAY.isoformat(), OCT2_DAY.isoformat()))
    parser.add_argument("--report", required=True)
    parser.add_argument("--receipts", required=True)
    args = parser.parse_args(argv)
    day = dt.date.fromisoformat(args.date)
    try:
        run_now = _guard(now or dt.datetime.now(SAST), day)
        report_path, receipt_base = _validate_cli_paths(args.report, args.receipts, day)
    except Stop as exc:
        parser.error(exc.reason)
    result = run_gate(now=run_now, report_path=report_path, marker_path=receipt_base, day=day)
    print(json.dumps(result, default=str))
    return 0 if result["status"] in {"completed", "already_used"} else 2

if __name__ == "__main__":
    raise SystemExit(main())
