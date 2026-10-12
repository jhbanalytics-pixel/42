"""A prior jobs release on disk, for the tests of the Release D tooling (core/setup/tests/test_jobs_prior.py).

PriorPacket writes the files of a finished jobs release the way Release B left them in its packet directory: the bindings, the lock that
hashes them, the independent review of that lock, the release manifest FreezeJobs froze with its sidecar, the FreezeJobs and AfterJobsUpdate
readbacks, and the log of the JobsUpdate run. The values are literals of this file (the digest is the one Release B went live on), never
read from the code that derives a record from them. The end to end test of Release D does not use this class: it derives its record from
the packet the real paste wrote.

record_for_world writes the record `prior-jobs-release` would give for a live world whose fourteen jobs are the prior release's, so a
test can bind a release to a prior without first making a prior packet.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from core.setup.release import chain_evidence as ce
from core.setup.release import jobs_only as jo
from core.setup.release import services_only as so
from core.setup.tests import jobs_world as jw

RID = "rel-89d4629-01"
COMMIT = "89d4629295bd6243149352c55ba42937bbddff47"
TREE = "35d2342d604029af71cec7a63664482013353047"
DIGEST = jw.B_LIVE_DIGEST
TERMINAL_AT = "2026-10-11T14:56:41.844177+00:00"
UPDATE_FOLDER = "runs/20261011T144348-JobsUpdate"
UPDATE_LOG = "jobs-update-01.json"


def sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def definitions():
    """What the prior release's JobsUpdate log says each job's definition was after the update: a fingerprint per job."""
    return {job: sha_text(f"definition of {job} after {RID}") for job in so.JOB_NAMES}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return sha_file(path)


class PriorPacket:
    def __init__(self, tmp, **over):
        self.root = Path(tmp) / "prior"
        self.packet = self.root / "packet"
        self.lock_dir = self.root / "lock"
        self.release_id, self.commit, self.digest = over.get("release_id", RID), over.get("commit", COMMIT), over.get("digest", DIGEST)
        self.terminal_at = over.get("terminal_at", TERMINAL_AT)
        self.definitions = over.get("definitions") or definitions()
        self.write()

    # where each file lies
    @property
    def bindings_path(self):
        return self.packet / "live-bindings.json"

    @property
    def lock_path(self):
        return self.lock_dir / f"PACKET-LOCK-{self.release_id}.json"

    @property
    def review_path(self):
        return self.packet / "independent-review.json"

    @property
    def manifest_path(self):
        return self.packet / "release-manifest.json"

    @property
    def sidecar_path(self):
        return self.packet / "release-manifest.sha256"

    def readback_path(self, phase, number=1):
        return self.packet / "readbacks" / f"{phase}-{number:02d}.json"

    @property
    def update_log_path(self):
        return self.packet / UPDATE_FOLDER / UPDATE_LOG

    # the files, in the order a release makes them
    def bindings(self):
        h = sha_text("bound file")
        return {
            "schema_version": 1, "mode": "jobs", "status": "BOUND_FOR_INDEPENDENT_REVIEW", "release_id": self.release_id, "target": self.commit,
            "tree": TREE, "releaseDir": str(self.packet), "baselinePath": str(self.root / "baseline-J.json"), "baselineSha256": h,
            "rollbackJobsDigest": jw.ROLLBACK_DIGEST, "callerAccount": jw.CALLER, "readTimeoutSeconds": {"gcloud": 120, "http": 30},
            "chainEvidenceBytesCap": 64 * 1024 * 1024, "templateHashes": ce.template_hashes(), "collectStartToleranceMinutes": 10,
            "baselineChainPath": str(self.packet / "chain.json"), "baselineChainSha256": h, "buildServiceAccount": "projects/p/serviceAccounts/b@p.iam",
            "buildConfigSha256": h, "dockerfileSha256": h, "durableManifestSha256": h, "durableManifestPath": str(self.packet / "durable.json"),
            "dryRunReceiptSha256": h, "dryRunReceiptPath": str(self.packet / "dry-run.log"),
            "schemaReadbackReceiptPath": str(self.packet / "schema-readback-receipt.json"), "schemaReadbackReceiptSha256": h,
            "quietTemplateHashes": jo.quiet_template_hashes(), "window": dict(jo.PINNED_WINDOW), "maxBaselineAgeDays": 2, "maxCandidateAgeHours": 12,
            "priorAttempts": [], "priorLedgers": [],
        }

    def manifest(self):
        reference = f"{jo.JOBS_REPO}@{self.digest}"
        value = {"schema_version": 1, "kind": "jobs-release-manifest",
                 "source": {"commit": self.commit, "attempt": 1, "release_id": self.release_id},
                 "image": {"repository": jo.JOBS_REPO, "tag": f"{self.commit[:12]}-01", "digest": self.digest, "registry_digest": self.digest,
                           "reference": reference, "build_id": "003a131f-727c-41ba-bbc2-c7ba5ac9d186"},
                 "frozen_utc": "2026-10-11T14:23:50.676925+00:00"}
        value["manifest_sha256"] = so.manifest_hash(value)
        return value

    def write_manifest(self, value):
        write_json(self.manifest_path, value)
        self.sidecar_path.write_text(value["manifest_sha256"] + "\n", encoding="utf-8", newline="\n")

    def readback(self, phase, observations):
        return {"at_utc": self.terminal_at if phase == "AfterJobsUpdate" else "2026-10-11T14:23:43.155979+00:00", "blocking": {}, "mode": "jobs",
                "observations": observations, "phase": phase, "release_id": self.release_id, "schema_version": 1}

    def update_log(self):
        return {"schema_version": 1, "action": "update", "window_end_sast": "21:00", "rollback_deadline_sast": "23:30",
                "started_at": "2026-10-11T14:51:59.795851+00:00", "finished_at": "2026-10-11T14:56:40.281831+00:00",
                "updated": list(jo.UPDATE_ORDER), "skipped_already_new": [], "restored": [], "unrestored": [],
                "changed_definitions": [{"job": job, "before_sha256": sha_text(f"before {job}"), "after_sha256": self.definitions[job]}
                                        for job in jo.UPDATE_ORDER],
                "active_executions": [], "stopped": None, "branch": None, "codes": [], "complete": True}

    def write(self):
        write_json(self.bindings_path, self.bindings())
        manifest = self.manifest()
        self.write_manifest(manifest)
        write_json(self.readback_path("FreezeJobs"), self.readback("FreezeJobs", {"image_digest": self.digest,
                                                                                 "manifest_sha256": manifest["manifest_sha256"]}))
        write_json(self.readback_path("AfterJobsUpdate"), self.readback("AfterJobsUpdate", {
            "executions_in_window": [], "jobs_at_digest": len(jo.UPDATE_ORDER),
            "first_b_chain": {"expect": "first-b", "run_date": "2026-10-12"}}))
        write_json(self.update_log_path, self.update_log())
        write_json(self.lock_path, {"schema_version": 1, "release_id": self.release_id, "expected_smoke_checks": 6,
                                    "bound_files": {"bindings": sha_file(self.bindings_path), "baseline": sha_text("baseline"),
                                                    "baseline_chain": sha_text("chain"), "durable_manifest": sha_text("durable"),
                                                    "dry_run_receipt": sha_text("dry run")}, "repo_files": {}, "tests": {}})
        self.write_review()

    def relock(self):
        """The lock and the review agree with the bindings as they are now: what a lock made after the edit would say."""
        self.edit(self.lock_path, lambda lock: lock["bound_files"].update({"bindings": sha_file(self.bindings_path)}))
        self.write_review()

    def write_review(self, verdict="ACCEPT", **over):
        value = {"schema_version": 1, "verdict": verdict, "target": self.commit, "lock_sha256": sha_file(self.lock_path)}
        value.update(over)
        write_json(self.review_path, value)

    # changes a test makes to a file that is already there
    def edit(self, path, change):
        """Read a JSON file, hand the value to `change` (which edits it in place), write it back."""
        path = Path(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        change(value)
        write_json(path, value)

    def refreeze(self, change):
        """Change the manifest and keep it self consistent: its hash recomputed, its sidecar rewritten. Only the readbacks and the pin can tell."""
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        change(value)
        value["manifest_sha256"] = so.manifest_hash(value)
        self.write_manifest(value)

    def derive_argv(self, out, **over):
        flags = {"--prior-lock": self.lock_path, "--prior-packet-dir": self.packet, "--expect-digest": jw.B_LIVE_DIGEST, "--out": out}
        flags.update({k: v for k, v in over.items() if v is not None})
        argv = ["prior-jobs-release"]
        for key, value in flags.items():
            argv += [key, str(value)]
        return argv


def record_for_world(world, *, release_id=RID, commit=COMMIT, digest=DIGEST, terminal_at=TERMINAL_AT):
    """The record `prior-jobs-release` gives for a prior release whose fourteen jobs stand in `world` right now: the definitions are the
    fingerprints of the jobs as the world holds them."""
    return {"schema_version": 1, "kind": "prior-jobs-release", "release_id": release_id, "commit": commit, "image_digest": digest,
            "terminal": {"kind": "AfterJobsUpdate", "at_utc": terminal_at},
            "job_definitions": {job: so.fingerprint(so.job_view(world.jobs[job])) for job in so.JOB_NAMES},
            "sources": {key: sha_text(key) for key in ("lock_sha256", "bindings_sha256", "manifest_sha256", "after_update_sha256", "update_log_sha256")}}


def world_on_prior(world, digest=DIGEST):
    """The live world after the prior release: every job on its image."""
    jw.set_all_jobs_image(world, digest)
    return world


def write_record(path, record):
    return write_json(path, copy.deepcopy(record))
