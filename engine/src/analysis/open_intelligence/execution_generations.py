import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .execution_origins import OriginRefusal, OriginRegistry, _bounded_file, load_origin_registry

_HEX_64 = re.compile(r"[0-9a-f]{64}")
_ACTIONS = frozenset({"deploy", "invoke", "read", "write"})
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]

# Reviewed generations, in the order they were admitted. Each entry is
# (origin_registry_sha256, resource_manifest_sha256, registry_relative_path,
# resource_manifest_relative_path). Append a reviewed entry to retain a prior
# generation; never edit or reorder an existing one's digests. Amendment d replaced the
# single entry by ruling: it was itself a replacement of the pair the store still
# holds active, and no v2 record was ever written under it. Amendment e does not
# replace: amendment d is live and the ledger has run under its pair, so that entry
# stays trusted, with its path moved to a retained copy of the same reviewed bytes
# (resource_manifest_v1.json now carries the amendment e manifest), and the amendment
# e pair is appended. Records written under ee809a4e keep reading by their own pair;
# fresh approval and consumption use only the active pair below. The bridge v3 generation
# is the third entry and the active pair: its registry links the v3 capture policy, and
# its manifest is the amendment e manifest under that registry's digest, so it carries
# every amendment e row. It replaced its own inactive candidate entry, under which no
# record was ever written; amendment d and amendment e stay trusted for reading.
# Amendment g added the route A capture's artifact bucket to the bridge manifest before
# the rotation to it, so the bridge entry was replaced again (manifest 1643e4ce to
# ab7802f4, registry unchanged); no record was ever written under 1643e4ce.
# The daily contract revision then registered three daily operations
# and bounded exposure, so the pair moved again: registry 573deeea to e6b95e35, manifest
# ab7802f4 to b7553041.
# Tightening the date regex to ASCII digits then moved them to f23001ed and 592ed45f.
_TRUSTED_GENERATIONS = (
    (
        "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
        "ee809a4e81dec5242ea71ddd703990c5e0a9e237613e11135e069be0ea51ad96",
        "configs/open_intelligence/execution_origins_v1.json",
        "configs/open_intelligence/resource_manifest_amendment_d.json",
    ),
    (
        "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
        "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09",
        "configs/open_intelligence/execution_origins_v1.json",
        "configs/open_intelligence/resource_manifest_v1.json",
    ),
    (
        "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2",
        "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe",
        "configs/open_intelligence/execution_origins_bridge_v3.json",
        "configs/open_intelligence/resource_manifest_bridge_v3.json",
    ),
)
# The single pair that fresh approval and consumption may use.
ACTIVE_GENERATION_PAIR = (
    "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2",
    "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe",
)


@dataclass(frozen=True, slots=True)
class TrustedGeneration:
    origin_registry_sha256: str
    resource_manifest_sha256: str
    registry: OriginRegistry
    resource_manifest: Mapping[str, object]
    registry_path: Path
    resource_manifest_path: Path


def _refuse(code: str) -> None:
    raise OriginRefusal(code)


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _duplicate_safe_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _refuse("execution_generation_invalid")
        value[key] = item
    return value


def _reject_nonfinite(_value: str) -> None:
    _refuse("execution_generation_invalid")


def _resource_manifest_bytes(path: Path, *, expected_sha256: str) -> bytes:
    try:
        raw = _bounded_file(path, _PACKAGE_ROOT)
    except OriginRefusal as error:
        raise OriginRefusal("execution_generation_invalid") from error
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        _refuse("execution_generation_digest_mismatch")
    return raw


def _parse_resource_manifest(raw: bytes, *, origin_registry_sha256: str) -> Mapping[str, object]:
    if raw.startswith(b"\xef\xbb\xbf"):
        _refuse("execution_generation_invalid")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_nonfinite,
        )
    except OriginRefusal:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise OriginRefusal("execution_generation_invalid") from error
    if (
        not isinstance(payload, dict)
        or payload.get("origin_registry_sha256") != origin_registry_sha256
        or not isinstance(payload.get("project"), str)
        or not isinstance(payload.get("region"), str)
        or not isinstance(payload.get("resources"), list)
        or not isinstance(payload.get("identities"), dict)
    ):
        _refuse("execution_generation_invalid")
    for row in payload["resources"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"name", "actions"}
            or not isinstance(row["name"], str)
            or not isinstance(row["actions"], list)
            or any(not isinstance(action, str) for action in row["actions"])
        ):
            _refuse("execution_generation_invalid")
    if any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in payload["identities"].items()
    ):
        _refuse("execution_generation_invalid")
    return _freeze(payload)


def load_trusted_generation(
    origin_registry_sha256: str,
    resource_manifest_sha256: str,
) -> TrustedGeneration:
    if not _is_digest(origin_registry_sha256) or not _is_digest(resource_manifest_sha256):
        _refuse("execution_generation_unknown")
    entries = [
        entry
        for entry in _TRUSTED_GENERATIONS
        if entry[:2] == (origin_registry_sha256, resource_manifest_sha256)
    ]
    if len(entries) != 1:
        _refuse("execution_generation_unknown")
    _registry_sha256, _resource_sha256, registry_relative, resource_relative = entries[0]
    registry_path = _PACKAGE_ROOT / registry_relative
    resource_manifest_path = _PACKAGE_ROOT / resource_relative
    registry = load_origin_registry(
        registry_path,
        expected_sha256=origin_registry_sha256,
        contract_root=_PACKAGE_ROOT,
    )
    resource_manifest = _parse_resource_manifest(
        _resource_manifest_bytes(resource_manifest_path, expected_sha256=resource_manifest_sha256),
        origin_registry_sha256=registry.sha256,
    )
    return TrustedGeneration(
        origin_registry_sha256=origin_registry_sha256,
        resource_manifest_sha256=resource_manifest_sha256,
        registry=registry,
        resource_manifest=resource_manifest,
        registry_path=registry_path,
        resource_manifest_path=resource_manifest_path,
    )


def active_generation() -> TrustedGeneration:
    return load_trusted_generation(*ACTIVE_GENERATION_PAIR)


def require_active_generation(
    origin_registry_sha256: str,
    resource_manifest_sha256: str,
) -> TrustedGeneration:
    generation = load_trusted_generation(origin_registry_sha256, resource_manifest_sha256)
    if (origin_registry_sha256, resource_manifest_sha256) != ACTIVE_GENERATION_PAIR:
        _refuse("execution_generation_inactive")
    return generation


def assert_resource_allowed(resource: object, action: object, *, generation: object) -> None:
    """Exact membership in the selected generation's resource manifest.

    Mirrors the reviewed deployment guard's admission rule: one exact provider-qualified
    name carrying the action, with no prefix, suffix or case tolerance.
    """
    if (
        type(generation) is not TrustedGeneration
        or type(resource) is not str
        or type(action) is not str
        or action not in _ACTIONS
    ):
        _refuse("resource_action_forbidden")
    for row in generation.resource_manifest["resources"]:
        if row["name"] == resource and action in row["actions"]:
            return
    _refuse("resource_action_forbidden")


__all__ = [
    "ACTIVE_GENERATION_PAIR",
    "TrustedGeneration",
    "active_generation",
    "assert_resource_allowed",
    "load_trusted_generation",
    "require_active_generation",
]
