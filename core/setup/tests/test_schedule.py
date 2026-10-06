"""Unit tests for core/setup/schedule.py. No cloud access: a fake gcloud records every argv."""
import re
from pathlib import Path

import pytest

from core.setup import schedule as sc

# The flag as shipped, read before the fixture below sets it for each test.
SHIPPED_SCHEDULED_ASKS_READY = sc.SCHEDULED_ASKS_READY


@pytest.fixture(autouse=True)
def digest_not_ready(monkeypatch):
    """The pinned schedule lists here predate the digest and the scheduled asks being switched on; their own tests set
    DIGEST_READY and SCHEDULED_ASKS_READY themselves."""
    monkeypatch.setattr(sc, "DIGEST_READY", False)
    monkeypatch.setattr(sc, "SCHEDULED_ASKS_READY", False)


NAME = "f42-collect-0200"
URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-collect:run"
BRIEF = "f42-brief-0615"
BRIEF_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-brief:run"
RECONCILE = "f42-reconcile-2330"
RECONCILE_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-reconcile:run"
GDELT = "f42-gdelt-0130"
GDELT_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-gdelt:run"
GDELT_DAILY = "f42-gdelt-daily-0330"
GDELT_DAILY_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-gdelt-daily:run"
QUARTER = "f42-watchdog-quarter"
HOURLY = "f42-watchdog-hourly"
WATCHDOG_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-watchdog:run"
DRIFT = "f42-drift-mon-0700"
DRIFT_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-drift:run"
SCHEDULED = "f42-scheduled-asks-0700"
SCHEDULED_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-scheduled-asks:run"
LEARN = "f42-learn-mon-0730"
LEARN_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-learn:run"
CALENDAR = "f42-calendar-mon-0630"
CALENDAR_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-calendar:run"
PINNED = {NAME: ("0 2 * * *", URI), BRIEF: ("15 6 * * *", BRIEF_URI), RECONCILE: ("30 23 * * *", RECONCILE_URI),
          GDELT: ("30 1 * * *", GDELT_URI), GDELT_DAILY: ("30 3 * * *", GDELT_DAILY_URI),
          QUARTER: ("*/15 2-7 * * *", WATCHDOG_URI),
          HOURLY: ("0 0,1,8-23 * * *", WATCHDOG_URI), DRIFT: ("0 7 * * 1", DRIFT_URI),
          LEARN: ("30 7 * * 1", LEARN_URI), CALENDAR: ("30 6 * * 1", CALENDAR_URI)}
SA = "f42-scheduler@ogilvy-trends-v2.iam.gserviceaccount.com"
FORBIDDEN = ("delete", "pause", "update", "resume", "remove")


class FakeGcloud:
    def __init__(self, existing=()):
        self.existing = list(existing)
        self.calls = []

    def run(self, args):
        self.calls.append(list(args))
        if args[:3] == ["scheduler", "jobs", "list"]:
            return "".join(f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{n}\n" for n in self.existing)
        return ""

    def writes(self):
        return [c for c in self.calls if "create" in c]


def assert_no_forbidden(fake):
    for call in fake.calls:
        for arg in call:
            for word in FORBIDDEN:
                assert word not in arg.lower(), call
        assert "--project=ogilvy-trends-v2" in call and "--location=us-central1" in call


def test_dry_run_makes_zero_writes_and_prints_the_plan(capsys):
    fake = FakeGcloud()
    assert sc.main([], gcloud=fake) == 0
    assert fake.writes() == []
    out = capsys.readouterr().out
    assert NAME in out and "0 2 * * *" in out and "Africa/Johannesburg" in out and URI in out and SA in out
    assert BRIEF in out and "15 6 * * *" in out and BRIEF_URI in out
    assert RECONCILE in out and "30 23 * * *" in out and RECONCILE_URI in out
    assert GDELT in out and "30 1 * * *" in out and GDELT_URI in out
    assert QUARTER in out and "*/15 2-7 * * *" in out and HOURLY in out and "0 0,1,8-23 * * *" in out
    assert WATCHDOG_URI in out
    assert DRIFT in out and "0 7 * * 1" in out and DRIFT_URI in out
    assert "dry run" in out.lower()
    assert_no_forbidden(fake)


def test_apply_creates_each_job_once_with_pinned_settings():
    fake = FakeGcloud(existing=["some-other-job"])
    assert sc.main(["--apply"], gcloud=fake) == 0
    writes = fake.writes()
    assert sorted(w[4] for w in writes) == sorted(PINNED)
    for argv in writes:
        name = argv[4]
        schedule, uri = PINNED[name]
        assert argv[:5] == ["scheduler", "jobs", "create", "http", name]
        for flag in (f"--schedule={schedule}", "--time-zone=Africa/Johannesburg", f"--uri={uri}",
                     "--http-method=POST", f"--oauth-service-account-email={SA}", "--max-retry-attempts=1",
                     "--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform"):
            assert flag in argv, (name, flag)
        assert any(a.startswith("--attempt-deadline=") for a in argv)
    assert_no_forbidden(fake)


@pytest.mark.parametrize("apply", [[], ["--apply"]])
def test_existing_jobs_mean_zero_writes(apply, capsys):
    fake = FakeGcloud(existing=list(PINNED))
    assert sc.main(apply, gcloud=fake) == 0
    assert fake.writes() == []
    assert capsys.readouterr().out.lower().count("exists") == len(PINNED)
    assert_no_forbidden(fake)


@pytest.mark.parametrize("missing", list(PINNED))
def test_apply_creates_only_the_missing_job(missing):
    fake = FakeGcloud(existing=[n for n in PINNED if n != missing])
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert [w[4] for w in fake.writes()] == [missing]
    assert_no_forbidden(fake)


def test_does_not_refuse_the_impersonated_builder(monkeypatch):
    monkeypatch.setenv("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT", "f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com")
    fake = FakeGcloud()
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert len(fake.writes()) == len(PINNED)


def test_reconcile_is_described_as_the_nightly_reconciliation_not_the_morning_chain():
    fake = FakeGcloud()
    sc.main(["--apply"], gcloud=fake)
    argv = next(w for w in fake.writes() if w[4] == RECONCILE)
    description = next(a for a in argv if a.startswith("--description="))
    assert "reconcil" in description and "morning chain" not in description


def test_watchdog_schedules_are_the_ones_watchdog_py_declares():
    from core.setup import watchdog
    ours = [(name, cron) for name, cron, job, _ in sc.SCHEDULES if job == watchdog.JOB["name"]]
    assert ours == list(watchdog.SCHEDULES)


@pytest.mark.parametrize("name, word", [(GDELT, "gdelt"), (QUARTER, "watchdog"), (HOURLY, "watchdog"),
                                        (DRIFT, "drift")])
def test_new_jobs_are_described_apart_from_the_morning_chain(name, word):
    fake = FakeGcloud()
    sc.main(["--apply"], gcloud=fake)
    argv = next(w for w in fake.writes() if w[4] == name)
    description = next(a for a in argv if a.startswith("--description="))
    assert "morning chain: starts" not in description
    assert word in description.lower()


def test_drift_runs_once_a_week_on_monday_at_seven_sast_and_starts_only_the_drift_job():
    [entry] = [s for s in sc.SCHEDULES if s[0] == DRIFT]
    assert entry[1:3] == ("0 7 * * 1", "f42-drift")
    assert sc.TIME_ZONE == "Africa/Johannesburg"
    assert [s[0] for s in sc.SCHEDULES if s[2] == "f42-drift"] == [DRIFT]


def test_learn_runs_once_a_week_on_monday_at_half_past_seven_sast_and_starts_only_the_learn_job():
    [entry] = [s for s in sc.SCHEDULES if s[0] == LEARN]
    assert entry[1:3] == ("30 7 * * 1", "f42-learn")
    assert sc.TIME_ZONE == "Africa/Johannesburg"
    assert [s[0] for s in sc.SCHEDULES if s[2] == "f42-learn"] == [LEARN]
    assert "f42-learn" not in chain_jobs()


def test_learn_is_planned_in_the_dry_run_and_described_apart_from_the_chain(capsys):
    fake = FakeGcloud()
    assert sc.main([], gcloud=fake) == 0
    assert fake.writes() == []
    out = capsys.readouterr().out
    assert f"Scheduler job {LEARN} in ogilvy-trends-v2/us-central1" in out
    assert "30 7 * * 1 (Africa/Johannesburg)" in out and LEARN_URI in out
    fake = FakeGcloud()
    sc.main(["--apply"], gcloud=fake)
    argv = next(w for w in fake.writes() if w[4] == LEARN)
    description = next(a for a in argv if a.startswith("--description="))
    assert "learn" in description and "morning chain" not in description


def test_scheduled_asks_run_every_day_at_seven_sast_and_start_only_their_job():
    [entry] = [s for s in sc.SCHEDULES if s[0] == SCHEDULED]
    assert entry[1:3] == ("0 7 * * *", "f42-scheduled-asks")
    assert [s[0] for s in sc.SCHEDULES if s[2] == "f42-scheduled-asks"] == [SCHEDULED]


def test_scheduled_asks_ship_switched_on():
    # Deliberate change on 3 October 2026: this was False until Albert said go.
    assert SHIPPED_SCHEDULED_ASKS_READY is True


def test_scheduled_asks_while_not_ready_are_one_skip_line_and_no_create(monkeypatch, capsys):
    monkeypatch.setattr(sc, "SCHEDULED_ASKS_READY", False)
    fake = FakeGcloud()
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert sorted(w[4] for w in fake.writes()) == sorted(PINNED)
    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if "scheduled-asks" in ln]
    assert line == f"Scheduler job {SCHEDULED}: skipped, {sc.SCHEDULED_ASKS_OFF}"
    for word in ("f42-agent", "bootstrap.py", "fastapi", "starlette", "sqlglot",
                 "SCHEDULED_ASKS_READY"):
        assert word in line, word
    assert "CHANNEL_WEBHOOK_URL" not in line and "webhook" not in line
    assert_no_forbidden(fake)


def test_scheduled_asks_when_ready_are_planned_in_the_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(sc, "SCHEDULED_ASKS_READY", True)
    fake = FakeGcloud()
    assert sc.main([], gcloud=fake) == 0
    assert fake.writes() == []
    out = capsys.readouterr().out
    assert SCHEDULED in out and SCHEDULED_URI in out
    assert not any("skipped" in ln for ln in out.splitlines() if "scheduled-asks" in ln)
    assert f"{SCHEDULED} is missing. Dry run: nothing created." in out


def test_scheduled_asks_when_ready_are_created_only_while_missing_and_never_edited(monkeypatch):
    monkeypatch.setattr(sc, "SCHEDULED_ASKS_READY", True)
    fake = FakeGcloud(existing=list(PINNED))
    assert sc.main(["--apply"], gcloud=fake) == 0
    [argv] = fake.writes()
    assert argv[:5] == ["scheduler", "jobs", "create", "http", SCHEDULED]
    assert "--schedule=0 7 * * *" in argv and "--time-zone=Africa/Johannesburg" in argv
    assert f"--uri={SCHEDULED_URI}" in argv
    description = next(a for a in argv if a.startswith("--description="))
    assert "scheduled questions" in description and "morning chain: starts" not in description
    assert_no_forbidden(fake)
    fake = FakeGcloud(existing=[*PINNED, SCHEDULED])
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert fake.writes() == []


def test_a_failed_list_stops_before_any_write():
    class Broken(FakeGcloud):
        def run(self, args):
            self.calls.append(list(args))
            raise sc.GcloudError("PERMISSION_DENIED")

    fake = Broken()
    with pytest.raises(sc.GcloudError):
        sc.main(["--apply"], gcloud=fake)
    assert fake.writes() == []


def test_real_gcloud_closes_stdin_and_decodes_utf8(monkeypatch):
    seen = {}

    class Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen.update(kw)
        return Proc()

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    assert sc.Gcloud("gcloud").run(["scheduler", "jobs", "list"]) == "ok"
    assert seen["argv"] == ["gcloud", "scheduler", "jobs", "list"]
    assert seen["stdin"] is sc.subprocess.DEVNULL and seen["encoding"] == "utf-8"


def test_no_banned_literals_or_prose_dashes_in_the_schedule_files():
    # Built with join so no banned literal survives compile time constant folding into the bytecode.
    banned = re.compile("|".join(["".join(["gen", "z"]), "".join(["gen", " z"]), "".join(["google", "_trends"])]), re.I)
    dashes = re.compile("[" + chr(0x2013) + chr(0x2014) + r"]|\s" + "-" * 2 + r"\s")
    for path in (Path(sc.__file__), Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert not banned.search(text), path
        assert not dashes.search(text), path


PULSE = "f42-pulse-3h"
PULSE_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-pulse:run"


def test_the_pulse_runs_every_three_hours_from_nine_to_nine_sast_and_starts_only_its_job():
    [entry] = [s for s in sc.SCHEDULES if s[0] == PULSE]
    assert entry[1:3] == ("0 9-21/3 * * *", "f42-pulse")
    assert [s[0] for s in sc.SCHEDULES if s[2] == "f42-pulse"] == [PULSE]
    assert "f42-pulse" not in chain_jobs()


def chain_jobs():
    from core.collect import chain
    return set(chain.JOBS.values())


def test_the_pulse_is_off_by_default_so_one_skip_line_and_no_create(capsys):
    assert sc.PULSE_READY is False
    fake = FakeGcloud()
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert sorted(w[4] for w in fake.writes()) == sorted(PINNED)
    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if "pulse" in ln]
    assert line == f"Scheduler job {PULSE}: skipped, {sc.PULSE_OFF}"
    for word in ("Albert", "PULSE_READY"):
        assert word in line, word
    assert_no_forbidden(fake)


def test_the_pulse_off_dry_run_prints_only_its_skip_line(capsys):
    fake = FakeGcloud()
    assert sc.main([], gcloud=fake) == 0
    assert fake.writes() == []
    out = capsys.readouterr().out
    assert [ln for ln in out.splitlines() if "pulse" in ln] == [f"Scheduler job {PULSE}: skipped, {sc.PULSE_OFF}"]


def test_the_pulse_when_ready_is_created_only_while_missing_and_described_apart_from_the_chain(monkeypatch):
    monkeypatch.setattr(sc, "PULSE_READY", True)
    fake = FakeGcloud(existing=[*PINNED, "f42-breaking-hourly"])
    assert sc.main(["--apply"], gcloud=fake) == 0
    [argv] = fake.writes()
    assert argv[:5] == ["scheduler", "jobs", "create", "http", PULSE]
    assert "--schedule=0 9-21/3 * * *" in argv and "--time-zone=Africa/Johannesburg" in argv
    assert f"--uri={PULSE_URI}" in argv
    description = next(a for a in argv if a.startswith("--description="))
    assert "pulse" in description and "morning chain" not in description
    assert_no_forbidden(fake)
    fake = FakeGcloud(existing=[*PINNED, PULSE, "f42-breaking-hourly"])
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert fake.writes() == []


BREAKING = "f42-breaking-hourly"
BREAKING_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-breaking:run"


def test_breaking_runs_hourly_at_five_past_and_starts_only_its_job():
    [entry] = [s for s in sc.SCHEDULES if s[0] == BREAKING]
    assert entry[1:3] == ("5 * * * *", "f42-breaking")
    assert [s[0] for s in sc.SCHEDULES if s[2] == "f42-breaking"] == [BREAKING]
    assert "f42-breaking" not in chain_jobs()


def test_breaking_is_off_with_the_pulse_so_one_skip_line_and_no_create(capsys):
    assert sc.PULSE_READY is False
    fake = FakeGcloud()
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert not any(BREAKING in a for w in fake.writes() for a in w)
    [line] = [ln for ln in capsys.readouterr().out.splitlines() if BREAKING in ln]
    assert line == f"Scheduler job {BREAKING}: skipped, {sc.PULSE_OFF}"


def test_breaking_when_the_pulse_is_ready_is_created_only_while_missing(monkeypatch):
    monkeypatch.setattr(sc, "PULSE_READY", True)
    fake = FakeGcloud(existing=[*PINNED, PULSE])
    assert sc.main(["--apply"], gcloud=fake) == 0
    [argv] = fake.writes()
    assert argv[:5] == ["scheduler", "jobs", "create", "http", BREAKING]
    assert "--schedule=5 * * * *" in argv and "--time-zone=Africa/Johannesburg" in argv
    assert f"--uri={BREAKING_URI}" in argv
    description = next(a for a in argv if a.startswith("--description="))
    assert "Breaking" in description and "morning chain" not in description
    assert_no_forbidden(fake)
    fake = FakeGcloud(existing=[*PINNED, PULSE, BREAKING])
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert fake.writes() == []


def test_the_weekly_drift_job_carries_the_calendar_analogues_with_the_grants_the_write_needs():
    """calendar_analogues is refreshed by f42-drift (core/collect/drift.py), so that job must stay weekly and run as
    an identity that can read posts and the legacy history and append to calendar_analogues."""
    from core.collect import calendar, drift
    from core.setup import bootstrap as bs
    from core.setup import deploy_jobs as dj
    from core.setup.tests.test_bootstrap import all_bindings

    assert (DRIFT, "0 7 * * 1", "f42-drift") in [s[:3] for s in sc.SCHEDULES]
    [job] = [j for j in dj.JOBS if j.name == "f42-drift"]
    assert job.module == drift.__name__ and job.args == ()
    assert "weekly_analogues" in drift._calendar_analogues.__code__.co_names
    held = {(b.role, b.kind, b.name) for b in all_bindings() if b.identity == job.identity}
    core, legacy = calendar.ANALOGUE_TABLE.split(".")[1], calendar.LEGACY_TABLE.split(".")[1]
    assert calendar.POSTS_TABLE.split(".")[1] == core
    assert (bs.EDIT, "dataset", core) in held
    assert (bs.VIEW, "dataset", legacy) in held
    assert ("roles/bigquery.jobUser", "project", bs.PROJECT) in held


DIGEST = "f42-digest-0645"
DIGEST_URI = "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/jobs/f42-digest:run"


def test_the_digest_runs_every_day_at_quarter_to_seven_sast_after_the_brief_and_starts_only_its_job():
    [entry] = [s for s in sc.SCHEDULES if s[0] == DIGEST]
    assert entry[1:3] == ("45 6 * * *", "f42-digest")
    assert [s[0] for s in sc.SCHEDULES if s[2] == "f42-digest"] == [DIGEST]
    assert "f42-digest" not in chain_jobs()
    [brief] = [s for s in sc.SCHEDULES if s[0] == BRIEF]
    assert brief[1] == "15 6 * * *"  # the publish deadline; the watchdog's brief_late fires from 06:30


def test_the_digest_while_not_ready_is_one_skip_line_and_no_create(capsys, monkeypatch):
    monkeypatch.setattr(sc, "DIGEST_READY", False)
    fake = FakeGcloud()
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert sorted(w[4] for w in fake.writes()) == sorted(PINNED)
    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if "digest" in ln]
    assert line == f"Scheduler job {DIGEST}: skipped, {sc.DIGEST_OFF}"
    for word in ("GMAIL_APP_PASSWORD", "f42-agent", "bootstrap.py", "DIGEST_READY"):
        assert word in line, word
    assert_no_forbidden(fake)


def test_the_digest_when_ready_is_created_only_while_missing_and_described_apart_from_the_chain(monkeypatch):
    monkeypatch.setattr(sc, "DIGEST_READY", True)
    fake = FakeGcloud(existing=list(PINNED))
    assert sc.main(["--apply"], gcloud=fake) == 0
    [argv] = fake.writes()
    assert argv[:5] == ["scheduler", "jobs", "create", "http", DIGEST]
    assert "--schedule=45 6 * * *" in argv and "--time-zone=Africa/Johannesburg" in argv
    assert f"--uri={DIGEST_URI}" in argv
    description = next(a for a in argv if a.startswith("--description="))
    assert "digest" in description and "morning chain" not in description
    assert_no_forbidden(fake)
    fake = FakeGcloud(existing=[*PINNED, DIGEST])
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert fake.writes() == []


def test_the_calendar_refresh_runs_on_monday_at_half_past_six_before_drift_and_starts_only_its_job():
    [entry] = [s for s in sc.SCHEDULES if s[0] == CALENDAR]
    assert entry[1:3] == ("30 6 * * 1", "f42-calendar")
    assert [s[0] for s in sc.SCHEDULES if s[2] == "f42-calendar"] == [CALENDAR]
    assert "f42-calendar" not in chain_jobs()
    [drift] = [s for s in sc.SCHEDULES if s[0] == DRIFT]
    assert drift[1] == "0 7 * * 1"


def test_the_calendar_refresh_is_the_one_new_create_and_every_existing_entry_is_left_unchanged(capsys):
    existing = [s[0] for s in sc.SCHEDULES if s[0] != CALENDAR]
    fake = FakeGcloud(existing=existing)
    assert sc.main([], gcloud=fake) == 0
    assert fake.writes() == []
    out = capsys.readouterr().out
    assert [ln.strip() for ln in out.splitlines() if "is missing" in ln] == [
        f"{CALENDAR} is missing. Dry run: nothing created. Run again with --apply to create it."]
    fake = FakeGcloud(existing=existing)
    assert sc.main(["--apply"], gcloud=fake) == 0
    [argv] = fake.writes()
    assert argv[:5] == ["scheduler", "jobs", "create", "http", CALENDAR]
    assert "--schedule=30 6 * * 1" in argv and "--time-zone=Africa/Johannesburg" in argv
    assert f"--uri={CALENDAR_URI}" in argv
    description = next(a for a in argv if a.startswith("--description="))
    assert "calendar" in description and "morning chain" not in description
    assert [c[:3] for c in fake.calls if "create" not in c] == [["scheduler", "jobs", "list"]]
    assert_no_forbidden(fake)


def test_with_the_shipped_flags_only_the_scheduled_asks_and_the_calendar_entries_are_new(monkeypatch, capsys):
    monkeypatch.setattr(sc, "SCHEDULED_ASKS_READY", SHIPPED_SCHEDULED_ASKS_READY)
    before = [s[0] for s in sc.SCHEDULES if s[0] not in (CALENDAR, SCHEDULED)]
    fake = FakeGcloud(existing=before)
    assert sc.main(["--apply"], gcloud=fake) == 0
    assert sorted(w[4] for w in fake.writes()) == sorted([SCHEDULED, CALENDAR])
    assert_no_forbidden(fake)


def test_the_gdelt_daily_aggregate_runs_at_half_past_three_sast_after_the_news_day_settles_and_starts_only_its_job():
    """Created by hand on 4 October 2026; declared so the plan, checks and docs match staging. f42-gdelt-daily refuses
    a UTC news day before 01:00 UTC (03:00 SAST), so its entry must fire after that. f42-gdelt-0130 stays as it is."""
    from core.setup import deploy_jobs as dj
    [entry] = [s for s in sc.SCHEDULES if s[0] == GDELT_DAILY]
    assert entry[1:3] == ("30 3 * * *", "f42-gdelt-daily")
    assert int(entry[1].split()[1]) >= 3 and sc.TIME_ZONE == "Africa/Johannesburg"
    assert [s[0] for s in sc.SCHEDULES if s[2] == "f42-gdelt-daily"] == [GDELT_DAILY]
    assert [s[:3] for s in sc.SCHEDULES if s[0] == GDELT] == [(GDELT, "30 1 * * *", "f42-gdelt")]
    assert "f42-gdelt-daily" in {j.name for j in dj.JOBS}
    assert "f42-gdelt-daily" not in chain_jobs()


def test_the_gdelt_daily_entry_already_on_staging_is_left_unchanged_and_never_edited(capsys):
    fake = FakeGcloud(existing=[s[0] for s in sc.SCHEDULES])
    for argv in ([], ["--apply"]):
        assert sc.main(argv, gcloud=fake) == 0
    assert fake.writes() == []
    out = capsys.readouterr().out
    assert f"Scheduler job {GDELT_DAILY} in ogilvy-trends-v2/us-central1" in out
    assert "30 3 * * * (Africa/Johannesburg)" in out and f"POST {GDELT_DAILY_URI}" in out and SA in out
    assert out.count(f"{GDELT_DAILY} already exists; left unchanged.") == 2
    assert [c[:3] for c in fake.calls] == [["scheduler", "jobs", "list"]] * 2
    assert_no_forbidden(fake)


def test_the_gdelt_daily_entry_is_described_apart_from_the_morning_chain():
    fake = FakeGcloud(existing=[s[0] for s in sc.SCHEDULES if s[0] != GDELT_DAILY])
    assert sc.main(["--apply"], gcloud=fake) == 0
    [argv] = fake.writes()
    assert argv[:5] == ["scheduler", "jobs", "create", "http", GDELT_DAILY]
    for flag in ("--schedule=30 3 * * *", "--time-zone=Africa/Johannesburg", f"--uri={GDELT_DAILY_URI}",
                 "--http-method=POST", f"--oauth-service-account-email={SA}"):
        assert flag in argv, flag
    description = next(a for a in argv if a.startswith("--description="))
    assert "gdelt daily" in description.lower() and "morning chain" not in description
    assert_no_forbidden(fake)
