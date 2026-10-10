"""Release B end to end, offline: a packet made by the same tools the lead runs, the real JOBS-PASTE.ps1 run as a pwsh process for each
action with its own Read-Native and Run-Logged, and a fake world behind every program that would reach a cloud (core/setup/tests/e2e_shim.py).

No file that the paste or a helper writes is put in place by a test. What the test supplies is what comes from outside Release B, and each
of those is listed with its writer in SEAMS-B.md (wave8/lanes/release-b):

    Release A's AfterPromotion readback    written by Release A's own helper (release_world.Scenario), copied as it is (copy_a_readback)
    the durable manifest                   built by the lead from je.build_jobs_manifest, which has no command line (write_durable)
    Albert's authorisation line            typed by Albert, pinned in jobs_packet_world.APPROVED_LINE
    Albert's quiet window line             typed by Albert, pinned in jobs_packet_world.QUIET_LINE
    the review verdict                     a reviewer's, recorded with the review command

Everything else is made by a tool: capture-baseline-j, bind, lock, review and receipt by the packet command line; the baseline chain
manifest by chain_evidence.main; the apply dry run log by core.schema.apply.main over a fake BigQuery, written with the CRLF line ends
and the UTF-8 encoding of `| Set-Content`; the build id, the release manifest, the readbacks, the schema receipt, the update token, the
quiet snapshot and the run logs by the paste and its helpers."""
from __future__ import annotations

import contextlib
import io
import json
import os
import pickle
import sys
from pathlib import Path

from core.schema import apply as schema_apply
from core.setup import durable_effects_check as de
from core.setup.release import chain_evidence as ce
from core.setup.release import jobs_effects as je
from core.setup.release import packet
from core.setup.tests import cloud_world as cw
from core.setup.tests import jobs_packet_world as jpw
from core.setup.tests import jobs_world as jw
from core.setup.tests.e2e_shim import ApplyBq
from core.setup.tests.jobs_paste_world import JobsPasteWorld

SHIM = Path(__file__).resolve().parent / "e2e_shim.py"
ROOT = jpw.ROOT


def a80_live():
    """BigQuery as it stands before Release B: the datasets exist and every table and view of the a80 schema files is there."""
    head = schema_apply.load_statements()
    files = de.git_tree_files(je.A80, "core", ROOT)
    sql = {Path(path).name: text for path, text in files if path.startswith("core/schema/") and path.endswith(".sql")}
    tables = {}
    for name in ("agent.sql", "core.sql"):
        for statement in schema_apply.split_statements(sql[name]):
            kind = schema_apply.kind(statement)
            if kind in ("table", "view"):
                tables[schema_apply.statement_name(statement)] = schema_apply.view_body(statement) if kind == "view" else None
    return {"datasets": {schema_apply.statement_name(s) for s in head if schema_apply.is_schema(s)}, "tables": tables}


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
        self.bin = self.bench.tmp / "bin"
        self.cli_calls = []
        self.tools_run = []

    def cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = packet.main(argv)
        self.cli_calls.append((argv[0], code))
        assert code == 0, (argv[0], err.getvalue())
        return out.getvalue()

    def run_producer(self, role):
        """The baseline chain manifest: chain_evidence.main with the producer bindings the packet command wrote, as the lead types it."""
        b = self.bench
        evidence = b.tmp / "producer-run"
        argv = ["--date", jw.RUN_DATE.isoformat(), "--bindings", str(b.producer), "--evidence", str(evidence), "--expect", role]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ce.main(argv, reader_factory=lambda timeouts: b.fixture.reader(), bq_factory=lambda bound: b.fixture.bq(), now=lambda: jpw.NOW)
        assert code == 0, err.getvalue()
        b.chain_path = b.packet / f"chain-evidence-{jw.RUN_DATE.isoformat()}.json"
        self.tools_run.append("chain_evidence")

    def apply_dry_run(self):
        """Step 1a of the packet: `py -3.13 -m core.schema.apply 2>&1 | Set-Content -Encoding utf8 <log>`, run from the release checkout over a
        fake BigQuery that holds today's schema. The log is what the tool printed; Set-Content on Windows ends each line with CRLF."""
        b = self.bench
        self.live = a80_live()
        files = tuple(b.repo / "core" / "schema" / path.name for path in schema_apply.SQL_FILES)
        real_load = schema_apply.load_statements
        b.monkeypatch.setattr(schema_apply.bigquery, "Client", lambda **kw: ApplyBq(self.live))
        b.monkeypatch.setattr(schema_apply, "load_statements", lambda paths=files: real_load(paths))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = schema_apply.main([])
        assert code == 0, out.getvalue()
        b.dry_run.parent.mkdir(parents=True, exist_ok=True)
        b.dry_run.write_bytes(out.getvalue().replace("\n", "\r\n").encode("utf-8"))
        self.tools_run.append("core.schema.apply")

    def install_programs(self):
        """py and gcloud as two .cmd files first on PATH. The paste starts them by name, as it starts the real ones."""
        self.bin.mkdir(parents=True, exist_ok=True)
        for name in ("py", "gcloud"):
            (self.bin / f"{name}.cmd").write_text(f'@echo off\r\n"{sys.executable}" -B "{SHIM}" "{self.state_path}" {name} %*\r\nexit /b %ERRORLEVEL%\r\n',
                                                  encoding="utf-8", newline="")

    def make_packet(self):
        """Every file of the packet, from the tools, in the order 8.2 of the packet draft fixes."""
        b = self.bench
        b.copy_a_readback()
        self.cli(b.capture_argv())
        self.cli(b.producer_argv())
        self.run_producer("baseline")
        b.write_durable()
        self.apply_dry_run()
        self.cli(b.bind_argv())
        self.cli(b.lock_argv())
        self.cli(b.review_argv())
        b.line.write_text(jpw.APPROVED_LINE.format(release_id=b.release_id) + chr(10), encoding="utf-8")
        b.quiet_line_file.write_text(jpw.QUIET_LINE.format(release_id=b.release_id) + chr(10), encoding="utf-8")
        self.cli(b.receipt_argv())
        clock = cw.Clock(b.fixture.now)
        cloud = cw.FakeCloudRun(b.world, clock)
        self.services_before = cloud.services_bytes()
        self.install_programs()
        self.save({"fixture": b.fixture, "cloud": cloud, "clock": clock, "schema_applied": False, "calls": 0, "log": [], "target": b.target,
                   "new_digest": jw.NEW_DIGEST, "live": self.live, **self.state_over})
        return self

    def save(self, state):
        self.state_path.write_bytes(pickle.dumps(state))

    def state(self):
        return pickle.loads(self.state_path.read_bytes())

    def paste(self, action, **kw):
        world = PackedPaste(self.bench, action)
        extra = {"real_native": True, "fixed_now": self.bench.fixture.now.isoformat(), **kw.pop("extra", {})}
        path = str(self.bin) + os.pathsep + os.environ["PATH"]
        before = len(self.state()["log"])
        result = world.run(extra=extra, env={"PATH": path}, **kw)
        result.played = [self.label(argv) for argv in self.state()["log"][before:]]
        result.commands = self.state()["log"][before:]
        return result

    @staticmethod
    def label(argv):
        """A short name for a command the shim played: the script, and the phase, check or verb it was asked for."""
        if argv[0] == "gcloud":
            return "gcloud config list"
        if argv[2] == "-m":
            return "core.schema.apply"
        name = argv[2].replace("\\", "/").rsplit("/", 1)[-1]
        args = argv[3:]
        for flag in ("--phase", "--check"):
            if flag in args:
                return f"{name} {args[args.index(flag) + 1]}"
        return f"{name} {args[0]}"

    # what the world looks like
    def job_images(self):
        cloud = self.state()["cloud"]
        return {job: cloud.digest_of(job) for job in cloud.world.jobs}

    def read(self, relative):
        return json.loads((self.bench.packet / relative).read_text(encoding="utf-8"))

    def run_folder(self, action):
        return next(iter(sorted((self.bench.packet / "runs").glob(f"*-{action}"))[-1:]), None)
