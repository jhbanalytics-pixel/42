"""Run the fixture-only Open Intelligence persistence canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.cloud import bigquery
from src.analysis.open_intelligence import persistence

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
WRITER_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
CANARY_VERSION = "v4"
FIXTURE_SCOPE = "fixture_scope"
FIXTURE_BRAND = "fixture_brand"
FIXTURE_THEME = "fixture_theme"
FIXTURE_MARKET = "za"
FIXTURE_URL = "https://example.invalid/open-intelligence/fixture"
_IDENTIFIER = re.compile(r"^(?:sig|ev|mem|pred|out)_[0-9a-f]{64}$")


class FixtureSafetyRefusal(ValueError):
    """The fixed canary payload no longer satisfies its fixture-only contract."""


class CleanupRefusal(RuntimeError):
    """A temporary table belonging to this canary remains after persistence."""


def _timestamp(run_date: date) -> datetime:
    return datetime(run_date.year, run_date.month, run_date.day, tzinfo=UTC)


def _scope(run_date: date) -> dict[str, object]:
    return {
        "client_scope_id": FIXTURE_SCOPE,
        "market_scope": [FIXTURE_MARKET],
        "brand_config_id": FIXTURE_BRAND,
        "audience_lens_ids": [],
        "theme_id": FIXTURE_THEME,
        "run_id": f"oi_persistence_canary_{CANARY_VERSION}_{run_date.isoformat()}",
        "contract_version": "3.0.0",
    }


def _fixture_identifier(prefix: str, role: str, run_date: date) -> str:
    preimage = f"oi_persistence_canary|{CANARY_VERSION}|{run_date.isoformat()}|{role}"
    return prefix + "_" + hashlib.sha256(preimage.encode()).hexdigest()


def fixture_rows(run_date: date) -> dict[str, list[dict[str, object]]]:
    """Return one deterministic row for each accepted persistence table."""
    now = _timestamp(run_date)
    signal_id = _fixture_identifier("sig", "signal", run_date)
    prior_signal_id = _fixture_identifier("sig", "prior_signal", run_date)
    evidence_id = _fixture_identifier("ev", "evidence", run_date)
    member_id = _fixture_identifier("mem", "member", run_date)
    prediction_id = _fixture_identifier("pred", "prediction", run_date)
    outcome_id = _fixture_identifier("out", "outcome", run_date)
    scope = _scope(run_date)
    return {
        "candidates": [
            {
                **scope,
                "signal_id": signal_id,
                "signal_date": run_date,
                "market": FIXTURE_MARKET,
                "label": "staging_fixture_canary",
                "label_member_identity": "za|keyword|staging_fixture_canary",
                "cluster_signature": "f" * 64,
                "cluster_build_version": "hybrid_graph_v2",
                "model_version": None,
                "discovery_mode": "replay",
                "topic_tags": ["staging_fixture_canary"],
                "novelty_score": 0.1,
                "velocity_score": 0.2,
                "breadth_score": 0.3,
                "independence_score": 0.4,
                "historical_similarity": None,
                "geo_confidence": 1.0,
                "evidence_state": "ready",
                "created_at": now,
            }
        ],
        "evidence": [
            {
                **scope,
                "signal_date": run_date,
                "market": FIXTURE_MARKET,
                "signal_id": signal_id,
                "evidence_id": evidence_id,
                "row_id": "staging_fixture_evidence_row",
                "source_family": "staging_fixture_source",
                "vendor_family": "staging_fixture_vendor",
                "channel_family": "staging_fixture_source",
                "platform": "staging_fixture_platform",
                "url": FIXTURE_URL,
                "published_at": now,
                "claim_role": "identity",
                "direction": "rising",
                "geo_confidence": 1.0,
                "source_label": "staging_fixture_source",
                "author_label": "staging_fixture_author",
                "excerpt": "Synthetic fixture evidence only.",
                "metric_label": "fixture_metric",
                "availability": "available",
                "evidence_state": "ready",
                "created_at": now,
            }
        ],
        "membership": [
            {
                **scope,
                "signal_date": run_date,
                "market": FIXTURE_MARKET,
                "signal_id": signal_id,
                "member_id": member_id,
                "member_identity": "za|keyword|staging_fixture_canary",
                "candidate_type": "keyword",
                "canonical_value": "staging_fixture_canary",
                "source_families": ["staging_fixture_source"],
                "vendor_families": ["staging_fixture_vendor"],
                "channel_families": ["staging_fixture_source"],
                "platforms": ["staging_fixture_platform"],
                "row_id": "staging_fixture_evidence_row",
                "qualifies_evidence": True,
                "created_at": now,
            }
        ],
        "lineage": [
            {
                **scope,
                "signal_date": run_date,
                "market": FIXTURE_MARKET,
                "from_signal_id": prior_signal_id,
                "to_signal_id": signal_id,
                "relation": "continues",
                "overlap_score": 1.0,
                "created_at": now,
            }
        ],
        "predictions": [
            {
                **scope,
                "prediction_id": prediction_id,
                "signal_id": signal_id,
                "signal_date": run_date,
                "market": FIXTURE_MARKET,
                "discovery_mode": "replay",
                "source_families": ["staging_fixture_source"],
                "evidence_state": "ready",
                "first_seen_at": now,
                "predicted_at": now,
                "expected_trajectory": "fixture_stable",
                "evaluation_date": run_date,
                "baseline": {
                    "velocity": 0.2,
                    "breadth": 0.3,
                    "evidence_family_count": 2,
                },
                "promotion_target": {
                    "velocity": 0.4,
                    "breadth": 0.5,
                    "evidence_family_count": 3,
                },
                "invalidation_condition": "staging_fixture_canary remains synthetic",
                "cluster_build_version": "hybrid_graph_v2",
                "source_family_map_version": "staging_fixture_source_map_v1",
                "rule_version": f"staging_fixture_canary_rules_{CANARY_VERSION}",
                "display_eligible": False,
            }
        ],
        "outcomes": [
            {
                **scope,
                "outcome_id": outcome_id,
                "prediction_id": prediction_id,
                "signal_id": signal_id,
                "signal_date": run_date,
                "market": FIXTURE_MARKET,
                "discovery_mode": "replay",
                "source_families": ["staging_fixture_source"],
                "source_family_map_version": "staging_fixture_source_map_v1",
                "evaluation_date": run_date,
                "evaluated_at": now,
                "outcome": "unresolved",
                "observed_velocity": None,
                "observed_breadth": None,
                "observed_evidence_family_count": None,
                "human_calibration_label": None,
                "human_reviewed_at": None,
                "resolution_reason": "staging_fixture_canary_unresolved",
                "rule_version": f"staging_fixture_canary_outcome_rules_{CANARY_VERSION}",
            }
        ],
    }


def fixture_rule(run_date: date) -> SimpleNamespace:
    return SimpleNamespace(
        status="replay_certified",
        rule_version=f"staging_fixture_canary_rules_{CANARY_VERSION}",
        replay_receipt_id=f"staging_fixture_canary_receipt_{CANARY_VERSION}",
        approved_at=_timestamp(run_date),
        approved_by="staging_fixture_canary_approver",
    )


def _refuse(condition: bool, message: str) -> None:
    if condition:
        raise FixtureSafetyRefusal(message)


def validate_fixture_contract(
    run_date: date,
    dataset: str,
    rows: dict[str, list[dict[str, object]]],
    rule: object,
) -> None:
    """Reject any payload that could escape the fixed staging fixture scope."""
    _refuse(dataset != DATASET, "dataset must be the exact main staging dataset")
    _refuse(not isinstance(run_date, date), "date must be a date")
    _refuse(tuple(rows) != tuple(persistence.TABLE_BINDINGS), "table set is invalid")
    for table in persistence.TABLE_BINDINGS:
        table_rows = rows[table]
        _refuse(len(table_rows) != 1, f"{table} row count must equal one")
        row = table_rows[0]
        _refuse(tuple(row) != persistence.ROW_FIELDS[table], f"{table} fields are invalid")
        _refuse(row["client_scope_id"] != FIXTURE_SCOPE, "fixture scope is required")
        _refuse(row["market_scope"] != [FIXTURE_MARKET], "market scope is invalid")
        _refuse(row["brand_config_id"] != FIXTURE_BRAND, "brand config is invalid")
        _refuse(row["audience_lens_ids"] != [], "audience lenses must be empty")
        _refuse(row["theme_id"] != FIXTURE_THEME, "theme is invalid")
        _refuse(row["run_id"] != _scope(run_date)["run_id"], "run ID is invalid")
        _refuse(row["signal_date"] != run_date, "date is invalid")
        _refuse(row["market"] != FIXTURE_MARKET, "market is invalid")
    candidate = rows["candidates"][0]
    evidence = rows["evidence"][0]
    membership = rows["membership"][0]
    lineage = rows["lineage"][0]
    prediction = rows["predictions"][0]
    outcome = rows["outcomes"][0]
    _refuse(
        candidate["label_member_identity"] != membership["member_identity"],
        "label member identity conflicts with membership identity",
    )
    matching_memberships = [
        item
        for item in rows["membership"]
        if item["member_identity"] == candidate["label_member_identity"]
    ]
    _refuse(len(matching_memberships) != 1, "label member identity must match exactly once")
    expected_member_identity = (
        f"{membership['market']}|{membership['candidate_type']}|{membership['canonical_value']}"
    )
    _refuse(
        membership["member_identity"] != expected_member_identity,
        "membership identity facts are invalid",
    )
    _refuse(
        candidate["label"] != membership["canonical_value"],
        "candidate label conflicts with membership canonical value",
    )
    _refuse(
        candidate["cluster_build_version"] != "hybrid_graph_v2"
        or prediction["cluster_build_version"] != "hybrid_graph_v2",
        "fixture graph version is invalid",
    )
    for field, value, prefix, role in (
        ("signal_id", candidate["signal_id"], "sig", "signal"),
        ("evidence_id", evidence["evidence_id"], "ev", "evidence"),
        ("member_id", membership["member_id"], "mem", "member"),
        ("from_signal_id", lineage["from_signal_id"], "sig", "prior_signal"),
        ("to_signal_id", lineage["to_signal_id"], "sig", "signal"),
        ("prediction_id", prediction["prediction_id"], "pred", "prediction"),
        ("outcome_id", outcome["outcome_id"], "out", "outcome"),
    ):
        _refuse(
            not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None,
            f"{field} is malformed",
        )
        _refuse(
            value != _fixture_identifier(prefix, role, run_date),
            f"{field} does not match the versioned fixture identity",
        )
    _refuse(evidence["url"] != FIXTURE_URL, "URL must remain under example.invalid")
    _refuse(
        evidence["vendor_family"] == evidence["channel_family"],
        "vendor family must remain distinct from channel family",
    )
    _refuse(
        evidence["source_family"] != evidence["channel_family"],
        "channel family must equal source family",
    )
    _refuse(
        tuple(membership["vendor_families"]) != (evidence["vendor_family"],),
        "membership vendor family is invalid",
    )
    _refuse(
        tuple(membership["channel_families"]) != tuple(membership["source_families"])
        or tuple(membership["channel_families"]) != (evidence["channel_family"],),
        "membership channel family is invalid",
    )
    _refuse(candidate["discovery_mode"] != "replay", "discovery mode must be replay")
    _refuse(prediction["discovery_mode"] != "replay", "discovery mode must be replay")
    _refuse(outcome["discovery_mode"] != "replay", "outcome discovery mode must be replay")
    _refuse(candidate["evidence_state"] != "ready", "evidence state must be ready")
    _refuse(evidence["evidence_state"] != "ready", "evidence state must be ready")
    _refuse(prediction["evidence_state"] != "ready", "evidence state must be ready")
    _refuse(prediction["display_eligible"] is not False, "display eligibility must be false")
    _refuse(
        outcome["prediction_id"] != prediction["prediction_id"], "outcome prediction is invalid"
    )
    _refuse(outcome["signal_id"] != candidate["signal_id"], "outcome signal is invalid")
    _refuse(outcome["outcome"] != "unresolved", "outcome state must remain unresolved")
    _refuse(
        any(
            outcome[field] is not None
            for field in (
                "observed_velocity",
                "observed_breadth",
                "observed_evidence_family_count",
            )
        ),
        "outcome observed metrics must remain null",
    )
    _refuse(outcome["human_calibration_label"] is not None, "outcome human label must be null")
    _refuse(outcome["human_reviewed_at"] is not None, "outcome human review must be null")
    _refuse(lineage["to_signal_id"] != candidate["signal_id"], "lineage target is invalid")
    _refuse(lineage["relation"] != "continues", "lineage relation is invalid")
    _refuse(getattr(rule, "status", None) != "replay_certified", "rule status is invalid")
    _refuse(
        "staging_fixture_canary" not in str(getattr(rule, "replay_receipt_id", "")),
        "receipt must identify the staging fixture",
    )
    _refuse(
        "staging_fixture_canary" not in str(getattr(rule, "approved_by", "")),
        "approver must identify the staging fixture",
    )


def build_fixture_batch(run_date: date) -> persistence.OpenIntelligenceRowBatch:
    rows = fixture_rows(run_date)
    rule = fixture_rule(run_date)
    validate_fixture_contract(run_date, DATASET, rows, rule)
    return persistence.OpenIntelligenceRowBatch(**rows)


def _target() -> persistence.PersistenceTarget:
    return persistence.PersistenceTarget(
        project=PROJECT,
        dataset=DATASET,
        location=LOCATION,
        writer_identity=WRITER_IDENTITY,
    )


def _counts(result: persistence.PersistenceResult) -> dict[str, dict[str, int]]:
    return {
        table: {
            "inserted": result.inserted_counts[table],
            "unchanged": result.unchanged_counts[table],
        }
        for table in persistence.TABLE_BINDINGS
    }


def _assert_counts(result: persistence.PersistenceResult, *, inserted: int, unchanged: int) -> None:
    for table in persistence.TABLE_BINDINGS:
        actual = (result.inserted_counts[table], result.unchanged_counts[table])
        if actual != (inserted, unchanged) or result.conflict_counts[table] != 0:
            raise FixtureSafetyRefusal(f"{table} persistence counts are invalid")


def _assert_first_counts(result: persistence.PersistenceResult) -> None:
    for table in persistence.TABLE_BINDINGS:
        inserted = result.inserted_counts[table]
        unchanged = result.unchanged_counts[table]
        if inserted + unchanged != 1 or result.conflict_counts[table] != 0:
            raise FixtureSafetyRefusal(f"{table} first persistence counts are invalid")


def _assert_write_receipt(
    result: persistence.PersistenceResult,
    expected_digests: dict[str, str],
) -> None:
    if result.cleanup_state != "complete":
        raise FixtureSafetyRefusal("cleanup state is not complete")
    if dict(result.statement_digests) != expected_digests:
        raise FixtureSafetyRefusal("statement digest differs from the dry plan")


def _cleanup_proof(client: Any, statement_digests: dict[str, str]) -> dict[str, list[str]]:
    prefixes = [f"_oi_{statement_digests[table][:12]}_" for table in persistence.TABLE_BINDINGS]
    remaining = [
        table.table_id
        for table in client.list_tables(f"{PROJECT}.{DATASET}")
        if any(table.table_id.startswith(prefix) for prefix in prefixes)
    ]
    if remaining:
        raise CleanupRefusal(f"temporary table remains: {remaining[0]}")
    return {"prefixes": prefixes, "remaining": remaining}


def run_canary(
    run_date: date,
    *,
    apply: bool,
    persistence_api: Any = persistence,
    client_factory: Any = bigquery.Client,
    source_sha: str | None = None,
) -> dict[str, object]:
    """Run the deterministic dry plan and, only when requested, its fixture writes."""
    rows = fixture_rows(run_date)
    rule = fixture_rule(run_date)
    validate_fixture_contract(run_date, DATASET, rows, rule)
    batch = persistence_api.OpenIntelligenceRowBatch(**rows)
    dry_run = persistence_api.persist_open_intelligence_rows(
        project=PROJECT,
        dataset=DATASET,
        client=_target(),
        batch=batch,
        rule_bundle=rule,
        dry_run=True,
    )
    if dry_run.cleanup_state != "not_started":
        raise FixtureSafetyRefusal("dry-run cleanup state is invalid")
    expected_digests = dict(dry_run.statement_digests)
    result: dict[str, object] = {
        "date": run_date.isoformat(),
        "run_id": _scope(run_date)["run_id"],
        "first_counts": {
            table: {"inserted": 0, "unchanged": 0} for table in persistence.TABLE_BINDINGS
        },
        "second_counts": {
            table: {"inserted": 0, "unchanged": 0} for table in persistence.TABLE_BINDINGS
        },
        "conflict_proof": {"count": 0, "raised": False},
        "statement_digests": expected_digests,
        "cleanup_proof": {"prefixes": [], "remaining": []},
        "source_sha": source_sha if source_sha is not None else os.environ.get("SOURCE_SHA", ""),
    }
    if not apply:
        return result

    client = client_factory(project=PROJECT, location=LOCATION)
    first = persistence_api.persist_open_intelligence_rows(
        project=PROJECT,
        dataset=DATASET,
        client=client,
        batch=batch,
        rule_bundle=rule,
        dry_run=False,
    )
    _assert_write_receipt(first, expected_digests)
    _assert_first_counts(first)
    result["first_counts"] = _counts(first)
    result["cleanup_proof"] = _cleanup_proof(client, expected_digests)

    second = persistence_api.persist_open_intelligence_rows(
        project=PROJECT,
        dataset=DATASET,
        client=client,
        batch=batch,
        rule_bundle=rule,
        dry_run=False,
    )
    _assert_write_receipt(second, expected_digests)
    _assert_counts(second, inserted=0, unchanged=1)
    result["second_counts"] = _counts(second)
    result["cleanup_proof"] = _cleanup_proof(client, expected_digests)

    altered_rows = fixture_rows(run_date)
    altered_rows["candidates"][0]["novelty_score"] = 0.11
    validate_fixture_contract(run_date, DATASET, altered_rows, rule)
    altered_batch = persistence_api.OpenIntelligenceRowBatch(**altered_rows)
    altered_dry_run = persistence_api.persist_open_intelligence_rows(
        project=PROJECT,
        dataset=DATASET,
        client=_target(),
        batch=altered_batch,
        rule_bundle=rule,
        dry_run=True,
    )
    if altered_dry_run.cleanup_state != "not_started":
        raise FixtureSafetyRefusal("altered dry-run cleanup state is invalid")
    conflict_digests = dict(altered_dry_run.statement_digests)
    if conflict_digests["candidates"] == expected_digests["candidates"]:
        raise FixtureSafetyRefusal("altered candidate digest did not change")
    for table in tuple(persistence.TABLE_BINDINGS)[1:]:
        if conflict_digests[table] != expected_digests[table]:
            raise FixtureSafetyRefusal("altered later-table digest changed")
    try:
        persistence_api.persist_open_intelligence_rows(
            project=PROJECT,
            dataset=DATASET,
            client=client,
            batch=altered_batch,
            rule_bundle=rule,
            dry_run=False,
        )
    except persistence_api.CandidateRunConflict as error:
        if error.result is None or error.result.conflict_counts["candidates"] != 1:
            raise FixtureSafetyRefusal("candidate conflict count is invalid") from error
        _assert_write_receipt(error.result, conflict_digests)
        if error.result.inserted_counts["candidates"] != 0:
            raise FixtureSafetyRefusal("candidate conflict inserted rows") from error
        for table in tuple(persistence.TABLE_BINDINGS)[1:]:
            if any(
                counts[table] != 0
                for counts in (
                    error.result.inserted_counts,
                    error.result.unchanged_counts,
                    error.result.conflict_counts,
                )
            ):
                raise FixtureSafetyRefusal("later table counts are not zero") from error
        result["conflict_proof"] = {"count": 1, "raised": True}
        result["cleanup_proof"] = _cleanup_proof(client, conflict_digests)
    else:
        raise FixtureSafetyRefusal("candidate conflict was not raised")
    return result


def render_result(result: dict[str, object]) -> str:
    return json.dumps(result, separators=(",", ":"), sort_keys=True)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=_parse_date)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(render_result(run_canary(args.date, apply=args.apply)))


if __name__ == "__main__":
    main()
