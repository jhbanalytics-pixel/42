"""Per-market and per-phase SocialCrawl credit attribution."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests
from src.analysis.open_intelligence.funded_lane import MonthlySpendSnapshot, evaluate_funded_lane
from src.analysis.open_intelligence.funded_lane_runtime import FundedLaneRuntime, _rules
from src.ingestion.connectors.socialcrawl import (
    PHASE_ORDER,
    FundedSocialCrawlContext,
    SocialCrawlConnector,
    SocialCrawlPhaseUsage,
)


def connector(market="za"):
    item = SocialCrawlConnector(market=market)
    item._budget = 100
    item._phase_share = dict.fromkeys(PHASE_ORDER, 1.0)
    item._timeout = 45
    return item


def response(*, credits_used, success=True):
    item = MagicMock()
    item.json.return_value = {
        "success": success,
        "credits_used": credits_used,
        "data": {"items": []},
    }
    return item


def test_success_records_attempt_budget_debit_and_vendor_credit() -> None:
    SocialCrawlConnector.reset_credits()
    session = MagicMock()
    session.get.return_value = response(credits_used=1)

    connector()._call(session, "tiktok/search/top", {"query": "amapiano"}, "search")

    assert SocialCrawlConnector.usage_for("za", "search") == SocialCrawlPhaseUsage(
        market="za",
        phase="search",
        calls=1,
        budget_debit_credits=1,
        vendor_reported_credits=1,
    )


def test_transport_failure_debits_breaker_without_inventing_vendor_spend() -> None:
    SocialCrawlConnector.reset_credits()
    session = MagicMock()
    session.get.side_effect = requests.Timeout("upstream timeout")

    connector()._call(session, "instagram/search/hashtag", {"hashtag": "amapiano"}, "search")

    usage = SocialCrawlConnector.usage_for("za", "search")
    assert usage.calls == 1
    assert usage.budget_debit_credits == 5
    assert usage.vendor_reported_credits == 0
    assert SocialCrawlConnector.credits_used() == 5


def test_cache_hit_or_refund_records_call_with_zero_spend() -> None:
    SocialCrawlConnector.reset_credits()
    session = MagicMock()
    session.get.return_value = response(credits_used=0)

    connector()._call(session, "tiktok/search/top", {"query": "amapiano"}, "search")

    usage = SocialCrawlConnector.usage_for("za", "search")
    assert usage.calls == 1
    assert usage.budget_debit_credits == 0
    assert usage.vendor_reported_credits == 0


def test_phase_rows_include_zero_usage_and_reconcile_to_run_total() -> None:
    SocialCrawlConnector.reset_credits()
    za = connector("za")
    ng = connector("ng")
    session = MagicMock()
    session.get.side_effect = [response(credits_used=1), response(credits_used=2)]

    za._call(session, "tiktok/search/top", {"query": "amapiano"}, "search")
    ng._call(session, "tiktok/trending", {"region": "NG"}, "discover")

    rows = SocialCrawlConnector.phase_usages()
    assert len(rows) == 3 * len(PHASE_ORDER)
    assert tuple((item.market, item.phase) for item in rows) == tuple(
        (market, phase) for market in ("za", "ng", "ke") for phase in PHASE_ORDER
    )
    assert sum(item.calls for item in rows) == 2
    assert sum(item.budget_debit_credits for item in rows) == 3
    assert sum(item.vendor_reported_credits for item in rows) == 3
    assert SocialCrawlConnector.credits_used_for("za") == 1
    assert SocialCrawlConnector.credits_used_for("ng") == 2


def test_reset_clears_every_attribution_counter() -> None:
    SocialCrawlConnector.reset_credits()
    session = MagicMock()
    session.get.return_value = response(credits_used=1)
    connector()._call(session, "tiktok/search/top", {"query": "amapiano"}, "search")

    SocialCrawlConnector.reset_credits()

    assert all(
        item.calls == item.budget_debit_credits == item.vendor_reported_credits == 0
        for item in SocialCrawlConnector.phase_usages()
    )


@pytest.mark.parametrize("market", ["ZA", "", "gh"])
def test_usage_refuses_unsupported_market(market) -> None:
    with pytest.raises(ValueError, match="market"):
        SocialCrawlConnector.usage_for(market, "search")


def test_usage_refuses_unsupported_phase_and_public_row_is_frozen() -> None:
    with pytest.raises(ValueError, match="phase"):
        SocialCrawlConnector.usage_for("za", "other")
    item = SocialCrawlConnector.usage_for("za", "search")
    with pytest.raises(FrozenInstanceError):
        item.calls = 2


def funded_config():
    return {
        "socialcrawl": {
            "enabled": True,
            "budget_credits_per_run": 620,
            "max_age_days": 21,
            "timeout": 45,
            "phases": ["search"],
            "phase_share": dict.fromkeys(PHASE_ORDER, 1.0),
            "caps": {"search_terms": 1, "threads_terms": 0},
            "markets": {"za": {"terms": ["amapiano"]}},
        }
    }


def test_funded_context_selects_ogilvy_secret_budget_and_phase_hook() -> None:
    SocialCrawlConnector.reset_credits()
    hooks = []
    authority_checks = []
    context = FundedSocialCrawlContext(
        credential_lane="ogilvy_funded",
        secret_id="SOCIALCRAWL_OGILVY_API_KEY",
        run_allowance=100,
        phase_close_hook=lambda usage: hooks.append(usage),
        pre_call_authority_hook=authority_checks.append,
    )
    SocialCrawlConnector.configure_funded_run(context)
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = [response(credits_used=1), response(credits_used=0)]
    secret_calls = []

    with (
        patch("src.ingestion.connectors.socialcrawl.load_sources", return_value=funded_config()),
        patch(
            "src.ingestion.connectors.socialcrawl.get_secret",
            side_effect=lambda secret_id: secret_calls.append(secret_id) or "funded_key",
        ),
        patch("requests.Session", return_value=session),
        patch.dict(
            "os.environ",
            {"TRENDS_ENV": "staging", "SOCIALCRAWL_CREDENTIAL_LANE": "ogilvy_funded"},
            clear=False,
        ),
    ):
        result = SocialCrawlConnector(market="za").fetch()

    assert secret_calls == ["SOCIALCRAWL_OGILVY_API_KEY"]
    assert session.headers["x-api-key"] == "funded_key"
    assert hooks == [SocialCrawlConnector.usage_for("za", "search")]
    assert hooks[0].calls == 2
    assert hooks[0].budget_debit_credits == 1
    assert authority_checks == [1, 2]
    assert result.empty


def test_funded_call_checks_cumulative_debit_immediately_before_each_http() -> None:
    SocialCrawlConnector.reset_credits()
    events = []
    SocialCrawlConnector.configure_funded_run(
        FundedSocialCrawlContext(
            credential_lane="ogilvy_funded",
            secret_id="SOCIALCRAWL_OGILVY_API_KEY",
            run_allowance=100,
            phase_close_hook=lambda _usage: None,
            pre_call_authority_hook=lambda debit: events.append(("authority", debit)),
        )
    )

    class Session:
        def __init__(self):
            self.reported = iter((3, 0))

        def get(self, _url, *, params, timeout, stream=False):
            events.append(("http", dict(params), timeout))
            return response(credits_used=next(self.reported))

    item = connector()
    item._call(Session(), "tiktok/search/top", {"query": "amapiano"}, "search")
    item._call(Session(), "instagram/search/hashtag", {"hashtag": "amapiano"}, "search")

    assert events == [
        ("authority", 1),
        ("http", {"query": "amapiano"}, 45),
        ("authority", 8),
        ("http", {"hashtag": "amapiano"}, 45),
    ]


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_funded_context_refuses_outside_staging_before_secret_or_network(environment) -> None:
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(
        FundedSocialCrawlContext(
            credential_lane="ogilvy_funded",
            secret_id="SOCIALCRAWL_OGILVY_API_KEY",
            run_allowance=100,
            phase_close_hook=lambda usage: None,
            pre_call_authority_hook=lambda _debit: None,
        )
    )
    session = MagicMock()
    secret_calls = []
    with (
        patch("src.ingestion.connectors.socialcrawl.load_sources", return_value=funded_config()),
        patch(
            "src.ingestion.connectors.socialcrawl.get_secret",
            side_effect=lambda secret_id: secret_calls.append(secret_id),
        ),
        patch("requests.Session", return_value=session),
        patch.dict(
            "os.environ",
            {"TRENDS_ENV": environment, "SOCIALCRAWL_CREDENTIAL_LANE": "ogilvy_funded"},
            clear=False,
        ),
    ):
        item = SocialCrawlConnector(market="za")
        result = item.fetch()

    assert result.empty
    assert secret_calls == []
    assert session.get.call_count == 0
    assert any("staging only" in failure for failure in item._fetch_failures)


def test_phase_hook_failure_stops_later_markets_and_leaves_execution_unclosed() -> None:
    SocialCrawlConnector.reset_credits()

    def fail_hook(usage):
        raise RuntimeError("ledger unavailable")

    SocialCrawlConnector.configure_funded_run(
        FundedSocialCrawlContext(
            credential_lane="ogilvy_funded",
            secret_id="SOCIALCRAWL_OGILVY_API_KEY",
            run_allowance=100,
            phase_close_hook=fail_hook,
            pre_call_authority_hook=lambda _debit: None,
        )
    )
    first_session = MagicMock()
    first_session.headers = {}
    first_session.get.side_effect = [response(credits_used=1), response(credits_used=0)]
    with (
        patch("src.ingestion.connectors.socialcrawl.load_sources", return_value=funded_config()),
        patch("src.ingestion.connectors.socialcrawl.get_secret", return_value="funded_key"),
        patch("requests.Session", return_value=first_session),
        patch.dict(
            "os.environ",
            {"TRENDS_ENV": "staging", "SOCIALCRAWL_CREDENTIAL_LANE": "ogilvy_funded"},
            clear=False,
        ),
    ):
        with pytest.raises(RuntimeError, match="ledger unavailable"):
            SocialCrawlConnector(market="za").fetch()
        blocked = SocialCrawlConnector(market="ng")
        result = blocked.fetch()

    assert result.empty
    assert first_session.get.call_count == 2
    assert any("attribution failed" in failure for failure in blocked._fetch_failures)


def test_safe_fetch_cannot_hide_attribution_failure_from_run_close() -> None:
    SocialCrawlConnector.reset_credits()
    as_of = datetime(2026, 8, 27, 10, 0, tzinfo=UTC)
    gate = evaluate_funded_lane(
        MonthlySpendSnapshot(
            as_of=as_of,
            month_start=date(2026, 8, 1),
            current_balance=Decimal("250100"),
            month_opening_balance=Decimal("250100"),
            monthly_ledger_debit=Decimal("0"),
            monthly_vendor_reported=Decimal("0"),
            unreconciled_execution_ids=(),
            activation_stage=1,
            consecutive_complete_runs=0,
            runs_today=0,
            catalog_digest="6cb7a55dbeec22d0c16bfe9c9e8fee6c2ba461e6e5ac08b762d8ca8046ffcff6",
            metadata_digest="501ff9eae79e1504840107432b58b66c19de70e44e2ebb16aea190e4d394c39a",
        ),
        _rules(),
    )

    def fail_writer(row):
        raise RuntimeError("ledger unavailable")

    runtime = FundedLaneRuntime(
        run_id="run_001",
        execution_id="execution_001",
        as_of=as_of,
        gate=gate,
        ledger_writer=fail_writer,
        clock=lambda: as_of,
    )
    SocialCrawlConnector.configure_funded_run(
        FundedSocialCrawlContext(
            credential_lane="ogilvy_funded",
            secret_id="SOCIALCRAWL_OGILVY_API_KEY",
            run_allowance=100,
            phase_close_hook=runtime.phase_close,
            pre_call_authority_hook=lambda _debit: None,
        )
    )
    session = MagicMock()
    session.headers = {}
    session.get.side_effect = [response(credits_used=1), response(credits_used=0)]
    with (
        patch("src.ingestion.connectors.socialcrawl.load_sources", return_value=funded_config()),
        patch("src.ingestion.connectors.socialcrawl.get_secret", return_value="funded_key"),
        patch("requests.Session", return_value=session),
        patch.dict(
            "os.environ",
            {"TRENDS_ENV": "staging", "SOCIALCRAWL_CREDENTIAL_LANE": "ogilvy_funded"},
            clear=False,
        ),
    ):
        result = SocialCrawlConnector(market="za").safe_fetch()

    assert result.empty
    with pytest.raises(ValueError, match="21 phase close"):
        runtime.close(post_balance=Decimal("250100"), recorded_at=as_of)


@pytest.mark.parametrize(
    "overrides",
    [
        {"credential_lane": "jhb_core"},
        {"secret_id": "SOCIALCRAWL_API_KEY"},
        {"run_allowance": 0},
        {"run_allowance": 101},
        {"phase_close_hook": None},
        {"pre_call_authority_hook": None},
    ],
)
def test_funded_context_is_exact_and_cannot_expand_stage_one(overrides) -> None:
    values = {
        "credential_lane": "ogilvy_funded",
        "secret_id": "SOCIALCRAWL_OGILVY_API_KEY",
        "run_allowance": 100,
        "phase_close_hook": lambda usage: None,
        "pre_call_authority_hook": lambda _debit: None,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        FundedSocialCrawlContext(**values)


@pytest.mark.parametrize("allowance", [25000 // 30, 25000 // 31, 0, 101, 1000])
def test_connector_allowance_refuses_a_monthly_allocation_divided_by_days(allowance) -> None:
    values = {
        "credential_lane": "ogilvy_funded",
        "secret_id": "SOCIALCRAWL_OGILVY_API_KEY",
        "phase_close_hook": lambda usage: None,
        "pre_call_authority_hook": lambda _debit: None,
    }
    with pytest.raises(ValueError, match="funded run allowance is invalid"):
        FundedSocialCrawlContext(run_allowance=allowance, **values)
    for accepted in (100, 250, 750):
        assert FundedSocialCrawlContext(run_allowance=accepted, **values).run_allowance == accepted
