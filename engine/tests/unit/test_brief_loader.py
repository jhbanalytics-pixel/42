"""Unit tests for src/alerts/brief_loader.load_briefs_by_topic_from_bq.

Patches get_client + get_dataset in the brief_loader namespace and serves fake
trend_analysis rows; verifies the email dict shape, the render_payload badge
re-apply, and the keys filter used by the cron supplement path.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

from src.alerts import brief_loader


class AttrRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _brief_row(market: str, topic: str, **overrides) -> AttrRow:
    base = {
        "market": market,
        "query_group": topic,
        "trend_score": 0.5,
        "headline": f"{market} {topic} headline",
        "trend_synthesis": "synthesis",
        "cultural_context": "context",
        "campaign_angles": ["angle"],
        "risk_flags": [],
        "platforms": ["tiktok"],
        "sentiment_summary": "positive",
        "status_tag": "Rising",
        "visual_anchor": "anchor",
        "nano_banana_prompt": "nb",
        "lyria_prompt": "lyria",
        "top_creators": ["@c"],
        "social_refs": [],
        "platform_counts": [],
        "b24_sentiment_trajectory": "",
        "render_payload": None,
    }
    base.update(overrides)
    return AttrRow(**base)


def _client(rows):
    job = MagicMock()
    job.result.return_value = rows
    client = MagicMock()
    client.project = "ogilvy-trends-v2"
    client.query.return_value = job
    return client


def _patches(client):
    return (
        patch.object(brief_loader, "get_client", return_value=client),
        patch.object(brief_loader, "get_dataset", return_value="trends"),
    )


def test_loads_all_markets_into_email_shape():
    client = _client([_brief_row("za", "music_amapiano"), _brief_row("ng", "afrobeats")])
    p1, p2 = _patches(client)
    with p1, p2:
        out = brief_loader.load_briefs_by_topic_from_bq(date(2026, 6, 28))

    assert set(out) == {("za", "music_amapiano"), ("ng", "afrobeats")}
    entry = out[("za", "music_amapiano")]
    assert entry["market"] == "za"
    assert entry["topic"] == "music_amapiano"
    assert entry["headline"] == "za music_amapiano headline"
    # campaign_angles maps to key_metrics; v1 aliases present.
    assert entry["key_metrics"] == ["angle"]
    assert entry["description_rationale"] == "synthesis"
    assert entry["activation_idea"] == "context"


def test_render_payload_badges_reapplied():
    payload = json.dumps(
        {
            "continuity_state": "day3plus",
            "continuity_day": 4,
            "forecast_outlook": "climbing 7d",
            "lifecycle_phase": "growth",
            "seed_score": 0.7,
            "display": {"state": "Holding"},
        }
    )
    client = _client([_brief_row("za", "music_amapiano", render_payload=payload)])
    p1, p2 = _patches(client)
    with p1, p2:
        out = brief_loader.load_briefs_by_topic_from_bq(date(2026, 6, 28))

    entry = out[("za", "music_amapiano")]
    assert entry["continuity_state"] == "day3plus"
    assert entry["continuity_day"] == 4
    assert entry["forecast_outlook"] == "climbing 7d"
    assert entry["lifecycle_phase"] == "growth"
    assert entry["seed_score"] == 0.7
    assert entry["display"] == {"state": "Holding"}


def test_keys_filter_restricts_result():
    client = _client(
        [
            _brief_row("za", "music_amapiano"),
            _brief_row("ng", "afrobeats"),
            _brief_row("ke", "gengetone"),
        ]
    )
    p1, p2 = _patches(client)
    with p1, p2:
        out = brief_loader.load_briefs_by_topic_from_bq(
            date(2026, 6, 28), keys=[("za", "music_amapiano"), ("ke", "gengetone")]
        )

    assert set(out) == {("za", "music_amapiano"), ("ke", "gengetone")}


def test_render_payload_seed_path_reapplied():
    payload = json.dumps({"seed_path": {"term": "amapiano", "channels": []}})
    client = _client([_brief_row("za", "music_amapiano", render_payload=payload)])
    p1, p2 = _patches(client)
    with p1, p2:
        out = brief_loader.load_briefs_by_topic_from_bq(date(2026, 6, 28))
    assert out[("za", "music_amapiano")]["seed_path"]["term"] == "amapiano"
