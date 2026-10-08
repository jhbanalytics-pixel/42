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


# N23: a topic the model never reached is held under the brief job's G10 reason, but no check ran on it.
def test_a_topic_the_model_never_reached_is_not_said_to_have_failed_checks():
    out = plain_reason({"reason_text": "Explanation failed its checks", "explanation_status": "not_run"})
    assert out["reason_text"] == "Not explained: the model did not get to this topic"
    assert out["reason_raw"] == "Explanation failed its checks"


@pytest.mark.parametrize("status", ["failed_checks", "explained", None])
def test_only_a_not_run_status_changes_the_explanation_hold_words(status):
    out = plain_reason({"reason_text": "Explanation failed its checks", "explanation_status": status})
    assert out["reason_text"] == "The explanation did not pass our checks"


def test_a_busy_model_wording_still_wins_over_the_not_reached_wording():
    busy = list(NOT_RUN_REASONS)[0]
    out = plain_reason({"reason_text": "Explanation failed its checks", "explanation_status": "not_run",
                        "failed_reason": busy})
    assert out["reason_text"] == "Not explained in time: the model was busy"


# W8-DEC-02: G10 keeps the hold, and the words say held with the reason shown, not "numbers and posts only".
def _contract_text():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "contract.md").read_text(encoding="utf-8")


def test_the_contract_words_a_g10_hold_as_held_with_the_reason_shown():
    text = _contract_text()
    for old in ("published numbers and posts only", "G10 keeps the card as numbers and posts",
                "numbers and posts still show on non-Today read surfaces"):
        assert old not in text
    assert "`partial` (at least one market held an item whose explanation failed its checks, reason shown" in text
    assert "`explanation_failed` (G10 holds the item, reason shown" in text
    assert "the item stays held, with its reason shown" in text


def test_the_recheck_docstring_words_a_failed_card_as_held_with_the_reason_shown():
    from core.api import today
    doc = " ".join(today._recheck_explanation.__doc__.split())
    assert "numbers and posts only" not in doc
    assert "held, reason shown" in doc
