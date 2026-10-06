"""Discover payload builder and BQ helpers."""

from datetime import date

from src.api import bq, seed_discover


def test_monday_on_or_before():
    assert bq._monday_on_or_before(date(2026, 7, 2)) == date(2026, 6, 29)
    assert bq._monday_on_or_before(date(2026, 6, 30)) == date(2026, 6, 29)


def test_seed_fit_dict_from_mapping():
    row = {"seed_fit": {"co_occur": 0.7, "genz": None, "slang": 0.4}}
    fit = bq._seed_fit_dict(row)
    assert fit["co_occur"] == 0.7
    assert "genz" not in fit
    assert fit["slang"] == 0.4


def test_build_discover_payload_shapes_rows(monkeypatch):
    rows = [
        {
            "candidate_id": "abc",
            "proposed_date": date(2026, 7, 1),
            "market": "ke",
            "candidate_type": "keyword",
            "candidate_value": "gengetone",
            "source": "seed_graph",
            "lane": "fast",
            "score": 0.91,
            "seed_fit": {"co_occur": 0.6, "visual_audio": 0.5, "genz": 0.7, "slang": 0.3},
            "safety_flags": [],
            "evidence_topics": ["music_gengetone"],
            "sample_row_ids": ["r1", "r2"],
            "status": "pending",
            "rationale": None,
        }
    ]
    monkeypatch.setattr(bq, "fetch_seed_candidates_week", lambda **kw: rows)
    monkeypatch.setattr(bq, "_dataset", lambda: "trends_v2_dev")
    monkeypatch.setattr(bq, "topic_label", lambda tg: tg.replace("_", " ").title())

    payload = seed_discover.build_discover_payload(
        market=None,
        status="pending",
        week_start=date(2026, 6, 30),
    )
    assert payload["dataset"] == "trends_v2_dev"
    ke = payload["markets"]["ke"]
    assert ke["pending_count"] == 1
    assert ke["fast_count"] == 1
    c = ke["candidates"][0]
    assert c["term"] == "gengetone"
    assert c["sample_post_count"] == 2
    assert c["topics"][0]["id"] == "music_gengetone"


def test_build_discover_payload_bq_error(monkeypatch):
    def boom(**kw):
        raise RuntimeError("table missing")

    monkeypatch.setattr(bq, "fetch_seed_candidates_week", boom)
    monkeypatch.setattr(bq, "_dataset", lambda: "trends_v2_dev")

    payload = seed_discover.build_discover_payload(week_start=date(2026, 6, 30))
    assert payload["error"]
    assert payload["markets"]["za"]["candidates"] == []
