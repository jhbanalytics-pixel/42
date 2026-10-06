"""Contract tests for the fixture-only Open Intelligence persistence canary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import persistence

SCRIPT_PATH = Path("scripts/staging/canary_open_intelligence_persistence.py")
PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
RUN_DATE = date(2026, 8, 26)
RUN_ID = "oi_persistence_canary_v4_2026-08-26"
SIGNAL_ID = "sig_ce460d4d31a3c598324cc01e473e7fabdf3dd44f080ae76349600a41212b1b67"
PRIOR_SIGNAL_ID = "sig_79dd6736909fab39b872b1bdaeec4fe9f07ee9834a14b787ee4dd84a0dfc29bf"
EVIDENCE_ID = "ev_e5996633fc8d0768a6e17564ff734c48aded5ac301b96d22a92e28fc613f1c4c"
MEMBER_ID = "mem_20568efcd151ec07f4fc3dc85f56feb2c5e3b1e0bcad39fee30752b98dc0320f"
PREDICTION_ID = "pred_954f8aec33f749734de0f805fb9680c2971a02e9c2b3f5f7f933d1b73ac31f74"
OUTCOME_ID = "out_da1d6057ccec05c47a3c9fc1247b5e84181615669be388c4fcd971af364e1b78"
NOW = datetime(2026, 8, 26, tzinfo=UTC)
TABLES = ("candidates", "evidence", "membership", "lineage", "predictions", "outcomes")
DIGESTS = {table: f"{index:x}" * 64 for index, table in enumerate(TABLES, start=1)}
ALTERED_DIGESTS = {**DIGESTS, "candidates": "f" * 64}


def _module():
    assert SCRIPT_PATH.is_file(), "RED: the canary entry point has not been created"
    spec = importlib.util.spec_from_file_location("persistence_canary", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _counts(*, inserted: int, unchanged: int, conflicts: int = 0) -> dict[str, int]:
    return {
        table: {"inserted": inserted, "unchanged": unchanged, "conflicts": conflicts}["inserted"]
        for table in TABLES
    }


def _result(
    *,
    inserted: int,
    unchanged: int,
    conflicts: int = 0,
    unchanged_counts: dict[str, int] | None = None,
    conflict_counts: dict[str, int] | None = None,
    statement_digests: dict[str, str] = DIGESTS,
    cleanup_state: str = "complete",
):
    resolved_conflicts = dict.fromkeys(TABLES, 0)
    resolved_conflicts["candidates"] = conflicts
    return persistence.PersistenceResult(
        project=PROJECT,
        dataset=DATASET,
        dry_run=False,
        validated_counts=dict.fromkeys(TABLES, 1),
        inserted_counts=dict.fromkeys(TABLES, inserted),
        unchanged_counts=unchanged_counts or dict.fromkeys(TABLES, unchanged),
        conflict_counts=conflict_counts or resolved_conflicts,
        statement_digests=statement_digests,
        cleanup_state=cleanup_state,
    )


class _Client:
    def __init__(self, table_ids: tuple[str, ...] = ()) -> None:
        self.table_ids = table_ids
        self.listed_datasets: list[str] = []

    def list_tables(self, dataset: str):
        self.listed_datasets.append(dataset)
        return [SimpleNamespace(table_id=table_id) for table_id in self.table_ids]


class _PersistenceBoundary:
    OpenIntelligenceRowBatch = persistence.OpenIntelligenceRowBatch
    CandidateRunConflict = persistence.CandidateRunConflict

    def __init__(
        self,
        *,
        write_cleanup_state: str = "complete",
        write_digests: dict[str, str] = DIGESTS,
        conflict_digests: dict[str, str] = ALTERED_DIGESTS,
        conflict_unchanged_counts: dict[str, int] | None = None,
        conflict_counts: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.write_calls: list[dict[str, object]] = []
        self.first_novelty: float | None = None
        self.write_cleanup_state = write_cleanup_state
        self.write_digests = write_digests
        self.conflict_digests = conflict_digests
        self.conflict_unchanged_counts = conflict_unchanged_counts
        self.conflict_counts = conflict_counts

    def persist_open_intelligence_rows(self, **kwargs: object):
        self.calls.append(kwargs)
        if kwargs["dry_run"] is True:
            digests = (
                self.conflict_digests
                if kwargs["batch"].candidates[0]["novelty_score"] != 0.1
                else DIGESTS
            )
            return persistence.PersistenceResult(
                project=PROJECT,
                dataset=DATASET,
                dry_run=True,
                validated_counts=dict.fromkeys(TABLES, 1),
                inserted_counts=dict.fromkeys(TABLES, 0),
                unchanged_counts=dict.fromkeys(TABLES, 0),
                conflict_counts=dict.fromkeys(TABLES, 0),
                statement_digests=digests,
                cleanup_state="not_started",
            )
        self.write_calls.append(kwargs)
        if len(self.write_calls) == 1:
            self.first_novelty = kwargs["batch"].candidates[0]["novelty_score"]
            return _result(
                inserted=1,
                unchanged=0,
                statement_digests=self.write_digests,
                cleanup_state=self.write_cleanup_state,
            )
        if len(self.write_calls) == 2:
            return _result(
                inserted=0,
                unchanged=1,
                statement_digests=self.write_digests,
                cleanup_state=self.write_cleanup_state,
            )
        if kwargs["batch"].candidates[0]["novelty_score"] == self.first_novelty:
            return _result(inserted=0, unchanged=1)
        conflict = persistence.CandidateRunConflict("candidate conflict")
        conflict.result = _result(
            inserted=0,
            unchanged=0,
            conflicts=1,
            unchanged_counts=self.conflict_unchanged_counts,
            conflict_counts=self.conflict_counts,
            statement_digests=self.conflict_digests,
            cleanup_state=self.write_cleanup_state,
        )
        raise conflict


class _FirstUnchangedBoundary(_PersistenceBoundary):
    def persist_open_intelligence_rows(self, **kwargs: object):
        self.calls.append(kwargs)
        if kwargs["dry_run"] is True:
            digests = (
                self.conflict_digests
                if kwargs["batch"].candidates[0]["novelty_score"] != 0.1
                else DIGESTS
            )
            return persistence.PersistenceResult(
                project=PROJECT,
                dataset=DATASET,
                dry_run=True,
                validated_counts=dict.fromkeys(TABLES, 1),
                inserted_counts=dict.fromkeys(TABLES, 0),
                unchanged_counts=dict.fromkeys(TABLES, 0),
                conflict_counts=dict.fromkeys(TABLES, 0),
                statement_digests=digests,
                cleanup_state="not_started",
            )
        self.write_calls.append(kwargs)
        if len(self.write_calls) in (1, 2):
            if len(self.write_calls) == 1:
                self.first_novelty = kwargs["batch"].candidates[0]["novelty_score"]
            return _result(inserted=0, unchanged=1)
        if kwargs["batch"].candidates[0]["novelty_score"] == self.first_novelty:
            return _result(inserted=0, unchanged=1)
        conflict = persistence.CandidateRunConflict("candidate conflict")
        conflict.result = _result(
            inserted=0,
            unchanged=0,
            conflicts=1,
            statement_digests=self.conflict_digests,
        )
        raise conflict


def test_red_guard_requires_the_canary_entry_point() -> None:
    assert SCRIPT_PATH.is_file(), "RED: create the fixture-only canary entry point"


def test_direct_script_launch_resolves_the_repository_package(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH.resolve()), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr


def test_build_fixture_batch_has_the_exact_six_safe_rows() -> None:
    module = _module()

    batch = module.build_fixture_batch(RUN_DATE)

    assert tuple(batch.candidates[0]) == persistence.ROW_FIELDS["candidates"]
    assert tuple(batch.evidence[0]) == persistence.ROW_FIELDS["evidence"]
    assert tuple(batch.membership[0]) == persistence.ROW_FIELDS["membership"]
    assert tuple(batch.lineage[0]) == persistence.ROW_FIELDS["lineage"]
    assert tuple(batch.predictions[0]) == persistence.ROW_FIELDS["predictions"]
    assert tuple(batch.outcomes[0]) == persistence.ROW_FIELDS["outcomes"]
    candidate = batch.candidates[0]
    assert candidate["client_scope_id"] == "fixture_scope"
    assert candidate["market_scope"] == ("za",)
    assert candidate["brand_config_id"] == "fixture_brand"
    assert candidate["audience_lens_ids"] == ()
    assert candidate["theme_id"] == "fixture_theme"
    assert candidate["run_id"] == RUN_ID
    assert candidate["signal_id"] == SIGNAL_ID
    assert candidate["label"] == "staging_fixture_canary"
    assert candidate["label_member_identity"] == "za|keyword|staging_fixture_canary"
    assert candidate["discovery_mode"] == "replay"
    assert candidate["evidence_state"] == "ready"
    assert candidate["created_at"] == NOW
    evidence = batch.evidence[0]
    assert evidence["evidence_id"] == EVIDENCE_ID
    assert evidence["vendor_family"] == "staging_fixture_vendor"
    assert evidence["channel_family"] == "staging_fixture_source"
    assert evidence["source_family"] == evidence["channel_family"]
    assert evidence["url"] == "https://example.invalid/open-intelligence/fixture"
    assert evidence["published_at"] == NOW
    membership = batch.membership[0]
    assert membership["member_id"] == MEMBER_ID
    assert membership["member_identity"] == "za|keyword|staging_fixture_canary"
    assert membership["candidate_type"] == "keyword"
    assert membership["canonical_value"] == "staging_fixture_canary"
    assert membership["vendor_families"] == ("staging_fixture_vendor",)
    assert membership["channel_families"] == ("staging_fixture_source",)
    assert membership["source_families"] == membership["channel_families"]
    assert membership["qualifies_evidence"] is True
    lineage = batch.lineage[0]
    assert lineage["from_signal_id"] == PRIOR_SIGNAL_ID
    assert lineage["to_signal_id"] == SIGNAL_ID
    assert lineage["relation"] == "continues"
    prediction = batch.predictions[0]
    assert prediction["prediction_id"] == PREDICTION_ID
    assert prediction["display_eligible"] is False
    assert prediction["baseline"] == {
        "velocity": 0.2,
        "breadth": 0.3,
        "evidence_family_count": 2,
    }
    assert prediction["promotion_target"] == {
        "velocity": 0.4,
        "breadth": 0.5,
        "evidence_family_count": 3,
    }
    outcome = batch.outcomes[0]
    assert outcome["outcome_id"] == OUTCOME_ID
    assert outcome["prediction_id"] == PREDICTION_ID
    assert outcome["signal_id"] == SIGNAL_ID
    assert outcome["outcome"] == "unresolved"
    assert outcome["human_calibration_label"] is None


def test_fixture_identifiers_are_unique_per_run_date() -> None:
    module = _module()
    first = module.fixture_rows(date(2026, 8, 26))
    second = module.fixture_rows(date(2026, 8, 27))

    assert first == module.fixture_rows(date(2026, 8, 26))
    for table, field in (
        ("candidates", "signal_id"),
        ("lineage", "from_signal_id"),
        ("evidence", "evidence_id"),
        ("membership", "member_id"),
        ("predictions", "prediction_id"),
        ("outcomes", "outcome_id"),
    ):
        assert first[table][0][field] != second[table][0][field]


def test_original_v3_fixture_retains_legacy_serialization_only():
    rows = _module().fixture_rows(RUN_DATE)
    original = {
        "run_id": "oi_persistence_canary_v3_2026-08-26",
        "signal_id": "sig_" + "a" * 64,
        "from_signal_id": "sig_" + "b" * 64,
        "to_signal_id": "sig_" + "a" * 64,
        "evidence_id": "ev_" + "c" * 64,
        "member_id": "mem_" + "d" * 64,
        "prediction_id": "pred_" + hashlib.sha256(b"prediction|2026-08-26").hexdigest(),
        "outcome_id": "out_" + hashlib.sha256(b"outcome|2026-08-26").hexdigest(),
        "cluster_build_version": "staging_fixture_cluster_v1",
    }
    expected = {
        "candidates": "d7ac5a3e69af6ef3d54dd66c3a898953a8e4c50563338ea89697ed9f66e4f9e2",
        "evidence": "23c6ef3e386f333d312ed1d433fa1f8742c334ee7b2b929dafaaeb40b04cff3c",
        "membership": "66b1e3fa13b1773f653b07e5b89bde44627a7d005ff595c6a0a12545980bf043",
        "lineage": "300e8593e4952e33c546c0df3dfc7b9be04a9ac8a670196b69ae26bd4e2fffaa",
        "predictions": "bbb9700c2040e64fe942349aecbbac45d893be15bfbb772b6cbda5bd280977a1",
        "outcomes": "c6918fc7dbc7ea18c6829aa725626dcbe6fe98bedb5a660468d5b70aedf6dd96",
    }
    for table, values in rows.items():
        row = values[0]
        row.update({field: value for field, value in original.items() if field in row})
        if table == "predictions":
            row["rule_version"] = "staging_fixture_canary_rules_v1"
        if table == "outcomes":
            row["rule_version"] = "staging_fixture_canary_outcome_rules_v1"
        encoded = persistence.canonical_typed_json(table, row)
        assert hashlib.sha256(encoded.encode()).hexdigest() == expected[table]
    with pytest.raises(persistence.BatchInvalid, match="source_provenance_version_invalid"):
        persistence.OpenIntelligenceRowBatch(**rows)


def test_v4_fixture_versions_and_identities_cannot_reuse_v3_rows():
    module = _module()
    rows = module.fixture_rows(RUN_DATE)
    assert module.CANARY_VERSION == "v4"
    assert rows["candidates"][0]["cluster_build_version"] == "hybrid_graph_v2"
    assert rows["predictions"][0]["cluster_build_version"] == "hybrid_graph_v2"
    assert module.fixture_rule(RUN_DATE).rule_version == "staging_fixture_canary_rules_v4"
    assert module.fixture_rule(RUN_DATE).replay_receipt_id == "staging_fixture_canary_receipt_v4"
    original_ids = (
        ("candidates", "signal_id", "sig_" + "a" * 64),
        ("lineage", "from_signal_id", "sig_" + "b" * 64),
        ("evidence", "evidence_id", "ev_" + "c" * 64),
        ("membership", "member_id", "mem_" + "d" * 64),
        (
            "predictions",
            "prediction_id",
            "pred_" + hashlib.sha256(b"prediction|2026-08-26").hexdigest(),
        ),
        ("outcomes", "outcome_id", "out_" + hashlib.sha256(b"outcome|2026-08-26").hexdigest()),
    )
    for table, field, original in original_ids:
        assert rows[table][0][field] != original
        changed = module.fixture_rows(RUN_DATE)
        changed[table][0][field] = original
        with pytest.raises(module.FixtureSafetyRefusal):
            module.validate_fixture_contract(
                RUN_DATE, DATASET, changed, module.fixture_rule(RUN_DATE)
            )


def test_dry_run_never_constructs_a_bigquery_client_and_reports_zero_writes() -> None:
    module = _module()
    boundary = _PersistenceBoundary()

    def forbidden_client_factory(*args: object, **kwargs: object) -> object:
        raise AssertionError("dry run must not construct a BigQuery client")

    result = module.run_canary(
        RUN_DATE,
        apply=False,
        persistence_api=boundary,
        client_factory=forbidden_client_factory,
        source_sha="source-sha",
    )

    assert result["first_counts"] == {table: {"inserted": 0, "unchanged": 0} for table in TABLES}
    assert result["second_counts"] == {table: {"inserted": 0, "unchanged": 0} for table in TABLES}
    assert result["conflict_proof"] == {"count": 0, "raised": False}
    assert len(boundary.calls) == 1
    assert isinstance(boundary.calls[0]["client"], persistence.PersistenceTarget)
    assert boundary.calls[0]["dry_run"] is True


def test_apply_calls_the_accepted_boundary_in_order_and_proves_idempotency_and_conflict() -> None:
    module = _module()
    boundary = _PersistenceBoundary()
    client = _Client()
    factory_calls: list[dict[str, object]] = []

    def client_factory(**kwargs: object) -> _Client:
        factory_calls.append(kwargs)
        return client

    result = module.run_canary(
        RUN_DATE,
        apply=True,
        persistence_api=boundary,
        client_factory=client_factory,
        source_sha="source-sha",
    )

    assert factory_calls == [{"project": PROJECT, "location": "US"}]
    assert [call["dry_run"] for call in boundary.calls] == [True, False, False, True, False]
    assert all(call["project"] == PROJECT and call["dataset"] == DATASET for call in boundary.calls)
    assert result["first_counts"] == {table: {"inserted": 1, "unchanged": 0} for table in TABLES}
    assert result["second_counts"] == {table: {"inserted": 0, "unchanged": 1} for table in TABLES}
    assert result["conflict_proof"] == {"count": 1, "raised": True}
    assert result["cleanup_proof"] == {
        "prefixes": [f"_oi_{ALTERED_DIGESTS[table][:12]}_" for table in TABLES],
        "remaining": [],
    }
    assert client.listed_datasets == [f"{PROJECT}.{DATASET}"] * 3


def test_apply_refuses_a_temporary_table_under_a_canary_digest_prefix() -> None:
    module = _module()
    boundary = _PersistenceBoundary()
    client = _Client((f"_oi_{DIGESTS['candidates'][:12]}_leaked_signal_candidates_v2",))

    with pytest.raises(module.CleanupRefusal, match="temporary table"):
        module.run_canary(
            RUN_DATE,
            apply=True,
            persistence_api=boundary,
            client_factory=lambda **_: client,
            source_sha="source-sha",
        )


def test_apply_accepts_a_preexisting_idempotent_first_batch() -> None:
    module = _module()
    boundary = _FirstUnchangedBoundary()

    result = module.run_canary(
        RUN_DATE,
        apply=True,
        persistence_api=boundary,
        client_factory=lambda **_: _Client(),
        source_sha="source-sha",
    )

    assert result["first_counts"] == {table: {"inserted": 0, "unchanged": 1} for table in TABLES}


@pytest.mark.parametrize(
    ("boundary", "message"),
    [
        (_PersistenceBoundary(write_cleanup_state="failed"), "cleanup"),
        (
            _PersistenceBoundary(
                write_digests={**DIGESTS, "candidates": "f" * 64},
            ),
            "digest",
        ),
        (_PersistenceBoundary(conflict_digests=DIGESTS), "altered candidate digest"),
        (
            _PersistenceBoundary(
                conflict_digests={**ALTERED_DIGESTS, "evidence": "e" * 64},
            ),
            "altered later-table digest",
        ),
        (
            _PersistenceBoundary(
                conflict_counts={
                    **dict.fromkeys(TABLES, 0),
                    "candidates": 1,
                    "evidence": 1,
                },
            ),
            "later table",
        ),
        (
            _PersistenceBoundary(
                conflict_unchanged_counts={
                    **dict.fromkeys(TABLES, 0),
                    "evidence": 1,
                },
            ),
            "later table",
        ),
    ],
)
def test_apply_refuses_untrusted_write_receipts(boundary, message: str) -> None:
    module = _module()

    with pytest.raises(module.FixtureSafetyRefusal, match=message):
        module.run_canary(
            RUN_DATE,
            apply=True,
            persistence_api=boundary,
            client_factory=lambda **_: _Client(),
            source_sha="source-sha",
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows, rule: rows["candidates"][0].__setitem__("client_scope_id", "other"), "scope"),
        (
            lambda rows, rule: rows["candidates"][0].__setitem__("audience_lens_ids", ["gen_z"]),
            "audience",
        ),
        (
            lambda rows, rule: rows["evidence"][0].__setitem__("url", "https://example.com/live"),
            "URL",
        ),
        (
            lambda rows, rule: rows["evidence"][0].__setitem__(
                "vendor_family", "staging_fixture_source"
            ),
            "vendor family",
        ),
        (
            lambda rows, rule: rows["evidence"][0].__setitem__("channel_family", "wrong_channel"),
            "channel family",
        ),
        (
            lambda rows, rule: rows["evidence"][0].pop("vendor_family"),
            "fields",
        ),
        (
            lambda rows, rule: rows["membership"][0].__setitem__(
                "vendor_families", ["wrong_vendor"]
            ),
            "membership vendor family",
        ),
        (
            lambda rows, rule: rows["membership"][0].__setitem__(
                "channel_families", ["wrong_channel"]
            ),
            "membership channel family",
        ),
        (
            lambda rows, rule: rows["predictions"][0].__setitem__("display_eligible", True),
            "display",
        ),
        (
            lambda rows, rule: rows["outcomes"][0].__setitem__("observed_velocity", 0.2),
            "observed",
        ),
        (
            lambda rows, rule: rows["outcomes"][0].__setitem__("observed_breadth", 0.3),
            "observed",
        ),
        (
            lambda rows, rule: rows["outcomes"][0].__setitem__("observed_evidence_family_count", 2),
            "observed",
        ),
        (
            lambda rows, rule: rows["candidates"][0].__setitem__("discovery_mode", "dynamic"),
            "discovery",
        ),
        (lambda rows, rule: setattr(rule, "approved_by", "real_approver"), "approver"),
        (lambda rows, rule: setattr(rule, "replay_receipt_id", "receipt_001"), "receipt"),
        (
            lambda rows, rule: rows["evidence"][0].__setitem__("signal_date", date(2026, 8, 25)),
            "date",
        ),
        (
            lambda rows, rule: rows["candidates"][0].__setitem__("signal_id", "signal-invalid"),
            "signal_id",
        ),
        (lambda rows, rule: rows["candidates"].append(dict(rows["candidates"][0])), "row count"),
    ],
)
def test_safety_validation_refuses_every_out_of_contract_fixture_mutation(
    mutation, message: str
) -> None:
    module = _module()
    rows = module.fixture_rows(RUN_DATE)
    rule = module.fixture_rule(RUN_DATE)
    mutation(rows, rule)

    with pytest.raises(module.FixtureSafetyRefusal, match=message):
        module.validate_fixture_contract(RUN_DATE, DATASET, rows, rule)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda rows: rows["candidates"][0].pop("label_member_identity"),
            "candidates fields",
        ),
        (
            lambda rows: rows["candidates"][0].__setitem__(
                "label_member_identity", "ng|keyword|staging_fixture_canary"
            ),
            "label member identity",
        ),
        (
            lambda rows: rows["membership"][0].__setitem__(
                "member_identity", "za|hashtag|staging_fixture_canary"
            ),
            "membership identity",
        ),
        (
            lambda rows: rows["membership"][0].__setitem__(
                "canonical_value", "different_fixture_value"
            ),
            "membership identity facts",
        ),
        (
            lambda rows: rows["membership"].append(dict(rows["membership"][0])),
            "membership row count",
        ),
        (
            lambda rows: rows["candidates"][0].__setitem__("label", "different_fixture_value"),
            "candidate label",
        ),
    ],
)
def test_canary_refuses_every_label_authority_mutation(mutation, message):
    module = _module()
    rows = module.fixture_rows(RUN_DATE)
    mutation(rows)
    with pytest.raises(module.FixtureSafetyRefusal, match=message):
        module.validate_fixture_contract(RUN_DATE, DATASET, rows, module.fixture_rule(RUN_DATE))


def test_safety_validation_refuses_any_dataset_other_than_main_staging() -> None:
    module = _module()

    with pytest.raises(module.FixtureSafetyRefusal, match="dataset"):
        module.validate_fixture_contract(
            RUN_DATE,
            "trends_v2_staging_qa",
            module.fixture_rows(RUN_DATE),
            module.fixture_rule(RUN_DATE),
        )


def test_json_output_contains_the_source_sha_and_compact_canary_proof() -> None:
    module = _module()
    rendered = module.render_result(
        {
            "date": "2026-08-26",
            "run_id": RUN_ID,
            "first_counts": {table: {"inserted": 0, "unchanged": 0} for table in TABLES},
            "second_counts": {table: {"inserted": 0, "unchanged": 0} for table in TABLES},
            "conflict_proof": {"count": 0, "raised": False},
            "statement_digests": DIGESTS,
            "cleanup_proof": {"prefixes": [], "remaining": []},
            "source_sha": "source-sha",
        }
    )

    assert json.loads(rendered)["source_sha"] == "source-sha"
    assert "\n" not in rendered
