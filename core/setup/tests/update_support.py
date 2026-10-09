"""Helpers shared by the update tests (W8-REL-B JU-04 to JU-07 and JX-01 to JX-07): the world at the moment the operator has typed
the words, the orchestrator calls, and the failure causes."""
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


def stopped(log):
    return log["stopped"]["code"] if log["stopped"] else None


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
