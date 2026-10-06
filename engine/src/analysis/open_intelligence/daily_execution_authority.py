"""Native-admitted capabilities for daily execution consumers."""

from __future__ import annotations

import hashlib
import re
import weakref
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import ClassVar, Protocol

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cycle import STEP_NUMBERS_V2
from src.analysis.open_intelligence.daily_execution_contracts import (
    CONSUMPTION_ID,
    DERIVATION_ID,
    DailyContractError,
    canonical_json_object,
    input_artifact_digests,
    sha256_bytes,
    validate_artifact_set,
    validate_capture_semantics,
    validate_execution_observation,
    validate_observation_time_bounds,
    validate_operation_context,
    validate_reconciliation,
    validate_result_metering,
)


class DailyAuthorityUnavailable(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DailyAuthorityIntegrityError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DailyAuthorityReadClients(Protocol):
    def read_derivation(self, derivation_id: str) -> Mapping[str, object]: ...

    def read_execution_observation(
        self, execution_name: str
    ) -> tuple[bytes, Mapping[str, object]]: ...

    def read_chain(
        self, derivation_id: str, canonical_observation_json: str, observation_digest: str
    ) -> Mapping[str, object]: ...

    def read_native_execution(self, execution_name: str) -> Mapping[str, object]: ...


class DailyAuthorityWriteClients(Protocol):
    def read_native_job(self, job_resource: str) -> Mapping[str, object]: ...

    def read_native_execution(self, execution_name: str) -> Mapping[str, object]: ...

    def admit_execution_observation(
        self, execution_name: str, canonical_bytes: bytes, digest: str
    ) -> tuple[bytes, Mapping[str, object]]: ...

    def read_provider_operation(self, operation_name: str) -> Mapping[str, object]: ...

    def derive_execution(self, *parameters: object) -> Mapping[str, object]: ...

    def select_derivation(self, *parameters: object) -> Mapping[str, object]: ...

    def consume_derivation(self, *parameters: object) -> Mapping[str, object]: ...

    def cancel_derivation(self, *parameters: object) -> Mapping[str, object]: ...

    def record_result(self, *parameters: object) -> Mapping[str, object]: ...


class RoutineTransport(Protocol):
    def call(
        self, routine: str, parameters: tuple[object, ...]
    ) -> tuple[Mapping[str, object], int]: ...


class ObjectTransport(Protocol):
    def read(self, object_name: str, maximum_bytes: int) -> tuple[bytes, Mapping[str, object]]: ...

    def write_once(self, object_name: str, body: bytes) -> None: ...


class NativeExecutionTransport(Protocol):
    def read(self, execution_name: str) -> Mapping[str, object]: ...

    def read_job(self, job_resource: str) -> Mapping[str, object]: ...

    def read_operation(self, operation_name: str) -> Mapping[str, object]: ...


class DailyAuthorityBudget:
    __slots__ = (
        "billed_bytes",
        "native_reads",
        "object_bytes",
        "object_reads",
        "object_write_bytes",
        "object_writes",
        "queries",
    )

    def __init__(
        self,
        *,
        queries: int,
        billed_bytes: int,
        object_reads: int,
        object_bytes: int,
        native_reads: int,
        object_writes: int = 0,
        object_write_bytes: int = 0,
    ):
        values = (
            queries,
            billed_bytes,
            object_reads,
            object_bytes,
            native_reads,
            object_writes,
            object_write_bytes,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise DailyAuthorityIntegrityError("daily_budget_invalid")
        self.queries = queries
        self.billed_bytes = billed_bytes
        self.object_reads = object_reads
        self.object_bytes = object_bytes
        self.native_reads = native_reads
        self.object_writes = object_writes
        self.object_write_bytes = object_write_bytes

    def reserve_observation(self, size: int) -> None:
        if (
            type(size) is not int
            or size < 0
            or self.object_reads < 1
            or self.object_bytes < size
            or self.object_writes < 1
            or self.object_write_bytes < size
        ):
            raise DailyAuthorityUnavailable("daily_object_budget_exhausted")
        self.object_reads -= 1
        self.object_bytes -= size
        self.object_writes -= 1
        self.object_write_bytes -= size

    def begin_query(self) -> None:
        if self.queries < 1:
            raise DailyAuthorityUnavailable("daily_query_budget_exhausted")
        self.queries -= 1

    def debit_query(self, billed_bytes: int) -> None:
        if type(billed_bytes) is not int or billed_bytes < 0:
            raise DailyAuthorityUnavailable("daily_query_metering_unavailable")
        if self.billed_bytes < billed_bytes:
            raise DailyAuthorityUnavailable("daily_query_budget_exhausted")
        self.billed_bytes -= billed_bytes

    def debit_object(self, size: int) -> None:
        if type(size) is not int or size < 0 or self.object_reads < 1 or self.object_bytes < size:
            raise DailyAuthorityUnavailable("daily_object_budget_exhausted")
        self.object_reads -= 1
        self.object_bytes -= size

    def debit_native(self) -> None:
        if self.native_reads < 1:
            raise DailyAuthorityUnavailable("daily_native_budget_exhausted")
        self.native_reads -= 1


class NativeDailyAuthorityReadAdapter:
    __slots__ = ("_budget", "_native", "_objects", "_routines")

    def __init__(
        self,
        *,
        routines: RoutineTransport,
        objects: ObjectTransport,
        native: NativeExecutionTransport,
        budget: DailyAuthorityBudget,
    ):
        self._routines = routines
        self._objects = objects
        self._native = native
        self._budget = budget

    def _call(self, name: str, parameters: tuple[object, ...]) -> Mapping[str, object]:
        self._budget.begin_query()
        try:
            value, billed = self._routines.call(name, parameters)
        except Exception as error:
            raise DailyAuthorityUnavailable("daily_query_unavailable") from error
        self._budget.debit_query(billed)
        return _mapping(value, "daily_query_shape_invalid")

    def read_derivation(self, derivation_id: str) -> Mapping[str, object]:
        return self._call("sp_read_open_intelligence_daily_derivation_v1", (derivation_id,))

    def read_execution_observation(self, execution_name: str) -> tuple[bytes, Mapping[str, object]]:
        object_name = (
            "42/daily/execution-observations/"
            + hashlib.sha256(execution_name.encode("utf-8")).hexdigest()
            + ".json"
        )
        if self._budget.object_reads < 1:
            raise DailyAuthorityUnavailable("daily_object_budget_exhausted")
        try:
            raw, metadata = self._objects.read(object_name, self._budget.object_bytes)
        except DailyAuthorityIntegrityError:
            raise
        except DailyAuthorityUnavailable:
            raise
        except Exception as error:
            raise DailyAuthorityUnavailable("daily_observation_unavailable") from error
        if not isinstance(raw, bytes):
            raise DailyAuthorityIntegrityError("daily_observation_storage_invalid")
        self._budget.debit_object(len(raw))
        return raw, _mapping(metadata, "daily_observation_storage_invalid")

    def read_chain(
        self, derivation_id: str, canonical_observation_json: str, observation_digest: str
    ) -> Mapping[str, object]:
        row = dict(
            self._call(
                "sp_read_open_intelligence_daily_chain_v1",
                (derivation_id, canonical_observation_json, observation_digest),
            )
        )
        raw = row.pop("canonical_operation_payload_json", None)
        if "operation_payload" in row:
            raise DailyAuthorityIntegrityError("daily_chain_fields_invalid")
        try:
            row["operation_payload"], _ = canonical_json_object(raw, "daily_payload_noncanonical")
        except (TypeError, ValueError) as error:
            raise _integrity(error) from error
        return row

    def read_native_execution(self, execution_name: str) -> Mapping[str, object]:
        self._budget.debit_native()
        try:
            return _mapping(self._native.read(execution_name), "daily_native_execution_invalid")
        except DailyAuthorityIntegrityError:
            raise
        except Exception as error:
            raise DailyAuthorityUnavailable("daily_native_execution_unavailable") from error


class NativeDailyAuthorityWriteAdapter:
    __slots__ = ("_budget", "_native", "_objects", "_routines")

    _ROUTINES: ClassVar[dict[str, str]] = {
        "derive_execution": "sp_derive_open_intelligence_daily_execution_v1",
        "select_derivation": "sp_select_open_intelligence_daily_derivation_v1",
        "consume_derivation": "sp_consume_open_intelligence_daily_derivation_v1",
        "cancel_derivation": "sp_cancel_open_intelligence_daily_derivation_v1",
        "record_result": "sp_record_open_intelligence_daily_result_v1",
    }

    def __init__(
        self,
        *,
        routines: RoutineTransport,
        objects: ObjectTransport,
        native: NativeExecutionTransport,
        budget: DailyAuthorityBudget,
    ):
        self._routines = routines
        self._objects = objects
        self._native = native
        self._budget = budget

    def read_native_job(self, job_resource: str) -> Mapping[str, object]:
        self._budget.debit_native()
        try:
            return _mapping(self._native.read_job(job_resource), "daily_native_job_invalid")
        except DailyAuthorityIntegrityError:
            raise
        except Exception as error:
            raise DailyAuthorityUnavailable("daily_native_job_unavailable") from error

    def read_native_execution(self, execution_name: str) -> Mapping[str, object]:
        self._budget.debit_native()
        try:
            return _mapping(self._native.read(execution_name), "daily_native_execution_invalid")
        except DailyAuthorityIntegrityError:
            raise
        except Exception as error:
            raise DailyAuthorityUnavailable("daily_native_execution_unavailable") from error

    def admit_execution_observation(
        self, execution_name: str, canonical_bytes: bytes, digest: str
    ) -> tuple[bytes, Mapping[str, object]]:
        object_name = (
            "42/daily/execution-observations/"
            + hashlib.sha256(execution_name.encode("utf-8")).hexdigest()
            + ".json"
        )
        if not isinstance(canonical_bytes, bytes) or sha256_bytes(canonical_bytes) != digest:
            raise DailyAuthorityIntegrityError("daily_observation_storage_invalid")
        self._budget.reserve_observation(len(canonical_bytes))
        with suppress(FileExistsError):
            self._objects.write_once(object_name, canonical_bytes)
        try:
            raw, metadata = self._objects.read(object_name, len(canonical_bytes))
        except DailyAuthorityIntegrityError:
            raise
        except Exception as error:
            raise DailyAuthorityUnavailable("daily_observation_unavailable") from error
        if not isinstance(raw, bytes):
            raise DailyAuthorityIntegrityError("daily_observation_storage_invalid")
        if raw != canonical_bytes or sha256_bytes(raw) != digest:
            raise DailyAuthorityIntegrityError("daily_observation_storage_invalid")
        return raw, _mapping(metadata, "daily_observation_storage_invalid")

    def read_provider_operation(self, operation_name: str) -> Mapping[str, object]:
        self._budget.debit_native()
        try:
            return _mapping(
                self._native.read_operation(operation_name),
                "daily_provider_operation_invalid",
            )
        except DailyAuthorityIntegrityError:
            raise
        except Exception as error:
            raise DailyAuthorityUnavailable("daily_provider_operation_unavailable") from error

    def _call(self, operation: str, parameters: tuple[object, ...]) -> Mapping[str, object]:
        self._budget.begin_query()
        try:
            value, billed = self._routines.call(self._ROUTINES[operation], parameters)
        except Exception as error:
            raise DailyAuthorityUnavailable("daily_query_unavailable") from error
        self._budget.debit_query(billed)
        return _mapping(value, "daily_query_shape_invalid")

    def derive_execution(self, *parameters: object) -> Mapping[str, object]:
        return self._call("derive_execution", parameters)

    def select_derivation(self, *parameters: object) -> Mapping[str, object]:
        return self._call("select_derivation", parameters)

    def consume_derivation(self, *parameters: object) -> Mapping[str, object]:
        return self._call("consume_derivation", parameters)

    def cancel_derivation(self, *parameters: object) -> Mapping[str, object]:
        return self._call("cancel_derivation", parameters)

    def record_result(self, *parameters: object) -> Mapping[str, object]:
        row = self._call("record_result", parameters)
        if set(row) != {"result"}:
            raise DailyAuthorityIntegrityError("daily_result_readback_mismatch")
        return _mapping(row["result"], "daily_result_invalid")


_SEAL = object()
_ISSUED_CONSUMPTIONS = weakref.WeakSet()
_ISSUED_CHAINS = weakref.WeakSet()


def _utc_now():
    return datetime.now(UTC)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DailyAuthorityIntegrityError(code)
    return value


_RESULT_ENVELOPE_FIELDS = frozenset(
    {
        "authorizing_approval_id",
        "authorizing_grant_digest",
        "business_attempt_id",
        "child_job_resource",
        "completed_at",
        "consumption_id",
        "contract_version",
        "derivation_id",
        "effect_state",
        "execution_name",
        "execution_observation_sha256",
        "manifest_sha256",
        "operation",
        "operation_context_sha256",
        "operation_payload",
        "payload_contract_version",
        "payload_digest",
        "result_reference",
        "spend_state",
        "stage_metering",
        "terminal_state",
    }
)


def _result_envelope(row: Mapping[str, object]) -> dict[str, object]:
    raw = row.get("canonical_result_json")
    if not isinstance(raw, str):
        raise DailyAuthorityIntegrityError("daily_result_invalid")
    try:
        envelope, encoded = canonical_json_object(raw, "daily_result_noncanonical")
        if set(envelope) != _RESULT_ENVELOPE_FIELDS:
            raise DailyAuthorityIntegrityError("daily_result_fields_invalid")
        validate_result_metering(
            envelope["stage_metering"],
            terminal_state=envelope["terminal_state"],
            effect_state=envelope["effect_state"],
            spend_state=envelope["spend_state"],
        )
    except (TypeError, ValueError) as error:
        raise _integrity(error) from error
    digest = sha256_bytes(encoded)
    if (
        envelope["contract_version"] != "daily_execution_result_v1"
        or row.get("result_contract_version") != "open_intelligence_execution_result_v3"
        or row.get("result_digest") != digest
        or row.get("result_id") != "exr_" + digest
    ):
        raise DailyAuthorityIntegrityError("daily_result_digest_invalid")
    payload = _mapping(envelope["operation_payload"], "daily_operation_payload_invalid")
    if (
        sha256_bytes(canonical_bytes(payload)) != envelope["payload_digest"]
        or payload.get("contract_version") != envelope["payload_contract_version"]
    ):
        raise DailyAuthorityIntegrityError("daily_payload_digest_invalid")
    physical = {
        "approval_id": envelope["derivation_id"],
        "consumption_id": envelope["consumption_id"],
        "execution_name": envelope["execution_name"],
        "manifest_sha256": envelope["manifest_sha256"],
        "operation": envelope["operation"],
        "result_reference": envelope["result_reference"],
        "status": envelope["terminal_state"],
        "completed_at": envelope["completed_at"],
    }
    if any(row.get(name) != value for name, value in physical.items()):
        raise DailyAuthorityIntegrityError("daily_result_row_mismatch")
    return envelope


def native_instant_key(value: object) -> tuple[int, Decimal]:
    if not isinstance(value, str):
        raise DailyAuthorityIntegrityError("daily_native_terminal_mismatch")
    match = re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d+))?(?:Z|\+00:00)", value)
    if match is None:
        raise DailyAuthorityIntegrityError("daily_native_terminal_mismatch")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise DailyAuthorityIntegrityError("daily_native_terminal_mismatch") from error
    seconds = stamp.toordinal() * 86400 + stamp.hour * 3600 + stamp.minute * 60 + stamp.second
    return seconds, Decimal("0." + (match.group(1) or "0"))


def _validate_observation_storage(
    storage: object, execution_name: str, raw: bytes, digest: str
) -> Mapping[str, object]:
    value = _mapping(storage, "daily_observation_storage_invalid")
    expected_object = (
        "42/daily/execution-observations/"
        + hashlib.sha256(execution_name.encode("utf-8")).hexdigest()
        + ".json"
    )
    if (
        set(value) != {"content_sha256", "created_at", "generation", "object_name", "size_bytes"}
        or value.get("object_name") != expected_object
        or not isinstance(value.get("generation"), str)
        or not value["generation"].isdigit()
        or int(value["generation"]) <= 0
        or value.get("content_sha256") != digest
        or value.get("size_bytes") != len(raw)
    ):
        raise DailyAuthorityIntegrityError("daily_observation_storage_invalid")
    try:
        observation, _raw = validate_execution_observation(raw)
        validate_observation_time_bounds(
            observation, stored_at=value["created_at"], read_at=_utc_now()
        )
    except DailyContractError as error:
        raise _integrity(error) from error
    return value


class AdmittedDailyConsumption:
    __slots__ = (
        "__weakref__",
        "_input_digests",
        "_seal",
        "consumption_id",
        "derivation_id",
        "execution_name",
        "grant",
        "manifest",
        "operation",
        "operation_context",
        "operation_context_sha256",
    )

    def __init__(self, seal: object, *, derivation, consumption, context, manifest, grant):
        if seal is not _SEAL:
            raise DailyAuthorityIntegrityError("daily_consumption_forged")
        self._seal = seal
        self.derivation_id = derivation["derivation_id"]
        self.consumption_id = consumption["consumption_id"]
        self.execution_name = consumption["execution_name"]
        self.operation_context_sha256 = derivation["operation_context_sha256"]
        self.operation = derivation["operation"]
        self.operation_context = _freeze(context)
        self.manifest = _freeze(manifest)
        self.grant = _freeze(grant)
        self._input_digests = MappingProxyType(input_artifact_digests(manifest))
        _ISSUED_CONSUMPTIONS.add(self)

    def input_artifact_sha256(self, name: str) -> str:
        try:
            return self._input_digests[name]
        except (KeyError, TypeError) as error:
            raise DailyAuthorityIntegrityError("daily_input_artifact_unavailable") from error

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise DailyAuthorityIntegrityError("daily_consumption_immutable")
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise DailyAuthorityIntegrityError("daily_consumption_copy_refused")

    def __deepcopy__(self, memo):
        raise DailyAuthorityIntegrityError("daily_consumption_copy_refused")


class AdmittedDailyExecutionChain:
    __slots__ = (
        "__weakref__",
        "_seal",
        "authorizing_approval",
        "consumption",
        "derivation",
        "effective_available_at",
        "execution_observation",
        "native_execution",
        "operation_context",
        "operation_payload",
        "result",
    )

    def __init__(self, seal: object, values: Mapping[str, object]):
        if seal is not _SEAL:
            raise DailyAuthorityIntegrityError("daily_chain_forged")
        self._seal = seal
        for name in (
            "derivation",
            "authorizing_approval",
            "consumption",
            "result",
            "operation_context",
            "operation_payload",
            "execution_observation",
            "effective_available_at",
            "native_execution",
        ):
            object.__setattr__(self, name, _freeze(values[name]))
        _ISSUED_CHAINS.add(self)

    def __copy__(self):
        raise DailyAuthorityIntegrityError("daily_chain_copy_refused")

    def __setattr__(self, name, value):
        if hasattr(self, name):
            raise DailyAuthorityIntegrityError("daily_chain_immutable")
        object.__setattr__(self, name, value)

    def __deepcopy__(self, memo):
        raise DailyAuthorityIntegrityError("daily_chain_copy_refused")


def _integrity(error: Exception) -> DailyAuthorityIntegrityError:
    if isinstance(error, DailyAuthorityIntegrityError):
        return error
    if isinstance(error, DailyContractError):
        return DailyAuthorityIntegrityError(error.code)
    return DailyAuthorityIntegrityError("daily_authority_integrity_invalid")


def _validate_consumption_parts(parts: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    try:
        if set(parts) != {
            "authorizing_approval",
            "consumption",
            "derivation",
            "grant",
            "manifest",
            "operation_context",
        }:
            raise DailyAuthorityIntegrityError("daily_consumption_fields_invalid")
        derivation = _mapping(parts["derivation"], "daily_derivation_invalid")
        consumption = _mapping(parts["consumption"], "daily_consumption_invalid")
        context = validate_operation_context(parts["operation_context"])
        manifest = _mapping(parts["manifest"], "daily_manifest_invalid")
        grant = _mapping(parts["grant"], "daily_grant_invalid")
        approval = _mapping(parts["authorizing_approval"], "daily_approval_invalid")
        if (
            not isinstance(derivation.get("derivation_id"), str)
            or DERIVATION_ID.fullmatch(derivation["derivation_id"]) is None
        ):
            raise DailyAuthorityIntegrityError("daily_derivation_id_invalid")
        if (
            not isinstance(consumption.get("consumption_id"), str)
            or CONSUMPTION_ID.fullmatch(consumption["consumption_id"]) is None
        ):
            raise DailyAuthorityIntegrityError("daily_consumption_id_invalid")
        for name in ("operation", "authorizing_grant_digest"):
            if derivation.get(name) != context.get(name):
                raise DailyAuthorityIntegrityError("daily_context_derivation_mismatch")
        if (
            consumption.get("approval_id") != derivation["derivation_id"]
            or consumption.get("operation") != derivation["operation"]
        ):
            raise DailyAuthorityIntegrityError("daily_consumption_derivation_mismatch")
        if (
            approval.get("approval_id") != derivation.get("authorizing_approval_id")
            or approval.get("manifest_sha256") != derivation.get("authorizing_grant_digest")
            or grant.get("contract_version") != "42_recurring_execution_grant_v2"
            or grant.get("grant_id") is None
            or derivation.get("operation") not in grant.get("allowed_operations", ())
        ):
            raise DailyAuthorityIntegrityError("daily_grant_approval_mismatch")
        digests = input_artifact_digests(manifest)
        if digests.get("source_policy") is not None and digests["source_policy"] != grant.get(
            "source_policy_digest"
        ):
            raise DailyAuthorityIntegrityError("daily_source_policy_mismatch")
        return derivation, consumption, context, manifest, grant, approval
    except Exception as error:
        raise _integrity(error) from error


def _admit_daily_consumption(parts: Mapping[str, object]) -> AdmittedDailyConsumption:
    derivation, consumption, context, manifest, grant, _approval = _validate_consumption_parts(
        parts
    )
    return AdmittedDailyConsumption(
        _SEAL,
        derivation=derivation,
        consumption=consumption,
        context=context,
        manifest=manifest,
        grant=grant,
    )


_MANIFEST_LIMITS = frozenset(
    {"max_bytes_billed", "max_credits", "max_model_calls", "max_rows_written"}
)
# The registry contracts write "no upper bound" as the int64 maximum; it is never a cap.
_UNBOUNDED_LIMIT = 9223372036854775807


def _require_bounded_limits(limits: object) -> None:
    if (
        not isinstance(limits, Mapping)
        or set(limits) != _MANIFEST_LIMITS
        or any(type(value) is not int or value < 0 for value in limits.values())
    ):
        raise DailyAuthorityIntegrityError("daily_manifest_limits_invalid")
    if any(value >= _UNBOUNDED_LIMIT for value in limits.values()):
        raise DailyAuthorityIntegrityError("daily_limit_unbounded")


def derive_daily_execution(
    *,
    canonical_manifest_json: str,
    manifest_sha256: str,
    canonical_operation_context_json: str,
    operation_context_sha256: str,
    canonical_operation_artifact_set_json: str,
    authorizing_grant_digest: str,
    clients: DailyAuthorityWriteClients,
) -> Mapping[str, object]:
    manifest, manifest_bytes = canonical_json_object(
        canonical_manifest_json, "daily_manifest_noncanonical"
    )
    _require_bounded_limits(manifest.get("limits"))
    context = validate_operation_context(canonical_operation_context_json)
    artifacts, artifact_bytes = validate_artifact_set(canonical_operation_artifact_set_json)
    if sha256_bytes(canonical_operation_context_json.encode("utf-8")) != operation_context_sha256:
        raise DailyAuthorityIntegrityError("daily_context_digest_mismatch")
    if sha256_bytes(manifest_bytes) != manifest_sha256:
        raise DailyAuthorityIntegrityError("daily_manifest_digest_mismatch")
    if sha256_bytes(artifact_bytes) != context["operation_artifact_set_sha256"]:
        raise DailyAuthorityIntegrityError("daily_artifact_set_digest_mismatch")
    if context["operation"] == "daily_source_snapshot_capture":
        validate_capture_semantics(context, artifacts, now=_utc_now())
    if context["authorizing_grant_digest"] != authorizing_grant_digest:
        raise DailyAuthorityIntegrityError("daily_grant_digest_mismatch")
    expected = input_artifact_digests(manifest)
    if set(artifacts) != set(expected) - {"operation_context"}:
        raise DailyAuthorityIntegrityError("daily_artifact_manifest_set_mismatch")
    if any(sha256_bytes(value) != expected[name] for name, value in artifacts.items()):
        raise DailyAuthorityIntegrityError("daily_artifact_manifest_digest_mismatch")
    result = clients.derive_execution(
        canonical_manifest_json,
        manifest_sha256,
        canonical_operation_context_json,
        operation_context_sha256,
        canonical_operation_artifact_set_json,
        authorizing_grant_digest,
    )
    row = _mapping(result, "daily_derivation_invalid")
    if (
        row.get("operation") != context["operation"]
        or row.get("operation_context_sha256") != operation_context_sha256
    ):
        raise DailyAuthorityIntegrityError("daily_derivation_readback_mismatch")
    return _freeze(row)


def select_daily_derivation(
    *,
    child_job_resource: str,
    child_image_uri: str,
    authorizing_grant_digest: str,
    child_job_policy_digest: str,
    clients: DailyAuthorityWriteClients,
) -> Mapping[str, object]:
    row = _mapping(
        clients.select_derivation(
            child_job_resource, child_image_uri, authorizing_grant_digest, child_job_policy_digest
        ),
        "daily_derivation_selection_invalid",
    )
    if (
        row.get("child_job_resource") != child_job_resource
        or row.get("child_image_uri") != child_image_uri
        or row.get("authorizing_grant_digest") != authorizing_grant_digest
        or row.get("child_job_policy_digest") != child_job_policy_digest
    ):
        raise DailyAuthorityIntegrityError("daily_derivation_selection_mismatch")
    return _freeze(row)


def consume_daily_derivation(
    *,
    derivation_id: str,
    execution_name: str,
    canonical_operation_context_json: str,
    canonical_operation_artifact_set_json: str,
    canonical_execution_observation_json: str,
    execution_observation_sha256: str,
    clients: DailyAuthorityWriteClients,
) -> AdmittedDailyConsumption:
    try:
        context = validate_operation_context(canonical_operation_context_json)
        artifacts, artifact_bytes = validate_artifact_set(canonical_operation_artifact_set_json)
        observation, observation_bytes = validate_execution_observation(
            canonical_execution_observation_json
        )
    except DailyContractError as error:
        raise _integrity(error) from error
    if (
        observation["derivation_id"] != derivation_id
        or observation["execution_name"] != execution_name
        or sha256_bytes(observation_bytes) != execution_observation_sha256
    ):
        raise DailyAuthorityIntegrityError("daily_observation_binding_invalid")
    if sha256_bytes(artifact_bytes) != context["operation_artifact_set_sha256"]:
        raise DailyAuthorityIntegrityError("daily_artifact_set_digest_mismatch")
    native_job = _mapping(
        clients.read_native_job(context["child_job_resource"]), "daily_native_job_invalid"
    )
    native_execution = _mapping(
        clients.read_native_execution(execution_name), "daily_native_execution_invalid"
    )
    try:
        validate_observation_time_bounds(observation, read_at=_utc_now())
    except DailyContractError as error:
        raise _integrity(error) from error
    native_binding = {
        "grant_digest": context["authorizing_grant_digest"],
        "image_uri": context["child_image_uri"],
        "job_policy_digest": context["child_job_policy_digest"],
        "job_resource": context["child_job_resource"],
        "service_identity": context["child_service_identity"],
    }
    if any(native_job.get(name) != value for name, value in native_binding.items()):
        raise DailyAuthorityIntegrityError("daily_native_job_mismatch")
    execution_binding = dict(
        native_binding,
        execution_name=execution_name,
        step_number=STEP_NUMBERS_V2[context["stage"]],
    )
    if any(native_execution.get(name) != value for name, value in execution_binding.items()):
        raise DailyAuthorityIntegrityError("daily_native_execution_mismatch")
    if (
        observation["child_job_resource"] != native_execution.get("job_resource")
        or observation["child_image_uri"] != native_execution.get("image_uri")
        or observation["child_service_identity"] != native_execution.get("service_identity")
        or observation["child_job_policy_digest"] != native_execution.get("job_policy_digest")
        or observation["authorizing_grant_digest"] != native_execution.get("grant_digest")
        or observation["execution_created_at"] != native_execution.get("execution_created_at")
        or observation["execution_started_at"] != native_execution.get("execution_started_at")
    ):
        raise DailyAuthorityIntegrityError("daily_observation_native_mismatch")
    admitted_bytes, storage = clients.admit_execution_observation(
        execution_name, observation_bytes, execution_observation_sha256
    )
    if admitted_bytes != observation_bytes:
        raise DailyAuthorityIntegrityError("daily_observation_storage_invalid")
    _validate_observation_storage(
        storage, execution_name, admitted_bytes, execution_observation_sha256
    )
    parts = _mapping(
        clients.consume_derivation(
            derivation_id,
            execution_name,
            canonical_operation_context_json,
            canonical_operation_artifact_set_json,
            canonical_execution_observation_json,
            execution_observation_sha256,
        ),
        "daily_consumption_readback_invalid",
    )
    derivation = _mapping(parts.get("derivation"), "daily_derivation_invalid")
    returned_context = parts.get("operation_context")
    returned_manifest = _mapping(parts.get("manifest"), "daily_manifest_invalid")
    consumption = _mapping(parts.get("consumption"), "daily_consumption_invalid")
    try:
        consumed_at = datetime.fromisoformat(consumption["consumed_at"])
        validate_observation_time_bounds(
            observation, stored_at=storage["created_at"], read_at=consumed_at
        )
    except (KeyError, TypeError, ValueError, DailyContractError) as error:
        raise DailyAuthorityIntegrityError("daily_observation_time_invalid") from error
    if (
        canonical_bytes(returned_context) != canonical_operation_context_json.encode("utf-8")
        or derivation.get("canonical_operation_context_json") != canonical_operation_context_json
        or derivation.get("operation_context_sha256")
        != sha256_bytes(canonical_operation_context_json.encode("utf-8"))
        or derivation.get("canonical_manifest_json")
        != canonical_bytes(returned_manifest).decode("utf-8")
        or derivation.get("manifest_sha256") != sha256_bytes(canonical_bytes(returned_manifest))
        or derivation.get("derivation_id") != derivation_id
        or derivation.get("business_attempt_id") != context["business_attempt_id"]
        or derivation.get("child_job_resource") != context["child_job_resource"]
    ):
        raise DailyAuthorityIntegrityError("daily_protected_readback_mismatch")
    expected_consumption_id = "exc_" + sha256_bytes(
        canonical_bytes(
            {
                "consumed_at": consumption.get("consumed_at"),
                "consumption_contract_version": "open_intelligence_execution_consumption_v3",
                "derivation_id": derivation_id,
                "execution_name": execution_name,
                "execution_observation_sha256": execution_observation_sha256,
                "origin_registry_sha256": consumption.get("origin_registry_sha256"),
                "resource_manifest_sha256": consumption.get("resource_manifest_sha256"),
            }
        )
    )
    if consumption.get("consumption_id") != expected_consumption_id:
        raise DailyAuthorityIntegrityError("daily_consumption_identity_mismatch")
    admitted = _admit_daily_consumption(parts)
    if admitted.execution_name != execution_name or admitted.derivation_id != derivation_id:
        raise DailyAuthorityIntegrityError("daily_consumption_readback_mismatch")
    expected = input_artifact_digests(admitted.manifest)
    if set(artifacts) != set(expected) - {"operation_context"}:
        raise DailyAuthorityIntegrityError("daily_artifact_manifest_set_mismatch")
    for name, content in artifacts.items():
        if sha256_bytes(content) != expected[name]:
            raise DailyAuthorityIntegrityError("daily_artifact_manifest_digest_mismatch")
    return admitted


def cancel_daily_derivation(
    *,
    derivation_id: str,
    reason_code: str,
    canonical_reconciliation_json: str,
    reconciliation_digest: str,
    clients: DailyAuthorityWriteClients,
) -> Mapping[str, object]:
    reconciliation, reconciliation_bytes = validate_reconciliation(canonical_reconciliation_json)
    if (
        sha256_bytes(reconciliation_bytes) != reconciliation_digest
        or reconciliation["derivation_id"] != derivation_id
        or reconciliation["reason_code"] != reason_code
    ):
        raise DailyAuthorityIntegrityError("daily_reconciliation_digest_mismatch")
    if reason_code == "provider_terminal_no_execution":
        provider = _mapping(
            clients.read_provider_operation(reconciliation["provider_operation_name"]),
            "daily_provider_operation_invalid",
        )
        if (
            provider.get("operation_name") != reconciliation["provider_operation_name"]
            or provider.get("terminal_state") != "done_no_execution"
            or provider.get("execution_created") is not False
            or provider.get("observation_reference")
            != reconciliation["dispatch_observation_reference"]
            or provider.get("observed_at") != reconciliation["observed_at"]
            or provider.get("observer_principal") != reconciliation["observer_principal"]
        ):
            raise DailyAuthorityIntegrityError("daily_provider_operation_mismatch")
    row = _mapping(
        clients.cancel_derivation(
            derivation_id, reason_code, canonical_reconciliation_json, reconciliation_digest
        ),
        "daily_tombstone_invalid",
    )
    if (
        row.get("derivation_id") != derivation_id
        or row.get("reason_code") != reason_code
        or row.get("reconciliation_digest") != reconciliation_digest
    ):
        raise DailyAuthorityIntegrityError("daily_tombstone_readback_mismatch")
    return _freeze(row)


def record_daily_result(
    *,
    derivation_id: str,
    consumption_id: str,
    canonical_payload_json: str,
    payload_digest: str,
    execution_observation_sha256: str,
    canonical_stage_metering_json: str,
    result_reference: str,
    terminal_state: str,
    effect_state: str,
    spend_state: str,
    clients: DailyAuthorityWriteClients,
) -> Mapping[str, object]:
    try:
        canonical_json_object(canonical_payload_json, "daily_payload_noncanonical")
        validate_result_metering(
            canonical_stage_metering_json,
            terminal_state=terminal_state,
            effect_state=effect_state,
            spend_state=spend_state,
        )
    except DailyContractError as error:
        raise _integrity(error) from error
    if sha256_bytes(canonical_payload_json.encode("utf-8")) != payload_digest:
        raise DailyAuthorityIntegrityError("daily_payload_digest_mismatch")
    row = _mapping(
        clients.record_result(
            derivation_id,
            consumption_id,
            canonical_payload_json,
            payload_digest,
            execution_observation_sha256,
            canonical_stage_metering_json,
            result_reference,
            terminal_state,
            effect_state,
            spend_state,
        ),
        "daily_result_invalid",
    )
    envelope = _result_envelope(row)
    expected = {
        "derivation_id": derivation_id,
        "consumption_id": consumption_id,
        "payload_digest": payload_digest,
        "execution_observation_sha256": execution_observation_sha256,
        "result_reference": result_reference,
        "terminal_state": terminal_state,
        "effect_state": effect_state,
        "spend_state": spend_state,
    }
    if (
        any(envelope.get(name) != value for name, value in expected.items())
        or canonical_bytes(envelope["operation_payload"]).decode("utf-8") != canonical_payload_json
        or canonical_bytes(envelope["stage_metering"]).decode("utf-8")
        != canonical_stage_metering_json
    ):
        raise DailyAuthorityIntegrityError("daily_result_readback_mismatch")
    return _freeze(row)


def require_daily_consumption(value: object, *, operation: str) -> AdmittedDailyConsumption:
    if (
        type(value) is not AdmittedDailyConsumption
        or value not in _ISSUED_CONSUMPTIONS
        or value._seal is not _SEAL
        or value.operation != operation
    ):
        raise DailyAuthorityIntegrityError("daily_consumption_capability_invalid")
    return value


def input_artifact_sha256(value: object, name: str) -> str:
    if type(value) is not AdmittedDailyConsumption or value not in _ISSUED_CONSUMPTIONS:
        raise DailyAuthorityIntegrityError("daily_consumption_capability_invalid")
    return value.input_artifact_sha256(name)


def read_daily_execution_chain(
    *, derivation_id: str, clients: DailyAuthorityReadClients
) -> AdmittedDailyExecutionChain:
    if not isinstance(derivation_id, str) or DERIVATION_ID.fullmatch(derivation_id) is None:
        raise DailyAuthorityIntegrityError("daily_derivation_id_invalid")
    try:
        summary = _mapping(clients.read_derivation(derivation_id), "daily_derivation_read_invalid")
    except DailyAuthorityIntegrityError:
        raise
    except DailyAuthorityUnavailable:
        raise
    except Exception as error:
        raise DailyAuthorityUnavailable("daily_derivation_unavailable") from error
    if set(summary) != {"consumption", "derivation", "lifecycle_state", "tombstone"}:
        raise DailyAuthorityIntegrityError("daily_derivation_read_invalid")
    if (
        summary["lifecycle_state"] != "consumed"
        or summary["tombstone"] is not None
        or not isinstance(summary["consumption"], Mapping)
    ):
        raise DailyAuthorityUnavailable("daily_chain_nonterminal")
    consumption = summary["consumption"]
    execution_name = consumption.get("execution_name")
    if not isinstance(execution_name, str) or not execution_name:
        raise DailyAuthorityIntegrityError("daily_execution_name_invalid")
    try:
        observation_bytes, storage = clients.read_execution_observation(execution_name)
    except DailyAuthorityIntegrityError:
        raise
    except DailyAuthorityUnavailable:
        raise
    except Exception as error:
        raise DailyAuthorityUnavailable("daily_observation_unavailable") from error
    try:
        observation, canonical_observation = validate_execution_observation(observation_bytes)
    except DailyContractError as error:
        raise DailyAuthorityIntegrityError(error.code) from error
    digest = sha256_bytes(canonical_observation)
    _validate_observation_storage(storage, execution_name, canonical_observation, digest)
    if (
        observation["derivation_id"] != derivation_id
        or observation["execution_name"] != execution_name
    ):
        raise DailyAuthorityIntegrityError("daily_observation_binding_invalid")
    try:
        chain = _mapping(
            clients.read_chain(derivation_id, canonical_observation.decode("utf-8"), digest),
            "daily_chain_invalid",
        )
        native = _mapping(
            clients.read_native_execution(execution_name), "daily_native_execution_invalid"
        )
    except DailyAuthorityIntegrityError:
        raise
    except DailyAuthorityUnavailable:
        raise
    except Exception as error:
        raise DailyAuthorityUnavailable("daily_chain_unavailable") from error
    required = {
        "authorizing_approval",
        "consumption",
        "derivation",
        "grant",
        "manifest",
        "operation_context",
        "operation_payload",
        "result",
    }
    if set(chain) != required:
        raise DailyAuthorityIntegrityError("daily_chain_fields_invalid")
    _validate_consumption_parts(
        {name: chain[name] for name in required - {"operation_payload", "result"}}
    )
    if chain["derivation"] != summary["derivation"] or chain["consumption"] != consumption:
        raise DailyAuthorityIntegrityError("daily_chain_summary_mismatch")
    if chain["result"] is None:
        raise DailyAuthorityUnavailable("daily_result_unavailable")
    result = _mapping(chain["result"], "daily_result_invalid")
    payload = _mapping(chain["operation_payload"], "daily_operation_payload_invalid")
    envelope = _result_envelope(result)
    if canonical_bytes(envelope["operation_payload"]) != canonical_bytes(payload):
        raise DailyAuthorityIntegrityError("daily_payload_digest_invalid")
    binding = {
        "authorizing_approval_id": chain["derivation"].get("authorizing_approval_id"),
        "authorizing_grant_digest": chain["derivation"].get("authorizing_grant_digest"),
        "business_attempt_id": chain["derivation"].get("business_attempt_id"),
        "child_job_resource": chain["derivation"].get("child_job_resource"),
        "derivation_id": derivation_id,
        "consumption_id": consumption.get("consumption_id"),
        "execution_name": execution_name,
        "execution_observation_sha256": digest,
        "manifest_sha256": chain["derivation"].get("manifest_sha256"),
        "operation": chain["derivation"].get("operation"),
        "operation_context_sha256": chain["derivation"].get("operation_context_sha256"),
    }
    if any(envelope.get(name) != value for name, value in binding.items()):
        raise DailyAuthorityIntegrityError("daily_result_binding_invalid")
    if native.get("execution_name") != execution_name:
        raise DailyAuthorityIntegrityError("daily_native_execution_mismatch")
    # A cancelled execution is terminal but never matches a recorded result.
    if native.get("terminal_state") not in {"succeeded", "failed", "cancelled"}:
        raise DailyAuthorityUnavailable("daily_native_execution_nonterminal")
    if native.get("terminal_state") != envelope["terminal_state"]:
        raise DailyAuthorityIntegrityError("daily_native_terminal_mismatch")
    native_completed = native_instant_key(native.get("completed_at"))
    result_completed = native_instant_key(envelope["completed_at"])
    consumed_at = native_instant_key(consumption.get("consumed_at"))
    if native_completed < consumed_at or result_completed < consumed_at:
        raise DailyAuthorityIntegrityError("daily_native_terminal_mismatch")
    effective_available_at = (
        native["completed_at"] if native_completed >= result_completed else envelope["completed_at"]
    )
    for observation_name, native_name in (
        ("child_job_resource", "job_resource"),
        ("child_image_uri", "image_uri"),
        ("child_service_identity", "service_identity"),
        ("child_job_policy_digest", "job_policy_digest"),
    ):
        if observation[observation_name] != native.get(native_name):
            raise DailyAuthorityIntegrityError("daily_native_execution_mismatch")
    for name in ("execution_created_at", "execution_started_at"):
        if observation[name] != native.get(name):
            raise DailyAuthorityIntegrityError("daily_native_execution_mismatch")
    values = dict(chain)
    values["execution_observation"] = observation
    values["native_execution"] = native
    values["effective_available_at"] = effective_available_at
    admitted = AdmittedDailyExecutionChain(_SEAL, values)
    return admitted


__all__ = [
    "AdmittedDailyConsumption",
    "AdmittedDailyExecutionChain",
    "DailyAuthorityBudget",
    "DailyAuthorityIntegrityError",
    "DailyAuthorityReadClients",
    "DailyAuthorityUnavailable",
    "DailyAuthorityWriteClients",
    "NativeDailyAuthorityReadAdapter",
    "NativeDailyAuthorityWriteAdapter",
    "cancel_daily_derivation",
    "consume_daily_derivation",
    "derive_daily_execution",
    "input_artifact_sha256",
    "native_instant_key",
    "read_daily_execution_chain",
    "record_daily_result",
    "require_daily_consumption",
    "select_daily_derivation",
]
