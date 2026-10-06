from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from core.eval import demo_pairs, trending_replay


SAST = timezone(timedelta(hours=2))


def test_trending_profile_freezes_three_exact_market_slots():
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE

    assert [(q.text, q.market) for q in profile.question_catalog] == [
        ("what is trending in south africa", "ZA"),
        ("what is trending in nigeria", "NG"),
        ("what is trending in kenya", "KE"),
    ]
    assert profile.attempt_sequence == tuple((q.id, 1) for q in profile.question_catalog)
    assert profile.authorization_slots == 3
    assert profile.attempt_cap_micros == 3_000_000
    assert demo_pairs._execution_profile(profile) is profile
    assert demo_pairs.RANKED_NOW_ONCE_PROFILE.authorization_slots == 1


def test_trending_attempts_append_in_market_order_with_one_budget_each(tmp_path, monkeypatch):
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    prior_dir = tmp_path / "prior"
    artifact_dir = tmp_path / "trending"
    prior_ids = [f"prior-{index}" for index in range(5)]
    prior = [
        {"question_id": f"DEMO-{index:02}", "attempt_number": 1,
         "guarded_charge_micros": amount, "status": "complete",
         "attempt_persistence": {"verified": True, "run_id": run_id}}
        for index, (amount, run_id) in enumerate(
            zip((100_000, 100_000, 100_000, 100_000, 26_564), prior_ids), 1)
    ]
    read_receipts = demo_pairs._existing_receipts
    monkeypatch.setattr(
        demo_pairs, "_existing_receipts",
        lambda path, profile=None: prior if Path(path) == prior_dir else read_receipts(path, profile=profile),
    )
    calls = []

    def dispatch(request, budget):
        calls.append(request)
        ticket = budget.reserve_fixed("structured", "offline", 1.0, 100)
        budget.mark_dispatched(ticket)
        budget.settle(ticket, actual_usd=0.5)
        rows = [{"claim_id": f"claim-{request.market}"}]
        return {"status": "complete", "answer": "offline",
                "raw_receipt": {"run_id": f"raw-{request.market}", "claim_rows": rows},
                "claim_readback": {"match": True, "rows": rows}}

    def persist(request, result):
        record_hash = "c" * 64
        run_id = f"run-{request.market}"
        return {
            "verified": True, "ask_id": f"ask-{request.market}", "run_id": run_id,
            "record_sha256": record_hash, "guarded_charge_micros": result["guarded_charge_micros"],
            "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
            "recorded_model_usd_ceiling_micros": 500_000,
            "reservation_release_ceiling_micros": 500_000, "native_net_micros": 25_000_000,
            "readback": {"match": True, "run_id": run_id, "record_sha256": record_hash,
                         "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
                         "recorded_model_usd_ceiling_micros": 500_000,
                         "reservation_release_ceiling_micros": 500_000,
                         "native_net_micros": 25_000_000},
        }

    app_ids = list(prior_ids)
    consumed = 426_564
    for question in profile.question_catalog:
        proof = {
            "schema_version": demo_pairs.DEMO_FUNDING_SCHEMA,
            "phase_id": demo_pairs.DEMO_PHASE_ID, "verified": True,
            "run_date": "2026-10-01", "reservation_run_id": "existing-reservation",
            "allocated_micros": 12_974_288, "consumed_micros": consumed,
            "source_proof_sha": "a" * 64, "native_net_micros": 25_000_000,
            "baseline_run_ids": list(demo_pairs.BASELINE_RUN_IDS), "app_run_ids": app_ids,
        }
        receipt = demo_pairs.run_attempt(
            question_id=question.id, attempt_number=1, data_dir=artifact_dir,
            prior_data_dir=prior_dir, funding_proof=proof,
            provided_context={"market": question.market, "source_ids": [], "posts": []},
            dispatch=dispatch, persist_attempt=persist, profile=profile,
            ranking_proof_bytes=demo_pairs.TRENDING_FALLBACK_PROFILE_PROOF_BYTES,
            source_commit="d" * 40,
            clock=lambda: datetime(2026, 10, 1, 12, tzinfo=SAST),
        )
        app_ids.append(receipt["attempt_persistence"]["run_id"])
        consumed += receipt["guarded_charge_micros"]

    assert [request.prompt for request in calls] == [q.text for q in profile.question_catalog]
    assert [request.market for request in calls] == ["ZA", "NG", "KE"]
    assert all(request.attempt_number == 1 for request in calls)
    assert all((artifact_dir / f"{q.id}-attempt-1.attempted").is_file()
               for q in profile.question_catalog)


def _funding_inputs(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    funding_dir = data / "funding"
    funding_dir.mkdir(parents=True, exist_ok=True)
    (funding_dir / "transfer.json").write_text(json.dumps({
        "verified": True,
        "run_date": "2026-10-01",
        "allocated_micros": 12_974_288,
        "consumed_micros": 0,
        "app_run_ids": [],
        "native_net_micros": 25_000_000,
        "baseline_run_ids": list(demo_pairs.BASELINE_RUN_IDS),
    }), encoding="utf-8")
    return inputs, data, tmp_path / "trending"


def _verified_prior_receipts():
    result = []
    for index, (guard, run_id) in enumerate(zip(
        (100_000, 100_000, 100_000, 100_000, 26_564),
        (f"prior-{number}" for number in range(5)),
    ), 1):
        model_usd = "0.01"
        ceiling = 10_000
        persistence = {
            "verified": True,
            "ask_id": f"prior-ask-{index}",
            "run_id": run_id,
            "record_sha256": f"{index:064x}",
            "guarded_charge_micros": guard,
            "recorded_model_usd": model_usd,
            "reservation_release_usd": model_usd,
            "recorded_model_usd_ceiling_micros": ceiling,
            "reservation_release_ceiling_micros": ceiling,
            "native_net_micros": 25_000_000,
            "readback": {"match": True},
        }
        result.append({"status": "complete", "model_usd": model_usd,
                       "guarded_charge_micros": guard, "attempt_persistence": persistence})
    return result


class _Runtime:
    def __init__(self, now, artifact_dir=None, saved_statuses=None, answer_statuses=None, versions=None,
                 source_commit=None, guarded_charges=None):
        self.events = []
        self.health_proof = {"http_status": 200, "ok": True, "version": "b" * 12}
        self.wiring = SimpleNamespace(now=lambda: now)
        self.native = SimpleNamespace(_phase_readback=lambda _funding: ({}, 0, []))
        self.repo_root = trending_replay.ROOT
        self.source_commit = source_commit or trending_replay.PRODUCT_FIX_COMMIT
        self.artifact_dir = Path(artifact_dir) if artifact_dir is not None else None
        self.saved_statuses = saved_statuses or ["partial", "insufficient_evidence", "complete"]
        self.answer_statuses = answer_statuses or ["complete", "complete", "partial"]
        self.versions = versions or ["agent-version-12", None, None]
        self.guarded_charges = guarded_charges or [500_000, 500_000, 500_000]
        self.run_calls = []
        self.run_count = 0
        self.funding_reads = []
        self.provider_values = []
        self.fail_run = False

    def _verify_committed_sources(self):
        self.events.append("sources")
        return {"clean": True, "head": self.source_commit,
                "hashes": {}, "ancestors": {}}

    def _load(self):
        self.events.append("load")

    def fresh_daily_readback(self, day):
        self.events.append(("daily", day.isoformat()))
        return {"verified": True, "all_pages_consumed": True, "run_date": day.isoformat(),
                "daily_cap_micros": 30_000_000, "canonical_total_micros": 0}

    def refresh_funding(self, funding, attempts):
        run_ids = [item["attempt_persistence"]["run_id"] for item in attempts]
        consumed = sum(item["guarded_charge_micros"] for item in attempts)
        self.funding_reads.append((list(run_ids), consumed))
        return {**funding, "verified": True, "allocated_micros": 12_974_288,
                "consumed_micros": consumed, "app_run_ids": run_ids,
                "native_net_micros": 25_000_000}

    def run_attempt(self, question, attempt_number, funding, context, *, profile, attempt_data_dir,
                    prior_data_dir, ranking_proof_bytes, source_commit):
        self.provider_values.append(os.environ.get("MODEL_PROVIDER"))
        if self.fail_run:
            raise RuntimeError("injected attempt failure")
        assert attempt_number == 1
        assert profile is demo_pairs.TRENDING_FALLBACK_PROFILE
        assert ranking_proof_bytes == demo_pairs.TRENDING_FALLBACK_PROFILE_PROOF_BYTES
        self.artifact_dir = Path(attempt_data_dir)
        index = self.run_count
        self.run_count += 1
        guarded_charge = self.guarded_charges[index]
        self.run_calls.append({"question_id": question.id, "market": question.market,
                               "app_run_ids": list(funding["app_run_ids"]),
                               "consumed_micros": funding["consumed_micros"]})
        attempt_key = demo_pairs._attempt_key(question.id, attempt_number)
        ask_id = f"ask-{question.market}"
        run_id = f"run-{question.market}"
        claim_ids = [f"claim-{question.market}-1", f"claim-{question.market}-2"]
        if self.saved_statuses[index] == "insufficient_evidence":
            claim_ids = claim_ids[:1]
        evidence_ids = [f"evidence-{question.market}-1", f"evidence-{question.market}-2"]
        raw = {
            "outcome": "COMPLETE",
            "raw_answer": {
                "claims": [{"id": claim_id, "evidence_ids": [evidence_ids[claim_index]]}
                           for claim_index, claim_id in enumerate(claim_ids)],
                "evidence": [{"id": evidence_id} for evidence_id in evidence_ids],
            },
            "app_record": {
                "answer": {"status": self.answer_statuses[index]},
                "version": self.versions[index],
                "run": {"run_id": run_id, "model_usd": "0.5"},
            },
        }
        raw_path = self.artifact_dir / "raw" / f"{attempt_key}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = json.dumps(raw).encode("utf-8")
        raw_path.write_bytes(raw_bytes)
        record_hash = f"{index + 1:064x}"
        row_hash = f"{index + 10:064x}"
        persistence = {
            "verified": True, "ask_id": ask_id, "run_id": run_id,
            "record_sha256": record_hash, "guarded_charge_micros": guarded_charge,
            "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
            "recorded_model_usd_ceiling_micros": 500_000,
            "reservation_release_ceiling_micros": 500_000,
            "native_net_micros": 25_000_000,
            "readback": {"match": True, "run_id": run_id, "record_sha256": record_hash,
                         "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
                         "recorded_model_usd_ceiling_micros": 500_000,
                         "reservation_release_ceiling_micros": 500_000,
                         "native_net_micros": 25_000_000},
        }
        receipt = {
            "question_id": question.id, "attempt_number": attempt_number,
            "attempt_key": attempt_key, "status": "complete", "model_usd": "0.5",
            "model_usd_ceiling_micros": 500_000, "guarded_charge_micros": guarded_charge,
            "attempt_cap_micros": 3_000_000,
            "execution_profile_id": profile.profile_id,
            "source_proof_sha256": profile.source_proof_sha256,
            "source_go_profile_id": profile.source_go_profile_id,
            "authorization_slots": profile.authorization_slots, "source_commit": source_commit,
            "attempt_persistence": persistence,
            "dispatch_result": {"claim_readback": {"match": True},
                                "raw_receipt_path": str(raw_path),
                                "raw_receipt_sha256": hashlib.sha256(raw_bytes).hexdigest()},
        }
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        (self.artifact_dir / f"{attempt_key}.json").write_text(json.dumps(receipt), encoding="utf-8")
        marker = {
            "attempt_key": attempt_key,
            "execution_profile_id": profile.profile_id,
            "prompt_source_sha256": profile.source_proof_sha256,
            "source_proof_sha256": profile.source_proof_sha256,
            "source_go_profile_id": profile.source_go_profile_id,
            "authorization_slots": profile.authorization_slots,
            "source_commit": source_commit,
        }
        (self.artifact_dir / f"{attempt_key}.attempted").write_text(json.dumps(marker), encoding="utf-8")
        saved = {
            "status": "stored", "ask_id": ask_id, "run_id": run_id,
            "record_sha256": f"sha256:{record_hash}", "ask_row_sha256": row_hash,
            "stored_ask_row_sha256": row_hash,
            "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
            "recorded_model_usd_ceiling_micros": 500_000,
            "reservation_release_ceiling_micros": 500_000, "native_net_micros": 25_000_000,
            "readback": {"ask_id": ask_id, "run_id": run_id,
                         "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
                         "recorded_model_usd_ceiling_micros": 500_000,
                         "reservation_release_ceiling_micros": 500_000},
            "scope_admitted_claim_ids": claim_ids,
            "saved_view_status": self.saved_statuses[index],
        }
        w6_path = self.artifact_dir / "w6" / f"{attempt_key}.json"
        w6_path.parent.mkdir(parents=True, exist_ok=True)
        w6_path.write_text(json.dumps(saved), encoding="utf-8")
        return receipt

    def read_raw_attempt(self, receipt):
        return json.loads(Path(receipt["dispatch_result"]["raw_receipt_path"]).read_text(encoding="utf-8"))


def _write_settled_ng_fixture(artifact_dir):
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    question = profile.question_catalog[1]
    attempt_key = demo_pairs._attempt_key(question.id, 1)
    ask_id = "a_20261001_6fa476a9"
    run_id = "r_20261001_231453_f1c126de_28bf5bcd6607481c89084a0092e55c39"
    record_hash = "94527d7562a209aa4d09fa0643643a416811c62d55311f8337d629012af8bbe8"
    row_hash = "2d99e217e2966e95323c8b95802d8f15e46b4c7d6bbe9afbc0463ecc0099e848"
    known_amounts = [0.007643, 0.008048, 0.007334, 0.034728, 0.041177, 0.042911,
                     0.050991, 0.054774, 0.057818, 0.023310, 0.025936, 0.011409,
                     0.010484, 0.270372, 0.035597, 0.055309]
    call_costs = [{"phase": "research", "status": "charged_known", "charged_usd": amount,
                   "reserved_usd": amount, "provider_retry_index": 0}
                  for amount in known_amounts]
    call_costs.extend([
        {"phase": "research", "status": "unknown_charged_ceiling", "charged_usd": 0.744597,
         "reserved_usd": 0.744597, "provider_http_status": 504, "provider_error_type": "ServerError",
         "provider_retry_index": 0, "retry_authorized": False, "measured_input_tokens": None,
         "measured_output_tokens": None, "charged_input_bound_tokens": 446398,
         "charged_output_bound_tokens": 10000},
        {"phase": "structured", "status": "refused_before_dispatch", "charged_usd": 0.0,
         "reserved_usd": 0.0, "stop_reason": "prior_call_failure"},
    ])
    raw = {
        "question_id": question.id, "attempt_number": 1, "attempt_key": attempt_key,
        "run_id": run_id,
        "outcome": "OPERATIONAL STOP", "stop_reason": "unknown_dispatched_cost",
        "error_type": "BudgetRefused", "research_http_status": 504,
        "unknown_cost": True, "raw_answer": None, "claim_rows": [], "call_costs": call_costs,
        "charged_usd": 1.482438, "guarded_charge_micros": 1_482_438,
        "model_usd_micros": None, "reported_model_usd": 1.482438,
        "app_record": {"ask_id": ask_id, "status": "failed", "answer": None,
                       "error_type": "BudgetRefused",
                       "run": {"run_id": run_id, "model_usd": 1.482438}},
        "booking_intents": [{"credit": False, "native_write": False,
                              "phase_reservation_before_dispatch": True,
                              "reported_usd": 1.482438, "usd": 1.482438,
                              "what": "staging_check_ask:TREND-NG-01"}],
        "spend_writes": [],
    }
    raw_dir = Path(artifact_dir) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{attempt_key}.json"
    raw_bytes = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    raw_path.write_bytes(raw_bytes)
    persistence = {
        "verified": True, "ask_id": ask_id, "run_id": run_id, "record_sha256": record_hash,
        "guarded_charge_micros": 1_482_438, "recorded_model_usd": "1.482438",
        "reservation_release_usd": "1.482438", "recorded_model_usd_ceiling_micros": 1_482_438,
        "reservation_release_ceiling_micros": 1_482_438, "native_net_micros": 25_000_000,
        "readback": {"match": True, "run_id": run_id, "record_sha256": record_hash,
                     "recorded_model_usd": "1.482438", "reservation_release_usd": "1.482438",
                     "recorded_model_usd_ceiling_micros": 1_482_438,
                     "reservation_release_ceiling_micros": 1_482_438,
                     "native_net_micros": 25_000_000},
    }
    dispatch = {"raw_receipt": raw, "raw_receipt_path": str(raw_path),
                "raw_receipt_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "claim_readback": {"match": True, "rows": []}}
    receipt = {
        "question_id": question.id, "attempt_number": 1, "attempt_key": attempt_key,
        "status": "unknown_cost", "execution_profile_id": profile.profile_id,
        "source_proof_sha256": profile.source_proof_sha256,
        "source_go_profile_id": profile.source_go_profile_id,
        "authorization_slots": profile.authorization_slots,
        "source_commit": demo_pairs.TRENDING_SETTLED_NG_SOURCE_COMMIT,
        "attempt_cap_micros": 3_000_000, "guarded_charge_micros": 1_482_438,
        "model_usd": "1.482438", "model_usd_ceiling_micros": 1_482_438,
        "attempt_persistence": persistence, "dispatch_result": dispatch,
    }
    Path(artifact_dir).mkdir(parents=True, exist_ok=True)
    (Path(artifact_dir) / f"{attempt_key}.json").write_text(json.dumps(receipt), encoding="utf-8")
    marker = {"attempt_key": attempt_key, "execution_profile_id": profile.profile_id,
              "prompt_source_sha256": profile.source_proof_sha256,
              "source_proof_sha256": profile.source_proof_sha256,
              "source_go_profile_id": profile.source_go_profile_id,
              "authorization_slots": profile.authorization_slots,
              "source_commit": demo_pairs.TRENDING_SETTLED_NG_SOURCE_COMMIT}
    (Path(artifact_dir) / f"{attempt_key}.attempted").write_text(json.dumps(marker), encoding="utf-8")
    saved = {
        "status": "stored", "ask_id": ask_id, "run_id": run_id,
        "record_sha256": f"sha256:{record_hash}", "ask_row_sha256": row_hash,
        "stored_ask_row_sha256": row_hash, "scope_admitted_claim_ids": [],
        "saved_view_status": "no_answer", "recorded_model_usd": "1.482438",
        "reservation_release_usd": "1.482438", "recorded_model_usd_ceiling_micros": 1_482_438,
        "reservation_release_ceiling_micros": 1_482_438, "native_net_micros": 25_000_000,
        "readback": {"ask_id": ask_id, "run_id": run_id,
                      "recorded_model_usd": "1.482438", "reservation_release_usd": "1.482438",
                      "recorded_model_usd_ceiling_micros": 1_482_438,
                      "reservation_release_ceiling_micros": 1_482_438},
    }
    w6_dir = Path(artifact_dir) / "w6"
    w6_dir.mkdir(parents=True, exist_ok=True)
    (w6_dir / f"{attempt_key}.json").write_text(json.dumps(saved), encoding="utf-8")
    return receipt, raw, raw_bytes


def test_prepare_reports_funding_date_block_after_local_runtime_load(tmp_path):
    inputs, data, artifact_dir = _funding_inputs(tmp_path)
    runtime = _Runtime(datetime(2026, 10, 2, 12, tzinfo=SAST))

    result = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: runtime, inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )

    assert result["status"] == "blocked"
    assert result["stop_reason"] == "funding_run_date_mismatch"
    assert runtime.events == ["sources", "load"]


def test_source_must_be_clean_and_descend_from_product_fix(tmp_path):
    inputs, data, artifact_dir = _funding_inputs(tmp_path)
    runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST))
    runtime._verify_committed_sources = lambda: {
        "clean": True, "head": "b" * 40, "hashes": {}, "ancestors": {},
    }

    result = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: runtime, inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )

    assert result["status"] == "blocked"
    assert result["stop_reason"] == "product_fix_source_ancestry_unverified"
    assert runtime.events == []


def test_saved_readback_accepts_actual_w5_w6_contract_and_preserves_insufficient_status(tmp_path):
    question = demo_pairs.TRENDING_FALLBACK_PROFILE.question_catalog[0]
    attempt_key = demo_pairs._attempt_key(question.id, 1)
    ask_id = "a_20261001_c762e30f"
    run_id = "r_20261001_221604_54f3d3bd_2aba742355ca408e9ba5022567dfa550"
    bare_record_hash = "ce0cada241b62c728bea20ef10ab50fc58c0b89150c24601444f2456b0008862"
    ask_row_hash = "ca8bfcc3b9e55808ecb57c0e53597187fb8787651da46e956373f0fbe54034e9"
    model_usd = "1.2668079375"
    ceiling = 1_266_808
    persistence = {
        "ask_id": ask_id, "run_id": run_id, "record_sha256": bare_record_hash,
        "recorded_model_usd": model_usd, "reservation_release_usd": model_usd,
        "recorded_model_usd_ceiling_micros": ceiling,
        "reservation_release_ceiling_micros": ceiling, "native_net_micros": 25_000_000,
        "readback": {
            "match": True, "run_id": run_id, "record_sha256": bare_record_hash,
            "recorded_model_usd": model_usd, "reservation_release_usd": model_usd,
            "recorded_model_usd_ceiling_micros": ceiling,
            "reservation_release_ceiling_micros": ceiling, "native_net_micros": 25_000_000,
        },
    }
    saved = {
        "status": "stored", "ask_id": ask_id, "run_id": run_id,
        "record_sha256": f"sha256:{bare_record_hash}", "ask_row_sha256": ask_row_hash,
        "stored_ask_row_sha256": ask_row_hash, "scope_admitted_claim_ids": [],
        "saved_view_status": "no_answer", "recorded_model_usd": model_usd,
        "reservation_release_usd": model_usd,
        "recorded_model_usd_ceiling_micros": ceiling,
        "reservation_release_ceiling_micros": ceiling, "native_net_micros": 25_000_000,
        "readback": {
            "ask_id": ask_id, "run_id": run_id, "recorded_model_usd": model_usd,
            "reservation_release_usd": model_usd,
            "recorded_model_usd_ceiling_micros": ceiling,
            "reservation_release_ceiling_micros": ceiling,
        },
    }
    w6_dir = tmp_path / "w6"
    w6_dir.mkdir()
    (w6_dir / f"{attempt_key}.json").write_text(json.dumps(saved), encoding="utf-8")
    raw = {
        "outcome": "COMPLETE",
        "raw_answer": {"claims": [{"id": "c1"}], "evidence": []},
        "app_record": {"status": "complete", "answer": {"status": "insufficient_evidence"},
                       "version": None},
    }
    receipt = {"attempt_persistence": {**persistence, "guarded_charge_micros": 1_267_125}}

    result = trending_replay._saved_ask_readback(tmp_path, question, receipt, raw)

    assert result[0]["ask_id"] == ask_id
    assert result[0]["record_sha256"] == f"sha256:{bare_record_hash}"
    assert result[2:4] == ([], [])
    assert result[4:] == ("insufficient_evidence", "insufficient_evidence", False)


def test_parent_za_snapshot_is_pinned_and_never_redispatched(tmp_path, monkeypatch):
    inputs, data, artifact_dir = _funding_inputs(tmp_path)
    prior = _verified_prior_receipts()
    read_receipts = demo_pairs._existing_receipts
    monkeypatch.setattr(
        demo_pairs, "_existing_receipts",
        lambda path, profile=None: prior if Path(path) == data else read_receipts(path, profile=profile),
    )
    monkeypatch.setattr(demo_pairs, "_claim_readback_matches", lambda value: value["claim_readback"]["match"])
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    za = profile.question_catalog[0]
    old_runtime = _Runtime(
        datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir,
        saved_statuses=["no_answer", "insufficient_evidence", "complete"],
        answer_statuses=["insufficient_evidence", "complete", "partial"],
        source_commit=trending_replay.PREVIOUS_LOCAL_SOURCE_COMMIT,
        guarded_charges=[1_267_125, 500_000, 500_000],
    )
    old_runtime.run_attempt(
        za, 1, {"app_run_ids": [], "consumed_micros": 0}, {"market": "ZA"},
        profile=profile, attempt_data_dir=artifact_dir, prior_data_dir=data,
        ranking_proof_bytes=demo_pairs.TRENDING_FALLBACK_PROFILE_PROOF_BYTES,
        source_commit=trending_replay.PREVIOUS_LOCAL_SOURCE_COMMIT,
    )
    old_runtime.run_calls.clear()

    assert trending_replay.PREVIOUS_LOCAL_ZA_ARTIFACT_HASHES == {
        "TREND-ZA-01-attempt-1.json": "1987a8a4bc037c37d9e7a02066da82c08e08bc3cf5a8a2ed71b3238e7ac0cc4b",
        "raw/TREND-ZA-01-attempt-1.json": "7d8dbbdfd08803775a546a95dee072b488b0a9c4e8743b23322f0166586501ce",
        "w6/TREND-ZA-01-attempt-1.json": "668f06f9e7cf221dbdd60fd536f99ccb9271ba4469bb26fc9c5871aa5d7e9768",
        "TREND-ZA-01-attempt-1.attempted": "9059230529634cab6a55597a1a63fbd7dcb76c5bb2bddcf689a96fc2cf039ead",
    }
    fixture_hashes = {
        relative: hashlib.sha256((artifact_dir / relative).read_bytes()).hexdigest()
        for relative in trending_replay.PREVIOUS_LOCAL_ZA_ARTIFACT_HASHES
    }
    monkeypatch.setattr(trending_replay, "PREVIOUS_LOCAL_ZA_ARTIFACT_HASHES", fixture_hashes)
    source_commit = "d" * 40
    ancestry_calls = []
    monkeypatch.setattr(trending_replay, "_verify_product_fix_source", lambda *_: None)

    def verify_ancestry(_repo_root, ancestor, descendant, _reason):
        ancestry_calls.append((ancestor, descendant))

    monkeypatch.setattr(trending_replay, "_verify_source_ancestor", verify_ancestry)
    runtime = _Runtime(
        datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir,
        source_commit=source_commit, guarded_charges=[1_267_125, 500_000, 500_000],
    )
    runtime.run_count = 1
    result = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )

    assert result["status"] == "complete"
    assert [call["market"] for call in runtime.run_calls] == ["NG", "KE"]
    assert [row["source_commit"] for row in result["markets"]] == [
        trending_replay.PREVIOUS_LOCAL_SOURCE_COMMIT, source_commit, source_commit,
    ]
    assert ancestry_calls == [(trending_replay.PREVIOUS_LOCAL_SOURCE_COMMIT, source_commit)] * 3

    raw_path = artifact_dir / "raw" / "TREND-ZA-01-attempt-1.json"
    raw_path.write_bytes(raw_path.read_bytes() + b" ")
    blocked_runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir,
                               source_commit=source_commit)
    blocked = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: blocked_runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )
    assert blocked["status"] == "stopped"
    assert blocked["stop_reason"] == "trending_prior_za_snapshot_changed"
    assert blocked_runtime.run_calls == []


def test_settled_ng_504_is_consumed_and_only_ke_dispatches(tmp_path, monkeypatch):
    inputs, data, artifact_dir = _funding_inputs(tmp_path)
    prior = _verified_prior_receipts()
    read_receipts = demo_pairs._existing_receipts
    monkeypatch.setattr(
        demo_pairs, "_existing_receipts",
        lambda path, profile=None: prior if Path(path) == data else read_receipts(path, profile=profile),
    )
    monkeypatch.setattr(demo_pairs, "_claim_readback_matches", lambda value: value["claim_readback"]["match"])
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    za, ng, _ = profile.question_catalog
    previous_runtime = _Runtime(
        datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir,
        saved_statuses=["no_answer", "insufficient_evidence", "complete"],
        answer_statuses=["insufficient_evidence", "complete", "partial"],
        source_commit=trending_replay.PREVIOUS_LOCAL_SOURCE_COMMIT,
        guarded_charges=[1_267_125, 1_482_438, 500_000],
    )
    previous_runtime.run_attempt(
        za, 1, {"app_run_ids": [], "consumed_micros": 0}, {"market": "ZA"},
        profile=profile, attempt_data_dir=artifact_dir, prior_data_dir=data,
        ranking_proof_bytes=demo_pairs.TRENDING_FALLBACK_PROFILE_PROOF_BYTES,
        source_commit=trending_replay.PREVIOUS_LOCAL_SOURCE_COMMIT,
    )
    ng_receipt, _, ng_raw_bytes = _write_settled_ng_fixture(artifact_dir)

    za_hashes = {
        relative: hashlib.sha256((artifact_dir / relative).read_bytes()).hexdigest()
        for relative in trending_replay.PREVIOUS_LOCAL_ZA_ARTIFACT_HASHES
    }
    ng_hashes = {
        "TREND-NG-01-attempt-1.json": hashlib.sha256(
            (artifact_dir / "TREND-NG-01-attempt-1.json").read_bytes()).hexdigest(),
        "raw/TREND-NG-01-attempt-1.json": hashlib.sha256(ng_raw_bytes).hexdigest(),
        "w6/TREND-NG-01-attempt-1.json": hashlib.sha256(
            (artifact_dir / "w6/TREND-NG-01-attempt-1.json").read_bytes()).hexdigest(),
        "TREND-NG-01-attempt-1.attempted": hashlib.sha256(
            (artifact_dir / "TREND-NG-01-attempt-1.attempted").read_bytes()).hexdigest(),
    }
    assert trending_replay.PREVIOUS_LOCAL_NG_ARTIFACT_HASHES == {
        "TREND-NG-01-attempt-1.json": "bdbb0f4c91577dd68bcad8ba4419a30e0edb44dcbac7d42bdc8d2d0bd25ad7dc",
        "raw/TREND-NG-01-attempt-1.json": "e6530e31d114b19f4cc15eeffd86c52047272e161b20ccfa06cfcd036229b26a",
        "w6/TREND-NG-01-attempt-1.json": "2b441109184ceca31c3a895c4a09e7617e933205519d403ac5d0481469505422",
        "TREND-NG-01-attempt-1.attempted": "7f78aab36d77f12ce2226a89d1667185e0dfb11c3a4c7df21e4fd8b4c2290e59",
    }
    monkeypatch.setattr(trending_replay, "PREVIOUS_LOCAL_ZA_ARTIFACT_HASHES", za_hashes)
    monkeypatch.setattr(trending_replay, "PREVIOUS_LOCAL_NG_ARTIFACT_HASHES", ng_hashes)
    assert demo_pairs.TRENDING_SETTLED_NG_RAW_RECEIPT_SHA256 == "e6530e31d114b19f4cc15eeffd86c52047272e161b20ccfa06cfcd036229b26a"
    monkeypatch.setattr(demo_pairs, "TRENDING_SETTLED_NG_RAW_RECEIPT_SHA256", ng_receipt["dispatch_result"]["raw_receipt_sha256"])
    assert demo_pairs._profile_receipt_is_verified(ng_receipt, profile)
    altered = json.loads(json.dumps(ng_receipt))
    altered["dispatch_result"]["raw_receipt"]["call_costs"][-2]["provider_http_status"] = 500
    assert not demo_pairs._profile_receipt_is_verified(altered, profile)
    source_commit = "d" * 40
    monkeypatch.setattr(trending_replay, "_verify_product_fix_source", lambda *_: None)
    ancestry_calls = []

    def verify_ancestry(_repo_root, ancestor, descendant, _reason):
        ancestry_calls.append((ancestor, descendant))

    monkeypatch.setattr(trending_replay, "_verify_source_ancestor", verify_ancestry)
    runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir,
                       source_commit=source_commit)
    runtime.run_count = 2

    result = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )

    assert result["status"] == "complete"
    assert [call["market"] for call in runtime.run_calls] == ["KE"]
    assert [row["source_commit"] for row in result["markets"]] == [
        trending_replay.PREVIOUS_LOCAL_SOURCE_COMMIT,
        demo_pairs.TRENDING_SETTLED_NG_SOURCE_COMMIT,
        source_commit,
    ]
    ng_result = result["markets"][1]
    assert ng_result["status"] == "operational_failure"
    assert ng_result["failure_stop_reason"] == "unknown_dispatched_cost"
    assert ng_result["provider_http_status"] == 504
    assert ng_result["usage_priced_model_usd"] is None
    assert ng_result["retained_guarded_charge_micros"] == 1_482_438
    assert ng_result["known_call_booking_micros"] == 737_841
    assert ng_result["failed_call_ceiling_micros"] == 744_597
    assert ancestry_calls.count((trending_replay.PREVIOUS_LOCAL_SOURCE_COMMIT, source_commit)) == 2
    assert ancestry_calls.count((demo_pairs.TRENDING_SETTLED_NG_SOURCE_COMMIT, source_commit)) == 2

    ng_raw_path = artifact_dir / "raw" / "TREND-NG-01-attempt-1.json"
    ng_raw_path.write_bytes(ng_raw_path.read_bytes() + b" ")
    blocked_runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir,
                               source_commit=source_commit)
    blocked = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: blocked_runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )
    assert blocked["status"] == "stopped"
    assert blocked["stop_reason"] == "trending_prior_ng_snapshot_changed"
    assert blocked_runtime.run_calls == []


def test_execute_is_sequential_skips_insufficient_and_preserves_saved_ask_metadata(tmp_path, monkeypatch):
    inputs, data, artifact_dir = _funding_inputs(tmp_path)
    prior = _verified_prior_receipts()
    read_receipts = demo_pairs._existing_receipts
    monkeypatch.setattr(
        demo_pairs, "_existing_receipts",
        lambda path, profile=None: prior if Path(path) == data else read_receipts(path, profile=profile),
    )
    monkeypatch.setattr(demo_pairs, "_claim_readback_matches", lambda value: value["claim_readback"]["match"])
    monkeypatch.setenv("MODEL_PROVIDER", "openai")  # a prior value the replay must put back
    runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir)

    result = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )

    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    assert [call["market"] for call in runtime.run_calls] == ["ZA", "NG", "KE"]
    assert runtime.provider_values == ["gemini", "gemini", "gemini"]
    assert os.environ["MODEL_PROVIDER"] == "openai"
    assert [call["app_run_ids"] for call in runtime.run_calls] == [
        [*(f"prior-{index}" for index in range(5))],
        [*(f"prior-{index}" for index in range(5)), "run-ZA"],
        [*(f"prior-{index}" for index in range(5)), "run-ZA", "run-NG"],
    ]
    assert [call["consumed_micros"] for call in runtime.run_calls] == [426_564, 926_564, 1_426_564]
    assert [row["question_id"] for row in result["markets"]] == [q.id for q in profile.question_catalog]
    assert [row["status"] for row in result["markets"]] == ["partial", "insufficient_evidence", "complete"]
    assert result["markets"][1]["answer_status"] == "complete"
    assert result["markets"][1]["qualifies_for_l5"] is False
    assert result["markets"][1]["version"] is None

    handoff_path = Path(result["markets"][0]["l5_handoff_path"])
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert {
        key: handoff[key]
        for key in ("ask_id", "object_id", "object_type", "version", "run_id", "record_sha256",
                    "claim_ids", "evidence_ids", "source_commit", "runtime_health_version",
                    "saved_view_status", "raw_answer_status", "accepted_ids")
    } == {
        "ask_id": "ask-ZA", "object_id": "ask-ZA", "object_type": "ask",
        "version": "agent-version-12", "run_id": "run-ZA", "record_sha256": f"sha256:{1:064x}",
        "claim_ids": ["claim-ZA-1", "claim-ZA-2"],
        "evidence_ids": ["evidence-ZA-1", "evidence-ZA-2"],
        "source_commit": trending_replay.PRODUCT_FIX_COMMIT,
        "runtime_health_version": "b" * 12, "saved_view_status": "partial",
        "raw_answer_status": "complete", "accepted_ids": [],
    }


def test_provider_is_restored_when_attempt_run_fails(tmp_path, monkeypatch):
    inputs, data, artifact_dir = _funding_inputs(tmp_path)
    prior = _verified_prior_receipts()
    read_receipts = demo_pairs._existing_receipts
    monkeypatch.setattr(
        demo_pairs, "_existing_receipts",
        lambda path, profile=None: prior if Path(path) == data else read_receipts(path, profile=profile),
    )
    monkeypatch.setenv("MODEL_PROVIDER", "openai")  # a prior value the replay must put back
    runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir)
    runtime.fail_run = True

    result = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )

    assert result["status"] == "stopped"
    assert result["stop_reason"].startswith("attempt_run_failed:")
    assert runtime.provider_values == ["gemini"]
    assert os.environ["MODEL_PROVIDER"] == "openai"


def test_restart_skips_verified_slots_and_blocks_incomplete_marker(tmp_path, monkeypatch):
    inputs, data, artifact_dir = _funding_inputs(tmp_path)
    prior = _verified_prior_receipts()
    read_receipts = demo_pairs._existing_receipts
    monkeypatch.setattr(
        demo_pairs, "_existing_receipts",
        lambda path, profile=None: prior if Path(path) == data else read_receipts(path, profile=profile),
    )
    monkeypatch.setattr(demo_pairs, "_claim_readback_matches", lambda value: value["claim_readback"]["match"])
    first_runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir)
    first = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: first_runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )
    assert first["status"] == "complete"

    resumed_runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=artifact_dir)
    resumed = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: resumed_runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=artifact_dir,
    )
    assert resumed["status"] == "complete"
    assert resumed_runtime.run_calls == []
    assert [row["run_id"] for row in resumed["markets"]] == ["run-ZA", "run-NG", "run-KE"]

    partial_artifacts = tmp_path / "incomplete"
    partial_artifacts.mkdir()
    attempt_key = demo_pairs._attempt_key(demo_pairs.TRENDING_FALLBACK_PROFILE.question_catalog[0].id, 1)
    (partial_artifacts / f"{attempt_key}.attempted").write_text("{}", encoding="utf-8")
    blocked_runtime = _Runtime(datetime(2026, 10, 1, 12, tzinfo=SAST), artifact_dir=partial_artifacts)
    blocked = trending_replay.run_trending_replay(
        runtime_factory=lambda **_: blocked_runtime, execute=True,
        inputs_dir=inputs, data_dir=data, artifact_dir=partial_artifacts,
    )
    assert blocked["status"] == "stopped"
    assert "unresolved_attempt_marker" in blocked["stop_reason"]
    assert blocked_runtime.run_calls == []

