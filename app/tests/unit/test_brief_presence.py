import pytest

from src.api import bq, desk, synth, topic


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, False),
        ({"trend_synthesis": None, "cultural_context": None}, False),
        ({"trend_synthesis": "  ", "cultural_context": "\n"}, False),
        ({"nano_banana_prompt": "An image prompt"}, False),
        ({"trend_synthesis": "Observed repair discussion."}, True),
        ({"cultural_context": "A partial cultural context."}, True),
    ],
)
def test_topic_brief_presence_comes_from_text_not_constructed_container(monkeypatch, fields, expected):
    monkeypatch.setattr(synth, "cache_get", lambda key: "A cached opportunity.")
    row = {"market": "za", "query_group": "music_amapiano", "trend_score": 0.4, **fields}
    base = desk._build_topic(row, {}, {}, (0.1, 0.2, 0.3), {}, "1d")
    assert isinstance(base["brief"], dict)
    assert base["brief"]["opportunity"] == "A cached opportunity."
    for name in ("fetch_topic_daily", "fetch_market_daily", "fetch_topic_channel_sov"):
        monkeypatch.setattr(bq, name, lambda *args: [])
    for name in ("fetch_voice_pools", "fetch_topic_tone_distribution", "derive_topic_hashtags"):
        monkeypatch.setattr(bq, name, lambda *args: {})
    result = topic.build_topic_profile("music_amapiano", "za", base)
    assert result["has_brief"] is expected
    assert result["brief"] == base["brief"]
