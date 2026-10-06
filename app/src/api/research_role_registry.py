"""Fail-closed registry for optional neutral research roles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[2]
RESEARCH_ROLES_PATH = ROOT_DIR / "configs" / "research_roles.yaml"
CONTRACT_VERSION = "1.0.0"
_ROLE_FIELDS = frozenset(
    {
        "role_id",
        "label",
        "role_version",
        "status",
        "purpose",
        "allowed_evidence_families",
        "question_templates",
        "output_constraints",
        "review_date",
        "owner",
    }
)
_ROLE_STATUSES = frozenset({"active", "pilot", "blocked", "retired"})
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "audience_lens_id",
        "audience_lens_ids",
        "client_data",
        "default_market",
        "default_markets",
        "generation_instruction",
        "hidden_generation_instruction",
        "model",
        "model_name",
        "product_name",
        "prompt",
        "prompt_template",
        "system_prompt",
    }
)
APPROVED_ROLE_DIGESTS = MappingProxyType({})


class ResearchRoleInvalidError(ValueError):
    """Local rejection that intentionally reveals no registry details."""

    def __init__(self) -> None:
        super().__init__("research_role_invalid")


@dataclass(frozen=True)
class ResearchRole:
    role_id: str
    label: str
    role_version: str
    status: str
    purpose: str
    allowed_evidence_families: tuple[str, ...]
    question_templates: tuple[str, ...]
    output_constraints: tuple[Any, ...]
    review_date: str
    owner: str


@dataclass(frozen=True)
class ResearchRoleRegistry:
    contract_version: str
    default_role_id: None
    roles: tuple[ResearchRole, ...]


_registry_cache: (
    dict[tuple[Path, tuple[tuple[str, str], ...]], ResearchRoleRegistry] | None
) = None


def _invalid() -> None:
    raise ResearchRoleInvalidError()


def _freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    if value is None or type(value) in {bool, float, int, str}:
        return value
    _invalid()


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid()
    return value.strip()


def _texts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _invalid()
    return tuple(_text(item) for item in value)


def _canonical_value(value: Any) -> Any:
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            _invalid()
        return {key: _canonical_value(item) for key, item in value.items()}
    if type(value) is list:
        return [_canonical_value(item) for item in value]
    if value is None or type(value) in {bool, float, int, str}:
        return value
    _invalid()


def _role_digest(raw: dict) -> str:
    try:
        canonical = json.dumps(
            _canonical_value(raw),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        _invalid()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _contains_forbidden_field(value: Any) -> bool:
    if type(value) is dict:
        return any(
            key.casefold() in _FORBIDDEN_FIELD_NAMES or _contains_forbidden_field(item)
            for key, item in value.items()
        )
    if type(value) is list:
        return any(_contains_forbidden_field(item) for item in value)
    return False


def _approved_digest_items(
    approved_role_digests: Mapping[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    mapping = (
        APPROVED_ROLE_DIGESTS
        if approved_role_digests is None
        else approved_role_digests
    )
    if not isinstance(mapping, MappingProxyType):
        _invalid()
    items = tuple(mapping.items())
    for role_id, digest in items:
        if (
            type(role_id) is not str
            or type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            _invalid()
    return tuple(sorted(items))


def _role_from_raw(raw: Any) -> ResearchRole:
    _canonical_value(raw)
    if not isinstance(raw, dict) or set(raw) != _ROLE_FIELDS:
        _invalid()
    if _contains_forbidden_field(raw):
        _invalid()

    status = _text(raw["status"])
    if status not in _ROLE_STATUSES:
        _invalid()
    output_constraints = raw["output_constraints"]
    if not isinstance(output_constraints, list) or not output_constraints:
        _invalid()

    return ResearchRole(
        role_id=_text(raw["role_id"]),
        label=_text(raw["label"]),
        role_version=_text(raw["role_version"]),
        status=status,
        purpose=_text(raw["purpose"]),
        allowed_evidence_families=_texts(raw["allowed_evidence_families"]),
        question_templates=_texts(raw["question_templates"]),
        output_constraints=tuple(_freeze(item) for item in output_constraints),
        review_date=_text(raw["review_date"]),
        owner=_text(raw["owner"]),
    )


def _load_registry(
    path: Path, approved_role_digests: Mapping[str, str]
) -> ResearchRoleRegistry:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        _invalid()
    if not isinstance(raw, dict) or set(raw) != {
        "contract_version",
        "default_role_id",
        "roles",
    }:
        _invalid()
    if (
        raw["contract_version"] != CONTRACT_VERSION
        or raw["default_role_id"] is not None
    ):
        _invalid()
    if not isinstance(raw["roles"], list):
        _invalid()

    roles_with_digests = tuple(
        (_role_from_raw(item), _role_digest(item)) for item in raw["roles"]
    )
    if any(
        approved_role_digests.get(role.role_id) != digest
        for role, digest in roles_with_digests
    ):
        _invalid()
    roles = tuple(role for role, _digest in roles_with_digests)
    if len({role.role_id for role in roles}) != len(roles):
        _invalid()
    return ResearchRoleRegistry(
        contract_version=CONTRACT_VERSION,
        default_role_id=None,
        roles=roles,
    )


def load_research_role_registry(
    path: str | Path | None = None,
    *,
    approved_role_digests: Mapping[str, str] | None = None,
) -> ResearchRoleRegistry:
    """Load the neutral registry, optionally from an explicit test or local path."""
    global _registry_cache
    config_path = Path(path) if path is not None else RESEARCH_ROLES_PATH
    approved_items = _approved_digest_items(approved_role_digests)
    cache_key = (config_path.resolve(), approved_items)
    if _registry_cache is None:
        _registry_cache = {}
    if cache_key in _registry_cache:
        return _registry_cache[cache_key]
    registry = _load_registry(config_path, dict(approved_items))
    _registry_cache[cache_key] = registry
    return registry


def list_available_roles(
    *, approved_role_digests: Mapping[str, str] | None = None
) -> tuple[ResearchRole, ...]:
    """Return active roles only, sorted by stable role ID."""
    return tuple(
        sorted(
            (
                role
                for role in load_research_role_registry(
                    approved_role_digests=approved_role_digests
                ).roles
                if role.status == "active"
            ),
            key=lambda role: role.role_id,
        )
    )


def resolve_research_role(
    role_id: str | None,
    role_version: str | None,
    *,
    approved_role_digests: Mapping[str, str] | None = None,
) -> ResearchRole | None:
    """Resolve an exact active role version without exposing unavailable roles."""
    if role_id is None and role_version is None:
        return None
    if role_id is None or role_version is None:
        _invalid()
    if not isinstance(role_id, str) or not isinstance(role_version, str):
        _invalid()

    for role in load_research_role_registry(
        approved_role_digests=approved_role_digests
    ).roles:
        if (
            role.role_id == role_id
            and role.role_version == role_version
            and role.status == "active"
        ):
            return role
    _invalid()


__all__ = [
    "ResearchRole",
    "APPROVED_ROLE_DIGESTS",
    "ResearchRoleInvalidError",
    "ResearchRoleRegistry",
    "list_available_roles",
    "load_research_role_registry",
    "resolve_research_role",
]
