"""Boundary tests for daily capture admission and latest-capture selection."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from src.analysis.open_intelligence.capture_registry import (
    REQUIRED_FIELDS,
    admit_capture,
    admit_validated_capture,
    explain_capture_selection,
    read_outcome_entries,
    select_latest_capture,
    validate_capture_entry,
)

NOW = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)
# The observation day the base entry closes: its window ends at the next UTC midnight.
CAPTURE_DAY = "20260911"
# The estate and the table naming the one capture producer that can execute writes
# into, as the plan module names them. An entry stands for the tables that producer
# creates, so the fixtures carry those names and nothing else.
CAPTURE_ESTATE = "ogilvy-trends-v2.trends_v2_staging"
CAPTURE_V2_ESTATE = "ogilvy-trends-v2.intelligence_42_sources_staging"
CAPTURE_DATASETS = ("trends_v2_staging",)


def capture_table(lane, day=CAPTURE_DAY):
    return f"open_intelligence_v3_source_{day}_{lane}"


def entry(**overrides):
    base = {
        "profile_id": "daily-2026-09-12-a",
        "profile_version": "42_capture_v1",
        "operation_id": "op-1",
        "result_id": "result-1",
        "source_kind": "staging_source_run",
        "source_tables": ["raw_content", "enriched_content"],
        "snapshot_tables": [capture_table("raw_content"), capture_table("enriched_content")],
        "scope": "scope-42",
        "markets": ["za", "ng", "ke"],
        "observation_window_end": WINDOW_END,
        "collection_started_at": WINDOW_END + timedelta(minutes=30),
        "collection_completed_at": WINDOW_END + timedelta(hours=1),
        "snapshot_as_of": WINDOW_END + timedelta(hours=2),
        "capture_available_at": WINDOW_END + timedelta(hours=3),
        "content_digest": "1" * 64,
        "schema_digest": "2" * 64,
        "policy_digest": "3" * 64,
        "source_digest": "4" * 64,
        "image_digest": "5" * 64,
        "generation": "10",
        "completion_state": "succeeded",
    }
    base.update(overrides)
    return base


def shifted(days: int, **overrides):
    delta = timedelta(days=days)
    fields = {
        "profile_id": f"daily-{(WINDOW_END + delta).date().isoformat()}",
        "operation_id": f"op-{days}",
        "result_id": f"result-{days}",
        "observation_window_end": WINDOW_END + delta,
        "collection_started_at": WINDOW_END + delta + timedelta(minutes=30),
        "collection_completed_at": WINDOW_END + delta + timedelta(hours=1),
        "snapshot_as_of": WINDOW_END + delta + timedelta(hours=2),
        "capture_available_at": WINDOW_END + delta + timedelta(hours=3),
    }
    fields.update(overrides)
    return entry(**fields)


def test_changed_authority_and_duplicate_conflict():
    entry = {
        "profile_id": "daily-a",
        "digest": "a" * 64,
        "generation": "10",
        "completion_state": "succeeded",
    }
    with pytest.raises(ValueError, match="authority_mismatch"):
        admit_capture(entry, {**entry, "digest": "b" * 64})
    with pytest.raises(ValueError, match="profile_conflict"):
        admit_capture(entry, entry, existing={**entry, "generation": "9"})


def test_valid_entry_admits_a_normalized_copy():
    candidate = entry()
    admitted = admit_validated_capture(candidate, dict(candidate), existing=dict(candidate))
    assert admitted == validate_capture_entry(candidate)
    assert admitted["source_tables"] == ("enriched_content", "raw_content")
    assert admitted["expires_at"] is None
    assert admitted is not candidate


@pytest.mark.parametrize(
    ("field", "altered"),
    [
        ("content_digest", "f" * 64),
        ("schema_digest", "e" * 64),
        ("source_tables", ["raw_content"]),
        ("snapshot_tables", ["snapshot_raw", "snapshot_other"]),
        ("generation", "11"),
        ("scope", "scope-43"),
        ("markets", ["za", "ng"]),
        ("observation_window_end", WINDOW_END - timedelta(days=1)),
        ("snapshot_as_of", WINDOW_END + timedelta(hours=2, seconds=1)),
    ],
)
def test_altered_valid_shaped_verification_is_refused(field, altered):
    candidate = entry()
    with pytest.raises(ValueError, match="authority_mismatch"):
        admit_validated_capture(candidate, entry(**{field: altered}))


def test_caller_success_string_is_insufficient():
    candidate = entry()
    with pytest.raises(ValueError, match="authority_mismatch"):
        admit_validated_capture(candidate, entry(completion_state="failed"))
    failed = entry(completion_state="failed")
    with pytest.raises(ValueError, match="authority_mismatch"):
        admit_validated_capture(failed, dict(failed))


def test_conflicting_duplicate_profile_id_is_refused():
    candidate = entry()
    with pytest.raises(ValueError, match="profile_conflict"):
        admit_validated_capture(candidate, dict(candidate), existing=entry(generation="9"))
    with pytest.raises(ValueError, match="profile_conflict"):
        admit_validated_capture(candidate, dict(candidate), existing=entry(policy_digest="9" * 64))


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_required_field_must_be_present(field):
    candidate = entry()
    del candidate[field]
    with pytest.raises(ValueError, match=f"capture_field_missing:{field}"):
        validate_capture_entry(candidate)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("profile_id", "", "capture_field_invalid:profile_id"),
        ("profile_id", " daily ", "capture_field_invalid:profile_id"),
        ("result_id", 7, "capture_field_invalid:result_id"),
        ("source_tables", [], "capture_field_invalid:source_tables"),
        ("source_tables", ["raw_content", "raw_content"], "capture_field_invalid:source_tables"),
        ("source_tables", "raw_content", "capture_field_invalid:source_tables"),
        ("snapshot_tables", ["bad-name"], "capture_field_invalid:snapshot_tables"),
        ("markets", ["za", "us"], "capture_field_invalid:markets"),
        ("markets", [], "capture_field_invalid:markets"),
        (
            "observation_window_end",
            datetime(2026, 9, 12),
            "capture_field_invalid:observation_window_end",
        ),
        (
            "capture_available_at",
            "2026-09-12T03:00:00Z",
            "capture_field_invalid:capture_available_at",
        ),
        ("content_digest", "A" * 64, "capture_field_invalid:content_digest"),
        ("image_digest", "5" * 63, "capture_field_invalid:image_digest"),
        ("generation", 10, "capture_field_invalid:generation"),
        ("generation", "010", "capture_field_invalid:generation"),
        ("generation", "0", "capture_field_invalid:generation"),
        ("completion_state", "done", "capture_field_invalid:completion_state"),
        ("expires_at", datetime(2026, 9, 20), "capture_field_invalid:expires_at"),
    ],
)
def test_invalid_field_values_are_refused(field, value, code):
    with pytest.raises(ValueError, match=code):
        validate_capture_entry(entry(**{field: value}))


def test_unknown_fields_and_non_mappings_are_refused():
    with pytest.raises(ValueError, match="capture_field_unknown:status"):
        validate_capture_entry(entry(status="succeeded"))
    with pytest.raises(ValueError, match="capture_entry_invalid"):
        validate_capture_entry(["not", "a", "mapping"])


def test_capture_instants_must_be_ordered():
    with pytest.raises(ValueError, match="capture_instants_unordered"):
        validate_capture_entry(entry(collection_started_at=WINDOW_END - timedelta(minutes=1)))
    with pytest.raises(ValueError, match="capture_instants_unordered"):
        validate_capture_entry(entry(capture_available_at=WINDOW_END + timedelta(hours=1)))


def test_newest_ineligible_entries_do_not_hide_the_latest_valid_capture():
    eligible = shifted(0)
    entries = [
        eligible,
        shifted(1, content_digest="not-a-digest"),
        None,
        shifted(2, expires_at=NOW - timedelta(seconds=1)),
        shifted(3, completion_state="failed"),
        shifted(4, completion_state="partial"),
        shifted(5, scope="scope-other"),
        shifted(
            0,
            profile_id="daily-2026-09-12-late",
            result_id="result-late",
            capture_available_at=NOW + timedelta(hours=1),
        ),
    ]
    explained = explain_capture_selection(entries, now=NOW, scope="scope-42")
    assert explained["selected"] == validate_capture_entry(eligible)
    assert [item["reason"] for item in explained["diagnostics"]] == [
        "eligible",
        "capture_field_invalid:content_digest",
        "missing",
        "expired",
        "not_succeeded:failed",
        "not_succeeded:partial",
        "scope_mismatch",
        "not_yet_available",
    ]
    assert select_latest_capture(entries, now=NOW, scope="scope-42") == explained["selected"]
    assert select_latest_capture([], now=NOW, scope="scope-42") is None


def test_exact_older_request_binding_remains_selectable():
    older = shifted(-1)
    newer = shifted(0)
    assert (
        select_latest_capture([older, newer], now=NOW, scope="scope-42")["profile_id"]
        == (newer["profile_id"])
    )
    bound = select_latest_capture(
        [older, newer], now=NOW, scope="scope-42", profile_id=older["profile_id"]
    )
    assert bound["profile_id"] == older["profile_id"]
    assert bound["generation"] == "10"
    assert (
        select_latest_capture([older, newer], now=NOW, scope="scope-42", profile_id="never") is None
    )


def test_same_cutoff_with_different_source_policy_is_a_different_profile():
    first = entry(profile_id="daily-2026-09-12-policy3")
    second = entry(
        profile_id="daily-2026-09-12-policy9",
        policy_digest="9" * 64,
        result_id="result-2",
        capture_available_at=WINDOW_END + timedelta(hours=4),
    )
    explained = explain_capture_selection([first, second], now=NOW, scope="scope-42")
    assert [item["eligible"] for item in explained["diagnostics"]] == [True, True]
    assert explained["selected"]["profile_id"] == "daily-2026-09-12-policy9"
    repointed = entry(policy_digest="9" * 64, result_id="result-2")
    with pytest.raises(ValueError, match="profile_conflict"):
        explain_capture_selection([first, entry(), repointed], now=NOW, scope="scope-42")


def test_attempts_of_one_profile_share_their_binding():
    failed = entry(result_id="result-1", generation="9", completion_state="failed")
    succeeded = entry(result_id="result-2", generation="10")
    explained = explain_capture_selection([failed, succeeded], now=NOW, scope="scope-42")
    assert explained["selected"]["result_id"] == "result-2"
    assert explained["diagnostics"][0]["reason"] == "not_succeeded:failed"


def test_read_outcome_diagnostics_are_distinct_and_never_zero_rows():
    with pytest.raises(ValueError, match="capture_read_access_denied"):
        read_outcome_entries({"status": "access_denied"})
    with pytest.raises(ValueError, match="capture_read_metadata_unavailable"):
        read_outcome_entries({"status": "metadata_unavailable"})
    with pytest.raises(ValueError, match="capture_read_authority_invalid"):
        read_outcome_entries({"status": "invalid_authority"})
    with pytest.raises(ValueError, match="capture_read_entries_missing"):
        read_outcome_entries({"status": "ok"})
    with pytest.raises(ValueError, match="capture_read_invalid"):
        read_outcome_entries({"status": "denied"})
    assert read_outcome_entries({"status": "ok", "entries": [entry()]}) == [entry()]
    with pytest.raises(ValueError, match="capture_entries_missing"):
        select_latest_capture(None, now=NOW, scope="scope-42")


def test_selection_requires_aware_now_and_scope():
    with pytest.raises(ValueError, match="capture_field_invalid:now"):
        select_latest_capture([entry()], now=datetime(2026, 9, 13), scope="scope-42")
    with pytest.raises(ValueError, match="capture_field_invalid:scope"):
        select_latest_capture([entry()], now=NOW, scope="")


class RecordingWriter:
    """Create only registry writer fake: conditional creation with scripted acknowledgements."""

    def __init__(self, acks=("created",), *, store_on_ambiguous=True):
        self.objects = {}
        self.calls = []
        self.acks = list(acks)
        self.store_on_ambiguous = store_on_ambiguous

    def create(self, key, value):
        self.calls.append(("create", key))
        ack = self.acks.pop(0) if self.acks else "created"
        if key in self.objects:
            return {"status": "exists"}
        if ack == "created" or (ack == "ambiguous" and self.store_on_ambiguous):
            self.objects[key] = dict(value)
        return {"status": ack}

    def read(self, key):
        self.calls.append(("read", key))
        return self.objects.get(key)

    def replace(self, key, value):
        raise AssertionError("the create only seam must never replace an object")


def test_create_only_writer_admits_and_stores_the_normalized_entry():
    from src.analysis.open_intelligence.capture_registry import (
        capture_registry_key,
        create_capture_entry,
    )

    candidate = entry()
    writer = RecordingWriter()
    admitted = create_capture_entry(candidate, dict(candidate), writer=writer)
    key = capture_registry_key(admitted)
    assert key == "daily-2026-09-12-a/result-1"
    assert admitted == validate_capture_entry(candidate)
    assert writer.objects[key] == admitted
    assert writer.calls == [("create", key)]


def test_admission_refusal_happens_before_any_write():
    from src.analysis.open_intelligence.capture_registry import create_capture_entry

    candidate = entry()
    writer = RecordingWriter()
    with pytest.raises(ValueError, match="authority_mismatch"):
        create_capture_entry(candidate, entry(content_digest="f" * 64), writer=writer)
    with pytest.raises(ValueError, match="profile_conflict"):
        create_capture_entry(
            candidate, dict(candidate), writer=writer, existing=entry(generation="9")
        )
    assert writer.calls == []


def test_ambiguous_acknowledgement_reads_the_exact_object_back_before_any_retry():
    from src.analysis.open_intelligence.capture_registry import (
        capture_registry_key,
        create_capture_entry,
    )

    candidate = entry()
    writer = RecordingWriter(acks=("ambiguous",))
    admitted = create_capture_entry(candidate, dict(candidate), writer=writer)
    key = capture_registry_key(admitted)
    assert writer.calls == [("create", key), ("read", key)]
    assert writer.objects[key] == admitted


def test_ambiguous_acknowledgement_without_an_object_retries_once():
    from src.analysis.open_intelligence.capture_registry import (
        capture_registry_key,
        create_capture_entry,
    )

    candidate = entry()
    writer = RecordingWriter(acks=("ambiguous", "created"), store_on_ambiguous=False)
    admitted = create_capture_entry(candidate, dict(candidate), writer=writer)
    key = capture_registry_key(admitted)
    assert writer.calls == [("create", key), ("read", key), ("create", key)]
    assert writer.objects[key] == admitted


def test_unresolved_ambiguity_is_bounded_and_never_overwrites():
    from src.analysis.open_intelligence.capture_registry import (
        capture_registry_key,
        create_capture_entry,
    )

    candidate = entry()
    writer = RecordingWriter(acks=("ambiguous", "ambiguous"), store_on_ambiguous=False)
    with pytest.raises(ValueError, match="capture_write_unresolved"):
        create_capture_entry(candidate, dict(candidate), writer=writer)
    key = capture_registry_key(validate_capture_entry(candidate))
    assert writer.calls == [("create", key), ("read", key), ("create", key), ("read", key)]
    assert writer.objects == {}


def test_existing_object_is_idempotent_when_equal_and_a_conflict_when_different():
    from src.analysis.open_intelligence.capture_registry import (
        capture_registry_key,
        create_capture_entry,
    )

    candidate = entry()
    admitted = validate_capture_entry(candidate)
    key = capture_registry_key(admitted)
    writer = RecordingWriter()
    writer.objects[key] = dict(admitted)
    assert create_capture_entry(candidate, dict(candidate), writer=writer) == admitted
    assert writer.calls == [("create", key), ("read", key)]
    writer.objects[key] = validate_capture_entry(entry(generation="9"))
    with pytest.raises(ValueError, match="profile_conflict"):
        create_capture_entry(candidate, dict(candidate), writer=writer)
    assert writer.objects[key]["generation"] == "9"


def test_ambiguous_read_back_of_a_different_object_is_a_conflict_not_a_retry():
    from src.analysis.open_intelligence.capture_registry import (
        capture_registry_key,
        create_capture_entry,
    )

    candidate = entry()
    key = capture_registry_key(validate_capture_entry(candidate))
    writer = RecordingWriter(acks=("ambiguous",), store_on_ambiguous=False)
    writer.objects[key] = validate_capture_entry(entry(content_digest="f" * 64))
    # The fake reports exists for a present key, so script a bare ambiguous acknowledgement.
    writer.create = lambda k, v: (writer.calls.append(("create", k)), {"status": "ambiguous"})[1]
    with pytest.raises(ValueError, match="profile_conflict"):
        create_capture_entry(candidate, dict(candidate), writer=writer)
    assert writer.calls == [("create", key), ("read", key)]


@pytest.mark.parametrize(
    ("writer", "code"),
    [
        (object(), "capture_writer_invalid"),
        (
            type("NoRead", (), {"create": lambda self, k, v: {"status": "created"}})(),
            "capture_writer_invalid",
        ),
        (
            type(
                "BadAck",
                (),
                {"create": lambda self, k, v: "created", "read": lambda self, k: None},
            )(),
            "capture_write_ack_invalid",
        ),
        (
            type(
                "UnknownAck",
                (),
                {"create": lambda self, k, v: {"status": "updated"}, "read": lambda self, k: None},
            )(),
            "capture_write_ack_invalid",
        ),
    ],
)
def test_writer_and_acknowledgement_shapes_are_checked(writer, code):
    from src.analysis.open_intelligence.capture_registry import create_capture_entry

    candidate = entry()
    with pytest.raises(ValueError, match=code):
        create_capture_entry(candidate, dict(candidate), writer=writer)


def test_registry_key_is_profile_and_attempt_and_needs_a_validated_entry():
    from src.analysis.open_intelligence.capture_registry import capture_registry_key

    assert capture_registry_key(entry(result_id="result-9")) == "daily-2026-09-12-a/result-9"
    with pytest.raises(ValueError, match="capture_field_missing:profile_version"):
        capture_registry_key({"profile_id": "daily-a"})


def creation_record(lane, table=None, *, state="succeeded", estate=CAPTURE_ESTATE, **overrides):
    """One creation record shaped as the executing producer writes a succeeded one."""
    record = {
        "lane": lane,
        "destination": f"{estate}.{table or capture_table(lane)}",
        "job_id": f"oi_v3_snapshot_{'a' * 64}_{lane}",
        "native_job_digest": "b" * 64,
        "state": state,
    }
    record.update(overrides)
    return record


def coverage(**overrides):
    base = {
        "market_scope": ["ke", "ng", "za"],
        "creation_records": [
            creation_record("enriched_content"),
            creation_record("raw_content"),
        ],
    }
    base.update(overrides)
    return base


def test_full_native_coverage_admits_the_entry_lane_and_market_set():
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    assert admit_capture_coverage(entry(), coverage()) is None
    assert admit_capture_coverage(entry(), coverage(), declared_datasets=CAPTURE_DATASETS) is None


def test_the_destinations_the_executing_capture_plan_produces_are_admitted():
    """The guard admits what the only producer that can execute actually writes, and only that.

    The entry is a real staging source profile entry and the coverage carries the
    destinations of the real capture plan the executing producer takes its destination
    from. The profile's own ``snapshot_tables`` are the last segments of those very
    destinations, so the entry declares the tables the capture creates and the admission
    compares the records against both that declared set and the plan's whole names.
    """
    from pathlib import Path

    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage
    from src.analysis.open_intelligence.production_snapshot_tables import build_snapshot_plan
    from src.analysis.open_intelligence.staging_source_profile import (
        build_staging_source_profile,
    )

    cutoff = date(2030, 1, 2)
    window_end = datetime(2030, 1, 3, tzinfo=UTC)
    now = window_end + timedelta(hours=12)
    metadata = (
        Path(__file__).resolve().parents[1]
        / "fixtures/open_intelligence/v3_source_relation_metadata.json"
    ).read_bytes()
    plan = build_snapshot_plan(cutoff, now=now, source_metadata=metadata)
    run = {
        "receipt": {
            "contract_version": "collection_receipt_v1",
            "run_id": "collect-2030-01-02",
            "execution_id": "attempt:collect:1",
            "source_sha": "a" * 40,
            "image_uri": "region-docker.pkg.dev/project/repo/image@sha256:" + "b" * 64,
            "policy_sha256": "3" * 64,
            "profile_sha256": "6" * 64,
            "cutoff": cutoff.isoformat(),
            "market_states": {"za": "collected", "ng": "collected", "ke": "collected"},
            "raw_rows_persisted": 10,
            "enriched_rows_persisted": 5,
            "funded_close": None,
            "complete": True,
        },
        "collection_started_at": window_end + timedelta(minutes=30),
        "collection_completed_at": window_end + timedelta(hours=1),
    }
    profile = build_staging_source_profile(
        run,
        snapshot_as_of=now,
        scope="ogilvy_default",
        schema_digest="2" * 64,
        source_digest="4" * 64,
        now=now,
    )
    candidate = profile.capture_entry(
        operation_id="op-1",
        result_id="result-1",
        content_digest="1" * 64,
        image_digest="5" * 64,
        generation="7",
        completion_state="succeeded",
        capture_available_at=now + timedelta(minutes=1),
    )
    produced = [
        {
            "lane": item.lane,
            "destination": item.destination_table,
            "job_id": f"oi_v3_snapshot_{'c' * 64}_{item.lane}",
            "native_job_digest": "d" * 64,
            "state": "succeeded",
        }
        for item in plan.statements
    ]
    assert set(profile.snapshot_tables) == {
        item.destination_table.rsplit(".", 1)[-1] for item in plan.statements
    }
    coverage_of_the_plan = {"market_scope": ["ke", "ng", "za"], "creation_records": produced}
    assert (
        admit_capture_coverage(
            candidate, coverage_of_the_plan, declared_datasets=("trends_v2_staging",)
        )
        is None
    )
    # The dataset the plan writes into is the dataset the approved capture origin binds.
    with pytest.raises(ValueError, match="capture_dataset_undeclared"):
        admit_capture_coverage(
            candidate, coverage_of_the_plan, declared_datasets=("intelligence_42_sources_staging",)
        )


def test_a_capture_that_admitted_no_table_is_never_a_complete_capture():
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    with pytest.raises(ValueError, match="capture_tables_unadmitted"):
        admit_capture_coverage(entry(), coverage(creation_records=[]))


def test_a_missing_or_extra_lane_record_is_refused():
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    partial = [creation_record("raw_content")]
    with pytest.raises(ValueError, match="capture_tables_unadmitted"):
        admit_capture_coverage(entry(), coverage(creation_records=partial))
    extra = [*coverage()["creation_records"], creation_record("event_ledger")]
    with pytest.raises(ValueError, match="capture_tables_unadmitted"):
        admit_capture_coverage(entry(), coverage(creation_records=extra))


def test_a_record_for_another_lanes_table_is_refused():
    """A destination is admitted for its own lane, so a swapped pairing never admits."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    records = [
        creation_record("enriched_content", capture_table("raw_content")),
        creation_record("raw_content", capture_table("enriched_content")),
    ]
    with pytest.raises(ValueError, match="capture_snapshot_tables_unadmitted:enriched_content"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


def test_a_record_for_another_observation_days_table_is_refused():
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    records = [
        creation_record("enriched_content"),
        creation_record("raw_content", capture_table("raw_content", "20260910")),
    ]
    with pytest.raises(ValueError, match="capture_snapshot_tables_unadmitted:raw_content"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


def test_a_destination_in_another_estate_is_refused():
    """Two records in one project but two datasets, and a record in another project."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    split = [
        creation_record("enriched_content"),
        creation_record("raw_content", estate=CAPTURE_V2_ESTATE),
    ]
    with pytest.raises(ValueError, match="capture_estate_unadmitted"):
        admit_capture_coverage(entry(), coverage(creation_records=split))
    elsewhere = [
        creation_record("enriched_content", estate="another-project.trends_v2_staging"),
        creation_record("raw_content", estate="another-project.trends_v2_staging"),
    ]
    with pytest.raises(ValueError, match="capture_estate_unadmitted"):
        admit_capture_coverage(entry(), coverage(creation_records=elsewhere))


@pytest.mark.parametrize(
    "digest", [None, "", "not-a-digest", "B" * 64, "b" * 63, 7, "x" + "d" * 64]
)
def test_a_record_whose_native_job_was_never_observed_is_refused(digest):
    """The one field derived from an observed native job is read, not merely required."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    records = [
        creation_record("enriched_content"),
        creation_record("raw_content", native_job_digest=digest),
    ]
    with pytest.raises(ValueError, match="capture_native_job_unobserved:raw_content"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


@pytest.mark.parametrize(
    "job_id",
    [
        None,
        "",
        "job-1",
        "oi_v3_snapshot_x_enriched_content",
        7,
        # A job that merely mentions its lane, or carries no manifest digest where the
        # producer puts one, is not the job this record claims to have been created by.
        f"oi_v3_snapshot_{'a' * 64}_raw_content_superseded",
        f"oi_v3_snapshot_{'a' * 63}_raw_content",
        f"oi_v3_snapshot_{'A' * 64}_raw_content",
        f"x_oi_v3_snapshot_{'a' * 64}_raw_content",
        "oi_v3_snapshot__raw_content",
        # Another producer's prefix of the same length, and a digest slot that merely
        # ends in sixty four hexadecimal characters.
        f"xx_v3_snapshot_{'a' * 64}_raw_content",
        f"oi_v3_snapshot_x{'a' * 64}_raw_content",
    ],
)
def test_a_record_whose_job_does_not_name_its_own_lane_is_refused(job_id):
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    records = [
        creation_record("enriched_content"),
        creation_record("raw_content", job_id=job_id),
    ]
    with pytest.raises(ValueError, match="capture_job_unobserved:raw_content"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


@pytest.mark.parametrize("state", ["pending", "started", "failed", "unresolved"])
def test_an_unfinished_creation_record_is_refused_by_its_own_state(state):
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    records = [
        creation_record("enriched_content"),
        creation_record("raw_content", state=state),
    ]
    with pytest.raises(ValueError, match=f"capture_table_unadmitted:{state}"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


def test_a_repeated_lane_record_never_stands_for_the_whole_table_set():
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    records = [
        creation_record("enriched_content"),
        creation_record("raw_content"),
        creation_record("raw_content"),
    ]
    with pytest.raises(ValueError, match="capture_coverage_lane_repeated"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


@pytest.mark.parametrize(
    "markets",
    [["ke", "ng"], ["ke", "ng", "za", "ug"], ["ke", "ng", "ng", "za"], []],
)
def test_a_market_scope_that_is_not_the_entry_scope_is_refused(markets):
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    with pytest.raises(ValueError, match="capture_markets_unadmitted"):
        admit_capture_coverage(entry(), coverage(market_scope=markets))


def test_the_market_scope_compared_is_the_entrys_own_set_and_not_the_module_constant():
    """An entry of two markets is admitted by its own two market coverage.

    The production profile builder sets the same three markets every time, so a guard
    that compared the module constant instead of the entry would pass every test that
    carries a full market set. This entry carries two.
    """
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    two = entry(markets=["za", "ng"])
    assert admit_capture_coverage(two, coverage(market_scope=["ng", "za"])) is None
    with pytest.raises(ValueError, match="capture_markets_unadmitted"):
        admit_capture_coverage(two, coverage(market_scope=["ke", "ng", "za"]))


@pytest.mark.parametrize(
    "value",
    [
        {"market_scope": ["ke", "ng", "za"]},
        {**coverage(), "cutoff_date": "2026-09-12"},
        ["ke", "ng", "za"],
        None,
    ],
)
def test_a_coverage_record_of_another_shape_is_refused(value):
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    with pytest.raises(ValueError, match="capture_coverage_invalid"):
        admit_capture_coverage(entry(), value)


def test_a_coverage_recorded_before_the_coverage_fields_existed_is_refused_by_its_own_name():
    """A readback written before the coverage travelled carries neither field.

    It is refused as unrecorded coverage rather than read as a capture that reached
    nothing, so the attempt names the one thing missing from its own result record. A
    result that does carry the two fields and reports nothing in them is a different
    fault, a runner that answered no coverage today, and is named separately.
    """
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    with pytest.raises(ValueError, match="capture_coverage_unrecorded"):
        admit_capture_coverage(entry(), {})
    with pytest.raises(ValueError, match="capture_coverage_unreported"):
        admit_capture_coverage(entry(), {"market_scope": None, "creation_records": None})
    with pytest.raises(ValueError, match="capture_coverage_invalid"):
        admit_capture_coverage(entry(), coverage(creation_records=None))


@pytest.mark.parametrize(
    "records",
    [
        7,
        {"raw_content": "snapshot_raw"},
        [{"lane": "raw_content", "state": "succeeded"}],
        [{**creation_record("raw_content"), "state": 7}],
        [{**creation_record("raw_content"), "lane": 7}],
        [{**creation_record("raw_content"), "lane": ""}],
        [{**creation_record("raw_content"), "destination": 7}],
        [{**creation_record("raw_content"), "destination": "one_segment"}],
        [{**creation_record("raw_content"), "destination": "a.b.c.d"}],
        # Four well formed parts, and a badly formed dataset or table beside a well
        # formed project, are each an invalid coverage rather than a wrong table.
        [
            {
                **creation_record("raw_content"),
                "destination": f"{CAPTURE_ESTATE}.{capture_table('raw_content')}.extra",
            }
        ],
        [
            {
                **creation_record("raw_content"),
                "destination": (f"ogilvy-trends-v2.bad-dataset.{capture_table('raw_content')}"),
            }
        ],
        [
            {
                **creation_record("raw_content"),
                "destination": f"{CAPTURE_ESTATE}.bad-table",
            }
        ],
        [{**creation_record("raw_content"), "destination": "Bad-Project.d.t"}],
        [{**creation_record("raw_content"), "note": "extra"}],
    ],
)
def test_a_creation_record_of_another_shape_is_refused(records):
    """The record field set is exact, so an unread extra field is never carried along."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    with pytest.raises(ValueError, match="capture_coverage_invalid"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


@pytest.mark.parametrize("markets", ["za", {"za": 1}, 7])
def test_a_market_scope_that_is_not_a_sequence_is_refused(markets):
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    with pytest.raises(ValueError, match="capture_coverage_invalid"):
        admit_capture_coverage(entry(), coverage(market_scope=markets))


def test_coverage_admission_validates_the_entry_first():
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    candidate = entry()
    del candidate["snapshot_tables"]
    with pytest.raises(ValueError, match="capture_field_missing:snapshot_tables"):
        admit_capture_coverage(candidate, coverage())


def test_a_destination_the_executing_capture_never_writes_is_refused():
    """A well formed name of the right shape is not the table this capture creates.

    Every one of these is a three part name in the capture's own project, carrying this
    record's lane and this entry's observation day. None of them is the destination the
    plan the producer runs writes, so none of them is admitted.
    """
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    for destination, code in (
        # The v2 naming, which no capture execution that can run today produces.
        (
            f"{CAPTURE_V2_ESTATE}.staging_source_{CAPTURE_DAY}_raw_content",
            "capture_snapshot_tables",
        ),
        # An invented dataset of the right project, and the approvals dataset beside it.
        (f"ogilvy-trends-v2.some_other_dataset.{capture_table('raw_content')}", "capture_estate"),
        (
            f"ogilvy-trends-v2.trends_v2_staging_approvals.{capture_table('raw_content')}",
            "capture_estate",
        ),
        # The right estate, a table of this lane and day, under an invented prefix.
        (f"{CAPTURE_ESTATE}.whatever_junk_{CAPTURE_DAY}_raw_content", "capture_snapshot_tables"),
        (f"{CAPTURE_ESTATE}.staging_source_{CAPTURE_DAY}_raw_content", "capture_snapshot_tables"),
    ):
        records = [
            creation_record("enriched_content"),
            creation_record("raw_content", destination=destination),
        ]
        with pytest.raises(ValueError, match=code):
            admit_capture_coverage(entry(), coverage(creation_records=records))


def test_a_destination_that_stops_at_the_observation_day_is_refused():
    """One table with no lane segment can never stand for every lane at once."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    stem = f"open_intelligence_v3_source_{CAPTURE_DAY}"
    records = [
        creation_record("enriched_content", stem),
        creation_record("raw_content", stem),
    ]
    with pytest.raises(ValueError, match="capture_snapshot_tables_unadmitted:enriched_content"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


def test_the_entrys_own_declared_snapshot_tables_are_compared():
    """A record is admitted only for a table the entry itself declares it stands for.

    The entry names the snapshot tables its capture creates. A record naming a table of
    the right shape that the entry never declared is refused, so the declared set is read
    rather than carried unexamined.
    """
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    undeclared = entry(snapshot_tables=[capture_table("raw_content"), capture_table("seed_graph")])
    with pytest.raises(ValueError, match="capture_snapshot_tables_undeclared"):
        admit_capture_coverage(undeclared, coverage())


def test_a_dataset_the_capture_origin_does_not_declare_is_refused():
    """The estate the producer writes into must be one the approved origin binds."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    with pytest.raises(ValueError, match="capture_dataset_undeclared"):
        admit_capture_coverage(entry(), coverage(), declared_datasets=("trends_v2_dev",))
    with pytest.raises(ValueError, match="capture_dataset_undeclared"):
        admit_capture_coverage(entry(), coverage(), declared_datasets=())
    assert (
        admit_capture_coverage(
            entry(), coverage(), declared_datasets=("trends_v2_dev", "trends_v2_staging")
        )
        is None
    )


def test_a_destination_that_is_not_a_string_is_never_read_as_one():
    """An object that prints as a qualified name is still not a destination."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    class Printed:
        def __str__(self):
            return f"{CAPTURE_ESTATE}.{capture_table('raw_content')}"

    records = [
        creation_record("enriched_content"),
        creation_record("raw_content", destination=Printed()),
    ]
    with pytest.raises(ValueError, match="capture_coverage_invalid"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


def test_a_table_that_merely_ends_in_the_producers_name_is_refused():
    """The table is the producer's whole name for this lane, not a name ending in it."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    records = [
        creation_record("enriched_content"),
        creation_record("raw_content", f"x_{capture_table('raw_content')}"),
    ]
    with pytest.raises(ValueError, match="capture_snapshot_tables_unadmitted:raw_content"):
        admit_capture_coverage(entry(), coverage(creation_records=records))


def test_every_record_agreeing_on_one_wrong_dataset_is_still_refused():
    """Agreement among the records is not the test; the estate the producer writes is.

    Five records that all name one dataset agree with each other and with nothing else.
    The dataset is compared against the one the capture producer writes into, so a whole
    coverage in an invented dataset of the right project registers nothing.
    """
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    for dataset in ("some_other_dataset", "trends_v2_staging_approvals", "trends_v2_dev"):
        records = [
            creation_record("enriched_content", estate=f"ogilvy-trends-v2.{dataset}"),
            creation_record("raw_content", estate=f"ogilvy-trends-v2.{dataset}"),
        ]
        with pytest.raises(ValueError, match="capture_estate_unadmitted"):
            admit_capture_coverage(entry(), coverage(creation_records=records))


def test_an_entry_declaring_more_tables_than_the_capture_created_is_refused():
    """The declared set is compared whole, so a capture cannot cover part of it."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    wider = entry(
        snapshot_tables=[
            capture_table("raw_content"),
            capture_table("enriched_content"),
            capture_table("seed_graph"),
        ]
    )
    with pytest.raises(ValueError, match="capture_snapshot_tables_undeclared"):
        admit_capture_coverage(wider, coverage())


def test_a_job_identity_that_is_not_a_string_is_never_read_as_one():
    """An object that prints as a job identity is still not one."""
    from src.analysis.open_intelligence.capture_registry import admit_capture_coverage

    class Printed:
        def __str__(self):
            return f"oi_v3_snapshot_{'a' * 64}_raw_content"

    records = [
        creation_record("enriched_content"),
        creation_record("raw_content", job_id=Printed()),
    ]
    with pytest.raises(ValueError, match="capture_job_unobserved:raw_content"):
        admit_capture_coverage(entry(), coverage(creation_records=records))
