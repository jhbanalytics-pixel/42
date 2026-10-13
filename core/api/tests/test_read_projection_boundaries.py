import datetime as dt

import pytest

from core.api import discover, fieldwork, history
from core.api.tests.test_fieldwork import D30, NOW, Patched


@pytest.mark.parametrize("posts", [
    {"2025-09-24": 100, "2025-09-26": 10},
    {"2025-09-24": 100, "2025-09-25": 60, "2025-09-26": 50},
])
def test_analogue_does_not_infer_fade_from_missing_day_or_wave_end(posts):
    wave = {"item_id": "synthetic", "market": "ZA", "wave_start": "2025-09-24",
            "wave_end": "2025-09-26", "peak_date": "2025-09-24", "peak_posts": 100}
    days = [{"item_id": "synthetic", "metric_date": day, "posts": count} for day, count in posts.items()]
    result = history._analogue({"item_id": "synthetic"}, "Synthetic", wave, days, 0, "synthetic_run")
    assert result["days_to_fade"] is None


def test_analogue_reports_fade_only_after_contiguous_observed_days():
    wave = {"item_id": "synthetic", "market": "ZA", "wave_start": "2025-09-24",
            "wave_end": "2025-09-26", "peak_date": "2025-09-24", "peak_posts": 100}
    days = [{"item_id": "synthetic", "metric_date": day, "posts": count} for day, count in
            [("2025-09-24", 100), ("2025-09-25", 60), ("2025-09-26", 49)]]
    result = history._analogue({"item_id": "synthetic"}, "Synthetic", wave, days, 0, "synthetic_run")
    assert result["days_to_fade"]["value"] == 2


@pytest.mark.parametrize("as_datetime", [False, True])
def test_fieldwork_shows_newer_running_attempt_after_finished_collection(as_datetime):
    runs = [
        {"run_id": "old", "stage": "collect", "status": "ok", "started_at": "2026-09-30T02:00:00+02:00",
         "finished_at": "2026-09-30T02:10:00+02:00", "counts": {"search_signal_states": {"ZA": "ok"}}},
        {"run_id": "new", "stage": "collect", "status": "running", "started_at": "2026-09-30T03:00:00+02:00",
         "finished_at": None, "counts": {}},
    ]
    if as_datetime:
        for run in runs:
            for key in ("started_at", "finished_at"):
                if run[key]:
                    run[key] = dt.datetime.fromisoformat(run[key])
    result = fieldwork.build_fieldwork(Patched(runs=lambda *_: runs), D30, now=NOW)
    assert result["collect_run"]["run_id"] == "new"
    assert result["collect_run"]["status"] == "running"
    trends = next(s for m in result["markets"] if m["market"] == "ZA" for s in m["sources"]
                  if s["series"] == "google_trends")
    assert trends["status_words"] != "Trending searches read"


def test_fieldwork_uses_terminal_row_of_same_attempt():
    run = {"run_id": "same", "started_at": "2026-09-30T03:00:00+02:00"}
    rows = [dict(run, status="running", finished_at=None),
            dict(run, status="ok", finished_at="2026-09-30T03:10:00+02:00")]
    assert fieldwork._collect_run(Patched(runs=lambda *_: rows), D30)["status"] == "ok"


@pytest.mark.parametrize("same_id", [False, True])
def test_topic_series_preserves_protocol_identity_without_overwriting(same_id):
    rows = [
        {"platform": "tiktok", "series": "feed_tiktok", "series_id": "a", "protocol": "feed?kind=hot",
         "day": "2026-09-30", "value": 3},
        {"platform": "tiktok", "series": "feed_tiktok", "series_id": "a" if same_id else "b",
         "protocol": "feed?kind=rising", "day": "2026-09-30", "value": 7},
    ]
    for ordered in (rows, list(reversed(rows))):
        result = discover._series(ordered, "2026-09-30")
        assert len(result) == 2
        assert {r["protocol"]: r["points"][-1]["value"] for r in result} == {
            "feed?kind=hot": 3, "feed?kind=rising": 7}
        assert {r["series_id"] for r in result} == ({"a"} if same_id else {"a", "b"})


def test_topic_series_legacy_rows_remain_one_stream_with_canonical_platform():
    rows = [{"platform": platform, "series": "panel_x_hub", "day": day, "value": value} for
            platform, day, value in [("twitter", "2026-09-29", 4), ("x", "2026-09-30", 5)]]
    result = discover._series(rows, "2026-09-30")
    assert len(result) == 1
    assert result[0]["platform"] == "x"
    assert "series_id" not in result[0] and "protocol" not in result[0]
    assert [p["value"] for p in result[0]["points"][-2:]] == [4, 5]
