"""Unit tests for seed_path builder (V3 Track A5, dark)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.analysis.seed_path import (
    _coverage_note,
    _is_generic_seed_term,
    build_seed_path,
)


def _row(term, platform, row_count, first_seen=None, term_type="slang", genz=0.0):
    return SimpleNamespace(
        term=term,
        term_type=term_type,
        platform=platform,
        row_count=row_count,
        avg_genz_score=genz,
        first_seen_event_date=first_seen,
        first_seen_ingest_date=None,
    )


def _run(rows, market="ke", topic="fintech_mpesa", trend_date=date(2026, 7, 7)):
    mock_client = MagicMock()
    mock_client.query.return_value.result.return_value = rows
    with (
        patch("src.analysis.seed_path.get_client", return_value=mock_client),
        patch("src.analysis.seed_path.get_dataset", return_value="trends_v2_dev"),
    ):
        return build_seed_path(market, topic, trend_date)


def test_coverage_note_names_missing_platforms():
    note = _coverage_note({"tiktok", "instagram"})
    assert "twitter" in note.lower()


def test_is_generic_seed_term_flags_geo_and_platform_mashups():
    assert _is_generic_seed_term("nairobi")
    assert _is_generic_seed_term("kenya")
    assert _is_generic_seed_term("kenyantiktok")
    assert _is_generic_seed_term("nairobitiktokers")
    assert _is_generic_seed_term("tiktoksouthafrica")
    assert _is_generic_seed_term("south")
    assert not _is_generic_seed_term("mpesa")
    assert not _is_generic_seed_term("amapiano")
    assert not _is_generic_seed_term("googlegemini")


def test_topical_term_outranks_generic_geo_headline():
    # nairobi has far more rows but is a bare place name; mpesa must win.
    rows = [
        _row("nairobi", "tiktok", 129),
        _row("nairobi", "instagram", 54),
        _row("mpesa", "tiktok", 32),
    ]
    out = _run(rows)
    assert out["term"] == "mpesa"


def test_generic_headline_survives_when_no_topical_term():
    rows = [_row("lagos", "tiktok", 40), _row("kenyantiktok", "instagram", 20)]
    out = _run(rows)
    assert out["term"] in {"lagos", "kenyantiktok"}


def test_slang_headline_outranks_higher_volume_token():
    # A body-text token ("south", 200 rows) must lose to a lower-volume slang
    # cultural term ("amapiano", 60 rows) via the term_type reweight.
    rows = [
        _row("south", "youtube", 200, term_type="token"),
        _row("amapiano", "tiktok", 60, term_type="slang"),
    ]
    out = _run(rows, market="za", topic="music_amapiano")
    assert out["term"] == "amapiano"


def test_headline_is_invariant_to_genz_measurement():
    low = [
        _row("amapiano", "tiktok", 50, term_type="slang", genz=0.0),
        _row("otherterm", "tiktok", 40, term_type="slang", genz=0.0),
    ]
    high = [low[0], _row("otherterm", "tiktok", 40, term_type="slang", genz=1.0)]

    assert _run(low, market="za", topic="music_amapiano")["term"] == "amapiano"
    assert _run(high, market="za", topic="music_amapiano")["term"] == "amapiano"


def test_seed_path_query_does_not_load_genz_measurement():
    mock_client = MagicMock()
    mock_client.query.return_value.result.return_value = []
    with (
        patch("src.analysis.seed_path.get_client", return_value=mock_client),
        patch("src.analysis.seed_path.get_dataset", return_value="trends_v2_dev"),
    ):
        build_seed_path("za", "music_amapiano", date(2026, 7, 7))

    sql = mock_client.query.call_args.args[0]
    assert "avg_genz_score" not in sql


def test_search_boilerplate_barred_from_headline():
    # Google-Trends scaffolding tokens on the search platform never headline.
    rows = [
        _row("terms", "search", 100, term_type="token"),
        _row("ranked", "search", 100, term_type="token"),
        _row("gengetone", "tiktok", 40, term_type="slang"),
    ]
    out = _run(rows, market="ke", topic="music_gengetone")
    assert out["term"] == "gengetone"


def test_duplicate_term_platform_channels_are_deduped():
    # Same (term, platform) arriving twice (residual term_type dupes) must
    # collapse to one channel with summed row_count.
    rows = [
        _row("mpesa", "tiktok", 20, date(2026, 7, 5)),
        _row("mpesa", "tiktok", 12, date(2026, 7, 3)),
        _row("mpesa", "instagram", 8, date(2026, 7, 6)),
    ]
    out = _run(rows)
    assert out["term"] == "mpesa"
    tiktok = [c for c in out["channels"] if c["platform"] == "tiktok"]
    assert len(tiktok) == 1
    assert tiktok[0]["row_count"] == 32
    # earliest first_seen kept
    assert tiktok[0]["first_seen"] == "2026-07-03"


def test_build_seed_path_empty_returns_thin():
    mock_client = MagicMock()
    mock_client.query.return_value.result.return_value = []
    with (
        patch("src.analysis.seed_path.get_client", return_value=mock_client),
        patch("src.analysis.seed_path.get_dataset", return_value="trends_v2_dev"),
    ):
        out = build_seed_path("za", "music_amapiano", date(2026, 7, 1))
    assert out["confidence"] == "thin"
    assert out["channels"] == []
