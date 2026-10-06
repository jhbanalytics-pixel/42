"""History (contract.md section 12.4) over the fixture store, and the SQL it runs.

The fixtures (core/api/fixtures/history_*.json) add waves, daily wave posts, stored centroids, two finished Ask
runs and three findings to the Stage 2 fixtures. The latest good detect run is 30 September 2026.
"""
import re
import shutil

import pytest

import json

from core.api import history
from core.api import today as today_mod
from core.api import store as store_mod
from core.api.discover import BadRequest, NotReady
from core.api.store import FIXTURES, BigQueryStore, FixtureStore
from core.api.today import NotFound

STEP = "7107ad863306852108ebb88392f1d5e0293922af5f5e7a5a4b81d466930f646e"
HER = "afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156"
BOARD = "447e6354b5e51acf11942783fda98f72ce4003b53c18f62d2e35753c3c44ade3"
TOPIC = "4b942b2ea393cbe2c2e4e883ed5be7441b361229e446c6c5bbcb7fdba540fa33"
HELD = "196c5995cccbc7eff97f244b604378afbc28dc4109d809a6d6e3cf7a4201bd97"
KE_TOPIC = "7f4c4b0a0f6c1e36013ef91407c68fc75dac4c06f10de9d3df6a5d04179b3429"
RUN = "r_detect_20260930_01"


def is_figure(f):
    assert set(f) == {"value", "unit", "query_id", "run_id", "result_hash"}
    assert f["query_id"].startswith("q_") and re.fullmatch(r"sha256:[0-9a-f]{64}", f["result_hash"])


def value(f):
    return None if f is None else f["value"]


@pytest.fixture
def fx():
    return FixtureStore()


def first_admitted_card():
    for r in json.loads((FIXTURES / "briefs.json").read_text(encoding="utf-8")):
        for card in (r.get("payload") or {}).get("cards") or []:
            if today_mod._today_card_admissible(card):
                return card
    raise AssertionError("no fixture card Today admits")


def without(tmp_path, *names):
    for f in FIXTURES.glob("*.json"):
        if f.stem not in names:
            shutil.copy(f, tmp_path / f.name)
    return FixtureStore(root=tmp_path)


# Item history.

def test_item_waves_newest_first_with_days_above_half_the_peak(fx):
    body = history.build_history_item(fx, HER, "ZA")
    assert body["item_id"] == HER and body["market"] == "ZA" and body["label"] == "#fixture_za_heritage"
    assert body["as_of"] == "2026-09-30"
    waves = body["waves"]
    assert [w["peak_date"] for w in waves] == ["2026-09-29", "2025-09-24"]
    assert [value(w["peak_posts"]) for w in waves] == [51, 240]
    # 2025: 240, 200 and 150 are at or above 120; 2026: 51 and 40 are at or above 25.5.
    assert [value(w["above_half_days"]) for w in waves] == [2, 3]
    assert [w["completed"] for w in waves] == [False, True]
    for w in waves:
        is_figure(w["peak_posts"])
        is_figure(w["above_half_days"])
    assert value(body["recurrences"]) == 1
    assert body["current_day"] == 18  # 12 to 30 September


def test_analogues_are_the_nearest_centroids_with_a_completed_wave_in_the_market(fx):
    body = history.build_history_item(fx, STEP, "ZA")
    assert body["current_day"] == 4  # its wave started 26 September
    got = [a["item_id"] for a in body["analogues"]]
    # KE_TOPIC is nearer but has no wave in South Africa; HELD's wave is still open.
    assert got == [HER, BOARD, TOPIC]
    her, board, topic = body["analogues"]
    assert her["wave"] == {"start": "2025-09-10", "peak_date": "2025-09-24", "end": "2025-10-02"}
    assert (value(her["days_to_peak"]), value(her["peak_posts"]), value(her["days_to_fade"])) == (10, 240, 13)
    assert (value(board["days_to_peak"]), value(board["peak_posts"]), value(board["days_to_fade"])) == (3, 80, 4)
    assert value(topic["days_to_peak"]) == -4 and topic["days_to_fade"] is None  # no daily rows for that wave
    for a in body["analogues"]:
        assert a["from_day"] == 4
        for key in ("days_to_peak", "peak_posts"):
            is_figure(a[key])
    assert body["note"] is None


def test_analogues_are_null_until_centroids_exist(fx, tmp_path):
    body = history.build_history_item(without(tmp_path, "history_centroids"), STEP, "ZA")
    assert body["analogues"] is None and body["note"] == "Analogues need 42's cultural map"
    body = history.build_history_item(fx, "d446222d56050176e27da32b1f13586cb9ca5359066bacbee8b56e83a8522593", "NG")
    assert body["analogues"] is None and body["note"] == "Analogues need 42's cultural map"


def test_item_history_errors(fx, tmp_path):
    with pytest.raises(NotFound):
        history.build_history_item(fx, "0" * 64, "ZA")
    with pytest.raises(BadRequest):
        history.build_history_item(fx, STEP, "all")
    with pytest.raises(NotReady):
        history.build_history_item(without(tmp_path, "runs_v2"), STEP, "ZA")


def test_waves_are_null_until_the_view_exists(tmp_path):
    body = history.build_history_item(without(tmp_path, "item_waves", "history_waves"), HER, "ZA")
    assert body["waves"] is None and body["recurrences"] is None


# Search.

def test_search_matches_label_or_alias_in_any_case_with_waves(fx):
    body = history.build_history_search(fx, "HERITAGE", None)
    assert [i["item_id"] for i in body["items"]] == [HER]
    assert [w["peak_date"] for w in body["items"][0]["waves"]] == ["2026-09-29", "2025-09-24"]
    assert [i["item_id"] for i in history.build_history_search(fx, "fixturezastep", "ZA")["items"]] == [STEP]
    za = history.build_history_search(fx, "fixture_za", "ZA")
    assert len(za["items"]) > 3 and all(w["market"] == "ZA" for i in za["items"] for w in i["waves"] or [])
    assert history.build_history_search(fx, "no such thing", None)["items"] == []


@pytest.mark.parametrize("q,market", [("", None), ("a", None), ("x" * 101, None), ("heritage", "XX")])
def test_search_arguments(fx, q, market):
    with pytest.raises(BadRequest):
        history.build_history_search(fx, q, market)


# Asks, briefs and findings.

def test_asks_newest_first_with_a_cursor(fx):
    body = history.build_history_asks(fx, limit=2)
    assert [a["ask_id"] for a in body["asks"]] == ["a_20260930_fixture_failed", "a_20260930_h2"]
    assert body["asks"][1] == {"ask_id": "a_20260930_h2", "question": "Where did #fixture_za_heritage start?",
                               "at": "2026-09-30T08:00:00+02:00", "status": "complete", "answer_status": "partial",
                               "market": "ZA"}  # market added 4 October 2026 so History can show and filter it
    assert body["asks"][0]["answer_status"] is None
    more = history.build_history_asks(fx, limit=2, before=body["next_before"])
    assert [a["ask_id"] for a in more["asks"]] == ["a_20260929_h1"] and more["next_before"] is None
    for bad in ({"limit": 0}, {"limit": 101}, {"before": "yesterday"}):
        with pytest.raises(BadRequest):
            history.build_history_asks(fx, **bad)


def test_briefs_by_date_with_every_market_and_status(fx):
    body = history.build_history_briefs(fx, limit=1)
    plain = [{k: m[k] for k in ("market", "status", "published_at")} for m in body["dates"][0]["markets"]]
    assert body["dates"][0]["date"] == "2026-09-30" and len(body["dates"]) == 1
    assert plain == [
        {"market": "ZA", "status": "published", "published_at": "2026-09-30T06:14:40+02:00"},
        {"market": "NG", "status": "published", "published_at": "2026-09-30T05:13:10+01:00"},
        {"market": "KE", "status": "data_issue", "published_at": "2026-09-30T07:10:00+03:00"}]
    assert body["next_before"] == "2026-09-30"
    more = history.build_history_briefs(fx, limit=30, before="2026-09-30")
    assert [d["date"] for d in more["dates"]] == ["2026-09-29"] and more["next_before"] is None
    with pytest.raises(BadRequest):
        history.build_history_briefs(fx, before="30 Sept")


def test_briefs_count_what_today_shows_and_holds_zero_card_briefs_included(fx):
    """Staging, 1 to 3 October 2026: every brief held every item. Such a brief is still a brief date, and History
    says how many cards Today showed of it and how many items it held back, counted as Today counts them."""
    body = history.build_history_briefs(fx)
    counts = {(d["date"], m["market"]): (value(m["cards"]), value(m["held"])) for d in body["dates"]
              for m in d["markets"]}
    for date in ("2026-09-30", "2026-09-29"):
        for m in today_mod.build_today(fx, date)["markets"]:
            if (date, m["market"]) in counts:
                assert counts[(date, m["market"])] == (len(m["cards"]) + len(m["more"]), m["held_back"]["count"])
    assert counts[("2026-09-29", "ZA")] == (0, 7) and counts[("2026-09-29", "NG")] == (0, 5)
    for d in body["dates"]:
        for m in d["markets"]:
            is_figure(m["cards"])
            is_figure(m["held"])
            assert m["cards"]["query_id"] == m["held"]["query_id"] == "q_history_briefs"
            assert m["cards"]["run_id"].startswith("r_brief_")



def test_briefs_count_a_card_on_a_suppressed_persons_post_as_held_as_today_does(fx, monkeypatch):
    """A card whose evidence includes a suppressed creator's post is held on Today; History counts it the same."""
    hidden = ({"tiktok:fixture_za_6"}, set(), set())
    monkeypatch.setattr(today_mod, "hidden_people", lambda store: hidden)
    monkeypatch.setattr(history, "hidden_people", lambda store: hidden)
    body = history.build_history_briefs(fx)
    za = [m for d in body["dates"] if d["date"] == "2026-09-30" for m in d["markets"] if m["market"] == "ZA"][0]
    t = [m for m in today_mod.build_today(fx, "2026-09-30")["markets"] if m["market"] == "ZA"][0]
    assert (value(za["cards"]), value(za["held"])) == (len(t["cards"]) + len(t["more"]), t["held_back"]["count"])
    assert value(za["cards"]) == 0

class HeldEverything(FixtureStore):
    """The fixture briefs with every card moved to held_back, as staging's briefs of 1 to 3 October were."""

    def brief_history(self, limit, before):
        rows = super().brief_history(limit, before)
        for r in rows:
            p = r["payload"] or {}
            moved = [{"item_id": c["item_id"], "reason": "explanation_failed"} for c in (p.get("cards") or [])
                     + (p.get("more") or [])]
            r["payload"] = {**p, "cards": [], "more": [],
                            "held_back": {"items": list((p.get("held_back") or {}).get("items") or []) + moved}}
        return rows


def test_a_brief_that_held_every_item_is_listed_with_its_held_count():
    body = history.build_history_briefs(HeldEverything())
    za = next(m for m in body["dates"][0]["markets"] if m["market"] == "ZA")
    assert body["dates"][0]["date"] == "2026-09-30" and za["status"] == "published"
    assert value(za["cards"]) == 0 and value(za["held"]) == 9


def test_brief_counts_follow_todays_reading():
    good = first_admitted_card()
    payload = {"cards": [good, {"item_id": "x1", "explained": False}], "more": [{"item_id": "x2"}, "junk"],
               "held_back": {"items": [{"item_id": "x1", "reason": "data_issue"}, {"reason": "data_issue"}, "junk",
                                       {"item_id": "x3"}, {"item_id": "x3"}]}}
    assert today_mod.brief_counts(payload) == {"shown": [good["item_id"]], "held": ["x1", None, "x3", "x2"]}
    assert today_mod.brief_counts(None) == {"shown": [], "held": []}
    assert today_mod.brief_counts({"cards": "nope", "held_back": []}) == {"shown": [], "held": []}


def test_findings_fallback_until_l3_sets_status(fx):
    body = history.build_history_findings(fx)
    assert body["note"] == "Whether a finding still holds is not checked yet"
    assert [f["finding_id"] for f in body["findings"]] == ["f_her_1", "f_step_1"]  # newest first, current only
    step = history.build_history_findings(fx, item_id=STEP, status="stale")
    assert [f["finding_id"] for f in step["findings"]] == ["f_step_1"]  # status ignored until L3 defines it
    f = step["findings"][0]
    assert f["claims"][0]["text"] == "It started on TikTok in Soweto."
    assert [e["id"] for e in f["evidence"]] == ["p01", "p02"]
    assert f["evidence"][0]["platform"] == "tiktok" and f["evidence"][0]["flags"] == []


class WithStatus(FixtureStore):
    def findings(self, item_id=None):
        rows = super().findings(item_id)
        return [dict(r, status="stale" if r["finding_id"] == "f_her_1" else "current") for r in rows]


def test_findings_filter_by_status_once_l3_sets_it():
    body = history.build_history_findings(WithStatus(), status="current")
    assert body["note"] is None and [f["finding_id"] for f in body["findings"]] == ["f_step_1"]
    assert len(history.build_history_findings(WithStatus())["findings"]) == 2


class FlaggedPosts(FixtureStore):
    def posts_by_id(self, post_ids):
        return [dict(p, flags=["likely_coordinated", "paid_led"] if p["post_id"] == "p01" else [])
                for p in super().posts_by_id(post_ids)]


def test_findings_keep_evidence_flags():
    f = history.build_history_findings(FlaggedPosts(), item_id=STEP)["findings"][0]
    assert [(e["id"], e["flags"]) for e in f["evidence"]] == [("p01", ["likely_coordinated", "paid_led"]),
                                                               ("p02", [])]


def test_findings_null_until_the_table_exists(tmp_path):
    body = history.build_history_findings(without(tmp_path, "history_findings"))
    assert body["findings"] is None


# Nothing calls a model.

def test_nothing_calls_a_model():
    from pathlib import Path

    src = Path(history.__file__).read_text(encoding="utf-8")
    for word in ("vertexai", "genai", "generate_content", "ML.GENERATE", "tvf_search_items"):
        assert word not in src


# BigQuery.

class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    def __init__(self, catalog, rows=None):
        self.catalog, self.rows, self.calls = catalog, rows or [], []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.catalog if "INFORMATION_SCHEMA" in sql else self.rows)


@pytest.fixture(autouse=True)
def fresh_catalog(monkeypatch):
    monkeypatch.setattr(store_mod, "_CATALOG", {})


OBJECTS = ("cultural_map", "v_item_waves", "v_item_daily_current", "seed_queue", "posts", "creators",
           "v_good_runs", "v_item_state_current")


MAP_COLUMNS = ("item_id", "kind", "label", "canonical_key", "aliases", "status", "centroid", "valid_to")
FINDINGS_COLUMNS = ("finding_id", "question", "answer", "as_of", "claims", "valid_from", "valid_to", "status")


def catalog(*drop, map_columns=MAP_COLUMNS, findings_columns=FINDINGS_COLUMNS):
    rows = [{"ds": "intelligence_42_core", "n": n, "what": "table"} for n in OBJECTS if n not in drop]
    rows += [{"ds": "intelligence_42_agent", "n": n, "what": "table"} for n in ("runs", "findings", "v_briefs_current")
             if n not in drop]
    rows += [{"ds": "intelligence_42_core", "n": c, "what": "map_column"} for c in map_columns]
    rows += [{"ds": "intelligence_42_agent", "n": c, "what": "findings_column"} for c in findings_columns]
    return rows


def lacking(column, where=MAP_COLUMNS):
    return tuple(c for c in where if c != column)


def run_history_queries(bq):
    bq.waves([STEP, HER], "ZA")
    bq.item_days([STEP], "ZA", "2025-09-10", "2026-09-30")
    bq.nearest_items(STEP, 20)
    bq.search_items("heritage", 20)
    bq.ask_history(20, "2026-09-30T08:00:00+02:00")
    bq.brief_history(30, "2026-09-30")
    bq.findings(STEP)
    bq.posts_by_id(["p01"])


def test_bigquery_history_reads_are_parameterised_capped_and_read_only():
    client = FakeClient(catalog(), rows=[{"n": 1}])
    run_history_queries(BigQueryStore(client=client))
    for sql, cfg in client.calls:
        assert cfg.maximum_bytes_billed == 2_000_000_000
        assert not re.search(r"\b(INSERT|UPDATE|MERGE|CREATE|DELETE|DROP|TRUNCATE)\b", sql)
        assert "ML." not in sql and "coord_score" not in sql
        if "INFORMATION_SCHEMA" not in sql:
            for literal in (STEP, "heritage", "'ZA'", "2026-09-30"):
                assert literal not in sql
    joined = "\n".join(s for s, _ in client.calls)
    assert "VECTOR_SEARCH" in joined and "distance_type => 'COSINE'" in joined
    assert "STRPOS(LOWER(" in joined and "UNNEST(cm.aliases)" in joined
    assert "lane = 'placebo'" in joined and "lane_class != '_any'" in joined


def test_bigquery_brief_history_reads_every_status_and_only_the_counted_parts_of_the_payload():
    client = FakeClient(catalog(), rows=[{"brief_date": "2026-10-03", "market": "ZA", "run_id": "r_brief_1",
                                          "status": "published", "published_at": None,
                                          "payload": '{"cards": null, "more": null, "held_back": {"items": '
                                                     '[{"item_id": "a"}, {"item_id": "b"}]}}'}])
    body = history.build_history_briefs(BigQueryStore(client=client))
    sql = [s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s][-1]
    assert "v_briefs_current" in sql and "status" not in sql.split("WHERE", 1)[1]
    assert "JSON_QUERY(b.payload, '$.held_back.items')" in sql
    assert sql.count("b.payload") == sql.count("JSON_QUERY(b.payload") == 3  # never the whole payload
    za = body["dates"][0]["markets"][0]
    assert value(za["cards"]) == 0 and value(za["held"]) == 2 and za["held"]["run_id"] == "r_brief_1"


def test_bigquery_nearest_items_is_null_without_a_stored_centroid():
    client = FakeClient(catalog(), rows=[{"n": 0}])
    assert BigQueryStore(client=client).nearest_items(STEP, 20) is None
    assert not any("VECTOR_SEARCH" in s for s, _ in client.calls)


def test_bigquery_history_degrades_when_objects_are_missing():
    client = FakeClient(catalog("v_item_waves", "v_item_daily_current", "cultural_map", "findings"))
    bq = BigQueryStore(client=client)
    assert bq.waves([STEP], "ZA") is None
    assert bq.item_days([STEP], "ZA", "2025-09-10", "2026-09-30") is None
    assert bq.nearest_items(STEP, 20) is None
    assert bq.search_items("heritage", 20) is None
    assert bq.findings(None) is None


def test_bigquery_catalog_reads_the_map_and_findings_columns():
    client = FakeClient(catalog())
    BigQueryStore(client=client).search_items("heritage", 20)
    sql = next(s for s, _ in client.calls if "INFORMATION_SCHEMA" in s)
    assert "c.table_name = 'cultural_map'" in sql and "c.table_name = 'findings'" in sql


def test_bigquery_without_a_centroid_column_there_are_no_neighbours():
    client = FakeClient(catalog(map_columns=lacking("centroid")), rows=[{"n": 1}])
    assert BigQueryStore(client=client).nearest_items(STEP, 20) is None  # analogues null, with NO_MAP's words
    assert not any("centroid" in s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)


def test_bigquery_search_without_an_aliases_column_matches_labels_only():
    row = {"item_id": HER, "kind": "hashtag", "label": "#fixture_za_heritage", "canonical_key": "x", "aliases": []}
    client = FakeClient(catalog(map_columns=lacking("aliases")), rows=[row])
    assert BigQueryStore(client=client).search_items("heritage", 20) == [row]
    sql = [s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s][-1]
    assert "cm.aliases" not in sql and "CAST([] AS ARRAY<STRING>) AS aliases" in sql
    assert "STRPOS(LOWER(IFNULL(cm.label, '')), LOWER(@q))" in sql


def test_bigquery_findings_without_a_status_column_give_the_fallback_note():
    row = {"finding_id": "f1", "question": "q", "answer": "a", "as_of": "2026-09-30T08:00:00+02:00", "claims": [],
           "valid_from": None, "valid_to": None, "status": None}
    client = FakeClient(catalog(findings_columns=lacking("status", FINDINGS_COLUMNS)), rows=[row])
    body = history.build_history_findings(BigQueryStore(client=client), status="current")
    sql = [s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s][-1]
    assert "f.status" not in sql and "CAST(NULL AS STRING) AS status" in sql
    assert body == {"findings": [{"finding_id": "f1", "question": "q", "answer": "a",
                                  "as_of": "2026-09-30T08:00:00+02:00", "status": None, "claims": [],
                                  "evidence": []}],
                    "note": "Whether a finding still holds is not checked yet"}
    store_mod._CATALOG.clear()
    client = FakeClient(catalog(), rows=[row])
    BigQueryStore(client=client).findings(None)
    assert "f.status" in [s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s][-1]


# Albert, 4 October 2026: with Nigeria picked in the header, History still listed South Africa's asks. Every History
# list takes the picked market; without one it lists every market as before.
class AsksInTwoMarkets(FixtureStore):
    def ask_history(self, limit, before, market=None):
        rows = [dict(r, market="ZA") for r in super().ask_history(limit, before)]
        rows.append({"ask_id": "a_20260930_ng", "question": "What is Lagos posting about?", "market": "NG",
                     "asked_at": "2026-09-30T07:00:00+02:00", "status": "complete", "answer_status": "complete"})
        rows = [r for r in rows if market is None or r["market"] == market]
        return sorted(rows, key=lambda r: r["asked_at"], reverse=True)[:limit]


def test_asks_follow_the_picked_market_and_carry_their_market():
    ng = history.build_history_asks(AsksInTwoMarkets(), limit=20, market="NG")
    assert [(a["ask_id"], a["market"]) for a in ng["asks"]] == [("a_20260930_ng", "NG")]
    every = history.build_history_asks(AsksInTwoMarkets(), limit=20)
    assert {a["market"] for a in every["asks"]} == {"ZA", "NG"}
    assert [a["ask_id"] for a in history.build_history_asks(FixtureStore(), limit=20, market="NG")["asks"]] == []
    assert len(history.build_history_asks(FixtureStore(), limit=20, market="ZA")["asks"]) == 3
    with pytest.raises(BadRequest):
        history.build_history_asks(FixtureStore(), market="GH")


def test_briefs_follow_the_picked_market(fx):
    body = history.build_history_briefs(fx, limit=30, market="KE")
    assert body["dates"] and all([m["market"] for m in d["markets"]] == ["KE"] for d in body["dates"])
    with pytest.raises(BadRequest):
        history.build_history_briefs(fx, market="ALL")


def test_findings_follow_the_picked_market_and_keep_those_with_no_recorded_market(fx):
    class Saved(FixtureStore):
        def findings(self, item_id=None):
            rows = super().findings(item_id)
            return [dict(r, answer=json.dumps({"market": "ZA"})) if r["finding_id"] == "f_her_1" else r
                    for r in rows]

    def market_of(body):
        return [f["finding_id"] for f in body["findings"]]
    assert "f_her_1" not in market_of(history.build_history_findings(Saved(), market="NG"))
    assert "f_step_1" in market_of(history.build_history_findings(Saved(), market="NG"))
    assert "f_her_1" in market_of(history.build_history_findings(Saved(), market="ZA"))
    with pytest.raises(BadRequest):
        history.build_history_findings(fx, market="GH")


def test_bigquery_history_lists_filter_by_market_as_a_parameter():
    client = FakeClient(catalog(), rows=[])
    bq = BigQueryStore(client=client)
    bq.ask_history(20, None, "NG")
    bq.brief_history(30, None, "NG")
    asks, briefs = [s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s][-2:]
    assert "JSON_VALUE(r.record, '$.market') = @market" in asks and "'NG'" not in asks
    assert "b.market = @market" in briefs and "d.market = @market" in briefs and "'NG'" not in briefs
