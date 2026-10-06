"""Configuration loader.

Configs load straight from YAML with no schema validation: the configs/schemas/
directory was never created, so the old JSON Schema path was always a no-op.
Malformed YAML still raises through load_yaml; structural correctness is pinned
by the unit tests in tests/unit/test_config_loader.py instead.
"""

from pathlib import Path
from typing import Any

import yaml

CONFIGS_DIR = Path(__file__).parent.parent.parent / "configs"


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML config file."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_market_keywords(market: str) -> dict[str, Any]:
    """Load keywords config for a specific market (za, ng, ke)."""
    path = CONFIGS_DIR / "keywords" / f"{market}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Keywords config not found for market: {market}")
    return load_yaml(path)


def load_market_creators(market: str) -> dict[str, Any]:
    """Load creator watchlist for a specific market."""
    path = CONFIGS_DIR / "creators" / f"{market}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Creators config not found for market: {market}")
    return load_yaml(path)


def load_sources() -> dict[str, Any]:
    """Load the sources configuration."""
    return load_yaml(CONFIGS_DIR / "sources.yaml")


def load_scoring() -> dict[str, Any]:
    """Load the scoring configuration."""
    return load_yaml(CONFIGS_DIR / "scoring.yaml")


def load_alerts() -> dict[str, Any]:
    """Load the alerts configuration."""
    return load_yaml(CONFIGS_DIR / "alerts.yaml")


def load_trend_cycles() -> dict[str, Any]:
    """Load trend cycle definitions."""
    return load_yaml(CONFIGS_DIR / "trend_cycles.yaml")


def load_entity_aliases() -> dict[str, Any]:
    """Load the event-ledger entity alias map (configs/entity_aliases.yaml).

    Returns {} when the file is missing so a fresh checkout without the
    config still runs; the ledger treats an empty map as "no extra aliases".
    """
    path = CONFIGS_DIR / "entity_aliases.yaml"
    if not path.exists():
        return {}
    return load_yaml(path)


def get_active_markets() -> list[str]:
    """Return list of markets with keyword configs."""
    keywords_dir = CONFIGS_DIR / "keywords"
    return [p.stem for p in keywords_dir.glob("*.yaml") if p.stem in ("za", "ng", "ke")]
