from datetime import datetime, timedelta, timezone

import pytest

from core.trust.claims import allowed_label, check_answer, place_fault, place_support, source_market

SAST = timezone(timedelta(hours=2))
START = datetime(2026, 9, 21, 0, 0, tzinfo=SAST)
END = datetime(2026, 9, 28, 23, 59, tzinfo=SAST)


def record(rid, platform="tiktok", handle="@creator", *, market=None, source_market=None, flags=None, text="A post"):
    return {
        "id": rid,
        "platform": platform,
        "handle": handle,
        "url": f"https://example.test/{rid}",
        "posted_at": "2026-09-26T19:40:00+02:00",
        "market": market,
        "source_market": source_market,
        "text": text,
        "engagement": {},
        "flags": flags if flags is not None else (["market_assumed"] if market is None else []),
    }


def run(text, records, *, label="single_source", market="KE", quotes=None):
    fillers = [record(f"f{i}", "youtube", f"@filler{i}") for i in range(2)]
    claims = [{
        "id": "c1",
        "text": text,
        "label": label,
        "kind": "observation",
        "evidence_ids": [r["id"] for r in records],
        "quotes": quotes or [],
        "numbers": [],
    }]
    claims.extend({
        "id": f"f{i}",
        "text": "Creators post a step",
        "label": "single_source",
        "kind": "observation",
        "evidence_ids": [r["id"]],
        "quotes": [],
        "numbers": [],
    } for i, r in enumerate(fillers))
    answer = {
        "status": "complete",
        "short_answer": "",
        "claims": claims,
        "evidence": records + fillers,
        "so_what": [],
        "watch_next": [],
        "gaps": [],
        "context": "",
    }
    result, checks = check_answer(answer, window_start=START, window_end=END, market=market)
    rows = {r["rule"]: r for r in checks if r["claim_id"] == "c1"}
    kept = next((c for c in result["claims"] if c["id"] == "c1"), None)
    return rows, kept


def test_source_market_accepts_only_a_known_explicit_market_code():
    assert source_market({"source_market": "ke", "route": "kenya"}) == "KE"
    assert source_market({"source_market": "UG"}) is None
    assert source_market({"source_market": 17}) is None
    assert source_market({"market": "KE", "route": "kenya"}) is None
    assert source_market({}) is None


def test_place_support_prefers_actual_location_then_explicit_source():
    kenya_feed = record("feed", source_market="KE")
    kenya_located = record("located", market="KE", source_market="KE")
    nigeria_feed = record("ng", source_market="NG")
    assert place_support("Seen in Kenya feeds", [kenya_feed]) == {"KE": "source"}
    assert place_support("Kenyan creators post", [kenya_feed, kenya_located]) == {"KE": "located"}
    assert place_support("Seen in Kenya feeds", [nigeria_feed]) == {"KE": None}
    assert place_support("Creators post a step", [nigeria_feed]) == {}


@pytest.mark.parametrize("text", [
    "Seen in Kenya's feeds, creators post the step",
    "Kenya’s feeds surface the step",
    "The step is showing up in Kenyan feeds",
    "The step is on Kenya trending",
])
def test_k3_accepts_explicit_feed_scoped_source_evidence(text):
    rows, kept = run(text, [record("ke-feed", source_market="KE")])
    assert rows["K3"]["verdict"] == "pass"
    assert kept is not None


@pytest.mark.parametrize("text", [
    "Seen in Kenya feeds, the step appears",
    "Kenya’s feeds surface the step",
])
@pytest.mark.parametrize("source", [None, "NG"])
def test_k3_requires_matching_source_market_for_feed_wording_even_when_located(text, source):
    evidence = record("located", market="KE", source_market=source, flags=[])
    rows, kept = run(text, [evidence])
    assert rows["K3"]["verdict"] == "cut", (text, source)
    assert "source market" in rows["K3"]["detail"]
    assert kept is None


@pytest.mark.parametrize("text", ["Seen in Kenya feeds, the step appears", "Kenya’s feeds surface the step"])
def test_matching_feed_identity_keeps_located_evidence_at_its_normal_confidence(text):
    records = [
        record("located", "tiktok", "@one", market="KE", source_market="KE"),
        record("source", "tiktok", "@two", source_market="KE"),
    ]
    rows, kept = run(text, records, label="observed")
    assert rows["K3"]["verdict"] == "pass"
    assert rows["K5"]["verdict"] == "pass"
    assert kept["label"] == "observed"


def test_physical_place_claims_still_use_located_evidence_without_source_identity():
    rows, kept = run("Kenyan creators post the step", [record("located", market="KE", flags=[])])
    assert rows["K3"]["verdict"] == "pass"
    assert rows["K5"]["verdict"] == "pass"
    assert kept["label"] == "single_source"


@pytest.mark.parametrize("text", [
    "Kenya embraced the trend, seen in Kenya feeds",
    "Nigeria embraced the trend, seen in Nigeria feeds",
    "Nigerians love the trend, seen in Nigerian feeds",
    "Nigerian creators post the trend, seen in Nigeria feeds",
    "People in Nigeria post the trend, seen in Nigeria feeds",
    "The trend is big in Kenya, seen in Kenya feeds",
])
def test_k3_rejects_source_market_mentions_left_outside_feed_scope(text):
    code = "NG" if "Nigeria" in text or "Niger" in text else "KE"
    rows, kept = run(text, [record("feed", source_market=code)], market=code)
    assert rows["K3"]["verdict"] == "cut", text
    assert rows["K3"]["detail"] == place_fault(text, [record("feed", source_market=code)])
    assert kept is None


@pytest.mark.parametrize("text,source,market", [
    ("NG embraced the trend, seen in Nigeria's feeds", "NG", "NG"),
    ("ZA embraced the trend, seen in South Africa's feeds", "ZA", "ZA"),
    ("KOT embraced the trend, seen in Kenya's feeds", "KE", "KE"),
])
def test_uppercase_place_codes_survive_feed_scope_removal(text, source, market):
    rows, kept = run(text, [record("feed", source_market=source)], market=market)
    assert rows["K3"]["verdict"] == "cut", text
    assert kept is None


@pytest.mark.parametrize("text", [
    "Kenyans post the step",
    "Kenyan creators post the step",
    "Creators in Nairobi post the step",
    "People in Kenya post the step",
    "The step is big in Kenya",
])
def test_k3_rejects_unscoped_or_people_claims_on_source_only_evidence(text):
    rows, kept = run(text, [record("ke-feed", source_market="KE")])
    assert rows["K3"]["verdict"] == "cut", text
    assert kept is None


def test_plain_feed_scope_passes_and_other_market_source_never_supports_it():
    text = "Seen in Nigeria feeds"
    rows, kept = run(text, [record("ng-feed", source_market="NG")], market="NG")
    assert rows["K3"]["verdict"] == "pass"
    assert kept is not None
    rows, kept = run(text, [record("ke-feed", source_market="KE")], market="NG")
    assert rows["K3"]["verdict"] == "cut"
    assert kept is None


def test_foreign_actual_location_still_cuts_even_when_source_market_matches():
    rows, kept = run(
        "Seen in Kenya feeds",
        [record("foreign", market="NG", source_market="KE", flags=[])],
        market="KE",
    )
    assert rows["K3"]["verdict"] == "cut"
    assert "located" in rows["K3"]["detail"]
    assert kept is None


def test_verified_quote_is_exempt_from_residual_place_check_only_when_feed_scope_is_outside_it():
    text = 'Seen in Kenya feeds, one caption says "Kenyans love this step"'
    assert place_fault(text, [record("ke-feed", source_market="KE")]) is not None
    assert place_fault(text, [record("ke-feed", source_market="KE")], {"Kenyans love this step"}) is None
    assert place_fault('One caption says "Seen in Kenya feeds"', [record("ke-feed", source_market="KE")],
                       {"Seen in Kenya feeds"}) is not None


def test_source_only_named_place_costs_exactly_one_k5_step():
    records = [
        record("one", "tiktok", "@one", source_market="KE"),
        record("two", "tiktok", "@two", source_market="KE"),
    ]
    rows, kept = run("Seen in Kenya feeds, creators post the step", records, label="observed")
    assert rows["K5"]["verdict"] == "downgrade"
    assert kept["label"] == "single_source"


def test_unlocated_generic_observation_keeps_its_normal_confidence():
    records = [
        record("one", "tiktok", "@one", source_market="KE"),
        record("two", "tiktok", "@two", source_market="KE"),
    ]
    rows, kept = run("Creators post the step", records, label="observed")
    assert rows["K3"]["verdict"] == "pass"
    assert rows["K5"]["verdict"] == "pass"
    assert kept["label"] == "observed"


def test_allowed_label_is_the_k5_ceiling_with_the_source_only_step():
    records = {r["id"]: r for r in (
        record("one", "tiktok", "@one", source_market="KE"),
        record("two", "tiktok", "@two", source_market="KE"),
    )}
    claim = {"id": "c1", "kind": "observation", "evidence_ids": ["one", "two"], "label": "corroborated"}
    assert allowed_label(dict(claim, text="Creators post the step"), records) == "observed"
    assert allowed_label(dict(claim, text="Seen in Kenya feeds, creators post the step"), records) == "single_source"
    assert allowed_label(dict(claim, text="Creators post the step", kind="interpretation"), records) == "inferred"
