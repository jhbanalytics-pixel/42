from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Mapping

from core.eval.ask_r2 import SessionBudget


DEMO_QUESTIONS_SHA256 = "d4613ac2f2390f8efb9c0b61af503c3050e3fa9bf03704aa839374b654830e2f"
SAST = timezone(timedelta(hours=2))
DEMO_RUN_DATE = date(2026, 10, 1)
DEMO_PHASE_ID = "l3-demo-20261001"
DEMO_FUNDING_SCHEMA = "demo-funding-v1"
SOURCE_RUN_DATE = date(2026, 9, 30)
BASELINE_RUN_IDS = (
    "l3-ask-20260930-r3-NOW-01-spend-12638a48",
    "l3-ask-20260930-r3-NOW-01-retry-1-spend-3f5031e1",
    "l3-ask-20260930-r3-NOW-01-continuation-1-spend-5df280f9",
    "l3-ask-20260930-r3-RISE-02-spend-8e9d4c20",
    "l3-ask-20260930-r3-CRE-01-spend-6a21605f",
    "l3-ask-20260930-r3-SPR-03-spend-8a5f5498",
    "l3-ask-20260930-r3-WHY-03-spend-19e2f107",
)
BASELINE_BOOKED_MICROS = 25_000_000
CUMULATIVE_CAP_MICROS = 25_000_000
KNOWN_UNUSED_MICROS = 13_471_903
PRESERVED_OLD_UNUSED_MICROS = 497_615
NEW_POOL_MICROS = KNOWN_UNUSED_MICROS - PRESERVED_OLD_UNUSED_MICROS
SOURCE_RESERVATION_RUN_ID = BASELINE_RUN_IDS[2]
SOURCE_BOOKED_MICROS = 15_000_000
SOURCE_CONSUMED_MICROS = 2_025_712
SOURCE_RECEIPT_SHA256 = "91e40ac060b8d572970a846b43657caf663bb82de4a57751765182f40ff1538b"
MAX_ATTEMPTS = 2
MAX_TOTAL_ATTEMPTS = 10
MAX_CONCURRENCY = 1

PRODUCTION_REQUIREMENTS = (
    "Refresh current-day headroom and the complete seven-row R3 ledger immediately before transfer.",
    "Append the source-date debit and current-date allocation in one atomic write with deterministic IDs, then read back all rows.",
    "Do not call the non-idempotent book_spend helper twice for the transfer.",
    "Dispatch only through an adapter that reuses this SessionBudget with BudgetedGeminiModel and zero provider retries.",
    "Use a bounded RefusingClient evaluation path; a live API path must prove SocialCrawl is disabled and enforce the same per-attempt cap.",
    "The attempt adapter must atomically persist the Ask record and matching reservation-release correction, then verify the readback.",
    "Persist each real Ask or failed Ask record with its verified hash and accounting before the next slot.",
    "Do not choose an attempt or create final saved work until both pair receipts are present.",
    "Fetch the anchored staging posts before dispatch and keep that context separate from the exact frozen prompt.",
)


class DemoRunRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoQuestion:
    id: str
    text: str
    market: str
    source_ids: tuple[str, ...]
    support_run_id: str


@dataclass(frozen=True)
class DemoExecutionProfile:
    profile_id: str
    question_catalog: tuple[DemoQuestion, ...]
    attempt_sequence: tuple[tuple[str, int], ...]
    source_proof_sha256: str
    source_go_profile_id: str
    authorization_slots: int
    attempt_cap_micros: int
    prior_guarded_micros: int
    prior_verified_app_count: int


DEMO_QUESTIONS = (
    DemoQuestion(
        "DEMO-01",
        "Which exact lines appear in the three cited TikTok posts mentioning Sabinus or Peller?",
        "NG",
        (
            "obs1_585d958e17e03bf9f9ccad5c7c2f0b38",
            "obs1_da53ac54e21872d0554146ef21188590",
            "obs1_78054f141c7fb15c269a8c1cfac4a1ae",
        ),
        "r_20260930_191919_83606d80_e02142b2f8d2458db8c10024bce55a6e",
    ),
    DemoQuestion(
        "DEMO-02",
        "Which venue is named in the one stored post with market=KE dated 29 September 2026?",
        "KE",
        ("obs1_4fce7a7d6895ef0915e0b217fbddf290",),
        "r_20260930_192833_e4b9f923_73632c8107474574880ac045ce01c19e",
    ),
    DemoQuestion(
        "DEMO-03",
        "What engagement is recorded on the 24 September @mr_funny_tv1 TikTok post?",
        "NG",
        ("obs1_da53ac54e21872d0554146ef21188590",),
        "r_20260930_191919_83606d80_e02142b2f8d2458db8c10024bce55a6e",
    ),
    DemoQuestion(
        "DEMO-04",
        "What engagement is recorded on the Reddit post that names Talanta Stadium?",
        "KE",
        ("obs1_4fce7a7d6895ef0915e0b217fbddf290",),
        "r_20260930_192833_e4b9f923_73632c8107474574880ac045ce01c19e",
    ),
    DemoQuestion(
        "DEMO-05",
        "What allegation is described in the 28 September IOL post about WhatsApp messages involving Joe Sibanyoni and Bafana Sindane?",
        "ZA",
        ("obs1_dce53bd6bb321b436ac9d0078763dc04",),
        "r_20260930_230741_753cffd0_a931a1f9740942f19d0a817a5d407091",
    ),
)

ATTEMPT_SEQUENCE = tuple(
    (question.id, attempt_number)
    for question in DEMO_QUESTIONS
    for attempt_number in range(1, MAX_ATTEMPTS + 1)
)

RANKED_NOW_ONCE_PROFILE = DemoExecutionProfile(
    profile_id="ranked-now-once-v1",
    question_catalog=(DemoQuestion(
        "NOW-01",
        "What's South African social actually talking about this week, outside the election and sport? "
        "Give me the three conversations with the most momentum right now and show me the posts that prove it.",
        "ZA",
        (),
        "",
    ),),
    attempt_sequence=(("NOW-01", 1),),
    source_proof_sha256="aa8e38b29bab749cee639302459dbcf385b3b92e11084cb49ca127a4ed17bfb1",
    source_go_profile_id="ALBERT-CHAT-GO-2026-10-01-TOP-RANKED-ONCE-USD25",
    authorization_slots=1,
    attempt_cap_micros=3_000_000,
    prior_guarded_micros=426_564,
    prior_verified_app_count=5,
)

_TRENDING_QUESTIONS = (
    DemoQuestion("TREND-ZA-01", "what is trending in south africa", "ZA", (), None),
    DemoQuestion("TREND-NG-01", "what is trending in nigeria", "NG", (), None),
    DemoQuestion("TREND-KE-01", "what is trending in kenya", "KE", (), None),
)
TRENDING_FALLBACK_PROFILE_PROOF_BYTES = (
    json.dumps({
        "schema_version": "l3-trending-fallback-profile-v1",
        "profile_id": "trending-fallback-once-per-market-v1",
        "source_go_profile_id": "ALBERT-CHAT-GO-2026-10-01-TRENDING-FALLBACK-ONCE-PER-MARKET",
        "questions": [
            {"id": question.id, "text": question.text, "market": question.market,
             "source_ids": list(question.source_ids), "support_run_id": question.support_run_id}
            for question in _TRENDING_QUESTIONS
        ],
        "attempt_sequence": [[question.id, 1] for question in _TRENDING_QUESTIONS],
        "attempt_cap_micros": 3_000_000,
        "authorization_slots": 3,
        "automatic_retries": 0,
        "new_native_allocation_allowed": False,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
).encode("utf-8")
TRENDING_FALLBACK_PROFILE = DemoExecutionProfile(
    profile_id="trending-fallback-once-per-market-v1",
    question_catalog=_TRENDING_QUESTIONS,
    attempt_sequence=tuple((question.id, 1) for question in _TRENDING_QUESTIONS),
    source_proof_sha256=hashlib.sha256(TRENDING_FALLBACK_PROFILE_PROOF_BYTES).hexdigest(),
    source_go_profile_id="ALBERT-CHAT-GO-2026-10-01-TRENDING-FALLBACK-ONCE-PER-MARKET",
    authorization_slots=3,
    attempt_cap_micros=3_000_000,
    prior_guarded_micros=426_564,
    prior_verified_app_count=5,
)


@dataclass(frozen=True)
class DemoAttemptRequest:
    question_id: str
    attempt_number: int
    attempt_key: str
    prompt: str
    market: str
    source_ids: tuple[str, ...]
    support_run_id: str
    provided_context: Mapping
    previous_attempt: Mapping | None
    attempt_cap_micros: int


def _refuse(reason):
    raise DemoRunRefused(reason)


def _strict_micros(value, reason):
    if type(value) is not int:
        _refuse(reason)
    return value


def _as_date(value, reason):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        _refuse(reason)


def _now_sast():
    return datetime.now(timezone.utc).astimezone(SAST)


def _check_demo_day(funding, clock=None):
    now = _now_sast() if clock is None else clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _refuse("demo_clock_must_be_aware")
    run_day = now.astimezone(SAST).date()
    if run_day != DEMO_RUN_DATE or funding.get("run_date") != run_day.isoformat():
        _refuse("demo_funding_date_stale")


def _money_micros(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        _refuse("funding_row_amount_invalid")
    if not amount.is_finite():
        _refuse("funding_row_amount_invalid")
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def _exact_usd(value, reason):
    if not isinstance(value, str) or not value:
        _refuse(reason)
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        _refuse(reason)
    if not amount.is_finite() or amount.is_signed():
        _refuse(reason)
    return amount


def _ceil_usd_micros(amount):
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _usd_string(amount):
    if amount == 0:
        return "0"
    return format(amount.normalize(), "f")


def _row_micros(row):
    value = row.get("model_usd_micros")
    if type(value) is int:
        return value
    if "usd" in row:
        return _money_micros(row["usd"])
    counts = row.get("counts")
    if isinstance(counts, Mapping) and "model_usd" in counts:
        return _money_micros(counts["model_usd"])
    _refuse("funding_row_amount_invalid")


def _normalized_row(row):
    if not isinstance(row, Mapping):
        _refuse("funding_row_invalid")
    run_id = row.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        _refuse("funding_row_identity_invalid")
    normalized = {str(key): _json_value(value) for key, value in row.items()
                  if key not in {"usd", "counts", "model_usd_micros"}}
    normalized["run_id"] = run_id
    normalized["run_date"] = _as_date(row.get("run_date"), "funding_row_date_invalid").isoformat()
    normalized["model_usd_micros"] = _row_micros(row)
    return normalized


def _canonical(value):
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_value(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        _refuse("receipt_value_invalid")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    _refuse("receipt_value_invalid")


def source_proof_hash(source_rows):
    rows = _validate_baseline_rows(source_rows)
    proof = {
        "schema_version": "r3-unused-source-v1",
        "baseline_rows": sorted(rows, key=lambda row: row["run_id"]),
        "native_net_micros": BASELINE_BOOKED_MICROS,
        "source_reservation_run_id": SOURCE_RESERVATION_RUN_ID,
        "source_booked_micros": SOURCE_BOOKED_MICROS,
        "source_consumed_micros": SOURCE_CONSUMED_MICROS,
        "transferable_micros": NEW_POOL_MICROS,
        "source_receipt_sha256": SOURCE_RECEIPT_SHA256,
    }
    return hashlib.sha256(_canonical(proof).encode("utf-8")).hexdigest()


def _validate_baseline_rows(source_rows):
    if not isinstance(source_rows, (list, tuple)) or len(source_rows) != len(BASELINE_RUN_IDS):
        _refuse("funding_baseline_rows_invalid")
    rows = [_normalized_row(row) for row in source_rows]
    if len({row["run_id"] for row in rows}) != len(BASELINE_RUN_IDS):
        _refuse("funding_baseline_ids_mismatch")
    expected_amounts = {
        run_id: amount for run_id, amount in zip(
            BASELINE_RUN_IDS,
            (1_357_150, 1_158_263, 15_000_000, 1_646_701, 2_388_517, 1_805_746, 1_643_623),
        )
    }
    if ({row["run_id"] for row in rows} != set(BASELINE_RUN_IDS)
            or any(row["run_date"] != SOURCE_RUN_DATE.isoformat()
                   or row["model_usd_micros"] != expected_amounts[row["run_id"]] for row in rows)):
        _refuse("funding_baseline_rows_invalid")
    if sum(row["model_usd_micros"] for row in rows) != BASELINE_BOOKED_MICROS:
        _refuse("funding_baseline_net_mismatch")
    return rows


def prepare_funding_transfer(*, run_date, daily_headroom_micros, source_proof_sha,
                             source_rows, unknown_rows, amount_micros=None):
    day = _as_date(run_date, "funding_run_date_invalid")
    if day != DEMO_RUN_DATE:
        _refuse("funding_run_date_mismatch")
    headroom = _strict_micros(daily_headroom_micros, "daily_headroom_invalid")
    if headroom <= 0:
        _refuse("funding_transfer_limit")
    if not isinstance(source_proof_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_proof_sha):
        _refuse("funding_source_proof_invalid")
    rows = _validate_baseline_rows(source_rows)
    computed_source_sha = source_proof_hash(rows)
    if source_proof_sha != computed_source_sha:
        _refuse("funding_source_proof_mismatch")
    if not isinstance(unknown_rows, (list, tuple)):
        _refuse("unknown_rows_invalid")
    unknown = [_normalized_row(row) for row in unknown_rows]
    row_by_id = {row["run_id"]: row for row in rows}
    if any(row_by_id.get(row["run_id"]) != row for row in unknown):
        _refuse("unknown_rows_changed")
    max_transfer = min(NEW_POOL_MICROS, headroom)
    amount = max_transfer if amount_micros is None else _strict_micros(amount_micros, "funding_transfer_amount_invalid")
    if amount <= 0 or amount > max_transfer:
        _refuse("funding_transfer_limit")
    identity = hashlib.sha256(
        f"{DEMO_PHASE_ID}|{day.isoformat()}|{amount}|{source_proof_sha}".encode("utf-8")
    ).hexdigest()[:16]
    prefix = f"{DEMO_PHASE_ID}-transfer-{identity}"
    corrections = [
        {"run_id": f"{prefix}-source-debit", "run_date": SOURCE_RUN_DATE.isoformat(),
         "model_usd_micros": -amount, "stage": "understand_spend", "what": "demo_pool_transfer"},
        {"run_id": f"{prefix}-current-credit", "run_date": day.isoformat(),
         "model_usd_micros": amount, "stage": "understand_spend", "what": "demo_pool_transfer"},
    ]
    return {
        "schema_version": "demo-funding-transfer-v1",
        "phase_id": DEMO_PHASE_ID,
        "run_date": day.isoformat(),
        "amount_micros": amount,
        "reservation_run_id": corrections[1]["run_id"],
        "source_proof_sha": source_proof_sha,
        "source_rows": rows,
        "unknown_rows": unknown,
        "correction_rows": corrections,
        "expected_native_net_micros": BASELINE_BOOKED_MICROS,
        "preserved_old_unused_micros": PRESERVED_OLD_UNUSED_MICROS,
    }


def validate_funding_transfer_readback(plan, readback_rows):
    if not isinstance(plan, Mapping) or plan.get("schema_version") != "demo-funding-transfer-v1":
        _refuse("funding_transfer_plan_invalid")
    try:
        expected = [_normalized_row(row) for row in plan["source_rows"] + plan["correction_rows"]]
        actual = [_normalized_row(row) for row in readback_rows]
        computed_source_sha = source_proof_hash(plan["source_rows"])
    except (KeyError, TypeError):
        _refuse("funding_readback_invalid")
    if plan.get("source_proof_sha") != computed_source_sha:
        _refuse("funding_source_proof_mismatch")
    expected_sorted = sorted(expected, key=lambda row: row["run_id"])
    actual_sorted = sorted(actual, key=lambda row: row["run_id"])
    if len({row["run_id"] for row in actual_sorted}) != len(actual_sorted):
        _refuse("funding_readback_duplicate_rows")
    if actual_sorted != expected_sorted:
        _refuse("funding_readback_rows_mismatch")
    if sum(row["model_usd_micros"] for row in actual_sorted) != BASELINE_BOOKED_MICROS:
        _refuse("funding_readback_net_mismatch")
    unknown_by_id = {row["run_id"]: row for row in plan["unknown_rows"]}
    actual_by_id = {row["run_id"]: row for row in actual_sorted}
    if any(actual_by_id.get(run_id) != row for run_id, row in unknown_by_id.items()):
        _refuse("unknown_rows_changed")
    amount = _strict_micros(plan.get("amount_micros"), "funding_transfer_amount_invalid")
    return {
        "schema_version": DEMO_FUNDING_SCHEMA,
        "phase_id": DEMO_PHASE_ID,
        "verified": True,
        "run_date": plan["run_date"],
        "reservation_run_id": plan["reservation_run_id"],
        "allocated_micros": amount,
        "consumed_micros": 0,
        "source_proof_sha": plan["source_proof_sha"],
        "native_net_micros": BASELINE_BOOKED_MICROS,
        "baseline_run_ids": list(BASELINE_RUN_IDS),
        "app_run_ids": [],
    }


def _validate_funding_proof(proof):
    if not isinstance(proof, Mapping) or proof.get("verified") is not True:
        _refuse("funding_readback_unverified")
    if (proof.get("schema_version") != DEMO_FUNDING_SCHEMA
            or proof.get("phase_id") != DEMO_PHASE_ID
            or _as_date(proof.get("run_date"), "funding_run_date_invalid") != DEMO_RUN_DATE):
        _refuse("funding_proof_identity_mismatch")
    reservation_id = proof.get("reservation_run_id")
    source_sha = proof.get("source_proof_sha")
    if not isinstance(reservation_id, str) or not reservation_id:
        _refuse("funding_reservation_id_invalid")
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
        _refuse("funding_source_proof_invalid")
    allocated = _strict_micros(proof.get("allocated_micros"), "funding_allocation_invalid")
    consumed = _strict_micros(proof.get("consumed_micros"), "funding_consumption_invalid")
    native_net = _strict_micros(proof.get("native_net_micros"), "funding_native_net_invalid")
    baseline_ids = proof.get("baseline_run_ids")
    app_ids = proof.get("app_run_ids")
    if tuple(baseline_ids or ()) != BASELINE_RUN_IDS:
        _refuse("funding_baseline_ids_mismatch")
    if not isinstance(app_ids, (list, tuple)) or any(not isinstance(item, str) or not item for item in app_ids):
        _refuse("funding_app_ids_invalid")
    if len(set(app_ids)) != len(app_ids):
        _refuse("funding_app_ids_invalid")
    if not 0 < allocated <= NEW_POOL_MICROS or not 0 <= consumed <= allocated:
        _refuse("funding_allocation_invalid")
    if native_net != CUMULATIVE_CAP_MICROS:
        _refuse("funding_native_net_mismatch")
    return {
        "schema_version": DEMO_FUNDING_SCHEMA,
        "phase_id": DEMO_PHASE_ID,
        "run_date": DEMO_RUN_DATE.isoformat(),
        "reservation_run_id": reservation_id,
        "allocated_micros": allocated,
        "consumed_micros": consumed,
        "source_proof_sha": source_sha,
        "native_net_micros": native_net,
        "baseline_run_ids": list(baseline_ids),
        "app_run_ids": list(app_ids),
    }


def _execution_profile(profile):
    if profile is None:
        return None
    if (not isinstance(profile, DemoExecutionProfile)
            or profile not in (RANKED_NOW_ONCE_PROFILE, TRENDING_FALLBACK_PROFILE)):
        _refuse("execution_profile_invalid")
    return profile


def _validate_trending_profile_proof_bytes(profile, proof_bytes):
    if (profile != TRENDING_FALLBACK_PROFILE
            or not isinstance(proof_bytes, bytes)
            or hashlib.sha256(proof_bytes).hexdigest() != profile.source_proof_sha256
            or proof_bytes != TRENDING_FALLBACK_PROFILE_PROOF_BYTES):
        _refuse("trending_profile_proof_invalid")


TRENDING_SETTLED_NG_SOURCE_COMMIT = "ab49a7f6ad05581108841b8b1457192b16d19abd"
TRENDING_SETTLED_NG_RAW_RECEIPT_SHA256 = "e6530e31d114b19f4cc15eeffd86c52047272e161b20ccfa06cfcd036229b26a"
TRENDING_SETTLED_NG_RUN_ID = "r_20261001_231453_f1c126de_28bf5bcd6607481c89084a0092e55c39"
TRENDING_SETTLED_NG_ASK_ID = "a_20261001_6fa476a9"
TRENDING_SETTLED_NG_GUARDED_MICROS = 1_482_438


def _settled_trending_ng_unknown_cost_receipt_is_verified(receipt, profile):
    if (profile != TRENDING_FALLBACK_PROFILE or not isinstance(receipt, Mapping)
            or receipt.get("question_id") != "TREND-NG-01" or receipt.get("attempt_number") != 1
            or receipt.get("attempt_key") != "TREND-NG-01-attempt-1"
            or receipt.get("execution_profile_id") != profile.profile_id
            or receipt.get("source_proof_sha256") != profile.source_proof_sha256
            or receipt.get("source_go_profile_id") != profile.source_go_profile_id
            or receipt.get("authorization_slots") != profile.authorization_slots
            or receipt.get("source_commit") != TRENDING_SETTLED_NG_SOURCE_COMMIT
            or receipt.get("status") != "unknown_cost"
            or receipt.get("attempt_cap_micros") != profile.attempt_cap_micros
            or receipt.get("guarded_charge_micros") != TRENDING_SETTLED_NG_GUARDED_MICROS
            or receipt.get("model_usd") != "1.482438"
            or receipt.get("model_usd_ceiling_micros") != TRENDING_SETTLED_NG_GUARDED_MICROS):
        return False
    dispatch = receipt.get("dispatch_result")
    raw = dispatch.get("raw_receipt") if isinstance(dispatch, Mapping) else None
    if (not isinstance(dispatch, Mapping) or dispatch.get("raw_receipt_sha256")
            != TRENDING_SETTLED_NG_RAW_RECEIPT_SHA256 or not isinstance(raw, Mapping)
            or not _claim_readback_matches(dispatch)):
        return False
    app_record = raw.get("app_record")
    run = app_record.get("run") if isinstance(app_record, Mapping) else None
    if (raw.get("question_id") != "TREND-NG-01" or raw.get("attempt_number") != 1
            or raw.get("attempt_key") != "TREND-NG-01-attempt-1"
            or raw.get("outcome") != "OPERATIONAL STOP"
            or raw.get("stop_reason") != "unknown_dispatched_cost"
            or raw.get("error_type") != "BudgetRefused" or raw.get("research_http_status") != 504
            or raw.get("unknown_cost") is not True or raw.get("raw_answer") is not None
            or raw.get("claim_rows") != [] or raw.get("spend_writes") != []
            or not isinstance(app_record, Mapping) or app_record.get("status") != "failed"
            or app_record.get("answer") is not None or app_record.get("error_type") != "BudgetRefused"
            or not isinstance(run, Mapping) or run.get("run_id") != TRENDING_SETTLED_NG_RUN_ID
            or run.get("model_usd") != 1.482438 or raw.get("run_id") != TRENDING_SETTLED_NG_RUN_ID
            or raw.get("model_usd_micros") is not None or raw.get("guarded_charge_micros")
            != TRENDING_SETTLED_NG_GUARDED_MICROS):
        return False
    call_costs = raw.get("call_costs")
    if not isinstance(call_costs, list):
        return False
    known = [item for item in call_costs if isinstance(item, Mapping) and item.get("status") == "charged_known"]
    failed = [item for item in call_costs
              if isinstance(item, Mapping) and item.get("status") == "unknown_charged_ceiling"]
    refused = [item for item in call_costs
               if isinstance(item, Mapping) and item.get("status") == "refused_before_dispatch"]
    if (len(call_costs) != 18 or len(known) != 16 or len(failed) != 1 or len(refused) != 1
            or any(item.get("phase") != "research" or item.get("provider_retry_index") != 0 for item in known)):
        return False
    failed_call = failed[0]
    refused_call = refused[0]
    try:
        known_micros = sum(
            (Decimal(str(item["charged_usd"])) * Decimal(1_000_000) for item in known), Decimal(0))
        failed_micros = Decimal(str(failed_call["charged_usd"])) * Decimal(1_000_000)
        failed_reserve_micros = Decimal(str(failed_call["reserved_usd"])) * Decimal(1_000_000)
        refused_micros = Decimal(str(refused_call["charged_usd"])) * Decimal(1_000_000)
        refused_reserve_micros = Decimal(str(refused_call["reserved_usd"])) * Decimal(1_000_000)
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if (known_micros != Decimal(737_841) or failed_micros != Decimal(744_597)
            or failed_reserve_micros != Decimal(744_597)
            or known_micros + failed_micros != Decimal(TRENDING_SETTLED_NG_GUARDED_MICROS)
            or refused_micros != 0 or refused_reserve_micros != 0
            or failed_call.get("phase") != "research" or failed_call.get("provider_http_status") != 504
            or failed_call.get("provider_error_type") != "ServerError"
            or failed_call.get("provider_retry_index") != 0 or failed_call.get("retry_authorized") is not False
            or failed_call.get("measured_input_tokens") is not None
            or failed_call.get("measured_output_tokens") is not None
            or type(failed_call.get("charged_input_bound_tokens")) is not int
            or type(failed_call.get("charged_output_bound_tokens")) is not int
            or refused_call.get("phase") != "structured"
            or refused_call.get("stop_reason") != "prior_call_failure"):
        return False
    intents = raw.get("booking_intents")
    if (not isinstance(intents, list) or len(intents) != 1
            or intents[0].get("credit") is not False or intents[0].get("native_write") is not False
            or intents[0].get("phase_reservation_before_dispatch") is not True
            or Decimal(str(intents[0].get("usd"))) != Decimal("1.482438")
            or Decimal(str(intents[0].get("reported_usd"))) != Decimal("1.482438")):
        return False
    persistence = receipt.get("attempt_persistence")
    try:
        validated = _validate_attempt_persistence(persistence, TRENDING_SETTLED_NG_GUARDED_MICROS)
    except DemoRunRefused:
        return False
    return (validated.get("ask_id") == TRENDING_SETTLED_NG_ASK_ID
            and validated.get("run_id") == TRENDING_SETTLED_NG_RUN_ID
            and validated.get("recorded_model_usd") == "1.482438"
            and validated.get("reservation_release_usd") == "1.482438"
            and validated.get("native_net_micros") == 25_000_000
            and receipt.get("model_usd") == validated.get("recorded_model_usd")
            and receipt.get("model_usd_ceiling_micros")
            == validated.get("recorded_model_usd_ceiling_micros"))


def _profile_receipt_is_verified(receipt, profile):
    if not isinstance(receipt, Mapping) or receipt.get("execution_profile_id") != profile.profile_id:
        return False
    if receipt.get("status") == "unknown_cost":
        return _settled_trending_ng_unknown_cost_receipt_is_verified(receipt, profile)
    if receipt.get("status") in {"ambiguous", "failed_before_dispatch"}:
        return False
    guard = receipt.get("guarded_charge_micros")
    persistence = receipt.get("attempt_persistence")
    try:
        if type(guard) is not int or guard <= 0 or guard > profile.attempt_cap_micros:
            return False
        validated = _validate_attempt_persistence(persistence, guard)
    except DemoRunRefused:
        return False
    return (receipt.get("model_usd") == validated.get("recorded_model_usd")
            and receipt.get("model_usd_ceiling_micros")
            == validated.get("recorded_model_usd_ceiling_micros"))


def _validate_ranked_proof_bytes(profile, proof_bytes):
    if not isinstance(proof_bytes, bytes):
        _refuse("ranked_proof_bytes_invalid")
    if hashlib.sha256(proof_bytes).hexdigest() != profile.source_proof_sha256:
        _refuse("ranked_proof_hash_mismatch")
    try:
        proof = json.loads(proof_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse("ranked_proof_invalid")
    if (not isinstance(proof, Mapping)
            or proof.get("schema_version") != "free-retrieval-rank-candidate-v1"
            or proof.get("project") != "ogilvy-trends-v2"
            or proof.get("run_status") != "complete"
            or any(type(proof.get(name)) is not int or proof.get(name) != 0
                   for name in ("model_calls", "native_writes", "paid_calls"))):
        _refuse("ranked_proof_invalid")
    ranked = proof.get("ranked_top_five")
    candidates = proof.get("candidates")
    if (not isinstance(ranked, list) or not ranked
            or ranked[0].get("id") != "NOW-01" or ranked[0].get("rank") != 1
            or any(item.get("id") in {"DEMO-01", "DEMO-03"} for item in ranked
                   if isinstance(item, Mapping))
            or not isinstance(candidates, list)):
        _refuse("ranked_candidate_not_authorized")
    candidate = next((item for item in candidates
                      if isinstance(item, Mapping) and item.get("id") == "NOW-01"), None)
    question = profile.question_catalog[0]
    if (not isinstance(candidate, Mapping)
            or candidate.get("rank") != 1
            or candidate.get("evidence_status") != "eligible_for_review"
            or candidate.get("question") != question.text
            or candidate.get("target_markets") != [question.market]
            or question.id != "NOW-01" or question.market != "ZA" or question.source_ids):
        _refuse("ranked_candidate_not_authorized")
    return proof


def _validate_context(question, context):
    if not isinstance(context, Mapping) or context.get("market") != question.market:
        _refuse("source_context_market_mismatch")
    source_ids = context.get("source_ids")
    posts = context.get("posts")
    if not isinstance(source_ids, (list, tuple)) or tuple(source_ids) != question.source_ids:
        _refuse("source_context_ids_mismatch")
    if not isinstance(posts, (list, tuple)) or len(posts) != len(question.source_ids):
        _refuse("source_context_posts_missing")
    if tuple(post.get("id") for post in posts if isinstance(post, Mapping)) != question.source_ids:
        _refuse("source_context_posts_mismatch")
    if any(not isinstance(post.get("text"), str) or not post["text"] for post in posts):
        _refuse("source_context_text_missing")


def _attempt_key(question_id, attempt_number):
    return f"{question_id}-attempt-{attempt_number}"


def _attempt_paths(data_dir, question_id, attempt_number):
    key = _attempt_key(question_id, attempt_number)
    return data_dir / f"{key}.attempted", data_dir / f"{key}.json"


def _read_json(path, reason):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _refuse(reason)
    if not isinstance(value, dict):
        _refuse(reason)
    return value


def _recovered_receipt(data_dir, key, receipt, original_bytes, recovery_path):
    sidecar = _read_json(recovery_path, "attempt_recovery_invalid")
    if sidecar.get("schema_version") != "demo-attempt-recovery-v1":
        _refuse("attempt_recovery_schema_invalid")
    if sidecar.get("attempt_key") != key:
        _refuse("attempt_recovery_identity_mismatch")
    question_id = receipt.get("question_id")
    attempt_number = receipt.get("attempt_number")
    if (sidecar.get("question_id") != question_id or sidecar.get("attempt_number") != attempt_number
            or key != _attempt_key(question_id, attempt_number)):
        _refuse("attempt_recovery_identity_mismatch")
    if (key != "DEMO-01-attempt-1" or receipt.get("status") != "ambiguous"
            or receipt.get("failure_type") != "UncertainWriteError"):
        _refuse("attempt_recovery_slot_invalid")
    expected_outer_sha = hashlib.sha256(original_bytes).hexdigest()
    if sidecar.get("original_receipt_sha256") != expected_outer_sha:
        _refuse("attempt_recovery_outer_hash_mismatch")
    dispatch_result = receipt.get("dispatch_result")
    if not isinstance(dispatch_result, Mapping):
        _refuse("attempt_recovery_raw_receipt_missing")
    raw_path_value = dispatch_result.get("raw_receipt_path")
    if not isinstance(raw_path_value, str) or not raw_path_value:
        _refuse("attempt_recovery_raw_receipt_missing")
    root = Path(data_dir).resolve()

    def bound_file(path_value, expected_sha, missing_reason, hash_reason):
        if not isinstance(path_value, str) or not path_value:
            _refuse(missing_reason)
        path = Path(path_value)
        if not path.is_absolute():
            path = root / path
        try:
            path = path.resolve(strict=True)
            if not path.is_relative_to(root):
                _refuse("attempt_recovery_path_outside_data")
            content = path.read_bytes()
        except OSError:
            _refuse(missing_reason)
        if not isinstance(expected_sha, str) or hashlib.sha256(content).hexdigest() != expected_sha:
            _refuse(hash_reason)
        return path, content

    raw_path, raw_bytes = bound_file(
        raw_path_value, dispatch_result.get("raw_receipt_sha256"),
        "attempt_recovery_raw_receipt_missing", "attempt_recovery_raw_hash_mismatch")
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    if sidecar.get("raw_receipt_sha256") != raw_sha:
        _refuse("attempt_recovery_raw_hash_mismatch")
    try:
        raw_receipt = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse("attempt_recovery_raw_receipt_invalid")
    if (not isinstance(raw_receipt, Mapping)
            or not isinstance(dispatch_result.get("raw_receipt"), Mapping)
            or _canonical(raw_receipt) != _canonical(dispatch_result["raw_receipt"])):
        _refuse("attempt_recovery_raw_receipt_mismatch")
    raw_app_record = raw_receipt.get("app_record")
    if (raw_receipt.get("unknown_cost") is not False
            or not isinstance(raw_app_record, Mapping)):
        _refuse("attempt_recovery_raw_receipt_invalid")
    expected_status = dispatch_result.get("outcome")
    if (not isinstance(expected_status, str) or not expected_status
            or raw_receipt.get("outcome") != expected_status
            or sidecar.get("status") != expected_status):
        _refuse("attempt_recovery_status_mismatch")
    budget = receipt.get("budget")
    if not isinstance(budget, Mapping):
        _refuse("attempt_recovery_budget_missing")
    raw_guard = _strict_micros(budget.get("charged_micros"), "attempt_recovery_budget_invalid")
    raw_reserved = _strict_micros(budget.get("reserved_micros"), "attempt_recovery_budget_invalid")
    attempt_cap = _strict_micros(receipt.get("attempt_cap_micros"), "attempt_recovery_cap_invalid")
    budget_cap = _strict_micros(budget.get("cap_micros"), "attempt_recovery_cap_invalid")
    calls = budget.get("calls")
    settled_statuses = {"charged_known", "charged_conservative_ceiling"}
    if (attempt_cap != budget_cap or raw_reserved != 0 or budget.get("stop_reason") is not None
            or not isinstance(calls, list) or not calls
            or any(not isinstance(call, Mapping) or call.get("status") not in settled_statuses for call in calls)):
        _refuse("attempt_recovery_calls_unsettled")
    guard = _strict_micros(sidecar.get("guarded_charge_micros"), "attempt_recovery_guard_invalid")
    if guard != raw_guard or guard > attempt_cap:
        _refuse("attempt_recovery_guard_mismatch")
    persistence = sidecar.get("attempt_persistence")
    persistence = _validate_attempt_persistence(persistence, guard)
    run_id = persistence["run_id"]
    if (sidecar.get("run_id") != run_id or raw_receipt.get("run_id") != run_id
            or raw_app_record.get("ask_id") != persistence["ask_id"]):
        _refuse("attempt_recovery_run_id_mismatch")
    model_usd = sidecar.get("model_usd")
    model_ceiling = _strict_micros(sidecar.get("model_usd_ceiling_micros"), "attempt_recovery_model_ceiling_invalid")
    if (model_usd != persistence["recorded_model_usd"]
            or model_ceiling != persistence["recorded_model_usd_ceiling_micros"]):
        _refuse("attempt_recovery_model_cost_mismatch")
    readback = sidecar.get("readback")
    if not isinstance(readback, Mapping) or _canonical(readback) != _canonical(persistence["readback"]):
        _refuse("attempt_recovery_readback_mismatch")
    w6_path, w6_bytes = bound_file(
        sidecar.get("w6_sidecar_path"), sidecar.get("w6_sidecar_sha256"),
        "attempt_recovery_w6_sidecar_missing", "attempt_recovery_w6_sidecar_hash_mismatch")
    try:
        w6 = json.loads(w6_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse("attempt_recovery_w6_sidecar_invalid")
    w6_checks = w6.get("checks") if isinstance(w6, Mapping) else None
    w6_proof = w6_checks.get("persist_existing") if isinstance(w6_checks, Mapping) else None
    if not isinstance(w6_proof, Mapping):
        _refuse("attempt_recovery_w6_proof_missing")
    w6_record_sha = w6_proof.get("record_sha256")
    w6_record_match = re.fullmatch(r"sha256:([0-9a-f]{64})", w6_record_sha) if isinstance(w6_record_sha, str) else None
    w6_ask_row_sha = w6_proof.get("ask_row_sha256")
    if (not w6_record_match or sidecar.get("w6_sha256") != w6_record_sha
            or w6_record_match.group(1) != persistence["record_sha256"]
            or w6_proof.get("question_id") != question_id
            or w6_proof.get("attempt") != attempt_number
            or w6_proof.get("ask_id") != persistence["ask_id"]
            or w6_proof.get("run_id") != run_id
            or w6_proof.get("source_receipt_sha256") != "sha256:" + raw_sha
            or w6_proof.get("recorded_model_usd") != persistence["recorded_model_usd"]
            or w6_proof.get("reservation_release_usd") != persistence["reservation_release_usd"]
            or w6_proof.get("recorded_model_usd_ceiling_micros") != persistence["recorded_model_usd_ceiling_micros"]
            or w6_proof.get("reservation_release_ceiling_micros") != persistence["reservation_release_ceiling_micros"]
            or w6_proof.get("native_net_micros") != CUMULATIVE_CAP_MICROS
            or not isinstance(w6_ask_row_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", w6_ask_row_sha)
            or w6_proof.get("stored_ask_row_sha256", w6_ask_row_sha) != w6_ask_row_sha):
        _refuse("attempt_recovery_w6_hash_mismatch")
    funding_path, funding_bytes = bound_file(
        sidecar.get("funding_proof_path"), sidecar.get("funding_proof_sha256"),
        "attempt_recovery_funding_file_missing", "attempt_recovery_funding_file_hash_mismatch")
    try:
        persisted_funding = json.loads(funding_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse("attempt_recovery_funding_file_invalid")
    if (not isinstance(persisted_funding, Mapping)
            or _canonical(persisted_funding) != _canonical(sidecar.get("funding_proof"))
            or _canonical(_validate_funding_proof(persisted_funding))
            != _canonical(receipt.get("funding_proof"))):
        _refuse("attempt_recovery_funding_proof_mismatch")
    funding = _validate_funding_proof(persisted_funding)
    phase = sidecar.get("native_phase_readback")
    phase_fields = {
        "verified", "phase_id", "run_date", "native_net_micros", "app_run_ids", "consumed_micros",
        "ask_run_id", "credit_run_id", "ask_row_sha256", "credit_row_sha256",
        "guarded_charge_micros", "reservation_run_id", "allocated_micros",
        "source_proof_sha", "baseline_run_ids",
    }
    if not isinstance(phase, Mapping) or not phase_fields.issubset(phase) or phase.get("verified") is not True:
        _refuse("attempt_recovery_phase_readback_invalid")
    if (phase.get("phase_id") != DEMO_PHASE_ID
            or phase.get("run_date") != DEMO_RUN_DATE.isoformat()
            or phase.get("native_net_micros") != CUMULATIVE_CAP_MICROS
            or phase.get("consumed_micros") != guard
            or phase.get("app_run_ids") != [run_id]
            or phase.get("ask_run_id") != run_id
            or phase.get("ask_row_sha256") != w6_ask_row_sha
            or not isinstance(phase.get("credit_run_id"), str) or not phase["credit_run_id"]
            or not isinstance(phase.get("credit_row_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", phase["credit_row_sha256"])
            or phase.get("guarded_charge_micros") != guard
            or phase.get("reservation_run_id") != funding["reservation_run_id"]
            or phase.get("allocated_micros") != funding["allocated_micros"]
            or phase.get("source_proof_sha") != funding["source_proof_sha"]
            or phase.get("baseline_run_ids") != funding["baseline_run_ids"]):
        _refuse("attempt_recovery_phase_readback_mismatch")
    recovered = dict(receipt)
    recovered.update({
        "status": expected_status,
        "guarded_charge_micros": guard,
        "model_usd": model_usd,
        "model_usd_ceiling_micros": model_ceiling,
        "retained_guard_difference_usd": _usd_string(
            Decimal(guard) / Decimal(1_000_000) - _exact_usd(model_usd, "attempt_recovery_model_cost_invalid")),
        "readback": dict(readback),
        "attempt_persistence": persistence,
        "funding_proof": funding,
        "native_phase_readback": dict(phase),
        "failure_type": None,
    })
    return recovered


def _existing_receipts(data_dir, *, profile=None):
    if not data_dir.exists():
        return []
    if (data_dir / ".demo-pairs.dispatching").exists():
        _refuse("dispatch_lock_exists")
    if profile is None:
        markers = sorted(data_dir.glob("*-attempt-*.attempted"))
        receipts = sorted(path for path in data_dir.glob("*-attempt-*.json")
                          if re.fullmatch(r"[^/]+-attempt-\d+\.json", path.name))
        if any(not re.fullmatch(r"DEMO-\d{2}-attempt-\d+\.attempted", path.name) for path in markers):
            _refuse("foreign_execution_profile")
        if any(not re.fullmatch(r"DEMO-\d{2}-attempt-\d+\.json", path.name) for path in receipts):
            _refuse("foreign_execution_profile")
    else:
        markers = sorted(data_dir.glob("*-attempt-*.attempted"))
        receipts = sorted(path for path in data_dir.glob("*-attempt-*.json")
                          if re.fullmatch(r"[^/]+-attempt-\d+\.json", path.name))
    recovery_paths = sorted(data_dir.glob("*-attempt-*.recovery.json"))
    marker_keys = {path.name.removesuffix(".attempted") for path in markers}
    receipt_keys = {path.stem for path in receipts}
    recovery_keys = {path.name.removesuffix(".recovery.json") for path in recovery_paths}
    if marker_keys != receipt_keys:
        _refuse("unresolved_attempt_marker")
    if not recovery_keys.issubset(marker_keys):
        _refuse("orphan_attempt_recovery")
    max_attempts = MAX_TOTAL_ATTEMPTS if profile is None else profile.authorization_slots
    if profile is None and len(markers) > max_attempts:
        _refuse("attempt_limit_exceeded")
    by_key = {}
    for marker in markers:
        key = marker.name.removesuffix(".attempted")
        receipt_path = data_dir / f"{key}.json"
        try:
            original_bytes = receipt_path.read_bytes()
        except OSError:
            _refuse("attempt_receipt_invalid")
        receipt = _read_json(receipt_path, "attempt_receipt_invalid")
        question_id = receipt.get("question_id")
        attempt_number = receipt.get("attempt_number")
        if key != _attempt_key(question_id, attempt_number):
            _refuse("attempt_receipt_identity_mismatch")
        marker_payload = _read_json(marker, "attempt_marker_invalid")
        if marker_payload.get("attempt_key") != key:
            _refuse("attempt_marker_identity_mismatch")
        if profile is None:
            if (marker_payload.get("prompt_source_sha256") != DEMO_QUESTIONS_SHA256
                    or "execution_profile_id" in marker_payload):
                _refuse("foreign_execution_profile")
        elif (marker_payload.get("execution_profile_id") != profile.profile_id
              or marker_payload.get("prompt_source_sha256") != profile.source_proof_sha256
              or marker_payload.get("source_proof_sha256") != profile.source_proof_sha256
              or marker_payload.get("source_go_profile_id") != profile.source_go_profile_id
              or marker_payload.get("authorization_slots") != profile.authorization_slots):
            _refuse("foreign_execution_profile")
        recovery_path = data_dir / f"{key}.recovery.json"
        if recovery_path.exists():
            receipt = _recovered_receipt(data_dir, key, receipt, original_bytes, recovery_path)
        elif key == "DEMO-01-attempt-2":
            reconciled = _reconcile_demo01_attempt_two(data_dir, key, receipt, original_bytes)
            if reconciled is not None:
                receipt = reconciled
        if key in by_key:
            _refuse("duplicate_attempt_marker")
        by_key[key] = receipt
    if profile is not None and len(markers) > max_attempts:
        _refuse("attempt_limit_exceeded")
    ordered = []
    sequence = ATTEMPT_SEQUENCE if profile is None else profile.attempt_sequence
    for question_id, attempt_number in sequence[:len(by_key)]:
        key = _attempt_key(question_id, attempt_number)
        if key not in by_key:
            _refuse("attempt_order_mismatch")
        ordered.append(by_key[key])
    if set(by_key) != {_attempt_key(question_id, attempt_number) for question_id, attempt_number in sequence[:len(ordered)]}:
        _refuse("attempt_order_mismatch")
    return ordered


def _validate_attempt_persistence(value, guarded_micros):
    required = {
        "verified", "ask_id", "run_id", "record_sha256", "guarded_charge_micros",
        "recorded_model_usd", "reservation_release_usd", "recorded_model_usd_ceiling_micros",
        "reservation_release_ceiling_micros", "native_net_micros", "readback",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("verified") is not True:
        _refuse("attempt_persistence_unverified")
    if not isinstance(value.get("ask_id"), str) or not value["ask_id"]:
        _refuse("attempt_persistence_identity_invalid")
    if not isinstance(value.get("run_id"), str) or not value["run_id"]:
        _refuse("attempt_persistence_identity_invalid")
    if not isinstance(value.get("record_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["record_sha256"]):
        _refuse("attempt_persistence_hash_invalid")
    reported = _exact_usd(value.get("recorded_model_usd"), "attempt_persistence_cost_invalid")
    release = _exact_usd(value.get("reservation_release_usd"), "attempt_persistence_release_invalid")
    reported_ceiling = _strict_micros(
        value.get("recorded_model_usd_ceiling_micros"), "attempt_persistence_cost_ceiling_invalid")
    release_ceiling = _strict_micros(
        value.get("reservation_release_ceiling_micros"), "attempt_persistence_release_ceiling_invalid")
    native_net = _strict_micros(value.get("native_net_micros"), "attempt_persistence_net_invalid")
    readback = value.get("readback")
    if not isinstance(readback, Mapping) or readback.get("match") is not True:
        _refuse("attempt_persistence_readback_unverified")
    if (value["run_id"] != readback.get("run_id")
            or value["record_sha256"] != readback.get("record_sha256")
            or _strict_micros(value.get("guarded_charge_micros"), "attempt_persistence_cost_invalid") != guarded_micros
            or value["recorded_model_usd"] != value["reservation_release_usd"]
            or reported_ceiling != _ceil_usd_micros(reported)
            or release_ceiling != _ceil_usd_micros(release)
            or readback.get("recorded_model_usd") != value["recorded_model_usd"]
            or readback.get("reservation_release_usd") != value["reservation_release_usd"]
            or _strict_micros(readback.get("recorded_model_usd_ceiling_micros"), "attempt_persistence_readback_invalid")
            != reported_ceiling
            or _strict_micros(readback.get("reservation_release_ceiling_micros"), "attempt_persistence_readback_invalid")
            != release_ceiling
            or reported_ceiling > guarded_micros
            or _strict_micros(readback.get("native_net_micros"), "attempt_persistence_readback_invalid") != native_net
            or native_net != CUMULATIVE_CAP_MICROS):
        _refuse("attempt_persistence_accounting_mismatch")
    return dict(value)


def _claim_readback_matches(result):
    raw_receipt = result.get("raw_receipt")
    claim_readback = result.get("claim_readback")
    if not isinstance(raw_receipt, Mapping) or not isinstance(claim_readback, Mapping):
        return False
    claims = raw_receipt.get("claim_rows")
    rows = claim_readback.get("rows")
    if claim_readback.get("match") is not True or not isinstance(claims, list) or not isinstance(rows, list):
        return False
    try:
        return Counter(_canonical(claim) for claim in claims) == Counter(_canonical(row) for row in rows)
    except DemoRunRefused:
        return False


def _reconcile_demo01_attempt_two(data_dir, key, receipt, original_bytes):
    if (key != "DEMO-01-attempt-2" or receipt.get("question_id") != "DEMO-01"
            or receipt.get("attempt_number") != 2 or receipt.get("status") != "ambiguous"
            or receipt.get("failure_type") != "claim_readback_mismatch"):
        return None
    dispatch_result = receipt.get("dispatch_result")
    if not isinstance(dispatch_result, Mapping):
        return None
    raw_path_value = dispatch_result.get("raw_receipt_path")
    if not isinstance(raw_path_value, str) or not raw_path_value:
        return None
    root = Path(data_dir).resolve()
    raw_path = Path(raw_path_value)
    if not raw_path.is_absolute():
        raw_path = root / raw_path
    try:
        raw_path = raw_path.resolve(strict=True)
        if not raw_path.is_relative_to(root):
            return None
        raw_bytes = raw_path.read_bytes()
    except OSError:
        return None
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    if dispatch_result.get("raw_receipt_sha256") != raw_sha:
        return None
    try:
        raw_receipt = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    embedded_receipt = dispatch_result.get("raw_receipt")
    if (not isinstance(raw_receipt, Mapping) or not isinstance(embedded_receipt, Mapping)
            or _canonical(raw_receipt) != _canonical(embedded_receipt)):
        return None
    raw_app_record = raw_receipt.get("app_record")
    if (raw_receipt.get("outcome") != "COMPLETE" or raw_receipt.get("unknown_cost") is not False
            or not isinstance(raw_app_record, Mapping)
            or dispatch_result.get("outcome") != "COMPLETE"
            or not _claim_readback_matches(dispatch_result)):
        return None
    budget = receipt.get("budget")
    if not isinstance(budget, Mapping):
        return None
    try:
        cap = _strict_micros(receipt.get("attempt_cap_micros"), "attempt_recovery_cap_invalid")
        budget_cap = _strict_micros(budget.get("cap_micros"), "attempt_recovery_cap_invalid")
        guard = _strict_micros(budget.get("charged_micros"), "attempt_recovery_budget_invalid")
        reserved = _strict_micros(budget.get("reserved_micros"), "attempt_recovery_budget_invalid")
        original_guard = _strict_micros(
            receipt.get("guarded_charge_micros"), "attempt_recovery_budget_invalid")
    except DemoRunRefused:
        return None
    calls = budget.get("calls")
    settled_statuses = {"charged_known", "charged_conservative_ceiling"}
    if (cap != budget_cap or original_guard != cap or guard > cap or reserved != 0
            or budget.get("stop_reason") is not None or not isinstance(calls, list) or not calls
            or any(not isinstance(call, Mapping) or call.get("status") not in settled_statuses for call in calls)):
        return None
    persistence = receipt.get("attempt_persistence")
    try:
        persistence = _validate_attempt_persistence(persistence, guard)
    except DemoRunRefused:
        return None
    if (persistence["run_id"] != raw_receipt.get("run_id")
            or persistence["ask_id"] != raw_app_record.get("ask_id")
            or receipt.get("model_usd") != persistence["recorded_model_usd"]
            or receipt.get("model_usd_ceiling_micros") != persistence["recorded_model_usd_ceiling_micros"]
            or not isinstance(receipt.get("readback"), Mapping)
            or _canonical(receipt["readback"]) != _canonical(persistence["readback"])):
        return None
    reconciled = dict(receipt)
    reconciled.update({
        "status": "COMPLETE",
        "guarded_charge_micros": guard,
        "attempt_persistence": persistence,
        "original_receipt_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "raw_receipt_sha256": raw_sha,
        "reconciliation_reason": "claim_readback_order_only",
    })
    return reconciled


def _write_exclusive(path, payload):
    data = (_canonical(payload) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_receipt(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    _write_exclusive(temporary, payload)
    os.replace(temporary, path)


def _call_statuses(budget):
    return [str(call.get("status")) for call in budget.calls]


def _was_dispatched(budget):
    dispatched = {"dispatched", "charged_known", "unknown_charged_ceiling",
                  "actual_cost_above_reserve", "session_cap_breached"}
    return any(status in dispatched for status in _call_statuses(budget))


def _receipt_base(request, proof, attempt_cap_micros, budget, context, *, profile=None, source_commit=None):
    receipt = {
        "schema_version": "demo-pair-attempt-v1",
        "prompt_source_sha256": (DEMO_QUESTIONS_SHA256 if profile is None
                                  else profile.source_proof_sha256),
        "question_id": request.question_id,
        "attempt_number": request.attempt_number,
        "attempt_key": request.attempt_key,
        "prompt": request.prompt,
        "market": request.market,
        "source_ids": list(request.source_ids),
        "support_run_id": request.support_run_id,
        "provided_context": _json_value(context),
        "previous_attempt": _json_value(request.previous_attempt),
        "funding_proof": proof,
        "attempt_cap_micros": attempt_cap_micros,
        "budget": {
            "cap_micros": budget.cap_micros,
            "charged_micros": budget.charged_micros,
            "reserved_micros": budget.reserved_micros,
            "stop_reason": budget.stop_reason,
            "calls": _json_value(budget.calls),
        },
    }
    if profile is not None:
        receipt.update({
            "execution_profile_id": profile.profile_id,
            "source_proof_sha256": profile.source_proof_sha256,
            "source_go_profile_id": profile.source_go_profile_id,
            "authorization_slots": profile.authorization_slots,
            "source_commit": source_commit,
        })
    return receipt


def run_attempt(*, question_id, attempt_number, data_dir, funding_proof,
                provided_context, dispatch, persist_attempt, price_for=None, clock=None,
                profile=None, prior_data_dir=None, ranking_proof_bytes=None, source_commit=None):
    profile = _execution_profile(profile)
    if profile is None:
        if prior_data_dir is not None or ranking_proof_bytes is not None or source_commit is not None:
            _refuse("ranked_profile_inputs_unexpected")
    else:
        if profile == RANKED_NOW_ONCE_PROFILE:
            _validate_ranked_proof_bytes(profile, ranking_proof_bytes)
        else:
            _validate_trending_profile_proof_bytes(profile, ranking_proof_bytes)
        if (not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit)
                or prior_data_dir is None):
            _refuse("ranked_source_provenance_invalid")
    questions = DEMO_QUESTIONS if profile is None else profile.question_catalog
    attempt_sequence = ATTEMPT_SEQUENCE if profile is None else profile.attempt_sequence
    question_by_id = {question.id: question for question in questions}
    question = question_by_id.get(question_id)
    if question is None:
        _refuse("demo_question_unknown" if profile is None else "ranked_attempt_not_authorized")
    if type(attempt_number) is not int:
        _refuse("attempt_number_invalid" if profile is None else "ranked_attempt_not_authorized")
    if profile is None:
        if attempt_number not in range(1, MAX_ATTEMPTS + 1):
            _refuse("attempt_number_invalid")
    elif (question_id, attempt_number) not in attempt_sequence:
        _refuse("ranked_attempt_not_authorized")
    if not callable(dispatch) or not callable(persist_attempt):
        _refuse("attempt_adapter_missing")
    _validate_context(question, provided_context)
    proof = _validate_funding_proof(funding_proof)
    _check_demo_day(proof, clock=clock)
    data_dir = Path(data_dir)
    if profile is None:
        prior_data_dir = data_dir
        prior_receipts = _existing_receipts(data_dir)
        active_receipts = prior_receipts
    else:
        prior_data_dir = Path(prior_data_dir)
        if prior_data_dir.resolve() == data_dir.resolve():
            _refuse("ranked_data_dir_must_be_separate")
        prior_receipts = _existing_receipts(prior_data_dir)
        active_receipts = _existing_receipts(data_dir, profile=profile)
    attempt_key = _attempt_key(question_id, attempt_number)
    marker_path, receipt_path = _attempt_paths(data_dir, question_id, attempt_number)
    if marker_path.exists() or receipt_path.exists():
        _refuse("duplicate_attempt_marker")
    if profile is None:
        expected_slot = (attempt_sequence[len(active_receipts)]
                         if len(active_receipts) < MAX_TOTAL_ATTEMPTS else None)
        if expected_slot != (question_id, attempt_number):
            _refuse("attempt_order_mismatch")
    previous_attempt = None
    if attempt_number == 2:
        previous = prior_receipts[-1] if prior_receipts else None
        if (not previous or previous.get("question_id") != question_id
                or previous.get("attempt_number") != 1
                or previous.get("status") in {"ambiguous", "failed_before_dispatch"}
                or not isinstance(previous.get("attempt_persistence"), dict)
                or previous["attempt_persistence"].get("verified") is not True
                or not isinstance(previous.get("readback"), dict)
                or previous["readback"].get("match") is not True):
            _refuse("followup_requires_persisted_attempt")
        previous_attempt = {
            "attempt_number": 1,
            "status": previous["status"],
            "attempt_persistence": previous["attempt_persistence"],
        }
    prior_consumed = sum(_strict_micros(receipt.get("guarded_charge_micros"), "attempt_receipt_cost_invalid")
                         for receipt in prior_receipts)
    prior_app_ids = [receipt["attempt_persistence"]["run_id"] for receipt in prior_receipts
                     if isinstance(receipt.get("attempt_persistence"), dict)
                     and receipt["attempt_persistence"].get("run_id")]
    active_consumed = 0
    active_app_ids = []
    if profile is not None:
        if any(receipt["attempt_persistence"].get("verified") is not True
               for receipt in prior_receipts if isinstance(receipt.get("attempt_persistence"), dict)):
            _refuse("ranked_prior_persistence_unverified")
        if (prior_consumed != profile.prior_guarded_micros
                or len(prior_app_ids) != profile.prior_verified_app_count):
            _refuse("ranked_funding_baseline_mismatch")
        if profile == RANKED_NOW_ONCE_PROFILE and active_receipts:
            _refuse("duplicate_attempt_marker")
        if any(not _profile_receipt_is_verified(receipt, profile) for receipt in active_receipts):
            _refuse("profile_prior_attempt_unverified")
        expected_slot = (attempt_sequence[len(active_receipts)]
                         if len(active_receipts) < profile.authorization_slots else None)
        if expected_slot != (question_id, attempt_number):
            _refuse("ranked_attempt_not_authorized")
        active_consumed = sum(receipt["guarded_charge_micros"] for receipt in active_receipts)
        active_app_ids = [receipt["attempt_persistence"]["run_id"] for receipt in active_receipts]
    known_consumed = prior_consumed + active_consumed
    expected_app_ids = [*prior_app_ids, *active_app_ids]
    if proof["consumed_micros"] != known_consumed or proof["app_run_ids"] != expected_app_ids:
        _refuse("funding_consumption_readback_mismatch")
    remaining = proof["allocated_micros"] - proof["consumed_micros"]
    attempt_cap_micros = (profile.attempt_cap_micros if profile is not None
                          else min(proof["allocated_micros"] // MAX_TOTAL_ATTEMPTS, remaining))
    if profile is not None and remaining < attempt_cap_micros:
        _refuse("attempt_cap_exhausted")
    if attempt_cap_micros <= 0:
        _refuse("attempt_cap_exhausted")
    budget = SessionBudget(cap_usd=attempt_cap_micros / 1_000_000,
                           price_for=price_for, provider_500_retries=0)
    budget.provider_retry_statuses = ()
    request = DemoAttemptRequest(
        question_id=question.id,
        attempt_number=attempt_number,
        attempt_key=attempt_key,
        prompt=question.text,
        market=question.market,
        source_ids=question.source_ids,
        support_run_id=question.support_run_id,
        provided_context=provided_context,
        previous_attempt=previous_attempt,
        attempt_cap_micros=attempt_cap_micros,
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".demo-pairs.dispatching"
    try:
        _write_exclusive(lock_path, {"attempt_key": attempt_key, "state": "dispatching"})
    except FileExistsError:
        _refuse("dispatch_lock_exists")
    receipt_written = False
    try:
        marker = {
            "schema_version": "demo-pair-marker-v1",
            "attempt_key": attempt_key,
            "prompt_source_sha256": DEMO_QUESTIONS_SHA256,
            "attempt_cap_micros": attempt_cap_micros,
        }
        if profile is not None:
            marker.update({
                "schema_version": ("demo-pair-ranked-marker-v1"
                                    if profile == RANKED_NOW_ONCE_PROFILE
                                    else "demo-pair-profile-marker-v1"),
                "prompt_source_sha256": profile.source_proof_sha256,
                "execution_profile_id": profile.profile_id,
                "source_proof_sha256": profile.source_proof_sha256,
                "source_go_profile_id": profile.source_go_profile_id,
                "authorization_slots": profile.authorization_slots,
                "source_commit": source_commit,
            })
        _write_exclusive(marker_path, marker)
        try:
            result = dispatch(request, budget)
        except Exception as exc:
            dispatched = _was_dispatched(budget)
            receipt = _receipt_base(request, proof, attempt_cap_micros, budget, provided_context,
                                    profile=profile, source_commit=source_commit)
            receipt.update({
                "status": "ambiguous" if dispatched else "failed_before_dispatch",
                "guarded_charge_micros": max(budget.charged_micros, attempt_cap_micros if dispatched else 0),
                "model_usd": None,
                "model_usd_ceiling_micros": None,
                "retained_guard_difference_usd": None,
                "readback": None,
                "attempt_persistence": None,
                "dispatch_result": None,
                "failure_type": type(exc).__name__,
            })
            _write_receipt(receipt_path, receipt)
            receipt_written = True
            return receipt
        if not isinstance(result, Mapping):
            result = {"adapter_result_invalid": True}
        statuses = _call_statuses(budget)
        unknown_cost = (budget.stop_reason == "unknown_dispatched_cost"
                        or "unknown_charged_ceiling" in statuses)
        dispatched = _was_dispatched(budget)
        if not dispatched or not budget.calls:
            receipt = _receipt_base(request, proof, attempt_cap_micros, budget, provided_context,
                                    profile=profile, source_commit=source_commit)
            receipt.update({
                "status": "failed_before_dispatch",
                "guarded_charge_micros": 0,
                "model_usd": None,
                "model_usd_ceiling_micros": None,
                "retained_guard_difference_usd": None,
                "readback": None,
                "attempt_persistence": None,
                "dispatch_result": _json_value(result),
                "failure_type": "model_budget_not_used",
            })
            _write_receipt(receipt_path, receipt)
            receipt_written = True
            return receipt
        guarded_micros = budget.charged_micros
        if unknown_cost:
            guarded_micros = max(guarded_micros, attempt_cap_micros if budget.reserved_micros else 0)
        if budget.charged_micros + budget.reserved_micros > attempt_cap_micros:
            guarded_micros = budget.charged_micros + budget.reserved_micros
        receipt = _receipt_base(request, proof, attempt_cap_micros, budget, provided_context,
                                profile=profile, source_commit=source_commit)
        readback = None
        reported = None
        persistence = None
        failure_type = None
        status = "unknown_cost" if unknown_cost else str(
            result.get("status") or result.get("outcome") or ("complete" if result.get("answer") is not None else "no_answer"))
        try:
            persisted_input = dict(result)
            persisted_input["guarded_charge_micros"] = guarded_micros
            persisted_input["guarded_call_costs"] = _json_value(budget.calls)
            persistence = persist_attempt(request, persisted_input)
            persistence = _validate_attempt_persistence(persistence, guarded_micros)
            reported = persistence["recorded_model_usd"]
            readback = persistence["readback"]
            if not _claim_readback_matches(result):
                _refuse("claim_readback_mismatch")
            if budget.reserved_micros or budget.charged_micros > attempt_cap_micros:
                status = "ambiguous"
                _refuse("attempt_budget_unsettled")
            settled_statuses = {"charged_known", "charged_conservative_ceiling"}
            if any(item not in settled_statuses for item in statuses) and not unknown_cost:
                status = "ambiguous"
                _refuse("attempt_call_status_invalid")
            if unknown_cost:
                status = "unknown_cost"
        except Exception as exc:
            status = "ambiguous" if dispatched else "failed_before_dispatch"
            guarded_micros = max(attempt_cap_micros, guarded_micros)
            failure_type = exc.args[0] if isinstance(exc, DemoRunRefused) and exc.args else type(exc).__name__
        receipt.update({
            "status": status,
            "guarded_charge_micros": guarded_micros,
            "model_usd": reported,
            "model_usd_ceiling_micros": (
                persistence["recorded_model_usd_ceiling_micros"] if persistence is not None else None),
            "retained_guard_difference_usd": (
                _usd_string(Decimal(guarded_micros) / Decimal(1_000_000)
                            - _exact_usd(reported, "attempt_persistence_cost_invalid"))
                if reported is not None and persistence is not None else None),
            "readback": readback,
            "attempt_persistence": persistence,
            "dispatch_result": _json_value(result),
            "failure_type": failure_type,
        })
        _write_receipt(receipt_path, receipt)
        receipt_written = True
        return receipt
    finally:
        if receipt_written:
            lock_path.unlink(missing_ok=True)
