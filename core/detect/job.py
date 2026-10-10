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
A day whose understand run is partial with cluster_stack_failed (its clusterer produced nothing) still runs: state
judges no topic items that day, the detect counts carry topics_failed with the plain-words data_issue, and
every other item is judged as usual.
Lane L1's chain helper opens and closes the detect run and starts the next job. Every write appends.

Entry point: python -m core.detect.job. The run date is RUN_DATE when set, else today in SAST (chain.today).
"""

import json
import sys
import traceback
from pathlib import Path

from google.cloud import bigquery

from . import aggregate, breakout, centroids, coaction, forecasts, runs, seeds, sqlrun, stats, watches
from .items import TOPIC_KIND

PROJECT = "ogilvy-trends-v2"
RULE_VERSION = "warmup-1"
SQL = Path(__file__).parent / "sql"

ITEM_STATE_COUNT_SQL = "SELECT COUNT(*) n FROM {core}.item_state s WHERE s.metric_date = @d AND s.run_id = @run_id"
TOPIC_ITEM_STATE_COUNT_SQL = (
    "SELECT COUNT(*) n FROM {core}.item_state s WHERE s.metric_date = @d AND s.run_id = @run_id AND s.kind = @kind")

CATCH_UP_DAYS = 3

# Understand records a day whose clusterer produced nothing as ok but partial (counts.partial_reason). Detect then
# judges no topic items for that day and still runs for the rest.
TOPICS_PARTIAL_REASON = "cluster_stack_failed"
TOPICS_DATA_ISSUE = "Data issue: topic grouping failed today"
UNDERSTAND_COUNTS_SQL = """
SELECT r.counts FROM {agent}.runs r
WHERE r.stage = 'understand' AND r.run_date = @d AND r.status = 'ok'
ORDER BY r.finished_at DESC LIMIT 1
"""
# state.sql is verbatim DATA.md 3.7, so the topic stop is applied to its text here and not written into the file.
STATE_MAP_JOIN = "JOIN {core}.cultural_map cm ON cm.item_id = a.item_id AND cm.valid_to IS NULL"

# The detect counts keys of the steps that build views or centroids with no runs row of their own.
VIEW_STEPS = ("spread", "agent_views", "news", "item_centroids")

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


def without_topics(script):
    """state.sql with topic items left out of item_state. It stops rather than guess if the join it extends
    is no longer in the text exactly once."""
    if script.count(STATE_MAP_JOIN) != 1:
        raise RuntimeError("state.sql no longer has the cultural_map join detect extends to leave out topic items")
    return script.replace(STATE_MAP_JOIN, STATE_MAP_JOIN + f" AND cm.kind != '{TOPIC_KIND}'")


def topics_failed_today(client, d, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """{reason, data_issue} when the latest ok understand run for d is partial because its clusterer failed, else
    None. The latest ok run decides, so a repaired rerun lifts it."""
    rows = sqlrun.query(client, UNDERSTAND_COUNTS_SQL, {"d": d}, core=core, agent=agent)
    counts = rows[0]["counts"] if rows else None
    counts = json.loads(counts) if isinstance(counts, str) else counts
    if not isinstance(counts, dict) or counts.get("partial_reason") != TOPICS_PARTIAL_REASON:
        return None
    return {"reason": TOPICS_PARTIAL_REASON, "data_issue": counts.get("data_issue") or TOPICS_DATA_ISSUE}


def topic_items_judged(client, d, run_id, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """How many item_state rows of kind TOPIC_KIND this run's state step wrote for d, read back from item_state."""
    rows = sqlrun.query(client, TOPIC_ITEM_STATE_COUNT_SQL, {"d": d, "run_id": run_id, "kind": TOPIC_KIND},
                        core=core, agent=agent)
    return rows[0]["n"]


def run_topic_count_step(client, d, run_id, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """The topic count for the detect counts. It is information only, so a failure never stops detect: it is logged
    and returned as the status and error."""
    try:
        return topic_items_judged(client, d, run_id, core, agent)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"detect {d.isoformat()}: topic count failed: {error}", file=sys.stderr)
        return {"status": "failed", "error": error}


def run_state(client, d, run_id, rule_version, core=sqlrun.CORE, agent=sqlrun.AGENT, topics_failed=False):
    """Run the state.sql script (a temp function and the item_state INSERT) and return the rows it wrote. With
    topics_failed, topic items are not judged."""
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("d", "DATE", d),
        bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
        bigquery.ScalarQueryParameter("rule_version", "STRING", rule_version)])
    text = (SQL / "state.sql").read_text(encoding="utf-8")
    script = sqlrun.render(without_topics(text) if topics_failed else text, core, agent)
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
            "series_test": stats.run_stats(client, d, rid, rule_version, core=core, agent=agent)})["series_test"]
        counts["coaction"] = run_coaction_step(client, d, rule_version, core, agent)
        topics = topics_failed_today(client, d, core, agent)
        if topics:
            counts["topics_failed"] = topics
        counts["item_state"] = run_state(client, d, detect.run_id, rule_version, core, agent,
                                         topics_failed=bool(topics))
        if topics:
            topics["topic_items_judged"] = run_topic_count_step(client, d, detect.run_id, core, agent)
            if isinstance(topics["topic_items_judged"], int):
                print(f"detect {d.isoformat()}: {topics['data_issue']}; {topics['topic_items_judged']} topic items "
                      "judged", file=sys.stderr)
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
