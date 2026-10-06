"""Pure persistence-plan boundary tests for open intelligence rows."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import TypeAlias, get_type_hints

import pytest
from google.auth.exceptions import RefreshError
from google.cloud import bigquery
from src.analysis.open_intelligence import persistence

NOW = datetime(2026, 8, 26, 6, 45, tzinfo=UTC)
SIGNAL_DATE = date(2026, 8, 26)
PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
LOCATION = "US"
WRITER_IDENTITY = "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"


def _provenance_membership():
    from src.analysis.open_intelligence.source_provenance import encode_source_provenance

    row = _membership()
    row.update(source_families=["news"], vendor_families=["rss"], channel_families=["news"])
    row["source_provenance_json"] = encode_source_provenance(
        {
            "contract_version": "candidate_source_provenance_v1",
            "coverage_basis": "sampled_record_support",
            "source_snapshot_digest": "a" * 64,
            "resolution_state": "resolved",
            "pairs": [
                {
                    "vendor_family": "rss",
                    "channel_family": "news",
                    "source_table": "enriched_content",
                    "sample_refs": [{"row_id": "row_001", "row_digest": "b" * 64}],
                }
            ],
            "unresolved_sample_refs": [],
        }
    )
    row["member_id"] = persistence._provenance_id(row, json.loads(row["source_provenance_json"]))
    return row


def test_v3_canonical_membership_binds_exact_envelope_and_sql_string():
    row = _provenance_membership()
    value = persistence.canonical_typed_json(
        "membership", row, cluster_build_version="hybrid_graph_v3"
    )
    assert json.loads(value)["source_provenance_json"] == {
        "type": "string",
        "value": row["source_provenance_json"],
    }
    sql = persistence.canonical_typed_json_sql(
        "membership", "m", cluster_build_version="hybrid_graph_v3"
    )
    assert "TO_JSON_STRING(m.source_provenance_json)" in sql


@pytest.mark.parametrize("mutation", ["null", "missing", "spaces", "summary", "version"])
def test_v3_canonical_membership_refuses_provenance_tampering(mutation):
    row = _provenance_membership()
    if mutation == "null":
        row["source_provenance_json"] = None
    elif mutation == "missing":
        row.pop("source_provenance_json")
    elif mutation == "spaces":
        row["source_provenance_json"] = json.dumps(
            json.loads(row["source_provenance_json"]), indent=2
        )
    elif mutation == "summary":
        row["vendor_families"] = ["google"]
    else:
        row["source_provenance_json"] = row["source_provenance_json"].replace(
            "candidate_source_provenance_v1", "unknown"
        )
    with pytest.raises(persistence.BatchInvalid):
        persistence.canonical_typed_json("membership", row, cluster_build_version="hybrid_graph_v3")


def test_legacy_nullable_provenance_keeps_exact_bytes():
    row = _membership()
    expected = persistence.canonical_typed_json("membership", row)
    for version in (None, "hybrid_graph_v1", "hybrid_graph_v2"):
        assert (
            persistence.canonical_typed_json(
                "membership", {**row, "source_provenance_json": None}, cluster_build_version=version
            )
            == expected
        )
    with pytest.raises(persistence.BatchInvalid):
        persistence.canonical_typed_json(
            "membership", _provenance_membership(), cluster_build_version="hybrid_graph_v2"
        )
    with pytest.raises(persistence.BatchInvalid):
        persistence.canonical_typed_json("membership", row, cluster_build_version="unknown")


def test_v3_batch_requires_exact_candidate_join_and_version():
    row = _provenance_membership()
    candidate = {**_candidate(), "cluster_build_version": "hybrid_graph_v3"}
    batch = _batch(
        candidates=(candidate,),
        membership=(row,),
        predictions=(),
        cluster_build_version="hybrid_graph_v3",
    )
    assert batch.membership[0]["source_provenance_json"] == row["source_provenance_json"]
    for candidates in (
        (),
        (candidate, candidate),
        (_candidate(),),
        ({**candidate, "market": "ng"},),
    ):
        with pytest.raises(persistence.BatchInvalid):
            _batch(
                candidates=candidates, membership=(row,), cluster_build_version="hybrid_graph_v3"
            )


@pytest.mark.parametrize(
    "field",
    ["member_id", "member_identity", "row_id", "canonical_value", "market", "client_scope_id"],
)
def test_v3_member_id_rejects_cross_row_binding(field):
    row = _provenance_membership()
    row[field] = "other"
    with pytest.raises(persistence.BatchInvalid):
        persistence.canonical_typed_json("membership", row, cluster_build_version="hybrid_graph_v3")


@pytest.mark.parametrize("versions", [("unknown",), ("hybrid_graph_v1", "hybrid_graph_v2")])
def test_default_batch_refuses_unknown_and_mixed_candidate_versions(versions):
    candidates = tuple(
        {**_candidate(), "signal_id": f"signal_{index}", "cluster_build_version": version}
        for index, version in enumerate(versions)
    )
    with pytest.raises(persistence.BatchInvalid, match="source_provenance_version_invalid"):
        _batch(candidates=candidates, membership=())


def test_receipt_version_must_agree_with_default_batch_candidates():
    from dataclasses import asdict

    from tests.unit.test_intelligence_brain_live_reader import _receipt_and_rows

    receipt, rows = _receipt_and_rows()
    values = asdict(receipt)
    values["cluster_build_version"] = "hybrid_graph_v1"
    batch = persistence.OpenIntelligenceRowBatch(
        **{table: tuple(rows[name]) for table, name in persistence.TABLE_BINDINGS.items()}
    )
    assert batch.candidates[0]["cluster_build_version"] == "hybrid_graph_v2"
    parameters = inspect.signature(persistence.build_run_receipt_row).parameters
    with pytest.raises(persistence.BatchInvalid, match="source_provenance_version_invalid"):
        persistence.build_run_receipt_row(
            batch,
            rows["signal_analysis_v2"],
            **{key: value for key, value in values.items() if key in parameters},
        )


def test_v3_member_id_uses_supplied_shared_derivation():
    from src.analysis.open_intelligence.candidates import Observation
    from src.analysis.open_intelligence.pipeline import _member_id
    from src.analysis.open_intelligence.source_provenance import provenance_member_id

    row = _provenance_membership()
    observation = Observation(
        market=row["market"],
        candidate_type=row["candidate_type"],
        term=row["canonical_value"],
        row_ids=(row["row_id"],),
        source_families=row["source_families"],
        platforms=row["platforms"],
    )
    legacy = _member_id(
        SimpleNamespace(client_scope_id=row["client_scope_id"]),
        SIGNAL_DATE,
        observation,
        row["row_id"],
    )
    assert row["member_id"] == provenance_member_id(
        legacy, json.loads(row["source_provenance_json"])
    )


def test_v3_transaction_and_temporary_schema_include_provenance():
    temporary = persistence._temporary_table(
        PROJECT, DATASET, "membership", "a" * 64, "hybrid_graph_v3"
    )
    assert temporary.schema[-1].name == "source_provenance_json"
    sql = persistence._transaction_sql(PROJECT, DATASET, "membership", temporary, "hybrid_graph_v3")
    assert "target.`source_provenance_json`" in sql
    assert "staged.`source_provenance_json`" in sql
    assert "BEGIN TRANSACTION" in sql
    assert "immutable_conflict" in sql


@pytest.mark.parametrize(
    "factory",
    [
        persistence.row_set_digest_sql,
        persistence.candidate_projection_digest_sql,
        persistence.candidate_projection_content_digest_sql,
    ],
)
def test_v3_digest_sql_requires_explicit_version_and_unique_matching_candidate(factory):
    legacy = factory("run_fixture")
    assert "open_intelligence_run_receipts_v1" not in legacy
    assert "IN ('hybrid_graph_v1', 'hybrid_graph_v2')" in legacy
    sql = factory("run_fixture", cluster_build_version="hybrid_graph_v3")
    assert "SELECT COUNT(*) FROM" in sql
    for key in ("client_scope_id", "run_id", "signal_date", "market", "signal_id"):
        assert f"version_candidate.{key} = " in sql
    assert "source_provenance_json IS NOT NULL" in sql
    assert "ERROR('source_provenance_conflict')" in sql


def _v3_bridge_inputs():
    from dataclasses import replace

    from src.analysis.open_intelligence.candidates import Observation
    from src.analysis.open_intelligence.pipeline import (
        DynamicSignalRunResult,
        ProvenanceSignalRunResult,
    )

    from tests.unit import test_dynamic_signal_rows as fixtures

    component = replace(
        fixtures.component(), label="repair routine", build_version="hybrid_graph_v3"
    )
    key = persistence.component_bridge_key(component)
    values = _measured_bridge_inputs(component, key, fixtures.readiness())
    projection = values["result"].evidence_projection
    member = projection.memberships_by_component["projection_component"][0]
    envelope = json.loads(_provenance_membership()["source_provenance_json"])
    row = {
        **_provenance_membership(),
        "client_scope_id": fixtures.scope().client_scope_id,
        "signal_date": fixtures.SIGNAL_DATE,
        "canonical_value": member.canonical_value,
        "platforms": member.platforms,
        "row_id": member.row_id,
    }
    member = replace(
        member,
        member_id=persistence._provenance_id(row, envelope),
        source_families=("news",),
        vendor_families=("rss",),
        channel_families=("news",),
    )
    projection.memberships_by_component["projection_component"] = (member,)
    observation = Observation(
        market="za",
        candidate_type="keyword",
        term=member.canonical_value,
        row_ids=(member.row_id,),
        source_families=("news",),
        platforms=member.platforms,
    )
    run = DynamicSignalRunResult(
        run_id="fixture",
        contract_version="2.0.0",
        rule_version="composition_rules_v3",
        source_windows={},
        input_counts={},
        observation_count=1,
        component_count=1,
        persistable_count=1,
        row_counts={},
        skipped_components=(),
        missing_work=(),
        dry_run=True,
        persistence_result=None,
        error_state=None,
        candidate_inputs=None,
        observations=(observation,),
        components=(component,),
        evidence_projection=projection,
    )
    values["result"] = ProvenanceSignalRunResult(run, {member.member_identity: envelope}, "a" * 64)
    return values


def test_v3_bridge_unwraps_and_persists_actual_pairs():
    outcome = _bridge(**_v3_bridge_inputs())
    assert len(outcome.batch.membership) == 1
    assert outcome.batch.cluster_build_version == "hybrid_graph_v3"
    member = outcome.batch.membership[0]
    assert member["vendor_families"] == ("rss",)
    assert member["channel_families"] == ("news",)
    assert (
        json.loads(member["source_provenance_json"])["coverage_basis"] == "sampled_record_support"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "digest",
        "envelope_digest",
        "missing",
        "extra",
        "rule",
        "component",
        "unwrapped",
        "member_id",
    ],
)
def test_v3_bridge_rejects_wrapper_and_row_binding_changes(mutation):
    from dataclasses import replace

    values = _v3_bridge_inputs()
    wrapper = values["result"]
    if mutation == "digest":
        wrapper = replace(wrapper, source_snapshot_digest="z" * 64)
    elif mutation == "envelope_digest":
        wrapper = replace(wrapper, source_snapshot_digest="b" * 64)
    elif mutation == "missing":
        wrapper = replace(wrapper, source_provenance_by_member={})
    elif mutation == "extra":
        wrapper = replace(
            wrapper,
            source_provenance_by_member={
                **wrapper.source_provenance_by_member,
                "extra": next(iter(wrapper.source_provenance_by_member.values())),
            },
        )
    elif mutation == "rule":
        wrapper = replace(wrapper, run=replace(wrapper.run, rule_version="composition_rules_v2"))
    elif mutation == "component":
        wrapper = replace(
            wrapper,
            run=replace(
                wrapper.run,
                components=(replace(wrapper.run.components[0], build_version="hybrid_graph_v2"),),
            ),
        )
    elif mutation == "unwrapped":
        wrapper = wrapper.run
    else:
        projection = wrapper.run.evidence_projection
        member = projection.memberships_by_component["projection_component"][0]
        projection.memberships_by_component["projection_component"] = (
            replace(member, member_id="mem_" + "f" * 64),
        )
    values["result"] = wrapper
    with pytest.raises(persistence.BatchInvalid):
        _bridge(**values)


@pytest.mark.parametrize(
    "mutation",
    [
        "candidate_missing",
        "candidate_duplicate",
        "candidate_version",
        "receipt_missing",
        "receipt_duplicate",
        "receipt_version",
        "null_envelope",
        "precompletion",
        "incomplete_partitions",
        "candidate_count",
        "membership_count",
        "member_scope",
        "legacy_envelope",
    ],
)
def test_completed_sql_selector_withholds_whole_bad_run_only(mutation):
    import sqlite3

    class AnyValue:
        def __init__(self):
            self.value = None

        def step(self, value):
            self.value = value

        def finalize(self):
            return self.value

    def refuse(reason):
        raise ValueError(reason)

    sql = persistence._membership_json_sql(
        "m", project="fixture", dataset="staging", from_receipt=True
    )
    sql = sql.replace(
        persistence.canonical_typed_json_sql(
            "membership", "m", cluster_build_version="hybrid_graph_v3"
        ),
        "'v3_bytes'",
    )
    sql = sql.replace(persistence.canonical_typed_json_sql("membership", "m"), "'legacy_bytes'")
    db = sqlite3.connect(":memory:")
    db.create_aggregate("ANY_VALUE", 1, AnyValue)

    class CountIf:
        def __init__(self):
            self.count = 0

        def step(self, value):
            self.count += bool(value)

        def finalize(self):
            return self.count

    db.create_aggregate("COUNTIF", 1, CountIf)
    db.create_function("ERROR", 1, refuse)
    keys = "client_scope_id TEXT, run_id TEXT, signal_date TEXT, market TEXT, signal_id TEXT"
    for relation in ("signal_candidates_v2", "open_intelligence_run_receipts_v1"):
        extra = (
            ", status TEXT, complete_partitions INTEGER, candidate_count INTEGER, membership_count INTEGER"
            if relation == "open_intelligence_run_receipts_v1"
            else ""
        )
        db.execute(
            f"CREATE TABLE `fixture.staging.{relation}` ({keys}, cluster_build_version TEXT{extra})"
        )
        db.executemany(
            f"INSERT INTO `fixture.staging.{relation}` VALUES ("
            + ",".join("?" for _ in range(10 if extra else 6))
            + ")",
            [
                ("scope", run, "2026-09-06", "za", "signal", "hybrid_graph_v3")
                + (("completed", 1, 1, 2) if extra else ())
                for run in ("good", "bad")
            ],
        )
    db.execute(
        f"CREATE TABLE `fixture.staging.signal_membership_v2` ({keys}, source_provenance_json TEXT)"
    )
    db.executemany(
        "INSERT INTO `fixture.staging.signal_membership_v2` VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("scope", run, "2026-09-06", "za", "signal", "envelope")
            for run in ("good", "bad")
            for _ in range(2)
        ],
    )
    if mutation == "null_envelope":
        db.execute(
            "UPDATE `fixture.staging.signal_membership_v2` SET source_provenance_json = NULL WHERE rowid = 3"
        )
    elif mutation == "member_scope":
        db.execute(
            "UPDATE `fixture.staging.signal_membership_v2` SET client_scope_id = 'other' WHERE rowid = 3"
        )
    elif mutation == "legacy_envelope":
        for relation in ("signal_candidates_v2", "open_intelligence_run_receipts_v1"):
            db.execute(
                f"UPDATE `fixture.staging.{relation}` SET cluster_build_version = 'hybrid_graph_v2' WHERE run_id = 'bad'"
            )
    elif mutation in (
        "precompletion",
        "incomplete_partitions",
        "candidate_count",
        "membership_count",
    ):
        assignment = {
            "precompletion": "status = 'started'",
            "incomplete_partitions": "complete_partitions = 0",
            "candidate_count": "candidate_count = 2",
            "membership_count": "membership_count = 3",
        }[mutation]
        db.execute(
            f"UPDATE `fixture.staging.open_intelligence_run_receipts_v1` SET {assignment} WHERE run_id = 'bad'"
        )
    else:
        relation = (
            "signal_candidates_v2"
            if mutation.startswith("candidate")
            else "open_intelligence_run_receipts_v1"
        )
        target = f"`fixture.staging.{relation}`"
        if mutation.endswith("missing"):
            db.execute(f"DELETE FROM {target} WHERE run_id = 'bad'")
        elif mutation.endswith("duplicate"):
            db.execute(f"INSERT INTO {target} SELECT * FROM {target} WHERE run_id = 'bad'")
            if relation == "signal_candidates_v2":
                db.execute(
                    "UPDATE `fixture.staging.open_intelligence_run_receipts_v1` "
                    "SET candidate_count = 2 WHERE run_id = 'bad'"
                )
        else:
            db.execute(
                f"UPDATE {target} SET cluster_build_version = 'unknown' WHERE run_id = 'bad'"
            )
    authority = persistence._completed_provenance_runs_sql(
        project=PROJECT, dataset=DATASET
    ).replace(f"{PROJECT}.{DATASET}", "fixture.staging")
    query = (
        f"WITH valid_provenance_runs AS ({authority}) SELECT m.run_id, {sql} "
        "FROM `fixture.staging.signal_membership_v2` m "
        "JOIN valid_provenance_runs m_authority ON m_authority.run_id = m.run_id ORDER BY m.run_id"
    )
    assert db.execute(query).fetchall() == [("good", "v3_bytes"), ("good", "v3_bytes")]
    db.close()


_DEFAULT_TARGET = object()

EXPECTED_ROW_FIELDS = {
    "candidates": (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "signal_id",
        "signal_date",
        "market",
        "label",
        "label_member_identity",
        "cluster_signature",
        "cluster_build_version",
        "model_version",
        "discovery_mode",
        "topic_tags",
        "novelty_score",
        "velocity_score",
        "breadth_score",
        "independence_score",
        "historical_similarity",
        "geo_confidence",
        "evidence_state",
        "created_at",
    ),
    "evidence": (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "signal_date",
        "market",
        "signal_id",
        "evidence_id",
        "row_id",
        "source_family",
        "vendor_family",
        "channel_family",
        "platform",
        "url",
        "published_at",
        "claim_role",
        "direction",
        "geo_confidence",
        "source_label",
        "author_label",
        "excerpt",
        "metric_label",
        "availability",
        "evidence_state",
        "created_at",
    ),
    "membership": (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "signal_date",
        "market",
        "signal_id",
        "member_id",
        "member_identity",
        "candidate_type",
        "canonical_value",
        "source_families",
        "vendor_families",
        "channel_families",
        "platforms",
        "row_id",
        "qualifies_evidence",
        "created_at",
    ),
    "lineage": (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "signal_date",
        "market",
        "from_signal_id",
        "to_signal_id",
        "relation",
        "overlap_score",
        "created_at",
    ),
    "predictions": (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "prediction_id",
        "signal_id",
        "signal_date",
        "market",
        "discovery_mode",
        "source_families",
        "evidence_state",
        "first_seen_at",
        "predicted_at",
        "expected_trajectory",
        "evaluation_date",
        "baseline",
        "promotion_target",
        "invalidation_condition",
        "cluster_build_version",
        "source_family_map_version",
        "rule_version",
        "display_eligible",
    ),
    "outcomes": (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "outcome_id",
        "prediction_id",
        "signal_id",
        "signal_date",
        "market",
        "discovery_mode",
        "source_families",
        "source_family_map_version",
        "evaluation_date",
        "evaluated_at",
        "outcome",
        "observed_velocity",
        "observed_breadth",
        "observed_evidence_family_count",
        "human_calibration_label",
        "human_reviewed_at",
        "resolution_reason",
        "rule_version",
    ),
}
EXPECTED_NATURAL_KEYS = {
    "candidates": ("client_scope_id", "signal_date", "market", "signal_id", "run_id"),
    "evidence": (
        "client_scope_id",
        "signal_date",
        "market",
        "signal_id",
        "evidence_id",
        "run_id",
    ),
    "membership": (
        "client_scope_id",
        "signal_date",
        "market",
        "signal_id",
        "member_id",
        "run_id",
    ),
    "lineage": (
        "client_scope_id",
        "signal_date",
        "market",
        "from_signal_id",
        "to_signal_id",
        "relation",
        "run_id",
    ),
    "predictions": ("prediction_id",),
    "outcomes": ("prediction_id", "evaluation_date", "run_id"),
}
EXPECTED_TABLE_BINDINGS = {
    "candidates": "signal_candidates_v2",
    "evidence": "signal_evidence_v2",
    "membership": "signal_membership_v2",
    "lineage": "signal_lineage_v2",
    "predictions": "signal_predictions_v2",
    "outcomes": "signal_outcomes_v2",
}
SchemaNode: TypeAlias = tuple[str, str, str, tuple["SchemaNode", ...]]
EXPECTED_SCHEMA_TREES: dict[str, tuple[SchemaNode, ...]] = {
    "candidates": (
        ("client_scope_id", "STRING", "REQUIRED", ()),
        ("market_scope", "STRING", "REPEATED", ()),
        ("brand_config_id", "STRING", "REQUIRED", ()),
        ("audience_lens_ids", "STRING", "REPEATED", ()),
        ("theme_id", "STRING", "REQUIRED", ()),
        ("run_id", "STRING", "REQUIRED", ()),
        ("contract_version", "STRING", "REQUIRED", ()),
        ("signal_id", "STRING", "REQUIRED", ()),
        ("signal_date", "DATE", "REQUIRED", ()),
        ("market", "STRING", "REQUIRED", ()),
        ("label", "STRING", "REQUIRED", ()),
        ("label_member_identity", "STRING", "REQUIRED", ()),
        ("cluster_signature", "STRING", "REQUIRED", ()),
        ("cluster_build_version", "STRING", "REQUIRED", ()),
        ("model_version", "STRING", "NULLABLE", ()),
        ("discovery_mode", "STRING", "REQUIRED", ()),
        ("topic_tags", "STRING", "REPEATED", ()),
        ("novelty_score", "FLOAT64", "REQUIRED", ()),
        ("velocity_score", "FLOAT64", "REQUIRED", ()),
        ("breadth_score", "FLOAT64", "REQUIRED", ()),
        ("independence_score", "FLOAT64", "REQUIRED", ()),
        ("historical_similarity", "FLOAT64", "NULLABLE", ()),
        ("geo_confidence", "FLOAT64", "REQUIRED", ()),
        ("evidence_state", "STRING", "REQUIRED", ()),
        ("created_at", "TIMESTAMP", "REQUIRED", ()),
    ),
    "evidence": (
        ("client_scope_id", "STRING", "REQUIRED", ()),
        ("market_scope", "STRING", "REPEATED", ()),
        ("brand_config_id", "STRING", "REQUIRED", ()),
        ("audience_lens_ids", "STRING", "REPEATED", ()),
        ("theme_id", "STRING", "REQUIRED", ()),
        ("run_id", "STRING", "REQUIRED", ()),
        ("contract_version", "STRING", "REQUIRED", ()),
        ("signal_date", "DATE", "REQUIRED", ()),
        ("market", "STRING", "REQUIRED", ()),
        ("signal_id", "STRING", "REQUIRED", ()),
        ("evidence_id", "STRING", "REQUIRED", ()),
        ("row_id", "STRING", "REQUIRED", ()),
        ("source_family", "STRING", "REQUIRED", ()),
        ("vendor_family", "STRING", "REQUIRED", ()),
        ("channel_family", "STRING", "REQUIRED", ()),
        ("platform", "STRING", "REQUIRED", ()),
        ("url", "STRING", "NULLABLE", ()),
        ("published_at", "TIMESTAMP", "NULLABLE", ()),
        ("claim_role", "STRING", "REQUIRED", ()),
        ("direction", "STRING", "REQUIRED", ()),
        ("geo_confidence", "FLOAT64", "REQUIRED", ()),
        ("source_label", "STRING", "NULLABLE", ()),
        ("author_label", "STRING", "NULLABLE", ()),
        ("excerpt", "STRING", "NULLABLE", ()),
        ("metric_label", "STRING", "NULLABLE", ()),
        ("availability", "STRING", "REQUIRED", ()),
        ("evidence_state", "STRING", "REQUIRED", ()),
        ("created_at", "TIMESTAMP", "REQUIRED", ()),
    ),
    "membership": (
        ("client_scope_id", "STRING", "REQUIRED", ()),
        ("market_scope", "STRING", "REPEATED", ()),
        ("brand_config_id", "STRING", "REQUIRED", ()),
        ("audience_lens_ids", "STRING", "REPEATED", ()),
        ("theme_id", "STRING", "REQUIRED", ()),
        ("run_id", "STRING", "REQUIRED", ()),
        ("contract_version", "STRING", "REQUIRED", ()),
        ("signal_date", "DATE", "REQUIRED", ()),
        ("market", "STRING", "REQUIRED", ()),
        ("signal_id", "STRING", "REQUIRED", ()),
        ("member_id", "STRING", "REQUIRED", ()),
        ("member_identity", "STRING", "REQUIRED", ()),
        ("candidate_type", "STRING", "REQUIRED", ()),
        ("canonical_value", "STRING", "REQUIRED", ()),
        ("source_families", "STRING", "REPEATED", ()),
        ("vendor_families", "STRING", "REPEATED", ()),
        ("channel_families", "STRING", "REPEATED", ()),
        ("platforms", "STRING", "REPEATED", ()),
        ("row_id", "STRING", "REQUIRED", ()),
        ("qualifies_evidence", "BOOL", "REQUIRED", ()),
        ("created_at", "TIMESTAMP", "REQUIRED", ()),
    ),
    "lineage": (
        ("client_scope_id", "STRING", "REQUIRED", ()),
        ("market_scope", "STRING", "REPEATED", ()),
        ("brand_config_id", "STRING", "REQUIRED", ()),
        ("audience_lens_ids", "STRING", "REPEATED", ()),
        ("theme_id", "STRING", "REQUIRED", ()),
        ("run_id", "STRING", "REQUIRED", ()),
        ("contract_version", "STRING", "REQUIRED", ()),
        ("signal_date", "DATE", "REQUIRED", ()),
        ("market", "STRING", "REQUIRED", ()),
        ("from_signal_id", "STRING", "REQUIRED", ()),
        ("to_signal_id", "STRING", "REQUIRED", ()),
        ("relation", "STRING", "REQUIRED", ()),
        ("overlap_score", "FLOAT64", "REQUIRED", ()),
        ("created_at", "TIMESTAMP", "REQUIRED", ()),
    ),
    "predictions": (
        ("client_scope_id", "STRING", "REQUIRED", ()),
        ("market_scope", "STRING", "REPEATED", ()),
        ("brand_config_id", "STRING", "REQUIRED", ()),
        ("audience_lens_ids", "STRING", "REPEATED", ()),
        ("theme_id", "STRING", "REQUIRED", ()),
        ("run_id", "STRING", "REQUIRED", ()),
        ("contract_version", "STRING", "REQUIRED", ()),
        ("prediction_id", "STRING", "REQUIRED", ()),
        ("signal_id", "STRING", "REQUIRED", ()),
        ("signal_date", "DATE", "REQUIRED", ()),
        ("market", "STRING", "REQUIRED", ()),
        ("discovery_mode", "STRING", "REQUIRED", ()),
        ("source_families", "STRING", "REPEATED", ()),
        ("evidence_state", "STRING", "REQUIRED", ()),
        ("first_seen_at", "TIMESTAMP", "REQUIRED", ()),
        ("predicted_at", "TIMESTAMP", "REQUIRED", ()),
        ("expected_trajectory", "STRING", "REQUIRED", ()),
        ("evaluation_date", "DATE", "REQUIRED", ()),
        (
            "baseline",
            "RECORD",
            "REQUIRED",
            (
                ("velocity", "FLOAT64", "REQUIRED", ()),
                ("breadth", "FLOAT64", "REQUIRED", ()),
                ("evidence_family_count", "INT64", "REQUIRED", ()),
            ),
        ),
        (
            "promotion_target",
            "RECORD",
            "REQUIRED",
            (
                ("velocity", "FLOAT64", "REQUIRED", ()),
                ("breadth", "FLOAT64", "REQUIRED", ()),
                ("evidence_family_count", "INT64", "REQUIRED", ()),
            ),
        ),
        ("invalidation_condition", "STRING", "REQUIRED", ()),
        ("cluster_build_version", "STRING", "REQUIRED", ()),
        ("source_family_map_version", "STRING", "REQUIRED", ()),
        ("rule_version", "STRING", "REQUIRED", ()),
        ("display_eligible", "BOOL", "REQUIRED", ()),
    ),
    "outcomes": (
        ("client_scope_id", "STRING", "REQUIRED", ()),
        ("market_scope", "STRING", "REPEATED", ()),
        ("brand_config_id", "STRING", "REQUIRED", ()),
        ("audience_lens_ids", "STRING", "REPEATED", ()),
        ("theme_id", "STRING", "REQUIRED", ()),
        ("run_id", "STRING", "REQUIRED", ()),
        ("contract_version", "STRING", "REQUIRED", ()),
        ("outcome_id", "STRING", "REQUIRED", ()),
        ("prediction_id", "STRING", "REQUIRED", ()),
        ("signal_id", "STRING", "REQUIRED", ()),
        ("signal_date", "DATE", "REQUIRED", ()),
        ("market", "STRING", "REQUIRED", ()),
        ("discovery_mode", "STRING", "REQUIRED", ()),
        ("source_families", "STRING", "REPEATED", ()),
        ("source_family_map_version", "STRING", "REQUIRED", ()),
        ("evaluation_date", "DATE", "REQUIRED", ()),
        ("evaluated_at", "TIMESTAMP", "REQUIRED", ()),
        ("outcome", "STRING", "REQUIRED", ()),
        ("observed_velocity", "FLOAT64", "NULLABLE", ()),
        ("observed_breadth", "FLOAT64", "NULLABLE", ()),
        ("observed_evidence_family_count", "INT64", "NULLABLE", ()),
        ("human_calibration_label", "STRING", "NULLABLE", ()),
        ("human_reviewed_at", "TIMESTAMP", "NULLABLE", ()),
        ("resolution_reason", "STRING", "REQUIRED", ()),
        ("rule_version", "STRING", "REQUIRED", ()),
    ),
}
FORBIDDEN_DML_TOKEN = re.compile(r"(?i)(?:^|[ \t\r\n])(delete|truncate|update)(?=$|[ \t\r\n])")


def _schema_tree(fields: Iterable[bigquery.SchemaField]) -> tuple[SchemaNode, ...]:
    return tuple(
        (field.name, field.field_type, field.mode, _schema_tree(field.fields)) for field in fields
    )


def _scope() -> dict[str, object]:
    return {
        "client_scope_id": "fixture_scope",
        "market_scope": ["za"],
        "brand_config_id": "fixture_brand",
        "audience_lens_ids": ["lens_a"],
        "theme_id": "fixture_theme",
        "run_id": "run_001",
        "contract_version": "3.0.0",
    }


def _candidate() -> dict[str, object]:
    return {
        **_scope(),
        "signal_id": "sig_" + "a" * 64,
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "label": "fixture signal",
        "label_member_identity": "za|keyword|fixture signal",
        "cluster_signature": "a" * 64,
        "cluster_build_version": "hybrid_graph_v1",
        "model_version": None,
        "discovery_mode": "dynamic",
        "topic_tags": ["culture", "music"],
        "novelty_score": 0.1,
        "velocity_score": 0.2,
        "breadth_score": 0.3,
        "independence_score": 0.4,
        "historical_similarity": None,
        "geo_confidence": 0.5,
        "evidence_state": "ready",
        "created_at": NOW,
    }


def _evidence() -> dict[str, object]:
    return {
        **_scope(),
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "signal_id": "sig_" + "a" * 64,
        "evidence_id": "ev_" + "b" * 64,
        "row_id": "row_001",
        "source_family": "news",
        "vendor_family": "rss",
        "channel_family": "news",
        "platform": "web",
        "url": None,
        "published_at": NOW,
        "claim_role": "identity",
        "direction": "rising",
        "geo_confidence": 0.5,
        "source_label": None,
        "author_label": None,
        "excerpt": None,
        "metric_label": None,
        "availability": "available",
        "evidence_state": "ready",
        "created_at": NOW,
    }


def _membership() -> dict[str, object]:
    return {
        **_scope(),
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "signal_id": "sig_" + "a" * 64,
        "member_id": "mem_" + "c" * 64,
        "member_identity": "za|keyword|fixture",
        "candidate_type": "keyword",
        "canonical_value": "fixture",
        "source_families": ["news", "search"],
        "vendor_families": ["rss", "semrush"],
        "channel_families": ["news", "search"],
        "platforms": ["search", "web"],
        "row_id": "row_001",
        "qualifies_evidence": True,
        "created_at": NOW,
    }


def _lineage() -> dict[str, object]:
    return {
        **_scope(),
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "from_signal_id": "sig_" + "d" * 64,
        "to_signal_id": "sig_" + "a" * 64,
        "relation": "continues",
        "overlap_score": 0.6,
        "created_at": NOW,
    }


def _prediction() -> dict[str, object]:
    return {
        **_scope(),
        "prediction_id": "pred_" + "e" * 64,
        "signal_id": "sig_" + "a" * 64,
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "discovery_mode": "dynamic",
        "source_families": ["news", "search"],
        "evidence_state": "ready",
        "first_seen_at": NOW - timedelta(days=1),
        "predicted_at": NOW,
        "expected_trajectory": "growing",
        "evaluation_date": SIGNAL_DATE + timedelta(days=7),
        "baseline": {"velocity": 0.2, "breadth": 0.3, "evidence_family_count": 2},
        "promotion_target": {"velocity": 0.4, "breadth": 0.5, "evidence_family_count": 3},
        "invalidation_condition": "velocity below baseline",
        "cluster_build_version": "hybrid_graph_v1",
        "source_family_map_version": "families_v1",
        "rule_version": "rules_v1",
        "display_eligible": False,
    }


def _outcome() -> dict[str, object]:
    return {
        **_scope(),
        "outcome_id": "out_" + "f" * 64,
        "prediction_id": "pred_" + "e" * 64,
        "signal_id": "sig_" + "a" * 64,
        "signal_date": SIGNAL_DATE,
        "market": "za",
        "discovery_mode": "dynamic",
        "source_families": ["news", "search"],
        "source_family_map_version": "families_v1",
        "evaluation_date": SIGNAL_DATE + timedelta(days=7),
        "evaluated_at": NOW + timedelta(days=7),
        "outcome": "sustained",
        "observed_velocity": 0.4,
        "observed_breadth": 0.5,
        "observed_evidence_family_count": 3,
        "human_calibration_label": None,
        "human_reviewed_at": None,
        "resolution_reason": "breadth_and_evidence_floor_sustained",
        "rule_version": "outcome_rules_v1",
    }


def _row(table: str) -> dict[str, object]:
    return {
        "candidates": _candidate,
        "evidence": _evidence,
        "membership": _membership,
        "lineage": _lineage,
        "predictions": _prediction,
        "outcomes": _outcome,
    }[table]()


def _only_table_batch(table: str, *rows: dict[str, object]) -> persistence.OpenIntelligenceRowBatch:
    values: dict[str, object] = {
        "candidates": (),
        "evidence": (),
        "membership": (),
        "lineage": (),
        "predictions": (),
        "outcomes": (),
    }
    values[table] = rows
    return _batch(**values)


def _batch(**overrides: object) -> persistence.OpenIntelligenceRowBatch:
    rows = {
        "candidates": (_candidate(),),
        "evidence": (_evidence(),),
        "membership": (_membership(),),
        "lineage": (_lineage(),),
        "predictions": (_prediction(),),
        "outcomes": (_outcome(),),
    }
    rows.update(overrides)
    return persistence.OpenIntelligenceRowBatch(**rows)


def _rules(**overrides: object) -> SimpleNamespace:
    values = {
        "status": "replay_certified",
        "rule_version": "rules_v1",
        "replay_receipt_id": "receipt_001",
        "approved_at": NOW,
        "approved_by": "Albert",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _target(**overrides: object) -> SimpleNamespace:
    values = {
        "project": PROJECT,
        "dataset": DATASET,
        "location": LOCATION,
        "writer_identity": WRITER_IDENTITY,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _ForbiddenClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def __getattr__(self, name: str) -> object:
        if name == "__name__":
            return "ForbiddenClient"
        self.calls.append((name,))
        raise AssertionError(f"dry run must not call client.{name}")


def _dry_run(
    batch: persistence.OpenIntelligenceRowBatch | None = None,
    *,
    target: object = _DEFAULT_TARGET,
):
    return persistence.persist_open_intelligence_rows(
        project=PROJECT,
        dataset=DATASET,
        client=_target() if target is _DEFAULT_TARGET else target,
        batch=batch or _batch(),
        rule_bundle=_rules(),
        dry_run=True,
    )


def test_constants_bind_only_the_approved_staging_target_and_six_tables() -> None:
    assert persistence.TARGET_PROJECT == PROJECT
    assert persistence.TARGET_DATASET == DATASET
    assert persistence.TARGET_LOCATION == LOCATION
    assert persistence.TARGET_WRITER_IDENTITY == WRITER_IDENTITY
    assert tuple(persistence.TABLE_BINDINGS) == (
        "candidates",
        "evidence",
        "membership",
        "lineage",
        "predictions",
        "outcomes",
    )
    assert tuple(persistence.TABLE_BINDINGS.values()) == (
        "signal_candidates_v2",
        "signal_evidence_v2",
        "signal_membership_v2",
        "signal_lineage_v2",
        "signal_predictions_v2",
        "signal_outcomes_v2",
    )


def test_schema_fields_and_natural_keys_match_approved_rows_exactly() -> None:
    assert tuple(persistence.ROW_FIELDS) == tuple(persistence.TABLE_BINDINGS)
    assert persistence.NATURAL_KEYS == EXPECTED_NATURAL_KEYS
    assert persistence.ROW_FIELDS == EXPECTED_ROW_FIELDS


def test_batch_deep_copies_to_immutable_canonical_rows() -> None:
    candidate = _candidate()
    prediction = _prediction()
    batch = _batch(candidates=(candidate,), predictions=(prediction,))

    candidate["topic_tags"].append("changed")
    prediction["baseline"]["velocity"] = 0.9

    assert batch.candidates[0]["topic_tags"] == ("culture", "music")
    assert batch.predictions[0]["baseline"]["velocity"] == 0.2
    with pytest.raises(TypeError):
        batch.candidates[0]["label"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        batch.predictions[0]["baseline"]["velocity"] = 0.9  # type: ignore[index]


def test_canonical_json_is_typed_compact_and_preserves_null_distinct_from_missing() -> None:
    row = _candidate()
    row["model_version"] = None
    canonical = persistence.canonical_typed_json("candidates", row)

    assert canonical == persistence.canonical_typed_json("candidates", dict(row))
    assert '"model_version":{"type":"null","value":null}' in canonical
    row.pop("model_version")
    with pytest.raises(persistence.BatchInvalid, match="fields"):
        persistence.canonical_typed_json("candidates", row)


def test_canonical_json_normalizes_dates_and_timestamps_to_iso_utc() -> None:
    candidate = _candidate()
    candidate["created_at"] = datetime(2026, 8, 26, 8, 45, tzinfo=UTC)
    rendered = persistence.canonical_typed_json("candidates", candidate)
    assert '"signal_date":{"type":"date","value":"2026-08-26"}' in rendered
    assert '"created_at":{"type":"timestamp","value":"2026-08-26T08:45:00Z"}' in rendered


@pytest.mark.parametrize(
    ("table", "builder", "field"),
    [
        ("candidates", _candidate, "label"),
        ("membership", _membership, "canonical_value"),
        ("predictions", _prediction, "invalidation_condition"),
    ],
)
def test_persistence_refuses_non_nfc_text_before_canonicalization(table, builder, field):
    row = builder()
    row[field] = "Cafe\u0301"
    with pytest.raises(persistence.BatchInvalid, match="noncanonical_unicode"):
        _batch(**{table: (row,)})


def test_input_order_does_not_change_canonical_batch_plan_or_digests() -> None:
    first = _candidate()
    second = _candidate()
    second["signal_id"] = "sig_" + "f" * 64
    forward = _batch(candidates=(first, second))
    reverse = _batch(candidates=(second, first))

    forward_result = _dry_run(forward)
    reverse_result = _dry_run(reverse)

    assert forward.candidates == reverse.candidates
    assert forward_result.statement_digests == reverse_result.statement_digests
    assert persistence.plan_statements(PROJECT, DATASET, forward) == persistence.plan_statements(
        PROJECT, DATASET, reverse
    )


def test_repeated_values_and_evidence_order_canonicalize_to_one_plan_and_digest() -> None:
    second_evidence = _evidence()
    second_evidence["evidence_id"] = "ev_" + "f" * 64
    second_evidence["row_id"] = "row_002"
    ordered = _batch(evidence=(_evidence(), second_evidence))

    candidate = _candidate()
    candidate["market_scope"] = ["za", "ng"]
    candidate["audience_lens_ids"] = ["lens_a", "lens_b"]
    candidate["topic_tags"] = ["culture", "music"]
    membership = _membership()
    membership["source_families"] = ["news", "search"]
    membership["platforms"] = ["search", "web"]
    prediction = _prediction()
    prediction["source_families"] = ["news", "search"]
    scrambled_candidate = dict(candidate)
    scrambled_candidate["market_scope"] = ["ng", "za"]
    scrambled_candidate["audience_lens_ids"] = ["lens_b", "lens_a"]
    scrambled_candidate["topic_tags"] = ["music", "culture"]
    scrambled_membership = dict(membership)
    scrambled_membership["source_families"] = ["search", "news"]
    scrambled_membership["platforms"] = ["web", "search"]
    scrambled_prediction = dict(prediction)
    scrambled_prediction["source_families"] = ["search", "news"]
    scrambled = _batch(
        candidates=(scrambled_candidate,),
        evidence=(second_evidence, _evidence()),
        membership=(scrambled_membership,),
        predictions=(scrambled_prediction,),
    )
    ordered_with_repeated_values = _batch(
        candidates=(candidate,),
        evidence=(_evidence(), second_evidence),
        membership=(membership,),
        predictions=(prediction,),
    )

    assert ordered != ordered_with_repeated_values
    assert ordered_with_repeated_values == scrambled
    assert (
        _dry_run(ordered_with_repeated_values).statement_digests
        == _dry_run(scrambled).statement_digests
    )


@pytest.mark.parametrize(
    ("table", "field", "values"),
    [
        ("candidates", "market_scope", ["za", "za"]),
        ("candidates", "audience_lens_ids", ["lens_a", "lens_a"]),
        ("candidates", "topic_tags", ["culture", "culture"]),
        ("membership", "source_families", ["news", "news"]),
        ("membership", "platforms", ["web", "web"]),
        ("predictions", "source_families", ["news", "news"]),
    ],
)
def test_repeated_values_reject_duplicates(table: str, field: str, values: list[str]) -> None:
    builder = {
        "candidates": _candidate,
        "membership": _membership,
        "predictions": _prediction,
        "outcomes": _outcome,
    }[table]
    row = builder()
    row[field] = values
    with pytest.raises(persistence.BatchInvalid, match="duplicate"):
        _batch(**{table: (row,)})


def test_statement_plans_are_immutable_and_digest_complete_canonical_payload() -> None:
    plans = persistence.plan_statements(PROJECT, DATASET, _batch())
    candidate_plan = plans["candidates"]

    assert candidate_plan.table == "signal_candidates_v2"
    assert tuple(candidate_plan.parameters) == (
        "canonical_rows",
        "natural_keys",
        "validated_row_count",
    )
    assert candidate_plan.parameters["canonical_rows"] == _batch().candidates
    assert candidate_plan.parameters["natural_keys"] == (
        ("fixture_scope", SIGNAL_DATE, "za", "sig_" + "a" * 64, "run_001"),
    )
    assert candidate_plan.parameters["validated_row_count"] == 1
    with pytest.raises(TypeError):
        candidate_plan.parameters["validated_row_count"] = 2  # type: ignore[index]

    changed = _candidate()
    changed["label"] = "changed signal"
    changed_result = _dry_run(_batch(candidates=(changed,)))
    baseline_result = _dry_run()
    assert (
        changed_result.statement_digests["candidates"]
        != baseline_result.statement_digests["candidates"]
    )
    for table in ("evidence", "membership", "lineage", "predictions"):
        assert changed_result.statement_digests[table] == baseline_result.statement_digests[table]


@pytest.mark.parametrize(
    ("project", "dataset"),
    [
        ("other-project", DATASET),
        (PROJECT, "trends_v2"),
        (PROJECT, "trends_v2_staging_qa"),
        (PROJECT, "trends_v2_staging_prod"),
        (PROJECT, "TRENDs_v2_staging"),
    ],
)
def test_every_nonexact_target_is_rejected_before_any_client_call(
    project: str, dataset: str
) -> None:
    client = _target()
    with pytest.raises(persistence.TargetInvalid):
        persistence.persist_open_intelligence_rows(
            project=project,
            dataset=dataset,
            client=client,
            batch=_batch(),
            rule_bundle=_rules(),
            dry_run=True,
        )


@pytest.mark.parametrize(
    "target",
    [
        None,
        _target(project="other-project"),
        _target(dataset="trends_v2_staging_qa"),
        _target(dataset="trends_v2"),
        _target(location="EU"),
        _target(location="us"),
        _target(writer_identity="default"),
        _target(
            writer_identity="trends-engine-production@ogilvy-trends-v2.iam.gserviceaccount.com"
        ),
        _target(
            writer_identity="trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com.invalid"
        ),
        _ForbiddenClient(),
    ],
)
def test_dry_run_rejects_missing_or_nonexact_target_descriptor_without_calls(
    target: object,
) -> None:
    with pytest.raises(persistence.TargetInvalid):
        _dry_run(target=target)
    if isinstance(target, _ForbiddenClient):
        assert target.calls == []


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_dry_run_requires_an_actual_boolean(value: object) -> None:
    with pytest.raises(persistence.BatchInvalid, match="dry_run"):
        persistence.persist_open_intelligence_rows(
            project=PROJECT,
            dataset=DATASET,
            client=_target(),
            batch=_batch(),
            rule_bundle=_rules(),
            dry_run=value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "rule_bundle",
    [
        _rules(status="provisional"),
        _rules(rule_version=""),
        _rules(replay_receipt_id=""),
        _rules(approved_at=None),
        _rules(approved_by=""),
    ],
)
def test_noncertified_or_malformed_rule_bundle_is_rejected_before_client_use(
    rule_bundle: SimpleNamespace,
) -> None:
    with pytest.raises(persistence.RuleInvalid):
        persistence.persist_open_intelligence_rows(
            project=PROJECT,
            dataset=DATASET,
            client=_target(),
            batch=_batch(),
            rule_bundle=rule_bundle,
            dry_run=True,
        )


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("candidates", "signal_date", "2026-08-26"),
        ("evidence", "published_at", datetime(2026, 8, 26, 6, 45)),
        ("lineage", "overlap_score", float("nan")),
        ("lineage", "overlap_score", float("inf")),
        ("membership", "qualifies_evidence", 1),
        ("candidates", "market_scope", {"za"}),
        ("predictions", "baseline", {"velocity": 0.2}),
    ],
)
def test_malformed_typed_values_are_rejected(table: str, field: str, value: object) -> None:
    row = {
        "candidates": _candidate,
        "evidence": _evidence,
        "membership": _membership,
        "lineage": _lineage,
        "predictions": _prediction,
    }[table]()
    row[field] = value
    with pytest.raises(persistence.BatchInvalid):
        _batch(**{table: (row,)})


@pytest.mark.parametrize("table", tuple(persistence.TABLE_BINDINGS))
def test_unknown_and_missing_fields_are_rejected(table: str) -> None:
    builder = {
        "candidates": _candidate,
        "evidence": _evidence,
        "membership": _membership,
        "lineage": _lineage,
        "predictions": _prediction,
        "outcomes": _outcome,
    }[table]
    unknown = builder()
    unknown["unexpected"] = "value"
    with pytest.raises(persistence.BatchInvalid, match="fields"):
        _batch(**{table: (unknown,)})

    missing = builder()
    missing.pop(next(iter(missing)))
    with pytest.raises(persistence.BatchInvalid, match="fields"):
        _batch(**{table: (missing,)})


@pytest.mark.parametrize("table", tuple(persistence.TABLE_BINDINGS))
def test_duplicate_natural_keys_fail_even_when_content_is_equal(table: str) -> None:
    builder = {
        "candidates": _candidate,
        "evidence": _evidence,
        "membership": _membership,
        "lineage": _lineage,
        "predictions": _prediction,
        "outcomes": _outcome,
    }[table]
    with pytest.raises(persistence.BatchInvalid, match="duplicate"):
        _batch(**{table: (builder(), builder())})


def test_dry_run_is_local_only_and_reports_only_validated_counts() -> None:
    result = persistence.persist_open_intelligence_rows(
        project=PROJECT,
        dataset=DATASET,
        client=_target(),
        batch=_batch(),
        rule_bundle=_rules(),
        dry_run=True,
    )

    assert result.project == PROJECT
    assert result.dataset == DATASET
    assert result.dry_run is True
    assert result.validated_counts == dict.fromkeys(persistence.TABLE_BINDINGS, 1)
    assert result.inserted_counts == dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    assert result.unchanged_counts == dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    assert result.conflict_counts == dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    assert tuple(result.statement_digests) == tuple(persistence.TABLE_BINDINGS)
    assert result.cleanup_state == "not_started"


def test_plans_are_in_exact_table_order_and_allow_only_approved_staging_sql() -> None:
    statements = persistence.plan_statements(PROJECT, DATASET, _batch())
    assert tuple(statements) == tuple(persistence.TABLE_BINDINGS)
    text = "\n".join(plan.sql.lower() for plan in statements.values())
    for table_name in persistence.TABLE_BINDINGS.values():
        assert f"`{PROJECT}.{DATASET}.{table_name}`" in text
    assert FORBIDDEN_DML_TOKEN.search("DELETE FROM table") is not None
    assert FORBIDDEN_DML_TOKEN.search("\nUPDATE\ttable") is not None
    assert FORBIDDEN_DML_TOKEN.search("\ttruncate\n") is not None
    for forbidden in ("production", "_qa", "lookalike"):
        assert forbidden not in text
    assert FORBIDDEN_DML_TOKEN.search(text) is None


class _FakeBackendError(Exception):
    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


_UNSET_RETRY = object()


def _fake_json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_fake_json_value(item) for item in value]
    if isinstance(value, dict):
        return {field: _fake_json_value(item) for field, item in value.items()}
    return value


def _fake_row(table: str, row: dict[str, object]) -> dict[str, object]:
    return {field: _fake_json_value(row[field]) for field in EXPECTED_ROW_FIELDS[table]}


class _FakeLoadJob:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        errors: tuple[dict[str, str], ...] = (),
        output_rows: int | None = None,
    ) -> None:
        self._error = error
        self.errors = errors
        self.output_rows = output_rows
        self.result_calls: list[tuple[object, object]] = []

    def result(
        self,
        retry: object = _UNSET_RETRY,
        timeout: object = None,
    ) -> _FakeLoadJob:
        self.result_calls.append((retry, timeout))
        if self._error is not None:
            raise self._error
        return self


class _FakeQueryJob:
    def __init__(
        self,
        *,
        rows: tuple[dict[str, int], ...] = (),
        error: Exception | None = None,
    ) -> None:
        self._rows = rows
        self._error = error
        self.errors: tuple[dict[str, str], ...] = ()
        self.result_calls: list[dict[str, object]] = []

    def result(
        self,
        page_size: int | None = None,
        max_results: int | None = None,
        retry: object = _UNSET_RETRY,
        timeout: object = _UNSET_RETRY,
        start_index: int | None = None,
        job_retry: object = _UNSET_RETRY,
    ) -> Iterable[dict[str, int]]:
        self.result_calls.append(
            {
                "page_size": page_size,
                "max_results": max_results,
                "retry": retry,
                "timeout": timeout,
                "start_index": start_index,
                "job_retry": job_retry,
            }
        )
        if self._error is not None:
            raise self._error
        return self._rows


class _FakeCredentials:
    def __init__(
        self,
        service_account_email: str = WRITER_IDENTITY,
        quota_project_id: str | None = None,
    ) -> None:
        self.service_account_email = service_account_email
        self.quota_project_id = quota_project_id


class _FakeBigQueryClient:
    def __init__(
        self,
        *,
        project: str = PROJECT,
        location: str = LOCATION,
        credentials: _FakeCredentials | None = None,
        load_error: Exception | None = None,
        load_errors: tuple[dict[str, str], ...] = (),
        partial_load: bool = False,
        query_errors: dict[str, Exception] | None = None,
        delete_error: Exception | None = None,
        target_rows: dict[str, tuple[dict[str, object], ...]] | None = None,
    ) -> None:
        self.project = project
        self.dataset = DATASET
        self.location = location
        self.writer_identity = WRITER_IDENTITY
        self._credentials = credentials or _FakeCredentials()
        self.load_error = load_error
        self.load_errors = load_errors
        self.partial_load = partial_load
        self.query_errors = query_errors or {}
        self.delete_error = delete_error
        supplied_targets = target_rows or {}
        self.target_rows = {
            table: [_fake_row(table, row) for row in supplied_targets.get(table, ())]
            for table in EXPECTED_ROW_FIELDS
        }
        self.created: list[bigquery.Table] = []
        self.loads: list[tuple[list[dict[str, object]], bigquery.Table]] = []
        self.queries: list[tuple[str, str]] = []
        self.query_calls: list[dict[str, object]] = []
        self.query_jobs: list[_FakeQueryJob] = []
        self.load_jobs: list[_FakeLoadJob] = []
        self.staged_rows: dict[str, list[dict[str, object]]] = {}
        self.deleted: list[bigquery.Table] = []
        self.committed: list[str] = []

    def create_table(self, table: bigquery.Table, *, exists_ok: bool = False) -> bigquery.Table:
        assert exists_ok is False
        self.created.append(table)
        return table

    def load_table_from_json(
        self,
        rows: list[dict[str, object]],
        destination: bigquery.Table,
        *,
        location: str,
        job_config: bigquery.LoadJobConfig,
    ) -> _FakeLoadJob:
        assert location == LOCATION
        assert isinstance(job_config, bigquery.LoadJobConfig)
        self.loads.append((rows, destination))
        self.staged_rows[destination.table_id] = [dict(row) for row in rows]
        job = _FakeLoadJob(
            error=self.load_error,
            errors=self.load_errors,
            output_rows=len(rows) - int(self.partial_load),
        )
        self.load_jobs.append(job)
        return job

    @staticmethod
    def _ordered_pairs(pairs: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
        ordered: list[tuple[str, str]] = []
        for pair in pairs:
            if pair not in ordered:
                ordered.append(pair)
        return tuple(ordered)

    @staticmethod
    def _matches(
        target: dict[str, object],
        staged: dict[str, object],
        pairs: tuple[tuple[str, str], ...],
    ) -> bool:
        return all(
            target[target_field] == staged[staged_field] for target_field, staged_field in pairs
        )

    @staticmethod
    def _canonical_projection(
        row: dict[str, object], pairs: tuple[tuple[str, str], ...]
    ) -> dict[str, object]:
        return {output_field: row[source_field] for source_field, output_field in pairs}

    def query(
        self,
        sql: str,
        *,
        job_config: bigquery.QueryJobConfig,
        location: str,
        retry: object = _UNSET_RETRY,
        job_retry: object = _UNSET_RETRY,
    ) -> _FakeQueryJob:
        assert isinstance(job_config, bigquery.QueryJobConfig)
        self.queries.append((sql, location))
        self.query_calls.append(
            {
                "sql": sql,
                "location": location,
                "retry": retry,
                "job_retry": job_retry,
            }
        )
        table = next(name for name, binding in EXPECTED_TABLE_BINDINGS.items() if binding in sql)
        error = self.query_errors.get(table)
        if error is not None:
            job = _FakeQueryJob(error=error)
            self.query_jobs.append(job)
            return job

        destination = next(
            destination
            for _, destination in reversed(self.loads)
            if destination.labels["open_intelligence_table"] == table
        )
        staged_rows = self.staged_rows[destination.table_id]
        targets = self.target_rows[table]
        join_pairs = self._ordered_pairs(
            re.findall(
                r"target\.\`([^`]+)\` (?:IS NOT DISTINCT FROM|=) staged\.\`([^`]+)\`",
                sql,
            )
        )
        staged_projection = self._ordered_pairs(
            re.findall(r"staged\.\`([^`]+)\` AS \`([^`]+)\`", sql)
        )
        target_projection = self._ordered_pairs(
            re.findall(r"target\.\`([^`]+)\` AS \`([^`]+)\`", sql)
        )
        comparison_operators = re.findall(
            r"WHERE TO_JSON_STRING\(STRUCT\(.*?\)\)\s*(!=|=)\s*"
            r"TO_JSON_STRING\(STRUCT\(",
            sql,
            flags=re.DOTALL,
        )
        compare_conflicts = "!=" in comparison_operators
        compare_unchanged = "=" in comparison_operators
        conflict_count = 0
        unchanged_count = 0
        for staged in staged_rows:
            for target in targets:
                if not self._matches(target, staged, join_pairs):
                    continue
                staged_content = self._canonical_projection(staged, staged_projection)
                target_content = self._canonical_projection(target, target_projection)
                if compare_conflicts and staged_content != target_content:
                    conflict_count += 1
                if compare_unchanged and staged_content == target_content:
                    unchanged_count += 1

        has_assert = "ASSERT conflict_count = 0" in sql or "ASSERT NOT EXISTS (" in sql
        if conflict_count and has_assert:
            job = _FakeQueryJob(
                rows=(
                    {
                        "inserted_count": 0,
                        "unchanged_count": 0,
                        "conflict_count": conflict_count,
                        "status": 1,
                    },
                )
            )
            self.query_jobs.append(job)
            return job

        target_identifier = f"`{PROJECT}.{DATASET}.{EXPECTED_TABLE_BINDINGS[table]}`"
        if table == "candidates":
            write_enabled = (
                f"MERGE {target_identifier}" in sql and "WHEN NOT MATCHED THEN INSERT" in sql
            )
        else:
            write_enabled = f"INSERT INTO {target_identifier}" in sql and "WHERE NOT EXISTS" in sql
        inserted_count = 0
        if write_enabled:
            for staged in staged_rows:
                if any(self._matches(target, staged, join_pairs) for target in targets):
                    continue
                targets.append(dict(staged))
                inserted_count += 1
        self.committed.append(table)
        job = _FakeQueryJob(
            rows=(
                {
                    "inserted_count": inserted_count,
                    "unchanged_count": unchanged_count,
                    "conflict_count": conflict_count,
                    "status": 0,
                },
            )
        )
        self.query_jobs.append(job)
        return job

    def delete_table(self, table: bigquery.Table, *, not_found_ok: bool) -> None:
        assert not_found_ok is True
        self.deleted.append(table)
        if self.delete_error is not None:
            raise self.delete_error


class _UncertainCreateClient(_FakeBigQueryClient):
    def __init__(self) -> None:
        super().__init__()
        self.delete_attempts: list[tuple[bigquery.Table, bool]] = []

    def create_table(self, table: bigquery.Table, *, exists_ok: bool = False) -> bigquery.Table:
        assert exists_ok is False
        self.created.append(table)
        raise _FakeBackendError("connection lost after temporary table creation")

    def delete_table(self, table: bigquery.Table, *, not_found_ok: bool) -> None:
        self.delete_attempts.append((table, not_found_ok))
        super().delete_table(table, not_found_ok=not_found_ok)


def _real_write(
    client: _FakeBigQueryClient,
    batch: persistence.OpenIntelligenceRowBatch | None = None,
):
    return persistence.persist_open_intelligence_rows(
        project=PROJECT,
        dataset=DATASET,
        client=client,
        batch=batch or _batch(),
        rule_bundle=_rules(),
        dry_run=False,
    )


def test_real_write_loads_temporary_rows_commits_in_order_and_cleans_up() -> None:
    client = _FakeBigQueryClient()

    result = _real_write(client)

    assert result.dry_run is False
    assert result.inserted_counts == dict.fromkeys(persistence.TABLE_BINDINGS, 1)
    assert result.unchanged_counts == dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    assert result.conflict_counts == dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    assert result.cleanup_state == "complete"
    assert client.committed == list(persistence.TABLE_BINDINGS)
    assert len(client.created) == len(persistence.TABLE_BINDINGS)
    assert len(client.loads) == len(persistence.TABLE_BINDINGS)
    assert client.deleted == list(reversed(client.created))
    for table, temporary in zip(persistence.TABLE_BINDINGS, client.created, strict=True):
        assert temporary.project == PROJECT
        assert temporary.dataset_id == DATASET
        assert temporary.table_id.endswith(persistence.TABLE_BINDINGS[table])
        assert temporary.expires is not None
        assert tuple(field.name for field in temporary.schema) == persistence.ROW_FIELDS[table]
    for table, (rows, destination) in zip(persistence.TABLE_BINDINGS, client.loads, strict=True):
        assert rows == persistence._load_rows(getattr(_batch(), table))
        assert destination is client.created[list(persistence.TABLE_BINDINGS).index(table)]
    sql = "\n".join(query for query, _ in client.queries)
    assert all(location == LOCATION for _, location in client.queries)
    assert "BEGIN TRANSACTION" in sql
    assert "COMMIT TRANSACTION" in sql
    assert "TO_JSON_STRING(STRUCT(" in sql
    assert any("candidate_run_conflict" in query for query, _ in client.queries)
    assert any("immutable_conflict" in query for query, _ in client.queries)
    assert _candidate()["label"] not in sql
    assert FORBIDDEN_DML_TOKEN.search(sql) is None


def test_non_candidate_antijoin_uses_required_field_equality() -> None:
    client = _FakeBigQueryClient()

    _real_write(client)

    queries = dict(zip(persistence.TABLE_BINDINGS, (sql for sql, _ in client.queries), strict=True))
    for table in tuple(persistence.TABLE_BINDINGS)[1:]:
        sql = queries[table]
        for field in persistence.NATURAL_KEYS[table]:
            assert field not in persistence._OPTIONAL_FIELDS
            assert f"target.`{field}` = staged.`{field}`" in sql
        assert "IS NOT DISTINCT FROM" not in sql


def test_temporary_table_ids_are_attributed_to_their_statement_digest() -> None:
    caller_text = "caller_supplied_temporary_identity"
    first_batch = _batch()
    first_client = _FakeBigQueryClient()
    first_result = _real_write(first_client, first_batch)

    rerun_client = _FakeBigQueryClient()
    rerun_result = _real_write(rerun_client, first_batch)

    changed_candidate = _candidate()
    changed_candidate["label"] = caller_text
    changed_client = _FakeBigQueryClient()
    changed_result = _real_write(changed_client, _batch(candidates=(changed_candidate,)))

    for table, first, rerun, changed in zip(
        persistence.TABLE_BINDINGS,
        first_client.created,
        rerun_client.created,
        changed_client.created,
        strict=True,
    ):
        first_prefix = f"_oi_{first_result.statement_digests[table][:12]}_"
        rerun_prefix = f"_oi_{rerun_result.statement_digests[table][:12]}_"
        changed_prefix = f"_oi_{changed_result.statement_digests[table][:12]}_"

        assert first.table_id.startswith(first_prefix)
        assert rerun.table_id.startswith(rerun_prefix)
        assert changed.table_id.startswith(changed_prefix)
        assert first_prefix == rerun_prefix
        assert first.table_id != rerun.table_id
        assert caller_text not in changed.table_id

        if table == "candidates":
            assert changed_prefix != first_prefix
            assert len(changed_result.statement_digests[table][:12]) == 12
            assert set(changed_result.statement_digests[table][:12]) <= set("0123456789abcdef")
            assert changed.table_id.endswith(persistence.TABLE_BINDINGS[table])

        for temporary, prefix in (
            (first, first_prefix),
            (rerun, rerun_prefix),
            (changed, changed_prefix),
        ):
            generated_uuid, separator, suffix = temporary.table_id.removeprefix(prefix).partition(
                "_"
            )
            assert len(generated_uuid) == 32
            assert set(generated_uuid) <= set("0123456789abcdef")
            assert separator == "_"
            assert suffix == persistence.TABLE_BINDINGS[table]

    assert (
        changed_result.statement_digests["candidates"]
        != first_result.statement_digests["candidates"]
    )
    for table in ("evidence", "membership", "lineage", "predictions"):
        assert changed_result.statement_digests[table] == first_result.statement_digests[table]


def test_uncertain_create_cleans_the_digest_attributed_temporary_table() -> None:
    client = _UncertainCreateClient()

    with pytest.raises(persistence.PersistenceError) as raised:
        _real_write(client, _only_table_batch("candidates", _candidate()))

    temporary = client.created[0]
    expected_prefix = f"_oi_{raised.value.result.statement_digests['candidates'][:12]}_"
    assert raised.value.__cause__ is not None
    assert raised.value.failed_table == "candidates"
    assert raised.value.result.cleanup_state == "complete"
    assert temporary.table_id.startswith(expected_prefix)
    assert temporary.table_id.endswith(persistence.TABLE_BINDINGS["candidates"])
    assert client.deleted == [temporary]
    assert client.delete_attempts == [(temporary, True)]


@pytest.mark.parametrize(
    "table",
    ["candidates", "evidence", "membership", "lineage", "predictions"],
)
def test_stateful_fake_persists_absent_rows_and_rerun_is_unchanged(table: str) -> None:
    client = _FakeBigQueryClient(target_rows={})
    row = _row(table)
    batch = _only_table_batch(table, row)

    first = _real_write(client, batch)
    second = _real_write(client, batch)

    assert first.inserted_counts[table] == 1
    assert first.unchanged_counts[table] == 0
    assert second.inserted_counts[table] == 0
    assert second.unchanged_counts[table] == 1
    assert len(client.target_rows[table]) == 1


def test_stateful_fake_derives_mixed_inserted_and_unchanged_counts() -> None:
    existing = _candidate()
    added = _candidate()
    added["signal_id"] = "sig_" + "f" * 64
    client = _FakeBigQueryClient(target_rows={"candidates": (existing,)})

    result = _real_write(client, _only_table_batch("candidates", existing, added))

    assert result.inserted_counts["candidates"] == 1
    assert result.unchanged_counts["candidates"] == 1
    assert len(client.target_rows["candidates"]) == 2


def test_stateful_fake_uses_the_complete_natural_key() -> None:
    staged = _candidate()
    other_run = _candidate()
    other_run["run_id"] = "run_002"
    other_run["label"] = "other run content"
    client = _FakeBigQueryClient(target_rows={"candidates": (other_run,)})

    result = _real_write(client, _only_table_batch("candidates", staged))

    assert result.inserted_counts["candidates"] == 1
    assert result.conflict_counts["candidates"] == 0
    assert len(client.target_rows["candidates"]) == 2


def test_stateful_fake_derives_multiple_conflicts_from_canonical_content() -> None:
    first = _candidate()
    second = _candidate()
    second["signal_id"] = "sig_" + "f" * 64
    first_conflict = dict(first)
    first_conflict["label"] = "changed first"
    second_conflict = dict(second)
    second_conflict["label"] = "changed second"
    client = _FakeBigQueryClient(target_rows={"candidates": (first_conflict, second_conflict)})
    before = tuple(dict(row) for row in client.target_rows["candidates"])

    with pytest.raises(persistence.CandidateRunConflict) as raised:
        _real_write(client, _only_table_batch("candidates", first, second))

    assert raised.value.result.conflict_counts["candidates"] == 2
    assert client.committed == []
    assert tuple(client.target_rows["candidates"]) == before


def test_stateful_fake_stops_after_conflict_before_later_tables() -> None:
    conflicting = _candidate()
    conflicting["label"] = "changed"
    client = _FakeBigQueryClient(target_rows={"candidates": (conflicting,)})

    with pytest.raises(persistence.CandidateRunConflict):
        _real_write(client)

    assert [table.labels["open_intelligence_table"] for table in client.created] == ["candidates"]
    assert len(client.queries) == 1
    assert client.committed == []
    assert all(
        client.target_rows[table] == [] for table in EXPECTED_ROW_FIELDS if table != "candidates"
    )


@pytest.mark.parametrize(
    "client",
    [
        _FakeBigQueryClient(project="other-project"),
        _FakeBigQueryClient(location="EU"),
        _FakeBigQueryClient(
            credentials=_FakeCredentials(service_account_email="other@example.com")
        ),
        _FakeBigQueryClient(credentials=_FakeCredentials(quota_project_id="foreign-project")),
    ],
)
def test_real_write_rejects_nonexact_client_or_credentials_before_side_effects(
    client: _FakeBigQueryClient,
) -> None:
    with pytest.raises(persistence.TargetInvalid):
        _real_write(client)
    assert client.created == []
    assert client.loads == []
    assert client.queries == []


def test_real_client_refreshes_default_metadata_identity_before_validation(monkeypatch) -> None:
    class MetadataCredentials:
        service_account_email = "default"
        quota_project_id = None

        def refresh(self, request: object) -> None:
            assert request is sentinel_request
            self.service_account_email = WRITER_IDENTITY

    sentinel_request = object()
    credentials = MetadataCredentials()
    client = _FakeBigQueryClient(credentials=credentials)
    monkeypatch.setattr(persistence, "ComputeCredentials", MetadataCredentials)
    monkeypatch.setattr(persistence, "AuthRequest", lambda: sentinel_request)

    assert persistence.validate_real_client(client, PROJECT) is client
    assert credentials.service_account_email == WRITER_IDENTITY


@pytest.mark.parametrize("refresh_result", ["other@example.com", RefreshError("metadata down")])
def test_real_client_rejects_unproven_metadata_identity(monkeypatch, refresh_result) -> None:
    class MetadataCredentials:
        service_account_email = "default"
        quota_project_id = None

        def refresh(self, request: object) -> None:
            if isinstance(refresh_result, Exception):
                raise refresh_result
            self.service_account_email = refresh_result

    client = _FakeBigQueryClient(credentials=MetadataCredentials())
    monkeypatch.setattr(persistence, "ComputeCredentials", MetadataCredentials)
    monkeypatch.setattr(persistence, "AuthRequest", object)

    with pytest.raises(persistence.TargetInvalid, match="staging writer"):
        persistence.validate_real_client(client, PROJECT)


def test_load_error_cleans_the_created_temp_table_and_preserves_cause() -> None:
    backend_error = _FakeBackendError("bad row")
    client = _FakeBigQueryClient(load_error=backend_error)

    with pytest.raises(persistence.PersistenceError) as raised:
        _real_write(client)

    assert raised.value.__cause__ is backend_error
    assert raised.value.failed_table == "candidates"
    assert raised.value.result.cleanup_state == "complete"
    assert client.committed == []
    assert client.deleted == [client.created[0]]


@pytest.mark.parametrize(
    "client",
    [
        _FakeBigQueryClient(load_errors=({"message": "bad row"},)),
        _FakeBigQueryClient(partial_load=True),
    ],
)
def test_load_row_errors_and_partial_load_stop_before_the_transaction(
    client: _FakeBigQueryClient,
) -> None:
    with pytest.raises(persistence.PersistenceError) as raised:
        _real_write(client)

    assert raised.value.failed_table == "candidates"
    assert raised.value.result.cleanup_state == "complete"
    assert client.queries == []
    assert client.deleted == [client.created[0]]


@pytest.mark.parametrize(
    ("table", "content_field", "changed_value", "expected_type"),
    [
        ("candidates", "label", "changed", persistence.CandidateRunConflict),
        ("evidence", "availability", "unavailable", persistence.ImmutableConflict),
        ("membership", "canonical_value", "changed", persistence.ImmutableConflict),
        ("lineage", "overlap_score", 0.9, persistence.ImmutableConflict),
        ("predictions", "expected_trajectory", "fading", persistence.ImmutableConflict),
        ("outcomes", "outcome", "noise", persistence.ImmutableConflict),
    ],
)
def test_conflicts_stop_later_tables_return_partial_result_and_clean_up(
    table: str,
    content_field: str,
    changed_value: object,
    expected_type: type[persistence.PersistenceError],
) -> None:
    staged = _row(table)
    conflicting = dict(staged)
    conflicting[content_field] = changed_value
    client = _FakeBigQueryClient(target_rows={table: (conflicting,)})

    with pytest.raises(expected_type) as raised:
        _real_write(client, _only_table_batch(table, staged))

    result = raised.value.result
    assert raised.value.failed_table == table
    assert result.conflict_counts[table] == 1
    assert result.cleanup_state == "complete"
    assert client.committed == []
    assert len(client.deleted) == 1


@pytest.fixture
def aborted_reason_error() -> _FakeBackendError:
    return _FakeBackendError("backend rejected write", reason="aborted")


@pytest.fixture
def concurrent_message_error() -> _FakeBackendError:
    return _FakeBackendError("concurrent update")


@pytest.fixture
def serialization_message_error() -> _FakeBackendError:
    return _FakeBackendError("serialization failure")


def _assert_concurrency_is_not_retried(error: _FakeBackendError) -> None:
    client = _FakeBigQueryClient(query_errors={"candidates": error})

    with pytest.raises(persistence.ConcurrentWriteConflict) as raised:
        _real_write(client)

    assert raised.value.failed_table == "candidates"
    assert len(client.queries) == 1
    assert len(client.deleted) == 1
    assert client.query_calls[0]["retry"] is None
    assert client.query_calls[0]["job_retry"] is None
    assert len(client.query_jobs[0].result_calls) == 1
    assert client.query_jobs[0].result_calls[0]["retry"] is None
    assert client.query_jobs[0].result_calls[0]["job_retry"] is None


def test_aborted_reason_is_classified_without_retry(
    aborted_reason_error: _FakeBackendError,
) -> None:
    _assert_concurrency_is_not_retried(aborted_reason_error)


def test_concurrent_message_is_classified_without_retry(
    concurrent_message_error: _FakeBackendError,
) -> None:
    _assert_concurrency_is_not_retried(concurrent_message_error)


def test_serialization_message_is_classified_without_retry(
    serialization_message_error: _FakeBackendError,
) -> None:
    _assert_concurrency_is_not_retried(serialization_message_error)


def test_generic_query_error_preserves_cause_and_rerun_counts_partial_commit_unchanged() -> None:
    backend_error = _FakeBackendError("permission denied")
    client = _FakeBigQueryClient(query_errors={"membership": backend_error})

    with pytest.raises(persistence.PersistenceError) as raised:
        _real_write(client)

    assert raised.value.__cause__ is backend_error
    assert raised.value.result.inserted_counts["candidates"] == 1
    assert raised.value.result.inserted_counts["evidence"] == 1
    client.query_errors.clear()
    rerun = _real_write(client)
    assert rerun.unchanged_counts["candidates"] == 1
    assert rerun.unchanged_counts["evidence"] == 1
    assert rerun.inserted_counts["membership"] == 1


def test_cleanup_failure_is_secondary_to_primary_error_and_primary_when_alone() -> None:
    primary = _FakeBackendError("permission denied")
    client = _FakeBigQueryClient(
        query_errors={"candidates": primary}, delete_error=_FakeBackendError("cleanup denied")
    )

    with pytest.raises(persistence.PersistenceError) as raised:
        _real_write(client)

    assert raised.value.__cause__ is primary
    assert isinstance(raised.value.cleanup_errors[0], persistence.CleanupFailure)
    assert raised.value.result.cleanup_state == "failed"

    cleanup_only = _FakeBigQueryClient(delete_error=_FakeBackendError("cleanup denied"))
    with pytest.raises(persistence.CleanupFailure) as cleanup_raised:
        _real_write(cleanup_only)
    assert cleanup_raised.value.result.cleanup_state == "failed"


class _DistinctCleanupFailureClient(_FakeBigQueryClient):
    def delete_table(self, table: bigquery.Table, *, not_found_ok: bool) -> None:
        assert not_found_ok is True
        self.deleted.append(table)
        table_name = table.labels["open_intelligence_table"]
        raise _FakeBackendError(f"cleanup denied for {table_name}")


def test_cleanup_only_failure_retains_every_error_in_attempt_order() -> None:
    client = _DistinctCleanupFailureClient()

    with pytest.raises(persistence.CleanupFailure) as raised:
        _real_write(client)

    attempted = tuple(
        reversed(("candidates", "evidence", "membership", "lineage", "predictions", "outcomes"))
    )
    assert tuple(str(error.__cause__) for error in raised.value.cleanup_errors) == tuple(
        f"cleanup denied for {table}" for table in attempted
    )
    assert tuple(table.labels["open_intelligence_table"] for table in client.deleted) == attempted
    assert raised.value.result.cleanup_state == "failed"


def test_primary_failure_retains_every_cleanup_error_in_attempt_order() -> None:
    primary = _FakeBackendError("permission denied")
    client = _DistinctCleanupFailureClient(query_errors={"predictions": primary})

    with pytest.raises(persistence.PersistenceError) as raised:
        _real_write(client)

    attempted = tuple(reversed(("candidates", "evidence", "membership", "lineage", "predictions")))
    assert raised.value.__cause__ is primary
    assert tuple(str(error.__cause__) for error in raised.value.cleanup_errors) == tuple(
        f"cleanup denied for {table}" for table in attempted
    )
    assert tuple(table.labels["open_intelligence_table"] for table in client.deleted) == attempted
    assert raised.value.result.cleanup_state == "failed"


def test_empty_real_write_creates_no_temp_tables_or_queries() -> None:
    client = _FakeBigQueryClient()
    result = _real_write(
        client,
        _batch(candidates=(), evidence=(), membership=(), lineage=(), predictions=(), outcomes=()),
    )

    assert result.inserted_counts == dict.fromkeys(persistence.TABLE_BINDINGS, 0)
    assert client.created == []
    assert client.loads == []
    assert client.queries == []
    assert client.deleted == []


class _InterfaceJob:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.output_rows = 1

    def result(
        self,
        page_size: int | None = None,
        max_results: int | None = None,
        retry: object = _UNSET_RETRY,
        timeout: object = _UNSET_RETRY,
        start_index: int | None = None,
        job_retry: object = _UNSET_RETRY,
    ) -> Iterable[bigquery.table.Row]:
        return (
            bigquery.table.Row(
                (0, 1, 0, 0),
                {"inserted_count": 0, "unchanged_count": 1, "conflict_count": 0, "status": 0},
            ),
        )


class _InterfaceClient:
    def __init__(self) -> None:
        self.project = PROJECT
        self.dataset = DATASET
        self.location = LOCATION
        self.writer_identity = WRITER_IDENTITY
        self._credentials = _FakeCredentials()
        self.created: list[bigquery.Table] = []
        self.deleted: list[bigquery.Table] = []

    def create_table(self, table: bigquery.Table, *, exists_ok: bool = False) -> bigquery.Table:
        assert isinstance(table, bigquery.Table)
        assert exists_ok is False
        self.created.append(table)
        return table

    def load_table_from_json(
        self,
        json_rows: object,
        destination: bigquery.Table,
        *,
        location: str,
        job_config: bigquery.LoadJobConfig,
    ) -> _InterfaceJob:
        assert isinstance(destination, bigquery.Table)
        assert isinstance(job_config, bigquery.LoadJobConfig)
        assert location == LOCATION
        return _InterfaceJob()

    def query(
        self,
        sql: str,
        *,
        job_config: bigquery.QueryJobConfig,
        location: str,
        retry: object,
        job_retry: object,
    ) -> _InterfaceJob:
        assert isinstance(job_config, bigquery.QueryJobConfig)
        assert location == LOCATION
        assert retry is None
        assert job_retry is None
        job = _InterfaceJob()
        return job

    def delete_table(self, table: bigquery.Table, *, not_found_ok: bool) -> None:
        assert not_found_ok is True
        self.deleted.append(table)


def test_legacy_plan_serializer_retains_original_version_fixture_hashes():
    from dataclasses import replace

    plans = dict(persistence.plan_statements(PROJECT, DATASET, _batch()))
    for table in ("candidates", "predictions"):
        plan = plans[table]
        plans[table] = replace(
            plan,
            parameters={
                **plan.parameters,
                "canonical_rows": tuple(
                    {**row, "cluster_build_version": "cluster_v1"}
                    for row in plan.parameters["canonical_rows"]
                ),
            },
        )
    digests = persistence._statement_digests(plans)
    assert (
        digests["candidates"] == "ee7416627c44b421bdbf0e0b43200295da56d35e43b06a7f3c8585f2144f0f30"
    )
    assert (
        digests["predictions"] == "1a1ad8c4b1d84a45dc9b1c67b690e0fddb5ea012e2a0d58813f1f12ea5fa64d6"
    )


def test_real_write_uses_installed_bigquery_resources_and_preserves_dry_run_shape() -> None:
    client = _InterfaceClient()

    result = _real_write(client)  # type: ignore[arg-type]
    dry_run = _dry_run()

    assert tuple(persistence.PersistenceResult.__dataclass_fields__) == (
        "project",
        "dataset",
        "dry_run",
        "validated_counts",
        "inserted_counts",
        "unchanged_counts",
        "conflict_counts",
        "statement_digests",
        "cleanup_state",
    )
    assert dict(dry_run.statement_digests) == {
        "candidates": "748accf318066daf56168ca986c32909db22515996c58296a487742aa7bfa52c",
        "evidence": "a63e485a0440ee50fdd2d52f122c72e3ace5115b779322ce59e5ea618ee8b544",
        "membership": "96951da7129589bdb8d77b0e5b59331606f84f0368a30c4e29c9b92034a29262",
        "lineage": "9f5477fbcbc4ba11b51c6c07f28aa339f84a5caaaada36428e4f674ab002d410",
        "predictions": "917c22769e849fe9a7065171b4b941f103e6fea10e42ebe608b4f016d2cb3865",
        "outcomes": "7ac310f0a8a3c79f8fdb3586e42ec09be702d21b7f869e597936e8644b0acd65",
    }
    assert result.cleanup_state == "complete"
    assert len(client.created) == 6
    assert client.deleted == list(reversed(client.created))
    assert all(table.expires is not None for table in client.created)
    assert all(table.schema for table in client.created)


def test_bigquery_protocols_include_retry_and_iterable_row_contracts() -> None:
    assert tuple(inspect.signature(persistence._BigQueryClientProtocol.query).parameters) == (
        "self",
        "sql",
        "job_config",
        "location",
        "retry",
        "job_retry",
    )
    assert tuple(inspect.signature(persistence._JobProtocol.result).parameters) == (
        "self",
        "page_size",
        "max_results",
        "retry",
        "timeout",
        "start_index",
        "job_retry",
    )
    assert (
        get_type_hints(persistence._JobProtocol.result)["return"]
        == Iterable[persistence._CountRowProtocol]
    )


def test_temporary_schemas_match_the_independent_recursive_literal_oracle() -> None:
    assert tuple(EXPECTED_SCHEMA_TREES) == (
        "candidates",
        "evidence",
        "membership",
        "lineage",
        "predictions",
        "outcomes",
    )
    for table, expected in EXPECTED_SCHEMA_TREES.items():
        assert _schema_tree(persistence._schema(table)) == expected


# --- Run receipt writer -------------------------------------------------------
#
# The receipt is the only marker that says a run closed. It is written last,
# every count is derived from the rows actually present, and no run may release
# itself: display_release_state is always blocked at write time.


def _receipt_kwargs(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "run_id": "run_001",
        "client_scope_id": "fixture_scope",
        "market_scope": ("za",),
        "signal_date": date(2026, 8, 25),
        "observation_start": date(2026, 8, 19),
        "observation_end": date(2026, 8, 25),
        "observation_method": "dynamic_signal_identity_v1",
        "source_window_digest": "a" * 64,
        "cluster_build_version": "hybrid_graph_v1",
        "source_family_map_version": "family_map_v2",
        "rule_version": "rule_v4",
        "status": "completed",
        "complete_partitions": True,
        "source_sha": "769408fc55680ca9d920a1e94dacaddba2c0bb91",
        "completed_at": NOW,
    }
    fields.update(overrides)
    return fields


def test_run_receipt_counts_are_derived_from_the_rows_not_supplied():
    """Break caught: a caller declaring counts the rows do not support."""
    batch = _batch()
    receipt = persistence.build_run_receipt_row(batch, (), **_receipt_kwargs())
    assert receipt.candidate_count == len(batch.candidates)
    assert receipt.evidence_count == len(batch.evidence)
    assert receipt.membership_count == len(batch.membership)
    assert receipt.lineage_count == len(batch.lineage)
    assert receipt.prediction_count == len(batch.predictions)
    # Analysis is not one of this batch's row families, so its count comes from
    # the analysis rows handed in, never from a number.
    assert receipt.analysis_count == 0
    assert (
        persistence.build_run_receipt_row(
            batch, ({"analysis_id": "an_1"}, {"analysis_id": "an_2"}), **_receipt_kwargs()
        ).analysis_count
        == 2
    )


def test_a_caller_may_not_supply_any_count_or_release_state():
    signature = inspect.signature(persistence.build_run_receipt_row)
    for forbidden in (
        "candidate_count",
        "evidence_count",
        "membership_count",
        "lineage_count",
        "analysis_count",
        "prediction_count",
        "display_release_state",
    ):
        assert forbidden not in signature.parameters, f"caller can supply {forbidden}"
    # The row set digest may be the one the written tables report through the digest SQL,
    # because BigQuery and Python render floats differently and the proof and release
    # recompute it in SQL. It is optional and must already be a digest.
    assert signature.parameters["row_set_digest"].default is None


def test_every_written_receipt_is_blocked_and_no_run_can_release_itself():
    receipt = persistence.build_run_receipt_row(_batch(), (), **_receipt_kwargs())
    assert receipt.display_release_state == "blocked"


def test_the_receipt_uses_the_one_shared_type_not_a_second_one():
    from src.analysis.open_intelligence import run_receipts

    receipt = persistence.build_run_receipt_row(_batch(), (), **_receipt_kwargs())
    assert isinstance(receipt, run_receipts.OpenIntelligenceRunReceipt)
    assert receipt.run_contract_version == run_receipts.RUN_RECEIPT_CONTRACT_VERSION


def test_row_set_digest_changes_when_any_row_family_changes():
    base = persistence.build_run_receipt_row(_batch(), (), **_receipt_kwargs())
    extra_evidence = _evidence()
    extra_evidence["evidence_id"] = "ev_" + "c" * 64
    changed = persistence.build_run_receipt_row(
        _batch(evidence=(_evidence(), extra_evidence)), (), **_receipt_kwargs()
    )
    assert base.row_set_digest != changed.row_set_digest
    with_analysis = persistence.build_run_receipt_row(
        _batch(), ({"analysis_id": "an_1"},), **_receipt_kwargs()
    )
    assert base.row_set_digest != with_analysis.row_set_digest


def test_row_set_digest_is_stable_across_analysis_input_order():
    """Break caught: one run digesting two ways because analysis arrived shuffled.

    The batch canonicalises its own families, so only analysis rows can reach
    the digest in caller order.
    """
    one, two = {"analysis_id": "an_1"}, {"analysis_id": "an_2"}
    first = persistence.build_run_receipt_row(_batch(), (one, two), **_receipt_kwargs())
    second = persistence.build_run_receipt_row(_batch(), (two, one), **_receipt_kwargs())
    assert first.row_set_digest == second.row_set_digest


def test_a_window_that_has_not_closed_is_refused_by_the_shared_contract():
    from src.analysis.open_intelligence import run_receipts

    with pytest.raises(run_receipts.RunReceiptError):
        persistence.build_run_receipt_row(
            _batch(),
            (),
            **_receipt_kwargs(signal_date=date(2026, 8, 24), observation_end=date(2026, 8, 25)),
        )


# --- The producing bridge -----------------------------------------------------
#
# Approved by 42-noncanary-run-release-approval-request-2026-08-29.md section
# 3.3: a pure function from a pipeline result plus metric and readiness inputs
# to a persistable batch. The law it enforces is that a component is either
# admitted with measured metrics or skipped with named missing work; there is
# no third, silent path, and a component the caller says nothing about refuses
# the whole build rather than vanishing.


def _bridge_component():
    from tests.unit import test_dynamic_signal_rows as fixtures

    return fixtures.component()


def _bridge_inputs(**overrides: object) -> dict[str, object]:
    from tests.unit import test_dynamic_signal_rows as fixtures

    component = fixtures.component()
    key = persistence.component_bridge_key(component)
    values: dict[str, object] = {
        "result": SimpleNamespace(components=(component,), error_state=None),
        "scope": fixtures.scope(),
        "signal_date": fixtures.SIGNAL_DATE,
        "created_at": fixtures.CREATED_AT,
        "metrics_by_component": {},
        "receipts_by_component": {},
        "readiness_by_component": {},
        "readiness_rules": fixtures.readiness_rules(),
        "missing_by_component": {key: ("velocity_formula_unapproved",)},
        "promotion_results_by_signal_id": {},
    }
    values.update(overrides)
    return values


def _bridge(**overrides: object):
    values = _bridge_inputs(**overrides)
    result = values.pop("result")
    return persistence.build_producing_batch(result, **values)


def test_a_component_without_metric_authority_is_skipped_with_named_reasons() -> None:
    outcome = _bridge()
    assert outcome.admitted == ()
    assert len(outcome.skipped) == 1
    key, reasons = outcome.skipped[0]
    assert key == persistence.component_bridge_key(_bridge_component())
    assert reasons == ("velocity_formula_unapproved",)
    for table in persistence.TABLE_BINDINGS:
        assert getattr(outcome.batch, table) == ()


def test_a_measured_component_is_admitted_with_exact_rows() -> None:
    from dataclasses import replace

    from src.analysis.open_intelligence import rows as rows_module
    from src.analysis.open_intelligence.pipeline import MembershipReceipt

    from tests.unit import test_dynamic_signal_rows as fixtures

    component = replace(fixtures.component(), label="repair routine")
    key = persistence.component_bridge_key(component)
    membership = MembershipReceipt(
        member_id="mem_" + "1" * 64,
        member_identity="za|keyword|repair routine",
        candidate_type="keyword",
        canonical_value="repair routine",
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        row_id="fixture_row_001",
        qualifies_evidence=False,
    )
    outcome = _bridge(
        result=SimpleNamespace(
            components=(component,),
            error_state=None,
            evidence_projection=SimpleNamespace(
                memberships_by_component={"projection_component": (membership,)}
            ),
        ),
        metrics_by_component={key: fixtures.metrics()},
        receipts_by_component={key: fixtures.receipts()},
        readiness_by_component={key: fixtures.readiness()},
        missing_by_component={},
        quality_evaluated=True,
    )
    assert outcome.admitted == (key,)
    assert outcome.skipped == ()
    candidate, evidence = rows_module.build_dynamic_signal_rows(
        component=component,
        scope=fixtures.scope(),
        signal_date=fixtures.SIGNAL_DATE,
        created_at=fixtures.CREATED_AT,
        metrics=fixtures.metrics(),
        receipts=fixtures.receipts(),
        readiness=fixtures.readiness(),
        readiness_rules=fixtures.readiness_rules(),
    )

    # The batch canonicalises rows into immutable mappings with tuple-valued
    # sequences, so compare content with sequences normalised.
    def _plain(row: object) -> dict[str, object]:
        return {
            field: list(value) if isinstance(value, (tuple, list)) else value
            for field, value in dict(row).items()
        }

    assert [_plain(row) for row in outcome.batch.candidates] == [_plain(candidate)]
    assert {row["evidence_id"] for row in outcome.batch.evidence} == {
        row["evidence_id"] for row in evidence
    }


def _promotion_result(component, receipt_mapping, readiness_result, metrics_value):
    from scripts.staging.scoring_qualification import approved_decision_strength_rules
    from src.analysis.open_intelligence import rows as rows_module
    from src.analysis.open_intelligence.scoring import SignalScoreInput, score_signal

    from tests.unit import test_dynamic_signal_rows as fixtures

    candidate, _evidence = rows_module.build_dynamic_signal_rows(
        component=component,
        scope=fixtures.scope(),
        signal_date=fixtures.SIGNAL_DATE,
        created_at=fixtures.CREATED_AT,
        metrics=metrics_value,
        receipts=receipt_mapping,
        readiness=readiness_result,
        readiness_rules=fixtures.readiness_rules(),
        quality_evaluated=True,
    )
    result = score_signal(
        SignalScoreInput(
            signal_id=candidate["signal_id"],
            market=candidate["market"],
            signal_date=candidate["signal_date"],
            discovery_mode=candidate["discovery_mode"],
            evidence_state=candidate["evidence_state"],
            qualifying_source_families=len(readiness_result.qualifying_families),
            qualifying_current_receipts=sum(
                item.availability == "available" for item in receipt_mapping.values()
            ),
            source_integrity=1.0,
            velocity=candidate["velocity_score"],
            breadth=candidate["breadth_score"],
            source_independence=candidate["independence_score"],
            geo_confidence=candidate["geo_confidence"],
            foreign_market_sample_reviewed=True,
            foreign_market_leakage=0,
            duplicate_identity=False,
            factual_conflict=False,
            directional_conflict=False,
            membership_receipts_complete=True,
            cluster_build_version=candidate["cluster_build_version"],
        ),
        approved_decision_strength_rules(),
    )
    return candidate, result


def test_a_measured_ready_component_requires_promotion_before_writing_prediction() -> None:
    from dataclasses import replace

    from src.analysis.open_intelligence.pipeline import MembershipReceipt

    from tests.unit import test_dynamic_signal_rows as fixtures

    component = replace(
        fixtures.component(),
        label="repair routine",
        build_version="hybrid_graph_v2",
    )
    key = persistence.component_bridge_key(component)
    membership = MembershipReceipt(
        member_id="mem_" + "1" * 64,
        member_identity="za|keyword|repair routine",
        candidate_type="keyword",
        canonical_value="repair routine",
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        row_id="fixture_row_001",
        qualifies_evidence=False,
    )
    result = SimpleNamespace(
        components=(component,),
        error_state=None,
        evidence_projection=SimpleNamespace(
            memberships_by_component={"projection_component": (membership,)}
        ),
    )
    without_promotion = _bridge(
        result=result,
        metrics_by_component={key: fixtures.metrics()},
        receipts_by_component={key: fixtures.receipts()},
        readiness_by_component={key: fixtures.readiness()},
        missing_by_component={},
        quality_evaluated=True,
    )
    assert without_promotion.batch.predictions == ()

    candidate, promotion = _promotion_result(
        component, fixtures.receipts(), fixtures.readiness(), fixtures.metrics()
    )
    outcome = _bridge(
        result=result,
        metrics_by_component={key: fixtures.metrics()},
        receipts_by_component={key: fixtures.receipts()},
        readiness_by_component={key: fixtures.readiness()},
        missing_by_component={},
        quality_evaluated=True,
        promotion_results_by_signal_id={candidate["signal_id"]: promotion},
    )

    assert len(outcome.batch.candidates) == 1
    candidate = outcome.batch.candidates[0]
    assert len(outcome.batch.membership) == 1
    member = outcome.batch.membership[0]
    assert member["signal_id"] == candidate["signal_id"]
    assert member["canonical_value"] == candidate["label"]
    assert member["qualifies_evidence"] is True
    assert len(outcome.batch.predictions) == 1
    prediction = outcome.batch.predictions[0]
    assert prediction["signal_id"] == candidate["signal_id"]
    assert prediction["evidence_state"] == "ready"
    assert prediction["display_eligible"] is False


def test_a_measured_component_without_exact_projection_membership_refuses() -> None:
    from tests.unit import test_dynamic_signal_rows as fixtures

    component = fixtures.component()
    key = persistence.component_bridge_key(component)
    result = SimpleNamespace(
        components=(component,),
        error_state=None,
        evidence_projection=SimpleNamespace(memberships_by_component={}),
    )
    with pytest.raises(persistence.BatchInvalid, match="membership projection"):
        _bridge(
            result=result,
            metrics_by_component={key: fixtures.metrics()},
            receipts_by_component={key: fixtures.receipts()},
            readiness_by_component={key: fixtures.readiness()},
            missing_by_component={},
            quality_evaluated=True,
        )


def test_exact_label_member_identity_resolves_same_value_type_ambiguity() -> None:
    from dataclasses import replace
    from types import MappingProxyType

    from src.analysis.open_intelligence.graph import SignalComponent
    from src.analysis.open_intelligence.pipeline import MembershipReceipt
    from src.analysis.open_intelligence.readiness import EvidenceRecord, evaluate_readiness

    from tests.unit import test_dynamic_signal_rows as fixtures

    component = SignalComponent(
        market="za",
        member_identities=(
            "za|hashtag|repair routine",
            "za|keyword|repair routine",
        ),
        terms=("repair routine",),
        row_receipts=("fixture_row_001", "fixture_row_002"),
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        creator_ids=(),
        label="repair routine",
        label_member_identity="za|hashtag|repair routine",
        build_version="hybrid_graph_v2",
    )
    receipts = MappingProxyType(
        {
            "fixture_row_001": fixtures.receipts()["fixture_row_001"],
            "fixture_row_002": replace(
                fixtures.receipts()["fixture_row_002"],
                member_identity="za|hashtag|repair routine",
            ),
        }
    )
    readiness = evaluate_readiness(
        tuple(
            EvidenceRecord(
                row_id=item.row_id,
                source_family=item.source_family,
                direction=item.direction,
                published_at=item.published_at,
                availability=item.availability,
                geo_confidence=item.geo_confidence,
                factual_conflict=item.factual_conflict,
            )
            for item in receipts.values()
        ),
        fixtures.readiness_rules(),
        quality_evaluated=True,
    )
    memberships = (
        MembershipReceipt(
            member_id="mem_" + "1" * 64,
            member_identity="za|hashtag|repair routine",
            candidate_type="hashtag",
            canonical_value="repair routine",
            source_families=("youtube",),
            platforms=("youtube",),
            row_id="fixture_row_002",
            qualifies_evidence=False,
        ),
        MembershipReceipt(
            member_id="mem_" + "2" * 64,
            member_identity="za|keyword|repair routine",
            candidate_type="keyword",
            canonical_value="repair routine",
            source_families=("reddit",),
            platforms=("reddit",),
            row_id="fixture_row_001",
            qualifies_evidence=False,
        ),
    )
    key = persistence.component_bridge_key(component)
    candidate, promotion = _promotion_result(component, receipts, readiness, fixtures.metrics())
    outcome = _bridge(
        result=SimpleNamespace(
            components=(component,),
            error_state=None,
            evidence_projection=SimpleNamespace(
                memberships_by_component={"projection_component": memberships}
            ),
        ),
        metrics_by_component={key: fixtures.metrics()},
        receipts_by_component={key: receipts},
        readiness_by_component={key: readiness},
        missing_by_component={},
        quality_evaluated=True,
        promotion_results_by_signal_id={candidate["signal_id"]: promotion},
    )

    assert len(outcome.batch.candidates) == 1
    assert len(outcome.batch.membership) == 2
    assert {
        (
            row["member_identity"],
            row["candidate_type"],
            row["canonical_value"],
            row["row_id"],
        )
        for row in outcome.batch.membership
    } == {
        (
            "za|hashtag|repair routine",
            "hashtag",
            "repair routine",
            "fixture_row_002",
        ),
        (
            "za|keyword|repair routine",
            "keyword",
            "repair routine",
            "fixture_row_001",
        ),
    }
    assert len(outcome.batch.predictions) == 1
    assert outcome.batch.candidates[0]["label_member_identity"] == ("za|hashtag|repair routine")


def test_a_component_named_in_neither_mapping_refuses_the_build() -> None:
    with pytest.raises(persistence.BatchInvalid):
        _bridge(missing_by_component={})


def test_a_component_named_in_both_mappings_refuses_the_build() -> None:
    from tests.unit import test_dynamic_signal_rows as fixtures

    key = persistence.component_bridge_key(_bridge_component())
    with pytest.raises(persistence.BatchInvalid):
        _bridge(
            metrics_by_component={key: fixtures.metrics()},
            receipts_by_component={key: fixtures.receipts()},
            readiness_by_component={key: fixtures.readiness()},
        )


def test_a_result_carrying_an_error_state_refuses_the_build() -> None:
    component = _bridge_component()
    with pytest.raises(persistence.BatchInvalid):
        _bridge(
            result=SimpleNamespace(components=(component,), error_state="candidate_input_invalid:x")
        )


def test_an_admitted_component_missing_receipts_or_readiness_refuses() -> None:
    from tests.unit import test_dynamic_signal_rows as fixtures

    key = persistence.component_bridge_key(_bridge_component())
    with pytest.raises(persistence.BatchInvalid):
        _bridge(
            metrics_by_component={key: fixtures.metrics()},
            missing_by_component={},
        )


R3_APPLY_IDENTITY = "trends-engine-oi-r3-apply@ogilvy-trends-v2.iam.gserviceaccount.com"


def test_dry_run_accepts_only_the_writer_identity_the_caller_names() -> None:
    # The R3 apply runs under its own operation identity, not the shared staging engine
    # identity, so the caller names the approved writer and the target check binds to it.
    batch = _batch()
    result = persistence.persist_open_intelligence_rows(
        project=PROJECT,
        dataset=DATASET,
        client=_target(writer_identity=R3_APPLY_IDENTITY),
        batch=batch,
        rule_bundle=_rules(),
        dry_run=True,
        writer_identity=R3_APPLY_IDENTITY,
    )
    assert result.dry_run is True
    with pytest.raises(persistence.TargetInvalid):
        persistence.persist_open_intelligence_rows(
            project=PROJECT,
            dataset=DATASET,
            client=_target(writer_identity=R3_APPLY_IDENTITY),
            batch=batch,
            rule_bundle=_rules(),
            dry_run=True,
        )
    with pytest.raises(persistence.TargetInvalid):
        persistence.persist_open_intelligence_rows(
            project=PROJECT,
            dataset=DATASET,
            client=_target(),
            batch=batch,
            rule_bundle=_rules(),
            dry_run=True,
            writer_identity=R3_APPLY_IDENTITY,
        )


def _measured_bridge_inputs(component, key, readiness_result):
    from src.analysis.open_intelligence.pipeline import MembershipReceipt

    from tests.unit import test_dynamic_signal_rows as fixtures

    membership = MembershipReceipt(
        member_id="mem_" + "1" * 64,
        member_identity="za|keyword|repair routine",
        candidate_type="keyword",
        canonical_value="repair routine",
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        row_id="fixture_row_001",
        qualifies_evidence=False,
    )
    return {
        "result": SimpleNamespace(
            components=(component,),
            error_state=None,
            evidence_projection=SimpleNamespace(
                memberships_by_component={"projection_component": (membership,)}
            ),
        ),
        "metrics_by_component": {key: fixtures.metrics()},
        "receipts_by_component": {key: fixtures.receipts()},
        "readiness_by_component": {key: readiness_result},
        "missing_by_component": {},
        "quality_evaluated": True,
    }


def test_bridge_forwards_the_quality_flags_the_readiness_was_computed_with() -> None:
    # The quality authority classifies readiness with the technical verdict and the conflict
    # flag; the row builder recomputes readiness from the receipts and must be handed the
    # same flags, or a component with a failed technical check refuses the whole run.
    from dataclasses import replace

    from src.analysis.open_intelligence.readiness import EvidenceRecord, evaluate_readiness

    from tests.unit import test_dynamic_signal_rows as fixtures

    component = replace(fixtures.component(), label="repair routine")
    key = persistence.component_bridge_key(component)
    records = [
        EvidenceRecord(
            row_id=item.row_id,
            source_family=item.source_family,
            direction=item.direction,
            published_at=item.published_at,
            availability=item.availability,
            geo_confidence=item.geo_confidence,
            factual_conflict=item.factual_conflict,
        )
        for item in fixtures.receipts().values()
    ]
    failed = evaluate_readiness(
        records, fixtures.readiness_rules(), quality_evaluated=True, quality_failed=True
    )
    assert failed != fixtures.readiness()
    inputs = _measured_bridge_inputs(component, key, failed)
    with pytest.raises(ValueError, match="readiness result does not match"):
        _bridge(**inputs)
    outcome = _bridge(**inputs, quality_failed_by_component={key: True})
    assert outcome.admitted == (key,)
    assert outcome.batch.candidates[0]["evidence_state"] == failed.state
    conflicted = evaluate_readiness(
        records, fixtures.readiness_rules(), quality_evaluated=True, factual_conflict=True
    )
    assert conflicted != fixtures.readiness()
    inputs = _measured_bridge_inputs(component, key, conflicted)
    with pytest.raises(ValueError, match="readiness result does not match"):
        _bridge(**inputs)
    outcome = _bridge(**inputs, factual_conflict_by_component={key: True})
    assert outcome.admitted == (key,)


def test_run_receipt_takes_the_row_set_digest_the_tables_report() -> None:
    # BigQuery and Python render floats differently, so a digest the receipt computed in
    # Python can never be reproduced by the proof and release SQL. The apply reads the
    # digest back from the written rows through the same SQL and hands it to the receipt.
    from datetime import UTC, date, datetime

    batch = persistence.OpenIntelligenceRowBatch((), (), (), (), (), ())
    common = {
        "run_id": "run_20260903_dynamic_apply_v2_r16",
        "client_scope_id": "ogilvy_default",
        "market_scope": ("za",),
        "signal_date": date(2026, 8, 27),
        "observation_start": date(2026, 8, 14),
        "observation_end": date(2026, 8, 27),
        "observation_method": "dynamic_replay",
        "source_window_digest": "1" * 64,
        "cluster_build_version": "hybrid_graph_v2",
        "source_family_map_version": "channel_family_v2",
        "rule_version": "composition_rules_v2",
        "status": "completed",
        "complete_partitions": True,
        "source_sha": "a" * 40,
        "completed_at": datetime(2026, 9, 2, tzinfo=UTC),
    }
    reported = persistence.build_run_receipt_row(batch, (), row_set_digest="b" * 64, **common)
    assert reported.row_set_digest == "b" * 64
    computed = persistence.build_run_receipt_row(batch, (), **common)
    assert computed.row_set_digest != "b" * 64
    with pytest.raises(persistence.BatchInvalid, match="row set digest"):
        persistence.build_run_receipt_row(batch, (), row_set_digest="zz", **common)


# D05: membership edges emit dispositions through the injected telemetry


def test_bridge_emits_membership_dispositions_and_unit_counts_through_the_telemetry():
    from src.analysis.open_intelligence.coverage_telemetry_sink import (
        BoundaryTelemetry,
        MemoryDispositionSink,
    )

    telemetry = BoundaryTelemetry(MemoryDispositionSink())
    silent = _bridge(**_v3_bridge_inputs())
    outcome = _bridge(**_v3_bridge_inputs(), telemetry=telemetry)
    assert outcome.batch == silent.batch
    member = outcome.batch.membership[0]
    assert len(telemetry.records) == 1
    record = telemetry.records[0]
    assert record["boundary"] == "membership"
    assert record["observation_key"] == member["member_identity"]
    assert record["identity_kind"] == "inferred"
    assert record["collection_event_id"] == member["signal_id"]
    assert record["source_row_id"] == member["row_id"]
    assert record["route"] == member["candidate_type"]
    assert record["market"] == member["market"]
    assert record["operation_id"] == member["run_id"]
    assert record["outcome"] == "admitted"
    assert record["source_binding_digest"] == "a" * 64
    counts = {n["unit"]: n["value"] for n in telemetry.notes if n["kind"] == "count"}
    assert counts == {"candidates": 1, "membership_edges": 1}


def test_bridge_without_source_provenance_records_membership_telemetry_as_unavailable():
    from dataclasses import replace

    from src.analysis.open_intelligence.coverage_telemetry_sink import (
        BoundaryTelemetry,
        MemoryDispositionSink,
    )
    from src.analysis.open_intelligence.pipeline import MembershipReceipt

    from tests.unit import test_dynamic_signal_rows as fixtures

    component = replace(
        fixtures.component(), label="repair routine", build_version="hybrid_graph_v2"
    )
    key = persistence.component_bridge_key(component)
    membership = MembershipReceipt(
        member_id="mem_" + "1" * 64,
        member_identity="za|keyword|repair routine",
        candidate_type="keyword",
        canonical_value="repair routine",
        source_families=("reddit", "youtube"),
        platforms=("reddit", "youtube"),
        row_id="fixture_row_001",
        qualifies_evidence=False,
    )
    result = SimpleNamespace(
        components=(component,),
        error_state=None,
        evidence_projection=SimpleNamespace(
            memberships_by_component={"projection_component": (membership,)}
        ),
    )
    telemetry = BoundaryTelemetry(MemoryDispositionSink())
    outcome = _bridge(
        result=result,
        metrics_by_component={key: fixtures.metrics()},
        receipts_by_component={key: fixtures.receipts()},
        readiness_by_component={key: fixtures.readiness()},
        missing_by_component={},
        quality_evaluated=True,
        telemetry=telemetry,
    )
    assert len(outcome.batch.membership) == 1
    assert telemetry.records == []
    unavailable = [n for n in telemetry.notes if n["kind"] == "unavailable"]
    assert [(n["boundary"], n["reason_code"]) for n in unavailable] == [
        ("membership", "source_binding_digest_unavailable")
    ]
