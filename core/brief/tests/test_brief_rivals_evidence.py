"""Detect's rival-explanation evidence in the brief's pack (METHOD-GAPS Gap 7, parts one and three).

Part one pins burst_share, top3_share, near_dup_share, sponsored_share, local_share, markets_hot and the 7-day post
count as numbers, and the share_flags, diffusion, lead_market, novelty, moment, small_at and large_at values as pinned
rows, each with a query_id, the detect run_id and a result_hash, so K2 re-runs them like creators3. Part three renders
at most two why-now sentences in code from those values.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from core.brief import evidence
from core.brief.tests.test_brief_evidence import (DETECT, add_post, build, canonical_hash, current, state_row, utc,
                                                  world)
from core.detect.tests import duck
from core.detect.tests.fixtures import D, day, item_daily, run
from core.trust.claims import check_answer

RIVAL_UNITS = [
    "posts in 7 days", "share of 7-day posts in the busiest 10 minutes", "share of 7-day posts by the top 3 creators",
    "share of 7-day posts that are near duplicates", "share of 7-day posts marked sponsored",
    "share of located 7-day posts from this market", "markets with a significant rise in 14 days"]
RIVAL_FIELDS = ["posts7", "burst_share", "top3_share", "near_dup_share", "sponsored_share", "local_share",
                "markets_hot"]
PIN_FIELDS = ["share_flags", "diffusion", "small_at", "large_at", "lead_market", "novelty", "moment"]
STATE = dict(sponsored_share=0.5, local_share=0.62, markets_hot=2, lead_market="NG", diffusion="bottom_up",
             novelty="new", moment="Heritage Day", share_flags=["burst", "concentrated"])


def rival_world(**state):
    """Fourteen posts for item i1 in ZA: 7 inside one 10 minute bucket (c1 x4 and c2 x3, c2 macro), the rest alone
    in their own buckets, so burst_share is 7/14 and top3_share is (4 + 3 + 2)/14; 5 are near duplicates."""
    con = world(**{**STATE, **state})
    plan = [("c1", utc(day(1), 10, 0), "micro"), ("c1", utc(day(1), 10, 1), "micro"),
            ("c1", utc(day(1), 10, 2), "micro"), ("c1", utc(day(1), 10, 3), "micro"),
            ("c2", utc(day(1), 10, 4), "macro"), ("c2", utc(day(1), 10, 5), "macro"),
            ("c2", utc(day(1), 10, 6), "macro"),
            ("c3", utc(day(2), 12), "micro"), ("c3", utc(day(2), 14), "micro"),
            ("c4", utc(day(3), 12), "micro"), ("c4", utc(day(3), 14), "micro"),
            ("c5", utc(day(4), 9), "micro"), ("c6", utc(day(4), 16), "micro"), ("c7", utc(day(5), 11), "micro")]
    for n, (creator, when, tier) in enumerate(plan):
        add_post(con, f"p{n}", creator, when, 100 - n, creator_tier_at_post=tier)
        if n < 5:
            duck.load(con, "core.post_enrichment", [{"post_id": f"p{n}", "near_dup_size": 3, "sponsored": False}])
    return con


def by_unit(pack):
    return {n["unit"]: n for n in pack["numbers"]}


def window_row(con):
    return duck.query(con, "SELECT * FROM {core}.tvf_item_window(@d) WHERE item_id = 'i1' AND market = 'ZA'",
                      {"d": D})[0]


def test_rival_numbers_follow_the_three_original_numbers_in_a_fixed_order():
    pack, *_ = build(rival_world())
    assert [n["unit"] for n in pack["numbers"]] == [
        "creators in 3 days", "posts in 3 days", "times usual"] + RIVAL_UNITS
    assert [n.get("rival_field") for n in pack["numbers"]] == [None, None, None] + RIVAL_FIELDS


def test_rival_numbers_have_the_values_a_hand_count_of_the_fixture_gives():
    con = rival_world()
    pack, *_ = build(con)
    got = {n["rival_field"]: n["value"] for n in pack["numbers"] if n.get("rival_field")}
    assert got == {"posts7": 14, "burst_share": pytest.approx(7 / 14), "top3_share": pytest.approx(9 / 14),
                   "near_dup_share": pytest.approx(5 / 14), "sponsored_share": 0.5, "local_share": 0.62,
                   "markets_hot": 2}


def test_each_rival_number_is_pinned_like_creators3_and_its_hash_is_recomputed_from_the_data():
    con = rival_world()
    pack, *_ = build(con)
    window, state = window_row(con), current(con)
    rivals = [n for n in pack["numbers"] if n.get("rival_field")]
    assert len(rivals) == len(RIVAL_FIELDS)
    for n in rivals:
        field = n["rival_field"]
        assert n["run_id"] == DETECT
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", n["result_hash"])
        assert isinstance(n["query_id"], str) and field in n["query_id"]
        source = window if field in window and field not in ("sponsored_share", "local_share",
                                                              "markets_hot") else state
        assert n["result_hash"] == canonical_hash([{"value": source[field]}])
    assert len({n["query_id"] for n in pack["numbers"]}) == len(pack["numbers"])


def test_non_numeric_detect_values_are_pinned_rows_that_never_sit_among_the_numbers():
    con = rival_world()
    pack, *_ = build(con)
    pins = {p["rival_field"]: p for p in pack["pinned"]}
    assert list(pins) == PIN_FIELDS
    window, state = window_row(con), current(con)
    for field, p in pins.items():
        source = window if field in ("small_at", "large_at") else state
        assert p["run_id"] == DETECT and field in p["query_id"]
        assert p["result_hash"] == canonical_hash([{"value": source[field]}])
    assert pins["share_flags"]["value"] == ["burst", "concentrated"]
    assert (pins["diffusion"]["value"], pins["lead_market"]["value"], pins["novelty"]["value"],
            pins["moment"]["value"]) == ("bottom_up", "NG", "new", "Heritage Day")
    assert datetime.fromisoformat(pins["small_at"]["value"]) == utc(day(5), 11)
    assert datetime.fromisoformat(pins["large_at"]["value"]) == utc(day(1), 10, 4)
    assert not any(n["unit"] in {"bottom_up", "NG"} for n in pack["numbers"])


def test_every_rival_number_and_pin_reruns_equal():
    con = rival_world()
    pack, _, rerun, _ = build(con)
    for entry in pack["numbers"] + pack["pinned"]:
        assert rerun(entry) == entry["value"], entry["query_id"]


def k2_row(pack, rerun, text, entries):
    claim = {"id": "c1", "text": text, "label": "observed", "kind": "observation",
             "evidence_ids": [pack["evidence"][0]["id"]], "quotes": [], "numbers": entries}
    answer = {"status": "complete", "short_answer": "", "evidence": pack["evidence"], "so_what": [],
              "watch_next": [], "gaps": [], "context": "", "claims": [claim]}
    _, rows = check_answer(answer, window_start="2026-09-14T00:00:00+02:00", window_end="2026-09-20T23:59:59+02:00",
                           market="ZA", rerun=rerun)
    return next(r for r in rows if r["rule"] == "K2")


def test_k2_passes_a_cited_rival_number_that_reruns_equal_and_cuts_one_whose_data_moved():
    con = rival_world()
    pack, _, rerun, _ = build(con)
    units = by_unit(pack)
    posts7, hot = units["posts in 7 days"], units["markets with a significant rise in 14 days"]
    text = "14 posts in seven days came from 2 markets."
    assert k2_row(pack, rerun, text, [posts7, hot])["verdict"] == "pass"
    con.execute("UPDATE core.item_state SET markets_hot = 3 WHERE run_id = ?", [DETECT])
    row = k2_row(pack, rerun, text, [posts7, hot])
    assert row["verdict"] == "cut" and "markets_hot" in row["detail"]
    con.execute("DELETE FROM core.post_observations WHERE post_id = 'p13'")
    assert k2_row(pack, rerun, "14 posts in seven days.", [posts7])["verdict"] == "cut"


def test_k2_passes_a_cited_share_that_reruns_equal():
    pack, _, rerun, _ = build(rival_world())
    burst = by_unit(pack)["share of 7-day posts in the busiest 10 minutes"]
    assert k2_row(pack, rerun, "0.5 of the posts came in ten minutes.", [burst])["verdict"] == "pass"


def test_a_pack_with_no_detect_rival_values_is_the_pack_it_was_before():
    pack, *_ = build(world())
    assert set(pack) == {"evidence", "numbers", "facts"}
    assert [n["unit"] for n in pack["numbers"]] == ["creators in 3 days", "posts in 3 days", "times usual"]


def test_a_failing_rival_query_leaves_the_base_pack_unchanged(capsys):
    con = rival_world()
    base, *_ = build(world())

    class Broken(duck.Client):
        def query(self, sql, job_config=None):
            if "po.observed_at <= @cutoff" in sql:
                raise RuntimeError("window query failed")
            return super().query(sql, job_config)

    pack, *_ = evidence.build_pack(Broken(con), current(con), D, "ZA", core="core", agent="agent")
    units = [n["unit"] for n in pack["numbers"]]
    assert units[:3] == ["creators in 3 days", "posts in 3 days", "times usual"]
    assert not any(n.get("rival_field") in ("posts7", "burst_share") for n in pack["numbers"])
    assert "rival_evidence_read_failed" in capsys.readouterr().err
    assert base["facts"][0] == pack["facts"][0]


# Why-now sentences rendered in code


def got(**kw):
    """The renderer's input: field -> (value, query_id)."""
    return {k: (v, f"q_{k}") for k, v in kw.items()}


ZA = timezone(timedelta(hours=2))
T = datetime(2026, 9, 18, 8, 0, tzinfo=timezone.utc)


def test_bottom_up_names_both_dates_and_both_query_ids():
    [sentence] = evidence.why_now(got(diffusion="bottom_up", small_at=T.isoformat(),
                                      large_at=(T + timedelta(days=2)).isoformat()), ZA)
    assert "2026-09-18" in sentence and "2026-09-20" in sentence
    assert "nano or micro" in sentence and "macro or mega" in sentence
    assert "q_small_at" in sentence and "q_large_at" in sentence and "q_diffusion" in sentence
    assert sentence.index("2026-09-18") < sentence.index("2026-09-20")


def test_top_down_says_the_large_creators_were_not_later():
    [sentence] = evidence.why_now(got(diffusion="top_down", small_at=(T + timedelta(days=2)).isoformat(),
                                      large_at=T.isoformat()), ZA)
    assert "macro or mega" in sentence and "no later than" in sentence
    assert sentence.index("2026-09-18") < sentence.index("2026-09-20")


def test_top_down_on_the_same_instant_is_still_top_down():
    [sentence] = evidence.why_now(got(diffusion="top_down", small_at=T.isoformat(), large_at=T.isoformat()), ZA)
    assert "no later than" in sentence


def test_top_down_with_no_small_creator_post_says_so():
    [sentence] = evidence.why_now(got(diffusion="top_down", small_at=None, large_at=T.isoformat()), ZA)
    assert "2026-09-18" in sentence and "none" in sentence
    assert "q_large_at" in sentence


def test_small_only_names_the_first_small_creator_date_and_no_large_creator_post():
    [sentence] = evidence.why_now(got(diffusion="small_only", small_at=T.isoformat(), large_at=None), ZA)
    assert "2026-09-18" in sentence and "no macro or mega" in sentence


def test_the_date_is_the_markets_local_date():
    late = datetime(2026, 9, 18, 23, 30, tzinfo=timezone.utc)
    [sentence] = evidence.why_now(got(diffusion="small_only", small_at=late.isoformat(), large_at=None), ZA)
    assert "2026-09-19" in sentence and "2026-09-18" not in sentence


@pytest.mark.parametrize("inputs", [
    dict(),
    dict(diffusion="bottom_up", small_at=T.isoformat()),
    dict(diffusion="bottom_up", large_at=T.isoformat()),
    dict(diffusion="small_only", small_at=None, large_at=None),
    dict(diffusion="small_only", small_at=T.isoformat(), large_at=T.isoformat()),
    dict(diffusion="top_down", small_at=None, large_at=None),
    dict(diffusion="bottom_up", small_at=(T + timedelta(days=1)).isoformat(), large_at=T.isoformat()),
    dict(diffusion="top_down", small_at=T.isoformat(), large_at=(T + timedelta(days=1)).isoformat()),
    dict(diffusion="sideways", small_at=T.isoformat(), large_at=T.isoformat()),
    dict(small_at=T.isoformat(), large_at=(T + timedelta(days=1)).isoformat()),
    dict(diffusion="bottom_up", small_at="not a time", large_at=T.isoformat()),
])
def test_no_diffusion_sentence_when_an_input_is_missing_or_the_label_disagrees_with_the_times(inputs):
    assert evidence.why_now(got(**inputs), ZA) == []


def test_lead_market_sentence_needs_both_the_market_and_the_market_count():
    [sentence] = evidence.why_now(got(lead_market="NG", markets_hot=2), ZA)
    assert "NG" in sentence and "2 markets" in sentence and "q_lead_market" in sentence
    assert "q_markets_hot" in sentence
    assert evidence.why_now(got(lead_market="NG"), ZA) == []
    assert evidence.why_now(got(markets_hot=2), ZA) == []
    assert evidence.why_now(got(lead_market=None, markets_hot=2), ZA) == []
    assert evidence.why_now(got(lead_market="NG", markets_hot=0), ZA) == []


def test_at_most_two_sentences_in_a_fixed_order():
    sentences = evidence.why_now(got(diffusion="small_only", small_at=T.isoformat(), large_at=None,
                                     lead_market="KE", markets_hot=3), ZA)
    assert len(sentences) == 2
    assert "nano or micro" in sentences[0] and "KE" in sentences[1]


def test_the_facts_carry_the_sentences_after_the_first_seen_line_and_name_real_query_ids():
    con = rival_world()
    duck.load(con, "agent.runs", [run("aggregate", day(6))])
    duck.load(con, "core.item_daily", [item_daily("i1", day(6), 2)])
    pack, *_ = build(con)
    facts = pack["facts"]
    diffusion = next(f for f in facts if "nano or micro" in f)
    market = next(f for f in facts if "2 markets" in f)
    assert diffusion.count("2026-09-15") == 1 and "2026-09-19" in diffusion
    ids = {n["query_id"] for n in pack["numbers"] + pack["pinned"]}
    for fact in (diffusion, market):
        quoted = set(re.findall(r"q_\w+", fact))
        assert quoted and quoted <= ids
    assert facts.index(diffusion) < facts.index(market)
    assert facts.index(diffusion) > next(i for i, f in enumerate(facts) if f.startswith("First seen"))


def test_no_sentence_reaches_the_facts_when_the_item_has_no_detect_rival_values():
    pack, *_ = build(world())
    assert not any("nano or micro" in f or "markets" in f for f in pack["facts"])

def test_a_claim_citing_a_rival_number_carries_the_pinned_entry_without_the_internal_field_name():
    from core.brief import explain

    pack, *_ = build(rival_world())
    ids = {n["query_id"]: f"n{i}" for i, n in enumerate(pack["numbers"], 1)}
    burst = by_unit(pack)["share of 7-day posts in the busiest 10 minutes"]
    draft = {"explanation": "x", "claims": [{"id": "c1", "text": "t", "label": "observed", "kind": "observation",
                                             "evidence_ids": [], "quotes": [], "number_ids": [ids[burst["query_id"]]]}]}
    [entry] = explain._answer(draft, pack)["claims"][0]["numbers"]
    assert entry == {k: v for k, v in burst.items() if k not in ("rival_field", "cutoff")}
