from datetime import date, datetime, timezone
import logging
from types import SimpleNamespace

import traceback

import pytest

from core.collect import parse, writers


class FakeJob:
    def __init__(self, rows=(), total_bytes_billed=0):
        self.rows = list(rows)
        self.total_bytes_billed = total_bytes_billed
        self.result_kwargs = None

    def result(self, **kwargs):
        self.result_kwargs = kwargs
        return self.rows


class FakeTable:
    schema = []


class FakeBQ:
    def __init__(self):
        self.posts = {}
        self.post_merge_sql = []
        self.queries = []
        self.query_options = []
        self.query_configs = []
        self.jobs = []
        self.billed_bytes = []
        self.default_query_job_config = None
        self.pull_rows = []
        self.loads = []
        self.table_reads = []
        self.fail_raw = False
        self.raw_error = None
        self.public_error = None

    def query(self, sql, job_config=None, **kwargs):
        params = {param.name: param for param in (job_config.query_parameters if job_config else [])}
        self.queries.append((sql, params))
        self.query_options.append(kwargs)
        self.query_configs.append(job_config)
        if "MAX(c.pull_seq)" in sql:
            assert "route = 'public_feed'" in sql
            counter_series = set(params["public_feed_counter_series"].values)
            observation_series = set(params["public_feed_observation_series"].values)
            legacy_series = set(params["series"].values)
            legacy_observation_series = set(params["observation_series"].values)
            maxima = {}
            for row in self.pull_rows:
                if row["kind"] == "counter":
                    included = row["series"] in legacy_series or (
                        row["route"] == "public_feed" and row["series"] in counter_series)
                else:
                    included = (
                        row["lane_class"] == "unbiased_rank" and row["series"] in legacy_observation_series
                    ) or (row["route"] == "public_feed" and row["series"] in observation_series)
                if not included:
                    continue
                key = (row["market"], row["series"], row["protocol"])
                maxima[key] = max(maxima.get(key, 0), row["pull_seq"])
            return FakeJob([{"market": market, "series": series, "protocol": protocol, "last_pull": pull}
                            for (market, series, protocol), pull in maxima.items()])
        if "collection_health" in sql:
            return FakeJob()
        if sql.lstrip().upper().startswith("MERGE") and writers.table("posts") in sql:
            self.post_merge_sql.append(sql)
            public_feed = "COALESCE(T.text, S.text)" in sql
            if public_feed and self.public_error is not None:
                raise self.public_error
            rows = [value.struct_values for value in params["rows"].values]
            post_ids = [row["post_id"] for row in rows]
            assert len(post_ids) == len(set(post_ids))
            for row in rows:
                stored = self.posts.get(row["post_id"])
                if stored is None:
                    self.posts[row["post_id"]] = dict(row)
                elif public_feed:
                    for field, value in row.items():
                        if field != "post_id" and stored[field] is None and value is not None:
                            stored[field] = value
                else:
                    if any(row[field] is not None for field in ("views", "likes", "comments", "shares")):
                        stored.update({field: row[field] for field in writers.METRICS})
                    if stored["geo_market"] is None and row["geo_market"] is not None:
                        stored.update({field: row[field] for field in writers.GEO})
            billed = self.billed_bytes.pop(0) if public_feed and self.billed_bytes else 0
            job = FakeJob(total_bytes_billed=billed)
            self.jobs.append(job)
            return job
        raise AssertionError(f"unexpected query: {sql}")

    def get_table(self, table_id):
        self.table_reads.append(table_id)
        return FakeTable()

    def load_table_from_json(self, rows, table_id, job_config=None):
        if self.fail_raw and table_id == writers.table("raw_responses"):
            raise self.raw_error or RuntimeError("raw_responses load failed")
        self.loads.append((table_id, [dict(row) for row in rows]))
        return FakeJob()

    def loaded(self, table_name):
        return [row for table_id, rows in self.loads if table_id.endswith("." + table_name) for row in rows]


def public_post(post_id, **values):
    row = dict.fromkeys(writers.POST_TYPES)
    row.update({"post_id": post_id, "platform": "news", "vendor": "public_feed",
                "source_regime": "public_feed", "run_id": "run-1"})
    row.update(values)
    return row


def legacy_post(post_id, **values):
    row = public_post(post_id, vendor="socialcrawl", source_regime="socialcrawl")
    row.update(values)
    return row


def observation(post_id, feed_id, day):
    row = dict.fromkeys(parse.OBSERVATION_COLUMNS)
    row.update({
        "post_id": post_id,
        "observed_at": datetime.fromisoformat(f"{day}T12:00:00+00:00"),
        "observed_date": date.fromisoformat(day),
        "market": "ZA",
        "platform": "news",
        "route": "public_feed/rss",
        "series": "public_feed_news",
        "protocol": f"public_feed:{feed_id}",
        "lane": "public_feed",
        "lane_class": "news",
        "seed_key": feed_id,
        "run_id": f"run-{day}",
    })
    return row


def raw_row(feed_id, day):
    return {
        "run_id": f"run-{day}",
        "job": "collect",
        "market": "ZA",
        "route": "public_feed/rss",
        "params_hash": "feed-hash",
        "lane": "public_feed",
        "seed_key": feed_id,
        "fetched_at": datetime.fromisoformat(f"{day}T12:00:00+00:00"),
        "http_status": 200,
        "credits_quoted": 0,
        "credits_charged": 0,
        "cache_hit": False,
        "body": {"safe_entries": [], "row_counts": {"accepted": 0, "rejected": 0}},
    }


def health_record(post_id, day, feed_id):
    return {
        "day": day,
        "market": "ZA",
        "platform": "news",
        "route": "public_feed/rss",
        "series": "public_feed_news",
        "protocol": f"public_feed:{feed_id}",
        "lane_class": "news",
        "calls": 1,
        "ok": True,
        "units_planned": 0,
        "units_ok": 0,
        "items": 1,
        "post_ids": [post_id],
    }


def collected(*, posts=(), observations=(), records=(), raw_rows=()):
    return SimpleNamespace(
        posts=list(posts),
        observations=list(observations),
        counters=[],
        records=list(records),
        items={},
        creators=[],
        public_feed_raw_rows=list(raw_rows),
    )


def test_public_feed_merge_preserves_existing_values_fills_nulls_and_inserts():
    bq = FakeBQ()
    original = public_post(
        "article-1", text="Original headline", url="https://news.example/a",
        published_at=datetime(2026, 9, 28, tzinfo=timezone.utc), post_date=date(2026, 9, 28),
        geo_market="ZA", geo_confidence=0.9, geo_source="ext_region",
    )
    assert writers.merge_public_feed_posts(bq, [original]) == 1

    changed = public_post(
        "article-1", text="Changed headline", url="https://news.example/changed",
        published_at=datetime(2026, 9, 29, tzinfo=timezone.utc), post_date=date(2026, 9, 29),
        geo_market="NG", geo_confidence=0.8, geo_source="place_mention", transcript="Added transcript",
    )
    missing = public_post("article-2", url="https://news.example/b")
    later = public_post("article-2", text="New headline", post_date=date(2026, 9, 29))
    assert writers.merge_public_feed_posts(bq, [changed, missing, later]) == 1

    kept = bq.posts["article-1"]
    assert (kept["text"], kept["url"], kept["post_date"], kept["published_at"]) == (
        "Original headline", "https://news.example/a", date(2026, 9, 28),
        datetime(2026, 9, 28, tzinfo=timezone.utc),
    )
    assert (kept["geo_market"], kept["geo_confidence"], kept["geo_source"]) == ("ZA", 0.9, "ext_region")
    assert kept["transcript"] == "Added transcript"
    assert bq.posts["article-2"]["text"] == "New headline"
    assert bq.posts["article-2"]["post_date"] == date(2026, 9, 29)

    sql = bq.post_merge_sql[-1]
    assert "ON T.post_id = S.post_id" in sql
    for field in ("text", "url", "published_at", "post_date", "geo_market", "geo_confidence", "geo_source"):
        assert f"{field} = COALESCE(T.{field}, S.{field})" in sql
    assert "vendor_labels = COALESCE(T.vendor_labels, SAFE.PARSE_JSON(S.vendor_labels))" in sql


def test_write_run_keeps_feed_observations_separate_and_post_id_stable_across_days():
    bq = FakeBQ()
    url = "https://news.example/shared"
    first = collected(
        posts=[public_post("article-shared", url=url, text="First headline"),
               legacy_post("legacy-post", views=10)],
        observations=[observation("article-shared", "feed-a", "2026-09-29"),
                      observation("article-shared", "feed-b", "2026-09-29")],
        records=[health_record("article-shared", "2026-09-29", "feed-a")],
        raw_rows=[raw_row("feed-a", "2026-09-29"), raw_row("feed-b", "2026-09-29")],
    )
    written = writers.write_run(bq, first, "run-2026-09-29")

    second = collected(
        posts=[public_post("article-shared", url=url, text="Later headline")],
        observations=[observation("article-shared", "feed-c", "2026-09-30")],
    )
    second.posts[0]["published_at"] = None
    second.posts[0]["post_date"] = None
    later_written = writers.write_run(bq, second, "run-2026-09-30")

    assert written["public_feed_raw_rows"] == 2 and written["public_feed_raw_error"] is None
    assert written["public_feed_merge_statements"] == 1 and later_written["public_feed_raw_rows"] == 0
    assert bq.posts["article-shared"]["text"] == "First headline"
    assert bq.posts["article-shared"]["published_at"] is None
    assert len(bq.posts) == 2
    observations = bq.loaded("post_observations")
    assert [row["seed_key"] for row in observations] == ["feed-a", "feed-b", "feed-c"]
    assert [row["protocol"] for row in observations] == [
        "public_feed:feed-a", "public_feed:feed-b", "public_feed:feed-c",
    ]
    assert all(row["source_market"] is None and row["source_region"] is None for row in observations)
    assert [row["seed_key"] for row in bq.loaded("raw_responses")] == ["feed-a", "feed-b"]
    assert bq.loaded("collection_health")[0]["protocol"] == "public_feed:feed-a"
    legacy_sql = writers.MERGE_SQL.format(table=writers.table("posts"))
    assert legacy_sql in bq.post_merge_sql
    legacy_options = [options for (sql, _), options in zip(bq.queries, bq.query_options) if sql == legacy_sql]
    assert legacy_options == [{}]


def test_legacy_run_with_no_public_raw_rows_adds_no_bigquery_calls():
    bq = FakeBQ()
    run = SimpleNamespace(posts=[], observations=[], counters=[], records=[], items={}, creators=[])

    written = writers.write_run(bq, run, "legacy-run")

    assert written["public_feed_raw_rows"] == 0 and written["public_feed_raw_error"] is None
    assert not bq.queries and not bq.loads and not bq.table_reads


def test_raw_response_write_failure_is_returned_without_logging_backend_details(caplog):
    bq = FakeBQ()
    bq.fail_raw = True
    bq.raw_error = RuntimeError("backend token=private query=SELECT sensitive_body FROM internal_table")
    run = collected(raw_rows=[raw_row("feed-a", "2026-09-29")])

    with caplog.at_level(logging.ERROR, logger=writers.log.name):
        written = writers.write_run(bq, run, "run-2026-09-29")

    assert written["public_feed_raw_rows"] == 0
    assert written["public_feed_raw_error"].startswith("RuntimeError: backend token=private")
    assert "public_feed_raw_write_failed" in caplog.text
    assert "backend" not in caplog.text and "private" not in caplog.text and "sensitive_body" not in caplog.text


def test_public_feed_batches_use_remaining_and_lower_default_caps_without_retries(monkeypatch):
    from google.cloud import bigquery

    assert writers.PUBLIC_FEED_MERGE_BUDGET_BYTES == 5 * 1024 ** 3
    monkeypatch.setattr(writers, "PUBLIC_FEED_MERGE_BUDGET_BYTES", 100)
    bq = FakeBQ()
    bq.default_query_job_config = bigquery.QueryJobConfig(maximum_bytes_billed=80)
    bq.billed_bytes = [60, 40]

    assert writers.merge_public_feed_posts(
        bq, [public_post("cost-1"), public_post("cost-2")], batch=1,
    ) == 2

    assert [config.maximum_bytes_billed for config in bq.query_configs] == [80, 40]
    assert bq.query_options == [{"retry": None, "job_retry": None}] * 2
    assert [job.result_kwargs for job in bq.jobs] == [
        {"retry": None, "job_retry": None}, {"retry": None, "job_retry": None},
    ]


def test_public_feed_merge_stops_before_next_batch_when_aggregate_budget_is_exhausted(monkeypatch):
    monkeypatch.setattr(writers, "PUBLIC_FEED_MERGE_BUDGET_BYTES", 100)
    bq = FakeBQ()
    bq.billed_bytes = [100]

    with pytest.raises(writers.PublicFeedWriteError, match="public_feed_write_failed"):
        writers.merge_public_feed_posts(
            bq, [public_post("cost-1"), public_post("cost-2")], batch=1,
        )

    assert len(bq.jobs) == 1
    assert len(bq.post_merge_sql) == 1


def test_public_feed_merge_fails_closed_when_billed_bytes_are_unavailable(monkeypatch):
    monkeypatch.setattr(writers, "PUBLIC_FEED_MERGE_BUDGET_BYTES", 100)
    bq = FakeBQ()
    bq.billed_bytes = [None]

    with pytest.raises(writers.PublicFeedWriteError, match="public_feed_write_failed"):
        writers.merge_public_feed_posts(
            bq, [public_post("cost-1"), public_post("cost-2")], batch=1,
        )

    assert len(bq.jobs) == 1


def test_public_feed_merge_fails_closed_when_actual_bytes_exceed_the_cap(monkeypatch):
    monkeypatch.setattr(writers, "PUBLIC_FEED_MERGE_BUDGET_BYTES", 100)
    bq = FakeBQ()
    bq.billed_bytes = [101]

    with pytest.raises(writers.PublicFeedWriteError, match="public_feed_write_failed"):
        writers.merge_public_feed_posts(
            bq, [public_post("cost-1"), public_post("cost-2")], batch=1,
        )

    assert len(bq.jobs) == 1


def test_public_feed_write_error_hides_message_details():
    bq = FakeBQ()
    bq.public_error = RuntimeError("backend token=private query=SELECT sensitive_body FROM internal_table")

    with pytest.raises(writers.PublicFeedWriteError) as caught:
        writers.merge_public_feed_posts(bq, [public_post("secret-error-test")])

    error = caught.value
    rendered = "".join(traceback.format_exception(error))
    assert error.category == "public_feed_write_failed"
    assert str(error) == error.category
    assert error._original_exception is bq.public_error
    assert error.__cause__ is None and error.__suppress_context__
    assert "backend token=private" not in rendered
    assert "SELECT sensitive_body" not in rendered
    assert "internal_table" not in rendered


def test_last_pulls_advances_route_scoped_public_feed_chart_and_news_keys():
    bq = FakeBQ()
    chart_protocol = "public_feed?feed_id=chart-a&url=https%3A%2F%2Ffeeds.example%2Fchart-a"
    playlist_protocol = "public_feed?feed_id=playlist-a&url=https%3A%2F%2Ffeeds.example%2Fplaylist-a"
    news_protocol = "public_feed?feed_id=news-a&url=https%3A%2F%2Ffeeds.example%2Fnews-a"

    def public_rows(chart_pull, playlist_pull, news_pull):
        return [
            {"kind": "counter", "market": "ZA", "series": "board_music_country", "protocol": chart_protocol,
             "route": "public_feed", "pull_seq": chart_pull},
            {"kind": "counter", "market": "KE", "series": "radio_playlist", "protocol": playlist_protocol,
             "route": "public_feed", "pull_seq": playlist_pull},
            {"kind": "observation", "market": "NG", "series": "news_rss", "protocol": news_protocol,
             "route": "public_feed", "lane_class": "context", "pull_seq": news_pull},
            {"kind": "counter", "market": "ZA", "series": "board_music_country", "protocol": "wrong-route",
             "route": "legacy", "pull_seq": 99},
            {"kind": "observation", "market": "NG", "series": "news_rss", "protocol": "wrong-route",
             "route": "legacy", "lane_class": "context", "pull_seq": 99},
        ]

    bq.pull_rows = public_rows(1, 4, 1)
    first = writers.last_pulls(bq, date(2026, 9, 30))
    bq.pull_rows = public_rows(2, 5, 2)
    second = writers.last_pulls(bq, date(2026, 9, 30))

    chart_key = ("ZA", "board_music_country", chart_protocol)
    playlist_key = ("KE", "radio_playlist", playlist_protocol)
    news_key = ("NG", "news_rss", news_protocol)
    assert first == {chart_key: 1, playlist_key: 4, news_key: 1}
    assert second == {chart_key: 2, playlist_key: 5, news_key: 2}
    assert second[chart_key] > first[chart_key] and second[news_key] > first[news_key]
    assert len(bq.queries) == 2
    for sql, params in bq.queries:
        assert "route = 'public_feed'" in sql
        assert set(params["public_feed_counter_series"].values) == {"board_music_country", "radio_playlist"}
        assert params["public_feed_observation_series"].values == ["news_rss"]
