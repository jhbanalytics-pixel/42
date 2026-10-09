"""Member-first evidence pack (C4 v2 section 19, appendix G) on the DuckDB harness.

The checks are plain functions of the SQL text, so the same checks run on the real statement and, in the mutant
test at the bottom, on deliberately broken copies of it. A check that no mutant can fail is not evidence.
"""

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from core.api.store import creator_key
from core.brief import evidence, pack_order
from core.brief.specificity import local_posts, showable_posts
from core.detect.tests import duck

UTC = timezone.utc
D = date(2026, 10, 7)
ITEM, MARKET = "i1", "NG"
NG = timezone(timedelta(hours=1))
START, END = datetime(2026, 10, 1, tzinfo=NG), datetime(2026, 10, 8, tzinfo=NG)
KEYS = [f"x:hub{n}" for n in range(4)]  # the confirmed outlet registry, as platform:handle


def statement():
    return evidence.QUERIES["evidence"]


def post(con, pid, *, creator, eng, platform="tiktok", handle=None, member=False, cluster="ng", lane_class="unbiased_rank",
         geo="NG", creator_row=True):
    published = datetime(2026, 10, 6, 9, tzinfo=UTC)
    if creator_row and not con.execute("SELECT 1 FROM core.creators WHERE creator_id = ? AND platform = ?",
                                       [creator, platform]).fetchall():
        duck.load(con, "core.creators", [{"creator_id": creator, "platform": platform, "handle": handle or creator}])
    duck.load(con, "core.posts", [{"post_id": pid, "platform": platform, "creator_id": creator, "published_at": published,
                                    "post_date": published.date(), "engagement": eng, "geo_market": geo,
                                    "geo_confidence": 0.9 if geo else None, "geo_source": "ext_region" if geo else None}])
    duck.load(con, "core.post_items", [{"post_id": pid, "item_id": ITEM, "via": "hashtag"}])
    seen = published + timedelta(hours=1)
    duck.load(con, "core.post_observations", [{"post_id": pid, "observed_at": seen, "observed_date": seen.date(),
                                                "market": MARKET, "platform": platform, "lane": "sweep",
                                                "lane_class": lane_class}])
    if member:
        duck.load(con, "core.cluster_members", [{"cluster_id": f"{D:%Y%m%d}-{cluster}-001", "post_id": pid,
                                                  "probability": 0.9}])


def clusters(con):
    duck.load(con, "core.clusters", [
        {"cluster_date": D, "cluster_id": f"{D:%Y%m%d}-ng-001", "market": "ng", "item_id": ITEM, "match_kind": "match"},
        {"cluster_date": D, "cluster_id": f"{D:%Y%m%d}-pan-001", "market": "pan", "item_id": ITEM, "match_kind": "match"},
        {"cluster_date": D - timedelta(days=10), "cluster_id": "old-ng-001", "market": "ng", "item_id": ITEM,
         "match_kind": "match"}])


def world(reverse=False):
    con = duck.connect()
    clusters(con)
    specs = [(f"o_news{n}", dict(creator=f"news{n}", eng=9000 - n, platform="news", geo=None, lane_class="panel"))
             for n in range(4)]  # outlets on the news platform
    specs += [(f"o_x{n}", dict(creator=f"hub{n}", eng=8000 - n, platform="twitter", handle=f"@Hub{n}", geo=None,
                               lane_class="panel")) for n in range(4)]  # outlets on X, in the registry
    specs += [(f"m{n}", dict(creator=f"cm{n}", eng=100 + n, member=True)) for n in range(6)]
    specs += [(f"mc{n}", dict(creator="heavy", eng=500 + n, member=True)) for n in range(5)]  # the cap keeps two
    specs += [(f"pm{n}", dict(creator=f"pm{n}", eng=300 + n, member=True, cluster="pan")) for n in range(2)]
    specs += [(f"cx{n}", dict(creator=f"cx{n}", eng=200 + n)) for n in range(3)]
    specs += [("tie_b", dict(creator="tb", eng=5000)), ("tie_a", dict(creator="ta", eng=5000)),
              ("mx_c1", dict(creator="mix", eng=900)), ("mx_c2", dict(creator="mix", eng=800)),
              ("mx_m", dict(creator="mix", eng=10, member=True)),  # the cap must keep the member
              ("stale", dict(creator="st", eng=6000)),  # member only of a cluster ten days back
              ("nohandle", dict(creator="nc", eng=7000, creator_row=False))]  # a post whose creator has no creators row
    for pid, kw in (reversed(specs) if reverse else specs):
        post(con, pid, **kw)
    duck.load(con, "core.cluster_members", [{"cluster_id": "old-ng-001", "post_id": "stale", "probability": 0.9},
                                             {"cluster_id": f"{D:%Y%m%d}-ng-001", "post_id": "ghost", "probability": 0.8}])
    return con  # "ghost" is a member the producer assigned that no sighting links


def pack(con, cap, keys=KEYS, sql=None):
    return duck.query(con, sql or statement(), {"item_id": ITEM, "market": MARKET, "d": D, "start": START, "end": END,
                                                "outlet_keys": keys, "outlet_cap": cap})


MEMBERS = ["mc4", "mc3", "m5", "m4", "m3", "m2", "m1", "m0", "mx_m"]  # tier 0, measured then engagement


def check_members_come_first_then_the_rest_in_the_old_order(sql):
    rows = pack(world(), 12, sql=sql)
    assert [r["post_id"] for r in rows] == MEMBERS + ["o_news0", "o_news1", "o_news2"]
    assert all(r["market_member"] for r in rows[:9]) and not any(r["market_member"] for r in rows[9:])


def check_the_creator_cap_keeps_the_member_post_of_a_creator_whose_other_posts_rank_higher(sql):
    ids = [r["post_id"] for r in pack(world(), 12, sql=sql)]
    assert "mx_m" in ids and ids.count("mc4") == 1 and "mc2" not in ids  # 2 per creator, member first


def check_a_member_of_a_cluster_dated_outside_the_window_is_context(sql):
    rows = {r["post_id"]: r for r in pack(world(), 0, sql=sql)}
    assert "stale" in rows and not rows["stale"]["market_member"]


def check_the_outlet_cap_limits_classified_outlets_and_zero_removes_them(sql):
    assert sum(r["is_outlet"] for r in pack(world(), 3, sql=sql)) == 3
    zero = pack(world(), 0, sql=sql)
    assert [r["post_id"] for r in zero][9:] == ["nohandle", "stale", "tie_a"] and not any(r["is_outlet"] for r in zero)


def check_outlets_are_the_news_platform_plus_the_registry_by_normalised_platform_and_handle(sql):
    ids = lambda keys: [r["post_id"] for r in pack(world(), 0, keys=keys, sql=sql)][9:]  # after the 9 members
    assert ids(KEYS) == ["nohandle", "stale", "tie_a"]  # 4 news and 4 X hubs all removed
    assert ids([]) == ["o_x0", "o_x1", "o_x2"]  # the news platform only: the X hubs are not outlets


def check_a_post_with_no_creator_row_is_not_an_outlet_and_is_never_dropped_by_the_cap(sql):
    rows = pack(world(), 0, sql=sql)
    assert "nohandle" in [r["post_id"] for r in rows]
    assert next(r for r in rows if r["post_id"] == "nohandle")["is_outlet"] is False


def check_ties_resolve_by_post_id_whatever_order_the_rows_arrived_in(sql):
    a, b = pack(world(), 0, sql=sql), pack(world(reverse=True), 0, sql=sql)
    assert [r["post_id"] for r in a] == [r["post_id"] for r in b]
    assert [r["post_id"] for r in a][11:] == ["tie_a"] and "tie_b" not in [r["post_id"] for r in a]


def check_stage_counts_say_which_cap_removed_which_posts(sql):
    # With the cap at 0 all 8 outlets are removed, and none had 12 kept posts ahead of it (only the 9 members do), so
    # the pack's places were the cap's to give and every one of them counts as removed by it.
    r = pack(world(), 0, sql=sql)[0]
    assert (r["available_posts"], r["after_creator_cap"], r["after_outlet_cap"]) == (31, 27, 19)
    assert r["available_members"] == 12  # of 13 the producer assigned
    assert (r["available_local"], r["after_creator_cap_local"], r["after_outlet_cap_local"]) == (23, 19, 19)
    assert (r["available_showable"], r["after_creator_cap_showable"], r["after_outlet_cap_showable"]) == (31, 27, 19)
    # At 3 the five outlets it removes all rank behind 12 kept posts, so they were never going to be in the pack.
    assert pack(world(), 3, sql=sql)[0]["after_outlet_cap"] == 27


ORIGINAL_COLUMNS = {
    "comments", "creator_rank", "creator_tier", "duration_s", "eng", "flagged", "geo_confidence", "geo_market",
    "geo_source", "handle", "likes", "measured", "near_dup", "platform", "post_id", "published_at", "quote_text",
    "shares", "source_market", "sponsor_checked", "sponsored", "thumbnail_url", "url", "views"}


def check_P08_the_statement_returns_every_column_the_original_evidence_statement_returned(sql):
    rows = pack(world(), 12, sql=sql)
    assert ORIGINAL_COLUMNS <= set(rows[0]), ORIGINAL_COLUMNS - set(rows[0])


def outlet_pile_world():
    """15 outlets: the top 12 located abroad (not showable), 3 showable ones below them, no members."""
    con = duck.connect()
    clusters(con)
    for n in range(12):
        post(con, f"abroad{n:02d}", creator=f"a{n}", eng=9000 - n, platform="news", geo="ZA")
    for n in range(3):
        post(con, f"showable{n}", creator=f"s{n}", eng=100 - n, platform="news", geo=None)
    return con


def stage_columns(row):
    return {k: v for k, v in row.items() if k.startswith(("available_", "after_"))}


def check_a_cap_of_12_changes_neither_the_pack_nor_the_stage_counts(sql):
    capped, uncapped = pack(outlet_pile_world(), 12, sql=sql), pack(outlet_pile_world(), 10_000, sql=sql)
    assert [r["post_id"] for r in capped] == [r["post_id"] for r in uncapped]
    assert stage_columns(capped[0]) == stage_columns(uncapped[0])
    assert capped[0]["after_outlet_cap_showable"] == 3  # the three behind the first 12 were never in the pack


def member_outlets_world():
    con = duck.connect()
    clusters(con)
    post(con, "n_hi", creator="hi", eng=9000, platform="news", geo=None)
    post(con, "n_mem", creator="mem", eng=10, platform="news", geo=None, member=True)
    return con


def check_the_outlet_cap_keeps_the_member_outlet_over_a_higher_engagement_one(sql):
    assert [r["post_id"] for r in pack(member_outlets_world(), 1, sql=sql)] == ["n_mem"]


def run_flags_world():
    con = duck.connect()
    clusters(con)
    duck.load(con, "core.clusters", [{"cluster_date": D + timedelta(days=1), "cluster_id": "next-ng-001",
                                       "market": "ng", "item_id": ITEM, "match_kind": "match"}])
    post(con, "in_ng", creator="a", eng=4, member=True)
    post(con, "in_pan", creator="b", eng=3, member=True, cluster="pan")
    post(con, "in_future", creator="c", eng=2)
    duck.load(con, "core.cluster_members", [{"cluster_id": "next-ng-001", "post_id": "in_future", "probability": 0.9}])
    post(con, "in_none", creator="d", eng=1)
    return con


def check_membership_flags_follow_the_run_and_the_window(sql):
    rows = {r["post_id"]: r for r in pack(run_flags_world(), 12, sql=sql)}
    flags = {pid: (r["market_member"], r["pan_member"]) for pid, r in rows.items()}
    assert flags == {"in_ng": (True, False), "in_pan": (False, True), "in_future": (False, False),
                     "in_none": (False, False)}


def outlet_only_world():
    con = duck.connect()
    clusters(con)
    for n in range(3):
        post(con, f"o{n}", creator=f"news{n}", eng=100 + n, platform="news", geo=None)
    return con


def check_stage_counts_survive_a_pack_the_outlet_cap_empties(sql):
    rows = pack(outlet_only_world(), 0, sql=sql)
    assert len(rows) == 1 and rows[0]["post_id"] is None
    assert (rows[0]["available_posts"], rows[0]["after_creator_cap"], rows[0]["after_outlet_cap"]) == (3, 3, 0)


def abroad_outlets_world():
    """15 news outlets: the first 12 by engagement are located abroad (not showable here), the 3 below them are."""
    con = duck.connect()
    clusters(con)
    for n in range(15):
        post(con, f"o{n:02d}", creator=f"news{n}", eng=9000 - n, platform="news", geo="KE" if n < 12 else None,
             lane_class="panel")
    return con


def check_the_outlet_stage_counts_a_capped_outlet_only_when_12_posts_rank_ahead_of_it(sql):
    # A cap of 12 removes the 3 showable outlets from behind the 12th place, so they still count at the outlet stage
    # and only the pack size loses them. A cap of 3 or 0 removes them from inside the first 12 places: that is the cap.
    def stage(cap, column):
        rows = pack(abroad_outlets_world(), cap, sql=sql)
        assert rows, f"no row at cap {cap}"
        return rows[0][column] or 0  # a COUNTIF over no rows reads null in the harness, 0 in BigQuery

    assert {cap: stage(cap, "after_outlet_cap_showable") for cap in (12, 11, 3, 0)} == {12: 3, 11: 0, 3: 0, 0: 0}
    assert {cap: stage(cap, "after_outlet_cap") for cap in (12, 11, 3, 0)} == {12: 15, 11: 11, 3: 3, 0: 0}


def test_the_sql_pack_size_is_the_one_pack_order_names():
    sql = statement()
    assert re.findall(r"LIMIT (\d+)", sql) == [str(pack_order.PACK_LIMIT)]
    assert re.findall(r"kept_ahead, 0\) >= (\d+)", sql) == [str(pack_order.PACK_LIMIT)]


def test_the_default_outlet_cap_is_12_and_changes_nothing():
    assert pack_order.OUTLET_CAP == 12
    capped, uncapped = pack(world(), pack_order.OUTLET_CAP), pack(world(), 10_000)
    assert [r["post_id"] for r in capped] == [r["post_id"] for r in uncapped]
    assert capped[0] == uncapped[0]


def sql_without_comments(sql):
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def order_by_clauses(sql):
    out, at = [], 0
    while (at := sql.find("ORDER BY", at)) != -1:
        depth, end = 0, at + 8
        while end < len(sql) and not (sql[end] == ")" and depth == 0) and not sql.startswith("LIMIT", end):
            depth += {"(": 1, ")": -1}.get(sql[end], 0)
            end += 1
        clause = re.sub(r"\s+ROWS\s+BETWEEN\s.*$", "", sql[at + 8:end].strip().rstrip(";").strip(), flags=re.DOTALL)
        out.append(clause)
        at = end
    return out


def check_every_order_by_ends_in_the_post_id_so_no_tie_is_left_to_the_engine(sql):
    clauses = order_by_clauses(sql_without_comments(sql))
    assert len(clauses) == 5 and all(c.endswith("post_id") for c in clauses), clauses


CHECKS = {name[len("check_"):]: fn for name, fn in sorted(globals().items()) if name.startswith("check_")}


@pytest.mark.parametrize("name", CHECKS)
def test_the_statement_passes(name):
    CHECKS[name](statement())


@pytest.mark.parametrize("handle", ["Hub", "@Hub", "@@hub", "u/Hub", "@u/hub", " hub ", "HUB"])
def test_the_registry_matches_a_handle_exactly_as_the_application_normalises_it(handle):
    con = duck.connect()
    clusters(con)
    post(con, "p", creator="c", eng=1, platform="twitter", handle=handle, geo=None)
    key = creator_key("x", handle)
    assert key == "x:hub"
    assert pack(con, 12, keys=[key])[0]["is_outlet"] is True
    assert pack(con, 12, keys=["x:other"])[0]["is_outlet"] is False


def test_an_uppercase_u_prefix_is_not_stripped_by_the_application_so_the_sql_must_not_strip_it_either():
    con = duck.connect()
    clusters(con)
    post(con, "p", creator="c", eng=1, platform="twitter", handle="U/Hub", geo=None)
    assert creator_key("x", "U/Hub") == "x:u/hub"
    assert pack(con, 12, keys=["x:u/hub"])[0]["is_outlet"] is True
    assert pack(con, 12, keys=["x:hub"])[0]["is_outlet"] is False


def test_a_registry_key_of_another_platform_does_not_classify_the_handle():
    con = duck.connect()
    clusters(con)
    post(con, "p", creator="c", eng=1, platform="instagram", handle="hub", geo=None)
    assert pack(con, 12, keys=["x:hub"])[0]["is_outlet"] is False
    assert pack(con, 12, keys=["instagram:hub"])[0]["is_outlet"] is True


def parity_world():
    con = duck.connect()
    clusters(con)
    seen = datetime(2026, 10, 6, 10, tzinfo=UTC)
    sighting = {"source_market": MARKET, "source_region": None, "route": "tiktok/trending",
                "protocol": "tiktok/trending?feed=local", "observed_at": seen, "obs_date": seen.date()}
    post(con, "loc_ng", creator="a", eng=60, geo="NG")
    post(con, "loc_za", creator="b", eng=50, geo="ZA")
    post(con, "feed_unknown", creator="c", eng=40, geo=None)
    duck.load(con, "core.source_market_fixture", [{"post_id": "feed_unknown", "source_markets": [MARKET],
                                                    "source_sightings": [sighting]}])
    post(con, "unsighted_unknown", creator="d", eng=30, geo=None)
    post(con, "lowconf_za", creator="e", eng=20, geo=None)
    con.execute("UPDATE core.posts SET geo_market = 'ZA', geo_confidence = 0.5, geo_source = 'ext_region' "
                "WHERE post_id = 'lowconf_za'")
    return con


def test_the_sql_local_and_showable_flags_agree_with_the_rules_the_floors_are_checked_by():
    tz = timezone(timedelta(hours=1))
    rows = pack(parity_world(), 12)
    records = [evidence._record(r, tz) for r in rows]
    local = {p["id"] for p in local_posts(records, MARKET)}
    showable = {p["id"] for p in showable_posts(records, MARKET)}
    assert local == {"loc_ng", "feed_unknown"}
    assert showable == {"loc_ng", "feed_unknown", "unsighted_unknown", "lowconf_za"}
    assert {r["post_id"] for r in rows if r["local_flag"]} == local
    assert {r["post_id"] for r in rows if r["showable_flag"]} == showable
    assert (rows[0]["available_local"], rows[0]["available_showable"], rows[0]["available_posts"]) == (2, 4, 5)


MUTANTS = {
    "tier dropped from the final order":
        [("ORDER BY f.market_member DESC, f.measured DESC", "ORDER BY f.measured DESC")],
    "tier dropped from the creator cap":
        [("ORDER BY mm.post_id IS NULL, s.measured DESC", "ORDER BY s.measured DESC")],
    "outlet cap off by one": [("class_rank <= @outlet_cap", "class_rank < @outlet_cap")],
    "no unique last key": [("f.eng DESC, f.post_id\nLIMIT 12", "f.eng DESC\nLIMIT 12")],
    "creator cap 3": [("r.creator_rank <= 2", "r.creator_rank <= 3")],
    "member window dropped": [("UPPER(k.market) = @market\n    AND k.cluster_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d",
                               "UPPER(k.market) = @market")],
    "pooled run counted as market member": [("UPPER(k.market) = @market\n",
                                              "(UPPER(k.market) = @market OR LOWER(k.market) = 'pan')\n")],
    "the news platform is not an outlet": [("ps.platform = 'news' OR ", "")],
    "registry handle not normalised": [("LOWER(REGEXP_REPLACE(TRIM(cr.handle), r'^@*(u/)?', ''))", "LOWER(cr.handle)")],
    "outlet stage count from the wrong stage": [("(SELECT COUNT(*) FROM g) after_outlet_cap",
                                                 "(SELECT COUNT(*) FROM c) after_outlet_cap")],
    "outlet stage counts the cap's removals past the pack":
        [(" OR IFNULL(k.kept_ahead, 0) >= 12)", ")")],
    "outlet stage counts a removal with only 11 kept posts ahead of it": [("kept_ahead, 0) >= 12", "kept_ahead, 0) >= 11")],
    "outlet stage counts a removal with 13 kept posts ahead of it": [("kept_ahead, 0) >= 12", "kept_ahead, 0) >= 13")],
    "outlet stage counts a removal by its place among all posts, not the kept ones":
        [("COUNTIF(NOT c.is_outlet OR c.class_rank <= @outlet_cap)", "COUNT(*)")],
    "member tier dropped from the outlet class rank": [("ORDER BY r.market_member DESC, r.measured DESC",
                                                        "ORDER BY r.measured DESC")],
    "pooled run flag read from the market run": [("WHERE k.item_id = @item_id AND LOWER(k.market) = 'pan'",
                                                  "WHERE k.item_id = @item_id AND UPPER(k.market) = @market")],
    "member window upper bound dropped": [("AND k.cluster_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d",
                                           "AND k.cluster_date >= DATE_SUB(@d, INTERVAL 6 DAY)")],
    "local stage count from the showable flag": [("(SELECT COUNTIF(c.local_flag) FROM c)",
                                                  "(SELECT COUNTIF(c.showable_flag) FROM c)")],
    "unknown handle reads null": [("IFNULL(ps.platform = 'news'", "(ps.platform = 'news'"),
                                  (", FALSE) is_outlet", ") is_outlet")],
    "an original column dropped": [("ps.views, ps.likes,", "ps.likes,")],
    "stage counts lost on an empty pack": [("LEFT JOIN f ON TRUE", "JOIN f ON TRUE")],
}


def mutate(sql, edits):
    for old, new in edits:
        assert sql.count(old) >= 1, f"the mutation target is not in the statement: {old!r}"
        sql = sql.replace(old, new, 1)
    return sql


@pytest.mark.parametrize("name", MUTANTS)
def test_each_mutant_of_the_statement_is_caught_by_at_least_one_check(name):
    broken = mutate(statement(), MUTANTS[name])
    assert broken != statement()
    killed_by = []
    for check, fn in CHECKS.items():
        try:
            fn(broken)
        except AssertionError:
            killed_by.append(check)
    assert killed_by, f"mutant survived every check: {name}"


def test_no_check_or_mutant_has_been_deleted_to_keep_the_suite_green():
    """Nothing else runs these tests, so the totals are pinned here: lowering either needs this line to change."""
    assert len(CHECKS) == 15 and len(MUTANTS) == 21


def test_the_statement_names_no_dataset_other_than_the_placeholders_and_is_read_only():
    sql = statement()
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|TRUNCATE)\b", sql, re.IGNORECASE)
    assert "{core}." in sql and "intelligence_42" not in sql
