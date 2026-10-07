"""Every held reason core/trust/gate.py writes reads in plain words on Today, Discover and the other pages
(core/api/held_words.py), so a change to the gate's wording cannot quietly fall back to the raw text."""
import re
from pathlib import Path

import pytest

from core.api.held_words import plain_reason
from core.brief.payload import NOT_RUN_REASONS
from core.trust import gate

GATE_SOURCE = Path(gate.__file__).read_text(encoding="utf-8")
CLEAN_CARD = {"market_scope": "market", "market_posts7": 6, "total_posts7": 8, "market_share7": 0.75,
              "sponsored_share": 0.0, "hashtags": ["#dance"], "canonical_key": "#dance", "state": "emerging"}
CLEAN_CTX = {"valid_days": [True, True, True], "lane_classes": ["unbiased_rank"], "campaign_hashtags": [],
             "political": False, "corroborated_unbiased": False, "explanation_passed": True}

# One card and context per held branch of gate_card, in the order gate_card tries them.
HELD = {
    "G1": ({}, {"valid_days": [True, False, True]}),
    "G3": ({}, {"lane_classes": ["search"]}),
    "G6 missing": ({"market_share7": None}, {}),
    "G6 inconsistent": ({"market_share7": 0.5}, {}),
    "G6 global": ({"market_scope": "global", "market_posts7": 3, "market_share7": 0.375}, {}),
    "G5": ({"sponsored_share": 0.62}, {}),
    "G5b": ({}, {"campaign_hashtags": ["#Dance"]}),
    "G4b": ({}, {"political": True}),
}


def held_reason(card, ctx):
    d = gate.gate_card({**CLEAN_CARD, **card}, {**CLEAN_CTX, **ctx})
    assert d.where == "held_back" and d.publish is False
    return d


def test_every_held_branch_of_the_gate_is_covered_here():
    assert len(re.findall(r"\breturn _held\(", GATE_SOURCE)) == len(HELD)
    assert [held_reason(*HELD[k]).rule for k in HELD] == ["G1", "G3", "G6", "G6", "G6", "G5", "G5b", "G4b"]


@pytest.mark.parametrize("name", list(HELD))
def test_every_gate_held_reason_maps_to_plain_words(name):
    raw = held_reason(*HELD[name]).reason
    out = plain_reason({"reason_text": raw})
    assert out.get("reason_raw") == raw
    assert out["reason_text"] != raw and out["reason_text"].strip()


def test_the_current_g3_and_g4b_wording_reads_plainly():
    assert plain_reason({"reason_text": "Found by search only: not yet in the feeds 42 measures every day"}) == {
        "reason_text": "Only found through our own searches so far",
        "reason_raw": "Found by search only: not yet in the feeds 42 measures every day"}
    assert plain_reason({"reason_text": "Not assessed: political topic that no neutral source has confirmed yet"}) == {
        "reason_text": "Political: waiting for independent confirmation",
        "reason_raw": "Not assessed: political topic that no neutral source has confirmed yet"}


@pytest.mark.parametrize("raw, words", [
    ("Found by search: seen only in search", "Only found through our own searches so far"),
    ("Not assessed: political item not Corroborated in an unbiased lane",
     "Political: waiting for independent confirmation"),
])
def test_briefs_stored_with_the_earlier_wording_still_read_plainly(raw, words):
    assert plain_reason({"reason_text": raw})["reason_text"] == words


# Tester report, 5 October 2026: "Explanation failed its checks" read as a broken market, and a topic a busy model
# left unexplained sat under the same words although no check ran on it.
@pytest.mark.parametrize("raw", ["Explanation failed its checks", "The explanation did not pass its checks"])
def test_an_explanation_hold_reads_as_what_happened_to_the_explanation(raw):
    assert plain_reason({"reason_text": raw, "failed_reason": "claim not supported by its posts"}) == {
        "reason_text": "The explanation did not pass our checks", "reason_raw": raw,
        "failed_reason": "claim not supported by its posts"}
    assert plain_reason({"reason_text": raw})["reason_text"] == "The explanation did not pass our checks"


@pytest.mark.parametrize("busy", list(NOT_RUN_REASONS))
def test_a_topic_a_busy_model_left_unexplained_says_so(busy):
    out = plain_reason({"reason_text": "Explanation failed its checks", "failed_reason": busy})
    assert out["reason_text"] == "Not explained in time: the model was busy"
    assert out["reason_raw"] == "Explanation failed its checks" and out["failed_reason"] == busy
