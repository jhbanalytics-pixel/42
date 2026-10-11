"""Fixtures for the packet command line tests (no cloud, no network, no pwsh).

fake_gcloud writes a stand-in for the gcloud binary: a script the real GcloudReader runs through its own subprocess path, which
answers the read commands from a simulated world and records every argument list it was given. Anything that is not one of the
five read commands the baseline needs makes it exit 97, so a write attempt cannot pass unnoticed.

make_repo builds a throwaway git repository holding the release sources the lock lists, so a test can commit, dirty and lock it.
"""
from __future__ import annotations

import hashlib
import json
import os
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


def a_world(*, jobs_digest=rw.B_JOBS_DIGEST):
    """The simulated live system as Release A leaves it: both services on A's pair by name with A's tag, the API naming A's agent tag
    URL with the canonical audience, and the fourteen jobs on an image other than e77c3819 (a jobs only release may have landed)."""
    return rw.World(after="A", jobs_digest=jobs_digest)


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


_ASK_CLOSURE = []


def ask_closure():
    """The files of core/ that importing core.agent.ask needs, found once by importing it in a child: the receipt command reads the model
    hold from the bound commit, so a throwaway repository must hold the code that computes it."""
    if not _ASK_CLOSURE:
        program = ("import sys, core.agent.ask; sys.stdout.write(chr(10).join(m.__file__ for n, m in sorted(sys.modules.items()) "
                   "if n.startswith('core') and getattr(m, '__file__', None)))")
        done = subprocess.run([sys.executable, "-B", "-c", program], cwd=ROOT, capture_output=True, encoding="utf-8", timeout=120,
                              env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH", "")]))})
        assert done.returncode == 0, done.stderr
        loaded = [Path(f).resolve().relative_to(ROOT) for f in done.stdout.splitlines() if f]
        # the modules read data files that sit beside them (a schema, a term list), so the folders they live in come whole, one level deep
        beside = {f.relative_to(ROOT) for folder in {m.parent for m in loaded} for f in (ROOT / folder).iterdir() if f.is_file() and f.suffix != ".pyc"}
        _ASK_CLOSURE.extend(sorted(path.as_posix() for path in {*loaded, *beside}))
    return list(_ASK_CLOSURE)


def make_repo(path):
    """A git repository at path holding the sources the lock lists, the build config and the code that computes the model hold, committed.
    Returns the path."""
    path = Path(path)
    for rel in (*locklib.REPO_FILES, "core/api/cloudbuild.yaml", *ask_closure()):
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


def write_receipts(packet_dir, verdict="pass", *, base=None, candidate=None, stores=None):
    """The two receipts bind reads. base and candidate are the commits the harnesses write into theirs; a release bound to an a80 prior
    release does not read them, one bound to a recorded prior release must."""
    packet_dir = Path(packet_dir)
    packet_dir.mkdir(parents=True, exist_ok=True)
    for name in ("compat-receipt.json", "old-reader-receipt.json"):
        value = {"schema_version": 1, "verdict": verdict, "receipt": name}
        if base is not None:
            value["base_commit"] = base
        if candidate is not None:
            value["candidate_commit"] = candidate
        if stores is not None and name == "old-reader-receipt.json":
            value["prior_stores_answer_meta"] = stores
        (packet_dir / name).write_text(json.dumps(value) + "\n", encoding="utf-8", newline="\n")


def edit_json(path, change):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    change(value)
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


class PriorPacket:
    """Release A's own packet as the lead holds it after A: the lock outside the checkout, and the packet directory with the bindings, the
    frozen manifest and its sidecar, the AfterPromotion readback, the tag ledger and the independent review. Every hash that ties one to
    another is computed here, not typed, so a test that changes one file breaks the tie and nothing else."""

    def __init__(self, root):
        self.root = Path(root)
        self.packet, self.lock_dir = self.root / "packet", self.root / "lock"
        self.packet.mkdir(parents=True)
        self.lock_dir.mkdir(parents=True)
        rid, commit = rw.A_RID, rw.A_COMMIT
        self.bindings = self.packet / "live-bindings.json"
        self.manifest = self.packet / "release-manifest.json"
        self.sidecar = self.packet / "release-manifest.sha256"
        self.readback = self.packet / "readbacks" / "AfterPromotion-01.json"
        self.ledger = self.packet / "tag-ledger.json"
        self.review = self.packet / "independent-review.json"
        self.lock = self.lock_dir / f"PACKET-LOCK-{rid}.json"
        self.write(self.bindings, {
            "schema_version": 1, "mode": "services-only", "status": "BOUND_FOR_INDEPENDENT_REVIEW", "release_id": rid, "target": commit,
            "tree": "25473218f777da6a296da8b02362f58a80483333",
            "envDeltas": {"f42-agent": {"F42_VERSION": rw.A_SHORT12},
                          "f42-api": {"F42_VERSION": rw.A_SHORT12, "AGENT_URL": rw.A_TAG_URL["f42-agent"], "AGENT_AUDIENCE": rw.CANON["f42-agent"]}}})
        manifest = {
            "schema_version": 1, "mode": "services-only", "release_id": rid,
            "source": {"commit": commit, "tree": "25473218f777da6a296da8b02362f58a80483333", "short7": commit[:7], "short12": rw.A_SHORT12},
            "canonical": {"agent_url": rw.CANON["f42-agent"], "api_url": rw.CANON["f42-api"], "api_agent_url_literal": rw.CANON["f42-agent"]},
            "candidates": {name: {"revision": rw.A_REV[name], "tag": rid, "tag_url": rw.A_TAG_URL[name]} for name in rw.so.SERVICES},
            "image": {"digest": rw.A_DIGEST, "registry_digest": rw.A_DIGEST, "reference": f"{rw.REPO}@{rw.A_DIGEST}", "repository": rw.REPO}}
        self.seal(manifest)
        self.write(self.ledger, {"schema_version": 1, "release_id": rid, "events": [
            {"phase": phase, "state": state, "service": name, "tag": rid, "revision": rw.A_REV[name], "tag_url": rw.A_TAG_URL[name],
             "digest": f"{rw.REPO}@{rw.A_DIGEST}", "manifest_sha256": manifest["manifest_sha256"], "at_utc": "2026-10-10T13:32:58+00:00"}
            for phase, state in (("BeforeSmoke", "CANDIDATE"), ("AfterPromotion", "SERVING")) for name in rw.so.SERVICES]})
        self.write(self.lock, {"schema_version": 1, "release_id": rid, "repo_files": {}, "tests": {}, "expected_smoke_checks": 6,
                               "bound_files": {"bindings": sha(self.bindings), "baseline": "0" * 64, "durable_manifest": "1" * 64,
                                               "compat_receipt": "2" * 64, "old_reader_receipt": "3" * 64}})
        self.write(self.review, {"schema_version": 1, "verdict": "ACCEPT", "target": commit, "lock_sha256": sha(self.lock)})

    @staticmethod
    def write(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    def seal(self, manifest):
        """The manifest with its own hash, its sidecar, and the AfterPromotion readback that cites it."""
        manifest["manifest_sha256"] = rw.so.manifest_hash(manifest)
        self.write(self.manifest, manifest)
        self.sidecar.write_text(manifest["manifest_sha256"] + "\n", encoding="utf-8", newline="\n")
        self.write(self.readback, {"schema_version": 1, "mode": "services-only", "phase": "AfterPromotion", "release_id": manifest["release_id"],
                                   "manifest_sha256": manifest["manifest_sha256"], "blocking": {}, "observations": {}})

    def expected(self):
        """The record `packet prior-release` must write, built here from the fixture's own values and hashes."""
        record = rw.prior_record_a(jobs_digest=None)
        record["sources"] = {"lock_sha256": sha(self.lock), "bindings_sha256": sha(self.bindings), "manifest_sha256": sha(self.manifest),
                             "after_promotion_sha256": sha(self.readback)}
        return record


def authorisation_line(release_id, letter="A", **replace):
    line = (f"RELEASE {letter} {release_id}: I authorise exactly one live T1 Ask, market ZA, against the candidate API tag URL only, "
            "at most 60 search credits and a USD 2.00 research budget, with a model hold ceiling of USD 4.92, no second Ask and "
            "no retry. The window is quiet and I will start no manual job until the execution ends. I will type DEPLOY, IDLE "
            "and the passcode myself.")
    for old, new in replace.items():
        line = line.replace(old.replace("_", " "), new)
    return line


class Bench:
    """One packet in progress: a repository at the release commit, the live world, and the packet directory."""

    def __init__(self, tmp, monkeypatch, *, world=None, attempt="01", repo=None, after=None):
        """after: "A" makes Release A the prior release: A's live state, A's packet on disk, and the flags that name the prior release."""
        from core.setup.release import bound_readback as helper

        self.tmp = Path(tmp)
        self.repo = make_repo(repo or self.tmp / "repo")
        self.packet = self.tmp / "packet"
        self.after = after
        self.letter = "C" if after == "A" else "A"
        self.prior_root = self.tmp / "prior-a"
        self.prior_file = self.tmp / "prior" / "prior-release.json"
        self.world = world or (a_world() if after == "A" else a80_world())
        self.argv, self.log = fake_gcloud(self.tmp / "fake", self.world)
        monkeypatch.setattr(helper, "gcloud_command", lambda: self.argv)
        if after == "A":
            # The real derivation runs the prior commit's writers (about ten seconds); the throwaway repository does not hold that commit.
            # test_release_prior_packet.py runs the real one on this repository.
            from core.setup.release import packet as packet_tool

            monkeypatch.setattr(packet_tool, "prior_writes_answer_meta", lambda repo, commit: True)
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
    def prior_argv(self):
        return ["prior-release", "--prior-lock", str(self.prior_root / "lock" / f"PACKET-LOCK-{rw.A_RID}.json"),
                "--prior-packet-dir", str(self.prior_root / "packet"), "--repo", str(self.repo), "--out", str(self.prior_file)]

    def capture_argv(self, *extra):
        prior = ["--prior-release", str(self.prior_file)] if self.after else []
        return ["capture-baseline", "--out", str(self.baseline), "--gcloud-timeout-seconds", GCLOUD_TIMEOUT, *prior, *extra]

    def bind_argv(self, **over):
        flags = {"--repo": self.repo, "--release-commit": self.target, "--attempt": self.attempt, "--packet-dir": self.packet,
                 "--baseline": self.baseline, "--durable-manifest": self.durable, "--caller-account": CALLER,
                 "--gcloud-timeout-seconds": GCLOUD_TIMEOUT, "--http-timeout-seconds": HTTP_TIMEOUT,
                 "--max-smoke-age-minutes": "360", "--inflight-asks-bytes-cap": "10485760"}
        if self.after:
            flags.update({"--prior-release": self.prior_file, "--release-letter": self.letter})
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
    def prepare_prior(self):
        """Release A's packet on disk, as the lead holds it. Nothing is derived from it yet."""
        self.prior = PriorPacket(self.prior_root)
        return self.prior

    def prepare_inputs(self):
        import contextlib
        import io

        from core.setup.release import packet as packet_tool

        if self.after:
            write_receipts(self.packet, base="3710c5e", candidate=self.target, stores=True)
            self.prepare_prior()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                assert packet_tool.main(self.prior_argv()) == 0
        else:
            write_receipts(self.packet)
        valid_durable_manifest(self.durable, self.release_id, self.repo)
        self.line.write_text(authorisation_line(self.release_id, self.letter) + "\n", encoding="utf-8")

    def lock_file(self):
        if self.lock_dir:
            return Path(self.lock_dir) / locklib.lock_path(self.repo, self.release_id).name
        return locklib.lock_path(self.repo, self.release_id)

    def bindings_value(self):
        return json.loads(self.bindings.read_text(encoding="utf-8"))

    def baseline_value(self):
        return json.loads(self.baseline.read_text(encoding="utf-8"))
