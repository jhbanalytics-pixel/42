from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from core.eval import demo_pairs, demo_operator
from core.eval.demo_operator import DATA, INPUTS, ROOT, OperatorRefused, production_runtime_factory


PRODUCT_FIX_COMMIT = "ae37fd14a15b2ead020a75939e3ad21d4a19d53d"
PREVIOUS_LOCAL_SOURCE_COMMIT = "c862a8adcab9ca6de94e040f31bb4dce5b8a8ad9"
PREVIOUS_LOCAL_NG_SOURCE_COMMIT = demo_pairs.TRENDING_SETTLED_NG_SOURCE_COMMIT
PREVIOUS_LOCAL_ZA_ARTIFACT_HASHES = {
    "TREND-ZA-01-attempt-1.json": "1987a8a4bc037c37d9e7a02066da82c08e08bc3cf5a8a2ed71b3238e7ac0cc4b",
    "raw/TREND-ZA-01-attempt-1.json": "7d8dbbdfd08803775a546a95dee072b488b0a9c4e8743b23322f0166586501ce",
    "w6/TREND-ZA-01-attempt-1.json": "668f06f9e7cf221dbdd60fd536f99ccb9271ba4469bb26fc9c5871aa5d7e9768",
    "TREND-ZA-01-attempt-1.attempted": "9059230529634cab6a55597a1a63fbd7dcb76c5bb2bddcf689a96fc2cf039ead",
}
PREVIOUS_LOCAL_NG_ARTIFACT_HASHES = {
    "TREND-NG-01-attempt-1.json": "bdbb0f4c91577dd68bcad8ba4419a30e0edb44dcbac7d42bdc8d2d0bd25ad7dc",
    "raw/TREND-NG-01-attempt-1.json": "e6530e31d114b19f4cc15eeffd86c52047272e161b20ccfa06cfcd036229b26a",
    "w6/TREND-NG-01-attempt-1.json": "2b441109184ceca31c3a895c4a09e7617e933205519d403ac5d0481469505422",
    "TREND-NG-01-attempt-1.attempted": "7f78aab36d77f12ce2226a89d1667185e0dfb11c3a4c7df21e4fd8b4c2290e59",
}
TRENDING_DATA = DATA / "trending-fallback-local-stored-2026-10-01"
EXPECTED_ALLOCATION_MICROS = 12_974_288


def _canonical_bytes(value):
    return demo_operator._canonical_bytes(value)


def _blocked(reason, **details):
    return {"status": "blocked", "stop_reason": reason, **details}


def _verify_source_ancestor(repo_root, ancestor_commit, descendant_commit, reason):
    try:
        from core.eval.ask_r2 import _git_executable

        result = subprocess.run(
            [_git_executable(), "merge-base", "--is-ancestor", ancestor_commit, descendant_commit],
            cwd=repo_root, capture_output=True, timeout=demo_operator.REQUEST_TIMEOUT_SECONDS,
        )
    except Exception:
        raise OperatorRefused(reason) from None
    if result.returncode != 0:
        raise OperatorRefused(reason)


def _verify_product_fix_source(repo_root, source_commit):
    _verify_source_ancestor(repo_root, PRODUCT_FIX_COMMIT, source_commit,
                            "product_fix_source_ancestry_unverified")


def _verify_previous_local_za_snapshot(artifact_dir):
    for relative_path, expected_sha256 in PREVIOUS_LOCAL_ZA_ARTIFACT_HASHES.items():
        try:
            actual_sha256 = hashlib.sha256((Path(artifact_dir) / relative_path).read_bytes()).hexdigest()
        except OSError:
            raise OperatorRefused("trending_prior_za_snapshot_missing") from None
        if actual_sha256 != expected_sha256:
            raise OperatorRefused("trending_prior_za_snapshot_changed")


def _verify_previous_local_ng_snapshot(artifact_dir):
    for relative_path, expected_sha256 in PREVIOUS_LOCAL_NG_ARTIFACT_HASHES.items():
        try:
            actual_sha256 = hashlib.sha256((Path(artifact_dir) / relative_path).read_bytes()).hexdigest()
        except OSError:
            raise OperatorRefused("trending_prior_ng_snapshot_missing") from None
        if actual_sha256 != expected_sha256:
            raise OperatorRefused("trending_prior_ng_snapshot_changed")


def _safe_reason(exc, fallback):
    reason = exc.args[0] if getattr(exc, "args", None) else None
    if isinstance(reason, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", reason):
        return reason
    return f"{fallback}:{type(exc).__name__}"


def _write_if_same(path, content):
    path = Path(path)
    if path.exists():
        if path.read_bytes() != content:
            raise OperatorRefused("trending_saved_artifact_changed")
        return path
    try:
        return demo_operator._write_exclusive(path, content)
    except FileExistsError:
        if path.read_bytes() != content:
            raise OperatorRefused("trending_saved_artifact_changed") from None
        return path


def _read_funding(path):
    funding = demo_operator._read_object(path, "existing_funding_proof_invalid")
    if (funding.get("verified") is not True
            or funding.get("run_date") != demo_pairs.DEMO_RUN_DATE.isoformat()
            or funding.get("allocated_micros") != EXPECTED_ALLOCATION_MICROS
            or funding.get("consumed_micros") != 0
            or funding.get("app_run_ids") != []
            or funding.get("native_net_micros") != demo_pairs.CUMULATIVE_CAP_MICROS
            or funding.get("baseline_run_ids") != list(demo_pairs.BASELINE_RUN_IDS)):
        raise OperatorRefused("existing_funding_proof_scope_mismatch")
    return funding


def _verify_funding_readback(runtime, funding, prior_receipts, trending_receipts, profile):
    attempts = [*prior_receipts, *trending_receipts]
    current = runtime.refresh_funding(funding, attempts)
    prior_ids = [row["attempt_persistence"]["run_id"] for row in prior_receipts]
    trending_ids = [row["attempt_persistence"]["run_id"] for row in trending_receipts]
    expected_consumed = profile.prior_guarded_micros + sum(
        row["guarded_charge_micros"] for row in trending_receipts)
    if (not isinstance(current, dict) or current.get("verified") is not True
            or current.get("allocated_micros") != EXPECTED_ALLOCATION_MICROS
            or current.get("consumed_micros") != expected_consumed
            or current.get("app_run_ids") != [*prior_ids, *trending_ids]
            or current.get("native_net_micros") != demo_pairs.CUMULATIVE_CAP_MICROS):
        raise OperatorRefused("prior_funding_readback_mismatch")
    return current


def _verify_daily_headroom(runtime, day, required_micros):
    daily = runtime.fresh_daily_readback(day)
    if (not isinstance(daily, dict) or daily.get("verified") is not True
            or daily.get("all_pages_consumed") is not True
            or daily.get("run_date") != day.isoformat()
            or type(daily.get("daily_cap_micros")) is not int
            or type(daily.get("canonical_total_micros")) is not int
            or daily["daily_cap_micros"] - daily["canonical_total_micros"] < required_micros):
        raise OperatorRefused("trending_daily_headroom_unverified")
    return daily


def _claim_evidence(raw, saved_claim_ids):
    answer = raw.get("raw_answer") if isinstance(raw, dict) else None
    claims = answer.get("claims") if isinstance(answer, dict) else None
    evidence = answer.get("evidence") if isinstance(answer, dict) else None
    if not isinstance(claims, list) or not isinstance(evidence, list):
        return [], []
    by_id = {}
    for claim in claims:
        if isinstance(claim, dict) and isinstance(claim.get("id"), str):
            by_id.setdefault(claim["id"], []).append(claim)
    evidence_ids = {item.get("id") for item in evidence
                    if isinstance(item, dict) and isinstance(item.get("id"), str)}
    admitted = []
    cited = []
    for claim_id in saved_claim_ids:
        matching = by_id.get(claim_id, [])
        claim = matching[0] if len(matching) == 1 else None
        refs = claim.get("evidence_ids") if isinstance(claim, dict) else None
        if not isinstance(refs, list) or not refs or any(item not in evidence_ids for item in refs):
            return [], []
        admitted.append(claim_id)
        cited.extend(refs)
    return admitted, sorted(set(cited))


def _saved_ask_readback(artifact_dir, question, receipt, raw):
    attempt_key = demo_pairs._attempt_key(question.id, 1)
    saved = demo_operator._read_object(artifact_dir / "w6" / f"{attempt_key}.json", "saved_ask_receipt_missing")
    persistence = receipt.get("attempt_persistence") or {}
    w5_readback = persistence.get("readback") or {}
    w6_readback = saved.get("readback") or {}
    record_sha256 = persistence.get("record_sha256")
    row_sha256 = saved.get("ask_row_sha256")
    accounting_fields = (
        "recorded_model_usd", "reservation_release_usd",
        "recorded_model_usd_ceiling_micros", "reservation_release_ceiling_micros",
    )
    if (saved.get("status") not in {"stored", "resumed"}
            or saved.get("ask_id") != persistence.get("ask_id")
            or saved.get("run_id") != persistence.get("run_id")
            or not isinstance(record_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", record_sha256)
            or saved.get("record_sha256") != f"sha256:{record_sha256}"
            or not isinstance(row_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", row_sha256)
            or row_sha256 != saved.get("stored_ask_row_sha256")
            or w5_readback.get("match") is not True
            or w5_readback.get("run_id") != saved.get("run_id")
            or w5_readback.get("record_sha256") != record_sha256
            or w6_readback.get("ask_id") != saved.get("ask_id")
            or w6_readback.get("run_id") != saved.get("run_id")
            or saved.get("native_net_micros") != persistence.get("native_net_micros")
            or w5_readback.get("native_net_micros") != persistence.get("native_net_micros")
            or any(persistence.get(key) != w5_readback.get(key)
                   or saved.get(key) != w6_readback.get(key)
                   or persistence.get(key) != saved.get(key) for key in accounting_fields)):
        raise OperatorRefused("saved_ask_readback_mismatch")
    app_record = raw.get("app_record") if isinstance(raw, dict) else None
    record_answer = app_record.get("answer") if isinstance(app_record, dict) else None
    raw_status = record_answer.get("status") if isinstance(record_answer, dict) else None
    raw_status = raw_status.lower() if isinstance(raw_status, str) else ""
    saved_claim_ids = saved.get("scope_admitted_claim_ids")
    if (not isinstance(saved_claim_ids, list)
            or any(not isinstance(item, str) or not item for item in saved_claim_ids)
            or len(saved_claim_ids) != len(set(saved_claim_ids))):
        raise OperatorRefused("saved_claim_ids_invalid")
    claim_ids, evidence_ids = _claim_evidence(raw, saved_claim_ids)
    saved_status = saved.get("saved_view_status")
    saved_status = saved_status.lower() if isinstance(saved_status, str) else ""
    qualifying = (raw_status in {"complete", "partial"}
                  and saved_status in {"complete", "partial"}
                  and len(claim_ids) >= 2 and bool(evidence_ids))
    outcome = saved_status if qualifying else "insufficient_evidence"
    return (saved, app_record if isinstance(app_record, dict) else {}, claim_ids, evidence_ids,
            outcome, raw_status, qualifying)


def _l5_handoff(question, receipt, raw, saved, claim_ids, evidence_ids, source, runtime_health_version,
                raw_answer_status):
    persistence = receipt["attempt_persistence"]
    app_record = raw.get("app_record") if isinstance(raw.get("app_record"), dict) else {}
    version = app_record.get("version")
    if not isinstance(version, str) or not version.strip():
        version = None
    handoff = {
        "schema_version": "l3-trending-fallback-l5-handoff-v1",
        "status": "pending_l5",
        "saved_view_status": saved.get("saved_view_status"),
        "raw_answer_status": raw_answer_status,
        "question_id": question.id,
        "market": question.market,
        "prompt": question.text,
        "ask_id": persistence["ask_id"],
        "object_id": persistence["ask_id"],
        "object_type": "ask",
        "version": version,
        "runtime_health_version": runtime_health_version,
        "run_id": persistence["run_id"],
        "record_sha256": saved["record_sha256"],
        "claim_ids": claim_ids,
        "evidence_ids": evidence_ids,
        "source_commit": receipt.get("source_commit"),
        "source_readback": persistence["readback"],
        "accepted_ids": [],
    }
    return handoff


def _verified_existing_attempts(artifact_dir, profile, source_commit, repo_root):
    artifact_dir = Path(artifact_dir)
    try:
        receipts = demo_pairs._existing_receipts(artifact_dir, profile=profile)
    except Exception as exc:
        reason = _safe_reason(exc, "receipt_check_failed")
        raise OperatorRefused(f"trending_existing_receipts_unverified:{reason}") from None

    expected_keys = set()
    for index, question in enumerate(profile.question_catalog[:len(receipts)]):
        receipt = receipts[index]
        attempt_key = demo_pairs._attempt_key(question.id, 1)
        marker = demo_operator._read_object(artifact_dir / f"{attempt_key}.attempted",
                                            "trending_attempt_marker_invalid")
        persistence = receipt.get("attempt_persistence") or {}
        guarded = receipt.get("guarded_charge_micros")
        receipt_source = receipt.get("source_commit")
        if receipt_source == PREVIOUS_LOCAL_SOURCE_COMMIT:
            if index != 0 or question.id != "TREND-ZA-01":
                raise OperatorRefused("trending_prior_source_slot_mismatch")
            _verify_previous_local_za_snapshot(artifact_dir)
            _verify_source_ancestor(repo_root, PREVIOUS_LOCAL_SOURCE_COMMIT, source_commit,
                                    "trending_prior_source_ancestry_unverified")
        elif receipt_source == PREVIOUS_LOCAL_NG_SOURCE_COMMIT:
            if index != 1 or question.id != "TREND-NG-01":
                raise OperatorRefused("trending_prior_source_slot_mismatch")
            _verify_previous_local_ng_snapshot(artifact_dir)
            _verify_source_ancestor(repo_root, PREVIOUS_LOCAL_NG_SOURCE_COMMIT, source_commit,
                                    "trending_prior_source_ancestry_unverified")
            if not demo_pairs._settled_trending_ng_unknown_cost_receipt_is_verified(receipt, profile):
                raise OperatorRefused("trending_prior_ng_unknown_cost_unsettled")
        elif receipt_source != source_commit:
            raise OperatorRefused("trending_existing_receipt_source_mismatch")
        if (receipt.get("question_id") != question.id or receipt.get("attempt_number") != 1
                or receipt.get("attempt_key") != attempt_key
                or receipt.get("execution_profile_id") != profile.profile_id
                or receipt.get("source_proof_sha256") != profile.source_proof_sha256
                or receipt.get("source_go_profile_id") != profile.source_go_profile_id
                or receipt.get("authorization_slots") != profile.authorization_slots
                or receipt.get("attempt_cap_micros") != profile.attempt_cap_micros
                or not demo_pairs._profile_receipt_is_verified(receipt, profile)
                or type(guarded) is not int or not 0 < guarded <= profile.attempt_cap_micros
                or persistence.get("guarded_charge_micros") != guarded
                or marker.get("source_commit") != receipt_source):
            raise OperatorRefused("trending_existing_receipt_scope_mismatch")
        expected_keys.add(attempt_key)

    for folder in ("raw", "w6"):
        path = artifact_dir / folder
        if path.exists() and any(item.stem not in expected_keys for item in path.glob("*.json")):
            raise OperatorRefused("trending_orphan_attempt_artifact")
    for path in artifact_dir.glob("*-attempt-*.cost.json"):
        if path.name.removesuffix(".cost.json") not in expected_keys:
            raise OperatorRefused("trending_orphan_attempt_artifact")
    return receipts


def _run_trending_replay(runtime_factory=None, *, execute=False, data_dir=None, inputs_dir=None,
                         artifact_dir=None):
    profile = demo_pairs.TRENDING_FALLBACK_PROFILE
    if (profile.attempt_sequence != tuple((q.id, 1) for q in profile.question_catalog)
            or profile.authorization_slots != 3 or profile.attempt_cap_micros != 3_000_000):
        raise OperatorRefused("trending_profile_contract_invalid")

    data_dir = Path(DATA if data_dir is None else data_dir)
    inputs_dir = Path(INPUTS if inputs_dir is None else inputs_dir)
    if artifact_dir is None:
        artifact_dir = TRENDING_DATA if data_dir.resolve() == DATA.resolve() else data_dir / TRENDING_DATA.name
    artifact_dir = Path(artifact_dir)
    factory = runtime_factory or production_runtime_factory
    runtime = factory(repo_root=ROOT, data_dir=data_dir, inputs_dir=inputs_dir)
    source = runtime._verify_committed_sources()
    if (not isinstance(source, dict) or source.get("clean") is not True
            or not isinstance(source.get("head"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", source["head"])):
        return _blocked("trending_source_commit_unverified")
    try:
        _verify_product_fix_source(getattr(runtime, "repo_root", ROOT), source["head"])
    except OperatorRefused as exc:
        return _blocked(_safe_reason(exc, "product_fix_source_unverified"))

    runtime._load()

    health = getattr(runtime, "health_proof", None)
    if not isinstance(health, dict) or health.get("http_status") != 200 or health.get("ok") is not True:
        return _blocked("local_runtime_health_unverified")
    runtime_health_version = health.get("version")
    if not isinstance(runtime_health_version, str) or not runtime_health_version.strip():
        runtime_health_version = None

    day = runtime.wiring.now().astimezone(demo_pairs.SAST).date()
    if day != demo_pairs.DEMO_RUN_DATE:
        return _blocked("funding_run_date_mismatch", expected=demo_pairs.DEMO_RUN_DATE.isoformat(),
                        observed=day.isoformat(), local_evaluator_source_commit=source["head"],
                        runtime_health_version=runtime_health_version)

    funding = _read_funding(data_dir / "funding" / "transfer.json")
    _, base_consumed, _ = runtime.native._phase_readback(funding)
    if base_consumed != 0:
        raise OperatorRefused("existing_funding_phase_not_unspent")
    from core.eval import ranked_demo

    prior_receipts = ranked_demo._verified_prior_attempts(demo_pairs._existing_receipts(data_dir))
    try:
        existing_attempts = _verified_existing_attempts(
            artifact_dir, profile, source["head"], getattr(runtime, "repo_root", ROOT))
    except OperatorRefused as exc:
        return {"mode": "execute" if execute else "prepare", "status": "stopped",
                "stop_reason": _safe_reason(exc, "trending_existing_receipts_unverified"),
                "local_evaluator_source_commit": source["head"],
                "runtime_health_version": runtime_health_version,
                "artifact_dir": str(artifact_dir), "markets": []}
    current = _verify_funding_readback(runtime, funding, prior_receipts, existing_attempts, profile)
    pending_slots = profile.authorization_slots - len(existing_attempts)
    required_headroom = profile.attempt_cap_micros * pending_slots
    daily = _verify_daily_headroom(runtime, day, required_headroom)
    remaining = current["allocated_micros"] - current["consumed_micros"]
    if remaining < required_headroom:
        raise OperatorRefused("trending_pool_headroom_unverified")

    result = {
        "mode": "execute" if execute else "prepare",
        "status": "prepared",
        "profile_id": profile.profile_id,
        "attempts": [{"question_id": q.id, "market": q.market, "prompt": q.text, "attempt": 1}
                     for q in profile.question_catalog],
        "attempt_cap_micros": profile.attempt_cap_micros,
        "remaining_micros": remaining,
        "prior_guarded_micros": profile.prior_guarded_micros,
        "already_consumed_trending_micros": sum(
            item["guarded_charge_micros"] for item in existing_attempts),
        "native_net_micros": current["native_net_micros"],
        "daily_headroom_micros": daily["daily_cap_micros"] - daily["canonical_total_micros"],
        "model_provider": "gemini",
        "runtime_health_http_status": health["http_status"],
        "runtime_health_version": runtime_health_version,
        "local_evaluator_source_commit": source["head"],
        "local_evaluator_source_hashes": source.get("hashes"),
        "artifact_dir": str(artifact_dir),
        "existing_attempts": len(existing_attempts),
        "usage_cost_basis": "app_priced_model_usd",
        "retained_cost_basis": "guarded_charge_micros_and_attempt_cap_micros",
        "provider_invoice_status": "unproven",
    }
    if not execute:
        return result

    attempts = list(existing_attempts)
    market_results = []
    funding = current
    for index, question in enumerate(profile.question_catalog):
        if index >= len(existing_attempts):
            slots_left = len(profile.question_catalog) - index
            try:
                _verify_daily_headroom(runtime, day, profile.attempt_cap_micros * slots_left)
            except Exception as exc:
                return {**result, "status": "stopped",
                        "stop_reason": f"daily_readback_failed:{_safe_reason(exc, 'unverified')}",
                        "markets": market_results}
            remaining = funding["allocated_micros"] - funding["consumed_micros"]
            if remaining < profile.attempt_cap_micros * slots_left:
                return {**result, "status": "stopped", "stop_reason": "trending_pool_headroom_exhausted",
                        "markets": market_results}
            context = {"market": question.market, "source_ids": [], "support_run_id": None, "posts": []}
            try:
                receipt = runtime.run_attempt(
                    question, 1, funding, context, profile=profile, attempt_data_dir=artifact_dir,
                    prior_data_dir=data_dir,
                    ranking_proof_bytes=demo_pairs.TRENDING_FALLBACK_PROFILE_PROOF_BYTES,
                    source_commit=source["head"],
                )
            except Exception as exc:
                return {**result, "status": "stopped",
                        "stop_reason": f"attempt_run_failed:{_safe_reason(exc, 'attempt_run_failed')}",
                        "markets": market_results, "preserved_attempts": [item["question_id"] for item in attempts],
                        "stopped_at_question_id": question.id}
            if not isinstance(receipt, dict):
                return {**result, "status": "stopped", "stop_reason": "attempt_receipt_invalid",
                        "markets": market_results, "preserved_attempts": [item["question_id"] for item in attempts],
                        "stopped_at_question_id": question.id}
            try:
                attempts = _verified_existing_attempts(
                    artifact_dir, profile, source["head"], getattr(runtime, "repo_root", ROOT))
            except Exception as exc:
                return {**result, "status": "stopped",
                        "stop_reason": _safe_reason(exc, "trending_attempt_persistence_unverified"),
                        "markets": market_results, "preserved_attempts": [item["question_id"] for item in attempts],
                        "stopped_at_question_id": question.id}
            if len(attempts) != index + 1 or attempts[-1].get("attempt_key") != receipt.get("attempt_key"):
                return {**result, "status": "stopped", "stop_reason": "trending_attempt_receipt_order_mismatch",
                        "markets": market_results, "preserved_attempts": [item["question_id"] for item in attempts],
                        "stopped_at_question_id": question.id}
            receipt = attempts[-1]
        else:
            receipt = attempts[index]

        try:
            raw = runtime.read_raw_attempt(receipt)
            dispatch = receipt.get("dispatch_result") or {}
            settled_ng_failure = (
                question.id == "TREND-NG-01"
                and demo_pairs._settled_trending_ng_unknown_cost_receipt_is_verified(receipt, profile)
            )
            if not demo_pairs._claim_readback_matches(dispatch):
                raise OperatorRefused("claim_readback_unverified")
            if not demo_operator._attempt_is_verified(receipt) and not settled_ng_failure:
                raise OperatorRefused("attempt_accounting_unverified")
            saved, app_record, claim_ids, evidence_ids, outcome, answer_status, qualifies = _saved_ask_readback(
                Path(artifact_dir), question, receipt, raw)
            if settled_ng_failure:
                outcome = "operational_failure"
        except Exception as exc:
            return {**result, "status": "stopped",
                    "stop_reason": f"attempt_readback_failed:{_safe_reason(exc, 'unverified')}",
                    "markets": market_results, "preserved_attempts": [item["question_id"] for item in attempts],
                    "stopped_at_question_id": question.id}

        cost = demo_operator._cost_receipt(question.id, 1, receipt, {
            "source_commit": receipt.get("source_commit"),
            "source_proof_sha256": receipt.get("source_proof_sha256"),
            "source_hashes": source.get("hashes") if receipt.get("source_commit") == source["head"] else None,
            "source_ancestor_proof": source.get("ancestors")
            if receipt.get("source_commit") == source["head"] else None,
        })
        cost.update({"profile_id": profile.profile_id,
                     "runtime_health_version": runtime_health_version,
                     "attempt_cap_micros": receipt.get("attempt_cap_micros"),
                     "guarded_charge_micros": receipt.get("guarded_charge_micros")})
        if settled_ng_failure:
            cost.update({"provider_cost_status": "unknown", "usage_priced_model_usd": None,
                         "app_recorded_model_usd": receipt.get("model_usd"),
                         "known_call_booking_micros": 737_841,
                         "failed_call_ceiling_micros": 744_597})
        cost_path = Path(artifact_dir) / f"{question.id}-attempt-1.cost.json"
        try:
            _write_if_same(cost_path, _canonical_bytes(cost))
        except Exception as exc:
            return {**result, "status": "stopped",
                    "stop_reason": f"cost_receipt_persist_failed:{_safe_reason(exc, 'unverified')}",
                    "markets": market_results, "preserved_attempts": [item["question_id"] for item in attempts],
                    "stopped_at_question_id": question.id}

        if index >= len(existing_attempts):
            try:
                funding = _verify_funding_readback(runtime, funding, prior_receipts, attempts, profile)
            except Exception as exc:
                return {**result, "status": "stopped",
                        "stop_reason": f"post_attempt_funding_readback_failed:{_safe_reason(exc, 'unverified')}",
                        "markets": market_results, "preserved_attempts": [item["question_id"] for item in attempts],
                        "stopped_at_question_id": question.id}

        handoff_path = None
        if qualifies:
            handoff = _l5_handoff(question, receipt, raw, saved, claim_ids, evidence_ids,
                                  source, runtime_health_version, answer_status)
            handoff_path = Path(artifact_dir) / "l5" / f"{question.id}.handoff.json"
            try:
                _write_if_same(handoff_path, _canonical_bytes(handoff))
            except Exception as exc:
                return {**result, "status": "stopped",
                        "stop_reason": f"saved_ask_handoff_persist_failed:{_safe_reason(exc, 'unverified')}",
                        "markets": market_results, "preserved_attempts": [item["question_id"] for item in attempts],
                        "stopped_at_question_id": question.id}
        version = app_record.get("version")
        if not isinstance(version, str) or not version.strip():
            version = None
        market_results.append({
            "question_id": question.id, "market": question.market,
            "source_commit": receipt.get("source_commit"),
            "status": outcome if qualifies or settled_ng_failure else "insufficient_evidence",
            "saved_view_status": saved.get("saved_view_status"),
            "answer_status": answer_status or None,
            "qualifies_for_l5": qualifies,
            "ask_id": receipt["attempt_persistence"]["ask_id"],
            "run_id": receipt["attempt_persistence"]["run_id"],
            "record_sha256": saved["record_sha256"],
            "object_type": "ask", "version": version,
            "claim_ids": claim_ids, "evidence_ids": evidence_ids,
            "usage_priced_model_usd": None if settled_ng_failure else receipt.get("model_usd"),
            "usage_price_ceiling_micros": None if settled_ng_failure else receipt.get("model_usd_ceiling_micros"),
            "retained_guarded_charge_micros": receipt.get("guarded_charge_micros"),
            "attempt_cap_micros": receipt.get("attempt_cap_micros"),
            "provider_invoice_status": "unproven",
            **({"failure_type": "BudgetRefused", "failure_stop_reason": "unknown_dispatched_cost",
                "provider_http_status": 504, "known_call_booking_micros": 737_841,
                "failed_call_ceiling_micros": 744_597,
                "app_recorded_model_usd": receipt.get("model_usd")}
               if settled_ng_failure else {}),
            "l5_handoff_path": None if handoff_path is None else str(handoff_path),
        })
    return {**result, "status": "complete", "markets": market_results,
            "final_guarded_micros": funding["consumed_micros"],
            "remaining_micros": funding["allocated_micros"] - funding["consumed_micros"]}


def run_trending_replay(runtime_factory=None, *, execute=False, data_dir=None, inputs_dir=None,
                        artifact_dir=None):
    previous_provider = os.environ.get("MODEL_PROVIDER")
    os.environ["MODEL_PROVIDER"] = "gemini"
    try:
        return _run_trending_replay(
            runtime_factory, execute=execute, data_dir=data_dir,
            inputs_dir=inputs_dir, artifact_dir=artifact_dir,
        )
    finally:
        if previous_provider is None:
            os.environ.pop("MODEL_PROVIDER", None)
        else:
            os.environ["MODEL_PROVIDER"] = previous_provider


def main(argv=None):
    parser = argparse.ArgumentParser(prog="py -3.13 -m core.eval.trending_replay")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_trending_replay(execute=args.execute)
    except OperatorRefused as exc:
        result = _blocked(exc.args[0] if exc.args else "trending_preflight_failed")
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("status") in {"prepared", "complete", "blocked"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
