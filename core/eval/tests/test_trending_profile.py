from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from core.eval import ask_r2, demo_pairs
from core.eval.demo_pairs import DemoRunRefused


SAST = timezone(timedelta(hours=2))
PRIOR_AMOUNTS = (100_000, 100_000, 100_000, 100_000, 26_564)


def _prior_receipts():
    return [
        {"question_id": f"DEMO-{index:02}", "attempt_number": 1,
         "status": "complete", "guarded_charge_micros": amount,
         "attempt_persistence": {"verified": True, "run_id": f"prior-{index}"}}
        for index, amount in enumerate(PRIOR_AMOUNTS, 1)
    ]


def _funding(consumed, app_run_ids, allocated=12_974_288):
    return {
        "schema_version": demo_pairs.DEMO_FUNDING_SCHEMA,
        "phase_id": demo_pairs.DEMO_PHASE_ID,
        "verified": True,
        "run_date": "2026-10-01",
        "reservation_run_id": "existing-reservation",
        "allocated_micros": allocated,
        "consumed_micros": consumed,
        "source_proof_sha": "a" * 64,
        "native_net_micros": demo_pairs.CUMULATIVE_CAP_MICROS,
        "baseline_run_ids": list(demo_pairs.BASELINE_RUN_IDS),
        "app_run_ids": list(app_run_ids),
    }


def _adapters(calls):
    def dispatch(request, budget):
        calls.append((request, budget))
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
            "reservation_release_ceiling_micros": 500_000,
            "native_net_micros": demo_pairs.CUMULATIVE_CAP_MICROS,
            "readback": {"match": True, "run_id": run_id, "record_sha256": record_hash,
                         "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
                         "recorded_model_usd_ceiling_micros": 500_000,
                         "reservation_release_ceiling_micros": 500_000,
                         "native_net_micros": demo_pairs.CUMULATIVE_CAP_MICROS},
        }

    return dispatch, persist


def _run(question, artifact_dir, prior_dir, funding, adapters, *, profile=None):
    selected = profile or demo_pairs.TRENDING_FALLBACK_PROFILE
    return demo_pairs.run_attempt(
        question_id=question.id, attempt_number=1, data_dir=artifact_dir,
        prior_data_dir=prior_dir, funding_proof=funding,
        provided_context={"market": question.market, "source_ids": [], "posts": []},
        dispatch=adapters[0], persist_attempt=adapters[1], profile=selected,
        ranking_proof_bytes=demo_pairs.TRENDING_FALLBACK_PROFILE_PROOF_BYTES,
        source_commit="d" * 40,
        clock=lambda: datetime(2026, 10, 1, 12, tzinfo=SAST),
    )


@pytest.fixture
def profile_paths(tmp_path, monkeypatch):
    prior = _prior_receipts()
    prior_dir = tmp_path / "prior"
    artifact_dir = tmp_path / "trending"
    original = demo_pairs._existing_receipts

    def receipts(path, *, profile=None):
        if Path(path) == prior_dir:
            return prior
        return original(path, profile=profile)

    monkeypatch.setattr(demo_pairs, "_existing_receipts", receipts)
    return prior_dir, artifact_dir, prior


def test_trending_profile_is_exact_and_leaves_ranked_now_singleton_unchanged():
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
    assert demo_pairs._execution_profile(demo_pairs.RANKED_NOW_ONCE_PROFILE) is demo_pairs.RANKED_NOW_ONCE_PROFILE
    with pytest.raises(DemoRunRefused, match="execution_profile_invalid"):
        demo_pairs._execution_profile(replace(profile, authorization_slots=2))


def test_run_attempt_consumes_only_next_market_slot_and_keeps_each_cap(tmp_path, profile_paths):
    prior_dir, artifact_dir, prior = profile_paths
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    calls = []
    adapters = _adapters(calls)
    app_ids = [item["attempt_persistence"]["run_id"] for item in prior]
    consumed = profile.prior_guarded_micros

    for question in profile.question_catalog:
        receipt = _run(question, artifact_dir, prior_dir, _funding(consumed, app_ids), adapters)
        app_ids.append(receipt["attempt_persistence"]["run_id"])
        consumed += receipt["guarded_charge_micros"]
        assert receipt["attempt_cap_micros"] == 3_000_000
        assert receipt["execution_profile_id"] == profile.profile_id

    assert [request.prompt for request, _ in calls] == [question.text for question in profile.question_catalog]
    assert [request.market for request, _ in calls] == ["ZA", "NG", "KE"]
    assert all(isinstance(budget, ask_r2.SessionBudget) for _, budget in calls)
    assert all(budget.cap_micros == 3_000_000 for _, budget in calls)
    assert all(budget.provider_500_retries == 0 and budget.provider_retry_statuses == ()
               for _, budget in calls)
    with pytest.raises(DemoRunRefused, match="duplicate_attempt_marker"):
        _run(profile.question_catalog[0], artifact_dir, prior_dir,
             _funding(consumed, app_ids), adapters)
    assert len(calls) == 3


def test_run_attempt_refuses_skips_and_insufficient_budget_before_dispatch(tmp_path, profile_paths):
    prior_dir, artifact_dir, prior = profile_paths
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    calls = []
    adapters = _adapters(calls)
    prior_ids = [item["attempt_persistence"]["run_id"] for item in prior]

    with pytest.raises(DemoRunRefused, match="ranked_attempt_not_authorized"):
        _run(profile.question_catalog[1], artifact_dir, prior_dir,
             _funding(profile.prior_guarded_micros, prior_ids), adapters)
    with pytest.raises(DemoRunRefused, match="attempt_cap_exhausted"):
        _run(profile.question_catalog[0], artifact_dir, prior_dir,
             _funding(profile.prior_guarded_micros, prior_ids, allocated=3_426_563), adapters)
    assert calls == []
    assert not artifact_dir.exists()

