"""The replay scorer, on hand-built results. Every expected value here is written out from ASK-POWER-PLAN section 1
and the review and score_run vocabularies, never read from a fixture or from the scorer's own constants."""

import copy
import json
from datetime import date

import pytest

from core.eval import ask_replay_score as score
from core.eval import quality_score

HASH = "sha256:" + "a" * 64


def post(pid, platform, handle, days_ago=1, text=None, market="ZA"):
    return {"id": pid, "platform": platform, "handle": handle, "url": f"https://example.test/{pid}",
            "posted_at": f"2026-10-{10 - days_ago + 1:02d}T10:00:00+02:00", "market": market,
            "text": text or f"post {pid} about the topic", "engagement": {"views": 10}, "flags": []}


def claim(cid, ids, quotes=(), label="observed", kind="observation", numbers=(), text=None):
    return {"id": cid, "text": text or f"Claim {cid}.", "label": label, "kind": kind, "evidence_ids": list(ids),
            "quotes": [{"evidence_id": e, "text": t} for e, t in quotes],
            "numbers": [{"value": v, "unit": "posts", "query_id": q, "run_id": "r_1", "result_hash": HASH}
                        for v, q in numbers]}


def answer(claims, evidence, status="complete", short="Short answer.", gaps=()):
    return {"status": status, "as_of": "2026-10-11T08:00:00+02:00", "short_answer": short, "claims": claims,
            "evidence": evidence, "so_what": [], "watch_next": [], "gaps": list(gaps)}


def spread(posts, creators, platforms):
    """posts evidence records over the given number of creators and platforms (a creator keeps one platform), one
    claim citing all of them."""
    names = ["tiktok", "x", "instagram", "youtube"][:platforms]
    ev = [post(f"p{i}", names[(i % creators) % platforms], f"@c{i % creators}") for i in range(posts)]
    return answer([claim("c1", [e["id"] for e in ev])], ev)


def test_cited_counts_posts_creators_and_platforms_once_each():
    ev = [post("a", "tiktok", "@Zed"), post("b", "tiktok", "zed"), post("c", "x", "@amy"),
          post("tt_comment_9", "tiktok", "@zed")]
    ev[3]["parent_id"] = "a"
    got = score.cited(answer([claim("c1", ["a", "b"]), claim("c2", ["c", "tt_comment_9"])], ev))
    assert got["ids"] == ["a", "b", "c", "tt_comment_9"]
    assert got["posts"] == 4
    assert got["creators"] == 2  # @Zed and zed are one creator on TikTok
    assert got["platforms"] == 2
    assert got["comment_claims"] == 1  # only c2 rests on a comment


@pytest.mark.parametrize("posts,creators,platforms,passes", [
    (8, 6, 2, True), (7, 6, 2, False), (8, 5, 2, False), (8, 6, 1, False), (30, 12, 3, True)])
def test_the_spread_measure_is_the_plan_eight_six_two(posts, creators, platforms, passes):
    got = {m["name"]: m for m in score.measures(result_for(spread(posts, creators, platforms)))}
    assert got["cited_spread"]["result"] == ("pass" if passes else "fail")


def result_for(ans, **over):
    base = {"id": "X-01", "family": "now", "market": "ZA", "tier": "T1", "answer": ans, "run": run_object(),
            "on_topic_ids": [f"p{i}" for i in range(150)], "claim_checks": [], "events": [], "hold_usd": 4.916}
    base.update(over)
    return base


def run_object(**over):
    base = {"run_id": "r_1", "tier": "T1", "mode": "live", "credits": 4.0, "model_usd": 0.5, "seconds": 0.2,
            "source_status": [], "posts": 150, "platforms": 2}
    base.update(over)
    return base


def test_on_topic_depth_needs_one_hundred_stored_posts():
    ans = spread(8, 6, 2)
    at = {m["name"]: m for m in score.measures(result_for(ans, on_topic_ids=[f"p{i}" for i in range(100)]))}
    under = {m["name"]: m for m in score.measures(result_for(ans, on_topic_ids=[f"p{i}" for i in range(99)]))}
    assert at["on_topic_posts"]["result"] == "pass" and at["on_topic_posts"]["value"] == 100
    assert under["on_topic_posts"]["result"] == "fail" and under["on_topic_posts"]["value"] == 99


def test_live_platforms_count_only_when_the_stored_side_is_thin():
    ans = spread(8, 6, 2)
    plenty = {m["name"]: m for m in score.measures(result_for(ans))}
    assert plenty["live_platforms"]["result"] == "not_applicable"
    status = [{"platform": "tiktok", "route": "tiktok/search/top", "status": "ok", "items": 5},
              {"platform": "twitter", "route": "twitter/search/tweets", "status": "ok", "items": 5},
              {"platform": "tiktok", "route": "tiktok/post/comments", "status": "ok", "items": 9},
              {"platform": "search", "route": "search/everywhere", "status": "ok", "items": 9},
              {"platform": "threads", "route": "threads/search", "status": "rate_limited", "items": 0}]
    thin = {m["name"]: m for m in score.measures(
        result_for(ans, on_topic_ids=["p1"], run=run_object(source_status=status)))}
    # comments are not a search, a multi-platform route names no platform, and a refused call searched nothing
    assert thin["live_platforms"]["result"] == "fail" and thin["live_platforms"]["value"] == 2
    assert thin["live_platforms"]["platforms"] == ["tiktok", "x"]
    status.append({"platform": "youtube", "route": "youtube/search/advanced", "status": "ok", "items": 2})
    three = {m["name"]: m for m in score.measures(
        result_for(ans, on_topic_ids=["p1"], run=run_object(source_status=status)))}
    assert three["live_platforms"]["result"] == "pass" and three["live_platforms"]["value"] == 3
    hashtag = [{"platform": "tiktok", "route": "tiktok/search/hashtag", "status": "partial", "items": 4}]
    one = {m["name"]: m for m in score.measures(
        result_for(ans, on_topic_ids=["p1"], run=run_object(source_status=hashtag)))}
    assert one["live_platforms"]["platforms"] == ["tiktok"]  # a hashtag search is a search; partial still answered


def test_comment_claims_are_asked_of_why_questions_only():
    ev = [post("a", "tiktok", "@a"), post("tiktok_comment_1", "tiktok", "@b"), post("tiktok_comment_2", "tiktok", "@c")]
    for e in ev[1:]:
        e["parent_id"] = "a"
    ans = answer([claim("c1", ["a"]), claim("c2", ["tiktok_comment_1"]), claim("c3", ["tiktok_comment_2"])], ev)
    why = {m["name"]: m for m in score.measures(result_for(ans, family="why"))}
    now = {m["name"]: m for m in score.measures(result_for(ans, family="now"))}
    assert why["comment_claims"]["result"] == "pass" and why["comment_claims"]["value"] == 2
    assert now["comment_claims"]["result"] == "not_applicable"
    one = answer(ans["claims"][:2], ev)
    assert {m["name"]: m for m in score.measures(result_for(one, family="why"))}["comment_claims"]["result"] == "fail"


def test_cause_first_is_left_to_a_reader_and_shows_what_to_read():
    ans = spread(8, 6, 2)
    ans["short_answer"] = "Water cuts took over."
    ans["claims"][0]["text"] = "Posts blame the municipality."
    why = {m["name"]: m for m in score.measures(result_for(ans, family="why"))}
    assert why["cause_first"]["result"] == "needs_reading"
    assert why["cause_first"]["read"] == {"short_answer": "Water cuts took over.",
                                          "first_claim": "Posts blame the municipality."}
    now = {m["name"]: m for m in score.measures(result_for(ans, family="now"))}
    assert now["cause_first"]["result"] == "not_applicable"


def test_money_and_credit_ceilings_are_the_plan_t1_figures():
    ans = spread(8, 6, 2)

    def got(**run):
        return {m["name"]: m for m in score.measures(result_for(ans, run=run_object(**run)))}

    assert got(model_usd=1.5)["model_usd"]["result"] == "pass"
    assert got(model_usd=1.51)["model_usd"]["result"] == "fail"
    assert got(credits=45)["credits"]["result"] == "pass"
    assert got(credits=45.5)["credits"]["result"] == "fail"
    assert got(model_usd=4.916)["within_hold"]["result"] == "pass"
    assert got(model_usd=4.917)["within_hold"]["result"] == "fail"
    assert got()["seconds"]["result"] == "live_only"


def test_a_t2_result_gets_the_t2_ceilings():
    ans = spread(8, 6, 2)
    run = run_object(tier="T2", credits=150, model_usd=4.0)
    got = {m["name"]: m for m in score.measures(result_for(ans, tier="T2", run=run, hold_usd=11.33))}
    assert got["model_usd"]["result"] == "pass" and got["credits"]["result"] == "pass"
    run = run_object(tier="T2", credits=151, model_usd=4.01)
    got = {m["name"]: m for m in score.measures(result_for(ans, tier="T2", run=run, hold_usd=11.33))}
    assert got["model_usd"]["result"] == "fail" and got["credits"]["result"] == "fail"


def test_outcomes_name_kept_cut_and_downgraded_claims_and_the_rules_that_cut():
    rows = [
        {"claim_id": "c1", "rule": "K1", "verdict": "pass"}, {"claim_id": "c1", "rule": "K5", "verdict": "pass"},
        {"claim_id": "c2", "rule": "K1", "verdict": "cut", "reason": "quote not verbatim in stored record p1"},
        {"claim_id": "c2", "rule": "K10", "verdict": "cut"},
        {"claim_id": "c3", "rule": "K5", "verdict": "downgrade"}, {"claim_id": "c3", "rule": "K1", "verdict": "pass"},
        {"claim_id": "c4", "rule": "K4", "verdict": "cut"}, {"claim_id": "c4", "rule": "K1", "verdict": "pass"},
        {"claim_id": "short_answer", "rule": "K10", "verdict": "pass"},
    ]
    events = [{"event": "claim", "claim": {"id": "c2"}, "check": "cut", "reason": "Quote not found (K1)"},
              {"event": "claim", "claim": {"id": "c4"}, "check": "cut", "reason": "Not supported (K4)"},
              {"event": "claim", "claim": {"id": "c1"}, "check": "verified", "reason": None}]
    ev = [post("p1", "x", "@a")]
    ans = answer([claim("c1", ["p1"]), claim("c3", ["p1"])], ev)
    got = score.outcomes(ans, rows, events)
    assert got["drafted"] == 4 and got["kept"] == ["c1", "c3"] and got["downgraded"] == ["c3"]
    assert got["cut"] == [{"claim_id": "c2", "rules": ["K1", "K10"], "reason": "Quote not found (K1)"},
                          {"claim_id": "c4", "rules": ["K4"], "reason": "Not supported (K4)"}]


def test_a_loosened_gate_shows_as_a_breach_because_the_independent_checks_still_fail():
    ev = [post("p1", "x", "@a", text="the log drum is everything")]
    good = answer([claim("c1", ["p1"], quotes=[("p1", "log drum is everything")])], ev)
    bad = copy.deepcopy(good)
    bad["claims"][0]["quotes"][0]["text"] = "the log drum is nothing"  # not in the post
    results = score.deterministic_checks([(good, {"id": "A", "window_days": 7, "expected_answerability": "answerable",
                                                  "min_evidence": {}, "markets": ["ZA"]}),
                                          (bad, {"id": "B", "window_days": 7, "expected_answerability": "answerable",
                                                 "min_evidence": {}, "markets": ["ZA"]})])
    assert results[0]["citation_integrity"]["pass"] is True
    assert results[1]["citation_integrity"]["pass"] is False
    assert "not found verbatim" in results[1]["citation_integrity"]["reason"]
    assert score.gate_leaks(results[0]) == [] and score.gate_leaks(results[1]) == ["citation_integrity"]


def test_an_age_lens_that_survived_the_gate_is_a_leak():
    ev = [post("p1", "x", "@a")]
    ans = answer([claim("c1", ["p1"], text="Gen Z creators are driving it.")], ev)
    (res,) = score.deterministic_checks([(ans, {"id": "A", "window_days": 7, "expected_answerability": "answerable",
                                                "min_evidence": {}, "markets": ["ZA"]})])
    assert score.gate_leaks(res) == ["no_age_lens"]


def test_the_depth_floor_failing_is_a_score_and_not_a_leak():
    ev = [post("p1", "x", "@a")]
    ans = answer([claim("c1", ["p1"])], ev)
    (res,) = score.deterministic_checks([(ans, {"id": "A", "window_days": 7, "expected_answerability": "answerable",
                                                "min_evidence": {"posts": 9, "platforms": 2, "creators": 6},
                                                "markets": ["ZA"]})])
    assert res["evidence_floor"]["pass"] is False
    assert score.gate_leaks(res) == []


def test_review_flags_rank_claims_by_the_weekly_review_risk_and_mark_non_english():
    ev = [post("p1", "x", "@a", market="KE", text="Manze niaje wasee, hii imeisha")]
    c1 = claim("c1", ["p1"], quotes=[("p1", "Manze niaje")], text="It spread because of the match.", numbers=[(3, "q_1")])
    c2 = claim("c2", ["p1"], text="A plain claim.")
    got = score.review_flags(answer([c2, c1], ev), "KE")
    assert got["scope"] == "evidence_only"
    assert [r["claim_id"] for r in got["read_first"]] == ["c1", "c2"]
    assert got["read_first"][0]["risk"] == 3 and got["read_first"][0]["non_english"] is True
    assert got["read_first"][1]["risk"] == 0
    assert score.review_flags(answer([c2], ev), "ZA")["scope"] == "full"


def test_score_run_view_counts_what_it_can_and_names_what_it_cannot_grade():
    ev = [post("p1", "x", "@a", text="the log drum is everything")]
    good = answer([claim("c1", ["p1"], quotes=[("p1", "log drum is everything")], numbers=[(3, "q_1")])], ev)
    vars_ = {"id": "A-01", "family": "now", "markets": ["ZA"], "window_days": 7,
             "expected_answerability": "answerable", "min_evidence": {}}
    (det,) = score.deterministic_checks([(good, vars_)])
    view = score.score_run_view([("A-01", vars_, good, det)])
    assert view["trust"]["citation_integrity"] == {"passed": 1, "checked": 1}
    assert view["trust"]["number_pinning"] == {"pinned": 1, "numbers": 1}
    assert view["fabricated_citations"] == [] and view["false_full_refusals"] == []
    assert view["ungraded"] == ["freshness", "grounding", "hard_fail_screen", "honesty", "so_what", "specificity",
                                "usefulness"]
    assert view["failures"] == {"A-01": []}


def test_a_replay_folder_is_never_read_as_a_weekly_score(tmp_path):
    (tmp_path / "replay-2026-10-11.json").write_text(json.dumps({"run": {"valid": True, "date": "2026-10-11"}}))
    (tmp_path / "replay-WHY-01.json").write_text("{}")
    assert quality_score.latest_run(tmp_path, date(2026, 10, 5)) is None
    assert score.score_file_names(tmp_path) == []


def test_the_report_marks_each_score_as_meaningful_offline_or_live_only():
    kinds = score.MEANING
    assert kinds["trust_gate"] == "offline" and kinds["privacy"] == "offline" and kinds["hold"] == "offline"
    assert kinds["on_topic_posts"] == "fixture" and kinds["cited_spread"] == "fixture"
    assert kinds["rubric_dimensions"] == "live" and kinds["seconds"] == "live" and kinds["cause_first"] == "live"
    assert set(kinds.values()) == {"offline", "fixture", "live"}


def test_a_result_with_no_tier_of_its_own_takes_the_tier_of_its_run():
    ans = spread(8, 6, 2)
    result = result_for(ans, run=run_object(tier="T2", credits=150, model_usd=4.0), hold_usd=11.33)
    del result["tier"]
    got = {m["name"]: m for m in score.measures(result)}
    assert got["model_usd"]["target"] == 4.0 and got["credits"]["target"] == 150


def receipt(rows, **over):
    from core.agent.context import result_hash

    return {"rows": rows, "row_count": len(rows), "result_hash": result_hash(rows), **over}


def test_a_number_is_pinned_only_when_its_hash_is_recomputed_from_the_receipt_rows():
    from core.agent.context import result_hash

    rows = [{"posts": 6, "authors": 5}]
    good = {"value": 6, "unit": "posts", "query_id": "q_1", "run_id": "r_1", "result_hash": result_hash(rows)}
    ans = answer([{**claim("c1", ["p1"]), "numbers": [good]}], [post("p1", "x", "@a")])
    assert score.number_receipts(ans, {"q_1": receipt(rows)}) == {"numbers": 1, "recomputed": 1, "problems": []}
    # a hash that is well formed but is not the hash of the rows is the case a shape check lets through
    forged = {**good, "result_hash": "sha256:" + "b" * 64}
    ans = answer([{**claim("c1", ["p1"]), "numbers": [forged]}], [post("p1", "x", "@a")])
    got = score.number_receipts(ans, {"q_1": receipt(rows)})
    assert got["recomputed"] == 0 and len(got["problems"]) == 1 and "q_1" in got["problems"][0]
    # a query with no receipt, and rows the receipt cut short (the hash covers every row), cannot be recomputed
    assert "no receipt" in score.number_receipts(ans, {})["problems"][0]
    cut = receipt(rows, row_count=60)
    assert "cut short" in score.number_receipts(ans, {"q_1": cut})["problems"][0]
