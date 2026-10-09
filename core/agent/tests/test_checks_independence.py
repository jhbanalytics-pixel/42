"""W8-DEC-16 in Ask: K5 allows Corroborated only for unrelated authors (core/agent/checks.py), on the grouping the
brief gate and the card label use. An Ask answer only carries x (core/agent/answer.py), so the fold of
twitter into x is tested on the card label (core/trust/tests/test_trust_independence.py)."""

import copy

from core.agent.tests.test_checks import ROWS, STORED, FakeWarehouse, make_ctx, make_draft, record, run, verdict


def labelled(extra, cited, *, numbers=True, edit=None):
    stored = copy.deepcopy(STORED) + extra
    for r in stored:
        if edit and r["id"] in edit:
            r.update(edit[r["id"]])
    ctx = make_ctx(stored=stored)
    draft = make_draft(ctx)
    c = draft["claims"][0]
    c["evidence_ids"] = cited
    c["quotes"] = [q for q in c["quotes"] if q["evidence_id"] in cited and q["text"] in ctx.evidence[q["evidence_id"]]["text"]]
    if not numbers:
        c["text"] = "People rated their Sunday lunch plates."
        del c["numbers"]
    draft["evidence"] = [copy.deepcopy(ctx.evidence[i]) for i in ("tt_1", "tt_2", "tt_3", "x_1", *[e["id"] for e in extra])]
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    return next(x for x in answer["claims"] if x["id"] == "c1")["label"], verdict(verdicts, "c1", "K5")["verdict"]


def test_two_handles_of_one_person_on_two_platforms_do_not_corroborate():
    edit = {"x_1": {"text": "Rate my 7 colours honestly, plate by @mpho.cooks.za #7colours"}}
    assert labelled([], ["tt_1", "x_1"], edit=edit) == ("observed", "downgrade")
    assert labelled([], ["tt_1", "x_1"]) == ("corroborated", "pass")


def test_one_clip_posted_under_two_handles_does_not_corroborate():
    edit = {"tt_1": {"thumbnail_url": "https://cdn.example/v/abc/cover.jpg"},
            "x_1": {"thumbnail_url": "https://cdn.example/v/abc/cover.jpg?sig=2"}}
    assert labelled([], ["tt_1", "x_1"], edit=edit) == ("observed", "downgrade")


def test_a_caption_copied_by_two_accounts_does_not_corroborate():
    copy_of = "Sunday 7 colours check. I can't be the only one who puts beetroot right next to the chakalaka"
    edit = {"x_1": {"text": copy_of + " #7colours"}}
    assert labelled([], ["tt_1", "x_1"], edit=edit) == ("observed", "downgrade")


def test_three_unrelated_authors_plus_a_metric_corroborate_and_without_one_do_not():
    assert labelled([], ["tt_1", "tt_2", "tt_3"]) == ("corroborated", "pass")
    assert labelled([], ["tt_1", "tt_2", "tt_3"], numbers=False) == ("observed", "downgrade")


def test_two_unrelated_authors_on_x_are_one_platform():
    extra = [record("tw_1", "x", "@zodwa", "Plate rating night, the pap is perfect #7colours")]
    assert labelled(extra, ["x_1", "tw_1"], numbers=False) == ("observed", "downgrade")
    assert labelled(extra, ["tt_1", "tw_1"], numbers=False) == ("corroborated", "pass")


def test_a_link_through_an_uncited_post_of_the_same_answer_counts():
    edit = {"tt_3": {"thumbnail_url": "https://cdn.example/v/q/cover.jpg", "handle": "@mpho.cooks.za"},
            "x_1": {"thumbnail_url": "https://cdn.example/v/q/cover.jpg"}}
    assert labelled([], ["tt_1", "x_1"], edit=edit) == ("observed", "downgrade")


# The reason beside the label counts the groups the label is judged on, not raw handles.

def rec(i, handle, platform, text):
    return {"id": i, "handle": handle, "platform": platform, "text": text, "flags": []}


def test_a_pair_linked_by_a_shared_page_is_worded_as_one_group_and_stays_observed():
    from core.agent import checks

    a = rec("p1", "alice", "tiktok", "read this https://news.example.com/story/123 now")
    b = rec("p2", "bob", "instagram", "see https://news.example.com/story/123 today")
    top, why = checks._evidence_label({"numbers": []}, [a, b])
    assert top == "observed"
    assert why == "1 unrelated author group on 2 platforms"
    assert "independent" not in why


def test_an_unrelated_pair_is_worded_as_two_groups_and_corroborates():
    from core.agent import checks

    a = rec("p1", "alice", "tiktok", "Dancing to this in Soweto all weekend long")
    b = rec("p2", "bob", "instagram", "My gran tried the step at the braai and nailed it")
    assert checks._evidence_label({"numbers": []}, [a, b]) == ("corroborated", "2 unrelated author groups on 2 platforms")


def test_the_reason_counts_x_and_twitter_as_one_platform_and_names_a_group_with_no_platform():
    from core.agent import checks

    a = rec("p1", "alice", "x", "Dancing to this in Soweto all weekend long")
    b = rec("p2", "bob", "twitter", "My gran tried the step at the braai and nailed it")
    c = rec("p3", "cleo", None, "Nobody told me the step was this hard on the knees")
    assert checks._evidence_label({"numbers": []}, [a, b, c])[1] == "2 unrelated author groups on 1 platform, 1 with no platform"


def test_a_group_with_no_platform_is_not_counted_toward_the_reason_that_the_label_does_not_count():
    from core.agent import checks

    a = rec("p1", "alice", "tiktok", "Dancing to this in Soweto all weekend long")
    a2 = rec("p2", "alice", "instagram", "My gran tried the step at the braai and nailed it")
    b = rec("p3", "bob", None, "Our whole taxi rank was singing it this morning")
    assert checks._evidence_label({"numbers": []}, [a, a2, b]) == (
        "observed", "1 unrelated author group on 2 platforms, 1 with no platform")
    c = rec("p4", "carol", None, "Cannot stop replaying the bridge on the way to work")
    assert checks._evidence_label({"numbers": []}, [a, a2, b, c]) == (
        "observed", "1 unrelated author group on 2 platforms, 2 with no platform")


def test_a_reason_with_every_record_on_a_platform_has_no_tail():
    from core.agent import checks

    a = rec("p1", "alice", "tiktok", "Dancing to this in Soweto all weekend long")
    b = rec("p2", "bob", "instagram", "My gran tried the step at the braai and nailed it")
    assert "no platform" not in checks._evidence_label({"numbers": []}, [a, b])[1]


def test_the_reason_is_worded_from_groups_for_a_single_author_too():
    from core.agent import checks

    a = rec("p1", "alice", "tiktok", "Dancing to this in Soweto all weekend long")
    assert checks._evidence_label({"numbers": []}, [a]) == ("single_source", "1 unrelated author group on 1 platform")
