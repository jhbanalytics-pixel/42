"""Cloud Function Gen2 entry point: alert if the daily pipeline did not run.

Cloud Scheduler invokes this once per day at 06:30 UTC (08:30 SAST), after
both pipeline triggers have had their window: the 00:30 UTC Cloud Run primary
and the GitHub Actions fallback (02:00 to 03:30 UTC). The function queries
BigQuery for today's pipeline_runs rows and alerts on either failure mode:
no success rows (the run did not happen at all), or success rows with no
healthy email-outcome marker (the run completed but the digest never went
out). Either would otherwise pass silently.

This is the last line of defence behind the two run triggers. It lives on
Cloud Scheduler, not GitHub Actions, on purpose: a GitHub-hosted watcher
would share the same failure mode as the GitHub schedule it is meant to
catch, and would be dropped on exactly the days it is needed.

Auth + config:
- Runs as the function's runtime service account (BigQuery read via ADC).
- GMAIL_USER + GMAIL_APP_PASSWORD are mounted from Secret Manager at deploy.
- GCP_PROJECT, BQ_DATASET, ALERT_EMAIL come from --set-env-vars.
- POST ?dry=1 to get the decision without sending mail (used to test).

Deployed by scripts/deploy_watchdog.sh.
"""

from __future__ import annotations

import datetime
import logging
import os
import smtplib
from email.mime.text import MIMEText

import functions_framework
from google.cloud import bigquery

logging.basicConfig(level=logging.INFO)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# Markets the daily run must complete. A market with no success row for the day
# (it failed, ran partial, or never ran) is an outage the watchdog must catch,
# even when the other markets succeeded and the digest went out.
REQUIRED_MARKETS = ("za", "ng", "ke")


def _count_success_rows(project: str, dataset: str) -> tuple[int, int]:
    """Return (success_row_count, distinct_market_count) for today (UTC)."""
    client = bigquery.Client(project=project)
    sql = f"""
    SELECT COUNT(*) AS n, COUNT(DISTINCT market) AS markets
    FROM `{project}.{dataset}.pipeline_runs`
    WHERE DATE(started_at) = CURRENT_DATE()
      AND status = 'success'
    """
    row = next(iter(client.query(sql).result()))
    return int(row.n or 0), int(row.markets or 0)


def _markets_missing_success(project: str, dataset: str) -> list[str]:
    """Required markets with no success row today (UTC), in REQUIRED_MARKETS order.

    A market that only has a 'partial' or 'failed' row, or no row at all, is
    missing here. This is what the success-row count and the email marker are both
    blind to: ZA can fail two days running while NG and KE succeed and the digest
    sends, and the count-only check still reads healthy.
    """
    client = bigquery.Client(project=project)
    sql = f"""
    SELECT DISTINCT market
    FROM `{project}.{dataset}.pipeline_runs`
    WHERE DATE(started_at) = CURRENT_DATE()
      AND status = 'success'
      AND market IS NOT NULL
    """
    succeeded = {str(r.market).lower() for r in client.query(sql).result()}
    return [m for m in REQUIRED_MARKETS if m not in succeeded]


def _count_email_failures_today(project: str, dataset: str) -> int:
    """Count today's send_failed or render_error email markers."""
    client = bigquery.Client(project=project)
    sql = f"""
    SELECT COUNT(*) AS n
    FROM `{project}.{dataset}.pipeline_runs`
    WHERE DATE(started_at) = CURRENT_DATE()
      AND status = 'email_audit'
      AND email_status IN ('send_failed', 'render_error')
    """
    row = next(iter(client.query(sql).result()))
    return int(row.n or 0)


def _count_healthy_email_today(project: str, dataset: str) -> int:
    """Count today's healthy email-outcome markers in pipeline_runs.

    A healthy outcome is the digest sent, or a legitimately quiet / already-sent
    skip (the same set run_rss_now treats as healthy). skipped_nothing_notable
    is only healthy when no send_failed/render_error marker exists on the same
    UTC day, so a fallback quiet skip cannot mask a primary send failure.
    """
    failures = _count_email_failures_today(project, dataset)
    allowed = ("sent", "dry_run", "skipped_already_sent")
    if failures <= 0:
        allowed = (*allowed, "skipped_nothing_notable")
    in_list = ", ".join(f"'{s}'" for s in allowed)
    client = bigquery.Client(project=project)
    sql = f"""
    SELECT COUNT(*) AS n
    FROM `{project}.{dataset}.pipeline_runs`
    WHERE DATE(started_at) = CURRENT_DATE()
      AND status = 'email_audit'
      AND email_status IN ({in_list})
    """
    row = next(iter(client.query(sql).result()))
    return int(row.n or 0)


def _count_recent_errors(project: str, dataset: str) -> tuple[int, str]:
    """Return (count, latest_message) of system_events ERROR rows in the last 24h.

    Backstop visibility for the system-observability feature: the real-time
    alert policy fires on the [TEV2_FATAL] marker, but this surfaces the same
    errors (including Listening Post chat errors the success/email check cannot
    see) in the daily watchdog return. Query failures reach the caller's
    existing unavailable-check response instead of claiming zero errors.
    """
    try:
        client = bigquery.Client(project=project)
        sql = f"""
        SELECT COUNT(*) AS n,
               ARRAY_AGG(
                 CONCAT(source, ': ', COALESCE(message, event_type, 'error'))
                 ORDER BY event_time DESC LIMIT 1
               )[SAFE_OFFSET(0)] AS latest
        FROM `{project}.{dataset}.system_events`
        WHERE event_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
          AND severity = 'ERROR'
        """
        row = next(iter(client.query(sql).result()))
        return int(row.n or 0), str(row.latest or "")
    except Exception as exc:
        logging.warning("watchdog recent_errors_query failed: error_type=%s", type(exc).__name__)
        raise


def _decide_alert(
    success_rows: int, email_ok: int, missing_markets: tuple[str, ...] | list[str] = ()
) -> str | None:
    """Classify today's health, or None when it is fine.

    "no_run"            -> no success rows: the pipeline did not run at all.
    "market_incomplete" -> some success rows exist, but a required market has no
                           success row (it failed, ran partial, or never ran).
    "no_email"          -> all required markets succeeded but no healthy email
                           marker: it ran but the digest silently never went out.
    None                -> every required market succeeded and the digest is
                           healthy, or the run correctly stayed quiet.
    """
    if success_rows <= 0:
        return "no_run"
    if missing_markets:
        return "market_incomplete"
    if email_ok <= 0:
        return "no_email"
    return None


def _send_alert(user: str, password: str, recipient: str, subject: str, body: str) -> None:
    """Send the plain-text alert via Gmail SMTP (mirrors the digest sender)."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [recipient], msg.as_string())


@functions_framework.http
def check(request):
    """Alert when today has no successful pipeline run.

    Returns a small JSON status. With ?dry=1 it reports the decision but
    never sends mail, so the function can be tested without spamming.
    """
    project = (os.environ.get("GCP_PROJECT") or "").strip()
    dataset = (os.environ.get("BQ_DATASET") or "").strip()
    recipient = (os.environ.get("ALERT_EMAIL") or "").strip()
    user = (os.environ.get("GMAIL_USER") or "").strip()
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").strip()
    dry = bool(request.args.get("dry"))
    today = datetime.date.today().isoformat()

    if not (project and dataset):
        logging.error("watchdog misconfigured: GCP_PROJECT or BQ_DATASET missing")
        return ({"error": "missing GCP_PROJECT or BQ_DATASET"}, 500)

    probe_stage = "success_rows_query"
    try:
        n, markets = _count_success_rows(project, dataset)
        probe_stage = "market_success_query"
        missing_markets = _markets_missing_success(project, dataset) if n > 0 else []
        probe_stage = "email_health_query"
        email_ok = _count_healthy_email_today(project, dataset)
        probe_stage = "recent_errors_query"
        recent_errors, latest_error = _count_recent_errors(project, dataset)
    except Exception as exc:
        reason = f"{probe_stage}: query_failed"
        logging.error(
            "watchdog probe failed: stage=%s reason=query_failed error_type=%s",
            probe_stage,
            type(exc).__name__,
        )
        # Try to alert that the check itself could not run; better a noisy
        # false alarm than a silent blind spot.
        if not dry and user and password and recipient:
            try:
                _send_alert(
                    user,
                    password,
                    recipient,
                    f"TEV2 ALERT: watchdog could not verify today's run ({today} UTC)",
                    f"The TEV2 pipeline watchdog could not verify today's run ({today} UTC). "
                    f"Probe unavailable: {reason}. Check the watchdog query access and telemetry before judging pipeline health.",
                )
            except Exception as alert_exc:
                logging.error(
                    "watchdog check-failure alert unavailable: error_type=%s",
                    type(alert_exc).__name__,
                )
        return ({"error": reason, "date": today}, 500)

    alert_kind = _decide_alert(n, email_ok, tuple(missing_markets))
    healthy = alert_kind is None
    logging.info(
        "watchdog: date=%s success_rows=%d markets=%d missing=%s email_ok=%d healthy=%s alert=%s dry=%s",
        today,
        n,
        markets,
        ",".join(missing_markets) or "-",
        email_ok,
        healthy,
        alert_kind,
        dry,
    )

    alerted = False
    if alert_kind and not dry:
        if not (user and password and recipient):
            logging.error("watchdog: alert needed (%s) but mail not configured", alert_kind)
            return (
                {
                    "date": today,
                    "success_rows": n,
                    "email_ok": email_ok,
                    "alerted": False,
                    "error": "mail unconfigured",
                },
                500,
            )
        if alert_kind == "no_run":
            subject = f"TEV2 ALERT: daily pipeline has not run ({today} UTC)"
            body = (
                f"The TEV2 daily pipeline has NOT run for {today} UTC.\n\n"
                f"No success rows in {dataset}.pipeline_runs by 06:30 UTC, so both the "
                f"00:30 UTC Cloud Run primary and the 02:30 UTC Cloud Run fallback missed.\n\n"
                f"Recover with a manual GCP run: gcloud run jobs execute "
                f"trends-engine-pipeline --project=ogilvy-trends-v2 --region=us-central1. "
                f"The idempotency guard makes a manual run safe.\n"
            )
        elif alert_kind == "market_incomplete":
            missing = ", ".join(missing_markets)
            subject = f"TEV2 ALERT: {missing} did not complete ({today} UTC)"
            body = (
                f"The TEV2 daily pipeline ran for {today} UTC but did not complete every "
                f"market. No success row for: {missing}.\n\n"
                f"The other markets succeeded, so the success-row count and the digest can "
                f"still read healthy. Check {dataset}.pipeline_runs for the failed or partial "
                f"market row and its errors, fix the cause (often a single connector), and "
                f"confirm tomorrow's run completes all of {', '.join(REQUIRED_MARKETS)}.\n"
            )
        else:  # no_email
            subject = f"TEV2 ALERT: pipeline ran but the digest never went out ({today} UTC)"
            body = (
                f"The TEV2 daily pipeline RAN for {today} UTC ({n} success rows across "
                f"{markets} markets) but the digest never went out: no healthy email_audit "
                f"marker (sent, or a legitimate quiet / already-sent skip) exists in "
                f"{dataset}.pipeline_runs by 06:30 UTC.\n\n"
                f"Check the email send path. Recover with the Resend Email Only workflow for "
                f"today, then confirm an email_audit/sent marker lands.\n"
            )
        if recent_errors:
            body += (
                f"\nSystem events: {recent_errors} ERROR event(s) in the last 24h "
                f"(latest: {latest_error}). See {dataset}.v_system_status.\n"
            )
        _send_alert(user, password, recipient, subject, body)
        alerted = True

    return (
        {
            "date": today,
            "success_rows": n,
            "markets": markets,
            "missing_markets": missing_markets,
            "email_ok": email_ok,
            "recent_errors_24h": recent_errors,
            "latest_error": latest_error,
            "healthy": healthy,
            "alert_kind": alert_kind,
            "alerted": alerted,
            "dry": dry,
        },
        200,
    )
