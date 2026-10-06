"""Independent regression tests for the Gemini canary review findings."""

from __future__ import annotations

import runpy
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.genai import types


def helpers():
    return runpy.run_path("tests/unit/test_gemini_canary_callers.py")


def configured(client, **overrides):
    values = {
        "project": "ogilvy-trends-v2",
        "location": "global",
        "vertexai": True,
        "api_version": "v1",
    }
    values.update(overrides)
    client.vertexai = values["vertexai"]
    client._api_client = SimpleNamespace(
        project=values["project"],
        location=values["location"],
        _http_options=SimpleNamespace(api_version=values["api_version"]),
    )
    return client


def budget_for(stage):
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    return OpenIntelligenceRunBudget(
        run_id=stage["task_id"],
        consumer=stage["consumer"],
        trend_date=date(2026, 8, 25),
        market="za",
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


def test_all_eleven_answers_stop_before_count_without_canonical_approval_record():
    from src.analysis.open_intelligence import open_question_answer
    from src.analysis.open_intelligence.canary_manifests import load_canonical_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink

    error_type = getattr(open_question_answer, "ApprovalRecordUnavailable", None)
    assert error_type is not None, "approval_record_unavailable error is missing"
    answer = helpers()["answering_output"]
    fake = helpers()["FakeClient"]
    stages = [
        stage for stage in load_canonical_manifest()["stages"] if stage["stage"] == "answering"
    ]
    assert len(stages) == 11
    for stage in stages:
        client = configured(fake(answer(stage["envelope"])))
        sink = MemoryCanaryReceiptSink()
        policy = resolve_canary_policy(stage["consumer"], stage["stage"], "canary")
        with pytest.raises(error_type) as caught:
            open_question_answer.answer_open_question(
                stage["envelope"],
                approved_plan=None,
                lane="canary",
                model=policy.model,
                policy=policy,
                sdk_client=client,
                budget=budget_for(stage),
                persist_usage=lambda event: event,
                receipt_sink=sink,
            )
        assert caught.value.code == "approval_record_unavailable"
        assert client.models.calls == []
        assert (
            sink.list_run(stage["envelope"]["approved_plan"]["run_id"], "gemini_3_7_canary_v1")
            == ()
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("intent", "risk"),
        ("decision", "Different decision"),
        ("markets", ["ke"]),
        ("window", {"start_date": "2020-01-01", "end_date": "2020-01-02"}),
        ("historical_comparison", {"required": True, "window_days": 10}),
        ("source_families", ["invented"]),
        ("output_form", "different"),
        ("audience_lens_ids", ["invented_lens"]),
        ("human_review_required", True),
    ],
)
def test_planning_rejects_every_frozen_field_drift(field, value):
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.open_question_answer import _validate_plan

    stage = stage_manifest("open_question_answer:planning:golden_01_emerging_without_keyword")
    output = helpers()["planning_output"](stage["envelope"])
    output[field] = value
    with pytest.raises(ValueError, match=r"planning .* changed"):
        _validate_plan(output, stage["envelope"])


@pytest.mark.parametrize(
    ("task_id", "text", "expected"),
    [
        (
            "golden_01_emerging_without_keyword",
            "Women aged 18 to 24 prefer this move.",
            "demographic",
        ),
        (
            "golden_01_emerging_without_keyword",
            "The supplied move causes higher participation.",
            "causal",
        ),
        (
            "golden_11_election_brand_role",
            "Vote for candidate X. Polling proves 80 percent support.",
            "election",
        ),
    ],
)
def test_answer_semantics_reject_unsupported_text(task_id, text, expected):
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.open_question_answer import (
        ApprovedPlanRecord,
        _validate_answer,
    )

    stage = stage_manifest(f"open_question_answer:answering:{task_id}")
    output = helpers()["answering_output"](stage["envelope"])
    output["claims"][0]["claim_text"] = text
    output["claims"][0]["assertion_kind"] = "association"
    output["claims"][0]["audience_lens_ids"] = []
    with pytest.raises(ValueError, match=expected):
        _validate_answer(output, stage["envelope"], approved_record(stage))


def test_summary_semantics_reject_demographic_and_causal_text():
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.dynamic_signal_summary import _validate

    stage = stage_manifest("dynamic_signal_summary:summary:golden_01_emerging_without_keyword")
    for text, expected in (
        ("Women aged 18 to 24 prefer this move.", "demographic"),
        ("The supplied move causes higher participation.", "causal"),
    ):
        output = helpers()["summary_output"](stage["envelope"])
        output["claims"][0]["claim_text"] = text
        output["claims"][0]["assertion_kind"] = "association"
        with pytest.raises(ValueError, match=expected):
            _validate(output, stage["envelope"])


@pytest.mark.parametrize("mode", ["partial", "error"])
def test_noncomplete_terminal_requires_exact_postwrite_readback(mode):
    from src.analysis.open_intelligence import canary_runtime
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.dynamic_signal_summary import summarize_dynamic_signal

    class StaleTerminalSink(MemoryCanaryReceiptSink):
        def read(self, receipt_id):
            events = super().read(receipt_id)
            return events[:1] if len(events) == 2 else events

    ns = helpers()
    stage = stage_manifest("dynamic_signal_summary:summary:golden_01_emerging_without_keyword")
    output = ns["summary_output"](stage["envelope"])
    if mode == "partial":
        client = configured(ns["MissingUsageClient"](output))
    else:
        output["claims"][0]["claim_id"] = "clm_" + "f" * 64
        client = configured(ns["FakeClient"](output))
    policy = resolve_canary_policy(stage["consumer"], stage["stage"], "canary")
    sink = StaleTerminalSink()
    canary_runtime._utc_now = lambda: datetime(2026, 8, 28, 8, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="terminal readback"):
        summarize_dynamic_signal(
            stage["envelope"],
            lane="canary",
            model=policy.model,
            policy=policy,
            sdk_client=client,
            budget=budget_for(stage),
            persist_usage=lambda event: event,
            receipt_sink=sink,
        )
    assert len(sink._events) == 2


def test_three_same_stage_attempts_get_distinct_deterministic_call_indexes():
    from src.analysis.open_intelligence import canary_runtime
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import (
        CanaryCallReservation,
        MemoryCanaryReceiptSink,
    )
    from src.analysis.open_intelligence.dynamic_signal_summary import summarize_dynamic_signal

    ns = helpers()
    stage = stage_manifest("dynamic_signal_summary:summary:golden_01_emerging_without_keyword")
    policy = resolve_canary_policy(stage["consumer"], stage["stage"], "canary")
    budget = budget_for(stage)
    sink = MemoryCanaryReceiptSink()
    usage = []
    canary_runtime._utc_now = lambda: datetime(2026, 8, 28, 8, tzinfo=UTC)
    receipt_ids = []
    for _ in range(3):
        result = summarize_dynamic_signal(
            stage["envelope"],
            lane="canary",
            model=policy.model,
            policy=policy,
            sdk_client=configured(ns["FakeClient"](ns["summary_output"](stage["envelope"]))),
            budget=budget,
            persist_usage=lambda event: usage.append(event) or event,
            receipt_sink=sink,
        )
        receipt_ids.append(result.receipt_id)
    reservations = [
        event
        for event in sink.list_run(
            stage["envelope"]["candidate_input"]["run_id"], "gemini_3_7_canary_v1"
        )
        if isinstance(event, CanaryCallReservation)
    ]
    assert [event.call_index for event in reservations] == [0, 1, 2]
    assert [event.call_index for event in usage] == [0, 1, 2]
    assert len(set(receipt_ids)) == 3


def test_sinks_reject_forged_reservation_identity():
    from src.analysis.open_intelligence.canary_receipts import (
        CanaryReceiptConflict,
        MemoryCanaryReceiptSink,
        StructuredLoggingCanaryReceiptSink,
        build_reservation,
    )

    reservation = build_reservation(
        lane="canary",
        consumer="dynamic_signal_summary",
        stage="summary",
        model="gemini-3.7-flash",
        thinking_level=types.ThinkingLevel.MEDIUM,
        run_id="run",
        call_index=0,
        input_digest="1" * 64,
        system_instruction_digest="2" * 64,
        response_schema_digest="3" * 64,
        sdk_version="2.20.0",
        api_version="v1",
        pricing_version="pricing",
        reserved_at=datetime(2026, 8, 28, tzinfo=UTC),
        contract_version="gemini_3_7_canary_v1",
    )
    forged = replace(reservation, input_digest="f" * 64)
    sinks = (
        MemoryCanaryReceiptSink(),
        StructuredLoggingCanaryReceiptSink(
            write_structured=lambda payload: None,
            read_structured=lambda receipt_id: (),
            list_structured=lambda run_id, contract_version: (),
        ),
    )
    for sink in sinks:
        with pytest.raises(CanaryReceiptConflict, match="identity"):
            sink.reserve(forged)


@pytest.mark.parametrize("wrong", ["run", "contract"])
def test_reconciliation_rejects_wrong_run_or_contract_events(wrong):
    from src.analysis.open_intelligence.canary_receipts import (
        CanaryReceiptConflict,
        ExpectedCanaryCall,
        build_reservation,
        build_terminal,
        reconcile_canary_run,
    )

    reservation = build_reservation(
        lane="canary",
        consumer="dynamic_signal_summary",
        stage="summary",
        model="gemini-3.7-flash",
        thinking_level=types.ThinkingLevel.MEDIUM,
        run_id="wrong_run" if wrong == "run" else "requested_run",
        call_index=0,
        input_digest="1" * 64,
        system_instruction_digest="2" * 64,
        response_schema_digest="3" * 64,
        sdk_version="2.20.0",
        api_version="v1",
        pricing_version="pricing",
        reserved_at=datetime(2026, 8, 28, tzinfo=UTC),
        contract_version="wrong_contract" if wrong == "contract" else "expected_contract",
    )
    terminal = build_terminal(
        reservation=reservation,
        started_at=datetime(2026, 8, 28, tzinfo=UTC),
        completed_at=datetime(2026, 8, 28, 0, 0, 1, tzinfo=UTC),
        terminal_state="complete",
        prompt_token_count=1,
        candidates_token_count=1,
        thoughts_token_count=1,
        total_token_count=3,
        estimated_cost_usd=Decimal("0.1"),
        usage_event_id="usage_x",
        error_code=None,
        completed_contract_version=reservation.contract_version,
    )

    class WrongScopeSink:
        def list_run(self, run_id, contract_version):
            return (reservation, terminal)

    with pytest.raises(CanaryReceiptConflict, match="run scope"):
        reconcile_canary_run(
            "requested_run",
            "expected_contract",
            (ExpectedCanaryCall(reservation.receipt_id),),
            sink=WrongScopeSink(),
        )


def test_request_keeps_http_options_on_validated_injected_client():
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_request import (
        build_canary_request,
        validate_canary_client,
    )

    stage = stage_manifest("dynamic_signal_summary:summary:golden_01_emerging_without_keyword")
    request = build_canary_request(
        stage, policy=resolve_canary_policy(stage["consumer"], stage["stage"], "canary")
    )
    assert "httpOptions" not in request.generation_config.model_dump(
        exclude_none=True, by_alias=True
    )
    assert "httpOptions" not in request.count_tokens_config.model_dump(
        exclude_none=True, by_alias=True
    )
    client = configured(helpers()["FakeClient"](helpers()["summary_output"](stage["envelope"])))
    validate_canary_client(client)
    for mutation in (
        {"project": "wrong"},
        {"location": "us-central1"},
        {"api_version": "v1beta1"},
        {"vertexai": False},
    ):
        with pytest.raises(ValueError, match="client configuration"):
            validate_canary_client(configured(helpers()["FakeClient"]({}), **mutation))


def test_schema_prose_coverage_is_exact_and_structural_fields_are_excluded():
    from src.analysis.open_intelligence.canary_semantics import schema_prose_paths
    from src.analysis.prompts.dynamic_signal_summary import RESPONSE_SCHEMA as SUMMARY_SCHEMA
    from src.analysis.prompts.open_question_answering import RESPONSE_SCHEMA as ANSWER_SCHEMA
    from src.analysis.prompts.open_question_planning import RESPONSE_SCHEMA as PLAN_SCHEMA

    assert schema_prose_paths(SUMMARY_SCHEMA) == (
        "claims[].claim_text",
        "claims[].confidence_reason",
        "possible_response.text",
        "signal_label",
    )
    assert schema_prose_paths(PLAN_SCHEMA) == (
        "claim_requirements[].question",
        "decision",
        "evidence_requirements[].description",
        "known_gaps[]",
        "stopping_conditions[]",
    )
    assert schema_prose_paths(ANSWER_SCHEMA) == (
        "claims[].claim_text",
        "claims[].confidence_reason",
        "recommendations[].text",
        "scope_label",
        "sections[].heading",
        "title",
    )


@pytest.mark.parametrize(
    ("stage", "field", "text", "expected"),
    [
        ("summary", "signal_label", "Women aged 18 to 24 prefer this.", "demographic"),
        ("summary", "possible_response", "This causes higher participation.", "causal"),
        ("summary", "confidence_reason", "This causes the observed move.", "causal"),
        ("planning", "description", "This causes higher participation.", "causal"),
        ("planning", "stopping_condition", "This causes higher participation.", "causal"),
        ("answer", "confidence_reason", "This causes the result.", "causal"),
        ("answer", "heading", "Polling proves 80 percent support.", "election"),
    ],
)
def test_all_seven_previous_semantic_escape_fields_are_rejected(stage, field, text, expected):
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.dynamic_signal_summary import _validate as validate_summary
    from src.analysis.open_intelligence.open_question_answer import (
        _validate_answer,
        _validate_plan,
    )

    ns = helpers()
    if stage == "summary":
        manifest = stage_manifest(
            "dynamic_signal_summary:summary:golden_01_emerging_without_keyword"
        )
        output = ns["summary_output"](manifest["envelope"])
        if field == "signal_label":
            output["signal_label"] = text
        elif field == "possible_response":
            output["possible_response"]["text"] = text
        else:
            output["claims"][0]["confidence_reason"] = text

        def action():
            validate_summary(output, manifest["envelope"])
    elif stage == "planning":
        manifest = stage_manifest(
            "open_question_answer:planning:golden_01_emerging_without_keyword"
        )
        output = ns["planning_output"](manifest["envelope"])
        if field == "description":
            output["evidence_requirements"][0]["description"] = text
        else:
            output["stopping_conditions"][0] = text

        def action():
            _validate_plan(output, manifest["envelope"])
    else:
        manifest = stage_manifest("open_question_answer:answering:golden_11_election_brand_role")
        output = ns["answering_output"](manifest["envelope"])
        if field == "confidence_reason":
            output["claims"][0]["confidence_reason"] = text
        else:
            output["sections"][0]["heading"] = text

        def action():
            _validate_answer(output, manifest["envelope"], approved_record(manifest))

    with pytest.raises(ValueError, match=expected):
        action()
