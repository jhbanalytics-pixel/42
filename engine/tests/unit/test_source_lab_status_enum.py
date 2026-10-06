"""The Source Lab status enum holds across every layer that carries it.

Ruled 5 Sep 2026: rejected, named by every contract layer and produced by nothing,
is gone; permanently_rejected, the Wave 1 kill test verdict the writer produces,
is in. This test fails if any two layers drift: the contract tuple, the fixture
module's set, the frozen fixture, the schema description, the migration repair
that carries the description to staging, the writer gate, the yield reader and
the contract doc.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.migrations import create_open_intelligence_v2 as migration
from src.analysis.open_intelligence import source_lab_persistence, source_yield
from src.contracts import open_intelligence_fixtures as fixtures
from src.contracts.open_intelligence import ROUTE_ROLES, SOURCE_LAB_STATUSES

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "infra" / "bigquery_schemas" / "source_performance_daily_v2.sql"
DOC = ROOT / "docs" / "contracts" / "open_intelligence_v2.md"
FIXTURE = ROOT / "tests" / "fixtures" / "open_intelligence" / "v2" / "source_lab_all_statuses.json"
STATUSES = frozenset(SOURCE_LAB_STATUSES)


def _description(column: str) -> str:
    pattern = rf"^\s+`?{column}`?\s+\w+(?: NOT NULL)? OPTIONS\(description = '([^']*)'\)"
    match = re.search(pattern, SCHEMA.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, column
    return match.group(1)


def _enum_from_sentence(sentence: str) -> frozenset[str]:
    words = sentence.rstrip(".").replace(", or ", ", ").replace(" or ", ", ").split(", ")
    return frozenset(word.strip().lower() for word in words)


def _doc_row(column: str) -> str:
    rows = [
        line
        for line in DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"| `{column}` |")
    ]
    assert len(rows) == 1, column
    return rows[0]


def _doc_enum(column: str) -> frozenset[str]:
    return frozenset(re.findall(r"`([a-z_]+)`", _doc_row(column).split("|")[4]))


def test_the_enum_names_the_measured_kill_verdict_and_not_the_reviewed_rejection() -> None:
    assert "permanently_rejected" in STATUSES
    assert "rejected" not in STATUSES
    assert len(SOURCE_LAB_STATUSES) == len(STATUSES)


def test_contract_tuple_and_fixture_module_agree() -> None:
    assert fixtures.SOURCE_STATUSES == STATUSES
    assert frozenset(ROUTE_ROLES) == fixtures.SOURCE_ROUTE_ROLES


def test_schema_description_and_migration_repair_carry_the_same_enum() -> None:
    assert _enum_from_sentence(_description("status")) == STATUSES
    plan = migration.build_source_lab_plan()
    repairs = [
        sql
        for sql in plan.source_lab_upgrade_statements
        if "ALTER COLUMN status SET OPTIONS" in sql
    ]
    assert len(repairs) == 1
    assert f"description = '{_description('status')}'" in repairs[0]
    for column in ("kill_test_result", "review_date", "rows", "unique_lift"):
        carried = [
            sql
            for sql in plan.source_lab_upgrade_statements
            if f"ALTER COLUMN {column} SET OPTIONS" in sql.replace("`", "")
        ]
        assert len(carried) == 1, column
        assert f"description = '{_description(column)}'" in carried[0]
    assert "rejected" not in _description("kill_test_result").replace("permanently_rejected", "")
    assert "permanently_rejected" in _description("kill_test_result")


def test_writer_gate_and_yield_reader_stay_inside_the_enum() -> None:
    assert source_lab_persistence.WRITABLE_STATUSES <= STATUSES
    assert "permanently_rejected" in source_lab_persistence.WRITABLE_STATUSES
    buckets = (
        source_yield._MEASURED_STATUSES,
        source_yield._INVENTORY_STATUSES,
        source_yield._INELIGIBLE_STATUSES,
    )
    assert frozenset().union(*buckets) == STATUSES
    assert sum(len(bucket) for bucket in buckets) == len(STATUSES)


def test_frozen_fixture_carries_one_row_per_status_with_the_kill_shape() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]
    rows = {row["status"]: row for row in payload["sources"]}
    assert frozenset(rows) == STATUSES
    assert len(payload["sources"]) == payload["synthetic_route_count"] == len(STATUSES)
    killed = rows["permanently_rejected"]
    assert killed["blocking_reason"] in fixtures.KILL_TEST_REASONS
    assert killed["kill_test_result"] == killed["blocking_reason"]
    assert killed["review_date"] is None
    assert not (killed["rows"] > 0 and killed["unique_lift"] > 0)
    assert killed["blocking_reason"] != "zero_unique_observations" or killed["rows"] == 0


def test_fixture_module_accepts_every_kill_shape_the_engine_writes() -> None:
    # source_lab.build_source_performance_rows writes rows = unique_observations and
    # unique_lift = marginal_candidates + marginal_evidence; evaluate_wave1_source_value
    # kills when either is zero, so one of the pair may be positive.
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]
    killed = next(row for row in payload["sources"] if row["status"] == "permanently_rejected")
    marginal_zero = {
        **killed,
        "rows": 12,
        "unique_lift": 0.0,
        "blocking_reason": "zero_marginal_downstream_rows",
        "kill_test_result": "zero_marginal_downstream_rows",
    }
    both_positive = {**marginal_zero, "unique_lift": 3.0}
    wrong_reason = {
        **marginal_zero,
        "blocking_reason": "zero_unique_observations",
        "kill_test_result": "zero_unique_observations",
    }
    for candidate, ok in ((marginal_zero, True), (both_positive, False), (wrong_reason, False)):
        trial = json.loads(json.dumps(payload))
        trial["sources"] = [
            candidate if row["status"] == "permanently_rejected" else row
            for row in trial["sources"]
        ]
        try:
            fixtures._validate_source_lab("source_lab_all_statuses", trial)
        except fixtures.FixtureValidationError:
            assert not ok, candidate
        else:
            assert ok, candidate


def test_contract_doc_names_the_same_enums_as_the_code() -> None:
    assert _doc_enum("status") == STATUSES
    assert _doc_enum("route_role") == frozenset(ROUTE_ROLES)
    assert "permanently_rejected" in _doc_row("kill_test_result")
    assert "rejected" not in _doc_row("review_date").replace("reviewed rejection", "")
    text = DOC.read_text(encoding="utf-8")
    assert "| `permanently_rejected` |" in text
    assert "| `rejected` |" not in text
    summary = [
        line
        for line in text.splitlines()
        if line.startswith("| Source Lab statuses and metric restrictions |")
    ]
    assert len(summary) == 1
    spelled = summary[0].split("|")[2].strip().rstrip(".")
    assert (
        frozenset(word.strip().lower().replace(" ", "_") for word in spelled.split(",")) == STATUSES
    )
