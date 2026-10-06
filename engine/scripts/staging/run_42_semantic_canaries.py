"""Run the QA-only 42 semantic canaries beside the persistence canary.

This runner is versioned on its own. It reuses the persistence canary's fixture
constructors and the ordinary engine validators, targets only the QA dataset and
QA identity the R03 resource manifest names, proves seven semantic cases through
the real readiness and claim-semantics path, appends its results with a bounded
retention, and never issues the persistence canary's temporary-table cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.cloud import bigquery
from scripts.staging import canary_open_intelligence_persistence as persistence_canary
from src.analysis.open_intelligence import persistence, run_receipts
from src.analysis.open_intelligence.canary_semantics import validate_claim_semantics
from src.analysis.open_intelligence.readiness import (
    EvidenceRecord,
    ReadinessRules,
    evaluate_readiness,
)

CANARY_NAME = "oi_semantic_canary"
CANARY_VERSION = "v1"
QA_PROJECT = "ogilvy-trends-v2"
QA_DATASET = "trends_v2_staging_qa"
QA_LOCATION = "US"
QA_IDENTITY = "intelligence-42-qa@ogilvy-trends-v2.iam.gserviceaccount.com"
QA_CLIENT_SCOPE_ID = run_receipts.QA_CLIENT_SCOPE_ID
QA_BRAND = "qa"
QA_THEME = "qa"
QA_RETENTION_DAYS = 90
QA_RESULTS_TABLE = "canary_results_v2"
QA_MARKET_SCOPE = ("za",)
ALARM_CODE = "qa_semantic_canary_failed"
DRILL_ERROR_CODE = "qa_drill_break"
MISMATCH_ERROR_CODE = "semantic_expectation_mismatch"
_DRILL_EXPECTATION = {"state": "qa_drill_unreachable", "reasons": ["qa_drill_break"]}
QA_MANIFEST_IDENTITY_KEY = "qa"
QA_DATASET_RESOURCE = f"//bigquery.googleapis.com/projects/{QA_PROJECT}/datasets/{QA_DATASET}"
CASES = ("coherent", "thin", "opposing", "foreign", "duplicate", "demographic", "causal")
EXPECTED_STATES: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "coherent": ("ready", ()),
    "thin": ("thin", ("insufficient_qualifying_families",)),
    "opposing": ("contradictory", ("opposing_family_directions",)),
    "foreign": ("thin", ("insufficient_qualifying_families", "weak_geo_evidence")),
    "duplicate": ("rejected", ("duplicate row id",)),
    "demographic": ("rejected", ("unsupported demographic language",)),
    "causal": ("rejected", ("unsupported causal language",)),
}
_READINESS_WINDOW_DAYS = 7
_MINIMUM_GEO_CONFIDENCE = 0.6
_FAMILIES = ("news", "reddit")
_FOREIGN_GEO_CONFIDENCE = 0.2
_CASE_EXCERPTS = {
    "demographic": "Women aged 18 to 24 are joining the fixture routine.",
    "causal": "The fixture routine is driven by the second source family.",
}


class QaTargetRefusal(RuntimeError):
    """The run would leave the QA-only target or identity contract."""


class QaAppendRefusal(RuntimeError):
    """The QA results append did not land as an append."""


def _refuse(condition: bool, code: str) -> None:
    if condition:
        raise QaTargetRefusal(code)


def manifest_contract(manifest: object) -> dict[str, str]:
    """The QA dataset and identity the R03 resource manifest names, or a named refusal."""
    if not isinstance(manifest, Mapping):
        raise QaTargetRefusal("qa_manifest_invalid")
    identities = manifest.get("identities")
    resources = manifest.get("resources")
    if not isinstance(identities, Mapping) or not isinstance(resources, list):
        raise QaTargetRefusal("qa_manifest_invalid")
    identity = identities.get(QA_MANIFEST_IDENTITY_KEY)
    if not isinstance(identity, str) or "/serviceAccounts/" not in identity:
        raise QaTargetRefusal("qa_identity_unnamed")
    datasets = [
        item["name"].rsplit("/datasets/", 1)[1]
        for item in resources
        if isinstance(item, Mapping)
        and isinstance(item.get("name"), str)
        and "/datasets/" in item["name"]
        and item["name"].count("/") == QA_DATASET_RESOURCE.count("/")
        and "write" in (item.get("actions") or ())
        and item["name"].startswith(f"//bigquery.googleapis.com/projects/{QA_PROJECT}/datasets/")
    ]
    qa_datasets = [name for name in datasets if name.endswith("_qa")]
    if len(qa_datasets) != 1:
        raise QaTargetRefusal("qa_dataset_unnamed")
    return {"dataset": qa_datasets[0], "identity": identity.rsplit("/serviceAccounts/", 1)[1]}


def validate_qa_target(dataset: object, identity: object) -> None:
    """Refuse any dataset or identity other than the QA pair the contract names."""
    _refuse(dataset != QA_DATASET, "qa_dataset_required")
    _refuse(identity != QA_IDENTITY, "qa_identity_required")


def _timestamp(run_date: date) -> datetime:
    return datetime(run_date.year, run_date.month, run_date.day, tzinfo=UTC)


def run_id(run_date: date, case: str) -> str:
    return f"{CANARY_NAME}_{CANARY_VERSION}_{run_date.isoformat()}_{case}"


def qa_identifier(prefix: str, case: str, role: str, run_date: date) -> str:
    """A QA identity distinct from every persistence-canary identity by preimage."""
    preimage = f"{CANARY_NAME}|{CANARY_VERSION}|{run_date.isoformat()}|{case}|{role}"
    return prefix + "_" + hashlib.sha256(preimage.encode()).hexdigest()


def _evidence_row(
    template: Mapping[str, object],
    *,
    run_date: date,
    case: str,
    role: str,
    signal_id: str,
    family: str,
    direction: str,
    geo_confidence: float,
    excerpt: str,
    row_id: str | None = None,
) -> dict[str, object]:
    return {
        **template,
        "signal_id": signal_id,
        "evidence_id": qa_identifier("ev", case, role, run_date),
        "row_id": row_id if row_id is not None else f"qa_{case}_{role}_row",
        "source_family": family,
        "channel_family": family,
        "direction": direction,
        "geo_confidence": geo_confidence,
        "excerpt": excerpt,
        "published_at": _timestamp(run_date),
    }


def case_rows(run_date: date, case: str) -> dict[str, list[dict[str, object]]]:
    """The persistence canary's fixture rows rekeyed to the QA identity for one case."""
    _refuse(case not in CASES, "qa_case_unknown")
    rows = persistence_canary.fixture_rows(run_date)
    signal_id = qa_identifier("sig", case, "signal", run_date)
    prior_signal_id = qa_identifier("sig", case, "prior_signal", run_date)
    identities = {
        "signal_id": signal_id,
        "evidence_id": qa_identifier("ev", case, "evidence", run_date),
        "member_id": qa_identifier("mem", case, "member", run_date),
        "from_signal_id": prior_signal_id,
        "to_signal_id": signal_id,
        "prediction_id": qa_identifier("pred", case, "prediction", run_date),
        "outcome_id": qa_identifier("out", case, "outcome", run_date),
    }
    rekeyed: dict[str, list[dict[str, object]]] = {}
    for table, table_rows in rows.items():
        rekeyed[table] = []
        for row in table_rows:
            item = dict(row)
            item["client_scope_id"] = QA_CLIENT_SCOPE_ID
            item["brand_config_id"] = QA_BRAND
            item["theme_id"] = QA_THEME
            item["run_id"] = run_id(run_date, case)
            if "discovery_mode" in item:
                item["discovery_mode"] = "canary"
            for field, value in identities.items():
                if field in item:
                    item[field] = value
            rekeyed[table].append(item)
    template = rekeyed["evidence"][0]
    excerpt = _CASE_EXCERPTS.get(case, str(template["excerpt"]))
    first_row_id = str(template["row_id"])
    common = {"run_date": run_date, "case": case, "signal_id": signal_id, "excerpt": excerpt}
    first_family, second_family = _FAMILIES
    if case == "thin":
        evidence = [
            _evidence_row(
                template,
                role="first",
                family=first_family,
                direction="rising",
                geo_confidence=1.0,
                row_id=first_row_id,
                **common,
            )
        ]
    else:
        second_direction = "declining" if case == "opposing" else "rising"
        geo_confidence = _FOREIGN_GEO_CONFIDENCE if case == "foreign" else 1.0
        second_row_id = first_row_id if case == "duplicate" else None
        evidence = [
            _evidence_row(
                template,
                role="first",
                family=first_family,
                direction="rising",
                geo_confidence=geo_confidence,
                row_id=first_row_id,
                **common,
            ),
            _evidence_row(
                template,
                role="second",
                family=second_family,
                direction=second_direction,
                geo_confidence=geo_confidence,
                row_id=second_row_id,
                **common,
            ),
        ]
    rekeyed["evidence"] = evidence
    return rekeyed


def _readiness_rules(run_date: date) -> ReadinessRules:
    return ReadinessRules(
        current_cutoff=_timestamp(run_date) - timedelta(days=_READINESS_WINDOW_DAYS),
        minimum_geo_confidence=_MINIMUM_GEO_CONFIDENCE,
    )


def evaluate_case(run_date: date, case: str) -> dict[str, object]:
    """Classify one case through the engine's own readiness and claim-semantics path."""
    rows = case_rows(run_date, case)
    try:
        validate_claim_semantics(
            [{"excerpt": row["excerpt"]} for row in rows["evidence"]],
            text_field="excerpt",
            audience_lenses=[],
        )
        records = [
            EvidenceRecord(
                row_id=str(row["row_id"]),
                source_family=str(row["source_family"]),
                direction=str(row["direction"]),
                published_at=row["published_at"],
                availability=str(row["availability"]),
                geo_confidence=float(row["geo_confidence"]),
            )
            for row in rows["evidence"]
        ]
        result = evaluate_readiness(records, _readiness_rules(run_date), quality_evaluated=True)
    except ValueError as error:
        return {"state": "rejected", "reasons": [str(error)]}
    reasons = sorted(
        reason for reason in result.reasons if not reason.startswith("independence_policy:")
    )
    return {"state": result.state, "reasons": reasons}


def corpus_sha256(run_date: date) -> str:
    """One digest over every case's fixture rows, so a rerun proves an unchanged corpus."""
    corpus = {case: case_rows(run_date, case) for case in CASES}
    text = json.dumps(corpus, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def alarm(cases: Mapping[str, Mapping[str, Any]], drill: object) -> dict[str, object] | None:
    """The diagnostic alarm a failed run raises, or None when every case passed."""
    failed = [case for case, outcome in cases.items() if not outcome["passed"]]
    if not failed:
        return None
    return {
        "code": ALARM_CODE,
        "severity": "ERROR",
        "failed_cases": failed,
        "drill": drill is not None,
    }


def _validate_qa_client(client: object) -> Any:
    return persistence.validate_real_client(client, QA_PROJECT, writer_identity=QA_IDENTITY)


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def result_rows(result: Mapping[str, Any]) -> list[dict[str, object]]:
    """One canary_results_v2 row per case, in the migrated QA table's exact columns."""
    run_date = date.fromisoformat(result["date"])
    drill_case = (result.get("drill") or {}).get("case")
    rows = []
    for case, outcome in result["cases"].items():
        if outcome["passed"]:
            error_code = None
        elif case == drill_case:
            error_code = DRILL_ERROR_CODE
        else:
            error_code = MISMATCH_ERROR_CODE
        rows.append(
            {
                "client_scope_id": QA_CLIENT_SCOPE_ID,
                "market_scope": list(QA_MARKET_SCOPE),
                "brand_config_id": QA_BRAND,
                "audience_lens_ids": [],
                "theme_id": QA_THEME,
                "run_id": run_id(run_date, case),
                "contract_version": f"{CANARY_NAME}_{CANARY_VERSION}",
                "canary_run_id": result["canary_run_id"],
                "canary_id": f"{CANARY_NAME}_{CANARY_VERSION}:{case}",
                "expected_result": _compact(outcome["expected"]),
                "actual_result": _compact(outcome["actual"]),
                "passed": outcome["passed"],
                "started_at": result["started_at"],
                "finished_at": result["finished_at"],
                "error_code": error_code,
            }
        )
    return rows


def append_results(client: object, result: Mapping[str, Any]) -> dict[str, object]:
    """Append the case rows to the QA results table; never merge, update or delete."""
    writer = _validate_qa_client(client)
    table = f"{QA_PROJECT}.{QA_DATASET}.{QA_RESULTS_TABLE}"
    rows = result_rows(result)
    errors = writer.insert_rows_json(table, rows)
    if errors:
        raise QaAppendRefusal(f"qa results append failed: {errors[0]}")
    return {"table": table, "appended": len(rows)}


def run_semantic_canaries(
    run_date: date,
    *,
    apply: bool,
    manifest: object,
    client_factory: Any = bigquery.Client,
    today: date | None = None,
    source_sha: str | None = None,
    expected: Mapping[str, tuple[str, tuple[str, ...]]] = EXPECTED_STATES,
    drill_break: str | None = None,
    clock: Any = None,
) -> dict[str, object]:
    """Prove the seven cases and, only when requested, append the results to QA.

    drill_break replaces one case's expectation with a state no engine path produces,
    so the operator can prove the alarm fires, then rerun without it on the same corpus.
    """
    _refuse(not isinstance(run_date, date) or isinstance(run_date, datetime), "qa_date_invalid")
    _refuse(drill_break is not None and drill_break not in CASES, "qa_case_unknown")
    now = clock if clock is not None else (lambda: datetime.now(UTC))
    started_at = now()
    contract = manifest_contract(manifest)
    _refuse(
        contract != {"dataset": QA_DATASET, "identity": QA_IDENTITY},
        "qa_contract_mismatch",
    )
    validate_qa_target(contract["dataset"], contract["identity"])
    persistence_canary.fixture_rule(run_date)
    cases: dict[str, dict[str, object]] = {}
    for case in CASES:
        state, reasons = expected[case]
        expectation = {"state": state, "reasons": list(reasons)}
        if case == drill_break:
            expectation = {
                "state": _DRILL_EXPECTATION["state"],
                "reasons": list(_DRILL_EXPECTATION["reasons"]),
            }
        actual = evaluate_case(run_date, case)
        cases[case] = {"expected": expectation, "actual": actual, "passed": actual == expectation}
    retention_until = run_date + timedelta(days=QA_RETENTION_DAYS)
    finished_at = now()
    drill = {"case": drill_break} if drill_break is not None else None
    result: dict[str, object] = {
        "canary": CANARY_NAME,
        "canary_version": CANARY_VERSION,
        "date": run_date.isoformat(),
        "target": {
            "project": QA_PROJECT,
            "dataset": QA_DATASET,
            "identity": QA_IDENTITY,
            "client_scope_id": QA_CLIENT_SCOPE_ID,
        },
        "retention_days": QA_RETENTION_DAYS,
        "retention_until": retention_until.isoformat(),
        "cases": cases,
        "status": "passed" if all(item["passed"] for item in cases.values()) else "failed",
        "alarm": alarm(cases, drill),
        "drill": drill,
        "corpus_sha256": corpus_sha256(run_date),
        "canary_run_id": f"{CANARY_NAME}_{CANARY_VERSION}_{run_date.isoformat()}_"
        + started_at.strftime("%Y%m%dT%H%M%S%fZ"),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "append": {"table": None, "appended": 0},
        "source_sha": source_sha if source_sha is not None else os.environ.get("SOURCE_SHA", ""),
    }
    if not apply:
        return result
    current = today if today is not None else datetime.now(UTC).date()
    _refuse(retention_until < current, "qa_retention_expired")
    client = client_factory(project=QA_PROJECT, location=QA_LOCATION)
    result["append"] = append_results(client, result)
    return result


def render_result(result: Mapping[str, object]) -> str:
    return json.dumps(result, separators=(",", ":"), sort_keys=True)


def _utc_today(clock: Any = None) -> date:
    """The current UTC day from an aware clock; the only implicit run date allowed."""
    now = clock() if clock is not None else datetime.now(UTC)
    _refuse(now.tzinfo is None or now.utcoffset() is None, "qa_date_invalid")
    return now.astimezone(UTC).date()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _load_manifest(path: str | None) -> dict[str, object]:
    location = path or os.environ.get("R03_CANONICAL_PROPOSAL_DIR")
    if not location:
        raise QaTargetRefusal("qa_manifest_required")
    candidate = Path(location)
    if candidate.is_dir():
        candidate = candidate / "resource_manifest.json"
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise QaTargetRefusal("qa_manifest_invalid") from error


def render_alarm(result: Mapping[str, Any]) -> str:
    """One structured ERROR line for the log-based alert, from a failed result."""
    cases = result.get("cases") or {}
    return json.dumps(
        {
            "severity": "ERROR",
            "message": ALARM_CODE,
            "canary": result.get("canary", CANARY_NAME),
            "canary_version": result.get("canary_version", CANARY_VERSION),
            "run_date": result.get("date"),
            "failed_cases": [case for case, item in cases.items() if not item.get("passed")],
            "drill": result.get("drill") is not None,
            "corpus_sha256": result.get("corpus_sha256"),
        },
        separators=(",", ":"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, type=_parse_date)
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--drill-break", default=None, choices=CASES)
    args = parser.parse_args()
    manifest = _load_manifest(args.manifest)
    run_date = args.date if args.date is not None else _utc_today()
    result = run_semantic_canaries(
        run_date, apply=args.apply, manifest=manifest, drill_break=args.drill_break
    )
    print(render_result(result))
    if result["status"] == "failed":
        print(render_alarm(result), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
