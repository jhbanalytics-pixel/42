"""The fake Cloud Run of the update tests (W8-REL-B JX-01): 14 job definitions with a template per revision, executions that
capture the template at the moment they start, and a scheduler double that injects a start of any job before, during or after
any update. It is the adapter's `_execute`: the argv the orchestrator built, and the allowlist judged, arrives here and the fake
answers as the command line would. No method of it touches a service, so the services can be compared byte for byte.

The same object also answers the reader questions the readback phases ask (services, revisions, registry, principals), so a
test can run an orchestrator action and then a readback phase against one world.
"""
from __future__ import annotations

import datetime as dt
import json

from core.setup.release import jobs_run as jr
from core.setup.release import services_only as so
from core.setup.tests import jobs_world as jw


class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + dt.timedelta(**kw)


class FakeCloudRun(jr.JobsAdapter, jw.FakeJobsReader):
    def __init__(self, world, clock):
        jw.FakeJobsReader.__init__(self, world)
        jr.JobsAdapter.__init__(self)
        self.clock = clock
        self.rev_of = {name: 1 for name in so.JOB_NAMES}
        self.templates = {name: [self.image_of(name)] for name in so.JOB_NAMES}
        self.captured = {}
        self.started = []
        self.fail = {}
        self.apply_then_fail = set()
        self.after_apply = {}
        self.injections = {}
        self.update_calls = 0
        self.expire_credential_at = None
        self.expire_reads_at = None
        self.read_failures = {}
        self.read_failures_after_update = {}
        self.missing_jobs = set()
        self.misapply_restores = {}
        self.minutes_per_update = 0
        self.refuse_restores = False

    # what the world holds ------------------------------------------------------------------------------------------
    def image_of(self, job):
        return self.world.jobs[job]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"]

    def digest_of(self, job):
        return self.image_of(job).split("@")[1]

    def services_bytes(self):
        return json.dumps({"raw": {n: self.world.service_raw(n) for n in so.SERVICES}, "revisions": self.world.revisions,
                           "names": self.world.revision_names}, sort_keys=True).encode("utf-8")

    def update_argvs(self):
        return [a for a in self.argv_calls if a[:4] == ["gcloud", "run", "jobs", "update"]]

    def start(self, job, tag="inj"):
        name = f"{job}-{tag}{len(self.started)}"
        digest = self.digest_of(job)
        self.world.executions[name] = jw.execution_raw(name, job, self.clock.now().isoformat(), None, digest, succeeded=0)
        self.captured[name] = (self.rev_of[job], digest)
        self.started.append({"name": name, "job": job, "revision": self.rev_of[job], "digest": digest, "at": self.clock.now()})
        return name

    def inject(self, key):
        for job in self.injections.get(key, []):
            self.start(job)

    # gcloud --------------------------------------------------------------------------------------------------------------
    def _execute(self, argv):
        tail = argv[1:]
        if tail[:3] == ["run", "jobs", "update"]:
            return self.update(tail[3], tail[5])
        if self.expire_reads_at is not None and self.update_calls >= self.expire_reads_at:
            return 1, "ERROR: credential expired"
        if tail[:3] == ["run", "jobs", "describe"]:
            if tail[3] in self.missing_jobs:
                return 1, "NOT_FOUND"
            if self.read_failures.get(tail[3]):
                self.read_failures[tail[3]] -= 1
                return 1, "ERROR: the read did not complete"
        if tail[:3] == ["run", "jobs", "describe"]:
            return 0, json.dumps(self.world.jobs[tail[3]])
        if tail[:4] == ["run", "jobs", "executions", "list"]:
            return 0, json.dumps([e for e in self.world.executions.values() if e["metadata"]["labels"].get(jw.JOB_LABEL) == tail[5]])
        if tail[:4] == ["run", "jobs", "executions", "describe"]:
            return (0, json.dumps(self.world.executions[tail[4]])) if tail[4] in self.world.executions else (1, "NOT_FOUND")
        raise AssertionError(f"the fake does not play {argv}")

    def update(self, job, reference):
        k = self.update_calls
        self.update_calls += 1
        restoring = reference.endswith(jw.ROLLBACK_DIGEST)
        self.inject(("before", k))
        if self.expire_credential_at is not None and k >= self.expire_credential_at:
            return 1, "credential expired"
        if restoring and self.refuse_restores:
            return 1, "refused"
        if self.fail.get(job) and not restoring and job not in self.apply_then_fail:
            return self.fail[job], "failed"
        self.inject(("during", k))
        if restoring and job in self.misapply_restores:
            reference = jw.image(self.misapply_restores[job])
        self.world.jobs[job]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = reference
        if job in self.read_failures_after_update and not restoring:
            self.read_failures[job] = self.read_failures.get(job, 0) + self.read_failures_after_update.pop(job)
        self.rev_of[job] += 1
        self.templates[job].append(reference)
        if job in self.after_apply and not restoring:
            self.after_apply[job](self.world.jobs[job])
        self.clock.advance(minutes=self.minutes_per_update)
        self.inject(("after", k))
        if job in self.apply_then_fail and not restoring:
            return self.fail.get(job, 1), "failed after applying"
        return 0, "ok"
