from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from core.eval import ask_r3_coverage


SAST = timezone(timedelta(hours=2))


def _modules(checked):
    return SimpleNamespace(
        sql=SimpleNamespace(
            MAX_BYTES_BILLED=123456,
            check_sql=lambda sql: checked.append(sql),
        ),
        staging=SimpleNamespace(SAST=SAST),
    )


def _coverage_rows():
    return [
        {
            "market": market,
            "eligible_distinct_posts": 12,
            "needs_embedding_distinct_posts": 4,
            "embedded_distinct_posts": 7,
            "invalid_nonempty_embedding_distinct_posts": 1,
            "geo_market_match_distinct_posts": 9,
            "creator_home_market_match_distinct_posts": 3,
            "source_sighting_match_distinct_posts": 5,
        }
        for market in ask_r3_coverage.MARKETS
    ]


def test_snapshot_uses_seven_inclusive_sast_dates_and_tvf_eligibility():
    checked = []
    calls = []

    def execute(sql, params, max_bytes):
        calls.append((sql, params, max_bytes))
        if sql == ask_r3_coverage.LATEST_RUN_SQL:
            return {"rows": [{"run_id": "understand-1", "run_date": "2026-10-01", "status": "ok",
                              "started_at": "2026-10-01T04:00:00+02:00", "finished_at": None}]}
        return {"rows": _coverage_rows()}

    wiring = SimpleNamespace(
        now=lambda: datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc),
        execute=execute,
    )
    result = ask_r3_coverage.read_embedding_coverage(wiring, _modules(checked))

    assert result["status"] == "ok"
    assert result["as_of_date"] == "2026-10-01"
    assert result["window_start_date"] == "2026-09-25"
    assert result["window_end_date"] == "2026-10-01"
    assert result["current_day_partial"] is True
    assert result["latest_embedding_run"]["run_id"] == "understand-1"
    assert result["markets"]["ZA"]["eligible_distinct_posts"] == 12
    assert result["markets"]["ZA"]["embedded_distinct_posts"] == 7
    assert result["markets"]["ZA"]["needs_embedding_distinct_posts"] == 4
    assert result["markets"]["ZA"]["eligibility_routes"]["source_sighting_match_distinct_posts"] == 5
    assert len(checked) == len(calls) == 2
    assert all(max_bytes == 123456 for _, _, max_bytes in calls)
    coverage_sql, coverage_params, _ = next(call for call in calls if call[0] == ask_r3_coverage.COVERAGE_SQL)
    assert coverage_params == {"window_start": datetime(2026, 9, 25).date(),
                               "window_end": datetime(2026, 10, 1).date()}
    assert "o.observed_date BETWEEN @window_start AND @window_end" in coverage_sql
    assert "ss.obs_date BETWEEN @window_start AND @window_end" in coverage_sql
    assert "p.post_date BETWEEN @window_start AND @window_end" in coverage_sql
    assert "CONCAT('\\n', p.transcript)" in coverage_sql
    assert "CONCAT('\n', p.transcript)" not in coverage_sql
    assert "p.geo_market = m.market" in coverage_sql
    assert "c.home_market AS market" in coverage_sql
    assert "ss.source_market AS market" in coverage_sql
    assert "c.market = m.market" in coverage_sql
    assert "sm.market = m.market" in coverage_sql
    assert "ON p.geo_market = m.market" not in coverage_sql


def test_post_date_fixture_includes_both_window_edges_and_excludes_outside_dates():
    checked = []
    fixtures = [
        {"market": "ZA", "post_date": date(2026, 9, 25), "observed_date": date(2026, 9, 25),
         "embedded": True},
        {"market": "ZA", "post_date": date(2026, 10, 1), "observed_date": date(2026, 10, 1),
         "embedded": False},
        {"market": "ZA", "post_date": date(2026, 9, 24), "observed_date": date(2026, 9, 25),
         "embedded": True},
        {"market": "ZA", "post_date": date(2026, 10, 2), "observed_date": date(2026, 10, 1),
         "embedded": True},
        {"market": "ZA", "post_date": date(2026, 9, 25), "observed_date": date(2026, 9, 24),
         "embedded": True},
    ]

    def execute(sql, params, max_bytes):
        if sql == ask_r3_coverage.LATEST_RUN_SQL:
            return {"rows": []}
        assert "p.post_date BETWEEN @window_start AND @window_end" in sql
        window_rows = [
            row for row in fixtures
            if params["window_start"] <= row["post_date"] <= params["window_end"]
            and params["window_start"] <= row["observed_date"] <= params["window_end"]
        ]
        out = []
        for market in ask_r3_coverage.MARKETS:
            rows = [row for row in window_rows if row["market"] == market]
            out.append({
                "market": market,
                "eligible_distinct_posts": len(rows),
                "needs_embedding_distinct_posts": sum(not row["embedded"] for row in rows),
                "embedded_distinct_posts": sum(row["embedded"] for row in rows),
                "invalid_nonempty_embedding_distinct_posts": 0,
                "geo_market_match_distinct_posts": len(rows),
                "creator_home_market_match_distinct_posts": 0,
                "source_sighting_match_distinct_posts": 0,
            })
        return {"rows": out}

    wiring = SimpleNamespace(
        now=lambda: datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc),
        execute=execute,
    )
    result = ask_r3_coverage.read_embedding_coverage(wiring, _modules(checked))

    assert result["window_start_date"] == "2026-09-25"
    assert result["window_end_date"] == "2026-10-01"
    assert result["markets"]["ZA"]["eligible_distinct_posts"] == 2
    assert result["markets"]["ZA"]["embedded_distinct_posts"] == 1
    assert result["markets"]["ZA"]["needs_embedding_distinct_posts"] == 1


def test_failed_coverage_read_is_classified_without_reporting_zero_or_error_text():
    checked = []

    def execute(sql, params, max_bytes):
        if sql == ask_r3_coverage.LATEST_RUN_SQL:
            return {"rows": []}
        raise PermissionDenied("secret and query text must not escape")

    class PermissionDenied(Exception):
        pass

    wiring = SimpleNamespace(
        now=lambda: datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        execute=execute,
    )
    result = ask_r3_coverage.read_embedding_coverage(wiring, _modules(checked))

    assert result["status"] == "partial"
    assert result["latest_embedding_run"] is None
    assert result["markets"]["ZA"]["eligible_distinct_posts"] is None
    assert result["markets"]["ZA"]["embedded_distinct_posts"] is None
    assert result["errors"] == [{"read": "coverage", "classification": "permission_error",
                                 "error_class": "PermissionDenied"}]
    assert "secret" not in str(result)


def test_result_shape_error_nulls_all_coverage_markets():
    checked = []

    def execute(sql, params, max_bytes):
        if sql == ask_r3_coverage.LATEST_RUN_SQL:
            return {"rows": [{"run_id": "understand-1", "run_date": "2026-09-30", "status": "ok"}]}
        return {"rows": _coverage_rows()[:2]}

    wiring = SimpleNamespace(
        now=lambda: datetime(2026, 9, 30, 12, tzinfo=timezone.utc),
        execute=execute,
    )
    result = ask_r3_coverage.read_embedding_coverage(wiring, _modules(checked))

    assert result["status"] == "partial"
    assert result["markets"]["ZA"]["eligible_distinct_posts"] is None
    assert result["markets"]["NG"]["embedded_distinct_posts"] is None
    assert result["markets"]["KE"]["needs_embedding_distinct_posts"] is None
    assert result["errors"] == [{"read": "coverage", "classification": "result_shape_invalid",
                                 "error_class": "ValueError"}]
