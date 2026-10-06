"""Contract tests for the immutable recurring execution grant (C03)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.recurring_grant import (
    ALLOWED_OPERATIONS,
    EXCLUDED_OPERATIONS,
    GRANT_CONTRACT,
    STAGE_OPERATIONS,
    V1_REGISTRY_OPERATIONS,
    GrantRefusal,
    capture_grant_from,
    check_grant,
    grant_digest,
    proposed_grant,
    proposed_release_manifest,
    render_grant_proposal,
    validate_recurring_grant,
)
from src.analysis.open_intelligence.staging_source_profile import (
    evaluate_capture_reservation,
    validate_capture_grant,
)

DOMAIN = "ogilvy-trends-v2.iam.gserviceaccount.com"
ORCHESTRATION = f"intelligence-42-orchestration@{DOMAIN}"
INGEST = f"intelligence-42-ingest@{DOMAIN}"
BRAIN = f"intelligence-42-brain@{DOMAIN}"
PROOF = f"intelligence-42-proof@{DOMAIN}"
RELEASE = f"intelligence-42-release@{DOMAIN}"
IMAGE = "sha256:" + "4" * 64
VALID_FROM = datetime(2026, 9, 15, tzinfo=UTC)
NOW = datetime(2026, 9, 20, 4, 30, tzinfo=UTC)
CUTOFFS = [(VALID_FROM + timedelta(days=offset)).date().isoformat() for offset in range(30)]
IDENTITIES = {
    "orchestration": f"//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/{ORCHESTRATION}",
    "ingestion": f"//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/{INGEST}",
    "execution_brain_read": f"//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/{BRAIN}",
    "execution_r3_proof_issue": f"//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/{PROOF}",
    "execution_r3_release": f"//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/{RELEASE}",
    "scheduler": f"//iam.googleapis.com/projects/ogilvy-trends-v2/serviceAccounts/x@{DOMAIN}",
}


def grant(**overrides):
    base = {
        "contract_version": GRANT_CONTRACT,
        "grant_id": "recurring_execution_20260915_v1",
        "environment": "staging",
        "issuing_principal": "Albert Meintjes",
        "executing_principals": [RELEASE, ORCHESTRATION, INGEST, BRAIN, PROOF],
        "resource_manifest_digest": "b" * 64,
        "allowed_operations": list(ALLOWED_OPERATIONS),
        "source_policy_digest": "c" * 64,
        "schema_version": "42_daily_v1",
        "approved_release_manifest_digest": "e" * 64,
        "permitted_image_digests": [IMAGE],
        "run_allowance_micro_usd": 450000,
        "monthly_allowance_micro_usd": 15000000,
        "reserved_micro_usd_per_capture": 500000,
        "cumulative_ceiling_micro_usd": 15000000,
        "permitted_cutoffs": list(CUTOFFS),
        "valid_from": "2026-09-15T00:00:00+00:00",
        "valid_until": "2026-10-15T00:00:00+00:00",
        "retry_rules": {
            "failed_stage": "retry_safe_only",
            "unknown_or_partial": "hold_for_reconciliation",
            "max_attempt_versions": 3,
        },
        "revocation_state": "active",
        "revocation_phrase_sha256": "f" * 64,
    }
    base.update(overrides)
    return base


def check(value, **overrides):
    arguments = {
        "now": NOW,
        "operation": "collection",
        "principal": ORCHESTRATION,
        "manifest_digest": "b" * 64,
        "source_policy_digest": "c" * 64,
        "image_digest": IMAGE,
    }
    arguments.update(overrides)
    return check_grant(value, **arguments)


def test_valid_grant_normalizes_and_digest_is_canonical():
    checked = validate_recurring_grant(grant())
    assert checked["executing_principals"] == sorted([RELEASE, ORCHESTRATION, INGEST, BRAIN, PROOF])
    assert checked["valid_from"] == "2026-09-15T00:00:00+00:00"
    terms = {field: value for field, value in checked.items() if field != "revocation_state"}
    assert grant_digest(grant()) == sha256(canonical_bytes(terms)).hexdigest()
    assert grant_digest(grant(revocation_state="revoked")) == grant_digest(grant())
    reordered = grant(executing_principals=[BRAIN, PROOF, RELEASE, INGEST, ORCHESTRATION])
    assert grant_digest(reordered) == grant_digest(grant())
    assert grant_digest(grant(grant_id="recurring_execution_20260915_v2")) != grant_digest(grant())


def test_excluded_operations_are_disjoint_from_allowed_and_cover_c03():
    assert set(ALLOWED_OPERATIONS).isdisjoint(EXCLUDED_OPERATIONS)
    assert set(STAGE_OPERATIONS.values()) == set(ALLOWED_OPERATIONS)
    assert set(EXCLUDED_OPERATIONS) == {
        "migration",
        "iam_change",
        "production_write",
        "email_send",
        "increased_spending",
        "client_artifact_approval",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda g: g.pop("grant_id"),
        lambda g: g.update(extra="x"),
        lambda g: g.update(contract_version="42_recurring_execution_grant_v0"),
        lambda g: g.update(grant_id="Bad Id"),
        lambda g: g.update(environment="production"),
        lambda g: g.update(issuing_principal=""),
        lambda g: g.update(executing_principals=[]),
        lambda g: g.update(executing_principals=[ORCHESTRATION, ORCHESTRATION]),
        lambda g: g.update(allowed_operations=[]),
        lambda g: g.update(allowed_operations=["collection", "migration"]),
        lambda g: g.update(allowed_operations=["collection", "teleport"]),
        lambda g: g.update(schema_version="42_daily_v0"),
        lambda g: g.update(permitted_image_digests=["4" * 64]),
        lambda g: g.update(run_allowance_micro_usd=460000),
        lambda g: g.update(cumulative_ceiling_micro_usd=14999999),
        lambda g: g.update(monthly_allowance_micro_usd=14999999),
        lambda g: g.update(permitted_cutoffs=[*CUTOFFS, "2026-10-15"]),
        lambda g: g.update(permitted_cutoffs=[CUTOFFS[0], CUTOFFS[0]]),
        lambda g: g.update(permitted_cutoffs=[]),
        lambda g: g.update(valid_until="2026-09-15T00:00:00+00:00"),
        lambda g: g.update(valid_from="2026-09-15T00:00:00"),
        lambda g: g.update(retry_rules={"failed_stage": "always"}),
        lambda g: g.update(revocation_state="paused"),
        lambda g: g.update(revocation_phrase_sha256="zz"),
        lambda g: g.update(reserved_micro_usd_per_capture=0),
    ],
)
def test_invalid_grants_refuse_in_one_family(mutation):
    value = grant()
    mutation(value)
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_"):
        validate_recurring_grant(value)


def test_check_grant_admits_exact_call_and_reports_identity():
    decision = check(grant())
    assert decision["grant_id"] == "recurring_execution_20260915_v1"
    assert decision["grant_digest"] == grant_digest(grant())
    assert decision["operation"] == "collection"
    assert decision["principal"] == ORCHESTRATION


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"now": datetime(2026, 10, 15, tzinfo=UTC)}, "recurring_grant_expired"),
        ({"now": datetime(2026, 9, 14, 23, 59, tzinfo=UTC)}, "recurring_grant_not_yet_valid"),
        (
            {"principal": f"intelligence-42-migration@{DOMAIN}"},
            "recurring_grant_principal_mismatch",
        ),
        ({"manifest_digest": "a" * 64}, "recurring_grant_manifest_mismatch"),
        ({"source_policy_digest": "a" * 64}, "recurring_grant_source_policy_unsupported"),
        ({"operation": "migration"}, "recurring_grant_operation_excluded"),
        ({"operation": "email_send"}, "recurring_grant_operation_excluded"),
        ({"operation": "teleport"}, "recurring_grant_operation_forbidden"),
        ({"image_digest": "sha256:" + "5" * 64}, "recurring_grant_image_forbidden"),
        ({"image_digest": ""}, "recurring_grant_image_forbidden"),
        (
            {"cutoff_utc": datetime(2026, 10, 15, tzinfo=UTC)},
            "recurring_grant_cutoff_forbidden",
        ),
        (
            {"cutoff_utc": datetime(2026, 9, 16, 12, tzinfo=UTC)},
            "recurring_grant_cutoff_forbidden",
        ),
    ],
)
def test_check_grant_refusals(overrides, code):
    with pytest.raises(GrantRefusal, match=rf"^{code}$"):
        check(grant(), **overrides)


def test_revoked_grant_and_disallowed_operation_refuse():
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_revoked$"):
        check(grant(revocation_state="revoked"))
    narrowed = grant(allowed_operations=["collection", "immutable_capture"])
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_operation_forbidden$"):
        check(narrowed, operation="composition")
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_operation_excluded$"):
        check(grant(), operation="client_artifact_approval")


def test_excluded_operation_is_refused_even_when_a_grant_lists_it():
    listed = grant(allowed_operations=[*ALLOWED_OPERATIONS, "production_write"])
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_"):
        check(listed, operation="production_write")


def test_permitted_cutoff_admits_midnight_instant():
    decision = check(grant(), cutoff_utc=datetime(2026, 9, 16, tzinfo=UTC))
    assert decision["cutoff"] == "2026-09-16"


def test_capture_grant_projection_feeds_the_existing_reservation_path():
    projection = capture_grant_from(grant())
    checked = validate_capture_grant(projection, now=NOW)
    assert checked["reserved_micro_usd_per_capture"] == 500000
    assert checked["cumulative_ceiling_micro_usd"] == 15000000
    assert checked["allowed_cutoffs"] == sorted(CUTOFFS)
    assert checked["contract_sha256"] == grant_digest(grant())
    decision = evaluate_capture_reservation(
        projection,
        now=NOW,
        cutoff="2026-09-16",
        mode="initial",
        grant_id="recurring_execution_20260915_v1",
        source_estate_digest="c" * 64,
        ledger={"slot_initial_count": 0, "slot_recovery_count": 0, "reserved_initial_count": 3},
    )
    assert decision["reserved_micro_usd"] == 2000000


APPROVER_HASH = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
# The 14 September proposal document, rendered with the human name as issuing principal
# from the manifest at 17aa67c3 (digest f8ef93da), source policy b219e607 and
# valid_from 2026-09-15 UTC; the identities it binds are exactly IDENTITIES.
SEPTEMBER_DOCUMENT_SHA256 = "c1a754514869f8ad9f60c44fd377dc39e2f42fa811827826e3925d4b9f089e28"
SEPTEMBER_GRANT_DIGEST_PREFIX = "2706c698"


def test_proposed_grant_carries_the_issuing_principal_it_is_given():
    proposal = proposed_grant(
        identities=IDENTITIES,
        issuing_principal=APPROVER_HASH,
        resource_manifest_digest="b" * 64,
        source_policy_digest="c" * 64,
        valid_from=VALID_FROM,
    )
    assert proposal["issuing_principal"] == APPROVER_HASH
    assert validate_recurring_grant(proposal)["issuing_principal"] == APPROVER_HASH
    with pytest.raises(TypeError):
        proposed_grant(
            identities=IDENTITIES,
            resource_manifest_digest="b" * 64,
            source_policy_digest="c" * 64,
            valid_from=VALID_FROM,
        )
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_invalid$"):
        proposed_grant(
            identities=IDENTITIES,
            issuing_principal="",
            resource_manifest_digest="b" * 64,
            source_policy_digest="c" * 64,
            valid_from=VALID_FROM,
        )


def test_proposed_grant_with_the_september_principal_reproduces_the_approved_rendering():
    proposal = proposed_grant(
        identities=IDENTITIES,
        issuing_principal="Albert Meintjes",
        resource_manifest_digest="f8ef93da7f4108bac7274d254648106d6c1ce6ce6ff32513d3a576ee9fe260e2",
        source_policy_digest="b219e607d9dc85336089815711469b5cc8bc28451af3868b8c58b67e56a533c8",
        valid_from=VALID_FROM,
    )
    rendered = render_grant_proposal(proposal).encode("utf-8")
    assert sha256(rendered).hexdigest() == SEPTEMBER_DOCUMENT_SHA256
    assert grant_digest(validate_recurring_grant(proposal)).startswith(
        SEPTEMBER_GRANT_DIGEST_PREFIX
    )


def test_proposed_grant_collapses_keys_sharing_one_account_and_keeps_distinct_ones():
    # Since amendment d the two r3 keys name the orchestration account, so the five
    # executing identity keys resolve to three principals; five distinct accounts still
    # yield five, and the uniqueness rule on the grant itself is unchanged.
    folded = dict(IDENTITIES)
    folded["execution_r3_proof_issue"] = IDENTITIES["orchestration"]
    folded["execution_r3_release"] = IDENTITIES["orchestration"]
    proposal = proposed_grant(
        issuing_principal="Albert Meintjes",
        identities=folded,
        resource_manifest_digest="b" * 64,
        source_policy_digest="c" * 64,
        valid_from=VALID_FROM,
    )
    assert proposal["executing_principals"] == sorted([ORCHESTRATION, INGEST, BRAIN])
    assert validate_recurring_grant(proposal)["executing_principals"] == sorted(
        [ORCHESTRATION, INGEST, BRAIN]
    )
    distinct = proposed_grant(
        issuing_principal="Albert Meintjes",
        identities=IDENTITIES,
        resource_manifest_digest="b" * 64,
        source_policy_digest="c" * 64,
        valid_from=VALID_FROM,
    )
    assert distinct["executing_principals"] == sorted(
        [ORCHESTRATION, INGEST, BRAIN, PROOF, RELEASE]
    )
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_invalid$"):
        validate_recurring_grant(grant(executing_principals=[ORCHESTRATION, ORCHESTRATION]))


def test_proposed_grant_binds_manifest_identities_and_tree_allowances():
    proposal = proposed_grant(
        issuing_principal="Albert Meintjes",
        identities=IDENTITIES,
        resource_manifest_digest="b" * 64,
        source_policy_digest="c" * 64,
        valid_from=VALID_FROM,
    )
    checked = validate_recurring_grant(proposal)
    assert checked["executing_principals"] == sorted([ORCHESTRATION, INGEST, BRAIN, PROOF, RELEASE])
    assert checked["reserved_micro_usd_per_capture"] == 500000
    assert checked["run_allowance_micro_usd"] == 450000
    assert checked["cumulative_ceiling_micro_usd"] == 30 * 500000
    assert checked["monthly_allowance_micro_usd"] == 30 * 500000
    assert checked["permitted_cutoffs"] == CUTOFFS
    assert checked["valid_until"] == "2026-10-15T00:00:00+00:00"
    assert checked["permitted_image_digests"] == []
    assert (
        checked["approved_release_manifest_digest"]
        == sha256(canonical_bytes(proposed_release_manifest())).hexdigest()
    )
    assert proposed_release_manifest()["delegation"] == "none"
    with pytest.raises(GrantRefusal, match=r"^recurring_grant_image_forbidden$"):
        check(proposal, now=NOW)
    assert grant_digest(proposal) == grant_digest(
        proposed_grant(
            issuing_principal="Albert Meintjes",
            identities=IDENTITIES,
            resource_manifest_digest="b" * 64,
            source_policy_digest="c" * 64,
            valid_from=VALID_FROM,
        )
    )


def test_proposed_grant_floors_valid_from_to_the_approval_day():
    later = proposed_grant(
        issuing_principal="Albert Meintjes",
        identities=IDENTITIES,
        resource_manifest_digest="b" * 64,
        source_policy_digest="c" * 64,
        valid_from=datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
    )
    assert later["valid_from"] == "2026-09-15T00:00:00+00:00"
    assert later["valid_until"] == "2026-10-15T00:00:00+00:00"
    assert later["permitted_cutoffs"][0] == "2026-09-15"
    assert grant_digest(later) == grant_digest(
        proposed_grant(
            issuing_principal="Albert Meintjes",
            identities=IDENTITIES,
            resource_manifest_digest="b" * 64,
            source_policy_digest="c" * 64,
            valid_from=VALID_FROM,
        )
    )
    admitted = proposed_grant(
        issuing_principal="Albert Meintjes",
        identities=IDENTITIES,
        resource_manifest_digest="b" * 64,
        source_policy_digest="c" * 64,
        valid_from=datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
        permitted_image_digests=[IMAGE],
    )
    decision = check(
        admitted,
        now=datetime(2026, 9, 15, 8, 30, tzinfo=UTC),
        cutoff_utc=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert decision["cutoff"] == "2026-09-15"


def test_rendered_proposal_carries_the_canonical_digest_and_obeys_prose_rules():
    proposal = proposed_grant(
        issuing_principal="Albert Meintjes",
        identities=IDENTITIES,
        resource_manifest_digest="b" * 64,
        source_policy_digest="c" * 64,
        valid_from=VALID_FROM,
    )
    text = render_grant_proposal(proposal)
    assert grant_digest(proposal) in text
    assert "Approve recurring execution grant" in text
    assert "Revoke recurring execution grant recurring_execution_20260915_v1" in text
    for principal in (ORCHESTRATION, INGEST, BRAIN, PROOF, RELEASE):
        assert principal in text
    for operation in ALLOWED_OPERATIONS:
        assert operation in text
    assert "staging_source_profile.py" in text
    chain = text.index("grant_missing")
    for state in (
        "stage_adapter_unavailable",
        "operation_unbound",
        "recurring_grant_image_forbidden",
        "release_pending",
    ):
        assert text.index(state, chain) > chain
        chain = text.index(state, chain)
    assert "00:00:00 UTC on the approval day" in text
    assert "\u2014" not in text
    assert "\u2013" not in text
    assert "--" not in text
    assert "C:/Users" not in text
    assert "C:\\Users" not in text
    assert text.endswith("\n")


def test_the_registry_map_covers_exactly_the_allowed_operations_in_grant_order():
    assert tuple(V1_REGISTRY_OPERATIONS) == ALLOWED_OPERATIONS
    assert tuple(V1_REGISTRY_OPERATIONS.values()) == (
        "collection_exposure_issue",
        "source_snapshot_capture",
        "r3_apply",
        "r3_proof_issue",
        "r3_release",
    )
    assert len(set(V1_REGISTRY_OPERATIONS.values())) == len(ALLOWED_OPERATIONS)
    assert set(V1_REGISTRY_OPERATIONS.values()).isdisjoint(ALLOWED_OPERATIONS)
    assert set(V1_REGISTRY_OPERATIONS.values()).isdisjoint(EXCLUDED_OPERATIONS)
    assert {V1_REGISTRY_OPERATIONS[operation] for operation in STAGE_OPERATIONS.values()} == set(
        V1_REGISTRY_OPERATIONS.values()
    )
