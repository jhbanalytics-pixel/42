from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from google.genai import types


def test_dependency_is_pinned_to_the_reviewed_sdk() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"

    assert '"google-genai==2.20.0"' in pyproject.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("consumer", "stage", "lane", "model", "thinking", "input_ceiling", "output_ceiling"),
    [
        (
            "dynamic_signal_summary",
            "summary",
            "baseline",
            "gemini-3.5-flash",
            types.ThinkingLevel.MEDIUM,
            8_000,
            800,
        ),
        (
            "dynamic_signal_summary",
            "summary",
            "canary",
            "gemini-3.7-flash",
            types.ThinkingLevel.MEDIUM,
            8_000,
            800,
        ),
        (
            "open_question_answer",
            "planning",
            "baseline",
            "gemini-3.5-flash",
            types.ThinkingLevel.MEDIUM,
            8_000,
            800,
        ),
        (
            "open_question_answer",
            "planning",
            "canary",
            "gemini-3.7-flash",
            types.ThinkingLevel.MEDIUM,
            8_000,
            800,
        ),
        (
            "open_question_answer",
            "answering",
            "baseline",
            "gemini-3.5-flash",
            types.ThinkingLevel.HIGH,
            32_000,
            4_000,
        ),
        (
            "open_question_answer",
            "answering",
            "canary",
            "gemini-3.7-flash",
            types.ThinkingLevel.HIGH,
            32_000,
            4_000,
        ),
    ],
)
def test_policy_is_consumer_stage_and_lane_exact(
    consumer, stage, lane, model, thinking, input_ceiling, output_ceiling
) -> None:
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy

    policy = resolve_canary_policy(consumer, stage, lane)

    assert (policy.consumer, policy.stage, policy.lane) == (consumer, stage, lane)
    assert (policy.model, policy.thinking_level) == (model, thinking)
    assert (policy.input_ceiling, policy.output_ceiling) == (input_ceiling, output_ceiling)


def test_consumer_rollback_changes_only_that_canary_lane() -> None:
    from src.analysis.open_intelligence.canary_policy import default_policy_registry

    original = default_policy_registry()
    rolled_back = original.rollback_consumer("dynamic_signal_summary")

    assert (
        rolled_back.resolve("dynamic_signal_summary", "summary", "canary").model
        == "gemini-3.5-flash"
    )
    assert rolled_back.resolve("dynamic_signal_summary", "summary", "baseline") == original.resolve(
        "dynamic_signal_summary", "summary", "baseline"
    )
    assert rolled_back.resolve("open_question_answer", "answering", "canary") == original.resolve(
        "open_question_answer", "answering", "canary"
    )
    assert (
        original.resolve("dynamic_signal_summary", "summary", "canary").model == "gemini-3.7-flash"
    )


@pytest.mark.parametrize(
    ("model", "at", "input_rate", "output_rate", "version"),
    [
        (
            "gemini-3.5-flash",
            datetime(2026, 8, 28, tzinfo=UTC),
            "1.50",
            "9.00",
            "gemini_3_5_global_2026_v1",
        ),
        (
            "gemini-3.7-flash",
            datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
            "0.75",
            "3.75",
            "gemini_3_7_intro_2026_v1",
        ),
        (
            "gemini-3.7-flash",
            datetime(2027, 1, 1, tzinfo=UTC),
            "1.50",
            "7.50",
            "gemini_3_7_standard_2027_v1",
        ),
    ],
)
def test_pricing_uses_exact_effective_windows(model, at, input_rate, output_rate, version) -> None:
    from src.analysis.open_intelligence.canary_pricing import price_for

    price = price_for(model, at)

    assert price.model == model
    assert price.input_per_million == Decimal(input_rate)
    assert price.output_per_million == Decimal(output_rate)
    assert price.pricing_version == version


def test_pricing_rejects_naive_time_unknown_model_and_prelaunch_gap() -> None:
    from src.analysis.open_intelligence.canary_pricing import price_for

    with pytest.raises(ValueError):
        price_for("gemini-3.7-flash", datetime(2026, 8, 28))
    with pytest.raises(ValueError):
        price_for(
            "gemini-3.7-flash",
            datetime(2026, 8, 28, tzinfo=timezone(timedelta(hours=2))),
        )
    with pytest.raises(ValueError):
        price_for("unknown", datetime(2026, 8, 28, tzinfo=UTC))
    with pytest.raises(ValueError):
        price_for("gemini-3.7-flash", datetime(2026, 8, 12, tzinfo=UTC))
