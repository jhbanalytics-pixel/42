"""EnsembleData /customer/* helpers for unit-spend introspection.

Both endpoints in this module cost 0 units. They report actual EnsembleData
account-level unit spend per platform, which engine_pulse consumes to
cross-check the connector's internal `_global_units_spent` ledger against
the vendor's authoritative count.

Kept OUT of the EnsembleConnector class because these calls do not
produce raw_content rows. They feed the observability layer, not the
ingestion pipeline.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import requests

from src.utils.log_redactor import get_logger

ENSEMBLE_BASE_URL = "https://ensembledata.com/apis"
USED_UNITS_PATH = "/customer/get-used-units"
HISTORY_PATH = "/customer/get-history"
DEFAULT_TIMEOUT_SECONDS = 30

_logger = get_logger("connector.ensemble.customer")


def _safe_url(url: str) -> str:
    """Strip the querystring (including ``token``) before logging."""
    try:
        return urlparse(url)._replace(query="").geturl()
    except Exception:
        return url.split("?", 1)[0]


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Single GET that never bubbles. Returns parsed JSON dict or None."""
    safe = _safe_url(url)
    _logger.info("EnsembleData customer request: %s", safe)
    try:
        resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as exc:
        _logger.warning("EnsembleData customer network error for %s: %s", safe, str(exc)[:200])
        return None

    if resp.status_code >= 400:
        _logger.warning("EnsembleData customer HTTP %d for %s", resp.status_code, safe)
        return None

    try:
        payload = resp.json()
    except ValueError:
        _logger.warning("EnsembleData customer non-JSON response from %s", safe)
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def fetch_used_units(token: str, date: str) -> dict[str, int]:
    """Return per-platform unit spend for ``date`` (YYYY-MM-DD).

    Response shape per docs: ``{"data": {"tiktok": int, "instagram": int,
    "reddit": int, "youtube": int, "twitch": int}}``. Returns empty dict
    on any failure so engine_pulse can degrade gracefully.

    Cost: 0 units.
    """
    if not token or not date:
        return {}
    payload = _get_json(
        f"{ENSEMBLE_BASE_URL}{USED_UNITS_PATH}",
        params={"token": token, "date": date},
    )
    if not payload:
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in data.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def fetch_units_history(token: str, days: int) -> list[dict[str, Any]]:
    """Return ``days`` of historical per-day per-platform unit spend.

    Response shape per docs: ``{"data": [{"date": "YYYY-MM-DD", "tiktok":
    int, ...}, ...]}``. Returns empty list on any failure.

    Cost: 0 units.
    """
    if not token or days <= 0:
        return []
    payload = _get_json(
        f"{ENSEMBLE_BASE_URL}{HISTORY_PATH}",
        params={"token": token, "days": int(days)},
    )
    if not payload:
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, dict):
            out.append(dict(entry))
    return out
