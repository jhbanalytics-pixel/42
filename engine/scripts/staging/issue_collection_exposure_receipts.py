"""Dark, approval-gated issuer for immutable collection exposure receipts."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.execution_origins import select_origin

EXPOSURE_CONTRACT_VERSION = "collection_exposure_receipt_v1"
PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
TABLE = "collection_exposure_receipts_v1"
EXECUTION_PROOF_CONTRACT_VERSION = "collection-exposure-execution-proof-v1"
R3_RUN_ID = "run_20260903_dynamic_apply_v2_r16"
R3_WINDOW_START = date(2026, 8, 21)
R3_WINDOW_END = date(2026, 9, 3)
SOURCE_COPY_REF_FIELDS = ("copy_run_id", "source_table", "source_set_digest")
EXPOSURE_RECEIPT_FIELDS = (
    "exposure_contract_version",
    "source_family",
    "exposure_date",
    "collection_policy_digest",
    "source_sha",
    "image_digest",
    "config_digest",
    "quota_authority_id",
    "quota_applicability",
    "quota_unit",
    "quota_limit",
    "quota_used",
    "quota_exhausted",
    "capture_complete",
    "source_copy_receipt_refs",
    "issued_at",
    "issuer_identity",
    "receipt_digest",
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ExposureRefusal(ValueError):
    """Exposure authority is incomplete or noncanonical."""


@dataclass(frozen=True, slots=True)
class ExposureProfile:
    """One named, immutable exposure run profile: its run and the window it may cover.

    ``label`` names the window in a refusal. ``contiguous`` requires each family's days in
    one set to form a single unbroken run, so a daily window cannot be assembled around a
    missing day; the R3 profile predates it and keeps its own rule.
    """

    name: str
    run_id: str
    window_start: date
    window_end: date
    label: str
    contiguous: bool


# The R3 profile is the one this issuer was written for, kept exactly: its run, its window
# and the bytes of every artifact it binds are unchanged.
R3_EXPOSURE_PROFILE = ExposureProfile(
    name="r3_exposure_v1",
    run_id=R3_RUN_ID,
    window_start=R3_WINDOW_START,
    window_end=R3_WINDOW_END,
    label="R3",
    contiguous=False,
)
DAILY_EXPOSURE_PROFILE_NAME = "daily_exposure_v1"
# The daily factor window the composer measures family direction over, ending on the
# closed day (``daily_composer.FACTOR_WINDOW_DAYS``).
DAILY_EXPOSURE_WINDOW_DAYS = 14


def daily_exposure_profile(signal_date: object) -> ExposureProfile:
    """The daily profile of one closed day: the fourteen days ending on it.

    It is chosen by naming it with its day, never by reading a receipt's date.
    """
    if not isinstance(signal_date, date) or isinstance(signal_date, datetime):
        raise ExposureRefusal("daily exposure signal date must be a calendar date")
    return ExposureProfile(
        name=DAILY_EXPOSURE_PROFILE_NAME,
        run_id=f"daily_exposure_{signal_date:%Y%m%d}",
        window_start=signal_date - timedelta(days=DAILY_EXPOSURE_WINDOW_DAYS - 1),
        window_end=signal_date,
        label="daily",
        contiguous=True,
    )


@dataclass(frozen=True, slots=True)
class ExposureExecutionProof:
    execution_proof_contract_version: str
    issuer_addendum_sha256: str
    execution_name: str
    job_resource: str
    image_digest: str
    source_sha: str
    service_identity: str
    command: str
    args: tuple[str, ...]
    config_digest: str
    run_id: str
    inserted_natural_keys: tuple[tuple[str, date], ...]
    inserted_natural_key_digest: str
    receipt_readback_digest: str
    started_at: datetime
    completed_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class ExposureIssueReport:
    inserted_natural_keys: tuple[tuple[str, date], ...]
    inserted_natural_key_digest: str
    receipt_readback_digest: str
    execution_proof: ExposureExecutionProof
    execution_proof_digest: str


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExposureRefusal(f"{field} must be a nonempty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ExposureRefusal(f"noncanonical_unicode:{field}")
    return value


def _digest(value: object, field: str) -> str:
    value = _text(value, field)
    if _DIGEST.fullmatch(value) is None:
        raise ExposureRefusal(f"{field} must be lower-case SHA256")
    return value


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExposureRefusal(f"{field} must be a nonnegative integer")
    return value


def _refs(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise ExposureRefusal("source_copy_receipt_refs must be nonempty")
    refs = []
    for item in value:
        if not isinstance(item, Mapping) or tuple(item) != SOURCE_COPY_REF_FIELDS:
            raise ExposureRefusal("source-copy receipt reference fields are invalid")
        refs.append(
            {
                "copy_run_id": _text(item["copy_run_id"], "copy_run_id"),
                "source_table": _text(item["source_table"], "source_table"),
                "source_set_digest": _digest(item["source_set_digest"], "source_set_digest"),
            }
        )
    keys = tuple(
        (item["copy_run_id"], item["source_table"], item["source_set_digest"]) for item in refs
    )
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise ExposureRefusal("source-copy receipt references must be sorted and unique")
    return tuple(refs)


def _preimage(fields: Mapping[str, object]) -> dict[str, object]:
    return {field: fields[field] for field in EXPOSURE_RECEIPT_FIELDS[:15]}


def exposure_receipt_digest(fields: Mapping[str, object]) -> str:
    if not isinstance(fields, Mapping):
        raise ExposureRefusal("exposure receipt is invalid")
    preimage = dict(_preimage(fields))
    preimage["source_family"] = _text(preimage["source_family"], "source_family")
    preimage["source_copy_receipt_refs"] = _refs(preimage["source_copy_receipt_refs"])
    return canonical_digest(preimage)


def validate_exposure_receipt(fields: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(fields, Mapping) or tuple(fields) != EXPOSURE_RECEIPT_FIELDS:
        raise ExposureRefusal("exposure receipt field order is invalid")
    if fields["exposure_contract_version"] != EXPOSURE_CONTRACT_VERSION:
        raise ExposureRefusal("exposure contract version is invalid")
    if not isinstance(fields["exposure_date"], date) or isinstance(
        fields["exposure_date"], datetime
    ):
        raise ExposureRefusal("exposure_date must be a date")
    _text(fields["source_family"], "source_family")
    for field in ("collection_policy_digest", "config_digest", "quota_authority_id"):
        _digest(fields[field], field)
    if not isinstance(fields["source_sha"], str) or _SHA.fullmatch(fields["source_sha"]) is None:
        raise ExposureRefusal("source_sha is invalid")
    if (
        not isinstance(fields["image_digest"], str)
        or _IMAGE.fullmatch(fields["image_digest"]) is None
    ):
        raise ExposureRefusal("image_digest is invalid")
    applicability = fields["quota_applicability"]
    if applicability == "metered":
        _text(fields["quota_unit"], "quota_unit")
        limit = _count(fields["quota_limit"], "quota_limit")
        used = _count(fields["quota_used"], "quota_used")
        if used > limit:
            raise ExposureRefusal("quota_used exceeds quota_limit")
    elif applicability == "unmetered":
        if any(fields[field] is not None for field in ("quota_unit", "quota_limit", "quota_used")):
            raise ExposureRefusal("unmetered quota fields must be null")
        if fields["quota_exhausted"] is not False:
            raise ExposureRefusal("unmetered quota cannot be exhausted")
    else:
        raise ExposureRefusal("quota_applicability is invalid")
    if type(fields["quota_exhausted"]) is not bool or type(fields["capture_complete"]) is not bool:
        raise ExposureRefusal("exposure booleans are invalid")
    refs = _refs(fields["source_copy_receipt_refs"])
    issued_at = fields["issued_at"]
    if not isinstance(issued_at, datetime) or issued_at.tzinfo is None:
        raise ExposureRefusal("issued_at must be timezone aware")
    _text(fields["issuer_identity"], "issuer_identity")
    if fields["receipt_digest"] != exposure_receipt_digest(fields):
        raise ExposureRefusal("exposure receipt digest differs")
    return {**fields, "source_copy_receipt_refs": refs}


def validate_exposure_receipt_window(
    receipts: Sequence[Mapping[str, object]],
    *,
    profile: ExposureProfile,
) -> tuple[Mapping[str, object], ...]:
    """Validate one receipt set under the exposure profile the caller names."""
    if not isinstance(profile, ExposureProfile):
        raise ExposureRefusal("exposure profile must be named explicitly")
    validated = tuple(validate_exposure_receipt(receipt) for receipt in receipts)
    keys = tuple((item["source_family"], item["exposure_date"]) for item in validated)
    if len(keys) != len(set(keys)):
        raise ExposureRefusal("duplicate exposure natural key")
    if any(
        item["exposure_date"] < profile.window_start or item["exposure_date"] > profile.window_end
        for item in validated
    ):
        raise ExposureRefusal(f"exposure receipt is outside the {profile.label} source window")
    ordered = tuple(
        sorted(validated, key=lambda item: (item["source_family"], item["exposure_date"]))
    )
    if profile.contiguous:
        days_by_family: dict[str, list[date]] = {}
        for item in ordered:
            days_by_family.setdefault(item["source_family"], []).append(item["exposure_date"])
        for days in days_by_family.values():
            if (days[-1] - days[0]).days + 1 != len(days):
                raise ExposureRefusal(
                    f"exposure receipts leave a gap in the {profile.label} source window"
                )
    return ordered


def validate_exposure_receipt_set(
    receipts: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    """The R3 receipt set, as it has always been validated."""
    return validate_exposure_receipt_window(receipts, profile=R3_EXPOSURE_PROFILE)


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ExposureRefusal(f"Execution authority {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExposureRefusal(f"Execution authority {field} is invalid") from error
    if parsed.tzinfo is None:
        raise ExposureRefusal(f"Execution authority {field} is invalid")
    return parsed.astimezone(UTC)


def _sql_text(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _sql_value(value: object) -> str:
    if value is None:
        return "NULL"
    if type(value) is bool:
        return "TRUE" if value else "FALSE"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, datetime):
        return f"TIMESTAMP {_sql_text(value.isoformat())}"
    if isinstance(value, date):
        return f"DATE {_sql_text(value.isoformat())}"
    if isinstance(value, str):
        return _sql_text(value)
    if isinstance(value, (tuple, list)):
        refs = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ExposureRefusal("source-copy receipt reference is invalid")
            refs.append(
                "STRUCT("
                + ", ".join(_sql_text(str(item[field])) for field in SOURCE_COPY_REF_FIELDS)
                + ")"
            )
        return "[" + ", ".join(refs) + "]"
    raise ExposureRefusal("exposure SQL value is invalid")


def _load_receipt_artifact(path: Path) -> tuple[Mapping[str, object], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ExposureRefusal("exposure receipt artifact is unreadable") from error
    return _parse_receipt_artifact(text)


def _parse_receipt_artifact(text: str | bytes) -> tuple[Mapping[str, object], ...]:
    """The R3 artifact: a bare list of receipts, validated under the R3 profile."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ExposureRefusal("exposure receipt artifact is unreadable") from error
    return validate_exposure_receipt_set(_artifact_receipts(payload))


def _artifact_profile(value: object) -> ExposureProfile:
    """The exposure profile an artifact names, exactly one of the known shapes."""
    if isinstance(value, dict) and value == {"name": R3_EXPOSURE_PROFILE.name}:
        return R3_EXPOSURE_PROFILE
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "signal_date"}
        or value["name"] != DAILY_EXPOSURE_PROFILE_NAME
        or not isinstance(value["signal_date"], str)
    ):
        raise ExposureRefusal("exposure profile of the receipt artifact is invalid")
    try:
        signal_date = date.fromisoformat(value["signal_date"])
    except ValueError as error:
        raise ExposureRefusal("exposure profile of the receipt artifact is invalid") from error
    return daily_exposure_profile(signal_date)


def _parse_profiled_receipt_artifact(
    text: str | bytes,
) -> tuple[ExposureProfile, tuple[Mapping[str, object], ...]]:
    """An artifact that names its profile, or a bare list, which is the R3 artifact.

    The profile rides in the artifact whose bytes the approved manifest pins, so the
    approval names it; it is never read off the receipts' dates.
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as error:
        raise ExposureRefusal("exposure receipt artifact is unreadable") from error
    if isinstance(payload, list):
        return R3_EXPOSURE_PROFILE, validate_exposure_receipt_set(_artifact_receipts(payload))
    if not isinstance(payload, dict) or set(payload) != {"exposure_profile", "receipts"}:
        raise ExposureRefusal("exposure receipt artifact fields are invalid")
    profile = _artifact_profile(payload["exposure_profile"])
    receipts = _artifact_receipts(payload["receipts"])
    return profile, validate_exposure_receipt_window(receipts, profile=profile)


def _artifact_receipts(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, list) or not payload:
        raise ExposureRefusal("exposure receipt artifact is empty")
    receipts = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != set(EXPOSURE_RECEIPT_FIELDS):
            raise ExposureRefusal("exposure receipt artifact fields are invalid")
        ordered = {field: item[field] for field in EXPOSURE_RECEIPT_FIELDS}
        try:
            ordered["exposure_date"] = date.fromisoformat(ordered["exposure_date"])
            ordered["issued_at"] = datetime.fromisoformat(
                ordered["issued_at"].replace("Z", "+00:00")
            )
        except (AttributeError, ValueError) as error:
            raise ExposureRefusal("exposure receipt artifact date is invalid") from error
        refs = ordered["source_copy_receipt_refs"]
        if not isinstance(refs, list) or any(
            not isinstance(ref, dict) or set(ref) != set(SOURCE_COPY_REF_FIELDS) for ref in refs
        ):
            raise ExposureRefusal("exposure receipt artifact references are invalid")
        ordered["source_copy_receipt_refs"] = tuple(
            {field: ref[field] for field in SOURCE_COPY_REF_FIELDS} for ref in refs
        )
        receipts.append(ordered)
    return receipts


_DURABLE_ARTIFACT_CONTEXT: dict[str, bytes] | None = None


def _build_execution_artifacts(
    receipts: tuple[Mapping[str, object], ...],
    *,
    profile: ExposureProfile = R3_EXPOSURE_PROFILE,
) -> dict[str, bytes]:
    source_copy_refs = tuple(
        sorted(
            (
                ref["copy_run_id"],
                ref["source_table"],
                ref["source_set_digest"],
            )
            for receipt in receipts
            for ref in receipt["source_copy_receipt_refs"]
        )
    )
    quota_rows = tuple(
        sorted(
            (
                receipt["quota_authority_id"],
                receipt["quota_applicability"],
                receipt["quota_unit"],
                receipt["quota_limit"],
                receipt["quota_used"],
                receipt["quota_exhausted"],
            )
            for receipt in receipts
        )
    )
    config_rows = tuple(sorted({receipt["config_digest"] for receipt in receipts}))
    contract = {
        "contract_version": EXPOSURE_CONTRACT_VERSION,
        "run_id": profile.run_id,
        "window_start": profile.window_start,
        "window_end": profile.window_end,
        "receipt_fields": EXPOSURE_RECEIPT_FIELDS,
        "source_copy_ref_fields": SOURCE_COPY_REF_FIELDS,
    }
    if profile != R3_EXPOSURE_PROFILE:
        # Every other profile names itself in the contract the approval pins; the R3
        # contract keeps the exact bytes it was approved with.
        contract["exposure_profile"] = profile.name
    return {
        "issuer_contract": canonical_bytes(contract),
        "source_copy_receipt_set": canonical_bytes(source_copy_refs),
        "vendor_quota_receipt": canonical_bytes(quota_rows),
        "config": canonical_bytes(config_rows),
    }


def _execution_approval_artifact_bytes(name: str) -> bytes:
    if _DURABLE_ARTIFACT_CONTEXT is None or name not in _DURABLE_ARTIFACT_CONTEXT:
        raise ExposureRefusal("execution approval artifact is unavailable")
    return _DURABLE_ARTIFACT_CONTEXT[name]


def _v2_execution_binding(authority):
    """Resolve the origin binding the issued authority's trusted generation selects.

    The issuer's own job, principal, repository and image namespace are read from that
    binding, never from the authority's loose fields; an authority without a trusted
    generation, or one whose job, principal, image or generation digests differ from the
    binding, is refused before consumption.
    """
    generation = getattr(authority, "generation", None)
    manifest = getattr(authority, "manifest", None)
    approval = getattr(authority, "approval", None)
    registry = getattr(generation, "registry", None)
    pair = (
        getattr(generation, "origin_registry_sha256", None),
        getattr(generation, "resource_manifest_sha256", None),
    )
    if (
        type(approval) is not execution_approval.ExecutionApprovalV2
        or registry is None
        or (approval.origin_registry_sha256, approval.resource_manifest_sha256) != pair
    ):
        raise ExposureRefusal("execution authority is not issued under a trusted origin generation")
    try:
        origin = select_origin(
            manifest_version=manifest.manifest_version,
            contract_sha256=manifest.contract_sha256,
            mode="new_consume",
            registry=registry,
        )
        binding = origin.operation_bindings["collection_exposure_issue"]
    except Exception as error:
        raise ExposureRefusal("execution authority origin is not admitted") from error
    image_uri = getattr(authority, "image_uri", None)
    if (
        getattr(authority, "operation", None) != "collection_exposure_issue"
        or getattr(authority, "job_resource", None) != binding.job_resource
        or manifest.job_resource != binding.job_resource
        or manifest.service_identity != binding.service_identity
        or not isinstance(image_uri, str)
        or re.fullmatch(origin.image_uri_regex, image_uri) is None
        or manifest.image_uri != image_uri
    ):
        raise ExposureRefusal("execution authority differs from its origin binding")
    return origin, binding


def _durable_execution_mapping(authority, completed_at: datetime, binding) -> dict[str, object]:
    manifest = authority.manifest
    return {
        "execution_name": authority.execution_name,
        "job_resource": binding.job_resource,
        "image_digest": authority.image_uri.rsplit("@", 1)[1],
        "source_sha": authority.source_sha,
        "service_identity": binding.service_identity,
        "command": " ".join(manifest.command),
        "args": manifest.arguments,
        "config_digest": dict(manifest.input_artifacts)["config"],
        "started_at": authority.approval.approved_at,
        "completed_at": completed_at,
        "status": "succeeded",
    }


def _issue_durable_collection_exposure(
    *,
    receipts: tuple[Mapping[str, object], ...],
    query_runner,
    authority,
    binding,
    profile: ExposureProfile = R3_EXPOSURE_PROFILE,
) -> ExposureIssueReport:
    validated = validate_exposure_receipt_window(receipts, profile=profile)
    manifest = authority.manifest
    image_digest = authority.image_uri.rsplit("@", 1)[1]
    for receipt in validated:
        if (
            receipt["source_sha"] != manifest.source_sha
            or receipt["image_digest"] != image_digest
            or receipt["issuer_identity"] != binding.service_identity
        ):
            raise ExposureRefusal("receipt differs from durable execution authority")
    natural_keys = tuple((item["source_family"], item["exposure_date"]) for item in validated)
    predicates = " OR ".join(
        f"(source_family = {_sql_text(family)} AND exposure_date = DATE {_sql_text(day.isoformat())})"
        for family, day in natural_keys
    )
    existing = list(
        query_runner(
            f"SELECT COUNT(*) AS existing FROM `{PROJECT}.{DATASET}.{TABLE}` WHERE {predicates}"
        )
    )
    if len(existing) != 1 or int(existing[0].get("existing", -1)) != 0:
        raise ExposureRefusal("exposure natural key already exists")
    statements = ["BEGIN TRANSACTION;"]
    for receipt in validated:
        key_predicate = (
            f"source_family = {_sql_text(receipt['source_family'])} AND "
            f"exposure_date = DATE {_sql_text(receipt['exposure_date'].isoformat())}"
        )
        statements.extend(
            (
                f"ASSERT (SELECT COUNT(*) FROM `{PROJECT}.{DATASET}.{TABLE}` "
                f"WHERE {key_predicate}) = 0 AS 'exposure natural key already exists';",
                f"INSERT INTO `{PROJECT}.{DATASET}.{TABLE}` ({', '.join(EXPOSURE_RECEIPT_FIELDS)}) "
                f"VALUES ({', '.join(_sql_value(receipt[field]) for field in EXPOSURE_RECEIPT_FIELDS)});",
                "ASSERT @@row_count = 1 AS 'exposure receipt insert count changed';",
            )
        )
    statements.append("COMMIT TRANSACTION;")
    query_runner("\n".join(statements))
    readback = list(
        query_runner(
            f"SELECT {', '.join(EXPOSURE_RECEIPT_FIELDS)} FROM `{PROJECT}.{DATASET}.{TABLE}` "
            f"WHERE {predicates} ORDER BY source_family, exposure_date"
        )
    )
    readback_validated = validate_exposure_receipt_window(readback, profile=profile)
    if readback_validated != validated:
        raise ExposureRefusal("exposure receipt readback differs from inserted rows")
    inserted_natural_key_digest = canonical_digest(natural_keys)
    receipt_readback_digest = canonical_digest(readback_validated)
    execution = _durable_execution_mapping(authority, datetime.now(UTC), binding)
    proof = ExposureExecutionProof(
        execution_proof_contract_version=EXECUTION_PROOF_CONTRACT_VERSION,
        issuer_addendum_sha256=authority.approval.manifest_sha256,
        run_id=profile.run_id,
        inserted_natural_keys=natural_keys,
        inserted_natural_key_digest=inserted_natural_key_digest,
        receipt_readback_digest=receipt_readback_digest,
        **execution,
    )
    return ExposureIssueReport(
        inserted_natural_keys=natural_keys,
        inserted_natural_key_digest=inserted_natural_key_digest,
        receipt_readback_digest=receipt_readback_digest,
        execution_proof=proof,
        execution_proof_digest=canonical_digest(proof),
    )


def _execute_durable_collection_exposure(
    *,
    receipts: tuple[Mapping[str, object], ...],
    query_runner,
    authority_loader=execution_approval._load_execution_authority,
    consumption_writer=None,
    result_writer=None,
    profile: ExposureProfile = R3_EXPOSURE_PROFILE,
) -> Mapping[str, object]:
    global _DURABLE_ARTIFACT_CONTEXT
    if not isinstance(profile, ExposureProfile):
        raise ExposureRefusal("exposure profile must be named explicitly")
    if _DURABLE_ARTIFACT_CONTEXT is not None:
        raise ExposureRefusal("execution approval artifact context is already active")
    _DURABLE_ARTIFACT_CONTEXT = _build_execution_artifacts(receipts, profile=profile)
    try:
        # The reader is this module's own: when the script runs as __main__ the default reader
        # would import a second copy of it by name and find an empty artifact context there.
        authority = authority_loader(
            "collection_exposure_issue",
            mode="new_consume",
            artifact_reader=_execution_approval_artifact_bytes,
        )
        _origin, binding = _v2_execution_binding(authority)
        if consumption_writer is None:
            consumption = execution_approval._consume_execution_authority(authority)
        else:
            consumption = execution_approval._consume_execution_authority(
                authority,
                consumption_writer=consumption_writer,
            )
        try:
            report = _issue_durable_collection_exposure(
                receipts=receipts,
                query_runner=query_runner,
                authority=authority,
                binding=binding,
                profile=profile,
            )
        except Exception:
            failure_payload = {
                "error_code": "collection_exposure_issue_failed",
                "run_id": profile.run_id,
                "status": "failed",
            }
            failure_json = canonical_bytes(failure_payload).decode("utf-8")
            failure_digest = hashlib.sha256(failure_json.encode()).hexdigest()
            failure_reference = f"bq://{PROJECT}.{DATASET}.{TABLE}#{profile.run_id}:failed"
            if result_writer is None:
                execution_approval._record_execution_result(
                    authority,
                    consumption,
                    failure_reference,
                    failure_json,
                    failure_digest,
                    "failed",
                )
            else:
                execution_approval._record_execution_result(
                    authority,
                    consumption,
                    failure_reference,
                    failure_json,
                    failure_digest,
                    "failed",
                    result_writer=result_writer,
                )
            raise
        receipt_payload = asdict(report.execution_proof)
        canonical_result_json = canonical_bytes(receipt_payload).decode("utf-8")
        result_digest = hashlib.sha256(canonical_result_json.encode()).hexdigest()
        # The receipts key on family and day and the ledger records one result per reference,
        # so the reference carries the run: a second run over the same days is a new result.
        result_reference = f"bq://{PROJECT}.{DATASET}.{TABLE}#{profile.run_id}/{report.inserted_natural_key_digest}"
        if result_writer is None:
            result = execution_approval._record_execution_result(
                authority,
                consumption,
                result_reference,
                canonical_result_json,
                result_digest,
                "succeeded",
            )
        else:
            result = execution_approval._record_execution_result(
                authority,
                consumption,
                result_reference,
                canonical_result_json,
                result_digest,
                "succeeded",
                result_writer=result_writer,
            )
        return {
            **receipt_payload,
            "execution_approval": {
                "manifest_sha256": authority.approval.manifest_sha256,
                "approval_id": authority.approval.approval_id,
                "consumption_id": consumption.consumption_id,
                "result_id": result.result_id,
            },
        }
    finally:
        _DURABLE_ARTIFACT_CONTEXT = None


# The execution contract pins the container arguments to this script alone, and every
# receipt carries the issuing build's own source SHA and image digest, which a file inside
# that image cannot know. The durable invocation therefore learns its manifest digest from
# the job annotation and reads the receipt object published for that manifest; the manifest
# pins the object's content through its config, source-copy and quota artifact digests.
DURABLE_RECEIPT_BUCKET = "ogilvy-trends-v2-execution-approvals-staging"


def durable_receipt_object_name(manifest_sha256: str) -> str:
    return f"exposure/receipts/{manifest_sha256}.json"


def _durable_manifest_sha256() -> str:
    try:
        payload = execution_approval._default_execution_reader()
        value = payload["job"]["template"]["annotations"]["42.ogilvy/execution-approval-sha256"]
    except Exception as error:
        raise ExposureRefusal("durable manifest annotation is unavailable") from error
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ExposureRefusal("durable manifest annotation is invalid")
    return value


def _read_durable_receipt_bytes(manifest_sha256: str) -> bytes:
    from google.cloud import storage

    try:
        client = storage.Client(
            project=PROJECT, credentials=execution_approval._runtime_credentials()
        )
        blob = client.bucket(DURABLE_RECEIPT_BUCKET).blob(
            durable_receipt_object_name(manifest_sha256)
        )
        return blob.download_as_bytes()
    except Exception as error:
        raise ExposureRefusal("exposure receipt object is unreadable") from error


def _parse_durable_cli(argv: Sequence[str]) -> Path | None:
    values = tuple(argv)
    if not values:
        return None
    if len(values) != 2 or values[0] != "--receipt-file":
        raise ExposureRefusal("issuer CLI grammar is invalid")
    path = Path(values[1])
    if not path.is_absolute():
        raise ExposureRefusal("issuer CLI paths must be absolute")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    receipt_path = _parse_durable_cli(values)
    if receipt_path is None:
        # The durable object names its profile, or is a bare list, which is the R3 artifact.
        profile, receipts = _parse_profiled_receipt_artifact(
            _read_durable_receipt_bytes(_durable_manifest_sha256())
        )
    else:
        profile, receipts = R3_EXPOSURE_PROFILE, _load_receipt_artifact(receipt_path)
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT, location="US")

    def query_runner(sql: str):
        return [
            dict(row)
            for row in client.query(sql, retry=None, job_retry=None).result(
                retry=None,
                job_retry=None,
            )
        ]

    payload = _execute_durable_collection_exposure(
        receipts=receipts,
        query_runner=query_runner,
        profile=profile,
    )
    sys.stdout.write(canonical_bytes(payload).decode("utf-8") + "\n")
    return 0


def run_executable(
    argv: Sequence[str] | None = None,
    *,
    runner=None,
    stderr=None,
) -> int:
    values = tuple(sys.argv[1:] if argv is None else argv)
    boundary = main if runner is None else runner
    error_output = sys.stderr if stderr is None else stderr
    try:
        return boundary(values)
    except ExposureRefusal as error:
        payload = {"error": str(error)}
    except execution_approval.ApprovalRefusal as error:
        # The refusal code is the operator's one diagnostic; it carries no secret.
        payload = {"error": "issuer_internal_refusal", "refusal": str(error)}
    except Exception as error:
        payload = {"error": "issuer_internal_refusal", "refusal": type(error).__name__}
    error_output.write(canonical_bytes(payload).decode("utf-8") + "\n")
    return 1


__all__ = [
    "DAILY_EXPOSURE_PROFILE_NAME",
    "DAILY_EXPOSURE_WINDOW_DAYS",
    "EXECUTION_PROOF_CONTRACT_VERSION",
    "EXPOSURE_RECEIPT_FIELDS",
    "R3_EXPOSURE_PROFILE",
    "SOURCE_COPY_REF_FIELDS",
    "ExposureExecutionProof",
    "ExposureIssueReport",
    "ExposureProfile",
    "ExposureRefusal",
    "daily_exposure_profile",
    "exposure_receipt_digest",
    "main",
    "run_executable",
    "validate_exposure_receipt",
    "validate_exposure_receipt_set",
    "validate_exposure_receipt_window",
]


if __name__ == "__main__":
    raise SystemExit(run_executable(sys.argv[1:]))
