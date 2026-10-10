"""A packet in progress for Release B (the jobs image release): a throwaway repository at the release commit, the live world after
Release A's promotion, the files the lead supplies between the commands, and the argument lists of every packet command in mode jobs.
Offline: gcloud is the stand-in of packet_world, the chain evidence producer reads the fake Cloud Run and the fake BigQuery of
jobs_world, nothing here starts pwsh.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from core.setup.release import chain_evidence as ce
from core.setup.release import jobs_effects as je
from core.setup.release import lock as locklib
from core.setup.release import packet
from core.setup.tests import jobs_world as jw
from core.setup.tests import packet_world as pw
from core.setup.tests import release_world as rw

ROOT = pw.ROOT
OWNER = {"lane": 3, "person": "Albert Meintjes"}
NOW = dt.datetime(2026, 10, 11, 8, 0, tzinfo=dt.timezone.utc)
CALLER = rw.CALLER
SCHEMA_DIR = ROOT / "core" / "schema"
_TREES = {}


def trees():
    """The head tree of this checkout and the a80 archive, read once: the inputs of the durable manifest the lead builds."""
    if not _TREES:
        from core.setup import durable_effects_check as de

        _TREES["head"] = jw.head_tree()
        _TREES["a80"] = de.git_tree_files(je.A80, "core", ROOT)
    return _TREES["head"], _TREES["a80"]


def make_jobs_repo(path):
    """The repository of packet_world.make_repo plus the schema folder, so the apply dry run receipt can be held to the views the commit
    declares. Committed."""
    path = Path(path)
    pw.make_repo(path)
    for source in sorted(SCHEMA_DIR.glob("*")):
        if source.is_file() and source.suffix in (".py", ".sql"):
            target = path / "core" / "schema" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    pw.git(path, "add", "-A")
    pw.git(path, "commit", "-q", "-m", "schema sources")
    return path


# The line Albert approved on 10 Oct 2026 (batch 8, decisions/FOR-ALBERT-PENDING.md item 11), pinned here as written and not built from the
# template in packet.py, so the template cannot move this value. It has no sentence beyond the approved one.
APPROVED_LINE = ("RELEASE B {release_id}: I authorise the jobs only release of this release id and nothing else. Only the 14 Cloud Run jobs "
                 "change, to one image built from the release commit; no service revision, traffic entry, tag, scheduler entry, secret, "
                 "environment variable or paid Ask changes.")


def authorisation_line(release_id):
    return APPROVED_LINE.format(release_id=release_id)


class JobsBench:
    def __init__(self, tmp, monkeypatch, *, repo=None, fixture_class=None):
        from core.setup.release import bound_readback as helper

        self.tmp = Path(tmp)
        self.monkeypatch = monkeypatch
        self.repo = make_jobs_repo(repo or self.tmp / "repo")
        self.packet = self.tmp / "packet"
        self.fixture = (fixture_class or jw.ChainFixture)(self.tmp / "world")
        self.world = self.fixture.world
        self.argv, self.log = pw.fake_gcloud(self.tmp / "fake", self.world)
        monkeypatch.setattr(helper, "gcloud_command", lambda: self.argv)
        monkeypatch.setattr(packet, "utc_now", lambda: NOW)
        self.target = pw.head(self.repo)
        self.tree = pw.git(self.repo, "rev-parse", "HEAD^{tree}")
        self.release_id = f"rel-{self.target[:7]}-01"
        self.a_readback = self.tmp / "a" / "AfterPromotion-01.json"
        self.baseline = self.tmp / "baseline" / "baseline-J.json"
        self.durable = self.packet / "durable-effects.json"
        self.dry_run = self.packet / "apply-dry-run-receipt.txt"
        self.chain_dir = self.packet
        self.bindings = self.packet / "live-bindings.json"
        self.producer = self.packet / "producer-bindings.json"
        self.review = self.packet / "independent-review.json"
        self.receipt = self.packet / "execution-receipt.json"
        self.line = self.tmp / "albert-line.txt"
        self.lock_dir = self.tmp / "locks"
        self.chain_path = None

    # the argument lists of each command, as the lead would type them
    def capture_argv(self, **over):
        flags = {"--out": self.baseline, "--gcloud-timeout-seconds": pw.GCLOUD_TIMEOUT, "--a-readback": self.a_readback, "--a-retire": "waits_for_b"}
        flags.update({k: v for k, v in over.items() if v is not None})
        out = ["capture-baseline-j"]
        for key, value in flags.items():
            out += [key, str(value)]
        return out

    def common_flags(self, **over):
        flags = {"--mode": "jobs", "--repo": self.repo, "--release-commit": self.target, "--attempt": "01", "--packet-dir": self.packet,
                 "--baseline-j": self.baseline, "--caller-account": CALLER, "--gcloud-timeout-seconds": pw.GCLOUD_TIMEOUT,
                 "--http-timeout-seconds": pw.HTTP_TIMEOUT, "--chain-evidence-bytes-cap": "67108864", "--collect-start-tolerance-minutes": "10"}
        flags.update({k: v for k, v in over.items() if v is not None})
        return flags

    def producer_argv(self, **over):
        out = ["bind", "--producer-only"]
        for key, value in self.common_flags(**over).items():
            out += [key, str(value)]
        return out

    def bind_argv(self, **over):
        flags = self.common_flags()
        flags.update({"--baseline-chain": self.chain_path, "--durable-manifest": self.durable, "--dry-run-receipt": self.dry_run,
                      "--max-baseline-age-days": "2", "--max-candidate-age-hours": "12"})
        flags.update({k: v for k, v in over.items() if v is not None})
        out = ["bind"]
        for key, value in flags.items():
            out += [key, str(value)]
        return out

    def lock_flags(self):
        return ["--mode", "jobs", "--lock-dir", str(self.lock_dir)]

    def lock_argv(self):
        return ["lock", "--repo", str(self.repo), "--packet-dir", str(self.packet), *self.lock_flags()]

    def review_argv(self, verdict="ACCEPT"):
        return ["review", "--repo", str(self.repo), "--packet-dir", str(self.packet), "--verdict", verdict, *self.lock_flags()]

    def receipt_argv(self, *extra):
        return ["receipt", "--repo", str(self.repo), "--packet-dir", str(self.packet), "--authorisation-line-file", str(self.line),
                *self.lock_flags(), *extra]

    # what the lead supplies, in the order the packet needs it
    def write_a_readback(self, **over):
        value = {"schema_version": 1, "mode": "services-only", "phase": "AfterPromotion", "release_id": jw.A_RID, "at_utc": jw.A_TERMINAL_AT}
        value.update(over)
        self.a_readback.parent.mkdir(parents=True, exist_ok=True)
        self.a_readback.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    def copy_a_readback(self):
        """Release A's AfterPromotion readback as A's own helper wrote it (jobs_world.a_promoted_scenario): the one input that comes from outside B."""
        self.a_readback.parent.mkdir(parents=True, exist_ok=True)
        self.a_readback.write_bytes(self.fixture.a_readback_file.read_bytes())

    def write_durable(self):
        head, a80 = trees()
        manifest = je.build_jobs_manifest(head, a80, release_id=self.release_id, commit=self.target, tree=self.tree, owner=OWNER)
        self.durable.parent.mkdir(parents=True, exist_ok=True)
        self.durable.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    def write_dry_run(self, text=None):
        self.dry_run.parent.mkdir(parents=True, exist_ok=True)
        self.dry_run.write_bytes((jw.dry_run_receipt_text() if text is None else text).encode("utf-8"))

    def write_chain(self, role="baseline", **over):
        """The baseline chain manifest, produced by the producer itself from the producer bindings the packet command wrote."""
        bound = json.loads(self.producer.read_text(encoding="utf-8"))
        manifest = ce.build_manifest(bound, self.fixture.reader(), self.fixture.bq(), jw.RUN_DATE, role, now=lambda: NOW)
        manifest.update(over)
        self.chain_dir.mkdir(parents=True, exist_ok=True)
        self.chain_path = ce.write_manifest(self.chain_dir, manifest)
        return self.chain_path

    def line_text(self):
        return authorisation_line(self.release_id)

    def lock_file(self):
        return Path(self.lock_dir) / locklib.lock_path(self.repo, self.release_id).name

    def bindings_value(self):
        return json.loads(self.bindings.read_text(encoding="utf-8"))

    def baseline_value(self):
        return json.loads(self.baseline.read_text(encoding="utf-8"))
