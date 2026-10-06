"""Register one exact human quality-review receipt in staging."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.cloud import bigquery
from src.analysis.open_intelligence import live_quality
from src.analysis.open_intelligence.brain_contract import canonical_bytes

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
APPROVED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class RegistrationRefusal(ValueError):
    pass


class RegistrationGrammarRefusal(RegistrationRefusal):
    pass


def _parse_cli(argv: Sequence[str]) -> tuple[str, Path, str]:
    values = tuple(argv)
    if (
        len(values) != 5
        or values[0] not in {"review", "apply"}
        or values[1] != "--receipt-file"
        or values[3] != "--receipt-sha256"
    ):
        raise RegistrationGrammarRefusal("quality review receipt CLI grammar is invalid")
    path = Path(values[2])
    digest = values[4]
    if not path.is_absolute() or _DIGEST.fullmatch(digest) is None:
        raise RegistrationGrammarRefusal("quality review receipt CLI grammar is invalid")
    return values[0], path, digest


def _object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise RegistrationRefusal("quality review receipt contains duplicate fields")
        output[key] = value
    return output


def _load_receipt(path: Path, expected_sha256: str) -> tuple[dict[str, object], bytes]:
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RegistrationRefusal("quality review receipt digest differs")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
    except RegistrationRefusal:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RegistrationRefusal("quality review receipt is unreadable") from error
    if not isinstance(payload, dict) or set(payload) != set(live_quality.REVIEW_RECEIPT_FIELDS):
        raise RegistrationRefusal("quality review receipt is invalid")
    receipt = {field: payload[field] for field in live_quality.REVIEW_RECEIPT_FIELDS}
    if (
        canonical_bytes(receipt) != raw
        or receipt.get("review_contract_version") != live_quality.REVIEW_CONTRACT_VERSION
        or receipt.get("reviewed_by") != "Albert"
        or receipt.get("decision") != "approved"
        or any(
            receipt.get(field) != []
            for field in (
                "foreign_market_evidence_ids",
                "factual_conflict_evidence_ids",
                "uncertain_evidence_ids",
            )
        )
        or live_quality.review_receipt_digest(receipt) != receipt.get("receipt_digest")
    ):
        raise RegistrationRefusal("quality review receipt is invalid")
    return receipt, raw


def _review_payload(receipt: dict[str, object], artifact_sha256: str) -> dict[str, object]:
    return {
        "contract_version": "open_intelligence_quality_review_registration_review_v1",
        "run_id": receipt["run_id"],
        "review_receipt_digest": receipt["receipt_digest"],
        "artifact_sha256": artifact_sha256,
        "source_window_digest": receipt["source_window_digest"],
        "candidate_projection_digest": receipt["candidate_projection_digest"],
        "packet_digest": receipt["packet_digest"],
    }


def _query(client: object, sql: str, *, parameters=()):
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            use_legacy_sql=False,
            query_parameters=list(parameters),
        ),
        location=LOCATION,
        retry=None,
        job_retry=None,
    )
    return tuple(job.result(max_results=2, retry=None, job_retry=None))


def _apply_receipt(receipt: dict[str, object], raw: bytes, artifact_sha256: str):
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    sql = (
        f"CALL `{PROJECT}.{DATASET}.sp_register_open_intelligence_quality_review_receipt_v1`"
        "(@canonical_review_receipt_json, @artifact_sha256)"
    )
    parameters = (
        bigquery.ScalarQueryParameter(
            "canonical_review_receipt_json", "STRING", raw.decode("utf-8")
        ),
        bigquery.ScalarQueryParameter("artifact_sha256", "STRING", artifact_sha256),
    )
    try:
        rows = _query(client, sql, parameters=parameters)
    except Exception:
        rows = _query(client, sql, parameters=parameters)
    if len(rows) != 1:
        raise RegistrationRefusal("quality review receipt registration readback differs")
    row = dict(rows[0])
    expected_fields = {
        "review_store_contract_version",
        "run_id",
        "canonical_review_receipt_json",
        "review_receipt_digest",
        "artifact_sha256",
        "source_window_digest",
        "candidate_projection_digest",
        "packet_digest",
        "registered_by",
        "registered_at",
    }
    expected = _review_payload(receipt, artifact_sha256)
    if (
        set(row) != expected_fields
        or row.get("review_store_contract_version") != "open_intelligence_quality_review_store_v1"
        or row.get("canonical_review_receipt_json") != raw.decode("utf-8")
        or row.get("registered_by") != APPROVED_BY
        or any(row.get(field) != expected[field] for field in tuple(expected)[1:])
    ):
        raise RegistrationRefusal("quality review receipt registration readback differs")
    return row


def _emit(payload: dict[str, object], *, error: bool = False) -> None:
    (sys.stderr if error else sys.stdout).write(canonical_bytes(payload).decode("utf-8") + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    mode, path, digest = _parse_cli(tuple(sys.argv[1:] if argv is None else argv))
    receipt, raw = _load_receipt(path, digest)
    if mode == "review":
        _emit(_review_payload(receipt, digest))
        return 0
    row = _apply_receipt(receipt, raw, digest)
    payload = _review_payload(receipt, digest)
    payload["contract_version"] = "open_intelligence_quality_review_registration_v1"
    payload["registered_by"] = row["registered_by"]
    payload["registered_at"] = row["registered_at"]
    _emit(payload)
    return 0


def run_executable(argv=None, *, runner=None, stderr=None) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    boundary = main if runner is None else runner
    error_output = sys.stderr if stderr is None else stderr
    try:
        return boundary(values)
    except RegistrationGrammarRefusal:
        payload = {"error": "quality_review_receipt_invalid_arguments"}
        code = 2
    except RegistrationRefusal:
        payload = {"error": "quality_review_receipt_refused"}
        code = 1
    except Exception:
        payload = {"error": "quality_review_receipt_internal_refusal"}
        code = 1
    error_output.write(canonical_bytes(payload).decode("utf-8") + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(run_executable(sys.argv[1:]))
