"""DuckDB fixture tests for the brief's full-window market scope query."""

from datetime import date, datetime, timedelta, timezone

import pytest

from core.brief import evidence, market_scope, pack_order
from core.brief.market_scope import read_market_scope
from core.detect import sqlrun
from core.detect.tests import duck

UTC = timezone.utc
D = date(2026, 9, 30)


def sighting(market, seen):
    return {"source_market": market, "source_region": None, "route": "tiktok/trending",
            "protocol": "tiktok/trending?feed=local",
            "observed_at": datetime.combine(seen, datetime.min.time(), UTC), "obs_date": seen}


def add_post(con, post_id, *, published_at=None, geo_market=None, geo_confidence=None, geo_source=None,
             creator_id=None, engagement=0,
             observation_markets=("ZA",), observed_date=D, lane="sweep", lane_class="unbiased_rank",
             series=None, source_sightings=None, source_markets=None, item_ids=("i1",)):
    published_at = published_at or datetime(2026, 9, 29, 12, tzinfo=UTC)
    duck.load(con, "core.posts", [{"post_id": post_id, "creator_id": creator_id, "engagement": engagement,
                                    "published_at": published_at,
                                    "post_date": published_at.date(), "geo_market": geo_market,
                                    "geo_confidence": geo_confidence, "geo_source": geo_source}])
    duck.load(con, "core.post_items", [{"post_id": post_id, "item_id": item_id, "via": via}
                                        for item_id in item_ids for via in ("hashtag", "cluster")])
    duck.load(con, "core.post_observations", [
        {"post_id": post_id, "observed_at": datetime.combine(observed_date, datetime.min.time(), UTC),
         "observed_date": observed_date, "market": market, "lane": lane, "lane_class": lane_class,
         "series": series}
        for market in observation_markets])
    if source_sightings is not None or source_markets is not None:
        duck.load(con, "core.source_market_fixture", [{
            "post_id": post_id, "source_markets": source_markets or [],
            "source_sightings": source_sightings or [],
        }])


def scope(con, market="ZA"):
    client = duck.Client(con)
    value = read_market_scope(client, {"item_id": "i1"}, D, market, core="core", agent="agent")
    return value, client


def world():
    return duck.connect(views=False)


def test_market_scope_uses_the_distinct_creator_capped_ranked_twelve_post_card_set():
    con = world()
    add_post(con, "creator_a_1", creator_id="creator_a", engagement=300, geo_market="ZA",
             geo_confidence=0.9, geo_source="ext_region", observation_markets=("ZA", "ZA"))
    add_post(con, "creator_a_2", creator_id="creator_a", engagement=200,
             source_sightings=[sighting("ZA", D)])
    add_post(con, "creator_a_3", creator_id="creator_a", engagement=95, geo_market="ZA",
             geo_confidence=0.9, geo_source="ext_region")
    for n in range(1, 12):
        fields = {"geo_market": "ZA", "geo_confidence": 0.9, "geo_source": "ext_region"} if n <= 4 else {}
        add_post(con, f"creator_{n}", creator_id=f"creator_{n}", engagement=200 - 10 * n, **fields)

    # These posts have tempting location or feed evidence but no eligible observation.
    for post_id, lane, lane_class, series in (
        ("legacy", "legacy", "legacy", None),
        ("placebo", "placebo", "search_presence", None),
        ("agent_live", "agent_live", "search_presence", None),
    ):
        add_post(con, post_id, geo_market="ZA", geo_confidence=0.99, lane=lane, lane_class=lane_class,
                 series=series, source_sightings=[sighting("ZA", D)])

    result, client = scope(con)

    assert result == {"market_scope": "global", "market_posts7": 6, "total_posts7": 12, "market_share7": 0.5}
    assert len(client.sql) == 1


def test_a_tie_and_an_empty_window_both_route_to_global_discover():
    con = world()
    for n in range(2):
        add_post(con, f"local_{n}", geo_market="ZA", geo_confidence=0.9, geo_source="ext_region")
    for n in range(2):
        add_post(con, f"unknown_{n}")

    tied, _ = scope(con)
    assert tied == {"market_scope": "global", "market_posts7": 2, "total_posts7": 4, "market_share7": 0.5}

    empty, _ = scope(world())
    assert empty == {"market_scope": "global", "market_posts7": 0, "total_posts7": 0, "market_share7": None}


def test_stale_source_index_profile_home_and_observation_market_do_not_prove_market_scope():
    con = world()
    stale = D - timedelta(days=7)
    add_post(con, "index_only", observation_markets=("ZA",), source_markets=["ZA"], source_sightings=[])
    add_post(con, "stale_sighting", observation_markets=("ZA",), geo_market="ZA", geo_confidence=0.69,
             source_markets=["ZA"], source_sightings=[sighting("ZA", stale)])
    add_post(con, "assumed_only", observation_markets=("ZA",))
    duck.load(con, "core.creators", [{"creator_id": "creator", "platform": "tiktok", "home_market": "ZA"}])

    result, _ = scope(con)

    assert result == {"market_scope": "global", "market_posts7": 0, "total_posts7": 3, "market_share7": 0.0}


def test_high_confidence_language_geo_is_not_a_known_physical_location():
    con = world()
    add_post(con, "language_guess", geo_market="ZA", geo_confidence=1.0, geo_source="language")
    add_post(con, "known_location", geo_market="ZA", geo_confidence=0.9, geo_source="place_mention")

    result, _ = scope(con)

    assert result == {"market_scope": "global", "market_posts7": 1, "total_posts7": 2, "market_share7": 0.5}


@pytest.mark.parametrize(("rows", "message"), [
    ([], "exactly one row"),
    (None, "exactly one row"),
    ([{"total_posts7": 4, "market_posts7": 5}], "between zero and total"),
    ([{"total_posts7": 4, "market_posts7": -1}], "between zero and total"),
    ([{"total_posts7": 4.5, "market_posts7": 1}], "finite non-boolean integers"),
    ([{"total_posts7": float("nan"), "market_posts7": 1}], "finite non-boolean integers"),
    ([{"total_posts7": True, "market_posts7": False}], "finite non-boolean integers"),
    ([{"total_posts7": "4", "market_posts7": "1"}], "finite non-boolean integers"),
    ([{"total_posts7": 4}], "missing required count fields"),
    ([None], "count fields"),
])
def test_invalid_market_scope_query_results_fail_closed(monkeypatch, rows, message):
    monkeypatch.setattr(market_scope.sqlrun, "query", lambda *args, **kwargs: rows)

    with pytest.raises(ValueError, match=message):
        read_market_scope(object(), {"item_id": "i1"}, D, "ZA")


def test_market_scope_query_must_return_exactly_one_row(monkeypatch):
    monkeypatch.setattr(market_scope.sqlrun, "query", lambda *args, **kwargs: [
        {"total_posts7": 0, "market_posts7": 0}, {"total_posts7": 0, "market_posts7": 0}])

    with pytest.raises(ValueError, match="exactly one row"):
        read_market_scope(object(), {"item_id": "i1"}, D, "ZA")


@pytest.mark.parametrize("market,offset", [("ZA", 2), ("NG", 1), ("KE", 3)])
def test_published_window_uses_aware_market_local_midnights(market, offset):
    con = world()
    start = datetime.combine(D - timedelta(days=6), datetime.min.time(), timezone(timedelta(hours=offset)))
    end = start + timedelta(days=7)
    add_post(con, "at_start", published_at=start.astimezone(UTC), geo_market=market, geo_confidence=0.9,
             geo_source="ext_region", observation_markets=(market,))
    add_post(con, "before_start", published_at=(start - timedelta(seconds=1)).astimezone(UTC),
             geo_market=market, geo_confidence=0.9, geo_source="ext_region", observation_markets=(market,))
    add_post(con, "at_end", published_at=end.astimezone(UTC), geo_market=market, geo_confidence=0.9,
             geo_source="ext_region", observation_markets=(market,))

    result, _ = scope(con, market)

    assert result == {"market_scope": "market", "market_posts7": 1, "total_posts7": 1, "market_share7": 1.0}


def confirm_find(con, post_id, **fields):
    """A find of the brief's confirm search, linked to i1 the way parse._seed_links links it."""
    add_post(con, post_id, lane="confirm", lane_class="search_presence", series="search", **fields)


def four_local_measured_posts(con):
    for n in range(4):
        add_post(con, f"local_{n}", creator_id=f"local_creator_{n}", engagement=100 + n, geo_market="ZA",
                 geo_confidence=0.9, geo_source="ext_region")


def nine_global_confirm_finds(con):
    for n in range(9):
        confirm_find(con, f"global_find_{n}", creator_id=f"global_creator_{n}", engagement=50_000 + n)


def test_confirm_finds_from_elsewhere_do_not_dilute_an_item_read_as_market():
    con = world()
    four_local_measured_posts(con)

    before, _ = scope(con)
    assert before == {"market_scope": "market", "market_posts7": 4, "total_posts7": 4, "market_share7": 1.0}

    nine_global_confirm_finds(con)

    after, _ = scope(con)
    assert after == before


def mixed_confirm_finds(con):
    add_post(con, "local_measured", creator_id="c1", engagement=10, geo_market="ZA", geo_confidence=0.9,
             geo_source="ext_region")
    add_post(con, "unlocated_measured", creator_id="c2", engagement=10)
    # A post the sweep saw and confirm found again still counts through its sweep sighting, as before.
    add_post(con, "swept_and_confirmed", creator_id="c3", engagement=10)
    duck.load(con, "core.post_observations", [{
        "post_id": "swept_and_confirmed", "observed_at": datetime.combine(D, datetime.min.time(), UTC),
        "observed_date": D, "market": "ZA", "lane": "confirm", "lane_class": "search_presence",
        "series": "search"}])
    # Confirm finds sourced from or located in the market count as local.
    confirm_find(con, "confirm_country_search", creator_id="c4", engagement=900,
                 source_markets=["ZA"], source_sightings=[sighting("ZA", D)])
    confirm_find(con, "confirm_located", creator_id="c5", engagement=800, geo_market="ZA",
                 geo_confidence=0.9, geo_source="place_mention")
    # Confirm finds that are not local by the same rule are left out of the pack.
    confirm_find(con, "confirm_elsewhere", creator_id="c6", engagement=70_000)
    confirm_find(con, "confirm_other_market", creator_id="c7", engagement=60_000, source_markets=["NG"],
                 source_sightings=[sighting("NG", D)])
    confirm_find(con, "confirm_stale_source", creator_id="c8", engagement=50_000, source_markets=["ZA"],
                 source_sightings=[sighting("ZA", D - timedelta(days=7))])
    confirm_find(con, "confirm_language_guess", creator_id="c9", engagement=40_000, geo_market="ZA",
                 geo_confidence=1.0, geo_source="language")


def test_confirm_finds_sourced_from_or_located_in_the_market_still_count_as_local():
    con = world()
    mixed_confirm_finds(con)

    result, _ = scope(con)

    assert result == {"market_scope": "market", "market_posts7": 3, "total_posts7": 5, "market_share7": 0.6}


def view_scope(con, market="ZA"):
    duck.load(con, "agent.runs", [{"run_id": "detect-1", "stage": "detect", "run_date": D, "status": "ok",
                                   "finished_at": datetime(2026, 9, 30, 6, tzinfo=UTC)}])
    duck.load(con, "core.item_state", [{"metric_date": D, "market": market, "item_id": "i1",
                                        "run_id": "detect-1"}])
    rows = duck.query(con, "SELECT market_scope, market_posts7, total_posts7, market_share7 "
                           "FROM {core}.v_item_market_scope WHERE item_id = 'i1' AND market = @market "
                           "AND metric_date = @d", {"market": market, "d": D})
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize("build", [
    lambda con: (four_local_measured_posts(con), nine_global_confirm_finds(con)),
    mixed_confirm_finds,
])
def test_detect_market_scope_view_agrees_with_the_brief_query_on_confirm_finds(build):
    query_con, view_con = world(), duck.connect()
    build(query_con)
    build(view_con)

    expected, _ = scope(query_con)

    assert view_scope(view_con) == expected


@pytest.mark.parametrize("detect_view", [False, True])
@pytest.mark.parametrize("lane", ["sweep", "confirm"])
@pytest.mark.parametrize("location,confidence,geo_source,source,seen,is_local", [
    ("AE", 0.9, "ext_region", "NG", D, False),
    ("AE", 0.9, "ext_region", None, None, False),
    ("NG", 0.7, "ext_region", None, None, True),
    (None, None, None, "NG", D, True),
    (None, None, None, None, None, False),
    (None, None, None, "NG", D - timedelta(days=7), False),
    ("AE", 0.69, "ext_region", "NG", D, True),
    ("AE", 0.9, "language", "NG", D, True),
    ("AE", None, "ext_region", "NG", D, True),
    ("AE", 0.9, None, "NG", D, True),
    ("", 0.9, "ext_region", "NG", D, True),
    (None, None, None, "invalid", D, False),
])
def test_known_foreign_veto_agrees_in_brief_and_detect_scope_without_changing_unknowns(
        detect_view, lane, location, confidence, geo_source, source, seen, is_local):
    con = duck.connect(views=detect_view)
    add_post(con, "p", geo_market=location, geo_confidence=confidence, geo_source=geo_source,
             observation_markets=("NG",), lane=lane,
             lane_class="search_presence" if lane == "confirm" else "unbiased_rank",
             source_sightings=[sighting(source, seen)] if source is not None else [])

    result = view_scope(con, "NG") if detect_view else scope(con, "NG")[0]
    total = int(lane != "confirm" or is_local)
    assert result == {"market_scope": "market" if is_local else "global", "market_posts7": int(is_local),
                      "total_posts7": total, "market_share7": float(is_local) if total else None}
    assert con.execute("SELECT COUNT(*) FROM core.posts WHERE post_id = 'p'").fetchone()[0] == 1
    if not detect_view:
        assert evidence_ids(con, "NG") == ({"p"} if total else set())


# The suppression list (SETUP.md data protection): the scope counts describe the pack evidence.sql admits, so a
# suppressed creator's posts leave the count before it ranks, as they leave the pack.


def evidence_ids(con, market="ZA"):
    start = datetime.combine(D - timedelta(days=6), datetime.min.time(), timezone(timedelta(hours=2)))
    # The path production takes: the registry goes as a typed array parameter, which sqlrun.query cannot send.
    rows = evidence._pack_rows(duck.Client(con), {"item_id": "i1", "market": market, "d": D, "start": start,
                                                  "end": start + timedelta(days=7),
                                                  "outlet_cap": pack_order.OUTLET_CAP}, "core", "agent")
    return {r["post_id"] for r in rows if r["post_id"] is not None}  # an empty pack comes back as one row, no post


def located_za():
    return {"geo_market": "ZA", "geo_confidence": 0.9, "geo_source": "ext_region"}


def test_suppressed_local_posts_do_not_make_a_pack_of_mostly_unlocated_posts_market_local():
    con = world()
    for n in range(6):
        add_post(con, f"hidden_{n}", creator_id=f"hidden_{n}", engagement=1000 + n, **located_za())
    for n in range(2):
        add_post(con, f"local_{n}", creator_id=f"local_{n}", engagement=500 + n, **located_za())
    for n in range(10):
        add_post(con, f"unlocated_{n}", creator_id=f"unlocated_{n}", engagement=100 + n)
    duck.load(con, "core.suppressed_fixture", [{"creator_id": f"hidden_{n}"} for n in range(6)])

    result, _ = scope(con)

    assert result == {"market_scope": "global", "market_posts7": 2, "total_posts7": 12,
                      "market_share7": 2 / 12}
    pack = evidence_ids(con)
    assert len(pack) == 12 and not any(p.startswith("hidden_") for p in pack)
    assert len({p for p in pack if p.startswith("local_")}) == result["market_posts7"]


def test_suppressed_unlocated_posts_do_not_hide_a_pack_of_mostly_local_posts():
    con = world()
    for n in range(6):
        add_post(con, f"hidden_{n}", creator_id=f"hidden_{n}", engagement=1000 + n)
    for n in range(7):
        add_post(con, f"local_{n}", creator_id=f"local_{n}", engagement=500 + n, **located_za())
    for n in range(5):
        add_post(con, f"unlocated_{n}", creator_id=f"unlocated_{n}", engagement=100 + n)
    duck.load(con, "core.suppressed_fixture", [{"creator_id": f"hidden_{n}"} for n in range(6)])

    result, _ = scope(con)

    assert result == {"market_scope": "market", "market_posts7": 7, "total_posts7": 12,
                      "market_share7": 7 / 12}
    pack = evidence_ids(con)
    assert len(pack) == 12 and not any(p.startswith("hidden_") for p in pack)
    assert len({p for p in pack if p.startswith("local_")}) == result["market_posts7"]
