"""Task 1 contract tests for frozen scoring qualification evidence."""

from __future__ import annotations

import ast
import inspect
import json
import math
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from scripts.staging import replay_open_intelligence as replay
from scripts.staging import scoring_qualification as qualification
from src.analysis.open_intelligence.scoring import (
    DecisionStrengthRules,
    SignalScoreInput,
    score_signal,
)

from tests.unit import test_open_intelligence_replay as replay_tests

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "open_intelligence" / "scoring_qualification"
)
COMPLETE_FIXTURE = FIXTURE_DIR / "complete_fixture_v1.json"
UNAVAILABLE_FIXTURE = FIXTURE_DIR / "unavailable_fixture_v1.json"
CANDIDATE_ID = "sig_" + "e" * 64

COMPONENT_NAMES = {
    "velocity",
    "breadth",
    "source_independence",
    "source_integrity",
    "geo_confidence",
    "qualifying_source_families",
    "qualifying_current_receipts",
}
GATE_NAMES = {
    "evidence_state",
    "foreign_market_sample_reviewed",
    "foreign_market_leakage",
    "duplicate_identity",
    "factual_conflict",
    "directional_conflict",
    "membership_receipts_complete",
    "cluster_build_version",
}
PROHIBITED_FIELDS = {
    "audience_fit",
    "genz",
    "gen_z",
    "gender",
    "demographic",
    "brand_relevance",
    "commercial_opportunity",
    "regional_score",
    "source_yield",
}
TYPE_FIELD_SETS = {
    "ScoringRowReference": {"row_id", "observed_at"},
    "ScoringProofValue": {
        "name",
        "state",
        "value",
        "source_rows",
        "window_start",
        "window_end",
        "numerator",
        "denominator",
        "missing_reason",
    },
    "ScoringGateProof": {
        "name",
        "state",
        "observed_value",
        "source_rows",
        "window_start",
        "window_end",
        "missing_reason",
    },
    "ReceiptCapCandidate": {"candidate_id", "cap"},
    "GeoFloorCandidate": {"candidate_id", "za", "ng", "ke"},
    "ScoringCandidateComparison": {
        "candidate_kind",
        "candidate_id",
        "parameters",
        "factors",
        "parameter_digest",
        "state",
        "value",
        "missing_reasons",
    },
    "MathematicalScoreProof": {
        "trust_gate_eligible",
        "velocity",
        "breadth",
        "source_independence",
        "evidence_strength",
        "geo_confidence",
        "scorer_decision_strength",
        "independent_decision_strength",
        "scorer_matches_independent",
        "missing_reasons",
    },
    "CohortDistribution": {
        "market",
        "signal_date",
        "discovery_mode",
        "rule_version",
        "eligible_count",
        "distribution",
        "missing_reasons",
    },
    "CohortSignalPercentile": {
        "signal_id",
        "market",
        "signal_date",
        "discovery_mode",
        "rule_version",
        "eligible_count",
        "first_rank",
        "last_rank",
        "percentile",
        "missing_reason",
    },
    "CorrelationDiagnostic": {
        "left_component",
        "right_component",
        "state",
        "coefficient",
        "input_count",
        "included_count",
        "excluded_count",
        "missing_reason",
    },
    "SourceDominanceDiagnostic": {
        "source_family",
        "state",
        "share",
        "numerator",
        "denominator",
        "missing_reason",
    },
    "ScoringQualificationRecord": {
        "qualification_version",
        "source_snapshot_digest",
        "provider_bundle_digest",
        "rule_candidate_digest",
        "signal_id",
        "market",
        "signal_date",
        "discovery_mode",
        "rule_version",
        "component_proofs",
        "gate_proofs",
        "mathematical_proof",
        "candidate_comparisons",
        "source_dominance",
        "missing_reasons",
        "canonical_sha256",
    },
    "ScoringQualificationBatch": {
        "qualification_version",
        "mode",
        "source_snapshot_digest",
        "provider_bundle_digest",
        "rule_candidate_digest",
        "records",
        "cohort_distributions",
        "signal_percentiles",
        "correlations",
        "missing_reasons",
        "certified",
        "recommendation",
        "promotion_authorized",
        "human_review_state",
        "canonical_sha256",
    },
}


def _fixture(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _row(raw: dict[str, object]) -> qualification.ScoringRowReference:
    return qualification.ScoringRowReference(
        row_id=raw["row_id"],
        observed_at=datetime.fromisoformat(raw["observed_at"]),
    )


def _component(raw: dict[str, object]) -> qualification.ScoringProofValue:
    value = raw["value"]
    return qualification.ScoringProofValue(
        name=raw["name"],
        state=raw["state"],
        value=None if value is None else Fraction(value),
        source_rows=tuple(_row(item) for item in raw["source_rows"]),
        window_start=(
            None if raw["window_start"] is None else datetime.fromisoformat(raw["window_start"])
        ),
        window_end=(
            None if raw["window_end"] is None else datetime.fromisoformat(raw["window_end"])
        ),
        numerator=raw["numerator"],
        denominator=raw["denominator"],
        missing_reason=raw["missing_reason"],
    )


def _gate(raw: dict[str, object]) -> qualification.ScoringGateProof:
    return qualification.ScoringGateProof(
        name=raw["name"],
        state=raw["state"],
        observed_value=raw["observed_value"],
        source_rows=tuple(_row(item) for item in raw["source_rows"]),
        window_start=(
            None if raw["window_start"] is None else datetime.fromisoformat(raw["window_start"])
        ),
        window_end=(
            None if raw["window_end"] is None else datetime.fromisoformat(raw["window_end"])
        ),
        missing_reason=raw["missing_reason"],
    )


def _mathematical(raw: dict[str, object]) -> qualification.MathematicalScoreProof:
    fraction_fields = {"velocity", "breadth", "source_independence", "geo_confidence"}
    decimal_fields = {
        "evidence_strength",
        "scorer_decision_strength",
        "independent_decision_strength",
    }
    values = dict(raw)
    for name in fraction_fields:
        values[name] = None if values[name] is None else Fraction(values[name])
    for name in decimal_fields:
        values[name] = None if values[name] is None else Decimal(values[name])
    values["missing_reasons"] = tuple(values["missing_reasons"])
    return qualification.MathematicalScoreProof(**values)


def _record(raw: dict[str, object]) -> qualification.ScoringQualificationRecord:
    return qualification.ScoringQualificationRecord(
        qualification_version=raw["qualification_version"],
        source_snapshot_digest=raw["source_snapshot_digest"],
        provider_bundle_digest=raw["provider_bundle_digest"],
        rule_candidate_digest=raw["rule_candidate_digest"],
        signal_id=raw["signal_id"],
        market=raw["market"],
        signal_date=date.fromisoformat(raw["signal_date"]),
        discovery_mode=raw["discovery_mode"],
        rule_version=raw["rule_version"],
        component_proofs=tuple(_component(item) for item in raw["component_proofs"]),
        gate_proofs=tuple(_gate(item) for item in raw["gate_proofs"]),
        mathematical_proof=_mathematical(raw["mathematical_proof"]),
        candidate_comparisons=(),
        source_dominance=(),
        missing_reasons=tuple(raw["missing_reasons"]),
        canonical_sha256=raw["canonical_sha256"],
    )


def _batch(raw: dict[str, object]) -> qualification.ScoringQualificationBatch:
    return qualification.ScoringQualificationBatch(
        qualification_version=raw["qualification_version"],
        mode=raw["mode"],
        source_snapshot_digest=raw["source_snapshot_digest"],
        provider_bundle_digest=raw["provider_bundle_digest"],
        rule_candidate_digest=raw["rule_candidate_digest"],
        records=tuple(_record(item) for item in raw["records"]),
        cohort_distributions=(),
        signal_percentiles=(),
        correlations=(),
        missing_reasons=tuple(raw["missing_reasons"]),
        certified=raw["certified"],
        recommendation=raw["recommendation"],
        promotion_authorized=raw["promotion_authorized"],
        human_review_state=raw["human_review_state"],
        canonical_sha256=raw["canonical_sha256"],
    )


def _rules() -> DecisionStrengthRules:
    return DecisionStrengthRules(
        approved_receipt_cap=8,
        geo_floor_by_market={"za": 0.05, "ng": 0.05, "ke": 0.05},
        approved_cluster_build_versions=("hybrid_graph_v1",),
        rule_version="decision_rules_fixture_v1",
    )


def _unsafe_replace(instance: object, **changes: object) -> object:
    clone = object.__new__(type(instance))
    for field in fields(instance):
        object.__setattr__(
            clone,
            field.name,
            changes.get(field.name, getattr(instance, field.name)),
        )
    return clone


def _scoring_bundle(*, reverse: bool = False) -> replay.ReplayInputBundle:
    base = replay_tests.adapt(replay_tests.projected_result())
    raw = _fixture(COMPLETE_FIXTURE)["records"][0]
    source_rows = (
        qualification.ScoringRowReference(
            row_id="shared",
            observed_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        ),
        qualification.ScoringRowReference(
            row_id="ledger_za_alpha",
            observed_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        ),
    )
    components = tuple(
        replace(_component(item), source_rows=source_rows) for item in raw["component_proofs"]
    )
    gates = tuple(replace(_gate(item), source_rows=source_rows) for item in raw["gate_proofs"])
    providers = (
        {"candidate_id": "provider_a", "provider_kind": "fixture", "provider_version": "1"},
        {"candidate_id": "provider_b", "provider_kind": "fixture", "provider_version": "1"},
    )
    if reverse:
        components = tuple(
            replace(item, source_rows=tuple(reversed(item.source_rows)))
            for item in reversed(components)
        )
        gates = tuple(
            replace(item, source_rows=tuple(reversed(item.source_rows))) for item in reversed(gates)
        )
        providers = tuple(reversed(providers))
    # Graph identity only. The old component_proofs and gate_proofs entries
    # were the caller-supplied value path and no longer exist.
    authority = {
        "signal_id": CANDIDATE_ID,
        "market": "za",
        "signal_date": date(2026, 8, 25),
        "discovery_mode": "replay",
    }
    _ = (components, gates)
    snapshot = replay_tests.mutable_tree(base.source_snapshot)
    snapshot["section_identity_sets"]["components"] = [CANDIDATE_ID]
    snapshot["section_counts"]["components"] = 1
    snapshot["section_identity_sets"]["provider_outputs"] = ["provider_a", "provider_b"]
    snapshot["section_counts"]["provider_outputs"] = 2
    replay_tests.refresh_snapshot_digests(snapshot)
    return replace(
        base,
        source_snapshot=snapshot,
        components_by_candidate={CANDIDATE_ID: authority},
        provider_outputs=providers,
    )


def _authority(bundle: replay.ReplayInputBundle) -> dict[str, object]:
    return dict(bundle.components_by_candidate[CANDIDATE_ID])


def _with_authority(
    bundle: replay.ReplayInputBundle,
    authority: dict[str, object],
) -> replay.ReplayInputBundle:
    return replace(bundle, components_by_candidate={CANDIDATE_ID: authority})


def independent_oracle(
    fixture_text: str,
    omitted_component: str | None = None,
) -> tuple[Decimal, Decimal]:
    import json
    from decimal import Decimal
    from fractions import Fraction

    raw = json.loads(fixture_text)
    components = {
        item["name"]: Fraction(item["value"]) for item in raw["records"][0]["component_proofs"]
    }

    def product(names: tuple[str, ...]) -> Fraction:
        result = Fraction(1)
        for name in names:
            if name != omitted_component:
                result *= components[name]
        return result

    evidence_product = product(
        (
            "qualifying_source_families",
            "qualifying_current_receipts",
            "source_integrity",
        )
    )
    evidence = (Decimal(evidence_product.numerator) / Decimal(evidence_product.denominator)) ** (
        Decimal(1) / Decimal(3)
    )
    decision_product = product(
        (
            "velocity",
            "breadth",
            "source_independence",
            "geo_confidence",
        )
    )
    decision = (
        (Decimal(decision_product.numerator) / Decimal(decision_product.denominator)) * evidence
    ) ** (Decimal(1) / Decimal(5))
    quantum = Decimal("0.000000000001")
    return evidence.quantize(quantum), decision.quantize(quantum)


def test_construction_has_exact_frozen_slotted_field_sets() -> None:
    for class_name, expected in TYPE_FIELD_SETS.items():
        cls = getattr(qualification, class_name)
        assert {field.name for field in fields(cls)} == expected
        assert cls.__dataclass_params__.frozen is True
        assert "__dict__" not in cls.__slots__


@pytest.mark.parametrize("path", [COMPLETE_FIXTURE, UNAVAILABLE_FIXTURE])
def test_construction_fixture_builds_frozen_tuple_only_batch(path: Path) -> None:
    batch = _batch(_fixture(path))

    assert isinstance(batch.records, tuple)
    assert isinstance(batch.records[0].component_proofs, tuple)
    assert isinstance(batch.records[0].gate_proofs, tuple)
    assert isinstance(batch.records[0].mathematical_proof.missing_reasons, tuple)
    with pytest.raises(FrozenInstanceError):
        batch.certified = True


def test_construction_complete_fixture_has_exact_names_states_and_windows() -> None:
    record = _batch(_fixture(COMPLETE_FIXTURE)).records[0]

    assert {proof.name for proof in record.component_proofs} == COMPONENT_NAMES
    assert {proof.name for proof in record.gate_proofs} == GATE_NAMES
    assert {proof.state for proof in record.component_proofs} == {"measured"}
    assert {proof.state for proof in record.gate_proofs} == {"measured"}
    assert all(proof.window_start <= proof.window_end for proof in record.component_proofs)
    assert all(proof.window_start <= proof.window_end for proof in record.gate_proofs)
    assert all(proof.observed_value == proof.observed_value.lower() for proof in record.gate_proofs)


def test_construction_unavailable_fixture_carries_no_results() -> None:
    record = _batch(_fixture(UNAVAILABLE_FIXTURE)).records[0]

    assert all(proof.value is None for proof in record.component_proofs)
    assert all(proof.numerator is None for proof in record.component_proofs)
    assert all(proof.denominator is None for proof in record.component_proofs)
    assert all(proof.observed_value is None for proof in record.gate_proofs)
    assert record.mathematical_proof.independent_decision_strength is None
    assert record.mathematical_proof.scorer_decision_strength is None


def test_malformed_row_rejects_duplicate_identity_and_non_utc_time() -> None:
    row = qualification.ScoringRowReference("row-1", datetime(2026, 8, 25, tzinfo=UTC))
    values = {
        "name": "velocity",
        "state": "measured",
        "value": Fraction(1, 2),
        "source_rows": (row, row),
        "window_start": datetime(2026, 8, 24, tzinfo=UTC),
        "window_end": datetime(2026, 8, 25, tzinfo=UTC),
        "numerator": 1,
        "denominator": 2,
        "missing_reason": None,
    }

    with pytest.raises(ValueError, match="unique"):
        qualification.ScoringProofValue(**values)
    with pytest.raises(ValueError, match="UTC"):
        qualification.ScoringRowReference("row-2", datetime(2026, 8, 25))


@pytest.mark.parametrize("field", ["numerator", "denominator"])
def test_malformed_component_rejects_boolean_as_integer(field: str) -> None:
    raw = _fixture(COMPLETE_FIXTURE)["records"][0]["component_proofs"][0]
    values = dict(raw)
    values[field] = True

    with pytest.raises(ValueError, match="integer"):
        _component(values)


def test_malformed_component_rejects_reversed_or_incomplete_measured_window() -> None:
    raw = _fixture(COMPLETE_FIXTURE)["records"][0]["component_proofs"][0]
    reversed_window = dict(raw, window_start=raw["window_end"], window_end=raw["window_start"])
    incomplete_window = dict(raw, window_end=None)

    with pytest.raises(ValueError, match="window"):
        _component(reversed_window)
    with pytest.raises(ValueError, match="window"):
        _component(incomplete_window)


def test_malformed_unavailable_component_and_gate_reject_results() -> None:
    component = _fixture(UNAVAILABLE_FIXTURE)["records"][0]["component_proofs"][0]
    gate = _fixture(UNAVAILABLE_FIXTURE)["records"][0]["gate_proofs"][0]

    with pytest.raises(ValueError, match="unavailable"):
        _component(dict(component, value="0.0"))
    with pytest.raises(ValueError, match="unavailable"):
        _gate(dict(gate, observed_value="false"))


def test_malformed_candidates_reject_boolean_duplicate_and_nonfinite_values() -> None:
    with pytest.raises(ValueError, match="integer"):
        qualification.ReceiptCapCandidate("cap-bool", True)
    with pytest.raises(ValueError, match="unique"):
        qualification.ScoringCandidateComparison(
            candidate_kind="formula",
            candidate_id="formula-a",
            parameters=(("formula", "geometric"), ("formula", "arithmetic")),
            factors=(),
            parameter_digest="a" * 64,
            state="measured",
            value=Decimal("0.5"),
            missing_reasons=(),
        )
    with pytest.raises(ValueError, match="finite"):
        qualification.ScoringCandidateComparison(
            candidate_kind="formula",
            candidate_id="formula-a",
            parameters=(("formula", "geometric"),),
            factors=(),
            parameter_digest="a" * 64,
            state="measured",
            value=Decimal("NaN"),
            missing_reasons=(),
        )


def test_malformed_record_rejects_wrong_market_digest_and_duplicate_names() -> None:
    record = _batch(_fixture(COMPLETE_FIXTURE)).records[0]

    with pytest.raises(ValueError, match="market"):
        replace(record, market="global")
    with pytest.raises(ValueError, match="digest"):
        replace(record, source_snapshot_digest="A" * 64)
    with pytest.raises(ValueError, match="component proofs"):
        replace(
            record,
            component_proofs=(*record.component_proofs[:-1], record.component_proofs[0]),
        )


def test_audience_neutral_schema_rejects_every_prohibited_field() -> None:
    all_fields = set().union(*TYPE_FIELD_SETS.values())
    assert all_fields.isdisjoint(PROHIBITED_FIELDS)
    with pytest.raises(TypeError):
        qualification.ReceiptCapCandidate(candidate_id="cap-8", cap=8, audience_fit="high")

    for fixture_path in (COMPLETE_FIXTURE, UNAVAILABLE_FIXTURE):
        raw_text = fixture_path.read_text(encoding="utf-8").lower()
        assert all(f'"{field}"' not in raw_text for field in PROHIBITED_FIELDS)


def test_audience_history_and_brand_metadata_cannot_change_oracle_values() -> None:
    raw = _fixture(COMPLETE_FIXTURE)
    baseline = independent_oracle(json.dumps(raw))
    mutated = json.loads(json.dumps(raw))
    mutated["records"][0]["audience_fit"] = "gen_z"
    mutated["records"][0]["historical_similarity"] = "high"
    mutated["records"][0]["brand_relevance"] = "high"

    assert independent_oracle(json.dumps(mutated)) == baseline
    with pytest.raises(TypeError):
        qualification.ScoringQualificationRecord(**mutated["records"][0])


def test_independent_oracle_uses_only_decimal_fraction_and_json() -> None:
    tree = ast.parse(inspect.getsource(independent_oracle))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    roots = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert imports == {"json", "Decimal", "Fraction"}
    assert roots == {"decimal", "fractions"}
    assert "qualification" not in inspect.getsource(independent_oracle)
    assert "score_signal" not in inspect.getsource(independent_oracle)


def test_independent_oracle_matches_hand_derived_frozen_vector() -> None:
    fixture_text = COMPLETE_FIXTURE.read_text(encoding="utf-8")
    record = _fixture(COMPLETE_FIXTURE)["records"][0]

    assert independent_oracle(fixture_text) == (
        Decimal(record["mathematical_proof"]["evidence_strength"]),
        Decimal(record["mathematical_proof"]["independent_decision_strength"]),
    )


@pytest.mark.parametrize(
    "omitted_component",
    [
        "velocity",
        "breadth",
        "source_independence",
        "source_integrity",
        "geo_confidence",
        "qualifying_source_families",
        "qualifying_current_receipts",
    ],
)
def test_asymmetric_vector_detects_every_component_removal(omitted_component: str) -> None:
    fixture_text = COMPLETE_FIXTURE.read_text(encoding="utf-8")
    baseline = independent_oracle(fixture_text)
    mutated = independent_oracle(fixture_text, omitted_component)
    missing = json.loads(fixture_text)
    missing["records"][0]["component_proofs"] = [
        item
        for item in missing["records"][0]["component_proofs"]
        if item["name"] != omitted_component
    ]

    assert mutated != baseline
    assert all(value.as_tuple().exponent == -12 for value in mutated)
    with pytest.raises(KeyError):
        independent_oracle(json.dumps(missing))


def test_genuine_zero_vector_remains_measured_and_produces_exact_zero() -> None:
    raw = _fixture(COMPLETE_FIXTURE)
    velocity = next(
        item for item in raw["records"][0]["component_proofs"] if item["name"] == "velocity"
    )
    velocity.update(value="0", numerator=0, denominator=1)
    mathematical = raw["records"][0]["mathematical_proof"]
    mathematical.update(
        velocity="0",
        scorer_decision_strength="0.000000000000",
        independent_decision_strength="0.000000000000",
        scorer_matches_independent=True,
    )

    record = _batch(raw).records[0]

    assert independent_oracle(json.dumps(raw)) == (
        Decimal("0.750000000000"),
        Decimal("0.000000000000"),
    )
    assert next(proof for proof in record.component_proofs if proof.name == "velocity").state == (
        "measured"
    )
    assert record.mathematical_proof.independent_decision_strength == Decimal("0")


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "scorer_decision_strength": "0.500000000000",
            "independent_decision_strength": "0.500000000000",
        },
        {"scorer_matches_independent": True},
        {
            "scorer_decision_strength": "0.500000000000",
            "independent_decision_strength": "0.500000000000",
            "scorer_matches_independent": True,
        },
    ],
)
def test_ineligible_mathematical_proof_rejects_decision_overfill(
    overrides: dict[str, object],
) -> None:
    raw = _fixture(UNAVAILABLE_FIXTURE)["records"][0]["mathematical_proof"]

    with pytest.raises(ValueError, match="ineligible"):
        _mathematical(dict(raw, **overrides))


@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("proof_kind", ["component", "gate"])
def test_row_window_rejects_measured_rows_outside_inclusive_bounds(
    boundary: str,
    proof_kind: str,
) -> None:
    record = _fixture(COMPLETE_FIXTURE)["records"][0]
    key = "component_proofs" if proof_kind == "component" else "gate_proofs"
    raw = record[key][0]
    observed_at = (
        "2026-08-23T23:59:59.999999+00:00"
        if boundary == "before"
        else "2026-08-26T00:00:00.000000+00:00"
    )
    mutated = dict(raw, source_rows=[dict(raw["source_rows"][0], observed_at=observed_at)])

    with pytest.raises(ValueError, match="window"):
        (_component if proof_kind == "component" else _gate)(mutated)


@pytest.mark.parametrize("proof_kind", ["component", "gate"])
def test_row_window_accepts_rows_on_inclusive_bounds(proof_kind: str) -> None:
    record = _fixture(COMPLETE_FIXTURE)["records"][0]
    key = "component_proofs" if proof_kind == "component" else "gate_proofs"
    raw = record[key][0]
    source_rows = [
        {"row_id": "window-start-row", "observed_at": raw["window_start"]},
        {"row_id": "window-end-row", "observed_at": raw["window_end"]},
    ]

    (_component if proof_kind == "component" else _gate)(dict(raw, source_rows=source_rows))


def test_task2_never_calls_the_scorer_while_any_authority_is_unavailable(monkeypatch) -> None:
    """Break caught: a score produced from authority that does not exist.

    The old form asserted the scorer and the independent recomputation agreed
    on a complete proof. No proof can be complete under the first registry, so
    the durable requirement is the stronger one: score_signal is not called at
    all. A number here would be manufactured, not measured.
    """
    import src.analysis.open_intelligence.scoring as scoring_module

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("score_signal was called while authority was unavailable")

    monkeypatch.setattr(scoring_module, "score_signal", refuse)
    monkeypatch.setattr(qualification, "score_signal", refuse, raising=False)

    record = qualification.build_scoring_proof(_scoring_bundle(), CANDIDATE_ID)
    assert record.mathematical_proof.scorer_decision_strength is None
    assert record.mathematical_proof.independent_decision_strength is None
    assert record.missing_reasons


@pytest.mark.parametrize(
    ("factor", "expected_reason"),
    [
        ("velocity", "velocity_formula_unapproved"),
        ("breadth", "breadth_formula_unapproved"),
        ("source_independence", "source_independence_formula_unapproved"),
        ("source_integrity", "source_integrity_formula_unapproved"),
        ("geo_confidence", "geo_confidence_formula_unapproved"),
        ("qualifying_source_families", "qualifying_source_family_policy_unapproved"),
        ("qualifying_current_receipts", "qualifying_receipt_policy_unapproved"),
        ("evidence_state", "evidence_state_source_unavailable"),
        ("foreign_market_sample_reviewed", "foreign_market_review_design_unapproved"),
        ("foreign_market_leakage", "foreign_market_review_design_unapproved"),
        ("duplicate_identity", "duplicate_identity_universe_incomplete"),
        ("factual_conflict", "factual_conflict_provider_unapproved"),
        ("directional_conflict", "directional_conflict_provider_unapproved"),
        ("membership_receipts_complete", "membership_receipt_universe_incomplete"),
        ("cluster_build_version", "cluster_build_version_source_unavailable"),
    ],
)
def test_task2_missing_authority_returns_deterministic_unavailable_without_zero(
    factor: str,
    expected_reason: str,
) -> None:
    """Every factor is unavailable, deterministically, and never as a zero.

    The old form removed one caller-supplied proof at a time to see which
    reason came back. There is no caller-supplied proof any more, so the same
    question is asked of the registry: each factor names exactly why it cannot
    be measured, and carries no value that could be mistaken for a measurement.
    """
    record = qualification.build_scoring_proof(_scoring_bundle(), CANDIDATE_ID)

    assert record.mathematical_proof.trust_gate_eligible is False
    assert record.mathematical_proof.evidence_strength is None
    assert record.mathematical_proof.scorer_decision_strength is None
    assert record.mathematical_proof.independent_decision_strength is None
    assert record.mathematical_proof.scorer_matches_independent is None
    assert expected_reason in record.missing_reasons
    assert record.missing_reasons == tuple(sorted(record.missing_reasons))

    affected = next(
        item for item in (*record.component_proofs, *record.gate_proofs) if item.name == factor
    )
    assert affected.state == "unavailable"
    assert affected.missing_reason == expected_reason
    # Unavailable carries no result at all. A zero here would read as measured.
    assert getattr(affected, "value", None) is None
    assert getattr(affected, "observed_value", None) is None
    assert getattr(affected, "numerator", None) is None
    assert getattr(affected, "denominator", None) is None


def test_task2_rejects_invalid_bundle_before_authority_access() -> None:
    with pytest.raises(replay.IncompleteReplayInput, match="bundle"):
        qualification.build_scoring_proof(object(), CANDIDATE_ID)


def test_task2_refuses_a_future_row_in_the_validated_source(monkeypatch) -> None:
    """Break caught: evidence dated after the cutoff reaching a score.

    The old form put a future timestamp on a caller-supplied row. Callers
    supply no rows now, so the same rule is asserted where it actually lives:
    the bundle validator refuses future source rows before scoring sees them.
    """
    bundle = _scoring_bundle()
    snapshot = replay_tests.mutable_tree(bundle.source_snapshot)
    future = datetime.combine(bundle.cutoff + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    snapshot["rows_by_table"]["enriched_content"][0]["collected_at"] = future.isoformat()
    with pytest.raises((replay.FutureLeakageDetected, replay.IncompleteReplayInput)):
        qualification.build_scoring_proof(replace(bundle, source_snapshot=snapshot), CANDIDATE_ID)


@pytest.mark.parametrize("field", ["source_rows", "row_references", "observed_rows"])
def test_task2_refuses_any_caller_supplied_row_reference(field: str) -> None:
    """There is no caller row path. Naming one is the violation."""
    bundle = _scoring_bundle()
    authority = _authority(bundle)
    authority[field] = (
        qualification.ScoringRowReference(
            row_id="shared",
            observed_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        ),
    )
    with pytest.raises(ValueError):
        qualification.build_scoring_proof(_with_authority(bundle, authority), CANDIDATE_ID)


def test_task2_rejects_altered_snapshot_digest() -> None:
    bundle = _scoring_bundle()
    snapshot = replay_tests.mutable_tree(bundle.source_snapshot)
    snapshot["rows_by_table"]["enriched_content"][0]["market"] = "ng"

    with pytest.raises(replay.IncompleteReplayInput, match="digest"):
        qualification.build_scoring_proof(replace(bundle, source_snapshot=snapshot), CANDIDATE_ID)


@pytest.mark.parametrize(
    "mutation",
    [
        "signal_identity",
        "mixed_market",
        "regional_score",
        "recurrence_state",
        "outcomes",
        "component_proofs",
        "gate_proofs",
        "scoring_sources",
    ],
)
def test_task2_rejects_altered_or_prohibited_authority(mutation: str) -> None:
    """Graph identity only, and nothing a caller adds to it is trusted.

    The first five cases are unchanged in intent: a wrong signal, a wrong
    market and any prohibited extra field are refused. The last three replace
    the retired numerator, denominator and window mutations, because those
    fields no longer reach this function at all: reattaching the value path is
    itself the violation now.
    """
    bundle = _scoring_bundle()
    authority = _authority(bundle)
    if mutation == "signal_identity":
        authority["signal_id"] = "sig_" + "f" * 64
    elif mutation == "mixed_market":
        authority["market"] = "ng"
    elif mutation == "component_proofs":
        authority["component_proofs"] = ()
    elif mutation == "gate_proofs":
        authority["gate_proofs"] = ()
    elif mutation == "scoring_sources":
        authority["scoring_sources"] = {"velocity": Fraction(1, 10)}
    else:
        authority[mutation] = "prohibited"

    with pytest.raises(ValueError):
        qualification.build_scoring_proof(_with_authority(bundle, authority), CANDIDATE_ID)


def test_task2_reverse_ordering_preserves_lineage_and_record_digest() -> None:
    forward = qualification.build_scoring_proof(_scoring_bundle(), CANDIDATE_ID)
    reversed_record = qualification.build_scoring_proof(_scoring_bundle(reverse=True), CANDIDATE_ID)

    assert reversed_record == forward
    assert reversed_record.source_snapshot_digest == forward.source_snapshot_digest
    assert reversed_record.provider_bundle_digest == forward.provider_bundle_digest
    assert reversed_record.rule_candidate_digest == forward.rule_candidate_digest
    assert reversed_record.canonical_sha256 == forward.canonical_sha256


def test_task2_record_cannot_gain_certification_or_recommendation() -> None:
    record = qualification.build_scoring_proof(_scoring_bundle(), CANDIDATE_ID)

    with pytest.raises(TypeError):
        replace(record, certified=True)
    with pytest.raises(TypeError):
        replace(record, recommendation="promote")


def test_fixture_output_remains_uncertified_pending_fixture_dry_run() -> None:
    for fixture_path in (COMPLETE_FIXTURE, UNAVAILABLE_FIXTURE):
        batch = _batch(_fixture(fixture_path))
        assert batch.mode == "fixture_dry_run"
        assert batch.certified is False
        assert batch.recommendation is None
        assert batch.promotion_authorized is False
        assert batch.human_review_state == "pending"


def test_pure_module_imports_no_live_boundary() -> None:
    source = inspect.getsource(qualification)
    tree = ast.parse(source)
    imported_modules = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    imports = {module.split(".")[0] for module in imported_modules}

    assert imports <= {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "fractions",
        "hashlib",
        "json",
        "re",
        "scripts",
        "src",
        # Pure stdlib, no live boundary. MappingProxyType is what makes the
        # formula registry genuinely immutable rather than immutable by
        # convention.
        "types",
        "typing",
    }
    assert {module for module in imported_modules if module.startswith(("scripts.", "src."))} == {
        "scripts.staging.replay_open_intelligence",
        "src.analysis.open_intelligence.scoring",
    }
    assert not any(
        token in module
        for module in imported_modules
        for token in ("bigquery", "google.cloud", "model", "pipeline", "subprocess", "requests")
    )


# --- Task 2: source authority -------------------------------------------------
#
# The blocking review changed velocity from 1/10 to 2/10 and current qualifying
# receipts from 5/8 to 4/8 while the cited rows and the source snapshot stayed
# identical, and both were accepted. Nothing in the old path proved the
# numerator came from the rows it cited, only that the fraction was internally
# coherent. These tests hold that door shut.


def _with_component_proofs(
    bundle: replay.ReplayInputBundle,
    numerator: int,
    denominator: int,
) -> replay.ReplayInputBundle:
    """Reattach the retired value path, carrying one drifted ratio."""
    raw = _fixture(COMPLETE_FIXTURE)["records"][0]
    source_rows = (
        qualification.ScoringRowReference(
            row_id="shared",
            observed_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        ),
    )
    components = []
    for item in raw["component_proofs"]:
        proof = replace(_component(item), source_rows=source_rows)
        if proof.name == "velocity":
            proof = _unsafe_replace(
                proof,
                value=Fraction(numerator, denominator),
                numerator=numerator,
                denominator=denominator,
            )
        components.append(proof)
    authority = _authority(bundle)
    authority["component_proofs"] = tuple(components)
    return _with_authority(bundle, authority)


def test_the_caller_supplied_value_path_no_longer_exists() -> None:
    """Break caught: the retired proof-input path surviving anywhere.

    The blocking review moved velocity from 1/10 to 2/10 with the cited rows
    and source snapshot unchanged, and it was accepted. There is now no value
    field to move: attaching the old path at all is refused, drifted or not.
    """
    for numerator, denominator in ((1, 10), (2, 10)):
        with pytest.raises(ValueError):
            qualification.build_scoring_proof(
                _with_component_proofs(_scoring_bundle(), numerator, denominator),
                CANDIDATE_ID,
            )


def test_every_factor_and_gate_is_unavailable_under_the_first_registry() -> None:
    record = qualification.build_scoring_proof(_scoring_bundle(), CANDIDATE_ID)
    assert all(item.state == "unavailable" for item in record.component_proofs)
    assert all(item.state == "unavailable" for item in record.gate_proofs)
    assert all(item.value is None for item in record.component_proofs)
    assert record.mathematical_proof.trust_gate_eligible is False
    assert record.mathematical_proof.scorer_decision_strength is None
    # Unavailable is not zero. No factor may present a measured zero here.
    assert not any(
        item.numerator == 0 and item.state == "measured" for item in record.component_proofs
    )


def test_missing_reasons_name_every_unapproved_decision() -> None:
    record = qualification.build_scoring_proof(_scoring_bundle(), CANDIDATE_ID)
    for reason in (
        "velocity_formula_unapproved",
        "breadth_formula_unapproved",
        "source_independence_formula_unapproved",
        "source_integrity_formula_unapproved",
        "geo_confidence_formula_unapproved",
        "qualifying_source_family_policy_unapproved",
        "qualifying_receipt_policy_unapproved",
        "shared_run_receipt_unavailable",
    ):
        assert reason in record.missing_reasons, reason
    assert record.missing_reasons == tuple(sorted(set(record.missing_reasons)))


def test_build_scoring_proof_no_longer_accepts_caller_rule_authority() -> None:
    """Break caught: caller-supplied rules standing in for approved policy."""
    signature = inspect.signature(qualification.build_scoring_proof)
    for forbidden in ("rules", "receipt_caps", "geo_floors"):
        assert forbidden not in signature.parameters, f"caller can still supply {forbidden}"


def test_component_and_gate_proofs_are_output_only_and_never_bundle_inputs() -> None:
    """Break caught: the old value path surviving inside components_by_candidate."""
    bundle = _scoring_bundle()
    authority = _authority(bundle)
    assert "component_proofs" not in authority or not authority["component_proofs"], (
        "components_by_candidate must return to a graph-only shape"
    )


def test_task2_refuses_a_scoring_source_bundle_until_its_producer_is_approved() -> None:
    """Break caught: a partial source bundle read as unavailable instead of invalid.

    Unavailable and invalid are different answers. An absent bundle is
    unavailable and honest. A bundle that is present but cannot be complete,
    because no approved producer exists to fill it, is invalid, and must not be
    quietly absorbed into a missing reason.
    """
    bundle = _scoring_bundle()
    attached = replace(
        bundle,
        scoring_sources_by_candidate={
            CANDIDATE_ID: {"contract_version": "replay_scoring_source_v1"}
        },
    )
    with pytest.raises(ValueError, match="source"):
        qualification.build_scoring_proof(attached, CANDIDATE_ID)


def test_task2_an_absent_scoring_source_is_unavailable_not_invalid() -> None:
    bundle = _scoring_bundle()
    assert bundle.scoring_sources_by_candidate is None
    record = qualification.build_scoring_proof(bundle, CANDIDATE_ID)
    assert "shared_run_receipt_unavailable" in record.missing_reasons
    # An empty mapping, and a mapping without this candidate, are equally
    # unavailable rather than errors.
    for sources in ({}, {"sig_" + "f" * 64: {}}):
        other = qualification.build_scoring_proof(
            replace(bundle, scoring_sources_by_candidate=sources), CANDIDATE_ID
        )
        assert other.missing_reasons == record.missing_reasons


# --- Approved raw factor formulas ---------------------------------------------
#
# Approved by 42-raw-factor-formula-approval-request-2026-08-29.md: seven
# formula rows and three authority constants. Every value is an exact set
# ratio over content-addressed citing-evidence units; an empty denominator is
# unavailable, never zero; novelty stays unapproved until its own sentence.


def test_the_approved_registry_names_the_seven_formulas_and_constants(monkeypatch):
    # Rewritten 5 Sep 2026. The approval request this registry mirrors is not
    # a committed file and the contract doc does not name the formula ids, so
    # the old retyped literals had no independent source. The formula map is
    # now checked against the components the derivation actually produces,
    # and each authority constant against the scoring behaviour it drives at
    # its own boundary, read from the registry rather than spelled again.
    formulas = qualification.APPROVED_COMPONENT_FORMULAS
    derived = qualification.derive_component_factor_ratios(_factor_evidence())
    assert set(formulas) == set(derived)
    assert all(isinstance(fid, str) and fid for fid in formulas.values())
    assert len(set(formulas.values())) == len(formulas)

    rules = qualification.approved_decision_strength_rules()
    values = {
        "signal_id": "sig_" + "a" * 64,
        "market": "za",
        "signal_date": date(2026, 8, 27),
        "discovery_mode": "replay",
        "evidence_state": "ready",
        "qualifying_source_families": 4,
        "qualifying_current_receipts": 2,
        "source_integrity": 1.0,
        "velocity": 0.5,
        "breadth": 0.5,
        "source_independence": 0.5,
        "geo_confidence": 0.9,
        "foreign_market_sample_reviewed": True,
        "foreign_market_leakage": 0,
        "duplicate_identity": False,
        "factual_conflict": False,
        "directional_conflict": False,
        "membership_receipts_complete": True,
        "cluster_build_version": next(iter(qualification.APPROVED_CLUSTER_VERSIONS)),
    }

    def scored(**over):
        return score_signal(SignalScoreInput(**{**values, **over}), rules)

    # Receipt cap: evidence strength saturates exactly at the cap and not below it.
    cap = rules.approved_receipt_cap
    assert scored(qualifying_current_receipts=cap).evidence_strength == 1.0
    assert scored(qualifying_current_receipts=cap + 1).evidence_strength == 1.0
    assert scored(qualifying_current_receipts=cap - 1).evidence_strength < 1.0

    # Geo floors: a candidate sitting on its market floor passes the gate and
    # the next float below it fails, for every market the registry names.
    for market, floor in rules.geo_floor_by_market.items():
        on_floor = scored(market=market, geo_confidence=floor)
        below = scored(market=market, geo_confidence=math.nextafter(floor, 0.0))
        assert on_floor.promotion_eligible is True
        assert "geo_below_market_floor" in below.reasons

    # Cluster authority: every approved version is admitted, an unknown one is not.
    for version in qualification.APPROVED_CLUSTER_VERSIONS:
        assert scored(cluster_build_version=version).promotion_eligible is True
    assert "cluster_build_unapproved" in scored(cluster_build_version="unapproved_v0").reasons

    # Novelty: the registry entry is the gate that lets metrics exist at all.
    assert isinstance(qualification.NOVELTY_FORMULA_ID, str)
    assert qualification.NOVELTY_FORMULA_ID
    metrics, reasons = qualification.derive_component_factor_metrics(_factor_evidence())
    assert metrics is not None
    assert reasons == ()
    monkeypatch.setattr(qualification, "NOVELTY_FORMULA_ID", None)
    metrics, reasons = qualification.derive_component_factor_metrics(_factor_evidence())
    assert metrics is None
    assert "novelty_formula_unapproved" in reasons


def test_approved_rules_refuse_v1_and_admit_v2_cluster_authority():
    rules = qualification.approved_decision_strength_rules()
    values = {
        "signal_id": "sig_" + "a" * 64,
        "market": "za",
        "signal_date": date(2026, 8, 27),
        "discovery_mode": "replay",
        "evidence_state": "ready",
        "qualifying_source_families": 2,
        "qualifying_current_receipts": 2,
        "source_integrity": 1.0,
        "velocity": 0.5,
        "breadth": 0.5,
        "source_independence": 0.5,
        "geo_confidence": 0.9,
        "foreign_market_sample_reviewed": True,
        "foreign_market_leakage": 0,
        "duplicate_identity": False,
        "factual_conflict": False,
        "directional_conflict": False,
        "membership_receipts_complete": True,
    }
    rejected = score_signal(
        SignalScoreInput(**values, cluster_build_version="hybrid_graph_v1"), rules
    )
    admitted = score_signal(
        SignalScoreInput(**values, cluster_build_version="hybrid_graph_v2"), rules
    )
    assert rejected.promotion_eligible is False
    assert "cluster_build_unapproved" in rejected.reasons
    assert admitted.promotion_eligible is True


def _factor_evidence(**over):
    values = {
        "current_rows": ("row_a", "row_b"),
        "window_rows": ("row_a", "row_b", "row_h1", "row_h2"),
        "families_current": ("news", "youtube"),
        "family_universe": ("music", "news", "reddit", "youtube"),
        "creators_current": ("@one", "@two"),
        "clean_current": ("row_a",),
        "geo_confirmed_current": ("row_a", "row_b"),
        "geo_best_regional_score": 1.0,
        "current_member_terms": ("arsenal",),
        "history_member_terms": (),
    }
    values.update(over)
    return qualification.ComponentFactorEvidence(**values)


def test_measured_factors_are_exact_set_ratios():
    metrics, reasons = qualification.derive_component_factor_metrics(_factor_evidence())
    assert reasons == ()
    assert metrics is not None
    assert metrics["velocity_score"] == 0.5
    assert metrics["novelty_score"] == 1.0
    assert metrics["historical_similarity"] is None


def test_ratios_are_exact_fractions_of_the_unit_sets():
    factors = qualification.derive_component_factor_ratios(_factor_evidence())
    assert factors["velocity"] == Fraction(2, 4)
    assert factors["breadth"] == Fraction(2, 4)
    assert factors["source_independence"] == Fraction(2, 2)
    assert factors["source_integrity"] == Fraction(1, 2)
    assert factors["geo_confidence"] == Fraction(2, 2)


def test_an_empty_denominator_is_unavailable_never_zero():
    evidence = _factor_evidence(
        current_rows=(),
        window_rows=(),
        families_current=(),
        creators_current=(),
        clean_current=(),
        geo_confirmed_current=(),
    )
    metrics, reasons = qualification.derive_component_factor_metrics(evidence)
    assert metrics is None
    assert "no_citing_evidence_in_window" in reasons


def test_a_numerator_outside_its_denominator_refuses():
    with pytest.raises(ValueError):
        # Only the current-within-window relation is violated: every other
        # set is adjusted to stay internally consistent, so no sibling check
        # can fire in this one's place.
        qualification.derive_component_factor_ratios(
            _factor_evidence(
                current_rows=("row_a", "row_x"),
                clean_current=("row_a",),
                geo_confirmed_current=("row_a",),
            )
        )
    with pytest.raises(ValueError):
        qualification.derive_component_factor_ratios(_factor_evidence(clean_current=("row_zz",)))


def test_geo_confidence_is_the_best_citing_post_regional_score():
    # Ruled by Albert on 4 Sep 2026: the geo gate reads the best citing post's
    # regional score against the market floor, on regional_score's own scale.
    # The share of rows carrying any marker (20 of 79 on the first ready
    # signal) is not the quantity the floor was approved on.
    evidence = qualification.ComponentFactorEvidence(
        current_rows=("row_a", "row_b"),
        window_rows=("row_a", "row_b", "row_c", "row_d"),
        families_current=("news", "youtube"),
        family_universe=("news", "youtube", "reddit", "search"),
        creators_current=("@a", "@b"),
        clean_current=("row_a",),
        geo_confirmed_current=("row_a",),
        current_member_terms=("johannesburg",),
        history_member_terms=(),
        geo_best_regional_score=0.75,
    )
    factors = qualification.derive_component_factor_ratios(evidence)
    assert factors["geo_confidence"] == Fraction(3, 4)
    metrics, reasons = qualification.derive_component_factor_metrics(evidence)
    assert reasons == ()
    assert metrics["geo_confidence"] == 0.75
    # A window with no scored post reads as zero, never as unavailable.
    unscored = qualification.ComponentFactorEvidence(
        current_rows=("row_a",),
        window_rows=("row_a",),
        families_current=("news",),
        family_universe=("news",),
        creators_current=("@a",),
        clean_current=("row_a",),
        geo_confirmed_current=(),
        current_member_terms=("x",),
        history_member_terms=(),
    )
    assert qualification.derive_component_factor_ratios(unscored)["geo_confidence"] == Fraction(0)
    with pytest.raises(ValueError):
        qualification.ComponentFactorEvidence(
            current_rows=("row_a",),
            window_rows=("row_a",),
            families_current=("news",),
            family_universe=("news",),
            creators_current=("@a",),
            clean_current=("row_a",),
            geo_confirmed_current=(),
            current_member_terms=("x",),
            history_member_terms=(),
            geo_best_regional_score=1.5,
        )
