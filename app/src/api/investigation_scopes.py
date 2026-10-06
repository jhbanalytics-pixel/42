"""Pinned audience-neutral scope resolution for Evidence Room 2.1."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parents[2]
INVESTIGATION_SCOPES_PATH = ROOT_DIR / "configs" / "investigation_scopes.yaml"
CONFIG_DIGEST = "e1a90542a42ff327cfa20b7145494ce127e5e820e25f0d8e26c5a14978ff659d"


class InvestigationScopeInvalid(ValueError):
    def __init__(self) -> None:
        super().__init__("scope_invalid")


@dataclass(frozen=True, slots=True)
class ResolvedInvestigationScope:
    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str | None
    audience_lens_ids: tuple[str, ...]
    theme_id: str | None


def _invalid() -> None:
    raise InvestigationScopeInvalid()


def _strings(value: object, *, nonempty: bool) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        _invalid()
    items = tuple(value)
    if nonempty and not items:
        _invalid()
    if any(not isinstance(item, str) or not item for item in items):
        _invalid()
    if len(items) != len(set(items)):
        _invalid()
    return items


def _load(path: Path) -> dict:
    try:
        raw_bytes = path.read_bytes()
    except OSError:
        _invalid()
    if hashlib.sha256(raw_bytes).hexdigest() != CONFIG_DIGEST:
        _invalid()
    try:
        config = yaml.safe_load(raw_bytes)
    except yaml.YAMLError:
        _invalid()
    if not isinstance(config, dict) or set(config) != {
        "contract_version",
        "default_scope_id",
        "scopes",
    }:
        _invalid()
    if (
        config["contract_version"] != "2.1.0"
        or config["default_scope_id"] != "ogilvy_default"
        or not isinstance(config["scopes"], dict)
    ):
        _invalid()
    return config


def default_client_scope_id(path: str | Path | None = None) -> str:
    """The server owned scope every read runs under until an authorized selector exists."""
    return _load(Path(path) if path is not None else INVESTIGATION_SCOPES_PATH)["default_scope_id"]


def resolve_investigation_scope(
    *,
    client_scope_id: object,
    market_scope: object,
    brand_config_id: object,
    audience_lens_ids: object,
    theme_id: object,
    path: str | Path | None = None,
) -> ResolvedInvestigationScope:
    if not isinstance(client_scope_id, str) or not client_scope_id:
        _invalid()
    config = _load(Path(path) if path is not None else INVESTIGATION_SCOPES_PATH)
    raw_scope = config["scopes"].get(client_scope_id)
    if not isinstance(raw_scope, dict) or set(raw_scope) != {
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "enabled",
    }:
        _invalid()
    if raw_scope["client_scope_id"] != client_scope_id or raw_scope["enabled"] is not True:
        _invalid()
    configured_markets = _strings(raw_scope["market_scope"], nonempty=True)
    requested_markets = _strings(market_scope, nonempty=True)
    if any(market != market.lower() for market in requested_markets):
        _invalid()
    if not set(requested_markets).issubset(configured_markets):
        _invalid()
    configured_lenses = _strings(raw_scope["audience_lens_ids"], nonempty=False)
    requested_lenses = _strings(audience_lens_ids, nonempty=False)
    if not set(requested_lenses).issubset(configured_lenses):
        _invalid()
    if brand_config_id != raw_scope["brand_config_id"] or theme_id != raw_scope["theme_id"]:
        _invalid()
    return ResolvedInvestigationScope(
        client_scope_id=client_scope_id,
        market_scope=requested_markets,
        brand_config_id=raw_scope["brand_config_id"],
        audience_lens_ids=requested_lenses,
        theme_id=raw_scope["theme_id"],
    )
