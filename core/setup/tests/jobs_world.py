"""A simulated Cloud Run and BigQuery world for the Release B tests (W8-REL-B v2.1, no cloud, no network).

The services side is release_world.World walked through Release A's AfterPromotion, so the two services stand in the post-A
state baseline-J pins. On top of it sit the 14 job definitions, Cloud Run executions in the v1 shape `run jobs executions
describe --format=json` returns, the agent.runs and output rows a normal chain leaves behind, and a fake BigQuery client that
records every query. A test builds a ChainFixture, changes one thing, and runs the producer or a readback.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path

from core.setup.release import jobs_only as jo
from core.setup.release import services_only as so
from core.setup.tests import release_world as rw

UTC = dt.timezone.utc
B_COMMIT = "b5e1a2c4d6f8091a2b3c4d5e6f708192a3b4c5d6"
B_TREE = "7f3a9c1e5b2d8046a1c3e5f7092b4d6f8a0c2e41"
B_RID = "rel-b5e1a2c-01"
A_RID = rw.RID
ROLLBACK_DIGEST = "sha256:e77c3819cc688d7690d4370a5fd575026e4d3e62c0ab322d54b6eabb515c75de"
NEW_DIGEST = "sha256:" + "b2" * 32
OLD_DIGEST = "sha256:17ad022e" + "0" * 56
RUN_DATE = dt.date(2026, 10, 10)
A_TERMINAL_AT = "2026-10-09T10:00:00+00:00"
CALLER = rw.CALLER
JOB_LABEL = "run.googleapis.com/job"
MARKETS = ("ZA", "NG", "KE")
CLUSTER_MARKETS = ("ZA", "NG", "KE", "pan")
SUBSTAGES = ("aggregate", "stats", "coaction", "breakout", "watch", "seeds", "forecast")
PARSE_JSON_ERROR = "ValueError: PARSE_JSON cannot round-trip through string representation of a float"


def utc(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def image(digest):
    return f"{jo.JOBS_REPO}@{digest}"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_frozen_manifest(release_dir, digest, **over):
    manifest = {"schema_version": 1, "kind": "jobs-release", "release_id": B_RID,
                "image": {"repository": jo.JOBS_REPO, "registry_digest": digest, "digest": digest, "reference": image(digest)}}
    manifest.update(over)
    manifest["manifest_sha256"] = so.manifest_hash(manifest)
    write_json(Path(release_dir) / "release-manifest.json", manifest)
    (Path(release_dir) / "release-manifest.sha256").write_text(manifest["manifest_sha256"] + "\n", encoding="utf-8")
    return manifest


def set_job_image(world, name, digest):
    world.jobs[name]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["image"] = image(digest)


def set_all_jobs_image(world, digest):
    for name in so.JOB_NAMES:
        set_job_image(world, name, digest)


def a_promoted_world(tmp_path):
    """The services after Release A's AfterPromotion and the 14 jobs still on the rollback digest."""
    scenario = rw.Scenario(tmp_path / "a-scenario")
    scenario.to("AfterPromotion")
    set_all_jobs_image(scenario.world, ROLLBACK_DIGEST)
    return scenario.world


def execution_raw(name, job, start, completion, digest, *, succeeded=1, failed=0, cancelled=0, retried=None, with_digest=True):
    ref = image(digest) if with_digest else f"{jo.JOBS_REPO}:tag-only"
    status = {"startTime": start, "succeededCount": succeeded, "failedCount": failed, "cancelledCount": cancelled}
    if completion:
        status["completionTime"] = completion
    if retried is not None:
        status["retriedCount"] = retried
    return {"metadata": {"name": name, "labels": {JOB_LABEL: job}},
            "spec": {"taskCount": 1, "template": {"spec": {"containers": [{"image": ref}]}}}, "status": status}


class FakeJobsReader(rw.FakeReader):
    """release_world.FakeReader plus the two execution reads. argv_log holds the literal argv of each read."""

    def __init__(self, world):
        super().__init__(world)
        self.argv_log = []

    def _argv(self, *argv):
        self.argv_log.append(list(argv))

    def config(self):
        self._argv("config", "list")
        return super().config()

    def service(self, name):
        self._argv("run", "services", "describe", name)
        return super().service(name)

    def revision(self, name):
        self._argv("run", "revisions", "describe", name)
        return super().revision(name)

    def revisions(self, service):
        self._argv("run", "revisions", "list", "--service", service)
        return super().revisions(service)

    def job(self, name):
        self._argv("run", "jobs", "describe", name)
        return super().job(name)

    def executions(self, job):
        self._argv("run", "jobs", "executions", "list", "--job", job)
        self.calls.append(("executions", job))
        if getattr(self.world, "executions_unreadable", False):
            raise so.Probe("A read exceeded its bound timeout")
        return [copy.deepcopy(e) for e in self.world.executions.values() if e["metadata"]["labels"].get(JOB_LABEL) == job]

    def execution(self, name):
        self._argv("run", "jobs", "executions", "describe", name)
        self.calls.append(("execution", name))
        if getattr(self.world, "executions_unreadable", False):
            raise so.Probe("A read exceeded its bound timeout")
        if name not in self.world.executions:
            raise so.NotFound(name)
        raw = copy.deepcopy(self.world.described.get(name, self.world.executions[name]))
        return raw


class FakeBq:
    """Records every query. dry_run answers an estimate, run answers the rows the fixture holds for that template."""

    def __init__(self, tables, estimate=1000, billed=900):
        self.tables, self.estimate, self.billed, self.log = tables, estimate, billed, []

    def dry_run(self, sql, params):
        self.log.append(("dry_run", sql, dict(params)))
        return self.estimate

    def run(self, sql, params, max_bytes):
        self.log.append(("run", sql, dict(params), max_bytes))
        from core.setup.release import chain_evidence as ce

        name = next(n for n, text in {**ce.TEMPLATES, **jo.QUIET_TEMPLATES}.items() if text == sql)
        handler = self.tables[name]
        return (handler(params) if callable(handler) else copy.deepcopy(handler)), self.billed


class ChainFixture:
    """One normal chain on run date RUN_DATE, on the bound digest, after Release A. Every part can be changed before the
    producer runs; rows are plain dicts as BigQuery would return them (counts as JSON text)."""

    # minutes after SAST midnight at which each stage starts, and how long each runs
    SCHEDULED = {"collect": 120, "understand": 161, "detect": 191, "brief": 221}
    LENGTH = {"collect": 40, "understand": 30, "detect": 29, "brief": 49}

    def __init__(self, tmp_path, *, digest=ROLLBACK_DIGEST, role="baseline", day=RUN_DATE, schedule=None, length=None):
        self.SCHEDULED, self.LENGTH = {**self.SCHEDULED, **(schedule or {})}, {**self.LENGTH, **(length or {})}
        self.tmp = Path(tmp_path)
        self.digest, self.role, self.day = digest, role, day
        self.midnight = dt.datetime(day.year, day.month, day.day, tzinfo=UTC) - dt.timedelta(hours=2)
        self.stamp = day.strftime("%Y%m%d")
        self.world = a_promoted_world(self.tmp)
        self.world.executions, self.world.described = {}, {}
        self.rows = []
        self.counts = {"collect": {"posts": 1200}, "understand": self.understand_counts(), "detect": {"items": 300}, "brief": {"cards": 12}}
        self.outputs = {"item_state": 300, "series_test": 40, "briefs": {m: 1 for m in MARKETS}, "item_locality": None}
        self.locality_table = False
        self.substage_status = {s: "ok" for s in SUBSTAGES}
        self.bound = None
        for stage in jo.CHAIN_STAGES:
            self.add_stage(stage)
        for index, sub in enumerate(SUBSTAGES):
            self.rows.append(self.substage_row(sub, index))
        for index in range(3):
            self.side_execution("f42-watchdog", f"f42-watchdog-w{index}", self.midnight + dt.timedelta(minutes=600 + 60 * index))
        self.skipped("brief")

    @staticmethod
    def understand_counts(**over):
        counts = {"embedded": 900, "index": "created_or_exists", "partial": False,
                  "cluster": {m: {"clusters": 14, "net_failed": False} for m in CLUSTER_MARKETS}}
        counts.update(over)
        return counts

    # building blocks ----------------------------------------------------------------------------------------------
    def times(self, stage):
        start = self.midnight + dt.timedelta(minutes=self.SCHEDULED[stage], seconds=5)
        return start, start + dt.timedelta(minutes=self.LENGTH[stage])

    def add_stage(self, stage, *, run_id=None, execution=None, replace=False, exec_digest=None, job=None, with_execution=True,
                  start_exec=None, completion_exec=None, terminal=True, status="ok", counts=None, **exec_over):
        started, finished = self.times(stage)
        run_id = run_id or f"{stage}-{self.stamp}-aaaaaaaaaaaa"
        execution = execution or f"f42-{stage}-{run_id[-5:]}"
        row_counts = dict(counts if counts is not None else self.counts[stage])
        running_counts = {"execution": execution} if execution else None
        self.rows.append({"run_id": run_id, "stage": stage, "status": "running", "started_at": started + dt.timedelta(seconds=15),
                          "finished_at": None, "counts": json.dumps(running_counts) if running_counts else None, "has_error": False,
                          "error_sha256": sha("")})
        if terminal:
            self.rows.append({"run_id": run_id, "stage": stage, "status": status, "started_at": started + dt.timedelta(seconds=15),
                              "finished_at": finished, "counts": json.dumps(row_counts), "has_error": status != "ok",
                              "error_sha256": sha("" if status == "ok" else "x")})
        if with_execution and execution and execution not in self.world.executions:
            self.world.executions[execution] = execution_raw(
                execution, job or jo.STAGE_JOB[stage], (start_exec or started).isoformat(),
                (completion_exec or finished + dt.timedelta(seconds=10)).isoformat(), exec_digest or self.digest, **exec_over)
        return run_id, execution

    def substage_row(self, sub, index):
        at = self.midnight + dt.timedelta(minutes=330, seconds=index)
        return {"run_id": f"{sub}-{self.stamp}-bbbbbbbbbbbb", "stage": sub, "status": self.substage_status.get(sub, "ok"), "started_at": at,
                "finished_at": at, "counts": json.dumps({"rows": 5}), "has_error": False, "error_sha256": sha("")}

    def skipped(self, stage, run_id="s", minutes=375):
        """A skipped_duplicate row and the execution it came from: the 06:15 SAST start of the brief on a published day."""
        at = self.midnight + dt.timedelta(minutes=minutes)
        self.rows.append({"run_id": f"{stage}-{self.stamp}-{run_id}", "stage": stage, "status": "skipped_duplicate", "started_at": at,
                          "finished_at": at, "counts": None, "has_error": True, "error_sha256": sha("already ran ok")})
        self.side_execution(jo.STAGE_JOB[stage], f"{jo.STAGE_JOB[stage]}-skip-{run_id}", at)

    def side_execution(self, job, name, start, digest=None, **over):
        self.world.executions[name] = execution_raw(name, job, start.isoformat(), (start + dt.timedelta(seconds=20)).isoformat(),
                                                    digest or self.digest, **over)

    def stage_rows(self, stage):
        return [r for r in self.rows if r["stage"] == stage]

    def set_terminal(self, stage, **changes):
        terminal = [r for r in self.stage_rows(stage) if r["status"] not in ("running", "skipped_duplicate")][-1]
        terminal.update(changes)
        return terminal

    def set_counts(self, stage, **changes):
        terminal = [r for r in self.stage_rows(stage) if r["status"] not in ("running", "skipped_duplicate")][-1]
        counts = json.loads(terminal["counts"])
        counts.update(changes)
        terminal["counts"] = json.dumps(counts)

    def set_cluster_error(self, market, text=PARSE_JSON_ERROR):
        terminal = [r for r in self.stage_rows("understand") if r["status"] not in ("running", "skipped_duplicate")][-1]
        counts = json.loads(terminal["counts"])
        counts["cluster"][market] = {"error": text}
        terminal["counts"] = json.dumps(counts)

    # the two things the producer talks to --------------------------------------------------------------------------
    def reader(self):
        return FakeJobsReader(self.world)

    def tables(self):
        out = {}

        def stage_rows(params):
            return [dict(r) for r in self.rows if r["stage"] in params["stages"]]

        out["stage_rows"] = stage_rows
        out["item_state_count"] = lambda params: [{"n": self.outputs["item_state"] if params["run_id"].startswith("detect") else 0}]
        out["series_test_count"] = lambda params: [{"n": self.outputs["series_test"] if params["run_id"].startswith("detect") else 0}]
        out["briefs_by_market"] = lambda params: [{"market": m, "n": n} for m, n in self.outputs["briefs"].items()]
        out["item_locality_count"] = lambda params: [{"n": self.outputs["item_locality"] or 0}]
        out["table_exists"] = lambda params: [{"n": 1 if self.locality_table else 0}]
        return out

    def bq(self, **kw):
        return FakeBq(self.tables(), **kw)

    # bindings, baseline-J ---------------------------------------------------------------------------------------------
    def baseline_j(self, *, a_kind="AfterPromotion", at_utc=A_TERMINAL_AT):
        reader = FakeJobsReader(self.world)
        return {"schema_version": 1, "kind": "baseline-J", "captured_utc": "2026-10-09T10:05:00+00:00",
                "jobs": {name: so.job_view(reader.job(name)) for name in so.JOB_NAMES},
                "services": jo.live_services(reader), "aTerminal": {"kind": a_kind, "at_utc": at_utc, "a_release_id": A_RID},
                "a80Serving": dict(rw.A80_REV)}

    def write_bindings(self, **over):
        from core.setup.release import chain_evidence as ce

        baseline = over.pop("baseline", None) or self.baseline_j()
        base_sha = write_json(self.tmp / "baseline" / "baseline-J.json", baseline)
        self.release_dir = self.tmp / "releases" / B_RID
        self.release_dir.mkdir(parents=True, exist_ok=True)
        self.evidence = self.tmp / "run"
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.bound = {"schema_version": 1, "mode": "jobs", "release_id": B_RID, "target": B_COMMIT, "tree": B_TREE,
                      "releaseDir": str(self.release_dir), "baselinePath": str(self.tmp / "baseline" / "baseline-J.json"),
                      "baselineSha256": base_sha, "rollbackJobsDigest": ROLLBACK_DIGEST, "callerAccount": CALLER,
                      "readTimeoutSeconds": {"gcloud": 120, "http": 30}, "chainEvidenceBytesCap": 64 * 1024 * 1024,
                      "templateHashes": ce.template_hashes(), "collectStartToleranceMinutes": 10}
        self.bound.update(over)
        return self.bound

    def freeze(self, digest):
        """The release manifest FreezeJobs leaves in the release directory: the frozen digest, hashed, with its sidecar."""
        return write_frozen_manifest(self.release_dir, digest)

    def manifest_path(self, run_date=RUN_DATE):
        return self.release_dir / f"chain-evidence-{run_date.isoformat()}.json"


QUIET_HASH_FIELD = "quietTemplateHashes"
SAST = dt.timezone(dt.timedelta(hours=2), "SAST")


class ReleaseWorld(ChainFixture):
    """The world a release action sees on the day of JobsUpdate: a produced baseline chain manifest of the day before, the
    manifest FreezeJobs froze, the registry tag, and a clock at 10:00 SAST. A test changes one thing and runs a phase."""

    def __init__(self, tmp_path, **kw):
        super().__init__(tmp_path, **kw)
        self.now = dt.datetime(2026, 10, 11, 8, 0, tzinfo=UTC)
        self.fake_reader = None
        self.quiet_rows = []

    def prepare(self, *, baseline=None, **over):
        """Bindings with every release key. The baseline chain manifest is produced by the producer itself."""
        from core.setup.release import chain_evidence as ce

        bound = self.write_bindings(**({"baseline": baseline} if baseline else {}))
        manifest = ce.build_manifest(bound, self.reader(), self.bq(), self.day, "baseline", now=lambda: self.now)
        path = ce.write_manifest(self.release_dir, manifest)
        bound.update({"baselineChainPath": str(path), "baselineChainSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "buildServiceAccount": rw.BUILD_SA, "buildConfigSha256": "c1" * 32, "dockerfileSha256": "d1" * 32,
                      "durableManifestSha256": "d2" * 32, "dryRunReceiptSha256": "d3" * 32,
                      "window": {"startSast": "08:05", "endSast": "21:00", "rollbackDeadlineSast": "23:30"},
                      "maxBaselineAgeDays": 2, "maxCandidateAgeHours": 12, "priorAttempts": [], "priorLedgers": [],
                      QUIET_HASH_FIELD: jo.quiet_template_hashes()})
        bound.update(over)
        self.freeze(NEW_DIGEST)
        self.fake_reader = FakeJobsReader(self.world)
        return bound

    def built(self):
        """The build ran: the registry now resolves the attempt tag to the digest FreezeJobs froze."""
        self.world.registry[jo.image_tag(self.bound)] = NEW_DIGEST

    def bq_client(self, **kw):
        client = FakeBq(self.tables(), **kw)
        return client

    def tables(self):
        out = super().tables()
        out["chain_rows"] = lambda params: [dict(r) for r in self.quiet_rows]
        return out

    def run(self, phase, *, now=None, bq=None, reader=None):
        from core.setup.release import jobs_only

        return jobs_only.run_phase(self.bound, phase, reader or self.fake_reader, self.evidence, now=lambda: now or self.now,
                                   bq=bq or self.bq_client(), sleep=lambda s: None)

    def stop(self, phase, **kw):
        try:
            self.run(phase, **kw)
        except so.Stop as error:
            return error
        raise AssertionError(f"{phase} did not stop")

    def update(self, names, digest=NEW_DIGEST):
        for name in names:
            set_job_image(self.world, name, digest)

    def update_prefix(self, count, digest=NEW_DIGEST):
        self.update(jo.UPDATE_ORDER[:count], digest)
