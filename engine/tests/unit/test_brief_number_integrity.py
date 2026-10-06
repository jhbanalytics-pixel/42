"""Tests for the brief-number integrity fix (4 Jun 2026).

The accuracy audit found brief engagement figures were Gemini free-text and
could diverge from the real data (a model wrote "2.9M views" peak when the
true peak was 34M, and relabelled a composite engagement SUM as "views").
These verify the deterministic key_metrics override: real numbers from the
engine row, correctly labelled, replacing the model's free-text.
"""

from __future__ import annotations

from src.analysis.gemini_client import BriefResponse
from src.analysis.generate_briefs import _build_key_metrics, _to_topic_brief


def _resp(key_metrics: list[str]) -> BriefResponse:
    return BriefResponse(
        parsed={
            "description_rationale": "x",
            "activation_idea": "y",
            "visual_anchor": "z",
            "nano_banana_prompt": "p",
            "lyria_prompt": "l",
            "key_metrics": key_metrics,
            "platforms": ["TikTok"],
            "sentiment_summary": "s",
            "status_tag": "Key",
        },
        raw_text="{}",
        prompt_tokens=10,
        completion_tokens=10,
        model="gemini-2.5-flash",
    )


def test_build_key_metrics_deterministic_and_labelled():
    out = _build_key_metrics(2_000_000.0, 198, 6, 97)
    assert out == [
        "Peak post engagement 2M",
        "198 posts across 6 sources",
        "97 creators tracked",
    ]
    # the composite engagement SUM is labelled "engagement", never "views"
    assert "views" not in out[0]


def test_build_key_metrics_humanises_fractional():
    assert _build_key_metrics(1_276_812.0, 0, 0, 0) == ["Peak post engagement 1.3M"]


def test_build_key_metrics_drops_zero_metrics():
    # zero peak -> no peak line; zero creators -> no creators line; no sources
    out = _build_key_metrics(0.0, 50, 0, 0)
    assert out == ["50 posts"]
    assert all(not m.startswith("0 ") and "engagement 0" not in m for m in out)


def test_to_topic_brief_override_replaces_model_metrics():
    """A divergent model figure is dropped for the deterministic metrics."""
    response = _resp(["FAKE 99M views", "made up", "hallucinated"])
    real = ["Peak post engagement 34.1M", "198 posts across 6 sources", "97 creators tracked"]
    brief = _to_topic_brief("za", "music_amapiano", 0.48, response, key_metrics_override=real)
    assert brief.key_metrics == real
    assert "FAKE 99M views" not in brief.key_metrics


def test_to_topic_brief_without_override_uses_model():
    """Back-compat: no override means the model's key_metrics still flow."""
    response = _resp(["100 mentions", "22 sources", "Velocity 0.07"])
    brief = _to_topic_brief("za", "music_amapiano", 0.48, response)
    assert brief.key_metrics == ["100 mentions", "22 sources", "Velocity 0.07"]
