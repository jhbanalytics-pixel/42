"""The writer claims only what its posts show (the 5 Oct brief review, 6 Oct).

On the 5 Oct brief 9 of 18 held topics failed on a claim or sentence the support check did not find in its posts, and
a "Multiple creators" sentence sat beside "1 creator in 3 days". The writer now gets the pack's own counts and the
crowd words the card's creator count allows, posts listed most on topic first, and the one repair round when a
support cut holds the draft. A crowd word the card's count does not reach is a K4 cut by code. No check is loosened:
the repaired draft meets every check again in full, and only one repair round is ever spent.
"""

import copy
import json
import re

from core.brief import explain, job
from core.brief.tests.test_brief_explain import (CANDIDATE, FakeModel, assert_numbers_only, good, make_pack,
                                                 outside_fences, post, run)
from core.brief.tests.test_brief_second_draft import WORDING, Critics
from core.brief.tests.test_brief_second_draft import run as run_second


def one_creator_pack():
    pack = make_pack()
    pack["numbers"][0] = dict(pack["numbers"][0], value=1)
    return pack


def cited_ids(prompt):
    return [line.split(" ", 2)[1] for line in prompt.splitlines() if re.match(r"^post \S+ \{", line)]


def writer_heads(prompt):
    return [json.loads(line[5:]) for line in prompt.splitlines() if line.startswith("post {")]


class Reasons(FakeModel):
    """FakeModel whose support check gives `reason` with each verdict it scripts."""

    def __init__(self, drafts, verdicts, reason="no cited post names a braai", **kw):
        super().__init__(drafts, verdicts=verdicts, **kw)
        self.reason = reason

    def complete_json(self, **kw):
        out, usage = super().complete_json(**kw)
        if "verdict" in kw["schema"]["properties"] and out["verdict"] != "supported":
            out = dict(out, reason=self.reason)
        return out, usage


# The evidence pack the writer reads


def test_the_writer_gets_the_packs_own_counts_and_the_crowd_words_its_creator_count_allows():
    model = FakeModel([good()])
    run(model)
    user = model.writer_calls()[0]["user"]
    [line] = [x for x in outside_fences(user).splitlines() if x.startswith("Counts,")]
    assert "3 citable posts from 3 different creators; 3 local posts from 3 different creators" in line
    assert "write none of them as a numeral" in line
    assert "The card's creator count is 31, for creators in 3 days" in line
    assert "multiple" in line and "many" in line and "dozens of" in line and "hundreds of" not in line


def test_a_one_creator_card_tells_the_writer_to_write_no_crowd_word():
    model = FakeModel([good()])
    run(model, pack=one_creator_pack())
    user = outside_fences(model.writer_calls()[0]["user"])
    assert ("The card's creator count is 1, for creators in 3 days; write no crowd word about creators, such as "
            "multiple, several or many.") in user


def test_without_a_creators_number_the_count_is_the_packs_different_handles():
    pack = make_pack()
    pack["numbers"] = [n for n in pack["numbers"] if not n["unit"].startswith("creators")]
    pack["evidence"][2]["handle"] = "@THANDI_MOVES"
    assert explain._card_creators(pack) == (2, "different creators in the pack's posts")


def test_the_writer_laws_say_to_claim_only_what_the_posts_show():
    laws = {line.split(" ", 1)[0]: line for line in explain.WRITER_SYSTEM.splitlines() if re.match(r"^\d+ ", line)}
    assert "outside the pack" in laws["14"] and "not evidence" in laws["14"]
    assert "only the posts whose own text or fields show that claim" in laws["15"]
    assert "Never guess a count" in laws["16"] and "Code checks this" in laws["16"]
    assert "posted_at" in laws["17"] and "Never move, guess or work out a date" in laws["17"]


def test_on_topic_posts_come_first_and_off_topic_ones_are_named():
    # Tester, 5 Oct: a held "bafana, pitso, egypt" item carried Kaizer Chiefs posts among its 21.
    pack = make_pack()
    pack["evidence"] = [
        post("za_1", "x", "@soccer_page", "Kaizer Chiefs lift the Champ of Champs trophy"),
        post("za_2", "x", "@fan_one", "Pitso has Bafana ready for Egypt, what a squad"),
        post("za_3", "tiktok", "@fan_two", "Bafana fans in full voice tonight"),
    ]
    candidate = dict(CANDIDATE, kind="topic", title="bafana, pitso, egypt")
    user = explain._writer_user(candidate, pack, "ZA", "s", "e")
    rows = writer_heads(user)
    assert [r["id"] for r in rows] == ["za_2", "za_3", "za_1"]
    assert [r["title_terms_named"] for r in rows] == [3, 1, 0]
    assert ('The trend title has 3 terms. Posts whose text names none of them: ["za_1"]; such a post may be about '
            'something else') in outside_fences(user)
    # The title's own words stay inside their fence; only code-written counts and ids sit outside.
    assert "pitso" not in outside_fences(user).lower()
    # Only the order the writer reads changes: the pack itself is untouched.
    assert [r["id"] for r in pack["evidence"]] == ["za_1", "za_2", "za_3"]


def test_a_hashtag_title_matches_its_written_out_form():
    record = {"text": "So proud of #BimboAdemoye today"}
    assert explain._terms_named(record, explain._title_terms("#bimboademoye")) == 1
    assert explain._terms_named({"text": "egg on toast"}, ["egg"]) == 1
    assert explain._terms_named({"text": "eggs on toast"}, ["egg"]) == 0


def test_the_support_check_reads_cited_on_topic_posts_first_without_the_title():
    pack = make_pack()
    pack["evidence"][0]["text"] = "Monday mood at the office"
    claim = {"text": "Creators post the shaya step.", "label": "observed", "numbers": []}
    prompt = explain._support_user(claim, pack["evidence"], pack, "ZA", explain._title_terms("#shayastep"))
    assert cited_ids(prompt) == ["ig_2", "tt_3", "tt_1"]
    assert "shayastep" not in prompt
    assert cited_ids(explain._support_user(claim, pack["evidence"], pack, "ZA")) == ["tt_1", "ig_2", "tt_3"]


# The crowd-word check (K4 by code)


def multiple():
    draft = good()
    draft["claims"][0]["text"] = 'Multiple creators post the "shaya step" dance, 31 creators in three days.'
    return draft


def test_a_crowd_word_the_card_count_does_not_reach_cuts_the_claim_and_goes_to_the_repair():
    pack = one_creator_pack()
    draft = multiple()
    draft["claims"][0]["text"] = 'Multiple creators post the "shaya step" dance.'
    draft["claims"][0]["number_ids"] = []
    model = FakeModel([draft, good(claims=[draft["claims"][0]] + good()["claims"][1:])])
    result = run(model, pack=pack)
    rows = [r for r in result["checks"] if r["claim_id"] == "c1" and r["rule"] == "K4" and r["checker"] == "code"]
    assert [r["verdict"] for r in rows] == ["cut", "cut"]
    assert rows[0]["detail"].startswith(job.REPAIR)
    assert "'Multiple creators' needs a creator count of at least 2; the card shows 1" in rows[1]["detail"]
    assert "Multiple creators" in model.writer_calls()[1]["user"] and "crowd wording" in model.writer_calls()[1]["user"]
    # c1 is cut and the sentence rests on it, so the card is held as on any unsupported claim.
    assert_numbers_only(result, "failed_checks")
    assert job.failed_reason({**result, "rests_on": ["c1", "c3"]}) == (
        "Support check: a claim the explanation rests on was not supported by its posts")


def test_the_same_crowd_word_passes_when_the_card_count_reaches_it():
    result = run(FakeModel([multiple()]))
    assert result["reason"] is None
    assert not [r for r in result["checks"] if r["rule"] == "K4" and r["checker"] == "code"]


def test_a_crowd_word_in_the_sentence_holds_the_card():
    draft = good(explanation="Multiple creators pair the shaya step with braais, likely because of the holiday "
                             "weekend.")
    model = FakeModel([draft, copy.deepcopy(draft)])
    result = run(model, pack=one_creator_pack())
    assert_numbers_only(result, "failed_checks")
    [row] = [r for r in result["checks"] if r["claim_id"] is None and r["rule"] == "K4"
             and not r["detail"].startswith(job.REPAIR)]
    assert row["checker"] == "code" and row["detail"].startswith("short_answer: crowd wording:")
    assert model.support_calls() == []


def test_a_crowd_word_inside_a_verified_quote_is_the_creators_own():
    pack = one_creator_pack()
    pack["evidence"][1]["text"] = "Heritage Day braai and many people are doing the shaya step"
    draft = good()
    draft["claims"][2]["text"] = ('It likely took off over the Heritage Day weekend: "many people are doing the shaya '
                                  'step".')
    draft["claims"][2]["quotes"] = [{"evidence_id": "ig_2", "text": "many people are doing the shaya step"}]
    checked = explain._answer(draft, pack)
    records = {r["id"]: r for r in pack["evidence"]}
    rows = explain._crowd_rows(checked, ["c1", "c3"], records, pack)
    assert rows == [] and [c["id"] for c in checked["claims"]] == ["c1", "c2", "c3"]
    draft["claims"][2]["quotes"] = []
    checked = explain._answer(draft, pack)
    assert [r["claim_id"] for r in explain._crowd_rows(checked, ["c1", "c3"], records, pack)] == ["c3"]


def test_crowd_words_count_creators_not_posts():
    pack = one_creator_pack()
    for text in ("Creators share many posts by creators.", "Two different creators post it.", "Creators post it."):
        assert explain._crowd_fault(text, pack) is None, text
    for text in ("Several South African TikTok creators post it.", "Many of the creators post it.",
                 "A number of local fans post it."):
        assert explain._crowd_fault(text, pack) is not None, text



def test_growing_crowd_words_need_the_card_count_too():
    """Writer review, 6 Oct: "a growing number of creators" and "more and more creators" went unread."""
    pack = make_pack()
    two = copy.deepcopy(pack)
    two["numbers"][0] = dict(two["numbers"][0], value=2)
    for text in ("A growing number of creators post the step.", "More and more local creators join in."):
        assert explain._crowd_fault(text, one_creator_pack()) is not None, text
        assert explain._crowd_fault(text, two) is not None, text
        assert explain._crowd_fault(text, pack) is None, text
    assert "a growing number of" in explain._crowd_words(pack) and "more and more" in explain._crowd_words(pack)

# The repair round on a support cut that holds the draft


def test_a_support_cut_that_holds_the_draft_gets_the_repair_with_the_claim_and_its_reason():
    repaired = good()
    repaired["claims"][2]["text"] = "It likely rose over the long weekend."
    model = Reasons([good(), repaired], verdicts={"Heritage Day weekend": "unsupported"})
    result = run(model)
    assert result["reason"] is None and result["explanation"]
    assert len(model.writer_calls()) == 2
    prompt = model.writer_calls()[1]["user"]
    assert prompt.startswith(model.writer_calls()[0]["user"])
    assert "Rewrite only those" in prompt and "Keep every other claim exactly as it is" in prompt
    fenced = prompt.rsplit(explain.FENCE_OPEN, 1)[1]
    assert "claim c3 (unsupported): no cited post names a braai" in fenced
    assert "no cited post names a braai" not in outside_fences(prompt)
    # Every check runs again on the repaired draft: three claims and the sentence, then the critic, once.
    assert len(model.support_calls()) == 3 + 4
    assert len(model.critic_calls()) == 1
    first = [r for r in result["checks"] if r["detail"].startswith(job.REPAIR)]
    assert {r["claim_id"] for r in first if r["rule"] == "K4" and r["verdict"] == "cut"} == {"c3"}
    assert not [r for r in result["checks"] if not r["detail"].startswith(job.REPAIR) and r["verdict"] == "cut"]


def test_the_repair_rewrites_only_the_failed_claim():
    repaired = good()
    repaired["claims"][0]["text"] = "Everyone in the country does the shaya step."
    repaired["claims"][2]["text"] = "It likely rose over the long weekend."
    model = FakeModel([good(), repaired], verdicts={"Heritage Day weekend": "unsupported"})
    result = run(model)
    assert result["reason"] is None
    texts = {c["id"]: c["text"] for c in result["claims"]}
    assert texts["c1"] == good()["claims"][0]["text"]
    assert texts["c3"] == "It likely rose over the long weekend."


def test_a_sentence_support_cut_names_the_sentence_in_the_repair():
    model = Reasons([good(), good(explanation="@thandi_moves and @jozi_dance do the shaya step at braais, likely "
                                              "because of the long weekend.")],
                    verdicts={"@thandi_moves pairs": "partial"}, reason="no post names a new track")
    result = run(model)
    assert result["reason"] is None
    fenced = model.writer_calls()[1]["user"].rsplit(explain.FENCE_OPEN, 1)[1]
    assert "the explanation sentence (partial): no post names a new track" in fenced


def test_a_support_cut_the_draft_can_stand_without_is_cut_with_no_repair():
    model = FakeModel([good()], verdicts={"earliest post": "unsupported"})
    result = run(model)
    assert result["reason"] is None and [c["id"] for c in result["claims"]] == ["c1", "c3"]
    assert len(model.writer_calls()) == 1


def test_a_repaired_draft_that_still_fails_is_cut_on_its_own_rows():
    model = FakeModel([good(), good()], verdicts={"Heritage Day weekend": "unsupported"})
    result = run(model)
    assert_numbers_only(result, "failed_checks")
    assert len(model.writer_calls()) == 2
    final = [r for r in result["checks"] if not r["detail"].startswith(job.REPAIR)]
    assert [r["claim_id"] for r in final if r["rule"] == "K4" and r["verdict"] == "cut"] == ["c3"]
    assert job.failed_reason({**result, "rests_on": ["c1", "c3"]}) == (
        "Support check: a claim the explanation rests on was not supported by its posts")


def test_only_one_repair_round_when_the_code_checks_spent_it():
    bad = good()
    bad["claims"][1]["number_ids"] = ["n9"]
    model = FakeModel([bad, good()], verdicts={"Heritage Day weekend": "unsupported"})
    result = run(model)
    assert_numbers_only(result, "failed_checks")
    assert len(model.writer_calls()) == 2


def test_a_second_draft_gets_no_support_repair():
    # The critic holds the first draft on its why-now alone; the second draft's resting claim then fails support and
    # is cut, with no third writer call (TRUST.md section 3 step 8: no further repair round).
    model = Critics([good(), good(), good()], [WORDING, WORDING])
    original = model.complete_json
    writes = []

    def complete_json(**kw):
        if "explanation_claim_ids" in kw["schema"]["properties"]:
            writes.append(1)
            if len(writes) == 2:
                model.verdicts = {"Heritage Day weekend": "unsupported"}
        return original(**kw)

    model.complete_json = complete_json
    result = run_second(model)
    assert_numbers_only(result, "failed_checks")
    assert len(writes) == 2


def test_a_support_cut_that_unpins_the_sentences_number_gets_the_repair():
    draft = good(explanation="31 creators do the shaya step at braais, likely because of the Heritage Day weekend.",
                 explanation_claim_ids=["c2", "c3"])
    draft["claims"][1]["text"] = 'The earliest post in the pack is from @thandi_moves: "shaya step".'
    draft["claims"][1]["quotes"] = [{"evidence_id": "tt_1", "text": "shaya step"}]
    model = FakeModel([draft, copy.deepcopy(draft)], verdicts={"dance, 31 creators": "unsupported"})
    result = run(model)
    assert_numbers_only(result, "failed_checks")
    assert len(model.writer_calls()) == 2
    assert "claim c1 (unsupported)" in model.writer_calls()[1]["user"]
    assert any(r["claim_id"] is None and r["rule"] == "K2" and r["detail"].startswith(job.REPAIR)
               for r in result["checks"])
