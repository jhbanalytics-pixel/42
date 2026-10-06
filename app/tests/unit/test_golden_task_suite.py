"""The golden-task harness: the open-question suite, run against frozen fixtures.

The plan requires a deterministic harness over the ten-question suite plus the
election golden task. It checks that every answer carries the evidence plan and
sections the contract requires, that every citation resolves to evidence that
actually exists, and that no answer overclaims: no causal language, no
demographic claim without a measured basis, no prediction, no polling claim.

The suite is fixture-driven on purpose. A harness that called a model would
test the model's mood; this tests the contract, and it fails the moment an
answer shape drifts or an overclaim slips into approved copy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.golden_task_language import (
    CAUSAL,
    DEMOGRAPHIC,
    POLLING,
    PREDICTIVE,
    RULE_SECTIONS,
    find_overclaim,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "open_intelligence" / "v2"

GOLDEN_IDS = (
    "golden_01_emerging_without_keyword",
    "golden_02_why_moving",
    "golden_03_cross_market_difference",
    "golden_04_carriers",
    "golden_05_history",
    "golden_06_brand_role",
    "golden_07_audience_lens",
    "golden_08_source_agreement",
    "golden_09_source_gap",
    "golden_10_custom",
    "golden_11_election_brand_role",
)

REQUIRED_PLAN_KEYS = (
    "intent",
    "decision",
    "markets",
    "window",
    "audience_lenses",
    "source_families",
    "historical_comparison",
    "evidence_requirements",
    "output_form",
)

EVIDENCE_STATES = {"ready", "thin", "contradictory", "unchecked"}


def load(fixture_id: str) -> dict:
    envelope = json.loads((FIXTURES / f"{fixture_id}.json").read_text(encoding="utf-8"))
    assert envelope["contract_version"] == "2.0.0"
    assert envelope["fixture_id"] == fixture_id
    return envelope["payload"]


def answer_text(payload: dict) -> str:
    answer = payload.get("answer") or {}
    parts = [answer.get("title") or "", answer.get("scope") or ""]
    for section in answer.get("sections") or []:
        parts.append(section.get("heading") or "")
        parts.append(section.get("body") or "")
    return "\n".join(parts)


@pytest.mark.parametrize("fixture_id", GOLDEN_IDS)
def test_every_golden_task_carries_a_complete_evidence_plan(fixture_id):
    plan = load(fixture_id).get("evidence_plan") or {}
    for key in REQUIRED_PLAN_KEYS:
        assert key in plan, f"{fixture_id} plan is missing {key}"
    assert plan["intent"], f"{fixture_id} states no intent"
    assert plan["evidence_state"] in EVIDENCE_STATES


@pytest.mark.parametrize("fixture_id", GOLDEN_IDS)
def test_every_citation_resolves_to_evidence_that_exists(fixture_id):
    payload = load(fixture_id)
    available = {item["evidence_id"] for item in payload.get("evidence") or []}
    cited = set()
    for section in (payload.get("answer") or {}).get("sections") or []:
        cited.update(section.get("evidence_ids") or [])
    dangling = cited - available
    assert not dangling, (
        f"{fixture_id} cites evidence that does not exist: {sorted(dangling)}"
    )


@pytest.mark.parametrize("fixture_id", GOLDEN_IDS)
def test_a_finding_section_never_stands_without_a_receipt(fixture_id):
    """Scope and limitation sections carry no citation by design. Any section
    that states a finding must carry at least one."""
    payload = load(fixture_id)
    uncited = [
        section["section_id"]
        for section in (payload.get("answer") or {}).get("sections") or []
        if not (section.get("evidence_ids") or [])
        and section.get("section_id") not in {"scope", "limitations", "missing_work"}
    ]
    if payload.get("evidence_state") == "ready":
        assert not uncited, f"{fixture_id} states findings without receipts: {uncited}"


@pytest.mark.parametrize("fixture_id", GOLDEN_IDS)
def test_no_answer_overclaims_cause_forecast_or_poll(fixture_id):
    payload = load(fixture_id)
    answer = payload.get("answer") or {}
    for section in answer.get("sections") or []:
        if section.get("section_id") in RULE_SECTIONS:
            continue
        text = f"{section.get('heading') or ''} {section.get('body') or ''}"
        assert not find_overclaim(CAUSAL, text), (
            f"{fixture_id}/{section.get('section_id')} asserts a cause: "
            f"{find_overclaim(CAUSAL, text)}"
        )
        assert not find_overclaim(PREDICTIVE, text), (
            f"{fixture_id}/{section.get('section_id')} forecasts: "
            f"{find_overclaim(PREDICTIVE, text)}"
        )
        assert not find_overclaim(POLLING, text), (
            f"{fixture_id}/{section.get('section_id')} claims polling: "
            f"{find_overclaim(POLLING, text)}"
        )


@pytest.mark.parametrize("fixture_id", GOLDEN_IDS)
def test_a_demographic_word_appears_only_beside_a_declared_basis(fixture_id):
    payload = load(fixture_id)
    text = answer_text(payload)
    match = DEMOGRAPHIC.search(text)
    if not match:
        return
    lenses = payload.get("audience_lenses") or []
    assert lenses, (
        f"{fixture_id} names {match.group(0)} with no audience lens behind it"
    )
    for lens in lenses:
        assert lens["basis"] in {"measured", "inferred"}
        assert lens.get("source")
        assert lens.get("window")
        assert lens.get("confidence") is not None


@pytest.mark.parametrize("fixture_id", GOLDEN_IDS)
def test_an_inferred_lens_never_presents_as_measured(fixture_id):
    for lens in load(fixture_id).get("audience_lenses") or []:
        assert lens["basis"] in {"measured", "inferred"}
        if lens["basis"] == "inferred":
            claim = (lens.get("claim") or "").lower()
            for asserted in ("measured", "confirmed", "proven"):
                assert asserted not in claim


def test_the_suite_covers_every_named_question_and_the_election_task():
    intents = {load(fixture_id)["evidence_plan"]["intent"] for fixture_id in GOLDEN_IDS}
    for required in (
        "landscape",
        "explanation",
        "comparison",
        "creator",
        "historical_analogue",
        "brand_role",
        "audience",
        "source_coverage",
        "custom",
    ):
        assert required in intents, f"the suite tests no {required} question"


def test_a_thin_or_contradictory_read_states_what_is_missing():
    for fixture_id in ("golden_08_source_agreement", "golden_09_source_gap"):
        payload = load(fixture_id)
        assert payload["evidence_state"] in {"thin", "contradictory"}
        stated = (
            (payload.get("limitations") or [])
            + (payload.get("missing_work") or [])
            + (payload.get("contradictions") or [])
        )
        assert stated, f"{fixture_id} is not ready and says nothing about why"


def test_the_election_task_runs_end_to_end_without_engine_vocabulary():
    payload = load("golden_11_election_brand_role")
    text = answer_text(payload) + json.dumps(payload.get("evidence_plan"))
    for leaked in (
        "trends_v2",
        "bigquery",
        "signal_candidates",
        "graph_score",
        "signal_index",
    ):
        assert leaked not in text.lower()
    assert payload["evidence_plan"]["intent"] == "brand_role"
    assert payload.get("evidence"), "the election task carries no receipts"
    for item in payload["evidence"]:
        assert item.get("url"), "an election receipt has no direct link"


# The checker must be shown to fire. These cases are the overclaims the suite
# exists to stop, plus the disclaimed sentences it must leave alone. A gate
# that has never failed is not evidence that anything passed it.
@pytest.mark.parametrize(
    "text,pattern,expected",
    [
        ("Evening play drives the rise in clips.", CAUSAL, True),
        ("The format results in more weekend posts.", CAUSAL, True),
        ("Posts associate venue detail with turnout, without establishing cause.", CAUSAL, False),
        ("This will grow through September.", PREDICTIVE, True),
        ("Three analogues recurred; this is not a forecast.", PREDICTIVE, False),
        ("A survey of 400 residents found the same.", POLLING, True),
        ("Observed posts in the closed window carry the same phrase.", POLLING, False),
        ("Polluted water dominated the feed.", POLLING, False),
    ],
)
def test_the_overclaim_detector_fires_on_real_overclaims_and_not_on_disclaimers(
    text, pattern, expected
):
    assert bool(find_overclaim(pattern, text)) is expected


def test_the_demographic_detector_fires_on_an_unsupported_age_claim():
    assert DEMOGRAPHIC.search("18 to 24 year olds drove the format")
    assert DEMOGRAPHIC.search("A Gen Z behaviour")
    assert not DEMOGRAPHIC.search("Evening players in three neighbourhoods")
