"""Pure canonical normalization for zero-credit Source Lab utility receipts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from urllib.parse import urlsplit

from src.contracts.open_intelligence import (
    CONTRACT_VERSION,
    MONTHLY_OPTIONAL_CREDIT_CAP,
    ResolvedScope,
    encode_identifier_part,
)

SOURCE_LAB_CONTRACT_VERSION = "2.1.0"
SOURCE_LAB_RESOURCE_VERSION = "source_lab_inventory_v1"
CATALOG_EXPECTATIONS = MappingProxyType(
    {
        "socialcrawl_catalog_2026_09_27": (
            67,
            630,
            32,
            "6d567fb218e3ead629e79258e6551c9e81c24897c87b8026db1661def75bbe84",
            "f530f3b6da26b0d88a576bff8c895db193c14f17d901ebdb65786cc8bd301b74",
        ),
        # Vendor catalog read on 5 September 2026 at zero credit through the funded
        # key, after the snapshot refused the day's read as drift from the 4 September
        # whole-catalog digests. Counts unchanged; the eight Wave 1 routes unchanged,
        # so the funded lane keeps its 4 September route identity.
        "socialcrawl_catalog_2026_09_05": (
            64,
            556,
            30,
            "2424a5760fb55b44b78971875b64a6c176fcd34d95de83b2ad158a3b866c0a34",
            "c77c64754e2a264bc8cf834e1105116a9ff7186117070ceaca7fcc3e50f857e5",
        ),
        # Vendor catalog read on 4 September 2026 at zero credit through the funded
        # key, approved for the source lab and the funded lane together.
        "socialcrawl_catalog_2026_09_04": (
            64,
            556,
            30,
            "08cf5728a5f52d884af994e4f038cf3636ab7e1d069ee3a723e04e243ce9f127",
            "ad2b500dd483c720f38b095d4549b7aaba9e75e9d36df43efab0f1f6a7432a4e",
        ),
        "socialcrawl_catalog_2026_09_03": (
            61,
            523,
            29,
            "1d123c826194e09243e5e60ec38e2ae63feb29a84cbd909ca2b7df8a5de7dc59",
            "3848a22d625fb72768b6871ed4dc3792b9c38b647be294047aefcaab95713769",
        ),
        "socialcrawl_catalog_2026_09_02": (
            55,
            449,
            29,
            "6a9997af380363ee10a5649ffa8f20055edffa6c60c54f45b60d6d352ff2e5e0",
            "83ca6a01f8b1c0d3234b96ffb211ceba810658a525df79401aff7e67fec566c1",
        ),
        "socialcrawl_catalog_2026_08_27": (
            50,
            400,
            28,
            "f843d6ad934e36b005090c1c642eb4b8e920bf2ca5d8828d7b9c1e7e4f143daa",
            "47290bc1da76f4c29226a70c22f25b239557bbe56ce27d9931a29d174ffdd7ca",
        ),
        "socialcrawl_catalog_2026_08_26": (
            50,
            400,
            28,
            "f843d6ad934e36b005090c1c642eb4b8e920bf2ca5d8828d7b9c1e7e4f143daa",
            "9432a98874dfeafa020211c9aee11d81cbb0270620a8e4988af89033c4bd61a0",
        ),
    }
)
CATALOG_NORMALIZED_ROUTE_COUNTS = MappingProxyType(
    {"socialcrawl_catalog_2026_09_27": 610}
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
    "vendor_family",
    "channel_family",
    "funded_lane",
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
)

_METHODS = frozenset({"GET", "POST", "PATCH", "DELETE"})
_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_{}-]{0,99}$")
_PLATFORM = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_WAVE1_CHANNEL_BY_ROUTE = MappingProxyType(
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


def _source_identity(route_path: str, source_family: str) -> tuple[str, str, str | None]:
    channel = _WAVE1_CHANNEL_BY_ROUTE.get(route_path)
    if channel is None:
        return source_family, source_family, None
    return "socialcrawl", channel, "stage_1_wave_1"


class CatalogInvalid(ValueError):
    pass


class SourceLabInventoryInvalid(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Wave1SourceValueResult:
    state: str
    reason: str | None
    human_override_allowed: bool = False
    unique_observations: int = 0
    marginal_candidates: int = 0
    marginal_evidence: int = 0


def evaluate_wave1_source_value(
    *, unique_observations: int, marginal_candidates: int, marginal_evidence: int
) -> Wave1SourceValueResult:
    for value in (unique_observations, marginal_candidates, marginal_evidence):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("source value counts must be nonnegative integers")
    if unique_observations == 0:
        return Wave1SourceValueResult(
            "permanently_rejected",
            "zero_unique_observations",
            False,
            unique_observations,
            marginal_candidates,
            marginal_evidence,
        )
    if marginal_candidates + marginal_evidence == 0:
        return Wave1SourceValueResult(
            "permanently_rejected",
            "zero_marginal_downstream_rows",
            False,
            unique_observations,
            marginal_candidates,
            marginal_evidence,
        )
    return Wave1SourceValueResult(
        "passed",
        None,
        False,
        unique_observations,
        marginal_candidates,
        marginal_evidence,
    )


def resolve_wave1_activation(result: Wave1SourceValueResult) -> str:
    if not isinstance(result, Wave1SourceValueResult):
        raise ValueError("source value result is required")
    if result.state == "passed" and result.reason is None:
        return "active"
    if result.state == "permanently_rejected" and result.reason in {
        "zero_unique_observations",
        "zero_marginal_downstream_rows",
    }:
        return "permanently_rejected"
    raise ValueError("source value result cannot activate Wave 1")


@dataclass(frozen=True, slots=True)
class EndpointCapability:
    endpoint_id: str
    canonical_route: str
    http_method: str
    route_path: str
    platform: str
    resource: str
    official_capability: str
    official_parameters: tuple[str, ...]
    credits: Decimal
    credits_label: str
    archetype: str
    paginated: bool
    metered: bool
    cache_ttl_seconds: int
    delivery: str | None
    docs_url: str


@dataclass(frozen=True, slots=True)
class CatalogRowDrop:
    row_number: int
    route: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    platform_count: int
    route_count: int
    social_platform_count: int
    catalog_digest: str
    normalized_content_digest: str
    endpoints: tuple[EndpointCapability, ...]
    checked_at: datetime
    request_id: str
    credits_used: Decimal
    credits_remaining: Decimal | None
    dropped_rows: tuple[CatalogRowDrop, ...] = ()


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    balance: Decimal
    recent_deductions: Decimal
    observed_at: datetime
    request_id: str
    funding_math_status: str = "unknown"
    funded_increase_observed: bool = False
    monthly_optional_credit_cap: int = MONTHLY_OPTIONAL_CREDIT_CAP
    optional_calls_enabled: bool = False


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CatalogInvalid(f"{field} must be an object")
    return value


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CatalogInvalid(f"{field} is invalid")
    return value


def _optional_decimal(value: object, field: str) -> Decimal | None:
    # Zero-credit utility routes may answer credits_remaining as null; the balance route is
    # the authority for the balance itself.
    if value is None:
        return None
    return _decimal(value, field)


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise CatalogInvalid(f"{field} is invalid")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise CatalogInvalid(f"{field} is invalid") from error
    if not result.is_finite() or result < 0:
        raise CatalogInvalid(f"{field} is invalid")
    return result


def _text(value: object, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise CatalogInvalid(f"{field} is invalid")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or "<" in result
        or ">" in result
        or any(ord(character) < 32 for character in result)
    ):
        raise CatalogInvalid(f"{field} is invalid")
    return result


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CatalogInvalid(f"{field} must be UTC")
    return value


def _route_path(value: object) -> str:
    raw = _text(value, "route path", maximum=300)
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise CatalogInvalid("route path is invalid")
    parts = tuple(part for part in parsed.path.split("/") if part)
    path = "/" + "/".join(parts)
    if len(parts) < 3 or parts[0] != "v1":
        raise CatalogInvalid("route path is invalid")
    return path


def _parameters(row: Mapping[str, object]) -> tuple[str, ...]:
    values = []
    for field in ("required_params", "optional_params"):
        raw = row.get(field)
        if not isinstance(raw, list):
            raise CatalogInvalid(f"{field} is invalid")
        values.extend(raw)
    one_of = row.get("one_of")
    if not isinstance(one_of, list) or any(not isinstance(group, list) for group in one_of):
        raise CatalogInvalid("one_of is invalid")
    for group in one_of:
        values.extend(group)
    if any(not isinstance(value, str) or _TOKEN.fullmatch(value) is None for value in values):
        raise CatalogInvalid("official parameter is invalid")
    return tuple(sorted(set(values)))


def _endpoint_id(method: str, path: str) -> str:
    payload = encode_identifier_part(method) + encode_identifier_part(path)
    return "endpoint_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_performance_id(
    client_scope_id: str,
    metric_date: date,
    vendor: str,
    http_method: str,
    route_path: str,
    market: str | None,
) -> str:
    payload = "".join(
        encode_identifier_part(value)
        for value in (
            client_scope_id,
            metric_date.isoformat(),
            vendor,
            http_method,
            route_path,
            market or "global",
        )
    )
    return "srcperf_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _endpoint(row: object, *, wave1_route: bool = False) -> EndpointCapability:
    source = _mapping(row, "endpoint")
    method = _text(source.get("method"), "HTTP method", maximum=10).upper()
    if method not in _METHODS:
        raise CatalogInvalid("HTTP method is invalid")
    path = _route_path(source.get("path"))
    platform = _text(source.get("platform"), "platform", maximum=64).lower()
    if _PLATFORM.fullmatch(platform) is None or path.split("/")[2] != platform:
        raise CatalogInvalid("platform is invalid")
    resource = _text(source.get("resource"), "resource", maximum=200)
    capability = _text(source.get("summary"), "official capability")
    raw_credits_label = source.get("credits_label")
    if isinstance(raw_credits_label, str) and any(
        ord(character) < 32 or 127 <= ord(character) <= 159
        for character in raw_credits_label
    ):
        raise CatalogInvalid("credits label is invalid")
    credits_label = _text(
        raw_credits_label, "credits label", maximum=10_000 if wave1_route else 500
    )
    archetype = _text(source.get("archetype"), "archetype", maximum=100)
    docs_url = _text(source.get("docs_url"), "docs URL", maximum=500)
    parsed_docs = urlsplit(docs_url)
    if parsed_docs.scheme != "https" or parsed_docs.hostname not in {
        "socialcrawl.dev",
        "www.socialcrawl.dev",
    }:
        raise CatalogInvalid("docs URL is invalid")
    paginated = source.get("paginated")
    metered = source.get("metered")
    if type(paginated) is not bool or type(metered) is not bool:
        raise CatalogInvalid("endpoint flags are invalid")
    delivery = source.get("delivery")
    if delivery is not None:
        delivery = _text(delivery, "delivery", maximum=100)
    canonical_route = f"{method} {path}"
    return EndpointCapability(
        endpoint_id=_endpoint_id(method, path),
        canonical_route=canonical_route,
        http_method=method,
        route_path=path,
        platform=platform,
        resource=resource,
        official_capability=capability,
        official_parameters=_parameters(source),
        credits=_decimal(source.get("credits"), "credits"),
        credits_label=credits_label,
        archetype=archetype,
        paginated=paginated,
        metered=metered,
        cache_ttl_seconds=_integer(source.get("cache_ttl_seconds"), "cache TTL"),
        delivery=delivery,
        docs_url=docs_url,
    )


def _catalog_row_route(row: object) -> str | None:
    if not isinstance(row, Mapping):
        return None
    try:
        method = _text(row.get("method"), "HTTP method", maximum=10).upper()
        path = _route_path(row.get("path"))
    except CatalogInvalid:
        return None
    return f"{method} {path}" if method in _METHODS else None


def wave1_route_identity(catalog: object, route_ids: Sequence[str]) -> tuple[str, str]:
    """The funded lane's catalog identity: the approved Wave 1 routes alone.

    Decided 4 September 2026 after the vendor changed pagination metadata on
    five LinkedIn routes within two hours of a funded-key read, which moved the
    whole-catalog digests and refused a pilot whose eight routes were untouched.
    Money rests on these routes, so the identity is their canonical route,
    credits, credit label and metered flag; the whole-catalog digests stay
    with the source lab. A missing route refuses.
    """
    endpoints = getattr(catalog, "endpoints", None)
    if not isinstance(endpoints, tuple):
        raise CatalogInvalid("catalog endpoints are unavailable")
    by_path = {item.route_path: item for item in endpoints if item.http_method == "GET"}
    rows = []
    for route_id in sorted(set(route_ids)):
        item = by_path.get(f"/v1/{route_id}")
        if item is None:
            raise CatalogInvalid(f"wave1 route is missing from the catalog: {route_id}")
        rows.append(
            {
                "canonical_route": item.canonical_route,
                "credits": format(item.credits, "f"),
                "credits_label": item.credits_label,
                "metered": item.metered,
            }
        )
    route_payload = "".join(f"{row['canonical_route']}\n" for row in rows)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (
        hashlib.sha256(route_payload.encode("utf-8")).hexdigest(),
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _normalized_content_digest(endpoints: tuple[EndpointCapability, ...]) -> str:
    payload = [
        {
            "endpoint_id": item.endpoint_id,
            "canonical_route": item.canonical_route,
            "http_method": item.http_method,
            "route_path": item.route_path,
            "platform": item.platform,
            "resource": item.resource,
            "official_capability": item.official_capability,
            "official_parameters": list(item.official_parameters),
            "credits": format(item.credits, "f"),
            "credits_label": item.credits_label,
            "archetype": item.archetype,
            "paginated": item.paginated,
            "metered": item.metered,
            "cache_ttl_seconds": item.cache_ttl_seconds,
            "delivery": item.delivery,
            "docs_url": item.docs_url,
        }
        for item in endpoints
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def inventory_metadata_digest(rows: tuple[Mapping[str, object], ...]) -> str:
    """Rebuild the catalog metadata digest from canonical inventory rows."""

    endpoints = tuple(
        sorted(
            (
                EndpointCapability(
                    endpoint_id=row["endpoint_id"],
                    canonical_route=f"{row['http_method']} {row['route_path']}",
                    http_method=row["http_method"],
                    route_path=row["route_path"],
                    platform=row["platform"],
                    resource=row["resource"],
                    official_capability=row["official_capability"],
                    official_parameters=tuple(row["official_parameters"]),
                    credits=row["official_credits"],
                    credits_label=row["official_credits_label"],
                    archetype=row["official_archetype"],
                    paginated=row["catalog_paginated"],
                    metered=row["catalog_metered"],
                    cache_ttl_seconds=row["cache_ttl_seconds"],
                    delivery=row["delivery"],
                    docs_url=row["docs_url"],
                )
                for row in rows
            ),
            key=lambda endpoint: endpoint.canonical_route,
        )
    )
    return _normalized_content_digest(endpoints)


def normalize_catalog(
    payload: object,
    *,
    checked_at: datetime,
    expected_platforms: int = 50,
    expected_endpoints: int = 400,
    wave1_route_ids: Sequence[str] | None = None,
) -> CatalogSnapshot:
    checked_at = _utc(checked_at, "checked_at")
    envelope = _mapping(payload, "catalog response")
    if envelope.get("success") is not True or envelope.get("endpoint") != "/v1/utility/endpoints":
        raise CatalogInvalid("catalog response is invalid")
    if _decimal(envelope.get("credits_used"), "credits used") != 0:
        raise CatalogInvalid("catalog utility must cost zero credit")
    data = _mapping(envelope.get("data"), "catalog data")
    stats = _mapping(data.get("stats"), "catalog stats")
    platform_count = _integer(stats.get("platforms"), "platform count", minimum=1)
    route_count = _integer(stats.get("endpoints"), "endpoint count", minimum=1)
    social_count = _integer(stats.get("social_platforms"), "social platform count")
    if platform_count != expected_platforms:
        raise CatalogInvalid("platform count does not match live expectation")
    if route_count != expected_endpoints or data.get("total") != route_count:
        raise CatalogInvalid("endpoint count does not match live expectation")
    raw_endpoints = data.get("endpoints")
    if not isinstance(raw_endpoints, list) or len(raw_endpoints) != route_count:
        raise CatalogInvalid("endpoint count does not match endpoint rows")
    wave1_paths = frozenset(_WAVE1_CHANNEL_BY_ROUTE)
    required_wave1_paths = frozenset()
    if wave1_route_ids is not None:
        if not isinstance(wave1_route_ids, Sequence) or isinstance(wave1_route_ids, str):
            raise CatalogInvalid("Wave 1 routes are invalid")
        required_wave1_paths = frozenset(f"/v1/{_text(route, 'Wave 1 route', maximum=300)}" for route in wave1_route_ids)
        if len(required_wave1_paths) != len(wave1_route_ids) or not required_wave1_paths:
            raise CatalogInvalid("Wave 1 routes are invalid")
    normalized = []
    dropped_rows = []
    for row_number, row in enumerate(raw_endpoints, start=1):
        route = _catalog_row_route(row)
        path = route.split(" ", 1)[1] if route is not None else None
        is_wave1_route = path in wave1_paths or path in required_wave1_paths
        try:
            normalized.append(_endpoint(row, wave1_route=is_wave1_route))
        except CatalogInvalid as error:
            if is_wave1_route:
                raise
            dropped_rows.append(
                CatalogRowDrop(row_number, route, str(error))
            )
    endpoints = tuple(sorted(normalized, key=lambda row: row.canonical_route))
    routes = tuple(row.canonical_route for row in endpoints)
    if len(routes) != len(set(routes)):
        raise CatalogInvalid("duplicate canonical route")
    derived_platforms = {row.platform for row in endpoints}
    if len(derived_platforms) != platform_count:
        if not dropped_rows:
            raise CatalogInvalid("platform count does not match endpoint rows")
    if required_wave1_paths:
        found_wave1_paths = {row.route_path for row in endpoints if row.route_path in required_wave1_paths}
        missing_wave1_paths = required_wave1_paths - found_wave1_paths
        if missing_wave1_paths:
            raise CatalogInvalid("approved Wave 1 route is missing from the catalog")
    catalog_payload = "".join(f"{route}\n" for route in routes)
    request_id = _text(envelope.get("request_id"), "request ID", maximum=200)
    return CatalogSnapshot(
        platform_count=platform_count,
        route_count=route_count,
        social_platform_count=social_count,
        catalog_digest=hashlib.sha256(catalog_payload.encode("utf-8")).hexdigest(),
        normalized_content_digest=_normalized_content_digest(endpoints),
        endpoints=endpoints,
        checked_at=checked_at,
        request_id=request_id,
        credits_used=Decimal(0),
        credits_remaining=_optional_decimal(envelope.get("credits_remaining"), "credits remaining"),
        dropped_rows=tuple(dropped_rows),
    )


def normalize_balance(payload: object, *, observed_at: datetime) -> BalanceSnapshot:
    observed_at = _utc(observed_at, "observed_at")
    envelope = _mapping(payload, "balance response")
    if envelope.get("success") is not True or envelope.get("endpoint") != "/v1/credits/balance":
        raise CatalogInvalid("balance response is invalid")
    if _decimal(envelope.get("credits_used"), "credits used") != 0:
        raise CatalogInvalid("balance utility must cost zero credit")
    data = _mapping(envelope.get("data"), "balance data")
    balance = _decimal(data.get("balance"), "balance")
    if _decimal(envelope.get("credits_remaining"), "credits remaining") != balance:
        raise CatalogInvalid("balance does not match credits remaining")
    return BalanceSnapshot(
        balance=balance,
        recent_deductions=_decimal(data.get("recent_deductions"), "recent deductions"),
        observed_at=observed_at,
        request_id=_text(envelope.get("request_id"), "request ID", maximum=200),
    )


def _validated_inventory_catalog(catalog: object) -> CatalogSnapshot:
    if not isinstance(catalog, CatalogSnapshot):
        raise SourceLabInventoryInvalid("catalog snapshot is invalid")
    if not isinstance(catalog.credits_used, Decimal) or not isinstance(
        catalog.credits_remaining, Decimal | None
    ):
        raise SourceLabInventoryInvalid("catalog credits must use native Decimal values")
    try:
        checked_at = _utc(catalog.checked_at, "catalog checked_at")
        platform_count = _integer(catalog.platform_count, "platform count", minimum=1)
        route_count = _integer(catalog.route_count, "route count", minimum=1)
        social_count = _integer(catalog.social_platform_count, "social platform count")
        _text(catalog.request_id, "catalog request ID", maximum=200)
        if _decimal(catalog.credits_used, "catalog credits used") != 0:
            raise SourceLabInventoryInvalid("catalog snapshot must be zero credit")
        _optional_decimal(catalog.credits_remaining, "catalog credits remaining")
    except CatalogInvalid as error:
        raise SourceLabInventoryInvalid(str(error)) from error
    if checked_at != catalog.checked_at:
        raise SourceLabInventoryInvalid("catalog checked_at is invalid")
    if social_count > platform_count:
        raise SourceLabInventoryInvalid("social platform count exceeds platform count")
    if (
        not isinstance(catalog.endpoints, tuple)
        or not isinstance(catalog.dropped_rows, tuple)
        or len(catalog.endpoints) + len(catalog.dropped_rows) != route_count
    ):
        raise SourceLabInventoryInvalid("route count does not match endpoint rows")
    for drop in catalog.dropped_rows:
        if (
            not isinstance(drop, CatalogRowDrop)
            or type(drop.row_number) is not int
            or not 1 <= drop.row_number <= route_count
            or (drop.route is not None and _catalog_row_route({"method": drop.route.split(" ", 1)[0], "path": drop.route.split(" ", 1)[1]}) != drop.route)
        ):
            raise SourceLabInventoryInvalid("catalog dropped row record is invalid")
        try:
            _text(drop.reason, "catalog drop reason", maximum=200)
        except CatalogInvalid as error:
            raise SourceLabInventoryInvalid(str(error)) from error
    routes = []
    platforms = set()
    for item in catalog.endpoints:
        if not isinstance(item, EndpointCapability):
            raise SourceLabInventoryInvalid("catalog endpoint is invalid")
        if not isinstance(item.credits, Decimal):
            raise SourceLabInventoryInvalid("official credits must use native Decimal values")
        try:
            method = _text(item.http_method, "HTTP method", maximum=10)
            path = _route_path(item.route_path)
            platform = _text(item.platform, "platform", maximum=64)
            _text(item.resource, "resource", maximum=200)
            _text(item.official_capability, "official capability")
            credits = _decimal(item.credits, "official credits")
            _text(
                item.credits_label,
                "official credits label",
                maximum=10_000 if path in _WAVE1_CHANNEL_BY_ROUTE else 500,
            )
            _text(item.archetype, "official archetype", maximum=100)
            ttl = _integer(item.cache_ttl_seconds, "cache TTL")
            docs_url = _text(item.docs_url, "docs URL", maximum=500)
        except CatalogInvalid as error:
            raise SourceLabInventoryInvalid(str(error)) from error
        if method not in _METHODS or method != method.upper():
            raise SourceLabInventoryInvalid("HTTP method is invalid")
        if _PLATFORM.fullmatch(platform) is None or path.split("/")[2] != platform:
            raise SourceLabInventoryInvalid("platform is invalid")
        canonical_route = f"{method} {path}"
        if item.canonical_route != canonical_route:
            raise SourceLabInventoryInvalid("canonical route is invalid")
        if item.endpoint_id != _endpoint_id(method, path):
            raise SourceLabInventoryInvalid("endpoint ID is invalid")
        if (
            not isinstance(item.official_parameters, tuple)
            or item.official_parameters != tuple(sorted(set(item.official_parameters)))
            or any(
                not isinstance(value, str) or _TOKEN.fullmatch(value) is None
                for value in item.official_parameters
            )
        ):
            raise SourceLabInventoryInvalid("official parameters are invalid")
        if type(item.paginated) is not bool or type(item.metered) is not bool:
            raise SourceLabInventoryInvalid("catalog flags are invalid")
        if item.delivery is not None:
            try:
                _text(item.delivery, "delivery", maximum=100)
            except CatalogInvalid as error:
                raise SourceLabInventoryInvalid(str(error)) from error
        parsed_docs = urlsplit(docs_url)
        if parsed_docs.scheme != "https" or parsed_docs.hostname not in {
            "socialcrawl.dev",
            "www.socialcrawl.dev",
        }:
            raise SourceLabInventoryInvalid("docs URL is invalid")
        if credits != item.credits or ttl != item.cache_ttl_seconds:
            raise SourceLabInventoryInvalid("catalog endpoint values are not canonical")
        routes.append(canonical_route)
        platforms.add(platform)
    if routes != sorted(routes) or len(routes) != len(set(routes)):
        raise SourceLabInventoryInvalid("canonical routes must be sorted and unique")
    if len(platforms) != platform_count:
        raise SourceLabInventoryInvalid("platform count does not match endpoint rows")
    digest = hashlib.sha256("".join(f"{route}\n" for route in routes).encode("utf-8")).hexdigest()
    if catalog.catalog_digest != digest:
        raise SourceLabInventoryInvalid("catalog digest does not match endpoint rows")
    if catalog.normalized_content_digest != _normalized_content_digest(catalog.endpoints):
        raise SourceLabInventoryInvalid(
            "normalized content digest does not match endpoint metadata"
        )
    return catalog


def _validated_inventory_balance(balance: object) -> BalanceSnapshot:
    if not isinstance(balance, BalanceSnapshot):
        raise SourceLabInventoryInvalid("balance snapshot is invalid")
    if not isinstance(balance.balance, Decimal) or not isinstance(
        balance.recent_deductions, Decimal
    ):
        raise SourceLabInventoryInvalid("balance values must use native Decimal values")
    try:
        _decimal(balance.balance, "balance")
        _decimal(balance.recent_deductions, "recent deductions")
        _utc(balance.observed_at, "balance observed_at")
        _text(balance.request_id, "balance request ID", maximum=200)
    except CatalogInvalid as error:
        raise SourceLabInventoryInvalid(str(error)) from error
    if balance.funding_math_status != "unknown":
        raise SourceLabInventoryInvalid("funding math must remain unknown")
    if balance.funded_increase_observed is not False:
        raise SourceLabInventoryInvalid("funded increase must remain false")
    if balance.monthly_optional_credit_cap != MONTHLY_OPTIONAL_CREDIT_CAP:
        raise SourceLabInventoryInvalid("monthly optional credit cap is invalid")
    if balance.optional_calls_enabled is not False:
        raise SourceLabInventoryInvalid("optional calls must remain disabled")
    return balance


def build_inventory_response(
    *,
    scope: ResolvedScope,
    catalog: CatalogSnapshot,
    balance: BalanceSnapshot,
    expectation_version: str,
    wave1_identity: tuple[Sequence[str], tuple[str, str]] | None = None,
) -> dict[str, object]:
    if not isinstance(scope, ResolvedScope) or scope.contract_version != CONTRACT_VERSION:
        raise SourceLabInventoryInvalid("resolved scope is invalid")
    if scope.audience_lens_ids:
        raise SourceLabInventoryInvalid("Source Lab inventory must remain audience-neutral")
    try:
        expectation_version = _text(expectation_version, "expectation version", maximum=200)
    except CatalogInvalid as error:
        raise SourceLabInventoryInvalid(str(error)) from error
    expectation = CATALOG_EXPECTATIONS.get(expectation_version)
    if expectation is None:
        raise SourceLabInventoryInvalid("expectation version is not approved")
    catalog = _validated_inventory_catalog(catalog)
    if wave1_identity is None:
        observed = (
            catalog.platform_count,
            catalog.route_count,
            catalog.social_platform_count,
            catalog.catalog_digest,
            catalog.normalized_content_digest,
        )
        if observed != expectation:
            raise SourceLabInventoryInvalid("catalog does not match the approved expectation")
    else:
        # The funded lane writes its source values against the catalog it ran
        # on, and that catalog is admitted by the eight Wave 1 routes' identity,
        # not by whole-catalog digests the vendor moves several times a day
        # (attempt 14, 4 Sep 2026, closed on the ledger and then refused here).
        routes, approved = wave1_identity
        try:
            observed_identity = wave1_route_identity(catalog, tuple(routes))
        except CatalogInvalid as error:
            raise SourceLabInventoryInvalid(str(error)) from error
        if observed_identity != tuple(approved):
            raise SourceLabInventoryInvalid(
                "catalog does not carry the approved Wave 1 route identity"
            )
    balance = _validated_inventory_balance(balance)
    sources = [
        {
            "endpoint_id": item.endpoint_id,
            "vendor": "socialcrawl",
            "platform": item.platform,
            "resource": item.resource,
            "http_method": item.http_method,
            "route_path": item.route_path,
            "route_role": "inventory",
            "source_family": "socialcrawl",
            "market": None,
            "status": "inventory_only",
            "official_capability": item.official_capability,
            "official_parameters": list(item.official_parameters),
            "official_credits": item.credits,
            "official_credits_label": item.credits_label,
            "official_archetype": item.archetype,
            "catalog_paginated": item.paginated,
            "catalog_metered": item.metered,
            "cache_ttl_seconds": item.cache_ttl_seconds,
            "delivery": item.delivery,
            "docs_url": item.docs_url,
            "calls": None,
            "credits": None,
            "rows": None,
            "integrity": None,
            "geo_precision": None,
            "unique_lift": None,
            "last_success_at": None,
            "downstream_consumers": [],
            "blocking_reason": None,
            "kill_test_result": None,
            "review_date": None,
            "quality_state": "unmeasured",
            "cost_state": "catalog_only",
            "yield_state": "unmeasured",
            "kill_test_state": "not_run",
            "last_checked_at": catalog.checked_at,
        }
        for item in catalog.endpoints
    ]
    return {
        "contract_version": SOURCE_LAB_CONTRACT_VERSION,
        "resource_version": SOURCE_LAB_RESOURCE_VERSION,
        "client_scope_id": scope.client_scope_id,
        "market_scope": list(scope.market_scope),
        "brand_config_id": scope.brand_config_id,
        "audience_lens_ids": [],
        "theme_id": scope.theme_id,
        "run_id": scope.run_id,
        "catalog": {
            "expectation_version": expectation_version,
            "platform_count": catalog.platform_count,
            "route_count": catalog.route_count,
            "normalized_route_count": len(catalog.endpoints),
            "dropped_endpoint_rows": [
                {"row_number": item.row_number, "route": item.route, "reason": item.reason}
                for item in catalog.dropped_rows
            ],
            "social_platform_count": catalog.social_platform_count,
            "catalog_digest": catalog.catalog_digest,
            "checked_at": catalog.checked_at,
            "completeness_state": "verified_snapshot",
        },
        "funding": {
            "balance": balance.balance,
            "recent_deductions": balance.recent_deductions,
            "observed_at": balance.observed_at,
            "funding_math_status": "unknown",
            "funded_increase_observed": False,
            "selected_top_up_credits": None,
            "monthly_optional_credit_cap": MONTHLY_OPTIONAL_CREDIT_CAP,
            "monthly_optional_credits_used": None,
            "optional_calls_enabled": False,
        },
        "sources": sources,
    }


def build_source_performance_rows(
    *,
    scope: ResolvedScope,
    catalog: CatalogSnapshot,
    balance: BalanceSnapshot,
    expectation_version: str,
    wave1_source_values: Mapping[str, Wave1SourceValueResult] | None = None,
    wave1_identity: tuple[Sequence[str], tuple[str, str]] | None = None,
) -> tuple[Mapping[str, object], ...]:
    """Build the exact insert-only Source Lab inventory and balance rows."""

    response = build_inventory_response(
        scope=scope,
        catalog=catalog,
        balance=balance,
        expectation_version=expectation_version,
        wave1_identity=wave1_identity,
    )
    inventory_date = catalog.checked_at.date()
    source_values = dict(wave1_source_values or {})
    if any(
        route not in _WAVE1_CHANNEL_BY_ROUTE or not isinstance(result, Wave1SourceValueResult)
        for route, result in source_values.items()
    ):
        raise SourceLabInventoryInvalid("Wave 1 source values are invalid")
    rows: list[Mapping[str, object]] = []
    for source in response["sources"]:
        vendor_family, channel_family, funded_lane = _source_identity(
            source["route_path"], source["source_family"]
        )
        source_value = source_values.get(source["route_path"])
        activation = resolve_wave1_activation(source_value) if source_value is not None else None
        downstream_consumers = ()
        if source_value is not None and activation == "active":
            downstream_consumers = tuple(
                name
                for count, name in (
                    (source_value.marginal_candidates, "signal_candidates_v2"),
                    (source_value.marginal_evidence, "signal_evidence_v2"),
                )
                if count > 0
            )
        row = {
            "client_scope_id": scope.client_scope_id,
            "market_scope": tuple(scope.market_scope),
            "brand_config_id": scope.brand_config_id,
            "audience_lens_ids": (),
            "theme_id": scope.theme_id,
            "run_id": scope.run_id,
            "contract_version": SOURCE_LAB_CONTRACT_VERSION,
            "source_performance_id": _source_performance_id(
                scope.client_scope_id,
                inventory_date,
                source["vendor"],
                source["http_method"],
                source["route_path"],
                source["market"],
            ),
            "metric_date": inventory_date,
            "vendor": source["vendor"],
            "http_method": source["http_method"],
            "route_path": source["route_path"],
            "endpoint_id": source["endpoint_id"],
            "route_role": "inventory",
            "source_family": channel_family,
            "market": None,
            "status": activation or "inventory_only",
            "platform": source["platform"],
            "resource": source["resource"],
            "catalog_digest": catalog.catalog_digest,
            "official_credits": source["official_credits"],
            "official_credits_label": source["official_credits_label"],
            "official_archetype": source["official_archetype"],
            "catalog_paginated": source["catalog_paginated"],
            "catalog_metered": source["catalog_metered"],
            "cache_ttl_seconds": source["cache_ttl_seconds"],
            "delivery": source["delivery"],
            "docs_url": source["docs_url"],
            "vendor_family": vendor_family,
            "channel_family": channel_family,
            "funded_lane": funded_lane,
            "calls": None,
            "credits": None,
            "rows": source_value.unique_observations if source_value is not None else None,
            "integrity": None,
            "geo_precision": None,
            "unique_lift": (
                float(source_value.marginal_candidates + source_value.marginal_evidence)
                if source_value is not None
                else None
            ),
            "last_success_at": None,
            "downstream_consumers": downstream_consumers,
            "official_capability": source["official_capability"],
            "official_parameters": tuple(source["official_parameters"]),
            "official_price_components": (),
            "blocking_reason": source_value.reason if source_value is not None else None,
            "kill_test_result": source_value.reason or "passed"
            if source_value is not None
            else None,
            "review_date": None,
            "last_checked_at": catalog.checked_at,
            "balance": None,
            "observed_at": None,
            "balance_read_status": None,
            "recent_deductions": None,
            "deductions_interval": None,
            "funding_math_status": None,
            "funded_increase_amount": None,
            "funded_increase_observed": None,
            "selected_top_up_credits": None,
            "baseline_credits_per_day": None,
            "baseline_window": None,
            "runway_days": None,
            "monthly_optional_credit_cap": None,
            "monthly_optional_credits_used": None,
            "optional_calls_enabled": None,
        }
        rows.append(MappingProxyType(row))

    balance_date = balance.observed_at.date()
    balance_path = "/v1/credits/balance"
    balance_row = {
        "client_scope_id": scope.client_scope_id,
        "market_scope": tuple(scope.market_scope),
        "brand_config_id": scope.brand_config_id,
        "audience_lens_ids": (),
        "theme_id": scope.theme_id,
        "run_id": scope.run_id,
        "contract_version": SOURCE_LAB_CONTRACT_VERSION,
        "source_performance_id": _source_performance_id(
            scope.client_scope_id,
            balance_date,
            "socialcrawl",
            "GET",
            balance_path,
            None,
        ),
        "metric_date": balance_date,
        "vendor": "socialcrawl",
        "http_method": "GET",
        "route_path": balance_path,
        "endpoint_id": _endpoint_id("GET", balance_path),
        "route_role": "utility_balance",
        "source_family": "socialcrawl",
        "market": None,
        "status": "inventory_only",
        "platform": None,
        "resource": None,
        "catalog_digest": None,
        "official_credits": None,
        "official_credits_label": None,
        "official_archetype": None,
        "catalog_paginated": None,
        "catalog_metered": None,
        "cache_ttl_seconds": None,
        "delivery": None,
        "docs_url": None,
        "vendor_family": "socialcrawl",
        "channel_family": "socialcrawl",
        "funded_lane": None,
        "calls": None,
        "credits": None,
        "rows": None,
        "integrity": None,
        "geo_precision": None,
        "unique_lift": None,
        "last_success_at": None,
        "downstream_consumers": (),
        "official_capability": "Reads the current vendor credit balance.",
        "official_parameters": (),
        "official_price_components": (),
        "blocking_reason": None,
        "kill_test_result": None,
        "review_date": None,
        "last_checked_at": balance.observed_at,
        "balance": balance.balance,
        "observed_at": balance.observed_at,
        "balance_read_status": "ok",
        "recent_deductions": balance.recent_deductions,
        "deductions_interval": None,
        "funding_math_status": "unknown",
        "funded_increase_amount": None,
        "funded_increase_observed": False,
        "selected_top_up_credits": None,
        "baseline_credits_per_day": None,
        "baseline_window": None,
        "runway_days": None,
        "monthly_optional_credit_cap": Decimal(MONTHLY_OPTIONAL_CREDIT_CAP),
        "monthly_optional_credits_used": None,
        "optional_calls_enabled": False,
    }
    rows.append(MappingProxyType(balance_row))
    return tuple(sorted(rows, key=lambda row: row["source_performance_id"]))
