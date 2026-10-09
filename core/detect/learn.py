"""The weekly learn job (BUILD.md 2.7, ENGINE.md section 6): writes the detection scorecard.

Entry point: python -m core.detect.learn [--week YYYY-MM-DD], run weekly. --week names the week's Monday; without
it the job scores the last complete Monday to Sunday week in SAST (chain.today, so RUN_DATE sets the day it
counts back from).

run_scorecard (core/detect/scorecard.py) computes one row per market, and the job appends them in one INSERT to
intelligence_42_agent.engine_scorecard: week_start, week_end, market, run_id,
rule_version, then one JSON Figure per metric. Then it appends one runs row (core/detect/runs.py), stage 'learn',
run_date the week's Monday, under the run_id the Figures carry; counts hold model_usd 0.0, since no model is
called.

Append only; nothing is replaced or removed. A week and market already written by an ok learn run is skipped, so
a rerun adds nothing. Rows of a failed run never count as written. When every market is already written the
scorecard is not computed and the runs row reads 'skipped'. FORCE_RERUN=1 writes every market again, and the
latest run_id per week and market wins.

Until lane L1 creates engine_scorecard, the job computes the rows, prints them, appends a 'skipped' runs row with
the reason and exits 0. Any other failure appends a 'failed' runs row and exits 1.

After the scorecard the same run scores forecasts and the weekly quality (ENGINE.md section 3, forecast scoring in
Monday's learn; FEATURES.md 26 and 7) through the packaged core.eval modules, on learn's client, calling no model:
core.eval.forecast_score scores every forecast whose window closed by the week's Sunday per rule, target and
horizon against persistence and appends one forecast_score row per cohort, promotion_eligible included; then
core.eval.quality_score appends one weekly_quality row per market (ALL, ZA, NG, KE), reading the scorecard just
written. With no forecast closed there is no cohort and no forecast_score row; closed forecasts not yet resolved
make a cohort with n 0, insufficient, never eligible. core/schema/agent.sql creates both tables (apply.py) and
learn never does: while one is missing its result is printed and counted with a note and nothing is written to it.
Scoring runs inside the learn run, so a scoring failure appends the same 'failed' runs row and exits 1, and the
retry writes the week again. When every market is already written by an ok learn run, scoring is skipped too, so a
rerun adds nothing; FORCE_RERUN=1 scores again (append only, the newest scored_at is current). The runs row's
counts carry each score's run_id, the rows written and the promotion eligible cohorts as rule/target/horizon.
Nothing here acts on promotion_eligible: forecasts stay hidden (TRUST.md K9) until a reader is built to check it.

The same run then measures how published cards and held items fared (score_outcomes, core/eval/card_outcome.py). It is
information only: it reads through the card outcome runner's queries and byte caps, writes no table, feeds no gate,
threshold, card or hold, and a failure of it is recorded in the runs row's counts under card_outcome without changing
the run's status. Like the scoring it is skipped when every market is already written. The step runs under one
deadline for all its reads, OUTCOME_DEADLINE_SECONDS (480; LEARN_OUTCOME_DEADLINE_SECONDS sets a shorter one, and a
longer value is clamped to 480 and recorded under card_outcome as deadline_clamped), well inside
the job's 30 minute task: when it passes the step is recorded as {"status": "timeout"} and the runs row is written at
once, so a slow warehouse cannot cost the run its row. It reads the core and agent datasets the run was given.
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from core.collect import chain
from core.eval import card_outcome, card_outcome_read
from core.eval import forecast_score, quality_score

from . import aggregate, runs, scorecard, sqlrun
from .sqlrun import AGENT, CORE

PROJECT = "ogilvy-trends-v2"
OUTCOME_DEADLINE_SECONDS = 480
TABLE = "engine_scorecard"
KEYS = (("week_start", "DATE"), ("week_end", "DATE"), ("market", "STRING"), ("run_id", "STRING"),
        ("rule_version", "STRING"))
FIGURES = ("time_to_detect", "lead_time", "precision", "recall", "breadth_platforms", "expansion_cluster_share",
           "expansion_platform_share", "expansion_language_share", "cost_per_confirmed")
FIELDS = KEYS + tuple((f, "STRING") for f in FIGURES)   # Figures travel as JSON text, parsed in the INSERT

APPEND_SQL = """
INSERT INTO {agent}.engine_scorecard (week_start, week_end, market, run_id, rule_version, time_to_detect,
  lead_time, precision, recall, breadth_platforms, expansion_cluster_share, expansion_platform_share,
  expansion_language_share, cost_per_confirmed)
SELECT n.week_start, n.week_end, n.market, n.run_id, n.rule_version, PARSE_JSON(n.time_to_detect),
  PARSE_JSON(n.lead_time), PARSE_JSON(n.precision), PARSE_JSON(n.recall), PARSE_JSON(n.breadth_platforms),
  PARSE_JSON(n.expansion_cluster_share), PARSE_JSON(n.expansion_platform_share),
  PARSE_JSON(n.expansion_language_share), PARSE_JSON(n.cost_per_confirmed)
FROM UNNEST(@rows) n
"""

# The markets of the week already written by an ok learn run.
DONE_SQL = """
SELECT DISTINCT s.market FROM {agent}.engine_scorecard s
JOIN {agent}.runs r ON r.run_id = s.run_id
WHERE s.week_start = @week_start AND r.stage = 'learn' AND r.status = 'ok'
"""


def last_week(today):
    """The Monday of the last Monday to Sunday week that ended before today."""
    return today - timedelta(days=today.weekday() + 7)


def week_for(now=None):
    return last_week(chain.today(now))


def table_exists(client, agent=AGENT, table=TABLE):
    try:
        client.get_table(f"{agent}.{table}")
    except NotFound:
        return False
    return True


def score_execute(client, agent=AGENT):
    """The core.eval modules' execute(sql, params) on learn's client. Their SQL names the staging agent dataset in
    full, so another agent dataset (the tests' DuckDB schema) is put in its place. Leading comment lines are left
    off, so each statement starts with its verb. A None parameter goes as a STRING NULL, which that SQL casts to its
    column type."""
    def execute(sql, params):
        sql = sqlrun._strip_leading_comments(sql)
        if agent != AGENT:
            sql = sql.replace(f"`{PROJECT}.{AGENT}.", f"`{agent}.")
        config = bigquery.QueryJobConfig(query_parameters=[sqlrun._param(k, v) for k, v in params.items()])
        return {"rows": [dict(row.items()) for row in client.query(sql, job_config=config).result()]}
    return execute


def score_week(client, week_start, now, agent=AGENT, scorecard_run_id=None):
    """Score the week's forecasts, then its quality, each written only when its table exists. The quality's
    precision reads scorecard_run_id's rows, the scorecard this learn run has just written, before any other
    attempt's. Returns the counts for the runs row and the full results to print."""
    execute = score_execute(client, agent)
    missing = "{agent}.{table} does not exist (core/schema/apply.py makes it); the scores are printed and none written"

    exists = table_exists(client, agent, "forecast_score")
    fs = forecast_score.run(execute, week_start, dry_run=not exists, now=now)
    written = len(fs["cohorts"]) if exists and fs["note"] is None else 0
    notes = [n for n in (fs["note"], None if exists else missing.format(agent=agent, table="forecast_score")) if n]
    counts = {"forecast_score": {
        "written": written, "run_id": fs["run_id"] if written else None, "note": "; ".join(notes) or None,
        "promotion_eligible": [f"{c['rule']}/{c['target']}/{c['horizon']}" for c in fs["cohorts"]
                               if c["promotion_eligible"]]}}

    exists = table_exists(client, agent, "weekly_quality")
    q = quality_score.run(execute, week_start, dry_run=not exists, now=now, scorecard_run_id=scorecard_run_id)
    counts["quality"] = {"written": len(q["rows"]) if exists else 0, "run_id": q["run_id"] if exists else None,
                         "notes": q["notes"]}
    if not exists:
        counts["quality"]["note"] = missing.format(agent=agent, table="weekly_quality")
    return counts, {"forecast_score": fs, "quality": q}


def outcome_deadline_setting():
    """(seconds, requested): the seconds the outcome step may take in all, and the environment's value when that
    was over the limit and so was clamped, else None. LEARN_OUTCOME_DEADLINE_SECONDS sets the deadline when it is a
    positive number, but never above OUTCOME_DEADLINE_SECONDS: a longer wait would cost the run its row (O3)."""
    try:
        seconds = float(os.environ.get("LEARN_OUTCOME_DEADLINE_SECONDS", ""))
    except ValueError:
        return OUTCOME_DEADLINE_SECONDS, None
    if not 0 < seconds < float("inf"):
        return OUTCOME_DEADLINE_SECONDS, None
    if seconds > OUTCOME_DEADLINE_SECONDS:
        return float(OUTCOME_DEADLINE_SECONDS), seconds
    return seconds, None


def outcome_deadline():
    """Seconds the outcome step may take in all: see outcome_deadline_setting."""
    return outcome_deadline_setting()[0]


def score_outcomes(client, week_start, core=CORE, agent=AGENT):
    """The card and hold outcome measure (core/eval/card_outcome.py, METHOD-GAPS section 7), run weekly. It reads the
    four weeks of run dates ending a week before the week's Sunday, so the newest has its t + 7 day inside the week,
    through the manual runner's own queries and byte caps (core/eval/card_outcome_read.py), on the core and agent
    datasets learn was given, and returns the held rate groups of all markets for the runs row. Information only:
    nothing is written to the warehouse, nothing reads the result, and a failure, a read over the cap or a deadline
    passed is returned as the status and the error text so the learn run itself is unaffected. The whole step has
    one deadline (outcome_deadline): the reads get it, and the step stops waiting for them when it passes, so it
    returns "timeout" at the deadline and a read that finishes later changes nothing. The groups hold counts and
    rates, never an item, title or row."""
    end = week_start - timedelta(days=1)
    start = end - timedelta(days=7 * card_outcome.WEEKS - 1)
    seconds, requested = outcome_deadline_setting()
    deadline = time.monotonic() + seconds
    out = {"definition": card_outcome.DEFINITION, "horizon": card_outcome.HEADLINE,
           "window": [start.isoformat(), end.isoformat()], "status": "ok", "error": None}
    if requested is not None:
        out["deadline_clamped"] = {"requested": requested, "used": seconds}
    done = {}

    def work():
        result = dict(out)
        try:
            got, metas = card_outcome_read.read_inputs(client, card_outcome_read.QUERIES, start, end, core=core,
                                                       agent=agent, deadline=deadline)
            rows = card_outcome.build_outcomes(card_outcome_read.briefs(got["briefs"]), got["states"],
                                               [r["run_date"] for r in got["detect_days"]])
            keep = ("kind", "market", "hold_reason", "stratum", "n", "held", "listed", "collapsed", "other",
                    "unmeasured", "pending", "text")
            result["groups"] = [{k: g[k] for k in keep} for g in card_outcome.summarize(rows, end=end)
                                if g["market"] == "ALL" and g["hold_reason"] is None]
            result["queries"] = [{k: m[k] for k in ("name", "estimated_bytes", "bytes_billed", "row_count")}
                                 for m in metas]
            result["bytes_billed"] = sum(m["bytes_billed"] or 0 for m in metas)
        except card_outcome_read.DeadlineExceeded as e:
            result.update(status="timeout", error=f"{type(e).__name__}: {e}")
        except Exception as e:
            result.update(status="error", error=f"{type(e).__name__}: {e}")
        done["result"] = result

    worker = threading.Thread(target=work, name="learn-card-outcome", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive() or "result" not in done:
        return {**out, "status": "timeout", "error": f"the outcome step passed its {seconds:g} second deadline"}
    return done["result"]


def rows_param(rows):
    """The scorecard rows as the @rows struct array of APPEND_SQL, each Figure as JSON text."""
    records = [{**{k: r[k] for k, _ in KEYS}, **{f: json.dumps(r[f]) for f in FIGURES}} for r in rows]
    return aggregate._struct_array("rows", records, FIELDS)


def run(client, week_start, *, core=CORE, agent=AGENT, now=None):
    """Score and append week_start's week, score its forecasts and quality, then append the runs row. Returns the
    run_id, status, error, counts, the rows written or, when the table is missing, the rows that would have been,
    and the scores. On any failure appends a 'failed' runs row and re-raises."""
    run_id = runs.new_run_id("learn", week_start)
    started = runs.now()
    counts = {"week_start": week_start.isoformat(), "week_end": (week_start + timedelta(days=6)).isoformat()}
    scores = {}
    try:
        exists = table_exists(client, agent)
        done = set()
        if exists and os.environ.get("FORCE_RERUN") != "1":
            done = {r["market"] for r in sqlrun.query(client, DONE_SQL, {"week_start": week_start},
                                                      core=core, agent=agent)}
        counts["already_written"] = [m for m in scorecard.MARKETS if m in done]
        rows = []
        if len(done) < len(scorecard.MARKETS):
            rows = [r for r in scorecard.run_scorecard(client, week_start, run_id=run_id, core=core, agent=agent)
                    if r["market"] not in done]
        if not exists:
            status = "skipped"
            error = (f"{agent}.{TABLE} does not exist yet; "
                     f"{len(rows)} rows printed, none written")
        elif not rows:
            status, error = "skipped", "every market already written by an ok learn run; FORCE_RERUN=1 writes again"
        else:
            aggregate._run(client, APPEND_SQL, [rows_param(rows)], core, agent)
            status, error = "ok", None
        counts["written"] = [r["market"] for r in rows] if status == "ok" else []
        if len(done) < len(scorecard.MARKETS):
            score_counts, scores = score_week(client, week_start, now or datetime.now(timezone.utc), agent,
                                              scorecard_run_id=run_id if status == "ok" else None)
            counts.update(score_counts)
            counts["card_outcome"] = score_outcomes(client, week_start, core, agent)
    except Exception as e:
        runs.append(client, run_id, "learn", week_start, "failed", started, runs.now(), counts,
                    error=f"{type(e).__name__}: {e}", agent=agent)
        raise
    runs.append(client, run_id, "learn", week_start, status, started, runs.now(), counts, error=error, agent=agent)
    return {"run_id": run_id, "status": status, "error": error, "counts": counts, "rows": rows, "scores": scores}


def _monday(text):
    d = date.fromisoformat(text)
    if d.weekday() != 0:
        raise argparse.ArgumentTypeError(f"{text} is not a Monday")
    return d


def main(argv=None, client=None, now=None, core=CORE, agent=AGENT):
    """Exit code: 0 when the week is written or skipped, 1 on any failure."""
    ap = argparse.ArgumentParser(prog="python -m core.detect.learn",
                                 description="Append the week's detection scorecard to engine_scorecard.")
    ap.add_argument("--week", type=_monday,
                    help="the week's Monday, YYYY-MM-DD; default the last complete week in SAST")
    a = ap.parse_args(argv)
    week = a.week or week_for(now)
    if week + timedelta(days=6) >= chain.today(now):
        # An unfinished week would be written as ok and then skipped by the Monday run for good.
        print(f"learn: the week of {week.isoformat()} has not ended; only complete weeks are scored", file=sys.stderr)
        return 2
    if client is None:
        client = bigquery.Client(project=PROJECT)
    try:
        res = run(client, week, core=core, agent=agent, now=now)
    except Exception:
        traceback.print_exc()
        return 1
    print(json.dumps({"learn": week.isoformat(), **res}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
