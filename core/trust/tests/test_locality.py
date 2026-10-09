import pytest

from core.trust.locality import METRIC_VERSION, locality_block, read_locality


# The six derived counts of a row whose members are five local and three foreign posts by distinct creators (and,
# in the first verify test, two unknown ones): verify() recounts them too (ruling condition C1).
DERIVED = {"feed_only_posts": 0, "vetoed_feed_posts": 0, "local_creators": 5, "known_creators": 8,
           "feed_only_creators": 0, "breadth_creators": 5}


def row(known, local, unknown=0, **over):
    foreign = known - local
    base = {"metric_version": METRIC_VERSION, "population_posts": known + unknown, "known_posts": known,
            "local_posts": local, "foreign_posts": foreign, "unknown_posts": unknown,
            "status": "market_unconfirmed" if known < 8 else ("local" if 5 * local >= 3 * known else "not_local"),
            "local_share": local / known if known else None}
    base.update(over)
    return base


@pytest.mark.parametrize(("r", "status"), [
    (row(0, 0), "market_unconfirmed"),                  # valid zero
    (row(0, 0, unknown=12), "market_unconfirmed"),
    (row(7, 7), "market_unconfirmed"),                  # 7 known, all local
    (row(7, 0), "market_unconfirmed"),                  # 7 known, none local
    (row(8, 4), "not_local"),
    (row(8, 5), "local"),
    (row(10, 6), "local"),                              # exactly 0.6
    (row(10, 5), "not_local"),
    (row(15, 9), "local"),                              # exactly 0.6 again
    (row(8, 0), "not_local"),                           # a valid zero local count is a real zero
])
def test_truth_table(r, status):
    got = read_locality(r)
    assert got.status == status and got.reason is None


@pytest.mark.parametrize(("r", "reason"), [
    (None, "missing"),
    ("not a row", "missing"),
    (row(8, 5, metric_version="locality_v2.0"), "version"),
    (row(8, 5, metric_version=None), "version"),
    (row(8, 5, known_posts=None), "null_count"),        # null
    (row(8, 5, local_posts=None), "null_count"),
    (row(8, 5, known_posts=8.0), "count_type"),         # invalid: float
    (row(8, 5, known_posts="8"), "count_type"),
    (row(8, 5, known_posts=True), "count_type"),
    (row(8, 5, local_posts=-1, foreign_posts=9), "negative_count"),
    (row(8, 5, foreign_posts=4), "inconsistent_counts"),
    (row(8, 5, unknown_posts=3), "inconsistent_counts"),
    (row(8, 4, status="local"), "status_mismatch"),     # the row's own claim never decides
    (row(8, 5, status=None), "status_mismatch"),
    (row(8, 5, local_share=0.1), "share_mismatch"),
    (row(0, 0, local_share=0.0), "share_mismatch"),
    (row(8, 5, local_share=float("nan")), "share_mismatch"),   # a stored NaN must not slip past the comparison
    (row(8, 5, local_share=float("inf")), "share_mismatch"),
    (row(8, 5, local_share="0.625"), "share_mismatch"),
    (row(8, 5, local_share=None), "share_mismatch"),
])
def test_invalid_and_unreadable_rows_are_never_zero(r, reason):
    got = read_locality(r)
    assert got.status == "unreadable" and got.reason == reason


def test_constants_echoed_in_the_row_are_ignored():
    r = row(8, 4, min_known=1, share_num=1, share_den=100)
    assert read_locality(r).status == "not_local"


def test_expected_version_is_pinned_by_the_caller_not_read_from_the_row():
    assert read_locality(row(8, 5), expected_version="locality_v2.2").reason == "version"


def test_digest_ignores_member_order_changes_with_a_class_and_hashes_the_empty_set_to_the_empty_string_hash():
    from core.trust.locality import digest
    a = {"post_id": "p1", "locality_class": "local", "creator_key": "tiktok:a", "feed_sighted": False}
    b = {"post_id": "p2", "locality_class": "unknown", "creator_key": None, "feed_sighted": True}
    assert digest([a, b]) == digest([b, a])
    assert digest([a, b]) != digest([a, {**b, "locality_class": "local"}])
    assert digest([]) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_row_from_prefixed_strips_only_the_prefix_and_keeps_local_share():
    from core.trust.locality import row_from_prefixed
    got = row_from_prefixed({"lrow_local_share": 0.5, "lrow_status": "local", "locality_status": "not_local", "item_id": "x"})
    assert got == {"local_share": 0.5, "status": "local"}                 # item_state.locality_status is not the row's status


GEO = {"local": ("NG", 0.9, "ext_region"), "foreign": ("AE", 0.9, "ext_region"), "unknown": (None, None, None)}


def geo(cls):
    """The geo fields of a member row that give it the class cls in market NG (verify recomputes the class from them)."""
    return dict(zip(("geo_market", "geo_confidence", "geo_source"), GEO[cls]))


def test_verify_accepts_a_key_whose_members_reproduce_its_row_and_refuses_a_row_that_only_agrees_with_itself():
    from core.trust.locality import digest, verify
    members = [{"post_id": f"p{n}", "locality_class": c, "creator_key": f"t:{n}", "feed_sighted": False, **geo(c)}
               for n, c in enumerate(["local"] * 5 + ["foreign"] * 3 + ["unknown"] * 2)]
    good = row(8, 5, unknown=2, population_digest=digest(members), market="NG", **DERIVED)
    assert verify(good, members)
    moved = dict(good, local_posts=6, foreign_posts=2, local_share=6 / 8, status="local")
    assert read_locality(moved).status == "local"                  # consistent with itself, so the reader accepts it
    assert not verify(moved, members)                              # but not with its members
    assert not verify(good, members[:-1])
    changed = [dict(m) for m in members]
    changed[0]["locality_class"] = "unknown"
    assert not verify(dict(good, population_digest=digest(members)), changed)


def test_verify_refuses_a_summary_whose_digest_belongs_to_other_members_with_the_same_class_counts():
    from core.trust.locality import digest, verify
    def make(prefix):
        return [{"post_id": f"{prefix}{n}", "locality_class": c, "creator_key": f"t:{n}", "feed_sighted": False,
                 **geo(c)} for n, c in enumerate(["local"] * 5 + ["foreign"] * 3)]
    mine, other = make("a"), make("b")
    summary = row(8, 5, population_digest=digest(mine), market="NG", **DERIVED)
    assert verify(summary, mine)
    assert not verify(summary, other)                              # identical counts, different posts: only the digest differs


COUNT_KEYS = ("known_posts", "local_posts", "local_share", "population_posts", "unknown_posts", "foreign_posts",
              "feed_only_posts", "vetoed_feed_posts", "breadth_creators")
STORED = {"feed_only_posts": 1, "vetoed_feed_posts": 2, "breadth_creators": 4, "schema_version": 1}


@pytest.mark.parametrize("record", [
    row(8, 0, status="local"),                               # the stored status contradicts the counts
    row(8, 5, local_share=0.1),                              # the share contradicts them
    row(8, 5, metric_version="locality_v2.0"),               # another version
    row(8, 5, unknown_posts=None),                           # a null count
    None,                                                    # no row at all
])
def test_an_unreadable_row_shows_no_counts_not_the_ones_it_stores(record):
    """Whatever a row stores, an unreadable one gives the consumer no count to quote: None for every one, never zero."""
    block = locality_block(None if record is None else {**record, **STORED})
    assert block["status"] == "unreadable" and block["label"] is None
    assert {k: block[k] for k in COUNT_KEYS} == dict.fromkeys(COUNT_KEYS)


def test_a_readable_row_shows_its_counts():
    block = locality_block({**row(10, 8, unknown=2), **STORED})
    assert (block["status"], block["known_posts"], block["local_posts"], block["local_share"]) == ("local", 10, 8, 0.8)
    assert (block["population_posts"], block["unknown_posts"], block["foreign_posts"]) == (12, 2, 2)
    assert (block["feed_only_posts"], block["vetoed_feed_posts"], block["breadth_creators"]) == (1, 2, 4)
