"""The daily compose seam that picks the capture a closed cutoff composes from.

Every entry here is built through the staging source profile the capture stage builds,
so the registry readback the selector reads is the shape the daily path registers.
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta

import pytest
from src.analysis.open_intelligence import staging_source_profile as profiles
from src.analysis.open_intelligence.capture_registry import validate_capture_entry
from src.analysis.open_intelligence.daily_capture_selection import (
    CAPTURE_NONE_ELIGIBLE,
    capture_entry_selector,
)

CUTOFF = datetime(2026, 9, 13, tzinfo=UTC)
DAY = date(2026, 9, 12)
POLICY = "3" * 64
OTHER_POLICY = "9" * 64
SCOPE = "scope-42"
STARTED = CUTOFF + timedelta(minutes=30)
COMPLETED = CUTOFF + timedelta(hours=1)
SNAPSHOT = COMPLETED + timedelta(minutes=5)
NOW = CUTOFF + timedelta(hours=6)


def _run(day=DAY, policy=POLICY):
    return {
        "receipt": {
            "contract_version": "collection_receipt_v1",
            "run_id": f"run-{day.isoformat()}",
            "execution_id": "exec-1",
            "source_sha": "a" * 40,
            "image_uri": "region-docker.pkg.dev/project/repo/image@sha256:" + "b" * 64,
            "policy_sha256": policy,
            "profile_sha256": "6" * 64,
            "cutoff": day.isoformat(),
            "market_states": {"za": "collected", "ng": "collected", "ke": "collected"},
            "raw_rows_persisted": 10,
            "enriched_rows_persisted": 5,
            "funded_close": None,
            "complete": True,
        },
        "collection_started_at": datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC)
        + timedelta(minutes=30),
        "collection_completed_at": datetime.combine(
            day + timedelta(days=1), datetime.min.time(), UTC
        )
        + timedelta(hours=1),
    }


def capture(
    generation,
    *,
    state="succeeded",
    available=None,
    expires_at=None,
    policy=POLICY,
    day=DAY,
    scope=SCOPE,
):
    run = _run(day, policy)
    if available is None:
        available = run["collection_completed_at"] + timedelta(minutes=7)
    built = profiles.build_staging_source_profile(
        run,
        snapshot_as_of=run["collection_completed_at"] + timedelta(minutes=5),
        scope=scope,
        schema_digest="2" * 64,
        source_digest="4" * 64,
        now=run["collection_completed_at"] + timedelta(minutes=10),
    )
    return built.capture_entry(
        operation_id=f"op-{generation}",
        result_id=f"result-{generation}",
        content_digest="1" * 64,
        image_digest="5" * 64,
        generation=str(generation),
        completion_state=state,
        capture_available_at=available,
        expires_at=expires_at,
    )


class Registry:
    """The registry readback seam: read only, counting every read, holding no writer."""

    def __init__(self, entries=(), status="ok"):
        self.entries = list(entries)
        self.status = status
        self.reads = []

    def __call__(self, profile_id):
        self.reads.append(profile_id)
        if self.status != "ok":
            return {"status": self.status}
        return {"status": "ok", "entries": list(self.entries)}


def selector(registry, *, now=NOW, records=None):
    return capture_entry_selector(
        read_registry=registry,
        source_policy_digest=POLICY,
        scope=SCOPE,
        clock=lambda: now,
        record=None if records is None else records.append,
    )


def test_newer_ineligible_entries_do_not_hide_the_latest_valid_capture():
    valid = capture(10)
    older = capture(9, available=SNAPSHOT + timedelta(minutes=1))
    registry = Registry(
        [
            older,
            valid,
            capture(11, state="partial", available=SNAPSHOT + timedelta(minutes=3)),
            capture(12, state="failed", available=SNAPSHOT + timedelta(minutes=4)),
            capture(
                13,
                available=SNAPSHOT + timedelta(minutes=5),
                expires_at=NOW - timedelta(minutes=1),
            ),
            capture(14, available=NOW + timedelta(hours=1)),
        ]
    )
    records = []
    capture_entry_for = selector(registry, records=records)

    assert capture_entry_for(CUTOFF) == validate_capture_entry(valid)
    [record] = records
    assert record["outcome"] == "selected"
    assert record["selected"]["result_id"] == "result-10"
    assert [item["reason"] for item in record["diagnostics"]] == [
        "eligible",
        "eligible",
        "not_succeeded:partial",
        "not_succeeded:failed",
        "expired",
        "not_yet_available",
    ]


def test_an_entry_of_another_source_policy_is_never_selected():
    ours = capture(10)
    theirs_newer = capture(20, policy=OTHER_POLICY, available=SNAPSHOT + timedelta(minutes=9))
    records = []
    assert selector(Registry([theirs_newer, ours]), records=records)(CUTOFF) == ours
    assert records[-1]["diagnostics"][0]["reason"] == "policy_mismatch"
    alone = selector(Registry([theirs_newer]), records=records)
    assert alone(CUTOFF) is None
    assert records[-1]["outcome"] == "refused"


def test_an_entry_of_another_day_or_scope_is_never_selected():
    records = []
    entries = [capture(30, day=DAY + timedelta(days=1)), capture(31, scope="scope-other")]
    assert selector(Registry(entries), now=NOW + timedelta(days=2), records=records)(CUTOFF) is None
    assert [item["reason"] for item in records[-1]["diagnostics"]] == [
        "profile_mismatch",
        "scope_mismatch",
    ]


def test_no_eligible_capture_is_a_recorded_refusal_and_nothing_is_written():
    records = []
    registry = Registry([capture(11, state="partial"), capture(12, available=NOW + timedelta(1))])
    capture_entry_for = selector(registry, records=records)

    assert capture_entry_for(CUTOFF) is None
    [record] = records
    assert record["outcome"] == "refused"
    assert record["code"] == CAPTURE_NONE_ELIGIBLE
    assert record["cutoff_utc"] == CUTOFF.isoformat()
    assert record["source_policy_digest"] == POLICY
    assert record["profile_id"] == f"staging-{DAY.isoformat()}-{POLICY[:16]}"
    assert record["selected"] is None
    assert [item["reason"] for item in record["diagnostics"]] == [
        "not_succeeded:partial",
        "not_yet_available",
    ]
    assert registry.reads == [record["profile_id"]]
    assert not hasattr(registry, "write")
    assert not hasattr(registry, "create")

    empty = []
    assert selector(Registry([]), records=empty)(CUTOFF) is None
    assert empty[0]["code"] == CAPTURE_NONE_ELIGIBLE
    assert empty[0]["diagnostics"] == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("access_denied", "capture_read_access_denied"),
        ("metadata_unavailable", "capture_read_metadata_unavailable"),
        ("invalid_authority", "capture_read_authority_invalid"),
    ],
)
def test_a_refused_registry_read_is_never_an_empty_registry(status, code):
    records = []
    with pytest.raises(ValueError, match=code):
        selector(Registry([capture(10)], status=status), records=records)(CUTOFF)
    assert records[-1]["outcome"] == "refused"
    assert records[-1]["code"] == code


def test_a_fresh_eligible_capture_recovers_on_the_next_call_exactly_once():
    registry = Registry([capture(11, state="failed")])
    records = []
    capture_entry_for = selector(registry, records=records)
    assert capture_entry_for(CUTOFF) is None

    fresh = capture(12, available=SNAPSHOT + timedelta(minutes=4))
    registry.entries.append(fresh)
    assert capture_entry_for(CUTOFF) == fresh
    assert [record["outcome"] for record in records] == ["refused", "selected"]
    assert len(registry.reads) == 2
    # Reading again changes nothing: the same capture, one more read, no other effect.
    assert capture_entry_for(CUTOFF) == fresh
    assert len(registry.reads) == 3
    assert registry.entries[-1] is fresh


def test_a_capture_not_yet_available_recovers_once_its_instant_passes():
    later = capture(10, available=NOW + timedelta(minutes=30))
    clock = {"now": NOW}
    capture_entry_for = capture_entry_selector(
        read_registry=Registry([later]),
        source_policy_digest=POLICY,
        scope=SCOPE,
        clock=lambda: clock["now"],
    )
    assert capture_entry_for(CUTOFF) is None
    clock["now"] = NOW + timedelta(hours=1)
    assert capture_entry_for(CUTOFF) == later


def test_the_callable_fits_the_compose_seam_capture_entry_for_cutoff():
    capture_entry_for = selector(Registry([capture(10)]))
    parameters = list(inspect.signature(capture_entry_for).parameters.values())
    assert [parameter.name for parameter in parameters] == ["cutoff"]
    found = capture_entry_for(CUTOFF)
    assert found == validate_capture_entry(found)
    assert found["observation_window_end"] == CUTOFF
    assert capture_entry_for(cutoff=CUTOFF) == found
    for bad in (CUTOFF.replace(tzinfo=None), CUTOFF + timedelta(hours=1), DAY, "2026-09-13"):
        with pytest.raises(ValueError, match="capture_cutoff_invalid"):
            capture_entry_for(bad)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"source_policy_digest": "x"}, "capture_selector_invalid:source_policy_digest"),
        ({"scope": " "}, "capture_selector_invalid:scope"),
        ({"read_registry": None}, "capture_selector_invalid:read_registry"),
        ({"clock": None}, "capture_selector_invalid:clock"),
        ({"record": "sink"}, "capture_selector_invalid:record"),
    ],
)
def test_the_selector_refuses_an_unbound_binding(overrides, code):
    arguments = {
        "read_registry": Registry(),
        "source_policy_digest": POLICY,
        "scope": SCOPE,
        "clock": lambda: NOW,
        "record": None,
    }
    arguments.update(overrides)
    with pytest.raises(ValueError, match=code):
        capture_entry_selector(**arguments)


def test_two_bindings_of_one_profile_are_a_recorded_conflict_not_a_choice():
    records = []
    entries = [capture(10), capture(11, scope="scope-other")]
    with pytest.raises(ValueError, match="profile_conflict"):
        selector(Registry(entries), records=records)(CUTOFF)
    assert records[-1]["code"] == "profile_conflict"
