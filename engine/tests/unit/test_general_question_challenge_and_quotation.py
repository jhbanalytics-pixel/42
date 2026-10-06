"""Controlled replays for challenge planning, quotation binding and prompt versioning."""

import copy
import hashlib
import importlib
import json
import re
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_control import QuestionStoreError
from src.analysis.open_intelligence.general_question_plan import validate_question_plan
from src.analysis.open_intelligence.general_question_policy import (
    build_intake_context,
    build_question_policy,
)
from src.analysis.open_intelligence.general_question_quote_span import quote_span
from src.analysis.open_intelligence.general_question_request import normalize_question_request

from tests.unit.test_general_question_boundary_codes import call_for

NOW = datetime(2026, 9, 6, 20, tzinfo=UTC)
POLICY = build_question_policy(pricing_verified_at=NOW)
WINDOW = {"start": "2026-08-23", "end": "2026-09-05", "closed": True}
SCOPE = {
    "client_scope_id": "synthetic_scope",
    "market_scope": ["ke", "ng", "za"],
    "brand_config_id": None,
    "audience_lens_ids": [],
    "theme_id": None,
}
USAGE = {
    "status": "resolved",
    "model_calls": 2,
    "input_tokens": 10,
    "output_tokens": 5,
    "usage_receipt_ids": ["usage_1"],
    "call_receipt_ids": ["call_1"],
    "reservation_ids": ["reservation_1"],
    "reserved_cost_usd": "0.000100",
    "reason": None,
}
POPULATION_BOUNDARY = (
    "This evidence does not establish population-level rates or market-wide trends."
)
SOURCE_RECORD_ONLY = "Source record only; prevalence and representativeness are not measured."


def planning():
    return importlib.import_module("src.analysis.open_intelligence.general_question_planning")


def answering():
    return importlib.import_module("src.analysis.open_intelligence.general_question_answer")


def continuity():
    return importlib.import_module("src.analysis.open_intelligence.general_question_parent_context")


def inputs(message="Why are commuters switching to shared taxis?", history=None, *, market="za"):
    request = normalize_question_request(
        {"message": message, "history": history or []},
        scope=SCOPE,
        request_id="00000000-0000-4000-8000-000000000103",
        admitted_at=NOW,
        policy_digest=POLICY["policy_digest"],
    )
    intake = build_intake_context(request, selected_market=market, source_window=True)
    return POLICY, request, intake


def requirement(identity, purpose, **overrides):
    value = {
        "requirement_id": identity,
        "question": f"Which admitted observations bear on {identity}?",
        "kind": "content",
        "mandatory": True,
        "search_terms": [identity],
        "evidence_purpose": purpose,
    }
    value.update(overrides)
    return value


def pair():
    return [
        requirement("support_switching", "support"),
        requirement("challenge_switching", "challenge"),
    ]


def draft(intent="explanation", requirements=None, **overrides):
    value = {
        "status": "ready",
        "intent": intent,
        "markets": ["za"],
        "window": copy.deepcopy(WINDOW),
        "decision": None,
        "requirements": pair() if requirements is None else requirements,
        "clarification": None,
        "limitations": [],
    }
    value.update(overrides)
    return value


def challenge_plan(request, intake, **overrides):
    return planning().validate_challenge_plan(draft(**overrides), request=request, intake=intake)


def receipt(rid, excerpt, *, index, market="za", published_at=None, reading_ids=()):
    return {
        "receipt_id": rid,
        "citation_label": f"R{index}",
        "kind": "content",
        "snapshot_id": "snapshot_q03",
        "market": market,
        "source_label": "Community notes",
        "source_family": "community",
        "platform": None,
        "author": None,
        "url": None,
        "source_row_id": "row_" + rid,
        "published_at": published_at,
        "collected_at": None,
        "excerpt": excerpt,
        "reading_ids": list(reading_ids),
        "limitations": [],
        "content_digest": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }


def material(plan, request, intake, excerpts, *, fulfilled=None, readings=(), **receipt_fields):
    receipts = [
        receipt(rid, text, index=index + 1, **receipt_fields.get(rid, {}))
        for index, (rid, text) in enumerate(excerpts.items())
    ]
    snapshot = {
        "contract_version": "general_question_snapshot_v1",
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "intake_digest": intake["intake_digest"],
        "plan_digest": plan["plan_digest"],
        "policy_digest": request["policy_digest"],
        "deployment_digest": "b" * 64,
        "snapshot_id": "snapshot_q03",
        "as_of": request["as_of"],
        "window": plan["window"],
        "receipts": receipts,
        "readings": list(readings),
        "limitations": [],
        "missing_work": [],
        "fulfilled_requirement_ids": [item["requirement_id"] for item in plan["requirements"]]
        if fulfilled is None
        else list(fulfilled),
    }
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    return {"request": request, "intake": intake, "plan": plan, "snapshot": snapshot}


def typed_draft(
    context,
    quotes,
    *,
    derived=True,
    text="Shared taxi use appears in the quoted commuter records.",
    parents=None,
    readings=(),
    codes=("corroboration_not_established",),
):
    spans = answering()._quote_spans(context["snapshot"])
    aliases = {
        row["receipt_id"]: f"r{index + 1}"
        for index, row in enumerate(context["snapshot"]["receipts"])
    }
    interpretations = [
        {
            "claim_id": "interpret",
            "text": text,
            "parent_claim_ids": [quotes[0][0]] if parents is None else list(parents),
            "boundary_codes": list(codes),
        }
    ]
    return {
        "quote_observations": [
            {
                "claim_id": cid,
                "receipt_id": aliases[rid],
                "span_id": spans[rid][index]["span_id"],
                "evidence_purpose": purpose,
            }
            for cid, rid, index, purpose in quotes
        ],
        "reading_observations": list(readings),
        "interpretations": interpretations if derived else [],
        "inferences": [],
        "proposals": [],
        "answer": ["interpret"] if derived else [quotes[0][0]],
    }


def hydrate(value, context, adapter="typed_v5"):
    return answering()._hydrate_typed_answer(value, **context, _adapter=adapter)


def project(hydrated, context, value=None, adapter="typed_v5"):
    call = (
        {}
        if value is None
        else {"_answer_call": call_for(answering(), context, POLICY, value, adapter)}
    )
    return answering().project_question_answer(
        hydrated, **context, usage=USAGE, _span_mode=True, _structural_uncertainty=True, **call
    )


def replay(context, quotes, **draft_fields):
    value = typed_draft(context, quotes, **draft_fields)
    return project(hydrate(value, context), context, value)


def claims_by_id(result):
    return {claim["claim_id"]: claim for claim in result["intelligence"]["claims"]}


def test_challenge_adapter_carries_evidence_purpose_under_distinct_prompt_and_schema_bytes():
    policy, request, intake = inputs()
    current = planning().build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60
    )
    challenge = planning().build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60, _adapter="challenge_v5"
    )
    assert challenge.system_instruction_digest != current.system_instruction_digest
    assert challenge.response_schema_digest != current.response_schema_digest
    assert challenge.input_digest == current.input_digest
    item = challenge.generation_config.response_schema["properties"]["requirements"]["items"]
    assert item["properties"]["evidence_purpose"]["enum"] == ["support", "challenge", "context"]
    assert "evidence_purpose" in item["required"]
    older = current.generation_config.response_schema["properties"]["requirements"]["items"]
    assert "evidence_purpose" not in older["properties"]
    assert item["properties"]["requirement_id"] == older["properties"]["requirement_id"]
    assert challenge.generation_config.max_output_tokens == 800
    assert (
        challenge.generation_config.max_output_tokens == current.generation_config.max_output_tokens
    )
    assert json.loads(challenge.contents)["retrieval_limits"]["query_jobs"] == 8
    assert (
        challenge.generation_config.response_schema["properties"]["requirements"]["maxItems"] == 8
    )
    assert policy["limits"]["calls_per_request"] == 2
    assert planning().planning_adapter_id(challenge.system_instruction_digest) == "challenge_v5"
    assert planning().planning_adapter_id(current.system_instruction_digest) == "source_window_v4"
    call = {
        **{
            field: getattr(challenge, field)
            for field in (
                "input_digest",
                "system_instruction_digest",
                "response_schema_digest",
                "model",
            )
        },
        "thinking_level": "LOW",
        "max_output_tokens": 800,
    }
    assert (
        planning().planning_request_for_call(call, request=request, intake=intake, policy=policy)
        == challenge
    )
    with pytest.raises(ValueError, match="planning binding"):
        planning().planning_request_for_call(
            {**call, "system_instruction_digest": current.system_instruction_digest},
            request=request,
            intake=intake,
            policy=policy,
        )
    older_intake = build_intake_context(request, selected_market="za")
    with pytest.raises(ValueError, match="planning binding"):
        planning().build_question_planning_request(
            request, older_intake, policy=policy, remaining_seconds=60, _adapter="challenge_v5"
        )


def test_causal_or_comparative_plan_needs_a_support_and_challenge_pair_inside_the_limits():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    assert plan["contract_version"] == "general_question_plan_v3"
    assert [item["evidence_purpose"] for item in plan["requirements"]] == ["support", "challenge"]
    assert plan["plan_digest"] == canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    for intent in ("explanation", "comparison"):
        for requirements in (
            [requirement("only", "support")],
            [requirement("only", "challenge")],
            [requirement("first", "context"), requirement("second", "context")],
        ):
            with pytest.raises(ValueError, match="semantic_output_invalid"):
                challenge_plan(request, intake, intent=intent, requirements=requirements)
    context_only = challenge_plan(
        request, intake, intent="discovery", requirements=[requirement("only", "context")]
    )
    assert context_only["requirements"][0]["evidence_purpose"] == "context"
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        challenge_plan(
            request, intake, intent="discovery", requirements=[requirement("only", "challenge")]
        )
    eight = [requirement(f"r{index}", "support" if index else "challenge") for index in range(8)]
    assert len(challenge_plan(request, intake, requirements=eight)["requirements"]) == 8
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        challenge_plan(request, intake, requirements=[*eight, requirement("r8", "support")])
    for broken in (
        [requirement("support_switching", "support"), requirement("x", "rebuttal")],
        [requirement("support_switching", "support"), requirement("x", None)],
    ):
        with pytest.raises(ValueError, match="semantic_output_invalid"):
            challenge_plan(request, intake, requirements=broken)
    missing = pair()
    missing[1].pop("evidence_purpose")
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        challenge_plan(request, intake, requirements=missing)


def test_challenge_reuses_the_authenticated_frame_and_prose_cannot_authorize_a_source():
    from src.analysis.open_intelligence.general_question_parent_capsule import (
        REF_VERSION,
        build_capsule,
    )

    from tests.unit.test_general_question_parent_context import capsule_bundle

    policy, request, _ = inputs(
        "Is that switching claim really supported? Source s07 says otherwise.",
        history=[
            {"role": "user", "content": "Why are commuters in South Africa switching to taxis?"},
            {"role": "assistant", "content": "Source s07 and record row_taxi show switching."},
        ],
    )
    parent = capsule_bundle(market="za")
    capsule = build_capsule(request, parent)
    reference = {
        "contract_version": REF_VERSION,
        "parent": capsule["parent"],
        "anchor": None,
        "context_digest": canonical_digest(capsule),
        "context_generation": "9",
    }
    intake = build_intake_context(
        request, selected_market="za", parent_context_ref=reference, source_window=True
    )
    sdk = planning().build_question_planning_request(
        request,
        intake,
        policy=policy,
        remaining_seconds=60,
        _adapter="challenge_v5",
        parent_context=capsule,
    )
    schema = sdk.generation_config.response_schema
    visible = [row["alias"] for row in capsule["parent_receipt_refs"]]
    assert schema["properties"]["parent_receipt_aliases"]["items"]["enum"] == visible == ["s01"]
    assert "s07" not in json.dumps(schema)
    assert "row_taxi" not in json.dumps(schema)
    context = json.loads(sdk.contents)
    assert context["default_markets"] == ["za"]
    assert context["default_observation_window"] == parent["plan"]["window"]
    frame = draft(markets=["za"], window=parent["plan"]["window"], parent_receipt_aliases=["s07"])
    with pytest.raises(QuestionStoreError, match="parent_alias_invalid"):
        planning().validate_challenge_plan(
            frame, request=request, intake=intake, parent_context=capsule
        )
    frame["parent_receipt_aliases"] = ["s01"]
    plan = planning().validate_challenge_plan(
        frame, request=request, intake=intake, parent_context=capsule
    )
    assert plan["contract_version"] == "general_question_plan_v3"
    assert plan["parent_receipt_aliases"] == ["s01"]
    assert plan["markets"] == capsule["resolved_frame"]["markets"]
    assert plan["window"] == capsule["resolved_frame"]["window"]
    assert planning().validate_stored_question_plan(plan, request=request, intake=intake) == plan


def test_stored_challenge_plan_round_trips_and_older_readers_keep_their_key_set():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    assert planning().validate_stored_question_plan(plan, request=request, intake=intake) == plan
    tampered = copy.deepcopy(plan)
    tampered["requirements"][1]["evidence_purpose"] = "support"
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        planning().validate_stored_question_plan(tampered, request=request, intake=intake)
    older_draft = draft()
    for item in older_draft["requirements"]:
        item.pop("evidence_purpose")
    older = validate_question_plan(older_draft, request=request, intake=intake)
    assert older["contract_version"] == "general_question_plan_v1"
    assert "evidence_purpose" not in older["requirements"][0]
    assert planning().validate_stored_question_plan(older, request=request, intake=intake) == older
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        validate_question_plan(draft(), request=request, intake=intake)
    relabelled = copy.deepcopy(plan)
    relabelled["contract_version"] = "general_question_plan_v1"
    relabelled["plan_digest"] = canonical_digest(
        {key: value for key, value in relabelled.items() if key != "plan_digest"}
    )
    with pytest.raises(ValueError, match="semantic_output_invalid"):
        planning().validate_stored_question_plan(relabelled, request=request, intake=intake)


def test_false_national_quantity_premise_plans_a_bounded_clarification():
    _, request, intake = inputs("Why do most Kenyans commute by shared taxi?", market="ke")
    clarification = (
        "No admitted evidence establishes the national share of commuters using shared taxis. "
        "Please give the basis for that figure, or ask about the commuting patterns observed "
        "in the admitted records."
    )
    plan = challenge_plan(
        request,
        intake,
        status="needs_clarification",
        markets=["ke"],
        window=None,
        requirements=[],
        clarification=clarification,
    )
    assert plan["status"] == "needs_clarification"
    assert plan["requirements"] == []
    assert plan["clarification"] == clarification
    assert plan["contract_version"] == "general_question_plan_v3"


def test_contradictory_evidence_found_binds_the_quote_and_names_the_finding():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    context = material(
        plan,
        request,
        intake,
        {
            "r_support": "Commuters in Soweto now share taxis to work.",
            "r_contra": "Commuters in Soweto still travel alone to work.",
        },
    )
    value = typed_draft(
        context,
        [("supports", "r_support", 0, "support"), ("contrary", "r_contra", 0, "challenge")],
        parents=["supports", "contrary"],
    )
    hydrated = hydrate(value, context)
    excerpt = context["snapshot"]["receipts"][1]["excerpt"]
    assert hydrated["quote_bindings"]["contrary"] == {
        "receipt_id": "r_contra",
        "source_field": "excerpt",
        "content_digest": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        "occurrence": 0,
        **quote_span(excerpt, excerpt),
    }
    result = project(hydrated, context, value)
    limitations = result["intelligence"]["limitations"]
    texts = answering()._CHALLENGE_TEXT
    assert texts["contradictory_evidence_found"] in limitations
    assert texts["challenge_incomplete"] not in limitations
    assert texts["challenge_completed_without_contradiction"] not in limitations
    assert answering().challenge_state(
        plan, context["snapshot"], result["intelligence"]["claims"]
    ) == {
        "state": "contradictory_evidence_found",
        "challenge_requirement_ids": ["challenge_switching"],
        "unfulfilled_requirement_ids": [],
        "finding_claim_ids": ["contrary"],
    }
    claims = claims_by_id(result)
    assert answering()._CHALLENGE_FINDING in claims["contrary"]["limitations"]
    assert answering()._CHALLENGE_FINDING not in claims["supports"]["limitations"]
    receipts = {row["receipt_id"]: row for row in result["intelligence"]["receipts"]}
    assert receipts["r_contra"]["quote_bindings"] == [
        {
            "claim_id": "contrary",
            "source_field": "excerpt",
            "content_digest": receipts["r_contra"]["content_digest"],
            "start": 0,
            "end": len(excerpt),
            "quote_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        }
    ]
    assert result["intelligence"]["status"] == "complete"
    assert "consensus" not in result["answer"].lower()
    tampered = copy.deepcopy(hydrated)
    tampered["quote_bindings"]["contrary"]["start"] += 1
    with pytest.raises(ValueError, match="answer_boundary_binding_invalid"):
        project(tampered, context, value)


def test_challenge_completed_without_contradiction_is_never_consensus():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    context = material(plan, request, intake, {"r_a": "Commuters in Soweto now share taxis."})
    result = replay(context, [("quoted", "r_a", 0, "support")])
    limitations = result["intelligence"]["limitations"]
    texts = answering()._CHALLENGE_TEXT
    completed = texts["challenge_completed_without_contradiction"]
    assert completed in limitations
    assert "not evidence of consensus" in completed
    assert texts["contradictory_evidence_found"] not in limitations
    assert texts["challenge_incomplete"] not in limitations
    assert answering().challenge_state(
        plan, context["snapshot"], result["intelligence"]["claims"]
    ) == {
        "state": "challenge_completed_without_contradiction",
        "challenge_requirement_ids": ["challenge_switching"],
        "unfulfilled_requirement_ids": [],
        "finding_claim_ids": [],
    }
    for phrase in (
        "There is consensus that commuters share taxis.",
        "Commuters share taxis without disagreement in the records.",
        "All sources agree that commuters share taxis.",
    ):
        with pytest.raises(ValueError, match="answer_consensus_invalid"):
            replay(context, [("quoted", "r_a", 0, "support")], text=phrase)


def test_incomplete_challenge_search_is_reported_apart_from_the_other_two_states():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    context = material(
        plan,
        request,
        intake,
        {"r_a": "Commuters in Soweto now share taxis."},
        fulfilled=["support_switching"],
    )
    intelligence = replay(context, [("quoted", "r_a", 0, "support")])["intelligence"]
    texts = answering()._CHALLENGE_TEXT
    assert texts["challenge_incomplete"] in intelligence["limitations"]
    assert texts["challenge_completed_without_contradiction"] not in intelligence["limitations"]
    assert texts["contradictory_evidence_found"] not in intelligence["limitations"]
    assert intelligence["status"] == "partial"
    question = plan["requirements"][1]["question"]
    assert (
        "Challenge search incomplete: challenge_switching: " + question
        in intelligence["missing_work"]
    )
    assert "Challenge requirements: challenge_switching." in intelligence["limitations"]
    assert "Missing required evidence: " + question in intelligence["missing_work"]
    assert answering().challenge_state(plan, context["snapshot"], intelligence["claims"]) == {
        "state": "challenge_incomplete",
        "challenge_requirement_ids": ["challenge_switching"],
        "unfulfilled_requirement_ids": ["challenge_switching"],
        "finding_claim_ids": [],
    }


def test_older_plans_and_adapters_keep_their_exact_packet_shape():
    _, request, intake = inputs()
    older_draft = draft()
    for item in older_draft["requirements"]:
        item.pop("evidence_purpose")
    plan = validate_question_plan(older_draft, request=request, intake=intake)
    context = material(plan, request, intake, {"r_a": "Commuters in Soweto now share taxis."})
    value = typed_draft(context, [("quoted", "r_a", 0, "support")])
    with pytest.raises(ValueError, match="answer_typed_invalid"):
        hydrate(value, context, "typed_v4")
    value["quote_observations"][0].pop("evidence_purpose")
    hydrated = hydrate(value, context, "typed_v4")
    assert set(hydrated) == {"claims", "sections"}
    result = project(hydrated, context, value, "typed_v4")
    assert not any("Challenge search" in text for text in result["intelligence"]["limitations"])
    assert all("quote_bindings" not in row for row in result["intelligence"]["receipts"])
    assert set(result["intelligence"]) == {
        "contract_version",
        "request_id",
        "request_digest",
        "status",
        "resolved_scope",
        "window",
        "as_of",
        "snapshot_id",
        "sections",
        "claims",
        "receipts",
        "readings",
        "limitations",
        "missing_work",
        "clarification",
        "review_required",
        "ready_for_downstream",
        "usage",
    }


def test_household_switching_anecdote_stays_a_source_record_not_a_prevalence_claim():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    context = material(
        plan, request, intake, {"r_home": "Our household switched to the cheaper taxi route."}
    )
    result = replay(
        context,
        [("anecdote", "r_home", 0, "support")],
        text="Households in the quoted records describe switching taxi routes.",
        codes=["population_not_established"],
    )
    claims = claims_by_id(result)
    assert claims["anecdote"]["limitations"] == [SOURCE_RECORD_ONLY]
    assert POPULATION_BOUNDARY in claims["interpret"]["limitations"]
    assert claims["interpret"]["text"].startswith("Hypothesis: ")
    for text in (
        "Most households are switching taxi routes.",
        "A majority of households switched taxi routes.",
        "Half of all households switched taxi routes.",
    ):
        with pytest.raises(ValueError, match="answer_quantity_invalid"):
            replay(context, [("anecdote", "r_home", 0, "support")], text=text)


def test_unrelated_count_ancestor_cannot_ground_a_challenge_interpretation():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    reading = {
        "reading_id": "count",
        "value": 1,
        "unit": "records",
        "window": plan["window"],
        "method": "selected_receipt_count",
        "denominator": None,
        "source_receipt_ids": ["r_a"],
        "limitations": [],
    }
    context = material(
        plan,
        request,
        intake,
        {"r_a": "Commuters in Soweto now share taxis."},
        readings=[reading],
        r_a={"reading_ids": ["count"]},
    )
    value = typed_draft(
        context,
        [("quoted", "r_a", 0, "support")],
        parents=["counted"],
        readings=[{"claim_id": "counted", "reading_id": "d1", "evidence_purpose": "challenge"}],
    )
    with pytest.raises(ValueError, match="answer_typed_invalid"):
        hydrate(value, context)
    value["interpretations"][0]["parent_claim_ids"] = ["quoted", "counted"]
    with pytest.raises(ValueError, match="answer_typed_invalid"):
        hydrate(value, context)
    value["interpretations"][0]["parent_claim_ids"] = ["quoted"]
    result = project(hydrate(value, context), context, value)
    assert answering()._CHALLENGE_FINDING in claims_by_id(result)["counted"]["limitations"]
    assert answering().challenge_state(plan, context["snapshot"], result["intelligence"]["claims"])[
        "finding_claim_ids"
    ] == ["counted"]


def test_literal_but_irrelevant_quote_binds_only_as_a_source_record():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    context = material(
        plan, request, intake, {"r_weather": "The weather in Durban was cold this week."}
    )
    value = typed_draft(context, [("weather", "r_weather", 0, "challenge")])
    hydrated = hydrate(value, context)
    assert hydrated["quote_bindings"]["weather"]["start"] == 0
    result = project(hydrated, context, value)
    claims = claims_by_id(result)
    assert claims["weather"]["support_state"] == "source_record"
    assert SOURCE_RECORD_ONLY in claims["weather"]["limitations"]
    assert claims["interpret"]["support_state"] == "derived"
    assert "Independent corroboration has not been established" in " ".join(
        claims["interpret"]["limitations"]
    )
    assert result["intelligence"]["review_required"] is False
    assert result["intelligence"]["ready_for_downstream"] is False


def test_post_outside_the_requested_date_is_refused_before_any_quotation():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    context = material(
        plan,
        request,
        intake,
        {"r_old": "Commuters in Soweto now share taxis."},
        r_old={"published_at": "2026-08-01T10:00:00+00:00"},
    )
    value = {
        "quote_observations": [
            {
                "claim_id": "quoted",
                "receipt_id": "r1",
                "span_id": "s0",
                "evidence_purpose": "support",
            }
        ],
        "reading_observations": [],
        "interpretations": [],
        "inferences": [],
        "proposals": [],
        "answer": ["quoted"],
    }
    with pytest.raises(ValueError, match="answer_receipt_invalid"):
        hydrate(value, context)
    with pytest.raises(ValueError, match="answer_receipt_invalid"):
        answering().build_question_answering_request(
            context["request"],
            context["intake"],
            context["plan"],
            context["snapshot"],
            policy=POLICY,
            remaining_input_tokens=100,
            remaining_output_tokens=100,
            remaining_seconds=30,
            _adapter="typed_v5",
        )


def test_punctuation_change_refuses_the_quotation_as_unsupported():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    excerpt = "Commuters in Soweto now share taxis to work."
    context = material(plan, request, intake, {"r_a": excerpt})
    hydrated = hydrate(
        typed_draft(context, [("quoted", "r_a", 0, "support")], derived=False), context
    )
    assert project(hydrated, context)["intelligence"]["receipts"][0]["quote_bindings"][0][
        "end"
    ] == len(excerpt)
    changed = excerpt[:-1] + "!"
    with pytest.raises(ValueError):
        quote_span(excerpt, changed)
    for text in (changed, excerpt[:-1], excerpt.replace("Soweto now", "Soweto, now")):
        tampered = copy.deepcopy(hydrated)
        tampered["claims"][0]["segments"][0]["text"] = text
        with pytest.raises(ValueError, match="answer_quote"):
            project(tampered, context)
    tampered = copy.deepcopy(hydrated)
    tampered["quote_bindings"]["quoted"]["quote_sha256"] = hashlib.sha256(
        changed.encode("utf-8")
    ).hexdigest()
    with pytest.raises(ValueError, match="answer_quote_unsupported"):
        project(tampered, context)
    tampered = copy.deepcopy(hydrated)
    tampered["quote_bindings"]["quoted"]["end"] -= 1
    with pytest.raises(ValueError, match="answer_quote_unsupported"):
        project(tampered, context)
    tampered = copy.deepcopy(hydrated)
    tampered["quote_bindings"].pop("quoted")
    with pytest.raises(ValueError, match="answer_quote_unsupported"):
        project(tampered, context)


def test_repeated_quote_binds_the_selected_occurrence():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    excerpt = "Taxis are full today. Taxis are full today. Nobody drives alone."
    context = material(plan, request, intake, {"r_a": excerpt})
    options = answering()._quote_spans(context["snapshot"])["r_a"]
    assert [option["text"] for option in options[:2]] == ["Taxis are full today."] * 2
    assert [option["start"] for option in options[:2]] == [0, 22]
    hydrated = hydrate(
        typed_draft(context, [("second", "r_a", 1, "support")], derived=False), context
    )
    binding = hydrated["quote_bindings"]["second"]
    assert (binding["occurrence"], binding["start"], binding["end"]) == (1, 22, 43)
    assert (
        binding["quote_sha256"] == quote_span(excerpt, "Taxis are full today.", 1)["quote_sha256"]
    )
    result = project(hydrated, context)
    recorded = result["intelligence"]["receipts"][0]["quote_bindings"]
    assert [(row["claim_id"], row["start"], row["end"]) for row in recorded] == [("second", 22, 43)]
    for occurrence in (0, 2, "1", True):
        tampered = copy.deepcopy(hydrated)
        tampered["quote_bindings"]["second"]["occurrence"] = occurrence
        with pytest.raises(ValueError, match="answer_quote_unsupported"):
            project(tampered, context)


def test_emoji_before_the_quoted_span_keeps_code_point_offsets():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    excerpt = "\U0001f525\nTaxis are full today."
    context = material(plan, request, intake, {"r_a": excerpt})
    options = answering()._quote_spans(context["snapshot"])["r_a"]
    index = next(i for i, option in enumerate(options) if option["text"] == "Taxis are full today.")
    hydrated = hydrate(
        typed_draft(context, [("quoted", "r_a", index, "support")], derived=False), context
    )
    binding = hydrated["quote_bindings"]["quoted"]
    assert binding["start"] == 2
    assert len(excerpt[: binding["start"]].encode("utf-8")) == 5
    assert len(excerpt[: binding["start"]].encode("utf-16-le")) // 2 == 3
    assert excerpt[binding["start"] : binding["end"]] == "Taxis are full today."
    result = project(hydrated, context)
    assert result["intelligence"]["receipts"][0]["quote_bindings"][0]["start"] == 2
    assert claims_by_id(result)["quoted"]["text"] == "Taxis are full today."


def test_cross_record_quote_substitution_fails():
    _, request, intake = inputs()
    plan = challenge_plan(request, intake)
    context = material(
        plan, request, intake, {"r_a": "Taxis are full today.", "r_b": "Buses are empty today."}
    )
    hydrated = hydrate(
        typed_draft(context, [("quoted", "r_a", 0, "support")], derived=False), context
    )
    for field, value in (
        ("receipt_id", "r_b"),
        ("content_digest", context["snapshot"]["receipts"][1]["content_digest"]),
        ("source_field", "source_label"),
    ):
        tampered = copy.deepcopy(hydrated)
        tampered["quote_bindings"]["quoted"][field] = value
        with pytest.raises(ValueError, match="answer_quote_unsupported"):
            project(tampered, context)
    tampered = copy.deepcopy(hydrated)
    tampered["quote_bindings"] = {"other": tampered["quote_bindings"]["quoted"]}
    with pytest.raises(ValueError, match="answer_quote_unsupported"):
        project(tampered, context)
    tampered = copy.deepcopy(hydrated)
    tampered["claims"][0]["segments"][0]["receipt_id"] = "r_b"
    tampered["claims"][0]["receipt_ids"] = ["r_b"]
    with pytest.raises(ValueError, match="answer_quote"):
        project(tampered, context)
    spans = answering()._quote_spans(context["snapshot"])
    value = typed_draft(context, [("quoted", "r_a", 0, "support")], derived=False)
    value["quote_observations"][0]["receipt_id"] = "r2"
    value["quote_observations"][0]["span_id"] = spans["r_a"][0]["span_id"]
    with pytest.raises(ValueError, match="answer_typed_invalid"):
        hydrate(value, context)


def test_false_national_quantity_premise_gets_the_bounded_correction_not_a_prevalence_claim():
    _, request, intake = inputs("Why do most Kenyans commute by shared taxi?", market="ke")
    plan = challenge_plan(request, intake, markets=["ke"])
    context = material(
        plan,
        request,
        intake,
        {"r_ke": "Commuters in Nairobi describe long shared taxi queues."},
        r_ke={"market": "ke"},
    )
    for text in (
        "Most Kenyans commute by shared taxi.",
        "The majority of Kenyan commuters use shared taxis.",
        "Eighty in every hundred Kenyans commute by shared taxi.",
    ):
        with pytest.raises(ValueError, match="answer_quantity_invalid"):
            replay(context, [("queue", "r_ke", 0, "support")], text=text)
    result = replay(
        context,
        [("queue", "r_ke", 0, "support")],
        text="The quoted Nairobi records describe shared taxi queues, not a national share.",
        codes=["population_not_established", "corroboration_not_established"],
    )
    interpret = claims_by_id(result)["interpret"]
    assert POPULATION_BOUNDARY in interpret["limitations"]
    assert POPULATION_BOUNDARY in result["intelligence"]["limitations"]
    assert "Limitation: " + POPULATION_BOUNDARY in result["answer"]
    assert "%" not in result["answer"]
    assert not any(char.isdigit() for char in re.sub(r"\[R[0-9]+\]", "", result["answer"]))


def test_response_versions_resolve_exact_digests_and_refuse_aliases_and_paths():
    policy, request, intake = inputs()
    plans = {
        name: planning().build_question_planning_request(
            request, intake, policy=policy, remaining_seconds=60, _adapter=name
        )
        for name in ("source_window_v4", "challenge_v5")
    }
    answers = {}
    for name in ("typed_v4", "typed_v5"):
        schema, instruction = answering()._answer_adapter_contract(name)
        answers[name] = (hashlib.sha256(instruction.encode()).hexdigest(), canonical_digest(schema))
        assert answering().answer_adapter_id(answers[name][1], answers[name][0]) == name

    def binding(instruction_digest, schema_digest, **extra):
        return {
            "system_instruction_digest": instruction_digest,
            "response_schema_digest": schema_digest,
            "model": policy["model"],
            "policy_digest": policy["policy_digest"],
            **extra,
        }

    def plan_binding(name):
        return binding(plans[name].system_instruction_digest, plans[name].response_schema_digest)

    resolve = continuity().resolve_response_versions
    old = resolve(
        policy_digest=policy["policy_digest"],
        planning=plan_binding("source_window_v4"),
        answering=binding(*answers["typed_v4"]),
    )
    current = resolve(
        policy_digest=policy["policy_digest"],
        planning=plan_binding("challenge_v5"),
        answering=binding(*answers["typed_v5"]),
    )
    assert old["contract_version"] == "general_question_response_versions_v1"
    assert (old["planning"]["adapter"], old["answering"]["adapter"]) == (
        "source_window_v4",
        "typed_v4",
    )
    assert (current["planning"]["adapter"], current["answering"]["adapter"]) == (
        "challenge_v5",
        "typed_v5",
    )
    assert (
        old["planning"]["system_instruction_digest"]
        != current["planning"]["system_instruction_digest"]
    )
    assert (
        old["answering"]["response_schema_digest"] != current["answering"]["response_schema_digest"]
    )
    assert old["policy_digest"] == current["policy_digest"] == policy["policy_digest"]
    assert old["answering"]["model"] == current["answering"]["model"] == policy["model"]
    planned_only = resolve(
        policy_digest=policy["policy_digest"], planning=plan_binding("challenge_v5"), answering=None
    )
    assert planned_only["answering"] is None
    good = plan_binding("challenge_v5")
    refusals = [
        {"planning": good, "answering": binding(answers["typed_v4"][0], answers["typed_v5"][1])},
        {"planning": good, "answering": binding(answers["typed_v5"][0], answers["typed_v4"][1])},
        {
            "planning": {"model": policy["model"], "policy_digest": policy["policy_digest"]},
            "answering": None,
        },
        {
            "planning": binding(
                "engine/src/analysis/open_intelligence/general_question_plan.py",
                plans["challenge_v5"].response_schema_digest,
            ),
            "answering": None,
        },
        {
            "planning": binding(hashlib.sha256(policy["model"].encode()).hexdigest(), "a" * 64),
            "answering": None,
        },
        {"planning": binding(*answers["typed_v5"]), "answering": None},
        {
            "planning": binding(
                plans["challenge_v5"].system_instruction_digest,
                plans["source_window_v4"].response_schema_digest,
            ),
            "answering": None,
        },
        {
            "planning": binding(
                plans["source_window_v4"].system_instruction_digest,
                plans["challenge_v5"].response_schema_digest,
            ),
            "answering": None,
        },
        {"planning": {**good, "policy_digest": "c" * 64}, "answering": None},
        {"planning": good, "answering": {"model": policy["model"]}},
        {"planning": None, "answering": binding(*answers["typed_v5"])},
    ]
    for case in refusals:
        with pytest.raises(ValueError, match="response_version_unresolved"):
            resolve(policy_digest=policy["policy_digest"], **case)
    with pytest.raises(ValueError, match="response_version_unresolved"):
        resolve(policy_digest="not a digest", planning=good, answering=None)


def test_recorded_versions_follow_the_answered_calls_on_a_request_context():
    policy, request, intake = inputs()
    sdk = planning().build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60, _adapter="challenge_v5"
    )
    schema, instruction = answering()._answer_adapter_contract("typed_v5")
    pointer = {"generation": "3", "digest": "d" * 64}
    planning_call = {
        "stage": "planning",
        "model": sdk.model,
        "policy_digest": policy["policy_digest"],
        "system_instruction_digest": sdk.system_instruction_digest,
        "response_schema_digest": sdk.response_schema_digest,
        "response": pointer,
    }
    answering_call = {
        "stage": "answering",
        "model": sdk.model,
        "policy_digest": policy["policy_digest"],
        "system_instruction_digest": hashlib.sha256(instruction.encode()).hexdigest(),
        "response_schema_digest": canonical_digest(schema),
        "response": None,
    }

    def context(calls):
        return {
            "request": {"policy_digest": policy["policy_digest"]},
            "admission": {"execution": {"calls": calls}},
        }

    recorded = continuity().recorded_response_versions
    assert recorded(context({})) is None
    planned = recorded(context({"planning": planning_call, "answering": answering_call}))
    assert planned["planning"]["adapter"] == "challenge_v5"
    assert planned["answering"] is None
    both = recorded(
        context({"planning": planning_call, "answering": {**answering_call, "response": pointer}})
    )
    assert both["answering"]["adapter"] == "typed_v5"
    assert (
        both["answering"]["system_instruction_digest"]
        == answering_call["system_instruction_digest"]
    )
    with pytest.raises(ValueError, match="response_version_unresolved"):
        recorded(context({"answering": {**answering_call, "response": pointer}}))
    with pytest.raises(ValueError, match="response_version_unresolved"):
        recorded(
            context(
                {
                    "planning": {
                        **planning_call,
                        "system_instruction_digest": hashlib.sha256(b"prompt.txt").hexdigest(),
                    }
                }
            )
        )


def test_challenge_plan_and_typed_v5_refuse_each_other_by_named_code():
    _, request, intake = inputs()
    excerpts = {"r_a": "Commuters in Soweto now share taxis."}
    plan = challenge_plan(request, intake)
    context = material(plan, request, intake, excerpts)
    value = typed_draft(context, [("quoted", "r_a", 0, "support")])
    older = copy.deepcopy(value)
    older["quote_observations"][0].pop("evidence_purpose")
    budget = {
        "policy": POLICY,
        "remaining_input_tokens": 32000,
        "remaining_output_tokens": 4000,
        "remaining_seconds": 60,
    }
    with pytest.raises(ValueError, match="answer_challenge_adapter_required"):
        hydrate(older, context, "typed_v4")
    with pytest.raises(ValueError, match="answer_challenge_adapter_required"):
        answering().build_question_answering_request(**context, **budget, _adapter="typed_v4")
    hydrated = hydrate(value, context)
    plain = {key: item for key, item in hydrated.items() if key in ("claims", "sections")}
    with pytest.raises(ValueError, match="answer_challenge_adapter_required"):
        project(plain, context)
    crossed = call_for(answering(), context, POLICY, value, "typed_v5")
    schema, instruction = answering()._answer_adapter_contract("typed_v4")
    crossed["binding"].update(
        response_schema_digest=canonical_digest(schema),
        system_instruction_digest=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    with pytest.raises(ValueError, match="answer_challenge_adapter_required"):
        answering().project_question_answer(
            hydrated,
            **context,
            usage=USAGE,
            _span_mode=True,
            _structural_uncertainty=True,
            _answer_call=crossed,
        )
    older_draft = draft()
    for item in older_draft["requirements"]:
        item.pop("evidence_purpose")
    v1 = validate_question_plan(older_draft, request=request, intake=intake)
    v1_context = material(v1, request, intake, excerpts)
    with pytest.raises(ValueError, match="answer_challenge_plan_required"):
        hydrate(typed_draft(v1_context, [("quoted", "r_a", 0, "support")]), v1_context)
    with pytest.raises(ValueError, match="answer_challenge_plan_required"):
        answering().build_question_answering_request(**v1_context, **budget, _adapter="typed_v5")
    v1_value = typed_draft(v1_context, [("quoted", "r_a", 0, "support")])
    v1_value["quote_observations"][0].pop("evidence_purpose")
    v1_hydrated = hydrate(v1_value, v1_context, "typed_v4")
    with pytest.raises(ValueError, match="answer_challenge_plan_required"):
        project({**v1_hydrated, "quote_bindings": {}, "evidence_purposes": {}}, v1_context)
    v1_call = call_for(answering(), v1_context, POLICY, v1_value, "typed_v4")
    schema, instruction = answering()._answer_adapter_contract("typed_v5")
    v1_call["binding"].update(
        response_schema_digest=canonical_digest(schema),
        system_instruction_digest=hashlib.sha256(instruction.encode()).hexdigest(),
    )
    with pytest.raises(ValueError, match="answer_challenge_plan_required"):
        answering().project_question_answer(
            v1_hydrated,
            **v1_context,
            usage=USAGE,
            _span_mode=True,
            _structural_uncertainty=True,
            _answer_call=v1_call,
        )
    accepted = project(v1_hydrated, v1_context, v1_value, "typed_v4")
    assert accepted["intelligence"]["status"] == "complete"


def test_unplanned_challenge_search_is_partial_and_names_the_missing_work():
    _, request, intake = inputs()
    plan = challenge_plan(
        request, intake, intent="discovery", requirements=[requirement("only", "context")]
    )
    context = material(plan, request, intake, {"r_a": "Commuters in Soweto now share taxis."})
    result = replay(context, [("quoted", "r_a", 0, "context")])
    intelligence = result["intelligence"]
    texts = answering()._CHALLENGE_TEXT
    unplanned = answering()._CHALLENGE_UNPLANNED
    assert texts["challenge_incomplete"] in intelligence["limitations"]
    assert texts["challenge_completed_without_contradiction"] not in intelligence["limitations"]
    assert texts["contradictory_evidence_found"] not in intelligence["limitations"]
    assert not any(
        text.startswith("Challenge requirements: ") for text in intelligence["limitations"]
    )
    assert intelligence["missing_work"] == [unplanned]
    assert intelligence["status"] == "partial"
    assert "Missing work: " + unplanned in result["answer"]
    state = answering().challenge_state(plan, context["snapshot"], intelligence["claims"])
    assert state["state"] == "challenge_incomplete"
    assert state["challenge_requirement_ids"] == []


def test_typed_v5_packet_carries_observation_purposes_and_challenge_requirement_ids():
    _, request, intake = inputs()
    plan = challenge_plan(
        request, intake, requirements=[*pair(), requirement("context_switching", "context")]
    )
    context = material(
        plan,
        request,
        intake,
        {
            "r_support": "Commuters in Soweto now share taxis to work.",
            "r_context": "Taxi ranks in Soweto open before dawn.",
        },
        fulfilled=["support_switching", "context_switching"],
    )
    value = typed_draft(
        context,
        [("supports", "r_support", 0, "support"), ("background", "r_context", 0, "context")],
        parents=["supports", "background"],
    )
    hydrated = hydrate(value, context)
    assert hydrated["evidence_purposes"] == {"supports": "support", "background": "context"}
    result = project(hydrated, context, value)
    intelligence = result["intelligence"]
    receipts = {row["receipt_id"]: row for row in intelligence["receipts"]}
    assert receipts["r_support"]["evidence_purposes"] == [
        {"claim_id": "supports", "evidence_purpose": "support"}
    ]
    assert receipts["r_context"]["evidence_purposes"] == [
        {"claim_id": "background", "evidence_purpose": "context"}
    ]
    assert "Challenge requirements: challenge_switching." in intelligence["limitations"]
    question = plan["requirements"][1]["question"]
    assert (
        "Challenge search incomplete: challenge_switching: " + question
        in intelligence["missing_work"]
    )
    assert intelligence["status"] == "partial"
    claims = claims_by_id(result)
    assert all(
        answering()._CHALLENGE_FINDING not in claims[cid]["limitations"]
        for cid in ("supports", "background")
    )
    tampered = copy.deepcopy(hydrated)
    tampered["evidence_purposes"]["background"] = "support"
    with pytest.raises(ValueError, match="answer_boundary_binding_invalid"):
        project(tampered, context, value)
    bare = hydrate(
        typed_draft(
            context,
            [("supports", "r_support", 0, "support"), ("background", "r_context", 0, "context")],
            derived=False,
        ),
        context,
    )
    assert project(bare, context)["intelligence"]["status"] == "partial"
    tampered = copy.deepcopy(bare)
    tampered["evidence_purposes"]["background"] = "challenge"
    with pytest.raises(ValueError, match="answer_purpose_invalid"):
        project(tampered, context)
    dropped = copy.deepcopy(bare)
    dropped["evidence_purposes"].pop("background")
    with pytest.raises(ValueError, match="answer_purpose_invalid"):
        project(dropped, context)


def test_planning_catalogue_binds_prompt_and_schema_digests_exactly():
    policy, request, intake = inputs()
    module = planning()
    requests = {
        name: module.build_question_planning_request(
            request, intake, policy=policy, remaining_seconds=60, _adapter=name
        )
        for name in ("source_window_v4", "challenge_v5")
    }
    for name, sdk in requests.items():
        assert module.planning_schema_digest(name) == sdk.response_schema_digest
        assert module.planning_adapter_id(sdk.system_instruction_digest) == name
        assert (
            module.planning_adapter_id(sdk.system_instruction_digest, sdk.response_schema_digest)
            == name
        )
    challenge, current = requests["challenge_v5"], requests["source_window_v4"]
    assert module.planning_schema_digest("challenge_v5", parent_aliases=["s01"]) not in {
        challenge.response_schema_digest,
        module.planning_schema_digest("challenge_v5", parent_aliases=[]),
    }
    for instruction, schema, aliases in (
        (challenge.system_instruction_digest, current.response_schema_digest, None),
        (current.system_instruction_digest, challenge.response_schema_digest, None),
        (challenge.system_instruction_digest, challenge.response_schema_digest, ["s01"]),
        (challenge.system_instruction_digest, challenge.response_schema_digest, []),
    ):
        with pytest.raises(ValueError, match="planning binding is invalid"):
            module.planning_adapter_id(instruction, schema, parent_aliases=aliases)
    with pytest.raises(ValueError, match="planning adapter is invalid"):
        module.planning_schema_digest("typed_v5")
