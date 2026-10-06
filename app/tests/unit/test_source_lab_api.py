from __future__ import annotations

import json
import hashlib
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import bq, credit_budget, deployment_contract, main, source_lab

client = TestClient(main.app)

CURRENT_DATE = date(2026, 8, 27)
CHECKED_AT = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
RUN_ID = "source_lab_2026-08-27"
BRAND_CONFIG_ID = "ogilvy_42"
THEME_ID = "ogilvy_intelligence"
APPROVED_EXPECTATION = "socialcrawl_catalog_2026_09_07"
APPROVED_DIGEST = "76358b388d4384cd548ed77e361c553a75035462b86e5b2bc9cd8676e89fd6a8"
SUPERSEDED_EXPECTATION = "socialcrawl_catalog_2026_09_04"
SUPERSEDED_DIGEST = "08cf5728a5f52d884af994e4f038cf3636ab7e1d069ee3a723e04e243ce9f127"
TIKTOK_PROFILE_ENDPOINT_ID = (
    "endpoint_23bce0075b3252e109489439f6483cb390493b9b22f4005ee0efee1e05ab3348"
)
BALANCE_ENDPOINT_ID = (
    "endpoint_4ca2e7114e29a597489cf5a505f190376817e12e4b35ccd98c4108963bc5a759"
)
TIKTOK_PROFILE_SOURCE_ID = (
    "srcperf_2bb022db8c42183178ec4a2489505ca219d30d61e9779c39afb0de25035e6582"
)
BALANCE_SOURCE_ID = (
    "srcperf_7729e4b948eecf87d28e0a2a647dc54b57aceb78fa2d55a81453190119885ff3"
)
LEGACY_TIKTOK_ENDPOINT_ID = (
    "endpoint_05600e1ac0b3ff91bdc967091f7fa118ff35b2c065605589187c56f955e2d8b3"
)
LEGACY_TIKTOK_SOURCE_ID = (
    "srcperf_ef7a1f9c2a207044f75007c670e87404883c4a5bbe21dc45d06d3f067d49ac63"
)
REPLACEMENT_ROUTE = "/v1/tiktok/profile-review-replacement"
REPLACEMENT_ENDPOINT_ID = (
    "endpoint_60f2bf41d5473367c5501604bdba2a980367acb56c1a2554d83cd3dbfcd15f1f"
)
REPLACEMENT_DIGEST = "8b624cafaf9aab08940874d62ef0cd2401023b3b89ee0286d02eb6226405f9d6"
SUPERSEDED_ROUTES_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "source_lab"
    / "approved_catalog_routes_2026_09_04.json"
)
# The vendor moved its whole catalog digests overnight while every count held.
# Between the 4 and 5 September reads it swapped one home_depot route for
# another, so the approved 5 September catalog is the 4 September routes with
# this one substitution, still 556 routes across 64 platforms.
RETIRED_ON_2026_09_05 = ("GET", "/v1/home_depot/category")
ADDED_ON_2026_09_05 = {
    "endpoint_id": (
        "endpoint_b24d9abee21a5d67c9a382d0db7c0e423330ca861aaf205891187c26b6166d8f"
    ),
    "http_method": "GET",
    "route_path": "/v1/home_depot/stores",
    "platform": "home_depot",
    "resource": "stores",
    "source_family": "socialcrawl",
}
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
PROJECTED_ROW_FIELDS = (
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


def _source_performance_id(
    method: str, route_path: str, metric_date: date = CURRENT_DATE
) -> str:
    values = (
        "ogilvy_default",
        metric_date.isoformat(),
        "socialcrawl",
        method,
        route_path,
        "global",
    )
    encoded = "".join(f"V{len(value.encode())}:{value}" for value in values)
    return "srcperf_" + hashlib.sha256(encoded.encode()).hexdigest()


@pytest.fixture(autouse=True)
def open_test_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def set_source_lab_runtime(monkeypatch) -> None:
    monkeypatch.setenv("BQ_DATASET", "trends_v2_staging")
    monkeypatch.setattr(
        main, "_validate_source_lab_runtime", lambda: None, raising=False
    )
    monkeypatch.setattr(
        main, "_source_lab_current_date", lambda: CURRENT_DATE, raising=False
    )


def fixture_routes(routes_path: Path) -> list[dict[str, object]]:
    return list(json.loads(routes_path.read_bytes())["routes"])


def approved_routes() -> list[dict[str, object]]:
    return fixture_routes(
        SUPERSEDED_ROUTES_PATH.with_name("approved_catalog_routes_2026_09_07.json")
    )


def september5_routes() -> list[dict[str, object]]:
    """Rebuild the approved 5 September catalog from the 4 September routes."""
    base = json.loads(SUPERSEDED_ROUTES_PATH.read_bytes())
    assert base["expectation_version"] == SUPERSEDED_EXPECTATION
    routes = [
        route
        for route in base["routes"]
        if (route["http_method"], route["route_path"]) != RETIRED_ON_2026_09_05
    ]
    assert len(routes) == 555
    return [*routes, dict(ADDED_ON_2026_09_05)]


def routes_digest(routes: list[dict[str, object]]) -> str:
    """Recompute the catalog digest the way the reader does, from the routes."""
    ordered = sorted((row["http_method"], row["route_path"]) for row in routes)
    return hashlib.sha256(
        "".join(f"{method} {path}\n" for method, path in ordered).encode()
    ).hexdigest()


def persisted_rows(
    *,
    routes: list[dict[str, object]] | None = None,
    catalog_digest: str = APPROVED_DIGEST,
    route_count: int = 557,
    metric_date: date = CURRENT_DATE,
    checked_at: datetime = CHECKED_AT,
    run_id: str = RUN_ID,
) -> list[dict[str, object]]:
    catalog = approved_routes() if routes is None else routes
    assert len(catalog) == route_count
    assert routes_digest(catalog) == catalog_digest
    rows = []
    for route_index, approved_route in enumerate(catalog):
        platform = approved_route["platform"]
        resource = approved_route["resource"]
        rows.append(
            {
                "contract_version": "2.1.0",
                "run_id": run_id,
                "client_scope_id": "ogilvy_default",
                "market_scope": ["za", "ng", "ke"],
                "brand_config_id": BRAND_CONFIG_ID,
                "audience_lens_ids": [],
                "theme_id": THEME_ID,
                "source_performance_id": _source_performance_id(
                    approved_route["http_method"],
                    approved_route["route_path"],
                    metric_date,
                ),
                "metric_date": metric_date,
                "vendor": "socialcrawl",
                "platform": platform,
                "resource": resource,
                "http_method": approved_route["http_method"],
                "route_path": approved_route["route_path"],
                "endpoint_id": approved_route["endpoint_id"],
                "route_role": "inventory",
                "source_family": approved_route.get("source_family", "socialcrawl"),
                "market": None,
                "status": "inventory_only",
                "catalog_digest": catalog_digest,
                "official_capability": f"Reads public {resource} data.",
                "official_parameters": ["handle"],
                "official_price_components": [],
                "official_credits": Decimal("0.5")
                if route_index == 0
                else Decimal("1"),
                "official_credits_label": "0.5 credit"
                if route_index == 0
                else "1 credit",
                "official_archetype": "Author",
                "catalog_paginated": False,
                "catalog_metered": False,
                "cache_ttl_seconds": 900,
                "delivery": None,
                "docs_url": f"https://www.socialcrawl.dev/platforms/{platform}",
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
                "last_checked_at": checked_at,
                "balance": None,
                "recent_deductions": None,
                "observed_at": None,
                "balance_read_status": None,
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
        )
    rows.append(
        {
            **{key: None for key in rows[0]},
            "contract_version": "2.1.0",
            "run_id": run_id,
            "client_scope_id": "ogilvy_default",
            "market_scope": ["za", "ng", "ke"],
            "brand_config_id": BRAND_CONFIG_ID,
            "audience_lens_ids": [],
            "theme_id": THEME_ID,
            "source_performance_id": _source_performance_id(
                "GET", "/v1/credits/balance", metric_date
            ),
            "metric_date": metric_date,
            "vendor": "socialcrawl",
            "http_method": "GET",
            "route_path": "/v1/credits/balance",
            "endpoint_id": BALANCE_ENDPOINT_ID,
            "route_role": "utility_balance",
            "source_family": "socialcrawl",
            "status": "inventory_only",
            "catalog_digest": None,
            "calls": None,
            "credits": None,
            "last_success_at": None,
            "downstream_consumers": [],
            "official_capability": "Reads the current credit balance.",
            "official_parameters": [],
            "official_price_components": [],
            "last_checked_at": checked_at,
            "balance": Decimal("250100"),
            "recent_deductions": Decimal("0"),
            "observed_at": checked_at,
            "balance_read_status": "ok",
            "funding_math_status": "unknown",
            "funded_increase_observed": False,
            "monthly_optional_credit_cap": Decimal("25000"),
            "optional_calls_enabled": False,
        }
    )
    return rows


def test_get_source_lab_returns_exact_approved_public_boundary(monkeypatch):
    set_source_lab_runtime(monkeypatch)
    monkeypatch.setattr(
        main.investigation_scopes,
        "resolve_investigation_scope",
        lambda **_kwargs: pytest.fail(
            "Source Lab called the shared investigation resolver"
        ),
    )
    monkeypatch.setattr(
        bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: persisted_rows()
    )

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 200
    assert len(response.content) == 561043
    assert hashlib.sha256(response.content).hexdigest().upper() == (
        "2DC6B8FE4E6A9987EC662A7E135EAFAFD075CB11EE16DFFF070BC5477FD02B40"
    )
    payload = response.json()
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
    assert payload["brand_config_id"] == BRAND_CONFIG_ID
    assert payload["theme_id"] == THEME_ID
    assert payload["run_id"] == RUN_ID
    assert payload["catalog"] == {
        "expectation_version": APPROVED_EXPECTATION,
        "platform_count": 64,
        "route_count": 557,
        "catalog_digest": APPROVED_DIGEST,
        "checked_at": "2026-08-27T18:00:00Z",
        "completeness_state": "verified_snapshot",
    }
    assert payload["funding"] == {
        "balance": 250100,
        "recent_deductions": 0,
        "observed_at": "2026-08-27T18:00:00Z",
        "funding_math_status": "unknown",
        "funded_increase_observed": False,
        "selected_top_up_credits": None,
        "monthly_optional_credit_cap": 25000,
        "monthly_optional_credits_used": None,
        "optional_calls_enabled": False,
    }
    assert len(payload["sources"]) == 557
    first = payload["sources"][0]
    assert tuple(first) == SOURCE_FIELDS
    assert first["official_credits"] == 0.5
    assert first["last_checked_at"] == "2026-08-27T18:00:00Z"
    tiktok_profile = next(
        source
        for source in payload["sources"]
        if source["route_path"] == "/v1/tiktok/profile"
    )
    assert tiktok_profile["endpoint_id"] == TIKTOK_PROFILE_ENDPOINT_ID
    assert all(source["calls"] is None for source in payload["sources"])
    assert all(source["downstream_consumers"] == [] for source in payload["sources"])


def test_get_source_lab_2_2_appends_only_exact_credit_budget(monkeypatch):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    budget = credit_budget.project_credit_budget(
        {
            "credential_lane": "ogilvy_funded",
            "funding_account": "ogilvy_albert",
            "activation_stage": 0,
            "opening_balance": Decimal("250100"),
            "current_balance": Decimal("250100"),
            "month_opening_balance": Decimal("250100"),
            "balance_observed_at": datetime(2026, 8, 30, 7, 0, tzinfo=UTC),
            "kill_state": "not_tested",
            "ledger_rows": [],
        },
        now=datetime(2026, 8, 30, 7, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)
    monkeypatch.setattr(credit_budget, "read_credit_budget", lambda: budget)

    v21 = client.get("/api/v2/source-lab?contract_version=2.1.0")
    v22 = client.get("/api/v2/source-lab?contract_version=2.2.0")

    assert v21.status_code == 200
    assert v22.status_code == 200
    payload21 = v21.json()
    payload22 = v22.json()
    assert tuple(payload22) == (*tuple(payload21), "credit_budget")
    assert payload22["contract_version"] == "2.2.0"
    assert payload22["resource_version"] == "source_lab_inventory_v2"
    assert {
        key: value
        for key, value in payload22.items()
        if key not in {"contract_version", "resource_version", "credit_budget"}
    } == {
        key: value
        for key, value in payload21.items()
        if key not in {"contract_version", "resource_version"}
    }
    assert payload22["credit_budget"] == source_lab.json_ready(budget)
    assert "credit_budget" not in payload21


def test_verified_engine_identifier_literals_are_projected_unchanged():
    rows = persisted_rows()
    tiktok = next(row for row in rows if row["route_path"] == "/v1/tiktok/profile")
    balance = rows[-1]

    assert tiktok["endpoint_id"] == TIKTOK_PROFILE_ENDPOINT_ID
    assert tiktok["source_performance_id"] == TIKTOK_PROFILE_SOURCE_ID
    assert balance["endpoint_id"] == BALANCE_ENDPOINT_ID
    assert balance["source_performance_id"] == BALANCE_SOURCE_ID


def test_source_lab_fixed_scope_is_exact_and_independent():
    scope = source_lab.fixed_scope()

    assert (
        scope.client_scope_id,
        scope.market_scope,
        scope.brand_config_id,
        scope.audience_lens_ids,
        scope.theme_id,
    ) == (
        "ogilvy_default",
        ("za", "ng", "ke"),
        BRAND_CONFIG_ID,
        (),
        THEME_ID,
    )


def test_get_source_lab_uses_approved_nonrevealing_errors(monkeypatch):
    set_source_lab_runtime(monkeypatch)
    wrong_version = client.get("/api/v2/source-lab?contract_version=2.0.0")
    assert wrong_version.status_code == 400
    assert wrong_version.json()["detail"] == {
        "code": "contract_version_unsupported",
        "message": "Source Lab contract version is unsupported.",
    }

    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: [])
    unavailable = client.get("/api/v2/source-lab?contract_version=2.1.0")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == {
        "code": "source_lab_unavailable",
        "message": "Source Lab is unavailable for this scope.",
    }


# The status case here used to be active, from the cut where every route the
# engine wrote was catalogue only. The engine wires routes now and writes the
# six settled statuses on the same inventory row, so active is a run this
# reader accepts and a word outside the six is the inconsistency to hold.
@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows.pop(0),
        lambda rows: rows.__setitem__(1, dict(rows[0])),
        lambda rows: rows.pop(),
        lambda rows: rows[0].__setitem__("catalog_digest", "f" * 64),
        lambda rows: rows[0].__setitem__("client_scope_id", "other_scope"),
        lambda rows: rows[0].__setitem__("status", "quarantined_pending_review"),
        lambda rows: rows[0].__setitem__("calls", 0),
        lambda rows: rows.append(dict(rows[0])),
        lambda rows: rows[-1].__setitem__("optional_calls_enabled", True),
        lambda rows: rows[0].__setitem__("official_credits", "$0.50"),
        lambda rows: rows[0].__setitem__("official_credits_label", None),
        lambda rows: rows[0].__setitem__("official_archetype", None),
        lambda rows: rows[0].__setitem__("delivery", 1),
        lambda rows: rows[0].__setitem__("docs_url", "https://example.com/catalog"),
    ],
)
def test_get_source_lab_fails_closed_on_partial_or_inconsistent_run(
    monkeypatch, mutate
):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    mutate(rows)
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "catalog_snapshot_invalid",
        "message": "Source Lab is unavailable for this scope.",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows[0].__setitem__("audience_lens_ids", None),
        lambda rows: rows[0].pop("audience_lens_ids"),
        lambda rows: rows[0].__setitem__(
            "source_performance_id", "srcperf_" + "0" * 64
        ),
        lambda rows: rows[0].pop("source_performance_id"),
        lambda rows: rows[0].__setitem__(
            "official_price_components", [{"currency": "USD"}]
        ),
        lambda rows: rows[0].__setitem__("balance_read_status", "ok"),
        lambda rows: rows[0].__setitem__(
            "deductions_interval", {"start_at": CHECKED_AT}
        ),
        lambda rows: rows[0].__setitem__("funded_increase_amount", Decimal("1")),
        lambda rows: rows[0].__setitem__("baseline_credits_per_day", Decimal("1")),
        lambda rows: rows[0].__setitem__("baseline_window", {"completed_days": 1}),
        lambda rows: rows[0].__setitem__("runway_days", Decimal("1")),
        lambda rows: rows[0].__setitem__("unexpected_projected_field", None),
    ],
)
def test_projected_engine_row_contract_fails_closed_on_mutation(monkeypatch, mutate):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    mutate(rows)
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_snapshot_invalid"


@pytest.mark.parametrize(
    ("field", "legacy_value"),
    [
        ("endpoint_id", LEGACY_TIKTOK_ENDPOINT_ID),
        ("source_performance_id", LEGACY_TIKTOK_SOURCE_ID),
    ],
)
def test_legacy_no_v_identifiers_fail_closed(monkeypatch, field, legacy_value):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    tiktok = next(row for row in rows if row["route_path"] == "/v1/tiktok/profile")
    tiktok[field] = legacy_value
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_snapshot_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "active"),
        ("catalog_digest", APPROVED_DIGEST),
        ("calls", 1),
        ("credits", Decimal("0")),
        ("last_success_at", CHECKED_AT),
        ("observed_at", datetime(2026, 8, 27, 17, 59, tzinfo=UTC)),
        ("balance", None),
        ("optional_calls_enabled", True),
    ],
)
def test_engine_balance_control_row_refuses_noncanonical_mutations(
    monkeypatch, field, value
):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    rows[-1][field] = value
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_snapshot_invalid"


def test_balance_control_row_never_leaks_into_sources(monkeypatch):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    rows[-1]["route_role"] = "inventory"
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_snapshot_invalid"


def test_expectation_version_is_the_exact_approved_contract():
    assert source_lab.EXPECTATION_VERSION == APPROVED_EXPECTATION


def _candidate(
    run_id: str,
    *,
    metric_date: date = CURRENT_DATE,
    client_scope_id: str = "ogilvy_default",
    contract_version: str = "2.1.0",
    vendor: str = "socialcrawl",
    inventory_count: int = 557,
    balance_count: int = 1,
    row_count: int = 558,
    snapshot_at: datetime = CHECKED_AT,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "metric_date": metric_date,
        "client_scope_id": client_scope_id,
        "contract_version": contract_version,
        "vendor": vendor,
        "inventory_count": inventory_count,
        "balance_count": balance_count,
        "row_count": row_count,
        "snapshot_at": snapshot_at,
        "catalog_digest": APPROVED_DIGEST,
    }


def test_bq_reader_selects_current_complete_run_from_controlled_candidates(monkeypatch):
    selected_parameters = []
    queries = []

    def run_query(sql, params=None, **_kwargs):
        queries.append(sql)
        values = {name: value for name, _type, value in params or []}
        if "run_id" not in values:
            return [
                _candidate("old_complete", metric_date=date(2026, 8, 26)),
                _candidate("cross_scope", client_scope_id="other_scope"),
                _candidate("wrong_contract", contract_version="2.0.0"),
                _candidate(RUN_ID),
            ]
        selected_parameters.append(values)
        return persisted_rows() if values["run_id"] == RUN_ID else []

    monkeypatch.setattr(bq, "_run_query", run_query)

    rows = bq.fetch_source_lab_rows(
        "ogilvy_default",
        current_metric_date=CURRENT_DATE,
        dataset="trends_v2_staging",
    )

    assert len(rows) == 558
    assert selected_parameters == [
        {
            "client_scope_id": "ogilvy_default",
            "run_id": RUN_ID,
            "metric_date": CURRENT_DATE,
        }
    ]
    projected = ", ".join(
        f"`{field}`" if field == "rows" else field for field in PROJECTED_ROW_FIELDS
    )
    assert queries[1].startswith(f"SELECT {projected} FROM ")


def test_bq_reader_skips_newer_partial_run_for_complete_snapshot(monkeypatch):
    calls = []

    def run_query(_sql, params=None, **_kwargs):
        calls.append(params)
        if any(name == "run_id" for name, _type, _value in params or []):
            assert (
                dict((name, value) for name, _type, value in params)["run_id"]
                == "complete"
            )
            return [{"retained": True}]
        return [
            _candidate(
                "complete", snapshot_at=datetime(2026, 8, 27, 17, 0, tzinfo=UTC)
            ),
            _candidate("newer_partial", inventory_count=448),
        ]

    monkeypatch.setattr(bq, "_run_query", run_query)

    assert bq.fetch_source_lab_rows(
        "ogilvy_default",
        current_metric_date=CURRENT_DATE,
        dataset="trends_v2_staging",
    ) == [{"retained": True}]
    assert len(calls) == 2


def test_bq_reader_returns_prior_complete_run_with_its_original_date(monkeypatch):
    selected = []

    def run_query(sql, params=None, **_kwargs):
        values = {name: value for name, _type, value in params or []}
        if "run_id" not in values:
            return [_candidate(RUN_ID, metric_date=date(2026, 8, 26))]
        selected.append(values)
        return [{"retained": True}]

    monkeypatch.setattr(
        bq,
        "_run_query",
        run_query,
    )

    assert bq.fetch_source_lab_rows(
        "ogilvy_default",
        current_metric_date=CURRENT_DATE,
        dataset="trends_v2_staging",
    ) == [{"retained": True}]
    assert selected[0]["metric_date"] == date(2026, 8, 26)


def test_previous_day_snapshot_keeps_its_actual_catalog_timestamp():
    rows = persisted_rows()
    result = source_lab.build_response(
        scope=source_lab.fixed_scope(),
        rows=rows,
        current_metric_date=CURRENT_DATE.replace(day=28),
    )
    assert result["catalog"]["checked_at"] == "2026-08-27T18:00:00Z"


def test_reader_binds_exact_catalog_profiles_and_excludes_future_rows(monkeypatch):
    queries = []
    newest = _candidate(
        "source_lab_2026-09-08",
        metric_date=date(2026, 9, 8),
        inventory_count=571,
        row_count=572,
    )
    newest["catalog_digest"] = (
        "5c51442cf52165f78a3f11925e47fd88f95bc85fcfadbde6aab3052c892112aa"
    )
    future = {**newest, "metric_date": date(2026, 9, 10)}
    wrong_count = {**newest, "inventory_count": 557, "row_count": 558}
    assert (
        bq._select_source_lab_candidate(
            [future, wrong_count, newest],
            client_scope_id="ogilvy_default",
            current_metric_date=date(2026, 9, 9),
        )
        == newest
    )
    assert (
        bq._select_source_lab_candidate(
            [future, wrong_count],
            client_scope_id="ogilvy_default",
            current_metric_date=date(2026, 9, 9),
        )
        is None
    )
    monkeypatch.setattr(
        bq, "_run_query", lambda sql, params: queries.append((sql, params)) or []
    )
    bq.fetch_source_lab_rows(
        "ogilvy_default",
        current_metric_date=date(2026, 9, 9),
        dataset="trends_v2_staging",
    )
    sql, params = queries[0]
    assert "metric_date <= @current_metric_date" in sql
    assert "catalog_count = 1" in sql and "balance_count = 1" in sql
    assert "inventory_count = 571 AND row_count = 572" in sql
    assert "inventory_count = 557 AND row_count = 558" in sql
    assert params == [
        ("client_scope_id", "STRING", "ogilvy_default"),
        ("current_metric_date", "DATE", date(2026, 9, 9)),
    ]


def test_september8_catalog_from_retained_native_rows_renders_with_original_date():
    catalog = json.loads(
        SUPERSEDED_ROUTES_PATH.with_name(
            "approved_catalog_routes_2026_09_08.json"
        ).read_bytes()
    )
    assert (
        routes_digest(catalog["routes"])
        == catalog["catalog_digest"]
        == "5c51442cf52165f78a3f11925e47fd88f95bc85fcfadbde6aab3052c892112aa"
    )
    rows = persisted_rows(
        routes=catalog["routes"],
        catalog_digest=catalog["catalog_digest"],
        route_count=571,
        metric_date=date(2026, 9, 8),
        checked_at=datetime(2026, 9, 8, 9, 4, 46, 247696, tzinfo=UTC),
        run_id="source_lab_2026-09-08",
    )
    result = source_lab.build_response(
        scope=source_lab.fixed_scope(), rows=rows, current_metric_date=date(2026, 9, 9)
    )
    assert result["catalog"]["route_count"] == 571
    assert result["catalog"]["platform_count"] == 65
    assert result["catalog"]["checked_at"] == "2026-09-08T09:04:46.247696Z"
    rows[0]["route_path"] += "-altered"
    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=date(2026, 9, 9),
        )


def test_coherent_replacement_catalog_is_not_the_approved_expectation(monkeypatch):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    target = next(row for row in rows if row["route_path"] == "/v1/tiktok/profile")
    target["route_path"] = REPLACEMENT_ROUTE
    target["resource"] = "profile-review-replacement"
    target["endpoint_id"] = REPLACEMENT_ENDPOINT_ID
    for row in rows:
        row["catalog_digest"] = REPLACEMENT_DIGEST
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_snapshot_invalid"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows[-1].__setitem__("run_id", "other_run"),
        lambda rows: rows[-1].__setitem__("client_scope_id", "other_scope"),
        lambda rows: rows[-1].__setitem__("contract_version", "2.0.0"),
        lambda rows: rows[-1].__setitem__("metric_date", date(2026, 8, 26)),
        lambda rows: rows[-1].__setitem__("catalog_digest", "0" * 64),
        lambda rows: rows[-1].__setitem__(
            "observed_at", datetime(2026, 8, 27, 17, 59, tzinfo=UTC)
        ),
        lambda rows: rows[-1].__setitem__(
            "last_checked_at", datetime(2026, 8, 27, 17, 59, tzinfo=UTC)
        ),
        lambda rows: rows[-1].__setitem__(
            "last_success_at", datetime(2026, 8, 27, 17, 59, tzinfo=UTC)
        ),
    ],
)
def test_balance_and_inventory_must_share_one_snapshot(monkeypatch, mutate):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    mutate(rows)
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_snapshot_invalid"


def test_prior_metric_date_is_not_admitted_as_current(monkeypatch):
    set_source_lab_runtime(monkeypatch)
    rows = persisted_rows()
    for row in rows:
        row["metric_date"] = date(2026, 8, 26)
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_snapshot_invalid"


def test_source_lab_runtime_requires_exact_profile_and_dataset(monkeypatch):
    assert hasattr(main, "_validate_source_lab_runtime")
    monkeypatch.setattr(main, "_RUNTIME_PROFILE_AT_STARTUP", "production")
    monkeypatch.setenv("BQ_DATASET", "trends_v2_staging")
    with pytest.raises(deployment_contract.DeploymentContractError):
        main._validate_source_lab_runtime()

    monkeypatch.setattr(
        main, "_RUNTIME_PROFILE_AT_STARTUP", "open-intelligence-staging"
    )
    monkeypatch.setenv("BQ_DATASET", "trends_v2")
    with pytest.raises(deployment_contract.DeploymentContractError):
        main._validate_source_lab_runtime()


def test_source_lab_runtime_failure_prevents_query(monkeypatch):
    monkeypatch.setattr(
        main, "_source_lab_current_date", lambda: CURRENT_DATE, raising=False
    )
    monkeypatch.setattr(
        main,
        "_validate_source_lab_runtime",
        lambda: (_ for _ in ()).throw(
            deployment_contract.DeploymentContractError("wrong runtime")
        ),
        raising=False,
    )
    queried = []
    monkeypatch.setattr(
        bq, "fetch_source_lab_rows", lambda *_args, **_kwargs: queried.append(True)
    )

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "source_lab_unavailable"
    assert queried == []


def test_empty_run_identity_is_rejected_by_reader_and_response(monkeypatch):
    set_source_lab_runtime(monkeypatch)
    reader = bq.fetch_source_lab_rows
    outcomes = []

    for run_id in ("", "   "):
        calls = []

        def run_query(_sql, params=None, **_kwargs):
            calls.append(params)
            if len(calls) == 1:
                return [_candidate(run_id)]
            return persisted_rows()

        monkeypatch.setattr(bq, "_run_query", run_query)
        reader_rows = reader(
            "ogilvy_default",
            current_metric_date=CURRENT_DATE,
            dataset="trends_v2_staging",
        )
        rows = persisted_rows()
        for row in rows:
            row["run_id"] = run_id
        monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda *_args, **_kwargs: rows)
        response = client.get("/api/v2/source-lab?contract_version=2.1.0")
        response_body = response.json()
        monkeypatch.setattr(bq, "fetch_source_lab_rows", reader)
        outcomes.append(
            {
                "run_id": run_id,
                "reader_rows": reader_rows,
                "reader_query_count": len(calls),
                "status": response.status_code,
                "code": response_body.get("detail", {}).get("code"),
            }
        )

    assert outcomes == [
        {
            "run_id": "",
            "reader_rows": [],
            "reader_query_count": 1,
            "status": 503,
            "code": "catalog_snapshot_invalid",
        },
        {
            "run_id": "   ",
            "reader_rows": [],
            "reader_query_count": 1,
            "status": 503,
            "code": "catalog_snapshot_invalid",
        },
    ]


PRIOR_DIGEST = "f843d6ad934e36b005090c1c642eb4b8e920bf2ca5d8828d7b9c1e7e4f143daa"
PRIOR_ROUTES_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "source_lab"
    / "approved_catalog_routes_2026_08_27.json"
)
NEXT_EXPECTATION = APPROVED_EXPECTATION
NEXT_DIGEST = APPROVED_DIGEST
NEXT_DATE = date(2026, 9, 7)
NEXT_RUN_ID = "source_lab_2026-09-07"
NEXT_CHECKED_AT = datetime(2026, 9, 7, 7, 29, tzinfo=UTC)


def next_snapshot_rows() -> list[dict[str, object]]:
    return persisted_rows(
        routes=approved_routes(),
        catalog_digest=NEXT_DIGEST,
        route_count=557,
        metric_date=NEXT_DATE,
        checked_at=NEXT_CHECKED_AT,
        run_id=NEXT_RUN_ID,
    )


def superseded_snapshot_rows() -> list[dict[str, object]]:
    return persisted_rows(
        routes=fixture_routes(SUPERSEDED_ROUTES_PATH),
        catalog_digest=SUPERSEDED_DIGEST,
        route_count=556,
        metric_date=NEXT_DATE,
        checked_at=NEXT_CHECKED_AT,
        run_id=NEXT_RUN_ID,
    )


def test_build_response_rejects_the_superseded_2026_09_04_catalog():
    rows = superseded_snapshot_rows()
    assert len(rows) == 557
    assert len({row["platform"] for row in rows if row["platform"]}) == 64

    with pytest.raises(source_lab.CatalogSnapshotInvalid) as rejected:
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )

    assert str(rejected.value) == "catalog_snapshot_invalid"
    assert source_lab.APPROVED_CATALOG_DIGEST != SUPERSEDED_DIGEST


def test_build_response_accepts_the_2026_09_07_staging_snapshot():
    rows = next_snapshot_rows()
    assert len(rows) == 558
    assert len([row for row in rows if row["route_role"] == "inventory"]) == 557
    assert len([row for row in rows if row["route_role"] == "utility_balance"]) == 1
    assert len({row["platform"] for row in rows if row["platform"]}) == 64

    payload = source_lab.build_response(
        scope=source_lab.fixed_scope(),
        rows=rows,
        current_metric_date=NEXT_DATE,
    )

    assert payload["run_id"] == NEXT_RUN_ID
    assert payload["catalog"]["expectation_version"] == NEXT_EXPECTATION
    assert payload["catalog"]["platform_count"] == 64
    assert payload["catalog"]["route_count"] == 557
    assert payload["catalog"]["catalog_digest"] == NEXT_DIGEST
    assert payload["catalog"]["checked_at"] == "2026-09-07T07:29:00Z"
    assert payload["catalog"]["completeness_state"] == "verified_snapshot"
    assert len(payload["sources"]) == 557


def test_build_response_rejects_the_superseded_2026_09_05_catalog():
    rows = persisted_rows(
        routes=september5_routes(),
        catalog_digest="2424a5760fb55b44b78971875b64a6c176fcd34d95de83b2ad158a3b866c0a34",
        route_count=556,
        metric_date=NEXT_DATE,
        checked_at=NEXT_CHECKED_AT,
        run_id=NEXT_RUN_ID,
    )
    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(), rows=rows, current_metric_date=NEXT_DATE
        )


def prior_snapshot_rows() -> list[dict[str, object]]:
    return persisted_rows(
        routes=fixture_routes(PRIOR_ROUTES_PATH),
        catalog_digest=PRIOR_DIGEST,
        route_count=400,
    )


def test_build_response_rejects_the_superseded_2026_08_27_snapshot():
    rows = prior_snapshot_rows()
    assert len(rows) == 401

    with pytest.raises(source_lab.CatalogSnapshotInvalid) as rejected:
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=CURRENT_DATE,
        )

    assert str(rejected.value) == "catalog_snapshot_invalid"


def test_build_response_rejects_the_superseded_digest_on_a_current_snapshot():
    rows = next_snapshot_rows()
    for row in rows:
        if row["route_role"] == "inventory":
            row["catalog_digest"] = PRIOR_DIGEST

    with pytest.raises(source_lab.CatalogSnapshotInvalid) as rejected:
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )

    assert str(rejected.value) == "catalog_snapshot_invalid"


def test_bq_reader_projects_the_whole_run_with_one_row_of_detection_margin(monkeypatch):
    queries = []

    def run_query(sql, params=None, **_kwargs):
        queries.append(sql)
        values = {name: value for name, _type, value in params or []}
        if "run_id" not in values:
            return [_candidate(RUN_ID)]
        return persisted_rows()

    monkeypatch.setattr(bq, "_run_query", run_query)

    rows = bq.fetch_source_lab_rows(
        "ogilvy_default",
        current_metric_date=CURRENT_DATE,
        dataset="trends_v2_staging",
    )

    assert len(rows) == source_lab.EXPECTED_ROUTE_COUNT + 1
    assert queries[1].endswith(f"LIMIT {source_lab.EXPECTED_ROUTE_COUNT + 2}")


WAVE1_CHANNEL_BY_ROUTE = {
    "/v1/tiktok/song": "short_video",
    "/v1/tiktok/song/videos": "short_video",
    "/v1/instagram/music/trending": "short_video",
    "/v1/instagram/audio/reels": "short_video",
    "/v1/instagram/search/reels": "short_video",
    "/v1/youtube/shorts/trending": "youtube",
    "/v1/youtube/video/comments": "youtube",
    "/v1/reddit/post/comments": "reddit",
}


def test_build_response_carries_the_wave_one_channel_families(monkeypatch):
    rows = next_snapshot_rows()
    reels = next(
        row for row in rows if row["route_path"] == "/v1/instagram/audio/reels"
    )
    assert reels["source_family"] == "short_video"

    payload = source_lab.build_response(
        scope=source_lab.fixed_scope(),
        rows=rows,
        current_metric_date=NEXT_DATE,
    )

    families = {
        source["route_path"]: source["source_family"] for source in payload["sources"]
    }
    assert {
        route: families[route] for route in WAVE1_CHANNEL_BY_ROUTE
    } == WAVE1_CHANNEL_BY_ROUTE
    assert families["/v1/tiktok/profile"] == "socialcrawl"
    counted = Counter(source["source_family"] for source in payload["sources"])
    assert sorted(counted.items()) == [
        ("reddit", 1),
        ("short_video", 5),
        ("socialcrawl", 549),
        ("youtube", 2),
    ]


def test_build_response_rejects_a_channel_family_on_an_unmapped_route():
    rows = next_snapshot_rows()
    unmapped = next(row for row in rows if row["route_path"] == "/v1/tiktok/profile")
    unmapped["source_family"] = "short_video"

    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )


def test_build_response_rejects_the_vendor_family_on_a_mapped_route():
    rows = next_snapshot_rows()
    mapped = next(
        row for row in rows if row["route_path"] == "/v1/instagram/audio/reels"
    )
    mapped["source_family"] = "socialcrawl"

    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )


# The engine promoted two Wave 1 routes on 5 September 2026. Both keep
# route_role inventory, because inventory and utility_balance are the only two
# roles the staging writer has ever written, and they carry the engine's own
# status alongside the yield, kill test and downstream consumers it measured.
# The values below are copied from
# ogilvy-trends-v2.trends_v2_staging.source_performance_daily_v2, run
# source_lab_2026-09-07, verified on 7 September 2026.
PROMOTED_ROUTES = {
    "/v1/instagram/search/reels": {"rows": 90, "unique_lift": 93.0},
    "/v1/youtube/video/comments": {"rows": 356, "unique_lift": 360.0},
}
PROMOTED_CONSUMERS = ["signal_candidates_v2", "signal_evidence_v2"]


def promoted_snapshot_rows() -> list[dict[str, object]]:
    rows = next_snapshot_rows()
    for row in rows:
        measured = PROMOTED_ROUTES.get(row["route_path"])
        if measured is None or row["route_role"] != "inventory":
            continue
        row["status"] = "active"
        row["rows"] = measured["rows"]
        row["unique_lift"] = measured["unique_lift"]
        row["kill_test_result"] = "passed"
        row["downstream_consumers"] = list(PROMOTED_CONSUMERS)
    return rows


def test_build_response_accepts_the_wired_wave_one_routes_the_engine_promoted():
    rows = promoted_snapshot_rows()
    promoted = [row for row in rows if row["status"] == "active"]
    assert len(promoted) == 2
    assert all(row["route_role"] == "inventory" for row in promoted)

    payload = source_lab.build_response(
        scope=source_lab.fixed_scope(),
        rows=rows,
        current_metric_date=NEXT_DATE,
    )

    assert len(payload["sources"]) == 557
    assert payload["catalog"]["catalog_digest"] == NEXT_DIGEST
    listed = {source["route_path"]: source for source in payload["sources"]}
    reels = listed["/v1/instagram/search/reels"]
    assert reels["status"] == "active"
    assert reels["rows"] == 90
    assert reels["unique_lift"] == 93.0
    assert reels["kill_test_result"] == "passed"
    assert reels["kill_test_state"] == "passed"
    assert reels["yield_state"] == "measured"
    assert reels["downstream_consumers"] == PROMOTED_CONSUMERS
    assert reels["calls"] is None
    assert reels["credits"] is None
    assert reels["integrity"] is None
    assert reels["cost_state"] == "catalog_only"
    assert reels["quality_state"] == "unmeasured"
    assert tuple(reels) == SOURCE_FIELDS
    comments = listed["/v1/youtube/video/comments"]
    assert comments["rows"] == 356
    assert comments["unique_lift"] == 360.0
    catalogue = listed["/v1/tiktok/profile"]
    assert catalogue["status"] == "inventory_only"
    assert catalogue["rows"] is None
    assert catalogue["yield_state"] == "unmeasured"
    assert catalogue["kill_test_state"] == "not_run"
    assert catalogue["downstream_consumers"] == []


def test_a_promoted_route_reaches_the_endpoint_as_a_measured_source(monkeypatch):
    set_source_lab_runtime(monkeypatch)
    monkeypatch.setattr(
        main, "_source_lab_current_date", lambda: NEXT_DATE, raising=False
    )
    monkeypatch.setattr(
        bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: promoted_snapshot_rows()
    )

    response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 200
    sources = response.json()["sources"]
    measured = [source for source in sources if source["status"] == "active"]
    assert [source["route_path"] for source in measured] == [
        "/v1/instagram/search/reels",
        "/v1/youtube/video/comments",
    ]
    assert [source["rows"] for source in measured] == [90, 356]


@pytest.mark.parametrize(
    "field,value",
    [
        ("rows", 12),
        ("unique_lift", 1.5),
        ("kill_test_result", "passed"),
        ("blocking_reason", "kill test refused this route"),
        ("last_success_at", datetime(2026, 9, 5, 6, 0, tzinfo=UTC)),
    ],
)
def test_an_unwired_route_carrying_a_measurement_is_refused(field, value):
    rows = next_snapshot_rows()
    unwired = next(row for row in rows if row["route_path"] == "/v1/tiktok/profile")
    assert unwired["status"] == "inventory_only"
    unwired[field] = value

    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )


def test_an_unwired_route_carrying_a_downstream_consumer_is_refused():
    rows = next_snapshot_rows()
    unwired = next(row for row in rows if row["route_path"] == "/v1/tiktok/profile")
    unwired["downstream_consumers"] = ["signal_evidence_v2"]

    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )


@pytest.mark.parametrize(
    "status", ["quarantined_pending_review", "rejected", "", None, "ACTIVE"]
)
def test_a_status_outside_the_settled_six_is_refused(status):
    rows = promoted_snapshot_rows()
    promoted = next(
        row for row in rows if row["route_path"] == "/v1/youtube/video/comments"
    )
    promoted["status"] = status

    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("rows", -1),
        ("rows", 3.5),
        ("rows", True),
        ("rows", "90"),
        ("unique_lift", -0.5),
        ("integrity", 1.5),
        ("calls", -2),
        ("credits", Decimal("-1")),
        ("kill_test_result", ""),
        ("blocking_reason", 7),
        ("last_success_at", datetime(2026, 9, 5, 6, 0)),
        ("review_date", "2026-09-05"),
        ("downstream_consumers", ["signal_evidence_v2", "signal_evidence_v2"]),
        ("downstream_consumers", [""]),
        ("downstream_consumers", "signal_evidence_v2"),
    ],
)
def test_a_promoted_route_with_an_out_of_contract_measurement_is_refused(field, value):
    rows = promoted_snapshot_rows()
    promoted = next(
        row for row in rows if row["route_path"] == "/v1/youtube/video/comments"
    )
    promoted[field] = value

    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("balance", Decimal("1")),
        ("optional_calls_enabled", False),
        ("runway_days", 4),
    ],
)
def test_a_funding_column_on_an_inventory_row_is_still_refused(field, value):
    rows = promoted_snapshot_rows()
    promoted = next(
        row for row in rows if row["route_path"] == "/v1/youtube/video/comments"
    )
    promoted[field] = value

    with pytest.raises(source_lab.CatalogSnapshotInvalid):
        source_lab.build_response(
            scope=source_lab.fixed_scope(),
            rows=rows,
            current_metric_date=NEXT_DATE,
        )


def test_a_refusal_names_the_guard_and_the_values_at_error(caplog):
    rows = next_snapshot_rows()
    unreadable = next(
        row for row in rows if row["route_path"] == "/v1/youtube/video/comments"
    )
    unreadable["status"] = "quarantined_pending_review"

    with caplog.at_level("ERROR", logger="listening_post.api.source_lab"):
        with pytest.raises(source_lab.CatalogSnapshotInvalid):
            source_lab.build_response(
                scope=source_lab.fixed_scope(),
                rows=rows,
                current_metric_date=NEXT_DATE,
            )

    refusals = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(refusals) == 1
    message = refusals[0].getMessage()
    assert "guard=inventory.route_status_unknown" in message
    assert "quarantined_pending_review" in message
    assert "/v1/youtube/video/comments" in message


@pytest.mark.parametrize(
    "mutate,guard",
    [
        (lambda rows: rows.pop(), "response.row_count"),
        (
            lambda rows: rows[0].__setitem__("catalog_digest", PRIOR_DIGEST),
            "response.catalog_digest_cardinality",
        ),
        (
            lambda rows: rows[0].__setitem__("theme_id", "other_theme"),
            "response.row_scope",
        ),
        (
            lambda rows: rows[0].__setitem__("run_id", "source_lab_other"),
            "response.run_identity",
        ),
        (
            lambda rows: rows[0].__setitem__("docs_url", "http://socialcrawl.dev/x"),
            "inventory.docs_url_scheme",
        ),
        (
            lambda rows: rows[0].__setitem__("endpoint_id", BALANCE_ENDPOINT_ID),
            "inventory.endpoint_id_mismatch",
        ),
    ],
)
def test_every_refusal_path_names_its_own_guard(caplog, mutate, guard):
    rows = next_snapshot_rows()
    mutate(rows)

    with caplog.at_level("ERROR", logger="listening_post.api.source_lab"):
        with pytest.raises(source_lab.CatalogSnapshotInvalid):
            source_lab.build_response(
                scope=source_lab.fixed_scope(),
                rows=rows,
                current_metric_date=NEXT_DATE,
            )

    messages = [record.getMessage() for record in caplog.records]
    assert any(f"guard={guard}" in message for message in messages), messages


def test_an_empty_run_logs_the_unavailable_guard(caplog):
    with caplog.at_level("ERROR", logger="listening_post.api.source_lab"):
        with pytest.raises(source_lab.SourceLabUnavailable):
            source_lab.build_response(
                scope=source_lab.fixed_scope(),
                rows=[],
                current_metric_date=NEXT_DATE,
            )

    messages = [record.getMessage() for record in caplog.records]
    assert any("guard=response.no_rows" in message for message in messages), messages


def test_the_handler_logs_the_refused_code_without_the_payload(monkeypatch, caplog):
    set_source_lab_runtime(monkeypatch)
    monkeypatch.setattr(
        main, "_source_lab_current_date", lambda: NEXT_DATE, raising=False
    )
    rows = next_snapshot_rows()
    rows[0]["status"] = "quarantined_pending_review"
    monkeypatch.setattr(bq, "fetch_source_lab_rows", lambda _scope, **_kwargs: rows)

    with caplog.at_level("ERROR"):
        response = client.get("/api/v2/source-lab?contract_version=2.1.0")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "catalog_snapshot_invalid"
    handler = [
        record.getMessage()
        for record in caplog.records
        if record.name == "listening_post.api"
    ]
    assert any("catalog_snapshot_invalid" in message for message in handler), handler
    assert not any("passcode" in message.lower() for message in handler), handler
