"""Bridge reads name only the pinned clone tables, and every row keeps its provenance.

The loaded capture comes from the synthetic, unissued world of the loader tests.
"""

import dataclasses

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest

from tests.unit.test_bridge_history_loader import load, register, world

CLONE = "ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_"


def queries():
    from src.analysis.open_intelligence import bridge_history_queries

    return bridge_history_queries


@pytest.fixture
def loaded(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    return load(w)


def parameter(query, name):
    (value,) = [item for item in query["parameters"] if item["name"] == name]
    return value


def array(query, name):
    return [item["value"] for item in parameter(query, name)["parameterValue"]["arrayValues"]]


def test_history_query_reads_only_the_pinned_clone_for_the_admitted_cells(loaded):
    query = queries().bridge_history_query(
        loaded, "trend_analysis", window_start="2026-09-14", window_end="2026-09-20"
    )
    assert f"`{CLONE}trend_analysis`" in query["sql"]
    assert "trends_v2_staging.trend_analysis`" not in query["sql"]
    assert query["sql"].count("`") == 2
    assert "trend_date" in query["sql"]
    assert array(query, "cell_keys") == ["ng|2026-09-19", "za|2026-09-19"]
    assert array(query, "markets") == ["ng", "za"]
    assert parameter(query, "markets")["parameterType"] == {
        "type": "ARRAY",
        "arrayType": {"type": "STRING"},
    }


def test_history_query_uses_the_lane_s_own_partition_column(loaded):
    assert (
        queries().bridge_history_query(
            loaded, "seed_candidates", window_start="2026-09-14", window_end="2026-09-20"
        )
        is None
    )
    assert queries().partition_column("seed_candidates") == "proposed_date"
    assert queries().partition_column("trend_analysis") == "trend_date"


def test_a_window_without_admitted_cells_reads_nothing(loaded):
    assert (
        queries().bridge_history_query(
            loaded, "trend_analysis", window_start="2026-09-20", window_end="2026-09-20"
        )
        is None
    )


@pytest.mark.parametrize("lane", ["event_ledger", "seed_graph", "trend_scores"])
def test_lanes_without_a_completion_record_are_never_queried(loaded, lane):
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().bridge_history_query(
            loaded, lane, window_start="2026-09-14", window_end="2026-09-20"
        )


@pytest.mark.parametrize(
    "window",
    [
        ("2026-09-20", "2026-09-19"),
        ("2026-09-14", "2026-09-21"),
        ("2026-9-14", "2026-09-20"),
        (None, "2026-09-20"),
    ],
)
def test_window_must_be_a_closed_range_inside_the_capture(loaded, window):
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().bridge_history_query(
            loaded, "trend_analysis", window_start=window[0], window_end=window[1]
        )


def test_a_relation_that_differs_from_the_pinned_clone_is_refused(loaded):
    relations = [dict(item) for item in loaded.binding["relation_bindings"]]
    for item in relations:
        if item["lane"] == "trend_analysis":
            item["destination_table"] = "ogilvy-trends-v2.trends_v2_staging.trend_analysis"
    changed = dataclasses.replace(
        loaded, binding={**loaded.binding, "relation_bindings": relations}
    )
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().bridge_history_query(
            changed, "trend_analysis", window_start="2026-09-14", window_end="2026-09-20"
        )


def test_only_a_loaded_bridge_source_is_queried(loaded):
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().bridge_history_query(
            dataclasses.asdict(loaded),
            "trend_analysis",
            window_start="2026-09-14",
            window_end="2026-09-20",
        )


def test_collection_query_is_bounded_by_the_closed_observation_window(loaded):
    query = queries().bridge_collection_query(
        loaded, "enriched_content", window_start="2026-09-14", window_end="2026-09-20"
    )
    assert f"`{CLONE}enriched_content`" in query["sql"]
    assert "intelligence_42_sources_staging.enriched_content`" not in query["sql"]
    assert parameter(query, "window_start")["parameterValue"]["value"] == (
        "2026-09-14T00:00:00.000000Z"
    )
    assert parameter(query, "window_end")["parameterValue"]["value"] == (
        "2026-09-21T00:00:00.000000Z"
    )
    assert parameter(query, "window_end")["parameterType"] == {"type": "TIMESTAMP"}
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().bridge_collection_query(
            loaded, "trend_analysis", window_start="2026-09-14", window_end="2026-09-20"
        )


def test_history_rows_carry_provenance_for_their_cell(loaded):
    rows = [
        {"market": "za", "trend_date": "2026-09-19", "term": "load shedding"},
        {"market": "ng", "trend_date": "2026-09-19", "term": "fuel"},
    ]
    admitted = queries().admit_history_rows(
        loaded, "trend_analysis", rows, window_start="2026-09-14", window_end="2026-09-20"
    )
    cells = {(cell["market"], cell["product_date"]): cell for cell in loaded.cells}
    for row, item in zip(rows, admitted, strict=True):
        assert item["row"] == row
        cell = cells[(row["market"], row["trend_date"])]
        assert item["provenance"] == {
            "registry_entry_digest": canonical_digest(loaded.registry_entry),
            "result_id": loaded.binding["result_id"],
            "snapshot_digest": loaded.binding["snapshot_digest"],
            "completion_entry_digest": cell["completion_entry_digest"],
            "available_at": cell["available_at"],
        }
    admitted[0]["row"]["term"] = "changed"
    assert rows[0]["term"] == "load shedding"


@pytest.mark.parametrize(
    "row",
    [
        {"market": "ke", "trend_date": "2026-09-19", "term": "outside scope"},
        {"market": "za", "trend_date": "2026-09-20", "term": "pending at cutoff"},
        {"market": "za", "trend_date": "2026-09-18", "term": "never admitted"},
        {"trend_date": "2026-09-19", "term": "no market"},
        {"market": "za", "term": "no date"},
    ],
)
def test_a_row_outside_the_admitted_cells_refuses_the_read(loaded, row):
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().admit_history_rows(
            loaded, "trend_analysis", [row], window_start="2026-09-14", window_end="2026-09-20"
        )


def test_admitted_row_count_cannot_exceed_the_completion_record(loaded):
    rows = [{"market": "ng", "trend_date": "2026-09-19", "term": str(n)} for n in range(2)]
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().admit_history_rows(
            loaded, "trend_analysis", rows, window_start="2026-09-14", window_end="2026-09-20"
        )


def test_a_history_row_in_an_admitted_cell_outside_the_window_refuses(loaded):
    # 19 Sept is an admitted completed cell, but a read for 20 Sept alone never selects it,
    # so a row for it is outside the read exactly as a collection row outside the window is.
    row = {"market": "za", "trend_date": "2026-09-19", "term": "load shedding"}
    assert queries().admit_history_rows(
        loaded, "trend_analysis", [row], window_start="2026-09-19", window_end="2026-09-19"
    )
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().admit_history_rows(
            loaded, "trend_analysis", [row], window_start="2026-09-20", window_end="2026-09-20"
        )


@pytest.mark.parametrize(
    "window",
    [("2026-09-20", "2026-09-19"), ("2026-09-14", "2026-09-21"), ("2026-9-14", "2026-09-20")],
)
def test_history_rows_are_admitted_only_for_a_closed_window_inside_the_capture(loaded, window):
    row = {"market": "za", "trend_date": "2026-09-19", "term": "load shedding"}
    with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
        queries().admit_history_rows(
            loaded, "trend_analysis", [row], window_start=window[0], window_end=window[1]
        )


def test_collection_rows_carry_the_capture_provenance(loaded):
    rows = [{"market": "za", "collected_at": "2026-09-20T10:00:00Z", "text": "x"}]
    admitted = queries().admit_collection_rows(
        loaded, "raw_content", rows, window_start="2026-09-14", window_end="2026-09-20"
    )
    assert admitted[0]["provenance"] == {
        "registry_entry_digest": canonical_digest(loaded.registry_entry),
        "result_id": loaded.binding["result_id"],
        "snapshot_digest": loaded.binding["snapshot_digest"],
        "collection_receipt_set_digest": loaded.binding["collection_receipt_set_digest"],
        "available_at": loaded.binding["available_at"],
    }
    for bad in (
        {"market": "ke", "collected_at": "2026-09-20T10:00:00Z"},
        {"market": "za", "collected_at": "2026-09-21T00:00:00Z"},
        {"market": "za", "collected_at": "2026-09-13T23:59:59Z"},
        {"market": "za", "collected_at": "2026-09-20T10:00:00"},
    ):
        with pytest.raises(ValueError, match=r"^bridge_query_invalid$"):
            queries().admit_collection_rows(
                loaded, "raw_content", [bad], window_start="2026-09-14", window_end="2026-09-20"
            )


def test_unavailable_cells_in_the_window_are_named_limitations_never_zero(loaded):
    limitations = queries().bridge_limitations(
        loaded, window_start="2026-09-19", window_end="2026-09-20"
    )
    assert limitations == sorted(
        limitations, key=lambda item: (item["lane"], item["market"], item["product_date"])
    )
    keys = {(item["lane"], item["market"], item["product_date"]) for item in limitations}
    assert ("trend_analysis", "za", "2026-09-20") in keys
    assert ("trend_analysis", "za", "2026-09-19") not in keys
    assert all(item["market"] in ("ng", "za") for item in limitations)
    assert {item["reason_code"] for item in limitations} == {
        "product_pending_at_cutoff",
        "native_completion_unrecorded",
    }
    assert queries().bridge_limitations(
        loaded, window_start="2026-09-20", window_end="2026-09-20"
    ) == [item for item in limitations if item["product_date"] == "2026-09-20"]


def test_window_days_outside_the_completion_set_are_limitations_too(loaded):
    limitations = queries().bridge_limitations(
        loaded, window_start="2026-09-18", window_end="2026-09-20"
    )
    outside = [item for item in limitations if item["product_date"] == "2026-09-18"]
    assert {(item["lane"], item["market"]) for item in outside} == {
        (lane, market)
        for lane in (
            "event_ledger",
            "seed_candidates",
            "seed_graph",
            "trend_analysis",
            "trend_scores",
        )
        for market in ("ng", "za")
    }
    assert {item["reason_code"] for item in outside} == {"outside_history_completion_set"}
