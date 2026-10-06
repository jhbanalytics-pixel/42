"""7-day boosted-tree forecast outlook per (market, topic_group).

Reframes the 7-day forecast as supervised learning (the ARIMA_PLUS model it
replaces lost to a naive "today holds" baseline on every backtest week, so it
was turned off 7 Jun 2026). Every (market, topic, day t) in the deep
``trend_scores`` history becomes one leak-safe labelled row, features as-of t,
label = the realised trend_score at t+7. A BigQuery ML BOOSTED_TREE_REGRESSOR
on those features (score + lags 1/3/7, 3-day + 7-day momentum, item_count, the
component scores, day-of-week, market, topic) predicts t+7 directly and beats
persistence on MAE across 3/3 walk-forward weeks. Each series reduces to a
compact heating / steady / cooling outlook for the PULSE brief.

Everything here is non-fatal and gated by ``FORECAST_ENABLED`` upstream: a
BigQuery, model, or data failure logs and yields an empty dict, so the daily
pipeline and the email are never blocked by the forecast. Output is purely
additive (a new ``forecast_outlook`` field on each brief); it never touches
``trend_score``.

Cost: the feature table and the boosted-tree both sit inside the BigQuery ML
create-model free tier (a ~1k-row train). The ``forecast_supervised`` table, the
``forecast_btree`` model, and the ``score_forecast_7d`` table are isolated
objects the cron refreshes only when the flag is on. Stays DARK until the
accuracy-watchdog rolling backtest confirms the live edge over persistence.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from google.cloud import bigquery

from src.utils.bigquery import get_client, get_dataset

logger = logging.getLogger(__name__)

# Day-7 forecast minus today's actual trend_score. Beyond +/- this band the
# series reads as heating or cooling; inside it, steady. trend_score sits in
# [0, 1] (live values around 0.18 to 0.54), so 0.04 is a meaningful move without
# flapping on forecast noise.
_OUTLOOK_BAND = 0.04

_SQL_DIR = Path(__file__).resolve().parents[2] / "infra" / "bigquery_queries"
_SQL_FILES = ("forecast_train.sql", "forecast_select.sql", "forecast_outlook.sql")


def classify_outlook(
    latest_actual: float, day7_forecast: float, band: float = _OUTLOOK_BAND
) -> str:
    """Reduce a series to heating / steady / cooling.

    Compares the 7-day-ahead point forecast to today's actual trend_score. Pure
    function (no IO) so it is unit-testable without BigQuery. The band is
    inclusive of steady: a move of exactly ``band`` stays steady. The delta is
    rounded to 6 places first so a binary-float tie at the band edge (e.g.
    0.34 - 0.30) does not flip the verdict.
    """
    delta = round(float(day7_forecast) - float(latest_actual), 6)
    if delta > band:
        return "heating"
    if delta < -band:
        return "cooling"
    return "steady"


def _read_sql(name: str, project: str, dataset: str) -> str:
    sql = (_SQL_DIR / name).read_text(encoding="utf-8")
    return sql.replace("{project}", project).replace("{dataset}", dataset)


def _archive_predictions(client, project: str, dataset: str, rows: list[dict]) -> None:
    """Append today's day-7 forecasts to predictions_archive (append-only).

    Streams the rows in with insert_rows_json so it is a pure INSERT, never a
    replace, and the trailing-28-day backtest window in the accuracy watchdog
    accumulates history across runs. A same-day re-run is skipped: the table is
    probed for any row with today's run_date first, so a re-run does not write
    duplicate (run_date, market, query_group) rows that would skew the
    watchdog's MAE gate. Non-fatal on its own: a missing table (the DDL not yet
    applied while the forecast is dropped), a streaming-insert error, or partial
    row errors all log and return so the outlook chip and the daily email are
    never blocked by the archive write.
    """
    if not rows:
        return
    table_ref = f"{project}.{dataset}.predictions_archive"
    run_date = rows[0].get("run_date")
    try:
        if run_date is not None and _archive_has_run_date(client, table_ref, run_date):
            logger.info(
                "predictions_archive already has rows for run_date=%s; skipping insert", run_date
            )
            return
    except Exception as exc:  # non-fatal: fall through to the insert attempt
        logger.warning(
            "predictions_archive dedupe probe failed (non-fatal, inserting anyway): %s", exc
        )
    try:
        errors = client.insert_rows_json(table_ref, rows)
        if errors:
            logger.error("predictions_archive insert had row errors (non-fatal): %s", errors)
        else:
            logger.info("Archived %d forecast rows to predictions_archive", len(rows))
    except Exception as exc:  # non-fatal by contract
        logger.error("predictions_archive insert failed (non-fatal): %s", exc, exc_info=True)


def _archive_has_run_date(client, table_ref: str, run_date: str) -> bool:
    """True when predictions_archive already holds any row for ``run_date``."""
    sql = f"SELECT 1 FROM `{table_ref}` WHERE run_date = @run_date LIMIT 1"
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("run_date", "DATE", run_date)],
        ),
    )
    return any(True for _ in job.result())


def compute_forecast_outlook(
    trend_date: datetime.date,
    *,
    band: float = _OUTLOOK_BAND,
) -> dict[tuple[str, str], dict]:
    """Train, forecast, and classify. Returns ``{(market, query_group): {...}}``.

    Keys are the series that clear the boosted-tree feature gate in
    forecast_select.sql: lags 1, 3, and 7 must all be present, so a series under
    8 days old is skipped. Those short or new series are absent here and the
    renderer simply shows no outlook chip for them. Non-fatal: any failure logs
    and returns ``{}`` so the caller can carry on.
    """
    try:
        client = get_client()
        dataset = get_dataset()
        project = client.project
        date_param = bigquery.ScalarQueryParameter("trend_date", "DATE", trend_date)

        def _run(name: str):
            sql = _read_sql(name, project, dataset)
            job = client.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[date_param],
                    # Bound the wait so a hung BQ job (slot starvation, an internal
                    # retry loop) cannot block the cron past the email send. A
                    # ~1k-row train is seconds; 3 min is generous. On timeout the
                    # job is cancelled server-side and result() raises, which the
                    # outer except turns into the {} no-chip path. Without this,
                    # result() waits indefinitely (the try/except catches an error,
                    # not a hang).
                    job_timeout_ms=180000,
                ),
            )
            return job.result()

        # 1. build the leak-safe feature table + (re)train the boosted-tree.
        _run("forecast_train.sql")
        # 2. predict t+7 per series into score_forecast_7d, clamped to [0, 1].
        _run("forecast_select.sql")
        # 3. join the day-7 forecast against today's actual trend_score.
        rows = _run("forecast_outlook.sql")

        outlook: dict[tuple[str, str], dict] = {}
        archive_rows: list[dict] = []
        for r in rows:
            market = str(r["market"])
            topic = str(r["query_group"])
            latest = float(r["latest_actual"] or 0.0)
            day7 = float(r["day7_forecast"] or 0.0)
            outlook[(market, topic)] = {
                "outlook": classify_outlook(latest, day7, band),
                "day7_forecast": round(day7, 4),
                "latest_actual": round(latest, 4),
                "delta": round(day7 - latest, 4),
            }
            archive_rows.append(
                {
                    "run_date": trend_date.isoformat(),
                    "forecast_day": (trend_date + datetime.timedelta(days=7)).isoformat(),
                    "market": market,
                    "query_group": topic,
                    "forecast_score": day7,
                    "latest_actual": latest,
                }
            )

        _archive_predictions(client, project, dataset, archive_rows)
        logger.info("Forecast outlook computed for %d series", len(outlook))
        return outlook
    except Exception as exc:  # non-fatal by contract
        logger.error("Forecast outlook failed (non-fatal): %s", exc, exc_info=True)
        return {}


def tag_briefs_with_outlook(
    briefs_by_topic: dict[tuple[str, str], dict],
    outlook: dict[tuple[str, str], dict],
) -> int:
    """Tag each brief in place with its forecast outlook; return the count tagged.

    Both sides key on (market, query_group). A brief whose key is absent from
    ``outlook`` (a short/new series, or any producer/consumer key-shape drift) is
    left untouched, so a key mismatch degrades to no-chip rather than an error.
    """
    tagged = 0
    for key, brief in briefs_by_topic.items():
        entry = outlook.get(key)
        if entry and entry.get("outlook"):
            brief["forecast_outlook"] = entry["outlook"]
            tagged += 1
    return tagged
