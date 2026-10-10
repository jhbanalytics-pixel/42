"""The update path of Release B (W8-REL-B v2.1 3.4 to 3.7, 3.9): the quiet snapshot, the 14 job updates and the rollback.

    py -3.13 core/setup/release/jobs_run.py snapshot|update|rollback --bindings <file> --evidence <run dir>

The release paste asks for the typed words and calls this program for the parts that need a decision between two commands: the
second quiet snapshot, the window before each update, the readback after each one and, inside the chain group, the automatic
restore. It lives here, not in the paste, so the same code is proven against a fake Cloud Run (core/setup/tests/cloud_world.py)
and every command it issues passes the positive list of plan.py (JOBS_ALLOWED) on the acting path, not only on the rendering path.

Exit 0 when the action completed, 1 when it stopped (the run log says where, and which declared branch applies), 3 when a read
could not complete. The only write it can make is `gcloud run jobs update <job> --image <repo>@sha256:<digest>`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.setup.release import jobs_only as jo  # noqa: E402
from core.setup.release import plan  # noqa: E402
from core.setup.release import services_only as so  # noqa: E402
from core.setup.release.services_only import Probe, Stop, require  # noqa: E402

HOLD_TEXT = ("hold: re-run JobsUpdate to finish the set by 21:00 SAST; if it cannot be finished, undo it with JobsRollback before "
             "23:30 SAST (both are operator actions, no chain job has changed)")
CHAIN_TEXT = ("the chain group was restored automatically and the ten non-chain jobs are new; finish the set with JobsUpdate by 21:00 SAST "
              "or undo it with JobsRollback before 23:30 SAST")
UNRESTORED_TEXT = ("CHAIN_PREFIX_UNRESTORED: a chain job is still on the new image; run JobsRollback before 23:30 SAST, which completes "
                   "the restore")


TOKEN_NAME = "update-token.json"
TOKEN_MAX_AGE_SECONDS = 60
TOKEN_FUTURE_SKEW_SECONDS = 5


def stdin_is_console():
    """True only when standard input is a real console. On Windows isatty() is also true for the NUL device, so the console mode of
    the handle is asked for, which only a console input buffer answers."""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt
        from ctypes import wintypes

        mode = wintypes.DWORD()
        return bool(ctypes.windll.kernel32.GetConsoleMode(wintypes.HANDLE(msvcrt.get_osfhandle(sys.stdin.fileno())), ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):
        return False


def consume_update_token(bound, evidence, wall):
    """The per-run token the paste writes into its run folder after DEPLOY is typed (RB-T2). The update verb is a write entry point,
    so it runs only when that file is there, names this release, holds a real token value and was written under a minute ago by the
    wall clock; it is used up here, so a second call with the same file is refused. The age is the file system's, not the run's own
    clock and not a time the file states about itself."""
    path = Path(evidence) / TOKEN_NAME
    require(path.is_file(), "UPDATE_TOKEN", "The paste wrote no update token for this run; the update verb is run by the paste after DEPLOY")
    age = wall() - path.stat().st_mtime
    require(-TOKEN_FUTURE_SKEW_SECONDS <= age <= TOKEN_MAX_AGE_SECONDS, "UPDATE_TOKEN", "The update token is not under a minute old")
    try:
        body = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        body = None
    require(isinstance(body, dict) and body.get("schema_version") == 1 and body.get("release_id") == bound["release_id"]
            and re.fullmatch(r"[0-9a-f]{32}", str(body.get("token"))) is not None, "UPDATE_TOKEN", "The update token is not for this release")
    path.unlink()


class JobsAdapter:
    """What the orchestrator asks of Cloud Run. Every call is built from the plan's own step builders and judged by the positive
    list before it is executed, so a command outside the list is refused here, whoever asked for it. `_execute` is the one method
    a subclass supplies: the real one runs gcloud, the test one plays a fake world."""

    def __init__(self):
        self.argv_calls = []

    def _execute(self, argv):
        raise NotImplementedError

    def _call(self, argv):
        argv = list(argv)
        require(len(plan.jobs_matching_entries(argv)) == 1, "WRITE_REFUSED", "That command is outside the positive list of the jobs actions")
        self.argv_calls.append(argv)
        return self._execute(argv)

    def _read(self, argv):
        code, out = self._call(argv)
        if code != 0:
            if "NOT_FOUND" in out or "not found" in out.lower():
                raise so.NotFound(" ".join(argv[3:6]))
            raise Probe(f"A read failed with exit {code}: {' '.join(argv[3:6])}")
        try:
            return json.loads(out)
        except ValueError:
            raise Probe("A read produced unreadable output") from None

    def job(self, name):
        return self._read(plan.job_readback(name, "read").argv)

    def executions(self, job):
        return self._read(plan.executions_list(job, "list").argv)

    def execution(self, name):
        return self._read(plan.execution_describe(name, "describe").argv)

    def update_job(self, job, digest):
        """The exit code of `jobs update --image`. A call that did not finish counts as a failure; the caller reads the definition."""
        try:
            code, _ = self._call(plan.job_update(job, digest, "update").argv)
        except Probe:
            return -1
        return code


class GcloudJobs(JobsAdapter):
    def __init__(self, timeouts):
        super().__init__()
        self.timeout = timeouts["gcloud"]

    def _execute(self, argv):
        require(argv[0] == "gcloud", "WRITE_REFUSED", "Only gcloud is run here")
        try:
            result = subprocess.run([shutil.which("gcloud") or "gcloud", *argv[1:]], stdin=subprocess.DEVNULL, capture_output=True,
                                    encoding="utf-8", errors="strict", timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise Probe("A call exceeded its bound timeout") from None
        except (UnicodeDecodeError, OSError):
            raise Probe("A call produced unreadable output") from None
        return result.returncode, (result.stdout if result.returncode == 0 else result.stderr)


def runner_for(bound, client):
    from core.setup.release.chain_evidence import BqRunner

    jo.check_quiet_templates(bound)
    return BqRunner(client, bound["chainEvidenceBytesCap"], jo.QUIET_TEMPLATES)


def numbered(folder, prefix):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{prefix}-{len(list(folder.glob(prefix + '-*.json'))) + 1:02d}.json"


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as out:
        out.write(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    return path


def run_snapshot(bound, adapter, bq_client, evidence, *, now):
    """The quiet snapshot with the window, taken before the schema apply in JobsCandidate (JU-06, JU-07). It records first and
    decides after, so a refused snapshot is still on disk."""
    jo.validate_jobs_bindings(bound)
    taken = now()
    require(jo.window_open(bound, taken), "WINDOW", "The schema apply and JobsUpdate run only inside the bound window")
    snapshot = jo.quiet_snapshot(adapter, runner_for(bound, bq_client), taken)
    write_new(numbered(evidence, "quiet-snapshot"), snapshot)
    jo.assert_quiet(snapshot)
    return snapshot


def new_log(action, now):
    return {"schema_version": 1, "action": action, "started_at": now().isoformat(), "finished_at": None, "updated": [],
            "skipped_already_new": [], "restored": [], "unrestored": [], "changed_definitions": [], "active_executions": None,
            "stopped": None, "branch": None, "codes": [], "complete": False}


def finalize(log, adapter, baseline, now, touched):
    """The changed definitions (every job this run touched, read again and compared with baseline-J) and the executions that are
    running or queued. A read that cannot complete is recorded, never allowed to hide the outcome."""
    for job in touched:
        try:
            view = so.job_view(adapter.job(job))
        except (Probe, so.NotFound):
            log["changed_definitions"].append({"job": job, "before_sha256": so.fingerprint(baseline["jobs"][job]), "after_sha256": None})
            continue
        if view != baseline["jobs"][job]:
            log["changed_definitions"].append({"job": job, "before_sha256": so.fingerprint(baseline["jobs"][job]), "after_sha256": so.fingerprint(view)})
    active = []
    try:
        for job in so.JOB_NAMES:
            active += [{"job": job, "name": e["metadata"]["name"]} for e in adapter.executions(job) if jo.is_active(e)]
        log["active_executions"] = active
    except (Probe, so.NotFound):
        log["active_executions"] = None
        log["active_executions_error"] = "unreadable"
    log["finished_at"] = now().isoformat()


def stop(log, job, code, message, text):
    log["stopped"] = {"code": code, "message": message, "job": job, "branch_text": text}
    log["codes"].append(code)


def restore_chain_group(adapter, baseline, rollback_digest, log, touched):
    """Inside the chain group an abort restores the group's prefix, collect first (the reverse of the update order), without a
    typed word. A job is restored when its definition is not baseline-J's, or cannot be read. A restore that fails is recorded."""
    live = {}
    for job in jo.CHAIN_GROUP:
        try:
            live[job] = so.job_view(adapter.job(job))
        except (Probe, so.NotFound):
            live[job] = None
    for job in reversed(jo.CHAIN_GROUP):
        if live[job] == baseline["jobs"][job]:
            continue
        if job not in touched:
            touched.append(job)
        code = adapter.update_job(job, rollback_digest)
        try:
            view = so.job_view(adapter.job(job)) if code == 0 else None
        except (Probe, so.NotFound):
            view = None
        if view is not None and view["image"] == jo.image_reference(rollback_digest):
            log["restored"].append(job)
            if jo.split_view(view) != jo.split_view(baseline["jobs"][job]):
                log.setdefault("residual_drift", []).append(job)
        else:
            log["unrestored"].append(job)


def run_schema_receipt(bound, evidence, *, now):
    """Write the schema readback receipt JobsCandidate leaves for JobsUpdate (RB-T3). The durable-effects checker's own output is read,
    and a schema effect counts only when the hash it reports is the one the pinned durable manifest expects: its `matches` flag is not
    believed. The receipt is exactly the body the manifest produces, so the hash the bindings bind can be set before the run."""
    jo.validate_jobs_bindings(bound)
    expected = jo.schema_receipt_body(bound, jo.load_durable(bound))
    digest = so.fingerprint(expected)
    require(digest == bound["schemaReadbackReceiptSha256"], "SCHEMA_RECEIPT",
            "The receipt hash the bindings bind is not the hash of the receipt this durable manifest requires")
    try:
        output = json.loads((Path(evidence) / "readbacks.json").read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        output = None
    require(isinstance(output, dict) and output.get("check") == "readbacks" and output.get("ok") is True and isinstance(output.get("results"), list),
            "SCHEMA_RECEIPT", "The durable readbacks left no passing output in the run folder")
    seen = {r["effect_id"]: r.get("result_sha256") for r in output["results"]
            if isinstance(r, dict) and r.get("matches") is True and isinstance(r.get("effect_id"), str)}
    for effect in expected["effects"]:
        require(seen.get(effect["effect_id"]) == effect["result_sha256"], "SCHEMA_RECEIPT",
                f"The readback of {effect['effect_id']} does not show the hash the pinned durable manifest expects")
    target = Path(bound["schemaReadbackReceiptPath"])
    if target.exists():
        try:
            held = json.loads(target.read_bytes().decode("utf-8-sig"))
        except ValueError:
            held = None
        require(isinstance(held, dict) and {k: v for k, v in held.items() if k not in ("receipt_sha256", "written_at")} == expected
                and held.get("receipt_sha256") == digest, "SCHEMA_RECEIPT", "A different schema readback receipt is already in the release directory")
        return target
    return write_new(target, {**expected, "receipt_sha256": digest, "written_at": now().isoformat()})


def run_update(bound, adapter, bq_client, evidence, *, now, sleep=None):
    jo.validate_jobs_bindings(bound)
    baseline = jo.load_baseline_j(bound)
    jo.check_schema_receipt(bound)
    digest = jo.frozen_digest(bound["releaseDir"])
    started = now()
    require(jo.window_open(bound, started), "WINDOW", "JobsUpdate runs only inside the bound window")
    folder = Path(bound["releaseDir"]) / "readbacks"
    found = sorted(folder.glob("BeforeJobsUpdate-*.json")) if folder.exists() else []
    first = json.loads(found[-1].read_text(encoding="utf-8")).get("quiet_snapshot") if found else None
    require(first is not None, "STALE_SNAPSHOT", "BeforeJobsUpdate left no quiet snapshot to compare with")
    age = jo.snapshot_age_minutes(first, started)
    require(0 <= age <= jo.QUIET_MAX_AGE_MINUTES, "STALE_SNAPSHOT", "The quiet snapshot is older than 15 minutes")
    second = jo.quiet_snapshot(adapter, runner_for(bound, bq_client), started)
    jo.assert_quiet(second)
    require(jo.chain_view(first) == jo.chain_view(second), "CHAIN_ACTIVE", "A chain execution or row changed since the first snapshot")

    log = new_log("update", now)
    touched = []
    try:
        for job in jo.UPDATE_ORDER:
            try:
                view = so.job_view(adapter.job(job))
            except (Probe, so.NotFound):
                stop(log, job, "READ_FAILED", f"{job} could not be read before its update", None)
                break
            base = baseline["jobs"][job]
            if view == jo.with_image(base, digest):
                log["skipped_already_new"].append(job)
                continue
            if view != base:
                stop(log, job, "JOB_DRIFT", f"{job} is neither baseline-J nor the new definition", None)
                break
            if not jo.window_open(bound, now()):
                stop(log, job, "WINDOW", "The clock passed the end of the window before this update", None)
                break
            touched.append(job)
            code = adapter.update_job(job, digest)
            if code != 0:
                stop(log, job, "UPDATE_FAILED", f"The update of {job} returned a nonzero exit", None)
                break
            try:
                jo.check_job_after_update(adapter, baseline, job, digest)
            except Stop as error:
                stop(log, job, error.code, error.message, None)
                break
            except (Probe, so.NotFound):
                stop(log, job, "READ_FAILED", f"{job} could not be read after its update", None)
                break
            log["updated"].append(job)
        else:
            log["complete"] = True
        if log["stopped"] is not None:
            job = log["stopped"]["job"]
            if job in jo.CHAIN_GROUP:
                restore_chain_group(adapter, baseline, bound["rollbackJobsDigest"], log, touched)
                if log["unrestored"]:
                    log["branch"] = "CHAIN_PREFIX_UNRESTORED"
                    log["codes"].append("CHAIN_PREFIX_UNRESTORED")
                    log["stopped"]["branch_text"] = UNRESTORED_TEXT
                else:
                    log["branch"] = "chain_group_restored"
                    log["stopped"]["branch_text"] = CHAIN_TEXT
            else:
                log["branch"] = "hold_non_chain"
                log["stopped"]["branch_text"] = HOLD_TEXT
        finalize(log, adapter, baseline, now, touched)
    except Exception as error:
        # Whatever else goes wrong after the first update call, the log of what was done is written before the error is raised.
        if log["stopped"] is None:
            stop(log, None, "UNEXPECTED", type(error).__name__, None)
        log["finished_at"] = now().isoformat()
        write_new(numbered(evidence, "jobs-update"), log)
        raise
    write_new(numbered(evidence, "jobs-update"), log)
    return log


def run_rollback(bound, adapter, evidence, *, now):
    jo.validate_jobs_bindings(bound)
    baseline = jo.load_baseline_j(bound)
    digest = bound["rollbackJobsDigest"]
    for job in jo.STAGE_JOB.values():
        require(not any(jo.is_active(e) for e in adapter.executions(job)), "CHAIN_ACTIVE",
                "A chain execution is running or queued; a rollback inside a live chain makes the reverse pairs")
    log = new_log("rollback", now)
    log["skipped_already_new"] = []
    touched = []
    try:
        for job in reversed(jo.UPDATE_ORDER):
            try:
                view = so.job_view(adapter.job(job))
            except (Probe, so.NotFound):
                stop(log, job, "READ_FAILED", f"{job} could not be read before its restore", "re-run JobsRollback before 23:30 SAST")
                break
            if view == baseline["jobs"][job]:
                log["skipped_already_new"].append(job)
                continue
            touched.append(job)
            if adapter.update_job(job, digest) != 0:
                stop(log, job, "UPDATE_FAILED", f"The restore of {job} returned a nonzero exit", "re-run JobsRollback before 23:30 SAST")
                break
            try:
                view = so.job_view(adapter.job(job))
            except (Probe, so.NotFound):
                view = None
            if view is None or view["image"] != jo.image_reference(digest):
                stop(log, job, "JOB_IMAGE", f"{job} does not run the rollback digest after its restore", "re-run JobsRollback before 23:30 SAST")
                break
            if jo.split_view(view) != jo.split_view(baseline["jobs"][job]):
                log.setdefault("residual_drift", []).append(job)
            log["updated"].append(job)
        else:
            log["complete"] = True
        finalize(log, adapter, baseline, now, touched)
    except Exception as error:
        if log["stopped"] is None:
            stop(log, None, "UNEXPECTED", type(error).__name__, None)
        log["finished_at"] = now().isoformat()
        write_new(numbered(evidence, "jobs-rollback"), log)
        raise
    write_new(numbered(evidence, "jobs-rollback"), log)
    return log


def main(argv=None, adapter_factory=None, bq_factory=None, now=None, console=None, wall=None):
    parser = argparse.ArgumentParser(description="Release B update orchestrator.")
    parser.add_argument("action", choices=plan.JOBS_RUN_ACTIONS)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    clock = now or (lambda: dt.datetime.now(dt.timezone.utc))
    if args.action == "update" and not (console or stdin_is_console)():
        print("STOP: NOT_INTERACTIVE: the update verb is run by the paste at a console, after DEPLOY; rollback and snapshot need neither",
              file=sys.stderr)
        return 1
    try:
        bound = json.loads(args.bindings.read_text(encoding="utf-8-sig"))
        jo.validate_jobs_bindings(bound)
        if args.action == "update":
            consume_update_token(bound, args.evidence, wall or time.time)
        if args.action == "schema-receipt":
            run_schema_receipt(bound, args.evidence, now=clock)
            print("schema-receipt: written")
            return 0
        adapter = (adapter_factory or GcloudJobs)(bound["readTimeoutSeconds"])
        if args.action == "rollback":
            log = run_rollback(bound, adapter, args.evidence, now=clock)
        else:
            from core.setup.release.chain_evidence import GoogleBq

            client = (bq_factory or (lambda b: GoogleBq(b["readTimeoutSeconds"])))(bound)
            if args.action == "snapshot":
                run_snapshot(bound, adapter, client, args.evidence, now=clock)
                print("snapshot: quiet")
                return 0
            log = run_update(bound, adapter, client, args.evidence, now=clock)
    except Stop as error:
        print(f"STOP: {error.code}: {error.message}", file=sys.stderr)
        return 1
    except Probe as error:
        print(f"PROBE: {error}", file=sys.stderr)
        return 3
    except (KeyError, ValueError, OSError) as error:
        print(f"STOP: BINDINGS: {type(error).__name__}", file=sys.stderr)
        return 1
    if log["stopped"] is not None:
        print(f"STOP: {log['stopped']['code']}: {log['stopped']['message']}", file=sys.stderr)
        print(log["stopped"]["branch_text"], file=sys.stderr)
        return 1
    print(f"{args.action}: complete ({len(log['updated'])} updated, {len(log['skipped_already_new'])} already in place)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
