"""Detect which market (ng, za, ke) content belongs to based on signals."""

import functools

from src.utils.config_loader import get_active_markets, load_market_keywords


@functools.lru_cache(maxsize=8)
def _cached_market_keywords(market: str) -> dict:
    return load_market_keywords(market)


def detect_market(
    text: str,
    author_handle: str = "",
    source_region: str = "",
    platform_region: str = "",
) -> str:
    """Assign a market code based on content signals.

    Priority: explicit region > keyword matching > default empty.
    """
    text_lower = (text or "").lower()
    handle_lower = (author_handle or "").lower()

    # Priority 1: Explicit region from API response
    region_map = {
        "za": "za",
        "south africa": "za",
        "zaf": "za",
        "ng": "ng",
        "nigeria": "ng",
        "nga": "ng",
        "ke": "ke",
        "kenya": "ke",
        "ken": "ke",
    }
    for region_str in (source_region, platform_region):
        mapped = region_map.get((region_str or "").lower().strip())
        if mapped:
            return mapped

    # Priority 2: Keyword matching against market configs
    scores = {}
    for market in get_active_markets():
        try:
            config = _cached_market_keywords(market)
        except (FileNotFoundError, ValueError):
            continue
        markers = config.get("regional_markers", [])
        score = sum(1 for m in markers if m.lower() in text_lower or m.lower() in handle_lower)
        if score > 0:
            scores[market] = score

    if scores:
        return max(scores, key=scores.get)

    return ""  # Unknown market
