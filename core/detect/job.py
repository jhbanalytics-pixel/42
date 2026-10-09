"""The detect Cloud Run job (DATA.md section 3 opening, BUILD.md 1.6 and 1.13).

For one run date: apply the views, v_item_waves, v_item_spread and the agent views L4's API reads, then aggregate
(item_daily), stats (series_test) and coaction (coord_signals and creators.coord_score, task 2.11), each under
its own run_id with its own runs row, then state.sql (item_state) under the detect run's id, then breakout
(today's breakout_signals rows, task 2.13), watch (today's watch_matches rows, task 2.9), seeds (tomorrow's
seed_queue rows, task 2.4) and forecast (DATA.md section 6: resolve closed forecasts, issue today's), each
under its own run_id. A coaction failure never stops state: its runs row reads 'skipped' when a library is
missing from the image and 'failed' otherwise. A breakout, watch, seeds or forecast failure never stops detect
or brief: its runs row reads 'failed' and the detect counts carry the error. Neither does an agent views
failure: it is logged and the detect counts carry the error under agent_views, and a v_item_spread failure
likewise under spread. Right after aggregate, the hashtag and sound centroids (centroids.py) are written to
cultural_map with no runs row of their own; a failure there is logged and carried under item_centroids, and never
stops detect. Those four builds (spread, agent_views, news, item_centroids) have no runs row of their own, so the
detect runs row names each one that failed, with its error, under view_failures in its counts ({} when none failed)
and the job logs one line naming them; the detect run still reads 'ok'. Before aggregating the run
date, any of the three days before it that has collection_health rows but no ok aggregate run is aggregated
first, each with its own runs row; stats, coaction and state run for the run date only.
Lane L1's chain helper opens and closes the detect run and starts the next job. Every write appends.

Entry point: python -m core.detect.job. The run date is RUN_DATE when set, else today in SAST (chain.today).
"""

import json
import sys
import time
import traceback
from pathlib import Path

from google.cloud import bigquery

from core.trust.locality import LOCALITY_AUTHORITY

from . import aggregate, breakout, centroids, coaction, forecasts, locality, runs, seeds, sqlrun, stats, watches

PROJECT = "ogilvy-trends-v2"


def rule_version_for(authority):
    """The version of the rule that writes item_state.eligible: warmup-1 while detect's own geo_status does, warmup-2
    once the retained locality_v2 row does (C4 v3 section 7.2), so a reader that groups by it sees the break."""
    return "warmup-1" if authority == "v1" else "warmup-2"


RULE_VERSION = rule_version_for(LOCALITY_AUTHORITY)
SQL = Path(__file__).parent / "sql"

ITEM_STATE_COUNT_SQL = "SELECT COUNT(*) n FROM {core}.item_state s WHERE s.metric_date = @d AND s.run_id = @run_id"

CATCH_UP_DAYS = 3

# The detect counts keys of the steps that build views or centroids with no runs row of their own.
VIEW_STEPS = ("spread", "agent_views", "news", "item_centroids", "locality_views")

# Whether detect builds v_sensitive_items_complete (sqlrun.agent_statements). The API turns creator item lists,
# recent posts and named communities on as soon as that view exists, so it stays False until Albert says go on
# staging; sqlrun.RELIGIOUS_HOLIDAYS_ARE_RELIGION must carry his answer first, or the agent views step fails soft.
SENSITIVE_COMPLETE_READY = False

# Days in the CATCH_UP_DAYS before @d with collection_health rows but no ok aggregate run. The panel series
# reads such a day as zero posts, so it is aggregated before @d.
MISSED_DAYS_SQL = """
SELECT DISTINCT h.day FROM {core}.collection_health h
WHERE h.day BETWEEN DATE_SUB(@d, INTERVAL @n DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
  AND NOT EXISTS (SELECT 1 FROM {agent}.runs r
                  WHERE r.stage = 'aggregate' AND r.status = 'ok' AND r.run_date = h.day)
ORDER BY h.day
"""

# Recovery dates where understand finished after the latest successful aggregate started and cluster output exists.
# v_good_runs selects each successful run id by finished_at.
LATE_CLUSTER_DAYS_SQL = """
SELECT DISTINCT k.cluster_date day
FROM {core}.clusters k
JOIN {core}.v_good_runs ag ON ag.stage = 'aggregate' AND ag.run_date = k.cluster_date
JOIN {agent}.runs ar ON ar.run_id = ag.run_id
JOIN {core}.v_good_runs ug ON ug.stage = 'understand' AND ug.run_date = k.cluster_date
JOIN {agent}.runs ur ON ur.run_id = ug.run_id
WHERE k.cluster_date BETWEEN DATE_SUB(@d, INTERVAL @n DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)
  AND ur.finished_at > ar.started_at
ORDER BY k.cluster_date
"""


def apply_waves(client, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Create or replace v_item_waves (sql/waves.sql), which state.sql reads; views.sql must come first."""
    for stmt in sqlrun.split(sqlrun.render((SQL / "waves.sql").read_text(encoding="utf-8"), core, agent)):
        client.query(stmt).result()


def _step(client, stage, d, agent, work):
    """Run work(run_id) under a fresh run_id for stage, then append its ok runs row. Returns work's counts."""
    run_id = runs.new_run_id(stage, d)
    started = runs.now()
    counts = work(run_id)
    runs.append(client, run_id, stage, d, "ok", started, runs.now(), counts, agent=agent)
    return counts


def run_coaction_step(client, d, rule_version, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Run coaction for d under its own run_id. It never stops state: a missing library appends a 'skipped'
    runs row and any other error a 'failed' one, and state then reads authenticity Not assessed because no
    ok coaction run exists for d. Returns the step's counts, or its status and error."""
    run_id = runs.new_run_id("coaction", d)
    started = runs.now()
    try:
        counts = coaction.run_coaction(client, d, run_id, rule_version, core, agent)
    except Exception as e:
        status = "skipped" if isinstance(e, ImportError) else "failed"
        error = f"{type(e).__name__}: {e}"
        runs.append(client, run_id, "coaction", d, status, started, runs.now(), error=error, agent=agent)
        return {"status": status, "error": error}
    runs.append(client, run_id, "coaction", d, "ok", started, runs.now(), counts, agent=agent)
    return counts


def run_seeds_step(client, d, detect_run_id, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Append tomorrow's seed_queue rows for d from the detect run's states under their own run_id. It never
    stops detect or brief: any error appends a 'failed' runs row and returns the status and error in place of
    the counts."""
    run_id = runs.new_run_id("seeds", d)
    started = runs.now()
    try:
        counts = seeds.run_seeds(client, d, detect_run_id, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        runs.append(client, run_id, "seeds", d, "failed", started, runs.now(), error=error, agent=agent)
        return {"status": "failed", "error": error}
    runs.append(client, run_id, "seeds", d, "ok", started, runs.now(), counts, agent=agent)
    return counts


def run_breakout_step(client, d, rule_version, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Append d's creator-breakout signals under their own run_id. It never stops detect or brief: any error
    appends a 'failed' runs row and returns the status and error in place of the counts."""
    run_id = runs.new_run_id("breakout", d)
    started = runs.now()
    try:
        counts = breakout.append_signals(client, d, run_id, rule_version, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        runs.append(client, run_id, "breakout", d, "failed", started, runs.now(), error=error, agent=agent)
        return {"status": "failed", "error": error}
    runs.append(client, run_id, "breakout", d, "ok", started, runs.now(), counts, agent=agent)
    return counts


def run_locality_shadow(client, d, detect_run_id, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """The read-only cross-tabulation of detect's geo_status against the checked v2 status for the run (section 10).
    A failed read is returned as a status and an error, and changes nothing."""
    try:
        return locality.shadow_comparison(client, d, detect_run_id, core, agent)
    except Exception as e:
        return {"status": "failed", "error": f"{type(e).__name__}: {e}"}


def run_locality_step(client, d, detect, core=sqlrun.CORE, agent=sqlrun.AGENT, *, max_bytes=locality.MAX_BYTES_BILLED,
                      compare=True):
    """Write and verify the locality_v2 rows of the detect run under their own run_id (C4 v3 section 8). It never
    stops state, brief or the chain: in shadow the rows are only evidence, and when authoritative a step that failed
    leaves its keys unread, which state carries as unreadable and the brief holds as a data issue. Any error, a key
    that fails write-time verification, a bytes refusal or a timeout appends a 'failed' runs row and returns the
    status and error in place of the counts. The runs row carries the step's duration and the bytes it billed
    (review Q15). compare: take the shadow cross-tabulation here, which needs the item_state rows of the run; the
    authoritative placement runs before state and takes it afterwards."""
    run_id = runs.new_run_id("locality", d)
    started = runs.now()
    began = time.monotonic()
    try:
        counts = locality.run_locality_step(client, d, detect, core, agent, max_bytes=max_bytes)
    except locality.LocalityFailed as e:
        runs.append(client, run_id, "locality", d, "failed", started, runs.now(), e.counts, error=str(e), agent=agent)
        return {"status": "failed", "error": str(e), **e.counts}
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        runs.append(client, run_id, "locality", d, "failed", started, runs.now(),
                    {"duration_s": round(time.monotonic() - began, 3)}, error=error, agent=agent)
        return {"status": "failed", "error": error}
    if compare:
        counts["locality_shadow"] = run_locality_shadow(client, d, detect.run_id, core, agent)
    runs.append(client, run_id, "locality", d, "ok", started, runs.now(), counts, agent=agent)
    return counts


def run_watch_step(client, d, detect_run_id, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Append today's watch_matches rows for the detect run under their own run_id. It never stops detect or
    brief: any error appends a 'failed' runs row and returns the status and error in place of the counts."""
    run_id = runs.new_run_id("watch", d)
    started = runs.now()
    try:
        counts = watches.run_watches(client, d, detect_run_id, run_id, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        runs.append(client, run_id, "watch", d, "failed", started, runs.now(), error=error, agent=agent)
        return {"status": "failed", "error": error}
    runs.append(client, run_id, "watch", d, "ok", started, runs.now(), counts, agent=agent)
    return counts


def run_forecast_step(client, d, detect_run_id, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Resolve closed forecasts and issue d's from the detect run's states, under their own run_id. It never
    stops detect or brief: any error appends a 'failed' runs row and returns the status and error in place of
    the counts."""
    run_id = runs.new_run_id("forecast", d)
    started = runs.now()
    try:
        counts = forecasts.run_forecasts(client, d, detect_run_id, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        runs.append(client, run_id, "forecast", d, "failed", started, runs.now(), error=error, agent=agent)
        return {"status": "failed", "error": error}
    runs.append(client, run_id, "forecast", d, "ok", started, runs.now(), counts, agent=agent)
    return counts


def apply_spread_step(client, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Apply v_item_spread (sql/spread.sql). Only the cards' spread reads it, so a failure never stops detect or
    brief: it is logged and returned as the status and error."""
    try:
        sqlrun.apply_spread(client, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"spread view failed: {error}", file=sys.stderr)
        return {"status": "failed", "error": error}
    return {"status": "ok"}


def apply_news_step(client, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Apply v_news_followthrough and v_item_origin (sql/news.sql). Only the news bridge and the cards' origin read
    them, so a failure never stops detect or brief: it is logged and returned as the status and error."""
    try:
        sqlrun.apply_news(client, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"news views failed: {error}", file=sys.stderr)
        return {"status": "failed", "error": error}
    return {"status": "ok"}


def apply_locality_views_step(client, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Apply v_item_locality_checked and v_item_locality_current (sql/locality_views.sql, C4 v3 section 7.1). The
    locality step's readers use them, so a failure is logged and returned as the status and error."""
    try:
        sqlrun.apply_locality_views(client, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"locality views failed: {error}", file=sys.stderr)
        return {"status": "failed", "error": error}
    return {"status": "ok"}


def apply_agent_views_step(client, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Apply the agent views L4's API reads. They serve only that API, so a failure never stops detect or
    brief: it is logged and returned as the status and error."""
    try:
        sqlrun.apply_agent_views(client, core, agent, sensitive_complete=SENSITIVE_COMPLETE_READY)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"agent views failed: {error}", file=sys.stderr)
        return {"status": "failed", "error": error}
    return {"status": "ok"}


def run_centroids_step(client, d, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Write d's hashtag and sound centroids (centroids.py). Only analogues read them, so a failure never stops
    detect or brief: it is logged and returned as the status and error."""
    try:
        return centroids.run_item_centroids(client, d, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"item centroids failed: {error}", file=sys.stderr)
        return {"status": "failed", "error": error}


# state.sql reads this run's rows from v_item_locality_checked. In shadow nothing in state decides from them, so the
# script that runs under the v1 authority reads an empty relation of the same columns instead (C4 v3 section 10): a
# failed or missing locality view, or locality DDL that has not landed, never fails detect before the switch.
_CHECKED_VIEW = "{core}.v_item_locality_checked k"
_NO_CHECKED_ROWS = ("(SELECT CAST(NULL AS DATE) run_date, CAST(NULL AS STRING) item_id, CAST(NULL AS STRING) market, "
                    "CAST(NULL AS STRING) detect_run_id, CAST(NULL AS STRING) checked_status LIMIT 0) k")


def state_script(authority, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """The text of state.sql to run for a locality authority: the file as it is under v2, and with the checked view
    replaced by the empty relation under v1."""
    sql = (SQL / "state.sql").read_text(encoding="utf-8")
    if authority != "v2":
        assert sql.count(_CHECKED_VIEW) == 1
        sql = sql.replace(_CHECKED_VIEW, _NO_CHECKED_ROWS)
    return sqlrun.render(sql, core, agent)


def run_state(client, d, run_id, rule_version, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Run the state.sql script (a temp function and the item_state INSERT) and return the rows it wrote."""
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("d", "DATE", d),
        bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        bigquery.ScalarQueryParameter("rule_version", "STRING", rule_version),
        bigquery.ScalarQueryParameter("authority", "STRING", LOCALITY_AUTHORITY)])
    script = state_script(LOCALITY_AUTHORITY, core, agent)
    client.query(script, job_config=config).result()
    rows = sqlrun.query(client, ITEM_STATE_COUNT_SQL, {"d": d, "run_id": run_id}, core=core, agent=agent)
    return rows[0]["n"]


def view_failures(counts):
    """{step: error} for each VIEW_STEPS step in counts that failed, in VIEW_STEPS order."""
    return {k: counts[k]["error"] for k in VIEW_STEPS
            if isinstance(counts.get(k), dict) and counts[k].get("status") == "failed"}


def _note_view_failures(d, counts):
    counts["view_failures"] = failed = view_failures(counts)
    if failed:
        print(f"detect {d.isoformat()}: view builds failed: {', '.join(failed)}", file=sys.stderr)


def run(client, d, *, chain, rule_version=RULE_VERSION, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Detect for day d. On any failure after begin, finishes the detect run as failed and re-raises."""
    detect = chain.begin("detect", d)
    counts = {}
    try:
        sqlrun.apply_views(client, core, agent)
        apply_waves(client, core, agent)
        counts["locality_views"] = apply_locality_views_step(client, core, agent)
        counts["spread"] = apply_spread_step(client, core, agent)
        counts["agent_views"] = apply_agent_views_step(client, core, agent)
        counts["news"] = apply_news_step(client, core, agent)
        missed = sqlrun.query(client, MISSED_DAYS_SQL, {"d": d, "n": CATCH_UP_DAYS}, core=core, agent=agent)
        late_clusters = sqlrun.query(client, LATE_CLUSTER_DAYS_SQL,
                                     {"d": d, "n": CATCH_UP_DAYS}, core=core, agent=agent)
        counts["catch_up"] = {}
        for m in sorted({r["day"] for r in missed} | {r["day"] for r in late_clusters}):
            counts["catch_up"][m.isoformat()] = _step(
                client, "aggregate", m, agent,
                lambda rid, m=m: aggregate.run_aggregate(client, m, rid, rule_version, core, agent))
        counts["aggregate"] = _step(client, "aggregate", d, agent,
                                    lambda rid: aggregate.run_aggregate(client, d, rid, rule_version, core, agent))
        counts["item_centroids"] = run_centroids_step(client, d, core, agent)
        counts["series_test"] = _step(client, "stats", d, agent, lambda rid: {
            "series_test": stats.run_stats(client, d, rid, rule_version, core=core)})["series_test"]
        counts["coaction"] = run_coaction_step(client, d, rule_version, core, agent)
        if LOCALITY_AUTHORITY == "v2":
            # State reads the checked view for its own run, so the rows must exist first (section 8.1).
            counts["locality"] = run_locality_step(client, d, detect, core, agent, compare=False)
            counts["item_state"] = run_state(client, d, detect.run_id, rule_version, core, agent)
            counts["locality_shadow"] = run_locality_shadow(client, d, detect.run_id, core, agent)
        else:
            counts["item_state"] = run_state(client, d, detect.run_id, rule_version, core, agent)
            counts["locality"] = run_locality_step(client, d, detect, core, agent)
            counts["locality_shadow"] = counts["locality"].pop("locality_shadow", None)
        counts["breakout"] = run_breakout_step(client, d, rule_version, core, agent)
        counts["watch"] = run_watch_step(client, d, detect.run_id, core, agent)
        counts["seeds"] = run_seeds_step(client, d, detect.run_id, core, agent)
        counts["forecast"] = run_forecast_step(client, d, detect.run_id, core, agent)
    except Exception as e:
        _note_view_failures(d, counts)
        chain.finish(detect, "failed", counts, error=f"{type(e).__name__}: {e}")
        raise
    _note_view_failures(d, counts)
    chain.finish(detect, "ok", counts)
    chain.start_next("detect", d)
    return counts


def main(client=None, chain=None, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """Exit code: 0 on success or AlreadyDone, 1 on UpstreamNotReady or any failure. On AlreadyDone,
    chain.restart_next starts brief when detect is ok for the day and brief has no runs row for it."""
    if chain is None:
        from core.collect import chain
    if client is None:
        client = bigquery.Client(project=PROJECT)
    d = chain.today()
    try:
        counts = run(client, d, chain=chain, core=core, agent=agent)
    except chain.AlreadyDone as e:
        print(f"detect {d.isoformat()}: nothing to do, {e}")
        try:
            started = chain.restart_next("detect", d)
        except Exception:
            traceback.print_exc()
            return 1
        if started:
            print(f"detect {d.isoformat()} was ok and brief had not started; started {started}")
        return 0
    except chain.UpstreamNotReady as e:
        print(f"detect {d.isoformat()}: {e}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    print(json.dumps({"detect": d.isoformat(), "counts": counts}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
