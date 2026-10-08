"""The understand job (f42-understand, runs as f42-enricher): embed posts, enrich RUN_DATE's posts with the fast model
(enrich.py), cluster them (cluster.py), then start detect. Every model spend is booked in runs (embed.book_spend):
each embed window's ceiling before the window is sent and a correction to its true spend after it returns,
enrichment's spend inside run_enrich and the label net's inside run_cluster as they happen. A run killed mid-window
leaves that window's ceiling booked (at most USD 15.36, 50,000 posts at the model's 2,048-token input, and never
below the window's true spend), so the daily cap never sees less than was spent. The understand row gets every
step's counts, the booked spend under booked_model_usd, and in model_usd only the spend no booking row holds, so the
daily spend query counts nothing twice.

Embed spend is booked on today's date in Africa/Johannesburg, not RUN_DATE, since the cap reads today: a backfill of
a past day spends today. Today is the day the job started. A run that passes midnight SAST keeps completed and
in-flight reservations and corrections on that day, and refuses to begin another paid model chunk. Before each
window the job reads that day's model spend (core.agent.ask.model_spend_today,
imported only then) and sends only as many posts as fit under MODEL_DAILY_USD at the most a post can cost
(cap_limited when that is fewer than a whole window). When the spend cannot be read, or a window's ceiling cannot be
booked, it sends no more windows (spend_unknown, with the error class in spend_error) and the run goes on.
A window that raises keeps its ceiling booked, since what it embedded cannot be read back, so a failing embed
retried all day could book a ceiling each time. Before each window the job also reads today's embed ceilings that
no correction followed (embed.uncorrected_today); once they reach one window's ceiling (USD 15.36) it sends no more
windows (retry_capped, with the amount in uncorrected_usd) and the run goes on.

An embed failure fails the run, since detect needs the embeddings. Enrichment fails soft, so it never stops the
morning brief: its error goes into counts as enrich_error (the exception class and its first line, URLs removed)
next to whatever enrichment counted before it raised (spend, a submitted batch job, closed jobs), the run finishes
ok and detect starts. run_enrich is imported only when enrichment starts, so an import failure in core.agent (which
enrich reads for the model and today's spend) fails enrichment the same soft way and never the embeddings.

Enrichment and clustering run only when RUN_DATE is today in Africa/Johannesburg. A backfill or rerun of a past day
embeds and skips both, with enrich_skipped_backfill true and cluster {"skipped": "backfill"} in counts: the daily
model cap reads today's spend, so a past day's enrichment would book its spend on a day the cap never checked, and
clustering a past day would move an item's last_seen back, count it as seen recently, match it to items first seen
after that day and fold old posts into today's centroids.

After enrichment, run_cluster clusters RUN_DATE for each of CLUSTER_MARKETS in turn, za, ng and ke and then
pan, the three pooled, which is the order and the set cluster.MARKETS takes. Each market's counts go into counts
under cluster, keyed by market. Clustering fails soft per market: an error goes in as that market's counts, the
error class and its first line with URLs removed, plus failed_batch and unwritten_cluster_ids when the write
reports them, and the next market still runs. run_cluster is imported only when clustering starts, so a failed
import of the BERTopic stack fails every market the same way. Each market's run sends its
new labels and keywords through the label net (cluster.label_net, one fast-model call) and books that spend on the
job's day under its run_id, as enrichment does; the spend joins the understand row's booked_model_usd, and any part
a booking missed its model_usd, also when the market's write raised after the net. A market clustered already that
day is skipped, so a rerun changes nothing. Memory is collected after each market, so the job's peak is one
market's fit, and each understand_phase line carries peak_rss_mb, the process's peak RSS so far, where it can be read.

One kind of clustering error is not soft: the stack itself being unusable (clusterer_unavailable: a module that
will not import, or numba unable to cache UMAP's compiled functions). Every market then fails the same way and a
night with no clusters is no night to brief on, as 1 to 4 Oct 2026 showed, when each run ended ok with zero clusters
and detect and the brief ran on nothing. The run is finished failed with the error and the per-market counts on its
row, detect is not started, video is skipped, and the job exits 1.

An ok run with enrich_error, or with any other error under a market in cluster, is a degraded run: Coverage reads those
counts and shows the understand stage as degraded with what failed (core/api/store.py, runs_of_day), while the
status stays ok and detect still starts, so the morning brief is never held for it. A run whose embed step sent
no window (retry_capped or spend_unknown, with nothing embedded) says so in embed_error, so the day with no
embeddings is not a bare ok. The job reads those counts back itself (degraded_reasons): a run with any soft failure,
embed_error included, prints one "understand degraded:" line on stderr naming each failed step ("embed", "enrich",
"cluster:<market>") with its error. The counts are left as they were, since Coverage and the run tests read them whole.

After clustering, run_video reads the clips that matter in today's clusters (video.py, BUILD.md 2.6), skipped on a
backfill and once the day has changed. A day change while it runs ends the video step only, never the run: the step
stops between clips under its own VIDEO_BUDGET_SECONDS, so the run always reaches its understand row and detect.
While VIDEO_DAILY (core/config/caps.yaml) has zero clips or zero credits it does nothing and the row's video is
{"off": true}; when the caps file cannot be read, {"error": "caps unreadable: ..."}; on a backfill or after a day
change, {"skipped": "backfill"} or {"skipped": "day_changed"}. Every finished run so has video counts, and a run row
with none is one that never finished. Otherwise its counts go in under video, its booked spend joins booked_model_usd
and any part a booking missed joins model_usd. It fails soft like clustering: an error goes in as video's error, next
to whatever it counted before it raised, and detect still starts.

    python -m core.understand.job              embeds and enriches RUN_DATE's posts, then clusters them
                                               (RUN_DATE defaults to today, SAST)
    EMBED_DAYS=90 python -m core.understand.job   backfill: 90 days in 7-day slices; enrich and clustering skip a
                                                  past RUN_DATE

A backfill on a day understand already ran ok needs FORCE_RERUN=1 as well, or chain.begin refuses it with
AlreadyDone and the job exits 0 having done nothing but chain.restart_next, which starts detect only when understand
is ok for that day and detect has no runs row for it (a start_next that failed after the ok row), and exits 1 when
that start fails. With FORCE_RERUN=1 the backfill runs, and start_next is
then called again for that day: detect is already done, so it exits clean on AlreadyDone.

BigQuery is reached with the job's own default credentials. Nothing here impersonates another account. Every
statement runs with job_retry=None: by default the client restarts a query job that failed on backendError,
internalError or rateLimitExceeded as a new job inside the same call, so an embed window that failed after
ML.GENERATE_EMBEDDING billed would embed again under one booked ceiling, its first spend never reaching the ledger.
The transport retry stays: it resends the same job id and fetches that job when it already exists, so it starts no
second job.
"""
import gc
import importlib
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from core.config.caps import video_daily
from core.understand.embed import book_spend, run_embed, spend_today, uncorrected_today

PROJECT = "ogilvy-trends-v2"
MISSING_CHAIN = ("core.collect.chain is not on this branch yet: the job chain helper arrives with the evening "
                 "merge of full-42-l1. Run the understand job only after that merge.")
SAST = timezone(timedelta(hours=2))  # Africa/Johannesburg keeps no daylight saving
CLUSTER_MARKETS = ("za", "ng", "ke", "pan")  # cluster.MARKETS, kept here so the job imports no BERTopic up front


def now():
    return datetime.now(timezone.utc)


def peak_rss_mb():
    """This process's peak resident memory so far in MiB (resource.getrusage), or None where the resource module is
    missing, as on Windows."""
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB on Linux, bytes on macOS
    return float(peak) / (1024 * 1024 if sys.platform == "darwin" else 1024)


def phase_line(run_id, phase, event, elapsed):
    """One understand_phase log line: the phase's elapsed seconds and, where it can be read, the peak RSS so far."""
    line = f"understand_phase run_id={run_id} phase={phase} event={event} elapsed_seconds={elapsed:.3f}"
    peak = peak_rss_mb()
    return line if peak is None else f"{line} peak_rss_mb={peak:.1f}"


def run_enrich(execute, **kwargs):
    """enrich.run_enrich, imported here so a failed import of core.agent fails enrichment soft, not the job."""
    from core.understand.enrich import run_enrich as enrich_posts

    return enrich_posts(execute, **kwargs)


def run_cluster(execute, **kwargs):
    """cluster.run_cluster, imported here so a failed import of the BERTopic stack fails clustering soft, not the
    job."""
    from core.understand.cluster import run_cluster as cluster_posts

    return cluster_posts(execute, **kwargs)


def video_caps():
    """VIDEO_DAILY, or None while either of its numbers is zero or the file cannot be read, so nothing is spent and
    the step is not even timed."""
    return video_plan()[0]


def video_plan():
    """(VIDEO_DAILY, None) when both its numbers are above zero, else (None, why the step does not run): {"off": True}
    at zero, {"error": ...} when the caps file cannot be read. why goes into the understand row as its video counts,
    so a run that read no clips always says why."""
    try:
        caps = video_daily()
    except Exception as err:
        return None, {"error": f"caps unreadable: {enrich_error(err)}"}
    if caps["clips"] > 0 and caps["credits"] > 0:
        return caps, None
    return None, {"off": True}


def run_video(execute, **kwargs):
    """video.run_video, imported here so a failed import fails video reading soft, not the job."""
    from core.understand.video import run_video as read_clips

    return read_clips(execute, **kwargs)


def bigquery_execute():
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT)
    types = {"date": "DATE", "int": "INT64", "str": "STRING", "float": "FLOAT64"}

    def execute(sql, params, max_bytes=None):
        config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(name, types[type(value).__name__], value) for name, value in params.items()])
        if max_bytes:
            config.maximum_bytes_billed = max_bytes
        job = client.query(sql, job_config=config, job_retry=None)
        rows = [dict(row.items()) for row in job.result()]
        return {"rows": rows, "num_dml_affected_rows": job.num_dml_affected_rows}

    return execute


def embed_not_run(counts) -> str | None:
    """Why the day has no embeddings when run_embed sent no window and said so (retry_capped, or spend_unknown),
    else None. The run still ends ok and detect still starts, so this is the only trace of it in the row."""
    if counts.get("embedded"):
        return None
    if counts.get("retry_capped"):
        return f"NoWindowSent: retry_capped, {counts.get('uncorrected_usd')} USD of embed ceilings no correction followed"
    if counts.get("spend_unknown"):
        return f"NoWindowSent: spend_unknown, {counts.get('spend_error') or 'spend could not be read or booked'}"
    return None


def degraded_reasons(counts) -> dict:
    """The steps a run failed soft on, each with its error text, read back from the run's own counts: "embed" when it
    has embed_error, "enrich" when it has enrich_error, then "cluster:<market>" for each market whose counts carry an
    error. The names are the ones core/api/store.py degraded_writes uses for enrich and cluster."""
    out = {}
    if counts.get("embed_error"):
        out["embed"] = counts["embed_error"]
    if counts.get("enrich_error"):
        out["enrich"] = counts["enrich_error"]
    cluster = counts.get("cluster") if isinstance(counts.get("cluster"), dict) else {}
    for market in CLUSTER_MARKETS:
        if isinstance(cluster.get(market), dict) and cluster[market].get("error"):
            out[f"cluster:{market}"] = cluster[market]["error"]
    return out


def degraded_steps(counts) -> list:
    return list(degraded_reasons(counts))


def clusterer_unavailable(err) -> bool:
    """True when the clustering stack cannot run at all, not when one market's write or fit went wrong: a module that
    will not import (ImportError, which holds ModuleNotFoundError), or numba unable to cache UMAP's compiled
    functions, a RuntimeError whose message says so."""
    text = str(err)
    return isinstance(err, ImportError) or "cannot cache function" in text or "no locator available" in text


def enrich_error(err) -> str:
    """The exception class and its first line, every URL removed, for runs.counts (enrichment's and clustering's)."""
    line = (str(err).splitlines() or [""])[0]
    line = re.sub(r"\w+://\S*?(?=[.,:;)]*(\s|$))", "<url>", line)
    return f"{type(err).__name__}: {line}"[:300]


def main(execute=None):
    if "--model-cap-readback" in sys.argv[1:]:
        from core.config.caps import model_daily_usd

        instant = now()
        today = instant.astimezone(SAST).date()
        print(f"model_daily_usd={model_daily_usd(now=instant):.2f} sast_date={today.isoformat()}")
        return 0
    try:
        chain = importlib.import_module("core.collect.chain")
    except ImportError:
        sys.exit(MISSING_CHAIN)
    try:
        run = chain.begin("understand")
    except chain.AlreadyDone as done:
        print(f"understand: {done}")
        try:
            started = chain.restart_next("understand", done.run.run_date)
        except Exception as err:
            print(f"understand for {done.run.run_date} is ok but detect did not start: {err}", file=sys.stderr)
            return 1
        if started:
            print(f"understand for {done.run.run_date} was ok and detect had not started; started {started}")
        return 0
    except chain.UpstreamNotReady as blocked:
        print(f"understand blocked: {blocked}", file=sys.stderr)
        return 1
    days = int(os.environ.get("EMBED_DAYS", "1"))
    started = now()
    today = started.astimezone(SAST).date()  # every spend read and booking below stays on this day
    counts, booked, step_seconds = {}, 0.0, {}
    print(phase_line(run.run_id, "embed", "start", 0.0), flush=True)
    try:
        execute = execute or bigquery_execute()

        def book(usd, what="embed"):
            return book_spend(execute, run_id=run.run_id, run_date=today, usd=usd, what=what, credit=True)

        counts = run_embed(execute, run_date=run.run_date, days=days, book=book,
                           spent_today=lambda: spend_today(execute, started),
                           uncorrected=lambda: uncorrected_today(execute, today), spend_day=today, clock=now)
        booked = counts.pop("booked_usd", 0.0)
        booked += book(counts.get("model_usd", 0) - booked)  # anything run_embed left unbooked; none when it booked all
        if no_window := embed_not_run(counts):
            counts["embed_error"] = no_window
    except Exception as err:
        elapsed = round(max(0.0, (now() - started).total_seconds()), 3)
        print(phase_line(run.run_id, "embed", "end", elapsed), flush=True)
        step_seconds["embed"] = elapsed
        counts = dict(getattr(err, "embed_counts", counts))
        booked = counts.pop("booked_usd", booked)
        if "model_usd" in counts:
            # run_embed books each window's ceiling before it is sent, so no embed spend is ever unbooked here and
            # none lands on RUN_DATE. What can be left is a credit: a correction that failed to book, or a window
            # that raised with its true spend unknown. It is dropped, so today stays at or above the true spend, by at
            # most one window's ceiling (USD 15.36).
            counts.update(model_usd=max(0.0, round(counts["model_usd"] - booked, 6)), booked_model_usd=round(booked, 6))
        counts["step_seconds"] = dict(step_seconds)
        chain.finish(run, "failed", counts, error=str(err))
        raise
    else:
        elapsed = round(max(0.0, (now() - started).total_seconds()), 3)
        print(phase_line(run.run_id, "embed", "end", elapsed), flush=True)
        step_seconds["embed"] = elapsed
    error, enriched = None, {}
    day_changed = bool(counts.get("day_changed"))
    backfill = str(run.run_date) != today.isoformat()
    if backfill:
        counts["enrich_skipped_backfill"] = True
        step_seconds["enrich"] = 0.0
    else:
        enrich_started = now()
        print(phase_line(run.run_id, "enrich", "start", 0.0), flush=True)
        try:
            enriched = run_enrich(execute, run_date=run.run_date, run_id=run.run_id, day=today, clock=now)
        except Exception as err:
            enriched, error = getattr(err, "enrich_counts", {}), enrich_error(err)
        finally:
            elapsed = round(max(0.0, (now() - enrich_started).total_seconds()), 3)
            print(phase_line(run.run_id, "enrich", "end", elapsed), flush=True)
            step_seconds["enrich"] = elapsed
    day_changed = day_changed or bool(enriched.get("day_changed"))
    # Enrichment's counts join under enrich_ names (enriched keeps its own). booked_model_usd is the spend both steps
    # already booked in runs; model_usd is only what is left (a same-day batch credit, or a booking that failed).
    spent = counts.get("model_usd", 0) + enriched.get("model_usd", 0)
    booked += enriched.get("booked_usd", 0)
    counts = {**counts, **{k if k == "enriched" else f"enrich_{k}": v for k, v in enriched.items()
                           if k not in ("model_usd", "booked_usd")},
              "model_usd": round(spent - booked, 6), "booked_model_usd": round(booked, 6)}
    if error:
        counts["enrich_error"] = error
    counts["cluster"] = {"skipped": "backfill"} if backfill else {}
    if backfill:
        step_seconds["clustering"] = 0.0
    else:
        cluster_started = now()
        print(phase_line(run.run_id, "clustering", "start", 0.0), flush=True)
        unavailable = None
        for market in CLUSTER_MARKETS:
            try:
                clustered = run_cluster(execute, run_date=run.run_date, market=market, run_id=run.run_id, day=today,
                                        clock=now)
            except Exception as err:
                clustered = {"error": enrich_error(err), **{k: getattr(err, k) for k in (
                    "failed_batch", "unwritten_cluster_ids", "model_usd", "booked_usd") if hasattr(err, k)}}
                if unavailable is None and clusterer_unavailable(err):
                    unavailable = clustered["error"]
            day_changed = day_changed or clustered.get("net_error") == "day_changed"
            spent += clustered.pop("model_usd", 0)
            booked += clustered.pop("booked_usd", 0)
            counts["cluster"][market] = {k: v for k, v in clustered.items() if k != "market"}
            # One market's posts, matrix and fitted model are garbage once its counts are in; collect them before
            # the next market fits, so the peak is one market's, not two.
            gc.collect()
        elapsed = round(max(0.0, (now() - cluster_started).total_seconds()), 3)
        print(phase_line(run.run_id, "clustering", "end", elapsed), flush=True)
        step_seconds["clustering"] = elapsed
        if unavailable:
            counts.update(model_usd=round(spent - booked, 6), booked_model_usd=round(booked, 6))
            counts["step_seconds"] = dict(step_seconds)
            chain.finish(run, "failed", counts, error=f"clustering unavailable: {unavailable}")
            print(f"understand failed: clustering unavailable: {unavailable}", file=sys.stderr)
            return 1
    caps, why = video_plan()
    if backfill:
        counts["video"] = {"skipped": "backfill"}
    elif day_changed:
        counts["video"] = {"skipped": "day_changed"}
    elif caps is None:
        counts["video"] = why
    else:
        video_started = now()
        print(phase_line(run.run_id, "video", "start", 0.0), flush=True)
        try:
            video = run_video(execute, run_date=run.run_date, run_id=run.run_id, day=today, clock=now, caps=caps)
        except Exception as err:
            video = {**getattr(err, "video_counts", {}), "error": enrich_error(err)}
        if not video.get("off"):
            step_seconds["video"] = round(max(0.0, (now() - video_started).total_seconds()), 3)
            print(phase_line(run.run_id, "video", "end", step_seconds["video"]), flush=True)
        spent += video.pop("model_usd", 0)
        booked += video.pop("booked_usd", 0)
        # A day change stops only the rest of the video step (its counts say day_changed); clips are not part of
        # what detect needs, so the run still ends ok and detect starts.
        counts["video"] = video
    counts.update(model_usd=round(spent - booked, 6), booked_model_usd=round(booked, 6))
    counts["step_seconds"] = dict(step_seconds)
    if reasons := degraded_reasons(counts):
        print("understand degraded: " + "; ".join(f"{step} ({why})" for step, why in reasons.items()), file=sys.stderr)
    chain.finish(run, "failed" if day_changed else "ok", counts, error="day_changed" if day_changed else None)
    if day_changed:
        return 1
    chain.start_next("understand", run.run_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
