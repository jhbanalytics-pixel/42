"""The support check and the writer read the same evidence and wording rules (22 Oct 2026 zero-card review, SUPPORT
D1 to D7). The repro tests of SUPPORT-test_support_lane.py are kept here as regressions on the fixed behaviour.

Fakes only: no model, no cloud.
"""

import json

from core.brief import explain, job
from core.brief.payload import NOT_RUN_REASONS, NOT_RUN_WORDS
from core.brief.tests.test_brief_explain import CANDIDATE, FakeModel, W_END, W_START, good, make_pack, run
from core.trust.claims import check_answer, place_fault


def pattern_pack():
    """Three different creators (three creator ids) dance on three days; two have no creators row, so no handle."""
    pack = make_pack()
    ev = pack["evidence"]
    ev[0].update(creator_id="c1", formats=["dance"], posted_at="2026-09-25T08:10:00+02:00")
    ev[1].update(creator_id="c2", formats=["dance"], handle=None, posted_at="2026-09-26T08:10:00+02:00")
    ev[2].update(creator_id="c3", formats=["dance"], handle=None, posted_at="2026-09-27T08:10:00+02:00")
    return pack


# D1: the crowd-word count basis is said to the writer; the check itself is unchanged.
def test_the_writer_is_told_crowd_words_count_handles_and_how_to_word_more_creators():
    pack = pattern_pack()
    user = explain._writer_user(CANDIDATE, pack, "ZA", W_START, W_END)
    assert "3 citable posts from 1 different creators" in user  # the count basis is unchanged
    assert ("Code checks a crowd word against the different handles of the cited posts only: a post with a null "
            "handle counts as no creator there, whatever its creator letter.") in user
    assert "such as \"three different creators\", never a crowd word." in user
    # The check stands: a crowd word the handles do not reach is still a K4 cut by code.
    fault = explain._crowd_fault("Several creators post the dance.", pack, (), pack["evidence"])
    assert fault is not None and "the cited posts show 1" in fault
    # "different creators" is no crowd word, so the wording the writer is pointed to is not cut.
    assert explain._crowd_fault("Three different creators post the dance.", pack, (), pack["evidence"]) is None


# D2: a sentence numeral must be pinned on a claim the sentence rests on.
def sentence_31_on_c2_c3():
    return good(explanation="31 creators post the shaya step in three days, likely because of the braai weekend.",
                explanation_claim_ids=["c2", "c3"])


def test_a_sentence_numeral_pinned_only_on_a_claim_it_does_not_rest_on_is_cut():
    pack = make_pack()
    answer = explain._answer(sentence_31_on_c2_c3(), pack)
    # check_answer alone lets it through (31 is pinned on c1, which survives) ...
    checked, rows = check_answer(answer, window_start=W_START, window_end=W_END, market="ZA")
    assert not [r for r in rows if r["claim_id"] is None and r["rule"] == "K2" and r["verdict"] == "cut"]
    # ... and the resting-claims check cuts it.
    records = {r["id"]: r for r in explain._source_evidence(pack)}
    spans = explain.window_spans(W_START, W_END)[2]
    rows = explain._resting_numeral_rows(checked, ["c2", "c3"], records, spans)
    assert rows == [{"claim_id": None, "rule": "K2", "verdict": "cut", "checker": "code",
                     "detail": "short_answer: numeral '31' has no pinned numbers entry on a claim the sentence "
                               "rests on"}]
    assert checked["short_answer"] == ""
    assert job._reason_code(rows[0]) == "sentence_number_unpinned"


def test_a_numeral_pinned_on_a_resting_claim_or_inside_its_quote_stands():
    pack = make_pack()
    records = {r["id"]: r for r in explain._source_evidence(pack)}
    spans = explain.window_spans(W_START, W_END)[2]
    on_c1 = good(explanation="31 creators post the shaya step, likely because of the braai weekend.",
                 explanation_claim_ids=["c1", "c3"])
    checked, _ = check_answer(explain._answer(on_c1, pack), window_start=W_START, window_end=W_END, market="ZA")
    assert explain._resting_numeral_rows(checked, ["c1", "c3"], records, spans) == []
    assert checked["short_answer"].startswith("31 creators")
    # A year read as a date is no figure, as K2 reads it.
    dated = {"short_answer": "Creators post it since 2026, likely because of the braai weekend.",
             "claims": checked["claims"]}
    assert explain._resting_numeral_rows(dated, ["c3"], records, spans) == []


def test_the_whole_run_holds_a_sentence_whose_numeral_rests_on_no_resting_claim():
    model = FakeModel([sentence_31_on_c2_c3(), sentence_31_on_c2_c3()])
    result = run(model)
    assert result["reason"] == "failed_checks" and result["numbers_only"] is True
    cuts = [r for r in result["checks"] if r["rule"] == "K2" and r["claim_id"] is None and r["verdict"] == "cut"]
    assert cuts and all("on a claim the sentence rests on" in r["detail"] for r in cuts)
    # The repair round got the exact fault.
    [_, repair] = model.writer_calls()
    assert "on a claim the sentence rests on" in repair["user"]
    assert model.critic_calls() == []


# D3: the board line hands the writer wording K3 passes, and the support check sees the mark.
def test_the_board_line_wording_passes_the_place_check():
    for code, name in (("KE", "Kenya"), ("ZA", "South Africa"), ("NG", "Nigeria")):
        line = explain._board_only_line(["yt_1"], name)
        assert f"ranked on {name}'s YouTube trending board, not a creator posting in {name}'s feeds" in line
        assert "ranked on YouTube trending in" not in line
        board = {"id": "yt_1", "platform": "youtube", "market": None, "flags": ["market_assumed"],
                 "source_market": code, "handle": "a"}
        assert place_fault(f"The clip ranked on {name}'s YouTube trending board.", [board]) is None
        # The old wording is still cut: the check is unchanged.
        assert place_fault(f"The clip ranked on YouTube trending in {name}.", [board]) is not None


def test_the_support_check_sees_board_only_and_is_told_what_it_shows():
    pack = make_pack()
    pack["evidence"][0].update(board_only=True)
    claim = {"text": "The clip ranked on South Africa's YouTube trending board.", "label": "observed",
             "evidence_ids": ["tt_1", "ig_2"]}
    user = explain._support_user(claim, pack["evidence"][:2], pack, "ZA")
    heads = {line.split()[1]: json.loads(line[line.index("{"):]) for line in user.split("\n")
             if line.startswith("post ")}
    assert heads["tt_1"]["board_only"] is True
    assert "board_only" not in heads["ig_2"]
    assert ("A cited post marked board_only true was seen only on the YouTube trending board of its source_market's "
            "feeds.") in " ".join(explain.SUPPORT_SYSTEM.split())


# D4: law 8 says how a year and a numbered name are written; K2 itself is unchanged.
def test_the_writer_is_told_numbered_names_are_not_pinned_numbers_and_k2_still_cuts_them():
    law8 = next(line for line in explain.WRITER_SYSTEM.splitlines() if line.startswith("8 "))
    assert '"World Cup 2027" or "AFCON 2025" is cut as an unpinned number' in law8
    assert "Part 2, Season 10 or Matric 2026, is not a pinned number either" in law8
    assert "copy it inside an exact quote of at least two words from a cited post" in law8
    pack = make_pack()
    for text in ("Creators post Part 2 clips of the shaya step.",
                 "Creators tie the shaya step to the Rugby World Cup 2027.",
                 "Creators post Season 10 clips of the shaya step."):
        draft = good()
        draft["claims"][1]["text"] = text
        _, rows = check_answer(explain._answer(draft, pack), window_start=W_START, window_end=W_END, market="ZA")
        assert [r["verdict"] for r in rows if r["claim_id"] == "c2" and r["rule"] == "K2"] == ["cut"]
    for text in ("Creators tie the shaya step to the 2027 Rugby World Cup.", "Creators post the shaya step in 2026."):
        draft = good()
        draft["claims"][1]["text"] = text
        _, rows = check_answer(explain._answer(draft, pack), window_start=W_START, window_end=W_END, market="ZA")
        assert [r["verdict"] for r in rows if r["claim_id"] == "c2" and r["rule"] == "K2"] == ["pass"]


# D5: writer-only context is never support for a clause; the writer is told so where it is asked to name the rival.
def test_the_writer_names_a_rival_and_an_event_date_only_from_a_cited_posts_own_text():
    text = " ".join(explain.WRITER_SYSTEM.split())
    assert ("Name a rival, and any event date the pattern is set against, only as a cited post's own text states it, "
            "and cite that post in a claim the sentence rests on: the facts lines, their calendar names and the "
            "simpler-explanations line are never support for a clause, and the sentence's support check sees only "
            "the cited posts.") in text
    # The calendar name still reaches the writer only as context, never the support check.
    pack = make_pack()
    pack["facts"].append("Post tt_1: posted on Thursday 2026-09-24, the calendar's Heritage Day")
    assert "the calendar's Heritage Day" in explain._writer_user(CANDIDATE, pack, "ZA", W_START, W_END)
    claim = {"text": "Posted on Heritage Day.", "label": "observed", "evidence_ids": ["tt_1"]}
    support = explain._support_user(claim, [pack["evidence"][0]], pack, "ZA")
    assert "the calendar's" not in support


# D6: an explanation that never ran names its cause in failed_reason, still a G10 hold.
def payload_cand(item_id="it_1"):
    pack = make_pack()
    return {"row": {"item_id": item_id, "title": "#shayastep", "kind": "hashtag", "state": "rising"},
            "pack": {"numbers": pack["numbers"], "evidence": pack["evidence"]}, "decision": {},
            "sparkline": {"unit": "posts a day", "points": []}}


def test_an_explanation_that_never_ran_names_its_cause_and_logs_the_error(capsys):
    for reason, words in NOT_RUN_WORDS.items():
        result = {"reason": reason, "numbers_only": True, "checks": [], "error": "Boom: " + "x" * 400}
        item = job._payload_candidate(payload_cand(), result)
        assert item["explanation_status"] == "not_run"
        assert item["failed_reason"] == words
        assert words.startswith("Explanation not run: ")
        err = capsys.readouterr().err
        assert f"brief: explanation not run for it_1: {reason}: Boom: " in err
        assert "x" * 300 not in err  # the error is cut to 300 characters
        assert "Boom" not in json.dumps(item, default=str)  # the model's error text never reaches the payload


def test_a_busy_models_wording_and_a_never_reached_item_keep_theirs():
    cand = dict(payload_cand(), busy_reason=NOT_RUN_REASONS[0])
    item = job._payload_candidate(cand, {"reason": "model_error", "numbers_only": True, "checks": [], "error": "e"})
    assert item["failed_reason"] == NOT_RUN_REASONS[0]
    assert job._payload_candidate(payload_cand(), None)["failed_reason"] is None


def test_the_held_item_keeps_the_not_run_cause_and_the_reader_wording_stays_cause_neutral():
    from core.api.held_words import NOT_COMPLETED_WORDS, plain_reason
    from core.brief.payload import _failed_reason
    for words in NOT_RUN_WORDS.values():
        assert _failed_reason({"failed_reason": words}, "not_run") == words
        out = plain_reason({"reason_text": "Explanation failed its checks", "failed_reason": words,
                            "explanation_status": "not_run"})
        assert out["reason_text"] == NOT_COMPLETED_WORDS
    # A not-run item with any other text keeps no failed_reason, as before.
    assert _failed_reason({"failed_reason": "free text"}, "not_run") is None


# D7: the pattern line's numerals carry the Counts line's note, and the docs say what the writer sees.
def test_the_writer_pattern_line_says_its_counts_are_not_pack_numbers():
    user = explain._writer_user(CANDIDATE, pattern_pack(), "ZA", W_START, W_END)
    block = next(b for b in user.split("\n\n") if b.startswith("Shared sounds and formats"))
    assert block.endswith("Its creators and days are counts for your wording only: they are not pack numbers, so "
                          "write none of them as a numeral.")


def test_the_evidence_docs_say_the_writer_sees_formats_and_the_day_lines_date_posts_only():
    from core.brief import evidence
    doc = " ".join(evidence.__doc__.split())
    assert "The writer sees formats, video_format and the sound as a code letter" in doc
    assert "only screen_text and video_sound stay off the writer" in doc
    day = " ".join(evidence._day_lines.__doc__.split())
    assert "A day line dates the post only, never an event" in day
    assert "law 12" not in day
