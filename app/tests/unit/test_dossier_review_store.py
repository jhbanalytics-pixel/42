"""Create-only review decisions: exact bytes, one id per content, replayed state."""

from __future__ import annotations

import json

import pytest
from google.api_core.exceptions import PreconditionFailed

from src.api import dossier_review_store as store
from src.api import dossier_store
from tests.unit.object_creator_bucket import listed_names

PREFIX = "open-intelligence/v2/staging/"
INVESTIGATION_ID = "inv_fixture"
DOSSIER_VERSION = "b" * 64
CLAIM_VERSION = "c" * 64
ARTIFACT_VERSION = "d" * 64
PRINCIPAL = "human:5f2c9a1b"
RECORDED_AT = "2026-09-13T08:00:00.000000Z"


# A generation-aware bucket double
#
# The shape the dossier store tests use, plus a prefix listing, because the
# decision reader lists a directory. Test-only hooks let a test replace an
# object or make the writer and the listing fail, none of which the store can do.


class ReadBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        data, generation = bucket.objects[name]
        self.generation = generation
        self.size = len(data)

    def download_as_bytes(self, *, if_generation_match=None, **kwargs):
        current = self.bucket.objects.get(self.name)
        if current is None:
            raise PreconditionFailed("gone")
        data, generation = current
        if if_generation_match is not None and if_generation_match != generation:
            raise PreconditionFailed("generation moved")
        return data


class WriteBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.generation = None

    def upload_from_string(self, data, *, content_type, if_generation_match):
        self.bucket.uploads.append((self.name, if_generation_match))
        assert content_type == "application/json"
        current = self.bucket.objects.get(self.name)
        current_generation = current[1] if current is not None else 0
        if if_generation_match != current_generation:
            raise PreconditionFailed("precondition")
        if self.name in self.bucket.failing:
            raise RuntimeError("storage unavailable")
        self.bucket.counter += 1
        self.bucket.objects[self.name] = (data, self.bucket.counter)
        self.generation = self.bucket.counter


class Bucket:
    name = "listening-post-staging-cache"

    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.counter = 100
        self.failing = set()
        self.list_failing = False

    def blob(self, name):
        return WriteBlob(self, name)

    def get_blob(self, name):
        return ReadBlob(self, name) if name in self.objects else None

    def list_blobs(self, *, prefix, max_results=None, delimiter=None):
        if self.list_failing:
            raise RuntimeError("listing unavailable")
        # Listing order is by name, which is by decision id, never by time.
        return [
            ReadBlob(self, name)
            for name in listed_names(self.objects, prefix, max_results, delimiter)
        ]

    # Test hooks. The store never rewrites an object.
    def replace(self, name, data):
        self.counter += 1
        self.objects[name] = (data, self.counter)


# Fixture decisions


def claim_fields(**overrides):
    fields = dict(
        investigation_id=INVESTIGATION_ID,
        dossier_version=DOSSIER_VERSION,
        resource="claim",
        resource_id="clm_001",
        resource_version=CLAIM_VERSION,
        action="approve",
        expected_state="pending_review",
        idempotency_key="idem-001",
        support_review={
            "verdict": "supported",
            "receipt_ids": ["gqr_001"],
            "note": "checked",
        },
        principal_ref=PRINCIPAL,
        recorded_at=RECORDED_AT,
    )
    fields.update(overrides)
    return fields


def artifact_fields(**overrides):
    fields = claim_fields(
        resource="artifact",
        resource_id="art_001",
        resource_version=ARTIFACT_VERSION,
        action="submit",
        expected_state="draft",
        idempotency_key="idem-art",
        support_review=None,
    )
    fields.update(overrides)
    return fields


def claim_decision(**overrides):
    return store.build_decision(**claim_fields(**overrides))


def name_of(decision):
    return store.decision_object_name(
        PREFIX, INVESTIGATION_ID, DOSSIER_VERSION, decision["decision_id"]
    )


def stored(bucket, decision):
    bucket.replace(name_of(decision), store.canonical_bytes(decision))


# The record


def test_a_decision_id_is_the_canonical_digest_of_the_record_without_it():
    decision = claim_decision()
    rest = {key: value for key, value in decision.items() if key != "decision_id"}
    assert decision["decision_id"] == dossier_store.canonical_digest(rest)
    assert set(decision) == set(store.DECISION_FIELDS)
    assert decision["contract_version"] == "dossier_review_v1"
    assert decision["next_state"] == "approved"
    assert decision["state"] == "approved"
    assert decision["principal_ref"] == PRINCIPAL


def test_a_decision_id_is_deterministic_and_moves_with_the_content():
    assert claim_decision()["decision_id"] == claim_decision()["decision_id"]
    later = claim_decision(recorded_at="2026-09-13T08:00:01.000000Z")
    assert later["decision_id"] != claim_decision()["decision_id"]
    reworded = claim_decision(
        support_review={
            "verdict": "supported",
            "receipt_ids": ["gqr_001"],
            "note": "checked twice",
        }
    )
    assert reworded["decision_id"] != claim_decision()["decision_id"]


def test_the_object_name_sits_under_the_dossier_prefix_by_version_and_id():
    decision = claim_decision()
    assert name_of(decision) == (
        f"{PREFIX}decisions/{INVESTIGATION_ID}/{DOSSIER_VERSION}/"
        f"{decision['decision_id']}.json"
    )
    with pytest.raises(ValueError):
        store.decision_object_name(
            "other/prefix/", INVESTIGATION_ID, DOSSIER_VERSION, decision["decision_id"]
        )
    with pytest.raises(ValueError):
        store.decision_object_name(PREFIX, INVESTIGATION_ID, DOSSIER_VERSION, "short")
    with pytest.raises(ValueError):
        store.decision_object_name(PREFIX, "bad id", DOSSIER_VERSION, "0" * 64)


@pytest.mark.parametrize(
    "fields",
    [
        claim_fields(support_review=None),
        artifact_fields(
            support_review={"verdict": "supported", "receipt_ids": [], "note": ""}
        ),
        claim_fields(
            support_review={"verdict": "maybe", "receipt_ids": [], "note": ""}
        ),
        claim_fields(support_review={"verdict": "supported", "note": ""}),
        claim_fields(
            support_review={"verdict": "supported", "receipt_ids": "gqr", "note": ""}
        ),
        claim_fields(
            support_review={"verdict": "supported", "receipt_ids": [1], "note": ""}
        ),
        claim_fields(
            support_review={"verdict": "supported", "receipt_ids": [], "note": None}
        ),
        claim_fields(principal_ref="service:worker@example.test"),
        claim_fields(principal_ref="human:albert@example.test"),
        claim_fields(action="supersede"),
        claim_fields(expected_state="approved"),
        claim_fields(resource="dossier"),
        claim_fields(resource_id=""),
        claim_fields(resource_version="zz"),
        claim_fields(dossier_version="short"),
        claim_fields(investigation_id="fixture"),
        claim_fields(recorded_at="2026-09-13 08:00:00"),
        claim_fields(idempotency_key=""),
        claim_fields(idempotency_key=" padded"),
    ],
)
def test_a_decision_outside_the_grammar_cannot_be_built(fields):
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        store.build_decision(**fields)
    assert caught.value.code == "decision_record_invalid"


def test_a_tampered_record_is_refused_on_read():
    decision = claim_decision()
    forged_id = dict(decision, decision_id="0" * 64)
    with pytest.raises(dossier_store.DossierRecordInvalid):
        store.parse_decision(forged_id)
    moved_state = dict(decision, state="rejected")
    with pytest.raises(dossier_store.DossierRecordInvalid):
        store.parse_decision(moved_state)
    extra_field = dict(decision, email="someone@example.test")
    with pytest.raises(dossier_store.DossierRecordInvalid):
        store.parse_decision(extra_field)
    assert store.parse_decision(dict(decision)) == decision


def test_the_command_grammar_is_exact():
    command = {
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
            "note": "checked",
        },
    }
    assert store.validate_review_command(command) == command
    for broken in (
        dict(command, principal_ref=PRINCIPAL),
        dict(command, contract_version="intelligence_dossier_v1"),
        dict(command, resource="artifact"),
        dict(command, support_review=None),
        dict(command, dossier_version="short"),
        dict(command, action=""),
        {key: value for key, value in command.items() if key != "idempotency_key"},
        [command],
        None,
    ):
        with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
            store.validate_review_command(broken)
        assert caught.value.code == "review_request_invalid"


# Create only


def test_create_writes_once_and_an_identical_retry_returns_the_same_decision():
    bucket = Bucket()
    decision = claim_decision()
    first = store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    assert first["created"] is True
    assert first["decision"] == decision
    assert bucket.uploads == [(name_of(decision), 0)]
    assert bucket.objects[name_of(decision)][0] == store.canonical_bytes(decision)
    second = store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    assert second["created"] is False
    assert second["decision"] == decision
    assert second["generation"] == first["generation"]
    assert len(bucket.objects) == 1


def test_create_refuses_a_name_already_holding_different_bytes():
    bucket = Bucket()
    decision = claim_decision()
    other = json.dumps(dict(decision, note="x")).encode("utf-8")
    bucket.replace(name_of(decision), other)
    with pytest.raises(dossier_store.DossierConflict) as caught:
        store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    assert caught.value.code == "decision_conflict"
    assert bucket.objects[name_of(decision)][0] == other


def test_create_fails_closed_when_the_writer_is_unavailable():
    bucket = Bucket()
    decision = claim_decision()
    bucket.failing.add(name_of(decision))
    with pytest.raises(dossier_store.DossierStoreError) as caught:
        store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    assert caught.value.code == "dossier_store_unavailable"
    assert bucket.objects == {}


def test_create_refuses_a_record_it_cannot_parse():
    bucket = Bucket()
    decision = dict(claim_decision(), decision_id="0" * 64)
    with pytest.raises(dossier_store.DossierRecordInvalid):
        store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    assert bucket.uploads == []


# Replay


def read_state(bucket, resource="claim", resource_id="clm_001"):
    return store.resource_state(
        bucket=bucket,
        prefix=PREFIX,
        investigation_id=INVESTIGATION_ID,
        dossier_version=DOSSIER_VERSION,
        resource=resource,
        resource_id=resource_id,
    )


def test_an_unknown_resource_has_the_initial_state_from_the_transition_table():
    bucket = Bucket()
    claim = read_state(bucket)
    assert claim["state"] == "pending_review"
    assert claim["resource_version"] is None
    assert claim["applied"] == []
    assert read_state(bucket, "relationship", "rel_x")["state"] == "pending_review"
    assert read_state(bucket, "artifact", "art_001")["state"] == "draft"


def test_decisions_replay_in_recorded_order_not_in_listing_order():
    bucket = Bucket()
    approve = claim_decision(recorded_at="2026-09-13T08:00:05Z")
    reject = claim_decision(
        action="reject",
        idempotency_key="idem-002",
        recorded_at="2026-09-13T08:00:05.5Z",
    )
    for decision in (approve, reject):
        store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    listed = store.list_decisions(
        bucket=bucket,
        prefix=PREFIX,
        investigation_id=INVESTIGATION_ID,
        dossier_version=DOSSIER_VERSION,
    )
    # A plain string sort would put "05.5Z" before "05Z". Recorded order does not.
    assert [item["decision_id"] for item in listed] == [
        approve["decision_id"],
        reject["decision_id"],
    ]
    state = read_state(bucket)
    assert state["state"] == "approved"
    assert state["resource_version"] == CLAIM_VERSION
    assert state["applied"] == [approve["decision_id"]]


def test_a_decision_made_against_a_state_that_had_moved_is_not_applied():
    bucket = Bucket()
    reject = claim_decision(action="reject", recorded_at="2026-09-13T08:00:00Z")
    approve = claim_decision(recorded_at="2026-09-13T08:00:01Z")
    for decision in (approve, reject):
        store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    state = read_state(bucket)
    assert state["state"] == "rejected"
    assert state["applied"] == [reject["decision_id"]]


def test_each_resource_replays_on_its_own():
    bucket = Bucket()
    submit = store.build_decision(**artifact_fields())
    approve = store.build_decision(
        **artifact_fields(
            action="approve",
            expected_state="pending_review",
            idempotency_key="idem-art-2",
            recorded_at="2026-09-13T08:00:01Z",
        )
    )
    other_claim = claim_decision(resource_id="clm_002", idempotency_key="idem-003")
    for decision in (submit, approve, other_claim):
        store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    assert read_state(bucket, "artifact", "art_001")["state"] == "approved"
    assert read_state(bucket, "claim", "clm_002")["state"] == "approved"
    assert read_state(bucket, "claim", "clm_001")["state"] == "pending_review"


def test_replay_fails_closed_when_one_resource_carries_two_versions():
    bucket = Bucket()
    stored(bucket, claim_decision())
    stored(bucket, claim_decision(resource_version="e" * 64, idempotency_key="k2"))
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read_state(bucket)
    assert caught.value.code == "decision_resource_version_conflict"


def test_listing_refuses_an_object_whose_name_disagrees_with_its_content():
    bucket = Bucket()
    decision = claim_decision()
    wrong_name = store.decision_object_name(
        PREFIX, INVESTIGATION_ID, DOSSIER_VERSION, "0" * 64
    )
    bucket.replace(wrong_name, store.canonical_bytes(decision))
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read_state(bucket)
    assert caught.value.code == "decision_record_invalid"


def test_listing_refuses_a_decision_about_another_dossier_version():
    bucket = Bucket()
    foreign = claim_decision(dossier_version="f" * 64)
    bucket.replace(name_of(foreign), store.canonical_bytes(foreign))
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read_state(bucket)
    assert caught.value.code == "decision_record_invalid"


def test_listing_refuses_bytes_that_are_not_the_canonical_record():
    bucket = Bucket()
    decision = claim_decision()
    bucket.replace(name_of(decision), json.dumps(decision, indent=2).encode("utf-8"))
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read_state(bucket)
    assert caught.value.code == "decision_record_invalid"


def test_objects_that_are_not_decisions_are_left_alone():
    bucket = Bucket()
    directory = store.decision_directory(PREFIX, INVESTIGATION_ID, DOSSIER_VERSION)
    bucket.replace(directory + "current.json", b"{}")
    decision = claim_decision()
    store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    assert read_state(bucket)["state"] == "approved"


def test_a_listing_failure_is_storage_unavailable_not_an_empty_history():
    bucket = Bucket()
    bucket.list_failing = True
    with pytest.raises(dossier_store.DossierStoreError) as caught:
        read_state(bucket)
    assert caught.value.code == "dossier_store_unavailable"


def test_decisions_recorded_at_one_instant_replay_as_the_chain_they_form():
    bucket = Bucket()
    submit = store.build_decision(**artifact_fields())
    approve = store.build_decision(
        **artifact_fields(
            action="approve",
            expected_state="pending_review",
            idempotency_key="idem-art-2",
        )
    )
    assert submit["recorded_at"] == approve["recorded_at"]
    for decision in (approve, submit):
        store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    state = read_state(bucket, "artifact", "art_001")
    assert state["state"] == "approved"
    assert state["applied"] == [submit["decision_id"], approve["decision_id"]]


def test_an_earlier_stale_decision_is_not_revived_by_a_later_one():
    bucket = Bucket()
    early_approve = store.build_decision(
        **artifact_fields(
            action="approve",
            expected_state="pending_review",
            idempotency_key="idem-art-2",
            recorded_at="2026-09-13T08:00:00Z",
        )
    )
    late_submit = store.build_decision(
        **artifact_fields(recorded_at="2026-09-13T08:00:01Z")
    )
    for decision in (early_approve, late_submit):
        store.create_decision(bucket=bucket, prefix=PREFIX, decision=decision)
    state = read_state(bucket, "artifact", "art_001")
    assert state["state"] == "pending_review"
    assert state["applied"] == [late_submit["decision_id"]]


def test_a_review_command_cannot_carry_a_purpose_override_field():
    from src.api import client_purpose

    for field in ("purpose_override", "client_purpose_override", "override_reason"):
        command = {
            "contract_version": store.REVIEW_CONTRACT_VERSION,
            **artifact_fields(),
            field: True,
        }
        with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
            store.validate_review_command(command)
        assert caught.value.code == "review_request_invalid"
    prohibited = {
        "policy_version": client_purpose.POLICY_VERSION,
        "state": "prohibited",
        "purpose_codes": ["electoral_purpose"],
        "evidence": [],
        "review_override": "refused",
    }
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        store.refuse_client_purpose_override(
            prohibited, role="dossier_approver", action="approve"
        )
    assert caught.value.code == client_purpose.REVIEW_OVERRIDE_REFUSED
