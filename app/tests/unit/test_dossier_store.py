"""Create-only dossier storage: exact bytes, exact generation, exact identity."""

from __future__ import annotations

import hashlib
import json
from functools import partial

import pytest
from google.api_core.exceptions import PreconditionFailed

from src.api import dossier_store, investigations
from tests.unit.object_creator_bucket import listed_names

PREFIX = "open-intelligence/v2/staging/"


# A generation-aware bucket double
#
# Copied from the shape the investigation API tests use, then extended: every
# object carries a generation, a create with if_generation_match=0 refuses an
# existing object, and a pinned download refuses a generation that has moved.
# Test-only hooks let a test move a generation, drop an object or make an
# upload acknowledge ambiguously, none of which the store itself can do.


class ReadBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        data, generation = bucket.objects[name]
        self.generation = generation
        self.size = len(data)

    def download_as_bytes(self, *, if_generation_match=None, **kwargs):
        self.bucket.downloads.append((self.name, if_generation_match))
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
        if self.name not in self.bucket.ambiguous:
            self.generation = self.bucket.counter


class Bucket:
    name = "listening-post-staging-cache"

    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.downloads = []
        self.counter = 100
        self.failing = set()
        self.ambiguous = set()

    def blob(self, name):
        return WriteBlob(self, name)

    def get_blob(self, name):
        return ReadBlob(self, name) if name in self.objects else None

    # The pointer reader lists its log, as the real bucket allows.
    def list_blobs(self, *, prefix, max_results=None, delimiter=None):
        return [
            ReadBlob(self, name)
            for name in listed_names(self.objects, prefix, max_results, delimiter)
        ]

    # Test hooks. The store never moves, drops or rewrites an object.
    def move_generation(self, name):
        data, _ = self.objects[name]
        self.counter += 1
        self.objects[name] = (data, self.counter)

    def drop(self, name):
        del self.objects[name]

    def replace(self, name, data):
        self.counter += 1
        self.objects[name] = (data, self.counter)


# Fixture record


def _hex(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


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


def scope_payload():
    value = investigations.investigation_frame_payload(frame())
    return {
        key: value[key]
        for key in (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "run_id",
            "contract_version",
        )
    }


def scope_digest():
    canonical = json.dumps(
        scope_payload(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def bindings():
    return {
        "policy_digest": _hex("policy"),
        "deployment_digest": _hex("deployment"),
        "source_binding_digest": _hex("source_binding"),
        "snapshot_digest": _hex("snapshot"),
        "result_digest": _hex("result"),
        "prompt_manifest_digest": _hex("prompt"),
        "publisher_build_digest": _hex("build"),
    }


def record(**overrides):
    value = {
        "contract_version": "investigation_dossier_record_v1",
        "investigation_id": investigations.investigation_id_for_frame(frame()),
        "frame": investigations.investigation_frame_payload(frame()),
        "scope": scope_payload(),
        "temporal_binding": {
            "window_start": "2026-08-01",
            "window_end": "2026-08-28",
            "as_of": "2026-08-29T06:30:00Z",
            "source_cutoff": "2026-08-28",
        },
        "request_reference": {
            "request_id": "5c2f6f2e-4a5d-4c2f-9b6e-1c0f2a3b4c5d",
            "request_digest": _hex("request"),
            "thread_anchor_request_id": None,
            "parent_request_id": None,
        },
        "admitted_answer": {"contract_version": "general_question_result_record_v1"},
        "evidence_snapshot": {"contract_version": "general_question_snapshot_v1"},
        "bindings": bindings(),
        "review_state": {
            "selected_claim_ids": [],
            "claim_decision_refs": [],
            "relationship_decision_refs": [],
            "unresolved_questions": [],
        },
        "predecessor_version": None,
        "created_at": "2026-09-12T00:00:00Z",
    }
    value.update(overrides)
    return value


def body_of(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def publication(**overrides):
    value = {
        "source_binding_digest": _hex("source_binding"),
        "publisher_ref": "engine:dossier-producer",
        "publisher_build_digest": _hex("build"),
        "created_at": "2026-09-12T00:00:01Z",
        "authority_ref": "authority:" + _hex("authority")[:16],
    }
    value.update(overrides)
    return value


def create(bucket, body=None, pointer_writer=None, **overrides):
    return dossier_store.create_dossier(
        bucket=bucket,
        prefix=PREFIX,
        body=body if body is not None else body_of(record()),
        publication=publication(**overrides),
        pointer_writer=pointer_writer,
    )


def read(bucket, version, **overrides):
    values = {
        "bucket": bucket,
        "prefix": PREFIX,
        "investigation_id": record()["investigation_id"],
        "dossier_version": version,
        "scope_digest": scope_digest(),
    }
    values.update(overrides)
    return dossier_store.read_dossier_version(**values)


def names(version):
    identity = record()["investigation_id"]
    return (
        f"{PREFIX}dossiers/{identity}/{version}.json",
        f"{PREFIX}dossiers/{identity}/{version}.publication.json",
        dossier_store.pointer_entry_name(PREFIX, identity, 1),
    )


def entry_name(sequence):
    return dossier_store.pointer_entry_name(
        PREFIX, record()["investigation_id"], sequence
    )


def top_entry(bucket, directory):
    """The name of the highest entry in one append-only pointer log."""
    return max(name for name in bucket.objects if name.startswith(directory))


def top_payload(bucket, directory):
    return json.loads(bucket.objects[top_entry(bucket, directory)][0])["pointer"]


def rewrite_top_payload(bucket, directory, payload):
    """Test only: rewrite the pointer the top entry holds, keeping it canonical."""
    name = top_entry(bucket, directory)
    entry = json.loads(bucket.objects[name][0])
    entry["pointer"] = payload
    bucket.replace(name, dossier_store._canonical(entry))


# Creation


def test_create_writes_exact_bytes_create_only_and_publishes_after_readback():
    bucket = Bucket()
    body = body_of(record())
    version = hashlib.sha256(body).hexdigest()
    pointers = []

    result = create(bucket, body, pointer_writer=pointers.append)

    object_name, publication_name, _ = names(version)
    assert result["created"] is True
    assert bucket.objects[object_name][0] == body
    assert bucket.uploads[0] == (object_name, 0)
    assert bucket.uploads[1] == (publication_name, 0)
    publication_record = result["publication"]
    assert publication_record == {
        "contract_version": "dossier_publication_v1",
        "investigation_id": record()["investigation_id"],
        "dossier_version": version,
        "object_name": object_name,
        "body_sha256": version,
        "generation": str(bucket.objects[object_name][1]),
        "source_binding_digest": _hex("source_binding"),
        "publisher_ref": "engine:dossier-producer",
        "publisher_build_digest": _hex("build"),
        "created_at": "2026-09-12T00:00:01Z",
        "authority_ref": publication()["authority_ref"],
    }
    assert json.loads(bucket.objects[publication_name][0]) == publication_record
    # The pointer went out once, after a pinned readback of the exact bytes.
    assert (object_name, bucket.objects[object_name][1]) in bucket.downloads
    assert pointers == [
        {
            "contract_version": "dossier_pointer_v1",
            "investigation_id": record()["investigation_id"],
            "dossier_version": version,
            "object_name": object_name,
            "body_sha256": version,
            "generation": publication_record["generation"],
            "scope_digest": scope_digest(),
            "predecessor_version": None,
            "published_at": "2026-09-12T00:00:01Z",
        }
    ]
    assert result["pointer"] == pointers[0]


def test_dossier_version_is_the_sha256_of_the_stored_bytes_and_of_the_canonical_record():
    body = body_of(record())
    assert dossier_store.dossier_version(body) == hashlib.sha256(body).hexdigest()
    assert dossier_store.dossier_version_for_record(
        record()
    ) == dossier_store.dossier_version(body)


def test_equal_retry_is_idempotent_and_writes_nothing_new():
    bucket = Bucket()
    first = create(bucket)
    uploads = list(bucket.uploads)
    objects = dict(bucket.objects)

    again = create(bucket)

    assert again["created"] is False
    assert again["publication"] == first["publication"]
    assert bucket.objects == objects
    assert bucket.uploads[len(uploads) :] == [
        (names(first["publication"]["dossier_version"])[0], 0)
    ]


def test_different_bytes_under_the_same_name_fail_closed_and_preserve_objects():
    bucket = Bucket()
    body = body_of(record())
    version = hashlib.sha256(body).hexdigest()
    object_name, publication_name, _ = names(version)
    other = body_of(record(created_at="2026-09-12T00:00:02Z"))
    bucket.replace(object_name, other)
    bucket.objects[publication_name] = (b"{}", 5)

    with pytest.raises(dossier_store.DossierConflict) as caught:
        create(bucket, body)

    assert caught.value.code == "dossier_body_conflict"
    assert bucket.objects[object_name][0] == other
    assert bucket.objects[publication_name][0] == b"{}"


def test_retry_with_a_different_publication_identity_is_a_conflict():
    bucket = Bucket()
    create(bucket)
    objects = dict(bucket.objects)
    for change in (
        {"publisher_ref": "engine:someone-else"},
        {"authority_ref": "authority:other"},
    ):
        with pytest.raises(dossier_store.DossierConflict) as caught:
            create(bucket, **change)
        assert caught.value.code == "dossier_publication_conflict"
    assert bucket.objects == objects


def test_retry_against_a_body_whose_publication_is_missing_fails_closed():
    bucket = Bucket()
    result = create(bucket)
    _, publication_name, _ = names(result["publication"]["dossier_version"])
    bucket.drop(publication_name)
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        create(bucket)
    assert caught.value.code == "dossier_publication_missing"
    assert publication_name not in bucket.objects


def test_publication_must_name_the_build_and_source_binding_the_body_carries():
    bucket = Bucket()
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        create(bucket, publisher_build_digest=_hex("another build"))
    assert caught.value.code == "publisher_build_unknown"
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        create(bucket, source_binding_digest=_hex("another capture"))
    assert caught.value.code == "source_binding_mismatch"
    assert bucket.uploads == []


@pytest.mark.parametrize(
    "publication_value",
    [
        None,
        {},
        publication(publisher_ref=""),
        publication(created_at="2026-09-12"),
        publication(authority_ref=None),
        publication(publisher_build_digest="abc"),
        {**publication(), "generation": "7"},
    ],
    ids=[
        "none",
        "empty",
        "blank_publisher",
        "bad_time",
        "no_authority",
        "bad_build",
        "self_generation",
    ],
)
def test_a_missing_or_malformed_publication_writes_nothing(publication_value):
    bucket = Bucket()
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        dossier_store.create_dossier(
            bucket=bucket,
            prefix=PREFIX,
            body=body_of(record()),
            publication=publication_value,
        )
    assert caught.value.code == "dossier_publication_invalid"
    assert bucket.uploads == []


def test_an_ambiguous_acknowledgement_fails_closed_and_keeps_the_object():
    bucket = Bucket()
    body = body_of(record())
    object_name, publication_name, _ = names(hashlib.sha256(body).hexdigest())
    bucket.ambiguous.add(object_name)
    with pytest.raises(dossier_store.DossierStoreError) as caught:
        create(bucket, body)
    assert caught.value.code == "dossier_generation_ambiguous"
    assert bucket.objects[object_name][0] == body
    assert publication_name not in bucket.objects


def test_a_storage_failure_is_named_and_never_reported_as_created():
    bucket = Bucket()
    body = body_of(record())
    object_name, _, _ = names(hashlib.sha256(body).hexdigest())
    bucket.failing.add(object_name)
    with pytest.raises(dossier_store.DossierStoreError) as caught:
        create(bucket, body)
    assert caught.value.code == "dossier_store_unavailable"
    assert object_name not in bucket.objects


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("review_state"),
        lambda value: value.update(extra=1),
        lambda value: value.update(contract_version="investigation_dossier_record_v2"),
        lambda value: value.update(investigation_id="request-uuid"),
        lambda value: value["scope"].pop("run_id"),
        lambda value: value["bindings"].update(publisher_build_digest="x"),
        lambda value: value.update(frame={}),
    ],
    ids=[
        "missing",
        "extra",
        "version",
        "identity",
        "scope_shape",
        "build",
        "empty_frame",
    ],
)
def test_a_body_outside_the_record_grammar_is_refused_before_any_write(mutate):
    bucket = Bucket()
    value = record()
    mutate(value)
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        create(bucket, body_of(value))
    assert caught.value.code == "dossier_record_invalid"
    assert bucket.uploads == []


def test_non_canonical_or_duplicate_key_bytes_are_refused():
    bucket = Bucket()
    padded = json.dumps(record(), sort_keys=True, indent=1).encode("utf-8")
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        create(bucket, padded)
    assert caught.value.code == "dossier_body_not_canonical"
    duplicated = body_of(record())[:-1] + b',"created_at":"2026-09-12T00:00:00Z"}'
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        create(bucket, duplicated)
    assert caught.value.code == "dossier_record_invalid"
    with pytest.raises(dossier_store.DossierRecordInvalid):
        create(bucket, b"")
    with pytest.raises(dossier_store.DossierRecordInvalid):
        create(bucket, "not bytes")
    assert bucket.uploads == []


def test_a_frame_whose_identity_is_not_the_named_investigation_is_refused():
    bucket = Bucket()
    value = record()
    value["frame"]["decision_question"] = "A different question."
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        create(bucket, body_of(value))
    assert caught.value.code == "dossier_identity_mismatch"
    assert bucket.uploads == []


def test_a_pointer_failure_is_recoverable_from_the_stored_objects():
    bucket = Bucket()

    def failing_writer(pointer):
        raise RuntimeError("pointer store down")

    with pytest.raises(dossier_store.DossierPointerFailed) as caught:
        create(bucket, pointer_writer=failing_writer)
    assert caught.value.code == "dossier_pointer_failed"
    version = caught.value.publication["dossier_version"]
    object_name, publication_name, _ = names(version)
    assert object_name in bucket.objects
    assert publication_name in bucket.objects

    # The retry finds equal bytes and an equal publication, then publishes.
    recovered = create(
        bucket,
        pointer_writer=partial(
            dossier_store.publish_pointer, bucket=bucket, prefix=PREFIX
        ),
    )
    assert recovered["created"] is False
    assert (
        dossier_store.read_pointer(
            bucket=bucket, prefix=PREFIX, investigation_id=record()["investigation_id"]
        )["pointer"]["dossier_version"]
        == version
    )


# Readback


def test_read_returns_exact_bytes_generation_record_and_publication():
    bucket = Bucket()
    body = body_of(record())
    created = create(bucket, body)
    version = created["publication"]["dossier_version"]

    found = read(bucket, version)

    assert found["body"] == body
    assert found["record"] == record()
    assert found["generation"] == created["publication"]["generation"]
    assert found["publication"] == created["publication"]
    assert found["object_name"] == names(version)[0]
    assert found["dossier_version"] == version
    assert bucket.downloads[-2:] == [
        (names(version)[0], int(found["generation"])),
        (names(version)[1], bucket.objects[names(version)[1]][1]),
    ]


def test_an_absent_version_is_named_not_invented():
    bucket = Bucket()
    with pytest.raises(dossier_store.DossierNotFound) as caught:
        read(bucket, "0" * 64)
    assert caught.value.code == "dossier_version_absent"


def test_same_id_with_different_body_is_refused_on_read():
    bucket = Bucket()
    created = create(bucket)
    version = created["publication"]["dossier_version"]
    object_name, _, _ = names(version)
    bucket.replace(object_name, body_of(record(created_at="2026-09-12T00:00:02Z")))
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read(bucket, version)
    assert caught.value.code == "dossier_version_mismatch"


def test_a_moved_object_generation_is_refused_on_read():
    bucket = Bucket()
    created = create(bucket)
    version = created["publication"]["dossier_version"]
    bucket.move_generation(names(version)[0])
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read(bucket, version)
    assert caught.value.code == "dossier_generation_mismatch"


def test_a_deleted_publication_fails_closed_on_read():
    bucket = Bucket()
    created = create(bucket)
    version = created["publication"]["dossier_version"]
    bucket.drop(names(version)[1])
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read(bucket, version)
    assert caught.value.code == "dossier_publication_missing"


def test_a_publication_for_another_object_or_build_is_refused_on_read():
    bucket = Bucket()
    created = create(bucket)
    version = created["publication"]["dossier_version"]
    _, publication_name, _ = names(version)
    for change, code in (
        ({"object_name": names("1" * 64)[0]}, "dossier_publication_mismatch"),
        ({"body_sha256": "1" * 64}, "dossier_publication_mismatch"),
        ({"publisher_build_digest": _hex("unknown build")}, "publisher_build_unknown"),
        ({"source_binding_digest": _hex("other capture")}, "source_binding_mismatch"),
        ({"investigation_id": "inv_other"}, "dossier_publication_mismatch"),
    ):
        stored = dict(created["publication"], **change)
        bucket.replace(publication_name, json.dumps(stored).encode("utf-8"))
        with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
            read(bucket, version)
        assert caught.value.code == code


def test_a_wrong_investigation_or_scope_reveals_nothing():
    bucket = Bucket()
    created = create(bucket)
    version = created["publication"]["dossier_version"]
    with pytest.raises(dossier_store.DossierNotFound):
        read(bucket, version, investigation_id="inv_other")
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read(bucket, version, scope_digest="1" * 64)
    assert caught.value.code == "dossier_scope_mismatch"
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read(bucket, version, scope_digest="not a digest")
    assert caught.value.code == "scope_digest_invalid"


@pytest.mark.parametrize(
    "field",
    [
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
    ],
)
def test_each_scope_field_changes_the_digest_the_reader_must_match(field):
    changed = scope_payload()
    changed[field] = (
        ["ke"] if field in ("market_scope", "audience_lens_ids") else "other"
    )
    canonical = json.dumps(
        changed, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    other_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    bucket = Bucket()
    created = create(bucket)
    version = created["publication"]["dossier_version"]
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        read(bucket, version, scope_digest=other_digest)
    assert caught.value.code == "dossier_scope_mismatch"


@pytest.mark.parametrize(
    ("prefix", "identity", "version"),
    [
        ("open-intelligence/v2/production/", "inv_a", "0" * 64),
        ("", "inv_a", "0" * 64),
        (PREFIX, "request-uuid", "0" * 64),
        (PREFIX, "inv_a", "0" * 63),
        (PREFIX, "inv_a", "A" * 64),
        (PREFIX, "inv_../b", "0" * 64),
    ],
)
def test_object_names_are_exact(prefix, identity, version):
    with pytest.raises(ValueError):
        dossier_store.dossier_object_name(prefix, identity, version)
    assert dossier_store.dossier_object_name(PREFIX, "inv_a", "0" * 64) == (
        f"{PREFIX}dossiers/inv_a/{'0' * 64}.json"
    )


# The current pointer


def test_pointer_moves_only_by_expected_generation_and_verified_target():
    bucket = Bucket()
    writer = partial(dossier_store.publish_pointer, bucket=bucket, prefix=PREFIX)
    first = create(bucket, pointer_writer=writer)
    version = first["publication"]["dossier_version"]
    assert bucket.uploads[-1] == (entry_name(1), 0)

    # An edit names its predecessor exactly and moves the pointer by CAS.
    edited = body_of(
        record(predecessor_version=version, created_at="2026-09-13T00:00:00Z")
    )
    second = create(bucket, edited, pointer_writer=writer)
    assert bucket.uploads[-1] == (entry_name(2), 0)
    current = dossier_store.read_pointer(
        bucket=bucket, prefix=PREFIX, investigation_id=record()["investigation_id"]
    )
    assert (
        current["pointer"]["dossier_version"]
        == second["publication"]["dossier_version"]
    )
    assert current["pointer"]["predecessor_version"] == version

    # A version that does not name the current target cannot take the pointer.
    stray = body_of(record(created_at="2026-09-14T00:00:00Z"))
    with pytest.raises(dossier_store.DossierPointerFailed) as caught:
        create(bucket, stray, pointer_writer=writer)
    assert caught.value.code == "dossier_pointer_failed"
    assert caught.value.__cause__.code == "pointer_predecessor_mismatch"
    assert entry_name(3) not in bucket.objects
    assert json.loads(bucket.objects[entry_name(2)][0])["pointer"] == current["pointer"]

    # The old version stays readable exactly as published.
    assert read(bucket, version)["record"] == record()


def test_pointer_refuses_an_unverified_or_moved_target():
    bucket = Bucket()
    created = create(bucket)
    pointer = created["pointer"]
    log = dossier_store.pointer_directory(PREFIX, record()["investigation_id"])
    uploads = len(bucket.uploads)
    bucket.move_generation(pointer["object_name"])
    with pytest.raises(dossier_store.DossierPointerFailed) as caught:
        dossier_store.publish_pointer(pointer, bucket=bucket, prefix=PREFIX)
    assert caught.value.code == "pointer_target_unverified"
    with pytest.raises(dossier_store.DossierPointerFailed) as caught:
        dossier_store.publish_pointer(
            dict(pointer, scope_digest="1" * 64), bucket=bucket, prefix=PREFIX
        )
    assert caught.value.code == "pointer_target_unverified"
    # Neither refusal tried to append to the pointer log, and the log is empty.
    assert bucket.uploads[uploads:] == []
    assert [name for name in bucket.objects if name.startswith(log)] == []
    assert names(pointer["dossier_version"])[2] not in bucket.objects

    # With a log already holding an entry, a refused move adds none.
    bucket = Bucket()
    writer = partial(dossier_store.publish_pointer, bucket=bucket, prefix=PREFIX)
    first = create(bucket, pointer_writer=writer)
    edited = create(
        bucket,
        body_of(
            record(
                predecessor_version=first["publication"]["dossier_version"],
                created_at="2026-09-13T00:00:00Z",
            )
        ),
    )
    before = sorted(name for name in bucket.objects if name.startswith(log))
    assert before == [entry_name(1)]
    bucket.move_generation(edited["pointer"]["object_name"])
    with pytest.raises(dossier_store.DossierPointerFailed) as caught:
        dossier_store.publish_pointer(edited["pointer"], bucket=bucket, prefix=PREFIX)
    assert caught.value.code == "pointer_target_unverified"
    assert sorted(name for name in bucket.objects if name.startswith(log)) == before
    assert entry_name(2) not in bucket.objects


def test_pointer_read_is_generation_pinned_and_absent_is_none():
    bucket = Bucket()
    identity = record()["investigation_id"]
    assert (
        dossier_store.read_pointer(
            bucket=bucket, prefix=PREFIX, investigation_id=identity
        )
        is None
    )
    writer = partial(dossier_store.publish_pointer, bucket=bucket, prefix=PREFIX)
    create(bucket, pointer_writer=writer)
    found = dossier_store.read_pointer(
        bucket=bucket, prefix=PREFIX, investigation_id=identity
    )
    pointer_name = entry_name(1)
    assert found["generation"] == str(bucket.objects[pointer_name][1])
    assert bucket.downloads[-1] == (pointer_name, bucket.objects[pointer_name][1])
    bucket.replace(pointer_name, b"{}")
    with pytest.raises(dossier_store.DossierRecordInvalid) as caught:
        dossier_store.read_pointer(
            bucket=bucket, prefix=PREFIX, investigation_id=identity
        )
    assert caught.value.code == "dossier_pointer_invalid"
