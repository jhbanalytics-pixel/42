"""Owner reconciliation of a daily consumption whose execution ended without a result.

A daily child that dies after consume and before record leaves a consumption with no
result row. Only the child identity can record, and cancel refuses a consumed
derivation, so without this routine the consumption stays in flight forever: the
derive routine refuses the job as unresolved and, for collection, the in flight credit
cap refuses every later collection. The routine lets the owner write the
owner_reconciliation_hold tombstone that the derive routine already honours for money,
bound to the consumed execution and to its observed terminal state.
"""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from src.analysis.open_intelligence import daily_consumption_reconciliation as reconciliation
from src.analysis.open_intelligence.daily_execution_contracts import DailyContractError

ROOT = Path(__file__).resolve().parents[3]
ROUTINES = ROOT / "engine/infra/bigquery_routines"
NAME = "sp_reconcile_open_intelligence_daily_consumption_v1"
APPROVER = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
PARAMETERS = (
    "derivation_id",
    "consumption_id",
    "execution_name",
    "execution_terminal_state",
    "canonical_reconciliation_json",
    "reconciliation_digest",
)
KEYS = (
    "child_job_resource,consumption_id,contract_version,derivation_id,execution_completed_at,"
    "execution_name,execution_readback_sha256,execution_terminal_state,observed_at,"
    "observer_principal"
)
TOMBSTONES = "`{project}.{dataset}.open_intelligence_execution_derivation_tombstones_v1`"
RESULTS = "`{project}.{dataset}.open_intelligence_execution_results_v2`"
CONSUMPTIONS = "`{project}.{dataset}.open_intelligence_execution_consumptions_v2`"
DERIVATION = "exd_" + "a" * 64
CONSUMPTION = "exc_" + "b" * 64
JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
EXECUTION = f"{JOB}/executions/intelligence-42-daily-staging-abcde"


def _sql():
    return (ROUTINES / f"{NAME}.sql").read_text(encoding="utf-8")


def _record(**changes):
    record = {
        "child_job_resource": JOB,
        "consumption_id": CONSUMPTION,
        "contract_version": "daily_consumption_reconciliation_v1",
        "derivation_id": DERIVATION,
        "execution_completed_at": "2026-09-23T06:10:00Z",
        "execution_name": EXECUTION,
        "execution_readback_sha256": "c" * 64,
        "execution_terminal_state": "failed",
        "observed_at": "2026-09-23T06:20:00Z",
        "observer_principal": "albert.meintjes@ogilvy.co.za",
    }
    record.update(changes)
    return record


def _canonical(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _request(**changes):
    # What the owner supplies: the derivation, the consumption, the execution name read
    # from the stored consumption row, and when and as whom they observed it. The
    # terminal state, completion time and readback digest are never typed in.
    request = {
        "child_job_resource": JOB,
        "consumption_id": CONSUMPTION,
        "contract_version": "daily_consumption_reconciliation_v1",
        "derivation_id": DERIVATION,
        "execution_name": EXECUTION,
        "observed_at": "2026-09-23T06:20:00Z",
        "observer_principal": "albert.meintjes@ogilvy.co.za",
    }
    request.update(changes)
    return request


def _execution(**changes):
    # A Cloud Run Admin API v2 Execution resource as GET run.googleapis.com/v2/{name}
    # returns it for an execution whose single task exited non zero.
    execution = {
        "name": EXECUTION,
        "uid": "7f0c9a52-5d1e-4c1b-9a53-2b0f5d6f0e11",
        "generation": "1",
        "createTime": "2026-09-23T06:00:00.412733Z",
        "startTime": "2026-09-23T06:00:04.118207Z",
        "completionTime": "2026-09-23T06:10:00.654321Z",
        "launchStage": "GA",
        "job": "intelligence-42-daily-staging",
        "parallelism": 1,
        "taskCount": 1,
        "failedCount": 1,
        "conditions": [
            {
                "type": "ResourcesAvailable",
                "state": "CONDITION_SUCCEEDED",
                "lastTransitionTime": "2026-09-23T06:00:03.901442Z",
            },
            {
                "type": "Started",
                "state": "CONDITION_SUCCEEDED",
                "lastTransitionTime": "2026-09-23T06:00:04.118207Z",
            },
            {
                "type": "Completed",
                "state": "CONDITION_FAILED",
                "message": "Task intelligence-42-daily-staging-abcde-task0 failed with message: exit 1.",
                "lastTransitionTime": "2026-09-23T06:10:00.654321Z",
                "executionReason": "NON_ZERO_EXIT_CODE",
            },
        ],
        "etag": '"CPjMu8YGEJiZ7s4B/cHJvamVjdHM"',
    }
    execution.update(changes)
    return execution


def _running(**changes):
    execution = _execution(**changes)
    del execution["completionTime"]
    del execution["failedCount"]
    execution["runningCount"] = 1
    execution["reconciling"] = True
    execution["conditions"][-1] = {
        "type": "Completed",
        "state": "CONDITION_RECONCILING",
        "lastTransitionTime": "2026-09-23T06:00:04.118207Z",
    }
    return execution


def _completed(execution, **condition):
    execution["conditions"][-1] = {
        "type": "Completed",
        "lastTransitionTime": "2026-09-23T06:10:00.654321Z",
        **condition,
    }
    return execution


def _bytes(value):
    # Retained exactly as the API returned it: indented, not canonical.
    return json.dumps(value, indent=2).encode("utf-8")


_DEFAULT = object()


def _render(request=_DEFAULT, readback=_DEFAULT):
    return reconciliation.render_call(
        _request() if request is _DEFAULT else request,
        _bytes(_execution()) if readback is _DEFAULT else readback,
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging_approvals",
    )


def _rendered_record(rendered):
    values = {item["name"]: item["value"] for item in rendered["parameters"]}
    return values, json.loads(values["canonical_reconciliation_json"])


# SQL routine


def test_routine_header_takes_the_execution_and_its_observed_terminal_state():
    sql = _sql()
    header = re.sub(r"\s+", " ", sql.split(")\nBEGIN\n", 1)[0])
    assert header.startswith(f"CREATE OR REPLACE PROCEDURE `{{project}}.{{dataset}}.{NAME}`( ")
    declared = tuple(item.strip().split(" ")[0] for item in header.split("`(", 1)[1].split(","))
    assert declared == PARAMETERS
    assert all(f"{name} STRING" in header for name in PARAMETERS)
    assert sql.rstrip().endswith("END;")


def test_only_the_approver_identity_may_reconcile():
    sql = _sql()
    gate = (
        "ASSERT CONCAT('usr_',LOWER(TO_HEX(SHA256(CONCAT('open-intelligence-execution-approver-v1:',"
        f"v_actor)))))='{APPROVER}'\n    AS 'daily_consumption_reconciliation_actor_invalid';"
    )
    assert gate in sql
    assert "DECLARE v_actor STRING DEFAULT SESSION_USER();" in sql
    # The gate runs before anything is read or written, and nothing else admits a caller:
    # neither the parent that derived nor the child that consumed can reconcile.
    assert sql.index(gate) < sql.index("SET v_derivation=")
    assert "derived_by" not in sql
    assert "child_service_identity" not in sql


def test_terminal_state_is_a_closed_set_of_cloud_run_terminal_outcomes():
    sql = _sql()
    assert (
        "ASSERT v_terminal_state IN ('cancelled','failed','succeeded') "
        "AS 'daily_consumption_reconciliation_state_invalid';"
    ) in sql
    assert reconciliation.TERMINAL_STATES == ("cancelled", "failed", "succeeded")


def test_reconciliation_json_is_canonical_digest_bound_and_names_the_actor():
    sql = _sql()
    for fragment in (
        "`{project}.{dataset}.fn_is_canonical_execution_json_v1`(canonical_reconciliation_json)",
        "LOWER(TO_HEX(SHA256(canonical_reconciliation_json)))=v_digest",
        f"mode=>'strict'),',')='{KEYS}'",
        "JSON_VALUE(canonical_reconciliation_json,'$.contract_version')='daily_consumption_reconciliation_v1'",
        "JSON_VALUE(canonical_reconciliation_json,'$.derivation_id')=v_derivation_id",
        "JSON_VALUE(canonical_reconciliation_json,'$.consumption_id')=v_consumption_id",
        "JSON_VALUE(canonical_reconciliation_json,'$.execution_name')=v_execution_name",
        "JSON_VALUE(canonical_reconciliation_json,'$.execution_terminal_state')=v_terminal_state",
        "JSON_VALUE(canonical_reconciliation_json,'$.observer_principal')=v_actor",
        "REGEXP_CONTAINS(JSON_VALUE(canonical_reconciliation_json,'$.execution_readback_sha256'),r'^[0-9a-f]{64}$')",
        "TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.execution_completed_at'))<=TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.observed_at'))",
        "TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.observed_at'))<=v_now",
    ):
        assert fragment in sql, fragment
    assert "AS 'daily_consumption_reconciliation_invalid';" in sql
    assert ",".join(sorted(reconciliation.CONSUMPTION_RECONCILIATION_FIELDS)) == KEYS


def test_execution_is_bound_to_the_stored_consumption_not_to_the_payload():
    sql = _sql()
    # The execution the owner observed must be the one the store recorded at consume,
    # read from the consumption row, and the job must be the derivation's child job.
    assert (
        f"SET v_consumption=(SELECT AS STRUCT execution_name,consumed_at FROM {CONSUMPTIONS}\n"
        "    WHERE consumption_id=v_consumption_id AND approval_id=v_derivation_id);"
    ) in sql
    binding = sql.split("AS 'daily_consumption_reconciliation_authority_invalid';", 1)[1].split(
        "AS 'daily_consumption_reconciliation_execution_mismatch';", 1
    )[0]
    assert "v_consumption.execution_name=v_execution_name" in binding
    assert (
        "JSON_VALUE(canonical_reconciliation_json,'$.child_job_resource')=v_derivation.child_job_resource"
        in binding
    )
    assert (
        "v_consumption.consumed_at<=TIMESTAMP(JSON_VALUE(canonical_reconciliation_json,'$.execution_completed_at'))"
        in binding
    )
    assert f"(SELECT COUNT(*) FROM {CONSUMPTIONS} WHERE approval_id=v_derivation_id)=1" in sql


def test_a_consumption_with_a_result_is_refused_before_any_write():
    sql = _sql()
    refusal = (
        f"ASSERT NOT EXISTS(SELECT 1 FROM {RESULTS} WHERE consumption_id=v_consumption_id)\n"
        "    AS 'daily_consumption_reconciliation_resolved';"
    )
    assert refusal in sql
    assert sql.index(refusal) < sql.index("IF EXISTS(")
    assert sql.index(refusal) < sql.index("INSERT INTO")


def test_writes_exactly_one_hold_tombstone_and_nothing_else():
    sql = _sql()
    assert re.findall(r"INSERT INTO (`[^`]+`)", sql) == [TOMBSTONES]
    assert "UPDATE `{project}.{dataset}.open_intelligence_execution_approval_lock_v2`" in sql
    assert (
        re.findall(r"UPDATE (`[^`]+`)", sql)
        == ["`{project}.{dataset}.open_intelligence_execution_approval_lock_v2`"] * 2
    )
    assert "DELETE" not in sql
    assert "MERGE" not in sql
    values = sql.split(f"INSERT INTO {TOMBSTONES}", 1)[1].split(";", 1)[0]
    assert "'owner_reconciliation_hold'" in values
    for name in ("v_digest", "v_actor", "v_now"):
        assert name in values
    assert "BEGIN TRANSACTION;" in sql
    assert sql.count("COMMIT TRANSACTION;") == 2


def test_tombstone_id_uses_the_cancel_preimage_with_the_hold_reason():
    sql = _sql()
    assert (
        "SET v_id=CONCAT('ext_',LOWER(TO_HEX(SHA256(TO_JSON_STRING(STRUCT("
        "'open_intelligence_execution_derivation_tombstone_v1' AS tombstone_contract_version,"
        "v_derivation_id AS derivation_id,'owner_reconciliation_hold' AS reason_code,"
        "v_digest AS reconciliation_digest))))));"
    ) in sql


def test_reissue_returns_the_same_hold_and_a_different_one_conflicts():
    sql = _sql()
    reissue = sql.split("IF EXISTS(", 1)[1].split("END IF;", 1)[0]
    assert f"SELECT 1 FROM {TOMBSTONES} WHERE derivation_id=v_derivation_id" in reissue
    assert "AND tombstone_id=v_id)=1 AS 'daily_consumption_reconciliation_conflict';" in reissue
    lines = reissue.rstrip().splitlines()
    assert lines[-1] == "    RETURN;"
    assert sql.count("RETURN;") == 1


# Existing routines honour the hold


def test_consume_refuses_a_held_derivation_through_its_tombstone_check():
    consume = (ROUTINES / "sp_consume_open_intelligence_daily_derivation_v1.sql").read_text(
        encoding="utf-8"
    )
    check = f"ASSERT NOT EXISTS(SELECT 1 FROM {TOMBSTONES} WHERE derivation_id=v_derivation_id)"
    assert check in consume
    # The tombstone check runs before the reissue branch, so a held consumption cannot be
    # handed back to a late child either.
    assert consume.index(check) < consume.index("IF (SELECT COUNT(*) FROM")


def test_record_refuses_a_result_for_a_held_consumption():
    record = (ROUTINES / "sp_record_open_intelligence_daily_result_v1.sql").read_text(
        encoding="utf-8"
    )
    refusal = (
        f"ASSERT NOT EXISTS(SELECT 1 FROM {TOMBSTONES} WHERE derivation_id=v_derivation_id)\n"
        "    AS 'daily_result_reconciled';"
    )
    assert refusal in record
    assert record.index("AS 'daily_result_actor_mismatch';") < record.index(refusal)
    assert record.index(refusal) < record.index("IF EXISTS(")


def test_derive_still_charges_the_held_reservation_against_the_monthly_allowance():
    derive = (ROUTINES / "sp_derive_open_intelligence_daily_execution_v1.sql").read_text(
        encoding="utf-8"
    )
    assert "(t.reason_code IS NULL OR t.reason_code='owner_reconciliation_hold')" in derive


# Python contract


def test_python_contract_accepts_each_terminal_state_and_returns_canonical_bytes():
    for state in ("cancelled", "failed", "succeeded"):
        record, raw = reconciliation.validate_consumption_reconciliation(
            _record(execution_terminal_state=state)
        )
        assert raw == _canonical(record).encode("utf-8")
        assert record["execution_terminal_state"] == state


@pytest.mark.parametrize(
    "changes",
    [
        {"execution_terminal_state": "running"},
        {"execution_terminal_state": "unknown"},
        {"execution_terminal_state": None},
        {"contract_version": "daily_dispatch_reconciliation_v1"},
        {"derivation_id": "exd_short"},
        {"consumption_id": "exr_" + "b" * 64},
        {"execution_name": ""},
        {"child_job_resource": ""},
        {"execution_readback_sha256": "C" * 64},
        {"observer_principal": ""},
        {"observed_at": "2026-09-23T06:00:00Z"},
        {"execution_completed_at": "not a time"},
        {"observed_at": "2026-09-23T06:20:00"},
    ],
)
def test_python_contract_refuses_each_defect(changes):
    with pytest.raises(DailyContractError, match=r"^daily_consumption_reconciliation_invalid$"):
        reconciliation.validate_consumption_reconciliation(_record(**changes))


def test_python_contract_refuses_extra_or_missing_fields_and_noncanonical_text():
    extra = _record(note="x")
    with pytest.raises(DailyContractError, match=r"^daily_consumption_reconciliation_invalid$"):
        reconciliation.validate_consumption_reconciliation(extra)
    missing = _record()
    del missing["execution_readback_sha256"]
    with pytest.raises(DailyContractError, match=r"^daily_consumption_reconciliation_invalid$"):
        reconciliation.validate_consumption_reconciliation(missing)
    spaced = json.dumps(_record(), sort_keys=True)
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_noncanonical$"
    ):
        reconciliation.validate_consumption_reconciliation(spaced)


def test_call_parameters_follow_the_routine_order_and_recompute_the_digest():
    readback = _bytes(_execution())
    parameters = reconciliation.call_parameters(_request(), readback)
    assert tuple(name for name, _ in parameters) == PARAMETERS
    values = dict(parameters)
    raw = values["canonical_reconciliation_json"]
    assert raw == _canonical(json.loads(raw))
    assert json.loads(raw) == _record(
        execution_completed_at="2026-09-23T06:10:00.654321Z",
        execution_readback_sha256=hashlib.sha256(readback).hexdigest(),
    )
    assert values["derivation_id"] == DERIVATION
    assert values["consumption_id"] == CONSUMPTION
    assert values["execution_name"] == EXECUTION
    assert values["execution_terminal_state"] == "failed"
    assert values["reconciliation_digest"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert reconciliation.ROUTINE == NAME


def test_render_call_names_the_routine_and_binds_every_value_as_a_parameter():
    request = _request()
    readback = _bytes(_execution())
    rendered = reconciliation.render_call(
        request, readback, project="ogilvy-trends-v2", dataset="trends_v2_staging_approvals"
    )
    assert rendered["statement"] == (
        f"CALL `ogilvy-trends-v2.trends_v2_staging_approvals.{NAME}`("
        + ", ".join(f"@{name}" for name in PARAMETERS)
        + ")"
    )
    assert rendered["parameters"] == [
        {"name": name, "type": "STRING", "value": value}
        for name, value in reconciliation.call_parameters(request, readback)
    ]
    # A resource name is never taken from the document: project and dataset are the
    # caller's fixed arguments and refuse anything outside the name grammar.
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_target_invalid$"
    ):
        reconciliation.render_call(request, readback, project="p`; DROP", dataset="d")
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_target_invalid$"
    ):
        reconciliation.render_call(request, readback, project="ogilvy-trends-v2", dataset="d.e")


# The execution readback, not the owner, decides the terminal state


def test_render_call_derives_state_completion_and_digest_from_the_readback_bytes():
    readback = _bytes(_execution())
    values, record = _rendered_record(_render(readback=readback))
    assert record["execution_name"] == EXECUTION
    assert record["execution_terminal_state"] == "failed"
    assert record["execution_completed_at"] == "2026-09-23T06:10:00.654321Z"
    assert record["execution_readback_sha256"] == hashlib.sha256(readback).hexdigest()
    assert values["execution_terminal_state"] == "failed"
    # The digest is over the bytes as retained, so a reformatted copy is another readback.
    other = json.dumps(_execution()).encode("utf-8")
    assert other != readback
    _, again = _rendered_record(_render(readback=other))
    assert again["execution_readback_sha256"] == hashlib.sha256(other).hexdigest()
    assert again["execution_readback_sha256"] != record["execution_readback_sha256"]


def test_each_terminal_outcome_is_read_from_the_completed_condition():
    succeeded = _completed(_execution(), state="CONDITION_SUCCEEDED")
    del succeeded["failedCount"]
    succeeded["succeededCount"] = 1
    cancelled = _completed(_execution(), state="CONDITION_FAILED", executionReason="CANCELLED")
    del cancelled["failedCount"]
    cancelled["cancelledCount"] = 1
    for execution, state in (
        (_execution(), "failed"),
        (succeeded, "succeeded"),
        (cancelled, "cancelled"),
    ):
        values, record = _rendered_record(_render(readback=_bytes(execution)))
        assert record["execution_terminal_state"] == state
        assert values["execution_terminal_state"] == state


def test_a_still_running_execution_readback_is_refused():
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_execution_not_terminal$"
    ):
        _render(readback=_bytes(_running()))


@pytest.mark.parametrize(
    "execution",
    [
        # No completion time: the execution has not ended, whatever its condition says.
        {key: value for key, value in _execution().items() if key != "completionTime"},
        _execution(completionTime=None),
        _execution(completionTime=""),
        # A completion time while the Completed condition is not terminal.
        _completed(_execution(), state="CONDITION_RECONCILING"),
        _completed(_execution(), state="CONDITION_PENDING"),
        _completed(_execution(), state="STATE_UNSPECIFIED"),
        _completed(_execution(), state="CONDITION_FAILED", executionReason="CANCELLING"),
        # CONDITION_SUCCEEDED carries no execution reason; one that does is not a
        # success the readback can prove, so it is refused rather than read as one.
        _completed(_execution(), state="CONDITION_SUCCEEDED", executionReason="NON_ZERO_EXIT_CODE"),
        _completed(_execution(), state="CONDITION_SUCCEEDED", executionReason="CANCELLED"),
        _completed(
            _execution(), state="CONDITION_SUCCEEDED", executionReason="EXECUTION_REASON_UNDEFINED"
        ),
        # Tasks still running or the resource still reconciling.
        _execution(runningCount=1),
        _execution(reconciling=True),
        # No Completed condition, or two of them.
        _execution(conditions=_execution()["conditions"][:2]),
        _execution(conditions=[*_execution()["conditions"], _execution()["conditions"][-1]]),
        _execution(conditions=None),
    ],
)
def test_an_execution_that_has_not_ended_is_refused(execution):
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_execution_not_terminal$"
    ):
        _render(readback=_bytes(execution))


def test_a_readback_of_another_execution_or_job_is_refused():
    other_execution = f"{JOB}/executions/intelligence-42-daily-staging-fghij"
    other_job = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-weekly"
    for execution in (
        _execution(name=other_execution),
        _execution(name=f"{other_job}/executions/intelligence-42-daily-staging-abcde"),
        _execution(job="intelligence-42-weekly"),
        _execution(job=other_job),
    ):
        with pytest.raises(
            DailyContractError, match=r"^daily_consumption_reconciliation_execution_mismatch$"
        ):
            _render(readback=_bytes(execution))
    # The stored execution name the owner copied from the consumption row must be the
    # execution the readback describes.
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_execution_mismatch$"
    ):
        _render(request=_request(execution_name=other_execution))
    # The execution must belong to the derivation's child job, even when the stored
    # name and the readback agree with each other.
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_execution_mismatch$"
    ):
        _render(
            request=_request(
                execution_name=f"{other_job}/executions/intelligence-42-daily-staging-abcde",
            ),
            readback=_bytes(
                _execution(
                    name=f"{other_job}/executions/intelligence-42-daily-staging-abcde",
                    job="intelligence-42-weekly",
                )
            ),
        )
    # The fully qualified job form binds as well as the short one.
    _, record = _rendered_record(_render(readback=_bytes(_execution(job=JOB))))
    assert record["execution_name"] == EXECUTION


@pytest.mark.parametrize(
    "field", ["execution_terminal_state", "execution_completed_at", "execution_readback_sha256"]
)
def test_the_owner_cannot_type_in_a_value_the_readback_decides(field):
    typed = {
        "execution_terminal_state": "succeeded",
        "execution_completed_at": "2026-09-23T06:10:00Z",
        "execution_readback_sha256": "c" * 64,
    }[field]
    with pytest.raises(DailyContractError, match=r"^daily_consumption_reconciliation_invalid$"):
        _render(request=_request(**{field: typed}))


@pytest.mark.parametrize(
    "readback",
    [
        "a string, not the retained bytes",
        None,
        b"",
        b"not json",
        b"[]",
        b"\xff\xfe",
        b'{"name":"x","name":"y"}',
        _bytes(_execution()).replace(b'"taskCount": 1', b'"taskCount": NaN'),
        _bytes({key: value for key, value in _execution().items() if key != "name"}),
        _bytes(_execution(name=None)),
    ],
    ids=[
        "text",
        "none",
        "empty",
        "not_json",
        "array",
        "not_utf8",
        "duplicate_key",
        "nan",
        "no_name",
        "null_name",
    ],
)
def test_a_readback_that_is_not_one_execution_resource_is_refused(readback):
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_readback_invalid$"
    ):
        _render(readback=readback)


def _duplicated(member, readback=None):
    # A retained readback with one more top level member appended: the first
    # occurrence is what the readback says first, the later one would override it.
    raw = _bytes(_execution()) if readback is None else readback
    assert raw.endswith(b"\n}")
    return raw[:-2] + b",\n  " + member + b"\n}"


_OTHER_EXECUTION = f"{JOB}/executions/intelligence-42-daily-staging-fghij"
_SUCCEEDED_CONDITIONS = json.dumps(
    _completed(_execution(), state="CONDITION_SUCCEEDED")["conditions"]
).encode("utf-8")


@pytest.mark.parametrize(
    ("readback", "field", "last_wins"),
    [
        # A later conditions member that ends a failed execution succeeded.
        (
            _duplicated(b'"conditions": ' + _SUCCEEDED_CONDITIONS),
            "execution_terminal_state",
            "succeeded",
        ),
        # A readback of another execution whose later name is the stored one.
        (
            _duplicated(
                b'"name": "' + EXECUTION.encode() + b'"',
                _bytes(_execution(name=_OTHER_EXECUTION)),
            ),
            "execution_name",
            EXECUTION,
        ),
        # A later completion time.
        (
            _duplicated(b'"completionTime": "2026-09-23T06:09:00Z"'),
            "execution_completed_at",
            "2026-09-23T06:09:00.000000Z",
        ),
        # A repeated member inside the Completed condition: a later reason that
        # turns a failure into a cancellation.
        (
            _bytes(_execution()).replace(
                b'"executionReason": "NON_ZERO_EXIT_CODE"',
                b'"executionReason": "NON_ZERO_EXIT_CODE",\n        "executionReason": "CANCELLED"',
            ),
            "execution_terminal_state",
            "cancelled",
        ),
    ],
    ids=["conditions", "name", "completion_time", "nested_reason"],
)
def test_a_readback_with_a_repeated_member_is_refused_whichever_occurrence_wins(
    readback, field, last_wins
):
    # Parsed with the last occurrence winning, each readback is one this module
    # renders, with a value the first occurrence contradicts. The retained bytes say
    # two things about one field, so they prove neither and are refused whole.
    _, record = _rendered_record(_render(readback=_bytes(json.loads(readback))))
    assert record[field] == last_wins
    with pytest.raises(
        DailyContractError, match=r"^daily_consumption_reconciliation_readback_invalid$"
    ):
        _render(readback=readback)


def test_completion_time_is_bounded_by_the_observation_and_normalised_for_the_store():
    # Completed after the owner says they observed it: the observation is false.
    with pytest.raises(DailyContractError, match=r"^daily_consumption_reconciliation_invalid$"):
        _render(request=_request(observed_at="2026-09-23T06:05:00Z"))
    # Nanosecond precision is truncated to the microseconds the store keeps.
    _, record = _rendered_record(
        _render(readback=_bytes(_execution(completionTime="2026-09-23T06:10:00.654321987Z")))
    )
    assert record["execution_completed_at"] == "2026-09-23T06:10:00.654321Z"
    _, record = _rendered_record(
        _render(readback=_bytes(_execution(completionTime="2026-09-23T06:10:00Z")))
    )
    assert record["execution_completed_at"] == "2026-09-23T06:10:00.000000Z"
    for bad in ("2026-09-23T06:10:00", "2026-09-23 06:10:00Z", "yesterday", 1790000000):
        with pytest.raises(
            DailyContractError, match=r"^daily_consumption_reconciliation_readback_invalid$"
        ):
            _render(readback=_bytes(_execution(completionTime=bad)))


def test_main_reads_the_request_and_the_retained_readback(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(_canonical(_request()), encoding="utf-8")
    readback = tmp_path / "execution.json"
    readback.write_bytes(_bytes(_execution()))
    code = reconciliation.main(
        [str(request), str(readback), "ogilvy-trends-v2", "trends_v2_staging_approvals"]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == reconciliation.render_call(
        _request(),
        _bytes(_execution()),
        project="ogilvy-trends-v2",
        dataset="trends_v2_staging_approvals",
    )
    readback.write_bytes(_bytes(_running()))
    code = reconciliation.main(
        [str(request), str(readback), "ogilvy-trends-v2", "trends_v2_staging_approvals"]
    )
    assert code == 1
    assert capsys.readouterr().err.strip() == (
        "daily_consumption_reconciliation_execution_not_terminal"
    )
    assert reconciliation.main([str(request), "ogilvy-trends-v2", "d"]) == 2


def test_unresolved_predicate_treats_a_held_consumption_as_settled():
    derive = (ROUTINES / "sp_derive_open_intelligence_daily_execution_v1.sql").read_text(
        encoding="utf-8"
    )
    predicate = derive.split("/* daily_unresolved_predicate_begin */", 1)[1].split(
        "/* daily_unresolved_predicate_end */", 1
    )[0]

    def json_value(raw, path):
        value = json.loads(raw) if raw is not None else None
        for key in path.removeprefix("$.").split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value) if isinstance(value, (str, int, float)) else None

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, json_value)
        connection.executescript(
            "CREATE TABLE d(id); CREATE TABLE t(derivation_id,reason_code);"
            " CREATE TABLE c(consumption_id); CREATE TABLE r(result_id,canonical_result_json);"
        )
        connection.execute("INSERT INTO d VALUES (1)")

        def blocks(reason):
            for table in ("t", "c", "r"):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("INSERT INTO c VALUES ('c')")
            if reason is not None:
                connection.execute("INSERT INTO t VALUES ('t',?)", (reason,))
            query = (
                "SELECT COUNT(*) FROM d LEFT JOIN t ON 1=1 LEFT JOIN c ON 1=1 "
                f"LEFT JOIN r ON 1=1 WHERE ({predicate}"
            )
            return connection.execute(query).fetchone()[0] == 1

        assert blocks(None)
        assert not blocks("owner_reconciliation_hold")
        # Only the hold settles a consumed derivation; no other reason can.
        assert blocks("dispatch_not_attempted")
        assert blocks("provider_terminal_no_execution")


def test_the_v2_install_plan_creates_the_routine_as_its_header_reads():
    """Amendment e installs the routine through the v2 SQL installation: one spec row
    with the store writer role and the header's six STRING parameters in order, one
    dataset authorization at that role, and no service account bound to it, since the
    owner invokes it under the approver identity and the delta grants that."""
    from scripts.migrations import (
        create_open_intelligence_execution_approval_store as migration,
    )

    installed = {name: (role, parameters) for name, role, parameters in migration.V2_ROUTINE_SPECS}
    assert NAME not in {name for name, *_ in migration.ROUTINE_SPECS}
    assert installed[NAME] == (
        "roles/bigquery.routineDataEditor",
        tuple((name, "STRING") for name in PARAMETERS),
    )
    assert reconciliation.PARAMETERS == PARAMETERS
    plan = migration.build_v2_plan()
    routines = [item for item in plan.routines if item.name == NAME]
    assert len(routines) == 1
    assert routines[0].sql == _sql()
    assert routines[0].sha256 == hashlib.sha256((ROUTINES / f"{NAME}.sql").read_bytes()).hexdigest()
    resource = f"projects/ogilvy-trends-v2/datasets/{migration.DATASET}/routines/{NAME}"
    assert [
        (item.dataset, item.role)
        for item in plan.iam_plan.routine_authorizations
        if item.routine == resource
    ] == [(f"ogilvy-trends-v2.{migration.DATASET}", "roles/bigquery.routineDataEditor")]
    assert not [
        item for item in plan.iam_plan.principal_routine_bindings if item.resource == resource
    ]
    for group in (
        migration.V2_RUNTIME_ROUTINES,
        migration.V2_DAILY_ROUTINES,
        migration.V2_OPERATOR_ROUTINES,
    ):
        assert NAME not in group
