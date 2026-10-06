"""Seed path (contract.md section 18) over the fixture store, the BigQuery SQL it runs and its route."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from core.api import seedpath
from core.api import store as store_mod
from core.api.discover import BadRequest
from core.api.store import FIXTURES, MAX_BYTES, BigQueryStore, FixtureStore

PASS = "seed-path-passcode"
STEP = "7107ad863306852108ebb88392f1d5e0293922af5f5e7a5a4b81d466930f646e"
HERITAGE = "afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156"


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
def amapiano():
    return seedpath.build_seed_path(FixtureStore(), "amapiano", "ZA")


def test_first_sighting_per_platform_in_order_with_the_market_rules(amapiano):
    b = amapiano
    assert b["status"] == "ok" and b["keyword"] == "amapiano" and b["market"] == "ZA"
    assert b["window"] == {"from": "2026-09-03", "to": "2026-09-30", "days": 28}
    got = [(p["platform"], p["first_seen"]["value"], p["before_window"]) for p in b["platforms"]]
    # TikTok's first sighting is older than the window; the legacy-only Instagram post of 2 September and the
    # Nigerian post never count in South Africa; twitter is named x.
    assert got == [("tiktok", "2026-08-25", True), ("x", "2026-09-09", False),
                   ("instagram", "2026-09-15", False), ("youtube", "2026-09-21", False)]
    by = {p["platform"]: p for p in b["platforms"]}
    # "amapianos" is not the word: TikTok has six matching posts all time, five of them in the window.
    assert by["tiktok"]["posts_all_time"]["value"] == 6 and by["tiktok"]["posts_in_window"]["value"] == 5
    # A post whose only match is its hashtags list counts (Instagram, 29 September).
    assert by["instagram"]["posts_in_window"]["value"] == 3
    assert by["youtube"]["posts_in_window"]["value"] == 1  # matched in the transcript
    assert b["matched_posts"]["value"] == 12


def test_daily_counts_cover_the_window_and_add_up(amapiano):
    for p in amapiano["platforms"]:
        pts = p["daily"]["points"]
        assert p["daily"]["query_id"] == "q_seed_path" and p["daily"]["run_id"] == amapiano["run_id"]
        assert len(pts) == 28 and pts[0]["date"] == "2026-09-03" and pts[-1]["date"] == "2026-09-30"
        assert sum(x["posts"] for x in pts) == p["posts_in_window"]["value"]


def test_every_number_carries_the_query_id(amapiano):
    found = list(figures(amapiano))
    assert len(found) > 10
    assert all(f["query_id"] == "q_seed_path" and f["run_id"] == "r_aggregate_20260930_01" for f in found)
    assert all(f["result_hash"].startswith("sha256:") for f in found)


def test_a_hash_and_any_case_trace_the_same_term(amapiano):
    again = seedpath.build_seed_path(FixtureStore(), "  #AmaPiano ", "za")
    assert again == amapiano


def test_side_words_need_two_posts_and_drop_mentions_stopwords_and_the_term(amapiano):
    words = {w["word"]: w["posts"]["value"] for w in amapiano["side_words"]}
    assert words == {"dance": 4, "drum": 4, "log": 4, "weekend": 4, "braai": 3, "challenge": 2, "soweto": 2}
    assert [w["word"] for w in amapiano["side_words"]][:4] == ["dance", "drum", "log", "weekend"]
    assert amapiano["side_words_min_posts"] == 2


def test_topics_are_the_markets_42_items_with_topic_links(amapiano):
    topics = [(t["item_id"], t["posts"]["value"]) for t in amapiano["topics"]]
    assert topics[:2] == [(STEP, 3), (HERITAGE, 2)]
    for t in amapiano["topics"]:
        assert t["href"] == f"#/t/{t['item_id']}?market=ZA" and t["title"] and t["kind"] != "creator"


def test_no_handle_or_post_text_leaves_the_api(amapiano):
    text = json.dumps(amapiano)
    assert "fixture_dj" not in text and "handle" not in text and "c_sp" not in text and "sp0" not in text


def test_another_market_reads_only_its_own_sightings():
    b = seedpath.build_seed_path(FixtureStore(), "amapiano", "NG")
    assert [(p["platform"], p["first_seen"]["value"]) for p in b["platforms"]] == [("tiktok", "2026-09-20")]
    assert b["side_words"] == []  # one post cannot give a word two posts


def test_no_match_is_an_honest_empty_result():
    b = seedpath.build_seed_path(FixtureStore(), "nothinglikethis", "KE")
    assert b["status"] == "no_match" and b["platforms"] == [] and b["side_words"] == [] and b["topics"] == []
    assert b["matched_posts"] is None and "Kenya" in b["message"]


@pytest.mark.parametrize("keyword,market", [("", "ZA"), ("a", "ZA"), ("#", "ZA"), ("x" * 61, "ZA"),
                                            ("amapiano", "US"), ("amapiano", None), (None, "ZA")])
def test_bad_requests(keyword, market):
    with pytest.raises(BadRequest):
        seedpath.build_seed_path(FixtureStore(), keyword, market)


def test_not_ready_without_posts_or_an_aggregate_run(tmp_path):
    (tmp_path / "a").mkdir()
    no_posts = copy_fixtures(tmp_path / "a", drop=("post_observations",))
    b = seedpath.build_seed_path(no_posts, "amapiano", "ZA")
    assert b["status"] == "not_ready" and b["platforms"] == [] and b["message"]
    (tmp_path / "b").mkdir()
    no_run = copy_fixtures(tmp_path / "b", drop=("runs_compare",))
    assert seedpath.build_seed_path(no_run, "amapiano", "ZA")["status"] == "not_ready"


def test_a_refused_read_says_the_term_is_too_broad():
    class Capped(FixtureStore):
        def seed_path_posts(self, *a):
            raise RuntimeError("Query exceeded limit for bytes billed: 2000000000.")

    b = seedpath.build_seed_path(Capped(), "amapiano", "ZA")
    assert b["status"] == "too_large" and b["platforms"] == []


def test_other_failures_are_not_swallowed():
    class Broken(FixtureStore):
        def seed_path_posts(self, *a):
            raise RuntimeError("Not found: Table posts")

    with pytest.raises(RuntimeError):
        seedpath.build_seed_path(Broken(), "amapiano", "ZA")


def test_the_row_cap_is_said(monkeypatch):
    monkeypatch.setattr(seedpath, "ROW_LIMIT", 3)
    b = seedpath.build_seed_path(FixtureStore(), "amapiano", "ZA")
    assert b["truncated"] is True and b["matched_posts"]["value"] == 3
    assert any("first 3 matching posts" in n for n in b["notes"])


# BigQuery: parameterised, capped, the market's sighting rule.

CATALOG = [{"ds": "intelligence_42_core", "n": n, "what": "table"}
           for n in ("posts", "post_observations", "post_items", "v_good_runs")]


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


def test_bigquery_seed_path_sql_is_parameterised_and_capped():
    client = FakeClient(CATALOG)
    assert BigQueryStore(client=client).seed_path_posts("ZA", ["o'neill"], "2026-09-03", "2026-09-30", 5001) == []
    sql, cfg = next((s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "o'neill" not in sql and "neill" not in sql  # the term only ever travels as a parameter
    assert cfg.maximum_bytes_billed == MAX_BYTES
    params = {p.name: getattr(p, "value", getattr(p, "values", None)) for p in cfg.query_parameters}
    assert params["market"] == "ZA" and params["limit"] == 5001 and params["tags"] == ["o'neill"]
    assert "@pattern" in sql and "@tags" in sql and "LIMIT @limit" in sql
    assert "po.lane_class != 'legacy'" in sql and "IFNULL(po.lane, '') NOT IN ('legacy', 'agent_live')" in sql
    assert re.search(r"h\.day BETWEEN @start AND @end", sql)
    assert not re.search(r"\b(INSERT|UPDATE|MERGE|CREATE|DELETE|DROP|ALTER)\b", sql)


def test_bigquery_seed_path_is_none_until_the_tables_exist():
    client = FakeClient([r for r in CATALOG if r["n"] != "post_items"])
    assert BigQueryStore(client=client).seed_path_posts("ZA", ["amapiano"], "2026-09-03", "2026-09-30", 10) is None


def test_the_re2_pattern_matches_whole_words_only():
    pattern = re.compile(store_mod._keyword_re2(["amapiano"]).replace("\\p{L}\\p{N}", "\\w"))
    assert pattern.search("new #amapiano mix") and pattern.search("Amapiano!")
    assert not pattern.search("amapianos everywhere")


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


def test_route_returns_the_trace_behind_the_passcode(client):
    assert client.get("/api/seed-path?keyword=amapiano&market=ZA").status_code == 401
    r = client.get("/api/seed-path?keyword=%23amapiano&market=ZA", headers={"X-Passcode": PASS})
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["platforms"]


def test_route_answers_a_bad_request_with_400(client):
    r = client.get("/api/seed-path?keyword=a&market=ZA", headers={"X-Passcode": PASS})
    assert r.status_code == 400
    assert client.get("/api/seed-path?keyword=amapiano&market=GB", headers={"X-Passcode": PASS}).status_code == 400
