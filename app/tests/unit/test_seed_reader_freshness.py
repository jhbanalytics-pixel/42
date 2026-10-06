from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api import bq, main


@pytest.fixture(autouse=True)
def clean_read_caches(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    main._market_cache.clear()
    bq._seed_graph_cache.clear()
    bq._latest_seed_graph_date_cache.clear()
    yield
    main._market_cache.clear()
    bq._seed_graph_cache.clear()
    bq._latest_seed_graph_date_cache.clear()


def test_empty_seeds_recovers_after_population_and_then_caches(monkeypatch):
    rows = []
    calls = []

    def read():
        calls.append(True)
        return list(rows)

    monkeypatch.setattr(bq, "fetch_seed_insights", read)
    client = TestClient(main.app)
    assert client.get("/api/seeds").json() == {"date": None, "seeds": []}
    rows.append(
        {
            "trend_date": date.today(),
            "rank": 1,
            "behaviour": "Shared repair",
            "markets": ["za"],
        }
    )
    populated = client.get("/api/seeds")
    assert populated.status_code == 200
    assert populated.json()["seeds"][0]["behaviour"] == "Shared repair"
    assert client.get("/api/seeds").json() == populated.json()
    assert len(calls) == 2


def test_graph_uses_each_markets_latest_nonfuture_snapshot(monkeypatch):
    today = date.today()
    snapshots = [
        ("za", today - timedelta(days=2)),
        ("ng", today),
        ("za", today + timedelta(days=1)),
    ]
    latest_reads = []

    def read(sql, params=None):
        values = {key: value for key, _kind, value in params or []}
        if "MAX(trend_date)" in sql:
            latest_reads.append((sql, values))
            candidates = snapshots
            if "market = @market" in sql:
                candidates = [
                    (market, stamp)
                    for market, stamp in candidates
                    if market == values["market"]
                ]
            if "trend_date <= CURRENT_DATE()" in sql:
                candidates = [
                    (market, stamp) for market, stamp in candidates if stamp <= today
                ]
            return [{"d": max(stamp for _, stamp in candidates)}]
        expected = today - timedelta(days=2) if values["market"] == "za" else today
        if values["d"] != expected:
            return []
        return [
            {
                "term": "repair",
                "term_type": "token",
                "platform": "reddit",
                "row_count": 3,
                "first_seen_event_date": expected,
            }
        ]

    monkeypatch.setattr(bq, "_run_query", read)
    za = bq.fetch_seed_path("repair", "za")
    ng = bq.fetch_seed_path("repair", "ng")
    assert za["trend_date"] == (today - timedelta(days=2)).isoformat()
    assert ng["trend_date"] == today.isoformat()
    assert len(za["channels"]) == len(ng["channels"]) == 1
    bq.fetch_seed_path("repair", "za")
    bq.fetch_seed_path("repair", "ng")
    assert len(latest_reads) == 2
    assert {values["market"] for _, values in latest_reads} == {"za", "ng"}
    assert all(
        "DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)" in sql for sql, _ in latest_reads
    )


@pytest.mark.parametrize("reader", [bq.fetch_seed_path, bq.fetch_seed_graph_adjacency])
@pytest.mark.parametrize("initial_date", [None, date(2026, 9, 6)])
def test_empty_graph_read_recovers_without_manual_cache_clear(
    monkeypatch, reader, initial_date
):
    state = {"date": initial_date, "rows": []}
    latest_calls = []

    def latest(*_args):
        latest_calls.append(True)
        return state["date"]

    monkeypatch.setattr(bq, "_latest_seed_graph_date", latest)
    monkeypatch.setattr(bq, "_run_query", lambda *_args, **_kwargs: list(state["rows"]))
    monkeypatch.setattr(
        bq, "_fetch_bridge_creators_for_keyword", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(bq, "fetch_lexicon_rows", lambda *_args, **_kwargs: [])
    empty = reader("repair", "za")
    assert not empty.get("channels", empty.get("found"))
    state["date"] = date.today()
    state["rows"] = [
        {
            "term": "repair",
            "term_type": "token",
            "platform": "reddit",
            "row_count": 3,
            "first_seen_event_date": date.today(),
            "topic_groups": ["economy_repair"],
            "co_occur_terms": ["mend"],
            "near_topics": [],
        }
    ]
    populated = reader("repair", "za")
    assert populated.get("channels", populated.get("found"))
    assert populated["trend_date"] == date.today().isoformat()
    assert reader("repair", "za") is populated
    assert len(latest_calls) == 2


def test_empty_latest_date_is_not_shared_or_cached(monkeypatch):
    values = [None, date.today()]

    def read(_sql, _params=None):
        return [{"d": values.pop(0)}]

    monkeypatch.setattr(bq, "_run_query", read)
    assert bq._latest_seed_graph_date("za") is None
    assert bq._latest_seed_graph_date("za") == date.today()
    assert bq._latest_seed_graph_date("za") == date.today()
    assert not values


@pytest.mark.parametrize("reader", [bq.fetch_seed_path, bq.fetch_seed_graph_adjacency])
def test_empty_result_does_not_pin_an_older_snapshot_date(monkeypatch, reader):
    state = {"date": date.today() - timedelta(days=1), "populated": False}
    latest_reads = []

    def read(sql, params=None):
        if "MAX(trend_date)" in sql:
            latest_reads.append(True)
            return [{"d": state["date"]}]
        values = {key: value for key, _kind, value in params or []}
        if not state["populated"] or values["d"] != state["date"]:
            return []
        return [
            {
                "term": "repair",
                "term_type": "token",
                "platform": "reddit",
                "row_count": 3,
                "first_seen_event_date": state["date"],
                "topic_groups": ["economy_repair"],
                "co_occur_terms": [],
                "near_topics": [],
            }
        ]

    monkeypatch.setattr(bq, "_run_query", read)
    monkeypatch.setattr(
        bq, "_fetch_bridge_creators_for_keyword", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(bq, "fetch_lexicon_rows", lambda *_args, **_kwargs: [])
    initial = reader("repair", "za")
    assert not initial.get("channels", initial.get("found"))
    state.update(date=date.today(), populated=True)
    populated = reader("repair", "za")
    assert populated["trend_date"] == date.today().isoformat()
    assert populated.get("channels", populated.get("found"))
    assert reader("repair", "za") is populated
    assert len(latest_reads) == 2
