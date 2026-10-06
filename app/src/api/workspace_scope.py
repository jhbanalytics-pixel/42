"""Server-owned scope resolution for read-only investigation workspaces.

``bind_boundary``, ``scope_cache_key``, ``admit_cache_key`` and
``ResolvedWorkspaceScope.bind`` are the projection and cache key shapes this
module defines; they are exercised by tests and called from nowhere in the
shipped application. The question routes write their own boundary record from
``ScopeBinding`` rather than through ``bind_boundary``, so the projection here
is a contract waiting for a consumer rather than the row the log carries.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from . import deployment_contract, investigation_scopes, investigation_store, investigations
from .dossier_store import SCOPE_FIELDS, canonical_digest

_INVESTIGATION_ID = re.compile(r"^inv_[A-Za-z0-9_-]{1,128}$")
_BUCKET_NAME = "listening-post-staging-cache"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")

# SCOPE_FIELDS holds the seven scope values every boundary records: five configured,
# run_id and contract_version resolved by the server for the run in hand.
# The C02 time binding travels beside the scope, never inside it.
TIME_BINDING_FIELDS = ("window_start", "window_end", "as_of", "source_cutoff")
TIME_BINDING_VERSION = "c02_time_binding_v1"
# The client lens is the optional configuration a request may name beside the
# seven scope values. It travels beside the scope in its own versioned binding,
# so the seven values and their legacy digest are unchanged for general 42.
LENS_BINDING_VERSION = "client_lens_binding_v1"
LENS_BINDING_FIELDS = ("lens_binding_version", "client_lens_id", "configuration_digest")
QUESTION_CONTRACT_VERSION = "general_cultural_question_v1"
INVESTIGATION_CONTRACT_VERSION = "2.1.0"
CACHE_KEY_VERSION = "scope_cache_key_v1"


class WorkspaceScopeError(ValueError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.detail = {"code": code, "message": message}


@dataclass(frozen=True, slots=True)
class BoundaryBinding:
    """One row of the frozen boundary table: the adapter version at that boundary."""

    boundary: str
    adapter: str
    scope_fields: tuple[str, ...] = SCOPE_FIELDS
    time_binding_version: str = TIME_BINDING_VERSION
    lens_binding_version: str = LENS_BINDING_VERSION


BOUNDARY_TABLE: tuple[BoundaryBinding, ...] = (
    BoundaryBinding("request", QUESTION_CONTRACT_VERSION),
    BoundaryBinding("plan", "general_question_plan_v2"),
    BoundaryBinding("retrieval", "released_evidence_v1"),
    BoundaryBinding("snapshot", "general_question_snapshot_v1"),
    BoundaryBinding("answer", "general_question_result_record_v1"),
    BoundaryBinding("cache", CACHE_KEY_VERSION),
    BoundaryBinding("history", "general_question_status_v1"),
    BoundaryBinding("investigation", "investigation_record_v1"),
    BoundaryBinding("artifact", "investigation_dossier_record_v1"),
    BoundaryBinding("export", "pdf_renderer_request_v1"),
)
_BOUNDARIES = {row.boundary: row for row in BOUNDARY_TABLE}


def _scope_invalid() -> None:
    raise WorkspaceScopeError(404, "scope_invalid", "Workspace is unavailable for this scope.")


def _time_binding_invalid() -> None:
    raise WorkspaceScopeError(400, "time_binding_invalid", "Time binding is invalid.")


def _identifier(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        _scope_invalid()
    return value


def _identifiers(value: object, *, lowercase: bool) -> list[str]:
    if type(value) not in (list, tuple):
        _scope_invalid()
    items = [_identifier(item) for item in value]
    if len(items) != len(set(items)) or (lowercase and any(i != i.lower() for i in items)):
        _scope_invalid()
    return items


@dataclass(frozen=True, slots=True)
class ClientLensBinding:
    """The optional client lens and the digest of the configuration it names."""

    client_lens_id: str | None
    configuration_digest: str | None

    @classmethod
    def general(cls) -> ClientLensBinding:
        """General 42 names no lens; it is still recorded, never omitted."""
        return cls(client_lens_id=None, configuration_digest=None)

    @classmethod
    def from_values(cls, values: object) -> ClientLensBinding:
        if values is None:
            return cls.general()
        if isinstance(values, ClientLensBinding):
            return values
        if not isinstance(values, Mapping) or set(values) != set(LENS_BINDING_FIELDS):
            _scope_invalid()
        if values["lens_binding_version"] != LENS_BINDING_VERSION:
            _scope_invalid()
        lens_id, digest = values["client_lens_id"], values["configuration_digest"]
        if lens_id is None and digest is None:
            return cls.general()
        if type(lens_id) is not str or not lens_id or type(digest) is not str:
            _scope_invalid()
        if _DIGEST.fullmatch(digest) is None:
            _scope_invalid()
        return cls(client_lens_id=lens_id, configuration_digest=digest)

    @property
    def is_general(self) -> bool:
        return self.client_lens_id is None

    @property
    def values(self) -> dict[str, object]:
        return {
            "lens_binding_version": LENS_BINDING_VERSION,
            "client_lens_id": self.client_lens_id,
            "configuration_digest": self.configuration_digest,
        }


@dataclass(frozen=True, slots=True)
class ScopeBinding:
    """The seven scope values plus their digest through the existing canonical digest."""

    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str | None
    audience_lens_ids: tuple[str, ...]
    theme_id: str | None
    run_id: str
    contract_version: str
    scope_digest: str
    client_lens: ClientLensBinding = ClientLensBinding(None, None)

    @classmethod
    def from_values(
        cls, values: Mapping[str, object], *, client_lens: object = None
    ) -> ScopeBinding:
        lens = ClientLensBinding.from_values(client_lens)
        if not isinstance(values, Mapping) or set(values) != set(SCOPE_FIELDS):
            _scope_invalid()
        markets = _identifiers(values["market_scope"], lowercase=True)
        if not markets:
            _scope_invalid()
        payload = {
            "client_scope_id": _identifier(values["client_scope_id"]),
            "market_scope": markets,
            "brand_config_id": None
            if values["brand_config_id"] is None
            else _identifier(values["brand_config_id"]),
            "audience_lens_ids": _identifiers(values["audience_lens_ids"], lowercase=False),
            "theme_id": None if values["theme_id"] is None else _identifier(values["theme_id"]),
            "run_id": _identifier(values["run_id"]),
            "contract_version": _identifier(values["contract_version"]),
        }
        # A named lens joins the digested payload; general 42 adds nothing, so a
        # request that names no lens keeps the digest it had before lenses existed.
        digested = payload if lens.is_general else {**payload, "client_lens": lens.values}
        return cls(
            client_scope_id=payload["client_scope_id"],
            market_scope=tuple(markets),
            brand_config_id=payload["brand_config_id"],
            audience_lens_ids=tuple(payload["audience_lens_ids"]),
            theme_id=payload["theme_id"],
            run_id=payload["run_id"],
            contract_version=payload["contract_version"],
            scope_digest=canonical_digest(digested),
            client_lens=lens,
        )

    @classmethod
    def from_frame(cls, frame: investigations.InvestigationFrame) -> ScopeBinding:
        """The frame's seven values and its lens; general 42 keeps its legacy digest."""
        return cls.from_values(
            {field: getattr(frame, field) for field in SCOPE_FIELDS},
            client_lens=frame.client_lens,
        )

    @classmethod
    def for_question(
        cls, scope: Mapping[str, object], *, request_id: str, client_lens: object = None
    ) -> ScopeBinding:
        """Bind the five configured values to the run the engine derives for request_id."""
        try:
            run_id = "question_" + UUID(request_id).hex
        except (TypeError, ValueError, AttributeError):
            _scope_invalid()
        if not isinstance(scope, Mapping) or set(scope) != set(SCOPE_FIELDS[:5]):
            _scope_invalid()
        return cls.from_values(
            {**scope, "run_id": run_id, "contract_version": QUESTION_CONTRACT_VERSION},
            client_lens=client_lens,
        )

    @property
    def values(self) -> dict[str, object]:
        return {
            "client_scope_id": self.client_scope_id,
            "market_scope": list(self.market_scope),
            "brand_config_id": self.brand_config_id,
            "audience_lens_ids": list(self.audience_lens_ids),
            "theme_id": self.theme_id,
            "run_id": self.run_id,
            "contract_version": self.contract_version,
        }

    @property
    def lens_values(self) -> dict[str, object]:
        return self.client_lens.values


def _date_text(value: object) -> date:
    if type(value) is not str or _DATE.fullmatch(value) is None:
        _time_binding_invalid()
    try:
        return date.fromisoformat(value)
    except ValueError:
        _time_binding_invalid()


def adapt_time_binding(value: object, *, version: str) -> dict[str, str]:
    """Project one versioned time shape onto the C02 binding, refusing anything else."""
    if version == TIME_BINDING_VERSION:
        if not isinstance(value, Mapping) or set(value) != set(TIME_BINDING_FIELDS):
            _time_binding_invalid()
        candidate = {field: value[field] for field in TIME_BINDING_FIELDS}
    elif version == "engine_window_v1":
        if (
            not isinstance(value, Mapping)
            or set(value) != {"window", "as_of", "source_cutoff"}
            or not isinstance(value["window"], Mapping)
            or set(value["window"]) != {"start", "end", "closed"}
            or type(value["window"]["closed"]) is not bool
        ):
            _time_binding_invalid()
        candidate = {
            "window_start": value["window"]["start"],
            "window_end": value["window"]["end"],
            "as_of": value["as_of"],
            "source_cutoff": value["source_cutoff"],
        }
    else:
        _time_binding_invalid()
    start = _date_text(candidate["window_start"])
    end = _date_text(candidate["window_end"])
    cutoff = _date_text(candidate["source_cutoff"])
    as_of = candidate["as_of"]
    if type(as_of) is not str or _TIMESTAMP.fullmatch(as_of) is None:
        _time_binding_invalid()
    try:
        as_of_day = datetime.fromisoformat(as_of.replace("Z", "+00:00")).date()
    except ValueError:
        _time_binding_invalid()
    if start > end or cutoff > as_of_day:
        _time_binding_invalid()
    return candidate


def bind_boundary(boundary: str, *, scope: ScopeBinding, time_binding: object) -> dict[str, object]:
    """Return the normalized projection one boundary records; nothing may be omitted."""
    row = _BOUNDARIES.get(boundary) if type(boundary) is str else None
    if row is None:
        raise WorkspaceScopeError(400, "boundary_invalid", "Boundary is unknown.")
    if not isinstance(scope, ScopeBinding):
        _scope_invalid()
    projection = {
        "boundary": row.boundary,
        "adapter": row.adapter,
        "scope": {field: scope.values[field] for field in row.scope_fields},
        "scope_digest": scope.scope_digest,
        "time_binding_version": row.time_binding_version,
        "time_binding": adapt_time_binding(time_binding, version=row.time_binding_version),
        "lens_binding_version": row.lens_binding_version,
        "client_lens": scope.client_lens.values,
    }
    projection["binding_digest"] = canonical_digest(projection)
    return projection


def scope_cache_key(*, scope: ScopeBinding, time_binding: object, subject: str) -> str:
    """Cache entries never cross a scope or a time binding; both digests sit in the key."""
    if not isinstance(scope, ScopeBinding):
        _scope_invalid()
    bound = adapt_time_binding(time_binding, version=TIME_BINDING_VERSION)
    subject_digest = canonical_digest(_identifier(subject))
    return "/".join((CACHE_KEY_VERSION, scope.scope_digest, canonical_digest(bound), subject_digest))


def admit_cache_key(key: object, *, scope: ScopeBinding, read: Callable[[str], object]) -> object:
    """The key is its own binding: its embedded digest must be the server scope before any read."""
    if not isinstance(scope, ScopeBinding) or type(key) is not str:
        _scope_invalid()
    parts = key.split("/")
    if (
        len(parts) != 4
        or parts[0] != CACHE_KEY_VERSION
        or parts[1] != scope.scope_digest
        or any(_DIGEST.fullmatch(part) is None for part in parts[1:])
    ):
        _scope_invalid()
    return read(key)


@dataclass(frozen=True, slots=True)
class ResolvedWorkspaceScope:
    investigation_id: str
    frame: investigations.InvestigationFrame
    response: dict[str, object]
    scope_digest: str

    @property
    def output_mode(self) -> str:
        return self.frame.output_mode

    @property
    def binding(self) -> ScopeBinding:
        return ScopeBinding.from_frame(self.frame)

    def bind(self, boundary: str, *, time_binding: object) -> dict[str, object]:
        return bind_boundary(boundary, scope=self.binding, time_binding=time_binding)

    @property
    def scope_payload(self) -> dict[str, object]:
        return {
            "client_scope_id": self.frame.client_scope_id,
            "market_scope": list(self.frame.market_scope),
            "brand_config_id": self.frame.brand_config_id,
            "audience_lens_ids": list(self.frame.audience_lens_ids),
            "theme_id": self.frame.theme_id,
            "run_id": self.frame.run_id,
            "contract_version": self.frame.contract_version,
        }


def _workspace_bucket():
    try:
        deployment_contract.validate_workspace_runtime_contract()
    except deployment_contract.DeploymentContractError as error:
        raise WorkspaceScopeError(503, "workspace_unavailable", "Workspace is unavailable.") from error
    from google.cloud import storage

    bucket = storage.Client().bucket(_BUCKET_NAME)
    if getattr(bucket, "name", None) != _BUCKET_NAME:
        raise WorkspaceScopeError(503, "workspace_unavailable", "Workspace is unavailable.")
    return bucket


def resolve_workspace_scope(
    investigation_id: str, *, client_scope_id: str | None = None
) -> ResolvedWorkspaceScope:
    """Read one investigation under the server scope.

    The record is its own binding: there is no separate index for investigations,
    so the stored client_scope_id is checked on that single read, before the plan
    is returned and before any dependent private read (history, dossier, export).
    """
    if client_scope_id is None:
        try:
            client_scope_id = investigation_scopes.default_client_scope_id()
        except investigation_scopes.InvestigationScopeInvalid:
            _scope_invalid()
    if type(client_scope_id) is not str or not client_scope_id:
        _scope_invalid()
    if not isinstance(investigation_id, str) or _INVESTIGATION_ID.fullmatch(investigation_id) is None:
        raise WorkspaceScopeError(400, "workspace_request_invalid", "Workspace request is invalid.")
    try:
        bucket = _workspace_bucket()
        parts = investigation_store.read_investigation_parts(
            bucket=bucket,
            prefix=investigation_store.STAGING_PREFIX,
            investigation_id=investigation_id,
        )
    except investigation_store.InvestigationRecordInvalid:
        _scope_invalid()
    except WorkspaceScopeError:
        raise
    except investigation_store.InvestigationStoreError as error:
        raise WorkspaceScopeError(503, "workspace_unavailable", "Workspace is unavailable.") from error
    if parts is None:
        _scope_invalid()
    frame, response, _record = parts
    if frame.client_scope_id != client_scope_id:
        _scope_invalid()
    try:
        allowed = investigation_scopes.resolve_investigation_scope(
            client_scope_id=frame.client_scope_id,
            market_scope=frame.market_scope,
            brand_config_id=frame.brand_config_id,
            audience_lens_ids=frame.audience_lens_ids,
            theme_id=frame.theme_id,
        )
    except investigation_scopes.InvestigationScopeInvalid:
        _scope_invalid()
    if (
        allowed.client_scope_id != frame.client_scope_id
        or allowed.market_scope != frame.market_scope
        or allowed.brand_config_id != frame.brand_config_id
        or allowed.audience_lens_ids != frame.audience_lens_ids
        or allowed.theme_id != frame.theme_id
        or frame.contract_version != INVESTIGATION_CONTRACT_VERSION
    ):
        _scope_invalid()
    return ResolvedWorkspaceScope(
        investigation_id=investigation_id,
        frame=frame,
        response=dict(response),
        scope_digest=ScopeBinding.from_frame(frame).scope_digest,
    )
