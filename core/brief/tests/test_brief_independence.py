"""W8-DEC-16: Corroborated needs genuinely independent people (core/brief/gatectx.py, TRUST.md section 3 step 5).

Corroborated: unrelated authors on 2 platforms, or 3 unrelated authors plus a metric. Authors are unrelated when no
post of one reuses media, caption text, a linked page or a reply relation with a post of the other, and a person
posting under several handles counts once.

Rule for missing data, tested below: a signal that is not stored never links two authors, so authors are unrelated
unless a stored signal links them. A record with no handle is still not an author.
"""

import random

import pytest

from core.brief import gatectx
from core.brief.tests.test_brief_gatectx import NUMBER, ctx, duck, ev, measured, world
from core.detect.tests.fixtures import D

CAPTION = "Capetonians are going mad for the new shaya step challenge this weekend"
ARTICLE = "https://www.news24.com/news24/southafrica/news/shaya-step-takes-over-schools-20261005"


def rec(pid, platform, handle, text="caption", **extra):
    record = ev(pid, platform, handle, text=text)
    record.update(extra)
    return record


def corroborated(con, evidence, numbers=()):
    return ctx(con, evidence=evidence, numbers=list(numbers))["corroborated_unbiased"]


def people(con, *rows):
    """rows: (post_id, platform, creator_id, handle, display_name)."""
    duck.load(con, "core.posts", [{"post_id": p, "platform": pl, "creator_id": c, "post_date": D}
                                  for p, pl, c, _, _ in rows])
    duck.load(con, "core.creators", [{"creator_id": c, "platform": pl, "handle": h, "display_name": n}
                                     for _, pl, c, h, n in rows])


# One person under several handles counts once


def test_two_handles_of_one_person_on_two_platforms_do_not_corroborate():
    con = world()
    measured(con, "p1", "p2")
    people(con, ("p1", "tiktok", "thandi_za", "thandi_za", "Thandi Mokoena"),
           ("p2", "instagram", "thandi.mokoena", "thandi.mokoena", "Thandi Mokoena"))
    evidence = [rec("p1", "tiktok", "thandi_za"), rec("p2", "instagram", "thandi.mokoena")]
    assert corroborated(con, evidence) is False


def test_two_handles_of_one_person_on_one_platform_count_once_for_the_three_author_route():
    con = world()
    measured(con, "p1", "p2", "p3")
    people(con, ("p1", "tiktok", "a_main", "a_main", "Sipho Dlamini"),
           ("p2", "tiktok", "a_backup", "a_backup", "Sipho Dlamini"),
           ("p3", "tiktok", "b", "b", "Lerato"))
    evidence = [rec("p1", "tiktok", "a_main"), rec("p2", "tiktok", "a_backup"), rec("p3", "tiktok", "b")]
    assert corroborated(con, evidence, [NUMBER]) is False


def test_two_different_people_with_different_names_on_two_platforms_corroborate():
    con = world()
    measured(con, "p1", "p2")
    people(con, ("p1", "tiktok", "thandi_za", "thandi_za", "Thandi Mokoena"),
           ("p2", "instagram", "jozi_hub", "jozi_hub", "Jozi Hub"))
    evidence = [rec("p1", "tiktok", "thandi_za"), rec("p2", "instagram", "jozi_hub")]
    assert corroborated(con, evidence) is True


def test_a_short_or_missing_display_name_never_folds_two_handles():
    con = world()
    measured(con, "p1", "p2")
    people(con, ("p1", "tiktok", "a", "a", "Jo"), ("p2", "instagram", "b", "b", "Jo"))
    evidence = [rec("p1", "tiktok", "a"), rec("p2", "instagram", "b")]
    assert corroborated(con, evidence) is True


# Media


def test_three_accounts_sharing_one_clip_do_not_corroborate():
    con = world()
    measured(con, "p1", "p2", "p3")
    thumbs = ["https://cdn.example/v/abc123/cover.jpg?sig=1", "https://cdn.example/v/abc123/cover.jpg?sig=2",
              "https://cdn.example/v/abc123/cover.jpg"]
    evidence = [rec(f"p{i + 1}", "tiktok", f"user{i}", f"own words number {i}", thumbnail_url=t)
                for i, t in enumerate(thumbs)]
    assert corroborated(con, evidence, [NUMBER]) is False


def test_one_clip_cross_posted_to_two_platforms_by_two_handles_does_not_corroborate():
    con = world()
    measured(con, "p1", "p2")
    evidence = [rec("p1", "tiktok", "a", "first words here", thumbnail_url="https://cdn.example/v/abc123/cover.jpg"),
                rec("p2", "instagram", "b", "other words there", thumbnail_url="https://cdn.example/v/abc123/cover.jpg")]
    assert corroborated(con, evidence) is False


def test_different_clips_do_not_link_and_a_missing_thumbnail_never_links():
    con = world()
    measured(con, "p1", "p2", "p3")
    evidence = [rec("p1", "tiktok", "a", thumbnail_url="https://cdn.example/v/one/cover.jpg"),
                rec("p2", "tiktok", "b", thumbnail_url="https://cdn.example/v/two/cover.jpg"),
                rec("p3", "tiktok", "c", thumbnail_url=None)]
    assert corroborated(con, evidence, [NUMBER]) is True


# Caption text


def test_three_accounts_copying_one_caption_do_not_corroborate():
    con = world()
    measured(con, "p1", "p2", "p3")
    evidence = [rec("p1", "twitter", "a", CAPTION), rec("p2", "twitter", "b", CAPTION.upper()),
                rec("p3", "twitter", "c", CAPTION + " #shayastep")]
    assert corroborated(con, evidence, [NUMBER]) is False


def test_a_near_identical_caption_on_another_platform_does_not_corroborate():
    con = world()
    measured(con, "p1", "p2")
    evidence = [rec("p1", "tiktok", "a", CAPTION), rec("p2", "facebook", "b", CAPTION + "!!")]
    assert corroborated(con, evidence) is False


def test_three_unrelated_captions_plus_a_metric_corroborate():
    con = world()
    measured(con, "p1", "p2", "p3")
    evidence = [rec("p1", "tiktok", "a", "Nobody told me the shaya step was this hard on the knees"),
                rec("p2", "tiktok", "b", "My gran learned the new school dance in one afternoon"),
                rec("p3", "tiktok", "c", "Matric farewell rehearsal went completely off the rails")]
    assert corroborated(con, evidence) is False
    assert corroborated(con, evidence, [NUMBER]) is True


def test_short_identical_captions_do_not_link():
    con = world()
    measured(con, "p1", "p2")
    evidence = [rec("p1", "tiktok", "a", "so true"), rec("p2", "youtube", "b", "so true")]
    assert corroborated(con, evidence) is True


# Linked page


def test_three_accounts_quoting_one_article_do_not_corroborate():
    con = world()
    measured(con, "p1", "p2", "p3")
    evidence = [rec("p1", "twitter", "a", f"Wow, schools are going wild {ARTICLE}"),
                rec("p2", "twitter", "b", f"This is everywhere now {ARTICLE}?utm_source=twitter"),
                rec("p3", "facebook", "c", f"Have you seen this one\n{ARTICLE.replace('https://www.', 'http://')}/")]
    assert corroborated(con, evidence, [NUMBER]) is False


def test_a_bare_site_address_or_different_pages_do_not_link():
    con = world()
    measured(con, "p1", "p2", "p3")
    evidence = [rec("p1", "twitter", "a", "Reading about it at https://www.news24.com today"),
                rec("p2", "twitter", "b", "Same site https://www.news24.com again"),
                rec("p3", "twitter", "c", "Other page https://www.news24.com/other/story-1")]
    assert corroborated(con, evidence, [NUMBER]) is True


# Reply relation


def test_a_post_that_mentions_another_cited_author_is_related_to_them():
    con = world()
    measured(con, "p1", "p2")
    evidence = [rec("p1", "twitter", "@thandi", "The new step is wild"),
                rec("p2", "facebook", "jozi_hub", "@Thandi agreed, my whole street is doing it")]
    assert corroborated(con, evidence) is False


def test_a_post_that_links_another_cited_post_is_related_to_it():
    con = world()
    measured(con, "p1", "p2")
    evidence = [rec("p1", "twitter", "thandi", "The new step is wild", url="https://x.com/thandi/status/1001"),
                rec("p2", "facebook", "jozi_hub", "Quoting this https://x.com/thandi/status/1001 with my own view",
                    url="https://facebook.com/jozi_hub/posts/9")]
    assert corroborated(con, evidence) is False


def test_a_mention_of_someone_outside_the_pack_does_not_link():
    con = world()
    measured(con, "p1", "p2")
    evidence = [rec("p1", "twitter", "thandi", "Thanks @someoneelse for the tip"),
                rec("p2", "facebook", "jozi_hub", "Also thanks @someoneelse")]
    assert corroborated(con, evidence) is True


# Groups, the two routes, and the rule for missing data


def test_related_authors_form_one_group_even_through_a_third():
    con = world()
    measured(con, "p1", "p2", "p3")
    evidence = [rec("p1", "tiktok", "a", "first alone", thumbnail_url="https://c.example/x/1.jpg"),
                rec("p2", "tiktok", "b", "second alone", thumbnail_url="https://c.example/x/1.jpg",
                    url="https://tiktok.com/@b/video/2"),
                rec("p3", "tiktok", "c", "third mentions @b here")]
    assert corroborated(con, evidence, [NUMBER]) is False


def test_two_unrelated_groups_on_two_platforms_corroborate_when_one_group_spans_both():
    con = world()
    measured(con, "p1", "p2", "p3")
    clip = "https://c.example/x/1.jpg"
    evidence = [rec("p1", "tiktok", "a", "one", thumbnail_url=clip), rec("p2", "youtube", "b", "two", thumbnail_url=clip),
                rec("p3", "tiktok", "c", "three")]
    assert corroborated(con, evidence) is True


def test_two_unrelated_groups_on_one_platform_without_a_metric_do_not():
    con = world()
    measured(con, "p1", "p2", "p3")
    clip = "https://c.example/x/1.jpg"
    evidence = [rec("p1", "tiktok", "a", "one", thumbnail_url=clip), rec("p2", "tiktok", "b", "two", thumbnail_url=clip),
                rec("p3", "tiktok", "c", "three")]
    assert corroborated(con, evidence) is False
    assert corroborated(con, evidence, [NUMBER]) is False


def test_missing_signal_data_leaves_authors_unrelated():
    con = world()
    measured(con, "p1", "p2")
    evidence = [{"id": "p1", "platform": "tiktok", "handle": "a", "flags": []},
                {"id": "p2", "platform": "youtube", "handle": "b", "flags": []}]
    assert corroborated(con, evidence) is True
    evidence.append({"id": "p3", "platform": "youtube", "handle": None, "flags": [], "text": "x" * 40})
    assert corroborated(con, evidence) is True


def test_a_paid_marker_in_the_stored_hashtags_or_caption_makes_the_author_not_independent():
    con = world()
    measured(con, "p1", "p2")
    duck.load(con, "core.posts", [{"post_id": "p2", "post_date": D, "hashtags": ["ad", "dance"]}])
    evidence = [rec("p1", "tiktok", "a", "own words about the step"),
                rec("p2", "youtube", "b", "different words, no marker in the text")]
    assert corroborated(con, evidence) is False
    con = world()
    measured(con, "p1", "p2")
    evidence[1]["text"] = evidence[1]["quote_text"] = "Loving this step #sponsored by a sports brand"
    assert corroborated(con, evidence) is False


# Properties


def test_adding_a_link_never_raises_the_group_count():
    rng = random.Random(16)
    for _ in range(60):
        n = rng.randint(2, 7)
        records = [rec(f"p{i}", rng.choice(["tiktok", "twitter", "youtube"]), f"h{i}", f"unique words about topic {i} {rng.random()}",
                       thumbnail_url=f"https://c.example/{i}/cover.jpg", url=f"https://s.example/{i}")
                   for i in range(n)]
        before = len(gatectx.independent_groups(records))
        i, j = rng.sample(range(n), 2)
        records[j]["thumbnail_url"] = records[i]["thumbnail_url"]
        after = len(gatectx.independent_groups(records))
        assert after <= before
        records[j]["text"] = records[i]["text"]
        assert len(gatectx.independent_groups(records)) <= after


def test_every_handle_is_in_exactly_one_group():
    records = [rec("p1", "tiktok", "a", CAPTION), rec("p2", "twitter", "b", CAPTION), rec("p3", "twitter", "c", "other words entirely different")]
    groups = gatectx.independent_groups(records)
    handles = sorted(h for g in groups for h in g["handles"])
    assert handles == ["a", "b", "c"]
    assert sorted(len(g["handles"]) for g in groups) == [1, 2]
    assert {tuple(sorted(g["platforms"])) for g in groups} == {("tiktok", "twitter"), ("twitter",)}


@pytest.mark.parametrize("flag", ["brand", "brand_owned", "paid", "sponsored", "generated", "near_duplicate", "flagged"])
def test_a_not_independent_flag_removes_the_author_before_grouping(flag):
    records = [rec("p1", "tiktok", "a", "own words"), rec("p2", "youtube", "b", "more words", flags=[flag])]
    assert [g["handles"] for g in gatectx.independent_groups(records)] == [{"a"}]
