from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.eval import ask_r2
from core.agent import writer


def test_context_snapshot_preserves_writer_evidence_exclusions():
    exclusions = {
        "count": 1,
        "excluded_ids": ["obs1_00000000000000000000000000000001"],
        "records": [{"evidence_id": "obs1_00000000000000000000000000000001",
                     "reason": "missing_required_fields", "fields": ["handle"]}],
    }

    snapshot = ask_r2._context_snapshot(SimpleNamespace(writer_evidence_exclusions=exclusions))

    assert snapshot == {"writer_evidence_exclusions": exclusions}


def test_structured_dispatches_include_full_prompt_schema_and_thinking_reserve_under_ten_dollars(monkeypatch):
    monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "2000")
    client = _FakeClient()
    model = _FakeModel(client)
    budget = ask_r2.SessionBudget(10.0)
    guarded = ask_r2.BudgetedGeminiModel(model, budget)

    for question_id in ask_r2.QUESTION_IDS:
        guarded.complete_json(
            system=writer.FIELDS_SYSTEM,
            user=f"{question_id}: " + ("stored evidence " * 1200),
            schema=writer.FIELDS_SCHEMA,
            model="gemini-3.8-flash",
            max_tokens=8000,
        )

    assert client.models.calls == 5
    assert budget.charged_usd == pytest.approx(0.3375)
    assert budget.charged_usd <= 10.0
    assert all(call["status"] == "charged_known" for call in budget.calls)
    assert all(call["output_reserve_tokens"] == 10000 for call in budget.calls)
    assert all(call["input_bound_tokens"] > 20000 for call in budget.calls)


def test_real_genai_usage_metadata_reads_camel_case_token_fields():
    from google.genai import types
    from core.llm import gemini

    response = types.GenerateContentResponse.model_validate({
        "usageMetadata": {
            "promptTokenCount": 100,
            "cachedContentTokenCount": 20,
            "toolUsePromptTokenCount": 3,
            "candidatesTokenCount": 10,
            "thoughtsTokenCount": 5,
        },
    })

    usage = gemini.usage_of(response, "gemini-3.8-flash")

    assert usage["input_tokens"] == 103
    assert usage["output_tokens"] == 15
    assert usage["usd"] == pytest.approx(0.00024)


def test_serialized_request_stats_count_payload_collections_without_schema_enums():
    request = {
        "user": json.dumps({
            "evidence": [{"evidence_id": "e1"}, {"evidence_id": "e1"}, {"text": "uncited"}],
            "posts": [{"post_id": "p1"}, {"post_id": "p2"}],
            "rows": [{"row_id": "r1"}],
        }),
        "history": [{"function_response": {"response": json.dumps({"evidence": [{"id": "e1"}]})}}],
        "schema": {"properties": {"evidence": {"type": "array", "items": {"enum": ["a", "b"]}}}},
    }

    counts = ask_r2._serialized_request_stats(request)["request_evidence_counts"]

    assert counts["method"] == "payload_collection_scan_v1"
    assert counts["status"] == "measured"
    assert counts["collections"] == {
        "evidence": {"entries": 4, "identified_entries": 3, "unique_ids": 1},
        "posts": {"entries": 2, "identified_entries": 2, "unique_ids": 2},
        "rows": {"entries": 1, "identified_entries": 1, "unique_ids": 1},
    }


def test_server_error_keeps_only_bounded_http_status():
    from google.genai.errors import ServerError

    error = ServerError(503, {"status": "UNAVAILABLE"})

    assert ask_r2._safe_http_error_status(error) == 503
    assert ask_r2._safe_http_error_status(SimpleNamespace(code=399, status_code=600)) is None


def test_unknown_structured_failure_exposes_conservative_usage_to_ask():
    client = _FakeClient()
    client.models.fail_next = True
    model = _FakeModel(client)
    budget = ask_r2.SessionBudget(10.0)
    guarded = ask_r2.BudgetedGeminiModel(model, budget)

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED") as caught:
        guarded.complete_json(
            system="system",
            user="question",
            schema={"type": "object", "properties": {}, "required": []},
            model="gemini-3.8-flash",
            max_tokens=8000,
        )

    assert caught.value.usage["usd"] == budget.charged_usd
    assert caught.value.usage["input_tokens"] is None
    assert caught.value.usage["output_tokens"] is None
    assert caught.value.usage["charged_input_bound_tokens"] == budget.calls[0]["input_bound_tokens"]
    assert caught.value.usage["charged_output_bound_tokens"] == 10000
    assert budget.calls[0]["measured_input_tokens"] is None
    assert budget.calls[0]["measured_output_tokens"] is None
    assert budget.stop_reason == "unknown_dispatched_cost"
    assert client.models.calls == 1


def test_unknown_dispatched_failure_books_reserved_ceiling_and_stops_next_call():
    budget = ask_r2.SessionBudget(
        10.0,
        price_for=lambda _: {"input": 1_000_000.0, "output": 0.0},
    )
    ticket = budget.reserve("research", "fake-model", 2, 0)
    budget.mark_dispatched(ticket)
    budget.settle(ticket, unknown=True)

    assert budget.charged_usd == 2.0
    assert budget.stop_reason == "unknown_dispatched_cost"
    with pytest.raises(ask_r2.BudgetRefused):
        budget.reserve("structured", "fake-model", 1, 0)
    assert budget.calls[0]["status"] == "unknown_charged_ceiling"


def test_actual_cost_above_reserve_is_recorded_and_stops_later_calls():
    budget = ask_r2.SessionBudget(
        10.0,
        price_for=lambda _: {"input": 1_000_000.0, "output": 0.0},
    )
    ticket = budget.reserve("research", "fake-model", 1, 0)
    budget.mark_dispatched(ticket)
    budget.settle(ticket, actual_usd=1.25, actual_input_tokens=1, actual_output_tokens=0)

    assert budget.charged_usd == 1.25
    assert budget.stop_reason == "actual_cost_above_reserve"
    assert budget.calls[0]["status"] == "actual_cost_above_reserve"
    assert budget.calls[0]["measured_input_tokens"] == 1
    assert budget.calls[0]["measured_output_tokens"] == 0
    with pytest.raises(ask_r2.BudgetRefused):
        budget.reserve("structured", "fake-model", 1, 0)


def test_lower_tier_hold_is_replaced_with_t1_hold_and_restored():
    ask_module = SimpleNamespace(MODEL="gemini-3.8-flash")
    ask_module.hold_usd = lambda tier, model: {"T0": 0.1, "T1": 3.0}[tier]
    restore = ask_r2._prevent_t0_fallback(ask_module)

    assert ask_module.hold_usd("T0", "fallback") == 3.0
    restore()
    assert ask_module.hold_usd("T0", "fallback") == 0.1


def test_research_generator_does_not_retry_and_books_unknown_failure_ceiling():
    client = _FakeClient()
    client.models.fail_next = True
    model = _FakeModel(client)
    research = SimpleNamespace(client_factory=None, _generate=lambda *args, **kwargs: None)
    modules = SimpleNamespace(
        no_retry_model=model,
        gemini_research=research,
        gemini=SimpleNamespace(usage_of=lambda response, model_name: response.usage),
    )
    budget = ask_r2.SessionBudget(10.0)
    restore = ask_r2._install_gemini_guards(modules, budget)
    try:
        with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
            research._generate(
                client,
                "gemini-3.8-flash",
                [{"role": "user", "parts": [{"text": "prompt"}]}],
                {"max_output_tokens": 10000, "system_instruction": "system"},
            )
    finally:
        restore()

    assert client.models.calls == 1
    assert budget.stop_reason == "unknown_dispatched_cost"
    assert budget.calls[0]["status"] == "unknown_charged_ceiling"
    assert budget.charged_usd == budget.calls[0]["reserved_usd"] <= 10.0


def test_structured_provider_500_retries_once_with_ceiling_and_fresh_reservation():
    client = _FakeClient()
    client.models.error_next = _FakeProviderError(500)
    budget = ask_r2.SessionBudget(10.0, provider_500_retries=1)
    guarded = ask_r2.BudgetedGeminiModel(_FakeModel(client), budget)

    output, usage = guarded.complete_json(
        system="system",
        user="question",
        schema={"type": "object", "properties": {}, "required": []},
        model="gemini-3.8-flash",
        max_tokens=8000,
    )

    assert output == {"ok": True}
    assert usage["usd"] == 0.0675
    assert client.models.calls == 2
    assert [call["status"] for call in budget.calls] == ["unknown_charged_ceiling", "charged_known"]
    assert budget.calls[0]["charged_usd"] == budget.calls[0]["reserved_usd"]
    assert budget.calls[1]["reserved_usd"] > 0
    assert budget.charged_usd == pytest.approx(sum(call["charged_usd"] for call in budget.calls))
    assert budget.stop_reason is None


def test_non_500_provider_error_is_not_retried():
    client = _FakeClient()
    client.models.error_next = _FakeProviderError(503)
    budget = ask_r2.SessionBudget(10.0, provider_500_retries=1)
    guarded = ask_r2.BudgetedGeminiModel(_FakeModel(client), budget)

    with pytest.raises(_FakeProviderError):
        guarded.complete_json(
            system="system",
            user="question",
            schema={"type": "object", "properties": {}, "required": []},
            model="gemini-3.8-flash",
            max_tokens=8000,
        )

    assert client.models.calls == 1
    assert len(budget.calls) == 1
    assert budget.calls[0]["status"] == "unknown_charged_ceiling"
    assert budget.stop_reason == "unknown_dispatched_cost"


def test_structured_504_records_timeout_schema_and_output_limit_without_retry(monkeypatch):
    monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "2000")
    client = _FakeClient()
    client.models.error_next = _FakeProviderError(504)
    model = _FakeModel(client)
    model.timeout_s = 7.5
    budget = ask_r2.SessionBudget(10.0, provider_500_retries=1)
    budget.capture_failed_structured_request = True
    guarded = ask_r2.BudgetedGeminiModel(model, budget)
    schema = {
        "type": "object",
        "properties": {"label": {"type": "string", "description": "café"}},
        "required": ["label"],
    }

    with pytest.raises(_FakeProviderError):
        guarded.complete_json(
            system="system",
            user="question",
            schema=schema,
            model="gemini-3.8-flash",
            max_tokens=8000,
        )

    call = budget.calls[0]
    expected_schema_bytes = len(json.dumps(
        schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8"))
    assert client.models.calls == 1
    assert call["status"] == "unknown_charged_ceiling"
    assert call["provider_http_status"] == 504
    assert call["provider_retry_index"] == 0
    assert call["charged_usd"] == call["reserved_usd"]
    assert call["client_timeout_s"] == 7.5
    assert call["requested_max_tokens"] == 8000
    assert call["effective_output_limit_tokens"] == 10000
    assert call["structured_schema_serialized_utf8_bytes"] == expected_schema_bytes
    assert call["failed_structured_request"]["schema_serialized_utf8_bytes"] == expected_schema_bytes
    assert budget.stop_reason == "unknown_dispatched_cost"


def test_research_provider_500_retries_once_with_ceiling_and_fresh_reservation():
    client = _FakeClient()
    client.models.error_next = _FakeProviderError(500)
    model = _FakeModel(client)
    research = SimpleNamespace(client_factory=None, _generate=lambda *args, **kwargs: None)
    modules = SimpleNamespace(
        no_retry_model=model,
        gemini_research=research,
        gemini=SimpleNamespace(usage_of=lambda response, model_name: response.usage),
    )
    budget = ask_r2.SessionBudget(10.0, provider_500_retries=1)
    restore = ask_r2._install_gemini_guards(modules, budget)
    try:
        response = research._generate(
            client,
            "gemini-3.8-flash",
            [{"role": "user", "parts": [{"text": "prompt"}]}],
            {"max_output_tokens": 10000, "system_instruction": "system"},
        )
    finally:
        restore()

    assert response.usage["usd"] == 0.0675
    assert client.models.calls == 2
    assert [call["status"] for call in budget.calls] == ["unknown_charged_ceiling", "charged_known"]
    assert budget.calls[0]["charged_usd"] == budget.calls[0]["reserved_usd"]
    assert budget.stop_reason is None


def test_structured_response_without_usage_metadata_books_full_reserve():
    client = _FakeClient()
    client.models.response = _FakeResponseWithoutUsage()
    budget = ask_r2.SessionBudget(10.0)
    guarded = ask_r2.BudgetedGeminiModel(_FakeModel(client), budget)

    output, usage = guarded.complete_json(
        system="system",
        user="question",
        schema={"type": "object", "properties": {}, "required": []},
        model="gemini-3.8-flash",
        max_tokens=8000,
    )

    assert output == {"ok": True}
    assert usage["usd"] == 0.0
    assert budget.calls[0]["charged_usd"] == budget.calls[0]["reserved_usd"]
    assert budget.calls[0]["charge_basis"] == "bounded_usage_missing"
    assert budget.calls[0]["status"] == "charged_conservative_ceiling"
    assert budget.stop_reason is None


@pytest.mark.parametrize("query_expression, params", [
    ("@q", {"q": "amapiano", "since": "2026-09-01", "until": "2026-09-30", "k": 10}),
    ("'amapiano'", {"since": "2026-09-01", "until": "2026-09-30", "k": 10}),
])
def test_semantic_tvf_uses_sql_dry_run_byte_cap_and_embedding_reservation(query_expression, params):
    from core.agent.tools import sql_query

    context = SimpleNamespace(events=[], record_query=lambda *args: ("q1", "a" * 64),
                              emit=lambda *args, **kwargs: None)
    backend = _FakeWarehouse()
    budget = ask_r2.SessionBudget(10.0)
    guarded = ask_r2.ReadOnlyWarehouse(backend, sql_query.check_sql, budget=budget)
    guarded.bind_context(context)
    sql = f"SELECT * FROM intelligence_42_agent.tvf_search_posts({query_expression}, NULL, @since, @until, @k)"

    result = sql_query.sql_query(context, guarded, sql, purpose="semantic", params=params,
                                 max_bytes_billed=777)

    assert result["rows"] == []
    assert backend.dry_run_calls == 1
    assert backend.run_calls == 1
    assert backend.last_max_bytes_billed == 777
    assert len(budget.calls) == 1
    assert budget.calls[0]["phase"] == "semantic_embedding"
    assert budget.calls[0]["model"] == "gemini-embedding-001"
    assert budget.calls[0]["reserved_usd"] >= 0.0003072
    assert budget.calls[0]["charged_usd"] == budget.calls[0]["reserved_usd"]
    assert budget.calls[0]["charge_basis"] == "conservative_embedding_ceiling"
    assert context.events[-1]["mode"] == "semantic"
    assert context.events[-1]["status"] == "completed"
    assert context.events[-1]["row_count"] == 0
    assert context.events[-1]["post_ids"] == []
    assert len(context.events[-1]["sql_sha256"]) == 64


def test_semantic_tvf_rejects_correlated_embedding_query_before_dispatch():
    from core.agent.tools import sql_query

    backend = _FakeWarehouse()
    context = SimpleNamespace(events=[])
    budget = ask_r2.SessionBudget(10.0)
    guarded = ask_r2.ReadOnlyWarehouse(backend, sql_query.check_sql, budget=budget)
    guarded.bind_context(context)
    sql = (
        "SELECT * FROM source_posts p, "
        "intelligence_42_agent.tvf_search_posts(p.query_text, NULL, @since, @until, @k)"
    )

    with pytest.raises(ask_r2.OperationRefused, match="semantic_invocation_unbounded"):
        ask_r2._semantic_invocation_count(
            sql, {"since": "2026-09-01", "until": "2026-09-30", "k": 10})

    with pytest.raises(ask_r2.OperationRefused):
        guarded.dry_run(sql, {"since": "2026-09-01", "until": "2026-09-30", "k": 10})

    assert backend.dry_run_calls == 0
    assert backend.run_calls == 0
    assert budget.calls == []
    assert context.events[-1]["mode"] == "semantic"
    assert context.events[-1]["refusal_category"] == "guard"


@pytest.mark.parametrize("query_expression, params", [
    ("@q", {"q": "x" * 2049, "since": "2026-09-01", "until": "2026-09-30", "k": 10}),
    ("'" + "x" * 2049 + "'", {"since": "2026-09-01", "until": "2026-09-30", "k": 10}),
])
def test_semantic_query_over_utf8_bound_refuses_before_dispatch(query_expression, params):
    from core.agent.tools import sql_query

    backend = _FakeWarehouse()
    budget = ask_r2.SessionBudget(10.0)
    guarded = ask_r2.ReadOnlyWarehouse(backend, sql_query.check_sql, budget=budget)
    sql = f"SELECT * FROM intelligence_42_agent.tvf_search_posts({query_expression}, NULL, @since, @until, @k)"

    with pytest.raises(ask_r2.OperationRefused, match="semantic_query_bound_exceeded"):
        sql_query.sql_query(SimpleNamespace(events=[]), guarded, sql, purpose="semantic", params=params)

    assert backend.dry_run_calls == 0
    assert backend.run_calls == 0
    assert budget.calls == []


def test_semantic_query_with_unicode_at_utf8_bound_is_allowed():
    from core.agent.tools import sql_query

    context = SimpleNamespace(events=[], record_query=lambda *args: ("q1", "a" * 64),
                              emit=lambda *args, **kwargs: None)
    backend = _FakeWarehouse()
    budget = ask_r2.SessionBudget(10.0)
    guarded = ask_r2.ReadOnlyWarehouse(backend, sql_query.check_sql, budget=budget)
    sql = (
        "SELECT * FROM intelligence_42_agent.tvf_search_posts(@q, NULL, @since, @until, @k)"
    )
    params = {"q": "é" * 1024, "since": "2026-09-01", "until": "2026-09-30", "k": 10}

    sql_query.sql_query(context, guarded, sql, purpose="semantic", params=params)

    assert backend.dry_run_calls == 1
    assert backend.run_calls == 1
    assert budget.calls[0]["charge_basis"] == "conservative_embedding_ceiling"


def test_search_posts_source_sightings_query_reserves_one_embedding_call():
    from datetime import date, datetime, timezone
    from core.agent.tools import sql_query, warehouse as warehouse_tools

    context = SimpleNamespace(as_of=datetime(2026, 9, 30, tzinfo=timezone.utc),
                              window_start=None, window_end=None, market="ZA",
                              events=[], queries={}, model_usd_extra=0.0)
    context.emit = lambda *args, **kwargs: None

    def record_query(sql, params, rows, purpose):
        query_id = f"q{len(context.queries) + 1}"
        context.queries[query_id] = {"sql": sql, "params": params, "rows": rows, "purpose": purpose}
        return query_id, "a" * 64

    context.record_query = record_query
    backend = _FakeWarehouse()
    budget = ask_r2.SessionBudget(10.0)
    guarded = ask_r2.ReadOnlyWarehouse(backend, sql_query.check_sql, budget=budget)
    guarded.bind_context(context)

    warehouse_tools.search_posts(context, guarded, "amapiano", since=date(2026, 9, 1),
                                 until=date(2026, 9, 30), limit=25)

    semantic_queries = [query["sql"] for query in context.queries.values()
                        if "tvf_search_posts" in query["sql"].casefold()]
    assert len(semantic_queries) == 1
    assert "LEFT JOIN (SELECT post_id, post_date, geo_source" in semantic_queries[0]
    assert backend.dry_run_calls == 2
    assert backend.run_calls == 2
    assert len(budget.calls) == 1
    assert budget.calls[0]["phase"] == "semantic_embedding"
    assert budget.calls[0]["charged_usd"] == budget.calls[0]["reserved_usd"]


def test_r3_main_disallows_resume_before_authority_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(ask_r2, "validate_execution_authority", lambda **kwargs: pytest.fail("authority checked"))

    status = ask_r2.main(["--execute", "--resume"], repo_root=tmp_path, allow_resume=False)

    assert status == ask_r2.REFUSED


def test_r3_main_reads_embedding_coverage_before_runner_receives_metadata(tmp_path, monkeypatch):
    from core.eval import ask_r3_coverage

    events = []
    captured = {}
    wiring = SimpleNamespace()
    modules = SimpleNamespace(
        no_retry_model=SimpleNamespace(retries=0),
        staging=SimpleNamespace(real_wiring=lambda: wiring, choose_questions=lambda _: _questions()),
    )
    reporter = SimpleNamespace(
        load_morning_records=lambda source_dir: ([], {"source_paths": [], "sha256": {}}))
    receipt = {"status": "ok", "markets": {"ZA": {"eligible_distinct_posts": 4}}}

    monkeypatch.setattr(ask_r2, "validate_execution_authority", lambda **kwargs: {})
    monkeypatch.setattr(ask_r2, "_verify_builder_adc", lambda: {
        "principal": ask_r2.BUILDER_EMAIL, "project": ask_r2.PROJECT})
    monkeypatch.setattr(ask_r2, "_load_modules", lambda repo_root: modules)
    monkeypatch.setattr(ask_r2, "_load_reporter", lambda repo_root: reporter)
    monkeypatch.setattr(ask_r2, "_module_origin", lambda repo_root, module: "local-origin")
    monkeypatch.setattr(ask_r3_coverage, "read_embedding_coverage",
                        lambda actual_wiring, actual_modules: (events.append("coverage") or receipt))

    def run_questions_stub(questions, **kwargs):
        events.append("runner")
        captured.update(kwargs)
        captured["questions"] = questions
        return {"exit_code": 0, "results": [], "not_run": [], "metadata": kwargs["metadata"],
                "charged_usd": 0.0}

    monkeypatch.setattr(ask_r2, "run_questions", run_questions_stub)
    status = ask_r2.main(
        ["--execute"], repo_root=tmp_path, output_path=tmp_path / "r3.md",
        data_dir=tmp_path / "r3-data", morning_dir=tmp_path, evaluation_label="R3",
        provider_500_retries=1, allow_resume=False)

    assert status == 0
    assert events == ["coverage", "runner"]
    assert tuple(question["id"] for question in captured["questions"]) == ask_r2.QUESTION_IDS
    assert captured["metadata"]["embedding_coverage"] is receipt
    assert captured["metadata"]["module_origins"]["ask_r3_coverage"] == "local-origin"
    assert captured["provider_500_retries"] == 1


def test_r3_resume_runs_remaining_questions_then_explicit_now_retry_with_same_cap(tmp_path, monkeypatch):
    from core.eval import ask_r3

    monkeypatch.setattr(ask_r2, "PREFIX_ROOT", ask_r3.PREFIX_ROOT)
    runtime = _fake_runtime()
    output = tmp_path / "ASK-2026-09-30-R3.md"
    data = tmp_path / "ASK-2026-09-30-R3-data"
    output.write_bytes(b"initial report\n")
    _seed_saved_r3_now(runtime, data)
    original_now = (data / "NOW-01.json").read_bytes()
    original_marker = (data / "NOW-01.attempted").read_bytes()

    result = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[], metadata=_r3_resume_metadata(),
        output_path=output, data_dir=data, provider_500_retries=1,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == 0
    assert [row["attempt_key"] for row in result["results"]] == [
        "NOW-01", "RISE-02", "WHY-03", "SPR-03", "CRE-01", ask_r2.R3_NOW_RETRY_KEY,
    ]
    assert [attempt["attempt_key"] for attempt in result["metadata"]["r3_continuation"]["attempt_order"]] == [
        "RISE-02", "WHY-03", "SPR-03", "CRE-01", ask_r2.R3_NOW_RETRY_KEY,
    ]
    assert runtime.client.models.calls == 10
    assert result["charged_usd"] == pytest.approx(2.03215)
    assert result["charged_usd"] <= 10.0
    assert result["metadata"]["session_cap_usd"] == 10.0
    assert "funding" not in result["metadata"]
    assert (data / "NOW-01.json").read_bytes() == original_now
    assert (data / "NOW-01.attempted").read_bytes() == original_marker
    assert (data / f"{ask_r2.R3_NOW_RETRY_KEY}.json").is_file()


def test_r3_provider_504_for_one_remaining_question_does_not_stop_cohort(tmp_path, monkeypatch):
    from core.eval import ask_r3

    monkeypatch.setattr(ask_r2, "PREFIX_ROOT", ask_r3.PREFIX_ROOT)
    runtime = _fake_runtime()
    runtime.client.models.error_queue = [_FakeProviderError(504), _FakeProviderError(504)]
    output = tmp_path / "ASK-2026-09-30-R3.md"
    data = tmp_path / "ASK-2026-09-30-R3-data"
    output.write_bytes(b"initial report\n")
    _seed_saved_r3_now(runtime, data)

    result = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[], metadata=_r3_resume_metadata(),
        output_path=output, data_dir=data, provider_500_retries=1,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == ask_r2.FAILED
    assert [row["attempt_key"] for row in result["results"]] == [
        "NOW-01", "RISE-02", "WHY-03", "SPR-03", "CRE-01", ask_r2.R3_NOW_RETRY_KEY,
    ]
    assert result["results"][1]["outcome"] == "OPERATIONAL STOP"
    rise_unknown = [call for call in result["results"][1]["call_costs"]
                    if call.get("status") == "unknown_charged_ceiling"]
    assert len(rise_unknown) == 2
    assert [call["provider_http_status"] for call in rise_unknown] == [504, 504]
    assert runtime.client.models.calls == 10
    assert result["charged_usd"] <= 10.0
    assert result["not_run"] == []


def test_r3_structured_504_after_research_retry_preserves_global_retry_links_and_cohort(tmp_path, monkeypatch):
    from core.eval import ask_r3

    monkeypatch.setattr(ask_r2, "PREFIX_ROOT", ask_r3.PREFIX_ROOT)
    runtime = _fake_runtime(research_retry_question="RISE-02", structured_error_question="RISE-02")
    output = tmp_path / "ASK-2026-09-30-R3.md"
    data = tmp_path / "ASK-2026-09-30-R3-data"
    output.write_bytes(b"initial report\n")
    _seed_saved_r3_now(runtime, data)

    result = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[], metadata=_r3_resume_metadata(),
        output_path=output, data_dir=data, provider_500_retries=1,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == ask_r2.FAILED
    assert result["not_run"] == []
    rise = result["results"][1]
    assert rise["call_index_base"] == 35
    assert runtime.client.models.calls == 12
    assert [call["provider_http_status"] for call in rise["call_costs"]
            if call.get("status") == "unknown_charged_ceiling"] == [500, 504, 504]
    assert rise["call_costs"][0]["authorized_retry_call_index"] == 36
    assert rise["call_costs"][1]["retry_of_call_index"] == 35
    assert rise["call_costs"][2]["authorized_retry_call_index"] == 38
    assert rise["call_costs"][3]["retry_of_call_index"] == 37
    assert result["charged_usd"] <= 10.0


def test_authority_checks_real_git_ancestry_and_rejects_dirty_source(tmp_path, monkeypatch):
    git = ask_r2._git_executable()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(git, repo, "init", "-b", "main")
    _git(git, repo, "config", "user.email", "runner@example.invalid")
    _git(git, repo, "config", "user.name", "Runner Test")
    runner_path = repo / "core" / "eval" / "ask_r2.py"
    runner_path.parent.mkdir(parents=True)
    runner_path.write_text("runner\n", encoding="utf-8")
    _git(git, repo, "add", "core/eval/ask_r2.py")
    _git(git, repo, "commit", "-m", "runner source")
    first = _git(git, repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    _git(git, repo, "add", "second.txt")
    _git(git, repo, "commit", "-m", "second ancestor")
    second = _git(git, repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "third.txt").write_text("third\n", encoding="utf-8")
    _git(git, repo, "add", "third.txt")
    _git(git, repo, "commit", "-m", "third ancestor")
    health_commit = _git(git, repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(ask_r2, "REQUIRED_ANCESTORS", (first, second, health_commit))
    monkeypatch.setattr(ask_r2, "ORIGINAL_SOURCE_COMMIT", first)
    gate_path = repo / "r2-health-gate.json"
    gate_path.write_text(json.dumps(_ready_health_gate(health_commit)), encoding="utf-8")
    now = datetime.now(timezone.utc)

    proof = ask_r2.validate_execution_authority(
        repo_root=repo,
        runner_path=runner_path,
        gate_path=gate_path,
        now=now,
    )
    assert proof["gate_validated"] is True
    assert proof["source_clean"] is True
    assert proof["ancestor_proof_by_sha"] == {
        first: True, second: True, health_commit: True,
    }
    assert proof["health_at_start"]["git_commit"] == health_commit
    assert proof["health_at_start"]["version"] == health_commit[:12]
    assert "source_HEAD=PASS,health=PASS" in proof["ancestor_proof"]

    mismatched_binding = _ready_health_gate(health_commit)
    mismatched_binding["source_binding"]["method"] = "deployment_digest"
    gate_path.write_text(json.dumps(mismatched_binding), encoding="utf-8")
    with pytest.raises(ask_r2.OperationRefused, match="gate_source_binding_mismatch"):
        ask_r2.validate_execution_authority(
            repo_root=repo,
            runner_path=runner_path,
            gate_path=gate_path,
            now=now,
        )

    gate_path.write_text(json.dumps(_ready_health_gate(first)), encoding="utf-8")
    with pytest.raises(ask_r2.OperationRefused, match="health_commit_missing_required_ancestor"):
        ask_r2.validate_execution_authority(
            repo_root=repo,
            runner_path=runner_path,
            gate_path=gate_path,
            now=now,
        )

    gate_path.write_text(json.dumps(_ready_health_gate(health_commit)), encoding="utf-8")
    runner_path.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ask_r2.OperationRefused, match="source_dirty"):
        ask_r2.validate_execution_authority(
            repo_root=repo,
            runner_path=runner_path,
            gate_path=gate_path,
            now=now,
        )


def test_main_with_missing_gate_refuses_before_adc_or_module_load(tmp_path, monkeypatch):
    git = ask_r2._git_executable()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(git, repo, "init", "-b", "main")
    _git(git, repo, "config", "user.email", "runner@example.invalid")
    _git(git, repo, "config", "user.name", "Runner Test")
    runner_path = repo / "core" / "eval" / "ask_r2.py"
    runner_path.parent.mkdir(parents=True)
    runner_path.write_text("runner\n", encoding="utf-8")
    _git(git, repo, "add", "core/eval/ask_r2.py")
    _git(git, repo, "commit", "-m", "runner source")
    head = _git(git, repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(ask_r2, "REQUIRED_ANCESTORS", (head,))
    called = {"identity": 0, "modules": 0}

    def identity_check():
        called["identity"] += 1
        return {"principal": ask_r2.BUILDER_EMAIL, "project": ask_r2.PROJECT}

    def modules_loader(repo_root):
        called["modules"] += 1
        return None

    monkeypatch.setattr(ask_r2, "_verify_builder_adc", identity_check)
    monkeypatch.setattr(ask_r2, "_load_modules", modules_loader)

    exit_code = ask_r2.main(
        ["--execute"],
        repo_root=repo,
        runner_path=runner_path,
        gate_path=repo / "missing-gate.json",
    )

    assert exit_code == ask_r2.REFUSED
    assert called == {"identity": 0, "modules": 0}


def test_adc_proof_requires_builder_impersonation_and_fixed_project(monkeypatch):
    import google.auth
    from google.auth import impersonated_credentials

    class FakeCredentials:
        _target_principal = ask_r2.BUILDER_EMAIL
        _iam_endpoint_override = None

    monkeypatch.setattr(impersonated_credentials, "Credentials", FakeCredentials)
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (FakeCredentials(), ask_r2.PROJECT))
    proof = ask_r2._verify_builder_adc()
    assert proof["principal"] == ask_r2.BUILDER_EMAIL
    assert proof["project"] == ask_r2.PROJECT
    assert "token" not in proof

    FakeCredentials._target_principal = "someone-else@example.invalid"
    with pytest.raises(ask_r2.OperationRefused, match="builder_adc_principal_mismatch"):
        ask_r2._verify_builder_adc()
    FakeCredentials._target_principal = ask_r2.BUILDER_EMAIL
    FakeCredentials._iam_endpoint_override = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/other:generateAccessToken"
    )
    with pytest.raises(ask_r2.OperationRefused, match="builder_adc_endpoint_mismatch"):
        ask_r2._verify_builder_adc()


def test_run_questions_refuses_without_execution_authority(tmp_path):
    result = ask_r2.run_questions(
        _questions(),
        wiring=None,
        modules=None,
        reporter=None,
        morning_records=[],
        metadata={},
        output_path=tmp_path / "report.md",
        data_dir=tmp_path / "data",
    )

    assert result["exit_code"] == ask_r2.REFUSED
    assert result["stop_reason"] == "execution_authority_required"
    assert not (tmp_path / "report.md").exists()
    assert not (tmp_path / "data").exists()


def test_five_questions_run_once_valid_insufficiency_continues(tmp_path):
    runtime = _fake_runtime(insufficient_question="RISE-02")
    output = tmp_path / "ASK-2026-09-30-R2.md"
    data = tmp_path / "ASK-2026-09-30-R2-data"
    result = ask_r2.run_questions(
        _questions(),
        wiring=runtime.wiring,
        modules=runtime.modules,
        reporter=_FakeReporter(),
        morning_records=[],
        metadata={"source_commit": "a" * 40, "source_clean": True},
        output_path=output,
        data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN,
    )

    assert result["exit_code"] == 0
    assert [row["id"] for row in result["results"]] == list(ask_r2.QUESTION_IDS)
    assert [row["outcome"] for row in result["results"]] == [
        "PASS", "VALID INSUFFICIENT", "PASS", "PASS", "PASS"
    ]
    assert runtime.client.models.calls == 10
    assert result["charged_usd"] == pytest.approx(0.675)
    assert result["metadata"]["session_booked_verified"] is True
    assert result["metadata"]["session_booked_usd"] == pytest.approx(0.675)
    assert all((data / f"{question_id}.attempted").is_file() for question_id in ask_r2.QUESTION_IDS)

    resumed = ask_r2.run_questions(
        _questions(),
        wiring=runtime.wiring,
        modules=runtime.modules,
        reporter=_FakeReporter(),
        morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output,
        data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN,
        resume=True,
    )
    assert resumed["exit_code"] == ask_r2.REFUSED
    assert resumed["stop_reason"] == "resume_source_provenance_invalid"
    assert resumed["not_run"] == ["RISE-02"]
    assert runtime.client.models.calls == 10


def test_resume_does_not_anchor_funding_from_now_alone(tmp_path):
    runtime = _fake_runtime()
    output = tmp_path / "ASK-2026-09-30-R2.md"
    data = tmp_path / "ASK-2026-09-30-R2-data"
    output.write_bytes(b"interim report\n")
    _seed_saved_now(runtime, data)
    now_bytes = (data / "NOW-01.json").read_bytes()
    marker_bytes = (data / "NOW-01.attempted").read_bytes()

    result = ask_r2.run_questions(
        _questions(),
        wiring=runtime.wiring,
        modules=runtime.modules,
        reporter=_FakeReporter(),
        morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output,
        data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN,
        resume=True,
    )

    assert result["exit_code"] == ask_r2.REFUSED
    assert result["stop_reason"] == "resume_funding_manifest_invalid"
    assert result["not_run"] == ["RISE-02", "WHY-03", "SPR-03", "CRE-01"]
    assert runtime.client.models.calls == 0
    assert (data / "NOW-01.json").read_bytes() == now_bytes
    assert (data / "NOW-01.attempted").read_bytes() == marker_bytes


def test_resume_carries_settled_server_error_ceiling_and_continues_untouched_questions(tmp_path):
    runtime = _fake_runtime()
    output = tmp_path / "ASK-2026-09-30-R2.md"
    data = tmp_path / "ASK-2026-09-30-R2-data"
    output.write_bytes(b"interim report\n")
    _seed_saved_now(runtime, data)
    _seed_saved_server_error(runtime, data)
    preserved = {name: (data / name).read_bytes() for name in (
        "NOW-01.json", "NOW-01.attempted", "RISE-02.json", "RISE-02.attempted",
    )}

    result = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output, data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == 0
    assert [row["id"] for row in result["results"]] == [
        "NOW-01", "RISE-02", "WHY-03", "SPR-03", "CRE-01", "RISE-02",
    ]
    assert [row["id"] for row in result["results"] if row["outcome"] == "OPERATIONAL STOP"] == ["RISE-02"]
    assert result["results"][-1]["attempt_key"] == ask_r2.RISE_RETRY_KEY
    assert runtime.client.models.calls == 8
    assert result["charged_usd"] == pytest.approx(1.995613 + 0.54)
    assert len(result["call_costs"]) == 53
    assert [call["status"] for call in result["call_costs"][41:45]] == [
        "charged_known", "charged_known", "unknown_charged_ceiling", "refused_before_dispatch",
    ]
    assert result["call_costs"][43]["charged_usd"] == pytest.approx(0.607707)
    assert result["call_costs"][43]["reserved_usd"] == pytest.approx(0.607707)
    assert [row["id"] for row in result["results"][2:]] == ["WHY-03", "SPR-03", "CRE-01", "RISE-02"]
    assert result["metadata"]["session_cap_usd"] == pytest.approx(9.995613)
    assert result["metadata"]["funding"] == {
        "baseline_spend_usd": 1.995613,
        "additional_cap_usd": 8.0,
        "combined_total_cap_usd": 9.995613,
    }
    assert all(row["session_cap_usd"] == pytest.approx(9.995613) for row in result["results"][2:])
    assert [attempt["attempt_key"] for attempt in result["metadata"]["attempts"]] == [
        "NOW-01", "RISE-02", "WHY-03", "SPR-03", "CRE-01", ask_r2.RISE_RETRY_KEY,
    ]
    assert result["metadata"]["not_run_attempts"] == []
    assert result["results"][1]["source_provenance"]["resume_source_commit"] == "b" * 40
    assert result["results"][1]["source_provenance"]["original_source_commit"] == (
        ask_r2.ORIGINAL_SOURCE_COMMIT
    )
    assert "resume_provenance" in result["metadata"]
    rendered = json.loads(output.read_text(encoding="utf-8"))
    assert rendered["ids"] == [
        "NOW-01", "RISE-02", "WHY-03", "SPR-03", "CRE-01", "RISE-02",
    ]
    assert all((data / name).read_bytes() == content for name, content in preserved.items())
    manifest = json.loads((data / "resume-manifest.json").read_text(encoding="utf-8"))
    assert manifest["retry_authorized_by_attempt_key"] == "RISE-02"

    after_retry = {path.name: path.read_bytes() for path in data.iterdir() if path.is_file()}
    duplicate = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output, data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )
    assert duplicate["exit_code"] == ask_r2.REFUSED
    assert duplicate["stop_reason"] == "no_questions_to_resume"
    assert runtime.client.models.calls == 8
    assert {path.name: path.read_bytes() for path in data.iterdir() if path.is_file()} == after_retry


def test_successful_original_rise_cannot_authorize_retry_at_same_funding_total(tmp_path):
    runtime = _fake_runtime()
    data = tmp_path / "data"
    _seed_saved_now(runtime, data)
    _seed_saved_server_error(runtime, data)
    path = data / "RISE-02.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["outcome"] = "PASS"
    record["stop_reason"] = None
    record["error_type"] = None
    record["research_error_type"] = None
    record["accounting_status"] = "metered_model_usage"
    record["raw_answer"] = {"short_answer": "answer"}
    record["bars"] = {
        "schema_valid": True,
        "pass": True,
        "unknown_ids": 0,
        "age_claims": 0,
        "numbers": 0,
        "numbers_reproduced": 0,
        "cited_in_window": 5,
        "platforms": ["x", "news"],
    }
    record["call_costs"][2]["status"] = "charged_known"
    claim = {
        "answer_or_brief_id": record["prefix"],
        "claim_id": "rise-claim",
        "rule": "K4",
        "verdict": "pass",
        "checker": "model",
        "run_id": record["run_id"],
        "reason": "supported",
    }
    record["claim_rows"] = [claim]
    record["claim_readback"] = {"match": True, "rows": [claim]}
    path.write_text(json.dumps(record), encoding="utf-8")

    saved = ask_r2._load_resume_prefix(data, _questions())

    assert saved["charged_micros"] == 1_995_613
    assert "RISE-02" not in saved["recoverable_attempt_keys"]
    with pytest.raises(ask_r2.OperationRefused, match="resume_retry_source_not_authorized"):
        ask_r2._ensure_resume_manifest(data, _resume_provenance(), saved)
    assert not (data / "resume-manifest.json").exists()


def test_resume_cap_refuses_before_model_dispatch(tmp_path):
    runtime = _fake_runtime()
    output = tmp_path / "report.md"
    data = tmp_path / "data"
    output.write_bytes(b"interim report\n")
    _seed_saved_now(runtime, data)
    _seed_saved_server_error(runtime, data)
    questions = _questions()
    questions[2]["question"] = "x" * 6_000_000

    result = ask_r2.run_questions(
        questions, wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output, data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == ask_r2.FAILED
    assert runtime.client.models.calls == 0
    assert result["charged_usd"] == pytest.approx(1.995613)
    assert result["call_costs"][-1]["status"] == "refused_before_dispatch"
    assert result["call_costs"][-1]["charged_usd"] == 0


def test_resume_funding_cap_does_not_refresh_after_interruption(tmp_path):
    runtime = _fake_runtime()
    data = tmp_path / "data"
    _seed_saved_now(runtime, data)
    _seed_saved_server_error(runtime, data)
    saved = ask_r2._load_resume_prefix(data, _questions())
    provenance = _resume_provenance()

    first = ask_r2._ensure_resume_manifest(data, provenance, saved)
    resumed_state = dict(saved, charged_micros=saved["charged_micros"] + 500_000)
    second = ask_r2._ensure_resume_manifest(data, provenance, resumed_state)

    assert first["funding"] == second["funding"]
    assert second["funding"]["baseline_spend_usd"] == pytest.approx(1.995613)
    assert second["funding"]["additional_cap_usd"] == pytest.approx(8.0)
    assert second["funding"]["combined_total_cap_usd"] == pytest.approx(9.995613)


def test_resume_refuses_pending_marker_before_any_paid_call(tmp_path):
    runtime = _fake_runtime()
    output = tmp_path / "report.md"
    data = tmp_path / "data"
    output.write_bytes(b"keep report\n")
    data.mkdir()
    (data / "NOW-01.attempted").write_bytes(b"attempt started\n")

    result = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output, data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == ask_r2.REFUSED
    assert result["stop_reason"] == "resume_pending_marker"
    assert result["not_run"] == list(ask_r2.QUESTION_IDS)
    assert runtime.client.models.calls == 0
    assert output.read_bytes() == b"keep report\n"


def test_resume_refuses_malformed_saved_identity_before_any_paid_call(tmp_path):
    runtime = _fake_runtime()
    output = tmp_path / "report.md"
    data = tmp_path / "data"
    output.write_bytes(b"keep report\n")
    _seed_saved_now(runtime, data)
    path = data / "NOW-01.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["id"] = "RISE-02"
    path.write_text(json.dumps(record), encoding="utf-8")

    result = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output, data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == ask_r2.REFUSED
    assert result["stop_reason"] == "resume_record_identity_or_status"
    assert runtime.client.models.calls == 0
    assert output.read_bytes() == b"keep report\n"


@pytest.mark.parametrize("duplicate_kind", ["claim", "spend"])
def test_resume_keeps_cross_session_prefix_duplicate_guard(tmp_path, duplicate_kind):
    runtime = _fake_runtime()
    output = tmp_path / "report.md"
    data = tmp_path / "data"
    output.write_bytes(b"keep report\n")
    _seed_saved_now(runtime, data)
    _seed_saved_server_error(runtime, data)
    next_id = "WHY-03"
    next_prefix = f"{ask_r2.PREFIX_ROOT}-{next_id}"
    day = runtime.wiring.now().astimezone(runtime.modules.staging.SAST).date()
    if duplicate_kind == "claim":
        runtime.backend.claims.append({
            "answer_or_brief_id": next_prefix,
            "claim_id": "old-claim",
            "rule": "K4",
            "verdict": "pass",
            "checker": "model",
            "run_id": "old-run",
            "reason": "supported",
        })
    else:
        runtime.backend.spends.append({
            "run_id": f"{next_prefix}-spend-old",
            "stage": "understand_spend",
            "run_date": day,
            "status": "ok",
            "counts": {"model_usd": 0.01, "what": f"staging_check_ask:{next_id}"},
        })

    result = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output, data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == ask_r2.REFUSED
    assert result["stop_reason"] == "duplicate_prefix"
    assert runtime.client.models.calls == 0
    assert output.read_bytes() == b"keep report\n"


def test_resume_refuses_when_fresh_claim_readback_differs(tmp_path):
    runtime = _fake_runtime()
    output = tmp_path / "report.md"
    data = tmp_path / "data"
    output.write_bytes(b"keep report\n")
    _seed_saved_now(runtime, data)
    _seed_saved_server_error(runtime, data)
    runtime.backend.claims.clear()

    result = ask_r2.run_questions(
        _questions(), wiring=runtime.wiring, modules=runtime.modules,
        reporter=_FakeReporter(), morning_records=[],
        metadata={"resume_provenance": _resume_provenance()},
        output_path=output, data_dir=data,
        _execution_token=ask_r2._EXECUTION_TOKEN, resume=True,
    )

    assert result["exit_code"] == ask_r2.REFUSED
    assert result["stop_reason"] == "resume_readback_mismatch"
    assert runtime.client.models.calls == 0
    assert output.read_bytes() == b"keep report\n"


def test_resume_requires_core_ask_modules_unchanged_since_original_source(tmp_path, monkeypatch):
    git = ask_r2._git_executable()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(git, repo, "init", "-b", "main")
    _git(git, repo, "config", "user.email", "runner@example.invalid")
    _git(git, repo, "config", "user.name", "Runner Test")
    paths = {
        "ask": "core/agent/ask.py",
        "gemini_research": "core/agent/gemini_research.py",
        "writer": "core/agent/writer.py",
        "warehouse": "core/agent/tools/warehouse.py",
        "sql": "core/agent/tools/sql_query.py",
        "staging": "core/eval/staging_check.py",
        "gemini": "core/llm/gemini.py",
        "embed": "core/understand/embed.py",
    }
    modules = {}
    for name, relative in paths.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n", encoding="utf-8")
        modules[name] = SimpleNamespace(__file__=str(path))
    _git(git, repo, "add", "core")
    _git(git, repo, "commit", "-m", "original Ask modules")
    original = _git(git, repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(ask_r2, "ORIGINAL_SOURCE_COMMIT", original)
    modules = SimpleNamespace(**modules)

    ask_r2._verify_resume_module_sources(repo, modules)
    (repo / paths["ask"]).write_text("changed\n", encoding="utf-8")
    _git(git, repo, "add", paths["ask"])
    _git(git, repo, "commit", "-m", "changed Ask module")

    with pytest.raises(ask_r2.OperationRefused, match="core_ask_code_changed"):
        ask_r2._verify_resume_module_sources(repo, modules)


def test_report_render_failure_preserves_existing_report_bytes(tmp_path):
    path = tmp_path / "report.md"
    path.write_bytes(b"preserved content\n")

    class BrokenReporter:
        def render_report(self, *args):
            raise TypeError("formatter failed")

    with path.open("r+", encoding="utf-8", newline="\n") as handle:
        with pytest.raises(TypeError, match="formatter failed"):
            ask_r2._render_report(BrokenReporter(), handle, [], [], {}, [])

    assert path.read_bytes() == b"preserved content\n"


def test_claim_readback_mismatch_stops_remaining_questions(tmp_path):
    runtime = _fake_runtime(hide_claim_question="NOW-01")
    result = ask_r2.run_questions(
        _questions(),
        wiring=runtime.wiring,
        modules=runtime.modules,
        reporter=_FakeReporter(),
        morning_records=[],
        metadata={},
        output_path=tmp_path / "report.md",
        data_dir=tmp_path / "data",
        _execution_token=ask_r2._EXECUTION_TOKEN,
    )

    assert result["exit_code"] == ask_r2.FAILED
    assert result["stop_reason"] == "readback_mismatch"
    assert result["results"][0]["claim_readback"]["match"] is False
    assert result["not_run"] == list(ask_r2.QUESTION_IDS[1:])
    assert runtime.client.models.calls == 2


def test_failed_final_question_has_nonzero_exit_and_exact_spend_readback(tmp_path):
    runtime = _fake_runtime(fail_question="CRE-01")
    result = ask_r2.run_questions(
        _questions(),
        wiring=runtime.wiring,
        modules=runtime.modules,
        reporter=_FakeReporter(),
        morning_records=[],
        metadata={},
        output_path=tmp_path / "report.md",
        data_dir=tmp_path / "data",
        _execution_token=ask_r2._EXECUTION_TOKEN,
    )

    assert result["exit_code"] == ask_r2.FAILED
    assert len(result["results"]) == 5
    assert result["results"][-1]["outcome"] == "OPERATIONAL STOP"
    assert result["results"][-1]["spend_readback"]["match"] is True
    assert result["metadata"]["session_booked_verified"] is True
    assert result["charged_usd"] == pytest.approx(0.675)
    assert runtime.client.models.calls == 10


def test_scoring_failure_after_paid_ask_still_books_guarded_spend(tmp_path):
    runtime = _fake_runtime(crash_after_run_question="NOW-01")
    result = ask_r2.run_questions(
        _questions(),
        wiring=runtime.wiring,
        modules=runtime.modules,
        reporter=_FakeReporter(),
        morning_records=[],
        metadata={},
        output_path=tmp_path / "report.md",
        data_dir=tmp_path / "data",
        _execution_token=ask_r2._EXECUTION_TOKEN,
    )

    row = result["results"][0]
    assert result["exit_code"] == ask_r2.FAILED
    assert row["spend_readback"]["match"] is True
    assert row["booked_usd"] == pytest.approx(row["charged_usd"])
    assert result["metadata"]["session_booked_verified"] is True
    assert runtime.client.models.calls == 2


def test_research_failure_returns_guarded_cost_and_stops_without_tier_hold_or_retry(tmp_path):
    runtime = _fake_runtime(research_fail_question="NOW-01")
    result = ask_r2.run_questions(
        _questions(),
        wiring=runtime.wiring,
        modules=runtime.modules,
        reporter=_FakeReporter(),
        morning_records=[],
        metadata={},
        output_path=tmp_path / "report.md",
        data_dir=tmp_path / "data",
        _execution_token=ask_r2._EXECUTION_TOKEN,
    )

    row = result["results"][0]
    assert result["exit_code"] == ask_r2.FAILED
    assert row["research_error_type"] == "RuntimeError"
    assert row["call_costs"][0]["status"] == "unknown_charged_ceiling"
    assert row["call_costs"][1]["status"] == "refused_before_dispatch"
    assert row["booked_usd"] == pytest.approx(row["charged_usd"])
    assert row["spend_readback"]["match"] is True
    assert runtime.client.models.calls == 1


def test_non_t1_context_is_stopped_before_research_dispatch(tmp_path):
    runtime = _fake_runtime(ctx_tier_override="T0")
    result = ask_r2.run_questions(
        _questions(),
        wiring=runtime.wiring,
        modules=runtime.modules,
        reporter=_FakeReporter(),
        morning_records=[],
        metadata={},
        output_path=tmp_path / "report.md",
        data_dir=tmp_path / "data",
        _execution_token=ask_r2._EXECUTION_TOKEN,
    )

    row = result["results"][0]
    assert result["exit_code"] == ask_r2.FAILED
    assert row["stop_reason"] == "ask_tier_mismatch"
    assert all(call["status"] == "refused_before_dispatch" for call in row["call_costs"])
    assert runtime.client.models.calls == 0


class _FakeResponse:
    usage = {"input_tokens": 20000, "output_tokens": 5000, "usd": 0.0675}
    usage_metadata = SimpleNamespace(prompt_token_count=20000)


class _FakeResponseWithoutUsage:
    pass


class _FakeProviderError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.code = status


class _FakeModels:
    def __init__(self):
        self.calls = 0
        self.fail_next = False
        self.error_next = None
        self.error_queue = []
        self.response = None

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("RESOURCE_EXHAUSTED")
        if self.error_next is not None:
            error, self.error_next = self.error_next, None
            raise error
        if self.error_queue:
            raise self.error_queue.pop(0)
        return self.response or _FakeResponse()


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


class _FakeModel:
    def __init__(self, client):
        self._client = client

    @property
    def client(self):
        return self._client

    def complete_json(self, **kwargs):
        response = self.client.models.generate_content(**kwargs)
        return {"ok": True}, getattr(
            response, "usage", {"input_tokens": 0, "output_tokens": 0, "usd": 0.0})


class _FakeWarehouse:
    def __init__(self):
        self.dry_run_calls = 0
        self.run_calls = 0
        self.last_max_bytes_billed = None

    def dry_run(self, sql, params):
        self.dry_run_calls += 1
        return {"bytes": 1, "tables": []}

    def run(self, sql, params, max_bytes_billed):
        self.run_calls += 1
        self.last_max_bytes_billed = max_bytes_billed
        return []


@dataclass
class _FakeDeps:
    warehouse: object
    tables: object
    model: object
    research: object
    socialcrawl: object
    spent_today_usd: object = None


class _FakeReportBackend:
    def __init__(self, *, hide_claim_question=None):
        from core.understand import embed

        self.spend_sql = embed.SPEND_ROW_SQL
        self.spends = []
        self.claims = []
        self.spend_id = 0
        self.hide_claim_question = hide_claim_question

    def execute(self, sql, params, max_bytes=None):
        params = dict(params or {})
        if sql == self.spend_sql:
            self.spend_id += 1
            self.spends.append({
                "run_id": params["run_id"],
                "stage": "understand_spend",
                "run_date": params["run_date"],
                "status": "ok",
                "counts": json.loads(params["counts"]),
            })
            return {"rows": [], "num_dml_affected_rows": 1}
        if "STARTS_WITH(run_id" in sql:
            rows = [row for row in self.spends
                    if row["run_date"] == params.get("day") and row["run_id"].startswith(params["prefix"])]
            return {"rows": rows}
        if "answer_or_brief_id" in sql:
            rows = [row for row in self.claims if row["answer_or_brief_id"] == params["prefix"]]
            if self.hide_claim_question and params["prefix"].endswith(self.hide_claim_question):
                rows = []
            return {"rows": rows}
        raise AssertionError("unexpected fake SQL")

    def insert(self, table, rows, row_ids=None):
        assert table == ask_r2.CLAIM_TABLE
        self.claims.extend(json.loads(json.dumps(rows)))


class _FakeReportWarehouse:
    def dry_run(self, sql, params):
        return {"bytes": 1, "tables": []}

    def run(self, sql, params, max_bytes_billed):
        return []


class _FakeReporter:
    def render_report(self, records, not_run, metadata, morning_records):
        return json.dumps({"ids": [row["id"] for row in records], "not_run": not_run,
                           "metadata": metadata}) + "\n"


def _questions():
    return [{"id": question_id, "question": f"Question {question_id}", "markets": ["ZA"]}
            for question_id in ask_r2.QUESTION_IDS]


def _resume_provenance():
    return {
        "resume_source_commit": "b" * 40,
        "source_clean": True,
        "health_at_resume": {
            "http_status": 200,
            "ok": True,
            "version": "c" * 12,
            "git_commit": "c" * 40,
        },
        "original_source_commit": ask_r2.ORIGINAL_SOURCE_COMMIT,
        "original_source_details": {
            "source_commit": ask_r2.ORIGINAL_SOURCE_COMMIT,
            "source_clean": True,
            "charged_usd": 1.375862,
            "session_booked_usd": 1.375862,
            "session_booked_verified": True,
        },
    }


def _r3_resume_metadata():
    health = {"http_status": 200, "ok": True, "version": "c" * 12, "git_commit": "c" * 40}
    return {
        "evaluation_label": "R3",
        "source_commit": "b" * 40,
        "source_clean": True,
        "health_at_start": health,
        "r3_continuation": {
            "evaluation_label": "R3",
            "initial_source_commit": ask_r2.R3_INITIAL_SOURCE_COMMIT,
            "initial_source_metadata_sha256": ask_r2.R3_SOURCE_METADATA_SHA256,
            "initial_now_record_sha256": ask_r2.R3_INITIAL_NOW_SHA256,
            "initial_report_sha256": ask_r2.R3_INITIAL_REPORT_SHA256,
            "initial_booked_usd": ask_r2.R3_INITIAL_BOOKED_USD,
            "resume_source_commit": "b" * 40,
            "resume_source_clean": True,
            "health_at_resume": health,
            "retry_attempt_key": ask_r2.R3_NOW_RETRY_KEY,
            "provider_retry_statuses": [500, 504],
        },
    }


def _seed_saved_now(runtime, data_dir):
    question_id = "NOW-01"
    prefix = f"{ask_r2.PREFIX_ROOT}-{question_id}"
    day = runtime.wiring.now().astimezone(runtime.modules.staging.SAST).date()
    amount = 1.375862
    reported = 1.3758488999999998
    what = f"staging_check_ask:{question_id}"
    run_id = f"{prefix}-spend-5011358d"
    claim = {
        "answer_or_brief_id": prefix,
        "claim_id": "saved-claim",
        "rule": "K4",
        "verdict": "pass",
        "checker": "model",
        "run_id": "saved-run",
        "reason": "supported",
    }
    ledger_row = {
        "run_id": run_id,
        "stage": "understand_spend",
        "run_date": day.isoformat(),
        "status": "ok",
        "what": what,
        "booked_usd": amount,
    }
    runtime.backend.spends.append({
        "run_id": run_id,
        "stage": "understand_spend",
        "run_date": day,
        "status": "ok",
        "counts": {"model_usd": amount, "what": what},
    })
    runtime.backend.claims.append(claim)
    costs = [{
        "phase": "research",
        "model": "gemini-3.8-flash",
        "reserved_usd": amount - 0.000040,
        "charged_usd": amount - 0.000040,
        "status": "charged_known",
    }] + [{
        "phase": "research",
        "model": "gemini-3.8-flash",
        "reserved_usd": 0.000001,
        "charged_usd": 0.000001,
        "status": "charged_known",
    } for _ in range(40)]
    record = {
        "id": question_id,
        "prefix": prefix,
        "run_id": "saved-run",
        "outcome": "PASS",
        "stop_reason": None,
        "error_type": None,
        "research_error_type": None,
        "accounting_status": "metered_model_usage",
        "reported_model_usd": reported,
        "booked_usd": amount,
        "run": {"tier": "T1", "model_usd": reported},
        "bars": {
            "schema_valid": True,
            "pass": True,
            "unknown_ids": 0,
            "age_claims": 0,
            "numbers": 1,
            "numbers_reproduced": 1,
            "cited_in_window": 5,
            "platforms": ["x", "news"],
        },
        "claim_rows": [claim],
        "claim_readback": {"match": True, "rows": [claim]},
        "spend_intents": [{
            "usd": amount,
            "reported_usd": reported,
            "guarded_minimum_usd": amount,
            "what": what,
            "credit": False,
            "run_prefix": prefix,
            "run_date": day.isoformat(),
            "booked_usd": amount,
        }],
        "spend_writes": [{
            "run_id": run_id,
            "stage": "understand_spend",
            "run_date": day.isoformat(),
            "status": "ok",
            "model_usd": amount,
            "what": what,
            "submitted": True,
        }],
        "spend_readback": {"match": True, "rows": [ledger_row]},
        "session_spend_readback": {
            "match": True,
            "rows": [ledger_row],
            "booked_usd": amount,
        },
        "call_costs": costs,
        "charged_usd": amount,
    }
    data_dir.mkdir(parents=True)
    ask_r2._write_json_exclusive(data_dir / f"{question_id}.json", record)
    (data_dir / f"{question_id}.attempted").write_bytes(b"attempt started\n")


def _seed_saved_r3_now(runtime, data_dir):
    _seed_saved_now(runtime, data_dir)
    record_path = data_dir / "NOW-01.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    amount = ask_r2.R3_INITIAL_BOOKED_USD
    reported = 0.749443
    record.update({
        "outcome": "OPERATIONAL STOP",
        "stop_reason": "unknown_dispatched_cost",
        "error_type": "BudgetRefused",
        "research_error_type": "ServerError",
        "accounting_status": "unknown_dispatched_cost",
        "reported_model_usd": reported,
        "booked_usd": amount,
        "bars": None,
        "raw_answer": None,
        "claim_rows": [],
        "claim_readback": {"match": True, "rows": []},
        "call_index_base": 0,
        "spend_intents": [{
            "usd": amount,
            "reported_usd": reported,
            "guarded_minimum_usd": amount,
            "what": "staging_check_ask:NOW-01",
            "credit": False,
            "run_prefix": record["prefix"],
            "run_date": record["spend_intents"][0]["run_date"],
            "booked_usd": amount,
        }],
        "call_costs": [
            {"phase": "research", "model": "gemini-3.8-flash",
             "reserved_usd": charged / 1_000_000, "charged_usd": charged / 1_000_000,
             "status": "charged_known"}
            for charged in ([22043] * 15 + [22042] * 19)
        ] + [{
            "phase": "structured",
            "model": "gemini-3.8-flash",
            "input_bound_tokens": 355138,
            "output_reserve_tokens": 10000,
            "reserved_usd": 0.607707,
            "charged_usd": 0.607707,
            "status": "unknown_charged_ceiling",
            "provider_http_status": 504,
            "provider_error_type": "ServerError",
            "actual_input_tokens": 355138,
            "actual_output_tokens": 10000,
            "measured_input_tokens": None,
            "measured_output_tokens": None,
        }],
        "charged_usd": amount,
        "run": {"tier": "T1", "model_usd": reported},
    })
    record["spend_writes"][0]["model_usd"] = amount
    record["spend_readback"]["rows"][0]["booked_usd"] = amount
    record["session_spend_readback"]["rows"][0]["booked_usd"] = amount
    record["session_spend_readback"]["booked_usd"] = amount
    record_path.write_text(json.dumps(record), encoding="utf-8")
    runtime.backend.claims.clear()
    runtime.backend.spends[0]["counts"]["model_usd"] = amount


def _seed_saved_server_error(runtime, data_dir):
    question_id = "RISE-02"
    prefix = f"{ask_r2.PREFIX_ROOT}-{question_id}"
    day = runtime.wiring.now().astimezone(runtime.modules.staging.SAST).date()
    amount = 0.619751
    what = f"staging_check_ask:{question_id}"
    run_id = f"{prefix}-spend-8612152c"
    write = {
        "run_id": run_id,
        "stage": "understand_spend",
        "run_date": day.isoformat(),
        "status": "ok",
        "model_usd": amount,
        "what": what,
        "submitted": True,
    }
    runtime.backend.spends.append({
        "run_id": run_id,
        "stage": "understand_spend",
        "run_date": day,
        "status": "ok",
        "counts": {"model_usd": amount, "what": what},
    })
    now_record = json.loads((data_dir / "NOW-01.json").read_text(encoding="utf-8"))
    rise_row = {
        "run_id": run_id,
        "stage": "understand_spend",
        "run_date": day.isoformat(),
        "status": "ok",
        "what": what,
        "booked_usd": amount,
    }
    session_rows = now_record["session_spend_readback"]["rows"] + [rise_row]
    costs = [
        {"phase": "research", "model": "gemini-3.8-flash", "input_bound_tokens": 11168,
         "output_reserve_tokens": 10000, "reserved_usd": 0.091752, "charged_usd": 0.006923,
         "status": "charged_known"},
        {"phase": "research", "model": "gemini-3.8-flash", "input_bound_tokens": 14022,
         "output_reserve_tokens": 10000, "reserved_usd": 0.096033, "charged_usd": 0.005121,
         "status": "charged_known"},
        {"phase": "research", "model": "gemini-3.8-flash", "input_bound_tokens": 355138,
         "output_reserve_tokens": 10000, "reserved_usd": 0.607707, "charged_usd": 0.607707,
         "actual_input_tokens": 355138, "actual_output_tokens": 10000,
         "status": "unknown_charged_ceiling"},
        {"phase": "structured", "model": "gemini-3.8-flash", "input_bound_tokens": 20130,
         "output_reserve_tokens": 10000, "reserved_usd": 0.0, "charged_usd": 0.0,
         "status": "refused_before_dispatch"},
    ]
    claim_rows = []
    record = {
        "id": question_id,
        "prefix": prefix,
        "run_id": "saved-rise-run",
        "outcome": "OPERATIONAL STOP",
        "stop_reason": "unknown_dispatched_cost",
        "error_type": "BudgetRefused",
        "research_error_type": "ServerError",
        "accounting_status": "unknown_dispatched_cost",
        "reported_model_usd": amount,
        "booked_usd": amount,
        "run": {"tier": "T1", "model_usd": amount},
        "bars": None,
        "raw_answer": None,
        "claim_rows": claim_rows,
        "claim_readback": {"match": True, "rows": claim_rows},
        "spend_intents": [{
            "usd": amount,
            "reported_usd": amount,
            "guarded_minimum_usd": amount,
            "what": what,
            "credit": False,
            "run_prefix": prefix,
            "run_date": day.isoformat(),
            "booked_usd": amount,
        }],
        "spend_writes": [write],
        "spend_readback": {"match": True, "rows": [rise_row]},
        "session_spend_readback": {
            "match": True,
            "rows": session_rows,
            "booked_usd": 1.995613,
        },
        "call_costs": costs,
        "charged_usd": amount,
        "readback_error_type": None,
        "session_readback_error_type": None,
        "source_provenance": _resume_provenance(),
    }
    ask_r2._write_json_exclusive(data_dir / f"{question_id}.json", record)
    (data_dir / f"{question_id}.attempted").write_bytes(b"attempt started\n")


def _fake_runtime(*, insufficient_question=None, fail_question=None, hide_claim_question=None,
                  crash_after_run_question=None, research_fail_question=None,
                  research_retry_question=None, structured_error_question=None,
                  ctx_tier_override=None):
    backend = _FakeReportBackend(hide_claim_question=hide_claim_question)
    warehouse = _FakeReportWarehouse()
    client = _FakeClient()
    model = _FakeModel(client)
    sast = timezone(timedelta(hours=2))
    now = datetime(2026, 9, 30, 4, 37, tzinfo=sast)

    def refusing_socialcrawl(mode, run_id=None):
        raise AssertionError("SocialCrawl must stay refused")

    def check_sql(sql):
        if not str(sql).strip().upper().startswith("SELECT ") or ";" in str(sql):
            raise ValueError("read-only query required")

    def spend_today(warehouse_arg, now_arg):
        return 0.0

    def model_daily_usd(*, now=None):
        return 40.0

    class _Tables:
        def insert(self, table, rows, row_ids=None):
            backend.insert(table, rows, row_ids)

    def research_fn(ctx, prompt, *args, **kwargs):
        if getattr(ctx, "question_id", None) == research_fail_question:
            client.models.fail_next = True
        if getattr(ctx, "question_id", None) == research_retry_question:
            client.models.error_queue = [_FakeProviderError(500)]
        response = modules.gemini_research._generate(
            model.client,
            "gemini-3.8-flash",
            [{"role": "user", "parts": [{"text": prompt}]}],
            {"max_output_tokens": 10000, "system_instruction": "research system"},
        )
        usage = modules.gemini.usage_of(response, "gemini-3.8-flash")
        if getattr(ctx, "question_id", None) == structured_error_question:
            client.models.error_queue = [_FakeProviderError(504), _FakeProviderError(504)]
        return {"note": "", "tokens": {"input": usage["input_tokens"],
                                           "output": usage["output_tokens"]},
                "usd": usage["usd"], "stopped": False}

    def ask_deps():
        return _FakeDeps(warehouse=warehouse, tables=_Tables(), model=model,
                         research=research_fn, socialcrawl=refusing_socialcrawl,
                         spent_today_usd=lambda: 0.0)

    def run_ask(request, emit, should_stop, *, deps):
        question_id = request["ask_id"].removeprefix(ask_r2.PREFIX_ROOT + "-")
        context = SimpleNamespace(tier=ctx_tier_override or request["tier"],
                                  question_id=question_id, events=[], queries={})
        research_result = deps.research(context, request["question"])
        run = {"run_id": f"run-{question_id}", "tier": "T1",
               "model_usd": research_result["usd"], "notices": []}
        try:
            _, usage = deps.model.complete_json(
                system="system prompt",
                user=request["question"],
                schema={"type": "object", "properties": {}, "required": []},
                model="gemini-3.8-flash",
                max_tokens=8000,
            )
        except BaseException as exc:
            exc.run = run
            raise
        run["model_usd"] += usage["usd"]
        if question_id == fail_question:
            error = RuntimeError("fake final failure")
            error.run = run
            raise error
        claim = {"answer_or_brief_id": request["ask_id"], "claim_id": "c1", "rule": "K4",
                 "verdict": "pass", "checker": "model", "run_id": run["run_id"], "reason": "supported"}
        deps.tables.insert(ask_r2.CLAIM_TABLE, [claim], row_ids=[f"{request['ask_id']}:c1:K4:0"])
        answer = {"short_answer": "answer", "claims": [], "evidence": [], "gaps": []}
        return {"answer": answer, "run": run}

    def book_spend(execute, *, run_id, run_date, usd, what, credit=False):
        amount = round(float(usd or 0), 6)
        if amount == 0:
            return 0.0
        row_id = f"{run_id}-spend-{backend.spend_id + 1:08d}"
        execute(backend.spend_sql, {
            "run_id": row_id,
            "run_date": run_date,
            "counts": json.dumps({"model_usd": amount, "what": what}),
        })
        return amount

    def check_ask(wiring, book, questions, record_dir=None):
        question = questions[0]
        question_id = question["id"]
        deps = wiring.ask_deps()
        request = {"question": question["question"], "tier": "T1", "mode": "live", "market": "ZA"}
        answer, run, error = None, {}, None
        try:
            output = wiring.run_ask(request, lambda _: None, lambda: False, deps=deps)
            answer, run = output["answer"], output["run"]
        except Exception as exc:
            answer, run, error = None, getattr(exc, "run", {}), type(exc).__name__
        if question_id == crash_after_run_question and error is None:
            raise RuntimeError("fake checker failure")
        booked = book(run.get("model_usd") or 0.0, f"staging_check_ask:{question_id}")
        insufficient = question_id == insufficient_question
        bars = None if error else {
            "schema_valid": True, "pass": not insufficient, "unknown_ids": 0, "age_claims": 0,
            "numbers": 0, "numbers_reproduced": 0,
            "cited_in_window": 3 if insufficient else 5,
            "platforms": ["x"] if insufficient else ["x", "news"],
        }
        return {"questions": [{"id": question_id, "question": question["question"], "error": error,
                               "error_type": error, "answer": answer, "run": run,
                               "model_usd": float(run.get("model_usd") or 0.0), "booked_usd": booked,
                               "bars": bars}]}

    staging = SimpleNamespace(
        SAST=sast,
        refusing_socialcrawl=refusing_socialcrawl,
        check_ask=check_ask,
    )
    ask = SimpleNamespace(
        BUDGET_SPENT="The model budget for today is spent",
        model_spend_today=spend_today,
        model_daily_usd=model_daily_usd,
    )
    research = SimpleNamespace(client_factory=None, _generate=lambda *args, **kwargs: None)
    modules = SimpleNamespace(
        staging=staging,
        ask=ask,
        sql=SimpleNamespace(MAX_BYTES_BILLED=1000, check_sql=check_sql),
        embed=SimpleNamespace(SPEND_ROW_SQL=backend.spend_sql, book_spend=book_spend),
        gemini_research=research,
        gemini=SimpleNamespace(usage_of=lambda response, model_name: response.usage),
        no_retry_model=model,
    )
    wiring = SimpleNamespace(
        execute=backend.execute,
        ask_deps=ask_deps,
        run_ask=run_ask,
        run_embed=lambda *args, **kwargs: None,
        run_enrich=lambda *args, **kwargs: None,
        enrich_model=None,
        spent_today=lambda: 0.0,
        now=lambda: now,
        warehouse=warehouse,
    )
    return SimpleNamespace(backend=backend, client=client, modules=modules, wiring=wiring)


def _git(executable, repo, *args):
    result = subprocess.run(
        [str(executable), "-C", str(repo), *args],
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result


def _ready_health_gate(health_commit):
    return {
        "schema_version": ask_r2.GATE_SCHEMA,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": "READY",
        "project": ask_r2.PROJECT,
        "health": {
            "http_status": 200,
            "ok": True,
            "version": health_commit[:12],
            "git_commit": health_commit,
        },
        "source_binding": {
            "status": "PROVEN",
            "method": "user_authorized_health_version",
        },
    }
