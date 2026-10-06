"""A comparison question answered from the one pinned capture, behind a release switch."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import general_question_snapshot as snapshot

from tests.unit import test_general_question_context_snapshot as context_fixture
from tests.unit import test_general_question_snapshot as released_fixture

SWITCH = "GENERAL_QUESTION_HISTORY_SOURCES"
SOURCE_AS_OF = "2026-09-08T00:00:00+00:00"
PIN = {
    "profile_id": "protected_context_20260907_v1",
    "cutoff_date": "2026-09-07",
    "manifest_sha256": "a" * 64,
    "consumption_id": "exc_" + "b" * 64,
    "result_id": "exr_" + "c" * 64,
    "result_digest": "d" * 64,
}
BINDING = {
    "profile_id": PIN["profile_id"],
    **context_fixture.SYNTHETIC_BINDING,
    "source_as_of": SOURCE_AS_OF,
}
HISTORY_REQUIREMENT = {
    "requirement_id": "prior_week",
    "question": "How does the later week compare with the week before?",
    "kind": "history",
    "mandatory": True,
    "search_terms": [],
}
# The snapshot digest the unchanged builder seals for the comparison fixture below with
# the switch off. It was read from the build on 39d7913 before any of this change landed,
# so an off switch is held to the bytes the core wrote, not to this change's own output.
OFF_SNAPSHOT_DIGEST = "a80cd971fa85b1405c44a883f6242bdea0309752265d1527cce2f2b1765f96dc"


def comparison():
    from src.analysis.open_intelligence import general_question_window_comparison

    return general_question_window_comparison


def receipt(index, published_at, *, collected_at=None):
    return {
        "receipt_id": f"gqctx_{index:02d}",
        "citation_label": f"R{index + 1}",
        "kind": "content",
        "snapshot_id": "gqs_" + "f" * 64,
        "market": "za",
        "source_label": "publisher",
        "source_family": "news",
        "platform": "web",
        "author": None,
        "url": None,
        "source_row_id": f"row-{index:02d}",
        "published_at": published_at,
        "collected_at": collected_at or published_at,
        "excerpt": f"Synthetic observation {index}",
        "reading_ids": [],
        "limitations": [],
        "content_digest": f"{index:064x}",
    }


def spread(earlier, later):
    """Receipts in each half of the fixture window, 2026-08-23 to 2026-09-05."""
    rows = []
    for offset in range(earlier):
        rows.append(receipt(len(rows), f"2026-08-{24 + offset % 5:02d}T10:00:00+00:00"))
    for offset in range(later):
        rows.append(receipt(len(rows), f"2026-09-{1 + offset % 4:02d}T10:00:00+00:00"))
    return rows


def comparison_plan(plan, intent="comparison", window=None):
    plan["intent"] = intent
    plan["requirements"] = [*plan["requirements"], copy.deepcopy(HISTORY_REQUIREMENT)]
    if window is not None:
        plan["window"] = window
    return plan


def uncapped_selection(kept=None):
    """Selection numbers of a read where neither the selector nor any LIMIT bound."""
    kept = 12 if kept is None else kept
    return {
        "selection": {"kept": kept, "eligible": kept},
        "lanes": {
            key: {"kept": 1, "matching": 1, "capped": False}
            for key in ("candidate", "enriched_index", "enriched", "raw_index", "raw")
        },
    }


def context_build(
    monkeypatch,
    receipts,
    *,
    intent="comparison",
    window=None,
    history=True,
    caps=None,
    pins=(PIN,),
):
    """Build a context snapshot over a stubbed admission, recording every query ordinal."""
    from src.analysis.open_intelligence import general_question_context_admission as admission
    from src.analysis.open_intelligence import general_question_queries as query_ledger
    from src.analysis.open_intelligence import general_question_query_execution as execution
    from src.analysis.open_intelligence import production_snapshot_storage as storage_boundary
    from src.analysis.open_intelligence import protected_context_registry

    module, store, plan = released_fixture.fixture(monkeypatch)
    if history:
        comparison_plan(plan, intent, window)
    monkeypatch.setattr(admission, "_PROTECTED_CONTEXT_PROFILES", pins)
    monkeypatch.setattr(protected_context_registry, "_PROTECTED_CONTEXT_PROFILES", pins)
    records, reserved = {}, {}

    class Ledger:
        def __init__(self, _store):
            pass

        def read_query(self, _request_id, *, scope, ordinal):
            return records[ordinal]

    monkeypatch.setattr(query_ledger, "GeneralQuestionQueries", Ledger)
    monkeypatch.setattr(storage_boundary, "SourceCaptureObjects", lambda bucket: object())
    monkeypatch.setattr(
        "google.cloud.bigquery.Client", lambda **kwargs: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(
        "google.cloud.storage.Client",
        lambda **kwargs: SimpleNamespace(bucket=lambda name: object(), close=lambda: None),
    )

    def result_query(*args, ordinal, maximum_bytes_billed, **kwargs):
        approval = SimpleNamespace(manifest_sha256=PIN["manifest_sha256"])
        rows = [{"authority": True}]
        receipt_value = {"result_digest": module.canonical_digest(rows)}
        records[ordinal] = {"reservation": {"ordinal": ordinal}, "receipt": receipt_value}
        reserved[ordinal] = maximum_bytes_billed
        return {
            "application_status": "accepted",
            "material": {"approval": approval, "result": {"result_id": PIN["result_id"]}},
            "rows": rows,
            "receipt": receipt_value,
        }

    monkeypatch.setattr(execution, "execute_context_result_query", result_query)
    loaded = SimpleNamespace(source_binding=copy.deepcopy(BINDING))

    def load(*args, result_reader, approval_reader, **kwargs):
        result_reader(PIN["consumption_id"])
        approval_reader(PIN["manifest_sha256"])
        return loaded

    monkeypatch.setattr(admission, "load_protected_context_source", load)

    def data_result(template, ordinal, count, maximum_bytes_billed):
        rows = [{"template": template}]
        receipt_value = {"result_digest": module.canonical_digest(rows)}
        records[ordinal] = {"reservation": {"ordinal": ordinal}, "receipt": receipt_value}
        reserved[ordinal] = maximum_bytes_billed
        return {
            "application_status": "accepted",
            "rows": rows,
            "receipt": receipt_value,
            "material": {"candidate_count": count, "records": [{"sample_row_ids": ["synthetic"]}]},
            "prepared_query": SimpleNamespace(template_id=template),
        }

    monkeypatch.setattr(
        execution,
        "execute_context_candidate_query",
        lambda *args, ordinal, maximum_bytes_billed, **kwargs: data_result(
            "candidate", ordinal, 1, maximum_bytes_billed
        ),
    )
    monkeypatch.setattr(
        execution,
        "execute_context_partition_query",
        lambda *args, ordinal, maximum_bytes_billed, **kwargs: data_result(
            "partitions", ordinal, 0, maximum_bytes_billed
        ),
    )
    monkeypatch.setattr(
        execution,
        "execute_context_evidence_query",
        lambda *args, lane, ordinal, maximum_bytes_billed, **kwargs: data_result(
            lane, ordinal, 1, maximum_bytes_billed
        ),
    )
    monkeypatch.setattr(
        execution,
        "execute_context_index_query",
        lambda *args, lane, ordinal, maximum_bytes_billed, **kwargs: data_result(
            lane + "_index", ordinal, 1, maximum_bytes_billed
        ),
    )
    stubbed = context_fixture.synthetic_admission(
        receipts=copy.deepcopy(receipts), fulfilled_requirement_ids=["content"]
    )
    monkeypatch.setattr(admission, "admit_protected_context", lambda *args, **kwargs: stubbed)
    selection = uncapped_selection(len(receipts)) if caps is None else caps

    def selection_caps(*args, **kwargs):
        if selection == "forbid":
            raise AssertionError("selection caps read with the switch off")
        return copy.deepcopy(selection)

    monkeypatch.setattr(admission, "context_selection_caps", selection_caps)
    result = released_fixture.build(module, store, plan)
    return result, plan, records, reserved


def off_values():
    return [None, "", "true", "True", "Enabled", "ENABLED", "enabled ", " enabled", "1", "on"]


def set_switch(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(SWITCH, raising=False)
    else:
        monkeypatch.setenv(SWITCH, value)


# Switch


@pytest.mark.parametrize("value", off_values())
def test_only_the_exact_value_enabled_turns_the_switch_on(monkeypatch, value):
    set_switch(monkeypatch, value)
    assert comparison().HISTORY_SOURCES_SWITCH == SWITCH
    assert comparison().history_sources_enabled() is False
    monkeypatch.setenv(SWITCH, "enabled")
    assert comparison().history_sources_enabled() is True


@pytest.mark.parametrize("value", off_values())
def test_switch_off_seals_the_bytes_the_core_sealed(monkeypatch, value):
    set_switch(monkeypatch, value)
    result, _plan, records, _caps = context_build(monkeypatch, spread(6, 6))
    assert result["status"] == "admitted"
    stored = result["snapshot"]
    assert "window_comparison" not in stored["provenance"]
    assert stored["missing_work"] == ["prior_week"]
    assert stored["fulfilled_requirement_ids"] == ["content"]
    assert stored["readings"] == []
    assert stored["snapshot_digest"] == OFF_SNAPSHOT_DIGEST
    assert set(records) == {1, 2, 3, 4, 5, 6, 7}


# Compared


def test_switch_on_compares_both_halves_with_unit_comparator_counts_and_receipts(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    receipts = spread(6, 7)
    result, _plan, _records, _caps = context_build(monkeypatch, receipts)
    assert result["status"] == "admitted"
    stored = result["snapshot"]
    record = stored["provenance"]["window_comparison"]
    earlier_ids = [row["receipt_id"] for row in receipts[:6]]
    later_ids = [row["receipt_id"] for row in receipts[6:]]
    assert record == {
        "contract_version": "general_question_window_comparison_v1",
        "state": "compared",
        "reason": None,
        "requirement_ids": ["prior_week"],
        "unit": "admitted_evidence_items",
        "comparator": "earlier_window",
        "method": "window_comparison_receipt_count",
        "minimum_per_window": 5,
        "split": "equal_halves_start_inclusive_end_exclusive",
        "capture": {
            "profile_id": PIN["profile_id"],
            "cutoff_date": "2026-09-07",
            "source_as_of": SOURCE_AS_OF,
        },
        "windows": {
            "earlier": {
                "start": "2026-08-23T00:00:00Z",
                "end": "2026-08-30T00:00:00Z",
                "count": 6,
                "receipt_ids": earlier_ids,
            },
            "later": {
                "start": "2026-08-30T00:00:00Z",
                "end": "2026-09-06T00:00:00Z",
                "count": 7,
                "receipt_ids": later_ids,
            },
        },
        "excluded": [],
        "selection": {
            "capped": False,
            "selected_items": 13,
            "eligible_items": 13,
            "capped_lanes": [],
            "lanes": uncapped_selection()["lanes"],
        },
    }
    readings = {row["window"]["start"]: row for row in stored["readings"]}
    earlier, later = readings["2026-08-23"], readings["2026-08-30"]
    assert earlier["window"] == {"start": "2026-08-23", "end": "2026-08-29", "closed": True}
    assert later["window"] == {"start": "2026-08-30", "end": "2026-09-05", "closed": True}
    for reading, ids in ((earlier, earlier_ids), (later, later_ids)):
        assert reading["unit"] == "admitted_evidence_items"
        assert reading["method"] == "window_comparison_receipt_count"
        assert reading["value"] == len(ids)
        assert reading["source_receipt_ids"] == ids
        assert reading["denominator"] is None
    assert "the named comparator" in earlier["limitations"][0]
    bounds = "2026-08-23T00:00:00Z inclusive to 2026-08-30T00:00:00Z exclusive"
    assert bounds in earlier["limitations"][0]
    assert "compared against the earlier window" in later["limitations"][0]
    by_id = {row["receipt_id"]: row for row in stored["receipts"]}
    assert all(by_id[rid]["reading_ids"] == [earlier["reading_id"]] for rid in earlier_ids)
    assert all(by_id[rid]["reading_ids"] == [later["reading_id"]] for rid in later_ids)
    assert stored["fulfilled_requirement_ids"] == ["content", "prior_week"]
    assert stored["missing_work"] == []
    assert any(
        line.startswith("Window comparison in admitted evidence items: the later window")
        for line in stored["limitations"]
    )
    # The admission the snapshot carries is the admission the read produced, untouched.
    assert [row["reading_ids"] for row in stored["provenance"]["admission"]["receipts"]] == [
        [] for _ in receipts
    ]
    assert stored["snapshot_digest"] == snapshot.canonical_digest(
        {key: value for key, value in stored.items() if key != "snapshot_digest"}
    )


def test_switch_on_reserves_no_additional_query_ordinal_or_byte(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    _off, _plan, off_records, off_caps = context_build(monkeypatch, spread(6, 6))
    monkeypatch.setenv(SWITCH, "enabled")
    on, _plan, on_records, on_caps = context_build(monkeypatch, spread(6, 6))
    assert on["snapshot"]["provenance"]["window_comparison"]["state"] == "compared"
    assert set(on_records) == set(off_records) == {1, 2, 3, 4, 5, 6, 7}
    assert on_caps == off_caps
    assert on["snapshot"]["provenance"]["limits"] == _off["snapshot"]["provenance"]["limits"]


# Refusals


def test_insufficient_history_refuses_without_answering_the_requirement(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    result, _plan, _records, _caps = context_build(monkeypatch, spread(4, 9))
    stored = result["snapshot"]
    record = stored["provenance"]["window_comparison"]
    assert record["state"] == "refused"
    assert record["reason"] == "insufficient_history"
    assert record["windows"]["earlier"]["count"] == 4
    assert record["windows"]["later"]["count"] == 9
    assert stored["readings"] == []
    assert stored["fulfilled_requirement_ids"] == ["content"]
    assert stored["missing_work"] == ["prior_week", "insufficient_history"]
    assert (
        "Window comparison refused (insufficient_history): the earlier window holds 4 and "
        "the later window 9 admitted evidence items; each needs at least 5."
    ) in stored["limitations"]


@pytest.mark.parametrize("earlier,later,state", [(5, 5, "compared"), (4, 5, "refused")])
def test_the_minimum_is_five_admitted_items_in_each_half(monkeypatch, earlier, later, state):
    monkeypatch.setenv(SWITCH, "enabled")
    result, _plan, _records, _caps = context_build(monkeypatch, spread(earlier, later))
    assert result["snapshot"]["provenance"]["window_comparison"]["state"] == state


def test_a_source_dated_after_the_capture_source_as_of_is_refused_and_never_counted(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    receipts = spread(5, 5)
    late = receipt(20, "2026-09-05T10:00:00+00:00", collected_at="2026-09-08T00:00:01+00:00")
    result, _plan, _records, _caps = context_build(monkeypatch, [*receipts, late])
    stored = result["snapshot"]
    record = stored["provenance"]["window_comparison"]
    assert record["state"] == "compared"
    assert record["windows"]["later"]["count"] == 5
    assert late["receipt_id"] not in record["windows"]["later"]["receipt_ids"]
    assert record["excluded"] == [
        {"receipt_id": late["receipt_id"], "reason": "source_after_capture"}
    ]
    later = next(row for row in stored["readings"] if row["window"]["start"] == "2026-08-30")
    assert later["value"] == 5
    assert late["receipt_id"] not in later["source_receipt_ids"]
    assert (
        "Window comparison excluded 1 admitted items dated after the capture source as of "
        f"{SOURCE_AS_OF}; they are not counted."
    ) in stored["limitations"]


def test_a_source_exactly_at_the_capture_source_as_of_is_not_after_it():
    record = comparison().build_window_comparison(
        comparison_plan(
            {
                "window": {"start": "2026-08-25", "end": "2026-09-07", "closed": True},
                "requirements": [],
            }
        ),
        [receipt(0, "2026-09-07T12:00:00+00:00", collected_at=SOURCE_AS_OF)],
        BINDING,
        uncapped_selection(),
    )
    assert record["excluded"] == []
    assert record["windows"]["later"]["receipt_ids"] == ["gqctx_00"]


def test_a_window_past_the_pinned_cutoff_is_refused_whole_and_never_zero():
    plan = comparison_plan(
        {
            "window": {"start": "2026-08-26", "end": "2026-09-08", "closed": True},
            "requirements": [],
        }
    )
    record = comparison().build_window_comparison(plan, spread(6, 6), BINDING, uncapped_selection())
    assert record["state"] == "refused"
    assert record["reason"] == "historical_window_uncovered"
    assert record["windows"] is None
    assert record["capture"]["cutoff_date"] == "2026-09-07"
    value = {
        "snapshot_id": "gqs_x",
        "receipts": [],
        "readings": [],
        "limitations": [],
        "missing_work": ["prior_week"],
        "fulfilled_requirement_ids": ["content"],
    }
    comparison().apply_window_comparison(value, record, plan)
    assert value["readings"] == []
    assert value["missing_work"] == ["prior_week", "historical_window_uncovered"]
    assert value["limitations"] == [
        "Window comparison refused (historical_window_uncovered): the window runs past "
        "2026-09-07, the pinned capture cutoff. Nothing after the cutoff was read, and it "
        "is not counted as zero."
    ]


@pytest.mark.parametrize("value", [None, "true", "Enabled"])
def test_an_uncovered_comparison_read_with_the_switch_off_refuses_as_the_core_does(
    monkeypatch, value
):
    set_switch(monkeypatch, value)
    window = {"start": "2026-08-26", "end": "2026-09-08", "closed": True}
    result, _plan, records, _caps = context_build(monkeypatch, spread(6, 6), window=window)
    assert result == snapshot._failure("coverage_incomplete", "bridge_source_uncovered")
    assert records == {}


def test_an_uncovered_comparison_read_names_the_pinned_cutoff(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    window = {"start": "2026-08-26", "end": "2026-09-08", "closed": True}
    result, _plan, records, _caps = context_build(monkeypatch, spread(6, 6), window=window)
    assert result == snapshot._failure(
        "coverage_incomplete",
        "bridge_source_uncovered",
        "historical_window_uncovered",
        "Historical window uncovered: the comparison window 2026-08-26 to 2026-09-08 runs "
        "past 2026-09-07, the latest pinned capture cutoff. Nothing after the cutoff was "
        "read, and it is not counted as zero.",
    )
    assert records == {}


@pytest.mark.parametrize("intent", ["history", "explanation", "discovery"])
def test_analogue_history_and_other_intents_are_untouched_with_the_switch_on(monkeypatch, intent):
    monkeypatch.delenv(SWITCH, raising=False)
    off, _plan, _records, _caps = context_build(monkeypatch, spread(6, 6), intent=intent)
    monkeypatch.setenv(SWITCH, "enabled")
    on, _plan, _records, _caps = context_build(monkeypatch, spread(6, 6), intent=intent)
    assert "window_comparison" not in on["snapshot"]["provenance"]
    assert on == off


def test_a_comparison_without_a_history_requirement_is_untouched(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    off, _plan, _records, _caps = context_build(monkeypatch, spread(6, 6), history=False)
    monkeypatch.setenv(SWITCH, "enabled")
    on, _plan, _records, _caps = context_build(monkeypatch, spread(6, 6), history=False)
    assert on == off


def test_a_one_day_comparison_has_no_earlier_period_and_stays_unavailable(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    day = {"start": "2026-09-01", "end": "2026-09-01", "closed": True}
    rows = [receipt(index, "2026-09-01T10:00:00+00:00") for index in range(12)]
    result, _plan, _records, _caps = context_build(monkeypatch, rows, window=day)
    stored = result["snapshot"]
    record = stored["provenance"]["window_comparison"]
    assert record["state"] == "refused"
    assert record["reason"] == "historical_sources_unavailable"
    assert record["windows"] is None
    assert stored["missing_work"] == ["prior_week", "historical_sources_unavailable"]


def test_a_challenge_history_requirement_is_never_answered_by_a_count():
    plan = {
        "intent": "comparison",
        "requirements": [
            {**HISTORY_REQUIREMENT, "evidence_purpose": "challenge"},
            {
                **HISTORY_REQUIREMENT,
                "requirement_id": "support_week",
                "evidence_purpose": "support",
            },
        ],
    }
    assert comparison().comparison_requirement_ids(plan) == ["support_week"]
    plan["requirements"].pop()
    assert comparison().comparison_requirement_ids(plan) == []


def test_the_legacy_history_resolver_stays_unavailable_with_the_switch_on(monkeypatch):
    from src.analysis.open_intelligence import general_question_execution as execution

    monkeypatch.setenv(SWITCH, "enabled")
    plan = comparison_plan(
        {
            "window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True},
            "requirements": [],
        }
    )
    answers = execution._history_resolver({}, {}, plan)(plan)
    assert set(answers) == {"prior_week"}
    assert answers["prior_week"].reason == "historical_sources_unavailable"
    assert execution.RETAINED_HISTORY_SOURCE_PROVIDER is None


# Boundaries


def split(start, end):
    return comparison().split_window({"start": start, "end": end, "closed": True})


def test_an_even_day_count_splits_at_a_day_boundary():
    low, middle, high = split("2026-08-23", "2026-09-05")
    assert low == datetime(2026, 8, 23, tzinfo=UTC)
    assert middle == datetime(2026, 8, 30, tzinfo=UTC)
    assert high == datetime(2026, 9, 6, tzinfo=UTC)
    assert middle - low == high - middle


def test_an_odd_day_count_splits_at_noon_of_the_middle_day():
    low, middle, high = split("2026-09-01", "2026-09-07")
    assert middle == datetime(2026, 9, 4, 12, tzinfo=UTC)
    assert middle - low == high - middle == timedelta(days=3, hours=12)


def test_a_two_day_window_is_the_shortest_that_splits():
    assert split("2026-09-01", "2026-09-01") is None
    assert split("2026-09-01", "2026-09-02")[1] == datetime(2026, 9, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    "instant,role",
    [
        ("2026-08-23T00:00:00+00:00", "earlier"),
        ("2026-08-29T23:59:59.999999+00:00", "earlier"),
        ("2026-08-30T00:00:00+00:00", "later"),
        ("2026-08-30T02:00:00+02:00", "later"),
        ("2026-08-30T01:59:59+02:00", "earlier"),
        ("2026-09-05T23:59:59.999999+00:00", "later"),
        ("2026-09-06T00:00:00+00:00", None),
        ("2026-08-22T23:59:59.999999+00:00", None),
    ],
)
def test_boundary_instants_fall_into_exactly_one_half(instant, role):
    plan = comparison_plan(
        {
            "window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True},
            "requirements": [],
        }
    )
    rows = [receipt(0, instant, collected_at="2026-09-06T00:00:00+00:00")]
    record = comparison().build_window_comparison(plan, rows, BINDING, uncapped_selection())
    placed = {
        name: half["receipt_ids"] for name, half in record["windows"].items() if half["receipt_ids"]
    }
    if role is None:
        assert placed == {}
        assert record["excluded"] == [{"receipt_id": "gqctx_00", "reason": "outside_window"}]
    else:
        assert placed == {role: ["gqctx_00"]}


def test_publication_time_places_a_receipt_before_collection_time():
    plan = comparison_plan(
        {
            "window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True},
            "requirements": [],
        }
    )
    rows = [
        receipt(0, "2026-08-29T10:00:00+00:00", collected_at="2026-09-02T10:00:00+00:00"),
        {**receipt(1, None, collected_at="2026-09-02T10:00:00+00:00"), "published_at": None},
    ]
    record = comparison().build_window_comparison(plan, rows, BINDING, uncapped_selection())
    assert record["windows"]["earlier"]["receipt_ids"] == ["gqctx_00"]
    assert record["windows"]["later"]["receipt_ids"] == ["gqctx_01"]


def test_an_odd_day_comparison_reads_the_middle_day_in_both_date_windows():
    window = {"start": "2026-09-01", "end": "2026-09-07", "closed": True}
    plan = comparison_plan({"window": window, "requirements": []})
    rows = [
        *(receipt(index, "2026-09-02T10:00:00+00:00") for index in range(5)),
        receipt(5, "2026-09-04T11:59:59+00:00"),
        receipt(6, "2026-09-04T12:00:00+00:00"),
        *(receipt(index, "2026-09-06T10:00:00+00:00") for index in range(7, 11)),
    ]
    record = comparison().build_window_comparison(plan, rows, BINDING, uncapped_selection())
    assert record["windows"]["earlier"]["end"] == "2026-09-04T12:00:00Z"
    assert record["windows"]["earlier"]["receipt_ids"][-1] == "gqctx_05"
    assert record["windows"]["later"]["receipt_ids"][0] == "gqctx_06"
    value = {
        "snapshot_id": "gqs_x",
        "receipts": copy.deepcopy(rows),
        "readings": [],
        "limitations": [],
        "missing_work": ["prior_week"],
        "fulfilled_requirement_ids": ["content"],
    }
    comparison().apply_window_comparison(value, record, plan)
    windows = [row["window"] for row in value["readings"]]
    assert windows == [
        {"start": "2026-09-01", "end": "2026-09-04", "closed": True},
        {"start": "2026-09-04", "end": "2026-09-07", "closed": True},
    ]


# Stored snapshots


def test_a_stored_comparison_is_checked_against_its_own_receipts_not_the_switch(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    result, plan, _records, _caps = context_build(monkeypatch, spread(6, 6))
    stored = result["snapshot"]
    provenance = stored["provenance"]
    record = provenance["window_comparison"]
    monkeypatch.delenv(SWITCH)
    checked = comparison().validate_stored_window_comparison(
        record,
        plan,
        provenance["admission"]["receipts"],
        provenance["source_binding"],
        uncapped_selection(12),
    )
    assert checked == record
    context = {
        "request": {
            "request_id": stored["request_id"],
            "request_digest": stored["request_digest"],
            "as_of": stored["as_of"],
        },
        "intake": {"intake_digest": stored["intake_digest"]},
        "admission": {
            "policy_digest": stored["policy_digest"],
            "deployment_digest": stored["deployment_digest"],
        },
    }
    assert (
        snapshot._project_context_snapshot(context, plan, provenance["admission"], provenance)
        == stored
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda record: record["windows"]["later"].update(count=99),
        lambda record: record["windows"]["later"]["receipt_ids"].pop(),
        lambda record: record.update(state="compared", reason=None, minimum_per_window=1),
        lambda record: record.update(unit="distinct_sources"),
        lambda record: record.update(extra=True),
        lambda record: record["capture"].update(cutoff_date="2026-09-30"),
    ],
)
def test_a_tampered_stored_comparison_is_refused(monkeypatch, change):
    monkeypatch.setenv(SWITCH, "enabled")
    result, plan, _records, _caps = context_build(monkeypatch, spread(6, 6))
    provenance = result["snapshot"]["provenance"]
    record = copy.deepcopy(provenance["window_comparison"])
    change(record)
    with pytest.raises(ValueError, match="window_comparison_invalid"):
        comparison().validate_stored_window_comparison(
            record,
            plan,
            provenance["admission"]["receipts"],
            provenance["source_binding"],
            uncapped_selection(12),
        )


def test_a_comparison_record_on_a_plan_that_asks_for_none_is_refused():
    plan = {
        "intent": "history",
        "window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True},
        "requirements": [HISTORY_REQUIREMENT],
    }
    with pytest.raises(ValueError, match="window_comparison_invalid"):
        comparison().build_window_comparison(plan, spread(6, 6), BINDING, uncapped_selection())


# Published result


async def uncovered_comparison_result(monkeypatch):
    from tests.unit import test_general_question_execution as execution_fixture
    from tests.unit import test_general_question_runtime as runtime_fixture
    from tests.unit import test_general_question_store as store_fixture

    now = datetime(2026, 9, 9, 17, 19, tzinfo=UTC)
    monkeypatch.setattr(store_fixture, "NOW", now)
    values = runtime_fixture.setup_runtime()
    draft = runtime_fixture.plan_draft()
    draft["intent"] = "comparison"
    draft["window"] = {"start": "2026-08-26", "end": "2026-09-08", "closed": True}
    draft["requirements"] = [*draft["requirements"], copy.deepcopy(HISTORY_REQUIREMENT)]
    runtime_fixture.intercept(monkeypatch, draft)
    result = await execution_fixture.execute(values, now=now)
    store = values[0]
    _, control = store._control()
    assert control["requests"][result["request_id"]]["execution"].get("queries", {}) == {}
    record = store._objects.read(
        f"requests/{result['request_id']}/results/{result['result_digest']}.json"
    )
    return record.value["response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "true", "Enabled"])
async def test_an_uncovered_comparison_publishes_what_the_core_published_with_the_switch_off(
    monkeypatch, value
):
    set_switch(monkeypatch, value)
    response = await uncovered_comparison_result(monkeypatch)
    assert response["reason"] == "retrieval_incomplete"
    assert response["intelligence"]["missing_work"] == [
        "retrieval_incomplete",
        "coverage_incomplete",
    ]


@pytest.mark.asyncio
async def test_an_uncovered_comparison_publishes_its_code_and_the_pinned_cutoff(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    response = await uncovered_comparison_result(monkeypatch)
    assert response["reason"] == "retrieval_incomplete"
    assert response["intelligence"]["missing_work"] == [
        "retrieval_incomplete",
        "coverage_incomplete",
        "historical_window_uncovered",
        "Historical window uncovered: the comparison window 2026-08-26 to 2026-09-08 runs "
        "past 2026-09-07, the latest pinned capture cutoff. Nothing after the cutoff was "
        "read, and it is not counted as zero.",
    ]


# Answer


def compared_answer_values():
    from src.analysis.open_intelligence.general_question_plan import validate_question_plan

    from tests.unit import test_general_question_answer as answer_fixture
    from tests.unit import test_general_question_plan as planning

    request, intake, _plan, _stored, usage, _output, policy = answer_fixture.fixture()
    draft = planning.draft()
    draft["intent"] = "comparison"
    draft["requirements"] = [*draft["requirements"], copy.deepcopy(HISTORY_REQUIREMENT)]
    plan = validate_question_plan(draft, request=request, intake=intake)
    rows = spread(5, 6)
    for row in rows:
        row["snapshot_id"] = "snapshot_window"
        row["excerpt"] = "Neighbours share tools at the repair café."
    value = {
        "contract_version": "general_question_snapshot_v1",
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "intake_digest": intake["intake_digest"],
        "plan_digest": plan["plan_digest"],
        "policy_digest": request["policy_digest"],
        "deployment_digest": "b" * 64,
        "snapshot_id": "snapshot_window",
        "as_of": request["as_of"],
        "window": plan["window"],
        "receipts": rows,
        "readings": [],
        "limitations": [],
        "missing_work": ["prior_week"],
        "fulfilled_requirement_ids": ["repair observations"],
    }
    record = comparison().build_window_comparison(plan, rows, BINDING, uncapped_selection())
    assert record["state"] == "compared"
    comparison().apply_window_comparison(value, record, plan)
    answer_fixture.rehash(value)
    return request, intake, plan, value, usage, policy


def reading_claim(claim_id, reading):
    return {
        "claim_id": claim_id,
        "kind": "observation",
        "segments": [{"kind": "reading", "reading_id": reading["reading_id"]}],
        "receipt_ids": list(reading["source_receipt_ids"]),
        "reading_ids": [reading["reading_id"]],
        "parent_claim_ids": [],
        "support_state": "source_record",
        "limitations": [],
        "falsifier": None,
    }


def test_a_compared_snapshot_answers_with_both_windows_named(monkeypatch):
    from tests.unit import test_general_question_answer as answer_fixture

    request, intake, plan, stored, usage, policy = compared_answer_values()
    earlier, later = stored["readings"]
    output = {
        "claims": [reading_claim("later", later), reading_claim("earlier", earlier)],
        "sections": [{"kind": "answer", "claim_ids": ["later", "earlier"]}],
    }
    result = answer_fixture.project((request, intake, plan, stored, usage, output, policy))
    intelligence = result["intelligence"]
    assert intelligence["status"] == "complete"
    assert intelligence["missing_work"] == []
    assert [row["reading_id"] for row in intelligence["readings"]] == [
        earlier["reading_id"],
        later["reading_id"],
    ]
    texts = [claim["text"] for claim in intelligence["claims"]]
    assert (
        "window_comparison_receipt_count: 6 admitted_evidence_items (2026-08-30 to 2026-09-05)."
        in texts
    )
    assert (
        "window_comparison_receipt_count: 5 admitted_evidence_items (2026-08-23 to 2026-08-29)."
        in texts
    )
    assert any("the named comparator" in line for line in intelligence["limitations"])
    cited = {row["receipt_id"] for row in intelligence["receipts"]}
    assert cited == {*earlier["source_receipt_ids"], *later["source_receipt_ids"]}


def test_no_interpretation_may_rest_on_a_window_comparison_count():
    from tests.unit import test_general_question_answer as answer_fixture

    request, intake, plan, stored, _usage, policy = compared_answer_values()
    subject = answer_fixture.module()
    context = {"request": request, "intake": intake, "plan": plan, "snapshot": stored}
    sdk = subject.build_question_answering_request(
        **context,
        policy=policy,
        remaining_input_tokens=32000,
        remaining_output_tokens=4000,
        remaining_seconds=60,
    )
    adapter = subject._answer_adapter(sdk.response_schema_digest, sdk.system_instruction_digest)
    draft = answer_fixture.typed_fixture(stored, boundary_codes=True)
    draft["interpretations"][0]["parent_claim_ids"].append("counted")
    with pytest.raises(ValueError, match="answer_typed_invalid"):
        subject._hydrate_typed_answer(draft, **context, _adapter=adapter)


# Stored round trip through the real context read


@pytest.mark.parametrize("value", [None, "enabled"])
def test_the_real_context_read_seals_and_restores_a_comparison(monkeypatch, value):
    """The full capture replay, with a comparison plan, builds and restores unchanged.

    The replay admits one row, so the comparison is refused as insufficient history;
    the stored validator still has to rebuild that record from the snapshot's own
    receipts, with the switch on or off at read time.
    """
    from tests.unit import test_general_question_query_execution as native

    original = native.runtime.plan_draft

    def comparison_draft():
        draft = original()
        draft["intent"] = "comparison"
        draft["requirements"] = [*draft["requirements"], copy.deepcopy(HISTORY_REQUIREMENT)]
        return draft

    monkeypatch.setattr(native.runtime, "plan_draft", comparison_draft)
    set_switch(monkeypatch, value)
    built = []
    real_build = snapshot.build_general_question_snapshot

    def recording_build(*args, **kwargs):
        result = real_build(*args, **kwargs)
        built.append(result)
        return result

    monkeypatch.setattr(snapshot, "build_general_question_snapshot", recording_build)
    context_fixture.test_actual_protected_capture_owned_queries_admission_and_restore_join(
        monkeypatch, False, False, False, 2
    )
    sealed = built[0]["snapshot"]
    if value is None:
        assert "window_comparison" not in sealed["provenance"]
    else:
        record = sealed["provenance"]["window_comparison"]
        assert record["state"] == "refused"
        assert record["reason"] == "insufficient_history"
        assert "insufficient_history" in sealed["missing_work"]


# Review: the real stored validator


def test_the_real_stored_validator_refuses_a_tampered_comparison(monkeypatch):
    """A comparison edited, re-folded and re-digested fails the real context validator.

    Re-folding and re-digesting keeps the snapshot self consistent, so only the rebuild
    of the comparison from the snapshot's own receipts and selection can catch it.
    """
    from tests.unit import test_general_question_query_execution as native

    original = native.runtime.plan_draft

    def comparison_draft():
        draft = original()
        draft["intent"] = "comparison"
        draft["requirements"] = [*draft["requirements"], copy.deepcopy(HISTORY_REQUIREMENT)]
        return draft

    monkeypatch.setattr(native.runtime, "plan_draft", comparison_draft)
    monkeypatch.setenv(SWITCH, "enabled")
    built, reads = [], []
    real_build = snapshot.build_general_question_snapshot
    real_validate = snapshot.validate_stored_general_question_snapshot

    def recording_build(*args, **kwargs):
        result = real_build(*args, **kwargs)
        built.append(result)
        return result

    def recording_validate(value, **kwargs):
        reads.append(dict(kwargs))
        return real_validate(value, **kwargs)

    monkeypatch.setattr(snapshot, "build_general_question_snapshot", recording_build)
    monkeypatch.setattr(snapshot, "validate_stored_general_question_snapshot", recording_validate)
    context_fixture.test_actual_protected_capture_owned_queries_admission_and_restore_join(
        monkeypatch, False, False, False, 2
    )
    sealed = built[0]["snapshot"]
    arguments = reads[0]
    monkeypatch.delenv(SWITCH)
    assert real_validate(copy.deepcopy(sealed), **arguments) == sealed
    context = {
        "request": arguments["request"],
        "intake": arguments["intake"],
        "admission": {
            "policy_digest": sealed["policy_digest"],
            "deployment_digest": sealed["deployment_digest"],
        },
    }

    def refold(record):
        provenance = copy.deepcopy(sealed["provenance"])
        provenance["window_comparison"] = record
        return snapshot._project_context_snapshot(
            context, arguments["plan"], provenance["admission"], provenance
        )

    assert refold(copy.deepcopy(sealed["provenance"]["window_comparison"])) == sealed
    tampered = copy.deepcopy(sealed["provenance"]["window_comparison"])
    if tampered["windows"] is not None:
        tampered["windows"]["later"]["count"] += 1
    else:
        tampered["selection"]["selected_items"] += 1
    forged = refold(tampered)
    assert forged != sealed
    assert forged["snapshot_digest"] == snapshot.canonical_digest(
        {key: value for key, value in forged.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError):
        real_validate(forged, **arguments)


# Review: the answer instruction


def test_the_answer_instruction_names_comparison_counts_only_when_one_is_supplied():
    import hashlib

    from tests.unit import test_general_question_answer as answer_fixture

    subject = answer_fixture.module()
    base = {
        "typed_v3": "078d513a585e0e3ab0655b814a99480fb2519843c7ad0291a59093dfb445423d",
        "typed_v4": "2d9f805fa32c52194e64a9ec123d68c4cc038cbd2c49c242d4ae8227ea2025d4",
        "typed_v5": "941b8243d26866512514c60d3f7191ac59ae242b6ecb390636f074834df49e02",
    }
    for name, digest in base.items():
        schema, instruction = subject._answer_adapter_contract(name)
        assert hashlib.sha256(instruction.encode()).hexdigest() == digest
        _, compared = subject._answer_adapter_contract(name, window_comparison=True)
        assert compared.startswith(instruction)
        assert "window_comparison_receipt_count" in compared[len(instruction) :]
        assert "selected-record counts per window" in compared
        assert "may be stated as observed" in compared
        assert "claim of change" in compared
        for text in (instruction, compared):
            assert (
                subject.answer_adapter_id(
                    subject.canonical_digest(schema), hashlib.sha256(text.encode()).hexdigest()
                )
                == name
            )

    request, intake, plan, stored, _usage, _output, policy = answer_fixture.fixture()
    arguments = {
        "policy": policy,
        "remaining_input_tokens": 32000,
        "remaining_output_tokens": 4000,
        "remaining_seconds": 60,
    }
    plain = subject.build_question_answering_request(request, intake, plan, stored, **arguments)
    assert plain.system_instruction_digest == base["typed_v4"]
    request, intake, plan, stored, _usage, policy = compared_answer_values()
    arguments["policy"] = policy
    compared = subject.build_question_answering_request(request, intake, plan, stored, **arguments)
    _, instruction = subject._answer_adapter_contract("typed_v4", window_comparison=True)
    assert compared.system_instruction_digest == hashlib.sha256(instruction.encode()).hexdigest()


# Review: a capped selection is never compared


def capped_selection(*, selector=False, lane=None):
    value = uncapped_selection(10)
    if selector:
        value["selection"] = {"kept": 10, "eligible": 37}
    if lane is not None:
        value["lanes"][lane] = {"kept": 1, "matching": 4, "capped": True}
    return value


@pytest.mark.parametrize(
    "caps,selected,eligible,lanes",
    [
        (capped_selection(selector=True), 10, 37, []),
        (capped_selection(lane="candidate"), 10, 10, ["candidate"]),
        (capped_selection(lane="raw"), 10, 10, ["raw"]),
        (capped_selection(selector=True, lane="enriched"), 10, 37, ["enriched"]),
    ],
)
def test_a_capped_selection_refuses_instead_of_comparing_ranked_samples(
    monkeypatch, caps, selected, eligible, lanes
):
    monkeypatch.setenv(SWITCH, "enabled")
    result, _plan, _records, _caps = context_build(monkeypatch, spread(6, 6), caps=caps)
    stored = result["snapshot"]
    record = stored["provenance"]["window_comparison"]
    assert record["state"] == "refused"
    assert record["reason"] == "comparison_selection_capped"
    assert record["windows"] is None
    assert record["selection"]["capped"] is True
    assert record["selection"]["selected_items"] == selected
    assert record["selection"]["eligible_items"] == eligible
    assert record["selection"]["capped_lanes"] == lanes
    assert stored["readings"] == []
    assert stored["missing_work"] == ["prior_week", "comparison_selection_capped"]
    assert any(
        line.startswith("Window comparison refused (comparison_selection_capped)")
        and f"{selected} of {eligible}" in line
        for line in stored["limitations"]
    )


def test_an_uncapped_selection_compares(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    result, _plan, _records, _caps = context_build(
        monkeypatch, spread(6, 6), caps=uncapped_selection(12)
    )
    record = result["snapshot"]["provenance"]["window_comparison"]
    assert record["state"] == "compared"
    assert record["selection"]["capped"] is False


def test_the_selection_caps_are_read_only_with_the_switch_on(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    result, _plan, _records, _caps = context_build(monkeypatch, spread(6, 6), caps="forbid")
    assert result["snapshot"]["snapshot_digest"] == OFF_SNAPSHOT_DIGEST


# Review: the uncovered line names the latest admitting cutoff


@pytest.mark.parametrize(
    "v1_cutoffs,bridge_cutoffs,latest",
    [
        (["2026-09-01", "2026-09-07"], [], "2026-09-07"),
        (["2026-09-07", "2026-09-01"], [], "2026-09-07"),
        (["2026-09-01"], ["2026-09-06"], "2026-09-06"),
        (["2026-09-06"], ["2026-09-02", "2026-09-05"], "2026-09-06"),
    ],
)
def test_the_uncovered_line_names_the_latest_admitting_cutoff(
    monkeypatch, v1_cutoffs, bridge_cutoffs, latest
):
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setenv(SWITCH, "enabled")
    pins = tuple({**PIN, "cutoff_date": cutoff} for cutoff in v1_cutoffs)
    bridges = tuple(
        SimpleNamespace(cutoff_date=cutoff, pinned=True, profile_id=f"staging_bridge_v3_{index}")
        for index, cutoff in enumerate(bridge_cutoffs)
    )
    monkeypatch.setattr(protected_context_registry, "bridge_entries", lambda: bridges)
    window = {"start": "2026-08-26", "end": "2026-09-08", "closed": True}
    result, _plan, records, _caps = context_build(
        monkeypatch, spread(6, 6), window=window, pins=pins
    )
    assert records == {}
    assert result["missing_work"][1] == "historical_window_uncovered"
    assert f"runs past {latest}, the latest pinned capture cutoff" in result["missing_work"][2]


# Review: publication after the capture


def test_a_row_published_after_source_as_of_but_collected_before_it_is_excluded():
    plan = comparison_plan(
        {
            "window": {"start": "2026-08-25", "end": "2026-09-07", "closed": True},
            "requirements": [],
        }
    )
    rows = [
        *spread(0, 0),
        receipt(0, "2026-09-08T00:00:01+00:00", collected_at="2026-09-07T10:00:00+00:00"),
        receipt(1, "2026-09-09T00:00:00+00:00", collected_at=SOURCE_AS_OF),
    ]
    record = comparison().build_window_comparison(plan, rows, BINDING, uncapped_selection())
    assert record["excluded"] == [
        {"receipt_id": "gqctx_00", "reason": "source_after_capture"},
        {"receipt_id": "gqctx_01", "reason": "source_after_capture"},
    ]
    assert record["windows"]["earlier"]["receipt_ids"] == []
    assert record["windows"]["later"]["receipt_ids"] == []


# Review: stored records rebuild under their own version


def test_a_stored_record_of_an_unknown_version_is_refused(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    result, plan, _records, _caps = context_build(monkeypatch, spread(6, 6))
    provenance = result["snapshot"]["provenance"]
    for version in (
        "general_question_window_comparison_v0",
        "general_question_window_comparison_v9",
        None,
    ):
        record = copy.deepcopy(provenance["window_comparison"])
        record["contract_version"] = version
        with pytest.raises(ValueError, match="window_comparison_invalid"):
            comparison().validate_stored_window_comparison(
                record,
                plan,
                provenance["admission"]["receipts"],
                provenance["source_binding"],
                uncapped_selection(12),
            )


def test_a_sealed_record_still_reads_after_a_later_version_changes_the_rules(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    rows = spread(5, 5)
    result, plan, _records, _caps = context_build(monkeypatch, rows)
    provenance = result["snapshot"]["provenance"]
    sealed = provenance["window_comparison"]
    assert sealed["contract_version"] == "general_question_window_comparison_v1"
    assert sealed["state"] == "compared"
    module = comparison()
    later = "general_question_window_comparison_v2"
    rules = {**module.WINDOW_COMPARISON_RULES[module.WINDOW_COMPARISON_VERSION]}
    rules["minimum_per_window"] = 6
    monkeypatch.setitem(module.WINDOW_COMPARISON_RULES, later, rules)
    monkeypatch.setattr(module, "WINDOW_COMPARISON_VERSION", later)
    selection = uncapped_selection(10)
    rebuilt = module.build_window_comparison(plan, rows, BINDING, selection)
    assert rebuilt["contract_version"] == later
    assert rebuilt["reason"] == "insufficient_history"
    arguments = (plan, provenance["admission"]["receipts"], provenance["source_binding"])
    assert module.validate_stored_window_comparison(sealed, *arguments, selection) == sealed


# Review: the odd day split is in the reading itself


def test_an_odd_day_reading_states_its_split_instant_and_an_even_one_needs_none():
    module = comparison()
    odd = comparison_plan(
        {"window": {"start": "2026-09-01", "end": "2026-09-07", "closed": True}, "requirements": []}
    )
    rows = [
        *(receipt(index, "2026-09-02T10:00:00+00:00") for index in range(5)),
        *(receipt(index, "2026-09-06T10:00:00+00:00") for index in range(5, 10)),
    ]
    value = {
        "snapshot_id": "gqs_x",
        "receipts": copy.deepcopy(rows),
        "readings": [],
        "limitations": [],
        "missing_work": ["prior_week"],
        "fulfilled_requirement_ids": ["content"],
    }
    record = module.build_window_comparison(odd, rows, BINDING, uncapped_selection(10))
    module.apply_window_comparison(value, record, odd)
    from src.analysis.open_intelligence.general_question_projection import materialize_claim_text

    readings = {row["reading_id"]: row for row in value["readings"]}
    texts = [
        materialize_claim_text(
            [{"kind": "reading", "reading_id": rid}], readings, reading_ids=[rid]
        )
        for rid in readings
    ]
    assert texts == [
        "5 admitted_evidence_items before 2026-09-04T12:00:00Z",
        "5 admitted_evidence_items from 2026-09-04T12:00:00Z",
    ]
    even = comparison_plan(
        {"window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True}, "requirements": []}
    )
    rows = spread(5, 5)
    value.update(receipts=copy.deepcopy(rows), readings=[])
    record = module.build_window_comparison(even, rows, BINDING, uncapped_selection(10))
    module.apply_window_comparison(value, record, even)
    assert [row["unit"] for row in value["readings"]] == ["admitted_evidence_items"] * 2


# Round three: caps read from a real admission, no stub


def real_admission_caps(monkeypatch, evidence_rows, *, full=None, fact_limit=None):
    """Admit through the real context admission and read its caps from the same inputs."""
    from src.analysis.open_intelligence import general_question_context_admission as admission

    from tests.unit import test_general_question_context_double_write as double_write

    if fact_limit is not None:
        monkeypatch.setattr(admission, "_FACT_LIMIT", fact_limit)
    seen = []
    real_admit = admission.admit_protected_context

    def recording_admit(request, plan, intake, **kwargs):
        seen.append((request, plan, intake, kwargs))
        return real_admit(request, plan, intake, **kwargs)

    monkeypatch.setattr(admission, "admit_protected_context", recording_admit)
    result = double_write._admit(evidence_rows, full=full)
    request, plan, intake, kwargs = seen[0]
    caps = admission.context_selection_caps(
        result,
        request=request,
        plan=plan,
        intake=intake,
        queries=kwargs["queries"],
        captures=kwargs["captures"],
        query_records=kwargs["query_records"],
    )
    comparison_plan_value = comparison_plan(
        {
            "window": {"start": "2026-09-01", "end": "2026-09-07", "closed": True},
            "requirements": copy.deepcopy(plan["requirements"]),
        }
    )
    record = comparison().build_window_comparison(
        comparison_plan_value, result["receipts"], kwargs["source"].source_binding, caps
    )
    return result, caps, record


def second_post():
    from tests.unit import test_general_question_context_double_write as double_write

    return {
        **double_write._FIRST,
        "url": "https://example.test/two-post",
        "text": "Night commute observations from a second source.",
        "collected_at": "2026-09-07T03:00:00+00:00",
        "published_at": "2026-09-06T00:00:00+00:00",
    }


def test_a_real_evidence_lane_limit_is_detected_with_its_numbers(monkeypatch):
    from tests.unit import test_general_question_context_double_write as double_write

    first = double_write._FIRST
    # Two byte identical copies of one row collapse to one admitted record, so the lane
    # returns three wire rows, keeps two records and reports nine matching at the source.
    rows = [("one", first, 2), ("one", first, 2), ("two", second_post(), 1)]
    result, caps, record = real_admission_caps(monkeypatch, rows, full=9)
    assert len(result["receipts"]) == 2
    assert caps["lanes"]["enriched"] == {"kept": 2, "matching": 8, "capped": True}
    assert caps["lanes"]["enriched_index"] == {"kept": 2, "matching": 2, "capped": False}
    assert caps["selection"] == {"kept": 2, "eligible": 2}
    assert record["state"] == "refused"
    assert record["reason"] == "comparison_selection_capped"
    assert record["windows"] is None
    assert record["selection"]["capped"] is True
    assert record["selection"]["selected_items"] == 2
    assert record["selection"]["eligible_items"] == 2
    assert record["selection"]["capped_lanes"] == ["enriched"]


def test_a_real_selector_cap_is_detected_with_its_numbers(monkeypatch):
    from tests.unit import test_general_question_context_double_write as double_write

    rows = [("one", double_write._FIRST, 1), ("two", second_post(), 1)]
    result, caps, record = real_admission_caps(monkeypatch, rows, fact_limit=1)
    assert len(result["receipts"]) == 1
    assert caps["selection"] == {"kept": 1, "eligible": 2}
    assert all(lane["capped"] is False for lane in caps["lanes"].values())
    assert caps["lanes"]["enriched"] == {"kept": 2, "matching": 2, "capped": False}
    assert record["reason"] == "comparison_selection_capped"
    assert record["selection"]["capped"] is True
    assert record["selection"]["selected_items"] == 1
    assert record["selection"]["eligible_items"] == 2
    assert record["selection"]["capped_lanes"] == []


def test_a_real_uncapped_read_is_not_refused_as_capped(monkeypatch):
    from tests.unit import test_general_question_context_double_write as double_write

    rows = [("one", double_write._FIRST, 1), ("two", second_post(), 1)]
    _result, caps, record = real_admission_caps(monkeypatch, rows)
    assert caps["selection"] == {"kept": 2, "eligible": 2}
    assert record["selection"]["capped"] is False
    assert record["reason"] == "insufficient_history"


# Round three: a stored answer bound to the other instruction variant refuses


def crossed_answer_call(values, *, variant):
    """The recorded answering call a worker would hold, made under one instruction variant."""
    from tests.unit import test_general_question_answer as answer_fixture

    subject = answer_fixture.module()
    request, intake, plan, stored, policy = values
    adapter_request = subject.build_question_answering_request(
        request,
        intake,
        plan,
        stored,
        policy=policy,
        remaining_input_tokens=32000,
        remaining_output_tokens=4000,
        remaining_seconds=60,
    )
    _, instruction = subject._answer_adapter_contract("typed_v4", window_comparison=variant)
    import hashlib

    return adapter_request, {
        "input_digest": adapter_request.input_digest,
        "system_instruction_digest": hashlib.sha256(instruction.encode()).hexdigest(),
        "response_schema_digest": adapter_request.response_schema_digest,
        "model": adapter_request.model,
        "thinking_level": adapter_request.generation_config.thinking_config.thinking_level.value,
        "max_output_tokens": adapter_request.generation_config.max_output_tokens,
    }


def rebound_request(values, call):
    """Resolve the recorded call's adapter and rebuild the request the way the worker does."""
    from tests.unit import test_general_question_answer as answer_fixture

    subject = answer_fixture.module()
    request, intake, plan, stored, policy = values
    adapter = subject._answer_adapter(
        call["response_schema_digest"], call["system_instruction_digest"]
    )
    assert adapter == "typed_v4"
    return subject.build_question_answering_request(
        request,
        intake,
        plan,
        stored,
        policy=policy,
        remaining_input_tokens=32000,
        remaining_output_tokens=4000,
        remaining_seconds=60,
        _adapter=adapter,
    )


def plain_answer_values():
    from tests.unit import test_general_question_answer as answer_fixture

    request, intake, plan, stored, _usage, _output, policy = answer_fixture.fixture()
    return request, intake, plan, stored, policy


def compared_values():
    request, intake, plan, stored, _usage, policy = compared_answer_values()
    return request, intake, plan, stored, policy


@pytest.mark.parametrize(
    "values,variant",
    [(plain_answer_values, True), (compared_values, False)],
    ids=["comparison_digest_on_plain_snapshot", "plain_digest_on_comparison_snapshot"],
)
def test_a_stored_answer_bound_to_the_other_instruction_variant_refuses(values, variant):
    from src.analysis.open_intelligence import general_question_runtime as runtime
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    value = values()
    _own, crossed = crossed_answer_call(value, variant=variant)
    rebuilt = rebound_request(value, crossed)
    assert rebuilt.input_digest == crossed["input_digest"]
    with pytest.raises(QuestionStoreError, match="answer_binding_invalid"):
        runtime._answer_binding(crossed, rebuilt)
    _own, matching = crossed_answer_call(value, variant=not variant)
    runtime._answer_binding(matching, rebound_request(value, matching))


def test_the_comparison_variant_digests_are_pinned():
    import hashlib

    from tests.unit import test_general_question_answer as answer_fixture

    subject = answer_fixture.module()
    pinned = {
        "typed_v3": "d1990dc932073c923f088f0b938f7925c1351b61efc4cb459556d53958c14f80",
        "typed_v4": "c3cdb6ec4b62f54565158b47c0c9e98009222ff186ae4c085eee242b36c6149e",
        "typed_v5": "fdead991a6b6d5bdafa1cbb32ad702cbdac6cf9dfdbbfe175830505f8bee8840",
    }
    for name, digest in pinned.items():
        _, instruction = subject._answer_adapter_contract(name, window_comparison=True)
        assert hashlib.sha256(instruction.encode()).hexdigest() == digest
    for name in ("typed", "typed_v1", "compact", "span_v1", "span_v2", "span_v3"):
        assert subject._answer_adapter_contract(
            name, window_comparison=True
        ) == subject._answer_adapter_contract(name)


# Round three: an unpinned bridge entry is never named


def test_an_unpinned_bridge_entry_with_a_later_cutoff_is_not_named(monkeypatch):
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setenv(SWITCH, "enabled")
    bridges = (
        SimpleNamespace(cutoff_date="2026-09-07", pinned=False, profile_id="staging_bridge_v3_0"),
    )
    monkeypatch.setattr(protected_context_registry, "bridge_entries", lambda: bridges)
    window = {"start": "2026-08-26", "end": "2026-09-08", "closed": True}
    result, _plan, records, _caps = context_build(
        monkeypatch, spread(6, 6), window=window, pins=({**PIN, "cutoff_date": "2026-09-01"},)
    )
    assert records == {}
    assert "runs past 2026-09-01, the latest pinned capture cutoff" in result["missing_work"][2]


# Round three: each version names its own record shape


def test_a_later_version_may_change_the_record_shape_and_v1_still_reads(monkeypatch):
    monkeypatch.setenv(SWITCH, "enabled")
    rows = spread(5, 5)
    result, plan, _records, _caps = context_build(monkeypatch, rows)
    provenance = result["snapshot"]["provenance"]
    sealed = provenance["window_comparison"]
    module = comparison()
    v1 = module.WINDOW_COMPARISON_RULES["general_question_window_comparison_v1"]
    later = "general_question_window_comparison_v2"
    monkeypatch.setitem(
        module.WINDOW_COMPARISON_RULES,
        later,
        {**v1, "record_fields": v1["record_fields"] | {"note"}},
    )
    monkeypatch.setattr(module, "WINDOW_COMPARISON_VERSION", later)
    selection = uncapped_selection(10)
    arguments = (plan, provenance["admission"]["receipts"], provenance["source_binding"])
    assert module.validate_stored_window_comparison(sealed, *arguments, selection) == sealed
    relabelled = {**copy.deepcopy(sealed), "contract_version": later}
    with pytest.raises(ValueError, match="window_comparison_invalid"):
        module.validate_stored_window_comparison(relabelled, *arguments, selection)
    assert "record_fields" not in sealed
    assert set(sealed) == v1["record_fields"]
