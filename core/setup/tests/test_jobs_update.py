"""The update path of Release B (W8-REL-B v2.1 3.4 to 3.7, JU-04 to JU-07) and the prefix recovery of the fake adapter
(JX-01 to JX-05, JX-07). One orchestrator, one fake Cloud Run, the same positive allowlist on every call it makes."""
import copy
import datetime as dt
import json

import pytest

from core.setup.release import jobs_only as jo
from core.setup.release import jobs_run as jr
from core.setup.release import plan
from core.setup.release import services_only as so
from core.setup.tests import cloud_world as cw
from core.setup.tests import jobs_world as jw

ORDER = ["f42-watchdog", "f42-probe", "f42-gdelt", "f42-gdelt-daily", "f42-reconcile", "f42-drift", "f42-learn", "f42-calendar",
         "f42-digest", "f42-scheduled-asks", "f42-brief", "f42-detect", "f42-understand", "f42-collect"]
CHAIN = ORDER[10:]
SAST = jw.SAST


def sast(hour, minute=0, day=11):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=SAST).astimezone(jw.UTC)


def started(tmp_path, *, at=None):
    """The world at the moment the operator has typed IDLE and DEPLOY: JobsCandidate and BeforeJobsUpdate have run."""
    w = jw.ReleaseWorld(tmp_path)
    w.now = at or w.now
    w.prepare()
    clock = cw.Clock(w.now)
    cloud = cw.FakeCloudRun(w.world, clock)
    w.fake_reader = cloud
    w.run("BeforeAnyWrite", reader=cloud)
    w.built()
    w.run("BeforeJobsUpdate", reader=cloud)
    return w, cloud, clock


def update(w, cloud, clock, **kw):
    return jr.run_update(w.bound, cloud, w.bq_client(), w.evidence, now=clock.now, **kw)


def rollback(w, cloud, clock):
    return jr.run_rollback(w.bound, cloud, w.evidence, now=clock.now)


def digests(cloud):
    return {job: cloud.digest_of(job) for job in so.JOB_NAMES}


def update_targets(cloud):
    """(job, digest) of every `jobs update` the orchestrator issued, in order."""
    return [(a[4], a[6].split("@")[1]) for a in cloud.argv_calls if a[:4] == ["gcloud", "run", "jobs", "update"]]


def restore_targets(cloud):
    return [job for job, digest in update_targets(cloud) if digest == jw.ROLLBACK_DIGEST]


NEW, OLD = jw.NEW_DIGEST, jw.ROLLBACK_DIGEST


# JX-01: the fake itself

def test_jx01_the_fake_world_holds_14_definitions_a_template_per_revision_and_no_service_method(tmp_path):
    w, cloud, clock = started(tmp_path)
    assert set(cloud.templates) == set(so.JOB_NAMES) and len(cloud.templates) == 14
    assert all(len(t) == 1 for t in cloud.templates.values())
    log = update(w, cloud, clock)
    assert log["complete"] is True and all(len(cloud.templates[j]) == 2 for j in so.JOB_NAMES)
    assert all(cloud.rev_of[j] == 2 for j in so.JOB_NAMES)
    assert {tuple(a[1:3]) for a in cloud.argv_calls} <= {("run", "jobs")}


def test_jx01_an_execution_captures_the_template_at_the_moment_it_starts(tmp_path):
    w, cloud, clock = started(tmp_path)
    before = cloud.start("f42-detect")
    cloud.world.jobs["f42-detect"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = jo.image_reference(NEW)
    cloud.rev_of["f42-detect"] += 1
    after = cloud.start("f42-detect")
    assert cloud.captured[before] == (1, OLD) and cloud.captured[after] == (2, NEW)
    assert jo.execution_view(cloud.world.executions[before])["image_digest"] == OLD


def test_jx01_the_scheduler_double_starts_any_job_before_during_and_after_each_update(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.injections = {("before", 2): ["f42-collect"], ("during", 2): ["f42-brief"], ("after", 2): ["f42-detect"]}
    update(w, cloud, clock)
    by_job = {s["job"]: s for s in cloud.started}
    assert set(by_job) == {"f42-collect", "f42-brief", "f42-detect"}
    assert [s["job"] for s in cloud.started] == ["f42-collect", "f42-brief", "f42-detect"]
    assert all(s["digest"] == OLD for s in cloud.started[:2])
    assert cloud.started[2]["digest"] == OLD  # detect is updated later; it is still old after the third update


def test_jx01_the_fake_plays_only_what_the_allowlist_passes(tmp_path):
    w, cloud, clock = started(tmp_path)
    with pytest.raises(so.Stop) as stop:
        cloud._call(["gcloud", "run", "jobs", "execute", "f42-collect", "--project", "ogilvy-trends-v2", "--region", "us-central1"])
    assert stop.value.code == "WRITE_REFUSED"
    assert cloud.update_calls == 0 and cloud.started == []


# JX-02

def test_jx02_a_start_during_an_update_keeps_the_template_it_started_with(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.injections = {("during", 0): ["f42-watchdog"], ("before", 0): ["f42-watchdog"], ("after", 0): ["f42-watchdog"]}
    update(w, cloud, clock)
    before, during, after = cloud.started
    assert (before["digest"], during["digest"], after["digest"]) == (OLD, OLD, NEW)
    assert jo.execution_view(cloud.world.executions[during["name"]])["image_digest"] == OLD
    assert cloud.digest_of("f42-watchdog") == NEW


# JX-03

@pytest.mark.parametrize("p", range(15))
def test_jx03_a_start_after_the_pth_update_runs_the_mixed_set_the_table_names(tmp_path, p):
    w, cloud, clock = started(tmp_path)
    # p updates have landed when the start is injected: after update p-1, or before the first update for p == 0
    cloud.injections = {("before", 0): list(ORDER)} if p == 0 else {("after", p - 1): list(ORDER)}
    update(w, cloud, clock)
    runs = {s["job"]: s["digest"] for s in cloud.started}
    assert set(runs) == set(ORDER)
    assert {job: runs[job] for job in ORDER[:p]} == {job: NEW for job in ORDER[:p]}
    assert {job: runs[job] for job in ORDER[p:]} == {job: OLD for job in ORDER[p:]}
    if 10 <= p < 14:
        assert [j for j in CHAIN if runs[j] == NEW] == CHAIN[:p - 10]  # never a new collect with an old downstream stage
    assert not (runs["f42-collect"] == NEW and any(runs[j] == OLD for j in CHAIN))


# JU-04 and JX-04

def stopped(log):
    return log["stopped"]["code"] if log["stopped"] else None


@pytest.mark.parametrize("k", range(10))
def test_ju04_outside_the_chain_group_a_nonzero_exit_stops_with_no_restore_and_the_declared_branch(tmp_path, k):
    w, cloud, clock = started(tmp_path)
    cloud.fail[ORDER[k]] = 1
    log = update(w, cloud, clock)
    assert stopped(log) == "UPDATE_FAILED" and log["branch"] == "hold_non_chain" and log["complete"] is False
    assert restore_targets(cloud) == []
    assert [job for job, _ in update_targets(cloud)] == ORDER[:k + 1]
    assert [digests(cloud)[j] for j in ORDER[:k]] == [NEW] * k and digests(cloud)[ORDER[k]] == OLD
    assert "21:00" in log["stopped"]["branch_text"] and "23:30" in log["stopped"]["branch_text"]


@pytest.mark.parametrize("k", [0, 5, 9])
def test_ju04_outside_the_chain_group_drift_after_an_update_stops_with_no_restore(tmp_path, k):
    w, cloud, clock = started(tmp_path)
    cloud.after_apply[ORDER[k]] = lambda raw: raw["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].update(args=["--drifted"])
    log = update(w, cloud, clock)
    assert stopped(log) == "JOB_DRIFT" and log["branch"] == "hold_non_chain" and restore_targets(cloud) == []
    assert digests(cloud)[ORDER[k]] == NEW


def test_ju04_the_run_log_records_changed_definitions_and_active_executions_in_every_case(tmp_path):
    for label, setup in (("complete", lambda c: None), ("hold", lambda c: c.fail.update({"f42-probe": 1})),
                         ("restored", lambda c: c.fail.update({"f42-detect": 1}))):
        w, cloud, clock = started(tmp_path / label)
        setup(cloud)
        cloud.start("f42-watchdog", "live")
        log = update(w, cloud, clock)
        assert isinstance(log["active_executions"], list) and {"job": "f42-watchdog", "name": "f42-watchdog-live0"} in log["active_executions"], label
        assert log["changed_definitions"], label
        for entry in log["changed_definitions"]:
            assert set(entry) == {"job", "before_sha256", "after_sha256"} and entry["before_sha256"] != entry["after_sha256"]
        written = sorted(w.evidence.glob("jobs-update-*.json"))
        assert len(written) == 1 and json.loads(written[0].read_text(encoding="utf-8"))["changed_definitions"] == log["changed_definitions"], label


JX04_CAUSES = ("exit", "drift", "window")


def cause(cloud, clock, how, job_index):
    """Make the update of ORDER[job_index] fail the way `how` says."""
    job = ORDER[job_index]
    if how == "exit":
        cloud.fail[job] = 1
    elif how == "drift":
        cloud.after_apply[job] = lambda raw: raw["spec"]["template"]["spec"]["template"]["spec"]["containers"][0].update(args=["--drifted"])
    else:
        cloud.minutes_per_update = 180 / job_index


@pytest.mark.parametrize("how", JX04_CAUSES)
@pytest.mark.parametrize("index", range(10, 14))
def test_jx04_inside_the_chain_group_the_abort_restores_the_prefix_in_reverse_order_then_stops(tmp_path, how, index):
    w, cloud, clock = started(tmp_path, at=sast(18, 0))
    cause(cloud, clock, how, index)
    log = update(w, cloud, clock)
    applied = ORDER[:index] if how in ("exit", "window") else ORDER[:index + 1]
    expected_restores = [j for j in reversed(applied) if j in CHAIN]
    assert restore_targets(cloud) == expected_restores
    assert stopped(log) == {"exit": "UPDATE_FAILED", "drift": "JOB_DRIFT", "window": "WINDOW"}[how]
    assert log["branch"] == "chain_group_restored" and log["unrestored"] == [] and log["restored"] == expected_restores
    final = digests(cloud)
    assert all(final[j] == OLD for j in CHAIN) and all(final[j] == NEW for j in ORDER[:10])
    w.run("BeforeJobsRollback", reader=cloud, now=clock.now())


@pytest.mark.parametrize("how", JX04_CAUSES)
@pytest.mark.parametrize("index", [11, 13])
def test_jx04_when_the_restore_itself_fails_the_prefix_is_recorded_unrestored_and_the_next_rollback_completes_it(tmp_path, how, index):
    w, cloud, clock = started(tmp_path, at=sast(18, 0))
    cause(cloud, clock, how, index)
    cloud.refuse_restores = True
    log = update(w, cloud, clock)
    assert "CHAIN_PREFIX_UNRESTORED" in log["codes"] and log["branch"] == "CHAIN_PREFIX_UNRESTORED"
    stuck = [j for j in CHAIN if digests(cloud)[j] == NEW]
    assert stuck and log["unrestored"] == [j for j in reversed(CHAIN) if j in stuck]
    cloud.refuse_restores = False
    outcome = rollback(w, cloud, clock)
    assert outcome["complete"] is True and all(v == OLD for v in digests(cloud).values())
    if how == "drift":
        # the rollback restores the image and never stops for a drift; the readback is what reports the drift that remains
        assert outcome["residual_drift"] and w.stop("AfterJobsRollback", reader=cloud, now=clock.now()).code == "JOB_DRIFT"
    else:
        assert w.run("AfterJobsRollback", reader=cloud, now=clock.now())["phase"] == "AfterJobsRollback"


def test_jx04_an_expired_credential_double_is_a_refused_restore_too(tmp_path):
    w, cloud, clock = started(tmp_path)
    cause(cloud, clock, "exit", 12)
    cloud.expire_credential_at = 12 + 1  # the failing update is call 12, the credential dies for every call after it
    log = update(w, cloud, clock)
    assert log["branch"] == "CHAIN_PREFIX_UNRESTORED" and set(log["unrestored"]) == {"f42-brief", "f42-detect"}


@pytest.mark.parametrize("index", range(10))
def test_jx04_outside_the_chain_group_the_abort_prints_the_declared_branch_and_does_not_restore(tmp_path, index, capsys):
    w, cloud, clock = started(tmp_path, at=sast(18, 0))
    cause(cloud, clock, "exit" if index % 2 else "drift", index)
    log = update(w, cloud, clock)
    assert log["branch"] == "hold_non_chain" and restore_targets(cloud) == [] and log["restored"] == []
    assert all(digests(cloud)[j] == NEW for j in ORDER[:index])


# JU-05

def test_ju05_a_second_run_after_a_complete_update_reads_every_job_and_updates_none(tmp_path):
    w, cloud, clock = started(tmp_path)
    update(w, cloud, clock)
    clock.advance(minutes=1)
    w.run("BeforeJobsUpdate", reader=cloud, now=clock.now())
    cloud.argv_calls.clear()
    before = dict(digests(cloud))
    log = update(w, cloud, clock)
    assert log["complete"] is True and update_targets(cloud) == [] and log["updated"] == [] and log["skipped_already_new"] == ORDER
    assert digests(cloud) == before
    assert sum(1 for a in cloud.argv_calls if a[3] == "describe") >= 14


def test_ju05_the_order_resumes_after_a_stop_and_touches_only_the_jobs_not_yet_updated(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.fail["f42-gdelt-daily"] = 1
    log = update(w, cloud, clock)
    assert stopped(log) == "UPDATE_FAILED"
    cloud.fail.clear()
    cloud.argv_calls.clear()
    clock.advance(minutes=1)
    w.run("BeforeJobsUpdate", reader=cloud, now=clock.now())
    log = update(w, cloud, clock)
    assert log["complete"] is True and [j for j, _ in update_targets(cloud)] == ORDER[3:]
    assert log["skipped_already_new"] == ORDER[:3] and all(v == NEW for v in digests(cloud).values())


def test_ju05_after_a_chain_group_restore_a_rerun_updates_only_the_chain_group(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.fail["f42-understand"] = 1
    update(w, cloud, clock)
    cloud.fail.clear()
    cloud.argv_calls.clear()
    clock.advance(minutes=1)
    w.run("BeforeJobsUpdate", reader=cloud, now=clock.now())
    log = update(w, cloud, clock)
    assert log["complete"] is True and [j for j, _ in update_targets(cloud)] == CHAIN


def test_ju05_a_job_that_is_neither_the_baseline_nor_the_new_definition_is_never_updated_over(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.world.jobs["f42-probe"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["args"] = ["--changed"]
    log = update(w, cloud, clock)
    assert stopped(log) == "JOB_DRIFT" and [j for j, _ in update_targets(cloud)] == ["f42-watchdog"]


# JU-06

def test_ju06_the_update_refuses_outside_the_window_before_the_first_call(tmp_path):
    for hour, minute in ((8, 4), (21, 0), (23, 0)):
        w, cloud, clock = started(tmp_path / f"{hour}{minute}", at=sast(10, 0))
        clock.t = sast(hour, minute)
        with pytest.raises(so.Stop) as stop:
            update(w, cloud, clock)
        assert stop.value.code == "WINDOW", (hour, minute)
        assert cloud.update_calls == 0
    w, cloud, clock = started(tmp_path / "open", at=sast(8, 5))
    assert update(w, cloud, clock)["complete"] is True


def test_ju06_the_window_check_itself_is_what_refuses_at_0804_and_2100_when_the_snapshot_is_fresh(tmp_path):
    for hour, minute, allowed in ((8, 4, False), (8, 5, True), (20, 59, True), (21, 0, False)):
        w = jw.ReleaseWorld(tmp_path / f"w{hour}{minute}")
        w.now = sast(hour, minute)
        w.prepare()
        clock = cw.Clock(w.now)
        cloud = cw.FakeCloudRun(w.world, clock)
        w.fake_reader = cloud
        snapshot = jo.quiet_snapshot(cloud, jr.runner_for(w.bound, w.bq_client()), clock.now())
        w.release_dir.joinpath("readbacks").mkdir(exist_ok=True)
        for phase in ("BeforeAnyWrite",):
            jw.write_json(w.release_dir / "readbacks" / f"{phase}-01.json", {"phase": phase, "at_utc": clock.now().isoformat()})
        jw.write_json(w.release_dir / "readbacks" / "BeforeJobsUpdate-01.json", {"phase": "BeforeJobsUpdate", "at_utc": clock.now().isoformat(),
                                                                                   "quiet_snapshot": snapshot})
        if allowed:
            assert update(w, cloud, clock)["complete"] is True
        else:
            with pytest.raises(so.Stop) as stop:
                update(w, cloud, clock)
            assert stop.value.code == "WINDOW" and cloud.update_calls == 0


@pytest.mark.parametrize("m", [1, 4, 9])
def test_ju06_once_the_clock_passes_2100_mid_run_the_next_update_is_refused_outside_the_chain_group(tmp_path, m):
    w, cloud, clock = started(tmp_path, at=sast(18, 0))
    cloud.minutes_per_update = 180 / m
    log = update(w, cloud, clock)
    assert stopped(log) == "WINDOW" and [j for j, _ in update_targets(cloud)] == ORDER[:m]
    assert log["branch"] == "hold_non_chain" and restore_targets(cloud) == []


def test_ju06_the_schema_apply_gate_refuses_outside_the_window_and_the_clock_is_injected(tmp_path):
    for hour, minute, allowed in ((8, 4, False), (8, 5, True), (20, 59, True), (21, 0, False)):
        w = jw.ReleaseWorld(tmp_path / f"s{hour}{minute}")
        w.now = sast(hour, minute)
        w.prepare()
        cloud = cw.FakeCloudRun(w.world, cw.Clock(w.now))
        if allowed:
            assert jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=lambda: w.now)["active"]["chain"] == []
        else:
            with pytest.raises(so.Stop) as stop:
                jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=lambda: w.now)
            assert stop.value.code == "WINDOW"


# JU-07

def test_ju07_a_running_or_queued_chain_execution_stops_the_snapshot_and_a_running_watchdog_does_not(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.prepare()
    cloud = cw.FakeCloudRun(w.world, cw.Clock(w.now))
    cloud.start("f42-watchdog", "live")
    snapshot = jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=lambda: w.now)
    assert snapshot["active"]["side"] == ["f42-watchdog-live0"] and snapshot["active"]["chain"] == []
    cloud.start("f42-detect", "live")
    with pytest.raises(so.Stop) as stop:
        jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=lambda: w.now)
    assert stop.value.code == "CHAIN_ACTIVE"


def test_ju07_a_chain_stage_row_for_today_that_is_still_running_stops_the_snapshot(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.prepare()
    cloud = cw.FakeCloudRun(w.world, cw.Clock(w.now))
    today = w.now.astimezone(SAST).date()
    w.quiet_rows = [{"run_id": "collect-x", "stage": "collect", "run_date": today, "status": "running", "started_at": w.now,
                     "finished_at": None}]
    with pytest.raises(so.Stop) as stop:
        jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=lambda: w.now)
    assert stop.value.code == "CHAIN_ACTIVE"
    w.quiet_rows[0]["run_date"] = today - dt.timedelta(days=1)
    assert jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=lambda: w.now)["chain_rows"]


def test_ju07_the_snapshot_records_the_latest_row_version_per_stage_and_day_and_is_written_to_the_evidence_folder(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.prepare()
    cloud = cw.FakeCloudRun(w.world, cw.Clock(w.now))
    today = w.now.astimezone(SAST).date()
    w.quiet_rows = [{"run_id": "detect-a", "stage": "detect", "run_date": today, "status": "running", "started_at": w.now - dt.timedelta(hours=9),
                     "finished_at": None},
                    {"run_id": "detect-a", "stage": "detect", "run_date": today, "status": "ok", "started_at": w.now - dt.timedelta(hours=9),
                     "finished_at": w.now - dt.timedelta(hours=8)}]
    snapshot = jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=lambda: w.now)
    assert snapshot["chain_rows"][f"detect:{today.isoformat()}"] == {"run_id": "detect-a", "status": "ok", "version": 2}
    assert [p.name for p in w.evidence.glob("quiet-snapshot-*.json")] == ["quiet-snapshot-01.json"]


def test_ju07_a_first_snapshot_older_than_15_minutes_stops_the_update_and_14_minutes_does_not(tmp_path):
    w, cloud, clock = started(tmp_path)
    clock.advance(minutes=14, seconds=59)
    assert update(w, cloud, clock)["complete"] is True
    w2, cloud2, clock2 = started(tmp_path / "late")
    clock2.advance(minutes=15, seconds=1)
    with pytest.raises(so.Stop) as stop:
        update(w2, cloud2, clock2)
    assert stop.value.code == "STALE_SNAPSHOT" and cloud2.update_calls == 0


def test_ju07_the_update_reads_the_snapshot_again_after_the_words_and_stops_on_a_new_chain_execution(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.world.executions["f42-collect-new"] = jw.execution_raw("f42-collect-new", "f42-collect", (clock.now() + dt.timedelta(seconds=30)).isoformat(),
                                                                  (clock.now() + dt.timedelta(minutes=1)).isoformat(), OLD)
    with pytest.raises(so.Stop) as stop:
        update(w, cloud, clock)
    assert stop.value.code == "CHAIN_ACTIVE" and "changed" in stop.value.message and cloud.update_calls == 0


def test_ju07_a_chain_execution_that_is_running_at_the_second_read_stops_the_update(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.start("f42-understand", "live")
    with pytest.raises(so.Stop) as stop:
        update(w, cloud, clock)
    assert stop.value.code == "CHAIN_ACTIVE" and cloud.update_calls == 0


def test_ju07_the_second_read_comes_before_the_first_update_call(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.argv_calls.clear()
    update(w, cloud, clock)
    first_update = next(i for i, a in enumerate(cloud.argv_calls) if a[3] == "update")
    reads = [a for a in cloud.argv_calls[:first_update] if a[3:5] == ["executions", "list"]]
    assert len(reads) == 14


def test_ju07_the_update_without_a_first_snapshot_in_the_before_readback_is_refused(tmp_path):
    w, cloud, clock = started(tmp_path)
    for path in (w.release_dir / "readbacks").glob("BeforeJobsUpdate-*.json"):
        path.unlink()
    with pytest.raises(so.Stop) as stop:
        update(w, cloud, clock)
    assert stop.value.code == "STALE_SNAPSHOT" and cloud.update_calls == 0


# JX-05

@pytest.mark.parametrize("index", [3, 10, 13])
def test_jx05_restore_is_idempotent_and_no_branch_touches_a_service(tmp_path, index):
    w, cloud, clock = started(tmp_path)
    services = cloud.services_bytes()
    cause(cloud, clock, "exit", index)
    update(w, cloud, clock)
    assert cloud.services_bytes() == services
    cloud.fail.clear()
    first = rollback(w, cloud, clock)
    assert first["complete"] is True and all(v == OLD for v in digests(cloud).values())
    cloud.argv_calls.clear()
    second = rollback(w, cloud, clock)
    assert second["complete"] is True and update_targets(cloud) == [] and second["updated"] == []
    assert cloud.services_bytes() == services
    assert not any(a[2:3] == ["services"] or "services" in a[:4] for a in cloud.argv_calls)


def test_jx05_the_rollback_runs_collect_first_and_the_watchdog_last_with_the_rollback_digest_only(tmp_path):
    w, cloud, clock = started(tmp_path)
    update(w, cloud, clock)
    cloud.argv_calls.clear()
    log = rollback(w, cloud, clock)
    assert update_targets(cloud) == [(job, OLD) for job in reversed(ORDER)] and log["complete"] is True


# JX-07

def test_jx07_a_chain_start_injected_inside_the_window_makes_the_schema_apply_gate_and_before_jobs_update_stop(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.prepare()
    clock = cw.Clock(w.now)
    cloud = cw.FakeCloudRun(w.world, clock)
    w.fake_reader = cloud
    cloud.injections = {("before", 0): ["f42-collect"]}
    cloud.inject(("before", 0))
    with pytest.raises(so.Stop) as stop:
        jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=clock.now)
    assert stop.value.code == "CHAIN_ACTIVE"
    w.run("BeforeAnyWrite", reader=cloud)
    w.built()
    assert w.stop("BeforeJobsUpdate", reader=cloud).code == "CHAIN_ACTIVE"


def test_jx07_a_live_chain_makes_before_jobs_rollback_stop_and_a_finished_one_does_not(tmp_path):
    w, cloud, clock = started(tmp_path)
    name = cloud.start("f42-brief")
    assert w.stop("BeforeJobsRollback", reader=cloud).code == "CHAIN_ACTIVE"
    cloud.world.executions[name]["status"]["completionTime"] = (clock.now() + dt.timedelta(minutes=1)).isoformat()
    assert w.run("BeforeJobsRollback", reader=cloud)["phase"] == "BeforeJobsRollback"


def test_jx07_the_orchestrators_own_rollback_also_refuses_while_a_chain_execution_is_live(tmp_path):
    w, cloud, clock = started(tmp_path)
    update(w, cloud, clock)
    cloud.start("f42-detect", "live")
    cloud.argv_calls.clear()
    with pytest.raises(so.Stop) as stop:
        rollback(w, cloud, clock)
    assert stop.value.code == "CHAIN_ACTIVE" and update_targets(cloud) == []


def test_jx07_window_is_proven_in_the_world_outside_it_the_phase_and_the_gate_stop(tmp_path):
    w, cloud, clock = started(tmp_path)
    assert w.stop("BeforeJobsUpdate", reader=cloud, now=sast(21, 0)).code in ("WINDOW", "CANDIDATE_AGE")


# the real call log of the orchestrator, judged by the positive list

def test_ju02_every_call_the_orchestrator_made_in_every_branch_matches_exactly_one_entry(tmp_path):
    w, cloud, clock = started(tmp_path)
    cause(cloud, clock, "exit", 11)
    update(w, cloud, clock)
    cloud.fail.clear()
    rollback(w, cloud, clock)
    jr.run_snapshot(w.bound, cloud, w.bq_client(), w.evidence, now=clock.now)
    assert len(cloud.argv_calls) > 60
    for argv in cloud.argv_calls:
        assert len(plan.jobs_matching_entries(argv)) == 1, argv
    assert {tuple(a[1:4]) for a in cloud.argv_calls} <= {("run", "jobs", "describe"), ("run", "jobs", "update"), ("run", "jobs", "executions")}


def test_ju02_the_call_log_is_what_the_plan_renders_for_the_same_order(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.argv_calls.clear()
    update(w, cloud, clock)
    rendered = plan.JOBS_ACTIONS["JobsUpdate"](plan.JobsCtx(release_id=jw.B_RID, commit=jw.B_COMMIT, helper="h", runner="r", bindings="b", evidence="e",
                                                            digest=NEW, rollback_digest=OLD, image_tag="t"))
    planned = [list(s.argv) for s in rendered if s.argv[:4] == ("gcloud", "run", "jobs", "update")]
    assert [a for a in cloud.argv_calls if a[3] == "update"] == planned


def test_ju07_a_chain_execution_the_first_snapshot_already_shows_still_stops_the_second_read(tmp_path):
    """A readback edited by hand to carry a live chain execution: the same execution is in both reads, so the comparison alone
    would pass, and the second read has to be asserted quiet on its own."""
    w, cloud, clock = started(tmp_path)
    cloud.start("f42-understand", "live")
    runner = jr.runner_for(w.bound, w.bq_client())
    loud = jo.quiet_snapshot(cloud, runner, clock.now())
    path = sorted((w.release_dir / "readbacks").glob("BeforeJobsUpdate-*.json"))[-1]
    data = json.loads(path.read_text(encoding="utf-8"))
    data["quiet_snapshot"] = loud
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(so.Stop) as stop:
        update(w, cloud, clock)
    assert stop.value.code == "CHAIN_ACTIVE" and "running or queued" in stop.value.message and cloud.update_calls == 0
