"""The recovered prism/profiles panels and tiktok/song counters start a fresh health reference.

Before the parser fixes these calls succeeded and parsed zero items, so collection_health holds valid days with a
median of 0 items under their old protocol. judge() then fails any nonzero day as "items" until fewer than 3 such
days remain in the 28 day window, and G1 holds the cards on the main platform. A new protocol token gives the
recovered series its own reference (DATA.md 3.2). The G1 checks run the publish gate's real context SQL on DuckDB.
"""

from datetime import date

import pytest

from core.brief.tests.test_brief_gatectx import ctx, item, series_platform, world
from core.collect import curated_creators, job, local_sources, writers
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day, rid
from core.trust.gate import gate_card

PLAN_DAY = date(2026, 9, 29)
STALE = (0.0, 20)  # what 20 zero item days leave: a median of 0 over 20 valid days


def _gossip(market="ZA"):
    fetch = next(f for f in local_sources.plan(PLAN_DAY) if f.route == "prism/profiles" and f.market == market)
    return {"name": "gossip", "market": market, "route": fetch.route, "platform": fetch.platform,
            "series": fetch.series, "lane_class": fetch.lane_class, "protocol": fetch.protocol(),
            "legacy": job.panel_protocol(fetch.params["items"]), "units": len(fetch.params["items"]), "items": 3}


def _desk(market="ZA"):
    call = next(c for c in job.panel_calls(market, PLAN_DAY, job.load_config()) if c.route == "prism/profiles")
    return {"name": "desk", "market": market, "route": call.route, "platform": job.ROUTES[call.route][1],
            "series": call.series(), "lane_class": call.lane_class(), "protocol": call.series_protocol(),
            "legacy": job.panel_protocol(call.params["items"]), "units": len(call.params["items"]), "items": 15}


def _song():
    call = job.Call("12", "tiktok/song", {"clipId": "7300000000000000009"}, "ZA", "watchlist",
                    seed_key="7300000000000000009")
    return {"name": "song", "market": call.health_market(), "route": call.route, "platform": job.ROUTES[call.route][1],
            "series": call.series(), "lane_class": call.lane_class(), "protocol": call.series_protocol(),
            "legacy": "tiktok/song", "units": 1, "items": 5}


CASES = {"gossip": _gossip, "desk": _desk, "song": _song}


def record(case, d, *, protocol, items=None, ok=True):
    return {"row": "x", "route": case["route"], "market": case["market"], "day": d.isoformat(),
            "platform": case["platform"], "series": case["series"], "protocol": protocol,
            "lane_class": case["lane_class"], "status": "ok" if ok else "http_503", "ok": ok, "calls": 1,
            "units_planned": case["units"], "units_ok": case["units"] if ok else 0,
            "items": case["items"] if items is None else items, "post_ids": [], "reason": "",
            "failure": "" if ok else "http_5xx"}


def health(case, d, refs, **kw):
    protocol = kw.pop("protocol", case["protocol"])
    [row] = writers.health_rows([record(case, d, protocol=protocol, **kw)], [], {d.isoformat(): refs},
                                rid("collect", d))
    return row


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_new_token_leaves_the_zero_item_reference_behind(name):
    case = CASES[name]()
    assert case["protocol"] != case["legacy"]
    stale = {(case["market"], case["series"], case["legacy"]): STALE}
    fresh = health(case, D, stale)
    assert (fresh["valid"], fresh["invalid_reason"], fresh["ref_days"], fresh["ref_items"]) == (True, None, 0, None)
    # The same day under the old token is the failure this guards against.
    old = health(case, D, stale, protocol=case["legacy"])
    assert (old["valid"], old["invalid_reason"], old["ref_days"]) == (False, "items", 20)


def test_the_new_tokens_are_pinned():
    assert _song()["protocol"] == "tiktok/song?proto=v2"
    assert _desk("ZA")["protocol"] == "panel:9a3c154daf73:v2"
    assert _gossip("ZA")["protocol"] == "panel:913d46fb659e:v2"
    for market in ("ZA", "NG", "KE"):
        for make in (_desk, _gossip):
            case = make(market)
            assert case["protocol"] == case["legacy"] + ":v2"


def test_every_prism_profiles_call_of_the_day_takes_the_new_token_curated_panel_included():
    calls = [c for c in job.panel_calls("ZA", PLAN_DAY, job.load_config(), curated_creators.load_manifest(), 50)
             if c.route == "prism/profiles"]
    assert len(calls) >= 2  # the desk and at least one curated batch
    assert all(c.protocol.endswith(":v2") and c.series_protocol() == c.protocol for c in calls)


def test_other_panels_and_counters_keep_their_tokens():
    config = job.load_config()
    handles = [e["handle"] for e in config["hubs"]["markets"]["za"].get("x") or []]
    x_calls = [c for c in job.panel_calls("ZA", PLAN_DAY, config) if c.route == "twitter/user/tweets"]
    assert x_calls and {c.protocol for c in x_calls} == {job.panel_protocol(handles)}
    assert not job.panel_protocol(handles).endswith(":v2")
    hashtag = job.Call("12", "tiktok/hashtag", {"hashtag": "x"}, "ZA", "watchlist", seed_key="x")
    assert hashtag.series_protocol() == "tiktok/hashtag"


def _gate(case, protocol, days=3):
    con = world()
    series_id = f"i1|{case['market']}|{case['series']}|{protocol}"
    series_platform(con, "instagram", series_id)
    stale = {(case["market"], case["series"], case["legacy"]): STALE}
    rows = [health(case, day(i), stale, protocol=protocol) for i in range(days)]
    for row in rows:
        row["day"] = date.fromisoformat(row["day"])
    duck.load(con, "core.collection_health", rows)
    got = ctx(con, item(main_series_id=series_id))
    return got["valid_days"], gate_card({"item_id": "i1"}, got).rule


@pytest.mark.parametrize("name", ["gossip", "desk"])
def test_g1_holds_no_card_on_a_new_token_and_does_on_the_old_one(name):
    case = CASES[name]()
    assert _gate(case, case["protocol"]) [0] == [True, True, True]
    assert _gate(case, case["protocol"])[1] != "G1"
    assert _gate(case, case["legacy"]) == ([False, False, False], "G1")


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_new_token_still_fails_on_real_failures_once_it_has_its_own_reference(name):
    case = CASES[name]()
    own = {(case["market"], case["series"], case["protocol"]): (case["items"] * 1.0, 3)}
    assert health(case, D, own)["valid"] is True
    too_many = health(case, D, own, items=case["items"] * 3)
    assert (too_many["valid"], too_many["invalid_reason"]) == (False, "items")
    empty = health(case, D, own, items=0)
    assert (empty["valid"], empty["invalid_reason"]) == (False, "items")
    down = health(case, D, own, ok=False)
    assert down["valid"] is False and down["invalid_reason"].startswith("calls")


# REFERENCE_SQL itself, run on DuckDB, and the sites that must keep their token

def _reference_on_duck(health_rows, runs, day):
    """writers.REFERENCE_SQL as the warehouse runs it, transpiled to DuckDB over real tables. Returns the
    (market, series, protocol) -> (median items, valid days) dict that writers.reference builds."""
    import duckdb
    import sqlglot

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE TABLE runs (run_id VARCHAR, stage VARCHAR, status VARCHAR, finished_at TIMESTAMPTZ)")
    con.execute("CREATE TABLE health (run_id VARCHAR, day DATE, market VARCHAR, series VARCHAR, protocol VARCHAR, "
                "items BIGINT, valid BOOLEAN)")
    for run_id, finished in runs:
        con.execute("INSERT INTO runs VALUES (?, 'collect', 'ok', ?)", [run_id, finished])
    for row in health_rows:
        con.execute("INSERT INTO health VALUES (?, ?, ?, ?, ?, ?, ?)", row)
    sql = writers.REFERENCE_SQL.format(runs="runs", health="health").replace("@d", f"DATE '{day.isoformat()}'")
    duck_sql = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
    rows = con.execute(duck_sql).fetchall()
    return {(m, s, p): (float(items), int(days)) for m, s, p, items, days in rows}


def _zero_days_then_deploy_day(case, deploy_day):
    """Twenty valid zero item days under the old token, ending on the deploy day, which also holds a later
    rerun under the v2 token that landed items."""
    old, new = case["legacy"], case["protocol"]
    key = (case["market"], case["series"])
    rows, runs = [], []
    for i in range(20):
        d = date.fromordinal(deploy_day.toordinal() - i)
        runs.append((f"r{i}", f"{d.isoformat()}T00:30:00+00:00"))
        rows.append((f"r{i}", d, *key, old, 0, True))
    runs.append(("deploy-rerun", f"{deploy_day.isoformat()}T01:30:00+00:00"))
    rows.append(("deploy-rerun", deploy_day, *key, new, case["items"], True))
    return rows, runs


@pytest.mark.parametrize("name", ["gossip", "desk"])
def test_reference_sql_keeps_the_old_zero_days_and_the_v2_day_apart(name):
    case = CASES[name]()
    deploy = date(2026, 9, 29)
    rows, runs = _zero_days_then_deploy_day(case, deploy)
    refs = _reference_on_duck(rows, runs, date(2026, 9, 30))
    key = (case["market"], case["series"])
    # Both rows of the deploy day survive: protocol is part of the one-row-per-day partition.
    assert refs[(*key, case["legacy"])] == (0.0, 20)
    assert refs[(*key, case["protocol"])] == (float(case["items"]), 1)


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_v2_day_after_old_token_zero_days_has_no_reference_from_the_warehouse_query(name):
    case = CASES[name]()
    day_before = date(2026, 9, 28)
    key = (case["market"], case["series"])
    runs = [(f"r{i}", f"{date.fromordinal(day_before.toordinal() - i).isoformat()}T00:30:00+00:00")
            for i in range(20)]
    rows = [(f"r{i}", date.fromordinal(day_before.toordinal() - i), *key, case["legacy"], 0, True)
            for i in range(20)]
    refs = _reference_on_duck(rows, runs, date(2026, 9, 29))
    assert refs[(*key, case["legacy"])] == (0.0, 20)
    assert (*key, case["protocol"]) not in refs
    # The writer then reads no reference for the v2 token, and its first day is valid with ref_days 0.
    [fresh] = writers.health_rows([record(case, date(2026, 9, 29), protocol=case["protocol"])], [],
                                  {"2026-09-29": refs}, rid("collect", date(2026, 9, 29)))
    assert (fresh["valid"], fresh["invalid_reason"], fresh["ref_days"], fresh["ref_items"]) == (True, None, 0, None)


def test_song_videos_and_the_facebook_pages_panel_are_not_versioned():
    from core.collect import parse

    assert parse.PROTOCOL_VERSIONS == {"tiktok/song": "v2"}
    videos = job.Call("11", "tiktok/song/videos", {"clipId": "7300000000000000009", "use": 1}, "ZA", "watchlist",
                      seed_key="7300000000000000009")
    assert videos.series_protocol() == "tiktok/song/videos"
    assert "proto=" not in videos.series_protocol()
    calls, _ = job.harvest_calls("ZA", PLAN_DAY, job.load_config())
    pages = [c for c in calls if c.route == "facebook/profile/posts"]
    assert pages
    members = [c.params["pageId"] for c in pages]
    assert {c.protocol for c in pages} == {job.panel_protocol(members)}
    assert not any(c.protocol.endswith(":v2") for c in pages)
