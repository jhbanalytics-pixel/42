"""Admit selected contextual evidence from a protected production capture."""

import copy
import gc
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

from scripts.staging.replay_open_intelligence import _digest

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_context_geo import foreign_local_exclusion
from src.analysis.open_intelligence.general_question_context_identity import (
    _HEX64,
    COLLECTOR_NAMES,
    MARKETS,
    RESULT_FIELDS,
    evidence_groups,
    observation_key,
    origin_authority_projection,
    validate_record,
)
from src.analysis.open_intelligence.general_question_context_queries import _context_requirements
from src.analysis.open_intelligence.general_question_context_selection import (
    FACT_FIELDS,
    LITERAL_METHOD,
    _normalize,
    select_context_facts,
)
from src.analysis.open_intelligence.general_question_planning import validate_stored_question_plan
from src.analysis.open_intelligence.general_question_policy import validate_intake_context
from src.analysis.open_intelligence.general_question_request import validate_question_request
from src.analysis.open_intelligence.production_snapshot import _scope
from src.analysis.open_intelligence.production_snapshot_capture import _read_protected_capture
from src.analysis.open_intelligence.production_snapshot_client import (
    _validate_live_snapshot_metadata,
)
from src.analysis.open_intelligence.production_snapshot_rows import (
    NATIVE_ID_PROJECTION_LEGACY,
    native_identity_state,
)
from src.analysis.open_intelligence.production_snapshot_tables import (
    PROFILE_ID,
    retained_origin_registry,
    retained_v1_result,
)
from src.analysis.open_intelligence.protected_context_registry import _PROTECTED_CONTEXT_PROFILES

_CAPTURE_KEYS = ("candidate", "enriched_index", "enriched", "raw_index", "raw")
_TEMPLATES = {
    "candidate": "protected_context_candidates_v1",
    "partitions": "protected_context_partitions_v1",
    "enriched_index": "protected_context_enriched_index_v1",
    "enriched": "protected_context_enriched_v1",
    "raw_index": "protected_context_raw_index_v1",
    "raw": "protected_context_raw_v1",
}
_OUTPUT_FIELDS = {
    "contract_version",
    "profile_id",
    "snapshot_id",
    "receipts",
    "readings",
    "fulfilled_requirement_ids",
    "limitations",
    "provenance",
    "admission_digest",
}
# The v2 admission carries the identity kernel's derived groups beside the receipts. The
# receipts, readings and provenance keep their v1 bytes; retained v1 admissions validate
# through the v1 shape untouched. v3 adds the origin authority projection as a required
# field, which is why it is a version of its own: a retained v2 record was written without
# that field and keeps validating through the v2 shape, byte for byte, rather than being
# refused for a field that did not exist when it was written.
_IDENTITY_FIELDS_V2 = (
    "observation_keys",
    "origin_groups",
    "independent_origin_count",
    "unknown_origin_count",
    "identity_policy_digest",
    "native_id_projection",
    "units",
)
_IDENTITY_FIELDS_V3 = (*_IDENTITY_FIELDS_V2, "origin_authority_projection")
_ADMISSION_VERSIONS = {
    "protected_context_admission_v1": _OUTPUT_FIELDS,
    "protected_context_admission_v2": _OUTPUT_FIELDS | set(_IDENTITY_FIELDS_V2),
    "protected_context_admission_v3": _OUTPUT_FIELDS | set(_IDENTITY_FIELDS_V3),
}
# The versions that carry the identity kernel's derived groups at all.
_IDENTITY_ADMISSION_VERSIONS = frozenset(
    {"protected_context_admission_v2", "protected_context_admission_v3"}
)
_CURRENT_ADMISSION_VERSION = "protected_context_admission_v3"
# Named refusal lines per admission, well inside the answer's two hundred limitation
# ceiling, so a capture full of rows the kernel cannot identify costs those rows and not
# the answer that was already paid for.
_ROW_REFUSAL_LIMIT = 20
# Native id projection bound per retained context profile. The v3 capture behind the
# current profile removed native_id from its physical rows, so identity there rests on
# the exact (market, platform, source_row_id) tuple and native identity is not projected.


def identity_policy_digest():
    """Digest of the kernel rule constants named below, and the pinned collector list.

    The digest labels a retained record with the policy it was admitted under. It is not
    what enforces that policy: the origin id grammar is applied again on every read of a
    retained record, so a record admitted under an older grammar is refused where it is
    read rather than trusted because its policy digest matched.
    """
    return canonical_digest(
        {
            "policy_version": "general_question_identity_policy_v1",
            "kernel": "general_question_context_identity",
            "markets": sorted(MARKETS),
            "collector_names": sorted(COLLECTOR_NAMES),
            "result_fields": list(RESULT_FIELDS),
            "origin_authority_digest_pattern": _HEX64.pattern,
        }
    )


@dataclass(frozen=True, slots=True)
class LoadedProtectedContext:
    profile_id: str
    capture: dict
    source_binding: dict


def _invalid(error=None):
    raise ValueError("protected_context_invalid") from error


class ForeignLocalEvidenceOnly(ValueError):
    def __init__(self, count):
        super().__init__("corroborated_foreign_local_only")
        self.count = count


class NoMatchingContextEvidence(ValueError):
    pass


def _context(request, plan, intake):
    try:
        scope = {
            key: request[key]
            for key in (
                "client_scope_id",
                "market_scope",
                "brand_config_id",
                "audience_lens_ids",
                "theme_id",
            )
        }
        request = validate_question_request(
            request, scope=scope, policy_digest=request["policy_digest"]
        )
        intake = validate_intake_context(intake, request=request)
        plan = validate_stored_question_plan(plan, request=request, intake=intake)
        if plan["status"] != "ready" or plan["window"] is None:
            _invalid()
        return request, plan, intake
    except (TypeError, ValueError, KeyError) as error:
        _invalid(error)


def _time(value):
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except (TypeError, ValueError, AttributeError) as error:
        _invalid(error)
    if parsed.tzinfo is None:
        _invalid()
    return parsed.astimezone(UTC)


def _profile(value):
    required = {
        "profile_id",
        "cutoff_date",
        "manifest_sha256",
        "consumption_id",
        "result_id",
        "result_digest",
    }
    if type(value) is not dict or set(value) != required:
        _invalid()
    return value


# An injected provider of the bridge loader's native clients, called with the snapshot's
# credentials. None in every deployment: the production provider is served only through
# the configuration switch below.
PRODUCTION_BRIDGE_CLIENTS = None
# The deployment turns bridge reads on by setting this variable to exactly "enabled". Absent,
# empty or any other value leaves them off, so the snapshot refuses every bridge read and no
# bridge cutoff moves the planner ceiling.
BRIDGE_READS_SWITCH = "GENERAL_QUESTION_BRIDGE_READS"


def bridge_clients_provider():
    """The provider of the bridge loader's native clients, or None while reads are off."""
    import os

    if PRODUCTION_BRIDGE_CLIENTS is not None:
        return PRODUCTION_BRIDGE_CLIENTS
    if os.environ.get(BRIDGE_READS_SWITCH) != "enabled":
        return None
    from src.analysis.open_intelligence import bridge_native_clients

    return bridge_native_clients.production_bridge_clients


def _bridge_pins():
    """The pins of every registered bridge capture, in the v1 profile pin shape.

    Empty while no bridge provider is served, so the planner never offers a week that Ask
    would then refuse as bridge_clients_unavailable.
    """
    from src.analysis.open_intelligence.protected_context_registry import bridge_entries

    if bridge_clients_provider() is None:
        return []
    return [
        {key: getattr(entry, key) for key in _PIN_KEYS}
        for entry in bridge_entries()
        if entry.pinned
    ]


_PIN_KEYS = (
    "profile_id",
    "cutoff_date",
    "manifest_sha256",
    "consumption_id",
    "result_id",
    "result_digest",
)


def source_window_hint(as_of, *, retained=None, validate_retained=False):
    """The newest registered capture closed before ``as_of``: the planner's source ceiling.

    A pinned bridge capture is an eligible ceiling beside the v1 profiles once its registry
    row is committed and the production bridge provider is wired. It is a ceiling only: the
    snapshot still admits nothing the bridge loader does not admit for that window.
    """
    closed_before = _time(as_of).date()
    eligible = []
    for raw in (*_PROTECTED_CONTEXT_PROFILES, *_bridge_pins()):
        profile = _profile(raw)
        cutoff = date.fromisoformat(profile["cutoff_date"])
        if cutoff.isoformat() != profile["cutoff_date"]:
            _invalid()
        if cutoff < closed_before:
            eligible.append(profile)
    if validate_retained:
        if retained is None:
            return None
        if (
            type(retained) is not dict
            or retained.get("contract_version") != "general_question_source_window_hint_v1"
        ):
            _invalid()
        pin = {key: value for key, value in retained.items() if key != "contract_version"}
        if _profile(pin) not in eligible:
            _invalid()
        return copy.deepcopy(retained)
    if not eligible:
        return None
    profile = max(eligible, key=lambda item: (item["cutoff_date"], item["profile_id"]))
    return {"contract_version": "general_question_source_window_hint_v1", **copy.deepcopy(profile)}


def _source_binding(profile, completed):
    capture, binding = completed["capture"], completed["binding"]
    snapshot = capture["assembly"]["snapshot"]
    tables = []
    for item in snapshot["table_snapshot_receipts"]:
        tables.append(
            {
                key: copy.deepcopy(item[key])
                for key in (
                    "lane",
                    "snapshot_table",
                    "snapshot_time",
                    "schema_digest",
                    "row_count",
                    "physical_result_digest",
                    "adapted_result_digest",
                )
            }
        )
    value = {
        "profile_id": profile["profile_id"],
        "source_relation_set_id": PROFILE_ID,
        **copy.deepcopy(binding),
        "snapshot_tables": tables,
    }
    value["source_binding_digest"] = canonical_digest(value)
    return value


_ISOLATED_READER_EXECUTION = ContextVar("isolated_reader_execution", default=False)


def load_protected_context_source(
    request, plan, intake, *, objects, native_client, result_reader, approval_reader, now
):
    suspend = _ISOLATED_READER_EXECUTION.get() and gc.isenabled()
    if suspend:
        gc.disable()
    try:
        return _load_protected_context_source(
            request,
            plan,
            intake,
            objects=objects,
            native_client=native_client,
            result_reader=result_reader,
            approval_reader=approval_reader,
            now=now,
        )
    finally:
        if suspend:
            gc.enable()


def _load_protected_context_source(
    request,
    plan,
    intake,
    *,
    objects,
    native_client,
    result_reader,
    approval_reader,
    now,
):
    request, plan, intake = _context(request, plan, intake)
    if not callable(result_reader) or not callable(approval_reader):
        _invalid()
    current = _time(now)
    as_of = _time(request["as_of"])
    if current < as_of:
        _invalid()
    candidates = []
    for raw in _PROTECTED_CONTEXT_PROFILES:
        profile = _profile(raw)
        cutoff = date.fromisoformat(profile["cutoff_date"])
        end = date.fromisoformat(plan["window"]["end"])
        if end <= cutoff:
            candidates.append((cutoff, profile))
    if not candidates:
        _invalid()
    profile = max(candidates, key=lambda item: (item[0], item[1]["profile_id"]))[1]
    try:
        result_rows = result_reader(profile["consumption_id"])
        if type(result_rows) not in (list, tuple) or len(result_rows) != 1:
            _invalid()
        result = retained_v1_result(
            result_rows[0],
            "protected_context_invalid",
            mode="historical_read",
            registry=retained_origin_registry(),
        )
        if (
            result.operation != "source_snapshot_capture"
            or result.status != "succeeded"
            or any(
                getattr(result, key) != profile[key]
                for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
            )
        ):
            _invalid()
        payload = json.loads(result.canonical_result_json)
        capture_client, capture_markets = _scope(
            payload["client_scope_id"], payload["market_scope"]
        )
        if (
            payload["contract_version"] != "open_intelligence_protected_source_snapshot_v1"
            or payload["cutoff_date"] != profile["cutoff_date"]
            or capture_client != request["client_scope_id"]
            or not set(plan["markets"]) <= set(request["market_scope"]) <= set(capture_markets)
        ):
            _invalid()
    except (KeyError, TypeError, ValueError) as error:
        _invalid(error)

    def cached_result_reader(consumption_id):
        if consumption_id == profile["consumption_id"]:
            return result_rows
        return result_reader(consumption_id)

    completed = _read_protected_capture(
        manifest_sha256=profile["manifest_sha256"],
        consumption_id=profile["consumption_id"],
        result_id=profile["result_id"],
        result_digest=profile["result_digest"],
        client_scope_id=capture_client,
        market_scope=list(capture_markets),
        objects=objects,
        result_reader=cached_result_reader,
        approval_reader=approval_reader,
    )
    binding = completed["binding"]
    if (
        any(binding[key] != profile[key] for key in profile if key != "profile_id")
        or binding["client_scope_id"] != capture_client
        or tuple(binding["market_scope"]) != capture_markets
        or _time(binding["captured_at"]) > as_of
        or current >= _time(binding["source_as_of"]) + timedelta(days=90)
        or current >= _time(binding["stored_artifact"]["created_at"]) + timedelta(days=90)
    ):
        _invalid()
    _validate_live_snapshot_metadata(completed["capture"]["assembly"], native_client)
    return LoadedProtectedContext(
        profile["profile_id"],
        copy.deepcopy(completed["capture"]),
        _source_binding(profile, completed),
    )


@dataclass(frozen=True, slots=True)
class LoadedBridgeSource:
    """A bridge v3 capture admitted for reading, with the cells its evidence admits.

    ``cells`` are the completed or empty history cells in the read scope, each checked
    against native evidence; ``limitations`` are the unavailable cells in scope with their
    reason code, never zero rows. On the approval ledger route ``collection_row_runs`` are
    the pipeline run ids a collected row must carry, those of the funded executions the
    receipts bind, and ``collection_evidence`` is the receipt set and funded closes that
    prove them; both are None on the daily chain route.
    """

    profile_id: str
    registry_entry: dict
    binding: dict
    profile: dict
    cells: tuple
    limitations: tuple
    collection_runs: tuple
    result_json: str | None = None
    history_completion_set: dict | None = None
    collection_row_runs: tuple | None = None
    collection_evidence: dict | None = None


_BRIDGE_READ_ARTIFACTS = (
    "bridge_policy",
    "capture_plan",
    "collection_receipt_set",
    "history_completion_set",
    "temporal_rules",
)
_BRIDGE_MANIFEST_INPUTS = frozenset(
    {
        *_BRIDGE_READ_ARTIFACTS,
        "build_provenance",
        "capture_contract",
        "cost_policy",
        "daily_profile",
        "operation_context",
        "recovery_context",
        "source_metadata",
        "storage_policy",
    }
)
_BRIDGE_CLONE_RETENTION = timedelta(days=90)


def _bridge_thaw(value):
    if isinstance(value, dict | MappingProxyType):
        return {key: _bridge_thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_bridge_thaw(item) for item in value]
    return value


def _bridge_instant(value):
    """A timezone aware instant as the bridge contracts spell it."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bridge_source_invalid")
        parsed = value
    else:
        if type(value) is not str:
            raise ValueError("bridge_source_invalid")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("bridge_source_invalid")
    return parsed.astimezone(UTC)


def _bridge_text(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def load_bridge_history_source(
    *,
    window_end,
    request_as_of,
    client_scope_id,
    market_scope,
    capture_clients,
    read_input,
    read_grant,
    evidence,
    now,
    ledger_reader=None,
    generation_loader=None,
    read_native_job=None,
    read_capture_facts=None,
    read_clone=None,
):
    """Admit the newest pinned bridge capture covering ``window_end`` for one read.

    A daily chain row names the derivation; ``capture_clients`` read its chain through the
    native chain reader and ``read_grant`` reads the recurring grant it was authorised
    under. A ledger row names one consumed approval ledger execution; ``ledger_reader``
    returns its chain rows by consumption id, ``generation_loader`` admits the generation
    they record (the packaged catalogue when None) and ``read_native_job`` reads each clone's
    creating job. Either way the manifest the row pins digests every artifact ``read_input``
    returns, every collection receipt and every completed or empty history entry is checked
    against the native evidence ``evidence`` resolves, and the read binding is validated
    against the row's own authority and ``now``. On the ledger route a collection receipt is
    bound instead to the funded pilot execution result ``ledger_reader`` returns by its
    result reference and to that execution, which ``evidence`` reads natively. Clones with
    neither a recorded chain nor a consumed approval are refused. On both routes ``read_capture_facts`` reads the stored
    capture facts the payload's stored artifact names, ``read_native_job`` each creation
    job and ``read_clone`` each clone's table resource at request time, so a clone replaced
    after capture is refused.
    """
    from src.analysis.open_intelligence.daily_execution_authority import (
        DailyAuthorityIntegrityError,
        DailyAuthorityUnavailable,
    )

    try:
        return _load_bridge_history_source(
            window_end=window_end,
            request_as_of=request_as_of,
            client_scope_id=client_scope_id,
            market_scope=market_scope,
            capture_clients=capture_clients,
            read_input=read_input,
            read_grant=read_grant,
            evidence=evidence,
            now=now,
            ledger_reader=ledger_reader,
            generation_loader=generation_loader,
            read_native_job=read_native_job,
            read_capture_facts=read_capture_facts,
            read_clone=read_clone,
        )
    except _BridgeUncovered as error:
        raise ValueError("bridge_source_uncovered") from error
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        DailyAuthorityIntegrityError,
        DailyAuthorityUnavailable,
    ) as error:
        raise ValueError("bridge_source_invalid") from error


class _BridgeUncovered(Exception):
    pass


def _load_bridge_history_source(
    *,
    window_end,
    request_as_of,
    client_scope_id,
    market_scope,
    capture_clients,
    read_input,
    read_grant,
    evidence,
    now,
    ledger_reader,
    generation_loader,
    read_native_job,
    read_capture_facts,
    read_clone,
):

    from src.analysis.open_intelligence.protected_context_registry import (
        bridge_authority,
        bridge_entries,
        bridge_registry_row,
    )
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        check_daily_completion_binding,
        check_daily_source_run_binding,
        resolve_funded_collection,
    )

    def require(condition):
        if not condition:
            raise ValueError("bridge_source_invalid")

    current = _bridge_instant(now)
    as_of = _bridge_instant(request_as_of)
    require(as_of <= current and callable(read_input))
    require(type(window_end) is str and date.fromisoformat(window_end).isoformat() == window_end)
    end = date.fromisoformat(window_end)
    require(
        type(market_scope) is list
        and bool(market_scope)
        and market_scope == sorted(set(market_scope))
    )
    covering = [entry for entry in bridge_entries() if end <= date.fromisoformat(entry.cutoff_date)]
    if not covering:
        raise _BridgeUncovered()
    entry = covering[0]
    row = bridge_registry_row(entry)
    # The row's own authority decides the route; neither route stands in for the other.
    if bridge_authority(entry) == "daily_chain":
        source = _bridge_chain_source(
            entry,
            row,
            capture_clients=capture_clients,
            read_input=read_input,
            read_grant=read_grant,
        )
    else:
        source = _bridge_ledger_source(
            entry,
            row,
            ledger_reader=ledger_reader,
            generation_loader=generation_loader,
            read_input=read_input,
        )
    profile, artifacts = source["profile"], source["artifacts"]
    readback = _bridge_clone_readback(
        profile,
        source["payload"],
        read_capture_facts=read_capture_facts,
        read_native_job=read_native_job,
        read_clone=read_clone,
    )
    require(current < _bridge_instant(profile["history_snapshot_as_of"]) + _BRIDGE_CLONE_RETENTION)
    runs = []
    row_runs = evidence_record = None
    receipts = artifacts["collection_receipt_set"]["receipts"]
    if source["read_funded_chain"] is None:
        for item in receipts:
            derivation_id, clients = evidence.chain_source(item["result_ref"])
            check_daily_source_run_binding(
                item,
                derivation_id=derivation_id,
                clients=clients,
                read_source_run=evidence.read_source_run,
            )
            runs.append(item["run_id"])
    else:
        # The ledger route binds each receipt to the funded pilot execution result it names
        # and to that execution, which must have succeeded inside the capture window: from
        # the observation window's end to the instant the collection clones snapshot.
        # Each bound execution's funded close names the pipeline run its collected rows
        # carry; only rows of those runs are read and admitted.
        bound = [
            resolve_funded_collection(
                item,
                read_chain=source["read_funded_chain"],
                read_funded_execution=evidence.read_funded_execution,
                window_start=_bridge_instant(profile["observation_window_end"]),
                window_end=_bridge_instant(profile["collection_snapshot_as_of"]),
                generation_loader=generation_loader,
            )
            for item in receipts
        ]
        runs.extend(item["execution_id"] for item in bound)
        row_runs = tuple(sorted({item["pipeline_run_id"] for item in bound}))
        evidence_record = {
            "collection_receipt_set": copy.deepcopy(artifacts["collection_receipt_set"]),
            "funded_results": [item["result_json"] for item in bound],
        }
    scope = set(market_scope)
    cells = []
    limitations = []
    for item in artifacts["history_completion_set"]["entries"]:
        cell = {key: item[key] for key in ("lane", "market", "product_date")}
        if item["state"] == "unavailable":
            if item["market"] in scope:
                limitations.append({**cell, "reason_code": item["reason_code"]})
            continue
        derivation_id, clients = evidence.chain_source(item["result_ref"])
        check_daily_completion_binding(
            item,
            derivation_id=derivation_id,
            operation_id=evidence.completion_operation(item["result_ref"]),
            clients=clients,
            read_completion=evidence.read_completion,
            read_product_rows=evidence.read_product_rows,
        )
        if item["market"] in scope:
            cells.append(
                {
                    **cell,
                    "state": item["state"],
                    "row_count": item["row_count"],
                    "completion_entry_digest": canonical_digest(item),
                    "available_at": item["available_at"],
                }
            )
    binding = {
        "contract_version": "source_bridge_read_binding_v1",
        "registry_entry_digest": canonical_digest(row),
        "profile_id": profile["profile_id"],
        "result_id": source["result_id"],
        "result_digest": source["result_digest"],
        "snapshot_digest": source["snapshot_digest"],
        "request_as_of": _bridge_text(as_of),
        "purpose": "current_context",
        "product_date": None,
        "client_scope_id": client_scope_id,
        "market_scope": list(market_scope),
        "relation_bindings": profile["relation_bindings"],
        "collection_receipt_set_digest": profile["collection_receipt_set_digest"],
        "history_completion_set_digest": profile["history_completion_set_digest"],
        "temporal_rules_version": profile["temporal_rules_version"],
        "temporal_rules_digest": profile["temporal_rules_digest"],
        "available_at": _bridge_text(_bridge_instant(source["available_at"])),
        "current_operation_id": None,
        "current_output_set_digest": None,
    }
    checked = source["validate"](binding, now=current, **readback)
    return LoadedBridgeSource(
        profile_id=profile["profile_id"],
        registry_entry=copy.deepcopy(row),
        binding=copy.deepcopy(checked),
        profile=copy.deepcopy(profile),
        cells=tuple(cells),
        limitations=tuple(limitations),
        collection_runs=tuple(runs),
        result_json=source["result_json"],
        history_completion_set=copy.deepcopy(artifacts["history_completion_set"]),
        collection_row_runs=row_runs,
        collection_evidence=evidence_record,
    )


def _bridge_chain_source(entry, row, *, capture_clients, read_input, read_grant):
    """The daily chain route: the chain the row's derivation names, and its grant."""
    import hashlib

    from src.analysis.open_intelligence.brain_contract import canonical_bytes
    from src.analysis.open_intelligence.daily_execution_authority import (
        read_daily_execution_chain,
    )
    from src.analysis.open_intelligence.daily_execution_contracts import (
        canonical_json_object,
        input_artifact_digests,
    )
    from src.analysis.open_intelligence.source_estate_bridge import (
        CAPTURE_OPERATION,
        validate_source_read_binding,
    )
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        parse_bridge_artifacts,
    )

    def sha256(raw):
        return hashlib.sha256(raw).hexdigest()

    def require(condition):
        if not condition:
            raise ValueError("bridge_source_invalid")

    chain = read_daily_execution_chain(derivation_id=entry.derivation_id, clients=capture_clients)
    derivation = chain.derivation
    require(
        derivation["derivation_id"] == entry.derivation_id
        and chain.operation_context["operation"] == CAPTURE_OPERATION
        and all(
            chain.result[key] == row[key]
            for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
        )
    )
    # The manifest is the one the registry row pins, and it digests every input read here.
    manifest_json = derivation["canonical_manifest_json"]
    require(type(manifest_json) is str)
    require(
        sha256(manifest_json.encode("utf-8"))
        == derivation["manifest_sha256"]
        == entry.manifest_sha256
    )
    manifest, _ = canonical_json_object(manifest_json, "bridge_source_invalid")
    digests = input_artifact_digests(manifest)
    require(set(digests) == _BRIDGE_MANIFEST_INPUTS)
    context_json = canonical_bytes(_bridge_thaw(chain.operation_context))
    require(
        derivation["canonical_operation_context_json"] == context_json.decode("utf-8")
        and sha256(context_json)
        == digests["operation_context"]
        == derivation["operation_context_sha256"]
    )
    values = {}
    for name in _BRIDGE_READ_ARTIFACTS:
        raw = read_input(name, digests[name])
        require(type(raw) is bytes and sha256(raw) == digests[name])
        values[name], _ = canonical_json_object(raw, "bridge_source_invalid")
    plan = values.pop("capture_plan")
    require(plan.get("contract_version") == "open_intelligence_protected_capture_plan_v3")
    profile = plan["snapshot_plan"]
    artifacts = parse_bridge_artifacts(values)
    payload = _bridge_thaw(chain.operation_payload)

    def validate(binding, *, now, **readback):
        # The recurring grant is read by the digest the chain was authorized under; the
        # validator checks that digest and loads the committed registry row itself.
        require(callable(read_grant))
        grant = read_grant(chain.operation_context["authorizing_grant_digest"])
        return validate_source_read_binding(
            binding,
            profile=profile,
            artifacts=artifacts,
            capture_chain=chain,
            grant=grant,
            now=now,
            **readback,
        )

    return {
        "profile": profile,
        "artifacts": artifacts,
        "payload": payload,
        "result_id": chain.result["result_id"],
        "result_digest": chain.result["result_digest"],
        "snapshot_digest": payload["snapshot_digest"],
        "available_at": chain.effective_available_at,
        "result_json": chain.result["canonical_result_json"],
        "validate": validate,
        "read_funded_chain": None,
    }


def _bridge_ledger_source(entry, row, *, ledger_reader, generation_loader, read_input):
    """The approval ledger route: the consumed execution the row's consumption id names."""
    from src.analysis.open_intelligence.bridge_ledger_binding import (
        read_ledger_capture,
        validate_ledger_read_binding,
    )

    if not callable(ledger_reader):
        raise ValueError("bridge_source_invalid")
    capture = read_ledger_capture(
        consumption_id=entry.consumption_id,
        ledger_rows=ledger_reader(entry.consumption_id),
        generation_loader=generation_loader,
        read_input=read_input,
    )
    result = capture.result
    if any(result[key] != row[key] for key in _BRIDGE_PINS):
        raise ValueError("bridge_source_invalid")

    def validate(binding, *, now, **readback):
        return validate_ledger_read_binding(binding, capture=capture, now=now, **readback)

    return {
        "profile": _bridge_thaw(capture.profile),
        "artifacts": _bridge_thaw(capture.artifacts),
        "payload": _bridge_thaw(capture.payload),
        "result_id": result["result_id"],
        "result_digest": result["result_digest"],
        "snapshot_digest": capture.payload["snapshot_digest"],
        "available_at": result["completed_at"],
        "result_json": result["canonical_result_json"],
        "validate": validate,
        "read_funded_chain": ledger_reader,
    }


_BRIDGE_PINS = ("manifest_sha256", "consumption_id", "result_id", "result_digest")


def _bridge_clone_readback(profile, payload, *, read_capture_facts, read_native_job, read_clone):
    """Read the stored capture facts, every creation job and every clone table natively.

    The names come from the capture's own records and the profile's relations; the bridge
    validators compare what is read with the digests the capture recorded.
    """
    if not (callable(read_capture_facts) and callable(read_native_job) and callable(read_clone)):
        raise ValueError("bridge_source_invalid")
    records = payload["creation_records"]
    if type(records) is not list or any(type(record) is not dict for record in records):
        raise ValueError("bridge_source_invalid")
    return {
        "capture_facts": read_capture_facts(copy.deepcopy(payload["stored_artifact"])),
        "native_jobs": {record["job_id"]: read_native_job(record["job_id"]) for record in records},
        "clone_metadata": {
            relation["destination_table"]: read_clone(relation["destination_table"])
            for relation in profile["relation_bindings"]
        },
    }


_FACT_LIMIT = 200


def _selection_text(payload):
    """The full source text the selector matches; the rendered excerpt is clipped later."""
    return " ".join(part for part in (payload.get("text"), payload.get("title")) if part)


def _fact_key(fact):
    return (fact["market"], fact["row_id"], fact["content_digest"])


def _allocate_context_facts(facts, plan, *, limit):
    """Pair coverage over geo eligible rows through the pure selector, one slot per fact."""
    requirements = [
        {
            "requirement_id": item["requirement_id"],
            "mandatory": item["mandatory"] is True,
            "search_terms": [term for term in item["search_terms"] if term.strip()],
        }
        for item in _context_requirements(plan)
    ]
    rows = [
        {
            **{field: fact[field] for field in FACT_FIELDS},
            "excerpt": fact["selection_text"],
            "source_label": fact["source_label"] or "unknown",
            "platform": fact["platform"] or "unknown",
        }
        for fact in facts
    ]
    try:
        return select_context_facts(rows, requirements, list(plan["markets"]), limit=limit)
    except ValueError as error:
        _invalid(error)


def _coverage_limitations(selection, unlimited):
    """Coverage lines: the cap size, the mandatory pairs the cap starved, the absent pairs.

    A pair uncovered even without a cap has no eligible record; that is absence, named so a
    missing market statement downstream can cite it, and it is kept apart from loss, which is
    a pair a larger capacity would have covered.
    """
    kept = len(selection["facts"])
    lines = []
    if selection["omitted_eligible_count"]:
        lines.append(
            f"Contextual evidence selection was capped at {kept} of "
            f"{kept + selection['omitted_eligible_count']} eligible records."
        )
    absent = {
        (pair["requirement_id"], pair["market"]) for pair in unlimited["uncovered_required_pairs"]
    }
    starved = [
        f"{pair['requirement_id']} in {pair['market']}"
        for pair in selection["uncovered_required_pairs"]
        if (pair["requirement_id"], pair["market"]) not in absent
    ]
    if starved:
        lines.append(
            "Capacity was exhausted before these mandatory requirement and market pairs were "
            f"covered: {', '.join(starved)}."
        )
    if absent:
        lines.append(
            "No eligible record was retrieved for these mandatory requirement and market pairs: "
            + ", ".join(
                f"{requirement_id} in {market}" for requirement_id, market in sorted(absent)
            )
            + "."
        )
    return lines


def _shown(signals, requirement_id, fact):
    """Coverage is reported only where the rendered excerpt shows the matched term.

    The row keeps its slot from the full text match; a term clipped out of the excerpt does not
    fulfil a requirement the citation cannot show.
    """
    shown = _normalize(fact["excerpt"])
    return any(
        signal["requirement_id"] == requirement_id
        and (
            signal["method"] not in (LITERAL_METHOD, "alias") or _normalize(signal["term"]) in shown
        )
        for signal in signals
    )


def _identity_record(fact):
    """The producer fields of one admitted row, carrying origin only where it was projected.

    A row of a capture whose physical schema has no origin column carries no origin field
    at all, which is not the same as a row that carried the column empty.
    """
    return {
        "market": fact["market"],
        "platform": fact["platform"],
        "source_row_id": fact["row_id"],
        "content_digest": fact["content_digest"],
        **(
            {
                "origin_id": fact["origin_id"],
                "origin_authority_digest": fact["origin_authority_digest"],
            }
            if fact["origin_projected"]
            else {}
        ),
    }


def _row_refusal(fact, reason):
    return (
        f"Refused admitted row {fact['market']}/{fact['platform']}/{fact['row_id']!r} "
        f"whose identity the kernel cannot establish ({reason}). "
        "The remaining admitted rows proceed."
    )


def _claimed(value):
    """A hashable stand in for a claimed field, so an unreadable value still compares."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _identifiable_facts(facts):
    """Keep the rows the identity kernel can establish, and name each row it refuses.

    One malformed row is that row's defect, not the whole admission's: it is refused by
    name and every other admitted row proceeds with its receipt and its coverage. Rows that
    contradict each other over one observation are all refused, because none of them is the
    one to keep, and again the rest proceed. An admission left with no row has nothing to
    admit and refuses through the ordinary path.

    A row refused on its own terms still claims the observation it names, so the
    contradiction sweep runs over every row that carries a readable observation key and not
    only over the rows that validated. Otherwise a contested observation reaches the answer
    on its one surviving claimant, with nothing saying it was contested at all. A row that
    already has its own refusal keeps it, because its own defect is the more precise reason.

    The refusal lines are capped and the remainder published as a count: the answer refuses
    a snapshot carrying more than two hundred limitations, so a line for each of two hundred
    ordinary malformed rows would lose the whole answer rather than those rows.
    """
    refusals, claims = {}, {}
    for index, fact in enumerate(facts):
        record = _identity_record(fact)
        try:
            key = observation_key(record)
        except ValueError as error:
            refusals[index] = _row_refusal(fact, error)
            continue
        try:
            validate_record(record)
        except ValueError as error:
            refusals[index] = _row_refusal(fact, error)
        claims.setdefault(key, []).append((index, record))
    for key, rows in claims.items():
        digests = {_claimed(record["content_digest"]) for _index, record in rows}
        origins = {_claimed(record.get("origin_id")) for _index, record in rows} - {None}
        if len(digests) > 1 or len(origins) > 1:
            field = "content_digest" if len(digests) > 1 else "origin_id"
            for index, _record in rows:
                refusals.setdefault(
                    index,
                    _row_refusal(facts[index], f"{field}: observation {key!r} is claimed two ways"),
                )
    kept = [fact for index, fact in enumerate(facts) if index not in refusals]
    named = [refusals[index] for index in sorted(refusals)]
    if len(named) > _ROW_REFUSAL_LIMIT:
        remainder = len(named) - _ROW_REFUSAL_LIMIT
        named = [
            *named[:_ROW_REFUSAL_LIMIT],
            f"Refused {remainder} further admitted rows whose identity the kernel cannot "
            f"establish, beyond the {_ROW_REFUSAL_LIMIT} named above. The remaining admitted "
            "rows proceed.",
        ]
    return kept, named


def _identity(facts, profile_id, version):
    """Derived groups over the admitted rows, named units, no prevalence claim.

    Observation identity comes from the producer fields the receipts already carry; the
    origin fields come from the record when present and are null otherwise, so a record
    without origin proof counts as unknown, never as independent.
    """
    projection = {
        entry["profile_id"]: NATIVE_ID_PROJECTION_LEGACY for entry in _PROTECTED_CONTEXT_PROFILES
    }.get(profile_id)
    if projection is None:
        _invalid()
    records = [_identity_record(fact) for fact in facts]
    try:
        groups = evidence_groups(records)
        states = [
            native_identity_state(
                {"market": fact["market"], "id": fact["row_id"], "platform": fact["platform"]},
                projection_version=projection,
            )["state"]
            for fact in facts
        ]
    except ValueError as error:
        _invalid(error)
    families = {fact["source_family"] for fact in facts if fact["source_family"] is not None}
    units = {
        "collected_records": len(facts),
        "unique_observations": len(groups["observation_keys"]),
        "source_families": len(families),
        "verified_independent_origins": groups["independent_origin_count"],
        "unknown_origins": groups["unknown_origin_count"],
    }
    return {
        "observation_keys": [list(key) for key in groups["observation_keys"]],
        "origin_groups": {
            origin: [list(key) for key in keys] for origin, keys in groups["origin_groups"].items()
        },
        "independent_origin_count": groups["independent_origin_count"],
        "unknown_origin_count": groups["unknown_origin_count"],
        "identity_policy_digest": identity_policy_digest(),
        "native_id_projection": {
            "version": projection,
            "identity_states": {state: states.count(state) for state in sorted(set(states))},
        },
        **(
            {"origin_authority_projection": origin_authority_projection(records)}
            if version == "protected_context_admission_v3"
            else {}
        ),
        "units": units,
    }, (
        f"Coverage counts name their units: {units['collected_records']} collected records, "
        f"{units['unique_observations']} unique observations, {units['source_families']} source "
        f"families, {units['verified_independent_origins']} verified independent origins; "
        f"{units['unknown_origins']} observations carry no verified origin. None of these counts "
        "is population prevalence."
    )


def _decode_context_rows(rows, query):
    from src.analysis.open_intelligence.general_question_context_queries import (
        decode_context_rows,
    )

    return decode_context_rows(rows, query=query)


def _collapse_identical_rows(decoded, wire_rows):
    """Count byte identical evidence rows of one lane once at the match count check.

    The producer can write one row twice, and the evidence query then returns both copies,
    each counting both. Copies whose wire rows are byte for byte the same are one row: the
    first is kept, and it passes the check when its match count equals the number of
    copies. Copies whose match count differs from the number of copies are a real double
    write, since the source counts the rows it holds, and refuse the whole admission. A row
    that shares a market and id with another but differs in any byte keeps its own match
    count, so it still refuses the whole admission at the same check.

    The comparison is over the wire rows as captured, payload string included, and needs
    them to line up one to one with the decoded records, which the evidence decoder always
    gives. When they do not, decoded records are never merged: two JSON spellings of one
    row decode alike but are different bytes. Records that repeat no identity pass
    through unchanged, and a repeated identity refuses the whole admission.
    """
    records = decoded["records"]
    wire = list(wire_rows[1:]) if type(wire_rows) in (list, tuple) else []
    aligned = len(wire) == len(records) and all(
        type(row) is dict
        and (row.get("lane"), row.get("market"), row.get("id"))
        == (record["lane"], record["market"], record["id"])
        for row, record in zip(wire, records, strict=False)
    )
    if not aligned:
        identities = [(record["lane"], record["market"], record["id"]) for record in records]
        if len(set(identities)) != len(identities):
            _invalid()
        return decoded
    keys = [canonical_bytes(row) for row in wire]
    copies = {}
    for key in keys:
        copies[key] = copies.get(key, 0) + 1
    kept, seen = [], set()
    for key, record in zip(keys, records, strict=True):
        if key in seen:
            continue
        seen.add(key)
        if copies[key] > 1:
            if record["match_count"] != copies[key]:
                _invalid()
            record = {**record, "match_count": 1}
        kept.append(record)
    return {**decoded, "records": kept}


def _query_material(queries, captures, query_records, source_binding):
    capture_keys = (*_CAPTURE_KEYS, "partitions") if "partitions" in queries else _CAPTURE_KEYS
    if any(
        type(value) is not dict or set(value) != set(capture_keys)
        for value in (queries, captures, query_records)
    ):
        _invalid()
    output, bindings = {}, {}
    for key in capture_keys:
        query, capture, record = queries[key], captures[key], query_records[key]
        allowed_templates = {_TEMPLATES[key]}
        if key in {"enriched_index", "enriched", "raw_index", "raw"}:
            allowed_templates.update(
                _TEMPLATES[key].replace("_v1", version) for version in ("_v2", "_v3", "_v4")
            )
        if (
            type(capture) is not dict
            or set(capture) != {"rows", "receipt"}
            or type(record) is not dict
            or set(record) != {"reservation", "receipt"}
            or capture["receipt"] != record["receipt"]
            or query.template_id not in allowed_templates
            or query.source_binding_digest != source_binding["source_binding_digest"]
            or query.source_snapshot_digest != source_binding["snapshot_digest"]
            or query.profile_id != source_binding["profile_id"]
        ):
            _invalid()
        reservation, receipt = record["reservation"], record["receipt"]
        expected = {
            "template_id": query.template_id,
            "sql_digest": query.sql_digest,
            "parameters_digest": query.parameters_digest,
        }
        if any(
            reservation.get(field) != value or receipt.get(field) != value
            for field, value in expected.items()
        ):
            _invalid()
        if receipt.get("query_state") != "succeeded" or receipt.get(
            "result_digest"
        ) != canonical_digest(capture["rows"]):
            _invalid()
        if key == "partitions":
            from src.analysis.open_intelligence.general_question_context_queries import (
                decode_context_partition_rows,
            )

            output[key] = decode_context_partition_rows(capture["rows"], query=query)
        else:
            output[key] = _decode_context_rows(capture["rows"], query)
            if key in ("enriched", "raw"):
                output[key] = _collapse_identical_rows(output[key], capture["rows"])
        bindings[key] = {
            **expected,
            "job_id": receipt.get("job_id"),
            "result_digest": receipt["result_digest"],
            "source_binding_digest": query.source_binding_digest,
            "source_snapshot_digest": query.source_snapshot_digest,
            "profile_id": query.profile_id,
        }
        if query.template_id.endswith("_v4"):
            value = next(
                (item.value for item in query.parameters if item.name == "parent_context_digest"),
                None,
            )
            if value is None:
                _invalid()
            bindings[key]["parent_context_digest"] = value
    return output, bindings


def _project(request, plan, source_binding, material, bindings, version, profile_id, caps=None):
    continuity_versions = [
        bindings[key]["template_id"].endswith("_v4")
        for key in ("enriched_index", "enriched", "raw_index", "raw")
    ]
    if any(continuity_versions) and not all(continuity_versions):
        _invalid()
    continuity = all(continuity_versions)
    parent_digest = bindings["enriched"].get("parent_context_digest", "none")
    if continuity and any(
        bindings[key].get("parent_context_digest") != parent_digest
        for key in ("enriched_index", "raw_index", "raw")
    ):
        _invalid()
    geo_versions = [
        bindings[key]["template_id"].endswith(("_v3", "_v4"))
        for key in ("enriched_index", "enriched", "raw_index", "raw")
    ]
    if any(geo_versions) and not all(geo_versions):
        _invalid()
    geo_policy = all(geo_versions)
    candidates = material["candidate"]["records"]
    sample_keys = {
        (row["market"], row_id) for row in candidates for row_id in row["sample_row_ids"]
    }
    if "partitions" in material:
        supplement = all(
            bindings[key]["template_id"].endswith(("_v2", "_v3", "_v4"))
            for key in ("enriched_index", "enriched", "raw_index", "raw")
        )
        if (sample_keys and not supplement) or (
            not any(term for req in _context_requirements(plan) for term in req["search_terms"])
            and not (continuity and parent_digest != "none")
        ):
            _invalid()
        sample_keys = {
            (row["market"], row["id"])
            for key in ("enriched_index", "raw_index")
            for row in material[key]["records"]
        }
    enriched = material["enriched"]["records"]
    enriched_keys = {(row["market"], row["id"]) for row in enriched}
    evidence = [*enriched, *material["raw"]["records"]]
    # Byte identical copies were counted once when decoded, so the truncation line counts
    # the unique rows kept and takes the copies it dropped out of the matching count.
    limitations = [
        f"{key.capitalize()} selection was truncated to {len(material[key]['records'])} of "
        f"{material[key]['full_matching_count'] - material[key]['candidate_count'] + len(material[key]['records'])} matching records."
        for key in _CAPTURE_KEYS
        if material[key]["overflow"]
    ]
    if continuity and parent_digest == "none":
        limitations.append(
            "Authenticated parent continuity was not supplied; prior conversation prose is context only."
        )
    if any((row["market"], row["id"]) in enriched_keys for row in material["raw"]["records"]):
        _invalid()
    excerpt_terms = [
        term.strip().lower()
        for requirement in sorted(
            _context_requirements(plan), key=lambda item: not item["mandatory"]
        )
        for term in requirement["search_terms"]
    ]
    facts = []
    foreign_local_excluded = 0
    start = datetime.combine(date.fromisoformat(plan["window"]["start"]), datetime.min.time(), UTC)
    end = datetime.combine(
        date.fromisoformat(plan["window"]["end"]) + timedelta(days=1), datetime.min.time(), UTC
    )
    as_of = _time(request["as_of"])
    for row in evidence:
        lane = row["lane"]
        if lane not in ("enriched_content", "raw_content"):
            _invalid()
        payload = row["payload"]
        published = None if payload["published_at"] is None else _time(payload["published_at"])
        collected = _time(payload["collected_at"])
        observed = collected if published is None else published
        if (
            row["match_count"] != 1
            or (row["market"], row["id"]) not in sample_keys
            or row["market"] not in plan["markets"]
            or not start <= observed < end
            or (published is not None and published > as_of)
            or collected > as_of
        ):
            _invalid()
        if geo_policy and foreign_local_exclusion(payload, request, plan):
            foreign_local_excluded += 1
            continue
        full_excerpt = payload.get("text") or payload.get("title")
        if type(full_excerpt) is not str or not full_excerpt.strip():
            _invalid()
        excerpt = full_excerpt[:4000]
        if "partitions" in material:
            selected = None
            for term in excerpt_terms:
                for source_text in (payload.get("text"), payload.get("title")):
                    if not source_text:
                        continue
                    for match in re.finditer(re.escape(term), source_text, flags=re.IGNORECASE):
                        if term in match.group().lower():
                            begin = max(0, match.start() - 1000)
                            selected = source_text[begin : begin + 4000]
                            full_excerpt = source_text
                            break
                    if selected is not None:
                        break
                if selected is not None:
                    break
            if selected is not None:
                excerpt = selected
        facts.append(
            {
                "lane": lane,
                "market": row["market"],
                "row_id": row["id"],
                "source_label": payload["source"],
                "source_family": payload.get("source_family"),
                "platform": payload["platform"],
                "author": None
                if payload.get("author_handle") == ""
                else payload.get("author_handle"),
                "url": None if payload.get("url") == "" else payload.get("url"),
                "published_at": None if published is None else published.isoformat(),
                "collected_at": collected.isoformat(),
                "excerpt": excerpt,
                "content_digest": _digest(payload),
                "excerpt_truncated": len(excerpt) != len(full_excerpt),
                "selection_text": _selection_text(payload),
                "origin_id": payload.get("origin_id"),
                "origin_authority_digest": payload.get("origin_authority_digest"),
                "origin_projected": "origin_id" in payload or "origin_authority_digest" in payload,
            }
        )
    if foreign_local_excluded:
        limitations.append(
            f"Excluded {foreign_local_excluded} corroborated foreign-local source records from this market-scoped answer."
        )
    if version == _CURRENT_ADMISSION_VERSION:
        facts, identity_refusals = _identifiable_facts(facts)
        limitations.extend(identity_refusals)
    selection = _allocate_context_facts(facts, plan, limit=_FACT_LIMIT)
    limitations.extend(
        _coverage_limitations(
            selection,
            _allocate_context_facts(facts, plan, limit=len(facts))
            if selection["omitted_eligible_count"]
            else selection,
        )
    )
    if caps is not None:
        caps.update(_selection_caps(selection, material))
    selected = {_fact_key(fact) for fact in selection["facts"]}
    # Rows without a retrieval signal stay admitted as context inside the capacity the
    # selector left; when it filled the limit, the omitted rows are the ones it ranked out.
    facts = [fact for fact in facts if _fact_key(fact) in selected] + [
        fact for fact in facts if _fact_key(fact) not in selected
    ][: _FACT_LIMIT - len(selected)]
    for fact in facts:
        del fact["selection_text"]
    facts.sort(
        key=lambda item: (
            item["market"],
            item["published_at"] or item["collected_at"],
            item["lane"],
            item["row_id"],
        )
    )
    positions = {_fact_key(fact): index for index, fact in enumerate(facts)}
    coverage = {}
    for requirement_id, indices in selection["coverage"].items():
        matched = sorted(
            positions[_fact_key(fact)]
            for index, fact in enumerate(selection["facts"])
            if index in indices
            and _shown(fact["retrieval_signals"], requirement_id, facts[positions[_fact_key(fact)]])
        )
        if matched:
            coverage[requirement_id] = matched
    if not coverage:
        if foreign_local_excluded and not facts:
            raise ForeignLocalEvidenceOnly(foreign_local_excluded)
        if not facts and all(
            material[key]["candidate_count"] == material[key]["full_matching_count"] == 0
            for key in ("enriched_index", "enriched", "raw_index", "raw")
        ):
            raise NoMatchingContextEvidence("no_matching_evidence")
        _invalid()
    snapshot_id = "gqs_" + canonical_digest(
        {
            "request_digest": request["request_digest"],
            "plan_digest": plan["plan_digest"],
            "source_binding_digest": source_binding["source_binding_digest"],
            "query_result_digests": [bindings[key]["result_digest"] for key in bindings],
            "fact_digests": [fact["content_digest"] for fact in facts],
        }
    )
    reading_ids = {
        requirement_id: "gqrd_"
        + canonical_digest(
            {
                "snapshot_id": snapshot_id,
                "requirement_id": requirement_id,
                "method": "selected_receipt_count",
            }
        )
        for requirement_id in coverage
    }
    receipts = []
    for index, fact in enumerate(facts):
        linked = [reading_ids[key] for key, positions in coverage.items() if index in positions]
        receipts.append(
            {
                "receipt_id": "gqctx_" + fact["content_digest"],
                "citation_label": f"R{index + 1}",
                "kind": "content",
                "snapshot_id": snapshot_id,
                **{
                    key: fact[key]
                    for key in (
                        "market",
                        "source_label",
                        "source_family",
                        "platform",
                        "author",
                        "url",
                    )
                },
                "source_row_id": fact["row_id"],
                "published_at": fact["published_at"],
                "collected_at": fact["collected_at"],
                "excerpt": fact["excerpt"],
                "reading_ids": linked,
                "limitations": [
                    "Contextual records do not carry promoted signal authority.",
                    *(
                        [
                            "Publication time is unavailable; collection time bounds selection, not publication claims."
                        ]
                        if fact["published_at"] is None
                        else []
                    ),
                    *(
                        ["The source excerpt was truncated; the content digest binds the full row."]
                        if fact["excerpt_truncated"]
                        else []
                    ),
                ],
                "content_digest": fact["content_digest"],
            }
        )
    readings = [
        {
            "reading_id": reading_ids[key],
            "value": len(positions),
            "unit": "records",
            "window": copy.deepcopy(plan["window"]),
            "method": "selected_receipt_count",
            "denominator": None,
            "source_receipt_ids": [receipts[index]["receipt_id"] for index in positions],
            "limitations": ["This selected-record count is not population prevalence."],
        }
        for key, positions in coverage.items()
    ]
    provenance = {
        "source_binding": copy.deepcopy(source_binding),
        "query_bindings": bindings,
        "selected_row_digests": [fact["content_digest"] for fact in facts],
        "requirement_coverage": coverage,
    }
    fulfilled = sorted(
        requirement["requirement_id"]
        for requirement in plan["requirements"]
        if requirement["kind"] == "content" and requirement["requirement_id"] in coverage
    )
    if any(
        requirement["kind"] == "aggregate" and requirement["requirement_id"] in coverage
        for requirement in plan["requirements"]
    ):
        limitations.append(
            "Matching source records provide context only. Selected-record counts do not satisfy the requested aggregate measurement."
        )
    identity = {}
    if version in _IDENTITY_ADMISSION_VERSIONS:
        identity, unit_line = _identity(facts, profile_id, version)
        limitations.append(unit_line)
    return snapshot_id, receipts, readings, fulfilled, provenance, limitations, identity


def _selection_caps(selection, material):
    """Whether the selector or any query LIMIT bound this read, with both numbers.

    The same signals the admission's own truncation and cap lines are written from: the
    selector's omitted eligible count, and each capture's overflow over its matching rows.
    """
    kept = len(selection["facts"])
    return {
        "selection": {"kept": kept, "eligible": kept + selection["omitted_eligible_count"]},
        "lanes": {
            key: {
                "kept": len(material[key]["records"]),
                "matching": material[key]["full_matching_count"]
                - material[key]["candidate_count"]
                + len(material[key]["records"]),
                "capped": bool(material[key]["overflow"]),
            }
            for key in _CAPTURE_KEYS
        },
    }


def context_selection_caps(admission, *, request, plan, intake, queries, captures, query_records):
    """Re-derive the selection caps of an admission from the captures it was admitted from.

    The admission record keeps these caps only as prose limitations, so a window
    comparison reads them here, from the same material and version, without a query.
    """
    request, plan, _intake = _context(request, plan, intake)
    if type(admission) is not dict:
        _invalid()
    source_binding = admission["provenance"]["source_binding"]
    material, bindings = _query_material(queries, captures, query_records, source_binding)
    caps = {}
    _project(
        request,
        plan,
        source_binding,
        material,
        bindings,
        admission["contract_version"],
        admission["profile_id"],
        caps=caps,
    )
    return caps


def _admission(version, profile_id, source_binding, request, plan, material, bindings):
    snapshot_id, receipts, readings, fulfilled, provenance, selection_limits, identity = _project(
        request, plan, source_binding, material, bindings, version, profile_id
    )
    value = {
        "contract_version": version,
        "profile_id": profile_id,
        "snapshot_id": snapshot_id,
        "receipts": receipts,
        "readings": readings,
        "fulfilled_requirement_ids": fulfilled,
        "limitations": [
            "Contextual evidence is not promoted signal or release authority.",
            *selection_limits,
        ],
        "provenance": provenance,
        **identity,
    }
    value["admission_digest"] = canonical_digest(value)
    return value


def admit_protected_context(request, plan, intake, *, source, queries, captures, query_records):
    request, plan, _intake = _context(request, plan, intake)
    if not isinstance(source, LoadedProtectedContext):
        _invalid()
    material, bindings = _query_material(queries, captures, query_records, source.source_binding)
    return _admission(
        _CURRENT_ADMISSION_VERSION,
        source.profile_id,
        source.source_binding,
        request,
        plan,
        material,
        bindings,
    )


def validate_stored_context_admission(
    value, *, request, plan, intake, queries, captures, query_records
):
    request, plan, _intake = _context(request, plan, intake)
    if type(value) is not dict:
        _invalid()
    version = value.get("contract_version")
    if version not in _ADMISSION_VERSIONS or set(value) != _ADMISSION_VERSIONS[version]:
        _invalid()
    source_binding = value["provenance"]["source_binding"]
    material, bindings = _query_material(queries, captures, query_records, source_binding)
    expected = _admission(
        version, source_binding["profile_id"], source_binding, request, plan, material, bindings
    )
    if value != expected:
        _invalid()
    return copy.deepcopy(expected)


# Bridge v3 context. A bridge source is loaded once by ``load_bridge_history_source`` and
# stored as the record below. Readback holds no native clients, so it re-verifies what the
# record can prove on its own: the committed registry still pins this bridge row under the
# same authority, the stored result bytes hash to the pinned result digest, the read
# binding and the completion set are the ones that result names, and the cells are the
# ones that completion set admits for the request scope.
BRIDGE_ADMISSION_VERSION = "protected_bridge_context_admission_v1"
BRIDGE_SOURCE_FIELDS = frozenset(
    {
        "authority",
        "registry_entry",
        "binding",
        "result_json",
        "history_completion_set",
        "collection_evidence",
    }
)
_BRIDGE_ADMISSION_FIELDS = frozenset(
    {
        "contract_version",
        "profile_id",
        "snapshot_id",
        "receipts",
        "readings",
        "fulfilled_requirement_ids",
        "limitations",
        "provenance",
        "admission_digest",
    }
)
_BRIDGE_EXCERPT_LIMIT = 4000


class BridgeContextInvalid(ValueError):
    def __init__(self):
        super().__init__("bridge_context_invalid")


def _bridge_require(condition):
    if not condition:
        raise BridgeContextInvalid()


def _bridge_entry(profile_id):
    from src.analysis.open_intelligence.protected_context_registry import bridge_entries

    matches = [entry for entry in bridge_entries() if entry.profile_id == profile_id]
    _bridge_require(len(matches) == 1)
    return matches[0]


def bridge_source_record(loaded):
    """The stored form of one loaded bridge source, which readback re-verifies."""
    from src.analysis.open_intelligence.protected_context_registry import bridge_authority

    _bridge_require(
        type(loaded) is LoadedBridgeSource
        and type(loaded.result_json) is str
        and type(loaded.history_completion_set) is dict
    )
    return {
        "authority": bridge_authority(_bridge_entry(loaded.profile_id)),
        "registry_entry": copy.deepcopy(loaded.registry_entry),
        "binding": copy.deepcopy(loaded.binding),
        "result_json": loaded.result_json,
        "history_completion_set": copy.deepcopy(loaded.history_completion_set),
        "collection_evidence": copy.deepcopy(loaded.collection_evidence),
    }


def _bridge_row_runs(authority, evidence, profile):
    """The pipeline runs a collected row may carry, re-proved from the stored evidence.

    None on the daily chain route. On the approval ledger route the stored receipt set must
    be the one the pinned result names and each stored funded close the bytes its receipt
    digests, and each close names the run its execution's collected rows carry.
    """
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        funded_pipeline_run,
    )

    if authority == "daily_chain":
        _bridge_require(evidence is None)
        return None
    _bridge_require(
        type(evidence) is dict and set(evidence) == {"collection_receipt_set", "funded_results"}
    )
    receipts = evidence["collection_receipt_set"]
    results = evidence["funded_results"]
    _bridge_require(
        type(receipts) is dict
        and canonical_digest(receipts) == profile["collection_receipt_set_digest"]
        and type(receipts.get("receipts")) is list
        and type(results) is list
        and len(results) == len(receipts["receipts"])
    )
    return tuple(
        sorted(
            {
                funded_pipeline_run(result, item["collection_receipt_digest"])
                for item, result in zip(receipts["receipts"], results, strict=True)
            }
        )
    )


def _bridge_result_payload(authority, entry, raw):
    """The v3 capture payload inside result bytes that hash to the pinned result digest."""
    import hashlib

    from src.analysis.open_intelligence.daily_execution_contracts import canonical_json_object
    from src.analysis.open_intelligence.source_estate_bridge import CAPTURE_OPERATION

    _bridge_require(type(raw) is str)
    value, encoded = canonical_json_object(raw, "bridge_context_invalid")
    digest = hashlib.sha256(encoded).hexdigest()
    _bridge_require(digest == entry.result_digest)
    if authority == "daily_chain":
        payload = value.get("operation_payload")
        _bridge_require(
            entry.result_id == "exr_" + digest
            and value.get("contract_version") == "daily_execution_result_v1"
            and value.get("operation") == CAPTURE_OPERATION
            and type(payload) is dict
            and hashlib.sha256(canonical_bytes(payload)).hexdigest() == value.get("payload_digest")
            and payload.get("contract_version") == value.get("payload_contract_version")
        )
        return payload
    _bridge_require(authority == "execution_ledger")
    return value


def _bridge_cell_view(entries, scope):
    """Completed or empty cells and unavailable limitations, as the loader derives them."""
    cells, limitations = [], []
    for item in entries:
        if item["market"] not in scope:
            continue
        cell = {key: item[key] for key in ("lane", "market", "product_date")}
        if item["state"] == "unavailable":
            limitations.append({**cell, "reason_code": item["reason_code"]})
        else:
            cells.append(
                {
                    **cell,
                    "state": item["state"],
                    "row_count": item["row_count"],
                    "completion_entry_digest": canonical_digest(item),
                    "available_at": item["available_at"],
                }
            )
    return tuple(cells), tuple(limitations)


def restore_bridge_source(record, *, request, plan):
    """Re-verify a stored bridge source record and return the source it names.

    Refuses with ``bridge_context_invalid`` unless the committed registry pins this bridge
    row under the recorded authority and every stored value is the one that pin proves.
    """
    from src.analysis.open_intelligence.protected_context_registry import (
        bridge_authority,
        bridge_registry_row,
        bridge_snapshot_tables,
    )
    from src.analysis.open_intelligence.source_estate_bridge import (
        CAPTURE_RESULT_VERSION,
        PROFILE_FIELDS,
        READ_FIELDS,
    )

    try:
        _bridge_require(type(record) is dict and set(record) == BRIDGE_SOURCE_FIELDS)
        row = record["registry_entry"]
        _bridge_require(type(row) is dict)
        entry = _bridge_entry(row.get("profile_id"))
        _bridge_require(
            bridge_registry_row(entry) == row and bridge_authority(entry) == record["authority"]
        )
        cutoff = date.fromisoformat(entry.cutoff_date)
        _bridge_require(date.fromisoformat(plan["window"]["end"]) <= cutoff)
        payload = _bridge_result_payload(record["authority"], entry, record["result_json"])
        profile = {key: payload[key] for key in PROFILE_FIELDS - {"schema_digest"}}
        _bridge_require(
            payload["contract_version"] == CAPTURE_RESULT_VERSION
            and payload["missing_checks"] == []
            and payload["profile_id"] == entry.profile_id
            and payload["cutoff_date"] == entry.cutoff_date
            and [item["destination_table"] for item in profile["relation_bindings"]]
            == bridge_snapshot_tables(cutoff)
        )
        binding = record["binding"]
        as_of = _bridge_instant(request["as_of"])
        scope = sorted(set(plan["markets"]))
        _bridge_require(type(binding) is dict and set(binding) == READ_FIELDS)
        expected = {
            "contract_version": "source_bridge_read_binding_v1",
            "registry_entry_digest": canonical_digest(row),
            "profile_id": entry.profile_id,
            "result_id": entry.result_id,
            "result_digest": entry.result_digest,
            "snapshot_digest": payload["snapshot_digest"],
            "request_as_of": _bridge_text(as_of),
            "purpose": "current_context",
            "product_date": None,
            "client_scope_id": request["client_scope_id"],
            "market_scope": scope,
            "relation_bindings": profile["relation_bindings"],
            "collection_receipt_set_digest": profile["collection_receipt_set_digest"],
            "history_completion_set_digest": profile["history_completion_set_digest"],
            "temporal_rules_version": profile["temporal_rules_version"],
            "temporal_rules_digest": profile["temporal_rules_digest"],
            "current_operation_id": None,
            "current_output_set_digest": None,
        }
        _bridge_require(all(binding[key] == value for key, value in expected.items()))
        available = _bridge_instant(binding["available_at"])
        _bridge_require(
            _bridge_text(available) == binding["available_at"]
            and _bridge_instant(payload["captured_at"]) <= available <= as_of
            and payload["client_scope_id"] == request["client_scope_id"]
            and set(scope) <= set(payload["market_scope"])
        )
        completion = record["history_completion_set"]
        # The completion set is the one the pinned result names, so its entries are the
        # ones the capture recorded and the loader checked against native evidence.
        _bridge_require(
            type(completion) is dict
            and canonical_digest(completion) == profile["history_completion_set_digest"]
            and completion.get("contract_version") == "42_bridge_history_completion_set_v1"
            and type(completion.get("entries")) is list
            and all(type(item) is dict for item in completion["entries"])
        )
        cells, limitations = _bridge_cell_view(completion["entries"], set(scope))
        row_runs = _bridge_row_runs(record["authority"], record["collection_evidence"], profile)
    except BridgeContextInvalid:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise BridgeContextInvalid() from error
    return LoadedBridgeSource(
        profile_id=entry.profile_id,
        registry_entry=copy.deepcopy(row),
        binding=copy.deepcopy(binding),
        profile=copy.deepcopy(profile),
        cells=cells,
        limitations=limitations,
        collection_runs=(),
        result_json=record["result_json"],
        history_completion_set=copy.deepcopy(completion),
        collection_row_runs=row_runs,
        collection_evidence=copy.deepcopy(record["collection_evidence"]),
    )


def _bridge_query_material(source, plan, queries, captures, query_records):
    """Decode each bridge read bound to its owned query record, and admit its rows."""
    from src.analysis.open_intelligence.bridge_history_queries import (
        admit_collection_rows,
        admit_history_rows,
    )
    from src.analysis.open_intelligence.general_question_context_queries import (
        BRIDGE_HISTORY_TEMPLATE,
        BRIDGE_TEMPLATES,
        decode_bridge_context_rows,
    )

    lanes = tuple(queries) if type(queries) is dict else ()
    _bridge_require(
        bool(lanes)
        and type(captures) is dict
        and type(query_records) is dict
        and set(captures) == set(lanes) == set(query_records)
    )
    binding_digest = canonical_digest(source.binding)
    material, bindings, overflow = {}, {}, []
    for lane in lanes:
        query, capture, record = queries[lane], captures[lane], query_records[lane]
        _bridge_require(
            query.template_id in BRIDGE_TEMPLATES
            and query.source_binding_digest == binding_digest
            and query.source_snapshot_digest == source.binding["snapshot_digest"]
            and query.profile_id == source.profile_id
            and type(capture) is dict
            and set(capture) == {"rows", "receipt"}
            and type(record) is dict
            and set(record) == {"reservation", "receipt"}
            and capture["receipt"] == record["receipt"]
        )
        reservation, receipt = record["reservation"], record["receipt"]
        expected = {
            "template_id": query.template_id,
            "sql_digest": query.sql_digest,
            "parameters_digest": query.parameters_digest,
        }
        _bridge_require(
            all(
                reservation.get(field) == value and receipt.get(field) == value
                for field, value in expected.items()
            )
            and reservation.get("candidate_limit") == query.candidate_limit
            and receipt.get("query_state") == "succeeded"
            and receipt.get("result_digest") == canonical_digest(capture["rows"])
        )
        decoded = decode_bridge_context_rows(capture["rows"], query=query)
        if decoded["overflow"]:
            overflow.append((lane, decoded["candidate_count"]))
        if query.template_id == BRIDGE_HISTORY_TEMPLATE:
            admit_rows = admit_history_rows
        else:
            admit_rows = admit_collection_rows
        material[lane] = admit_rows(
            source,
            lane,
            decoded["records"],
            window_start=plan["window"]["start"],
            window_end=plan["window"]["end"],
        )
        bindings[lane] = {
            **expected,
            "job_id": receipt.get("job_id"),
            "result_digest": receipt["result_digest"],
            "bridge_binding_digest": binding_digest,
        }
    return material, bindings, overflow


def _bridge_url(value):
    from urllib.parse import urlparse

    if type(value) is not str or not value:
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.netloc
        or parsed.username
        or any(ord(character) < 32 for character in value)
    ):
        return None
    return value


def _bridge_optional_text(value):
    return value if type(value) is str and value.strip() else None


def _bridge_facts(request, plan, source, material):
    """Context facts over every admitted bridge row, each carrying its row provenance."""
    from src.analysis.open_intelligence.bridge_history_queries import partition_column
    from src.analysis.open_intelligence.source_estate_bridge import COLLECTION_TABLES

    start = datetime.combine(date.fromisoformat(plan["window"]["start"]), datetime.min.time(), UTC)
    end = datetime.combine(
        date.fromisoformat(plan["window"]["end"]) + timedelta(days=1), datetime.min.time(), UTC
    )
    as_of = _bridge_instant(request["as_of"])
    facts, excluded = [], {"untimely": 0, "textless": 0, "unbound": 0}
    bound = None if source.collection_row_runs is None else set(source.collection_row_runs)
    for lane, items in material.items():
        for item in items:
            row, provenance = item["row"], item["provenance"]
            _bridge_require(type(row) is dict and row.get("market") in plan["markets"])
            digest = canonical_digest(row)
            if lane in COLLECTION_TABLES:
                _bridge_require(type(row.get("id")) is str and bool(row["id"].strip()))
                # A clone holds the whole live table: a row another run wrote, a replay
                # copy among them, is not this capture's collection.
                if bound is not None and row.get("pipeline_run_id") not in bound:
                    excluded["unbound"] += 1
                    continue
                collected = _bridge_instant(row["collected_at"])
                published = (
                    None
                    if row.get("published_at") is None
                    else _bridge_instant(row["published_at"])
                )
                if collected > as_of or (
                    published is not None and (published > as_of or not start <= published < end)
                ):
                    excluded["untimely"] += 1
                    continue
                full = _bridge_optional_text(row.get("text")) or _bridge_optional_text(
                    row.get("title")
                )
                if full is None:
                    excluded["textless"] += 1
                    continue
                fact = {
                    "lane": lane,
                    "market": row["market"],
                    "row_id": row["id"],
                    "source_label": _bridge_optional_text(row.get("source")) or "unknown",
                    "source_family": _bridge_optional_text(row.get("source_family")),
                    "platform": _bridge_optional_text(row.get("platform")) or "unknown",
                    "author": _bridge_optional_text(row.get("author_handle")),
                    "url": _bridge_url(row.get("url")),
                    "published_at": None if published is None else published.isoformat(),
                    "collected_at": collected.isoformat(),
                    "selection_text": _selection_text(row),
                    "note": (
                        f"Collected record admitted under collection receipt set "
                        f"{provenance['collection_receipt_set_digest']}, available "
                        f"{provenance['available_at']}."
                    ),
                }
            else:
                day = row[partition_column(lane)]
                available = _bridge_instant(provenance["available_at"])
                _bridge_require(available <= as_of)
                full = canonical_bytes(row).decode("utf-8")
                fact = {
                    "lane": lane,
                    "market": row["market"],
                    "row_id": f"{lane}:{day}:{digest[:16]}",
                    "source_label": lane,
                    "source_family": "derived_history",
                    "platform": "derived_history",
                    "author": None,
                    "url": None,
                    "published_at": None,
                    "collected_at": available.isoformat(),
                    "selection_text": full,
                    "note": (
                        f"Daily {lane} product row for {day} under completion entry "
                        f"{provenance['completion_entry_digest']}, available "
                        f"{provenance['available_at']}; a product date is not a publication time."
                    ),
                }
            excerpt = full[:_BRIDGE_EXCERPT_LIMIT]
            fact.update(
                excerpt=excerpt,
                excerpt_truncated=len(excerpt) != len(full),
                content_digest=digest,
                provenance={"lane": lane, **copy.deepcopy(provenance)},
            )
            facts.append(fact)
    return facts, excluded


def _bridge_history_limitations(source, plan):
    """Every history cell in the window and scope without an admitted outcome."""
    from src.analysis.open_intelligence.bridge_history_queries import bridge_limitations

    return bridge_limitations(
        source, window_start=plan["window"]["start"], window_end=plan["window"]["end"]
    )


def _bridge_limitation_lines(cells):
    """One line per lane, market and reason, naming each day without an outcome."""
    grouped = {}
    for item in cells:
        key = (item["lane"], item["market"], item["reason_code"])
        grouped.setdefault(key, []).append(item["product_date"])
    return [
        f"History lane {lane} in {market} holds no admitted outcome on "
        f"{', '.join(days)} ({reason}); these days are unavailable, not zero."
        for (lane, market, reason), days in sorted(grouped.items())
    ]


def _bridge_admission(request, plan, source, material, bindings, overflow, limit):
    facts, excluded = _bridge_facts(request, plan, source, material)
    unavailable = _bridge_history_limitations(source, plan)
    limitations = [
        "Contextual evidence is not promoted signal or release authority.",
        f"Read from bridge capture {source.profile_id} with cutoff "
        f"{source.registry_entry['cutoff_date']}, result {source.binding['result_id']}.",
        *(
            f"Bridge {lane} read was capped at the newest {count} matching rows; older "
            "matching rows were not read."
            for lane, count in overflow
        ),
        *_bridge_limitation_lines(unavailable),
    ]
    if excluded["untimely"]:
        limitations.append(
            f"Excluded {excluded['untimely']} collected records whose publication or "
            "collection time falls outside the window or after the request time."
        )
    if excluded["textless"]:
        limitations.append(f"Excluded {excluded['textless']} collected records without text.")
    if excluded["unbound"]:
        limitations.append(
            f"Excluded {excluded['unbound']} collected records that no receipted collection "
            "run of this capture wrote."
        )
    try:
        selection = _allocate_context_facts(facts, plan, limit=limit)
        unlimited = (
            _allocate_context_facts(facts, plan, limit=len(facts))
            if selection["omitted_eligible_count"]
            else selection
        )
    except ValueError as error:
        raise BridgeContextInvalid() from error
    limitations.extend(_coverage_limitations(selection, unlimited))
    selected = {_fact_key(fact) for fact in selection["facts"]}
    facts = [fact for fact in facts if _fact_key(fact) in selected]
    facts.sort(
        key=lambda item: (
            item["market"],
            item["published_at"] or item["collected_at"],
            item["lane"],
            item["row_id"],
        )
    )
    positions = {_fact_key(fact): index for index, fact in enumerate(facts)}
    coverage = {}
    for requirement_id, indices in selection["coverage"].items():
        matched = sorted(
            positions[_fact_key(fact)]
            for index, fact in enumerate(selection["facts"])
            if index in indices
            and _shown(fact["retrieval_signals"], requirement_id, facts[positions[_fact_key(fact)]])
        )
        if matched:
            coverage[requirement_id] = matched
    if not coverage:
        raise NoMatchingContextEvidence("no_matching_evidence")
    binding_digest = canonical_digest(source.binding)
    snapshot_id = "gqs_" + canonical_digest(
        {
            "request_digest": request["request_digest"],
            "plan_digest": plan["plan_digest"],
            "bridge_binding_digest": binding_digest,
            "query_result_digests": [bindings[lane]["result_digest"] for lane in bindings],
            "fact_digests": [fact["content_digest"] for fact in facts],
        }
    )
    reading_ids = {
        requirement_id: "gqrd_"
        + canonical_digest(
            {
                "snapshot_id": snapshot_id,
                "requirement_id": requirement_id,
                "method": "selected_receipt_count",
            }
        )
        for requirement_id in coverage
    }
    receipts, row_provenance = [], []
    for index, fact in enumerate(facts):
        receipt_id = "gqctx_" + fact["content_digest"]
        receipts.append(
            {
                "receipt_id": receipt_id,
                "citation_label": f"R{index + 1}",
                "kind": "content",
                "snapshot_id": snapshot_id,
                "market": fact["market"],
                "source_label": fact["source_label"],
                "source_family": fact["source_family"],
                "platform": fact["platform"],
                "author": fact["author"],
                "url": fact["url"],
                "source_row_id": fact["row_id"],
                "published_at": fact["published_at"],
                "collected_at": fact["collected_at"],
                "excerpt": fact["excerpt"],
                "reading_ids": [
                    reading_ids[key] for key, matched in coverage.items() if index in matched
                ],
                "limitations": [
                    "Contextual records do not carry promoted signal authority.",
                    f"Admitted from bridge capture {source.profile_id} result "
                    f"{source.binding['result_id']} snapshot {source.binding['snapshot_digest']}.",
                    fact["note"],
                    *(
                        [
                            "Publication time is unavailable; collection time bounds selection, not publication claims."
                        ]
                        if fact["published_at"] is None
                        else []
                    ),
                    *(
                        ["The source excerpt was truncated; the content digest binds the full row."]
                        if fact["excerpt_truncated"]
                        else []
                    ),
                ],
                "content_digest": fact["content_digest"],
            }
        )
        row_provenance.append({"receipt_id": receipt_id, **fact["provenance"]})
    readings = [
        {
            "reading_id": reading_ids[key],
            "value": len(matched),
            "unit": "records",
            "window": copy.deepcopy(plan["window"]),
            "method": "selected_receipt_count",
            "denominator": None,
            "source_receipt_ids": [receipts[index]["receipt_id"] for index in matched],
            "limitations": ["This selected-record count is not population prevalence."],
        }
        for key, matched in coverage.items()
    ]
    fulfilled = sorted(
        requirement["requirement_id"]
        for requirement in plan["requirements"]
        if requirement["kind"] == "content" and requirement["requirement_id"] in coverage
    )
    if any(
        requirement["kind"] == "aggregate" and requirement["requirement_id"] in coverage
        for requirement in plan["requirements"]
    ):
        limitations.append(
            "Matching source records provide context only. Selected-record counts do not satisfy the requested aggregate measurement."
        )
    value = {
        "contract_version": BRIDGE_ADMISSION_VERSION,
        "profile_id": source.profile_id,
        "snapshot_id": snapshot_id,
        "receipts": receipts,
        "readings": readings,
        "fulfilled_requirement_ids": fulfilled,
        "limitations": limitations,
        "provenance": {
            "bridge_binding_digest": binding_digest,
            "query_bindings": bindings,
            "selected_row_digests": [fact["content_digest"] for fact in facts],
            "requirement_coverage": coverage,
            "row_provenance": row_provenance,
            "bridge_limitations": unavailable,
        },
    }
    value["admission_digest"] = canonical_digest(value)
    return value


def admit_bridge_context(
    request, plan, intake, *, source, queries, captures, query_records, evidence_limit
):
    """Admit the rows of the bridge reads as context facts, each with its provenance.

    Refuses with ``bridge_context_invalid`` when a read is not bound to its owned query
    record or a row falls outside what the loaded capture admitted; raises
    ``NoMatchingContextEvidence`` when no admitted row matches a planned requirement.
    """
    request, plan, _intake = _context(request, plan, intake)
    _bridge_require(
        type(source) is LoadedBridgeSource
        and type(evidence_limit) is int
        and 1 <= evidence_limit <= _FACT_LIMIT
    )
    try:
        material, bindings, overflow = _bridge_query_material(
            source, plan, queries, captures, query_records
        )
    except BridgeContextInvalid:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise BridgeContextInvalid() from error
    try:
        return _bridge_admission(
            request, plan, source, material, bindings, overflow, evidence_limit
        )
    except (BridgeContextInvalid, NoMatchingContextEvidence):
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise BridgeContextInvalid() from error


def validate_stored_bridge_admission(
    value, *, request, plan, intake, source, queries, captures, query_records, evidence_limit
):
    """Rebuild a stored bridge admission from its bound reads; refuse any difference."""
    if type(value) is not dict or set(value) != _BRIDGE_ADMISSION_FIELDS:
        raise BridgeContextInvalid()
    expected = admit_bridge_context(
        request,
        plan,
        intake,
        source=source,
        queries=queries,
        captures=captures,
        query_records=query_records,
        evidence_limit=evidence_limit,
    )
    if value != expected:
        raise BridgeContextInvalid()
    return copy.deepcopy(expected)


__all__ = [
    "BRIDGE_ADMISSION_VERSION",
    "LoadedBridgeSource",
    "LoadedProtectedContext",
    "admit_bridge_context",
    "admit_protected_context",
    "bridge_source_record",
    "identity_policy_digest",
    "load_protected_context_source",
    "restore_bridge_source",
    "validate_stored_bridge_admission",
    "validate_stored_context_admission",
]
