import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.eval import ask_r2, demo_pairs, ranked_demo
from core.eval.demo_operator import OperatorRefused


GO_ID = "ALBERT-CHAT-GO-2026-10-01-TOP-RANKED-ONCE-USD25"
SAST = timezone(timedelta(hours=2))


def _profile(matrix_sha):
    question = SimpleNamespace(id="NOW-01", text="Ranked NOW-01 question", market="ZA",
                               source_ids=(), support_run_id=None)
    return SimpleNamespace(
        profile_id="ranked-now-once-v1", question_catalog=(question,),
        attempt_sequence=(("NOW-01", 1),), source_proof_sha256=matrix_sha,
        source_go_profile_id=GO_ID, authorization_slots=1,
        attempt_cap_micros=3_000_000, prior_guarded_micros=426_564,
        prior_verified_app_count=5,
    )


def _receipt(index, guard):
    run_id = ranked_demo.EXPECTED_PRIOR_RUN_IDS[index - 1]
    return {
        "question_id": "DEMO-01", "attempt_number": index, "attempt_key": f"old-{index}",
        "status": "COMPLETE", "model_usd": "0.100000", "model_usd_ceiling_micros": 100_000,
        "guarded_charge_micros": guard,
        "attempt_persistence": {
            "verified": True, "ask_id": f"a_old_{index}", "run_id": run_id,
            "record_sha256": "a" * 64, "guarded_charge_micros": guard,
            "recorded_model_usd": "0.100000", "reservation_release_usd": "0.100000",
            "recorded_model_usd_ceiling_micros": 100_000,
            "reservation_release_ceiling_micros": 100_000, "native_net_micros": 25_000_000,
            "readback": {"match": True, "run_id": run_id, "ask_id": f"a_old_{index}"},
        },
    }


class FakeRuntime:
    def __init__(self, *, old_receipts, now):
        self.old_receipts = old_receipts
        self.events = []
        self.authority = {"principal": ask_r2.BUILDER_EMAIL, "project": ask_r2.PROJECT}
        self.health_proof = {"http_status": 200, "ok": True, "version": "local-test"}
        self.modules = SimpleNamespace(staging=SimpleNamespace(SAST=SAST))
        self.wiring = SimpleNamespace(now=lambda: now)
        self.native = SimpleNamespace(
            _phase_readback=self._phase_readback,
            transfer_unspent=lambda **_kwargs: pytest.fail("ranked run attempted a new native allocation"),
        )

    def _verify_committed_sources(self):
        return {"clean": True, "head": "d" * 40, "hashes": {"ranked_demo.py": "e" * 64}}

    def _load(self):
        self.events.append("load")

    def _phase_readback(self, funding):
        self.events.append(("phase", tuple(funding.get("app_run_ids", []))))
        return {}, funding.get("consumed_micros", 0), []

    def refresh_funding(self, funding, attempts):
        self.events.append(("refresh", len(attempts)))
        ids = [row["attempt_persistence"]["run_id"] for row in attempts]
        consumed = sum(row["guarded_charge_micros"] for row in attempts)
        return {**funding, "verified": True, "app_run_ids": ids, "consumed_micros": consumed}

    def fresh_daily_readback(self, day):
        self.events.append(("daily", day.isoformat()))
        return {"verified": True, "all_pages_consumed": True, "run_date": day.isoformat(),
                "canonical_total_micros": 6_000_000, "daily_cap_micros": 40_000_000}

    def run_attempt(self, question, attempt, funding, context, **kwargs):
        self.events.append(("attempt", question.id, attempt, kwargs))
        run_id = "ranked-run-1"
        return {
            "question_id": question.id, "attempt_number": attempt, "status": "COMPLETE",
            "model_usd": "0.250000", "model_usd_ceiling_micros": 250_000,
            "guarded_charge_micros": 300_000,
            "attempt_persistence": {
                "verified": True, "ask_id": "a_ranked_1", "run_id": run_id,
                "record_sha256": "f" * 64, "guarded_charge_micros": 300_000,
                "recorded_model_usd": "0.250000", "reservation_release_usd": "0.250000",
                "recorded_model_usd_ceiling_micros": 250_000,
                "reservation_release_ceiling_micros": 250_000, "native_net_micros": 25_000_000,
                "readback": {"match": True, "run_id": run_id, "ask_id": "a_ranked_1"},
            },
        }


def _setup(tmp_path, monkeypatch, *, execute):
    matrix_bytes = b'{"rank":1,"question_id":"NOW-01","market":"ZA"}'
    matrix_sha = hashlib.sha256(matrix_bytes).hexdigest()
    authority = {
        "schema_version": "ranked-demo-authority-v1",
        "target": {"question_id": "NOW-01", "market": "ZA", "rank": 1, "attempt_number": 1},
        "max_question_attempts": 1, "automatic_retries": 0, "attempt_cap_micros": 3_000_000,
        "per_attempt_cap_usd": 3, "cumulative_model_cap_usd": 25,
        "native_transfer_allowed": False, "reservation_release_allowed": False,
        "replay_existing_demo_attempts": False, "ranking_matrix_filename": ranked_demo.RANKING_MATRIX_NAME,
        "ranking_matrix_sha256": matrix_sha,
    }
    authority_bytes = (json.dumps(authority, sort_keys=True, separators=(",", ":")) + "\n").encode()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / ranked_demo.RANKING_MATRIX_NAME).write_bytes(matrix_bytes)
    (inputs / ranked_demo.AUTHORITY_NAME).write_bytes(authority_bytes)
    _write_l1_qualification(inputs, monkeypatch)
    data = tmp_path / "old-run"
    data.mkdir()
    funding_dir = data / "funding"
    funding_dir.mkdir()
    funding = {
        "schema_version": "demo-funding-v1", "phase_id": "l3-demo-20261001", "verified": True,
        "run_date": "2026-10-01", "reservation_run_id": "reservation", "allocated_micros": 12_974_288,
        "consumed_micros": 0, "source_proof_sha": "a" * 64, "native_net_micros": 25_000_000,
        "baseline_run_ids": list(demo_pairs.BASELINE_RUN_IDS), "app_run_ids": [],
    }
    (funding_dir / "transfer.json").write_text(json.dumps(funding), encoding="utf-8")
    guards = [100_000, 100_000, 100_000, 100_000, 26_564]
    receipts = [_receipt(index, guard) for index, guard in enumerate(guards, 1)]
    fake_runtime = FakeRuntime(
        old_receipts=receipts, now=datetime(2026, 10, 1, 12, tzinfo=SAST))
    monkeypatch.setattr(ranked_demo, "INPUTS", inputs)
    monkeypatch.setattr(ranked_demo, "DATA", data)
    monkeypatch.setattr(ranked_demo, "RANKED_DATA", tmp_path / "ranked-once")
    monkeypatch.setattr(ranked_demo, "RANKING_MATRIX_SHA256", matrix_sha)
    monkeypatch.setattr(ranked_demo, "AUTHORITY_SHA256", hashlib.sha256(authority_bytes).hexdigest())
    monkeypatch.setattr(demo_pairs, "_existing_receipts", lambda path: receipts if Path(path) == data else [])
    monkeypatch.setattr(demo_pairs, "RANKED_NOW_ONCE_PROFILE", _profile(matrix_sha), raising=False)
    monkeypatch.setattr(ranked_demo, "production_runtime_factory",
                        lambda **_kwargs: fake_runtime)
    return fake_runtime


def _write_l1_qualification(inputs, monkeypatch, *, za_cards=1, source_execution_id="l1-execution-rerun-42"):
    board_line = "L1 posts brief rerun published cards for ZA"
    proof = {
        "schema_version": "l3-now-cards-qualification-v1", "source_lane": "L1",
        "basis": "brief_rerun_published_cards", "source_execution_id": source_execution_id,
        "cards_by_market": {"ZA": za_cards}, "l1_board_line": board_line,
        "board_line_sha256": hashlib.sha256(board_line.encode("utf-8")).hexdigest(),
        "qualified_at": "2026-10-01T16:00:00+02:00",
    }
    content = ranked_demo._canonical_bytes(proof)
    (inputs / ranked_demo.L1_CARDS_QUALIFICATION_NAME).write_bytes(content)
    monkeypatch.setattr(ranked_demo, "L1_CARDS_QUALIFICATION_SHA256", hashlib.sha256(content).hexdigest())
    return content


def _write_pre_dispatch_parent(monkeypatch, mutation=None):
    root = ranked_demo.RANKED_DATA
    root.mkdir()
    raw_dir = root / "raw"
    raw_dir.mkdir()
    matrix_sha = ranked_demo.RANKING_MATRIX_SHA256
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    funding = json.loads((ranked_demo.DATA / "funding" / "transfer.json").read_text(encoding="utf-8"))
    funding.update({"consumed_micros": 426_564, "app_run_ids": list(ranked_demo.EXPECTED_PRIOR_RUN_IDS)})
    call = {
        "phase": "structured", "status": "refused_before_dispatch",
        "stop_reason": "prior_call_failure", "charged_usd": 0.0, "reserved_usd": 0.0,
    }
    if mutation == "charged":
        call["charged_usd"] = 0.01
    elif mutation == "dispatched":
        call["status"] = "charged_known"
    elif mutation == "reason":
        call["stop_reason"] = "different_failure"
    run = {"run_id": ranked_demo.PRE_DISPATCH_RUN_ID, "model_usd": 0.0, "credits": 0.0,
           "tokens": {"input": 0, "output": 0}, "seconds": 2.0}
    intent = {"credit": False, "native_write": False, "phase_reservation_before_dispatch": True,
              "reported_usd": 0.0, "usd": 0.0, "what": "staging_check_ask:NOW-01"}
    raw = {
        "schema_version": "r3-demo-attempt-raw-v1", "id": "NOW-01", "question_id": "NOW-01",
        "attempt": 1, "attempt_number": 1, "attempt_key": ranked_demo.PRE_DISPATCH_ATTEMPT_KEY,
        "stage": "COMPLETE", "outcome": "OPERATIONAL STOP", "run_id": ranked_demo.PRE_DISPATCH_RUN_ID,
        "run": run, "app_record": {"status": "failed", "run": run, "error": {"error": "internal"}},
        "model_usd_micros": 0, "reported_run_model_usd_micros": 0,
        "guarded_charge_micros": 0, "charged_usd": 0.0, "unknown_cost": mutation == "unknown",
        "error_type": "BudgetRefused", "research_error_type": "DemoDispatchRefused",
        "stop_reason": "research_failed", "call_costs": [call], "source": {"prompt_catalog_sha256": matrix_sha},
        "booking_intents": intent, "spend_intents": intent, "spend_writes": [],
    }
    raw_bytes = ranked_demo._canonical_bytes(raw)
    raw_sha = ranked_demo._sha(raw_bytes)
    raw_path = raw_dir / f"{ranked_demo.PRE_DISPATCH_ATTEMPT_KEY}.json"
    raw_path.write_bytes(raw_bytes)
    dispatch = {
        **raw, "raw_receipt": raw, "raw_receipt_path": str(raw_path.resolve()),
        "raw_receipt_sha256": raw_sha,
    }
    marker = {
        "schema_version": "demo-pair-ranked-marker-v1",
        "attempt_key": ranked_demo.PRE_DISPATCH_ATTEMPT_KEY,
        "attempt_cap_micros": ranked_demo.ATTEMPT_CAP_MICROS,
        "prompt_source_sha256": matrix_sha, "execution_profile_id": profile.profile_id,
        "source_go_profile_id": ranked_demo.GO_PROFILE_ID, "source_proof_sha256": matrix_sha,
        "source_commit": ranked_demo.PRE_DISPATCH_SOURCE_COMMIT, "authorization_slots": 1,
    }
    receipt = {
        "schema_version": "demo-pair-attempt-v1", "question_id": "NOW-01", "attempt_number": 1,
        "attempt_key": ranked_demo.PRE_DISPATCH_ATTEMPT_KEY, "status": "failed_before_dispatch",
        "failure_type": "model_budget_not_used", "guarded_charge_micros": 0,
        "attempt_cap_micros": ranked_demo.ATTEMPT_CAP_MICROS, "model_usd": None,
        "model_usd_ceiling_micros": None, "readback": None, "attempt_persistence": None,
        "budget": {"cap_micros": ranked_demo.ATTEMPT_CAP_MICROS, "charged_micros": 0,
                   "reserved_micros": 0, "stop_reason": "research_failed", "calls": [call]},
        "funding_proof": funding, "source_commit": ranked_demo.PRE_DISPATCH_SOURCE_COMMIT,
        "source_proof_sha256": matrix_sha, "source_go_profile_id": ranked_demo.GO_PROFILE_ID,
        "execution_profile_id": profile.profile_id, "authorization_slots": 1,
        "dispatch_result": dispatch,
    }
    marker_bytes = ranked_demo._canonical_bytes(marker)
    receipt_bytes = ranked_demo._canonical_bytes(receipt)
    (root / f"{ranked_demo.PRE_DISPATCH_ATTEMPT_KEY}.attempted").write_bytes(marker_bytes)
    (root / f"{ranked_demo.PRE_DISPATCH_ATTEMPT_KEY}.json").write_bytes(receipt_bytes)
    monkeypatch.setattr(ranked_demo, "PRE_DISPATCH_MARKER_SHA256", ranked_demo._sha(marker_bytes))
    monkeypatch.setattr(ranked_demo, "PRE_DISPATCH_OUTER_SHA256", ranked_demo._sha(receipt_bytes))
    monkeypatch.setattr(ranked_demo, "PRE_DISPATCH_RAW_SHA256", raw_sha)
    snapshot = {
        f"{ranked_demo.PRE_DISPATCH_ATTEMPT_KEY}.attempted": marker_bytes,
        f"{ranked_demo.PRE_DISPATCH_ATTEMPT_KEY}.json": receipt_bytes,
        f"raw/{ranked_demo.PRE_DISPATCH_ATTEMPT_KEY}.json": raw_bytes,
    }
    return snapshot


def test_prepare_verifies_rank_authority_and_existing_funding_without_attempt(tmp_path, monkeypatch):
    runtime = _setup(tmp_path, monkeypatch, execute=False)

    result = ranked_demo.run_ranked_demo()

    assert result["status"] == "prepared"
    assert result["prior_guarded_micros"] == 426_564
    assert result["available_micros"] == 12_547_724
    assert not any(event[0] == "attempt" for event in runtime.events if isinstance(event, tuple))
    assert not ranked_demo.RANKED_DATA.exists()


def test_execute_runs_one_ranked_attempt_and_refreshes_existing_phase(tmp_path, monkeypatch):
    runtime = _setup(tmp_path, monkeypatch, execute=True)

    result = ranked_demo.run_ranked_demo(execute=True)

    attempts = [event for event in runtime.events if isinstance(event, tuple) and event[0] == "attempt"]
    assert result["status"] == "complete"
    assert len(attempts) == 1
    assert attempts[0][1:3] == ("NOW-01", 1)
    kwargs = attempts[0][3]
    assert kwargs["profile"].profile_id == "ranked-now-once-v1"
    assert kwargs["prior_data_dir"] == ranked_demo.DATA
    assert kwargs["attempt_data_dir"] == ranked_demo.RANKED_DATA
    assert kwargs["ranking_proof_bytes"] == (ranked_demo.INPUTS / ranked_demo.RANKING_MATRIX_NAME).read_bytes()
    assert kwargs["source_commit"] == "d" * 40
    assert result["final_guarded_micros"] == 726_564
    cost = json.loads((ranked_demo.RANKED_DATA / "NOW-01-attempt-1.cost.json").read_text(encoding="utf-8"))
    assert cost["provider_invoice_status"] == "unproven"


def test_prepare_refuses_wrong_matrix_bytes_before_runtime_load(tmp_path, monkeypatch):
    runtime = _setup(tmp_path, monkeypatch, execute=False)
    (ranked_demo.INPUTS / ranked_demo.RANKING_MATRIX_NAME).write_bytes(b"changed")

    with pytest.raises(OperatorRefused, match="ranked_proof_hash_mismatch"):
        ranked_demo.run_ranked_demo()

    assert runtime.events == []


def test_explicit_pre_dispatch_resume_is_single_append_only_child(tmp_path, monkeypatch):
    runtime = _setup(tmp_path, monkeypatch, execute=False)
    original = _write_pre_dispatch_parent(monkeypatch)

    result = ranked_demo.run_ranked_demo(resume_pre_dispatch=True)

    child = ranked_demo.RANKED_DATA / ranked_demo.PRE_DISPATCH_CHILD_NAME
    attempts = [event for event in runtime.events if isinstance(event, tuple) and event[0] == "attempt"]
    assert result["mode"] == "resume_pre_dispatch"
    assert result["status"] == "complete"
    assert len(attempts) == 1
    kwargs = attempts[0][3]
    assert kwargs["attempt_data_dir"] == child
    assert kwargs["prior_data_dir"] == ranked_demo.DATA
    assert kwargs["source_commit"] == "d" * 40
    assert kwargs["ranking_proof_bytes"] == (ranked_demo.INPUTS / ranked_demo.RANKING_MATRIX_NAME).read_bytes()
    assert json.loads((child / "pre-dispatch-resume.json").read_text(encoding="utf-8")) == result["resume_parent"]
    assert result["resume_parent"]["parent_run_id"] == ranked_demo.PRE_DISPATCH_RUN_ID
    assert result["resume_parent"]["prior_guarded_micros"] == 426_564
    assert {name: (ranked_demo.RANKED_DATA / name).read_bytes() for name in original} == original
    with pytest.raises(OperatorRefused, match="pre_dispatch_resume_already_used"):
        ranked_demo.run_ranked_demo(resume_pre_dispatch=True)
    assert len([event for event in runtime.events if isinstance(event, tuple) and event[0] == "attempt"]) == 1


@pytest.mark.parametrize("mutation", ["charged", "dispatched", "reason", "unknown"])
def test_pre_dispatch_resume_rejects_costed_or_unknown_parent(tmp_path, monkeypatch, mutation):
    runtime = _setup(tmp_path, monkeypatch, execute=False)
    _write_pre_dispatch_parent(monkeypatch, mutation)

    with pytest.raises(OperatorRefused):
        ranked_demo.run_ranked_demo(resume_pre_dispatch=True)

    assert runtime.events == []


def test_now_resume_stays_parked_without_l1_qualification_sha(tmp_path, monkeypatch):
    runtime = _setup(tmp_path, monkeypatch, execute=False)
    monkeypatch.setattr(ranked_demo, "L1_CARDS_QUALIFICATION_SHA256", None)

    with pytest.raises(OperatorRefused, match="ranked_l1_cards_gate_unconfigured"):
        ranked_demo.run_ranked_demo(resume_pre_dispatch=True)

    assert runtime.events == []
    assert not (ranked_demo.RANKED_DATA / ranked_demo.PRE_DISPATCH_CHILD_NAME).exists()


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [("missing", "ranked_l1_cards_proof_missing"),
     ("zero", "ranked_l1_cards_qualification_mismatch"),
     ("old", "ranked_l1_cards_qualification_mismatch")],
)
def test_now_resume_rejects_missing_or_unqualified_l1_cards(tmp_path, monkeypatch, mutation, reason):
    runtime = _setup(tmp_path, monkeypatch, execute=False)
    if mutation == "missing":
        (ranked_demo.INPUTS / ranked_demo.L1_CARDS_QUALIFICATION_NAME).unlink()
    elif mutation == "zero":
        _write_l1_qualification(ranked_demo.INPUTS, monkeypatch, za_cards=0)
    else:
        _write_l1_qualification(ranked_demo.INPUTS, monkeypatch, source_execution_id="l1-execution-f7j74")

    with pytest.raises(OperatorRefused, match=reason):
        ranked_demo.run_ranked_demo(resume_pre_dispatch=True)

    assert runtime.events == []
