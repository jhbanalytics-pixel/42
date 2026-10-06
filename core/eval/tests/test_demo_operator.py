import json
import copy
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.eval import ask_r2, demo_operator
from core.eval.demo_operator import (
    _AgentClient,
    _attempt_is_verified,
    OperatorRefused,
    production_runtime_factory,
    run_operator,
)
from core.eval.demo_pairs import DEMO_QUESTIONS


def _contexts(*, missing=None):
    contexts = {}
    for question in DEMO_QUESTIONS:
        ids = list(question.source_ids)
        posts = [{"id": source_id, "text": f"fresh {source_id}"} for source_id in ids]
        if question.id == missing:
            posts = []
        contexts[question.id] = {
            "market": question.market,
            "source_ids": ids,
            "support_run_id": question.support_run_id,
            "posts": posts,
        }
    return contexts


class FakeRuntime:
    def __init__(self, *, missing=None, attempts=10):
        self.events = []
        self.missing = missing
        self.attempts = attempts

    def prepare(self):
        self.events.append("prepare")
        return {"verified": True, "anchor_contexts": _contexts(missing=self.missing)}

    def transfer_funding(self, prepared):
        self.events.append("transfer")
        return {"verified": True, "allocated_micros": 12_000_000}

    def authorize_execute(self, prepared):
        self.events.append("authorize")
        return True

    def refresh_funding(self, funding, attempts):
        self.events.append(("refresh", len(attempts)))
        return {**funding, "consumed_micros": len(attempts) * 10_000}

    def run_attempt(self, question, attempt_number, funding, context):
        self.events.append(("attempt", question.id, attempt_number))
        if len([event for event in self.events if isinstance(event, tuple) and event[0] == "attempt"]) > self.attempts:
            raise AssertionError("operator exceeded the bounded attempt sequence")
        run_id = f"run-{question.id}-{attempt_number}"
        raw = {
            "question_id": question.id,
            "attempt_number": attempt_number,
            "run_id": run_id,
            "attempt": attempt_number,
            "raw_answer": {"status": "refused", "claims": [], "evidence": []},
            "call_costs": [{"status": "charged_known", "actual_usd": "0.010000"}],
        }
        return {
            "status": "complete",
            "raw_receipt": raw,
            "attempt_persistence": {
                "verified": True,
                "run_id": run_id,
                "ask_id": f"a_{question.id}_{attempt_number}",
                "recorded_model_usd": "0.010000",
                "reservation_release_usd": "0.010000",
                "recorded_model_usd_ceiling_micros": 10_000,
                "reservation_release_ceiling_micros": 10_000,
                "native_net_micros": 25_000_000,
                "readback": {"match": True, "run_id": run_id},
            },
            "model_usd": "0.010000",
            "model_usd_ceiling_micros": 10_000,
        }

    def read_raw_attempt(self, receipt):
        self.events.append(("raw", receipt["raw_receipt"]["question_id"], receipt["raw_receipt"]["attempt_number"]))
        return receipt["raw_receipt"]

    def select_pair(self, question_id, first, second):
        self.events.append(("select", question_id, first["attempt_number"], second["attempt_number"]))
        return {
            "question_id": question_id,
            "status": "selected",
            "selected_attempt": 1,
            "stability_status": "unproven",
            "full_pass": False,
            "admitted_claim_ids": {"1": [], "2": []},
            "run_ids": {"1": first["run_id"], "2": second["run_id"]},
            "raw_hashes": {"1": "a" * 64, "2": "b" * 64},
        }

    def l5_manifest(self, question_id, selection, saved_work):
        self.events.append(("l5", question_id))
        return {"accepted_ids": [], "sha256": "c" * 64, "status": "pending_l5"}

    def read_l5_acceptance(self, question_id, selection, saved_work):
        self.events.append(("read_l5", question_id))
        return None

    def create_saved_work(self, *args, **kwargs):
        question_id = args[0]
        self.events.append(("dossier", question_id))
        return {"dossier_id": f"d-{question_id}", "app_relative_path": f"#/dossiers/d-{question_id}"}


def test_prepare_is_default_and_does_not_transfer_or_start_attempts(tmp_path):
    runtime = FakeRuntime()

    result = run_operator(lambda **_: runtime, data_dir=tmp_path)

    assert result["mode"] == "prepare"
    assert result["status"] == "prepared"
    assert runtime.events == ["prepare"]
    assert list(tmp_path.iterdir()) == []


def test_missing_anchor_refuses_before_funding_transfer_or_attempt_marker(tmp_path):
    runtime = FakeRuntime(missing="DEMO-05")

    with pytest.raises(OperatorRefused, match="anchor_context_incomplete"):
        run_operator(lambda **_: runtime, execute=True, data_dir=tmp_path)

    assert runtime.events == ["prepare"]
    assert list(tmp_path.iterdir()) == []


def test_execute_saves_each_selected_dossier_before_pending_l5_handoff(tmp_path):
    runtime = FakeRuntime()

    result = run_operator(lambda **_: runtime, execute=True, data_dir=tmp_path)

    attempts = [event for event in runtime.events if isinstance(event, tuple) and event[0] == "attempt"]
    selections = [event for event in runtime.events if isinstance(event, tuple) and event[0] == "select"]
    dossiers = [event for event in runtime.events if isinstance(event, tuple) and event[0] == "dossier"]
    l5_events = [event for event in runtime.events if isinstance(event, tuple) and event[0] == "l5"]
    assert len(attempts) == 10
    assert [(row[1], row[2]) for row in attempts] == [
        (question.id, attempt)
        for question in DEMO_QUESTIONS
        for attempt in (1, 2)
    ]
    assert len(selections) == 5
    assert len(dossiers) == 5
    assert len(l5_events) == 5
    for question in DEMO_QUESTIONS:
        assert runtime.events.index(("dossier", question.id)) < runtime.events.index(("l5", question.id))
        assert runtime.events.index(("dossier", question.id)) < runtime.events.index(("read_l5", question.id))
    assert all(pair["l5_status"] == "pending_l5" for pair in result["pairs"])
    assert all(pair["app_relative_path"].startswith("#/dossiers/") for pair in result["pairs"])
    assert runtime.events.index("transfer") < runtime.events.index(attempts[0])
    handoffs = list(tmp_path.glob("*.handoff.json"))
    costs = list(tmp_path.glob("*.cost.json"))
    assert len(handoffs) == 5
    assert len(costs) == 10
    assert all(json.loads(path.read_text(encoding="utf-8"))["accepted_l5_ids"] == [] for path in handoffs)
    assert all(json.loads(path.read_text(encoding="utf-8"))["provider_invoice_status"] == "unproven"
               for path in costs)


def test_production_runtime_rejects_a_fresh_phase_directory(tmp_path):
    with pytest.raises(OperatorRefused, match="production_paths_must_be_fixed"):
        production_runtime_factory(data_dir=tmp_path / "new", inputs_dir=tmp_path / "inputs")


def test_agent_client_uses_existing_dossier_and_ask_routes_without_network():
    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, *, json, timeout):
            self.calls.append(("POST", url, json, timeout))
            return object()

        def get(self, url, *, timeout):
            self.calls.append(("GET", url, timeout))
            return object()

    session = FakeSession()
    client = _AgentClient(session, "https://f42-agent-fibxg5ynpq-uc.a.run.app/")

    client.post("/api/dossiers", json={"from": {"ask_id": "a_demo"}})
    client.get("/api/ask/a_demo")

    assert session.calls == [
        ("POST", "https://f42-agent-fibxg5ynpq-uc.a.run.app/api/dossiers",
         {"from": {"ask_id": "a_demo"}}, 20),
        ("GET", "https://f42-agent-fibxg5ynpq-uc.a.run.app/api/ask/a_demo", 20),
    ]


def test_fractional_microdollar_cost_is_verified_with_separate_ceiling():
    receipt = {
        "status": "complete",
        "model_usd": "2.0244805",
        "attempt_persistence": {
            "verified": True,
            "recorded_model_usd": "2.0244805",
            "reservation_release_usd": "2.0244805",
            "recorded_model_usd_ceiling_micros": 2_024_481,
            "reservation_release_ceiling_micros": 2_024_481,
            "native_net_micros": 25_000_000,
            "readback": {"match": True},
        },
    }

    assert _attempt_is_verified(receipt) is True


def _saved_dossier():
    return {
        "dossier_id": "d_saved",
        "dossier_content_hash": "c" * 64,
        "ask_id": "a_saved",
        "dossier_source_ask_id": "a_saved",
        "dossier_object_ids": ["dobj_1", "dobj_2"],
        "scope_admission_manifest_sha256": "f" * 64,
        "handoff_receipt": {
            "source_run_id": "run_saved",
            "source_answer_sha256": "a" * 64,
            "source_receipt_sha256": "b" * 64,
        },
    }


def _l5_acceptance(*, object_ids):
    saved = _saved_dossier()
    return {
        "schema_version": "demo-l5-acceptance-v1",
        "status": "accepted",
        "question_id": "DEMO-01",
        "dossier_id": saved["dossier_id"],
        "dossier_content_hash": saved["dossier_content_hash"],
        "source_ask_id": saved["ask_id"],
        "source_run_id": saved["handoff_receipt"]["source_run_id"],
        "source_answer_sha256": saved["handoff_receipt"]["source_answer_sha256"],
        "source_receipt_sha256": saved["handoff_receipt"]["source_receipt_sha256"],
        "scope_admission_manifest_sha256": saved["scope_admission_manifest_sha256"],
        "accepted_object_ids": object_ids,
    }


def test_l5_freezes_object_ids_from_the_saved_dossier(tmp_path):
    runtime = object.__new__(demo_operator._ProductionRuntime)
    runtime.data_dir = tmp_path
    path = tmp_path / "l5" / "DEMO-01.accepted.json"
    path.parent.mkdir()
    path.write_bytes(demo_operator._canonical_bytes(_l5_acceptance(object_ids=["dobj_1"])))

    result = runtime.read_l5_acceptance("DEMO-01", {}, _saved_dossier())

    assert result["verified"] is True
    assert result["accepted_ids"] == ["dobj_1"]
    frozen = json.loads((tmp_path / "l5" / "DEMO-01.accepted-frozen.json").read_text(encoding="utf-8"))
    assert frozen["accepted_object_ids"] == ["dobj_1"]
    assert frozen["dossier_id"] == "d_saved"
    assert frozen["source_ask_id"] == "a_saved"


def test_l5_rejects_object_ids_outside_the_saved_dossier(tmp_path):
    runtime = object.__new__(demo_operator._ProductionRuntime)
    runtime.data_dir = tmp_path
    path = tmp_path / "l5" / "DEMO-01.accepted.json"
    path.parent.mkdir()
    path.write_bytes(demo_operator._canonical_bytes(_l5_acceptance(object_ids=["claim_c1"])))

    with pytest.raises(OperatorRefused, match="l5_acceptance_scope_mismatch"):
        runtime.read_l5_acceptance("DEMO-01", {}, _saved_dossier())

    assert not (tmp_path / "l5" / "DEMO-01.accepted-frozen.json").exists()


def test_production_attempt_bridge_persists_direct_dispatch_and_normalizes_real_w6_for_w5(
    tmp_path, monkeypatch
):
    from core.eval import demo_dispatch, demo_pairs
    from core.eval.demo_native_accounting import run_row_sha256
    from core.eval.demo_operator import _ProductionRuntime
    from core.eval.tests.test_demo_saved_work import _case

    values = _case()
    question = next(row for row in DEMO_QUESTIONS if row.id == "DEMO-01")
    app_record = copy.deepcopy(values["app_record"])
    app_record["ask_id"] = "a_20261001_demo01_attempt1"
    app_record["run"]["run_id"] = "run_demo01_attempt1"
    app_record["run"]["model_usd"] = 0.01
    direct_result = {
        "schema_version": "r3-demo-attempt-raw-v1",
        "stage": "COMPLETE",
        "id": question.id,
        "attempt_key": "DEMO-01-attempt-1",
        "question": question.text,
        "markets": [question.market],
        "outcome": "COMPLETE",
        "stop_reason": None,
        "run_id": "run_demo01_attempt1",
        "raw_answer": copy.deepcopy(app_record["answer"]),
        "model_usd_micros": 10_000,
        "reported_run_model_usd_micros": 10_000,
        "reported_model_usd": "0.010000",
        "guarded_charge_micros": 10_000,
        "run": copy.deepcopy(app_record["run"]),
        "claim_rows": [],
        "claim_readback": {"match": True, "rows": []},
        "call_costs": [],
        "app_record": app_record,
    }

    def fake_dispatch(request, budget, *, funding_proof, dependencies):
        ticket = budget.reserve_fixed("structured", "gemini-test", 0.01, 100)
        budget.mark_dispatched(ticket)
        budget.settle(ticket, actual_usd=0.01)
        direct_result["call_costs"] = copy.deepcopy(budget.calls)
        return copy.deepcopy(direct_result)

    class FakeNative:
        def convert_prebooked_ask(self, *, real_ask_row, funding, guarded_charge):
            record = json.loads(real_ask_row["record"])
            apppriced = format(Decimal(str(record["run"]["model_usd"])), "f")
            retained = format(Decimal(guarded_charge) - Decimal(apppriced), "f")
            return {
                "status": "stored",
                "ask_row": real_ask_row,
                "ask_row_sha256": run_row_sha256(real_ask_row),
                "accounting": {
                    "apppriced_usd": apppriced,
                    "guarded_usd": guarded_charge,
                    "available_before_usd": "12.974288",
                    "released_usd": apppriced,
                    "retained_usd": retained,
                },
            }

    monkeypatch.setattr(demo_dispatch, "dispatch_attempt", fake_dispatch)
    monkeypatch.setattr(demo_pairs, "_now_sast", lambda: datetime(2026, 10, 1, 12, tzinfo=demo_pairs.SAST))
    runtime = object.__new__(_ProductionRuntime)
    runtime.data_dir = tmp_path
    runtime.native = FakeNative()
    runtime.modules = SimpleNamespace()
    runtime.wiring = SimpleNamespace()
    runtime.frozen_ask_ids = set()
    runtime.frozen_dossier_ids = set()
    funding = {
        "schema_version": "demo-funding-v1",
        "phase_id": "l3-demo-20261001",
        "verified": True,
        "run_date": "2026-10-01",
        "reservation_run_id": "reservation-demo",
        "allocated_micros": 12_974_288,
        "consumed_micros": 0,
        "source_proof_sha": "a" * 64,
        "native_net_micros": 25_000_000,
        "baseline_run_ids": list(demo_pairs.BASELINE_RUN_IDS),
        "app_run_ids": [],
    }
    context = {"market": question.market, "source_ids": list(question.source_ids),
               "support_run_id": question.support_run_id,
               "posts": [{"id": item, "text": "fresh source anchor"} for item in question.source_ids]}

    outer = runtime.run_attempt(question, 1, funding, context)

    expected_keys = {
        "verified", "ask_id", "run_id", "record_sha256", "guarded_charge_micros",
        "recorded_model_usd", "recorded_model_usd_ceiling_micros", "reservation_release_usd",
        "reservation_release_ceiling_micros", "native_net_micros", "readback",
    }
    persistence = outer["attempt_persistence"]
    assert outer["status"] == "COMPLETE", {
        "failure_type": outer.get("failure_type"),
        "w6_files": [path.name for path in (tmp_path / "w6").glob("*")] if (tmp_path / "w6").exists() else [],
    }
    assert _attempt_is_verified(outer) is True
    assert _attempt_is_verified(outer) is True
    assert set(persistence) == expected_keys
    assert "w6" not in persistence
    assert len(persistence["record_sha256"]) == 64
    assert persistence["readback"]["match"] is True
    w6 = json.loads((tmp_path / "w6" / "DEMO-01-attempt-1.json").read_text(encoding="utf-8"))
    assert w6["record_sha256"].startswith("sha256:")
    raw = json.loads((tmp_path / "raw" / "DEMO-01-attempt-1.json").read_text(encoding="utf-8"))
    assert raw["question_id"] == question.id and raw["attempt_number"] == 1


def test_ranked_profile_bridge_persists_raw_before_identity_refusal(tmp_path, monkeypatch):
    from core.eval import demo_dispatch, demo_pairs
    from core.eval.demo_operator import _ProductionRuntime

    question = demo_pairs.DemoQuestion("NOW-01", "Ranked question", "ZA", (), "")
    profile = SimpleNamespace(profile_id="ranked-now-once-v1", question_catalog=(question,))
    prior_dir = tmp_path / "prior"
    prior_dir.mkdir()
    artifact_dir = tmp_path / "ranked"
    captured = {}

    def fake_pairs_run_attempt(**kwargs):
        captured.update(kwargs)
        request = SimpleNamespace(question_id="NOW-01", attempt_number=1,
                                  attempt_key="NOW-01-ranked-once-attempt-1")
        kwargs["dispatch"](request, object())

    def fake_dispatch(request, budget, *, funding_proof, dependencies, profile):
        assert profile is captured["profile"]
        return {"id": "NOW-02", "run_id": "ranked-run-1", "outcome": "COMPLETE"}

    monkeypatch.setattr(demo_pairs, "run_attempt", fake_pairs_run_attempt)
    monkeypatch.setattr(demo_dispatch, "dispatch_attempt", fake_dispatch)
    runtime = object.__new__(_ProductionRuntime)
    runtime.data_dir = prior_dir
    runtime.modules = SimpleNamespace()
    runtime.wiring = SimpleNamespace()
    runtime.frozen_ask_ids = set()
    runtime.frozen_dossier_ids = set()

    with pytest.raises(OperatorRefused, match="dispatcher_raw_identity_mismatch"):
        runtime.run_attempt(
            question, 1, {}, {"market": "ZA", "source_ids": [], "posts": []},
            profile=profile, attempt_data_dir=artifact_dir, prior_data_dir=prior_dir,
            ranking_proof_bytes=b"immutable ranking proof", source_commit="b" * 40,
        )

    raw_path = artifact_dir / "raw" / "NOW-01-ranked-once-attempt-1.json"
    raw_bytes = raw_path.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    assert raw["id"] == "NOW-02"
    assert raw["question_id"] == "NOW-01"
    assert captured["profile"] is profile
    assert captured["prior_data_dir"] == prior_dir
    assert captured["ranking_proof_bytes"] == b"immutable ranking proof"
    assert captured["source_commit"] == "b" * 40
    assert not (prior_dir / "raw").exists()


def test_operator_transfer_uses_native_canonical_source_proof_and_returns_native_funding(
    tmp_path, monkeypatch
):
    from core.eval import demo_native_accounting, demo_pairs
    from core.eval.tests.test_demo_native_accounting import FakeWarehouse, BASELINE, DAY, _adapter

    monkeypatch.setattr(demo_native_accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    native = _adapter(warehouse)
    native_rows = native._fetch_rows(BASELINE)
    canonical_rows, source_sha = demo_native_accounting._approved_source_rows(native_rows)
    assert source_sha != demo_pairs.source_proof_hash(native_rows)
    assert source_sha == demo_pairs.source_proof_hash(canonical_rows)
    plan = demo_pairs.prepare_funding_transfer(
        run_date=DAY,
        daily_headroom_micros=40_000_000 - 4_793_311,
        source_proof_sha=source_sha,
        source_rows=canonical_rows,
        unknown_rows=canonical_rows,
    )
    runtime = object.__new__(demo_operator._ProductionRuntime)
    runtime.data_dir = tmp_path
    runtime.native = native
    prepared = {"funding_plan": plan, "run_date": DAY.isoformat()}

    funding = runtime.transfer_funding(prepared)

    assert funding["verified"] is True
    assert funding["source_proof_sha"] == source_sha
    assert funding["allocated_micros"] == plan["amount_micros"]
    assert funding["baseline_run_ids"] == list(BASELINE)
    assert funding["app_run_ids"] == []
    assert len(warehouse.execute_calls) == 1
    assert (tmp_path / "funding" / "transfer.json").is_file()


def test_two_verified_existing_attempts_accumulate_guard_before_next_slot(tmp_path, monkeypatch):
    from core.eval import demo_dispatch, demo_pairs
    from core.eval.demo_operator import _ProductionRuntime

    first_question, next_question = DEMO_QUESTIONS[:2]
    first_path = tmp_path / "DEMO-01-attempt-1.cost.json"
    second_path = tmp_path / "DEMO-01-attempt-2.cost.json"
    first_path.write_bytes(b"frozen first cost receipt")
    second_path.write_bytes(b"frozen second cost receipt")

    def verified(question, attempt, run_id, ask_id, model_usd, guard, ceiling):
        key = demo_pairs._attempt_key(question.id, attempt)
        (tmp_path / f"{key}.attempted").write_bytes(b"original marker")
        (tmp_path / f"{key}.json").write_bytes(b"original outer receipt")
        return {
            "question_id": question.id, "attempt_number": attempt, "attempt_key": key,
            "status": "COMPLETE", "model_usd": model_usd, "guarded_charge_micros": guard,
            "attempt_persistence": {
                "verified": True, "ask_id": ask_id, "run_id": run_id,
                "record_sha256": "a" * 64, "guarded_charge_micros": guard,
                "recorded_model_usd": model_usd, "reservation_release_usd": model_usd,
                "recorded_model_usd_ceiling_micros": ceiling,
                "reservation_release_ceiling_micros": ceiling, "native_net_micros": 25_000_000,
                "readback": {"match": True, "run_id": run_id, "ask_id": ask_id},
            },
        }

    first = verified(first_question, 1, "run_demo01_attempt1", "a_demo01_attempt1",
                     "0.126353475", 126_665, 126_354)
    second = verified(first_question, 2, "run_demo01_attempt2", "a_demo01_attempt2",
                      "0.12207314999999999", 122_076, 122_074)
    monkeypatch.setattr(demo_pairs, "_existing_receipts", lambda _path: [first, second])
    dispatches = []

    def fake_dispatch(request, budget, *, funding_proof, dependencies):
        dispatches.append(request.attempt_key)
        return {"id": request.question_id, "run_id": "run_demo02_attempt1", "outcome": "COMPLETE"}

    def invoke_production_dispatch(*, question_id, attempt_number, dispatch, **_kwargs):
        request = SimpleNamespace(question_id=question_id, attempt_number=attempt_number,
                                  attempt_key=demo_pairs._attempt_key(question_id, attempt_number))
        result = dispatch(request, object())
        return {"question_id": question_id, "attempt_number": attempt_number, "dispatch_result": result}

    monkeypatch.setattr(demo_pairs, "run_attempt", invoke_production_dispatch)
    monkeypatch.setattr(demo_dispatch, "dispatch_attempt", fake_dispatch)
    runtime = object.__new__(_ProductionRuntime)
    runtime.data_dir = tmp_path
    runtime.modules = SimpleNamespace()
    runtime.wiring = SimpleNamespace()

    class FakeNative:
        def __init__(self):
            self.phase_states = []

        def _phase_readback(self, state):
            self.phase_states.append(state)
            consumed = 126_665 if len(state["app_run_ids"]) == 1 else 248_741
            return {}, consumed, list(state["app_run_ids"])

    runtime.native = FakeNative()

    first_result = runtime.run_attempt(
        first_question, 1, {"app_run_ids": [], "consumed_micros": 0}, {})
    second_result = runtime.run_attempt(
        first_question, 2, {"app_run_ids": ["run_demo01_attempt1"], "consumed_micros": 126_665}, {})
    next_result = runtime.run_attempt(
        next_question, 1, {"app_run_ids": ["run_demo01_attempt1", "run_demo01_attempt2"],
                           "consumed_micros": 248_741}, {})

    assert first_result["_recovered_existing"] is True
    assert second_result["_recovered_existing"] is True
    assert runtime.native.phase_states[0]["app_run_ids"] == ["run_demo01_attempt1"]
    assert runtime.native.phase_states[0]["consumed_micros"] == 126_665
    assert runtime.native.phase_states[1]["app_run_ids"] == ["run_demo01_attempt1", "run_demo01_attempt2"]
    assert runtime.native.phase_states[1]["consumed_micros"] == 248_741
    assert next_result["dispatch_result"]["raw_receipt"]["attempt_key"] == "DEMO-02-attempt-1"
    assert dispatches == ["DEMO-02-attempt-1"]
    assert first_path.read_bytes() == b"frozen first cost receipt"
    assert second_path.read_bytes() == b"frozen second cost receipt"


def test_recover_existing_only_proves_fixed_attempt_without_native_writes(tmp_path, monkeypatch):
    from core.eval import demo_dispatch, demo_native_accounting, demo_pairs, demo_saved_work
    from core.eval.demo_native_accounting import _approved_source_rows
    from core.eval.tests.test_demo_native_accounting import BASELINE, DAY, SOURCE_DAY, FakeWarehouse, _adapter
    from core.eval.tests.test_demo_saved_work import _case

    monkeypatch.setattr(demo_native_accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    native = _adapter(warehouse)
    source_rows, source_sha = _approved_source_rows(native._fetch_rows(BASELINE))
    funding_plan = demo_pairs.prepare_funding_transfer(
        run_date=DAY,
        daily_headroom_micros=35_206_689,
        source_proof_sha=source_sha,
        source_rows=source_rows,
        unknown_rows=source_rows,
    )
    transfer = native.transfer_unspent(
        amount_usd=Decimal(funding_plan["amount_micros"]) / 1_000_000,
        source_date=SOURCE_DAY,
        run_date=DAY,
        baseline_run_ids=BASELINE,
        source_proof_sha=source_sha,
        native_net_micros=25_000_000,
    )
    funding = transfer["funding"]
    question = DEMO_QUESTIONS[0]
    attempt_key = "DEMO-01-attempt-1"
    run_id = "run_demo01_attempt1"
    ask_id = "a_20261001_demo01_attempt1"
    values = _case()
    app_record = copy.deepcopy(values["app_record"])
    app_record["ask_id"] = ask_id
    app_record["question"] = question.text
    app_record["created_at"] = "2026-10-01T12:00:00+02:00"
    app_record["finished_at"] = "2026-10-01T12:00:10+02:00"
    app_record["answer"] = {
        "status": "complete", "as_of": "2026-10-01T12:00:00+02:00", "short_answer": "",
        "claims": [], "evidence": [], "so_what": [], "watch_next": [], "gaps": [],
    }
    app_record["run"]["run_id"] = run_id
    app_record["run"]["model_usd"] = "0.126353475"
    raw = {
        "schema_version": "r3-demo-attempt-raw-v1", "question_id": question.id,
        "attempt": 1, "attempt_number": 1, "attempt_key": attempt_key,
        "outcome": "COMPLETE",
        "run_id": run_id, "raw_answer": copy.deepcopy(app_record["answer"]),
        "app_record": app_record, "unknown_cost": False,
        "call_costs": [
            {"status": "charged_known", "actual_usd": "0.126353475"},
            {"status": "charged_conservative_ceiling", "actual_usd": "0.000308"},
            {"status": "charged_known", "actual_usd": "0.000003"},
        ],
        "context": {"window_start": "2026-09-24", "window_end": "2026-09-30", "evidence": []},
    }
    raw_bytes = demo_operator._canonical_bytes(raw)
    raw_sha = demo_operator._sha(raw_bytes)
    seeded = demo_saved_work.persist_attempt_view(
        selected_receipt=raw,
        app_record=app_record,
        safe_payload=None,
        admitted_claim_ids=[],
        raw_receipt_bytes=raw_bytes,
        raw_receipt_sha256="sha256:" + raw_sha,
        funding=funding,
        guarded_charge_usd="0.126665",
        native_adapter=native,
    )
    assert seeded["status"] == "stored"

    raw_path = tmp_path / "raw" / f"{attempt_key}.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(raw_bytes)
    funding_path = tmp_path / "funding" / "transfer.json"
    funding_path.parent.mkdir(parents=True)
    funding_bytes = demo_operator._canonical_bytes(funding)
    funding_path.write_bytes(funding_bytes)
    marker_path = tmp_path / f"{attempt_key}.attempted"
    marker_bytes = demo_operator._canonical_bytes({
        "attempt_key": attempt_key, "prompt_source_sha256": demo_pairs.DEMO_QUESTIONS_SHA256,
    })
    marker_path.write_bytes(marker_bytes)
    outer = {
        "question_id": question.id, "attempt_number": 1, "attempt_key": attempt_key,
        "status": "ambiguous", "failure_type": "UncertainWriteError",
        "prompt_source_sha256": demo_pairs.DEMO_QUESTIONS_SHA256,
        "prompt": question.text, "market": question.market,
        "source_ids": list(question.source_ids), "support_run_id": question.support_run_id,
        "provided_context": {"market": question.market, "source_ids": list(question.source_ids),
                             "support_run_id": question.support_run_id, "posts": []},
        "funding_proof": demo_pairs._validate_funding_proof(funding),
        "guarded_charge_micros": 1_297_428, "attempt_cap_micros": 1_297_428,
        "budget": {"cap_micros": 1_297_428, "charged_micros": 126_665,
                   "reserved_micros": 0, "stop_reason": None,
                   "calls": raw["call_costs"]},
        "dispatch_result": {"status": "COMPLETE", "outcome": "COMPLETE",
                             "raw_receipt": raw, "raw_receipt_path": str(raw_path),
                             "raw_receipt_sha256": raw_sha},
    }
    receipt_path = tmp_path / f"{attempt_key}.json"
    outer_bytes = demo_operator._canonical_bytes(outer)
    receipt_path.write_bytes(outer_bytes)
    native_changes = []

    def deny_native_mutation(*args, **kwargs):
        native_changes.append((args, kwargs))
        raise AssertionError("recovery attempted a native write or claim")

    native._execute = deny_native_mutation
    native._claim_once = deny_native_mutation
    monkeypatch.setattr(demo_dispatch, "dispatch_attempt",
                        lambda *_args, **_kwargs: pytest.fail("recovery redispatched a paid attempt"))
    runtime = object.__new__(demo_operator._ProductionRuntime)
    runtime.data_dir = tmp_path
    runtime.native = native
    runtime.modules = SimpleNamespace(
        ask=SimpleNamespace(model_daily_usd=lambda *, now: 40.0),
        staging=SimpleNamespace(SAST=timezone(timedelta(hours=2))),
    )
    result = runtime.recover_existing_only({
        "run_date": DAY.isoformat(), "funding_plan": funding_plan,
    })

    assert result["status"] == "recovered"
    assert result["guarded_charge_micros"] == 126_665
    assert native_changes == []
    assert marker_path.read_bytes() == marker_bytes
    assert receipt_path.read_bytes() == outer_bytes
    assert raw_path.read_bytes() == raw_bytes
    assert funding_path.read_bytes() == funding_bytes
    assert len(warehouse.execute_calls) == 2
    saved_sidecar = json.loads((tmp_path / "w6" / f"{attempt_key}.json").read_text(encoding="utf-8"))
    saved = saved_sidecar["checks"]["persist_existing"]
    assert saved["status"] == "resumed"
    assert saved["ask_row_sha256"] == demo_native_accounting.run_row_sha256(
        next(row for row in warehouse.rows if row.get("run_id") == run_id)
    )
    recovered = next(row for row in demo_pairs._existing_receipts(tmp_path) if row["attempt_key"] == attempt_key)
    assert _attempt_is_verified(recovered) is True
    assert recovered["guarded_charge_micros"] == 126_665
    assert recovered["attempt_persistence"]["record_sha256"] == saved["record_sha256"].removeprefix("sha256:")


def test_source_gate_allows_untracked_private_plans_but_refuses_dirty_tracked_code(tmp_path, monkeypatch):
    git = ask_r2._git_executable()
    subprocess.run([git, "init"], cwd=tmp_path, check=True, capture_output=True)
    source = tmp_path / "core/eval/demo_operator.py"
    source.parent.mkdir(parents=True)
    source.write_text("tracked source\n", encoding="utf-8")
    subprocess.run([git, "add", "core/eval/demo_operator.py"], cwd=tmp_path, check=True)
    subprocess.run([git, "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "source"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans/private.md").write_text("private note\n", encoding="utf-8")
    monkeypatch.setattr(demo_operator, "LIVE_SOURCE_FILES", ("core/eval/demo_operator.py",))
    monkeypatch.setattr(ask_r2, "REQUIRED_ANCESTORS", ())
    runtime = object.__new__(demo_operator._ProductionRuntime)
    runtime.repo_root = tmp_path
    runtime.inputs_dir = tmp_path

    assert runtime._verify_committed_sources()["clean"] is True
    source.write_text("changed source\n", encoding="utf-8")
    assert runtime._verify_committed_sources()["clean"] is False


def test_new_execution_proof_does_not_depend_on_the_old_r2_gate(tmp_path, monkeypatch):
    sast = timezone(timedelta(hours=2))
    proof = {"clean": True, "head": "a" * 40, "hashes": {"core/eval/demo_operator.py": "b" * 64},
             "ancestors": {}}

    class HealthResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class Session:
        @staticmethod
        def get(url, timeout):
            return HealthResponse()

    runtime = object.__new__(demo_operator._ProductionRuntime)
    runtime.repo_root = tmp_path
    runtime.inputs_dir = tmp_path
    runtime._verify_committed_sources = lambda: proof
    runtime.agent_session = Session()
    runtime.modules = SimpleNamespace(
        staging=SimpleNamespace(SAST=sast),
        ask=SimpleNamespace(model_daily_usd=lambda *, now: 40.0),
    )
    runtime.wiring = SimpleNamespace(now=lambda: datetime(2026, 10, 1, 10, tzinfo=sast))
    runtime.fresh_daily_readback = lambda day: {
        "verified": True,
        "all_pages_consumed": True,
        "run_date": day.isoformat(),
        "daily_cap_micros": 40_000_000,
        "canonical_total_micros": 5_000_000,
    }
    monkeypatch.setattr(demo_operator, "_verify_approval", lambda *_: {
        "questions_sha256": "c" * 64,
        "source_receipt_sha256": "d" * 64,
        "approval_sha256": "f" * 64,
        "archived_report_sha256": "e" * 64,
    })
    monkeypatch.setattr(ask_r2, "_verify_builder_adc", lambda: {
        "principal": ask_r2.BUILDER_EMAIL, "project": ask_r2.PROJECT,
    })
    monkeypatch.setattr(ask_r2, "REQUIRED_ANCESTORS", ())
    monkeypatch.setattr(ask_r2, "validate_execution_authority",
                        lambda **_: (_ for _ in ()).throw(AssertionError("old R2 gate called")))
    prepared = {
        "committed_sources": True,
        "source_commit": proof["head"],
        "source_hashes": proof["hashes"],
        "source_ancestor_proof": proof["ancestors"],
        "questions_sha256": "c" * 64,
        "source_receipt_sha256": "d" * 64,
        "approval_sha256": "f" * 64,
        "archived_report_sha256": "e" * 64,
        "run_date": "2026-10-01",
        "funding_plan": {"amount_micros": 12_000_000},
    }

    assert runtime.authorize_execute(prepared) is True


def test_unknown_attempt_cost_stops_before_the_next_slot(tmp_path):
    class UnknownRuntime(FakeRuntime):
        def run_attempt(self, question, attempt_number, funding, context):
            receipt = super().run_attempt(question, attempt_number, funding, context)
            receipt["status"] = "unknown_cost"
            receipt["attempt_persistence"]["verified"] = False
            return receipt

    runtime = UnknownRuntime()

    result = run_operator(lambda **_: runtime, execute=True, data_dir=tmp_path)

    attempts = [event for event in runtime.events if isinstance(event, tuple) and event[0] == "attempt"]
    assert len(attempts) == 1
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "attempt_accounting_unverified"
