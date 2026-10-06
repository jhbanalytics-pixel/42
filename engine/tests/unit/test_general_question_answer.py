"""Grounded answer construction and deterministic public projection controls."""

import copy
import importlib
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.client_lens import ClientLensUnavailable
from src.analysis.open_intelligence.general_question_plan import validate_question_plan
from src.analysis.open_intelligence.general_question_policy import build_intake_context

from tests.unit import test_general_question_plan as planning
from tests.unit import test_general_question_policy as policy_fixture


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_answer")


def retained_request(request_id="9183090c-5307-4ab4-b731-0a1f58f5648c"):
    root = Path(
        os.environ.get(
            "OPEN_INTELLIGENCE_RETAINED_FIXTURES",
            Path(__file__).resolve().parents[1] / "fixtures/retained-questions",
        )
    )
    path = root / f"question-request-{request_id}"
    if not path.is_dir():
        pytest.skip("Retained request artifacts are not installed on this host")
    context = {
        name: json.loads((path / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("request", "intake", "plan", "snapshot")
    }
    call = json.loads((path / "calls/answering.json").read_text(encoding="utf-8"))
    from src.analysis.open_intelligence.general_question_policy import build_question_policy
    from src.analysis.open_intelligence.general_question_runtime import _draft_from_snapshot

    pricing = json.loads(
        (root / "QUESTION_ACTIVATION_READINESS_20260908.json").read_text(encoding="utf-8")
    )["pricing"]
    policy = build_question_policy(
        approval_contract_digest="519a658ad91b3b4d447298611d259421bbac5b7765d7c0f0049dc8bdd08c5857",
        pricing_verified_at=datetime.fromisoformat(pricing["verified_at"]),
        input_usd_per_million=pricing["input_usd_per_million"],
        output_usd_per_million=pricing["output_including_reasoning_usd_per_million"],
    )
    policy["limits"]["deadline_seconds"] = 180
    policy["policy_digest"] = canonical_digest(
        {key: value for key, value in policy.items() if key != "policy_digest"}
    )
    return context, call, _draft_from_snapshot(call), policy


def typed_fixture(snapshot, *, boundary_codes=False):
    option = module()._quote_spans(snapshot)[snapshot["receipts"][0]["receipt_id"]][0]
    draft = {
        "quote_observations": [
            {
                "claim_id": "observed",
                "receipt_id": "r1",
                "span_id": option["span_id"],
            }
        ],
        "reading_observations": [{"claim_id": "counted", "reading_id": "d1"}],
        "interpretations": [
            {
                "claim_id": "interpret",
                "text": "Interest in shared access is possible.",
                "parent_claim_ids": ["observed"],
                "limitations": ["Selected source only."],
            }
        ],
        "inferences": [
            {
                "claim_id": "infer",
                "text": "Hypothesis: Hypothesis: Shared repairs merit exploration.",
                "parent_claim_ids": ["interpret"],
                "limitations": ["Further evidence is needed."],
            }
        ],
        "answer": ["infer"],
        "proposals": [
            {
                "claim_id": "propose",
                "text": "Consider a shared tool trial.",
                "parent_claim_ids": ["infer"],
                "limitations": ["Participation remains uncertain."],
                "falsifier": "Stop if neighbours decline the trial.",
            }
        ],
    }
    if boundary_codes:
        for field in ("interpretations", "inferences", "proposals"):
            for claim in draft[field]:
                claim.pop("limitations")
                claim["boundary_codes"] = ["corroboration_not_established"]
    return draft


def project_typed(draft, context, usage):
    hydrated = module()._hydrate_typed_answer(draft, **context)
    return module().project_question_answer(
        hydrated, **context, usage=usage, _span_mode=True, _structural_uncertainty=True
    )


def test_retained_2b93_observation_limitations_are_not_provider_fields():
    context, _, draft, _ = retained_request("2b93ddf9-900a-4e4f-8446-22b07eb11992")
    assert draft["quote_observations"][1]["claim_id"] == "c2"
    assert "one consumer" in draft["quote_observations"][1]["limitations"][0]
    schema = module()._typed_provider_schema()
    for field in ("quote_observations", "reading_observations"):
        assert "limitations" not in schema["properties"][field]["items"]["properties"]
    current = copy.deepcopy(draft)
    for field in ("quote_observations", "reading_observations"):
        for observation in current[field]:
            observation.pop("limitations")
    result = project_typed(current, context, fixture()[4])
    intelligence = result["intelligence"]
    assert intelligence["status"] == "partial"
    assert len(intelligence["claims"]) == 7
    assert len(result["sources"]) == 13
    assert intelligence["window"] == {"start": "2026-09-01", "end": "2026-09-07", "closed": True}
    assert intelligence["resolved_scope"]["market_scope"] == ["za"]
    assert intelligence["sections"] == [
        {"kind": "answer", "claim_ids": ["c1", "c2", "c3", "c4", "c5", "c6"]},
        {"kind": "actions", "claim_ids": ["c7"]},
    ]
    claims = {claim["claim_id"]: claim for claim in intelligence["claims"]}
    for cid in ("c1", "c2"):
        assert claims[cid]["limitations"] == [
            "Source record only; prevalence and representativeness are not measured."
        ]
    assert claims["c3"]["limitations"] == [
        "Bounded reading only; population prevalence is not measured."
    ]
    for field in ("interpretations", "inferences", "proposals"):
        for original in draft[field]:
            projected = claims[original["claim_id"]]
            assert projected["limitations"] == original["limitations"]
            prefix = "" if field == "proposals" else "Hypothesis: "
            assert projected["text"] == prefix + original["text"]
    assert [source["citation_label"] for source in result["sources"]] == [
        f"R{i}" for i in range(1, 14)
    ]
    receipts = {row["receipt_id"]: row for row in context["snapshot"]["receipts"]}
    for receipt in intelligence["receipts"]:
        assert receipt["excerpt"] == receipts[receipt["receipt_id"]]["excerpt"]
    for field in ("quote_observations", "reading_observations"):
        altered = copy.deepcopy(current)
        altered[field][0]["limitations"] = ["Provider supplied prose."]
        with pytest.raises(ValueError, match="answer_typed_invalid"):
            project_typed(altered, context, fixture()[4])


def test_retained_2b93_typed_v1_binding_stays_exact_and_refused():
    context, call, draft, policy = retained_request("2b93ddf9-900a-4e4f-8446-22b07eb11992")
    binding = call["binding"]
    assert (
        module()._answer_adapter(
            binding["response_schema_digest"], binding["system_instruction_digest"]
        )
        == "typed_v1"
    )
    sdk = module().build_question_answering_request(
        **context,
        policy=policy,
        remaining_input_tokens=30197,
        remaining_output_tokens=3565,
        remaining_seconds=60,
        _adapter="typed_v1",
    )
    assert (
        sdk.input_digest
        == binding["input_digest"]
        == "f62546524287ba5a9f29ef20eb0456badab05af49678e22cc45f4211c20ab9ca"
    )
    assert (
        sdk.response_schema_digest
        == binding["response_schema_digest"]
        == "9a6eb5ed406bb51064e96a4c646522809a1ec1d92e4af904c13e048b934ea105"
    )
    assert (
        sdk.system_instruction_digest
        == binding["system_instruction_digest"]
        == "0314aabe414cf603f2655f0dabf5dbfda9844e631fa528379bb1746e734465d6"
    )
    hydrated = module()._hydrate_typed_answer(draft, **context, _adapter="typed_v1")
    assert hydrated["claims"][1]["limitations"] == draft["quote_observations"][1]["limitations"]
    with pytest.raises(ValueError, match="answer_quantity_invalid"):
        module().project_question_answer(
            hydrated, **context, usage=fixture()[4], _span_mode=True, _structural_uncertainty=True
        )


def test_retained_9183090c_generic_shapes_are_not_representable_in_typed_provider():
    from jsonschema.exceptions import ValidationError
    from src.analysis.open_intelligence.canary_runtime import validate_response_schema

    context, call, draft, policy = retained_request()
    validate_response_schema(draft, module()._span_provider_schema(derived_references=True))
    assert len(draft["claims"][0]["segments"]) == 2
    assert draft["claims"][3]["kind"] == "observation"
    assert draft["claims"][3]["segments"][0]["kind"] == "text"
    assert draft["claims"][3]["parent_claim_ids"]
    with pytest.raises(ValueError, match="answer_alias_invalid"):
        module()._expand_answer_aliases(draft, **context, span_mode=True, derived_references=True)
    sdk = module().build_question_answering_request(
        **context,
        policy=policy,
        remaining_input_tokens=30192,
        remaining_output_tokens=3565,
        remaining_seconds=60,
    )
    assert sdk.response_schema_digest != call["binding"]["response_schema_digest"]
    schema = module()._typed_provider_schema()
    assert set(schema["properties"]) == {
        "quote_observations",
        "reading_observations",
        "interpretations",
        "inferences",
        "answer",
        "proposals",
    }
    for claim in (draft["claims"][0], draft["claims"][3]):
        altered = typed_fixture(context["snapshot"])
        altered["quote_observations"] = [claim]
        with pytest.raises(ValidationError):
            validate_response_schema(altered, schema)


def test_typed_hydration_preserves_sources_readings_sections_and_single_hypothesis():
    request, intake, plan, snapshot, usage, _, _ = fixture()
    context = {"request": request, "intake": intake, "plan": plan, "snapshot": snapshot}
    draft = typed_fixture(snapshot)
    before = copy.deepcopy(draft)
    result = project_typed(draft, context, usage)
    assert draft == before
    assert result["intelligence"]["sections"] == [
        {"kind": "answer", "claim_ids": ["infer"]},
        {"kind": "evidence", "claim_ids": ["observed", "counted"]},
        {"kind": "interpretation", "claim_ids": ["interpret"]},
        {"kind": "actions", "claim_ids": ["propose"]},
    ]
    indexed = {c["claim_id"]: c for c in result["intelligence"]["claims"]}
    assert indexed["observed"]["text"] == snapshot["receipts"][0]["excerpt"]
    assert indexed["counted"]["receipt_ids"] == ["receipt_tools"]
    assert indexed["counted"]["reading_ids"] == ["reading_count"]
    assert indexed["infer"]["text"] == "Hypothesis: Shared repairs merit exploration."
    assert result["sources"][0]["receipt_id"] == "receipt_tools"
    assert result["sources"][0]["citation_label"] == "R1"


HISTORICAL_ADAPTERS = [
    (
        "compact",
        "25a4704798f6180cb2823a926475fcf9ea789d19026839686d96b6b9c6f4121a",
        "4d01080ca62c3f94a209dc4952635162e0c1aa232aebd4e1e0e9e3e66d1e0f35",
        "899a9cb6f1a817149a6b4ea261fb71a984aa310dd24975091c8be6387bdb2f85",
    ),
    (
        "span_v1",
        "ae7f0719053d06165bc2599df0e13ead52aca95c53086002e242312fe51f400d",
        "124095f33aabcc2f7c5b6545c5d4ba45fd58d4aefbd222dd8a06946b5309bbd9",
        "f784baea74621500119c1d18023b8d97b76c46838105dfa678b29d7da4c12e19",
    ),
    (
        "span_v2",
        "ae7f0719053d06165bc2599df0e13ead52aca95c53086002e242312fe51f400d",
        "f7b6905ca63c184e98ee98044711f4d40d940d248b3ac47578dd710ed053cc22",
        "bf100a5350ee65beaf9b257789a203aba39d8afd7158608dee9f3eda8a6856fd",
    ),
    (
        "span_v3",
        "ae7f0719053d06165bc2599df0e13ead52aca95c53086002e242312fe51f400d",
        "f7b6905ca63c184e98ee98044711f4d40d940d248b3ac47578dd710ed053cc22",
        "1f520ed2ca9da69ba776a7f5633d38968a72bc27fa3bfa4859ee7ec4476158d7",
    ),
    (
        "typed_v1",
        "ae7f0719053d06165bc2599df0e13ead52aca95c53086002e242312fe51f400d",
        "9a6eb5ed406bb51064e96a4c646522809a1ec1d92e4af904c13e048b934ea105",
        "0314aabe414cf603f2655f0dabf5dbfda9844e631fa528379bb1746e734465d6",
    ),
]


@pytest.mark.parametrize(
    "adapter,input_digest,schema_digest,instruction_digest", HISTORICAL_ADAPTERS
)
def test_historical_adapter_bytes_are_exact(
    adapter, input_digest, schema_digest, instruction_digest
):
    context, call, _, policy = retained_request()
    sdk = module().build_question_answering_request(
        **context,
        policy=policy,
        remaining_input_tokens=30192,
        remaining_output_tokens=3565,
        remaining_seconds=60,
        _adapter=adapter,
    )
    assert (sdk.input_digest, sdk.response_schema_digest, sdk.system_instruction_digest) == (
        input_digest,
        schema_digest,
        instruction_digest,
    )
    assert module()._answer_adapter(schema_digest, instruction_digest) == adapter
    if adapter == "span_v3":
        for field in ("input_digest", "response_schema_digest", "system_instruction_digest"):
            assert getattr(sdk, field) == call["binding"][field]


def test_adapter_dispatch_refuses_unknown_and_crossed_digest_pairs():
    pairs = [(row[2], row[3]) for row in HISTORICAL_ADAPTERS]
    request, intake, plan, snapshot, _, _, policy = fixture()
    current = module().build_question_answering_request(
        request,
        intake,
        plan,
        snapshot,
        policy=policy,
        remaining_input_tokens=30000,
        remaining_output_tokens=3500,
        remaining_seconds=60,
    )
    pairs.append((current.response_schema_digest, current.system_instruction_digest))
    for schema in {p[0] for p in pairs} | {"0" * 64}:
        for instruction in {p[1] for p in pairs} | {"0" * 64}:
            if (schema, instruction) not in pairs:
                with pytest.raises(ValueError, match="answer_binding_invalid"):
                    module()._answer_adapter(schema, instruction)


def test_retained_source_selections_pass_typed_public_projection_without_prose_rewriting():
    context, _, raw, _ = retained_request()
    draft = {
        name: []
        for name in (
            "quote_observations",
            "reading_observations",
            "interpretations",
            "inferences",
            "answer",
            "proposals",
        )
    }
    parent_ids = {}
    for claim in raw["claims"][:3]:
        parent_ids[claim["claim_id"]] = []
        for index, segment in enumerate(claim["segments"]):
            cid = f"{claim['claim_id']}_{index}"
            parent_ids[claim["claim_id"]].append(cid)
            draft["quote_observations"].append(
                {
                    "claim_id": cid,
                    "receipt_id": segment["receipt_id"],
                    "span_id": segment["span_id"],
                }
            )
    summary = raw["claims"][3]
    draft["interpretations"] = [
        {
            "claim_id": summary["claim_id"],
            "text": summary["segments"][0]["text"],
            "parent_claim_ids": [
                cid for parent in summary["parent_claim_ids"] for cid in parent_ids[parent]
            ],
            "limitations": summary["limitations"],
        }
    ]
    draft["answer"] = [summary["claim_id"]]
    proposal = raw["claims"][-1]
    draft["proposals"] = [
        {
            "claim_id": proposal["claim_id"],
            "text": proposal["segments"][0]["text"],
            "parent_claim_ids": [summary["claim_id"]],
            "limitations": proposal["limitations"],
            "falsifier": proposal["falsifier"],
        }
    ]
    result = project_typed(draft, context, fixture()[4])
    assert result["intelligence"]["resolved_scope"]["market_scope"] == ["za"]
    assert result["intelligence"]["window"] == {
        "start": "2026-09-01",
        "end": "2026-09-07",
        "closed": True,
    }
    assert [s["kind"] for s in result["intelligence"]["sections"]] == [
        "answer",
        "evidence",
        "actions",
    ]
    assert [s["receipt_id"] for s in result["sources"]] == [
        context["snapshot"]["receipts"][i]["receipt_id"] for i in (1, 3, 0)
    ]
    assert [s["citation_label"] for s in result["sources"]] == ["R1", "R2", "R3"]
    for public, original in zip(
        result["intelligence"]["receipts"],
        (context["snapshot"]["receipts"][i] for i in (1, 3, 0)),
        strict=True,
    ):
        assert public["excerpt"] == original["excerpt"]
    claims = {c["claim_id"]: c for c in result["intelligence"]["claims"]}
    assert claims[summary["claim_id"]]["text"] == "Hypothesis: " + summary["segments"][0]["text"]
    assert claims[proposal["claim_id"]]["text"] == proposal["segments"][0]["text"]
    spans = module()._quote_spans(context["snapshot"])
    for quote in draft["quote_observations"]:
        source = context["snapshot"]["receipts"][int(quote["receipt_id"][1:]) - 1]
        span = next(s for s in spans[source["receipt_id"]] if s["span_id"] == quote["span_id"])
        assert claims[quote["claim_id"]]["text"] == source["excerpt"][span["start"] : span["end"]]


@pytest.mark.parametrize(
    "case",
    [
        "unknown_span",
        "unknown_reading",
        "source_alias",
        "original_receipt",
        "duplicate_id",
        "missing_parent",
        "forward_parent",
        "empty_parents",
        "duplicate_parent",
        "empty_limitations",
        "causal",
        "numeric",
        "falsifier",
        "empty_falsifier",
        "unknown_answer",
        "proposal_answer",
        "duplicate_answer",
        "empty_answer",
        "altered_source",
        "kind",
        "support_state",
        "section",
        "segments",
        "receipt_ids",
        "reading_ids",
        "observation_text",
        "observation_parents",
        "copied_quote_text",
    ],
)
def test_typed_provider_refuses_invalid_drafts_at_complete_projection(case):
    request, intake, plan, snapshot, usage, _, _ = fixture()
    context = {"request": request, "intake": intake, "plan": plan, "snapshot": snapshot}
    draft = typed_fixture(snapshot)
    quote = draft["quote_observations"][0]
    interpretation = draft["interpretations"][0]
    if case == "unknown_span":
        quote["span_id"] = "unknown"
    elif case == "unknown_reading":
        draft["reading_observations"][0]["reading_id"] = "unknown"
    elif case == "source_alias":
        quote["receipt_id"] = "d1"
    elif case == "original_receipt":
        quote["receipt_id"] = "receipt_tools"
    elif case == "duplicate_id":
        interpretation["claim_id"] = "observed"
    elif case == "missing_parent":
        interpretation["parent_claim_ids"] = ["missing"]
    elif case == "forward_parent":
        interpretation["parent_claim_ids"] = ["infer"]
    elif case == "empty_parents":
        interpretation["parent_claim_ids"] = []
    elif case == "duplicate_parent":
        interpretation["parent_claim_ids"] *= 2
    elif case == "empty_limitations":
        interpretation["limitations"] = []
    elif case == "causal":
        interpretation["text"] = "This may lead to a change."
    elif case == "numeric":
        interpretation["text"] = "There are two possible changes."
    elif case == "falsifier":
        draft["proposals"][0]["falsifier"] = "A quarter of shoppers agree."
    elif case == "empty_falsifier":
        draft["proposals"][0]["falsifier"] = ""
    elif case == "unknown_answer":
        draft["answer"] = ["unknown"]
    elif case == "proposal_answer":
        draft["answer"] = ["propose"]
    elif case == "duplicate_answer":
        draft["answer"] *= 2
    elif case == "empty_answer":
        draft["answer"] = []
    elif case == "altered_source":
        snapshot["receipts"][0]["excerpt"] = "Altered source."
        rehash(snapshot)
    elif case == "observation_text":
        quote["text"] = "Made up."
    elif case == "observation_parents":
        quote["parent_claim_ids"] = ["interpret"]
    elif case == "copied_quote_text":
        quote["text"] = snapshot["receipts"][0]["excerpt"]
    else:
        interpretation[case] = []
    with pytest.raises(ValueError):
        project_typed(draft, context, usage)


def fixture():
    policy = policy_fixture._policy()
    request = planning.request()
    request["policy_digest"] = policy["policy_digest"]
    request["request_digest"] = canonical_digest(
        {key: value for key, value in request.items() if key != "request_digest"}
    )
    intake = build_intake_context(request, selected_market="za")
    draft = planning.draft()
    plan = validate_question_plan(draft, request=request, intake=intake)
    receipt = {
        "receipt_id": "receipt_tools",
        "citation_label": "R9",
        "kind": "content",
        "snapshot_id": "snapshot_tools",
        "market": "za",
        "source_label": "Repair notes",
        "source_family": "community",
        "platform": None,
        "author": None,
        "url": None,
        "source_row_id": "row_tools",
        "published_at": None,
        "collected_at": None,
        "excerpt": "Neighbours share tools at the repair café.",
        "reading_ids": ["reading_count"],
        "limitations": ["Publication time and URL are unavailable."],
        "content_digest": "a" * 64,
    }
    reading = {
        "reading_id": "reading_count",
        "value": 1,
        "unit": "records",
        "window": plan["window"],
        "method": "selected_receipt_count",
        "denominator": None,
        "source_receipt_ids": [receipt["receipt_id"]],
        "limitations": ["Selected sample only."],
    }
    snapshot = {
        "contract_version": "general_question_snapshot_v1",
        "request_id": request["request_id"],
        "request_digest": request["request_digest"],
        "intake_digest": intake["intake_digest"],
        "plan_digest": plan["plan_digest"],
        "policy_digest": request["policy_digest"],
        "deployment_digest": "b" * 64,
        "snapshot_id": "snapshot_tools",
        "as_of": request["as_of"],
        "window": plan["window"],
        "receipts": [receipt],
        "readings": [reading],
        "limitations": ["A selected sample, not a population estimate."],
        "missing_work": [],
        "fulfilled_requirement_ids": [item["requirement_id"] for item in plan["requirements"]],
    }
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    observation = {
        "claim_id": "observed",
        "kind": "observation",
        "segments": [{"kind": "quote", "receipt_id": "receipt_tools", "text": receipt["excerpt"]}],
        "receipt_ids": ["receipt_tools"],
        "reading_ids": [],
        "parent_claim_ids": [],
        "support_state": "source_record",
        "limitations": [],
        "falsifier": None,
    }
    interpretation = {
        "claim_id": "interpret",
        "kind": "interpretation",
        "segments": [{"kind": "text", "text": "This may suggest interest in shared access."}],
        "receipt_ids": [],
        "reading_ids": [],
        "parent_claim_ids": ["observed"],
        "support_state": "derived",
        "limitations": ["A hypothesis limited to the selected source."],
        "falsifier": None,
    }
    proposal = {
        "claim_id": "propose",
        "kind": "proposal",
        "segments": [{"kind": "text", "text": "Consider a shared tool trial."}],
        "receipt_ids": [],
        "reading_ids": [],
        "parent_claim_ids": ["observed"],
        "support_state": "proposed",
        "limitations": ["Participation remains uncertain."],
        "falsifier": "Stop if neighbours decline the trial.",
    }
    output = {
        "claims": [proposal, interpretation, observation],
        "sections": [
            {"kind": "answer", "claim_ids": ["observed"]},
            {"kind": "interpretation", "claim_ids": ["interpret"]},
            {"kind": "actions", "claim_ids": ["propose"]},
        ],
    }
    usage = {
        "status": "resolved",
        "model_calls": 2,
        "input_tokens": 100,
        "output_tokens": 50,
        "usage_receipt_ids": ["usage_planning", "usage_answering"],
        "call_receipt_ids": ["call_planning", "call_answering"],
        "reservation_ids": [request["request_id"]],
        "reserved_cost_usd": "0.100000",
        "reason": None,
    }
    return request, intake, plan, snapshot, usage, output, policy


def project(values):
    request, intake, plan, snapshot, usage, output, _policy = values
    return module().project_question_answer(
        output, request=request, intake=intake, plan=plan, snapshot=snapshot, usage=usage
    )


def rehash(snapshot):
    snapshot["snapshot_digest"] = canonical_digest(
        {k: v for k, v in snapshot.items() if k != "snapshot_digest"}
    )


@pytest.mark.parametrize(
    "published_at,admitted",
    [
        ("2026-08-23T00:00:00+00:00", True),
        ("2026-09-05T23:59:59+00:00", True),
        ("2019-10-30T05:45:03+00:00", False),
        ("2026-08-22T23:59:59+00:00", False),
    ],
)
def test_an_item_published_outside_the_window_never_counts_as_evidence_of_it(
    published_at, admitted
):
    """A post collected inside the window but published years earlier is not a window event.

    A capture can admit an old item by its collection time; its own publication time
    still has to fall inside the question window before an answer may cite it.
    """
    values = fixture()
    snapshot = values[3]
    snapshot["receipts"][0]["published_at"] = published_at
    snapshot["receipts"][0]["collected_at"] = "2026-09-01T08:00:00+00:00"
    rehash(snapshot)
    if admitted:
        assert project(values)["intelligence"]["status"] == "complete"
    else:
        with pytest.raises(ValueError, match=r"^answer_receipt_invalid$"):
            project(values)


def test_grounded_answer_and_proposal_render_from_sections_with_first_citation():
    values = fixture()
    before = copy.deepcopy(values)
    result = project(values)
    intelligence = result["intelligence"]
    assert set(result) == {"answer", "sources", "intelligence"}
    assert intelligence["status"] == "complete"
    assert intelligence["review_required"] is True
    assert intelligence["ready_for_downstream"] is False
    assert intelligence["readings"] == []
    assert intelligence["receipts"][0]["reading_ids"] == []
    assert intelligence["receipts"][0]["citation_label"] == "R1"
    assert result["sources"][0]["url"] is None
    assert result["answer"].startswith("Neighbours share tools at the repair café. [R1]")
    assert "Proposal: Consider a shared tool trial." in result["answer"]
    assert values == before


@pytest.mark.parametrize(
    "case",
    [
        "quote",
        "quantity",
        "word_quantity",
        "demographic",
        "causal",
        "reading_unit",
        "reading_unknown",
        "cross_snapshot",
        "snapshot_digest",
        "reading_value",
        "parent",
        "uncertainty",
        "usage_unknown",
        "plan_binding",
    ],
)
def test_unsupported_projection_refuses(case):
    values = fixture()
    _request, _intake, _plan, snapshot, usage, output, _policy = values
    observe = output["claims"][2]
    if case == "quote":
        observe["segments"][0]["text"] = "Everybody shares tools."
    elif case in ("quantity", "word_quantity", "demographic", "causal"):
        text = {
            "quantity": "Participation rose 99%.",
            "word_quantity": "One hundred people agree.",
            "demographic": "Women prefer shared access.",
            "causal": "This causes shared access.",
        }[case]
        output["claims"][1]["segments"][0]["text"] = text
    elif case in ("reading_unit", "reading_unknown"):
        observe["segments"] = [{"kind": "reading", "reading_id": "reading_count"}]
        observe["reading_ids"] = ["reading_count"]
        if case == "reading_unit":
            observe["segments"][0]["unit"] = "people"
        else:
            observe["segments"][0]["reading_id"] = "missing"
    elif case == "cross_snapshot":
        snapshot["receipts"][0]["snapshot_id"] = "foreign"
        rehash(snapshot)
    elif case == "snapshot_digest":
        snapshot["receipts"][0]["excerpt"] = "changed"
    elif case == "reading_value":
        snapshot["readings"][0]["value"] = 999
        rehash(snapshot)
    elif case == "parent":
        output["claims"][1]["parent_claim_ids"] = ["propose"]
    elif case == "uncertainty":
        output["claims"][1]["limitations"] = []
    elif case == "usage_unknown":
        usage.update(status="unresolved", output_tokens=None, reason="response_usage_unavailable")
    else:
        snapshot["plan_digest"] = "c" * 64
        rehash(snapshot)
    with pytest.raises(ValueError):
        project(values)


def test_typed_reading_observation_and_missing_requirement_are_partial():
    values = fixture()
    observation = values[5]["claims"][2]
    observation["segments"] = [{"kind": "reading", "reading_id": "reading_count"}]
    observation["reading_ids"] = ["reading_count"]
    values[3]["fulfilled_requirement_ids"] = []
    rehash(values[3])
    result = project(values)
    assert result["intelligence"]["status"] == "partial"
    assert "1 records" in result["answer"]
    assert result["intelligence"]["missing_work"]


def test_first_citation_order_is_independent_of_snapshot_and_claim_array_order():
    values = fixture()
    snapshot, output = values[3], values[5]
    second = {
        **copy.deepcopy(snapshot["receipts"][0]),
        "receipt_id": "receipt_opposing",
        "citation_label": "R3",
        "source_row_id": "row_opposing",
        "reading_ids": [],
        "excerpt": "Neighbours prefer borrowing from friends.",
    }
    snapshot["receipts"].append(second)
    rehash(snapshot)
    observation = {
        **copy.deepcopy(output["claims"][2]),
        "claim_id": "opposing",
        "receipt_ids": [second["receipt_id"]],
        "segments": [
            {"kind": "quote", "receipt_id": second["receipt_id"], "text": second["excerpt"]}
        ],
    }
    output["claims"].append(observation)
    output["sections"][0]["claim_ids"].insert(0, "opposing")
    result = project(values)
    assert [item["receipt_id"] for item in result["sources"]] == [
        "receipt_opposing",
        "receipt_tools",
    ]
    assert result["answer"].startswith(second["excerpt"] + " [R1]")
    assert "Neighbours share tools at the repair café. [R2]" in result["answer"]


def test_answer_request_binds_full_context_and_actual_remaining_budget():
    request, intake, plan, snapshot, _usage, _output, policy = fixture()
    sdk = module().build_question_answering_request(
        request,
        intake,
        plan,
        snapshot,
        policy=policy,
        remaining_input_tokens=30000,
        remaining_output_tokens=3500,
        remaining_seconds=12,
    )
    assert sdk.model == policy["model"]
    assert sdk.generation_config.max_output_tokens == 3500
    assert sdk.generation_config.thinking_config.thinking_level.value == "LOW"
    view = json.loads(sdk.contents)["snapshot"]
    assert view["snapshot_digest"] == snapshot["snapshot_digest"]
    assert view["receipts"][0]["excerpt"] == snapshot["receipts"][0]["excerpt"]
    assert sdk.input_digest == canonical_digest(json.loads(sdk.contents))
    assert sdk.generation_config.http_options.timeout == 12000


def test_private_validation_material_never_enters_model_input():
    request, intake, plan, snapshot, _usage, _output, policy = fixture()
    snapshot["provenance"] = {"source": {"rows": ["unselected private validation material"]}}
    snapshot["snapshot_digest"] = canonical_digest(
        {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
    )
    sdk = module().build_question_answering_request(
        request,
        intake,
        plan,
        snapshot,
        policy=policy,
        remaining_input_tokens=24000,
        remaining_output_tokens=3200,
        remaining_seconds=30,
    )
    payload = json.loads(sdk.contents)
    assert "provenance" not in payload["snapshot"]
    assert "unselected private validation material" not in sdk.contents
    assert [row["excerpt"] for row in payload["snapshot"]["receipts"]] == [
        row["excerpt"] for row in snapshot["receipts"]
    ]
    assert payload["snapshot"]["snapshot_digest"] == snapshot["snapshot_digest"]
    assert sdk.count_tokens_config.system_instruction == sdk.generation_config.system_instruction


@pytest.mark.parametrize("budget", [0, -1, True, 4001])
def test_answer_builder_refuses_invalid_remaining_output(budget):
    request, intake, plan, snapshot, _usage, _output, policy = fixture()
    with pytest.raises(ValueError):
        module().build_question_answering_request(
            request,
            intake,
            plan,
            snapshot,
            policy=policy,
            remaining_input_tokens=30000,
            remaining_output_tokens=budget,
            remaining_seconds=12,
        )


def test_answer_provider_projection_removes_only_lengths_and_binds_digest():
    values = fixture()
    request, intake, plan, snapshot, _, _, policy = values
    before = copy.deepcopy(module().GENERAL_QUESTION_ANSWER_SCHEMA)
    sdk = module().build_question_answering_request(
        request,
        intake,
        plan,
        snapshot,
        policy=policy,
        remaining_input_tokens=30000,
        remaining_output_tokens=3200,
        remaining_seconds=30,
    )
    projected = module()._typed_provider_schema(boundary_codes=True)
    assert sdk.response_schema_digest == canonical_digest(projected)
    from google.genai import types

    assert sdk.count_tokens_config.generation_config.response_schema == types.Schema.model_validate(
        sdk.generation_config.response_schema
    )

    def visit(value):
        if isinstance(value, dict):
            assert not set(value) & {"minItems", "maxItems", "minLength", "maxLength"}
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(projected)
    assert "anyOf" not in json.dumps(projected)
    assert "segments" not in json.dumps(projected)
    assert before == module().GENERAL_QUESTION_ANSWER_SCHEMA
    for invalid in [
        dict(values[5], claims=values[5]["claims"] * 41),
        dict(values[5], unknown="bad"),
    ]:
        changed = list(values)
        changed[5] = invalid
        with pytest.raises(ValueError):
            project(tuple(changed))


def test_model_view_aliases_preserve_evidence_and_strict_expansion():
    request, intake, plan, snapshot, usage, output, policy = fixture()
    before = copy.deepcopy(snapshot)
    sdk = module().build_question_answering_request(
        request,
        intake,
        plan,
        snapshot,
        policy=policy,
        remaining_input_tokens=30000,
        remaining_output_tokens=3500,
        remaining_seconds=30,
    )
    view = json.loads(sdk.contents)
    assert view["snapshot"]["snapshot_digest"] == snapshot["snapshot_digest"]
    assert view["snapshot"]["receipts"][0]["receipt_id"] == "r1"
    assert view["snapshot"]["receipts"][0]["excerpt"] == snapshot["receipts"][0]["excerpt"]
    assert "content_digest" not in view["snapshot"]["receipts"][0]
    assert sdk.input_digest == canonical_digest(view)
    aliased = copy.deepcopy(output)
    for claim in aliased["claims"]:
        claim["receipt_ids"] = ["r1" for _ in claim["receipt_ids"]]
        for segment in claim["segments"]:
            if segment["kind"] == "quote":
                segment["receipt_id"] = "r1"
    expanded = module()._expand_answer_aliases(
        aliased, request=request, intake=intake, plan=plan, snapshot=snapshot
    )
    assert expanded == output
    assert snapshot == before
    assert module().project_question_answer(
        expanded, request=request, intake=intake, plan=plan, snapshot=snapshot, usage=usage
    ) == project((request, intake, plan, snapshot, usage, output, policy))
    bad = copy.deepcopy(aliased)
    bad["claims"][0]["receipt_ids"] = [snapshot["receipts"][0]["receipt_id"]]
    with pytest.raises(ValueError):
        module()._expand_answer_aliases(
            bad, request=request, intake=intake, plan=plan, snapshot=snapshot
        )


def test_forty_receipts_two_readings_alias_bijection_and_strict_references():
    request, intake, plan, snapshot, usage, _, _ = fixture()
    base = snapshot["receipts"][0]
    snapshot["receipts"] = [
        {
            **copy.deepcopy(base),
            "receipt_id": f"full_receipt_{n}",
            "citation_label": f"R{n + 1}",
            "reading_ids": [f"full_reading_{n % 2}"],
        }
        for n in range(40)
    ]
    snapshot["readings"] = [
        {
            "reading_id": f"full_reading_{n}",
            "value": 20,
            "unit": "records",
            "window": snapshot["window"],
            "method": "selected_receipt_count",
            "denominator": None,
            "source_receipt_ids": [row["receipt_id"] for row in snapshot["receipts"][n::2]],
            "limitations": [],
        }
        for n in range(2)
    ]
    rehash(snapshot)
    before = copy.deepcopy(snapshot)
    view, receipt_map, reading_map = module()._answer_model_view(request, intake, plan, snapshot)
    assert len(view["snapshot"]["receipts"]) == 40
    assert len(view["snapshot"]["readings"]) == 2
    assert view["snapshot"]["snapshot_digest"] == snapshot["snapshot_digest"]
    assert view == module()._answer_model_view(request, intake, plan, snapshot)[0]
    for original, aliased in zip(snapshot["receipts"], view["snapshot"]["receipts"], strict=True):
        restored = {
            **aliased,
            "receipt_id": receipt_map[aliased["receipt_id"]],
            "reading_ids": [reading_map[v] for v in aliased["reading_ids"]],
        }
        assert restored == {
            k: v
            for k, v in original.items()
            if k not in ("content_digest", "snapshot_id", "source_row_id")
        }
    for original, aliased in zip(snapshot["readings"], view["snapshot"]["readings"], strict=True):
        assert {
            **aliased,
            "reading_id": reading_map[aliased["reading_id"]],
            "source_receipt_ids": [receipt_map[v] for v in aliased["source_receipt_ids"]],
        } == original
    output = {
        "claims": [
            {
                "claim_id": "observed",
                "kind": "observation",
                "segments": [{"kind": "reading", "reading_id": "d1"}],
                "receipt_ids": view["snapshot"]["readings"][0]["source_receipt_ids"],
                "reading_ids": ["d1"],
                "parent_claim_ids": [],
                "support_state": "source_record",
                "limitations": [],
                "falsifier": None,
            }
        ],
        "sections": [{"kind": "answer", "claim_ids": ["observed"]}],
    }
    expanded = module()._expand_answer_aliases(
        output, request=request, intake=intake, plan=plan, snapshot=snapshot
    )
    module().project_question_answer(
        expanded, request=request, intake=intake, plan=plan, snapshot=snapshot, usage=usage
    )
    assert output["claims"][0]["segments"][0]["reading_id"] == "d1"
    for bad in ("r1", "full_reading_0", "unknown"):
        mutated = copy.deepcopy(output)
        mutated["claims"][0]["segments"][0]["reading_id"] = bad
        with pytest.raises(ValueError):
            module()._expand_answer_aliases(
                mutated, request=request, intake=intake, plan=plan, snapshot=snapshot
            )
    for bad in ("d1", "full_receipt_0", "unknown"):
        mutated = copy.deepcopy(output)
        mutated["claims"][0]["receipt_ids"][0] = bad
        with pytest.raises(ValueError):
            module()._expand_answer_aliases(
                mutated, request=request, intake=intake, plan=plan, snapshot=snapshot
            )
    duplicate = copy.deepcopy(output)
    duplicate["claims"][0]["receipt_ids"].append("r1")
    expanded = module()._expand_answer_aliases(
        duplicate, request=request, intake=intake, plan=plan, snapshot=snapshot
    )
    with pytest.raises(ValueError):
        module().project_question_answer(
            expanded, request=request, intake=intake, plan=plan, snapshot=snapshot, usage=usage
        )
    assert snapshot == before


@pytest.mark.parametrize("reference", ["unknown", "d1", "receipt_tools"])
def test_quote_alias_refuses_unknown_cross_kind_and_original_id(reference):
    request, intake, plan, snapshot, _, output, _ = fixture()
    draft = copy.deepcopy(output)
    for claim in draft["claims"]:
        claim["receipt_ids"] = ["r1" for _ in claim["receipt_ids"]]
        for segment in claim["segments"]:
            if segment["kind"] == "quote":
                segment["receipt_id"] = reference
    with pytest.raises(ValueError):
        module()._expand_answer_aliases(
            draft, request=request, intake=intake, plan=plan, snapshot=snapshot
        )
    changed = copy.deepcopy(snapshot)
    changed["receipts"][0]["excerpt"] = "changed"
    with pytest.raises(ValueError):
        module()._answer_model_view(request, intake, plan, changed)


def test_exact_span_adapter_preserves_source_and_constructs_unique_sections():
    request, intake, plan, snapshot, usage, output, _ = fixture()
    before = copy.deepcopy(snapshot)
    spans = module()._quote_spans(snapshot)
    rid = snapshot["receipts"][0]["receipt_id"]
    option = spans[rid][0]
    assert snapshot["receipts"][0]["excerpt"][option["start"] : option["end"]] == option["text"]
    provider = copy.deepcopy(output)
    sections = {
        cid: section["kind"] for section in provider.pop("sections") for cid in section["claim_ids"]
    }
    for claim in provider["claims"]:
        claim["section"] = sections[claim["claim_id"]]
        claim["receipt_ids"] = ["r1" for _ in claim["receipt_ids"]]
        for segment in claim["segments"]:
            if segment["kind"] == "quote":
                segment.clear()
                segment.update(kind="quote_span", receipt_id="r1", span_id=option["span_id"])
    expanded = module()._expand_answer_aliases(
        provider, request=request, intake=intake, plan=plan, snapshot=snapshot, span_mode=True
    )
    module().project_question_answer(
        expanded,
        request=request,
        intake=intake,
        plan=plan,
        snapshot=snapshot,
        usage=usage,
        _span_mode=True,
    )
    assert snapshot == before
    for bad in ("unknown", "r2"):
        altered = copy.deepcopy(provider)
        altered["claims"][0]["segments"][0]["span_id"] = bad
        with pytest.raises(ValueError):
            module()._expand_answer_aliases(
                altered,
                request=request,
                intake=intake,
                plan=plan,
                snapshot=snapshot,
                span_mode=True,
            )
    changed = copy.deepcopy(snapshot)
    changed["receipts"][0]["excerpt"] = "Cost is 42. No increase is established."
    rehash(changed)
    options = module()._quote_spans(changed)[rid]
    assert [item["text"] for item in options] == ["No increase is established."]
    assert options[0]["span_id"] != option["span_id"]


def test_span_boundaries_and_missing_semantic_limitations_stay_strict():
    request, intake, plan, snapshot, usage, output, _ = fixture()
    row = snapshot["receipts"][0]
    row["excerpt"] = "Not 42.5 units. No increase is established. No increase is established."
    rehash(snapshot)
    options = module()._quote_spans(snapshot)[row["receipt_id"]]
    assert [item["text"] for item in options] == [
        "No increase is established.",
        "No increase is established.",
    ]
    assert options[0]["span_id"] != options[1]["span_id"]
    for option in options:
        assert row["excerpt"][option["start"] : option["end"]] == option["text"]
    request, intake, plan, snapshot, usage, output, _ = fixture()
    next(claim for claim in output["claims"] if claim["kind"] == "proposal")["limitations"] = []
    with pytest.raises(ValueError, match="answer_interpretation_invalid"):
        module().project_question_answer(
            output, request=request, intake=intake, plan=plan, snapshot=snapshot, usage=usage
        )


def test_span_v2_derives_links_and_refuses_provider_parallel_arrays():
    request, intake, plan, snapshot, usage, _, _ = fixture()
    option = module()._quote_spans(snapshot)[snapshot["receipts"][0]["receipt_id"]][0]
    draft = {
        "claims": [
            {
                "claim_id": "obs",
                "kind": "observation",
                "section": "answer",
                "segments": [
                    {"kind": "quote_span", "receipt_id": "r1", "span_id": option["span_id"]}
                ],
                "parent_claim_ids": [],
                "support_state": "source_record",
                "limitations": [],
                "falsifier": None,
            }
        ]
    }
    expanded = module()._expand_answer_aliases(
        draft,
        request=request,
        intake=intake,
        plan=plan,
        snapshot=snapshot,
        span_mode=True,
        derived_references=True,
    )
    assert expanded["claims"][0]["receipt_ids"] == [snapshot["receipts"][0]["receipt_id"]]
    assert expanded["claims"][0]["reading_ids"] == []
    module().project_question_answer(
        expanded,
        request=request,
        intake=intake,
        plan=plan,
        snapshot=snapshot,
        usage=usage,
        _span_mode=True,
    )
    for field in ("receipt_ids", "reading_ids"):
        altered = copy.deepcopy(draft)
        altered["claims"][0][field] = []
        with pytest.raises(ValueError):
            module()._expand_answer_aliases(
                altered,
                request=request,
                intake=intake,
                plan=plan,
                snapshot=snapshot,
                span_mode=True,
                derived_references=True,
            )
    with pytest.raises(ValueError):
        module()._guard_prose("This may lead to a change.")


def test_structural_hypothesis_label_and_calendar_falsifier_are_narrow():
    request, intake, plan, snapshot, usage, output, _ = fixture()
    proposal = next(c for c in output["claims"] if c["kind"] == "proposal")
    proposal["falsifier"] = (
        "Reject the proposal if feedback does not improve over the following quarter."
    )
    interpretation = next(c for c in output["claims"] if c["kind"] == "interpretation")
    interpretation["segments"] = [
        {"kind": "text", "text": "There are indications of interest in shared repairs."}
    ]
    with pytest.raises(ValueError):
        module().project_question_answer(
            output, request=request, intake=intake, plan=plan, snapshot=snapshot, usage=usage
        )
    result = module().project_question_answer(
        output,
        request=request,
        intake=intake,
        plan=plan,
        snapshot=snapshot,
        usage=usage,
        _structural_uncertainty=True,
    )
    assert "Hypothesis: There are indications" in result["answer"]
    for text in (
        "A quarter of shoppers agree.",
        "Review after 2 quarters.",
        "Review the next quarter of sales.",
    ):
        proposal["falsifier"] = text
        with pytest.raises(ValueError):
            module().project_question_answer(
                output,
                request=request,
                intake=intake,
                plan=plan,
                snapshot=snapshot,
                usage=usage,
                _structural_uncertainty=True,
            )


BSA_SCOPE = {
    "client_scope_id": "bsa_pulse",
    "market_scope": ["ke", "ng", "za"],
    "brand_config_id": "bsa",
    "audience_lens_ids": [],
    "theme_id": None,
}
PROHIBITED_QUESTION = "How is the election being discussed by Global South Africans?"
PROHIBITED_INTERPRETATION = "This may suggest election messaging is reaching undecided voters."


def scoped(values, *, question, scope, interpretation_text, client_lens=None):
    request, intake, plan, snapshot, usage, output, policy = copy.deepcopy(values)
    request = planning.request(question=question, scope=scope)
    request["policy_digest"] = policy["policy_digest"]
    if client_lens is not None:
        request["client_lens"] = copy.deepcopy(client_lens)
    request["request_digest"] = canonical_digest(
        {key: value for key, value in request.items() if key != "request_digest"}
    )
    intake = build_intake_context(request, selected_market="za")
    plan = validate_question_plan(planning.draft(), request=request, intake=intake)
    snapshot.update(
        request_id=request["request_id"],
        request_digest=request["request_digest"],
        intake_digest=intake["intake_digest"],
        plan_digest=plan["plan_digest"],
        policy_digest=request["policy_digest"],
        as_of=request["as_of"],
        window=plan["window"],
    )
    rehash(snapshot)
    output["claims"][1]["segments"][0]["text"] = interpretation_text
    usage["reservation_ids"] = [request["request_id"]]
    return request, intake, plan, snapshot, usage, output, policy


def test_bsa_scope_refuses_a_prohibited_purpose_at_projection_and_general_42_projects_it():
    prohibited = scoped(
        fixture(),
        question=PROHIBITED_QUESTION,
        scope=BSA_SCOPE,
        interpretation_text=PROHIBITED_INTERPRETATION,
    )
    with pytest.raises(ValueError) as caught:
        project(prohibited)
    assert str(caught.value) == "client_purpose_refused_at_projection"
    assert set(caught.value.purpose_codes) >= {"electoral_purpose", "voter_targeting_purpose"}
    assert caught.value.stage == "projection"

    general = scoped(
        fixture(),
        question=PROHIBITED_QUESTION,
        scope={**BSA_SCOPE, "client_scope_id": "synthetic_scope", "brand_config_id": None},
        interpretation_text=PROHIBITED_INTERPRETATION,
    )
    result = project(general)
    assert result["intelligence"]["status"] == "complete"
    assert PROHIBITED_INTERPRETATION in result["answer"]

    permitted = scoped(
        fixture(),
        question="What are Global South Africans saying about load shedding?",
        scope=BSA_SCOPE,
        interpretation_text="This may suggest interest in shared access.",
    )
    assert project(permitted)["intelligence"]["status"] == "complete"


BSA_LENS_BINDING = {
    "client_lens_id": "bsa_pulse_lens",
    "configuration_digest": "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf",
    "lens_binding_version": "client_lens_binding_v1",
}


def test_projection_runs_the_policy_the_lens_authorizes_not_the_one_the_brand_name_picks():
    """Projection selects a configuration the way admission does: through the registry.

    The stored request carries the lens it was admitted under. Projection hands
    the purpose gate that resolved lens, and hands it the request's own client
    scope beside the brand name, so the configuration the policy runs under is
    the one an entry authorizes rather than whatever a brand name loads.
    """
    seen = []
    answer = module()
    original = answer.refuse_prohibited_client_purpose

    def record(scope, texts, *, stage, root=None, lens=None):
        seen.append((dict(scope), lens))
        return original(scope, texts, stage=stage, root=root, lens=lens)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(answer, "refuse_prohibited_client_purpose", record)
        without = scoped(
            fixture(),
            question="What are Global South Africans saying about load shedding?",
            scope=BSA_SCOPE,
            interpretation_text="This may suggest interest in shared access.",
        )
        assert project(without)["intelligence"]["status"] == "complete"
        carried = scoped(
            fixture(),
            question="What are Global South Africans saying about load shedding?",
            scope=BSA_SCOPE,
            interpretation_text="This may suggest interest in shared access.",
            client_lens=BSA_LENS_BINDING,
        )
        assert project(carried)["intelligence"]["status"] == "complete"

    assert len(seen) == 2
    assert seen[0][0]["client_scope_id"] == BSA_SCOPE["client_scope_id"]
    assert seen[0][1] is None
    assert seen[1][0]["client_scope_id"] == BSA_SCOPE["client_scope_id"]
    assert seen[1][1] is not None
    assert seen[1][1].client_lens_id == BSA_LENS_BINDING["client_lens_id"]
    assert seen[1][1].configuration_digest == BSA_LENS_BINDING["configuration_digest"]


def test_a_stored_lens_the_registry_no_longer_authorizes_is_refused_at_projection():
    """A record asserts a lens. It does not grant one, so projection checks again.

    The bytes the record pinned at admission are compared with the bytes the
    entry authorizes now, and a projection whose pin has moved refuses instead
    of answering under a configuration nobody authorized for it.
    """
    moved = scoped(
        fixture(),
        question="What are Global South Africans saying about load shedding?",
        scope=BSA_SCOPE,
        interpretation_text="This may suggest interest in shared access.",
        client_lens={**BSA_LENS_BINDING, "configuration_digest": "0" * 64},
    )
    with pytest.raises(ClientLensUnavailable) as caught:
        project(moved)
    assert str(caught.value) == "client_lens_configuration_changed"

    unknown = scoped(
        fixture(),
        question="What are Global South Africans saying about load shedding?",
        scope=BSA_SCOPE,
        interpretation_text="This may suggest interest in shared access.",
        client_lens={**BSA_LENS_BINDING, "client_lens_id": "other_lens"},
    )
    with pytest.raises(ClientLensUnavailable) as caught:
        project(unknown)
    assert str(caught.value) == "client_lens_unauthorized"


def answering_contents(values):
    request, intake, plan, snapshot, _usage, _output, policy = values
    sdk = module().build_question_answering_request(
        request,
        intake,
        plan,
        snapshot,
        policy=policy,
        remaining_input_tokens=1000,
        remaining_output_tokens=1000,
        remaining_seconds=60,
    )
    return sdk, json.loads(sdk.contents)


def test_the_answering_model_frames_evidence_under_the_lens_the_request_was_admitted_under():
    """The same wording is answered under what its own lens means, and general 42 under none.

    The answering bytes of a request admitted under the BSA lens carry that lens's
    reading of the question: the property it names, the prevalence claims it
    refuses and the markets it leaves uncovered. The same wording admitted under
    the same scope without a lens carries none of that.
    """
    question = "What are Global South Africans saying about load shedding?"
    unlensed_sdk, unlensed = answering_contents(
        scoped(
            fixture(),
            question=question,
            scope=BSA_SCOPE,
            interpretation_text="This may suggest interest in shared access.",
        )
    )
    assert "client_lens" not in unlensed

    lensed_sdk, lensed = answering_contents(
        scoped(
            fixture(),
            question=question,
            scope=BSA_SCOPE,
            interpretation_text="This may suggest interest in shared access.",
            client_lens=BSA_LENS_BINDING,
        )
    )
    view = lensed["client_lens"]
    assert view["client_lens_id"] == BSA_LENS_BINDING["client_lens_id"]
    assert view["configuration_digest"] == BSA_LENS_BINDING["configuration_digest"]
    assert view["matched_entities"] == ["entity_gsa"]
    assert view["prevalence_claims_allowed"] is False
    assert "run_id" not in view
    assert lensed_sdk.input_digest != unlensed_sdk.input_digest

    moved = scoped(
        fixture(),
        question=question,
        scope=BSA_SCOPE,
        interpretation_text="This may suggest interest in shared access.",
        client_lens={**BSA_LENS_BINDING, "configuration_digest": "0" * 64},
    )
    with pytest.raises(ClientLensUnavailable) as caught:
        answering_contents(moved)
    assert str(caught.value) == "client_lens_configuration_changed"
