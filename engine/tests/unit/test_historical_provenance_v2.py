"""Contract tests for the Historical Evidence provenance binding boundary."""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROW_FIELDS = (
    "contract_version",
    "run_id",
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "signal_date",
    "market",
    "signal_id",
    "evidence_id",
    "row_id",
    "source_family",
    "platform",
    "source_label",
    "author_label",
    "excerpt",
    "metric_label",
    "url",
    "published_at",
    "claim_role",
    "direction",
    "geo_confidence",
    "evidence_state",
    "availability",
    "created_at",
)
EVIDENCE_ID = "ev_" + "1" * 64
SIGNAL_ID = "sig_" + "a" * 64


def _canonical_contract_value(value):
    if is_dataclass(value):
        return _canonical_contract_value(asdict(value))
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_canonical_contract_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_contract_value(item) for key, item in value.items()}
    return value


def _contract_digest(prefix, value):
    encoded = json.dumps(
        _canonical_contract_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()


def frame(**overrides):
    from src.analysis.open_intelligence.historical_evidence_bridge import HistoricalBridgeFrame

    values = {
        "bridge_version": "historical_evidence_bridge_v1",
        "investigation_id": "inv_fixture",
        "client_scope_id": "fixture_scope",
        "market_scope": ("za",),
        "brand_config_id": "fixture_brand",
        "audience_lens_ids": (),
        "theme_id": "fixture_theme",
        "run_id": "run_fixture",
        "contract_version": "2.1.0",
        "output_mode": "internal_working_paper",
        "as_of": datetime(2026, 8, 28, tzinfo=UTC),
    }
    values.update(overrides)
    return HistoricalBridgeFrame(**values)


def row(**overrides):
    values = {
        "contract_version": "2.1.0",
        "run_id": "run_fixture",
        "client_scope_id": "fixture_scope",
        "market_scope": ("za",),
        "brand_config_id": "fixture_brand",
        "audience_lens_ids": (),
        "theme_id": "fixture_theme",
        "signal_date": date(2026, 8, 20),
        "market": "za",
        "signal_id": SIGNAL_ID,
        "evidence_id": EVIDENCE_ID,
        "row_id": "fixture_row_001",
        "source_family": "reddit",
        "platform": "reddit",
        "source_label": "Fixture forum",
        "author_label": "@fixture_author",
        "excerpt": "Sanitized fixture evidence.",
        "metric_label": "1 fixture observation",
        "url": "https://evidence.invalid/fixture/1",
        "published_at": datetime(2026, 8, 20, 8, tzinfo=UTC),
        "claim_role": "context",
        "direction": "rising",
        "geo_confidence": 0.9,
        "evidence_state": "ready",
        "availability": "available",
        "created_at": datetime(2026, 8, 20, 8, 5, tzinfo=UTC),
    }
    values.update(overrides)
    return values


def schema():
    from src.analysis.open_intelligence.historical_evidence_reader import VIEW_SCHEMA

    return tuple(SimpleNamespace(**item) for item in VIEW_SCHEMA)


class FakeJob:
    def __init__(self, rows):
        self.schema = schema()
        self._rows = rows

    def result(self, max_results=None):
        assert isinstance(max_results, int)
        assert max_results > 0
        return tuple(self._rows)


class FakeClient:
    project = "ogilvy-trends-v2"
    location = "US"
    credentials = SimpleNamespace(
        service_account_email="trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
    )

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def query(self, sql, *, job_config, location):
        self.calls.append((sql, job_config, location))
        return FakeJob(self.rows)


def analogue_source():
    from src.analysis.open_intelligence.historical_analogue import (
        AnalogueReceipt,
        HistoricalSignalSnapshot,
    )

    receipt = AnalogueReceipt(
        receipt_id=EVIDENCE_ID,
        signal_id=SIGNAL_ID,
        published_at=datetime(2026, 8, 20, 8, tzinfo=UTC),
        source_family="reddit",
        market="za",
    )
    candidate = HistoricalSignalSnapshot(
        signal_id=SIGNAL_ID,
        as_of=datetime(2026, 8, 20, 12, tzinfo=UTC),
        market="za",
        terms=("fixture",),
        source_families=("reddit",),
        trajectory_signature=("rising", "rising"),
        evidence_state="ready",
        receipts=(receipt,),
    )
    current = HistoricalSignalSnapshot(
        signal_id="sig_" + "b" * 64,
        as_of=datetime(2026, 8, 28, tzinfo=UTC),
        market="za",
        terms=("fixture",),
        source_families=("reddit",),
        trajectory_signature=("rising", "rising"),
        evidence_state="ready",
        receipts=(),
    )
    return current, candidate


def test_exact_row_schema_and_canonical_literal_ids():
    from src.analysis.open_intelligence.historical_evidence_reader import (
        VIEW_SCHEMA_DIGEST,
        PersistedSignalEvidenceRow,
        natural_key_digest,
        source_projection_digest,
    )

    value = PersistedSignalEvidenceRow(**row())
    assert tuple(item.name for item in fields(value)) == ROW_FIELDS
    assert (
        VIEW_SCHEMA_DIGEST == "pse_b41555fd47cb2447afa5e7fd85c587814f719e9c44da91a28faea7869f30587c"
    )
    assert (
        natural_key_digest(value)
        == "sen_631968d9547832b0455fb0e0a632124d42a8bef8856db4b51378a37db5372447"
    )
    assert (
        source_projection_digest((value,))
        == "hsp_a5438048932644f5033b765dea8fb244b095d51860558e568dc8af7badad892b"
    )


def test_source_projection_sorts_by_natural_key_values_not_key_digest():
    import hashlib

    from src.analysis.open_intelligence.historical_evidence_reader import (
        PersistedSignalEvidenceRow,
        source_projection_digest,
    )
    from src.analysis.open_intelligence.historical_provenance import canonical_json_bytes

    first = PersistedSignalEvidenceRow(**row())
    second = PersistedSignalEvidenceRow(
        **row(evidence_id="ev_" + "2" * 64, row_id="fixture_row_002")
    )
    expected = (
        "hsp_"
        + hashlib.sha256(
            canonical_json_bytes(
                [[getattr(item, field.name) for field in fields(item)] for item in (first, second)]
            )
        ).hexdigest()
    )
    assert source_projection_digest((second, first)) == expected


def test_runtime_reader_has_fixed_signature_query_target_and_projection(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader

    current, candidate = analogue_source()
    client = FakeClient((row(),))
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: client)
    monkeypatch.setattr(reader, "_utc_now", lambda: datetime(2026, 8, 28, 8, tzinfo=UTC))
    result = reader.read_runtime_analogue_evidence_projection(
        frame=frame(), current=current, candidates=(candidate,)
    )
    parameters = inspect.signature(reader.read_runtime_analogue_evidence_projection).parameters
    assert tuple(parameters) == ("frame", "current", "candidates")
    sql, config, location = client.calls[0]
    assert "`ogilvy-trends-v2.trends_v2_staging.v_signal_evidence_v2`" in sql
    assert "SELECT *" not in sql
    assert all(name in sql for name in ROW_FIELDS)
    assert all(
        token in sql
        for token in (
            "@client_scope_id",
            "@run_id",
            "@contract_version",
            "@market",
            "@evidence_ids",
            "@as_of",
            "published_at IS NOT NULL",
            "published_at <= @as_of",
            "created_at <= @as_of",
            "signal_date <= DATE(@as_of)",
        )
    )
    assert "LIMIT 2" in sql
    assert location == "US"
    assert len(config.query_parameters) == 6
    receipt, snapshot = reader.validate_runtime_projection_read(result)
    assert receipt.reader_mode == "runtime_view"
    assert receipt.row_count == 1
    assert snapshot.evidence_refs[0].evidence_id == EVIDENCE_ID
    assert snapshot.source_projection_digest == receipt.source_projection_digest


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"contract_version": "2.0.0"}, "historical_evidence_reader_scope_mismatch"),
        ({"market_scope": ("ng",)}, "historical_evidence_reader_scope_mismatch"),
        ({"market": "ng"}, "historical_evidence_reader_scope_mismatch"),
        (
            {"published_at": datetime(2026, 8, 29, tzinfo=UTC)},
            "historical_evidence_reader_future_leak",
        ),
        (
            {"created_at": datetime(2026, 8, 29, tzinfo=UTC)},
            "historical_evidence_reader_future_leak",
        ),
        ({"signal_date": date(2026, 8, 29)}, "historical_evidence_reader_future_leak"),
        ({"claim_role": "invented"}, "historical_evidence_reader_semantics_invalid"),
        ({"geo_confidence": 2.0}, "historical_evidence_reader_semantics_invalid"),
        ({"source_family": "youtube"}, "historical_evidence_reader_semantics_invalid"),
    ],
)
def test_runtime_reader_fails_closed_on_row_mutations(monkeypatch, mutation, code):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    current, candidate = analogue_source()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient((row(**mutation),)))
    with pytest.raises(HistoricalBridgeError) as caught:
        reader.read_runtime_analogue_evidence_projection(
            frame=frame(), current=current, candidates=(candidate,)
        )
    assert caught.value.code == code


def test_missing_extra_duplicate_and_schema_drift_fail(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    current, candidate = analogue_source()
    cases = [
        ((), "historical_evidence_reader_incomplete"),
        ((row(), row(evidence_id="ev_" + "2" * 64)), "historical_evidence_reader_limit_exceeded"),
        ((row(), row()), "historical_evidence_reader_limit_exceeded"),
    ]
    for rows, code in cases:
        monkeypatch.setattr(reader, "_create_runtime_client", lambda rows=rows: FakeClient(rows))
        with pytest.raises(HistoricalBridgeError) as caught:
            reader.read_runtime_analogue_evidence_projection(
                frame=frame(), current=current, candidates=(candidate,)
            )
        assert caught.value.code == code

    client = FakeClient((row(),))
    bad = list(schema())
    bad[-1] = SimpleNamespace(name="changed", field_type="STRING", mode="NULLABLE")

    class BadJob(FakeJob):
        schema = bad

    client.query = lambda *args, **kwargs: SimpleNamespace(
        schema=bad, result=lambda max_results=None: (row(),)
    )
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: client)
    with pytest.raises(HistoricalBridgeError) as caught:
        reader.read_runtime_analogue_evidence_projection(
            frame=frame(), current=current, candidates=(candidate,)
        )
    assert caught.value.code == "historical_evidence_reader_schema_mismatch"


def test_fixture_read_is_distinct_and_runtime_ineligible():
    from src.analysis.open_intelligence.historical_evidence_fixture_reader import (
        FixtureHistoricalEvidenceProjectionRead,
        read_fixture_historical_evidence_projection,
    )
    from src.analysis.open_intelligence.historical_evidence_reader import (
        RuntimeHistoricalEvidenceProjectionRead,
        validate_runtime_projection_read,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    fixture = Path("tests/fixtures/open_intelligence/historical_evidence_provenance_v2.json")
    digest = __import__("hashlib").sha256(fixture.read_bytes()).hexdigest()
    value = read_fixture_historical_evidence_projection(
        frame=frame(),
        fixture_id="historical_evidence_provenance_v2",
        expected_fixture_digest=digest,
    )
    assert type(value) is FixtureHistoricalEvidenceProjectionRead
    assert type(value) is not RuntimeHistoricalEvidenceProjectionRead
    with pytest.raises(HistoricalBridgeError) as caught:
        validate_runtime_projection_read(value)
    assert caught.value.code == "historical_fixture_projection_runtime_ineligible"


def test_provenance_leaf_is_acyclic_and_engine_owns_no_claim_attachment():
    root = Path("src/analysis/open_intelligence")
    tree = ast.parse((root / "historical_provenance.py").read_text(encoding="utf-8"))
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any("open_intelligence" in name for name in imports)
    bridge = (root / "historical_evidence_bridge.py").read_text(encoding="utf-8")
    assert all(
        token not in bridge
        for token in (
            "InvestigationFrame",
            "ClaimRelationshipDecision",
            "attach_historical_evidence",
            "approve_claim_relationship",
            "claim_digest",
            "supporting_evidence_ids",
            "opposing_evidence_ids",
            "source_yield",
            "outcome",
            "readiness",
            "decision_strength",
        )
    )


def test_binding_literals_and_stale_source_change_all_ids():
    from src.analysis.open_intelligence.historical_provenance import (
        build_recurrence_binding,
        provenance_bindings_digest,
    )
    from src.analysis.open_intelligence.historical_recurrence import RecurrenceOccurrence

    occurrence = RecurrenceOccurrence(
        occurrence_id="occurrence_001",
        pattern_id="pattern_fixture",
        occurred_on=date(2026, 8, 20),
        market="za",
        receipt_ids=(EVIDENCE_ID,),
    )
    binding = build_recurrence_binding(
        occurrence=occurrence,
        receipt_id=EVIDENCE_ID,
        expected_signal_id=SIGNAL_ID,
        expected_source_family="reddit",
        rule_version="recurrence_rules_v1",
    )
    assert (
        binding.source_object_digest
        == "hso_1914e29287a265650c38cc43c3982fc2159377c2ce03c2eefc7d1d20ccc85388"
    )
    assert (
        binding.binding_id == "hpb_ddf40988531707914ef2aa2a81da7269165b93433433f24095f0eb493ebfcf42"
    )
    assert (
        provenance_bindings_digest((binding,))
        == "hbd_4c6926a20abbb93e5d06d00024dbfc86ef16c7786d0d95c6026b4e621efe53d8"
    )
    changed = build_recurrence_binding(
        occurrence=replace(occurrence, occurred_on=date(2026, 8, 19)),
        receipt_id=EVIDENCE_ID,
        expected_signal_id=SIGNAL_ID,
        expected_source_family="reddit",
        rule_version="recurrence_rules_v1",
    )
    assert changed.source_object_digest != binding.source_object_digest
    assert changed.binding_id != binding.binding_id


def test_all_runtime_surfaces_reject_caller_controlled_inputs():
    from src.analysis.open_intelligence import historical_diffusion as diffusion
    from src.analysis.open_intelligence import historical_evidence_bridge as bridge
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence import historical_recurrence as recurrence

    forbidden = {
        "client",
        "client_factory",
        "project",
        "dataset",
        "view",
        "location",
        "rows",
        "mapping",
        "evidence_index",
        "evidence_refs",
        "bindings",
        "override",
        "target",
    }
    functions = (
        reader.read_runtime_analogue_evidence_projection,
        reader.read_runtime_recurrence_evidence_projection,
        reader.read_runtime_diffusion_evidence_projection,
        recurrence.detect_historical_recurrence_with_provenance,
        diffusion.forecast_cross_market_diffusion_with_provenance,
        bridge.build_analogue_evidence,
        bridge.build_recurrence_evidence,
        bridge.build_diffusion_shadow,
    )
    for function in functions:
        assert forbidden.isdisjoint(inspect.signature(function).parameters)


def test_fixture_contains_no_lp_decision_or_claim_material():
    payload = json.loads(
        Path("tests/fixtures/open_intelligence/historical_evidence_provenance_v2.json").read_text(
            encoding="utf-8"
        )
    )
    serialized = json.dumps(payload, sort_keys=True)
    assert payload["fixture_version"] == "historical_evidence_provenance_fixture_v2"
    assert len(payload["persisted_rows"]) >= 1
    assert all(
        token not in serialized
        for token in (
            "claim_draft",
            "relationship",
            "decided_by",
            "supporting_evidence_ids",
            "opposing_evidence_ids",
            "attachment_output",
        )
    )


def analogue_rules():
    from src.analysis.open_intelligence.historical_analogue import HistoricalAnalogueRules

    return HistoricalAnalogueRules(
        term_weight=0.4,
        source_weight=0.2,
        trajectory_weight=0.3,
        geography_weight=0.1,
        minimum_similarity=0.5,
        allow_cross_market=False,
        minimum_trajectory_points=2,
        eligible_evidence_states=("ready",),
    )


def _assert_complete_fixture_chain(payload):
    top_level_fields = (
        "fixture_version",
        "contract_version",
        "fixture_id",
        "persisted_rows",
        "projection_read_receipt",
        "projection_snapshot",
        "recurrence_source_object",
        "bindings",
        "provenance_bindings_digest",
        "historical_receipts",
        "issues",
    )
    read_receipt_fields = (
        "projection_read_receipt_id",
        "reader_version",
        "reader_mode",
        "source_project",
        "source_dataset",
        "source_view",
        "source_location",
        "reader_identity",
        "fixture_id",
        "fixture_digest",
        "view_schema_digest",
        "scope_digest",
        "run_id",
        "contract_version",
        "markets",
        "requested_evidence_ids",
        "row_natural_key_digests",
        "row_count",
        "as_of",
        "read_at",
        "source_projection_digest",
    )
    evidence_ref_fields = (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "evidence_id",
        "signal_id",
        "signal_date",
        "market",
        "source_family",
        "published_at",
        "claim_role",
        "direction",
        "geo_confidence",
        "availability",
        "evidence_state",
    )
    snapshot_fields = (
        "projection_receipt_id",
        "projection_owner_version",
        "projection_read_receipt_id",
        "scope_digest",
        "evidence_refs",
        "evidence_refs_digest",
        "source_projection_digest",
    )
    source_object_fields = (
        "occurrence_id",
        "pattern_id",
        "occurred_on",
        "market",
        "receipt_ids",
    )
    binding_fields = (
        "binding_id",
        "binding_version",
        "source_kind",
        "source_object_id",
        "source_object_digest",
        "receipt_id",
        "expected_signal_id",
        "expected_source_family",
        "historical_cutoff",
        "rule_version",
    )
    historical_receipt_fields = (
        "historical_receipt_id",
        "bridge_version",
        "investigation_id",
        "scope_digest",
        "kind",
        "source_object_id",
        "source_object_state",
        "as_of",
        "source_market",
        "target_market",
        "source_families",
        "evidence_ids",
        "evidence_refs_digest",
        "evidence_projection_receipt_id",
        "source_projection_digest",
        "provenance_binding_ids",
        "provenance_bindings_digest",
        "observed_window_start",
        "observed_window_end",
        "historical_window_start",
        "historical_window_end",
        "difference_reasons",
        "limitations",
        "rule_version",
        "future_leak_state",
        "future_leak_reasons",
        "attachment_eligible",
        "display_eligible",
    )
    natural_key_fields = (
        "client_scope_id",
        "signal_date",
        "market",
        "signal_id",
        "evidence_id",
        "run_id",
    )
    scope_fields = (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
    )

    assert tuple(payload) == top_level_fields
    assert payload["fixture_version"] == "historical_evidence_provenance_fixture_v2"
    assert payload["contract_version"] == "2.1.0"
    assert payload["fixture_id"] == "historical_evidence_provenance_v2"
    rows = payload["persisted_rows"]
    assert rows
    assert all(tuple(item) == ROW_FIELDS for item in rows)
    assert rows == [_canonical_contract_value(row())]
    ordered_rows = sorted(rows, key=lambda item: tuple(item[field] for field in natural_key_fields))
    natural_keys = [
        _contract_digest("sen_", {field: item[field] for field in natural_key_fields})
        for item in ordered_rows
    ]
    source_projection_digest = _contract_digest(
        "hsp_", [[item[field] for field in ROW_FIELDS] for item in ordered_rows]
    )
    first_scope = {field: ordered_rows[0][field] for field in scope_fields}
    assert all(
        {field: item[field] for field in scope_fields} == first_scope for item in ordered_rows
    )
    scope_digest = _contract_digest("scp_", first_scope)

    read_receipt = payload["projection_read_receipt"]
    assert tuple(read_receipt) == read_receipt_fields
    assert read_receipt["reader_version"] == "historical_evidence_projection_reader_v1"
    assert read_receipt["reader_mode"] == "runtime_view"
    assert read_receipt["source_project"] == "ogilvy-trends-v2"
    assert read_receipt["source_dataset"] == "trends_v2_staging"
    assert read_receipt["source_view"] == "v_signal_evidence_v2"
    assert read_receipt["source_location"] == "US"
    assert read_receipt["reader_identity"] == (
        "trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert read_receipt["fixture_id"] is None
    assert read_receipt["fixture_digest"] is None
    assert read_receipt["view_schema_digest"] == (
        "pse_b41555fd47cb2447afa5e7fd85c587814f719e9c44da91a28faea7869f30587c"
    )
    assert read_receipt["scope_digest"] == scope_digest
    assert read_receipt["run_id"] == first_scope["run_id"]
    assert read_receipt["contract_version"] == first_scope["contract_version"]
    assert read_receipt["markets"] == sorted({item["market"] for item in ordered_rows})
    assert read_receipt["requested_evidence_ids"] == sorted(
        item["evidence_id"] for item in ordered_rows
    )
    assert read_receipt["row_natural_key_digests"] == natural_keys
    assert read_receipt["row_count"] == len(ordered_rows)
    assert read_receipt["as_of"] == "2026-08-28T00:00:00.000000Z"
    assert read_receipt["read_at"] == "2026-08-28T08:00:00.000000Z"
    assert read_receipt["source_projection_digest"] == source_projection_digest
    read_preimage = {
        field: read_receipt[field]
        for field in read_receipt_fields
        if field != "projection_read_receipt_id"
    }
    assert read_receipt["projection_read_receipt_id"] == _contract_digest("hrr_", read_preimage)

    expected_refs = [
        {field: item[field] for field in evidence_ref_fields}
        for item in sorted(ordered_rows, key=lambda item: item["evidence_id"])
    ]
    snapshot = payload["projection_snapshot"]
    assert tuple(snapshot) == snapshot_fields
    assert snapshot["projection_owner_version"] == "historical_evidence_projection_v1"
    assert snapshot["projection_read_receipt_id"] == read_receipt["projection_read_receipt_id"]
    assert snapshot["scope_digest"] == scope_digest
    assert snapshot["evidence_refs"] == expected_refs
    assert snapshot["evidence_refs_digest"] == _contract_digest("evr_", expected_refs)
    assert snapshot["source_projection_digest"] == source_projection_digest
    snapshot_preimage = {
        field: snapshot[field] for field in snapshot_fields if field != "projection_receipt_id"
    }
    assert snapshot["projection_receipt_id"] == _contract_digest("hpr_", snapshot_preimage)

    source_object = payload["recurrence_source_object"]
    assert tuple(source_object) == source_object_fields
    assert source_object == {
        "occurrence_id": "occurrence_001",
        "pattern_id": "pattern_fixture",
        "occurred_on": "2026-08-20",
        "market": "za",
        "receipt_ids": [EVIDENCE_ID],
    }
    source_digest = _contract_digest("hso_", source_object)
    refs_by_id = {item["evidence_id"]: item for item in expected_refs}
    bindings = sorted(
        payload["bindings"],
        key=lambda item: (item["source_kind"], item["source_object_id"], item["receipt_id"]),
    )
    assert all(tuple(item) == binding_fields for item in bindings)
    assert [item["receipt_id"] for item in bindings] == sorted(source_object["receipt_ids"])
    for item in bindings:
        assert item["binding_version"] == "historical_evidence_provenance_binding_v1"
        assert item["source_kind"] == "recurrence"
        assert item["source_object_id"] == source_object["occurrence_id"]
        assert item["source_object_digest"] == source_digest
        assert item["expected_signal_id"] == refs_by_id[item["receipt_id"]]["signal_id"]
        assert item["expected_source_family"] == refs_by_id[item["receipt_id"]]["source_family"]
        assert item["historical_cutoff"] == source_object["occurred_on"]
        binding_preimage = {field: item[field] for field in binding_fields if field != "binding_id"}
        assert item["binding_id"] == _contract_digest("hpb_", binding_preimage)
    bindings_digest = _contract_digest("hbd_", bindings)
    assert payload["provenance_bindings_digest"] == bindings_digest

    historical_receipts = payload["historical_receipts"]
    assert len(historical_receipts) == 1
    historical_receipt = historical_receipts[0]
    assert tuple(historical_receipt) == historical_receipt_fields
    selected_refs = [refs_by_id[item] for item in historical_receipt["evidence_ids"]]
    assert historical_receipt["bridge_version"] == "historical_evidence_bridge_v1"
    assert historical_receipt["investigation_id"] == "inv_fixture"
    assert historical_receipt["scope_digest"] == scope_digest
    assert historical_receipt["kind"] == "recurrence"
    assert historical_receipt["source_object_id"] == source_object["pattern_id"]
    assert historical_receipt["source_object_state"] == "recurrent"
    assert historical_receipt["as_of"] == read_receipt["as_of"]
    assert historical_receipt["source_market"] == source_object["market"]
    assert historical_receipt["target_market"] == source_object["market"]
    assert historical_receipt["source_families"] == sorted(
        {item["source_family"] for item in selected_refs}
    )
    assert historical_receipt["evidence_refs_digest"] == _contract_digest("evr_", selected_refs)
    assert historical_receipt["evidence_projection_receipt_id"] == snapshot["projection_receipt_id"]
    assert historical_receipt["source_projection_digest"] == source_projection_digest
    assert historical_receipt["provenance_binding_ids"] == [item["binding_id"] for item in bindings]
    assert historical_receipt["provenance_bindings_digest"] == bindings_digest
    published = sorted(item["published_at"] for item in selected_refs)
    assert historical_receipt["observed_window_start"] == published[0]
    assert historical_receipt["observed_window_end"] == published[-1]
    assert historical_receipt["historical_window_start"] == source_object["occurred_on"]
    assert historical_receipt["historical_window_end"] == source_object["occurred_on"]
    assert historical_receipt["difference_reasons"] == ["bounded_intervals"]
    assert historical_receipt["limitations"] == [
        "recurrence_does_not_establish_cause",
        "recurrence_is_not_a_forecast",
    ]
    assert historical_receipt["rule_version"] == bindings[0]["rule_version"]
    assert historical_receipt["future_leak_state"] == "passed"
    assert historical_receipt["future_leak_reasons"] == []
    assert historical_receipt["attachment_eligible"] is True
    assert historical_receipt["display_eligible"] is False
    receipt_preimage = {
        field: historical_receipt[field]
        for field in historical_receipt_fields
        if field != "historical_receipt_id"
    }
    assert historical_receipt["historical_receipt_id"] == _contract_digest("heb_", receipt_preimage)
    assert payload["issues"] == []


def _provenance_fixture_payload():
    return json.loads(
        Path("tests/fixtures/open_intelligence/historical_evidence_provenance_v2.json").read_text(
            encoding="utf-8"
        )
    )


def test_fixture_rederives_the_complete_canonical_chain():
    _assert_complete_fixture_chain(_provenance_fixture_payload())


def test_fixture_rederivation_rejects_rehashed_source_market_mutation():
    payload = copy.deepcopy(_provenance_fixture_payload())
    receipt = payload["historical_receipts"][0]
    receipt["source_market"] = "ng"
    receipt["historical_receipt_id"] = _contract_digest(
        "heb_", {key: value for key, value in receipt.items() if key != "historical_receipt_id"}
    )

    with pytest.raises(AssertionError):
        _assert_complete_fixture_chain(payload)


def test_analogue_bridge_binds_projection_and_selected_reference_digest(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_analogue import (
        AnalogueReceipt,
        HistoricalSignalSnapshot,
    )
    from src.analysis.open_intelligence.historical_evidence_bridge import build_analogue_evidence
    from src.analysis.open_intelligence.historical_provenance import (
        EMPTY_BINDINGS_DIGEST,
        evidence_refs_digest,
    )

    current_receipt = AnalogueReceipt(
        receipt_id="ev_" + "2" * 64,
        signal_id="sig_" + "b" * 64,
        published_at=datetime(2026, 8, 27, 8, tzinfo=UTC),
        source_family="reddit",
        market="za",
    )
    _, candidate = analogue_source()
    current = HistoricalSignalSnapshot(
        signal_id=current_receipt.signal_id,
        as_of=frame().as_of,
        market="za",
        terms=("fixture",),
        source_families=("reddit",),
        trajectory_signature=("rising", "rising"),
        evidence_state="ready",
        receipts=(current_receipt,),
    )
    rows = (
        row(),
        row(
            evidence_id=current_receipt.receipt_id,
            signal_id=current.signal_id,
            row_id="fixture_row_002",
            signal_date=date(2026, 8, 27),
            published_at=current_receipt.published_at,
            created_at=datetime(2026, 8, 27, 8, 5, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_analogue_evidence_projection(
        frame=frame(), current=current, candidates=(candidate,)
    )
    result = build_analogue_evidence(
        frame=frame(),
        current=current,
        candidates=(candidate,),
        rules=analogue_rules(),
        evidence_projection=projection,
        limit=1,
    )
    assert result.projection_read_receipt == projection.receipt
    assert result.evidence_projection == projection.snapshot
    assert result.bindings == ()
    assert len(result.receipts) == 1
    receipt = result.receipts[0]
    selected = tuple(
        ref for ref in projection.snapshot.evidence_refs if ref.evidence_id == EVIDENCE_ID
    )
    assert receipt.evidence_refs_digest == evidence_refs_digest(selected)
    assert receipt.provenance_binding_ids == ()
    assert receipt.provenance_bindings_digest == EMPTY_BINDINGS_DIGEST


def test_analogue_bridge_rejects_candidate_at_current_time_as_future_leak(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_analogue import (
        AnalogueReceipt,
        HistoricalSignalSnapshot,
    )
    from src.analysis.open_intelligence.historical_evidence_bridge import build_analogue_evidence
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    scoped_frame = frame()
    _, candidate = analogue_source()
    candidate = replace(candidate, as_of=scoped_frame.as_of)
    current_receipt = AnalogueReceipt(
        receipt_id="ev_" + "2" * 64,
        signal_id="sig_" + "b" * 64,
        published_at=datetime(2026, 8, 27, 8, tzinfo=UTC),
        source_family="reddit",
        market="za",
    )
    current = HistoricalSignalSnapshot(
        signal_id=current_receipt.signal_id,
        as_of=scoped_frame.as_of,
        market="za",
        terms=("fixture",),
        source_families=("reddit",),
        trajectory_signature=("rising", "rising"),
        evidence_state="ready",
        receipts=(current_receipt,),
    )
    rows = (
        row(),
        row(
            evidence_id=current_receipt.receipt_id,
            signal_id=current.signal_id,
            row_id="fixture_row_002",
            signal_date=date(2026, 8, 27),
            published_at=current_receipt.published_at,
            created_at=datetime(2026, 8, 27, 8, 5, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_analogue_evidence_projection(
        frame=scoped_frame, current=current, candidates=(candidate,)
    )

    with pytest.raises(HistoricalBridgeError) as caught:
        build_analogue_evidence(
            frame=scoped_frame,
            current=current,
            candidates=(candidate,),
            rules=analogue_rules(),
            evidence_projection=projection,
            limit=1,
        )

    assert caught.value.code == "historical_future_leak"
    assert caught.value.source_object_ids == (candidate.signal_id,)
    assert caught.value.evidence_ids == (candidate.receipts[0].receipt_id,)
    assert caught.value.reasons == ("candidate_not_prior",)


def recurrence_sources():
    from src.analysis.open_intelligence.historical_recurrence import RecurrenceOccurrence

    occurrences = tuple(
        RecurrenceOccurrence(
            occurrence_id=f"occurrence_{index:03d}",
            pattern_id="pattern_fixture",
            occurred_on=date(2026, 8, day),
            market="za",
            receipt_ids=(f"ev_{index:064x}",),
        )
        for index, day in enumerate((1, 8, 15, 22), 1)
    )
    rows = tuple(
        row(
            evidence_id=item.receipt_ids[0],
            row_id=f"fixture_row_{index:03d}",
            signal_id=f"sig_{index:064x}",
            signal_date=item.occurred_on,
            published_at=datetime.combine(item.occurred_on, datetime.min.time(), UTC),
            created_at=datetime.combine(item.occurred_on, datetime.min.time(), UTC),
        )
        for index, item in enumerate(occurrences, 1)
    )
    return occurrences, rows


def _recurrence_bridge_result(monkeypatch, *, bridge_frame=None, source_rows=None):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import build_recurrence_evidence
    from src.analysis.open_intelligence.historical_recurrence import RecurrenceRules

    occurrences, default_rows = recurrence_sources()
    bridge_frame = bridge_frame or frame()
    rows = source_rows or default_rows
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_recurrence_evidence_projection(
        frame=bridge_frame,
        occurrences=occurrences,
        pattern_id="pattern_fixture",
        market="za",
    )
    result = build_recurrence_evidence(
        frame=bridge_frame,
        occurrences=occurrences,
        pattern_id="pattern_fixture",
        market="za",
        rules=RecurrenceRules(),
        evidence_projection=projection,
    )
    return result


def _replace_projection_chain(
    result, *, read_receipt=None, evidence_refs=None, source_projection_digest=None
):
    from src.analysis.open_intelligence.historical_evidence_bridge import HistoricalEvidenceReceipt
    from src.analysis.open_intelligence.historical_provenance import (
        HistoricalEvidenceProjectionSnapshot,
    )

    read_receipt = read_receipt or result.projection_read_receipt
    refs = tuple(evidence_refs or result.evidence_projection.evidence_refs)
    source_projection_digest = (
        source_projection_digest or result.evidence_projection.source_projection_digest
    )
    refs_digest = _contract_digest(
        "evr_", [asdict(item) for item in sorted(refs, key=lambda item: item.evidence_id)]
    )
    snapshot_values = {
        "projection_owner_version": result.evidence_projection.projection_owner_version,
        "projection_read_receipt_id": read_receipt.projection_read_receipt_id,
        "scope_digest": result.evidence_projection.scope_digest,
        "evidence_refs": refs,
        "evidence_refs_digest": refs_digest,
        "source_projection_digest": source_projection_digest,
    }
    snapshot = HistoricalEvidenceProjectionSnapshot(
        projection_receipt_id=_contract_digest("hpr_", snapshot_values), **snapshot_values
    )
    receipts = []
    for item in result.receipts:
        values = asdict(item)
        values.pop("historical_receipt_id")
        values["evidence_projection_receipt_id"] = snapshot.projection_receipt_id
        values["source_projection_digest"] = source_projection_digest
        values["evidence_refs_digest"] = _contract_digest(
            "evr_",
            [
                asdict(ref)
                for ref in sorted(
                    (ref for ref in refs if ref.evidence_id in item.evidence_ids),
                    key=lambda ref: ref.evidence_id,
                )
            ],
        )
        receipts.append(
            HistoricalEvidenceReceipt(
                historical_receipt_id=_contract_digest("heb_", values), **values
            )
        )
    forged = replace(
        result,
        projection_read_receipt=read_receipt,
        evidence_projection=snapshot,
        receipts=tuple(receipts),
    )
    if hasattr(result, "_authority"):
        object.__setattr__(forged, "_authority", result._authority)
    return forged


def _drift_read_receipt(result, **updates):
    from src.analysis.open_intelligence.historical_evidence_reader import ProjectionReadReceipt

    values = asdict(result.projection_read_receipt)
    values.pop("projection_read_receipt_id")
    values.update(updates)
    receipt = ProjectionReadReceipt(
        projection_read_receipt_id=_contract_digest("hrr_", values), **values
    )
    return _replace_projection_chain(
        result,
        read_receipt=receipt,
        source_projection_digest=values["source_projection_digest"],
    )


def test_recurrence_wrapper_preserves_algorithm_and_binds_every_receipt(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import build_recurrence_evidence
    from src.analysis.open_intelligence.historical_recurrence import (
        RecurrenceRules,
        detect_historical_recurrence,
        detect_historical_recurrence_with_provenance,
    )

    occurrences, rows = recurrence_sources()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_recurrence_evidence_projection(
        frame=frame(), occurrences=occurrences, pattern_id="pattern_fixture", market="za"
    )
    rules = RecurrenceRules()
    direct = detect_historical_recurrence(
        occurrences=occurrences,
        pattern_id="pattern_fixture",
        market="za",
        as_of=frame().as_of.date(),
        rules=rules,
    )
    wrapped, bindings = detect_historical_recurrence_with_provenance(
        occurrences=occurrences,
        pattern_id="pattern_fixture",
        market="za",
        as_of=frame().as_of.date(),
        rules=rules,
        evidence_projection=projection,
    )
    assert wrapped == direct
    assert len(bindings) == 4
    assert {item.receipt_id for item in bindings} == set(direct.receipt_ids)
    result = build_recurrence_evidence(
        frame=frame(),
        occurrences=occurrences,
        pattern_id="pattern_fixture",
        market="za",
        rules=rules,
        evidence_projection=projection,
    )
    assert result.bindings == bindings
    assert len(result.receipts) == 1
    assert set(result.receipts[0].provenance_binding_ids) == {item.binding_id for item in bindings}


def diffusion_sources():
    from src.analysis.open_intelligence.historical_diffusion import (
        CurrentDiffusionSignal,
        HistoricalDiffusionSequence,
    )

    current = CurrentDiffusionSignal(
        signal_id="sig_" + "f" * 64,
        origin_market="za",
        origin_date=date(2026, 8, 25),
        as_of=date(2026, 8, 28),
        carrier_markets=("za",),
        source_families=("reddit", "youtube"),
    )
    sequences = tuple(
        HistoricalDiffusionSequence(
            sequence_id=f"sequence_{index:03d}",
            origin_market="za",
            origin_date=date(2026, 8, index),
            target_market="ng",
            target_date=date(2026, 8, index + lag),
            receipt_ids=(f"ev_{index:064x}",),
        )
        for index, lag in enumerate((4, 5, 6), 1)
    )
    rows = tuple(
        row(
            market_scope=("za", "ng"),
            market="ng",
            evidence_id=item.receipt_ids[0],
            row_id=f"diffusion_row_{index:03d}",
            signal_id=f"sig_{index + 10:064x}",
            signal_date=item.target_date,
            published_at=datetime.combine(item.target_date, datetime.min.time(), UTC),
            created_at=datetime.combine(item.target_date, datetime.min.time(), UTC),
        )
        for index, item in enumerate(sequences, 1)
    )
    return current, sequences, rows


def test_diffusion_wrapper_preserves_algorithm_and_stays_ineligible(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_diffusion import (
        DiffusionRules,
        forecast_cross_market_diffusion,
        forecast_cross_market_diffusion_with_provenance,
    )
    from src.analysis.open_intelligence.historical_evidence_bridge import build_diffusion_shadow

    current, sequences, rows = diffusion_sources()
    scoped = frame(market_scope=("za", "ng"))
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_diffusion_evidence_projection(
        frame=scoped, current=current, historical_sequences=sequences, target_markets=("ng",)
    )
    rules = DiffusionRules()
    direct = forecast_cross_market_diffusion(
        current=current, historical_sequences=sequences, target_market="ng", rules=rules
    )
    wrapped, bindings = forecast_cross_market_diffusion_with_provenance(
        current=current,
        historical_sequences=sequences,
        target_market="ng",
        rules=rules,
        evidence_projection=projection,
    )
    assert wrapped == direct
    assert len(bindings) == 3
    result = build_diffusion_shadow(
        frame=scoped,
        current=current,
        historical_sequences=sequences,
        target_markets=("ng",),
        rules=rules,
        evidence_projection=projection,
    )
    assert len(result.receipts) == 1
    assert result.receipts[0].attachment_eligible is False
    assert result.receipts[0].display_eligible is False


def test_bridge_rejects_fixture_projection_before_computation():
    from src.analysis.open_intelligence.historical_evidence_bridge import build_analogue_evidence
    from src.analysis.open_intelligence.historical_evidence_fixture_reader import (
        read_fixture_historical_evidence_projection,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    path = Path("tests/fixtures/open_intelligence/historical_evidence_provenance_v2.json")
    digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    projection = read_fixture_historical_evidence_projection(
        frame=frame(),
        fixture_id="historical_evidence_provenance_v2",
        expected_fixture_digest=digest,
    )
    current, candidate = analogue_source()
    with pytest.raises(HistoricalBridgeError) as caught:
        build_analogue_evidence(
            frame=frame(),
            current=current,
            candidates=(candidate,),
            rules=analogue_rules(),
            evidence_projection=projection,
            limit=1,
        )
    assert caught.value.code == "historical_fixture_projection_runtime_ineligible"


def test_static_production_target_fails_before_client_creation(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    called = []
    monkeypatch.setattr(reader, "DATASET", "trends_v2")
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: called.append(True))
    current, candidate = analogue_source()
    with pytest.raises(HistoricalBridgeError) as caught:
        reader.read_runtime_analogue_evidence_projection(
            frame=frame(), current=current, candidates=(candidate,)
        )
    assert caught.value.code == "historical_evidence_reader_production_forbidden"
    assert called == []


def test_same_day_post_candidate_creation_is_future_leak(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    current, candidate = analogue_source()
    late = row(created_at=datetime(2026, 8, 20, 23, tzinfo=UTC))
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient((late,)))
    with pytest.raises(HistoricalBridgeError) as caught:
        reader.read_runtime_analogue_evidence_projection(
            frame=frame(), current=current, candidates=(candidate,)
        )
    assert caught.value.code == "historical_evidence_reader_future_leak"


def test_duplicate_rows_fail_even_when_total_count_matches_request(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_analogue import (
        AnalogueReceipt,
        HistoricalSignalSnapshot,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    current, candidate = analogue_source()
    second_receipt = AnalogueReceipt(
        receipt_id="ev_" + "2" * 64,
        signal_id="sig_" + "b" * 64,
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
        source_family="reddit",
        market="za",
    )
    second = HistoricalSignalSnapshot(
        signal_id=second_receipt.signal_id,
        as_of=datetime(2026, 8, 21, 1, tzinfo=UTC),
        market="za",
        terms=("fixture",),
        source_families=("reddit",),
        trajectory_signature=("rising", "rising"),
        evidence_state="ready",
        receipts=(second_receipt,),
    )
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient((row(), row())))
    with pytest.raises(HistoricalBridgeError) as caught:
        reader.read_runtime_analogue_evidence_projection(
            frame=frame(), current=current, candidates=(candidate, second)
        )
    assert caught.value.code == "historical_evidence_reader_duplicate"


def test_opaque_runtime_type_has_no_caller_usable_constructor():
    from src.analysis.open_intelligence.historical_evidence_reader import (
        RuntimeHistoricalEvidenceProjectionRead,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    with pytest.raises(HistoricalBridgeError) as caught:
        RuntimeHistoricalEvidenceProjectionRead()
    assert caught.value.code == "historical_projection_capability_invalid"


def test_downstream_revalidation_rejects_projection_and_receipt_tamper(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        build_recurrence_evidence,
        validate_historical_evidence_bridge_result,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError
    from src.analysis.open_intelligence.historical_recurrence import RecurrenceRules

    occurrences, rows = recurrence_sources()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_recurrence_evidence_projection(
        frame=frame(), occurrences=occurrences, pattern_id="pattern_fixture", market="za"
    )
    result = build_recurrence_evidence(
        frame=frame(),
        occurrences=occurrences,
        pattern_id="pattern_fixture",
        market="za",
        rules=RecurrenceRules(),
        evidence_projection=projection,
    )
    validate_historical_evidence_bridge_result(result)
    object.__setattr__(result.evidence_projection, "source_projection_digest", "hsp_" + "0" * 64)
    with pytest.raises(HistoricalBridgeError) as caught:
        validate_historical_evidence_bridge_result(result)
    assert caught.value.code == "historical_evidence_projection_digest_mismatch"


def test_downstream_revalidation_rejects_fully_rehashed_foreign_scope_reference(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        validate_historical_evidence_bridge_result,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    result = _recurrence_bridge_result(monkeypatch)
    foreign_refs = tuple(
        replace(item, client_scope_id="foreign_scope")
        for item in result.evidence_projection.evidence_refs
    )
    forged = _replace_projection_chain(result, evidence_refs=foreign_refs)

    with pytest.raises(HistoricalBridgeError) as caught:
        validate_historical_evidence_bridge_result(forged)

    assert caught.value.code == "historical_evidence_reader_scope_mismatch"
    assert caught.value.source_object_ids == (
        "occurrence_001",
        "occurrence_002",
        "occurrence_003",
        "occurrence_004",
        "pattern_fixture",
    )
    assert caught.value.evidence_ids == tuple(sorted(item.evidence_id for item in foreign_refs))
    assert caught.value.reasons == ("evidence_reference_scope",)


def test_downstream_revalidation_rejects_fully_rehashed_source_projection_digest(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        validate_historical_evidence_bridge_result,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    result = _recurrence_bridge_result(monkeypatch)
    forged = _drift_read_receipt(result, source_projection_digest="hsp_" + "0" * 64)
    object.__setattr__(result, "projection_read_receipt", forged.projection_read_receipt)
    object.__setattr__(result, "evidence_projection", forged.evidence_projection)
    object.__setattr__(result, "receipts", forged.receipts)

    with pytest.raises(HistoricalBridgeError) as caught:
        validate_historical_evidence_bridge_result(result)

    assert caught.value.code == "historical_evidence_projection_digest_mismatch"
    assert caught.value.source_object_ids == (
        "occurrence_001",
        "occurrence_002",
        "occurrence_003",
        "occurrence_004",
        "pattern_fixture",
    )
    assert caught.value.evidence_ids == tuple(
        sorted(item.evidence_id for item in result.evidence_projection.evidence_refs)
    )
    assert caught.value.reasons == ("authoritative_source_projection",)


def test_downstream_revalidation_rejects_rehashed_recurrence_source_market_in_scope(
    monkeypatch,
):
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoricalEvidenceReceipt,
        validate_historical_evidence_bridge_result,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    occurrences, rows = recurrence_sources()
    scoped_rows = tuple({**item, "market_scope": ("za", "ng")} for item in rows)
    result = _recurrence_bridge_result(
        monkeypatch,
        bridge_frame=frame(market_scope=("za", "ng")),
        source_rows=scoped_rows,
    )
    values = asdict(result.receipts[0])
    values.pop("historical_receipt_id")
    values["source_market"] = "ng"
    forged_receipt = HistoricalEvidenceReceipt(
        historical_receipt_id=_contract_digest("heb_", values), **values
    )
    object.__setattr__(result, "receipts", (forged_receipt,))

    with pytest.raises(HistoricalBridgeError) as caught:
        validate_historical_evidence_bridge_result(result)

    assert caught.value.code == "historical_provenance_source_object_mismatch"
    assert caught.value.source_object_ids == (
        "occurrence_001",
        "occurrence_002",
        "occurrence_003",
        "occurrence_004",
        "pattern_fixture",
    )
    assert caught.value.evidence_ids == tuple(
        sorted(receipt for item in occurrences for receipt in item.receipt_ids)
    )
    assert caught.value.reasons == ("authoritative_receipt_source_market",)


def test_bridge_authority_does_not_change_public_result_fields(monkeypatch):
    result = _recurrence_bridge_result(monkeypatch)

    assert tuple(item.name for item in fields(result)) == (
        "frame",
        "projection_read_receipt",
        "evidence_projection",
        "bindings",
        "receipts",
        "issues",
    )
    assert "_authority" not in asdict(result)


@pytest.mark.parametrize(
    ("updates", "code", "reason"),
    [
        (
            {"scope_digest": "scp_" + "0" * 64},
            "historical_evidence_reader_scope_mismatch",
            "scope_digest",
        ),
        (
            {"contract_version": "2.0.0"},
            "historical_evidence_reader_scope_mismatch",
            "contract_version",
        ),
        (
            {"requested_evidence_ids": ()},
            "historical_evidence_projection_extra",
            "requested_evidence_ids",
        ),
        ({"row_count": 0}, "historical_evidence_reader_incomplete", "row_count"),
        (
            {"markets": ()},
            "historical_evidence_reader_scope_mismatch",
            "markets",
        ),
        (
            {"row_natural_key_digests": ()},
            "historical_evidence_reader_incomplete",
            "row_natural_key_digests",
        ),
    ],
)
def test_downstream_revalidation_rejects_rehashed_read_receipt_drift(
    monkeypatch, updates, code, reason
):
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        validate_historical_evidence_bridge_result,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    result = _recurrence_bridge_result(monkeypatch)
    forged = _drift_read_receipt(result, **updates)

    with pytest.raises(HistoricalBridgeError) as caught:
        validate_historical_evidence_bridge_result(forged)

    assert caught.value.code == code
    assert caught.value.source_object_ids == (
        "occurrence_001",
        "occurrence_002",
        "occurrence_003",
        "occurrence_004",
        "pattern_fixture",
    )
    assert caught.value.evidence_ids == tuple(
        sorted(item.evidence_id for item in result.evidence_projection.evidence_refs)
    )
    assert caught.value.reasons == (reason,)


def test_historical_bridge_error_sorts_affected_identity_envelope():
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    error = HistoricalBridgeError(
        "historical_future_leak",
        "future subject",
        source_object_ids=("source_z", "source_a"),
        evidence_ids=("ev_" + "2" * 64, "ev_" + "1" * 64),
        reasons=("z_reason", "a_reason"),
    )

    assert error.source_object_ids == ("source_a", "source_z")
    assert error.evidence_ids == ("ev_" + "1" * 64, "ev_" + "2" * 64)
    assert error.reasons == ("a_reason", "z_reason")


def test_frame_rejects_market_outside_exact_allowlist():
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    with pytest.raises(HistoricalBridgeError) as caught:
        frame(market_scope=("us",))
    assert caught.value.code == "historical_scope_invalid"


def test_downstream_revalidation_rejects_recomputed_family_substitution(monkeypatch):
    import hashlib

    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoricalEvidenceReceipt,
        build_recurrence_evidence,
        validate_historical_evidence_bridge_result,
    )
    from src.analysis.open_intelligence.historical_provenance import (
        HistoricalBridgeError,
        canonical_json_bytes,
    )
    from src.analysis.open_intelligence.historical_recurrence import RecurrenceRules

    occurrences, rows = recurrence_sources()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_recurrence_evidence_projection(
        frame=frame(), occurrences=occurrences, pattern_id="pattern_fixture", market="za"
    )
    result = build_recurrence_evidence(
        frame=frame(),
        occurrences=occurrences,
        pattern_id="pattern_fixture",
        market="za",
        rules=RecurrenceRules(),
        evidence_projection=projection,
    )
    values = asdict(result.receipts[0])
    values.pop("historical_receipt_id")
    values["source_families"] = ("youtube",)
    forged = HistoricalEvidenceReceipt(
        historical_receipt_id="heb_" + hashlib.sha256(canonical_json_bytes(values)).hexdigest(),
        **values,
    )
    object.__setattr__(result, "receipts", (forged,))
    with pytest.raises(HistoricalBridgeError) as caught:
        validate_historical_evidence_bridge_result(result)
    assert caught.value.code == "historical_provenance_source_family_mismatch"


def test_source_wrapper_revalidates_read_receipt_before_binding(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError
    from src.analysis.open_intelligence.historical_recurrence import (
        RecurrenceRules,
        detect_historical_recurrence_with_provenance,
    )

    occurrences, rows = recurrence_sources()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_recurrence_evidence_projection(
        frame=frame(), occurrences=occurrences, pattern_id="pattern_fixture", market="za"
    )
    values = asdict(projection.receipt)
    values.pop("projection_read_receipt_id")
    values["reader_mode"] = "fixture"
    forged = reader.ProjectionReadReceipt(
        projection_read_receipt_id=reader._read_receipt_id(values), **values
    )
    projection.receipt = forged
    with pytest.raises(HistoricalBridgeError) as caught:
        detect_historical_recurrence_with_provenance(
            occurrences=occurrences,
            pattern_id="pattern_fixture",
            market="za",
            as_of=frame().as_of.date(),
            rules=RecurrenceRules(),
            evidence_projection=projection,
        )
    assert caught.value.code == "historical_evidence_reader_target_invalid"


def test_unavailable_evidence_cannot_produce_attachment_eligible_receipt(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_analogue import (
        AnalogueReceipt,
        HistoricalSignalSnapshot,
    )
    from src.analysis.open_intelligence.historical_evidence_bridge import build_analogue_evidence
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    _, candidate = analogue_source()
    current_receipt = AnalogueReceipt(
        receipt_id="ev_" + "2" * 64,
        signal_id="sig_" + "b" * 64,
        published_at=datetime(2026, 8, 27, 8, tzinfo=UTC),
        source_family="reddit",
        market="za",
    )
    current = HistoricalSignalSnapshot(
        signal_id=current_receipt.signal_id,
        as_of=frame().as_of,
        market="za",
        terms=("fixture",),
        source_families=("reddit",),
        trajectory_signature=("rising", "rising"),
        evidence_state="ready",
        receipts=(current_receipt,),
    )
    monkeypatch.setattr(
        reader,
        "_create_runtime_client",
        lambda: FakeClient(
            (
                row(availability="unavailable"),
                row(
                    evidence_id=current_receipt.receipt_id,
                    signal_id=current.signal_id,
                    row_id="fixture_row_002",
                    signal_date=date(2026, 8, 27),
                    published_at=current_receipt.published_at,
                    created_at=datetime(2026, 8, 27, 8, 5, tzinfo=UTC),
                ),
            )
        ),
    )
    projection = reader.read_runtime_analogue_evidence_projection(
        frame=frame(), current=current, candidates=(candidate,)
    )
    with pytest.raises(HistoricalBridgeError) as caught:
        build_analogue_evidence(
            frame=frame(),
            current=current,
            candidates=(candidate,),
            rules=analogue_rules(),
            evidence_projection=projection,
            limit=1,
        )
    assert caught.value.code == "historical_source_mismatch"


HISTORY_RECORD_FIELDS = (
    "history_record_id",
    "record_version",
    "bridge_version",
    "contract_version",
    "requirement_id",
    "investigation_id",
    "scope_digest",
    "kind",
    "source_object_id",
    "source_object_state",
    "as_of",
    "source_market",
    "target_market",
    "source_families",
    "evidence_ids",
    "evidence_refs_digest",
    "historical_receipt_id",
    "projection_read_receipt_id",
    "evidence_projection_receipt_id",
    "source_projection_digest",
    "provenance_binding_ids",
    "provenance_bindings_digest",
    "observed_window_start",
    "observed_window_end",
    "historical_window_start",
    "historical_window_end",
    "difference_reasons",
    "limitations",
    "rule_version",
    "availability",
    "future_leak_state",
    "attachment_eligible",
    "display_eligible",
    "reader_mode",
    "reader_version",
)


def retained_analogue_material(**candidate_row):
    """One retained current signal and one retained earlier candidate."""
    from src.analysis.open_intelligence.historical_analogue import (
        AnalogueReceipt,
        HistoricalSignalSnapshot,
    )

    current_receipt = AnalogueReceipt(
        receipt_id="ev_" + "2" * 64,
        signal_id="sig_" + "b" * 64,
        published_at=datetime(2026, 8, 27, 8, tzinfo=UTC),
        source_family="reddit",
        market="za",
    )
    _, candidate = analogue_source()
    current = HistoricalSignalSnapshot(
        signal_id=current_receipt.signal_id,
        as_of=frame().as_of,
        market="za",
        terms=("fixture",),
        source_families=("reddit",),
        trajectory_signature=("rising", "rising"),
        evidence_state="ready",
        receipts=(current_receipt,),
    )
    rows = (
        row(**candidate_row),
        row(
            evidence_id=current_receipt.receipt_id,
            signal_id=current.signal_id,
            row_id="fixture_row_002",
            signal_date=date(2026, 8, 27),
            published_at=current_receipt.published_at,
            created_at=datetime(2026, 8, 27, 8, 5, tzinfo=UTC),
        ),
    )
    return current, candidate, rows


def retained_analogue_bridge(monkeypatch, **candidate_row):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import build_analogue_evidence

    current, candidate, rows = retained_analogue_material(**candidate_row)
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    projection = reader.read_runtime_analogue_evidence_projection(
        frame=frame(), current=current, candidates=(candidate,)
    )
    result = build_analogue_evidence(
        frame=frame(),
        current=current,
        candidates=(candidate,),
        rules=analogue_rules(),
        evidence_projection=projection,
        limit=1,
    )
    return current, candidate, projection, result


def test_history_records_expose_the_frozen_shape_and_rederive_their_own_id(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HISTORY_RECORD_VERSION,
        HistoryRecord,
        build_history_records,
    )

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    records = build_history_records(result, requirement_id="history")
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, HistoryRecord)
    assert tuple(item.name for item in fields(record)) == HISTORY_RECORD_FIELDS
    assert record.record_version == HISTORY_RECORD_VERSION == "history_record_v2"
    assert record.history_record_id.startswith("hrc_")
    assert len(record.history_record_id) == 68
    values = {key: value for key, value in asdict(record).items() if key != "history_record_id"}
    assert record.history_record_id == _contract_digest("hrc_", values)


def test_history_record_binds_the_exact_admitted_projection_read(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import build_history_records

    _current, _candidate, projection, result = retained_analogue_bridge(monkeypatch)
    record = build_history_records(result, requirement_id="history")[0]
    receipt = result.receipts[0]
    assert record.historical_receipt_id == receipt.historical_receipt_id
    assert record.evidence_refs_digest == receipt.evidence_refs_digest
    assert record.provenance_bindings_digest == receipt.provenance_bindings_digest
    assert record.evidence_ids == receipt.evidence_ids
    assert record.projection_read_receipt_id == projection.receipt.projection_read_receipt_id
    assert record.source_projection_digest == projection.receipt.source_projection_digest
    assert record.reader_mode == "runtime_view"
    assert record.reader_version == "historical_evidence_projection_reader_v1"
    assert record.availability == "available"
    assert record.future_leak_state == "passed"
    assert record.kind == "analogue"
    assert record.attachment_eligible is True
    assert record.display_eligible is False


def test_a_history_record_edited_without_rehashing_loses_its_identity(monkeypatch):
    """An edit that forgets to rehash is caught by the content digest.

    What this shows is exactly that and no more. ``replace`` carries the old
    record id across, so the digest no longer matches the values. An edit that
    does rehash passes this check: ``history_record_values`` and
    ``_contract_digest`` in this file are the counterexample, and every other
    record test here uses them. Such an edit is caught, when it is caught, by
    the enumerated values the record admits and by the historical receipt id
    rederived from the record's own fields, each of which has its own test.
    """
    from src.analysis.open_intelligence.historical_evidence_bridge import build_history_records
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    record = build_history_records(result, requirement_id="history")[0]
    for change in ({"source_market": "ng"}, {"evidence_ids": ()}, {"display_eligible": True}):
        with pytest.raises(HistoricalBridgeError) as caught:
            replace(record, **change)
        assert caught.value.code == "historical_provenance_binding_digest_mismatch"


def test_history_records_refuse_a_bridge_result_without_its_authority(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import build_history_records
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    object.__setattr__(result, "_authority", None)
    with pytest.raises(HistoricalBridgeError) as caught:
        build_history_records(result, requirement_id="history")
    assert caught.value.code == "historical_projection_capability_invalid"


def test_history_record_availability_follows_the_retained_projection(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import build_history_records

    _current, _candidate, _projection, result = retained_analogue_bridge(
        monkeypatch, availability="aged_out"
    )
    record = build_history_records(result, requirement_id="history")[0]
    assert record.availability == "aged_out"
    assert "display_material_aged_out" in record.limitations


def history_plan(*requirement_ids):
    return {
        "requirements": [
            {
                "requirement_id": requirement_id,
                "question": "What is the earlier history?",
                "kind": "history",
                "mandatory": True,
                "search_terms": [],
            }
            for requirement_id in requirement_ids
        ]
        + [
            {
                "requirement_id": "content",
                "question": "What is happening?",
                "kind": "content",
                "mandatory": True,
                "search_terms": [],
            }
        ]
    }


def test_question_history_resolver_answers_a_planned_requirement_from_retained_evidence(
    monkeypatch,
):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecordAnswer,
        RetainedHistorySource,
        build_question_history_resolver,
    )

    current, candidate, rows = retained_analogue_material()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    resolve = build_question_history_resolver(
        frame=frame(),
        sources=(RetainedHistorySource("history", current, (candidate,)),),
        rules=analogue_rules(),
        limit=1,
    )
    answers = resolve(history_plan("history"))
    assert set(answers) == {"history"}
    answer = answers["history"]
    assert isinstance(answer, HistoryRecordAnswer)
    assert answer.state == "retained_analogue"
    assert answer.requirement_id == "history"
    assert answer.matched_signal_id == candidate.signal_id
    assert answer.as_of == frame().as_of
    assert answer.record.source_object_id == candidate.signal_id


def test_question_history_resolver_names_the_refusal_instead_of_answering_nothing(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        RetainedHistorySource,
        UnresolvedHistory,
        build_question_history_resolver,
    )

    current, candidate, _rows = retained_analogue_material()
    leaking = (
        row(published_at=datetime(2026, 8, 29, tzinfo=UTC)),
        row(
            evidence_id="ev_" + "2" * 64,
            signal_id=current.signal_id,
            row_id="fixture_row_002",
            signal_date=date(2026, 8, 27),
            published_at=datetime(2026, 8, 27, 8, tzinfo=UTC),
            created_at=datetime(2026, 8, 27, 8, 5, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(leaking))
    resolve = build_question_history_resolver(
        frame=frame(),
        sources=(RetainedHistorySource("history", current, (candidate,)),),
        rules=analogue_rules(),
        limit=1,
    )
    answer = resolve(history_plan("history"))["history"]
    assert isinstance(answer, UnresolvedHistory)
    assert answer.state == "unresolved"
    assert answer.reason == "historical_evidence_reader_future_leak"


def test_question_history_resolver_answers_every_planned_requirement(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecordAnswer,
        RetainedHistorySource,
        UnresolvedHistory,
        build_question_history_resolver,
    )

    current, candidate, rows = retained_analogue_material()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    resolve = build_question_history_resolver(
        frame=frame(),
        sources=(RetainedHistorySource("history", current, (candidate,)),),
        rules=analogue_rules(),
        limit=1,
    )
    answers = resolve(history_plan("history", "second_history"))
    assert set(answers) == {"history", "second_history"}
    assert isinstance(answers["history"], HistoryRecordAnswer)
    unnamed = answers["second_history"]
    assert isinstance(unnamed, UnresolvedHistory)
    assert unnamed.reason == "historical_source_not_retained"


def test_question_history_resolver_without_any_retained_source_still_answers(monkeypatch):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        UnresolvedHistory,
        build_question_history_resolver,
    )

    monkeypatch.setattr(
        reader, "_create_runtime_client", lambda: pytest.fail("no retained source to read")
    )
    resolve = build_question_history_resolver(
        frame=frame(), sources=(), rules=analogue_rules(), limit=1
    )
    answers = resolve(history_plan("history"))
    assert isinstance(answers["history"], UnresolvedHistory)
    assert answers["history"].reason == "historical_source_not_retained"


def history_record_values(record, **changes):
    """The digested field set of one record, with the named fields replaced."""
    values = {key: value for key, value in asdict(record).items() if key != "history_record_id"}
    values.update(changes)
    return values


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"record_version": "history_record_v1"}, "historical_provenance_incomplete"),
        ({"bridge_version": "historical_evidence_bridge_v0"}, "historical_provenance_incomplete"),
        ({"contract_version": "2.0.0"}, "historical_contract_version_invalid"),
        ({"requirement_id": "   "}, "historical_provenance_incomplete"),
        ({"requirement_id": 7}, "historical_provenance_incomplete"),
        ({"future_leak_state": "rejected"}, "historical_future_leak"),
        ({"availability": "probably"}, "historical_source_mismatch"),
        ({"reader_mode": "fixture"}, "historical_evidence_reader_target_invalid"),
        ({"reader_version": "totally_made_up"}, "historical_evidence_reader_target_invalid"),
        ({"kind": "not_a_kind"}, "historical_provenance_source_object_mismatch"),
        ({"evidence_ids": ()}, "historical_evidence_projection_missing"),
        # The diffusion eligibility rule is one line with two halves, and a
        # record built as an analogue is attachment eligible already. Each
        # half is tried on its own, then both together, so neither half can
        # be dropped behind the other.
        ({"kind": "diffusion_shadow"}, "historical_diffusion_ineligible"),
        (
            {"kind": "diffusion_shadow", "attachment_eligible": False, "display_eligible": True},
            "historical_diffusion_ineligible",
        ),
        (
            {"kind": "diffusion_shadow", "display_eligible": True},
            "historical_diffusion_ineligible",
        ),
    ],
)
def test_history_record_refuses_every_enumerated_value_it_never_admitted(monkeypatch, change, code):
    """A correctly rehashed record cannot claim a value the bridge never set.

    The record is rehashed around each change, so the content digest matches
    and cannot be what refuses it. Each case names the guard that does, by the
    code that guard raises, so a guard deleted behind another still fails this
    test rather than being answered by the wrong refusal.
    """
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecord,
        build_history_records,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    record = build_history_records(result, requirement_id="history")[0]
    values = history_record_values(record, **change)
    with pytest.raises(HistoricalBridgeError) as caught:
        HistoryRecord(history_record_id=_contract_digest("hrc_", values), **values)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "change",
    [
        {"observed_window_end": datetime(2020, 1, 1, tzinfo=UTC)},
        {"historical_window_end": date(2020, 1, 1)},
    ],
)
def test_history_record_refuses_a_window_that_ends_before_it_starts(monkeypatch, change):
    """A rehashed record with a reversed window is refused, not carried."""
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecord,
        build_history_records,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    record = build_history_records(result, requirement_id="history")[0]
    values = history_record_values(record, **change)
    with pytest.raises(HistoricalBridgeError) as caught:
        HistoryRecord(history_record_id=_contract_digest("hrc_", values), **values)
    assert caught.value.code == "historical_provenance_incomplete"


def test_history_record_is_bound_to_the_one_requirement_it_answers(monkeypatch):
    """The requirement id is inside the digest, so no record answers two."""
    from src.analysis.open_intelligence.historical_evidence_bridge import build_history_records

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    first = build_history_records(result, requirement_id="history")[0]
    second = build_history_records(result, requirement_id="history_two")[0]
    assert first.requirement_id == "history"
    assert second.requirement_id == "history_two"
    assert first.history_record_id != second.history_record_id
    assert history_record_values(first, requirement_id="history_two") == history_record_values(
        second
    )


def test_a_history_answer_refuses_a_record_bound_to_another_requirement(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecordAnswer,
        build_history_records,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    record = build_history_records(result, requirement_id="history")[0]
    assert HistoryRecordAnswer("history", record).requirement_id == "history"
    with pytest.raises(HistoricalBridgeError) as caught:
        HistoryRecordAnswer("history_two", record)
    assert caught.value.code == "historical_provenance_source_object_mismatch"


def test_planned_history_requirement_ids_refuse_an_id_that_is_not_text():
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        planned_history_requirement_ids,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    for identity in (7, "", "   ", None):
        plan = history_plan("history")
        plan["requirements"][0]["requirement_id"] = identity
        with pytest.raises(HistoricalBridgeError) as caught:
            planned_history_requirement_ids(plan)
        assert caught.value.code == "historical_provenance_incomplete"


def test_question_history_resolver_records_a_reader_failure_that_is_not_a_bridge_error():
    """A transport failure at the reader seam leaves the requirement unresolved."""
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        RetainedHistorySource,
        UnresolvedHistory,
        build_question_history_resolver,
    )

    current, candidate, _rows = retained_analogue_material()

    def failing_read(**_kwargs):
        raise RuntimeError("the retained projection read never reached its target")

    resolve = build_question_history_resolver(
        frame=frame(),
        sources=(RetainedHistorySource("history", current, (candidate,)),),
        rules=analogue_rules(),
        limit=1,
        read_projection=failing_read,
    )
    answer = resolve(history_plan("history"))["history"]
    assert isinstance(answer, UnresolvedHistory)
    assert answer.state == "unresolved"
    assert answer.reason == "historical_evidence_read_failed"


HISTORY_RECORD_RECEIPT_FORGERIES = (
    {"display_eligible": True},
    {"attachment_eligible": False},
    {"investigation_id": "inv_never_ran"},
    {"scope_digest": "sco_" + "0" * 64},
    {"source_object_id": "sig_" + "c" * 64},
    {"evidence_refs_digest": "erd_" + "0" * 64},
    {"evidence_projection_receipt_id": "hep_" + "0" * 64},
    {"source_projection_digest": "spd_" + "0" * 64},
    {"provenance_bindings_digest": "pbd_" + "0" * 64},
    {"rule_version": "analogue_rules_" + "0" * 64},
    {"source_market": "ng"},
    {"target_market": "ng"},
    {"limitations": ()},
)


def test_history_record_rederives_the_historical_receipt_it_names(monkeypatch):
    """The receipt id is recomputed from the record's own fields.

    Every field the historical receipt digested is carried on the record
    except the leak reasons, and the record admits only a passed leak state,
    which is the state that leaves those reasons empty. So the receipt id is
    fully rederivable here and is rederived. What this shows is only that the
    id cannot be set independently of those twenty six fields. Every input is
    the record's own, no receipt is fetched, and a record built for a receipt
    that was never issued, with its id computed the same way, is accepted.
    """
    from src.analysis.open_intelligence.historical_evidence_bridge import build_history_records

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    record = build_history_records(result, requirement_id="history")[0]
    receipt = result.receipts[0]
    assert record.historical_receipt_id == receipt.historical_receipt_id
    values = history_record_values(record)
    receipt_values = {
        name: () if name == "future_leak_reasons" else values[name]
        for name in fields_of_the_historical_receipt()
    }
    assert record.historical_receipt_id == _contract_digest("heb_", receipt_values)


def fields_of_the_historical_receipt():
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoricalEvidenceReceipt,
    )

    return tuple(
        item.name
        for item in fields(HistoricalEvidenceReceipt)
        if item.name != "historical_receipt_id"
    )


@pytest.mark.parametrize("change", HISTORY_RECORD_RECEIPT_FORGERIES)
def test_history_record_refuses_a_field_the_historical_receipt_already_fixed(monkeypatch, change):
    """A correctly rehashed record cannot rename what the receipt digested."""
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecord,
        build_history_records,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    record = build_history_records(result, requirement_id="history")[0]
    values = history_record_values(record, **change)
    with pytest.raises(HistoricalBridgeError) as caught:
        HistoryRecord(history_record_id=_contract_digest("hrc_", values), **values)
    assert caught.value.code == "historical_provenance_binding_digest_mismatch"


def test_a_history_answer_refuses_a_record_the_bridge_did_not_build(monkeypatch):
    """The answer type takes only a record carrying the bridge capability.

    The hand built record below compares equal to the admitted one field for
    field, because the capability is not a field and cannot be written into a
    stored projection or rehashed around. That is the whole point: the content
    digest is computed over the record's own values, so anyone holding those
    values can reproduce it, and only the capability distinguishes a record
    the bridge built from one assembled to look like it. That distinction
    holds against untrusted data only; code in the same process can copy the
    capability off a genuine record.
    """
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecord,
        HistoryRecordAnswer,
        build_history_records,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    _current, _candidate, _projection, result = retained_analogue_bridge(monkeypatch)
    record = build_history_records(result, requirement_id="history")[0]
    values = history_record_values(record)
    handmade = HistoryRecord(history_record_id=_contract_digest("hrc_", values), **values)
    assert handmade == record
    assert HistoryRecordAnswer("history", record).record is record
    with pytest.raises(HistoricalBridgeError) as caught:
        HistoryRecordAnswer("history", handmade)
    assert caught.value.code == "historical_projection_capability_invalid"


def distant_analogue_rules():
    """Rules a candidate sharing no terms with the current signal cannot meet."""
    from src.analysis.open_intelligence.historical_analogue import HistoricalAnalogueRules

    return HistoricalAnalogueRules(
        term_weight=0.4,
        source_weight=0.2,
        trajectory_weight=0.3,
        geography_weight=0.1,
        minimum_similarity=0.9,
        allow_cross_market=False,
        minimum_trajectory_points=2,
        eligible_evidence_states=("ready",),
    )


def test_question_history_resolver_says_nothing_comparable_was_retained(monkeypatch):
    """The ordinary answer: signals were read and none of them compared.

    This is not a refusal and not an error. The retained read succeeded, the
    bridge admitted it, and the similarity rules matched nothing, so the
    requirement is answered with the reason and the question carries it as
    missing work.
    """
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        RetainedHistorySource,
        UnresolvedHistory,
        build_question_history_resolver,
    )

    current, candidate, rows = retained_analogue_material()
    unlike = replace(candidate, terms=("nothing", "in", "common"))
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: FakeClient(rows))
    resolve = build_question_history_resolver(
        frame=frame(),
        sources=(RetainedHistorySource("history", current, (unlike,)),),
        rules=distant_analogue_rules(),
        limit=1,
    )
    answer = resolve(history_plan("history"))["history"]
    assert isinstance(answer, UnresolvedHistory)
    assert answer.state == "unresolved"
    assert answer.reason == "historical_analogue_unavailable"


def test_question_history_resolver_refuses_two_sources_for_one_requirement():
    """Two retained sources for one requirement silently kept only the last."""
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        RetainedHistorySource,
        build_question_history_resolver,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    current, candidate, _rows = retained_analogue_material()
    other = replace(candidate, terms=("something", "else"))
    with pytest.raises(HistoricalBridgeError) as caught:
        build_question_history_resolver(
            frame=frame(),
            sources=(
                RetainedHistorySource("history", current, (candidate,)),
                RetainedHistorySource("history", current, (other,)),
            ),
            rules=analogue_rules(),
            limit=1,
        )
    assert caught.value.code == "historical_provenance_source_object_mismatch"


@pytest.mark.parametrize(
    "arguments",
    [
        ("history", ""),
        ("history", None),
        ("history", 7),
        ("", "historical_source_not_retained"),
        (None, "historical_source_not_retained"),
    ],
)
def test_an_unresolved_history_needs_both_an_identifier_and_a_reason(arguments):
    """An unresolved state with no reason says nothing, so it is refused."""
    from src.analysis.open_intelligence.historical_evidence_bridge import UnresolvedHistory
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    with pytest.raises(HistoricalBridgeError) as caught:
        UnresolvedHistory(*arguments)
    assert caught.value.code == "historical_provenance_incomplete"


def test_planned_history_requirement_ids_refuse_a_requirement_with_no_kind():
    """A requirement with no kind is refused, never read as not history."""
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        planned_history_requirement_ids,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    plan = history_plan("history")
    del plan["requirements"][0]["kind"]
    with pytest.raises(HistoricalBridgeError) as caught:
        planned_history_requirement_ids(plan)
    assert caught.value.code == "historical_provenance_incomplete"


def test_planned_history_requirement_ids_refuse_two_requirements_sharing_an_id():
    """Two requirements with one id would collapse into a single answer."""
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        planned_history_requirement_ids,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    assert planned_history_requirement_ids(history_plan("b_history", "a_history")) == (
        "a_history",
        "b_history",
    )
    with pytest.raises(HistoricalBridgeError) as caught:
        planned_history_requirement_ids(history_plan("history", "history"))
    assert caught.value.code == "historical_provenance_incomplete"
