from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from core.eval import ask_r2, demo_pairs
from core.eval.demo_pairs import (
    CUMULATIVE_CAP_MICROS,
    DEMO_QUESTIONS,
    DEMO_QUESTIONS_SHA256,
    MAX_ATTEMPTS,
    NEW_POOL_MICROS,
    DemoRunRefused,
    prepare_funding_transfer,
    run_attempt,
    source_proof_hash,
    validate_funding_transfer_readback,
)


RUN_DATE = date(2026, 10, 1)
SAST = timezone(timedelta(hours=2))


@pytest.fixture(autouse=True)
def freeze_demo_clock(monkeypatch):
    monkeypatch.setattr(demo_pairs, "_now_sast", lambda: datetime(2026, 10, 1, 12, tzinfo=SAST))


def r3_rows():
    values = (1_357_150, 1_158_263, 15_000_000, 1_646_701,
              2_388_517, 1_805_746, 1_643_623)
    ids = (
        "NOW-01-spend-12638a48", "NOW-01-retry-1-spend-3f5031e1",
        "NOW-01-continuation-1-spend-5df280f9", "RISE-02-spend-8e9d4c20",
        "CRE-01-spend-6a21605f", "SPR-03-spend-8a5f5498", "WHY-03-spend-19e2f107",
    )
    return [
        {"run_id": f"l3-ask-20260930-r3-{run_id}",
         "run_date": "2026-09-30", "model_usd_micros": amount}
        for run_id, amount in zip(ids, values)
    ]


def funding_proof(*, allocated=NEW_POOL_MICROS, consumed=0, app_run_ids=()):
    return {
        "schema_version": "demo-funding-v1",
        "phase_id": "l3-demo-20261001",
        "verified": True,
        "run_date": RUN_DATE.isoformat(),
        "reservation_run_id": "demo-funding-20261001-source-proof",
        "allocated_micros": allocated,
        "consumed_micros": consumed,
        "source_proof_sha": source_proof_hash(r3_rows()),
        "native_net_micros": CUMULATIVE_CAP_MICROS,
        "baseline_run_ids": [row["run_id"] for row in r3_rows()],
        "app_run_ids": list(app_run_ids),
    }


def context_for(question_id="DEMO-01"):
    question = next(question for question in DEMO_QUESTIONS if question.id == question_id)
    return {
        "market": question.market,
        "source_ids": list(question.source_ids),
        "posts": [{"id": source_id, "text": "Fetched source text"} for source_id in question.source_ids],
    }


def _ranked_context():
    return {"market": "ZA", "source_ids": [], "posts": []}


def _write_prior_five(data_dir):
    amounts = (100_000, 100_000, 100_000, 100_000, 26_564)
    app_ids = []
    data_dir.mkdir(parents=True, exist_ok=True)
    for index, ((question_id, attempt_number), amount) in enumerate(
            zip(demo_pairs.ATTEMPT_SEQUENCE[:5], amounts), start=1):
        attempt_key = demo_pairs._attempt_key(question_id, attempt_number)
        app_id = f"prior-app-{index}"
        app_ids.append(app_id)
        marker = {
            "schema_version": "demo-pair-marker-v1",
            "attempt_key": attempt_key,
            "prompt_source_sha256": DEMO_QUESTIONS_SHA256,
            "attempt_cap_micros": NEW_POOL_MICROS // 10,
        }
        receipt = {
            "question_id": question_id,
            "attempt_number": attempt_number,
            "status": "complete",
            "guarded_charge_micros": amount,
            "attempt_persistence": {"verified": True, "run_id": app_id},
        }
        (data_dir / f"{attempt_key}.attempted").write_text(
            json.dumps(marker), encoding="utf-8")
        (data_dir / f"{attempt_key}.json").write_text(
            json.dumps(receipt), encoding="utf-8")
    return app_ids


def dispatch_result(budget, *, run_id="ask-DEMO-01-1", answer="offline answer", reported_micros=200_000):
    return {
        "answer": answer,
        "status": "complete" if answer is not None else "no_answer",
        "raw_receipt": {"run_id": run_id, "claim_rows": []},
        "claim_readback": {"match": True, "rows": []},
    }


def persist_attempt(request, result):
    raw = result["raw_receipt"]
    reported = "0" if result.get("status") == "unknown_cost" else "0.2"
    ceiling = 0 if reported == "0" else 200_000
    record_sha = "b" * 64
    return {
        "verified": True,
        "ask_id": raw["run_id"],
        "run_id": f"r_{request.attempt_key.replace('-', '_')}",
        "record_sha256": record_sha,
        "guarded_charge_micros": result["guarded_charge_micros"],
        "recorded_model_usd": reported,
        "reservation_release_usd": reported,
        "recorded_model_usd_ceiling_micros": ceiling,
        "reservation_release_ceiling_micros": ceiling,
        "native_net_micros": CUMULATIVE_CAP_MICROS,
        "readback": {
            "match": True,
            "run_id": f"r_{request.attempt_key.replace('-', '_')}",
            "record_sha256": record_sha,
            "recorded_model_usd": reported,
            "reservation_release_usd": reported,
            "recorded_model_usd_ceiling_micros": ceiling,
            "reservation_release_ceiling_micros": ceiling,
            "native_net_micros": CUMULATIVE_CAP_MICROS,
        },
    }


def spend_once(budget, amount_usd=1.0):
    ticket = budget.reserve_fixed("structured", "offline", amount_usd, 100)
    budget.mark_dispatched(ticket)
    budget.settle(ticket, actual_usd=0.5)


def test_frozen_catalog_keeps_exact_prompts_markets_and_source_ids():
    assert DEMO_QUESTIONS_SHA256 == "d4613ac2f2390f8efb9c0b61af503c3050e3fa9bf03704aa839374b654830e2f"
    assert tuple(question.id for question in DEMO_QUESTIONS) == tuple(f"DEMO-0{i}" for i in range(1, 6))
    assert tuple(question.market for question in DEMO_QUESTIONS) == ("NG", "KE", "NG", "KE", "ZA")
    assert DEMO_QUESTIONS[0].text == "Which exact lines appear in the three cited TikTok posts mentioning Sabinus or Peller?"
    assert DEMO_QUESTIONS[0].source_ids == (
        "obs1_585d958e17e03bf9f9ccad5c7c2f0b38",
        "obs1_da53ac54e21872d0554146ef21188590",
        "obs1_78054f141c7fb15c269a8c1cfac4a1ae",
    )
    assert DEMO_QUESTIONS[1].source_ids == ("obs1_4fce7a7d6895ef0915e0b217fbddf290",)
    assert DEMO_QUESTIONS[2].source_ids == ("obs1_da53ac54e21872d0554146ef21188590",)
    assert DEMO_QUESTIONS[3].source_ids == ("obs1_4fce7a7d6895ef0915e0b217fbddf290",)
    assert DEMO_QUESTIONS[4].source_ids == ("obs1_dce53bd6bb321b436ac9d0078763dc04",)
    assert MAX_ATTEMPTS == 2


def test_transfer_plan_is_balanced_and_readback_preserves_source_rows():
    before = r3_rows()
    unknown = [before[4], before[5]]
    source_sha = source_proof_hash(before)
    plan = prepare_funding_transfer(
        run_date=RUN_DATE,
        amount_micros=NEW_POOL_MICROS,
        daily_headroom_micros=40_000_000,
        source_proof_sha=source_sha,
        source_rows=before,
        unknown_rows=unknown,
    )

    assert plan["amount_micros"] == NEW_POOL_MICROS
    assert sum(row["model_usd_micros"] for row in plan["correction_rows"]) == 0
    proof = validate_funding_transfer_readback(plan, before + plan["correction_rows"])
    assert proof == {
        "schema_version": "demo-funding-v1",
        "phase_id": "l3-demo-20261001",
        "verified": True,
        "run_date": RUN_DATE.isoformat(),
        "reservation_run_id": plan["reservation_run_id"],
        "allocated_micros": NEW_POOL_MICROS,
        "consumed_micros": 0,
        "source_proof_sha": source_sha,
        "native_net_micros": CUMULATIVE_CAP_MICROS,
        "baseline_run_ids": [row["run_id"] for row in before],
        "app_run_ids": [],
    }
    assert plan["unknown_rows"] == unknown


def test_transfer_refuses_amount_above_pool_or_fresh_daily_headroom():
    before = r3_rows()
    source_sha = source_proof_hash(before)
    with pytest.raises(DemoRunRefused, match="funding_transfer_limit"):
        prepare_funding_transfer(
            run_date=RUN_DATE,
            amount_micros=NEW_POOL_MICROS,
            daily_headroom_micros=NEW_POOL_MICROS - 1,
            source_proof_sha=source_sha,
            source_rows=before,
            unknown_rows=[before[4]],
        )


def test_transfer_rejects_source_hash_not_bound_to_approved_rows():
    before = r3_rows()
    with pytest.raises(DemoRunRefused, match="funding_source_proof_mismatch"):
        prepare_funding_transfer(
            run_date=RUN_DATE,
            amount_micros=NEW_POOL_MICROS,
            daily_headroom_micros=NEW_POOL_MICROS,
            source_proof_sha="a" * 64,
            source_rows=before,
            unknown_rows=[before[4]],
        )


def test_attempt_requires_verified_funding_before_dispatch(tmp_path):
    calls = []

    def dispatch(_request, _budget):
        calls.append("dispatch")
        return {}

    invalid = funding_proof()
    invalid["verified"] = False
    with pytest.raises(DemoRunRefused, match="funding_readback_unverified"):
        run_attempt(
            question_id="DEMO-01", attempt_number=1, data_dir=tmp_path,
            funding_proof=invalid, provided_context=context_for(), dispatch=dispatch,
            persist_attempt=persist_attempt,
        )

    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_stale_sast_funding_proof_refuses_before_marker_or_dispatch(tmp_path):
    calls = []

    def dispatch(_request, _budget):
        calls.append("dispatch")
        return {}

    with pytest.raises(DemoRunRefused, match="demo_funding_date_stale"):
        run_attempt(
            question_id="DEMO-01", attempt_number=1, data_dir=tmp_path,
            funding_proof=funding_proof(), provided_context=context_for(), dispatch=dispatch,
            persist_attempt=persist_attempt,
            clock=lambda: datetime(2026, 10, 2, 0, 1, tzinfo=SAST),
        )

    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_attempt_uses_session_budget_once_and_keeps_context_separate(tmp_path):
    contexts = context_for()
    calls = []

    def dispatch(request, budget):
        calls.append((request, budget))
        spend_once(budget)
        return dispatch_result(budget, run_id=f"ask-{request.attempt_key}")

    receipt = run_attempt(
        question_id="DEMO-01", attempt_number=1, data_dir=tmp_path,
        funding_proof=funding_proof(), provided_context=contexts, dispatch=dispatch,
        persist_attempt=persist_attempt,
    )

    request, budget = calls[0]
    assert len(calls) == 1
    assert isinstance(budget, ask_r2.SessionBudget)
    assert budget.provider_500_retries == 0 and budget.provider_retry_statuses == ()
    assert request.prompt == DEMO_QUESTIONS[0].text
    assert request.provided_context is contexts
    assert request.market == "NG" and request.source_ids == DEMO_QUESTIONS[0].source_ids
    assert receipt["attempt_cap_micros"] == NEW_POOL_MICROS // 10 == 1_297_428
    assert receipt["guarded_charge_micros"] == 500_000
    assert receipt["model_usd"] == "0.2"
    assert receipt["model_usd_ceiling_micros"] == 200_000
    assert receipt["retained_guard_difference_usd"] == "0.3"
    assert receipt["attempt_persistence"]["verified"] is True
    assert set(receipt["attempt_persistence"]) == {
        "verified", "ask_id", "run_id", "record_sha256", "guarded_charge_micros",
        "recorded_model_usd", "reservation_release_usd", "recorded_model_usd_ceiling_micros",
        "reservation_release_ceiling_micros", "native_net_micros", "readback",
    }
    assert receipt["attempt_persistence"]["ask_id"] != receipt["attempt_persistence"]["run_id"]

    saved = json.loads((tmp_path / "DEMO-01-attempt-1.json").read_text(encoding="utf-8"))
    assert saved["prompt"] == DEMO_QUESTIONS[0].text
    assert saved["prompt_source_sha256"] == DEMO_QUESTIONS_SHA256
    assert saved["provided_context"] == contexts
    marker = json.loads((tmp_path / "DEMO-01-attempt-1.attempted").read_text(encoding="utf-8"))
    assert marker == {
        "schema_version": "demo-pair-marker-v1",
        "attempt_key": "DEMO-01-attempt-1",
        "prompt_source_sha256": DEMO_QUESTIONS_SHA256,
        "attempt_cap_micros": NEW_POOL_MICROS // 10,
    }


def test_ranked_once_profile_uses_verified_prior_funding_and_preserves_old_receipts(tmp_path):
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    prior_dir = tmp_path / "prior"
    app_ids = _write_prior_five(prior_dir)
    before = {path.name: path.read_bytes() for path in prior_dir.iterdir()}
    ranked_dir = tmp_path / "ranked-once"
    calls = []
    proof_path = Path("C:/Users/AlbertMeintjes/dev/42-inputs/FREE-RETRIEVAL-RANK-MATRIX-2026-10-01.json")
    proof_bytes = proof_path.read_bytes()

    def dispatch(request, budget):
        calls.append(request)
        spend_once(budget)
        return dispatch_result(budget, run_id=f"ask-{request.attempt_key}")

    receipt = run_attempt(
        question_id="NOW-01", attempt_number=1, data_dir=ranked_dir,
        prior_data_dir=prior_dir,
        funding_proof=funding_proof(allocated=NEW_POOL_MICROS, consumed=426_564,
                                   app_run_ids=app_ids),
        provided_context=_ranked_context(), dispatch=dispatch,
        persist_attempt=persist_attempt, profile=profile,
        ranking_proof_bytes=proof_bytes, source_commit="a" * 40,
    )

    assert len(calls) == 1
    assert calls[0].prompt == profile.question_catalog[0].text
    assert calls[0].market == "ZA" and calls[0].source_ids == ()
    assert receipt["attempt_cap_micros"] == 3_000_000
    assert receipt["prompt_source_sha256"] == profile.source_proof_sha256
    assert receipt["source_proof_sha256"] == profile.source_proof_sha256
    assert receipt["source_go_profile_id"] == profile.source_go_profile_id
    assert receipt["authorization_slots"] == 1
    assert receipt["source_commit"] == "a" * 40
    marker = json.loads((ranked_dir / "NOW-01-attempt-1.attempted").read_text(encoding="utf-8"))
    assert marker["execution_profile_id"] == profile.profile_id
    assert marker["prompt_source_sha256"] == profile.source_proof_sha256
    assert marker["source_proof_sha256"] == profile.source_proof_sha256
    assert marker["authorization_slots"] == 1
    assert before == {path.name: path.read_bytes() for path in prior_dir.iterdir()}

    with pytest.raises(DemoRunRefused, match="duplicate_attempt_marker"):
        run_attempt(
            question_id="NOW-01", attempt_number=1, data_dir=ranked_dir,
            prior_data_dir=prior_dir,
            funding_proof=funding_proof(allocated=NEW_POOL_MICROS, consumed=426_564,
                                       app_run_ids=app_ids),
            provided_context=_ranked_context(), dispatch=dispatch,
            persist_attempt=persist_attempt, profile=profile,
            ranking_proof_bytes=proof_bytes, source_commit="a" * 40,
        )
    assert len(calls) == 1


@pytest.mark.parametrize(("question_id", "attempt_number"), (("DEMO-01", 1), ("DEMO-03", 1), ("NOW-01", 2)))
def test_ranked_once_profile_rejects_other_questions_and_slots(tmp_path, question_id, attempt_number):
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    prior_dir = tmp_path / "prior"
    app_ids = _write_prior_five(prior_dir)
    ranked_dir = tmp_path / "ranked-once"
    calls = []
    proof_bytes = Path("C:/Users/AlbertMeintjes/dev/42-inputs/FREE-RETRIEVAL-RANK-MATRIX-2026-10-01.json").read_bytes()

    with pytest.raises(DemoRunRefused, match="ranked_attempt_not_authorized"):
        run_attempt(
            question_id=question_id, attempt_number=attempt_number, data_dir=ranked_dir,
            prior_data_dir=prior_dir,
            funding_proof=funding_proof(allocated=NEW_POOL_MICROS, consumed=426_564,
                                       app_run_ids=app_ids),
            provided_context=_ranked_context(), dispatch=lambda *_args: calls.append("dispatch"),
            persist_attempt=persist_attempt, profile=profile,
            ranking_proof_bytes=proof_bytes, source_commit="a" * 40,
        )
    assert calls == [] and not ranked_dir.exists()


def test_ranked_once_profile_refuses_changed_proof_before_marker(tmp_path):
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    prior_dir = tmp_path / "prior"
    app_ids = _write_prior_five(prior_dir)
    ranked_dir = tmp_path / "ranked-once"
    proof_bytes = Path("C:/Users/AlbertMeintjes/dev/42-inputs/FREE-RETRIEVAL-RANK-MATRIX-2026-10-01.json").read_bytes() + b" "

    with pytest.raises(DemoRunRefused, match="ranked_proof_hash_mismatch"):
        run_attempt(
            question_id="NOW-01", attempt_number=1, data_dir=ranked_dir,
            prior_data_dir=prior_dir,
            funding_proof=funding_proof(allocated=NEW_POOL_MICROS, consumed=426_564,
                                       app_run_ids=app_ids),
            provided_context=_ranked_context(), dispatch=lambda *_args: pytest.fail("dispatch called"),
            persist_attempt=persist_attempt, profile=profile,
            ranking_proof_bytes=proof_bytes, source_commit="a" * 40,
        )
    assert not ranked_dir.exists()


@pytest.mark.parametrize(
    ("allocated", "consumed", "proof_app_count", "reason"),
    ((NEW_POOL_MICROS, 426_563, 5, "funding_consumption_readback_mismatch"),
     (NEW_POOL_MICROS, 426_564, 4, "funding_consumption_readback_mismatch"),
     (3_426_563, 426_564, 5, "attempt_cap_exhausted")),
)
def test_ranked_once_profile_refuses_funding_or_cap_mismatch(
        tmp_path, allocated, consumed, proof_app_count, reason):
    profile = demo_pairs.RANKED_NOW_ONCE_PROFILE
    prior_dir = tmp_path / "prior"
    app_ids = _write_prior_five(prior_dir)
    ranked_dir = tmp_path / "ranked-once"
    proof_bytes = Path("C:/Users/AlbertMeintjes/dev/42-inputs/FREE-RETRIEVAL-RANK-MATRIX-2026-10-01.json").read_bytes()

    with pytest.raises(DemoRunRefused, match=reason):
        run_attempt(
            question_id="NOW-01", attempt_number=1, data_dir=ranked_dir,
            prior_data_dir=prior_dir,
            funding_proof=funding_proof(allocated=allocated, consumed=consumed,
                                       app_run_ids=app_ids[:proof_app_count]),
            provided_context=_ranked_context(), dispatch=lambda *_args: pytest.fail("dispatch called"),
            persist_attempt=persist_attempt, profile=profile,
            ranking_proof_bytes=proof_bytes, source_commit="a" * 40,
        )
    assert not ranked_dir.exists()


def test_ranked_profile_rejects_legacy_markers_as_foreign(tmp_path):
    _write_prior_five(tmp_path)
    with pytest.raises(DemoRunRefused, match="foreign_execution_profile"):
        demo_pairs._existing_receipts(tmp_path, profile=demo_pairs.RANKED_NOW_ONCE_PROFILE)


def test_exact_fractional_cost_rejects_rounded_reservation_overcredit():
    record_sha = "b" * 64
    proof = {
        "verified": True,
        "ask_id": "a_demo_01",
        "run_id": "r_demo_01",
        "record_sha256": record_sha,
        "guarded_charge_micros": 2_024_481,
        "recorded_model_usd": "2.0244805",
        "reservation_release_usd": "2.0244805",
        "recorded_model_usd_ceiling_micros": 2_024_481,
        "reservation_release_ceiling_micros": 2_024_481,
        "native_net_micros": CUMULATIVE_CAP_MICROS,
        "readback": {
            "match": True,
            "run_id": "r_demo_01",
            "record_sha256": record_sha,
            "recorded_model_usd": "2.0244805",
            "reservation_release_usd": "2.0244805",
            "recorded_model_usd_ceiling_micros": 2_024_481,
            "reservation_release_ceiling_micros": 2_024_481,
            "native_net_micros": CUMULATIVE_CAP_MICROS,
        },
    }
    assert demo_pairs._validate_attempt_persistence(proof, 2_024_481)["recorded_model_usd"] == "2.0244805"

    proof["reservation_release_usd"] = "2.024481"
    proof["readback"]["reservation_release_usd"] = "2.024481"
    with pytest.raises(DemoRunRefused, match="attempt_persistence_accounting_mismatch"):
        demo_pairs._validate_attempt_persistence(proof, 2_024_481)

    proof["recorded_model_usd"] = "2.024481"
    proof["reservation_release_usd"] = "2.024481"
    proof["readback"]["recorded_model_usd"] = "2.024481"
    proof["readback"]["reservation_release_usd"] = "2.024481"
    with pytest.raises(DemoRunRefused, match="attempt_persistence_accounting_mismatch"):
        demo_pairs._validate_attempt_persistence(proof, 2_024_480)


def test_insufficient_first_attempt_is_persisted_before_second_without_final_saved_work(tmp_path):
    first_context = context_for()
    seen = []
    final_artifacts = []

    def dispatch(request, budget):
        seen.append(request)
        if request.attempt_number == 2:
            prior = json.loads((tmp_path / "DEMO-01-attempt-1.json").read_text(encoding="utf-8"))
            assert prior["status"] == "insufficient_evidence"
            assert prior["attempt_persistence"]["verified"] is True
            assert request.previous_attempt["attempt_persistence"] == prior["attempt_persistence"]
            assert "dossier_id" not in prior and final_artifacts == []
        spend_once(budget)
        result = dispatch_result(
            budget, run_id=f"ask-{request.attempt_key}",
            answer=None if request.attempt_number == 1 else "second answer",
        )
        if request.attempt_number == 1:
            result["status"] = "insufficient_evidence"
        return result

    first = run_attempt(
        question_id="DEMO-01", attempt_number=1, data_dir=tmp_path,
        funding_proof=funding_proof(), provided_context=first_context, dispatch=dispatch,
        persist_attempt=persist_attempt,
    )
    updated_proof = funding_proof(
        consumed=first["guarded_charge_micros"],
        app_run_ids=[first["attempt_persistence"]["run_id"]])
    second = run_attempt(
        question_id="DEMO-01", attempt_number=2, data_dir=tmp_path,
        funding_proof=updated_proof, provided_context=first_context, dispatch=dispatch,
        persist_attempt=persist_attempt,
    )

    assert len(seen) == 2
    assert first["status"] == "insufficient_evidence"
    assert second["attempt_number"] == 2
    assert final_artifacts == []


def test_unknown_dispatched_cost_is_held_without_automatic_retry(tmp_path):
    calls = []

    def dispatch(_request, budget):
        calls.append("dispatch")
        ticket = budget.reserve_fixed("structured", "offline", budget.cap_micros / 1_000_000, 100)
        budget.mark_dispatched(ticket)
        budget.settle(ticket, unknown=True)
        return {
            "answer": None,
            "status": "unknown_cost",
            "raw_receipt": {"run_id": "ask-DEMO-01-attempt-1", "claim_rows": []},
            "claim_readback": {"match": True, "rows": []},
        }

    receipt = run_attempt(
        question_id="DEMO-01", attempt_number=1, data_dir=tmp_path,
        funding_proof=funding_proof(), provided_context=context_for(), dispatch=dispatch,
        persist_attempt=persist_attempt,
    )
    assert receipt["status"] == "unknown_cost"
    assert receipt["guarded_charge_micros"] == receipt["attempt_cap_micros"]
    assert receipt["attempt_persistence"]["verified"] is True
    assert calls == ["dispatch"]


def test_reusing_an_attempt_marker_never_dispatches_twice(tmp_path):
    calls = []

    def dispatch(_request, budget):
        calls.append("dispatch")
        spend_once(budget)
        return dispatch_result(budget, run_id=f"ask-{_request.attempt_key}")

    run_attempt(
        question_id="DEMO-01", attempt_number=1, data_dir=tmp_path,
        funding_proof=funding_proof(), provided_context=context_for(), dispatch=dispatch,
        persist_attempt=persist_attempt,
    )
    with pytest.raises(DemoRunRefused, match="duplicate_attempt_marker"):
        run_attempt(
            question_id="DEMO-01", attempt_number=1, data_dir=tmp_path,
            funding_proof=funding_proof(), provided_context=context_for(), dispatch=dispatch,
            persist_attempt=persist_attempt,
        )

    assert calls == ["dispatch"]


def _write_recovery_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return payload


def _recovery_case(root):
    key = "DEMO-01-attempt-1"
    raw_path = root / "raw" / f"{key}.json"
    w6_path = root / "data" / "w6" / f"{key}.json"
    funding_path = root / "funding" / "transfer.json"
    outer_path = root / f"{key}.json"
    marker_path = root / f"{key}.attempted"
    recovery_path = root / f"{key}.recovery.json"
    run_id = "r_20261001_115341_66d3df96_5b6f2aa8005f47c3bc029ee3c875436e"
    ask_id = "a_20261001_155bfe7d"
    guarded_micros = 126_665
    model_usd = "0.12635347500000002"
    model_ceiling_micros = 126_354
    record_sha = "a" * 64
    w6_record_sha = "sha256:" + record_sha
    ask_row_sha = "b" * 64
    raw = {
        "schema_version": "r3-demo-attempt-raw-v1",
        "outcome": "COMPLETE",
        "run_id": run_id,
        "app_record": {"ask_id": ask_id},
        "unknown_cost": False,
    }
    raw_bytes = _write_recovery_json(raw_path, raw)
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    readback = {
        "match": True,
        "ask_id": ask_id,
        "run_id": run_id,
        "record_sha256": record_sha,
        "recorded_model_usd": model_usd,
        "reservation_release_usd": model_usd,
        "recorded_model_usd_ceiling_micros": model_ceiling_micros,
        "reservation_release_ceiling_micros": model_ceiling_micros,
        "native_net_micros": CUMULATIVE_CAP_MICROS,
    }
    persistence = {
        "verified": True,
        "ask_id": ask_id,
        "run_id": run_id,
        "record_sha256": record_sha,
        "guarded_charge_micros": guarded_micros,
        "recorded_model_usd": model_usd,
        "reservation_release_usd": model_usd,
        "recorded_model_usd_ceiling_micros": model_ceiling_micros,
        "reservation_release_ceiling_micros": model_ceiling_micros,
        "native_net_micros": CUMULATIVE_CAP_MICROS,
        "readback": readback,
    }
    proof = funding_proof()
    proof["allocated_micros"] = 12_974_288
    proof["reservation_run_id"] = "l3-demo-20261001-transfer-45cac5562534d3ca-current-credit"
    funding_bytes = _write_recovery_json(funding_path, proof)
    funding_sha = hashlib.sha256(funding_bytes).hexdigest()
    w6 = {
        "checks": {
            "persist_existing": {
                "question_id": "DEMO-01",
                "attempt": 1,
                "ask_id": ask_id,
                "run_id": run_id,
                "record_sha256": w6_record_sha,
                "ask_row_sha256": ask_row_sha,
                "stored_ask_row_sha256": ask_row_sha,
                "source_receipt_sha256": "sha256:" + raw_sha,
                "recorded_model_usd": model_usd,
                "reservation_release_usd": model_usd,
                "recorded_model_usd_ceiling_micros": model_ceiling_micros,
                "reservation_release_ceiling_micros": model_ceiling_micros,
                "native_net_micros": CUMULATIVE_CAP_MICROS,
            }
        },
        "rows": [{"run_id": "credit-row"}, {"run_id": run_id}],
    }
    w6_bytes = _write_recovery_json(w6_path, w6)
    w6_sidecar_sha = hashlib.sha256(w6_bytes).hexdigest()
    phase = {
        "verified": True,
        "phase_id": "l3-demo-20261001",
        "run_date": "2026-10-01",
        "native_net_micros": CUMULATIVE_CAP_MICROS,
        "app_run_ids": [run_id],
        "consumed_micros": guarded_micros,
        "ask_run_id": run_id,
        "credit_run_id": "l3-demo-20261001-app-credit",
        "ask_row_sha256": ask_row_sha,
        "credit_row_sha256": "c" * 64,
        "guarded_charge_micros": guarded_micros,
        "reservation_run_id": proof["reservation_run_id"],
        "allocated_micros": proof["allocated_micros"],
        "source_proof_sha": proof["source_proof_sha"],
        "baseline_run_ids": proof["baseline_run_ids"],
    }
    calls = [
        {"status": "charged_known", "charged_usd": "0.126353"},
        {
            "status": "charged_conservative_ceiling",
            "charge_basis": "conservative_embedding_ceiling",
            "charged_usd": "0.000308",
            "ceiling_usd": "0.000308",
        },
    ]
    receipt = {
        "schema_version": "demo-pair-attempt-v1",
        "question_id": "DEMO-01",
        "attempt_number": 1,
        "attempt_key": key,
        "status": "ambiguous",
        "failure_type": "UncertainWriteError",
        "attempt_cap_micros": 1_297_428,
        "guarded_charge_micros": 1_297_428,
        "budget": {
            "cap_micros": 1_297_428,
            "charged_micros": guarded_micros,
            "reserved_micros": 0,
            "stop_reason": None,
            "calls": calls,
        },
        "funding_proof": demo_pairs._validate_funding_proof(proof),
        "dispatch_result": {
            "outcome": raw["outcome"],
            "raw_receipt_path": str(raw_path.relative_to(root)),
            "raw_receipt_sha256": raw_sha,
            "raw_receipt": raw,
        },
    }
    outer_bytes = _write_recovery_json(outer_path, receipt)
    marker_bytes = _write_recovery_json(marker_path, {
        "attempt_key": key,
        "prompt_source_sha256": DEMO_QUESTIONS_SHA256,
    })
    sidecar = {
        "schema_version": "demo-attempt-recovery-v1",
        "attempt_key": key,
        "question_id": "DEMO-01",
        "attempt_number": 1,
        "original_receipt_sha256": hashlib.sha256(outer_bytes).hexdigest(),
        "raw_receipt_sha256": raw_sha,
        "status": raw["outcome"],
        "guarded_charge_micros": guarded_micros,
        "run_id": run_id,
        "model_usd": model_usd,
        "model_usd_ceiling_micros": model_ceiling_micros,
        "readback": readback,
        "attempt_persistence": persistence,
        "w6_sha256": w6_record_sha,
        "w6_sidecar_path": str(w6_path.relative_to(root)),
        "w6_sidecar_sha256": w6_sidecar_sha,
        "funding_proof_path": str(funding_path.relative_to(root)),
        "funding_proof_sha256": funding_sha,
        "funding_proof": proof,
        "native_phase_readback": phase,
    }
    _write_recovery_json(recovery_path, sidecar)
    return {
        "root": root,
        "outer_path": outer_path,
        "outer_bytes": outer_bytes,
        "marker_path": marker_path,
        "marker_bytes": marker_bytes,
        "raw_path": raw_path,
        "raw_bytes": raw_bytes,
        "sidecar_path": recovery_path,
        "sidecar": sidecar,
        "receipt": receipt,
        "funding_path": funding_path,
    }


def test_recovery_overlays_verified_slot_without_changing_source_files(tmp_path):
    case = _recovery_case(tmp_path)

    recovered, = demo_pairs._existing_receipts(case["root"])

    assert recovered["status"] == "COMPLETE"
    assert recovered["guarded_charge_micros"] == 126_665
    assert recovered["attempt_cap_micros"] == 1_297_428
    assert recovered["budget"]["calls"][1]["status"] == "charged_conservative_ceiling"
    assert recovered["budget"]["calls"][1]["ceiling_usd"] == "0.000308"
    assert recovered["native_phase_readback"]["ask_row_sha256"] == "b" * 64
    assert recovered["attempt_persistence"]["record_sha256"] == "a" * 64
    assert "verified" not in case["receipt"]["funding_proof"]
    assert recovered["funding_proof"] == case["receipt"]["funding_proof"]
    assert recovered["failure_type"] is None
    assert case["outer_path"].read_bytes() == case["outer_bytes"]
    assert case["marker_path"].read_bytes() == case["marker_bytes"]
    assert case["raw_path"].read_bytes() == case["raw_bytes"]


def test_recovery_rejects_wrong_raw_hash_unknown_calls_wrong_slot_and_funding(tmp_path):
    cases = [
        ("raw-hash", "attempt_recovery_raw_hash_mismatch"),
        ("unknown-call", "attempt_recovery_calls_unsettled"),
        ("wrong-slot", "attempt_recovery_identity_mismatch"),
        ("funding-hash", "attempt_recovery_funding_file_hash_mismatch"),
        ("funding-unverified", "funding_readback_unverified"),
    ]
    for name, reason in cases:
        case = _recovery_case(tmp_path / name)
        sidecar = case["sidecar"]
        if name == "raw-hash":
            sidecar["raw_receipt_sha256"] = "0" * 64
        elif name == "unknown-call":
            case["receipt"]["budget"]["calls"][0]["status"] = "unknown_charged_ceiling"
            outer_bytes = _write_recovery_json(case["outer_path"], case["receipt"])
            sidecar["original_receipt_sha256"] = hashlib.sha256(outer_bytes).hexdigest()
        elif name == "wrong-slot":
            sidecar["question_id"] = "DEMO-02"
        elif name == "funding-unverified":
            proof = dict(sidecar["funding_proof"])
            proof["verified"] = False
            sidecar["funding_proof"] = proof
            funding_bytes = _write_recovery_json(case["funding_path"], proof)
            sidecar["funding_proof_sha256"] = hashlib.sha256(funding_bytes).hexdigest()
        else:
            case["funding_path"].write_bytes(case["funding_path"].read_bytes() + b" ")
        _write_recovery_json(case["sidecar_path"], sidecar)

        with pytest.raises(DemoRunRefused, match=reason):
            demo_pairs._existing_receipts(case["root"])


def _attempt_two_reconciliation_case(root):
    _recovery_case(root)
    key = "DEMO-01-attempt-2"
    raw_path = root / "raw" / f"{key}.json"
    outer_path = root / f"{key}.json"
    marker_path = root / f"{key}.attempted"
    run_id = "r_20261001_attempt_two"
    ask_id = "a_20261001_attempt_two"
    claims = [
        {"claim_id": "K10", "text": "ten"},
        {"claim_id": "K2", "text": "two"},
        {"claim_id": "K2", "text": "two"},
    ]
    raw = {
        "schema_version": "r3-demo-attempt-raw-v1",
        "outcome": "COMPLETE",
        "run_id": run_id,
        "app_record": {"ask_id": ask_id},
        "unknown_cost": False,
        "claim_rows": claims,
    }
    raw_bytes = _write_recovery_json(raw_path, raw)
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    model_usd = "0.12207314999999999"
    model_ceiling = 122_074
    guarded_micros = 122_076
    record_sha = "d" * 64
    readback = {
        "match": True,
        "run_id": run_id,
        "record_sha256": record_sha,
        "recorded_model_usd": model_usd,
        "reservation_release_usd": model_usd,
        "recorded_model_usd_ceiling_micros": model_ceiling,
        "reservation_release_ceiling_micros": model_ceiling,
        "native_net_micros": CUMULATIVE_CAP_MICROS,
    }
    persistence = {
        "verified": True,
        "ask_id": ask_id,
        "run_id": run_id,
        "record_sha256": record_sha,
        "guarded_charge_micros": guarded_micros,
        "recorded_model_usd": model_usd,
        "reservation_release_usd": model_usd,
        "recorded_model_usd_ceiling_micros": model_ceiling,
        "reservation_release_ceiling_micros": model_ceiling,
        "native_net_micros": CUMULATIVE_CAP_MICROS,
        "readback": readback,
    }
    calls = [
        {
            "phase": "semantic_embedding",
            "status": "charged_conservative_ceiling",
            "charged_usd": "0.000003",
            "ceiling_usd": "0.000003",
        },
        {"phase": "research", "status": "charged_known", "charged_usd": model_usd},
    ]
    dispatch_result = {
        "outcome": "COMPLETE",
        "raw_receipt_path": str(raw_path.relative_to(root)),
        "raw_receipt_sha256": raw_sha,
        "raw_receipt": raw,
        "claim_readback": {"match": True, "rows": [claims[2], claims[0], claims[1]]},
    }
    receipt = {
        "schema_version": "demo-pair-attempt-v1",
        "question_id": "DEMO-01",
        "attempt_number": 2,
        "attempt_key": key,
        "status": "ambiguous",
        "failure_type": "claim_readback_mismatch",
        "attempt_cap_micros": 1_297_428,
        "guarded_charge_micros": 1_297_428,
        "model_usd": model_usd,
        "model_usd_ceiling_micros": model_ceiling,
        "readback": readback,
        "attempt_persistence": persistence,
        "budget": {
            "cap_micros": 1_297_428,
            "charged_micros": guarded_micros,
            "reserved_micros": 0,
            "stop_reason": None,
            "calls": calls,
        },
        "dispatch_result": dispatch_result,
    }
    outer_bytes = _write_recovery_json(outer_path, receipt)
    marker_bytes = _write_recovery_json(marker_path, {
        "attempt_key": key,
        "prompt_source_sha256": DEMO_QUESTIONS_SHA256,
    })
    return {
        "root": root,
        "raw": raw,
        "raw_path": raw_path,
        "raw_bytes": raw_bytes,
        "receipt": receipt,
        "outer_path": outer_path,
        "outer_bytes": outer_bytes,
        "marker_path": marker_path,
        "marker_bytes": marker_bytes,
    }


def test_claim_readback_matches_as_exact_multiset():
    claims = [
        {"claim_id": "K10", "text": "ten"},
        {"claim_id": "K2", "text": "two"},
        {"claim_id": "K2", "text": "two"},
    ]
    result = {
        "raw_receipt": {"claim_rows": claims},
        "claim_readback": {"match": True, "rows": [claims[2], claims[0], claims[1]]},
    }

    assert demo_pairs._claim_readback_matches(result)

    for rows in (
        claims[:-1],
        claims + [{"claim_id": "K3", "text": "extra"}],
        [dict(claim, text="changed") if index == 0 else claim for index, claim in enumerate(claims)],
    ):
        result["claim_readback"]["rows"] = rows
        assert not demo_pairs._claim_readback_matches(result)


def test_attempt_two_reconciles_order_only_readback_in_memory(tmp_path):
    case = _attempt_two_reconciliation_case(tmp_path)

    first, recovered = demo_pairs._existing_receipts(case["root"])

    assert first["status"] == "COMPLETE"
    assert recovered["status"] == "COMPLETE"
    assert recovered["guarded_charge_micros"] == 122_076
    assert recovered["budget"]["calls"][0]["status"] == "charged_conservative_ceiling"
    assert recovered["budget"]["calls"][0]["ceiling_usd"] == "0.000003"
    assert recovered["attempt_persistence"]["verified"] is True
    assert recovered["attempt_persistence"]["native_net_micros"] == CUMULATIVE_CAP_MICROS
    assert recovered["original_receipt_sha256"] == hashlib.sha256(case["outer_bytes"]).hexdigest()
    assert recovered["raw_receipt_sha256"] == hashlib.sha256(case["raw_bytes"]).hexdigest()
    assert recovered["reconciliation_reason"] == "claim_readback_order_only"
    assert recovered["failure_type"] == "claim_readback_mismatch"
    assert case["outer_path"].read_bytes() == case["outer_bytes"]
    assert case["marker_path"].read_bytes() == case["marker_bytes"]
    assert case["raw_path"].read_bytes() == case["raw_bytes"]


@pytest.mark.parametrize("unknown_source", ["raw", "call"])
def test_attempt_two_reconciliation_keeps_unknown_cost_held(tmp_path, unknown_source):
    case = _attempt_two_reconciliation_case(tmp_path)
    if unknown_source == "raw":
        case["raw"]["unknown_cost"] = True
        raw_bytes = _write_recovery_json(case["raw_path"], case["raw"])
        case["receipt"]["dispatch_result"]["raw_receipt_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
        case["receipt"]["dispatch_result"]["raw_receipt"] = case["raw"]
    else:
        case["receipt"]["budget"]["calls"][0]["status"] = "unknown_charged_ceiling"
    _write_recovery_json(case["outer_path"], case["receipt"])

    _, held = demo_pairs._existing_receipts(case["root"])

    assert held["status"] == "ambiguous"
    assert held["guarded_charge_micros"] == 1_297_428
    assert "reconciliation_reason" not in held
