import hashlib
import inspect
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
import src.analysis.open_intelligence.seo_visibility as seo_visibility
from src.analysis.open_intelligence.seo_visibility import (
    assess_historical_comparison,
    assess_strategist_claim,
    build_observation,
    require_zero_paid_call_authority,
    strategist_projection,
    validate_request,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "open_intelligence" / "seo_visibility"
)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 1, 12, tzinfo=tz or UTC)


def _authority_reader(*receipts: dict[str, object]):
    available = receipts or (_fixture("query_authority_human_public_v1.json"),)

    def read(receipt_id: str):
        return [
            receipt
            for receipt in available
            if receipt.get("query_authority_receipt_id") == receipt_id
        ]

    return seo_visibility._issue_query_authority_reader(read)


def _readdress_authority(receipt: dict[str, object]) -> dict[str, object]:
    identity = {key: value for key, value in receipt.items() if key != "query_authority_receipt_id"}
    receipt["query_authority_receipt_id"] = "seqa_" + _canonical_digest(identity)
    return receipt


def _validate_public_request(payload: dict[str, object], *receipts: dict[str, object]):
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(seo_visibility, "datetime", _FrozenDateTime)
        return validate_request(
            payload,
            query_authority_reader=_authority_reader(*receipts),
        )


def test_request_fixture_is_exact_bounded_public_research() -> None:
    payload = _fixture("request_public_research_v2.json")

    calls = []

    def reader(receipt_id: str):
        calls.append(receipt_id)
        return [_fixture("query_authority_human_public_v1.json")]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(seo_visibility, "datetime", _FrozenDateTime)
        request = validate_request(
            payload,
            query_authority_reader=seo_visibility._issue_query_authority_reader(reader),
        )

    assert asdict(request) == {
        **payload,
        "markets": ("za",),
        "queries": (
            "public transport reliability",
            "commuter rail service updates",
        ),
        "brand_domains": ("example.org",),
        "competitor_domains": ("comparison.example",),
    }
    assert calls == [payload["query_authority_receipt_id"]]


def test_request_refuses_a_caller_supplied_unissued_authority_reader() -> None:
    payload = _fixture("request_public_research_v2.json")
    receipt = _fixture("query_authority_human_public_v1.json")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(seo_visibility, "datetime", _FrozenDateTime)
        with pytest.raises(ValueError, match="authority reader"):
            validate_request(
                payload,
                query_authority_reader=lambda _receipt_id: [receipt],
            )


def test_exact_request_fields_do_not_depend_on_json_object_order() -> None:
    payload = _fixture("request_public_research_v2.json")
    reordered = {key: payload[key] for key in reversed(payload)}

    request = _validate_public_request(reordered)

    assert request.request_id == payload["request_id"]


@pytest.mark.parametrize(
    "field",
    ["provider", "provider_endpoint", "project", "dataset", "table", "secret", "cost_ceiling"],
)
def test_request_refuses_caller_selected_authority_fields(field: str) -> None:
    payload = _fixture("request_public_research_v2.json")
    payload[field] = "caller-selected"

    with pytest.raises(ValueError, match="request fields are invalid"):
        _validate_public_request(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("markets", [], "markets"),
        ("markets", ["za", "za"], "markets"),
        ("markets", ["us"], "markets"),
        ("queries", [], "queries"),
        ("queries", [f"query {index}" for index in range(26)], "queries"),
        ("queries", ["Public term", "public term"], "queries"),
        ("brand_domains", [f"brand{index}.example" for index in range(6)], "brand_domains"),
        (
            "competitor_domains",
            [f"competitor{index}.example" for index in range(11)],
            "competitor_domains",
        ),
        ("brand_domains", ["https://example.org/path"], "brand_domains"),
    ],
)
def test_request_refuses_unbounded_duplicate_or_noncanonical_collections(
    field: str, value: object, message: str
) -> None:
    payload = _fixture("request_public_research_v2.json")
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        _validate_public_request(payload)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-01-01T00:00:00.000000Z", "2026-04-02T00:00:00.000000Z"),
        ("2026-08-31T00:00:00.000000Z", "2026-08-30T00:00:00.000000Z"),
        ("2026-08-01", "2026-08-30T23:59:59.000000Z"),
        ("2026-08-01T00:00:00.000000+02:00", "2026-08-30T23:59:59.000000Z"),
        ("2026-08-01T00:00:00.000000Z", "2099-08-30T23:59:59.000000Z"),
    ],
)
def test_request_requires_closed_utc_window_of_at_most_ninety_days(start: str, end: str) -> None:
    payload = _fixture("request_public_research_v2.json")
    payload["window_start"] = start
    payload["window_end"] = end

    with pytest.raises(ValueError, match="window"):
        _validate_public_request(payload)


@pytest.mark.parametrize(
    "query",
    [
        "person@example.org",
        "+27 82 555 0101",
        "unpublished client launch plan",
        "internal only strategy notes",
    ],
)
def test_request_refuses_apparent_personal_or_nonpublic_research_terms(query: str) -> None:
    payload = _fixture("request_public_research_v2.json")
    payload["queries"] = [query]

    with pytest.raises(ValueError, match="public research"):
        _validate_public_request(payload)


def test_direct_request_validation_without_fixed_authority_reader_fails_closed() -> None:
    with pytest.raises(ValueError, match="query authority is required"):
        validate_request(_fixture("request_public_research_v2.json"))


def test_request_v1_fails_closed_without_a_compatibility_bridge() -> None:
    payload = _fixture("request_public_research_v2.json")
    payload["adapter_contract_version"] = "seo_visibility_request_v1"
    payload.pop("query_authority_receipt_id")

    with pytest.raises(ValueError, match=r"fields|version"):
        _validate_public_request(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "seor_changed"),
        ("client_scope_id", "scope_other_public_brand_za"),
        ("run_id", "run_other_public_search"),
        ("decision", "Assess a different public search decision."),
        ("queries", ["changed public query"]),
    ],
)
def test_valid_authority_cannot_authorize_changed_request_binding(field, value) -> None:
    payload = _fixture("request_public_research_v2.json")
    payload[field] = value

    with pytest.raises(ValueError, match="query authority"):
        _validate_public_request(payload)


def test_missing_or_duplicate_authority_fails_closed() -> None:
    payload = _fixture("request_public_research_v2.json")
    receipt = _fixture("query_authority_human_public_v1.json")

    with pytest.raises(ValueError, match="query authority"):
        _validate_public_request(
            payload, {**receipt, "query_authority_receipt_id": "seqa_" + "b" * 64}
        )
    with pytest.raises(ValueError, match="query authority"):
        _validate_public_request(payload, receipt, dict(receipt))


@pytest.mark.parametrize(
    "mutation",
    [
        {"authority_contract_version": "seo_public_query_authority_v0"},
        {"expires_at": "2026-09-01T11:59:59.000000Z"},
        {"approver_principal": "foreign.approver@example.org"},
    ],
)
def test_unsupported_expired_or_foreign_authority_fails_closed(mutation) -> None:
    payload = _fixture("request_public_research_v2.json")
    receipt = _readdress_authority({**_fixture("query_authority_human_public_v1.json"), **mutation})
    payload["query_authority_receipt_id"] = receipt["query_authority_receipt_id"]

    with pytest.raises(ValueError, match="query authority"):
        _validate_public_request(payload, receipt)


def test_malformed_authority_receipt_fails_closed() -> None:
    payload = _fixture("request_public_research_v2.json")
    receipt = _fixture("query_authority_human_public_v1.json")
    receipt.pop("decision_digest")

    with pytest.raises(ValueError, match="query authority"):
        _validate_public_request(payload, receipt)


def test_authority_receipt_content_drift_fails_closed() -> None:
    payload = _fixture("request_public_research_v2.json")
    receipt = _fixture("query_authority_human_public_v1.json")
    receipt["issued_at"] = "2026-09-01T00:00:01.000000Z"

    with pytest.raises(ValueError, match="query authority"):
        _validate_public_request(payload, receipt)


def test_deterministic_public_derivative_requires_a_public_parent_receipt() -> None:
    payload = _fixture("request_public_research_v2.json")
    receipt = _fixture("query_authority_human_public_v1.json")
    receipt.update(
        {
            "authority_basis": "deterministic_public_derivative",
            "approver_principal": None,
        }
    )
    _readdress_authority(receipt)
    payload["query_authority_receipt_id"] = receipt["query_authority_receipt_id"]

    with pytest.raises(ValueError, match="query authority"):
        _validate_public_request(payload, receipt)


@pytest.mark.parametrize(
    "query",
    [
        "person@example.org",
        "+27 82 555 0101",
        "Albert Meintjes bank account 123456789",
        "Albert Meintjes identity number 1234567890",
        "Albert Meintjes private address",
        "Albert Meintjes medical record",
    ],
)
def test_valid_authority_does_not_override_private_data_rejection(query: str) -> None:
    payload = _fixture("request_public_research_v2.json")
    payload["queries"] = [query]
    receipt = _fixture("query_authority_human_public_v1.json")
    receipt["query_set_digest"] = hashlib.sha256(
        json.dumps(
            payload["queries"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    _readdress_authority(receipt)
    payload["query_authority_receipt_id"] = receipt["query_authority_receipt_id"]

    with pytest.raises(ValueError, match="public research"):
        _validate_public_request(payload, receipt)


def _canonical_digest(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def test_observation_is_content_addressed_and_created_at_is_not_identity() -> None:
    payload = _fixture("organic_serp_direct_v1.json")

    first = build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")
    later = build_observation(payload, created_at="2026-08-31T06:00:02.000000Z")

    identity_payload = asdict(first)
    identity_payload.pop("observation_id")
    identity_payload.pop("created_at")
    assert first.observation_id == _canonical_digest(identity_payload)
    assert later.observation_id == first.observation_id
    assert later.created_at != first.created_at


def test_exact_observation_fields_do_not_depend_on_json_object_order() -> None:
    payload = _fixture("organic_serp_direct_v1.json")
    reordered = {key: payload[key] for key in reversed(payload)}

    observation = build_observation(reordered, created_at="2026-08-31T06:00:01.000000Z")

    assert observation.surface == "organic_serp"


def test_any_observation_content_change_changes_its_identity() -> None:
    payload = _fixture("organic_serp_direct_v1.json")
    changed = {**payload, "title": "A different public result title"}

    original = build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")
    replacement = build_observation(changed, created_at="2026-08-31T06:00:01.000000Z")

    assert replacement.observation_id != original.observation_id


def test_named_provider_in_evidence_is_carried_onto_the_observation() -> None:
    # 5 Sep 2026: this test used to also assert a module constant SELECTED_PROVIDER
    # was None. Nothing in the adapter read that constant, so it and the assertion
    # are gone. What remains measures the adapter: the provider named in the
    # evidence payload must reach the built observation unchanged.
    observation = build_observation(
        _fixture("ai_citation_direct_v1.json"),
        created_at="2026-08-31T06:05:01.000000Z",
    )

    assert observation.provider == "fixture_ai_search_provider"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("measurement_state", "inferred", "direct"),
        ("result_type", "organic_result", "direct"),
        ("provider", None, "provider"),
        ("provider_endpoint", None, "provider"),
        ("source_receipt_id", None, "receipt"),
        ("ai_citation_target", None, "citation target"),
    ],
)
def test_ai_citation_requires_direct_named_provider_measurement(
    field: str, value: object, message: str
) -> None:
    payload = _fixture("ai_citation_direct_v1.json")
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        build_observation(payload, created_at="2026-08-31T06:05:01.000000Z")


@pytest.mark.parametrize("dimension", ["engine=", "language=", "device=", "location="])
def test_direct_ai_measurement_requires_bound_engine_language_device_and_location(
    dimension: str,
) -> None:
    payload = _fixture("ai_citation_direct_v1.json")
    payload["limitations"] = [
        item for item in payload["limitations"] if not item.startswith(dimension)
    ]

    with pytest.raises(ValueError, match="direct measurement dimensions"):
        build_observation(payload, created_at="2026-08-31T06:05:01.000000Z")


def test_ordinary_organic_result_cannot_carry_ai_citation_evidence() -> None:
    payload = _fixture("organic_serp_direct_v1.json")
    payload.update(
        {
            "ai_answer_present": True,
            "ai_citation_present": True,
            "ai_citation_target": "https://example.org/public-transport-facts",
        }
    )

    with pytest.raises(ValueError, match="AI fields"):
        build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("result_position", None),
        ("result_url", None),
        ("result_domain", None),
        ("result_type", None),
    ],
)
def test_measured_organic_result_requires_occupied_result_evidence(
    field: str, value: object
) -> None:
    payload = _fixture("organic_serp_direct_v1.json")
    payload[field] = value

    with pytest.raises(ValueError, match="organic result"):
        build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")


def test_search_demand_cannot_carry_organic_result_occupation() -> None:
    payload = _fixture("organic_serp_direct_v1.json")
    payload["surface"] = "search_demand"

    with pytest.raises(ValueError, match="search demand"):
        build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")


def test_ai_overview_cannot_carry_organic_result_occupation() -> None:
    payload = _fixture("ai_citation_direct_v1.json")
    payload.update(
        {
            "surface": "ai_overview",
            "ai_citation_present": None,
            "ai_citation_target": None,
        }
    )

    with pytest.raises(ValueError, match="AI Overview"):
        build_observation(payload, created_at="2026-08-31T06:05:01.000000Z")


def test_brand_presence_cannot_bypass_the_direct_ai_citation_surface() -> None:
    payload = _fixture("ai_citation_direct_v1.json")
    payload["surface"] = "brand_presence"

    with pytest.raises(ValueError, match="brand presence"):
        build_observation(payload, created_at="2026-08-31T06:05:01.000000Z")


def test_direct_ai_citation_target_must_equal_the_supporting_result_url() -> None:
    payload = _fixture("ai_citation_direct_v1.json")
    payload["ai_citation_target"] = "https://other.example/different-source"

    with pytest.raises(ValueError, match="citation target"):
        build_observation(payload, created_at="2026-08-31T06:05:01.000000Z")


def test_absent_direct_ai_citation_has_no_retained_result_target() -> None:
    payload = _fixture("ai_citation_direct_v1.json")
    payload["ai_citation_present"] = False
    payload["ai_citation_target"] = None

    with pytest.raises(ValueError, match="absent citation"):
        build_observation(payload, created_at="2026-08-31T06:05:01.000000Z")


def test_zero_paid_call_authority_has_no_provider_or_budget_selector() -> None:
    assert tuple(inspect.signature(require_zero_paid_call_authority).parameters) == (
        "requested_paid_calls",
    )
    assert require_zero_paid_call_authority(0) == 0


@pytest.mark.parametrize("requested_paid_calls", [1, -1, True, 0.1])
def test_default_authority_refuses_every_nonzero_or_noninteger_paid_call_request(
    requested_paid_calls: object,
) -> None:
    with pytest.raises(ValueError, match="zero paid calls"):
        require_zero_paid_call_authority(requested_paid_calls)


def _organic_observation(
    *,
    created_at: str,
    provider: str = "fixture_search_provider",
    provider_endpoint: str = "/fixture/v1/organic-results",
    result_type: str = "organic_result",
):
    payload = _fixture("organic_serp_direct_v1.json")
    payload["provider"] = provider
    payload["provider_endpoint"] = provider_endpoint
    payload["result_type"] = result_type
    payload["observed_at"] = created_at.replace(":01.000000Z", ":00.000000Z")
    return build_observation(payload, created_at=created_at)


def test_same_provider_query_and_result_format_are_directly_comparable() -> None:
    observations = (
        _organic_observation(created_at="2026-08-31T06:00:01.000000Z"),
        _organic_observation(created_at="2026-08-31T06:10:01.000000Z"),
    )

    comparison = assess_historical_comparison(observations, as_of="2026-08-31T06:11:00.000000Z")

    assert comparison.state == "comparable"
    assert comparison.directly_comparable is True
    assert comparison.provider_continuity is True
    assert comparison.query_set_continuity is True
    assert comparison.result_format_continuity is True
    assert comparison.transfer_limits == ()


@pytest.mark.parametrize(
    ("changed", "expected_limit"),
    [
        ({"provider": "fixture_replacement_provider"}, "provider_changed"),
        ({"provider_endpoint": "/fixture/v2/organic-results"}, "provider_endpoint_changed"),
        ({"result_type": "organic_result_v2"}, "result_format_changed"),
    ],
)
def test_provider_or_result_format_change_breaks_direct_history(
    changed: dict[str, str], expected_limit: str
) -> None:
    observations = (
        _organic_observation(created_at="2026-08-31T06:00:01.000000Z"),
        _organic_observation(created_at="2026-08-31T06:10:01.000000Z", **changed),
    )

    comparison = assess_historical_comparison(observations, as_of="2026-08-31T06:11:00.000000Z")

    assert comparison.state == "historical_break"
    assert comparison.directly_comparable is False
    assert expected_limit in comparison.transfer_limits


@pytest.mark.parametrize(
    ("field", "value", "expected_limit"),
    [
        ("market", "ng", "market_changed"),
        ("client_scope_id", "other_scope", "client_scope_changed"),
    ],
)
def test_market_or_client_scope_change_breaks_direct_history(
    field: str, value: str, expected_limit: str
) -> None:
    first = _fixture("organic_serp_direct_v1.json")
    second = dict(first)
    second[field] = value
    second["observed_at"] = "2026-08-31T06:10:00.000000Z"
    observations = (
        build_observation(first, created_at="2026-08-31T06:00:01.000000Z"),
        build_observation(second, created_at="2026-08-31T06:10:01.000000Z"),
    )

    comparison = assess_historical_comparison(observations, as_of="2026-08-31T06:11:00.000000Z")

    assert comparison.state == "historical_break"
    assert comparison.directly_comparable is False
    assert expected_limit in comparison.transfer_limits


def test_history_uses_only_observations_available_by_as_of() -> None:
    observations = (
        _organic_observation(created_at="2026-08-31T06:00:01.000000Z"),
        _organic_observation(created_at="2026-08-31T07:00:01.000000Z"),
    )

    comparison = assess_historical_comparison(observations, as_of="2026-08-31T06:30:00.000000Z")

    assert comparison.state == "unavailable"
    assert comparison.directly_comparable is False
    assert comparison.observation_ids == (observations[0].observation_id,)
    assert comparison.transfer_limits == ("insufficient_as_of_observations",)


MEASURED_VALUE_FIELDS = (
    "result_position",
    "result_type",
    "result_url",
    "result_domain",
    "title",
    "snippet_digest",
    "ai_answer_present",
    "ai_citation_present",
    "ai_citation_target",
    "confidence",
)


def test_unavailable_observation_requires_null_measured_values() -> None:
    payload = _fixture("unavailable_failed_read_v1.json")

    observation = build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")

    assert observation.measurement_state == "unavailable"
    assert all(getattr(observation, field) is None for field in MEASURED_VALUE_FIELDS)


def test_unavailable_observation_refuses_a_retained_measured_value() -> None:
    payload = _fixture("unavailable_failed_read_v1.json")
    payload["result_position"] = 1

    with pytest.raises(ValueError, match=r"unavailable.*null"):
        build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")


def test_strategist_projection_strips_every_internal_provider_field() -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    stored_row = {
        **asdict(observation),
        "secret_name": "fixture-secret",
        "internal_table_name": "fixture.internal_table",
        "natural_key": "internal-natural-key",
        "request_headers": {"authorization": "redacted"},
        "provider_account_id": "fixture-account",
        "raw_provider_payload": {"private": "discard"},
    }

    projection = strategist_projection(stored_row, as_of="2026-08-31T06:30:00.000000Z")

    assert projection["measurement_state"] == "measured"
    assert projection["result_position"] == 1
    assert not {
        "secret_name",
        "internal_table_name",
        "natural_key",
        "request_headers",
        "provider_account_id",
        "raw_provider_payload",
    }.intersection(projection)


@pytest.mark.parametrize(
    ("as_of", "read_error", "reason"),
    [
        ("2026-09-01T06:00:01.000000Z", None, "stale"),
        ("2026-08-31T06:30:00.000000Z", RuntimeError("permission denied"), "read_failed"),
    ],
)
def test_stale_or_failed_read_returns_unavailable_with_null_measured_values(
    as_of: str, read_error: Exception | None, reason: str
) -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")

    projection = strategist_projection(asdict(observation), as_of=as_of, read_error=read_error)

    assert projection["measurement_state"] == "unavailable"
    assert all(projection[field] is None for field in MEASURED_VALUE_FIELDS)
    assert reason in projection["limitations"]


def test_failed_read_without_a_previous_row_is_still_unavailable() -> None:
    projection = strategist_projection(
        None,
        as_of="2026-08-31T06:30:00.000000Z",
        read_error=PermissionError("permission denied"),
    )

    assert projection["measurement_state"] == "unavailable"
    assert all(projection[field] is None for field in MEASURED_VALUE_FIELDS)
    assert projection["limitations"] == ("read_failed",)


def test_stored_content_drift_with_the_same_observation_id_is_an_immutable_conflict() -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    stored_row = {**asdict(observation), "title": "Changed under the same identity"}

    with pytest.raises(ValueError, match="identity"):
        strategist_projection(stored_row, as_of="2026-08-31T06:30:00.000000Z")


@pytest.mark.parametrize(
    ("claim", "claim_kind", "reason"),
    [
        ("Search demand caused the service response.", "causal", "causal_claim_unsupported"),
        ("Gen Z supports this result.", "demographic", "demographic_claim_unsupported"),
        ("People believe the brand is reliable.", "population", "population_claim_unsupported"),
        ("Overall AI visibility is strong.", "ai_visibility", "single_query_overall_visibility"),
    ],
)
def test_strategist_claim_refuses_unsupported_causal_demographic_population_or_scope(
    claim: str, claim_kind: str, reason: str
) -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")

    assessment = assess_strategist_claim(claim, (projection,), claim_kind=claim_kind)

    assert assessment.allowed is False
    assert assessment.export_allowed is False
    assert reason in assessment.reason_codes


@pytest.mark.parametrize(
    "claim",
    [
        "The brand is visible across AI search.",
        "AI visibility is strong everywhere.",
        "The brand appears across all AI engines.",
    ],
)
def test_one_query_cannot_be_generalized_to_broad_ai_visibility(claim: str) -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")

    assessment = assess_strategist_claim(claim, (projection,), claim_kind="ai_visibility")

    assert assessment.allowed is False
    assert "single_query_overall_visibility" in assessment.reason_codes


@pytest.mark.parametrize(
    "claim",
    [
        "The result triggered extra purchases.",
        "Twenty-somethings favour this result.",
    ],
)
def test_claim_kind_cannot_launder_arbitrary_prose_through_ai_visibility(claim) -> None:
    first = _fixture("organic_serp_direct_v1.json")
    second = dict(first)
    second["query_text"] = "commuter rail service updates"
    second["query_digest"] = hashlib.sha256(second["query_text"].encode()).hexdigest()
    second["observed_at"] = "2026-08-31T06:10:00.000000Z"
    projections = tuple(
        strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
        for observation in (
            build_observation(first, created_at="2026-08-31T06:00:01.000000Z"),
            build_observation(second, created_at="2026-08-31T06:10:01.000000Z"),
        )
    )
    assessment = assess_strategist_claim(claim, projections, claim_kind="ai_visibility")
    assert assessment.allowed is False
    assert assessment.export_allowed is False
    assert "ai_visibility_claim_unsupported" in assessment.reason_codes


def test_exact_measured_ai_visibility_claim_is_exportable() -> None:
    first = _fixture("organic_serp_direct_v1.json")
    second = dict(first)
    second["query_text"] = "commuter rail service updates"
    second["query_digest"] = hashlib.sha256(second["query_text"].encode()).hexdigest()
    second["observed_at"] = "2026-08-31T06:10:00.000000Z"
    projections = tuple(
        strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
        for observation in (
            build_observation(first, created_at="2026-08-31T06:00:01.000000Z"),
            build_observation(second, created_at="2026-08-31T06:10:01.000000Z"),
        )
    )
    assessment = assess_strategist_claim(
        "publisher.example appeared in 2 measured queries.",
        projections,
        claim_kind="ai_visibility",
    )
    assert assessment.allowed is True
    assert assessment.export_allowed is True
    assert assessment.reason_codes == ()


def test_election_claim_requires_human_review_and_blocks_export() -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")

    assessment = assess_strategist_claim(
        "The election query result may shape a cited content opportunity.",
        (projection,),
        claim_kind="election",
    )

    assert assessment.allowed is True
    assert assessment.human_review_required is True
    assert assessment.export_allowed is False
    assert assessment.reason_codes == ("election_human_review_required",)


def test_election_query_requires_review_even_when_the_claim_uses_neutral_words() -> None:
    payload = _fixture("organic_serp_direct_v1.json")
    payload["query_text"] = "national election updates"
    payload["query_digest"] = hashlib.sha256(payload["query_text"].encode()).hexdigest()
    observation = build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")

    assessment = assess_strategist_claim(
        "A cited content opportunity exists for this query.",
        (projection,),
        claim_kind="election",
    )

    assert assessment.human_review_required is True
    assert assessment.export_allowed is False
    assert assessment.reason_codes == ("election_human_review_required",)


def test_unavailable_evidence_cannot_produce_a_strategist_opportunity() -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(
        asdict(observation),
        as_of="2026-08-31T06:30:00.000000Z",
        read_error=RuntimeError("read failed"),
    )

    assessment = assess_strategist_claim(
        "A cited content opportunity exists for this query.",
        (projection,),
        claim_kind="descriptive",
    )

    assert assessment.allowed is False
    assert assessment.export_allowed is False
    assert assessment.reason_codes == ("evidence_not_ready",)


@pytest.mark.parametrize(
    ("claim", "claim_kind", "reason"),
    [
        ("Teenagers prefer this result.", "demographic", "demographic_claim_unsupported"),
        ("This result boosted demand.", "causal", "causal_claim_unsupported"),
    ],
)
def test_structured_claim_kinds_fail_closed_without_demographic_or_causal_authority(
    claim, claim_kind, reason
) -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
    assessment = assess_strategist_claim(claim, (projection,), claim_kind=claim_kind)
    assert assessment.allowed is False
    assert assessment.export_allowed is False
    assert reason in assessment.reason_codes


@pytest.mark.parametrize(
    ("claim", "reason"),
    [
        ("This result makes demand stronger.", "causal_claim_unsupported"),
        ("This result produced stronger demand.", "causal_claim_unsupported"),
        (
            "South Africans aged 18 to 24 prefer this result.",
            "demographic_claim_unsupported",
        ),
        (
            "People aged eighteen to twenty four prefer this result.",
            "demographic_claim_unsupported",
        ),
    ],
)
def test_prose_semantics_cannot_be_laundered_as_descriptive(claim, reason) -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
    assessment = assess_strategist_claim(claim, (projection,), claim_kind="descriptive")
    assert assessment.allowed is False
    assert assessment.export_allowed is False
    assert reason in assessment.reason_codes


def test_ai_citation_claim_requires_a_direct_positive_citation_target() -> None:
    payload = _fixture("ai_citation_direct_v1.json")
    payload.update(
        {
            "result_position": None,
            "result_type": "direct_provider_measurement",
            "result_url": None,
            "result_domain": None,
            "title": None,
            "snippet_digest": None,
            "ai_citation_present": False,
            "ai_citation_target": None,
        }
    )
    observation = build_observation(payload, created_at="2026-08-31T06:05:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
    assessment = assess_strategist_claim(
        "The AI cited example.org for this query.",
        (projection,),
        claim_kind="ai_citation",
    )
    assert assessment.allowed is False
    assert assessment.export_allowed is False
    assert "citation_not_observed" in assessment.reason_codes


@pytest.mark.parametrize(
    "claim_template",
    [
        "The AI did not cite {domain} for this query.",
        "The AI omitted {domain} from its citations.",
    ],
)
def test_ai_citation_claim_cannot_reverse_observed_presence(claim_template) -> None:
    observation = build_observation(
        _fixture("ai_citation_direct_v1.json"),
        created_at="2026-08-31T06:05:01.000000Z",
    )
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
    assessment = assess_strategist_claim(
        claim_template.format(domain=observation.result_domain),
        (projection,),
        claim_kind="ai_citation",
    )
    assert assessment.allowed is False
    assert assessment.export_allowed is False
    assert "citation_polarity_unsupported" in assessment.reason_codes


def test_exact_observed_ai_citation_claim_is_exportable() -> None:
    observation = build_observation(
        _fixture("ai_citation_direct_v1.json"),
        created_at="2026-08-31T06:05:01.000000Z",
    )
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
    assessment = assess_strategist_claim(
        f"The AI cited {observation.result_domain} for this query.",
        (projection,),
        claim_kind="ai_citation",
    )
    assert assessment.allowed is True
    assert assessment.export_allowed is True
    assert assessment.reason_codes == ()


def test_exact_observed_descriptive_claim_is_exportable() -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
    assessment = assess_strategist_claim(
        "publisher.example was observed at position 1 for this query.",
        (projection,),
        claim_kind="descriptive",
    )
    assert assessment.allowed is True
    assert assessment.export_allowed is True
    assert assessment.reason_codes == ()


def test_unstructured_descriptive_claim_fails_closed() -> None:
    observation = _organic_observation(created_at="2026-08-31T06:00:01.000000Z")
    projection = strategist_projection(asdict(observation), as_of="2026-08-31T06:30:00.000000Z")
    assessment = assess_strategist_claim(
        "A cited content opportunity exists for this query.",
        (projection,),
        claim_kind="descriptive",
    )
    assert assessment.allowed is False
    assert assessment.export_allowed is False
    assert "descriptive_claim_unsupported" in assessment.reason_codes


def test_unavailable_observations_never_form_a_historical_comparison() -> None:
    payload = _fixture("unavailable_failed_read_v1.json")
    first = build_observation(payload, created_at="2026-08-31T06:00:01.000000Z")
    payload["observed_at"] = "2026-08-31T06:01:00.000000Z"
    second = build_observation(payload, created_at="2026-08-31T06:01:01.000000Z")
    comparison = assess_historical_comparison((first, second), as_of="2026-08-31T06:30:00.000000Z")
    assert comparison.state == "unavailable"
    assert comparison.directly_comparable is False
    assert comparison.observation_ids == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_scope_id", "person@example.org"),
        ("request_id", "request@example.org"),
        ("run_id", "run@example.org"),
    ],
)
def test_request_identifiers_are_opaque_and_cannot_carry_email_pii(field, value) -> None:
    payload = _fixture("request_public_research_v2.json")
    payload[field] = value
    with pytest.raises(ValueError, match=r"identifier|public research"):
        _validate_public_request(payload)


def test_market_aware_phone_pii_is_rejected_from_public_queries() -> None:
    payload = _fixture("request_public_research_v2.json")
    payload["markets"] = ["ke"]
    payload["queries"] = ["commuter complaint +254 712 345 678"]
    with pytest.raises(ValueError, match="public research"):
        _validate_public_request(payload)


@pytest.mark.parametrize(
    "query",
    ["Albert Meintjes home address", "Albert Meintjes salary"],
)
def test_private_person_attribute_research_is_rejected(query) -> None:
    payload = _fixture("request_public_research_v2.json")
    payload["queries"] = [query]
    with pytest.raises(ValueError, match="public research"):
        _validate_public_request(payload)
