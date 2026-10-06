"""Approved Open Intelligence contract primitives."""

from dataclasses import dataclass
from pathlib import Path

from src.utils.config_loader import CONFIGS_DIR, load_yaml

CONTRACT_VERSION = "2.0.0"
EVIDENCE_STATES = ("ready", "thin", "contradictory", "unchecked")
LINEAGE_RELATIONS = ("continues", "merges_into", "splits_into")
SOURCE_LAB_STATUSES = (
    "active",
    "pilot",
    "available_unwired",
    "blocked",
    "permanently_rejected",
    "inventory_only",
)
ROUTE_ROLES = (
    "evidence",
    "utility_balance",
    "utility_catalog",
    "identity_discovery",
    "identity_hygiene",
    "inventory",
)
IDENTITY_HEALTH_STATES = (
    "active",
    "quiet",
    "dead",
    "handle_drift",
    "upstream_exhausted",
    "unknown",
)
PROFILE_EVIDENCE_STATES = ("live", "stale", "not_found", "unavailable")
POST_EVIDENCE_STATES = (
    "sampled",
    "observed_empty",
    "not_supported",
    "not_found",
    "unavailable",
)
QUESTION_INTENTS = (
    "landscape",
    "explanation",
    "comparison",
    "trajectory",
    "audience",
    "creator",
    "brand_role",
    "whitespace",
    "risk",
    "historical_analogue",
    "campaign_opportunity",
    "source_coverage",
    "custom",
)
ASYNC_STATES = ("pending", "running", "complete", "partial", "failed")
OUTCOME_STATES = ("peaked", "sustained", "fizzled", "noise", "unresolved")
TOKEN_CEILINGS = {
    "dynamic_signal_summary": {"input": 8000, "output": 800},
    "open_question_answer": {"input": 32000, "output": 4000},
}
MONTHLY_OPTIONAL_CREDIT_CAP = 25000
RUNWAY_FLOOR_DAYS = 14


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str
    audience_lens_ids: tuple[str, ...]
    theme_id: str
    run_id: str
    contract_version: str


def encode_identifier_part(value: str | int | None) -> str:
    if value is None:
        return "N:"
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TypeError("identifier parts must be str, int, or None")
    canonical_value = str(value)
    return f"V{len(canonical_value.encode('utf-8'))}:{canonical_value}"


def resolve_client_scope(
    *,
    run_id: str,
    client_scope_id: str | None = None,
    market_scope: tuple[str, ...] | None = None,
    audience_lens_ids: tuple[str, ...] | None = None,
    brand_config_id: str | None = None,
    theme_id: str | None = None,
    config_path: Path | None = None,
) -> ResolvedScope:
    config = load_yaml(config_path or CONFIGS_DIR / "client_scopes.yaml")
    if config["contract_version"] != CONTRACT_VERSION:
        raise ValueError("config contract_version does not match the accepted contract")
    scope_id = config["default_scope_id"] if client_scope_id is None else client_scope_id
    scope = config["scopes"].get(scope_id)
    if scope is None:
        raise ValueError(f"unknown client scope: {scope_id}")
    if not scope["enabled"]:
        raise ValueError(f"disabled client scope: {scope_id}")
    if brand_config_id is not None and brand_config_id != scope["brand_config_id"]:
        raise ValueError(f"unknown brand_config_id for scope: {brand_config_id}")
    if theme_id is not None and theme_id != scope["theme_id"]:
        raise ValueError(f"unknown theme_id for scope: {theme_id}")

    configured_markets = tuple(scope["market_scope"])
    resolved_markets = configured_markets if market_scope is None else tuple(market_scope)
    if not resolved_markets:
        raise ValueError("market_scope must be nonempty")
    if any(market != market.lower() for market in resolved_markets):
        raise ValueError("market_scope values must be lower case")
    if len(set(resolved_markets)) != len(resolved_markets):
        raise ValueError("market_scope values must be unique")
    if not set(resolved_markets).issubset(configured_markets):
        raise ValueError("market_scope must be a configured subset")

    configured_lenses = tuple(scope["audience_lens_ids"])
    resolved_lenses = configured_lenses if audience_lens_ids is None else tuple(audience_lens_ids)
    if len(set(resolved_lenses)) != len(resolved_lenses):
        raise ValueError("audience_lens_ids must be unique")
    if not set(resolved_lenses).issubset(configured_lenses):
        raise ValueError("audience_lens_ids must be a configured subset")

    return ResolvedScope(
        client_scope_id=scope["client_scope_id"],
        market_scope=resolved_markets,
        brand_config_id=scope["brand_config_id"],
        audience_lens_ids=resolved_lenses,
        theme_id=scope["theme_id"],
        run_id=run_id,
        contract_version=CONTRACT_VERSION,
    )
