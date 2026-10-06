"""Fail-closed Source Lab 2.1 read model for persisted staging rows."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from urllib.parse import urlsplit

from .investigation_scopes import ResolvedInvestigationScope

logger = logging.getLogger("listening_post.api.source_lab")

CONTRACT_VERSION = "2.1.0"
RESOURCE_VERSION = "source_lab_inventory_v1"
CONTRACT_VERSION_V2 = "2.2.0"
RESOURCE_VERSION_V2 = "source_lab_inventory_v2"
SUPPORTED_CONTRACT_VERSIONS = {CONTRACT_VERSION, CONTRACT_VERSION_V2}
EXPECTATION_VERSION = "socialcrawl_catalog_2026_09_07"
APPROVED_CATALOG_DIGEST = (
    "76358b388d4384cd548ed77e361c553a75035462b86e5b2bc9cd8676e89fd6a8"
)
EXPECTED_PLATFORM_COUNT = 64
EXPECTED_ROUTE_COUNT = 557
APPROVED_CATALOGS = MappingProxyType(
    {
        APPROVED_CATALOG_DIGEST: (
            EXPECTATION_VERSION,
            EXPECTED_PLATFORM_COUNT,
            EXPECTED_ROUTE_COUNT,
        ),
        "5c51442cf52165f78a3f11925e47fd88f95bc85fcfadbde6aab3052c892112aa": (
            "socialcrawl_catalog_2026_09_08",
            65,
            571,
        ),
    }
)
WAVE1_CHANNEL_BY_ROUTE = MappingProxyType(
    {
        "/v1/tiktok/song": "short_video",
        "/v1/tiktok/song/videos": "short_video",
        "/v1/instagram/music/trending": "short_video",
        "/v1/instagram/audio/reels": "short_video",
        "/v1/instagram/search/reels": "short_video",
        "/v1/youtube/shorts/trending": "youtube",
        "/v1/youtube/video/comments": "youtube",
        "/v1/reddit/post/comments": "reddit",
    }
)
MONTHLY_OPTIONAL_CREDIT_CAP = Decimal("25000")

# The six statuses the engine writes, settled by the task 75 ruling. A route
# the engine has not wired reads inventory_only; the other five say the engine
# has assessed the route, and a row carrying one of them may carry the
# measurements that assessment produced.
ROUTE_STATUSES = frozenset(
    {
        "active",
        "pilot",
        "available_unwired",
        "blocked",
        "permanently_rejected",
        "inventory_only",
    }
)
UNWIRED_ROUTE_STATUS = "inventory_only"

# The columns the engine fills once it has read a route. They stay null while
# the route is unwired, so the reader still refuses a measurement on a route
# the engine says it never called.
MEASURED_INVENTORY_FIELDS = (
    "calls",
    "credits",
    "rows",
    "integrity",
    "geo_precision",
    "unique_lift",
    "last_success_at",
    "blocking_reason",
    "kill_test_result",
    "review_date",
)

# The balance row's own columns. They are never written on a route row, whatever
# its status, so an inventory row carrying one is a writer regression.
FUNDING_ONLY_FIELDS = (
    "balance",
    "recent_deductions",
    "observed_at",
    "balance_read_status",
    "deductions_interval",
    "funding_math_status",
    "funded_increase_amount",
    "funded_increase_observed",
    "selected_top_up_credits",
    "baseline_credits_per_day",
    "baseline_window",
    "runway_days",
    "monthly_optional_credit_cap",
    "monthly_optional_credits_used",
    "optional_calls_enabled",
)

SOURCE_PERFORMANCE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "source_performance_id",
    "metric_date",
    "vendor",
    "http_method",
    "route_path",
    "endpoint_id",
    "route_role",
    "source_family",
    "market",
    "status",
    "calls",
    "credits",
    "rows",
    "integrity",
    "geo_precision",
    "unique_lift",
    "last_success_at",
    "downstream_consumers",
    "official_capability",
    "official_parameters",
    "official_price_components",
    "blocking_reason",
    "kill_test_result",
    "review_date",
    "last_checked_at",
    "balance",
    "observed_at",
    "balance_read_status",
    "recent_deductions",
    "deductions_interval",
    "funding_math_status",
    "funded_increase_amount",
    "funded_increase_observed",
    "selected_top_up_credits",
    "baseline_credits_per_day",
    "baseline_window",
    "runway_days",
    "monthly_optional_credit_cap",
    "monthly_optional_credits_used",
    "optional_calls_enabled",
    "platform",
    "resource",
    "catalog_digest",
    "official_credits",
    "official_credits_label",
    "official_archetype",
    "catalog_paginated",
    "catalog_metered",
    "cache_ttl_seconds",
    "delivery",
    "docs_url",
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ENDPOINT_ID = re.compile(r"^endpoint_[0-9a-f]{64}$")


class SourceLabUnavailable(ValueError):
    pass


class CatalogSnapshotInvalid(ValueError):
    pass


def _describe(value: object) -> str:
    """One printable value, capped so a refusal line stays a line."""
    text = repr(value)
    if len(text) <= 120:
        return text
    return f"<{type(value).__name__} chars={len(text)}>"


def _detail(values: dict[str, object]) -> str:
    return " ".join(f"{key}={_describe(value)}" for key, value in values.items())


def _invalid(guard: str, **values: object) -> None:
    """Refuse the snapshot, and say in the log which guard refused and on what.

    Only catalog columns and scope identifiers are ever passed here. No
    credential, token or passcode reaches this module, so none can reach a log
    line, and every value is capped by _describe before it is written.
    """
    logger.error(
        "source lab refused the catalog snapshot: guard=%s %s",
        guard,
        _detail(values),
    )
    raise CatalogSnapshotInvalid("catalog_snapshot_invalid")


def _unavailable(guard: str, **values: object) -> None:
    """Refuse to answer at all, and say which guard refused."""
    logger.error(
        "source lab is unavailable: guard=%s %s",
        guard,
        _detail(values),
    )
    raise SourceLabUnavailable("source_lab_unavailable")


def _utc(value: object, *, field: str = "timestamp", route: str = "") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _invalid(
            "timestamp.not_an_aware_datetime",
            field=field,
            route=route,
            value=value,
            value_type=type(value).__name__,
        )
    normalized = value.astimezone(UTC)
    if value.utcoffset() != normalized.utcoffset():
        _invalid(
            "timestamp.offset_is_not_utc",
            field=field,
            route=route,
            value=value,
            utc_offset=value.utcoffset(),
        )
    return normalized


def _iso_z(value: object, *, field: str = "timestamp", route: str = "") -> str:
    return _utc(value, field=field, route=route).isoformat().replace("+00:00", "Z")


def _decimal(value: object, *, field: str = "amount", route: str = "") -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        _invalid(
            "amount.not_a_non_negative_decimal",
            field=field,
            route=route,
            value=value,
            value_type=type(value).__name__,
        )
    return value


def json_ready(value: object) -> object:
    """Convert native NUMERIC values to JSON numbers without string coercion."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def _identifier_part(value: str) -> str:
    return f"V{len(value.encode())}:{value}"


def _expected_source_family(route_path: object) -> str:
    if not isinstance(route_path, str):
        return "socialcrawl"
    return WAVE1_CHANNEL_BY_ROUTE.get(route_path, "socialcrawl")


def _endpoint_id(method: str, route_path: str) -> str:
    encoded = _identifier_part(method) + _identifier_part(route_path)
    return "endpoint_" + hashlib.sha256(encoded.encode()).hexdigest()


def _route_label(row: dict[str, object]) -> str:
    return f"{row.get('http_method')} {row.get('route_path')}"


def fixed_scope() -> ResolvedInvestigationScope:
    return ResolvedInvestigationScope(
        client_scope_id="ogilvy_default",
        market_scope=("za", "ng", "ke"),
        brand_config_id="ogilvy_42",
        audience_lens_ids=(),
        theme_id="ogilvy_intelligence",
    )


def _source_performance_id(row: dict[str, object]) -> str:
    route = _route_label(row)
    metric_date = row.get("metric_date")
    method = row.get("http_method")
    route_path = row.get("route_path")
    market = row.get("market")
    if not isinstance(metric_date, date):
        _invalid(
            "identity.metric_date_type",
            route=route,
            value=metric_date,
            value_type=type(metric_date).__name__,
        )
    if not isinstance(method, str):
        _invalid(
            "identity.http_method_type",
            route=route,
            value=method,
            value_type=type(method).__name__,
        )
    if not isinstance(route_path, str):
        _invalid(
            "identity.route_path_type",
            route=route,
            value=route_path,
            value_type=type(route_path).__name__,
        )
    if market is not None and not isinstance(market, str):
        _invalid(
            "identity.market_type",
            route=route,
            value=market,
            value_type=type(market).__name__,
        )
    parts = (
        ("client_scope_id", row.get("client_scope_id")),
        ("metric_date", metric_date.isoformat()),
        ("vendor", row.get("vendor")),
        ("http_method", method),
        ("route_path", route_path),
        ("market", market or "global"),
    )
    for name, value in parts:
        if not isinstance(value, str) or not value:
            _invalid(
                "identity.empty_component",
                route=route,
                component=name,
                value=value,
                value_type=type(value).__name__,
            )
    encoded = "".join(_identifier_part(value) for _name, value in parts)
    return "srcperf_" + hashlib.sha256(encoded.encode()).hexdigest()


def _scope_mismatch(
    row: dict[str, object], scope: ResolvedInvestigationScope
) -> str | None:
    """The first scope field on this row that is not the fixed scope's."""
    audience_lens_ids = row.get("audience_lens_ids")
    if row.get("contract_version") != CONTRACT_VERSION:
        return "contract_version"
    if row.get("client_scope_id") != scope.client_scope_id:
        return "client_scope_id"
    if tuple(row.get("market_scope") or ()) != scope.market_scope:
        return "market_scope"
    if row.get("brand_config_id") != scope.brand_config_id:
        return "brand_config_id"
    if audience_lens_ids not in ([], ()):
        return "audience_lens_ids"
    if tuple(audience_lens_ids) != scope.audience_lens_ids or scope.audience_lens_ids:
        return "audience_lens_ids"
    if row.get("theme_id") != scope.theme_id:
        return "theme_id"
    return None


def _same_scope(row: dict[str, object], scope: ResolvedInvestigationScope) -> bool:
    return _scope_mismatch(row, scope) is None


def _is_real(value: object) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _optional_count(row: dict[str, object], field: str, route: str) -> int | None:
    value = row.get(field)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        _invalid(
            "inventory.count_not_a_non_negative_integer",
            route=route,
            field=field,
            value=value,
            value_type=type(value).__name__,
        )
    return value


def _optional_real(
    row: dict[str, object],
    field: str,
    route: str,
    *,
    maximum: float | None = None,
) -> object:
    value = row.get(field)
    if value is None:
        return None
    if not _is_real(value) or value < 0 or (maximum is not None and value > maximum):
        _invalid(
            "inventory.measure_out_of_range",
            route=route,
            field=field,
            value=value,
            value_type=type(value).__name__,
            maximum=maximum,
        )
    return value


def _optional_text(row: dict[str, object], field: str, route: str) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        _invalid(
            "inventory.text_not_a_non_empty_string",
            route=route,
            field=field,
            value=value,
            value_type=type(value).__name__,
        )
    return value


def _downstream_consumers(row: dict[str, object], route: str) -> list[str]:
    value = row.get("downstream_consumers")
    if value in ([], ()):
        return []
    if not isinstance(value, (list, tuple)):
        _invalid(
            "inventory.downstream_consumers_type",
            route=route,
            value=value,
            value_type=type(value).__name__,
        )
    if any(not isinstance(item, str) or not item for item in value):
        _invalid(
            "inventory.downstream_consumer_not_a_non_empty_string",
            route=route,
            value=list(value),
        )
    if len(set(value)) != len(value):
        _invalid(
            "inventory.downstream_consumers_repeat",
            route=route,
            value=list(value),
        )
    return list(value)


def _measurements(row: dict[str, object], route: str) -> dict[str, object]:
    """Validate the engine's assessment of one route and project it unchanged.

    An unwired route carries none of it. A route the engine has wired carries
    whatever it measured and nothing it did not, and the four reading states
    below say which of those two it is rather than asserting one of them.
    """
    status = row.get("status")
    if status not in ROUTE_STATUSES:
        _invalid(
            "inventory.route_status_unknown",
            route=route,
            status=status,
            settled=sorted(ROUTE_STATUSES),
        )
    if status == UNWIRED_ROUTE_STATUS:
        for field in MEASURED_INVENTORY_FIELDS:
            if row.get(field) is not None:
                _invalid(
                    "inventory.unwired_route_carries_a_measurement",
                    route=route,
                    field=field,
                    value=row.get(field),
                    status=status,
                )
        if row.get("downstream_consumers") not in ([], ()):
            _invalid(
                "inventory.unwired_route_carries_a_consumer",
                route=route,
                value=row.get("downstream_consumers"),
                status=status,
            )
    calls = _optional_count(row, "calls", route)
    read_rows = _optional_count(row, "rows", route)
    credits = row.get("credits")
    if credits is not None:
        credits = _decimal(credits, field="credits", route=route)
    integrity = _optional_real(row, "integrity", route, maximum=1)
    geo_precision = _optional_real(row, "geo_precision", route)
    unique_lift = _optional_real(row, "unique_lift", route)
    last_success_at = row.get("last_success_at")
    if last_success_at is not None:
        last_success_at = _iso_z(last_success_at, field="last_success_at", route=route)
    blocking_reason = _optional_text(row, "blocking_reason", route)
    kill_test_result = _optional_text(row, "kill_test_result", route)
    review_date = row.get("review_date")
    if review_date is not None:
        if not isinstance(review_date, date) or isinstance(review_date, datetime):
            _invalid(
                "inventory.review_date_type",
                route=route,
                value=review_date,
                value_type=type(review_date).__name__,
            )
        review_date = review_date.isoformat()
    if kill_test_result is None:
        kill_test_state = "not_run"
    elif kill_test_result == "passed":
        kill_test_state = "passed"
    else:
        kill_test_state = "failed"
    return {
        "status": status,
        "calls": calls,
        "credits": credits,
        "rows": read_rows,
        "integrity": integrity,
        "geo_precision": geo_precision,
        "unique_lift": unique_lift,
        "last_success_at": last_success_at,
        "downstream_consumers": _downstream_consumers(row, route),
        "blocking_reason": blocking_reason,
        "kill_test_result": kill_test_result,
        "review_date": review_date,
        "quality_state": "unmeasured" if integrity is None else "measured",
        "cost_state": "catalog_only"
        if calls is None and credits is None
        else "measured",
        "yield_state": "unmeasured" if read_rows is None else "measured",
        "kill_test_state": kill_test_state,
    }


def _inventory_source(
    row: dict[str, object], checked_at: datetime
) -> dict[str, object]:
    route = _route_label(row)
    method = row.get("http_method")
    route_path = row.get("route_path")
    endpoint_id = row.get("endpoint_id")
    platform = row.get("platform")
    resource = row.get("resource")
    if not isinstance(method, str) or method != method.upper():
        _invalid(
            "inventory.http_method",
            route=route,
            value=method,
            value_type=type(method).__name__,
        )
    if not isinstance(route_path, str) or not route_path.startswith("/v1/"):
        _invalid(
            "inventory.route_path",
            route=route,
            value=route_path,
            value_type=type(route_path).__name__,
        )
    if not isinstance(endpoint_id, str) or _ENDPOINT_ID.fullmatch(endpoint_id) is None:
        _invalid(
            "inventory.endpoint_id_shape",
            route=route,
            value=endpoint_id,
            value_type=type(endpoint_id).__name__,
        )
    if endpoint_id != _endpoint_id(method, route_path):
        _invalid(
            "inventory.endpoint_id_mismatch",
            route=route,
            stored=endpoint_id,
            recomputed=_endpoint_id(method, route_path),
        )
    if row.get("source_performance_id") != _source_performance_id(row):
        _invalid(
            "inventory.source_performance_id_mismatch",
            route=route,
            stored=row.get("source_performance_id"),
            recomputed=_source_performance_id(row),
        )
    if not isinstance(platform, str) or not platform:
        _invalid(
            "inventory.platform",
            route=route,
            value=platform,
            value_type=type(platform).__name__,
        )
    if not isinstance(resource, str) or not resource:
        _invalid(
            "inventory.resource",
            route=route,
            value=resource,
            value_type=type(resource).__name__,
        )
    if route_path.split("/")[2] != platform:
        _invalid(
            "inventory.route_path_platform_mismatch",
            route=route,
            route_path_segment=route_path.split("/")[2],
            platform=platform,
        )
    if row.get("vendor") != "socialcrawl":
        _invalid("inventory.vendor", route=route, value=row.get("vendor"))
    if row.get("route_role") != "inventory":
        _invalid("inventory.route_role", route=route, value=row.get("route_role"))
    if row.get("source_family") != _expected_source_family(route_path):
        _invalid(
            "inventory.source_family",
            route=route,
            value=row.get("source_family"),
            expected=_expected_source_family(route_path),
        )
    if row.get("market") is not None:
        _invalid("inventory.market", route=route, value=row.get("market"))
    if _utc(row.get("last_checked_at"), field="last_checked_at", route=route) != (
        checked_at
    ):
        _invalid(
            "inventory.last_checked_at",
            route=route,
            value=row.get("last_checked_at"),
            snapshot=checked_at,
        )
    for field in FUNDING_ONLY_FIELDS:
        if row.get(field) is not None:
            _invalid(
                "inventory.funding_column_on_a_route",
                route=route,
                field=field,
                value=row.get(field),
            )
    measurements = _measurements(row, route)
    parameters = row.get("official_parameters")
    credits_label = row.get("official_credits_label")
    archetype = row.get("official_archetype")
    delivery = row.get("delivery")
    docs_url = row.get("docs_url")
    if (
        not isinstance(row.get("official_capability"), str)
        or not row["official_capability"]
    ):
        _invalid(
            "inventory.official_capability",
            route=route,
            value=row.get("official_capability"),
        )
    if not isinstance(parameters, (list, tuple)) or any(
        not isinstance(value, str) or not value for value in parameters
    ):
        _invalid("inventory.official_parameters", route=route, value=parameters)
    if len(parameters) != len(set(parameters)):
        _invalid("inventory.official_parameters_repeat", route=route, value=parameters)
    if row.get("official_price_components") not in ([], ()):
        _invalid(
            "inventory.official_price_components",
            route=route,
            value=row.get("official_price_components"),
        )
    if not isinstance(credits_label, str) or not credits_label:
        _invalid("inventory.official_credits_label", route=route, value=credits_label)
    if not isinstance(archetype, str) or not archetype:
        _invalid("inventory.official_archetype", route=route, value=archetype)
    if delivery is not None and not isinstance(delivery, str):
        _invalid(
            "inventory.delivery",
            route=route,
            value=delivery,
            value_type=type(delivery).__name__,
        )
    if type(row.get("catalog_paginated")) is not bool:
        _invalid(
            "inventory.catalog_paginated",
            route=route,
            value=row.get("catalog_paginated"),
        )
    if type(row.get("catalog_metered")) is not bool:
        _invalid(
            "inventory.catalog_metered", route=route, value=row.get("catalog_metered")
        )
    if (
        not isinstance(row.get("cache_ttl_seconds"), int)
        or row["cache_ttl_seconds"] < 0
    ):
        _invalid(
            "inventory.cache_ttl_seconds",
            route=route,
            value=row.get("cache_ttl_seconds"),
        )
    parsed_docs = urlsplit(docs_url) if isinstance(docs_url, str) else None
    if parsed_docs is None or parsed_docs.scheme != "https":
        _invalid("inventory.docs_url_scheme", route=route, value=docs_url)
    if parsed_docs.hostname not in {"socialcrawl.dev", "www.socialcrawl.dev"}:
        _invalid("inventory.docs_url_host", route=route, value=parsed_docs.hostname)
    official_credits = _decimal(
        row.get("official_credits"), field="official_credits", route=route
    )
    return {
        "endpoint_id": endpoint_id,
        "vendor": "socialcrawl",
        "platform": platform,
        "resource": resource,
        "http_method": method,
        "route_path": route_path,
        "route_role": "inventory",
        "source_family": _expected_source_family(route_path),
        "market": None,
        "status": measurements["status"],
        "official_capability": row["official_capability"],
        "official_parameters": list(parameters),
        "official_credits": official_credits,
        "official_credits_label": credits_label,
        "official_archetype": archetype,
        "catalog_paginated": row["catalog_paginated"],
        "catalog_metered": row["catalog_metered"],
        "cache_ttl_seconds": row["cache_ttl_seconds"],
        "delivery": delivery,
        "docs_url": docs_url,
        "calls": measurements["calls"],
        "credits": measurements["credits"],
        "rows": measurements["rows"],
        "integrity": measurements["integrity"],
        "geo_precision": measurements["geo_precision"],
        "unique_lift": measurements["unique_lift"],
        "last_success_at": measurements["last_success_at"],
        "downstream_consumers": measurements["downstream_consumers"],
        "blocking_reason": measurements["blocking_reason"],
        "kill_test_result": measurements["kill_test_result"],
        "review_date": measurements["review_date"],
        "quality_state": measurements["quality_state"],
        "cost_state": measurements["cost_state"],
        "yield_state": measurements["yield_state"],
        "kill_test_state": measurements["kill_test_state"],
        "last_checked_at": _iso_z(checked_at, field="last_checked_at", route=route),
    }


def _funding(
    row: dict[str, object],
    *,
    snapshot_at: datetime,
) -> dict[str, object]:
    route = _route_label(row)
    literals = (
        ("vendor", row.get("vendor"), "socialcrawl"),
        ("route_role", row.get("route_role"), "utility_balance"),
        (
            "source_family",
            row.get("source_family"),
            _expected_source_family(row.get("route_path")),
        ),
        ("market", row.get("market"), None),
        ("status", row.get("status"), "inventory_only"),
        ("http_method", row.get("http_method"), "GET"),
        ("route_path", row.get("route_path"), "/v1/credits/balance"),
        (
            "endpoint_id",
            row.get("endpoint_id"),
            _endpoint_id("GET", "/v1/credits/balance"),
        ),
        (
            "source_performance_id",
            row.get("source_performance_id"),
            _source_performance_id(row),
        ),
        ("balance_read_status", row.get("balance_read_status"), "ok"),
        ("funding_math_status", row.get("funding_math_status"), "unknown"),
        (
            "monthly_optional_credit_cap",
            row.get("monthly_optional_credit_cap"),
            MONTHLY_OPTIONAL_CREDIT_CAP,
        ),
        ("selected_top_up_credits", row.get("selected_top_up_credits"), None),
        (
            "monthly_optional_credits_used",
            row.get("monthly_optional_credits_used"),
            None,
        ),
    )
    for field, value, expected in literals:
        if value != expected:
            _invalid(
                "balance.field_is_not_the_contract_value",
                route=route,
                field=field,
                value=value,
                expected=expected,
            )
    if row.get("funded_increase_observed") is not False:
        _invalid(
            "balance.funded_increase_observed",
            route=route,
            value=row.get("funded_increase_observed"),
        )
    if row.get("optional_calls_enabled") is not False:
        _invalid(
            "balance.optional_calls_enabled",
            route=route,
            value=row.get("optional_calls_enabled"),
        )
    if _utc(row.get("last_checked_at"), field="last_checked_at", route=route) != (
        snapshot_at
    ):
        _invalid(
            "balance.last_checked_at",
            route=route,
            value=row.get("last_checked_at"),
            snapshot=snapshot_at,
        )
    if _utc(row.get("observed_at"), field="observed_at", route=route) != snapshot_at:
        _invalid(
            "balance.observed_at",
            route=route,
            value=row.get("observed_at"),
            snapshot=snapshot_at,
        )
    for field in (
        "platform",
        "resource",
        "catalog_digest",
        "official_credits",
        "official_credits_label",
        "official_archetype",
        "catalog_paginated",
        "catalog_metered",
        "cache_ttl_seconds",
        "delivery",
        "docs_url",
        "calls",
        "credits",
        "rows",
        "integrity",
        "geo_precision",
        "unique_lift",
        "last_success_at",
        "blocking_reason",
        "kill_test_result",
        "review_date",
        "funded_increase_amount",
        "baseline_credits_per_day",
        "baseline_window",
        "runway_days",
    ):
        if row.get(field) is not None:
            _invalid(
                "balance.expected_null_column",
                route=route,
                field=field,
                value=row.get(field),
            )
    for field in (
        "official_parameters",
        "official_price_components",
        "downstream_consumers",
    ):
        if row.get(field) not in ([], ()):
            _invalid(
                "balance.expected_empty_list",
                route=route,
                field=field,
                value=row.get(field),
            )
    if (
        not isinstance(row.get("official_capability"), str)
        or not row["official_capability"]
    ):
        _invalid(
            "balance.official_capability",
            route=route,
            value=row.get("official_capability"),
        )
    balance = _decimal(row.get("balance"), field="balance", route=route)
    deductions = _decimal(
        row.get("recent_deductions"), field="recent_deductions", route=route
    )
    observed_at = _iso_z(row.get("observed_at"), field="observed_at", route=route)
    return {
        "balance": balance,
        "recent_deductions": deductions,
        "observed_at": observed_at,
        "funding_math_status": "unknown",
        "funded_increase_observed": False,
        "selected_top_up_credits": None,
        "monthly_optional_credit_cap": MONTHLY_OPTIONAL_CREDIT_CAP,
        "monthly_optional_credits_used": None,
        "optional_calls_enabled": False,
    }


def build_response(
    *,
    scope: ResolvedInvestigationScope,
    rows: list[dict[str, object]],
    current_metric_date: object,
) -> dict[str, object]:
    if not rows:
        _unavailable(
            "response.no_rows",
            client_scope_id=getattr(scope, "client_scope_id", None),
            current_metric_date=current_metric_date,
        )
    if not isinstance(scope, ResolvedInvestigationScope) or scope != fixed_scope():
        _invalid("response.scope", scope=scope, expected=fixed_scope())
    candidate_digests = {
        row.get("catalog_digest")
        for row in rows
        if isinstance(row, dict) and row.get("route_role") == "inventory"
    }
    if len(candidate_digests) != 1:
        _invalid("response.catalog_digest_cardinality", counted=len(candidate_digests))
    if next(iter(candidate_digests)) not in APPROVED_CATALOGS:
        _invalid(
            "response.catalog_digest_not_approved",
            stored=sorted(str(value) for value in candidate_digests),
        )
    expectation_version, platform_count, route_count = APPROVED_CATALOGS[
        next(iter(candidate_digests))
    ]
    if len(rows) != route_count + 1:
        _invalid(
            "response.row_count",
            counted=len(rows),
            expected=route_count + 1,
        )
    expected_fields = set(SOURCE_PERFORMANCE_FIELDS)
    for row in rows:
        if not isinstance(row, dict):
            _invalid("response.row_type", value_type=type(row).__name__)
        if set(row) != expected_fields:
            _invalid(
                "response.row_field_set",
                route=_route_label(row),
                missing=sorted(expected_fields - set(row)),
                unexpected=sorted(set(row) - expected_fields),
            )
        mismatch = _scope_mismatch(row, scope)
        if mismatch is not None:
            _invalid(
                "response.row_scope",
                route=_route_label(row),
                field=mismatch,
                value=row.get(mismatch),
                expected=getattr(scope, mismatch, CONTRACT_VERSION),
            )
    run_ids = {row.get("run_id") for row in rows}
    metric_dates = {row.get("metric_date") for row in rows}
    if not isinstance(current_metric_date, date):
        _invalid(
            "response.current_metric_date_type",
            value=current_metric_date,
            value_type=type(current_metric_date).__name__,
        )
    if len(metric_dates) != 1 or not all(
        type(value) is date and value <= current_metric_date for value in metric_dates
    ):
        _invalid(
            "response.metric_date",
            metric_dates=sorted(str(value) for value in metric_dates),
            expected=current_metric_date,
        )
    snapshot_date = next(iter(metric_dates))
    expected_run_id = f"source_lab_{snapshot_date.isoformat()}"
    if run_ids != {expected_run_id}:
        _invalid(
            "response.run_identity",
            run_ids=sorted(str(value) for value in run_ids),
            expected=expected_run_id,
        )
    inventory_rows = [row for row in rows if row.get("route_role") == "inventory"]
    balance_rows = [row for row in rows if row.get("route_role") == "utility_balance"]
    if len(inventory_rows) != route_count or len(balance_rows) != 1:
        _invalid(
            "response.route_role_split",
            inventory=len(inventory_rows),
            utility_balance=len(balance_rows),
            expected_inventory=route_count,
        )
    checked_values = {
        _utc(row.get("last_checked_at"), field="last_checked_at")
        for row in inventory_rows
    }
    digests = {row.get("catalog_digest") for row in inventory_rows}
    if len(checked_values) != 1:
        _invalid(
            "response.snapshot_cardinality",
            counted=len(checked_values),
            values=sorted(value.isoformat() for value in checked_values),
        )
    if len(digests) != 1:
        _invalid(
            "response.catalog_digest_cardinality",
            counted=len(digests),
            values=sorted(str(value) for value in digests),
        )
    checked_at = next(iter(checked_values))
    catalog_digest = next(iter(digests))
    if checked_at.date() != snapshot_date:
        _invalid(
            "response.snapshot_date",
            checked_at=checked_at,
            current_metric_date=current_metric_date,
        )
    if not isinstance(catalog_digest, str) or _DIGEST.fullmatch(catalog_digest) is None:
        _invalid("response.catalog_digest_shape", value=catalog_digest)
    if catalog_digest not in APPROVED_CATALOGS:
        _invalid(
            "response.catalog_digest_not_approved",
            stored=catalog_digest,
            approved=APPROVED_CATALOG_DIGEST,
        )
    sources = [_inventory_source(row, checked_at) for row in inventory_rows]
    sources.sort(key=lambda row: (row["http_method"], row["route_path"]))
    routes = [f"{row['http_method']} {row['route_path']}" for row in sources]
    endpoint_ids = [row["endpoint_id"] for row in sources]
    platforms = {row["platform"] for row in sources}
    calculated_digest = hashlib.sha256(
        "".join(f"{route}\n" for route in routes).encode()
    ).hexdigest()
    if len(routes) != len(set(routes)):
        _invalid(
            "response.route_repeat", counted=len(routes), distinct=len(set(routes))
        )
    if len(endpoint_ids) != len(set(endpoint_ids)):
        _invalid(
            "response.endpoint_id_repeat",
            counted=len(endpoint_ids),
            distinct=len(set(endpoint_ids)),
        )
    if len(platforms) != platform_count:
        _invalid(
            "response.platform_count",
            counted=len(platforms),
            expected=platform_count,
        )
    if calculated_digest != catalog_digest:
        _invalid(
            "response.catalog_digest_recompute",
            recomputed=calculated_digest,
            stored=catalog_digest,
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "resource_version": RESOURCE_VERSION,
        "client_scope_id": scope.client_scope_id,
        "market_scope": list(scope.market_scope),
        "brand_config_id": scope.brand_config_id,
        "audience_lens_ids": [],
        "theme_id": scope.theme_id,
        "run_id": next(iter(run_ids)),
        "catalog": {
            "expectation_version": expectation_version,
            "platform_count": platform_count,
            "route_count": route_count,
            "catalog_digest": catalog_digest,
            "checked_at": _iso_z(checked_at, field="last_checked_at"),
            "completeness_state": "verified_snapshot",
        },
        "funding": _funding(
            balance_rows[0],
            snapshot_at=checked_at,
        ),
        "sources": sources,
    }


def build_versioned_response(
    payload: dict[str, object],
    *,
    contract_version: str,
    credit_budget_reading: dict[str, object] | None = None,
) -> dict[str, object]:
    if contract_version == CONTRACT_VERSION:
        return payload
    if contract_version != CONTRACT_VERSION_V2:
        _unavailable("versioned.contract_version", contract_version=contract_version)
    if credit_budget_reading is None:
        _unavailable(
            "versioned.credit_budget_missing", contract_version=contract_version
        )
    versioned = dict(payload)
    versioned["contract_version"] = CONTRACT_VERSION_V2
    versioned["resource_version"] = RESOURCE_VERSION_V2
    versioned["credit_budget"] = credit_budget_reading
    return versioned
