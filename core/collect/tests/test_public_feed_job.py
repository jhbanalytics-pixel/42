import json
from collections import Counter
from urllib.parse import urlsplit

from core.collect import chain, job, public_feed_collect
from core.collect.tests.test_job import (BothClient, FakeBQ, FakeGeo, FakeGet, FakeJobs, NOW, TUESDAY,
                                         fake_item_id)
from core.public_feeds.catalog import confirmed_feeds


SABC_URL = "https://www.sabcnews.com/sabcnews/"
SABC_PAGE = b'<h2><a href="/sabcnews/public-interest/">A public interest headline</a></h2>'
TURN_TABLE_URL = "https://www.turntablecharts.com/charts/1"
TURN_TABLE_PAGE = (b'<table><tbody><tr><td><p class="rank">1</p></td>'
                   b'<td><p class="title">A Song</p></td><td><p class="artist">A Singer</p></td>'
                   b'</tr></tbody></table>')
_OMIT_TRANSPORT = object()


def _transport():
    calls = []

    def transport(url, *, timeout, max_bytes):
        calls.append(url)
        if url.endswith("/robots.txt"):
            if urlsplit(url).hostname in {"www.sabcnews.com", "www.turntablecharts.com"}:
                return 200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\n"
            return 403, {}, b""
        if url == SABC_URL:
            return 200, {"content-type": "text/html; charset=utf-8"}, SABC_PAGE
        if url == TURN_TABLE_URL:
            return 200, {"content-type": "text/html; charset=utf-8"}, TURN_TABLE_PAGE
        raise AssertionError(f"unexpected feed URL: {url}")

    return calls, transport


def _main(bq, runs, jobs, client, transport=_OMIT_TRANSPORT, *, env=None):
    options = {} if transport is _OMIT_TRANSPORT else {"public_feed_transport": transport}
    return job.main(["--run-date", TUESDAY.isoformat()], env=env or {}, runs=runs, jobs=jobs, bq=bq,
                    make_client=lambda run_id, caps: client, fns=(fake_item_id, FakeGeo()), clock=lambda: NOW,
                    get=FakeGet(), **options)


def test_plan_names_each_confirmed_public_feed_as_a_zero_credit_read(capsys):
    assert job.main(["--plan", "--run-date", "2026-09-30"], env={}, bq=object(), runs=object(), jobs=object()) == 0

    out = capsys.readouterr().out
    feeds = confirmed_feeds()
    assert len(feeds) == 43
    assert "public feeds: 43 confirmed reads, 0 SocialCrawl credits" in out
    for feed in feeds:
        assert f"{feed.market} {feed.name} {feed.url}" in out


def test_main_collects_confirmed_feeds_and_writes_safe_rows_without_charging_socialcrawl():
    calls, transport = _transport()
    bq, runs, jobs, client = FakeBQ(), chain.MemoryRunsStore(), FakeJobs(), BothClient()

    assert _main(bq, runs, jobs, client, transport) == 0

    counts = runs.rows[-1]["counts"]
    expected_by_market = dict(Counter(feed.market for feed in confirmed_feeds()))
    assert counts["public_feed_summary"]["feeds_planned"] == 43
    assert counts["public_feed_summary"]["feeds_failed"] == 41
    assert counts["public_feed_summary"]["statuses"] == {"robots_disallowed": 41, "ok": 2}
    assert counts["public_feed_records_by_market"] == expected_by_market
    assert counts["public_feed_raw_rows"] == 43
    assert counts["public_feed_raw_error"] is None
    assert counts["credits_charged"] == sum(row["credits"] for row in client.charged) + counts["local_credits"]
    assert all(row["credits_quoted"] == row["credits_charged"] == 0 for row in bq.loaded("raw_responses"))
    assert SABC_URL in calls

    feeds = bq.loaded("raw_responses")
    assert len(feeds) == 43
    assert {row["route"] for row in feeds} == {"public_feed"}
    assert {row["lane"] for row in feeds} == {"public_feed"}
    assert Counter(row["market"] for row in feeds) == expected_by_market
    successful = {row["seed_key"]: row for row in feeds if row["body"]["accepted_count"]}
    assert set(successful) == {"za_sabc_news", "ng_turntable_top_100"}
    assert successful["za_sabc_news"]["body"]["accepted_count"] == 1
    assert successful["ng_turntable_top_100"]["body"]["accepted_count"] == 1
    assert successful["ng_turntable_top_100"]["body"]["safe_entries"][0]["source_market"] == "NG"
    assert any(row.get("label") == "A Singer - A Song" for row in bq.mapped)
    safe = successful["za_sabc_news"]["body"]["safe_entries"][0]
    assert safe["source_market"] == "ZA" and safe["source_feed_id"] == "za_sabc_news"
    assert set(safe) == {"kind", "platform", "post_id", "url", "text", "published_at", "source_market",
                         "source_feed_id", "source_url", "observed_at", "artist", "item_key", "rank",
                         "time_text", "reason"}

    public_posts = [post for post in bq.posts.values() if post["source_regime"] == "public_feed"]
    assert len(public_posts) == 1
    assert public_posts[0]["geo_market"] is None and public_posts[0]["geo_confidence"] is None
    assert public_posts[0]["creator_id"] is None and "age" not in public_posts[0]
    public_observations = [row for row in bq.loaded("post_observations") if row["route"] == "public_feed"]
    assert len(public_observations) == 1
    assert (public_observations[0]["source_market"], public_observations[0]["source_region"]) == ("ZA", None)

    health = [row for row in bq.loaded("collection_health") if row["route"] == "public_feed"]
    assert len(health) == 43
    assert Counter(row["market"] for row in health) == expected_by_market


def test_live_main_uses_the_bounded_reader_transport_by_default(monkeypatch):
    calls, transport = _transport()
    monkeypatch.setattr(public_feed_collect, "https_transport", transport)
    bq, runs, jobs, client = FakeBQ(), chain.MemoryRunsStore(), FakeJobs(), BothClient()

    assert _main(bq, runs, jobs, client) == 0

    assert SABC_URL in calls
    assert runs.rows[-1]["counts"]["public_feed_summary"]["feeds_planned"] == 43


def test_public_feed_reads_stay_outside_the_current_150_credit_cap():
    calls, transport = _transport()
    bq, runs, jobs, client = FakeBQ(), chain.MemoryRunsStore(), FakeJobs(), BothClient()

    assert _main(bq, runs, jobs, client, transport, env={"COLLECT_CAP_OVERRIDE": "150"}) == 0

    counts = runs.rows[-1]["counts"]
    assert counts["credits_charged"] <= 150
    assert counts["public_feed_summary"]["feeds_planned"] == 43
    assert calls
    assert all(row["credits_charged"] == 0 for row in bq.loaded("raw_responses"))


def test_stopped_collect_records_feed_holds_without_calling_the_transport():
    calls, transport = _transport()
    client = BothClient(lambda n, route, market: "insufficient_credits" if n == 1 else None)
    run = job.collect(client, TUESDAY, "stopped-test", item_id_fn=fake_item_id, geo_fn=FakeGeo(),
                      clock=lambda: NOW, local_get=FakeGet(), public_feed_transport=transport)

    assert run.stopped == "insufficient_credits"
    assert calls == []
    assert run.public_feed_summary["feeds_planned"] == 43
    assert run.public_feed_summary["feeds_attempted"] == 0
    records = [record for record in run.records if record["route"] == "public_feed"]
    assert len(records) == 43
    assert {record["status"] for record in records} == {"stopped"}
    assert all(record["calls"] == 0 for record in records)


def test_pure_collect_without_transport_leaves_public_feeds_inert():
    run = job.collect(BothClient(), TUESDAY, "pure-test", item_id_fn=fake_item_id, geo_fn=FakeGeo(),
                      clock=lambda: NOW)

    assert run.public_feed_summary is None
    assert run.public_feed_raw_rows == []
    assert not [record for record in run.records if record["route"] == "public_feed"]


def test_generic_raw_backfill_does_not_replay_public_feed_rows():
    assert "public_feed" not in job.BACKFILL_ROUTES


def test_public_feed_raw_write_failure_fails_collect_with_a_safe_error_category():
    class BrokenRawBQ(FakeBQ):
        def load_table_from_json(self, rows, table_id, job_config=None):
            if table_id.endswith(".raw_responses"):
                raise RuntimeError("private backend detail")
            return super().load_table_from_json(rows, table_id, job_config)

    _, transport = _transport()
    bq, runs, jobs, client = BrokenRawBQ(), chain.MemoryRunsStore(), FakeJobs(), BothClient()

    assert _main(bq, runs, jobs, client, transport) == 1

    final = runs.rows[-1]
    assert final["status"] == "failed"
    assert final["error"] == "public_feed_raw_append_failed"
    assert final["counts"]["public_feed_raw_error"] == "append_failed"
    assert "private backend detail" not in json.dumps(final)
    assert jobs.started == []


def test_public_feed_merge_failure_fails_collect_without_advancing_chain():
    class BrokenPublicMergeBQ(FakeBQ):
        def query(self, sql, job_config=None, **kw):
            if "COALESCE(T." in sql:
                raise RuntimeError("private backend detail")
            return super().query(sql, job_config=job_config, **kw)

    _, transport = _transport()
    bq, runs, jobs, client = BrokenPublicMergeBQ(), chain.MemoryRunsStore(), FakeJobs(), BothClient()

    assert _main(bq, runs, jobs, client, transport) == 1

    final = runs.rows[-1]
    assert final["status"] == "failed"
    assert final["error"] == "public_feed_write_failed"
    assert final["counts"]["public_feed_write_error"] == "public_feed_write_failed"
    assert "private backend detail" not in json.dumps(final)
    assert jobs.started == []
