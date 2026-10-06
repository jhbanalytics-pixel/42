from datetime import date
from pathlib import Path

import sqlglot
from sqlglot import exp


SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "tvf_search_posts.sql"
SINCE = date(2026, 9, 22)
UNTIL = date(2026, 9, 28)


def _tvf():
    return sqlglot.parse_one(SQL_PATH.read_text(encoding="utf-8"), read="bigquery")


def _and_terms(node):
    if isinstance(node, exp.And):
        yield from _and_terms(node.this)
        yield from _and_terms(node.expression)
    else:
        yield node


def _duckdb_sql(node):
    translated = node.copy()
    for table in translated.find_all(exp.Table):
        table.set("catalog", None)
    return translated.sql(dialect="duckdb")


def _market_fixture_query():
    distance = next(node for node in _tvf().find_all(exp.Anonymous) if node.name.upper() == "DISTANCE")
    scoring = distance.find_ancestor(exp.Select)
    terms = list(_and_terms(scoring.args["where"].this))
    market_term = next(
        term for term in terms
        if any(isinstance(column, exp.Column) and column.name == "market" and not column.table
               for column in term.find_all(exp.Column))
    )
    date_term = next(
        term for term in terms
        if any(isinstance(column, exp.Column) and column.name == "post_date"
               for column in term.find_all(exp.Column))
    )
    return (
        "SELECT w.post_id FROM intelligence_42_core.posts AS w "
        "CROSS JOIN (SELECT ? AS market, ? AS since, ? AS until) AS params "
        f"WHERE {_duckdb_sql(date_term)} AND {_duckdb_sql(market_term)}"
    )


def _fixture():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE TABLE intelligence_42_core.posts ("
                "post_id VARCHAR, platform VARCHAR, creator_id VARCHAR, post_date DATE, "
                "geo_market VARCHAR, geo_source VARCHAR)")
    con.execute("CREATE TABLE intelligence_42_core.creators ("
                "platform VARCHAR, creator_id VARCHAR, home_market VARCHAR)")
    con.execute("CREATE TABLE intelligence_42_core.v_post_source_markets ("
                "post_id VARCHAR, source_markets VARCHAR[], source_sightings STRUCT("
                "source_market VARCHAR, source_region VARCHAR, route VARCHAR, protocol VARCHAR, "
                "observed_at TIMESTAMP, obs_date DATE)[])")

    rows = [
        ("ke_feed", "tiktok", "u_ke_feed", date(2026, 9, 27), None, None),
        ("ng_feed", "tiktok", "u_ng_feed", date(2026, 9, 27), None, None),
        ("ng_located_ke_feed", "tiktok", "u_ng_located", date(2026, 9, 27), "NG", "ext_region"),
        ("old_ke_feed", "tiktok", "u_old_ke", date(2026, 9, 27), None, None),
        ("ke_outlet", "tiktok", "u_ke_outlet", date(2026, 9, 27), None, None),
        ("null_source_feed", "tiktok", "u_null_source", date(2026, 9, 27), None, None),
        ("ig_location_feed", "instagram", "u_ig_location", date(2026, 9, 27), None, None),
        ("profile_only", "tiktok", "u_profile", date(2026, 9, 27), None, None),
        ("cross_platform_profile", "tiktok", "shared", date(2026, 9, 27), None, None),
        ("profile_geo_source", "tiktok", "u_profile_geo", date(2026, 9, 27), "NG", "home_market"),
        ("located_elsewhere", "tiktok", "u_elsewhere", date(2026, 9, 27), "NG", "ext_region"),
        ("located_ke", "tiktok", "u_located_ke", date(2026, 9, 27), "KE", "ext_region"),
        ("outside_post_window", "tiktok", "u_outside", date(2026, 9, 20), "KE", "ext_region"),
    ]
    con.executemany("INSERT INTO intelligence_42_core.posts VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.executemany("INSERT INTO intelligence_42_core.creators VALUES (?, ?, ?)", [
        ("tiktok", "u_profile", "KE"),
        ("x", "shared", "KE"),
        ("tiktok", "u_profile_geo", "KE"),
    ])

    def sighting(market, region, route, day):
        return {"source_market": market, "source_region": region, "route": route,
                "protocol": route, "observed_at": None, "obs_date": day}

    sightings = {
        "ke_feed": [sighting("KE", "KE", "tiktok/trending", date(2026, 9, 27))],
        "ng_feed": [sighting("NG", "NG", "tiktok/trending", date(2026, 9, 27))],
        "ng_located_ke_feed": [sighting("KE", "KE", "tiktok/trending", date(2026, 9, 27))],
        "old_ke_feed": [sighting("KE", "KE", "tiktok/trending", date(2026, 9, 15))],
        "ke_outlet": [sighting("KE", None, "twitter/user/tweets", date(2026, 9, 27))],
        "null_source_feed": [sighting(None, "KE", "instagram/location", date(2026, 9, 27))],
        "ig_location_feed": [sighting("KE", None, "instagram/location", date(2026, 9, 27))],
        "located_elsewhere": [sighting("KE", "KE", "tiktok/trending", date(2026, 9, 27))],
    }
    con.executemany(
        "INSERT INTO intelligence_42_core.v_post_source_markets VALUES (?, ?, ?)",
        [(post_id, sorted({s["source_market"] for s in entries}), entries)
         for post_id, entries in sightings.items()],
    )
    return con


def test_tvf_market_filter_executes_its_own_dated_source_and_profile_predicate():
    con = _fixture()
    query = _market_fixture_query()
    rows = con.execute(query, ["KE", SINCE, UNTIL]).fetchall()
    assert {row[0] for row in rows} == {
        "ke_feed", "ke_outlet", "ig_location_feed", "profile_only", "profile_geo_source", "located_ke",
    }


def test_tvf_market_null_preserves_all_posts_in_the_requested_post_date_window():
    con = _fixture()
    query = _market_fixture_query()
    rows = con.execute(query, [None, SINCE, UNTIL]).fetchall()
    assert {row[0] for row in rows} == {
        "ke_feed", "ng_feed", "ng_located_ke_feed", "old_ke_feed", "ke_outlet", "null_source_feed",
        "ig_location_feed", "profile_only", "cross_platform_profile", "profile_geo_source",
        "located_elsewhere", "located_ke",
    }
