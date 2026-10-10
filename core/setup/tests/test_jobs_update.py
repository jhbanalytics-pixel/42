"""The update path of Release B (W8-REL-B v2.1 3.4 to 3.7, JU-04 to JU-07): one orchestrator, one fake Cloud Run, the same positive
allowlist on every call it makes."""
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
from core.setup.tests.update_support import (CHAIN, NEW, OLD, ORDER, SAST, JX04_CAUSES, cause, digests, restore_targets, rollback, sast, started, stopped,
                                                  update, update_targets)


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
        w.schema_receipt()
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


# RB-T2 (F2): the update verb is a write entry point, so it asks for what the paste gives it: a console on stdin and a per-run token
# file the paste writes after DEPLOY, under a minute old, which the verb consumes. Rollback and snapshot stay wordless.

def cli(w, cloud, clock, verb, *, console, wall=None):
    path = w.tmp / "bindings-file.json"
    jw.write_json(path, w.bound)
    kwargs = {"console": lambda: console}
    if wall is not None:
        kwargs["wall"] = wall
    return jr.main([verb, "--bindings", str(path), "--evidence", str(w.evidence)], adapter_factory=lambda t: cloud,
                   bq_factory=lambda b: w.bq_client(), now=clock.now, **kwargs)


def write_token(w, *, age=1.0, **over):
    import os
    import time

    body = {"schema_version": 1, "release_id": w.bound["release_id"], "token": "a1" * 16, **over}
    path = w.evidence / jr.TOKEN_NAME
    path.write_text(json.dumps(body), encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_rb_t2_update_with_no_console_is_refused_before_any_call_even_with_a_fresh_token(tmp_path, capsys):
    w, cloud, clock = started(tmp_path)
    token = write_token(w)
    assert cli(w, cloud, clock, "update", console=False) == 1
    assert "NOT_INTERACTIVE" in capsys.readouterr().err
    assert cloud.update_calls == 0 and token.exists()


def test_rb_t2_update_with_a_console_and_no_token_is_refused_before_any_call(tmp_path, capsys):
    w, cloud, clock = started(tmp_path)
    assert cli(w, cloud, clock, "update", console=True) == 1
    assert "UPDATE_TOKEN" in capsys.readouterr().err and cloud.update_calls == 0


@pytest.mark.parametrize("age", [61.0, 600.0, -30.0])
def test_rb_t2_a_token_older_than_a_minute_or_dated_in_the_future_is_refused(tmp_path, capsys, age):
    w, cloud, clock = started(tmp_path)
    write_token(w, age=age)
    assert cli(w, cloud, clock, "update", console=True) == 1
    assert "UPDATE_TOKEN" in capsys.readouterr().err and cloud.update_calls == 0


@pytest.mark.parametrize("over", [{"release_id": "rel-0000000-01"}, {"schema_version": 2}, {"token": ""}, {"token": "short"}])
def test_rb_t2_a_token_for_another_release_or_without_a_real_token_value_is_refused(tmp_path, capsys, over):
    w, cloud, clock = started(tmp_path)
    write_token(w, **over)
    assert cli(w, cloud, clock, "update", console=True) == 1
    assert "UPDATE_TOKEN" in capsys.readouterr().err and cloud.update_calls == 0


def test_rb_t2_a_token_that_is_not_json_is_refused(tmp_path, capsys):
    w, cloud, clock = started(tmp_path)
    (w.evidence / jr.TOKEN_NAME).write_text("not json", encoding="utf-8")
    assert cli(w, cloud, clock, "update", console=True) == 1
    assert "UPDATE_TOKEN" in capsys.readouterr().err and cloud.update_calls == 0


def test_rb_t2_a_console_and_a_fresh_token_run_the_update_and_the_token_is_used_up(tmp_path):
    w, cloud, clock = started(tmp_path)
    token = write_token(w, age=30.0)
    assert cli(w, cloud, clock, "update", console=True) == 0
    assert len(update_targets(cloud)) == 14 and not token.exists()


def test_rb_t2_a_second_run_with_the_same_token_is_refused(tmp_path, capsys):
    w, cloud, clock = started(tmp_path)
    write_token(w)
    assert cli(w, cloud, clock, "update", console=True) == 0
    cloud.argv_calls.clear()
    assert cli(w, cloud, clock, "update", console=True) == 1
    assert "UPDATE_TOKEN" in capsys.readouterr().err and update_targets(cloud) == []


def test_rb_t2_the_token_age_is_judged_by_the_wall_clock_and_not_by_the_clock_of_the_run(tmp_path, capsys):
    import time

    w, cloud, clock = started(tmp_path)
    write_token(w, age=10.0)
    assert cli(w, cloud, clock, "update", console=True, wall=lambda: time.time() + 120) == 1
    assert "UPDATE_TOKEN" in capsys.readouterr().err and cloud.update_calls == 0


@pytest.mark.parametrize("verb", ["rollback", "snapshot"])
def test_rb_t2_rollback_and_snapshot_ask_for_no_console_and_no_token(tmp_path, verb):
    w, cloud, clock = started(tmp_path)
    assert cli(w, cloud, clock, verb, console=False) == 0


def test_rb_t2_the_real_command_line_with_stdin_not_a_console_refuses_the_update_before_it_reads_anything(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    done = subprocess.run([sys.executable, "-B", str(root / "core/setup/release/jobs_run.py"), "update", "--bindings", str(tmp_path / "none.json"),
                           "--evidence", str(tmp_path / "none")], stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", timeout=120)
    assert done.returncode == 1 and "NOT_INTERACTIVE" in done.stderr, (done.stdout, done.stderr)


def test_rb_t2_a_pipe_on_stdin_is_not_a_console_either(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    done = subprocess.run([sys.executable, "-B", str(root / "core/setup/release/jobs_run.py"), "update", "--bindings", str(tmp_path / "none.json"),
                           "--evidence", str(tmp_path / "none")], input="DEPLOY\n", capture_output=True, encoding="utf-8", timeout=120)
    assert done.returncode == 1 and "NOT_INTERACTIVE" in done.stderr, (done.stdout, done.stderr)


def test_rb_t2_the_console_test_is_false_for_a_redirected_stdin_in_process():
    assert jr.stdin_is_console() is False  # pytest captures stdin


# RB-T5 (F5): a read that raises Probe or NotFound inside the update loop is a READ_FAILED stop, not an escape. Inside the chain group the
# automatic restore runs, and the run log is written in every case. The credential double fails reads as well as updates.

def lapse(cloud, after_updates):
    """The credential dies once `after_updates` updates have been attempted: every update and every read after that point fails."""
    cloud.expire_credential_at = after_updates
    cloud.expire_reads_at = after_updates


def logs(w):
    return sorted(w.evidence.glob("jobs-update-*.json"))


def test_rb_t5_the_credential_double_fails_reads_as_well_as_updates(tmp_path):
    w, cloud, clock = started(tmp_path)
    lapse(cloud, 0)
    with pytest.raises(so.Probe):
        cloud.job("f42-watchdog")
    with pytest.raises(so.Probe):
        cloud.executions("f42-watchdog")
    assert cloud.update_job("f42-watchdog", NEW) != 0


def test_rb_t5_the_reviewers_input_the_credential_lapses_after_the_update_of_brief_and_the_readback_fails(tmp_path):
    w, cloud, clock = started(tmp_path)
    lapse(cloud, 11)  # ten side jobs and brief are updated; the describe that follows brief's update is the first call to fail
    log = update(w, cloud, clock)
    assert stopped(log) == "READ_FAILED" and log["stopped"]["job"] == "f42-brief" and "READ_FAILED" in log["codes"]
    assert len(logs(w)) == 1 and json.loads(logs(w)[0].read_text(encoding="utf-8"))["stopped"]["code"] == "READ_FAILED"
    assert restore_targets(cloud) == ["f42-collect", "f42-understand", "f42-detect", "f42-brief"]  # the restore was tried, in reverse order
    assert log["branch"] == "CHAIN_PREFIX_UNRESTORED" and "f42-brief" in log["unrestored"]
    assert log["active_executions"] is None and log["active_executions_error"] == "unreadable"


def test_rb_t5_a_read_that_fails_once_inside_the_chain_group_stops_restores_the_prefix_and_writes_the_log(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.read_failures["f42-detect"] = 1  # the read of detect before its update
    log = update(w, cloud, clock)
    assert stopped(log) == "READ_FAILED" and log["stopped"]["job"] == "f42-detect"
    assert log["branch"] == "chain_group_restored" and log["restored"] == ["f42-brief"] and log["unrestored"] == []
    assert digests(cloud)["f42-brief"] == OLD and len(logs(w)) == 1
    assert [j for j, _ in update_targets(cloud)] == ORDER[:11] + ["f42-brief"]


def test_rb_t5_a_failed_readback_after_an_update_inside_the_chain_group_is_a_read_failed_stop_with_the_restore(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.read_failures_after_update["f42-detect"] = 1  # the describe that follows detect's update
    log = update(w, cloud, clock)
    assert stopped(log) == "READ_FAILED" and log["stopped"]["job"] == "f42-detect"
    assert set(log["restored"]) == {"f42-detect", "f42-brief"} and digests(cloud)["f42-detect"] == OLD and digests(cloud)["f42-brief"] == OLD
    assert len(logs(w)) == 1


@pytest.mark.parametrize("index", [0, 5, 9])
def test_rb_t5_a_read_failure_outside_the_chain_group_holds_with_the_declared_branch_and_a_log(tmp_path, index):
    w, cloud, clock = started(tmp_path)
    cloud.read_failures[ORDER[index]] = 1
    log = update(w, cloud, clock)
    assert stopped(log) == "READ_FAILED" and log["stopped"]["job"] == ORDER[index] and log["branch"] == "hold_non_chain"
    assert restore_targets(cloud) == [] and [j for j, _ in update_targets(cloud)] == ORDER[:index] and len(logs(w)) == 1
    assert "21:00" in log["stopped"]["branch_text"]


def test_rb_t5_a_job_that_cannot_be_found_is_a_read_failed_stop_too(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.missing_jobs.add("f42-probe")
    log = update(w, cloud, clock)
    assert stopped(log) == "READ_FAILED" and log["stopped"]["job"] == "f42-probe" and len(logs(w)) == 1


def test_rb_t5_the_command_line_exits_one_with_the_code_and_not_three_when_a_read_fails_inside_the_loop(tmp_path, capsys):
    w, cloud, clock = started(tmp_path)
    cloud.read_failures["f42-detect"] = 1
    path = w.tmp / "bindings-file.json"
    jw.write_json(path, w.bound)
    token = w.evidence / jr.TOKEN_NAME
    token.write_text(json.dumps({"schema_version": 1, "release_id": w.bound["release_id"], "token": "a1" * 16}), encoding="utf-8")
    code = jr.main(["update", "--bindings", str(path), "--evidence", str(w.evidence)], adapter_factory=lambda t: cloud, bq_factory=lambda b: w.bq_client(),
                   now=clock.now, console=lambda: True)
    assert code == 1 and "READ_FAILED" in capsys.readouterr().err and len(logs(w)) == 1


def test_rb_t5_an_unexpected_error_inside_the_loop_still_leaves_the_run_log_and_is_raised(tmp_path):
    w, cloud, clock = started(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    cloud.after_apply["f42-probe"] = boom
    with pytest.raises(RuntimeError):
        update(w, cloud, clock)
    written = logs(w)
    assert len(written) == 1 and json.loads(written[0].read_text(encoding="utf-8"))["stopped"]["code"] == "UNEXPECTED"


def test_rb_t5_a_rollback_whose_read_fails_stops_with_read_failed_and_writes_its_log(tmp_path):
    w, cloud, clock = started(tmp_path)
    update(w, cloud, clock)
    cloud.read_failures["f42-digest"] = 1
    log = rollback(w, cloud, clock)
    assert stopped(log) == "READ_FAILED" and log["stopped"]["job"] == "f42-digest" and log["complete"] is False
    assert "23:30" in log["stopped"]["branch_text"] and len(sorted(w.evidence.glob("jobs-rollback-*.json"))) == 1
    rerun = rollback(w, cloud, clock)
    assert rerun["complete"] is True and all(v == OLD for v in digests(cloud).values())
