"""Tests for the G1 forecast (core/brief/whatif_g1.py) on fake BigQuery rows: no cloud, no model."""

import json
from datetime import date

import pytest

from core.brief import gatectx, whatif_g1
from core.brief.market_scope import QUERIES as SCOPE_QUERIES

D = date(2026, 10, 3)
PASSING, GLOBAL, BARE = "a" * 64, "b" * 64, "c" * 64
G1_REASON = "Data issue: 1 of the last 3 market-days invalid on the main platform"


def _post(pid, handle, platform="tiktok"):
    return {"id": pid, "platform": platform, "handle": handle, "market": "ZA", "source_market": "ZA", "flags": [],
            "text": "caption", "quote_text": "caption"}


def _held(item_id, title, **over):
    item = {"item_id": item_id, "title": title, "rule": "G1", "reason": "data_issue", "reason_text": G1_REASON,
            "evidence": [_post(f"{title}-{i}", f"creator{i}") for i in range(3)],
            "numbers": [{"value": 9, "unit": "creators in 3 days", "query_id": "q_creators3", "run_id": "r"}]}
    item.update(over)
    return item


def _state(item_id, label):
    return {"item_id": item_id, "market": "ZA", "metric_date": D, "run_id": "detect-20261003", "state": "rising",
            "kind": "hashtag", "main_series_id": f"{item_id[:4]}|ZA|board_youtube", "sponsored_share": 0.0,
            "authenticity": None, "untested": True, "map_kind": "hashtag", "map_status": "active", "label": label,
            "canonical_key": label.lower().replace(" ", "")}


PAYLOAD = {"cards": [], "more": [], "banners": [], "held_back": {"count": 4, "items": [
    _held(PASSING, "Amapiano Friday"),
    _held(GLOBAL, "Global Song"),
    # No item_state row is read for it and its stored record carries no evidence.
    {"item_id": BARE, "title": "Bare Item", "rule": "G1", "reason": "data_issue", "reason_text": G1_REASON},
    {"item_id": "d" * 64, "title": "Paid Thing", "rule": "G5", "reason": "paid_led", "reason_text": "Paid-led"},
]}}
SCOPE = {PASSING: (9, 10), GLOBAL: (2, 10), BARE: (8, 10)}


class Job:
    def __init__(self, rows, statement_type="SELECT", total=1024):
        self.rows, self.statement_type, self.total_bytes_processed = rows, statement_type, total

    def result(self):
        return self.rows


class FakeBigQuery:
    """Answers each query the forecast runs from fixed rows, and records every job config it was given."""

    def __init__(self, total=1024, statement_type="SELECT"):
        self.total, self.statement_type, self.calls = total, statement_type, []

    def query(self, sql, job_config=None):
        params = {p.name: p.value for p in job_config.query_parameters}
        self.calls.append((sql, job_config))
        if job_config.dry_run:
            return Job([], self.statement_type, self.total)
        return Job(self.rows(sql, params))

    def rows(self, sql, params):
        item = params.get("item_id")
        if "v_briefs_current" in sql:
            return [{"market": "ZA", "run_id": "r1", "status": "data_issue", "payload": json.dumps(PAYLOAD)},
                    {"market": "NG", "run_id": "r1", "status": "published",
                     "payload": json.dumps({"held_back": {"count": 0, "items": []}})}]
        # Before the suppression list: the market scope query names v_suppressed_creators too.
        if "-- name: market_scope" in sql:
            market_posts, total = SCOPE[item]
            return [{"total_posts7": total, "market_posts7": market_posts, "market_news_posts7": 0}]
        if "v_suppressed_creators" in sql:
            return []
        if "v_item_state_current s" in sql:
            wanted = set(params["item_ids"].split(","))
            return [r for r in (_state(PASSING, "Amapiano Friday"), _state(GLOBAL, "Global Song"))
                    if r["item_id"] in wanted]
        if "-- name: series_platform" in sql:
            return [{"platform": "youtube"}]
        if "-- name: sightings" in sql:
            return [{"platform": "youtube", "lane_class": "unbiased_rank", "n": 4}]
        if "-- name: board" in sql:
            return [{"n": 2}]
        if "-- name: health" in sql:
            return [{"day": D, "ok": False}]
        if "-- name: names" in sql:
            return []
        if any(f"-- name: {n}" in sql for n in ("post_lanes", "post_tags", "post_authors")):
            return []
        raise AssertionError(f"unexpected query: {sql[:80]}")


def _forecast(fake=None):
    client = whatif_g1.CappedClient(fake or FakeBigQuery())
    return whatif_g1.forecast(client, D, campaign_hashtags=[], political_terms={"ZA": [], "NG": [], "KE": []})


def test_item_that_passes_the_data_gates():
    _, outcomes = _forecast()
    passing = next(o for o in outcomes if o.item_id == PASSING)
    assert (passing.verdict, passing.platform) == ("pass", "youtube")
    assert passing.line() == ("ZA | Amapiano Friday | youtube | passes data gates; still needs confirm, critic and "
                              "support model checks")


def test_item_that_g6_would_hold_as_global():
    _, outcomes = _forecast()
    held = next(o for o in outcomes if o.item_id == GLOBAL)
    assert (held.verdict, held.gate) == ("held", "G6")
    assert held.line() == ("ZA | Global Song | youtube | held by G6: Global: 2 of 10 card source posts in the last 7 "
                           "days were located in this market or came from its feeds")


def test_item_with_missing_fields_is_unknown_at_the_first_gate_that_needs_them():
    _, outcomes = _forecast()
    bare = next(o for o in outcomes if o.item_id == BARE)
    assert (bare.verdict, bare.gate) == ("unknown", "G5")
    assert bare.line() == ("ZA | Bare Item | youtube | unknown at G5: item_state row (sponsored_share, state, "
                           "map_status, authenticity, canonical_key, hashtags); stored evidence")


def test_only_g1_items_are_read_and_the_counts_close_per_market():
    markets, outcomes = _forecast()
    assert [o.item_id for o in outcomes] == [PASSING, GLOBAL, BARE]
    assert whatif_g1.summary(markets, outcomes) == [
        "ZA: 3 held at G1; 1 pass data gates, 0 to Moments, 1 held (G6 1), 1 unknown",
        "NG: 0 held at G1; 0 pass data gates, 0 to Moments, 0 held, 0 unknown",
        "all: 3 held at G1; 1 pass data gates, 0 to Moments, 1 held (G6 1), 1 unknown"]


def test_g1_is_cleared_on_a_copy_and_the_stored_g1_still_holds_without_it():
    """The health rows still say invalid, so the gate holds at G1 on the real context."""
    fake = FakeBigQuery()
    client = whatif_g1.CappedClient(fake)
    row = {**_state(PASSING, "Amapiano Friday"), "market_scope": "market", "market_posts7": 9, "total_posts7": 10,
           "market_share7": 0.9}
    ctx = gatectx.build_ctx(client, row, D, "ZA", PAYLOAD["held_back"]["items"][0]["evidence"], campaign_hashtags=[],
                            political_terms=[])
    assert ctx["valid_days"][0] is False
    outcome = whatif_g1.judge(client, D, "ZA", PAYLOAD["held_back"]["items"][0], _state(PASSING, "Amapiano Friday"),
                              (set(), set(), set()), campaign_hashtags=[], political_terms=[])
    assert outcome.verdict == "pass"


def test_every_query_runs_dry_first_then_capped_and_once():
    fake = FakeBigQuery()
    _forecast(fake)
    sqls = [sql for sql, _ in fake.calls]
    for i in range(0, len(fake.calls), 2):
        dry, real = fake.calls[i][1], fake.calls[i + 1][1]
        assert dry.dry_run is True and not real.dry_run
        assert real.maximum_bytes_billed == whatif_g1.MAX_BYTES
        assert fake.calls[i][0] == fake.calls[i + 1][0]
    keys = [(sql, tuple(sorted((p.name, str(p.value)) for p in cfg.query_parameters)))
            for sql, cfg in fake.calls[1::2]]
    assert len(keys) == len(set(keys))
    assert all(whatif_g1.read_only(sql) for sql in sqls)


def test_a_query_over_the_cap_is_refused_before_it_runs(capsys):
    fake = FakeBigQuery(total=whatif_g1.MAX_BYTES + 1)
    assert whatif_g1.main(["whatif_g1", D.isoformat()], client=fake) == 2
    assert all(cfg.dry_run for _, cfg in fake.calls)
    assert "refused: query would read" in capsys.readouterr().err


def test_a_dry_run_that_is_not_a_select_is_refused():
    fake = FakeBigQuery(statement_type="INSERT")
    with pytest.raises(whatif_g1.Refused):
        _forecast(fake)
    assert all(cfg.dry_run for _, cfg in fake.calls)


def test_write_statements_are_refused_before_any_call():
    fake = FakeBigQuery()
    client = whatif_g1.CappedClient(fake)
    for sql in ("INSERT INTO t VALUES (1)", "DELETE FROM t WHERE TRUE", "SELECT 1; DROP TABLE t",
                "CREATE OR REPLACE VIEW v AS SELECT 1"):
        with pytest.raises(whatif_g1.Refused):
            client.query(sql)
    assert fake.calls == []


def test_the_queries_it_runs_are_read_only():
    for sql in [whatif_g1.ROWS, whatif_g1.BRIEFS, SCOPE_QUERIES["market_scope"], *gatectx.QUERIES.values()]:
        assert whatif_g1.read_only(sql), sql[:60]


def test_main_prints_one_line_per_item_then_the_counts(capsys):
    fake = FakeBigQuery()
    assert whatif_g1.main(["whatif_g1", D.isoformat()], client=fake) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("brief 2026-10-03: items held at G1")
    assert out[1].startswith("ZA | Amapiano Friday | youtube | passes data gates")
    assert out[2].startswith("ZA | Global Song | youtube | held by G6: Global")
    assert out[3].startswith("ZA | Bare Item | youtube | unknown at G5")
    assert any(line.startswith("ZA: 3 held at G1") for line in out)
    assert out[-1].startswith("bytes read: ")


def test_a_title_naming_a_person_suppressed_after_the_brief_is_masked():
    """The stored title is masked with today's list, so a later suppression is never printed."""
    client = whatif_g1.CappedClient(FakeBigQuery())
    item = {**PAYLOAD["held_back"]["items"][0], "title": "@kay_dance challenge"}
    outcome = whatif_g1.judge(client, D, "ZA", item, _state(PASSING, "Amapiano Friday"),
                              ({"tiktok:kay_dance"}, set(), set()), campaign_hashtags=[], political_terms=[])
    assert "kay_dance" not in (outcome.title or "")
    assert "kay_dance" not in outcome.line()


def test_a_title_a_cp1252_console_cannot_show_does_not_stop_the_report(monkeypatch):
    """Albert's PC, piped: stdout is cp1252, and a held title such as 昕昕 raised UnicodeEncodeError."""
    import io
    import sys

    outcome = whatif_g1.Outcome("KE", "i1" * 6, "昕昕", "tiktok", "pass", None, "passes data gates")
    monkeypatch.setattr(whatif_g1, "forecast", lambda client, d: (["KE"], [outcome]))
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    assert whatif_g1.main(["whatif_g1", D.isoformat()], client=FakeBigQuery()) == 0
    sys.stdout.flush()
    text = raw.getvalue().decode("cp1252")
    assert "KE | ?? | tiktok | passes data gates" in text and text.rstrip().splitlines()[-1].startswith("bytes read")
