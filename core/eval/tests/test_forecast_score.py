"""Forecast scoring (FEATURES.md 26, DATA.md section 6): resolved forecasts scored against persistence, per rule,
target and horizon, through the lifted forecast_cohort.review_arrival_cohort, with Brier scores and skill."""

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from core.eval import forecast_score

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
WEEK = date(2026, 9, 21)          # a Monday; the week runs to Sunday 27 September
BEFORE = date(2026, 9, 28)        # windows that closed before this day are scored
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)


def row(i, *, prob, persistence, observed, rule="logistic_v1", target="reach_rising", horizon=7, market="ZA",
        issue=date(2026, 9, 1)):
    fid = f"{rule}-{target}-{horizon}-{market}-{i}"
    return {"forecast_id": fid, "item_id": f"item{i}", "market": market, "target": target, "issue_date": issue,
            "horizon": horizon, "rule": rule, "prob": prob, "predicted_arrival": prob >= 0.5,
            "persistence_arrival": persistence, "resolve_date": issue + timedelta(days=horizon),
            "observed_arrival": observed}


def good_cohort(n_each=100, **kw):
    """Half arrive (prob 0.8), half do not (prob 0.3); persistence says every one arrives.
    Brier 0.065, persistence Brier 0.5, skill 0.87."""
    return ([row(i, prob=0.8, persistence=True, observed=True, **kw) for i in range(n_each)]
            + [row(n_each + i, prob=0.3, persistence=True, observed=False, **kw) for i in range(n_each)])


# The SQL files

@pytest.mark.parametrize("path", sorted(SQL_DIR.glob("*.sql")), ids=lambda p: p.name)
def test_sql_parses_as_bigquery_and_never_deletes_or_replaces(path):
    text = path.read_text(encoding="utf-8")
    for statement in sqlglot.parse(text, read="bigquery"):
        assert not isinstance(statement, (exp.Drop, exp.Delete, exp.TruncateTable, exp.Update, exp.Merge))
        if isinstance(statement, exp.Create):
            assert statement.args.get("exists"), f"{path.name}: CREATE without IF NOT EXISTS"
            assert not statement.args.get("replace"), f"{path.name}: CREATE OR REPLACE"
    assert "expiration" not in text.lower()


def test_forecast_score_table_is_append_only_in_the_agent_dataset():
    text = (SQL_DIR / "forecast_score_table.sql").read_text(encoding="utf-8")
    assert "`ogilvy-trends-v2.intelligence_42_agent.forecast_score`" in text
    assert "CREATE TABLE IF NOT EXISTS" in text


# The cohort score

def test_brier_persistence_brier_and_skill_on_a_hand_computed_cohort():
    [c] = forecast_score.score(good_cohort(), query_id="q1")
    assert (c["rule"], c["target"], c["horizon"]) == ("logistic_v1", "reach_rising", 7)
    assert c["n"] == 200 and c["unresolved"] == 0 and c["no_baseline"] == 0 and c["no_prob"] == 0
    assert c["brier"] == pytest.approx(0.065)
    assert c["persistence_brier"] == pytest.approx(0.5)
    assert c["skill"] == pytest.approx(0.87)
    assert c["promotion_eligible"] is True
    assert c["insufficient"] is False and c["reason"] is None
    assert c["query_id"] == "q1"
    assert c["minimum"] == 200


def test_below_200_resolved_is_insufficient_and_never_a_skill():
    rows = good_cohort()[:199]
    [c] = forecast_score.score(rows, query_id="q1")
    assert c["n"] == 199
    assert c["skill"] is None
    assert c["insufficient"] is True
    assert "199" in c["reason"] and "200" in c["reason"]
    assert c["promotion_eligible"] is False


def test_unresolved_rows_are_counted_but_not_scored_and_do_not_make_the_minimum():
    rows = good_cohort()[:190] + [row(500 + i, prob=0.9, persistence=True, observed=None) for i in range(20)]
    [c] = forecast_score.score(rows, query_id="q1")
    assert c["n"] == 190 and c["unresolved"] == 20
    assert c["skill"] is None and c["insufficient"] is True


def test_a_forecast_worse_than_persistence_has_negative_skill_and_is_not_eligible():
    rows = ([row(i, prob=0.3, persistence=True, observed=True) for i in range(150)]
            + [row(150 + i, prob=0.6, persistence=True, observed=False) for i in range(50)])
    [c] = forecast_score.score(rows, query_id="q1")
    # Brier (150 * 0.49 + 50 * 0.36) / 200 = 0.4575; persistence 50 / 200 = 0.25; skill 1 - 1.83 = -0.83
    assert c["brier"] == pytest.approx(0.4575)
    assert c["persistence_brier"] == pytest.approx(0.25)
    assert c["skill"] == pytest.approx(-0.83)
    assert c["promotion_eligible"] is False


def test_skill_of_exactly_zero_is_not_eligible():
    rows = [row(i, prob=1.0, persistence=True, observed=i < 100) for i in range(200)]
    [c] = forecast_score.score(rows, query_id="q1")
    assert c["skill"] == pytest.approx(0.0)
    assert c["promotion_eligible"] is False


def test_a_perfect_persistence_baseline_leaves_skill_undefined_with_a_reason():
    rows = [row(i, prob=0.9, persistence=True, observed=True) for i in range(200)]
    [c] = forecast_score.score(rows, query_id="q1")
    assert c["persistence_brier"] == 0
    assert c["skill"] is None and c["promotion_eligible"] is False
    assert "persistence" in c["reason"]


def test_rows_without_a_persistence_baseline_or_probability_are_counted_apart_not_raised():
    rows = good_cohort() + [row(900, prob=0.7, persistence=None, observed=True),
                            {**row(901, prob=0.7, persistence=True, observed=True), "prob": None},
                            {**row(902, prob=0.7, persistence=None, observed=True), "prob": None}]
    [c] = forecast_score.score(rows, query_id="q1")
    assert c["n"] == 200 and c["no_baseline"] == 1 and c["no_prob"] == 2
    assert c["skill"] == pytest.approx(0.87)


def test_cohorts_are_split_by_rule_target_and_horizon():
    rows = (good_cohort() + good_cohort(rule="ask_v1") + good_cohort(target="persist_50")
            + good_cohort(horizon=14)[:10])
    cohorts = forecast_score.score(rows, query_id="q1")
    keys = [(c["rule"], c["target"], c["horizon"]) for c in cohorts]
    assert keys == [("ask_v1", "reach_rising", 7), ("logistic_v1", "persist_50", 7),
                    ("logistic_v1", "reach_rising", 7), ("logistic_v1", "reach_rising", 14)]
    assert [c["insufficient"] for c in cohorts] == [False, False, False, True]


def test_the_lifted_review_decides_the_binary_comparison_it_reports():
    [c] = forecast_score.score(good_cohort(), query_id="q1")
    # predicted_arrival is prob >= 0.5: 0 errors; persistence says all arrive: 100 errors
    assert c["forecast_error"] == 0 and c["persistence_error"] == pytest.approx(0.5)
    assert c["beats_persistence"] is True


def test_positive_skill_with_more_binary_errors_than_persistence_is_not_eligible():
    # prob 0.49 on 150 arrivals: every one called wrong at 0.5 (150 errors against persistence's 50), yet
    # Brier 150 * 0.2601 / 200 = 0.195075 against persistence's 0.25, skill 1 - 0.7803 = 0.2197
    rows = ([row(i, prob=0.49, persistence=True, observed=True) for i in range(150)]
            + [row(150 + i, prob=0.0, persistence=True, observed=False) for i in range(50)])
    [c] = forecast_score.score(rows, query_id="q1")
    assert c["skill"] == pytest.approx(0.2197)
    assert c["beats_persistence"] is False
    assert c["promotion_eligible"] is False


def test_a_binary_tie_with_persistence_is_not_eligible_as_in_l2s_forecasts_score():
    # 100 errors each (prob 0.6 on the 100 that did not arrive); skill 1 - 0.185 / 0.5 = 0.63. L2's
    # forecasts.score takes review_arrival_cohort's beats_persistence, strictly fewer errors, so a tie fails.
    rows = ([row(i, prob=0.9, persistence=True, observed=True) for i in range(100)]
            + [row(100 + i, prob=0.6, persistence=True, observed=False) for i in range(100)])
    [c] = forecast_score.score(rows, query_id="q1")
    assert c["skill"] == pytest.approx(0.63)
    assert c["forecast_error"] == c["persistence_error"]
    assert c["promotion_eligible"] is False


def test_every_cohort_carries_the_hash_and_row_count_of_the_whole_read_whatever_its_order():
    rows = good_cohort() + good_cohort(target="persist_50")[:20]
    cohorts = forecast_score.score(rows, query_id="q1")
    again = forecast_score.score(list(reversed(rows)), query_id="q1")
    assert {c["row_count"] for c in cohorts} == {220}
    assert len({c["result_hash"] for c in cohorts + again}) == 1
    assert cohorts[0]["result_hash"].startswith("sha256:")
    assert forecast_score.score(rows[:-1], query_id="q1")[0]["result_hash"] != cohorts[0]["result_hash"]


# The pooled skill the weekly quality score reads

def test_pooled_skill_pools_squared_errors_over_every_resolved_row():
    rows = good_cohort() + good_cohort(rule="ask_v1", market="NG")
    fig = forecast_score.pooled_skill(rows, query_id="q9", result_hash="sha256:ab", row_count=410)
    assert fig["value"] == pytest.approx(0.87) and fig["n"] == 400
    assert fig["query_id"] == "q9" and fig["unit"]
    assert fig["result_hash"] == "sha256:ab" and fig["row_count"] == 410
    assert fig["insufficient"] is False


def test_pooled_skill_below_the_minimum_is_insufficient_never_zero():
    fig = forecast_score.pooled_skill(good_cohort()[:50], query_id="q9", result_hash="sha256:ab", row_count=50)
    assert fig["value"] is None and fig["n"] == 50 and fig["insufficient"] is True and fig["reason"]


# Against the DuckDB warehouse

def test_run_reads_the_current_row_of_each_closed_forecast(warehouse):
    wh = warehouse("forecasts")
    rows = good_cohort()
    issue_rows = [{**r, "observed_arrival": None} for r in rows]
    wh.insert("forecasts", issue_rows + rows)                  # issue row, then its resolution row
    late = row(999, prob=0.9, persistence=True, observed=None, issue=date(2026, 9, 25))  # window still open
    wh.insert("forecasts", [late])
    out = forecast_score.run(wh.execute, WEEK, dry_run=True, now=NOW)
    [c] = out["cohorts"]
    assert c["n"] == 200 and c["unresolved"] == 0
    assert c["skill"] == pytest.approx(0.87)
    assert c["query_id"] == "core/eval/sql/forecast_rows.sql?d=2026-09-28"


def test_dry_run_writes_nothing(warehouse):
    wh = warehouse("forecasts")
    wh.insert("forecasts", good_cohort())
    forecast_score.run(wh.execute, WEEK, dry_run=True, now=NOW)
    assert not wh.exists("forecast_score")
    assert wh.statements and not any(isinstance(st, (exp.Insert, exp.Create))
                                     for s in wh.statements for st in sqlglot.parse(s, read="bigquery"))


def test_a_real_run_creates_the_table_once_and_appends_every_run(warehouse):
    wh = warehouse("forecasts")
    wh.insert("forecasts", good_cohort() + good_cohort(target="cross_market")[:30])
    forecast_score.run(wh.execute, WEEK, dry_run=False, now=NOW)
    forecast_score.run(wh.execute, WEEK, dry_run=False, now=NOW + timedelta(hours=1))
    stored = wh.rows("forecast_score")
    assert len(stored) == 4
    first = [r for r in stored if r["target"] == "reach_rising"][0]
    assert first["week_start"] == WEEK and first["week_end"] == date(2026, 9, 27)
    assert first["skill"] == pytest.approx(0.87) and first["promotion_eligible"] is True
    assert first["n"] == 200 and first["query_id"] == "core/eval/sql/forecast_rows.sql?d=2026-09-28"
    assert first["row_count"] == 230 and first["result_hash"].startswith("sha256:")
    assert first["no_baseline"] == 0 and first["no_prob"] == 0
    thin = [r for r in stored if r["target"] == "cross_market"][0]
    assert thin["skill"] is None and thin["promotion_eligible"] is False and thin["reason"]
    assert len({r["run_id"] for r in stored}) == 2


def test_missing_forecasts_table_is_reported_not_raised(warehouse):
    wh = warehouse("feedback")
    out = forecast_score.run(wh.execute, WEEK, dry_run=True, now=NOW)
    assert out["cohorts"] == [] and "forecasts" in out["note"]


# The CLI

def test_cli_help_works():
    done = subprocess.run([sys.executable, "-m", "core.eval.forecast_score", "--help"],
                          capture_output=True, cwd=Path(__file__).resolve().parents[3])
    assert done.returncode == 0
    assert b"--dry-run" in done.stdout and b"--week-start" in done.stdout


def test_week_start_must_be_a_monday():
    with pytest.raises(SystemExit):
        forecast_score.main(["--week-start", "2026-09-22", "--dry-run"])
