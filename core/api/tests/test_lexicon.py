"""Lexicon (contract.md section 20) over the fixture store, the BigQuery SQL it runs and its route."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from core.api import lexicon
from core.api import store as store_mod
from core.api.discover import BadRequest
from core.api.store import FIXTURES, MAX_BYTES, BigQueryStore, FixtureStore

PASS = "lexicon-passcode"
SHARP = "64739b91332b834ad7018d3d5d050508f0659cec60a61f6a679a37e757a4c3d5"
AMAPIANO = "dc421a437e7bc5b4dc9567815ed539301ce2a133ae84ded9986fb5cdc667eed3"
EISH = "66c02f8db1294a76141d9d7fbd75713840e605889a2f9276699300c8dd062805"
FYP = "023b761cb6a7a01ef425f0293b629e43ca8dcaaf12c09f7b3bb817fc8c725ad9"
VIRAL = "001c600240c06dd3cf222d8f7e38b8523a6e86ac2d3b656215881534cf7e29b9"
CREATOR = "ffef2f462e2d6ba8e94ca8c7bb6202eeadd9a53ffe7559b23061ce12b7515b0f"
TOPIC = "6d6fe31b0dfce706c190d9d7e0acd932386400c1ec8c14dfe14b9faec87bea51"
REJECTED = "70ae5b2652d77b67b76b0a15d258365799d5cb25f9e4d1ab8e6a05c66300acd2"
WAHALA = "6ff5dd66a07fc42d03f622d543c1884d6d868788ea6419a6ab52d5d83316957a"


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


class Collected(FixtureStore):
    """The fixture store with collection running since August, so the earlier week is past warm-up."""

    def first_ok_collect_date(self):
        return "2026-08-01"


@pytest.fixture
def za():
    return lexicon.build_lexicon(Collected(), "ZA")


def by_id(body):
    return {t["item_id"]: t for t in body["terms"]}


# The builder over the fixture store

def test_the_window_ends_on_the_latest_good_aggregate_run(za):
    assert za["status"] == "ok" and za["market"] == "ZA" and za["market_name"] == "South Africa"
    assert za["query_id"] == "q_lexicon" and za["run_id"] == "r_aggregate_20260930_01"
    assert za["window"] == {"from": "2026-09-03", "to": "2026-09-30", "days": 28,
                            "week": {"from": "2026-09-24", "to": "2026-09-30"},
                            "prior_week": {"from": "2026-09-17", "to": "2026-09-23"}}
    assert za["kinds"] == ["meme", "hashtag"]


def test_terms_are_the_markets_words_and_hashtags_by_posts(za):
    got = [(t["label"], t["kind"], t["posts"]["value"], t["week_posts"]["value"], t["prior_week_posts"]["value"])
           for t in za["terms"]]
    # sharp sharp: 4 + 8 + 12; the 30 August row is outside the window, the _any row and the smaller lane class on
    # 25 September are not added. amapiano: the placebo search_presence count of 26 September is dropped.
    assert got == [("fixture sharp sharp", "meme", 24, 12, 8),
                   ("#fixtureamapiano", "hashtag", 16, 6, 10),
                   ("fixture eish wena", "meme", 3, 3, 0)]


def test_no_creator_topic_generic_stoplisted_rejected_or_closed_row_is_listed(za):
    ids = set(by_id(za))
    assert not ids & {FYP, VIRAL, CREATOR, TOPIC, REJECTED}
    assert all(t["kind"] in ("meme", "hashtag") for t in za["terms"])
    assert "old fixture label" not in json.dumps(za)


def test_week_change_is_a_percentage_only_against_a_week_with_posts(za):
    terms = by_id(za)
    assert terms[SHARP]["change"] == {"percent": 50, "reason": None, "text": "Up 50% on the week before"}
    assert terms[AMAPIANO]["change"] == {"percent": -40, "reason": None, "text": "Down 40% on the week before"}
    eish = terms[EISH]["change"]
    assert eish["percent"] is None and eish["reason"] == "no_earlier_posts" and eish["text"]
    assert "%" not in eish["text"]


def test_week_change_is_null_while_collection_is_warming_up():
    body = lexicon.build_lexicon(FixtureStore(), "ZA")  # the fixture collect runs start on 29 September
    assert body["terms"]
    for t in body["terms"]:
        assert t["change"]["percent"] is None and t["change"]["reason"] == "warming_up"
        assert "%" not in t["change"]["text"]
    assert any("two full weeks" in n for n in body["notes"])


def test_each_term_links_to_its_seed_path_and_topic(za):
    terms = by_id(za)
    assert terms[AMAPIANO]["seed_term"] == "fixtureamapiano"
    assert terms[SHARP]["seed_term"] == "fixture sharp sharp"
    assert terms[SHARP]["topic_href"] == f"#/t/{SHARP}?market=ZA"
    assert terms[SHARP]["first_seen"] == "2026-08-28"


def test_every_number_carries_the_query_id(za):
    found = list(figures(za))
    assert len(found) == 9
    assert all(f["query_id"] == "q_lexicon" and f["run_id"] == "r_aggregate_20260930_01" for f in found)
    assert all(f["result_hash"].startswith("sha256:") for f in found)


def test_another_market_counts_only_its_own_days():
    ng = lexicon.build_lexicon(Collected(), "ng")
    got = [(t["item_id"], t["posts"]["value"], t["change"]["percent"]) for t in ng["terms"]]
    assert got == [(WAHALA, 6, 100), (SHARP, 2, None)]


def test_a_market_with_no_terms_is_an_honest_empty_answer():
    ke = lexicon.build_lexicon(Collected(), "KE")
    assert ke["status"] == "empty" and ke["terms"] == [] and ke["message"]


@pytest.mark.parametrize("market", [None, "", "all", "GB", "za ng"])
def test_bad_markets(market):
    with pytest.raises(BadRequest):
        lexicon.build_lexicon(FixtureStore(), market)


def test_no_aggregate_run_is_not_ready(tmp_path):
    store = copy_fixtures(tmp_path, drop=("runs_compare",))
    with pytest.raises(lexicon.NotReady):
        lexicon.build_lexicon(store, "ZA")


def test_missing_daily_counts_are_a_not_ready_answer_not_a_crash(tmp_path):
    store = copy_fixtures(tmp_path, drop=("lexicon_item_days",))
    body = lexicon.build_lexicon(store, "ZA")
    assert body["status"] == "not_ready" and body["terms"] == [] and body["message"]


def test_a_store_row_of_a_hidden_kind_never_leaves_the_builder():
    class Leaky(Collected):
        def lexicon_terms(self, *args):
            rows = super().lexicon_terms(*args)
            return rows + [dict(rows[0], item_id=CREATOR, kind="creator", label="fixture person"),
                           dict(rows[0], item_id=FYP, kind="hashtag", label="#FYP", canonical_key="fyp"),
                           dict(rows[0], item_id=VIRAL, status="generic")]

    body = lexicon.build_lexicon(Leaky(), "ZA")
    assert not set(by_id(body)) & {CREATOR, FYP, VIRAL}


def test_the_term_cap_is_said(monkeypatch):
    monkeypatch.setattr(lexicon, "TERM_LIMIT", 2)
    body = lexicon.build_lexicon(Collected(), "ZA")
    assert len(body["terms"]) == 2 and body["truncated"] is True
    assert any("2 most" in n for n in body["notes"])


def test_dropped_rows_do_not_make_the_list_read_as_cut(monkeypatch):
    # Review, 3 October 2026: generic rows the builder drops never count toward the cap note.
    class Padded(Collected):
        def lexicon_terms(self, *args):
            rows = super().lexicon_terms(*args)
            return rows + [dict(rows[0], item_id=f"{i:064x}", status="generic") for i in range(2)]

    shown = len(lexicon.build_lexicon(Collected(), "ZA")["terms"])
    monkeypatch.setattr(lexicon, "TERM_LIMIT", shown)
    body = lexicon.build_lexicon(Padded(), "ZA")
    assert len(body["terms"]) == shown and body["truncated"] is False
    assert not any("most" in n for n in body["notes"])


# The fixture store read

def test_fixture_store_reads_only_open_rows_of_the_asked_kinds():
    rows = FixtureStore().lexicon_terms("ZA", ("meme", "hashtag"), "2026-09-03", "2026-09-30", "2026-09-24",
                                        "2026-09-17", 10)
    assert {r["item_id"] for r in rows} == {SHARP, AMAPIANO, EISH, FYP}
    sharp = next(r for r in rows if r["item_id"] == SHARP)
    assert (sharp["posts_window"], sharp["posts_week"], sharp["posts_prior_week"]) == (24, 12, 8)
    assert sharp["label"] == "fixture sharp sharp" and sharp["first_seen"] == "2026-08-28"
    assert [r["posts_window"] for r in rows] == sorted((r["posts_window"] for r in rows), reverse=True)


def test_fixture_store_is_none_without_its_daily_counts(tmp_path):
    store = copy_fixtures(tmp_path, drop=("lexicon_item_days",))
    assert store.lexicon_terms("ZA", ("meme",), "2026-09-03", "2026-09-30", "2026-09-24", "2026-09-17", 10) is None


def test_fixture_labels_carry_the_fixture_marker():
    for name in ("lexicon_cultural_map",):
        for row in json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")):
            assert "fixture" in row["label"].lower()


# BigQuery: parameterised, capped, the item_days counting rule.

CATALOG = [{"ds": "intelligence_42_core", "n": n, "what": "table"}
           for n in ("cultural_map", "v_item_daily_current", "seed_queue", "v_good_runs")]


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


def test_bigquery_lexicon_sql_is_parameterised_bounded_and_counts_as_item_days():
    client = FakeClient(CATALOG)
    got = BigQueryStore(client=client).lexicon_terms("ZA", ("meme", "hashtag"), "2026-09-03", "2026-09-30",
                                                      "2026-09-24", "2026-09-17", 101)
    assert got == []
    sql, cfg = next((s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s)
    assert cfg.maximum_bytes_billed == MAX_BYTES
    params = {p.name: getattr(p, "value", getattr(p, "values", None)) for p in cfg.query_parameters}
    assert params["market"] == "ZA" and params["limit"] == 101 and params["kinds"] == ["meme", "hashtag"]
    assert "LIMIT @limit" in sql and "i.metric_date BETWEEN @start AND @end" in sql
    assert "i.lane_class != '_any'" in sql and "q.lane = 'placebo'" in sql and "MAX(lc.posts)" in sql
    assert "cm.valid_to IS NULL" in sql and "cm.kind IN UNNEST(@kinds)" in sql and "cm.status = 'active'" in sql
    assert "ZA" not in sql and "meme" not in sql
    assert not re.search(r"\b(INSERT|UPDATE|MERGE|CREATE|DELETE|DROP|ALTER)\b", sql)


def test_bigquery_lexicon_without_seed_queue_still_reads():
    client = FakeClient([r for r in CATALOG if r["n"] != "seed_queue"])
    assert BigQueryStore(client=client).lexicon_terms("ZA", ("meme",), "2026-09-03", "2026-09-30", "2026-09-24",
                                                      "2026-09-17", 10) == []
    sql = next(s for s, _c in client.calls if "INFORMATION_SCHEMA" not in s)
    assert "placebo" not in sql


@pytest.mark.parametrize("missing", ["cultural_map", "v_item_daily_current"])
def test_bigquery_lexicon_is_none_until_its_views_exist(missing):
    client = FakeClient([r for r in CATALOG if r["n"] != missing])
    assert BigQueryStore(client=client).lexicon_terms("ZA", ("meme",), "2026-09-03", "2026-09-30", "2026-09-24",
                                                      "2026-09-17", 10) is None
    assert all("INFORMATION_SCHEMA" in s for s, _c in client.calls)


def test_the_bigquery_path_never_returns_fixture_rows():
    class Bq(BigQueryStore):
        def latest_aggregate_run(self):
            return {"run_id": "r1", "run_date": "2026-09-30"}

        def first_ok_collect_date(self):
            return "2026-08-01"

    body = lexicon.build_lexicon(Bq(client=FakeClient(CATALOG)), "ZA")
    assert body["status"] == "empty" and body["terms"] == []
    assert "fixture" not in json.dumps(body).lower()


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


def test_route_returns_the_lexicon_behind_the_passcode(client):
    assert client.get("/api/lexicon?market=ZA").status_code == 401
    r = client.get("/api/lexicon?market=za", headers={"X-Passcode": PASS})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["terms"] and body["query_id"] == "q_lexicon"
    assert not any(t["kind"] == "creator" for t in body["terms"])


@pytest.mark.parametrize("query", ["", "?market=", "?market=all", "?market=GB"])
def test_route_answers_a_bad_market_with_400(client, query):
    r = client.get("/api/lexicon" + query, headers={"X-Passcode": PASS})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_route_answers_409_before_any_aggregate_run(client, monkeypatch):
    monkeypatch.setattr(FixtureStore, "latest_aggregate_run", lambda self: None)
    r = client.get("/api/lexicon?market=ZA", headers={"X-Passcode": PASS})
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
