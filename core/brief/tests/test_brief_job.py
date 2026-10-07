"""The morning brief job (core/brief/job.py), end to end on DuckDB fixture tables.

Fakes stand in for lane L1's chain, the SocialCrawl confirm lane, the gate context and the model. The evidence
packs, the explanation step, the claim checks, the gate and the payload are the real modules.
"""

import json
import re
import sys
import threading
import time as time_module
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.brief import confirm as confirm_lane
from core.brief import explain
from core.brief import gatectx
from core.brief import job
from core.brief import market_scope as brief_market_scope
from core.brief.payload import REASON_TEXT
from core.collect import chain as live_chain
from core.config.caps import model_daily_usd as read_model_daily_usd
from core.detect.tests import duck
from core.detect.tests.fixtures import D, at, counter, day, item_daily, rid, run

# The cap tests here work out their arithmetic, and the temporary window's SAST expiry, on MODEL_DAILY_USD as it
# stood until 3 October 2026 (USD 20, USD 80 on 1 and 2 October), read from a fixture rather than the live caps.
CAPS_UNTIL_20261003 = Path(__file__).parent / "fixtures" / "caps_until_20261003.yaml"


def shared_model_daily_usd(*, now=None):
    return read_model_daily_usd(CAPS_UNTIL_20261003, now=now)


@pytest.fixture(autouse=True)
def busy_waits(monkeypatch):
    """The seconds the brief waited on a busy model, recorded instead of slept."""
    waits = []
    monkeypatch.setattr(job, "_sleep", waits.append)
    return waits


SAST = timezone(timedelta(hours=2), "SAST")
EARLY = datetime.combine(D, time(5, 30), SAST)
LATE = datetime.combine(D, time(6, 20), SAST)
CAP_BEFORE_EXPIRY = datetime(2026, 10, 2, 23, 59, 59, tzinfo=SAST)
CAP_AFTER_EXPIRY = datetime(2026, 10, 3, 0, 0, tzinfo=SAST)
MARKETS = ("ZA", "NG", "KE")
MARKET_KEYS = {"market", "label", "status", "headline", "banners", "cards", "more", "held_back", "moments", "boards",
               "coverage", "critic"}
CARD_KEYS = {"item_id", "market", "date", "rank", "kind", "title", "state", "state_word", "flag", "flag_word",
             "explained", "explanation", "explanation_claim_ids", "claims", "count_line", "numbers", "sparkline",
             "thumbnails", "evidence_ids", "evidence", "ask", "explanation_status", "failed_reason", "also",
             "market_scope", "market_posts7", "total_posts7", "market_share7", "specificity", "news_driven",
             "title_written"}  # the writer's checked title, added 6 October 2026
ROW_KEYS = {"brief_date", "market", "run_id", "published_at", "status", "payload", "rule_version"}
CHECK_KEYS = {"answer_or_brief_id", "claim_id", "rule", "verdict", "checker", "run_id", "reason"}
REASONS = {"data_issue", "likely_coordinated", "political_unconfirmed", "paid_led", "not_local", "too_few_creators",
           "not_confirmed", "explanation_failed"}
WRITES = re.compile(r"\b(CREATE|DROP|DELETE|TRUNCATE|MERGE|INSERT|UPDATE|ALTER)\b", re.IGNORECASE)
DEFAULT_GEO = object()


def pin_brief_model_cap(monkeypatch, instant):
    reader = lambda: shared_model_daily_usd(now=instant)
    monkeypatch.setattr(job, "model_daily_usd", reader)
    monkeypatch.setattr(explain, "model_daily_usd", reader)


@pytest.fixture(autouse=True)
def market_scope_is_valid_by_default(monkeypatch):
    def read_market_scope(client, row, d, market, *, core, agent):
        return {"market_scope": "market", "market_posts7": 6, "total_posts7": 8, "market_share7": 0.75}

    monkeypatch.setattr(job, "read_market_scope", read_market_scope, raising=False)


# The world


def item(market, n):
    return f"{market.lower()}{n}"


def world(n=7, markets=MARKETS, first_collect=day(2)):
    con = duck.connect()
    duck.load(con, "agent.runs", [run("detect", D), run("collect", day(3), status="failed")]
              + [run("collect", day(i)) for i in range((D - first_collect).days, -1, -1)])
    for m in markets:
        for i in range(1, n + 1):
            add_item(con, m, item(m, i), worth=1 - i / 20)
    return con


def add_item(con, market, item_id, worth, map_status="active", posts=3, label=None, key=None, geo=DEFAULT_GEO,
             **state):
    geo = market if geo is DEFAULT_GEO else geo
    row = {"metric_date": D, "market": market, "item_id": item_id, "kind": "hashtag", "state_raw": "emerging",
           "state": "emerging", "untested": False, "main_ratio": 2.5, "creators3": 12, "posts3": 20,
           "top_creator_share3": 0.2, "authenticity": "clear", "sponsored_share": 0.0, "geo_status": "local",
           "local_share": 0.9, "geo_known_posts7": 10, "worth_raw": worth, "eligible": True,
           "run_id": rid("detect", D),
           "rule_version": "r1", **state}
    duck.load(con, "core.item_state", [row])
    duck.load(con, "core.cultural_map", [{"item_id": item_id, "kind": "hashtag", "canonical_key": key or item_id,
                                          "label": f"#{item_id}" if label is None else (label or None),
                                          "first_seen": day(5), "status": map_status,
                                          "valid_from": at(day(400)), "valid_to": None}])
    for k, platform in enumerate(("tiktok", "instagram", "tiktok")[:posts], 1):
        pid, creator = f"{item_id}_p{k}", f"{item_id}_c{k}"
        duck.load(con, "core.creators", [{"creator_id": creator, "platform": platform, "handle": f"@{creator}",
                                          "coord_score": 0}])
        duck.load(con, "core.posts", [{
            "post_id": pid, "platform": platform, "creator_id": creator, "creator_tier_at_post": "micro",
            "text": "Dancing to the new sound at home with friends", "published_at": at(day(1), 9),
            "geo_market": geo, "geo_confidence": 0.9 if geo else None, "geo_source": "ext_region" if geo else None,
            "post_date": day(1), "engagement": 100 - k}])
        duck.load(con, "core.post_items", [{"post_id": pid, "item_id": item_id, "via": "hashtag"}])
        duck.load(con, "core.post_observations", [{
            "post_id": pid, "observed_at": at(day(1), 10), "observed_date": day(1), "market": market,
            "platform": platform, "lane": "sweep", "lane_class": "unbiased_rank", "run_id": rid("collect", day(1))}])


class Client(duck.Client):
    """The DuckDB stand-in plus insert_rows_json. DuckDB connections are not thread-safe, so queries take a lock."""

    def __init__(self, con):
        super().__init__(con)
        self.inserted = {}
        self._lock = threading.Lock()

    def query(self, sql, job_config=None):
        with self._lock:
            return super().query(sql, job_config)

    def insert_rows_json(self, table, rows):
        self.inserted.setdefault(table, []).extend(json.loads(json.dumps(rows)))
        return []


# Fakes


class FakeChain:
    class AlreadyDone(Exception):
        def __init__(self, message, run):
            super().__init__(message)
            self.run = run

    class UpstreamNotReady(Exception):
        def __init__(self, message, run):
            super().__init__(message)
            self.run = run

    def __init__(self, raises=None):
        self.raises = raises
        self.begun, self.finished, self.started = [], [], []

    def begin(self, stage, run_date=None):
        r = SimpleNamespace(run_id=f"{stage}-{run_date:%Y%m%d}-fake", stage=stage, run_date=run_date)
        self.begun.append((stage, run_date))
        if self.raises == "upstream":
            raise self.UpstreamNotReady(f"upstream detect for {run_date.isoformat()} is not ok (latest status "
                                        "failed)", r)
        if self.raises == "done":
            raise self.AlreadyDone(f"brief for {run_date.isoformat()} already ran ok", r)
        return r

    def finish(self, run, status, counts, error=None):
        self.finished.append({"run": run, "status": status, "counts": counts, "error": error})

    def start_next(self, stage, run_date=None):
        self.started.append(stage)

    def today(self, now=None):
        return now.astimezone(SAST).date()

    def past_deadline(self, now, run_date=None):
        return now.astimezone(SAST) >= datetime.combine(run_date or self.today(now), time(6, 15), SAST)


def draft(ids, local_ids=None):
    local_ids = list(ids if local_ids is None else local_ids)
    quote_id = local_ids[-1] if local_ids else ids[-1]
    return {
        "explanation": "Local creators are posting videos with this tag, likely through dance clips shared at home.",
        "explanation_claim_ids": ["c1", "c3"],
        "claims": [
            {"id": "c1", "text": "Local creators are posting videos with this tag.", "label": "observed",
             "kind": "observation", "evidence_ids": local_ids,
             "quotes": [{"evidence_id": quote_id, "text": "Dancing to the new sound"}], "number_ids": []},
            {"id": "c2", "text": "The earliest post in the pack comes from a local creator.",
             "label": "single_source", "kind": "observation", "evidence_ids": ids[:1], "quotes": [],
             "number_ids": []},
            {"id": "c3", "text": "It likely spread through dance clips shared at home.", "label": "inferred",
             "kind": "interpretation", "evidence_ids": local_ids[1:3], "quotes": [], "number_ids": []},
        ],
    }


class FakeModel:
    """Writes a passing draft from the post ids in the prompt; every support check says supported; the critic
    rules the simple explanation out unless `ruled_out` is False."""

    def __init__(self, usd=0.01, ruled_out=True):
        self.usd = usd
        self.ruled_out = ruled_out
        self.calls = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        support = "verdict" in schema["properties"]
        critic = "ruled_out" in schema["properties"]
        self.calls.append({"user": user, "support": support, "critic": critic})
        usage = {"input_tokens": 100, "output_tokens": 50, "usd": self.usd}
        if critic:
            return {"non_cultural_explanation": "a paid campaign", "ruled_out": self.ruled_out,
                    "local_why_now": True,
                    "reason": "fake"}, usage
        if support:
            return {"verdict": "supported", "reason": "fake"}, usage
        posts = [json.loads(line[5:]) for line in user.splitlines() if line.startswith("post {")]
        ids = [post["id"] for post in posts if post.get("id")]
        match = re.search(r"\bMarket: (ZA|NG|KE)\.", user)
        market = match.group(1) if match else None
        local_ids = [post["id"] for post in posts if post.get("id") and
                     (post.get("market") == market or post.get("source_market") == market)]
        return draft(ids, local_ids=local_ids), usage

    def writer_calls(self):
        return [c for c in self.calls if not c["support"] and not c.get("critic")]


class FakeCtx:
    """build_ctx with a clean context unless the test overrides keys for an item."""

    def __init__(self, overrides=None, error=None):
        self.overrides = overrides or {}
        self.error = error
        self.calls = []

    def __call__(self, client, item_row, d, market, evidence, *, campaign_hashtags, political_terms,
                 explanation_passed=None, numbers=(), core=None, agent=None):
        if self.error:
            raise self.error
        self.calls.append({"item_id": item_row["item_id"], "market": market, "evidence": list(evidence),
                           "numbers": list(numbers), "political_terms": list(political_terms)})
        ctx = {"valid_days": [True, True, True], "lane_classes": ["unbiased_rank"],
               "campaign_hashtags": list(campaign_hashtags), "political": False, "corroborated_unbiased": True,
               "explanation_passed": explanation_passed}
        ctx.update(self.overrides.get(item_row["item_id"], {}))
        return ctx


class FakeConfirm:
    def __init__(self, credits=5.0, found=None, fail_in=()):
        self.credits = credits
        self.found = found or {}
        self.fail_in = set(fail_in)
        self.calls = []

    def __call__(self, candidates, *, sc, market, d, max_credits=150, share_used=0, seen=None, client=None,
                 ingest=None, clock=None, stop=None):
        self.calls.append({"candidates": [dict(c) for c in candidates], "sc": sc, "market": market, "d": d,
                           "share_used": share_used, "seen": seen, "client": client, "ingest": ingest})
        if market in self.fail_in:
            raise RuntimeError("credit_ledger read failed")
        return {c["item_id"]: {"status": "done", "platforms_found": list(self.found.get(c["item_id"], [])),
                               "credits": self.credits, "calls": []} for c in candidates}


SC = object()


def brief(con, *, chain=None, model=None, sc=SC, clock=None, ctx=None, confirm=None, workers=5, campaign=()):
    client = Client(con)
    chain = chain or FakeChain()
    model = model or FakeModel()
    ctx = ctx or FakeCtx()
    confirm = confirm or FakeConfirm()
    make_sc = None if sc is None else (lambda run_id: sc)
    counts = job.run(client, D, chain=chain, model=model, make_sc=make_sc, clock=clock or (lambda: EARLY),
                     build_ctx=ctx, confirm=confirm, campaign_hashtags=list(campaign),
                     political_terms={m: ["election"] for m in MARKETS}, workers=workers, core="core",
                     agent="agent")
    return SimpleNamespace(client=client, chain=chain, model=model, ctx=ctx, confirm=confirm, counts=counts)


def rows(r):
    return {row["market"]: row for row in r.client.inserted.get("agent.briefs", [])}


def payload(r, market):
    return json.loads(rows(r)[market]["payload"])


def checks(r):
    return r.client.inserted.get("agent.claim_checks", [])


def all_cards(p):
    return p["cards"] + p["more"]


# A normal morning


def test_a_normal_morning_writes_one_briefs_row_per_market_in_the_contract_shape():
    r = brief(world(), ctx=FakeCtx({item(m, 3): {"valid_days": [True, False, True]} for m in MARKETS}))
    assert sorted(rows(r)) == sorted(MARKETS)
    for m in MARKETS:
        row = rows(r)[m]
        assert set(row) == ROW_KEYS
        assert row["brief_date"] == D.isoformat()
        assert row["run_id"] == "brief-20260920-fake"
        assert row["rule_version"] == job.RULE_VERSION
        assert row["status"] == "published"
        assert datetime.fromisoformat(row["published_at"]) == EARLY
        p = payload(r, m)
        assert set(p) == MARKET_KEYS
        assert p["market"] == m and p["status"] == "published"
        assert len(p["cards"]) == 5 and len(p["more"]) == 1
        assert [c["rank"] for c in all_cards(p)] == [1, 2, 3, 4, 5, 6]
        assert [c["item_id"] for c in all_cards(p)] == [item(m, i) for i in (1, 2, 4, 5, 6, 7)]
        for c in all_cards(p):
            assert set(c) == CARD_KEYS
            assert c["explained"] is True
            assert c["specificity"]["status"] == "pass"
            assert c["explanation_claim_ids"] == ["c1", "c3"]
            assert len(c["evidence"]) == 3
            assert c["title"] == f"#{c['item_id']}"
            assert {n["unit"] for n in c["numbers"]} == {"creators in 3 days", "posts in 3 days", "times usual"}
    assert r.chain.begun == [("brief", D)]
    assert r.chain.started == []


def test_the_run_finishes_ok_with_counts_and_writes_only_briefs_and_claim_checks():
    r = brief(world(), ctx=FakeCtx({item(m, 3): {"valid_days": [False, True, True]} for m in MARKETS}))
    [fin] = r.chain.finished
    assert fin["run"].run_id == "brief-20260920-fake"
    assert fin["status"] == "ok"
    assert fin["counts"]["markets"] == 3
    assert fin["counts"]["cards"] == 18
    assert fin["counts"]["held"] == 3
    assert fin["counts"]["credits"] == pytest.approx(5.0 * 18)
    assert fin["counts"]["model_usd"] == pytest.approx(0.01 * 6 * 18)  # writer, 3 claim checks, sentence check, critic
    assert fin["counts"] == r.counts
    assert set(r.client.inserted) == {"agent.briefs", "agent.claim_checks"}
    assert not [s for s in r.client.sql if WRITES.search(s)]


def test_held_items_land_in_held_back_with_contract_reason_codes():
    con = world()
    add_item(con, "ZA", "za_coord", 0.99, authenticity="likely_coordinated")
    overrides = {"za1": {"valid_days": [None, False, True]}, "za2": {"political": True,
                 "corroborated_unbiased": False}}
    r = brief(con, ctx=FakeCtx(overrides), campaign=["#ZA3"])
    held = payload(r, "ZA")["held_back"]
    by_id = {i["item_id"]: i for i in held["items"]}
    assert {k: v["reason"] for k, v in by_id.items()} == {
        "za_coord": "likely_coordinated", "za1": "data_issue", "za2": "political_unconfirmed", "za3": "paid_led"}
    assert {k: v["rule"] for k, v in by_id.items()} == {"za_coord": "G4", "za1": "G1", "za2": "G4b", "za3": "G5b"}
    assert held["count"] == 4
    for i in held["items"]:
        assert i["reason"] in REASONS
        assert i["reason_text"]
        assert len(i["evidence"]) == 3 and i["evidence_ids"] == [e["id"] for e in i["evidence"]]
    assert "za_coord" not in {c["item_id"] for c in all_cards(payload(r, "ZA"))}


def test_a_candidate_the_gate_holds_is_never_sent_to_the_model_or_confirmed():
    con = world()
    add_item(con, "ZA", "za_coord", 0.99, authenticity="likely_coordinated")
    r = brief(con, ctx=FakeCtx({"za1": {"lane_classes": ["search_presence"]}}))
    prompts = "\n".join(c["user"] for c in r.model.calls)
    assert "#za1" not in prompts and "za1_p1" not in prompts
    assert "#za_coord" not in prompts and "za_coord_p1" not in prompts
    assert "#za2" in prompts
    confirmed = {c["item_id"] for call in r.confirm.calls for c in call["candidates"]}
    assert "za1" not in confirmed and "za_coord" not in confirmed and "za2" in confirmed
    assert not [c for c in checks(r) if c["answer_or_brief_id"].endswith(":za1")]


def test_candidates_are_the_top_ten_of_the_current_detect_run_by_worth():
    con = world(n=12, markets=("ZA",))
    duck.load(con, "core.item_state", [{"metric_date": D, "market": "ZA", "item_id": "stale", "state": "rising",
                                        "worth_raw": 5.0, "run_id": "detect-old"}])
    duck.load(con, "core.item_state", [{"metric_date": day(1), "market": "ZA", "item_id": "yesterday",
                                        "state": "rising", "worth_raw": 5.0, "run_id": rid("detect", D)}])
    r = brief(con)
    p = payload(r, "ZA")
    assert [c["item_id"] for c in all_cards(p)] == [item("ZA", i) for i in range(1, 11)]
    assert [c["market"] for c in r.ctx.calls] == ["ZA"] * 10
    assert payload(r, "NG")["cards"] == [] and payload(r, "NG")["held_back"]["count"] == 0


def test_candidates_prioritize_local_majority_with_post_floor_before_worth(monkeypatch):
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = world(n=0, markets=("ZA",))
    for rank in range(10, 0, -1):
        add_item(con, "ZA", f"za_global_{rank}", float(rank), geo=None)
    add_item(con, "ZA", "za_local_three", 0.3, posts=3)
    add_item(con, "ZA", "za_source_only", 0.2, posts=3, geo=None)
    source_sighting = {"source_market": "ZA", "source_region": None, "route": "tiktok/trending",
                       "protocol": "tiktok/trending?feed=local", "observed_at": at(day(1), 10),
                       "obs_date": day(1)}
    duck.load(con, "core.source_market_fixture", [
        {"post_id": f"za_source_only_p{post}", "source_markets": [],
         "source_sightings": [dict(source_sighting)]}
        for post in (1, 2)
    ])
    add_item(con, "ZA", "za_local_full_share", 0.1, posts=3)
    add_item(con, "ZA", "za_thin_local", 100.0, posts=2)
    add_item(con, "ZA", "za_fifty_tie", 50.0, posts=4, geo=None)
    tie_post, tie_creator = "za_fifty_tie_p4", "za_fifty_tie_c4"
    duck.load(con, "core.creators", [{"creator_id": tie_creator, "platform": "instagram",
                                      "handle": f"@{tie_creator}", "coord_score": 0}])
    duck.load(con, "core.posts", [{
        "post_id": tie_post, "platform": "instagram", "creator_id": tie_creator,
        "creator_tier_at_post": "micro", "text": "Dancing to the new sound at home with friends",
        "published_at": at(day(1), 9), "geo_market": None, "geo_confidence": None, "geo_source": None,
        "post_date": day(1), "engagement": 96,
    }])
    duck.load(con, "core.post_items", [{"post_id": tie_post, "item_id": "za_fifty_tie", "via": "hashtag"}])
    duck.load(con, "core.post_observations", [{
        "post_id": tie_post, "observed_at": at(day(1), 10), "observed_date": day(1), "market": "ZA",
        "platform": "instagram", "lane": "sweep", "lane_class": "unbiased_rank", "run_id": rid("collect", day(1)),
    }])
    duck.load(con, "core.source_market_fixture", [
        {"post_id": f"za_fifty_tie_p{post}", "source_markets": [],
         "source_sightings": [dict(source_sighting)]}
        for post in (1, 2)
    ])

    by_market = job._candidates(Client(con), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    candidates = by_market["ZA"]
    ids = [cand["row"]["item_id"] for cand in candidates]
    assert {"za_local_three", "za_source_only", "za_local_full_share"} <= set(ids)
    assert len(candidates) == 10
    assert ids[:3] == ["za_local_three", "za_source_only", "za_local_full_share"]
    assert ids[3] == "za_thin_local"
    assert ids[4:] == ["za_fifty_tie", "za_global_10", "za_global_9", "za_global_8", "za_global_7",
                       "za_global_6"]
    rows = {cand["row"]["item_id"]: cand["row"] for cand in candidates}
    assert (rows["za_source_only"]["market_scope"], rows["za_source_only"]["market_posts7"],
            rows["za_source_only"]["total_posts7"]) == ("market", 2, 3)
    assert (rows["za_fifty_tie"]["market_scope"], rows["za_fifty_tie"]["market_posts7"],
            rows["za_fifty_tie"]["total_posts7"], rows["za_fifty_tie"]["market_share7"]) == (
                "global", 2, 4, 0.5)


def test_a_broad_candidate_outranks_a_one_creator_board_item_with_higher_worth(monkeypatch):
    # 3 Oct: single-video YouTube board tags, which can never reach MIN_EVIDENCE, took judged slots on worth.
    # Fewer than 2 creators or 3 posts in 3 days now ranks after the rest of its bucket; nothing is dropped.
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = world(n=0, markets=("ZA",))
    add_item(con, "ZA", "za_board_one_creator", 0.99, creators3=1, posts3=1)
    add_item(con, "ZA", "za_two_posts", 0.98, creators3=2, posts3=2)
    add_item(con, "ZA", "za_no_counts", 0.97, creators3=None, posts3=None)
    add_item(con, "ZA", "za_broad", 0.5)
    add_item(con, "ZA", "za_just_broad", 0.4, creators3=2, posts3=3)

    by_market = job._candidates(Client(con), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    assert [cand["row"]["item_id"] for cand in by_market["ZA"]] == [
        "za_broad", "za_just_broad", "za_board_one_creator", "za_two_posts", "za_no_counts"]


def add_post(con, market, item_id, pid, creator, platform, *, geo=None, geo_source="ext_region", route=None):
    """One post linked to item_id and sighted in market on day 1; with route, also a source sighting in market from
    that route, as v_post_source_markets reads it."""
    duck.load(con, "core.creators", [{"creator_id": creator, "platform": platform, "handle": f"@{creator}",
                                      "coord_score": 0}])
    duck.load(con, "core.posts", [{
        "post_id": pid, "platform": platform, "creator_id": creator, "creator_tier_at_post": "micro",
        "text": "Dancing to the new sound at home with friends", "published_at": at(day(1), 9),
        "geo_market": geo, "geo_confidence": 0.9 if geo else None, "geo_source": geo_source if geo else None,
        "post_date": day(1), "engagement": 50}])
    duck.load(con, "core.post_items", [{"post_id": pid, "item_id": item_id, "via": "hashtag"}])
    duck.load(con, "core.post_observations", [{
        "post_id": pid, "observed_at": at(day(1), 10), "observed_date": day(1), "market": market,
        "platform": platform, "route": route, "lane": "sweep", "lane_class": "unbiased_rank",
        "run_id": rid("collect", day(1))}])
    if route:
        duck.load(con, "core.source_market_fixture", [{
            "post_id": pid, "source_markets": [market],
            "source_sightings": [{"source_market": market, "source_region": market, "route": route,
                                  "protocol": route, "observed_at": at(day(1), 10), "obs_date": day(1)}]}])


def add_board_channel(con, market, item_id, worth, label):
    """A known foreign channel's video on the market's YouTube board stays in the total but has no local post."""
    add_item(con, market, item_id, worth, posts=0, label=label, creators3=1, posts3=1)
    add_post(con, market, item_id, f"{item_id}_v1", f"{item_id}_channel", "youtube", geo="US",
             geo_source="home_market", route="youtube/videos/trending")


def add_board_tag(con, market, item_id, worth, channels=3):
    """A tag under videos of foreign channels on the market's YouTube trending board, location unknown: market_scope
    reads every video as a market post and creators3 counts every channel, so it is a strict market majority."""
    add_item(con, market, item_id, worth, posts=0, creators3=channels, posts3=channels)
    for k in range(1, channels + 1):
        add_post(con, market, item_id, f"{item_id}_v{k}", f"{item_id}_channel{k}", "youtube",
                 route="youtube/videos/trending")


def add_local_breadth(con, market, item_id, worth, unlocated=0):
    """6 posts by 4 local creators: 2 located TikTok creators with 2 posts each and 2 Instagram creators sighted in
    the market's location feed. unlocated TikTok posts by other creators lower the market share."""
    add_item(con, market, item_id, worth, posts=0)
    for k, (platform, posts, geo, route) in enumerate((("tiktok", 2, market, None), ("tiktok", 2, market, None),
                                                       ("instagram", 1, None, "instagram/location/posts"),
                                                       ("instagram", 1, None, "instagram/location/posts")), 1):
        for n in range(1, posts + 1):
            add_post(con, market, item_id, f"{item_id}_c{k}_p{n}", f"{item_id}_c{k}", platform, geo=geo,
                     route=route)
    for k in range(1, unlocated + 1):
        add_post(con, market, item_id, f"{item_id}_u{k}", f"{item_id}_u{k}", "tiktok")


def test_local_creators_beyond_the_youtube_board_outrank_board_only_items_with_higher_worth(monkeypatch):
    # 3 Oct rerun: foreign YouTube channels and tags with board videos ("posts 1 local 1 showable 1") filled the ZA
    # pool: every board video carries the board's region as source market, so a tag under 3 of them is a strict
    # market majority with 3 creators and outranked trends local creators post on TikTok and Instagram on worth.
    # Fewer than 2 local creators beyond the board now ranks after the rest of the same scope bucket; nothing is
    # dropped and the scope is as read.
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = world(n=0, markets=("ZA",))
    add_board_channel(con, "ZA", "za_board_channel", 0.99, "Zack D. Films")
    add_board_tag(con, "ZA", "za_board_tag", 0.98)
    add_local_breadth(con, "ZA", "za_local_breadth", 0.3)

    by_market = job._candidates(Client(con), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    assert [cand["row"]["item_id"] for cand in by_market["ZA"]] == [
        "za_local_breadth", "za_board_tag", "za_board_channel"]
    rows = {cand["row"]["item_id"]: cand["row"] for cand in by_market["ZA"]}
    assert (rows["za_board_channel"]["market_scope"], rows["za_board_channel"]["market_posts7"],
            rows["za_board_channel"]["total_posts7"]) == ("global", 0, 1)
    assert (rows["za_board_tag"]["market_scope"], rows["za_board_tag"]["market_posts7"],
            rows["za_board_tag"]["total_posts7"]) == ("market", 3, 3)
    assert (rows["za_local_breadth"]["market_scope"], rows["za_local_breadth"]["market_posts7"],
            rows["za_local_breadth"]["total_posts7"]) == ("market", 6, 6)


def test_local_first_rank_keeps_the_order_among_equals_and_never_lifts_a_global_row(monkeypatch):
    # Inside each scope bucket, rows on the same side of the local-first line keep their order: thin rows last, then
    # worth. A global-scope row never reaches Today or the market's payload (job.py _for_today, _market_payload), so
    # four local creators do not lift it over a market-scope row with none.
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = world(n=0, markets=("ZA",))
    add_local_breadth(con, "ZA", "za_breadth_global", 0.95, unlocated=6)
    add_item(con, "ZA", "za_one_local_post", 0.9, posts=1)
    add_board_channel(con, "ZA", "za_board_channel", 0.85, "FatSongsong and ThinErmao")
    add_board_tag(con, "ZA", "za_board_tag_high", 0.8)
    add_board_tag(con, "ZA", "za_board_tag_low", 0.7)
    add_item(con, "ZA", "za_local_market_low", 0.2)
    add_item(con, "ZA", "za_local_market_high", 0.4)

    by_market = job._candidates(Client(con), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    assert [cand["row"]["item_id"] for cand in by_market["ZA"]] == [
        "za_local_market_high", "za_local_market_low", "za_board_tag_high", "za_board_tag_low",
        "za_one_local_post", "za_breadth_global", "za_board_channel"]
    rows = {cand["row"]["item_id"]: cand["row"] for cand in by_market["ZA"]}
    assert (rows["za_breadth_global"]["market_scope"], rows["za_breadth_global"]["market_posts7"],
            rows["za_breadth_global"]["total_posts7"]) == ("global", 6, 12)
    assert rows["za_one_local_post"]["market_scope"] == "market"


def test_posts_located_in_another_market_do_not_count_as_local_creators_for_rank(monkeypatch):
    # A post from the market's own feed but located with confidence in another market is not a local creator's
    # post for rank. The foreign veto also leaves it out of the local market count.
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = world(n=0, markets=("ZA",))
    add_item(con, "ZA", "za_fed_from_ng", 0.9, posts=0)
    for k in range(1, 5):
        add_post(con, "ZA", "za_fed_from_ng", f"za_fed_from_ng_p{k}", f"za_fed_from_ng_c{k}", "tiktok", geo="NG",
                 route="tiktok/trending")
    add_item(con, "ZA", "za_local", 0.3)

    by_market = job._candidates(Client(con), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    assert [cand["row"]["item_id"] for cand in by_market["ZA"]] == ["za_local", "za_fed_from_ng"]
    rows = {cand["row"]["item_id"]: cand["row"] for cand in by_market["ZA"]}
    assert (rows["za_fed_from_ng"]["market_scope"], rows["za_fed_from_ng"]["market_posts7"],
            rows["za_fed_from_ng"]["total_posts7"]) == ("global", 0, 4)


def test_candidate_market_priority_remains_after_detect_eligibility_and_generic_gate(monkeypatch):
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = world(n=0, markets=("ZA",))
    for rank in range(9, 0, -1):
        add_item(con, "ZA", f"za_global_{rank}", float(rank), geo=None)
    add_item(con, "ZA", "za_generic", 100.0, map_status="generic", eligible=False)

    by_market = job._candidates(Client(con), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    candidates = by_market["ZA"]
    assert [cand["row"]["item_id"] for cand in candidates] == [
        "za_global_9", "za_global_8", "za_global_7", "za_global_6", "za_global_5", "za_global_4",
        "za_global_3", "za_global_2", "za_global_1", "za_generic",
    ]
    generic = candidates[-1]
    assert generic["row"]["market_scope"] == "market"
    assert generic["held_reason"] == "not_confirmed"
    assert generic["decision"].rule == "G2"
    assert job._for_today(generic) is False


def test_id_titled_candidates_never_take_a_top_ten_slot_from_a_named_one(monkeypatch):
    # 2 Oct: user6242947817218 and 7691513826449034006 took ZA slots while named items ranked lower.
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = world(n=0, markets=("ZA",))
    for rank in range(10, 0, -1):
        add_item(con, "ZA", f"za_named_{rank}", float(rank))
    add_item(con, "ZA", "za_tiktok_user", 100.0, label="", key="user6242947817218")
    add_item(con, "ZA", "za_tiktok_id", 99.0, label="7691513826449034006", key="7691513826449034006")
    add_item(con, "ZA", "za_reddit_user", 98.0, label="", key="t2_2hs85v1ovd")
    add_item(con, "ZA", "za_chosen_handle", 1.5, label="", key="virtual-mycologist13")

    by_market = job._candidates(Client(con), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    ids = [cand["row"]["item_id"] for cand in by_market["ZA"]]
    assert ids == [f"za_named_{rank}" for rank in range(10, 1, -1)] + ["za_chosen_handle"]  # a chosen handle is a name


def test_id_titled_candidates_still_fill_slots_named_ones_leave_empty(monkeypatch):
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = world(n=0, markets=("ZA",))
    add_item(con, "ZA", "za_named", 1.0)
    add_item(con, "ZA", "za_tiktok_user", 100.0, label="", key="user6242947817218")
    by_market = job._candidates(Client(con), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    candidates = by_market["ZA"]
    assert [cand["row"]["item_id"] for cand in candidates] == ["za_named", "za_tiktok_user"]
    assert candidates[1]["decision"].reason == "No readable name"


def test_the_gate_context_gets_each_pack_and_the_market_political_terms():
    r = brief(world(n=2))
    za1 = next(c for c in r.ctx.calls if c["item_id"] == "za1")
    assert sorted(e["id"] for e in za1["evidence"]) == ["za1_p1", "za1_p2", "za1_p3"]
    assert {n["unit"] for n in za1["numbers"]} == {"creators in 3 days", "posts in 3 days", "times usual"}
    assert za1["political_terms"] == ["election"]


# Confirm


def test_confirm_runs_on_publishable_candidates_per_market_inside_one_share():
    con = world(n=3)
    duck.load(con, "agent.runs", [run("aggregate", day(i)) for i in (1, 4, 6, -1)])
    duck.load(con, "core.item_daily", [item_daily("ng1", day(i), 3, market="NG") for i in (1, 4)]
              + [item_daily("ng1", day(6), 3, market="ZA"), item_daily("ng1", D + timedelta(days=1), 3,
                                                                        market="NG")])
    duck.load(con, "core.post_observations", [{
        "post_id": "ng1_p1", "observed_at": at(day(1), 10), "observed_date": day(1), "market": "ZA",
        "platform": "youtube", "lane": "sweep", "lane_class": "unbiased_rank", "run_id": rid("collect", day(1))}])
    r = brief(con, ctx=FakeCtx({"ng2": {"valid_days": [False, True, True]}}))
    assert [c["market"] for c in r.confirm.calls] == ["ZA", "NG", "KE"]
    assert [c["share_used"] for c in r.confirm.calls] == [0, 15.0, 25.0]
    assert all(c["sc"] is SC and c["d"] == D and c["seen"] is None for c in r.confirm.calls)
    ng = r.confirm.calls[1]["candidates"]
    assert [c["item_id"] for c in ng] == ["ng1", "ng3"]
    assert {"item_id", "kind", "label", "canonical_key", "first_seen", "seen_platforms"} <= set(ng[0])
    assert ng[0]["label"] == "#ng1" and ng[0]["canonical_key"] == "ng1" and ng[0]["kind"] == "hashtag"
    assert ng[0]["first_seen"] == day(4)
    assert ng[0]["seen_platforms"] == ["instagram", "tiktok"]
    assert ng[1]["first_seen"] is None
    assert r.counts["credits"] == pytest.approx(40.0)


def test_without_a_socialcrawl_client_confirm_is_skipped_with_a_coverage_note():
    r = brief(world(n=2), sc=None)
    assert r.confirm.calls == []
    for m in MARKETS:
        p = payload(r, m)
        [note] = [b for b in p["banners"] if b["kind"] == "thin_coverage"]
        assert "confirmation" in note["text"].lower()
        assert all(c["explained"] for c in all_cards(p))
    assert r.counts["credits"] == 0


# The deadline and the model cap


def test_spent_today_counts_other_stage_bookings_and_terminal_rows_once():
    con = world(n=1)
    duck.load(con, "agent.runs", [
        {**run("brief", D, run_id="brief-terminal"), "counts": json.dumps({"model_usd": 2.0})},
        {**run("brief", D, run_id="brief-terminal"), "counts": json.dumps({"model_usd": 2.0})},
        {**run("ask", D, run_id="ask-terminal"), "record": json.dumps({"run": {"model_usd": 3.0}})},
        {**run("ask", D, run_id="ask-terminal"), "record": json.dumps({"run": {"model_usd": 3.0}})},
        {**run("understand", D, run_id="understand-terminal"),
         "counts": json.dumps({"model_usd": 0.25, "booked_model_usd": 2.0})},
        {**run("understand_spend", D, run_id="understand-ceiling"),
         "counts": json.dumps({"model_usd": 1.5, "what": "embed_ceiling"})},
        {**run("understand_spend", D, run_id="understand-correction"),
         "counts": json.dumps({"model_usd": -0.5, "what": "embed_correction"})},
        {**run("understand_spend", day(1), run_id="understand-yesterday"),
         "counts": json.dumps({"model_usd": 100.0})},
    ])

    assert job.spent_today(Client(con), D, core="core", agent="agent") == pytest.approx(6.25)


def test_global_scope_stays_available_for_discover_but_leaves_today_and_held_counts(monkeypatch, capsys):
    def read_market_scope(client, row, d, market, *, core, agent):
        if row["item_id"] == "za1":
            return {"market_scope": "global", "market_posts7": 6, "total_posts7": 12, "market_share7": 0.5}
        if row["item_id"] == "za2":
            return {"market_scope": "global", "market_posts7": 0, "total_posts7": 0, "market_share7": None}
        return {"market_scope": "market", "market_posts7": 7, "total_posts7": 10, "market_share7": 0.7}

    monkeypatch.setattr(job, "read_market_scope", read_market_scope, raising=False)
    by_market = job._candidates(Client(world(n=3, markets=("ZA",))), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    scoped = {c["row"]["item_id"]: c for c in by_market["ZA"]}
    assert set(scoped) == {"za1", "za2", "za3"}
    assert scoped["za1"]["row"]["market_scope"] == "global"
    assert scoped["za2"]["row"]["market_share7"] is None
    assert scoped["za3"]["row"]["market_posts7"] == 7
    assert [item_id for item_id, cand in scoped.items() if job._for_today(cand)] == ["za3"]

    today = scoped["za3"]
    result = specificity_result([e["id"] for e in today["pack"]["evidence"]])
    today["decision"] = job._gate(today, True)
    payload = job._market_payload("ZA", D, by_market["ZA"], {id(today): result}, banners=[], moments_=[],
                                  boards_=[], issues=[])
    assert [c["item_id"] for c in payload["cards"] + payload["more"]] == ["za3"]
    assert {c["item_id"] for c in payload["held_back"]["items"]}.isdisjoint({"za1", "za2"})
    assert payload["held_back"]["count"] == 0
    assert "market_scope_read_failed" not in capsys.readouterr().err


def test_failed_market_scope_read_logs_safe_category_and_keeps_fail_closed_defaults(monkeypatch, capsys):
    sentinel = "scope-reader-sentinel-do-not-log"

    def read_market_scope(*args, **kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(job, "read_market_scope", read_market_scope, raising=False)
    by_market = job._candidates(Client(world(n=3, markets=("ZA",))), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")

    captured = capsys.readouterr()
    assert captured.err.splitlines() == [f"brief {D.isoformat()}: market_scope_read_failed"] * 3
    assert sentinel not in captured.err and sentinel not in captured.out
    assert {cand["row"]["item_id"] for cand in by_market["ZA"]} == {"za1", "za2", "za3"}
    for cand in by_market["ZA"]:
        row = cand["row"]
        assert row["market_scope"] == "global"
        assert row["market_posts7"] is None
        assert row["total_posts7"] is None
        assert row["market_share7"] is None
        assert job._for_today(cand) is False


def test_failed_market_scope_read_is_held_as_unreadable_evidence_and_counts_toward_the_data_issue(monkeypatch):
    def read_market_scope(client, row, d, market, *, core, agent):
        if market == "ZA":
            raise RuntimeError("scope read failed")
        return {"market_scope": "market", "market_posts7": 7, "total_posts7": 10, "market_share7": 0.7}

    monkeypatch.setattr(job, "read_market_scope", read_market_scope, raising=False)
    r = brief(world(n=3))

    za = payload(r, "ZA")
    assert all_cards(za) == []
    assert za["status"] == "data_issue"
    assert [b["text"] for b in za["banners"] if b["kind"] == "data_issue"] == [
        "Data issue: 3 of 3 candidates held for invalid collection days or unreadable evidence"]
    assert za["held_back"]["count"] == 3
    assert {(i["item_id"], i["reason"], i["reason_text"]) for i in za["held_back"]["items"]} == {
        (f"za{n}", "data_issue", "Evidence could not be read") for n in (1, 2, 3)}
    assert [c["market"] for c in r.confirm.calls] == ["NG", "KE"]
    assert all(c["item_id"][:2] != "za" for call in r.confirm.calls for c in call["candidates"])
    assert all_cards(payload(r, "NG"))


def test_past_the_deadline_before_any_start_every_candidate_is_held_back(monkeypatch):
    for name in ("FORCE_RERUN", "RUN_DATE", "BRIEF_RECOVERY_UNTIL"):
        monkeypatch.delenv(name, raising=False)
    r = brief(world(n=3), clock=lambda: LATE)
    assert r.model.calls == []
    assert checks(r) == []
    assert r.counts["explanation_stop"] == {
        "reason": "deadline", "attempted": 0,
        "skipped": [{"market": m, "item_id": item(m, i)} for i in (1, 2, 3) for m in MARKETS],
    }
    for m in MARKETS:
        assert rows(r)[m]["status"] == "partial"
        p = payload(r, m)
        assert p["cards"] == [] and p["more"] == []
        assert p["held_back"]["count"] == 3
        assert all(c["reason"] == "explanation_failed" and len(c["evidence"]) == 3
                   for c in p["held_back"]["items"])
        assert p["headline"] is None
    assert r.chain.finished[0]["status"] == "ok"


@pytest.mark.parametrize("hour,minute", [(6, 10), (6, 20)])
def test_forced_same_day_recovery_extends_confirm_and_explain_and_reports_check_time(monkeypatch, hour, minute):
    monkeypatch.setenv("FORCE_RERUN", "1")
    monkeypatch.setenv("RUN_DATE", D.isoformat())
    monkeypatch.setenv("BRIEF_RECOVERY_UNTIL", "06:30")
    checked_at = datetime.combine(D, time(hour, minute), SAST)
    confirmed = []

    def confirm(candidates, **kwargs):
        confirmed.append((kwargs["market"], [c["item_id"] for c in candidates]))
        return {}

    model = FakeModel()
    r = brief(world(n=1), model=model, confirm=confirm, clock=lambda: checked_at)

    assert len(confirmed) == 3
    assert len(model.writer_calls()) == 3
    assert "explanation_stop" not in r.counts
    assert all({"kind": "late_run", "text": f"Late run: checked at {checked_at:%H:%M}"} in payload(r, m)["banners"]
               for m in MARKETS)


@pytest.mark.parametrize("force,run_date,recovery_until", [
    (None, D.isoformat(), "06:30"),
    ("1", (D + timedelta(days=1)).isoformat(), "06:30"),
    ("1", D.isoformat(), "6:30"),
])
def test_recovery_is_ignored_without_force_same_day_run_date_or_valid_time(
        monkeypatch, force, run_date, recovery_until):
    if force is None:
        monkeypatch.delenv("FORCE_RERUN", raising=False)
    else:
        monkeypatch.setenv("FORCE_RERUN", force)
    monkeypatch.setenv("RUN_DATE", run_date)
    monkeypatch.setenv("BRIEF_RECOVERY_UNTIL", recovery_until)
    confirmed = []
    model = FakeModel()
    r = brief(world(n=1), model=model, confirm=lambda *args, **kwargs: confirmed.append(args),
              clock=lambda: LATE)

    assert confirmed == []
    assert model.calls == []
    assert r.counts["explanation_stop"]["reason"] == "deadline"
    assert all(b["kind"] != "late_run" for m in MARKETS for b in payload(r, m)["banners"])


def test_recovery_deadline_expires_at_the_configured_minute(monkeypatch):
    monkeypatch.setenv("FORCE_RERUN", "1")
    monkeypatch.setenv("RUN_DATE", D.isoformat())
    monkeypatch.setenv("BRIEF_RECOVERY_UNTIL", "06:30")
    confirmed = []
    model = FakeModel()
    checked_at = datetime.combine(D, time(6, 30), SAST)
    r = brief(world(n=1), model=model, confirm=lambda *args, **kwargs: confirmed.append(args),
              clock=lambda: checked_at)

    assert confirmed == []
    assert model.calls == []
    assert r.counts["explanation_stop"] == {
        "reason": "deadline", "attempted": 0,
        "skipped": [{"market": m, "item_id": item(m, 1)} for m in MARKETS],
    }
    assert all({"kind": "late_run", "text": "Late run: checked at 06:30"} in payload(r, m)["banners"]
               for m in MARKETS)


def test_model_admission_refuses_non_current_data_days_even_when_run_date_is_overridden(monkeypatch):
    by_market = job._candidates(Client(world(n=1)), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    tasks = job._in_rank_order(by_market)
    clock = lambda: EARLY
    chain = SimpleNamespace(today=live_chain.today, past_deadline=lambda now, run_date: False)
    model = FakeModel()
    spend = {"usd": 0.0}

    for offset in (-1, 1):
        data_day = D + timedelta(days=offset)
        monkeypatch.setenv("RUN_DATE", data_day.isoformat())
        results, unavailable, stop = job._explain_all(tasks[:1], model=model, base_usd=0.0, spend=spend,
                                                      clock=clock, chain=chain, d=data_day, workers=1)
        assert chain.today(clock()) == data_day
        assert results == {}
        assert unavailable is None
        assert stop is None

    assert model.calls == []
    assert spend["usd"] == 0.0


def test_mid_explanation_accounting_day_rollover_stops_calls_and_books_completed_usage():
    instants = iter((EARLY, EARLY, datetime.combine(D + timedelta(days=1), time(0), SAST)))
    by_market = job._candidates(Client(world(n=1)), D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    tasks = job._in_rank_order(by_market)
    model = FakeModel(usd=0.01)
    spend = {"usd": 0.0}

    results, unavailable, stop = job._explain_all(tasks[:1], model=model, base_usd=0.0, spend=spend,
                                                  clock=lambda: next(instants), chain=FakeChain(), d=D, workers=1)

    assert len(model.calls) == 1
    assert results[id(tasks[0])]["reason"] == "model_error"
    assert spend["usd"] == pytest.approx(0.01)
    assert unavailable is None
    assert stop is None


def test_at_the_deadline_candidates_not_yet_explained_are_held_back():
    model = FakeModel()
    r = brief(world(n=3), model=model, workers=1, clock=lambda: LATE if model.writer_calls() else EARLY)
    assert len(model.writer_calls()) == 1
    za = payload(r, "ZA")
    assert [c["item_id"] for c in za["cards"]] == ["za1"]
    assert {c["item_id"] for c in za["held_back"]["items"]} == {"za2", "za3"}
    assert za["headline"]["item_id"] == "za1"
    for m in MARKETS:
        assert rows(r)[m]["status"] == "partial"
    assert all(payload(r, m)["cards"] == [] and payload(r, m)["held_back"]["count"] == 3 for m in ("NG", "KE"))
    assert payload(r, "NG")["headline"] is None


def test_explanations_run_one_at_a_time_in_rank_order_across_markets():
    started, lock = [], threading.Lock()
    active, max_active = 0, 0

    class Slow(FakeModel):
        def complete_json(self, **kw):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                if "claims" in kw["schema"]["properties"]:
                    started.append(re.search(r"#(\w+)", kw["user"]).group(1))
            time_module.sleep(0.005)
            try:
                return super().complete_json(**kw)
            finally:
                with lock:
                    active -= 1

    brief(world(n=3), model=Slow(), workers=5)
    assert max_active == 1
    assert started == ["za1", "ng1", "ke1", "za2", "ng2", "ke2", "za3", "ng3", "ke3"]
    assert len(started) == 9


def test_the_model_cap_reached_mid_run_holds_the_rest_back(monkeypatch):
    pin_brief_model_cap(monkeypatch, CAP_AFTER_EXPIRY)
    con = world(n=3)
    duck.load(con, "agent.runs", [
        {**run("brief", D, run_id="brief-earlier", status="failed"), "counts": json.dumps({"model_usd": 18.9})},
        {**run("ask", D, run_id="ask-1"), "record": json.dumps({"run": {"model_usd": 0.5}})},
        {**run("ask", day(1), run_id="ask-old"), "record": json.dumps({"run": {"model_usd": 10.0}})},
        {**run("collect", D, run_id="collect-noise"), "counts": json.dumps({"credits": 50.0})},
    ])
    model = FakeModel(usd=0.1)
    r = brief(con, model=model, workers=1)
    za = payload(r, "ZA")
    assert [c["item_id"] for c in za["cards"]] == ["za1"]
    assert {c["item_id"] for c in za["held_back"]["items"]} == {"za2", "za3"}
    assert all(payload(r, m)["cards"] == [] and payload(r, m)["held_back"]["count"] == 3 for m in ("NG", "KE"))
    # za1's six calls (writer, three claim checks, sentence check, critic) use the 0.6 left; no later writer starts.
    assert len(model.writer_calls()) == 1
    assert r.counts["model_usd"] == pytest.approx(0.6)
    assert 19.4 + r.counts["model_usd"] <= 20.0
    assert {rows(r)[m]["status"] for m in MARKETS} == {"partial"}


def test_job_cap_uses_the_shared_reader_again_after_the_temporary_window_expires(monkeypatch):
    instants = iter((CAP_BEFORE_EXPIRY, CAP_AFTER_EXPIRY))
    caps = []

    def cap_at_job_decision():
        cap = shared_model_daily_usd(now=next(instants))
        caps.append(cap)
        return cap

    monkeypatch.setattr(job, "model_daily_usd", cap_at_job_decision)
    monkeypatch.setattr(explain, "model_daily_usd",
                        lambda: shared_model_daily_usd(now=CAP_BEFORE_EXPIRY))
    con = world(n=1)
    duck.load(con, "agent.runs", [{**run("brief", D, run_id="brief-earlier", status="failed"),
                                   "counts": json.dumps({"model_usd": 25.0})}])
    model = FakeModel(usd=0.1)
    r = brief(con, model=model, workers=1)

    assert caps == [80.0, 20.0]
    assert len(model.writer_calls()) == 1
    assert payload(r, "ZA")["cards"][0]["explained"] is True
    assert all(payload(r, market)["cards"] == [] and payload(r, market)["held_back"]["count"] == 1
               for market in ("NG", "KE"))


def test_five_explanations_cannot_collectively_overrun_the_model_cap(monkeypatch):
    pin_brief_model_cap(monkeypatch, CAP_BEFORE_EXPIRY)
    monkeypatch.setattr(explain, "_estimate_usd", lambda *args: 0.1)
    con = world(n=2)
    duck.load(con, "agent.runs", [{**run("brief", D, run_id="brief-earlier", status="failed"),
                                   "counts": json.dumps({"model_usd": 79.75})}])
    model = FakeModel(usd=0.1)
    r = brief(con, model=model, workers=5)

    assert r.counts["model_usd"] == pytest.approx(0.2)
    assert 79.75 + r.counts["model_usd"] <= 80.0
    assert 79.75 + r.counts["model_usd"] == pytest.approx(79.95)
    assert 79.75 + r.counts["model_usd"] <= 80.0


def test_once_one_explanation_hits_the_cap_no_later_one_starts_even_if_it_would_fit(monkeypatch):
    pin_brief_model_cap(monkeypatch, CAP_AFTER_EXPIRY)
    # ng1's long label makes its writer estimate too big for what is left; ke1's smaller one would still fit.
    con = world(n=1)
    con.execute("UPDATE core.cultural_map SET label = ? WHERE item_id = 'ng1'", ["#ng1 " + "long " * 12000])
    duck.load(con, "agent.runs", [{**run("brief", D, run_id="brief-earlier", status="failed"),
                                   "counts": json.dumps({"model_usd": 19.4})}])
    model = FakeModel(usd=0.1)
    r = brief(con, model=model, workers=1)
    assert len(model.writer_calls()) == 1
    assert payload(r, "ZA")["cards"][0]["explained"] is True
    assert all(payload(r, market)["cards"] == [] and payload(r, market)["held_back"]["count"] == 1
               for market in ("NG", "KE"))


def test_spend_already_at_the_cap_explains_nothing(monkeypatch):
    pin_brief_model_cap(monkeypatch, CAP_AFTER_EXPIRY)
    con = world(n=2)
    duck.load(con, "agent.runs", [{**run("brief", D, run_id="brief-earlier", status="failed"),
                                   "counts": json.dumps({"model_usd": 20.0})}])
    r = brief(con)
    assert r.model.calls == []
    assert all(payload(r, m)["cards"] == [] and payload(r, m)["held_back"]["count"] == 2 for m in MARKETS)


# Claim checks


def test_claim_checks_rows_carry_the_brief_id_and_only_the_contract_keys():
    r = brief(world(n=2))
    rows_ = checks(r)
    assert rows_
    assert all(set(c) == CHECK_KEYS for c in rows_)
    assert all(c["run_id"] == "brief-20260920-fake" for c in rows_)
    ids = {c["answer_or_brief_id"] for c in rows_}
    assert ids == {f"brief-20260920-fake:{m}:{item(m, i)}" for m in MARKETS for i in (1, 2)}
    za1 = [c for c in rows_ if c["answer_or_brief_id"].endswith(":ZA:za1")]
    assert {c["rule"] for c in za1} >= {"K1", "K2", "K3", "K4", "K5", "K6", "K8", "K10"}
    assert {c["checker"] for c in za1} == {"code", "model"}
    assert {c["claim_id"] for c in za1 if c["rule"] == "K4"} == {"c1", "c2", "c3", None}
    critic = [c for c in rows_ if c["rule"] == "critic"]
    assert len(critic) == 6
    assert all(c["claim_id"] is None and c["verdict"] == "pass" and c["checker"] == "model" for c in critic)


def test_a_simpler_explanation_the_critic_cannot_rule_out_is_held_back():
    r = brief(world(n=1), model=FakeModel(ruled_out=False))
    for m in MARKETS:
        p = payload(r, m)
        assert p["cards"] == [] and p["headline"] is None
        [held] = p["held_back"]["items"]
        assert held["reason"] == "explanation_failed" and len(held["evidence"]) == 3
    assert "explanation_stop" not in r.counts
    critic = [c for c in checks(r) if c["rule"] == "critic"]
    assert len(critic) == 3 and all(c["verdict"] == "cut" and c["claim_id"] is None for c in critic)


# Banners, moments, boards and the headline


def test_the_warm_up_banner_counts_days_since_the_first_ok_collect_run():
    r = brief(world(n=1, first_collect=day(2)))
    for m in MARKETS:
        assert {"kind": "warming_up", "text": "Warming up: day 3 of 14"} in payload(r, m)["banners"]


def test_no_warm_up_banner_after_day_fourteen():
    r = brief(world(n=1, first_collect=day(14)))
    assert all(b["kind"] != "warming_up" for m in MARKETS for b in payload(r, m)["banners"])
    r = brief(world(n=1, first_collect=day(13)))
    assert {"kind": "warming_up", "text": "Warming up: day 14 of 14"} in payload(r, "ZA")["banners"]


def test_a_market_with_over_30_percent_held_for_data_carries_the_data_issue_banner():
    held = {f"za{i}": {"valid_days": [False, True, True]} for i in (1, 2, 3)}
    r = brief(world(n=7), ctx=FakeCtx(held))
    za = payload(r, "ZA")
    assert [b["kind"] for b in za["banners"]].count("data_issue") == 1
    assert za["status"] == "data_issue" and rows(r)["ZA"]["status"] == "data_issue"
    assert all(b["kind"] != "data_issue" for b in payload(r, "NG")["banners"])


# Candidates G1 holds (invalid data days) stay in held_back but do not take one of the 10 judged slots. On 3 Oct
# staging board_youtube failed on 2 Oct and youtube-led items held by G1 took 24 of the 30 slots across the markets.

BAD_DAYS = {"valid_days": [True, False, True]}


def _called(ctx, market):
    """The items whose gate context was built for market, sorted by rank (packs are built in parallel)."""
    return sorted((c["item_id"] for c in ctx.calls if c["market"] == market), key=lambda i: int(i[2:]))


def _mentions(prompts, item_id):
    return re.search(rf"#{item_id}\b|\b{item_id}_p\d", prompts) is not None


def test_candidates_held_by_g1_do_not_starve_the_judged_slots():
    g1 = [item("ZA", i) for i in range(1, 9)]
    ctx = FakeCtx({i: BAD_DAYS for i in g1})
    r = brief(world(n=30, markets=("ZA",)), ctx=ctx)
    assert _called(ctx, "ZA") == [item("ZA", i) for i in range(1, 19)]
    p = payload(r, "ZA")
    assert [c["item_id"] for c in all_cards(p)] == [item("ZA", i) for i in range(9, 19)]
    held = held_items(r, "ZA")
    assert set(held) == set(g1)
    assert {i: (held[i]["rule"], held[i]["reason"]) for i in g1} == {i: ("G1", "data_issue") for i in g1}
    assert all(held[i]["reason_text"] == "Data issue: 1 of the last 3 market-days invalid on the main platform"
               for i in g1)


def test_backfilled_rows_held_by_g1_are_backfilled_again_in_rank_order():
    g1 = [item("ZA", i) for i in (2, 4, 6, 8, 10, 12, 14)]
    by_market = job._candidates(Client(world(n=30, markets=("ZA",))), D,
                                build_ctx=FakeCtx({i: BAD_DAYS for i in g1}), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    ids = [c["row"]["item_id"] for c in by_market["ZA"]]
    assert ids == [item("ZA", i) for i in range(1, 18)]
    assert [c["row"]["item_id"] for c in by_market["ZA"] if c["decision"].rule != "G1"] == [
        item("ZA", i) for i in (1, 3, 5, 7, 9, 11, 13, 15, 16, 17)]


def test_27_g1_holds_ahead_of_10_clean_rows_still_yield_10_judged_candidates():
    # Staging, 3 Oct: about 27 youtube-led G1 holds per market left only 3 non-G1 items judged out of a pool of 30.
    g1 = [item("ZA", i) for i in range(1, 28)]
    ctx = FakeCtx({i: BAD_DAYS for i in g1})
    by_market = job._candidates(Client(world(n=37, markets=("ZA",))), D, build_ctx=ctx, campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    judged = [c["row"]["item_id"] for c in by_market["ZA"] if c["decision"].rule != "G1"]
    assert judged == [item("ZA", i) for i in range(28, 38)]
    assert _called(ctx, "ZA") == [item("ZA", i) for i in range(1, 38)]  # prepared only as needed, never the pool


def test_the_candidates_read_returns_exactly_the_pool():
    limits = re.findall(r"\bLIMIT\s+(\d+)\s*;?\s*$", job.QUERIES["candidates"], re.IGNORECASE | re.MULTILINE)
    assert [int(n) for n in limits] == [job.POOL]
    assert job.POOL == 90 and job.CANDIDATES == 10


# The brief's task timeout (chain.TIMEOUTS["brief"], 1 h) ends the job whatever the hour, and the 06:15 deadline
# only stops work in time when the brief starts after about 05:15. So no backfill round and no explanation starts
# once the run is FINISH_MARGIN short of the timeout; what is left publishes exactly as at the deadline.

DAWN = datetime.combine(D, time(4, 0), SAST)  # early enough that 06:15 never stops this run
TIME_LIMIT = live_chain.TIMEOUTS["brief"] - timedelta(minutes=8)


def _no_recovery(monkeypatch):
    for name in ("FORCE_RERUN", "RUN_DATE", "BRIEF_RECOVERY_UNTIL"):
        monkeypatch.delenv(name, raising=False)


def test_the_time_limit_is_the_brief_task_timeout_less_an_8_minute_finish_margin(monkeypatch):
    assert job.FINISH_MARGIN == timedelta(minutes=8)
    assert not live_chain.past_deadline(DAWN + TIME_LIMIT, D)
    assert job._out_of_time(DAWN + TIME_LIMIT - timedelta(seconds=1), DAWN) is False
    assert job._out_of_time(DAWN + TIME_LIMIT, DAWN) is True
    assert job._out_of_time(DAWN + TIME_LIMIT, None) is False
    monkeypatch.setitem(live_chain.TIMEOUTS, "brief", timedelta(minutes=30))  # read from the chain, not a copy
    assert job._out_of_time(DAWN + timedelta(minutes=22), DAWN) is True
    assert job._out_of_time(DAWN + timedelta(minutes=21, seconds=59), DAWN) is False


def test_an_explanation_queue_that_crosses_the_time_limit_holds_the_rest_as_at_the_deadline(monkeypatch):
    _no_recovery(monkeypatch)
    model = FakeModel()
    r = brief(world(n=3), model=model, workers=1,
              clock=lambda: DAWN + (TIME_LIMIT if model.writer_calls() else timedelta(0)))
    assert len(model.writer_calls()) == 1
    assert r.counts["explanation_stop"] == {
        "reason": "deadline", "limit": "task_timeout", "attempted": 1,
        "skipped": [{"market": m, "item_id": item(m, i)} for i in (1, 2, 3) for m in MARKETS][1:],
    }
    assert [c["item_id"] for c in all_cards(payload(r, "ZA"))] == ["za1"]
    for m in MARKETS:
        assert rows(r)[m]["status"] == "partial"
        held = payload(r, m)["held_back"]["items"]
        assert {c["item_id"] for c in held} == {item(m, i) for i in ((2, 3) if m == "ZA" else (1, 2, 3))}
        assert all(c["reason"] == "explanation_failed" and len(c["evidence"]) == 3 for c in held)
    assert r.chain.finished[0]["status"] == "ok"

    # The same cards and holds as a run stopped by 06:15 at the same point.
    model = FakeModel()
    at_deadline = brief(world(n=3), model=model, workers=1, clock=lambda: LATE if model.writer_calls() else EARLY)
    for m in MARKETS:
        assert all_cards(payload(r, m)) == all_cards(payload(at_deadline, m))
        assert payload(r, m)["held_back"] == payload(at_deadline, m)["held_back"]


def test_explanations_short_of_the_time_limit_all_run(monkeypatch):
    _no_recovery(monkeypatch)
    model = FakeModel()
    r = brief(world(n=3), model=model, workers=1,
              clock=lambda: DAWN + (TIME_LIMIT - timedelta(seconds=1) if model.writer_calls() else timedelta(0)))
    assert len(model.writer_calls()) == 9
    assert "explanation_stop" not in r.counts


@pytest.mark.parametrize("late,judged", [
    ("time_limit", (9, 10)),
    ("deadline", (9, 10)),
    ("in_time", tuple(range(9, 19))),
])
def test_no_backfill_round_starts_past_the_time_limit_or_the_deadline(monkeypatch, late, judged):
    _no_recovery(monkeypatch)
    g1 = [item("ZA", i) for i in range(1, 9)]
    ctx = FakeCtx({i: BAD_DAYS for i in g1})
    start, then = {"time_limit": (DAWN, DAWN + TIME_LIMIT), "deadline": (EARLY, LATE),
                   "in_time": (DAWN, DAWN + TIME_LIMIT - timedelta(seconds=1))}[late]
    by_market = job._candidates(Client(world(n=30, markets=("ZA",))), D, build_ctx=ctx, campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent",
                                chain=FakeChain(), clock=lambda: then if ctx.calls else start, started=start)
    assert _called(ctx, "ZA") == [item("ZA", i) for i in range(1, max(judged) + 1)]
    assert [c["row"]["item_id"] for c in by_market["ZA"] if not job._held_g1(c)] == [item("ZA", i) for i in judged]


def test_a_run_past_the_time_limit_after_its_first_round_judges_and_holds_only_that_round(monkeypatch):
    _no_recovery(monkeypatch)
    g1 = [item("ZA", i) for i in range(1, 9)]
    ctx = FakeCtx({i: BAD_DAYS for i in g1})
    model = FakeModel()
    r = brief(world(n=30, markets=("ZA",)), ctx=ctx, model=model, workers=1,
              clock=lambda: DAWN + (TIME_LIMIT if ctx.calls else timedelta(0)))
    assert _called(ctx, "ZA") == [item("ZA", i) for i in range(1, 11)]
    assert model.calls == []
    assert r.counts["explanation_stop"] == {
        "reason": "deadline", "limit": "task_timeout", "attempted": 0,
        "skipped": [{"market": "ZA", "item_id": "za9"}, {"market": "ZA", "item_id": "za10"}],
    }
    held = held_items(r, "ZA")
    assert set(held) == set(g1) | {"za9", "za10"}
    assert {held[i]["reason"] for i in g1} == {"data_issue"}
    assert {held[i]["reason"] for i in ("za9", "za10")} == {"explanation_failed"}


def test_no_more_than_the_pool_of_candidates_is_processed_per_market_and_none_held_by_g1_reach_the_model():
    pool = job.POOL
    con = world(n=pool + 2, markets=("ZA", "KE"))
    g1 = {item("ZA", i): BAD_DAYS for i in range(1, pool + 3)}
    g1.update({item("KE", i): BAD_DAYS for i in range(1, pool - 5)})
    ctx = FakeCtx(g1)
    r = brief(con, ctx=ctx)
    assert _called(ctx, "ZA") == [item("ZA", i) for i in range(1, pool + 1)]
    # pool - 6 held by G1, then the 6 left in the pool
    assert _called(ctx, "KE") == [item("KE", i) for i in range(1, pool + 1)]
    za = payload(r, "ZA")
    assert all_cards(za) == [] and za["held_back"]["count"] == pool
    assert {i["rule"] for i in za["held_back"]["items"]} == {"G1"}
    ke = payload(r, "KE")
    assert [c["item_id"] for c in all_cards(ke)] == [item("KE", i) for i in range(pool - 5, pool + 1)]
    prompts = "\n".join(c["user"] for c in r.model.calls)
    assert not [i for i in g1 if _mentions(prompts, i)]
    assert len(r.model.writer_calls()) == 6
    confirmed = {c["item_id"] for call in r.confirm.calls for c in call["candidates"]}
    assert not confirmed & set(g1)
    assert not [c for c in checks(r) if c["answer_or_brief_id"].rsplit(":", 1)[1] in g1]


def test_the_data_issue_banner_counts_every_candidate_considered():
    # 4 G1 holds plus 10 judged: 4 of 14 is under 30%, so no banner.
    r = brief(world(n=30, markets=("ZA",)), ctx=FakeCtx({item("ZA", i): BAD_DAYS for i in range(1, 5)}))
    za = payload(r, "ZA")
    assert len(all_cards(za)) == 10 and za["held_back"]["count"] == 4
    assert all(b["kind"] != "data_issue" for b in za["banners"])
    assert za["status"] == "published"
    # 5 G1 holds plus 10 judged: 5 of 15 is over 30%.
    r = brief(world(n=30, markets=("ZA",)), ctx=FakeCtx({item("ZA", i): BAD_DAYS for i in range(1, 6)}))
    za = payload(r, "ZA")
    assert len(all_cards(za)) == 10 and za["held_back"]["count"] == 5
    assert {"kind": "data_issue", "text": "Data issue: 5 of 15 candidates held for invalid collection days or "
                                          "unreadable evidence"} in za["banners"]
    assert za["status"] == "data_issue"
    # Every candidate in the top 30 held by G1: 30 of 30.
    r = brief(world(n=30, markets=("ZA",)), ctx=FakeCtx({item("ZA", i): BAD_DAYS for i in range(1, 31)}))
    assert {"kind": "data_issue", "text": "Data issue: 30 of 30 candidates held for invalid collection days or "
                                          "unreadable evidence"} in payload(r, "ZA")["banners"]


def test_an_unreadable_evidence_pack_still_takes_a_judged_slot(monkeypatch):
    real = job.build_pack

    def build_pack(client, row, d, market, **kw):
        if row["item_id"] == "za1":
            raise RuntimeError("pack read failed")
        return real(client, row, d, market, **kw)

    monkeypatch.setattr(job, "build_pack", build_pack)
    ctx = FakeCtx()
    brief(world(n=30, markets=("ZA",)), ctx=ctx)
    assert _called(ctx, "ZA") == [item("ZA", i) for i in range(2, 11)]  # za1 failed before its context


def test_moments_come_from_calendar_rows_in_the_next_14_days():
    con = world(n=1)
    duck.load(con, "core.calendar", [
        {"moment_date": D + timedelta(days=3), "market": "ZA", "name": "Heritage Day", "kind": "holiday",
         "source": "date.nager.at", "item_ids": []},
        {"moment_date": D + timedelta(days=14), "market": "ZA", "name": "Edge", "kind": "festival",
         "source": "moments.yaml", "item_ids": ["za1"]},
        {"moment_date": D + timedelta(days=15), "market": "ZA", "name": "Too far", "kind": "holiday",
         "source": "moments.yaml", "item_ids": []},
        {"moment_date": day(1), "market": "ZA", "name": "Past", "kind": "holiday", "source": "moments.yaml",
         "item_ids": []},
        {"moment_date": D, "market": "NG", "name": "Other market", "kind": "holiday", "source": "moments.yaml",
         "item_ids": []},
    ])
    r = brief(con)
    assert payload(r, "ZA")["moments"] == [
        {"date": "2026-09-23", "name": "Heritage Day", "kind": "holiday", "source": "calendar", "item_ids": []},
        {"date": "2026-10-04", "name": "Edge", "kind": "festival", "source": "calendar", "item_ids": ["za1"]},
    ]
    assert [x["name"] for x in payload(r, "NG")["moments"]] == ["Other market"]
    assert payload(r, "KE")["moments"] == []


def test_boards_come_from_today_board_entries_never_x_trends():
    con = world(n=3)
    duck.load(con, "agent.runs", [run("collect", D)])
    duck.load(con, "core.item_counter_daily", [
        counter("za2", "board_tiktok_hashtag", D, 1, unit="rank", is_board=True, pull_seq=1),
        counter("za1", "board_tiktok_hashtag", D, 2, unit="rank", is_board=True, pull_seq=1),
        counter("za1", "board_tiktok_hashtag", D, 1, unit="appearances", is_board=True, pull_seq=1),
        counter("za3", "x_trends", D, 1, unit="rank", is_board=True, platform="x", pull_seq=1),
        counter("za3", "board_tiktok_hashtag", day(1), 1, unit="rank", is_board=True, pull_seq=1,
                read_day=day(1)),
        counter("za3", "feed_tiktok", D, 4, unit="appearances", is_board=False, pull_seq=1),
        counter("ng1", "board_youtube", D, 1, unit="appearances", is_board=True, market="NG", platform="youtube",
                pull_seq=1),
    ])
    r = brief(con)
    assert payload(r, "ZA")["boards"] == [{"platform": "tiktok", "list": "TikTok hashtag board", "entries": [
        {"rank": 1, "title": "#za2", "item_id": "za2"}, {"rank": 2, "title": "#za1", "item_id": "za1"}]}]
    assert payload(r, "NG")["boards"] == [{"platform": "youtube", "list": "YouTube trending board", "entries": [
        {"rank": None, "title": "#ng1", "item_id": "ng1"}]}]
    assert payload(r, "KE")["boards"] == []


def test_the_headline_is_the_rank_one_explained_card_else_null():
    r = brief(world(n=2))
    za = payload(r, "ZA")
    top = za["cards"][0]
    assert za["headline"] == {"text": top["explanation"], "market": "ZA", "item_id": "za1",
                              "claim_ids": top["explanation_claim_ids"]}
    assert za["headline"]["text"] and za["headline"]["claim_ids"] == ["c1", "c3"]

    model = FakeModel()
    r = brief(world(n=2), model=model, workers=1, clock=lambda: LATE if model.writer_calls() else EARLY)
    assert payload(r, "ZA")["headline"]["item_id"] == "za1"
    assert payload(r, "NG")["headline"] is None and payload(r, "KE")["headline"] is None


# The chain


def test_upstream_not_ready_before_the_deadline_exits_1_without_writing():
    con = world(n=2)
    client, chain, model = Client(con), FakeChain(raises="upstream"), FakeModel()
    code = job.main(client=client, chain=chain, model=model, make_sc=lambda run_id: SC, clock=lambda: EARLY, build_ctx=FakeCtx(),
                    confirm=FakeConfirm(), campaign_hashtags=[], political_terms={m: [] for m in MARKETS},
                    core="core", agent="agent")
    assert code == 1
    assert client.inserted == {}
    assert chain.finished == []
    assert model.calls == []


def test_upstream_not_ready_past_the_deadline_publishes_three_data_issue_rows():
    con = world(n=2)
    duck.load(con, "core.calendar", [{"moment_date": D + timedelta(days=2), "market": "KE", "name": "Mashujaa",
                                      "kind": "holiday", "source": "date.nager.at", "item_ids": []}])
    client, chain, model = Client(con), FakeChain(raises="upstream"), FakeModel()
    code = job.main(client=client, chain=chain, model=model, make_sc=lambda run_id: SC, clock=lambda: LATE, build_ctx=FakeCtx(),
                    confirm=FakeConfirm(), campaign_hashtags=[], political_terms={m: [] for m in MARKETS},
                    core="core", agent="agent")
    assert code == 0
    briefs = {r["market"]: r for r in client.inserted["agent.briefs"]}
    assert sorted(briefs) == sorted(MARKETS)
    for m in MARKETS:
        assert set(briefs[m]) == ROW_KEYS
        assert briefs[m]["status"] == "data_issue"
        assert briefs[m]["run_id"] == "brief-20260920-fake"
        p = json.loads(briefs[m]["payload"])
        assert set(p) == MARKET_KEYS
        assert p["status"] == "data_issue"
        assert p["cards"] == [] and p["more"] == [] and p["headline"] is None
        assert [b["kind"] for b in p["banners"]] == ["warming_up", "data_issue"]
    assert json.loads(briefs["KE"]["payload"])["moments"][0]["name"] == "Mashujaa"
    assert "agent.claim_checks" not in client.inserted
    assert model.calls == []
    [fin] = chain.finished
    assert fin["run"].run_id == "brief-20260920-fake"
    assert fin["status"] == "ok"
    assert fin["counts"] == {"markets": 3, "cards": 0, "held": 0, "credits": 0, "model_usd": 0.0}
    assert "upstream detect" in fin["error"]


def test_already_done_exits_0_without_writing():
    client, chain = Client(world(n=1)), FakeChain(raises="done")
    code = job.main(client=client, chain=chain, model=FakeModel(), make_sc=lambda run_id: SC, clock=lambda: EARLY, build_ctx=FakeCtx(),
                    confirm=FakeConfirm(), campaign_hashtags=[], political_terms={m: [] for m in MARKETS},
                    core="core", agent="agent")
    assert code == 0
    assert client.inserted == {}
    assert chain.finished == []


def test_a_failure_after_begin_finishes_the_run_failed_and_exits_1():
    client, chain = Client(world(n=1)), FakeChain()
    code = job.main(client=client, chain=chain, model=FakeModel(), make_sc=lambda run_id: SC, clock=lambda: EARLY,
                    build_ctx=FakeCtx(error=RuntimeError("health table missing")), confirm=FakeConfirm(),
                    campaign_hashtags=[], political_terms={m: [] for m in MARKETS}, core="core", agent="agent")
    assert code == 1
    assert "agent.briefs" not in client.inserted
    [fin] = chain.finished
    assert fin["status"] == "failed"
    assert "health table missing" in fin["error"]
    assert fin["counts"]["model_usd"] == 0


def test_main_runs_a_normal_morning_to_exit_0():
    client, chain = Client(world(n=1)), FakeChain()
    code = job.main(client=client, chain=chain, model=FakeModel(), make_sc=lambda run_id: SC, clock=lambda: EARLY, build_ctx=FakeCtx(),
                    confirm=FakeConfirm(), campaign_hashtags=[], political_terms={m: [] for m in MARKETS},
                    core="core", agent="agent")
    assert code == 0
    assert len(client.inserted["agent.briefs"]) == 3
    assert chain.finished[0]["status"] == "ok"


# Confirm results, Seasonal items and the SocialCrawl client


def test_confirm_results_reach_the_writer_facts_as_presence_only_and_the_run_counts():
    r = brief(world(n=2), confirm=FakeConfirm(found={"za1": ["instagram", "reddit"]}))
    za1 = next(c["user"] for c in r.model.writer_calls() if "#za1" in c["user"])
    za2 = next(c["user"] for c in r.model.writer_calls() if "#za2" in c["user"])
    assert "- Also found by search since 2026-09-13: instagram, reddit" in za1
    assert "Also found by search" not in za2
    assert r.counts["platforms_found"] == {"ZA": {"za1": ["instagram", "reddit"]}}
    card = payload(r, "ZA")["cards"][0]
    assert card["item_id"] == "za1" and card["explained"] is True
    assert sorted(card["evidence_ids"]) == ["za1_p1", "za1_p2", "za1_p3"]


def test_seasonal_candidates_are_neither_confirmed_nor_explained_and_go_to_moments():
    con = world(n=2)
    add_item(con, "ZA", "za_season", 0.99, state="seasonal", moment="Heritage Day")
    r = brief(con)
    assert "za_season" not in {c["item_id"] for call in r.confirm.calls for c in call["candidates"]}
    assert not any("#za_season" in c["user"] for c in r.model.calls)
    assert not [c for c in checks(r) if c["answer_or_brief_id"].endswith(":za_season")]
    p = payload(r, "ZA")
    assert [m["item_ids"] for m in p["moments"] if m["kind"] == "seasonal_item"] == [["za_season"]]
    assert rows(r)["ZA"]["status"] == "published"


def main_args(**kw):
    return dict(model=FakeModel(), clock=lambda: EARLY, build_ctx=FakeCtx(), campaign_hashtags=[],
                political_terms={m: [] for m in MARKETS}, core="core", agent="agent", **kw)


def test_main_without_a_socialcrawl_client_skips_confirm_with_the_coverage_note(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.collect.socialcrawl_client", None)
    client, chain, confirm = Client(world(n=1)), FakeChain(), FakeConfirm()
    code = job.main(client=client, chain=chain, **main_args(confirm=confirm))
    assert code == 0
    assert confirm.calls == []
    for row in client.inserted["agent.briefs"]:
        assert "thin_coverage" in [b["kind"] for b in json.loads(row["payload"])["banners"]]
    assert chain.finished[0]["status"] == "ok"
    assert "skipped" in chain.finished[0]["counts"]["confirm"]


def test_a_socialcrawl_factory_that_raises_skips_confirm_and_is_built_after_begin():
    client, chain, confirm, made = Client(world(n=1)), FakeChain(), FakeConfirm(), []

    def make_sc(run_id):
        made.append((run_id, list(chain.begun)))
        raise RuntimeError("caps.yaml missing")

    code = job.main(client=client, chain=chain, make_sc=make_sc, **main_args(confirm=confirm))
    assert code == 0
    assert made == [("brief-20260920-fake", [("brief", D)])]
    assert confirm.calls == []
    assert "caps.yaml missing" in chain.finished[0]["counts"]["confirm"]
    assert len(client.inserted["agent.briefs"]) == 3


def test_the_socialcrawl_factory_is_never_called_when_begin_refuses():
    for raises, clock in (("done", EARLY), ("upstream", EARLY), ("upstream", LATE)):
        made = []
        job.main(client=Client(world(n=1)), chain=FakeChain(raises=raises), make_sc=made.append,
                 **{**main_args(confirm=FakeConfirm()), "clock": lambda: clock})
        assert made == []


# A failing or slow confirm lane


class RaisingSC:
    def __init__(self):
        self.calls = 0

    def call(self, route, params=None, **kw):
        self.calls += 1
        raise RuntimeError("raw_responses lookup failed")


def no_confirm(p):
    return [b for b in p["banners"] if b["kind"] == "thin_coverage"]


def test_a_confirm_lane_that_raises_still_publishes_three_briefs_rows():
    sc = RaisingSC()
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm)
    assert sc.calls == 1
    assert sorted(rows(r)) == sorted(MARKETS)
    for m in MARKETS:
        assert no_confirm(payload(r, m))
        assert all(c["explained"] for c in payload(r, m)["cards"])
    assert r.chain.finished[0]["status"] == "ok"
    assert "raw_responses lookup failed" in r.counts["confirm"]


def test_a_confirm_failure_mid_run_stops_confirming_and_keeps_the_credits_spent():
    confirm = FakeConfirm(fail_in={"NG"})
    r = brief(world(n=2), confirm=confirm)
    assert [c["market"] for c in confirm.calls] == ["ZA", "NG"]
    assert r.counts["credits"] == pytest.approx(10.0)
    assert not no_confirm(payload(r, "ZA"))
    assert no_confirm(payload(r, "NG")) and no_confirm(payload(r, "KE"))
    assert "credit_ledger read failed" in r.counts["confirm"]
    assert r.chain.finished[0]["status"] == "ok" and len(rows(r)) == 3


def test_confirm_is_skipped_for_markets_reached_past_the_deadline():
    confirm = FakeConfirm()
    r = brief(world(n=2), confirm=confirm, clock=lambda: LATE if confirm.calls else EARLY)
    assert [c["market"] for c in confirm.calls] == ["ZA"]
    assert not no_confirm(payload(r, "ZA"))
    assert no_confirm(payload(r, "NG")) and no_confirm(payload(r, "KE"))
    assert "deadline" in r.counts["confirm"]
    assert len(rows(r)) == 3


def test_confirm_is_skipped_for_markets_reached_past_the_time_limit(monkeypatch):
    _no_recovery(monkeypatch)
    confirm = FakeConfirm()
    r = brief(world(n=2), confirm=confirm, clock=lambda: DAWN + (TIME_LIMIT if confirm.calls else timedelta(0)))
    assert [c["market"] for c in confirm.calls] == ["ZA"]
    assert not no_confirm(payload(r, "ZA"))
    assert no_confirm(payload(r, "NG")) and no_confirm(payload(r, "KE"))
    assert r.counts["confirm"] == "stopped at the task time limit"
    assert len(rows(r)) == 3


# Confirm finds into posts through lane L1's core.collect.parse.ingest


class LiveSC:
    """A SocialCrawl stand-in for the real confirm lane: every call answers ok with one instagram row."""

    run_id = "brief-20260920-fake"

    def __init__(self, mode="live"):
        self.mode, self.calls = mode, []

    def call(self, route, params=None, *, market=None, item_id=None, lane=None):
        self.calls.append({"route": route, "market": market, "item_id": item_id})
        row = {"platform": "instagram", "post": {"id": f"{item_id}_ig"}}
        return SimpleNamespace(status="ok", route=route, credits_charged=1, items=[row], reason="",
                               body={"data": {"items": [row]}})


class FakeIngest:
    """Stands in for core.collect.parse.ingest: records each call, writes 2 posts and 3 observations."""

    def __init__(self, error=None):
        self.calls, self.error = [], error

    def __call__(self, client, result, market, lane="confirm", **kw):
        self.calls.append({"client": client, "result": result, "market": market, "lane": lane, **kw})
        if self.error:
            raise self.error
        return {"posts": [{}, {}], "observations": [{}, {}, {}]}


def fake_ingest(monkeypatch, **kw):
    from core.collect import parse

    ingest = FakeIngest(**kw)
    monkeypatch.setattr(parse, "ingest", ingest, raising=False)
    return ingest


def per_market(sc):
    return {m: sum(1 for c in sc.calls if c["market"] == m) for m in MARKETS}


def test_confirm_finds_are_ingested_once_per_ok_result_and_counted_per_market(monkeypatch):
    ingest, sc = fake_ingest(monkeypatch), LiveSC()
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm)
    calls = per_market(sc)
    assert all(calls.values())
    assert len(ingest.calls) == len(sc.calls)
    for rec, made in zip(ingest.calls, sc.calls, strict=True):
        assert rec["client"] is r.client and rec["lane"] == "confirm" and rec["market"] == made["market"]
        assert rec["run_id"] == "brief-20260920-fake" and rec["fetched_at"] == EARLY
        assert rec["seed_key"] == made["item_id"] and rec["result"].route == made["route"]
    assert r.counts["ingest"] == {"status": "ok", "by_market": {
        m: {"posts": 2 * calls[m], "observations": 3 * calls[m], "errors": 0} for m in MARKETS}}
    assert r.chain.finished[0]["status"] == "ok" and len(rows(r)) == 3


def test_a_missing_ingest_is_recorded_once_as_unavailable_and_the_brief_finishes(monkeypatch):
    from core.collect import parse

    monkeypatch.delattr(parse, "ingest", raising=False)
    loads, real = [], confirm_lane.load_ingest
    monkeypatch.setattr(confirm_lane, "load_ingest", lambda: loads.append(1) or real())
    sc = LiveSC()
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm)
    assert loads == [1]
    assert r.counts["ingest"] == {"status": "unavailable"}
    assert set(r.counts["platforms_found"]) == set(MARKETS)
    assert "confirm" not in r.counts
    assert r.chain.finished[0]["status"] == "ok" and len(rows(r)) == 3


def test_an_ingest_that_raises_is_counted_and_the_brief_finishes(monkeypatch):
    ingest, sc = fake_ingest(monkeypatch, error=RuntimeError("merge on posts failed")), LiveSC()
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm)
    calls = per_market(sc)
    assert len(ingest.calls) == len(sc.calls)
    assert r.counts["ingest"] == {"status": "ok", "by_market": {
        m: {"posts": 0, "observations": 0, "errors": calls[m]} for m in MARKETS}}
    assert set(r.counts["platforms_found"]) == set(MARKETS)
    assert "confirm" not in r.counts
    for m in MARKETS:
        assert not no_confirm(payload(r, m))
    assert r.chain.finished[0]["status"] == "ok" and len(rows(r)) == 3


def test_no_ingest_in_replay_mode(monkeypatch):
    ingest, sc = fake_ingest(monkeypatch), LiveSC(mode="replay")
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm)
    assert sc.calls and ingest.calls == []
    assert r.counts["ingest"] == {"status": "replay"}
    assert r.chain.finished[0]["status"] == "ok"


def test_ingested_posts_are_not_evidence_in_the_same_run(monkeypatch):
    fake_ingest(monkeypatch)
    r = brief(world(n=2), sc=LiveSC(), confirm=confirm_lane.confirm)
    card = payload(r, "ZA")["cards"][0]
    assert sorted(card["evidence_ids"]) == ["za1_p1", "za1_p2", "za1_p3"]


class CapSC(LiveSC):
    """Answers ok for its first `ok` calls, then the client's cap_reached refusal with nothing charged."""

    def __init__(self, ok=0):
        super().__init__()
        self.ok = ok

    def call(self, route, params=None, *, market=None, item_id=None, lane=None):
        if len(self.calls) < self.ok:
            return super().call(route, params, market=market, item_id=item_id, lane=lane)
        self.calls.append({"route": route, "market": market, "item_id": item_id})
        return SimpleNamespace(status="cap_reached", route=route, credits_charged=0, items=[], reason="share cap",
                               body=None)


def test_a_confirm_lane_refused_by_the_cap_keeps_the_coverage_note_in_every_market_it_did_not_finish():
    sc = CapSC()
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm)
    assert len(sc.calls) == 1
    for m in MARKETS:
        assert no_confirm(payload(r, m))
    assert "cap_reached" in r.counts["confirm"]
    assert r.chain.finished[0]["status"] == "ok" and len(rows(r)) == 3


def test_a_confirm_lane_stopped_part_way_keeps_the_markets_it_finished_and_the_credits_spent():
    finished_za = sum(len(confirm_lane._plan(c, "ZA", D)) for c in
                      [{"kind": "hashtag", "label": "#za1"}, {"kind": "hashtag", "label": "#za2"}])
    sc = CapSC(ok=finished_za)
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm)
    assert per_market(sc)["ZA"] == finished_za and per_market(sc)["KE"] == 0
    assert not no_confirm(payload(r, "ZA"))
    assert no_confirm(payload(r, "NG")) and no_confirm(payload(r, "KE"))
    assert r.counts["credits"] == pytest.approx(finished_za)

    within = CapSC(ok=1)
    r = brief(world(n=2), sc=within, confirm=confirm_lane.confirm)
    assert all(no_confirm(payload(r, m)) for m in MARKETS)
    assert r.counts["credits"] == pytest.approx(1.0)


def test_a_confirm_share_used_up_before_any_call_keeps_the_coverage_note(monkeypatch):
    monkeypatch.setattr(confirm_lane, "load_caps", lambda: {"ENGINE_DAILY": {"confirm": 0}})
    sc = LiveSC()
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm)
    assert sc.calls == []
    for m in MARKETS:
        assert no_confirm(payload(r, m))
    assert "confirm share used" in r.counts["confirm"]


@pytest.mark.parametrize(("start", "late", "note"), [
    (EARLY, LATE, "stopped at the 06:15 deadline"),
    (DAWN, DAWN + TIME_LIMIT, "stopped at the task time limit"),
])
def test_no_confirm_search_starts_once_one_returns_past_the_deadline_or_the_time_limit(monkeypatch, start, late,
                                                                                         note):
    _no_recovery(monkeypatch)
    sc = LiveSC()
    r = brief(world(n=2), sc=sc, confirm=confirm_lane.confirm, clock=lambda: late if sc.calls else start)
    assert len(sc.calls) == 1
    assert r.counts["credits"] == pytest.approx(1.0)
    for m in MARKETS:
        assert no_confirm(payload(r, m))
    assert r.counts["confirm"] == note
    assert len(rows(r)) == 3


# Staging findings: generic tags, thin evidence, a rate-limited model


def held_items(r, market):
    return {i["item_id"]: i for i in payload(r, market)["held_back"]["items"]}


def specificity_evidence(evidence_id, *, market="ZA", source_market=None, text="Dancing to the new sound at home"):
    return {"id": evidence_id, "platform": "tiktok", "handle": f"@{evidence_id}",
            "url": f"https://x/{evidence_id}", "posted_at": "2026-09-29T19:40:00+02:00",
            "market": market, "source_market": source_market, "text": text, "quote_text": text,
            "engagement": {"views": 10}, "flags": [], "thumbnail_url": f"https://img/{evidence_id}.jpg"}


def publication_candidate(evidence, decision=None):
    row = {"item_id": "za_specific", "kind": "hashtag", "state": "rising", "untested": False,
           "main_y": 12, "main_mu": 4.2, "main_ratio": 2.8, "creators3": 31, "posts3": 40,
           "top_creator_share3": 0.1, "authenticity": "clear", "geo_status": "local", "worth_raw": 1.0,
           "title": "#specific", "market_scope": "market", "market_posts7": 6, "total_posts7": 8,
           "market_share7": 0.75}
    return {"row": row, "market": "ZA", "decision": decision or job.Decision(True, "today", None, None, None, False),
            "held_reason": None, "pack": {"evidence": evidence, "numbers": []}, "sparkline": None, "also": []}


def specificity_result(evidence_ids, *, quote_text="Dancing to the new sound", checked=True):
    explanation = "Recent posts show friends dancing to a new sound at home."
    quote = {"evidence_id": evidence_ids[0], "text": quote_text}
    claims = [
        {"id": "c1", "text": "Creators dance to a new sound at home.", "label": "observed",
         "kind": "observation", "evidence_ids": evidence_ids[:2], "quotes": [quote], "numbers": []},
        {"id": "c2", "text": "Friends share the sound in dance clips.", "label": "observed",
         "kind": "observation", "evidence_ids": evidence_ids[1:], "quotes": [], "numbers": []},
    ]
    return {"reason": None, "numbers_only": False, "explanation": explanation,
            "explanation_claim_ids": ["c1", "c2"],
            "claims": claims, "local_why_now_checked": checked,
            "specificity": {"status": "pass", "local_evidence_ids": evidence_ids[:2], "quote": quote,
                            "why_now": explanation, "reason": None}}


def publication_payload(cand, result):
    results = {} if result is None else {id(cand): result}
    return job._market_payload("ZA", D, [cand], results, banners=[], moments_=[], boards_=[], issues=[])


def preflight_candidate(evidence):
    return {"market": "ZA", "ctx": {}, "pack": {"evidence": evidence},
            "row": {"market_scope": "market", "map_status": "active", "nameless": False,
                    "authenticity": "clear", "sponsored_share": 0}}


@pytest.mark.parametrize("reason", ["model_cap", "model_error", "failed_checks"])
def test_market_payload_holds_numbers_only_or_failed_explanations(reason):
    evidence = [specificity_evidence(f"za_p{i}") for i in range(1, 4)]
    cand = publication_candidate(evidence, job.Decision(True, "today", None, None, "G10", True))
    result = specificity_result([e["id"] for e in evidence])
    result["reason"] = reason

    p = publication_payload(cand, result)

    assert p["cards"] == [] and p["more"] == []
    [held] = p["held_back"]["items"]
    assert (held["item_id"], held["reason"], held["reason_text"]) == (
        "za_specific", "explanation_failed", "Explanation failed its checks")
    assert held["evidence_ids"] == [e["id"] for e in evidence]


def test_market_payload_holds_an_unrun_explanation():
    evidence = [specificity_evidence(f"za_p{i}") for i in range(1, 4)]
    cand = publication_candidate(evidence, job.Decision(True, "today", None, None, "G10", True))

    p = publication_payload(cand, None)

    assert p["cards"] == [] and p["held_back"]["items"][0]["reason"] == "explanation_failed"


def test_market_payload_holds_a_checked_result_when_the_gate_marks_numbers_only():
    evidence = [specificity_evidence(f"za_p{i}") for i in range(1, 4)]
    cand = publication_candidate(evidence, job.Decision(True, "today", None, None, "G10", True))
    result = specificity_result([e["id"] for e in evidence])

    p = publication_payload(cand, result)

    assert p["cards"] == [] and p["held_back"]["items"][0]["reason"] == "explanation_failed"


@pytest.mark.parametrize(("publish", "numbers_only"), [(1, False), (True, "false"), (False, False)],
                         ids=["publish-int", "numbers-only-string", "publish-false"])
def test_market_payload_holds_inconsistent_or_malformed_decision_flags(publish, numbers_only):
    evidence = [specificity_evidence(f"za_p{i}") for i in range(1, 4)]
    decision = {"publish": publish, "where": "today", "flag": None, "reason": None, "rule": None,
                "numbers_only": numbers_only}
    cand = publication_candidate(evidence, decision)
    result = specificity_result([e["id"] for e in evidence])

    p = publication_payload(cand, result)

    assert p["cards"] == [] and p["held_back"]["items"][0]["reason"] == "explanation_failed"


def test_market_payload_holds_when_numbers_only_status_is_missing():
    evidence = [specificity_evidence(f"za_p{i}") for i in range(1, 4)]
    cand = publication_candidate(evidence)
    result = specificity_result([e["id"] for e in evidence])
    result.pop("numbers_only")

    p = publication_payload(cand, result)

    assert p["cards"] == [] and p["held_back"]["items"][0]["reason"] == "explanation_failed"


def test_market_payload_recomputes_local_support_instead_of_trusting_a_writer_pass():
    evidence = [specificity_evidence(f"za_p{i}", market=None) for i in range(1, 4)]
    cand = publication_candidate(evidence)
    result = specificity_result([e["id"] for e in evidence])

    p = publication_payload(cand, result)

    assert result["specificity"]["status"] == "pass" and result["local_why_now_checked"] is True
    assert p["cards"] == [] and p["held_back"]["items"][0]["reason"] == "explanation_failed"
    assert p["held_back"]["items"][0]["evidence_ids"] == [e["id"] for e in evidence]


def test_market_payload_recomputes_the_quote_against_the_pack_instead_of_trusting_a_writer_pass():
    evidence = [specificity_evidence(f"za_p{i}") for i in range(1, 4)]
    cand = publication_candidate(evidence)
    result = specificity_result([e["id"] for e in evidence], quote_text="A phrase absent from the source")

    p = publication_payload(cand, result)

    assert result["specificity"]["status"] == "pass"
    assert p["cards"] == [] and p["held_back"]["items"][0]["reason"] == "explanation_failed"


def test_market_payload_holds_when_local_why_now_was_not_checked():
    evidence = [specificity_evidence(f"za_p{i}") for i in range(1, 4)]
    cand = publication_candidate(evidence)
    result = specificity_result([e["id"] for e in evidence], checked=False)

    p = publication_payload(cand, result)

    assert p["cards"] == [] and p["held_back"]["items"][0]["reason"] == "explanation_failed"


def test_market_payload_holds_a_generic_result_without_supported_claim_references():
    evidence = [specificity_evidence(f"za_p{i}") for i in range(1, 4)]
    cand = publication_candidate(evidence)
    result = specificity_result([e["id"] for e in evidence])
    result.update(explanation="This trend is popular.", explanation_claim_ids=[], claims=[])

    p = publication_payload(cand, result)

    assert p["cards"] == [] and p["held_back"]["items"][0]["reason"] == "explanation_failed"


def test_market_payload_emits_recomputed_specificity_for_a_checked_local_result():
    evidence = [specificity_evidence("za_local_1"), specificity_evidence("za_local_2"),
                specificity_evidence("za_unknown", market=None)]
    cand = publication_candidate(evidence)
    result = specificity_result([e["id"] for e in evidence])

    p = publication_payload(cand, result)

    [card] = p["cards"]
    assert card["specificity"] == {
        "status": "pass", "local_evidence_ids": ["za_local_1", "za_local_2"],
        "quote": {"evidence_id": "za_local_1", "text": "Dancing to the new sound"},
        "why_now": result["explanation"], "reason": None,
    }


def test_a_platform_generic_tag_with_the_top_worth_is_held_never_a_card():
    con = world(n=3)
    add_item(con, "ZA", "fyp", 5.0, map_status="generic", eligible=False, top_creator_share3=0.5)
    r = brief(con)
    assert "fyp" not in {c["item_id"] for c in all_cards(payload(r, "ZA"))}
    held = held_items(r, "ZA")["fyp"]
    assert held["reason_text"] == "Platform-generic tag"
    assert held["reason"] == "not_confirmed"
    assert not any("#fyp" in c["user"] for c in r.model.calls)
    assert "fyp" not in {c["item_id"] for call in r.confirm.calls for c in call["candidates"]}


def test_ineligible_rows_rank_after_eligible_ones_whatever_their_worth():
    con = world(n=10, markets=("ZA",))
    add_item(con, "ZA", "fyp", 5.0, map_status="generic", eligible=False)
    r = brief(con)
    assert [c["item_id"] for c in r.confirm.calls[0]["candidates"]] == [item("ZA", i) for i in range(1, 11)]
    assert "fyp" not in {c["item_id"] for c in r.ctx.calls}


def test_a_candidate_with_fewer_than_3_posts_is_held_as_not_confirmed():
    con = world(n=2)
    add_item(con, "NG", "samsung", 0.99, posts=0, state="new_to_42", untested=True, creators3=2)
    add_item(con, "NG", "two_posts", 0.98, posts=2, creators3=2)
    r = brief(con)
    ng = payload(r, "NG")
    assert {c["item_id"] for c in all_cards(ng)} == {"ng1", "ng2"}
    held = held_items(r, "NG")
    for item_id in ("samsung", "two_posts"):
        assert held[item_id]["reason"] == "not_confirmed"
        assert held[item_id]["reason_text"] == "Fewer than 3 posts 42 can show"
        assert not any(f"#{item_id}" in c["user"] for c in r.model.calls)
    assert all(len(c["evidence"]) >= 3 for m in MARKETS for c in all_cards(payload(r, m)))


class RateLimited(FakeModel):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def complete_json(self, **kw):
        self.calls.append({"user": kw["user"], "support": False})
        time_module.sleep(0.2)  # slow enough that calls made in parallel would all reach the model
        raise self.error


def rate_limit_errors():
    from google.genai import errors

    return [errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted",
                                               "status": "RESOURCE_EXHAUSTED"}}),
            RuntimeError("Error code: 429 RESOURCE_EXHAUSTED quota exceeded")]


# A refused call is made again BUSY_RETRIES times; BUSY_TRIP_ITEMS explanations in a row that run out of tries trip
# the breaker (6 Oct 2026: the first refusal used to trip it, and 25 of 30 topics were never tried).
SUSTAINED_CALLS = (job.BUSY_RETRIES + 1) * job.BUSY_TRIP_ITEMS


@pytest.mark.parametrize("error", rate_limit_errors(), ids=["ClientError429", "RESOURCE_EXHAUSTED"])
def test_a_sustained_rate_limit_stops_every_model_call_for_the_run(error, busy_waits):
    model = RateLimited(error)
    r = brief(world(n=4), model=model)
    assert len(model.calls) == SUSTAINED_CALLS
    assert len(busy_waits) == job.BUSY_RETRIES * job.BUSY_TRIP_ITEMS
    for m in MARKETS:
        assert payload(r, m)["cards"] == [] and payload(r, m)["held_back"]["count"] == 4
        assert rows(r)[m]["status"] == "partial"
    assert r.counts["model"]["reason"] == "model_unavailable"
    assert type(error).__name__ in r.counts["model"]["error"]
    assert sorted(r.counts["model"]["numbers_only"]) == sorted(item(m, i) for m in MARKETS for i in range(1, 5))
    assert r.chain.finished[0]["status"] == "ok"


def test_other_model_errors_do_not_stop_the_run():
    model = RateLimited(RuntimeError("503 service unavailable"))
    r = brief(world(n=2), model=model, workers=1)
    assert len(model.calls) == 6
    assert "model" not in r.counts


class StatusError(Exception):
    def __init__(self, message, **attrs):
        super().__init__(message)
        for k, v in attrs.items():
            setattr(self, k, v)


class RateLimitError(Exception):
    """Named like the SDK's class, with no status attribute: the name alone must trip the breaker."""


@pytest.mark.parametrize("error", [StatusError("quota", status_code=429), StatusError("quota", status=429),
                                   RateLimitError("slow down")], ids=["status_code", "status", "class_name"])
def test_a_429_status_or_a_rate_limit_class_trips_the_breaker(error):
    model = RateLimited(error)
    r = brief(world(n=2), model=model)
    assert len(model.calls) == SUSTAINED_CALLS
    assert r.counts["model"]["reason"] == "model_unavailable"


@pytest.mark.parametrize("error", [RuntimeError("503 unavailable, request id req_4291ab"),
                                   StatusError("gateway error 429ms", status_code=502)],
                         ids=["request_id", "other_status"])
def test_a_429_inside_other_text_does_not_trip_the_breaker(error):
    model = RateLimited(error)
    r = brief(world(n=2), model=model, workers=1)
    assert len(model.calls) == 6
    assert "model" not in r.counts


# A stalled model call


def test_on_gemini_the_brief_model_sends_a_finite_http_timeout(monkeypatch):
    from google import genai

    from core.llm.gemini import GeminiModel

    made = {}
    monkeypatch.setattr(genai, "Client", lambda **kw: made.update(kw) or SimpleNamespace())
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    model = job.brief_model()
    assert isinstance(model, GeminiModel) and model.project == job.PROJECT
    model.client
    assert 0 < job.GEMINI_TIMEOUT_S <= 120
    assert made["http_options"].timeout == job.GEMINI_TIMEOUT_S * 1000
    assert made["project"] == job.PROJECT and made["vertexai"] is True


def test_with_model_provider_unset_the_brief_model_is_gemini_too(monkeypatch):
    from google import genai

    from core.llm.gemini import GeminiModel

    made = {}
    monkeypatch.setattr(genai, "Client", lambda **kw: made.update(kw) or SimpleNamespace())
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    model = job.brief_model()
    assert isinstance(model, GeminiModel) and model.project == job.PROJECT
    model.client
    assert made["http_options"].timeout == job.GEMINI_TIMEOUT_S * 1000


def test_main_builds_the_brief_model_when_none_is_given(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(job, "brief_model", lambda: model)
    code = job.main(client=Client(world(n=1)), chain=FakeChain(), make_sc=lambda run_id: SC, clock=lambda: EARLY,
                    build_ctx=FakeCtx(), confirm=FakeConfirm(), campaign_hashtags=[],
                    political_terms={m: [] for m in MARKETS}, core="core", agent="agent")
    assert code == 0
    assert model.writer_calls()


def timeout_errors():
    import httpx
    from google.genai import errors

    return [httpx.ReadTimeout("The read operation timed out"),
            errors.ServerError(504, {"error": {"code": 504, "status": "DEADLINE_EXCEEDED",
                                               "message": "Deadline expired before operation could complete."}})]


@pytest.mark.parametrize("error", timeout_errors(), ids=["http_timeout", "server_deadline"])
def test_a_timed_out_call_holds_its_card_and_the_run_goes_on(error):
    model = RateLimited(error)
    r = brief(world(n=2), model=model, workers=1)
    assert len(model.calls) == 6  # one call per card: the timeout ends that card, not the run
    assert "model" not in r.counts
    for m in MARKETS:
        p = payload(r, m)
        assert p["cards"] == [] and p["held_back"]["count"] == 2
        assert all(c["reason"] == "explanation_failed" for c in p["held_back"]["items"])
    assert r.chain.finished[0]["status"] == "ok"


def test_no_explanation_starts_after_a_timed_out_call_that_ended_past_the_deadline():
    import httpx

    model = RateLimited(httpx.ReadTimeout("The read operation timed out"))
    r = brief(world(n=3), model=model, workers=1, clock=lambda: LATE if model.calls else EARLY)
    assert len(model.calls) == 1
    for m in MARKETS:
        assert rows(r)[m]["status"] == "partial"
        p = payload(r, m)
        assert p["cards"] == [] and p["held_back"]["count"] == 3
    assert r.chain.finished[0]["status"] == "ok"


def test_preflight_requires_two_distinct_local_posts_not_three_unknown_records(monkeypatch):
    evidence = [specificity_evidence("za_local"), specificity_evidence("za_local"),
                specificity_evidence("za_unknown_1", market=None),
                specificity_evidence("za_unknown_2", market=None)]
    cand = preflight_candidate(evidence)
    monkeypatch.setattr(job, "gate_card", lambda *_: job.Decision(True, "today", None, None, None, False))

    decision = job._gate(cand, None)

    assert decision.where == "held_back"
    assert cand["held_reason"] == "not_confirmed"
    assert "local" in decision.reason.lower()


@pytest.mark.parametrize("location,where", [(None, "today"), ("NG", "held_back")])
def test_preflight_counts_unlocated_own_feeds_but_vetoes_known_foreign_locations(monkeypatch, location, where):
    evidence = [specificity_evidence("za_feed_1", market=location, source_market="ZA"),
                specificity_evidence("za_feed_2", market=location, source_market="ZA"),
                specificity_evidence("za_unknown", market=None)]
    cand = preflight_candidate(evidence)
    monkeypatch.setattr(job, "gate_card", lambda *_: job.Decision(True, "today", None, None, None, False))

    decision = job._gate(cand, None)

    assert decision.where == where
    assert cand["held_reason"] == (None if where == "today" else "not_confirmed")


def test_two_local_posts_and_one_unknown_record_can_publish():
    con = world(n=0)
    add_item(con, "ZA", "za_two_local", 0.99)
    con.execute("UPDATE core.posts SET geo_market = NULL, geo_confidence = NULL, geo_source = NULL "
                "WHERE post_id = 'za_two_local_p3'")
    r = brief(con)

    card = next(c for c in all_cards(payload(r, "ZA")) if c["item_id"] == "za_two_local")

    assert len(card["evidence"]) == 3
    assert card["specificity"]["status"] == "pass"
    assert card["specificity"]["local_evidence_ids"] == ["za_two_local_p1", "za_two_local_p2"]


def test_posts_placed_in_another_market_do_not_count_toward_the_three():
    con = world(n=2)
    add_item(con, "ZA", "za_mixed", 0.99)
    add_item(con, "ZA", "za_unsure", 0.98)
    con.execute("UPDATE core.posts SET geo_market = 'NG', geo_confidence = 0.9, geo_source = 'ext_region' "
                "WHERE post_id = 'za_mixed_p1'")
    con.execute("UPDATE core.posts SET geo_market = 'NG', geo_confidence = 0.5, geo_source = 'place_mention' "
                "WHERE post_id = 'za_unsure_p1'")
    r = brief(con)
    held = held_items(r, "ZA")
    assert held["za_mixed"]["reason_text"] == "Fewer than 3 posts 42 can show"
    assert not any("#za_mixed" in c["user"] for c in r.model.calls)
    assert "za_unsure" in {c["item_id"] for c in all_cards(payload(r, "ZA"))}


# Readable titles, explanation status and market case


HASH = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CHANNEL = "ucabcdefghijklmnopqrstuv"


def test_readable_title_ignores_opaque_labels_and_uses_readable_canonical_key():
    assert job.readable_title(HASH, "amapiano") == "amapiano"
    for channel_id in (CHANNEL, CHANNEL.upper()):
        assert job.readable_title(channel_id, "amapiano") == "amapiano"
        assert job.readable_title(channel_id, HASH) is None


def test_readable_title_ignores_platform_account_and_post_ids():
    # Seen as 2 Oct brief titles: a TikTok numeric id, a TikTok default handle and a Reddit account fullname.
    for opaque in ("7691513826449034006", "user6242947817218", "@user6242947817218", "t2_2hs85v1ovd",
                   "T3_1abcde", "#7691513826449034006"):
        assert job.readable_title(opaque, "amapiano") == "amapiano"
        assert job.readable_title(opaque, None) is None
        assert job.readable_title(None, opaque) is None


def test_readable_title_keeps_chosen_handles_and_short_numbers():
    for name in ("virtual-mycologist13", "#sama28", "#2026", "user_stories", "userexperience", "Tyla",
                 "t2 the movie", "#afrohouse"):
        assert job.readable_title(name, None) == name


def test_boards_preserve_tied_ranks_and_leave_unranked_entries_unranked(monkeypatch):
    rows = [
        {"platform": "tiktok", "series": "board_tiktok_hashtag", "item_id": "ranked_a", "label": "Ranked A",
         "canonical_key": None, "best_rank": 1, "appearances": None},
        {"platform": "tiktok", "series": "board_tiktok_hashtag", "item_id": "ranked_b", "label": "Ranked B",
         "canonical_key": None, "best_rank": 1, "appearances": None},
        {"platform": "tiktok", "series": "board_tiktok_hashtag", "item_id": "unranked", "label": "Unranked",
         "canonical_key": None, "best_rank": None, "appearances": 3},
    ]
    monkeypatch.setattr(job, "_query", lambda *args, **kwargs: rows)

    result, unnamed = job.boards(None, D, "ZA")

    assert result == [{"platform": "tiktok", "list": "TikTok hashtag board", "entries": [
        {"rank": 1, "title": "Ranked A", "item_id": "ranked_a"},
        {"rank": 1, "title": "Ranked B", "item_id": "ranked_b"},
        {"rank": None, "title": "Unranked", "item_id": "unranked"}]}]
    assert unnamed == 0


def cmap_row(item_id, label=None, key=None):
    return {"item_id": item_id, "kind": "sound", "canonical_key": key, "label": label, "status": "active",
            "valid_from": at(day(400)), "valid_to": None}


def test_board_titles_are_readable_names_and_nameless_entries_are_counted_not_shown():
    con = world(n=1)
    duck.load(con, "agent.runs", [run("collect", D)])
    duck.load(con, "core.cultural_map", [cmap_row("t_label", label="Water by Tyla"), cmap_row("t_key", key="amapiano"),
                                         cmap_row("t_hash", key=HASH), cmap_row("t_channel", key=CHANNEL)])
    duck.load(con, "core.item_counter_daily", [
        counter(i, "board_apple_music", D, n, unit="rank", is_board=True, platform="apple_music", pull_seq=1)
        for n, i in enumerate(["t_hash", "t_label", "t_channel", "t_key", "t_nomap"], 1)])
    r = brief(con)
    za = payload(r, "ZA")
    assert za["boards"] == [{"platform": "apple_music", "list": "Apple Music chart", "entries": [
        {"rank": 2, "title": "Water by Tyla", "item_id": "t_label"},
        {"rank": 4, "title": "amapiano", "item_id": "t_key"}]}]
    assert za["coverage"] == {"issues": ["3 board entries without a name"]}
    assert payload(r, "NG")["coverage"] == {"issues": []}


def test_card_titles_follow_the_readable_rule_and_a_nameless_candidate_is_held():
    con = world(n=1)
    add_item(con, "ZA", "za_key", 0.99, label="", key="amapiano")
    add_item(con, "ZA", "za_hash", 0.98, label="", key=HASH)
    add_item(con, "ZA", "za_chan", 0.97, label="", key=CHANNEL.upper())
    r = brief(con)
    za = payload(r, "ZA")
    assert {c["item_id"]: c["title"] for c in all_cards(za)} == {"za_key": "amapiano", "za1": "#za1"}
    held = held_items(r, "ZA")
    for item_id in ("za_hash", "za_chan"):
        assert held[item_id]["reason_text"] == "No readable name"
        assert held[item_id]["reason"] == "not_confirmed"
        assert held[item_id]["title"] == "Unnamed hashtag"
    assert not any(HASH in c["user"] for c in r.model.calls)


def test_explained_cards_stay_visible_and_incomplete_results_are_held():
    r = brief(world(n=2))
    assert {c["explanation_status"] for m in MARKETS for c in all_cards(payload(r, m))} == {"explained"}

    model = FakeModel()
    r = brief(world(n=2), model=model, workers=1, clock=lambda: LATE if model.writer_calls() else EARLY)
    za = payload(r, "ZA")
    assert [c["explanation_status"] for c in za["cards"]] == ["explained"]
    assert {c["item_id"] for c in za["held_back"]["items"]} == {"za2"}
    assert all(payload(r, m)["cards"] == [] and payload(r, m)["held_back"]["count"] == 2
               for m in ("NG", "KE"))

    r = brief(world(n=1), model=RateLimited(RuntimeError("RESOURCE_EXHAUSTED")))
    assert all(payload(r, m)["cards"] == [] and payload(r, m)["held_back"]["count"] == 1 for m in MARKETS)

    class Unciting(FakeModel):
        def complete_json(self, **kw):
            out, usage = super().complete_json(**kw)
            if "claims" in kw["schema"]["properties"]:
                for claim in out["claims"]:
                    claim["evidence_ids"] = ["no_such_post"]
            return out, usage

    r = brief(world(n=1), model=Unciting())
    cards = [c for m in MARKETS for c in all_cards(payload(r, m))]
    assert cards == []
    assert all(payload(r, m)["held_back"]["count"] == 1 for m in MARKETS)


def test_unknown_evidence_remains_showable_but_does_not_meet_the_local_post_floor():
    con = world(n=0, markets=("ZA",))
    add_item(con, "ZA", "za_unplaced", 0.99, geo=None)
    r = brief(con)
    held = held_items(r, "ZA")["za_unplaced"]
    assert len(held["evidence"]) == 3
    assert all(e["market"] is None and "market_assumed" in e["flags"] for e in held["evidence"])
    assert not any("za_unplaced" in c["user"] for c in r.model.calls)
    # Held only by the local floor, so confirm searches it (posts it finds could lift the hold); it found none here.
    assert "za_unplaced" in {c["item_id"] for call in r.confirm.calls for c in call["candidates"]}
    assert "regrown" not in r.counts


def test_a_lowercase_geo_market_still_counts_as_the_brief_market():
    con = world(n=1)
    add_item(con, "ZA", "za_lower", 0.99, geo="za")
    r = brief(con)
    assert "za_lower" in {c["item_id"] for c in all_cards(payload(r, "ZA"))}


# Parallel packs, paid markers and the held-back summary


def test_packs_build_in_parallel_keep_rank_order_and_a_failing_pack_is_held(monkeypatch):
    real, lock, live = job.build_pack, threading.Lock(), {"now": 0, "max": 0}

    def slow_pack(client, row, d, market, **kw):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        try:
            time_module.sleep(0.02 * (4 - int(row["item_id"][2:])))  # later ranks finish first
            if row["item_id"] == "za2":
                raise RuntimeError("evidence query timed out")
            return real(client, row, d, market, **kw)
        finally:
            with lock:
                live["now"] -= 1

    monkeypatch.setattr(job, "build_pack", slow_pack)
    model = FakeModel()
    r = brief(world(n=3), model=model, workers=1)
    assert live["max"] >= 2
    order = [re.search(r"#(\w+)", c["user"]).group(1) for c in model.writer_calls()]
    assert order == ["za1", "ng1", "ke1", "za3", "ng2", "ke2", "ng3", "ke3"]  # za3 is ZA's second once za2 is held
    held = held_items(r, "ZA")["za2"]
    assert held["reason"] == "data_issue"
    assert held["reason_text"] == "Evidence could not be read"
    assert "za2" not in {c["item_id"] for c in r.ctx.calls}
    assert r.counts["pack_errors"] == {"ZA:za2": "RuntimeError: evidence query timed out"}
    assert r.chain.finished[0]["status"] == "ok"


def test_an_item_whose_key_is_a_paid_marker_is_always_paid_led():
    r = brief(world(n=2), ctx=FakeCtx({"za1": {"paid_key": "ad"}}))
    held = held_items(r, "ZA")["za1"]
    assert held["reason"] == "paid_led" and held["rule"] == "G5b"
    assert held["reason_text"] == "Paid-led: #ad marks paid posts"
    assert not any("#za1" in c["user"] for c in r.model.calls)


def test_evidence_sponsored_share_raises_the_item_state_share_for_g5():
    r = brief(world(n=2), ctx=FakeCtx({"za1": {"sponsored_share": 0.67}, "za2": {"sponsored_share": 0.34}}))
    held = held_items(r, "ZA")
    assert held["za1"]["reason"] == "paid_led" and held["za1"]["rule"] == "G5"
    assert "za2" not in held


# Data-issue counting, music collaborations and the order of holds


def test_a_market_whose_evidence_reads_all_fail_carries_the_data_issue_banner(monkeypatch):
    real = job.build_pack

    def failing(client, row, d, market, **kw):
        if market == "ZA":
            raise RuntimeError("evidence query timed out")
        return real(client, row, d, market, **kw)

    monkeypatch.setattr(job, "build_pack", failing)
    r = brief(world(n=3))
    za = payload(r, "ZA")
    assert za["cards"] == []
    assert "data_issue" in [b["kind"] for b in za["banners"]]
    assert rows(r)["ZA"]["status"] == "data_issue"
    assert rows(r)["NG"]["status"] == "published"


def test_a_music_collaboration_tag_in_captions_is_not_paid_but_ad_is():
    con = world(n=2)
    con.execute("UPDATE core.posts SET text = 'New track #collab with #partner vibes' "
                "WHERE post_id IN ('za1_p1', 'za1_p2')")
    con.execute("UPDATE core.posts SET text = 'Loving this #ad' WHERE post_id IN ('za2_p1', 'za2_p2')")
    r = brief(con, ctx=gatectx.build_ctx)
    assert "za1" in {c["item_id"] for c in all_cards(payload(r, "ZA"))}
    held = held_items(r, "ZA")
    assert "za1" not in held
    assert held["za2"]["reason"] == "paid_led" and held["za2"]["rule"] == "G5"


def test_invalid_data_days_are_checked_before_the_briefs_own_holds():
    con = world(n=2)
    add_item(con, "ZA", "fyp", 0.4, map_status="generic")
    add_item(con, "ZA", "za_hash", 0.3, label="", key=HASH)
    add_item(con, "ZA", "za_paid", 0.2)
    bad = {"valid_days": [False, True, True]}
    r = brief(con, ctx=FakeCtx({"fyp": bad, "za_hash": bad, "za_paid": {**bad, "paid_key": "ad"}}))
    held = held_items(r, "ZA")
    assert {i: held[i]["rule"] for i in ("fyp", "za_hash", "za_paid")} == {"fyp": "G1", "za_hash": "G1",
                                                                          "za_paid": "G1"}
    assert {held[i]["reason"] for i in ("fyp", "za_hash", "za_paid")} == {"data_issue"}
    assert rows(r)["ZA"]["status"] == "data_issue"


# Held reasons (TRUST.md section 2: a held item is shown with its reason). Only fixed wording reaches the card or
# claim_checks.reason; no model text, scraped text, dict repr or internal field name.

# intelligence_42_agent.claim_checks as get_table read it on staging, 29 September 2026 (all STRING, NULLABLE).
STAGING_CLAIM_CHECKS = {"answer_or_brief_id", "claim_id", "rule", "verdict", "checker", "run_id", "reason"}
# The reviewer's leak phrases (round 2): age readings past the word lists, names, contacts and post text.
LEAKS = [
    "Thabo Mokoena is just dancing at home, nothing cultural", "one creator, Thabo Mokoena, made every clip",
    "thabo_moves made every clip", "thabo moves made every clip", "the post lists 082 555 1234 as a booking line",
    "the post gives thabo@gmail.com for bookings", "the clip at tiktok.com/thabo_moves/video/123 is the source",
    "the posts say new amapiano sound and little else", "it says 'amapiano sound at' only",
    "the post reads ngoma ni moto sana, a party post", "ngoma ni moto, a party post", "teens reposting",
    "youngsters reposting", "ama2000 reposting one clip", "school kids on break", "Gen_Z reposting",
    "GenZ reposting", "gen-z reposting", "gen zed reposting", "amagenz reposting", "tweens on school break",
    "high schoolers on break", "Grade 12s after exams", "the class of 2026 celebrating", "people born after 2000",
    "vijana reposting one clip", "intsha reposting one clip", "zillennials reposting",
    "varsity first-years at orientation", "Gеn Z reposting", "Ｇｅｎ Ｚ reposting",
]
LEAK_TOKENS = {"thabo", "mokoena", "082", "gmail", "tiktok", "amapiano", "ngoma", "moto", "teens", "youngsters",
               "ama2000", "kids", "gen", "genz", "amagenz", "tweens", "schoolers", "grade", "class", "born",
               "vijana", "intsha", "zillennials", "varsity", "first", "years", "school", "exams", "reposting",
               "orientation", "booking", "bookings", "party", "cultural", "dancing"}
MENU = ("a paid campaign", "a platform feature change", "a coordinated push", "a news or scheduled event",
        "a scraping or collection artefact", "one viral post or creator")


def fixed_words():
    """Every string job.check_reason and job.failed_reason may write."""
    names = set(job.RULE_NAMES.values()) | {"Claim checks"}
    parts = (set(job.CLAIM_WORDS.values()) | set(job.SENTENCE_WORDS.values()) | set(job.OTHER_WORDS.values())
             | {w.replace("a claim", "a claim the explanation rests on", 1) for w in job.CLAIM_WORDS.values()})
    out = {f"{n}: {p}" for n in names for p in parts | {"passed"}}
    critic = {"Critic: a simpler explanation was not ruled out", "Critic: the simpler explanation was ruled out"}
    out |= critic | {f"{c}: {m}" for c in critic for m in MENU}
    held = {"Critic: a simpler explanation was not ruled out"} | {
        f"Critic: a simpler explanation was not ruled out: {m}" for m in MENU}
    out |= {f"{c}; local why-now not shown" for c in held} | {"Critic: local why-now not shown"}
    out |= {job.NEWS_PASS} | {f"{job.NEWS_PASS}: {m}" for m in MENU}
    out |= {job.TOO_FEW, job.NO_REST}
    out |= set(REASON_TEXT.values())
    return out | {job.REPAIR + w for w in out}


class ScriptedModel(FakeModel):
    """FakeModel with scripted support and critic outputs: claim_support {claim text fragment: (verdict, reason)},
    sentence (verdict, reason) for the explanation sentence check, critic (non_cultural_explanation, reason)."""

    def __init__(self, claim_support=None, sentence=None, critic=None, **kw):
        super().__init__(ruled_out=critic is None, **kw)
        self.claim_support = claim_support or {}
        self.sentence = sentence
        self.critic = critic

    def complete_json(self, *, system, user, schema, model, max_tokens):
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model,
                                           max_tokens=max_tokens)
        if "ruled_out" in schema["properties"] and self.critic:
            return {"non_cultural_explanation": self.critic[0], "ruled_out": False, "local_why_now": True,
                    "reason": self.critic[1]}, usage
        if "verdict" in schema["properties"]:
            if "Label: explanation sentence" in user:
                if self.sentence:
                    return {"verdict": self.sentence[0], "reason": self.sentence[1]}, usage
                return out, usage
            for fragment, (verdict, reason) in self.claim_support.items():
                if fragment in user:
                    return {"verdict": verdict, "reason": reason}, usage
        return out, usage


class BadDraft(FakeModel):
    """The reviewer's case: c1, which the sentence rests on, cites an unknown post (K1); c2, which it does not rest
    on, cites an unknown number (K2)."""

    def complete_json(self, *, system, user, schema, model, max_tokens):
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model,
                                           max_tokens=max_tokens)
        if "verdict" in schema["properties"] or "ruled_out" in schema["properties"]:
            return out, usage
        out["claims"][0]["evidence_ids"] = ["no_such_post"]
        out["claims"][1]["number_ids"] = ["no_such_number"]
        return out, usage


def za1_checks(r):
    return [c for c in checks(r) if c["answer_or_brief_id"].endswith(":ZA:za1")]


def za1_held(r):
    return held_items(r, "ZA")["za1"]


def test_every_claim_checks_row_carries_fixed_wording_for_its_rule_as_reason():
    r = brief(world(n=1))
    rows_ = za1_checks(r)
    assert rows_ and all(c["reason"] in fixed_words() for c in rows_)
    assert {c["reason"] for c in rows_ if c["rule"] == "K1"} == {"Quote check: passed"}
    assert {c["reason"] for c in rows_ if c["rule"] == "K4"} == {"Support check: passed"}
    [critic] = [c for c in rows_ if c["rule"] == "critic"]
    assert critic["reason"] == "Critic: the simpler explanation was ruled out: a paid campaign"


def test_a_critic_hold_maps_its_explanation_onto_the_menu():
    r = brief(world(n=1), model=ScriptedModel(critic=("a scraping artefact", "all collected in one sweep")))
    [critic] = [c for c in za1_checks(r) if c["rule"] == "critic"]
    assert critic["verdict"] == "cut"
    assert critic["reason"] == "Critic: a simpler explanation was not ruled out: a scraping or collection artefact"
    held = za1_held(r)
    assert held["reason"] == "explanation_failed" and len(held["evidence"]) == 3
    assert held["reason_text"] == "Explanation failed its checks"


@pytest.mark.parametrize("explanation,menu", [
    ("a sponsored push by a drinks brand", "a paid campaign"),
    ("TikTok changed a platform feature this week", "a platform feature change"),
    ("bots posting on a schedule", "a coordinated push"),
    ("a news event about the match", "a news or scheduled event"),
    ("a collection artefact of one sweep", "a scraping or collection artefact"),
    ("a single viral post carrying the count", "one viral post or creator"),
])
def test_the_critic_explanation_is_named_only_by_its_menu_wording(explanation, menu):
    r = brief(world(n=1), model=ScriptedModel(critic=(explanation, "nothing rules it out")))
    [critic] = [c for c in za1_checks(r) if c["rule"] == "critic"]
    assert critic["reason"] == f"Critic: a simpler explanation was not ruled out: {menu}"
    assert za1_held(r)["reason"] == "explanation_failed"


def critic_out(ruled_out, local_why_now, explanation="a sponsored push by a drinks brand"):
    return {"non_cultural_explanation": explanation, "ruled_out": ruled_out, "local_why_now": local_why_now,
            "reason": "the posts say so"}


@pytest.mark.parametrize("ruled_out,local_why_now,reason", [
    (False, True, "Critic: a simpler explanation was not ruled out: a paid campaign"),
    (True, False, "Critic: local why-now not shown"),
    (False, False, "Critic: a simpler explanation was not ruled out: a paid campaign; local why-now not shown"),
    (True, True, "Critic: the simpler explanation was ruled out: a paid campaign"),
])
def test_a_critic_cut_names_the_part_that_failed(ruled_out, local_why_now, reason):
    row = explain._critic_row(critic_out(ruled_out, local_why_now))
    assert row["verdict"] == ("pass" if ruled_out and local_why_now else "cut")
    assert job.check_reason(row) == reason
    assert reason in fixed_words()
    if row["verdict"] == "cut":
        assert job.failed_reason({"reason": "failed_checks", "rests_on": ["c1"], "checks": [row]}) == reason


def test_a_local_why_now_failure_off_the_menu_still_names_the_local_why_now():
    row = explain._critic_row(critic_out(False, False, explanation="Thabo Mokoena is just dancing at home"))
    assert job.check_reason(row) == "Critic: a simpler explanation was not ruled out; local why-now not shown"
    assert "thabo" not in job.check_reason(row).lower()


def test_a_critic_explanation_off_the_menu_gets_the_fixed_wording_alone():
    r = brief(world(n=1), model=ScriptedModel(critic=("Thabo Mokoena is just dancing at home", "fine")))
    [critic] = [c for c in za1_checks(r) if c["rule"] == "critic"]
    assert critic["reason"] == "Critic: a simpler explanation was not ruled out"
    assert za1_held(r)["reason"] == "explanation_failed"


def test_a_sentence_support_cut_names_the_support_check():
    r = brief(world(n=1), model=ScriptedModel(sentence=("partial", "the posts show dancing but not why")))
    # The cut holds the draft, so it gets the one repair round; the first draft's row is marked as before it.
    before, row = [c for c in za1_checks(r) if c["rule"] == "K4" and c["claim_id"] is None]
    assert before["reason"] == job.REPAIR + "Support check: the explanation sentence was not supported by its posts"
    assert row["verdict"] == "cut"
    assert row["reason"] == "Support check: the explanation sentence was not supported by its posts"
    assert za1_held(r)["reason"] == "explanation_failed"


def test_a_cut_on_a_claim_the_sentence_rests_on_names_its_rule_not_the_last_cut():
    # The reviewer's first input: c1 (rested on) is cut by K1 and c2 (not rested on) by K2, which comes later.
    r = brief(world(n=1), model=BadDraft())
    cuts = {c["claim_id"]: c["rule"] for c in za1_checks(r) if c["verdict"] == "cut"
            and not c["reason"].startswith(job.REPAIR)}
    assert cuts == {"c1": "K1", "c2": "K2"}
    assert za1_held(r)["reason"] == "explanation_failed"


def test_a_support_cut_on_a_rested_claim_is_named_even_when_a_later_claim_is_cut():
    # The reviewer's second input: c1 (rested on) and c2 (not rested on) both fail the support check.
    model = ScriptedModel(claim_support={"Local creators are posting": ("unsupported", "no post names creators"),
                                         "earliest post in the pack": ("unsupported", "from a brand page")})
    r = brief(world(n=1), model=model)
    # The cut on c1 holds the draft, so it gets the one repair round; the repaired draft's rows decide.
    final = [c for c in za1_checks(r) if not c["reason"].startswith(job.REPAIR)]
    assert {c["claim_id"] for c in final if c["rule"] == "K4" and c["verdict"] == "cut"} == {"c1", "c2"}
    assert {c["reason"] for c in final if c["rule"] == "K4" and c["verdict"] == "cut"} == {
        "Support check: a claim was not supported by its posts"}
    assert za1_held(r)["reason"] == "explanation_failed"


def test_too_few_claims_is_fixed_wording_not_the_rule_of_the_last_cut():
    result = {"reason": "too_few_claims", "rests_on": ["c1", "c3"], "checks": [
        {"claim_id": "c1", "rule": "K1", "verdict": "cut", "checker": "code",
         "detail": "before repair: unresolved evidence ids: ['x']"},
        {"claim_id": "c2", "rule": "K3", "verdict": "cut", "checker": "code",
         "detail": "names Kenya but cites no record located there"},
        {"claim_id": None, "rule": "K10", "verdict": "downgrade", "checker": "code", "detail": "status partial"}]}
    assert job.failed_reason(result) == "Claim checks: fewer than 2 claims passed"


def test_a_hold_with_no_row_behind_it_gets_the_fixed_wording():
    result = {"reason": "failed_checks", "rests_on": [], "checks": [
        {"claim_id": "c1", "rule": "K1", "verdict": "pass", "checker": "code", "detail": "1 ids resolved"}]}
    assert job.failed_reason(result) == job.NO_REST


def test_the_place_fault_names_the_place_check():
    result = {"reason": "failed_checks", "rests_on": ["c1"], "checks": [
        {"claim_id": None, "rule": "K3", "verdict": "cut", "checker": "code",
         "detail": "short_answer: the explanation names a place (Kenya) that no post located there shows"}]}
    assert job.failed_reason(result) == "Place check: the explanation named a place no cited post is located in"


def test_a_before_repair_row_keeps_the_prefix_and_fixed_wording():
    row = {"claim_id": "c1", "rule": "K1", "verdict": "cut", "checker": "code",
           "detail": "before repair: quote 'new sound at home' from e1: not verbatim on word boundaries"}
    assert job.check_reason(row) == "before repair: Quote check: a quote or evidence id in a claim did not check out"


@pytest.mark.parametrize("leak", LEAKS)
def test_no_model_text_reaches_failed_reason_or_reason(leak):
    model = ScriptedModel(critic=(leak, leak), claim_support={"earliest post in the pack": ("unsupported", leak)})
    critic = brief(world(n=1), model=model)
    sentence = brief(world(n=1), model=ScriptedModel(sentence=("unsupported", leak)))
    held = [za1_held(r) for r in (critic, sentence)]
    texts = ([c["reason"] for r in (critic, sentence) for c in checks(r)]
             + [item["reason_text"] for item in held])
    assert all(item["reason"] in REASONS for item in held)
    assert all(t in fixed_words() for t in texts)
    for t in texts:
        assert leak not in t
        assert not set(re.findall(r"\w+", t.lower())) & LEAK_TOKENS


@pytest.mark.parametrize("leak", LEAKS)
def test_no_code_detail_reaches_reason(leak):
    for rule in ("K1", "K2", "K3", "K6", "K8", "K10", "K4", "critic"):
        for claim_id in ("c1", None):
            row = {"claim_id": claim_id, "rule": rule, "verdict": "cut", "checker": "code",
                   "detail": f"quote {leak!r} from e1: {{'number_id': 'n9'}} short_answer: {leak}"}
            reason = job.check_reason(row)
            assert reason in fixed_words()
            assert leak not in reason and "short_answer" not in reason and "{" not in reason


def test_failed_explanations_are_held_and_successful_cards_have_no_failed_reason():
    r = brief(world(n=2), model=ScriptedModel(critic=("a paid campaign", "no flags either way")))
    for m in MARKETS:
        p = payload(r, m)
        assert p["cards"] == [] and p["held_back"]["count"] == 2
        assert all(i["reason"] == "explanation_failed" for i in p["held_back"]["items"])
    explained = brief(world(n=2))
    assert all(c["explanation_status"] == "explained" and c["failed_reason"] is None
               for m in MARKETS for c in all_cards(payload(explained, m)))
    late = brief(world(n=2), clock=lambda: LATE)
    assert all(payload(late, m)["cards"] == [] and payload(late, m)["held_back"]["count"] == 2 for m in MARKETS)


def test_every_wording_is_bounded():
    assert max(map(len, fixed_words())) <= job.HELD_MAX <= job.REASON_MAX


def test_claim_checks_rows_fit_the_staging_table():
    r = brief(world(n=2), model=ScriptedModel(critic=("a scraping artefact", "all collected in one sweep")))
    assert checks(r)
    for c in checks(r):
        assert set(c) == STAGING_CLAIM_CHECKS
        assert all(v is None or isinstance(v, str) for v in c.values())


# The suppression list (SETUP.md data protection): the brief stores nothing of a suppressed creator


def test_a_suppressed_creator_is_left_out_of_the_packs_prompts_and_every_stored_row():
    con = world()
    add_item(con, "ZA", "za_named", 0.97, label="Dance with za1_c1")
    duck.load(con, "core.suppressed_fixture", [{"creator_id": "za1_c1"}])
    r = brief(con)
    stored = json.dumps(r.client.inserted)
    assert "za1_c1" not in stored and "za1_p1" not in stored
    prompts = "\n".join(c["user"] for c in r.model.calls)
    assert "za1_c1" not in prompts and "za1_p1" not in prompts
    za = payload(r, "ZA")
    za1 = next(i for i in all_cards(za) + za["held_back"]["items"] if i["item_id"] == "za1")
    assert [e["id"] for e in za1["evidence"]] == ["za1_p2", "za1_p3"]
    za2 = next(c for c in all_cards(za) if c["item_id"] == "za2")
    assert len(za2["evidence"]) == 3
    named = next(i for i in all_cards(za) + za["held_back"]["items"] if i["item_id"] == "za_named")
    assert named["title"] == "Dance with ***"  # masked as Today masks a bare handle


def test_an_unreadable_suppression_list_stops_the_brief_with_a_clear_error_and_writes_nothing():
    con = world()
    con.execute("DROP VIEW core.v_suppressed_creators")
    client, chain, model = Client(con), FakeChain(), FakeModel()
    with pytest.raises(job.SuppressionUnreadable, match="suppression list"):
        job.run(client, D, chain=chain, model=model, make_sc=None, clock=lambda: EARLY, build_ctx=FakeCtx(),
                confirm=FakeConfirm(), campaign_hashtags=[], political_terms={m: [] for m in MARKETS},
                core="core", agent="agent")
    [fin] = chain.finished
    assert fin["status"] == "failed" and "suppression list" in fin["error"]
    assert client.inserted == {} and model.calls == []


# Confirm on candidates held only by the post floors


class GrowingConfirm(FakeConfirm):
    """FakeConfirm that, for the items in `grow`, writes that many new local posts linked to the item (as ingest
    plus post linking would) and reports them as ingested."""

    def __init__(self, con, grow, geo=DEFAULT_GEO, **kw):
        super().__init__(**kw)
        self.con, self.grow, self.geo = con, grow, geo

    def __call__(self, candidates, *, market, **kw):
        out = super().__call__(candidates, market=market, **kw)
        for c in candidates:
            n = self.grow.get(c["item_id"], 0)
            geo = market if self.geo is DEFAULT_GEO else self.geo
            for k in range(1, n + 1):
                pid, creator = f"{c['item_id']}_cf{k}", f"{c['item_id']}_cfc{k}"
                duck.load(self.con, "core.creators", [{"creator_id": creator, "platform": "instagram",
                                                       "handle": f"@{creator}", "coord_score": 0}])
                duck.load(self.con, "core.posts", [{
                    "post_id": pid, "platform": "instagram", "creator_id": creator,
                    "creator_tier_at_post": "micro", "text": "Dancing to the new sound at home with friends",
                    "published_at": at(day(1), 11), "geo_market": geo, "geo_confidence": 0.9 if geo else None,
                    "geo_source": "ext_region" if geo else None, "post_date": day(1), "engagement": 50 - k}])
                duck.load(self.con, "core.post_items", [{"post_id": pid, "item_id": c["item_id"], "via": "confirm"}])
                duck.load(self.con, "core.post_observations", [{
                    "post_id": pid, "observed_at": at(D, 5), "observed_date": D, "market": market,
                    "platform": "instagram", "lane": "confirm", "lane_class": "search_presence",
                    "run_id": "brief-20260920-fake"}])
            if n:
                out[c["item_id"]]["ingest"] = {"posts": n, "observations": n, "errors": 0}
        return out


def test_floor_held_candidates_are_confirmed_after_the_today_bound_ones():
    con = world(n=2)
    add_item(con, "NG", "one_post", 0.99, posts=1, creators3=2)
    add_item(con, "NG", "coord", 0.98, posts=1, authenticity="likely_coordinated")
    r = brief(con)
    ng = [c["item_id"] for c in r.confirm.calls[1]["candidates"]]
    assert ng == ["ng1", "ng2", "one_post"]
    assert held_items(r, "NG")["one_post"]["reason_text"] == "Fewer than 3 posts 42 can show"
    assert "regrown" not in r.counts


def test_posts_confirm_finds_for_a_floor_held_candidate_can_make_it_a_card_in_the_same_run():
    con = world(n=2)
    add_item(con, "NG", "one_post", 0.99, posts=1, creators3=2)
    r = brief(con, confirm=GrowingConfirm(con, {"one_post": 3}))
    cards = {c["item_id"]: c for c in all_cards(payload(r, "NG"))}
    assert "one_post" in cards and cards["one_post"]["explained"] is True
    assert sorted(cards["one_post"]["evidence_ids"]) == ["one_post_cf1", "one_post_cf2", "one_post_cf3",
                                                          "one_post_p1"]
    assert "one_post" not in held_items(r, "NG")
    assert r.counts["regrown"] == [{"market": "NG", "item_id": "one_post", "posts_before": 1, "posts_after": 4,
                                    "held": False}]
    assert any("#one_post" in c["user"] for c in r.model.writer_calls())


def test_a_regrown_candidate_still_under_the_floors_stays_held_and_is_not_explained():
    con = world(n=2)
    add_item(con, "NG", "one_post", 0.99, posts=1, creators3=2)
    r = brief(con, confirm=GrowingConfirm(con, {"one_post": 1}))
    held = held_items(r, "NG")
    assert held["one_post"]["reason_text"] == "Fewer than 3 posts 42 can show"
    assert r.counts["regrown"][0]["held"] is True and r.counts["regrown"][0]["posts_after"] == 2
    assert not any("#one_post" in c["user"] for c in r.model.calls)


@pytest.mark.parametrize("late", [True, False])
def test_no_pack_is_read_again_past_the_time_limit(monkeypatch, late):
    """Regrow starts no market once the run is FINISH_MARGIN short of the task timeout, as at the deadline; the
    floor-held candidate then keeps its hold, exactly as if confirm had found nothing."""
    _no_recovery(monkeypatch)
    con = world(n=2)
    add_item(con, "NG", "one_post", 0.99, posts=1, creators3=2)
    confirm = GrowingConfirm(con, {"one_post": 3})
    then = TIME_LIMIT if late else TIME_LIMIT - timedelta(seconds=1)
    r = brief(con, confirm=confirm, clock=lambda: DAWN + (then if len(confirm.calls) == len(MARKETS) else timedelta(0)))
    assert [c["market"] for c in confirm.calls] == list(MARKETS)
    if late:
        assert "regrown" not in r.counts
        assert held_items(r, "NG")["one_post"]["reason_text"] == "Fewer than 3 posts 42 can show"
        assert r.counts["explanation_stop"]["limit"] == "task_timeout"
    else:
        assert r.counts["regrown"] == [{"market": "NG", "item_id": "one_post", "posts_before": 1, "posts_after": 4,
                                        "held": False}]
        assert "one_post" in {c["item_id"] for c in all_cards(payload(r, "NG"))}


def test_regrowth_never_lifts_the_two_local_floor_with_posts_located_elsewhere():
    con = world(n=2)
    add_item(con, "NG", "one_post", 0.99, posts=1, creators3=2)
    r = brief(con, confirm=GrowingConfirm(con, {"one_post": 3}, geo="ZA"))
    assert "one_post" in held_items(r, "NG")
    assert "one_post" not in {c["item_id"] for c in all_cards(payload(r, "NG"))}


def test_today_bound_packs_are_not_read_again_when_confirm_writes_posts():
    con = world(n=2)
    r = brief(con, confirm=GrowingConfirm(con, {"za1": 3}))
    card = {c["item_id"]: c for c in all_cards(payload(r, "ZA"))}["za1"]
    assert sorted(card["evidence_ids"]) == ["za1_p1", "za1_p2", "za1_p3"]
    assert "regrown" not in r.counts


def test_past_the_deadline_floor_held_candidates_are_not_read_again(monkeypatch):
    con = world(n=2)
    add_item(con, "NG", "one_post", 0.99, posts=1, creators3=2)
    confirm = GrowingConfirm(con, {"one_post": 3})
    real = job._brief_deadline
    monkeypatch.setattr(job, "_brief_deadline",
                        lambda now, d, chain: (True, None) if len(confirm.calls) == 3 else real(now, d, chain))
    r = brief(con, confirm=confirm)
    assert [c["market"] for c in confirm.calls] == ["ZA", "NG", "KE"]
    assert "regrown" not in r.counts
    assert held_items(r, "NG")["one_post"]["reason_text"] == "Fewer than 3 posts 42 can show"


def test_payload_candidate_carries_news_driven_only_for_an_explained_result():
    cand = {"row": {"item_id": "it_1"}, "pack": {"numbers": [], "evidence": []}, "decision": None,
            "sparkline": None}
    ok = {"reason": None, "numbers_only": False, "explanation": "E.", "explanation_claim_ids": ["c1"],
          "claims": [], "news_driven": True}
    assert job._payload_candidate(cand, ok)["news_driven"] is True
    assert job._payload_candidate(cand, dict(ok, news_driven=False))["news_driven"] is False
    failed = dict(ok, reason="failed_checks", numbers_only=True, checks=[])
    assert job._payload_candidate(cand, failed)["news_driven"] is False
    assert job._payload_candidate(cand, None)["news_driven"] is False


def test_a_news_driven_pass_is_recorded_as_news_driven_never_as_ruled_out():
    out = {"non_cultural_explanation": "a news event", "ruled_out": False, "news_driven": True,
           "local_reaction": True, "local_why_now": True, "reason": "the posts say so"}
    row = explain._critic_row(out, 2)
    assert row["verdict"] == "pass"
    reason = job.check_reason(row)
    assert reason == "Critic: news-driven, local creators react in their own words: a news or scheduled event"
    assert "ruled out" not in reason and reason in fixed_words()
    cut = explain._critic_row(dict(out, local_why_now=False), 2)
    assert job.check_reason(cut) == "Critic: local why-now not shown"


def test_the_critics_answer_is_stored_in_the_briefs_payload_for_shown_and_held_items():
    shown = brief(world(n=1))
    held = brief(world(n=1), model=FakeModel(ruled_out=False))
    answer = {"non_cultural_explanation": "a paid campaign", "local_why_now": True, "reason": "fake",
              "news_driven": None, "scheduled_event": None, "local_reaction": None}
    for m in MARKETS:
        assert payload(shown, m)["critic"] == [{"item_id": item(m, 1), **answer, "ruled_out": True}]
        assert payload(held, m)["critic"] == [{"item_id": item(m, 1), **answer, "ruled_out": False}]
        assert [c["item_id"] for c in all_cards(payload(shown, m))] == [item(m, 1)]
        assert all("critic" not in c for c in all_cards(payload(shown, m)))
        assert all("critic" not in i for i in payload(held, m)["held_back"]["items"])
    # The claim_checks rows keep their fixed wording; the answer is not written there.
    assert all(set(c) == CHECK_KEYS for c in checks(held))
    assert {c["reason"] for c in checks(held) if c["rule"] == "critic"} == {
        "Critic: a simpler explanation was not ruled out: a paid campaign"}


# BR-3 (4 Oct): creator items keyed by a platform id ("youtube:uc…") showed the raw key as their title in the
# 3 Oct holds and in the writer pack. A creator whose label is only the id its key was built from now takes the
# creators row's display name, else its @handle when the handle is not itself an id; with neither it is
# "Unnamed creator" and held for having no readable name, as every other id-titled item already is.

YT_ID = "UCabcdefghijklmnopqrstuvwx"
YT_KEY = f"youtube:{YT_ID.lower()}"


def with_creator_names(con, *rows):
    """The fixture creators table gains the live table's display_name and followers columns (core/schema/core.sql)."""
    con.execute("ALTER TABLE core.creators ADD COLUMN display_name VARCHAR")
    con.execute("ALTER TABLE core.creators ADD COLUMN followers BIGINT")
    duck.load(con, "core.creators", [{"coord_score": 0, **r} for r in rows])
    return con


def add_creator_item(con, item_id, worth, *, label=None, key=YT_KEY):
    add_item(con, "ZA", item_id, worth, label=YT_ID.lower() if label is None else label, key=key, kind="creator")


def title_of(r, market, item_id):
    p = payload(r, market)
    found = {c["item_id"]: c for c in all_cards(p)} | {i["item_id"]: i for i in p["held_back"]["items"]}
    return found[item_id]["title"], found[item_id].get("reason_text")


def test_a_creator_keyed_by_its_channel_id_takes_the_creators_display_name():
    con = with_creator_names(world(n=1), {"creator_id": YT_ID, "platform": "youtube", "handle": "lasizwe",
                                          "display_name": "Lasizwe", "followers": 100})
    add_creator_item(con, "za_yt", 0.99)
    r = brief(con)
    title, reason = title_of(r, "ZA", "za_yt")
    assert title == "Lasizwe"
    assert reason != "No readable name"
    assert not any(YT_KEY in c["user"] for c in r.model.calls)


def test_a_creator_with_no_display_name_takes_its_handle_and_never_an_id_handle():
    con = with_creator_names(world(n=1), {"creator_id": YT_ID, "platform": "youtube", "handle": "@lasizwe",
                                          "display_name": None, "followers": 100},
                             {"creator_id": "UCzzzzzzzzzzzzzzzzzzzzzzzz", "platform": "youtube",
                              "handle": "UCzzzzzzzzzzzzzzzzzzzzzzzz", "display_name": None, "followers": 5})
    add_creator_item(con, "za_yt", 0.99)
    add_creator_item(con, "za_yt_id", 0.98, label="", key="youtube:uczzzzzzzzzzzzzzzzzzzzzzzz")
    r = brief(con)
    assert title_of(r, "ZA", "za_yt")[0] == "@lasizwe"
    assert title_of(r, "ZA", "za_yt_id") == ("Unnamed creator", "No readable name")


def test_a_creator_keyed_by_an_id_with_no_creators_row_is_unnamed_and_held():
    con = with_creator_names(world(n=1))
    add_creator_item(con, "za_yt", 0.99)
    r = brief(con)
    assert title_of(r, "ZA", "za_yt") == ("Unnamed creator", "No readable name")
    assert not any(YT_KEY in c["user"] for c in r.model.calls)


def test_a_suppressed_creator_is_never_named_from_the_creators_table():
    con = with_creator_names(world(n=1), {"creator_id": YT_ID, "platform": "youtube", "handle": "lasizwe",
                                          "display_name": "Lasizwe", "followers": 100})
    duck.load(con, "core.suppressed_fixture", [{"creator_id": YT_ID}])
    add_creator_item(con, "za_yt", 0.99)
    r = brief(con)
    assert "Lasizwe" not in json.dumps(payload(r, "ZA"))
    assert not any("Lasizwe" in c["user"] for c in r.model.calls)


def test_a_creator_with_a_chosen_handle_key_keeps_its_readable_title_without_a_creators_row():
    con = with_creator_names(world(n=1))
    add_creator_item(con, "za_handle", 0.99, label="lasizwe", key="tiktok:lasizwe")
    r = brief(con)
    assert title_of(r, "ZA", "za_handle")[0] == "lasizwe"


def test_a_named_creator_ranks_with_named_items_and_names_are_read_once_per_run(monkeypatch):
    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    con = with_creator_names(world(n=0, markets=("ZA",)),
                             {"creator_id": YT_ID, "platform": "youtube", "handle": "lasizwe",
                              "display_name": "Lasizwe", "followers": 100})
    for rank in range(10, 0, -1):
        add_item(con, "ZA", f"za_named_{rank}", float(rank))
    add_creator_item(con, "za_yt", 100.0)
    add_creator_item(con, "za_yt_unnamed", 99.0, label="", key="youtube:uczzzzzzzzzzzzzzzzzzzzzzzz")
    client = Client(con)
    by_market = job._candidates(client, D, build_ctx=FakeCtx(), campaign_hashtags=[],
                                political_terms={m: ["election"] for m in MARKETS}, core="core", agent="agent")
    ids = [cand["row"]["item_id"] for cand in by_market["ZA"]]
    assert ids == ["za_yt"] + [f"za_named_{rank}" for rank in range(10, 1, -1)]
    assert by_market["ZA"][0]["row"]["title"] == "Lasizwe" and by_market["ZA"][0]["row"]["nameless"] is False
    assert sum("display_name" in sql for sql in client.sql) == 1


def creator_cmap_row(item_id, label, key):
    return {**cmap_row(item_id, label=label, key=key), "kind": "creator"}


def test_board_creators_keyed_by_a_channel_id_are_named_from_creators_or_left_out():
    # A YouTube trending board counts each video's channel. A channel item whose label is only its id titled the
    # board entry "youtube:uc..." (readable_title fell back to the key), which Today showed as a name.
    other_id = "UCzzzzzzzzzzzzzzzzzzzzzzzz"
    con = with_creator_names(world(n=1), {"creator_id": YT_ID, "platform": "youtube", "handle": "lasizwe",
                                          "display_name": "Lasizwe", "followers": 100})
    duck.load(con, "agent.runs", [run("collect", D)])
    duck.load(con, "core.cultural_map", [
        creator_cmap_row("yt_named", YT_ID, YT_KEY),
        creator_cmap_row("yt_unknown", other_id, f"youtube:{other_id.lower()}"),
        creator_cmap_row("yt_chosen", "Sol Phenduka", "youtube:solphenduka")])
    duck.load(con, "core.item_counter_daily", [
        counter(i, "board_youtube", D, n, unit="rank", is_board=True, platform="youtube", pull_seq=1)
        for n, i in enumerate(["yt_unknown", "yt_named", "yt_chosen"], 1)])
    za = payload(brief(con), "ZA")
    assert za["boards"] == [{"platform": "youtube", "list": "YouTube trending board", "entries": [
        {"rank": 2, "title": "Lasizwe", "item_id": "yt_named"},
        {"rank": 3, "title": "Sol Phenduka", "item_id": "yt_chosen"}]}]
    assert za["coverage"] == {"issues": ["1 board entry without a name"]}
    assert "youtube:uc" not in json.dumps(za["boards"]).lower()


def test_a_suppressed_board_creator_is_never_named_from_the_creators_table():
    con = with_creator_names(world(n=1), {"creator_id": YT_ID, "platform": "youtube", "handle": "lasizwe",
                                          "display_name": "Lasizwe", "followers": 100})
    duck.load(con, "core.suppressed_fixture", [{"creator_id": YT_ID}])
    duck.load(con, "agent.runs", [run("collect", D)])
    duck.load(con, "core.cultural_map", [creator_cmap_row("yt_named", YT_ID, YT_KEY)])
    duck.load(con, "core.item_counter_daily", [
        counter("yt_named", "board_youtube", D, 1, unit="rank", is_board=True, platform="youtube", pull_seq=1)])
    za = payload(brief(con), "ZA")
    assert "Lasizwe" not in json.dumps(za)
    assert za["boards"] == []
    assert za["coverage"] == {"issues": ["1 board entry without a name"]}
