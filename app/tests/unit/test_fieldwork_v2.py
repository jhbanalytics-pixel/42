from copy import deepcopy
import json
from pathlib import Path
from datetime import UTC, datetime
from uuid import UUID

import pytest

from src.api import fieldwork


STATUSES = (
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
STAMP = "2026-09-07T12:00:00Z"
SCOPE = dict(
    client_scope_id="ogilvy_default",
    market_scope=["za"],
    brand_config_id=None,
    audience_lens_ids=[],
    theme_id=None,
)


def row(number=1, status="complete"):
    identity = str(UUID(int=number))
    return dict(
        operation_id="gq_" + identity.replace("-", ""),
        kind="research",
        operation_type="general_question",
        entity_id=identity,
        title="A retained question",
        status=status,
        market_scope=["za"],
        updated_at=STAMP,
        evidence_readiness="unchecked",
        gaps=[],
        next_operation=dict(
            action="open_question_request",
            entity_id=identity,
            label="Open request",
            available=True,
            reason=None,
        ),
    )


def observation(rows=(), state="complete", unread=0):
    counts = dict.fromkeys(STATUSES, 0)
    for item in rows:
        counts[item["status"]] += 1
    return dict(
        scope=deepcopy(SCOPE),
        rows=list(rows),
        coverage=dict(
            state=state,
            observed_at=STAMP,
            window=dict(basis="retained_question_ledger", start=None, end=STAMP),
            known_count=len(rows),
            known_by_status=counts,
            total_count=len(rows) if state == "complete" else None,
            returned_count=len(rows),
            unread_count=unread,
            invalid_count=0,
            reasons=[] if state == "complete" else ["record_unavailable"],
        ),
    )


def read(questions=None, investigations=None):
    return fieldwork.build_fieldwork_workspace_v2(
        scope=deepcopy(SCOPE),
        general_questions=questions,
        investigation_plans=investigations,
        now=datetime(2026, 9, 9, 22, tzinfo=UTC),
    )


def test_unavailable_inputs_have_no_complete_totals_or_vendor_credits():
    result = read()
    assert result["contract_version"] == "fieldwork_workspace_v2"
    assert result["operation_summary"]["count_state"] == "unavailable"
    assert result["operation_summary"]["total"] is None
    assert result["research_operation_count"] is None
    assert result["operation_coverage"]["source_executions"]["total_count"] is None
    assert result["budget_summary"]["credits_reserved"] is None


def test_known_counts_precede_display_cut_and_partial_is_not_complete():
    result = read(
        observation([row(i, "partial" if i == 1 else "complete") for i in range(1, 74)])
    )
    assert len(result["research_operations"]) == 50
    assert result["operation_summary"]["known_total"] == 73
    assert result["operation_summary"]["known_by_status"]["complete"] == 72
    assert result["operation_summary"]["known_by_status"]["partial"] == 1
    assert result["operation_coverage"]["general_questions"]["returned_count"] == 50
    assert result["operation_summary"]["total"] is None
    assert result["research_operations_truncated"] is True


def test_complete_research_empty_is_zero_but_workspace_total_stays_unknown():
    investigations = observation()
    investigations["coverage"]["window"]["basis"] = "retained_investigation_store"
    result = read(observation(), investigations)
    assert result["research_operation_count"] == 0
    assert result["operation_summary"]["total"] is None
    assert result["operation_summary"]["count_state"] == "partial"


def test_partial_input_keeps_known_rows_and_null_total():
    result = read(observation([row()], state="partial", unread=None))
    assert result["operation_summary"]["known_total"] == 1
    assert result["research_operation_count"] is None
    assert result["operation_coverage"]["general_questions"]["unread_count"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_scope_id", "other"),
        ("brand_config_id", "other"),
        ("audience_lens_ids", ["other"]),
        ("theme_id", "other"),
        ("market_scope", ["ng"]),
    ],
)
def test_observation_scope_must_match_server_scope(field, value):
    bundle = observation([row()])
    bundle["scope"][field] = value
    with pytest.raises(fieldwork.FieldworkContractError):
        read(bundle)


@pytest.mark.parametrize(
    "change",
    [
        lambda bundle: bundle["coverage"].update(known_count=True),
        lambda bundle: bundle["coverage"].update(total_count=2),
        lambda bundle: bundle["coverage"].update(unexpected="hidden"),
        lambda bundle: bundle["rows"][0].update(status="running"),
        lambda bundle: bundle["rows"][0].update(evidence_readiness="ready"),
        lambda bundle: bundle["rows"][0].update(market_scope=["ng"]),
        lambda bundle: bundle["rows"][0]["next_operation"].update(
            target="https://evil.test"
        ),
        lambda bundle: bundle["rows"][0]["next_operation"].update(
            entity_id=str(UUID(int=99))
        ),
        lambda bundle: bundle["rows"][0].update(updated_at="2026-09-07"),
        lambda bundle: bundle["rows"].append(deepcopy(bundle["rows"][0])),
    ],
)
def test_malformed_or_unproven_observations_refuse(change):
    bundle = observation([row()])
    change(bundle)
    with pytest.raises(fieldwork.FieldworkContractError):
        read(bundle)


def test_output_validation_rejects_resealed_wrong_counts():
    result = read(observation([row()]))
    result["operation_summary"]["known_total"] = 2
    result["resource_version"] = fieldwork._resource_version(result)
    with pytest.raises(fieldwork.FieldworkContractError):
        fieldwork.validate_fieldwork_payload_v2(result)


def test_projection_does_not_mutate_source_and_fetch_time_is_not_identity():
    bundle = observation([row()])
    original = deepcopy(bundle)
    result = read(bundle)
    later = fieldwork.build_fieldwork_workspace_v2(
        scope=deepcopy(SCOPE),
        general_questions=bundle,
        investigation_plans=None,
        now=datetime(2026, 9, 10, 22, tzinfo=UTC),
    )
    assert bundle == original
    assert result["resource_version"] == later["resource_version"]
    assert result["research_operations"][0]["updated_at"] == STAMP


def test_hidden_status_counts_cannot_claim_running_without_execution_reader():
    bundle = observation([row()])
    bundle["coverage"].update(known_count=2, total_count=2)
    bundle["coverage"]["known_by_status"]["running"] = 1
    with pytest.raises(fieldwork.FieldworkContractError):
        read(bundle)


def test_public_order_does_not_depend_on_private_scope_dictionary_order():
    result = fieldwork.build_fieldwork_workspace_v2(
        scope=dict(reversed(list(SCOPE.items()))),
        general_questions=None,
        investigation_plans=None,
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )
    assert list(result)[:7] == [
        "contract_version",
        "resource_version",
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
    ]


def test_equal_timestamps_use_identity_and_null_timestamps_sort_last():
    last = row(1)
    last["updated_at"] = None
    result = read(observation([last, row(3), row(2)]))
    assert [item["entity_id"] for item in result["research_operations"]] == [
        str(UUID(int=i)) for i in (2, 3, 1)
    ]


@pytest.mark.parametrize(
    "change",
    [
        lambda bundle: bundle["rows"][0].update(status=[]),
        lambda bundle: bundle["rows"][0].update(updated_at="2026-09-08T12:00:00Z"),
        lambda bundle: bundle["coverage"].update(observed_at="2026-09-08T12:00:00Z"),
    ],
)
def test_reviewed_type_and_observation_time_controls(change):
    bundle = observation([row()])
    change(bundle)
    with pytest.raises(fieldwork.FieldworkContractError):
        read(bundle)


def test_oversized_display_row_is_withheld_without_changing_known_counts():
    large = row()
    large["title"] = "x" * 70000
    result = read(observation([large]))
    assert len(json.dumps(result).encode("utf-8")) <= 65536
    assert result["research_operations"] == []
    assert result["operation_summary"]["known_total"] == 1
    assert result["operation_coverage"]["general_questions"]["returned_count"] == 0
    assert result["research_operations_truncated"] is True


def test_shared_v2_fixture_matches_strict_projection_contract():
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/workspaces/fieldwork_workspace_v2.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture["provenance"] == "fixture-only"
    fieldwork.validate_fieldwork_payload_v2(fixture["partial"])
