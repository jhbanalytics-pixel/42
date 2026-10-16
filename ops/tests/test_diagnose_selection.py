"""The selection diagnostic on the DuckDB harness and on fake warehouse rows, without creating a cloud client."""
import json

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


def test_the_report_names_each_judged_item_with_its_hold_and_both_ranks(con, monkeypatch, capsys, tmp_path):  # noqa: F811
    new_rows, old_rows = (duck.query(con, stub(sql), {"d": D, "market": "NG"})
                          for sql in (job.QUERIES["candidates"], diag.old_order(job.QUERIES["candidates"])))
    payload = {"held_back": {"items": [
        {"item_id": "person", "reason_text": "The explanation did not pass our checks", "failed_reason": "Critic"},
        {"item_id": "fixture", "reason_text": "The explanation did not pass our checks", "failed_reason": "Critic"}]}}
    answers = iter([[{"market": "NG", "p": json.dumps(payload)}],
                    [], [], new_rows, old_rows, [], []])            # ZA, NG and KE, each new then old
    monkeypatch.setattr(diag, "query", lambda client, sql, params: next(answers))
    monkeypatch.setattr(diag.bigquery, "Client", lambda project: None)
    assert diag.main([D.isoformat(), "--out", str(tmp_path)]) == 0
    report = json.loads((tmp_path / f"selection-{D.isoformat()}.json").read_text())
    ng = report["markets"]["NG"]
    assert ng["summary"]["judged"] == 2 and ng["summary"]["judged_by_bucket"] == {"0": 0, "1": 0, "2": 2}
    assert ng["summary"]["pool_by_bucket"] == {"0": 2, "1": 4, "2": 6}
    by = {e["item_id"]: e for e in ng["items"]}
    assert (by["sound"]["new_rank"], by["sound"]["old_rank"], by["sound"]["outcome"]) == (1, 11, None)
    assert by["person"]["outcome"] == "The explanation did not pass our checks: Critic"
    assert "handle" not in capsys.readouterr().out
