import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import threading
from types import ModuleType
from types import SimpleNamespace

import pytest

from core.eval import ask_r3_now_recovery as recovery


BASE_COSTS = {
    "NOW-01": 1.357150,
    "RISE-02": 1.646701,
    "WHY-03": 1.643623,
    "SPR-03": 1.805746,
    "CRE-01": 2.388517,
}
CRE_RUN_ID = "l3-ask-20260930-r3-CRE-01-spend-6a21605f"
CRE_PREFIX = "l3-ask-20260930-r3-CRE-01"
R3_ROOT = "l3-ask-20260930-r3"
RETRY_PREFIX = "l3-ask-20260930-r3-NOW-01-retry-1"
BASE_RUN_IDS = {
    "NOW-01": f"{R3_ROOT}-NOW-01-spend-12638a48",
    "RISE-02": f"{R3_ROOT}-RISE-02-spend-8e9d4c20",
    "WHY-03": f"{R3_ROOT}-WHY-03-spend-19e2f107",
    "SPR-03": f"{R3_ROOT}-SPR-03-spend-8a5f5498",
}
NOW_HASH = "c3d4c2d14a089a369e08178a301f93fdb0e8ce73fdf1cd466d5104b742e2d974"
SOURCE_153 = "15342e3c8faff2a1516443e55ca955403af37f0d"
SOURCE_E72 = "e72eaef2bb8506df20e7bde102aec6bdd5daeb06"
HEALTH_COMMIT = "5db40e553e2cc53260c4495d99850cbddd7f6b6a"


def _row(question_id, amount, *, run_id=None):
    prefix = f"{R3_ROOT}-{question_id}"
    run_id = run_id or BASE_RUN_IDS.get(question_id, f"{prefix}-spend-fixture")
    return {
        "run_id": run_id,
        "stage": "understand_spend",
        "run_date": "2026-09-30",
        "status": "ok",
        "counts": {"model_usd": amount, "what": f"staging_check_ask:{question_id}"},
    }


def _claim(question_id):
    return {
        "answer_or_brief_id": f"{R3_ROOT}-{question_id}",
        "claim_id": "c1",
        "rule": "K1",
        "verdict": "pass",
        "checker": "fixture",
        "run_id": f"fixture-{question_id}",
        "reason": "fixture evidence",
    }


def _report_row(row):
    counts = row["counts"]
    return {
        "run_id": row["run_id"],
        "stage": row["stage"],
        "run_date": row["run_date"],
        "status": row["status"],
        "what": counts["what"],
        "booked_usd": round(float(counts["model_usd"]), 6),
    }


def _receipt(question_id, amount, call_base, prior_rows):
    prefix = f"{R3_ROOT}-{question_id}"
    row = _row(question_id, amount)
    recoverable = question_id in ("NOW-01", "SPR-03")
    call = {
        "phase": "structured",
        "model": "gemini-3.8-flash",
        "input_bound_tokens": 100,
        "output_reserve_tokens": 20,
        "reserved_usd": amount + 0.1,
        "charged_usd": amount,
        "actual_input_tokens": 100,
        "actual_output_tokens": 20,
        "status": "charged_known",
    }
    if recoverable:
        call.update(provider_error_type="ProviderError",
                    provider_http_status=504 if question_id == "NOW-01" else 429)
    claim_rows = [] if recoverable else [_claim(question_id)]
    write = {
        "run_id": row["run_id"],
        "stage": row["stage"],
        "run_date": row["run_date"],
        "status": row["status"],
        "what": row["counts"]["what"],
        "model_usd": amount,
        "params": {"run_id": row["run_id"], "run_date": row["run_date"], "counts": json.dumps(row["counts"])},
        "submitted": True,
    }
    writes = [write]
    session_rows = prior_rows + [_report_row(row)]
    record = {
        "id": question_id,
        "attempt_key": question_id,
        "prefix": prefix,
        "outcome": "OPERATIONAL STOP" if recoverable else "PASS",
        "stop_reason": "research_failed" if recoverable else None,
        "error_type": "ProviderError" if recoverable else None,
        "research_error_type": None,
        "accounting_status": "metered_model_usage",
        "reported_model_usd": amount,
        "charged_usd": amount,
        "booked_usd": amount,
        "call_index_base": call_base,
        "call_costs": [call],
        "run": {"tier": "T1", "model_usd": amount},
        "bars": None if recoverable else {
            "schema_valid": True, "pass": True, "cited_in_window": 5,
            "platforms": ["x", "tiktok"], "unknown_ids": 0,
            "age_claims": 0, "numbers": 0, "numbers_reproduced": 0,
        },
        "raw_answer": None if recoverable else {"claims": ["c1"]},
        "claim_rows": claim_rows,
        "claim_readback": {"match": True, "rows": claim_rows},
        "spend_writes": writes,
        "spend_readback": {"match": True, "rows": [_report_row(row)]},
        "session_spend_readback": {
            "match": True, "rows": session_rows,
            "booked_usd": round(sum(item["booked_usd"] for item in session_rows), 6),
        },
        "spend_intents": [{
            "usd": amount, "reported_usd": amount, "guarded_minimum_usd": amount,
            "booked_usd": amount, "what": row["counts"]["what"],
            "credit": False, "run_prefix": prefix,
        }],
        "session_cap_usd": 10.0,
    }
    if question_id != "NOW-01":
        record["r3_continuation"] = {
            "evaluation_label": "R3",
            "initial_source_commit": SOURCE_153,
            "initial_source_metadata_sha256": recovery.INITIAL_SOURCE_METADATA_SHA256,
            "initial_now_record_sha256": recovery.INITIAL_NOW_SHA256,
            "initial_report_sha256": recovery.INITIAL_REPORT_SHA256,
            "initial_booked_usd": BASE_COSTS["NOW-01"],
            "resume_source_commit": SOURCE_E72,
            "resume_source_clean": True,
            "health_at_resume": {"http_status": 200, "ok": True, "version": "5db40e553e2c",
                                 "git_commit": HEALTH_COMMIT},
            "retry_attempt_key": "NOW-01-retry-1",
            "provider_retry_statuses": [500, 504],
            "now_retry_provider_retry_statuses": [],
            "attempt_order": [
                {"id": "RISE-02", "attempt_key": "RISE-02"},
                {"id": "WHY-03", "attempt_key": "WHY-03"},
                {"id": "SPR-03", "attempt_key": "SPR-03"},
                {"id": "CRE-01", "attempt_key": "CRE-01"},
                {"id": "NOW-01", "attempt_key": "NOW-01-retry-1"},
            ],
        }
    return record, row, claim_rows


def _fixture(tmp_path, monkeypatch, *, native_change=False, retry_marker=False, retry_native=False,
             old_now_change=False, reserve_price=200, fail_call=False,
             omit_reserved_readback=False, extra_spend=False, daily_outside_usd=0.0,
             daily_cap_usd=100.0, daily_spend_bump=0.0, extra_hold_at_model=False,
             hold_after_initial_check=False,
             replace_hold_between_calls=False,
             omit_continuation_readback=False, flip_day_after_reservation=False):
    data_dir = tmp_path / "R3-data"
    data_dir.mkdir()
    prior_rows = []
    local_claims = {}
    records = {}
    call_base = 0
    for question_id in ("NOW-01", "RISE-02", "WHY-03", "SPR-03"):
        record, row, claims = _receipt(question_id, BASE_COSTS[question_id], call_base, prior_rows)
        records[question_id] = record
        prior_rows.append(_report_row(row))
        local_claims[f"{R3_ROOT}-{question_id}"] = claims
        call_base += len(record["call_costs"])
        (data_dir / f"{question_id}.attempted").write_bytes(b"attempt started\n")
        (data_dir / f"{question_id}.json").write_text(json.dumps(record), encoding="utf-8")
    (data_dir / "CRE-01.attempted").write_bytes(b"attempt started\n")
    now_path = data_dir / "NOW-01.json"
    now_bytes = now_path.read_bytes()
    expected_now_hash = hashlib.sha256(now_bytes).hexdigest()
    if old_now_change:
        now_path.write_bytes(now_bytes + b" ")
    if retry_marker:
        (data_dir / "NOW-01-retry-1.attempted").write_bytes(b"attempt started\n")

    cre_row = _row("CRE-01", BASE_COSTS["CRE-01"], run_id=CRE_RUN_ID)
    base_rows = [cre_row] + [
        _row(q, BASE_COSTS[q], run_id=records[q]["spend_writes"][0]["run_id"])
        for q in ("NOW-01", "RISE-02", "WHY-03", "SPR-03")
    ]
    snapshot = {
        "checked_at_utc": "2026-09-30T18:23:23.612421+00:00",
        "prefix": CRE_PREFIX,
        "spend_rows": [cre_row],
        "claim_rows": [],
        "session_rows": base_rows,
    }
    if native_change:
        snapshot["session_rows"][0]["counts"]["model_usd"] += 0.000001
    snapshot_path = tmp_path / "interruption.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    source_metadata = {"source_commit": SOURCE_153, "source_clean": True}
    source_path = tmp_path / "source.json"
    source_path.write_text(json.dumps(source_metadata), encoding="utf-8")
    initial_report = tmp_path / "initial.md"
    initial_report.write_text("initial report\n", encoding="utf-8")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    initial_report_hash = hashlib.sha256(initial_report.read_bytes()).hexdigest()
    for question_id in ("RISE-02", "WHY-03", "SPR-03"):
        records[question_id]["r3_continuation"].update(
            initial_source_metadata_sha256=source_hash,
            initial_now_record_sha256=expected_now_hash,
            initial_report_sha256=initial_report_hash,
        )
        (data_dir / f"{question_id}.json").write_text(
            json.dumps(records[question_id]), encoding="utf-8")
    current_report = tmp_path / "current.md"
    current_report.write_text("current report stays unchanged\n", encoding="utf-8")
    audit = {
        "checked_at_utc": "2026-09-30T18:32:30.540105+00:00",
        "cre_book": {"run_id": CRE_RUN_ID},
        "all_pages_consumed": True,
        "query_jobs": [{"state": "DONE", "semantic": False} for _ in range(50)],
        "postbook_semantic_jobs": [],
        "unresolved_jobs": [],
        "verdict": "NO_POSTBOOK_SEMANTIC_JOBS",
    }
    audit_path = tmp_path / "postbook.json"
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    monkeypatch.setattr(recovery, "INITIAL_NOW_SHA256", expected_now_hash)
    monkeypatch.setattr(recovery, "INITIAL_SOURCE_METADATA_SHA256", source_hash)
    monkeypatch.setattr(recovery, "INITIAL_REPORT_SHA256", initial_report_hash)
    monkeypatch.setattr(recovery, "POSTBOOK_AUDIT_SHA256", hashlib.sha256(audit_path.read_bytes()).hexdigest())

    native = SimpleNamespace(
        rows=base_rows,
        new_rows=[_row("NOW-01", 0.2, run_id=f"{RETRY_PREFIX}-spend-fixture")]
        if retry_native else [],
        claims=local_claims,
        model_calls=0,
        write_count=0,
        reservation_seen_before_model=False,
        reservation_amount=None,
        omit_reserved_readback=omit_reserved_readback,
        omit_continuation_readback=omit_continuation_readback,
        extra_spend=extra_spend,
        extra_spend_refused=False,
        daily_outside_usd=daily_outside_usd,
        daily_cap_usd=daily_cap_usd,
        daily_spend_bump=daily_spend_bump,
        daily_spent_seen=None,
        extra_hold_at_model=extra_hold_at_model,
        hold_after_initial_check=hold_after_initial_check,
        initial_check_hold_injected=False,
        replace_hold_between_calls=replace_hold_between_calls,
        flip_day_after_reservation=flip_day_after_reservation,
    )
    native.rows = json.loads(json.dumps(native.rows))
    if native_change:
        native.rows[0]["counts"]["model_usd"] += 0.000001

    def execute(sql, params=None, max_bytes=None):
        params = dict(params or {})
        if sql == "SPEND_INSERT":
            native.write_count += 1
            counts = json.loads(params["counts"])
            row = {"run_id": params["run_id"], "stage": "understand_spend",
                   "run_date": params["run_date"].isoformat(), "status": "ok", "counts": counts}
            native.new_rows.append(row)
            native.reservation_amount = counts["model_usd"]
            native.daily_outside_usd += native.daily_spend_bump
            return {"rows": []}
        if "claim_checks" in sql:
            return {"rows": native.claims.get(params.get("prefix"), [])}
        if "runs" in sql:
            prefix = params.get("prefix", "")
            rows = native.rows + native.new_rows
            if native.omit_reserved_readback:
                rows = [row for row in rows if not row["run_id"].startswith(f"{RETRY_PREFIX}-spend-")]
            if native.omit_continuation_readback:
                rows = [row for row in rows if not row["run_id"].startswith(f"{recovery.CONTINUATION_PREFIX}-spend-")]
            return {"rows": [row for row in rows if row["run_id"].startswith(prefix)]}
        return {"rows": []}

    class FakeWarehouse:
        def run(self, sql, params, max_bytes_billed):
            params = dict(params or {})
            if "claim_checks" in sql:
                return native.claims.get(params.get("prefix"), [])
            if "runs" in sql:
                rows = native.rows + native.new_rows
                if native.omit_reserved_readback:
                    rows = [row for row in rows if not row["run_id"].startswith(f"{RETRY_PREFIX}-spend-")]
                if native.omit_continuation_readback:
                    rows = [row for row in rows if not row["run_id"].startswith(f"{recovery.CONTINUATION_PREFIX}-spend-")]
                prefix = params.get("prefix", "")
                return [row for row in rows if row["run_id"].startswith(prefix)]
            return []

    ask_module_ref = {}

    def model_spend_today(warehouse, now):
        if warehouse.backend is None:
            raise AttributeError("fake warehouse backend missing")
        if native.hold_after_initial_check and not native.initial_check_hold_injected:
            ask_module = ask_module_ref["module"]
            with ask_module._SPEND_LOCK:
                ask_module._IN_FLIGHT[object()] = 1.0
                ask_module._IN_FLIGHT[object()] = 2.0
            native.initial_check_hold_injected = True
        day = now.astimezone(timezone.utc).date().isoformat()
        daily_rows = [row for row in native.rows + native.new_rows if str(row["run_date"]) == day]
        return round(native.daily_outside_usd + sum(float(row["counts"]["model_usd"])
                                                     for row in daily_rows), 6)

    class FakeTables:
        def insert(self, table, rows, row_ids=None):
            native.claims.setdefault(rows[0]["answer_or_brief_id"], []).extend(rows)

    @dataclass
    class FakeDeps:
        warehouse: object
        socialcrawl: object
        tables: object
        model: object
        research: object
        now: object = lambda: datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        check: object = None
        spent_today_usd: object = None

    class FakeClientModels:
        def generate_content(self, *args, **kwargs):
            native.reservation_seen_before_model = (
                native.model_calls == 0 and len(native.new_rows) == 1
                and native.new_rows[0]["run_id"].startswith(f"{RETRY_PREFIX}-spend-")
                and round(native.new_rows[0]["counts"]["model_usd"], 6) == 1.158263
            )
            native.model_calls += 1
            if fail_call:
                error = RuntimeError("provider timeout")
                error.code = 504
                raise error
            return SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=10))

    class FakeModel:
        retries = 0
        timeout_s = 120

        def __init__(self):
            self._client = SimpleNamespace(models=FakeClientModels())

        @property
        def client(self):
            return self._client

        def complete_json(self, **kwargs):
            self.client.models.generate_content(model=kwargs["model"], contents=kwargs["user"])
            if fail_call:
                raise AssertionError("provider error should have propagated")
            return {"claims": ["c1"]}, {"usd": 0.2, "input_tokens": 10, "output_tokens": 5}

    staging = SimpleNamespace(
        SAST=timezone.utc,
        refusing_socialcrawl=lambda *args, **kwargs: None,
        choose_questions=lambda ids: [{"id": "NOW-01", "question": "Question", "markets": ["ZA"]}],
    )

    def run_ask(request, emit, should_stop, *, deps=None):
        own_hold = object()
        other_hold = object()
        with modules.ask._SPEND_LOCK:
            modules.ask._IN_FLIGHT[own_hold] = 1.0
            if native.extra_hold_at_model:
                modules.ask._IN_FLIGHT[other_hold] = 2.0
        try:
            native.daily_spent_seen = deps.spent_today_usd()
            ctx = SimpleNamespace(run_id="run-now", tier="T1", queries={}, events=[], evidence=[])
            deps.research(ctx)
            answer, usage = deps.model.complete_json(
                model="gemini-3.8-flash", system="system", user="question", schema={"type": "object"}, max_tokens=1
            )
            if native.replace_hold_between_calls:
                with modules.ask._SPEND_LOCK:
                    modules.ask._IN_FLIGHT.pop(own_hold, None)
                    own_hold = object()
                    modules.ask._IN_FLIGHT[own_hold] = 1.0
                deps.model.complete_json(
                    model="gemini-3.8-flash", system="system", user="question", schema={"type": "object"}, max_tokens=1
                )
            deps.tables.insert("intelligence_42_agent.claim_checks", [{
                "answer_or_brief_id": request["ask_id"], "claim_id": "c1", "rule": "K1",
                "verdict": "pass", "checker": "fixture", "run_id": "run-now", "reason": "fixture evidence",
            }])
            return {"answer": answer, "run": {"run_id": "run-now", "tier": "T1", "model_usd": usage["usd"], "notices": []}}
        finally:
            with modules.ask._SPEND_LOCK:
                modules.ask._IN_FLIGHT.pop(own_hold, None)
                modules.ask._IN_FLIGHT.pop(other_hold, None)

    def check_ask(wiring, book, questions, record_dir=None):
        assert len(questions) == 1 and questions[0]["id"] == "NOW-01"
        request = {"tier": "T1", "mode": "live"}
        try:
            result = wiring.run_ask(request, lambda event: None, lambda: False, deps=wiring.ask_deps())
            row = {"id": "NOW-01", "answer": result["answer"], "run": result["run"], "error": None,
                   "bars": {"schema_valid": True, "pass": True, "cited_in_window": 5,
                            "platforms": ["x", "tiktok"], "unknown_ids": 0,
                            "age_claims": 0, "numbers": 0, "numbers_reproduced": 0}}
        except Exception as exc:
            row = {"id": "NOW-01", "answer": None, "run": {}, "error": type(exc).__name__,
                   "error_type": type(exc).__name__, "bars": None}
        row["booked_usd"] = book((row.get("run") or {}).get("model_usd", 0), "staging_check_ask:NOW-01")
        recovery.shared._semantic_invocation_count("semantic", {})
        wiring.warehouse.run("semantic", {}, 1000)
        if native.extra_spend:
            try:
                wiring.execute("SPEND_INSERT", {
                    "run_id": f"{RETRY_PREFIX}-spend-extra",
                    "run_date": date(2026, 9, 30),
                    "counts": json.dumps({"model_usd": 0.01, "what": "staging_check_ask:NOW-01"}),
                })
            except recovery.shared.OperationRefused as exc:
                native.extra_spend_refused = exc.args[0] == "post_reservation_spend_write_forbidden"
                raise
        return {"questions": [row], "pass": bool(row.get("bars", {}).get("pass"))}

    staging.check_ask = check_ask
    modules = SimpleNamespace(
        staging=staging,
        ask=SimpleNamespace(MODEL="gemini-3.8-flash", hold_usd=lambda tier, model: 1.0,
                            model_spend_today=model_spend_today,
                            model_daily_usd=lambda now: native.daily_cap_usd,
                            BUDGET_SPENT="budget spent", _IN_FLIGHT={}, _SPEND_LOCK=threading.RLock()),
        sql=SimpleNamespace(MAX_BYTES_BILLED=1000, check_sql=lambda sql: None),
        embed=SimpleNamespace(
            SPEND_ROW_SQL="SPEND_INSERT",
            book_spend=lambda execute, *, run_id, run_date, usd, what, credit=False: _book(
                execute, run_id, run_date, usd, what, credit),
        ),
        gemini_research=SimpleNamespace(client_factory=lambda: None, _generate=lambda *args: None),
        no_retry_model=FakeModel(),
    )
    ask_module_ref["module"] = modules.ask
    wiring = SimpleNamespace(
        execute=execute,
        ask_deps=lambda: FakeDeps(
            warehouse=FakeWarehouse(), socialcrawl=None, tables=FakeTables(), model=None,
            research=lambda ctx: None),
        run_ask=run_ask,
        warehouse=FakeWarehouse(),
        now=lambda: (datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc)
                     if native.flip_day_after_reservation and native.new_rows
                     else datetime(2026, 9, 30, 12, tzinfo=timezone.utc)),
    )
    context = {
        "repo_root": tmp_path,
        "data_dir": data_dir,
        "snapshot_path": snapshot_path,
        "source_metadata_path": source_path,
        "initial_report_path": initial_report,
        "current_report_path": current_report,
        "postbook_audit_path": audit_path,
        "modules": modules,
        "wiring": wiring,
        "authority": {"source_clean": True, "source_commit": SOURCE_E72},
        "health": {"http_status": 200, "ok": True, "version": "fresh-version", "git_commit": HEALTH_COMMIT},
        "budget_factory": lambda cap: recovery.shared.SessionBudget(
            cap_usd=cap, price_for=lambda model: {"input": reserve_price, "output": 0}, provider_500_retries=0
        ),
    }
    return context, native, data_dir, current_report


def _continuation_context(tmp_path, monkeypatch, **fixture_options):
    context, native, closed_data, _ = _fixture(tmp_path, monkeypatch, **fixture_options)
    old_row = _row("NOW-01", 1.158263, run_id=f"{RETRY_PREFIX}-spend-3f5031e1")
    native.rows.append(old_row)
    native.rows.sort(key=lambda item: item["run_id"])
    (closed_data / "NOW-01-retry-1.attempted").write_bytes(b"attempt started\n")
    session_rows = recovery.shared._spend_rows_for_report(native.rows)
    old_retry_record = {
        "stage": "COMPLETE", "id": "NOW-01", "attempt_key": "NOW-01-retry-1",
        "prefix": RETRY_PREFIX, "phase_reserved_usd": 1.158263,
        "charged_usd": 0.660648, "phase_unspent_reserved_usd": 0.497615,
        "raw_answer": None,
        "session_spend_readback": {"match": True, "rows": session_rows, "booked_usd": 10.0},
        "spend_writes": [{"run_id": old_row["run_id"], "model_usd": 1.158263,
                          "what": "staging_check_ask:NOW-01", "submitted": True}],
    }
    (closed_data / "NOW-01-retry-1.json").write_text(json.dumps(old_retry_record), encoding="utf-8")
    closed_report = tmp_path / "ASK-2026-09-30-R3.md"
    closed_report.write_text("Closed R3 report fixture\n", encoding="utf-8")
    final_receipt = tmp_path / "r3-final-receipt.json"
    final_receipt.write_text(json.dumps({
        "native_session_booked_total_usd": "10.000000",
        "cap_usd": "10",
        "recorded_headroom_usd": "0.000000",
        "retry_unspent_reservation_usd": "0.497615",
    }), encoding="utf-8")
    interruption = tmp_path / "ASK-2026-09-30-R3-interruption-readback.json"
    postbook = tmp_path / "ASK-2026-09-30-R3-postbook-job-audit.json"
    interruption.write_bytes(context["snapshot_path"].read_bytes())
    postbook.write_bytes(context["postbook_audit_path"].read_bytes())
    files = [closed_report, final_receipt, interruption, postbook]
    files.extend(closed_data.iterdir())
    manifest = {"schema_version": "r3-closed-evidence-v1", "native_booked_usd": "10.000000",
                "files": {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    assert len(manifest["files"]) == 15
    manifest_path = tmp_path / "closed-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    approval = {
        "schema_version": "r3-now-continuation-approval-v1",
        "prior_native_booking_usd": "10.000000",
        "prior_unused_reservation_included_usd": "0.497615",
        "additional_allowance_usd": "15.000000",
        "cumulative_cap_usd": "25.000000",
        "question_ids": ["NOW-01"],
        "maximum_new_question_executions": 1,
        "automatic_retries": 0,
        "release_prior_reservations": False,
    }
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    monkeypatch.setattr(recovery, "CLOSED_MANIFEST_SHA256", hashlib.sha256(manifest_path.read_bytes()).hexdigest())
    monkeypatch.setattr(recovery, "CONTINUATION_APPROVAL_SHA256", hashlib.sha256(approval_path.read_bytes()).hexdigest())
    continuation_dir = tmp_path / "continuation-data"
    context["phase_config"] = {
        "kind": "continuation-1", "data_dir": continuation_dir,
        "closed_data_dir": closed_data, "approval_path": approval_path, "manifest_path": manifest_path,
    }
    return context, native, closed_data, manifest


def _prefunded_context(tmp_path, monkeypatch):
    context, native, closed_data, manifest = _continuation_context(
        tmp_path, monkeypatch, daily_outside_usd=13.935557, daily_cap_usd=40.0)
    continuation_dir = context["phase_config"]["data_dir"]
    continuation_dir.mkdir()
    marker = continuation_dir / f"{recovery.CONTINUATION_KEY}.attempted"
    marker.write_bytes(b"attempt started\n")
    reservation = _row("NOW-01", 15, run_id=recovery.CONTINUATION_RESERVATION_RUN_ID)
    native.rows.append(reservation)
    native.rows.sort(key=lambda item: item["run_id"])
    setup_log = tmp_path / "ASK-2026-09-30-R3-CONTINUATION-1-SETUP-LOG.txt"
    setup_log.write_text("prefunded setup readback fixture\n", encoding="utf-8")
    setup_readback = tmp_path / "ASK-2026-09-30-R3-CONTINUATION-1-SETUP-READBACK.json"
    setup_readback.write_text(json.dumps({
        "checked_at": "2026-09-30T22:42:35+02:00",
        "source_commit": "ae3f91e0a2c70397a7e2cd9508bd218829d6fe13",
        "daily_spend_usd": 38.935557,
        "daily_cap_usd": 40.0,
        "session_rows": native.rows,
        "phase_rows": [reservation],
        "claim_rows": [],
    }), encoding="utf-8")
    daily_audit = tmp_path / "ASK-2026-09-30-R3-CONTINUATION-1-DAILY-QUERY-AUDIT.json"
    daily_audit.write_text(json.dumps({
        "all_pages_consumed": True,
        "jobs_examined": 18,
        "matched_daily_queries": [
            {"job_id": "c0aa7df0-8b1a-486e-959a-e4e6ccd4e7be", "cache_hit": False,
             "rows": [{"usd": 23.935557000000003}]},
            {"job_id": "9045e80a-a772-464f-88b5-957ec19888a3", "cache_hit": False,
             "rows": [{"usd": 38.935557}]},
        ],
    }), encoding="utf-8")
    proof_files = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (setup_log, setup_readback, daily_audit, marker)}
    proof = {
        "schema_version": "r3-prefunded-setup-recovery-v1",
        "failed_source_commit": "ae3f91e0a2c70397a7e2cd9508bd218829d6fe13",
        "terminal_session": 84859,
        "observed_exit_code": 1,
        "observed_refusal": "daily_cap_exceeded_after_phase_reservation",
        "guard_position": "after reservation readback and before the sole staging.check_ask invocation",
        "paid_model_dispatches": 0,
        "no_dispatch_basis": "Observed outer refusal from the identified source control flow; zero claims alone are not the proof.",
        "phase_prefix": recovery.CONTINUATION_PREFIX,
        "reservation_run_id": recovery.CONTINUATION_RESERVATION_RUN_ID,
        "reservation_usd": "15.000000",
        "cumulative_native_usd": "25.000000",
        "native_daily_usd": "38.935557",
        "daily_cap_usd": "40.000000",
        "native_claim_rows": 0,
        "before_query_job": "c0aa7df0-8b1a-486e-959a-e4e6ccd4e7be",
        "after_query_job": "9045e80a-a772-464f-88b5-957ec19888a3",
        "before_daily_raw": "23.935557000000003",
        "after_daily_raw": "38.935557",
        "approval_sha256": recovery.CONTINUATION_APPROVAL_SHA256,
        "closed_manifest_sha256": recovery.CLOSED_MANIFEST_SHA256,
        "reuse_only_no_new_booking": True,
        "automatic_model_repeats": 0,
        "files": proof_files,
    }
    proof_path = tmp_path / "ASK-2026-09-30-R3-CONTINUATION-1-PREFUNDED-RECOVERY.json"
    proof_path.write_text(json.dumps(proof), encoding="utf-8")
    monkeypatch.setattr(recovery, "PREFUNDED_RECOVERY_SHA256",
                        hashlib.sha256(proof_path.read_bytes()).hexdigest())
    context["phase_config"]["resume_prefunded"] = True
    context["phase_config"]["prefunded_proof_path"] = proof_path
    return context, native, continuation_dir, proof_path, setup_log


def _book(execute, run_id, run_date, usd, what, credit):
    payload = {"model_usd": round(float(usd), 6), "what": what}
    suffix = f"fixture-{round(float(usd) * 1_000_000)}"
    execute("SPEND_INSERT", {
        "run_id": f"{run_id}-spend-{suffix}", "run_date": run_date,
        "counts": json.dumps(payload),
    })
    return payload["model_usd"]


def _run(context):
    return recovery._execute_now_retry(**context)


def test_phase_ceiling_is_booked_before_model_and_scoring_cost_is_reported(tmp_path, monkeypatch):
    context, native, data_dir, current_report = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(recovery.shared, "_semantic_invocation_count", lambda sql, params: 1)
    original_report_hash = hashlib.sha256(current_report.read_bytes()).hexdigest()

    record = _run(context)

    saved = json.loads((data_dir / "NOW-01-retry-1.json").read_text(encoding="utf-8"))
    assert record["attempt_key"] == "NOW-01-retry-1"
    assert record["call_index_scope"] == "isolated_recovery_phase"
    assert record["native_base_usd"] == 8.841737
    assert record["aggregate_cap_usd"] == 10.0
    assert saved["stage"] == "COMPLETE"
    assert native.model_calls == 1
    assert native.write_count == 1
    assert native.reservation_seen_before_model
    assert native.reservation_amount == pytest.approx(1.158263)
    assert record["charged_usd"] == pytest.approx(0.200308)
    assert record["phase_reserved_usd"] == pytest.approx(1.158263)
    assert record["phase_unspent_reserved_usd"] == pytest.approx(0.957955)
    assert record["session_spend_readback"]["booked_usd"] == pytest.approx(10.0)
    assert len(record["spend_writes"]) == 1
    assert hashlib.sha256(current_report.read_bytes()).hexdigest() == original_report_hash
    with pytest.raises(recovery.shared.OperationRefused):
        _run(context)
    assert native.model_calls == 1
    assert native.write_count == 1


def test_normalizes_real_wiring_with_warehouse_from_ask_deps(tmp_path, monkeypatch):
    context, native, _, _ = _fixture(tmp_path, monkeypatch)
    context["wiring"].warehouse = None

    record = _run(context)

    assert record["phase_reserved_usd"] == pytest.approx(1.158263)
    assert native.model_calls == 1
    assert native.write_count == 1
    assert native.reservation_seen_before_model
    assert native.reservation_amount == pytest.approx(1.158263)


@pytest.mark.parametrize("change", ["native", "retry_marker", "retry_native", "old_now"])
def test_mismatched_base_or_consumed_retry_refuses_before_model_dispatch(tmp_path, monkeypatch, change):
    context, native, _, _ = _fixture(
        tmp_path, monkeypatch, native_change=change == "native",
        retry_marker=change == "retry_marker", retry_native=change == "retry_native",
        old_now_change=change == "old_now",
    )

    with pytest.raises(recovery.shared.OperationRefused):
        _run(context)

    assert native.model_calls == 0
    assert native.write_count == 0


def test_budget_exhaustion_refuses_before_provider_dispatch(tmp_path, monkeypatch):
    context, native, data_dir, _ = _fixture(tmp_path, monkeypatch, reserve_price=5000)

    record = _run(context)

    assert native.model_calls == 0
    assert native.write_count == 1
    assert record["charged_usd"] == 0
    assert record["call_costs"][0]["status"] == "refused_before_dispatch"
    assert record["phase_reserved_usd"] == pytest.approx(1.158263)
    assert native.reservation_amount == pytest.approx(1.158263)
    assert (data_dir / "NOW-01-retry-1.attempted").read_bytes() == b"attempt started\n"


def test_failed_call_books_its_reserved_ceiling_and_has_readbacks(tmp_path, monkeypatch):
    context, native, _, _ = _fixture(tmp_path, monkeypatch, fail_call=True)
    monkeypatch.setattr(recovery.shared, "_semantic_invocation_count", lambda sql, params: 0)

    record = _run(context)

    assert native.model_calls == 1
    assert native.write_count == 1
    assert native.reservation_seen_before_model
    assert record["call_costs"][0]["status"] == "unknown_charged_ceiling"
    assert record["charged_usd"] == record["call_costs"][0]["reserved_usd"]
    assert record["phase_reserved_usd"] == pytest.approx(1.158263)
    assert record["phase_unspent_reserved_usd"] == pytest.approx(1.158263 - record["charged_usd"])
    assert record["session_spend_readback"]["booked_usd"] == pytest.approx(10.0)
    assert record["spend_readback"]["match"] is True
    assert record["session_spend_readback"]["match"] is True


def test_continuation_uses_closed_ten_base_without_readding_unused_reservation(tmp_path, monkeypatch):
    context, native, closed_data, manifest = _continuation_context(
        tmp_path, monkeypatch, daily_outside_usd=13.935557, daily_cap_usd=40.0, daily_spend_bump=0.5)
    ask_module = ModuleType("fake_ask_module")
    ask_module.__dict__.update(vars(context["modules"].ask))
    context["modules"].ask = ask_module
    original_spend_reader = ask_module.model_spend_today
    closed_hashes = {Path(path): digest for path, digest in manifest["files"].items()}

    record = _run(context)

    assert record["attempt_key"] == "NOW-01-continuation-1"
    assert record["phase_reserved_usd"] == pytest.approx(15.0)
    assert record["aggregate_cap_usd"] == pytest.approx(25.0)
    assert record["aggregate_booked_usd"] == pytest.approx(25.0)
    assert record["r3_isolated_recovery"]["prior_unused_reservation_included_usd"] == pytest.approx(0.497615)
    assert record["r3_isolated_recovery"]["prior_unused_reservation_added_again"] is False
    assert native.daily_spent_seen == pytest.approx(24.435557)
    assert ask_module.model_spend_today is original_spend_reader
    ordinary_total = original_spend_reader(
        recovery.shared.ReadOnlyWarehouse(context["wiring"].ask_deps().warehouse,
                                         context["modules"].sql.check_sql),
        context["wiring"].now())
    assert ordinary_total == pytest.approx(39.435557)
    assert native.model_calls == 1
    assert native.write_count == 1
    with pytest.raises(recovery.shared.OperationRefused, match="continuation_local_attempt_exists"):
        _run(context)
    assert native.model_calls == 1
    assert native.write_count == 1
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
               for path, digest in closed_hashes.items())


def test_continuation_caps_phase_to_positive_daily_headroom(tmp_path, monkeypatch):
    context, native, _, _ = _continuation_context(
        tmp_path, monkeypatch, daily_outside_usd=16.0, daily_cap_usd=40.0)

    record = _run(context)

    assert record["phase_reserved_usd"] == pytest.approx(14.0)
    assert record["aggregate_booked_usd"] == pytest.approx(24.0)
    assert record["aggregate_cap_usd"] == pytest.approx(25.0)
    assert native.model_calls == 1
    assert native.write_count == 1


def test_continuation_refuses_existing_inflight_hold_before_reserving(tmp_path, monkeypatch):
    context, native, _, _ = _continuation_context(tmp_path, monkeypatch)
    ask_module = context["modules"].ask
    with ask_module._SPEND_LOCK:
        ask_module._IN_FLIGHT[object()] = 2.0

    with pytest.raises(recovery.shared.OperationRefused, match="ask_hold_exists_before_reservation"):
        _run(context)

    assert native.write_count == 0
    assert native.model_calls == 0


def test_continuation_rechecks_holds_under_lock_immediately_before_booking(tmp_path, monkeypatch):
    context, native, _, _ = _continuation_context(
        tmp_path, monkeypatch, hold_after_initial_check=True)

    with pytest.raises(recovery.shared.OperationRefused, match="ask_hold_exists_before_reservation"):
        _run(context)

    assert native.initial_check_hold_injected is True
    assert native.write_count == 0
    assert native.model_calls == 0
    with context["modules"].ask._SPEND_LOCK:
        context["modules"].ask._IN_FLIGHT.clear()


def test_continuation_refuses_other_hold_at_provider_boundary(tmp_path, monkeypatch):
    context, native, _, _ = _continuation_context(tmp_path, monkeypatch, extra_hold_at_model=True)

    record = _run(context)

    assert record["phase_reserved_usd"] == pytest.approx(15.0)
    assert record["aggregate_booked_usd"] == pytest.approx(25.0)
    assert native.model_calls == 0
    assert native.write_count == 1
    assert any(call.get("status") == "not_dispatched_zero" for call in record["call_costs"])


def test_continuation_refuses_replaced_hold_identity_before_second_dispatch(tmp_path, monkeypatch):
    context, native, _, _ = _continuation_context(tmp_path, monkeypatch, replace_hold_between_calls=True)

    record = _run(context)

    assert native.model_calls == 1
    assert native.write_count == 1
    assert record["call_costs"][-1]["status"] == "not_dispatched_zero"
    assert record["call_costs"][-1]["stop_reason"] == "continuation_concurrent_ask_hold"
    assert record["r3_isolated_recovery"]["automatically_retried"] is False


def test_continuation_failed_prebook_readback_blocks_dispatch(tmp_path, monkeypatch):
    context, native, _, _ = _continuation_context(tmp_path, monkeypatch, omit_continuation_readback=True)

    with pytest.raises(recovery.shared.OperationRefused, match="phase_reservation_readback_failed"):
        _run(context)

    assert native.write_count == 1
    assert native.model_calls == 0


def test_continuation_refuses_if_daily_total_moves_over_cap_after_prebooking(tmp_path, monkeypatch):
    context, native, _, _ = _continuation_context(
        tmp_path, monkeypatch, daily_outside_usd=13.935557, daily_cap_usd=40.0, daily_spend_bump=2.0)

    with pytest.raises(recovery.shared.OperationRefused, match="daily_cap_exceeded_after_phase_reservation"):
        _run(context)

    assert native.write_count == 1
    assert native.model_calls == 0


def test_continuation_refuses_after_booked_sast_day_changes(tmp_path, monkeypatch):
    context, native, _, _ = _continuation_context(tmp_path, monkeypatch, flip_day_after_reservation=True)

    with pytest.raises(recovery.shared.OperationRefused, match="phase_reservation_sast_day_changed"):
        _run(context)

    assert native.write_count == 1
    assert native.model_calls == 0


def test_reservation_readback_mismatch_refuses_before_provider_dispatch(tmp_path, monkeypatch):
    context, native, _, _ = _fixture(tmp_path, monkeypatch, omit_reserved_readback=True)

    with pytest.raises(recovery.shared.OperationRefused, match="phase_reservation_readback_failed"):
        _run(context)

    assert native.write_count == 1
    assert native.model_calls == 0


def test_no_second_native_spend_write_after_phase_reservation(tmp_path, monkeypatch):
    context, native, _, _ = _fixture(tmp_path, monkeypatch, extra_spend=True)

    record = _run(context)

    assert record["outcome"] == "OPERATIONAL STOP"
    assert record["error_type"] == "OperationRefused"
    assert native.model_calls == 1
    assert native.extra_spend_refused
    assert native.write_count == 1
    assert len(record["spend_writes"]) == 1
    assert record["spend_writes"][0]["model_usd"] == pytest.approx(1.158263)


def test_daily_ledger_delta_rounds_float_query_values_to_microdollars():
    before = 23.935557000000003
    after = 38.935557

    assert recovery.shared._micros(before) + 15_000_000 == recovery.shared._micros(after) + 1
    assert recovery._ledger_micros(Decimal(str(after)) - Decimal(str(before))) == 15_000_000


def test_prefunded_resume_reuses_exact_reservation_once(tmp_path, monkeypatch):
    context, native, continuation_dir, _, _ = _prefunded_context(tmp_path, monkeypatch)

    record = _run(context)

    assert record["outcome"] == "PASS"
    assert record["reservation_reused"] is True
    assert record["native_spend_writes_this_execution"] == 0
    assert record["original_reservation_source"] == "ae3f91e0a2c70397a7e2cd9508bd218829d6fe13"
    assert record["aggregate_booked_usd"] == pytest.approx(25.0)
    assert record["phase_reserved_usd"] == pytest.approx(15.0)
    assert native.write_count == 0
    assert native.model_calls == 1
    assert (continuation_dir / recovery.CONTINUATION_EXECUTION_MARKER).read_bytes() == b"execution started\n"

    with pytest.raises(recovery.shared.OperationRefused, match="prefunded_resume_already_started"):
        _run(context)

    assert native.write_count == 0
    assert native.model_calls == 1


@pytest.mark.parametrize("failure", ["proof_file", "native_row", "native_claim"])
def test_prefunded_resume_proof_or_native_mismatch_blocks_dispatch(tmp_path, monkeypatch, failure):
    context, native, _, _, setup_log = _prefunded_context(tmp_path, monkeypatch)
    if failure == "proof_file":
        setup_log.write_text("changed after the pinned proof\n", encoding="utf-8")
    elif failure == "native_row":
        row = next(row for row in native.rows
                   if row["run_id"] == recovery.CONTINUATION_RESERVATION_RUN_ID)
        row["counts"]["model_usd"] = 14.999999
    else:
        native.claims[recovery.CONTINUATION_PREFIX] = [_claim("NOW-01")]

    with pytest.raises(recovery.shared.OperationRefused):
        _run(context)

    assert native.write_count == 0
    assert native.model_calls == 0
