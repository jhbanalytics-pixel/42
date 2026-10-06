"""Tests for the confirm lane (core/brief/confirm.py). A fake SocialCrawl client stands in: no calls, no credits."""

import copy
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import core.collect
from core.brief import confirm as confirm_mod
from core.brief.confirm import confirm
from core.collect import parse as parse_mod
from core.collect.job import safe_geo
from core.detect import geo, items

D = date(2026, 9, 20)
SIX = ["instagram", "youtube", "reddit", "twitter", "threads", "facebook"]
SOURCES_MD = Path(__file__).resolve().parents[3] / "docs" / "full-42" / "SOURCES.md"
LIVE = json.loads((Path(__file__).parent / "fixtures" / "search_multi_live.json").read_text(encoding="utf-8"))
MULTI_BODY = LIVE["search_multi"]


@dataclass
class Res:
    status: str
    route: str
    credits_charged: float = 0
    items: list = field(default_factory=list)
    reason: str = ""
    body: dict | None = None


def multi_ok(found, credits=None):
    """A search/multi result in the live shape: the platforms in found each return one row in data.items, the
    row naming its platform, and data.sources holds only per-platform call metadata."""
    def reply(route, params):
        asked = params["platforms"].split(",")
        rows = [{"platform": p, "post": {"id": f"{p}_1"}} for p in asked if p in found]
        sources = {p: {"credits": 1 if p in found else 0, "endpoint": f"{p}/search", "rows": int(p in found),
                       "rows_kept": int(p in found), "status": "ok"} for p in asked}
        charged = len(rows) if credits is None else credits
        return Res("ok", route, charged, rows, body={"data": {"items": rows, "sources": sources},
                                                      "credits_used": charged})
    return reply


class FakeSC:
    def __init__(self, multi=None, tiktok=None, mode="live", run_id="brief-20260920-fake"):
        self.calls = []
        self.mode, self.run_id = mode, run_id
        self.now = datetime(2026, 9, 20, 3, 30, tzinfo=UTC)
        self.multi = multi or multi_ok(set())
        self.tiktok = tiktok or (lambda route, params: Res("ok", route, 1, [{"id": "tt_1"}],
                                                           body={"data": {"items": [{"id": "tt_1"}]}}))

    def clock(self):
        return self.now

    def call(self, route, params=None, *, market=None, item_id=None, lane=None):
        self.calls.append({"route": route, "params": dict(params), "market": market, "item_id": item_id,
                           "lane": lane})
        self.now += timedelta(seconds=5)  # the call takes time, so the clock at return is past the clock at call
        reply = self.multi if route == "search/multi" else self.tiktok
        return reply(route, params)


def cand(item_id="i1", kind="topic", label="Shaya step", canonical_key="shayastep", first_seen=date(2026, 9, 16),
         seen_platforms=("tiktok",)):
    return {"item_id": item_id, "kind": kind, "label": label, "canonical_key": canonical_key,
            "first_seen": first_seen, "seen_platforms": list(seen_platforms)}


def test_one_search_multi_per_candidate_on_the_missing_platforms_since_first_sighting():
    sc = FakeSC()
    out = confirm([cand(seen_platforms=("tiktok", "youtube", "reddit"))], sc=sc, market="ZA", d=D)
    assert sc.calls == [{
        "route": "search/multi", "market": "ZA", "item_id": "i1", "lane": "confirm",
        "params": {"query": "Shaya step", "since": "2026-09-16",
                   "platforms": "instagram,twitter,threads,facebook"}}]
    assert out["i1"]["status"] == "done"


def test_since_defaults_to_d_minus_7_and_query_falls_back_to_the_canonical_key():
    sc = FakeSC()
    confirm([cand(label=None, first_seen=None)], sc=sc, market="NG", d=D)
    params = sc.calls[0]["params"]
    assert params["since"] == "2026-09-13"
    assert params["query"] == "shayastep"
    assert params["platforms"] == ",".join(SIX)


def test_hashtag_and_sound_items_also_get_a_tiktok_top_search_by_date_in_the_market():
    for kind in ("hashtag", "sound"):
        for market in ("ZA", "NG", "KE"):
            sc = FakeSC()
            confirm([cand(kind=kind)], sc=sc, market=market, d=D)
            assert [c["route"] for c in sc.calls] == ["search/multi", "tiktok/search/top"]
            tiktok = sc.calls[1]
            assert tiktok["params"] == {"query": "Shaya step", "country": market, "sort_by": "date-posted"}
            assert tiktok["lane"] == "confirm" and tiktok["item_id"] == "i1" and tiktok["market"] == market


def test_other_kinds_get_no_tiktok_search():
    for kind in ("topic", "format", "meme", "creator", "brand", "event"):
        sc = FakeSC()
        confirm([cand(kind=kind)], sc=sc, market="ZA", d=D)
        assert [c["route"] for c in sc.calls] == ["search/multi"]


def test_no_search_multi_when_the_item_is_already_on_every_platform():
    sc = FakeSC()
    out = confirm([cand(kind="hashtag", seen_platforms=["tiktok"] + SIX)], sc=sc, market="ZA", d=D)
    assert [c["route"] for c in sc.calls] == ["tiktok/search/top"]
    assert out["i1"]["status"] == "done"

    sc = FakeSC()
    out = confirm([cand(kind="topic", seen_platforms=SIX)], sc=sc, market="ZA", d=D)
    assert sc.calls == [] and out["i1"] == {"status": "done", "platforms_found": [], "credits": 0.0, "calls": []}


def test_platforms_found_and_credits_come_from_the_results():
    sc = FakeSC(multi=multi_ok({"instagram", "threads"}))
    out = confirm([cand(kind="hashtag")], sc=sc, market="ZA", d=D)
    assert out["i1"]["platforms_found"] == ["instagram", "threads", "tiktok"]
    assert out["i1"]["credits"] == 3.0
    assert [c["status"] for c in out["i1"]["calls"]] == ["ok", "ok"]
    assert out["i1"]["calls"][0]["platforms"] == ["instagram", "threads"]


def test_empty_results_find_nothing():
    sc = FakeSC(tiktok=lambda route, params: Res("empty", route, 0, []))
    out = confirm([cand(kind="hashtag")], sc=sc, market="ZA", d=D)
    assert out["i1"]["platforms_found"] == []
    assert out["i1"]["credits"] == 0.0


def test_multi_rows_carrying_a_platform_field_count_when_there_is_no_body():
    def reply(route, params):
        return Res("cached", route, 0, [{"platform": "youtube", "id": "y1"}, {"platform": "Reddit", "id": "r1"}])
    out = confirm([cand()], sc=FakeSC(multi=reply), market="ZA", d=D)
    assert out["i1"]["platforms_found"] == ["reddit", "youtube"]


def test_candidates_run_in_rank_order():
    sc = FakeSC()
    out = confirm([cand("a"), cand("b"), cand("c")], sc=sc, market="ZA", d=D)
    assert [c["item_id"] for c in sc.calls] == ["a", "b", "c"]
    assert list(out) == ["a", "b", "c"]


def test_the_lane_stops_before_a_call_that_would_pass_max_credits():
    sc = FakeSC(multi=multi_ok({"instagram", "twitter", "facebook"}))
    three = ("tiktok", "youtube", "reddit", "threads")
    cands = [cand(i, seen_platforms=three) for i in ("a", "b", "c", "d")]
    out = confirm(cands, sc=sc, market="ZA", d=D, max_credits=8)
    assert [c["item_id"] for c in sc.calls] == ["a", "b"]
    assert [out[i]["status"] for i in "abcd"] == ["done", "done", "stopped", "stopped"]
    assert out["c"] == {"status": "stopped", "platforms_found": [], "credits": 0.0, "calls": []}


def test_share_used_counts_toward_the_limit():
    sc = FakeSC()
    out = confirm([cand()], sc=sc, market="KE", d=D, max_credits=150, share_used=145)
    assert sc.calls == []
    assert out["i1"]["status"] == "stopped"

    sc = FakeSC()
    confirm([cand()], sc=sc, market="KE", d=D, max_credits=150, share_used=144)
    assert len(sc.calls) == 1


def test_the_budget_uses_what_was_charged_not_what_was_quoted():
    sc = FakeSC(multi=multi_ok(set(), credits=0))
    cands = [cand(str(i)) for i in range(5)]
    out = confirm(cands, sc=sc, market="ZA", d=D, max_credits=6)
    assert len(sc.calls) == 5
    assert all(out[str(i)]["status"] == "done" for i in range(5))


def test_a_halting_status_stops_the_lane_at_once():
    for status in ("cap_reached", "balance_floor", "insufficient_credits"):
        sc = FakeSC(multi=lambda route, params, s=status: Res(s, route, 0, reason="halt"))
        out = confirm([cand("a", kind="hashtag"), cand("b")], sc=sc, market="ZA", d=D)
        assert [c["route"] for c in sc.calls] == ["search/multi"], status
        assert out["a"]["status"] == "stopped"
        assert out["a"]["calls"] == [{"route": "search/multi", "status": status, "credits": 0.0, "platforms": [],
                                      "reason": "halt"}]
        assert out["b"] == {"status": "stopped", "platforms_found": [], "credits": 0.0, "calls": []}


def test_no_call_starts_once_stop_says_so_and_the_credits_already_charged_are_kept():
    sc = FakeSC(multi=multi_ok({"instagram"}))
    out = confirm([cand("a", kind="hashtag"), cand("b")], sc=sc, market="ZA", d=D, stop=lambda: bool(sc.calls))
    assert [c["route"] for c in sc.calls] == ["search/multi"]
    assert out["a"]["status"] == "stopped"
    assert out["a"]["credits"] == 1.0 and out["a"]["platforms_found"] == ["instagram"]
    assert out["b"] == {"status": "stopped", "platforms_found": [], "credits": 0.0, "calls": []}


def test_forbidden_error_and_not_in_replay_are_recorded_and_the_lane_goes_on():
    for status in ("forbidden", "error", "not_in_replay"):
        sc = FakeSC(multi=lambda route, params, s=status: Res(s, route, 0, reason="why"))
        out = confirm([cand("a", kind="hashtag"), cand("b")], sc=sc, market="ZA", d=D)
        assert [c["route"] for c in sc.calls] == ["search/multi", "tiktok/search/top", "search/multi"], status
        assert out["a"]["status"] == "done"
        assert out["a"]["calls"][0] == {"route": "search/multi", "status": status, "credits": 0.0, "platforms": [],
                                        "reason": "why"}
        assert out["a"]["platforms_found"] == ["tiktok"]
        assert out["b"]["status"] == "done"


def test_only_the_two_confirm_routes_are_ever_called_and_neither_is_never_used():
    sc = FakeSC(multi=multi_ok(set(SIX)))
    kinds = ("hashtag", "sound", "topic", "meme")
    confirm([cand(str(i), kind=k) for i, k in enumerate(kinds)], sc=sc, market="ZA", d=D)
    assert {c["route"] for c in sc.calls} == confirm_mod.ROUTES == {"search/multi", "tiktok/search/top"}
    never = SOURCES_MD.read_text(encoding="utf-8").split("Never used:", 1)[1].split("\n", 1)[0]
    for route in confirm_mod.ROUTES:
        assert route not in never
    assert all(c["lane"] == "confirm" for c in sc.calls)


def test_the_result_is_plain_json():
    sc = FakeSC(multi=multi_ok({"youtube"}))
    out = confirm([cand(kind="sound")], sc=sc, market="ZA", d=D)
    assert json.loads(json.dumps(out)) == out


def test_a_seen_id_goes_on_the_tiktok_search_only_when_given():
    sc = FakeSC()
    confirm([cand(kind="hashtag")], sc=sc, market="ZA", d=D, seen="seen_abc")
    by_route = {c["route"]: c["params"] for c in sc.calls}
    assert by_route["tiktok/search/top"]["seen"] == "seen_abc"
    assert "seen" not in by_route["search/multi"]
    sc = FakeSC()
    confirm([cand(kind="hashtag")], sc=sc, market="ZA", d=D)
    assert all("seen" not in c["params"] for c in sc.calls)


# The live search/multi shape: rows in data.items naming their platform, call metadata in data.sources


def test_found_reads_the_platform_of_each_row_of_the_live_search_multi_body():
    result = Res("ok", "search/multi", 7, [], body=copy.deepcopy(MULTI_BODY))
    assert confirm_mod._found("search/multi", result) == ["instagram", "reddit", "twitter"]

    sc = FakeSC(multi=lambda route, params: Res("ok", route, 7, [], body=copy.deepcopy(MULTI_BODY)))
    out = confirm([cand(seen_platforms=("tiktok", "youtube", "threads", "facebook"))], sc=sc, market="ZA", d=D)
    assert out["i1"]["platforms_found"] == ["instagram", "reddit", "twitter"]
    assert out["i1"]["calls"][0]["platforms"] == ["instagram", "reddit", "twitter"]


def test_found_falls_back_to_rows_kept_when_the_body_has_no_items():
    body = copy.deepcopy(MULTI_BODY)
    del body["data"]["items"]
    body["data"]["sources"]["reddit"]["rows_kept"] = 0
    assert confirm_mod._found("search/multi", Res("ok", "search/multi", 7, [], body=body)) == [
        "instagram", "twitter"]

    body["data"]["items"] = []
    assert confirm_mod._found("search/multi", Res("ok", "search/multi", 7, [], body=body)) == [
        "instagram", "twitter"]


def test_sources_holding_only_call_metadata_find_nothing_on_their_own():
    body = copy.deepcopy(MULTI_BODY)
    del body["data"]["items"]
    for source in body["data"]["sources"].values():
        source["rows_kept"] = 0
    assert confirm_mod._found("search/multi", Res("ok", "search/multi", 7, [], body=body)) == []


# Ingest: confirm finds into posts through lane L1's core.collect.parse.ingest


class Recorder:
    def __init__(self, error=None, posts=2, observations=3):
        self.calls, self.error, self.posts, self.observations = [], error, posts, observations

    def __call__(self, client, result, market, lane="confirm", **kw):
        self.calls.append({"client": client, "result": result, "market": market, "lane": lane, **kw})
        if self.error:
            raise self.error
        return {"posts": [{}] * self.posts, "observations": [{}] * self.observations}


@pytest.fixture
def ingest(monkeypatch):
    """L1's parse.ingest replaced by a recorder (added when this branch does not have it yet), loaded through
    confirm.load_ingest as the brief job loads it."""
    rec = Recorder()
    monkeypatch.setattr(parse_mod, "ingest", rec, raising=False)
    return rec


CLIENT = object()


def test_ingest_takes_every_ok_result_with_the_exact_arguments(ingest):
    sc = FakeSC(multi=lambda route, params: Res("ok", route, 7, [], body=copy.deepcopy(MULTI_BODY)))
    returns = []
    real_call = sc.call

    def call(route, params=None, **kw):
        result = real_call(route, params, **kw)
        returns.append(sc.now)
        return result

    sc.call = call
    out = confirm([cand(kind="hashtag")], sc=sc, market="NG", d=D, client=CLIENT,
                  ingest=confirm_mod.load_ingest(), clock=sc.clock)
    assert [c["result"].route for c in ingest.calls] == ["search/multi", "tiktok/search/top"]
    for rec, made, fetched in zip(ingest.calls, sc.calls, returns, strict=True):
        assert set(rec) == {"client", "result", "market", "lane", "params", "run_id", "fetched_at", "item_id_fn",
                            "geo_fn", "seed_key"}
        assert rec["client"] is CLIENT and rec["market"] == "NG" and rec["lane"] == "confirm"
        assert rec["result"].route == made["route"]
        assert rec["params"] == made["params"]
        assert rec["run_id"] == "brief-20260920-fake"
        assert rec["fetched_at"] == fetched
        assert rec["seed_key"] == "i1"
    rec = ingest.calls[0]
    assert rec["item_id_fn"]("hashtag", "#Shaya", "tiktok") == items.item_id(
        "hashtag", items.canonical_key("hashtag", "#Shaya", "tiktok"))
    # safe_geo around geo_for_post: a free-text signal that is not a string reads as absent
    assert rec["geo_fn"]("tiktok", "NG", text=123) == safe_geo(geo.geo_for_post)("tiktok", "NG", text=None)
    assert out["i1"]["ingest"] == {"posts": 4, "observations": 6, "errors": 0}
    assert out["i1"]["platforms_found"] == ["instagram", "reddit", "tiktok", "twitter"]


def test_ingest_takes_only_ok_empty_and_cached_results(ingest):
    for status in ("ok", "empty", "cached", "error", "forbidden", "not_in_replay", "cap_reached"):
        ingest.calls.clear()
        sc = FakeSC(multi=lambda route, params, s=status: Res(s, route, 0, [], body={"data": {"items": []}}))
        confirm([cand()], sc=sc, market="ZA", d=D, client=CLIENT, ingest=confirm_mod.load_ingest(),
                clock=sc.clock)
        assert len(ingest.calls) == (1 if status in ("ok", "empty", "cached") else 0), status


def test_no_ingest_in_replay_mode_without_a_client_or_outside_za_ng_ke(ingest):
    fn = confirm_mod.load_ingest()
    sc = FakeSC(mode="replay")
    out = confirm([cand()], sc=sc, market="ZA", d=D, client=CLIENT, ingest=fn, clock=sc.clock)
    assert ingest.calls == [] and "ingest" not in out["i1"]
    sc = FakeSC()
    out = confirm([cand()], sc=sc, market="ZA", d=D, client=None, ingest=fn, clock=sc.clock)
    assert ingest.calls == [] and "ingest" not in out["i1"]
    sc = FakeSC()
    out = confirm([cand()], sc=sc, market="GLOBAL", d=D, client=CLIENT, ingest=fn, clock=sc.clock)
    assert ingest.calls == [] and "ingest" not in out["i1"]
    sc = FakeSC()
    out = confirm([cand()], sc=sc, market="KE", d=D, client=CLIENT, ingest=None, clock=sc.clock)
    assert "ingest" not in out["i1"]


def test_an_ingest_error_is_counted_and_the_lane_goes_on(monkeypatch):
    rec = Recorder(error=RuntimeError("merge on posts failed"))
    monkeypatch.setattr(parse_mod, "ingest", rec, raising=False)
    sc = FakeSC(multi=lambda route, params: Res("ok", route, 7, [], body=copy.deepcopy(MULTI_BODY)))
    out = confirm([cand("a", kind="hashtag"), cand("b")], sc=sc, market="ZA", d=D, client=CLIENT,
                  ingest=confirm_mod.load_ingest(), clock=sc.clock)
    assert [c["item_id"] for c in sc.calls] == ["a", "a", "b"]
    assert out["a"]["ingest"] == {"posts": 0, "observations": 0, "errors": 2}
    assert out["b"]["ingest"] == {"posts": 0, "observations": 0, "errors": 1}
    assert out["a"]["calls"][0]["ingest_error"] == "RuntimeError: merge on posts failed"
    assert out["a"]["platforms_found"] == ["instagram", "reddit", "tiktok", "twitter"]
    assert out["a"]["status"] == out["b"]["status"] == "done"
    assert json.loads(json.dumps(out)) == out


def test_load_ingest_is_none_while_parse_has_no_ingest(monkeypatch):
    monkeypatch.delattr(parse_mod, "ingest", raising=False)
    assert confirm_mod.load_ingest() is None


def test_load_ingest_is_none_when_the_parse_module_cannot_be_imported(monkeypatch):
    monkeypatch.delattr(core.collect, "parse")
    monkeypatch.setitem(sys.modules, "core.collect.parse", None)
    assert confirm_mod.load_ingest() is None
