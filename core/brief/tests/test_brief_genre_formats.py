"""A genre is not an adopted format (23 Oct 2026, genre-formats-skipped).

On 23 Oct NG "funny, joke" and a serialized drama ranked in pattern bucket 0 on a format 4 local creators used on 2 to 4
days, and the critic rightly cut both: a skit under a comedy tag is what the tag's genre looks like. A skit, tutorial,
slideshow, image meme, duet or stitch now counts toward no adopted format in the candidate statement's pattern bucket,
the shared-pattern line the writer and the critic read, or the count behind an inferred why-now, all read from one
list (core/brief/pattern_formats.py). A dance still counts. Rank and adoption count only: no gate, check or threshold
changes. Fakes and the DuckDB harness only.
"""

import json
from datetime import timedelta

import pytest

from core.brief import evidence, explain, job, pattern_formats
from core.brief.tests.test_brief_critic_patterns import post, shared
from core.brief.tests.test_brief_explain import FakeModel, good, run
from core.brief.tests.test_brief_why_now_inferred import NO_CAUSE, adopted_pack
from core.brief.tests.test_candidates_locality import D, RUN, STUBS, T
from core.brief.tests.test_candidates_pattern import item, order, posts
from core.detect.tests import duck
from core.understand import enrich, video

GENRES = ("skit", "tutorial", "slideshow", "meme_image", "duet", "stitch")


def test_the_one_list_holds_the_generic_formats_and_the_genres_and_leaves_dance_counted():
    assert pattern_formats.PATTERN_SKIP_FORMATS == ("talking_head", "other", "news_clip") + GENRES
    assert explain.PATTERN_SKIP_FORMATS is pattern_formats.PATTERN_SKIP_FORMATS
    # Every name is one the enrichment and video models can give, so a misspelt name never silently counts.
    assert set(pattern_formats.PATTERN_SKIP_FORMATS) <= set(enrich.FORMATS) == set(video.FORMATS)
    assert set(enrich.FORMATS) == set(evidence.VIDEO_FORMATS)
    assert set(enrich.FORMATS) - set(pattern_formats.PATTERN_SKIP_FORMATS) == {"dance"}


def test_the_candidate_statement_reads_the_list_from_its_one_place():
    sql = job.QUERIES["candidates"]
    assert pattern_formats.SQL_PLACEHOLDER not in sql
    assert sql.count("WHERE post_format NOT IN ('talking_head', 'other', 'news_clip', 'skit', 'tutorial', "
                     "'slideshow', 'meme_image', 'duet', 'stitch')") == 1
    raw = job.SQL.read_text(encoding="utf-8")
    assert raw.count(pattern_formats.SQL_PLACEHOLDER) == 1 and "'news_clip', 'talking_head'" not in raw


def test_the_funnel_scoreboard_ranks_with_the_same_list():
    pytest.importorskip("google.cloud.bigquery")
    from ops.runners import funnel_scoreboard

    sql = funnel_scoreboard.rank_sql()
    assert pattern_formats.SQL_PLACEHOLDER not in sql
    assert f"NOT IN ({pattern_formats.sql_list()})" in sql


@pytest.fixture
def con():
    c = duck.connect()
    c.execute(STUBS)
    duck.load(c, "agent.runs", [{"run_id": RUN, "stage": "detect", "run_date": D, "status": "ok", "started_at": T,
                                 "finished_at": T + timedelta(minutes=20)}])
    item(c, "dance", .10)
    posts(c, "dance", [("d1", "a", 0, None, ["dance"]), ("d2", "b", 1, None, ["dance"]),
                       ("d3", "c", 1, None, ["dance"])])
    for n, genre in enumerate(GENRES):
        name = f"g_{genre}"
        item(c, name, .50 - n / 100)
        posts(c, name, [(f"{name}1", "a", 0, None, [genre]), (f"{name}2", "b", 1, None, [genre]),
                        (f"{name}3", "c", 2, None, [genre])])
    # The video model's format is read the same way: a skit it watched counts no more than an enriched one.
    item(c, "v_skit", .30)
    posts(c, "v_skit", [("v1", "a", 0, None, None, "skit"), ("v2", "b", 1, None, None, "skit"),
                        ("v3", "c", 1, None, None, "skit")])
    return c


def test_a_genre_used_by_three_local_creators_on_two_days_is_not_adopted_and_a_dance_still_is(con):
    ids, rows = order(con)
    by = {r["item_id"]: r for r in rows}
    assert by["dance"]["_selection_pattern"] == 0 and ids[0] == "dance"
    for name in [f"g_{g}" for g in GENRES] + ["v_skit"]:
        assert by[name]["_selection_pattern"] == 1, name
        # Rank only: the genre item stays in the pool, and the counts it shows leave the genre out.
        assert by[name]["_selection_format_creators"] is None
    # The genre items keep their worth order among themselves, behind the adopted dance.
    assert ids == ["dance", "g_skit", "g_tutorial", "g_slideshow", "g_meme_image", "g_duet", "g_stitch", "v_skit"]


@pytest.mark.parametrize("genre", GENRES)
def test_the_pattern_line_the_writer_and_the_critic_read_leaves_a_genre_out(genre):
    pack = {"evidence": [post("p1", "ada", 10, formats=[genre]), post("p2", "bayo", 11, formats=[genre, "dance"]),
                         post("p3", "chi", 12, video_format=genre), post("p4", "dayo", 12, formats=["dance"])]}
    got = shared(pack)
    assert f"format {genre}" not in got
    assert got["format dance"]["post_ids"] == ["p2", "p4"]
    line = explain._writer_pattern_line(pack["evidence"], explain._sound_letters(pack))
    assert f"format {genre}" not in line and "format dance" in line
    assert ("(talking_head, other, news_clip, skit, tutorial, slideshow, meme_image, duet and stitch left out)"
            in line)


@pytest.mark.parametrize("genre", GENRES)
def test_a_genre_never_carries_an_inferred_why_now(genre):
    pack = adopted_pack()
    for r in pack["evidence"]:
        r["formats"] = [genre]
    assert explain.adopted_pattern(pack, "ZA") is None
    result = run(FakeModel([good()], critic=NO_CAUSE), pack=pack)
    # Ruled out with no post-stated cause and no counted adoption: held as before 22 Oct, never inferred.
    assert result["reason"] == "failed_checks" and result["why_now_inferred"] is False
    # The same posts as a dance are counted, as on 22 Oct.
    assert explain.adopted_pattern(adopted_pack(), "ZA")["pattern"] == "format dance"


def test_a_genre_as_the_video_models_format_never_carries_an_inferred_why_now():
    pack = adopted_pack()
    for r in pack["evidence"]:
        r["formats"] = []
        r["video_format"] = "skit"
    assert explain.adopted_pattern(pack, "ZA") is None


def test_the_writer_and_the_critic_are_told_the_whole_list():
    words = "talking_head, other, news_clip, skit, tutorial, slideshow, meme_image, duet or stitch"
    assert f"{words} alone does not" in " ".join(explain.WRITER_SYSTEM.split())
    critic = " ".join(explain.CRITIC_SYSTEM.split())
    assert f"a generic format or a genre, {words}, alone shows no adopted practice" in critic
    assert ("each format, talking_head, other, news_clip, skit, tutorial, slideshow, meme_image, duet and stitch left "
            "out, that 2 or more different creators share") in critic
    # The support check judges whether a claim is true of its posts, not adoption, so its wording is unchanged.
    assert "skit, tutorial, slideshow, meme_image, duet or stitch" not in explain.SUPPORT_SYSTEM


def test_the_critic_records_the_line_it_read_without_the_genre():
    pack = adopted_pack()
    for r in pack["evidence"]:
        r["formats"] = ["skit", "dance"]
    seen = explain._critic_patterns(pack, "ZA")
    assert [p["pattern"] for p in seen] == ["format dance"]
    assert json.dumps(seen).count("skit") == 0
