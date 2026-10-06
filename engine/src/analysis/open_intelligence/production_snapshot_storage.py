"""Private create-only storage for protected source capture artifacts."""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime

BUCKET_NAME = "ogilvy-trends-v2-oi-source-artifacts-staging"
MAX_ARTIFACT_BYTES = 536_870_912
_CAPTURE_VERSION = "open_intelligence_protected_source_capture_artifact_v1"
_CAPTURE_VERSION_V2 = "open_intelligence_protected_source_capture_artifact_v2"
_INPUT_NAMES = {
    "bridge_policy",
    "temporal_rules",
    "collection_receipt_set",
    "history_completion_set",
    "build_provenance",
    "capture_contract",
    "capture_plan",
    "recovery_context",
    "source_metadata",
    "storage_policy",
    # The v3 bridge plan's own inputs, which the bridge loader reads by manifest digest.
    "bridge_policy",
    "collection_receipt_set",
    "history_completion_set",
    "temporal_rules",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT_FIELDS = {
    "contract_version",
    "initial_manifest_sha256",
    "capture_plan",
    "capture",
    "creation_evidence",
}
_ATTEMPT_FIELDS = {"uri", "size_bytes", "sha256", "captured_at"}
_STORED_FIELDS = {"uri", "generation", "size_bytes", "sha256", "created_at"}


class SourceCaptureStorageError(ValueError):
    pass


def _error(code, cause=None):
    if cause is None:
        raise SourceCaptureStorageError(code)
    raise SourceCaptureStorageError(code) from cause


def _digest(value, code="artifact_invalid"):
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _error(code)
    return value


def _timeout(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        _error("storage_timeout_invalid")
    return value


def _timestamp(value, code="artifact_invalid"):
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            _error(code, error)
    else:
        _error(code)
    if parsed.tzinfo is None:
        _error(code)
    return parsed.astimezone(UTC)


def _canonical_timestamp(value, code="artifact_invalid"):
    parsed = _timestamp(value, code)
    if type(value) is not str or value != parsed.isoformat():
        _error(code)
    return value


def _version_timestamp(value, artifact_version):
    if artifact_version == _CAPTURE_VERSION:
        return _canonical_timestamp(value)
    if artifact_version != _CAPTURE_VERSION_V2:
        _error("artifact_version_invalid")
    from .execution_approval import _parse_timestamp

    try:
        _parse_timestamp(value, "artifact_invalid")
    except ValueError as error:
        _error("artifact_invalid", error)
    return value


def _stored_timestamp(value, artifact_version):
    parsed = _timestamp(value)
    if artifact_version == _CAPTURE_VERSION:
        return parsed.isoformat()
    if artifact_version != _CAPTURE_VERSION_V2:
        _error("artifact_version_invalid")
    from .execution_approval import _format_timestamp

    return _format_timestamp(parsed, "artifact_invalid")


def _json_value(value, depth=0):
    if depth > 40:
        _error("artifact_invalid")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            _error("artifact_invalid")
        return {key: _json_value(item, depth + 1) for key, item in value.items()}
    if type(value) is list:
        return [_json_value(item, depth + 1) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    _error("artifact_invalid")


def _pack(value):
    if type(value) is not dict:
        _error("artifact_invalid")
    try:
        raw = json.dumps(
            _json_value(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        _error("artifact_invalid", error)
    if not raw or len(raw) > MAX_ARTIFACT_BYTES:
        _error("artifact_too_large")
    return raw


def _unpack(raw):
    if type(raw) is not bytes or not raw or len(raw) > MAX_ARTIFACT_BYTES:
        _error("artifact_invalid")

    def unique(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                _error("artifact_invalid")
            output[key] = value
        return output

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        _error("artifact_invalid", error)
    if _pack(value) != raw:
        _error("artifact_invalid")
    return value


def _capture_name(manifest, digest):
    return f"captures/{manifest}/{digest}/capture.json"


def _capture_uri(name):
    return f"gs://{BUCKET_NAME}/{name}"


def _validate_artifact(value, manifest, artifact_version=_CAPTURE_VERSION):
    _digest(manifest)
    if (
        type(value) is not dict
        or set(value) != _ARTIFACT_FIELDS
        or artifact_version not in (_CAPTURE_VERSION, _CAPTURE_VERSION_V2)
        or value["contract_version"] != artifact_version
        or value["initial_manifest_sha256"] != manifest
        or type(value["capture_plan"]) is not dict
        or type(value["capture"]) is not dict
        or type(value["creation_evidence"]) is not list
    ):
        _error("artifact_invalid")
    if artifact_version == _CAPTURE_VERSION_V2:
        from .source_estate_bridge import FACT_FIELDS, LANES, READBACK_FIELDS

        capture = value["capture"]
        if (
            set(capture) != FACT_FIELDS
            or capture["contract_version"] != "open_intelligence_source_bridge_capture_v1"
            or type(capture["relation_readbacks"]) is not list
            or len(capture["relation_readbacks"]) != len(LANES)
            or any(
                type(row) is not dict or set(row) != READBACK_FIELDS
                for row in capture["relation_readbacks"]
            )
        ):
            _error("artifact_invalid")
    _capture_timestamp(value, artifact_version)
    return value


def _capture_timestamp(artifact, artifact_version=_CAPTURE_VERSION):
    try:
        value = (
            artifact["capture"]["captured_at"]
            if artifact_version == _CAPTURE_VERSION_V2
            else artifact["capture"]["assembly"]["snapshot"]["captured_at"]
        )
    except (KeyError, TypeError) as error:
        _error("artifact_invalid", error)
    return _version_timestamp(value, artifact_version)


def _validate_attempt(value, artifact_version=_CAPTURE_VERSION):
    if (
        type(value) is not dict
        or set(value) != _ATTEMPT_FIELDS
        or type(value["uri"]) is not str
        or type(value["size_bytes"]) is not int
        or not 0 < value["size_bytes"] <= MAX_ARTIFACT_BYTES
    ):
        _error("artifact_invalid")
    digest = _digest(value["sha256"])
    captured_at = _version_timestamp(value["captured_at"], artifact_version)
    prefix = f"gs://{BUCKET_NAME}/captures/"
    parts = value["uri"].removeprefix(prefix).split("/")
    if (
        not value["uri"].startswith(prefix)
        or len(parts) != 3
        or _DIGEST.fullmatch(parts[0]) is None
        or parts[1] != digest
        or parts[2] != "capture.json"
    ):
        _error("artifact_invalid")
    return parts[0], digest, captured_at


def _validate_stored(value, attempt, artifact_version=_CAPTURE_VERSION):
    if (
        type(value) is not dict
        or set(value) != _STORED_FIELDS
        or value["uri"] != attempt["uri"]
        or type(value["generation"]) is not int
        or value["generation"] <= 0
        or value["size_bytes"] != attempt["size_bytes"]
        or value["sha256"] != attempt["sha256"]
    ):
        _error("artifact_invalid")
    created = _version_timestamp(value["created_at"], artifact_version)
    if _timestamp(created) < _timestamp(attempt["captured_at"]):
        _error("artifact_invalid")
    return value


class SourceCaptureObjects:
    def __init__(self, bucket):
        if getattr(bucket, "name", None) != BUCKET_NAME:
            _error("storage_target_invalid")
        self.bucket = bucket

    def preflight(self, *, timeout):
        timeout = _timeout(timeout)
        try:
            self.bucket.reload(timeout=timeout, retry=None)
            iam = self.bucket.iam_configuration
            rules = list(self.bucket.lifecycle_rules)
            expected = {
                "action": {"type": "Delete"},
                "condition": {"age": 90, "matchesPrefix": ["captures/"]},
            }
            capture_delete_rules = []
            for rule in rules:
                action = rule.get("action") if isinstance(rule, Mapping) else None
                condition = rule.get("condition") if isinstance(rule, Mapping) else None
                if type(action) is not dict or action.get("type") != "Delete":
                    continue
                prefixes = condition.get("matchesPrefix") if type(condition) is dict else None
                if not prefixes or any(
                    type(prefix) is not str
                    or "captures/".startswith(prefix)
                    or prefix.startswith("captures/")
                    for prefix in prefixes
                ):
                    capture_delete_rules.append(rule)
            if (
                self.bucket.location != "US"
                or iam.uniform_bucket_level_access_enabled is not True
                or iam.public_access_prevention != "enforced"
                or "retentionPolicy" in self.bucket._properties
                or capture_delete_rules != [expected]
            ):
                _error("storage_preflight_invalid")
        except SourceCaptureStorageError:
            raise
        except Exception as error:
            _error("storage_preflight_invalid", error)

    def read_input(self, name, digest, *, timeout):
        timeout = _timeout(timeout)
        digest = _digest(digest, "input_invalid")
        if name not in _INPUT_NAMES:
            _error("input_invalid")
        object_name = f"inputs/{digest}/{name}.json"
        try:
            blob = self.bucket.get_blob(object_name, timeout=timeout, retry=None)
            if blob is None:
                _error("input_unavailable")
            generation = blob.generation
            if (
                type(generation) is not int
                or generation <= 0
                or type(blob.size) is not int
                or not 0 <= blob.size <= MAX_ARTIFACT_BYTES
            ):
                _error("input_invalid")
            raw = blob.download_as_bytes(
                if_generation_match=generation,
                raw_download=True,
                timeout=timeout,
                retry=None,
            )
        except SourceCaptureStorageError:
            raise
        except Exception as error:
            _error("input_unavailable", error)
        if (
            type(raw) is not bytes
            or len(raw) != blob.size
            or hashlib.sha256(raw).hexdigest() != digest
        ):
            _error("input_invalid")
        return raw

    def read_stored_capture(self, stored_artifact, *, timeout):
        """Read one stored v2 capture artifact by the receipt its result recorded.

        The receipt names the object, its generation, size, digest and creation time; the
        object read back must match every one of them, and the bytes must hash to the digest
        its own name carries. Only a v2 bridge capture artifact is returned.
        """
        timeout = _timeout(timeout)
        if type(stored_artifact) is not dict or set(stored_artifact) != _STORED_FIELDS:
            _error("artifact_invalid")
        uri = stored_artifact["uri"]
        digest = _digest(stored_artifact["sha256"])
        generation = stored_artifact["generation"]
        size = stored_artifact["size_bytes"]
        if (
            type(uri) is not str
            or type(generation) is not int
            or generation <= 0
            or type(size) is not int
            or not 0 < size <= MAX_ARTIFACT_BYTES
        ):
            _error("artifact_invalid")
        created = _version_timestamp(stored_artifact["created_at"], _CAPTURE_VERSION_V2)
        prefix = f"gs://{BUCKET_NAME}/captures/"
        parts = uri.removeprefix(prefix).split("/")
        if (
            not uri.startswith(prefix)
            or len(parts) != 3
            or _DIGEST.fullmatch(parts[0]) is None
            or parts[1] != digest
            or parts[2] != "capture.json"
        ):
            _error("artifact_invalid")
        name = _capture_name(parts[0], digest)
        try:
            blob = self.bucket.get_blob(name, generation=generation, timeout=timeout, retry=None)
            if blob is None:
                _error("artifact_unavailable")
            if (
                blob.generation != generation
                or blob.size != size
                or _stored_timestamp(blob.time_created, _CAPTURE_VERSION_V2) != created
            ):
                _error("artifact_conflict")
            raw = blob.download_as_bytes(
                if_generation_match=generation,
                raw_download=True,
                timeout=timeout,
                retry=None,
            )
        except SourceCaptureStorageError:
            raise
        except Exception as error:
            _error("artifact_unavailable", error)
        if type(raw) is not bytes or len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            _error("artifact_conflict")
        try:
            artifact = _unpack(raw)
            _validate_artifact(artifact, parts[0], _CAPTURE_VERSION_V2)
        except SourceCaptureStorageError as error:
            _error("artifact_conflict", error)
        return artifact

    def prepare_capture(
        self, artifact, *, initial_manifest_sha256, artifact_version=_CAPTURE_VERSION
    ):
        artifact = _validate_artifact(artifact, initial_manifest_sha256, artifact_version)
        raw = _pack(artifact)
        digest = hashlib.sha256(raw).hexdigest()
        name = _capture_name(initial_manifest_sha256, digest)
        return raw, {
            "uri": _capture_uri(name),
            "size_bytes": len(raw),
            "sha256": digest,
            "captured_at": _capture_timestamp(artifact, artifact_version),
        }

    def store_capture(
        self, raw_bytes, artifact_attempt, *, timeout, artifact_version=_CAPTURE_VERSION
    ):
        timeout = _timeout(timeout)
        manifest, digest, _captured = _validate_attempt(artifact_attempt, artifact_version)
        artifact = _unpack(raw_bytes)
        _validate_artifact(artifact, manifest, artifact_version)
        if (
            len(raw_bytes) != artifact_attempt["size_bytes"]
            or hashlib.sha256(raw_bytes).hexdigest() != digest
            or _capture_timestamp(artifact, artifact_version) != artifact_attempt["captured_at"]
        ):
            _error("artifact_invalid")
        name = _capture_name(manifest, digest)
        blob = self.bucket.blob(name)
        generation = None
        try:
            blob.upload_from_string(
                raw_bytes,
                content_type="application/json",
                if_generation_match=0,
                timeout=timeout,
                retry=None,
            )
            if type(blob.generation) is int and blob.generation > 0:
                generation = blob.generation
        except Exception:
            pass
        stored = None
        if generation is not None:
            created = _stored_timestamp(blob.time_created, artifact_version)
            stored = {
                "uri": artifact_attempt["uri"],
                "generation": generation,
                "size_bytes": artifact_attempt["size_bytes"],
                "sha256": digest,
                "created_at": created,
            }
        _raw, result = self.read_capture(
            artifact_attempt,
            stored_artifact=stored,
            timeout=timeout,
            artifact_version=artifact_version,
        )
        return result

    def read_capture(
        self, artifact_attempt, stored_artifact=None, *, timeout, artifact_version=_CAPTURE_VERSION
    ):
        timeout = _timeout(timeout)
        manifest, digest, _captured = _validate_attempt(artifact_attempt, artifact_version)
        if stored_artifact is not None:
            _validate_stored(stored_artifact, artifact_attempt, artifact_version)
            requested_generation = stored_artifact["generation"]
        else:
            requested_generation = None
        name = _capture_name(manifest, digest)
        try:
            blob = self.bucket.get_blob(
                name, generation=requested_generation, timeout=timeout, retry=None
            )
            if blob is None:
                _error(
                    "artifact_version_unavailable"
                    if requested_generation is not None
                    else "artifact_unavailable"
                )
            generation = blob.generation
            if (
                type(generation) is not int
                or generation <= 0
                or (requested_generation is not None and generation != requested_generation)
                or type(blob.size) is not int
                or not 0 < blob.size <= MAX_ARTIFACT_BYTES
            ):
                _error("artifact_invalid")
            created_at = _stored_timestamp(blob.time_created, artifact_version)
            raw = blob.download_as_bytes(
                if_generation_match=generation,
                raw_download=True,
                timeout=timeout,
                retry=None,
            )
        except SourceCaptureStorageError:
            raise
        except Exception as error:
            _error("artifact_unavailable", error)
        result = {
            "uri": artifact_attempt["uri"],
            "generation": generation,
            "size_bytes": blob.size,
            "sha256": hashlib.sha256(raw).hexdigest() if type(raw) is bytes else "",
            "created_at": created_at,
        }
        if (
            type(raw) is not bytes
            or len(raw) != blob.size
            or result["size_bytes"] != artifact_attempt["size_bytes"]
            or result["sha256"] != digest
            or _timestamp(created_at) < _timestamp(artifact_attempt["captured_at"])
        ):
            _error("artifact_conflict")
        try:
            artifact = _unpack(raw)
            _validate_artifact(artifact, manifest, artifact_version)
            if _capture_timestamp(artifact, artifact_version) != artifact_attempt["captured_at"]:
                _error("artifact_conflict")
        except SourceCaptureStorageError as error:
            _error("artifact_conflict", error)
        if stored_artifact is not None and result != stored_artifact:
            _error("artifact_conflict")
        return raw, result


__all__ = [
    "BUCKET_NAME",
    "MAX_ARTIFACT_BYTES",
    "SourceCaptureObjects",
    "SourceCaptureStorageError",
]
