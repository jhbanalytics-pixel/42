"""Select one optional client lens by authorization, and keep its gaps visible.

General 42 is the default and names no lens. A lens exists only as a registry
entry naming the client scope it belongs to, the brand configuration it carries
and the exact configuration bytes it authorizes. An unregistered lens, a lens
belonging to another client scope, a lens whose brand does not match the request
and a lens whose configuration bytes have moved are four separate refusals, each
of them before the configuration is used.

Handing a brand name to the loader selects nothing on its own. A request that
names a lens is bound to that lens's entry. A request that names none is not
left unconfigured either: ``authorize_scope_configuration`` resolves the single
enabled entry for that request's own client scope and brand configuration and
pins the same bytes, so the client purpose policy keeps running on the general
path while the selection still comes from an entry rather than from a name. A
brand under a client scope no entry names, and a brand no entry carries at all,
are refused here rather than loaded.

A resolved lens also carries the client inputs it still lacks. Those names are
read from the configuration document itself and are never supplied here: an
entity's pending inputs, an empty verified handle or ambassador roster, and a
named input document that resolves to nothing. ``require_client_input`` is the
refusal available to work that needs one. The first such work is a benchmarking
question: the planner and the answering model are handed the comparator set only
once its input document resolves, and are told by name that it is owed until
then. An input document whose values are absent or hold a placeholder stays
unresolved, so nothing can put a plausible value where a client input is owed,
and no input document exists in the tree for the shipped configuration to
resolve.

``request_lens_view`` is what the model calls see. It admits the lens a stored
request asserts again on that request's own scope through ``admit_request_lens``
and ``client_lens_binding``, refuses a pin that no longer matches the bytes the
entry authorizes, and hands over ``lens_planning_view`` for that request, so the
same wording is planned and answered under what its own lens means. General 42
sees nothing and its model bytes are unchanged.

``authorized_client_lenses`` here mirrors the roster the application serves
from its own registry copy; the engine has no roster surface of its own.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml

from src.enrichment import topic_overlays

CLIENT_LENS_REGISTRY_VERSION = "client_lens_registry_v1"
CLIENT_LENS_BINDING_VERSION = "client_lens_binding_v1"
CLIENT_LENS_INPUT_VERSION = "client_lens_input_v1"
DEFAULT_CLIENT_LENS_ID = None

_CONFIGS = Path(__file__).resolve().parents[3] / "configs"
CLIENT_LENS_REGISTRY_PATH = _CONFIGS / "client_lenses.yaml"
CLIENT_LENS_INPUTS_DIR = _CONFIGS / "client_lens_inputs"

ENTRY_FIELDS = (
    "client_lens_id",
    "label",
    "client_scope_id",
    "brand_config_id",
    "overlay_id",
    "configuration_digest",
    "enabled",
)
INPUT_ENTRY_FIELDS = ("entry_id", "canonical_domain", "locale_aliases", "proof_ref")
# The configuration fields that name an input document the client owes.
INPUT_REFS = ("comparator_set_ref", "search_visibility_policy_ref")

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
# The same placeholder vocabulary the client overlay compiler refuses, so a
# value that only looks supplied cannot pass here either.
_PLACEHOLDER = re.compile(
    r"(?i)(?:(?<![a-z0-9])(?:tbc|tbd|todo|placeholder|pending|unsupplied|xxx+)(?![a-z0-9])"
    r"|<[^<>]*>|\{\{[^{}]*\}\})"
)


class ClientLensUnavailable(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClientInputUnavailable(ValueError):
    def __init__(self, input_name: str) -> None:
        self.code = "client_input_unavailable"
        self.input_name = input_name
        super().__init__(f"client_input_unavailable:{input_name}")


@dataclass(frozen=True, slots=True)
class ClientLens:
    client_lens_id: str
    label: str
    client_scope_id: str
    brand_config_id: str
    overlay_id: str
    configuration_digest: str
    overlay: MappingProxyType
    unresolved_client_inputs: tuple[str, ...]
    resolved_client_inputs: MappingProxyType


def _refuse(code: str) -> None:
    raise ClientLensUnavailable(code)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def overlay_digest(overlay_id: str, *, root: Path | None = None) -> str:
    """The sha256 of the configuration document's exact bytes."""
    if not _identifier(overlay_id):
        _refuse("client_lens_unauthorized")
    directory = topic_overlays.TOPIC_OVERLAYS_DIR if root is None else Path(root)
    try:
        return hashlib.sha256((directory / f"{overlay_id}.yaml").read_bytes()).hexdigest()
    except OSError as error:
        raise ClientLensUnavailable("client_lens_configuration_unavailable") from error


def load_client_lens_registry(path: str | Path | None = None) -> dict:
    """Read and validate the registry; an unknown shape is one refusal."""
    location = CLIENT_LENS_REGISTRY_PATH if path is None else Path(path)
    try:
        document = yaml.safe_load(location.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ClientLensUnavailable("client_lens_registry_invalid") from error
    if not isinstance(document, dict) or set(document) != {
        "contract_version",
        "default_client_lens_id",
        "lenses",
    }:
        _refuse("client_lens_registry_invalid")
    if document["contract_version"] != CLIENT_LENS_REGISTRY_VERSION:
        _refuse("client_lens_registry_invalid")
    if document["default_client_lens_id"] is not DEFAULT_CLIENT_LENS_ID:
        _refuse("client_lens_registry_invalid")
    lenses = document["lenses"]
    if not isinstance(lenses, dict) or not lenses:
        _refuse("client_lens_registry_invalid")
    for key, entry in lenses.items():
        if not _identifier(key) or not isinstance(entry, dict) or set(entry) != set(ENTRY_FIELDS):
            _refuse("client_lens_registry_invalid")
        if entry["client_lens_id"] != key:
            _refuse("client_lens_registry_invalid")
        for field in ("client_scope_id", "brand_config_id", "overlay_id"):
            if not _identifier(entry[field]):
                _refuse("client_lens_registry_invalid")
        if not isinstance(entry["label"], str) or not entry["label"].strip():
            _refuse("client_lens_registry_invalid")
        if (
            not isinstance(entry["configuration_digest"], str)
            or _DIGEST.fullmatch(entry["configuration_digest"]) is None
        ):
            _refuse("client_lens_registry_invalid")
        if entry["enabled"] is not True and entry["enabled"] is not False:
            _refuse("client_lens_registry_invalid")
    return copy.deepcopy(document)


def authorized_client_lenses(
    *, client_scope_id: object, registry_path: str | Path | None = None
) -> list[dict]:
    """What a caller under this client scope may choose, general 42 excluded."""
    registry = load_client_lens_registry(registry_path)
    return [
        {
            "client_lens_id": entry["client_lens_id"],
            "label": entry["label"],
            "configuration_digest": entry["configuration_digest"],
        }
        for entry in registry["lenses"].values()
        if entry["enabled"] and entry["client_scope_id"] == client_scope_id
    ]


def authorize_scope_configuration(
    scope: object,
    *,
    registry_path: str | Path | None = None,
    overlay_root: str | Path | None = None,
) -> dict | None:
    """The registry entry that authorizes this scope's configuration, or none.

    A brand name is not an authorization. The entry that names both this
    request's own client scope and the brand configuration it carries is what
    selects the document, and the bytes that entry pins are checked before the
    document is read. A brand no entry carries, a brand whose entry belongs to
    another client scope, and moved bytes are three refusals rather than a load.
    """
    brand = scope.get("brand_config_id") if isinstance(scope, dict) else None
    if brand is None:
        return None
    client_scope_id = scope.get("client_scope_id")
    registry = load_client_lens_registry(registry_path)
    entries = [
        entry
        for entry in registry["lenses"].values()
        if entry["enabled"]
        and entry["brand_config_id"] == brand
        and isinstance(client_scope_id, str)
        and entry["client_scope_id"] == client_scope_id
    ]
    if len(entries) != 1:
        _refuse("client_lens_unauthorized")
    entry = entries[0]
    root = None if overlay_root is None else Path(overlay_root)
    if overlay_digest(entry["overlay_id"], root=root) != entry["configuration_digest"]:
        _refuse("client_lens_configuration_changed")
    return copy.deepcopy(entry)


def _input_document(input_id: str, root: Path | None) -> dict | None:
    """One named client input, or None while it is absent or incomplete."""
    if not _identifier(input_id):
        return None
    directory = CLIENT_LENS_INPUTS_DIR if root is None else Path(root)
    try:
        document = yaml.safe_load((directory / f"{input_id}.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(document, dict) or set(document) != {
        "contract_version",
        "input_id",
        "entries",
    }:
        return None
    if (
        document["contract_version"] != CLIENT_LENS_INPUT_VERSION
        or document["input_id"] != input_id
    ):
        return None
    entries = document["entries"]
    if not isinstance(entries, list) or not entries:
        return None
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != set(INPUT_ENTRY_FIELDS):
            return None
        for field in ("entry_id", "canonical_domain", "proof_ref"):
            value = entry[field]
            if not isinstance(value, str) or not value.strip():
                return None
            if _PLACEHOLDER.search(value) is not None:
                return None
        aliases = entry["locale_aliases"]
        if not isinstance(aliases, dict) or not aliases:
            return None
        for market, alias in aliases.items():
            if not isinstance(market, str) or not market.strip():
                return None
            if not isinstance(alias, str) or not alias.strip():
                return None
            if _PLACEHOLDER.search(alias) is not None:
                return None
    return copy.deepcopy(document)


def _client_inputs(overlay: dict, inputs_root: Path | None) -> tuple[tuple[str, ...], dict]:
    """Which client inputs this configuration still lacks, read from itself."""
    unresolved: set[str] = set()
    resolved: dict[str, dict] = {}
    for field in INPUT_REFS:
        input_id = overlay.get(field)
        if not isinstance(input_id, str) or not input_id.strip():
            continue
        document = _input_document(input_id, inputs_root)
        if document is None:
            unresolved.add(input_id)
        else:
            resolved[input_id] = document
    for roster in ("verified_handles", "ambassador_refs"):
        if not overlay.get(roster):
            unresolved.add(roster)
    for entity_id, entity in overlay.get("entities", {}).items():
        if entity.get("pending_inputs"):
            unresolved.add(f"entities.{entity_id}.pending_inputs")
        if "known_ambassador_handles" in entity and not entity["known_ambassador_handles"]:
            unresolved.add(f"entities.{entity_id}.known_ambassador_handles")
    return tuple(sorted(unresolved)), resolved


def resolve_client_lens(
    *,
    client_scope_id: object,
    brand_config_id: object,
    client_lens_id: object,
    registry_path: str | Path | None = None,
    overlay_root: str | Path | None = None,
    inputs_root: str | Path | None = None,
) -> ClientLens | None:
    """Resolve the named lens for this client scope, or general 42 when none is named."""
    if client_lens_id is DEFAULT_CLIENT_LENS_ID:
        return None
    if not _identifier(client_lens_id):
        _refuse("client_lens_id_invalid")
    registry = load_client_lens_registry(registry_path)
    entry = registry["lenses"].get(client_lens_id)
    if (
        entry is None
        or not entry["enabled"]
        or not isinstance(client_scope_id, str)
        or entry["client_scope_id"] != client_scope_id
    ):
        _refuse("client_lens_unauthorized")
    if entry["brand_config_id"] != brand_config_id:
        _refuse("client_lens_binding_invalid")
    root = None if overlay_root is None else Path(overlay_root)
    if overlay_digest(entry["overlay_id"], root=root) != entry["configuration_digest"]:
        _refuse("client_lens_configuration_changed")
    try:
        overlay = topic_overlays.load_topic_overlay(entry["overlay_id"], root=root)
    except topic_overlays.TopicOverlayInvalid as error:
        raise ClientLensUnavailable("client_lens_configuration_invalid") from error
    if (
        overlay["brand_config_id"] != entry["brand_config_id"]
        or overlay["client_scope_id"] != entry["client_scope_id"]
    ):
        _refuse("client_lens_binding_invalid")
    unresolved, resolved = _client_inputs(
        overlay, None if inputs_root is None else Path(inputs_root)
    )
    return ClientLens(
        client_lens_id=entry["client_lens_id"],
        label=entry["label"],
        client_scope_id=entry["client_scope_id"],
        brand_config_id=entry["brand_config_id"],
        overlay_id=entry["overlay_id"],
        configuration_digest=entry["configuration_digest"],
        overlay=_freeze(overlay),
        unresolved_client_inputs=unresolved,
        resolved_client_inputs=_freeze(resolved),
    )


def client_lens_binding(lens: ClientLens | None) -> dict:
    """What every boundary records about the lens, general 42 included."""
    if lens is None:
        return {
            "lens_binding_version": CLIENT_LENS_BINDING_VERSION,
            "client_lens_id": DEFAULT_CLIENT_LENS_ID,
            "configuration_digest": None,
            "unresolved_client_inputs": [],
        }
    if not isinstance(lens, ClientLens):
        _refuse("client_lens_binding_invalid")
    return {
        "lens_binding_version": CLIENT_LENS_BINDING_VERSION,
        "client_lens_id": lens.client_lens_id,
        "configuration_digest": lens.configuration_digest,
        "unresolved_client_inputs": list(lens.unresolved_client_inputs),
    }


def require_client_input(lens: ClientLens | None, input_name: str) -> dict:
    """Return one supplied client input, or refuse by its name while it is owed."""
    if lens is None or not isinstance(lens, ClientLens):
        raise ClientInputUnavailable(input_name)
    document = lens.resolved_client_inputs.get(input_name)
    if document is None:
        raise ClientInputUnavailable(input_name)
    return _thaw(document)


def _input_supplied(lens: ClientLens, field: str) -> bool:
    input_id = lens.overlay.get(field)
    return isinstance(input_id, str) and input_id in lens.resolved_client_inputs


def lens_planning_view(lens: ClientLens, request: dict) -> dict:
    """What a planner may see from one bound lens for one normalized request.

    The lens adds its identity and its configuration digest to the overlay's own
    planning view, and states which claims it cannot support. A comparison or a
    search visibility claim needs a client input the first client has not
    supplied, so both stay refused until that input resolves.
    """
    if not isinstance(lens, ClientLens):
        _refuse("client_lens_binding_invalid")
    if not isinstance(request, dict) or request.get("client_scope_id") != lens.client_scope_id:
        _refuse("client_lens_unauthorized")
    view = topic_overlays.overlay_planning_view(_thaw(lens.overlay), request)
    view.update(
        lens_binding_version=CLIENT_LENS_BINDING_VERSION,
        client_lens_id=lens.client_lens_id,
        configuration_digest=lens.configuration_digest,
        unresolved_client_inputs=list(lens.unresolved_client_inputs),
        comparator_claims_allowed=_input_supplied(lens, "comparator_set_ref"),
        search_visibility_claims_allowed=_input_supplied(lens, "search_visibility_policy_ref"),
    )
    return view


def _comparator_set(lens: ClientLens, question: object) -> dict | None:
    """The comparator set a benchmarking question needs, or the name it is owed by.

    Only a question the configuration itself reads as a comparison asks for the
    set. The set is handed over only once its input document resolves; until
    then the planner is told the set is owed and is given no entries, so nothing
    can stand in for the list the client has not supplied.
    """
    from src.analysis.open_intelligence.client_overlay import classify_dimensions

    matched = classify_dimensions({"dimensions": _thaw(lens.overlay["dimensions"])}, question)
    if not any("comparator" in value["distinctions"] for value in matched.values()):
        return None
    input_id = lens.overlay.get("comparator_set_ref")
    try:
        document = require_client_input(lens, input_id)
    except ClientInputUnavailable:
        return {"input_id": input_id, "status": "owed", "entries": []}
    return {"input_id": input_id, "status": "supplied", "entries": document["entries"]}


def request_lens_view(request: dict) -> dict | None:
    """What a model call may see from the lens a stored request was admitted under.

    General 42 names no lens and sees nothing. A stored request asserts a lens
    rather than granting one, so the lens is admitted again on the request's own
    scope and the bytes the request pinned must be the bytes the entry
    authorizes now before its planning view is handed to any model.
    """
    binding = request.get("client_lens") if isinstance(request, dict) else None
    if binding is None:
        return None
    if not isinstance(binding, dict):
        _refuse("client_lens_binding_invalid")
    lens, bound = admit_request_lens(request, client_lens_id=binding.get("client_lens_id"))
    if lens is None or bound["configuration_digest"] != binding.get("configuration_digest"):
        _refuse("client_lens_configuration_changed")
    view = lens_planning_view(lens, request)
    view["comparator_set"] = _comparator_set(lens, request.get("question"))
    return view


def admit_request_lens(
    request: dict,
    *,
    client_lens_id: object,
    registry_path: str | Path | None = None,
    overlay_root: str | Path | None = None,
    inputs_root: str | Path | None = None,
) -> tuple[ClientLens | None, dict]:
    """Bind one lens to one normalized request, on that request's own scope."""
    if not isinstance(request, dict):
        _refuse("client_lens_unauthorized")
    lens = resolve_client_lens(
        client_scope_id=request.get("client_scope_id"),
        brand_config_id=request.get("brand_config_id"),
        client_lens_id=client_lens_id,
        registry_path=registry_path,
        overlay_root=overlay_root,
        inputs_root=inputs_root,
    )
    return lens, client_lens_binding(lens)


__all__ = [
    "CLIENT_LENS_BINDING_VERSION",
    "CLIENT_LENS_INPUT_VERSION",
    "CLIENT_LENS_REGISTRY_VERSION",
    "DEFAULT_CLIENT_LENS_ID",
    "ClientInputUnavailable",
    "ClientLens",
    "ClientLensUnavailable",
    "admit_request_lens",
    "authorize_scope_configuration",
    "authorized_client_lenses",
    "client_lens_binding",
    "lens_planning_view",
    "load_client_lens_registry",
    "overlay_digest",
    "request_lens_view",
    "require_client_input",
    "resolve_client_lens",
]
