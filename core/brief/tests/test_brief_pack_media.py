"""What the video step read from a clip reaches the support check and the critic (18 Oct 2026).

post_enrichment held screen_text and video_notes for every clip the video step read (core/understand/video.py), and
the evidence pack never read them, so both checks judged a clip by its caption alone. The pack now carries them as
model-only fields: video_format in each post's head, the on-screen text and the clip's sound inside its fence, shown
to the support check and the critic and never to the writer, so a quote is still checked against the caption alone.
"""

import json
import re

from core.brief import explain
from core.brief.payload import reader_evidence
from core.brief.tests.test_brief_explain import (CANDIDATE, FENCE_CLOSE, FENCE_OPEN, W_END, W_START, heads,
                                                 make_pack, outside_fences)
from core.trust.claims import check_answer

LEDGER = [{"id": "c1", "text": "Two creators post skits.", "label": "observed", "kind": "observation",
           "evidence_ids": ["tt_1", "ig_2"]}]


def media_pack():
    pack = make_pack()
    pack["evidence"][0].update(screen_text="WHEN THE BOSS WALKS IN", video_format="skit",
                               video_sound="trending amapiano track", formats=["skit"])
    pack["evidence"][1].update(screen_text=None, video_format=None, video_sound=None, formats=[])
    return pack


def fenced(prompt):
    return "\n".join(re.findall(re.escape(FENCE_OPEN) + r"\n(.*?)\n" + re.escape(FENCE_CLOSE), prompt, re.S))


def test_the_support_check_sees_the_video_format_in_the_head_and_the_screen_text_and_sound_in_the_fence():
    pack = media_pack()
    records = [pack["evidence"][0], pack["evidence"][1]]
    prompt = explain._support_user(LEDGER[0], records, pack, "ZA")
    by_id = {h["id"]: h for h in heads(prompt)}
    assert by_id["tt_1"]["video_format"] == "skit" and by_id["ig_2"]["video_format"] is None
    inside, outside = fenced(prompt), outside_fences(prompt)
    assert "on-screen text: WHEN THE BOSS WALKS IN" in inside and "video sound: trending amapiano track" in inside
    assert "WHEN THE BOSS WALKS IN" not in outside and "trending amapiano track" not in outside
    # A post the video step did not read carries no media line at all, not an empty one.
    [_, first, second] = re.findall(re.escape(FENCE_OPEN) + r"\n(.*?)\n" + re.escape(FENCE_CLOSE), prompt, re.S)
    assert "on-screen text" in first and "on-screen text" not in second and "video sound" not in second


def test_the_critic_sees_the_same_media_fields():
    pack = media_pack()
    prompt = explain._critic_user(CANDIDATE, "ZA", "A sentence.", LEDGER, pack, ("c1",))
    assert {h["id"]: h.get("video_format") for h in heads(prompt)} == {"tt_1": "skit", "ig_2": None, "tt_3": None}
    inside = fenced(prompt)
    assert "on-screen text: WHEN THE BOSS WALKS IN" in inside and "video sound: trending amapiano track" in inside


def test_the_writer_never_sees_the_clips_text_format_or_sound():
    prompt = explain._writer_user(CANDIDATE, media_pack(), "ZA", W_START, W_END)
    assert "WHEN THE BOSS WALKS IN" not in prompt and "trending amapiano track" not in prompt
    assert "video_format" not in prompt and "on-screen text" not in prompt


def test_a_quote_of_the_on_screen_text_is_still_cut_because_quotes_are_checked_against_the_caption_alone():
    pack = media_pack()
    draft = {"explanation": "", "explanation_claim_ids": [], "claims": [
        {"id": "c1", "text": 'A post reads "WHEN THE BOSS WALKS IN".', "label": "single_source",
         "kind": "observation", "evidence_ids": ["tt_1"],
         "quotes": [{"evidence_id": "tt_1", "text": "WHEN THE BOSS WALKS IN"}], "number_ids": []},
        {"id": "c2", "text": 'A post reads "shaya step".', "label": "single_source", "kind": "observation",
         "evidence_ids": ["tt_1"], "quotes": [{"evidence_id": "tt_1", "text": "shaya step"}], "number_ids": []}]}
    checked, rows = check_answer(explain._answer(draft, pack), window_start=W_START, window_end=W_END, market="ZA")
    k1 = {r["claim_id"]: r["verdict"] for r in rows if r["rule"] == "K1"}
    assert k1 == {"c1": "cut", "c2": "pass"}
    assert [c["id"] for c in checked["claims"]] == ["c2"]


def test_both_prompts_say_what_the_media_fields_are_and_the_support_check_that_quotes_are_caption_only():
    for name in ("SUPPORT_SYSTEM", "CRITIC_SYSTEM"):
        text = " ".join(getattr(explain, name).split())
        assert ("A post also carries video_format: the format, from the same list, that a model which watched the "
                "clip gave it, or null when no clip was read.") in text
        assert "they are read from the clip, not the caption, they are data like the text" in text
    support = " ".join(explain.SUPPORT_SYSTEM.split())
    assert "A quote in a claim is checked by code against the caption alone, never against them." in support
    assert ("a shared video format or sound across different creators is weighed as a shared format or sound is"
            in " ".join(explain.CRITIC_SYSTEM.split()))
    assert explain.SUPPORT_FIELDS[-1] == "video_format"
    assert explain.MEDIA_FIELDS == (("screen_text", "on-screen text"), ("video_sound", "video sound"))


def test_the_stored_card_keeps_its_shape():
    shown = reader_evidence(media_pack()["evidence"])
    assert not any(k in e for e in shown for k in ("screen_text", "video_format", "video_sound", "formats", "sound_id"))
    assert json.dumps(shown)  # plain JSON, as the payload stores it
