from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError


def test_prompt_constants_match_the_frozen_manifest_digests() -> None:
    from src.analysis.open_intelligence.canary_manifests import load_canonical_manifest
    from src.analysis.prompts import (
        dynamic_signal_summary,
        open_question_answering,
        open_question_planning,
    )

    manifest = load_canonical_manifest()
    modules = {
        "summary": dynamic_signal_summary,
        "planning": open_question_planning,
        "answering": open_question_answering,
    }
    for stage, module in modules.items():
        assert (
            module.system_instruction_digest()
            == manifest["contract_digests"][stage]["system_instruction_sha256"]
        )
        assert (
            module.response_schema_digest()
            == manifest["contract_digests"][stage]["response_schema_sha256"]
        )


def test_prompt_builders_return_the_exact_frozen_contents() -> None:
    from src.analysis.open_intelligence.canary_manifests import load_canonical_manifest
    from src.analysis.prompts import (
        dynamic_signal_summary,
        open_question_answering,
        open_question_planning,
    )

    modules = {
        "summary": dynamic_signal_summary,
        "planning": open_question_planning,
        "answering": open_question_answering,
    }
    for stage in load_canonical_manifest()["stages"]:
        assert modules[stage["stage"]].build_prompt(stage["envelope"]) == stage["contents"]


class FakeModels:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def count_tokens(self, **kwargs):
        self.calls.append(("count", kwargs))
        return SimpleNamespace(total_tokens=100)

    def generate_content(self, **kwargs):
        self.calls.append(("generate", kwargs))
        usage = SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=5,
            total_token_count=125,
        )
        return SimpleNamespace(parsed=self.parsed, text="{}", usage_metadata=usage)


class FakeClient:
    def __init__(self, parsed):
        self.models = FakeModels(parsed)
        self.vertexai = True
        self._api_client = SimpleNamespace(
            project="ogilvy-trends-v2",
            location="global",
            _http_options=SimpleNamespace(api_version="v1"),
        )


class MissingUsageModels(FakeModels):
    def generate_content(self, **kwargs):
        self.calls.append(("generate", kwargs))
        usage = SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=None,
            total_token_count=None,
        )
        return SimpleNamespace(parsed=self.parsed, text="{}", usage_metadata=usage)


class MissingUsageClient:
    def __init__(self, parsed):
        self.models = MissingUsageModels(parsed)
        self.vertexai = True
        self._api_client = SimpleNamespace(
            project="ogilvy-trends-v2",
            location="global",
            _http_options=SimpleNamespace(api_version="v1"),
        )


def approved_record(stage):
    import hashlib

    from src.analysis.open_intelligence.open_question_answer import ApprovedPlanRecord, _canonical

    envelope = stage["envelope"]
    return ApprovedPlanRecord(
        plan_id=envelope["plan_id"],
        plan_digest=hashlib.sha256(
            _canonical(envelope["approved_plan"]).encode("utf-8")
        ).hexdigest(),
        approved_by="fixture_reviewer",
        approved_at=datetime(2026, 8, 25, tzinfo=UTC),
        approval_id="approval_fixture",
        approved_claim_ids=tuple(envelope["claim_slots"]),
    )


def summary_output(envelope):
    claim_id = envelope["claim_slots"][0]
    evidence_ids = [item["evidence_id"] for item in envelope["candidate_input"]["evidence"]]
    return {
        "schema_version": "dynamic_signal_summary_v1",
        "signal_id": envelope["candidate_input"]["signal"]["signal_id"],
        "signal_label": envelope["candidate_input"]["signal"]["label"],
        "evidence_state": envelope["candidate_input"]["signal"]["evidence_state"],
        "claims": [
            {
                "claim_id": claim_id,
                "claim_text": "Two supplied fixture families rise together.",
                "claim_type": "measured",
                "assertion_kind": "observation",
                "evidence_ids": evidence_ids,
                "market_scope": envelope["candidate_input"]["market_scope"],
                "audience_lens_ids": [],
                "confidence": 0.8,
                "confidence_reason": "The supplied receipts agree.",
                "human_review_required": False,
            }
        ],
        "why_now_claim_ids": [claim_id],
        "possible_response": {
            "text": "Investigate the supplied move.",
            "supporting_claim_ids": [claim_id],
            "evidence_ids": evidence_ids,
        },
        "contradiction_ids": [],
        "limitation_ids": [item["id"] for item in envelope["limitations"]],
        "missing_work_ids": [],
        "human_review_required": False,
    }


def planning_output(envelope):
    return {
        "schema_version": "open_question_plan_v1",
        "plan_id": envelope["plan_id"],
        "intent": "landscape",
        "decision": envelope["request"]["decision"],
        "markets": envelope["request"]["market_scope"],
        "window": envelope["request"]["window"],
        "audience_lens_ids": envelope["request"]["audience_lens_ids"],
        "historical_comparison": envelope["context_index"]["historical_comparison"],
        "source_families": envelope["context_index"]["source_families"],
        "evidence_requirements": [
            {
                "requirement_id": envelope["requirement_slots"][0],
                "description": "Resolve the move across supplied families.",
                "required_source_families": envelope["context_index"]["source_families"],
                "minimum_receipts": 2,
                "blocking": True,
            }
        ],
        "claim_requirements": [
            {
                "claim_id": envelope["claim_slots"][0],
                "question": "What is newly emerging?",
                "claim_type": "measured",
                "assertion_kind": "observation",
                "required_source_families": envelope["context_index"]["source_families"],
                "minimum_evidence_state": "ready",
                "allow_inferred_audience": False,
                "human_review_required": False,
            }
        ],
        "output_form": "cited_brief",
        "stopping_conditions": ["Stop without resolvable evidence."],
        "known_gaps": [],
        "human_review_required": False,
    }


def answering_output(envelope):
    claim_id = envelope["claim_slots"][0]
    evidence_ids = [item["evidence_id"] for item in envelope["resolved_evidence"]]
    return {
        "schema_version": "open_question_answer_v1",
        "plan_id": envelope["plan_id"],
        "title": "Synthetic fixture answer",
        "scope_label": "Synthetic fixture scope",
        "evidence_state": envelope["approved_plan"]["evidence_state"],
        "claims": [
            {
                "claim_id": claim_id,
                "claim_text": "Two supplied fixture families show the move.",
                "claim_type": "measured",
                "assertion_kind": "observation",
                "evidence_ids": evidence_ids,
                "market_scope": envelope["approved_plan"]["market_scope"],
                "audience_lens_ids": envelope["approved_plan"]["audience_lens_ids"],
                "confidence": 0.8,
                "confidence_reason": "The supplied receipts agree.",
                "human_review_required": envelope["human_review_required"],
            }
        ],
        "sections": [
            {
                "section_id": envelope["section_slots"][0],
                "heading": "Finding",
                "claim_ids": [claim_id],
            }
        ],
        "recommendations": [
            {
                "recommendation_id": envelope["recommendation_slots"][0],
                "text": envelope["approved_plan"]["decision"],
                "supporting_claim_ids": [claim_id],
                "evidence_ids": evidence_ids,
                "human_review_required": envelope["human_review_required"],
            }
        ],
        "contradiction_ids": [item["id"] for item in envelope["contradictions"]],
        "limitation_ids": [item["id"] for item in envelope["limitations"]],
        "missing_work_ids": [item["id"] for item in envelope["missing_work"]],
        "human_review_required": envelope["human_review_required"],
        "export_allowed": envelope["export_allowed"],
    }


@pytest.mark.parametrize(
    ("stage_id", "caller_name", "output_builder"),
    [
        (
            "dynamic_signal_summary:summary:golden_01_emerging_without_keyword",
            "summary",
            summary_output,
        ),
        (
            "open_question_answer:planning:golden_01_emerging_without_keyword",
            "planning",
            planning_output,
        ),
    ],
)
def test_callers_reserve_count_generate_meter_usage_and_complete_receipt(
    monkeypatch, stage_id, caller_name, output_builder
) -> None:
    from src.analysis.open_intelligence import canary_runtime
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.dynamic_signal_summary import summarize_dynamic_signal
    from src.analysis.open_intelligence.open_question_answer import plan_open_question
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    monkeypatch.setattr(canary_runtime, "_utc_now", lambda: datetime(2026, 8, 28, 8, 0, tzinfo=UTC))
    stage = stage_manifest(stage_id)
    output = output_builder(stage["envelope"])
    client = FakeClient(output)
    sink = MemoryCanaryReceiptSink()
    policy = resolve_canary_policy(stage["consumer"], stage["stage"], "canary")
    budget = OpenIntelligenceRunBudget(
        run_id=stage["task_id"],
        consumer=stage["consumer"],
        trend_date=date(2026, 8, 25),
        market="za",
    )
    persisted = []
    kwargs = {
        "input_envelope": stage["envelope"],
        "lane": "canary",
        "model": policy.model,
        "policy": policy,
        "sdk_client": client,
        "budget": budget,
        "persist_usage": lambda event: persisted.append(event) or event,
        "receipt_sink": sink,
    }
    if caller_name == "summary":
        result = summarize_dynamic_signal(**kwargs)
    elif caller_name == "planning":
        result = plan_open_question(**kwargs)
    else:
        raise AssertionError("caller is invalid")

    assert result.parsed == output
    assert result.candidates_tokens == 20
    assert result.thoughts_tokens == 5
    assert persisted[0].completion_tokens == 25
    assert [name for name, _ in client.models.calls] == ["count", "generate"]
    events = sink.read(result.receipt_id)
    assert len(events) == 2
    assert events[1].terminal_state == "complete"
    assert events[1].usage_event_id == persisted[0].usage_id


def test_nonmember_output_id_is_rejected_before_successful_usage() -> None:
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.open_question_answer import plan_open_question
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    stage = stage_manifest("open_question_answer:planning:golden_01_emerging_without_keyword")
    output = planning_output(stage["envelope"])
    output["evidence_requirements"][0]["requirement_id"] = "req_" + "f" * 64
    policy = resolve_canary_policy("open_question_answer", "planning", "canary")
    persisted = []

    with pytest.raises(ValueError, match="requirement"):
        plan_open_question(
            stage["envelope"],
            lane="canary",
            model=policy.model,
            policy=policy,
            sdk_client=FakeClient(output),
            budget=OpenIntelligenceRunBudget(
                run_id=stage["task_id"],
                consumer="open_question_answer",
                trend_date=date(2026, 8, 25),
                market="za",
            ),
            persist_usage=lambda event: persisted.append(event) or event,
            receipt_sink=MemoryCanaryReceiptSink(),
        )

    assert persisted == []


def test_reservation_readback_failure_stops_before_count_or_generation() -> None:
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.dynamic_signal_summary import summarize_dynamic_signal
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    class BrokenReadbackSink(MemoryCanaryReceiptSink):
        def read(self, receipt_id):
            return ()

    stage = stage_manifest("dynamic_signal_summary:summary:golden_01_emerging_without_keyword")
    client = FakeClient(summary_output(stage["envelope"]))
    policy = resolve_canary_policy("dynamic_signal_summary", "summary", "canary")

    with pytest.raises(RuntimeError, match="reservation readback"):
        summarize_dynamic_signal(
            stage["envelope"],
            lane="canary",
            model=policy.model,
            policy=policy,
            sdk_client=client,
            budget=OpenIntelligenceRunBudget(
                run_id=stage["task_id"],
                consumer="dynamic_signal_summary",
                trend_date=date(2026, 8, 25),
                market="za",
            ),
            persist_usage=lambda event: event,
            receipt_sink=BrokenReadbackSink(),
        )

    assert client.models.calls == []


def test_incomplete_usage_metadata_writes_partial_terminal_and_no_usage_event() -> None:
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.dynamic_signal_summary import summarize_dynamic_signal
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    stage = stage_manifest("dynamic_signal_summary:summary:golden_01_emerging_without_keyword")
    policy = resolve_canary_policy("dynamic_signal_summary", "summary", "canary")
    sink = MemoryCanaryReceiptSink()
    persisted = []

    with pytest.raises(ValueError, match="usage metadata"):
        summarize_dynamic_signal(
            stage["envelope"],
            lane="canary",
            model=policy.model,
            policy=policy,
            sdk_client=MissingUsageClient(summary_output(stage["envelope"])),
            budget=OpenIntelligenceRunBudget(
                run_id=stage["task_id"],
                consumer="dynamic_signal_summary",
                trend_date=date(2026, 8, 25),
                market="za",
            ),
            persist_usage=lambda event: persisted.append(event) or event,
            receipt_sink=sink,
        )

    assert persisted == []
    events = sink.list_run(stage["envelope"]["candidate_input"]["run_id"], "gemini_3_7_canary_v1")
    assert len(events) == 2
    assert events[1].terminal_state == "partial"
    assert events[1].usage_event_id is None


def test_existing_usage_event_schema_is_unchanged() -> None:
    from dataclasses import fields

    from src.utils.gemini_usage import GeminiUsageEvent

    assert tuple(field.name for field in fields(GeminiUsageEvent)) == (
        "usage_id",
        "trend_date",
        "run_id",
        "consumer",
        "stage",
        "call_index",
        "market",
        "gemini_model",
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "recorded_at",
    )


def test_summary_rejects_an_audience_lens_absent_from_the_input() -> None:
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.dynamic_signal_summary import summarize_dynamic_signal
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    stage = stage_manifest("dynamic_signal_summary:summary:golden_01_emerging_without_keyword")
    output = summary_output(stage["envelope"])
    output["claims"][0]["audience_lens_ids"] = ["invented_lens"]
    policy = resolve_canary_policy("dynamic_signal_summary", "summary", "canary")
    persisted = []

    with pytest.raises(ValueError, match="audience"):
        summarize_dynamic_signal(
            stage["envelope"],
            lane="canary",
            model=policy.model,
            policy=policy,
            sdk_client=FakeClient(output),
            budget=OpenIntelligenceRunBudget(
                run_id=stage["task_id"],
                consumer="dynamic_signal_summary",
                trend_date=date(2026, 8, 25),
                market="za",
            ),
            persist_usage=lambda event: persisted.append(event) or event,
            receipt_sink=MemoryCanaryReceiptSink(),
        )

    assert persisted == []


def test_untyped_measured_lens_cannot_authorize_demographic_language() -> None:
    from src.analysis.open_intelligence.canary_semantics import validate_claim_semantics

    with pytest.raises(ValueError, match="demographic"):
        validate_claim_semantics(
            (
                {
                    "claim_text": "Women prefer this response.",
                    "audience_lens_ids": ("measured_age_18_24",),
                },
            ),
            text_field="claim_text",
            audience_lenses=(
                {
                    "lens_id": "measured_age_18_24",
                    "basis": "measured",
                },
            ),
        )


@pytest.mark.parametrize("mutation", ["recommendation", "section", "claim", "causation"])
def test_answer_rejects_nonmember_and_forbidden_output_mutations(mutation) -> None:
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_runtime import validate_response_schema
    from src.analysis.open_intelligence.open_question_answer import _validate_answer
    from src.analysis.prompts.open_question_answering import RESPONSE_SCHEMA

    stage = stage_manifest("open_question_answer:answering:golden_01_emerging_without_keyword")
    output = answering_output(stage["envelope"])
    if mutation == "recommendation":
        output["recommendations"][0]["recommendation_id"] = "rec_" + "f" * 64
    elif mutation == "section":
        output["sections"][0]["section_id"] = "sec_" + "f" * 64
    elif mutation == "claim":
        output["claims"][0]["claim_id"] = "clm_" + "f" * 64
    else:
        output["claims"][0]["assertion_kind"] = "causation"

    def validate_mutation():
        validate_response_schema(output, RESPONSE_SCHEMA)
        _validate_answer(output, stage["envelope"], approved_record(stage))

    with pytest.raises((ValueError, ValidationError)):
        validate_mutation()


@pytest.mark.parametrize("mutation", ["human_review", "export"])
def test_election_answer_cannot_remove_human_review_or_enable_export(mutation) -> None:
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.open_question_answer import _validate_answer

    stage = stage_manifest("open_question_answer:answering:golden_11_election_brand_role")
    output = answering_output(stage["envelope"])
    if mutation == "human_review":
        output["human_review_required"] = False
    else:
        output["export_allowed"] = True
    with pytest.raises(ValueError):
        _validate_answer(output, stage["envelope"], approved_record(stage))


def test_planning_consumes_budget_and_answering_stops_without_approval(monkeypatch) -> None:
    from src.analysis.open_intelligence import canary_runtime
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.open_question_answer import (
        ApprovalRecordUnavailable,
        answer_open_question,
        plan_open_question,
    )
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    monkeypatch.setattr(canary_runtime, "_utc_now", lambda: datetime(2026, 8, 28, 8, 0, tzinfo=UTC))
    planning = stage_manifest("open_question_answer:planning:golden_01_emerging_without_keyword")
    answering = stage_manifest("open_question_answer:answering:golden_01_emerging_without_keyword")
    budget = OpenIntelligenceRunBudget(
        run_id=planning["task_id"],
        consumer="open_question_answer",
        trend_date=date(2026, 8, 25),
        market="za",
    )
    sink = MemoryCanaryReceiptSink()
    usage = []
    plan_policy = resolve_canary_policy("open_question_answer", "planning", "canary")

    plan_open_question(
        planning["envelope"],
        lane="canary",
        model=plan_policy.model,
        policy=plan_policy,
        sdk_client=FakeClient(planning_output(planning["envelope"])),
        budget=budget,
        persist_usage=lambda event: usage.append(event) or event,
        receipt_sink=sink,
    )
    with pytest.raises(ApprovalRecordUnavailable):
        answer_open_question(
            answering["envelope"],
            approved_plan=None,
            lane="canary",
            model="gemini-3.7-flash",
            policy=resolve_canary_policy("open_question_answer", "answering", "canary"),
            sdk_client=FakeClient(answering_output(answering["envelope"])),
            budget=budget,
            persist_usage=lambda event: usage.append(event) or event,
            receipt_sink=sink,
        )

    assert budget.snapshot.input_used == 100
    assert budget.snapshot.output_used == 25
    assert budget.snapshot.remaining_max_output_tokens == 3_975
    assert [(event.stage, event.call_index) for event in usage] == [("planning", 0)]


def test_terminal_readback_failure_never_attempts_a_second_terminal(monkeypatch) -> None:
    from src.analysis.open_intelligence import canary_runtime
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.dynamic_signal_summary import summarize_dynamic_signal
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    class StaleTerminalReadbackSink(MemoryCanaryReceiptSink):
        def read(self, receipt_id):
            events = super().read(receipt_id)
            return events[:1] if len(events) == 2 else events

        @property
        def event_count(self):
            return len(self._events)

    monkeypatch.setattr(canary_runtime, "_utc_now", lambda: datetime(2026, 8, 28, 8, 0, tzinfo=UTC))
    stage = stage_manifest("dynamic_signal_summary:summary:golden_01_emerging_without_keyword")
    policy = resolve_canary_policy("dynamic_signal_summary", "summary", "canary")
    sink = StaleTerminalReadbackSink()

    with pytest.raises(RuntimeError, match="terminal readback"):
        summarize_dynamic_signal(
            stage["envelope"],
            lane="canary",
            model=policy.model,
            policy=policy,
            sdk_client=FakeClient(summary_output(stage["envelope"])),
            budget=OpenIntelligenceRunBudget(
                run_id=stage["task_id"],
                consumer="dynamic_signal_summary",
                trend_date=date(2026, 8, 25),
                market="za",
            ),
            persist_usage=lambda event: event,
            receipt_sink=sink,
        )

    assert sink.event_count == 2
