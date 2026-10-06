"""A stage whose contract cap is unbounded refuses to derive.

The int64 maximum as an upper bound is "no bound", not a bound. The exposure
(collection_exposure_issue) and compose (then r3_apply) contracts carried it until the
daily contract revision bounded exposure and gave compose its own operation,
daily_composition_apply; they are now bounded. The refusal stands for any stage whose
cap is unbounded: the parent refuses before any effect, the derive library refuses a
manifest carrying it, and so does the derive routine.
"""

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import pytest
from src.analysis.open_intelligence import daily_operation_map
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cost_policy import DailyCostPolicyRefusal
from src.analysis.open_intelligence.daily_execution_authority import (
    DailyAuthorityIntegrityError,
    derive_daily_execution,
)

from tests.unit.test_daily_derivation_authority import (
    LEASE,
    Derive,
    FakeObjectClient,
    authority,
    cost_policy,
    frame,
    grant,
    grant_row,
    registry,
)

ROOT = Path(__file__).resolve().parents[2]
DERIVE = ROOT / "infra/bigquery_routines/sp_derive_open_intelligence_daily_execution_v1.sql"
UNBOUNDED = 2**63 - 1
STAGE_OPERATIONS = {
    "exposure": "daily_collection_exposure_issue",
    "compose": "daily_composition_apply",
}


@pytest.mark.parametrize("stage", sorted(STAGE_OPERATIONS))
def test_the_contract_cap_of_exposure_and_compose_is_now_bounded(stage):
    binding = daily_operation_map.child_binding(STAGE_OPERATIONS[stage], registry=registry())
    assert binding.unbounded_limits() == ()
    assert UNBOUNDED not in binding.bounded_limits().values()


def _unbounded(real):
    def binding_for(operation, *, registry):
        found = real(operation, registry=registry)
        if operation not in STAGE_OPERATIONS.values():
            return found
        rules = dict(found.limit_rules)
        rules["max_bytes_billed"] = MappingProxyType({"minimum": 0, "maximum": UNBOUNDED})
        return replace(found, limit_rules=MappingProxyType(rules))

    return binding_for


@pytest.mark.parametrize("stage", sorted(STAGE_OPERATIONS))
def test_a_granted_stage_with_an_unbounded_cap_refuses_before_any_effect(stage):
    value = grant(
        allowed_operations=sorted({*grant()["allowed_operations"], *STAGE_OPERATIONS.values()})
    )
    binding_for = _unbounded(daily_operation_map.child_binding)
    objects = FakeObjectClient()
    derive = Derive(objects)
    # The parent's cost policy is the reviewed one; only the binding the parent resolves
    # carries the int64 sentinel, so the refusal is the parent's own.
    subject_authority, _derive, _store, _objects = authority(
        objects=objects,
        derive=derive,
        grant_row=grant_row(value),
        policy=cost_policy(),
        producers={operation: lambda frame, binding: {} for operation in STAGE_OPERATIONS.values()},
        binding_for=binding_for,
    )
    with pytest.raises(daily_operation_map.DailyOperationRefusal, match=r"^daily_limit_unbounded$"):
        subject_authority.prepare(stage, frame(stage), lease=LEASE)
    assert derive.calls == []
    assert objects.objects == {}


class Clients:
    def __init__(self):
        self.calls = []

    def derive_execution(self, *parameters):
        self.calls.append(parameters)
        raise AssertionError("derive must not be called")


def prepared_release():
    subject_authority, _derive, _store, _objects = authority()
    return subject_authority.prepare("release", frame("release"), lease=LEASE)


def derive_with(limits):
    prepared = prepared_release()
    manifest = json.loads(prepared["canonical_manifest_json"])
    manifest["limits"] = limits
    manifest_json = canonical_bytes(manifest).decode("utf-8")
    clients = Clients()
    with pytest.raises(DailyAuthorityIntegrityError) as refused:
        derive_daily_execution(
            canonical_manifest_json=manifest_json,
            manifest_sha256=sha256(manifest_json.encode()).hexdigest(),
            canonical_operation_context_json=prepared["canonical_operation_context_json"],
            operation_context_sha256=prepared["intent"]["operation_context_sha256"],
            canonical_operation_artifact_set_json=prepared["canonical_operation_artifact_set_json"],
            authorizing_grant_digest=prepared["intent"]["authorizing_grant_digest"],
            clients=clients,
        )
    assert clients.calls == []
    return refused.value.code


BOUNDED = {"max_bytes_billed": 0, "max_credits": 0, "max_model_calls": 0, "max_rows_written": 2}


@pytest.mark.parametrize("name", sorted(BOUNDED))
def test_the_derive_library_refuses_an_unbounded_manifest_limit(name):
    assert derive_with(dict(BOUNDED, **{name: UNBOUNDED})) == "daily_limit_unbounded"


@pytest.mark.parametrize(
    "limits",
    [
        None,
        {},
        {key: value for key, value in BOUNDED.items() if key != "max_credits"},
        dict(BOUNDED, max_credits=-1),
        dict(BOUNDED, max_credits=1.5),
        dict(BOUNDED, max_credits=True),
        dict(BOUNDED, extra=0),
    ],
)
def test_the_derive_library_refuses_malformed_manifest_limits(limits):
    assert derive_with(limits) == "daily_manifest_limits_invalid"


def _json_value(document, path):
    value = json.loads(document)
    for key in path.removeprefix("$.").split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value if value is None or isinstance(value, str) else json.dumps(value)


def _safe_int64(value):
    if not isinstance(value, str) or re.fullmatch(r"-?[0-9]+", value) is None:
        return None
    number = int(value)
    return number if -(2**63) <= number < 2**63 else None


def limits_predicate():
    text = DERIVE.read_text(encoding="utf-8")
    end = text.index("AS 'daily_limit_unbounded'")
    start = text.rindex("ASSERT ", 0, end)
    predicate = text[start + len("ASSERT ") : end]
    return re.sub(
        r"SAFE_CAST\((JSON_VALUE\([^)]*\)) AS INT64\)", r"SAFE_INT64(\1)", predicate
    ).replace("9223372036854775806", str(UNBOUNDED - 1))


def routine_accepts(limits):
    manifest = json.dumps({"limits": limits})
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function("JSON_VALUE", 2, _json_value)
        connection.create_function("SAFE_INT64", 1, _safe_int64)
        connection.execute("CREATE TABLE inputs(canonical_manifest_json)")
        connection.execute("INSERT INTO inputs VALUES (?)", (manifest,))
        return connection.execute(f"SELECT ({limits_predicate()}) FROM inputs").fetchone()[0]


def test_the_derive_routine_checks_limits_before_it_writes():
    text = DERIVE.read_text(encoding="utf-8")
    assert text.count("AS 'daily_limit_unbounded'") == 1
    assert text.index("AS 'daily_limit_unbounded'") < text.index("INSERT INTO")


def test_the_derive_routine_accepts_bounded_limits():
    assert routine_accepts(BOUNDED) == 1


@pytest.mark.parametrize(
    "limits",
    [
        *(dict(BOUNDED, **{name: UNBOUNDED}) for name in sorted(BOUNDED)),
        {key: value for key, value in BOUNDED.items() if key != "max_rows_written"},
        dict(BOUNDED, max_credits=-1),
        dict(BOUNDED, max_credits=1.5),
    ],
)
def test_the_derive_routine_refuses_unbounded_or_missing_limits(limits):
    assert not routine_accepts(limits)
