"""Seed Explorer bq helpers: adjacency and behaviour path."""

from datetime import date

from src.api import bq


def _seed_rows():
    return [
        {
            "market": "za",
            "term": "amapiano",
            "term_type": "token",
            "platform": "tiktok",
            "trend_date": date(2026, 7, 1),
            "row_count": 120,
            "topic_groups": ["music_amapiano"],
            "near_topics": ["music_afrobeats"],
            "co_occur_terms": ["piano", "groove"],
        },
        {
            "market": "za",
            "term": "amapiano",
            "term_type": "token",
            "platform": "instagram",
            "trend_date": date(2026, 7, 1),
            "row_count": 80,
            "topic_groups": ["music_amapiano"],
            "near_topics": [],
            "co_occur_terms": ["piano", "dance"],
        },
    ]


def test_fetch_seed_graph_adjacency_aggregates(monkeypatch):
    bq._seed_graph_cache.clear()
    calls = []

    def fake_latest(market):
        assert market == "za"
        return date(2026, 7, 1)

    def fake_rows(market, trend_date, terms):
        calls.append((market, trend_date, terms))
        return _seed_rows()

    monkeypatch.setattr(bq, "_latest_seed_graph_date", fake_latest)
    monkeypatch.setattr(bq, "_fetch_seed_graph_term_rows", fake_rows)
    monkeypatch.setattr(bq, "_fetch_bridge_creators_for_keyword", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_lexicon_rows", lambda *a, **k: [{"term": "piano", "n": 42}])

    out = bq.fetch_seed_graph_adjacency("Amapiano", "za")
    assert out["found"] is True
    assert out["market"] == "za"
    assert out["keyword"] == "amapiano"
    terms = {c["term"] for c in out["co_occur_terms"]}
    assert "piano" in terms
    assert out["topic_overlap"][0]["topic"] == "music_amapiano"
    assert out["lexicon_hits"][0]["term"] == "piano"
    assert calls[0][0] == "za"


def test_fetch_seed_graph_adjacency_cached(monkeypatch):
    bq._seed_graph_cache.clear()
    n = {"v": 0}

    def fake_rows(*a, **k):
        n["v"] += 1
        return _seed_rows()

    monkeypatch.setattr(bq, "_latest_seed_graph_date", lambda _market: date(2026, 7, 1))
    monkeypatch.setattr(bq, "_fetch_seed_graph_term_rows", fake_rows)
    monkeypatch.setattr(bq, "_fetch_bridge_creators_for_keyword", lambda *a, **k: [])
    monkeypatch.setattr(bq, "fetch_lexicon_rows", lambda *a, **k: [])

    bq.fetch_seed_graph_adjacency("amapiano", "za")
    bq.fetch_seed_graph_adjacency("amapiano", "za")
    assert n["v"] == 1


def test_fetch_seed_path_builds_trail(monkeypatch):
    bq._seed_graph_cache.clear()

    def fake_run(sql, params=None):
        assert "v_seed_first_seen" in sql
        return [
            {
                "term": "amapiano",
                "term_type": "token",
                "platform": "tiktok",
                "row_count": 120,
                "first_seen_event_date": date(2026, 6, 20),
                "first_seen_ingest_date": date(2026, 6, 25),
            },
            {
                "term": "amapiano",
                "term_type": "token",
                "platform": "instagram",
                "row_count": 80,
                "first_seen_event_date": date(2026, 6, 28),
                "first_seen_ingest_date": date(2026, 6, 28),
            },
        ]

    monkeypatch.setattr(bq, "_latest_seed_graph_date", lambda _market: date(2026, 7, 1))
    monkeypatch.setattr(bq, "_run_query", fake_run)

    out = bq.fetch_seed_path("amapiano", "za")
    assert out["term"] == "amapiano"
    assert len(out["channels"]) == 2
    assert out["channels"][0]["platform"] == "TikTok"
    assert out["span_days"] == 8
    assert out["confidence"] == "measured"


def test_normalize_seed_keyword_strips_hash():
    assert bq._normalize_seed_keyword("#amapiano") == "amapiano"


def test_latest_seed_graph_date_is_cached(monkeypatch):
    """Repeated lookups for one market reuse its latest-date result."""
    bq._seed_graph_cache.clear()
    bq._latest_seed_graph_date_cache.clear()
    n = {"v": 0}

    def fake_run(sql, params=None):
        n["v"] += 1
        return [{"d": date(2026, 7, 1)}]

    monkeypatch.setattr(bq, "_run_query", fake_run)
    assert bq._latest_seed_graph_date("za") == date(2026, 7, 1)
    assert bq._latest_seed_graph_date("za") == date(2026, 7, 1)
    assert n["v"] == 1


def test_adjacency_runs_bridge_creators_concurrently(monkeypatch):
    """fetch_seed_graph_adjacency fires four BigQuery reads, and the heaviest of
    them (bridge creators, a 30-day regex scan of enriched_content) depends on
    nothing but the keyword and market. Serialising it behind the date lookup and
    the term rows puts it on the critical path for no reason.

    The term-row read here blocks until the bridge-creator read has started, so a
    serial implementation cannot pass."""
    import threading

    bq._seed_graph_cache.clear()
    bridge_started = threading.Event()

    def fake_rows(market, trend_date, terms):
        assert bridge_started.wait(timeout=5), (
            "bridge creators did not start concurrently"
        )
        return _seed_rows()

    def fake_bridge(*a, **k):
        bridge_started.set()
        return []

    monkeypatch.setattr(bq, "_latest_seed_graph_date", lambda _market: date(2026, 7, 1))
    monkeypatch.setattr(bq, "_fetch_seed_graph_term_rows", fake_rows)
    monkeypatch.setattr(bq, "_fetch_bridge_creators_for_keyword", fake_bridge)
    monkeypatch.setattr(bq, "fetch_lexicon_rows", lambda *a, **k: [])

    out = bq.fetch_seed_graph_adjacency("amapiano", "za")
    assert out["found"] is True
    assert out["bridge_creators"] == []
