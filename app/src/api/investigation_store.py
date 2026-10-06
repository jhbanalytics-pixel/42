"""Create-only durable GCS storage for Evidence Room investigations."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from google.api_core.exceptions import Forbidden, PreconditionFailed

from . import investigations

_PREFIX = re.compile(r"^(?:[A-Za-z0-9_-]+/)+$")
_INVESTIGATION_ID = re.compile(r"^inv_[A-Za-z0-9_-]{1,128}$")
STAGING_PREFIX = "open-intelligence/v2/staging/"


class InvestigationConflict(RuntimeError):
    pass


class InvestigationStoreError(RuntimeError):
    pass


class InvestigationRecordInvalid(InvestigationStoreError):
    pass


@dataclass(frozen=True, slots=True)
class InvestigationWriteResult:
    object_name: str
    created: bool
    unchanged: bool


def investigation_object_name(prefix: object, investigation_id: object) -> str:
    if not isinstance(prefix, str) or _PREFIX.fullmatch(prefix) is None or ".." in prefix:
        raise ValueError("prefix is invalid")
    if prefix != STAGING_PREFIX:
        raise ValueError("prefix must be exact staging namespace")
    if (
        not isinstance(investigation_id, str)
        or _INVESTIGATION_ID.fullmatch(investigation_id) is None
    ):
        raise ValueError("investigation id is invalid")
    return f"{prefix}investigations/{investigation_id}.json"


def _validated_record(record: object) -> Mapping[str, object]:
    if not isinstance(record, Mapping):
        raise ValueError("investigation record must be an object")
    response = record.get("response")
    if isinstance(response, Mapping) and response.get("status") != "plan_ready":
        raise ValueError("status is invalid for creation")
    investigations.validate_investigation_storage_record(record)
    return record


def canonical_investigation_bytes(record: object) -> bytes:
    validated = _validated_record(record)
    try:
        payload = json.dumps(
            validated,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("investigation record is not JSON serializable") from error
    return payload.encode("utf-8")


def create_investigation(*, bucket: object, prefix: str, record: object) -> InvestigationWriteResult:
    validated = _validated_record(record)
    investigation_id = str(validated["response"]["investigation_id"])
    object_name = investigation_object_name(prefix, investigation_id)
    canonical = canonical_investigation_bytes(validated)
    try:
        blob = bucket.blob(object_name)
        blob.upload_from_string(
            canonical,
            content_type="application/json",
            if_generation_match=0,
        )
    except PreconditionFailed:
        return _existing(bucket, object_name, canonical)
    except Forbidden as error:
        # The staging identity may create but not replace, so a create to a
        # taken name can be refused for permission rather than for its
        # precondition. A taken name is the same fact either way; a free one
        # means the identity cannot write at all.
        try:
            taken = bucket.get_blob(object_name) is not None
        except Exception as lookup_error:
            raise InvestigationStoreError("investigation create failed") from lookup_error
        if not taken:
            raise InvestigationStoreError("investigation create failed") from error
        return _existing(bucket, object_name, canonical)
    except Exception as error:
        raise InvestigationStoreError("investigation create failed") from error
    return InvestigationWriteResult(object_name, created=True, unchanged=False)


def _existing(bucket: object, object_name: str, canonical: bytes) -> InvestigationWriteResult:
    try:
        existing_blob = bucket.get_blob(object_name)
        if existing_blob is None:
            raise InvestigationStoreError("existing investigation could not be read")
        existing = existing_blob.download_as_bytes()
    except Exception as error:
        raise InvestigationStoreError("investigation create failed") from error
    if existing == canonical:
        return InvestigationWriteResult(object_name, created=False, unchanged=True)
    raise InvestigationConflict("investigation canonical content conflict")


def read_investigation(
    *, bucket: object, prefix: str, investigation_id: str
) -> dict[str, object] | None:
    object_name = investigation_object_name(prefix, investigation_id)
    try:
        blob = bucket.get_blob(object_name)
    except Exception as error:
        raise InvestigationStoreError("investigation read failed") from error
    if blob is None:
        return None
    try:
        raw = blob.download_as_bytes()
    except Exception as error:
        raise InvestigationStoreError("investigation read failed") from error
    try:
        payload = json.loads(raw)
        _frame, response = investigations.validate_investigation_storage_record(payload)
    except Exception as error:
        if isinstance(error, InvestigationStoreError):
            raise
        raise InvestigationRecordInvalid("stored investigation is invalid") from error
    if response.get("investigation_id") != investigation_id:
        raise InvestigationRecordInvalid("stored investigation identity mismatch")
    return dict(payload)


def read_investigation_parts(
    *, bucket: object, prefix: str, investigation_id: str
) -> tuple[investigations.InvestigationFrame, dict[str, object], dict[str, object]] | None:
    record = read_investigation(
        bucket=bucket,
        prefix=prefix,
        investigation_id=investigation_id,
    )
    if record is None:
        return None
    try:
        frame, response = investigations.validate_investigation_storage_record(record)
    except ValueError as error:
        raise InvestigationRecordInvalid("stored investigation is invalid") from error
    return frame, response, record
