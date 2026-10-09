"""The weekly learn job (BUILD.md 2.7): the scorecard appended to engine_scorecard, one runs row per run.

run_scorecard runs for real on DuckDB tables through LearnClient, which adds insert_rows_json for the runs row
and a get_table that looks the table up in DuckDB's catalog and raises NotFound as BigQuery does. The fixture
week is Monday 28 September to Sunday 4 October 2026, across a month end.
"""

import json
import os
import re
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


# The weekly card and hold outcome step: information only


def outcome_inputs(items=("item-secret-1", "item-secret-2")):
    """What read_inputs returns for one published ZA card, one held item and a good detect run on every day."""
    d = date(2026, 9, 20)
    brief = {"brief_date": d, "market": "ZA", "run_id": "b1", "status": "published",
             "published_at": datetime(2026, 9, 20, 5, tzinfo=timezone.utc),
             "cards_json": json.dumps([{"item_id": items[0], "rank": 1, "state": "rising"}]), "more_json": "[]",
             "held_json": json.dumps([{"item_id": items[1], "reason": "not_confirmed", "rule": "G3"}])}
    states = [{"metric_date": date(2026, 9, 27), "market": "ZA", "item_id": items[0], "state": "peaking",
               "base_state": None, "untested": False, "main_lane_class": "panel", "signal_lanes": ["panel"]},
              {"metric_date": date(2026, 9, 27), "market": "ZA", "item_id": items[1], "state": "rising",
               "base_state": None, "untested": False, "main_lane_class": "panel",
               "signal_lanes": ["panel", "search_presence"]}]
    days = [{"run_date": date(2026, 9, 10) + timedelta(days=i)} for i in range(40)]
    return {"briefs": [brief], "states": states, "detect_days": days}


META = [{"name": n, "job_id": f"job-{n}", "estimated_bytes": 1000, "bytes_processed": 900, "bytes_billed": 1048576,
         "maximum_bytes_billed": 5 * 1024 ** 3, "cache_hit": False, "row_count": 1}
        for n in ("briefs", "states", "detect_days")]


@pytest.fixture(autouse=True)
def outcome_reads(monkeypatch):
    """The outcome step reads BigQuery SQL that the DuckDB harness cannot run; every test gets a fake read and
    records how it was called."""
    calls = []

    def read(client, queries, start, end, **kw):
        calls.append({"queries": queries, "start": start, "end": end, "kw": kw})
        return outcome_inputs(), META

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", read)
    return calls


def outcome_counts(con):
    return json.loads(learn_runs(con)[-1]["counts"])["card_outcome"]


def test_learn_reads_the_outcome_window_ending_a_week_before_the_weeks_end_through_the_runner_queries(con, outcome_reads):
    assert main(con) == 0
    [call] = outcome_reads
    assert call["end"] == date(2026, 9, 27) and call["start"] == date(2026, 8, 31)   # 4 weeks, ending t + 7 inside the week
    assert call["queries"] is learn.card_outcome_read.QUERIES and set(call["queries"]) == {"briefs", "states", "detect_days"}
    assert set(call["kw"]) == {"core", "agent", "deadline"}      # no byte cap of its own: the runner's caps stand


def test_the_weekly_outcome_step_records_the_definition_window_bytes_and_group_counts_in_the_runs_row(con):
    assert main(con) == 0
    out = outcome_counts(con)
    assert out["definition"] == "active28_by_base_v3" and out["horizon"] == 7
    assert out["window"] == ["2026-08-31", "2026-09-27"] and out["error"] is None
    assert out["bytes_billed"] == 3 * 1048576 and [q["name"] for q in out["queries"]] == ["briefs", "states", "detect_days"]
    assert {(g["market"], g["hold_reason"]) for g in out["groups"]} == {("ALL", None)}      # no per-market or per-reason row
    assert {g["stratum"] for g in out["groups"]} == {"all", "trend"}
    published = next(g for g in out["groups"] if g["kind"] == "published" and g["market"] == "ALL" and g["stratum"] == "all")
    held = next(g for g in out["groups"] if g["kind"] == "held" and g["market"] == "ALL" and g["hold_reason"] is None
                and g["stratum"] == "all")
    # the card is Peaking on a panel lane at t + 7: held. The held item is Rising but a search lane also signalled: unmeasured.
    assert (published["n"], published["held"], published["unmeasured"], published["text"]) == (1, 1, 0, "not enough data")
    assert (held["n"], held["held"], held["unmeasured"]) == (0, 0, 1)


def test_the_outcome_counts_carry_no_item_id_title_or_row(con):
    assert main(con) == 0
    blob = learn_runs(con)[-1]["counts"]
    assert "item-secret" not in blob and "item_id" not in blob and "title" not in blob


def test_a_failing_outcome_read_is_recorded_and_the_learn_run_is_still_ok(con, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("warehouse said no")

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", boom)
    assert main(con) == 0
    [run] = learn_runs(con)
    assert run["status"] == "ok" and len(scorecard_rows(con)) == 3
    assert outcome_counts(con)["error"] == "RuntimeError: warehouse said no"


def test_a_read_over_the_byte_cap_is_recorded_and_the_learn_run_is_still_ok(con, monkeypatch):
    def refuse(*a, **k):
        raise learn.card_outcome_read.Refused("states: estimate 6442450944 is over the 5368709120 byte cap")

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", refuse)
    assert main(con) == 0
    assert learn_runs(con)[-1]["status"] == "ok" and "byte cap" in outcome_counts(con)["error"]


def test_the_scorecard_and_scores_are_identical_whether_the_outcome_step_succeeds_or_fails(con, monkeypatch):
    def written(c):
        """The three tables with every run id masked, and every result_hash too, since a hash digests rows that carry
        the run id; the values the hashes stand for are compared in full."""
        drop = ("scored_at", "finished_at")
        tables = [scorecard_rows(c), score_rows(c, "forecast_score"), score_rows(c, "weekly_quality")]
        text = json.dumps([[{k: v for k, v in r.items() if k not in drop} for r in t] for t in tables], default=str,
                          sort_keys=True)
        text = re.sub(r"(learn|forecast|quality)[-_a-z]*-\d{8}-[0-9a-f]+|[a-z_]+-\d{8}-[0-9a-f]{8,}", "RUN", text)
        return re.sub(r"sha256:[0-9a-f]{64}", "HASH", text)

    con.execute(SCORE_TABLES)
    duck.load(con, "agent.forecasts", beats_persistence())
    assert main(con) == 0
    with_step = written(con)
    other = duck.connect()
    other.execute(EXTRA_TABLES)
    duck.load(other, "core.credit_ledger", [ledger(date(2026, 9, 30), 6.0, item="i1"), ledger(date(2026, 10, 2), 2.0, item="i2")])
    other.execute(SCORE_TABLES)
    duck.load(other, "agent.forecasts", beats_persistence())

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", boom)
    assert main(other) == 0
    without = written(other)
    other.close()
    assert with_step == without and "promotion_eligible" in with_step


def test_a_rerun_after_an_ok_run_does_not_read_again_and_force_rerun_does(con, monkeypatch, outcome_reads):
    assert main(con) == 0 and len(outcome_reads) == 1
    assert main(con) == 0 and len(outcome_reads) == 1
    assert "card_outcome" not in json.loads(learn_runs(con)[-1]["counts"])
    monkeypatch.setenv("FORCE_RERUN", "1")
    assert main(con) == 0 and len(outcome_reads) == 2


def test_the_outcome_step_writes_nothing_and_creates_nothing(con):
    client = LearnClient(con)
    assert learn.main(["--week", WEEK.isoformat()], client=client, now=AFTER, core="core", agent="agent") == 0
    assert not any("card_outcome" in sql for sql in client.sql)
    assert not learn.table_exists(client, "agent", "card_outcome")


ROOT = Path(learn.__file__).parents[2]
READERS_OF_THE_MEASURE = {"core/eval/card_outcome.py", "core/eval/card_outcome_read.py", "core/detect/learn.py",
                          "ops/card_outcome_report.py"}
SOURCE = (".py", ".sql", ".mjs", ".js", ".jsx", ".yaml", ".yml", ".sh", ".json")


def test_no_gate_threshold_card_or_hold_reads_the_outcome_measure():
    found = set()
    for folder in ("core", "ops", "app/src", "app/scripts"):
        for path in (ROOT / folder).rglob("*"):
            if (not path.is_file() or path.suffix not in SOURCE or "tests" in path.parts or "node_modules" in path.parts
                    or path.name.startswith("test_") or "__pycache__" in path.parts):
                continue
            if "card_outcome" in path.read_text(encoding="utf-8", errors="ignore"):
                found.add(path.relative_to(ROOT).as_posix())
    assert found == READERS_OF_THE_MEASURE | {"core/eval/sql/card_outcome.sql", "core/schema/card_outcome.sql"}


def test_learn_uses_the_outcome_measure_only_inside_its_step_and_stores_the_result_under_one_counts_key():
    import ast

    tree = ast.parse(Path(learn.__file__).read_text(encoding="utf-8"))
    step = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "score_outcomes")
    inside = {id(n) for n in ast.walk(step)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ("card_outcome_read", "card_outcome"):
            assert id(node) in inside, f"line {node.lineno} uses the outcome measure outside score_outcomes"
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "score_outcomes"]
    assert len(calls) == 1
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    [assign] = [n for n in ast.walk(run) if isinstance(n, ast.Assign) and any(c is calls[0] for c in ast.walk(n))]
    assert ast.unparse(assign.targets[0]) == "counts['card_outcome']"
    later = [n for n in ast.walk(run) if isinstance(n, ast.Name) and n.id in ("status", "error", "rows")
             and isinstance(n.ctx, ast.Store) and n.lineno > assign.lineno]
    assert later == []              # the run's status, error and rows are all settled before the step


# O3 and O4. The outcome step is bounded by a deadline and reads the datasets learn was run with.


@pytest.fixture
def release():
    """Lets a deliberately slow read finish once the test is over, so no thread outlives it."""
    import threading

    event = threading.Event()
    yield event
    event.set()


def test_the_default_deadline_of_the_outcome_step_is_eight_minutes():
    assert learn.OUTCOME_DEADLINE_SECONDS == 480


def test_a_slow_outcome_read_times_out_and_the_runs_row_is_still_written(con, monkeypatch, release):
    import time

    monkeypatch.setattr(learn, "OUTCOME_DEADLINE_SECONDS", 0.3)

    def slow(client, queries, start, end, **kw):
        release.wait(20)
        return outcome_inputs(), META

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", slow)
    began = time.monotonic()
    assert main(con) == 0
    assert time.monotonic() - began < 10          # not 20: the run did not wait for the read
    [run] = learn_runs(con)
    assert run["status"] == "ok" and run["error"] is None and len(scorecard_rows(con)) == 3
    assert json.loads(run["counts"])["card_outcome"]["status"] == "timeout"
    assert "groups" not in json.loads(run["counts"])["card_outcome"]


def test_a_read_that_finishes_inside_the_deadline_records_ok(con):
    assert main(con) == 0
    out = outcome_counts(con)
    assert out["status"] == "ok" and out["error"] is None and out["groups"]


def test_a_failed_read_records_status_error(con, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("warehouse said no")

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", boom)
    assert main(con) == 0
    assert outcome_counts(con)["status"] == "error"


def test_a_read_that_runs_out_its_own_deadline_is_a_timeout_too(con, monkeypatch):
    def late(*a, **k):
        raise learn.card_outcome_read.DeadlineExceeded("states: the deadline passed")

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", late)
    assert main(con) == 0
    assert outcome_counts(con)["status"] == "timeout"


def test_the_deadline_is_configurable_from_the_environment(con, monkeypatch, outcome_reads):
    import time

    monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", "90")
    before = time.monotonic()
    assert main(con) == 0
    [call] = outcome_reads
    assert before + 85 < call["kw"]["deadline"] <= time.monotonic() + 90


@pytest.mark.parametrize("value", ["481", "3600", "1e9"])
def test_a_deadline_in_the_environment_never_exceeds_the_default(monkeypatch, value):
    monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", value)
    assert learn.outcome_deadline() == learn.OUTCOME_DEADLINE_SECONDS == 480


@pytest.mark.parametrize("value, expected", [("480", 480.0), ("479.5", 479.5), ("90", 90.0), ("0.5", 0.5)])
def test_a_deadline_at_or_under_the_default_is_used_as_set(monkeypatch, value, expected):
    monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", value)
    assert learn.outcome_deadline() == expected


def test_a_deadline_over_the_default_reaches_the_reads_clamped_and_the_runs_row_says_so(con, monkeypatch, outcome_reads):
    import time

    monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", "3600")
    before = time.monotonic()
    assert main(con) == 0
    [call] = outcome_reads
    assert before + 470 < call["kw"]["deadline"] <= time.monotonic() + 480
    assert outcome_counts(con)["deadline_clamped"] == {"requested": 3600.0, "used": 480.0}


def test_the_clamp_follows_the_default_in_force_and_is_recorded_with_it(con, monkeypatch, outcome_reads):
    monkeypatch.setattr(learn, "OUTCOME_DEADLINE_SECONDS", 60)
    monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", "90")
    assert main(con) == 0
    assert outcome_counts(con)["deadline_clamped"] == {"requested": 90.0, "used": 60.0}


@pytest.mark.parametrize("value", [None, "", "abc", "0", "-5", "nan", "inf", "90", "480"])
def test_nothing_is_recorded_as_clamped_when_the_deadline_was_not_over_the_default(con, monkeypatch, outcome_reads, value):
    if value is not None:
        monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", value)
    assert main(con) == 0
    assert "deadline_clamped" not in outcome_counts(con)


def test_the_deadline_the_reads_get_is_the_one_for_the_whole_step(con, outcome_reads):
    import time

    before = time.monotonic()
    assert main(con) == 0
    [call] = outcome_reads
    assert before + 470 < call["kw"]["deadline"] <= time.monotonic() + 480


def test_a_late_read_cannot_change_what_was_recorded(con, monkeypatch, release):
    monkeypatch.setattr(learn, "OUTCOME_DEADLINE_SECONDS", 0.2)
    seen = []

    def slow(client, queries, start, end, **kw):
        release.wait(20)
        seen.append("finished")
        return outcome_inputs(), META

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", slow)
    assert main(con) == 0
    release.set()
    import time

    time.sleep(0.3)
    assert json.loads(learn_runs(con)[-1]["counts"])["card_outcome"] == {
        "definition": "active28_by_base_v3", "horizon": 7, "window": ["2026-08-31", "2026-09-27"],
        "status": "timeout", "error": "the outcome step passed its 0.2 second deadline"}


def test_learn_passes_its_own_dataset_names_to_the_outcome_reads(con, outcome_reads):
    assert main(con) == 0
    [call] = outcome_reads
    assert call["kw"]["core"] == "core" and call["kw"]["agent"] == "agent"


@pytest.mark.parametrize("value", ["", "abc", "0", "-5", "nan", "inf"])
def test_a_bad_deadline_in_the_environment_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv("LEARN_OUTCOME_DEADLINE_SECONDS", value)
    assert learn.outcome_deadline() == learn.OUTCOME_DEADLINE_SECONDS


def test_a_read_left_behind_by_a_timeout_cannot_hold_the_process_open(con, monkeypatch, release):
    import threading

    monkeypatch.setattr(learn, "OUTCOME_DEADLINE_SECONDS", 0.2)

    def slow(client, queries, start, end, **kw):
        release.wait(20)
        return outcome_inputs(), META

    monkeypatch.setattr(learn.card_outcome_read, "read_inputs", slow)
    assert main(con) == 0
    left = [t for t in threading.enumerate() if t.name == "learn-card-outcome"]
    assert left and all(t.daemon for t in left)
