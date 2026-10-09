"""The rules W8-DEC-01a leaves alone: MIN_EVIDENCE, the 2-local rule, G1 to G5b, G8, G10 (and the K rules and the critic,
which sit behind the brief's explanation and are not touched by this change).

Each case runs the brief's own gate (job._gate) on the same candidate twice, once admitted under the v1 pack scope and
once under locality_v2.1 with a row that passes, and requires the same decision. Only the locality rule itself (G6) is
allowed to differ between the two, and that is tested in test_locality_gate.py and test_locality_authority.py."""

import pytest

from core.brief import job
from core.brief.specificity import MIN_EVIDENCE
from core.brief.tests.test_brief_news_scope import located, news
from core.trust.locality import V2_BASIS
from core.trust.tests.test_locality_gate import lrow

CTX = {"valid_days": [True, True, True], "lane_classes": ["unbiased_rank"], "campaign_hashtags": [], "political": False,
       "corroborated_unbiased": False}


def candidate(evidence, basis, *, ctx=None, **row):
    base = {"item_id": "x", "state": "rising", "map_status": "active", "nameless": False, "authenticity": "clear",
            "sponsored_share": 0, "locality_basis": basis}
    if basis == V2_BASIS:
        base.update(lrow(20, 20))                               # local, label local: passes G6 with no flag
    else:
        base.update(market_scope="market", market_posts7=12, total_posts7=12, market_share7=1.0)
    base.update(row)
    return {"market": "ZA", "ctx": {**CTX, **(ctx or {})}, "pack": {"evidence": evidence}, "row": base}


def decide(evidence, basis, passed=True, **kw):
    cand = candidate(evidence, basis, **kw)
    return job._gate(cand, passed), cand


def shape(decision, cand):
    return (decision.publish, decision.where, decision.rule, decision.reason, decision.numbers_only,
            cand.get("held_reason"), bool(cand.get("floor_held")))


CASES = {
    "all news posts": dict(evidence=news(5)),
    "news and one local post": dict(evidence=news(4) + located(1)),
    "two local posts only": dict(evidence=located(2)),
    "three local posts": dict(evidence=located(3)),
    "news and two local posts": dict(evidence=news(3) + located(2)),
    "paid-led": dict(evidence=located(3), sponsored_share=0.6),
    "campaign hashtag": dict(evidence=located(3), hashtags=["#acme"], ctx={"campaign_hashtags": ["acme"]}),
    "political, not corroborated": dict(evidence=located(3), ctx={"political": None}),
    "invalid data day": dict(evidence=located(3), ctx={"valid_days": [True, False, True]}),
    "no measured lane": dict(evidence=located(3), ctx={"lane_classes": ["search_presence"]}),
    "seasonal": dict(evidence=located(3), state="seasonal"),
    "generic tag": dict(evidence=located(3), map_status="generic"),
    "no readable name": dict(evidence=located(3), nameless=True),
    "likely coordinated": dict(evidence=located(3), authenticity="likely_coordinated"),
    "explanation failed": dict(evidence=located(3), passed=False),
}


@pytest.mark.parametrize("name", list(CASES))
def test_the_decision_is_the_same_under_v1_and_under_locality_v2_when_the_locality_row_passes(name):
    spec = dict(CASES[name])
    evidence, passed = spec.pop("evidence"), spec.pop("passed", True)
    v1 = shape(*decide(evidence, "v1", passed, **spec))
    v2 = shape(*decide(evidence, V2_BASIS, passed, **spec))
    assert v1 == v2


def test_the_reserved_rules_still_hold_what_they_held():
    """The expected decisions, written out, so that two copies of the same change could not agree with each other."""
    def rule(case, basis=V2_BASIS):
        spec = dict(CASES[case])
        evidence, passed = spec.pop("evidence"), spec.pop("passed", True)
        decision, cand = decide(evidence, basis, passed, **spec)
        return decision.rule, decision.where, decision.reason, decision.numbers_only

    assert MIN_EVIDENCE == 3
    assert rule("all news posts")[2] == "Fewer than 2 supported local posts"
    assert rule("news and one local post")[2] == "Fewer than 2 supported local posts"
    assert rule("two local posts only")[2] == "Fewer than 3 posts 42 can show"
    assert rule("three local posts")[:2] == (None, "today")
    assert rule("news and two local posts")[:2] == (None, "today")
    assert rule("paid-led")[0] == "G5" and rule("campaign hashtag")[0] == "G5b"
    assert rule("political, not corroborated")[0] == "G4b" and rule("invalid data day")[0] == "G1"
    assert rule("no measured lane")[0] == "G3" and rule("seasonal")[:2] == ("G8", "moments")
    assert rule("explanation failed")[0] == "G10" and rule("explanation failed")[3] is True
    assert rule("generic tag")[2] == "Platform-generic tag" and rule("likely coordinated")[0] == "G4"
