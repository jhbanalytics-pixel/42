from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
from contextlib import nullcontext
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from types import SimpleNamespace

from core.eval import ask_r2 as shared


ROOT = Path(__file__).resolve().parents[2]
R3_ROOT = "l3-ask-20260930-r3"
RETRY_KEY = "NOW-01-retry-1"
RETRY_PREFIX = f"{R3_ROOT}-{RETRY_KEY}"
CRE_PREFIX = f"{R3_ROOT}-CRE-01"
BASE_LOCAL_IDS = ("NOW-01", "RISE-02", "WHY-03", "SPR-03")
BASE_MARKER_IDS = (*BASE_LOCAL_IDS, "CRE-01")
BASE_USD = 8.841737
LOCAL_USD = 6.453220
CAP_USD = 10.0
CRE_USD = 2.388517
CRE_RUN_ID = f"{CRE_PREFIX}-spend-6a21605f"
CRE_WHAT = "staging_check_ask:CRE-01"
INITIAL_SOURCE_COMMIT = "15342e3c8faff2a1516443e55ca955403af37f0d"
RESUME_SOURCE_COMMIT = "e72eaef2bb8506df20e7bde102aec6bdd5daeb06"
RESUME_HEALTH_COMMIT = "5db40e553e2cc53260c4495d99850cbddd7f6b6a"
POSTBOOK_AUDIT_SHA256 = "3763c29912b3e394583f506b0ef7f4793b65ec8d1635a41a41a5eb92f4c9278b"
INITIAL_NOW_SHA256 = shared.R3_INITIAL_NOW_SHA256
INITIAL_SOURCE_METADATA_SHA256 = shared.R3_SOURCE_METADATA_SHA256
INITIAL_REPORT_SHA256 = shared.R3_INITIAL_REPORT_SHA256
DATA_DIR = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-data")
SNAPSHOT_PATH = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-interruption-readback.json")
POSTBOOK_AUDIT_PATH = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-postbook-job-audit.json")
INITIAL_REPORT_PATH = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-initial-stop.md")
CURRENT_REPORT_PATH = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3.md")
SOURCE_METADATA_PATH = ROOT / "plans" / "r3-source-metadata.json"
CONTINUATION_KEY = "NOW-01-continuation-1"
CONTINUATION_PREFIX = f"{R3_ROOT}-{CONTINUATION_KEY}"
CONTINUATION_DATA_DIR = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-continuation-1-data")
CONTINUATION_APPROVAL_PATH = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-CONTINUATION-1-APPROVAL.json")
CLOSED_MANIFEST_PATH = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-CLOSED-MANIFEST.json")
CONTINUATION_APPROVAL_SHA256 = "a90af9a933a261c0413dcddba684ec4ad1c3ca3de8d754792200ec8cd0d5e634"
CLOSED_MANIFEST_SHA256 = "44e159b69bfc0bb313a24e39e2e1af68c06e99f1ecc10f48ea2eb5569f98eeea"
PREFUNDED_RECOVERY_PATH = Path(
    "C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R3-CONTINUATION-1-PREFUNDED-RECOVERY.json")
PREFUNDED_RECOVERY_SHA256 = "e45b5f7359c308eb1c6b81c9c715ea9e542c7812d9dfcda59ed3076a24186f10"
CONTINUATION_EXECUTION_MARKER = "NOW-01-continuation-1.execution-started"
CONTINUATION_RESERVATION_RUN_ID = f"{CONTINUATION_PREFIX}-spend-5df280f9"
CONTINUATION_RESERVATION_MICROS = 15_000_000
PREFUNDED_EVIDENCE_NAMES = {
    "ASK-2026-09-30-R3-CONTINUATION-1-SETUP-LOG.txt",
    "ASK-2026-09-30-R3-CONTINUATION-1-SETUP-READBACK.json",
    "ASK-2026-09-30-R3-CONTINUATION-1-DAILY-QUERY-AUDIT.json",
    f"{CONTINUATION_KEY}.attempted",
}
CONTINUATION_NATIVE_IDS = {
    f"{R3_ROOT}-CRE-01-spend-6a21605f",
    f"{R3_ROOT}-NOW-01-retry-1-spend-3f5031e1",
    f"{R3_ROOT}-NOW-01-spend-12638a48",
    f"{R3_ROOT}-RISE-02-spend-8e9d4c20",
    f"{R3_ROOT}-SPR-03-spend-8a5f5498",
    f"{R3_ROOT}-WHY-03-spend-19e2f107",
}
REFUSED = shared.REFUSED
FAILED = shared.FAILED


def _json(path, reason):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        raise shared.OperationRefused(reason) from None


def _sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        raise shared.OperationRefused("recovery_input_missing") from None


def _ledger_micros(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise shared.OperationRefused("daily_spend_preflight_invalid") from None
    if not amount.is_finite() or amount < 0:
        raise shared.OperationRefused("daily_spend_preflight_invalid")
    return int((amount * shared.MICROS).to_integral_value(rounding=ROUND_HALF_UP))


def _lineage(receipt):
    provenance = receipt.get("r3_continuation")
    if not isinstance(provenance, dict):
        raise shared.OperationRefused("r3_local_lineage_missing")
    health = provenance.get("health_at_resume") or {}
    if not (
        provenance.get("evaluation_label") == "R3"
        and provenance.get("initial_source_commit") == INITIAL_SOURCE_COMMIT
        and provenance.get("initial_source_metadata_sha256") == INITIAL_SOURCE_METADATA_SHA256
        and provenance.get("initial_now_record_sha256") == INITIAL_NOW_SHA256
        and provenance.get("initial_report_sha256") == INITIAL_REPORT_SHA256
        and shared._micros(provenance.get("initial_booked_usd")) == shared._micros(1.357150)
        and provenance.get("resume_source_commit") == RESUME_SOURCE_COMMIT
        and provenance.get("resume_source_clean") is True
        and health.get("http_status") == 200
        and health.get("ok") is True
        and health.get("git_commit") == RESUME_HEALTH_COMMIT
        and provenance.get("retry_attempt_key") == RETRY_KEY
        and provenance.get("provider_retry_statuses") == [500, 504]
        and provenance.get("now_retry_provider_retry_statuses") == []
    ):
        raise shared.OperationRefused("r3_local_lineage_mismatch")


def _validate_inputs(data_dir, snapshot_path, source_metadata_path, initial_report_path,
                     current_report_path, postbook_audit_path):
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise shared.OperationRefused("r3_data_missing")
    expected_files = {f"{question_id}.attempted" for question_id in BASE_MARKER_IDS}
    expected_files.update(f"{question_id}.json" for question_id in BASE_LOCAL_IDS)
    if {path.name for path in data_dir.iterdir()} != expected_files:
        raise shared.OperationRefused("r3_attempt_state_changed")
    for question_id in BASE_MARKER_IDS:
        if (data_dir / f"{question_id}.attempted").read_bytes() != b"attempt started\n":
            raise shared.OperationRefused("r3_original_marker_invalid")
    now_path = data_dir / "NOW-01.json"
    if _sha256(now_path) != INITIAL_NOW_SHA256:
        raise shared.OperationRefused("r3_initial_now_changed")
    source_sha = _sha256(source_metadata_path)
    if source_sha != INITIAL_SOURCE_METADATA_SHA256:
        raise shared.OperationRefused("r3_source_metadata_changed")
    source = _json(source_metadata_path, "r3_source_metadata_invalid")
    if source.get("source_commit") != INITIAL_SOURCE_COMMIT or source.get("source_clean") is not True:
        raise shared.OperationRefused("r3_initial_source_mismatch")
    initial_report_sha = _sha256(initial_report_path)
    if initial_report_sha != INITIAL_REPORT_SHA256:
        raise shared.OperationRefused("r3_initial_report_changed")
    current_report_sha = _sha256(current_report_path)
    audit_sha = _sha256(postbook_audit_path)
    if audit_sha != POSTBOOK_AUDIT_SHA256:
        raise shared.OperationRefused("postbook_audit_changed")
    audit = _json(postbook_audit_path, "postbook_audit_invalid")
    jobs = audit.get("query_jobs")
    if not (
        audit.get("verdict") == "NO_POSTBOOK_SEMANTIC_JOBS"
        and audit.get("all_pages_consumed") is True
        and (audit.get("cre_book") or {}).get("run_id") == CRE_RUN_ID
        and isinstance(jobs, list) and len(jobs) == 50
        and all(job.get("state") == "DONE" and job.get("semantic") is False for job in jobs)
        and audit.get("postbook_semantic_jobs") == []
        and audit.get("unresolved_jobs") == []
    ):
        raise shared.OperationRefused("postbook_audit_unproven")

    prior_writes, prior_call_count = [], 0
    local_receipts, input_hashes = {}, {}
    for question_id in BASE_LOCAL_IDS:
        receipt_path = data_dir / f"{question_id}.json"
        input_hashes[receipt_path.name] = _sha256(receipt_path)
        receipt = _json(receipt_path, "r3_local_receipt_invalid")
        if (receipt.get("id") != question_id or receipt.get("attempt_key") != question_id
                or receipt.get("prefix") != f"{R3_ROOT}-{question_id}"):
            raise shared.OperationRefused("r3_local_receipt_identity_mismatch")
        if question_id != "NOW-01":
            _lineage(receipt)
        previous_prefix_root = shared.PREFIX_ROOT
        shared.PREFIX_ROOT = R3_ROOT
        try:
            charge, calls, recoverable = shared._saved_record_micros(
                receipt, question_id, question_id, prior_writes,
                evaluation_label="R3", expected_call_index_base=prior_call_count)
        except (KeyError, TypeError, ValueError, shared.OperationRefused):
            raise shared.OperationRefused("r3_local_receipt_invalid") from None
        finally:
            shared.PREFIX_ROOT = previous_prefix_root
        if charge != shared._micros(receipt.get("charged_usd")):
            raise shared.OperationRefused("r3_local_charge_mismatch")
        if question_id == "NOW-01" and not recoverable:
            raise shared.OperationRefused("r3_initial_now_not_recoverable")
        local_receipts[question_id] = receipt
        prior_call_count += len(calls)
    local_micros = sum(shared._micros(item["model_usd"]) for item in prior_writes)
    if local_micros != shared._micros(LOCAL_USD):
        raise shared.OperationRefused("r3_local_total_mismatch")
    snapshot = _json(snapshot_path, "r3_interruption_snapshot_invalid")
    if snapshot.get("prefix") != CRE_PREFIX:
        raise shared.OperationRefused("r3_interruption_snapshot_mismatch")
    cre_spend = snapshot.get("spend_rows")
    cre_claims = snapshot.get("claim_rows")
    session_rows = snapshot.get("session_rows")
    if not isinstance(cre_spend, list) or not isinstance(cre_claims, list) or not isinstance(session_rows, list):
        raise shared.OperationRefused("r3_interruption_snapshot_invalid")
    if len(cre_spend) != 1 or cre_claims:
        raise shared.OperationRefused("cre_native_snapshot_invalid")
    cre = cre_spend[0]
    if (cre.get("run_id") != CRE_RUN_ID or cre.get("stage") != "understand_spend"
            or str(cre.get("run_date")) != "2026-09-30" or cre.get("status") != "ok"):
        raise shared.OperationRefused("cre_native_spend_identity_mismatch")
    counts = shared._counts(cre.get("counts"))
    if (shared._micros(counts.get("model_usd")) != shared._micros(CRE_USD)
            or counts.get("what") != CRE_WHAT):
        raise shared.OperationRefused("cre_native_spend_value_mismatch")
    if len(session_rows) != 5:
        raise shared.OperationRefused("r3_snapshot_session_count_mismatch")
    expected_session = shared._saved_spend_rows(prior_writes) + shared._spend_rows_for_report(cre_spend)
    if shared._canonical_rows(expected_session) != shared._canonical_rows(shared._spend_rows_for_report(session_rows)):
        raise shared.OperationRefused("r3_snapshot_local_receipt_mismatch")
    if _sha256(current_report_path) != current_report_sha:
        raise shared.OperationRefused("r3_current_report_changed")
    input_hashes.update({
        "interruption_snapshot": _sha256(snapshot_path),
        "source_metadata": source_sha,
        "initial_report": initial_report_sha,
        "postbook_audit": audit_sha,
        "original_now": INITIAL_NOW_SHA256,
        "current_report_before": current_report_sha,
    })
    return {
        "local_receipts": local_receipts,
        "local_writes": prior_writes,
        "call_count": prior_call_count,
        "snapshot": snapshot,
        "source_metadata": source,
        "postbook_audit": audit,
        "input_hashes": input_hashes,
        "current_report_sha256": current_report_sha,
    }


def _rows_from_report(rows):
    result = []
    for row in rows:
        result.append({
            "run_id": row["run_id"],
            "stage": row["stage"],
            "run_date": date.fromisoformat(str(row["run_date"])),
            "status": row["status"],
            "counts": {"model_usd": row["booked_usd"], "what": row["what"]},
        })
    return result


def _validate_prefunded_evidence(*, proof_path, continuation_data_dir, expected_base_rows,
                                 input_hashes):
    if _sha256(proof_path) != PREFUNDED_RECOVERY_SHA256:
        raise shared.OperationRefused("prefunded_recovery_proof_changed")
    proof = _json(proof_path, "prefunded_recovery_proof_invalid")
    expected = {
        "schema_version": "r3-prefunded-setup-recovery-v1",
        "failed_source_commit": "ae3f91e0a2c70397a7e2cd9508bd218829d6fe13",
        "terminal_session": 84859,
        "observed_exit_code": 1,
        "observed_refusal": "daily_cap_exceeded_after_phase_reservation",
        "guard_position": "after reservation readback and before the sole staging.check_ask invocation",
        "paid_model_dispatches": 0,
        "phase_prefix": CONTINUATION_PREFIX,
        "reservation_run_id": CONTINUATION_RESERVATION_RUN_ID,
        "reservation_usd": "15.000000",
        "cumulative_native_usd": "25.000000",
        "native_daily_usd": "38.935557",
        "daily_cap_usd": "40.000000",
        "native_claim_rows": 0,
        "before_query_job": "c0aa7df0-8b1a-486e-959a-e4e6ccd4e7be",
        "after_query_job": "9045e80a-a772-464f-88b5-957ec19888a3",
        "before_daily_raw": "23.935557000000003",
        "after_daily_raw": "38.935557",
        "approval_sha256": CONTINUATION_APPROVAL_SHA256,
        "closed_manifest_sha256": CLOSED_MANIFEST_SHA256,
        "reuse_only_no_new_booking": True,
        "automatic_model_repeats": 0,
    }
    if any(proof.get(key) != value for key, value in expected.items()):
        raise shared.OperationRefused("prefunded_recovery_proof_mismatch")
    before_raw = Decimal(proof["before_daily_raw"])
    after_raw = Decimal(proof["after_daily_raw"])
    if (_ledger_micros(after_raw - before_raw) != CONTINUATION_RESERVATION_MICROS
            or after_raw > Decimal(proof["daily_cap_usd"])):
        raise shared.OperationRefused("prefunded_daily_reconciliation_mismatch")
    files = proof.get("files")
    if not isinstance(files, dict) or len(files) != len(PREFUNDED_EVIDENCE_NAMES):
        raise shared.OperationRefused("prefunded_evidence_file_set_invalid")
    paths = {Path(raw_path).name: Path(raw_path) for raw_path in files}
    if set(paths) != PREFUNDED_EVIDENCE_NAMES:
        raise shared.OperationRefused("prefunded_evidence_file_set_invalid")
    for raw_path, digest in files.items():
        if _sha256(raw_path) != digest:
            raise shared.OperationRefused("prefunded_evidence_changed")
        input_hashes[str(raw_path)] = digest
    marker_path = Path(continuation_data_dir) / f"{CONTINUATION_KEY}.attempted"
    if paths[marker_path.name].resolve() != marker_path.resolve():
        raise shared.OperationRefused("prefunded_marker_path_mismatch")
    if (not Path(continuation_data_dir).is_dir() or not marker_path.is_file()
            or marker_path.read_bytes() != b"attempt started\n"):
        raise shared.OperationRefused("prefunded_attempt_state_invalid")
    output_path = Path(continuation_data_dir) / f"{CONTINUATION_KEY}.json"
    execution_path = Path(continuation_data_dir) / CONTINUATION_EXECUTION_MARKER
    if output_path.exists() or execution_path.exists():
        raise shared.OperationRefused("prefunded_resume_already_started")
    if {path.name for path in Path(continuation_data_dir).iterdir()} != {marker_path.name}:
        raise shared.OperationRefused("prefunded_attempt_state_invalid")
    setup_path = paths["ASK-2026-09-30-R3-CONTINUATION-1-SETUP-READBACK.json"]
    setup = _json(setup_path, "prefunded_setup_readback_invalid")
    phase_rows = setup.get("phase_rows") or []
    session_rows = setup.get("session_rows") or []
    expected_phase = [{
        "run_id": CONTINUATION_RESERVATION_RUN_ID,
        "stage": "understand_spend",
        "run_date": "2026-09-30",
        "status": "ok",
        "counts": {"model_usd": 15, "what": "staging_check_ask:NOW-01"},
    }]
    expected_session = expected_base_rows + shared._spend_rows_for_report(expected_phase)
    if (setup.get("source_commit") != proof["failed_source_commit"]
            or setup.get("daily_cap_usd") != 40.0
            or _ledger_micros(setup.get("daily_spend_usd")) != _ledger_micros(after_raw)
            or shared._canonical_rows(shared._spend_rows_for_report(phase_rows))
            != shared._canonical_rows(shared._spend_rows_for_report(expected_phase))
            or shared._canonical_rows(shared._spend_rows_for_report(session_rows))
            != shared._canonical_rows(expected_session)
            or setup.get("claim_rows") != []):
        raise shared.OperationRefused("prefunded_setup_readback_mismatch")
    daily_audit = _json(paths["ASK-2026-09-30-R3-CONTINUATION-1-DAILY-QUERY-AUDIT.json"],
                        "prefunded_daily_audit_invalid")
    queries = {item.get("job_id"): item for item in daily_audit.get("matched_daily_queries", [])}
    before_query = queries.get(proof["before_query_job"])
    after_query = queries.get(proof["after_query_job"])
    if (daily_audit.get("all_pages_consumed") is not True
            or before_query is None or after_query is None
            or before_query.get("cache_hit") is not False or after_query.get("cache_hit") is not False
            or before_query.get("rows") != [{"usd": float(before_raw)}]
            or after_query.get("rows") != [{"usd": float(after_raw)}]):
        raise shared.OperationRefused("prefunded_daily_audit_mismatch")
    input_hashes["prefunded_recovery_proof"] = PREFUNDED_RECOVERY_SHA256
    return {"proof": proof, "setup": setup}


def _validate_continuation_inputs(*, approval_path, manifest_path, closed_data_dir,
                                  continuation_data_dir, prefunded_proof_path=None):
    if _sha256(approval_path) != CONTINUATION_APPROVAL_SHA256:
        raise shared.OperationRefused("continuation_approval_changed")
    if _sha256(manifest_path) != CLOSED_MANIFEST_SHA256:
        raise shared.OperationRefused("closed_manifest_changed")
    approval = _json(approval_path, "continuation_approval_invalid")
    manifest = _json(manifest_path, "closed_manifest_invalid")
    if not (
        approval.get("schema_version") == "r3-now-continuation-approval-v1"
        and approval.get("prior_native_booking_usd") == "10.000000"
        and approval.get("prior_unused_reservation_included_usd") == "0.497615"
        and approval.get("additional_allowance_usd") == "15.000000"
        and approval.get("cumulative_cap_usd") == "25.000000"
        and approval.get("question_ids") == ["NOW-01"]
        and approval.get("maximum_new_question_executions") == 1
        and approval.get("automatic_retries") == 0
        and approval.get("release_prior_reservations") is False
        and manifest.get("schema_version") == "r3-closed-evidence-v1"
        and manifest.get("native_booked_usd") == "10.000000"
    ):
        raise shared.OperationRefused("continuation_funding_contract_invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or len(files) != 15:
        raise shared.OperationRefused("closed_manifest_file_set_invalid")
    input_hashes = {"approval": CONTINUATION_APPROVAL_SHA256,
                    "closed_manifest": CLOSED_MANIFEST_SHA256}
    paths = {}
    for raw_path, expected_hash in files.items():
        path = Path(raw_path)
        if _sha256(path) != expected_hash:
            raise shared.OperationRefused("closed_evidence_changed")
        paths[path.name] = path
        input_hashes[str(path)] = expected_hash
    required = {
        "ASK-2026-09-30-R3.md", "r3-final-receipt.json",
        "ASK-2026-09-30-R3-interruption-readback.json",
        "ASK-2026-09-30-R3-postbook-job-audit.json",
        *{f"{question_id}.attempted" for question_id in BASE_MARKER_IDS},
        *{f"{question_id}.json" for question_id in BASE_LOCAL_IDS},
        f"{RETRY_KEY}.attempted", f"{RETRY_KEY}.json",
    }
    if set(paths) != required:
        raise shared.OperationRefused("closed_manifest_file_set_invalid")
    closed_data_dir = Path(closed_data_dir)
    closed_data_root = closed_data_dir.resolve()
    expected_local_files = {path.name for path in paths.values()
                            if path.parent.resolve() == closed_data_root}
    if {path.name for path in closed_data_dir.iterdir()} != expected_local_files:
        raise shared.OperationRefused("closed_local_data_changed")
    for marker_id in BASE_MARKER_IDS + (RETRY_KEY,):
        if (closed_data_dir / f"{marker_id}.attempted").read_bytes() != b"attempt started\n":
            raise shared.OperationRefused("closed_attempt_marker_invalid")
    final_receipt = _json(paths["r3-final-receipt.json"], "closed_final_receipt_invalid")
    if not (
        final_receipt.get("native_session_booked_total_usd") == "10.000000"
        and shared._micros(final_receipt.get("cap_usd")) == shared._micros(10.0)
        and final_receipt.get("recorded_headroom_usd") == "0.000000"
        and final_receipt.get("retry_unspent_reservation_usd") == "0.497615"
    ):
        raise shared.OperationRefused("closed_final_receipt_mismatch")
    closed_now = _json(paths[f"{RETRY_KEY}.json"], "closed_now_receipt_invalid")
    session_readback = closed_now.get("session_spend_readback") or {}
    report_rows = session_readback.get("rows") or []
    if (closed_now.get("attempt_key") != RETRY_KEY
            or closed_now.get("stage") != "COMPLETE"
            or session_readback.get("match") is not True
            or shared._micros(session_readback.get("booked_usd")) != shared._micros(10.0)
            or {row.get("run_id") for row in report_rows} != CONTINUATION_NATIVE_IDS
            or len(report_rows) != 6
            or shared._micros(closed_now.get("phase_reserved_usd")) != shared._micros(1.158263)
            or shared._micros(closed_now.get("phase_unspent_reserved_usd")) != shared._micros(0.497615)):
        raise shared.OperationRefused("closed_now_receipt_mismatch")
    native_rows = _rows_from_report(report_rows)
    native_total = sum(shared._micros(shared._counts(row["counts"]).get("model_usd")) for row in native_rows)
    if native_total != shared._micros(10.0):
        raise shared.OperationRefused("closed_native_total_mismatch")
    interruption = _json(paths["ASK-2026-09-30-R3-interruption-readback.json"],
                         "closed_interruption_readback_invalid")
    cre_spend, cre_claims = interruption.get("spend_rows"), interruption.get("claim_rows")
    if (interruption.get("prefix") != CRE_PREFIX or not isinstance(cre_spend, list)
            or len(cre_spend) != 1 or cre_claims):
        raise shared.OperationRefused("closed_cre_snapshot_mismatch")
    prefunded = None
    if prefunded_proof_path is None:
        if Path(continuation_data_dir).exists():
            raise shared.OperationRefused("continuation_local_attempt_exists")
    else:
        prefunded = _validate_prefunded_evidence(
            proof_path=prefunded_proof_path, continuation_data_dir=continuation_data_dir,
            expected_base_rows=shared._spend_rows_for_report(native_rows), input_hashes=input_hashes)
    return {
        "approval": approval,
        "manifest": manifest,
        "final_receipt": final_receipt,
        "closed_now": closed_now,
        "snapshot": {"session_rows": native_rows, "spend_rows": cre_spend, "claim_rows": cre_claims},
        "expected_base_rows": shared._spend_rows_for_report(native_rows),
        "native_base_micros": native_total,
        "old_unspent_micros": shared._micros(approval["prior_unused_reservation_included_usd"]),
        "input_hashes": input_hashes,
        "closed_report_path": paths["ASK-2026-09-30-R3.md"],
        "closed_report_sha256": files[str(paths["ASK-2026-09-30-R3.md"])],
        "current_report_sha256": files[str(paths["ASK-2026-09-30-R3.md"])],
        "postbook_audit": _json(paths["ASK-2026-09-30-R3-postbook-job-audit.json"],
                                 "closed_postbook_audit_invalid"),
        "prefunded_resume": prefunded,
    }


def _readonly(wiring, modules):
    wrapped = copy.copy(wiring)
    execute = wiring.execute

    def select_only(sql, params=None, max_bytes=None):
        try:
            modules.sql.check_sql(sql)
        except Exception:
            raise shared.OperationRefused("query_guard") from None
        return execute(sql, params, max_bytes)

    wrapped.execute = select_only
    return wrapped


def _session_rows(wiring, modules, day):
    sql = (
        "SELECT run_id, stage, run_date, status, counts "
        "FROM `ogilvy-trends-v2.intelligence_42_agent.runs` "
        "WHERE STARTS_WITH(run_id, @prefix) AND run_date = @day ORDER BY run_id"
    )
    return shared._query(wiring, modules, sql, {"prefix": R3_ROOT, "day": day})


def _source_hashes(repo_root, modules):
    paths = {"recovery_runner": Path(__file__), "ask_r2": Path(shared.__file__)}
    for name, module in vars(modules).items():
        path = getattr(module, "__file__", None)
        if path:
            paths[name] = Path(path)
    hashes = {}
    for name, path in paths.items():
        try:
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise shared.OperationRefused("source_hash_missing") from None
    return hashes


def _request_statistics(calls):
    fields = ("phase", "request_payload_utf8_bytes", "request_payload_characters",
              "request_payload_sha256", "request_evidence_counts",
              "structured_schema_serialized_utf8_bytes", "client_timeout_s",
              "requested_max_tokens", "effective_output_limit_tokens")
    return [{key: call[key] for key in fields if key in call} for call in calls]


def _atomic_json(path, payload):
    pending = Path(path).with_name(Path(path).name + ".pending")
    with pending.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(shared._jsonable(payload), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, path)


def _execute_now_retry(*, repo_root, data_dir, snapshot_path, source_metadata_path,
                       initial_report_path, current_report_path, postbook_audit_path,
                       modules, wiring, authority, health, budget_factory=None, phase_config=None):
    if authority.get("source_clean") is not True or not isinstance(authority.get("source_commit"), str):
        raise shared.OperationRefused("source_provenance_invalid")
    if health.get("http_status") != 200 or health.get("ok") is not True:
        raise shared.OperationRefused("health_unready")
    continuation = phase_config is not None
    resume_prefunded = False
    if continuation:
        if phase_config.get("kind") != "continuation-1":
            raise shared.OperationRefused("continuation_phase_invalid")
        resume_prefunded = phase_config.get("resume_prefunded") is True
        data_dir = Path(phase_config["data_dir"])
        inputs = _validate_continuation_inputs(
            approval_path=phase_config["approval_path"],
            manifest_path=phase_config["manifest_path"],
            closed_data_dir=phase_config["closed_data_dir"],
            continuation_data_dir=data_dir,
            prefunded_proof_path=(phase_config.get("prefunded_proof_path")
                                  if resume_prefunded else None))
        if resume_prefunded and inputs["prefunded_resume"] is None:
            raise shared.OperationRefused("prefunded_resume_proof_missing")
        attempt_key = CONTINUATION_KEY
        phase_prefix = CONTINUATION_PREFIX
        current_report_path = inputs["closed_report_path"]
        native_base_micros_expected = inputs["native_base_micros"]
        cumulative_cap_micros = shared._micros(inputs["approval"]["cumulative_cap_usd"])
        max_phase_micros = min(
            shared._micros(inputs["approval"]["additional_allowance_usd"]),
            cumulative_cap_micros - native_base_micros_expected)
    else:
        inputs = _validate_inputs(data_dir, snapshot_path, source_metadata_path,
                                  initial_report_path, current_report_path, postbook_audit_path)
        attempt_key = RETRY_KEY
        phase_prefix = RETRY_PREFIX
        native_base_micros_expected = shared._micros(BASE_USD)
        cumulative_cap_micros = shared._micros(CAP_USD)
        max_phase_micros = cumulative_cap_micros - native_base_micros_expected
    wiring = shared._normalize_wiring(wiring)
    now = wiring.now()
    day = now.astimezone(modules.staging.SAST).date()
    readonly = _readonly(wiring, modules)
    if continuation:
        with modules.ask._SPEND_LOCK:
            if modules.ask._IN_FLIGHT:
                raise shared.OperationRefused("ask_hold_exists_before_reservation")
    preflight_prefix = f"{phase_prefix}-resume-preflight" if resume_prefunded else phase_prefix
    preflight = shared._preflight(
        readonly, modules, preflight_prefix, now, cumulative_cap_micros / shared.MICROS)
    retry_spend, retry_claims = shared._prefix_queries(readonly, modules, phase_prefix, day)
    if resume_prefunded:
        if (len(retry_spend) != 1 or retry_claims
                or retry_spend[0].get("run_id") != CONTINUATION_RESERVATION_RUN_ID
                or retry_spend[0].get("stage") != "understand_spend"
                or str(retry_spend[0].get("run_date")) != day.isoformat()
                or retry_spend[0].get("status") != "ok"):
            raise shared.OperationRefused("prefunded_native_row_mismatch")
        retry_counts = shared._counts(retry_spend[0].get("counts"))
        if (shared._micros(retry_counts.get("model_usd")) != CONTINUATION_RESERVATION_MICROS
                or retry_counts.get("what") != "staging_check_ask:NOW-01"):
            raise shared.OperationRefused("prefunded_native_row_mismatch")
    elif retry_spend or retry_claims:
        raise shared.OperationRefused("retry_prefix_already_used")
    native_session = _session_rows(readonly, modules, day)
    snapshot = inputs["snapshot"]
    if continuation and any(str(row.get("run_date")) != day.isoformat() for row in snapshot["session_rows"]):
        raise shared.OperationRefused("closed_base_sast_day_mismatch")
    if resume_prefunded:
        expected_native_session = (inputs["expected_base_rows"]
                                   + shared._spend_rows_for_report(retry_spend))
        actual_native_session = shared._spend_rows_for_report(native_session)
        if shared._canonical_rows(actual_native_session) != shared._canonical_rows(expected_native_session):
            raise shared.OperationRefused("r3_native_base_changed")
    elif shared._canonical_rows(native_session) != shared._canonical_rows(snapshot["session_rows"]):
        raise shared.OperationRefused("r3_native_base_changed")
    cre_spend, cre_claims = shared._prefix_queries(readonly, modules, CRE_PREFIX, day)
    if (shared._canonical_rows(cre_spend) != shared._canonical_rows(snapshot["spend_rows"])
            or cre_claims != [] or snapshot["claim_rows"] != []):
        raise shared.OperationRefused("cre_native_readback_changed")
    if resume_prefunded:
        expected_base = inputs["expected_base_rows"]
        actual_base = expected_base
    elif continuation:
        expected_base = inputs["expected_base_rows"]
        actual_base = shared._spend_rows_for_report(native_session)
    else:
        expected_base = shared._saved_spend_rows(inputs["local_writes"]) + shared._spend_rows_for_report(cre_spend)
        actual_base = shared._spend_rows_for_report(native_session)
    if shared._canonical_rows(expected_base) != shared._canonical_rows(actual_base):
        raise shared.OperationRefused("r3_native_receipt_mismatch")
    native_base_micros = sum(shared._micros(row["booked_usd"]) for row in actual_base)
    if native_base_micros != native_base_micros_expected:
        raise shared.OperationRefused("r3_native_base_total_mismatch")
    if resume_prefunded:
        remaining_micros = CONTINUATION_RESERVATION_MICROS
        if remaining_micros > max_phase_micros:
            raise shared.OperationRefused("prefunded_phase_reservation_exceeds_approval")
    elif continuation:
        daily_room = (Decimal(str(preflight["daily_model_cap_usd"]))
                      - Decimal(str(preflight["spent_before_usd"])))
        daily_room_micros = int((daily_room * shared.MICROS).to_integral_value(rounding=ROUND_FLOOR))
        remaining_micros = min(max_phase_micros, daily_room_micros)
    else:
        remaining_micros = min(max_phase_micros, cumulative_cap_micros - native_base_micros)
    if remaining_micros <= 0:
        raise shared.OperationRefused("r3_retry_budget_exhausted")
    if native_base_micros + remaining_micros > cumulative_cap_micros:
        raise shared.OperationRefused("cumulative_cap_exceeded")
    native_base_usd = native_base_micros / shared.MICROS
    cumulative_cap_usd = cumulative_cap_micros / shared.MICROS
    questions = modules.staging.choose_questions("NOW-01")
    if len(questions) != 1 or questions[0].get("id") != "NOW-01":
        raise shared.OperationRefused("now_question_selection_mismatch")
    phase_reserved_usd = remaining_micros / shared.MICROS
    if budget_factory is None:
        budget = shared.SessionBudget(cap_usd=remaining_micros / shared.MICROS, provider_500_retries=0)
    else:
        budget = budget_factory(remaining_micros / shared.MICROS)
    if budget.cap_micros != remaining_micros or budget.charged_micros != 0 or budget.provider_500_retries != 0:
        raise shared.OperationRefused("isolated_budget_invalid")
    budget.provider_retry_statuses = ()
    phase_state = {
        "ask_active": False, "ask_finished": False,
        "hold_baseline": frozenset(), "own_hold_id": None, "daily_warehouse": None,
    }
    call_wiring = wiring
    run_modules = modules
    if continuation:
        real_model_spend_today = modules.ask.model_spend_today
        booked_day = day
        daily_cap_micros = shared._micros(preflight["daily_model_cap_usd"])

        def refuse_phase(reason):
            budget.stop_reason = reason
            raise shared.OperationRefused(reason)

        def phase_spend_today(warehouse, now_value):
            current_day = now_value.astimezone(modules.staging.SAST).date()
            if current_day != booked_day:
                refuse_phase("phase_reservation_sast_day_changed")
            phase_state["daily_warehouse"] = warehouse
            total = real_model_spend_today(warehouse, now_value)
            if not math.isfinite(float(total)) or float(total) < 0:
                refuse_phase("daily_spend_preflight_invalid")
            read_wiring = SimpleNamespace(
                execute=lambda sql, params, max_bytes=None: warehouse.run(sql, params, max_bytes)
            )
            phase_rows, _ = shared._prefix_queries(read_wiring, modules, phase_prefix, booked_day)
            if len(phase_rows) != 1:
                refuse_phase("phase_reservation_daily_row_mismatch")
            row = phase_rows[0]
            counts = shared._counts(row.get("counts"))
            writes = captured.get("spend_writes") or []
            if (not str(row.get("run_id", "")).startswith(f"{phase_prefix}-spend-")
                    or row.get("stage") != "understand_spend" or row.get("status") != "ok"
                    or str(row.get("run_date")) != booked_day.isoformat()
                    or shared._micros(counts.get("model_usd")) != remaining_micros
                    or counts.get("what") != "staging_check_ask:NOW-01"
                    or len(writes) != 1 or row.get("run_id") != writes[0].get("run_id")
                    or writes[0].get("submitted") is not True
                    or shared._micros(writes[0].get("model_usd")) != remaining_micros):
                refuse_phase("phase_reservation_daily_row_mismatch")
            total_micros = shared._micros(total)
            if total_micros < remaining_micros or total_micros > daily_cap_micros:
                refuse_phase("daily_cap_exceeded_after_phase_reservation")
            return (total_micros - remaining_micros) / shared.MICROS

        run_modules = copy.copy(modules)
        run_modules.ask = SimpleNamespace(**vars(modules.ask))
        run_modules.ask.model_spend_today = phase_spend_today
        call_wiring = copy.copy(wiring)
        original_run_ask = wiring.run_ask

        def tracked_run_ask(*args, **kwargs):
            if phase_state["ask_active"] or phase_state["ask_finished"]:
                raise shared.OperationRefused("continuation_ask_execution_repeated")
            with modules.ask._SPEND_LOCK:
                phase_state["hold_baseline"] = frozenset(modules.ask._IN_FLIGHT)
                if phase_state["hold_baseline"]:
                    raise shared.OperationRefused("continuation_other_ask_hold_before_run")
            phase_state["ask_active"] = True
            try:
                return original_run_ask(*args, **kwargs)
            finally:
                phase_state["ask_active"] = False
                phase_state["ask_finished"] = True

        call_wiring.run_ask = tracked_run_ask

        mark_dispatched = budget.mark_dispatched

        def guarded_mark_dispatched(ticket):
            def refuse_ticket(reason):
                ticket.record["stop_reason"] = reason
                budget.stop_reason = reason
                raise shared.BudgetRefused(reason)

            current_now = wiring.now()
            daily_warehouse = phase_state["daily_warehouse"]
            if daily_warehouse is None:
                refuse_ticket("continuation_daily_read_missing")
            try:
                phase_spend_today(daily_warehouse, current_now)
            except shared.OperationRefused as exc:
                reason = exc.args[0] if exc.args else "continuation_daily_preflight_failed"
                refuse_ticket(reason)
            with modules.ask._SPEND_LOCK:
                current_holds = frozenset(modules.ask._IN_FLIGHT)
            if phase_state["ask_active"]:
                if phase_state["own_hold_id"] is None:
                    if len(current_holds) != 1:
                        refuse_ticket("continuation_concurrent_ask_hold")
                    new_holds = current_holds - phase_state["hold_baseline"]
                    if len(new_holds) != 1:
                        refuse_ticket("continuation_concurrent_ask_hold")
                    phase_state["own_hold_id"] = next(iter(new_holds))
                elif current_holds != phase_state["hold_baseline"] | {phase_state["own_hold_id"]}:
                    refuse_ticket("continuation_concurrent_ask_hold")
            elif phase_state["ask_finished"] and ticket.record.get("phase") == "semantic_embedding":
                if current_holds:
                    refuse_ticket("continuation_other_ask_hold_during_scoring")
            else:
                refuse_ticket("continuation_dispatch_outside_ask")
            with modules.ask._SPEND_LOCK:
                latest_holds = frozenset(modules.ask._IN_FLIGHT)
                allowed_holds = (
                    phase_state["hold_baseline"] | {phase_state["own_hold_id"]}
                    if phase_state["ask_active"] else frozenset()
                )
                if latest_holds != allowed_holds:
                    refuse_ticket("continuation_concurrent_ask_hold")
                mark_dispatched(ticket)

        budget.mark_dispatched = guarded_mark_dispatched
    reused_write = []
    if resume_prefunded:
        reused_counts = shared._counts(retry_spend[0].get("counts"))
        reused_write = [{
            "run_id": retry_spend[0]["run_id"], "stage": retry_spend[0]["stage"],
            "run_date": day, "status": retry_spend[0]["status"],
            "model_usd": float(reused_counts["model_usd"]),
            "what": reused_counts["what"], "submitted": True, "reservation_reused": True,
        }]
    captured = {"question_id": "NOW-01", "ctx": None, "answer": None, "run": {},
                "error_type": None, "research_error_type": None, "research_http_status": None,
                "claim_rows": [], "spend_intents": [], "spend_writes": reused_write}
    restore_tier = shared._prevent_t0_fallback(modules.ask)
    restore_gemini = shared._install_gemini_guards(run_modules, budget)
    guarded = shared._wrap_wiring(
        call_wiring, run_modules, prefix=phase_prefix, question_id="NOW-01", day=day,
        captured=captured, budget=budget)
    start_charge = budget.charged_micros
    start_call = len(budget.calls)
    reservation_intent = {
        "usd": phase_reserved_usd, "reported_usd": 0.0,
        "guarded_minimum_usd": phase_reserved_usd,
        "what": "staging_check_ask:NOW-01", "credit": False,
        "run_prefix": phase_prefix, "run_date": day,
        "phase_reservation_before_dispatch": True,
        "reservation_reused": resume_prefunded,
        "native_spend_writes_this_execution": 0 if resume_prefunded else 1,
        "original_reservation_source": "ae3f91e0a2c70397a7e2cd9508bd218829d6fe13"
        if resume_prefunded else None,
    }
    captured["spend_intents"].append(reservation_intent)
    result = {"id": "NOW-01", "answer": None, "run": {}, "bars": None, "error": None}
    check_error = None

    def defer_booking(usd, what, credit=False):
        if what != "staging_check_ask:NOW-01" or credit:
            raise shared.OperationRefused("spend_intent_guard")
        amount = (budget.charged_micros - start_charge) / shared.MICROS
        reservation_intent["reported_usd"] = round(float(usd or 0), 6)
        reservation_intent["call_charge_at_score_usd"] = amount
        reservation_intent["score_booking_callback_seen"] = True
        return phase_reserved_usd

    reservation_lock = modules.ask._SPEND_LOCK if continuation else nullcontext()
    try:
        with reservation_lock:
            if continuation and not resume_prefunded:
                try:
                    data_dir.mkdir(parents=True, exist_ok=False)
                except FileExistsError:
                    raise shared.OperationRefused("continuation_local_attempt_exists") from None
            if not resume_prefunded:
                attempt_path = Path(data_dir) / f"{attempt_key}.attempted"
                try:
                    with attempt_path.open("x", encoding="utf-8", newline="\n") as handle:
                        handle.write("attempt started\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                except FileExistsError:
                    raise shared.OperationRefused("existing_attempt_marker") from None
            if continuation and modules.ask._IN_FLIGHT:
                raise shared.OperationRefused("ask_hold_exists_before_reservation")
            if resume_prefunded:
                reserved_booked = phase_reserved_usd
            else:
                reserved_booked = modules.embed.book_spend(
                    guarded.execute, run_id=phase_prefix, run_date=day,
                    usd=phase_reserved_usd, what="staging_check_ask:NOW-01", credit=False)
            reservation_intent["booked_usd"] = reserved_booked
            if shared._micros(reserved_booked) != remaining_micros or len(captured["spend_writes"]) != 1:
                raise shared.OperationRefused("phase_reservation_write_mismatch")
            execute_after_reservation = guarded.execute

            def no_later_spend_write(sql, params=None, max_bytes=None):
                if sql == modules.embed.SPEND_ROW_SQL:
                    raise shared.OperationRefused("post_reservation_spend_write_forbidden")
                return execute_after_reservation(sql, params, max_bytes)

            guarded.execute = no_later_spend_write
            reservation_readback = shared._readbacks(
                guarded, run_modules, phase_prefix, day,
                {"question_id": "NOW-01", "claim_rows": [], "spend_writes": captured["spend_writes"]})
            expected_reserved_session = shared._spend_rows_for_report(snapshot["session_rows"])
            expected_reserved_session += shared._saved_spend_rows(captured["spend_writes"])
            reserved_session_rows = _session_rows(guarded, modules, day)
            reserved_session_report = shared._spend_rows_for_report(reserved_session_rows)
            reserved_session_booked = round(sum(row["booked_usd"] for row in reserved_session_report), 6)
            reservation_session_readback = {
                "match": shared._canonical_rows(expected_reserved_session)
                == shared._canonical_rows(reserved_session_report),
                "rows": reserved_session_report,
                "booked_usd": reserved_session_booked,
            }
            if (reservation_readback["spend_readback"]["match"] is not True
                    or reservation_readback["claim_readback"]["match"] is not True
                    or reservation_readback["claim_readback"]["rows"]
                    or reservation_session_readback["match"] is not True
                    or shared._micros(reserved_session_booked) != native_base_micros + remaining_micros
                    or shared._micros(reserved_session_booked) > cumulative_cap_micros):
                raise shared.OperationRefused("phase_reservation_readback_failed")
            if continuation:
                checked_now = wiring.now()
                if checked_now.astimezone(modules.staging.SAST).date() != day:
                    raise shared.OperationRefused("phase_reservation_sast_day_changed")
                daily_warehouse = shared.ReadOnlyWarehouse(wiring.warehouse, modules.sql.check_sql)
                daily_spend_after = modules.ask.model_spend_today(daily_warehouse, checked_now)
                daily_delta_micros = None
                if not resume_prefunded:
                    daily_delta_micros = _ledger_micros(
                        Decimal(str(daily_spend_after)) - Decimal(str(preflight["spent_before_usd"])))
                if (not math.isfinite(float(daily_spend_after))
                        or float(daily_spend_after) > preflight["daily_model_cap_usd"]
                        or (not resume_prefunded and daily_delta_micros < remaining_micros)):
                    raise shared.OperationRefused("daily_cap_exceeded_after_phase_reservation")
            if resume_prefunded:
                execution_path = Path(data_dir) / CONTINUATION_EXECUTION_MARKER
                try:
                    with execution_path.open("x", encoding="utf-8", newline="\n") as handle:
                        handle.write("execution started\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                except FileExistsError:
                    raise shared.OperationRefused("prefunded_resume_already_started") from None
                inputs["input_hashes"]["continuation_execution_marker"] = _sha256(execution_path)
        try:
            check = run_modules.staging.check_ask(guarded, defer_booking, questions, record_dir=None)
            returned = check.get("questions") or []
            if len(returned) != 1 or returned[0].get("id") != "NOW-01":
                raise shared.OperationRefused("one_question_result_guard")
            result = returned[0]
        except BaseException as exc:
            check_error = type(exc).__name__
            captured["error_type"] = check_error
            result["error"] = check_error
            result["error_type"] = check_error
            result["run"] = captured.get("run") or getattr(exc, "run", None) or {}
        if captured.get("answer") is not None:
            result["answer"] = captured["answer"]
        if captured.get("run"):
            result["run"] = captured["run"]
        final_charge = (budget.charged_micros - start_charge) / shared.MICROS
        reservation_intent["phase_unspent_reserved_usd"] = round(phase_reserved_usd - final_charge, 6)
        result["booked_usd"] = phase_reserved_usd
        calls = copy.deepcopy(budget.calls[start_call:])
        run = result.get("run") or {}
        ctx = captured.get("ctx")
        checkpoint = {
            "schema_version": "r3-now-isolated-recovery-v1",
            "stage": "POST_SCORE_CHECKPOINT",
            "id": "NOW-01", "attempt_key": attempt_key, "prefix": phase_prefix,
            "call_index_base": 0,
            "call_index_scope": "isolated_continuation_phase" if continuation else "isolated_recovery_phase",
            "native_base_usd": native_base_usd, "remaining_cap_usd": remaining_micros / shared.MICROS,
            "aggregate_cap_usd": cumulative_cap_usd, "phase_reserved_usd": phase_reserved_usd,
            "phase_unspent_reserved_usd": round(phase_reserved_usd - final_charge, 6),
            "reservation_reused": resume_prefunded,
            "native_spend_writes_this_execution": 0 if resume_prefunded else 1,
            "original_reservation_source": "ae3f91e0a2c70397a7e2cd9508bd218829d6fe13"
            if resume_prefunded else None,
            "charged_usd": final_charge,
            "error_type": result.get("error_type") or check_error,
            "raw_answer": result.get("answer"), "run": run, "bars": result.get("bars"),
            "context": shared._context_snapshot(ctx), "query_hashes": shared._query_hashes(ctx),
            "claim_rows": captured["claim_rows"], "call_costs": calls,
            "request_statistics": _request_statistics(calls),
            "source": {"clean": True, "commit": authority["source_commit"],
                       "initial_r3_commit": INITIAL_SOURCE_COMMIT,
                       "continued_r3_commit": RESUME_SOURCE_COMMIT,
                       "file_sha256": _source_hashes(repo_root, modules)},
            "health_at_start": health,
            "input_sha256": inputs["input_hashes"],
            "postbook_audit": {"path": str(Path(postbook_audit_path)),
                               "sha256": POSTBOOK_AUDIT_SHA256,
                               "verdict": "NO_POSTBOOK_SEMANTIC_JOBS",
                               "query_jobs": 50, "semantic_jobs": 0},
            "phase_reservation_readback": reservation_readback,
            "phase_reservation_session_readback": reservation_session_readback,
        }
        output_path = Path(data_dir) / f"{attempt_key}.json"
        shared._write_json_exclusive(output_path, checkpoint)
        readback = {"spend_readback": {"match": False, "rows": []},
                    "claim_readback": {"match": False, "rows": []}}
        readback_error = None
        try:
            readback = shared._readbacks(
                guarded, run_modules, phase_prefix, day,
                {"question_id": "NOW-01", "claim_rows": captured["claim_rows"],
                 "spend_writes": captured["spend_writes"]})
        except BaseException as exc:
            readback_error = type(exc).__name__
        try:
            fresh_session = _session_rows(guarded, modules, day)
            expected_session = shared._spend_rows_for_report(snapshot["session_rows"])
            expected_session += shared._saved_spend_rows(captured["spend_writes"])
            session_total = round(sum(row["booked_usd"] for row in shared._spend_rows_for_report(fresh_session)), 6)
            session_match = shared._canonical_rows(expected_session) == shared._canonical_rows(
                shared._spend_rows_for_report(fresh_session))
            session_readback = {"match": session_match,
                                "rows": shared._spend_rows_for_report(fresh_session),
                                "booked_usd": session_total}
        except BaseException as exc:
            session_readback = {"match": False, "rows": [], "booked_usd": None}
            session_error = type(exc).__name__
        else:
            session_error = None
        outcome, violation = shared._outcome(result, readback, modules.ask, captured["spend_intents"])
        if readback_error:
            outcome, violation = "OPERATIONAL STOP", "readback_error"
        elif session_error:
            outcome, violation = "OPERATIONAL STOP", "session_spend_readback_error"
        elif not session_readback["match"]:
            outcome, violation = "OPERATIONAL STOP", "session_spend_readback_mismatch"
        elif shared._micros(session_readback["booked_usd"]) != native_base_micros + remaining_micros:
            outcome, violation = "OPERATIONAL STOP", "session_total_mismatch"
        elif budget.charged_micros > remaining_micros:
            outcome, violation = "OPERATIONAL STOP", "aggregate_cap_exceeded"
        if captured.get("research_error_type"):
            outcome, violation = "OPERATIONAL STOP", "research_failed"
        if budget.stop_reason:
            outcome, violation = "OPERATIONAL STOP", budget.stop_reason
        try:
            reported_micros = shared._micros(run.get("model_usd", 0) or 0)
        except shared.BudgetRefused:
            reported_micros = budget.cap_micros + 1
        if reported_micros > budget.charged_micros:
            outcome, violation = "OPERATIONAL STOP", "reported_usage_exceeds_guarded_calls"
        record = {
            "schema_version": checkpoint["schema_version"], "stage": "COMPLETE",
            "id": "NOW-01", "attempt_key": attempt_key, "prefix": phase_prefix,
            "question": questions[0].get("question"), "markets": questions[0].get("markets") or [],
            "outcome": outcome, "stop_reason": violation,
            "error_type": result.get("error_type") or check_error,
            "research_error_type": captured.get("research_error_type"),
            "research_http_status": captured.get("research_http_status"),
            "call_index_base": 0,
            "call_index_scope": "isolated_continuation_phase" if continuation else "isolated_recovery_phase",
            "native_base_usd": native_base_usd, "remaining_cap_usd": remaining_micros / shared.MICROS,
            "aggregate_cap_usd": cumulative_cap_usd, "aggregate_booked_usd": session_readback.get("booked_usd"),
            "reported_model_usd": run.get("model_usd"), "charged_usd": final_charge,
            "booked_usd": result.get("booked_usd"), "run": run,
            "run_id": run.get("run_id"), "raw_answer": result.get("answer"),
            "bars": result.get("bars"), "context": shared._context_snapshot(ctx),
            "query_hashes": shared._query_hashes(ctx), "warehouse_billed_bytes": shared._warehouse_bytes(ctx),
            "claim_rows": captured["claim_rows"], "spend_intents": captured["spend_intents"],
            "spend_writes": captured["spend_writes"], "spend_readback": readback["spend_readback"],
            "claim_readback": readback["claim_readback"], "session_spend_readback": session_readback,
            "readback_error_type": readback_error, "session_readback_error_type": session_error,
            "call_costs": calls, "request_statistics": _request_statistics(calls),
            "preflight": preflight,
            "source": checkpoint["source"], "health_at_start": health,
            "input_sha256": inputs["input_hashes"], "postbook_audit": checkpoint["postbook_audit"],
            "r3_isolated_recovery": ({
                "phase": "NOW-01-continuation-1",
                "native_base_rows": snapshot["session_rows"],
                "native_base_proof": "closed manifest matched fresh six row native readback",
                "closed_manifest_sha256": inputs["input_hashes"]["closed_manifest"],
                "approval_sha256": inputs["input_hashes"]["approval"],
                "prior_unused_reservation_included_usd": inputs["old_unspent_micros"] / shared.MICROS,
                "prior_unused_reservation_added_again": False,
                "additional_allowance_usd": max_phase_micros / shared.MICROS,
                "phase_reserved_usd": phase_reserved_usd,
                "phase_unspent_reserved_usd": round(phase_reserved_usd - final_charge, 6),
                "reservation_reused": resume_prefunded,
                "native_spend_writes_this_execution": 0 if resume_prefunded else 1,
                "original_reservation_source": "ae3f91e0a2c70397a7e2cd9508bd218829d6fe13"
                if resume_prefunded else None,
                "native_total_before_phase_usd": native_base_usd,
                "native_total_after_reservation_usd": native_base_usd + phase_reserved_usd,
                "cumulative_cap_usd": cumulative_cap_usd,
                "daily_model_cap_usd": preflight["daily_model_cap_usd"],
                "daily_spend_before_reservation_usd": None if resume_prefunded else preflight["spent_before_usd"],
                "daily_spend_after_reservation_usd": None if resume_prefunded else daily_spend_after,
                "daily_spend_at_resume_start_usd": preflight["spent_before_usd"] if resume_prefunded else None,
                "daily_spend_at_resume_validation_usd": daily_spend_after if resume_prefunded else None,
                "phase_reservation_sast_day": day.isoformat(),
                "reservation_basis": "up to approved allowance, bounded by same day headroom; unused amount not credited",
                "provider_retry_statuses": [], "automatically_retried": False,
            } if continuation else {
                "phase": "NOW-01-retry-1",
                "native_base_rows": snapshot["session_rows"],
                "native_base_proof": "saved interruption snapshot matched fresh native rows and four local receipts",
                "cre_native_spend_rows": cre_spend, "cre_native_claim_rows": [],
                "provider_retry_statuses": [], "automatically_retried": False,
                "local_ledger_cost_usd": LOCAL_USD, "cre_native_only_cost_usd": CRE_USD,
                "native_total_before_retry_usd": native_base_usd,
                "phase_reserved_usd": phase_reserved_usd,
                "phase_unspent_reserved_usd": round(phase_reserved_usd - final_charge, 6),
                "native_total_after_reservation_usd": cumulative_cap_usd,
                "reservation_basis": "remaining phase ceiling before dispatch; unused amount not credited",
            }),
            "phase_reserved_usd": phase_reserved_usd,
            "phase_unspent_reserved_usd": round(phase_reserved_usd - final_charge, 6),
            "reservation_reused": resume_prefunded,
            "native_spend_writes_this_execution": 0 if resume_prefunded else 1,
            "original_reservation_source": "ae3f91e0a2c70397a7e2cd9508bd218829d6fe13"
            if resume_prefunded else None,
            "phase_reservation_readback": reservation_readback,
            "phase_reservation_session_readback": reservation_session_readback,
        }
        if _sha256(current_report_path) != inputs["current_report_sha256"]:
            record["outcome"], record["stop_reason"] = "OPERATIONAL STOP", "r3_current_report_changed"
        _atomic_json(output_path, record)
        return record
    finally:
        restore_gemini()
        restore_tier()


def _source_proof(repo_root, runner_path):
    root = Path(repo_root).resolve()
    status = shared._git(root, "status", "--porcelain", "--untracked-files=no")
    if status.returncode or status.stdout.strip():
        raise shared.OperationRefused("source_dirty")
    head = shared._git(root, "rev-parse", "HEAD")
    commit = head.stdout.strip()
    if head.returncode or len(commit) != 40:
        raise shared.OperationRefused("source_commit_missing")
    try:
        relative = Path(runner_path).resolve().relative_to(root).as_posix()
    except ValueError:
        raise shared.OperationRefused("runner_outside_source_root") from None
    if shared._git(root, "ls-files", "--error-unmatch", relative).returncode:
        raise shared.OperationRefused("runner_uncommitted")
    return {"source_clean": True, "source_commit": commit}


def _read_health_only(repo_root):
    helper = Path(repo_root) / "plans" / "r3-health-read.py"
    try:
        result = subprocess.run(
            [sys.executable, str(helper), "--health-only"], cwd=repo_root,
            capture_output=True, encoding="utf-8", errors="replace", timeout=45, check=False)
        payload = json.loads(result.stdout)
        health = payload["health"]
    except Exception:
        raise shared.OperationRefused("health_read_failed") from None
    if result.returncode or health.get("http_status") != 200 or health.get("ok") is not True:
        raise shared.OperationRefused("health_unready")
    return health


def main(argv=None):
    parser = argparse.ArgumentParser(prog="py -3.13 -m core.eval.ask_r3_now_recovery")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--continuation-1", action="store_true")
    parser.add_argument("--resume-prefunded-continuation-1", action="store_true")
    args = parser.parse_args(argv)
    if args.continuation_1 and args.resume_prefunded_continuation_1:
        parser.error("choose one continuation mode")
    if not args.execute:
        print("Refused: pass --execute after the source and health checks are ready.")
        return REFUSED
    try:
        authority = _source_proof(ROOT, __file__)
        health = _read_health_only(ROOT)
        identity = shared._verify_builder_adc()
        if identity.get("principal") != shared.BUILDER_EMAIL or identity.get("project") != shared.PROJECT:
            raise shared.OperationRefused("builder_identity_mismatch")
        previous_provider = os.environ.get("MODEL_PROVIDER")
        os.environ["MODEL_PROVIDER"] = "gemini"
        try:
            modules = shared._load_modules(ROOT)
            shared._module_origin(ROOT, sys.modules[__name__])
            if getattr(modules.no_retry_model, "retries", 0) != 0:
                raise shared.OperationRefused("gemini_retries_enabled")
            wiring = modules.staging.real_wiring()
            phase_config = None
            data_dir = DATA_DIR
            if args.continuation_1 or args.resume_prefunded_continuation_1:
                data_dir = CONTINUATION_DATA_DIR
                phase_config = {
                    "kind": "continuation-1",
                    "data_dir": CONTINUATION_DATA_DIR,
                    "closed_data_dir": DATA_DIR,
                    "approval_path": CONTINUATION_APPROVAL_PATH,
                    "manifest_path": CLOSED_MANIFEST_PATH,
                    "resume_prefunded": args.resume_prefunded_continuation_1,
                    "prefunded_proof_path": PREFUNDED_RECOVERY_PATH,
                }
            record = _execute_now_retry(
                repo_root=ROOT, data_dir=data_dir, snapshot_path=SNAPSHOT_PATH,
                source_metadata_path=SOURCE_METADATA_PATH, initial_report_path=INITIAL_REPORT_PATH,
                current_report_path=CURRENT_REPORT_PATH, postbook_audit_path=POSTBOOK_AUDIT_PATH,
                modules=modules, wiring=wiring, authority=authority, health=health,
                phase_config=phase_config)
        finally:
            if previous_provider is None:
                os.environ.pop("MODEL_PROVIDER", None)
            else:
                os.environ["MODEL_PROVIDER"] = previous_provider
    except shared.OperationRefused as exc:
        print(f"Refused: {exc.args[0] if exc.args else 'recovery_preflight_failed'}")
        return REFUSED
    except Exception as exc:
        print(f"Recovery failed: {type(exc).__name__}")
        return FAILED
    phase_label = ("NOW continuation 1 resumed" if args.resume_prefunded_continuation_1
                   else "NOW continuation 1" if args.continuation_1 else "NOW retry")
    print(f"R3 {phase_label} status={record['outcome']}; charged_model_usd={record['charged_usd']:.6f}; "
          f"aggregate_booked_usd={record.get('aggregate_booked_usd')}; "
          f"stop_reason={record.get('stop_reason') or 'none'}")
    return 0 if record["outcome"] in ("PASS", "VALID INSUFFICIENT") else FAILED


if __name__ == "__main__":
    raise SystemExit(main())
