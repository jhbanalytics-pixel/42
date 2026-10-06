import copy
from datetime import UTC, datetime, timedelta, timezone

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_policy import (
    build_intake_context,
    build_question_policy,
    require_question_admission_policy,
    validate_intake_context,
    validate_question_policy,
)
from src.analysis.open_intelligence.general_question_request import (
    QuestionRequestInvalid,
    normalize_question_request,
)

VERIFIED_AT = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def _policy(**kwargs):
    return build_question_policy(pricing_verified_at=VERIFIED_AT, **kwargs)


def _scope():
    return {
        "client_scope_id": "synthetic_scope",
        "market_scope": ["ng", "za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
    }


def _request():
    return normalize_question_request(
        {"message": "What is changing in synthetic mobility?", "history": []},
        scope=_scope(),
        request_id="b5b3c8a0-6f54-4a27-9d9c-3c3f662e1f30",
        admitted_at=datetime(2026, 9, 6, 10, 11, 12, tzinfo=UTC),
        policy_digest="b" * 64,
    )


def _reseal_policy(value):
    value["policy_digest"] = canonical_digest(
        {key: item for key, item in value.items() if key != "policy_digest"}
    )
    return value


def _reseal_intake(value):
    value["intake_digest"] = canonical_digest(
        {key: item for key, item in value.items() if key != "intake_digest"}
    )
    return value


def test_builds_exact_fixed_policy_for_current_model_contract():
    verified = datetime(2026, 9, 6, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    policy = build_question_policy(pricing_verified_at=verified)

    assert policy == _reseal_policy(
        {
            "contract_version": "general_question_policy_v1",
            "policy_id": "general_cultural_question_staging_eval_v1",
            "approval_contract_digest": (
                "2885012a99bfc352c9ad92ced7b5bddd0b022f88e8ddc9d7688f237179062a8b"
            ),
            "model": "gemini-3.8-flash",
            "project": "ogilvy-trends-v2",
            "location": "global",
            "api_version": "v1",
            "service_tier": "standard",
            "expires_at": "2026-09-30T23:59:59Z",
            "limits": {
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
                "deadline_seconds": 240,
                "model_timeout_seconds": 60,
            },
            "stages": {
                "planning": {
                    "thinking_level": "LOW",
                    "input_tokens": 8_000,
                    "output_tokens": 800,
                },
                "answering": {
                    "thinking_level": "LOW",
                    "input_tokens": 32_000,
                    "output_tokens": 4_000,
                },
            },
            "pricing": {
                "input_usd_per_million": "0.750000",
                "output_usd_per_million": "3.750000",
                "verified_at": "2026-09-06T10:00:00.000000Z",
                "source_url": (
                    "https://cloud.google.com/gemini-enterprise-agent-platform/"
                    "generative-ai/pricing"
                ),
            },
        }
    )


def test_policy_validation_returns_detached_data_without_activation_capability():
    policy = _policy()
    validated = validate_question_policy(policy)
    validated["limits"]["requests"] = 1

    assert policy["limits"]["requests"] == 100
    assert isinstance(policy, dict)
    assert set(policy) == {
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


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("limits", "requests"), True),
        (("limits", "requests"), 101),
        (("limits", "billed_bytes"), 0),
        (("stages", "planning", "thinking_level"), "MEDIUM"),
        (("stages", "answering", "input_tokens"), 8_000),
        (("model",), "gemini-3.7-flash"),
        (("project",), "other-project"),
        (("expires_at",), "2026-10-01T00:00:00Z"),
        (("pricing", "source_url"), "https://example.invalid/pricing"),
    ],
)
def test_resealed_policy_cannot_change_fixed_contract(path, value):
    policy = _policy()
    target = policy
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    _reseal_policy(policy)

    with pytest.raises(ValueError):
        validate_question_policy(policy)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_usd_per_million", "1.5"),
        ("input_usd_per_million", "0.000000"),
        ("input_usd_per_million", 1.5),
        ("output_usd_per_million", "-9.000000"),
        ("output_usd_per_million", "9.00000"),
        ("output_usd_per_million", True),
    ],
)
def test_build_refuses_invalid_price_types_and_shapes(field, value):
    kwargs = {field: value}
    with pytest.raises(ValueError, match="pricing"):
        _policy(**kwargs)


@pytest.mark.parametrize("verified", [None, "2026-09-06T10:00:00Z", datetime(2026, 9, 6)])
def test_build_requires_explicit_aware_pricing_review_time(verified):
    with pytest.raises(ValueError, match="pricing verified_at"):
        build_question_policy(pricing_verified_at=verified)


def test_policy_unknown_missing_and_digest_fields_refuse():
    for mutate in (
        lambda value: value.update(extra=True),
        lambda value: value.pop("pricing"),
        lambda value: value["pricing"].update(extra=True),
        lambda value: value["stages"].pop("planning"),
        lambda value: value.update(policy_digest="0" * 64),
    ):
        policy = _policy()
        mutate(policy)
        with pytest.raises(ValueError):
            validate_question_policy(policy)


def test_historical_deadline_policy_remains_valid_but_other_deadlines_refuse():
    historical = _policy(
        approval_contract_digest="519a658ad91b3b4d447298611d259421bbac5b7765d7c0f0049dc8bdd08c5857"
    )
    historical["limits"]["deadline_seconds"] = 180
    assert validate_question_policy(_reseal_policy(historical)) == historical

    for seconds in (179, 181, 239, 241, 300, True):
        unsupported = _policy()
        unsupported["limits"]["deadline_seconds"] = seconds
        with pytest.raises(ValueError, match="limits"):
            validate_question_policy(_reseal_policy(unsupported))


def test_admission_accepts_exact_24_hour_review_and_fixed_expiry_boundary():
    policy = _policy()
    admitted = require_question_admission_policy(policy, now=VERIFIED_AT + timedelta(hours=24))
    assert admitted == policy
    expiry_policy = build_question_policy(
        pricing_verified_at=datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    )
    assert (
        require_question_admission_policy(
            expiry_policy, now=datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
        )
        == expiry_policy
    )


@pytest.mark.parametrize(
    ("verified_at", "now", "reason"),
    [
        (VERIFIED_AT, VERIFIED_AT - timedelta(microseconds=1), "future"),
        (VERIFIED_AT, VERIFIED_AT + timedelta(hours=24, microseconds=1), "stale"),
        (
            datetime(2026, 9, 30, 23, 0, tzinfo=UTC),
            datetime(2026, 10, 1, tzinfo=UTC),
            "expired",
        ),
    ],
)
def test_admission_refuses_future_stale_or_expired_policy(verified_at, now, reason):
    policy = build_question_policy(pricing_verified_at=verified_at)
    with pytest.raises(ValueError, match=reason):
        require_question_admission_policy(policy, now=now)


def test_admission_refuses_prices_above_request_reservation_without_invalidating_recovery():
    policy = _policy(input_usd_per_million="2.000000", output_usd_per_million="10.000000")
    assert validate_question_policy(policy) == policy

    with pytest.raises(ValueError, match="cost"):
        require_question_admission_policy(policy, now=VERIFIED_AT)


@pytest.mark.parametrize("now", [None, "2026-09-06T10:00:00Z", datetime(2026, 9, 6)])
def test_admission_requires_explicit_aware_now(now):
    with pytest.raises(ValueError, match="now"):
        require_question_admission_policy(_policy(), now=now)


def test_builds_exact_intake_context_and_preserves_request():
    request = _request()
    before = copy.deepcopy(request)

    intake = build_intake_context(request, selected_market="ng")

    assert intake == _reseal_intake(
        {
            "contract_version": "general_question_intake_context_v1",
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "selected_market": "ng",
            "captured_at": request["as_of"],
        }
    )
    assert request == before
    assert validate_intake_context(intake, request=request) == intake


def test_intake_null_selection_means_all_and_absent_field_refuses():
    request = _request()
    intake = build_intake_context(request, selected_market=None)
    assert intake["selected_market"] is None
    missing = copy.deepcopy(intake)
    missing.pop("selected_market")
    _reseal_intake(missing)

    with pytest.raises(QuestionRequestInvalid):
        validate_intake_context(missing, request=request)


@pytest.mark.parametrize("selected", ["ke", "all", "NG", "", "   ", 1, True])
def test_intake_refuses_scope_expansion_or_invalid_selection(selected):
    with pytest.raises(QuestionRequestInvalid, match="scope_invalid"):
        build_intake_context(_request(), selected_market=selected)


def test_intake_validation_rejects_unknown_fields_binding_changes_and_changed_selection():
    request = _request()
    intake = build_intake_context(request, selected_market="za")
    mutations = []
    unknown = copy.deepcopy(intake)
    unknown["extra"] = True
    mutations.append(unknown)
    for field, value in (
        ("request_id", "00000000-0000-4000-8000-000000000001"),
        ("request_digest", "0" * 64),
        ("captured_at", "2026-09-06T10:11:13.000000Z"),
        ("selected_market", "ng"),
        ("intake_digest", "0" * 64),
    ):
        changed = copy.deepcopy(intake)
        changed[field] = value
        if field != "intake_digest":
            _reseal_intake(changed)
        mutations.append(changed)

    for value in mutations:
        if value.get("selected_market") == "ng" and set(value) == set(intake):
            assert validate_intake_context(value, request=request) == value
            assert value["intake_digest"] != intake["intake_digest"]
        else:
            with pytest.raises(QuestionRequestInvalid):
                validate_intake_context(value, request=request)


def test_invalid_normalized_request_refuses_without_exposing_question():
    request = _request()
    request["question"] = "SYNTHETIC_PRIVATE_QUESTION"

    with pytest.raises(QuestionRequestInvalid) as raised:
        build_intake_context(request, selected_market=None)

    assert "SYNTHETIC_PRIVATE_QUESTION" not in str(raised.value)


def test_legacy_medium_and_amended_low_profiles_are_digest_bound():
    current = _policy()
    assert current["stages"]["planning"]["thinking_level"] == "LOW"
    legacy = _policy(
        approval_contract_digest="a0410e47911ff343ef77726e4ffefbfd57f275e9cf341633a0a9fd1b01e506e0"
    )
    legacy["stages"]["planning"]["thinking_level"] = "MEDIUM"
    legacy["stages"]["answering"]["thinking_level"] = "HIGH"
    legacy = _reseal_policy(legacy)
    assert validate_question_policy(legacy) == legacy
    assert legacy["limits"] == current["limits"]
    for source, wrong in [(legacy, "LOW"), (current, "MEDIUM")]:
        changed = copy.deepcopy(source)
        changed["stages"]["planning"]["thinking_level"] = wrong
        with pytest.raises(ValueError):
            validate_question_policy(_reseal_policy(changed))


def test_three_thinking_profiles_require_exact_amendment_digest():
    current = _policy()
    assert current["stages"]["answering"]["thinking_level"] == "LOW"
    for digest, planning, answering in [
        ("a0410e47911ff343ef77726e4ffefbfd57f275e9cf341633a0a9fd1b01e506e0", "MEDIUM", "HIGH"),
        ("99c597f7a083ea14d4c87c34bbccdb6d5cea5958ba466352891b8d06731c4389", "LOW", "HIGH"),
        ("1306582d25499311bd70c59e1e25804b5219f7494a30a18a59b03c9ad3d474c5", "LOW", "MEDIUM"),
        (current["approval_contract_digest"], "LOW", "LOW"),
    ]:
        value = _policy(approval_contract_digest=digest)
        value["stages"]["planning"]["thinking_level"] = planning
        value["stages"]["answering"]["thinking_level"] = answering
        assert validate_question_policy(_reseal_policy(value))["limits"] == current["limits"]
        value["stages"]["answering"]["thinking_level"] = "MEDIUM" if answering == "HIGH" else "HIGH"
        with pytest.raises(ValueError):
            validate_question_policy(_reseal_policy(value))


EXTENDED_EXPIRES_AT = "2026-12-31T23:59:59Z"


def test_the_extended_expiry_is_the_one_other_expiry_a_policy_may_carry():
    extended = _policy(expires_at=EXTENDED_EXPIRES_AT)
    assert extended["expires_at"] == EXTENDED_EXPIRES_AT
    assert validate_question_policy(extended) == extended
    fixed = _policy()
    assert fixed["expires_at"] == "2026-09-30T23:59:59Z"
    assert validate_question_policy(fixed) == fixed
    # Only the expiry and the digest over it differ between the two.
    assert {key for key in fixed if fixed[key] != extended[key]} == {
        "expires_at",
        "policy_digest",
    }


@pytest.mark.parametrize(
    "expires_at",
    [
        "2026-12-31T23:59:58Z",
        "2027-01-01T00:00:00Z",
        "2026-12-31T23:59:59.000000Z",
        "2026-12-31T23:59:59+00:00",
        None,
        datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC),
    ],
)
def test_build_and_validation_refuse_any_other_expiry(expires_at):
    with pytest.raises(ValueError, match="binding"):
        _policy(expires_at=expires_at)
    policy = _policy()
    policy["expires_at"] = expires_at
    with pytest.raises(ValueError, match="binding"):
        validate_question_policy(_reseal_policy(policy))


def test_admission_holds_the_extended_policy_to_its_own_expiry():
    policy = build_question_policy(
        pricing_verified_at=datetime(2026, 12, 31, 23, 0, tzinfo=UTC),
        expires_at=EXTENDED_EXPIRES_AT,
    )
    boundary = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert require_question_admission_policy(policy, now=boundary) == policy
    with pytest.raises(ValueError, match="expired"):
        require_question_admission_policy(
            policy, now=boundary + timedelta(microseconds=1)
        )
    # The day after the fixed expiry the extended policy still admits.
    october = build_question_policy(
        pricing_verified_at=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        expires_at=EXTENDED_EXPIRES_AT,
    )
    assert (
        require_question_admission_policy(
            october, now=datetime(2026, 10, 1, 1, 0, tzinfo=UTC)
        )
        == october
    )
