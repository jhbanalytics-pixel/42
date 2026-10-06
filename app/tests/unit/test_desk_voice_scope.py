from datetime import UTC, datetime

import pytest

from src.api import bq, desk, synth


def voice(market, handle):
    stamp = datetime.now(UTC)
    return {
        "topic": "music_amapiano",
        "id": handle,
        "title": "",
        "text": f"{handle} shares a detailed account of the local music scene today.",
        "platform": "tiktok",
        "market": market,
        "author_handle": handle,
        "url": f"https://example.test/{handle}",
        "topic_groups": ["music_amapiano"],
        "engagement": 100,
        "published_at": stamp,
        "collected_at": stamp,
        "slang_terms": "",
    }


@pytest.mark.parametrize("market,expected", [("za", "za_dancer"), ("ng", "ng_dancer")])
def test_desk_voices_and_receipts_keep_the_topic_market(monkeypatch, market, expected):
    rows = [
        voice("za", "za_dancer"),
        voice("ng", "ng_dancer"),
        voice(None, "unknown_dancer"),
    ]
    monkeypatch.setattr(bq, "_run_query", lambda *args: rows)
    monkeypatch.setattr(synth, "cache_get", lambda key: None)
    pools = bq.fetch_voice_pools(["music_amapiano"], "all")
    topic = desk._build_topic(
        {"query_group": "music_amapiano", "market": market},
        {},
        {},
        (0, 0.5, 1),
        pools,
        "1d",
    )
    assert [receipt[1] for receipt in topic["receipts"]] == ["@" + expected]
    assert [item[1] for item in topic["voices"]] == [voice(market, expected)["text"]]


def test_a_topic_without_market_cannot_admit_unscoped_voice_rows(monkeypatch):
    monkeypatch.setattr(synth, "cache_get", lambda key: None)
    topic = desk._build_topic(
        {"query_group": "music_amapiano"},
        {},
        {},
        (0, 0.5, 1),
        {
            "music_amapiano": [
                {**voice(None, "unknown_dancer"), "handle": "unknown_dancer"}
            ]
        },
        "1d",
    )
    assert topic["voices"] == []
    assert topic.get("receipts", []) == []


@pytest.mark.parametrize("per_market", [False, True])
def test_voice_pool_query_has_explicit_scope_order_and_one_bounded_read(
    monkeypatch, per_market
):
    calls = []
    monkeypatch.setattr(
        bq, "_run_query", lambda sql, params: calls.append((sql, params)) or []
    )
    bq.fetch_voice_pools(
        ["music_amapiano", "music_amapiano"], "all", per_market=per_market
    )
    assert len(calls) == 1
    sql, params = calls[0]
    partition = "market, topic" if per_market else "topic"
    assert f"PARTITION BY {partition} ORDER BY rel DESC," in sql
    assert f"PARTITION BY {partition}, textkey ORDER BY rel DESC," in sql
    assert sql.count("platform ASC, id ASC, url ASC") == 2
    assert sql.count("TO_JSON_STRING(STRUCT(") == 2
    assert sql.endswith(
        "ORDER BY topic, market, rn" if per_market else "ORDER BY topic, rn"
    )
    assert "WHERE rn <= 12" in sql
    assert "collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)" in sql
    assert "AND (@market = 'all' OR market = @market)" in sql
    assert params == [
        ("market", "STRING", "all"),
        ("topics", "STRING", ["music_amapiano"]),
    ]


def test_default_voice_pool_keeps_combined_topic_profile_semantics(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bq,
        "_run_query",
        lambda sql, params: (
            calls.append(sql) or [voice("za", "za_dancer"), voice("ng", "ng_dancer")]
        ),
    )
    result = bq.fetch_voice_pools(["music_amapiano"], "all")
    assert list(result) == ["music_amapiano"]
    assert [row["market"] for row in result["music_amapiano"]] == ["za", "ng"]
    assert "PARTITION BY topic, textkey" in calls[0]
    assert "PARTITION BY market, topic" not in calls[0]
