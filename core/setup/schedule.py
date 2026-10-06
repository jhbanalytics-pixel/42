"""Create the Cloud Scheduler jobs: the morning chain, reconcile, GDELT seeds and daily aggregate, the watchdog, drift, learn,
scheduled asks, pulse, Breaking, digest, the calendar refresh.

f42-collect-0200 starts collect at 02:00 SAST. f42-brief-0615 starts brief at 06:15 SAST, the publish
deadline: if the chain already published, brief exits as AlreadyDone; if detect failed or is late,
brief publishes whatever passed with a data-issue banner (core/collect/chain.py). f42-reconcile-2330
starts the credit reconciliation (core/collect/reconcile.py) at 23:30 SAST; it is not part of the chain.
f42-gdelt-0130 starts the GDELT seed generator (core/collect/gdelt.py, task 2.5) at 01:30 SAST, so its
seeds are in seed_queue before collect. f42-gdelt-daily-0330 starts the GDELT daily aggregate
(core/collect/gdelt_daily.py --apply --backfill, task 2.5) at 03:30 SAST, after 03:00 SAST (01:00 UTC) when the
UTC news day it reads has settled; it is not part of the chain. It was created by hand on 4 October 2026 and is
declared here so the plan matches staging. f42-watchdog-quarter and f42-watchdog-hourly start the watchdog
(core/setup/watchdog.py, task 2.10) every quarter hour from 02:00 to 07:45 SAST and hourly otherwise.
f42-drift-mon-0700 starts the weekly seed drift report (core/collect/drift.py, task 2.4) on Monday at
07:00 SAST; after the report the same job refreshes the calendar analogues (task 3.6), failing soft. f42-learn-mon-0730 starts the weekly learn job (core/detect/learn.py, task 2.7), which appends
last week's engine scorecard, on Monday at 07:30 SAST, after the brief; it is not part of the chain.
f42-scheduled-asks-0700 starts the scheduled questions (core/api/scheduled.py, contract.md 14.2)
every day at 07:00 SAST, after the 06:15 brief deadline; it is not part of the chain, and it is planned only while
SCHEDULED_ASKS_READY is True. f42-pulse-3h starts the intraday pulse (core/collect/pulse_job.py, FEATURES.md row
30a) every 3 hours from 09:00 to 21:00 SAST; it is not part of the chain, and it stays off while PULSE_READY is False.
f42-breaking-hourly starts the hourly Breaking rule (core/detect/breaking.py, L2 Needs 34) at 5 past every hour; it
is not part of the chain either, and it stays off with the pulse while PULSE_READY is False.
f42-digest-0645 starts the daily alert email digest (core/api/digest.py --send, BUILD.md 2.9) at 06:45 SAST, after
the 06:15 brief deadline and the watchdog's 06:30 brief_late check; it is not part of the chain, sends at most once a
day, and stays off while DIGEST_READY is False.
f42-calendar-mon-0630 starts the weekly calendar refresh (core/collect/calendar.py --apply, task 1.4) on Monday at
06:30 SAST, before f42-drift; it is not part of the chain. Its MERGE inserts only the moments not yet in the calendar
table, for the 90 days from that Monday (or to known_until in moments.yaml, if later), so the window moves on each
week; a row already loaded is never changed.

    py -3.13 core/setup/schedule.py            dry run: prints the plan and whether each job exists, changes nothing
    py -3.13 core/setup/schedule.py --apply    creates each job only if it is missing

It runs as whatever gcloud is configured to act as (f42-builder, which holds
cloudscheduler.admin and iam.serviceAccountUser on f42-scheduler). It only creates: an existing job is
reported and left exactly as it is, and nothing here edits, stops or removes a Scheduler job. Between
the two, each job's last step starts the next one, see core/collect/chain.py.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.collect import chain  # noqa: E402
from core.setup import watchdog  # noqa: E402

# f42-scheduled-asks and its Scheduler entry, here and in deploy_jobs.py. bootstrap.py grants f42-agent secretAccessor
# on the SocialCrawl key, the job's only secret, and requirements-jobs.txt carries fastapi, starlette and sqlglot.
# Switched on 3 October 2026 so schedules get their daily run. Each run is bounded by SCHEDULED_DAILY, ASK_DAILY and
# MODEL_DAILY_USD in core/config/caps.yaml (core/api/scheduled.py checks all three before each ask). The
# job mounts no channel webhook: the team channel post (contract.md 14.3) is not wired, so nothing is posted outside 42.
# The jobs image must be built from a commit with these packages.
SCHEDULED_ASKS_READY = True
SCHEDULED_ASKS_JOB = "f42-scheduled-asks"
SCHEDULED_ASKS_OFF = ("off until Albert runs bootstrap.py --apply (SocialCrawl key access for f42-agent) and says go; "
                      "the jobs image then needs a build with fastapi, starlette and sqlglot "
                      "(SCHEDULED_ASKS_READY in core/setup/schedule.py)")
# f42-pulse and its Scheduler entry stay off, here and in deploy_jobs.py, until Albert says go. The pulse share
# (PULSE_DAILY 60) and the item_hourly table exist; the job also needs an image built from a commit that holds
# core/collect/pulse_job.py. Then set True.
PULSE_READY = False
PULSE_JOB = "f42-pulse"
# The hourly Breaking rule judges what the pulse adds, so it is switched on and off with it.
BREAKING_JOB = "f42-breaking"
PULSE_OFF = "off until Albert says go (PULSE_READY in core/setup/schedule.py)"
# f42-digest and its Scheduler entry stay off, here and in deploy_jobs.py, so a full jobs deploy, which the chain jobs
# ride, never fails on a missing secret: the f42-digest deploy mounts GMAIL_APP_PASSWORD. Sending also needs
# DIGEST_RECIPIENTS and DIGEST_SEND_ENABLED=true set on the deployed job; until then each run prints why it sent
# nothing and exits 0.
# True once GMAIL_APP_PASSWORD exists and bootstrap has granted f42-agent; the jobs image already covers every digest import.
# Albert created GMAIL_APP_PASSWORD in ogilvy-trends-v2 on 2 October 2026; bootstrap --apply grants f42-agent before this deploys.
DIGEST_READY = True
DIGEST_JOB = "f42-digest"
DIGEST_OFF = ("off until the GMAIL_APP_PASSWORD secret is created with f42-agent access from bootstrap.py "
              "(DIGEST_READY in core/setup/schedule.py)")

# The weekly calendar refresh: core/collect/calendar.py --apply on its own job, always on. It only inserts missing rows.
CALENDAR_JOB = "f42-calendar"

PROJECT = chain.PROJECT
LOCATION = chain.REGION
TIME_ZONE = "Africa/Johannesburg"
# Scheduler job name, cron in TIME_ZONE, the Cloud Run job it starts, what that job is.
SCHEDULES = (
    ("f42-collect-0200", "0 2 * * *", chain.JOBS["collect"], "42 morning chain: starts collect"),
    ("f42-brief-0615", "15 6 * * *", chain.JOBS["brief"], "42 morning chain: starts brief"),
    ("f42-reconcile-2330", "30 23 * * *", "f42-reconcile", "42 nightly credit reconciliation"),
    ("f42-gdelt-0130", "30 1 * * *", "f42-gdelt", "42 GDELT seeds before collect"),
    ("f42-gdelt-daily-0330", "30 3 * * *", "f42-gdelt-daily", "42 GDELT daily aggregate after the news day settles"),
    *((name, cron, watchdog.JOB["name"], "42 watchdog") for name, cron in watchdog.SCHEDULES),
    ("f42-drift-mon-0700", "0 7 * * 1", "f42-drift", "42 weekly seed drift report"),
    ("f42-learn-mon-0730", "30 7 * * 1", "f42-learn", "42 weekly learn job: engine scorecard"),
    ("f42-scheduled-asks-0700", "0 7 * * *", SCHEDULED_ASKS_JOB, "42 daily scheduled questions"),
    ("f42-pulse-3h", "0 9-21/3 * * *", PULSE_JOB, "42 intraday pulse"),
    ("f42-breaking-hourly", "5 * * * *", BREAKING_JOB, "42 hourly Breaking rule"),
    ("f42-digest-0645", "45 6 * * *", DIGEST_JOB, "42 daily alert email digest"),
    ("f42-calendar-mon-0630", "30 6 * * 1", CALENDAR_JOB, "42 weekly calendar moments refresh"),
)
SERVICE_ACCOUNT = f"f42-scheduler@{PROJECT}.iam.gserviceaccount.com"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
# jobs.run returns a long-running operation at once, so the attempt only has to wait for that reply.
ATTEMPT_DEADLINE = "180s"
MAX_RETRIES = 1


class GcloudError(Exception):
    pass


class Gcloud:
    def __init__(self, path=None):
        self.path = path or shutil.which("gcloud") or "gcloud"

    def run(self, args):
        # stdin closed: a gcloud prompt fails at once instead of waiting silently for an answer.
        proc = subprocess.run([self.path, *args], stdin=subprocess.DEVNULL, capture_output=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise GcloudError(f"gcloud {' '.join(args[:4])} failed: {proc.stderr.strip()}")
        return proc.stdout


def where():
    return [f"--project={PROJECT}", f"--location={LOCATION}"]


def list_argv():
    return ["scheduler", "jobs", "list", *where(), "--format=value(name)"]


def target(job):
    return f"{chain.API}/projects/{PROJECT}/locations/{LOCATION}/jobs/{job}:run"


def create_argv(name, schedule, job, what):
    return ["scheduler", "jobs", "create", "http", name, *where(),
            f"--schedule={schedule}", f"--time-zone={TIME_ZONE}",
            f"--uri={target(job)}", "--http-method=POST",
            f"--oauth-service-account-email={SERVICE_ACCOUNT}", f"--oauth-token-scope={SCOPE}",
            f"--attempt-deadline={ATTEMPT_DEADLINE}", f"--max-retry-attempts={MAX_RETRIES}",
            f"--description={what} at {schedule} {TIME_ZONE}"]


def existing(gcloud):
    """Short names of the Scheduler jobs already in the project and location."""
    return {line.strip().rsplit("/", 1)[-1] for line in gcloud.run(list_argv()).splitlines() if line.strip()}


def main(argv=None, gcloud=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="create each job that is missing")
    args = parser.parse_args(argv)
    gcloud = gcloud or Gcloud()

    present = existing(gcloud)
    for name, schedule, job, what in SCHEDULES:
        if job == SCHEDULED_ASKS_JOB and not SCHEDULED_ASKS_READY:
            print(f"Scheduler job {name}: skipped, {SCHEDULED_ASKS_OFF}")
            continue
        if job in (PULSE_JOB, BREAKING_JOB) and not PULSE_READY:
            print(f"Scheduler job {name}: skipped, {PULSE_OFF}")
            continue
        if job == DIGEST_JOB and not DIGEST_READY:
            print(f"Scheduler job {name}: skipped, {DIGEST_OFF}")
            continue
        print(f"Scheduler job {name} in {PROJECT}/{LOCATION}")
        print(f"  schedule   {schedule} ({TIME_ZONE})")
        print(f"  target     POST {target(job)}")
        print(f"  token      OAuth as {SERVICE_ACCOUNT}")
        print(f"  attempts   deadline {ATTEMPT_DEADLINE}, max retries {MAX_RETRIES}")
        if name in present:
            print(f"  {name} already exists; left unchanged.")
        elif not args.apply:
            print(f"  {name} is missing. Dry run: nothing created. Run again with --apply to create it.")
        else:
            gcloud.run(create_argv(name, schedule, job, what))
            print(f"  {name} created.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
