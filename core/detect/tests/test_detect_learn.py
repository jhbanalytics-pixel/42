"""The weekly learn job (BUILD.md 2.7): the scorecard appended to engine_scorecard, one runs row per run.

run_scorecard runs for real on DuckDB tables through LearnClient, which adds insert_rows_json for the runs row
and a get_table that looks the table up in DuckDB's catalog and raises NotFound as BigQuery does. The fixture
week is Monday 28 September to Sunday 4 October 2026, across a month end.
"""

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from .. import learn, runs, scorecard, sqlrun
from . import duck
from .test_detect_scorecard import EXTRA_TABLES, FIGURES, ledger

WEEK = date(2026, 9, 28)
WEEK_END = date(2026, 10, 4)
FIGURE_KEYS = {"value", "unit", "query_id", "run_id", "result_hash", "n", "reason", "regime"}   # scorecard-2


class LearnClient(duck.Client):
    def __init__(self, con, fail_on=None):
        super().__init__(con)
        self.fail_on = fail_on

    def query(self, sql, job_config=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("refused")
        return super().query(sql, job_config)

    def insert_rows_json(self, table, rows):
        duck.load(self.con, table, rows)
        return []

    def get_table(self, ref):
        schema, table = ref.split(".")[-2:]
        found = self.con.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
                                 [schema, table]).fetchall()
        if not found:
            raise NotFound(f"Not found: Table {ref}")
        return bigquery.Table("p." + ref, schema=[])


@pytest.fixture
def con():
    c = duck.connect()
    c.execute(EXTRA_TABLES)
    duck.load(c, "core.credit_ledger", [ledger(date(2026, 9, 30), 6.0, item="i1"),
                                        ledger(date(2026, 10, 2), 2.0, item="i2")])
    yield c
    c.close()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.delenv("FORCE_RERUN", raising=False)
    monkeypatch.delenv("RUN_DATE", raising=False)


def scorecard_rows(con):
    return duck.query(con, "SELECT * FROM {agent}.engine_scorecard s ORDER BY s.run_id, s.market")


def learn_runs(con):
    return duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'learn' ORDER BY r.finished_at")


AFTER = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)          # the fixture week has ended by then


def main(con, argv=("--week", WEEK.isoformat()), **kw):
    return learn.main(list(argv), client=LearnClient(con, **kw), now=AFTER, core="core", agent="agent")


# Week arithmetic


@pytest.mark.parametrize("now, week_start", [
    (datetime(2026, 10, 4, 22, 30, tzinfo=timezone.utc), date(2026, 9, 28)),   # Monday 5 October 00:30 SAST
    (datetime(2026, 10, 4, 21, 30, tzinfo=timezone.utc), date(2026, 9, 21)),   # Sunday 4 October 23:30 SAST
    (datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc), date(2026, 9, 21)),    # a Thursday
    (datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc), date(2026, 10, 5)),    # the Monday after
    (datetime(2027, 1, 4, 5, 0, tzinfo=timezone.utc), date(2026, 12, 28)),     # across a year end
])
def test_the_default_week_is_the_last_complete_monday_to_sunday_week_in_sast(now, week_start):
    assert learn.week_for(now) == week_start


def test_the_default_week_follows_run_date_when_set(monkeypatch):
    monkeypatch.setenv("RUN_DATE", "2026-10-05")
    assert learn.week_for(datetime(2026, 12, 1, tzinfo=timezone.utc)) == WEEK


def test_a_week_that_is_not_a_monday_is_refused(con):
    with pytest.raises(SystemExit) as e:
        main(con, ("--week", "2026-09-29"))
    assert e.value.code == 2
    assert learn_runs(con) == [] and scorecard_rows(con) == []


def test_an_unfinished_week_is_refused_and_nothing_is_written(con, monkeypatch):
    # Reviewer's case: written on Wednesday as ok, the Monday run would then skip the week for good.
    monkeypatch.setenv("RUN_DATE", "2026-09-30")
    assert main(con) == 2
    monkeypatch.setenv("RUN_DATE", "2026-10-04")                 # Sunday: the week has not ended either
    assert main(con) == 2
    assert learn_runs(con) == [] and scorecard_rows(con) == []
    monkeypatch.setenv("RUN_DATE", "2026-10-05")
    assert main(con) == 0 and len(scorecard_rows(con)) == 3


def test_without_week_the_job_scores_the_last_complete_week(con, monkeypatch):
    monkeypatch.setenv("RUN_DATE", "2026-10-05")
    assert main(con, ()) == 0
    rows = scorecard_rows(con)
    assert {(r["week_start"], r["week_end"]) for r in rows} == {(WEEK, WEEK_END)}


# The append


def test_the_week_is_appended_one_row_per_market_with_a_json_figure_per_metric(con, capsys):
    assert main(con) == 0
    rows = scorecard_rows(con)
    [run] = learn_runs(con)
    assert [r["market"] for r in rows] == ["KE", "NG", "ZA"]
    for r in rows:
        assert (r["week_start"], r["week_end"]) == (WEEK, WEEK_END)
        assert r["run_id"] == run["run_id"] and r["rule_version"] == scorecard.RULE_VERSION
        for f in FIGURES:
            figure = json.loads(r[f])
            assert set(figure) == FIGURE_KEYS and figure["run_id"] == run["run_id"]
    za = next(r for r in rows if r["market"] == "ZA")
    assert json.loads(za["expansion_cluster_share"])["value"] == 0.75           # 6 of 8 credits on one item
    assert json.loads(za["lead_time"])["value"] is None and json.loads(za["lead_time"])["reason"]
    assert run["status"] == "ok" and run["run_date"] == WEEK and run["error"] is None
    assert run["run_id"].startswith("learn-20260928-")
    counts = json.loads(run["counts"])
    assert counts["model_usd"] == 0.0 and counts["written"] == ["ZA", "NG", "KE"]
    assert counts["week_start"] == "2026-09-28" and counts["week_end"] == "2026-10-04"
    assert '"ZA"' in capsys.readouterr().out


def test_the_figures_written_are_the_figures_run_scorecard_returns(con):
    assert main(con) == 0
    [run] = learn_runs(con)
    expected = scorecard.run_scorecard(duck.Client(con), WEEK, run_id=run["run_id"], core="core", agent="agent")
    written = {r["market"]: r for r in scorecard_rows(con)}
    for row in expected:
        for f in FIGURES:
            assert json.loads(written[row["market"]][f]) == row[f]


def test_only_one_insert_and_selects_reach_bigquery(con):
    client = LearnClient(con)
    assert learn.main(["--week", WEEK.isoformat()], client=client, now=AFTER, core="core", agent="agent") == 0
    heads = [sql.lstrip().split(None, 1)[0].upper() for sql in client.sql]
    assert heads.count("INSERT") == 1 and set(heads) <= {"SELECT", "WITH", "INSERT"}
    for word in ("DELETE", "MERGE", "UPDATE ", "TRUNCATE", "DROP", "REPLACE"):
        assert word not in learn.APPEND_SQL.upper() and word not in learn.DONE_SQL.upper()


# Rerun safety


def test_a_rerun_after_an_ok_run_writes_nothing_and_is_recorded_as_skipped(con):
    assert main(con) == 0
    client = LearnClient(con)
    assert learn.main(["--week", WEEK.isoformat()], client=client, now=AFTER, core="core", agent="agent") == 0
    assert len(scorecard_rows(con)) == 3
    first, second = learn_runs(con)
    assert first["status"] == "ok" and second["status"] == "skipped"
    assert "already written" in second["error"]
    assert json.loads(second["counts"])["already_written"] == ["ZA", "NG", "KE"]
    assert not any(q in sql for sql in client.sql for q in scorecard.queries(scorecard.load_reference()).values())


def test_only_the_markets_an_ok_learn_run_wrote_are_skipped(con):
    # ZA was written by an ok learn run; NG and KE only by a run whose runs row reads failed.
    duck.load(con, "agent.runs", [
        {"run_id": "learn-20260928-ok", "stage": "learn", "run_date": WEEK, "status": "ok"},
        {"run_id": "learn-20260928-failed", "stage": "learn", "run_date": WEEK, "status": "failed"}])
    duck.load(con, "agent.engine_scorecard", [
        {"week_start": WEEK, "week_end": WEEK_END, "market": m, "run_id": r}
        for m, r in (("ZA", "learn-20260928-ok"), ("NG", "learn-20260928-failed"), ("KE", "learn-20260928-failed"))])
    assert main(con) == 0
    last = learn_runs(con)[-1]
    assert last["status"] == "ok"
    counts = json.loads(last["counts"])
    assert counts["written"] == ["NG", "KE"] and counts["already_written"] == ["ZA"]
    assert sorted(r["market"] for r in scorecard_rows(con) if r["run_id"] == last["run_id"]) == ["KE", "NG"]


def test_rows_from_another_stage_or_week_do_not_count_as_written(con):
    duck.load(con, "agent.runs", [{"run_id": "detect-x", "stage": "detect", "run_date": WEEK, "status": "ok"},
                                  {"run_id": "learn-y", "stage": "learn", "run_date": WEEK, "status": "ok"}])
    duck.load(con, "agent.engine_scorecard", [
        {"week_start": WEEK, "week_end": WEEK_END, "market": "ZA", "run_id": "detect-x"},
        {"week_start": date(2026, 9, 21), "week_end": date(2026, 9, 27), "market": "NG", "run_id": "learn-y"}])
    assert main(con) == 0
    assert json.loads(learn_runs(con)[-1]["counts"])["written"] == ["ZA", "NG", "KE"]


def test_force_rerun_writes_the_week_again(con, monkeypatch):
    assert main(con) == 0
    monkeypatch.setenv("FORCE_RERUN", "1")
    assert main(con) == 0
    first, second = learn_runs(con)
    assert second["status"] == "ok" and second["run_id"] != first["run_id"]
    rows = scorecard_rows(con)
    assert len(rows) == 6
    assert sorted(r["market"] for r in rows if r["run_id"] == second["run_id"]) == ["KE", "NG", "ZA"]


# The table L1 has not created yet


def hide_table(con):
    con.execute("ALTER TABLE agent.engine_scorecard RENAME TO engine_scorecard_hidden")


def test_a_missing_table_prints_the_rows_records_skipped_and_exits_0(con, capsys):
    hide_table(con)
    client = LearnClient(con)
    assert learn.main(["--week", WEEK.isoformat()], client=client, now=AFTER, core="core", agent="agent") == 0
    [run] = learn_runs(con)
    assert run["status"] == "skipped"
    assert "agent.engine_scorecard does not exist" in run["error"]
    assert json.loads(run["counts"])["written"] == []
    assert not any(sql.lstrip().upper().startswith("INSERT") for sql in client.sql)
    assert duck.query(con, "SELECT * FROM {agent}.engine_scorecard_hidden") == []
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "skipped"
    assert [r["market"] for r in printed["rows"]] == ["ZA", "NG", "KE"]
    assert all(set(r[f]) == FIGURE_KEYS for r in printed["rows"] for f in FIGURES)


def test_table_exists_reads_not_found_as_missing(con):
    assert learn.table_exists(LearnClient(con), "agent") is True
    hide_table(con)
    assert learn.table_exists(LearnClient(con), "agent") is False


# Failure


def test_a_failed_append_records_a_failed_runs_row_and_exits_1(con):
    assert main(con, fail_on="INSERT INTO agent.engine_scorecard") == 1
    [run] = learn_runs(con)
    assert run["status"] == "failed" and "refused" in run["error"]
    assert scorecard_rows(con) == []
    assert main(con) == 0                   # the failed run wrote nothing, so the next run writes the week
    assert len(scorecard_rows(con)) == 3


# Forecast scoring and the weekly quality score (FEATURES.md 26 and 7; ENGINE.md section 3)


SCORE_TABLES = """
CREATE TABLE agent.forecast_score (
  week_start DATE NOT NULL, week_end DATE NOT NULL, run_id VARCHAR NOT NULL, scored_at TIMESTAMPTZ,
  rule VARCHAR, target VARCHAR, horizon BIGINT, n BIGINT, unresolved BIGINT, no_baseline BIGINT, no_prob BIGINT,
  minimum BIGINT, brier DOUBLE, persistence_brier DOUBLE, skill DOUBLE, promotion_eligible BOOLEAN,
  query_id VARCHAR, result_hash VARCHAR, row_count BIGINT, reason VARCHAR);
CREATE TABLE agent.weekly_quality (
  week_start DATE NOT NULL, week_end DATE NOT NULL, market VARCHAR NOT NULL, run_id VARCHAR NOT NULL,
  scored_at TIMESTAMPTZ, score DOUBLE, counted VARCHAR, change DOUBLE, previous_run_id VARCHAR, questions BIGINT,
  question_set_hash VARCHAR, parts JSON, context JSON, notes VARCHAR);
"""


def forecast(i, *, prob, observed, target="reach_rising", horizon=7, rule="logistic_v1", market="ZA",
             issue=date(2026, 9, 10)):
    return {"forecast_id": f"{rule}-{target}-{horizon}-{market}-{i}", "item_id": f"item{i}", "market": market,
            "target": target, "issue_date": issue, "horizon": horizon, "rule": rule, "prob": prob,
            "predicted_arrival": prob >= 0.5, "persistence_arrival": True,
            "resolve_date": issue + timedelta(days=horizon), "observed_arrival": observed}


def beats_persistence(n_each=100):
    """200 resolved: half arrive at prob 0.8, half do not at prob 0.3, and persistence says every one arrives.
    Brier 0.065 against persistence's 0.5, so skill 0.87 and fewer binary errors: promotion eligible."""
    return ([forecast(i, prob=0.8, observed=True) for i in range(n_each)]
            + [forecast(n_each + i, prob=0.3, observed=False) for i in range(n_each)])


def score_rows(con, table):
    return duck.query(con, "SELECT * FROM {agent}." + table + " t ORDER BY t.run_id")


def test_learn_scores_the_closed_forecasts_into_forecast_score_with_promotion_eligible(con):
    con.execute(SCORE_TABLES)
    rows = beats_persistence()
    duck.load(con, "agent.forecasts", [{**r, "observed_arrival": None} for r in rows] + rows)
    open_row = forecast(999, prob=0.9, observed=None, issue=date(2026, 10, 1))      # window closes after Sunday
    duck.load(con, "agent.forecasts", [open_row])
    assert main(con) == 0
    [run] = learn_runs(con)
    [stored] = score_rows(con, "forecast_score")
    assert (stored["week_start"], stored["week_end"]) == (WEEK, WEEK_END)
    assert (stored["rule"], stored["target"], stored["horizon"], stored["n"]) == ("logistic_v1", "reach_rising", 7, 200)
    assert stored["skill"] == pytest.approx(0.87) and stored["promotion_eligible"] is True
    assert stored["query_id"] == "core/eval/sql/forecast_rows.sql?d=2026-10-05"
    counts = json.loads(run["counts"])
    assert run["status"] == "ok"
    assert counts["forecast_score"]["written"] == 1 and counts["forecast_score"]["run_id"] == stored["run_id"]
    assert counts["forecast_score"]["promotion_eligible"] == ["logistic_v1/reach_rising/7"]


def test_learn_writes_the_weekly_quality_score_with_the_forecast_skill_part(con):
    con.execute(SCORE_TABLES)
    duck.load(con, "agent.forecasts", beats_persistence())
    assert main(con) == 0
    [run] = learn_runs(con)
    quality = score_rows(con, "weekly_quality")
    assert sorted(r["market"] for r in quality) == ["ALL", "KE", "NG", "ZA"]
    allm = next(r for r in quality if r["market"] == "ALL")
    assert (allm["week_start"], allm["week_end"]) == (WEEK, WEEK_END)
    assert json.loads(allm["parts"])["forecast_skill"]["value"] == pytest.approx(0.87)
    assert "forecast_skill" in allm["counted"].split(",")
    assert json.loads(run["counts"])["quality"] == {"written": 4, "run_id": allm["run_id"], "notes": []}



def test_the_quality_precision_reads_the_scorecard_this_learn_run_wrote_not_an_earlier_attempt(con):
    # An earlier attempt wrote the week and then failed. Its run_id sorts after any new one, as a random hex
    # suffix can, and its precision would dominate the pooled figure.
    con.execute(SCORE_TABLES)
    stale = "learn-20260928-ffffffffffff"
    duck.load(con, "agent.runs", [{"run_id": stale, "stage": "learn", "run_date": WEEK, "status": "failed"}])
    duck.load(con, "agent.engine_scorecard", [
        {"week_start": WEEK, "week_end": WEEK_END, "market": m, "run_id": stale,
         "precision": json.dumps({"value": 0.99, "unit": "share", "n": 500})} for m in ("ZA", "NG", "KE")])
    assert main(con) == 0
    [run] = [r for r in learn_runs(con) if r["status"] == "ok"]
    assert run["run_id"] < stale
    written = [r for r in scorecard_rows(con) if r["run_id"] == run["run_id"]]
    assert sorted(r["market"] for r in written) == ["KE", "NG", "ZA"]
    figures = [json.loads(r["precision"]) for r in written]
    expected_n = sum(f.get("n") or 0 for f in figures if f.get("value") is not None)
    allm = next(r for r in score_rows(con, "weekly_quality") if r["market"] == "ALL")
    assert json.loads(allm["parts"])["precision"]["n"] == expected_n


def test_with_no_resolved_forecasts_scoring_writes_no_cohort_and_learn_is_ok(con):
    con.execute(SCORE_TABLES)
    duck.load(con, "agent.forecasts", [forecast(1, prob=0.7, observed=None)])        # closed, not yet resolved
    assert main(con) == 0
    [run] = learn_runs(con)
    assert run["status"] == "ok" and len(scorecard_rows(con)) == 3
    [cohort] = score_rows(con, "forecast_score")                    # counted as unresolved, never scored
    assert cohort["n"] == 0 and cohort["unresolved"] == 1 and cohort["skill"] is None
    assert cohort["promotion_eligible"] is False and cohort["reason"]
    counts = json.loads(run["counts"])
    assert counts["forecast_score"]["promotion_eligible"] == []
    assert next(r for r in score_rows(con, "weekly_quality") if r["market"] == "ALL")["score"] is None


def test_with_no_forecasts_at_all_forecast_score_gets_no_row_and_learn_is_ok(con):
    con.execute(SCORE_TABLES)
    assert main(con) == 0
    [run] = learn_runs(con)
    assert run["status"] == "ok" and score_rows(con, "forecast_score") == []
    assert json.loads(run["counts"])["forecast_score"]["written"] == 0


def test_missing_score_tables_are_scored_and_printed_but_never_created(con, capsys):
    duck.load(con, "agent.forecasts", beats_persistence())
    client = LearnClient(con)
    assert learn.main(["--week", WEEK.isoformat()], client=client, now=AFTER, core="core", agent="agent") == 0
    [run] = learn_runs(con)
    assert run["status"] == "ok" and len(scorecard_rows(con)) == 3
    assert not any(("INSERT INTO" in sql.upper() or "CREATE TABLE" in sql.upper())
                   and ("forecast_score" in sql or "weekly_quality" in sql) for sql in client.sql)
    for table in ("forecast_score", "weekly_quality"):
        assert not learn.table_exists(client, "agent", table)
    counts = json.loads(run["counts"])
    assert counts["forecast_score"]["written"] == 0 and "does not exist" in counts["forecast_score"]["note"]
    assert counts["forecast_score"]["promotion_eligible"] == ["logistic_v1/reach_rising/7"]
    assert counts["quality"]["written"] == 0 and "does not exist" in counts["quality"]["note"]
    printed = json.loads(capsys.readouterr().out)
    assert printed["scores"]["forecast_score"]["cohorts"][0]["skill"] == pytest.approx(0.87)


def test_a_failed_score_write_records_a_failed_runs_row_and_exits_1(con):
    con.execute(SCORE_TABLES)
    duck.load(con, "agent.forecasts", beats_persistence())
    assert main(con, fail_on="INSERT INTO `agent.forecast_score`") == 1
    [run] = learn_runs(con)
    assert run["status"] == "failed" and "refused" in run["error"]
    assert main(con) == 0                   # the failed run's scorecard rows do not count, so the retry writes
    assert learn_runs(con)[-1]["status"] == "ok" and len(score_rows(con, "forecast_score")) == 1


def test_a_rerun_after_an_ok_run_scores_nothing_again(con):
    con.execute(SCORE_TABLES)
    duck.load(con, "agent.forecasts", beats_persistence())
    assert main(con) == 0 and main(con) == 0
    assert len(score_rows(con, "forecast_score")) == 1 and len(score_rows(con, "weekly_quality")) == 4
    assert "forecast_score" not in json.loads(learn_runs(con)[-1]["counts"])


def test_learn_scores_through_the_packaged_eval_modules_not_ops():
    text = Path(learn.__file__).read_text(encoding="utf-8")
    assert "from core.eval import forecast_score, quality_score" in text and "ops." not in text


# Dry run of the append on staging, skipped until L1 creates the table


def dry_run_append(client, agent=sqlrun.AGENT):
    """Dry-run DONE_SQL and APPEND_SQL, or skip the test while agent.engine_scorecard does not exist."""
    if not learn.table_exists(client, agent):
        pytest.skip(f"{agent}.engine_scorecard does not exist on staging yet")
    row = {"week_start": WEEK, "week_end": WEEK_END, "market": "ZA", "run_id": runs.new_run_id("learn", WEEK),
           "rule_version": scorecard.RULE_VERSION, **{f: json.dumps({"value": None}) for f in FIGURES}}
    for sql, params in ((learn.DONE_SQL, [sqlrun._param("week_start", WEEK)]),
                        (learn.APPEND_SQL, [learn.rows_param([row])])):
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=params)
        assert client.query(sqlrun.render(sql, agent=agent), job_config=config).dry_run


def test_the_staging_dry_run_skips_while_the_table_is_missing(con):
    hide_table(con)
    with pytest.raises(pytest.skip.Exception, match="does not exist on staging yet"):
        dry_run_append(LearnClient(con), "agent")


@pytest.mark.skipif(os.environ.get("F42_BQ") != "1", reason="set F42_BQ=1 to dry-run on BigQuery staging")
def test_the_append_dry_runs_on_staging_once_the_table_exists():
    dry_run_append(bigquery.Client(project=learn.PROJECT))
