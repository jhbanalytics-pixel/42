"""W8-DEC-16 in the card label: K5 allows Corroborated only for unrelated authors (core/trust/claims.py), on the
grouping the gate uses (core/trust/independence.py). x and twitter are one platform."""

import pytest

from core.brief import gatectx
from core.detect import coaction, neardup
from core.trust import independence
from core.trust.tests.test_trust_claims import NUM_28, answer, claim, filler, label_of, record, run, verdict

CAPTION = "Capetonians are going mad for the new shaya step challenge this weekend"


def labelled(records, cited, *, numbers=None, text="Creators are posting the step"):
    c = claim("c1", text, cited, label="corroborated", numbers=numbers)
    ans = answer([c] + filler())
    ans["evidence"] = records + [r for r in ans["evidence"] if r["id"] == "yt_8"]
    new, checks = run(ans)
    return label_of(new, "c1"), verdict(checks, "c1", "K5")


def test_two_handles_of_one_person_on_two_platforms_do_not_corroborate():
    one = record("a1", "tiktok", "@thandi_za", "Learning the shaya step with my gran this weekend, such fun")
    two = record("a2", "instagram", "@thandi.mokoena", "Same shaya step lesson today, cut by @thandi_za")
    assert labelled([one, two], ["a1", "a2"]) == ("observed", "downgrade")


def test_one_clip_cross_posted_under_two_handles_does_not_corroborate():
    one = record("a1", "tiktok", "@first_handle", "first words here")
    two = record("a2", "instagram", "@second_handle", "other words there")
    one["thumbnail_url"] = two["thumbnail_url"] = "https://cdn.example/v/abc123/cover.jpg"
    assert labelled([one, two], ["a1", "a2"]) == ("observed", "downgrade")


def test_a_link_through_an_uncited_post_of_the_same_pack_counts():
    cited_a = record("a1", "tiktok", "@thandi", "Nobody told me the step was this hard on the knees")
    cited_b = record("b1", "instagram", "@sipho", "My gran learned the new school dance in one afternoon")
    other = record("a2", "tiktok", "@thandi", "Matric farewell rehearsal went completely off the rails")
    other["thumbnail_url"] = cited_b["thumbnail_url"] = "https://cdn.example/v/xyz/cover.jpg"
    assert labelled([cited_a, cited_b, other], ["a1", "b1"]) == ("observed", "downgrade")


def test_two_unrelated_authors_on_two_platforms_corroborate():
    one = record("a1", "tiktok", "@thandi", "Nobody told me the step was this hard on the knees")
    two = record("a2", "instagram", "@sipho", "My gran learned the new school dance in one afternoon")
    assert labelled([one, two], ["a1", "a2"]) == ("corroborated", "pass")


def test_three_unrelated_authors_plus_a_metric_corroborate_and_without_one_do_not():
    rows = [record("a1", "tiktok", "@thandi", "Nobody told me the step was this hard on the knees"),
            record("a2", "tiktok", "@sipho", "My gran learned the new school dance in one afternoon"),
            record("a3", "tiktok", "@lerato", "Matric farewell rehearsal went completely off the rails")]
    text = "Posts reached 2.8 times usual daily posts"
    assert labelled(rows, ["a1", "a2", "a3"], numbers=[NUM_28], text=text) == ("corroborated", "pass")
    assert labelled(rows, ["a1", "a2", "a3"]) == ("observed", "downgrade")


def test_three_accounts_quoting_one_article_with_a_metric_do_not_corroborate():
    link = "https://www.news24.com/news24/southafrica/news/shaya-step-takes-over-schools-20261005"
    rows = [record("a1", "tiktok", "@thandi", f"Schools are going wild {link}"),
            record("a2", "tiktok", "@sipho", f"This is everywhere now {link}?utm_source=x"),
            record("a3", "tiktok", "@lerato", f"Have you seen this one {link}/")]
    text = "Posts reached 2.8 times usual daily posts"
    assert labelled(rows, ["a1", "a2", "a3"], numbers=[NUM_28], text=text) == ("observed", "downgrade")


def test_x_and_twitter_are_one_platform():
    one = record("a1", "x", "@thandi", "Nobody told me the step was this hard on the knees")
    two = record("a2", "twitter", "@sipho", "My gran learned the new school dance in one afternoon")
    assert labelled([one, two], ["a1", "a2"]) == ("observed", "downgrade")
    three = record("a3", "tiktok", "@lerato", "Matric farewell rehearsal went completely off the rails")
    assert labelled([one, three], ["a1", "a3"]) == ("corroborated", "pass")
    assert labelled([two, three], ["a2", "a3"]) == ("corroborated", "pass")


def test_flagged_authors_still_do_not_count():
    one = record("a1", "tiktok", "@thandi", "Nobody told me the step was this hard on the knees")
    two = record("a2", "instagram", "@sipho", "My gran learned the new school dance in one afternoon", flags=["paid"])
    assert labelled([one, two], ["a1", "a2"]) == ("single_source", "downgrade")


# The shared module


def test_the_gate_and_the_label_use_the_same_function():
    rows = [record("a1", "tiktok", "@a", CAPTION), record("a2", "x", "@b", CAPTION.upper())]
    groups = independence.independent_groups(rows, excluded=gatectx.NOT_INDEPENDENT)
    assert [g["handles"] for g in groups] == [{"a", "b"}]
    assert groups == gatectx.independent_groups(rows)


def test_platform_names_fold_twitter_into_x():
    assert independence.platform_name("Twitter") == "x" and independence.platform_name(" X ") == "x"
    assert independence.platform_name("tiktok") == "tiktok" and independence.platform_name(None) == ""


def test_the_decision_function():
    g = lambda *platforms: {"handles": {"h"}, "platforms": set(platforms), "post_ids": {"p"}}  # noqa: E731
    assert independence.is_corroborated([g("tiktok"), g("x")], False) is True
    assert independence.is_corroborated([g("tiktok", "x")], True) is False
    assert independence.is_corroborated([g("tiktok"), g("tiktok")], True) is False
    assert independence.is_corroborated([g("tiktok"), g("tiktok"), g("tiktok")], True) is True
    assert independence.is_corroborated([g("tiktok"), g("tiktok"), g("tiktok")], False) is False
    assert independence.is_corroborated([], True) is False


# The light copy of the co-action text rule stays equal to the detect modules'


def test_the_text_rule_constants_equal_co_actions():
    assert (independence.JACCARD, independence.SHINGLE, independence.MIN_TEXT_CHARS) == (
        coaction.JACCARD, coaction.SHINGLE, coaction.MIN_TEXT_CHARS)
    assert independence.PLACEHOLDERS == coaction.PLACEHOLDERS
    assert independence.URL.pattern == coaction.URL.pattern


@pytest.mark.parametrize("text", [CAPTION, "Visit https://a.example/x @someone 2026 #tag and 12:30 later today ok",
                                  "short", "", None, "Ngiyabonga   kakhulu,\nle challenge imnandi!"])
def test_mask_and_plain_text_equal_co_actions(text):
    assert independence.mask(text) == coaction.mask(text)
    assert independence.plain_text(text) == neardup.plain_text(text)


def test_flags_are_read_in_lower_case_and_an_author_needs_a_handle():
    rows = [record("a1", "tiktok", "@a", "Nobody told me the step was this hard on the knees", flags=["PAID"]),
            record("a2", "x", "@b", "My gran learned the new school dance in one afternoon"),
            record("a3", "x", None, "Matric farewell rehearsal went completely off the rails")]
    groups = independence.independent_groups(rows, excluded={"paid"})
    assert [g["handles"] for g in groups] == [{"b"}]


def test_a_record_with_no_platform_is_not_a_second_platform():
    one = record("a1", "tiktok", "@thandi", "Nobody told me the step was this hard on the knees")
    two = record("a2", None, "@sipho", "My gran learned the new school dance in one afternoon")
    blank = record("a3", "  ", "@zodwa", "Matric farewell rehearsal went completely off the rails")
    groups = independence.independent_groups([one, two, blank], excluded=gatectx.NOT_INDEPENDENT)
    assert len(groups) == 3
    assert sorted(p for g in groups for p in g["platforms"]) == ["tiktok"]
    assert independence.is_corroborated(groups, False) is False
    assert labelled([one, two], ["a1", "a2"]) == ("observed", "downgrade")


def test_a_missing_platform_still_counts_the_author_toward_three_with_a_metric():
    rows = [record("a1", "tiktok", "@thandi", "Nobody told me the step was this hard on the knees"),
            record("a2", None, "@sipho", "My gran learned the new school dance in one afternoon"),
            record("a3", "tiktok", "@zodwa", "Matric farewell rehearsal went completely off the rails")]
    groups = independence.independent_groups(rows, excluded=gatectx.NOT_INDEPENDENT)
    assert independence.is_corroborated(groups, True) is True
    assert independence.is_corroborated(groups, False) is False

