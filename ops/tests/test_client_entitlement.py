import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.onboarding import client_entitlement
from ops.onboarding.client_entitlement import (
    uncovered_contract_cells,
    validate_client_entitlement,
)

SCHEMA_SHA256 = "2e8b9e9d9314357b939b4adee2a8ee2941200b40b1e8f98953d2eb1261504001"
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
FEATURES = ["ai_answer_citation", "search_demand", "search_rank", "social"]


def unproven(evidence_ref=None):
    return {
        "status": "unproven",
        "evidence_ref": evidence_ref,
        "verified_at": None,
        "expires_at": None,
        "revoked_at": None,
    }


def current(expires_at="2027-01-01T00:00:00Z"):
    return {
        "status": "current",
        "evidence_ref": "synthetic-structure-fixture",
        "verified_at": "2026-01-01T00:00:00Z",
        "expires_at": expires_at,
        "revoked_at": None,
    }


def synthetic_document():
    markets = ["DE", "ZA"]
    cells = sorted(
        (market.lower(), feature) for market in markets for feature in FEATURES
    )
    proof = {key: unproven() for key in client_entitlement.TOP_PROOF_LEAVES}
    rights = []
    for feature in FEATURES:
        rights.append(
            {
                "right_id": f"{feature}_synthetic",
                "source_id": f"{feature}_source",
                "features": [feature],
                "markets": None,
                "permitted_uses": None,
                "observed_evidence": "Synthetic structure fixture only.",
                "proof": unproven("synthetic-fixture"),
            }
        )
    return {
        "contract_version": "client_entitlement_v1",
        "client_scope_id": "synthetic_client",
        "funding_basis_ref": None,
        "permitted_uses": None,
        "contracted_markets": list(markets),
        "comparator_markets": ["NG"],
        "supported_languages": [{"tag": "en", "name": "English"}],
        "source_credentials_refs": [
            {
                "client_scope_id": "synthetic_client",
                "source_id": "social_source",
                "resource": None,
                "proof": unproven("synthetic-fixture"),
            }
        ],
        "review_roles": ["commercial_decision_owner", "finance"],
        "retention_policy_ref": None,
        "commercial_approval_ref": None,
        "expires_at": None,
        "source_rights": rights,
        "identity_assets": [
            {
                "asset_id": "synthetic_identity",
                "asset_ref": None,
                "rights_ref": None,
                "proof": unproven("synthetic-fixture"),
            }
        ],
        "proof": proof,
        "market_roles": {
            "roles": [
                {"role_id": "domestic", "markets": ["ZA"]},
                {"role_id": "published_study_origins", "markets": ["DE"]},
            ],
            "required_union": list(markets),
            "observation_market_and_discussed_country_are_distinct": True,
        },
        "comparator_roles": {
            "roles": [
                {"role_id": "client_contract", "markets": ["NG"]},
                {"role_id": "published_study_design", "markets": ["EG"]},
            ],
            "sets_are_distinct": True,
        },
        "familiarity": {
            "unit": "respondent_cohort",
            "labels": [
                {"cohort_id": "familiar", "display_name": "Experience"},
                {"cohort_id": "non_familiar", "display_name": "Influence"},
            ],
            "basis": "Synthetic respondent-level fixture.",
            "country_wide_binary_forbidden": True,
            "unsupported_cohort_value": "unknown",
        },
        "search": {
            "tracked_queries": ["synthetic query"],
            "benchmark_domains": [
                {
                    "service": "Synthetic benchmark",
                    "canonical_domain": "example.test",
                    "aliases": None,
                    "proof": unproven("synthetic-fixture"),
                }
            ],
            "surfaces": [
                "ai_answer_citation",
                "search_demand",
                "search_rank",
            ],
            "surface_substitution_forbidden": True,
        },
        "coverage": {
            "matrix_status": "unfrozen",
            "initial_digital_subset": {
                "subset_id": "synthetic_digital_subset",
                "description": "Synthetic two-market digital subset.",
                "markets": list(markets),
                "features": FEATURES,
                "required_cells": [list(cell) for cell in cells],
                "document_claimed_current_cells": [],
                "native_verified_cells": [],
                "uncovered_cells": [list(cell) for cell in cells],
                "subset_complete": False,
            },
            "full_required_coverage": {
                "matrix_ref": None,
                "complete": False,
                "required_cells": None,
                "unresolved_features": ["print", "radio", "television"],
                "open_dependencies": ["d02"],
                "reason": "Synthetic full matrix remains open.",
                "reduced_scope_amendment_ref": None,
            },
        },
    }


def make_all_current(document):
    value = deepcopy(document)
    value["funding_basis_ref"] = "synthetic/funding"
    value["permitted_uses"] = ["synthetic_use"]
    value["retention_policy_ref"] = "synthetic/retention"
    value["commercial_approval_ref"] = "synthetic/commercial"
    value["expires_at"] = "2027-01-01T00:00:00Z"
    value["proof"] = {key: current() for key in client_entitlement.TOP_PROOF_LEAVES}
    for row in value["source_rights"]:
        row["markets"] = value["contracted_markets"]
        row["permitted_uses"] = ["synthetic_use"]
        row["proof"] = current()
    credential = value["source_credentials_refs"][0]
    credential["resource"] = (
        "projects/synthetic-project/secrets/synthetic-secret/versions/7"
    )
    credential["proof"] = current()
    asset = value["identity_assets"][0]
    asset["asset_ref"] = "synthetic/asset"
    asset["rights_ref"] = "synthetic/asset-rights"
    asset["proof"] = current()
    benchmark = value["search"]["benchmark_domains"][0]
    benchmark["aliases"] = ["www.example.test"]
    benchmark["proof"] = current()
    return value


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(client_entitlement, "_utc_now", lambda: NOW)


def test_committed_schema_matches_reviewed_bytes():
    schema_path = Path(client_entitlement.__file__).parents[2] / (
        "engine/configs/client_onboarding.schema.json"
    )
    assert hashlib.sha256(schema_path.read_bytes()).hexdigest() == SCHEMA_SHA256


def test_valid_unproven_document_returns_exact_safe_output():
    document = synthetic_document()
    result = validate_client_entitlement(document)
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert result == {
        "contract_version": "client_entitlement_validation_v1",
        "structure_valid": True,
        "client_scope_id": "synthetic_client",
        "document_sha256": hashlib.sha256(canonical).hexdigest(),
        "document_claim_state": "unproven",
        "initial_digital_subset_uncovered_cells": document["coverage"][
            "initial_digital_subset"
        ]["uncovered_cells"],
        "full_required_coverage_complete": False,
        "coverage_matrix_status": "unfrozen",
        "native_rights_verified": False,
        "activation_ready": False,
        "errors": [],
    }


def test_canonical_digest_uses_utf8_unicode_without_trailing_newline():
    document = synthetic_document()
    document["search"]["tracked_queries"] = ["café"]
    expected = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert not expected.endswith(b"\n")
    assert (
        validate_client_entitlement(document)["document_sha256"]
        == hashlib.sha256(expected).hexdigest()
    )


def test_unknown_secret_like_key_is_never_echoed():
    document = synthetic_document()
    sentinel = "SYNTHETIC_SECRET_VALUE_DO_NOT_ECHO"
    document["source_rights"][0][sentinel] = "synthetic"
    result = validate_client_entitlement(document)
    assert result["structure_valid"] is False
    assert result["client_scope_id"] is None
    assert result["document_sha256"] is None
    assert sentinel not in json.dumps(result)
    assert result["errors"] == [{"code": "unknown_field", "path": "/source_rights/0"}]


def test_missing_full_study_membership_is_invalid():
    document = synthetic_document()
    document["contracted_markets"].remove("DE")
    document["market_roles"]["required_union"].remove("DE")
    subset = document["coverage"]["initial_digital_subset"]
    subset["markets"].remove("DE")
    subset["required_cells"] = [
        cell for cell in subset["required_cells"] if cell[0] != "de"
    ]
    subset["uncovered_cells"] = [
        cell for cell in subset["uncovered_cells"] if cell[0] != "de"
    ]
    result = validate_client_entitlement(document)
    assert result["errors"] == [
        {"code": "requirements_incomplete", "path": "/market_roles"}
    ]


def test_revoked_source_right_wins_aggregation():
    document = make_all_current(synthetic_document())
    document["source_rights"][0]["proof"] = current() | {
        "status": "revoked",
        "revoked_at": "2026-05-01T00:00:00Z",
    }
    result = validate_client_entitlement(document)
    assert result["document_claim_state"] == "revoked"
    assert result["full_required_coverage_complete"] is False


def test_expired_authorization_is_valid_but_not_current():
    document = make_all_current(synthetic_document())
    document["proof"]["commercial_approval_ref"] = current("2026-05-01T00:00:00Z") | {
        "status": "expired"
    }
    assert validate_client_entitlement(document)["document_claim_state"] == "expired"


def test_future_verification_returns_non_echoing_invalid_proof_error():
    document = make_all_current(synthetic_document())
    document["identity_assets"][0]["proof"]["verified_at"] = "2026-07-01T00:00:00Z"
    result = validate_client_entitlement(document)
    assert result["document_claim_state"] == "invalid"
    assert result["client_scope_id"] is None
    assert result["errors"] == [
        {"code": "invalid_proof_status", "path": "/identity_assets/0/proof"}
    ]


def test_foreign_declared_credential_client_is_rejected_without_echo():
    document = synthetic_document()
    document["source_credentials_refs"][0]["client_scope_id"] = "foreign_client"
    result = validate_client_entitlement(document)
    assert result["errors"] == [
        {
            "code": "credential_declared_client_mismatch",
            "path": "/source_credentials_refs/0",
        }
    ]
    assert "foreign_client" not in json.dumps(result)


@pytest.mark.parametrize(
    ("resource", "code"),
    [
        ("SYNTHETIC_SECRET_VALUE_DO_NOT_ECHO", "raw_secret_forbidden"),
        (
            "projects/synthetic-project/secrets/synthetic-secret/versions/latest",
            "secret_version_not_pinned",
        ),
        (
            "projects/1/secrets/synthetic-secret/versions/7",
            "credential_resource_invalid",
        ),
    ],
)
def test_raw_and_unpinned_credential_references_are_rejected(resource, code):
    document = synthetic_document()
    document["source_credentials_refs"][0]["resource"] = resource
    result = validate_client_entitlement(document)
    assert result["errors"] == [
        {"code": code, "path": "/source_credentials_refs/0/resource"}
    ]
    assert resource not in json.dumps(result)


def test_cross_source_credential_is_rejected():
    document = synthetic_document()
    document["source_credentials_refs"][0]["source_id"] = "missing_source"
    result = validate_client_entitlement(document)
    assert result["errors"] == [
        {"code": "credential_source_mismatch", "path": "/source_credentials_refs/0"}
    ]


@pytest.mark.parametrize(
    "proof_path",
    ["asset", "credential", "source_right", "benchmark"],
)
def test_each_nested_proof_location_changes_the_aggregate(proof_path):
    document = make_all_current(synthetic_document())
    if proof_path == "asset":
        document["identity_assets"][0]["proof"] = unproven()
        document["identity_assets"][0]["asset_ref"] = None
        document["identity_assets"][0]["rights_ref"] = None
    elif proof_path == "credential":
        document["source_credentials_refs"][0]["proof"] = unproven()
        document["source_credentials_refs"][0]["resource"] = None
    elif proof_path == "source_right":
        document["source_rights"][0]["proof"] = unproven()
        document["source_rights"][0]["source_id"] = None
        document["source_rights"][0]["markets"] = None
        document["source_rights"][0]["permitted_uses"] = None
    else:
        document["search"]["benchmark_domains"][0]["proof"] = unproven()
    assert validate_client_entitlement(document)["document_claim_state"] == "unproven"


@pytest.mark.parametrize("proof_key", client_entitlement.TOP_PROOF_LEAVES)
def test_each_top_level_proof_location_changes_the_aggregate(proof_key):
    document = make_all_current(synthetic_document())
    document["proof"][proof_key] = unproven()
    document[proof_key] = None
    assert validate_client_entitlement(document)["document_claim_state"] == "unproven"


def test_mixed_revoked_and_expired_resolves_revoked():
    document = make_all_current(synthetic_document())
    document["source_rights"][0]["proof"] = current() | {
        "status": "expired",
        "expires_at": "2026-05-01T00:00:00Z",
    }
    document["identity_assets"][0]["proof"] = current() | {
        "status": "revoked",
        "revoked_at": "2026-05-01T00:00:00Z",
    }
    assert validate_client_entitlement(document)["document_claim_state"] == "revoked"


def test_clock_crosses_current_expiry(monkeypatch):
    document = make_all_current(synthetic_document())
    assert validate_client_entitlement(document)["document_claim_state"] == (
        "claimed_current"
    )
    monkeypatch.setattr(
        client_entitlement,
        "_utc_now",
        lambda: datetime(2027, 1, 2, tzinfo=timezone.utc),
    )
    assert validate_client_entitlement(document)["document_claim_state"] == "expired"


def set_all_proof_expiries(document, expires_at):
    for key in client_entitlement.TOP_PROOF_LEAVES:
        document["proof"][key]["expires_at"] = expires_at
    for row in document["source_rights"]:
        row["proof"]["expires_at"] = expires_at
    for row in document["source_credentials_refs"]:
        row["proof"]["expires_at"] = expires_at
    for row in document["identity_assets"]:
        row["proof"]["expires_at"] = expires_at
    for row in document["search"]["benchmark_domains"]:
        row["proof"]["expires_at"] = expires_at


def test_one_nanosecond_future_overall_expiry_remains_current(monkeypatch):
    document = make_all_current(synthetic_document())
    set_all_proof_expiries(document, "2027-01-01T00:00:00.000000002Z")
    document["expires_at"] = "2027-01-01T00:00:00.000000001Z"
    monkeypatch.setattr(
        client_entitlement,
        "_utc_now",
        lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    assert validate_client_entitlement(document)["document_claim_state"] == (
        "claimed_current"
    )


def test_verification_one_nanosecond_after_expiry_is_invalid(monkeypatch):
    document = make_all_current(synthetic_document())
    proof = document["source_rights"][0]["proof"]
    proof["verified_at"] = "2026-01-01T00:00:00.000000002Z"
    proof["expires_at"] = "2026-01-01T00:00:00.000000001Z"
    monkeypatch.setattr(
        client_entitlement,
        "_utc_now",
        lambda: datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    result = validate_client_entitlement(document)
    assert result["errors"] == [
        {"code": "invalid_proof_status", "path": "/source_rights/0/proof"}
    ]


def test_expiry_exactly_at_clock_is_expired(monkeypatch):
    document = make_all_current(synthetic_document())
    set_all_proof_expiries(document, "2027-01-01T00:00:00.000000000Z")
    document["expires_at"] = "2027-01-01T00:00:00Z"
    monkeypatch.setattr(
        client_entitlement,
        "_utc_now",
        lambda: datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    assert validate_client_entitlement(document)["document_claim_state"] == "expired"


@pytest.mark.parametrize(
    ("clock_microsecond", "expiry", "expected"),
    [
        (100_000, "2027-01-01T00:00:00.100000001Z", "claimed_current"),
        (100_001, "2027-01-01T00:00:00.100000999Z", "expired"),
    ],
)
def test_mixed_fractional_precision_uses_integer_nanoseconds(
    monkeypatch, clock_microsecond, expiry, expected
):
    document = make_all_current(synthetic_document())
    set_all_proof_expiries(document, expiry)
    document["expires_at"] = expiry
    monkeypatch.setattr(
        client_entitlement,
        "_utc_now",
        lambda: datetime(
            2027, 1, 1, microsecond=clock_microsecond, tzinfo=timezone.utc
        ),
    )
    assert validate_client_entitlement(document)["document_claim_state"] == expected


def test_domestic_social_does_not_close_international_search():
    required = {("za", "social"), ("de", "search_rank")}
    proven = {("za", "social")}
    assert uncovered_contract_cells(required, proven) == [("de", "search_rank")]


def test_out_of_scope_proven_cell_is_rejected():
    with pytest.raises(ValueError, match="^contract_cell_invalid$"):
        uncovered_contract_cells({("za", "social")}, {("de", "search_rank")})


def test_full_coverage_status_cannot_disagree_with_full_matrix_claim():
    document = synthetic_document()
    document["coverage"]["matrix_status"] = "document_claimed_complete"
    result = validate_client_entitlement(document)
    assert result["errors"] == [
        {"code": "requirements_incomplete", "path": "/coverage/full_required_coverage"}
    ]


def test_complete_document_claim_still_cannot_activate_native_rights():
    document = make_all_current(synthetic_document())
    coverage = document["coverage"]
    coverage["matrix_status"] = "document_claimed_complete"
    full = coverage["full_required_coverage"]
    full["matrix_ref"] = "synthetic/full-matrix"
    full["complete"] = True
    full["required_cells"] = coverage["initial_digital_subset"]["required_cells"]
    full["unresolved_features"] = []
    full["open_dependencies"] = []
    result = validate_client_entitlement(document)
    assert result["document_claim_state"] == "claimed_current"
    assert result["full_required_coverage_complete"] is True
    assert result["native_rights_verified"] is False
    assert result["activation_ready"] is False


def test_validation_uses_packaged_schema_and_calls_no_native_boundary(monkeypatch):
    monkeypatch.setattr(
        "builtins.open", lambda *args, **kwargs: pytest.fail("I/O called")
    )
    result = validate_client_entitlement(synthetic_document())
    assert result["native_rights_verified"] is False
    assert result["activation_ready"] is False
