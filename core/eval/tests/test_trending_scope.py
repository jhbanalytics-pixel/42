from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.agent.context import RunContext
from core.agent.tools.warehouse import _store
from core.eval import demo_dispatch, demo_operator, demo_pairs, demo_saved_work
from core.eval.demo_scope import _DEMO_SOURCES, _TREND_MARKETS, assess_answer


SAST = timezone(timedelta(hours=2))
WINDOW = {"window_start": "2026-10-01", "window_end": "2026-10-01"}


def _receipt(market="ZA", *, text=None, evidence_market=None, number=None):
    question_id = {"ZA": "TREND-ZA-01", "NG": "TREND-NG-01", "KE": "TREND-KE-01"}[market]
    rows = [
        _stored_evidence(market, 1, "The caption asks visitors to bring a reusable cup.",
                         evidence_market=evidence_market),
        _stored_evidence(market, 2, "The post invites people to share a local walking route.",
                         evidence_market=evidence_market),
    ]
    claims = [
        {"id": "claim-1", "label": "observed",
         "text": text or "The caption asks visitors to bring a reusable cup.",
         "evidence_ids": [rows[0]["id"]],
         "quotes": [{"evidence_id": rows[0]["id"], "text": rows[0]["text"]}],
         "numbers": []},
        {"id": "claim-2", "label": "observed",
         "text": "The post invites people to share a local walking route.",
         "evidence_ids": [rows[1]["id"]],
         "quotes": [{"evidence_id": rows[1]["id"], "text": rows[1]["text"]}],
         "numbers": []},
    ]
    if number is not None:
        claims[0]["text"] = number
    answer = {"status": "complete", "short_answer": claims[0]["text"],
              "claims": claims, "evidence": rows, "so_what": []}
    return {
        "question_id": question_id,
        "markets": [market],
        "context_provenance": {"market": market, "source_ids": []},
        "context": {**WINDOW, "market": market, "evidence": rows, "queries": {}},
        "raw_answer": answer,
    }


def _stored_evidence(market, index, text, *, evidence_market=None,
                     include_geo=True, include_source_sighting=True):
    source_market = evidence_market or market
    context = RunContext(
        run_id="trend-scope-test", tier="T1",
        as_of=datetime(2026, 10, 1, 12, tzinfo=SAST), market=market,
        window_start=date(2026, 10, 1), window_end=date(2026, 10, 1),
    )
    post_id = f"post-{market}-{index}"
    _store(context, {
        "post_id": post_id, "platform": "tiktok", "url": f"https://example.org/{post_id}",
        "creator_id": f"creator-{index}", "handle": f"@creator{index}",
        "published_at": f"2026-10-01T{9 + index}:00:00+02:00", "post_date": "2026-10-01",
        "geo_market": source_market if include_geo else None,
        "geo_source": "coordinates" if include_geo else None,
        "text": text,
        "source_sightings": ([{"source_market": source_market}] if include_source_sighting else []),
    })
    return context.evidence[post_id]


def test_trend_ids_are_explicit_market_bound_and_have_no_frozen_source_anchors():
    assert dict(_TREND_MARKETS) == {
        "TREND-ZA-01": "ZA", "TREND-NG-01": "NG", "TREND-KE-01": "KE",
    }
    assert not set(_TREND_MARKETS).intersection(_DEMO_SOURCES)
    stored = _receipt("ZA")["context"]["evidence"][0]
    assert stored["market"] == "ZA" and stored["source_market"] == "ZA"
    assert stored["flags"] == [] and "source_sightings" not in stored


def test_two_receipt_supported_claims_are_admitted_for_bound_market():
    result = assess_answer(_receipt("ZA"), "TREND-ZA-01")

    assert result["admitted_claim_ids"] == ["claim-1", "claim-2"]
    assert result["safe"] is True
    assert result["dropped"] == []


def test_trend_claim_can_use_four_receipt_supported_posts():
    receipt = _receipt("ZA")
    evidence = receipt["context"]["evidence"]
    claims = receipt["raw_answer"]["claims"]
    for index in (3, 4):
        row = _stored_evidence("ZA", index, f"Another source notes theme {index}.")
        evidence.append(row)
        claims[0]["evidence_ids"].append(row["id"])
        claims[0]["quotes"].append({"evidence_id": row["id"], "text": row["text"]})
    receipt["raw_answer"]["evidence"] = evidence

    result = assess_answer(receipt, "TREND-ZA-01")

    assert result["admitted_claim_ids"] == ["claim-1", "claim-2"]


def test_trend_claims_from_another_market_are_dropped():
    result = assess_answer(_receipt("ZA", evidence_market="NG"), "TREND-ZA-01")

    assert result["admitted_claim_ids"] == []
    assert any("market" in item["reason"] for item in result["dropped"])


@pytest.mark.parametrize("include_source_sighting", [False, True])
def test_assumed_market_records_do_not_qualify_as_located_trend_evidence(include_source_sighting):
    receipt = _receipt("ZA")
    text = receipt["raw_answer"]["claims"][0]["quotes"][0]["text"]
    assumed = _stored_evidence(
        "ZA", 1, text, include_geo=False, include_source_sighting=include_source_sighting,
    )
    receipt["context"]["evidence"][0] = assumed
    assert assumed["market"] == "ZA"
    assert assumed["flags"] == ["market_assumed"]
    assert (assumed["source_market"] == "ZA") is include_source_sighting

    result = assess_answer(receipt, "TREND-ZA-01")

    assert result["admitted_claim_ids"] == ["claim-2"]
    assert any(item["claim_id"] == "claim-1" and "direct market evidence" in item["reason"]
               for item in result["dropped"])


def test_trend_evidence_with_missing_store_flags_fails_closed():
    receipt = _receipt("KE")
    del receipt["context"]["evidence"][0]["flags"]

    result = assess_answer(receipt, "TREND-KE-01")

    assert result["admitted_claim_ids"] == ["claim-2"]
    assert any(item["claim_id"] == "claim-1" for item in result["dropped"])


def test_national_dominance_claim_is_still_dropped():
    result = assess_answer(
        _receipt("ZA", text="This topic dominates South African social media nationwide."),
        "TREND-ZA-01",
    )

    assert result["admitted_claim_ids"] == ["claim-2"]
    assert any(item["claim_id"] == "claim-1" and "breadth" in item["reason"]
               for item in result["dropped"])


def test_unreceipted_numeric_claim_is_still_dropped():
    result = assess_answer(
        _receipt("KE", number="The post received 1,234 views."), "TREND-KE-01",
    )

    assert result["admitted_claim_ids"] == ["claim-2"]
    assert any(item["claim_id"] == "claim-1" and "numbers" in item["reason"]
               for item in result["dropped"])


def test_production_run_attempt_persist_callback_uses_trend_scope(tmp_path, monkeypatch):
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    prior_dir = tmp_path / "prior"
    artifact_dir = tmp_path / "trending"
    prior = [
        {"question_id": f"DEMO-{index:02}", "attempt_number": 1, "status": "complete",
         "guarded_charge_micros": amount,
         "attempt_persistence": {"verified": True, "run_id": f"prior-{index}"}}
        for index, amount in enumerate((100_000, 100_000, 100_000, 100_000, 26_564), 1)
    ]
    existing = demo_pairs._existing_receipts
    monkeypatch.setattr(demo_pairs, "_now_sast", lambda: datetime(2026, 10, 1, 12, tzinfo=SAST))
    monkeypatch.setattr(
        demo_pairs, "_existing_receipts",
        lambda path, profile=None: prior if Path(path) == prior_dir else existing(path, profile=profile),
    )
    monkeypatch.setattr(demo_operator, "REQUEST_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(demo_dispatch, "dispatch_attempt", lambda request, budget, **_kwargs: _dispatch(request, budget))
    admitted = {}

    def persist_attempt_view(**kwargs):
        admitted["ids"] = list(kwargs["admitted_claim_ids"])
        run_id = "run-za"
        ask_id = "ask-za"
        record_hash = "e" * 64
        row_hash = "f" * 64
        return {
            "status": "stored", "ask_id": ask_id, "run_id": run_id,
            "record_sha256": "sha256:" + record_hash,
            "ask_row_sha256": row_hash, "stored_ask_row_sha256": row_hash,
            "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
            "recorded_model_usd_ceiling_micros": 500_000,
            "reservation_release_ceiling_micros": 500_000,
            "native_net_micros": demo_pairs.CUMULATIVE_CAP_MICROS,
            "readback": {"run_id": run_id, "ask_id": ask_id,
                         "recorded_model_usd": "0.5", "reservation_release_usd": "0.5",
                         "recorded_model_usd_ceiling_micros": 500_000,
                         "reservation_release_ceiling_micros": 500_000},
        }

    monkeypatch.setattr(demo_saved_work, "persist_attempt_view", persist_attempt_view)
    runtime = object.__new__(demo_operator._ProductionRuntime)
    runtime.data_dir = tmp_path / "production-data"
    runtime.modules = SimpleNamespace()
    runtime.wiring = SimpleNamespace()
    runtime.native = SimpleNamespace()
    runtime.frozen_ask_ids = set()
    question = profile.question_catalog[0]
    receipt = runtime.run_attempt(
        question, 1,
        {"schema_version": demo_pairs.DEMO_FUNDING_SCHEMA, "phase_id": demo_pairs.DEMO_PHASE_ID,
         "verified": True, "run_date": "2026-10-01", "reservation_run_id": "funding",
         "allocated_micros": 12_974_288, "consumed_micros": 426_564,
         "source_proof_sha": "a" * 64, "native_net_micros": demo_pairs.CUMULATIVE_CAP_MICROS,
         "baseline_run_ids": list(demo_pairs.BASELINE_RUN_IDS),
         "app_run_ids": [row["attempt_persistence"]["run_id"] for row in prior]},
        {"market": "ZA", "source_ids": [], "posts": [], "support_run_id": None},
        profile=profile, attempt_data_dir=artifact_dir, prior_data_dir=prior_dir,
        ranking_proof_bytes=demo_pairs.TRENDING_FALLBACK_PROFILE_PROOF_BYTES,
        source_commit="d" * 40,
    )

    assert receipt["status"] == "COMPLETE"
    assert admitted["ids"] == ["claim-1", "claim-2"]


def _dispatch(request, budget):
    ticket = budget.reserve_fixed("structured", "offline", 1.0, 100)
    budget.mark_dispatched(ticket)
    budget.settle(ticket, actual_usd=0.5)
    bound = _receipt(request.market)
    return {
        **bound,
        "id": request.question_id,
        "run_id": "run-za",
        "outcome": "COMPLETE",
        "claim_rows": [{"claim_id": "claim-1"}],
        "claim_readback": {"match": True, "rows": [{"claim_id": "claim-1"}]},
        "spend_writes": [],
    }
