"""The Fieldwork read projection.

Fieldwork answers five questions for a strategist: what are we trying to learn,
what will be observed, which source coverage exists, what blocks evidence
readiness, and what is the next safe operation. It is a read. It creates no
investigation, source call, vendor call, research job, model call, approval
record, artifact, data row, reservation or budget debit.

Every value here is either observed or explicitly unavailable. Nothing is
defaulted to zero, empty or ready to make the workspace look complete: an
absent input is a fact a strategist needs, not a blank to be filled.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

CONTRACT_VERSION = "fieldwork_workspace_v1"

# The complete set the contract allows. A caller cannot widen it, and a value
# outside it is a contract violation rather than a new state.
WORKSPACE_STATES = (
    "ready",
    "empty",
    "no_discovery",
    "unavailable_source",
    "budget_exhausted",
)
UNAVAILABLE_INPUTS = (
    "source_lab",
    "investigation_store",
    "claim_ledger",
    "budget_authority",
    "historical",
)
FRESHNESS_STATES = ("current", "stale", "unavailable")
BUDGET_STATES = (
    "not_required",
    "unconfigured",
    "available",
    "held",
    "exhausted",
    "unavailable",
)

# At most 50 research rows are returned. The complete count is reported
# separately so a truncated view can never read as the whole set.
RESEARCH_ROW_LIMIT = 50


class FieldworkContractError(ValueError):
    """The request or the projection violates the Fieldwork contract."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )


def _resource_version(payload: dict[str, Any]) -> str:
    """Identity of this response, excluding the fields that always move.

    generated_at and resource_version are excluded so two identical readings
    carry the same identity and a strategist is not shown a change that did not
    happen.
    """
    subject = {
        key: value
        for key, value in payload.items()
        if key not in ("generated_at", "resource_version")
    }
    return "fwr_" + hashlib.sha256(_canonical(subject).encode("utf-8")).hexdigest()


def _unavailable_freshness(reasons: tuple[str, ...]) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "source_lab_checked_at": None,
        "investigations_updated_at": None,
        "oldest_operation_updated_at": None,
        "reasons": list(reasons),
    }


def _empty_operation_summary() -> dict[str, int]:
    # Every count is a real count of the rows returned. With no rows, each is a
    # measured zero rather than a stand-in for something unknown.
    return {
        "total": 0,
        "active": 0,
        "held": 0,
        "complete": 0,
        "killed": 0,
        "unavailable": 0,
    }


def _unavailable_budget(reasons: tuple[str, ...]) -> dict[str, Any]:
    """Budget with no authoritative evidence.

    Never 'available' and never 'exhausted'. Exhaustion is a claim about
    measured credits, and a visible balance without attribution is not that.
    """
    return {
        "state": "unavailable",
        "funding_lane": None,
        "credit_ceiling": None,
        "credits_reserved": None,
        "credits_used": None,
        "credits_remaining": None,
        "reserve_floor": None,
        "observed_at": None,
        "reasons": list(reasons),
    }


def read_fieldwork_workspace(
    *,
    client_scope_id: str,
    market_scope: tuple[str, ...],
    now: datetime | None = None,
) -> dict[str, Any]:
    """The Fieldwork projection for the server-resolved scope.

    The scope is a parameter of the server, never of the caller. Today the
    operation inputs are not wired, so the workspace reports
    `unavailable_source` and names every input it could not read. That is the
    honest answer, and it is deliberately not `empty`: empty would say the
    inputs were read and held nothing.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    reasons = ("fieldwork_operation_inputs_unavailable",)
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "resource_version": "",
        "client_scope_id": client_scope_id,
        "market_scope": list(market_scope),
        "workspace_state": "unavailable_source",
        "generated_at": stamp.isoformat().replace("+00:00", "Z"),
        "freshness": _unavailable_freshness(reasons),
        "operation_summary": _empty_operation_summary(),
        "budget_summary": _unavailable_budget(reasons),
        "source_operations": [],
        "research_operations": [],
        "research_operation_count": 0,
        "research_operations_truncated": False,
        "unavailable_inputs": ["source_lab", "investigation_store"],
    }
    payload["resource_version"] = _resource_version(payload)
    validate_fieldwork_payload(payload)
    return payload


def validate_fieldwork_payload(payload: dict[str, Any]) -> None:
    """The response contract, asserted on the way out.

    A projection that drifts from this shape is a defect that would reach a
    strategist, so it fails here rather than rendering.
    """
    expected_order = (
        "contract_version",
        "resource_version",
        "client_scope_id",
        "market_scope",
        "workspace_state",
        "generated_at",
        "freshness",
        "operation_summary",
        "budget_summary",
        "source_operations",
        "research_operations",
        "research_operation_count",
        "research_operations_truncated",
        "unavailable_inputs",
    )
    if tuple(payload) != expected_order:
        raise FieldworkContractError("fieldwork response field order is invalid")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise FieldworkContractError("fieldwork contract version is unsupported")
    if payload["workspace_state"] not in WORKSPACE_STATES:
        raise FieldworkContractError("fieldwork workspace state is unsupported")
    if payload["freshness"]["state"] not in FRESHNESS_STATES:
        raise FieldworkContractError("fieldwork freshness state is unsupported")
    if payload["budget_summary"]["state"] not in BUDGET_STATES:
        raise FieldworkContractError("fieldwork budget state is unsupported")
    summary = payload["operation_summary"]
    if summary["total"] != sum(
        summary[key] for key in ("active", "held", "complete", "killed", "unavailable")
    ):
        raise FieldworkContractError("fieldwork operation summary does not reconcile")
    rows = payload["research_operations"]
    if len(rows) > RESEARCH_ROW_LIMIT:
        raise FieldworkContractError(
            "fieldwork returned more research rows than the limit"
        )
    if payload["research_operations_truncated"] != (
        payload["research_operation_count"] > len(rows)
    ):
        raise FieldworkContractError(
            "fieldwork truncation flag does not match the counts"
        )
    unavailable = payload["unavailable_inputs"]
    if len(set(unavailable)) != len(unavailable):
        raise FieldworkContractError("fieldwork unavailable inputs must be unique")
    if any(item not in UNAVAILABLE_INPUTS for item in unavailable):
        raise FieldworkContractError("fieldwork unavailable input is unsupported")


CONTRACT_VERSION_V2 = "fieldwork_workspace_v2"
OPERATION_STATUSES = (
    "planned",
    "running",
    "unconfirmed",
    "complete",
    "partial",
    "needs_clarification",
    "refused",
    "held",
    "killed",
    "unavailable",
)
_SCOPE_FIELDS = {
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
}
_COVERAGE_FIELDS = {
    "state",
    "observed_at",
    "window",
    "known_count",
    "known_by_status",
    "total_count",
    "returned_count",
    "unread_count",
    "invalid_count",
    "reasons",
}
_COVERAGE_REASONS = {
    "not_wired",
    "listing_unavailable",
    "record_unavailable",
    "record_invalid",
    "read_limit_reached",
    "read_deadline_reached",
}
_INPUT_TYPES = {
    "general_questions": "general_question",
    "investigation_plans": "investigation_plan",
    "source_executions": None,
}
_ROW_FIELDS = {
    "operation_id",
    "kind",
    "operation_type",
    "entity_id",
    "title",
    "status",
    "market_scope",
    "updated_at",
    "evidence_readiness",
    "gaps",
    "next_operation",
}


def _require_v2(condition: bool) -> None:
    if not condition:
        raise FieldworkContractError("fieldwork v2 observation is invalid")


def _keys_v2(value: Any, fields: set[str]) -> None:
    _require_v2(isinstance(value, dict) and set(value) == fields)


def _integer_v2(value: Any, *, nullable: bool = False) -> None:
    _require_v2((nullable and value is None) or (type(value) is int and value >= 0))


def _text_v2(value: Any, *, nullable: bool = False) -> None:
    _require_v2(
        (nullable and value is None) or (isinstance(value, str) and bool(value.strip()))
    )


def _timestamp_v2(value: Any, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _text_v2(value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FieldworkContractError("fieldwork timestamp is invalid") from error
    _require_v2(
        "T" in value
        and parsed.utcoffset() is not None
        and parsed.utcoffset().total_seconds() == 0
    )


def _strings_v2(value: Any, *, unique: bool = False) -> None:
    _require_v2(isinstance(value, list))
    for item in value:
        _text_v2(item)
    if unique:
        _require_v2(len(value) == len(set(value)))


def _scope_v2(scope: Any) -> None:
    _keys_v2(scope, _SCOPE_FIELDS)
    _text_v2(scope["client_scope_id"])
    for key in ("brand_config_id", "theme_id"):
        _text_v2(scope[key], nullable=True)
    _strings_v2(scope["audience_lens_ids"], unique=True)
    markets = scope["market_scope"]
    _strings_v2(markets, unique=True)
    _require_v2(bool(markets) and set(markets) <= {"za", "ng", "ke"})


def _status_counts_v2(value: Any, total: int) -> None:
    _keys_v2(value, set(OPERATION_STATUSES))
    for count in value.values():
        _integer_v2(count)
    _require_v2(sum(value.values()) == total)


def _coverage_v2(value: Any, name: str) -> None:
    _keys_v2(value, _COVERAGE_FIELDS)
    _require_v2(value["state"] in ("complete", "partial", "unavailable"))
    _timestamp_v2(value["observed_at"], nullable=True)
    for key in ("known_count", "returned_count"):
        _integer_v2(value[key])
    for key in ("total_count", "unread_count", "invalid_count"):
        _integer_v2(value[key], nullable=True)
    _status_counts_v2(value["known_by_status"], value["known_count"])
    allowed_statuses = (
        {"planned"}
        if name == "investigation_plans"
        else set(OPERATION_STATUSES) - {"planned", "running", "killed"}
    )
    _require_v2(
        all(
            value["known_by_status"][status] == 0
            for status in set(OPERATION_STATUSES) - allowed_statuses
        )
    )
    _strings_v2(value["reasons"], unique=True)
    _require_v2(set(value["reasons"]) <= _COVERAGE_REASONS)
    _require_v2(value["returned_count"] <= value["known_count"])
    if value["state"] == "complete":
        _require_v2(
            value["total_count"] == value["known_count"]
            and value["unread_count"] == 0
            and value["invalid_count"] == 0
            and not value["reasons"]
        )
    else:
        _require_v2(value["total_count"] is None and bool(value["reasons"]))
    if value["state"] == "unavailable":
        _require_v2(
            value["window"] is None
            and value["known_count"] == 0
            and value["observed_at"] is None
        )
    else:
        _require_v2(
            name != "source_executions"
            and (value["observed_at"] is not None or value["known_count"] == 0)
        )
        _keys_v2(value["window"], {"basis", "start", "end"})
        basis = (
            "retained_question_ledger"
            if name == "general_questions"
            else "retained_investigation_store"
        )
        _require_v2(value["window"]["basis"] == basis)
        _timestamp_v2(value["window"]["start"], nullable=True)
        _timestamp_v2(value["window"]["end"])
        if value["observed_at"] is not None:
            _require_v2(
                datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
                <= datetime.fromisoformat(value["window"]["end"].replace("Z", "+00:00"))
            )
        if value["window"]["start"] is not None:
            _require_v2(
                datetime.fromisoformat(value["window"]["start"].replace("Z", "+00:00"))
                <= datetime.fromisoformat(value["window"]["end"].replace("Z", "+00:00"))
            )


def _operation_v2(row: Any, scope: dict[str, Any], expected_type: str) -> None:
    _keys_v2(row, _ROW_FIELDS)
    _require_v2(row["kind"] == "research" and row["operation_type"] == expected_type)
    _require_v2(row["evidence_readiness"] == "unchecked")
    _text_v2(row["title"])
    _text_v2(row["status"])
    _text_v2(row["entity_id"])
    _timestamp_v2(row["updated_at"], nullable=True)
    _strings_v2(row["gaps"])
    markets = row["market_scope"]
    _strings_v2(markets, unique=True)
    _require_v2(
        bool(markets)
        and markets == sorted(markets)
        and set(markets) <= set(scope["market_scope"])
    )
    if expected_type == "general_question":
        try:
            canonical = str(UUID(row["entity_id"]))
        except ValueError as error:
            raise FieldworkContractError(
                "fieldwork request identity is invalid"
            ) from error
        _require_v2(
            canonical == row["entity_id"]
            and row["operation_id"] == "gq_" + canonical.replace("-", "")
        )
        _require_v2(
            row["status"] in set(OPERATION_STATUSES) - {"planned", "running", "killed"}
        )
        action = "open_question_request"
    else:
        _require_v2(
            re.fullmatch(r"inv_[A-Za-z0-9_-]{1,128}", row["entity_id"]) is not None
            and row["operation_id"] == row["entity_id"]
            and row["status"] == "planned"
        )
        action = "open_investigation"
    link = row["next_operation"]
    _keys_v2(link, {"action", "entity_id", "label", "available", "reason"})
    _require_v2(
        link["action"] == action
        and link["entity_id"] == row["entity_id"]
        and type(link["available"]) is bool
    )
    _text_v2(link["label"])
    _text_v2(link["reason"], nullable=True)
    _require_v2(
        (link["available"] and link["reason"] is None)
        or (not link["available"] and link["reason"] is not None)
    )


def _unavailable_coverage_v2() -> dict[str, Any]:
    return dict(
        state="unavailable",
        observed_at=None,
        window=None,
        known_count=0,
        known_by_status=dict.fromkeys(OPERATION_STATUSES, 0),
        total_count=None,
        returned_count=0,
        unread_count=None,
        invalid_count=None,
        reasons=["not_wired"],
    )


def _row_sort_v2(row: dict[str, Any]) -> tuple[float, str]:
    stamp = row["updated_at"]
    return (
        -(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())
        if stamp is not None
        else float("inf"),
        row["operation_id"],
    )


def build_fieldwork_workspace_v2(
    *,
    scope: dict[str, Any],
    general_questions: dict[str, Any] | None,
    investigation_plans: dict[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    _scope_v2(scope)
    coverage = {}
    rows = []
    for name, bundle in (
        ("general_questions", general_questions),
        ("investigation_plans", investigation_plans),
    ):
        if bundle is None:
            coverage[name] = _unavailable_coverage_v2()
            continue
        _keys_v2(bundle, {"scope", "coverage", "rows"})
        _require_v2(bundle["scope"] == scope)
        _coverage_v2(bundle["coverage"], name)
        _require_v2(isinstance(bundle["rows"], list))
        _require_v2(bundle["coverage"]["returned_count"] == len(bundle["rows"]))
        counts = dict.fromkeys(OPERATION_STATUSES, 0)
        for row in bundle["rows"]:
            _operation_v2(row, scope, _INPUT_TYPES[name])
            if row["updated_at"] is not None:
                _require_v2(
                    datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                    <= datetime.fromisoformat(
                        bundle["coverage"]["window"]["end"].replace("Z", "+00:00")
                    )
                )
            counts[row["status"]] += 1
        _require_v2(
            all(
                counts[key] <= bundle["coverage"]["known_by_status"][key]
                for key in OPERATION_STATUSES
            )
        )
        if len(bundle["rows"]) == bundle["coverage"]["known_count"]:
            _require_v2(counts == bundle["coverage"]["known_by_status"])
        coverage[name] = deepcopy(bundle["coverage"])
        rows.extend(deepcopy(bundle["rows"]))
    _require_v2(len({row["operation_id"] for row in rows}) == len(rows))
    coverage["source_executions"] = _unavailable_coverage_v2()
    rows = sorted(rows, key=_row_sort_v2)[:RESEARCH_ROW_LIMIT]
    for name, kind in _INPUT_TYPES.items():
        coverage[name]["returned_count"] = sum(
            row["operation_type"] == kind for row in rows
        )
    known = sum(item["known_count"] for item in coverage.values())
    research_count = (
        sum(
            coverage[name]["known_count"]
            for name in ("general_questions", "investigation_plans")
        )
        if all(
            coverage[name]["state"] == "complete"
            for name in ("general_questions", "investigation_plans")
        )
        else None
    )
    reasons = ("fieldwork_operation_inputs_unavailable",)
    stamp = now or datetime.now(UTC)
    _require_v2(stamp.utcoffset() is not None)
    payload = dict(
        contract_version=CONTRACT_VERSION_V2,
        resource_version="",
        client_scope_id=scope["client_scope_id"],
        market_scope=deepcopy(scope["market_scope"]),
        brand_config_id=scope["brand_config_id"],
        audience_lens_ids=deepcopy(scope["audience_lens_ids"]),
        theme_id=scope["theme_id"],
        workspace_state="unavailable_source",
        generated_at=stamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        freshness=_unavailable_freshness(reasons),
        operation_coverage=coverage,
        operation_summary=dict(
            count_state="unavailable"
            if all(item["state"] == "unavailable" for item in coverage.values())
            else "partial",
            total=None,
            known_total=known,
            known_by_status={
                key: sum(item["known_by_status"][key] for item in coverage.values())
                for key in OPERATION_STATUSES
            },
        ),
        budget_summary=_unavailable_budget(reasons),
        source_operations=[],
        research_operations=rows,
        research_operation_count=research_count,
        research_operations_truncated=known > len(rows),
        unavailable_inputs=[
            name
            for name, key in (
                ("general_questions", "general_questions"),
                ("investigation_store", "investigation_plans"),
                ("source_executions", "source_executions"),
            )
            if coverage[key]["state"] != "complete"
        ]
        + ["budget_authority"],
    )
    payload["resource_version"] = _resource_version(payload)
    while len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) > 65536 and rows:
        rows.pop()
        for name, kind in _INPUT_TYPES.items():
            coverage[name]["returned_count"] = sum(
                row["operation_type"] == kind for row in rows
            )
        payload["research_operations_truncated"] = known > len(rows)
        payload["resource_version"] = _resource_version(payload)
    validate_fieldwork_payload_v2(payload)
    return payload


def validate_fieldwork_payload_v2(payload: dict[str, Any]) -> None:
    expected_order = (
        "contract_version",
        "resource_version",
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "workspace_state",
        "generated_at",
        "freshness",
        "operation_coverage",
        "operation_summary",
        "budget_summary",
        "source_operations",
        "research_operations",
        "research_operation_count",
        "research_operations_truncated",
        "unavailable_inputs",
    )
    _require_v2(isinstance(payload, dict) and tuple(payload) == expected_order)
    _keys_v2(
        payload,
        {
            "contract_version",
            "resource_version",
            *_SCOPE_FIELDS,
            "workspace_state",
            "generated_at",
            "freshness",
            "operation_coverage",
            "operation_summary",
            "budget_summary",
            "source_operations",
            "research_operations",
            "research_operation_count",
            "research_operations_truncated",
            "unavailable_inputs",
        },
    )
    _require_v2(
        payload["contract_version"] == CONTRACT_VERSION_V2
        and payload["workspace_state"] == "unavailable_source"
    )
    scope = {key: payload[key] for key in _SCOPE_FIELDS}
    _scope_v2(scope)
    _timestamp_v2(payload["generated_at"])
    coverage = payload["operation_coverage"]
    _keys_v2(coverage, set(_INPUT_TYPES))
    for name, value in coverage.items():
        _coverage_v2(value, name)
    _require_v2(coverage["source_executions"] == _unavailable_coverage_v2())
    rows = payload["research_operations"]
    _require_v2(isinstance(rows, list) and len(rows) <= RESEARCH_ROW_LIMIT)
    for row in rows:
        _require_v2(
            isinstance(row, dict)
            and row.get("operation_type") in ("general_question", "investigation_plan")
        )
        _operation_v2(row, scope, row["operation_type"])
    _require_v2(
        len({row["operation_id"] for row in rows}) == len(rows)
        and rows == sorted(rows, key=_row_sort_v2)
    )
    for name, kind in _INPUT_TYPES.items():
        selected = [row for row in rows if row["operation_type"] == kind]
        _require_v2(coverage[name]["returned_count"] == len(selected))
        for row in selected:
            if row["updated_at"] is not None:
                _require_v2(
                    datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
                    <= datetime.fromisoformat(
                        coverage[name]["window"]["end"].replace("Z", "+00:00")
                    )
                )
        for status in OPERATION_STATUSES:
            _require_v2(
                sum(row["status"] == status for row in selected)
                <= coverage[name]["known_by_status"][status]
            )
    summary = payload["operation_summary"]
    _keys_v2(summary, {"count_state", "total", "known_total", "known_by_status"})
    _integer_v2(summary["known_total"])
    _status_counts_v2(summary["known_by_status"], summary["known_total"])
    _require_v2(
        summary["total"] is None
        and summary["known_total"]
        == sum(value["known_count"] for value in coverage.values())
    )
    _require_v2(
        summary["known_by_status"]
        == {
            key: sum(value["known_by_status"][key] for value in coverage.values())
            for key in OPERATION_STATUSES
        }
    )
    state = (
        "unavailable"
        if all(value["state"] == "unavailable" for value in coverage.values())
        else "partial"
    )
    _require_v2(summary["count_state"] == state)
    complete = all(
        coverage[key]["state"] == "complete"
        for key in ("general_questions", "investigation_plans")
    )
    _integer_v2(payload["research_operation_count"], nullable=True)
    _require_v2(
        payload["research_operation_count"]
        == (summary["known_total"] if complete else None)
    )
    _require_v2(
        type(payload["research_operations_truncated"]) is bool
        and payload["research_operations_truncated"]
        == (summary["known_total"] > len(rows))
    )
    _require_v2(payload["source_operations"] == [])
    reasons = ("fieldwork_operation_inputs_unavailable",)
    _require_v2(
        payload["budget_summary"] == _unavailable_budget(reasons)
        and payload["freshness"] == _unavailable_freshness(reasons)
    )
    expected = [
        name
        for name, key in (
            ("general_questions", "general_questions"),
            ("investigation_store", "investigation_plans"),
            ("source_executions", "source_executions"),
        )
        if coverage[key]["state"] != "complete"
    ] + ["budget_authority"]
    _require_v2(
        payload["unavailable_inputs"] == expected
        and payload["resource_version"] == _resource_version(payload)
    )
    _require_v2(len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) <= 65536)
