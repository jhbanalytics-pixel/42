"""Create-only durable storage for immutable investigation dossier versions.

A version is the SHA256 of the exact bytes the engine produced, and the object
name carries it, so the same version can only ever hold the same bytes. The
body is written once with a zero-generation precondition. Beside it sits a
publication record, written once as well, that says which object, which
generation, which publisher and which authority produced it. A body cannot
establish its own authority: the reader admits a version only when the body,
its publication and the object generation agree with each other and with the
identity and scope the caller was resolved into.

The current pointer is not one mutable object. The serving identity on staging
may create and read objects in the cache bucket but not replace them, so the
pointer is an append-only log: every move creates the next numbered entry
under a zero generation precondition, and the current pointer is the entry with
the highest number. Two writers racing for the same number get one create and
one precondition failure, which is the compare and swap the single object used
to give. A move goes only towards a target that has just been read back, and
only from the version the new one names as its predecessor.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping

from google.api_core.exceptions import Forbidden, PreconditionFailed

from . import investigation_store, investigations

STAGING_PREFIX = investigation_store.STAGING_PREFIX
DOSSIER_CONTRACT_VERSION = "investigation_dossier_record_v1"
PUBLICATION_CONTRACT_VERSION = "dossier_publication_v1"
POINTER_CONTRACT_VERSION = "dossier_pointer_v1"
POINTER_ENTRY_CONTRACT_VERSION = "dossier_pointer_entry_v1"
CREATE_RESULT_VERSION = "dossier_create_result_v1"

RECORD_FIELDS = (
    "contract_version",
    "investigation_id",
    "frame",
    "scope",
    "temporal_binding",
    "request_reference",
    "admitted_answer",
    "evidence_snapshot",
    "bindings",
    "review_state",
    "predecessor_version",
    "created_at",
)
SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
)
BINDING_FIELDS = (
    "policy_digest",
    "deployment_digest",
    "source_binding_digest",
    "snapshot_digest",
    "result_digest",
    "prompt_manifest_digest",
    "publisher_build_digest",
)
PUBLICATION_INPUT_FIELDS = (
    "source_binding_digest",
    "publisher_ref",
    "publisher_build_digest",
    "created_at",
    "authority_ref",
)
PUBLICATION_FIELDS = (
    "contract_version",
    "investigation_id",
    "dossier_version",
    "object_name",
    "body_sha256",
    "generation",
    *PUBLICATION_INPUT_FIELDS,
)
POINTER_FIELDS = (
    "contract_version",
    "investigation_id",
    "dossier_version",
    "object_name",
    "body_sha256",
    "generation",
    "scope_digest",
    "predecessor_version",
    "published_at",
)

_INVESTIGATION_ID = re.compile(r"^inv_[A-Za-z0-9_-]{1,128}$")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_GENERATION = re.compile(r"[1-9][0-9]{0,31}\Z")


class DossierStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DossierConflict(DossierStoreError):
    pass


class DossierRecordInvalid(DossierStoreError):
    pass


class DossierNotFound(DossierStoreError):
    pass


class DossierPointerFailed(DossierStoreError):
    def __init__(self, code: str, publication: dict | None = None) -> None:
        super().__init__(code)
        self.publication = publication


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def dossier_version(body: bytes) -> str:
    if type(body) is not bytes or not body:
        raise DossierRecordInvalid("dossier_body_invalid")
    return hashlib.sha256(body).hexdigest()


def dossier_version_for_record(record: Mapping[str, object]) -> str:
    return canonical_digest(record)


def scope_digest_for_record(record: Mapping[str, object]) -> str:
    """The seven scope values, plus the frame's lens envelope when it names one.

    This is the digest ScopeBinding.from_frame gives the same frame, so a
    general 42 record keeps the digest it always had and a lensed record's
    pointer can only match a scope resolved under that same lens.
    """
    scope = record["scope"]
    values = {field: scope[field] for field in SCOPE_FIELDS}
    frame = record.get("frame")
    if isinstance(frame, Mapping) and "client_lens" in frame:
        values["client_lens"] = frame["client_lens"]
    return canonical_digest(values)


def _digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _text(value: object) -> bool:
    return type(value) is str and bool(value) and value == value.strip()


def _validate_prefix(prefix: object) -> str:
    if prefix != STAGING_PREFIX:
        raise ValueError("prefix must be exact staging namespace")
    return prefix


def _validate_investigation_id(investigation_id: object) -> str:
    if (
        type(investigation_id) is not str
        or _INVESTIGATION_ID.fullmatch(investigation_id) is None
    ):
        raise ValueError("investigation id is invalid")
    return investigation_id


def _validate_version(version: object) -> str:
    if not _digest(version):
        raise ValueError("dossier version is invalid")
    return version


def dossier_object_name(
    prefix: object, investigation_id: object, version: object
) -> str:
    _validate_prefix(prefix)
    _validate_investigation_id(investigation_id)
    _validate_version(version)
    return f"{prefix}dossiers/{investigation_id}/{version}.json"


def publication_object_name(
    prefix: object, investigation_id: object, version: object
) -> str:
    return dossier_object_name(prefix, investigation_id, version)[: -len(".json")] + (
        ".publication.json"
    )


def pointer_object_name(prefix: object, investigation_id: object) -> str:
    """The retired single object pointer. Never written; a reader refuses it."""
    _validate_prefix(prefix)
    _validate_investigation_id(investigation_id)
    return f"{prefix}dossiers/{investigation_id}/current.json"


def pointer_directory(prefix: object, investigation_id: object) -> str:
    return pointer_object_name(prefix, investigation_id)[: -len(".json")] + "/"


def pointer_entry_name(prefix: object, investigation_id: object, sequence: object) -> str:
    return pointer_directory(prefix, investigation_id) + _entry_tail(sequence)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _load(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise DossierRecordInvalid("dossier_record_invalid") from error
    if type(value) is not dict:
        raise DossierRecordInvalid("dossier_record_invalid")
    return value


def parse_dossier_body(body: object) -> dict:
    """The top-level record grammar, asserted on every byte sequence that enters or leaves.

    The engine validates the inner material when it builds the bytes. This side
    checks what the store itself relies on: the twelve fields, the contract, the
    identity the frame actually hashes to, the seven scope fields and the seven
    binding digests. A body that is not the canonical serialization of its own
    record is refused, because then two byte sequences could share a version.
    """
    if type(body) is not bytes or not body:
        raise DossierRecordInvalid("dossier_body_invalid")
    record = _load(body)
    if tuple(sorted(record)) != tuple(sorted(RECORD_FIELDS)):
        raise DossierRecordInvalid("dossier_record_invalid")
    if record["contract_version"] != DOSSIER_CONTRACT_VERSION:
        raise DossierRecordInvalid("dossier_record_invalid")
    scope = record["scope"]
    bindings = record["bindings"]
    if (
        type(record["investigation_id"]) is not str
        or _INVESTIGATION_ID.fullmatch(record["investigation_id"]) is None
        or type(scope) is not dict
        or set(scope) != set(SCOPE_FIELDS)
        or type(bindings) is not dict
        or set(bindings) != set(BINDING_FIELDS)
        or not all(_digest(bindings[field]) for field in BINDING_FIELDS)
        or type(record["review_state"]) is not dict
        or type(record["review_state"].get("selected_claim_ids")) is not list
        or type(record["temporal_binding"]) is not dict
        or any(
            type(record[field]) is not dict or not record[field]
            for field in ("frame", "admitted_answer", "evidence_snapshot")
        )
        or (
            record["predecessor_version"] is not None
            and not _digest(record["predecessor_version"])
        )
        or type(record["created_at"]) is not str
        or _TIMESTAMP.fullmatch(record["created_at"]) is None
    ):
        raise DossierRecordInvalid("dossier_record_invalid")
    try:
        frame = investigations.investigation_frame_from_payload(record["frame"])
    except ValueError as error:
        raise DossierRecordInvalid("dossier_record_invalid") from error
    if investigations.investigation_id_for_frame(frame) != record["investigation_id"]:
        raise DossierRecordInvalid("dossier_identity_mismatch")
    if _canonical(record) != body:
        raise DossierRecordInvalid("dossier_body_not_canonical")
    return record


def _validate_publication_input(publication: object) -> dict:
    if type(publication) is not dict or set(publication) != set(
        PUBLICATION_INPUT_FIELDS
    ):
        raise DossierRecordInvalid("dossier_publication_invalid")
    if (
        not _digest(publication["source_binding_digest"])
        or not _digest(publication["publisher_build_digest"])
        or not _text(publication["publisher_ref"])
        or not _text(publication["authority_ref"])
        or type(publication["created_at"]) is not str
        or _TIMESTAMP.fullmatch(publication["created_at"]) is None
    ):
        raise DossierRecordInvalid("dossier_publication_invalid")
    return {field: publication[field] for field in PUBLICATION_INPUT_FIELDS}


def _validate_publication(publication: object) -> dict:
    if type(publication) is not dict or set(publication) != set(PUBLICATION_FIELDS):
        raise DossierRecordInvalid("dossier_publication_invalid")
    if publication["contract_version"] != PUBLICATION_CONTRACT_VERSION:
        raise DossierRecordInvalid("dossier_publication_invalid")
    _validate_publication_input(
        {field: publication[field] for field in PUBLICATION_INPUT_FIELDS}
    )
    if (
        not _text(publication["investigation_id"])
        or not _digest(publication["dossier_version"])
        or not _digest(publication["body_sha256"])
        or not _text(publication["object_name"])
        or type(publication["generation"]) is not str
        or _GENERATION.fullmatch(publication["generation"]) is None
    ):
        raise DossierRecordInvalid("dossier_publication_invalid")
    return publication


def _generation(blob: object) -> int:
    value = getattr(blob, "generation", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        text = str(value) if value is not None else ""
        if _GENERATION.fullmatch(text) is None:
            raise DossierStoreError("dossier_generation_ambiguous")
        value = int(text)
    return value


def _upload(
    bucket: object,
    name: str,
    data: bytes,
    *,
    if_generation_match: int,
    content_type: str = "application/json",
) -> int:
    try:
        blob = bucket.blob(name)
        blob.upload_from_string(
            data,
            content_type=content_type,
            if_generation_match=if_generation_match,
        )
    except PreconditionFailed:
        raise
    except Forbidden as error:
        # Without storage.objects.delete a create to a taken name may be
        # refused for permission rather than for its precondition. When the
        # name is taken that is the same fact as the precondition; when it is
        # free the identity cannot write at all.
        if if_generation_match == 0 and _read_object(bucket, name) is not None:
            raise PreconditionFailed("object exists") from error
        raise DossierStoreError("dossier_store_unavailable") from error
    except Exception as error:
        raise DossierStoreError("dossier_store_unavailable") from error
    return _generation(blob)


def _read_object(bucket: object, name: str) -> tuple[bytes, int] | None:
    """One object with its generation, downloaded pinned to that generation."""
    try:
        blob = bucket.get_blob(name)
    except Exception as error:
        raise DossierStoreError("dossier_store_unavailable") from error
    if blob is None:
        return None
    generation = _generation(blob)
    try:
        raw = blob.download_as_bytes(if_generation_match=generation)
    except Exception as error:
        raise DossierStoreError("dossier_store_unavailable") from error
    if type(raw) is not bytes:
        raise DossierStoreError("dossier_store_unavailable")
    return raw, generation


def create_once(
    *, bucket: object, name: str, body: bytes, conflict_code: str, ambiguous_code: str
) -> dict:
    """Store one object once. An identical retry reads back what is stored.

    The upload carries a zero generation precondition. When that fails the
    object is reread: identical bytes are the same object, differing bytes are
    a conflict named conflict_code, and an object that cannot be read back is
    ambiguous_code. Nothing is overwritten or deleted on any path.
    """
    created = True
    try:
        generation = _upload(bucket, name, body, if_generation_match=0)
    except PreconditionFailed:
        created = False
        existing = _read_object(bucket, name)
        if existing is None:
            raise DossierStoreError(ambiguous_code) from None
        existing_bytes, generation = existing
        if existing_bytes != body:
            raise DossierConflict(conflict_code) from None
    return {"created": created, "generation": str(generation)}


def _read_publication(bucket: object, name: str) -> dict | None:
    found = _read_object(bucket, name)
    if found is None:
        return None
    raw, _generation_value = found
    return _validate_publication(_load(raw))


def _publication_identity(publication: Mapping[str, object]) -> dict:
    # The creation time is retained, not compared: a retry of the same
    # publication is the same publication whenever it happens to be retried.
    return {key: value for key, value in publication.items() if key != "created_at"}


def _pointer(record: Mapping[str, object], publication: Mapping[str, object]) -> dict:
    return {
        "contract_version": POINTER_CONTRACT_VERSION,
        "investigation_id": record["investigation_id"],
        "dossier_version": publication["dossier_version"],
        "object_name": publication["object_name"],
        "body_sha256": publication["body_sha256"],
        "generation": publication["generation"],
        "scope_digest": scope_digest_for_record(record),
        "predecessor_version": record["predecessor_version"],
        "published_at": publication["created_at"],
    }


def create_dossier(
    *,
    bucket: object,
    prefix: str,
    body: bytes,
    publication: dict,
    pointer_writer: Callable[[dict], object] | None = None,
) -> dict:
    """Store one immutable version, publish it, read it back, then point at it.

    Nothing here overwrites or deletes. A conflict rereads the stored bytes and
    the stored publication and compares both exactly: an equal retry returns
    what is stored, anything else fails and leaves every object in place.
    """
    record = parse_dossier_body(body)
    intent = _validate_publication_input(publication)
    bindings = record["bindings"]
    if intent["publisher_build_digest"] != bindings["publisher_build_digest"]:
        raise DossierRecordInvalid("publisher_build_unknown")
    if intent["source_binding_digest"] != bindings["source_binding_digest"]:
        raise DossierRecordInvalid("source_binding_mismatch")
    investigation_id = record["investigation_id"]
    version = dossier_version(body)
    object_name = dossier_object_name(prefix, investigation_id, version)
    publication_name = publication_object_name(prefix, investigation_id, version)
    created = True
    try:
        generation = _upload(bucket, object_name, body, if_generation_match=0)
    except PreconditionFailed:
        created = False
        existing = _read_object(bucket, object_name)
        if existing is None:
            raise DossierStoreError("dossier_create_ambiguous") from None
        existing_bytes, generation = existing
        if existing_bytes != body:
            raise DossierConflict("dossier_body_conflict") from None
    publication_record = {
        "contract_version": PUBLICATION_CONTRACT_VERSION,
        "investigation_id": investigation_id,
        "dossier_version": version,
        "object_name": object_name,
        "body_sha256": version,
        "generation": str(generation),
        **intent,
    }
    stored = _read_publication(bucket, publication_name)
    if stored is None:
        if not created:
            raise DossierRecordInvalid("dossier_publication_missing")
        try:
            _upload(
                bucket,
                publication_name,
                _canonical(publication_record),
                if_generation_match=0,
            )
        except PreconditionFailed:
            stored = _read_publication(bucket, publication_name)
            if stored is None:
                raise DossierStoreError("dossier_create_ambiguous") from None
    if stored is not None:
        if _publication_identity(stored) != _publication_identity(publication_record):
            raise DossierConflict("dossier_publication_conflict")
        publication_record = stored
    readback = read_dossier_version(
        bucket=bucket,
        prefix=prefix,
        investigation_id=investigation_id,
        dossier_version=version,
        scope_digest=scope_digest_for_record(record),
    )
    if readback["body"] != body or readback["generation"] != str(generation):
        raise DossierStoreError("dossier_readback_failed")
    pointer = _pointer(record, publication_record)
    if pointer_writer is not None:
        try:
            pointer_writer(dict(pointer))
        except Exception as error:
            raise DossierPointerFailed(
                "dossier_pointer_failed", publication_record
            ) from error
    return {
        "contract_version": CREATE_RESULT_VERSION,
        "created": created,
        "publication": publication_record,
        "pointer": pointer,
        "pointer_published": pointer_writer is not None,
    }


def read_dossier_version(
    *,
    bucket: object,
    prefix: str,
    investigation_id: str,
    dossier_version: str,
    scope_digest: str,
) -> dict:
    """One exact version, admitted only when bytes, publication and generation agree."""
    object_name = dossier_object_name(prefix, investigation_id, dossier_version)
    if not _digest(scope_digest):
        raise DossierRecordInvalid("scope_digest_invalid")
    found = _read_object(bucket, object_name)
    if found is None:
        raise DossierNotFound("dossier_version_absent")
    raw, generation = found
    if hashlib.sha256(raw).hexdigest() != dossier_version:
        raise DossierRecordInvalid("dossier_version_mismatch")
    record = parse_dossier_body(raw)
    if record["investigation_id"] != investigation_id:
        raise DossierRecordInvalid("dossier_investigation_mismatch")
    if scope_digest_for_record(record) != scope_digest:
        raise DossierRecordInvalid("dossier_scope_mismatch")
    publication = _read_publication(
        bucket, publication_object_name(prefix, investigation_id, dossier_version)
    )
    if publication is None:
        raise DossierRecordInvalid("dossier_publication_missing")
    if (
        publication["object_name"] != object_name
        or publication["body_sha256"] != dossier_version
        or publication["dossier_version"] != dossier_version
        or publication["investigation_id"] != investigation_id
    ):
        raise DossierRecordInvalid("dossier_publication_mismatch")
    if publication["generation"] != str(generation):
        raise DossierRecordInvalid("dossier_generation_mismatch")
    bindings = record["bindings"]
    if publication["publisher_build_digest"] != bindings["publisher_build_digest"]:
        raise DossierRecordInvalid("publisher_build_unknown")
    if publication["source_binding_digest"] != bindings["source_binding_digest"]:
        raise DossierRecordInvalid("source_binding_mismatch")
    return {
        "investigation_id": investigation_id,
        "dossier_version": dossier_version,
        "object_name": object_name,
        "generation": str(generation),
        "scope_digest": scope_digest,
        "body": raw,
        "record": record,
        "publication": publication,
    }


def _validate_pointer(pointer: object) -> dict:
    if type(pointer) is not dict or set(pointer) != set(POINTER_FIELDS):
        raise DossierRecordInvalid("dossier_pointer_invalid")
    if (
        pointer["contract_version"] != POINTER_CONTRACT_VERSION
        or type(pointer["investigation_id"]) is not str
        or _INVESTIGATION_ID.fullmatch(pointer["investigation_id"]) is None
        or not _digest(pointer["dossier_version"])
        or not _digest(pointer["body_sha256"])
        or not _digest(pointer["scope_digest"])
        or not _text(pointer["object_name"])
        or type(pointer["generation"]) is not str
        or _GENERATION.fullmatch(pointer["generation"]) is None
        or (
            pointer["predecessor_version"] is not None
            and not _digest(pointer["predecessor_version"])
        )
        or type(pointer["published_at"]) is not str
        or _TIMESTAMP.fullmatch(pointer["published_at"]) is None
    ):
        raise DossierRecordInvalid("dossier_pointer_invalid")
    return pointer


# The append-only pointer log
#
# Both pointers the review flow moves, this dossier pointer and the review state
# pointer beside the decisions, are kept the same way. Entry n lives at
# <directory>/<n as twenty digits>.json, is created once with a zero generation
# precondition and never touched again. It names entry n - 1 by number and by
# the SHA256 of its exact bytes, so a gap in the numbers or an entry that does
# not follow from its predecessor is refused as corrupt instead of read as the
# current state. A reader lists the names under the directory (the retired
# single object name shares the listing prefix, so one listing finds it too),
# reads the top entry and its predecessor, and checks the link between them.
# Earlier links were checked by the writer that appended the next entry, and
# the objects cannot change afterwards.
#
# The listing is bounded: a log may hold at most POINTER_HISTORY_LIMIT entries,
# so one read lists at most that many names (a few pages of one thousand) and
# downloads two objects. A log that reaches the bound refuses further moves and
# reads as exhausted rather than being read without limit. One entry is one
# published dossier version or one committed review decision on one version.

POINTER_HISTORY_LIMIT = 4096
ENTRY_FIELDS = (
    "contract_version",
    "sequence",
    "predecessor_sequence",
    "predecessor_sha256",
    "pointer",
)
_SEQUENCE_DIGITS = 20
_ENTRY_TAIL = re.compile(r"[0-9]{20}\.json\Z")


def _entry_tail(sequence: object) -> str:
    if (
        type(sequence) is not int
        or not 0 < sequence < 10**_SEQUENCE_DIGITS
    ):
        raise ValueError("pointer sequence is invalid")
    return f"{sequence:0{_SEQUENCE_DIGITS}d}.json"


def _read_entry(
    bucket: object,
    directory: str,
    sequence: int,
    *,
    contract: str,
    invalid_code: str,
    parse_payload: Callable[[object], dict],
) -> tuple[dict, bytes, int]:
    found = _read_object(bucket, directory + _entry_tail(sequence))
    if found is None:
        # Listed and then gone. Nothing deletes an entry, so the storage
        # cannot be trusted to answer for this pointer right now.
        raise DossierStoreError("dossier_store_unavailable")
    raw, generation = found
    try:
        entry = _load(raw)
    except DossierRecordInvalid:
        raise DossierRecordInvalid(invalid_code) from None
    if (
        set(entry) != set(ENTRY_FIELDS)
        or entry["contract_version"] != contract
        or type(entry["sequence"]) is not int
        or entry["sequence"] != sequence
        or entry["predecessor_sequence"] != (sequence - 1 if sequence > 1 else None)
        or type(entry["predecessor_sequence"]) not in (int, type(None))
        or (
            entry["predecessor_sha256"] is not None
            if sequence == 1
            else not _digest(entry["predecessor_sha256"])
        )
        or _canonical(entry) != raw
    ):
        raise DossierRecordInvalid(invalid_code)
    entry["pointer"] = parse_payload(entry["pointer"])
    return entry, raw, generation


def read_pointer_log(
    bucket: object,
    *,
    directory: str,
    retired_name: str,
    contract: str,
    invalid_code: str,
    exhausted_code: str,
    parse_payload: Callable[[object], dict],
) -> dict | None:
    """The top entry of one pointer log, or None when nothing was ever appended.

    The result carries the entry's sequence, its payload, the generation of
    the object that holds it and the SHA256 of its bytes, which the next entry
    must name.
    """
    listing_prefix = directory[:-1]
    if not directory.endswith("/") or not retired_name.startswith(listing_prefix):
        raise ValueError("pointer log layout is invalid")
    try:
        names = []
        # One more than the bound, so a log past it is seen without listing all of it.
        listing = bucket.list_blobs(
            prefix=listing_prefix, max_results=POINTER_HISTORY_LIMIT + 1
        )
        for blob in listing:
            names.append(blob.name)
            if len(names) > POINTER_HISTORY_LIMIT:
                break
    except Exception as error:
        raise DossierStoreError("dossier_store_unavailable") from error
    if len(names) > POINTER_HISTORY_LIMIT:
        raise DossierStoreError(exhausted_code)
    sequences = []
    for name in names:
        if type(name) is not str or not name.startswith(listing_prefix):
            raise DossierStoreError("dossier_store_unavailable")
        tail = name[len(directory) :]
        if not name.startswith(directory) or _ENTRY_TAIL.fullmatch(tail) is None:
            # The retired single object, or anything else under the pointer's
            # name, is not an entry this reader can place in the log.
            raise DossierRecordInvalid(invalid_code)
        sequences.append(int(tail[: -len(".json")]))
    if not sequences:
        return None
    sequences.sort()
    if sequences != list(range(1, len(sequences) + 1)):
        raise DossierRecordInvalid(invalid_code)
    top = sequences[-1]
    read = {
        "contract": contract,
        "invalid_code": invalid_code,
        "parse_payload": parse_payload,
    }
    entry, raw, generation = _read_entry(bucket, directory, top, **read)
    if top > 1:
        _prior, prior_raw, _prior_generation = _read_entry(
            bucket, directory, top - 1, **read
        )
        if hashlib.sha256(prior_raw).hexdigest() != entry["predecessor_sha256"]:
            raise DossierRecordInvalid(invalid_code)
    return {
        "sequence": top,
        "payload": entry["pointer"],
        "generation": generation,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def append_pointer_log(
    bucket: object,
    *,
    directory: str,
    contract: str,
    exhausted_code: str,
    head: Mapping[str, object] | None,
    payload: dict,
    accept_identical: bool = False,
) -> int:
    """Create the entry after head. A taken number raises PreconditionFailed.

    With accept_identical, a number already holding exactly these bytes is
    this append, landed by an earlier attempt whose answer was lost and
    retried by the client library, so its generation is returned as the
    write's own. The review state log does not ask for this: its caller
    rereads the head on a taken number and reports what that head holds.
    """
    sequence = 1 if head is None else head["sequence"] + 1
    if sequence > POINTER_HISTORY_LIMIT:
        raise DossierStoreError(exhausted_code)
    entry = {
        "contract_version": contract,
        "sequence": sequence,
        "predecessor_sequence": None if head is None else head["sequence"],
        "predecessor_sha256": None if head is None else head["sha256"],
        "pointer": payload,
    }
    name = directory + _entry_tail(sequence)
    body = _canonical(entry)
    try:
        return _upload(bucket, name, body, if_generation_match=0)
    except PreconditionFailed:
        if not accept_identical:
            raise
        found = _read_object(bucket, name)
        if found is None or found[0] != body:
            raise
        return found[1]


def _pointer_log(prefix: str, investigation_id: str) -> dict:
    return {
        "directory": pointer_directory(prefix, investigation_id),
        "contract": POINTER_ENTRY_CONTRACT_VERSION,
        "exhausted_code": "dossier_pointer_history_exhausted",
    }


def _read_pointer_head(bucket: object, prefix: str, investigation_id: str) -> dict | None:
    def parse(payload: object) -> dict:
        pointer = _validate_pointer(payload)
        if pointer["investigation_id"] != investigation_id:
            raise DossierRecordInvalid("dossier_pointer_invalid")
        return pointer

    log = _pointer_log(prefix, investigation_id)
    return read_pointer_log(
        bucket,
        directory=log["directory"],
        retired_name=pointer_object_name(prefix, investigation_id),
        contract=log["contract"],
        invalid_code="dossier_pointer_invalid",
        exhausted_code=log["exhausted_code"],
        parse_payload=parse,
    )


def read_pointer(*, bucket: object, prefix: str, investigation_id: str) -> dict | None:
    """The current pointer with the generation of the entry holding it, or None when nothing is published."""
    head = _read_pointer_head(bucket, prefix, investigation_id)
    if head is None:
        return None
    return {"pointer": head["payload"], "generation": str(head["generation"])}


def publish_pointer(pointer: dict, *, bucket: object, prefix: str) -> dict:
    """Append the next pointer entry, towards a verified target, from its predecessor."""
    pointer = _validate_pointer(pointer)
    investigation_id = pointer["investigation_id"]
    try:
        target = read_dossier_version(
            bucket=bucket,
            prefix=prefix,
            investigation_id=investigation_id,
            dossier_version=pointer["dossier_version"],
            scope_digest=pointer["scope_digest"],
        )
    except DossierStoreError as error:
        raise DossierPointerFailed("pointer_target_unverified") from error
    if (
        target["object_name"] != pointer["object_name"]
        or target["generation"] != pointer["generation"]
        or target["dossier_version"] != pointer["body_sha256"]
        or target["record"]["predecessor_version"] != pointer["predecessor_version"]
    ):
        raise DossierPointerFailed("pointer_target_unverified")
    head = _read_pointer_head(bucket, prefix, investigation_id)
    if head is not None:
        if head["payload"] == pointer:
            return {
                "pointer": pointer,
                "generation": str(head["generation"]),
                "moved": False,
            }
        if head["payload"]["dossier_version"] != pointer["predecessor_version"]:
            raise DossierPointerFailed("pointer_predecessor_mismatch")
    log = _pointer_log(prefix, investigation_id)
    try:
        generation = append_pointer_log(
            bucket,
            directory=log["directory"],
            contract=log["contract"],
            exhausted_code=log["exhausted_code"],
            head=head,
            payload=pointer,
            accept_identical=True,
        )
    except PreconditionFailed:
        raise DossierPointerFailed("pointer_generation_conflict") from None
    except DossierStoreError as error:
        raise DossierPointerFailed(error.code) from error
    return {"pointer": pointer, "generation": str(generation), "moved": True}
