"""Tests for core/collect/google_rss.py (Google's free daily trending searches feed) and for the Google search
terms that steer row 14's country searches (google_trends.queue_seeds and the collect job's seed path).

The feed body is a live sample of https://trends.google.com/trending/rss?geo=ZA saved on 4 October 2026.
No network: every read goes through a fake transport with the reader's (url, timeout, max_bytes) shape.
"""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from core.collect import chain, google_rss, google_trends, job, writers
from core.collect.gdelt import blocked
from core.collect.socialcrawl_client import PRICED, quote_for
from core.collect.tests.test_job import BLOCKED_SEED, FakeBQ, FakeClient, TUESDAY, collect, queue_seed, run_main
from core.public_feeds import reader

SAMPLE = (Path(__file__).resolve().parent / "fixtures" / "google_trends_rss_ZA.xml").read_bytes()
FETCHED = datetime(2026, 10, 4, 0, 5, tzinfo=timezone.utc)
ROBOTS = b"# trends.google.com/robots.txt\n\nUser-agent: *\nDisallow: /explore?\nDisallow: /trends/explore?\n"
SAMPLE_TERMS = ["f1 standings", "max verstappen", "news 24", "orlando pirates f.c.", "jake white",
                "african national congress", "f1", "diesel fuel",
                "madlanga commission extortion testimony sibanyoni", "f1 schedule"]


def feed(*titles, extra=""):
    items = "".join(
        f"<item><title>{t}</title><ht:approx_traffic>1000+</ht:approx_traffic>"
        f"<pubDate>Sun, 4 Oct 2026 03:30:00 -0700</pubDate></item>" for t in titles)
    return (f'<?xml version="1.0" encoding="UTF-8"?>{extra}<rss xmlns:ht="https://trends.google.com/trending/rss" '
            f'version="2.0"><channel><title>Daily Search Trends</title>{items}</channel></rss>').encode("utf-8")


class Transport:
    """The reader transport: robots.txt and one body per market, recording every URL and limit."""

    def __init__(self, bodies=None, robots=(200, ROBOTS), fail=()):
        self.bodies = bodies if bodies is not None else {m: (200, SAMPLE) for m in google_rss.MARKETS}
        self.robots, self.fail, self.calls = robots, set(fail), []

    def __call__(self, url, *, timeout, max_bytes):
        self.calls.append((url, timeout, max_bytes))
        if url == "https://trends.google.com/robots.txt":
            return self.robots[0], {}, self.robots[1]
        market = url.rsplit("=", 1)[-1]
        if market in self.fail:
            raise TimeoutError("request exceeded its total deadline")
        status, body = self.bodies[market]
        return status, {"content-type": "application/rss+xml"}, body


def read(transport):
    return google_rss.read_signals(transport, run_id="collect-test", run_date=date(2026, 10, 4),
                                   clock=lambda: FETCHED)


# The feed


def test_the_saved_sample_parses_into_ranked_google_rss_search_signals():
    signals, state = google_rss.parse(SAMPLE, "ZA", FETCHED)
    assert state == google_trends.SourceState("ok")
    assert [s.term for s in signals] == SAMPLE_TERMS
    assert [s.rank for s in signals] == list(range(1, 11))
    first = signals[0]
    assert (first.market, first.source, first.kind, first.fetched_at) == ("ZA", "google_rss", "daily", FETCHED)
    assert first.refreshed_at == "2026-10-04T10:30:00Z"  # pubDate Sun, 4 Oct 2026 03:30:00 -0700
    assert first.raw_payload["approx_traffic"] == "200+"
    assert first.raw_payload["geo"] == "ZA"
    assert len(first.raw_payload["news"]) == 3
    assert first.raw_payload["news"][0]["source"] == "Formula 1"
    assert first.as_consumer_row()["label"] == "Google search interest"


def test_one_read_a_market_through_the_bounded_reader_after_robots_txt():
    transport = Transport()
    batch = read(transport)
    urls = [url for url, _, _ in transport.calls]
    assert urls == ["https://trends.google.com/robots.txt"] + [
        f"https://trends.google.com/trending/rss?geo={m}" for m in ("ZA", "NG", "KE")]
    assert {(t, b) for _, t, b in transport.calls} == {(reader.TIMEOUT_SECONDS, reader.MAX_RESPONSE_BYTES)}
    assert {m: batch.states[m, "google_rss"].status for m in google_rss.MARKETS} == {
        "ZA": "ok", "NG": "ok", "KE": "ok"}
    assert len(batch.signals) == 30 and {s.source for s in batch.signals} == {"google_rss"}


def test_rows_fit_google_search_signals_without_a_schema_change():
    rows = read(Transport()).load_rows()
    columns = {"market", "term", "source", "kind", "fetched_at", "refreshed_at", "rank", "refresh_date",
               "raw_payload"}
    assert all(set(r) == columns for r in rows)
    assert all(r["source"] == "google_rss" and r["kind"] == "daily" and r["refresh_date"] is None for r in rows)
    assert all(isinstance(r["fetched_at"], str) and r["term"] and isinstance(r["raw_payload"], dict) for r in rows)
    bq = FakeBQ()
    assert writers.write_search_signals(bq, rows) == (30, None)
    assert {r["market"] for r in bq.loaded("google_search_signals")} == {"ZA", "NG", "KE"}


def test_a_rule_one_term_is_never_written_to_google_search_signals():
    rows = read(Transport()).load_rows()[:3]
    for row, term in zip(rows, ("gen z protests", "school holidays", "amapiano charts")):
        row["term"] = term
    bq = FakeBQ()
    assert writers.write_search_signals(bq, rows) == (1, None)
    assert [r["term"] for r in bq.loaded("google_search_signals")] == ["amapiano charts"]


def test_a_run_of_only_rule_one_terms_appends_nothing():
    rows = read(Transport()).load_rows()[:2]
    for row, term in zip(rows, ("gen z protests", "boomer memes")):
        row["term"] = term
    bq = FakeBQ()
    assert writers.write_search_signals(bq, rows) == (0, None)
    assert bq.loaded("google_search_signals") == []


def test_write_run_counts_the_rule_one_terms_it_held_back():
    from core.collect.tests.test_job import FakeClient, collect

    run = collect(FakeClient())
    rows = read(Transport()).load_rows()[:3]
    for row, term in zip(rows, ("gen z protests", "amapiano charts", "boomer memes")):
        row["term"] = term
    run.search_rows = rows
    bq = FakeBQ()
    written = writers.write_run(bq, run, "collect-1")
    assert written["google_search_signals"] == 1 and written["google_search_signals_blocked"] == 2
    assert [r["term"] for r in bq.loaded("google_search_signals")] == ["amapiano charts"]


def test_a_failed_market_is_recorded_and_never_stops_the_others():
    transport = Transport(bodies={"ZA": (302, b""), "NG": (200, b"<rss><channel>"), "KE": (200, SAMPLE)})
    batch = read(transport)
    assert batch.states["ZA", "google_rss"] == google_trends.SourceState("unavailable", "http_302")
    assert batch.states["NG", "google_rss"] == google_trends.SourceState("unavailable", "malformed_feed")
    assert batch.states["KE", "google_rss"].status == "ok"
    assert {s.market for s in batch.signals} == {"KE"}

    batch = read(Transport(fail={"NG"}))
    assert batch.states["NG", "google_rss"] == google_trends.SourceState("unknown", "transport_TimeoutError")
    assert {s.market for s in batch.signals} == {"ZA", "KE"}


def test_a_body_with_a_document_type_or_entity_is_refused_unparsed():
    entity = '<!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa">]>'
    signals, state = google_rss.parse(feed("&a;", extra=entity), "ZA", FETCHED)
    assert signals == [] and state == google_trends.SourceState("unavailable", "doctype_refused")


def test_an_empty_feed_and_an_over_limit_body_are_recorded():
    assert google_rss.parse(feed(), "ZA", FETCHED) == ([], google_trends.SourceState("empty", "no_items"))
    big = b" " * (reader.MAX_RESPONSE_BYTES + 1)
    batch = read(Transport(bodies={m: (200, big) for m in google_rss.MARKETS}))
    assert {s.reason for s in batch.states.values()} == {"body_over_limit"}


def test_robots_txt_that_cannot_be_read_means_no_feed_is_fetched():
    transport = Transport(robots=(403, b""))
    batch = read(transport)
    assert [url for url, _, _ in transport.calls] == ["https://trends.google.com/robots.txt"]
    assert batch.signals == ()
    assert {s.status for s in batch.states.values()} == {"not_made"}


# The seeds: Google terms steer which queries row 14 searches, never evidence


def signal_row(market, term, source="google_rss", kind="daily", rank=1, day=date(2026, 9, 28)):
    return {"market": market, "term": term, "source": source, "kind": kind, "rank": rank,
            "fetched_at": f"{day.isoformat()}T00:05:00Z"}


def test_queue_seeds_cap_dedupe_filter_and_label_each_market():
    rows = [signal_row("ZA", f"term {i}", rank=i) for i in range(1, 16)]
    rows += [signal_row("ZA", "Cyril Ramaphosa", rank=1, source="google_trending", kind="trending"),
             signal_row("ZA", "cyril-ramaphosa", rank=2, source="google_bq", kind="rising"),
             signal_row("ZA", "weather", rank=1, source="google_bq", kind="top"),
             signal_row("ZA", "fyp", rank=3, source="google_trending", kind="trending"),
             signal_row("NG", "Super Eagles", rank=1, source="google_bq", kind="rising"),
             signal_row("NG", "already queued", rank=2)]
    picked = google_trends.queue_seeds(rows, run_date=TUESDAY,
                                       existing_terms_by_market={"NG": ["Already-Queued"]})
    za = picked["ZA"]
    assert len(za) == google_trends.GOOGLE_SEEDS_PER_MARKET == 10
    assert [s["query"] for s in za[:3]] == ["term 1", "Cyril Ramaphosa", "term 2"]  # sources taken in turn
    assert "weather" not in {s["query"] for s in za}            # BigQuery Trends terms are never seeds
    assert "fyp" not in {s["query"] for s in za}                # the platform-generic stoplist holds
    assert "cyril-ramaphosa" not in {s["query"] for s in za}    # one cluster once
    assert [s["query"] for s in picked["NG"]] == []             # BigQuery rising is not a seed either
    assert picked["KE"] == []
    seed = za[0]
    hold = quote_for("tiktok/search/top", PRICED["tiktok/search/top"].method,
                     {"query": "term 1", "country": "ZA", "publish_time": "this-week", "seen": "x"})
    assert seed == {"seed_date": TUESDAY, "market": "ZA", "item_id": None, "query": "term 1", "kind": "google_rss",
                    "lane": "expansion", "priority": 0.0, "template": "tiktok/search/top", "ttl_days": 1,
                    "credits_estimate": float(hold), "yield_posts": None, "yield_new_creators": None,
                    "source": "google_rss"}


def test_queue_seeds_drop_rule_one_terms():
    assert blocked(BLOCKED_SEED)
    picked = google_trends.queue_seeds([signal_row("KE", BLOCKED_SEED), signal_row("KE", "Harambee Stars", rank=2)],
                                       run_date=TUESDAY, existing_terms_by_market={})
    assert [s["query"] for s in picked["KE"]] == ["Harambee Stars"]


def google_feed_transport(titles):
    bodies = {m: (200, feed(*[f"{m} {t}" for t in titles])) for m in google_rss.MARKETS}
    return Transport(bodies=bodies)


def row14(calls):
    return {m: sum(c["route"] == "tiktok/search/top" and c["market"] == m and c["seed_key"] is not None
                   for c in calls) for m in ("ZA", "NG", "KE")}


@pytest.mark.usefixtures("collect_share_2000")
def test_with_full_row14_slots_google_terms_replace_picks_and_add_no_call():
    queue = [queue_seed(f"queued {m} {i}", market=m, template="tiktok/search/top", day=TUESDAY)
             for m in ("ZA", "NG", "KE") for i in range(90)]
    base = FakeClient()
    collect(base, seeds=queue)
    assert row14(base.calls) == {m: job.search_calls(m) for m in ("ZA", "NG", "KE")}
    client = FakeClient()
    run = collect(client, seeds=queue, google_rss_transport=google_feed_transport(["amapiano night", "derby day"]))
    assert row14(client.calls) == row14(base.calls)
    assert len(client.calls) == len(base.calls)
    assert charged(client.calls) == charged(base.calls)
    searched = {(c["market"], c["params"]["query"]) for c in client.calls if c["route"] == "tiktok/search/top"}
    assert {(m, f"{m} {t}") for m in ("ZA", "NG", "KE") for t in ("amapiano night", "derby day")} <= searched
    assert run.counts()["google_seeds"] == {"ZA": 2, "NG": 2, "KE": 2}


def test_collect_puts_the_days_google_terms_into_unfilled_row14_slots_only():
    transport = google_feed_transport(["amapiano night", "derby day"])
    previous = [signal_row("ZA", "Load shedding", source="google_trending", kind="trending")]
    client = FakeClient()
    run = collect(client, google_rss_transport=transport, google_terms=lambda: previous)
    assert all(row14(client.calls)[m] <= job.search_calls(m) for m in ("ZA", "NG", "KE"))
    searched = {(c["market"], c["params"]["query"]) for c in client.calls if c["route"] == "tiktok/search/top"}
    assert {("ZA", "ZA amapiano night"), ("NG", "NG derby day"), ("KE", "KE amapiano night"),
            ("ZA", "Load shedding")} <= searched
    google = [c for c in client.calls if c["route"] == "tiktok/search/top"
              and c["params"]["query"] in {"ZA amapiano night", "Load shedding"}]
    assert all(c["params"]["country"] == c["market"] == "ZA" for c in google)
    assert {r["source"] for r in run.search_rows} == {"google_rss"}
    counts = run.counts()
    assert counts["google_rss_signals"] == 6
    assert counts["google_rss_states"] == {"ZA": "ok", "NG": "ok", "KE": "ok"}
    assert counts["google_seeds"] == {"ZA": 3, "NG": 2, "KE": 2}
    assert counts["google_terms_read"] == 1 and counts["google_terms_error"] is None


def charged(calls):
    return sum(quote_for(c["route"], PRICED[c["route"]].method, c["params"]) for c in calls)


def test_google_seed_calls_are_ordinary_row14_searches_and_never_evidence():
    client = FakeClient()
    run = collect(client, google_rss_transport=google_feed_transport(["derby day"]))
    rows = [row for row in run.seed_rows if row["kind"] == "google_rss"]
    assert rows and all(r["template"] == "tiktok/search/top" and r["ttl_days"] == 0 for r in rows)
    calls = [c for c in client.calls if c["route"] == "tiktok/search/top" and c["params"]["query"].endswith("derby day")]
    assert len(calls) == 3 and all(c["lane"] == "expansion" for c in calls)
    # The same provenance as every other row 14 country search: the term changes nothing about locality.
    google_keys = {c["seed_key"] for c in calls}
    other_keys = {c["seed_key"] for c in client.calls if c["route"] == "tiktok/search/top" and c["seed_key"]} - google_keys
    provenance = lambda keys: {(o["market"], o["source_market"], o["source_region"], o["lane_class"])
                               for o in run.observations if o["seed_key"] in keys}
    assert provenance(google_keys) and provenance(google_keys) <= provenance(other_keys)
    google_posts = {o["post_id"] for o in run.observations if o["seed_key"] in google_keys}
    assert all(p["geo_source"] in (None, "ext_region") for p in run.posts if p["post_id"] in google_posts)


def test_a_failing_google_terms_read_or_feed_never_stops_collect():
    def broken():
        raise RuntimeError("table gone")

    client = FakeClient()
    run = collect(client, google_rss_transport=Transport(fail={"ZA", "NG", "KE"}), google_terms=broken)
    counts = run.counts()
    assert counts["google_terms_error"] == "RuntimeError: table gone"
    assert counts["google_rss_states"] == {"ZA": "unknown", "NG": "unknown", "KE": "unknown"}
    assert counts["google_seeds"] == {"ZA": 0, "NG": 0, "KE": 0}
    assert counts["stopped"] is None and run.search_rows == []


def test_main_reads_the_previous_days_google_rows_and_the_feed_before_the_expansion_phase():
    class SignalsBQ(FakeBQ):
        def query(self, sql, job_config=None, **kw):
            if "google_search_signals" in sql:
                params = {p.name: p for p in (job_config.query_parameters if job_config else [])}
                self.queries.append((sql, params))
                return type("Job", (), {"result": lambda self, **k: rows})()
            return super().query(sql, job_config, **kw)

    rows = [signal_row("NG", "Super Eagles", source="google_trending", kind="trending")]
    bq, client, runs = SignalsBQ(), FakeClient(), chain.MemoryRunsStore()
    transport = google_feed_transport(["derby day"])
    assert run_main(["--run-date", "2026-09-29"], bq=bq, client=client, runs=runs,
                    public_feed_transport=transport) == 0
    [(sql, params)] = [(s, p) for s, p in bq.queries if "google_search_signals" in s]
    assert params["d"].value == TUESDAY and "DATE_SUB(@d, INTERVAL 1 DAY)" in sql
    assert "'google_rss'" in sql and "google_bq" not in sql
    searched = {(c["market"], c["params"]["query"]) for c in client.calls if c["route"] == "tiktok/search/top"}
    assert {("NG", "Super Eagles"), ("ZA", "ZA derby day")} <= searched
    assert {r["source"] for r in bq.loaded("google_search_signals")} >= {"google_rss"}
    counts = runs.rows[-1]["counts"]
    assert runs.rows[-1]["status"] == "ok" and counts["google_seeds"]["NG"] == 2
    assert counts["credits_charged"] == sum(
        quote_for(c["route"], PRICED[c["route"]].method, c["params"]) for c in client.calls)


def test_main_goes_on_when_the_google_rows_read_fails():
    runs = chain.MemoryRunsStore()
    assert run_main(["--run-date", "2026-09-29"], runs=runs) == 0  # FakeBQ refuses the unexpected read
    counts = runs.rows[-1]["counts"]
    assert runs.rows[-1]["status"] == "ok"
    assert counts["google_terms_error"].startswith("AssertionError")


@pytest.mark.usefixtures("collect_share_2000")
def test_plan_reads_no_bigquery_and_its_placeholders_take_existing_row14_slots():
    planned = job.plan(TUESDAY)
    for market in ("ZA", "NG", "KE"):
        google = [c for c in planned["calls"][market] if c.seed and c.seed.kind == "google_search"]
        assert google and all(c.row == "14" and c.route == "tiktok/search/top" for c in google)
        assert all(c.params["query"].startswith(f"<{market} Google search term") for c in google)
        assert len(google) <= google_trends.GOOGLE_SEEDS_PER_MARKET
        assert sum(c.row == "14" for c in planned["calls"][market]) == job.search_calls(market)


def test_queue_seeds_leave_out_fixtures_between_two_other_national_teams():
    rows = [signal_row("ZA", "croatia vs england", rank=1, source="google_trending", kind="trending"),
            signal_row("ZA", "Wales v Norway national football team", rank=2, source="google_trending",
                       kind="trending"),
            signal_row("ZA", "eritrea vs south africa", rank=3, source="google_trending", kind="trending"),
            signal_row("ZA", "kaizer chiefs vs orlando pirates", rank=4, source="google_trending", kind="trending"),
            signal_row("ZA", "Bafana Bafana vs Lesotho", rank=5, source="google_trending", kind="trending"),
            signal_row("NG", "ghana vs senegal", rank=1, source="google_trending", kind="trending"),
            signal_row("NG", "super eagles vs benin", rank=2, source="google_trending", kind="trending"),
            signal_row("KE", "uganda versus tanzania", rank=1, source="google_trending", kind="trending"),
            signal_row("KE", "harambee stars vs gabon", rank=2, source="google_trending", kind="trending")]
    picked = google_trends.queue_seeds(rows, run_date=TUESDAY, existing_terms_by_market={})
    assert [s["query"] for s in picked["ZA"]] == ["eritrea vs south africa", "kaizer chiefs vs orlando pirates",
                                                  "Bafana Bafana vs Lesotho"]
    assert [s["query"] for s in picked["NG"]] == ["super eagles vs benin"]
    assert [s["query"] for s in picked["KE"]] == ["harambee stars vs gabon"]
