"""Write-time verification of one retained locality_v2 key (C4 v3 section 8.3, ruling finding F1).

verify() must recount every count the row carries from the member rows, not only the five class counts: the brief's
v2 ordering reads breadth_creators from the row, so a damaged derived count has to be refused at write time."""

import pytest

from core.trust.locality import (DERIVED_COUNTS, MIN_CONFIDENCE, VALID_SOURCES, counts_from_members, digest,
                                 member_class, read_locality, verify)

MARKET = "NG"
GEO = {"local": (MARKET, 0.9, "ext_region"), "foreign": ("AE", 0.9, "ext_region"), "unknown": (None, None, None)}


def member(n, cls, creator="auto", feed=False):
    """A member row as MEMBERS_SQL returns it: the geo fields that decide the class travel with it."""
    geo_market, geo_confidence, geo_source = GEO[cls]
    return {"post_id": f"p{n:02d}", "locality_class": cls, "feed_sighted": feed,
            "creator_key": f"t:{n}" if creator == "auto" else creator,
            "geo_market": geo_market, "geo_confidence": geo_confidence, "geo_source": geo_source}


def honest_row(members):
    counts = counts_from_members(members)
    known, local = counts["known_posts"], counts["local_posts"]
    status = "market_unconfirmed" if known < 8 else ("local" if 5 * local >= 3 * known else "not_local")
    return {"metric_version": "locality_v2.1", "market": MARKET, **counts, "status": status,
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


@pytest.mark.parametrize(("geo_market", "geo_confidence", "geo_source", "expected"), [
    ("NG", 0.9, "ext_region", "local"),
    (" ng ", 0.8, "home_market", "local"),                   # trimmed and upper-cased, as the SQL does
    ("NG", MIN_CONFIDENCE, "place_mention", "local"),        # exactly the floor
    ("AE", 0.9, "ext_region", "foreign"),
    ("NG", 0.69, "ext_region", "unknown"),                   # below the floor
    ("NG", 1.0, "language", "unknown"),                      # a source that never counts
    ("NG", 0.9, None, "unknown"),
    ("", 0.9, "home_market", "unknown"),                     # an empty location
    (None, 0.9, "home_market", "unknown"),
    ("NG", None, "ext_region", "unknown"),
    ("NG", "0.9", "ext_region", "unknown"),                  # not a number
])
def test_member_class_is_the_rule_of_the_sql_with_the_modules_own_constants(geo_market, geo_confidence, geo_source,
                                                                           expected):
    assert member_class({"geo_market": geo_market, "geo_confidence": geo_confidence, "geo_source": geo_source},
                        MARKET) == expected


def test_the_valid_sources_are_the_three_the_sql_names():
    assert set(VALID_SOURCES) == {"ext_region", "home_market", "place_mention"}


def faulty(members, n, **geo):
    out = [dict(m) for m in members]
    out[n].update(geo)
    return out


def test_a_class_that_the_members_own_geo_fields_do_not_give_is_refused_though_every_count_and_the_digest_agree():
    """A fault in the class rule writes a class, counts and a digest that agree with one another. Only recomputing the
    class from the geo fields of the same member rows catches it (ruling finding F10)."""
    for n, geo in ((0, {"geo_confidence": 0.5}),                 # stored local, confidence under the floor
                   (0, {"geo_source": "language"}),              # stored local, a source that never counts
                   (0, {"geo_market": "ZA"}),                    # stored local, located elsewhere
                   (3, {"geo_market": MARKET}),                  # stored foreign, located here
                   (5, {"geo_market": MARKET, "geo_confidence": 0.9, "geo_source": "ext_region"})):   # stored unknown
        members = faulty(MEMBERS, n, **geo)
        row = honest_row(members)                                # counts and digest follow the stored classes
        assert counts_from_members(members) == {k: row[k] for k in counts_from_members(members)}
        assert not verify(row, members), (n, geo)


def test_a_member_without_its_geo_fields_cannot_be_verified():
    members = [{k: v for k, v in m.items() if not k.startswith("geo_")} for m in MEMBERS]
    assert not verify(honest_row(MEMBERS), members)


def test_a_summary_without_a_market_cannot_be_verified():
    row = honest_row(MEMBERS)
    del row["market"]
    assert not verify(row, MEMBERS)
