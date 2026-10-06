"""Durable general-question routing without an unowned generation fallback."""

import asyncio
import copy
import json
import logging
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from src.api import (
    client_lenses,
    investigation_scopes,
    operator_timeline,
    question_worker_store,
    workspace_scope,
)
from src.api.general_question_oidc import (
    WorkerOIDCError,
    verify_question_worker_authorization,
)
from src.api.question_worker_deadline import deadline_from_request, stored_request_lens
from src.api.question_worker_process import (
    WorkerProcessError,
    emit_parent_diagnostic,
    parent_failure_code,
)
from src.api.question_worker_protocol import (
    WorkerProtocolError,
    _digest,
    _parent_references,
    _read_reply,
    encode_worker_input,
)
from src.api.question_worker_store import read_request_bytes

logger = logging.getLogger("listening_post.question_worker")
_IDEMPOTENCY_NAMESPACE = UUID("6d7e0c1a-3f4b-4c52-9a8e-2b1f0c9d7e42")
# The declared shape of a durable name that carries a lens. A name that carries
# none predates lenses and keeps its original unversioned shape.
_LENSED_IDENTITY_VERSION = "client_lens_identity_v1"
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")
_STAGE_EVENT_VERSION = "question_stage_event_v1"
_STAGE_EVENT_FIELDS = frozenset(
    {
        "event_version",
        "request_id",
        "invocation_id",
        "policy_digest",
        "deployment_digest",
        "stage",
        "event",
        "occurred_at",
        "elapsed_ms",
        "reason_code",
        "usage_state",
    }
)
# Frozen copy of the engine's STAGE_REASON_CODES; a unit test holds the two sets identical.
_STAGE_REASON_CODES = frozenset(
    {
        "request_invalid",
        "scope_invalid",
        "approval_required",
        "request_expired",
        "retrieval_incomplete",
        "evidence_insufficient",
        "claim_support_failed",
        "answer_invalid",
        "semantic_output_invalid",
        "model_timeout",
        "metering_persistence_failed",
        "response_usage_unavailable",
        "storage_unavailable",
        "worker_failed",
        "budget_exhausted_no_grounded_result",
        "request_cancelled",
        "request_terminal",
        "request_conflict",
        "queue_dispatch_unresolved",
    }
)
_USAGE_STATES = ("none", "reserved", "pending", "settled", "unknown")
_SCOPE_BINDING_VERSION = "question_scope_binding_v1"
_LENS_ROSTER_RECORD_VERSION = "question_lens_roster_read_v1"
_POLICY_REVIEW_LAPSED = (
    "The question was not admitted. Nothing was written. The service's "
    "pricing review has lapsed and needs renewing before questions can be answered."
)
# The engine refuses a stripped question longer than this at normalization, after
# a child process has started and read the store. The same bound is applied here
# so an oversized question or follow up is refused before any of that work.
_QUESTION_CHARACTER_LIMIT = 4000
_QUESTION_TOO_LONG = {
    "code": "question_too_long",
    "message": f"The question is longer than {_QUESTION_CHARACTER_LIMIT} characters.",
}
_CHAT_INPUT_TOO_LARGE = {
    "code": "chat_input_too_large",
    "message": "The question and its conversation are too large to send.",
}
_PARENT_REFUSALS = {
    "parent_reference_invalid": (400, "parent_reference_invalid"),
    "parent_unavailable": (409, "parent_context_unavailable"),
    "parent_scope_mismatch": (409, "parent_context_unavailable"),
    "parent_source_unavailable": (409, "parent_context_unavailable"),
    "parent_context_invalid": (409, "parent_context_invalid"),
    "parent_context_conflict": (409, "parent_context_conflict"),
    "parent_alias_invalid": (409, "parent_alias_invalid"),
    "parent_read_budget_exhausted": (503, "parent_read_budget_exhausted"),
    "parent_context_deadline": (503, "parent_context_deadline"),
}


def _uuid_text(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def validate_stage_event(value):
    """Refuse any record beyond the closed lifecycle shape; no field admits free text."""
    if (
        type(value) is not dict
        or set(value) != _STAGE_EVENT_FIELDS
        or value["event_version"] != _STAGE_EVENT_VERSION
        or not _uuid_text(value["request_id"])
        or not _uuid_text(value["invocation_id"])
        or not _digest(value["policy_digest"])
        or not _digest(value["deployment_digest"])
        or value["stage"] not in ("admission", "queue")
        or value["event"] not in ("entered", "completed", "failed")
        or type(value["occurred_at"]) is not str
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value["occurred_at"])
        is None
        or type(value["elapsed_ms"]) is not int
        or not 0 <= value["elapsed_ms"] <= 600000
        or value["usage_state"] not in _USAGE_STATES
        or (value["event"] == "failed") != (value["reason_code"] is not None)
        or (value["reason_code"] is not None and value["reason_code"] not in _STAGE_REASON_CODES)
    ):
        raise ValueError("stage_event_invalid")
    return value


class _StageTimeline:
    """Admission and queue stage events for one browser submission attempt."""

    def __init__(self, service):
        self.service = service
        self.invocation_id = str(uuid4())
        self.started = time.monotonic()
        self.request_id = None

    def emit(self, stage, event, *, usage_state, reason_code=None):
        if self.request_id is None:
            return
        elapsed = time.monotonic() - self.started
        record = validate_stage_event(
            {
                "event_version": _STAGE_EVENT_VERSION,
                "request_id": self.request_id,
                "invocation_id": self.invocation_id,
                "policy_digest": self.service.policy_digest,
                "deployment_digest": self.service.deployment_digest,
                "stage": stage,
                "event": event,
                "occurred_at": datetime.now(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
                "elapsed_ms": min(600000, max(0, int(elapsed * 1000))),
                "reason_code": reason_code,
                "usage_state": usage_state,
            }
        )
        try:
            logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
        except Exception:
            pass


def _lifecycle(request_id, engine_state, deadline_at, response=None):
    state = "pending"
    reason = None
    if response is not None:
        reason = response.get("reason") if type(response) is dict else None
        state = engine_state
        if engine_state == "unavailable" and reason == "evidence_insufficient":
            state = "insufficient_evidence"
    return {
        "request_id": request_id,
        "state": state,
        "engine_state": engine_state,
        "reason_code": reason if type(reason) is str else None,
        "deadline_at": deadline_at,
    }


def configured_service(request):
    service = getattr(request.app.state, "general_question_routes", None)
    if not isinstance(service, GeneralQuestionRoutes):
        emit_parent_diagnostic("configuration", "failed", "worker_unconfigured")
        raise HTTPException(503, "General question worker is not configured")
    return service


def _question_binding(scope, request_id, *, client_lens=None):
    """The seven scope values for one durable request: five configured, two resolved.

    An authorized client lens travels beside them in its own versioned binding,
    so the seven values and their digest are unchanged wherever none is named.
    """
    try:
        return workspace_scope.ScopeBinding.for_question(
            scope,
            request_id=request_id,
            client_lens=getattr(client_lens, "envelope", client_lens),
        )
    except workspace_scope.WorkspaceScopeError:
        raise HTTPException(403, "Worker request scope is invalid") from None


def _identity_name(scope, idempotency_key, lens):
    """The durable name one idempotency key binds under this scope and lens.

    A request that names no lens keeps the exact name it bound before lenses
    existed, so it carries no version component. A lensed name adds the lens
    identifier and declares its own version, so the next change to this shape is
    a declared one rather than a silent re keying.

    The configuration digest is deliberately not part of the name. At any instant
    the registry maps one lens identifier to one configuration, so the identifier
    alone already separates every pair of distinct configurations, while a digest
    in the name would re key every outstanding request whenever a configuration
    was republished and would stop a past request's name being recomputable from
    durable data once the registry moved.
    """
    name = scope["client_scope_id"] + "\n" + idempotency_key
    if lens is None:
        return name
    return name + "\n" + _LENSED_IDENTITY_VERSION + "\n" + lens.client_lens_id


def _inventory_binding(scope):
    """The scope one inventory read listed; it names no run and no lens."""
    try:
        return workspace_scope.ScopeBinding.from_values(
            {
                **scope,
                "run_id": "question_inventory",
                "contract_version": workspace_scope.QUESTION_CONTRACT_VERSION,
            }
        )
    except workspace_scope.WorkspaceScopeError:
        raise HTTPException(403, "Worker request scope is invalid") from None


def _client_lens(scope, client_lens_id):
    """Resolve the console's lens selection under the server scope, or refuse it.

    General 42 is the default: an absent selection is not a lens and resolves to
    none. A named lens must already be authorized for this client scope, so a
    brand name, another client's lens and an unregistered identifier all refuse
    here, before any request identity or admission envelope is built.
    """
    if client_lens_id is None:
        return None
    try:
        return client_lenses.resolve_client_lens(
            client_scope_id=scope["client_scope_id"],
            brand_config_id=scope["brand_config_id"],
            client_lens_id=client_lens_id,
        )
    except client_lenses.ClientLensUnavailable:
        raise HTTPException(400, "Chat client lens is invalid") from None


def client_lens_roster():
    """What the Console may offer under the server scope; general 42 is the default."""
    scope = _current_scope()
    roster = {
        "contract_version": "client_lens_roster_v1",
        "default_client_lens_id": client_lenses.DEFAULT_CLIENT_LENS_ID,
        "client_scope_id": scope["client_scope_id"],
        "lenses": client_lenses.authorized_client_lenses(
            client_scope_id=scope["client_scope_id"]
        ),
    }
    record_lens_roster_read(roster)
    return roster


_COVERAGE_VERSION = "general_question_coverage_v1"
_COVERAGE_REGISTRY = ("configs", "open_intelligence", "protected_context_registry_v1.json")
_COVERAGE_REGISTRY_VERSION = "protected_context_registry_v1"
_COVERAGE_ENTRY_FIELDS = frozenset(
    {
        "consumption_id",
        "cutoff_date",
        "manifest_sha256",
        "market_scope",
        "profile_id",
        "result_digest",
        "result_id",
        "snapshot_tables",
        "source_as_of",
    }
)
_COVERAGE_PINS = {
    "consumption_id": re.compile(r"exc_[0-9a-f]{64}\Z"),
    "manifest_sha256": re.compile(r"[0-9a-f]{64}\Z"),
    "result_digest": re.compile(r"[0-9a-f]{64}\Z"),
    "result_id": re.compile(r"exr_[0-9a-f]{64}\Z"),
}
# The engine's production snapshot lanes, in its order. The parity test runs the
# engine's own registry loader over the same registries, so a drift here fails.
_COVERAGE_LANES = ("event_ledger", "seed_graph", "seed_candidates", "enriched_content", "raw_content")
# A fresh undated question resolves to the fourteen days ending at the source ceiling.
_COVERAGE_DAYS = 14
# A v2 entry carries its result contract version; a daily chain bridge entry also its
# derivation id. The worker's source ceiling reads v1 profiles and pinned bridge captures.
_COVERAGE_RESULT_V2 = "open_intelligence_execution_result_v2"
_COVERAGE_RESULT_V3 = "open_intelligence_execution_result_v3"
_COVERAGE_DERIVATION = re.compile(r"exd_[0-9a-f]{64}\Z")
# The worker reads bridge captures only while this variable is exactly "enabled"
# (general_question_context_admission.BRIDGE_READS_SWITCH); the page follows it.
_COVERAGE_BRIDGE_SWITCH = "GENERAL_QUESTION_BRIDGE_READS"


def _coverage_today():
    return datetime.now(UTC).date()


# The engine loader's own refusal reasons. A refusal is logged by one of these, or
# by the class of the error that caused it, and never by anything it read.
_COVERAGE_REASONS = frozenset(
    {
        "authority pins must be complete or all null",
        "bridge chain pins",
        "cutoff date",
        "duplicate key",
        "duplicate or unordered cutoffs",
        "entries",
        "entry fields",
        "market scope",
        "noncanonical registry",
        "noninteger number",
        "profile id",
        "registry fields",
        "registry too large",
        "registry version or id",
        "result contract version",
        "snapshot tables",
        "source as of",
    }
)
# The entry families the engine loader tells apart by key set, each with the result
# contract version it must carry. A v2 key set with a bridge profile id is a bridge
# capture recorded on the approval ledger; a v3 key set is one recorded as a daily chain.
_COVERAGE_FAMILIES = {
    _COVERAGE_ENTRY_FIELDS: ("v1", None),
    _COVERAGE_ENTRY_FIELDS | {"result_contract_version"}: ("v2", _COVERAGE_RESULT_V2),
    _COVERAGE_ENTRY_FIELDS | {"result_contract_version", "derivation_id"}: ("v3", _COVERAGE_RESULT_V3),
}
_COVERAGE_BRIDGE_PROFILE = re.compile(r"staging_bridge_v3_\d{8}\Z")
# The seven bridge clone lanes and the five v2 source lanes, in the engine's order.
_COVERAGE_BRIDGE_LANES = (
    "enriched_content",
    "event_ledger",
    "raw_content",
    "seed_candidates",
    "seed_graph",
    "trend_analysis",
    "trend_scores",
)
_COVERAGE_V2_LANES = ("enriched_content", "event_ledger", "raw_content", "seed_candidates", "seed_graph")


def _coverage_invalid(reason):
    raise ValueError("coverage_registry_invalid") from ValueError(reason)


def coverage_refusal_code(error):
    cause = error.__cause__ if error.__cause__ is not None else error
    if type(cause) is ValueError and cause.args and cause.args[0] in _COVERAGE_REASONS:
        return cause.args[0]
    return type(cause).__name__


def _coverage_tables(family, day):
    if family in ("v3", "ledger"):
        dataset, name, lanes = "trends_v2_staging", "staging_bridge_v3", _COVERAGE_BRIDGE_LANES
    elif family == "v2":
        dataset, name, lanes = "intelligence_42_sources_staging", "staging_source", _COVERAGE_V2_LANES
    else:
        dataset, name, lanes = "trends_v2_staging", "open_intelligence_v3_source", _COVERAGE_LANES
    return [f"ogilvy-trends-v2.{dataset}.{name}_{day:%Y%m%d}_{lane}" for lane in lanes]


def _coverage_entry(value):
    """The cutoff an entry serves as the source ceiling, or None; refused as the engine does.

    The checks follow the engine loader's entry rules family by family, with its reasons.
    A v1 profile serves once pinned. A bridge capture, on the ledger or as a daily chain,
    serves only while the worker's bridge reads are exactly enabled, as the engine's source
    ceiling admits it. A v2 profile is valid but never a ceiling.
    """
    if type(value) is not dict or frozenset(value) not in _COVERAGE_FAMILIES:
        _coverage_invalid("entry fields")
    family, version = _COVERAGE_FAMILIES[frozenset(value)]
    if family == "v2" and type(value["profile_id"]) is str and _COVERAGE_BRIDGE_PROFILE.match(value["profile_id"]):
        family = "ledger"
    if family != "v1" and value["result_contract_version"] != version:
        _coverage_invalid("result contract version")
    cutoff = value["cutoff_date"]
    if type(cutoff) is not str or date.fromisoformat(cutoff).isoformat() != cutoff:
        _coverage_invalid("cutoff date")
    day = date.fromisoformat(cutoff)
    bridge = family in ("v3", "ledger")
    expected_profile = f"staging_bridge_v3_{day:%Y%m%d}" if bridge else f"protected_context_{day:%Y%m%d}_{family}"
    if value["profile_id"] != expected_profile:
        _coverage_invalid("profile id")
    if value["source_as_of"] != datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC).isoformat():
        _coverage_invalid("source as of")
    markets = value["market_scope"]
    if (
        type(markets) is not list
        or not markets
        or any(type(market) is not str for market in markets)
        or markets != sorted(set(markets))
        or not set(markets) <= {"ke", "ng", "za"}
    ):
        _coverage_invalid("market scope")
    if value["snapshot_tables"] != _coverage_tables(family, day):
        _coverage_invalid("snapshot tables")
    pins = [value[key] for key in _COVERAGE_PINS]
    if family == "v1" and all(pin is None for pin in pins):
        return None
    if not all(type(value[key]) is str and pattern.match(value[key]) for key, pattern in _COVERAGE_PINS.items()):
        _coverage_invalid("authority pins must be complete or all null")
    if family == "v3" and (
        type(value["derivation_id"]) is not str
        or not _COVERAGE_DERIVATION.match(value["derivation_id"])
        or value["result_id"] != "exr_" + value["result_digest"]
    ):
        _coverage_invalid("bridge chain pins")
    if family == "v2" or (bridge and os.environ.get(_COVERAGE_BRIDGE_SWITCH) != "enabled"):
        return None
    return cutoff


def question_coverage(bundle_root, *, today):
    """Refuse, as the engine's loader does, every registry that does not read exactly."""
    try:
        return _question_coverage(bundle_root, today)
    except (UnicodeError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        if str(error) == "coverage_registry_invalid":
            raise
        raise ValueError("coverage_registry_invalid") from error


def _question_coverage(bundle_root, today):
    """The dates a fresh question can be answered for, read from the worker's registry.

    The worker bundle carries the protected context registry the engine selects its
    source profile from, so this reads that file rather than a copy, and refuses it
    wherever the engine's own loader would. The app process cannot import the engine
    package (both are the top level package src), so the checks are restated here
    and a test runs the engine's loader over the same registries. The selection
    follows the engine's source window hint: only a profile with its authority pins
    recorded is served, only once its cutoff day has closed, and the latest such
    cutoff bounds the default observation window. A pinned bridge capture is served
    beside the v1 profiles only while the worker's bridge reads are on, as the engine's
    source ceiling admits it; a v2 profile is never a ceiling.
    """

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                _coverage_invalid("duplicate key")
            result[key] = item
        return result

    def number(_value):
        _coverage_invalid("noninteger number")

    raw = Path(bundle_root).joinpath(*_COVERAGE_REGISTRY).read_bytes()
    if len(raw) > 1024 * 1024:
        _coverage_invalid("registry too large")
    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=pairs, parse_float=number, parse_constant=number
    )
    if type(value) is not dict or set(value) != {"contract_version", "registry_id", "entries"}:
        _coverage_invalid("registry fields")
    if (
        value["contract_version"] != _COVERAGE_REGISTRY_VERSION
        or value["registry_id"] != _COVERAGE_REGISTRY_VERSION
    ):
        _coverage_invalid("registry version or id")
    if json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() != raw:
        _coverage_invalid("noncanonical registry")
    if type(value["entries"]) is not list or not value["entries"]:
        _coverage_invalid("entries")
    pinned = [_coverage_entry(item) for item in value["entries"]]
    cutoffs = [item["cutoff_date"] for item in value["entries"]]
    if cutoffs != sorted(set(cutoffs)):
        _coverage_invalid("duplicate or unordered cutoffs")
    served = [cutoff for cutoff in pinned if cutoff is not None and date.fromisoformat(cutoff) < today]
    if not served:
        return {"contract_version": _COVERAGE_VERSION, "state": "unavailable", "window": None, "cutoff_date": None}
    end = date.fromisoformat(max(served))
    return {
        "contract_version": _COVERAGE_VERSION,
        "state": "covered",
        "window": {
            "start": (end - timedelta(days=_COVERAGE_DAYS - 1)).isoformat(),
            "end": end.isoformat(),
        },
        "cutoff_date": end.isoformat(),
    }


def record_lens_roster_read(roster):
    """Log which configurations this scope was told about.

    The roster carries a client scope and the configuration digests that scope
    may name, but it carries no run and no time binding, so it is not one of the
    ten scope boundaries and gets its own record rather than a boundary row.
    """
    record = {
        "record_version": _LENS_ROSTER_RECORD_VERSION,
        "client_scope_id": roster["client_scope_id"],
        "default_client_lens_id": roster["default_client_lens_id"],
        "offered": [
            {
                "client_lens_id": entry["client_lens_id"],
                "configuration_digest": entry["configuration_digest"],
            }
            for entry in roster["lenses"]
        ],
    }
    logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return record


def record_scope_binding(boundary, binding, *, request_id, reference=None, lens_known=True):
    """Log the scope projection one app boundary carries; the engine binds time at admission.

    The lens key has three states rather than two. It is absent when this request
    named no lens, which is the shape the record had before lenses existed. It
    carries the lens when one was named. It is an explicit null when the boundary
    is written before whatever would say which of the two this was, so a record
    written ahead of a read is never mistaken for a completed general 42 read.
    """
    adapter = {row.boundary: row.adapter for row in workspace_scope.BOUNDARY_TABLE}[boundary]
    record = {
        "record_version": _SCOPE_BINDING_VERSION,
        "boundary": boundary,
        "adapter": adapter,
        "request_id": request_id,
        "scope": binding.values,
        "scope_digest": binding.scope_digest,
        "reference": reference,
    }
    if not lens_known:
        record["client_lens"] = None
    elif not binding.client_lens.is_general:
        record["client_lens"] = binding.lens_values
    logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return record


def _current_scope():
    config = investigation_scopes._load(investigation_scopes.INVESTIGATION_SCOPES_PATH)
    configured = config["scopes"][config["default_scope_id"]]
    fields = {
        key: configured[key]
        for key in (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
        )
    }
    resolved = investigation_scopes.resolve_investigation_scope(**fields)
    return {
        key: sorted(resolved.market_scope)
        if key == "market_scope"
        else list(getattr(resolved, key))
        if key == "audience_lens_ids"
        else getattr(resolved, key)
        for key in fields
    }


class GeneralQuestionRoutes:
    def __init__(self, worker, queue, *, policy_digest, deployment_digest):
        if not _digest(policy_digest) or not _digest(deployment_digest):
            raise ValueError("worker_configuration_invalid")
        self.worker, self.queue = worker, queue
        self.policy_digest, self.deployment_digest = policy_digest, deployment_digest

    async def start(self, body):
        audit = {"request_id": None, "operation": "admission"}
        try:
            return await self._start(body, audit)
        except (Exception, asyncio.CancelledError) as error:
            fallback = (
                "request_invalid"
                if isinstance(error, HTTPException) and error.status_code in (400, 413)
                else "worker_failed"
            )
            emit_parent_diagnostic(
                audit["operation"],
                "failed",
                parent_failure_code(error, fallback),
                request_id=audit["request_id"],
            )
            raise

    async def _start(self, body, audit):
        scope = _current_scope()
        required = {"message", "history", "market"}
        optional = {
            "parent_request_id",
            "thread_anchor_request_id",
            "idempotency_key",
            "client_lens_id",
        }
        if type(body) is not dict or not required <= set(body) <= required | optional:
            raise HTTPException(400, "Chat input is invalid")
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and (
            type(idempotency_key) is not str or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        ):
            raise HTTPException(400, "Chat idempotency key is invalid")
        parent, anchor = body.get("parent_request_id"), body.get("thread_anchor_request_id")
        if not _parent_references(parent, anchor):
            raise HTTPException(400, "Chat parent reference is invalid")
        lens = _client_lens(scope, body.get("client_lens_id"))
        selected = None if body["market"] == "all" else body["market"]
        if selected is not None and selected not in scope["market_scope"]:
            raise HTTPException(400, "Chat market is invalid")
        if (
            type(body["message"]) is not str
            or not body["message"].strip()
            or type(body["history"]) is not list
        ):
            raise HTTPException(400, "Chat input is invalid")
        if len(body["message"].strip()) > _QUESTION_CHARACTER_LIMIT:
            raise HTTPException(413, _QUESTION_TOO_LONG)
        if self.queue is None:
            raise HTTPException(503, "General question queue is not configured")
        # A browser identity binds one durable request; wording alone never joins one.
        # The lens is part of that identity: the same key under a different
        # configuration is a different durable request, so a resubmission can never
        # be handed the first caller's job and therefore the other configuration's
        # answer. A key that names no lens keeps the identity it had before lenses.
        request_id = (
            str(uuid4())
            if idempotency_key is None
            else str(uuid5(_IDEMPOTENCY_NAMESPACE, _identity_name(scope, idempotency_key, lens)))
        )
        binding = _question_binding(scope, request_id, client_lens=lens)
        record_scope_binding(
            "request",
            binding,
            request_id=request_id,
            reference=None if parent is None else {"kind": "parent_request", "id": parent},
        )
        payload = {
            "contract_version": "general_question_admission_v1",
            "request_id": request_id,
            "transport": {
                "message": body["message"],
                "history": copy.deepcopy(body["history"]),
            },
            "scope": scope,
            "selected_market": selected,
            "policy_digest": self.policy_digest,
            "deployment_digest": self.deployment_digest,
        }
        if parent is not None:
            payload.update(
                contract_version="general_question_admission_v2",
                parent_request_id=parent,
                thread_anchor_request_id=anchor,
            )
        if lens is not None:
            # The engine re-resolves both values against the configuration bytes
            # it holds, so this envelope asserts a lens rather than granting one.
            payload["client_lens"] = lens.envelope
        deadline = time.monotonic() + 20
        audit["request_id"] = payload["request_id"]
        timeline = _StageTimeline(self)
        timeline.request_id = payload["request_id"]
        emit_parent_diagnostic(
            "admission",
            "started",
            "operation_started",
            request_id=payload["request_id"],
        )
        timeline.emit("admission", "entered", usage_state="none")
        try:
            outcome = await self.worker.run("admit", payload, deadline=deadline)
        except (Exception, asyncio.CancelledError) as error:
            code = parent_failure_code(error)
            timeline.emit(
                "admission",
                "failed",
                usage_state="none",
                reason_code="request_conflict"
                if code == "run_id_conflict"
                else code
                if code in _STAGE_REASON_CODES
                else "worker_failed",
            )
            raise
        emit_parent_diagnostic(
            "admission",
            "succeeded",
            "operation_succeeded",
            request_id=payload["request_id"],
        )
        timeline.emit("admission", "completed", usage_state="reserved")
        audit["operation"] = "queue_dispatch"
        reply = outcome.engine_reply.reply
        timeline.emit("queue", "entered", usage_state="reserved")
        try:
            admitted_remaining = (
                datetime.fromisoformat(reply["deadline_at"]) - datetime.now(UTC)
            ).total_seconds()
            enqueue_deadline = min(deadline, time.monotonic() + admitted_remaining)
            queued = await asyncio.to_thread(
                self.queue.enqueue, reply["invocation"], deadline=enqueue_deadline
            )
            if queued["state"] != "verified":
                logger.warning(
                    "question_enqueue_unresolved request_id=%s", payload["request_id"]
                )
                timeline.emit(
                    "queue",
                    "failed",
                    usage_state="reserved",
                    reason_code="queue_dispatch_unresolved",
                )
            else:
                timeline.emit("queue", "completed", usage_state="reserved")
        except Exception as error:
            emit_parent_diagnostic(
                "queue_dispatch",
                "unresolved",
                parent_failure_code(error),
                request_id=payload["request_id"],
            )
            logger.warning(
                "question_enqueue_unresolved request_id=%s", payload["request_id"]
            )
            timeline.emit(
                "queue",
                "failed",
                usage_state="reserved",
                reason_code="queue_dispatch_unresolved",
            )
        return {"job_id": reply["job_id"]}

    async def status(self, job_id):
        audit = {"request_id": None}
        try:
            return await self._status(job_id, audit)
        except (Exception, asyncio.CancelledError) as error:
            fallback = (
                "request_invalid"
                if isinstance(error, HTTPException) and error.status_code == 400
                else "worker_failed"
            )
            emit_parent_diagnostic(
                "status",
                "failed",
                parent_failure_code(error, fallback),
                request_id=audit["request_id"],
            )
            raise

    async def _status(self, job_id, audit):
        if (
            type(job_id) is not str
            or re.fullmatch(r"chat_[0-9a-f]{32}", job_id) is None
        ):
            raise HTTPException(400, "Chat job identity is invalid")
        audit["request_id"] = str(UUID(hex=job_id[5:]))
        emit_parent_diagnostic(
            "status", "started", "operation_started", request_id=audit["request_id"]
        )
        payload = {
            "contract_version": "general_question_status_request_v1",
            "request_id": str(UUID(hex=job_id[5:])),
            "scope": _current_scope(),
        }
        # A job id does not name a lens, and a caller must not be able to say which
        # one this request ran under, so the history boundary takes it from the
        # engine's own reply. The boundary is written before the read as well, so
        # a process that dies mid read still leaves a record of the boundary it
        # crossed. That first record states that the lens is not yet known; it is
        # never a claim that the request ran as general 42, because a read that
        # never returned cannot support that claim.
        record_scope_binding(
            "history",
            _question_binding(payload["scope"], payload["request_id"]),
            request_id=payload["request_id"],
            lens_known=False,
        )
        outcome = await self.worker.run("status", payload, deadline=time.monotonic() + 20)
        record_scope_binding(
            "history",
            _question_binding(
                payload["scope"],
                payload["request_id"],
                client_lens=outcome.engine_reply.reply.get("client_lens"),
            ),
            request_id=payload["request_id"],
        )
        emit_parent_diagnostic(
            "status", "succeeded", "operation_succeeded", request_id=audit["request_id"]
        )
        reply = outcome.engine_reply.reply
        if outcome.result_record is not None:
            record = outcome.result_record
            response = copy.deepcopy(record["response"])
            response["lifecycle"] = _lifecycle(
                payload["request_id"], record["state"], reply.get("deadline_at"), response
            )
            return response
        return {
            "pending": True,
            "lifecycle": _lifecycle(payload["request_id"], reply["state"], reply.get("deadline_at")),
        }

    async def execute(self, request):
        audit = {"request_id": None, "operation": "authentication"}
        try:
            return await self._execute(request, audit)
        except (Exception, asyncio.CancelledError) as error:
            fallback = {
                "task_validation": "worker_task_invalid",
                "binding_validation": "worker_binding_invalid",
                "scope_validation": "worker_scope_invalid",
            }.get(audit["operation"], "worker_failed")
            emit_parent_diagnostic(
                audit["operation"],
                "failed",
                parent_failure_code(error, fallback),
                request_id=audit["request_id"],
            )
            raise

    async def _execute(self, request, audit):
        lookup_deadline = time.monotonic() + 20
        emit_parent_diagnostic("authentication", "started", "operation_started")
        await asyncio.to_thread(
            verify_question_worker_authorization,
            request.headers.getlist("authorization"),
            deadline=lookup_deadline,
        )
        audit["operation"] = "task_validation"
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 256 * 1024:
                raise HTTPException(413, "Worker task exceeds limit")
        invocation = _read_reply(bytes(data), max_bytes=256 * 1024)
        encode_worker_input("execute", invocation)
        audit["request_id"] = invocation["request_id"]
        audit["operation"] = "binding_validation"
        if (
            invocation["policy_digest"] != self.policy_digest
            or invocation["deployment_digest"] != self.deployment_digest
        ):
            raise HTTPException(403, "Worker binding is not active")
        audit["operation"] = "request_read"
        emit_parent_diagnostic(
            "request_read",
            "started",
            "operation_started",
            request_id=invocation["request_id"],
        )
        raw = await asyncio.to_thread(
            read_request_bytes,
            self.worker.storage_client,
            invocation=invocation,
            deadline=lookup_deadline,
        )
        audit["operation"] = "scope_validation"
        scope = _current_scope()
        normalized = _read_reply(raw)
        if (
            any(
                normalized.get(key) != scope[key]
                for key in ("client_scope_id", "brand_config_id", "theme_id")
            )
            or not set(normalized.get("market_scope", [])) <= set(scope["market_scope"])
            or not set(normalized.get("audience_lens_ids", []))
            <= set(scope["audience_lens_ids"])
        ):
            raise HTTPException(403, "Worker request scope is invalid")
        if (
            normalized.get("run_id") != "question_" + UUID(invocation["request_id"]).hex
            or normalized.get("contract_version") != workspace_scope.QUESTION_CONTRACT_VERSION
        ):
            raise HTTPException(403, "Worker request identity is invalid")
        # The scope fields, the run identity and the contract version all agree by
        # here, and none of them binds the lens these bytes carry. Only the
        # invocation digest does, so the boundary is recorded after
        # deadline_from_request has recomputed it, never before.
        def record_request_boundary():
            record_scope_binding(
                "request",
                _question_binding(
                    {key: normalized[key] for key in workspace_scope.SCOPE_FIELDS[:5]},
                    invocation["request_id"],
                    client_lens=stored_request_lens(normalized),
                ),
                request_id=invocation["request_id"],
            )

        try:
            audit["operation"] = "execution"
            deadline = deadline_from_request(
                raw, invocation, now=datetime.now(UTC), monotonic_now=time.monotonic()
            )
        except WorkerProcessError as error:
            if error.reason != "worker_deadline_reached":
                raise
            # An expired request is reached only after the same digest has passed,
            # so this branch has bound bytes to record too.
            record_request_boundary()
            audit["operation"] = "status"
            outcome = await self.worker.run(
                "status",
                {
                    "contract_version": "general_question_status_request_v1",
                    "request_id": invocation["request_id"],
                    "scope": scope,
                },
                deadline=lookup_deadline,
            )
            if outcome.result_record is None:
                raise HTTPException(503, "Expired worker state is unresolved")
            return
        record_request_boundary()
        await self.worker.run("execute", invocation, deadline=deadline)
        emit_parent_diagnostic(
            "execution",
            "succeeded",
            "operation_succeeded",
            request_id=invocation["request_id"],
        )


async def read_question_observation(request, request_id):
    service = configured_service(request)
    scope = _current_scope()
    payload = {
        "contract_version": "general_question_observe_request_v1",
        "scope": scope,
        "request_id": request_id,
    }
    deadline = time.monotonic() + 20
    outcome = await service.worker.run("observe", payload, deadline=deadline)
    observed = outcome.engine_reply.reply
    if request_id is None:
        # The inventory lists every question in the scope, so what it records is
        # that scope rather than a run: one history boundary record naming the
        # inventory and carrying no lens, because no lens was applied to the list.
        record_scope_binding(
            "history",
            _inventory_binding(scope),
            request_id=None,
            reference={"kind": "question_inventory", "id": None},
        )
        return {
            "scope": scope,
            "coverage": observed["coverage"],
            "rows": observed["rows"],
        }
    detail = await asyncio.to_thread(
        question_worker_store.read_observed_question_detail,
        service.worker.storage_client,
        observe_input=payload,
        observed_reply=observed,
        deadline=deadline,
    )
    # The lens comes from the request bytes the invocation digest already binds,
    # so this boundary records the configuration the request was admitted under
    # rather than whatever the caller of this read happens to name.
    record_scope_binding(
        "history",
        _question_binding(scope, request_id, client_lens=detail["client_lens"]),
        request_id=request_id,
    )
    question = json.loads(detail["request_bytes"])
    intake = json.loads(detail["intake_bytes"])
    result = detail["result_record"]
    # A reopened answer names the lens its own stored request bytes carry, so the
    # Console shows, continues and exports it under that lens rather than under
    # whatever the page happens to have selected. General 42 adds no key, so a
    # request that named no lens keeps the detail it had before lenses existed.
    lens = {} if detail["client_lens"] is None else {"client_lens": detail["client_lens"]}
    return {
        **lens,
        "contract_version": "general_question_detail_v1",
        "request_id": request_id,
        "observed_state": observed["observed_state"],
        "question": question["question"],
        "history": question["history"],
        "selected_market": intake["selected_market"],
        "requested_window": question["requested_window"],
        "response": result["response"] if result is not None else None,
        # A reply written without a planned window carries the engine's default
        # window, the days before as_of, rather than one the question resolved
        # to. That is every reply written before a plan, and a reply written on
        # expiry or cancel after a plan that named no window, which still binds
        # the plan's digest. So only a window the bound plan names, carried by
        # the reply, is one the question resolved to.
        "window_from_plan": result is not None
        and detail["plan_window"] is not None
        and result["response"]["intelligence"]["window"] == detail["plan_window"],
        "reserved_microusd": observed["reserved_microusd"],
        "missing_work": observed["missing_work"],
    }


async def execute_route(request: Request):
    try:
        await configured_service(request).execute(request)
    except WorkerOIDCError as error:
        raise HTTPException(error.status_code, error.code) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "General question worker is unavailable") from None
    return Response(status_code=204)


async def start_route(request, body):
    try:
        return await configured_service(request).start(body)
    except HTTPException:
        raise
    except WorkerProtocolError as error:
        if error.args == ("worker_input_too_large",):
            raise HTTPException(413, _CHAT_INPUT_TOO_LARGE) from None
        raise HTTPException(503, "General question admission is unavailable") from None
    except WorkerProcessError as error:
        code = parent_failure_code(error)
        if code == "run_id_conflict":
            raise HTTPException(409, "request_conflict") from None
        if code == "policy_review_lapsed":
            # The admit child stopped at the policy check, before the intake was
            # built or anything was written, so this is a refusal and not an
            # admission whose outcome is unknown.
            raise HTTPException(
                503, {"code": code, "message": _POLICY_REVIEW_LAPSED}
            ) from None
        if type(body) is dict and body.get("parent_request_id") is not None and code in _PARENT_REFUSALS:
            status, detail = _PARENT_REFUSALS[code]
            raise HTTPException(status, detail) from None
        raise HTTPException(503, "General question admission is unavailable") from None
    except Exception:
        raise HTTPException(503, "General question admission is unavailable") from None


async def status_route(request, job_id):
    try:
        return await configured_service(request).status(job_id)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "General question status is unavailable") from None


async def coverage_route(request):
    service = getattr(request.app.state, "general_question_routes", None)
    if not isinstance(service, GeneralQuestionRoutes):
        raise HTTPException(503, "General question worker is not configured")
    try:
        return question_coverage(service.worker.bundle_root, today=_coverage_today())
    except (OSError, ValueError, AttributeError) as error:
        logger.warning("question_coverage_unavailable reason=%s", coverage_refusal_code(error))
        raise HTTPException(503, "Question coverage is unavailable") from None


def served_response_digest(served):
    """The rendered response digest of a terminal status body, or None while pending.

    A terminal status body is the stored response with only lifecycle added, so
    the digest is the one operator_timeline derives from the stored response.
    It travels in a response header, never in the body, so the status body
    stays exactly the stored response and its lifecycle.
    """
    if type(served) is not dict or served.get("pending") is True or "lifecycle" not in served:
        return None
    return operator_timeline.rendered_response_digest(
        {key: value for key, value in served.items() if key != "lifecycle"}
    )


_DELIVERY_UNKNOWN_CODES = frozenset(
    {"request_unknown", "scope_invalid", "observation_request_unavailable"}
)


def _delivery_refusal(refused):
    return JSONResponse(
        {"detail": {"code": refused.code}},
        status_code=refused.status,
        headers={"Cache-Control": "no-store"},
    )


async def _scoped_status(request, request_id):
    """The same scope bound status read the browser polled, or a delivery refusal.

    A request unknown in this server's scope, or held under another, is 404.
    """
    try:
        service = configured_service(request)
        return await service.status("chat_" + UUID(request_id).hex)
    except WorkerProcessError as error:
        if error.reason in _DELIVERY_UNKNOWN_CODES:
            raise operator_timeline.DeliveryRefused(404, "request_unknown") from None
        raise operator_timeline.DeliveryRefused(503, "delivery_unavailable") from None
    except (Exception, asyncio.CancelledError):
        raise operator_timeline.DeliveryRefused(503, "delivery_unavailable") from None


async def _verified_delivery_report(request, client_key):
    operator_timeline.DELIVERY_RATE_LIMIT.check(client_key)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > operator_timeline.DELIVERY_BODY_LIMIT:
            raise operator_timeline.DeliveryRefused(413, "delivery_report_too_large")
    report = operator_timeline.parse_delivery_report(bytes(data))
    served = await _scoped_status(request, report["request_id"])
    digest = served_response_digest(served)
    if digest is None:
        raise operator_timeline.DeliveryRefused(409, "response_unavailable")
    if digest != report["response_digest"]:
        raise operator_timeline.DeliveryRefused(409, "response_digest_mismatch")
    return report


async def delivery_route(request, *, client_key):
    """Record one advisory browser delivery report after checking it names a stored response.

    The report is telemetry. Nothing here writes to the question store, the
    result record or any review record, so a report can never answer a
    question, move its lifecycle or approve anything.
    """
    try:
        report = await _verified_delivery_report(request, client_key)
    except operator_timeline.DeliveryRefused as refused:
        logger.info("question_delivery_report_refused code=%s", refused.code)
        return _delivery_refusal(refused)
    record = operator_timeline.DELIVERY_LEDGER.record(report)
    return JSONResponse(
        {
            "contract_version": operator_timeline.DELIVERY_STATUS_VERSION,
            "request_id": record["request_id"],
            "delivery": record["event_type"],
            "authority": "advisory",
        },
        status_code=202,
        headers={"Cache-Control": "no-store"},
    )


async def delivery_status_route(request, request_id):
    """What this process heard from the browser; no acknowledgement reads as unknown.

    The request is read through the scope bound status first, so a request
    unknown in this scope is refused before anything recorded about it is shown.
    """
    try:
        if not _uuid_text(request_id):
            raise operator_timeline.DeliveryRefused(400, "delivery_request_invalid")
        await _scoped_status(request, request_id)
        status = operator_timeline.DELIVERY_LEDGER.status(request_id)
    except operator_timeline.DeliveryRefused as refused:
        return _delivery_refusal(refused)
    return JSONResponse(status, headers={"Cache-Control": "no-store"})
