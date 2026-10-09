"""A candidate admitted under locality_v2.1 whose retained locality row cannot be read (C4 v3 sections 8.4 and 11.2).

It is held as unreadable evidence the way a failed scope read is held: no rule, the reason "Evidence could not be
read", held_reason data_issue and scope_error set, and a market scope of none, never global. It is not an invalid-day
hold, so it takes one of the market's judged slots and the brief does not go on preparing replacements for it. With
every row of a market unreadable (the whole-step failure of section 8.4) the brief prepares CANDIDATES rows per market,
not the whole pool."""

from datetime import date

import pytest

from core.brief import job
from core.trust.locality import V2_BASIS

D = date(2026, 10, 7)
GOOD_CTX = {"valid_days": [True, True, True], "lane_classes": ["unbiased_rank"], "campaign_hashtags": [],
            "political": False, "corroborated_unbiased": False}


def v2_rows(n, **over):
    return [{"item_id": f"i{k:03d}", "label": f"topic {k}", "canonical_key": f"topic:{k}", "kind": "topic",
             "map_kind": "topic", "state": "rising", "map_status": "active", "authenticity": "clear",
             "sponsored_share": 0, "locality_basis": V2_BASIS, "eligible": True, "run_id": "detect-x", **over}
            for k in range(n)]


@pytest.fixture
def stubbed(monkeypatch):
    """The real _prepare and _gate, with the reads behind them stubbed. Returns the list of prepared item ids."""
    prepared = []
    real = job._prepare

    def counting(client, d, market, row, **kw):
        prepared.append(row["item_id"])
        return real(client, d, market, row, **kw)

    monkeypatch.setattr(job, "_prepare", counting)
    monkeypatch.setattr(job, "read_market_scope", lambda *a, **k: {
        "market_scope": "market", "market_posts7": 9, "total_posts7": 10, "market_share7": 0.9})
    monkeypatch.setattr(job, "build_pack", lambda *a, **k: ({"evidence": [], "numbers": [], "facts": []}, None, None))
    monkeypatch.setattr(job, "post_set", lambda *a, **k: set())
    monkeypatch.setattr(job, "creator_names", lambda client, rows, hidden, core, agent: rows)
    return prepared


def candidates(monkeypatch, rows, ctx=GOOD_CTX):
    monkeypatch.setattr(job, "_candidate_rows", lambda client, d, market, core, agent, receipt, failed=None:
                        [dict(r) for r in rows] if market == "NG" else [])
    return job._candidates(None, D, build_ctx=lambda *a, **k: dict(ctx), campaign_hashtags=[],
                           political_terms={m: [] for m in job.MARKETS}, core="core", agent="agent", hidden=(set(), set(), {}))


def test_every_row_unreadable_prepares_ten_candidates_not_the_whole_pool(monkeypatch, stubbed):
    out = candidates(monkeypatch, v2_rows(90))
    assert len(stubbed) == job.CANDIDATES == 10
    assert len(out["NG"]) == 10


def test_a_row_with_a_contradicting_locality_row_is_held_the_same_way(monkeypatch, stubbed):
    contradicting = {"lrow_metric_version": "locality_v2.1", "lrow_population_posts": 8, "lrow_known_posts": 8,
                     "lrow_local_posts": 0, "lrow_foreign_posts": 8, "lrow_unknown_posts": 0,
                     "lrow_status": "local", "lrow_local_share": 0.0}
    out = candidates(monkeypatch, v2_rows(90, **contradicting))
    assert len(stubbed) == 10
    assert {(c["decision"].rule, c["held_reason"], c["scope_error"]) for c in out["NG"]} == {(None, "data_issue", True)}


def test_an_unreadable_row_is_held_with_the_data_issue_decision_and_no_scope(monkeypatch, stubbed):
    [cand] = candidates(monkeypatch, v2_rows(1))["NG"]
    decision = cand["decision"]
    assert (decision.rule, decision.reason, decision.publish, decision.where) == (
        None, "Evidence could not be read", False, "held_back")
    assert cand["held_reason"] == "data_issue" and cand["scope_error"] is True
    assert cand["row"]["market_scope"] is None and cand["row"]["market_scope_basis"] == V2_BASIS
    assert cand["row"]["locality_v2"]["status"] == "unreadable"
    assert not job._held_g1(cand)


def test_an_unreadable_row_on_an_invalid_day_is_still_the_g1_hold_and_is_replaced(monkeypatch, stubbed):
    bad_day = {**GOOD_CTX, "valid_days": [True, False, True]}
    out = candidates(monkeypatch, v2_rows(90), ctx=bad_day)
    assert len(stubbed) == 90
    assert {c["decision"].rule for c in out["NG"]} == {"G1"}


def test_a_readable_row_is_unchanged_by_the_hold_rule(monkeypatch, stubbed):
    local = {"lrow_metric_version": "locality_v2.1", "lrow_population_posts": 20, "lrow_known_posts": 20,
             "lrow_local_posts": 20, "lrow_foreign_posts": 0, "lrow_unknown_posts": 0,
             "lrow_status": "local", "lrow_local_share": 1.0}
    [cand] = candidates(monkeypatch, v2_rows(1, **local))["NG"]
    assert cand["scope_error"] is False and cand["row"]["market_scope"] == "market"
    assert cand["held_reason"] != "data_issue"         
