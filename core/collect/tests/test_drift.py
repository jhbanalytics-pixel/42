"""Weekly drift report (task 2.4). No network: BigQuery is faked."""
import logging
import re
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from core.collect import drift, job

END = date(2026, 9, 28)


def row(side, dimension, market, item, weight):
    return {"side": side, "dimension": dimension, "market": market, "item": item, "weight": weight}


DATA = (
    [row("expansion", "topic", "ZA", "a", 70.0)] + [row("expansion", "topic", "ZA", k, 10.0) for k in "bcd"]
    + [row("feed", "topic", "ZA", f"f{i}", 3) for i in range(10)]
    + [row("expansion", "platform", "ZA", "tiktok", 15.0)]
    + [row("expansion", "platform", "ZA", p, 70 / 6) for p in job.MULTI_PLATFORMS.split(",")]
    + [row("feed", "platform", "ZA", "tiktok", 400)]
    + [row("expansion", "topic", "NG", "x", 12.0)]
    + [row("expansion", "language", "KE", "sw", 5), row("expansion", "language", "KE", "en", 5),
       row("feed", "language", "KE", "sw", 9), row("feed", "language", "KE", "en", 1)]
)


class FakeBQ:
    def __init__(self, rows=DATA):
        self.rows = list(rows)
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, {p.name: p for p in job_config.query_parameters}))
        return SimpleNamespace(result=lambda: self.rows)


def test_herfindahl_index_is_the_sum_of_squared_shares():
    assert drift.hhi({"a": 1, "b": 1}) == 0.5
    assert drift.hhi({"a": 3, "b": 1}) == pytest.approx(0.625)
    assert drift.hhi({"a": 5}) == 1.0
    assert drift.hhi({k: 2 for k in "abcd"}) == pytest.approx(0.25)
    assert drift.hhi({k: 1 for k in "abcdefghij"}) == pytest.approx(0.10)
    assert drift.hhi({}) is None and drift.hhi({"a": 0}) is None
    assert drift.hhi({"a": 2, "b": None, "c": 2}) == 0.5


def test_limit_is_the_diversity_cap_or_the_feeds_plus_a_margin():
    assert drift.FLOOR == 0.25 and drift.MARGIN == 0.10
    assert drift.limit(None) == 0.25
    assert drift.limit(0.05) == 0.25
    assert drift.limit(0.30) == pytest.approx(0.40)


def test_report_compares_expansion_with_the_unseeded_feeds():
    found = {(r["market"], r["dimension"]): r for r in drift.report(DATA)}
    assert len(found) == 9
    za = found[("ZA", "topic")]
    assert za["expansion"] == pytest.approx(0.52) and za["feed"] == pytest.approx(0.10)
    assert (za["limit"], za["top"], za["drifted"]) == (0.25, "a", True) and za["top_share"] == pytest.approx(0.7)
    platform = found[("ZA", "platform")]
    assert platform["expansion"] < 0.25 and platform["feed"] == 1.0 and not platform["drifted"]
    ng = found[("NG", "topic")]
    assert ng["expansion"] == 1.0 and ng["feed"] is None and ng["drifted"]
    ke = found[("KE", "language")]
    assert ke["expansion"] == 0.5 and ke["feed"] == pytest.approx(0.82) and not ke["drifted"]
    assert found[("KE", "topic")]["expansion"] is None and not found[("KE", "topic")]["drifted"]


def test_alert_line_carries_every_breach_in_the_shared_format():
    line = drift.alert_line(drift.report(DATA), END)
    assert line == ("42 ALERT drift: 2026-09-22 to 2026-09-28 ZA topic HHI 0.52 against feeds 0.10 (limit 0.25), "
                    "top a 70%; NG topic HHI 1.00 against feeds none (limit 0.25), top x 100%")
    calm = [r for r in DATA if r["market"] == "KE"]
    assert drift.alert_line(drift.report(calm), END) is None


def test_read_is_one_parameterised_select_over_the_week():
    bq = FakeBQ()
    assert drift.read(bq, END) == DATA
    [(sql, params)] = bq.queries
    for name in ("raw_responses", "item_counter_daily", "credit_ledger", "post_observations", "post_enrichment"):
        assert f"`ogilvy-trends-v2.intelligence_42_core.{name}`" in sql
    assert params["start"].value == date(2026, 9, 22) and params["end"].value == END
    assert params["markets"].values == ["ZA", "NG", "KE"]
    assert params["lanes"].values == ["expansion", "exploration", "anchor"]
    assert params["multi"].values == job.MULTI_PLATFORMS.split(",")
    assert "SUM(l.credits_charged) AS weight" in sql and "seed_queue" not in sql
    assert "c.unit = 'appearances'" in sql and "l.job = 'collect'" in sql
    assert sql.lstrip().upper().startswith("SELECT")
    assert not re.search(r"\b(MERGE|INSERT|UPDATE|DELETE|DROP|TRUNCATE|CREATE|REPLACE)\b", sql, re.I)


def test_the_weekly_job_logs_one_alert_line(caplog):
    caplog.set_level(logging.INFO)
    assert drift.main([], env={"RUN_DATE": "2026-09-28"}, bq=FakeBQ()) == 0
    alerts = [r for r in caplog.records if r.getMessage().startswith("42 ALERT drift: ")]
    assert len(alerts) == 1 and alerts[0].levelno == logging.ERROR
    assert alerts[0].getMessage().startswith("42 ALERT drift: 2026-09-22 to 2026-09-28 ZA topic ")
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 9


def test_no_alert_line_without_drift(caplog):
    assert drift.main([], env={"RUN_DATE": "2026-09-28"}, bq=FakeBQ([r for r in DATA if r["market"] == "KE"])) == 0
    assert not [r for r in caplog.records if "42 ALERT" in r.getMessage()]


def test_the_report_flag_prints_the_week_without_an_alert_line(capsys, caplog):
    assert drift.main(["--report", "--run-date", "2026-09-28"], env={}, bq=FakeBQ()) == 0
    out = capsys.readouterr().out
    assert "drift report 2026-09-22 to 2026-09-28" in out
    assert re.search(r"^ZA\s+topic\s+0\.52\s+0\.10\s+0\.25\s+a 70%\s+drifted$", out, re.M)
    assert "would alert: ZA topic HHI 0.52" in out
    assert "42 ALERT" not in out and not [r for r in caplog.records if "42 ALERT" in r.getMessage()]


def test_the_week_ends_yesterday_in_sast_by_default():
    bq = FakeBQ([])
    assert drift.main([], env={}, bq=bq, clock=lambda: datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)) == 0
    assert bq.queries[0][1]["end"].value == date(2026, 9, 27)


DUCK_TABLES = {
    "seed_queue": "seed_date DATE, market VARCHAR, item_id VARCHAR, query VARCHAR, kind VARCHAR, lane VARCHAR, "
                  "priority DOUBLE, template VARCHAR, ttl_days BIGINT, credits_estimate DOUBLE, "
                  "yield_posts BIGINT, yield_new_creators BIGINT",
    "item_counter_daily": "obs_date DATE, market VARCHAR, item_id VARCHAR, unit VARCHAR, lane_class VARCHAR, "
                          "value DOUBLE",
    "credit_ledger": "trend_date DATE, run_id VARCHAR, job VARCHAR, lane VARCHAR, market VARCHAR, "
                     "platform VARCHAR, route VARCHAR, params_hash VARCHAR, item_id VARCHAR, calls BIGINT, "
                     "credits_charged DOUBLE",
    "raw_responses": "run_id VARCHAR, job VARCHAR, market VARCHAR, route VARCHAR, params_hash VARCHAR, "
                     "lane VARCHAR, seed_key VARCHAR, fetched_at TIMESTAMPTZ, credits_charged DOUBLE",
    "post_observations": "post_id VARCHAR, observed_date DATE, market VARCHAR, platform VARCHAR, lane VARCHAR, "
                         "lane_class VARCHAR",
    "post_enrichment": "post_id VARCHAR, langs VARCHAR[]",
}


def duck_read(tables):
    import duckdb
    import sqlglot

    con = duckdb.connect()
    for name, columns in DUCK_TABLES.items():
        con.execute(f"CREATE TABLE {name} ({columns})")
        for record in tables.get(name, []):
            con.execute(f"INSERT INTO {name} ({', '.join(record)}) VALUES ({', '.join('?' for _ in record)})",
                        list(record.values()))

    def listed(values):
        return "[" + ", ".join(f"'{value}'" for value in values) + "]"

    sql = drift.READ_SQL.format(**{name: name for name in DUCK_TABLES})
    values = (("@start", f"DATE '{drift.start_of(END).isoformat()}'"), ("@end", f"DATE '{END.isoformat()}'"),
              ("@markets", listed(drift.MARKETS)), ("@lanes", listed(drift.LANES)),
              ("@multi", listed(job.MULTI_PLATFORMS.split(","))))
    for name, value in values:
        sql = sql.replace(name, value)
    cursor = con.execute(sqlglot.transpile(sql, read="bigquery", write="duckdb")[0])
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, values)) for values in cursor.fetchall()]


def yield_row(item_id, query, lane):
    return {"seed_date": date(2026, 9, 26), "market": "ZA", "item_id": item_id, "query": query, "kind": "topic",
            "lane": lane, "priority": None, "template": "search/multi", "ttl_days": None,
            "credits_estimate": None, "yield_posts": 3, "yield_new_creators": 1}


def ledger_row(run_id, params_hash, credits, item_id, *, job_name="collect", lane="expansion",
               route="search/multi"):
    return {"trend_date": date(2026, 9, 26), "run_id": run_id, "job": job_name, "lane": lane, "market": "ZA",
            "platform": "instagram", "route": route, "params_hash": params_hash, "item_id": item_id,
            "calls": 1, "credits_charged": credits}


def raw_row(run_id, params_hash, seed_key, *, job_name="collect", lane="expansion", route="search/multi",
            fetched_at=datetime(2026, 9, 26, 0, 30, tzinfo=timezone.utc)):
    return {"run_id": run_id, "job": job_name, "market": "ZA", "route": route, "params_hash": params_hash,
            "lane": lane, "seed_key": seed_key, "fetched_at": fetched_at, "credits_charged": 10.0}


def topic_report(ledger, raw, seed_rows=()):
    tables = {"seed_queue": list(seed_rows), "credit_ledger": list(ledger), "raw_responses": list(raw)}
    return next(r for r in drift.report(duck_read(tables)) if r["market"] == "ZA" and r["dimension"] == "topic")


def test_topic_drift_uses_actual_credits_and_prefers_ledger_item_id():
    ledger = [ledger_row("run-a", "hash-a", 10, "topic|authority"),
              ledger_row("run-b", "hash-b", 1, None, route="tiktok/search/top")]
    raw = [raw_row("run-a", "hash-a", "wrong query"),
           raw_row("run-b", "hash-b", "Harvest Term", route="tiktok/search/top")]
    report = topic_report(ledger, raw, [yield_row("topic|authority", "Authority", "expansion")])
    assert report["top"] == "topic|authority"
    assert report["top_share"] == pytest.approx(10 / 11)
    assert report["expansion"] == pytest.approx((10 / 11) ** 2 + (1 / 11) ** 2)


def test_topic_drift_attribution_matches_job_and_lane():
    ledger = [ledger_row("run-a", "hash-a", 10, "topic|authority"),
              ledger_row("run-b", "hash-b", 1, "topic|other", route="tiktok/search/top")]
    raw = [raw_row("run-a", "hash-a", "authority", job_name="collect", lane="expansion"),
           raw_row("run-a", "hash-a", "wrong job", job_name="probe", lane="expansion"),
           raw_row("run-a", "hash-a", "wrong lane", job_name="collect", lane="anchor"),
           raw_row("run-b", "hash-b", "other", route="tiktok/search/top")]
    report = topic_report(ledger, raw)
    assert report["top_share"] == pytest.approx(10 / 11)
    assert report["expansion"] == pytest.approx((10 / 11) ** 2 + (1 / 11) ** 2)


def test_topic_drift_deduplicates_repeated_raw_responses():
    ledger = [ledger_row("run-a", "hash-a", 10, "topic|authority"),
              ledger_row("run-b", "hash-b", 1, "topic|other", route="tiktok/search/top")]
    raw = [raw_row("run-a", "hash-a", "authority"),
           raw_row("run-a", "hash-a", "authority", fetched_at=datetime(2026, 9, 26, 1, 30,
                                                                         tzinfo=timezone.utc)),
           raw_row("run-b", "hash-b", "other", route="tiktok/search/top")]
    report = topic_report(ledger, raw)
    assert report["top_share"] == pytest.approx(10 / 11)
    assert report["expansion"] == pytest.approx((10 / 11) ** 2 + (1 / 11) ** 2)


# The weekly calendar analogues step (task 3.6), carried by this job -----------------------


def test_the_weekly_job_runs_the_calendar_analogues_after_the_drift_lines(caplog):
    caplog.set_level(logging.INFO)
    bq, seen = FakeBQ(), []

    def step(client):
        seen.append((client, [r.getMessage() for r in caplog.records]))
        return 0

    assert drift.main([], env={"RUN_DATE": "2026-09-28"}, bq=bq, analogues=step) == 0
    [(client, before)] = seen
    assert client is bq
    assert any(m.startswith("42 ALERT drift: ") for m in before)
    [done] = [r for r in caplog.records if r.getMessage().startswith("calendar analogues")]
    assert done.levelno == logging.INFO and done.getMessage() == "calendar analogues: appended"


def test_a_failed_analogues_step_is_logged_and_never_fails_the_drift_job(caplog):
    caplog.set_level(logging.INFO)

    def step(client):
        raise RuntimeError("Access Denied: calendar_analogues")

    assert drift.main([], env={"RUN_DATE": "2026-09-28"}, bq=FakeBQ(), analogues=step) == 0
    assert len([r for r in caplog.records if r.getMessage().startswith("42 ALERT drift: ")]) == 1
    [failed] = [r for r in caplog.records if r.getMessage().startswith("calendar analogues")]
    assert failed.levelno == logging.WARNING
    assert failed.getMessage() == "calendar analogues: failed, drift report unaffected (RuntimeError: Access Denied: " \
                                  "calendar_analogues)"
    assert "42 ALERT" not in failed.getMessage()


def test_an_analogues_read_refused_over_the_byte_cap_is_logged_and_never_fails_the_drift_job(caplog):
    caplog.set_level(logging.INFO)
    assert drift.main([], env={"RUN_DATE": "2026-09-28"}, bq=FakeBQ(), analogues=lambda client: 1) == 0
    [refused] = [r for r in caplog.records if r.getMessage().startswith("calendar analogues")]
    assert refused.levelno == logging.WARNING
    assert refused.getMessage() == "calendar analogues: refused (exit 1), drift report unaffected"


def test_the_report_flag_never_runs_the_analogues_step():
    def step(client):
        raise AssertionError("--report is read-only")
    assert drift.main(["--report", "--run-date", "2026-09-28"], env={}, bq=FakeBQ(), analogues=step) == 0


def test_the_deployed_job_runs_the_calendar_analogues_with_its_own_client(monkeypatch, caplog):
    from google.cloud import bigquery

    from core.collect import calendar

    caplog.set_level(logging.INFO)
    bq, seen = FakeBQ(), []
    monkeypatch.setattr(bigquery, "Client", lambda project: bq)
    monkeypatch.setattr(calendar, "weekly_analogues", lambda client: seen.append(client) or 0)
    assert drift.main([], env={"RUN_DATE": "2026-09-28"}) == 0
    assert seen == [bq]
    assert "calendar analogues: appended" in [r.getMessage() for r in caplog.records]
