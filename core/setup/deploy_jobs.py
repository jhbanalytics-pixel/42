"""Build the 42 jobs image and deploy the Cloud Run jobs to staging (tasks 0.4, 0.5, 1.3, 1.5, 1.13, 2.4, 2.5, 2.9, 2.10, 4.x, 30a).

    py -3.13 core/setup/deploy_jobs.py                     dry run: prints every command, calls no gcloud
    py -3.13 core/setup/deploy_jobs.py --build             builds the image for HEAD in Cloud Build as f42-deployer
    py -3.13 core/setup/deploy_jobs.py --apply             deploys each enabled job on the image built for HEAD
    py -3.13 core/setup/deploy_jobs.py --build --apply --only f42-probe
    py -3.13 core/setup/deploy_jobs.py --smoke             dry run of the smoke jobs, one per identity (task 0.4)
    py -3.13 core/setup/deploy_jobs.py --smoke --apply     deploys the smoke jobs instead of the chain jobs
    py -3.13 core/setup/deploy_jobs.py --run-smoke         prints the builder step and the command that runs each smoke job
    py -3.13 core/setup/deploy_jobs.py --model-provider gemini   also sets MODEL_PROVIDER on f42-brief, f42-understand and
                                                                 f42-scheduled-asks

It runs as whatever gcloud and application default credentials act as (f42-builder). The image is
built from a git archive of HEAD, so its tag, the git short sha, names exactly what is inside it; --build
and --apply refuse while core/ has uncommitted changes. --build uploads the archive with gcloud storage cp into the media bucket, then creates the build through the Cloud Build
REST API with f42-deployer as the build identity, because gcloud builds submit needs bucket permissions
f42-builder does not hold. Jobs are deployed by digest, taken from the build results or read back from
Artifact Registry. Nothing here removes a resource, and nothing here starts a job: running one is a
separate step.

understand joins the morning chain only when every chain job carries CHAIN_UNDERSTAND=1
(core/collect/chain.py). The four chain jobs get it together, and only while core/understand/job.py is
in the tree being deployed and the CHAIN_UNDERSTAND switch below is on.

--model-provider gemini (the only provider) sets MODEL_PROVIDER on f42-brief and f42-understand, the two chain jobs
that call a model, and on f42-scheduled-asks, which runs Ask (with gemini also the f42-agent service's GEMINI_MODEL); without it
their MODEL_PROVIDER is left as it is. Every env change goes through --update-env-vars, which adds
or changes only the variables it names and keeps every other one (MODEL_PROVIDER, GEMINI_*, any cap override);
switching understand off adds --remove-env-vars=CHAIN_UNDERSTAND beside it. The smoke jobs never carry
MODEL_PROVIDER, so the gemini smoke path runs only with --update-env-vars=MODEL_PROVIDER=gemini on the smoke execute.

f42-scheduled-asks (lane L4, core/api/contract.md 14.2) is outside the chain and never gets CHAIN_UNDERSTAND: its
own env points core/api at BigQuery, and the SocialCrawl key is its only secret (live research). It mounts no channel
webhook: core/api/scheduled.py posts nothing outside 42, and the team channel post (contract 14.3) is not wired, so a
schedule's deliver "channel" is stored and not acted on. --set-secrets replaces every mounted secret, so a redeploy
also drops an older CHANNEL_WEBHOOK_URL mount. It runs Ask, so --model-provider sets MODEL_PROVIDER on it in the same
--update-env-vars, and with gemini also the GEMINI_MODEL the f42-agent service carries in core/api/deploy_flags.env;
without the flag its MODEL_PROVIDER is left as it is. It is deployed only while SCHEDULED_ASKS_READY in
core/setup/schedule.py is True (one skip line in the dry run and nothing deployed on --apply while it is False); the
comment there names what has to exist first.

f42-calendar (task 1.4, core/collect/calendar.py --apply) is the weekly calendar refresh, outside the chain: the
collector identity, no key, 10 minutes and one retry, which is safe because its MERGE only inserts missing rows.

f42-gdelt-daily (task 2.5, core/collect/gdelt_daily.py --apply --backfill) is the daily GDELT aggregate, rising
entities and bridge, outside the chain: the collector identity, no key, 30 minutes and one retry, which is safe because
a day already in gdelt_daily is skipped. Its Scheduler entry (f42-gdelt-daily-0330 in core/setup/schedule.py, 03:30
SAST) has to start it after 03:00 SAST (01:00 UTC), when the UTC news day it reads has settled; before that the run
refuses the day and exits 1.

f42-pulse (FEATURES.md row 30a, core/collect/pulse_job.py) is outside the chain too: the collector identity, the
SocialCrawl key, 15 minutes and no retry, so a failed pulse waits for the next one 3 hours later instead of spending
twice. It stays off in the same way while PULSE_READY in core/setup/schedule.py is False.

f42-breaking (core/detect/breaking.py, L2 Needs 34) is the hourly Breaking rule, outside the chain: the detect
identity, no key (it reads and appends BigQuery only), 15 minutes and one retry, which is safe because an hour,
market and item an ok breaking run wrote is not written again. It stays off with the pulse while PULSE_READY is
False.

f42-digest (BUILD.md 2.9, core/api/digest.py --send) mails the daily alert digest through Gmail SMTP as f42-agent,
which already reads the API data and writes runs rows. GMAIL_APP_PASSWORD is mounted as a secret and is the only one;
GMAIL_USER is set in env. DIGEST_RECIPIENTS and DIGEST_SEND_ENABLED are never set here: they are set by hand on the
deployed job, and --update-env-vars keeps them on every redeploy, so the job deploys switched off and stays as it was
left. Its deploy mounts GMAIL_APP_PASSWORD, so it stays off, one skip line and nothing deployed, while DIGEST_READY in
core/setup/schedule.py is False, and a full deploy never fails on the secret before Albert has created it.
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.collect import chain  # noqa: E402
from core.setup import schedule, smoke, watchdog  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PROJECT = chain.PROJECT
REGION = chain.REGION
REPO = "intelligence-42"
IMAGE = f"{REGION}-docker.pkg.dev/{PROJECT}/{REPO}/jobs"
CONFIG = Path(__file__).resolve().parent / "cloudbuild.jobs.yaml"
DEPLOYER = f"projects/{PROJECT}/serviceAccounts/f42-deployer@{PROJECT}.iam.gserviceaccount.com"
BUILDS = f"https://cloudbuild.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/builds"
POLL_SECONDS = 15
FAILED = ("FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED")
ROUTES = "docs/full-42/reference/sc_routes.json"
SOURCES = ("core", ROUTES)
SECRET = "SOCIALCRAWL_OGILVY_API_KEY"
UNDERSTAND = "core.understand.job"
# Set False to keep understand out of the chain even once its module is in the tree.
CHAIN_UNDERSTAND = True
CHAIN_ENV = "CHAIN_UNDERSTAND=1"
PROVIDERS = ("gemini",)
MODEL_JOBS = (chain.JOBS["understand"], chain.JOBS["brief"])
# Jobs that run Ask. --model-provider sets MODEL_PROVIDER on them too, so they ask on the same family as the f42-agent
# service, and with gemini also the service's GEMINI_MODEL, read from core/api/deploy_flags.env.
ASK_JOBS = (schedule.SCHEDULED_ASKS_JOB,)
AGENT_FLAGS = ROOT / "core" / "api" / "deploy_flags.env"
DIGEST_SENDER = "jhb.analytics@gmail.com"


@dataclass(frozen=True)
class Job:
    name: str
    module: str | None
    identity: str
    timeout: timedelta
    secret: bool
    note: str = ""
    args: tuple = ()
    retries: int = 1
    secrets: tuple = ()  # more Secret Manager names mounted as env vars of the same name, beside the SocialCrawl key
    env: tuple = ()  # NAME=value pairs set through --update-env-vars on a job outside the chain
    memory: str | None = None  # --memory on deploy; None sends no flag, so the job keeps what it has
    cpu: str | None = None  # --cpu on deploy, the same way

    @property
    def enabled(self):
        return self.module is not None


JOBS = (
    Job("f42-probe", "core.collect.probe", "f42-collector", timedelta(minutes=30), True, retries=0),
    # GDELT seeds for collect, started by Scheduler at 01:30 SAST; it never calls SocialCrawl, so no key.
    Job("f42-gdelt", "core.collect.gdelt", "f42-collector", timedelta(minutes=15), False),
    # The GDELT daily aggregate, rising entities and the news to social bridge (task 2.5); not a chain stage, started
    # by Scheduler after 03:00 SAST, since the UTC news day it reads (yesterday) settles at 01:00 UTC. --backfill
    # writes any of the 28 days before that are missing and skips the rest, so a missed run heals the baseline on the
    # next one. Reads GKG and posts and appends gdelt_daily only, under the 5 GB dry-run cap; never calls SocialCrawl,
    # so no key. One retry is safe: a day already in gdelt_daily under the market rule is not written again.
    Job("f42-gdelt-daily", "core.collect.gdelt_daily", "f42-collector", timedelta(minutes=30), False,
        args=("--apply", "--backfill")),
    # Every morning chain job records its size, with headroom over what it uses now, so data growth or a deploy
    # never leaves one on the 512Mi default (detect died there on 5 Oct 2026, the brief published 0 cards).
    Job(chain.JOBS["collect"], "core.collect.job", "f42-collector", chain.TIMEOUTS["collect"], True,
        memory="2Gi", cpu="1"),
    # Clustering holds every post embedding of a market at once: it died on 512Mi on 1 Oct and was raised to 1024Mi
    # by hand, then 2Gi. On 5 Oct, the first day clusters were written, it finished on 2Gi; 4Gi with 2 cpu keeps
    # headroom as the window fills, and recording it here stops a deploy from dropping it.
    Job(chain.JOBS["understand"], UNDERSTAND, "f42-enricher", chain.TIMEOUTS["understand"], False,
        "lane L3, arrives with the merge of full-42-l3", retries=0, memory="4Gi", cpu="2"),
    # Detect died out of memory on 512Mi on 5 Oct 2026, the first day understand wrote clusters (two OOM events in
    # execution f42-detect-2czbj). 4Gi with 2 cpu keeps headroom as cluster items grow; recorded here so a deploy
    # sets it and never drops it.
    Job(chain.JOBS["detect"], "core.detect.job", "f42-brief", chain.TIMEOUTS["detect"], False,
        "lane L2, arrives with the evening merge", memory="4Gi", cpu="2"),
    Job(chain.JOBS["brief"], "core.brief.job", "f42-brief", chain.TIMEOUTS["brief"], True,
        "lane L2, arrives with the evening merge", memory="2Gi", cpu="1"),
    # The nightly credit reconciliation; not a chain stage, started by Scheduler at 23:30 SAST.
    Job("f42-reconcile", "core.collect.reconcile", "f42-collector", timedelta(minutes=15), True),
    # Not a chain stage either; Scheduler starts it every quarter hour from 02:00 to 07:45, hourly otherwise.
    Job(watchdog.JOB["name"], watchdog.JOB["module"], watchdog.JOB["identity"], watchdog.JOB["timeout"],
        watchdog.JOB["secret"], retries=watchdog.JOB["retries"]),
    # The weekly seed drift report (task 2.4); not a chain stage, started by Scheduler on Monday at 07:00 SAST.
    # The report only reads BigQuery; after it the job appends calendar_analogues (task 3.6, fails soft) under the
    # 5 GB dry-run cap. It never calls SocialCrawl, so no key.
    Job("f42-drift", "core.collect.drift", "f42-collector", timedelta(minutes=10), False),
    # The weekly learn job (BUILD.md 2.7), which appends the engine scorecard, the forecast scores and the weekly
    # quality score; not a chain stage, started by Scheduler on Monday at 07:30 SAST, after the brief. It runs as the detect identity, reads and writes BigQuery
    # only and calls no model and no SocialCrawl, so no key. One retry is safe: a market already written by an ok
    # learn run is skipped.
    Job("f42-learn", "core.detect.learn", "f42-brief", timedelta(minutes=30), False,
        "lane L2, arrives with the merge of full-42-l2"),
    # The weekly calendar refresh (task 1.4); not a chain stage, started by Scheduler on Monday at 06:30 SAST, before
    # f42-drift. core/collect/calendar.py --apply builds the 90 days of moments from that day out of the recorded
    # date.nager.at fixtures and core/config/moments.yaml (no live fetch), dry-runs its MERGE, then runs it. The MERGE
    # inserts only rows whose date, market and name are not in the calendar table yet; it never updates or deletes, so
    # the one retry is safe. Same identity as f42-drift, which already writes intelligence_42_core; no key. The
    # fixtures cover 2026 and 2027: from 3 October 2027 the window reaches 2028, so nager_<market>_2028.json must be in
    # core/collect/tests/fixtures by then, or the run fails (and job_failed alerts) without loading anything.
    Job(schedule.CALENDAR_JOB, "core.collect.calendar", "f42-collector", timedelta(minutes=10), False,
        args=("--apply",)),
    # The intraday pulse (FEATURES.md row 30a); not a chain stage, started by Scheduler every 3 hours from 09:00 to
    # 21:00 SAST. No retry: a failed pulse is not repeated, the next one reads the hot items again.
    Job(schedule.PULSE_JOB, "core.collect.pulse_job", "f42-collector", timedelta(minutes=15), True, retries=0),
    # The hourly Breaking rule; not a chain stage, started by Scheduler at 5 past every hour, on and off with the
    # pulse. Reads and appends BigQuery only, so no key.
    Job(schedule.BREAKING_JOB, "core.detect.breaking", "f42-brief", timedelta(minutes=15), False),
    # The daily alert email digest (BUILD.md 2.9); not a chain stage, started by Scheduler at 06:45 SAST. One retry
    # is safe: a day with an ok digest runs row, or one still sending, is not sent again.
    Job(schedule.DIGEST_JOB, "core.api.digest", "f42-agent", timedelta(minutes=10), False,
        "lane L4, arrives with core/api from full-42", args=("--send",), secrets=("GMAIL_APP_PASSWORD",),
        env=("F42_DATA=bigquery", f"F42_PROJECT={PROJECT}", f"GMAIL_USER={DIGEST_SENDER}")),
    # Scheduled questions (L4, contract.md 14.2); not a chain stage, started by Scheduler every day at 07:00 SAST.
    # Only the SocialCrawl key is mounted: no channel webhook, since the channel post (contract 14.3) is not wired.
    # --live asks for real only while SCHEDULED_DAILY is in core/config/caps.yaml, else the module runs dry. An hour
    # covers the day's share (120 credits is two T1 or twelve T0 asks, one at a time); one retry is safe because a
    # schedule with a stage ask runs row today is not asked again.
    Job(schedule.SCHEDULED_ASKS_JOB, "core.api.scheduled", "f42-agent", timedelta(minutes=60), True,
        "lane L4, arrives with core/api from full-42", args=("--live",),
        env=("F42_DATA=bigquery", f"F42_PROJECT={PROJECT}")),
)

# One smoke job per runtime identity, run once by hand, so no retry hides a failed check.
SMOKE_JOBS = tuple(
    Job(f"f42-smoke-{role}", "core.setup.smoke", f"f42-{role}", timedelta(minutes=10),
        role in ("collector", "enricher", "agent", "brief"), args=("--role", role), retries=0)
    for role in smoke.JOB_ROLES)


class GcloudError(Exception):
    pass


def _run(argv, cwd=None):
    # stdin closed: a prompt fails at once instead of waiting silently for an answer.
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                          errors="replace", cwd=cwd)
    if proc.returncode != 0:
        raise GcloudError(f"{' '.join(argv[:4])} failed: {proc.stderr.strip()}")
    return proc.stdout


class Gcloud:
    def __init__(self, path=None):
        self.path = path or shutil.which("gcloud") or "gcloud"

    def run(self, args):
        return _run([self.path, *args])


def git(args):
    return _run([shutil.which("git") or "git", *args], cwd=ROOT)


def module_file(module):
    return ROOT.joinpath(*module.split(".")).with_suffix(".py")


def module_present(module):
    return module_file(module).is_file()


def sa_email(name):
    return f"{name}@{PROJECT}.iam.gserviceaccount.com"


def source_location():
    flags = AGENT_FLAGS.read_text(encoding="utf-8")
    location = re.search(r'^BUILD_SOURCE_STAGING_DIR="gs://([^/\"]+)/([^\"]+)"$', flags, re.M)
    if location is None:
        raise ValueError("deploy_flags.env must set BUILD_SOURCE_STAGING_DIR to a gs:// bucket and prefix")
    return location.groups()


def source_object(tag):
    _, prefix = source_location()
    return f"{prefix.rstrip('/')}/jobs-{tag}.tar.gz"


def upload_argv(source, tag):
    # The object name carries the sha, so an existing one already holds this exact source: never overwrite it.
    bucket, _ = source_location()
    return ["storage", "cp", str(source), f"gs://{bucket}/{source_object(tag)}", "--no-clobber",
            f"--project={PROJECT}"]


def substitute(value, subs):
    if isinstance(value, str):
        return re.sub(r"\$\{(_[A-Z0-9_]+)\}", lambda m: subs[m.group(1)], value)
    if isinstance(value, list):
        return [substitute(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, subs) for k, v in value.items()}
    return value


def build_request(tag):
    """The Cloud Build API body: steps and images from cloudbuild.jobs.yaml with _IMAGE and _TAG filled in here."""
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    subs = {**cfg.get("substitutions", {}), "_IMAGE": IMAGE, "_TAG": tag}
    bucket, _ = source_location()
    return {
        "source": {"storageSource": {"bucket": bucket, "object": source_object(tag)}},
        "steps": substitute(cfg["steps"], subs),
        "images": substitute(cfg["images"], subs),
        "serviceAccount": DEPLOYER,
        "options": {**cfg.get("options", {}), "logging": "CLOUD_LOGGING_ONLY"},
        "timeout": cfg["timeout"],
    }


def cloud_session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def digest_argv(tag):
    # The image path carries the region; describe takes no region flag.
    return ["artifacts", "docker", "images", "describe", f"{IMAGE}:{tag}", f"--project={PROJECT}",
            "--format=value(image_summary.digest)"]


def understand_in_chain(exists=module_present):
    return CHAIN_UNDERSTAND and exists(UNDERSTAND)


def ask_service_gemini_model(path=None):
    """GEMINI_MODEL in the f42-agent service's AGENT_ENV in deploy_flags.env, or None when it sets none."""
    for line in Path(path or AGENT_FLAGS).read_text(encoding="utf-8").splitlines():
        if line.startswith("AGENT_ENV="):
            found = re.search(r"(?:^|[,\"])GEMINI_MODEL=([^,\"\s]+)", line[len("AGENT_ENV="):])
            return found.group(1) if found else None
    return None


def model_env(job, provider):
    """The model variables --model-provider sets on a job: MODEL_PROVIDER on the model and Ask jobs, plus the Ask
    service's GEMINI_MODEL on an Ask job when the provider is gemini. Empty without --model-provider."""
    if not provider:
        return []
    if job.name in MODEL_JOBS:
        return [f"MODEL_PROVIDER={provider}"]
    if job.name in ASK_JOBS:
        gemini = ask_service_gemini_model() if provider == "gemini" else None
        return [f"MODEL_PROVIDER={provider}"] + ([f"GEMINI_MODEL={gemini}"] if gemini else [])
    return []


def deploy_argv(job, image, understand=False, provider=None):
    argv = ["run", "jobs", "deploy", job.name, f"--image={image}", f"--service-account={sa_email(job.identity)}",
            "--command=python", f"--args=-m,{job.module}" + "".join("," + a for a in job.args), "--tasks=1", "--parallelism=1",
            f"--max-retries={job.retries}",
            f"--task-timeout={int(job.timeout.total_seconds())}s", f"--project={PROJECT}", f"--region={REGION}"]
    if job.memory:
        argv.append(f"--memory={job.memory}")
    if job.cpu:
        argv.append(f"--cpu={job.cpu}")
    mounted = ([SECRET] if job.secret else []) + list(job.secrets)
    if mounted:
        argv.append("--set-secrets=" + ",".join(f"{name}={name}:latest" for name in mounted))
    model = model_env(job, provider)
    if job.name in chain.JOBS.values():
        # gcloud run jobs deploy keeps env vars it is not told about, so switching off has to remove it.
        # --update-env-vars adds or changes only the variables it names; gcloud keeps every other one.
        if understand:
            argv.append("--update-env-vars=" + ",".join([CHAIN_ENV, *model]))
        else:
            argv.append(f"--remove-env-vars={CHAIN_ENV.split('=')[0]}")
            argv += [f"--update-env-vars={m}" for m in model]
    elif job.env or model:
        argv.append("--update-env-vars=" + ",".join([*job.env, *model]))
    return argv


def execute_argv(job):
    return ["run", "jobs", "execute", job.name, "--wait", f"--project={PROJECT}", f"--region={REGION}"]


def show(argv):
    return "gcloud " + " ".join(argv)


BUILDER_STEP = "py -3.13 core/setup/smoke.py --role builder"


def print_run_smoke():
    print("Smoke run, in this order (task 0.4), once the smoke jobs are deployed. First, from this PC as f42-builder:")
    print(f"  {BUILDER_STEP}")
    print(f"    writes {smoke.OBJECT_URL} if missing, creates the Scheduler job {smoke.SCHEDULE_JOB} paused if missing,")
    print("    then resumes it, runs it now and pauses it again: its token, minted as f42-scheduler, must start")
    print(f"    {smoke.NOOP_JOB} within {smoke.POLL_LIMIT_SECONDS} s. That dispatch is the f42-scheduler smoke test.")
    print("Then each smoke job, which exits non-zero if any of its checks fails:")
    for job in SMOKE_JOBS:
        print("  " + show(execute_argv(job)))


def main(argv=None, gcloud=None, git=git, exists=module_present, workdir=None, session=None, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build", action="store_true", help="build the image for HEAD in Cloud Build")
    parser.add_argument("--apply", action="store_true", help="deploy each enabled job on the image built for HEAD")
    parser.add_argument("--only", choices=[j.name for j in JOBS + SMOKE_JOBS], help="limit the deploy to one job")
    parser.add_argument("--smoke", action="store_true", help="plan or deploy the smoke jobs instead of the chain jobs")
    parser.add_argument("--run-smoke", action="store_true", help="print the commands that run the smoke jobs")
    parser.add_argument("--model-provider", choices=PROVIDERS,
                        help="set MODEL_PROVIDER on f42-brief, f42-understand and f42-scheduled-asks (with gemini also "
                             "the f42-agent service's GEMINI_MODEL on f42-scheduled-asks) through --update-env-vars; left "
                             "as it is when not passed. Smoke jobs never get it: the gemini smoke path runs only with "
                             "--update-env-vars=MODEL_PROVIDER=gemini on the smoke execute")
    args = parser.parse_args(argv)
    if args.only in {j.name for j in SMOKE_JOBS} and not args.smoke:
        sys.exit(f"--only {args.only} is a smoke job: add --smoke")
    understand = not args.smoke and understand_in_chain(exists)
    provider = args.model_provider
    chain_jobs = ", ".join(chain.JOBS.values())
    if understand and args.only in chain.JOBS.values():
        sys.exit(f"--only {args.only} refused: understand is in the chain, so {chain_jobs} deploy together "
                 f"with {CHAIN_ENV}. Drop --only.")
    if args.run_smoke:
        if args.build or args.apply:
            parser.error("--run-smoke only prints; run it on its own")
        print_run_smoke()
        return 0
    gcloud = gcloud or Gcloud()

    tag = git(["rev-parse", "--short", "HEAD"]).strip()
    dirty = git(["status", "--porcelain", "--", *SOURCES]).strip()
    if dirty and (args.build or args.apply):
        sys.exit("Uncommitted changes under core/ or the routes file would not be in the image. Commit first:\n"
                 + dirty)

    print(f"Image {IMAGE}:{tag}, built from a git archive of HEAD {tag} ({', '.join(SOURCES)})")
    if dirty:
        print("Note: uncommitted changes under core/ are not in HEAD, so they would not be in the image.")

    source = Path(workdir) / "source.tar.gz" if workdir else None
    body = build_request(tag)
    upload = upload_argv(source or "<git archive of HEAD>.tar.gz", tag)
    print("Upload the source:")
    print("  " + show(upload))
    print("Build as f42-deployer through the Cloud Build API:")
    print(f"  POST {BUILDS}")
    print(f"    source {upload[3]}, service account {DEPLOYER}, "
          f"logging {body['options']['logging']}, timeout {body['timeout']}")
    for step in body["steps"]:
        print(f"    step {step['name']} {' '.join(step.get('args', []))}")
    for image in body["images"]:
        print(f"    image {image}")
    print(f"  then polls the build every {POLL_SECONDS} s until it ends.")
    print("Digest from the build results, or read back with:")
    print("  " + show(digest_argv(tag)))

    digest = None
    if args.build:
        session = session or cloud_session()
        if workdir is None:
            with tempfile.TemporaryDirectory() as tmp:
                digest = build(gcloud, git, session, sleep, Path(tmp) / "source.tar.gz", tag, body)
        else:
            digest = build(gcloud, git, session, sleep, source, tag, body)
        print("  build finished.")
    if args.apply and digest is None:
        digest = gcloud.run(digest_argv(tag)).strip()
        if not digest.startswith("sha256:"):
            sys.exit(f"No image digest for {IMAGE}:{tag}. Run with --build first.")
        print(f"  digest {digest}")

    image = f"{IMAGE}@{digest or '<digest of tag ' + tag + '>'}"
    if understand:
        print(f"understand is in the chain: {CHAIN_ENV} on {chain_jobs}, all four deployed together.")
    elif not args.smoke:
        print(f"understand is out of the chain: {UNDERSTAND} is missing or the switch is off, "
              f"so {chain_jobs} drop CHAIN_UNDERSTAND and detect follows collect.")
    print("Jobs:")
    if args.smoke:
        print(f"  f42-builder: from this PC before the smoke jobs run: {BUILDER_STEP}")
    for job in SMOKE_JOBS if args.smoke else JOBS:
        if args.only and job.name != args.only:
            continue
        if not job.enabled:
            print(f"  {job.name}: skipped, {job.note}")
            continue
        if job.name == schedule.SCHEDULED_ASKS_JOB and not schedule.SCHEDULED_ASKS_READY:
            print(f"  {job.name}: skipped, {schedule.SCHEDULED_ASKS_OFF}")
            continue
        if job.name in (schedule.PULSE_JOB, schedule.BREAKING_JOB) and not schedule.PULSE_READY:
            print(f"  {job.name}: skipped, {schedule.PULSE_OFF}")
            continue
        if job.name == schedule.DIGEST_JOB and not schedule.DIGEST_READY:
            print(f"  {job.name}: skipped, {schedule.DIGEST_OFF}")
            continue
        if not exists(job.module):
            where = module_file(job.module).relative_to(ROOT).as_posix()
            print(f"  {job.name}: skipped, {where} is not in the working tree ({job.note or 'not built yet'}). "
                  "Once it is, this runs:")
            print("    " + show(deploy_argv(job, image, understand, provider)))
            continue
        extra = ", SocialCrawl key mounted" if job.secret else ""
        extra += "".join(f", {name} mounted" for name in job.secrets)
        if job.env:
            extra += f", env {','.join(job.env)}"
        if job.memory or job.cpu:
            extra += f", memory {job.memory or 'as it is'}, cpu {job.cpu or 'as it is'}"
        if understand and job.name in chain.JOBS.values():
            extra += f", env {CHAIN_ENV}"
        if model_env(job, provider):
            extra += f", env {','.join(model_env(job, provider))}"
        print(f"  {job.name}: python -m {' '.join([job.module, *job.args])} as {job.identity}, "
              f"timeout {job.timeout}, 1 task, max retries {job.retries}{extra}")
        print("    " + show(deploy_argv(job, image, understand, provider)))
        if job.name in ASK_JOBS and not provider:
            print(f"    no --model-provider: MODEL_PROVIDER on {job.name} is left as it is (gemini, the only provider, "
                  "when unset); pass --model-provider gemini to ask on the same model as the f42-agent service")
        if args.smoke:
            print("    runs with: " + show(execute_argv(job)))
        if args.apply:
            gcloud.run(deploy_argv(job, image, understand, provider))
            print(f"    {job.name} deployed.")

    if not (args.build or args.apply):
        print("Dry run: nothing built or deployed. --build builds the image, --apply deploys the jobs above.")
    print("No job was started. Running a job is a separate step" + (": --run-smoke prints the order." if args.smoke else "."))
    return 0


def build(gcloud, git, session, sleep, source, tag, body):
    """Archive, upload, create the build, poll it to the end. Returns the image digest from the results, or None."""
    git(["archive", "--format=tar.gz", f"--output={source}", tag, *SOURCES])
    upload = upload_argv(source, tag)
    gcloud.run(upload)
    print(f"  uploaded {upload[3]}")
    resp = session.post(BUILDS, json=body, timeout=60)
    if resp.status_code != 200:
        sys.exit(f"Cloud Build refused the build: HTTP {resp.status_code} {resp.text}")
    build = resp.json()["metadata"]["build"]
    status = None
    while True:
        if build["status"] != status:
            status = build["status"]
            print(f"  build {build['id']}: {status}")
        if status == "SUCCESS":
            break
        if status in FAILED:
            logs = build.get("logUrl") or (f"https://console.cloud.google.com/cloud-build/builds;region={REGION}/"
                                           f"{build['id']}?project={PROJECT}")
            sys.exit(f"Build {build['id']} ended {status}. Logs: {logs}")
        sleep(POLL_SECONDS)
        resp = session.get(f"{BUILDS}/{build['id']}", timeout=60)
        if resp.status_code != 200:
            sys.exit(f"Could not read build {build['id']}: HTTP {resp.status_code} {resp.text}")
        build = resp.json()
    for image in build.get("results", {}).get("images", []):
        if image.get("name") == f"{IMAGE}:{tag}" and image.get("digest", "").startswith("sha256:"):
            return image["digest"]
    return None


if __name__ == "__main__":
    sys.exit(main())
