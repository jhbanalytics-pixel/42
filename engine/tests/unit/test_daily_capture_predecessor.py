"""The capture stage's predecessor rule, in the Python validator and both routines.

The plan decides it:

* D04 (docs/plans/2026-09-12-42-data-intelligence.md:344): "Build each
  manifest from profile digest, operation ID, UTC cutoff and predecessor output digest";
* C03 (docs/plans/2026-09-12-42-plan-contracts.md:89): "Snapshot the admitted
  completed run at an instant that actually contains its writes".

So capture has a predecessor in both modes. ``validate_operation_context`` requires one,
and the routines agree: an initial capture names the succeeded exposure result of its own
slot, and a recovery names the failed initial capture of its own cutoff. The derive
routine applies the same predicate as the consume routine, so a capture that consume
would refuse is never derived and never reserves allowance.
"""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from src.analysis.open_intelligence.daily_execution_contracts import (
    DailyContractError,
    validate_operation_context,
)

from tests.unit.test_daily_derivation_authority import LEASE, authority, frame

ROOT = Path(__file__).resolve().parents[2]
CONSUME = ROOT / "infra/bigquery_routines/sp_consume_open_intelligence_daily_derivation_v1.sql"
DERIVE = ROOT / "infra/bigquery_routines/sp_derive_open_intelligence_daily_execution_v1.sql"
ROUTINES = pytest.mark.parametrize("routine", [CONSUME, DERIVE], ids=["consume", "derive"])
GRANT = "g" * 64
SLOT = "5" * 64
CUTOFF = "2026-09-23T00:00:00+00:00"
EXPOSURE = ("exr_" + "a" * 64, "b" * 64)
INITIAL = ("exr_" + "c" * 64, "d" * 64)


def _json_value(document, path):
    if document is None:
        return None
    value = json.loads(document)
    for key in path.removeprefix("$.").split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value if value is None or isinstance(value, str) else json.dumps(value)


def capture_predicate(routine=CONSUME):
    text = routine.read_text(encoding="utf-8")
    start = text.index("    ASSERT (v_mode='initial'")
    end = text.index("AS 'daily_capture_recovery_invalid';", start)
    predicate = text[start + len("    ASSERT ") : end]
    return re.sub(r"`\{project\}\.\{dataset\}\.([a-z0-9_]+)`", r"\1", predicate)


def evaluate(*, mode, predecessor, recovery="null", results=(), routine=CONSUME):
    context = {
        "cutoff_utc": CUTOFF,
        "predecessor_result_digest": predecessor[1],
        "predecessor_result_id": predecessor[0],
        "slot_id": SLOT,
    }
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _json_value)
        connection.executescript(
            """
            CREATE TABLE v_derivation(
              v_mode, v_capture_recovery, authorizing_grant_digest,
              canonical_operation_context_json);
            CREATE TABLE open_intelligence_execution_results_v2(
              result_id, result_digest, consumption_id, canonical_result_json);
            CREATE TABLE open_intelligence_execution_consumptions_v2(consumption_id, approval_id);
            CREATE TABLE open_intelligence_execution_derivations_v1(
              derivation_id, operation, authorizing_grant_digest, canonical_operation_context_json);
            """
        )
        raw = json.dumps(context)
        # One row carries the procedure's variables and parameter: the routine asserts
        # earlier that the parameter equals v_derivation.canonical_operation_context_json.
        connection.execute(
            "INSERT INTO v_derivation VALUES (?, ?, ?, ?)", (mode, recovery, GRANT, raw)
        )
        for index, row in enumerate(results):
            connection.execute(
                "INSERT INTO open_intelligence_execution_results_v2 VALUES (?, ?, ?, ?)",
                (
                    row["id"],
                    row["digest"],
                    f"c{index}",
                    json.dumps({"terminal_state": row["state"]}),
                ),
            )
            connection.execute(
                "INSERT INTO open_intelligence_execution_consumptions_v2 VALUES (?, ?)",
                (f"c{index}", f"d{index}"),
            )
            connection.execute(
                "INSERT INTO open_intelligence_execution_derivations_v1 VALUES (?, ?, ?, ?)",
                (
                    f"d{index}",
                    row["operation"],
                    row.get("grant", GRANT),
                    json.dumps(
                        {"cutoff_utc": row.get("cutoff", CUTOFF), "slot_id": row.get("slot", SLOT)}
                    ),
                ),
            )
        predicate = capture_predicate(routine)
        return connection.execute(f"SELECT ({predicate}) FROM v_derivation").fetchone()[0]


def exposure(**changes):
    row = {
        "id": EXPOSURE[0],
        "digest": EXPOSURE[1],
        "state": "succeeded",
        "operation": "daily_collection_exposure_issue",
    }
    row.update(changes)
    return row


def test_the_python_validator_requires_a_predecessor_for_an_initial_capture():
    subject_authority, _derive, _store, _objects = authority()
    prepared = subject_authority.prepare("release", frame("release"), lease=LEASE)
    context = dict(
        prepared["operation_context"],
        operation="daily_source_snapshot_capture",
        stage="capture",
        mode="initial",
        predecessor_result_id=None,
        predecessor_result_digest=None,
    )
    with pytest.raises(DailyContractError, match=r"^daily_context_predecessor_invalid$"):
        validate_operation_context(context)


@ROUTINES
def test_an_initial_capture_consumes_against_the_succeeded_exposure_of_its_slot(routine):
    assert (
        evaluate(mode="initial", predecessor=EXPOSURE, results=[exposure()], routine=routine) == 1
    )


@ROUTINES
def test_an_initial_capture_without_a_predecessor_refuses(routine):
    assert (
        evaluate(mode="initial", predecessor=(None, None), results=[exposure()], routine=routine)
        == 0
    )


@pytest.mark.parametrize(
    "row",
    [
        exposure(state="failed"),
        exposure(slot="6" * 64),
        exposure(operation="daily_source_collection"),
        exposure(digest="e" * 64),
    ],
)
@ROUTINES
def test_an_initial_capture_naming_anything_but_that_result_refuses(row, routine):
    assert evaluate(mode="initial", predecessor=EXPOSURE, results=[row], routine=routine) == 0


def recovery(result=INITIAL):
    return json.dumps(
        {
            "contract_version": "open_intelligence_daily_source_capture_recovery_v1",
            "initial_operation": "daily_source_snapshot_capture",
            "initial_result_digest": result[1],
            "initial_result_id": result[0],
        },
        sort_keys=True,
    )


def initial_capture(**changes):
    row = {
        "id": INITIAL[0],
        "digest": INITIAL[1],
        "state": "failed",
        "operation": "daily_source_snapshot_capture",
    }
    row.update(changes)
    return row


@ROUTINES
def test_a_recovery_capture_consumes_against_the_failed_initial_capture_of_its_cutoff(routine):
    assert (
        evaluate(
            mode="recover",
            predecessor=INITIAL,
            recovery=recovery(),
            results=[initial_capture()],
            routine=routine,
        )
        == 1
    )


@ROUTINES
def test_a_recovery_capture_naming_the_initial_capture_of_another_cutoff_refuses(routine):
    # The routine compared the initial derivation's cutoff with itself: inside the subquery
    # the unqualified context column resolved to the joined derivation's own column.
    row = initial_capture(cutoff="2026-09-22T00:00:00+00:00")
    assert (
        evaluate(
            mode="recover", predecessor=INITIAL, recovery=recovery(), results=[row], routine=routine
        )
        == 0
    )


def test_derive_applies_the_consume_predicate_word_for_word():
    assert capture_predicate(DERIVE) == capture_predicate(CONSUME)


def test_derive_checks_a_capture_predecessor_before_it_reserves_anything():
    text = DERIVE.read_text(encoding="utf-8")
    guard = text.index("IF v_operation='daily_source_snapshot_capture' THEN")
    check = text.index("AS 'daily_capture_recovery_invalid';")
    insert = text.index(
        "INSERT INTO `{project}.{dataset}.open_intelligence_execution_derivations_v1`"
    )
    assert guard < check < insert
    body = text[guard:check]
    assert "SET v_derivation=STRUCT(canonical_operation_context_json," in body
    assert "SET v_mode=JSON_VALUE(canonical_operation_context_json,'$.mode');" in body
    assert "WHERE JSON_VALUE(item,'$.name')='recovery_context');" in body
