"""The selection diagnostic on the DuckDB harness and on fake warehouse rows, without creating a cloud client."""
import json

import pytest

from core.brief import job
from core.brief.tests.test_candidates_pattern import D, con  # noqa: F401  (con is the shared fixture)
from core.brief.tests.test_candidates_locality import stubbed
from core.detect.tests import duck
from ops.runners import diagnose_selection as diag


def stub(sql):
    return stubbed(sql, ordered=sql[sql.index("ordered AS ("):])


def test_the_old_order_is_the_statement_without_the_pattern_bucket_and_orders_by_worth_alone(con):  # noqa: F811
    old = diag.old_order(job.QUERIES["candidates"])
    assert old.count("WHEN s.kind = 'creator'") == 1          # the selected column stays, the ordering key goes
    rows = duck.query(con, stub(old), {"d": D, "market": "NG"})
    worth = [r["worth_raw"] for r in rows]
    assert worth == sorted(worth, reverse=True)
    assert [r["item_id"] for r in rows][:3] == ["person", "solo", "promo"]


def test_the_whole_market_statement_is_the_pool_statement_without_its_limit_in_the_same_order(con):  # noqa: F811
    sql = job.QUERIES["candidates"]
    whole = diag.whole_market(sql)
    assert sql.endswith(f"LIMIT {job.POOL}") and "LIMIT" not in whole[whole.index("ordered AS ("):]
    assert whole == sql[:sql.rindex("\nLIMIT")]
    pool, every = (duck.query(con, stub(s), {"d": D, "market": "NG"}) for s in (sql, whole))
    assert [r["item_id"] for r in every] == [r["item_id"] for r in pool]
    with pytest.raises(SystemExit):
        diag.whole_market(whole)


def test_the_report_names_each_judged_item_with_its_hold_and_both_ranks(con, monkeypatch, capsys, tmp_path):  # noqa: F811
    new_rows, old_rows, all_rows = (
        duck.query(con, stub(sql), {"d": D, "market": "NG"})
        for sql in (job.QUERIES["candidates"], diag.old_order(job.QUERIES["candidates"]),
                    diag.whole_market(job.QUERIES["candidates"])))
    payload = {"held_back": {"items": [
        {"item_id": "person", "reason_text": "The explanation did not pass our checks", "failed_reason": "Critic"},
        {"item_id": "fixture", "reason_text": "The explanation did not pass our checks", "failed_reason": "Critic"}]}}
    answers = iter([[{"market": "NG", "p": json.dumps(payload)}],
                    [], [], [], new_rows, old_rows, all_rows, [], [], []])  # ZA, NG and KE: new, old, whole market
    monkeypatch.setattr(diag, "query", lambda client, sql, params: next(answers))
    monkeypatch.setattr(diag, "make_client", lambda: None)
    assert diag.main([D.isoformat(), "--out", str(tmp_path)]) == 0
    report = json.loads((tmp_path / f"selection-{D.isoformat()}.json").read_text())
    ng = report["markets"]["NG"]
    assert ng["summary"]["judged"] == 2 and ng["summary"]["judged_by_bucket"] == {"0": 0, "1": 0, "2": 2}
    assert ng["summary"]["pool_by_bucket"] == {"0": 2, "1": 4, "2": 6}
    assert ng["summary"]["market_rows"] == 12 and ng["summary"]["market_by_bucket"] == {"0": 2, "1": 4, "2": 6}
    by = {e["item_id"]: e for e in ng["items"]}
    assert (by["sound"]["new_rank"], by["sound"]["old_rank"], by["sound"]["outcome"]) == (1, 11, None)
    assert by["person"]["outcome"] == "The explanation did not pass our checks: Critic"
    out = capsys.readouterr().out
    assert "handle" not in out
    assert ("== NG 2026-10-07: pool 12, pool by bucket {0: 2, 1: 4, 2: 6}, market 12, market by bucket "
            "{0: 2, 1: 4, 2: 6}, judged 2 (by bucket {0: 0, 1: 0, 2: 2}, published 0), new ten never judged 8") in out


def test_the_market_counts_cover_rows_past_the_pool(con, monkeypatch, capsys):  # noqa: F811
    # A pool cut at 3 rows, as the top 90 cuts a large market: the whole-market counts still see every row.
    pool_sql = job.QUERIES["candidates"].replace(f"LIMIT {job.POOL}", "LIMIT 3")
    new_rows, old_rows, all_rows = (
        duck.query(con, stub(sql), {"d": D, "market": "NG"})
        for sql in (pool_sql, diag.old_order(pool_sql), diag.whole_market(job.QUERIES["candidates"])))
    answers = iter([[], [], [], [], new_rows, old_rows, all_rows, [], [], []])
    monkeypatch.setattr(diag, "query", lambda client, sql, params: next(answers))
    monkeypatch.setattr(diag, "make_client", lambda: None)
    assert diag.main([D.isoformat()]) == 0
    out = capsys.readouterr().out
    assert ("pool 3, pool by bucket {0: 2, 1: 1, 2: 0}, market 12, market by bucket {0: 2, 1: 4, 2: 6}, judged 0"
            in out)


def test_the_runner_reads_as_the_impersonated_builder_not_the_callers_own_login(monkeypatch):
    import google.auth
    from google.auth import impersonated_credentials

    seen = {}
    monkeypatch.setattr(google.auth, "default", lambda scopes: ("adc", "p"))
    monkeypatch.setattr(impersonated_credentials, "Credentials",
                        lambda **kw: seen.setdefault("creds", kw) and "builder")
    monkeypatch.setattr(diag.bigquery, "Client", lambda **kw: seen.setdefault("client", kw))
    diag.make_client()
    assert seen["creds"]["source_credentials"] == "adc" and seen["creds"]["target_principal"] == diag.BUILDER
    assert seen["client"]["credentials"] == "builder" and seen["client"]["project"] == diag.P
