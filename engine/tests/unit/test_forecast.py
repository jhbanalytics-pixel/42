"""Unit tests for the Layer 2 forecast outlook.

Pure-logic and render-string tests only; no BigQuery. The IO path
(``compute_forecast_outlook``) is exercised through a raising client mock to
prove it is non-fatal. The OUTLOOK chip is asserted via ``render_card`` so the
flag-off (absent field) render stays byte-identical to today's.
"""

import datetime
from unittest import mock

from src.scoring.forecast import (
    _archive_predictions,
    classify_outlook,
    compute_forecast_outlook,
    tag_briefs_with_outlook,
)


def test_classify_outlook_heating():
    assert classify_outlook(0.30, 0.40) == "heating"


def test_classify_outlook_cooling():
    assert classify_outlook(0.40, 0.30) == "cooling"


def test_classify_outlook_steady_within_band():
    assert classify_outlook(0.30, 0.32) == "steady"


def test_classify_outlook_band_edge_belongs_to_steady():
    # A move of exactly the band stays steady; one tick past it flips.
    assert classify_outlook(0.30, 0.34) == "steady"
    assert classify_outlook(0.30, 0.3401) == "heating"
    assert classify_outlook(0.30, 0.26) == "steady"
    assert classify_outlook(0.30, 0.2599) == "cooling"


def test_classify_outlook_custom_band():
    assert classify_outlook(0.30, 0.35, band=0.10) == "steady"
    assert classify_outlook(0.30, 0.45, band=0.10) == "heating"


def test_compute_forecast_outlook_non_fatal_on_bq_error():
    # A BigQuery/client failure must yield {} (never raise), so the cron and the
    # email are never blocked by the forecast.
    with mock.patch("src.scoring.forecast.get_client", side_effect=RuntimeError("bq down")):
        out = compute_forecast_outlook(datetime.date(2026, 6, 1))
    assert out == {}


def test_compute_forecast_outlook_happy_path():
    # Real-rows path: all three SQLs run (train, select, outlook), and the day-7
    # vs latest-actual classification flows through into the keyed dict. The
    # train/select jobs return the same stub; only the outlook rows are iterated.
    rows = [
        {
            "market": "za",
            "query_group": "music_amapiano",
            "latest_actual": 0.30,
            "day7_forecast": 0.40,
        },
        {
            "market": "ng",
            "query_group": "music_afrobeats",
            "latest_actual": 0.50,
            "day7_forecast": 0.50,
        },
        {"market": "ke", "query_group": "genz_sheng", "latest_actual": 0.40, "day7_forecast": 0.30},
    ]
    fake_job = mock.Mock()
    fake_job.result.return_value = rows
    fake_client = mock.Mock()
    fake_client.project = "p"
    fake_client.query.return_value = fake_job
    with (
        mock.patch("src.scoring.forecast.get_client", return_value=fake_client),
        mock.patch("src.scoring.forecast.get_dataset", return_value="ds"),
    ):
        out = compute_forecast_outlook(datetime.date(2026, 6, 1))
    # train + select + outlook + the archive run_date dedupe probe.
    assert fake_client.query.call_count == 4
    assert out[("za", "music_amapiano")]["outlook"] == "heating"
    assert out[("ng", "music_afrobeats")]["outlook"] == "steady"
    assert out[("ke", "genz_sheng")]["outlook"] == "cooling"
    assert out[("za", "music_amapiano")]["delta"] == 0.1


def test_tag_briefs_with_outlook_matches_and_misses():
    # Pins the (market, query_group) key-shape contract between the forecast dict
    # and briefs_by_topic: a matched key is tagged, a non-matched brief is left
    # untouched, so key-shape drift degrades to no-chip rather than an error.
    briefs = {("za", "music_amapiano"): {}, ("ng", "music_afrobeats"): {}}
    outlook = {
        ("za", "music_amapiano"): {"outlook": "heating"},
        ("ke", "genz_sheng"): {"outlook": "cooling"},  # no matching brief
    }
    tagged = tag_briefs_with_outlook(briefs, outlook)
    assert tagged == 1
    assert briefs[("za", "music_amapiano")]["forecast_outlook"] == "heating"
    assert "forecast_outlook" not in briefs[("ng", "music_afrobeats")]


# --- OUTLOOK chip render -------------------------------------------------


def _render(brief_extra: dict) -> str:
    from src.alerts.email_render.brand import load_brand
    from src.alerts.email_render.card import render_card

    brand = load_brand("ogilvy")
    brief = {
        "market": "za",
        "topic": "music_amapiano",
        "headline": "Amapiano holds the line",
        "trend_synthesis": "Still the default sound of the timeline.",
        "trend_score": 0.5,
        **brief_extra,
    }
    display = {
        "state": {"badge": "", "direction": "up"},
        "phase": "Steady",
        "confidence": "single-source",
        "channels": [],
    }
    return render_card(brief, display, brand)


def test_outlook_chip_present_when_set():
    html = _render({"forecast_outlook": "heating"})
    assert "OUTLOOK" in html
    assert "heating" in html


def test_outlook_chip_absent_when_unset():
    # The default (flag off) path: no field, no chip, byte-identical render.
    assert "OUTLOOK" not in _render({})


def test_outlook_chip_ignores_unknown_value():
    # Only the three known outlook words earn a chip; anything else is dropped.
    assert "OUTLOOK" not in _render({"forecast_outlook": "exploding"})


def test_outlook_off_path_is_byte_identical():
    # The hard contract, stronger than "OUTLOOK absent": the no-field, empty, and
    # unknown-value renders are all byte-identical (the slot adds exactly nothing),
    # pinning the flag-off path against a stray-whitespace or chip-reorder regression.
    base = _render({})
    assert _render({"forecast_outlook": ""}) == base
    assert _render({"forecast_outlook": "exploding"}) == base


def _render_hero(brief_extra: dict) -> str:
    from src.alerts.email_render import render_hero
    from src.alerts.email_render.brand import load_brand

    brand = load_brand("ogilvy")
    brief = {
        "market": "za",
        "topic": "music_amapiano",
        "headline": "Amapiano holds the line",
        "trend_synthesis": "Still the default sound of the timeline.",
        "trend_score": 0.5,
        **brief_extra,
    }
    display = {
        "state": {"badge": "", "direction": "up"},
        "phase": "Steady",
        "confidence": "single-source",
        "channels": [],
    }
    return render_hero(brief, display, brand)


def test_hero_outlook_chip_present_when_set():
    # The lead brief renders as the hero, a chip path distinct from render_card.
    html = _render_hero({"forecast_outlook": "heating"})
    assert "OUTLOOK" in html
    assert "heating" in html


def test_hero_outlook_chip_absent_when_unset():
    assert "OUTLOOK" not in _render_hero({})


def test_hero_outlook_chip_ignores_unknown_value():
    assert "OUTLOOK" not in _render_hero({"forecast_outlook": "exploding"})


# ----- _archive_predictions same-day dedupe ----------------------------------


class _ProbeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return iter(self._rows)


class _ArchiveClient:
    """Records insert_rows_json calls; answers the run_date probe with preset rows."""

    def __init__(self, existing_rows):
        self.existing_rows = existing_rows
        self.insert_calls = []

    def query(self, _sql, *, job_config=None):
        return _ProbeJob(self.existing_rows)

    def insert_rows_json(self, _table_ref, rows):
        self.insert_calls.append(rows)
        return []


_ROWS = [
    {"run_date": "2026-06-16", "market": "za", "query_group": "music_amapiano"},
    {"run_date": "2026-06-16", "market": "ng", "query_group": "music_afrobeats"},
]


def test_archive_skips_insert_when_run_date_already_present():
    # A same-day re-run must not write duplicate rows that would skew the
    # watchdog MAE gate.
    client = _ArchiveClient(existing_rows=[(1,)])
    _archive_predictions(client, "proj", "ds", _ROWS)
    assert client.insert_calls == []


def test_archive_inserts_when_run_date_absent():
    client = _ArchiveClient(existing_rows=[])
    _archive_predictions(client, "proj", "ds", _ROWS)
    assert client.insert_calls == [_ROWS]


def test_archive_inserts_when_probe_raises():
    # The dedupe probe is non-fatal: a probe failure must still attempt the
    # insert rather than silently dropping the archive write.
    class _RaisingProbeClient(_ArchiveClient):
        def query(self, _sql, *, job_config=None):
            raise RuntimeError("table not found")

    client = _RaisingProbeClient(existing_rows=[])
    _archive_predictions(client, "proj", "ds", _ROWS)
    assert client.insert_calls == [_ROWS]


def test_archive_no_rows_is_noop():
    client = _ArchiveClient(existing_rows=[(1,)])
    _archive_predictions(client, "proj", "ds", [])
    assert client.insert_calls == []
