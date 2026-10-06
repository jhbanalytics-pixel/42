import json
import random
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.collect import google_trends as trends, seeds
from core.collect.job import seed_params
from core.collect.socialcrawl_client import PRICED, params_hash, quote_for


RUN_ID = "collect-20261001-google-trends-fixture"
RUN_DATE = date(2026, 10, 1)
RUN_AT = datetime(2026, 10, 1, 4, 12, tzinfo=timezone.utc)
DOC_RESPONSE_PROVENANCE = {
    "source_url": "https://www.socialcrawl.dev/docs/google_trends",
    "retrieved_at_utc": "2026-10-01T14:12:05Z",
    "fixture_kind": "official_publisher_sample_not_native",
}
DOC_RESPONSE_NOT_NATIVE = {
    "success": True,
    "data": {
        "items": [
            {
                "rank": 1,
                "title": "judith und mel",
                "search_volume": 50000,
                "increase_percent": 1000,
                "started_at": "2026-09-12T11:10:00.000Z",
                "ended_at": None,
                "active": True,
                "categories": ["entertainment"],
                "breakdown": ["mel jersey", "t online"],
                "news": [
                    {
                        "title": "Nach Herzinfarkt und Hausbrand: Schlagerstar schwer gestürzt",
                        "url": "https://www.t-online.de/unterhaltung/stars/id_101432554/judith-und-mel-nach-herzinfarkt-und-hausbrand-schlagerstar-gestuerzt.html",
                        "source": "T-Online",
                        "domain": "t-online.de",
                        "image_url": None,
                        "published_at": "2026-09-12T09:44:47.000Z",
                    }
                ],
            }
        ],
        "total": 431,
    },
}


class FakeClient:
    def __init__(self, results=(), *, run_id=RUN_ID, share="collect"):
        self.run_id = run_id
        self.share = share
        self.results = list(results)
        self.calls = []

    def call(self, route, params=None, **kwargs):
        self.calls.append((route, dict(params or {}), kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def sc_result(status="ok", body=DOC_RESPONSE_NOT_NATIVE, http_status=200, reason=""):
    return SimpleNamespace(
        status=status, body=body, http_status=http_status, reason=reason
    )


def collect(client, *, fetched_at=RUN_AT, raw_receipts=(), ledger_receipts=()):
    return trends.collect_search_signals(
        client,
        run_id=RUN_ID,
        run_date=RUN_DATE,
        fetched_at=fetched_at,
        raw_receipts=raw_receipts,
        ledger_receipts=ledger_receipts,
    )


def receipt_hash(market):
    return params_hash("GET", {"location": market, "hours": "24"})


def raw_receipt(market, body, *, http_status=200, run_id=RUN_ID, fetched_at=RUN_AT):
    return {
        "run_id": run_id,
        "job": "collect",
        "market": market,
        "route": trends.SC_ROUTE,
        "params_hash": receipt_hash(market),
        "lane": "google_trending",
        "seed_key": None,
        "fetched_at": fetched_at.isoformat(),
        "http_status": http_status,
        "credits_quoted": 5,
        "credits_charged": 5,
        "cache_hit": False,
        "body": json.dumps(body) if body is not None else None,
    }


def ledger_receipt(
    market, *, run_id=RUN_ID, cache_hit=False, calls=1, logged_at=RUN_AT
):
    return {
        "trend_date": RUN_DATE,
        "logged_at": logged_at.isoformat(),
        "run_id": run_id,
        "job": "collect",
        "lane": "google_trending",
        "market": market,
        "route": trends.SC_ROUTE,
        "params_hash": receipt_hash(market),
        "calls": calls,
        "credits_quoted": 0 if cache_hit else 5,
        "credits_charged": 0 if cache_hit else 5,
        "cache_hit": cache_hit,
        "posts_new": 0,
    }


def test_morning_entrypoint_makes_three_bounded_collect_share_calls():
    client = FakeClient([sc_result(), sc_result(), sc_result()])

    batch = collect(client)

    assert len(client.calls) == trends.SC_CALLS_PER_RUN == 3
    assert trends.MAX_SC_CREDITS == 15
    assert [call[0] for call in client.calls] == [trends.SC_ROUTE] * 3
    assert [call[1] for call in client.calls] == [
        {"location": market, "hours": "24"} for market in trends.MARKETS
    ]
    assert [call[2]["market"] for call in client.calls] == list(trends.MARKETS)
    assert all(state.status == "ok" for state in batch.states.values())
    assert [signal.source for signal in batch.signals] == ["google_trending"] * 3
    assert all(signal.refresh_date is None for signal in batch.signals)


def test_official_doc_sample_is_labelled_not_native_and_preserves_source_body():
    client = FakeClient([sc_result(), sc_result(), sc_result()])
    batch = collect(client)

    assert DOC_RESPONSE_PROVENANCE == {
        "source_url": "https://www.socialcrawl.dev/docs/google_trends",
        "retrieved_at_utc": "2026-10-01T14:12:05Z",
        "fixture_kind": "official_publisher_sample_not_native",
    }
    za = next(signal for signal in batch.signals if signal.market == "ZA")
    assert (za.term, za.rank, za.source, za.kind) == (
        "judith und mel",
        1,
        "google_trending",
        "trending",
    )
    assert za.raw_payload == DOC_RESPONSE_NOT_NATIVE["data"]["items"][0]
    assert za.as_consumer_row() == {
        "term": "judith und mel",
        "market": "ZA",
        "source": "google_trending",
        "rank": 1,
        "refreshed_at": "2026-10-01T04:12:00Z",
        "label": "Google search interest",
    }


@pytest.mark.parametrize(
    "rank, expected_status",
    [
        (None, "ok"),
        (0, "unavailable"),
        (-1, "unavailable"),
        (True, "unavailable"),
        ("1", "unavailable"),
    ],
    ids=["nullable", "zero", "negative", "boolean", "string"],
)
def test_publisher_sample_rank_mutation_is_nullable_only_for_null(
    rank, expected_status
):
    response = json.loads(json.dumps(DOC_RESPONSE_NOT_NATIVE))
    response["data"]["items"][0]["rank"] = rank
    client = FakeClient([sc_result(body=response), sc_result(), sc_result()])

    batch = collect(client)

    za = [signal for signal in batch.signals if signal.market == "ZA"]
    assert batch.states[("ZA", "google_trending")].status == expected_status
    if rank is None:
        assert [(signal.term, signal.rank) for signal in za] == [
            ("judith und mel", None)
        ]
    else:
        assert za == []
        assert batch.states[("ZA", "google_trending")].reason == "invalid_response_item"
    assert {signal.market for signal in batch.signals if signal.market != "ZA"} == {
        "NG",
        "KE",
    }


def test_existing_market_receipt_skips_only_that_runs_recorded_request():
    client = FakeClient([sc_result(), sc_result()])

    later = datetime(2026, 10, 1, 6, 12, tzinfo=timezone.utc)
    batch = collect(
        client,
        fetched_at=later,
        raw_receipts=[raw_receipt("ZA", DOC_RESPONSE_NOT_NATIVE)],
        ledger_receipts=[ledger_receipt("ZA")],
    )

    assert len(client.calls) == 2
    assert [call[2]["market"] for call in client.calls] == ["NG", "KE"]
    assert batch.states[("ZA", "google_trending")].status == "ok"
    za = next(signal for signal in batch.signals if signal.market == "ZA")
    assert za.fetched_at == RUN_AT
    assert za.refreshed_at == "2026-10-01T04:12:00Z"
    assert {signal.market for signal in batch.signals} == {"ZA", "NG", "KE"}


def test_same_day_client_cache_ledger_joins_the_saved_raw_response():
    client = FakeClient([sc_result(), sc_result()])

    batch = collect(
        client,
        raw_receipts=[raw_receipt("ZA", DOC_RESPONSE_NOT_NATIVE, run_id="prior-run")],
        ledger_receipts=[ledger_receipt("ZA", cache_hit=True, calls=0)],
    )

    assert [call[2]["market"] for call in client.calls] == ["NG", "KE"]
    assert batch.states[("ZA", "google_trending")].status == "ok"
    assert (
        next(signal for signal in batch.signals if signal.market == "ZA").term
        == "judith und mel"
    )


def test_empty_error_and_bodyless_http200_receipts_do_not_retrigger_markets():
    client = FakeClient()

    batch = collect(
        client,
        raw_receipts=[
            raw_receipt("ZA", None, http_status=404),
            raw_receipt("NG", None, http_status=500),
            raw_receipt("KE", None, http_status=200),
        ],
        ledger_receipts=[ledger_receipt(market) for market in trends.MARKETS],
    )

    assert client.calls == []
    assert batch.states[("ZA", "google_trending")].status == "empty"
    assert batch.states[("NG", "google_trending")].status == "unavailable"
    assert batch.states[("KE", "google_trending")].reason == "invalid_response_shape"
    assert batch.signals == ()


def test_ledger_only_unknown_receipt_skips_retry_and_keeps_outcome_unknown():
    client = FakeClient()

    batch = collect(client, ledger_receipts=[ledger_receipt("ZA")])

    assert [call[2]["market"] for call in client.calls] == ["NG", "KE"]
    assert batch.states[("ZA", "google_trending")].status == "unknown"
    assert (
        batch.states[("ZA", "google_trending")].reason
        == "ledger_receipt_without_raw_response"
    )


def test_prior_collect_run_receipt_suppresses_a_second_same_day_fetch():
    client = FakeClient([sc_result(), sc_result()])

    batch = collect(
        client,
        raw_receipts=[raw_receipt("ZA", DOC_RESPONSE_NOT_NATIVE, run_id="prior-run")],
        ledger_receipts=[ledger_receipt("ZA", run_id="prior-run")],
    )

    assert [call[2]["market"] for call in client.calls] == ["NG", "KE"]
    assert batch.states[("ZA", "google_trending")].status == "ok"


def test_newer_bodyless_attempt_supersedes_an_older_saved_body():
    client = FakeClient([sc_result(), sc_result()])
    later_attempt = datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)

    batch = collect(
        client,
        raw_receipts=[raw_receipt("ZA", DOC_RESPONSE_NOT_NATIVE, run_id="prior-run")],
        ledger_receipts=[
            ledger_receipt("ZA", run_id="prior-run"),
            ledger_receipt("ZA", logged_at=later_attempt),
        ],
    )

    assert [call[2]["market"] for call in client.calls] == ["NG", "KE"]
    assert batch.states[("ZA", "google_trending")].status == "unknown"
    assert not [signal for signal in batch.signals if signal.market == "ZA"]


def test_repeated_same_client_with_no_new_receipts_never_calls_market_twice():
    client = FakeClient([sc_result(), sc_result(), sc_result()])

    first = collect(client)
    calls_after_first = list(client.calls)
    second = collect(client)

    assert len(calls_after_first) == 3
    assert client.calls == calls_after_first
    assert second.signals == first.signals


def test_attempt_is_memoized_before_client_exception():
    client = FakeClient([RuntimeError("transport failure"), sc_result(), sc_result()])

    first = collect(client)
    calls_after_first = list(client.calls)
    second = collect(client)

    assert first.states[("ZA", "google_trending")].status == "unknown"
    assert len(calls_after_first) == 3
    assert client.calls == calls_after_first
    assert second.states[("ZA", "google_trending")].status == "unknown"


def test_market_seed_selection_deduplicates_per_market_and_respects_existing_slots():
    client = FakeClient([sc_result(), sc_result(), sc_result()])
    batch = collect(client)

    selected = trends.select_market_seeds(
        batch,
        existing_terms_by_market={"ZA": ["judith und mel"], "NG": [], "KE": []},
        slots_by_market={"ZA": 1, "NG": 1, "KE": 0},
    )

    assert selected["ZA"] == []
    assert [(seed.market, seed.term, seed.source) for seed in selected["NG"]] == [
        ("NG", "judith und mel", "google_trending")
    ]
    assert selected["KE"] == []
    queue_seed = selected["NG"][0].as_queue_seed()
    expected_hold = quote_for(
        trends.MULTI,
        PRICED[trends.MULTI].method,
        seed_params(
            trends.MULTI,
            "judith und mel",
            "google_trending",
            "NG",
            RUN_DATE,
            RUN_DATE,
        ),
    )
    assert queue_seed == {
        "seed_date": RUN_DATE,
        "market": "NG",
        "item_id": None,
        "query": "judith und mel",
        "kind": "google_trending",
        "lane": "expansion",
        "priority": 0.0,
        "template": trends.MULTI,
        "ttl_days": 1,
        "credits_estimate": float(expected_hold),
        "yield_posts": None,
        "yield_new_creators": None,
    }
    live = seeds.live([queue_seed], RUN_DATE)
    mixed = seeds.mix(
        {"hashtag": [], "sound": []},
        live["NG"],
        {},
        random.Random(0),
        slots14=0,
        slots16=1,
        cost14=1,
        cost16=expected_hold,
        explore_share=0,
    )
    assert len(mixed["16"]) == 1
    assert mixed["16"][0].query == "judith und mel"
    assert mixed["16"][0].kind == "google_trending"
    assert mixed["16"][0].item_id is None
    assert mixed["16"][0].cost == expected_hold


def test_entrypoint_requires_the_morning_collect_client():
    client = FakeClient([sc_result(), sc_result(), sc_result()], share="ask")

    with pytest.raises(ValueError, match="collect share"):
        collect(client)
    mismatched_run = FakeClient(
        [sc_result(), sc_result(), sc_result()], run_id="prior-run"
    )
    with pytest.raises(ValueError, match="client run"):
        collect(mismatched_run)
    with pytest.raises(TypeError, match="raw_receipts.*ledger_receipts"):
        trends.collect_search_signals(
            FakeClient(), run_id=RUN_ID, run_date=RUN_DATE, fetched_at=RUN_AT
        )


def test_nullable_rank_does_not_remove_a_titled_search_signal():
    signal = trends.SearchSignal(
        market="ZA",
        term="judith und mel",
        source="google_trending",
        kind="trending",
        rank=None,
        fetched_at=RUN_AT,
        refreshed_at="2026-10-01T04:12:00Z",
        refresh_date=None,
        raw_payload=DOC_RESPONSE_NOT_NATIVE["data"]["items"][0],
    )

    assert signal.as_table_row()["term"] == "judith und mel"
    assert signal.as_table_row()["rank"] is None


def test_additive_table_contract_keeps_search_signals_out_of_post_identity():
    root = Path(__file__).parents[3]
    sql = (root / "core" / "schema" / "google_search_signals.sql").read_text(
        encoding="utf-8"
    )
    canonical_sql = (root / "core" / "schema" / "core.sql").read_text(encoding="utf-8")

    for field in (
        "market STRING NOT NULL",
        "term STRING NOT NULL",
        "source STRING NOT NULL",
        "kind STRING NOT NULL",
        "fetched_at TIMESTAMP NOT NULL",
        "refreshed_at STRING NOT NULL",
        "rank INT64",
        "refresh_date DATE",
        "raw_payload JSON NOT NULL",
    ):
        assert field in sql
    assert sql.count("CREATE TABLE") == 1
    assert sql.count(";") == 1
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "PARTITION BY DATE(fetched_at)" in sql
    assert "CLUSTER BY market, source" in sql
    assert "google_search_signals" not in canonical_sql
    assert not any(
        token in sql.upper()
        for token in ("DROP", "DELETE", "TRUNCATE", "CREATE OR REPLACE", "EXPIRY")
    )


def test_the_params_each_market_sends_pass_the_real_price_quote():
    from core.collect.socialcrawl_client import PRICED, quote_for

    client = FakeClient([sc_result(), sc_result(), sc_result()])
    collect(client)
    quotes = [quote_for(route, PRICED[route].method, params) for route, params, _ in client.calls]
    assert quotes == [5, 5, 5] and sum(quotes) == trends.MAX_SC_CREDITS


# The free BigQuery Google Trends tables: ZA and NG, newest partition only, search interest only

BQ_RUN_DATE = date(2026, 10, 2)
BQ_RUN_AT = datetime(2026, 10, 2, 0, 5, tzinfo=timezone.utc)
NEWEST = date(2026, 10, 1)


class FakeJob:
    def __init__(self, rows=(), total_bytes_processed=None, total_bytes_billed=None):
        self.rows = list(rows)
        self.total_bytes_processed = total_bytes_processed
        self.total_bytes_billed = total_bytes_billed

    def result(self):
        return self.rows


def bq_row(market, term, rank, *, region="ZA-GT", week=date(2026, 9, 27), refresh=NEWEST, **extra):
    return {"country_code": market, "region_code": region, "term": term, "rank": rank, "week": week,
            "refresh_date": refresh, **extra}


class FakeTrendsBQ:
    """Answers the partition read and the two table reads; every query is recorded with its config."""

    def __init__(self, *, newest=None, rows=None, estimates=None, fail=()):
        self.newest = newest if newest is not None else {
            "international_top_terms": "20261001", "international_top_rising_terms": "20261001"}
        self.rows = rows if rows is not None else {
            "top": [bq_row("ZA", "Springbok rugby", 1), bq_row("NG", "Super Eagles", 2, region="NG-LA")],
            "rising": [bq_row("ZA", "Amapiano awards", 1, percent_gain=350)]}
        self.estimates = estimates or {}
        self.fail = set(fail)
        self.calls = []

    def _kind(self, sql):
        if "INFORMATION_SCHEMA.PARTITIONS" in sql:
            return "meta"
        return "rising" if "international_top_rising_terms" in sql else "top"

    def query(self, sql, job_config=None, location=None):
        kind = self._kind(sql)
        self.calls.append((kind, sql, job_config, location))
        if job_config.dry_run:
            default = 10_485_760 if kind == "meta" else 75_927_600
            return FakeJob(total_bytes_processed=self.estimates.get(kind, default))
        if kind in self.fail:
            raise RuntimeError(f"{kind} read failed")
        if kind == "meta":
            return FakeJob([{"table_name": t, "newest": p} for t, p in self.newest.items()], total_bytes_billed=10)
        return FakeJob(self.rows[kind], total_bytes_billed=76_546_048)


def read_bq(bq):
    return trends.read_bq_signals(bq, run_date=BQ_RUN_DATE, fetched_at=BQ_RUN_AT)


def test_bq_reads_dry_run_first_and_only_the_two_table_reads_carry_the_512_mib_cap():
    bq = FakeTrendsBQ()
    read_bq(bq)
    kinds = [(kind, config.dry_run) for kind, _, config, _ in bq.calls]
    assert kinds == [("meta", True), ("meta", False), ("top", True), ("top", False),
                     ("rising", True), ("rising", False)]
    caps = {kind: config.maximum_bytes_billed for kind, _, config, _ in bq.calls if not config.dry_run}
    assert caps == {"meta": 64 * 1024 * 1024, "top": 536_870_912, "rising": 536_870_912}
    assert trends.BQ_MAX_BYTES == 536_870_912 and trends.BQ_META_MAX_BYTES == 67_108_864
    assert {location for *_, location in bq.calls} == {"US"}


@pytest.mark.parametrize("estimate", [536_870_913, None])
def test_a_table_read_the_dry_run_puts_over_512_mib_or_cannot_size_is_never_run(estimate):
    bq = FakeTrendsBQ(estimates={"top": estimate})
    batch = read_bq(bq)
    assert [config.dry_run for kind, _, config, _ in bq.calls if kind == "top"] == [True]
    assert batch.states[("ZA", "top")].status == "capped"
    assert batch.states[("NG", "top")].status == "capped"
    assert batch.states[("ZA", "rising")].status == "ok"
    assert {s.kind for s in batch.signals} == {"rising"}


def test_a_metadata_read_over_64_mib_is_never_run_and_no_table_is_read():
    bq = FakeTrendsBQ(estimates={"meta": 64 * 1024 * 1024 + 1})
    batch = read_bq(bq)
    assert [(kind, config.dry_run) for kind, _, config, _ in bq.calls] == [("meta", True)]
    assert batch.signals == ()
    assert {state.status for state in batch.states.values()} == {"capped"}


def test_bq_reads_only_the_newest_partition_for_za_and_ng():
    bq = FakeTrendsBQ(newest={"international_top_terms": "20261001",
                              "international_top_rising_terms": "20260930"},
                      rows={"top": [bq_row("ZA", "Springbok rugby", 1)],
                            "rising": [bq_row("ZA", "Amapiano awards", 1, refresh=date(2026, 9, 30))]})
    batch = read_bq(bq)
    sql = {kind: text for kind, text, config, _ in bq.calls if not config.dry_run}
    assert "refresh_date = DATE '2026-10-01'" in sql["top"]
    assert "refresh_date = DATE '2026-09-30'" in sql["rising"]
    for kind in ("top", "rising"):
        assert "'ZA'" in sql[kind] and "'NG'" in sql[kind] and "'KE'" not in sql[kind]
        assert "QUALIFY week = MAX(week) OVER (PARTITION BY country_code)" in sql[kind]
    assert batch.refresh_dates == {"top": date(2026, 10, 1), "rising": date(2026, 9, 30)}
    assert trends.BQ_MARKETS == ("ZA", "NG")


def test_bq_rows_become_search_interest_signals_with_region_and_rank():
    batch = read_bq(FakeTrendsBQ())
    by_term = {s.term: s for s in batch.signals}
    top = by_term["Springbok rugby"]
    assert (top.market, top.source, top.kind, top.rank) == ("ZA", "google_bq", "top", 1)
    assert top.refresh_date == NEWEST and top.refreshed_at == "2026-10-01"
    assert top.raw_payload["region_code"] == "ZA-GT"
    assert top.raw_payload["table"] == "bigquery-public-data.google_trends.international_top_terms"
    rising = by_term["Amapiano awards"]
    assert (rising.kind, rising.raw_payload["percent_gain"]) == ("rising", 350)
    assert by_term["Super Eagles"].raw_payload["region_code"] == "NG-LA"
    assert top.as_consumer_row() == {"term": "Springbok rugby", "market": "ZA", "source": "google_bq",
                                     "rank": 1, "refreshed_at": "2026-10-01",
                                     "label": "Google search interest"}
    assert batch.states[("NG", "rising")].status == "empty"
    assert batch.bytes_billed == 10 + 76_546_048 * 2


def test_bq_load_rows_are_json_ready_and_carry_no_post_identity():
    batch = read_bq(FakeTrendsBQ())
    rows = batch.load_rows()
    json.dumps(rows)
    assert rows[0]["fetched_at"] == "2026-10-02T00:05:00Z"
    assert rows[0]["refresh_date"] == "2026-10-01"
    assert all(set(row) == {"market", "term", "source", "kind", "fetched_at", "refreshed_at", "rank",
                            "refresh_date", "raw_payload"} for row in rows)


@pytest.mark.parametrize("bad", [
    bq_row("KE", "Harambee Stars", 1),
    bq_row("ZA", "Springbok rugby", 1, refresh=date(2026, 9, 30)),
    bq_row("ZA", "  ", 1),
    bq_row("ZA", "Springbok rugby", 0),
    bq_row("ZA", "Springbok rugby", True),
])
def test_a_row_outside_the_read_or_malformed_drops_that_whole_table(bad):
    bq = FakeTrendsBQ(rows={"top": [bq_row("ZA", "Load shedding", 2), bad],
                            "rising": [bq_row("ZA", "Amapiano awards", 1)]})
    batch = read_bq(bq)
    assert {s.kind for s in batch.signals} == {"rising"}
    assert batch.states[("ZA", "top")].status == "unavailable"
    assert batch.states[("NG", "top")].status == "unavailable"


def test_a_failed_table_read_keeps_the_other_table_and_never_raises():
    batch = read_bq(FakeTrendsBQ(fail={"top"}))
    assert batch.states[("ZA", "top")] == trends.SourceState("unknown", "query_failed:RuntimeError")
    assert batch.states[("ZA", "rising")].status == "ok"
    batch = read_bq(FakeTrendsBQ(fail={"meta"}))
    assert batch.signals == ()
    assert {state.reason for state in batch.states.values()} == {"partition_read_failed:RuntimeError"}


def test_a_table_without_a_partition_is_recorded_empty():
    batch = read_bq(FakeTrendsBQ(newest={"international_top_terms": "20261001"}))
    assert batch.states[("ZA", "rising")] == trends.SourceState("empty", "no_partition")
    assert batch.states[("ZA", "top")].status == "ok"


def test_bq_search_interest_never_becomes_a_seed():
    batch = read_bq(FakeTrendsBQ())
    as_trending = trends.SearchSignalBatch("run", BQ_RUN_DATE, batch.signals,
                                           {(m, "google_trending"): trends.SourceState("ok") for m in trends.MARKETS})
    picked = trends.select_market_seeds(as_trending, existing_terms_by_market={},
                                        slots_by_market={m: 5 for m in trends.MARKETS})
    assert picked == {m: [] for m in trends.MARKETS}
