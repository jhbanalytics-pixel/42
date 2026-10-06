from datetime import date, datetime
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.agent import ask as ask_module
from core.agent.context import RunContext
from core.eval import ask_r2, demo_pairs, staging_check
from core.eval import demo_dispatch
from core.eval.demo_dispatch import DemoDispatchRefused, dispatch_attempt


def _proof(*, consumed=0, app_run_ids=()):
    return {
        "schema_version": demo_pairs.DEMO_FUNDING_SCHEMA,
        "phase_id": demo_pairs.DEMO_PHASE_ID,
        "verified": True,
        "run_date": demo_pairs.DEMO_RUN_DATE.isoformat(),
        "reservation_run_id": "demo-funding-20261001-source-proof",
        "allocated_micros": demo_pairs.NEW_POOL_MICROS,
        "consumed_micros": consumed,
        "source_proof_sha": "a" * 64,
        "native_net_micros": demo_pairs.CUMULATIVE_CAP_MICROS,
        "baseline_run_ids": list(demo_pairs.BASELINE_RUN_IDS),
        "app_run_ids": list(app_run_ids),
    }


def _request():
    question = demo_pairs.DEMO_QUESTIONS[0]
    context = {
        "market": question.market,
        "source_ids": list(question.source_ids),
        "posts": [{"id": source_id, "text": f"Fetched text for {source_id}"}
                  for source_id in question.source_ids],
    }
    return demo_pairs.DemoAttemptRequest(
        question_id=question.id,
        attempt_number=1,
        attempt_key="DEMO-01-attempt-1",
        prompt=question.text,
        market=question.market,
        source_ids=question.source_ids,
        support_run_id=question.support_run_id,
        provided_context=context,
        previous_attempt=None,
        attempt_cap_micros=demo_pairs.NEW_POOL_MICROS // demo_pairs.MAX_TOTAL_ATTEMPTS,
    )


def _ranked_request():
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    question = profile.question_catalog[0]
    context = {"market": question.market, "source_ids": [], "posts": []}
    return demo_pairs.DemoAttemptRequest(
        question_id=question.id,
        attempt_number=1,
        attempt_key="NOW-01-attempt-1",
        prompt=question.text,
        market=question.market,
        source_ids=question.source_ids,
        support_run_id=question.support_run_id,
        provided_context=context,
        previous_attempt=None,
        attempt_cap_micros=profile.attempt_cap_micros,
    )


def _budget():
    budget = ask_r2.SessionBudget(
        cap_usd=_request().attempt_cap_micros / ask_r2.MICROS,
        price_for=lambda _model: {"input": 0, "output": 0},
        provider_500_retries=0,
    )
    budget.provider_retry_statuses = ()
    return budget


def _ranked_budget():
    budget = ask_r2.SessionBudget(
        cap_usd=3.0,
        price_for=lambda _model: {"input": 0, "output": 0},
        provider_500_retries=0,
    )
    budget.provider_retry_statuses = ()
    return budget


def test_ranked_dispatch_binds_profile_question_market_and_one_slot_cap():
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    request = _ranked_request()
    question, context = demo_dispatch._source_request(request, profile=profile)
    assert question == profile.question_catalog[0]
    assert question.id == "NOW-01" and question.market == "ZA"
    assert context == {"market": "ZA", "source_ids": [], "posts": []}

    proof = _proof(consumed=426_564, app_run_ids=(f"prior-app-{i}" for i in range(5)))
    normalized = demo_dispatch._proof_and_budget(
        request, _ranked_budget(), proof, profile=profile)
    assert normalized["consumed_micros"] == 426_564

    with pytest.raises(DemoDispatchRefused, match="ranked_attempt_not_authorized"):
        demo_dispatch._source_request(replace(request, question_id="DEMO-03"), profile=profile)
    with pytest.raises(DemoDispatchRefused, match="attempt_budget_mismatch"):
        demo_dispatch._proof_and_budget(request, _budget(), proof, profile=profile)


def _dependencies(*, run_behavior="complete", daily_total=None, daily_caps=None,
                 request=None, budget=None, empty_anchor_no_query=False, missing_anchor_query=False):
    request = _request() if request is None else request
    budget = _budget() if budget is None else budget
    observed = {"budget": budget, "requests": [], "queries": [], "table_writes": [],
                "daily_calls": [], "model_transports": [], "events": [], "research_prompts": []}

    class Tables:
        def insert(self, table, rows, row_ids=None):
            observed["table_writes"].append((table, rows, row_ids))

    tables = Tables()

    class NoRetryModel:
        retries = 0
        timeout_s = 90

        def __init__(self):
            self._client = object()

        @property
        def client(self):
            return self._client

    research_module = SimpleNamespace(client_factory=lambda: object(), _generate=lambda: None)
    model_module = SimpleNamespace(
        MODEL="gemini-test",
        provider=lambda: "gemini",
        hold_usd=lambda *_args: 1.0,
        model_spend_today=lambda _warehouse, _now: total / ask_r2.MICROS,
        model_daily_usd=lambda *, now=None: 40.0,
    )

    def base_research(_ctx, *args, **_kwargs):
        observed["events"].append("research")
        if args:
            observed["research_prompts"].append(args[0])
        return {"note": "", "tokens": {"input": 0, "output": 0}, "usd": 0.0, "stopped": False}

    base = ask_module.Deps(
        warehouse=object(),
        socialcrawl=staging_check.refusing_socialcrawl,
        tables=tables,
        model=object(),
        research=base_research,
        now=lambda: datetime(2026, 10, 1, 12, tzinfo=staging_check.SAST),
    )

    def execute(sql, params=None, max_bytes=None):
        observed["queries"].append((sql, params, max_bytes))
        if "claim_checks" in str(sql):
            return {"rows": [dict(row) for table, rows, _ids in observed["table_writes"]
                    if table == ask_r2.CLAIM_TABLE for row in rows]}
        if "runs" in str(sql):
            return {"rows": []}
        return {"rows": []}

    run_id = "demo-ask-run-1"
    claim = {
        "answer_or_brief_id": None,
        "claim_id": "c1",
        "rule": "K1",
        "verdict": "pass",
        "checker": "unit-test",
        "run_id": run_id,
        "reason": None,
    }
    answer = {
        "short_answer": "The stored post names a venue.",
        "context": "",
        "evidence": [],
        "claims": [],
        "so_what": [],
        "watch_next": [],
        "gaps": [],
    }

    def run_ask(request_value, emit, _should_stop, *, deps=None):
        observed["requests"].append(dict(request_value))
        assert deps.model.budget is budget
        assert deps.warehouse.budget is budget
        ctx = RunContext(
            run_id=run_id,
            tier="T1",
            as_of=deps.now(),
            market=request.market,
            window_start=date(2026, 9, 1),
            window_end=date(2026, 10, 1),
        )
        deps.research(ctx, "Question: " + request.prompt)
        emit({"event": "step", "kind": "research", "text": "source context loaded"})
        def dispatch_one(ticket):
            try:
                budget.mark_dispatched(ticket)
            except BaseException:
                if not ticket.dispatched and not ticket.settled:
                    budget.settle(ticket, actual_usd=0.0)
                raise
            observed["model_transports"].append(ticket.record["phase"])

        if run_behavior == "daily_bound":
            ticket = budget.reserve_fixed(
                "structured", "gemini-test", budget.cap_micros / ask_r2.MICROS, 100)
            dispatch_one(ticket)
        if run_behavior == "rollover":
            for index in range(2):
                ticket = budget.reserve_fixed("structured", "gemini-test", 0.2, 100)
                dispatch_one(ticket)
                budget.settle(ticket, actual_usd=0.1)
                if index == 0:
                    wiring.now = lambda: datetime(2026, 10, 2, 0, tzinfo=staging_check.SAST)
            return {"answer": answer, "run": {"run_id": run_id, "tier": "T1", "model_usd": 0.2}}
        if run_behavior == "unknown":
            ticket = budget.reserve_fixed(
                "structured", "gemini-test", budget.cap_micros / ask_r2.MICROS, 100)
            dispatch_one(ticket)
            budget.settle(ticket, unknown=True)
            error = RuntimeError("provider failure")
            error.run = {
                "run_id": run_id,
                "tier": "T1",
                "model_usd": None,
                "window": {"from": "2026-09-01", "to": "2026-10-01"},
                "source_status": [],
                "notices": [],
            }
            observed["failure_run"] = error.run
            raise error
        ticket = budget.reserve_fixed("structured", "gemini-test", 0.3, 100)
        dispatch_one(ticket)
        observed["events"].append("model")
        budget.settle(ticket, actual_usd=0.2, actual_input_tokens=10, actual_output_tokens=5)
        claim["answer_or_brief_id"] = request_value["ask_id"]
        deps.tables.insert(ask_r2.CLAIM_TABLE, [dict(claim)], row_ids=["row-1"])
        return {
            "answer": answer,
            "run": {
                "run_id": run_id,
                "tier": "T1",
                "model_usd": 0.2,
                "window": {"from": "2026-09-01", "to": "2026-10-01"},
                "source_status": [],
                "notices": [],
            },
        }

    wiring = SimpleNamespace(
        execute=execute,
        ask_deps=lambda: base,
        run_ask=run_ask,
        run_embed=lambda *_args, **_kwargs: None,
        run_enrich=lambda *_args, **_kwargs: None,
        enrich_model=None,
        spent_today=lambda: 5.0,
        now=lambda: datetime(2026, 10, 1, 12, tzinfo=staging_check.SAST),
        warehouse=object(),
    )
    def fetch_posts(ctx, warehouse, ids, window):
        observed["events"].append("fetch_posts")
        observed["anchor_args"] = (warehouse, tuple(ids), window)
        if missing_anchor_query or (empty_anchor_no_query and not ids):
            return {"evidence": []}
        if run_behavior == "missing_anchor":
            evidence = []
        else:
            evidence = []
            for source_id in ids:
                record = {
                    "id": source_id,
                    "evidence_id": source_id,
                    "posted_at": "2026-09-29",
                    "text": f"<untrusted_content>Fetched source text for {source_id}</untrusted_content>",
                    "platform": "TikTok",
                    "url": None,
                }
                ctx.evidence[source_id] = dict(record)
                evidence.append(record)
        query_id = "q_anchor"
        ctx.queries[query_id] = {
            "sql": "SELECT anchored posts",
            "params": {"source_ids": list(ids)},
            "rows": evidence,
            "result_hash": "sha256:anchor-query",
            "purpose": "fetch_posts",
        }
        return {"evidence": evidence, "query_id": query_id,
                "skipped_outside_window": len(ids) - len(evidence)}

    modules = SimpleNamespace(
        ask=model_module,
        gemini_research=research_module,
        no_retry_model=NoRetryModel(),
        staging=staging_check,
        sql=SimpleNamespace(check_sql=lambda _sql: None, MAX_BYTES_BILLED=10_000_000),
        embed=SimpleNamespace(SPEND_ROW_SQL="INSERT INTO runs"),
        warehouse=SimpleNamespace(fetch_posts=fetch_posts),
        gemini=SimpleNamespace(usage_of=lambda *_args: {}),
    )
    total = 5_000_000 if daily_total is None else daily_total

    def fresh_daily_readback(day):
        observed["daily_calls"].append(day)
        daily_cap = (daily_caps or [40_000_000])[min(len(observed["daily_calls"]) - 1,
                                                       len(daily_caps or [40_000_000]) - 1)]
        return {
            "verified": True,
            "run_date": day.isoformat(),
            "all_pages_consumed": True,
            "canonical_rows": [{
                "run_id": "native-existing-row",
                "run_date": day.isoformat(),
                "model_usd_micros": total,
            }],
            "canonical_total_micros": total,
            "daily_cap_micros": daily_cap,
        }

    dependencies = SimpleNamespace(
        modules=modules,
        wiring=wiring,
        fresh_daily_readback=fresh_daily_readback,
    )
    return dependencies, observed


def test_dispatch_refuses_missing_fresh_daily_readback_before_ask():
    dependencies, observed = _dependencies()
    dependencies.fresh_daily_readback = None

    with pytest.raises(DemoDispatchRefused, match="fresh_daily_readback_missing"):
        dispatch_attempt(_request(), _budget(), funding_proof=_proof(), dependencies=dependencies)

    assert observed["requests"] == []
    assert observed["table_writes"] == []


def test_dispatch_runs_one_bounded_staging_ask_with_separate_frozen_context():
    dependencies, observed = _dependencies()
    budget = observed["budget"]
    prior_hold = dependencies.modules.ask.hold_usd
    prior_factory = dependencies.modules.gemini_research.client_factory
    prior_generate = dependencies.modules.gemini_research._generate

    receipt = dispatch_attempt(_request(), budget, funding_proof=_proof(), dependencies=dependencies)

    assert len(observed["requests"]) == 1
    passed = observed["requests"][0]
    assert passed["question"] == _request().prompt
    assert passed["market"] == "NG"
    assert passed["tier"] == "T1"
    assert passed["from_card"]["provenance"] == "frozen_demo_context"
    assert passed["from_card"]["posts"] == _request().provided_context["posts"]
    assert observed["events"] == ["fetch_posts", "research", "model"]
    assert observed["anchor_args"][1:] == (
        _request().source_ids, (date(2026, 9, 1), date(2026, 10, 1)))
    assert receipt["anchor_prefetch"]["resolved_ids"] == list(_request().source_ids)
    assert receipt["anchor_prefetch"]["query_id"] == "q_anchor"
    assert "frozen_demo_context_lookup" in observed["research_prompts"][0]
    assert receipt["question_id"] == "DEMO-01" and receipt["attempt_number"] == 1
    assert receipt["source"]["prompt_catalog_sha256"] == demo_pairs.DEMO_QUESTIONS_SHA256
    assert receipt["app_record"]["ask_id"] == receipt["prefix"]
    assert receipt["app_record"]["ask_id"].startswith("a_20261001_")
    assert receipt["app_record"]["answer"] == receipt["raw_answer"]
    assert receipt["app_record"]["run"] == receipt["run"]
    assert receipt["app_record"]["finished_at"]
    assert receipt["app_record"]["steps"][0]["text"] == "source context loaded"
    assert receipt["raw_answer"]["short_answer"] == "The stored post names a venue."
    assert receipt["call_costs"] == budget.calls
    assert receipt["request_statistics"] == [{"phase": "structured"}]
    assert receipt["claim_readback"]["match"] is True
    assert receipt["spend_readback"]["match"] is True
    assert receipt["spend_writes"] == []
    assert receipt["booking_intents"][0]["native_write"] is False
    assert [table for table, _rows, _ids in observed["table_writes"]] == [ask_r2.CLAIM_TABLE]
    assert observed["daily_calls"] == [demo_pairs.DEMO_RUN_DATE, demo_pairs.DEMO_RUN_DATE]
    assert budget.provider_500_retries == 0 and budget.provider_retry_statuses == ()
    assert dependencies.modules.ask.hold_usd is prior_hold
    assert dependencies.modules.gemini_research.client_factory is prior_factory
    assert dependencies.modules.gemini_research._generate is prior_generate


def test_dispatch_rejects_daily_total_above_cap_before_staging():
    dependencies, observed = _dependencies(daily_total=40_000_001)

    with pytest.raises(DemoDispatchRefused, match="daily_cap_exhausted"):
        dispatch_attempt(_request(), _budget(), funding_proof=_proof(), dependencies=dependencies)

    assert observed["requests"] == []
    assert observed["table_writes"] == []
    assert observed["daily_calls"] == []


def test_provider_failure_keeps_unknown_ceiling_and_never_retries():
    dependencies, observed = _dependencies(run_behavior="unknown")
    budget = observed["budget"]

    receipt = dispatch_attempt(_request(), budget, funding_proof=_proof(), dependencies=dependencies)

    assert receipt["outcome"] == "OPERATIONAL STOP"
    assert receipt["unknown_cost"] is True
    assert receipt["raw_answer"] is None
    assert receipt["model_usd_micros"] is None
    assert receipt["guarded_charge_micros"] == budget.cap_micros
    assert receipt["call_costs"][0]["status"] == "unknown_charged_ceiling"
    assert receipt["app_record"]["status"] == "failed"
    assert receipt["app_record"]["error_type"] == "RuntimeError"
    assert receipt["app_record"]["run"] == observed["failure_run"]
    assert receipt["app_record"]["finished_at"]
    assert len(receipt["call_costs"]) == 1
    assert budget.provider_500_retries == 0 and budget.provider_retry_statuses == ()
    assert observed["requests"] and len(observed["requests"]) == 1
    assert observed["table_writes"] == []


def test_daily_headroom_refuses_full_attempt_bound_before_transport():
    dependencies, observed = _dependencies(daily_total=39_000_000, run_behavior="daily_bound")
    budget = observed["budget"]

    receipt = dispatch_attempt(_request(), budget, funding_proof=_proof(), dependencies=dependencies)

    assert receipt["outcome"] == "OPERATIONAL STOP"
    assert receipt["stop_reason"] == "daily_cap_exceeded_before_transport"
    assert observed["model_transports"] == []
    assert budget.charged_micros == 0 and budget.reserved_micros == 0
    assert budget.calls[0]["status"] == "not_dispatched_zero"
    assert receipt["dispatch_daily_readbacks"][-1]["projected_total_micros"] == (
        39_000_000 + _request().attempt_cap_micros)
    assert receipt["dispatch_daily_readbacks"][-1]["daily_cap_micros"] == 40_000_000


def test_ranked_dispatch_reports_the_rank_proof_as_prompt_source():
    dependencies, _observed = _dependencies()
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    proof = _proof(consumed=426_564, app_run_ids=(f"prior-app-{i}" for i in range(5)))

    receipt = dispatch_attempt(
        _ranked_request(), _ranked_budget(), funding_proof=proof,
        dependencies=dependencies, profile=profile,
    )

    assert receipt["question_id"] == "NOW-01"
    assert receipt["source"]["prompt_catalog_sha256"] == profile.source_proof_sha256


def test_ranked_dispatch_without_source_ids_calls_guarded_research_without_anchor_prefetch():
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    request = _ranked_request()
    budget = _ranked_budget()
    dependencies, observed = _dependencies(
        request=request, budget=budget, empty_anchor_no_query=True)
    proof = _proof(consumed=426_564, app_run_ids=(f"prior-app-{i}" for i in range(5)))

    receipt = dispatch_attempt(
        request, budget, funding_proof=proof, dependencies=dependencies, profile=profile)

    assert receipt["outcome"] == "COMPLETE"
    assert observed["events"] == ["research", "model"]
    assert observed["research_prompts"] == ["Question: " + profile.question_catalog[0].text]
    assert receipt["anchor_prefetch"] is None
    assert observed["requests"][0]["from_card"]["posts"] == []
    assert budget.cap_micros == profile.attempt_cap_micros
    assert budget.provider_500_retries == 0 and budget.provider_retry_statuses == ()
    assert observed["model_transports"] == ["structured"]


def test_sast_rollover_between_transports_preserves_first_charge_and_refuses_second():
    dependencies, observed = _dependencies(run_behavior="rollover")
    budget = observed["budget"]

    receipt = dispatch_attempt(_request(), budget, funding_proof=_proof(), dependencies=dependencies)

    assert receipt["outcome"] == "OPERATIONAL STOP"
    assert receipt["stop_reason"] == "funding_sast_day_changed"
    assert observed["model_transports"] == ["structured"]
    assert [call["status"] for call in budget.calls] == ["charged_known", "not_dispatched_zero"]
    assert budget.charged_micros == 100_000
    assert observed["daily_calls"] == [demo_pairs.DEMO_RUN_DATE, demo_pairs.DEMO_RUN_DATE]


def test_missing_anchor_fails_before_any_model_transport():
    dependencies, observed = _dependencies(run_behavior="missing_anchor")
    budget = observed["budget"]

    receipt = dispatch_attempt(_request(), budget, funding_proof=_proof(), dependencies=dependencies)

    assert receipt["outcome"] == "OPERATIONAL STOP"
    assert receipt["anchor_prefetch"]["resolved_ids"] == []
    assert observed["events"] == ["fetch_posts"]
    assert observed["model_transports"] == []
    assert budget.charged_micros == 0


def test_research_refusal_reason_is_preserved_in_raw_receipt():
    dependencies, observed = _dependencies(missing_anchor_query=True)
    budget = observed["budget"]

    receipt = dispatch_attempt(_request(), budget, funding_proof=_proof(), dependencies=dependencies)

    assert receipt["outcome"] == "OPERATIONAL STOP"
    assert receipt["research_error_type"] == "DemoDispatchRefused"
    assert receipt["research_refusal_reason"] == "anchor_query_readback_missing"
    assert observed["model_transports"] == []
    assert budget.charged_micros == 0


def test_daily_cap_increase_between_preflight_and_transport_refuses_call():
    dependencies, observed = _dependencies(daily_caps=[40_000_000, 41_000_000])
    budget = observed["budget"]

    receipt = dispatch_attempt(_request(), budget, funding_proof=_proof(), dependencies=dependencies)

    assert receipt["outcome"] == "OPERATIONAL STOP"
    assert receipt["stop_reason"] == "daily_cap_changed_during_attempt"
    assert observed["daily_calls"] == [demo_pairs.DEMO_RUN_DATE, demo_pairs.DEMO_RUN_DATE]
    assert observed["model_transports"] == []
    assert budget.charged_micros == 0 and budget.reserved_micros == 0
    assert budget.calls[0]["status"] == "not_dispatched_zero"
