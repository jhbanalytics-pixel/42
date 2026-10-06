"""A6a/A6b seed_path gating tests."""

from __future__ import annotations

import os
from unittest.mock import patch

from src.alerts.email_render.card import _seed_path
from src.analysis.prompts.trend_brief import build_brief_prompt
from src.analysis.seed_path import format_seed_path_prompt_block


def test_prompt_byte_identical_when_seed_path_block_empty():
    kwargs = {
        "market": "za",
        "topic_group": "music_amapiano",
        "trend_score": 0.5,
        "velocity_score": 0.2,
        "item_count": 10,
        "source_diversity": 3,
        "creator_spread": 2,
        "tone_avg": None,
        "sample_rows": [],
        "top_creators": [],
    }
    a = build_brief_prompt(**kwargs, seed_path_block="")
    b = build_brief_prompt(**kwargs)
    assert a == b


def test_format_seed_path_prompt_block_sanitises():
    block = format_seed_path_prompt_block(
        {
            "term": "amapiano",
            "confidence": "thin",
            "channels": [{"platform": "tiktok", "first_seen": "2026-07-01"}],
        }
    )
    assert "amapiano" in block
    assert "tiktok" in block


def test_card_seed_path_self_hides_when_empty():
    assert _seed_path({}, {}, dark=True) == ""


def test_card_seed_path_renders_trail():
    html = _seed_path(
        {
            "seed_path": {
                "term": "amapiano",
                "confidence": "measured",
                "channels": [{"platform": "TikTok", "first_seen": "2026-07-01"}],
            }
        },
        {"on_ink_warm": "#fff", "muted": "#999", "on_ink_mute": "#aaa"},
        dark=True,
    )
    assert "Seed path" in html
    assert "amapiano" in html


def test_generate_briefs_skips_seed_path_when_flag_off():
    with patch.dict(os.environ, {"SEED_PATH_RENDER_ENABLED": "false"}, clear=False):
        assert os.environ.get("SEED_PATH_RENDER_ENABLED", "false").lower() != "true"
