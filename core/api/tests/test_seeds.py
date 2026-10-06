"""Seeds (contract.md section 19) over the fixture store, the BigQuery SQL it runs and its route."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from core.api import seeds
from core.api import store as store_mod
from core.api.discover import BadRequest
from core.api.store import FIXTURES, MAX_BYTES, BigQueryStore, FixtureStore

PASS = "seeds-passcode"
STEP = "7107ad863306852108ebb88392f1d5e0293922af5f5e7a5a4b81d466930f646e"
HERITAGE = "afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156"
RISING = "4b942b2ea393cbe2c2e4e883ed5be7441b361229e446c6c5bbcb7fdba540fa33"
BOARD = "447e6354b5e51acf11942783fda98f72ce4003b53c18f62d2e35753c3c44ade3"
OUTAGE = "196c5995cccbc7eff97f244b604378afbc28dc4109d809a6d6e3cf7a4201bd97"
COLA = "31550a81e157653d924381e70a8a17f45012b04a85e496ade43753036a6f8e36"
HANDLES = ("fixture_za_creator_handle", "fixture_za_person", "fixture_za_creator_two", "fixture_ng_macro",
           "fixture_ng_hidden")


def figures(value):
    if isinstance(value, dict):
        if "result_hash" in value:
            yield value
        else:
            for v in value.values():
                yield from figures(v)
    elif isinstance(value, list):
        for v in value:
            yield from figures(v)


def copy_fixtures(tmp_path, drop=(), **tables):
    for f in FIXTURES.glob("*.json"):
        if f.stem not in drop:
            (tmp_path / f.name).write_bytes(f.read_bytes())
    for name, rows in tables.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(rows), encoding="utf-8")
    return FixtureStore(root=tmp_path)


@pytest.fixture
def za():
    return seeds.build_seeds(FixtureStore(), "ZA")


def test_the_queue_is_the_latest_queued_day_in_priority_order(za):
    q = za["queue"]
    assert za["status"] == "ok" and za["market"] == "ZA" and za["market_name"] == "South Africa"
    assert q["seed_date"] == "2026-10-01"
    got = [(s["label"], s["lane"], s["credits"]["value"]) for s in q["seeds"]]
    assert got == [("Fixture Cola", "expansion", 5.0), ("#fixture_za_step", "expansion", 1.0),
                   ("fixture za rising topic", "expansion", 5.0), ("fixture za other meme", "exploration", 5.0),
                   ("fixture_za_anchor", "anchor", 1.0)]


def test_a_queued_seed_says_where_42_will_look_and_links_to_its_page(za):
    by = {s["label"]: s for s in za["queue"]["seeds"]}
    assert by["#fixture_za_step"]["platforms"] == ["TikTok"]
    assert by["Fixture Cola"]["platforms"] == ["X", "Threads", "Reddit"]  # a brand is searched where news is talked
    assert by["fixture za rising topic"]["platforms"] == ["Instagram", "YouTube", "Reddit", "X", "Threads",
                                                          "Facebook"]
    assert by["#fixture_za_step"]["href"] == f"#/t/{STEP}?market=ZA"
    assert by["fixture_za_anchor"]["item_id"] is None
    assert by["fixture_za_anchor"]["href"] == "#/seedpath/fixture_za_anchor?region=za"
    assert by["Fixture Cola"]["lane_words"] == "Following what is rising"
    assert by["fixture za other meme"]["lane_words"] == "Trying something new"
    assert by["fixture_za_anchor"]["lane_words"] == "Re-checking what worked"


def test_lanes_and_totals_count_creator_accounts_but_never_the_hidden_control(za):
    q = za["queue"]
    lanes = {lane["lane"]: (lane["seeds"]["value"], lane["credits"]["value"]) for lane in q["lanes"]}
    assert lanes == {"expansion": (5, 13.0), "exploration": (1, 5.0), "anchor": (1, 1.0)}
    assert q["seeds_total"]["value"] == 7 and q["credits_total"]["value"] == 19.0
    assert q["creator_accounts"]["seeds"]["value"] == 2 and q["creator_accounts"]["credits"]["value"] == 2.0
    assert [lane["lane"] for lane in q["lanes"]] == ["expansion", "exploration", "anchor"]


def test_the_hidden_control_never_appears():
    for market in ("ZA", "NG", "KE"):
        body = seeds.build_seeds(FixtureStore(), market)
        text = json.dumps(body)
        assert "placebo" not in text and BOARD not in text and "fixture_za_board" not in text
        assert "control" not in text.lower()


def test_last_results_take_the_newest_day_with_yields_and_the_max_per_seed(za):
    r = za["results"]
    assert r["seed_date"] == "2026-09-30"
    got = [(s["label"], s["posts"]["value"], s["new_creators"]["value"]) for s in r["seeds"]]
    assert got == [("#fixture_za_heritage", 14, 3), ("fixture_za_anchor", 4, 0), ("#fixture_za_outage", 0, 0)]
    lanes = {lane["lane"]: (lane["seeds"]["value"], lane["posts"]["value"], lane["new_creators"]["value"])
             for lane in r["lanes"]}
    assert lanes == {"expansion": (2, 19, 4), "exploration": (1, 0, 0), "anchor": (1, 4, 0)}
    assert (r["totals"]["seeds"]["value"], r["totals"]["posts"]["value"], r["totals"]["new_creators"]["value"]) \
        == (4, 23, 4)
    c = r["creator_accounts"]
    assert (c["seeds"]["value"], c["posts"]["value"], c["new_creators"]["value"]) == (1, 5, 1)
    assert r["seeds"][0]["href"] == f"#/t/{HERITAGE}?market=ZA"


def test_every_number_carries_a_seeds_query_id(za):
    found = list(figures(za))
    assert len(found) > 20
    assert {f["query_id"] for f in found} == {"q_seeds_queue", "q_seeds_results"}
    assert all(f["result_hash"].startswith("sha256:") and f["run_id"] is None for f in found)


def test_no_handle_leaves_the_api():
    for market in ("ZA", "NG", "KE"):
        text = json.dumps(seeds.build_seeds(FixtureStore(), market))
        for handle in HANDLES:
            assert handle not in text
        assert "@" not in text and "profile/videos" not in text and "user/tweets" not in text


def test_a_suppressed_creator_is_not_even_counted():
    ng = seeds.build_seeds(FixtureStore(), "NG")
    q = ng["queue"]
    assert [s["label"] for s in q["seeds"]] == ["#fixture_ng_owambe"]
    assert q["creator_accounts"]["seeds"]["value"] == 1  # fixture_ng_macro; fixture_ng_hidden is suppressed
    assert q["seeds_total"]["value"] == 2
    assert ng["results"] is None


def test_an_item_the_map_calls_a_creator_is_a_creator_account_whatever_the_row_says(tmp_path):
    person = "5eed00000000000000000000000000000000000000000000000000000000ffff"
    cmap = json.loads((FIXTURES / "cultural_map.json").read_text(encoding="utf-8")) + [
        {"item_id": person, "kind": "creator", "canonical_key": "tiktok:fixture_someone",
         "label": "@fixture_someone", "aliases": [], "status": "active", "first_seen": "2026-09-01",
         "valid_to": None}]
    rows = [{"seed_date": "2026-10-01", "market": "KE", "item_id": person, "query": "fixture someone words",
             "kind": "topic", "lane": "exploration", "priority": 0.6, "template": "search/multi", "ttl_days": 1,
             "credits_estimate": 5.0, "yield_posts": None, "yield_new_creators": None},
            {"seed_date": "2026-10-01", "market": "KE", "item_id": None, "query": "@fixture_atname",
             "kind": "topic", "lane": "exploration", "priority": 0.5, "template": "search/multi", "ttl_days": 1,
             "credits_estimate": 5.0, "yield_posts": None, "yield_new_creators": None},
            {"seed_date": "2026-10-01", "market": "KE", "item_id": None, "query": "fixture_ig_account",
             "kind": "topic", "lane": "exploration", "priority": 0.4, "template": "instagram/profile/posts",
             "ttl_days": 1, "credits_estimate": 1.0, "yield_posts": None, "yield_new_creators": None}]
    store = copy_fixtures(tmp_path, cultural_map=cmap, seed_queue=rows)
    body = seeds.build_seeds(store, "KE")
    text = json.dumps(body)
    assert body["queue"]["seeds"] == [] and body["queue"]["creator_accounts"]["seeds"]["value"] == 3
    assert "fixture_someone" not in text and "fixture someone" not in text and person not in text
    assert "fixture_atname" not in text and "fixture_ig_account" not in text


def test_a_market_with_nothing_queued_says_so_plainly():
    ke = seeds.build_seeds(FixtureStore(), "KE")
    assert ke["status"] == "empty" and ke["queue"] is None and ke["results"] is None
    assert "Kenya" in ke["message"] and "morning" in ke["message"]


def test_not_ready_without_the_table(tmp_path):
    store = copy_fixtures(tmp_path, drop=("seed_queue",))
    body = seeds.build_seeds(store, "ZA")
    assert body["status"] == "not_ready" and body["queue"] is None and body["results"] is None
    assert body["message"]


def test_a_cut_list_says_so(monkeypatch):
    monkeypatch.setattr(seeds, "ROW_LIMIT", 2)
    q = seeds.build_seeds(FixtureStore(), "ZA")["queue"]
    assert q["truncated"] is True and len(q["seeds"]) <= 2
    assert any("first 2" in n for n in seeds.build_seeds(FixtureStore(), "ZA")["notes"])


@pytest.mark.parametrize("market", [None, "", "US", "ALL", "za ng"])
def test_bad_markets(market):
    with pytest.raises(BadRequest):
        seeds.build_seeds(FixtureStore(), market)


def test_a_lower_case_market_is_read():
    assert seeds.build_seeds(FixtureStore(), " za ")["market"] == "ZA"


def test_the_platform_words_follow_the_seed_templates():
    from core.detect import seeds as detect_seeds

    assert seeds.MULTI_PLATFORMS == detect_seeds.MULTI_PLATFORMS
    assert seeds.NEWS_PLATFORMS == detect_seeds.NEWS_PLATFORMS
    assert set(detect_seeds.CREATOR_ROUTES.values()) <= seeds.PROFILE_ROUTES


# The fixture store

def test_fixture_store_returns_queued_and_yield_rows_without_the_control():
    rows = FixtureStore().seed_queue("ZA", "2026-07-01", "2026-10-20", 500)
    assert {r["grain"] for r in rows} == {"queued", "yield"}
    assert all(r["lane"] != "placebo" for r in rows)
    assert {r["seed_date"] for r in rows if r["grain"] == "queued"} == {"2026-10-01"}
    assert {r["seed_date"] for r in rows if r["grain"] == "yield"} == {"2026-09-30"}
    heritage = [r for r in rows if r["grain"] == "yield" and r["item_id"] == HERITAGE]
    assert len(heritage) == 1 and heritage[0]["yield_posts"] == 14 and heritage[0]["yield_new_creators"] == 3


def test_fixture_store_is_none_without_the_table(tmp_path):
    assert copy_fixtures(tmp_path, drop=("seed_queue",)).seed_queue("ZA", "2026-07-01", "2026-10-20", 5) is None


# BigQuery: parameterised, capped, bounded, the control left out.

CATALOG = [{"ds": "intelligence_42_core", "n": n, "what": "table"} for n in ("seed_queue", "cultural_map")]


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    def __init__(self, catalog):
        self.catalog, self.calls = catalog, []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.catalog if "INFORMATION_SCHEMA" in sql else [])


@pytest.fixture(autouse=True)
def fresh_catalog(monkeypatch):
    monkeypatch.setattr(store_mod, "_CATALOG", {})


def test_bigquery_seed_queue_sql_is_parameterised_capped_and_bounded():
    client = FakeClient(CATALOG)
    assert BigQueryStore(client=client).seed_queue("ZA", "2026-07-05", "2026-10-17", 301) == []
    sql, cfg = next((s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s)
    assert cfg.maximum_bytes_billed == MAX_BYTES
    params = {p.name: getattr(p, "value", None) for p in cfg.query_parameters}
    assert params["market"] == "ZA" and params["limit"] == 301
    assert str(params["since"]) == "2026-07-05" and str(params["until"]) == "2026-10-17"
    assert "'ZA'" not in sql and "q.market = @market" in sql
    assert re.search(r"q\.seed_date BETWEEN @since AND @until", sql)
    assert sql.count("LIMIT @limit") == 2
    assert "IFNULL(q.lane, '') != 'placebo'" in sql
    assert "MAX(q.yield_posts)" in sql and "MAX(q.yield_new_creators)" in sql
    assert not re.search(r"\b(INSERT|UPDATE|MERGE|CREATE|DELETE|DROP|ALTER)\b", sql)


def test_bigquery_seed_queue_is_none_until_the_table_exists():
    client = FakeClient([r for r in CATALOG if r["n"] != "seed_queue"])
    assert BigQueryStore(client=client).seed_queue("ZA", "2026-07-05", "2026-10-17", 10) is None


def test_the_bigquery_path_never_returns_fixture_rows():
    client = FakeClient(CATALOG)
    body = seeds.build_seeds(BigQueryStore(client=client), "ZA")
    assert body["status"] == "empty" and "fixture" not in json.dumps(body).lower()


# The route

@pytest.fixture
def client(monkeypatch):
    from core.api import app as api_mod
    from core.api import auth

    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.setenv("F42_DATA", "fixtures")
    for name in ("F42_AUTH_MODE", "IAP_AUDIENCE", "IAP_ALLOWED_EMAILS", "LP_ALLOW_OPEN_GATE"):
        monkeypatch.delenv(name, raising=False)
    auth.auth_limiter.hits.clear()
    return TestClient(api_mod.app)


def test_route_returns_the_queue_behind_the_passcode(client):
    assert client.get("/api/seeds?market=ZA").status_code == 401
    r = client.get("/api/seeds?market=ZA", headers={"X-Passcode": PASS})
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["queue"]["seeds"]
    text = r.text
    assert not any(h in text for h in HANDLES) and "placebo" not in text


def test_route_answers_a_bad_market_with_400(client):
    for path in ("/api/seeds", "/api/seeds?market=GB"):
        r = client.get(path, headers={"X-Passcode": PASS})
        assert r.status_code == 400 and r.json()["error"] == "bad_request"


# Review, 3 October 2026: the queue says whether its day is still to come, and an unrecorded yield is not a zero.

@pytest.mark.parametrize("today,when", [("2026-09-30", "upcoming"), ("2026-10-01", "today"), ("2026-10-03", "past")])
def test_the_queue_says_whether_its_day_is_still_to_come(today, when):
    import datetime as dt
    body = seeds.build_seeds(FixtureStore(), "ZA", today=dt.date.fromisoformat(today))
    assert body["queue"]["seed_date"] == "2026-10-01" and body["queue"]["when"] == when


def test_a_yield_never_recorded_is_not_shown_as_zero():
    class NoCreators(FixtureStore):
        def seed_queue(self, *args):
            rows = super().seed_queue(*args)
            return None if rows is None else [dict(r, yield_new_creators=None) for r in rows]

    results = seeds.build_seeds(NoCreators(), "ZA")["results"]
    assert results["totals"]["new_creators"] is None
    assert all(lane["new_creators"] is None for lane in results["lanes"])
    assert results["totals"]["posts"]["value"] > 0
