from datetime import date, datetime, timezone

import pytest

from core.agent.context import RunContext
from core.agent.tools import warehouse as warehouse_tools
from core.agent.tools.sql_query import MAX_BYTES_BILLED
from core.agent.tools.warehouse import MAX_POSTS


class ScenarioWarehouse:
    def __init__(self, responses, fail_at=None, failure=None):
        self.responses = responses
        self.fail_at = fail_at
        self.failure = failure or RuntimeError("warehouse unavailable")
        self.dry_runs = []
        self.runs = []

    def dry_run(self, sql, params):
        self.dry_runs.append((sql, params))
        tables = []
        if "v_briefs_current" in sql:
            tables.append("ogilvy-trends-v2.intelligence_42_agent.v_briefs_current")
        if "intelligence_42_core.posts" in sql:
            tables.append("ogilvy-trends-v2.intelligence_42_core.posts")
        return {"bytes": 10_000, "tables": tables}

    def run(self, sql, params, max_bytes_billed):
        index = len(self.runs)
        self.runs.append((sql, params, max_bytes_billed))
        if index == self.fail_at:
            raise self.failure
        return [dict(row) for row in self.responses[index]]


def make_ctx(as_of=datetime(2026, 10, 1, 6, 0)):
    return RunContext(run_id="fallback_test", tier="T0", as_of=as_of)


def retrieve(ctx, warehouse, market):
    helper = getattr(warehouse_tools, "get_trending_fallback_snapshot", None)
    assert callable(helper), "trending fallback retrieval helper is missing"
    return helper(ctx, warehouse, market)


def today_row(market, **extra):
    return {
        "brief_date": date(2026, 10, 1), "market": market, "run_id": f"today-{market}",
        "published_at": datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc), "status": "published",
        "card_count": 1, "cards_type": "array", "valid_card_count": 1, **extra,
    }


def latest_row(market, **extra):
    return {
        "brief_date": date(2026, 9, 30), "market": market, "run_id": f"brief-{market}",
        "published_at": datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc), "status": "published",
        "card_count": 1, "cards_type": "array", "item_id": "trend-1", "item_id_type": "string",
        "title": "Local food references", "title_type": "string", "rank": "2", "kind": "format",
        "state": "rising", "card_offset": 0, **extra,
    }


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
def test_snapshot_returns_market_scoped_today_brief_posts_and_older_card_leads(market):
    ctx = make_ctx(datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc))
    posts = [
        {"post_id": "new-located", "published_at": None, "post_date": date(2026, 10, 1),
         "geo_market": market, "geo_source": "post_location"},
        {"post_id": "oldest-in-window", "published_at": datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc),
         "post_date": date(2026, 9, 24),
         "geo_market": market, "geo_source": None},
        {"post_id": "profile-only", "published_at": None, "post_date": date(2026, 10, 1),
         "geo_market": market, "geo_source": "home_market"},
        {"post_id": "feed-only", "published_at": None, "post_date": date(2026, 10, 1),
         "geo_market": None, "geo_source": None, "source_sightings": [{"source_market": market}]},
        {"post_id": "other-market", "published_at": None, "post_date": date(2026, 10, 1),
         "geo_market": next(code for code in ("ZA", "NG", "KE") if code != market),
         "geo_source": "post_location"},
        {"post_id": "before-window", "published_at": None, "post_date": date(2026, 9, 24),
         "geo_market": market, "geo_source": "post_location"},
    ]
    empty_today = today_row(market, card_count=0, valid_card_count=0)
    wh = ScenarioWarehouse([[empty_today], posts, [latest_row(market)]])

    result = retrieve(ctx, wh, market)

    assert result["complete"] is True and result["error"] is None, result
    assert result["market"] == market and result["as_of"] == date(2026, 10, 1)
    assert result["window"] == {"from": date(2026, 9, 25), "to": date(2026, 10, 1)}
    assert result["today"]["complete"] is True
    assert result["today"]["available"] is False
    assert result["today"]["brief_date"] == date(2026, 10, 1)
    assert result["today"]["run_id"] == f"today-{market}"
    assert result["today"]["status"] == "published"
    assert result["today"]["card_count"] == 0
    assert [row["post_id"] for row in result["located_posts"]["post_ids"]] == [
        "new-located", "oldest-in-window"]
    assert result["located_posts"]["post_ids"][0]["posted_at"] == date(2026, 10, 1)
    assert result["located_posts"]["post_ids"][1]["posted_at"] == date(2026, 9, 25)
    assert result["latest_brief"]["brief"]["brief_date"] == date(2026, 9, 30)
    assert result["latest_brief"]["brief"]["status"] == "published"
    assert result["latest_brief"]["brief"]["cards"] == [{
        "item_id": "trend-1", "title": "Local food references", "rank": "2",
        "kind": "format", "state": "rising",
    }]
    latest_rows = ctx.queries[result["latest_brief"]["query_id"]]["rows"]
    assert all(not ({"cards", "payload", "claims", "evidence", "ask"} & set(row)) for row in latest_rows)
    assert all(result[key]["query_id"] in ctx.queries for key in ("today", "located_posts", "latest_brief"))

    today_sql, today_params, today_cap = wh.runs[0]
    posts_sql, posts_params, posts_cap = wh.runs[1]
    latest_sql, latest_params, latest_cap = wh.runs[2]
    assert "intelligence_42_agent.v_briefs_current" in today_sql
    assert "intelligence_42_agent.v_briefs_current" in latest_sql
    assert "intelligence_42_core.posts" in posts_sql
    assert today_params["market"] == posts_params["market"] == latest_params["market"] == market
    assert posts_params["since"] == date(2026, 9, 25) and posts_params["until"] == date(2026, 10, 1)
    assert posts_params["limit"] <= MAX_POSTS
    assert posts_params["profile_source"] == "home_market"
    assert "geo_market = @market" in posts_sql
    assert "COALESCE(p.geo_source, '')" in posts_sql
    assert "source_sightings" not in posts_sql and "home_market" not in posts_sql.split("WHERE", 1)[0]
    assert "status IN ('published', 'partial')" in latest_sql
    assert "ARRAY_LENGTH(JSON_QUERY_ARRAY" in latest_sql
    assert latest_params["as_of"] == date(2026, 10, 1)
    assert not any(cap > MAX_BYTES_BILLED for _, _, cap in wh.runs)
    assert today_cap <= MAX_BYTES_BILLED and posts_cap <= MAX_BYTES_BILLED and latest_cap <= MAX_BYTES_BILLED


def test_successful_empty_today_is_distinct_from_a_failed_read():
    ctx = make_ctx()
    wh = ScenarioWarehouse([[], [], [latest_row("ZA")]])

    result = retrieve(ctx, wh, "ZA")

    assert result["complete"] is True, result
    assert result["today"] == {
        "complete": True, "available": False, "brief_date": None, "run_id": None,
        "published_at": None, "status": None, "card_count": 0, "query_id": "q_1",
    }
    assert result["latest_brief"]["brief"]["brief_date"] == date(2026, 9, 30)


def test_missing_published_brief_with_cards_is_a_complete_empty_read():
    ctx = make_ctx()
    wh = ScenarioWarehouse([[], [], []])

    result = retrieve(ctx, wh, "KE")

    assert result["complete"] is True and result["error"] is None
    assert result["today"]["available"] is False
    assert result["latest_brief"]["complete"] is True
    assert result["latest_brief"]["brief"] is None
    assert result["latest_brief"]["query_id"] == "q_3"


@pytest.mark.parametrize("status", ["published", "partial"])
def test_today_brief_with_approved_cards_counts_as_available_without_fallback_reads(status):
    ctx = make_ctx()
    wh = ScenarioWarehouse([[today_row("KE", status=status)], [], [latest_row("KE", status="partial")]])

    result = retrieve(ctx, wh, "KE")

    assert result["complete"] is True
    assert result["today"]["available"] is True
    assert result["today"]["card_count"] == 1
    assert result["today"]["status"] == status
    assert len(wh.runs) == 1


def test_latest_partial_brief_with_cards_can_supply_research_leads():
    ctx = make_ctx()
    empty_today = today_row("KE", card_count=0, valid_card_count=0)
    wh = ScenarioWarehouse([[empty_today], [], [latest_row("KE", status="partial")]])

    result = retrieve(ctx, wh, "KE")

    assert result["complete"] is True
    assert result["today"]["available"] is False
    assert result["latest_brief"]["brief"]["status"] == "partial"


@pytest.mark.parametrize("status", ["published", "partial", "data_issue"])
def test_valid_empty_today_cards_confirm_no_published_cards_and_preserve_status(status):
    ctx = make_ctx()
    row = today_row("KE", status=status, card_count=0, cards_type="array", valid_card_count=0)
    wh = ScenarioWarehouse([[row], [], [latest_row("KE")]])

    result = retrieve(ctx, wh, "KE")

    assert result["complete"] is True
    assert result["today"]["status"] == status
    assert result["today"]["available"] is False
    assert result["today"]["card_count"] == 0
    assert len(wh.runs) == 3


@pytest.mark.parametrize("row", [
    today_row("ZA", status="data_issue", card_count=1, valid_card_count=1),
    today_row("ZA", status="unknown", card_count=0, cards_type="array", valid_card_count=0),
    today_row("ZA", status=None, card_count=0, cards_type="array", valid_card_count=0),
    today_row("ZA", status="published", card_count=1, valid_card_count=0),
    today_row("ZA", status="published", card_count=None, cards_type="null", valid_card_count=0),
    today_row("ZA", status="published", card_count=None, cards_type=None, valid_card_count=0),
])
def test_untrusted_today_status_or_card_shape_is_unknown_not_empty(row):
    ctx = make_ctx()
    wh = ScenarioWarehouse([[row], [], []])

    result = retrieve(ctx, wh, "ZA")

    assert result["complete"] is False
    assert result["today"]["available"] is None
    assert result["error"]["stage"] == "today"


@pytest.mark.parametrize(("failed_at", "failed_stage"), [
    (0, "today"), (1, "located_posts"), (2, "latest_brief"),
])
def test_query_failure_marks_snapshot_incomplete_and_does_not_claim_today_empty(failed_at, failed_stage):
    ctx = make_ctx()
    empty_today = today_row("ZA", card_count=0, valid_card_count=0)
    wh = ScenarioWarehouse([[empty_today], [], [latest_row("ZA")]], fail_at=failed_at)

    result = retrieve(ctx, wh, "ZA")

    assert result["complete"] is False
    assert result["error"]["stage"] == failed_stage
    assert result["error"]["type"] == "RuntimeError"
    assert result[failed_stage]["complete"] is False
    assert len(wh.runs) == failed_at + 1
    if failed_stage == "today":
        assert result["today"]["available"] is None
    else:
        assert result["today"]["available"] is False


def test_malformed_today_card_shape_is_an_error_not_an_empty_board():
    ctx = make_ctx()
    wh = ScenarioWarehouse([[today_row("ZA", card_count=None, cards_type="object")], [], []])

    result = retrieve(ctx, wh, "ZA")

    assert result["complete"] is False
    assert result["today"]["available"] is None
    assert result["error"]["stage"] == "today"


def test_today_blank_card_metadata_is_an_error_not_an_empty_board():
    ctx = make_ctx()
    wh = ScenarioWarehouse([[today_row("ZA", valid_card_count=0)], [], []])

    result = retrieve(ctx, wh, "ZA")

    assert result["complete"] is False
    assert result["today"]["available"] is None
    assert result["error"]["stage"] == "today"
    today_sql = wh.runs[0][0]
    assert "NULLIF(TRIM(JSON_VALUE(card, '$.item_id')), '') IS NOT NULL" in today_sql
    assert "NULLIF(TRIM(JSON_VALUE(card, '$.title')), '') IS NOT NULL" in today_sql


def test_malformed_latest_brief_cards_are_an_error_not_missing_context():
    ctx = make_ctx()
    malformed = latest_row("NG", item_id=None, item_id_type="null")
    wh = ScenarioWarehouse([[], [], [malformed]])

    result = retrieve(ctx, wh, "NG")

    assert result["complete"] is False
    assert result["latest_brief"]["brief"] is None
    assert result["error"]["stage"] == "latest_brief"


@pytest.mark.parametrize(("item_id_type", "title_type"), [
    ("null", "string"), ("string", "null"), (None, "string"), ("string", None),
])
def test_latest_brief_requires_string_item_id_and_title(item_id_type, title_type):
    ctx = make_ctx()
    row = latest_row("NG", item_id_type=item_id_type, title_type=title_type)
    wh = ScenarioWarehouse([[], [], [row]])

    result = retrieve(ctx, wh, "NG")

    assert result["complete"] is False
    assert result["latest_brief"]["brief"] is None
    assert result["error"]["stage"] == "latest_brief"


@pytest.mark.parametrize(("field", "value"), [
    ("item_id", ""), ("item_id", "   "), ("title", ""), ("title", "   "),
])
def test_latest_brief_rejects_blank_or_whitespace_card_metadata(field, value):
    ctx = make_ctx()
    row = latest_row("NG", **{field: value})
    wh = ScenarioWarehouse([[], [], [row]])

    result = retrieve(ctx, wh, "NG")

    assert result["complete"] is False
    assert result["latest_brief"]["brief"] is None
    assert result["error"]["stage"] == "latest_brief"


def test_invalid_market_fails_before_any_warehouse_call():
    ctx = make_ctx()
    wh = ScenarioWarehouse([[], [], []])

    result = retrieve(ctx, wh, "US")

    assert result["complete"] is False
    assert result["error"]["category"] == "guard"
    assert not wh.dry_runs and not wh.runs


def test_authorization_failure_does_not_report_today_as_empty():
    class Forbidden(Exception):
        __module__ = "google.api_core.exceptions"
        code = 403

    ctx = make_ctx()
    wh = ScenarioWarehouse([[], [], []], fail_at=0, failure=Forbidden("denied"))

    result = retrieve(ctx, wh, "ZA")

    assert result["complete"] is False
    assert result["today"]["available"] is None
    assert result["error"]["category"] == "permission"
