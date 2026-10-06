import copy
import json

import pytest
from google.genai import types

from tests.unit import test_general_question_answer as fixture


def setup():
    request, intake, plan, snapshot, usage, _, policy = fixture.fixture()
    context = {"request": request, "intake": intake, "plan": plan, "snapshot": snapshot}
    draft = fixture.typed_fixture(snapshot)
    for field in ("interpretations", "inferences", "proposals"):
        for claim in draft[field]:
            claim.pop("limitations")
            claim["boundary_codes"] = ["demographic_not_established"]
    return fixture.module(), context, usage, policy, draft


def call_for(subject, context, policy, draft, adapter="typed_v4"):
    sdk = subject.build_question_answering_request(
        **context,
        policy=policy,
        remaining_input_tokens=32000,
        remaining_output_tokens=4000,
        remaining_seconds=60,
        _adapter=adapter,
    )
    return {
        "contract_version": "general_question_known_response_v1",
        "binding": {
            **{
                key: getattr(sdk, key)
                for key in (
                    "input_digest",
                    "system_instruction_digest",
                    "response_schema_digest",
                    "model",
                )
            },
            **{
                key: context["request"][key]
                for key in ("request_id", "request_digest", "policy_digest")
            },
            "intake_digest": context["intake"]["intake_digest"],
            "deployment_digest": context["snapshot"]["deployment_digest"],
            "stage": "answering",
        },
        "response_model": sdk.model,
        "received_at": "2026-09-06T10:00:00Z",
        "raw_sdk_response": types.GenerateContentResponse(
            model_version=sdk.model,
            candidates=[
                types.Candidate(
                    content=types.Content(parts=[types.Part(text=json.dumps(draft))]),
                    finish_reason="STOP",
                )
            ],
        ).model_dump(mode="json"),
    }


def project(subject, context, usage, policy, draft):
    hydrated = subject._hydrate_typed_answer(draft, **context, _adapter="typed_v4")
    return subject.project_question_answer(
        hydrated,
        **context,
        usage=usage,
        _span_mode=True,
        _structural_uncertainty=True,
        _answer_call=call_for(subject, context, policy, draft),
    )


def test_codes_remain_private_until_checked_server_projection():
    subject, context, usage, policy, draft = setup()
    hydrated = subject._hydrate_typed_answer(draft, **context, _adapter="typed_v4")
    derived = next(c for c in hydrated["claims"] if c["claim_id"] == "infer")
    assert derived["boundary_codes"] == ["demographic_not_established"]
    assert derived["limitations"] == []
    result = project(subject, context, usage, policy, draft)
    assert (
        "This evidence does not establish demographic differences or breakdowns."
        in result["intelligence"]["limitations"]
    )
    assert all("boundary_codes" not in claim for claim in result["intelligence"]["claims"])


@pytest.mark.parametrize(
    "codes",
    [
        [],
        ["unknown"],
        ["Women switch stores."],
        ["population_not_established"] * 2,
        ["population_not_established"] * 5,
    ],
)
def test_invalid_codes_refuse(codes):
    subject, context, _, _, draft = setup()
    draft["inferences"][0]["boundary_codes"] = codes
    with pytest.raises(ValueError):
        subject._hydrate_typed_answer(draft, **context, _adapter="typed_v4")


@pytest.mark.parametrize("field", ["text", "falsifier"])
@pytest.mark.parametrize(
    "text",
    [
        "Women prefer this option.",
        "This causes switching.",
        "The share is 20%.",
        "Most shoppers switch.",
    ],
)
def test_codes_cannot_authorize_generated_claims_or_falsifiers(field, text):
    subject, context, usage, policy, draft = setup()
    draft["proposals"][0][field] = text
    with pytest.raises(ValueError):
        project(subject, context, usage, policy, draft)


@pytest.mark.parametrize("extra", ["limitations", "notes"])
def test_provider_free_text_cannot_be_smuggled_beside_codes(extra):
    subject, context, _, _, draft = setup()
    draft["inferences"][0][extra] = [
        "This evidence does not establish demographic differences or breakdowns."
    ]
    with pytest.raises(ValueError):
        subject._hydrate_typed_answer(draft, **context, _adapter="typed_v4")


@pytest.mark.parametrize("mutation", ["moved", "free_text", "unrecorded", "legacy_call"])
def test_fabricated_hydration_cannot_enable_boundary_projection(mutation):
    subject, context, usage, policy, draft = setup()
    hydrated = subject._hydrate_typed_answer(draft, **context, _adapter="typed_v4")
    call = call_for(subject, context, policy, draft)
    if mutation == "moved":
        hydrated["claims"][0]["boundary_codes"] = hydrated["claims"][-1].pop("boundary_codes")
    elif mutation == "free_text":
        hydrated["claims"][-1]["limitations"] = [
            "This evidence does not establish demographic differences or breakdowns."
        ]
    elif mutation == "unrecorded":
        call = None
    else:
        call = call_for(subject, context, policy, draft, adapter="typed_v3")
    with pytest.raises(ValueError):
        subject.project_question_answer(
            hydrated,
            **context,
            usage=usage,
            _span_mode=True,
            _structural_uncertainty=True,
            _answer_call=call,
        )


@pytest.mark.parametrize("field", ["interpretations", "inferences", "proposals"])
def test_v4_retains_count_ancestry_refusal(field):
    subject, context, _, _, draft = setup()
    draft[field][0]["parent_claim_ids"].append("counted")
    with pytest.raises(ValueError):
        subject._hydrate_typed_answer(draft, **context, _adapter="typed_v4")


def test_legacy_duplicate_limitations_remain_exact():
    request, intake, plan, snapshot, usage, _, _ = fixture.fixture()
    draft = fixture.typed_fixture(snapshot)
    draft["inferences"][0]["limitations"] = ["Further evidence is needed."] * 2
    result = fixture.project_typed(
        draft, {"request": request, "intake": intake, "plan": plan, "snapshot": snapshot}, usage
    )
    assert (
        next(c for c in result["intelligence"]["claims"] if c["claim_id"] == "infer")["limitations"]
        == ["Further evidence is needed."] * 2
    )
