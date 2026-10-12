"""The prefix recovery of the fake adapter (W8-REL-B v2.1 3.6, JX-01 to JX-05 and JX-07): for every prefix of the 14 updates the mixed set,
the automatic restore, the idempotent rollback and the services left byte for byte as they were."""
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


def test_jx02_a_start_during_an_update_keeps_the_template_it_started_with(tmp_path):
    w, cloud, clock = started(tmp_path)
    cloud.injections = {("during", 0): ["f42-watchdog"], ("before", 0): ["f42-watchdog"], ("after", 0): ["f42-watchdog"]}
    update(w, cloud, clock)
    before, during, after = cloud.started
    assert (before["digest"], during["digest"], after["digest"]) == (OLD, OLD, NEW)
    assert jo.execution_view(cloud.world.executions[during["name"]])["image_digest"] == OLD
    assert cloud.digest_of("f42-watchdog") == NEW


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
