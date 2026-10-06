from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence import execution_approval, recurring_grant_phrases
from src.analysis.open_intelligence.execution_origins import OriginRefusal, OriginRegistry

PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging_approvals"
LOCATION = "US"
APPROVED_BY = "usr_7cddf28d0ef5034c8df4303b474ca9cc20047f0fa6a951fc2b6617fe13f7d2ab"
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_ROUTINE_DIR = ROOT / "infra" / "bigquery_routines"
_MANIFEST_V1 = "open_intelligence_execution_manifest_v1"
_MANIFEST_V2 = "open_intelligence_execution_manifest_v2"
# The exact version literal selects the mode. Review of retained v1 bytes is historical
# read; review and approval of a v2 manifest are new approval against the active pair.
_MODE_BY_VERSION = {_MANIFEST_V1: "historical_read", _MANIFEST_V2: "new_approval"}
# The v1 routine derives its actor as usr_ + sha256(prefix + SESSION_USER()); the same
# bytes are derived here from the credential path before any v2 native write.
_ACTOR_PREFIX = "open-intelligence-execution-approver-v1:"
_SESSION_USER_SQL = "SELECT SESSION_USER() AS session_user"
_ROUTINES = {
    "sp_approve_open_intelligence_execution_v1",
    "sp_approve_open_intelligence_execution_v2",
    "sp_approve_open_intelligence_execution_v3",
    "sp_disable_open_intelligence_execution_approval_v1",
    "sp_disable_open_intelligence_execution_approval_v2",
}
# The v3 routine is v2 plus the capture branch, the daily collection and composition
# rules and the grant kind. Those manifests can only be approved there; every other v2
# operation keeps the reviewed v2 routine.
_APPROVE_V2 = "sp_approve_open_intelligence_execution_v2"
_APPROVE_V3 = "sp_approve_open_intelligence_execution_v3"
_RECEIPT_BY_ROUTINE = {
    _APPROVE_V2: "open_intelligence_execution_approval_receipt_v2",
    _APPROVE_V3: "open_intelligence_execution_approval_receipt_v3",
}
_GRANT_WORDS = {
    "approve-grant": execution_approval._RECURRING_GRANT_KIND,
    "revoke-grant": execution_approval._RECURRING_GRANT_REVOCATION_KIND,
}
# The v3 routine dispatches on the phrase prefix; each word names one kind, so a phrase
# of another kind refuses here before any client exists rather than after the commit.
_MANIFEST_KIND = "execution_manifest_v2"
_PHRASE_PREFIX_BY_KIND = {
    _MANIFEST_KIND: "I approve one 42 staging execution of ",
    execution_approval._RECURRING_GRANT_KIND: recurring_grant_phrases.APPROVAL_PHRASE_PREFIX,
    execution_approval._RECURRING_GRANT_REVOCATION_KIND: (
        recurring_grant_phrases.REVOCATION_PHRASE_PREFIX
    ),
}
_DISABLE_ROUTINES = {
    _MANIFEST_V1: (
        "sp_disable_open_intelligence_execution_approval_v1",
        "open_intelligence_execution_disable_receipt_v1",
    ),
    _MANIFEST_V2: (
        "sp_disable_open_intelligence_execution_approval_v2",
        "open_intelligence_execution_disable_receipt_v2",
    ),
}


class ApprovalCliRefusal(RuntimeError):
    pass


def _load_credentials() -> object:
    import google.auth

    credentials, project = google.auth.default(
        scopes=("https://www.googleapis.com/auth/cloud-platform",)
    )
    if project != PROJECT:
        raise ApprovalCliRefusal("execution_approval_target_invalid")
    return credentials


def _bigquery_client(credentials: object):
    from google.cloud import bigquery

    return bigquery.Client(project=PROJECT, credentials=credentials, location=LOCATION)


def _routine_source(name: str) -> str:
    if name not in _ROUTINES:
        raise ApprovalCliRefusal("execution_approval_target_invalid")
    return (_ROUTINE_DIR / f"{name}.sql").read_text(encoding="utf-8")


def _active_generation():
    """Load the active trusted generation from packaged source.

    The generation module belongs to the authority adapter. An import failure refuses;
    no registry path, digest or source scope is ever taken from the command line.
    """
    try:
        from src.analysis.open_intelligence.execution_generations import active_generation
    except ImportError as exc:
        raise ApprovalCliRefusal("execution_generation_unavailable") from exc
    try:
        return active_generation()
    except ValueError:
        raise
    except Exception as exc:
        raise ApprovalCliRefusal("execution_generation_unavailable") from exc


def _require_generation(generation):
    registry = getattr(generation, "registry", None)
    digest = getattr(generation, "origin_registry_sha256", None)
    resource_digest = getattr(generation, "resource_manifest_sha256", None)
    resource_manifest = getattr(generation, "resource_manifest", None)
    if (
        type(registry) is not OriginRegistry
        or digest != registry.sha256
        or not isinstance(resource_digest, str)
        or _HEX_64.fullmatch(resource_digest) is None
        or not isinstance(resource_manifest, Mapping)
        or resource_manifest.get("origin_registry_sha256") != digest
    ):
        raise ApprovalCliRefusal("execution_generation_unavailable")
    return generation


def _derive_actor(session_user: object) -> str:
    if not isinstance(session_user, str) or not session_user:
        raise ApprovalCliRefusal("execution_approval_identity_invalid")
    digest = hashlib.sha256((_ACTOR_PREFIX + session_user).encode("utf-8")).hexdigest()
    return f"usr_{digest}"


def _read_manifest_bytes(manifest_path: Path, expected_sha256: str) -> tuple[bytes, object, str]:
    if (
        not isinstance(manifest_path, Path)
        or not manifest_path.is_absolute()
        or not isinstance(expected_sha256, str)
        or _HEX_64.fullmatch(expected_sha256) is None
    ):
        raise ApprovalCliRefusal("execution_approval_manifest_invalid")
    try:
        raw = manifest_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalCliRefusal("execution_approval_manifest_invalid") from exc
    version = payload.get("manifest_version") if isinstance(payload, Mapping) else None
    if version not in _MODE_BY_VERSION:
        raise ApprovalCliRefusal("execution_approval_manifest_invalid")
    return raw, payload, version


def _load_manifest(manifest_path: Path, expected_sha256: str) -> tuple[dict[str, object], bytes]:
    raw, payload, version = _read_manifest_bytes(manifest_path, expected_sha256)
    mode = _MODE_BY_VERSION[version]
    registry = _require_generation(_active_generation()).registry
    try:
        canonical = execution_approval.canonical_manifest_bytes(
            payload, mode=mode, registry=registry
        )
    except (execution_approval.ApprovalRefusal, OriginRefusal) as exc:
        raise ApprovalCliRefusal("execution_approval_manifest_invalid") from exc
    if (
        raw != canonical
        or execution_approval.manifest_sha256(payload, mode=mode, registry=registry)
        != expected_sha256
    ):
        raise ApprovalCliRefusal("execution_approval_manifest_mismatch")
    return payload, canonical


def _load_v2_manifest(
    manifest_path: Path, expected_sha256: str
) -> tuple[dict[str, object], bytes, object]:
    raw, payload, version = _read_manifest_bytes(manifest_path, expected_sha256)
    if version != _MANIFEST_V2:
        raise ApprovalCliRefusal("execution_origin_mode_forbidden")
    generation = _require_generation(_active_generation())
    registry = generation.registry
    try:
        canonical = execution_approval.canonical_manifest_bytes(
            payload, mode="new_approval", registry=registry
        )
    except (execution_approval.ApprovalRefusal, OriginRefusal) as exc:
        raise ApprovalCliRefusal("execution_approval_manifest_invalid") from exc
    if (
        raw != canonical
        or execution_approval.manifest_sha256(payload, mode="new_approval", registry=registry)
        != expected_sha256
    ):
        raise ApprovalCliRefusal("execution_approval_manifest_mismatch")
    return payload, canonical, generation


def review_manifest(manifest_path: Path, expected_sha256: str) -> Mapping[str, object]:
    _raw, _payload, version = _read_manifest_bytes(manifest_path, expected_sha256)
    if version == _MANIFEST_V1:
        payload, _canonical = _load_manifest(manifest_path, expected_sha256)
        return {
            "contract_version": "open_intelligence_execution_review_v1",
            "manifest": payload,
            "manifest_sha256": expected_sha256,
            "operation": payload["operation"],
        }
    payload, _canonical, generation = _load_v2_manifest(manifest_path, expected_sha256)
    return {
        "contract_version": "open_intelligence_execution_review_v2",
        "manifest": payload,
        "manifest_sha256": expected_sha256,
        "operation": payload["operation"],
        "origin_registry_sha256": generation.origin_registry_sha256,
        "resource_manifest_sha256": generation.resource_manifest_sha256,
        "origin_mode": "new_approval",
    }


def _read_approval_phrase() -> str:
    raw = sys.stdin.buffer.read()
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    else:
        raise ApprovalCliRefusal("execution_approval_manifest_mismatch")
    if b"\n" in raw or b"\r" in raw:
        raise ApprovalCliRefusal("execution_approval_manifest_mismatch")
    try:
        phrase = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApprovalCliRefusal("execution_approval_manifest_mismatch") from exc
    if not phrase:
        raise ApprovalCliRefusal("execution_approval_manifest_mismatch")
    return phrase


def _require_phrase_kind(phrase: str, kind: str) -> None:
    prefix = _PHRASE_PREFIX_BY_KIND.get(kind)
    if prefix is None or not phrase.startswith(prefix) or len(phrase) <= len(prefix):
        raise ApprovalCliRefusal("approval_phrase_kind_mismatch")


def _row_value(row: object, field: str) -> object:
    if isinstance(row, Mapping):
        return row.get(field)
    try:
        return row[field]
    except (KeyError, TypeError):
        return getattr(row, field, None)


def _format_time(value: object, code: str) -> str:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
        value,
    ):
        return value
    raise ApprovalCliRefusal(code)


def _query_rows(client: object, sql: str, parameters: list[object]) -> tuple[object, ...]:
    try:
        from google.cloud import bigquery

        rows = tuple(
            client.query(
                sql,
                job_config=bigquery.QueryJobConfig(query_parameters=parameters),
                retry=None,
                job_retry=None,
            ).result(retry=None, job_retry=None)
        )
    except ApprovalCliRefusal:
        raise
    except Exception as exc:
        message = str(exc)
        for code in (
            "execution_approval_identity_invalid",
            "execution_approval_manifest_mismatch",
            "execution_approval_expired",
            "execution_approval_conflict",
            "execution_approval_concurrent_conflict",
            "execution_approval_schema_mismatch",
        ):
            if code in message:
                raise ApprovalCliRefusal(code) from exc
        if "bigquery.jobs.create" in message:
            raise ApprovalCliRefusal("execution_approval_identity_invalid") from exc
        raise ApprovalCliRefusal("execution_approval_internal_refusal") from exc
    if len(rows) != 1:
        raise ApprovalCliRefusal("execution_approval_schema_mismatch")
    return rows


def _run_procedure(name: str, parameters: list[object], *, client: object = None) -> object:
    if name not in _ROUTINES:
        raise ApprovalCliRefusal("execution_approval_target_invalid")
    if client is None:
        client = _bigquery_client(_load_credentials())
    sql = (
        f"CALL `{PROJECT}.{DATASET}.{name}`("
        + ",".join(f"@{item.name}" for item in parameters)
        + ")"
    )
    return _query_rows(client, sql, parameters)[0]


# Operations whose rule only the v3 routine knows: the capture and the daily
# contract's collection and composition. Every other operation stays on v2.
_V3_OPERATIONS = frozenset(
    {"source_snapshot_capture", "source_collection", "daily_composition_apply"}
)


def _approve_routine(operation: object) -> str:
    return _APPROVE_V3 if operation in _V3_OPERATIONS else _APPROVE_V2


def _session_actor(client: object) -> str:
    """Derive the pinned actor from the credential path's SESSION_USER, a read only probe."""
    row = _query_rows(client, _SESSION_USER_SQL, [])[0]
    actor = _derive_actor(_row_value(row, "session_user"))
    if actor != APPROVED_BY:
        raise ApprovalCliRefusal("execution_approval_identity_invalid")
    return actor


def approve_manifest(manifest_path: Path, expected_sha256: str) -> Mapping[str, object]:
    """Approve one v2 manifest against the active pair. Retained v1 bytes refuse here."""
    payload, canonical, generation = _load_v2_manifest(manifest_path, expected_sha256)
    origin_registry_sha256 = generation.origin_registry_sha256
    resource_manifest_sha256 = generation.resource_manifest_sha256
    phrase = _read_approval_phrase()
    _require_phrase_kind(phrase, _MANIFEST_KIND)
    try:
        from google.cloud import bigquery

        client = _bigquery_client(_load_credentials())
        _session_actor(client)
        parameters = [
            bigquery.ScalarQueryParameter(
                "canonical_manifest_json", "STRING", canonical.decode("utf-8")
            ),
            bigquery.ScalarQueryParameter("manifest_sha256", "STRING", expected_sha256),
            bigquery.ScalarQueryParameter("approval_phrase", "STRING", phrase),
            bigquery.ScalarQueryParameter(
                "origin_registry_sha256", "STRING", origin_registry_sha256
            ),
            bigquery.ScalarQueryParameter(
                "resource_manifest_sha256", "STRING", resource_manifest_sha256
            ),
        ]
        routine = _approve_routine(payload["operation"])
        row = _run_procedure(routine, parameters, client=client)
    except ApprovalCliRefusal:
        raise
    except Exception as exc:
        raise ApprovalCliRefusal("execution_approval_internal_refusal") from exc
    approved_at = _row_value(row, "approved_at")
    approved_at_text = _format_time(approved_at, "execution_approval_schema_mismatch")
    approved_by = _row_value(row, "approved_by")
    approval_id = _row_value(row, "approval_id")
    expires_at = _format_time(
        _row_value(row, "expires_at"),
        "execution_approval_schema_mismatch",
    )
    expected_id = execution_approval.approval_id_v2(
        expected_sha256,
        APPROVED_BY,
        datetime.strptime(approved_at_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC),
        origin_registry_sha256=origin_registry_sha256,
        resource_manifest_sha256=resource_manifest_sha256,
    )
    receipt = {
        "contract_version": _row_value(row, "contract_version"),
        "approval_id": approval_id,
        "approved_at": approved_at_text,
        "approved_by": approved_by,
        "expires_at": expires_at,
        "manifest_sha256": _row_value(row, "manifest_sha256"),
        "operation": _row_value(row, "operation"),
        "origin_registry_sha256": _row_value(row, "origin_registry_sha256"),
        "resource_manifest_sha256": _row_value(row, "resource_manifest_sha256"),
        "origin_mode": "new_approval",
    }
    if (
        receipt["contract_version"] != _RECEIPT_BY_ROUTINE[routine]
        or approval_id != expected_id
        or approved_by != APPROVED_BY
        or receipt["manifest_sha256"] != expected_sha256
        or receipt["operation"] != payload["operation"]
        or expires_at != payload["expires_at"]
        or receipt["origin_registry_sha256"] != origin_registry_sha256
        or receipt["resource_manifest_sha256"] != resource_manifest_sha256
    ):
        raise ApprovalCliRefusal("execution_approval_schema_mismatch")
    return receipt


def _load_grant(grant_path: Path, expected_sha256: str) -> tuple[dict[str, object], bytes]:
    """Exact canonical grant bytes whose terms digest is the expected one; no client."""
    if (
        not isinstance(grant_path, Path)
        or not grant_path.is_absolute()
        or not isinstance(expected_sha256, str)
        or _HEX_64.fullmatch(expected_sha256) is None
    ):
        raise ApprovalCliRefusal("execution_approval_manifest_invalid")
    try:
        raw = grant_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        canonical = recurring_grant_phrases.canonical_grant_bytes(payload)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ApprovalCliRefusal("execution_approval_manifest_invalid") from exc
    if payload["revocation_state"] != "active":
        raise ApprovalCliRefusal("execution_approval_manifest_invalid")
    if raw != canonical or recurring_grant_phrases.grant_digest(payload) != expected_sha256:
        raise ApprovalCliRefusal("execution_approval_manifest_mismatch")
    return payload, canonical


def _record_grant(grant_path: Path, expected_sha256: str, kind: str) -> Mapping[str, object]:
    payload, canonical = _load_grant(grant_path, expected_sha256)
    generation = _require_generation(_active_generation())
    origin_registry_sha256 = generation.origin_registry_sha256
    resource_manifest_sha256 = generation.resource_manifest_sha256
    phrase = _read_approval_phrase()
    _require_phrase_kind(phrase, kind)
    try:
        from google.cloud import bigquery

        client = _bigquery_client(_load_credentials())
        _session_actor(client)
        parameters = [
            bigquery.ScalarQueryParameter(
                "canonical_manifest_json", "STRING", canonical.decode("utf-8")
            ),
            bigquery.ScalarQueryParameter("manifest_sha256", "STRING", expected_sha256),
            bigquery.ScalarQueryParameter("approval_phrase", "STRING", phrase),
            bigquery.ScalarQueryParameter(
                "origin_registry_sha256", "STRING", origin_registry_sha256
            ),
            bigquery.ScalarQueryParameter(
                "resource_manifest_sha256", "STRING", resource_manifest_sha256
            ),
        ]
        row = _run_procedure(_APPROVE_V3, parameters, client=client)
    except ApprovalCliRefusal:
        raise
    except Exception as exc:
        raise ApprovalCliRefusal("execution_approval_internal_refusal") from exc
    approved_at_text = _format_time(
        _row_value(row, "approved_at"), "execution_approval_schema_mismatch"
    )
    approval_id = _row_value(row, "approval_id")
    approved_by = _row_value(row, "approved_by")
    expected_id = execution_approval.approval_id_v2(
        expected_sha256,
        APPROVED_BY,
        datetime.strptime(approved_at_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC),
        origin_registry_sha256=origin_registry_sha256,
        resource_manifest_sha256=resource_manifest_sha256,
    )
    receipt = {
        "contract_version": _row_value(row, "contract_version"),
        "approval_id": approval_id,
        "approved_at": approved_at_text,
        "approved_by": approved_by,
        "expires_at": _format_time(
            _row_value(row, "expires_at"), "execution_approval_schema_mismatch"
        ),
        "manifest_sha256": _row_value(row, "manifest_sha256"),
        "operation": _row_value(row, "operation"),
        "origin_registry_sha256": _row_value(row, "origin_registry_sha256"),
        "resource_manifest_sha256": _row_value(row, "resource_manifest_sha256"),
        "origin_mode": "new_approval",
        "grant_id": payload["grant_id"],
    }
    if (
        receipt["contract_version"] != _RECEIPT_BY_ROUTINE[_APPROVE_V3]
        or approval_id != expected_id
        or approved_by != APPROVED_BY
        or receipt["manifest_sha256"] != expected_sha256
        or receipt["operation"] != kind
        or receipt["origin_registry_sha256"] != origin_registry_sha256
        or receipt["resource_manifest_sha256"] != resource_manifest_sha256
    ):
        raise ApprovalCliRefusal("execution_approval_schema_mismatch")
    return receipt


def approve_grant(grant_path: Path, expected_sha256: str) -> Mapping[str, object]:
    """Record one recurring grant approval, keyed by the grant digest, through v3."""
    return _record_grant(grant_path, expected_sha256, _GRANT_WORDS["approve-grant"])


def revoke_grant(grant_path: Path, expected_sha256: str) -> Mapping[str, object]:
    """Record the revocation row for an approved grant, keyed by the same digest."""
    return _record_grant(grant_path, expected_sha256, _GRANT_WORDS["revoke-grant"])


def _disable_approvals(manifest_version: str) -> Mapping[str, object]:
    """Disable one exact lock. The version is an explicit selector, never inferred."""
    if manifest_version not in _DISABLE_ROUTINES:
        raise ApprovalCliRefusal("execution_approval_manifest_invalid")
    routine, receipt_version = _DISABLE_ROUTINES[manifest_version]
    phrase = _read_approval_phrase()
    try:
        from google.cloud import bigquery

        row = _run_procedure(
            routine,
            [bigquery.ScalarQueryParameter("approval_phrase", "STRING", phrase)],
        )
    except ApprovalCliRefusal:
        raise
    except Exception as exc:
        raise ApprovalCliRefusal("execution_approval_internal_refusal") from exc
    receipt = {
        "contract_version": _row_value(row, "contract_version"),
        "state": _row_value(row, "state"),
        "disabled_at": _format_time(
            _row_value(row, "disabled_at"),
            "execution_approval_schema_mismatch",
        ),
        "disabled_by": _row_value(row, "disabled_by"),
        "lock_version": _row_value(row, "lock_version"),
    }
    if (
        receipt["contract_version"] != receipt_version
        or receipt["state"] != "disabled"
        or receipt["disabled_by"] != APPROVED_BY
        or type(receipt["lock_version"]) is not int
        or receipt["lock_version"] <= 0
    ):
        raise ApprovalCliRefusal("execution_approval_schema_mismatch")
    return receipt


def _emit(payload: Mapping[str, object], *, error: bool = False) -> None:
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stream = sys.stderr if error else sys.stdout
    stream.write(line + "\n")


def _parse_cli(argv: tuple[str, ...]) -> tuple[str, Path | None, str | None]:
    if (
        len(argv) == 5
        and argv[0] == "review"
        and argv[1] == "--manifest-file"
        and argv[3] == "--manifest-sha256"
    ):
        return "review", Path(argv[2]), argv[4]
    if (
        len(argv) == 6
        and argv[0] == "approve"
        and argv[1] == "--manifest-file"
        and argv[3] == "--manifest-sha256"
        and argv[5] == "--approval-phrase-stdin"
    ):
        return "approve", Path(argv[2]), argv[4]
    if (
        len(argv) == 6
        and argv[0] in _GRANT_WORDS
        and argv[1] == "--grant-file"
        and argv[3] == "--grant-sha256"
        and argv[5] == "--approval-phrase-stdin"
    ):
        return argv[0], Path(argv[2]), argv[4]
    if (
        len(argv) == 4
        and argv[0] == "disable"
        and argv[1] == "--manifest-version"
        and argv[2] in _DISABLE_ROUTINES
        and argv[3] == "--approval-phrase-stdin"
    ):
        return "disable", None, argv[2]
    raise ApprovalCliRefusal("execution_approval_manifest_invalid")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        # The third value is the manifest digest for review and approve, and the exact
        # manifest version selector for disable.
        action, manifest_path, selector = _parse_cli(tuple(sys.argv[1:] if argv is None else argv))
    except ApprovalCliRefusal as exc:
        _emit({"error": str(exc)}, error=True)
        return 2
    try:
        if action == "disable":
            receipt = _disable_approvals(selector)
        elif action == "review":
            receipt = review_manifest(manifest_path, selector)
        elif action == "approve-grant":
            receipt = approve_grant(manifest_path, selector)
        elif action == "revoke-grant":
            receipt = revoke_grant(manifest_path, selector)
        else:
            receipt = approve_manifest(manifest_path, selector)
        _emit(receipt)
        return 0
    except ApprovalCliRefusal as exc:
        _emit({"error": str(exc)}, error=True)
        return 1
    except Exception:
        _emit({"error": "execution_approval_internal_refusal"}, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
