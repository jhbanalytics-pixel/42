"""The critic's code-written line on the rivals detect's numbers show (core/brief/explain.py _rivals_line, 18 Oct 2026).

The critic saw top3_share, burst_share and near_dup_share among the pack's numbers as raw shares, with neither the
thresholds nor the floor of 10 posts in 7 days that detect's own share_flags apply (core/detect/sql/state.sql), so a
share over a handful of posts could read as a simpler explanation. A line from core/brief/rivals.py code_rivals now
says what is found, not found and not assessed, with the thresholds and the floor. The shadow record stays shadow.
"""

import pytest

from core.brief import explain, rivals
from core.brief.tests.test_brief_explain import CANDIDATE, RULED_OUT, FakeModel, good, outside_fences, run
from core.brief.tests.test_brief_rivals_shadow import comparable, quiet

LEDGER = [{"id": "c1", "text": "Two creators post.", "label": "observed", "kind": "observation",
           "evidence_ids": ["tt_1", "ig_2"]}]


def line(pack):
    prompt = outside_fences(explain._critic_user(CANDIDATE, "ZA", "A sentence.", LEDGER, pack, ("c1",)))
    [block] = [b for b in prompt.split("\n\n") if b.startswith("Simpler explanations checked by code")]
    return block


def test_found_not_found_and_not_assessed_are_named_with_the_thresholds():
    text = line(quiet(burst_share=0.5, top3_share=0.3))
    assert "found: a burst push (burst_share)." in text
    assert ("Not found: a paid or sponsored push (sponsored_share); a few creators carrying the count (top3_share); "
            "near-duplicate posts (near_dup_share); a calendar moment detect lists; a news outlet leading (the pack's "
            "earliest post is a news post).") in text
    assert "Not assessed: a collection regime break." in text
    assert ("Thresholds: sponsored_share 0.5 or more; burst_share 0.35 or more and top3_share 0.6 or more, each only "
            "with 10 or more posts in 7 days; near_dup_share 0.3 or more.") in text
    assert "Values used: posts7 20, burst_share 0.5, top3_share 0.3, near_dup_share 0.05, sponsored_share 0.0" in text
    assert "posts in 7 days: 20." in text and "below the floor" not in text
    assert text.endswith("This is a record from detect's numbers, not a verdict.")
    assert "over the trend's 7-day posts, not these posts (read ok)" in text


def test_a_high_share_under_the_floor_is_not_found_and_the_line_says_why():
    text = line(quiet(top3_share=0.9, burst_share=0.8, posts7=6))
    assert "found: none." in text
    assert "a few creators carrying the count (top3_share)" in text.split("Not found:", 1)[1].split("Not assessed", 1)[0]
    assert ("posts in 7 days: 6, below the floor of 10, so burst and concentrated are not found whatever their "
            "share.") in text
    assert "A share below its threshold, or under the floor, is not a found rival on its own." in text


def test_a_missing_input_is_not_assessed_not_absent():
    text = line(quiet(near_dup_share=None))
    assert "near-duplicate posts (near_dup_share)" in text.split("Not assessed:", 1)[1]
    assert "near-duplicate posts (near_dup_share)" not in text.split("Not found:", 1)[1].split("Not assessed", 1)[0]


def test_a_pack_without_detect_rival_values_is_not_assessed():
    from core.brief.tests.test_brief_explain import make_pack

    assert line(make_pack()).endswith("not assessed; the pack carries no detect rival values.")


def test_a_fault_in_the_rivals_code_reads_as_not_assessed_and_the_line_still_reaches_the_critic(monkeypatch):
    def boom(pack):
        raise TypeError("not a pack")

    monkeypatch.setattr(rivals, "code_rivals", boom)
    assert line(quiet()).endswith("not assessed (TypeError).")


def test_the_line_sits_after_the_pattern_line_and_before_the_numbers():
    prompt = outside_fences(explain._critic_user(CANDIDATE, "ZA", "A sentence.", LEDGER, quiet(), ("c1",)))
    blocks = prompt.split("\n\n")
    at = next(i for i, b in enumerate(blocks) if b.startswith("Simpler explanations checked by code"))
    assert blocks[at - 1].startswith("Shared sounds and formats, counted by code")
    assert blocks[at + 1].startswith("The trend's numbers")


def test_the_shadow_record_is_unchanged_and_no_call_is_added():
    base = run(FakeModel([good()], critic=RULED_OUT), pack=quiet(burst_share=0.5))
    model = FakeModel([good()], critic=RULED_OUT)
    result = run(model, pack=quiet(burst_share=0.5))
    [row] = [r for r in result["checks"] if r["rule"] == "critic"]
    assert row["code_rivals"]["found"] == ["burst"] and row["code_rivals"]["disagreement"] is True
    assert comparable(result) == comparable(base)
    [call] = model.critic_calls()
    assert "Simpler explanations checked by code" in call["user"] and "q_burst_share" in call["user"]
    assert len(model.critic_calls()) == 1


def test_the_critic_is_told_what_the_line_is_and_that_a_share_under_the_floor_is_no_rival():
    text = " ".join(explain.CRITIC_SYSTEM.split())
    assert ("A line written by code says which simpler explanations detect's own numbers show (found), do not show "
            "(not found) or could not assess, with the thresholds and the floor of 10 posts in 7 days that detect "
            "itself applies to burst_share and top3_share") in text
    assert "is not a found rival on its own" in text and "It is evidence you may weigh, not a verdict." in text


@pytest.mark.parametrize("rival", rivals.RIVALS)
def test_every_rival_the_code_knows_has_words(rival):
    assert rival in explain.RIVAL_WORDS


def test_the_floor_the_prompt_names_is_the_one_the_code_applies():
    assert rivals.MIN_POSTS7 == 10 and "floor of 10 posts in 7 days" in " ".join(explain.CRITIC_SYSTEM.split())
