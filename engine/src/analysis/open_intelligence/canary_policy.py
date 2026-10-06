"""Consumer-specific Gemini baseline, canary, and rollback policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from google.genai import types

Lane = Literal["baseline", "canary"]


@dataclass(frozen=True, slots=True)
class CanaryPolicy:
    consumer: str
    stage: str
    lane: Lane
    model: str
    thinking_level: types.ThinkingLevel
    input_ceiling: int
    output_ceiling: int


_POLICIES = MappingProxyType(
    {
        ("dynamic_signal_summary", "summary", "baseline"): CanaryPolicy(
            "dynamic_signal_summary",
            "summary",
            "baseline",
            "gemini-3.5-flash",
            types.ThinkingLevel.MEDIUM,
            8_000,
            800,
        ),
        ("dynamic_signal_summary", "summary", "canary"): CanaryPolicy(
            "dynamic_signal_summary",
            "summary",
            "canary",
            "gemini-3.7-flash",
            types.ThinkingLevel.MEDIUM,
            8_000,
            800,
        ),
        ("open_question_answer", "planning", "baseline"): CanaryPolicy(
            "open_question_answer",
            "planning",
            "baseline",
            "gemini-3.5-flash",
            types.ThinkingLevel.MEDIUM,
            8_000,
            800,
        ),
        ("open_question_answer", "planning", "canary"): CanaryPolicy(
            "open_question_answer",
            "planning",
            "canary",
            "gemini-3.7-flash",
            types.ThinkingLevel.MEDIUM,
            8_000,
            800,
        ),
        ("open_question_answer", "answering", "baseline"): CanaryPolicy(
            "open_question_answer",
            "answering",
            "baseline",
            "gemini-3.5-flash",
            types.ThinkingLevel.HIGH,
            32_000,
            4_000,
        ),
        ("open_question_answer", "answering", "canary"): CanaryPolicy(
            "open_question_answer",
            "answering",
            "canary",
            "gemini-3.7-flash",
            types.ThinkingLevel.HIGH,
            32_000,
            4_000,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class CanaryPolicyRegistry:
    policies: MappingProxyType

    def resolve(self, consumer: str, stage: str, lane: Lane) -> CanaryPolicy:
        try:
            return self.policies[(consumer, stage, lane)]
        except KeyError:
            raise ValueError("canary policy is not approved") from None

    def rollback_consumer(self, consumer: str) -> CanaryPolicyRegistry:
        if consumer not in {policy.consumer for policy in self.policies.values()}:
            raise ValueError("canary consumer is not approved")
        policies = dict(self.policies)
        for key, policy in tuple(policies.items()):
            if policy.consumer == consumer and policy.lane == "canary":
                policies[key] = replace(policy, model="gemini-3.5-flash")
        return CanaryPolicyRegistry(MappingProxyType(policies))


def default_policy_registry() -> CanaryPolicyRegistry:
    return CanaryPolicyRegistry(_POLICIES)


def resolve_canary_policy(consumer: str, stage: str, lane: Lane) -> CanaryPolicy:
    return default_policy_registry().resolve(consumer, stage, lane)


__all__ = [
    "CanaryPolicy",
    "CanaryPolicyRegistry",
    "Lane",
    "default_policy_registry",
    "resolve_canary_policy",
]
