"""The dossier approval state machine, and its refusal to run.

Approval needs a server-authenticated principal and an immutable create-only
record. The passcode cannot provide either. Until identity validation, role
mapping and durable storage are separately approved and proved, every approval
action refuses with one code and every control is disabled with the same
reason.

The state machine is built and tested anyway, because the refusal has to be a
refusal to perform a known transition, not an absence of one. A system that
cannot say what it would have done is not withholding an action, it simply
lacks it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from google.api_core.exceptions import PreconditionFailed

from src.api import dossier_approval as approval
from src.api import dossier_review_store, dossier_store, investigations, workspace_scope
from tests.unit.object_creator_bucket import listed_names


def test_the_four_roles_are_exactly_the_contract_roles():
    assert approval.APPROVAL_ROLES == frozenset(
        {
            "dossier_editor",
            "claim_approver",
            "relationship_approver",
            "client_read_approver",
        }
    )


@pytest.mark.parametrize(
    ("resource", "state", "action", "role", "next_state"),
    [
        ("claim", "pending_review", "approve", "claim_approver", "approved"),
        ("claim", "pending_review", "reject", "claim_approver", "rejected"),
        (
            "relationship",
            "pending_review",
            "approve",
            "relationship_approver",
            "approved",
        ),
        (
            "relationship",
            "pending_review",
            "reject",
            "relationship_approver",
            "rejected",
        ),
        ("artifact", "draft", "submit", "dossier_editor", "pending_review"),
        ("artifact", "pending_review", "approve", "client_read_approver", "approved"),
        ("artifact", "pending_review", "reject", "client_read_approver", "rejected"),
    ],
)
def test_every_contract_transition_resolves_exactly(
    resource, state, action, role, next_state
):
    plan = approval.plan_transition(resource, state, action)
    assert plan.required_role == role
    assert plan.next_state == next_state


@pytest.mark.parametrize(
    ("resource", "state", "action"),
    [
        ("claim", "approved", "approve"),
        ("claim", "rejected", "approve"),
        ("claim", "superseded", "approve"),
        ("artifact", "approved", "reject"),
        ("relationship", "draft", "submit"),
        ("claim", "pending_review", "submit"),
        ("dossier", "pending_review", "approve"),
    ],
)
def test_a_transition_the_contract_does_not_define_is_refused(resource, state, action):
    """No transition reverses in place, and nothing outside the table exists."""
    with pytest.raises(approval.ApprovalTransitionUnknown):
        approval.plan_transition(resource, state, action)


def test_supersession_is_server_only_and_not_requestable():
    """Superseded is an event the server records, never an action a role takes."""
    with pytest.raises(approval.ApprovalTransitionUnknown):
        approval.plan_transition("claim", "approved", "supersede")


def test_approving_an_artifact_approves_nothing_else():
    plan = approval.plan_transition("artifact", "pending_review", "approve")
    assert plan.resource == "artifact"
    assert plan.approves_by_implication == ()


def test_approving_a_claim_approves_no_relationship():
    plan = approval.plan_transition("claim", "pending_review", "approve")
    assert plan.approves_by_implication == ()


# --- The refusal --------------------------------------------------------------


def test_authority_is_unavailable_today():
    state = approval.authority_state()
    assert state.available is False
    assert state.code == "approval_authority_unavailable"
    assert state.reason


@pytest.mark.parametrize(
    ("resource", "state", "action"),
    [
        ("claim", "pending_review", "approve"),
        ("relationship", "pending_review", "reject"),
        ("artifact", "draft", "submit"),
        ("artifact", "pending_review", "approve"),
    ],
)
def test_every_approval_action_refuses_with_one_code(resource, state, action):
    refusal = approval.attempt_transition(
        resource, state, action, principal_ref="anything"
    )
    assert refusal.status == 409
    assert refusal.code == "approval_authority_unavailable"


def test_the_refusal_names_a_known_transition_rather_than_denying_it_exists():
    """A refusal to act is not the same as having no action to refuse."""
    refusal = approval.attempt_transition(
        "claim", "pending_review", "approve", principal_ref="anything"
    )
    assert refusal.required_role == "claim_approver"
    assert refusal.would_have_reached == "approved"


def test_an_undefined_transition_is_still_refused_as_undefined():
    """Authority being unavailable must not turn an impossible action into a
    merely deferred one."""
    with pytest.raises(approval.ApprovalTransitionUnknown):
        approval.attempt_transition(
            "claim", "approved", "approve", principal_ref="anything"
        )


def test_controls_are_disabled_for_the_same_reason_the_action_refuses():
    control = approval.control_state("claim", "pending_review", "approve")
    assert control.enabled is False
    assert control.code == approval.authority_state().code


@pytest.mark.parametrize(
    "principal",
    [
        "service:trends-engine-canary@ogilvy-trends-v2.iam.gserviceaccount.com",
        "model:gemini-3.5-flash",
        "passcode",
        "browser_field",
        "legacy_author:someone",
        "",
        None,
    ],
)
def test_no_service_model_passcode_or_stored_author_can_hold_a_role(principal):
    with pytest.raises(approval.ApprovalPrincipalInvalid):
        approval.validate_principal(principal)


def test_a_server_authenticated_human_reference_is_the_only_shape_accepted():
    assert approval.validate_principal("human:5f2c9a1b") == "human:5f2c9a1b"


def test_a_principal_reference_carries_no_raw_identity():
    """Raw email, token or identity-provider claims never enter these payloads."""
    with pytest.raises(approval.ApprovalPrincipalInvalid):
        approval.validate_principal("human:albert.meintjes@ogilvy.co.za")


# Recording a review


def test_the_initial_state_of_each_resource_is_read_off_the_table():
    assert approval.initial_state("claim") == "pending_review"
    assert approval.initial_state("relationship") == "pending_review"
    assert approval.initial_state("artifact") == "draft"
    with pytest.raises(approval.ApprovalTransitionUnknown):
        approval.initial_state("dossier")


PREFIX = dossier_store.STAGING_PREFIX
INVESTIGATION_ID = "inv_fixture"
DOSSIER_VERSION = "b" * 64
CLAIM_VERSION = "c" * 64
ARTIFACT_VERSION = "d" * 64
SCOPE_DIGEST = "a" * 64
NOW = datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc)
ADMITTED = approval.AuthorityState(
    available=True,
    code="approval_authority_admitted",
    reason="admitted for this test only",
)


class Blob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.generation = bucket.objects[name][1] if name in bucket.objects else None

    def download_as_bytes(self, *, if_generation_match=None, **kwargs):
        data, generation = self.bucket.objects[self.name]
        if if_generation_match is not None and if_generation_match != generation:
            raise PreconditionFailed("generation moved")
        return data

    def upload_from_string(self, data, *, content_type, if_generation_match):
        current = self.bucket.objects.get(self.name)
        if if_generation_match != (current[1] if current is not None else 0):
            raise PreconditionFailed("precondition")
        if self.bucket.write_failing:
            raise RuntimeError("storage unavailable")
        self.bucket.counter += 1
        self.bucket.objects[self.name] = (data, self.bucket.counter)
        self.generation = self.bucket.counter
        self.bucket.writes.append(self.name)


class Bucket:
    name = "listening-post-staging-cache"

    def __init__(self):
        self.objects = {}
        self.writes = []
        self.counter = 100
        self.write_failing = False
        self.read_failing = False
        self.list_failing = False

    def blob(self, name):
        return Blob(self, name)

    def get_blob(self, name):
        if self.read_failing:
            raise RuntimeError("storage unavailable")
        return Blob(self, name) if name in self.objects else None

    def list_blobs(self, *, prefix, max_results=None, delimiter=None):
        if self.list_failing:
            raise RuntimeError("listing unavailable")
        return [
            Blob(self, name)
            for name in listed_names(self.objects, prefix, max_results, delimiter)
        ]

    def put(self, name, data):
        self.counter += 1
        self.objects[name] = (data, self.counter)


def frame():
    return investigations.InvestigationFrame(
        client_scope_id="ogilvy_default",
        market_scope=("za", "ng", "ke"),
        brand_config_id=None,
        audience_lens_ids=(),
        theme_id=None,
        run_id="investigation_run_001",
        contract_version="2.1.0",
        decision_question="Which emerging behaviour should the brand act on in six weeks?",
        time_horizon_days=42,
        brand_context=None,
        known_assumptions=(),
        change_my_mind_if=(),
        research_role_id=None,
        research_role_version=None,
        output_mode="internal_working_paper",
    )


def scope(investigation_id=INVESTIGATION_ID, scope_digest=SCOPE_DIGEST):
    return workspace_scope.ResolvedWorkspaceScope(
        investigation_id=investigation_id,
        frame=frame(),
        response={},
        scope_digest=scope_digest,
    )


def pointer(**overrides):
    value = {
        "contract_version": "dossier_pointer_v1",
        "investigation_id": INVESTIGATION_ID,
        "dossier_version": DOSSIER_VERSION,
        "object_name": f"{PREFIX}dossiers/{INVESTIGATION_ID}/{DOSSIER_VERSION}.json",
        "body_sha256": DOSSIER_VERSION,
        "generation": "101",
        "scope_digest": SCOPE_DIGEST,
        "predecessor_version": None,
        "published_at": "2026-09-13T07:00:00.000000Z",
    }
    value.update(overrides)
    return value


def workspace(current=None):
    bucket = Bucket()
    if current is not False:
        bucket.put(
            dossier_store.pointer_entry_name(PREFIX, INVESTIGATION_ID, 1),
            dossier_store._canonical(
                {
                    "contract_version": "dossier_pointer_entry_v1",
                    "sequence": 1,
                    "predecessor_sequence": None,
                    "predecessor_sha256": None,
                    "pointer": pointer(**(current or {})),
                }
            ),
        )
    return bucket


def command(**overrides):
    value = {
        "contract_version": "dossier_review_v1",
        "dossier_version": DOSSIER_VERSION,
        "resource": "claim",
        "resource_id": "clm_001",
        "resource_version": CLAIM_VERSION,
        "action": "approve",
        "expected_state": "pending_review",
        "idempotency_key": "idem-001",
        "support_review": {
            "verdict": "supported",
            "receipt_ids": ["gqr_001"],
            "note": "checked against the receipts",
        },
    }
    value.update(overrides)
    return value


def artifact_command(**overrides):
    value = command(
        resource="artifact",
        resource_id="art_001",
        resource_version=ARTIFACT_VERSION,
        action="submit",
        expected_state="draft",
        idempotency_key="idem-art-1",
        support_review=None,
    )
    value.update(overrides)
    return value


def principal(role="claim_approver", principal_ref="human:5f2c9a1b"):
    return {"principal_ref": principal_ref, "role": role}


def record(bucket, **overrides):
    settings = dict(
        bucket=bucket,
        scope=scope(),
        principal=principal(),
        command=command(),
        authority=ADMITTED,
        now=NOW,
    )
    settings.update(overrides)
    return approval.record_review(**settings)


def refusal(bucket, **overrides):
    with pytest.raises(approval.ReviewRefused) as caught:
        record(bucket, **overrides)
    return caught.value.refusal


def stored_state(bucket, resource="claim", resource_id="clm_001"):
    return dossier_review_store.resource_state(
        bucket=bucket,
        prefix=PREFIX,
        investigation_id=INVESTIGATION_ID,
        dossier_version=DOSSIER_VERSION,
        resource=resource,
        resource_id=resource_id,
    )


def test_an_admitted_reviewer_records_one_decision_bound_to_the_server_principal():
    bucket = workspace()
    result = record(bucket)
    assert set(result) == {
        "contract_version",
        "investigation_id",
        "dossier_version",
        "decision_id",
        "state",
    }
    assert result["contract_version"] == "dossier_review_v1"
    assert result["investigation_id"] == INVESTIGATION_ID
    assert result["dossier_version"] == DOSSIER_VERSION
    assert result["state"] == "approved"
    name = dossier_review_store.decision_object_name(
        PREFIX, INVESTIGATION_ID, DOSSIER_VERSION, result["decision_id"]
    )
    assert bucket.writes == [
        name,
        dossier_review_store.state_pointer_entry_name(
            PREFIX, INVESTIGATION_ID, DOSSIER_VERSION, 1
        ),
    ]
    stored = dossier_review_store.parse_decision(
        dossier_store._load(bucket.objects[name][0])
    )
    assert stored["principal_ref"] == "human:5f2c9a1b"
    assert stored["recorded_at"] == "2026-09-13T08:00:00.000000Z"
    assert stored["resource_version"] == CLAIM_VERSION
    assert stored["support_review"] == command()["support_review"]
    assert stored_state(bucket)["state"] == "approved"


def test_a_same_key_same_command_retry_returns_the_existing_decision():
    bucket = workspace()
    first = record(bucket)
    later = datetime(2026, 9, 13, 9, 0, 0, tzinfo=timezone.utc)
    again = record(bucket, now=later)
    assert again == first
    assert len(bucket.writes) == 2


def test_a_same_key_different_command_is_a_conflict():
    bucket = workspace()
    record(bucket)
    refused = refusal(bucket, command=command(action="reject"))
    assert (refused.status, refused.code) == (409, "review_version_conflict")
    assert len(bucket.writes) == 2


def test_a_decision_against_a_state_that_has_moved_is_a_conflict():
    bucket = workspace()
    record(bucket)
    refused = refusal(bucket, command=command(idempotency_key="idem-002"))
    assert (refused.status, refused.code) == (409, "review_version_conflict")
    assert refused.required_role == "claim_approver"
    assert refused.would_have_reached == "approved"
    assert len(bucket.writes) == 2


def test_a_decision_on_a_version_that_is_not_current_is_a_conflict():
    bucket = workspace()
    refused = refusal(bucket, command=command(dossier_version="e" * 64))
    assert (refused.status, refused.code) == (409, "review_version_conflict")
    assert bucket.writes == []
    unpublished = workspace(current=False)
    refused = refusal(unpublished)
    assert (refused.status, refused.code) == (409, "review_version_conflict")
    assert unpublished.writes == []


def test_a_resource_version_that_disagrees_with_the_record_is_a_conflict():
    bucket = workspace()
    submitted = record(
        bucket, principal=principal("dossier_editor"), command=artifact_command()
    )
    assert submitted["state"] == "pending_review"
    approve = artifact_command(
        action="approve", expected_state="pending_review", idempotency_key="idem-art-2"
    )
    refused = refusal(
        bucket,
        principal=principal("client_read_approver"),
        command=dict(approve, resource_version="e" * 64),
    )
    assert (refused.status, refused.code) == (409, "review_version_conflict")
    approved = record(
        bucket, principal=principal("client_read_approver"), command=approve
    )
    assert approved["state"] == "approved"


def test_a_pointer_for_another_scope_is_an_invalid_scope_that_reveals_nothing():
    bucket = workspace(current={"scope_digest": "9" * 64})
    refused = refusal(bucket)
    assert (refused.status, refused.code) == (404, "scope_invalid")
    assert bucket.writes == []
    assert "9" * 64 not in refused.reason
    refused = refusal(workspace(), scope=scope(investigation_id="fixture"))
    assert (refused.status, refused.code) == (404, "scope_invalid")


@pytest.mark.parametrize(
    "broken",
    [
        dict(command(), principal_ref="human:5f2c9a1b"),
        dict(command(), contract_version="intelligence_dossier_v1"),
        dict(command(), support_review=None),
        dict(command(), action="supersede"),
        dict(command(), expected_state="approved"),
        dict(command(), resource="dossier"),
        dict(command(), resource_version="c" * 63),
        {key: value for key, value in command().items() if key != "idempotency_key"},
        "not a command",
    ],
)
def test_a_command_outside_the_package_shape_is_refused_before_storage(broken):
    bucket = workspace()
    bucket.read_failing = True
    refused = refusal(bucket, command=broken)
    assert (refused.status, refused.code) == (400, "review_request_invalid")
    assert bucket.writes == []


def test_a_principal_in_the_command_never_reaches_the_record():
    bucket = workspace()
    refused = refusal(bucket, command=dict(command(), principal_ref="human:deadbeef"))
    assert refused.code == "review_request_invalid"
    record(bucket)
    name = bucket.writes[0]
    assert b"deadbeef" not in bucket.objects[name][0]
    assert b"human:5f2c9a1b" in bucket.objects[name][0]


@pytest.mark.parametrize(
    "holder",
    [
        principal("relationship_approver"),
        principal("dossier_editor"),
        principal("client_read_approver"),
        principal("superuser"),
        principal(principal_ref="service:worker@example.test"),
        principal(principal_ref="human:5f2c9a1b@example.test"),
        {"principal_ref": "human:5f2c9a1b"},
        "human:5f2c9a1b",
        None,
    ],
)
def test_a_principal_without_the_required_role_is_refused_before_storage(holder):
    bucket = workspace()
    refused = refusal(bucket, principal=holder)
    assert (refused.status, refused.code) == (403, "review_role_required")
    assert bucket.writes == []


def test_the_refusal_for_a_wrong_role_names_the_role_it_would_have_needed():
    refused = refusal(workspace(), principal=principal("relationship_approver"))
    assert refused.required_role == "claim_approver"
    assert refused.would_have_reached == "approved"


def test_without_admitted_authority_nothing_is_recorded():
    bucket = workspace()
    with pytest.raises(approval.ReviewRefused) as caught:
        approval.record_review(
            bucket=bucket, scope=scope(), principal=principal(), command=command()
        )
    refused = caught.value.refusal
    assert (refused.status, refused.code) == (503, "review_authority_unavailable")
    assert refused.required_role == "claim_approver"
    assert bucket.writes == []
    withheld = approval.AuthorityState(available=False, code="withheld", reason="no")
    refused = refusal(bucket, authority=withheld)
    assert (refused.status, refused.code) == (503, "review_authority_unavailable")
    assert bucket.writes == []


def test_the_control_and_the_action_read_the_same_authority():
    control = approval.control_state(
        "claim", "pending_review", "approve", authority=ADMITTED
    )
    assert control.enabled is True
    assert control.code == ADMITTED.code
    assert approval.control_state("claim", "pending_review", "approve").enabled is False


def test_a_writer_failure_is_storage_unavailable_and_leaves_nothing_behind():
    bucket = workspace()
    bucket.write_failing = True
    refused = refusal(bucket)
    assert (refused.status, refused.code) == (503, "review_storage_unavailable")
    assert bucket.writes == []
    listing = workspace()
    listing.list_failing = True
    refused = refusal(listing)
    assert (refused.status, refused.code) == (503, "review_storage_unavailable")
    reading = workspace()
    reading.read_failing = True
    refused = refusal(reading)
    assert (refused.status, refused.code) == (503, "review_storage_unavailable")


def test_approving_an_artifact_records_nothing_about_its_claims():
    bucket = workspace()
    record(bucket, principal=principal("dossier_editor"), command=artifact_command())
    approved = record(
        bucket,
        principal=principal("client_read_approver"),
        command=artifact_command(
            action="approve",
            expected_state="pending_review",
            idempotency_key="idem-art-2",
        ),
    )
    assert approved["state"] == "approved"
    assert stored_state(bucket, "artifact", "art_001")["state"] == "approved"
    assert stored_state(bucket, "claim", "clm_001")["state"] == "pending_review"
    assert stored_state(bucket, "relationship", "rel_001")["state"] == "pending_review"
    assert len(bucket.writes) == 4
