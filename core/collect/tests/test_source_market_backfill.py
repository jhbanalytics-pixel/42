import importlib
import re
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.collect.parse import _protocol, _source_provenance


def _backfill():
    try:
        return importlib.import_module("core.collect.source_market_backfill")
    except ModuleNotFoundError as exc:
        if exc.name == "core.collect.source_market_backfill":
            pytest.fail("source_market_backfill is not implemented")
        raise


def _row(route, protocol, market, *, source_market=None, source_region=None, post_id="post"):
    return {
        "post_id": post_id,
        "observed_at": "2026-09-01T00:00:00+00:00",
        "observed_date": "2026-09-01",
        "market": market,
        "source_market": source_market,
        "source_region": source_region,
        "platform": "tiktok",
        "route": route,
        "series": "feed_tiktok",
        "protocol": protocol,
    }


def _scoped_row(route, market, *, source_region=None, post_id="post"):
    params_and_keys = {
        "tiktok/trending": ({"feed": "local", "region": market}, ("region", "feed")),
        "youtube/videos/trending": (
            {"region": market, "category": "25", "language": "en", "max_results": 20},
            ("region", "category", "language", "max_results"),
        ),
        "tiktok/search/hashtag": (
            {"region": market, "max_age_days": 7, "min_views": 100, "sort_rows": "views"},
            ("region", "max_age_days", "min_views", "sort_rows"),
        ),
    }
    params, keys = params_and_keys[route]
    protocol = _protocol(route, params, keys)
    assert _source_provenance(route, params, market)[0] == market
    return _row(route, protocol, market, source_region=source_region, post_id=post_id)


def test_country_candidates_follow_only_stored_explicit_scope_for_each_market():
    backfill = _backfill()
    rows = [
        _scoped_row(route, market, source_region=market)
        for route in (
            "tiktok/trending",
            "youtube/videos/trending",
            "tiktok/search/hashtag",
        )
        for market in ("ZA", "NG", "KE")
    ]

    assert [backfill.source_market_candidate(row) for row in rows] == [
        market for _route in range(3) for market in ("ZA", "NG", "KE")
    ]


def test_unknown_conflicting_or_unstored_scopes_remain_unclassified():
    backfill = _backfill()
    local = _scoped_row("tiktok/trending", "ZA")
    global_feed = _row("tiktok/trending", local["protocol"].replace("feed=local", "feed=global"), "ZA")
    missing_region = _row("youtube/videos/trending", "youtube/videos/trending?category=25", "ZA")
    mismatched_market = _scoped_row("tiktok/search/hashtag", "ZA")
    mismatched_market["market"] = "NG"
    mismatched_region = _scoped_row("youtube/videos/trending", "ZA", source_region="NG")
    mismatched_route = _row("youtube/videos/trending", local["protocol"], "ZA")
    unsupported_search = _row("tiktok/search/top", "tiktok/search/top?country=ZA", "ZA")
    unknown_country = _row("tiktok/trending", "tiktok/trending?feed=local&region=UG", "UG")
    extra_parameter = _row(
        "tiktok/trending",
        local["protocol"] + "&country=ZA",
        "ZA",
    )

    for row in (
        global_feed,
        missing_region,
        mismatched_market,
        mismatched_region,
        mismatched_route,
        unsupported_search,
        unknown_country,
        extra_parameter,
        _row("tiktok/trending", "tiktok/trending?feed=local&region=NG&region=ZA", "NG"),
        _row(
            "youtube/videos/trending",
            "youtube/videos/trending?category=25?region=NG&language=en&max_results=20&region=ZA",
            "NG",
        ),
        _row(
            "tiktok/search/hashtag",
            "tiktok/search/hashtag?max_age_days=7?region=KE&min_views=100&region=ZA",
            "KE",
        ),
    ):
        assert backfill.source_market_candidate(row) is None


def test_only_null_sightings_are_candidates_and_same_post_can_have_multiple_markets():
    backfill = _backfill()
    za = _scoped_row("youtube/videos/trending", "ZA", post_id="same-post")
    ng = _scoped_row("youtube/videos/trending", "NG", post_id="same-post")
    existing = _scoped_row("youtube/videos/trending", "KE", post_id="existing")
    existing["source_market"] = "ZA"

    assert [backfill.source_market_candidate(row) for row in (za, ng, existing)] == ["ZA", "NG", None]
    assert za["post_id"] == ng["post_id"]


def test_update_is_null_only_scoped_and_changes_only_observation_source_market():
    sql = " ".join(_backfill().build_update_sql().lower().split())
    set_clause = sql.split(" set ", 1)[1].split(" where ", 1)[0]

    assert "update `ogilvy-trends-v2.intelligence_42_core.post_observations` as o" in sql
    assert re.match(r"source_market\s*=\s*case", set_clause)
    assert "source_region" not in set_clause
    assert "o.source_market is null" in sql
    assert "o.observed_date <= @cutoff_date" in sql
    assert "posts" not in sql
    assert "geo_market" not in sql


def test_readback_rejects_row_changes_count_drift_and_wrong_market_values():
    backfill = _backfill()
    before = {
        "row_count": 6,
        "null_count": 3,
        "nonnull_count": 3,
        "eligible_count": 2,
        "invalid_nonnull_count": 1,
        "rowkey_xor": 10,
        "rowkey_sum": 15,
        "expected_source_xor": 17,
        "expected_source_sum": 23,
    }
    after = {
        "row_count": 6,
        "null_count": 1,
        "nonnull_count": 5,
        "eligible_count": 0,
        "invalid_nonnull_count": 1,
        "rowkey_xor": 10,
        "rowkey_sum": 15,
        "actual_source_xor": 17,
        "actual_source_sum": 23,
    }

    assert backfill.readback_matches(before, after, rows_updated=2)
    for field, wrong_value in (
        ("row_count", 7),
        ("null_count", 2),
        ("nonnull_count", 4),
        ("invalid_nonnull_count", 0),
        ("rowkey_xor", 11),
        ("rowkey_sum", 16),
        ("actual_source_xor", 18),
        ("actual_source_sum", 24),
    ):
        changed = dict(after, **{field: wrong_value})
        assert not backfill.readback_matches(before, changed, rows_updated=2)
    assert not backfill.readback_matches(before, after, rows_updated=1)


def test_cutoff_is_frozen_timezone_aware_and_includes_the_current_partition():
    backfill = _backfill()
    now = datetime(2026, 9, 30, 16, 30, tzinfo=timezone(timedelta(hours=2)))

    assert backfill.resolve_cutoff_timestamp(None, apply=False, now=now) == datetime(
        2026, 9, 30, 14, 30, tzinfo=timezone.utc
    )
    assert backfill.resolve_cutoff_timestamp(
        "2026-09-29T23:59:00+02:00", apply=True, now=now
    ) == datetime(2026, 9, 29, 21, 59, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="exact --cutoff-timestamp"):
        backfill.resolve_cutoff_timestamp(None, apply=True, now=now)
    with pytest.raises(ValueError, match="timezone offset"):
        backfill.resolve_cutoff_timestamp("2026-09-30T12:00:00", apply=False, now=now)
    with pytest.raises(ValueError, match="future"):
        backfill.resolve_cutoff_timestamp("2026-10-01T00:00:00Z", apply=False, now=now)


_BEFORE = {
    "row_count": 6,
    "null_count": 3,
    "nonnull_count": 3,
    "eligible_count": 2,
    "invalid_nonnull_count": 1,
    "rowkey_xor": 10,
    "rowkey_sum": 15,
    "expected_source_xor": 17,
    "expected_source_sum": 23,
}
_AFTER = {
    "row_count": 6,
    "null_count": 1,
    "nonnull_count": 5,
    "eligible_count": 0,
    "invalid_nonnull_count": 1,
    "rowkey_xor": 10,
    "rowkey_sum": 15,
    "actual_source_xor": 17,
    "actual_source_sum": 23,
}


class _Job:
    def __init__(self, job_id, bytes_processed, *, rows=(), affected_rows=None):
        self.job_id = job_id
        self.total_bytes_processed = bytes_processed
        self.num_dml_affected_rows = affected_rows
        self._rows = list(rows)

    def result(self):
        return iter(self._rows)


class _Client:
    def __init__(
        self,
        backfill,
        *,
        estimates=None,
        summaries=None,
        affected_rows=2,
        missing_actual_job_ids=(),
    ):
        self.backfill = backfill
        self.estimates = estimates or {"update": 10, "preview": 20, "summary": 30}
        self.summaries = list(summaries or [_BEFORE, _AFTER])
        self.missing_actual_job_ids = set(missing_actual_job_ids)
        self.calls = []
        self.update_attempts = []
        self.summary_calls = 0
        self._sequence = 0
        self.affected_rows = affected_rows

    def _job(self, name, bytes_processed, *, rows=(), affected_rows=None):
        self._sequence += 1
        return _Job(f"{name}-{self._sequence}", bytes_processed, rows=rows, affected_rows=affected_rows)

    def query(self, sql, *, job_config, job_id=None):
        self.calls.append((sql, job_config, job_id))
        if sql == self.backfill.build_update_sql():
            stage = "update"
        elif sql == self.backfill.build_preview_sql():
            stage = "preview"
        elif sql == self.backfill.build_summary_sql():
            stage = "summary"
        else:
            raise AssertionError("run issued an unrecognized query")
        if job_config.dry_run:
            return _Job(None, self.estimates.get(stage))
        if stage == "update":
            self.update_attempts.append(job_id)
            job = self._job("update", 5, affected_rows=self.affected_rows)
            job.job_id = job_id
        elif stage == "preview":
            job = self._job(
                "preview", 8, rows=[{"source_market": "ZA", "sightings": 2, "posts": 2}]
            )
        else:
            self.summary_calls += 1
            job = self._job("summary", 9, rows=[self.summaries.pop(0)])
        if stage in self.missing_actual_job_ids:
            job.job_id = None
        return job


def _fake_job_config(monkeypatch, backfill):
    def make_config(cutoff_timestamp, *, dry_run=False):
        return SimpleNamespace(
            dry_run=dry_run,
            maximum_bytes_billed=backfill.MAX_BYTES_BILLED,
            query_parameters={
                "cutoff_timestamp": cutoff_timestamp,
                "cutoff_date": backfill._partition_date_cap(cutoff_timestamp),
            },
        )

    monkeypatch.setattr(backfill, "_job_config", make_config)


def test_query_config_carries_timestamp_and_partition_date_cap(monkeypatch):
    backfill = _backfill()
    fake_bigquery = SimpleNamespace(
        ScalarQueryParameter=lambda name, kind, value: (name, kind, value),
        QueryJobConfig=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    monkeypatch.setitem(sys.modules, "google.cloud", SimpleNamespace(bigquery=fake_bigquery))
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery)
    cutoff = datetime(2026, 9, 29, 22, 30, tzinfo=timezone.utc)

    config = backfill._job_config(cutoff, dry_run=True)

    assert config.dry_run is True
    assert config.maximum_bytes_billed == backfill.MAX_BYTES_BILLED
    assert config.query_parameters == [
        ("cutoff_timestamp", "TIMESTAMP", cutoff),
        ("cutoff_date", "DATE", date(2026, 9, 30)),
    ]


def test_adc_identity_is_checked_before_bigquery_client_construction(monkeypatch):
    backfill = _backfill()
    client_calls = []
    fake_auth = SimpleNamespace(default=lambda: (SimpleNamespace(service_account_email="owner@example.com"), None))
    fake_bigquery = SimpleNamespace(Client=lambda **kwargs: client_calls.append(kwargs))
    monkeypatch.setitem(sys.modules, "google.auth", fake_auth)
    monkeypatch.setitem(sys.modules, "google.cloud", SimpleNamespace(bigquery=fake_bigquery))
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery)

    with pytest.raises(RuntimeError, match=backfill.EXPECTED_BUILDER_EMAIL):
        backfill._builder_client()

    assert client_calls == []
    assert backfill.assert_builder_identity(
        SimpleNamespace(service_account_email=backfill.EXPECTED_BUILDER_EMAIL)
    ) == backfill.EXPECTED_BUILDER_EMAIL


def test_preflight_fails_closed_when_estimate_is_unknown_or_over_cap(monkeypatch):
    backfill = _backfill()
    _fake_job_config(monkeypatch, backfill)
    for estimate in (None, backfill.MAX_BYTES_BILLED + 1):
        client = _Client(backfill, estimates={"update": estimate, "preview": 20, "summary": 30})

        with pytest.raises(RuntimeError, match="estimate"):
            backfill.run(
                client,
                cutoff_timestamp=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc),
            )

        assert all(config.dry_run for _, config, _ in client.calls)
        assert client.update_attempts == []


def test_preflight_caps_combined_preview_update_and_readback_bytes(monkeypatch):
    backfill = _backfill()
    _fake_job_config(monkeypatch, backfill)
    client = _Client(
        backfill,
        estimates={"update": 2_000_000_000, "preview": 1_500_000_000, "summary": 1_500_000_000},
    )

    with pytest.raises(RuntimeError, match="combined.*estimate"):
        backfill.run(
            client,
            cutoff_timestamp=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc),
        )

    assert all(config.dry_run for _, config, _ in client.calls)
    assert client.update_attempts == []


def test_preview_records_metrics_and_reuses_frozen_cutoff(monkeypatch):
    backfill = _backfill()
    _fake_job_config(monkeypatch, backfill)
    client = _Client(backfill)
    cutoff = datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc)

    result = backfill.run(client, cutoff_timestamp=cutoff)

    assert result["cutoff_timestamp_exclusive"] == cutoff.isoformat()
    assert result["cutoff_date_partition_cap"] == "2026-09-30"
    assert result["estimated_run_bytes"] == 90
    assert result["query_jobs"][0]["statement"] == "preview"
    assert result["query_jobs"][0]["query_id"].startswith("preview-")
    assert result["query_jobs"][0]["estimated_bytes"] == 20
    assert result["query_jobs"][0]["bytes_processed"] == 8
    assert all(job["query_id"] is None for job in result["preflight_jobs"])
    assert all(job["query_id_persistence"] == "nonpersistent_dry_run" for job in result["preflight_jobs"])
    assert all(config.maximum_bytes_billed == backfill.MAX_BYTES_BILLED for _, config, _ in client.calls)
    assert all(
        config.query_parameters["cutoff_timestamp"] == cutoff
        and config.query_parameters["cutoff_date"] == date(2026, 9, 30)
        for _, config, _ in client.calls
    )
    assert client.update_attempts == []


def test_apply_returns_one_attempt_receipt_and_readback_metrics(monkeypatch):
    backfill = _backfill()
    _fake_job_config(monkeypatch, backfill)
    client = _Client(backfill)
    cutoff = datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc)

    result = backfill.run(client, cutoff_timestamp=cutoff, apply=True)

    assert result["rows_updated"] == 2
    assert result["readback"] == "passed"
    assert result["attempt_receipt"]["status"] == "readback_passed"
    assert result["attempt_receipt"]["attempt_id"] == result["attempt_receipt"]["update_job_id"]
    assert client.update_attempts == [result["attempt_receipt"]["update_job_id"]]
    assert [record["statement"] for record in result["query_jobs"]] == [
        "preview", "summary_before", "update", "summary_after"
    ]
    update = result["query_jobs"][2]
    assert update["query_id"] == result["attempt_receipt"]["update_job_id"]
    assert update["estimated_bytes"] == 10
    assert update["bytes_processed"] == 5
    assert update["affected_rows"] == 2


@pytest.mark.parametrize(
    "missing_stage, apply",
    (("preview", False), ("update", True)),
)
def test_executed_query_and_dml_jobs_require_returned_job_ids(monkeypatch, missing_stage, apply):
    backfill = _backfill()
    _fake_job_config(monkeypatch, backfill)
    client = _Client(backfill, missing_actual_job_ids={missing_stage})

    with pytest.raises(RuntimeError, match="actual job ID|operator inspection"):
        backfill.run(
            client,
            cutoff_timestamp=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc),
            apply=apply,
        )

    if apply:
        assert len(client.update_attempts) == 1


def test_late_write_or_row_drift_stops_after_one_apply_attempt(monkeypatch):
    backfill = _backfill()
    _fake_job_config(monkeypatch, backfill)
    client = _Client(backfill, summaries=[_BEFORE, dict(_AFTER, row_count=7)])

    with pytest.raises(RuntimeError, match="frozen-set readback.*no retry or rollback.*receipt=") as failure:
        backfill.run(
            client,
            cutoff_timestamp=datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc),
            apply=True,
        )

    assert len(client.update_attempts) == 1
    assert client.summary_calls == 2
    assert "summary_after" in str(failure.value)
    assert "affected_rows" in str(failure.value)
