"""Backend tests. BigQuery and Vertex are mocked throughout: no network, no
credentials, no live clients."""

import json
import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from src.api import bq, creator, desk, main, synth, topic

client = TestClient(main.app)


def test_valid_origin_rejects_unsafe_overrides():
    # Credentials are enabled, so wildcard and non-TLS origins must be rejected
    # and a trailing slash normalised before the allow-list is built.
    assert main._valid_origin("https://www.ogilvy.co.za") is True
    assert main._valid_origin("https://www.ogilvy.co.za/") is True
    assert main._valid_origin("http://localhost:5173") is True
    assert main._valid_origin("*") is False
    assert main._valid_origin("http://evil.example") is False
    assert main._valid_origin("") is False


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    # An empty CACHE_BUCKET turns the GCS cache layer off; the GCS tests
    # below substitute a fake bucket explicitly.
    monkeypatch.setenv("CACHE_BUCKET", "")
    synth._CACHE.clear()
    main._ask_hits.clear()
    main._market_cache.clear()
    bq._HASHTAG_CACHE.clear()
    bq._seed_graph_cache.clear()
    with main._brief_jobs_lock:
        main._brief_jobs.clear()
    with main._generation_capacity_lock:
        main._generation_active = 0
    yield
    _wait_for_brief_jobs()
    _wait_for_chat_jobs()
    synth._CACHE.clear()
    main._ask_hits.clear()
    main._market_cache.clear()
    bq._HASHTAG_CACHE.clear()
    bq._seed_graph_cache.clear()


def _wait_for_brief_jobs(timeout: float = 5.0) -> None:
    """Background brief threads must finish inside the test that started them,
    while its monkeypatched model is still in place."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with main._brief_jobs_lock:
            if not main._brief_jobs:
                return
        time.sleep(0.01)
    raise AssertionError("a brief job never finished")


def _wait_for_chat_jobs(timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with main._chat_jobs_lock:
            if not main._chat_jobs:
                return
        time.sleep(0.01)
    raise AssertionError("a chat job never finished")


def _ask_brief_and_wait(payload: dict):
    """POST a cold ask, assert the immediate 202, then poll the status
    endpoint to the resolved response, the way the client does."""
    r = client.post("/api/ask/brief", json=payload)
    if r.status_code != 202:
        return r
    assert r.json() == {"pending": True}
    deadline = time.time() + 5.0
    params = {"query": payload["query"], "region": payload["region"]}
    while time.time() < deadline:
        s = client.get("/api/ask/brief/status", params=params)
        assert s.status_code == 200
        if not s.json().get("pending"):
            return s
        time.sleep(0.01)
    raise AssertionError("the brief never resolved through the status endpoint")


def _today():
    return datetime.now(UTC).date()


FULL_RENDER_PAYLOAD = json.dumps(
    {
        "display": {
            "state": {"badge": "SUSTAINED", "direction": "up"},
            "phase": "Peak",
            "window": "7d",
            "in_market_pct": 82,
            "channels": [["tiktok", 40], ["instagram", 25]],
            "confidence": "High",
            "search": "Search interest is climbing",
        },
        "comment_sentiment": "Warm",
        "comment_themes": ["pride", "humor"],
        "driving_hashtags": [{"tag": "#amapiano", "share_pct": 41.5, "mood": "hype"}],
    }
)


def _trend_row(market, topic, score, render_payload):
    return {
        "market": market,
        "query_group": topic,
        "headline": "Headline for " + topic,
        "trend_synthesis": "Synthesis for " + topic,
        "cultural_context": "Context for " + topic,
        "status_tag": "SUSTAINED",
        "trend_score": score,
        "platforms": ["tiktok"],
        "top_creators": ["@kabza | tiktok | 9 mentions"],
        "social_refs": [
            "https://example.com/ref | tiktok | A big post",
            "https://example.com/bare",
            "no url in this entry",
        ],
        "visual_anchor": "log drum",
        "nano_banana_prompt": "image prompt",
        "lyria_prompt": "audio prompt",
        "sentiment_summary": "warm",
        "b24_sentiment_trajectory": "steady",
        "render_payload": render_payload,
    }


TODAY_TREND_ROWS = [
    _trend_row("za", "music_amapiano", 0.41, FULL_RENDER_PAYLOAD),
    _trend_row(
        "ng", "economy_sapa_hustle", 0.77, json.dumps({"comment_sentiment": "Mixed"})
    ),
    _trend_row("ke", "music_gengetone", 0.10, None),
]

VERDICT_ROW = {
    "through_line": "Music carries the week",
    "summary_text": "The week reads loud.",
    "call_to_action": "Lean into the log drum",
    "key_topics": ["music_amapiano"],
    "rising_topics": ["music_gengetone"],
}


def _today_dispatcher(pipeline_stamp, fallback_stamp=None):
    def fake_run_query(sql, params=None):
        if "trend_analysis" in sql and "MAX(trend_date)" in sql:
            return [{"d": _today()}]
        if "trend_analysis" in sql:
            return list(TODAY_TREND_ROWS)
        if "REGEXP_EXTRACT_ALL" in sql:
            return [
                {
                    "market": "za",
                    "topic": "music_amapiano",
                    "tag": "#amapiano",
                    "n": 50,
                },
                {
                    "market": "ng",
                    "topic": "economy_sapa_hustle",
                    "tag": "#sapa",
                    "n": 12,
                },
                # NG tag on a topic the KE brief shares: must never leak into KE.
                {"market": "ng", "topic": "music_gengetone", "tag": "#naija", "n": 5},
            ]
        if "daily_summary" in sql:
            return [{**VERDICT_ROW, "trend_date": _today()}]
        if "pipeline_runs" in sql:
            return [{"stamp": pipeline_stamp}]
        if "enriched_content" in sql and "MAX(collected_at)" in sql:
            return [{"stamp": fallback_stamp}]
        raise AssertionError("unexpected sql: " + sql)

    return fake_run_query


def test_today_payload_assembly(monkeypatch):
    stamp = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(bq, "_run_query", _today_dispatcher(stamp))
    r = client.get("/api/today?market=all")
    assert r.status_code == 200
    data = r.json()

    assert data["date"] == _today().isoformat()
    assert data["freshness"]["status"] == "green"
    assert 0.9 < data["freshness"]["age_hours"] < 1.1

    assert data["verdict"]["through_line"] == "Music carries the week"
    # Topic chips carry the raw searchable key plus the humanized label.
    assert data["verdict"]["rising_topics"] == [
        {"query": "music_gengetone", "label": "Gengetone"}
    ]
    assert data["verdict"]["key_topics"] == [
        {"query": "music_amapiano", "label": "Amapiano"}
    ]
    assert data["verdict"]["verdict_date"] == _today().isoformat()

    briefs = data["briefs"]
    assert [b["trend_score"] for b in briefs] == [0.77, 0.41, 0.10]
    assert briefs[0]["market_label"] == "Nigeria"
    # social_refs are structured for the client; the url-less entry is dropped.
    assert briefs[0]["social_refs"] == [
        {"url": "https://example.com/ref", "platform": "tiktok", "title": "A big post"},
        {"url": "https://example.com/bare", "platform": None, "title": None},
    ]

    full = next(b for b in briefs if b["topic"] == "music_amapiano")
    assert full["display"]["state"]["badge"] == "SUSTAINED"
    assert full["display"]["in_market_pct"] == 82
    assert full["display"]["channels"] == [["tiktok", 40], ["instagram", 25]]
    assert full["comment_sentiment"] == "Warm"
    assert full["driving_hashtags"][0]["share_pct"] == 41.5

    partial = next(b for b in briefs if b["topic"] == "economy_sapa_hustle")
    assert partial["display"] is None
    assert partial["comment_sentiment"] == "Mixed"
    assert partial["comment_themes"] == []
    # The NG brief backfills from the NG-scoped derived tags.
    assert [t["tag"] for t in partial["driving_hashtags"]] == ["#sapa"]

    absent = next(b for b in briefs if b["topic"] == "music_gengetone")
    assert absent["display"] is None
    # The KE brief never inherits the NG tag for the same topic key.
    assert absent["driving_hashtags"] == []


def test_freshness_falls_back_to_collected_at(monkeypatch):
    fallback = datetime.now(UTC) - timedelta(hours=5)
    monkeypatch.setattr(bq, "_run_query", _today_dispatcher(None, fallback))
    r = client.get("/api/today")
    assert r.status_code == 200
    fresh = r.json()["freshness"]
    assert fresh["status"] == "amber"
    assert 4.9 < fresh["age_hours"] < 5.1


def test_today_caches_heavy_assembly(monkeypatch):
    # Second request for the same day's data must not re-run the heavy briefs
    # query; it is served from the in-process cache. Freshness stays live.
    stamp = datetime.now(UTC) - timedelta(hours=1)
    base = _today_dispatcher(stamp)
    calls = {"briefs": 0}

    def counting(sql, params=None):
        if "trend_analysis" in sql and "MAX(trend_date)" not in sql:
            calls["briefs"] += 1
        return base(sql, params)

    monkeypatch.setattr(bq, "_run_query", counting)
    first = client.get("/api/today?market=all").json()
    second = client.get("/api/today?market=all").json()
    assert first["briefs"] == second["briefs"]
    assert first["verdict"] == second["verdict"]
    assert calls["briefs"] == 1
    assert second["freshness"]["status"] == "green"


def test_growth_metrics_are_cumulative(monkeypatch):
    def fake_run_query(sql, params=None):
        if "pipeline_runs" in sql:
            return [{"posts": 431726, "briefs": 1001, "trends": 880}]
        if "enriched_content" in sql:
            return [{"creators": 5234, "platforms": 9, "markets": 3}]
        raise AssertionError("unexpected sql: " + sql)

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    out = bq.fetch_growth_metrics()
    assert out == {
        "posts": 431726,
        "briefs": 1001,
        "trends": 880,
        "creators": 5234,
        "platforms": 9,
        "markets": 3,
    }


def test_fetch_ingest_cache_version(monkeypatch):
    captured = {}

    def fake_run_query(sql, params=None):
        captured["sql"] = sql
        return [{"m": "1710000000", "voice_plat": 10, "tw": 42}]

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    assert bq.fetch_ingest_cache_version() == "1710000000:10:42"
    assert "voice_plat" in captured["sql"]
    assert "twitter" in captured["sql"]


def test_metrics_cache_busts_when_voice_platform_count_changes(monkeypatch):
    calls = {"n": 0}

    def fake_metrics():
        calls["n"] += 1
        return {"platforms": 9 if calls["n"] == 1 else 10}

    keys = iter(["ts:9:0", "ts:10:5"])
    monkeypatch.setattr(main, "_ingest_cache_key", lambda: next(keys))
    monkeypatch.setattr(bq, "fetch_growth_metrics", fake_metrics)
    main._market_cache.clear()
    assert client.get("/api/metrics").json()["platforms"] == 9
    assert client.get("/api/metrics").json()["platforms"] == 10
    assert calls["n"] == 2


def test_fetch_top_authors_ranks_real_creators(monkeypatch):
    rows = [
        # Reddit's bot: highly active but not an influencer, dropped.
        {
            "handle": "AutoModerator",
            "platform": "reddit",
            "mentions": 300,
            "reach": 900,
            "topic_bag": ["music_amapiano"],
        },
        # Anonymous numeric id: masks to a generic label, dropped.
        {
            "handle": "12345678",
            "platform": "tiktok",
            "mentions": 20,
            "reach": 5_000_000,
            "topic_bag": [],
        },
        # A real creator: kept, the @ stripped, top two topics mapped to labels.
        {
            "handle": "@kasi_swenka",
            "platform": "tiktok",
            "mentions": 144,
            "reach": 67_000_000,
            "topic_bag": [
                "music_amapiano",
                "music_amapiano",
                "genz_lifestyle",
                "sports_rugby",
            ],
        },
    ]
    captured = {}

    def _cap(sql, params=None):
        captured["sql"] = sql
        return rows

    monkeypatch.setattr(bq, "_run_query", _cap)
    out = bq.fetch_top_authors("za", limit=10)
    assert [a["name"] for a in out] == ["kasi_swenka"]
    # Gates on the handle's home (dominant) market, so a creator whose posts are
    # mostly in another market is kept off this market's board.
    assert "WITH home AS" in captured["sql"] and "mrank = 1" in captured["sql"]
    a = out[0]
    assert a["platform"] == bq._platform_label("tiktok")
    assert a["reach"] == 67_000_000
    assert a["mentions"] == 144
    assert a["topics"] == [
        bq.topic_label("music_amapiano"),
        bq.topic_label("genz_lifestyle"),
    ]
    # An unknown market returns nothing and never touches BigQuery.
    assert bq.fetch_top_authors("xx") == []


def test_metrics_endpoint(monkeypatch):
    monkeypatch.setattr(
        bq,
        "fetch_growth_metrics",
        lambda: {
            "posts": 10,
            "briefs": 2,
            "trends": 3,
            "creators": 4,
            "platforms": 5,
            "markets": 3,
        },
    )
    r = client.get("/api/metrics")
    assert r.status_code == 200
    assert r.json() == {
        "posts": 10,
        "briefs": 2,
        "trends": 3,
        "creators": 4,
        "platforms": 5,
        "markets": 3,
    }


def test_intel_mentions_endpoint(monkeypatch):
    captured = {}

    def fake_feed(market, sentiment=None, category=None, limit=30):
        captured.update(market=market, sentiment=sentiment, category=category)
        return {
            "results": [{"host": "h", "sentiment": "positive"}],
            "cursor": None,
            "has_more": False,
        }

    monkeypatch.setattr(bq, "listen_feed", fake_feed)
    r = client.get("/api/intel/mentions?market=ng&sentiment=positive&category=tiktok")
    assert r.status_code == 200
    assert r.json()["results"][0]["host"] == "h"
    assert captured == {"market": "ng", "sentiment": "positive", "category": "tiktok"}


def test_intel_mentions_rejects_bad_market():
    assert client.get("/api/intel/mentions?market=xx").status_code == 400


def test_intel_mentions_all_market(monkeypatch):
    monkeypatch.setattr(
        bq,
        "listen_feed",
        lambda market, **k: {"results": [], "cursor": None, "has_more": False},
    )
    assert client.get("/api/intel/mentions?market=all").status_code == 200


def test_parse_render_payload_edge_cases():
    out = bq.parse_render_payload("not json at all")
    assert out == {
        "display": None,
        "comment_sentiment": None,
        "comment_themes": [],
        "driving_hashtags": [],
        "forecast_outlook": None,
    }
    assert bq.parse_render_payload(None)["display"] is None
    partial = bq.parse_render_payload(json.dumps({"display": {"phase": "Early"}}))
    assert partial["display"]["phase"] == "Early"
    assert partial["display"]["state"] is None
    assert partial["display"]["in_market_pct"] is None
    assert partial["display"]["channels"] == []


def test_parse_render_payload_forecast_outlook():
    # Absent on every row today (FORECAST_ENABLED off): no chip data.
    assert (
        bq.parse_render_payload(json.dumps({"display": {}}))["forecast_outlook"] is None
    )
    # The three values the engine emits, normalised to lower-case.
    assert (
        bq.parse_render_payload(json.dumps({"forecast_outlook": "heating"}))[
            "forecast_outlook"
        ]
        == "heating"
    )
    assert (
        bq.parse_render_payload(json.dumps({"forecast_outlook": "COOLING"}))[
            "forecast_outlook"
        ]
        == "cooling"
    )
    assert (
        bq.parse_render_payload(json.dumps({"forecast_outlook": " steady "}))[
            "forecast_outlook"
        ]
        == "steady"
    )
    # Empty, unknown, or non-string collapse to None so the chip no-ops.
    assert (
        bq.parse_render_payload(json.dumps({"forecast_outlook": ""}))[
            "forecast_outlook"
        ]
        is None
    )
    assert (
        bq.parse_render_payload(json.dumps({"forecast_outlook": "exploding"}))[
            "forecast_outlook"
        ]
        is None
    )
    assert (
        bq.parse_render_payload(json.dumps({"forecast_outlook": 5}))["forecast_outlook"]
        is None
    )


def test_clean_momentum_label_whitelist():
    # The four engine values, normalised to lower-case.
    assert bq.clean_momentum_label("rising") == "rising"
    assert bq.clean_momentum_label("BUILDING") == "building"
    assert bq.clean_momentum_label(" Cooling ") == "cooling"
    # Absent, empty, unknown or non-string collapse to None so the pill falls
    # back to the velocity read.
    assert bq.clean_momentum_label(None) is None
    assert bq.clean_momentum_label("") is None
    assert bq.clean_momentum_label("surging") is None
    assert bq.clean_momentum_label(7) is None


def test_momentum_label_from_windows_bands():
    # 7-day clearly leading the 30-day reads rising; trailing reads cooling.
    assert bq.momentum_label_from_windows(0.30, 0.10) == "rising"
    assert bq.momentum_label_from_windows(0.05, 0.40) == "cooling"
    # Positive but flat between the windows reads building; flat-low reads steady.
    assert bq.momentum_label_from_windows(0.20, 0.20) == "building"
    assert bq.momentum_label_from_windows(0.01, 0.01) == "steady"
    # Either window missing yields None so nothing is invented.
    assert bq.momentum_label_from_windows(None, 0.10) is None
    assert bq.momentum_label_from_windows(0.10, None) is None
    assert bq.momentum_label_from_windows("x", 0.10) is None


def test_clean_lifecycle_and_continuity_whitelist():
    assert bq.clean_lifecycle_phase("growth") == "growth"
    assert bq.clean_lifecycle_phase("DECLINE") == "decline"
    assert bq.clean_lifecycle_phase(None) is None
    assert bq.clean_lifecycle_phase("plateau") is None
    assert bq.clean_lifecycle_phase(3) is None
    assert bq.clean_continuity_state("new") == "new"
    assert bq.clean_continuity_state("Day3Plus") == "day3plus"
    assert bq.clean_continuity_state("rebounding") == "rebounding"
    assert bq.clean_continuity_state(None) is None
    assert bq.clean_continuity_state("day4") is None
    assert bq.clean_continuity_state(2) is None


def test_build_platform_heat_aggregates_and_guards():
    topics = [
        {"platforms": [["TikTok", 40], ["Instagram", 25]]},
        {"platforms": [["TikTok", 10], ["YouTube", 5]]},
    ]
    heat = desk.build_platform_heat(topics)
    # TikTok leads at 50 of 80 total weight; rows are sorted by weight.
    assert [r["platform"] for r in heat] == ["TikTok", "Instagram", "YouTube"]
    assert heat[0] == {"platform": "TikTok", "weight": 50.0, "share": 62.5}
    assert sum(r["weight"] for r in heat) == 80.0
    # No platform breakdown anywhere yields an empty rollup so the panel hides.
    assert desk.build_platform_heat([{"platforms": []}, {}]) == []
    assert desk.build_platform_heat([]) == []
    # Malformed pairs and non-positive weights are skipped, never crash.
    messy = [
        {"platforms": [["TikTok"], ["Instagram", 0], ["YouTube", -3], ["Reddit", 4]]}
    ]
    assert desk.build_platform_heat(messy) == [
        {"platform": "Reddit", "weight": 4.0, "share": 100.0}
    ]


def test_rails_momentum_and_read(monkeypatch):
    long_read = " ".join(["signalword"] * 30)
    queries = []

    def rail(topic, score, velocity, read="short read"):
        return {
            "topic_group": topic,
            "market": "za",
            "market_label": "South Africa",
            "headline": "h " + topic,
            "tier": "Trending",
            "trend_score": score,
            "velocity_score": velocity,
            "description_rationale": read,
        }

    rows = [
        rail("rising_topic", 0.55, 0.12, long_read),
        rail("building_topic", 0.22, 0.15),
        rail("cooling_topic", 0.50, -0.20),
        rail("steady_topic", 0.50, 0.00),
    ]

    def fake_run_query(sql, params=None):
        queries.append((sql, params or []))
        if "trend_analysis" in sql and "MAX(trend_date)" in sql:
            return [{"d": _today()}]
        if "trend_analysis" in sql and "trend_scores" in sql:
            return rows
        raise AssertionError("unexpected sql: " + sql)

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    r = client.get("/api/rails?market=za")
    assert r.status_code == 200
    rails = {x["topic"]: x for x in r.json()["rails"]}
    assert rails["rising_topic"]["momentum"] == "Rising"
    assert rails["building_topic"]["momentum"] == "Building"
    assert rails["cooling_topic"]["momentum"] == "Cooling"
    assert rails["steady_topic"]["momentum"] == "Steady"
    read = rails["rising_topic"]["read"]
    assert read.endswith("...")
    assert len(read) <= 144
    assert "signalwo..." not in read
    assert len(queries) == 2
    latest_sql, latest_params = queries[0]
    rails_sql, rails_params = queries[1]
    assert "MAX(trend_date) AS d" in latest_sql
    assert "trend_analysis" in latest_sql
    assert "trend_scores" not in latest_sql
    assert latest_params == []
    required_rails_sql = (
        "a.query_group AS topic_group",
        "LOWER(a.market) AS market",
        "a.trend_score",
        "s.velocity_score",
        "a.trend_synthesis AS description_rationale",
        "s.trend_date = a.trend_date",
        "LOWER(s.market) = LOWER(a.market)",
        "s.query_group = a.query_group",
        "a.trend_date = @d",
        "(@market = 'all' OR LOWER(a.market) = @market)",
        "a.trend_score >= 0.45",
        "a.trend_score >= 0.30",
        "a.trend_score >= 0.18",
        "ORDER BY a.trend_score DESC, a.query_group ASC",
    )
    assert all(fragment in rails_sql for fragment in required_rails_sql)
    assert rails_params == [("d", "DATE", _today()), ("market", "STRING", "za")]
    joined = "\n".join(sql for sql, _params in queries)
    assert "v_trend_briefs" not in joined
    assert "genz_score" not in joined
    assert "b24_sentiment_trajectory" not in joined


def test_momentum_from_velocity_unit():
    assert bq.momentum_from_velocity(0.12, 0.55) == "Rising"
    assert bq.momentum_from_velocity(0.15, 0.22) == "Building"
    assert bq.momentum_from_velocity(-0.20, 0.50) == "Cooling"
    assert bq.momentum_from_velocity(0.00, 0.50) == "Steady"


def test_compute_momentum_all_labels():
    assert synth.compute_momentum([0] * 9 + [8] * 14 + [12] * 7) == "Rising"
    assert synth.compute_momentum([0] * 9 + [8] * 14 + [4] * 7) == "Cooling"
    assert synth.compute_momentum([0] * 9 + [8] * 14 + [9] * 7) == "Steady"
    assert synth.compute_momentum([0] * 9 + [2] * 14 + [6] * 7) == "Building"
    assert synth.compute_momentum([0] * 30) == "Steady"


def test_mask_handle():
    assert bq.mask_handle("@coolhandle", "tiktok") == "@coolhandle"
    assert bq.mask_handle("12345678", "tiktok") == "TikTok creator"
    assert bq.mask_handle("x" * 25, "instagram") == "Instagram creator"
    assert bq.mask_handle("[deleted]", "reddit") == "Reddit creator"
    assert bq.mask_handle("spam_bot", "youtube") == "YouTube creator"
    assert bq.mask_handle("", "threads") == "Threads creator"


def test_clean_items_dedupe_foreign_and_urls():
    items = [
        {
            "id": "1",
            "text": "Amapiano is taking over the timeline",
            "platform": "tiktok",
            "market": "za",
            "handle": "user1",
            "engagement": 100,
            "slang_terms": "",
        },
        {
            "id": "2",
            "text": "Amapiano is taking over the timeline!!",
            "platform": "tiktok",
            "market": "za",
            "handle": "user2",
            "engagement": 90,
            "slang_terms": "",
        },
        {
            "id": "3",
            "text": "Você não sabe que isso é muito bom",
            "platform": "instagram",
            "market": "ng",
            "handle": "user3",
            "engagement": 80,
            "slang_terms": "",
        },
        {
            "id": "4",
            "text": "Check this https://example.com/x out",
            "platform": "youtube",
            "market": "ke",
            "handle": "user4",
            "engagement": 70,
            "slang_terms": "",
        },
    ]
    cleaned = bq.clean_items(items)
    ids = [c["id"] for c in cleaned]
    assert ids == ["1", "4"]
    assert "https" not in cleaned[1]["text"]
    assert cleaned[1]["text"] == "Check this out"


def test_clean_items_drops_stale_posts():
    now = datetime.now(UTC)
    items = [
        {
            "id": "fresh",
            "text": "This amapiano set is the sound of the week right now",
            "platform": "tiktok",
            "market": "za",
            "handle": "freshvoice",
            "engagement": 50,
            "published_at": now - timedelta(days=2),
            "collected_at": now,
            "slang_terms": "",
        },
        {
            "id": "stale_viral",
            "text": "An old clip that keeps getting rescraped long after it first posted",
            "platform": "tiktok",
            "market": "za",
            "handle": "oldvoice",
            "engagement": 5_000_000,
            "published_at": now - timedelta(days=270),
            "collected_at": now,
            "slang_terms": "",
        },
        {
            "id": "stampless",
            "text": "No stamp here so the age cannot be proven stale at all",
            "platform": "instagram",
            "market": "za",
            "handle": "nostamp",
            "engagement": 10,
            "slang_terms": "",
        },
    ]
    ids = [c["id"] for c in bq.clean_items(items)]
    # The 270-day post is dropped despite the largest engagement; a stampless
    # row stays (its age is unknown), and the genuinely fresh post leads.
    assert "stale_viral" not in ids
    assert set(ids) == {"fresh", "stampless"}
    assert ids[0] == "fresh"


def test_voice_pool_ranks_by_relevance(monkeypatch):
    captured = []

    def fake_run_query(sql, params=None):
        captured.append(sql)
        return []

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    bq.fetch_voice_pools(["music_amapiano"], "za")
    # The wall ranks by a composite relevance score (topic focus, regional
    # and slang signals, recency, platform credibility), not raw engagement, and
    # collapses duplicate text to one post before the top-12 cut.
    assert captured
    sql = captured[0]
    assert "genz_score" not in sql
    assert "regional_score" in sql
    assert "slang_score" in sql
    assert "ARRAY_LENGTH(topic_groups)" in sql
    assert "dup_rn" in sql
    assert "rn <= 12" in sql


@pytest.mark.parametrize("topic_fit", [None, "0.8"])
def test_general_relevance_has_no_implicit_age_score(topic_fit):
    expression = bq._relevance_sql(topic_fit)
    assert "genz_score" not in expression
    assert "regional_score" in expression
    assert "slang_score" in expression
    assert "engagement_total" in expression
    assert "TIMESTAMP_DIFF" in expression


def test_truncate_words_boundary():
    text = "alpha bravo charlie delta echo foxtrot"
    out = bq.truncate_words(text, 20)
    assert out == "alpha bravo charlie..."
    assert bq.truncate_words("short", 20) == "short"


def test_normalize_query_synonyms():
    assert bq.normalize_query("  Soccer Fans  ") == "football fans"
    assert bq.normalize_query("Amapiano") == "amapiano"


def _ask_item(i, **overrides):
    item = {
        "id": "id" + str(i),
        "title": "",
        "text": f"unique post number {i} about street football culture",
        "platform": "tiktok",
        "market": "za",
        "handle": "user" + str(i),
        "engagement": 1000 - i,
        "slang_terms": "",
    }
    item.update(overrides)
    return item


def _rich_search_result():
    today = _today()
    daily = {}
    for offset in range(30):
        daily[today - timedelta(days=offset)] = 12 if offset < 7 else 8
    items = [_ask_item(i) for i in range(14)]
    items[0]["slang_terms"] = "eish"
    items[0]["text"] = "Eish this street football culture is moving fast"
    items.insert(3, _ask_item(90, handle="12345678", engagement=998))
    items.append(_ask_item(91, text="Você não sabe que isso é muito bom para você"))
    items.append(
        _ask_item(92, text="unique post number 0 about street football culture")
    )
    return {
        "total_matches": 300,
        "daily_counts": daily,
        "platform_split": [("tiktok", 200), ("instagram", 100)],
        "market_split": [("za", 220), ("ng", 80)],
        "creators": [("coolhandle", "tiktok", 9), ("12345678", "instagram", 5)],
        "items": items,
    }


VALID_MODEL_JSON = json.dumps(
    {
        "trends": [
            {
                "statement": "Street football is the week's social glue.",
                "evidence_indices": [0, 1, 2],
                "how_to_use": "Anchor content in pickup football moments.",
                "platforms": ["tiktok"],
                "markets": ["za"],
            }
        ],
        "slang": [
            {"term": "eish", "meaning": "an exclamation of frustration or surprise"},
            {"term": "fakeword", "meaning": "not present in the evidence"},
        ],
        "overall_read": "Football talk is loud and local. The energy sits with street creators.",
    }
)


@pytest.mark.parametrize("path", ["/api/ask?q=football&market=za", "/card/football?market=za"])
def test_archive_gets_never_start_model_calls(monkeypatch, path):
    calls = []
    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(synth, "_call_model", lambda *args: calls.append(args) or VALID_MODEL_JSON)
    response = client.get(path)
    assert response.status_code == 200
    assert calls == []
    if path.startswith("/api/"):
        assert set(response.json()) == {
            "query", "market", "total_matches", "thin", "broad", "volume_series",
            "platform_split", "market_split", "trends", "quotes", "slang", "creators",
            "overall_read", "limited_voice",
        }


def test_retrieval_only_card_has_no_empty_read_or_invented_processing_claim(monkeypatch):
    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    response = client.get("/card/football?market=za")
    assert response.status_code == 200
    assert '<div class="note"></div>' not in response.text
    assert "Retrieved evidence" in response.text
    assert "Momentum is computed" not in response.text
    assert "synthesis only restates" not in response.text
    assert "original-source links" in response.text
    assert "Rendered on" in response.text


@pytest.mark.parametrize("total", [None, True, -1, "unavailable"])
def test_card_does_not_turn_unknown_match_counts_into_zero(total):
    html = main.card_render.render_card({"query": "football", "market": "za", "total_matches": total}, datetime(2026, 9, 6, tzinfo=UTC))
    assert "Match count unavailable" in html
    assert "0 matches" not in html


def test_opportunity_get_never_starts_a_model_call(monkeypatch):
    calls = []
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", lambda *args: calls.append(args) or '{"opportunity":"Generated by a GET"}')
    response = client.get("/api/desk/opportunity/music_amapiano?region=za")
    assert response.status_code == 200
    assert response.json() == {"opportunity": ""}
    assert calls == []


def test_archive_cache_expiry_returns_evidence_without_regeneration(monkeypatch):
    key = ("football", "za")
    monkeypatch.setenv("CACHE_TTL", "3600")
    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    calls = []
    monkeypatch.setattr(synth, "_call_model", lambda *args: calls.append(args) or VALID_MODEL_JSON)
    synth._CACHE[key] = (time.time() - 3601, {"overall_read": "Expired interpretation"})
    response = client.get("/api/ask?q=football&market=za")
    assert response.status_code == 200
    assert response.json()["overall_read"] == ""
    assert response.json()["trends"] == []
    assert response.json()["total_matches"] == 300
    assert calls == []


def test_fresh_legacy_archive_cache_keeps_its_original_age_and_content(monkeypatch):
    key = ("football", "za")
    monkeypatch.setenv("CACHE_TTL", "3600")
    cached = {"query": "football", "market": "za", "thin": False, "trends": [], "overall_read": "Existing legacy read"}
    stored_at = time.time() - 600
    synth._CACHE[key] = (stored_at, cached)
    monkeypatch.setattr(bq, "search_content", lambda *args: pytest.fail("A fresh cached read must not retrieve again"))
    monkeypatch.setattr(synth, "_call_model", lambda *args: pytest.fail("A cached read must not generate"))
    response = client.get("/api/ask?q=football&market=za")
    assert response.status_code == 200
    assert response.json() == cached
    assert synth._CACHE[key][0] == stored_at
    assert "general_cultural_question_v1" not in response.text


def test_uncached_archive_read_never_reserves_or_releases_generation_capacity(monkeypatch):
    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(main, "_try_acquire_generation_capacity", lambda: pytest.fail("A read cannot reserve generation"))
    monkeypatch.setattr(main, "_release_generation_capacity", lambda: pytest.fail("A read cannot release another request's slot"))
    monkeypatch.setattr(synth, "_call_model", lambda *args: pytest.fail("A read cannot generate"))
    assert client.get("/api/ask?q=football&market=za").status_code == 200


@pytest.mark.parametrize("cache_value", ["expired", {"unexpected": "shape"}])
def test_opportunity_read_withholds_expired_or_malformed_cache_without_generation(monkeypatch, cache_value):
    monkeypatch.setenv("CACHE_TTL", "3600")
    monkeypatch.setattr(main, "_desk_cached", lambda region, loader: {"updated": "2026-09-06", "topics": [{"id": "music_amapiano", "region": "ZA"}]})
    key = ("opp", "za:music_amapiano", "2026-09-06")
    synth._CACHE[key] = (time.time() - (3601 if cache_value == "expired" else 1), cache_value)
    monkeypatch.setattr(synth, "_call_model", lambda *args: pytest.fail("An unavailable cache entry cannot generate"))
    response = client.get("/api/desk/opportunity/music_amapiano?region=za")
    assert response.status_code == 200
    assert response.json() == {"opportunity": ""}


def test_ask_rich_payload(monkeypatch):
    calls = []

    def fake_call_model(prompt):
        calls.append(prompt)
        return VALID_MODEL_JSON

    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(synth, "_call_model", fake_call_model)

    r = client.get("/api/ask?q=football&market=all")
    assert r.status_code == 200
    data = r.json()

    assert data["query"] == "football"
    assert data["thin"] is False
    assert data["total_matches"] == 300
    assert calls == []

    series = data["volume_series"]
    assert len(series) == 30
    assert series[-1]["n"] == 12
    assert series[0]["n"] == 8

    assert data["platform_split"] == [
        {"platform": "tiktok", "n": 200},
        {"platform": "instagram", "n": 100},
    ]
    assert data["market_split"][0] == {"market": "za", "n": 220}

    assert data["trends"] == []
    assert data["slang"] == []
    assert data["overall_read"] == ""

    # The numeric anon id "12345678" masks to a generic label, so it is dropped.
    # Only named accounts survive in "who is driving it".
    assert data["creators"] == [
        {"handle": "coolhandle", "mentions": 9},
    ]

    texts = [q["text"] for q in data["quotes"]]
    assert len(texts) == len(set(texts))
    assert len(data["quotes"]) <= 12
    masked = [q for q in data["quotes"] if q["handle"] == "TikTok creator"]
    assert masked
    assert data["limited_voice"] is False

    cached = client.get("/api/ask?q=football&market=all")
    assert cached.status_code == 200
    assert calls == []


def test_ask_thin_signal_skips_synthesis(monkeypatch):
    calls = []

    def fake_call_model(prompt):
        calls.append(prompt)
        return VALID_MODEL_JSON

    thin_result = _rich_search_result()
    thin_result["items"] = [_ask_item(i) for i in range(5)]
    thin_result["total_matches"] = 7
    monkeypatch.setattr(bq, "search_content", lambda q, m: thin_result)
    monkeypatch.setattr(synth, "_call_model", fake_call_model)

    r = client.get("/api/ask?q=quantum computing")
    assert r.status_code == 200
    data = r.json()
    assert data["thin"] is True
    assert data["trends"] == []
    assert data["slang"] == []
    assert data["overall_read"] == main.THIN_READ
    assert calls == []
    assert len(data["volume_series"]) == 30


def test_ask_read_never_attempts_synthesis_when_the_model_would_fail(monkeypatch):
    calls = []

    def fake_call_model(prompt):
        calls.append(prompt)
        return "this is not json"

    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(synth, "_call_model", fake_call_model)

    r = client.get("/api/ask?q=football")
    assert r.status_code == 200
    data = r.json()
    assert calls == []
    assert data["trends"] == []
    assert data["overall_read"] == ""
    assert data["thin"] is False
    assert data["total_matches"] == 300


def test_search_content_aggregation(monkeypatch):
    d1 = _today() - timedelta(days=1)
    d2 = _today() - timedelta(days=2)

    def fake_run_query(sql, params=None):
        assert params is not None
        if "GROUP BY day" in sql:
            return [
                {"day": d1, "platform": "tiktok", "market": "za", "n": 5},
                {"day": d1, "platform": "instagram", "market": "ng", "n": 3},
                {"day": d2, "platform": "tiktok", "market": "za", "n": 2},
            ]
        if "GROUP BY author_handle" in sql:
            return [{"author_handle": "cool", "platform": "tiktok", "n": 4}]
        if "LIMIT 150" in sql:
            return [
                {
                    "id": "a",
                    "title": "t",
                    "text": "x",
                    "platform": "tiktok",
                    "market": "za",
                    "author_handle": "cool",
                    "engagement": 9.0,
                    "collected_at": None,
                    "slang_terms": "eish",
                }
            ]
        raise AssertionError("unexpected sql: " + sql)

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    out = bq.search_content("amapiano", "all")
    assert out["total_matches"] == 10
    assert out["daily_counts"] == {d1: 8, d2: 2}
    assert out["platform_split"] == [("tiktok", 7), ("instagram", 3)]
    assert out["market_split"] == [("za", 7), ("ng", 3)]
    assert out["creators"] == [("cool", "tiktok", 4)]
    assert out["items"][0]["handle"] == "cool"
    assert out["broad"] is False


def test_query_tokens_drop_stopwords():
    assert bq.query_tokens("world cup opening ceremony") == [
        "world",
        "cup",
        "opening",
        "ceremony",
    ]
    assert bq.query_tokens("the rise of the amapiano") == ["rise", "amapiano"]
    # An all-stopword query keeps its tokens rather than matching nothing.
    assert bq.query_tokens("the and of") == ["the", "and", "of"]
    assert bq.query_tokens("amapiano") == ["amapiano"]


def test_search_content_single_token_single_param(monkeypatch):
    captured = []

    def fake_run_query(sql, params=None):
        captured.append((sql, list(params or [])))
        return []

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    out = bq.search_content("amapiano", "all")
    assert out["broad"] is False
    assert len(captured) == 3
    agg_sql, agg_params = captured[0]
    # Each token rides as two params now: a substring match for topic_groups and
    # a word-boundary match for free text.
    assert [p for p in agg_params if p[0] == "term0"] == [
        ("term0", "STRING", "amapiano")
    ]
    assert [p for p in agg_params if p[0] == "term0_wb"] == [
        ("term0_wb", "STRING", r"(^|\W)amapiano(\W|$)")
    ]
    assert "@term0" in agg_sql
    assert "@term0_wb" in agg_sql
    assert "@term1" not in agg_sql


def test_search_content_multiword_ands_all_tokens(monkeypatch):
    captured = []
    rows = [
        {
            "id": "i" + str(i),
            "title": "World Cup opening ceremony build-up number " + str(i),
            "text": "The opening ceremony of the world cup is loading, post " + str(i),
            "platform": "tiktok",
            "market": "za",
            "author_handle": "fan" + str(i),
            "engagement": 100 - i,
            "published_at": None,
            "collected_at": datetime.now(UTC),
            "slang_terms": "",
        }
        for i in range(20)
    ]

    def fake_run_query(sql, params=None):
        captured.append((sql, list(params or [])))
        if "LIMIT 150" in sql:
            return rows
        return []

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    out = bq.search_content("the world cup opening ceremony", "za")
    # Rich AND pass: no broadened retry, three queries only.
    assert out["broad"] is False
    assert len(captured) == 3

    agg_sql, agg_params = captured[0]
    term_params = [
        p for p in agg_params if p[0].startswith("term") and not p[0].endswith("_wb")
    ]
    # Stopword "the" dropped; each remaining token is its own parameter.
    assert [(p[0], p[2]) for p in term_params] == [
        ("term0", "world"),
        ("term1", "cup"),
        ("term2", "opening"),
        ("term3", "ceremony"),
    ]
    # Tokens are AND-ed, never a literal phrase.
    assert agg_sql.count("@term") >= 4
    assert ")) AND (REGEXP_CONTAINS" in agg_sql
    assert "world cup" not in agg_sql
    # Every query in the pass carries the same token parameters.
    for _sql, params in captured:
        assert [
            p[0] for p in params if p[0].startswith("term") and not p[0].endswith("_wb")
        ] == [
            "term0",
            "term1",
            "term2",
            "term3",
        ]


def test_search_content_thin_and_retries_broad_or(monkeypatch):
    captured = []

    def fake_run_query(sql, params=None):
        captured.append((sql, list(params or [])))
        return []

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    out = bq.search_content("world cup opening ceremony", "za")
    # Thin AND pass triggers exactly one broadened retry: six queries total.
    assert out["broad"] is True
    assert len(captured) == 6

    broad_sql, broad_params = captured[3]
    term_params = [
        p for p in broad_params if p[0].startswith("term") and not p[0].endswith("_wb")
    ]
    # The two longest tokens, OR-ed.
    assert sorted(p[2] for p in term_params) == ["ceremony", "opening"]
    assert ")) OR (REGEXP_CONTAINS" in broad_sql
    assert ")) AND (REGEXP_CONTAINS" not in broad_sql
    # Single-token queries never retry broad even when thin.
    captured.clear()
    out2 = bq.search_content("amapiano", "za")
    assert out2["broad"] is False
    assert len(captured) == 3


def test_search_content_multiword_geo_exclusion_survives(monkeypatch):
    captured = []

    def fake_run_query(sql, params=None):
        captured.append((sql, list(params or [])))
        return []

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    bq.search_content("sapa hustle", "ng")
    # Both the strict pass and the broadened retry carry the geo blocklist.
    assert len(captured) == 6
    for sql, params in captured:
        assert "NOT REGEXP_CONTAINS" in sql
        assert any(p[0] == "geo_pattern" for p in params)
    agg_params = captured[0][1]
    pattern = [p for p in agg_params if p[0] == "geo_pattern"][0][2]
    assert "vietnam" in pattern


def test_passcode_gate(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    monkeypatch.setattr(
        bq,
        "fetch_today",
        lambda market: {
            "date": "2026-06-10",
            "freshness": {},
            "verdict": None,
            "briefs": [],
        },
    )

    r = client.get("/api/today")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")

    r = client.get("/api/today", headers={"X-Passcode": "wrong"})
    assert r.status_code == 401

    r = client.get("/api/today", headers={"X-Passcode": "s3cret"})
    assert r.status_code == 200

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["passcode"] is True


def test_auth_verify_before_storage(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    r = client.post("/api/auth/verify", json={"passcode": "wrong"})
    assert r.status_code == 401
    r = client.post("/api/auth/verify", json={"passcode": "s3cret"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_refine_instruction_length_cap(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    topic = _generated_topic()
    r = client.post(
        "/api/ask/refine",
        json={"topic": topic, "instruction": "x" * 501},
        headers={"X-Passcode": "s3cret"},
    )
    assert r.status_code == 400


def test_trim_chat_history_caps_turns():
    long_history = [{"role": "user", "content": "hi"} for _ in range(50)]
    trimmed = main._trim_chat_history(long_history)
    assert len(trimmed) == main.MAX_CHAT_HISTORY_TURNS


def test_health_open_without_passcode():
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["dataset"] == "trends_v2_dev"
    assert data["passcode"] is False


def test_gate_fail_closed_without_passcode(monkeypatch):
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.delenv("UI_PASSCODE", raising=False)
    r = client.get("/api/today?market=za")
    assert r.status_code == 503


def test_invalid_market_rejected():
    r = client.get("/api/today?market=uk")
    assert r.status_code == 400


def test_rate_limit_trips(monkeypatch):
    monkeypatch.setattr(
        main,
        "build_ask_payload",
        lambda q, m: {
            "query": q,
            "market": m,
            "total_matches": 0,
            "thin": True,
            "volume_series": [],
            "platform_split": [],
            "market_split": [],
            "trends": [],
            "quotes": [],
            "slang": [],
            "creators": [],
            "overall_read": main.THIN_READ,
            "limited_voice": True,
        },
    )
    for i in range(30):
        r = client.get(f"/api/ask?q=topic{i}")
        assert r.status_code == 200
    r = client.get("/api/ask?q=onemore")
    assert r.status_code == 429


def test_rate_limit_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(
        main,
        "build_ask_payload",
        lambda q, m: {
            "query": q,
            "market": m,
            "total_matches": 0,
            "thin": True,
            "volume_series": [],
            "platform_split": [],
            "market_split": [],
            "trends": [],
            "quotes": [],
            "slang": [],
            "creators": [],
            "overall_read": main.THIN_READ,
            "limited_voice": True,
        },
    )
    for i in range(30):
        assert client.get(f"/api/ask?q=topic{i}").status_code == 200
    with caplog.at_level("WARNING", logger="listening_post.api"):
        r = client.get("/api/ask?q=onemore")
    assert r.status_code == 429
    assert any("rate limit tripped" in rec.getMessage() for rec in caplog.records)


def test_card_renders_from_cache():
    payload = {
        "query": "amapiano",
        "market": "za",
        "total_matches": 5922,
        "thin": False,
        "volume_series": [],
        "platform_split": [],
        "market_split": [],
        "trends": [
            {
                "statement": "Log drums are the sound of the season.",
                "how_to_use": "Score content with amapiano rhythms.",
                "momentum": "Rising",
                "evidence_count": 3,
                "platforms": ["tiktok"],
                "markets": ["za"],
            }
        ],
        "quotes": [
            {
                "text": "This log drum hits different",
                "platform": "tiktok",
                "market": "za",
                "engagement": 1200000,
                "handle": "@kabza",
            }
        ],
        "slang": [],
        "creators": [],
        "overall_read": "Amapiano carries the week.",
        "limited_voice": True,
    }
    synth.cache_set(("amapiano", "za"), payload)

    r = client.get("/card/amapiano?market=za")
    assert r.status_code == 200
    body = r.text
    assert "42" in body
    assert "PULSE" not in body
    assert "Sub-Saharan Africa cultural trends" in body
    assert "Log drums are the sound of the season." in body
    assert "This log drum hits different" in body
    assert "1.2M" in body
    assert "Rendered on" in body
    assert "@media print" in body


def test_card_escapes_html_in_model_fields():
    # The card renders Gemini-authored statement/how_to_use and post text via
    # _esc (html.escape). A model that emits markup, or an entity-encoded
    # payload, must never reach the page as live HTML. Confirms the escape
    # boundary holds for every rendered string field.
    payload = {
        "query": "<img src=x onerror=alert(1)>",
        "market": "za",
        "total_matches": 1,
        "thin": False,
        "volume_series": [],
        "platform_split": [],
        "market_split": [],
        "trends": [
            {
                "statement": "<script>alert('xss')</script>",
                "how_to_use": "<b>bold</b> &#40;encoded&#41;",
                "momentum": "Rising",
                "evidence_count": 1,
                "platforms": ["tiktok"],
                "markets": ["za"],
            }
        ],
        "quotes": [
            {
                "text": "<svg/onload=alert(2)>",
                "platform": "tiktok",
                "market": "za",
                "engagement": 10,
                "handle": "<i>@evil</i>",
            }
        ],
        "slang": [],
        "creators": [],
        "overall_read": "ok",
        "limited_voice": True,
    }
    synth.cache_set(("xsskey", "za"), payload)

    r = client.get("/card/xsskey?market=za")
    assert r.status_code == 200
    body = r.text
    # No raw executable markup from model fields survives.
    assert "<script>alert('xss')</script>" not in body
    assert "<svg/onload=alert(2)>" not in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert "<i>@evil</i>" not in body
    # The escaped forms are present, proving the content rendered escaped.
    assert "&lt;script&gt;" in body
    # An entity-encoded payload has its ampersand escaped, so it cannot decode.
    assert "&amp;#40;" in body


def test_research_attachment_filename_uses_42_identity():
    response = main._research_html_attachment({"title": "Cited read"}, slug="read_001")
    disposition = response.headers["content-disposition"]

    assert 'filename="42-brief-read_001.html"' in disposition
    assert "pulse-brief" not in disposition


def test_index_serves_placeholder_with_no_cache_headers():
    r = client.get("/")
    assert r.status_code == 200
    # The cache headers are the durable contract and hold either way: the
    # index must never be cached, built or not.
    assert r.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    # The 42 title only exists in the built bundle. CI runs the backend tests
    # before the frontend build, so asserting it unconditionally asserted the
    # local machine's leftover dist directory rather than the application.
    if (main.WEB_DIST / "index.html").is_file():
        assert "42 · Open Intelligence" in r.text
    else:
        assert "<!DOCTYPE html>" in r.text


def test_vite_assets_serve_immutable_cache_headers():
    assets_dir = main.WEB_DIST / "assets"
    if not assets_dir.is_dir():
        return
    sample = next(assets_dir.iterdir(), None)
    if sample is None:
        return
    r = client.get("/assets/" + sample.name)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_html_responses_carry_security_headers(monkeypatch):
    # The index HTML page must deny framing and MIME sniffing.
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    # The printable card page is HTML too. With a passcode set and none supplied
    # it returns the locked HTML, which still passes through the middleware.
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    locked = client.get("/card/amapiano?market=za")
    assert locked.status_code == 401
    assert "42" in locked.text
    assert "PULSE" not in locked.text
    assert locked.headers["x-frame-options"] == "DENY"
    assert locked.headers["x-content-type-options"] == "nosniff"


def test_search_content_sapa_geo_filters_stats_and_creators(monkeypatch):
    captured = []

    def fake_run_query(sql, params=None):
        captured.append((sql, list(params or [])))
        return []

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    out = bq.search_content("sapa", "ng")
    assert out["total_matches"] == 0
    assert len(captured) == 3

    agg_sql, agg_params = captured[0]
    assert "NOT REGEXP_CONTAINS" in agg_sql
    geo_params = [p for p in agg_params if p[0] == "geo_pattern"]
    assert len(geo_params) == 1
    pattern = geo_params[0][2]
    assert "vietnam" in pattern
    assert "fansipan" in pattern
    assert "|" in pattern

    creators_sql, creators_params = captured[1]
    assert "NOT REGEXP_CONTAINS" in creators_sql
    assert any(p[0] == "geo_pattern" for p in creators_params)
    # Creators are voice-only: aggregator platforms are excluded in SQL.
    assert "NOT IN" in creators_sql
    assert "'web'" in creators_sql
    assert "'gdelt'" in creators_sql

    items_sql, items_params = captured[2]
    assert "NOT REGEXP_CONTAINS" in items_sql
    assert any(p[0] == "geo_pattern" for p in items_params)


def test_search_content_plain_query_has_no_geo_clause(monkeypatch):
    captured = []

    def fake_run_query(sql, params=None):
        captured.append((sql, list(params or [])))
        return []

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    bq.search_content("amapiano", "all")
    agg_sql, agg_params = captured[0]
    assert "NOT REGEXP_CONTAINS" not in agg_sql
    assert all(p[0] != "geo_pattern" for p in agg_params)
    creators_sql, _ = captured[1]
    # Voice-only creators apply to every query, geo topic or not.
    assert "NOT IN" in creators_sql


def test_derive_topic_hashtags_guards_scoping_and_cache(monkeypatch):
    calls = []
    rows = [
        {"market": "ng", "topic": "sports_football", "tag": "#supereagles", "n": 30},
        {"market": "ke", "topic": "sports_football", "tag": "#harambeestars", "n": 20},
        {"market": "ng", "topic": "economy_sapa_hustle", "tag": "#sapa", "n": 25},
        # Geo-collision tag on a blocklisted topic: dropped.
        {"market": "ng", "topic": "economy_sapa_hustle", "tag": "#vietnam", "n": 50},
        # Letterless tag (an HTML entity remnant): dropped.
        {"market": "ng", "topic": "economy_sapa_hustle", "tag": "#8217", "n": 10},
    ]

    def fake_run_query(sql, params=None):
        calls.append(sql)
        # Entities are stripped in SQL before the hashtag extraction runs.
        assert "REGEXP_REPLACE" in sql
        assert "GROUP BY market, topic, tag" in sql
        return rows

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    out = bq.derive_topic_hashtags("all")

    assert [t["tag"] for t in out[("ng", "sports_football")]] == ["#supereagles"]
    assert [t["tag"] for t in out[("ke", "sports_football")]] == ["#harambeestars"]
    sapa = out[("ng", "economy_sapa_hustle")]
    assert [t["tag"] for t in sapa] == ["#sapa"]
    assert sapa[0]["share_pct"] == 100.0

    # Second call inside the TTL serves from the in-process cache.
    again = bq.derive_topic_hashtags("all")
    assert again == out
    assert len(calls) == 1


def test_parse_social_refs_unit():
    refs = [
        "https://example.com/a | youtube | Some title with spaces",
        "http://example.com/b | tiktok",
        "https://example.com/c",
        "not a url | tiktok | junk",
        42,
    ]
    out = bq.parse_social_refs(refs)
    assert out == [
        {
            "url": "https://example.com/a",
            "platform": "youtube",
            "title": "Some title with spaces",
        },
        {"url": "http://example.com/b", "platform": "tiktok", "title": None},
        {"url": "https://example.com/c", "platform": None, "title": None},
    ]
    assert bq.parse_social_refs(None) == []


def test_build_quotes_tags_and_junk_filters():
    def item(text, engagement):
        return {
            "text": text,
            "platform": "tiktok",
            "market": "za",
            "engagement": engagement,
            "handle": "@a",
        }

    items = [
        item(
            "This sound is taking over my feed #amapiano #logdrum #mzansi #sa #extra",
            10,
        ),
        item("#just #tags #here", 9),
        item("Get this incredible deal right now #ad", 8),
        item("New drop out now link in bio everyone", 7),
        item("Two trailing tags stay in the text #one #two", 6),
    ]
    quotes = bq.build_quotes(items)
    assert len(quotes) == 2
    assert quotes[0]["text"] == "This sound is taking over my feed"
    assert quotes[0]["tags"] == ["#amapiano", "#logdrum", "#mzansi", "#sa"]
    assert quotes[1]["text"] == "Two trailing tags stay in the text #one #two"
    assert "tags" not in quotes[1]


def test_retrieved_payload_caches_without_synthesis(monkeypatch):
    calls = []

    def fake_call_model(prompt):
        calls.append(prompt)
        return "this is not json"

    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(synth, "_call_model", fake_call_model)

    r1 = client.get("/api/ask?q=football")
    assert r1.status_code == 200
    assert r1.json()["overall_read"] == ""
    assert calls == []

    r2 = client.get("/api/ask?q=football")
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert calls == []
    assert synth.cache_get(("football", "all")) == r1.json()


def test_thin_payload_still_caches(monkeypatch):
    searches = []

    def fake_search(q, m):
        searches.append(q)
        thin = _rich_search_result()
        thin["items"] = [_ask_item(i) for i in range(5)]
        return thin

    monkeypatch.setattr(bq, "search_content", fake_search)
    client.get("/api/ask?q=quantum computing")
    client.get("/api/ask?q=quantum computing")
    assert len(searches) == 1


def test_synth_cache_cap_evicts_oldest():
    for i in range(synth._CACHE_MAX_ENTRIES):
        synth.cache_set(("q" + str(i), "all"), {"i": i})
    # Force a deterministic oldest entry, then overflow.
    synth._CACHE[("q0", "all")] = (0.0, {"i": 0})
    synth.cache_set(("overflow", "all"), {"i": "x"})
    assert len(synth._CACHE) == synth._CACHE_MAX_ENTRIES
    assert ("q0", "all") not in synth._CACHE
    assert ("overflow", "all") in synth._CACHE


def _stub_ask_payload(monkeypatch):
    monkeypatch.setattr(
        main,
        "build_ask_payload",
        lambda q, m: {
            "query": q,
            "market": m,
            "total_matches": 0,
            "thin": True,
            "volume_series": [],
            "platform_split": [],
            "market_split": [],
            "trends": [],
            "quotes": [],
            "slang": [],
            "creators": [],
            "overall_read": main.THIN_READ,
            "limited_voice": True,
        },
    )


def test_rate_limit_is_per_client(monkeypatch):
    _stub_ask_payload(monkeypatch)
    for i in range(30):
        r = client.get(f"/api/ask?q=topic{i}", headers={"x-forwarded-for": "1.1.1.1"})
        assert r.status_code == 200
    r = client.get("/api/ask?q=onemore", headers={"x-forwarded-for": "1.1.1.1"})
    assert r.status_code == 429
    # A different client is unaffected by the first client's burst.
    r = client.get("/api/ask?q=other", headers={"x-forwarded-for": "2.2.2.2, 9.9.9.9"})
    assert r.status_code == 200


def test_card_requires_key_when_passcode_set(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    synth.cache_set(
        ("amapiano", "za"),
        {
            "query": "amapiano",
            "market": "za",
            "total_matches": 10,
            "thin": True,
            "volume_series": [],
            "platform_split": [],
            "market_split": [],
            "trends": [],
            "quotes": [],
            "slang": [],
            "creators": [],
            "overall_read": main.THIN_READ,
            "limited_voice": True,
        },
    )

    r = client.get("/card/amapiano?market=za")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/html")
    assert "passcode" in r.text.lower()

    # The raw passcode is no longer accepted as a query param (it leaked into
    # logs), so ?key= is ignored and a bad token is rejected.
    r = client.get("/card/amapiano?market=za&key=s3cret")
    assert r.status_code == 401
    r = client.get("/card/amapiano?market=za&t=deadbeef")
    assert r.status_code == 401

    # A valid per-card token authenticates; it is scoped to this query+market.
    token = main._card_token("amapiano", "za")
    r = client.get(f"/card/amapiano?market=za&t={token}")
    assert r.status_code == 200
    assert "42" in r.text
    assert "PULSE" not in r.text
    # The same token does not unlock a different market.
    r = client.get(f"/card/amapiano?market=ng&t={token}")
    assert r.status_code == 401

    # The X-Passcode header still works for the in-app open.
    r = client.get("/card/amapiano?market=za", headers={"X-Passcode": "s3cret"})
    assert r.status_code == 200

    # The mint endpoint is header-gated and returns a tokenized, forwardable URL.
    r = client.get(
        "/api/card-link?q=amapiano&market=za", headers={"X-Passcode": "s3cret"}
    )
    assert r.status_code == 200
    minted = r.json()["url"]
    assert minted == f"/card/amapiano?market=za&t={token}"
    r = client.get("/api/card-link?q=amapiano&market=za")
    assert r.status_code == 401


def test_card_is_rate_limited(monkeypatch):
    _stub_ask_payload(monkeypatch)
    for i in range(30):
        r = client.get(f"/card/topic{i}", headers={"x-forwarded-for": "3.3.3.3"})
        assert r.status_code == 200
    r = client.get("/card/onemore", headers={"x-forwarded-for": "3.3.3.3"})
    assert r.status_code == 429


def test_today_and_rails_responses_ttl_cached(monkeypatch):
    today_calls = []
    rails_calls = []
    monkeypatch.setattr(
        bq,
        "fetch_today",
        lambda market: (
            today_calls.append(market)
            or {"date": None, "freshness": {}, "verdict": None, "briefs": []}
        ),
    )
    monkeypatch.setattr(
        bq,
        "fetch_rails",
        lambda market: rails_calls.append(market) or {"date": None, "rails": []},
    )
    assert client.get("/api/today").status_code == 200
    assert client.get("/api/today").status_code == 200
    assert client.get("/api/rails").status_code == 200
    assert client.get("/api/rails").status_code == 200
    assert today_calls == ["all"]
    assert rails_calls == ["all"]
    # A different market is its own cache entry.
    client.get("/api/today?market=za")
    assert today_calls == ["all", "za"]


def test_verdict_falls_back_with_date(monkeypatch):
    yesterday = _today() - timedelta(days=1)
    captured = {}

    def fake_run_query(sql, params=None):
        if "trend_analysis" in sql and "MAX(trend_date)" in sql:
            return [{"d": _today()}]
        if "trend_analysis" in sql:
            return list(TODAY_TREND_ROWS)
        if "REGEXP_EXTRACT_ALL" in sql:
            return []
        if "daily_summary" in sql:
            captured["sql"] = sql
            captured["params"] = list(params or [])
            return [{**VERDICT_ROW, "trend_date": yesterday}]
        if "pipeline_runs" in sql:
            return [{"stamp": datetime.now(UTC)}]
        raise AssertionError("unexpected sql: " + sql)

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    r = client.get("/api/today")
    assert r.status_code == 200
    verdict = r.json()["verdict"]
    # The verdict is pinned at or before the briefs' trend_date and carries
    # its own date so the client can label a stale verdict.
    assert "trend_date <= @d" in captured["sql"]
    assert ("d", "DATE", _today()) in captured["params"]
    assert verdict["verdict_date"] == yesterday.isoformat()


def test_topic_chip_strips_market_prefix():
    assert bq._topic_chip("ke/politics_maandamano") == {
        "query": "politics_maandamano",
        "market": "ke",
        "label": "Maandamano protests in Kenya",
    }
    assert bq._topic_chip("music_amapiano") == {
        "query": "music_amapiano",
        "label": "Amapiano",
    }


# Signal Desk fixtures. Two ZA topics on the latest day: one fully populated
# brief, one with every optional field absent.


def _desk_row(**overrides):
    row = {
        "trend_date": _today(),
        "market": "za",
        "query_group": "music_amapiano",
        "trend_score": 0.412,
        "velocity_score": 0.12,
        "tone_score": 0.61,
        "item_count": 88,
        "headline": "Amapiano carries the weekend. The log drum is everywhere.",
        "trend_synthesis": "Synthesis for amapiano.",
        "cultural_context": "Context for amapiano.",
        "sentiment_summary": "warm",
        "campaign_angles": [
            "Built from 128 posts across 6 sources this week",
            "12 creators tracked across TikTok",
        ],
        "top_creators": [
            "@kabza | tiktok | 9 mentions",
            "@123456789012345678901 | instagram | 5 mentions",
        ],
        "platforms": ["tiktok"],
        "render_payload": FULL_RENDER_PAYLOAD,
        "nano_banana_prompt": "image prompt",
        "lyria_prompt": "audio prompt",
        "social_refs": None,
    }
    row.update(overrides)
    return row


DESK_DIGEST_ROW = {
    "trend_date": date(2026, 6, 30),
    "through_line": "za/music_amapiano sets the tempo",
    "summary_text": "Watch economy_sapa_hustle for the counter-mood.",
    "call_to_action": "Track music_amapiano into the weekend.",
    "key_topics": ["music_amapiano"],
    "rising_topics": ["economy_sapa_hustle"],
}

DESK_ROWS = [
    _desk_row(),
    _desk_row(
        query_group="sports_rugby",
        trend_score=0.5,
        velocity_score=-0.2,
        tone_score=1.5,
        item_count=None,
        headline=None,
        trend_synthesis=None,
        cultural_context=None,
        sentiment_summary=None,
        campaign_angles=None,
        top_creators=None,
        platforms=["tiktok"],
        render_payload=None,
        nano_banana_prompt=None,
        lyria_prompt=None,
        social_refs=[
            "https://www.sarugbymag.co.za/form-guide | news | Off-season form guide",
            "not a url",
        ],
    ),
]


def test_clamp_sentiment_recenter_and_no_signal():
    # tone_score is a 0..1 score with 0.5 neutral; recenter to a signed -1..1.
    assert desk._clamp_sentiment(0.5, 5) == 0.0
    assert desk._clamp_sentiment(0.489, 5) == -0.022
    assert desk._clamp_sentiment(0.678, 5) == 0.356
    # values past the ends clamp to [-1, 1].
    assert desk._clamp_sentiment(1.5, 5) == 1.0
    # tone_rows == 0 means no media-tone signal: no pill, not a false neutral.
    assert desk._clamp_sentiment(0.0, 0) is None
    assert desk._clamp_sentiment(None, 5) is None


def test_mood_label_parses_qualified_trajectory():
    assert bq.mood_label("improving (+6pp positive share)") == "Warming up"
    assert bq.mood_label("declining (-5pp positive share)") == "Cooling off"
    assert bq.mood_label("stable") == "Steady and balanced"
    assert bq.mood_label("") == "Steady and balanced"
    assert bq.mood_label(None) == "Steady and balanced"


def test_channel_sov_share_per_platform():
    topic = {"tiktok": 40, "instagram": 12, "youtube": 0}
    market = {"tiktok": 500, "instagram": 600, "youtube": 100}
    out = bq._channel_sov(topic, market)
    # Ordered by topic count desc; sov_pct is the topic's share of each platform.
    assert [d["n"] for d in out] == [40, 12, 0]
    by_plat = {d["platform"]: d["sov_pct"] for d in out}
    assert by_plat[bq._platform_label("tiktok")] == 8.0
    assert by_plat[bq._platform_label("instagram")] == 2.0
    assert by_plat[bq._platform_label("youtube")] == 0.0
    # A platform absent from the market totals yields 0, never a divide error.
    assert bq._channel_sov({"reddit": 5}, {})[0]["sov_pct"] == 0.0


def _desk_history_rows():
    today = _today()
    return [
        {
            "market": "za",
            "query_group": "music_amapiano",
            "trend_date": today,
            "trend_score": 0.412,
        },
        {
            "market": "za",
            "query_group": "music_amapiano",
            "trend_date": today - timedelta(days=1),
            "trend_score": 0.38,
        },
        {
            "market": "za",
            "query_group": "music_amapiano",
            "trend_date": today - timedelta(days=5),
            "trend_score": 0.2,
        },
        {
            "market": "za",
            "query_group": "sports_rugby",
            "trend_date": today,
            "trend_score": 0.5,
        },
    ]


def _desk_pool_row(i, text, platform="tiktok"):
    return {
        "topic": "music_amapiano",
        "id": "p" + str(i),
        "title": "",
        "text": text,
        "platform": platform,
        "market": "za",
        "author_handle": "voice" + str(i),
        "engagement": 100 - i,
        "slang_terms": "",
        "url": "https://ex.com/p" + str(i),
    }


DESK_POOL_ROWS = [
    _desk_pool_row(0, "The log drum on this track is unreal"),
    _desk_pool_row(1, "Amapiano mixes carrying my whole exam season"),
    _desk_pool_row(2, "Everyone at the function knows this dance now", "instagram"),
    _desk_pool_row(3, "One more amapiano line that should not surface"),
]


def _bridge_row(handle, post_id, topics, engagement=50, platform="tiktok"):
    return {
        "handle": handle,
        "market": "za",
        "id": post_id,
        "platform": platform,
        "engagement": engagement,
        "topics": topics,
    }


# @kabza carries two posts in each scene with no cross-tagging: a real bridge.
# @oneoff double-tags every post, which is multi-label classification, not a
# bridge, and must not surface.
DESK_BRIDGE_ROWS = [
    _bridge_row("@kabza", "b1", ["music_amapiano"], 900),
    _bridge_row("@kabza", "b2", ["music_amapiano"], 800),
    _bridge_row("@kabza", "b3", ["sports_rugby"], 700),
    _bridge_row("@kabza", "b4", ["sports_rugby"], 600),
    _bridge_row("@oneoff", "b5", ["music_amapiano", "sports_rugby"], 500),
    _bridge_row("@oneoff", "b6", ["music_amapiano", "sports_rugby"], 400),
]

DESK_LEXICON_ROWS = [
    {"term": "log drum", "market": "za", "n": 30, "n7": 18, "n_prev": 9},
    {"term": "log drum", "market": "ng", "n": 4, "n7": 1, "n_prev": 1},
    {"term": "thin", "market": "za", "n": 5, "n7": 2, "n_prev": 1},
]


def _desk_dispatcher(calls=None):
    stamp = datetime.now(UTC) - timedelta(hours=2)

    def fake_run_query(sql, params=None):
        if calls is not None:
            calls.append(sql)
        if "pipeline_runs" in sql:
            return [{"stamp": stamp}]
        if "daily_summary" in sql:
            return [dict(DESK_DIGEST_ROW)]
        if "LEFT JOIN" in sql:
            return [dict(r) for r in DESK_ROWS]
        if "BETWEEN DATE_SUB(@d" in sql:
            return _desk_history_rows()
        if "ROW_NUMBER" in sql:
            assert any(
                p[0] == "topics" and isinstance(p[2], list) for p in (params or [])
            )
            return [dict(r) for r in DESK_POOL_ROWS]
        if "crossing" in sql:
            return [dict(r) for r in DESK_BRIDGE_ROWS]
        if "n_prev" in sql:
            return [dict(r) for r in DESK_LEXICON_ROWS]
        # Wave 2 tone split. It runs via _safe_query, which would swallow a
        # NotFound from the absent column in prod; in the test the dispatcher
        # returns empty so the desk's tone bar stays hidden, matching today's
        # dark state.
        if "sentiment_lexicon_score" in sql:
            return []
        # The stories surface is probed before it is read. Staging has never
        # carried pan_african_stories, so the probe answers absent and the
        # story SELECT must not follow it.
        if "INFORMATION_SCHEMA.TABLES" in sql:
            assert params == [("name", "STRING", "pan_african_stories")]
            return [{"present": 0}]
        if "pan_african_stories" in sql:
            raise AssertionError("story SELECT issued against an absent table")
        if "GROUP BY market, tg" in sql:
            return [
                {
                    "market": "za",
                    "topic": "music_amapiano",
                    "mentions": 88,
                    "reach": 5_000_000,
                },
                {
                    "market": "za",
                    "topic": "sports_rugby",
                    "mentions": 12,
                    "reach": 900_000,
                },
            ]
        if "COUNT(*) AS n" in sql and "GROUP BY" not in sql:
            return [{"n": 1000}]
        # The engine-owned run receipt probe. The staging estate carries only
        # canary rows and zero display-eligible predictions, so no run
        # qualifies and Open Discover must report unavailable, never an empty
        # success.
        if "open_intelligence_run_receipts_v1" in sql:
            return []
        raise AssertionError("unexpected sql: " + sql)

    return fake_run_query


def _no_model(*args, **kwargs):
    raise AssertionError("the desk build must never call the model")


def test_desk_contract_shape(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    r = client.get("/api/desk?region=za")
    assert r.status_code == 200
    data = r.json()
    assert data["updated"] == _today().isoformat()
    assert data["freshness"]["status"] == "green"

    topics = data["topics"]
    assert [t["id"] for t in topics] == ["sports_rugby", "music_amapiano"]

    t = next(x for x in topics if x["id"] == "music_amapiano")
    assert t["region"] == "ZA"
    assert t["topic"] == "Amapiano"
    assert t["label"] == "Music · South Africa"
    assert t["momentum"] == "rising"
    assert t["score"] == 0.412
    assert t["delta"] == 0.032
    assert t["mentions"] == 88
    assert t["sources"] == 6
    assert t["creators"] == 12
    # tone_score 0.61 on the engine's 0..1 scale recenters to (0.61-0.5)*2 = 0.22.
    assert t["sentiment"] == 0.22
    # no b24_sentiment_trajectory in this fixture, so social mood is absent.
    assert t["social_mood"] is None
    # abs velocities today are [0.12, 0.2]: 0.12 sits in the bottom quartile.
    assert t["velocity"] == "Low"
    assert t["age"] == "2h"
    assert t["why"] == "Amapiano carries the weekend."
    assert len(t["why"].split()) <= 18
    assert t["platforms"] == [["TikTok", 40], ["Instagram", 25]]
    # The 21-char anon handle is dropped; only the named creator survives.
    assert t["creators_list"] == ["@kabza"]
    assert t["voices"] == [
        ["TikTok", "The log drum on this track is unreal", ""],
        ["TikTok", "Amapiano mixes carrying my whole exam season", ""],
        ["Instagram", "Everyone at the function knows this dance now", ""],
    ]
    brief = t["brief"]
    assert brief["trend"] == "Synthesis for amapiano."
    # "warm" is boilerplate-short, so relevance falls back to the stat angle.
    assert brief["relevance"] == "Built from 128 posts across 6 sources this week"
    # The opportunity lens is lazy: absent until /api/desk/opportunity fills
    # the cache for this (topic, trend_date).
    assert "opportunity" not in brief
    assert brief["idea"] == {
        "tool": "Editorial response",
        "text": "Context for amapiano.",
    }
    assert brief["prompt"] == {"nano": "image prompt", "lyria": "audio prompt"}

    bare = next(x for x in topics if x["id"] == "sports_rugby")
    assert bare["momentum"] == "cooling"
    assert bare["velocity"] == "Very high"
    assert bare["sentiment"] == 1.0
    # mentions are re-based onto the real 30-day enriched count from
    # fetch_topics_reach (12 for this topic), not the engine's scored-day item_count.
    assert bare["mentions"] == 12
    assert bare["sources"] == 0
    assert bare["creators"] == 0
    assert bare["why"] == ""
    assert bare["platforms"] == [["TikTok", 1]]
    assert bare["creators_list"] == []
    assert bare["voices"] == []
    assert "genz" not in t
    assert "genz" not in bare
    # No synthesis and no context: the opportunity lens never fires and the
    # field drops instead of carrying filler.
    assert "opportunity" not in bare["brief"]
    assert bare["brief"]["idea"]["tool"] == "Editorial response"


def test_desk_dynamic_discovery_is_unavailable_when_no_run_qualifies(monkeypatch):
    """Break caught: the canary-only estate reported as an empty success."""
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()
    dynamic = data["dynamic_discovery"]

    assert dynamic["contract_version"] == "desk_dynamic_signal_v2"
    assert dynamic["status"] == "unavailable"
    assert dynamic["requested_market"] == "za"
    assert dynamic["run"] is None
    assert dynamic["signals"] == []
    assert dynamic["error"]["code"] == "no_released_closed_run"
    assert dynamic["error"]["retryable"] is True
    message = dynamic["error"]["message"]
    assert message and len(message) <= 240
    # A strategist-facing message names no dataset, table, query or identity.
    for leaked in ("trends_v2", "open_intelligence_run_receipts", "SELECT", "@", "iam"):
        assert leaked not in message


def test_desk_dynamic_discovery_reports_dependency_failure_separately(monkeypatch):
    """Break caught: an unreadable engine surface presented as no discovery."""
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()

    def failing(sql, params=None):
        if "open_intelligence_run_receipts_v1" in sql:
            raise gexc.NotFound("table absent")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", failing)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = client.get("/api/desk?region=za").json()["dynamic_discovery"]
    assert dynamic["status"] == "unavailable"
    assert dynamic["run"] is None
    assert dynamic["signals"] == []
    # Distinct from no_released_closed_run: the surface could not be read at all.
    assert dynamic["error"]["code"] == "dependency_unavailable"
    assert dynamic["error"]["retryable"] is True


def test_desk_dynamic_failure_does_not_fail_the_existing_desk_response(monkeypatch):
    """Break caught: a dynamic dependency error taking the whole desk down."""
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()

    def failing(sql, params=None):
        if "open_intelligence_run_receipts_v1" in sql:
            raise gexc.NotFound("table absent")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", failing)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    response = client.get("/api/desk?region=za")
    assert response.status_code == 200
    assert [t["id"] for t in response.json()["topics"]] == [
        "sports_rugby",
        "music_amapiano",
    ]


def test_desk_dynamic_discovery_is_additive_and_changes_no_existing_member(monkeypatch):
    """Break caught: the new member reordering or rewriting curated desk data."""
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()

    # dynamic_discovery is appended, so every pre-existing member keeps its
    # exact key order as well as its value.
    assert list(data)[-1] == "dynamic_discovery"
    assert list(data)[:-1] == [
        "updated",
        "freshness",
        "digest",
        "topics",
        "bridges",
        "lexicon",
        "platform_heat",
        "tone_split",
        "pan_african",
        "pan_african_surface",
    ]
    # No dynamic row is merged, appended or decorated into curated topics.
    assert [t["id"] for t in data["topics"]] == ["sports_rugby", "music_amapiano"]
    assert all("signal_id" not in t for t in data["topics"])


def test_desk_dynamic_discovery_requested_market_is_the_validated_region(monkeypatch):
    """Break caught: an unvalidated caller market reaching the response."""
    seen = []

    def no_receipt(market):
        seen.append(market)
        return []

    monkeypatch.setattr(bq, "fetch_dynamic_run_receipt", no_receipt)

    for region, expected in (
        ("za", "za"),
        ("ZA", "za"),
        ("  ng  ", "ng"),
        ("ke", "ke"),
        ("all", "all"),
        ("", "all"),
        ("uk", "all"),
        ("za; DROP", "all"),
    ):
        assert desk.build_dynamic_discovery(region)["requested_market"] == expected
    # The market that reaches the engine read is the validated one, never the
    # caller's raw string.
    assert seen == ["za", "za", "ng", "ke", "all", "all", "all", "all"]


def test_desk_series_zero_pad_and_delta(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()
    t = next(x for x in data["topics"] if x["id"] == "music_amapiano")
    series = t["series"]
    assert len(series) == 30
    assert series[-1] == 0.412
    assert series[-2] == 0.38
    assert series[24] == 0.2
    assert series.count(0.0) == 27

    bare = next(x for x in data["topics"] if x["id"] == "sports_rugby")
    assert len(bare["series"]) == 30
    assert bare["series"][-1] == 0.5
    # No yesterday row: the delta reads 0, never a fabricated move.
    assert bare["delta"] == 0.0


def test_desk_topic_endpoint_reuses_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher(calls))
    monkeypatch.setattr(synth, "_call_model", _no_model)

    assert client.get("/api/desk?region=za").status_code == 200
    queries_after_desk = len(calls)

    r = client.get("/api/desk/topic/music_amapiano?region=za")
    assert r.status_code == 200
    assert r.json()["id"] == "music_amapiano"
    # The topic endpoint serves from the cached desk payload. Its only query is
    # the ingest cache-version probe every cached read pays so a changed ingest
    # mix busts the cache; no per-topic data query fires.
    assert len(calls) == queries_after_desk + 1

    assert client.get("/api/desk/topic/nope?region=za").status_code == 404


def test_desk_costs_eleven_queries(monkeypatch):
    # The ingest cache-version probe, freshness, desk rows, digest, history,
    # voice pools, bridge posts, slang counts, the batched per-topic reach, plus
    # the two Wave 2 reads (the lexicon tone split and the pan-African stories).
    # Share of voice is derived from the topics' own mention counts, so there is
    # still nothing per-topic; the desk cost is flat per region, not per topic.
    calls = []
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher(calls))
    monkeypatch.setattr(synth, "_call_model", _no_model)
    client.get("/api/desk?region=za")
    # Twelve since Open Discover added the engine-owned run receipt probe. It is
    # one flat read per region like the rest, never one per topic.
    assert len(calls) == 12
    voice_queries = [sql for sql in calls if "AS dup_rn" in sql]
    assert len(voice_queries) == 1
    assert "PARTITION BY market, topic ORDER BY" in voice_queries[0]
    assert "PARTITION BY market, topic, textkey ORDER BY" in voice_queries[0]


def test_desk_independent_reads_overlap(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)
    barrier = threading.Barrier(2, timeout=2)
    digest = bq.fetch_digest
    history = bq.fetch_score_history

    def read_digest(latest):
        barrier.wait()
        return digest(latest)

    def read_history(latest, region):
        barrier.wait()
        return history(latest, region)

    monkeypatch.setattr(bq, "fetch_digest", read_digest)
    monkeypatch.setattr(bq, "fetch_score_history", read_history)
    assert len(desk.build_desk_payload("za")["topics"]) == 2


def test_desk_topic_cache_reads_overlap_and_preserve_score_ties(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    rows = [dict(row, trend_score=0.5) for row in DESK_ROWS]
    monkeypatch.setattr(bq, "fetch_desk_rows", lambda region: rows)
    barrier = threading.Barrier(2, timeout=2)

    def cache_read(key):
        barrier.wait()
        return "Cached opportunity " + key[1]

    monkeypatch.setattr(synth, "cache_get", cache_read)
    payload = desk.build_desk_payload("za")
    assert [item["id"] for item in payload["topics"]] == [
        row["query_group"] for row in rows
    ]
    assert all(item["brief"]["opportunity"] for item in payload["topics"])


def test_desk_parallel_reads_share_one_bound_across_markets(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    release = threading.Event()
    four = threading.Event()
    fifth = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    outputs = []
    failures = []

    def tracked(function):
        def read(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active >= 4:
                    four.set()
                if active > 4:
                    fifth.set()
            try:
                assert release.wait(3), "test never released blocked reads"
                return function(*args, **kwargs)
            finally:
                with lock:
                    active -= 1

        return read

    for name in (
        "fetch_digest",
        "fetch_score_history",
        "fetch_voice_pools",
        "fetch_topics_reach",
        "fetch_bridge_rows",
        "fetch_lexicon_rows",
        "fetch_tone_split",
        "fetch_pan_african_rows",
    ):
        monkeypatch.setattr(bq, name, tracked(getattr(bq, name)))
    monkeypatch.setattr(synth, "cache_get", tracked(synth.cache_get))
    monkeypatch.setattr(
        desk, "build_dynamic_discovery", tracked(desk.build_dynamic_discovery)
    )

    def load(region):
        try:
            outputs.append(desk.build_desk_payload(region))
        except Exception as error:
            failures.append(error)

    threads = [
        threading.Thread(target=load, args=(region,)) for region in ("za", "all")
    ]
    for thread in threads:
        thread.start()
    try:
        assert four.wait(2), "independent reads never overlapped"
        assert not fifth.wait(0.2), "market loads multiplied the four-read bound"
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
    assert not any(thread.is_alive() for thread in threads)
    assert not failures
    assert len(outputs) == 2
    assert peak == 4
    assert active == 0


def test_desk_read_failure_drains_running_work(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    failures = []

    def blocked_digest(latest):
        entered.set()
        assert release.wait(3)
        return None

    def failed_history(latest, region):
        assert entered.wait(2)
        raise ValueError("history read failed")

    monkeypatch.setattr(bq, "fetch_digest", blocked_digest)
    monkeypatch.setattr(bq, "fetch_score_history", failed_history)

    def load():
        try:
            desk.build_desk_payload("za")
        except Exception as error:
            failures.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=load)
    thread.start()
    try:
        assert entered.wait(2)
        assert not finished.wait(0.2), "request returned while its read still ran"
    finally:
        release.set()
        thread.join(3)
    assert finished.is_set()
    assert len(failures) == 1
    assert str(failures[0]) == "history read failed"


def test_desk_bridges_and_lexicon(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()

    # The exclusive-post rule keeps @kabza and drops the double-tagger.
    assert len(data["bridges"]) == 1
    b = data["bridges"][0]
    assert b["h"] == "@kabza"
    assert b["mk"] == "ZA"
    assert {b["from"], b["to"]} == {"Amapiano", "Rugby and Springboks"}
    assert b["eng"] == "3k"
    assert "weight" not in b
    assert "posts in" in b["note"]

    # Aggregated across markets, the 25-row floor applied, wow from real weeks.
    assert data["lexicon"] == [{"term": "log drum", "market": "ZA", "n": 34, "wow": 90}]


def test_desk_wave1_fields_absent_today(monkeypatch):
    # The default desk rows carry none of the Wave 1 columns (they are NULL in
    # prod until the engine flags flip), so momentum still reads from velocity
    # and neither badge key rides on the topic: byte-identical to today.
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()
    amap = next(t for t in data["topics"] if t["id"] == "music_amapiano")
    # velocity 0.12, score 0.412 -> Rising via the legacy rail, not an engine label.
    assert amap["momentum"] == "rising"
    assert "lifecycle" not in amap
    assert "continuity" not in amap
    # The platform heat-map still folds the topics' own channel breakdowns: it is
    # an aggregate of data that already flows, not a Wave 1 gated column. Amapiano
    # carries TikTok 40 + Instagram 25 from its channels; rugby adds TikTok 1 from
    # the platform fallback, so TikTok totals 41 of the 66 board weight.
    assert data["platform_heat"] == [
        {"platform": "TikTok", "weight": 41.0, "share": 62.1},
        {"platform": "Instagram", "weight": 25.0, "share": 37.9},
    ]


def test_desk_wave1_fields_surface_when_present(monkeypatch):
    # Inject the Wave 1 columns onto the amapiano row the way the engine will
    # write them post-migration. The stored momentum_label overrides the velocity
    # read, and the two badges attach.
    def dispatcher(sql, params=None):
        if "LEFT JOIN" in sql:
            rows = [dict(r) for r in DESK_ROWS]
            rows[0].update(
                {
                    "momentum_label": "Building",
                    "lifecycle_phase": "Growth",
                    "continuity_state": "day3plus",
                }
            )
            # Garbage on the second row must collapse to no override / no badge.
            rows[1].update(
                {
                    "momentum_label": "surging",
                    "lifecycle_phase": "plateau",
                    "continuity_state": None,
                }
            )
            return rows
        return _desk_dispatcher()(sql, params)

    monkeypatch.setattr(bq, "_run_query", dispatcher)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()
    amap = next(t for t in data["topics"] if t["id"] == "music_amapiano")
    # The stored label wins over the velocity-derived "rising".
    assert amap["momentum"] == "building"
    assert amap["lifecycle"] == "growth"
    assert amap["continuity"] == "day3plus"

    rugby = next(t for t in data["topics"] if t["id"] == "sports_rugby")
    # Unknown label falls back to the velocity rail (cooling); bad badges drop.
    assert rugby["momentum"] == "cooling"
    assert "lifecycle" not in rugby
    assert "continuity" not in rugby


# Wave 2 (19 Jun 2026): tone split + pan-African on the desk, both defensive
# against the locked Wave 2 names. The tone split bins enriched_content's
# sentiment_lexicon_score behind _safe_query, so a pre-migration run degrades
# to empty. The pan-African view reads the pan_african_stories table behind a
# probe instead: an absent table is reported as absent, a refused probe or
# read as a dependency failure, and only a present table with no rows as a day
# with no stories.


def test_safe_query_swallows_missing_surface(monkeypatch):
    from google.api_core import exceptions as gexc

    def boom(sql, params=None):
        raise gexc.NotFound("no such table")

    monkeypatch.setattr(bq, "_run_query", boom)
    # A NotFound on a not-yet-migrated surface degrades to empty, never 500s.
    assert bq._safe_query("SELECT 1", []) == []

    def other(sql, params=None):
        raise ValueError("a real bug, not a missing table")

    monkeypatch.setattr(bq, "_run_query", other)
    with pytest.raises(ValueError):
        bq._safe_query("SELECT 1", [])


def test_build_tone_split_shares_and_empty():
    # No scored rows (the column is NULL across the feed today) yields {} so the
    # bar hides.
    assert (
        desk.build_tone_split({"positive": 0, "neutral": 0, "negative": 0, "scored": 0})
        == {}
    )
    assert desk.build_tone_split({}) == {}
    # A clean 6 / 2 / 2 of 10 scored -> 60 / 20 / 20, shares sum to 100.
    split = desk.build_tone_split(
        {"positive": 6, "neutral": 2, "negative": 2, "scored": 10}
    )
    assert split["positive"] == {"n": 6, "share": 60.0}
    assert split["neutral"] == {"n": 2, "share": 20.0}
    assert split["negative"] == {"n": 2, "share": 20.0}
    assert split["scored"] == 10
    # Neutral is derived from the scored total so the three shares always cover
    # 100 even when the engine's neutral count and the others disagree slightly.
    derived = desk.build_tone_split({"positive": 7, "negative": 1, "scored": 10})
    assert derived["neutral"]["n"] == 2


def test_build_pan_african_two_market_rule_and_sort():
    rows = [
        {
            "story_id": "amapiano",
            "story_label": "Amapiano crosses into West Africa",
            "markets": ["za", "ng", "za"],
            "topic_keys": ["music_amapiano"],
            "total_item_count": 420,
            "momentum_composite": 0.81,
        },
        {
            "story_id": "sapa",
            "story_label": "The broke-economy joke spreads",
            "markets": ["ng", "ke", "za"],
            "topic_keys": ["economy_sapa_hustle"],
            "total_item_count": 260,
            "momentum_composite": 0.64,
        },
        {
            "story_id": "local",
            "story_label": "Single market only",
            "markets": ["za"],
            "topic_keys": ["lifestyle_soft_life"],
            "total_item_count": 99,
            "momentum_composite": 0.99,
        },
        {
            "story_id": "nolabel",
            "story_label": "",
            "markets": ["za", "ng"],
            "topic_keys": [],
            "total_item_count": 10,
            "momentum_composite": 0.5,
        },
    ]
    out = desk.build_pan_african(rows)
    # The single-market row and the label-less row drop; the rest sort by momentum.
    assert [s["label"] for s in out] == [
        "Amapiano crosses into West Africa",
        "The broke-economy joke spreads",
    ]
    # Markets are de-duplicated and upper-cased for display.
    assert out[0]["markets"] == ["ZA", "NG"]
    assert out[1]["markets"] == ["NG", "KE", "ZA"]
    # Empty input is empty out.
    assert desk.build_pan_african([]) == []
    assert desk.build_pan_african(None) == []


def test_desk_wave2_absent_today(monkeypatch):
    # The dispatcher returns empties for both Wave 2 reads (the column and table
    # are not populated until the migrations apply), so the desk carries an empty
    # tone split and no pan-African stories: the panels hide, byte-identical to
    # today.
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()
    assert data["tone_split"] == {}
    assert data["pan_african"] == []
    # The stories surface says the table is absent. An empty list alone would
    # read as a run that found no cross-market story, which is not what happened.
    assert data["pan_african_surface"] == {
        "status": "unavailable",
        "error": {
            "code": "surface_missing",
            "message": (
                "The pan-African stories surface is not provisioned in this dataset."
            ),
            "retryable": False,
        },
    }
    # The dedicated endpoints read the same cached payload.
    tone = client.get("/api/desk/tone-split?region=za").json()
    assert tone == {"region": "za", "tone_split": {}}
    pan = client.get("/api/desk/pan-african?region=za").json()
    assert pan == {
        "region": "za",
        "status": "unavailable",
        "error": data["pan_african_surface"]["error"],
        "stories": [],
    }


def test_desk_wave2_surface_when_present(monkeypatch):
    # Feed the two Wave 2 reads the way the engine will once the migrations apply
    # and the flags flip: a scored tone row and two cross-market stories.
    def dispatcher(sql, params=None):
        if "sentiment_lexicon_score" in sql:
            return [{"positive": 6, "negative": 2, "neutral": 2, "scored": 10}]
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            return [
                {
                    "story_id": "amapiano",
                    "story_label": "Amapiano crosses into West Africa",
                    "markets": ["za", "ng"],
                    "topic_keys": ["music_amapiano"],
                    "total_item_count": 420,
                    "momentum_composite": 0.81,
                },
                {
                    "story_id": "sapa",
                    "story_label": "The broke-economy joke spreads",
                    "markets": ["ng", "ke", "za"],
                    "topic_keys": ["economy_sapa_hustle"],
                    "total_item_count": 260,
                    "momentum_composite": 0.64,
                },
            ]
        return _desk_dispatcher()(sql, params)

    monkeypatch.setattr(bq, "_run_query", dispatcher)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()
    assert data["tone_split"]["positive"] == {"n": 6, "share": 60.0}
    assert [s["label"] for s in data["pan_african"]] == [
        "Amapiano crosses into West Africa",
        "The broke-economy joke spreads",
    ]
    assert data["pan_african"][0]["markets"] == ["ZA", "NG"]
    assert data["pan_african_surface"] == {"status": "ready", "error": None}

    pan = client.get("/api/desk/pan-african?region=za").json()
    assert pan["region"] == "za"
    assert pan["status"] == "ready"
    assert pan["error"] is None
    assert len(pan["stories"]) == 2


def test_desk_pan_african_present_table_with_no_rows_is_no_stories(monkeypatch):
    # The table exists and the stage wrote nothing for the day: that is the one
    # honest "no stories" state, distinct from the table being absent.
    inner = _desk_dispatcher()

    def dispatcher(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            return []
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", dispatcher)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()
    assert data["pan_african"] == []
    assert data["pan_african_surface"] == {"status": "no_stories", "error": None}
    pan = client.get("/api/desk/pan-african?region=za").json()
    assert pan == {"region": "za", "status": "no_stories", "error": None, "stories": []}


def test_desk_pan_african_missing_table_issues_no_story_query(monkeypatch):
    # Staging at 06:29 UTC on 20 Sep 2026: three failed jobs per desk build, all
    # "Not found: Table ...pan_african_stories". The probe must answer first and
    # the story SELECT must never reach BigQuery when the table is absent.
    issued: list = []
    inner = _desk_dispatcher()

    def dispatcher(sql, params=None):
        issued.append(sql)
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", dispatcher)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()
    assert data["pan_african_surface"]["status"] == "unavailable"
    assert data["pan_african_surface"]["error"]["code"] == "surface_missing"
    probes = [q for q in issued if "INFORMATION_SCHEMA.TABLES" in q]
    story_reads = [
        q
        for q in issued
        if "pan_african_stories" in q and "INFORMATION_SCHEMA" not in q
    ]
    assert len(probes) == 1
    assert story_reads == []


def test_desk_pan_african_unreadable_surface_is_dependency_unavailable(monkeypatch):
    # The probe itself is refused (the service account lacks the permission):
    # report a dependency failure, never an empty success, never a missing
    # table, and never a 500 for the whole desk.
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()

    def failing_probe(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            raise gexc.Forbidden("Access Denied: Dataset test-project:trends")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", failing_probe)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    response = client.get("/api/desk?region=za")
    assert response.status_code == 200
    surface = response.json()["pan_african_surface"]
    assert surface["status"] == "unavailable"
    assert surface["error"]["code"] == "dependency_unavailable"
    # A refused permission does not fix itself, so it is not retryable.
    assert surface["error"]["retryable"] is False
    # The rest of the desk is untouched by the stories surface failing.
    assert [t["id"] for t in response.json()["topics"]] == [
        "sports_rugby",
        "music_amapiano",
    ]

    # A table that vanishes between the probe and the read is the same failure.
    def failing_read(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            raise gexc.NotFound("table gone")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", failing_read)
    main._market_cache.clear()
    surface = client.get("/api/desk?region=za").json()["pan_african_surface"]
    assert surface["status"] == "unavailable"
    assert surface["error"]["code"] == "dependency_unavailable"

    # Any other exception is a real bug and still propagates.
    def broken(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            raise ValueError("a real bug, not a missing table")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", broken)
    with pytest.raises(ValueError):
        bq.fetch_pan_african_rows("za")

    # The same on the story read: only BigQuery's own answers are a surface
    # failure, so a bug past the probe still propagates.
    def broken_read(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            raise ValueError("a real bug, not a refused read")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", broken_read)
    with pytest.raises(ValueError):
        bq.fetch_pan_african_rows("za")


def test_fetch_pan_african_rows_probe_pins_the_table_name(monkeypatch):
    # The probe asks BigQuery about a pinned name and never reads the name back
    # from the answer. A probe that counted any row would prove only that the
    # dataset has tables.
    seen: list = []

    def dispatcher(sql, params=None):
        seen.append((sql, params))
        return [{"present": 0}]

    monkeypatch.setattr(bq, "_run_query", dispatcher)
    with pytest.raises(bq.PanAfricanSurfaceMissing):
        bq.fetch_pan_african_rows("all")
    ((sql, params),) = seen
    assert "INFORMATION_SCHEMA.TABLES" in sql
    assert "WHERE table_name = @name" in sql
    assert params == [("name", "STRING", "pan_african_stories")]


_PAN_AFRICAN_UNREADABLE = {
    "code": "dependency_unavailable",
    "message": "The pan-African stories surface could not be read.",
    "retryable": True,
}


@pytest.mark.parametrize(
    ("exc_name", "retryable"),
    [("Forbidden", False), ("DeadlineExceeded", True), ("ServiceUnavailable", True)],
)
def test_desk_pan_african_read_refused_is_dependency_unavailable(
    monkeypatch, exc_name, retryable
):
    # The table is present but BigQuery refuses the story read: permission
    # denied, a timeout, or a transient outage. Each is the surface being
    # unreadable, reported as its own failure with the desk still rendering,
    # never as a 500 for the whole desk and never as absent.
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()
    reads: list = []

    def refused_read(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            reads.append(sql)
            raise getattr(gexc, exc_name)("refused by BigQuery")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", refused_read)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    response = client.get("/api/desk?region=za")
    assert response.status_code == 200
    data = response.json()
    assert data["pan_african"] == []
    assert data["pan_african_surface"] == {
        "status": "unavailable",
        "error": dict(_PAN_AFRICAN_UNREADABLE, retryable=retryable),
    }
    assert [t["id"] for t in data["topics"]] == ["sports_rugby", "music_amapiano"]
    # The story SELECT reads the pinned table, in the FROM and in the MAX
    # subquery that picks the latest date.
    assert len(reads) == 1
    assert reads[0].count(".pan_african_stories`") == 2
    pan = client.get("/api/desk/pan-african?region=za")
    assert pan.status_code == 200
    assert pan.json() == {
        "region": "za",
        "status": "unavailable",
        "error": dict(_PAN_AFRICAN_UNREADABLE, retryable=retryable),
        "stories": [],
    }
    # No BigQuery error text reaches the client.
    assert "refused by BigQuery" not in response.text
    assert "refused by BigQuery" not in pan.text


def test_desk_pan_african_bad_query_on_read_is_not_retryable(monkeypatch):
    # A BadRequest on the story read is the query being wrong, which the next
    # request will not fix: still dependency_unavailable, labelled not
    # retryable so the desk does not rebuild on every request.
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()

    def bad_read(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            raise gexc.BadRequest("Unrecognized name: momentum_composite")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", bad_read)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    response = client.get("/api/desk?region=za")
    assert response.status_code == 200
    assert response.json()["pan_african_surface"] == {
        "status": "unavailable",
        "error": dict(_PAN_AFRICAN_UNREADABLE, retryable=False),
    }
    assert "momentum_composite" not in response.text


def test_desk_pan_african_probe_forbidden_on_no_rows_branch(monkeypatch):
    # No desk rows for the region and the probe is refused. Before the probe
    # existed this branch made no pan-African call at all, so it must not be
    # the one place a permission fault takes the desk down.
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()

    def dispatcher(sql, params=None):
        if "LEFT JOIN" in sql:
            return []
        if "INFORMATION_SCHEMA.TABLES" in sql:
            raise gexc.Forbidden("Access Denied: Dataset test-project:trends")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", dispatcher)
    monkeypatch.setattr(synth, "_call_model", _no_model)

    response = client.get("/api/desk?region=za")
    assert response.status_code == 200
    data = response.json()
    assert data["topics"] == []
    assert data["updated"] is None
    assert data["pan_african"] == []
    assert data["pan_african_surface"] == {
        "status": "unavailable",
        "error": dict(_PAN_AFRICAN_UNREADABLE, retryable=False),
    }
    assert "Access Denied" not in response.text


def test_desk_pan_african_no_rows_branch_reports_the_surface(monkeypatch):
    # The no rows branch carries the same honest surface as the full build:
    # absent table is surface_missing, present table with a story is ready.
    inner = _desk_dispatcher()

    def absent(sql, params=None):
        if "LEFT JOIN" in sql:
            return []
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", absent)
    monkeypatch.setattr(synth, "_call_model", _no_model)
    data = client.get("/api/desk?region=za").json()
    assert data["topics"] == []
    assert data["pan_african_surface"] == {
        "status": "unavailable",
        "error": {
            "code": "surface_missing",
            "message": (
                "The pan-African stories surface is not provisioned in this dataset."
            ),
            "retryable": False,
        },
    }

    def present(sql, params=None):
        if "LEFT JOIN" in sql:
            return []
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            return [
                {
                    "story_id": "amapiano",
                    "story_label": "Amapiano crosses into West Africa",
                    "markets": ["za", "ng"],
                    "topic_keys": ["music_amapiano"],
                    "total_item_count": 420,
                    "momentum_composite": 0.81,
                }
            ]
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", present)
    main._market_cache.clear()
    data = client.get("/api/desk?region=za").json()
    assert data["topics"] == []
    assert data["pan_african_surface"] == {"status": "ready", "error": None}
    assert [s["label"] for s in data["pan_african"]] == [
        "Amapiano crosses into West Africa"
    ]


def test_desk_pan_african_transient_failure_is_not_cached_for_the_day(monkeypatch):
    # dependency_unavailable says retryable, so the next request must retry
    # it. The desk payload is otherwise cached until UTC midnight, which would
    # pin one refused probe for the whole day.
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()
    probes: list = []

    def refused(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            probes.append("refused")
            raise gexc.ServiceUnavailable("BigQuery is unavailable")
        return inner(sql, params)

    def healthy(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            probes.append("healthy")
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            return []
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", refused)
    monkeypatch.setattr(synth, "_call_model", _no_model)
    first = client.get("/api/desk/pan-african?region=za").json()
    assert first["status"] == "unavailable"
    assert first["error"]["code"] == "dependency_unavailable"

    # BigQuery recovers. The cache is left alone on purpose.
    monkeypatch.setattr(bq, "_run_query", healthy)
    second = client.get("/api/desk/pan-african?region=za").json()
    assert probes == ["refused", "healthy"]
    assert second == {
        "region": "za",
        "status": "no_stories",
        "error": None,
        "stories": [],
    }
    # A healthy payload is cached as before: the third request reads no
    # BigQuery at all.
    third = client.get("/api/desk/pan-african?region=za").json()
    assert third == second
    assert probes == ["refused", "healthy"]


def test_desk_pan_african_missing_surface_stays_cached_for_the_day(monkeypatch):
    # surface_missing is not retryable: an absent table does not appear
    # between two requests, so the round one budget of one probe per build per
    # region holds and the desk is not rebuilt on every request.
    inner = _desk_dispatcher()
    probes: list = []

    def absent(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            probes.append(sql)
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", absent)
    monkeypatch.setattr(synth, "_call_model", _no_model)
    first = client.get("/api/desk/pan-african?region=za").json()
    second = client.get("/api/desk/pan-african?region=za").json()
    assert first["error"]["code"] == "surface_missing"
    assert second == first
    assert len(probes) == 1


def test_desk_pan_african_forbidden_probe_is_permanent_and_cached(monkeypatch):
    # A refused permission does not fix itself between two requests. Treated as
    # retryable it kept the desk out of the cache, so every request rebuilt the
    # whole desk and probed again. It is reported as not retryable and the
    # payload is cached for the normal TTL.
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()
    issued: list = []

    def refused(sql, params=None):
        issued.append(sql)
        if "INFORMATION_SCHEMA.TABLES" in sql:
            raise gexc.Forbidden(
                "Access Denied: Dataset test-project:trends",
                errors=[{"reason": "accessDenied"}],
            )
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", refused)
    monkeypatch.setattr(synth, "_call_model", _no_model)
    first = client.get("/api/desk?region=za").json()
    assert first["pan_african_surface"] == {
        "status": "unavailable",
        "error": dict(_PAN_AFRICAN_UNREADABLE, retryable=False),
    }
    built = len(issued)
    second = client.get("/api/desk?region=za").json()
    assert second["pan_african_surface"] == first["pan_african_surface"]
    assert len([q for q in issued if "INFORMATION_SCHEMA.TABLES" in q]) == 1
    # Only the ingest version that keys the cache is read again.
    assert len(issued) - built <= 1


def test_desk_pan_african_forbidden_read_is_permanent(monkeypatch):
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()

    def refused_read(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return [{"present": 1}]
        if "pan_african_stories" in sql:
            raise gexc.Forbidden("Access Denied: Table pan_african_stories")
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", refused_read)
    monkeypatch.setattr(synth, "_call_model", _no_model)
    surface = client.get("/api/desk?region=za").json()["pan_african_surface"]
    assert surface["error"]["retryable"] is False


def test_desk_pan_african_rate_limited_probe_stays_retryable(monkeypatch):
    # BigQuery answers a rate or quota limit with 403 as well; that one clears
    # on its own, so it stays retryable and is not cached.
    from google.api_core import exceptions as gexc

    inner = _desk_dispatcher()
    probes: list = []

    def limited(sql, params=None):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            probes.append(sql)
            raise gexc.Forbidden(
                "Exceeded rate limits", errors=[{"reason": "rateLimitExceeded"}]
            )
        return inner(sql, params)

    monkeypatch.setattr(bq, "_run_query", limited)
    monkeypatch.setattr(synth, "_call_model", _no_model)
    first = client.get("/api/desk?region=za").json()
    assert first["pan_african_surface"]["error"]["retryable"] is True
    client.get("/api/desk?region=za")
    assert len(probes) == 2


def test_run_query_does_not_retry_a_refused_permission(monkeypatch):
    from google.api_core import exceptions as gexc

    attempts: list = []

    class Client:
        def __init__(self, error):
            self.error = error

        def query(self, sql, job_config=None):
            attempts.append(sql)
            raise self.error

    monkeypatch.setattr(bq.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        bq, "_get_client", lambda: Client(gexc.Forbidden("Access Denied"))
    )
    with pytest.raises(gexc.Forbidden):
        bq._run_query("SELECT 1")
    assert len(attempts) == 1

    attempts.clear()
    monkeypatch.setattr(
        bq,
        "_get_client",
        lambda: Client(
            gexc.Forbidden("rate", errors=[{"reason": "rateLimitExceeded"}])
        ),
    )
    with pytest.raises(gexc.Forbidden):
        bq._run_query("SELECT 1")
    assert len(attempts) == 3

    attempts.clear()
    monkeypatch.setattr(
        bq, "_get_client", lambda: Client(gexc.ServiceUnavailable("outage"))
    )
    with pytest.raises(gexc.ServiceUnavailable):
        bq._run_query("SELECT 1")
    assert len(attempts) == 3


def test_build_bridges_exclusivity_floor():
    rows = [
        _bridge_row("@kabza", "b1", ["music_amapiano"]),
        _bridge_row("@kabza", "b2", ["music_amapiano"]),
        _bridge_row("@kabza", "b3", ["sports_rugby"]),
    ]
    # One exclusive rugby post is under the two-post floor: no bridge.
    assert desk.build_bridges(rows) == []
    # Same-domain pairs never bridge, whatever the volume.
    rows = [
        _bridge_row("@kabza", "b" + str(i), [t])
        for i, t in enumerate(["music_amapiano"] * 2 + ["music_gqom"] * 2)
    ]
    assert desk.build_bridges(rows) == []


def test_build_lexicon_floor_and_wow():
    rows = [
        {"term": "sapa", "market": "ng", "n": 24, "n7": 10, "n_prev": 0},
        {"term": "japa", "market": "ng", "n": 26, "n7": 10, "n_prev": 0},
    ]
    out = desk.build_lexicon(rows)
    # 24 misses the floor; with no prior-week volume the wow field drops.
    assert out == [{"term": "japa", "market": "NG", "n": 26}]


def test_desk_digest_and_receipts(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    data = client.get("/api/desk?region=za").json()

    # The digest is the engine's daily_summary with topic keys humanized.
    assert data["digest"] == {
        "trend_date": "2026-06-30",
        "through_line": "Amapiano in South Africa sets the tempo",
        "summary_text": "Watch Sapa hustle for the counter-mood.",
        "call_to_action": "Track Amapiano into the weekend.",
        "key_topics": [{"query": "music_amapiano", "label": "Amapiano"}],
        "rising_topics": [{"query": "economy_sapa_hustle", "label": "Sapa hustle"}],
    }

    # Receipts lead with named voice posts: platform, @handle, snippet, metric,
    # age (empty when the pool rows carry no stamp), and the source url so the
    # receipt is clickable to the original.
    t = next(x for x in data["topics"] if x["id"] == "music_amapiano")
    assert t["receipts"][0] == [
        "TikTok",
        "@voice0",
        "The log drum on this track is unreal",
        "100 engagement",
        "",
        "https://ex.com/p0",
    ]
    assert len(t["receipts"]) == 3
    assert all(len(r) == 6 for r in t["receipts"])

    # No voice pool: the structured social_refs fill in with the host and carry
    # the real ref url through to the receipt.
    bare = next(x for x in data["topics"] if x["id"] == "sports_rugby")
    assert bare["receipts"] == [
        [
            "News",
            "sarugbymag.co.za",
            "Off-season form guide",
            "",
            "",
            "https://www.sarugbymag.co.za/form-guide",
        ],
    ]


def test_receipts_omitted_when_no_real_rows():
    assert desk._receipts(None, None) == []
    assert desk._receipts([], ["not a url"]) == []
    # Snippets cap at 90 characters.
    long = desk._snippet("word " * 40)
    assert len(long) <= 90
    assert long.endswith("…")
    assert desk._humanize_count(1_200_000) == "1.2m"
    assert desk._humanize_count(42_300) == "42.3k"
    assert desk._humanize_count(318) == "318"


def test_opportunity_cache_and_drop_on_fail(monkeypatch):
    # logger.warning segfaults under pytest on Windows + Py3.13; the failure
    # paths below would hit it, so it is muted for this test only.
    monkeypatch.setattr(synth.logger, "warning", lambda *a, **k: None)
    calls = []

    def ok(prompt, model=None):
        calls.append(prompt)
        return json.dumps({"opportunity": "Hidden angle here."})

    monkeypatch.setattr(synth, "_call_model", ok)
    out = synth.generate_opportunity("za:music_amapiano", "2026-06-10", "syn", "ctx")
    assert out == "Hidden angle here."
    assert "syn" in calls[0]
    assert "ctx" in calls[0]
    # Same (topic, date) serves from the cache: one model call total.
    again = synth.generate_opportunity("za:music_amapiano", "2026-06-10", "syn", "ctx")
    assert again == out
    assert len(calls) == 1

    def bad(prompt, model=None):
        calls.append(prompt)
        raise RuntimeError("model down")

    monkeypatch.setattr(synth, "_call_model", bad)
    assert synth.generate_opportunity("za:other", "2026-06-10", "syn", "ctx") == ""
    # The failure was not cached: a later attempt reaches the model and wins.
    monkeypatch.setattr(synth, "_call_model", ok)
    assert (
        synth.generate_opportunity("za:other", "2026-06-10", "syn", "ctx")
        == "Hidden angle here."
    )

    # Empty inputs never touch the model.
    before = len(calls)
    assert synth.generate_opportunity("za:empty", "2026-06-10", "", "") == ""
    assert len(calls) == before


def test_synth_prompts_require_demographic_evidence_before_audience_attribution():
    audience_rule = getattr(synth, "AUDIENCE_EVIDENCE_RULE", "")
    prompts = (
        synth.build_prompt(
            "street football",
            [
                {
                    "platform": "tiktok",
                    "market": "za",
                    "engagement": 12,
                    "text": "Pickup games",
                }
            ],
            {"total": 1},
        ),
        synth.OPPORTUNITY_PROMPT.format(synthesis="Observed movement", context="ZA"),
        synth.build_brief_prompt("street football", "za"),
        synth.REFINE_PROMPT.format(
            region_name="South Africa",
            topic="street football",
            current="{}",
            instruction="Sharpen the evidence",
        ),
    )

    assert audience_rule
    assert "Do not infer age or demographics" in audience_rule
    for prompt in prompts:
        normalized = prompt.casefold()
        for dash in "‐‑‒–—―":
            normalized = normalized.replace(dash, "-")
        assert "gen z" not in normalized
        assert "gen-z" not in normalized
        assert "youth" not in normalized
        assert "18 to 24" not in normalized
        assert "18-24" not in normalized
        assert audience_rule in prompt


VALID_BRIEF_JSON = json.dumps(
    {
        "region": "NG",
        "label": "Street football culture",
        "momentum": "rising",
        "sentiment": 1.7,
        "velocity": "High",
        "why": "Pickup football clips are everywhere this week.",
        "voices": [
            ["TikTok", "model invented quote one"],
            ["X", "model invented quote two"],
            ["IG", "model invented quote three"],
        ],
        "trend": "Trend prose.",
        "relevance": "Relevance prose.",
        "opportunity": "Opportunity prose.",
        "ideaFormat": "Editorial response",
        "idea": "Idea prose.",
        "visualDirection": "Visual direction.",
        "audioDirection": "Audio direction.",
    }
)


def test_ask_brief_region_forcing_and_grounding(monkeypatch):
    prompts = []

    def fake_call_model(prompt):
        prompts.append(prompt)
        return VALID_BRIEF_JSON

    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(synth, "_call_model", fake_call_model)

    r = _ask_brief_and_wait({"query": "football", "region": "za"})
    assert r.status_code == 200
    data = r.json()

    # Region forcing reaches the prompt and overrides the model's claim.
    assert 'MUST be "ZA"' in prompts[0]
    assert "South Africa" in prompts[0]
    assert data["region"] == "ZA"

    # Voices are grounded: three real retrieved quotes, never the model's.
    assert len(data["voices"]) == 3
    for platform, quote, age in data["voices"]:
        assert platform == "TikTok"
        assert "model invented" not in quote
        assert age == ""
    assert data["voices"][0][1] == "Eish this street football culture is moving fast"
    assert "genz" not in data

    # Every numeric field comes from the real aggregates.
    assert data["mentions"] == 300
    assert data["sources"] == 2
    assert data["creators"] == 1
    # Log-scaled volume: log10(301)/5 rounded, not the saturating total/1000.
    assert data["score"] == 0.496
    assert len(data["series"]) == 30
    assert data["series"][-1] == 1.0
    assert data["series"][0] == 0.667
    assert data["delta"] == 0.0
    assert data["platforms"] == [["TikTok", 200], ["Instagram", 100]]
    assert data["creators_list"] == ["@coolhandle"]

    # Model enums are clamped, prose carried through.
    assert data["sentiment"] == 1.0
    assert data["velocity"] == "High"
    assert data["momentum"] == "rising"
    assert data["brief"]["idea"] == {
        "tool": "Editorial response",
        "text": "Idea prose.",
    }
    assert data["brief"]["prompt"]["nano"] == "Visual direction."
    assert data["thin"] is False
    assert data["generated"] is True

    # Cached by (query, region): a second post answers inline, no model call.
    r2 = client.post("/api/ask/brief", json={"query": "football", "region": "za"})
    assert r2.status_code == 200
    assert r2.json()["generated"] is True
    assert len(prompts) == 1


def test_ask_brief_all_region_lets_model_choose(monkeypatch):
    prompts = []

    def fake_call_model(prompt):
        prompts.append(prompt)
        return VALID_BRIEF_JSON

    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(synth, "_call_model", fake_call_model)

    r = _ask_brief_and_wait({"query": "football", "region": "all"})
    assert r.status_code == 200
    assert "Pick the single most culturally relevant region" in prompts[0]
    assert r.json()["region"] == "NG"


def test_ask_brief_thin_path_skips_model(monkeypatch):
    calls = []

    def fake_call_model(prompt):
        calls.append(prompt)
        return VALID_BRIEF_JSON

    thin = _rich_search_result()
    thin["items"] = [_ask_item(i) for i in range(5)]
    monkeypatch.setattr(bq, "search_content", lambda q, m: thin)
    monkeypatch.setattr(synth, "_call_model", fake_call_model)

    r = _ask_brief_and_wait({"query": "quantum computing", "region": "ke"})
    assert r.status_code == 200
    data = r.json()
    assert data["thin"] is True
    assert calls == []
    assert data["total_matches"] == 300
    assert len(data["volume_series"]) == 30
    assert data["platform_split"][0] == {"platform": "tiktok", "n": 200}
    assert data["creators"] == [{"handle": "coolhandle", "mentions": 9}]


def test_ask_brief_model_failure_records_error_and_retry_works(monkeypatch):
    monkeypatch.setattr(synth.logger, "warning", lambda *a, **k: None)
    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(
        synth, "_call_model", lambda prompt, model=None: "this is not json"
    )

    # A model outage lands as a recorded {"error": true} on the status poll.
    r = _ask_brief_and_wait({"query": "football", "region": "za"})
    assert r.status_code == 200
    assert r.json() == {"error": True}
    # The recorded failure stays out of GCS and never poses as an answer: a
    # fresh ask retries the generation, and the success replaces the error.
    monkeypatch.setattr(
        synth, "_call_model", lambda prompt, model=None: VALID_BRIEF_JSON
    )
    r2 = _ask_brief_and_wait({"query": "football", "region": "za"})
    assert r2.status_code == 200
    assert r2.json()["generated"] is True


def test_ask_brief_cold_returns_202_immediately(monkeypatch):
    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    monkeypatch.setattr(
        synth, "_call_model", lambda prompt, model=None: VALID_BRIEF_JSON
    )
    r = client.post("/api/ask/brief", json={"query": "street football", "region": "ng"})
    assert r.status_code == 202
    assert r.json() == {"pending": True}


def test_ask_brief_status_pending_then_ready_with_duplicate_guard(monkeypatch):
    import threading as _threading

    release = _threading.Event()
    started = []
    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")

    def slow_search(q, m):
        started.append(q)
        assert release.wait(timeout=5.0)
        return _rich_search_result()

    monkeypatch.setattr(bq, "search_content", slow_search)
    monkeypatch.setattr(
        synth, "_call_model", lambda prompt, model=None: VALID_BRIEF_JSON
    )

    assert (
        client.post(
            "/api/ask/brief", json={"query": "football", "region": "za"}
        ).status_code
        == 202
    )
    # While the job runs: status reports pending, and a duplicate ask answers
    # 202 without spawning a second thread for the same (query, region) key.
    s = client.get(
        "/api/ask/brief/status", params={"query": "football", "region": "za"}
    )
    assert s.status_code == 200
    assert s.json() == {"pending": True}
    assert (
        client.post(
            "/api/ask/brief", json={"query": "football", "region": "za"}
        ).status_code
        == 202
    )
    release.set()
    r = _ask_brief_and_wait({"query": "football", "region": "za"})
    assert r.json()["region"] == "ZA"
    assert len(started) == 1


def test_ask_brief_status_validation():
    assert (
        client.get(
            "/api/ask/brief/status", params={"query": "", "region": "za"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/ask/brief/status", params={"query": "x", "region": "uk"}
        ).status_code
        == 400
    )
    # An unknown key is simply not ready yet; the client's deadline bounds it.
    s = client.get(
        "/api/ask/brief/status", params={"query": "never asked", "region": "za"}
    )
    assert s.status_code == 200
    assert s.json() == {"pending": True}


def test_ask_brief_validation():
    assert (
        client.post("/api/ask/brief", json={"query": "", "region": "za"}).status_code
        == 400
    )
    assert (
        client.post("/api/ask/brief", json={"query": "x", "region": "uk"}).status_code
        == 400
    )


def _generated_topic():
    return {
        "id": "ask_amapiano_za",
        "generated": True,
        "region": "ZA",
        "topic": "amapiano",
        "label": "Sound culture",
        "momentum": "rising",
        "score": 0.3,
        "delta": 0.012,
        "mentions": 300,
        "sources": 2,
        "creators": 1,
        "sentiment": 0.4,
        "velocity": "High",
        "why": "Old why line.",
        "series": [0.5] * 30,
        "platforms": [["TikTok", 200]],
        "creators_list": ["@coolhandle"],
        "voices": [["TikTok", "a real retrieved quote"]],
        "brief": {
            "trend": "Old trend.",
            "relevance": "Old relevance.",
            "opportunity": "Old opportunity.",
            "idea": {"tool": "Nano Banana", "text": "Old idea."},
            "prompt": {"nano": "old nano", "lyria": "old lyria"},
        },
    }


REFINE_MODEL_JSON = json.dumps(
    {
        "why": "Sharper why line.",
        "label": "Sound culture",
        "trend": "New trend.",
        "relevance": "New relevance.",
        "opportunity": "New opportunity.",
        "ideaFormat": "Editorial response",
        "idea": "New idea.",
        "visualDirection": "New visual direction.",
        "audioDirection": "New audio direction.",
        "voices": [["TikTok", "model invented quote"]],
        "mentions": 9999,
    }
)


def test_refine_brief_rewrites_prose_and_preserves_evidence(monkeypatch):
    prompts = []

    def fake_call_model(prompt):
        prompts.append(prompt)
        return REFINE_MODEL_JSON

    monkeypatch.setattr(synth, "_call_model", fake_call_model)
    topic = _generated_topic()
    r = client.post(
        "/api/ask/refine", json={"topic": topic, "instruction": "Sharper PR angle"}
    )
    assert r.status_code == 200
    data = r.json()

    # The instruction and the current brief both reach the prompt.
    assert "Sharper PR angle" in prompts[0]
    assert "Old trend." in prompts[0]
    assert "South Africa" in prompts[0]

    # Prose moved.
    assert data["why"] == "Sharper why line."
    assert data["brief"]["trend"] == "New trend."
    assert data["brief"]["idea"] == {"tool": "Editorial response", "text": "New idea."}
    assert data["brief"]["prompt"]["nano"] == "New visual direction."
    assert data["refined"] == 1

    # Evidence never moves: numbers, series, and the real quotes carry over.
    assert data["mentions"] == 300
    assert data["score"] == 0.3
    assert data["series"] == [0.5] * 30
    assert data["voices"] == [["TikTok", "a real retrieved quote"]]
    assert "genz" not in data
    assert data["generated"] is True

    # Never cached: a second identical call reaches the model again.
    r2 = client.post(
        "/api/ask/refine", json={"topic": topic, "instruction": "Sharper PR angle"}
    )
    assert r2.status_code == 200
    assert len(prompts) == 2


def test_refine_brief_validation_and_failure(monkeypatch):
    monkeypatch.setattr(synth.logger, "warning", lambda *a, **k: None)
    topic = _generated_topic()

    # Missing instruction and non-generated topics never reach the model.
    r = client.post("/api/ask/refine", json={"topic": topic, "instruction": "  "})
    assert r.status_code == 400
    tracked = dict(topic)
    tracked.pop("generated")
    r = client.post("/api/ask/refine", json={"topic": tracked, "instruction": "x"})
    assert r.status_code == 400

    # A model failure surfaces as 502.
    monkeypatch.setattr(synth, "_call_model", lambda prompt, model=None: "not json")
    r = client.post("/api/ask/refine", json={"topic": topic, "instruction": "x"})
    assert r.status_code == 502


def test_refine_requires_passcode(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    r = client.post(
        "/api/ask/refine", json={"topic": _generated_topic(), "instruction": "x"}
    )
    assert r.status_code == 401


def test_desk_requires_passcode(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    assert client.get("/api/desk").status_code == 401
    assert (
        client.post("/api/ask/brief", json={"query": "x", "region": "za"}).status_code
        == 401
    )


def _voice_item(i, text, engagement, collected_at=None, handle=None):
    return {
        "id": "v" + str(i),
        "title": "",
        "text": text,
        "platform": "tiktok",
        "market": "za",
        "handle": handle or ("voice" + str(i)),
        "engagement": engagement,
        "collected_at": collected_at,
        "slang_terms": "",
    }


def test_clean_items_recency_outranks_stale_viral():
    now = datetime.now(UTC)
    items = [
        _voice_item(
            0,
            "Stale viral countdown post from weeks back",
            1_000_000,
            now - timedelta(days=20),
        ),
        _voice_item(
            1, "Fresh kickoff reaction from the stands", 10_000, now - timedelta(days=2)
        ),
        _voice_item(
            2, "Mid window take on the squad selection", 50_000, now - timedelta(days=5)
        ),
        _voice_item(3, "Unstamped post with big numbers", 2_000_000, None),
    ]
    cleaned = bq.clean_items(items)
    assert [c["id"] for c in cleaned] == ["v1", "v2", "v3", "v0"]
    # Within the stale bucket, engagement still decides.
    assert cleaned[-2]["id"] == "v3"


def test_clean_items_stale_survives_when_nothing_fresh():
    now = datetime.now(UTC)
    items = [
        _voice_item(
            0, "Old post one still carrying the topic", 500, now - timedelta(days=15)
        ),
        _voice_item(
            1,
            "Old post two still carrying the topic too",
            900,
            now - timedelta(days=12),
        ),
    ]
    cleaned = bq.clean_items(items)
    assert [c["id"] for c in cleaned] == ["v1", "v0"]


def test_build_quotes_age_strings():
    now = datetime.now(UTC)
    items = bq.clean_items(
        [
            _voice_item(
                0,
                "Posted a few hours ago about the match",
                100,
                now - timedelta(hours=3),
            ),
            _voice_item(
                1, "Posted two days ago about the warmup", 90, now - timedelta(days=2)
            ),
            _voice_item(
                2,
                "Posted three weeks ago about the buildup",
                80,
                now - timedelta(days=22),
            ),
            _voice_item(3, "No stamp on this one at all sadly", 70, None),
        ]
    )
    quotes = {q["text"]: q for q in bq.build_quotes(items)}
    assert quotes["Posted a few hours ago about the match"]["age"] == "3h"
    assert quotes["Posted two days ago about the warmup"]["age"] == "2d"
    assert quotes["Posted three weeks ago about the buildup"]["age"] == "3w"
    assert "age" not in quotes["No stamp on this one at all sadly"]


def test_age_prefers_published_at_over_collected_at():
    now = datetime.now(UTC)
    # Posted two days ago, crawled an hour ago: the age must read the post's
    # own stamp, not the crawl time.
    stamped = _voice_item(
        0, "Posted long before the engine found it", 100, now - timedelta(hours=1)
    )
    stamped["published_at"] = now - timedelta(days=2)
    unstamped = _voice_item(
        1, "Only a crawl stamp on this one here", 90, now - timedelta(hours=3)
    )
    quotes = {
        q["text"]: q for q in bq.build_quotes(bq.clean_items([stamped, unstamped]))
    }
    assert quotes["Posted long before the engine found it"]["age"] == "2d"
    assert quotes["Only a crawl stamp on this one here"]["age"] == "3h"
    assert bq.best_stamp({"published_at": None, "collected_at": now}) == now


def test_is_outlet_handle():
    for handle in (
        "asiaone",
        "@asiaone",
        "Reuters_Africa",
        "bbcnews",
        "citizentvkenya",
        "kenya_daily",
        "capitalfm",
        "channelstv",
        "enca_updates",
        "metro_tv",
    ):
        assert bq.is_outlet_handle(handle), handle
    for handle in ("@kabza", "coolhandle", "encanto_fan", "voice1", ""):
        assert not bq.is_outlet_handle(handle), handle


def test_outlet_handles_leave_voices_but_not_counts(monkeypatch):
    result = _rich_search_result()
    result["items"].insert(
        0,
        _voice_item(
            50,
            "Twenty five days to the World Cup kickoff today",
            5_000_000,
            handle="asiaone",
        ),
    )
    result["creators"] = [("asiaone", "tiktok", 40)] + result["creators"]
    monkeypatch.setattr(bq, "search_content", lambda q, m: result)
    monkeypatch.setattr(
        synth, "_call_model", lambda prompt, model=None: VALID_MODEL_JSON
    )

    r = client.get("/api/ask?q=world cup&market=all")
    assert r.status_code == 200
    data = r.json()
    # Aggregates are untouched; the outlet exits only the voice layer.
    assert data["total_matches"] == 300
    assert all("World Cup kickoff" not in q["text"] for q in data["quotes"])
    assert all(c["handle"] != "asiaone" for c in data["creators"])
    assert data["creators"] == [{"handle": "coolhandle", "mentions": 9}]


def test_clean_creator_strings_drops_outlets():
    out = bq.clean_creator_strings(
        [
            "@kabza | tiktok | 9 mentions",
            "@reuters | twitter | 5 mentions",
            "@citizentvkenya | youtube | 4 mentions",
        ]
    )
    assert out == ["@kabza | tiktok | 9 mentions"]


def test_parse_angle_counts_unit():
    sources, creators, stat = desk.parse_angle_counts(
        ["nothing here", "1,204 posts across 14 sources", "8 creators tracked"]
    )
    assert (sources, creators) == (14, 8)
    assert stat == "1,204 posts across 14 sources"
    assert desk.parse_angle_counts(None) == (0, 0, "")
    assert desk.parse_angle_counts(["no stats in this angle"]) == (0, 0, "")


def test_desk_opportunity_endpoint_reads_only_fresh_cached_opportunities(monkeypatch):
    calls = []

    def fake_call_model(prompt, model=None):
        calls.append((prompt, model))
        return json.dumps({"opportunity": "The hidden angle."})

    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    monkeypatch.setattr(synth, "_call_model", fake_call_model)

    # The desk build itself never reaches the model and ships no opportunity.
    data = client.get("/api/desk?region=za").json()
    assert calls == []
    t = next(x for x in data["topics"] if x["id"] == "music_amapiano")
    assert "opportunity" not in t["brief"]

    r = client.get("/api/desk/opportunity/music_amapiano?region=za")
    assert r.status_code == 200
    assert r.json() == {"opportunity": ""}
    assert calls == []

    key = ("opp", "za:music_amapiano", data["updated"])
    synth.cache_set(key, "The hidden angle.")
    stored_at = synth._CACHE[key][0]
    r2 = client.get("/api/desk/opportunity/music_amapiano?region=za")
    assert r2.json() == {"opportunity": "The hidden angle."}
    assert calls == []
    assert synth._CACHE[key][0] == stored_at

    # A fresh desk build now carries the cached opportunity in the brief.
    main._market_cache.clear()
    data = client.get("/api/desk?region=za").json()
    t = next(x for x in data["topics"] if x["id"] == "music_amapiano")
    assert t["brief"]["opportunity"] == "The hidden angle."

    # No synthesis and no context: the lens stays empty, no model call.
    bare = client.get("/api/desk/opportunity/sports_rugby?region=za")
    assert bare.status_code == 200
    assert bare.json() == {"opportunity": ""}
    assert calls == []

    assert client.get("/api/desk/opportunity/nope?region=za").status_code == 404


def test_desk_opportunity_requires_passcode(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    assert (
        client.get("/api/desk/opportunity/music_amapiano?region=za").status_code == 401
    )


def test_desk_opportunity_is_rate_limited(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _desk_dispatcher())
    headers = {"x-forwarded-for": "7.7.7.7"}
    for _ in range(30):
        r = client.get("/api/desk/opportunity/sports_rugby?region=za", headers=headers)
        assert r.status_code == 200
    r = client.get("/api/desk/opportunity/sports_rugby?region=za", headers=headers)
    assert r.status_code == 429


def _empty_desk_payload():
    return {
        "updated": None,
        "freshness": {},
        "digest": None,
        "topics": [],
        "bridges": [],
        "lexicon": [],
    }


def _patch_prewarm_loaders(monkeypatch, desk_build):
    monkeypatch.setattr(main, "_ingest_cache_key", lambda: "test")
    monkeypatch.setattr(desk, "build_desk_payload", desk_build)
    monkeypatch.setattr(bq, "fetch_today", lambda market: {"briefs": []})
    monkeypatch.setattr(bq, "fetch_rails", lambda market: {"rails": []})
    monkeypatch.setattr(creator, "build_voices", lambda region: {"voices": []})
    monkeypatch.setattr(bq, "fetch_growth_metrics", lambda: {"metrics": {}})


def test_prewarm_warms_all_targets(monkeypatch):
    regions = []

    def fake_build(region):
        regions.append(region)
        return _empty_desk_payload()

    _patch_prewarm_loaders(monkeypatch, fake_build)
    r = client.get("/api/prewarm")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["warmed"]["desk:za"] == 0
    for region in ("all", "za", "ng", "ke"):
        for kind in ("desk", "today", "rails", "voices"):
            assert kind + ":" + region in data["warmed"]
    assert "metrics:all" in data["warmed"]
    assert not any(name.startswith("intel_overview:") for name in data["warmed"])
    assert sorted(regions) == ["all", "ke", "ng", "za"]

    # The desk cache is hot: a follow-up desk request builds nothing new.
    assert client.get("/api/desk?region=za").status_code == 200
    assert len(regions) == 4


def test_prewarm_warms_current_topic_profiles(monkeypatch):
    calls = []

    def fake_build(region):
        payload = _empty_desk_payload()
        payload["topics"] = [{"id": f"{region}/music_amapiano"}]
        return payload

    _patch_prewarm_loaders(monkeypatch, fake_build)
    monkeypatch.setattr(
        main,
        "_topic_profile",
        lambda topic_id, region: calls.append((topic_id, region)) or {"id": topic_id},
    )

    data = client.get("/api/prewarm").json()

    assert data["ok"] is True
    for region in ("all", "za", "ng", "ke"):
        assert data["warmed"]["topic_profiles:" + region] == 1
    assert sorted(calls) == [
        ("all/music_amapiano", "all"),
        ("ke/music_amapiano", "ke"),
        ("ng/music_amapiano", "ng"),
        ("za/music_amapiano", "za"),
    ]


def test_prewarm_failure_is_per_target(monkeypatch):
    def fake_build(region):
        if region == "ng":
            raise RuntimeError("bq down")
        return _empty_desk_payload()

    _patch_prewarm_loaders(monkeypatch, fake_build)
    data = client.get("/api/prewarm").json()
    assert data["ok"] is False
    assert data["warmed"]["desk:ng"] is None
    assert data["warmed"]["desk:za"] == 0
    assert data["warmed"]["today:ng"] == 0


def test_prewarm_requires_passcode(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    assert client.get("/api/prewarm").status_code == 401


def test_ask_brief_retries_twice_then_succeeds(monkeypatch):
    monkeypatch.setattr(synth.logger, "warning", lambda *a, **k: None)
    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    calls = []

    def flaky(prompt, model=None):
        calls.append(prompt)
        if len(calls) < 3:
            raise RuntimeError("transient model failure")
        return VALID_BRIEF_JSON

    monkeypatch.setattr(synth, "_call_model", flaky)
    r = _ask_brief_and_wait({"query": "football", "region": "za"})
    assert r.status_code == 200
    assert len(calls) == 3
    assert r.json()["region"] == "ZA"


def test_opportunity_model_env(monkeypatch):
    monkeypatch.delenv("OPPORTUNITY_MODEL", raising=False)
    assert synth._opportunity_model() == "gemini-2.5-flash-lite"
    monkeypatch.setenv("OPPORTUNITY_MODEL", "gemini-2.0-flash")
    assert synth._opportunity_model() == "gemini-2.0-flash"
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("OPPORTUNITY_MODEL", "   ")
    assert synth._opportunity_model() == "gemini-2.5-flash"
    monkeypatch.setenv("OPPORTUNITY_MODEL", "Not A Model!!")
    assert synth._opportunity_model() == "gemini-2.5-flash"


class _FakeBlob:
    def __init__(self, name, store):
        self.name = name
        self.store = store

    def upload_from_string(self, data, content_type=None):
        self.store[self.name] = (data, datetime.now(UTC))

    @property
    def updated(self):
        return self.store[self.name][1]

    def download_as_text(self):
        return self.store[self.name][0]


class _FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, name):
        return _FakeBlob(name, self.store)

    def get_blob(self, name):
        return _FakeBlob(name, self.store) if name in self.store else None


def test_cache_persists_to_gcs_and_rehydrates(monkeypatch):
    bucket = _FakeBucket()
    monkeypatch.setattr(synth, "_gcs_bucket", lambda: bucket)
    key = ("brief", "football", "za")
    synth.cache_set(key, {"a": 1})

    names = list(bucket.store)
    assert len(names) == 1
    assert names[0].startswith("cache/")
    assert names[0].endswith(".json")
    assert json.loads(bucket.store[names[0]][0]) == {"a": 1}

    # A cold instance (empty in-process cache) restores the value from GCS.
    synth._CACHE.clear()
    assert synth.cache_get(key) == {"a": 1}
    assert key in synth._CACHE

    # A blob older than the TTL is a miss.
    bucket.store[names[0]] = (
        bucket.store[names[0]][0],
        datetime.now(UTC) - timedelta(seconds=90000),
    )
    synth._CACHE.clear()
    assert synth.cache_get(key) is None


def test_gcs_cache_failures_are_non_fatal(monkeypatch):
    monkeypatch.setattr(synth.logger, "warning", lambda *a, **k: None)

    class _BrokenBucket:
        def blob(self, name):
            raise RuntimeError("gcs down")

        def get_blob(self, name):
            raise RuntimeError("gcs down")

    monkeypatch.setattr(synth, "_gcs_bucket", lambda: _BrokenBucket())
    synth.cache_set(("k", "v"), {"x": 1})
    assert synth.cache_get(("k", "v")) == {"x": 1}
    synth._CACHE.clear()
    assert synth.cache_get(("k", "v")) is None


def test_safe_key_is_stable_and_distinct():
    a = synth._safe_key(("brief", "football", "za"))
    assert a == synth._safe_key(("brief", "football", "za"))
    assert a != synth._safe_key(("brief", "football", "ng"))
    weird = synth._safe_key(("opp", "za:music_amapiano", "2026-06-10"))
    assert "/" not in weird
    assert ":" not in weird


def _brief_search_result(total):
    res = _rich_search_result()
    res["total_matches"] = total
    return res


def test_ask_brief_score_is_log_scaled_not_saturated(monkeypatch):
    # The old min(0.99, total/1000) flatlined every busy query at 0.99. The log
    # scale must separate volume across orders of magnitude and never saturate
    # until the very top.
    monkeypatch.setattr(
        synth, "_call_model", lambda prompt, model=None: VALID_BRIEF_JSON
    )

    monkeypatch.setattr(bq, "search_content", lambda q, m: _brief_search_result(10))
    low = _ask_brief_and_wait({"query": "lowvol", "region": "za"})
    assert low.json()["score"] == 0.208

    monkeypatch.setattr(bq, "search_content", lambda q, m: _brief_search_result(5000))
    mid = _ask_brief_and_wait({"query": "midvol", "region": "za"})
    assert mid.json()["score"] == 0.74

    monkeypatch.setattr(bq, "search_content", lambda q, m: _brief_search_result(100000))
    high = _ask_brief_and_wait({"query": "highvol", "region": "za"})
    assert high.json()["score"] == 0.99

    # 5000 matches no longer reads the same as 100000.
    assert mid.json()["score"] != high.json()["score"]


def test_compute_momentum_thin_history_is_steady_not_building():
    # A short history (under the 21-day window) padded with leading zeros used
    # to false-positive Building off two or three nonzero days.
    assert synth.compute_momentum([0, 0, 0, 1, 2]) == "Steady"
    assert synth.compute_momentum([3, 4]) == "Steady"
    # A single late spike on an otherwise empty short window is still thin.
    assert synth.compute_momentum([0, 0, 0, 0, 0, 0, 9]) == "Steady"
    # The full-length labels are unchanged by the guard.
    assert synth.compute_momentum([0] * 9 + [2] * 14 + [6] * 7) == "Building"
    assert synth.compute_momentum([0] * 30) == "Steady"


def test_build_lexicon_shrinking_term_is_negative_not_artifact():
    # A term with real prior-week volume that shrank this week yields a sane
    # negative wow, never a divide-by-zero or an inflated positive.
    rows = [{"term": "fade", "market": "ke", "n": 40, "n7": 6, "n_prev": 24}]
    out = desk.build_lexicon(rows)
    assert out == [{"term": "fade", "market": "KE", "n": 40, "wow": -75}]


def test_ask_brief_has_data_flag(monkeypatch):
    monkeypatch.setattr(
        synth, "_call_model", lambda prompt, model=None: VALID_BRIEF_JSON
    )

    # A real series (nonzero peak) reports has_data true.
    monkeypatch.setattr(bq, "search_content", lambda q, m: _rich_search_result())
    live = _ask_brief_and_wait({"query": "football", "region": "za"})
    ld = live.json()
    assert ld["has_data"] is True
    assert any(v > 0 for v in ld["series"])

    # An all-zero series (no volume on any day) is honest: has_data false, and
    # the flat-zero series is still present, not removed.
    flat = _rich_search_result()
    flat["daily_counts"] = {}
    monkeypatch.setattr(bq, "search_content", lambda q, m: flat)
    dead = _ask_brief_and_wait({"query": "flatline", "region": "za"})
    dd = dead.json()
    assert dd["has_data"] is False
    assert dd["series"] == [0.0] * 30


def test_desk_delta_needs_both_latest_and_prior(monkeypatch):
    # Yesterday wrote a row but the latest day did not: the delta must read 0.0,
    # never latest's implicit zero minus a real prior (a fabricated drop).
    today = _today()
    history = [
        {
            "market": "za",
            "query_group": "music_amapiano",
            "trend_date": today - timedelta(days=1),
            "trend_score": 0.40,
        },
        {
            "market": "za",
            "query_group": "music_amapiano",
            "trend_date": today - timedelta(days=3),
            "trend_score": 0.20,
        },
    ]
    series_map, delta_map = desk._series_and_delta(history, today)
    key = ("za", "music_amapiano")
    # Yesterday present, today absent: no fabricated move.
    assert delta_map[key] == 0.0
    # The series itself still carries the real prior days.
    assert series_map[key][-2] == 0.4


def test_freshness_clamps_future_stamp(monkeypatch):
    # A pipeline stamp in the future (clock skew) must not yield a negative age.
    future = datetime.now(UTC) + timedelta(hours=2)

    def fake_run_query(sql, params=None):
        if "pipeline_runs" in sql:
            return [{"stamp": future}]
        raise AssertionError("unexpected sql: " + sql)

    monkeypatch.setattr(bq, "_run_query", fake_run_query)
    fresh = bq._freshness()
    assert fresh["age_hours"] >= 0.0
    assert fresh["status"] == "green"


# Intelligence Centre chat. The model seam (_run_turn) is monkeypatched so the
# loop runs with no Vertex client and no network; the tool sources (bq)
# are stubbed so a tool call reads fixtures, not BigQuery.

from src.api import chat  # noqa: E402


def test_chat_system_contract_is_audience_neutral(monkeypatch):
    prompt_and_tools = chat.SYSTEM_INSTRUCTION + json.dumps(chat.TOOL_SCHEMAS)
    tool_names = {item["name"] for item in chat.TOOL_SCHEMAS}

    assert "Gen Z" not in prompt_and_tools
    assert "Gen-Z" not in prompt_and_tools
    assert "Legacy audience scores are not demographic evidence" in prompt_and_tools
    assert (
        "Do not infer age or demographics from a platform, post, creator, or topic"
        in prompt_and_tools
    )
    assert "demographics" not in " ".join(tool_names)
    assert not any("demographics" in name for name in chat._TOOLS)
    monkeypatch.setattr(
        bq, "search_content", lambda query, market: _rich_search_result()
    )
    result = chat.tool_search_posts("street football", "za")
    assert "avg_genz_score" not in result


def _legacy_chat_result(payload):
    from types import SimpleNamespace
    result = chat.run_chat(payload["message"], payload.get("history", []), payload.get("market", "za"))
    return SimpleNamespace(status_code=200, json=lambda: result)


QUESTION_DEADLINE_AT = "2099-01-01T00:00:00Z"


def _configured_question_routes(monkeypatch, *, fail=False):
    from types import SimpleNamespace
    from src.api.general_question_routes import GeneralQuestionRoutes
    from tests.unit.test_question_worker_result import raw_fixture
    record = json.loads(raw_fixture())
    public = record["response"]
    async def run(operation, payload, **kwargs):
        if fail:
            raise RuntimeError("synthetic worker unavailable")
        if operation == "admit":
            public["intelligence"]["request_id"] = payload["request_id"]
            public["intelligence"]["request_digest"] = "c" * 64
            public["intelligence"]["usage"]["reservation_ids"] = [payload["request_id"]]
            invocation = {key:payload[key] for key in ("request_id","policy_digest","deployment_digest")}
            invocation.update(contract_version="general_cultural_question_v1",request_digest="c"*64,intake_digest="d"*64)
            return SimpleNamespace(engine_reply=SimpleNamespace(reply={"job_id":"chat_"+payload["request_id"].replace("-",""),"invocation":invocation,"deadline_at":QUESTION_DEADLINE_AT}),result_record=None)
        # A status read carries both halves of the worker outcome: the engine
        # reply with the state and the deadline, and the stored result record
        # once the durable request is terminal.
        return SimpleNamespace(
            engine_reply=SimpleNamespace(
                reply={"state": record["state"], "deadline_at": QUESTION_DEADLINE_AT}
            ),
            result_record=record,
        )
    service = GeneralQuestionRoutes(SimpleNamespace(run=run),SimpleNamespace(enqueue=lambda *args,**kwargs:{"state":"verified"}),policy_digest="a"*64,deployment_digest="b"*64)
    monkeypatch.setattr(main.app.state,"general_question_routes",service,raising=False)
    return public


def _chat_send_and_wait(payload: dict):
    """POST a chat turn, assert the 202 + job_id, then poll the status endpoint
    to the resolved response the way the client does."""
    r = client.post("/api/chat/send", json=payload)
    if r.status_code != 202:
        return r
    job_id = r.json()["job_id"]
    assert job_id
    deadline = time.time() + 5.0
    while time.time() < deadline:
        s = client.get("/api/chat/status", params={"job_id": job_id})
        assert s.status_code == 200
        if not s.json().get("pending"):
            return s
        time.sleep(0.01)
    raise AssertionError("the chat turn never resolved through the status endpoint")


def test_legacy_chat_tool_call_then_final_answer(monkeypatch):
    # First model turn asks for a tool, second returns prose: the loop runs the
    # tool, feeds the result back, and resolves to the final answer.
    turns = []

    def fake_run_turn(message, history, turn_state):
        turns.append(message)
        if len(turns) == 1:
            return {"tool_calls": [{"name": "get_trends", "args": {"market": "za"}}]}
        return {"text": "Amapiano leads the desk in South Africa right now."}

    invoked = []

    def fake_desk_rows(market):
        invoked.append(market)
        return [
            {
                "query_group": "music_amapiano",
                "trend_score": 0.41,
                "velocity_score": 0.12,
                "headline": "Amapiano carries the weekend.",
            }
        ]

    monkeypatch.setattr(chat, "_run_turn", fake_run_turn)
    monkeypatch.setattr(bq, "fetch_desk_rows", fake_desk_rows)

    r = _legacy_chat_result(
        {"message": "what is hot in ZA?", "history": [], "market": "za"}
    )
    assert r.status_code == 200
    data = r.json()
    assert data["answer"] == "Amapiano leads the desk in South Africa right now."
    # The tool ran against the right market, and the source is recorded.
    assert invoked == ["za"]
    assert data["sources"] == [{"tool": "trends", "market": "za"}]
    # Two model turns: the tool request and the final answer.
    assert len(turns) == 2



def test_chat_send_returns_job_id_then_status_resolves(monkeypatch):
    expected = _configured_question_routes(monkeypatch)
    result = _chat_send_and_wait({"message":"hello","market":"ng"})
    assert result.status_code == 200
    # The resolved turn is the stored public response exactly, under the
    # lifecycle envelope the status contract adds: the durable request id, the
    # engine state of the stored record and the reason that record carries.
    assert result.json() == expected | {
        "lifecycle": {
            "request_id": expected["intelligence"]["request_id"],
            "state": "unavailable",
            "engine_state": "unavailable",
            "reason_code": "request_expired",
            "deadline_at": QUESTION_DEADLINE_AT,
        }
    }


def test_generation_capacity_is_shared_by_chat_and_ask_brief(monkeypatch):


    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    monkeypatch.setattr(
        main.desk,
        "build_ask_brief",
        lambda query, region: {"query": query, "region": region},
    )

    try:
        assert main._try_acquire_generation_capacity() is True

        response = client.post(
            "/api/ask/brief", json={"query": "street football", "region": "ng"}
        )

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "30"
        assert response.json() == {
            "error": "capacity_busy",
            "message": "The desk is at capacity. Try again shortly.",
            "retry_after_seconds": 30,
        }

        invalid = client.post("/api/ask/brief", json={"query": "", "region": "ng"})
        assert invalid.status_code == 400

        monkeypatch.setenv("UI_PASSCODE", "secret")
        unauthorized = client.post(
            "/api/chat/send", json={"message": "hello", "market": "za"}
        )
        assert unauthorized.status_code == 401
        monkeypatch.delenv("UI_PASSCODE")
    finally:
        main._release_generation_capacity()

    retry = client.post(
        "/api/ask/brief", json={"query": "street football", "region": "ng"}
    )
    assert retry.status_code == 202
    _wait_for_brief_jobs()




def test_generation_capacity_releases_after_failed_chat(monkeypatch, tmp_path):
    from tests.unit.test_general_question_startup import test_worker_failure_releases_the_existing_generation_slot
    test_worker_failure_releases_the_existing_generation_slot(tmp_path, monkeypatch)
    monkeypatch.setattr(main.desk, "build_ask_brief", lambda query, region: {"query": query, "region": region})
    retry = client.post("/api/ask/brief", json={"query": "street football", "region": "ng"})
    assert retry.status_code == 202
    _wait_for_brief_jobs()


def test_archive_read_remains_available_while_chat_occupies_generation(monkeypatch):


    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    monkeypatch.setattr(
        main,
        "_ask_payload_cached",
        lambda query, market: {"query": query, "market": market},
    )

    try:
        assert main._try_acquire_generation_capacity() is True

        response = client.get("/api/ask", params={"q": "amapiano", "market": "za"})

        assert response.status_code == 200
        assert response.json() == {"query": "amapiano", "market": "za"}
        assert main._generation_active == 1
    finally:
        main._release_generation_capacity()




def test_warm_ask_cache_hit_bypasses_generation_capacity(monkeypatch):
    payload = {"query": "amapiano", "market": "za", "thin": False}


    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    synth.cache_set(("amapiano", "za"), payload)

    try:
        assert main._try_acquire_generation_capacity() is True

        response = client.get("/api/ask", params={"q": "amapiano", "market": "za"})

        assert response.status_code == 200
        assert response.json() == payload
    finally:
        main._release_generation_capacity()




def test_archive_read_does_not_consume_generation_capacity(monkeypatch):
    payload = {"query": "amapiano", "market": "za", "thin": False}
    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    monkeypatch.setattr(main, "_ask_payload_cached", lambda query, market: payload)
    monkeypatch.setattr(
        main.desk,
        "build_ask_brief",
        lambda query, region: {"query": query, "region": region},
    )

    response = client.get("/api/ask", params={"q": "amapiano", "market": "za"})
    assert response.status_code == 200
    assert response.json() == payload

    retry = client.post(
        "/api/ask/brief", json={"query": "street football", "region": "ng"}
    )
    assert retry.status_code == 202


def test_failed_archive_read_does_not_consume_generation_capacity(monkeypatch):
    def failed_ask(query, market):
        raise RuntimeError("retrieval unavailable")

    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    monkeypatch.setattr(main, "_ask_payload_cached", failed_ask)
    monkeypatch.setattr(
        main.desk,
        "build_ask_brief",
        lambda query, region: {"query": query, "region": region},
    )

    with pytest.raises(RuntimeError, match="retrieval unavailable"):
        client.get("/api/ask", params={"q": "amapiano", "market": "za"})

    retry = client.post(
        "/api/ask/brief", json={"query": "street football", "region": "ng"}
    )
    assert retry.status_code == 202


def test_generation_launch_failure_releases_capacity_and_job_key(monkeypatch):
    real_start = threading.Thread.start
    launches = 0

    def fail_first_launch(thread):
        nonlocal launches
        if thread._target is main._run_brief_job:
            launches += 1
            if launches == 1:
                raise RuntimeError("thread launch failed")
        return real_start(thread)

    monkeypatch.setenv("GENERATION_MAX_CONCURRENT", "1")
    monkeypatch.setattr(threading.Thread, "start", fail_first_launch)
    monkeypatch.setattr(
        main.desk,
        "build_ask_brief",
        lambda query, region: {"query": query, "region": region},
    )

    try:
        with pytest.raises(RuntimeError, match="thread launch failed"):
            client.post(
                "/api/ask/brief", json={"query": "street football", "region": "ng"}
            )

        retry = client.post(
            "/api/ask/brief", json={"query": "street football", "region": "ng"}
        )
        assert retry.status_code == 202

        resolved = None
        deadline = time.time() + 1
        while time.time() < deadline:
            status = client.get(
                "/api/ask/brief/status",
                params={"query": "street football", "region": "ng"},
            )
            if not status.json().get("pending"):
                resolved = status.json()
                break
            time.sleep(0.01)
        assert resolved == {"query": "street football", "region": "ng"}
    finally:
        with main._brief_jobs_lock:
            main._brief_jobs.clear()
        with main._generation_capacity_lock:
            main._generation_active = 0


def test_legacy_chat_no_tool_short_circuits(monkeypatch):
    # A question the model answers without tools resolves in one turn, no tool
    # call, empty sources.
    calls = []

    def fake_run_turn(message, history, turn_state):
        calls.append(message)
        return {"text": "I read trends for ZA, NG and KE. Ask me about a market."}

    monkeypatch.setattr(chat, "_run_turn", fake_run_turn)
    r = _legacy_chat_result({"message": "what can you do?", "market": "za"})
    assert r.status_code == 200
    assert r.json()["sources"] == []
    assert len(calls) == 1



def test_legacy_chat_top_creators_reads_engine_data(monkeypatch):
    # "Who is driving the market" routes to top_creators, which reads the engine's
    # own author data and labels the source
    # "creators".
    seen = {}

    def fake_authors(market, limit=12):
        seen["market"] = market
        return [
            {"name": "kasi_swenka", "platform": "TikTok", "reach": 5, "mentions": 9}
        ]

    turns = []

    def fake_run_turn(message, history, turn_state):
        turns.append(message)
        if len(turns) == 1:
            return {"tool_calls": [{"name": "top_creators", "args": {"market": "za"}}]}
        return {"text": "kasi_swenka leads on TikTok."}

    monkeypatch.setattr(chat, "_run_turn", fake_run_turn)
    monkeypatch.setattr(bq, "fetch_top_authors", fake_authors)
    r = _legacy_chat_result({"message": "who is driving ZA?", "market": "za"})
    assert r.status_code == 200
    assert seen["market"] == "za"
    assert r.json()["sources"] == [{"tool": "creators", "market": "za"}]



def test_chat_worker_failure_is_controlled(monkeypatch):
    _configured_question_routes(monkeypatch, fail=True)
    result = client.post("/api/chat/send",json={"message":"what is hot?","market":"za"})
    assert result.status_code == 503
    assert result.json() == {"detail":"General question admission is unavailable"}


def test_chat_send_validates_market_and_message(monkeypatch):
    _configured_question_routes(monkeypatch)
    monkeypatch.setattr(
        chat, "_run_turn", lambda message, history, turn_state: {"text": "ok"}
    )
    # "all" is a valid console desk now: the loop fans the model's tool calls
    # across za/ng/ke and reports each distinctly.
    assert (
        client.post(
            "/api/chat/send", json={"message": "hi", "market": "all"}
        ).status_code
        == 202
    )
    # An unknown market is still rejected.
    assert (
        client.post(
            "/api/chat/send", json={"message": "hi", "market": "xx"}
        ).status_code
        == 400
    )
    # An empty message is rejected before any job starts.
    assert (
        client.post(
            "/api/chat/send", json={"message": "   ", "market": "za"}
        ).status_code
        == 400
    )


def test_chat_status_requires_job_id(monkeypatch):
    _configured_question_routes(monkeypatch)
    assert client.get("/api/chat/status").status_code == 400


def test_chat_requires_passcode(monkeypatch):
    monkeypatch.setenv("UI_PASSCODE", "s3cret")
    assert (
        client.post(
            "/api/chat/send", json={"message": "hi", "market": "za"}
        ).status_code
        == 401
    )
    assert (
        client.get("/api/chat/status", params={"job_id": "chat_1"}).status_code == 401
    )


def test_legacy_chat_tool_market_defaults_when_model_omits(monkeypatch):
    # The model requests search_posts without a market; the loop fills the
    # request-level default so the tool never runs marketless.
    seen = {}

    def fake_search(q, m):
        seen["query"] = q
        seen["market"] = m
        return _rich_search_result()

    turns = []

    def fake_run_turn(message, history, turn_state):
        turns.append(message)
        if len(turns) == 1:
            return {
                "tool_calls": [{"name": "search_posts", "args": {"query": "amapiano"}}]
            }
        return {"text": "Amapiano shows strong volume."}

    monkeypatch.setattr(chat, "_run_turn", fake_run_turn)
    monkeypatch.setattr(bq, "search_content", fake_search)
    r = _legacy_chat_result({"message": "search amapiano", "market": "ng"})
    assert r.status_code == 200
    # The market fell back to the request default (ng), and the source reflects it.
    assert seen["market"] == "ng"
    assert r.json()["sources"] == [{"tool": "search_posts", "market": "ng"}]



# ---- Creator profile + voices (the influence layer) ----------------------


def test_fetch_creator_overview_aggregates_markets(monkeypatch):
    rows = [
        {
            "market": "za",
            "platform": "tiktok",
            "posts": 18,
            "reach": 120_000_000,
            "top_eng": 34_000_000,
            "topic_bag": ["music_amapiano", "music_amapiano", "genz_lifestyle"],
            "platforms": ["tiktok"],
        },
        {
            "market": "ng",
            "platform": "instagram",
            "posts": 4,
            "reach": 6_000_000,
            "top_eng": 2_000_000,
            "topic_bag": ["music_amapiano"],
            "platforms": ["instagram"],
        },
    ]
    monkeypatch.setattr(bq, "_run_query", lambda sql, params=None: rows)
    ov = bq.fetch_creator_overview("@emily.112056")
    assert ov["handle"] == "emily.112056"
    # Home market is where the handle commands the most reach.
    assert ov["market"] == "za"
    assert ov["posts"] == 22
    assert ov["reach"] == 126_000_000
    assert ov["top_engagement"] == 34_000_000
    assert ov["platforms"] == [
        bq._platform_label("tiktok"),
        bq._platform_label("instagram"),
    ]
    assert ov["markets"] == ["za", "ng"]
    # The most-common topic key leads the bag.
    assert ov["topic_keys"][0] == "music_amapiano"


def test_fetch_creator_overview_drops_outlet_and_empty(monkeypatch):
    rows = [
        {
            "market": "za",
            "platform": "tiktok",
            "posts": 5,
            "reach": 10,
            "top_eng": 5,
            "topic_bag": [],
            "platforms": ["tiktok"],
        }
    ]
    monkeypatch.setattr(bq, "_run_query", lambda sql, params=None: rows)
    # A news-outlet handle is not a person.
    assert bq.fetch_creator_overview("citizentv") is None
    # No rows in the window returns None so the API can 404.
    monkeypatch.setattr(bq, "_run_query", lambda sql, params=None: [])
    assert bq.fetch_creator_overview("ghost") is None


def test_fetch_reach_ranked_handles_filters_and_orders(monkeypatch):
    rows = [
        {
            "handle": "@kasi_swenka",
            "platform": "tiktok",
            "mentions": 100,
            "reach": 90_000_000,
        },
        {
            "handle": "AutoModerator",
            "platform": "reddit",
            "mentions": 300,
            "reach": 80_000_000,
        },
        {
            "handle": "12345678",
            "platform": "tiktok",
            "mentions": 50,
            "reach": 70_000_000,
        },
        {
            "handle": "@nairobi.notes",
            "platform": "threads",
            "mentions": 20,
            "reach": 30_000_000,
        },
    ]
    monkeypatch.setattr(bq, "_run_query", lambda sql, params=None: rows)
    out = bq.fetch_reach_ranked_handles("za")
    # The bot and the anon numeric id are dropped; named handles keep reach order.
    assert [r["handle"] for r in out] == ["kasi_swenka", "nairobi.notes"]
    assert out[0]["reach"] == 90_000_000
    # An unknown market never touches BigQuery.
    assert bq.fetch_reach_ranked_handles("xx") == []


def test_fetch_creator_reach_series_zero_fills(monkeypatch):
    today = _today()
    rows = [
        {"day": today, "reach": 5_000_000},
        {"day": today - timedelta(days=2), "reach": 1_000_000},
    ]
    monkeypatch.setattr(bq, "_run_query", lambda sql, params=None: rows)
    series = bq.fetch_creator_reach_series("emily.112056")
    assert len(series) == 30
    assert series[-1] == {"date": today.isoformat(), "reach": 5_000_000}
    # The gap day is zero-filled, never interpolated.
    assert series[-2]["reach"] == 0
    assert series[-3]["reach"] == 1_000_000


def test_fetch_voices_series_batches_handles(monkeypatch):
    today = _today()
    rows = [
        {"handle": "emily.112056", "day": today, "reach": 4_000_000},
        {
            "handle": "emily.112056",
            "day": today - timedelta(days=1),
            "reach": 2_000_000,
        },
        {"handle": "joyfulcook_", "day": today, "reach": 1_000_000},
    ]
    monkeypatch.setattr(bq, "_run_query", lambda sql, params=None: rows)
    out = bq.fetch_voices_series(["@Emily.112056", "joyfulcook_"], "za")
    assert set(out) == {"emily.112056", "joyfulcook_"}
    assert len(out["emily.112056"]) == 30
    assert out["emily.112056"][-1] == 4_000_000
    assert out["emily.112056"][-2] == 2_000_000
    assert out["joyfulcook_"][-1] == 1_000_000
    assert out["joyfulcook_"][-2] == 0


def test_voices_endpoint_ranks_and_traps(monkeypatch):
    authors = [
        {
            "name": "emily.112056",
            "platform": "TikTok",
            "mentions": 23,
            "reach": 142_000_000,
            "topics": ["Amapiano"],
        },
        {
            "name": "samic_edit0",
            "platform": "TikTok",
            "mentions": 44,
            "reach": 54_000_000,
            "topics": ["Amapiano"],
        },
    ]
    monkeypatch.setattr(
        bq, "fetch_top_authors", lambda m, limit=40: authors if m == "za" else []
    )
    monkeypatch.setattr(
        bq,
        "fetch_voices_series",
        lambda handles, market, days=30: {
            "emily.112056": [1, 2, 3],
            "samic_edit0": [4, 5, 6],
        },
    )
    r = client.get("/api/voices?region=za")
    assert r.status_code == 200
    d = r.json()
    assert d["market_label"] == "South Africa"
    cr = d["creators"]
    # Ranked by reach, the influence signal, not by post count.
    assert [c["handle"] for c in cr] == ["emily.112056", "samic_edit0"]
    assert cr[0]["posts"] == 23
    assert cr[0]["reach"] == 142_000_000
    assert cr[0]["series"] == [1, 2, 3]
    assert cr[0]["market"] == "ZA"
    # The trap: the busiest poster (samic, 44) is not the reach leader (emily).
    trap = d["trap"]
    assert trap["poster"] == "samic_edit0"
    assert trap["poster_posts"] == 44
    assert trap["poster_rank"] == 2
    assert trap["leader"] == "emily.112056"


def test_voices_caches_board(monkeypatch):
    # The board is static for the UTC day, so a repeat request must serve from
    # cache and not re-query the authors.
    authors = [
        {
            "name": "emily.112056",
            "platform": "TikTok",
            "mentions": 23,
            "reach": 142_000_000,
            "topics": ["Amapiano"],
        }
    ]
    calls = {"authors": 0}

    def fake_authors(m, limit=40):
        if m == "za":
            calls["authors"] += 1
            return authors
        return []

    monkeypatch.setattr(bq, "fetch_top_authors", fake_authors)
    monkeypatch.setattr(
        bq,
        "fetch_voices_series",
        lambda handles, market, days=30: {"emily.112056": [1, 2, 3]},
    )
    first = client.get("/api/voices?region=za").json()
    second = client.get("/api/voices?region=za").json()
    assert first["creators"] == second["creators"]
    assert calls["authors"] == 1


def test_creator_profile_endpoint(monkeypatch):
    monkeypatch.setattr(
        bq,
        "fetch_creator_overview",
        lambda h: {
            "handle": "emily.112056",
            "platform": "TikTok",
            "market": "za",
            "markets": ["za"],
            "posts": 20,
            "reach": 142_000_000,
            "top_engagement": 34_000_000,
            "platforms": ["TikTok"],
            "topic_keys": ["music_amapiano", "genz_lifestyle"],
        },
    )
    monkeypatch.setattr(
        bq,
        "fetch_creator_reach_rank",
        lambda h, m: (2, 120),
    )
    now = datetime.now(UTC)
    monkeypatch.setattr(
        bq,
        "fetch_creator_posts",
        lambda h, limit=60: [
            {
                "id": "p1",
                "title": "",
                "text": "Ray was not ready, the timing on this edit is unreal",
                "url": "https://example.test/creator-post-p1",
                "platform": "tiktok",
                "market": "za",
                "handle": "emily.112056",
                "engagement": 34_000_000,
                "published_at": now - timedelta(days=1),
                "collected_at": now,
                "slang_terms": "",
                "hashtags": "#amapiano #dance",
                "topic_groups": ["music_amapiano"],
            },
            {
                "id": "p2",
                "title": "",
                "text": "Soft life is the survival tactic, romanticise the small wins",
                "platform": "tiktok",
                "market": "za",
                "handle": "emily.112056",
                "engagement": 5_000_000,
                "published_at": now - timedelta(days=3),
                "collected_at": now,
                "slang_terms": "",
                "hashtags": "#softlife",
                "topic_groups": ["genz_lifestyle"],
            },
        ],
    )
    monkeypatch.setattr(
        bq, "fetch_creator_reach_series", lambda h, days=30: [{"date": "d", "reach": 7}]
    )

    r = client.get("/api/creator/emily.112056")
    assert r.status_code == 200
    d = r.json()
    assert d["handle"] == "emily.112056"
    assert d["market_label"] == "South Africa"
    assert d["rank"] == 2
    assert d["rank_total"] == 120
    assert d["reach"] == 142_000_000
    assert d["avg_engagement"] == round(142_000_000 / 20)
    assert d["top_engagement"] == 34_000_000
    assert [t["id"] for t in d["topics"]] == ["music_amapiano", "genz_lifestyle"]
    assert d["topics"][0]["label"] == bq.topic_label("music_amapiano")
    assert d["read"].startswith("Ranks on reach, not volume.")
    assert d["reach_series"] == [{"date": "d", "reach": 7}]
    # The wall is newest first; the top post is the max-engagement row.
    assert [p["id"] for p in d["wall"]] == ["p1", "p2"]
    assert d["wall"][0]["topic_label"] == bq.topic_label("music_amapiano")
    assert d["wall"][0]["hashtags"] == ["#amapiano", "#dance"]
    assert d["top_post"]["engagement"] == 34_000_000
    assert d["top_post"]["id"] == "p1"
    assert d["top_post"]["url"] == "https://example.test/creator-post-p1"
    assert datetime.fromisoformat(d["top_post"]["published_at"]) == now - timedelta(days=1)
    assert datetime.fromisoformat(d["top_post"]["collected_at"]) == now
    assert d["wall"][0]["url"] == d["top_post"]["url"]
    assert d["wall"][1]["url"] is None
    # The derived endpoints reuse the same cached build.
    rs = client.get("/api/creator/emily.112056/reach-series")
    assert rs.json()["series"] == [{"date": "d", "reach": 7}]
    pw = client.get("/api/creator/emily.112056/posts")
    assert pw.json()["count"] == 2


def test_creator_profile_404_when_no_rows(monkeypatch):
    monkeypatch.setattr(bq, "fetch_creator_overview", lambda h: None)
    assert client.get("/api/creator/ghost").status_code == 404
    assert client.get("/api/creator/ghost/posts").status_code == 404


# ---- Topic story (desk brief merged with live aggregates) ----------------


def test_topic_profile_endpoint(monkeypatch):
    today = _today()
    monkeypatch.setattr(
        desk,
        "build_desk_payload",
        lambda region: {
            "topics": [
                {
                    "id": "music_amapiano",
                    "region": "ZA",
                    "topic": "Amapiano",
                    "label": "Music · South Africa",
                    "momentum": "rising",
                    "score": 0.41,
                    "mentions": 128,
                    "sources": 6,
                    "creators": 12,
                    "sentiment": 0.3,
                    "velocity": "High",
                    "age": "2h",
                    "why": "The log drum graduated into a mood layer.",
                    "series": [0.3] * 30,
                    "platforms": [["TikTok", 40]],
                    "creators_list": ["@kabza"],
                    "voices": [["TikTok", "log drum hits different", "3h"]],
                    "brief": {
                        "trend": "T",
                        "relevance": "R",
                        "idea": {"tool": "Nano Banana", "text": "I"},
                        "prompt": {"nano": "n", "lyria": "l"},
                    },
                    "receipts": [["TikTok", "@kabza", "snip", "9k engagement", "3h"]],
                }
            ]
        },
    )
    monkeypatch.setattr(
        bq,
        "fetch_topic_daily",
        lambda t, m: [
            {"day": today, "n": 5, "reach": 1_000_000},
            {"day": today - timedelta(days=2), "n": 3, "reach": 500_000},
        ],
    )
    monkeypatch.setattr(
        bq,
        "fetch_market_daily",
        lambda m: [
            {"day": today, "n": 20},
            {"day": today - timedelta(days=2), "n": 10},
        ],
    )
    now = datetime.now(UTC)
    monkeypatch.setattr(
        bq,
        "fetch_voice_pools",
        lambda topics, m: {
            "music_amapiano": [
                {
                    "id": "p1",
                    "title": "",
                    "text": "Log drum set tonight was unreal energy in the room",
                    "platform": "tiktok",
                    "market": "za",
                    "handle": "kabza",
                    "engagement": 900000,
                    "published_at": now - timedelta(days=1),
                    "collected_at": now,
                    "slang_terms": "amapiano, log drum",
                    "url": "https://www.tiktok.com/@kabza/video/p1",
                    "topic_groups": ["music_amapiano", "genz_lifestyle"],
                }
            ]
        },
    )
    monkeypatch.setattr(
        bq,
        "derive_topic_hashtags",
        lambda m: {
            ("za", "music_amapiano"): [
                {"tag": "#amapiano", "share_pct": 41.0, "mood": ""}
            ]
        },
    )
    monkeypatch.setattr(
        bq,
        "fetch_topic_channel_sov",
        lambda t, m: [
            {"platform": "TikTok", "n": 40, "sov_pct": 8.0},
            {"platform": "Instagram", "n": 12, "sov_pct": 2.5},
        ],
    )
    monkeypatch.setattr(
        bq,
        "fetch_topic_tone_distribution",
        lambda t, m: {"positive": 30, "negative": 10, "neutral": 60, "n": 100},
    )

    r = client.get("/api/topic/music_amapiano?region=za")
    assert r.status_code == 200
    d = r.json()
    assert d["topic"] == "Amapiano"
    assert d["reach"] == 1_500_000
    assert d["mentions"] == 8
    assert len(d["sov_series"]) == 30
    assert len(d["volume_series"]) == 30
    # SoV latest = topic today / market today = 5 / 20 = 25%.
    assert d["sov_series"][-1] == {"date": today.isoformat(), "sov_pct": 25.0}
    assert d["sov"] == 25.0
    assert d["sov_by_channel"] == [
        {"platform": "TikTok", "n": 40, "sov_pct": 8.0},
        {"platform": "Instagram", "n": 12, "sov_pct": 2.5},
    ]
    assert d["media_tone_dist"] == {
        "positive": 30,
        "negative": 10,
        "neutral": 60,
        "n": 100,
    }
    assert d["tags"] == ["#amapiano"]
    assert d["slang"][:2] == ["amapiano", "log drum"]
    assert d["brief"]["trend"] == "T"
    assert d["voices"] == [["TikTok", "log drum hits different", "3h"]]
    assert "genz" not in d
    assert [p["id"] for p in d["wall"]] == ["p1"]
    # The wall card carries the source url for "view original" and lists the
    # post's other topic as a cross-tag, with the current topic stripped out.
    assert d["wall"][0]["url"] == "https://www.tiktok.com/@kabza/video/p1"
    assert (
        len(d["wall"][0]["also"]) == 1
        and "music" not in d["wall"][0]["also"][0].lower()
    )
    # The series endpoints reuse the same cached build.
    sov = client.get("/api/topic/music_amapiano/sov-series?region=za").json()["series"]
    assert sov[-1]["sov_pct"] == 25.0
    vol = client.get("/api/topic/music_amapiano/momentum-series?region=za").json()[
        "series"
    ]
    assert vol[-1]["n"] == 5


def test_topic_profile_404_when_absent(monkeypatch):
    monkeypatch.setattr(desk, "build_desk_payload", lambda region: {"topics": []})
    monkeypatch.setattr(bq, "fetch_topic_daily", lambda t, m: [])
    assert client.get("/api/topic/ghost_topic?region=za").status_code == 404


def test_topic_profile_overlaps_independent_data_reads(monkeypatch):
    active = 0
    peak = 0
    started = 0
    lock = threading.Lock()
    all_started = threading.Event()

    def overlap(value):
        def read(*args):
            nonlocal active, peak, started
            with lock:
                active += 1
                started += 1
                peak = max(peak, active)
                if started == 6:
                    all_started.set()
            all_started.wait(timeout=0.1)
            with lock:
                active -= 1
            return value

        return read

    monkeypatch.setattr(bq, "fetch_topic_daily", overlap([]))
    monkeypatch.setattr(bq, "fetch_market_daily", overlap([]))
    monkeypatch.setattr(bq, "fetch_voice_pools", overlap({}))
    monkeypatch.setattr(bq, "derive_topic_hashtags", overlap({}))
    monkeypatch.setattr(bq, "fetch_topic_channel_sov", overlap([]))
    monkeypatch.setattr(bq, "fetch_topic_tone_distribution", overlap({}))

    result = topic.build_topic_profile(
        "music_amapiano", "za", {"id": "music_amapiano", "region": "ZA"}
    )

    assert result is not None
    assert peak == 6


def test_listen_endpoint(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(
        bq,
        "search_content",
        lambda q, m: {
            "total_matches": 42,
            "items": [
                {
                    "id": "x1",
                    "title": "",
                    "text": "Castle Lager braai season is here, who is pulling up this weekend",
                    "platform": "tiktok",
                    "market": "za",
                    "handle": "fan",
                    "url": "https://t.co/x1",
                    "engagement": 5000,
                    "published_at": now,
                    "collected_at": now,
                    "slang_terms": "",
                }
            ],
        },
    )
    monkeypatch.setattr(
        bq,
        "fetch_listen",
        lambda q, m: {
            "sov_by_channel": [{"platform": "TikTok", "n": 30, "sov_pct": 6.0}],
            "media_tone": 0.12,
            "tone_n": 8,
            "total_matches": 30,
        },
    )
    d = client.get("/api/listen?q=castle%20lager&region=za").json()
    assert d["query"] == "castle lager"
    assert d["total_matches"] == 42
    assert d["sov_by_channel"][0]["platform"] == "TikTok"
    assert d["media_tone"] == 0.12 and d["tone_n"] == 8
    assert [p["id"] for p in d["wall"]] == ["x1"]
    assert d["wall"][0]["url"] == "https://t.co/x1"
    assert d["wall"][0]["platform"] == bq._platform_label("tiktok")
    # The plain mock post carries no hashtag or mention, so entities are empty
    # but the structure is always present.
    assert d["entities"] == {"hashtags": [], "handles": []}


def test_listen_requires_query():
    assert client.get("/api/listen?q=%20&region=za").status_code == 400


def test_top_entities_counts_hashtags_and_mentions():
    items = [
        {
            "text": "Braai season #braai @castlelager so good",
            "title": "",
            "hashtags": "#braai,#summer",
        },
        {"text": "another #braai post @castlelager", "title": "", "hashtags": ""},
    ]
    out = bq.top_entities(items)
    tags = {h["tag"]: h["n"] for h in out["hashtags"]}
    handles = {h["handle"]: h["n"] for h in out["handles"]}
    assert tags["#braai"] == 3 and tags["#summer"] == 1
    assert handles["@castlelager"] == 2


def test_lexicon_term_endpoint(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(
        bq,
        "fetch_lexicon_term_posts",
        lambda term, market, limit=60: [
            {
                "id": "l1",
                "title": "",
                "text": "Sapa caught me in 4K this month but we move regardless",
                "platform": "tiktok",
                "market": "ng",
                "handle": "naijacomic",
                "engagement": 1_900_000,
                "published_at": now - timedelta(days=1),
                "collected_at": now,
                "slang_terms": "sapa, japa",
            }
        ],
    )
    r = client.get("/api/lexicon/sapa?region=ng")
    assert r.status_code == 200
    d = r.json()
    assert d["term"] == "sapa"
    assert d["post_count"] == 1
    assert d["posts"][0]["platform"] == bq._platform_label("tiktok")
    assert d["posts"][0]["market"] == "NG"


# --- chat-turn observability (system_events) ----------------------------


def test_run_chat_records_ok_event(monkeypatch):
    from src.api import chat as chatmod

    monkeypatch.setattr(chatmod, "_run_turn", lambda m, h, t: {"text": "the read"})
    recorded = {}
    monkeypatch.setattr(
        chatmod, "record_event", lambda *a, **k: recorded.update(args=a, kw=k)
    )
    out = chatmod.run_chat("hi", [], "za")
    assert out["answer"] == "the read"
    assert recorded["args"][0] == "lp_chat"
    assert recorded["args"][1] == "INFO"
    assert recorded["kw"]["status"] == "ok"
    assert "latency_ms" in recorded["kw"]


def test_run_chat_records_error_and_reraises(monkeypatch):
    from src.api import chat as chatmod

    def boom(*a, **k):
        raise RuntimeError("vertex down")

    monkeypatch.setattr(chatmod, "_run_turn", boom)
    recorded = {}
    monkeypatch.setattr(
        chatmod, "record_event", lambda *a, **k: recorded.update(args=a, kw=k)
    )
    with pytest.raises(RuntimeError):
        chatmod.run_chat("hi", [], "za")
    assert recorded["args"][1] == "ERROR"
    assert recorded["kw"]["status"] == "failed"
    assert recorded["kw"]["fatal"] is True
    assert recorded["kw"]["error_detail"] == "RuntimeError"


def test_chat_diagnostics_do_not_log_question_or_exception_content(monkeypatch, caplog):
    from src.api import chat as chatmod
    events = []
    monkeypatch.setattr(chatmod, "record_event", lambda *args, **kwargs: events.append(kwargs))
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_EXCEPTION_CONTENT")
    monkeypatch.setattr(chatmod, "_run_chat_inner", fail)
    monkeypatch.setattr(synth, "cache_set", lambda *args: None)
    monkeypatch.setattr(main, "_release_generation_capacity", lambda: None)
    main._run_chat_job(("chat", "job_diagnostic"), "PRIVATE_QUESTION_CONTENT", [], "za")
    captured = repr(events) + caplog.text
    assert "PRIVATE_QUESTION_CONTENT" not in captured
    assert "PRIVATE_EXCEPTION_CONTENT" not in captured
    assert "RuntimeError" in captured
    assert "job_diagnostic" in caplog.text


def test_record_event_skips_write_under_pytest():
    from src.api import events as ev

    # PYTEST_CURRENT_TEST is set during the run, so the writer short-circuits
    # before touching BigQuery: no network, no junk row, never raises.
    ev.record_event("lp_chat", "ERROR", "chat_turn", status="failed", fatal=True)


def test_chat_all_desk_forces_first_tool_call(monkeypatch):
    # The ALL desk brief must pull data, never answer from memory. The opening
    # round is forced (mode=ANY); later rounds are AUTO so the model can finalize.
    from src.api import chat as chatmod

    seen_force = []

    def fake_run_turn(message, history, turn_state, force_tools=False):
        seen_force.append(force_tools)
        if len(seen_force) == 1:
            return {"tool_calls": [{"name": "get_trends", "args": {"market": "za"}}]}
        return {"text": "ZA: **Amapiano** (follow, Lyria)."}

    monkeypatch.setattr(chatmod, "_run_turn", fake_run_turn)
    monkeypatch.setattr(chatmod, "_run_tool", lambda name, args: {"ok": True})
    monkeypatch.setattr(chatmod, "record_event", lambda *a, **k: None)

    out = chatmod.run_chat("what should we brief this week?", [], "all")
    assert seen_force[0] is True
    assert seen_force[1] is False
    assert out["sources"] == [{"tool": "trends", "market": "za"}]


def test_chat_single_market_does_not_force_tools(monkeypatch):
    # A single-market turn stays AUTO so meta questions can answer without a tool.
    from src.api import chat as chatmod

    seen_force = []

    def fake_run_turn(message, history, turn_state, force_tools=False):
        seen_force.append(force_tools)
        return {"text": "I read ZA, NG and KE. Ask me about a market."}

    monkeypatch.setattr(chatmod, "_run_turn", fake_run_turn)
    monkeypatch.setattr(chatmod, "record_event", lambda *a, **k: None)

    chatmod.run_chat("what can you do?", [], "za")
    assert seen_force[0] is False


def test_system_instruction_forbids_internal_leak():
    # The analyst must never expose its plumbing (tool names, the market arg, the
    # desk-context line) to a stakeholder. Regression guard for the leak where it
    # answered "how do we use it" by explaining get_trends(market='za').
    from src.api.chat import SYSTEM_INSTRUCTION

    si = SYSTEM_INSTRUCTION.lower()
    assert "never reveal your own plumbing" in si
    assert "get_trends" in si  # named so the model is told NOT to say it
    assert "product level" in si


# --- seeding journey (seed intelligence) --------------------------------


def _seed_row(rank=1):
    return {
        "trend_date": "2026-06-22",
        "rank": rank,
        "behaviour": "Premiumising everyday rituals into status flexes",
        "the_shift": "Survival reframed as aspiration.",
        "evidence": [
            "za/food_rituals_braai - gourmet braai as a flex",
            "ke/food_nyamachoma - elevated roadside grilling",
            "a free-form note with no ref",
        ],
        "why_hidden": "Brands read these as hardship.",
        "timing": "Seed before the festive cycle.",
        "markets": ["za", "ke", "ng"],
        "brand_opportunity": "Validate the hustle as status.",
        "activation_tool": "Nanobanana",
        "activation_angle": "Premium ritual mood boards.",
        "activation_prompt": "A high-fashion photo... Created with Gemini",
        "signal_strength": "strong",
    }


def test_seeds_payload_parses_evidence(monkeypatch):
    from src.api import bq, seeds

    monkeypatch.setattr(bq, "fetch_seed_insights", lambda: [_seed_row()])
    payload = seeds.build_seeds_payload()
    assert payload["date"] == "2026-06-22"
    s = payload["seeds"][0]
    assert s["behaviour"].startswith("Premiumising")
    assert s["activation"]["tool"] == "Nanobanana"
    # the two ref lines parse to linkable topics, the third stays a plain note
    refs = [e["topic"] for e in s["evidence"] if e["topic"]]
    assert refs == ["food_rituals_braai", "food_nyamachoma"]
    assert s["evidence"][0]["market"] == "za"
    assert any(e["ref"] is None for e in s["evidence"])


def test_seeds_endpoint_returns_payload(monkeypatch):
    from src.api import bq

    monkeypatch.setattr(bq, "fetch_seed_insights", lambda: [_seed_row()])
    r = client.get("/api/seeds", headers={"X-Passcode": ""})
    assert r.status_code == 200
    assert r.json()["seeds"][0]["behaviour"].startswith("Premiumising")


def test_chat_get_seeds_tool_returns_behaviours(monkeypatch):
    from src.api import chat as chatmod
    from src.api import seeds as seeds_mod

    monkeypatch.setattr(
        seeds_mod,
        "build_seeds_payload",
        lambda: {
            "date": "2026-06-22",
            "seeds": [
                {
                    "behaviour": "Premiumising everyday rituals",
                    "the_shift": "x",
                    "why_hidden": "y",
                    "timing": "now",
                    "markets": ["za"],
                    "evidence": [
                        {"ref": "za/food_rituals_braai", "topic": "food_rituals_braai"}
                    ],
                    "activation": {"tool": "Nanobanana", "angle": "a", "prompt": "p"},
                    "signal_strength": "strong",
                }
            ],
        },
    )
    out = chatmod.tool_get_seeds()
    assert out["seeds"][0]["behaviour"] == "Premiumising everyday rituals"
    assert out["seeds"][0]["activation_surface"] == "Nanobanana"
    assert out["seeds"][0]["evidence"] == ["za/food_rituals_braai"]


# ---- Performance: desk TTL, channel totals cache, creator rank ------------


def test_desk_cache_ttl_covers_rest_of_utc_day():
    ttl = main._market_cache_ttl_seconds("desk")
    assert ttl >= 60.0
    assert ttl <= 86400.0
    assert main._market_cache_ttl_seconds("metrics") == main.MARKET_CACHE_TTL_SECONDS


def test_market_channel_totals_cached(monkeypatch):
    calls = {"n": 0}

    def fake_run(sql, params=None):
        calls["n"] += 1
        return [{"platform": "tiktok", "n": 10}]

    monkeypatch.setattr(bq, "_run_query", fake_run)
    bq._channel_totals_cache.clear()
    assert bq._market_channel_totals("za")["tiktok"] == 10
    assert bq._market_channel_totals("za")["tiktok"] == 10
    assert calls["n"] == 1


def test_fetch_creator_reach_rank_returns_rank(monkeypatch):
    monkeypatch.setattr(
        bq,
        "_run_query",
        lambda sql, params=None: [{"rank": 3, "total": 120}],
    )
    rank, total = bq.fetch_creator_reach_rank("creatorx", "za")
    assert rank == 3
    assert total == 120


# ---- Security headers + cache stampede + client timeout ------------------


def test_enforcing_csp_allows_the_shipped_font_sources():
    r = client.get("/api/health")
    assert "Content-Security-Policy" in r.headers
    assert "Content-Security-Policy-Report-Only" not in r.headers
    assert r.headers["strict-transport-security"] == "max-age=31536000"
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp
    assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com" in csp
    assert "font-src 'self' https://fonts.gstatic.com" in csp
    assert "img-src 'self' data:" in csp
    assert "connect-src 'self'" in csp
    assert "base-uri 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_both_page_policies_let_the_client_read_frame_use_its_embedded_face():
    """The approved Client Read carries its face as a data URL and is shown in
    a sandboxed srcdoc frame, which inherits the page's policy. Without data:
    in font-src the face is refused and the frame falls back to a request."""
    for policy in (main.CSP, main.REVIEW_SIGNIN_CSP):
        directive = next(
            part.strip() for part in policy.split(";") if part.strip().startswith("font-src")
        )
        assert directive == "font-src 'self' https://fonts.gstatic.com data:"


def test_cors_allows_known_origin():
    # An allowed origin gets echoed back in Access-Control-Allow-Origin, with
    # credentials enabled, proving the CORS middleware is active.
    origin = main._DEFAULT_ALLOWED_ORIGINS[0]
    r = client.get("/api/health", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_cors_rejects_unknown_origin():
    # A disallowed origin gets no Access-Control-Allow-Origin header back, so a
    # rogue site cannot read responses cross-origin.
    r = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_market_cache_loader_runs_once_under_concurrent_same_key(monkeypatch):
    # Concurrent requests for the same key must not both miss and both run the
    # slow loader; only the first runs it, the rest wait on that key's lock.
    main._market_cache.clear()
    main._market_cache_locks.clear()
    calls = {"n": 0}
    start = threading.Event()

    def slow_loader():
        calls["n"] += 1
        # Hold the key long enough that every thread is past the first check.
        time.sleep(0.2)
        return {"loaded": calls["n"]}

    def worker(results, idx):
        start.wait()
        results[idx] = main._market_cached("today", "za", slow_loader)

    results: dict = {}
    threads = [threading.Thread(target=worker, args=(results, i)) for i in range(8)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    assert calls["n"] == 1
    # Every caller got the same cached payload.
    assert all(r == {"loaded": 1} for r in results.values())


def test_chat_client_sets_request_timeout(monkeypatch):
    # The Vertex client is built with an http timeout so a hung call fails
    # instead of pinning the daemon job thread forever.
    from google import genai
    from google.genai import types

    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setattr(chat, "_client", None)
    chat._get_client()
    http_options = captured.get("http_options")
    assert isinstance(http_options, types.HttpOptions)
    assert http_options.timeout == chat.CLIENT_TIMEOUT_MS
    # 60 seconds, expressed in milliseconds.
    assert chat.CLIENT_TIMEOUT_MS == 60000
    # Reset the module-level client so no fake leaks into later tests.
    chat._client = None


def test_api_seed_path_requires_keyword(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    r = client.get("/api/seed-path?market=za")
    assert r.status_code == 400


def test_api_seed_path_shape(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.setattr(
        bq,
        "fetch_seed_graph_adjacency",
        lambda kw, mk: {
            "keyword": kw,
            "market": mk,
            "co_occur_terms": [],
            "found": False,
        },
    )
    monkeypatch.setattr(
        bq,
        "fetch_seed_path",
        lambda kw, mk: {"term": kw, "market": mk, "channels": [], "confidence": "thin"},
    )
    r = client.get("/api/seed-path?keyword=amapiano&market=za")
    assert r.status_code == 200
    body = r.json()
    assert body["keyword"] == "amapiano"
    assert "adjacency" in body and "path" in body


def test_api_market_topics(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.setattr(
        bq,
        "fetch_market_topics",
        lambda markets, per_market=10, trend_date=None: [
            {
                "market": "za",
                "query_group": "music_amapiano",
                "label": "Amapiano",
                "post_count": 12,
            }
        ],
    )
    r = client.get("/api/market-topics?markets=za")
    assert r.status_code == 200
    assert r.json()["topics"][0]["query_group"] == "music_amapiano"


# --- The released run reaches Open Discover -----------------------------------
#
# Approved producer contract, verification gates 4 to 7: a qualifying released
# run with zero admitted signals is no_discovery, a count mismatch is
# run_integrity_failed, an invalid released candidate poisons the whole run,
# and one valid signal returns the exact shape. The receipt, counts and view
# reads are faked at the query layer, exactly as the other desk tests do.


_RUN_RECEIPT_ROW = {
    "run_id": "run_20260827_dynamic_apply_v1",
    "signal_date": date(2026, 8, 27),
    "market_scope": ["za", "ng", "ke"],
    "observation_start": date(2026, 8, 27),
    "observation_end": date(2026, 8, 27),
    "observation_method": "dynamic_source_copy_apply_v1",
    "completed_at": datetime(2026, 8, 29, 19, 3, 28, tzinfo=UTC),
    "row_set_digest": "5de7941a4f6f2827800972889cc9369db0fbe95b1a1bf8a4103afe2a41942f4f",
    "candidate_count": 0,
    "evidence_count": 0,
    "membership_count": 0,
    "lineage_count": 0,
    "analysis_count": 0,
    "prediction_count": 0,
}

_ZERO_COUNTS_ROW = {
    "candidate_count": 0,
    "evidence_count": 0,
    "membership_count": 0,
    "lineage_count": 0,
    "analysis_count": 0,
    "prediction_count": 0,
}


def _signal_view_row(**over):
    row = {
        "contract_version": "desk_dynamic_signal_v2",
        "run_id": "run_20260827_dynamic_apply_v1",
        "signal_date": date(2026, 8, 27),
        "market": "za",
        "signal_id": "sig_" + "a" * 64,
        "signal_name": "Weekend repair meetups",
        "discovery_mode": "phrase",
        "evidence_state": "ready",
        "why_now": None,
        "possible_response": None,
        "observation_start": date(2026, 8, 27),
        "observation_end": date(2026, 8, 27),
        "observation_method": "dynamic_source_copy_apply_v1",
        "receipts": [],
        "velocity_score": 0.67,
        "novelty_score": 0.66,
        "breadth_score": 0.34,
        "independence_score": 0.33,
        "historical_similarity": None,
        "geo_confidence": 1.0,
        "topic_tags": ["repair", "participation"],
    }
    row.update(over)
    return row


def _released_dispatcher(receipt=None, counts=None, view_rows=None):
    inner = _desk_dispatcher()

    def dispatch(sql, params=None):
        if "open_intelligence_run_receipts_v1" in sql:
            return [dict(receipt or _RUN_RECEIPT_ROW)]
        if "v_desk_dynamic_signals_v2" in sql:
            return [dict(row) for row in (view_rows or [])]
        if "candidate_count" in sql:
            return [dict(counts or _ZERO_COUNTS_ROW)]
        return inner(sql, params)

    return dispatch


def test_a_released_run_with_zero_admitted_signals_is_no_discovery(monkeypatch):
    monkeypatch.setattr(bq, "_run_query", _released_dispatcher())
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = client.get("/api/desk?region=za").json()["dynamic_discovery"]
    assert dynamic["status"] == "no_discovery"
    assert dynamic["signals"] == []
    assert dynamic["error"] is None
    run = dynamic["run"]
    assert run["run_id"] == "run_20260827_dynamic_apply_v1"
    assert run["signal_date"] == "2026-08-27"
    assert run["market_scope"] == ["za", "ng", "ke"]
    assert run["observation_start"] == "2026-08-27"
    assert run["observation_end"] == "2026-08-27"
    assert run["observation_method"] == "dynamic_source_copy_apply_v1"
    assert run["closed_at"].startswith("2026-08-29T19:03:28")
    assert run["closed_at"].endswith("Z") or run["closed_at"].endswith("+00:00")


def test_a_count_mismatch_makes_the_run_integrity_failed(monkeypatch):
    counts = dict(_ZERO_COUNTS_ROW, candidate_count=3)
    monkeypatch.setattr(bq, "_run_query", _released_dispatcher(counts=counts))
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = client.get("/api/desk?region=za").json()["dynamic_discovery"]
    assert dynamic["status"] == "unavailable"
    assert dynamic["run"] is None
    assert dynamic["signals"] == []
    assert dynamic["error"]["code"] == "run_integrity_failed"
    assert dynamic["error"]["retryable"] is False


def test_an_invalid_released_candidate_poisons_the_whole_run(monkeypatch):
    """The producer never turns invalid rows into a valid result."""
    receipt = dict(_RUN_RECEIPT_ROW, candidate_count=1, prediction_count=1)
    counts = dict(_ZERO_COUNTS_ROW, candidate_count=1, prediction_count=1)
    bad = _signal_view_row(signal_id="not_a_signal_id")
    monkeypatch.setattr(
        bq, "_run_query", _released_dispatcher(receipt, counts, [bad])
    )
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = client.get("/api/desk?region=za").json()["dynamic_discovery"]
    assert dynamic["status"] == "unavailable"
    assert dynamic["run"] is None
    assert dynamic["signals"] == []
    assert dynamic["error"]["code"] == "signal_contract_violation"
    assert dynamic["error"]["retryable"] is False


def test_duplicate_signal_identity_poisons_the_whole_run(monkeypatch):
    receipt = dict(_RUN_RECEIPT_ROW, candidate_count=2, prediction_count=2)
    counts = dict(_ZERO_COUNTS_ROW, candidate_count=2, prediction_count=2)
    duplicate = _signal_view_row()
    monkeypatch.setattr(
        bq, "_run_query", _released_dispatcher(receipt, counts, [duplicate, duplicate])
    )
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = client.get("/api/desk?region=za").json()["dynamic_discovery"]
    assert dynamic["status"] == "unavailable"
    assert dynamic["run"] is None
    assert dynamic["signals"] == []
    assert dynamic["error"]["code"] == "signal_contract_violation"


def test_a_released_run_with_one_admitted_signal_is_ready(monkeypatch):
    receipt = dict(_RUN_RECEIPT_ROW, candidate_count=1, prediction_count=1)
    counts = dict(_ZERO_COUNTS_ROW, candidate_count=1, prediction_count=1)
    monkeypatch.setattr(
        bq, "_run_query", _released_dispatcher(receipt, counts, [_signal_view_row()])
    )
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = client.get("/api/desk?region=za").json()["dynamic_discovery"]
    assert dynamic["status"] == "ready"
    assert dynamic["error"] is None
    assert dynamic["run"]["run_id"] == "run_20260827_dynamic_apply_v1"
    assert len(dynamic["signals"]) == 1
    signal = dynamic["signals"][0]["signal"]
    assert signal["signal_id"] == "sig_" + "a" * 64
    assert signal["signal_name"] == "Weekend repair meetups"
    assert signal["discovery_mode"] == "phrase"
    assert signal["evidence_state"] == "ready"
    assert signal["market"] == "za"
    assert signal["run_id"] == "run_20260827_dynamic_apply_v1"
    assert signal["receipts"] == []


def test_a_signal_outside_the_requested_market_scope_poisons_the_run(monkeypatch):
    receipt = dict(_RUN_RECEIPT_ROW, candidate_count=1, prediction_count=1)
    counts = dict(_ZERO_COUNTS_ROW, candidate_count=1, prediction_count=1)
    foreign = _signal_view_row(market="gh")
    monkeypatch.setattr(
        bq, "_run_query", _released_dispatcher(receipt, counts, [foreign])
    )
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = client.get("/api/desk?region=za").json()["dynamic_discovery"]
    assert dynamic["status"] == "unavailable"
    assert dynamic["error"]["code"] == "signal_contract_violation"


def test_a_market_request_never_carries_another_markets_signals(monkeypatch):
    """Found by independent review: a za request admitted ng and ke signals
    whenever the released run was multi-market. The requested market bounds
    the response; other markets' signals are out of request scope, not a
    violation of the run."""
    receipt = dict(_RUN_RECEIPT_ROW, candidate_count=2, prediction_count=2)
    counts = dict(_ZERO_COUNTS_ROW, candidate_count=2, prediction_count=2)
    za = _signal_view_row()
    ng = _signal_view_row(market="ng", signal_id="sig_" + "b" * 64)
    monkeypatch.setattr(bq, "_run_query", _released_dispatcher(receipt, counts, [za, ng]))
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = client.get("/api/desk?region=za").json()["dynamic_discovery"]
    assert dynamic["status"] == "ready"
    markets = [item["signal"]["market"] for item in dynamic["signals"]]
    assert markets == ["za"]


def test_an_all_request_carries_every_market_in_scope(monkeypatch):
    receipt = dict(_RUN_RECEIPT_ROW, candidate_count=2, prediction_count=2)
    counts = dict(_ZERO_COUNTS_ROW, candidate_count=2, prediction_count=2)
    za = _signal_view_row()
    ng = _signal_view_row(market="ng", signal_id="sig_" + "b" * 64)
    monkeypatch.setattr(bq, "_run_query", _released_dispatcher(receipt, counts, [za, ng]))
    monkeypatch.setattr(synth, "_call_model", _no_model)

    dynamic = desk.build_dynamic_discovery("all")
    assert dynamic["status"] == "ready"
    assert sorted(item["signal"]["market"] for item in dynamic["signals"]) == ["ng", "za"]


# --- open lens copy: no FOLLOW and SEED framing, no vendor tool names ---------


def test_system_instruction_drops_follow_seed_framing_and_vendor_tools():
    # A seed is the topic a brand could plant a move on before it peaks. The
    # instruction says that in plain words and never names a vendor tool.
    from src.api.chat import SYSTEM_INSTRUCTION

    si = SYSTEM_INSTRUCTION.lower()
    assert "follow" not in si
    assert "before it peaks" in si
    for banned in ("nano banana", "nanobanana", "lyria", "google"):
        assert banned not in si


def test_tool_get_seeds_renders_activation_surface_label(monkeypatch):
    from src.api import chat as chatmod
    from src.api import seeds as seeds_mod

    def payload(tool):
        return {
            "date": "2026-09-03",
            "seeds": [
                {
                    "behaviour": "b",
                    "the_shift": "x",
                    "why_hidden": "y",
                    "timing": "now",
                    "markets": ["za"],
                    "evidence": [],
                    "activation": {"tool": tool, "angle": "a", "prompt": "p"},
                    "signal_strength": "strong",
                }
            ],
        }

    expected = {"visual": "Visual", "audio": "Audio", "video": "Video", "live": "Live", "": ""}
    for tool, label in expected.items():
        monkeypatch.setattr(seeds_mod, "build_seeds_payload", lambda tool=tool: payload(tool))
        out = chatmod.tool_get_seeds()
        assert out["seeds"][0]["activation_surface"] == label
        assert "activation_tool" not in out["seeds"][0]
