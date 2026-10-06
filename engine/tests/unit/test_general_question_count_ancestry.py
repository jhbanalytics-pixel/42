import copy

import pytest

from tests.unit import test_general_question_answer as fixture
from tests.unit import test_general_question_runtime as runtime_fixture


def active_context():
    request, intake, plan, snapshot, usage, _, policy = fixture.fixture()
    context = {"request": request, "intake": intake, "plan": plan, "snapshot": snapshot}
    subject = fixture.module()
    sdk = subject.build_question_answering_request(
        **context,
        policy=policy,
        remaining_input_tokens=32000,
        remaining_output_tokens=4000,
        remaining_seconds=60,
    )
    adapter = subject._answer_adapter(sdk.response_schema_digest, sdk.system_instruction_digest)
    return subject, context, usage, adapter


@pytest.mark.parametrize("field", ["interpretations", "inferences", "proposals"])
def test_active_answer_refuses_retrieval_count_in_derived_ancestry(field):
    subject, context, _, adapter = active_context()
    draft = fixture.typed_fixture(context["snapshot"], boundary_codes=True)
    draft[field][0]["parent_claim_ids"].append("counted")
    with pytest.raises(ValueError, match="answer_typed_invalid"):
        subject._hydrate_typed_answer(draft, **context, _adapter=adapter)


def test_active_answer_preserves_standalone_counts_and_exact_quote_ancestry():
    subject, context, usage, adapter = active_context()
    draft = fixture.typed_fixture(context["snapshot"], boundary_codes=True)
    before = copy.deepcopy(draft)
    hydrated = subject._hydrate_typed_answer(draft, **context, _adapter=adapter)
    from tests.unit.test_general_question_boundary_codes import call_for

    result = subject.project_question_answer(
        hydrated,
        **context,
        usage=usage,
        _span_mode=True,
        _structural_uncertainty=True,
        _answer_call=call_for(subject, context, fixture.fixture()[-1], draft),
    )
    assert draft == before
    claims = {row["claim_id"]: row for row in result["intelligence"]["claims"]}
    assert claims["counted"]["reading_ids"] == ["reading_count"]
    assert claims["counted"]["receipt_ids"] == ["receipt_tools"]
    assert claims["interpret"]["parent_claim_ids"] == ["observed"]
    assert result["intelligence"]["readings"][0]["value"] == 1


def test_historical_typed_adapter_keeps_prior_count_ancestry_behavior():
    subject, context, _, _ = active_context()
    draft = fixture.typed_fixture(context["snapshot"])
    draft["interpretations"][0]["parent_claim_ids"].append("counted")
    hydrated = subject._hydrate_typed_answer(draft, **context, _adapter="typed")
    assert next(row for row in hydrated["claims"] if row["claim_id"] == "interpret")[
        "parent_claim_ids"
    ] == ["observed", "counted"]


def test_non_retrieval_reading_contract_remains_unchanged():
    subject, context, _, adapter = active_context()
    context["snapshot"]["readings"][0]["method"] = "fixture_measurement"
    fixture.rehash(context["snapshot"])
    draft = fixture.typed_fixture(context["snapshot"], boundary_codes=True)
    draft["interpretations"][0]["parent_claim_ids"].append("counted")
    assert subject._hydrate_typed_answer(draft, **context, _adapter=adapter)


@pytest.mark.asyncio
async def test_active_runtime_refuses_count_ancestry_without_a_second_generation(monkeypatch):
    values = await runtime_fixture.answering_fixture(monkeypatch)
    draft = values[5]
    draft["interpretations"][0]["parent_claim_ids"].append("counted")
    observed = runtime_fixture.intercept(monkeypatch, draft)
    for _ in range(2):
        with pytest.raises(runtime_fixture.QuestionStoreError, match="answer_invalid"):
            await runtime_fixture.answer(values)
    assert len(observed) == 2
    store, _, invocation, *_ = values
    context = store.read_request(
        invocation["request_id"], scope=runtime_fixture.store_fixture.scope()
    )
    assert context["admission"]["execution"]["calls"]["answering"]["response"] is not None
    assert context["admission"]["reserved_microusd"] == 100000
