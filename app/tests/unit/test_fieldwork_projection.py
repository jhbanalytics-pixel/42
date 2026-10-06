"""The Fieldwork response contract.

Fieldwork reports what it could actually read. These tests exist because the
cheapest way to make a workspace look finished is to turn an input it could not
read into a zero, an empty list or a green state, and every one of those reads
to a strategist as a fact about the world rather than a fact about the system.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from src.api import fieldwork

FIELD_ORDER = (
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


def _read(**overrides):
    return fieldwork.read_fieldwork_workspace(
        client_scope_id=overrides.get("client_scope_id", "ogilvy_default"),
        market_scope=overrides.get("market_scope", ("za", "ng", "ke")),
        now=overrides.get("now", datetime(2026, 8, 29, 6, 30, tzinfo=UTC)),
    )


def test_the_response_carries_the_exact_field_order():
    assert tuple(_read()) == FIELD_ORDER


def test_scope_is_the_servers_and_markets_stay_distinct():
    payload = _read()
    assert payload["client_scope_id"] == "ogilvy_default"
    assert payload["market_scope"] == ["za", "ng", "ke"]


def test_unreadable_inputs_report_unavailable_source_not_empty():
    """Break caught: an unread input presented as a completed empty read.

    empty means the inputs were read and held nothing. unavailable_source
    means they could not be read. Collapsing the two tells a strategist there
    is no fieldwork when the truth is that nobody looked.
    """
    payload = _read()
    assert payload["workspace_state"] == "unavailable_source"
    assert payload["workspace_state"] != "empty"
    assert payload["unavailable_inputs"] == ["source_lab", "investigation_store"]
    assert payload["freshness"]["state"] == "unavailable"
    assert payload["freshness"]["reasons"]


def test_budget_without_evidence_is_never_available_or_exhausted():
    """Break caught: a visible balance read as permission to spend."""
    budget = _read()["budget_summary"]
    assert budget["state"] == "unavailable"
    assert budget["state"] not in ("available", "exhausted")
    # Every credit figure is unknown, not zero. Zero would be a measurement.
    for field in (
        "credit_ceiling",
        "credits_reserved",
        "credits_used",
        "credits_remaining",
        "reserve_floor",
        "observed_at",
        "funding_lane",
    ):
        assert budget[field] is None, field
    assert budget["reasons"]


def test_operation_counts_reconcile_and_rows_are_honestly_empty():
    payload = _read()
    summary = payload["operation_summary"]
    assert summary["total"] == sum(
        summary[key] for key in ("active", "held", "complete", "killed", "unavailable")
    )
    assert payload["source_operations"] == []
    assert payload["research_operations"] == []
    assert payload["research_operation_count"] == 0
    assert payload["research_operations_truncated"] is False


def test_no_execution_surface_appears_anywhere_in_the_response():
    """Break caught: an action the workspace has no authority to offer."""
    payload = _read()
    # Match whole tokens, not substrings: generated_at is a timestamp, not a
    # generate action, and a substring check would call it a violation.
    tokens = set(re.findall(r"[a-z_]+", repr(payload).lower()))
    for forbidden in (
        "enable",
        "execute",
        "generate",
        "approve",
        "publish",
        "export",
        "run",
    ):
        assert forbidden not in tokens, forbidden
    # No row carries an action key at all, in any state.
    for row in (*payload["source_operations"], *payload["research_operations"]):
        assert "actions" not in row
        assert "execute" not in row


def test_the_same_reading_has_the_same_identity():
    """resource_version excludes the clock, so an unchanged world looks unchanged."""
    first = _read(now=datetime(2026, 8, 29, 6, 30, tzinfo=UTC))
    second = _read(now=datetime(2026, 8, 29, 9, 45, tzinfo=UTC))
    assert first["generated_at"] != second["generated_at"]
    assert first["resource_version"] == second["resource_version"]
    assert first["resource_version"].startswith("fwr_")


def test_a_different_scope_is_a_different_reading():
    a = _read(client_scope_id="ogilvy_default")
    b = _read(client_scope_id="other_scope")
    assert a["resource_version"] != b["resource_version"]


@pytest.mark.parametrize(
    "mutation",
    [
        {"workspace_state": "invented"},
        {"contract_version": "fieldwork_workspace_v2"},
        {"research_operations_truncated": True},
        {"unavailable_inputs": ["source_lab", "source_lab"]},
        {"unavailable_inputs": ["not_a_real_input"]},
    ],
    ids=["state", "version", "truncation", "duplicate_input", "unknown_input"],
)
def test_a_drifted_payload_is_refused_before_it_can_render(mutation):
    payload = _read()
    payload.update(mutation)
    with pytest.raises(fieldwork.FieldworkContractError):
        fieldwork.validate_fieldwork_payload(payload)


def test_a_reordered_payload_is_refused():
    payload = _read()
    reordered = {key: payload[key] for key in reversed(FIELD_ORDER)}
    with pytest.raises(fieldwork.FieldworkContractError):
        fieldwork.validate_fieldwork_payload(reordered)


def test_an_operation_summary_that_does_not_reconcile_is_refused():
    payload = _read()
    payload["operation_summary"] = {**payload["operation_summary"], "total": 3}
    with pytest.raises(fieldwork.FieldworkContractError):
        fieldwork.validate_fieldwork_payload(payload)
