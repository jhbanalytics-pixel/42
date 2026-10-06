"""The retained replay entry point binds a local protected capture to its registry pins."""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from scripts.staging import replay_manifest
from scripts.staging.replay_manifest import (
    RetainedCaptureMismatch,
    evaluate_retained_replay,
)
from scripts.staging.replay_open_intelligence import ReplaySignal
from src.analysis.open_intelligence.production_snapshot import _deserialize_plan
from src.analysis.open_intelligence.protected_context_registry import ProtectedContextEntry

from tests.unit import test_protected_snapshot_replay_integration as integration
from tests.unit.test_provenance_replay_v3 import Provider

PROFILE_ID = "protected_context_20260907_v1"
# Fields the online reader and the retained path may differ on: the scope names its run,
# the provider record carries a measured runtime, and the digest covers both.
PARITY_EXEMPT = {"scope", "provider_outputs", "replay_digest"}
# The scope fields that differ: the retained path resolves its scope from the registry
# with its own run id (retained_replay_<cutoff>) and the default brand configuration and
# theme, while the online reader's fixture scope names its own run, brand and theme.
# Because scope and replay_digest differ, a retained stable_replay_digest can never be
# compared with an online replay's digest; only the fields below are compared.
SCOPE_FIELDS_THAT_DIFFER = {"brand_config_id", "theme_id", "run_id"}


@pytest.fixture(scope="module")
def retained():
    """One completed synthetic protected capture, read back as its stored bytes."""
    patch = pytest.MonkeyPatch()
    try:
        _http, storage_http, _ledger, inputs = integration.completed_capture(patch)
        replay = integration.subject._protected_snapshot_result_to_replay(**inputs)
        name = next(
            key
            for key in storage_http.objects
            if isinstance(key, str) and key.startswith("captures/")
        )
        raw = storage_http.objects[name]["raw"]
        row = dict(_ledger.result)
    finally:
        patch.undo()
    # The result ledger row as the owner's read saves it: every value a string, the
    # completion time at microsecond precision in UTC.
    row["completed_at"] = row["completed_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    artifact = json.loads(raw)
    snapshot = artifact["capture"]["assembly"]["snapshot"]
    plan = _deserialize_plan(artifact["capture"]["assembly"]["snapshot_plan"])
    entry = ProtectedContextEntry(
        consumption_id=inputs["consumption_id"],
        cutoff_date=snapshot["cutoff_date"],
        manifest_sha256=inputs["manifest_sha256"],
        profile_id=PROFILE_ID,
        result_digest=inputs["result_digest"],
        result_id=inputs["result_id"],
        source_as_of=snapshot["source_as_of"],
        market_scope=tuple(snapshot["market_scope"]),
        snapshot_tables=tuple(item.destination_table for item in plan.statements),
    )
    binding = {
        "profile_id": PROFILE_ID,
        "cutoff_date": snapshot["cutoff_date"],
        "consumption_id": inputs["consumption_id"],
        "manifest_sha256": inputs["manifest_sha256"],
        "result_id": inputs["result_id"],
        "result_digest": inputs["result_digest"],
        "capture_sha256": hashlib.sha256(raw).hexdigest(),
        "snapshot_digest": snapshot["source_digest"],
        "table_digests": dict(snapshot["table_digests"]),
        "known_event_set_digest": None,
    }
    components = replay["components_by_candidate"]
    signals = tuple(
        ReplaySignal(
            signal_id=f"sig_{index}",
            market=component["market"],
            cluster_signature=candidate_id,
            member_identities=tuple(component["member_identities"]),
            source_max_observed_at=datetime(2026, 9, 7, 12, tzinfo=UTC),
            membership_complete=True,
            evidence_ready=True,
            geo_proven=True,
        )
        for index, (candidate_id, component) in enumerate(sorted(components.items()))
    )
    return {
        "raw": raw,
        "entry": entry,
        "binding": binding,
        "signals": signals,
        "row": row,
        "online": replay,
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def evaluate(retained, **overrides):
    values = {
        "capture_raw": retained["raw"],
        "binding": dict(retained["binding"]),
        "signals": retained["signals"],
        "known_event_set": None,
        "registry_entry": retained["entry"],
        "semantic_provider": Provider(),
        "sample_per_market": 1,
    }
    values.update(overrides)
    return evaluate_retained_replay(**values)


SELF_CONSISTENCY = {
    "capture_sha256": "held",
    "capture_structure": "held",
    "snapshot_digest": "held",
    "cutoff": "held",
    "profile": "held",
    "registry_scope": "held",
    "table_digests": "held",
    "capture_owner": "held",
}
UNPROVEN = [
    "capture_owner",
    "capture_sha256",
    "consumption_id",
    "manifest_sha256",
    "result_digest",
    "result_id",
    "snapshot_digest",
    "table_digests",
]


@pytest.fixture
def repo_pins(monkeypatch, retained):
    """The fixture's registry entry stands in as the repo registry's own entry."""
    from src.analysis.open_intelligence import protected_context_registry

    entry = retained["entry"]
    monkeypatch.setattr(
        protected_context_registry,
        "profile_for",
        lambda cutoff: entry if cutoff == entry.cutoff_date else None,
    )
    return entry


def test_bound_capture_evaluates_as_real_with_recall_unknown(retained, repo_pins):
    result = evaluate(retained, result_row=retained["row"])
    assert result["evidence_scope"] == "retained_capture"
    assert result["authority"] == "authority_verified"
    assert result["real"] is True
    assert result["cutoff"] == retained["binding"]["cutoff_date"]
    assert result["bindings"] == {"registry_pins": "held", "known_event_set": "absent"}
    assert result["self_consistency"] == SELF_CONSISTENCY
    assert result["independent_pins"] == {"result_row": "held"}
    assert result["registry_entry_source"] == "repo"
    assert result["unproven"] == []
    assert result["unproven_reasons"] == []
    assert result["snapshot_digest"] == retained["binding"]["snapshot_digest"]
    assert result["known_event_recall"] == {
        "state": "unknown",
        "reason": "no_known_event_set",
        "metric": None,
    }
    assert set(result["known_event_recall_by_market"]) == {"ke", "ng", "za"}
    assert all(
        value == {"state": "unknown", "reason": "no_known_event_set", "metric": None}
        for value in result["known_event_recall_by_market"].values()
    )
    assert "known event" in result["known_event_gap"]
    assert result["counts"]["signals"] == len(retained["signals"])
    assert result["counts"]["row_counts_by_table"]
    assert result["markets_without_signals"] == sorted(
        {"ke", "ng", "za"} - {signal.market for signal in retained["signals"]}
    )


@pytest.mark.parametrize(
    "field",
    [
        "profile_id",
        "cutoff_date",
        "consumption_id",
        "manifest_sha256",
        "result_id",
        "result_digest",
    ],
)
def test_each_registry_pin_mismatch_refuses(retained, field):
    binding = dict(retained["binding"])
    binding[field] = (
        "2026-09-08"
        if field == "cutoff_date"
        else "protected_context_20260908_v1"
        if field == "profile_id"
        else binding[field][:-1] + ("0" if binding[field][-1] != "0" else "1")
    )
    with pytest.raises(RetainedCaptureMismatch, match=f"registry_pin_mismatch:{field}"):
        evaluate(retained, binding=binding)


@pytest.mark.parametrize(
    "field", ["consumption_id", "manifest_sha256", "result_id", "result_digest", "cutoff_date"]
)
def test_a_registry_entry_disagreeing_with_the_binding_refuses(retained, field):
    changed = {
        "cutoff_date": "2026-09-08",
    }.get(field, "exr_" + "0" * 64 if field == "result_id" else None)
    if changed is None:
        value = getattr(retained["entry"], field)
        changed = value[:-1] + ("0" if value[-1] != "0" else "1")
    entry = replace(retained["entry"], **{field: changed})
    with pytest.raises(RetainedCaptureMismatch, match="registry_pin_mismatch"):
        evaluate(retained, registry_entry=entry)


def test_an_unpinned_registry_entry_refuses(retained):
    entry = replace(
        retained["entry"],
        consumption_id=None,
        manifest_sha256=None,
        result_id=None,
        result_digest=None,
    )
    with pytest.raises(RetainedCaptureMismatch, match="registry_entry_unpinned"):
        evaluate(retained, registry_entry=entry)


def test_snapshot_digest_mismatch_refuses(retained):
    binding = {**retained["binding"], "snapshot_digest": "0" * 64}
    with pytest.raises(RetainedCaptureMismatch, match="snapshot_digest_mismatch"):
        evaluate(retained, binding=binding)


@pytest.mark.parametrize("change", ["value", "missing", "extra"])
def test_per_table_digest_mismatch_refuses(retained, change):
    tables = dict(retained["binding"]["table_digests"])
    first = sorted(tables)[0]
    if change == "value":
        tables[first] = "0" * 64
    elif change == "missing":
        tables.pop(first)
    else:
        tables["other_table"] = "0" * 64
    binding = {**retained["binding"], "table_digests": tables}
    with pytest.raises(RetainedCaptureMismatch, match="table_digests_mismatch"):
        evaluate(retained, binding=binding)


def test_registry_snapshot_tables_must_be_the_captured_tables(retained):
    entry = replace(
        retained["entry"], snapshot_tables=(*retained["entry"].snapshot_tables[:-1], "x.y.z")
    )
    with pytest.raises(RetainedCaptureMismatch, match="registry_pin_mismatch:snapshot_tables"):
        evaluate(retained, registry_entry=entry)


def test_a_tampered_capture_refuses_under_its_pinned_digest(retained):
    raw = retained["raw"].replace(b'"ke"', b'"KE"', 1)
    assert raw != retained["raw"]
    with pytest.raises(RetainedCaptureMismatch, match="capture_sha256_mismatch"):
        evaluate(retained, capture_raw=raw)


def test_a_tampered_capture_resealed_to_a_new_digest_still_refuses(retained):
    artifact = json.loads(retained["raw"])
    snapshot = artifact["capture"]["assembly"]["snapshot"]
    snapshot["row_counts_by_table"] = {
        key: value + 1 for key, value in snapshot["row_counts_by_table"].items()
    }
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    binding = {**retained["binding"], "capture_sha256": hashlib.sha256(raw).hexdigest()}
    with pytest.raises(RetainedCaptureMismatch, match="capture_invalid"):
        evaluate(retained, capture_raw=raw, binding=binding)


def test_a_capture_whose_owner_is_not_the_pinned_consumption_refuses(retained):
    artifact = json.loads(retained["raw"])
    for item in artifact["creation_evidence"]:
        item["consumption_id"] = "exc_" + "0" * 64
    from src.analysis.open_intelligence.production_snapshot_storage import _pack

    raw = _pack(artifact)
    binding = {**retained["binding"], "capture_sha256": hashlib.sha256(raw).hexdigest()}
    with pytest.raises(RetainedCaptureMismatch, match="capture_owner_mismatch"):
        evaluate(retained, capture_raw=raw, binding=binding)


def test_binding_fields_are_exact(retained):
    with pytest.raises(RetainedCaptureMismatch, match="binding_fields_invalid"):
        evaluate(retained, binding={**retained["binding"], "extra": 1})
    binding = dict(retained["binding"])
    binding.pop("snapshot_digest")
    with pytest.raises(RetainedCaptureMismatch, match="binding_fields_invalid"):
        evaluate(retained, binding=binding)


def known_set(retained, events):
    return {
        "set_id": "pinned_known_events",
        "events": events,
        "digest": replay_manifest._canonical_sha256(events),
    }


def test_a_known_event_set_must_carry_its_pinned_digest(retained):
    signal = retained["signals"][0]
    events = [
        {
            "event_id": "known_1",
            "market": signal.market,
            "member_identities": [signal.member_identities[0]],
        }
    ]
    supplied = known_set(retained, events)
    with pytest.raises(RetainedCaptureMismatch, match="known_event_set_digest_mismatch"):
        evaluate(retained, known_event_set=supplied)
    wrong = {**retained["binding"], "known_event_set_digest": "0" * 64}
    with pytest.raises(RetainedCaptureMismatch, match="known_event_set_digest_mismatch"):
        evaluate(retained, known_event_set=supplied, binding=wrong)
    self_inconsistent = {**supplied, "digest": "0" * 64}
    pinned = {**retained["binding"], "known_event_set_digest": "0" * 64}
    with pytest.raises(RetainedCaptureMismatch, match="known_event_set_digest_mismatch"):
        evaluate(retained, known_event_set=self_inconsistent, binding=pinned)


def test_a_pinned_known_event_set_measures_recall_only_where_it_has_events(retained):
    signal = retained["signals"][0]
    events = [
        {
            "event_id": "known_1",
            "market": signal.market,
            "member_identities": [signal.member_identities[0]],
        }
    ]
    supplied = known_set(retained, events)
    binding = {**retained["binding"], "known_event_set_digest": supplied["digest"]}
    result = evaluate(retained, known_event_set=supplied, binding=binding)
    assert result["bindings"]["known_event_set"] == "held"
    by_market = result["known_event_recall_by_market"]
    assert by_market[signal.market] == {
        "state": "measured",
        "reason": None,
        "metric": {"numerator": 1, "denominator": 1, "rate": 1.0},
    }
    for market, value in by_market.items():
        if market != signal.market:
            assert value["state"] == "unknown"
            assert value["reason"] == "no_known_events_in_market"
    assert result["known_event_recall"]["state"] == "unknown"
    assert result["known_event_recall"]["reason"] == "markets_without_known_events"


def test_known_event_members_must_be_admitted_observations(retained):
    signal = retained["signals"][0]
    events = [
        {"event_id": "known_1", "market": signal.market, "member_identities": ["za|x|absent"]}
    ]
    supplied = known_set(retained, events)
    binding = {**retained["binding"], "known_event_set_digest": supplied["digest"]}
    with pytest.raises(RetainedCaptureMismatch, match="known_event_member_not_admitted"):
        evaluate(retained, known_event_set=supplied, binding=binding)


def test_signals_must_name_admitted_members_and_stay_before_the_cutoff(retained):
    signal = retained["signals"][0]
    stray = replace(signal, signal_id="stray", member_identities=("za|x|absent",))
    with pytest.raises(RetainedCaptureMismatch, match="signal_member_not_admitted"):
        evaluate(retained, signals=(*retained["signals"], stray))
    future = replace(signal, source_max_observed_at=datetime(2026, 9, 8, 0, 0, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="future source timestamp"):
        evaluate(retained, signals=(future,))
    with pytest.raises(ValueError, match="duplicate signal ID"):
        evaluate(retained, signals=(signal, signal))


def test_without_an_independent_pin_the_capture_is_self_consistent_but_not_real(retained):
    result = evaluate(retained)
    assert result["authority"] == "capture_self_consistent_authority_unverified"
    assert result["real"] is False
    assert result["self_consistency"] == SELF_CONSISTENCY
    assert result["independent_pins"] == {"result_row": "absent"}
    assert result["unproven"] == UNPROVEN
    assert "unverified" in result["authority_note"]
    assert "result ledger row" in result["authority_note"]
    assert "unproven_offline" not in result


def test_a_verified_capture_says_what_the_ledger_row_does_not_cover(retained, repo_pins):
    result = evaluate(retained, result_row=retained["row"])
    assert result["authority"] == "authority_verified"
    assert "approval row" in result["authority_note"]
    assert "input artifacts" in result["authority_note"]
    assert "capture only" in result["authority_note"]
    for supplied in ("signals", "known events", "quality flags"):
        assert supplied in result["authority_note"]


def resealed_row(retained, change_payload=None, **row_changes):
    from src.analysis.open_intelligence import execution_approval
    from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

    row = dict(retained["row"])
    payload = json.loads(row["canonical_result_json"])
    if change_payload is not None:
        change_payload(payload)
    row.update(row_changes)
    row["canonical_result_json"] = canonical_bytes(payload).decode()
    row["result_digest"] = canonical_digest(payload)
    row["result_id"] = execution_approval.result_id(
        row["consumption_id"],
        row["result_reference"],
        row["result_digest"],
        row["status"],
        datetime.fromisoformat(row["completed_at"].replace("Z", "+00:00")),
    )
    return row


def repinned(retained, row):
    """The registry and binding pinned to a resealed row, so only the payload checks bite."""
    pins = {"result_id": row["result_id"], "result_digest": row["result_digest"]}
    return {
        "registry_entry": replace(retained["entry"], **pins),
        "binding": {**retained["binding"], **pins},
    }


def test_a_result_row_unchanged_but_resealed_verifies_under_its_own_pins(retained):
    # Tightened: the pins here come from the caller's registry entry, not the repo's,
    # so the row holds against them but proves no authority.
    row = resealed_row(retained)
    assert row == retained["row"]
    result = evaluate(retained, result_row=row, **repinned(retained, row))
    assert result["authority"] == "capture_self_consistent_authority_unverified"
    assert result["real"] is False
    assert result["registry_entry_source"] == "caller_supplied"
    assert result["unproven"] == UNPROVEN
    assert result["unproven_reasons"] == ["registry_entry_not_from_repo"]
    assert "did not come from the repo" in result["authority_note"]


@pytest.mark.parametrize("repo", ["other", "missing", "raises"])
def test_a_caller_registry_entry_never_verifies(retained, monkeypatch, repo):
    from src.analysis.open_intelligence import protected_context_registry

    def profile_for(cutoff):
        if repo == "raises":
            raise ValueError("unknown cutoff")
        if repo == "missing":
            return None
        return replace(retained["entry"], profile_id="protected_context_20260906_v1")

    monkeypatch.setattr(protected_context_registry, "profile_for", profile_for)
    result = evaluate(retained, result_row=retained["row"])
    assert result["authority"] == "capture_self_consistent_authority_unverified"
    assert result["real"] is False
    assert result["independent_pins"] == {"result_row": "held"}
    assert result["unproven_reasons"] == ["registry_entry_not_from_repo"]


def test_a_caller_entry_equal_to_the_repo_entry_verifies(retained, repo_pins):
    entry = replace(retained["entry"])
    assert entry is not repo_pins
    result = evaluate(retained, result_row=retained["row"], registry_entry=entry)
    assert result["authority"] == "authority_verified"
    assert result["registry_entry_source"] == "repo"


def test_without_a_row_the_reason_is_the_missing_row(retained, repo_pins):
    result = evaluate(retained)
    assert result["unproven_reasons"] == ["no_result_row"]
    assert result["registry_entry_source"] == "repo"


def test_a_row_for_another_v1_operation_refuses(retained):
    execution = (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "trends-engine-oi-approval-bootstrap-staging/executions/bootstrap-1"
    )
    row = resealed_row(
        retained,
        operation="bootstrap_migration_apply",
        execution_name=execution,
        result_reference=execution + "#source-snapshot",
    )
    with pytest.raises(RetainedCaptureMismatch, match="result_row_not_a_completed_capture"):
        evaluate(retained, result_row=row, **repinned(retained, row))


def test_a_result_row_that_is_not_the_pinned_result_refuses(retained):
    def change(payload):
        payload["query_count"] = payload["query_count"] - 1

    row = resealed_row(retained, change)
    with pytest.raises(RetainedCaptureMismatch, match="result_row_pin_mismatch"):
        evaluate(retained, result_row=row)


@pytest.mark.parametrize(
    ("field", "value"),
    [("result_id", "exr_" + "0" * 64), ("result_digest", "0" * 64)],
)
def test_a_registry_pin_the_row_does_not_carry_refuses(retained, field, value):
    pins = {field: value}
    with pytest.raises(RetainedCaptureMismatch, match="result_row_pin_mismatch"):
        evaluate(
            retained,
            result_row=retained["row"],
            registry_entry=replace(retained["entry"], **pins),
            binding={**retained["binding"], **pins},
        )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda p: p["stored_artifact"].update(sha256="0" * 64), "result_row_capture_mismatch"),
        (lambda p: p.update(stored_artifact=None), "result_row_capture_mismatch"),
        (lambda p: p.update(snapshot_digest="0" * 64), "result_row_snapshot_mismatch"),
        (lambda p: p.update(capture_receipt_digest="0" * 64), "result_row_snapshot_mismatch"),
        (lambda p: p.update(missing_checks=["x"]), "result_row_scope_mismatch"),
        (lambda p: p.update(client_scope_id="other"), "result_row_scope_mismatch"),
        (lambda p: p.update(market_scope=["ke", "ng"]), "result_row_scope_mismatch"),
        (lambda p: p.update(cutoff_date="2026-09-06"), "result_row_scope_mismatch"),
        (lambda p: p.update(source_as_of="2026-09-09T00:00:00+00:00"), "result_row_scope_mismatch"),
    ],
)
def test_a_pinned_result_row_must_describe_this_capture(retained, change, code):
    row = resealed_row(retained, change)
    with pytest.raises(RetainedCaptureMismatch, match=code):
        evaluate(retained, result_row=row, **repinned(retained, row))


def test_a_pinned_result_row_must_be_a_completed_capture(retained):
    row = resealed_row(retained, status="failed")
    with pytest.raises(RetainedCaptureMismatch, match="result_row_not_a_completed_capture"):
        evaluate(retained, result_row=row, **repinned(retained, row))
    reference = retained["row"]["execution_name"] + "#other"
    row = resealed_row(retained, result_reference=reference)
    with pytest.raises(RetainedCaptureMismatch, match="result_row_not_a_completed_capture"):
        evaluate(retained, result_row=row, **repinned(retained, row))


@pytest.mark.parametrize("field", ["consumption_id", "manifest_sha256"])
def test_a_pinned_result_row_must_name_the_pinned_consumption(retained, field):
    value = retained["row"][field]
    row = resealed_row(retained, **{field: value[:-1] + ("0" if value[-1] != "0" else "1")})
    with pytest.raises(RetainedCaptureMismatch, match="result_row_owner_mismatch"):
        evaluate(retained, result_row=row, **repinned(retained, row))


@pytest.mark.parametrize(
    "row",
    [
        "not a row",
        {"result_id": "exr_" + "0" * 64},
    ],
)
def test_a_malformed_result_row_refuses(retained, row):
    with pytest.raises(RetainedCaptureMismatch, match="result_row_invalid"):
        evaluate(retained, result_row=row)


@pytest.mark.parametrize(
    "change",
    [
        {"completed_at": "not a time"},
        {"completed_at": "2026-09-09T12:00:05.000000Z"},
        {"canonical_result_json": '{"x":1}'},
        {"status": "failed"},
    ],
)
def test_a_result_row_edited_without_resealing_refuses(retained, change):
    with pytest.raises(RetainedCaptureMismatch, match="result_row_invalid"):
        evaluate(retained, result_row={**retained["row"], **change})


QUALITY_METRICS = (
    "duplicate_rate",
    "foreign_leakage",
    "receipt_completeness",
    "evidence_coverage",
    "geo_coverage",
)


def test_unmeasured_quality_flags_give_null_rates(retained):
    signals = tuple(
        replace(signal, membership_complete=False, evidence_ready=False, geo_proven=False)
        for signal in retained["signals"]
    )
    quality = evaluate(retained, signals=signals)["quality"]
    assert quality["measured"] is False
    assert quality["human_coherence_status"] == "pending"
    for name in QUALITY_METRICS:
        assert quality[name]["rate"] is None
        assert quality[name]["denominator"] == len(signals)
    for name in ("receipt_completeness", "evidence_coverage", "geo_coverage"):
        assert quality[name]["numerator"] == 0


def test_quality_is_unmeasured_unless_the_caller_says_the_flags_are_measurements(retained):
    quality = evaluate(retained)["quality"]
    assert quality["measured"] is False
    assert all(quality[name]["rate"] is None for name in QUALITY_METRICS)
    assert quality["receipt_completeness"]["numerator"] == len(retained["signals"])


def many_signals(retained, count=6):
    """count signals over the fixture's admitted members, each with its own cluster."""
    base = retained["signals"][0]
    return tuple(
        replace(base, signal_id=f"many_{index}", cluster_signature=f"cluster_{index}")
        for index in range(count)
    )


def test_measured_quality_counts_each_flag_duplicates_and_leakage(retained):
    signals = list(many_signals(retained))
    signals = [
        replace(
            signal,
            membership_complete=index % 2 == 0,
            evidence_ready=index % 3 == 0,
            geo_proven=index == 0,
        )
        for index, signal in enumerate(signals)
    ]
    first = signals[0]
    same_market = [s for s in signals[1:] if s.market == first.market]
    assert same_market, "the fixture needs two signals in one market"
    twin = same_market[0]
    signals[signals.index(twin)] = replace(twin, cluster_signature=first.cluster_signature)
    count = len(signals)
    quality = evaluate(retained, signals=tuple(signals), quality_measured=True)["quality"]
    assert quality["measured"] is True
    expected = {
        "duplicate_rate": 1,
        "foreign_leakage": 0,
        "receipt_completeness": sum(1 for index in range(count) if index % 2 == 0),
        "evidence_coverage": sum(1 for index in range(count) if index % 3 == 0),
        "geo_coverage": 1,
    }
    for name, numerator in expected.items():
        assert quality[name] == {
            "numerator": numerator,
            "denominator": count,
            "rate": numerator / count,
        }


def test_quality_measured_must_be_a_boolean(retained):
    with pytest.raises(RetainedCaptureMismatch, match="quality_measured_invalid"):
        evaluate(retained, quality_measured="yes")


def test_the_stable_replay_digest_ignores_only_runtime(retained):
    base = {
        "provider_outputs": [{"runtime_ms": 1.0, "pair_count": 2}],
        "nested": {"runtime_ms": 3.0},
        "replay_digest": "a" * 64,
    }
    same = {
        "provider_outputs": [{"runtime_ms": 9.0, "pair_count": 2}],
        "nested": {"runtime_ms": 4.0},
        "replay_digest": "b" * 64,
    }
    other = {
        "provider_outputs": [{"runtime_ms": 1.0, "pair_count": 3}],
        "nested": {"runtime_ms": 3.0},
        "replay_digest": "a" * 64,
    }
    digest = replay_manifest.stable_replay_digest
    assert digest(base) == digest(same)
    assert digest(base) != digest(other)
    assert base["provider_outputs"][0]["runtime_ms"] == 1.0
    assert digest(base) == replay_manifest.replay._digest(
        {
            "provider_outputs": [{"runtime_ms": None, "pair_count": 2}],
            "nested": {"runtime_ms": None},
        }
    )


def test_the_same_capture_twice_gives_the_same_stable_digest(retained, monkeypatch):
    import itertools
    import time

    ticks = itertools.accumulate(itertools.count(1))
    monkeypatch.setattr(time, "perf_counter", lambda: float(next(ticks)))
    first = evaluate(retained)
    second = evaluate(retained)
    assert first["replay_digest"] != second["replay_digest"]
    assert first["stable_replay_digest"] == second["stable_replay_digest"]
    payload = replay_manifest.retained_replay_payload(
        capture_raw=retained["raw"],
        binding=retained["binding"],
        registry_entry=retained["entry"],
        semantic_provider=Provider(),
    )
    assert first["stable_replay_digest"] == replay_manifest.stable_replay_digest(payload)
    assert first["components_digest"] == replay_manifest.replay._digest(
        payload["components_by_candidate"]
    )


def test_the_retained_payload_matches_the_online_protected_reader(retained):
    payload = replay_manifest.retained_replay_payload(
        capture_raw=retained["raw"],
        binding=retained["binding"],
        registry_entry=retained["entry"],
        semantic_provider=Provider(),
    )
    online = retained["online"]
    assert set(payload) == set(online)
    assert payload["components_by_candidate"] == online["components_by_candidate"]
    assert payload["observations"] == online["observations"]
    assert payload["production_snapshot"] == online["production_snapshot"]
    differing = sorted(key for key in payload if payload[key] != online[key])
    assert set(differing) <= PARITY_EXEMPT, differing
    without_runtime = replay_manifest._without_runtime
    assert without_runtime(payload["provider_outputs"]) == without_runtime(
        online["provider_outputs"]
    )
    scope_differs = {
        key for key in payload["scope"] if payload["scope"][key] != online["scope"][key]
    }
    assert scope_differs == SCOPE_FIELDS_THAT_DIFFER
    result = evaluate(retained)
    assert result["counts"]["components"] == len(online["components_by_candidate"])
    assert result["counts"]["observations"] == len(online["observations"])
    assert result["components_digest"] == replay_manifest.replay._digest(
        online["components_by_candidate"]
    )


def test_a_source_as_of_the_registry_does_not_pin_refuses(retained):
    entry = replace(retained["entry"], source_as_of="2026-09-09T00:00:00+00:00")
    with pytest.raises(RetainedCaptureMismatch, match="registry_pin_mismatch:source_as_of"):
        evaluate(retained, registry_entry=entry)


def test_a_registry_entry_for_another_result_family_refuses(retained):
    entry = replace(retained["entry"], result_contract_version="open_intelligence_other_v2")
    with pytest.raises(RetainedCaptureMismatch, match="registry_entry_not_v1_capture"):
        evaluate(retained, registry_entry=entry)


def test_a_registry_entry_must_be_the_registry_type(retained):
    from dataclasses import asdict

    with pytest.raises(RetainedCaptureMismatch, match="registry_entry_invalid"):
        evaluate(retained, registry_entry=asdict(retained["entry"]))


def expected_sample(signals, cutoff, per_market):
    chosen = []
    for market in sorted({signal.market for signal in signals}):
        ranked = sorted(
            (signal for signal in signals if signal.market == market),
            key=lambda signal: hashlib.sha256(f"{cutoff}|{signal.signal_id}".encode()).hexdigest(),
        )
        chosen.extend(signal.signal_id for signal in ranked[:per_market])
    return chosen


def test_the_review_sample_takes_sample_per_market_from_each_market(retained):
    signals = many_signals(retained)
    cutoff = retained["binding"]["cutoff_date"]
    one = evaluate(retained, signals=signals, sample_per_market=1)["review_sample_signal_ids"]
    two = evaluate(retained, signals=signals, sample_per_market=2)["review_sample_signal_ids"]
    assert one == expected_sample(signals, cutoff, 1)
    assert two == expected_sample(signals, cutoff, 2)
    assert len(one) == 1
    assert len(two) == 2
    assert two[0] == one[0]
    for bad in (0, True, 1.0):
        with pytest.raises(ValueError, match="sample_per_market"):
            evaluate(retained, sample_per_market=bad)


def test_the_certified_entry_point_is_unchanged():
    import inspect

    source = inspect.getsource(replay_manifest.evaluate_replay_manifest)
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == (
        EVALUATE_REPLAY_MANIFEST_SOURCE_SHA256
    )


EVALUATE_REPLAY_MANIFEST_SOURCE_SHA256 = (
    "72dc53b07fc0de67953b83527a7440466e65445b154702b9f904971c9dd06d51"
)


BUILD = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def runner(monkeypatch, retained):
    from scripts.staging import run_replay_manifest
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setattr(run_replay_manifest, "checkout_state", lambda: (BUILD, ""))
    monkeypatch.setattr(run_replay_manifest, "hidden_index_entries", lambda: ())
    monkeypatch.setattr(
        protected_context_registry,
        "profile_for",
        lambda cutoff: retained["entry"] if cutoff == retained["entry"].cutoff_date else None,
    )
    return run_replay_manifest


def signal_rows(retained, signals=None):
    return [
        {
            "signal_id": item.signal_id,
            "market": item.market,
            "cluster_signature": item.cluster_signature,
            "member_identities": list(item.member_identities),
            "source_max_observed_at": "2026-09-07T12:00:00Z",
            "membership_complete": True,
            "evidence_ready": True,
            "geo_proven": True,
        }
        for item in (retained["signals"] if signals is None else signals)
    ]


def retained_entry(
    tmp_path,
    retained,
    *,
    binding=None,
    raw=None,
    name="capture",
    row=None,
    sample=1,
    signals=None,
):
    (tmp_path / f"{name}.json").write_bytes(retained["raw"] if raw is None else raw)
    (tmp_path / f"{name}_signals.json").write_text(
        json.dumps(signal_rows(retained, signals)), encoding="utf-8"
    )
    if row is not None:
        (tmp_path / f"{name}_result_row.json").write_text(json.dumps(row), encoding="utf-8")
    return {
        "kind": "retained_capture",
        "capture_path": f"{name}.json",
        "binding": dict(retained["binding"]) if binding is None else binding,
        "signals_path": f"{name}_signals.json",
        "known_event_set_path": None,
        "result_row_path": None if row is None else f"{name}_result_row.json",
        "quality_measured": False,
        "sample_per_market": sample,
    }


def test_runner_marks_a_fully_bound_retained_receipt_real(tmp_path, retained, runner):
    entry = retained_entry(tmp_path, retained, row=retained["row"])
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["refused"] == []
    assert receipt["evidence_scope"] == "retained_capture"
    assert receipt["authority"] == "authority_verified"
    assert receipt["real"] is True
    (section,) = receipt["cutoffs"]
    assert section["input_kind"] == "retained_capture"
    assert section["authority"] == "authority_verified"
    assert section["real"] is True
    assert section["quality"]["measured"] is False
    assert section["known_event_recall"]["state"] == "unknown"
    assert section["known_event_gap"] == replay_manifest.KNOWN_EVENT_GAP
    assert "known event" in receipt["evidence_note"]


def test_runner_refuses_a_binding_mismatch_and_never_marks_the_receipt_real(
    tmp_path, retained, runner
):
    binding = {**retained["binding"], "snapshot_digest": "0" * 64}
    entry = retained_entry(tmp_path, retained, binding=binding)
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 0
    assert receipt["refused"][0]["input_path"] == "capture.json"
    assert receipt["refused"][0]["detail"] == "snapshot_digest_mismatch"
    assert receipt["real"] is False
    assert receipt["evidence_scope"] == "retained_capture_refused"


def test_runner_refuses_a_tampered_local_capture(tmp_path, retained, runner):
    raw = retained["raw"].replace(b'"ke"', b'"KE"', 1)
    entry = retained_entry(tmp_path, retained, raw=raw)
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["refused"][0]["detail"] == "capture_sha256_mismatch"
    assert receipt["real"] is False


def test_a_real_receipt_needs_every_bundle_bound(tmp_path, retained, runner):
    good = retained_entry(tmp_path, retained)
    bad = retained_entry(
        tmp_path,
        retained,
        binding={**retained["binding"], "result_digest": "0" * 64},
        name="second",
    )
    receipt = runner.run_replay_manifest(
        {"bundles": [good, bad]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 1
    assert receipt["refused"][0]["detail"] == "registry_pin_mismatch:result_digest"
    assert receipt["real"] is False


def test_runner_keeps_an_unverified_capture_from_being_real(tmp_path, retained, runner):
    receipt = runner.run_replay_manifest(
        {"bundles": [retained_entry(tmp_path, retained)]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["refused"] == []
    assert receipt["evidence_scope"] == "retained_capture"
    assert receipt["authority"] == "capture_self_consistent_authority_unverified"
    assert receipt["real"] is False
    assert "unverified" in receipt["evidence_note"]
    (section,) = receipt["cutoffs"]
    assert section["real"] is False
    assert section["unproven"] == UNPROVEN


@pytest.mark.parametrize(
    ("authority", "real"),
    [
        ("capture_self_consistent_authority_unverified", True),
        ("authority_verified", False),
        ("authority_verified", "true"),
        (None, True),
    ],
)
def test_runner_never_stamps_real_on_a_section_that_is_not_verified(
    tmp_path, retained, runner, monkeypatch, authority, real
):
    original = runner.evaluate_retained_replay

    def claimed(**kwargs):
        return {**original(**kwargs), "authority": authority, "real": real}

    monkeypatch.setattr(runner, "evaluate_retained_replay", claimed)
    entry = retained_entry(tmp_path, retained, row=retained["row"])
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 1
    assert receipt["real"] is False
    assert receipt["authority"] == "capture_self_consistent_authority_unverified"


def test_runner_refuses_a_second_bundle_for_the_same_cutoff(tmp_path, retained, runner):
    first = retained_entry(tmp_path, retained, row=retained["row"])
    second = retained_entry(tmp_path, retained, row=retained["row"], name="again")
    receipt = runner.run_replay_manifest(
        {"bundles": [first, second]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 1
    assert receipt["refused"] == [
        {
            "index": 1,
            "input_path": "again.json",
            "reason": "ReplayManifestRefusal",
            "detail": "duplicate_cutoff",
        }
    ]
    assert receipt["real"] is False
    assert receipt["evidence_scope"] == "retained_capture_refused"


def test_runner_passes_sample_per_market_through(tmp_path, retained, runner):
    signals = many_signals(retained)
    entry = retained_entry(tmp_path, retained, sample=2, signals=signals)
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    (section,) = receipt["cutoffs"]
    sample = section["review_sample_signal_ids"]
    assert sample == expected_sample(signals, retained["binding"]["cutoff_date"], 2)
    assert len(sample) == 2


def test_runner_passes_the_quality_mark_through(tmp_path, retained, runner):
    entry = {**retained_entry(tmp_path, retained), "quality_measured": True}
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    (section,) = receipt["cutoffs"]
    assert section["quality"]["measured"] is True
    assert section["quality"]["geo_coverage"]["rate"] == 1.0


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"quality_measured": "no"}, "bundle_quality_measured_invalid"),
        ({"quality_measured": None}, "bundle_quality_measured_invalid"),
        ({"result_row_path": ""}, "bundle_result_row_path_invalid"),
        ({"result_row_path": 3}, "bundle_result_row_path_invalid"),
        ({"sample_per_market": 0}, "bundle_sample_per_market_invalid"),
        ({"sample_per_market": True}, "bundle_sample_per_market_invalid"),
    ],
)
def test_runner_refuses_invalid_retained_fields(tmp_path, retained, runner, change, code):
    entry = {**retained_entry(tmp_path, retained), **change}
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 0
    assert receipt["refused"][0]["detail"] == code


@pytest.mark.parametrize(
    "mutation",
    [
        lambda entry: entry.pop("known_event_set_path"),
        lambda entry: entry.update(kind="other"),
        lambda entry: entry.update(binding="x"),
        lambda entry: entry.update(extra=1),
    ],
)
def test_runner_refuses_malformed_retained_entries(tmp_path, retained, runner, mutation):
    entry = retained_entry(tmp_path, retained)
    mutation(entry)
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 0
    assert receipt["real"] is False


def test_synthetic_receipts_stay_synthetic_and_carry_no_real_mark(tmp_path, runner):
    from tests.unit.test_open_intelligence_replay import bundle_payload, bytes_digest
    from tests.unit.test_replay_manifest_runner import signal_rows as v1_signals

    payload = bundle_payload()
    (tmp_path / "first.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "first_signals.json").write_text(json.dumps(v1_signals()), encoding="utf-8")
    entry = {
        "input_path": "first.json",
        "expected_input_sha256": bytes_digest(payload),
        "signals_path": "first_signals.json",
        "sample_per_market": 1,
    }
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evidence_scope"] == "synthetic_test_only"
    assert "real" not in receipt
    assert "input_kind" not in receipt["cutoffs"][0]


def test_mixing_synthetic_and_retained_is_never_real(tmp_path, retained, runner):
    from tests.unit.test_open_intelligence_replay import bundle_payload, bytes_digest
    from tests.unit.test_replay_manifest_runner import signal_rows as v1_signals

    payload = bundle_payload()
    (tmp_path / "first.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "first_signals.json").write_text(json.dumps(v1_signals()), encoding="utf-8")
    v1 = {
        "input_path": "first.json",
        "expected_input_sha256": bytes_digest(payload),
        "signals_path": "first_signals.json",
        "sample_per_market": 1,
    }
    receipt = runner.run_replay_manifest(
        {"bundles": [v1, retained_entry(tmp_path, retained)]},
        base_dir=tmp_path,
        build_version=BUILD,
    )
    assert receipt["evaluated_count"] == 2
    assert receipt["evidence_scope"] == "mixed"
    assert receipt["real"] is False
