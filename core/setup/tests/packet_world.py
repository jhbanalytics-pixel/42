"""Fixtures for the packet command line tests (no cloud, no network, no pwsh).

fake_gcloud writes a stand-in for the gcloud binary: a script the real GcloudReader runs through its own subprocess path, which
answers the read commands from a simulated world and records every argument list it was given. Anything that is not one of the
five read commands the baseline needs makes it exit 97, so a write attempt cannot pass unnoticed.

make_repo builds a throwaway git repository holding the release sources the lock lists, so a test can commit, dirty and lock it.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from core.setup import durable_effects_check as dec
from core.setup.release import lock as locklib
from core.setup.tests import release_world as rw

ROOT = Path(__file__).resolve().parents[3]
UTC = "2026-10-09T10:00:00+00:00"
A80_JOBS_DIGEST = "sha256:e77c3819cc68" + "0" * 52
GCLOUD_TIMEOUT, HTTP_TIMEOUT = "120", "30"
CALLER = rw.CALLER
STEPS = ("Candidate", "Promote", "Rollback", "Retire")

FAKE_GCLOUD = r'''
import json
import sys

world = json.load(open(sys.argv[1], encoding="utf-8"))
with open(sys.argv[2], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[3:]) + "\n")
args = sys.argv[3:]
if world.get("fail_on") and world["fail_on"] in args:
    sys.stderr.write("backend unavailable\n")
    sys.exit(5)


def answer(value):
    sys.stdout.write(json.dumps(value))
    sys.exit(0)


if args[:3] == ["run", "services", "describe"]:
    answer(world["services"][args[3]])
if args[:3] == ["run", "revisions", "describe"]:
    if args[3] not in world["revisions"]:
        sys.stderr.write("NOT_FOUND\n")
        sys.exit(1)
    answer(world["revisions"][args[3]])
if args[:3] == ["run", "revisions", "list"]:
    answer([{"metadata": {"name": n}} for n in world["revision_names"][args[args.index("--service") + 1]]])
if args[:3] == ["run", "jobs", "describe"]:
    answer(world["jobs"][args[3]])
if args[:3] == ["run", "services", "get-iam-policy"]:
    answer(world["policy"])
sys.stderr.write("UNEXPECTED COMMAND\n")
sys.exit(97)
'''


def a80_world(*, serving=None, jobs_digest=A80_JOBS_DIGEST, declared=rw.SYNTHETIC_DECLARED):
    """The simulated live system at a80: both services on the a80 revisions, the fourteen jobs on the e77c3819 image."""
    world = rw.World(declared=declared)
    for name in world.jobs:
        container = world.jobs[name]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
        container["image"] = f"{rw.so.REGION}-docker.pkg.dev/{rw.PROJECT}/intelligence-42/jobs@{jobs_digest}"
    for service, revision in (serving or {}).items():
        world.svc[service]["traffic"] = [{"revisionName": revision, "percent": 100}]
        world.svc[service]["latest_created"] = world.svc[service]["latest_ready"] = revision
    return world


def fake_gcloud(tmp, world, **extra):
    """Write the world and the stand-in binary. Returns (argv prefix for gcloud_command, call log path)."""
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    dump = {"services": {n: world.service_raw(n) for n in rw.so.SERVICES}, "revisions": world.revisions,
            "revision_names": world.revision_names, "jobs": world.jobs, "policy": world.policy, **extra}
    (tmp / "world.json").write_text(json.dumps(dump), encoding="utf-8")
    (tmp / "gcloud_stand_in.py").write_text(FAKE_GCLOUD, encoding="utf-8")
    log = tmp / "gcloud-calls.jsonl"
    log.write_text("", encoding="utf-8")
    return [sys.executable, "-B", str(tmp / "gcloud_stand_in.py"), str(tmp / "world.json"), str(log)], log


def calls(log):
    return [json.loads(line) for line in Path(log).read_text(encoding="utf-8").splitlines() if line.strip()]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(repo, *args):
    done = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          cwd=repo, capture_output=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def make_repo(path):
    """A git repository at path holding the sources the lock lists plus the build config, committed. Returns the path."""
    path = Path(path)
    for rel in (*locklib.REPO_FILES, "core/api/cloudbuild.yaml"):
        if (ROOT / rel).is_file():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))
    git(path, "init", "-q")
    git(path, "config", "core.autocrlf", "false")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "release sources")
    return path


def head(repo):
    return git(repo, "rev-parse", "HEAD")


def valid_durable_manifest(path, release_id, repo):
    """The template with every owner named, its release and source bound, and its hash recomputed: one the checker accepts."""
    manifest = json.loads((ROOT / "core/setup/release/durable-effects-manifest.template.json").read_text(encoding="utf-8"))
    manifest["release_id"] = release_id
    manifest["source"] = {"commit": head(repo), "tree": git(repo, "rev-parse", "HEAD^{tree}")}
    manifest["generated_from"]["head"] = head(repo)
    for effect in manifest["effects"]:
        effect["owner"] = {"lane": 3, "person": "Test Person"}
    manifest["manifest_sha256"] = dec.manifest_hash(manifest)
    assert dec.validate_manifest(manifest) == [], dec.validate_manifest(manifest)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return Path(path)


def write_receipts(packet_dir, verdict="pass"):
    packet_dir = Path(packet_dir)
    packet_dir.mkdir(parents=True, exist_ok=True)
    for name in ("compat-receipt.json", "old-reader-receipt.json"):
        text = json.dumps({"schema_version": 1, "verdict": verdict, "receipt": name}) + "\n"
        (packet_dir / name).write_text(text, encoding="utf-8", newline="\n")


def authorisation_line(release_id, **replace):
    line = (f"RELEASE A {release_id}: I authorise exactly one live T1 Ask, market ZA, against the candidate API tag URL only, "
            "at most 60 search credits and a USD 2.00 research budget, with a model hold ceiling of USD 4.32, no second Ask and "
            "no retry. The window is quiet and I will start no manual job until the execution ends. I will type DEPLOY, IDLE "
            "and the passcode myself.")
    for old, new in replace.items():
        line = line.replace(old.replace("_", " "), new)
    return line


class Bench:
    """One packet in progress: a repository at the release commit, the live world, and the packet directory."""

    def __init__(self, tmp, monkeypatch, *, world=None, attempt="01", repo=None):
        from core.setup.release import bound_readback as helper

        self.tmp = Path(tmp)
        self.repo = make_repo(repo or self.tmp / "repo")
        self.packet = self.tmp / "packet"
        self.world = world or a80_world()
        self.argv, self.log = fake_gcloud(self.tmp / "fake", self.world)
        monkeypatch.setattr(helper, "gcloud_command", lambda: self.argv)
        self.target = head(self.repo)
        self.attempt = attempt
        self.release_id = f"rel-{self.target[:7]}-{attempt}"
        self.baseline = self.tmp / "baseline" / "baseline-A.json"
        self.durable = self.tmp / "durable" / "durable-effects-manifest.json"
        self.bindings = self.packet / "live-bindings.json"
        self.review = self.packet / "independent-review.json"
        self.receipt = self.packet / "execution-receipt.json"
        self.line = self.tmp / "albert-line.txt"
        self.lock_dir = None

    # the argv of each subcommand, as the lead would type it
    def capture_argv(self):
        return ["capture-baseline", "--out", str(self.baseline), "--gcloud-timeout-seconds", GCLOUD_TIMEOUT]

    def bind_argv(self, **over):
        flags = {"--repo": self.repo, "--release-commit": self.target, "--attempt": self.attempt, "--packet-dir": self.packet,
                 "--baseline": self.baseline, "--durable-manifest": self.durable, "--caller-account": CALLER,
                 "--gcloud-timeout-seconds": GCLOUD_TIMEOUT, "--http-timeout-seconds": HTTP_TIMEOUT,
                 "--max-smoke-age-minutes": "360", "--inflight-asks-bytes-cap": "10485760"}
        flags.update({k: v for k, v in over.items() if v is not None})
        out = ["bind"]
        for key, value in flags.items():
            out += [key, str(value)]
        return out

    def lock_flags(self):
        return ["--lock-dir", str(self.lock_dir)] if self.lock_dir else []

    def lock_argv(self):
        return ["lock", "--repo", str(self.repo), "--packet-dir", str(self.packet), *self.lock_flags()]

    def review_argv(self, verdict="ACCEPT"):
        return ["review", "--repo", str(self.repo), "--packet-dir", str(self.packet), "--verdict", verdict, *self.lock_flags()]

    def receipt_argv(self, *extra):
        return ["receipt", "--repo", str(self.repo), "--packet-dir", str(self.packet), "--authorisation-line-file", str(self.line),
                *self.lock_flags(), *extra]

    # preparation the lead does by hand between the subcommands
    def prepare_inputs(self):
        write_receipts(self.packet)
        valid_durable_manifest(self.durable, self.release_id, self.repo)
        self.line.write_text(authorisation_line(self.release_id) + "\n", encoding="utf-8")

    def lock_file(self):
        if self.lock_dir:
            return Path(self.lock_dir) / locklib.lock_path(self.repo, self.release_id).name
        return locklib.lock_path(self.repo, self.release_id)

    def bindings_value(self):
        return json.loads(self.bindings.read_text(encoding="utf-8"))

    def baseline_value(self):
        return json.loads(self.baseline.read_text(encoding="utf-8"))
