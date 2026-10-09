"""Detection scorecard (BUILD.md 2.7, ENGINE.md section 6): known answers on DuckDB fixtures.

One fixture week, Monday 5 to Sunday 11 October 2026. ZA carries data for every metric; NG carries only what
proves a metric reads "not yet measured" with its reason instead of a fabricated 0.
"""

import json
import os
import re
from datetime import date, timedelta

import pytest
import yaml
import yaml

from core.detect import scorecard, sqlrun

from . import duck
from .fixtures import counter, item_daily, rid, run

WEEK = date(2026, 10, 5)
ISO_WEEK = "2026-W41"
RUN_ID = "learn-20261005-test"
FIGURES = ("time_to_detect", "lead_time", "precision", "recall", "breadth_platforms",
           "expansion_cluster_share", "expansion_platform_share", "expansion_language_share",
           "cost_per_confirmed")

EXTRA_TABLES = """
CREATE TABLE agent.feedback (who VARCHAR, what VARCHAR, reason VARCHAR, "at" TIMESTAMPTZ);
CREATE TABLE core.credit_ledger (
  trend_date DATE NOT NULL, logged_at TIMESTAMPTZ, run_id VARCHAR, job VARCHAR, lane VARCHAR, agent VARCHAR,
  market VARCHAR, platform VARCHAR, route VARCHAR, endpoint VARCHAR, params_hash VARCHAR, item_id VARCHAR,
  calls BIGINT, credits_quoted DOUBLE, credits_charged DOUBLE, cache_hit BOOLEAN, posts_new BIGINT,
  balance_after DOUBLE);
ALTER TABLE agent.briefs ADD COLUMN IF NOT EXISTS payload JSON;
"""

REFERENCE = """
entries:
  - id: za-mopepe
    market: za
    title: "Mopepe dance challenge"
    event_date: 2026-10-07
    match_terms: [mopepe]
    added: 2026-10-01
    source: "fixture"
  - id: za-toy-story
    market: za
    title: "Toy Story casting"
    event_date: 2026-10-10
    match_terms: [toy story]
    added: 2026-10-01
    source: "fixture"
  - id: za-barbarians
    market: za
    title: "Springboks against the Barbarians"
    event_date: 2026-10-09
    match_terms: [barbarians]
    added: 2026-10-01
    source: "fixture"
  - id: za-mopepe-earlier
    market: za
    title: "Mopepe, an earlier week"
    event_date: 2026-09-20
    match_terms: [mopepe]
    added: 2026-09-01
    source: "fixture"
  - id: ng-mobo
    market: ng
    title: "MOBO nominations"
    event_date: 2026-10-07
    match_terms: [mopepe]
    added: 2026-10-01
    source: "fixture"
"""


def d(month, dd):
    return date(2026, month, dd)


# Rows as state.sql writes them for a Seasonal or Recurring item that also qualifies as Rising (tested, floors,
# significant with ratio 2 on 2 of the last 3 days) or as Emerging (tested, floors, new), and for one that only
# qualifies as a tested Spike (floors, one significant day, not new) or as New to 42 (untested, 3 creators).
# The series tests behind each row are loaded with sig(); the stored columns alone never make a row count.
RISING = dict(untested=False, creators3=6, posts3=12, top_creator_share3=.25, sig_days3=2, novelty="ongoing")
ONE_DAY = dict(RISING, sig_days3=1)     # tested, floors, ratio 2 today only: Rising needs a second platform or market
EMERGING = dict(untested=False, creators3=5, posts3=8, top_creator_share3=.25, sig_days3=1, novelty="new")
SPIKE = dict(untested=False, creators3=5, posts3=8, top_creator_share3=.25, sig_days3=1, novelty="ongoing")
FRESH = dict(untested=True, creators3=3, posts3=3, top_creator_share3=1 / 3, sig_days3=0, novelty="new")
WARM_UP = dict(FRESH, creators3=5, posts3=8, top_creator_share3=.25)    # floors met, but untested


def state(item, when, st, market="ZA", found=None, run_id=None, eligible=True, state_raw=None, **extra):
    return {"metric_date": when, "market": market, "item_id": item, "state": st, "state_raw": state_raw or st,
            "found_platforms": found, "eligible": eligible, "run_id": run_id or f"detect-{when:%Y%m%d}", **extra}


def sig(item, when, ratio, market="ZA", platform="tiktok", significant=True, test="nb", obs_prior=10,
        run_id=None):
    """One series_test row: the item's series on a platform in a market, as the stats step wrote it."""
    return {"metric_date": when, "series_id": f"{item}-{market}-{platform}", "item_id": item, "market": market,
            "platform": platform, "series": f"feed_{platform}", "protocol": "p1", "lane_class": "unbiased_rank",
            "y": 5.0, "obs_prior": obs_prior, "peak28": 5.0, "test": test, "ratio": ratio,
            "q": .01 if significant else .5, "significant": significant, "run_id": run_id or rid("stats", when)}


def cmap(item, label, aliases=(), parent=None):
    return {"item_id": item, "kind": "topic", "canonical_key": label.lower(), "label": label,
            "aliases": list(aliases), "parent_item_id": parent, "status": "active", "valid_to": None}


def review(card, verdict, *, market="ZA", stratum="top3", week=ISO_WEEK, kind="card", who="rev1"):
    what = {"kind": kind, "id": card, "market": market, "stratum": stratum, "week": week, "verdict": verdict,
            "useful": "yes", "double": False, "non_english": False, "language": "", "source": "42_review_v1"}
    return {"who": who, "what": json.dumps(what, sort_keys=True), "reason": ""}


def ledger(when, credits, *, job="collect", lane="expansion", platform="tiktok", item=None, market="ZA"):
    return {"trend_date": when, "job": job, "lane": lane, "market": market, "platform": platform,
            "item_id": item, "calls": 1, "credits_charged": credits, "cache_hit": False}


def brief(when, cards, more=(), banners=(), market="ZA", run_id=None):
    payload = {"market": market, "cards": [{"item_id": i} for i in cards], "more": [{"item_id": i} for i in more],
               "banners": [{"kind": k, "text": k} for k in banners]}
    return {"brief_date": when, "market": market, "run_id": run_id or f"brief-{when:%Y%m%d}",
            "payload": json.dumps(payload)}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    con = duck.connect()
    con.execute(EXTRA_TABLES)
    days = [d(7, 1) + timedelta(days=i) for i in range(110)]
    runs = [run(stage, x) for x in days for stage in ("collect", "aggregate", "stats", "detect", "brief")]
    runs.append(run("detect", d(10, 9), run_id="detect-bad", status="failed"))
    runs.append(run("brief", d(10, 9), run_id="brief-bad", status="failed"))
    duck.load(con, "agent.runs", runs)

    # Time to detect. A: seen 28 Sep, emerging 30 Sep, mainstream 6 Oct: 2 days.
    # B: panel sighting 25 Sep, rising 30 Sep, mainstream 7 Oct: 5 days; its earlier watchlist sighting,
    # vendor-history counter day and GLOBAL counter read are not unbiased sightings in ZA.
    # C reached Mainstream before the week; D never was Emerging or Rising; E: counter read 1 Oct, emerging
    # 3 Oct, mainstream 9 Oct: 2 days. F is Mainstream only in a failed detect run.
    duck.load(con, "core.item_daily", [
        item_daily("A", d(9, 28), 4, lane_class="unbiased_rank", platform="tiktok", series=None, protocol=None),
        item_daily("B", d(9, 25), 3),
        item_daily("B", d(9, 15), 3, lane_class="watchlist", series=None, protocol=None),
        item_daily("C", d(9, 1), 5, lane_class="unbiased_rank", platform="tiktok", series=None, protocol=None),
        item_daily("D", d(10, 1), 5, lane_class="unbiased_rank", platform="tiktok", series=None, protocol=None),
    ])
    duck.load(con, "core.item_counter_daily", [
        counter("B", "curve_tiktok_hashtag", d(9, 20), 40, lane_class="unbiased_counter", unit="delta",
                source="vendor_history", read_day=d(9, 26)),
        counter("B", "counter_tiktok_hashtag", d(9, 18), 40, market="GLOBAL", lane_class="unbiased_counter",
                unit="delta"),
        counter("E", "counter_tiktok_hashtag", d(10, 1), 12, lane_class="unbiased_counter", unit="delta"),
        counter("E", "x_trends", d(9, 1), 1, platform="x"),
    ])
    states = [
        state("A", d(9, 30), "emerging"), state("A", d(10, 1), "rising"), state("A", d(10, 6), "mainstream", found=3),
        state("B", d(9, 30), "rising"), state("B", d(10, 7), "mainstream"),
        state("C", d(9, 29), "mainstream"), state("C", d(10, 6), "mainstream"),
        state("D", d(10, 8), "mainstream"),
        state("E", d(10, 3), "emerging"), state("E", d(10, 9), "mainstream", found=2),
        state("F", d(10, 9), "mainstream", found=4, run_id="detect-bad"),
        # Lead time: G matches "mopepe" and was first flagged 2 Oct (its 1 Aug flag is outside the 28 days);
        # H matches "toy story" by alias and was flagged 11 Oct, a day after the list had it.
        state("G", d(8, 1), "emerging"), state("G", d(10, 2), "emerging"), state("G", d(10, 4), "rising"),
        state("G", d(10, 6), "seasonal", found=1, **EMERGING),
        state("H", d(10, 11), "rising"),
        state("A", d(10, 7), "mainstream", market="NG", found=5),
    ]
    duck.load(con, "core.item_state", states)
    duck.load(con, "core.series_test", [sig("G", d(10, 6), 1.5)])
    duck.load(con, "core.cultural_map", [
        cmap("A", "Amapiano weekend"), cmap("G", "Mopepe dance"), cmap("G2", "Mopepe remix", parent="G"),
        cmap("H", "Tyla casting news", aliases=["Toy Story 5"]), cmap("Z", "Rugby tour"),
    ])

    # Precision: ZA top-3 cards c1 real, c2 split between two reviewers (not real), c3 not real, c4 real: 2 of 4.
    duck.load(con, "agent.feedback", [
        review("c1", "real"), review("c2", "real"), review("c2", "not_real", who="rev2"),
        review("c3", "not_real"), review("c4", "real"), review("c4", "real"),
        review("c5", "not_real", stratum="random"), review("c6", "not_real", week="2026-W40"),
        review("x1", "supported", kind="claim"),
        {"who": "app", "what": "real", "reason": "tap"},
        {"who": "app", "what": json.dumps({"kind": "card", "id": "c9", "market": "ZA", "stratum": "top3",
                                           "week": ISO_WEEK, "verdict": "not_real"}), "reason": "tap"},
    ])

    # Recall: 3 ZA moments in the week; only m1's item had a state on the day.
    duck.load(con, "core.calendar", [
        {"moment_date": d(10, 6), "market": "ZA", "name": "m1", "kind": "holiday", "item_ids": ["G"]},
        {"moment_date": d(10, 8), "market": "ZA", "name": "m2", "kind": "fixture", "item_ids": ["H"]},
        {"moment_date": d(10, 9), "market": "ZA", "name": "m3", "kind": "festival", "item_ids": []},
        {"moment_date": d(9, 30), "market": "ZA", "name": "m0", "kind": "holiday", "item_ids": ["A"]},
        {"moment_date": d(10, 7), "market": "NG", "name": "n1", "kind": "holiday", "item_ids": []},
    ])

    # Breadth: 20 placebo items make ZA's platform counts readable; NG has 3.
    duck.load(con, "core.seed_queue",
              [{"seed_date": d(10, 1), "market": "ZA", "item_id": f"p{i}", "lane": "placebo"} for i in range(20)]
              + [{"seed_date": d(10, 1), "market": "NG", "item_id": f"p{i}", "lane": "placebo"} for i in range(3)])

    # Expansion: tiktok 40 (G 30, G2 10: one family), instagram H 40, reddit 20 with no item.
    # Cost: engine credits 100 expansion + 20 sweep + 30 confirm = 150; Ask and next week's credits are out.
    duck.load(con, "core.credit_ledger", [
        ledger(d(10, 5), 30, item="G"), ledger(d(10, 6), 10, item="G2"),
        ledger(d(10, 7), 40, platform="instagram", item="H"), ledger(d(10, 8), 20, platform="reddit"),
        ledger(d(10, 8), 20, lane="sweep", platform="youtube", item="A"),
        ledger(d(10, 9), 30, job="confirm", lane="confirm", item="A"),
        ledger(d(10, 9), 50, job="ask", lane="agent_live", item="A"),
        ledger(d(10, 12), 99, item="G"),
        ledger(d(10, 6), 70, market="NG", item="A"),
    ])
    # Confirmed trends: A, G, E on Today; H only on a day confirmation did not run; X only in a failed run.
    duck.load(con, "agent.briefs", [
        brief(d(10, 6), ["A", "G"], more=["E"]), brief(d(10, 7), ["A"]),
        brief(d(10, 8), ["H"], banners=["thin_coverage"]),
        brief(d(10, 9), ["X"], run_id="brief-bad"),
    ])

    ref = tmp_path_factory.mktemp("ref") / "ground_truth.yaml"
    ref.write_text(REFERENCE, encoding="utf-8")
    client = duck.Client(con)
    with pytest.MonkeyPatch.context() as patch:
        # A module fixture is built before the per-test authority fixture runs, and the statements differ by authority
        # (sqlrun.for_authority), so it is built under the one the suite run asks for.
        if os.environ.get("F42_TEST_LOCALITY_AUTHORITY"):
            from core.conftest import set_locality_authority

            set_locality_authority(patch, os.environ["F42_TEST_LOCALITY_AUTHORITY"])
        rows = scorecard.run_scorecard(client, WEEK, run_id=RUN_ID, reference=ref, core="core", agent="agent")
    return {"rows": {r["market"]: r for r in rows}, "client": client, "con": con, "ref": ref}


def za(world, name):
    return world["rows"]["ZA"][name]


def ng(world, name):
    return world["rows"]["NG"][name]


def test_one_row_per_market_for_the_week(world):
    rows = world["rows"]
    assert sorted(rows) == ["KE", "NG", "ZA"]
    for row in rows.values():
        assert row["week_start"] == WEEK and row["week_end"] == d(10, 11)
        assert row["run_id"] == RUN_ID
        for name in FIGURES:
            assert set(row[name]) >= {"value", "unit", "query_id", "run_id", "result_hash", "n", "reason"}


def test_time_to_detect_is_the_median_over_items_first_mainstream_in_the_week(world):
    f = za(world, "time_to_detect")
    assert f["value"] == 3.5 and f["n"] == 4 and f["unit"].startswith("days")   # A 2, E 2, B 5, D never flagged
    assert "first Emerging or Rising" in f["unit"] and "more than" not in f["unit"]
    assert f["reason"] is None


def test_time_to_detect_without_mainstream_items_is_not_measured(world):
    f = world["rows"]["KE"]["time_to_detect"]
    assert f["value"] is None and "Mainstream" in f["reason"]


def test_lead_time_scores_only_held_out_entries_dated_in_the_week(world):
    f = za(world, "lead_time")
    assert f["value"] == 2 and f["n"] == 2          # mopepe 5 days early, toy story 1 day late
    assert "3" in f["unit"]                          # of the 3 entries dated this week


def test_lead_time_without_a_flagged_entry_is_null_with_a_reason(world):
    f = ng(world, "lead_time")
    assert f["value"] is None and "1" in f["reason"]


def test_precision_reads_the_random_review_top3_only_never_taps(world):
    f = za(world, "precision")
    assert f["value"] == 0.5 and f["n"] == 4


def test_precision_without_review_rows_is_not_yet_measured(world):
    f = ng(world, "precision")
    assert f["value"] is None and "not yet measured" in f["reason"]


def test_recall_counts_moments_whose_item_had_a_state_that_day(world):
    f = za(world, "recall")
    assert f["value"] == pytest.approx(1 / 3) and f["n"] == 3
    assert "no matched item count as misses" in f["unit"]


def test_recall_without_matched_moments_is_null(world):
    assert ng(world, "recall")["value"] is None and "matched" in ng(world, "recall")["reason"]
    assert world["rows"]["KE"]["recall"]["value"] is None


def test_breadth_is_the_share_of_trends_found_on_two_or_more_platforms(world):
    f = za(world, "breadth_platforms")
    assert f["value"] == pytest.approx(2 / 7) and f["n"] == 7


def test_breadth_below_twenty_placebo_items_is_not_yet_measured(world):
    f = ng(world, "breadth_platforms")
    assert f["value"] is None and "placebo" in f["reason"]


def test_expansion_shares_are_the_largest_platform_and_cluster(world):
    assert za(world, "expansion_platform_share")["value"] == pytest.approx(0.4)
    assert za(world, "expansion_cluster_share")["value"] == pytest.approx(0.5)
    lang = za(world, "expansion_language_share")
    assert lang["value"] is None and "language" in lang["reason"]


def test_cluster_share_says_credits_with_no_item_are_left_out(world):
    assert "no item" in za(world, "expansion_cluster_share")["unit"]
    assert "no item" not in za(world, "expansion_platform_share")["unit"]


def test_expansion_without_credits_is_null(world):
    assert world["rows"]["KE"]["expansion_platform_share"]["value"] is None


def test_cost_per_confirmed_trend(world):
    f = za(world, "cost_per_confirmed")
    assert f["value"] == 50 and f["n"] == 3


def test_cost_without_confirmed_trends_is_null(world):
    f = ng(world, "cost_per_confirmed")
    assert f["value"] is None and "confirmed" in f["reason"]


SURFACING_REFERENCE = """
entries:
  - {id: ke-march, market: KE, title: t, event_date: 2026-10-07, match_terms: [saba saba march], added: 2026-10-01,
     source: fixture}
  - {id: ke-protest, market: KE, title: t, event_date: 2026-10-07, match_terms: [saba saba protest],
     added: 2026-10-01, source: fixture}
  - {id: ke-held, market: KE, title: t, event_date: 2026-10-08, match_terms: [held tag], added: 2026-10-01,
     source: fixture}
  - {id: ke-boards, market: KE, title: t, event_date: 2026-10-08, match_terms: [board song], added: 2026-10-01,
     source: fixture}
  - {id: ke-fresh, market: KE, title: t, event_date: 2026-10-08, match_terms: [fresh tune], added: 2026-10-01,
     source: fixture}
  - {id: ke-spike, market: KE, title: t, event_date: 2026-10-08, match_terms: [spike song], added: 2026-10-01,
     source: fixture}
  - {id: ke-short, market: KE, title: t, event_date: 2026-10-08, match_terms: [ft w], added: 2026-10-01,
     source: fixture}
"""


def query_rows(client, name, market, week, reference):
    sql = scorecard.queries(scorecard.load_reference(reference))[name]
    return sqlrun.query(client, sql, scorecard.params(market, week), core="core", agent="agent")


@pytest.fixture(scope="module")
def surfacing(tmp_path_factory):
    """KE only, 5 to 11 October 2026: the states 42 publishes as a trend signal, and the ones it does not."""
    con = duck.connect()
    con.execute(EXTRA_TABLES)
    days = [d(9, 1) + timedelta(days=i) for i in range(50)]
    duck.load(con, "agent.runs",
              [run(stage, x) for x in days for stage in ("collect", "aggregate", "stats", "detect", "brief")])
    ke = dict(market="KE")
    duck.load(con, "core.item_state", [
        # K: Rising with a moment attached, so state.sql calls it Seasonal, 4 days before the entry.
        state("K", d(10, 3), "seasonal", **ke, **RISING), state("K", d(10, 8), "seasonal", **ke, **RISING),
        # S: a hashtag key with no spaces, Rising 3 days before the entry.
        state("S", d(10, 4), "rising", **ke),
        # F: only ever Fading. R: Rising but likely coordinated, so ineligible. B: only on a platform's board.
        state("F", d(10, 6), "fading", **ke),
        state("R", d(10, 5), "rising", **ke, eligible=False),
        state("R", d(10, 6), "rising", **ke, eligible=False),
        state("B", d(10, 5), "on_the_boards", **ke),
        state("B", d(10, 7), "on_the_boards", **ke),
        # NT: New to 42 near a moment, so Seasonal. SP: a tested Spike near a moment, so Seasonal.
        # HS: Seasonal only because yesterday was (two-day hysteresis); today it classified Peaking.
        state("NT", d(10, 4), "seasonal", **ke, **FRESH), state("NT", d(10, 7), "seasonal", **ke, **FRESH),
        state("SP", d(10, 5), "seasonal", **ke, **SPIKE), state("SP", d(10, 7), "seasonal", **ke, **SPIKE),
        state("HS", d(10, 7), "seasonal", state_raw="peaking", **ke, **RISING),
        # UW: untested with the floors met but 3 observed days. UE: the same with 6 observed days and no fall
        # in posts, so it qualifies as untested Emerging.
        state("UW", d(10, 7), "seasonal", **ke, **WARM_UP), state("UE", d(10, 7), "seasonal", **ke, **WARM_UP),
        # ND surfaced the day after its moment, TD two days after. SW: the key a short term would hit.
        state("ND", d(10, 10), "rising", **ke), state("TD", d(10, 11), "rising", **ke),
        state("SW", d(10, 4), "rising", **ke),
        # Time to detect over an even count: M1 2 days, M2 5 days.
        state("M1", d(10, 3), "emerging", **ke), state("M1", d(10, 6), "mainstream", **ke),
        state("M2", d(10, 3), "rising", **ke), state("M2", d(10, 7), "mainstream", **ke),
    ])
    duck.load(con, "core.series_test", [
        sig("K", d(10, 3), 3.0, "KE"), sig("K", d(10, 8), 3.0, "KE"),
        sig("SP", d(10, 5), 1.5, "KE"), sig("SP", d(10, 7), 1.5, "KE"), sig("HS", d(10, 7), 3.0, "KE"),
        sig("UW", d(10, 7), None, "KE", significant=False, test="none", obs_prior=2),
        sig("UE", d(10, 7), None, "KE", significant=False, test="none", obs_prior=5),
    ])
    duck.load(con, "core.item_daily", [
        item_daily("M1", d(10, 1), 4, market="KE", lane_class="unbiased_rank", platform="tiktok", series=None,
                   protocol=None),
        item_daily("M2", d(9, 28), 3, market="KE"),
    ])
    duck.load(con, "core.cultural_map", [
        cmap("K", "Saba Saba march"), cmap("S", "#sabasabaprotest"), cmap("F", "fadingthing"),
        cmap("R", "#heldtag"), cmap("B", "Board song"), cmap("NT", "#freshtune"), cmap("SP", "Spike song"),
        cmap("SW", "#ftw2026"),
    ])
    moments = [("fading", 6, "F"), ("held", 6, "R"), ("boards", 7, "B"), ("seasonal", 8, "K"),
               ("fading only", 9, "F"), ("fresh", 7, "NT"), ("spike", 7, "SP"), ("hysteresis", 7, "HS"),
               ("warm-up", 7, "UW"), ("warm-up emerging", 7, "UE"), ("next day", 9, "ND"),
               ("two days", 9, "TD")]
    duck.load(con, "core.calendar",
              [{"moment_date": d(10, dd), "market": "KE", "name": n, "kind": "holiday", "item_ids": [i]}
               for n, dd, i in moments]
              + [{"moment_date": d(10, 10), "market": "KE", "name": "unmatched", "kind": "holiday",
                  "item_ids": []}])
    ref = tmp_path_factory.mktemp("surfacing") / "ground_truth.yaml"
    ref.write_text(SURFACING_REFERENCE, encoding="utf-8")
    client = duck.Client(con)
    rows = scorecard.run_scorecard(client, WEEK, run_id=RUN_ID, reference=ref, core="core", agent="agent")
    return {"row": {r["market"]: r for r in rows}["KE"], "client": client, "ref": ref}


def test_lead_time_counts_seasonal_and_hashtag_keys_with_no_spaces(surfacing):
    f = surfacing["row"]["lead_time"]
    assert f["n"] == 2                               # the march 4 days early, the hashtag 3 days early
    assert f["value"] == 3.5
    assert "2 of 7" in f["unit"]


def test_lead_time_never_counts_what_is_not_a_trend_signal(surfacing):
    rows = {r["ref_id"]: r for r in query_rows(surfacing["client"], "lead_time", "KE", WEEK, surfacing["ref"])}
    assert rows["ke-march"]["lead_days"] == 4 and rows["ke-protest"]["lead_days"] == 3
    for ref_id in ("ke-held", "ke-boards", "ke-fresh", "ke-spike", "ke-short"):
        assert rows[ref_id]["flagged_on"] is None, ref_id


def test_recall_counts_a_trend_signal_on_the_day_or_the_day_after(surfacing):
    rows = {r["name"]: r["surfaced"]
            for r in query_rows(surfacing["client"], "recall", "KE", WEEK, surfacing["ref"])}
    assert set(rows) == {"fading", "held", "boards", "seasonal", "fading only", "fresh", "spike", "hysteresis",
                         "warm-up", "warm-up emerging", "next day", "two days", "unmatched"}
    assert {n for n, s in rows.items() if s} == {"seasonal", "warm-up emerging", "next day"}
    f = surfacing["row"]["recall"]
    assert f["n"] == 13 and f["value"] == pytest.approx(3 / 13)


def test_breadth_counts_only_trend_signals(surfacing):
    rows = query_rows(surfacing["client"], "breadth_platforms", "KE", WEEK, surfacing["ref"])
    assert {r["item_id"] for r in rows} == {"K", "UE", "ND", "TD", "M1", "M2"}   # S and SW rose before the week


def test_time_to_detect_over_an_even_count_is_the_mean_of_the_middle_two(surfacing):
    f = surfacing["row"]["time_to_detect"]
    assert f["n"] == 2 and f["value"] == 3.5


def test_a_moment_whose_only_item_was_fading_gives_recall_zero(tmp_path):
    con = duck.connect()
    con.execute(EXTRA_TABLES)
    duck.load(con, "agent.runs", [run(stage, d(10, 6)) for stage in ("collect", "aggregate", "detect", "brief")])
    duck.load(con, "core.item_state", [state("F", d(10, 6), "fading", market="KE")])
    duck.load(con, "core.calendar", [
        {"moment_date": d(10, 6), "market": "KE", "name": "m", "kind": "holiday", "item_ids": ["F"]}])
    ref = tmp_path / "ground_truth.yaml"
    ref.write_text(SURFACING_REFERENCE, encoding="utf-8")
    rows = scorecard.run_scorecard(duck.Client(con), WEEK, run_id=RUN_ID, reference=ref, core="core", agent="agent")
    f = {r["market"]: r for r in rows}["KE"]["recall"]
    assert f["value"] == 0 and f["n"] == 1


def test_state_sql_seasonal_from_new_to_42_is_not_surfaced_but_from_rising_is(tmp_path):
    """probe2's input: state.sql itself turns a New to 42 item near a moment into Seasonal."""
    from . import test_detect_states as ts
    from .fixtures import D

    con = duck.connect()
    for stmt in ts.sql_statements("waves.sql"):
        con.execute(duck.create_statement(stmt))
    con.execute(EXTRA_TABLES)
    w = ts.World()
    ts.fresh(w, "cal")
    w.t["core.calendar"] += [dict(ts.moment(0), name="fresh day"),
                             dict(ts.moment(0, item="riser"), name="rising day")]
    w.build(con)
    fresh = ts.detect(con)["cal"]
    assert (fresh["state_raw"], fresh["untested"], fresh["eligible"]) == ("seasonal", True, True)
    duck.load(con, "core.item_state", [state("riser", D, "seasonal", run_id=ts.RUN, **RISING)])
    duck.load(con, "core.series_test", [sig("riser", D, 3.0, run_id=ts.STATS_RUN)])
    duck.load(con, "agent.runs", [run("detect", D, run_id=ts.RUN)])
    ref = tmp_path / "ground_truth.yaml"
    ref.write_text(SURFACING_REFERENCE, encoding="utf-8")
    monday = D - timedelta(days=D.weekday())
    rows = {r["name"]: r["surfaced"] for r in query_rows(duck.Client(con), "recall", "ZA", monday, ref)}
    assert rows == {"fresh day": False, "rising day": True}


def overcount_world(ratio_today):
    """rev3_overcount's input: a tested item significant at ratio 3 on the two days before d while the floors
    were not met, then significant at ratio_today on d with the floors met and a moment on d. state.sql runs for
    d-1 and d."""
    from core.detect import stats

    from . import test_detect_states as ts
    from .fixtures import D, day

    con = duck.connect()
    for stmt in ts.sql_statements("waves.sql"):
        con.execute(duck.create_statement(stmt))
    con.execute(EXTRA_TABLES)
    w = ts.World()
    w.protocol("feed_tiktok", 60)
    for i in range(60, -1, -1):
        w.counter("overcountx", "feed_tiktok", i, 1.0)
    for i, cs in ((2, "ab"), (1, "cd"), (0, "efgh")):
        for c in cs:
            w.posts("overcountx", i, [f"ov-{c}"])
    w.t["core.calendar"] += [dict(ts.moment(0, item="overcountx"), name="the moment")]
    w.load(con)
    signal = duck.query(con, "SELECT * FROM {core}.tvf_series_signal(@d)", {"d": D})
    base = stats.passthrough_rows(signal, D, "x", ts.RULE)
    assert len(base) == 1
    duck.load(con, "core.series_test", [
        dict(base[0], metric_date=day(i), run_id=f"stats-{i}", test="nb", significant=True, ratio=ratio, mu=1.0,
             p_mid=.001, q=.01, first_measured=day(60), peak28=max(base[0]["peak28"] or 0, 1.0))
        for i, ratio in ((2, 3.0), (1, 3.0), (0, ratio_today))])
    duck.load(con, "agent.runs", [run("stats", day(i), f"stats-{i}") for i in (2, 1, 0)])
    temp, insert = ts.sql_statements("state.sql")
    con.execute(duck.temp_macro(temp))
    for when in (day(1), D):
        duck.run_duck(con, insert, {"d": when, "run_id": ts.RUN, "rule_version": ts.RULE, "authority": "v1"})
        duck.load(con, "agent.runs", [run("detect", when, run_id=ts.RUN)])
    got = duck.query(con, "SELECT * FROM {core}.item_state s WHERE s.metric_date = @d", {"d": D})
    return con, D, {r["item_id"]: r for r in got}["overcountx"]


@pytest.mark.parametrize("ratio_today,surfaced", [(1.5, False), (3.0, True)])
def test_a_spike_after_two_strong_days_is_not_rising_but_a_rising_day_is(tmp_path, ratio_today, surfaced):
    con, day0, row = overcount_world(ratio_today)
    assert (row["state"], row["eligible"], row["untested"], row["sig_days3"] >= 2) == ("seasonal", True, False, True)
    assert row["base_state"] == ("rising" if surfaced else "spike")
    ref = tmp_path / "ground_truth.yaml"
    ref.write_text("entries:\n  - {id: ov, market: ZA, title: t, event_date: %s, match_terms: [overcountx], "
                   "added: 2026-01-01, source: s}\n" % day0.isoformat(), encoding="utf-8")
    monday = day0 - timedelta(days=day0.weekday())
    client = duck.Client(con)
    assert [r["surfaced"] for r in query_rows(client, "recall", "ZA", monday, ref)] == [surfaced]
    flagged = [r["flagged_on"] for r in query_rows(client, "lead_time", "ZA", monday, ref)]
    assert flagged == [day0 if surfaced else None]


def test_a_stats_rerun_after_the_detect_run_is_not_scored(tmp_path):
    """rev4 probe B: the ratio 1.5 Spike, then stats for d rerun at ratio 3.0 at 18:00, after the 12:00 detect
    run. The recheck would score data state.sql never saw, so the row is dropped. The same item with ratio 3.0
    from the start, every run at 12:00, still counts (test_a_spike_after_two_strong_days_...[3.0-True])."""
    con, day0, _ = overcount_world(1.5)
    base = duck.query(con, "SELECT * FROM core.series_test WHERE run_id = 'stats-0'", {})[0]
    duck.load(con, "core.series_test", [dict(base, run_id="stats-0b", ratio=3.0)])
    duck.load(con, "agent.runs", [run("stats", day0, "stats-0b", hour=18)])
    ref = tmp_path / "ground_truth.yaml"
    ref.write_text("entries:\n  - {id: ov, market: ZA, title: t, event_date: %s, match_terms: [overcountx], "
                   "added: 2026-01-01, source: s}\n" % day0.isoformat(), encoding="utf-8")
    monday = day0 - timedelta(days=day0.weekday())
    client = duck.Client(con)
    assert [r["surfaced"] for r in query_rows(client, "recall", "ZA", monday, ref)] == [False]
    assert [r["flagged_on"] for r in query_rows(client, "lead_time", "ZA", monday, ref)] == [None]


def test_a_stats_rerun_during_the_detect_run_is_not_scored(tmp_path):
    """rev5s_race: detect ran 11:00 to 12:00 and its own stats step finished at 11:30; a rerun for d finished at
    11:50, before detect finished. v_good_runs now reads the rerun, which state.sql may not have seen."""
    con, day0, _ = overcount_world(1.5)
    con.execute("UPDATE agent.runs SET finished_at = finished_at - INTERVAL 30 MINUTE WHERE stage = 'stats'")
    base = duck.query(con, "SELECT * FROM core.series_test WHERE run_id = 'stats-0'", {})[0]
    duck.load(con, "core.series_test", [dict(base, run_id="stats-0b", ratio=3.0)])
    rerun = run("stats", day0, "stats-0b")
    rerun["finished_at"] -= timedelta(minutes=10)
    duck.load(con, "agent.runs", [rerun])
    ref = tmp_path / "ground_truth.yaml"
    ref.write_text("entries:\n  - {id: ov, market: ZA, title: t, event_date: %s, match_terms: [overcountx], "
                   "added: 2026-01-01, source: s}\n" % day0.isoformat(), encoding="utf-8")
    monday = day0 - timedelta(days=day0.weekday())
    client = duck.Client(con)
    assert [r["surfaced"] for r in query_rows(client, "recall", "ZA", monday, ref)] == [False]


def recall_after(con, day0, tmp_path):
    ref = tmp_path / "ground_truth.yaml"
    ref.write_text("entries:\n  - {id: ov, market: ZA, title: t, event_date: %s, match_terms: [overcountx], "
                   "added: 2026-01-01, source: s}\n" % day0.isoformat(), encoding="utf-8")
    monday = day0 - timedelta(days=day0.weekday())
    return [r["surfaced"] for r in query_rows(duck.Client(con), "recall", "ZA", monday, ref)]


def test_a_stats_rerun_for_an_earlier_day_after_the_detect_run_started_is_not_scored(tmp_path):
    """The Rising day (ratio 3.0 throughout), then stats for d-1 rerun at 18:00 on d: state.sql for d read the
    d-1 run that came before it."""
    from .fixtures import day

    con, day0, _ = overcount_world(3.0)
    assert recall_after(con, day0, tmp_path) == [True]
    base = duck.query(con, "SELECT * FROM core.series_test WHERE run_id = 'stats-1'", {})[0]
    duck.load(con, "core.series_test", [dict(base, run_id="stats-1b")])
    rerun = run("stats", day(1), "stats-1b")
    rerun["finished_at"] = rerun["finished_at"] + timedelta(days=1, hours=6)
    duck.load(con, "agent.runs", [rerun])
    assert recall_after(con, day0, tmp_path) == [False]


@pytest.mark.parametrize("finished", ["18:00", "unknown"])
def test_the_only_stats_run_for_the_day_finishing_after_detect_is_not_scored(tmp_path, finished):
    """The Rising day again, but its one stats run finished after the detect run did, or has no finish time: the
    detect run cannot have read it."""
    con, day0, _ = overcount_world(3.0)
    value = "finished_at + INTERVAL 6 HOUR" if finished == "18:00" else "NULL"
    con.execute(f"UPDATE agent.runs SET finished_at = {value} WHERE run_id = 'stats-0'")
    assert recall_after(con, day0, tmp_path) == [False]


def test_a_detect_run_with_no_finish_time_is_not_trusted(tmp_path):
    """rev5s_probe case 3: the 18:00 rerun, and the detect ok row carries no finished_at."""
    con, day0, _ = overcount_world(1.5)
    base = duck.query(con, "SELECT * FROM core.series_test WHERE run_id = 'stats-0'", {})[0]
    duck.load(con, "core.series_test", [dict(base, run_id="stats-0b", ratio=3.0)])
    duck.load(con, "agent.runs", [run("stats", day0, "stats-0b", hour=18)])
    con.execute("UPDATE agent.runs SET finished_at = NULL WHERE stage = 'detect'")
    ref = tmp_path / "ground_truth.yaml"
    ref.write_text("entries:\n  - {id: ov, market: ZA, title: t, event_date: %s, match_terms: [overcountx], "
                   "added: 2026-01-01, source: s}\n" % day0.isoformat(), encoding="utf-8")
    monday = day0 - timedelta(days=day0.weekday())
    client = duck.Client(con)
    assert [r["surfaced"] for r in query_rows(client, "recall", "ZA", monday, ref)] == [False]


@pytest.fixture(scope="module")
def rising_paths(tmp_path_factory):
    """KE, 5 to 11 October 2026: Seasonal rows with ratio 2 today on one day only, each with a different second
    source, and three items for time to detect: two whose first flag is a Seasonal row, and one first seen before
    the 56-day lookback (10 August)."""
    con = duck.connect()
    con.execute(EXTRA_TABLES)
    days = [d(7, 20) + timedelta(days=i) for i in range(90)]
    duck.load(con, "agent.runs",
              [run(stage, x) for x in days for stage in ("collect", "aggregate", "stats", "detect", "brief")])
    ke = dict(market="KE")
    seasonal = ["P2", "OM", "GP", "GS", "P1", "LR"]
    # Each fails one floor or the significance a tested Emerging needs, with a strong series that day.
    short = {"FC": dict(RISING, creators3=4), "FP": dict(RISING, posts3=7), "FS": dict(RISING, top_creator_share3=.5),
             "NS": EMERGING}
    duck.load(con, "core.item_state",
              [state(i, d(10, 7), "seasonal", **ke, **ONE_DAY) for i in seasonal]
              + [state(i, d(10, 7), "seasonal", **ke, **cols) for i, cols in short.items()] + [
        # TT: seen 28 Sep, Rising-grade Seasonal 30 Sep, Rising 2 Oct, Mainstream 6 Oct: 2 days.
        # TX: seen 28 Sep, Spike-grade Seasonal 30 Sep, Rising 2 Oct, Mainstream 7 Oct: 4 days.
        state("TT", d(9, 30), "seasonal", **ke, **RISING), state("TT", d(10, 2), "rising", **ke),
        state("TT", d(10, 6), "mainstream", **ke),
        state("TX", d(9, 30), "seasonal", **ke, **RISING), state("TX", d(10, 2), "rising", **ke),
        state("TX", d(10, 7), "mainstream", **ke),
        state("OLD", d(9, 30), "rising", **ke), state("OLD", d(10, 8), "mainstream", **ke),
    ])
    today = d(10, 7)
    duck.load(con, "core.series_test", [
        sig("P2", today, 3.0, "KE"), sig("P2", today, 1.2, "KE", platform="instagram"),     # second platform
        sig("OM", today, 3.0, "KE"), sig("OM", d(10, 6), 1.2, "NG"),                         # another market
        sig("GP", today, 3.0, "KE"), sig("GP", today, 1.2, "GLOBAL", platform="youtube"),   # GLOBAL, new platform
        sig("GS", today, 3.0, "KE"), sig("GS", today, 1.2, "GLOBAL"),                        # GLOBAL, same platform
        sig("P1", today, 3.0, "KE"),                                                         # nothing else
        sig("LR", today, 1.5, "KE"), sig("LR", today, 1.2, "KE", platform="instagram"),      # no ratio 2 today
        sig("TT", d(9, 30), 3.0, "KE"), sig("TX", d(9, 30), 1.5, "KE"),
        sig("FC", today, 3.0, "KE"), sig("FP", today, 3.0, "KE"), sig("FS", today, 3.0, "KE"),
        sig("NS", today, 3.0, "KE", significant=False),
    ])
    duck.load(con, "core.item_daily", [
        item_daily(i, d(9, 28), 4, market="KE", lane_class="unbiased_rank", platform="tiktok", series=None,
                   protocol=None) for i in ("TT", "TX")]
        + [item_daily("OLD", x, 2, market="KE") for x in (d(7, 25), d(8, 10), d(9, 1))])
    duck.load(con, "core.calendar", [
        {"moment_date": today, "market": "KE", "name": i, "kind": "holiday", "item_ids": [i]}
        for i in seasonal + list(short)])
    ref = tmp_path_factory.mktemp("rising") / "ground_truth.yaml"
    ref.write_text(SURFACING_REFERENCE, encoding="utf-8")
    return {"client": duck.Client(con), "ref": ref}


def test_rising_from_a_second_platform_another_market_or_a_new_global_platform(rising_paths):
    rows = query_rows(rising_paths["client"], "recall", "KE", WEEK, rising_paths["ref"])
    assert {r["name"] for r in rows if r["surfaced"]} == {"P2", "OM", "GP"}
    assert len(rows) == 10


def test_time_to_detect_counts_a_rising_grade_seasonal_row_as_the_first_flag(rising_paths):
    rows = {r["item_id"]: r for r in query_rows(rising_paths["client"], "time_to_detect", "KE", WEEK,
                                                rising_paths["ref"])}
    assert (rows["TT"]["flagged_on"], rows["TT"]["days"]) == (d(9, 30), 2)
    assert (rows["TX"]["flagged_on"], rows["TX"]["days"]) == (d(10, 2), 4)


def test_time_to_detect_counts_from_the_true_first_sighting_however_early(rising_paths):
    rows = {r["item_id"]: r for r in query_rows(rising_paths["client"], "time_to_detect", "KE", WEEK,
                                                rising_paths["ref"])}
    assert (rows["OLD"]["first_seen"], rows["OLD"]["flagged_on"], rows["OLD"]["days"]) == (d(7, 25), d(9, 30), 67)


@pytest.fixture(scope="module")
def based(tmp_path_factory):
    """KE, 5 to 11 October 2026: Seasonal and Recurring rows that carry base_state, the state state.sql decided
    before the override, next to rows written before that column existed (base_state NULL). Each item has its
    own moment on its row's day. 8 October's stats were rerun at 18:00, after that day's detect run."""
    con = duck.connect()
    con.execute(EXTRA_TABLES)
    days = [d(9, 1) + timedelta(days=i) for i in range(50)]
    duck.load(con, "agent.runs",
              [run(stage, x) for x in days for stage in ("collect", "aggregate", "stats", "detect", "brief")]
              + [run("stats", d(10, 8), "stats-late", hour=18)])
    ke, today = dict(market="KE"), d(10, 7)
    rows = {
        # Resting on Rising or Emerging: counts, with no series test for a recheck to find.
        "BR": ("seasonal", "rising", today, {}), "BE": ("recurring", "emerging", today, {}),
        # Resting on Spike or New to 42: never counts, though the tables now pass the Rising or Emerging recheck.
        "BS": ("seasonal", "spike", today, RISING), "BN": ("seasonal", "new_to_42", today, WARM_UP),
        # Resting on Rising, but ineligible, held at Seasonal by the hysteresis, or on a day stats were rerun late.
        "BI": ("seasonal", "rising", today, {"eligible": False}),
        "BH": ("seasonal", "rising", today, {"state_raw": "peaking"}),
        "BL": ("seasonal", "rising", d(10, 8), {}),
        # Written before the column: the recheck decides, as before.
        "PR": ("seasonal", None, today, RISING), "PS": ("seasonal", None, today, SPIKE),
        "PE": ("recurring", None, today, WARM_UP),
    }
    duck.load(con, "core.item_state", [state(i, when, st, **ke, base_state=base, **cols)
                                       for i, (st, base, when, cols) in rows.items()])
    duck.load(con, "core.series_test", [
        sig("BS", today, 3.0, "KE"), sig("PR", today, 3.0, "KE"), sig("PS", today, 1.5, "KE"),
        sig("BN", today, None, "KE", significant=False, test="none", obs_prior=5),
        sig("PE", today, None, "KE", significant=False, test="none", obs_prior=5),
    ])
    duck.load(con, "core.calendar", [
        {"moment_date": when, "market": "KE", "name": i, "kind": "holiday", "item_ids": [i]}
        for i, (_, _, when, _) in rows.items()])
    ref = tmp_path_factory.mktemp("based") / "ground_truth.yaml"
    ref.write_text(SURFACING_REFERENCE, encoding="utf-8")
    return {"client": duck.Client(con), "ref": ref}


def test_base_state_decides_a_seasonal_or_recurring_row_and_null_falls_back_to_the_recheck(based):
    rows = {r["name"]: r["surfaced"] for r in query_rows(based["client"], "recall", "KE", WEEK, based["ref"])}
    assert len(rows) == 10
    assert {n for n, s in rows.items() if s} == {"BR", "BE", "PR", "PE"}


@pytest.fixture(scope="module")
def waits(tmp_path_factory):
    """rev5s_probe and rev5s_ttd's time to detect cases. KE: GAP seen 25 Jul and 20 Aug, EDGE seen 9 and 11 Aug,
    both Rising 30 Sep; FLG seen 25 Jul, Rising 1 Aug and 25 Aug. ZA: A 2 days, B 3 days, C seen 10 Aug and
    Rising 4 Oct. NG: P 2 days; Q seen 1 Sep and S seen 10 Sep, never flagged; R flagged 1 Oct with no
    sighting before it."""
    con = duck.connect()
    con.execute(EXTRA_TABLES)
    days = [d(7, 1) + timedelta(days=i) for i in range(110)]
    duck.load(con, "agent.runs", [run(s, x) for x in days for s in ("collect", "aggregate", "stats", "detect")])
    ke, ng = dict(market="KE"), dict(market="NG")
    duck.load(con, "core.item_state", [
        state("GAP", d(9, 30), "rising", **ke), state("GAP", d(10, 8), "mainstream", **ke),
        state("EDGE", d(9, 30), "rising", **ke), state("EDGE", d(10, 8), "mainstream", **ke),
        state("FLG", d(8, 1), "rising", **ke), state("FLG", d(8, 25), "rising", **ke),
        state("FLG", d(10, 8), "mainstream", **ke),
        state("A", d(9, 30), "rising"), state("A", d(10, 6), "mainstream"),
        state("B", d(9, 30), "rising"), state("B", d(10, 6), "mainstream"),
        state("C", d(10, 4), "rising"), state("C", d(10, 7), "mainstream"),
        state("P", d(9, 30), "rising", **ng), state("P", d(10, 6), "mainstream", **ng),
        state("Q", d(10, 7), "mainstream", **ng),
        state("R", d(10, 1), "rising", **ng), state("R", d(10, 8), "mainstream", **ng),
        state("S", d(10, 8), "mainstream", **ng),
    ])
    duck.load(con, "core.item_daily",
              [item_daily("GAP", x, 2, **ke) for x in (d(7, 25), d(8, 20))]
              + [item_daily("EDGE", x, 2, **ke) for x in (d(8, 9), d(8, 11))]
              + [item_daily("FLG", x, 2, **ke) for x in (d(7, 25), d(8, 20))]
              + [item_daily("A", d(9, 28), 2), item_daily("B", d(9, 27), 2), item_daily("C", d(8, 10), 2)]
              + [item_daily("P", d(9, 28), 2, **ng), item_daily("Q", d(9, 1), 2, **ng),
                 item_daily("R", d(10, 3), 2, **ng), item_daily("S", d(9, 10), 2, **ng)])
    ref = tmp_path_factory.mktemp("waits") / "ground_truth.yaml"
    ref.write_text(SURFACING_REFERENCE, encoding="utf-8")
    client = duck.Client(con)
    rows = {m: query_rows(client, "time_to_detect", m, WEEK, ref) for m in ("KE", "ZA", "NG")}
    trace = {"query_id": "q", "run_id": RUN_ID, "result_hash": "h"}
    return {m: ({r["item_id"]: r for r in rs}, scorecard.time_to_detect(rs, trace)) for m, rs in rows.items()}


def test_time_to_detect_counts_waits_that_began_long_before_the_week(waits):
    rows, _ = waits["KE"]
    assert {i: rows[i]["days"] for i in ("GAP", "EDGE", "FLG")} == {"GAP": 67, "EDGE": 52, "FLG": 7}
    assert rows["FLG"]["flagged_on"] == d(8, 1)


def test_time_to_detect_keeps_a_long_wait_in_the_median(waits):
    rows, f = waits["ZA"]
    assert rows["C"]["days"] == 55
    assert f["value"] == 3 and f["n"] == 3 and "3 of 3" in f["unit"] and "more than" not in f["unit"]


def test_time_to_detect_median_on_an_unmeasured_item_reads_more_than(waits):
    rows, f = waits["NG"]
    assert (rows["Q"]["first_seen"], rows["Q"]["flagged_on"]) == (d(9, 1), None)
    assert rows["R"]["first_seen"] is None and rows["R"]["days"] is None
    # P 2; R at least the longest measured wait, 2; S more than 28 (10 Sep to 8 Oct); Q more than 36.
    assert f["value"] == 15 and f["n"] == 4 and "1 of 4" in f["unit"] and "more than" in f["unit"]


def test_sql_literals_come_from_the_python_constants():
    raw = scorecard.SQL.read_text(encoding="utf-8")
    for literal in ("INTERVAL 28 DAY", "INTERVAL 56 DAY", ">= 6", "2026-04-21"):
        assert literal not in raw, literal
    sql = scorecard.queries(scorecard.load_reference())
    assert f"INTERVAL {scorecard.LEAD_WINDOW} DAY" in sql["lead_time"]
    assert f">= {scorecard.MIN_TERM}" in sql["lead_time"]
    assert f"INTERVAL {scorecard.RECALL_DAYS} DAY" in sql["recall"]
    assert f"DATE '{scorecard.DATA_START.isoformat()}'" in sql["time_to_detect"]
    for name, text in sql.items():
        assert not re.search(r"\{(?!core\}|agent\})", text), name
    f = scorecard.recall([], {"query_id": "q", "run_id": "r", "result_hash": "h"})
    assert f"{scorecard.RECALL_DAYS} day" in f["unit"]
    body = [line for line in scorecard.REFERENCE.read_text(encoding="utf-8").splitlines() if "under" in line]
    assert any(f"under {scorecard.MIN_TERM} characters" in line for line in body)


def test_time_to_detect_unit_says_how_many_mainstream_items_it_rests_on(world):
    f = za(world, "time_to_detect")
    assert "3 of 4" in f["unit"]                     # D reached Mainstream but was never Emerging or Rising


def test_state_sql_untested_seasonal_counts_only_as_untested_emerging(tmp_path):
    """state.sql's warm-up rows near a moment: Emerging (5 observed days, posts not falling) counts; the same
    with 4 observed days or falling posts is New to 42 underneath and does not."""
    from . import test_detect_states as ts
    from .fixtures import D

    con = duck.connect()
    for stmt in ts.sql_statements("waves.sql"):
        con.execute(duck.create_statement(stmt))
    con.execute(EXTRA_TABLES)
    w = ts.World()
    ts.emerging_item(w, "emerge", 5, 8, "p1")
    ts.emerging_item(w, "four", 4, 0, "p2")
    ts.emerging_item(w, "falling", 5, 9, "p3")
    w.t["core.calendar"] += [dict(ts.moment(0, item=i), name=i) for i in ("emerge", "four", "falling")]
    w.build(con)
    got = ts.detect(con)
    assert {i: (got[i]["state"], got[i]["untested"]) for i in ("emerge", "four", "falling")} == {
        i: ("seasonal", True) for i in ("emerge", "four", "falling")}
    assert {i: got[i]["base_state"] for i in got} == {"emerge": "emerging", "four": "new_to_42",
                                                      "falling": "new_to_42"}
    duck.load(con, "agent.runs", [run("detect", D, run_id=ts.RUN)])
    ref = tmp_path / "ground_truth.yaml"
    ref.write_text(SURFACING_REFERENCE, encoding="utf-8")
    monday = D - timedelta(days=D.weekday())
    rows = {r["name"]: r["surfaced"] for r in query_rows(duck.Client(con), "recall", "ZA", monday, ref)}
    assert rows == {"emerge": True, "four": False, "falling": False}
    # rev4 probe A: after the detect run, a creator who posted only in the previous 3 days is flagged, so
    # tvf_item_window now reads posts3_prev 8 against posts3 8. state.sql saw 9 and held falling out of Emerging.
    con.execute("UPDATE core.creators SET coord_score = 1 WHERE creator_id = 'falling-prev0'")
    rows = {r["name"]: r["surfaced"] for r in query_rows(duck.Client(con), "recall", "ZA", monday, ref)}
    assert rows == {"emerge": True, "four": False, "falling": False}


def test_real_reference_terms_miss_standing_tags_and_hit_the_event():
    con = duck.connect()
    con.execute(EXTRA_TABLES)
    duck.load(con, "agent.runs", [run("detect", d(6, 1) + timedelta(days=i)) for i in range(40)])
    duck.load(con, "core.item_state", [
        state("TP", d(6, 20), "rising", market="KE"), state("BL", d(6, 18), "rising"),
        state("TS", d(6, 22), "rising"), state("BK", d(6, 20), "rising"),
    ])
    duck.load(con, "core.cultural_map", [
        cmap("TP", "#torturedpoetsdepartment"), cmap("BL", "#blast16"), cmap("TS", "#toystory"),
        cmap("BK", "#bafanaknockout"),
    ])
    client = duck.Client(con)
    ke = {r["ref_id"]: r for r in query_rows(client, "lead_time", "KE", d(6, 22), scorecard.REFERENCE)}
    za = {r["ref_id"]: r for r in query_rows(client, "lead_time", "ZA", d(6, 22), scorecard.REFERENCE)}
    assert ke["ke-protesters-tortured"]["flagged_on"] is None
    assert za["za-bafana-first-wc-knockout"]["flagged_on"] == d(6, 20)     # the event tag, not #blast16
    za = {r["ref_id"]: r for r in query_rows(client, "lead_time", "ZA", d(6, 29), scorecard.REFERENCE)}
    assert za["za-tyla-toy-story-5"]["flagged_on"] is None


def test_every_measured_number_traces_to_a_query_and_its_result(world):
    for row in world["rows"].values():
        for name in FIGURES:
            f = row[name]
            if name == "expansion_language_share":
                continue
            assert f["query_id"].startswith("q_") and f["result_hash"].startswith("sha256:")
            assert f["run_id"] == RUN_ID


def test_a_rerun_gives_the_same_query_ids_and_hashes(world):
    again = scorecard.run_scorecard(world["client"], WEEK, run_id=RUN_ID, reference=world["ref"],
                                    core="core", agent="agent")
    for row in again:
        before = world["rows"][row["market"]]
        for name in FIGURES:
            assert row[name] == before[name]


def test_the_scorecard_only_reads(world):
    for sql in world["client"].sql:
        head = sql.lstrip().split(None, 1)[0].upper()
        assert head in ("SELECT", "WITH"), head
    text = scorecard.SQL.read_text(encoding="utf-8").upper()
    for word in ("INSERT", "MERGE", "DELETE", "UPDATE ", "CREATE", "DROP", "TRUNCATE"):
        assert word not in text


def test_week_start_must_be_a_monday(world):
    with pytest.raises(ValueError):
        scorecard.run_scorecard(world["client"], d(10, 6), reference=world["ref"], core="core", agent="agent")


def test_lifted_reference_list_loads_and_carries_no_age_terms():
    entries = scorecard.load_reference()
    assert entries and all(e["market"] in ("ZA", "NG", "KE") for e in entries)
    text = scorecard.REFERENCE.read_text(encoding="utf-8").lower()
    short = [t for e in entries for t in e["match_terms"] if len(t.replace(" ", "")) < 6]
    assert not short                                  # a term under 6 characters never matches
    assert "- wreath\n" not in text                   # a bare wreath names no event
    for word in ("gen z", "gen-z", "genz", "youth", "millennial", "google trends"):
        assert word not in text


# Bare standing topics: each matches months of coverage, so as a term it would fake lead time.
STANDING_TOPICS = {
    "bafana", "bafana bafana", "springboks", "tyla", "ramaphosa", "ekurhuleni", "kaizer chiefs", "sundowns",
    "soweto derby", "cape town", "weather", "load shedding", "nigeria", "independence", "independence day",
    "nollywood", "ruth kadiri", "olu jacobs", "atiku", "peter obi", "election", "elections", "fuel price",
    "petrol price", "dangote", "bbnaija", "big brother", "davido", "inflation", "raila", "raila odinga",
    "eacc", "corruption", "churches", "refinery",
}


def test_real_reference_list_schema_ids_markets_dates_and_terms():
    """The shipped list: unique ids, ZA/NG/KE only, dates inside the data 42 holds and never a far-off
    guess, and every term long enough to match and naming an event rather than a bare standing topic."""
    raw = yaml.safe_load(scorecard.REFERENCE.read_text(encoding="utf-8"))["entries"]
    ids = [str(e["id"]) for e in raw]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)
    entries = scorecard.load_reference()
    for e in entries:
        assert e["market"] in ("ZA", "NG", "KE"), e["id"]
        assert e["id"].startswith(e["market"].lower() + "-"), e["id"]
        added = e["added"] if isinstance(e["added"], date) else date.fromisoformat(str(e["added"]))
        assert scorecard.DATA_START <= e["event_date"] <= added + timedelta(days=14), e["id"]
        for t in e["match_terms"]:
            assert len(t.replace(" ", "")) >= scorecard.MIN_TERM, (e["id"], t)
            assert t not in STANDING_TOPICS, (e["id"], t)


def test_reference_list_carries_the_28_sep_to_4_oct_week_from_thabangs_moments():
    """f42-learn scores 28 Sep to 4 Oct on Mon 5 Oct: each market needs dated entries for that week, each
    citing the colleague file and page it came from, and none marked as strategist-confirmed."""
    week = [e for e in scorecard.load_reference() if date(2026, 9, 28) <= e["event_date"] <= date(2026, 10, 4)]
    assert {e["market"] for e in week} == {"ZA", "NG", "KE"}
    for e in week:
        src = e["source"]
        assert "Thabang weekly moments 2026-10-02" in src, e["id"]
        assert re.search(r"[\w-]+\.pdf p\d", src), e["id"]
        assert "awaiting strategist confirmation" in src, e["id"]
        assert str(e["added"]) == "2026-10-02", e["id"]


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_scorecard_queries_dry_run_on_staging():
    from google.cloud import bigquery

    from core.detect import sqlrun
    client = bigquery.Client(project="ogilvy-trends-v2")
    for name, sql in scorecard.queries(scorecard.load_reference()).items():
        params = scorecard.params("ZA", WEEK)
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False,
                                         query_parameters=[sqlrun._param(k, v) for k, v in params.items()])
        job = client.query(sqlrun.render(sql), job_config=config)
        assert job.dry_run, name
