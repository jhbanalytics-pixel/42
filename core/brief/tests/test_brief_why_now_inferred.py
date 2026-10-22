"""An adopted format with no post-stated cause publishes with its why-now labelled inferred (Albert, 22 Oct 2026).

"A format adopted by 3+ local creators on 2+ days, with rivals ruled out, can publish with its why-now shown as
inferred." The critic's rival test stays required; code, never the model, counts the adoption among the posts the
writer may cite; an item with a post-stated local cause passes as before; a rival left standing still holds.

Fakes only: no model, no cloud.
"""

import copy

import pytest

from core.api.today import _publication_fault
from core.brief import explain, job
from core.brief.quality import adoption_counted, cultural_critic_passes, why_now_inferred
from core.brief.specificity import assess_specificity
from core.brief.tests.test_brief_explain import RULED_OUT, FakeModel, good, make_pack, run

NO_CAUSE = dict(RULED_OUT, local_why_now=False)
RIVAL_STANDS = dict(RULED_OUT, ruled_out=False, local_why_now=False,
                    reason="the posts follow the weekend and show no shared practice beyond it")


def adopted_pack(days=(25, 26, 26), creators=("c1", "c2", "c3")):
    """The writer's usual three ZA posts, each a dance, by the given creators on the given September days."""
    pack = make_pack()
    for r, day, creator in zip(pack["evidence"], days, creators):
        r.update(formats=["dance"], creator_id=creator, posted_at=f"2026-09-{day}T08:10:00+02:00")
    return pack


def extra(pack, **fields):
    """A fourth dance post by a fourth creator on a third day, not cited by the draft."""
    post = {**copy.deepcopy(pack["evidence"][2]), "id": "tt_4", "handle": "@fourth", "creator_id": "c4",
            "url": "https://x/tt_4", "text": "My own take on the step", "posted_at": "2026-09-27T08:10:00+02:00"}
    post.update(fields)
    pack["evidence"].append(post)
    return pack


def critic_row(result):
    [row] = [r for r in result["checks"] if r["rule"] == "critic"]
    return row


def assert_held(result):
    assert result["numbers_only"] is True and result["reason"] == "failed_checks"
    assert result["why_now_inferred"] is False
    assert critic_row(result)["verdict"] == "cut"
    assert "why_now_adoption" not in result["critic"]
    assert result["specificity"]["status"] == "fail"


def test_three_local_creators_on_two_days_with_the_rival_ruled_out_publish_with_an_inferred_why_now():
    result = run(FakeModel([good()], critic=NO_CAUSE), pack=adopted_pack())
    assert result["numbers_only"] is False and result["reason"] is None
    assert result["why_now_inferred"] is True and result["local_why_now_checked"] is False
    # The stored card's label: specificity passes with why_now_basis inferred.
    assert result["specificity"]["status"] == "pass" and result["specificity"]["reason"] is None
    assert result["specificity"]["why_now_basis"] == "inferred"
    adoption = {"pattern": "format dance", "post_ids": ["tt_1", "ig_2", "tt_3"], "creators": 3, "days": 2}
    assert result["critic"]["why_now_adoption"] == adoption
    row = critic_row(result)
    assert row["verdict"] == "pass" and row["why_now_inferred"] is True and row["local_why_now"] is False
    assert job.check_reason(row) == ("Critic: the simpler explanation was ruled out: a paid campaign; "
                                     "why-now inferred from local adoption")
    # The critic's own answer is stored as it returned it; the count is code's, beside it.
    assert {k: result["critic"][k] for k in explain.CRITIC_FIELDS} == NO_CAUSE


def test_a_post_stated_local_cause_passes_as_before_with_no_inferred_label():
    result = run(FakeModel([good()], critic=RULED_OUT), pack=adopted_pack())
    assert result["reason"] is None and result["local_why_now_checked"] is True
    assert result["why_now_inferred"] is False
    assert "why_now_basis" not in result["specificity"]
    assert "why_now_adoption" not in result["critic"]
    assert job.check_reason(critic_row(result)) == "Critic: the simpler explanation was ruled out: a paid campaign"


def test_two_creators_hold():
    assert_held(run(FakeModel([good()], critic=NO_CAUSE), pack=adopted_pack(creators=("c1", "c2", "c1"))))


def test_one_day_holds():
    assert_held(run(FakeModel([good()], critic=NO_CAUSE), pack=adopted_pack(days=(26, 26, 26))))


@pytest.mark.parametrize("where", [{"market": "NG"}, {"market": None, "source_market": "NG"}],
                         ids=["located_elsewhere", "found_in_another_markets_feeds"])
def test_a_creator_who_is_not_local_does_not_count(where):
    two_local = adopted_pack(creators=("c1", "c2", "c1"))
    assert_held(run(FakeModel([good()], critic=NO_CAUSE), pack=extra(two_local, **where)))
    # The same fourth creator, local, makes three.
    passed = run(FakeModel([good()], critic=NO_CAUSE), pack=extra(adopted_pack(creators=("c1", "c2", "c1"))))
    assert passed["reason"] is None and passed["why_now_inferred"] is True
    assert passed["critic"]["why_now_adoption"]["creators"] == 3


def test_a_post_found_in_the_markets_own_feeds_counts_as_local():
    pack = extra(adopted_pack(creators=("c1", "c2", "c1")), market=None, source_market="ZA")
    result = run(FakeModel([good()], critic=NO_CAUSE), pack=pack)
    assert result["reason"] is None and result["why_now_inferred"] is True


def test_a_rival_that_stands_holds_whatever_the_adoption():
    result = run(FakeModel([good()], critic=RIVAL_STANDS), pack=adopted_pack())
    assert_held(result)
    assert job.check_reason(critic_row(result)).startswith("Critic: a simpler explanation was not ruled out")


@pytest.mark.parametrize("field", ["news_driven", "scheduled_event", "local_reaction"])
def test_a_news_or_event_reading_stays_held(field):
    # ruled_out with an event flag is an inconsistent answer and a cut; with ruled_out false the rival stands.
    for critic in (dict(NO_CAUSE, **{field: True}),
                   dict(RIVAL_STANDS, **{field: True}, **({"news_driven": True} if field == "local_reaction" else {}))):
        assert_held(run(FakeModel([good()], critic=critic), pack=adopted_pack()))


def test_paid_flagged_and_linked_creators_are_not_independent():
    sponsored = adopted_pack()
    sponsored["evidence"][2]["flags"] = ["sponsored"]
    assert_held(run(FakeModel([good()], critic=NO_CAUSE), pack=sponsored))
    # Two authors sharing one thumbnail are linked (core/trust/independence.py), so they count once.
    linked = adopted_pack()
    for r in linked["evidence"][1:]:
        r["thumbnail_url"] = "https://cdn.example/same.jpg"
    assert explain.adopted_pattern(linked, "ZA") is None
    assert explain._independent_creators(linked["evidence"]) == 2


def test_the_count_is_codes_own_and_the_floor_is_exact():
    assert adoption_counted({"pattern": "format dance", "post_ids": ["a", "b", "c"], "creators": 3, "days": 2})
    for bad in ({"post_ids": ["a", "b", "c"], "creators": 2, "days": 2},
                {"post_ids": ["a", "b", "c"], "creators": 3, "days": 1},
                {"post_ids": ["a", "b"], "creators": 3, "days": 2},
                {"post_ids": ["a", "b", "c"], "creators": "3", "days": 2},
                {"post_ids": ["a", "b", "c"], "creators": True, "days": 2}, None, []):
        assert not adoption_counted(bad)
    adoption = {"pattern": "format dance", "post_ids": ["a", "b", "c"], "creators": 3, "days": 2}
    assert cultural_critic_passes(NO_CAUSE, adoption) and why_now_inferred(NO_CAUSE, adoption)
    assert not cultural_critic_passes(NO_CAUSE) and not cultural_critic_passes(RIVAL_STANDS, adoption)
    assert cultural_critic_passes(RULED_OUT) and not why_now_inferred(RULED_OUT, adoption)
    # A model that writes its own count into its answer gains nothing: only the code's record is read.
    assert not cultural_critic_passes(dict(NO_CAUSE, why_now_adoption=adoption))


def test_specificity_and_today_read_the_inferred_basis_the_same_way():
    pack = adopted_pack()
    result = run(FakeModel([good()], critic=NO_CAUSE), pack=pack)
    again = assess_specificity(explanation=result["explanation"], claims=result["claims"],
                               explanation_claim_ids=result["explanation_claim_ids"], evidence=pack["evidence"],
                               market="ZA", local_why_now=False, why_now_inferred=True)
    assert again == result["specificity"]
    held = assess_specificity(explanation=result["explanation"], claims=result["claims"],
                              explanation_claim_ids=result["explanation_claim_ids"], evidence=pack["evidence"],
                              market="ZA")
    assert held["reason"] == "local_why_now_not_checked" and "why_now_basis" not in held
    card = {"item_id": "it_1", "kind": "hashtag", "title": "#shayastep"}
    entry = {"item_id": "it_1", **result["critic"]}
    assert _publication_fault(card, {"critic": [entry], "held_back": {"items": []}}) is None
    bare = {k: v for k, v in entry.items() if k != "why_now_adoption"}
    assert _publication_fault(card, {"critic": [bare], "held_back": {"items": []}}) == (
        "A distinct cultural pattern was not established")


def test_the_trust_doc_states_the_rule():
    from pathlib import Path
    text = " ".join((Path(__file__).parents[3] / "docs/full-42/TRUST.md").read_text(encoding="utf-8").split())
    assert ("adopted by 3 or more independent local creators on 2 or more days among the posts the writer may cite"
            in text)
    assert "its why-now is labelled Inferred" in text
