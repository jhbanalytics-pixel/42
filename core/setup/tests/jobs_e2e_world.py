"""Release B end to end, offline: a packet produced by the command line, the real JOBS-PASTE.ps1 run as a pwsh process for each action, and
a fake world behind every command it starts (core/setup/tests/e2e_shim.py plays the commands). The packet files are not written by a
test: capture-baseline-j, bind, lock, review and receipt make them, and the paste then reads exactly those files."""
from __future__ import annotations

import contextlib
import io
import json
import pickle
from pathlib import Path

from core.setup.release import packet
from core.setup.tests import cloud_world as cw
from core.setup.tests import jobs_packet_world as jpw
from core.setup.tests import jobs_world as jw
from core.setup.tests.jobs_paste_world import JobsPasteWorld

SHIM = Path(__file__).resolve().parent / "e2e_shim.py"


class PackedPaste(JobsPasteWorld):
    """A paste world over a packet that already exists on disk (made by the packet command line), not one built from fakes."""

    def __init__(self, bench, action):
        self.tmp, self.action, self.rid = bench.tmp, action, bench.release_id
        self.commit, self.tree = bench.target, bench.tree
        self.repo = bench.repo
        self.paste = bench.repo / "core/setup/release/JOBS-PASTE.ps1"
        self.release_dir = bench.packet
        self.bindings, self.lock, self.review, self.receipt = bench.bindings, bench.lock_file(), bench.review, bench.receipt
        self.durable, self.baseline, self.chain, self.dry_run = bench.durable, bench.baseline, bench.chain_path, bench.dry_run


class EndToEnd:
    def __init__(self, tmp, monkeypatch, **state_over):
        self.bench = jpw.JobsBench(tmp, monkeypatch, fixture_class=jw.ReleaseWorld)
        self.state_over = state_over
        self.state_path = self.bench.tmp / "state.pkl"
        self.cli_calls = []

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = packet.main(argv)
        self.cli_calls.append((argv[0], code))
        assert code == 0, (argv[0], err.getvalue())
        return out.getvalue()

    def make_packet(self):
        """Every file of the packet, from the command line, in the order 8.2 of the packet draft fixes."""
        b = self.bench
        b.write_a_readback()
        self.cli(b.capture_argv())
        self.cli(b.producer_argv())
        b.write_chain()
        b.write_durable()
        b.write_dry_run()
        self.cli(b.bind_argv())
        self.cli(b.lock_argv())
        self.cli(b.review_argv())
        b.line.write_text(b.line_text() + chr(10), encoding="utf-8")
        self.cli(b.receipt_argv())
        clock = cw.Clock(b.fixture.now)
        cloud = cw.FakeCloudRun(b.world, clock)
        self.services_before = cloud.services_bytes()
        self.save({"fixture": b.fixture, "cloud": cloud, "clock": clock, "schema_applied": False, "calls": 0, "log": [], "target": b.target,
                   "new_digest": jw.NEW_DIGEST, **self.state_over})
        return self

    def save(self, state):
        self.state_path.write_bytes(pickle.dumps(state))

    def state(self):
        return pickle.loads(self.state_path.read_bytes())

    def paste(self, action, **kw):
        world = PackedPaste(self.bench, action)
        extra = {"executor": str(SHIM), "state": str(self.state_path), "fixed_now": self.bench.fixture.now.isoformat(), **kw.pop("extra", {})}
        return world.run(extra=extra, **kw)

    # what the world looks like
    def job_images(self):
        cloud = self.state()["cloud"]
        return {job: cloud.digest_of(job) for job in cloud.world.jobs}

    def read(self, relative):
        return json.loads((self.bench.packet / relative).read_text(encoding="utf-8"))
