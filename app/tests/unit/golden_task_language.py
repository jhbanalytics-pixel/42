"""The language rules the golden-task suite enforces, kept where they can be read.

Each pattern names a claim the evidence cannot carry: a cause, a forecast, a
poll, or a demographic asserted rather than measured.

Two refinements were earned against the frozen fixtures rather than assumed. A
sentence that denies the claim is not making it, so "this is not a forecast"
and "without establishing cause" clear rather than fail: the contract asks for
those disclaimers, and a checker that flags them teaches a reader to ignore it.
And the readiness section explains the tool's own gate, which is a statement
about the rule rather than about culture, so it is read separately.
"""

from __future__ import annotations

import re

CAUSAL = re.compile(
    r"\b(causes?|caused|because of this|drives?|driven by|leads? to|results? in|due to)\b",
    re.IGNORECASE,
)

PREDICTIVE = re.compile(
    r"\b(will (?:be|become|grow|rise|fall|continue)|going to|"
    r"expect(?:ed)? to (?:grow|rise|fall)|forecasts?|predicts?)\b",
    re.IGNORECASE,
)

POLLING = re.compile(
    r"\b(polls?|polling|surveys?|surveyed|respondents?|margin of error|sample of \d)\b",
    re.IGNORECASE,
)

DEMOGRAPHIC = re.compile(
    r"\b(\d{2}\s*(?:to|-|–)\s*\d{2}\s*year[- ]olds?|gen ?z|millennials?|"
    r"male audience|female audience|men aged|women aged)\b",
    re.IGNORECASE,
)

DISCLAIMED = re.compile(
    r"\b(not a forecast|is not a prediction|without establishing cause|"
    r"no causal claim|not (?:a poll|representative polling)|does not establish|"
    r"association is not causation|no population estimate)\b",
    re.IGNORECASE,
)

# The readiness section explains why the gate opened, in the gate's own terms.
RULE_SECTIONS = frozenset({"evidence_readiness"})


def claim_sentences(text: str) -> list[str]:
    """Sentences that actually assert something about the world."""
    return [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence.strip() and not DISCLAIMED.search(sentence)
    ]


def find_overclaim(pattern: re.Pattern, text: str) -> str | None:
    for sentence in claim_sentences(text):
        match = pattern.search(sentence)
        if match:
            return match.group(0)
    return None
