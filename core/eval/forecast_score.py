"""Forecast scoring (FEATURES.md 26; DATA.md section 6; TRUST.md K9): 42's calls scored against persistence.

    py -3.13 -m core.eval.forecast_score [--week-start MONDAY] [--dry-run]

Reads the current row of every forecast whose window closed by the Sunday of the week (sql/forecast_rows.sql),
the engine's logistic_v1 rows and Ask's ask_v1 rows alike, and scores each rule, target and horizon apart through
the lifted forecast_cohort.review_arrival_cohort. Per cohort it reports n (resolved forecasts), the Brier score of
the forecast probability, the Brier score of the persistence baseline taken as 0 or 1, and skill, 1 minus their
ratio. Below 200 resolved forecasts (DATA.md section 6) a cohort is insufficient and carries no skill, only the
counts and Briers behind it. A row without a probability is counted as no_prob, and one with a probability but no
persistence baseline (Ask writes a NULL baseline when the item had no state) as no_baseline; both are left out of
the score rather than failing the cohort.

promotion_eligible is skill above 0 at 200 or more resolved AND review_arrival_cohort's beats_persistence, strictly
fewer binary errors (predicted_arrival against observed_arrival) than persistence, so a tie is not eligible. That is
the rule of L2's core/detect/forecasts.score: its Brier below persistence's is skill above 0, and it takes
beats_persistence from the same kernel. L2 scores the engine rule alone; this module scores every rule, adds skill,
and is the one the weekly quality score reads.

A real run appends one row per cohort to intelligence_42_agent.forecast_score (sql/forecast_score_table.sql,
CREATE TABLE IF NOT EXISTS); --dry-run prints the result and creates and writes nothing. Every cohort carries the
query_id of the read that produced it, with result_hash (sha256 over the rows it returned, sorted, so BigQuery's
row order does not change it) and row_count.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.agent.context import result_hash
from core.eval.forecast_cohort import review_arrival_cohort

SQL_DIR = Path(__file__).resolve().parent / "sql"
PROJECT = "ogilvy-trends-v2"
MINIMUM = 200  # DATA.md section 6: minimum_comparable_sample
SKILL_UNIT = ("Brier skill against persistence: 1 minus the forecast's summed squared error over persistence's, "
              "on resolved forecasts; 0 is no better than 'today holds'")


def load(name):
    return (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


def query_id(name, **params):
    base = f"core/eval/sql/{name}.sql"
    return base + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else base


def rows_hash(rows):
    """result_hash of a query's rows in a fixed order, so the same result always hashes the same."""
    return result_hash(sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str)))


def read(execute, name, params, table):
    """The rows of a named query, and a note instead of rows when its table does not exist yet (BigQuery's
    NotFound; DuckDB's CatalogException in the tests). Any other error is raised."""
    try:
        return execute(load(name), params)["rows"], None
    except Exception as err:
        if type(err).__name__ not in ("NotFound", "CatalogException"):
            raise
        return None, f"{table} is absent, so {name}.sql was not read"


def closed_rows(execute, d):
    """The current row of every forecast whose window closed before d, a note if the table is absent, and the
    query_id of the read."""
    rows, note = read(execute, "forecast_rows", {"d": d}, "intelligence_42_agent.forecasts")
    return rows, note, query_id("forecast_rows", d=d.isoformat())


def _has_prob(r):
    return r["prob"] is not None and r["predicted_arrival"] is not None


def _scorable(r):
    return _has_prob(r) and r["persistence_arrival"] is not None


def _brier(pairs):
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs) if pairs else None


def cohort(rows, *, query_id, minimum=MINIMUM):
    """Score one rule, target and horizon cohort. result_hash and row_count are left to score(), which has the
    whole read."""
    usable = [r for r in rows if _scorable(r)]
    review = review_arrival_cohort([{
        "forecast_id": r["forecast_id"], "forecast_issued": f"{str(r['issue_date'])[:10]}T00:00:00Z",
        "predicted_arrival": r["predicted_arrival"], "persistence_arrival": r["persistence_arrival"],
        "observed_arrival": r["observed_arrival"], "horizon": int(r["horizon"]),
        "provenance": {"rule": r["rule"], "target": r["target"], "item_id": r["item_id"], "market": r["market"]},
    } for r in usable], minimum_comparable_sample=minimum)
    done = [r for r in usable if r["observed_arrival"] is not None]
    n = review["resolved"]
    brier = _brier([(float(r["prob"]), float(r["observed_arrival"])) for r in done])
    base = _brier([(float(r["persistence_arrival"]), float(r["observed_arrival"])) for r in done])
    skill, reason = None, None
    if not review["comparable_sample_met"]:
        reason = f"{n} resolved forecasts, fewer than the {minimum} needed to score skill"
    elif base == 0:
        reason = "persistence was right on every resolved forecast, so skill against it is undefined"
    else:
        skill = 1 - brier / base
    no_prob = sum(not _has_prob(r) for r in rows)
    return {"n": n, "unresolved": review["unresolved"], "no_baseline": len(rows) - len(usable) - no_prob,
            "no_prob": no_prob, "minimum": minimum,
            "brier": brier, "persistence_brier": base, "skill": skill,
            "forecast_error": review["forecast_error"], "persistence_error": review["persistence_error"],
            "beats_persistence": review["beats_persistence"],
            "promotion_eligible": skill is not None and skill > 0 and review["beats_persistence"],
            "insufficient": not review["comparable_sample_met"], "reason": reason, "query_id": query_id}


def score(rows, *, query_id, minimum=MINIMUM):
    """One cohort per rule, target and horizon, in that sort order. rows is the whole read, and every cohort
    carries its result_hash and row_count."""
    source = {"result_hash": rows_hash(rows), "row_count": len(rows)}
    groups = defaultdict(list)
    for r in rows:
        groups[(r["rule"], r["target"], int(r["horizon"]))].append(r)
    return [{"rule": rule, "target": target, "horizon": horizon,
             **cohort(rs, query_id=query_id, minimum=minimum), **source}
            for (rule, target, horizon), rs in sorted(groups.items())]


def pooled_skill(rows, *, query_id, result_hash, row_count, minimum=MINIMUM):
    """One skill Figure over every resolved row given, whatever its rule, target or horizon. result_hash and
    row_count describe the whole read the rows were taken from."""
    done = [r for r in rows if _scorable(r) and r["observed_arrival"] is not None]
    n = len(done)
    fig = {"value": None, "unit": SKILL_UNIT, "query_id": query_id, "result_hash": result_hash,
           "row_count": row_count, "n": n, "minimum": minimum,
           "insufficient": n < minimum, "reason": None}
    if n < minimum:
        fig["reason"] = f"{n} resolved forecasts, fewer than the {minimum} needed to score skill"
        return fig
    err = sum((float(r["prob"]) - float(r["observed_arrival"])) ** 2 for r in done)
    base = sum((float(r["persistence_arrival"]) - float(r["observed_arrival"])) ** 2 for r in done)
    if base == 0:
        fig["reason"] = "persistence was right on every resolved forecast, so skill against it is undefined"
    else:
        fig["value"] = 1 - err / base
    return fig


def run(execute, week_start, *, dry_run, now):
    """Score the forecasts whose window closed by the Sunday ending week_start's week; append unless dry_run."""
    week_end = week_start + timedelta(days=6)
    rows, note, qid = closed_rows(execute, week_end + timedelta(days=1))
    cohorts = score(rows or [], query_id=qid)
    run_id = f"forecast-score-{week_start.isoformat()}-{now:%Y%m%dT%H%M%SZ}"
    if not dry_run and rows is not None:
        execute(load("forecast_score_table"), {})
        for c in cohorts:
            execute(load("forecast_score_insert"), {
                "week_start": week_start, "week_end": week_end, "run_id": run_id, "scored_at": now,
                "rule": c["rule"], "target": c["target"], "horizon": c["horizon"], "n": c["n"],
                "unresolved": c["unresolved"], "no_baseline": c["no_baseline"], "no_prob": c["no_prob"],
                "minimum": c["minimum"], "result_hash": c["result_hash"], "row_count": c["row_count"],
                "brier": c["brier"], "persistence_brier": c["persistence_brier"], "skill": c["skill"],
                "promotion_eligible": c["promotion_eligible"], "query_id": c["query_id"], "reason": c["reason"]})
    return {"week_start": week_start, "week_end": week_end, "run_id": run_id, "dry_run": dry_run,
            "cohorts": cohorts, "note": note}


def bigquery_execute(project=PROJECT):
    """execute(sql, params) on BigQuery with named scalar parameters. A None parameter goes as a STRING NULL, so the
    SQL casts every nullable one to its column type."""
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    types = {"date": "DATE", "datetime": "TIMESTAMP", "int": "INT64", "float": "FLOAT64", "str": "STRING",
             "bool": "BOOL", "NoneType": "STRING"}

    def execute(sql, params):
        config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(name, types[type(value).__name__], value) for name, value in params.items()])
        return {"rows": [dict(row.items()) for row in client.query(sql, job_config=config).result()]}

    return execute


def monday(text):
    d = date.fromisoformat(text)
    if d.weekday() != 0:
        raise argparse.ArgumentTypeError(f"{text} is not a Monday")
    return d


def last_full_week(today):
    """The Monday of the last week that has ended by today."""
    return today - timedelta(days=today.weekday() + 7)


def main(argv=None):
    p = argparse.ArgumentParser(prog="py -3.13 -m core.eval.forecast_score",
                                description="Score resolved forecasts against persistence (DATA.md section 6).")
    p.add_argument("--week-start", type=monday, help="Monday of the week to score (default: the last full week)")
    p.add_argument("--dry-run", action="store_true", help="read and print the scores; create and write nothing")
    a = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    out = run(bigquery_execute(), a.week_start or last_full_week(now.date()), dry_run=a.dry_run, now=now)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
