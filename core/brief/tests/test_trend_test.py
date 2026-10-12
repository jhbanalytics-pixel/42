"""The trend test in shadow (contract TREND-TEST-v1): a label, TREND, NEWS, MIXED or UNKNOWN, recorded for every candidate
and published card of a brief run with every input figure, read by nothing.

Every expectation below is a literal written in this file. None is read from the module under test, so a change to a
cut-off, a column or a rule fails here (the defect class: a value that proves something taken from what it proves
something about)."""

import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.agent.context import Refused
from core.agent.tools.sql_query import check_sql
from core.brief import job, pack_order, trend_test
from core.brief.tests import test_brief_golden_path as golden
from core.brief.tests.test_brief_job import EARLY, Client, FakeChain, FakeConfirm, pin_brief_model_cap
from core.detect.tests import duck
from core.detect.tests.fixtures import D

ROOT = Path(__file__).resolve().parents[3]

COLUMNS = (
    "run_date", "market", "item_id", "brief_run_id", "rule_version", "schema_version", "computed_at",
    "population_cutoff", "window_start", "window_end", "placement", "held_rule", "label", "reason", "news_axis",
    "trend_axis", "registry_status", "registry_keys", "outlet_hosts", "posts", "keyless_posts", "accounts",
    "located_posts", "feed_posts", "registry_posts", "outlet_posts", "outlet_accounts", "outlet_share", "share_posts",
    "rt_posts", "outlet_link_posts", "link_posts", "own_accounts", "own_share", "active_days", "growth_days",
    "peak_day_share", "first_post_date", "last_post_date", "top_sound_accounts", "challenge_accounts",
    "caption_accounts", "format_signal", "cause_dated_event", "cause_cue", "news_driven", "verified_accounts",
    "large_accounts")


# The version, the cut-offs and the table

def test_the_rule_version_cut_offs_and_table_are_the_v1_literals():
    assert trend_test.RULE_VERSION == "trend_test.v1"
    assert trend_test.SCHEMA_VERSION == 1
    assert trend_test.TABLE == "trend_test_private"
    assert trend_test.CUTOFFS == {"min_posts": 5, "outlet_share": 0.25, "own_share": 0.5, "peak_day_share": 0.5,
                                  "growth_days": 4, "format_accounts": 3}
    assert trend_test.WINDOW_DAYS == 7
    assert trend_test.EXCLUDED_LANES == ("confirm", "placebo", "agent_live", "legacy")
    assert trend_test.MAX_BYTES_BILLED == 2 * 1024 ** 3
    assert trend_test.COLUMNS == COLUMNS


# The label rule

BASE = {"posts": 20, "own_share": 0.8, "outlet_share": 0.0, "format_signal": True, "growth_days": 5,
        "peak_day_share": 0.2, "cause_dated_event": False}
THIN = {"posts": 20, "own_share": 0.3, "outlet_share": 0.1, "format_signal": False, "growth_days": 2,
        "peak_day_share": 0.7}


@pytest.mark.parametrize("fig, expected", [
    (BASE, ("TREND", "trend_axis", False, True)),
    ({**BASE, "own_share": 0.2, "outlet_share": 0.6, "format_signal": False, "growth_days": 1, "peak_day_share": 0.9,
      "cause_dated_event": True}, ("NEWS", "news_axis", True, False)),
    ({**BASE, "outlet_share": 0.3}, ("MIXED", "both_axes", True, True)),
    ({**BASE, "cause_dated_event": True, "peak_day_share": 0.6}, ("MIXED", "both_axes", True, True)),
    ({**BASE, "posts": 4}, ("UNKNOWN", "too_few_posts", None, None)),
    ({**BASE, "posts": None}, ("UNKNOWN", "population_unreadable", None, None)),
    ({**BASE, "own_share": None}, ("UNKNOWN", "no_accounts", None, None)),
    ({**THIN, "cause_dated_event": None}, ("UNKNOWN", "cause_missing", None, False)),
    ({**THIN, "cause_dated_event": False}, ("UNKNOWN", "neither_axis", False, False)),
    ({**THIN, "cause_dated_event": True}, ("NEWS", "news_axis", True, False)),
    ({**BASE, "cause_dated_event": None}, ("TREND", "trend_axis", False, True)),
    ({**BASE, "own_share": 0.5, "format_signal": False, "growth_days": 4}, ("TREND", "trend_axis", False, True)),
    ({**BASE, "outlet_share": 0.25}, ("MIXED", "both_axes", True, True)),
    ({**BASE, "own_share": 0.9, "format_signal": False, "growth_days": 3}, ("UNKNOWN", "neither_axis", False, False)),
    ({**BASE, "posts": 5, "own_share": 0.49}, ("UNKNOWN", "neither_axis", False, False)),
], ids=["trend", "news", "mixed_by_outlets", "mixed_by_dated_spike", "too_few_posts", "population_unreadable",
        "no_accounts", "cause_missing_decides", "neither", "dated_spike_is_news", "cause_missing_not_deciding",
        "trend_at_both_cut_offs", "outlets_at_the_cut_off", "own_words_without_format_or_growth", "own_share_below"])
def test_the_label_follows_the_v1_table(fig, expected):
    assert trend_test.classify(fig) == expected


# The cause in the explanation

@pytest.mark.parametrize("text, expected", [
    ("South African creators posted on TikTok and Twitter regarding the football match between Orlando Pirates and "
     "Mamelodi Sundowns at Moses Mabhida Stadium in Durban, likely because the MTN8 Cup Final took place on Saturday "
     "10 Oct.", (True, "saturday|final")),
    ("Creators in South Africa are posting their own kasi step videos across the week, likely because the dance "
     "dropped on Monday, as two of them say.", (True, "monday|dropped")),
    ("Creators are posting their own versions of a kitchen dance, likely because it keeps spreading between friends.",
     (False, None)),
    ("Fans reacted to the match on Sunday.", (True, "sunday|match")),
    ("Creators reacted, likely because of the memorial.", (False, None)),
    ("Creators marked the day around 2026-10-01, likely because of Independence Day.", (True, "2026-10-01|independence")),
    ("Creators posted about a football fixture on 11 Oct, likely because the home side defeated the visitors.",
     (True, "11 oct|defeated")),
    ("Creators shared reactions, likely because of its release announced on 2026-10-01.", (True, "2026-10-01|release")),
    ("Creators discussed the Premier League, likely because fans kept talking.", (False, None)),
    ("", (None, None)),
    (None, (None, None)),
], ids=["dated_final", "dated_release", "no_date_no_event", "no_because_clause", "event_without_date", "iso_date_holiday",
        "date_before_the_clause_inflected_event", "iso_date_release", "league_name_is_not_an_event", "empty", "absent"])
def test_the_cause_is_a_dated_single_event_only_with_a_date_and_an_event_word(text, expected):
    assert trend_test.cause(text) == expected


# The figures

def post(pid, platform, creator, handle, days_before, text, *, hashtags=(), sound=None, url=None, geo=None,
         verified=None, tier=None):
    return {"item_id": "i1", "post_id": pid, "platform": platform, "creator_id": creator, "handle": handle,
            "verified": verified, "tier": tier, "post_date": D - timedelta(days=days_before), "text": text,
            "hashtags": list(hashtags), "sound_id": sound, "url": url, "geo_market": geo}


POPULATION = [
    post("r1", "tiktok", "c1", "@ama", 3, "my own take on the step #KasiStepChallenge", hashtags=["kasistepchallenge"],
         sound="s1", geo="ZA", verified=False, tier="micro"),
    post("r2", "tiktok", "c2", "@bo", 2, "trying the step at home", hashtags=["kasistepchallenge"], sound="s1"),
    post("r3", "tiktok", "c3", "@cee", 2, "trying the step at home with gran", hashtags=["kasistepchallenge"],
         sound="s1"),
    post("r4", "twitter", "c4", "SundayTimesZA", 1, "Kasi step takes over https://www.timeslive.co.za/x",
         verified=True, tier="mega"),
    post("r5", "news", None, None, 1, "Kasi step story", url="https://www.timeslive.co.za/news/a"),
    post("r6", "twitter", "c5", "@dee", 1, "RT @SundayTimesZA: Kasi step takes over", tier="micro"),
    post("r7", "twitter", "c6", "@eve", 1, "look at this https://timeslive.co.za/news/a"),
    post("r8", "facebook", None, None, 1, "a group post"),
]


def test_the_figures_count_outlets_shares_own_versions_days_and_formats():
    got = trend_test.figures(POPULATION, "ZA", ["x:sundaytimesza"], ["https://www.timeslive.co.za/news/a"])
    assert got == {
        "outlet_hosts": 1, "posts": 8, "keyless_posts": 1, "accounts": 7, "located_posts": 1, "feed_posts": 1,
        "registry_posts": 1, "outlet_posts": 2, "outlet_accounts": 2, "outlet_share": 0.25, "share_posts": 2,
        "rt_posts": 1, "outlet_link_posts": 1, "link_posts": 1, "own_accounts": 3, "own_share": 0.428571,
        "active_days": 3, "growth_days": 3, "peak_day_share": 0.4, "first_post_date": D - timedelta(days=3),
        "last_post_date": D - timedelta(days=1), "top_sound_accounts": 3, "challenge_accounts": 3,
        "caption_accounts": 2, "format_signal": True, "verified_accounts": 1, "large_accounts": 1}


def test_an_empty_population_is_a_valid_zero_and_a_missing_one_is_null():
    empty = trend_test.figures([], "ZA", [], [])
    assert (empty["posts"], empty["accounts"], empty["own_share"], empty["outlet_share"], empty["peak_day_share"]) == (
        0, 0, None, None, None)
    assert trend_test.classify({**empty, "cause_dated_event": None})[:2] == ("UNKNOWN", "too_few_posts")
    missing = trend_test.figures(None, "ZA", [], [])
    assert set(missing.values()) == {None}
    assert trend_test.classify({**missing, "cause_dated_event": True})[:2] == ("UNKNOWN", "population_unreadable")


# The registry is a draft: read for the shadow figure only, never as the pack's authority

def test_the_draft_registry_is_read_for_the_shadow_and_the_pack_authority_stays_empty():
    reg = trend_test.registry()
    assert reg["status"] == "draft"
    assert {"x:sundaytimesza", "x:iol", "x:soccer_laduma", "x:citizentvkenya", "x:channelstv"} <= set(reg["keys"])
    assert "instagram:tyla" not in reg["keys"]  # a culture desk page is not an outlet
    assert len(reg["keys"]) == 15 and reg["keys"] == sorted(set(reg["keys"]))
    assert pack_order.outlet_keys() == []


# The candidates of a stored brief

PAYLOAD = {
    "cards": [{"item_id": "a", "explanation": "x, likely because the final was played on Saturday.", "news_driven": True,
               "also": [{"item_id": "m", "title": "merged"}]}],
    "more": [{"item_id": "b", "explanation": None, "news_driven": False, "also": []}],
    "held_back": {"count": 2, "items": [{"item_id": "c", "rule": "G10"}, {"item_id": "a", "rule": "G1"}]},
}


def test_every_candidate_of_a_stored_brief_is_listed_once_with_its_placement():
    assert trend_test.candidates(PAYLOAD) == [
        {"item_id": "a", "placement": "card", "held_rule": None,
         "explanation": "x, likely because the final was played on Saturday.", "news_driven": True},
        {"item_id": "b", "placement": "more", "held_rule": None, "explanation": None, "news_driven": False},
        {"item_id": "c", "placement": "held", "held_rule": "G10", "explanation": None, "news_driven": None},
        {"item_id": "m", "placement": "merged", "held_rule": None, "explanation": None, "news_driven": None},
    ]
    assert trend_test.candidates({"cards": [], "more": [], "held_back": {"count": 0, "items": []}}) == []


# End to end on the golden path's honest local trend

class LoadingClient(Client):
    """The brief tests' client, which also lands the briefs rows in DuckDB so the stored brief can be read back."""

    def insert_rows_json(self, table, rows):
        if table == "agent.briefs":
            duck.load(self.con, "agent.briefs", rows)
        return super().insert_rows_json(table, rows)


class LoadingChain(FakeChain):
    """FakeChain that also writes the finished run's runs row, as lane L1's chain does, so v_briefs_current shows it."""

    def __init__(self, con, raises=None):
        super().__init__(raises)
        self.con = con

    def finish(self, run, status, counts, error=None):
        duck.load(self.con, "agent.runs", [{"run_id": run.run_id, "stage": run.stage, "run_date": run.run_date,
                                            "status": status, "started_at": EARLY, "finished_at": EARLY}])
        return super().finish(run, status, counts, error)


def honest_world(monkeypatch):
    from core.brief import market_scope as brief_market_scope

    monkeypatch.setattr(job, "read_market_scope", brief_market_scope.read_market_scope, raising=False)
    pin_brief_model_cap(monkeypatch, EARLY)
    con = golden.world(located=True)
    con.execute("ALTER TABLE core.creators ADD COLUMN verified BOOLEAN")  # core.sql has it; the DuckDB stand-in does not
    return con


def run_main(con, monkeypatch, chain=None):
    client = LoadingClient(con)
    chain = chain or LoadingChain(con)
    code = job.main(client=client, chain=chain, model=golden.HonestModel(True), make_sc=lambda run_id: object(),
                    clock=lambda: EARLY, build_ctx=golden.gatectx.build_ctx, confirm=FakeConfirm(),
                    core="core", agent="agent")
    return code, client, chain


NOW = datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc)


def test_the_honest_local_trend_is_recorded_as_trend_with_every_input(monkeypatch):
    con = honest_world(monkeypatch)
    client = LoadingClient(con)
    job.run(client, D, chain=LoadingChain(con), model=golden.HonestModel(True), make_sc=lambda run_id: object(),
            clock=lambda: EARLY, build_ctx=golden.gatectx.build_ctx, confirm=FakeConfirm(), core="core", agent="agent")
    [row] = trend_test.build_rows(client, D, core="core", agent="agent", now=NOW)
    assert tuple(row) == COLUMNS
    assert row == {
        "run_date": "2026-09-20", "market": "ZA", "item_id": "za_kasi_step", "brief_run_id": "brief-20260920-fake",
        "rule_version": "trend_test.v1", "schema_version": 1, "computed_at": "2026-09-20T04:00:00+00:00",
        "population_cutoff": "2026-09-20T03:30:00+00:00", "window_start": "2026-09-14", "window_end": "2026-09-20",
        "placement": "card", "held_rule": None, "label": "TREND", "reason": "trend_axis", "news_axis": False,
        "trend_axis": True, "registry_status": "draft", "registry_keys": 15, "outlet_hosts": 0,
        "posts": 6, "keyless_posts": 0, "accounts": 4, "located_posts": 6, "feed_posts": 0, "registry_posts": 0,
        "outlet_posts": 0, "outlet_accounts": 0, "outlet_share": 0.0, "share_posts": 0, "rt_posts": 0,
        "outlet_link_posts": 0, "link_posts": 0, "own_accounts": 4, "own_share": 1.0, "active_days": 6,
        "growth_days": 4, "peak_day_share": 0.25, "first_post_date": "2026-09-14", "last_post_date": "2026-09-19",
        "top_sound_accounts": 0, "challenge_accounts": 4, "caption_accounts": 1, "format_signal": True,
        "cause_dated_event": True, "cause_cue": "monday|dropped", "news_driven": False, "verified_accounts": 0,
        "large_accounts": 0}


def test_main_records_the_shadow_rows_after_the_brief_and_the_brief_is_byte_identical_without_them(monkeypatch):
    code, client, _ = run_main(honest_world(monkeypatch), monkeypatch)
    assert code == 0
    [row] = client.inserted["agent.trend_test_private"]
    assert (row["market"], row["item_id"], row["label"], row["placement"]) == ("ZA", "za_kasi_step", "TREND", "card")
    recorded = client.inserted["agent.briefs"]

    def broken(*args, **kwargs):
        raise RuntimeError("trend test table not found")

    monkeypatch.setattr(trend_test, "build_rows", broken)
    code, client, chain = run_main(honest_world(monkeypatch), monkeypatch)
    assert code == 0
    assert "agent.trend_test_private" not in client.inserted
    assert client.inserted["agent.briefs"] == recorded
    assert [f["status"] for f in chain.finished] == ["ok"]


def test_main_does_not_record_when_the_brief_did_not_run(monkeypatch):
    calls = []
    monkeypatch.setattr(trend_test, "record_quietly", lambda *a, **k: calls.append(a))
    con = honest_world(monkeypatch)
    code, client, _ = run_main(con, monkeypatch, chain=LoadingChain(con, raises="done"))
    assert code == 0 and calls == [] and client.inserted == {}


def test_every_shadow_query_carries_the_byte_ceiling(monkeypatch):
    con = honest_world(monkeypatch)
    client = LoadingClient(con)
    job.run(client, D, chain=LoadingChain(con), model=golden.HonestModel(True), make_sc=lambda run_id: object(),
            clock=lambda: EARLY, build_ctx=golden.gatectx.build_ctx, confirm=FakeConfirm(), core="core", agent="agent")
    configs = []
    real = client.query

    def query(sql, job_config=None):
        configs.append(job_config)
        return real(sql, job_config)

    monkeypatch.setattr(client, "query", query)
    trend_test.build_rows(client, D, core="core", agent="agent", now=NOW)
    assert len(configs) == 3
    assert [c.maximum_bytes_billed for c in configs] == [2 * 1024 ** 3] * 3


def test_a_market_whose_population_cannot_be_read_records_unknown_rows(monkeypatch):
    con = honest_world(monkeypatch)
    client = LoadingClient(con)
    job.run(client, D, chain=LoadingChain(con), model=golden.HonestModel(True), make_sc=lambda run_id: object(),
            clock=lambda: EARLY, build_ctx=golden.gatectx.build_ctx, confirm=FakeConfirm(), core="core", agent="agent")
    real = client.query

    def query(sql, job_config=None):
        if "tvf_post_items" in sql:
            raise RuntimeError("Not found: Table function tvf_post_items")
        return real(sql, job_config)

    monkeypatch.setattr(client, "query", query)
    [row] = trend_test.build_rows(client, D, core="core", agent="agent", now=NOW)
    assert (row["label"], row["reason"], row["posts"], row["own_share"]) == ("UNKNOWN", "population_unreadable", None,
                                                                             None)
    assert row["cause_dated_event"] is True and row["placement"] == "card"


# Nobody reads it

def test_the_ask_sql_tool_refuses_the_shadow_table():
    for sql in ("SELECT * FROM `ogilvy-trends-v2.intelligence_42_agent.trend_test_private`",
                "SELECT label FROM intelligence_42_agent.trend_test_private"):
        with pytest.raises(Refused):
            check_sql(sql)


def test_only_the_recorder_and_the_brief_entry_point_name_the_trend_test():
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    files = [p for p in tracked.decode("utf-8").split("\0") if p]
    naming = []
    for path in files:
        if not path.endswith((".py", ".sql", ".js", ".jsx", ".ts", ".tsx", ".yaml", ".yml", ".json", ".ps1")):
            continue
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "trend_test" in text:
            naming.append(path)
    assert sorted(naming) == ["core/brief/job.py", "core/brief/sql/trend_test.sql", "core/brief/tests/test_trend_test.py",
                              "core/brief/trend_test.py", "core/schema/tests/test_trend_test_schema.py",
                              "core/schema/trend_test.sql"]
    text = (ROOT / "core/brief/job.py").read_text(encoding="utf-8")
    assert text.count("trend_test") == 2  # the import and the one call in main, after the run has finished
    assert "    trend_test.record_quietly(client, d, " in text
