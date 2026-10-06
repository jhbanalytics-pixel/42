"""Unit tests for the seed-intelligence pass.

Pins the prompt's behaviour-not-topic discipline, the brief shaping, the
persisted row shape, the thin-briefs skip, and the non-fatal contract. The
Gemini call is mocked; no network.
"""

import datetime as dt
from unittest import mock

from src.analysis import generate_seed_intelligence as gsi
from src.analysis.prompts.seed_intelligence import (
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    build_seed_prompt,
)


def _briefs(n=6):
    out = {}
    for i in range(n):
        out[("za", f"topic_{i}")] = {
            "status_tag": "Rising",
            "trend_synthesis": f"synthesis {i}",
            "cultural_context": f"context {i}",
            "headline": f"headline {i}",
            "platforms": ["TikTok"],
            "seed_score": 0.5 - i * 0.01,
        }
    return out


def test_prompt_forbids_topic_as_seed():
    si = SYSTEM_INSTRUCTION.lower()
    assert "a topic is not a seed" in si
    assert "braai" in si  # the worked example of behaviour-not-topic
    assert "nanobanana" in si
    assert "lyria" in si


def test_seed_prompt_contract_is_audience_neutral():
    prompt = build_seed_prompt(trend_date="2026-06-22", briefs=[]).lower()
    schema = str(RESPONSE_SCHEMA).lower()
    instruction = SYSTEM_INSTRUCTION.lower()
    for audience_label in ("gen z", "gen-z", "young people", "youth-facing"):
        assert audience_label not in prompt
        assert audience_label not in schema
        assert audience_label not in instruction
    assert "cultural behaviour" in instruction


def test_seed_prompt_has_general_purpose_without_mandatory_client_branding():
    prompt = build_seed_prompt(trend_date="2026-06-22", briefs=[]).lower()
    instruction = SYSTEM_INSTRUCTION.lower()
    assert "evidence-grounded cultural understanding" in instruction
    assert "evidence-grounded cultural understanding" in prompt
    for text in (instruction, prompt):
        assert "google x ogilvy" not in text
        assert "primary objective" not in text
        assert "driving usage" not in text


def test_seed_asset_schema_survives_general_purpose_framing():
    seed = RESPONSE_SCHEMA["properties"]["seeds"]["items"]
    activation = seed["properties"]["activation"]
    assert "activation" in seed["required"]
    assert activation["required"] == ["tool", "angle", "prompt"]
    assert activation["properties"]["tool"]["enum"] == ["Nanobanana", "Lyria"]
    prompt_description = activation["properties"]["prompt"]["description"]
    assert "exclude clause" in prompt_description
    assert "market-correct genre" in prompt_description


def test_response_schema_seed_shape():
    item = RESPONSE_SCHEMA["properties"]["seeds"]["items"]["properties"]
    for k in (
        "behaviour",
        "the_shift",
        "evidence",
        "why_hidden",
        "timing",
        "markets",
        "brand_opportunity",
        "activation",
        "signal_strength",
    ):
        assert k in item
    assert item["activation"]["properties"]["tool"]["enum"] == ["Nanobanana", "Lyria"]


def test_build_seed_prompt_includes_seed_and_briefs():
    briefs = [
        {
            "market": "za",
            "topic_group": "food_rituals_braai",
            "status_tag": "Rising",
            "trend_synthesis": "gourmet braai as a flex",
            "cultural_context": "premium plating",
            "headline": "Braai goes gourmet",
            "platforms": ["TikTok", "Instagram"],
            "seed_score": 0.43,
            "velocity_score": 0.1,
        }
    ]
    p = build_seed_prompt(trend_date="2026-06-22", briefs=briefs)
    assert "za/food_rituals_braai" in p
    assert "seed 0.43" in p
    assert "<briefs>" in p
    assert "behaviour" in p.lower()


def _fake_response(seeds):
    r = mock.Mock()
    r.parsed = {"seeds": seeds}
    r.model = "gemini-3.5-flash"
    r.prompt_tokens = 100
    r.completion_tokens = 200
    return r


def test_generate_skips_when_too_few_briefs():
    out = gsi.generate_seed_intelligence(
        trend_date=dt.date(2026, 6, 22), briefs_by_topic=_briefs(3)
    )
    assert out == []


def test_generate_returns_and_persists(monkeypatch):
    seed = {
        "behaviour": "Premiumising everyday rituals into status flexes",
        "the_shift": "x",
        "evidence": ["za/food_rituals_braai - gourmet braai"],
        "why_hidden": "y",
        "timing": "now",
        "markets": ["za"],
        "brand_opportunity": "z",
        "activation": {"tool": "Nanobanana", "angle": "a", "prompt": "p"},
        "signal_strength": "strong",
    }
    captured = {}

    fake_client = mock.Mock()
    fake_client.project = "p"
    # _existing query -> 0 rows
    job = mock.Mock()
    job.result.return_value = iter([mock.Mock(n=0)])
    fake_client.query.return_value = job

    gem = mock.Mock()
    gem.generate_brief.return_value = _fake_response([seed])

    monkeypatch.setattr(gsi, "get_client", lambda: fake_client)
    monkeypatch.setattr(gsi, "get_dataset", lambda: "ds")
    monkeypatch.setattr(
        gsi, "insert_dataframe", lambda df, t: captured.update(table=t, rows=df.to_dict("records"))
    )

    out = gsi.generate_seed_intelligence(
        trend_date=dt.date(2026, 6, 22),
        briefs_by_topic=_briefs(8),
        gemini_client=gem,
    )
    assert len(out) == 1
    assert captured["table"] == "seed_insights"
    row = captured["rows"][0]
    assert row["behaviour"].startswith("Premiumising")
    assert row["activation_tool"] == "Nanobanana"
    assert row["rank"] == 1
    assert row["evidence"] == ["za/food_rituals_braai - gourmet braai"]


def test_generate_non_fatal_on_gemini_error(monkeypatch):
    fake_client = mock.Mock()
    fake_client.project = "p"
    job = mock.Mock()
    job.result.return_value = iter([mock.Mock(n=0)])
    fake_client.query.return_value = job
    gem = mock.Mock()
    gem.generate_brief.side_effect = RuntimeError("vertex down")

    monkeypatch.setattr(gsi, "get_client", lambda: fake_client)
    monkeypatch.setattr(gsi, "get_dataset", lambda: "ds")

    out = gsi.generate_seed_intelligence(
        trend_date=dt.date(2026, 6, 22), briefs_by_topic=_briefs(8), gemini_client=gem
    )
    assert out == []
