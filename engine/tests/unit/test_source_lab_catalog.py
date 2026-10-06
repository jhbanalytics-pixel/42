"""Canonical live SocialCrawl catalog and balance tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.source_lab import (
    CatalogInvalid,
    normalize_balance,
    normalize_catalog,
    wave1_route_identity,
)
from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

CHECKED_AT = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
LIVE_CATALOG_AT = datetime(2026, 9, 27, 13, 10, tzinfo=UTC)
FUNDED_LIVE_CATALOG = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "source_lab"
    / "wave1_metered_catalog_rows.json"
)


def endpoint(
    path: str,
    *,
    method: str = "GET",
    platform: str = "tiktok",
    credits: int = 1,
    summary: str = "Gets public profile data.",
) -> dict[str, object]:
    return {
        "id": f"{platform}/{path.rsplit('/', 1)[-1]}",
        "path": path,
        "method": method,
        "platform": platform,
        "resource": path.removeprefix(f"/v1/{platform}/"),
        "summary": summary,
        "credits": credits,
        "credits_label": f"{credits} (standard)",
        "archetype": "Author",
        "required_params": ["handle"],
        "one_of": [],
        "optional_params": ["trim"],
        "paginated": False,
        "metered": False,
        "cache_ttl_seconds": 900,
        "delivery": None,
        "docs_url": f"https://www.socialcrawl.dev/platforms/{platform}",
    }


def payload(endpoints: list[dict[str, object]], platforms: int) -> dict[str, object]:
    return {
        "success": True,
        "platform": "utility",
        "endpoint": "/v1/utility/endpoints",
        "data": {
            "kind": "endpoint_catalog",
            "stats": {
                "platforms": platforms,
                "endpoints": len(endpoints),
                "social_platforms": platforms,
            },
            "total": len(endpoints),
            "endpoints": endpoints,
        },
        "credits_used": 0,
        "credits_remaining": 6879,
        "request_id": "req-catalog",
        "cached": False,
    }


def test_catalog_identity_and_digest_ignore_source_order():
    first = endpoint("/v1/tiktok/profile")
    second = endpoint(
        "/v1/youtube/search",
        platform="youtube",
        summary="Searches public videos.",
    )

    forward = normalize_catalog(
        payload([first, second], 2),
        checked_at=CHECKED_AT,
        expected_platforms=2,
        expected_endpoints=2,
    )
    reverse = normalize_catalog(
        payload([second, first], 2),
        checked_at=CHECKED_AT,
        expected_platforms=2,
        expected_endpoints=2,
    )

    assert forward == reverse
    assert forward.route_count == 2
    assert forward.platform_count == 2
    assert (
        forward.catalog_digest == "82bcdde6253645bc358a0d0c31afc4d5f6122d6b8acdf803cfd36be3c7d94321"
    )
    assert tuple(item.canonical_route for item in forward.endpoints) == (
        "GET /v1/tiktok/profile",
        "GET /v1/youtube/search",
    )
    assert all(item.endpoint_id.startswith("endpoint_") for item in forward.endpoints)
    assert all(len(item.endpoint_id) == 73 for item in forward.endpoints)
    assert forward.request_id == "req-catalog"
    assert forward.credits_used == 0
    assert not hasattr(forward.endpoints[0], "route_role")
    assert not hasattr(forward.endpoints[0], "status")
    assert not hasattr(forward.endpoints[0], "integrity")


def test_catalog_normalizes_method_path_and_parameters():
    row = endpoint(" /v1/tiktok/profile/ ", method=" get ")
    row["required_params"] = ["handle", "handle"]
    row["optional_params"] = ["trim", "region"]
    row["one_of"] = [["handle", "user_id"]]

    catalog = normalize_catalog(
        payload([row], 1),
        checked_at=CHECKED_AT,
        expected_platforms=1,
        expected_endpoints=1,
    )

    item = catalog.endpoints[0]
    assert item.http_method == "GET"
    assert item.route_path == "/v1/tiktok/profile"
    assert item.official_parameters == ("handle", "region", "trim", "user_id")


def test_catalog_preserves_the_observed_long_native_credit_label():
    row = endpoint("/v1/tiktok/profile")
    row["credits_label"] = "x" * 331

    catalog = normalize_catalog(
        payload([row], 1),
        checked_at=CHECKED_AT,
        expected_platforms=1,
        expected_endpoints=1,
    )

    assert catalog.endpoints[0].credits_label == "x" * 331


@pytest.mark.parametrize("rows", [[endpoint("/v1/tiktok/profile"), endpoint("/v1/tiktok/profile")]])
def test_catalog_rejects_duplicate_official_rows(rows):
    with pytest.raises(CatalogInvalid):
        normalize_catalog(
            payload(rows, 1),
            checked_at=CHECKED_AT,
            expected_platforms=1,
            expected_endpoints=len(rows),
        )


@pytest.mark.parametrize(
    "row",
    [
        endpoint("/v1/tiktok/profile?handle=x"),
        endpoint("/v1/tiktok/profile", summary="<b>Profile</b>"),
        endpoint("/v1/tiktok/profile", credits=-1),
    ],
)
def test_invalid_non_wave1_rows_are_dropped_and_recorded(row):
    catalog = normalize_catalog(
        payload([row], 1),
        checked_at=CHECKED_AT,
        expected_platforms=1,
        expected_endpoints=1,
    )

    assert catalog.route_count == 1
    assert catalog.endpoints == ()
    assert len(catalog.dropped_rows) == 1
    assert catalog.dropped_rows[0].row_number == 1
    assert catalog.dropped_rows[0].reason


def test_catalog_rejects_live_count_or_free_call_mismatch():
    body = payload([endpoint("/v1/tiktok/profile")], 1)
    body["credits_used"] = 1
    with pytest.raises(CatalogInvalid, match="zero credit"):
        normalize_catalog(
            body,
            checked_at=CHECKED_AT,
            expected_platforms=1,
            expected_endpoints=1,
        )

    with pytest.raises(CatalogInvalid, match="endpoint count"):
        normalize_catalog(
            payload([endpoint("/v1/tiktok/profile")], 1),
            checked_at=CHECKED_AT,
            expected_platforms=1,
            expected_endpoints=2,
        )


def test_balance_normalization_fails_closed_on_funding_and_optional_calls():
    snapshot = normalize_balance(
        {
            "success": True,
            "endpoint": "/v1/credits/balance",
            "data": {"balance": 6879, "recent_deductions": 428},
            "credits_used": 0,
            "credits_remaining": 6879,
            "request_id": "req-balance",
        },
        observed_at=CHECKED_AT,
    )
    assert snapshot.balance == 6879
    assert snapshot.recent_deductions == 428
    assert snapshot.funding_math_status == "unknown"
    assert snapshot.funded_increase_observed is False
    assert snapshot.monthly_optional_credit_cap == 25_000
    assert snapshot.optional_calls_enabled is False


@pytest.mark.parametrize("balance", [-1, True, "6879", None])
def test_balance_rejects_non_numeric_or_negative_values(balance):
    with pytest.raises(CatalogInvalid, match="balance"):
        normalize_balance(
            {
                "success": True,
                "endpoint": "/v1/credits/balance",
                "data": {"balance": balance, "recent_deductions": 0},
                "credits_used": 0,
                "credits_remaining": 0,
                "request_id": "req-balance",
            },
            observed_at=CHECKED_AT,
        )


def test_catalog_and_balance_require_utc_observation_times():
    naive = datetime(2026, 8, 26, 18, 0)
    with pytest.raises(CatalogInvalid, match="UTC"):
        normalize_catalog(
            payload([endpoint("/v1/tiktok/profile")], 1),
            checked_at=naive,
            expected_platforms=1,
            expected_endpoints=1,
        )
    with pytest.raises(CatalogInvalid, match="UTC"):
        normalize_balance(
            {
                "success": True,
                "endpoint": "/v1/credits/balance",
                "data": {"balance": 6879, "recent_deductions": 0},
                "credits_used": 0,
                "credits_remaining": 6879,
                "request_id": "req-balance",
            },
            observed_at=naive,
        )


def test_saved_funded_catalog_accepts_long_wave1_labels_and_records_external_drops():
    raw = json.loads(FUNDED_LIVE_CATALOG.read_text(encoding="utf-8"))
    catalog = normalize_catalog(
        raw,
        checked_at=LIVE_CATALOG_AT,
        expected_platforms=4,
        expected_endpoints=9,
        wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
    )

    assert (catalog.platform_count, catalog.route_count, catalog.social_platform_count) == (
        4,
        9,
        4,
    )
    assert len(catalog.endpoints) == 8
    assert len(catalog.dropped_rows) == 1
    assert all(drop.row_number > 0 and drop.route and drop.reason for drop in catalog.dropped_rows)
    wave1_routes = frozenset(f"GET /v1/{route}" for route in WAVE1_ROUTE_SPECS)
    assert all(drop.route not in wave1_routes for drop in catalog.dropped_rows)
    assert all(drop.reason == "credits label is invalid" for drop in catalog.dropped_rows)

    expected = {
        "tiktok/song": ("1", "1 (standard)", False),
        "tiktok/song/videos": ("1", "1 (standard)", False),
        "instagram/music/trending": ("5", "5 (advanced)", False),
        "instagram/audio/reels": ("1", "1 (standard)", False),
        "instagram/search/reels": ("1", "1-9 (metered):", True),
        "youtube/shorts/trending": ("5", "5-15 (metered):", True),
        "youtube/video/comments": ("1", "1-5 (metered):", True),
        "reddit/post/comments": ("5", "5-9 (metered):", True),
    }
    endpoints = {item.route_path.removeprefix("/v1/"): item for item in catalog.endpoints}
    assert set(endpoints) >= set(expected)
    for route, (credits, label_prefix, metered) in expected.items():
        item = endpoints[route]
        assert format(item.credits, "f") == credits
        assert item.credits_label.startswith(label_prefix)
        assert item.metered is metered

    assert wave1_route_identity(catalog, tuple(WAVE1_ROUTE_SPECS)) == (
        "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6",
        "501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a",
    )


@pytest.mark.parametrize("invalid_character", ["<", ">", "\x01", "\t", "\r", "\n", "\x1f", "\x7f", "\x85"])
@pytest.mark.parametrize("position", ["leading", "embedded", "trailing"])
def test_wave1_catalog_rejects_long_credit_labels_with_forbidden_characters(
    invalid_character, position,
):
    raw = json.loads(FUNDED_LIVE_CATALOG.read_text(encoding="utf-8"))
    paths = {f"/v1/{route}" for route in WAVE1_ROUTE_SPECS}
    raw["data"]["endpoints"] = [
        row for row in raw["data"]["endpoints"] if row["path"] in paths
    ]
    raw["data"]["stats"].update(platforms=4, endpoints=8, social_platforms=4)
    raw["data"]["total"] = 8
    row = next(
        item
        for item in raw["data"]["endpoints"]
        if item["path"] == "/v1/instagram/search/reels"
    )
    label = row["credits_label"]
    if position == "leading":
        row["credits_label"] = invalid_character + label
    elif position == "embedded":
        row["credits_label"] = label[:20] + invalid_character + label[20:]
    else:
        row["credits_label"] = label + invalid_character

    with pytest.raises(CatalogInvalid, match="credits label is invalid"):
        normalize_catalog(
            raw,
            checked_at=LIVE_CATALOG_AT,
            expected_platforms=4,
            expected_endpoints=8,
            wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
        )


def test_non_wave1_invalid_summary_is_dropped_and_recorded():
    raw = json.loads(FUNDED_LIVE_CATALOG.read_text(encoding="utf-8"))
    row = next(item for item in raw["data"]["endpoints"] if item["path"] == "/v1/tiktok/search")
    row["summary"] = "<invalid summary>"

    catalog = normalize_catalog(
        raw,
        checked_at=LIVE_CATALOG_AT,
        expected_platforms=4,
        expected_endpoints=9,
        wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
    )

    assert any(
        drop.route == "GET /v1/tiktok/search" and drop.reason == "official capability is invalid"
        for drop in catalog.dropped_rows
    )
