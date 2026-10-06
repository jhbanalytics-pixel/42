"""Both pointers under the staging grants: create and read, never replace.

The serving identity holds objectCreator and objectViewer on the cache bucket.
A pointer that moves by replacing one object fails there with 403 the second
time it moves. These tests hold the dossier pointer and the review state
pointer to a bucket that refuses every replace, under both answers a taken
name can give to a zero generation create.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import PreconditionFailed

from src.api import (
    dossier_review_store,
    dossier_store,
    main,
    workspace_scope,
)
from tests.unit import dossier_pdf_fakes
from tests.unit import test_dossier_store as store_fixture
from tests.unit import test_main_dossier_review_routes as routes
from tests.unit.object_creator_bucket import ObjectCreatorBucket
from tests.unit.test_dossier_resolver import dossier, resolved_scope
from tests.unit.test_main_dossier_review_routes import review_environment  # noqa: F401

PREFIX = store_fixture.PREFIX
IDENTITY = store_fixture.record()["investigation_id"]
TAKEN = ("precondition", "forbidden")


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def writer(bucket):
    return lambda pointer: dossier_store.publish_pointer(
        pointer, bucket=bucket, prefix=PREFIX
    )


def publish(bucket, *, predecessor=None, created_at="2026-09-12T00:00:00Z"):
    body = store_fixture.body_of(
        store_fixture.record(predecessor_version=predecessor, created_at=created_at)
    )
    return store_fixture.create(bucket, body, pointer_writer=writer(bucket))


def current(bucket):
    return dossier_store.read_pointer(
        bucket=bucket, prefix=PREFIX, investigation_id=IDENTITY
    )


def entry_name(sequence):
    return dossier_store.pointer_entry_name(PREFIX, IDENTITY, sequence)


# The dossier pointer


@pytest.mark.parametrize("taken", TAKEN)
def test_the_dossier_pointer_moves_twice_without_replacing_an_object(taken):
    bucket = ObjectCreatorBucket(taken)
    first = publish(bucket)
    one = first["publication"]["dossier_version"]
    second = publish(bucket, predecessor=one, created_at="2026-09-13T00:00:00Z")
    two = second["publication"]["dossier_version"]
    third = publish(bucket, predecessor=two, created_at="2026-09-14T00:00:00Z")

    found = current(bucket)
    assert found["pointer"] == third["pointer"]
    assert found["pointer"]["predecessor_version"] == two
    assert bucket.overwrites == []
    # Every write was a create of a free name.
    assert {precondition for _, precondition in bucket.uploads} == {0}


@pytest.mark.parametrize("taken", TAKEN)
def test_an_identical_publish_moves_nothing(taken):
    bucket = ObjectCreatorBucket(taken)
    first = publish(bucket)
    uploads = len(bucket.uploads)
    again = dossier_store.publish_pointer(first["pointer"], bucket=bucket, prefix=PREFIX)
    assert again["moved"] is False
    assert again["pointer"] == first["pointer"]
    assert again["generation"] == current(bucket)["generation"]
    assert len(bucket.uploads) == uploads


@pytest.mark.parametrize("taken", TAKEN)
def test_a_version_that_does_not_name_the_current_one_cannot_take_the_pointer(taken):
    bucket = ObjectCreatorBucket(taken)
    first = publish(bucket)
    stray = store_fixture.body_of(
        store_fixture.record(created_at="2026-09-14T00:00:00Z")
    )
    with pytest.raises(dossier_store.DossierPointerFailed) as caught:
        store_fixture.create(bucket, stray, pointer_writer=writer(bucket))
    assert caught.value.__cause__.code == "pointer_predecessor_mismatch"
    assert current(bucket)["pointer"] == first["pointer"]


@pytest.mark.parametrize("taken", TAKEN)
def test_two_publishers_racing_from_one_version_get_one_move_and_one_conflict(taken):
    bucket = ObjectCreatorBucket(taken)
    one = publish(bucket)["publication"]["dossier_version"]
    theirs = store_fixture.create(
        bucket,
        store_fixture.body_of(
            store_fixture.record(predecessor_version=one, created_at="2026-09-13T00:00:00Z")
        ),
    )
    mine = store_fixture.create(
        bucket,
        store_fixture.body_of(
            store_fixture.record(predecessor_version=one, created_at="2026-09-13T00:00:01Z")
        ),
    )
    # The other publisher lands the next entry after this one has read the
    # current pointer and before its own create reaches the bucket.
    bucket.before_upload.append(
        (
            lambda name: name == entry_name(2),
            lambda: dossier_store.publish_pointer(
                theirs["pointer"], bucket=bucket, prefix=PREFIX
            ),
        )
    )
    with pytest.raises(dossier_store.DossierPointerFailed) as caught:
        dossier_store.publish_pointer(mine["pointer"], bucket=bucket, prefix=PREFIX)
    assert caught.value.code == "pointer_generation_conflict"
    assert current(bucket)["pointer"] == theirs["pointer"]
    assert entry_name(3) not in bucket.objects


def test_a_cold_read_picks_the_latest_entry_and_its_generation():
    bucket = ObjectCreatorBucket()
    assert current(bucket) is None
    version = None
    published = []
    for day in range(12, 16):
        created = publish(
            bucket, predecessor=version, created_at=f"2026-09-{day}T00:00:00Z"
        )
        version = created["publication"]["dossier_version"]
        published.append(created["pointer"])
    found = current(bucket)
    assert found["pointer"] == published[-1]
    assert found["generation"] == str(bucket.objects[entry_name(4)][1])
    entry = json.loads(bucket.objects[entry_name(4)][0])
    assert entry["sequence"] == 4
    assert entry["predecessor_sequence"] == 3
    assert entry["predecessor_sha256"] == hashlib.sha256(
        bucket.objects[entry_name(3)][0]
    ).hexdigest()
    assert entry["pointer"] == published[-1]
    # The first entry names no predecessor.
    first = json.loads(bucket.objects[entry_name(1)][0])
    assert (first["predecessor_sequence"], first["predecessor_sha256"]) == (None, None)


def _refused_on_read(bucket):
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        current(bucket)
    assert caught.value.code == "dossier_pointer_invalid"


def test_a_fork_is_refused_as_corrupt():
    bucket = ObjectCreatorBucket()
    one = publish(bucket)["publication"]["dossier_version"]
    two = publish(bucket, predecessor=one, created_at="2026-09-13T00:00:00Z")
    entry = json.loads(bucket.objects[entry_name(2)][0])
    forked = dict(entry, sequence=3, predecessor_sequence=2, predecessor_sha256="0" * 64)
    bucket.plant(entry_name(3), canonical(forked))
    _refused_on_read(bucket)
    # A writer refuses to build on it as well.
    with pytest.raises(dossier_store.DossierStoreError):
        dossier_store.publish_pointer(two["pointer"], bucket=bucket, prefix=PREFIX)


def test_a_gap_is_refused_as_corrupt():
    bucket = ObjectCreatorBucket()
    publish(bucket)
    entry = json.loads(bucket.objects[entry_name(1)][0])
    skipped = dict(
        entry,
        sequence=3,
        predecessor_sequence=2,
        predecessor_sha256=hashlib.sha256(bucket.objects[entry_name(1)][0]).hexdigest(),
    )
    bucket.plant(entry_name(3), canonical(skipped))
    _refused_on_read(bucket)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: entry.update(sequence=2),
        lambda entry: entry.update(predecessor_sequence=0),
        lambda entry: entry.update(contract_version="dossier_pointer_v1"),
        lambda entry: entry["pointer"].update(investigation_id="inv_other"),
        lambda entry: entry.update(extra=True),
    ],
)
def test_an_entry_outside_its_grammar_is_refused(mutate):
    bucket = ObjectCreatorBucket()
    publish(bucket)
    entry = json.loads(bucket.objects[entry_name(1)][0])
    mutate(entry)
    del bucket.objects[entry_name(1)]
    bucket.plant(entry_name(1), canonical(entry))
    _refused_on_read(bucket)


def test_a_retired_single_object_pointer_is_refused_not_misread():
    bucket = ObjectCreatorBucket()
    created = store_fixture.create(bucket)
    legacy = f"{PREFIX}dossiers/{IDENTITY}/current.json"
    assert dossier_store.pointer_object_name(PREFIX, IDENTITY) == legacy
    bucket.plant(legacy, canonical(created["pointer"]))
    _refused_on_read(bucket)
    # Alongside new entries it is refused all the same.
    bucket = ObjectCreatorBucket()
    publish(bucket)
    bucket.plant(legacy, canonical(created["pointer"]))
    _refused_on_read(bucket)


def test_a_foreign_name_under_the_pointer_prefix_is_refused():
    bucket = ObjectCreatorBucket()
    publish(bucket)
    bucket.plant(entry_name(1)[: -len(".json")] + ".tmp", b"{}")
    _refused_on_read(bucket)


def test_the_history_bound_refuses_rather_than_reading_without_limit(monkeypatch):
    bucket = ObjectCreatorBucket()
    monkeypatch.setattr(dossier_store, "POINTER_HISTORY_LIMIT", 3)
    version = None
    for day in range(12, 15):
        version = publish(
            bucket, predecessor=version, created_at=f"2026-09-{day}T00:00:00Z"
        )["publication"]["dossier_version"]
    with pytest.raises(dossier_store.DossierPointerFailed) as caught:
        publish(bucket, predecessor=version, created_at="2026-09-15T00:00:00Z")
    assert caught.value.__cause__.code == "dossier_pointer_history_exhausted"
    assert entry_name(4) not in bucket.objects
    monkeypatch.setattr(dossier_store, "POINTER_HISTORY_LIMIT", 2)
    with pytest.raises(dossier_store.DossierStoreError) as caught:
        current(bucket)
    assert caught.value.code == "dossier_pointer_history_exhausted"


# The review state pointer

VERSION = "b" * 64
REVIEW_ID = "inv_fixture"
SETTINGS = {"prefix": PREFIX, "investigation_id": REVIEW_ID, "dossier_version": VERSION}


def decision(resource_id, action="approve", key=None, **overrides):
    fields = dict(
        investigation_id=REVIEW_ID,
        dossier_version=VERSION,
        resource="claim",
        resource_id=resource_id,
        resource_version="c" * 64,
        action=action,
        expected_state="pending_review",
        idempotency_key=key or f"idem-{resource_id}-{action}",
        support_review={"verdict": "supported", "receipt_ids": [], "note": ""},
        principal_ref="human:5f2c9a1b",
        recorded_at="2026-09-13T08:00:00.000000Z",
    )
    fields.update(overrides)
    return dossier_review_store.build_decision(**fields)


def record_and_advance(bucket, value, current=None):
    dossier_review_store.create_decision(bucket=bucket, prefix=PREFIX, decision=value)
    return dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=value, current=current
    )


def state(bucket):
    return dossier_review_store.read_state_pointer(bucket=bucket, **SETTINGS)


def state_entry(sequence):
    return dossier_review_store.state_pointer_entry_name(
        PREFIX, REVIEW_ID, VERSION, sequence
    )


@pytest.mark.parametrize("taken", TAKEN)
def test_approve_and_reject_commit_one_after_another(taken):
    bucket = ObjectCreatorBucket(taken)
    assert state(bucket)["generation"] == 0
    approved = record_and_advance(bucket, decision("clm_1"))
    rejected = record_and_advance(bucket, decision("clm_2", "reject"))
    third = record_and_advance(bucket, decision("clm_3"))
    assert (approved["applied"], rejected["applied"], third["applied"]) == (
        True,
        True,
        True,
    )
    found = state(bucket)
    assert found["generation"] == 3
    assert found["pointer"] == third["pointer"]
    assert {key: entry["state"] for key, entry in found["pointer"]["resources"].items()} == {
        "claim:clm_1": "approved",
        "claim:clm_2": "rejected",
        "claim:clm_3": "approved",
    }
    assert bucket.overwrites == []
    # The decisions listing still sees only decisions.
    listed = dossier_review_store.list_decisions(bucket=bucket, **SETTINGS)
    assert len(listed) == 3


@pytest.mark.parametrize("taken", TAKEN)
def test_an_identical_retry_moves_nothing_even_from_a_stale_read(taken):
    bucket = ObjectCreatorBucket(taken)
    before = state(bucket)
    value = decision("clm_1")
    first = record_and_advance(bucket, value, current=before)
    uploads = len(bucket.uploads)
    again = dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=value
    )
    assert again["applied"] is False
    assert again["pointer"] == first["pointer"]
    assert again["generation"] == first["generation"] == 1
    stale = dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=value, current=before
    )
    assert stale["applied"] is False
    assert stale["pointer"] == first["pointer"]
    # The stale retry tried one create and wrote nothing.
    assert [name for name, _ in bucket.uploads[uploads:]] == [state_entry(1)]
    assert state_entry(2) not in bucket.objects


@pytest.mark.parametrize("taken", TAKEN)
def test_two_writers_on_one_read_get_one_commit_and_one_conflict(taken):
    bucket = ObjectCreatorBucket(taken)
    record_and_advance(bucket, decision("clm_0"))
    read = state(bucket)
    theirs = decision("clm_1", key="idem-theirs")
    mine = decision("clm_1", "reject", key="idem-mine")
    other = decision("clm_2", key="idem-other")
    for value in (theirs, mine, other):
        dossier_review_store.create_decision(bucket=bucket, prefix=PREFIX, decision=value)
    dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=theirs, current=read
    )
    # The same resource moved: the command is history, not state.
    with pytest.raises(dossier_store.DossierConflict) as caught:
        dossier_review_store.advance_state_pointer(
            bucket=bucket, prefix=PREFIX, decision=mine, current=read
        )
    assert caught.value.code == "state_pointer_conflict"
    # Another resource on the same stale read may retry.
    with pytest.raises(dossier_store.DossierConflict) as caught:
        dossier_review_store.advance_state_pointer(
            bucket=bucket, prefix=PREFIX, decision=other, current=read
        )
    assert caught.value.code == "state_pointer_generation_moved"
    retried = dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=other
    )
    assert retried["applied"] is True
    assert state(bucket)["generation"] == 3
    assert bucket.overwrites == []


def _state_refused(bucket):
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        state(bucket)
    assert caught.value.code == "state_pointer_invalid"


def test_a_state_fork_or_gap_is_refused_as_corrupt():
    bucket = ObjectCreatorBucket()
    record_and_advance(bucket, decision("clm_1"))
    record_and_advance(bucket, decision("clm_2"))
    entry = json.loads(bucket.objects[state_entry(2)][0])
    bucket.plant(
        state_entry(3),
        canonical(dict(entry, sequence=3, predecessor_sequence=2, predecessor_sha256="0" * 64)),
    )
    _state_refused(bucket)
    with pytest.raises(dossier_store.DossierRecordInvalid):
        record_and_advance(bucket, decision("clm_3"))

    bucket = ObjectCreatorBucket()
    record_and_advance(bucket, decision("clm_1"))
    entry = json.loads(bucket.objects[state_entry(1)][0])
    bucket.plant(
        state_entry(3),
        canonical(
            dict(
                entry,
                sequence=3,
                predecessor_sequence=2,
                predecessor_sha256=hashlib.sha256(
                    bucket.objects[state_entry(1)][0]
                ).hexdigest(),
            )
        ),
    )
    _state_refused(bucket)


def test_a_retired_single_object_state_pointer_is_refused_not_misread():
    bucket = ObjectCreatorBucket()
    legacy = dossier_review_store.state_pointer_object_name(PREFIX, REVIEW_ID, VERSION)
    assert legacy == f"{PREFIX}decisions/{REVIEW_ID}/{VERSION}/pointer.json"
    bucket.plant(
        legacy,
        canonical(
            {
                "contract_version": "dossier_review_state_v1",
                "investigation_id": REVIEW_ID,
                "dossier_version": VERSION,
                "resources": {},
            }
        ),
    )
    _state_refused(bucket)


def test_a_state_entry_about_another_version_is_refused():
    bucket = ObjectCreatorBucket()
    record_and_advance(bucket, decision("clm_1"))
    entry = json.loads(bucket.objects[state_entry(1)][0])
    entry["pointer"]["dossier_version"] = "9" * 64
    del bucket.objects[state_entry(1)]
    bucket.plant(state_entry(1), canonical(entry))
    _state_refused(bucket)


# The routes, end to end, on the staging grants


@pytest.fixture
def staging_workspace(monkeypatch):
    bucket = ObjectCreatorBucket()
    value = dossier()
    created = dossier_store.create_dossier(
        bucket=bucket,
        prefix=PREFIX,
        body=store_fixture.body_of(value),
        publication=store_fixture.publication(),
        pointer_writer=writer(bucket),
    )
    scope = resolved_scope(value)
    dossier_pdf_fakes.install(monkeypatch)
    monkeypatch.setattr(main, "_investigation_bucket", lambda: bucket)
    monkeypatch.setattr(
        workspace_scope,
        "resolve_workspace_scope",
        lambda investigation_id: (
            scope
            if investigation_id == scope.investigation_id
            else routes.scope_missing(investigation_id)
        ),
    )
    return SimpleNamespace(
        bucket=bucket,
        dossier=value,
        scope=scope,
        version=created["publication"]["dossier_version"],
        review=f"/api/internal/v2/investigations/{scope.investigation_id}/dossier/review",
    )


def test_review_routes_approve_and_reject_on_the_staging_grants(
    monkeypatch, staging_workspace
):
    ws = staging_workspace
    browser, ready = routes.reviewer(monkeypatch, routes.SUBJECTS["claims"])
    approved = browser.post(ws.review, json=routes.claim_command(ws), headers=ready)
    assert approved.status_code == 200, approved.text
    rejected = browser.post(
        ws.review,
        json=routes.claim_command(
            ws,
            "clm_2",
            action="reject",
            idempotency_key="idem-reject",
            support_review={"verdict": "unsupported", "receipt_ids": [], "note": ""},
        ),
        headers=ready,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "rejected"
    replay = browser.post(ws.review, json=routes.claim_command(ws), headers=ready)
    assert replay.status_code == 200
    assert replay.json() == approved.json()
    found = dossier_review_store.read_state_pointer(
        bucket=ws.bucket,
        prefix=PREFIX,
        investigation_id=ws.scope.investigation_id,
        dossier_version=ws.version,
    )
    assert {key: entry["state"] for key, entry in found["pointer"]["resources"].items()} == {
        "claim:clm_1": "approved",
        "claim:clm_2": "rejected",
    }
    assert ws.bucket.overwrites == []


# Create-once writes on the staging grants


def test_identical_create_retries_read_back_when_a_taken_name_answers_403():
    bucket = ObjectCreatorBucket("forbidden")
    first = store_fixture.create(bucket)
    again = store_fixture.create(bucket)
    assert (first["created"], again["created"]) == (True, False)
    assert again["publication"] == first["publication"]
    value = decision("clm_1")
    made = dossier_review_store.create_decision(bucket=bucket, prefix=PREFIX, decision=value)
    retried = dossier_review_store.create_decision(
        bucket=bucket, prefix=PREFIX, decision=value
    )
    assert (made["created"], retried["created"]) == (True, False)
    assert bucket.overwrites == []


def test_a_create_the_identity_may_not_make_is_unavailable_not_taken():
    bucket = ObjectCreatorBucket("forbidden")
    bucket.create_denied = True
    with pytest.raises(dossier_store.DossierStoreError) as caught:
        dossier_review_store.create_decision(
            bucket=bucket, prefix=PREFIX, decision=decision("clm_1")
        )
    assert caught.value.code == "dossier_store_unavailable"


# A client retry after a lost answer


@pytest.mark.parametrize("taken", TAKEN)
def test_a_dossier_pointer_append_whose_retry_finds_its_own_bytes_is_a_move(taken):
    bucket = ObjectCreatorBucket(taken)
    created = store_fixture.create(bucket)
    bucket.landed_then_refused.add(entry_name(1))
    moved = dossier_store.publish_pointer(created["pointer"], bucket=bucket, prefix=PREFIX)
    assert moved["moved"] is True
    assert moved["generation"] == str(bucket.objects[entry_name(1)][1])
    assert current(bucket)["pointer"] == created["pointer"]


@pytest.mark.parametrize("taken", TAKEN)
def test_a_state_append_whose_retry_finds_its_own_bytes_is_not_a_conflict(taken):
    bucket = ObjectCreatorBucket(taken)
    value = decision("clm_1")
    dossier_review_store.create_decision(bucket=bucket, prefix=PREFIX, decision=value)
    bucket.landed_then_refused.add(state_entry(1))
    applied = dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=value
    )
    # The reread finds the decision already committed: the real head, not a conflict.
    assert (applied["applied"], applied["generation"]) == (False, 1)
    assert applied["pointer"] == state(bucket)["pointer"]


@pytest.mark.parametrize("taken", TAKEN)
def test_a_stale_retry_of_a_committed_decision_reports_the_real_head(taken):
    bucket = ObjectCreatorBucket(taken)
    empty = state(bucket)
    first = decision("clm_1")
    record_and_advance(bucket, first, current=empty)
    record_and_advance(bucket, decision("clm_2"))
    head = state(bucket)
    assert head["generation"] == 2
    retried = dossier_review_store.advance_state_pointer(
        bucket=bucket, prefix=PREFIX, decision=first, current=empty
    )
    assert retried["applied"] is False
    assert retried["generation"] == 2
    assert retried["pointer"] == head["pointer"]
    assert set(retried["pointer"]["resources"]) == {"claim:clm_1", "claim:clm_2"}
    assert state_entry(3) not in bucket.objects


def test_different_bytes_at_the_number_are_still_a_conflict():
    bucket = ObjectCreatorBucket()
    created = store_fixture.create(bucket)
    other = dict(created["pointer"], published_at="2026-09-12T00:00:02Z")
    bucket.plant(
        entry_name(1),
        canonical(
            {
                "contract_version": "dossier_pointer_entry_v1",
                "sequence": 1,
                "predecessor_sequence": None,
                "predecessor_sha256": None,
                "pointer": other,
            }
        ),
    )
    # The entry at the number holds other bytes, so the create stays refused.
    with pytest.raises(PreconditionFailed):
        dossier_store.append_pointer_log(
            bucket,
            directory=dossier_store.pointer_directory(PREFIX, IDENTITY),
            contract="dossier_pointer_entry_v1",
            exhausted_code="dossier_pointer_history_exhausted",
            head=None,
            payload=created["pointer"],
        )


# Bounded listings


def test_the_pointer_listing_asks_for_one_more_than_the_bound():
    bucket = ObjectCreatorBucket()
    publish(bucket)
    bucket.listings.clear()
    current(bucket)
    assert bucket.listings == [
        {
            "prefix": dossier_store.pointer_directory(PREFIX, IDENTITY)[:-1],
            "max_results": dossier_store.POINTER_HISTORY_LIMIT + 1,
            "delimiter": None,
        }
    ]


def test_listing_decisions_does_not_walk_the_state_pointer_entries():
    bucket = ObjectCreatorBucket()
    for index in range(3):
        record_and_advance(bucket, decision(f"clm_{index}"))
    bucket.listings.clear()
    listed = dossier_review_store.list_decisions(bucket=bucket, **SETTINGS)
    assert len(listed) == 3
    directory = dossier_review_store.decision_directory(PREFIX, REVIEW_ID, VERSION)
    assert bucket.listings == [
        {"prefix": directory, "max_results": None, "delimiter": "/"}
    ]
    returned = bucket.list_blobs(prefix=directory, delimiter="/")
    assert all("/pointer/" not in blob.name for blob in returned)


@pytest.mark.parametrize("generation", [False, 0.0, "0"])
def test_a_held_read_whose_number_is_not_an_integer_is_refused(generation):
    bucket = ObjectCreatorBucket()
    value = decision("clm_1")
    dossier_review_store.create_decision(bucket=bucket, prefix=PREFIX, decision=value)
    held = dict(state(bucket), generation=generation)
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        dossier_review_store.advance_state_pointer(
            bucket=bucket, prefix=PREFIX, decision=value, current=held
        )
    assert caught.value.code == "state_pointer_invalid"
    assert not any("/pointer/" in name for name in bucket.objects)
