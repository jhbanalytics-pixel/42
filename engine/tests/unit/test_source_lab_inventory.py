"""Caller-facing Source Lab 2.1 inventory contract tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pytest
from src.analysis.open_intelligence.source_lab import (
    CATALOG_EXPECTATIONS,
    CatalogInvalid,
    SourceLabInventoryInvalid,
    build_inventory_response,
    normalize_balance,
    normalize_catalog,
    wave1_route_identity,
)
from src.contracts.open_intelligence import resolve_client_scope

CHECKED_AT = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
HISTORICAL_CATALOG_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "socialcrawl_catalog_2026_08_26.json"
)
CATALOG_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "socialcrawl_catalog_2026_08_27.json"
)
SOURCE_FIELDS = (
    "endpoint_id",
    "vendor",
    "platform",
    "resource",
    "http_method",
    "route_path",
    "route_role",
    "source_family",
    "market",
    "status",
    "official_capability",
    "official_parameters",
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
    "downstream_consumers",
    "blocking_reason",
    "kill_test_result",
    "review_date",
    "quality_state",
    "cost_state",
    "yield_state",
    "kill_test_state",
    "last_checked_at",
)


def endpoint(
    path: str, platform: str, credits: int, credits_label: str | None = None
) -> dict[str, object]:
    resource = path.rsplit("/", 1)[-1]
    return {
        "id": f"{platform}/{resource}",
        "path": path,
        "method": "GET",
        "platform": platform,
        "resource": resource,
        "summary": f"Reads public {resource} data.",
        "credits": credits,
        "credits_label": credits_label or f"{credits} credits",
        "archetype": "Author",
        "required_params": ["handle"],
        "one_of": [],
        "optional_params": [],
        "paginated": False,
        "metered": False,
        "cache_ttl_seconds": 900,
        "delivery": None,
        "docs_url": f"https://www.socialcrawl.dev/platforms/{platform}",
    }


@lru_cache(maxsize=1)
def snapshots():
    raw_catalog = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    catalog = normalize_catalog(
        raw_catalog,
        checked_at=CHECKED_AT,
    )
    balance = normalize_balance(
        {
            "success": True,
            "endpoint": "/v1/credits/balance",
            "data": {"balance": 6879, "recent_deductions": 428},
            "credits_used": 0,
            "credits_remaining": 6879,
            "request_id": "balance-request",
        },
        observed_at=CHECKED_AT,
    )
    return catalog, balance


def response():
    catalog, balance = snapshots()
    return build_inventory_response(
        scope=resolve_client_scope(run_id="source_lab_run_001"),
        catalog=catalog,
        balance=balance,
        expectation_version="socialcrawl_catalog_2026_08_27",
    )


def test_reviewed_fixture_changes_only_the_six_reddit_metadata_fields() -> None:
    assert CATALOG_FIXTURE.exists()
    historical = json.loads(HISTORICAL_CATALOG_FIXTURE.read_text(encoding="utf-8"))
    current = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    historical["request_id"] = current["request_id"]
    changes = []
    for before, after in zip(
        historical["data"]["endpoints"], current["data"]["endpoints"], strict=True
    ):
        for field in sorted(set(before) | set(after)):
            if before.get(field) != after.get(field):
                changes.append((before["id"], field, before.get(field), after.get(field)))
    label = (
        "1-26 (metered): 1 credit for the search page. With include_body=true, "
        "1 extra credit per post whose body is fetched, up to 25 per page, unused credits "
        "refunded."
    )
    assert changes == [
        (
            "reddit/search",
            "credits_label",
            "1 (standard)",
            label,
        ),
        ("reddit/search", "metered", False, True),
        (
            "reddit/search",
            "optional_params",
            ["sort", "timeframe", "after", "trim"],
            ["sort", "timeframe", "after", "trim", "include_body"],
        ),
        (
            "reddit/subreddit/search",
            "credits_label",
            "1 (standard)",
            label,
        ),
        ("reddit/subreddit/search", "metered", False, True),
        (
            "reddit/subreddit/search",
            "optional_params",
            ["query", "sort", "timeframe", "cursor"],
            ["query", "sort", "timeframe", "cursor", "include_body"],
        ),
    ]


def test_historical_and_current_expectations_remain_independently_admissible() -> None:
    historical = normalize_catalog(
        json.loads(HISTORICAL_CATALOG_FIXTURE.read_text(encoding="utf-8")),
        checked_at=CHECKED_AT,
    )
    current, balance = snapshots()
    scope = resolve_client_scope(run_id="source_lab_run_001")

    assert (
        historical.normalized_content_digest
        == CATALOG_EXPECTATIONS["socialcrawl_catalog_2026_08_26"][4]
    )
    assert current.catalog_digest == historical.catalog_digest
    assert current.normalized_content_digest == (
        "47290bc1da76f4c29226a70c22f25b239557bbe56ce27d9931a29d174ffdd7ca"
    )
    assert (
        build_inventory_response(
            scope=scope,
            catalog=historical,
            balance=balance,
            expectation_version="socialcrawl_catalog_2026_08_26",
        )["catalog"]["expectation_version"]
        == "socialcrawl_catalog_2026_08_26"
    )


@pytest.mark.parametrize("mutation", ["include_body", "metered", "label", "add", "delete"])
def test_current_expectation_kills_reviewed_metadata_and_route_mutations(mutation: str) -> None:
    raw = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    reddit = next(item for item in raw["data"]["endpoints"] if item["id"] == "reddit/search")
    if mutation == "include_body":
        reddit["optional_params"].remove("include_body")
    elif mutation == "metered":
        reddit["metered"] = False
    elif mutation == "label":
        reddit["credits_label"] = "1 (standard)"
    elif mutation == "add":
        added = dict(reddit, id="reddit/search-copy", path="/v1/reddit/search-copy")
        raw["data"]["endpoints"].append(added)
        raw["data"]["stats"]["endpoints"] = raw["data"]["total"] = 401
    else:
        raw["data"]["endpoints"].remove(reddit)
        raw["data"]["stats"]["endpoints"] = raw["data"]["total"] = 399

    try:
        catalog = normalize_catalog(raw, checked_at=CHECKED_AT)
    except CatalogInvalid:
        return
    _, balance = snapshots()
    with pytest.raises(SourceLabInventoryInvalid, match="approved expectation"):
        build_inventory_response(
            scope=resolve_client_scope(run_id="source_lab_run_001"),
            catalog=catalog,
            balance=balance,
            expectation_version="socialcrawl_catalog_2026_08_27",
        )


def test_response_has_exact_version_scope_catalog_and_funding_contract() -> None:
    payload = response()

    assert tuple(payload) == (
        "contract_version",
        "resource_version",
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "catalog",
        "funding",
        "sources",
    )
    assert payload["contract_version"] == "2.1.0"
    assert payload["resource_version"] == "source_lab_inventory_v1"
    assert payload["client_scope_id"] == "ogilvy_default"
    assert payload["market_scope"] == ["za", "ng", "ke"]
    assert payload["audience_lens_ids"] == []
    assert payload["catalog"] == {
        "expectation_version": "socialcrawl_catalog_2026_08_27",
        "platform_count": 50,
        "route_count": 400,
        "normalized_route_count": 400,
        "dropped_endpoint_rows": [],
        "social_platform_count": 28,
        "catalog_digest": payload["catalog"]["catalog_digest"],
        "checked_at": CHECKED_AT,
        "completeness_state": "verified_snapshot",
    }
    assert payload["catalog"]["catalog_digest"] == (
        "f843d6ad934e36b005090c1c642eb4b8e920bf2ca5d8828d7b9c1e7e4f143daa"
    )
    assert payload["funding"] == {
        "balance": Decimal("6879"),
        "recent_deductions": Decimal("428"),
        "observed_at": CHECKED_AT,
        "funding_math_status": "unknown",
        "funded_increase_observed": False,
        "selected_top_up_credits": None,
        "monthly_optional_credit_cap": 25_000,
        "monthly_optional_credits_used": None,
        "optional_calls_enabled": False,
    }


def test_every_catalog_route_appears_once_and_balance_is_not_a_source() -> None:
    payload = response()

    assert len(payload["sources"]) == payload["catalog"]["route_count"] == 400
    routes = [item["route_path"] for item in payload["sources"]]
    assert "/v1/tiktok/profile" in routes
    assert "/v1/youtube/search" in routes
    assert len({item["endpoint_id"] for item in payload["sources"]}) == 400
    assert all(item["route_path"] != "/v1/credits/balance" for item in payload["sources"])


def test_inventory_source_fields_are_exact_honest_and_keep_native_credit_semantics() -> None:
    item = next(
        source for source in response()["sources"] if source["route_path"] == "/v1/tiktok/profile"
    )

    assert tuple(item) == SOURCE_FIELDS
    assert item["vendor"] == "socialcrawl"
    assert item["route_role"] == "inventory"
    assert item["source_family"] == "socialcrawl"
    assert item["market"] is None
    assert item["status"] == "inventory_only"
    assert item["official_credits"] == Decimal("1")
    assert item["official_credits_label"] == "1 (standard)"
    assert item["official_parameters"] == ["handle", "user_id"]
    for field in (
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
    ):
        assert item[field] is None
    assert item["downstream_consumers"] == []
    assert item["quality_state"] == "unmeasured"
    assert item["cost_state"] == "catalog_only"
    assert item["yield_state"] == "unmeasured"
    assert item["kill_test_state"] == "not_run"


def test_nonempty_default_audience_lens_refuses_instead_of_becoming_inventory_identity() -> None:
    catalog, balance = snapshots()
    scope = replace(resolve_client_scope(run_id="source_lab_run_001"), audience_lens_ids=("gen_z",))

    with pytest.raises(SourceLabInventoryInvalid, match="audience-neutral"):
        build_inventory_response(
            scope=scope,
            catalog=catalog,
            balance=balance,
            expectation_version="socialcrawl_catalog_2026_08_27",
        )


@pytest.mark.parametrize(
    ("catalog_change", "balance_change", "message"),
    [
        ({"route_count": 3}, {}, "route count"),
        ({"catalog_digest": "bad"}, {}, "catalog digest"),
        ({"credits_used": Decimal("1")}, {}, "zero credit"),
        ({}, {"funding_math_status": "complete"}, "funding math"),
        ({}, {"optional_calls_enabled": True}, "optional calls"),
    ],
)
def test_forged_normalized_snapshots_fail_closed(
    catalog_change: dict[str, object], balance_change: dict[str, object], message: str
) -> None:
    catalog, balance = snapshots()

    with pytest.raises(SourceLabInventoryInvalid, match=message):
        build_inventory_response(
            scope=resolve_client_scope(run_id="source_lab_run_001"),
            catalog=replace(catalog, **catalog_change),
            balance=replace(balance, **balance_change),
            expectation_version="socialcrawl_catalog_2026_08_27",
        )


@pytest.mark.parametrize(
    ("endpoint_change", "message"),
    [
        ({"credits": 1.0}, "native Decimal"),
        ({"official_parameters": (1,)}, "official parameters"),
        (
            {"credits": Decimal("0.01"), "credits_label": "$0.01"},
            "normalized content digest",
        ),
    ],
)
def test_forged_endpoint_capability_cannot_bypass_native_catalog_types(
    endpoint_change: dict[str, object], message: str
) -> None:
    catalog, balance = snapshots()
    forged = replace(
        catalog,
        endpoints=(replace(catalog.endpoints[0], **endpoint_change), *catalog.endpoints[1:]),
    )

    with pytest.raises(SourceLabInventoryInvalid, match=message):
        build_inventory_response(
            scope=resolve_client_scope(run_id="source_lab_run_001"),
            catalog=forged,
            balance=balance,
            expectation_version="socialcrawl_catalog_2026_08_27",
        )


def test_unknown_or_short_production_expectation_cannot_emit_verified_snapshot() -> None:
    catalog, balance = snapshots()
    scope = resolve_client_scope(run_id="source_lab_run_001")

    with pytest.raises(SourceLabInventoryInvalid, match="expectation version"):
        build_inventory_response(
            scope=scope,
            catalog=catalog,
            balance=balance,
            expectation_version="invented_expectation",
        )

    short_catalog = normalize_catalog(
        {
            "success": True,
            "endpoint": "/v1/utility/endpoints",
            "data": {
                "stats": {"platforms": 1, "endpoints": 1, "social_platforms": 1},
                "total": 1,
                "endpoints": [endpoint("/v1/tiktok/profile", "tiktok", 1)],
            },
            "credits_used": 0,
            "credits_remaining": 6879,
            "request_id": "short-catalog",
        },
        checked_at=CHECKED_AT,
        expected_platforms=1,
        expected_endpoints=1,
    )
    with pytest.raises(SourceLabInventoryInvalid, match="approved expectation"):
        build_inventory_response(
            scope=scope,
            catalog=short_catalog,
            balance=balance,
            expectation_version="socialcrawl_catalog_2026_08_27",
        )


def test_zero_credit_catalog_envelope_may_omit_credits_remaining() -> None:
    # Since 2 September 2026 the utility catalog route answers credits_remaining as null and
    # adds cached and platform envelope keys; the balance route still carries the balance.
    raw = json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))
    raw["credits_remaining"] = None
    raw["cached"] = False
    raw["platform"] = None
    catalog = normalize_catalog(raw, checked_at=CHECKED_AT)
    assert catalog.credits_remaining is None
    assert catalog.credits_used == Decimal(0)
    _, balance = snapshots()
    response = build_inventory_response(
        scope=resolve_client_scope(run_id="source_lab_run_001"),
        catalog=catalog,
        balance=balance,
        expectation_version="socialcrawl_catalog_2026_08_27",
    )
    assert response["catalog"]["expectation_version"] == "socialcrawl_catalog_2026_08_27"
    raw["credits_remaining"] = "12"
    with pytest.raises(CatalogInvalid):
        normalize_catalog(raw, checked_at=CHECKED_AT)


CURRENT_CATALOG_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "socialcrawl_catalog_2026_09_02.json"
)


def test_2026_09_02_catalog_matches_its_approved_expectation() -> None:
    # Vendor catalog read on 2 September 2026 at zero credit. The expectation entry holds what
    # normalize_catalog computes from the committed fixture; nothing below is typed by hand.
    raw = json.loads(CURRENT_CATALOG_FIXTURE.read_text(encoding="utf-8"))
    catalog = normalize_catalog(
        raw,
        checked_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        expected_platforms=raw["data"]["stats"]["platforms"],
        expected_endpoints=raw["data"]["stats"]["endpoints"],
    )
    observed = (
        catalog.platform_count,
        catalog.route_count,
        catalog.social_platform_count,
        catalog.catalog_digest,
        catalog.normalized_content_digest,
    )
    assert observed == CATALOG_EXPECTATIONS["socialcrawl_catalog_2026_09_02"]
    assert observed[:3] == (55, 449, 29)
    assert catalog.credits_remaining is None
    _, balance = snapshots()
    scope = resolve_client_scope(run_id="source_lab_run_001")
    response = build_inventory_response(
        scope=scope,
        catalog=catalog,
        balance=balance,
        expectation_version="socialcrawl_catalog_2026_09_02",
    )
    assert response["catalog"]["expectation_version"] == "socialcrawl_catalog_2026_09_02"
    with pytest.raises(SourceLabInventoryInvalid, match="approved expectation"):
        build_inventory_response(
            scope=scope,
            catalog=catalog,
            balance=balance,
            expectation_version="socialcrawl_catalog_2026_08_27",
        )


LATEST_CATALOG_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "socialcrawl_catalog_2026_09_03.json"
)
FUNDED_CATALOG_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "socialcrawl_catalog_2026_09_04.json"
)
LIVE_FUNDED_CATALOG_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "source_lab"
    / "wave1_metered_catalog_rows.json"
)


def test_wave1_identity_admits_a_catalog_the_vendor_moved_elsewhere() -> None:
    # Attempt 14 on staging, 4 Sep 2026: the funded lane admitted the live catalog
    # by its Wave 1 route identity, ran, closed on the ledger, and then the source
    # value writer refused the same catalog against whole-catalog digests the
    # vendor had moved on five LinkedIn routes. The rows validate the way the
    # lane admits; whole-catalog matching stays the snapshot's rule.
    from src.analysis.open_intelligence.source_lab import build_source_performance_rows
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    raw = json.loads(FUNDED_CATALOG_FIXTURE.read_text(encoding="utf-8"))
    moved = next(item for item in raw["data"]["endpoints"] if item["platform"] == "linkedin")
    moved["credits"] = int(moved["credits"]) + 1
    catalog = normalize_catalog(
        raw,
        checked_at=datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
        expected_platforms=raw["data"]["stats"]["platforms"],
        expected_endpoints=raw["data"]["stats"]["endpoints"],
    )
    _catalog, balance = snapshots()
    identity = (
        tuple(WAVE1_ROUTE_SPECS),
        (
            "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6",
            "7d7152b2441f367417c74169a7bd4fb1a190a92763bbb43532167d5d4612fdfc",
        ),
    )
    common = {
        "scope": resolve_client_scope(run_id="run_wave1_moved_catalog"),
        "catalog": catalog,
        "balance": balance,
        "expectation_version": "socialcrawl_catalog_2026_09_04",
    }
    with pytest.raises(SourceLabInventoryInvalid, match="does not match"):
        build_source_performance_rows(**common)
    rows = build_source_performance_rows(**common, wave1_identity=identity)
    assert len(rows) == catalog.route_count + 1
    wrong = (tuple(WAVE1_ROUTE_SPECS), ("0" * 64, identity[1][1]))
    with pytest.raises(SourceLabInventoryInvalid, match="Wave 1 route identity"):
        build_source_performance_rows(**common, wave1_identity=wrong)
    missing = ((*WAVE1_ROUTE_SPECS, "tiktok/does/not/exist"), identity[1])
    with pytest.raises(SourceLabInventoryInvalid, match="missing"):
        build_source_performance_rows(**common, wave1_identity=missing)


def test_2026_09_04_catalog_remains_the_historical_funded_expectation() -> None:
    # Vendor catalog read on 4 September 2026 at zero credit through the funded key,
    # the day the first Wave 1 pilot ran. The funded lane had been pinned to the
    # 27 August catalog (50 platforms, 400 routes) and refused every run once the
    # vendor moved; it now names this expectation and the counts and digests come
    # from the committed fixture, nothing typed by hand.
    raw = json.loads(FUNDED_CATALOG_FIXTURE.read_text(encoding="utf-8"))
    catalog = normalize_catalog(
        raw,
        checked_at=datetime(2026, 9, 4, 15, 0, tzinfo=UTC),
        expected_platforms=raw["data"]["stats"]["platforms"],
        expected_endpoints=raw["data"]["stats"]["endpoints"],
    )
    observed = (
        catalog.platform_count,
        catalog.route_count,
        catalog.social_platform_count,
        catalog.catalog_digest,
        catalog.normalized_content_digest,
    )
    assert observed == CATALOG_EXPECTATIONS["socialcrawl_catalog_2026_09_04"]
    assert observed[:3] == (64, 556, 30)
    # The funded lane's identity is the eight Wave 1 routes of this catalog, not
    # its whole-catalog digests: the vendor changed pagination metadata on five
    # LinkedIn routes within two hours of this read and refused a pilot whose
    # routes were untouched (4 Sep 2026).
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    assert wave1_route_identity(catalog, tuple(WAVE1_ROUTE_SPECS)) == (
        "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6",
        "7d7152b2441f367417c74169a7bd4fb1a190a92763bbb43532167d5d4612fdfc",
    )
    with pytest.raises(CatalogInvalid, match="missing"):
        wave1_route_identity(catalog, (*WAVE1_ROUTE_SPECS, "tiktok/does/not/exist"))


def test_2026_09_27_funded_catalog_is_accepted_with_recorded_normalization_drops() -> None:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    raw = json.loads(LIVE_FUNDED_CATALOG_FIXTURE.read_text(encoding="utf-8"))
    catalog = normalize_catalog(
        raw,
        checked_at=datetime(2026, 9, 27, 13, 10, tzinfo=UTC),
        expected_platforms=4,
        expected_endpoints=9,
        wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
    )
    _catalog, balance = snapshots()

    payload = build_inventory_response(
        scope=resolve_client_scope(run_id="source_lab_run_2026_09_27"),
        catalog=catalog,
        balance=balance,
        expectation_version="socialcrawl_catalog_2026_09_04",
        wave1_identity=(
            tuple(WAVE1_ROUTE_SPECS),
            (
                "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6",
                "501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a",
            ),
        ),
    )

    assert payload["catalog"]["route_count"] == 9
    assert payload["catalog"]["normalized_route_count"] == 8
    assert len(payload["catalog"]["dropped_endpoint_rows"]) == 1
    assert len(payload["sources"]) == 8
    assert all(
        row["route"] and row["reason"]
        for row in payload["catalog"]["dropped_endpoint_rows"]
    )


@pytest.mark.parametrize("mutation", ["range", "metered"])
def test_live_wave1_range_or_meter_flag_drift_refuses_funded_source_rows(mutation: str) -> None:
    from src.analysis.open_intelligence.source_lab import build_source_performance_rows
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    raw = json.loads(LIVE_FUNDED_CATALOG_FIXTURE.read_text(encoding="utf-8"))
    row = next(
        item
        for item in raw["data"]["endpoints"]
        if item["path"] == "/v1/instagram/search/reels"
    )
    if mutation == "range":
        row["credits_label"] = row["credits_label"].replace("1-9 (metered)", "1-8 (metered)", 1)
    else:
        row["metered"] = False
    catalog = normalize_catalog(
        raw,
        checked_at=datetime(2026, 9, 27, 13, 10, tzinfo=UTC),
        expected_platforms=4,
        expected_endpoints=9,
        wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
    )
    _catalog, balance = snapshots()

    with pytest.raises(SourceLabInventoryInvalid, match="Wave 1 route identity"):
        build_source_performance_rows(
            scope=resolve_client_scope(run_id=f"run_drift_{mutation}"),
            catalog=catalog,
            balance=balance,
            expectation_version="socialcrawl_catalog_2026_09_27",
            wave1_identity=(
                tuple(WAVE1_ROUTE_SPECS),
                (
                    "6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6",
                    "501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a",
                ),
            ),
        )


def test_2026_09_03_catalog_matches_its_approved_expectation() -> None:
    # Vendor catalog read on 3 September 2026 at zero credit: 61 platforms and 523 routes
    # against the 55 and 449 of the day before. The expectation entry holds what
    # normalize_catalog computes from the committed fixture; nothing below is typed by hand.
    raw = json.loads(LATEST_CATALOG_FIXTURE.read_text(encoding="utf-8"))
    catalog = normalize_catalog(
        raw,
        checked_at=datetime(2026, 9, 3, 7, 0, tzinfo=UTC),
        expected_platforms=raw["data"]["stats"]["platforms"],
        expected_endpoints=raw["data"]["stats"]["endpoints"],
    )
    observed = (
        catalog.platform_count,
        catalog.route_count,
        catalog.social_platform_count,
        catalog.catalog_digest,
        catalog.normalized_content_digest,
    )
    assert observed == CATALOG_EXPECTATIONS["socialcrawl_catalog_2026_09_03"]
    assert observed[:3] == (61, 523, 29)
    _, balance = snapshots()
    scope = resolve_client_scope(run_id="source_lab_run_001")
    response = build_inventory_response(
        scope=scope,
        catalog=catalog,
        balance=balance,
        expectation_version="socialcrawl_catalog_2026_09_03",
    )
    assert response["catalog"]["expectation_version"] == "socialcrawl_catalog_2026_09_03"
    with pytest.raises(SourceLabInventoryInvalid, match="approved expectation"):
        build_inventory_response(
            scope=scope,
            catalog=catalog,
            balance=balance,
            expectation_version="socialcrawl_catalog_2026_09_02",
        )
