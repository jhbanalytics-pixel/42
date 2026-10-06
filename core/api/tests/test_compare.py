"""Compare (contract.md section 11) over the fixture store, and the BigQuery SQL it runs."""
import datetime as dt
import json
import re
from collections import Counter

import pytest

from core.api import compare
from core.api import store as store_mod
from core.api.discover import BadRequest, NotReady
from core.api.store import FIXTURES, BigQueryStore, FixtureStore

STEP = "7107ad863306852108ebb88392f1d5e0293922af5f5e7a5a4b81d466930f646e"
HER = "afe2bf2632b65cd9b5354f5bd4272ea81a4f7e004d01b3615fa9b479990a5156"
TOPIC = "4b942b2ea393cbe2c2e4e883ed5be7441b361229e446c6c5bbcb7fdba540fa33"
BOT = "1fde20148f78cbd64749e8ad9f4ed50c5ab2843d53d5ad7d661e7a230554e5bf"
BRAND = "31550a81e157653d924381e70a8a17f45012b04a85e496ade43753036a6f8e36"
TO = "2026-09-30"
AGG_RUN = "r_aggregate_20260930_01"
KEYWORD_NOTE = "counts the posts 42 linked to it plus posts that name it; name matches may include unrelated uses"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def window(days):
    end = dt.date.fromisoformat(TO)
    return [(end - dt.timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def expected(item_id, market, days, platform=None, terms=None):
    """Recomputes a subject's numbers straight from the fixture rows, without the store or compare."""
    posts = {p["post_id"]: p for p in load("posts")}
    ids = {r["post_id"] for r in load("post_items") if r["item_id"] == item_id}
    if terms is not None:
        ids |= {pid for pid, p in posts.items() for t in terms for f in ("text", "transcript")
                if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", p.get(f) or "", re.IGNORECASE)}
    first = {}
    for o in load("post_observations"):
        if o["market"] != market or o["lane_class"] == "legacy" or o["lane"] in ("legacy", "agent_live"):
            continue
        first[o["post_id"]] = min(first.get(o["post_id"], "9999-12-31"), o["observed_date"])
    hits = [pid for pid in ids if pid in first and (platform is None or posts[pid]["platform"] == platform)]
    days_ = window(days)
    win = [pid for pid in hits if days_[0] <= first[pid] <= days_[-1]]
    health = load("collection_health")
    daily = Counter(first[pid] for pid in win)
    valid = {d for d in days_ if any(h["day"] == d and h["market"] == market and h["valid"]
                                     and (platform is None or h["platform"] == platform) for h in health)}
    return {
        "posts": len(win),
        "creators": len({posts[pid]["creator_id"] for pid in win} - {None}),
        "engagement": sum((posts[pid]["likes"] or 0) + (posts[pid]["comments"] or 0) + (posts[pid]["shares"] or 0)
                          for pid in win),
        "platforms": len({posts[pid]["platform"] for pid in win}),
        "first_seen": min((first[pid] for pid in hits), default=None),
        "points": [{"date": d, "value": daily.get(d, 0) if d in valid else None} for d in days_],
    }


def values(body, metric):
    row = next(r for r in body["rows"] if r["metric"] == metric)
    return {k: (None if v is None else v["value"]) for k, v in row["values"].items()}


def is_figure(f, run_id=None):
    assert set(f) == {"value", "unit", "query_id", "run_id", "result_hash"}
    assert f["query_id"].startswith("q_") and re.fullmatch(r"sha256:[0-9a-f]{64}", f["result_hash"])
    if run_id:
        assert f["run_id"] == run_id


@pytest.fixture
def fx():
    return FixtureStore()


# Items mode: linked hashtags, keyword brand and topic, a held-back item, all in one market.

def test_items_mode_numbers_match_the_fixture_rows(fx):
    body = compare.build_compare(fx, "items", items=f"{STEP},{HER},{TOPIC},{BRAND},{BOT}", market="ZA", days=7)
    assert body["mode"] == "items"
    assert body["window"] == {"from": "2026-09-24", "to": TO, "days": 7}
    subjects = {s["item_id"]: s for s in body["subjects"]}
    keys = {s["item_id"]: s["key"] for s in body["subjects"]}
    assert [s["key"] for s in body["subjects"]] == ["s1", "s2", "s3", "s4", "s5"]
    assert {i: s["matched_by"] for i, s in subjects.items()} == {
        STEP: "linked", HER: "linked", TOPIC: "keyword", BRAND: "keyword", BOT: "linked"}
    assert all(s["market"] == "ZA" and s["platform"] is None for s in body["subjects"])
    assert subjects[BRAND]["label"] == "Fixture Cola"
    assert subjects[TOPIC]["label"] == "fixture za rising topic"  # the current map row, not the retired one
    terms = {TOPIC: ["fixture za rising topic"], BRAND: ["Fixture Cola", "FixCola"]}
    for item_id, key in keys.items():
        exp = expected(item_id, "ZA", 7, terms=terms.get(item_id))
        for metric in ("posts", "creators", "engagement", "platforms", "first_seen"):
            assert values(body, metric)[key] == exp[metric], (item_id, metric)
        series = next(s for s in body["series"] if s["key"] == key)
        assert series["unit"] == "posts a day" and series["points"] == exp["points"], item_id
    for row in body["rows"]:
        for f in row["values"].values():
            if f is not None:
                is_figure({k: v for k, v in f.items() if not (row["metric"] == "state" and k == "word")})
    for metric in ("posts", "creators", "engagement", "platforms", "first_seen"):
        row = next(r for r in body["rows"] if r["metric"] == metric)
        assert all(f["run_id"] == AGG_RUN and f["query_id"] == "q_compare" for f in row["values"].values())


def test_items_mode_hand_counted_values(fx):
    body = compare.build_compare(fx, "items", items=f"{STEP},{HER},{BRAND}", market="ZA", days=7)
    s_step, s_her, s_brand = "s1", "s2", "s3"
    # p01, p04, p16 first seen 29 Sep; p02, p06, p17 on 30 Sep; p03 first seen 10 Sep (re-sighted 30 Sep, not new);
    # p04's legacy sighting on 20 Sep is ignored; p05 was only seen by an agent_live call.
    assert values(body, "posts") == {s_step: 6, s_her: 2, s_brand: 2}
    assert values(body, "creators") == {s_step: 4, s_her: 2, s_brand: 2}  # c1 twice, p16 has no creator
    assert values(body, "engagement") == {s_step: 188, s_her: 16, s_brand: 9}
    assert values(body, "platforms") == {s_step: 3, s_her: 2, s_brand: 2}
    assert values(body, "first_seen") == {s_step: "2026-09-10", s_her: "2026-08-20", s_brand: "2026-09-26"}
    step_points = next(s for s in body["series"] if s["key"] == s_step)["points"]
    assert [p["value"] for p in step_points] == [None, None, None, None, None, 3, 3]
    her_points = next(s for s in body["series"] if s["key"] == s_her)["points"]
    assert [p["value"] for p in her_points] == [None, None, None, None, None, 0, 1]  # 24 Sep had no collection
    body28 = compare.build_compare(fx, "items", items=f"{STEP},{HER}", market="ZA", days=28)
    assert body28["window"]["from"] == "2026-09-03"
    assert values(body28, "posts")["s1"] == 7 and values(body28, "creators")["s1"] == 5


class Counters(FixtureStore):
    """The fixture posts with some posts' likes, comments and shares replaced."""

    def __init__(self, counters):
        super().__init__()
        self.counters = counters

    def _optional(self, name):
        rows = super()._optional(name)
        if name == "posts":
            rows = [dict(p, **self.counters[p["post_id"]]) if p["post_id"] in self.counters else p for p in rows]
        return rows


def test_engagement_unreported_by_every_post_is_unknown_and_a_partial_total_says_so():
    unreported = {"likes": None, "comments": None, "shares": None}
    # HER's two window posts (p11, p12) report no counter; STEP's p17 neither, its other five do.
    store = Counters({"p10": unreported, "p11": unreported, "p12": unreported, "p17": unreported})
    body = compare.build_compare(store, "items", items=f"{STEP},{HER},{BRAND}", market="ZA", days=7)
    assert values(body, "posts") == {"s1": 6, "s2": 2, "s3": 2}
    assert values(body, "engagement") == {"s1": 185, "s2": None, "s3": 9}
    assert "Engagement for #fixture_za_heritage: none of its 2 posts report likes, comments or shares" in body["notes"]
    assert ("Engagement for #fixture_za_step counts 5 of its 6 posts; the others report no likes, comments or "
            "shares") in body["notes"]
    assert not any(n.startswith("Engagement for Fixture Cola") for n in body["notes"])
    # Explicit zero counters are a measured zero, with no note.
    zero = {"likes": 0, "comments": 0, "shares": 0}
    body = compare.build_compare(Counters({"p10": zero, "p11": zero, "p12": zero}), "items",
                                 items=f"{STEP},{HER}", market="ZA", days=7)
    assert values(body, "engagement")["s2"] == 0
    assert not any(n.startswith("Engagement for") for n in body["notes"])


def test_items_mode_whole_word_keyword_match_and_note(fx):
    body = compare.build_compare(fx, "items", items=f"{STEP},{BRAND},{TOPIC}", market="ZA", days=7)
    # Fixture Cola: p06 text and p09 transcript "fixcola!"; never p08 "Fixture Colas"
    assert values(body, "posts")["s2"] == 2
    # the topic: p07 in capitals; never p12 "topicality", never p14 which was only seen in Nigeria
    assert values(body, "posts")["s3"] == 1
    notes = body["notes"]
    assert f"Fixture Cola {KEYWORD_NOTE}" in notes and f"fixture za rising topic {KEYWORD_NOTE}" in notes
    assert not any("#fixture_za_step" in n for n in notes)


class ClusterLinked(FixtureStore):
    """The fixture with the topic linked through post_items (via cluster) to one ZA post that does not name it."""

    def __init__(self, post_id):
        super().__init__()
        self.post_id = post_id

    def _optional(self, name):
        rows = super()._optional(name)
        if name == "post_items":
            rows = rows + [{"post_id": self.post_id, "item_id": TOPIC, "via": "cluster"}]
        return rows


# Visual QA, 5 October 2026 (CP01): a topic matched by name alone read 0 posts in 28 days beside its card's 18
# creators in 3 days. A topic also counts the posts detect linked to it, the population its card reads.
def test_a_keyword_subject_also_counts_its_linked_posts(fx):
    before = values(compare.build_compare(fx, "items", items=f"{STEP},{TOPIC}", market="ZA", days=7), "posts")["s2"]
    linked = next(r["post_id"] for r in load("post_items") if r["item_id"] == STEP)
    body = compare.build_compare(ClusterLinked(linked), "items", items=f"{STEP},{TOPIC}", market="ZA", days=7)
    assert values(body, "posts")["s2"] == before + 1
    assert f"fixture za rising topic {KEYWORD_NOTE}" in body["notes"]


def test_items_mode_growth_reach_state_from_item_state_at_window_to(fx):
    body = compare.build_compare(fx, "items", items=f"{STEP},{BRAND},{BOT}", market="ZA", days=28)
    metrics = [r["metric"] for r in body["rows"]]
    assert metrics == ["posts", "creators", "engagement", "first_seen", "growth", "reach", "state", "platforms"]
    words = {r["metric"]: r["words"] for r in body["rows"]}
    assert words["creators"] == "Distinct creators in the window"
    assert words["first_seen"] == "First seen in 42's collection (all time, not clipped to the window)"
    reach = next(r for r in body["rows"] if r["metric"] == "reach")["values"]
    state = next(r for r in body["rows"] if r["metric"] == "state")["values"]
    growth = next(r for r in body["rows"] if r["metric"] == "growth")["values"]
    assert reach["s1"]["value"] == 31 and reach["s1"]["run_id"] == "r_detect_20260930_01"
    assert state["s1"]["value"] == "emerging" and state["s1"]["word"] == "Emerging"
    assert growth["s1"] is None  # warm-up: growth needs 14 days
    assert reach["s2"] is None and state["s2"] is None  # the brand is not in item_state
    cards = {s["key"]: s["card"] for s in body["subjects"]}
    assert cards["s1"]["item_id"] == STEP and cards["s1"]["reach"] == reach["s1"]
    assert cards["s2"] is None
    assert cards["s3"]["held_back"]["reason"] == "likely_coordinated"  # compared like any other
    assert values(body, "posts")["s3"] == 1


def test_coverage_note_is_always_there(fx):
    body = compare.build_compare(fx, "items", items=f"{STEP},{HER}", market="ZA", days=7)
    assert body["notes"][0] == "Coverage: 7 of 8 South Africa series-days usable over the window"


# Markets mode: one item in two or three markets.

def test_markets_mode_counts_each_market_on_its_own_first_sighting(fx):
    body = compare.build_compare(fx, "markets", items=STEP, markets="ZA,NG,KE", days=7)
    assert [(s["key"], s["market"], s["item_id"]) for s in body["subjects"]] == [
        ("s1", "ZA", STEP), ("s2", "NG", STEP), ("s3", "KE", STEP)]
    for s in body["subjects"]:
        exp = expected(STEP, s["market"], 7)
        for metric in ("posts", "creators", "engagement", "platforms", "first_seen"):
            assert values(body, metric)[s["key"]] == exp[metric], (s["market"], metric)
        assert next(x for x in body["series"] if x["key"] == s["key"])["points"] == exp["points"]
    assert values(body, "posts") == {"s1": 6, "s2": 1, "s3": 1}  # p01 counts once in ZA and once in NG
    ke = next(x for x in body["series"] if x["key"] == "s3")["points"]
    assert [p["value"] for p in ke][-2:] == [0, 1]  # Kenya's TikTok feed failed on 30 Sep, YouTube did not
    assert body["notes"][0] == ("Coverage differs: 7 of 8 South Africa series-days usable, 5 of 7 Nigeria "
                                "series-days usable, 4 of 6 Kenya series-days usable over the window; read "
                                "differences with care")
    assert [r["metric"] for r in body["rows"]][-4:] == ["growth", "reach", "state", "platforms"]
    cards = {s["market"]: s["card"] for s in body["subjects"]}
    assert cards["ZA"]["item_id"] == STEP and cards["NG"] is None and cards["KE"] is None


# Platforms mode: one item in one market across platforms.

def test_platforms_mode_splits_by_post_platform_and_omits_item_state_rows(fx):
    body = compare.build_compare(fx, "platforms", items=STEP, market="ZA", platforms="tiktok,instagram,x", days=7)
    assert [(s["key"], s["platform"]) for s in body["subjects"]] == [("s1", "tiktok"), ("s2", "instagram"),
                                                                       ("s3", "x")]
    assert [r["metric"] for r in body["rows"]] == ["posts", "creators", "engagement", "first_seen"]
    for s in body["subjects"]:
        exp = expected(STEP, "ZA", 7, platform=s["platform"])
        for metric in ("posts", "creators", "engagement", "first_seen"):
            assert values(body, metric)[s["key"]] == exp[metric], (s["platform"], metric)
        assert next(x for x in body["series"] if x["key"] == s["key"])["points"] == exp["points"]
    assert values(body, "posts") == {"s1": 3, "s2": 2, "s3": 1}
    points = {x["key"]: [p["value"] for p in x["points"]][-2:] for x in body["series"]}
    assert points == {"s1": [2, 1], "s2": [None, None], "s3": [None, 1]}
    assert body["notes"][0] == ("Coverage differs: 2 of 3 South Africa TikTok series-days usable, no South Africa "
                                "Instagram series reported, 1 of 1 South Africa X series-days usable over the "
                                "window; read differences with care")


# Arguments.

@pytest.mark.parametrize("kwargs", [
    {"mode": "brands", "items": f"{STEP},{HER}", "market": "ZA"},
    {"mode": "items", "items": STEP, "market": "ZA"},
    {"mode": "items", "items": ",".join([STEP, HER, TOPIC, BRAND, BOT, "8e23ba63bae911fc0e2fe5a5caaa53559c0fee60600421cd02acffaa756fade9"]), "market": "ZA"},
    {"mode": "items", "items": f"{STEP},{STEP}", "market": "ZA"},
    {"mode": "items", "items": f"{STEP},unknown_item", "market": "ZA"},
    {"mode": "items", "items": f"{STEP},{HER}", "market": "XX"},
    {"mode": "items", "items": f"{STEP},{HER}", "market": "ZA", "days": 10},
    {"mode": "markets", "items": STEP, "markets": "ZA"},
    {"mode": "markets", "items": f"{STEP},{HER}", "markets": "ZA,NG"},
    {"mode": "markets", "items": STEP, "markets": "ZA,GH"},
    {"mode": "markets", "items": STEP, "markets": "ZA,ZA"},
    {"mode": "platforms", "items": STEP, "market": "ZA", "platforms": "tiktok"},
    {"mode": "platforms", "items": STEP, "market": "ZA", "platforms": "tiktok,myspace"},
    {"mode": "platforms", "items": "unknown_item", "market": "ZA", "platforms": "tiktok,x"},
])
def test_bad_arguments_are_400(fx, kwargs):
    with pytest.raises(BadRequest):
        compare.build_compare(fx, **kwargs)


def test_arguments_are_normalised(fx):
    body = compare.build_compare(fx, "platforms", items=f" {STEP} ", market="za", platforms="TikTok, twitter")
    assert [s["platform"] for s in body["subjects"]] == ["tiktok", "x"] and body["window"]["days"] == 28


def test_no_good_aggregate_run_is_not_ready(tmp_path):
    with pytest.raises(NotReady):
        compare.build_compare(FixtureStore(root=tmp_path), "items", items=f"{STEP},{HER}", market="ZA")


def test_window_ends_on_the_latest_good_aggregate_run(fx):
    assert fx.latest_aggregate_run() == {"run_id": AGG_RUN, "run_date": TO}  # 1 October failed


def test_cap_refusal_says_so_instead_of_a_partial_answer(fx):
    class Capped(FixtureStore):
        def compare_counts(self, subjects, start, end):
            exc = RuntimeError("Query exceeded limit for bytes billed: 2000000000. 5000000000 or higher required.")
            exc.errors = [{"reason": "bytesBilledLimitExceeded"}]
            raise exc

    body = compare.build_compare(Capped(), "items", items=f"{STEP},{HER}", market="ZA")
    assert body["notes"] == ["This comparison needs more data than one question may read"]
    assert body["rows"] == [] and body["series"] == []
    assert [s["item_id"] for s in body["subjects"]] == [STEP, HER]


def test_other_failures_are_not_taken_for_a_cap_refusal(fx):
    class Broken(FixtureStore):
        def compare_counts(self, subjects, start, end):
            raise RuntimeError("unreachable")

    with pytest.raises(RuntimeError):
        compare.build_compare(Broken(), "items", items=f"{STEP},{HER}", market="ZA")


def test_cap_refused_reads_the_reason_or_the_message():
    exc = RuntimeError("x")
    exc.errors = [{"reason": "bytesBilledLimitExceeded"}]
    assert store_mod.cap_refused(exc)
    assert store_mod.cap_refused(RuntimeError("Query exceeded limit for bytes billed: 2000000000."))
    assert not store_mod.cap_refused(RuntimeError("Not found: Table posts"))


# BigQuery: parameterised, capped, and the SQL follows the one market and day rule.

CORE_OBJECTS = ("posts", "post_observations", "post_items", "cultural_map", "v_collection_health_current",
                "v_good_runs", "v_item_state_current")
CATALOG = [{"ds": "intelligence_42_core", "n": n, "what": "table"} for n in CORE_OBJECTS] + [
    {"ds": "intelligence_42_agent", "n": "runs", "what": "table"}]


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


SUBJECTS = [{"key": "s1", "item_id": STEP, "market": "ZA", "platform": None, "terms": None},
            {"key": "s2", "item_id": BRAND, "market": "ZA", "platform": None, "terms": ["Fixture Cola", "Fix.Cola"]}]


def compare_sql(client):
    return next(s for s, _ in client.calls if "post_observations" in s)


def test_bigquery_compare_sql_follows_the_market_and_day_rule():
    client = FakeClient(CATALOG)
    BigQueryStore(client=client).compare_counts(SUBJECTS, "2026-09-24", TO)
    sql = compare_sql(client)
    assert "post_date" not in sql  # posts joined by post_id, no partition filter that would drop old posts
    sight = re.search(r"sight AS \((.*?)\)\s*,", sql, re.S).group(1)
    assert "MIN(po.observed_date)" in sight and "GROUP BY po.post_id, po.market" in sight
    assert "@start" not in sight and "@end" not in sight  # first sighting over all partitions
    assert re.search(r"first_day BETWEEN @start AND @end", sql)  # the window applies after it
    assert "po.lane_class != 'legacy'" in sight and "IFNULL(po.lane, '') NOT IN ('legacy', 'agent_live')" in sight
    assert re.search(r"COUNT\(DISTINCT \w+\.creator_id\)", sql)
    assert re.search(r"COUNT\(DISTINCT \w+\.post_id\)", sql)
    assert "JOIN `ogilvy-trends-v2.intelligence_42_core.post_items`" in sql
    assert "REGEXP_CONTAINS" in sql and "p.transcript" in sql and "p.text" in sql
    assert not re.search(r"\b(INSERT|UPDATE|MERGE|CREATE|DELETE)\b", sql)


def test_bigquery_compare_keeps_unreported_engagement_apart_from_zero():
    client = FakeClient(CATALOG)
    BigQueryStore(client=client).compare_counts(SUBJECTS, "2026-09-24", TO)
    sql = compare_sql(client)
    assert "IF(COALESCE(p.likes, p.comments, p.shares) IS NULL, NULL," in sql
    assert "COUNT(DISTINCT IF(w.engagement IS NULL, NULL, w.post_id)) AS engagement_posts" in sql
    assert "IF(IFNULL(t.posts, 0) > 0 AND t.engagement_posts = 0, NULL, IFNULL(t.engagement, 0)) AS engagement" in sql


def test_bigquery_compare_is_parameterised_and_capped():
    client = FakeClient(CATALOG)
    bq = BigQueryStore(client=client)
    bq.compare_counts(SUBJECTS, "2026-09-24", TO)
    bq.latest_aggregate_run()
    bq.map_items([STEP, BRAND])
    bq.health_range("2026-09-24", TO, ["ZA"])
    for sql, cfg in client.calls:
        assert cfg.maximum_bytes_billed == 2_000_000_000
        if "INFORMATION_SCHEMA" not in sql:
            assert STEP not in sql and "'ZA'" not in sql and TO not in sql and "Cola" not in sql
    params = {p.name: p for _, cfg in client.calls for p in cfg.query_parameters}
    assert {"start", "end", "markets", "item_ids"} <= set(params)
    patterns = [p.value for p in params.values() if isinstance(getattr(p, "value", None), str)
                and "Cola" in p.value]
    assert patterns == [r"(?i)(?:^|[^\p{L}\p{N}_])(?:Fixture\ Cola|Fix\.Cola)(?:$|[^\p{L}\p{N}_])"]
    linked = [p for n, p in params.items() if n.startswith("r") and n[1:].isdigit()]
    assert any(p.value is None for p in linked)  # a linked subject carries no pattern
    joined = "\n".join(s for s, _ in client.calls)
    assert "stage = 'aggregate'" in joined and "valid_to IS NULL" in joined
    assert "v_collection_health_current" in joined


def test_bigquery_compare_degrades_when_a_table_is_missing():
    client = FakeClient([r for r in CATALOG if r["n"] != "post_items"])
    assert BigQueryStore(client=client).compare_counts(SUBJECTS, "2026-09-24", TO) is None
    assert not any("post_observations" in s for s, _ in client.calls)
    store_mod._CATALOG.clear()  # the catalog is cached per project; this is a second, different project state
    client = FakeClient([r for r in CATALOG if r["n"] != "cultural_map"])
    assert BigQueryStore(client=client).map_items([STEP]) is None


def test_keyword_subject_with_no_words_is_refused_not_matched_to_everything():
    from core.api import compare

    with pytest.raises(compare.BadRequest):
        compare._terms_or_refuse({"item_id": "x1", "label": "", "canonical_key": None, "aliases": []})
    assert compare._terms_or_refuse({"item_id": "x2", "label": "Mzansi", "aliases": ["mzansi"]}) == ["Mzansi"]


def test_twitter_rows_count_as_x_in_platform_mode_sql_and_fixtures():
    from core.api import store as store_mod

    sql = store_mod.BigQueryStore.__dict__["compare_counts"].__code__.co_consts
    text = " ".join(c for c in sql if isinstance(c, str))
    assert "IF(p.platform = 'twitter', 'x', p.platform)" in text
    assert store_mod.canon_platform("twitter") == "x" and store_mod.canon_platform("tiktok") == "tiktok"


# Visual QA, 5 October 2026 (CP02): Compare and Lexicon date posts differently, and Compare now says so.
def test_posts_row_and_a_note_name_the_counting_basis(fx):
    body = compare.build_compare(fx, "items", items=f"{STEP},{HER}", market="ZA", days=28)
    assert {r["metric"]: r["words"] for r in body["rows"]}["posts"] == "Posts first seen in the window"
    assert compare.BASIS_NOTE in body["notes"]
    assert "Lexicon" in compare.BASIS_NOTE
