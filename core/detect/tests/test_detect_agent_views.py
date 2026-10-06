"""Known-answer tests for the read-only views lane L4's API reads from L2 (contract sections 10.7 and 12.1,
DATA.md section 7): v_item_gate_current, v_item_evidence, tvf_item_timeseries, v_sensitive_items and
v_item_tone_daily.

The SQL under test is BigQuery Standard SQL in sql/agent_views.sql; duck.py runs it on DuckDB against
fixture tables. The BigQuery dry runs at the end are skipped unless F42_BQ=1.
"""

import json
import os
import re

import pytest
import sqlglot
import yaml

from .. import items, sqlrun
from . import duck
from .fixtures import D, at, cmap, counter, creator, day, health, obs, post, run

AGENT_OBJECTS = ["agent.v_item_gate_current", "agent.v_item_evidence", "agent.tvf_item_timeseries",
                 "core.v_sensitive_items", "agent.v_item_tone_daily"]


@pytest.fixture
def con():
    c = duck.connect(views=False)
    # Columns and tables L1's DDL has on staging that the shared DuckDB harness does not carry yet.
    c.execute("ALTER TABLE agent.briefs ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ")
    c.execute("ALTER TABLE agent.briefs ADD COLUMN IF NOT EXISTS status VARCHAR")
    c.execute("ALTER TABLE agent.briefs ADD COLUMN IF NOT EXISTS payload JSON")
    c.execute("ALTER TABLE agent.briefs ADD COLUMN IF NOT EXISTS rule_version VARCHAR")
    c.execute("CREATE TABLE IF NOT EXISTS core.media (sha256 VARCHAR, post_id VARCHAR, gcs_uri VARCHAR, kind VARCHAR)")
    for stmt in sqlrun.statements("core", "agent") + sqlrun.agent_statements("core", "agent"):
        c.execute(duck.create_statement(stmt))
    yield c
    c.close()


# sqlrun: statements, rendering and apply order


def test_agent_statements_name_every_object_in_order():
    stmts = sqlrun.agent_statements("core", "agent")
    assert [sqlrun.object_name(s) for s in stmts] == AGENT_OBJECTS
    default = [sqlrun.object_name(s) for s in sqlrun.agent_statements()]
    assert default == ["intelligence_42_agent.v_item_gate_current", "intelligence_42_agent.v_item_evidence",
                       "intelligence_42_agent.tvf_item_timeseries", "intelligence_42_core.v_sensitive_items",
                       "intelligence_42_agent.v_item_tone_daily"]
    for s in stmts:
        assert "{core}" not in s and "{agent}" not in s and "{sensitive_terms}" not in s
        assert not s.rstrip().endswith(";")


def test_agent_statements_parse_as_bigquery_and_only_create_views_or_table_functions():
    for s in sqlrun.agent_statements():
        assert re.match(r"CREATE OR REPLACE (VIEW|TABLE FUNCTION) ", s)
        assert sqlglot.parse_one(duck.split_create(s)[3], read="bigquery") is not None
        body = duck.split_create(s)[3].upper()
        for word in ("INSERT", "MERGE", "DELETE", "DROP", "TRUNCATE", "UPDATE", "ALTER"):
            assert not re.search(rf"\b{word}\b", body), word


class FakeJob:
    def result(self):
        return []


class FakeClient:
    def __init__(self):
        self.sql = []

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        return FakeJob()


def test_apply_agent_views_runs_every_create_in_order():
    client = FakeClient()
    names = sqlrun.apply_agent_views(client, core="core", agent="agent")
    assert names == AGENT_OBJECTS
    assert [sqlrun.object_name(s) for s in client.sql] == AGENT_OBJECTS


# v_item_gate_current


def market_payload(market, cards=(), more=(), held=(), moments=()):
    return {"market": market, "status": "published", "headline": None, "banners": [],
            "cards": [{"item_id": i, "rank": r} for r, i in enumerate(cards, 1)],
            "more": [{"item_id": i, "rank": r} for r, i in enumerate(more, 6)],
            "held_back": {"count": len(held), "text": "", "items": list(held)},
            "moments": list(moments), "boards": [], "coverage": {"issues": []}}


def brief(market, run_id, payload, d=D):
    return {"brief_date": d, "market": market, "run_id": run_id, "status": payload.get("status"),
            "payload": json.dumps(payload), "rule_version": "r1"}


def gate(con):
    rows = duck.query(con, "SELECT g.item_id, g.market, g.brief_date, g.rule, g.reason, g.reason_text, g.place "
                           "FROM {agent}.v_item_gate_current g")
    return sorted((tuple(r.values()) for r in rows), key=lambda t: tuple(str(v) for v in t))


def test_gate_reads_the_latest_good_brief_per_market_and_date(con):
    duck.load(con, "agent.runs", [run("brief", D, run_id="brief-a", hour=5),
                                  run("brief", D, run_id="brief-b", hour=6),
                                  run("brief", D, run_id="brief-c", status="failed", hour=7),
                                  run("brief", day(1), run_id="brief-y", hour=6)])
    held = {"item_id": "h1", "title": "#h1", "rule": "G5", "reason": "paid_led", "reason_text": "Paid-led",
            "evidence_ids": [], "evidence": []}
    held_own = {"item_id": "h2", "title": "#h2", "rule": None, "reason": "too_few_creators",
                "reason_text": "Too few creators", "evidence_ids": [], "evidence": []}
    moments = [{"date": "2026-09-20", "name": "#s1", "kind": "seasonal_item", "source": "detect", "item_ids": ["s1"]},
               {"date": "2026-09-24", "name": "Heritage Day", "kind": "holiday", "source": "calendar",
                "item_ids": ["c1"]}]
    duck.load(con, "agent.briefs", [
        brief("ZA", "brief-a", market_payload("ZA", cards=["old"])),
        brief("ZA", "brief-b", market_payload("ZA", cards=["c1", "c2"], more=["m1"], held=[held, held_own],
                                              moments=moments)),
        brief("NG", "brief-b", market_payload("NG", cards=["n1"])),
        brief("ZA", "brief-c", market_payload("ZA", cards=["failed"])),
        brief("KE", "brief-y", market_payload("KE", held=[{**held, "item_id": "k1"}]), d=day(1)),
    ])
    assert gate(con) == sorted([
        ("c1", "ZA", D, None, None, None, "today"),
        ("c2", "ZA", D, None, None, None, "today"),
        ("m1", "ZA", D, None, None, None, "today"),
        ("h1", "ZA", D, "G5", "paid_led", "Paid-led", "held_back"),
        ("h2", "ZA", D, None, "too_few_creators", "Too few creators", "held_back"),
        ("s1", "ZA", D, None, None, None, "moments"),
        ("n1", "NG", D, None, None, None, "today"),
        ("k1", "KE", day(1), "G5", "paid_led", "Paid-led", "held_back"),
    ], key=lambda t: tuple(str(v) for v in t))


def test_gate_survives_a_data_issue_payload_with_empty_or_missing_lists(con):
    duck.load(con, "agent.runs", [run("brief", D, run_id="brief-a")])
    bare = {"market": "ZA", "status": "data_issue", "banners": [{"kind": "data_issue", "text": "x"}], "cards": []}
    duck.load(con, "agent.briefs", [brief("ZA", "brief-a", bare),
                                    brief("NG", "brief-a", market_payload("NG", held=[]))])
    assert gate(con) == []


# v_item_evidence

EVIDENCE_COLUMNS = ["post_id", "item_id", "market", "platform", "url", "handle", "creator_tier", "excerpt",
                    "views", "likes", "comments", "shares", "engagement", "thumbnail_url", "duration_s",
                    "clip_uri", "published_at", "first_seen_at"]


def evidence(con, item_id):
    return duck.query(con, "SELECT e.* FROM {agent}.v_item_evidence e WHERE e.item_id = @i "
                           "ORDER BY e.post_id, e.market", {"i": item_id})


def test_evidence_lists_each_post_per_market_without_excluded_lanes_or_coord_score(con):
    long_text = "a" * 150 + " " + "b" * 200
    duck.load(con, "core.creators", [creator("cr1", coord_score=3), creator("cr2")])
    duck.load(con, "core.posts", [
        post("p1", "cr1", day(2), tier="macro", text=long_text, transcript="spoken", url="https://t/p1",
             thumbnail_url="https://t/p1.jpg", duration_s=21.0, views=100, likes=10, comments=2, shares=1,
             engagement=13),
        post("p2", "cr2", day(1), text="", transcript="words from the clip", url="https://t/p2"),
        post("p3", "cr2", day(1), text="legacy only"),
        post("p4", "cr2", day(1), text="placebo only"),
        post("p5", "cr2", day(1), text="agent live only"),
        post("p6", "cr2", day(1), text="seen in two markets"),
    ])
    duck.load(con, "core.post_items", [{"post_id": p, "item_id": "i1", "via": "hashtag"}
                                       for p in ("p1", "p2", "p3", "p4", "p5", "p6")]
                                      + [{"post_id": "p1", "item_id": "i2", "via": "hashtag"}])
    duck.load(con, "core.post_observations", [
        obs("p1", day(2), "legacy", "legacy", hour=8),
        obs("p1", day(2), "unbiased_rank", "sweep", hour=10),
        obs("p1", day(1), "unbiased_rank", "sweep", hour=10),
        obs("p2", day(1), "search_presence", "expansion"),
        obs("p3", day(1), "legacy", "legacy"),
        obs("p4", day(1), "search_presence", "placebo"),
        obs("p5", day(1), "search_presence", "agent_live"),
        obs("p6", day(1), "unbiased_rank", "sweep", market="ZA", hour=9),
        obs("p6", D, "unbiased_rank", "sweep", market="NG", hour=11),
    ])
    duck.load(con, "core.media", [{"sha256": "s1", "post_id": "p1", "gcs_uri": "gs://m/p1.mp4", "kind": "clip"},
                                  {"sha256": "s2", "post_id": "p1", "gcs_uri": "gs://m/p1.jpg", "kind": "thumbnail"},
                                  {"sha256": "s3", "post_id": "p2", "gcs_uri": "gs://m/p2.jpg", "kind": "keyframe"}])
    rows = evidence(con, "i1")
    assert list(rows[0]) == EVIDENCE_COLUMNS
    assert [(r["post_id"], r["market"]) for r in rows] == [("p1", "ZA"), ("p2", "ZA"), ("p6", "NG"), ("p6", "ZA")]
    p1, p2, p6ng, p6za = rows
    assert p1["excerpt"] == long_text[:280] and len(p1["excerpt"]) == 280
    assert (p1["platform"], p1["url"], p1["handle"], p1["creator_tier"]) == ("tiktok", "https://t/p1", "cr1", "macro")
    assert (p1["views"], p1["likes"], p1["comments"], p1["shares"], p1["engagement"]) == (100, 10, 2, 1, 13)
    assert (p1["thumbnail_url"], p1["duration_s"], p1["clip_uri"]) == ("https://t/p1.jpg", 21.0, "gs://m/p1.mp4")
    assert p1["published_at"] == at(day(2), 9)
    assert p1["first_seen_at"] == at(day(2), 10)            # the legacy sighting at 08:00 does not count
    assert p2["excerpt"] == "words from the clip" and p2["clip_uri"] is None
    assert p6za["first_seen_at"] == at(day(1), 9) and p6ng["first_seen_at"] == at(D, 11)
    assert all("coord" not in c for c in rows[0])
    assert [r["post_id"] for r in evidence(con, "i2")] == ["p1"]


# tvf_item_timeseries


def timeseries(con, item_id, market, days):
    return duck.query(con, "SELECT t.series_id, t.day, t.value, t.trials, t.lane_class "
                           "FROM {agent}.tvf_item_timeseries(@i, @m, @n) t ORDER BY t.series_id, t.day",
                      {"i": item_id, "m": market, "n": days})


def test_timeseries_gives_one_row_per_series_and_day_with_null_gaps(con):
    days = [day(i) for i in range(5, -1, -1)]
    duck.load(con, "agent.runs", [run("collect", d) for d in days])
    kw = dict(lane_class="unbiased_counter", unit="delta")
    duck.load(con, "core.collection_health",
              [health("feed_tiktok", d, valid=(d != day(2))) for d in days]
              + [health("feed_tiktok", d, market="NG") for d in days]
              + [health("counter_tiktok_hashtag", d, lane_class="unbiased_counter") for d in days])
    duck.load(con, "core.item_counter_daily", [
        counter("itA", "feed_tiktok", day(4), 1.0),
        counter("itA", "feed_tiktok", day(1), 2.0),
        counter("itA", "feed_tiktok", D, 3.0),
        counter("itA", "feed_tiktok", D, 7.0, market="NG"),
        counter("itB", "feed_tiktok", D, 9.0),
        counter("itA", "counter_tiktok_hashtag", day(1), 40.0, **kw),
        counter("itA", "counter_tiktok_hashtag", D, 50.0, **kw),
    ])
    rows = timeseries(con, "itA", "ZA", 4)
    assert [(r["series_id"], r["day"], r["value"]) for r in rows] == [
        ("itA|ZA|counter_tiktok_hashtag|p1", day(3), None),     # before the first read: a gap, not a zero
        ("itA|ZA|counter_tiktok_hashtag|p1", day(2), None),
        ("itA|ZA|counter_tiktok_hashtag|p1", day(1), 40.0),
        ("itA|ZA|counter_tiktok_hashtag|p1", D, 50.0),
        ("itA|ZA|feed_tiktok|p1", day(3), 0.0),                 # valid day, not on the list
        ("itA|ZA|feed_tiktok|p1", day(2), None),                # invalid collection day
        ("itA|ZA|feed_tiktok|p1", day(1), 2.0),
        ("itA|ZA|feed_tiktok|p1", D, 3.0),
    ]
    assert {r["lane_class"] for r in rows[4:]} == {"unbiased_rank"} and rows[7]["trials"] == 3
    assert [(r["day"], r["value"]) for r in timeseries(con, "itA", "NG", 1)] == [(D, 7.0)]
    assert timeseries(con, "itA", "KE", 28) == []
    assert timeseries(con, "itA", "ZA", 0) == []


# v_item_tone_daily


def tones(con, item_id):
    return duck.query(con, "SELECT t.metric_date, t.market, t.enriched_posts, t.tone FROM {agent}.v_item_tone_daily t "
                           "WHERE t.item_id = @i ORDER BY t.metric_date, t.market", {"i": item_id})


def toned(con, rows, d, item="i1", market="ZA", lane_class="unbiased_rank", lane="sweep"):
    """One post per (post_id, tone) dated d, linked to item and sighted in market; tone None is not enriched."""
    duck.load(con, "core.posts", [post(pid, "cr1", d) for pid, _ in rows])
    duck.load(con, "core.post_items", [{"post_id": pid, "item_id": item, "via": "hashtag"} for pid, _ in rows])
    duck.load(con, "core.post_observations", [obs(pid, d, lane_class, lane, market=market) for pid, _ in rows])
    duck.load(con, "core.post_enrichment", [{"post_id": pid, "tone": tone} for pid, tone in rows if tone])


def test_tone_is_the_mean_polarity_of_the_items_enriched_posts_per_market_and_day(con):
    toned(con, [("a1", "celebratory"), ("a2", "hopeful"), ("a3", "humorous"), ("a4", "angry"),
                ("a5", "neutral"), ("a6", None)], D)
    toned(con, [("b1", "sad"), ("b2", "sarcastic"), ("b3", "angry"), ("b4", "informative"), ("b5", "mixed")], day(1))
    toned(con, [("c1", "celebratory")] * 1 + [(f"c{i}", "hopeful") for i in range(2, 6)], D, market="NG")
    assert [(r["metric_date"], r["market"], r["enriched_posts"], r["tone"]) for r in tones(con, "i1")] == [
        (day(1), "ZA", 5, -0.6), (D, "NG", 5, 1.0), (D, "ZA", 5, 0.4)]


def test_tone_needs_five_enriched_posts_and_counts_each_post_once(con):
    toned(con, [("a1", "angry"), ("a2", "angry"), ("a3", "angry"), ("a4", "angry")], D)
    duck.load(con, "core.post_items", [{"post_id": "a1", "item_id": "i1", "via": "sound"}])
    duck.load(con, "core.post_enrichment", [{"post_id": "a1", "tone": "angry"}])
    duck.load(con, "core.post_observations", [obs("a1", D, "unbiased_rank", "sweep", hour=15)])
    toned(con, [("x1", "angry")], D, lane_class="legacy", lane="legacy")
    toned(con, [("x2", "angry")], D, lane_class="search_presence", lane="placebo")
    toned(con, [("x3", "angry")], D, lane_class="search_presence", lane="agent_live")
    assert [(r["enriched_posts"], r["tone"]) for r in tones(con, "i1")] == [(4, None)]
    toned(con, [("a5", "celebratory")], D)
    assert [(r["enriched_posts"], r["tone"]) for r in tones(con, "i1")] == [(5, -0.6)]


# v_sensitive_items


REASONS = {"political", "religion", "health", "sex_life", "race_ethnicity", "crime"}


def test_sensitive_terms_come_from_the_two_yaml_lists_with_their_key_rule():
    terms = {(r, t): rule for r, t, rule in sqlrun.sensitive_terms()}
    listed = yaml.safe_load(sqlrun.SENSITIVE_YAML.read_text(encoding="utf-8"))
    gives = listed.pop("gives_way_to")
    ordinary = listed.pop("ordinary")
    assert gives == sqlrun.gives_way()
    labels = set(listed.pop("labels_only"))
    keys = set(listed.pop("keys_only"))
    words_only = set(listed.pop("whole_words_in_keys"))
    starts = set(listed.pop("start_of_keys"))
    inside = set(listed.pop("anywhere_in_keys"))
    assert set(listed.pop("never_sensitive")) == set(sqlrun.never_sensitive())
    assert set(listed) == REASONS
    for reason, words in listed.items():
        assert words
        for w in words:
            want = ("label" if w in labels else "keys" if w in keys else "words" if w in words_only
                    else "starts" if w in starts else "inside" if w in inside else "edges")
            assert terms[(reason, w)] == want, (reason, w)
    assert labels == {"PrEP", "419"}
    assert inside == {"gbv", "gay"}
    assert {"eid", "rape", "raped", "aids", "race", "pray", "rapist", "luo", "std"} <= starts
    assert {"sti", "gnu", "coup", "pope", "condom", "tik", "amen", "killed", "killing", "coloured", "tribe",
            "trans", "juju", "indian"} <= words_only                     # they sit inside common words
    assert keys == {"tb"}                                                 # #TBThursday, never read as TB
    assert {"tiktok", "killing it", "find your tribe"} <= set(ordinary)
    assert all(terms[("ordinary", w)] == "edges" for w in ordinary)
    lists = [labels, keys, words_only, starts, inside]
    assert sum(map(len, lists)) == len(set().union(*lists))               # no term on two rule lists
    everywhere = {w for words in listed.values() for w in words}
    assert set().union(*lists) <= everywhere                              # each also sits under a reason
    for term, longer in gives.items():                 # a term gives way only to listed terms that hold it
        assert term in everywhere and longer
        for w in longer:
            assert w in everywhere and sqlrun._squash(term) in sqlrun._squash(w), (term, w)

    political = yaml.safe_load(sqlrun.POLITICAL_YAML.read_text(encoding="utf-8"))
    for words in political.values():
        for w in words:
            assert terms[("political", w)] in ("g4b", "edges")        # edges only where sensitive.yaml lists it too
    assert {r for r, _ in terms} == REASONS | {"ordinary"}


def test_every_term_has_two_letters_or_digits_to_pair_on():
    # a term that squashed to nothing would pair with every item; 419 pairs on its digits
    short = [t for _, t, _ in sqlrun.sensitive_terms() if len(sqlrun._needle(t)) < 2]
    assert short == []
    assert sqlrun._needle("419") == "419" and sqlrun._needle("TB") == "tb"


AGE_WORDS = {"age", "aged", "ages", "youth", "young", "teen", "teens", "teenager", "teenagers", "adolescent",
             "gen z", "genz", "gen alpha", "millennial", "millennials", "boomer", "boomers", "child", "children",
             "kid", "kids", "student", "students", "elderly", "senior", "seniors", "pensioner", "pensioners"}


def test_no_age_terms_in_either_list():
    for _, term, _ in sqlrun.sensitive_terms():
        assert term.casefold() not in AGE_WORDS
        assert not any(w in AGE_WORDS for w in term.casefold().split())


def item(item_id, label, key=None, aliases=None, kind="topic", valid_to=None):
    return {**cmap(item_id, kind), "label": label, "canonical_key": key if key is not None else label.casefold(),
            "aliases": aliases, "valid_to": valid_to}


def sensitive(con):
    return sorted((r["item_id"], r["reason"]) for r in duck.query(
        con, "SELECT s.item_id, s.reason FROM {core}.v_sensitive_items s"))


def test_sensitive_items_match_label_key_or_alias_under_the_g4b_rules(con):
    duck.load(con, "core.cultural_map", [
        item("e1", "#Elections2026", key="elections2026", kind="hashtag"),     # long term inside the key
        item("e2", "#VoteANC", key="voteanc", kind="hashtag"),                 # capitals split in the label
        item("e3", "Da Les new song", aliases=["da les"]),                     # DA matches only in capitals
        item("e4", "Sunday church outfits"),
        item("e5", "Testing week", aliases=["HIV testing"]),
        item("e6", "Amapiano"),
        item("e7", "Murder in the township and an ANC rally"),
        item("e8", "murder", valid_to=at(day(3))),                            # an old version of e8
        item("e8", "Dance challenge"),
        item("e9", "#fyp", key="fyp", kind="hashtag"),
        item("e10", "Zion Christian Church Easter pilgrimage", key="zion christian church easter pilgrimage"),
        item("e11", "#Pride", key="lgbtqpride", kind="hashtag"),
        item("e12", "Racism at school", key="racism at school"),
        item("e13", "Free testing drive", key="hiv_testing"),                 # a short term as a key word only
    ])
    assert sensitive(con) == sorted([
        ("e1", "political"), ("e2", "political"), ("e4", "religion"), ("e5", "health"),
        ("e7", "crime"), ("e7", "political"), ("e10", "religion"), ("e11", "sex_life"),
        ("e12", "race_ethnicity"), ("e13", "health"),
    ])


def test_sensitive_view_holds_every_term_as_an_array_literal():
    stmt = next(s for s in sqlrun.agent_statements() if sqlrun.object_name(s).endswith(".v_sensitive_items"))
    for reason, term, _ in sqlrun.sensitive_terms():
        assert f"'{term}'" in stmt or f"'{term.replace(chr(39), chr(92) + chr(39))}'" in stmt, (reason, term)


# Hashtag keys are lowercase compounds: every one of these must land in the sensitive set, by the reason given.
MUST_MATCH = {
    "hivawareness": "health", "hivpositive": "health", "hivtesting": "health", "aidsawareness": "health",
    "anxiety": "health", "adhd": "health", "weightloss": "health", "ozempic": "health",
    "gaypride": "sex_life", "pride": "sex_life", "trans": "sex_life", "nonbinary": "sex_life",
    "homophobia": "sex_life", "sextips": "sex_life",
    "endgbv": "crime", "gbvawareness": "crime", "rapeculture": "crime", "police": "crime", "shooting": "crime",
    "stabbing": "crime", "corruption": "crime", "gangster": "crime", "kidnapped": "crime", "hijacked": "crime",
    "yahooboys": "crime",
    "jesus": "religion", "god": "religion", "allah": "religion", "eidmubarak": "religion", "prayer": "religion",
    "gospel": "religion", "gospelmusic": "religion", "worship": "religion", "zcc": "religion",
    "shembe": "religion",
    "operationdudula": "race_ethnicity",
    "endsars": "political", "rutomustgo": "political", "rejectfinancebill2024": "political", "protest": "political",
    "bokoharam": "crime", "terrorism": "crime", "drugabuse": "crime", "nyaope": "crime", "saps": "crime",
    "yahooboy": "crime", "jailed": "crime", "stolen": "crime",
    "peterobi": "political", "obidient": "political", "feesmustfall": "political",
    "putsouthafricansfirst": "political",
    "blacklivesmatter": "race_ethnicity", "whiteprivilege": "race_ethnicity",
    "hijab": "religion", "sangoma": "religion", "traditionalhealer": "religion", "sundayservice": "religion",
    "amen": "religion", "praisebreak": "religion", "hymns": "religion",
    "depressed": "health", "therapy": "health", "comingout": "sex_life",
    "politics": "political", "abuse": "crime", "tb": "health",
    "zulu": "race_ethnicity", "yoruba": "race_ethnicity", "kikuyu": "race_ethnicity",
    "endhiv": "health", "eidaladha": "religion", "happyeidmubarak": "religion", "endrapeculture": "crime",
    "worldaidsday": "health", "prayers": "religion", "racism": "race_ethnicity",
}
MUST_MATCH_TOPICS = {"Jesus is lord": "religion", "Gospel music": "religion", "Feeling depressed today": "health",
                     "Therapy session vlog": "health", "Coming out stories": "sex_life",
                     "Pride and Prejudice": None}      # None: listed in never_sensitive, so it must not match
# Ordinary tags that must stay out. The last three guard the choice to keep political.yaml's names and
# acronyms on the G4b rules: Obi, Zuma and EFF sit at the edge of these keys. #ancestors, which guarded ANC,
# is now a religion term; PROBE_EXACT holds it to religion alone, so ANC still does not reach it.
MUST_NOT = ["amapiano", "afrobeats", "jollof", "braai", "springbok", "football", "dancechallenge", "lagos",
            "nairobi", "zumba", "effortless",
            "goddess", "godfather", "godzilla", "gangnamstyle", "racecar", "transport", "prideandprejudice",
            "hive", "hivemind", "unisex", "sussex", "essex", "middlesex", "raceday", "racehorse", "pridelands",
            "drugstore", "jailbreak", "scampi", "grandtheftauto", "eidolon", "cancerseason", "godmother", "godson",
            "goddessbraids", "racer", "racetrack", "penaltyshootout", "moneyheist", "killingit", "drugstoremakeup",
            "presidentscup", "godofwar", "pornstarmartini", "gabrieljesus", "templerun", "frace", "racecourse",
            "maasaimara", "gangstasparadise", "ministerofsound", "obiwankenobi", "bbnaijavote", "foodporn", "carporn",
            "fordescort", "kwazulunatal", "kwazulu"]    # the last two are ordinary phrases now, not never keys


def test_sensitive_set_catches_compound_hashtag_keys_and_leaves_ordinary_tags(con):
    rows = [item(f"h_{k}", f"#{k}", key=k, kind="hashtag") for k in list(MUST_MATCH) + MUST_NOT]
    rows += [item(f"t_{label}", label) for label in MUST_MATCH_TOPICS]
    duck.load(con, "core.cultural_map", rows)
    found = {}
    for item_id, reason in sensitive(con):
        found.setdefault(item_id, set()).add(reason)
    missed = {k: r for k, r in MUST_MATCH.items() if r not in found.get(f"h_{k}", set())}
    missed.update({k: r for k, r in MUST_MATCH_TOPICS.items() if r and r not in found.get(f"t_{k}", set())})
    assert missed == {}
    assert {k: found[f"h_{k}"] for k in MUST_NOT if f"h_{k}" in found} == {}
    assert "t_Pride and Prejudice" not in found


# As collect writes them: a raw hashtag or topic keyed by items.canonical_key, the raw text as the label. Each
# must land in the sensitive set by the reason given (the reviewer's probe of 29 September).
PROBE_MATCH = {
    "political": ["Politics", "#politics", "#SAPolitics", "Nigerian politics", "Kenyan politics",
                  "Government of National Unity", "#GNU", "#Mashatile", "#Steenhuisen", "#Kenyatta", "#Uhuru",
                  "#coup", "Democracy", "Minister of Finance budget speech"],
    "race_ethnicity": ["#Zulu", "#Xhosa", "#Yoruba", "#Igbo", "#Hausa", "#Kikuyu", "#Luo", "#Afrikaner",
                       "#Coloured", "#tribe", "#tribal", "#colourism", "#blackgirlmagic", "Black tax", "#apartheid"],
    "crime": ["#abuse", "Domestic violence", "#assault", "#violence", "#humantrafficking", "#trafficking",
              "#killed", "#killing", "#mobjustice", "#bandits", "#banditry", "#gunman"],
    "health": ["#TB", "TB treatment", "Mental illness", "#bipolar", "#PTSD", "#epilepsy", "#obesity", "#IVF",
               "#infertility", "#miscarriage", "#STI", "#chemo", "#dementia", "#sicklecell", "#eatingdisorder"],
    "religion": ["Faith", "#faith", "#spiritual", "Pentecostal revival", "#tithe", "#Hajj", "#Diwali"],
}
# The exact reasons each of these gets: short terms inside common words must not lend them a label.
PROBE_EXACT = {
    "#apartheid": {"race_ethnicity"}, "#eid": {"religion"}, "#eidmubarak": {"religion"}, "#rape": {"crime"},
    "#rapeculture": {"crime"}, "#faithful": {"religion"}, "#hivawareness": {"health"},
    "#aidsawareness": {"health"}, "#sextips": {"sex_life"},
    "#grapes": set(), "#grapevine": set(), "#drape": set(), "#draped": set(), "#scrape": set(), "#reid": set(),
    "#stickers": set(), "#zumba": set(), "#lookbook": set(), "#gnuplot": set(), "#tbt": set(), "#tbh": set(),
    "#braids": set(), "#boxbraids": set(), "#archive": set(), "#unisexsalon": set(), "#grace": set(),
    "#amazinggrace": set(), "#embrace": set(), "#hairspray": set(), "#spray": set(), "#ramen": set(),
    "#skilled": set(), "#upskilling": set(), "#coupon": set(), "#couples": set(), "#diatribe": set(),
    "#colouredpencils": set(), "#fluorescent": set(),
    # round 2: a longer term wins over a short one inside it, and short tokens stay out of ordinary tags
    "TB Joshua": {"religion"}, "#tbjoshua": {"religion"}, "#protestant": {"religion"}, "#cashintransit": {"crime"},
    "#ancestors": {"religion"}, "#policebrutality": {"political", "crime"},
    "#farmmurders": {"race_ethnicity", "crime"},
    "#foreignersmustgo": {"race_ethnicity", "political"}, "PrEP": {"health"}, "#PrEP": {"health"}, "#prep": set(),
    "Meal prep": set(), "#mealprep": set(), "#419": {"crime"}, "#popeyes": set(), "#jujutsukaisen": set(),
    "#khalifa": set(), "#hospitality": set(), "#pedicure": set(), "#boerewors": set(), "#condominium": set(),
    "#moneyheist": set(), "#multicoloured": set(), "#votes": set(), "#devotion": set(), "#persona": set(),
    "#bailey": set(), "#backstroke": set(), "#rabbit": set(), "#culture": set(), "#classic": set(),
    "#therapist": {"health"}, "#rapist": {"crime"}, "#luoculture": {"race_ethnicity"}, "#tiktok": set(),
    "#nonalcoholic": set(), "#stdtesting": {"health", "sex_life"}, "#blackpride": {"race_ethnicity", "sex_life"},
    "Gayton McKenzie": {"political"}, "#tribalpolitics": {"race_ethnicity", "political"},
    # round 3: an item keeps every reason its terms carry; only gives_way_to takes one away
    "#STI": {"health", "sex_life"}, "#STD": {"health", "sex_life"}, "#Syphilis": {"health", "sex_life"},
    "#Herpes": {"health", "sex_life"}, "#Biafra": {"political", "race_ethnicity"},
    "#IPOB": {"political", "race_ethnicity"}, "#dance1419": set(), "#4190": set(), "#kinkyhair": set(),
    "#analysis": set(), "#analytics": set(), "#slurp": set(),
    # round 4: tik and other keys-only terms never read a split label; gives_way_to counts occurrences
    "#TikTokMadeMeBuyIt": set(), "#TikTokMzansi": set(), "#TikTokNigeria": set(), "#TikTokKenya": set(),
    "#TikTokSA": set(), "#tiktokmademebuyit": set(), "TikTok ban in Kenya": set(), "#ChristianTikTok": {"religion"},
    "#Tik": {"crime"}, "#tik": {"crime"}, "#tiklolly": {"crime"}, "KwaZulu-Natal floods": set(),
    "#ProtestantChurchProtest": {"political", "religion"}, "#GaytonMcKenzieGayRights": {"political", "sex_life"},
    "#UKBlackPride": {"race_ethnicity", "sex_life"}, "#TBJoshuaTBTreatment": {"health", "religion"},
    "#TBThursday": set(), "#TribeVibes": set(), "#KillingEve": {"crime"}, "#JujuOnThatBeat": set(),
    "#AmenCorner": set(), "#HawksNation": set(), "#ColouredContacts": set(), "#IndianFood": set(),
    # round 5: the 15 words read split labels again; an ordinary phrase takes a term's reason occurrence by
    # occurrence, and each name is counted on its own
    "#TikAddictsTikTok": {"crime"}, "#StopTik": {"crime"}, "#ColouredPride": {"race_ethnicity"},
    "drug resistant TB": {"health", "crime"}, "#ColouredPencilsAndColouredCommunity": {"race_ethnicity"},
    "#KillingIt": set(), "#KilledIt": {"crime"}, "#MomTribe": set(), "#TribalPrint": set(), "#IndianOcean": set(),
    "#AtlantaHawks": set(), "#AmenBreak": {"religion"}, "#JujuMusic": set(), "#BailOutTheBand": {"crime"},
    "#ConvictConditioning": set(), "#TikTokShop": set(), "#KwaZuluNatal": set(), "#KwaZulu": set(),
}


# The reviewer's round 2 set (29 September), with the round 3, 4 and 5 misses added: plain sensitive labels
# by the reason they must carry, and under
# None the tags that must carry no reason at all. Christmas, Easter, Good Friday and Lent are left out: they are
# Albert's open question.
REVIEW_CASES = {
    "political": ["#SONA2026", "#sona", "State of the Nation Address", "#statecapture", "State capture", "#ActionSA",
        "Patriotic Alliance", "IFP", "#RiseMzansi", "#ZumaMustFall", "Phala Phala", "#landexpropriation",
        "#EndBadGovernance", "#endbadgovernance", "Buhari", "#Tinubu", "#tinubu", "#ObiDatti", "IPOB", "Nnamdi Kanu",
        "#fuelsubsidy", "fuel subsidy removal", "senate", "governor", "president", "#RejectFinanceBill2024",
        "#rejectfinancebill2024", "#RutoMustGo", "#rutomustgo", "#OccupyParliament", "Maandamano", "#maandamano",
        "Kalonzo", "housing levy", "#Azimio", "#impeachment", "#Gachagua", "#VoteANC", "#voteanc", "#EFF", "#eff",
        "#iec", "#inec", "#iebc", "#ANC", "#anc", "#DA", "#da", "#uda", "#odm", "#apc", "#pdp", "Julius Malema",
        "#elections2026", "#localelections", "#mkparty", "#protests", "#endsars", "#abductions", "#Biafra",
        "#policebrutality", "#tenderpreneur", "all progressives congress", "coalition talks", "#Wike",
        "#SONADebate", "#sonadebate", "#PostSONA", "#TribeBeforeNation"],
    "religion": ["#church", "RCCG", "Winners Chapel", "#Shepherdbushiri", "TB Joshua", "#tbjoshua", "SCOAN",
        "#happyeid", "#HappyEid", "#eidaladha", "#SallahMubarak", "#sallah", "Jummah", "#jumma", "#ramadankareem",
        "Holy Spirit", "#holyghost", "#anointing", "#deliverance", "crusade", "bishop", "apostle", "#pope",
        "#rosary", "#ancestors", "#amadlozi", "#witchcraft", "#juju", "babalawo", "#orisha", "#ifa", "#atheist",
        "#halal", "#keeppraying", "#KeepPraying", "#praying", "#blessed", "#godisgood", "#jesuslovesyou",
        "#biblestudy", "#worshipsong", "#gospelmusic", "#prayerwarrior", "#muslimah", "#islamic", "#hijabi",
        "#christianmusic", "#christiantiktok", "#sermon", "#preacher", "#legiomaria", "#akorino", "Nazareth Baptist",
        "#ZCC", "#zcc", "#zccmusic", "#shembe", "#mboro", "#prophetshepherdbushiri", "#prayers", "#protestant",
        "#Adeboye", "seventh day adventist", "#Anglican", "#Methodist", "#Shakahola", "#Hallelujah",
        "#Alhamdulillah", "#SayAmen", "#sayamen", "#AmenAndAmen", "#TypeAmen", "#typeamen", "#JujuOath"],
    "health": ["#HIV", "#hivprevention", "PrEP", "ARVs", "antiretroviral", "#TB", "#tb", "#endtb", "#tbfree",
        "#breastcancer", "#breastcancerawareness", "stroke", "hypertension", "#asthma", "#lupus", "#sicklecell",
        "#monkeypox", "#ebola", "Lassa fever", "#measles", "#polio", "#vaccine", "#vaccination", "#coronavirus",
        "#covid19", "#mentalhealth", "#mentalhealthawareness", "#schizophrenia", "#OCD", "#endometriosis", "#pcos",
        "#menopause", "#std", "#herpes", "#syphilis", "#hpv", "#hepatitis", "#kidneyfailure", "#dialysis",
        "#hospital", "#surgery", "#addiction", "#alcoholism", "#codeine", "#tramadol", "#suicideprevention",
        "#selfharm", "#grief", "#cancersurvivor", "#chemotherapy", "#worldaidsday", "#endaids", "#EndAIDS",
        "#disabled", "#wheelchair", "#autistic", "#downsyndrome", "#adhd", "#depression", "#anxietyrelief",
        "#stroke", "#diabetes", "#miscarriage", "#infertility", "#ivfjourney", "#pregnancyjourney", "#postpartum",
        "#stillbirth", "#mpox", "#cholera", "#malaria", "#dementia", "#alzheimers", "#epilepsy", "#bipolar",
        "#ptsd", "#rehab", "#sobriety", "#therapy", "#eatingdisorder", "#anorexia", "#bulimia", "#obesity",
        "#NHI", "national health insurance"],
    "sex_life": ["#gay", "#lesbian", "#lgbtqia", "LGBTQ+", "#queer", "#trans", "#transgender", "#pride",
        "#pridemonth", "#homosexual", "#homosexuality", "#bisexual", "#pansexual", "#asexual", "#dragqueen",
        "#dragrace", "same sex marriage", "#samesexmarriage", "gender identity", "#moffie", "#stabane", "#sexwork",
        "#sexworker", "#escort", "#hookup", "#onlyfans", "#nudes", "#sextape", "#leakedvideo", "#condom",
        "#contraception", "#birthcontrol", "#virginity", "#polyamory", "#sugardaddy", "#blesser", "#sugarmummy",
        "#nonbinary", "#comingout", "#queerafrica", "#gaynigeria", "#lgbtkenya", "#lesbiansoftiktok",
        "#gaytiktok", "#sexeducation", "#sexualhealth", "#porn", "#antihomosexualitybill", "#transrights",
        "#transwomen", "#BodyCount", "body count", "#Orgasm", "#Libido", "erectile dysfunction", "#Viagra",
        "#Gonorrhea", "morning after pill", "#FriendsWithBenefits", "#OneNightStand", "#Threesome", "#BDSM",
        "#Masturbation", "#STI", "#STD", "#Syphilis", "#Herpes", "#Kamasutra", "#StripClub", "#Mjolo",
        "#Infidelity", "#TransVisibility", "#TransDayOfVisibility", "#TransLivesMatter", "#translivesmatter"],
    "race_ethnicity": ["#zulu", "#xhosa", "#sotho", "#tswana", "#venda", "#tsonga", "#pedi", "#ndebele",
        "#afrikaner", "#boer", "#boerlivesmatter", "#farmmurders", "#whitegenocide", "#coloured", "#capecoloured",
        "#khoisan", "#yoruba", "#igbo", "#hausa", "#fulani", "#ijaw", "#kikuyu", "#luo", "#luhya", "#kalenjin",
        "#kamba", "#maasai", "#somali", "#kisii", "#tribalism", "#xenophobia", "#foreignersmustgo",
        "#putsouthafricansfirst", "#amakwerekwere", "#racism", "#bbbee", "#blackeconomicempowerment",
        "#colonialism", "#rhodesmustfall", "#blackexcellence", "#melanin", "#racist", "#operationdudula",
        "#zuluculture", "#xhosawedding", "#yorubawedding", "#igbowedding", "#kikuyuwedding", "#zuluwedding",
        "#indian", "#whiteprivilege", "#blacklivesmatter", "#blm", "colorism", "#Oduduwa", "#AntiBlackness",
        "#Arewa", "hate speech", "#Biafra", "#IPOB", "#SkinBleaching",
        "#TribalClashes", "#tribalclashes", "#tribalhate", "#ColouredCommunity", "#colouredcommunity",
        "#ColouredLivesMatter", "#ColouredPride", "#IndianCommunity", "#indiancommunity", "#IndianSouthAfrican",
        "#AntiIndian", "indians in durban"],
    "crime": ["#SenzoMeyiwaTrial", "senzo meyiwa trial", "#Mugging", "#StopTik", "#HawksRaid", "#hawksraid",
        "#HawksInvestigation", "#BailDenied", "#baildenied", "#BailApplication", "#NoBailForRapists", "#nobail",
        "#ConvictEscapes", "#EscapedConvict", "#escapedconvicts", "#ConvictsReleased", "#StopTheKilling",
        "#stopthekillings", "#MobKilling", "#KillingOfWomen", "#StopKillingUs", "#stopkillingus", "#WomenKilled",
        "#murder", "#killed", "#shotdead", "#gunshot",
        "#hijack", "#hijacked", "#carjacking",
        "#cashintransit", "#citheist", "#heist", "#robbery", "#armedrobbery", "#burglary", "#kidnap", "#abducted",
        "#missingperson", "#rape", "#sexualassault", "#molestation", "#childabuse", "#gbv", "#femicide",
        "#domesticviolence", "#AmINext", "#aminext", "#justicefor", "#arrested", "#sentenced", "#convicted",
        "#bail", "#police", "#saps", "#hawks", "#npa", "#efcc", "#dci", "#yahooyahoo", "#yahooboys", "#419",
        "#scam", "#fraud", "#ritualkilling", "#moneyritual", "#cultism", "#bandits", "#kidnappers",
        "#herdsmen", "#bokoharam", "#iswap", "#alshabaab", "#terrorism", "#extortion", "#zamazama",
        "#illegalmining", "#gangviolence", "#taxiviolence", "#mobjustice", "#necklacing", "#vigilante",
        "#lynching", "#corruption", "#bribery", "#moneylaundering", "#drugs", "#cocaine", "#tik", "#mandrax",
        "#smuggling", "#humantrafficking", "#rhinopoaching", "#poaching", "#stabbed", "#crime", "#truecrime",
        "#prison", "#inmate", "#endgbv", "#stoprape", "#StopRape", "#endrape", "#justiceforuyinene",
        "#killing", "#massshooting", "#shooting", "#gunviolence", "#stolen", "#pickpocket", "#jailed"],
    None: ["#transfer", "#transfernews", "#transferwindow", "#transit", "#transparent", "#translation",
           "#transformers", "#transformationtuesday", "#transport", "#tbt", "#grapes", "#braids", "#hearingaids",
           "#ramen", "#skilled", "#stickers", "#coupon", "#essex", "#middlesex", "#sussex", "#jailbreak",
           "#drugstore", "#raceday", "#racehorse", "#scampi", "#grandtheftauto", "#eidolon", "#cancerseason",
           "#pridelands", "#hivemind", "#archive", "#godzilla", "#amapiano", "#jollof", "#nairobi", "#zumba",
           "#lagos", "#football", "#afcon", "#sundaybest", "#classic", "#passion", "#assassinscreed", "#dragon",
           "#mass", "#killer", "#killerqueen", "#tribeca", "#antman", "#blackfriday", "#whitewedding", "#votes",
           "#devotion", "#divorce", "#riotgames", "#abcd", "#saturday", "#PenaltyShootout", "#PresidentsCup",
           "#DrugstoreMakeup", "#PornstarMartini", "#KillingIt", "#MoneyHeist", "#GodOfWar", "#dance1419",
           "#GabrielJesus", "#TempleRun", "#F1Race", "#Racecourse", "#ColouredContacts", "#IndianFood",
           "#MaasaiMara", "#GangstasParadise", "#MinisterOfSound", "#ObiWanKenobi", "#BBNaijaVote", "#Foodporn",
           "#FordEscort", "#CarPorn", "#TikTokSA", "#KwaZuluNatal", "tik tok trends", "find your tribe",
           "coloured contact lenses", "indian ocean beach", "atlanta hawks", "juju music",
           "killing it on stage", "#TrendingTribe", "#TribeCalledQuest", "#FitnessTribe", "#TribalTattoo",
           "#ColouredHair", "#IndianPremierLeague", "#KillingTheGame", "#KillingTime",
           "#KilledTheLook"],
}


@pytest.mark.parametrize("reason", list(REVIEW_CASES), ids=lambda r: r or "none")
def test_review_cases_carry_their_reason(con, reason):
    raws = REVIEW_CASES[reason]
    duck.load(con, "core.cultural_map", [probe_row(n, raw) for n, raw in enumerate(raws)])
    found = {}
    for item_id, got in sensitive(con):
        found.setdefault(item_id, set()).add(got)
    if reason is None:
        assert {raw: found[f"p{n}"] for n, raw in enumerate(raws) if f"p{n}" in found} == {}
    else:
        assert [raw for n, raw in enumerate(raws) if reason not in found.get(f"p{n}", set())] == []


def probe_row(n, raw):
    kind = "hashtag" if raw.startswith("#") else "topic"
    return {**cmap(f"p{n}", kind), "label": raw, "canonical_key": items.canonical_key(kind, raw)}


def test_sensitive_set_holds_every_probe_term_as_collect_keys_it(con):
    raws = [(reason, raw) for reason, group in PROBE_MATCH.items() for raw in group]
    duck.load(con, "core.cultural_map", [probe_row(n, raw) for n, (_, raw) in enumerate(raws)])
    found = set(sensitive(con))
    assert [(raw, reason) for n, (reason, raw) in enumerate(raws) if (f"p{n}", reason) not in found] == []


def test_short_terms_inside_common_words_give_no_wrong_reason(con):
    duck.load(con, "core.cultural_map", [probe_row(n, raw) for n, raw in enumerate(PROBE_EXACT)])
    found = {}
    for item_id, reason in sensitive(con):
        found.setdefault(item_id, set()).add(reason)
    got = {raw: found.get(f"p{n}", set()) for n, raw in enumerate(PROBE_EXACT)}
    assert {raw: r for raw, r in got.items() if r != PROBE_EXACT[raw]} == {}


# never keys whose false hit comes through the label as people write it, split where its capitals change
NEVER_LABELS = {"moneyheist": "#MoneyHeist", "frace": "#F1Race", "gabrieljesus": "#GabrielJesus",
                "templerun": "#TempleRun", "ministerofsound": "#MinisterOfSound", "obiwankenobi": "#ObiWanKenobi",
                "bbnaijavote": "#BBNaijaVote", "fordescort": "#FordEscort"}


def test_never_sensitive_is_checked_first_and_every_entry_would_otherwise_match(con):
    never = sqlrun.never_sensitive()
    assert set(never) >= {"goddess", "godfather", "godzilla", "gangnamstyle", "racecar", "prideandprejudice"}
    assert "transport" not in never          # trans is whole words only now, so transport needs no entry
    assert set(never) <= set(MUST_NOT)
    duck.load(con, "core.cultural_map", [item(f"h_{k}", NEVER_LABELS.get(k, f"#{k}"), key=k, kind="hashtag")
                                         for k in never])
    assert sensitive(con) == []
    # the same keys match once the never list is empty: each entry guards a real false hit
    stmt = next(s for s in sqlrun.agent_statements("core", "agent", never=[])
                if s.split()[4] == "core.v_sensitive_items")
    con.execute(duck.create_statement(stmt))
    assert {i for i, _ in sensitive(con)} == {f"h_{k}" for k in never}


def test_every_sensitive_row_carries_the_list_version(con):
    duck.load(con, "core.cultural_map", [item("x1", "#jesus", key="jesus", kind="hashtag"),
                                         item("x2", "Murder and ANC rally")])
    rows = duck.query(con, "SELECT s.item_id, s.reason, s.list_version FROM {core}.v_sensitive_items s")
    assert len(rows) == 3 and {r["list_version"] for r in rows} == {sqlrun.list_version()}
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", sqlrun.list_version())
    terms = sqlrun.sensitive_terms()
    assert sqlrun.list_version(terms[:-1]) != sqlrun.list_version(terms)
    assert sqlrun.list_version(terms, never=[]) != sqlrun.list_version(terms)


FLOOR = "A floor, not a complete list: it hides obvious cases; anything it misses is not safe to show next to a named person."


def test_the_view_and_the_yaml_say_the_list_is_a_floor():
    sql = " ".join(line.lstrip("- ").strip() for line in sqlrun.AGENT_VIEWS_SQL.read_text(encoding="utf-8").splitlines())
    yml = " ".join(line.lstrip("# ").strip() for line in sqlrun.SENSITIVE_YAML.read_text(encoding="utf-8").splitlines())
    assert FLOOR in sql and FLOOR in yml


# v_sensitive_items_complete: the keyword floor plus the understand job's model reasons (L4 Needs 19). The API
# switches creator item lists and named communities on as soon as this view exists, so it is built only when
# the detect job's SENSITIVE_COMPLETE_READY says so, and only once Albert has said whether religious-root holidays
# count as religion (NA-9).

COMPLETE = "core.v_sensitive_items_complete"


def test_the_complete_view_is_left_out_by_default_and_the_flag_and_answer_are_still_open():
    from .. import job

    assert job.SENSITIVE_COMPLETE_READY is False
    assert sqlrun.RELIGIOUS_HOLIDAYS_ARE_RELIGION is None
    assert COMPLETE not in [sqlrun.object_name(s) for s in sqlrun.agent_statements("core", "agent")]
    built = [sqlrun.object_name(s) for s in sqlrun.agent_statements("core", "agent", sensitive_complete=True,
                                                                    religious_holidays=False)]
    assert built == AGENT_OBJECTS + [COMPLETE]


def test_the_complete_view_is_refused_while_the_religious_holiday_question_is_open():
    with pytest.raises(ValueError, match="religious"):
        sqlrun.agent_statements("core", "agent", sensitive_complete=True)
    client = FakeClient()
    with pytest.raises(ValueError, match="religious"):
        sqlrun.apply_agent_views(client, core="core", agent="agent", sensitive_complete=True)
    assert client.sql == []          # refused before anything is applied


def test_the_detect_job_never_creates_the_complete_view_while_the_flag_is_off():
    from .. import job

    client = FakeClient()
    assert job.apply_agent_views_step(client, "core", "agent") == {"status": "ok"}
    assert [sqlrun.object_name(s) for s in client.sql] == AGENT_OBJECTS
    assert not any("v_sensitive_items_complete" in s for s in client.sql)


def test_the_detect_job_creates_the_complete_view_once_the_flag_is_on(monkeypatch):
    from .. import job

    monkeypatch.setattr(job, "SENSITIVE_COMPLETE_READY", True)
    monkeypatch.setattr(sqlrun, "RELIGIOUS_HOLIDAYS_ARE_RELIGION", True)
    client = FakeClient()
    assert job.apply_agent_views_step(client, "core", "agent") == {"status": "ok"}
    assert [sqlrun.object_name(s) for s in client.sql] == AGENT_OBJECTS + [COMPLETE]


def test_the_flag_on_with_the_question_open_fails_soft_and_creates_nothing(monkeypatch):
    from .. import job

    monkeypatch.setattr(job, "SENSITIVE_COMPLETE_READY", True)
    client = FakeClient()
    result = job.apply_agent_views_step(client, "core", "agent")
    assert result["status"] == "failed" and "religious" in result["error"]
    assert client.sql == []


def complete_view(con, religious_holidays):
    con.execute("ALTER TABLE core.post_enrichment ADD COLUMN IF NOT EXISTS tone VARCHAR")
    con.execute("ALTER TABLE core.post_enrichment ADD COLUMN IF NOT EXISTS sensitive VARCHAR[]")
    stmt = next(s for s in sqlrun.agent_statements("core", "agent", sensitive_complete=True,
                                                   religious_holidays=religious_holidays)
                if sqlrun.object_name(s) == COMPLETE)
    con.execute(duck.create_statement(stmt))


def read_posts(item_id, prefix, reasons_per_post):
    """post_items links and one enrichment row per post, each with its reasons (['none'] for none)."""
    links = [{"post_id": f"{prefix}{i}", "item_id": item_id, "via": "hashtag"} for i in range(len(reasons_per_post))]
    rows = [{"post_id": f"{prefix}{i}", "tone": "neutral", "sensitive": r} for i, r in enumerate(reasons_per_post)]
    return links, rows


def load_world(con):
    links, rows = [], []
    for item_id, prefix, reasons in [
        ("m_two_posts", "a", [["political"]] * 2 + [["none"]] * 8),          # 2 of 10: at least two posts
        ("m_share", "b", [["health"]] + [["none"]] * 3),                      # 1 of 4: 25%, over a fifth
        ("m_one_in_ten", "c", [["crime"]] + [["none"]] * 9),                  # 1 of 10: neither
        ("m_two_reasons", "d", [["political", "crime"], ["crime"]] + [["none"]] * 8),
        ("m_holiday", "e", [["religious_holiday"]] * 2 + [["none"]] * 3),
    ]:
        more_links, more_rows = read_posts(item_id, prefix, reasons)
        links += more_links
        rows += more_rows
    # The crime post of m_one_in_ten linked twice still counts once.
    links.append({"post_id": "c0", "item_id": "m_one_in_ten", "via": "sound"})
    # Fan-out: f0 has an embedding row, the enrichment row and a second copy from a race. One post, not three.
    fan_links, fan_rows = read_posts("m_fan_out", "f", [["religion"]] + [["none"]] * 9)
    links += fan_links
    rows += fan_rows + [{"post_id": "f0", "tone": None, "sensitive": None},
                        {"post_id": "f0", "tone": "neutral", "sensitive": ["religion"]}]
    # Only posts the model read count: 20 rows written before the field existed hold no reason, so 1 of 1 read.
    old_links, old_rows = read_posts("m_old_rows", "g", [["crime"]] + [[]] * 10 + [None] * 10)
    links += old_links
    rows += old_rows
    # The keyword floor: the model read every post as none, and the item stays in the set by its label.
    floor_links, floor_rows = read_posts("k_floor", "k", [["none"]] * 5)
    links += floor_links
    rows += floor_rows
    duck.load(con, "core.post_items", links)
    duck.load(con, "core.post_enrichment", rows)
    duck.load(con, "core.cultural_map", [item("k_floor", "Murder in the township"), item("m_two_posts", "Amapiano")])


def complete(con):
    rows = duck.query(con, "SELECT s.item_id, s.market, s.category, s.source FROM {core}.v_sensitive_items_complete s")
    return sorted((r["item_id"], r["market"], r["category"], r["source"]) for r in rows)


def test_complete_view_is_the_floor_plus_items_whose_read_posts_carry_a_reason(con):
    complete_view(con, religious_holidays=False)
    load_world(con)
    assert complete(con) == sorted([
        ("k_floor", None, "crime", "keyword"),
        ("m_two_posts", None, "political", "model"),
        ("m_share", None, "health", "model"),
        ("m_two_reasons", None, "crime", "model"),
        ("m_old_rows", None, "crime", "model"),
    ])


def test_religious_holidays_count_as_religion_only_when_albert_says_so(con):
    complete_view(con, religious_holidays=True)
    load_world(con)
    assert ("m_holiday", None, "religion", "model") in complete(con)
    assert not [r for r in complete(con) if r[2] == "religious_holiday"]


def test_the_model_never_takes_an_item_out_of_the_keyword_floor(con):
    complete_view(con, religious_holidays=False)
    load_world(con)
    floor = {r["item_id"] for r in duck.query(con, "SELECT s.item_id FROM {core}.v_sensitive_items s")}
    assert floor == {"k_floor"}
    assert floor <= {r[0] for r in complete(con)}


def test_the_complete_view_thresholds_are_stated_and_conservative():
    assert sqlrun.SENSITIVE_MODEL_MIN_POSTS == 2 and sqlrun.SENSITIVE_MODEL_SHARE == 0.2
    stmt = next(s for s in sqlrun.agent_statements("core", "agent", sensitive_complete=True, religious_holidays=False)
                if sqlrun.object_name(s) == COMPLETE)
    assert "core.v_sensitive_items" in stmt and "UNION" in stmt.upper()
    assert not re.search(r"\b(age|nationality)\b", stmt.lower())


# BigQuery dry runs on staging (F42_BQ=1): parse, resolve names and plan, nothing is created or billed.


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
@pytest.mark.parametrize("name", [n.split(".")[1] for n in AGENT_OBJECTS])
def test_bigquery_dry_run(name):
    from google.cloud import bigquery

    from .test_detect_bigquery import PROJECT, body_with_params, dry_run

    stmt = next(s for s in sqlrun.agent_statements() if sqlrun.object_name(s).endswith("." + name))
    client = bigquery.Client(project=PROJECT)
    job = dry_run(client, body_with_params(stmt), {"item_id": "x", "market": "ZA", "days": 28})
    print(name, job.total_bytes_processed)
