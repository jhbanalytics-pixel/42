"""Server-owned immutable approval companions for Gemini canary plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol

from src.analysis.open_intelligence.canary_manifests import (
    CANONICAL_MANIFEST_SHA256,
    stage_manifest,
)

APPROVAL_CONTRACT_VERSION = "gemini_canary_plan_approval_v1"
COMPANION_VERSION = "gemini_canary_approval_companion_v1"
TASK_IDS = (
    "golden_01_emerging_without_keyword",
    "golden_02_why_moving",
    "golden_03_cross_market_difference",
    "golden_04_carriers",
    "golden_05_history",
    "golden_06_brand_role",
    "golden_07_audience_lens",
    "golden_08_source_agreement",
    "golden_09_source_gap",
    "golden_10_custom",
    "golden_11_election_brand_role",
)
PLAN_IDS = (
    "ep_fixture_01",
    "ep_fixture_02",
    "ep_fixture_03",
    "ep_fixture_04",
    "ep_fixture_05",
    "ep_fixture_06",
    "ep_fixture_07",
    "ep_fixture_08",
    "ep_fixture_09",
    "ep_fixture_0a",
    "ep_fixture_0b",
)
CLAIM_REQUIREMENT_FIELDS = (
    "claim_id",
    "question",
    "claim_type",
    "assertion_kind",
    "required_source_families",
    "minimum_evidence_state",
    "allow_inferred_audience",
    "human_review_required",
)
APPROVAL_RECORD_FIELDS = (
    "contract_version",
    "manifest_sha256",
    "task_id",
    "stage",
    "plan_id",
    "plan_digest",
    "claim_requirements",
    "approved_by",
    "approved_at",
    "approval_id",
)
APPROVAL_PREIMAGE_FIELDS = APPROVAL_RECORD_FIELDS[:-1]
COMPANION_FIELDS = (
    "companion_version",
    "contract_version",
    "manifest_sha256",
    "task_count",
    "task_ids",
    "approval_records",
    "companion_sha256",
)
APPROVAL_PHRASE = (
    "I approve the server to create, at the UTC time this approval action is received, "
    "the eleven immutable Gemini canary answering-plan approval records for caller manifest "
    "SHA256 0f90586306fae9932b88424a16699182e4fc3ecbda47015a63bb3491dbb833c7 "
    "and task IDs golden_01_emerging_without_keyword, golden_02_why_moving, "
    "golden_03_cross_market_difference, golden_04_carriers, golden_05_history, "
    "golden_06_brand_role, golden_07_audience_lens, golden_08_source_agreement, "
    "golden_09_source_gap, golden_10_custom, and golden_11_election_brand_role. "
    "This approval does not backdate the current turn and does not approve a model call, "
    "deployment, production use, or any task outside this manifest."
)


class CanaryApprovalFailure(ValueError):
    pass


def _canonical_sorted(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_ordered(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_string(value: object) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise CanaryApprovalFailure("canary_approval_timestamp_source_invalid")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CanaryApprovalFailure(f"{field}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ApprovalActionRequest:
    manifest_sha256: str
    task_ids: tuple[str, ...]
    approval_phrase: str


@dataclass(frozen=True, slots=True)
class AuthenticatedServerActor:
    stable_user_id: str
    source: str


@dataclass(frozen=True, slots=True)
class CanaryPlanApprovalRecord:
    contract_version: str
    manifest_sha256: str
    task_id: str
    stage: str
    plan_id: str
    plan_digest: str
    claim_requirements: tuple[Mapping[str, object], ...]
    approved_by: str
    approved_at: str
    approval_id: str


@dataclass(frozen=True, slots=True)
class CanaryApprovalCompanion:
    companion_version: str
    contract_version: str
    manifest_sha256: str
    task_count: int
    task_ids: tuple[str, ...]
    approval_records: tuple[CanaryPlanApprovalRecord, ...]
    companion_sha256: str


@dataclass(frozen=True, slots=True)
class StoredCompanion:
    canonical_companion_json: str
    companion_sha256: str


@dataclass(frozen=True, slots=True)
class ReadbackApprovedPlan:
    record: CanaryPlanApprovalRecord
    companion_sha256: str
    plan: Mapping[str, object]

    @property
    def plan_id(self) -> str:
        return self.record.plan_id

    @property
    def plan_digest(self) -> str:
        return self.record.plan_digest

    @property
    def approved_by(self) -> str:
        return self.record.approved_by

    @property
    def approved_at(self) -> str:
        return self.record.approved_at

    @property
    def approval_id(self) -> str:
        return self.record.approval_id

    @property
    def approved_claim_ids(self) -> tuple[str, ...]:
        return tuple(item["claim_id"] for item in self.record.claim_requirements)


class CanaryPlanStore(Protocol):
    def load(self, task_ids: Sequence[str]) -> tuple[tuple[str, Mapping[str, object]], ...]: ...


class CanaryPlanApprovalStore(Protocol):
    def append_batch(self, canonical_companion_json: str, companion_sha256: str) -> None: ...
    def read_manifest(self, manifest_sha256: str) -> StoredCompanion: ...
    def read_approval(self, approval_id: str) -> CanaryPlanApprovalRecord: ...


class FixtureCanaryPlanStore:
    def __init__(self, plans: Mapping[str, Mapping[str, object]]) -> None:
        self._plans = deepcopy(dict(plans))

    def load(self, task_ids: Sequence[str]) -> tuple[tuple[str, Mapping[str, object]], ...]:
        output = []
        for task_id in task_ids:
            if task_id not in self._plans:
                raise CanaryApprovalFailure("canary_approval_planning_output_missing")
            output.append((task_id, deepcopy(self._plans[task_id])))
        return tuple(output)


def _record_payload(record: CanaryPlanApprovalRecord, *, include_id: bool) -> dict[str, object]:
    fields = APPROVAL_RECORD_FIELDS if include_id else APPROVAL_PREIMAGE_FIELDS
    return {field: getattr(record, field) for field in fields}


def _record_id(record: CanaryPlanApprovalRecord) -> str:
    return "apr_" + _sha(_canonical_ordered(_record_payload(record, include_id=False)))


def _companion_payload(
    companion: CanaryApprovalCompanion, *, include_digest: bool
) -> dict[str, object]:
    output = {
        "companion_version": companion.companion_version,
        "contract_version": companion.contract_version,
        "manifest_sha256": companion.manifest_sha256,
        "task_count": companion.task_count,
        "task_ids": companion.task_ids,
        "approval_records": tuple(
            _record_payload(item, include_id=True) for item in companion.approval_records
        ),
    }
    if include_digest:
        output["companion_sha256"] = companion.companion_sha256
    return output


def _companion_digest(companion: CanaryApprovalCompanion) -> str:
    return _sha(_canonical_ordered(_companion_payload(companion, include_digest=False)))


def canonical_companion_json(companion: CanaryApprovalCompanion) -> str:
    return _canonical_ordered(_companion_payload(companion, include_digest=True))


def _normalized_requirements(plan: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    items = plan.get("claim_requirements")
    if not isinstance(items, list) or not items:
        raise CanaryApprovalFailure("canary_approval_claim_requirements_missing")
    output = []
    seen = set()
    for item in items:
        if not isinstance(item, Mapping) or tuple(item) != CLAIM_REQUIREMENT_FIELDS:
            raise CanaryApprovalFailure("canary_approval_plan_schema_invalid")
        normalized = {field: deepcopy(item[field]) for field in CLAIM_REQUIREMENT_FIELDS}
        claim_id = normalized["claim_id"]
        if not isinstance(claim_id, str) or claim_id in seen:
            raise CanaryApprovalFailure("canary_approval_claim_requirements_missing")
        seen.add(claim_id)
        output.append(normalized)
    return tuple(output)


def _validate_plan(task_id: str, plan: Mapping[str, object]) -> None:
    from src.analysis.open_intelligence.canary_runtime import validate_response_schema
    from src.analysis.open_intelligence.open_question_answer import _validate_plan as validate_plan
    from src.analysis.prompts.open_question_planning import RESPONSE_SCHEMA

    try:
        validate_response_schema(plan, RESPONSE_SCHEMA)
        envelope = stage_manifest(f"open_question_answer:planning:{task_id}")["envelope"]
        validate_plan(plan, envelope)
    except Exception as error:
        raise CanaryApprovalFailure("canary_approval_plan_schema_invalid") from error


def _plan_digest(plan: Mapping[str, object]) -> str:
    return _sha(_canonical_sorted(plan))


def _validate_actor(value: object) -> str:
    if not isinstance(value, AuthenticatedServerActor):
        raise CanaryApprovalFailure("canary_approval_actor_unavailable")
    if (
        value.source != "authenticated_application_user_id"
        or not isinstance(value.stable_user_id, str)
        or not value.stable_user_id
        or "@" in value.stable_user_id
        or any(character.isspace() for character in value.stable_user_id)
    ):
        raise CanaryApprovalFailure("canary_approval_actor_source_invalid")
    return value.stable_user_id


def _build_record(
    *,
    task_id: str,
    plan_id: str,
    plan: Mapping[str, object],
    approved_by: str,
    approved_at: str,
) -> CanaryPlanApprovalRecord:
    provisional = CanaryPlanApprovalRecord(
        contract_version=APPROVAL_CONTRACT_VERSION,
        manifest_sha256=CANONICAL_MANIFEST_SHA256,
        task_id=task_id,
        stage="answering",
        plan_id=plan_id,
        plan_digest=_plan_digest(plan),
        claim_requirements=_normalized_requirements(plan),
        approved_by=approved_by,
        approved_at=approved_at,
        approval_id="",
    )
    return CanaryPlanApprovalRecord(
        **{**asdict(provisional), "approval_id": _record_id(provisional)}
    )


def _build_companion(records: tuple[CanaryPlanApprovalRecord, ...]) -> CanaryApprovalCompanion:
    provisional = CanaryApprovalCompanion(
        companion_version=COMPANION_VERSION,
        contract_version=APPROVAL_CONTRACT_VERSION,
        manifest_sha256=CANONICAL_MANIFEST_SHA256,
        task_count=len(TASK_IDS),
        task_ids=TASK_IDS,
        approval_records=records,
        companion_sha256="",
    )
    return CanaryApprovalCompanion(
        companion_version=COMPANION_VERSION,
        contract_version=APPROVAL_CONTRACT_VERSION,
        manifest_sha256=CANONICAL_MANIFEST_SHA256,
        task_count=len(TASK_IDS),
        task_ids=TASK_IDS,
        approval_records=records,
        companion_sha256=_companion_digest(provisional),
    )


def _parse_approval_records(payload: Mapping[str, object]) -> tuple[CanaryPlanApprovalRecord, ...]:
    records = []
    for item in payload["approval_records"]:
        if not isinstance(item, dict) or tuple(item) != APPROVAL_RECORD_FIELDS:
            raise CanaryApprovalFailure("canary_approval_companion_digest_mismatch")
        item = dict(item)
        item["claim_requirements"] = tuple(item["claim_requirements"])
        record = CanaryPlanApprovalRecord(**item)
        if record.approval_id != _record_id(record):
            raise CanaryApprovalFailure("canary_approval_id_mismatch")
        records.append(record)
    return tuple(records)


def _validate_companion_timestamps(records: tuple[CanaryPlanApprovalRecord, ...]) -> None:
    try:
        for timestamp in {item.approved_at for item in records}:
            datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError) as error:
        raise CanaryApprovalFailure("canary_approval_timestamp_source_invalid") from error


def _validate_companion(
    companion: CanaryApprovalCompanion,
    records: tuple[CanaryPlanApprovalRecord, ...],
) -> None:
    actors = {item.approved_by for item in records}
    timestamps = {item.approved_at for item in records}
    if (
        companion.companion_version != COMPANION_VERSION
        or companion.contract_version != APPROVAL_CONTRACT_VERSION
        or len(actors) != 1
        or len(timestamps) != 1
        or not all(
            item.contract_version == companion.contract_version
            and item.manifest_sha256 == companion.manifest_sha256
            and item.stage == "answering"
            and item.approved_by
            and "@" not in item.approved_by
            and item.plan_digest == _require_digest(item.plan_digest, "plan_digest")
            for item in records
        )
        or companion.task_count != 11
        or companion.task_ids != TASK_IDS
        or tuple(item.task_id for item in records) != TASK_IDS
        or tuple(item.plan_id for item in records) != PLAN_IDS
        or len({item.approval_id for item in records}) != 11
        or companion.companion_sha256 != _companion_digest(companion)
    ):
        raise CanaryApprovalFailure("canary_approval_companion_digest_mismatch")


def _parse_companion(value: str) -> CanaryApprovalCompanion:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise CanaryApprovalFailure("canary_approval_companion_digest_mismatch") from error
    if not isinstance(payload, dict) or tuple(payload) != COMPANION_FIELDS:
        raise CanaryApprovalFailure("canary_approval_companion_digest_mismatch")
    records = _parse_approval_records(payload)
    companion = CanaryApprovalCompanion(
        companion_version=payload["companion_version"],
        contract_version=payload["contract_version"],
        manifest_sha256=payload["manifest_sha256"],
        task_count=payload["task_count"],
        task_ids=tuple(payload["task_ids"]),
        approval_records=records,
        companion_sha256=payload["companion_sha256"],
    )
    _validate_companion_timestamps(records)
    _validate_companion(companion, records)
    return companion


class MemoryCanaryPlanApprovalStore:
    def __init__(self) -> None:
        self._companions: dict[str, StoredCompanion] = {}
        self._approvals: dict[str, CanaryPlanApprovalRecord] = {}

    @property
    def count(self) -> int:
        return len(self._companions)

    def append_batch(self, canonical_companion_json: str, companion_sha256: str) -> None:
        companion = _parse_companion(canonical_companion_json)
        if companion.companion_sha256 != companion_sha256:
            raise CanaryApprovalFailure("canary_approval_companion_digest_mismatch")
        if companion.manifest_sha256 in self._companions or any(
            item.approval_id in self._approvals for item in companion.approval_records
        ):
            raise CanaryApprovalFailure("canary_approval_conflict")
        companions = dict(self._companions)
        approvals = dict(self._approvals)
        companions[companion.manifest_sha256] = StoredCompanion(
            canonical_companion_json, companion_sha256
        )
        approvals.update({item.approval_id: item for item in companion.approval_records})
        self._companions = companions
        self._approvals = approvals

    def read_manifest(self, manifest_sha256: str) -> StoredCompanion:
        try:
            return self._companions[manifest_sha256]
        except KeyError:
            raise CanaryApprovalFailure("canary_answer_plan_unapproved") from None

    def read_approval(self, approval_id: str) -> CanaryPlanApprovalRecord:
        try:
            return self._approvals[approval_id]
        except KeyError:
            raise CanaryApprovalFailure("canary_answer_plan_unapproved") from None


def create_approval_companion(
    action_request: ApprovalActionRequest,
    *,
    plan_store: CanaryPlanStore,
    approval_store: CanaryPlanApprovalStore,
    resolve_actor: Callable[[], AuthenticatedServerActor],
    utc_now: Callable[[], datetime],
) -> CanaryApprovalCompanion:
    if not isinstance(action_request, ApprovalActionRequest):
        raise CanaryApprovalFailure("canary_approval_batch_scope_mismatch")
    if action_request.manifest_sha256 != CANONICAL_MANIFEST_SHA256:
        raise CanaryApprovalFailure("canary_approval_manifest_mismatch")
    if action_request.task_ids != TASK_IDS:
        raise CanaryApprovalFailure("canary_approval_batch_scope_mismatch")
    if action_request.approval_phrase != APPROVAL_PHRASE:
        raise CanaryApprovalFailure("canary_approval_phrase_mismatch")
    loaded = plan_store.load(TASK_IDS)
    if tuple(task_id for task_id, _plan in loaded) != TASK_IDS or len(loaded) != 11:
        raise CanaryApprovalFailure("canary_approval_batch_scope_mismatch")
    plans = []
    for ordinal, (task_id, plan) in enumerate(loaded):
        _validate_plan(task_id, plan)
        if plan.get("plan_id") != PLAN_IDS[ordinal]:
            raise CanaryApprovalFailure("canary_approval_plan_id_mismatch")
        plans.append((task_id, deepcopy(dict(plan))))
    approved_by = _validate_actor(resolve_actor())
    approved_at = _utc_string(utc_now())
    records = tuple(
        _build_record(
            task_id=task_id,
            plan_id=PLAN_IDS[ordinal],
            plan=plan,
            approved_by=approved_by,
            approved_at=approved_at,
        )
        for ordinal, (task_id, plan) in enumerate(plans)
    )
    companion = _build_companion(records)
    canonical = canonical_companion_json(companion)
    approval_store.append_batch(canonical, companion.companion_sha256)
    stored = approval_store.read_manifest(CANONICAL_MANIFEST_SHA256)
    if (
        stored.canonical_companion_json != canonical
        or stored.companion_sha256 != companion.companion_sha256
        or _parse_companion(stored.canonical_companion_json) != companion
        or any(approval_store.read_approval(item.approval_id) != item for item in records)
    ):
        raise CanaryApprovalFailure("canary_approval_storage_readback_mismatch")
    return companion


def load_approved_plan(
    *,
    manifest_sha256: str,
    task_id: str,
    plan_store: CanaryPlanStore,
    approval_store: CanaryPlanApprovalStore,
) -> ReadbackApprovedPlan:
    if manifest_sha256 != CANONICAL_MANIFEST_SHA256 or task_id not in TASK_IDS:
        raise CanaryApprovalFailure("canary_answer_plan_unapproved")
    stored = approval_store.read_manifest(manifest_sha256)
    try:
        companion = _parse_companion(stored.canonical_companion_json)
    except CanaryApprovalFailure as error:
        raise CanaryApprovalFailure("canary_approval_storage_readback_mismatch") from error
    if stored.companion_sha256 != companion.companion_sha256:
        raise CanaryApprovalFailure("canary_approval_storage_readback_mismatch")
    matches = [item for item in companion.approval_records if item.task_id == task_id]
    loaded = plan_store.load((task_id,))
    if len(matches) != 1 or len(loaded) != 1 or loaded[0][0] != task_id:
        raise CanaryApprovalFailure("canary_answer_plan_unapproved")
    record = matches[0]
    plan = loaded[0][1]
    _validate_plan(task_id, plan)
    if record.plan_digest != _plan_digest(plan):
        raise CanaryApprovalFailure("canary_approval_plan_digest_mismatch")
    if record.claim_requirements != _normalized_requirements(plan):
        raise CanaryApprovalFailure("canary_approval_claim_requirements_missing")
    if approval_store.read_approval(record.approval_id) != record:
        raise CanaryApprovalFailure("canary_approval_storage_readback_mismatch")
    return ReadbackApprovedPlan(record, companion.companion_sha256, deepcopy(dict(plan)))


__all__ = [
    "APPROVAL_CONTRACT_VERSION",
    "APPROVAL_PHRASE",
    "COMPANION_VERSION",
    "PLAN_IDS",
    "TASK_IDS",
    "ApprovalActionRequest",
    "AuthenticatedServerActor",
    "CanaryApprovalCompanion",
    "CanaryApprovalFailure",
    "CanaryPlanApprovalRecord",
    "CanaryPlanApprovalStore",
    "CanaryPlanStore",
    "FixtureCanaryPlanStore",
    "MemoryCanaryPlanApprovalStore",
    "ReadbackApprovedPlan",
    "StoredCompanion",
    "canonical_companion_json",
    "create_approval_companion",
    "load_approved_plan",
]
