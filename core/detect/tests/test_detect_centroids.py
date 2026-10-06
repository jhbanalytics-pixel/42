"""Known answers for hashtag and sound centroids (BUILD.md 3.3): the mean of an item's post embeddings over the
window, written to cultural_map as a new version, run on DuckDB fixture tables through duck.Client."""

import pytest

from .. import centroids
from . import duck
from .fixtures import D, at, cmap, day, obs

WINDOW = centroids.WINDOW_DAYS
MIN = centroids.MIN_POSTS


@pytest.fixture
def con():
    c = duck.connect()
    yield c
    c.close()


def item(con, iid, kind="hashtag", status="active", centroid=None, **extra):
    row = cmap(iid, kind)
    row.update(status=status, centroid=centroid, label="#" + iid, first_seen=day(60), first_seen_market="ZA",
               first_seen_platform="tiktok", last_seen=D, recurrences=0, **extra)
    duck.load(con, "core.cultural_map", [row])


def posts(con, iid, vectors, seen=D, prefix=None):
    """One post per vector, linked to iid, sighted on seen, with one embed row and one enrich row (no vector)."""
    prefix = prefix or iid
    for n, v in enumerate(vectors):
        pid = f"{prefix}-{n}"
        duck.load(con, "core.post_observations", [obs(pid, seen, "unbiased_rank", "sweep")])
        duck.load(con, "core.post_items", [{"post_id": pid, "item_id": iid, "via": "hashtag"}])
        duck.load(con, "core.post_enrichment", [{"post_id": pid, "embedding": v}, {"post_id": pid, "embedding": []}])


def run(con, d=D):
    return centroids.run_item_centroids(duck.Client(con), d, core="core", agent="agent")


def rows(con, iid):
    return duck.query(con, "SELECT * FROM {core}.cultural_map c WHERE c.item_id = @i ORDER BY c.valid_from",
                      {"i": iid})


def test_a_hashtag_with_enough_posts_gets_the_mean_of_their_embeddings_as_a_new_version(con):
    item(con, "amapiano", aliases=["amapiano"])
    posts(con, "amapiano", [[1.0, 0.0, 0.0]] * 3 + [[0.0, 1.0, 0.0]] * (MIN - 3))
    counts = run(con)
    assert counts == {"centroids": 1}
    old, new = rows(con, "amapiano")
    assert old["centroid"] is None and old["valid_to"] is not None
    assert new["valid_to"] is None and new["valid_from"] == old["valid_to"]
    assert new["centroid"] == pytest.approx([3 / MIN, (MIN - 3) / MIN, 0.0])
    for col in ("kind", "canonical_key", "label", "aliases", "first_seen", "first_seen_market",
                "first_seen_platform", "last_seen", "recurrences", "status"):
        assert new[col] == old[col]


def test_a_sound_gets_a_centroid_too(con):
    item(con, "snd", kind="sound")
    posts(con, "snd", [[0.0, 0.0, 2.0]] * MIN)
    run(con)
    assert rows(con, "snd")[-1]["centroid"] == pytest.approx([0.0, 0.0, 2.0])


def test_too_few_posts_topics_generic_tags_and_creators_are_left_alone(con):
    item(con, "few")
    posts(con, "few", [[1.0, 0.0, 0.0]] * (MIN - 1))
    item(con, "topic1", kind="topic", centroid=[0.5, 0.5, 0.0])
    posts(con, "topic1", [[1.0, 0.0, 0.0]] * MIN)
    item(con, "fyp", status="generic")
    posts(con, "fyp", [[1.0, 0.0, 0.0]] * MIN)
    item(con, "cr", kind="creator")
    posts(con, "cr", [[1.0, 0.0, 0.0]] * MIN)
    assert run(con) == {"centroids": 0}
    assert len(rows(con, "few")) == len(rows(con, "fyp")) == len(rows(con, "cr")) == 1
    (topic,) = rows(con, "topic1")
    assert topic["centroid"] == pytest.approx([0.5, 0.5, 0.0]) and topic["valid_to"] is None


def test_only_posts_sighted_inside_the_window_count_and_one_must_be_sighted_on_the_day(con):
    item(con, "win")
    posts(con, "win", [[1.0, 0.0, 0.0]] * MIN, seen=day(WINDOW - 1), prefix="in")
    posts(con, "win", [[0.0, 1.0, 0.0]] * MIN, seen=day(WINDOW), prefix="out")
    assert run(con) == {"centroids": 0}     # no post of it sighted on D: nothing new to fold in
    posts(con, "win", [[1.0, 0.0, 0.0]], seen=D, prefix="today")
    assert run(con) == {"centroids": 1}
    assert rows(con, "win")[-1]["centroid"] == pytest.approx([1.0, 0.0, 0.0])


def test_a_post_with_two_vector_rows_counts_once(con):
    item(con, "dup")
    posts(con, "dup", [[1.0, 0.0, 0.0]] * MIN)
    duck.load(con, "core.post_enrichment", [{"post_id": "dup-0", "embedding": [0.9, 0.0, 0.0]}])
    run(con)
    expected = (0.9 + (MIN - 1)) / MIN
    assert rows(con, "dup")[-1]["centroid"] == pytest.approx([expected, 0.0, 0.0])


def test_a_rerun_with_the_same_posts_writes_nothing_and_new_posts_write_one_more_version(con):
    item(con, "re")
    posts(con, "re", [[1.0, 0.0, 0.0]] * MIN)
    assert run(con) == {"centroids": 1}
    assert run(con) == {"centroids": 0}
    assert len(rows(con, "re")) == 2
    posts(con, "re", [[0.0, 1.0, 0.0]] * MIN, prefix="more")
    assert run(con) == {"centroids": 1}
    versions = rows(con, "re")
    assert len(versions) == 3 and [v["valid_to"] is None for v in versions] == [False, False, True]
    assert versions[-1]["centroid"] == pytest.approx([0.5, 0.5, 0.0])


def test_closed_versions_are_never_touched(con):
    item(con, "old", valid_to=at(day(10)), centroid=[0.0, 0.0, 1.0])
    item(con, "old", valid_from=at(day(10)))
    posts(con, "old", [[1.0, 0.0, 0.0]] * MIN)
    run(con)
    versions = rows(con, "old")
    assert len(versions) == 3
    assert versions[0]["valid_to"] == at(day(10)) and versions[0]["centroid"] == pytest.approx([0.0, 0.0, 1.0])
    assert sum(v["valid_to"] is None for v in versions) == 1


def test_the_centroid_sql_only_merges():
    sql = centroids.ITEM_CENTROIDS_SQL.upper()
    assert "MERGE" in sql
    for word in ("DELETE", "DROP", "TRUNCATE", "REPLACE"):
        assert word not in sql
