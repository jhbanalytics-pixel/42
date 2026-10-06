import argparse
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime


from core.eval.demo_operator import (
    DATA,
    INPUTS,
    ROOT,
    OperatorRefused,
    _attempt_is_verified,
    _canonical_bytes,
    _read_object,
    _sha,
    production_runtime_factory,
)


RANKING_MATRIX_NAME = "FREE-RETRIEVAL-RANK-MATRIX-2026-10-01.json"
RANKING_MATRIX_SHA256 = "aa8e38b29bab749cee639302459dbcf385b3b92e11084cb49ca127a4ed17bfb1"
AUTHORITY_NAME = "ASK-RANKED-NOW-01-2026-10-01-AUTHORITY.json"
AUTHORITY_SHA256 = "2175c84db88b58ef701fa171088b33131186e4f90faca0e765950721866e6ea7"
L1_CARDS_QUALIFICATION_NAME = "NOW-01-L1-CARDS-QUALIFICATION.json"
L1_CARDS_QUALIFICATION_SHA256 = None
GO_PROFILE_ID = "ALBERT-CHAT-GO-2026-10-01-TOP-RANKED-ONCE-USD25"
RANKED_DATA = INPUTS / "ASK-RANKED-NOW-01-2026-10-01-RUN"
EXPECTED_PRIOR_GUARD_MICROS = 426_564
EXPECTED_ALLOCATION_MICROS = 12_974_288
ATTEMPT_CAP_MICROS = 3_000_000
PRE_DISPATCH_CHILD_NAME = "pre-dispatch-resume-1"
PRE_DISPATCH_ATTEMPT_KEY = "NOW-01-attempt-1"
PRE_DISPATCH_SOURCE_COMMIT = "c9a5685f42224f8bff8234c911616c62762cbe42"
PRE_DISPATCH_RUN_ID = "r_20261001_180927_753cffd0_2c4befb7e62849999b6006a53f6adcf6"
PRE_DISPATCH_OUTER_SHA256 = "5c1d8d184dd76e12c999cc5d422b3757c7b3a7de1717e792aae7426850827238"
PRE_DISPATCH_RAW_SHA256 = "60f24624dd216e47c17f25d47826aac0b714d0ec1b62f35163ad3e2df0372373"
PRE_DISPATCH_MARKER_SHA256 = "7f48a536da398a44cd3fcd3523e976a17dc5f1a0fd43a6bd4a5db0cb222ee2b4"
EXPECTED_PRIOR_RUN_IDS = (
    "r_20261001_115341_66d3df96_5b6f2aa8005f47c3bc029ee3c875436e",
    "r_20261001_133944_66d3df96_a0be7cffca7849b29b91264dca6362aa",
    "r_20261001_140638_d6e55f59_3c924eed27004f57ac2b33f649dfcd02",
    "r_20261001_140809_d6e55f59_12094792264e4ed690197af270f86cd5",
    "r_20261001_141049_c6d64d19_0bafc57e8c7d42d8b78e1dbbeb1aca43",
)


def _profile_value(profile, name):
    if isinstance(profile, dict):
        return profile.get(name)
    return getattr(profile, name, None)


def _validate_profile(profile, authority, matrix_sha):
    target = authority.get("target")
    questions = _profile_value(profile, "question_catalog")
    sequence = _profile_value(profile, "attempt_sequence")
    if not isinstance(target, dict) or target != {
        "question_id": "NOW-01", "market": "ZA", "rank": 1, "attempt_number": 1,
    }:
        raise OperatorRefused("ranked_authority_target_mismatch")
    if not isinstance(questions, (tuple, list)) or len(questions) != 1:
        raise OperatorRefused("ranked_profile_question_catalog_invalid")
    question = questions[0]
    if (_profile_value(question, "id") != "NOW-01" or _profile_value(question, "market") != "ZA"
            or not isinstance(_profile_value(question, "text"), str)
            or not _profile_value(question, "text")):
        raise OperatorRefused("ranked_profile_target_mismatch")
    if tuple(sequence or ()) != (("NOW-01", 1),):
        raise OperatorRefused("ranked_profile_attempt_sequence_mismatch")
    if (_profile_value(profile, "source_proof_sha256") != matrix_sha
            or _profile_value(profile, "source_go_profile_id") != GO_PROFILE_ID
            or _profile_value(profile, "attempt_cap_micros") != ATTEMPT_CAP_MICROS
            or _profile_value(profile, "authorization_slots") != 1
            or _profile_value(profile, "prior_guarded_micros") != EXPECTED_PRIOR_GUARD_MICROS
            or _profile_value(profile, "prior_verified_app_count") != 5):
        raise OperatorRefused("ranked_profile_authority_mismatch")
    if (authority.get("schema_version") != "ranked-demo-authority-v1"
            or authority.get("ranking_matrix_filename") != RANKING_MATRIX_NAME
            or authority.get("ranking_matrix_sha256") != matrix_sha
            or authority.get("max_question_attempts") != 1
            or authority.get("automatic_retries") != 0
            or authority.get("attempt_cap_micros") != ATTEMPT_CAP_MICROS
            or authority.get("per_attempt_cap_usd") != 3
            or authority.get("cumulative_model_cap_usd") != 25
            or authority.get("native_transfer_allowed") is not False
            or authority.get("reservation_release_allowed") is not False
            or authority.get("replay_existing_demo_attempts") is not False):
        raise OperatorRefused("ranked_authority_contract_mismatch")
    return question


def _verify_l1_cards_qualification():
    expected_sha = L1_CARDS_QUALIFICATION_SHA256
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise OperatorRefused("ranked_l1_cards_gate_unconfigured")
    path = INPUTS / L1_CARDS_QUALIFICATION_NAME
    try:
        content = path.read_bytes()
    except OSError:
        raise OperatorRefused("ranked_l1_cards_proof_missing") from None
    if _sha(content) != expected_sha:
        raise OperatorRefused("ranked_l1_cards_proof_hash_mismatch")
    proof = _read_object(path, "ranked_l1_cards_proof_invalid")
    cards = proof.get("cards_by_market")
    count = cards.get("ZA") if isinstance(cards, dict) else None
    board_line = proof.get("l1_board_line")
    board_sha = proof.get("board_line_sha256")
    source_execution_id = proof.get("source_execution_id")
    qualified_at = proof.get("qualified_at")
    try:
        qualified_time = datetime.fromisoformat(qualified_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        qualified_time = None
    if (proof.get("schema_version") != "l3-now-cards-qualification-v1"
            or proof.get("source_lane") != "L1"
            or proof.get("basis") != "brief_rerun_published_cards"
            or not isinstance(source_execution_id, str) or not source_execution_id
            or source_execution_id.casefold().endswith("f7j74")
            or type(count) is not int or count <= 0
            or not isinstance(board_line, str) or not board_line or board_line != board_line.strip()
            or not isinstance(board_sha, str) or _sha(board_line.encode("utf-8")) != board_sha
            or qualified_time is None or qualified_time.tzinfo is None or qualified_time.utcoffset() is None):
        raise OperatorRefused("ranked_l1_cards_qualification_mismatch")
    return {"sha256": expected_sha, "source_execution_id": source_execution_id,
            "cards_by_market": cards, "l1_board_line": board_line, "qualified_at": qualified_at}


def _verified_prior_attempts(receipts):
    verified = [row for row in receipts if _attempt_is_verified(row)]
    if len(verified) != 5:
        raise OperatorRefused("ranked_prior_receipt_count_mismatch")
    guards = [row.get("guarded_charge_micros") for row in verified]
    run_ids = [row.get("attempt_persistence", {}).get("run_id") for row in verified]
    if (any(type(value) is not int or value <= 0 for value in guards)
            or sum(guards) != EXPECTED_PRIOR_GUARD_MICROS
            or any(not isinstance(value, str) or not value for value in run_ids)
            or len(run_ids) != len(set(run_ids))):
        raise OperatorRefused("ranked_prior_receipt_accounting_mismatch")
    for row in receipts:
        if row in verified:
            continue
        if (row.get("status") != "failed_before_dispatch"
                or row.get("guarded_charge_micros") != 0):
            raise OperatorRefused("ranked_prior_receipt_unresolved")
    return verified


def _zero_money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return not isinstance(value, bool) and amount.is_finite() and amount == 0


def _verify_pre_dispatch_parent(matrix_sha, authority_sha, profile):
    from core.eval import demo_pairs

    if not RANKED_DATA.is_dir():
        raise OperatorRefused("pre_dispatch_parent_missing")
    key = PRE_DISPATCH_ATTEMPT_KEY
    marker_path = RANKED_DATA / f"{key}.attempted"
    receipt_path = RANKED_DATA / f"{key}.json"
    raw_path = RANKED_DATA / "raw" / f"{key}.json"
    expected_files = {f"{key}.attempted", f"{key}.json", f"raw/{key}.json"}
    parent_items = [item for item in RANKED_DATA.rglob("*")
                    if item.relative_to(RANKED_DATA).parts[0] != PRE_DISPATCH_CHILD_NAME]
    actual_files = {item.relative_to(RANKED_DATA).as_posix() for item in parent_items if item.is_file()}
    actual_dirs = {item.relative_to(RANKED_DATA).as_posix() for item in parent_items if item.is_dir()}
    if actual_files != expected_files or actual_dirs != {"raw"}:
        raise OperatorRefused("pre_dispatch_parent_file_set_mismatch")
    marker_bytes, outer_bytes, raw_bytes = marker_path.read_bytes(), receipt_path.read_bytes(), raw_path.read_bytes()
    if (_sha(marker_bytes) != PRE_DISPATCH_MARKER_SHA256
            or _sha(outer_bytes) != PRE_DISPATCH_OUTER_SHA256
            or _sha(raw_bytes) != PRE_DISPATCH_RAW_SHA256):
        raise OperatorRefused("pre_dispatch_parent_hash_mismatch")
    marker = _read_object(marker_path, "pre_dispatch_parent_marker_invalid")
    outer = _read_object(receipt_path, "pre_dispatch_parent_receipt_invalid")
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise OperatorRefused("pre_dispatch_parent_raw_invalid") from None
    if not isinstance(raw, dict):
        raise OperatorRefused("pre_dispatch_parent_raw_invalid")
    if (marker.get("attempt_key") != key or marker.get("attempt_cap_micros") != ATTEMPT_CAP_MICROS
            or marker.get("schema_version") != "demo-pair-ranked-marker-v1"
            or marker.get("prompt_source_sha256") != matrix_sha
            or marker.get("execution_profile_id") != _profile_value(profile, "profile_id")
            or marker.get("source_go_profile_id") != GO_PROFILE_ID
            or marker.get("source_proof_sha256") != matrix_sha
            or marker.get("source_commit") != PRE_DISPATCH_SOURCE_COMMIT
            or marker.get("authorization_slots") != 1):
        raise OperatorRefused("pre_dispatch_parent_marker_binding_mismatch")
    if (outer.get("question_id") != "NOW-01" or outer.get("attempt_number") != 1
            or outer.get("attempt_key") != key or outer.get("status") != "failed_before_dispatch"
            or outer.get("failure_type") != "model_budget_not_used"
            or outer.get("guarded_charge_micros") != 0 or outer.get("attempt_cap_micros") != ATTEMPT_CAP_MICROS
            or outer.get("model_usd") is not None or outer.get("model_usd_ceiling_micros") is not None
            or outer.get("attempt_persistence") is not None or outer.get("readback") is not None
            or outer.get("source_commit") != PRE_DISPATCH_SOURCE_COMMIT
            or outer.get("source_proof_sha256") != matrix_sha
            or outer.get("source_go_profile_id") != GO_PROFILE_ID
            or outer.get("execution_profile_id") != _profile_value(profile, "profile_id")
            or outer.get("authorization_slots") != 1):
        raise OperatorRefused("pre_dispatch_parent_receipt_state_mismatch")
    budget = outer.get("budget")
    calls = budget.get("calls") if isinstance(budget, dict) else None
    if (not isinstance(budget, dict) or budget.get("cap_micros") != ATTEMPT_CAP_MICROS
            or budget.get("charged_micros") != 0 or budget.get("reserved_micros") != 0
            or budget.get("stop_reason") != "research_failed" or not isinstance(calls, list) or len(calls) != 1):
        raise OperatorRefused("pre_dispatch_parent_budget_mismatch")
    call = calls[0]
    if (call.get("phase") != "structured" or call.get("status") != "refused_before_dispatch"
            or call.get("stop_reason") != "prior_call_failure"
            or not _zero_money(call.get("charged_usd")) or not _zero_money(call.get("reserved_usd"))):
        raise OperatorRefused("pre_dispatch_parent_refusal_mismatch")
    dispatch = outer.get("dispatch_result")
    if (not isinstance(dispatch, dict) or dispatch.get("outcome") != "OPERATIONAL STOP"
            or dispatch.get("stop_reason") != "research_failed"
            or dispatch.get("error_type") != "BudgetRefused"
            or dispatch.get("research_error_type") != "DemoDispatchRefused"
            or dispatch.get("unknown_cost") is not False
            or not _zero_money(dispatch.get("charged_usd"))
            or dispatch.get("model_usd_micros") != 0
            or dispatch.get("reported_run_model_usd_micros") != 0
            or dispatch.get("guarded_charge_micros") != 0
            or dispatch.get("source") != {"prompt_catalog_sha256": matrix_sha}
            or dispatch.get("call_costs") != calls
            or dispatch.get("raw_receipt_path") != str(raw_path.resolve())
            or dispatch.get("raw_receipt_sha256") != PRE_DISPATCH_RAW_SHA256
            or dispatch.get("raw_receipt") != raw
            or dispatch.get("spend_writes") not in (None, [])):
        raise OperatorRefused("pre_dispatch_parent_dispatch_mismatch")
    for intent_name in ("booking_intents", "spend_intents"):
        intent = dispatch.get(intent_name)
        if (not isinstance(intent, dict) or intent.get("native_write") is not False
                or intent.get("credit") is not False
                or not _zero_money(intent.get("usd"))
                or not _zero_money(intent.get("reported_usd"))):
            raise OperatorRefused("pre_dispatch_parent_native_intent_mismatch")
    app_record = raw.get("app_record")
    run = app_record.get("run") if isinstance(app_record, dict) else None
    tokens = run.get("tokens") if isinstance(run, dict) else None
    if (raw.get("question_id") != "NOW-01" or raw.get("attempt_number") != 1
            or raw.get("attempt_key") != key or raw.get("run_id") != PRE_DISPATCH_RUN_ID
            or raw.get("unknown_cost") is not False or raw.get("error_type") != "BudgetRefused"
            or raw.get("research_error_type") != "DemoDispatchRefused"
            or raw.get("stop_reason") != "research_failed"
            or raw.get("outcome") != "OPERATIONAL STOP"
            or not _zero_money(raw.get("charged_usd"))
            or raw.get("model_usd_micros") != 0
            or raw.get("reported_run_model_usd_micros") != 0
            or raw.get("guarded_charge_micros") != 0
            or raw.get("source", {}).get("prompt_catalog_sha256") != matrix_sha
            or raw.get("call_costs") != calls or raw.get("spend_writes") not in (None, [])
            or not isinstance(app_record, dict) or app_record.get("status") != "failed"
            or not isinstance(run, dict) or run.get("run_id") != PRE_DISPATCH_RUN_ID
            or not _zero_money(run.get("model_usd")) or not _zero_money(run.get("credits"))
            or not isinstance(tokens, dict) or tokens.get("input") != 0 or tokens.get("output") != 0):
        raise OperatorRefused("pre_dispatch_parent_raw_state_mismatch")
    proof = outer.get("funding_proof")
    if (not isinstance(proof, dict) or proof.get("verified") is not True
            or proof.get("run_date") != "2026-10-01"
            or proof.get("allocated_micros") != EXPECTED_ALLOCATION_MICROS
            or proof.get("consumed_micros") != EXPECTED_PRIOR_GUARD_MICROS
            or proof.get("native_net_micros") != 25_000_000
            or proof.get("baseline_run_ids") != list(demo_pairs.BASELINE_RUN_IDS)
            or proof.get("app_run_ids") != list(EXPECTED_PRIOR_RUN_IDS)):
        raise OperatorRefused("pre_dispatch_parent_funding_mismatch")
    return {
        "schema_version": "ranked-pre-dispatch-resume-v1",
        "parent_attempt_key": key,
        "parent_source_commit": PRE_DISPATCH_SOURCE_COMMIT,
        "parent_run_id": PRE_DISPATCH_RUN_ID,
        "parent_receipt_sha256": PRE_DISPATCH_OUTER_SHA256,
        "parent_raw_receipt_sha256": PRE_DISPATCH_RAW_SHA256,
        "parent_marker_sha256": PRE_DISPATCH_MARKER_SHA256,
        "parent_failure_type": "model_budget_not_used",
        "parent_refusal_type": "DemoDispatchRefused",
        "parent_refusal_reason": "prior_call_failure",
        "ranking_matrix_sha256": matrix_sha,
        "authority_sha256": authority_sha,
        "prior_guarded_micros": EXPECTED_PRIOR_GUARD_MICROS,
        "prior_run_ids": list(EXPECTED_PRIOR_RUN_IDS),
    }


def run_ranked_demo(runtime_factory=None, *, execute=False, profile=None, resume_pre_dispatch=False):
    from core.eval import demo_pairs

    matrix_path = INPUTS / RANKING_MATRIX_NAME
    authority_path = INPUTS / AUTHORITY_NAME
    try:
        matrix_bytes = matrix_path.read_bytes()
        authority_bytes = authority_path.read_bytes()
    except OSError:
        raise OperatorRefused("ranked_proof_file_missing") from None
    matrix_sha = _sha(matrix_bytes)
    authority_sha = _sha(authority_bytes)
    if matrix_sha != RANKING_MATRIX_SHA256 or authority_sha != AUTHORITY_SHA256:
        raise OperatorRefused("ranked_proof_hash_mismatch")
    authority = _read_object(authority_path, "ranked_authority_invalid")
    selected_profile = profile or getattr(demo_pairs, "RANKED_NOW_ONCE_PROFILE", None)
    if selected_profile is None:
        raise OperatorRefused("ranked_profile_unavailable")
    question = _validate_profile(selected_profile, authority, matrix_sha)
    if resume_pre_dispatch:
        execute = True
    l1_qualification = _verify_l1_cards_qualification() if execute else None
    if resume_pre_dispatch:
        resume_parent = _verify_pre_dispatch_parent(matrix_sha, authority_sha, selected_profile)
        resume_parent["l1_cards_qualification_sha256"] = l1_qualification["sha256"]
        artifact_dir = RANKED_DATA / PRE_DISPATCH_CHILD_NAME
        if artifact_dir.exists():
            raise OperatorRefused("pre_dispatch_resume_already_used")
    else:
        resume_parent = None
        artifact_dir = RANKED_DATA

    factory = runtime_factory or production_runtime_factory
    runtime = factory(repo_root=ROOT, data_dir=DATA, inputs_dir=INPUTS)
    source_proof = runtime._verify_committed_sources()
    if not isinstance(source_proof, dict) or source_proof.get("clean") is not True:
        raise OperatorRefused("ranked_source_commit_unverified")
    source_commit = source_proof.get("head")
    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise OperatorRefused("ranked_source_commit_invalid")
    runtime._load()
    from core.eval import ask_r2
    if (runtime.authority.get("principal") != ask_r2.BUILDER_EMAIL
            or runtime.authority.get("project") != ask_r2.PROJECT):
        raise OperatorRefused("builder_identity_mismatch")
    now = runtime.wiring.now()
    run_day = now.astimezone(runtime.modules.staging.SAST).date()
    if run_day.isoformat() != "2026-10-01":
        raise OperatorRefused("ranked_demo_run_date_mismatch")

    funding_path = DATA / "funding" / "transfer.json"
    funding = _read_object(funding_path, "existing_funding_proof_invalid")
    if (funding.get("verified") is not True or funding.get("run_date") != run_day.isoformat()
            or funding.get("allocated_micros") != EXPECTED_ALLOCATION_MICROS
            or funding.get("consumed_micros") != 0 or funding.get("app_run_ids") != []
            or funding.get("native_net_micros") != 25_000_000):
        raise OperatorRefused("existing_funding_proof_scope_mismatch")
    _, base_consumed, _ = runtime.native._phase_readback(funding)
    if base_consumed != 0:
        raise OperatorRefused("existing_funding_phase_not_unspent")
    old_receipts = _verified_prior_attempts(demo_pairs._existing_receipts(DATA))
    current_funding = runtime.refresh_funding(funding, old_receipts)
    if (current_funding.get("consumed_micros") != EXPECTED_PRIOR_GUARD_MICROS
            or current_funding.get("native_net_micros") != 25_000_000):
        raise OperatorRefused("ranked_prior_funding_readback_mismatch")
    remaining_micros = current_funding["allocated_micros"] - current_funding["consumed_micros"]
    daily = runtime.fresh_daily_readback(run_day)
    if (daily.get("verified") is not True or daily.get("all_pages_consumed") is not True
            or daily.get("run_date") != run_day.isoformat()
            or daily.get("daily_cap_micros", 0) - daily.get("canonical_total_micros", 0) < ATTEMPT_CAP_MICROS
            or remaining_micros < ATTEMPT_CAP_MICROS):
        raise OperatorRefused("ranked_attempt_headroom_unverified")
    if not resume_pre_dispatch and RANKED_DATA.exists() and any(RANKED_DATA.iterdir()):
        raise OperatorRefused("ranked_output_already_started")

    result = {
        "mode": "resume_pre_dispatch" if resume_pre_dispatch else "execute" if execute else "prepare",
        "status": "prepared",
        "question_id": "NOW-01",
        "market": "ZA",
        "rank": 1,
        "attempt_number": 1,
        "attempt_cap_micros": ATTEMPT_CAP_MICROS,
        "ranking_matrix_sha256": matrix_sha,
        "authority_sha256": authority_sha,
        "source_commit": source_commit,
        "source_hashes": source_proof.get("hashes"),
        "health_proof": runtime.health_proof,
        "prior_run_ids": current_funding.get("app_run_ids"),
        "prior_guarded_micros": current_funding.get("consumed_micros"),
        "available_micros": remaining_micros,
        "native_net_micros": current_funding.get("native_net_micros"),
        "provider_invoice_status": "unproven",
        "l1_cards_qualification_sha256": (
            None if l1_qualification is None else l1_qualification["sha256"]),
        "artifact_dir": str(artifact_dir),
    }
    if resume_parent is not None:
        result["resume_parent"] = resume_parent
    if not execute:
        return result
    if resume_pre_dispatch:
        artifact_dir.mkdir(parents=True, exist_ok=False)
        from core.eval.demo_operator import _write_exclusive
        _write_exclusive(artifact_dir / "pre-dispatch-resume.json", _canonical_bytes(resume_parent))
    context = {"market": "ZA", "source_ids": [], "support_run_id": None, "posts": []}
    receipt = runtime.run_attempt(
        question, 1, current_funding, context, profile=selected_profile,
        attempt_data_dir=artifact_dir, prior_data_dir=DATA,
        ranking_proof_bytes=matrix_bytes, source_commit=source_commit)
    if not isinstance(receipt, dict):
        raise OperatorRefused("ranked_attempt_receipt_invalid")
    result["attempt_receipt"] = receipt
    result["status"] = "complete" if _attempt_is_verified(receipt) else "held"
    if _attempt_is_verified(receipt):
        after = runtime.refresh_funding(funding, [*old_receipts, receipt])
        expected = EXPECTED_PRIOR_GUARD_MICROS + receipt["guarded_charge_micros"]
        if (after.get("consumed_micros") != expected or after.get("native_net_micros") != 25_000_000
                or after.get("app_run_ids") != [*current_funding["app_run_ids"],
                                                 receipt["attempt_persistence"]["run_id"]]):
            raise OperatorRefused("ranked_attempt_phase_readback_mismatch")
        result["final_guarded_micros"] = after["consumed_micros"]
        cost = {
            "schema_version": "ranked-demo-cost-v1", "profile_id": _profile_value(selected_profile, "profile_id"),
            "question_id": "NOW-01", "attempt_number": 1, "status": receipt.get("status"),
            "run_id": receipt["attempt_persistence"]["run_id"],
            "ask_id": receipt["attempt_persistence"]["ask_id"],
            "model_usd": receipt.get("model_usd"),
            "model_usd_ceiling_micros": receipt.get("model_usd_ceiling_micros"),
            "guarded_charge_micros": receipt.get("guarded_charge_micros"),
            "provider_invoice_status": "unproven", "native_net_micros": 25_000_000,
            "ranking_matrix_sha256": matrix_sha, "authority_sha256": authority_sha,
            "source_commit": source_commit,
            "l1_cards_qualification_sha256": l1_qualification["sha256"],
        }
        from core.eval.demo_operator import _write_exclusive
        _write_exclusive(artifact_dir / "NOW-01-attempt-1.cost.json", _canonical_bytes(cost))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the one authorized ranked NOW-01 attempt.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--resume-pre-dispatch", action="store_true")
    args = parser.parse_args(argv)
    result = run_ranked_demo(execute=args.execute, resume_pre_dispatch=args.resume_pre_dispatch)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
