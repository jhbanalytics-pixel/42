"""Stage adapters the daily cycle kernel dispatches.

``build_stage_handlers`` binds the five ``StageHandler`` callables of one daily profile
(collect, capture, compose, certify, release) over injected native clients. Every handler
recomputes the kernel's manifest digest from the same canonical bytes, dispatches at most
one paid native call per business attempt, and admits its result only from a durable
readback through that stage's own validator: the collection receipt ledger and the D03
completed run validator, the capture registry admission kernel, the run receipt reader,
the release script's certification readers and the release record readback. A handler
never returns a state it made up. A refusal before any native call is a retry safe
failure naming its code; a native call whose durable outcome could not be read back is
unknown; a partial or failed paid result stays exactly that with retry_safe False, and a
validator that refuses a read back paid result is a failure that is not retry safe.

Native identity is the clients' concern. The collection ledger is keyed by the business
attempt the kernel issued, so the same attempt reconciles to the same receipt; the
capture registry entry carries the attempt as its operation id; the run receipt is keyed
by the approved release profile's run id. Nothing here opens a connection or constructs a
cloud or model client; the two staging scripts are imported lazily inside the handlers
that use their readers and contracts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256

from scripts.staging.collect_42_sources import (
    collect_staging,
    collection_receipt,
    unverified_authority,
)

from src.analysis.open_intelligence import persistence
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.capture_registry import (
    CAPTURE_COVERAGE_FIELDS,
    admit_capture_coverage,
    create_capture_entry,
    validate_capture_entry,
)
from src.analysis.open_intelligence.daily_cycle import (
    STAGE_ORDER,
    StageResult,
    validate_daily_profile,
)
from src.analysis.open_intelligence.readiness import (
    EXPLICIT_ORIGIN_POLICY,
    evaluate_readiness,
    require_independent_release,
)
from src.analysis.open_intelligence.run_receipts import run_receipt_digest
from src.analysis.open_intelligence.staging_source_profile import (
    build_staging_source_profile,
    read_completed_source_run,
)

CLIENT_FIELDS = (
    "clock",
    "store",
    "collection_profile",
    "collector",
    "collection_ledger",
    "source_runs",
    "generation",
    "capture",
    "composer",
    "persist",
    "warehouse",
    "certifier",
    "release_profile",
    "products",
)
CAPTURE_OPERATION = "source_snapshot_capture"
CAPTURE_ORIGIN_MANIFEST_VERSION = "open_intelligence_execution_manifest_v2"
CAPTURE_ORIGIN_MODE = "new_consume"
CAPTURE_AUTHORITY_REFUSAL = "capture_authority_unbound"
CAPTURE_READBACK_FIELDS = frozenset(
    {
        "result_id",
        "content_digest",
        "image_digest",
        "generation",
        "completion_state",
        "capture_available_at",
        "snapshot_as_of",
        "scope",
        "schema_digest",
        "source_digest",
        "market_scope",
        "creation_records",
    }
)
# The readback of an attempt whose result was recorded before the capture coverage
# travelled with it. Such an object is read, never mistaken for a corrupt record, and
# its absent coverage is refused by its own name at the admission.
CAPTURE_PRE_COVERAGE_READBACK_FIELDS = CAPTURE_READBACK_FIELDS - CAPTURE_COVERAGE_FIELDS
CERTIFICATION_ARTIFACT_FIELDS = frozenset(
    {
        "execution_proof_bytes",
        "execution_proof_manifest_sha256",
        "quality_review_receipt",
        "review_packet",
        "candidate_projection_digest",
        "apply_binding",
    }
)
_MANIFEST_FIELDS = frozenset(
    {"operation_id", "stage", "cutoff_utc", "profile", "predecessor_digest"}
)
_PERSISTED_MARKET_STATES = frozenset({"collected", "partial"})
# The collection boundary asks every caller to name the authority it runs under.
# This stage reaches it under the daily control chain and keys the run by the
# attempt it issues, not by an execution any resource manifest read here bound,
# so it says so by name rather than leaving the difference implicit.
COLLECTION_AUTHORITY = unverified_authority("daily_collect_stage")
_RESERVED_BATCH_FIELDS = frozenset(
    {"signal_date", "created_at", "independence_policy", "telemetry"}
)
_PROFILE_KEY = "_profile"


class StageRefusal(ValueError):
    """A validator refusal; before a native call it is retry safe, after one it is not."""


class _Unresolved(Exception):
    """A native call ran and its durable outcome cannot be admitted: the state is unknown."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _refuse(code: str) -> None:
    raise StageRefusal(code)


def _unresolved(code: str) -> None:
    raise _Unresolved(code)


def _result(
    state: str, input_digest: str, *, output_digest: str = "", reference: str, retry_safe: bool
) -> StageResult:
    return {
        "state": state,  # type: ignore[typeddict-item]
        "input_digest": input_digest,
        "output_digest": output_digest,
        "result_reference": reference,
        "retry_safe": retry_safe,
    }


def _refused(stage: str, digest: str, error: BaseException, *, entered: bool) -> StageResult:
    code = getattr(error, "code", None) or str(error)
    return _result("failed", digest, reference=f"{stage}_refused:{code}", retry_safe=not entered)


def _unknown(stage: str, digest: str, error: BaseException) -> StageResult:
    code = getattr(error, "code", None) or (
        str(error) if isinstance(error, ValueError) else type(error).__name__
    )
    return _result("unknown", digest, reference=f"{stage}_unresolved:{code}", retry_safe=False)


def _aware(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _refuse(code)
    return value  # type: ignore[return-value]


def _instant(value: object, code: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            _refuse(code)
    return _aware(value, code)


def _now(clients: Mapping[str, object]) -> datetime:
    return _aware(clients["clock"](), "clock_invalid")  # type: ignore[operator]


def _native(call: Callable[[], object], code: str) -> object:
    """Run one native call; a stage refusal keeps its code, anything else leaves the stage unknown.

    A client that refuses deterministically before its paid call raises ``StageRefusal``
    and the handler records it by its own boundary (retry safe before entry, unknown after);
    every other exception is a native call whose durable outcome cannot be read back.
    """
    try:
        return call()
    except StageRefusal:
        raise
    except Exception as error:
        raise _Unresolved(f"{code}:{type(error).__name__}") from error


def _admit_manifest(
    clients: Mapping[str, object], manifest: object, stage: str
) -> tuple[str, datetime]:
    """Recompute the kernel's digest over the same canonical bytes and read the cutoff."""
    if (
        not isinstance(manifest, Mapping)
        or set(manifest) != _MANIFEST_FIELDS
        or manifest["stage"] != stage
        or not isinstance(manifest["cutoff_utc"], str)
        or not isinstance(manifest["predecessor_digest"], str)
    ):
        raise ValueError("stage_manifest_invalid")
    if validate_daily_profile(manifest["profile"]) != clients[_PROFILE_KEY]:
        raise ValueError("stage_profile_conflict")
    try:
        cutoff = datetime.fromisoformat(manifest["cutoff_utc"])
    except ValueError as error:
        raise ValueError("stage_manifest_invalid") from error
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("stage_manifest_invalid")
    return sha256(canonical_bytes(manifest)).hexdigest(), cutoff


def _attempt_id(authority_receipt: object) -> str:
    if not isinstance(authority_receipt, Mapping):
        raise ValueError("authority_receipt_invalid")
    attempt = authority_receipt.get("business_attempt_id")
    if not isinstance(attempt, str) or not attempt:
        raise ValueError("authority_receipt_invalid")
    return attempt


def _execution_authority(
    authority_receipt: Mapping[str, object], stage: str
) -> Mapping[str, object]:
    """The issued execution authority the receipt carries for this stage.

    The daily authority issues it beside the plain consumption: the authority object and
    the consumption the reservation path answered, under the execution mode the v2 origin
    binds. A receipt without them (an attempt issued by an earlier process, or a fake that
    carries the reference alone) cannot dispatch, so the stage refuses retry safe.
    """
    execution = authority_receipt.get("execution")
    if (
        not isinstance(execution, Mapping)
        or execution.get("authority") is None
        or execution.get("consumption") is None
        or execution.get("stage") != stage
        or execution.get("mode") != CAPTURE_ORIGIN_MODE
    ):
        _refuse(f"{stage}_authority_unbound")
    return execution  # type: ignore[return-value]


def _authority_present(authority_receipt: object) -> bool:
    try:
        _attempt_id(authority_receipt)
    except ValueError:
        return False
    return True


def _stage_record(clients: Mapping[str, object], operation_id: str, stage: str) -> dict:
    record = clients["store"].read_stage(operation_id, stage)  # type: ignore[attr-defined]
    if not isinstance(record, Mapping) or record.get("state") != "succeeded":
        _refuse(f"{stage}_record_unavailable")
    return dict(record)


def _predecessor(clients: Mapping[str, object], manifest: Mapping[str, object]) -> dict:
    """The kernel's own record of the previous stage, which must carry this manifest's digest."""
    stage = STAGE_ORDER[STAGE_ORDER.index(manifest["stage"]) - 1]
    record = _stage_record(clients, manifest["operation_id"], stage)
    if not record["output_digest"] or record["output_digest"] != manifest["predecessor_digest"]:
        _refuse("predecessor_record_differs")
    return record


def _reference_suffix(reference: object, prefix: str) -> str:
    if not isinstance(reference, str) or not reference.startswith(prefix):
        _refuse("predecessor_reference_invalid")
    return reference[len(prefix) :]


def _serialized(value: Mapping[str, object]) -> dict:
    return {
        name: item.isoformat() if isinstance(item, datetime) else item
        for name, item in value.items()
    }


def _closed_day(cutoff: datetime) -> date:
    """The observation day a midnight cutoff closes; any other instant names its own day."""
    end = cutoff.astimezone(UTC)
    if end.time() == time(0):
        return end.date() - timedelta(days=1)
    return end.date()


def _release_module():
    from scripts.staging import release_open_intelligence_run

    return release_open_intelligence_run


def _read_optional_receipt(run_id: str, warehouse):
    """The run receipt through the release script's reader, or None while none is recorded."""
    release = _release_module()
    try:
        return release._read_receipt(run_id, warehouse)
    except release.ReleaseRefusal as error:
        message = str(error)
        if message.startswith("expected exactly one receipt") and message.endswith("found 0"):
            return None
        raise


def _receipt_reference(run_id: str) -> str:
    release = _release_module()
    return f"bq://{release.PROJECT}.{release.DATASET}.{release.RECEIPT_TABLE}#{run_id}"


# Collect


def _collection_readback(ledger, receipt: object, attempt: str) -> dict:
    """Read the receipt back from the ledger under the attempt and admit it through the validator.

    The receipt must carry the attempt the handler keyed the ledger with as its execution
    id; one stamped under another id, whether returned by the terminal or planted under
    the attempt's key, cannot be attributed to this attempt and leaves the stage unknown.
    """
    if not isinstance(receipt, Mapping):
        _unresolved("collection_receipt_invalid")
    if receipt.get("execution_id") != attempt:  # type: ignore[union-attr]
        _unresolved("collection_receipt_attempt_mismatch")
    recorded = ledger.operation(attempt)
    if not isinstance(recorded, Mapping):
        _unresolved("collection_receipt_unrecorded")
    fields = dict(recorded)
    contract_version = fields.pop("contract_version", None)
    fields.pop("complete", None)
    try:
        validated = collection_receipt(
            run_id=fields.pop("run_id"),
            execution_id=fields.pop("execution_id"),
            source_sha=fields.pop("source_sha"),
            image_uri=fields.pop("image_uri"),
            policy_sha256=fields.pop("policy_sha256"),
            profile_sha256=fields.pop("profile_sha256"),
            cutoff=fields.pop("cutoff"),
            market_states=fields.pop("market_states"),
            raw_count=fields.pop("raw_rows_persisted"),
            enriched_count=fields.pop("enriched_rows_persisted"),
            funded_close=fields.pop("funded_close"),
            authority_kind=fields.pop("authority_kind", None),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _Unresolved("collection_receipt_invalid") from error
    if fields or contract_version != validated["contract_version"] or validated != dict(recorded):
        _unresolved("collection_receipt_invalid")
    if validated != dict(receipt):
        _unresolved("collection_receipt_differs")
    return validated


def _collection_state(receipt: Mapping[str, object]) -> str:
    if receipt["complete"] is True:
        return "succeeded"
    states = receipt["market_states"]
    if any(state in _PERSISTED_MARKET_STATES for state in states.values()):  # type: ignore[union-attr]
        return "partial"
    return "failed"


def _recorded_instants(clients: Mapping[str, object], run_id: str) -> tuple[datetime, datetime]:
    outcome = clients["source_runs"].read(run_id)  # type: ignore[attr-defined]
    run = outcome.get("run") if isinstance(outcome, Mapping) else None
    if not isinstance(run, Mapping):
        _refuse("source_run_unrecorded")
    return (
        _aware(run["collection_started_at"], "source_run_invalid"),
        _aware(run["collection_completed_at"], "source_run_invalid"),
    )


def _collect_completed(
    clients: Mapping[str, object], recorded: dict, *, cutoff: datetime, started, completed
) -> dict:
    """Record the completed run with its instants and read it back through the D03 validator."""
    run_id = recorded["run_id"]
    source_runs = clients["source_runs"]
    existing = source_runs.read(run_id)  # type: ignore[attr-defined]
    if not (isinstance(existing, Mapping) and isinstance(existing.get("run"), Mapping)):
        run = {
            "receipt": recorded,
            "collection_started_at": started,
            "collection_completed_at": completed,
        }
        _native(lambda: source_runs.record(run_id, run), "source_run_record")  # type: ignore[attr-defined]
    validated = read_completed_source_run(source_runs.read, run_id)  # type: ignore[attr-defined]
    if validated["receipt"] != recorded:
        _refuse("source_run_differs")
    if validated["observation_window_end"] != cutoff:
        _refuse("collection_window_mismatch")
    return {
        "receipt": recorded,
        "observation_window_end": validated["observation_window_end"].isoformat(),
        "collection_started_at": validated["collection_started_at"].isoformat(),
        "collection_completed_at": validated["collection_completed_at"].isoformat(),
    }


def _collect(clients: Mapping[str, object], *, manifest, authority_receipt) -> StageResult:
    digest, cutoff = _admit_manifest(clients, manifest, "collect")
    attempt = _attempt_id(authority_receipt)
    ledger = clients["collection_ledger"]
    entered = False
    try:
        prior = _native(lambda: ledger.operation(attempt), "collection_ledger_read")  # type: ignore[attr-defined]
        if isinstance(prior, Mapping):
            entered = True
            receipt = dict(prior)
            started = completed = None
        else:
            started = _now(clients)

            def terminal(*, stop_after_ingestion: bool) -> dict:
                nonlocal entered
                entered = True
                return _native(
                    lambda: clients["collector"](  # type: ignore[operator]
                        stop_after_ingestion=stop_after_ingestion, attempt_id=attempt
                    ),
                    "collector",
                )

            try:
                receipt = collect_staging(
                    dict(clients["collection_profile"]),  # type: ignore[arg-type]
                    authority=COLLECTION_AUTHORITY,
                    collector=terminal,
                )
            except ValueError as error:
                # The terminal's own guards fire before the producer is imported or invoked.
                raise StageRefusal(str(error)) from error
            completed = _now(clients)
        recorded = _collection_readback(ledger, receipt, attempt)
        if recorded["policy_sha256"] != manifest["profile"]["source_policy_digest"]:
            _refuse("collection_policy_differs")
        state = _collection_state(recorded)
        if state != "succeeded":
            return _result(
                state, digest, reference=f"collection:{recorded['execution_id']}", retry_safe=False
            )
        if started is None:
            started, completed = _recorded_instants(clients, recorded["run_id"])
        payload = _collect_completed(
            clients, recorded, cutoff=cutoff, started=started, completed=completed
        )
    except _Unresolved as error:
        return _unknown("collect", digest, error)
    except ValueError as error:
        return _refused("collect", digest, error, entered=entered)
    return _result(
        "succeeded",
        digest,
        output_digest=canonical_digest(payload),
        reference=f"source_run:{recorded['run_id']}",
        retry_safe=False,
    )


# Capture


def _capture_origin(generation: object):
    """The active v2 origin binding the capture operation, mirroring the approval lookup."""
    registry = getattr(generation, "registry", None)
    if not isinstance(registry, Mapping):
        _refuse("capture_origin_unavailable")
    matches = [
        origin
        for origin in registry.values()  # type: ignore[union-attr]
        if getattr(origin, "manifest_version", None) == CAPTURE_ORIGIN_MANIFEST_VERSION
        and CAPTURE_OPERATION in getattr(origin, "operation_bindings", {})
        and CAPTURE_ORIGIN_MODE in getattr(origin, "allowed_execution_modes", ())
    ]
    if len(matches) != 1:
        _refuse("capture_origin_unavailable")
    return matches[0]


def _capture_datasets(origin: object) -> tuple[str, ...]:
    """The datasets the approved capture origin binds the capture operation to.

    The coverage admission compares the estate the capture wrote into against these, so a
    producer whose destinations drift out of the approved estate registers nothing. An
    origin that binds the operation without naming its datasets is not the approved
    capture origin, and is refused before anything dispatches.
    """
    binding = getattr(origin, "operation_bindings", {}).get(CAPTURE_OPERATION)
    datasets = getattr(binding, "datasets", None)
    if (
        not isinstance(datasets, tuple)
        or not datasets
        or any(not isinstance(name, str) or not name for name in datasets)
    ):
        _refuse("capture_origin_unavailable")
    return datasets  # type: ignore[return-value]


def _compact_payload(payload: object) -> dict:
    from scripts.staging.capture_protected_production_snapshot import _COMPACT_RESULT_FIELDS

    if not isinstance(payload, Mapping) or set(payload) != _COMPACT_RESULT_FIELDS:
        _unresolved("capture_result_invalid")
    return dict(payload)


def _capture_readback(verified: object) -> dict:
    if not isinstance(verified, Mapping) or set(verified) not in (
        CAPTURE_READBACK_FIELDS,
        CAPTURE_PRE_COVERAGE_READBACK_FIELDS,
    ):
        _unresolved("capture_readback_invalid")
    values = dict(verified)
    # A result recorded before the coverage travelled with it carries neither coverage
    # field, and it keeps carrying neither here. It is read as what it is, so the attempt
    # reports the one thing missing from its own record rather than wedging on an invalid
    # record it can never be rewritten out of, and so the admission can tell such an object
    # apart from a runner that answered both fields and reported nothing in them.
    for field in ("capture_available_at", "snapshot_as_of"):
        values[field] = _instant(values[field], "capture_readback_invalid")
    return values


def _capture(clients: Mapping[str, object], *, manifest, authority_receipt) -> StageResult:
    digest, cutoff = _admit_manifest(clients, manifest, "capture")
    attempt = _attempt_id(authority_receipt)
    capture = clients["capture"]
    writer = capture.writer  # type: ignore[attr-defined]
    entered = False
    try:
        origin = _capture_origin(clients["generation"])
        datasets = _capture_datasets(origin)
        collect = _predecessor(clients, manifest)
        run_id = _reference_suffix(collect["result_reference"], "source_run:")
        run = read_completed_source_run(clients["source_runs"].read, run_id)  # type: ignore[attr-defined]
        if run["observation_window_end"] != cutoff:
            _refuse("collection_window_mismatch")
        if run["receipt"]["policy_sha256"] != manifest["profile"]["source_policy_digest"]:
            _refuse("collection_policy_differs")
        run_mapping = {
            "receipt": run["receipt"],
            "collection_started_at": run["collection_started_at"],
            "collection_completed_at": run["collection_completed_at"],
        }
        prior = _native(lambda: capture.verified(attempt), "capture_readback")  # type: ignore[attr-defined]
        if prior is None:
            execution = _execution_authority(authority_receipt, "capture")
            snapshot_as_of = _now(clients)
            entered = True
            outcome = _native(
                lambda: capture.snapshot(  # type: ignore[attr-defined]
                    run_mapping,
                    snapshot_as_of=snapshot_as_of,
                    attempt_id=attempt,
                    origin=origin,
                    authority=execution,
                ),
                "capture_snapshot",
            )
            if not isinstance(outcome, tuple) or len(outcome) != 2:
                _unresolved("capture_result_invalid")
            payload, status = outcome
            payload = _compact_payload(payload)
            if status not in {"succeeded", "failed"}:
                _unresolved("capture_result_invalid")
            if _instant(payload["source_as_of"], "capture_result_invalid") != snapshot_as_of:
                _unresolved("capture_result_invalid")
            verified = _capture_readback(
                _native(lambda: capture.verified(attempt), "capture_readback")  # type: ignore[attr-defined]
            )
            claimed = {
                **verified,
                "content_digest": payload["snapshot_digest"],
                "completion_state": status,
                "capture_available_at": _instant(payload["captured_at"], "capture_result_invalid"),
                "snapshot_as_of": snapshot_as_of,
                "scope": payload["client_scope_id"],
            }
        else:
            # A native result already exists for this attempt: reconcile it, never snapshot again.
            entered = True
            verified = _capture_readback(prior)
            claimed = dict(verified)
        profile = build_staging_source_profile(
            run_mapping,
            snapshot_as_of=claimed["snapshot_as_of"],
            scope=claimed["scope"],
            schema_digest=verified["schema_digest"],
            source_digest=verified["source_digest"],
            now=_now(clients),
        )
        if claimed["completion_state"] != "succeeded":
            return _result(
                "failed",
                digest,
                reference=f"capture:{profile.profile_id}/{claimed['result_id']}",
                retry_safe=False,
            )
        entry_fields = (
            "result_id",
            "content_digest",
            "image_digest",
            "generation",
            "completion_state",
        )
        entry = profile.capture_entry(
            operation_id=attempt,
            capture_available_at=claimed["capture_available_at"],
            **{field: claimed[field] for field in entry_fields},
        )
        if verified["snapshot_as_of"] != profile.snapshot_as_of:
            raise ValueError("authority_mismatch")
        verified_entry = profile.capture_entry(
            operation_id=attempt,
            capture_available_at=verified["capture_available_at"],
            **{field: verified[field] for field in entry_fields},
        )
        # Register only after the native result admits every required snapshot table and
        # every market of the entry; the entry names them, only the result reached them.
        # The coverage is passed as the record carries it, so a record written before the
        # coverage travelled presents neither field rather than two empty ones.
        admit_capture_coverage(
            entry,
            {
                field: verified[field]
                for field in sorted(CAPTURE_COVERAGE_FIELDS)
                if field in verified
            },
            declared_datasets=datasets,
        )
        key = f"{entry['profile_id']}/{entry['result_id']}"
        existing = _native(lambda: writer.read(key), "capture_registry_read")
        admitted = create_capture_entry(entry, verified_entry, writer=writer, existing=existing)
        stored = _native(lambda: writer.read(key), "capture_registry_read")
        if stored is None or validate_capture_entry(stored) != admitted:
            _unresolved("capture_readback_differs")
        telemetry = clients.get("telemetry")
        if telemetry is not None:
            # The claimed and verified capture entries carry a result id, content,
            # image, schema and source digests, a generation, a completion state
            # and two instants, and no observation identity, so this boundary is
            # recorded as unknown at the observation level, never filled by a guess.
            telemetry.boundary_unavailable(
                "capture",
                operation_id=attempt,
                reason_code="capture_result_without_observation_identity",
            )
    except _Unresolved as error:
        return _unknown("capture", digest, error)
    except ValueError as error:
        if entered:
            return _unknown("capture", digest, error)
        return _refused("capture", digest, error, entered=False)
    return _result(
        "succeeded",
        digest,
        output_digest=canonical_digest(_serialized(admitted)),
        reference=f"capture:{key}",
        retry_safe=False,
    )


# Compose


def _admitted_capture(clients: Mapping[str, object], record: Mapping[str, object]) -> dict:
    key = _reference_suffix(record["result_reference"], "capture:")
    stored = _native(lambda: clients["capture"].writer.read(key), "capture_registry_read")  # type: ignore[attr-defined]
    if stored is None:
        _refuse("capture_entry_unavailable")
    entry = validate_capture_entry(stored)
    if canonical_digest(_serialized(entry)) != record["output_digest"]:
        _refuse("capture_entry_differs")
    return entry


def _product_completion(clients, manifest, record):
    operation = manifest["operation_id"]
    if record["result_reference"] != f"products-v1:{operation}":
        _refuse("product_completion_reference_invalid")
    completion = clients["products"].completion.require(
        operation, expected_digest=record["output_digest"]
    )
    capture = _admitted_capture(clients, _stage_record(clients, operation, "capture"))
    if (
        completion["capture_digest"] != canonical_digest(_serialized(capture))
        or completion["policy_digest"] != clients["products"].policy_digest
        or completion["cutoff_utc"] != manifest["cutoff_utc"]
    ):
        _refuse("completion_binding_differs")
    return completion


def _compose(clients: Mapping[str, object], *, manifest, authority_receipt) -> StageResult:
    digest, cutoff = _admit_manifest(clients, manifest, "compose")
    _attempt_id(authority_receipt)
    release_profile = clients["release_profile"]
    run_id = release_profile.run_id  # type: ignore[attr-defined]
    signal_date = _closed_day(cutoff)
    warehouse = clients["warehouse"]
    entered = False
    try:
        entry = _admitted_capture(clients, _predecessor(clients, manifest))
        if entry["completion_state"] != "succeeded" or entry["observation_window_end"] != cutoff:
            _refuse("capture_entry_not_admitted")
        if entry["policy_digest"] != release_profile.source_policy_digest:  # type: ignore[attr-defined]
            _refuse("capture_policy_differs")
        if release_profile.cutoff != signal_date.isoformat():  # type: ignore[attr-defined]
            _refuse("release_profile_cutoff_differs")
        products = clients.get("products")
        if products is not None:
            products.admit(entry=entry, manifest=manifest, authority_receipt=authority_receipt)
            from .daily_native_clients import bigquery_warehouse

            warehouse = bigquery_warehouse(products.dynamic_client)
        receipt = _read_optional_receipt(run_id, warehouse)
        product_result = None
        if products is not None:
            if receipt is not None:
                prior = _stage_record(clients, manifest["operation_id"], "compose")
                completion = _product_completion(clients, manifest, prior)
                if completion["dynamic_receipt_digest"] != run_receipt_digest(receipt):
                    _refuse("run_receipt_differs")
                return _result(
                    "succeeded",
                    digest,
                    output_digest=canonical_digest(completion),
                    reference=f"products-v1:{manifest['operation_id']}",
                    retry_safe=False,
                )
            entered = True
            product_result = _native(
                lambda: products.run(
                    entry=entry, manifest=manifest, authority_receipt=authority_receipt
                ),
                "products",
            )
        if receipt is None:
            entered = True
            inputs = _native(lambda: dict(clients["composer"](entry, cutoff=cutoff)), "composer")  # type: ignore[operator]
            if "result" not in inputs or _RESERVED_BATCH_FIELDS & set(inputs):
                _unresolved("compose_inputs_invalid")
            result = inputs.pop("result")
            bridge = persistence.build_producing_batch(
                result,
                signal_date=signal_date,
                created_at=_now(clients),
                independence_policy=EXPLICIT_ORIGIN_POLICY,
                telemetry=clients.get("telemetry"),
                **inputs,
            )
            created_at = _now(clients)
            _native(
                lambda: clients["persist"](  # type: ignore[operator]
                    bridge, run_id=run_id, signal_date=signal_date, created_at=created_at
                ),
                "persist",
            )
            receipt = _read_optional_receipt(run_id, warehouse)
            if receipt is None:
                _unresolved("run_receipt_unrecorded")
        if receipt.signal_date != signal_date or receipt.run_id != run_id:
            _refuse("run_receipt_differs")
        if receipt.status != "completed" or receipt.complete_partitions is not True:
            return _result(
                "partial" if receipt.status == "completed" else "failed",
                digest,
                reference=_receipt_reference(run_id),
                retry_safe=False,
            )
        if products is not None:
            completion = _native(
                lambda: products.finish(product_result, dynamic_receipt=receipt),
                "products_completion",
            )
            return _result(
                "succeeded",
                digest,
                output_digest=canonical_digest(completion),
                reference=f"products-v1:{manifest['operation_id']}",
                retry_safe=False,
            )
    except _Unresolved as error:
        return _unknown("compose", digest, error)
    except ValueError as error:
        return _refused("compose", digest, error, entered=entered)
    return _result(
        "succeeded",
        digest,
        output_digest=run_receipt_digest(receipt),
        reference=_receipt_reference(run_id),
        retry_safe=False,
    )


# Certify


def _certified_policy(certifier, run_id: str) -> str:
    """Re-evaluate readiness per candidate under the explicit origin policy; refuse drift."""
    inputs = _native(lambda: certifier.readiness(run_id), "certifier_readiness")
    if not isinstance(inputs, Mapping) or set(inputs) != {
        "candidates",
        "rules",
        "quality_evaluated",
    }:
        _refuse("certify_readiness_inputs_invalid")
    policies = set()
    for item in inputs["candidates"]:
        candidate = item["candidate"]
        result = evaluate_readiness(
            item["records"],
            inputs["rules"],
            quality_evaluated=inputs["quality_evaluated"],
            independence_policy=EXPLICIT_ORIGIN_POLICY,
        )
        if result.state != candidate.get("evidence_state"):
            _refuse("certify_readiness_differs")
        if candidate["evidence_state"] == "ready":
            require_independent_release(candidate, independent_pairs=result.independent_pairs)
        policies.add(result.independence_policy)
    if policies != {EXPLICIT_ORIGIN_POLICY}:
        _refuse("certify_policy_differs")
    return EXPLICIT_ORIGIN_POLICY


def _source_authority(clients: Mapping[str, object], operation_id: str) -> tuple[dict, ...]:
    """The admitted capture entry of this operation, read back from the registry."""
    return (_admitted_capture(clients, _stage_record(clients, operation_id, "capture")),)


def _certification_artifacts(certifier, receipt) -> dict:
    artifacts = _native(lambda: certifier.artifacts(receipt), "certifier_artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != CERTIFICATION_ARTIFACT_FIELDS:
        _refuse("certify_artifacts_invalid")
    return dict(artifacts)


def _certify(clients: Mapping[str, object], *, manifest, authority_receipt) -> StageResult:
    digest, _cutoff = _admit_manifest(clients, manifest, "certify")
    _attempt_id(authority_receipt)
    release = _release_module()
    profile = clients["release_profile"]
    certifier = clients["certifier"]
    warehouse = clients["warehouse"]
    run_id = profile.run_id  # type: ignore[attr-defined]
    try:
        compose = _predecessor(clients, manifest)
        if clients.get("products") is not None:
            completion = _product_completion(clients, manifest, compose)
            compose = {
                **compose,
                "result_reference": _receipt_reference(run_id),
                "output_digest": completion["dynamic_receipt_digest"],
            }
        if compose["result_reference"] != _receipt_reference(run_id):
            _refuse("compose_record_differs")
        recorded = _native(lambda: certifier.read(run_id), "certifier_read")
        if recorded is None:
            receipt = release._read_receipt(run_id, warehouse)
            if run_receipt_digest(receipt) != compose["output_digest"]:
                _refuse("run_receipt_differs")
            release._require_source_authority(
                profile, receipt, _source_authority(clients, manifest["operation_id"])
            )
            release._require_membership_certification(run_id, receipt, warehouse)
            policy = _certified_policy(certifier, run_id)
            inputs = release._build_release_inputs(
                receipt=receipt,
                **_certification_artifacts(certifier, receipt),
                profile=profile,
                run_independence_policy=policy,
            )
            evidence = asdict(inputs.evidence)
            _native(lambda: certifier.record(run_id, evidence), "certifier_record")
            recorded = _native(lambda: certifier.read(run_id), "certifier_read")
            if recorded is None or release.ReleaseEvidence(**dict(recorded)) != inputs.evidence:
                _unresolved("certification_readback_differs")
        evidence_record = release.ReleaseEvidence(**dict(recorded))
        if (
            evidence_record.profile_digest != profile.digest  # type: ignore[attr-defined]
            or evidence_record.independence_policy != profile.independence_policy  # type: ignore[attr-defined]
            or evidence_record.blocked_run_receipt_digest != compose["output_digest"]
        ):
            _refuse("certification_differs")
    except _Unresolved as error:
        return _unknown("certify", digest, error)
    except (ValueError, TypeError) as error:
        return _refused("certify", digest, error, entered=False)
    return _result(
        "succeeded",
        digest,
        output_digest=canonical_digest(dict(recorded)),
        reference=f"certification:{run_id}",
        retry_safe=False,
    )


# Release


class _ReleaseAdapter:
    """The release script's live profile seam, bound to this operation's durable records."""

    def __init__(self, clients: Mapping[str, object], *, operation_id: str) -> None:
        self._clients = clients
        self._operation_id = operation_id

    def compose(self, profile):
        return _source_authority(self._clients, self._operation_id)

    def certify(self, profile):
        release = _release_module()
        record = _stage_record(self._clients, self._operation_id, "certify")
        recorded = self._clients["certifier"].read(profile.run_id)  # type: ignore[attr-defined]
        if recorded is None or canonical_digest(dict(recorded)) != record["output_digest"]:
            _refuse("certification_readback_differs")
        return release.ReleaseEvidence(**dict(recorded))

    def release(self, profile):
        return self._clients["warehouse"]


def release_adapter(clients: object, *, operation_id: str) -> _ReleaseAdapter:
    """The adapter the release script's live profile path consumes for one operation."""
    return _ReleaseAdapter(_validated_clients(clients), operation_id=operation_id)


class _TransactionWatch:
    """Wrap the warehouse so the handler knows whether the release transaction was issued."""

    def __init__(self, warehouse) -> None:
        self._warehouse = warehouse
        self.transaction_issued = False

    def __call__(self, sql: str):
        if isinstance(sql, str) and sql.startswith("BEGIN TRANSACTION"):
            self.transaction_issued = True
        return self._warehouse(sql)


def _release_record(run_id: str, warehouse) -> dict | None:
    release = _release_module()
    rows = list(
        warehouse(
            f"SELECT {', '.join(release.QUALITY_RELEASE_FIELDS)} FROM "
            f"`{release.PROJECT}.{release.DATASET}.{release.RELEASE_RECORD_TABLE}` "
            f"WHERE run_id = '{release._escape(run_id)}'"
        )
    )
    if not rows:
        return None
    if len(rows) != 1:
        _refuse("release_record_ambiguous")
    record = dict(rows[0])
    if record.get("run_id") != run_id:
        _refuse("release_record_differs")
    return record


def _release(clients: Mapping[str, object], *, manifest, authority_receipt) -> StageResult:
    digest, _cutoff = _admit_manifest(clients, manifest, "release")
    if not _authority_present(authority_receipt):
        # The kernel holds here before dispatch; a direct caller without the issued
        # receipt gets the same hold and no warehouse call.
        return _result("release_pending", digest, reference="release_pending", retry_safe=False)
    release = _release_module()
    profile = clients["release_profile"]
    run_id = profile.run_id  # type: ignore[attr-defined]
    watch = _TransactionWatch(clients["warehouse"])
    try:
        certify = _predecessor(clients, manifest)
        if clients.get("products") is not None:
            _product_completion(
                clients, manifest, _stage_record(clients, manifest["operation_id"], "compose")
            )
        if certify["result_reference"] != f"certification:{run_id}":
            _refuse("certify_record_differs")
        record = _release_record(run_id, watch)
        if record is None:
            adapter = _ReleaseAdapter(
                {**clients, "warehouse": watch}, operation_id=manifest["operation_id"]
            )
            release.release_live_profile(
                profile,
                adapter=adapter,
                released_at=_now(clients),
                telemetry=clients.get("telemetry"),
            )
            record = _release_record(run_id, watch)
            if record is None:
                _unresolved("release_record_unrecorded")
        after = release._read_receipt(run_id, watch)
        if after.display_release_state != "enabled" or run_receipt_digest(after) != record.get(
            "run_receipt_digest"
        ):
            _unresolved("release_readback_differs")
        if clients.get("products") is not None:
            compose_record = _stage_record(clients, manifest["operation_id"], "compose")
            product_release = _native(
                lambda: clients["products"].verify_release(
                    run_id=run_id,
                    operation_id=manifest["operation_id"],
                    composition_digest=compose_record["output_digest"],
                    release_record_digest=canonical_digest(record),
                ),
                "product_route_readback",
            )
            return _result(
                "succeeded",
                digest,
                output_digest=canonical_digest(product_release),
                reference=f"products-release-v1:{manifest['operation_id']}",
                retry_safe=False,
            )
    except _Unresolved as error:
        return _unknown("release", digest, error)
    except ValueError as error:
        if watch.transaction_issued:
            return _unknown("release", digest, error)
        return _refused("release", digest, error, entered=False)
    except Exception as error:
        if not watch.transaction_issued:
            raise
        return _unknown("release", digest, error)
    return _result(
        "succeeded",
        digest,
        output_digest=canonical_digest(record),
        reference=f"bq://{release.PROJECT}.{release.DATASET}.{release.RELEASE_RECORD_TABLE}#{run_id}",
        retry_safe=False,
    )


# Builder


def _validated_clients(clients: object) -> dict[str, object]:
    # Legacy injected adapters may omit products; the native factory and guard require them.
    if not isinstance(clients, Mapping) or set(clients) - {"telemetry", "products"} != set(
        CLIENT_FIELDS
    ) - {"products"}:
        raise ValueError("clients_inexact")
    for name in ("clock", "collector", "composer", "persist", "warehouse"):
        if not callable(clients[name]):
            raise ValueError(f"client_invalid:{name}")
    for name, attributes in (
        ("store", ("read_stage",)),
        ("collection_ledger", ("operation",)),
        ("source_runs", ("record", "read")),
        ("capture", ("snapshot", "verified", "writer")),
        ("certifier", ("readiness", "artifacts", "record", "read")),
    ):
        if any(not hasattr(clients[name], attribute) for attribute in attributes):
            raise ValueError(f"client_invalid:{name}")
    if not isinstance(clients["collection_profile"], Mapping):
        raise ValueError("client_invalid:collection_profile")
    return dict(clients)


def build_stage_handlers(
    clients: object, *, profile: object
) -> Mapping[str, Callable[..., StageResult]]:
    """Bind the five stage handlers of one daily profile over the injected native clients."""
    checked = validate_daily_profile(profile)
    bound = _validated_clients(clients)
    release_profile = bound["release_profile"]
    if (
        getattr(release_profile, "source_policy_digest", None) != checked["source_policy_digest"]
        or getattr(release_profile, "is_replay", True)
        or getattr(release_profile, "independence_policy", None) != EXPLICIT_ORIGIN_POLICY
    ):
        raise ValueError("profile_policy_mismatch")
    bound[_PROFILE_KEY] = checked
    functions = {
        "collect": _collect,
        "capture": _capture,
        "compose": _compose,
        "certify": _certify,
        "release": _release,
    }

    def bind(function):
        def handler(*, manifest, authority_receipt) -> StageResult:
            return function(bound, manifest=manifest, authority_receipt=authority_receipt)

        handler.__name__ = function.__name__.lstrip("_")
        if function is _compose and bound.get("products") is not None:

            def validate_reuse(*, manifest, result):
                _product_completion(bound, manifest, result)

            handler.validate_reuse = validate_reuse
        if function is _release and bound.get("products") is not None:

            def validate_release_reuse(*, manifest, result):
                if result["result_reference"] != f"products-release-v1:{manifest['operation_id']}":
                    _refuse("products_release_reference_invalid")
                bound["products"].completion.require_release(
                    manifest["operation_id"], expected_digest=result["output_digest"]
                )

            handler.validate_reuse = validate_release_reuse
        return handler

    return {stage: bind(functions[stage]) for stage in STAGE_ORDER}


__all__ = [
    "CAPTURE_OPERATION",
    "CAPTURE_PRE_COVERAGE_READBACK_FIELDS",
    "CAPTURE_READBACK_FIELDS",
    "CERTIFICATION_ARTIFACT_FIELDS",
    "CLIENT_FIELDS",
    "StageRefusal",
    "build_stage_handlers",
    "release_adapter",
]
