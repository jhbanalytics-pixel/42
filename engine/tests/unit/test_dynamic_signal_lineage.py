"""Boundary tests for deterministic dynamic signal lineage rows."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.graph import SignalComponent
from src.analysis.open_intelligence.lineage import (
    LINEAGE_ROW_FIELDS,
    LineageRules,
    SignalSnapshot,
    build_signal_lineage_rows,
)
from src.contracts.open_intelligence import ResolvedScope

ROOT = Path(__file__).resolve().parents[2]
SIGNAL_A = "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SIGNAL_B = "sig_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
SIGNAL_C = "sig_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
SIGNAL_D = "sig_dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
SIGNAL_DATE = date(2026, 8, 26)
CREATED_AT = datetime(2026, 8, 26, 6, 44, tzinfo=UTC)
FROZEN_MANIFEST_SHA256 = "3440cd263099157f43e107ea55a6a56c835bbc1a5e7e86ef8519ce1abd181ebf"
FIXTURE_SIGNAL_A = "sig_1111111111111111111111111111111111111111111111111111111111111111"
FIXTURE_SIGNAL_B = "sig_2222222222222222222222222222222222222222222222222222222222222222"
FIXTURE_SIGNAL_C = "sig_3333333333333333333333333333333333333333333333333333333333333333"
FIXTURE_DATE = date(2026, 8, 25)
FIXTURE_CREATED_AT = datetime(2026, 8, 25, 6, 44, tzinfo=UTC)
MERGE_EDGES = (
    {
        "audience_lens_ids": [],
        "brand_config_id": "fixture_brand",
        "client_scope_id": "fixture_scope",
        "contract_version": "2.0.0",
        "created_at": "2026-08-25T06:44:00Z",
        "from_signal_id": "sig_1111111111111111111111111111111111111111111111111111111111111111",
        "market": "za",
        "market_scope": ["za"],
        "overlap_score": 0.81,
        "relation": "merges_into",
        "run_id": "run_fixture_lineage_001",
        "signal_date": "2026-08-25",
        "theme_id": "fixture_theme",
        "to_signal_id": "sig_3333333333333333333333333333333333333333333333333333333333333333",
    },
    {
        "audience_lens_ids": [],
        "brand_config_id": "fixture_brand",
        "client_scope_id": "fixture_scope",
        "contract_version": "2.0.0",
        "created_at": "2026-08-25T06:44:00Z",
        "from_signal_id": "sig_2222222222222222222222222222222222222222222222222222222222222222",
        "market": "za",
        "market_scope": ["za"],
        "overlap_score": 0.77,
        "relation": "merges_into",
        "run_id": "run_fixture_lineage_001",
        "signal_date": "2026-08-25",
        "theme_id": "fixture_theme",
        "to_signal_id": "sig_3333333333333333333333333333333333333333333333333333333333333333",
    },
)
SPLIT_EDGES = (
    {
        "audience_lens_ids": [],
        "brand_config_id": "fixture_brand",
        "client_scope_id": "fixture_scope",
        "contract_version": "2.0.0",
        "created_at": "2026-08-25T06:44:00Z",
        "from_signal_id": "sig_1111111111111111111111111111111111111111111111111111111111111111",
        "market": "za",
        "market_scope": ["za"],
        "overlap_score": 0.81,
        "relation": "splits_into",
        "run_id": "run_fixture_lineage_001",
        "signal_date": "2026-08-25",
        "theme_id": "fixture_theme",
        "to_signal_id": "sig_2222222222222222222222222222222222222222222222222222222222222222",
    },
    {
        "audience_lens_ids": [],
        "brand_config_id": "fixture_brand",
        "client_scope_id": "fixture_scope",
        "contract_version": "2.0.0",
        "created_at": "2026-08-25T06:44:00Z",
        "from_signal_id": "sig_1111111111111111111111111111111111111111111111111111111111111111",
        "market": "za",
        "market_scope": ["za"],
        "overlap_score": 0.77,
        "relation": "splits_into",
        "run_id": "run_fixture_lineage_001",
        "signal_date": "2026-08-25",
        "theme_id": "fixture_theme",
        "to_signal_id": "sig_3333333333333333333333333333333333333333333333333333333333333333",
    },
)


def scope() -> ResolvedScope:
    return ResolvedScope(
        client_scope_id="fixture_scope",
        market_scope=("za",),
        brand_config_id="fixture_brand",
        audience_lens_ids=(),
        theme_id="fixture_theme",
        run_id="run_fixture_lineage_001",
        contract_version="2.0.0",
    )


def snapshot(signal_id: str, signal_date: date, *members: str) -> SignalSnapshot:
    return SignalSnapshot(
        signal_id=signal_id,
        signal_date=signal_date,
        component=SignalComponent(
            market="za",
            member_identities=tuple(f"za|keyword|{member}" for member in members),
            terms=tuple(members),
            row_receipts=tuple(f"row_{member}" for member in members),
            source_families=("news", "search"),
            platforms=("search", "web"),
            creator_ids=(),
            label=members[0],
        ),
    )


def build(*, prior: object, current: object, rules: LineageRules | None = None):
    return build_signal_lineage_rows(
        prior_signals=prior,
        current_signals=current,
        scope=scope(),
        signal_date=SIGNAL_DATE,
        created_at=CREATED_AT,
        rules=rules or LineageRules(overlap_floor=0.5),
    )


def schema_fields(filename: str) -> tuple[str, ...]:
    schema = (ROOT / "infra" / "bigquery_schemas" / filename).read_text(encoding="utf-8")
    column_block = schema.split(")\nPARTITION BY", maxsplit=1)[0]
    return tuple(re.findall(r"^  ([a-z_]+) ", column_block, re.M))


def fixture(name: str) -> dict[str, object]:
    path = ROOT / "tests" / "fixtures" / "open_intelligence" / "v2" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_scope() -> ResolvedScope:
    return ResolvedScope(
        client_scope_id="fixture_scope",
        market_scope=("za",),
        brand_config_id="fixture_brand",
        audience_lens_ids=(),
        theme_id="fixture_theme",
        run_id="run_fixture_lineage_001",
        contract_version="2.0.0",
    )


def fixture_snapshot(signal_id: str, signal_date: date, members: range) -> SignalSnapshot:
    member_values = tuple(f"member_{value:03d}" for value in members)
    return snapshot(signal_id, signal_date, *member_values)


def fixture_rows(rows: tuple[dict[str, object], ...]) -> list[dict[str, object]]:
    return [
        {
            key: value.isoformat().replace("+00:00", "Z")
            if isinstance(value, datetime)
            else value.isoformat()
            if isinstance(value, date)
            else value
            for key, value in row.items()
        }
        for row in rows
    ]


def test_lineage_schema_and_fixtures_are_immutable_literal_contracts() -> None:
    manifest = fixture("manifest")
    merge = fixture("lineage_merge")
    split = fixture("lineage_split")
    assert schema_fields("signal_lineage_v2.sql") == LINEAGE_ROW_FIELDS
    assert manifest["manifest_sha256"] == FROZEN_MANIFEST_SHA256
    assert merge["payload"] == {"edges": list(MERGE_EDGES)}
    assert split["payload"] == {"edges": list(SPLIT_EDGES)}


def test_lineage_builder_matches_the_exact_frozen_merge_and_split_payloads() -> None:
    merge_rows = build_signal_lineage_rows(
        prior_signals=(
            fixture_snapshot(FIXTURE_SIGNAL_A, date(2026, 8, 24), range(0, 81)),
            fixture_snapshot(FIXTURE_SIGNAL_B, date(2026, 8, 24), range(23, 100)),
        ),
        current_signals=(fixture_snapshot(FIXTURE_SIGNAL_C, FIXTURE_DATE, range(0, 100)),),
        scope=fixture_scope(),
        signal_date=FIXTURE_DATE,
        created_at=FIXTURE_CREATED_AT,
        rules=LineageRules(overlap_floor=0.5),
    )
    split_rows = build_signal_lineage_rows(
        prior_signals=(fixture_snapshot(FIXTURE_SIGNAL_A, date(2026, 8, 24), range(0, 100)),),
        current_signals=(
            fixture_snapshot(FIXTURE_SIGNAL_B, FIXTURE_DATE, range(0, 81)),
            fixture_snapshot(FIXTURE_SIGNAL_C, FIXTURE_DATE, range(23, 100)),
        ),
        scope=fixture_scope(),
        signal_date=FIXTURE_DATE,
        created_at=FIXTURE_CREATED_AT,
        rules=LineageRules(overlap_floor=0.5),
    )
    assert fixture_rows(merge_rows) == list(MERGE_EDGES)
    assert fixture_rows(split_rows) == list(SPLIT_EDGES)


def test_one_to_one_overlap_continues_with_exact_scope_and_schema_fields() -> None:
    prior = snapshot(SIGNAL_A, date(2026, 8, 25), "alpha", "beta", "gamma")
    current = snapshot(SIGNAL_A, SIGNAL_DATE, "alpha", "beta", "gamma", "delta")
    rows = build(prior=(prior,), current=(current,), rules=LineageRules(overlap_floor=0.75))
    assert rows == (
        {
            "client_scope_id": "fixture_scope",
            "market_scope": ["za"],
            "brand_config_id": "fixture_brand",
            "audience_lens_ids": [],
            "theme_id": "fixture_theme",
            "run_id": "run_fixture_lineage_001",
            "contract_version": "2.0.0",
            "signal_date": date(2026, 8, 26),
            "market": "za",
            "from_signal_id": "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "to_signal_id": "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "relation": "continues",
            "overlap_score": 0.75,
            "created_at": datetime(2026, 8, 26, 6, 44, tzinfo=UTC),
        },
    )


def test_two_prior_signals_merging_into_one_current_signal_match_fixture_relation() -> None:
    prior = (
        snapshot(SIGNAL_A, date(2026, 8, 25), "alpha", "beta"),
        snapshot(SIGNAL_B, date(2026, 8, 25), "gamma", "delta"),
    )
    current = snapshot(SIGNAL_C, SIGNAL_DATE, "alpha", "beta", "gamma", "delta")
    rows = build(prior=prior, current=(current,))
    assert [(row["from_signal_id"], row["to_signal_id"], row["relation"]) for row in rows] == [
        (
            "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sig_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "merges_into",
        ),
        (
            "sig_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "sig_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "merges_into",
        ),
    ]
    assert [row["overlap_score"] for row in rows] == [0.5, 0.5]


def test_one_prior_signal_splitting_into_two_current_signals_match_fixture_relation() -> None:
    prior = snapshot(SIGNAL_A, date(2026, 8, 25), "alpha", "beta", "gamma", "delta")
    current = (
        snapshot(SIGNAL_B, SIGNAL_DATE, "alpha", "beta"),
        snapshot(SIGNAL_C, SIGNAL_DATE, "gamma", "delta"),
    )
    rows = build(prior=(prior,), current=current)
    assert [(row["from_signal_id"], row["to_signal_id"], row["relation"]) for row in rows] == [
        (
            "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sig_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "splits_into",
        ),
        (
            "sig_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sig_cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "splits_into",
        ),
    ]


def test_floor_is_inclusive_and_insufficient_overlap_emits_no_relation() -> None:
    prior = snapshot(SIGNAL_A, date(2026, 8, 25), "alpha", "beta", "gamma", "delta")
    current = snapshot(SIGNAL_B, SIGNAL_DATE, "alpha", "beta", "gamma", "epsilon")
    assert (
        build(prior=(prior,), current=(current,), rules=LineageRules(overlap_floor=0.6))[0][
            "overlap_score"
        ]
        == 0.6
    )
    assert build(prior=(prior,), current=(current,), rules=LineageRules(overlap_floor=0.61)) == ()


def test_lineage_is_deterministic_under_input_reversal_without_duplicate_edges() -> None:
    prior = (
        snapshot(SIGNAL_A, date(2026, 8, 25), "alpha", "beta"),
        snapshot(SIGNAL_B, date(2026, 8, 25), "gamma", "delta"),
    )
    current = (
        snapshot(SIGNAL_C, SIGNAL_DATE, "alpha", "beta", "gamma", "delta"),
        snapshot(SIGNAL_D, SIGNAL_DATE, "epsilon", "zeta"),
    )
    forward = build(prior=prior, current=current)
    reverse = build(prior=tuple(reversed(prior)), current=tuple(reversed(current)))
    assert forward == reverse
    assert len(forward) == len(
        {(row["from_signal_id"], row["to_signal_id"], row["relation"]) for row in forward}
    )


def test_lineage_rejects_reversed_dates_scope_mismatch_and_ambiguous_many_to_many() -> None:
    prior = snapshot(SIGNAL_A, SIGNAL_DATE, "alpha", "beta")
    current = snapshot(SIGNAL_B, date(2026, 8, 25), "alpha", "beta")
    with pytest.raises(ValueError, match="earlier"):
        build(prior=(prior,), current=(current,))

    outside = SignalSnapshot(
        signal_id=SIGNAL_B,
        signal_date=SIGNAL_DATE,
        component=SignalComponent(
            market="ng",
            member_identities=("ng|keyword|alpha",),
            terms=("alpha",),
            row_receipts=("row_alpha",),
            source_families=("news", "search"),
            platforms=("search", "web"),
            creator_ids=(),
            label="alpha",
        ),
    )
    with pytest.raises(ValueError, match="market scope"):
        build(prior=(), current=(outside,))

    prior_pair = (
        snapshot(SIGNAL_A, date(2026, 8, 25), "alpha", "beta"),
        snapshot(SIGNAL_B, date(2026, 8, 25), "alpha", "beta"),
    )
    current_pair = (
        snapshot(SIGNAL_C, SIGNAL_DATE, "alpha", "beta"),
        snapshot(SIGNAL_D, SIGNAL_DATE, "alpha", "beta"),
    )
    with pytest.raises(ValueError, match="many-to-many"):
        build(prior=prior_pair, current=current_pair)
