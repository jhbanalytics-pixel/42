"""Write-time verification of one retained locality_v2 key (C4 v3 section 8.3, ruling finding F1).

verify() must recount every count the row carries from the member rows, not only the five class counts: the brief's
v2 ordering reads breadth_creators from the row, so a damaged derived count has to be refused at write time."""

import pytest

from core.trust.locality import DERIVED_COUNTS, counts_from_members, digest, read_locality, verify


def member(n, cls, creator="auto", feed=False):
    return {"post_id": f"p{n:02d}", "locality_class": cls, "feed_sighted": feed,
            "creator_key": f"t:{n}" if creator == "auto" else creator}


def honest_row(members):
    counts = counts_from_members(members)
    known, local = counts["known_posts"], counts["local_posts"]
    status = "market_unconfirmed" if known < 8 else ("local" if 5 * local >= 3 * known else "not_local")
    return {"metric_version": "locality_v2.1", **counts, "status": status,
            "local_share": local / known if known else None, "population_digest": digest(members)}


# 8 members, 5 distinct creators in all (two creators post twice, one post has no creator key): local, foreign,
# unknown and feed-only classes are all present, with a foreign feed post for the veto count.
MEMBERS = [
    member(0, "local", "t:a"), member(1, "local", "t:a"), member(2, "local", "t:b", feed=True),
    member(3, "foreign", "t:c", feed=True), member(4, "foreign", "t:c"), member(5, "unknown", "t:d", feed=True),
    member(6, "unknown", None, feed=True), member(7, "local", "t:e"),
]


def test_counts_from_members_recount_every_count_the_row_carries():
    got = counts_from_members(MEMBERS)
    assert got == {
        "population_posts": 8, "known_posts": 6, "local_posts": 4, "foreign_posts": 2, "unknown_posts": 2,
        "feed_only_posts": 2, "vetoed_feed_posts": 1,
        "local_creators": 3, "known_creators": 4, "feed_only_creators": 2,
        "breadth_creators": 5}                                   # a, b, e local; d and the post with no creator key feed only


def test_a_creator_with_no_key_counts_as_the_post_itself():
    two = [member(0, "local", None), member(1, "local", None)]
    assert counts_from_members(two)["local_creators"] == 2


def test_an_honest_row_is_accepted():
    assert verify(honest_row(MEMBERS), MEMBERS)


@pytest.mark.parametrize("name", DERIVED_COUNTS)
def test_a_row_with_the_right_class_counts_and_digest_and_a_wrong_derived_count_is_refused(name):
    row = honest_row(MEMBERS)
    assert verify(row, MEMBERS)
    row[name] = 99
    assert not verify(row, MEMBERS), name


def test_the_reader_alone_accepts_the_damaged_breadth_which_is_why_verify_must_not():
    row = honest_row(MEMBERS)
    row["breadth_creators"] = 99
    assert read_locality(row).status != "unreadable"            # the reader never sees the members
    assert not verify(row, MEMBERS)


def test_all_derived_counts_wrong_at_once_is_refused_the_ruling_probe():
    row = honest_row(MEMBERS)
    for name in DERIVED_COUNTS:
        row[name] = 99
    assert read_locality(row).status != "unreadable"
    assert not verify(row, MEMBERS)


def test_a_summary_missing_a_derived_count_is_refused_not_an_error():
    row = honest_row(MEMBERS)
    del row["breadth_creators"]
    assert not verify(row, MEMBERS)


def test_class_counts_digest_and_status_are_still_checked():
    row = honest_row(MEMBERS)
    assert not verify(dict(row, local_posts=5, foreign_posts=1), MEMBERS)
    assert not verify(dict(row, population_digest="0" * 64), MEMBERS)
    assert not verify(row, MEMBERS[:-1])
