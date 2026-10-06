"""Pure policy and intake validation for admitted general questions."""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_request import (
    QuestionRequestInvalid,
    validate_question_request,
)

_POLICY_VERSION = "general_question_policy_v1"
_POLICY_ID = "general_cultural_question_staging_eval_v1"
_LEGACY_APPROVAL_CONTRACT_DIGEST = (
    "a0410e47911ff343ef77726e4ffefbfd57f275e9cf341633a0a9fd1b01e506e0"
)
_LOW_PLANNING_CONTRACT_DIGEST = "99c597f7a083ea14d4c87c34bbccdb6d5cea5958ba466352891b8d06731c4389"
_MEDIUM_ANSWER_CONTRACT_DIGEST = "1306582d25499311bd70c59e1e25804b5219f7494a30a18a59b03c9ad3d474c5"
_LOW_ANSWER_CONTRACT_DIGEST = "519a658ad91b3b4d447298611d259421bbac5b7765d7c0f0049dc8bdd08c5857"
_APPROVAL_CONTRACT_DIGEST = "2885012a99bfc352c9ad92ced7b5bddd0b022f88e8ddc9d7688f237179062a8b"
_CONTRACT_MODELS = {
    _LEGACY_APPROVAL_CONTRACT_DIGEST: "gemini-3.5-flash",
    _LOW_PLANNING_CONTRACT_DIGEST: "gemini-3.5-flash",
    _MEDIUM_ANSWER_CONTRACT_DIGEST: "gemini-3.5-flash",
    _LOW_ANSWER_CONTRACT_DIGEST: "gemini-3.5-flash",
    _APPROVAL_CONTRACT_DIGEST: "gemini-3.8-flash",
}
_PROJECT = "ogilvy-trends-v2"
_LOCATION = "global"
_API_VERSION = "v1"
_SERVICE_TIER = "standard"
_EXPIRES_AT = "2026-09-30T23:59:59Z"
# The one later expiry a policy may carry. A policy with it is written only by
# replacing the active policy's expiry and nothing else, so a policy with the
# fixed expiry stays valid until the replacement is active.
EXTENDED_EXPIRES_AT = "2026-12-31T23:59:59Z"
_ACCEPTED_EXPIRES_AT = (_EXPIRES_AT, EXTENDED_EXPIRES_AT)
_PRICING_SOURCE = "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing"
_PRICE = re.compile(r"[0-9]+\.[0-9]{6}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
QUESTION_DEADLINE_SECONDS = 240
_SUPPORTED_DEADLINE_SECONDS = frozenset({180, QUESTION_DEADLINE_SECONDS})
_POLICY_FIELDS = frozenset(
    {
        "contract_version",
        "policy_id",
        "approval_contract_digest",
        "policy_digest",
        "model",
        "project",
        "location",
        "api_version",
        "service_tier",
        "expires_at",
        "limits",
        "stages",
        "pricing",
    }
)
_LIMITS = {
    "requests": 100,
    "reserved_microusd": 10_000_000,
    "request_microusd": 100_000,
    "calls_per_request": 2,
    "input_tokens": 32_000,
    "output_tokens": 4_000,
    "query_jobs": 8,
    "billed_bytes": 1_000_000_000,
    "candidate_records": 1_000,
    "evidence_records": 200,
    "deadline_seconds": QUESTION_DEADLINE_SECONDS,
    "model_timeout_seconds": 60,
}
_STAGE_FIELDS = frozenset({"thinking_level", "input_tokens", "output_tokens"})
_PRICING_FIELDS = frozenset(
    {"input_usd_per_million", "output_usd_per_million", "verified_at", "source_url"}
)
_INTAKE_VERSION = "general_question_intake_context_v1"
_INTAKE_FIELDS = frozenset(
    {
        "contract_version",
        "request_id",
        "request_digest",
        "selected_market",
        "captured_at",
        "intake_digest",
    }
)
_REQUEST_SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
)


def _aware_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} is invalid")
    try:
        if value.utcoffset() is None:
            raise ValueError(f"{label} is invalid")
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    return value.astimezone(UTC)


def _format_timestamp(value: object, label: str) -> str:
    return _aware_datetime(value, label).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if _format_timestamp(parsed, label) != value:
        raise ValueError(f"{label} is invalid")
    return parsed


def _price(value: object) -> Decimal:
    if not isinstance(value, str) or _PRICE.fullmatch(value) is None:
        raise ValueError("pricing is invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("pricing is invalid") from exc
    if parsed <= 0:
        raise ValueError("pricing is invalid")
    return parsed


def _expected_stages(contract_digest=_APPROVAL_CONTRACT_DIGEST) -> dict[str, dict[str, object]]:
    output = {
        "planning": {"thinking_level": "MEDIUM", "input_tokens": 8_000, "output_tokens": 800},
        "answering": {"thinking_level": "HIGH", "input_tokens": 32_000, "output_tokens": 4_000},
    }
    if contract_digest in (
        _APPROVAL_CONTRACT_DIGEST,
        _LOW_ANSWER_CONTRACT_DIGEST,
        _MEDIUM_ANSWER_CONTRACT_DIGEST,
        _LOW_PLANNING_CONTRACT_DIGEST,
    ):
        output["planning"]["thinking_level"] = "LOW"
    if contract_digest == _MEDIUM_ANSWER_CONTRACT_DIGEST:
        output["answering"]["thinking_level"] = "MEDIUM"
    if contract_digest in (_APPROVAL_CONTRACT_DIGEST, _LOW_ANSWER_CONTRACT_DIGEST):
        output["answering"]["thinking_level"] = "LOW"
    return output


def build_question_policy(
    *,
    pricing_verified_at: datetime,
    input_usd_per_million: str | None = None,
    output_usd_per_million: str | None = None,
    approval_contract_digest: str = _APPROVAL_CONTRACT_DIGEST,
    expires_at: str = _EXPIRES_AT,
) -> dict:
    if (
        not isinstance(approval_contract_digest, str)
        or approval_contract_digest not in _CONTRACT_MODELS
    ):
        raise ValueError("question policy binding is invalid")
    if not isinstance(expires_at, str) or expires_at not in _ACCEPTED_EXPIRES_AT:
        raise ValueError("question policy binding is invalid")
    model = _CONTRACT_MODELS[approval_contract_digest]
    if input_usd_per_million is None:
        input_usd_per_million = "0.750000" if model == "gemini-3.8-flash" else "1.500000"
    if output_usd_per_million is None:
        output_usd_per_million = "3.750000" if model == "gemini-3.8-flash" else "9.000000"
    _price(input_usd_per_million)
    _price(output_usd_per_million)
    policy = {
        "contract_version": _POLICY_VERSION,
        "policy_id": _POLICY_ID,
        "approval_contract_digest": approval_contract_digest,
        "model": model,
        "project": _PROJECT,
        "location": _LOCATION,
        "api_version": _API_VERSION,
        "service_tier": _SERVICE_TIER,
        "expires_at": expires_at,
        "limits": dict(_LIMITS),
        "stages": _expected_stages(approval_contract_digest),
        "pricing": {
            "input_usd_per_million": input_usd_per_million,
            "output_usd_per_million": output_usd_per_million,
            "verified_at": _format_timestamp(pricing_verified_at, "pricing verified_at"),
            "source_url": _PRICING_SOURCE,
        },
    }
    policy["policy_digest"] = canonical_digest(policy)
    return validate_question_policy(policy)


def validate_question_policy(value) -> dict:
    if not isinstance(value, dict) or set(value) != _POLICY_FIELDS:
        raise ValueError("question policy fields are invalid")
    contract_digest = value["approval_contract_digest"]
    if not isinstance(contract_digest, str) or contract_digest not in _CONTRACT_MODELS:
        raise ValueError("question policy binding is invalid")
    expected_scalars = {
        "contract_version": _POLICY_VERSION,
        "policy_id": _POLICY_ID,
        "approval_contract_digest": contract_digest,
        "model": _CONTRACT_MODELS[contract_digest],
        "project": _PROJECT,
        "location": _LOCATION,
        "api_version": _API_VERSION,
        "service_tier": _SERVICE_TIER,
    }
    if any(value[field] != expected for field, expected in expected_scalars.items()):
        raise ValueError("question policy binding is invalid")
    expires_at = value["expires_at"]
    if not isinstance(expires_at, str) or expires_at not in _ACCEPTED_EXPIRES_AT:
        raise ValueError("question policy binding is invalid")

    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != set(_LIMITS):
        raise ValueError("question policy limits are invalid")
    if any(
        type(limits[field]) is not int or limits[field] != expected
        for field, expected in _LIMITS.items()
        if field != "deadline_seconds"
    ) or (
        type(limits["deadline_seconds"]) is not int
        or limits["deadline_seconds"] not in _SUPPORTED_DEADLINE_SECONDS
        or (
            contract_digest == _APPROVAL_CONTRACT_DIGEST
            and limits["deadline_seconds"] != QUESTION_DEADLINE_SECONDS
        )
    ):
        raise ValueError("question policy limits are invalid")

    stages = value["stages"]
    expected_stages = _expected_stages(contract_digest)
    if not isinstance(stages, dict) or set(stages) != set(expected_stages):
        raise ValueError("question policy stages are invalid")
    for stage, expected in expected_stages.items():
        actual = stages[stage]
        if (
            not isinstance(actual, dict)
            or set(actual) != _STAGE_FIELDS
            or actual != expected
            or type(actual["input_tokens"]) is not int
            or type(actual["output_tokens"]) is not int
        ):
            raise ValueError("question policy stages are invalid")

    pricing = value["pricing"]
    if not isinstance(pricing, dict) or set(pricing) != _PRICING_FIELDS:
        raise ValueError("question policy pricing fields are invalid")
    _price(pricing["input_usd_per_million"])
    _price(pricing["output_usd_per_million"])
    _parse_timestamp(pricing["verified_at"], "pricing verified_at")
    if pricing["source_url"] != _PRICING_SOURCE:
        raise ValueError("question policy pricing source is invalid")

    digest = value["policy_digest"]
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or digest
        != canonical_digest({key: item for key, item in value.items() if key != "policy_digest"})
    ):
        raise ValueError("question policy digest is invalid")
    return copy.deepcopy(value)


def require_question_admission_policy(value, *, now: datetime) -> dict:
    policy = validate_question_policy(value)
    current = _aware_datetime(now, "admission now")
    verified = _parse_timestamp(policy["pricing"]["verified_at"], "pricing verified_at")
    if verified > current:
        raise ValueError("pricing review is in the future")
    if current - verified > timedelta(hours=24):
        raise ValueError("pricing review is stale")
    expires = datetime.fromisoformat(policy["expires_at"].replace("Z", "+00:00"))
    if current > expires:
        raise ValueError("question policy is expired")
    maximum_microusd = (
        _price(policy["pricing"]["input_usd_per_million"]) * policy["limits"]["input_tokens"]
        + _price(policy["pricing"]["output_usd_per_million"]) * policy["limits"]["output_tokens"]
    )
    if maximum_microusd > policy["limits"]["request_microusd"]:
        raise ValueError("question policy cost exceeds request reservation")
    return policy


def _validated_request(request) -> dict:
    try:
        if not isinstance(request, dict):
            raise QuestionRequestInvalid()
        scope = {field: request[field] for field in _REQUEST_SCOPE_FIELDS}
        policy_digest = request["policy_digest"]
        return validate_question_request(request, scope=scope, policy_digest=policy_digest)
    except QuestionRequestInvalid:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise QuestionRequestInvalid() from exc


def _validate_selection(selected_market: object, request: dict) -> None:
    if selected_market is None:
        return
    if (
        not isinstance(selected_market, str)
        or not selected_market
        or selected_market != selected_market.strip()
        or selected_market not in request["market_scope"]
    ):
        raise QuestionRequestInvalid("scope_invalid")


def build_intake_context(
    request, *, selected_market: str | None, parent_context_ref=None, source_window=False
) -> dict:
    normalized = _validated_request(request)
    _validate_selection(selected_market, normalized)
    intake = {
        "contract_version": _INTAKE_VERSION
        if parent_context_ref is None
        else "general_question_intake_context_v2",
        "request_id": normalized["request_id"],
        "request_digest": normalized["request_digest"],
        "selected_market": selected_market,
        "captured_at": normalized["as_of"],
    }
    if parent_context_ref is not None:
        from src.analysis.open_intelligence.general_question_parent_capsule import (
            validate_context_ref,
        )

        intake["parent_context_ref"] = copy.deepcopy(validate_context_ref(parent_context_ref))
    if source_window:
        from src.analysis.open_intelligence.general_question_context_admission import (
            source_window_hint,
        )

        intake["contract_version"] = "general_question_intake_context_v3"
        intake["source_window_hint"] = source_window_hint(normalized["as_of"])
    intake["intake_digest"] = canonical_digest(intake)
    return validate_intake_context(intake, request=normalized)


def validate_intake_context(value, *, request) -> dict:
    normalized = _validated_request(request)
    v2 = (
        isinstance(value, dict)
        and value.get("contract_version") == "general_question_intake_context_v2"
    )
    v3 = (
        isinstance(value, dict)
        and value.get("contract_version") == "general_question_intake_context_v3"
    )
    parent = v2 or (v3 and "parent_context_ref" in value)
    if not isinstance(value, dict) or set(value) != _INTAKE_FIELDS | (
        {"parent_context_ref"} if parent else set()
    ) | ({"source_window_hint"} if v3 else set()):
        raise QuestionRequestInvalid()
    if (
        value["contract_version"]
        != (
            "general_question_intake_context_v3"
            if v3
            else "general_question_intake_context_v2"
            if v2
            else _INTAKE_VERSION
        )
        or value["request_id"] != normalized["request_id"]
        or value["request_digest"] != normalized["request_digest"]
        or value["captured_at"] != normalized["as_of"]
    ):
        raise QuestionRequestInvalid()
    _validate_selection(value["selected_market"], normalized)
    if parent:
        from src.analysis.open_intelligence.general_question_parent_capsule import (
            validate_context_ref,
        )

        validate_context_ref(value["parent_context_ref"])
    if v3:
        from src.analysis.open_intelligence.general_question_context_admission import (
            source_window_hint,
        )

        source_window_hint(
            normalized["as_of"], retained=value["source_window_hint"], validate_retained=True
        )
    digest = value["intake_digest"]
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or digest
        != canonical_digest({key: item for key, item in value.items() if key != "intake_digest"})
    ):
        raise QuestionRequestInvalid()
    return copy.deepcopy(value)


__all__ = [
    "EXTENDED_EXPIRES_AT",
    "QUESTION_DEADLINE_SECONDS",
    "build_intake_context",
    "build_question_policy",
    "require_question_admission_policy",
    "validate_intake_context",
    "validate_question_policy",
]
