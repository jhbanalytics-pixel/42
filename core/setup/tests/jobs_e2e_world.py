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
import copy
import datetime as dt
import io
import json
import os
import pickle
import shlex
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
    from core.setup.release import jobs_effects

    statements = [s for name in ("agent.sql", "core.sql") for s in schema_apply.split_statements(sql[name])]
    columns = {name: jobs_effects.final_columns(statements, name.removeprefix("ogilvy-trends-v2."))
               for name, body in tables.items() if body is None}
    return {"datasets": {schema_apply.statement_name(s) for s in head if schema_apply.is_schema(s)}, "tables": tables,
            "columns": columns}


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
    def __init__(self, tmp, monkeypatch, *, bench_kwargs=None, **state_over):
        """`bench_kwargs` go to the JobsBench: a Release D run starts from the world Release B left, with a clock, a chain date, a commit,
        a release letter and a prior release record of its own."""
        self.bench = jpw.JobsBench(tmp, monkeypatch, fixture_class=jw.ReleaseWorld, **(bench_kwargs or {}))
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
        argv = ["--date", b.run_date.isoformat(), "--bindings", str(b.producer), "--evidence", str(evidence), "--expect", role]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = ce.main(argv, reader_factory=lambda timeouts: b.fixture.reader(), bq_factory=lambda bound: b.fixture.bq(), now=lambda: b.now)
        assert code == 0, err.getvalue()
        b.chain_path = b.packet / f"chain-evidence-{b.run_date.isoformat()}.json"
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
        """py and gcloud stand-ins first on PATH, in the executable format of the host."""
        self.bin.mkdir(parents=True, exist_ok=True)
        for name in ("py", "gcloud"):
            if os.name == "nt":
                (self.bin / f"{name}.cmd").write_text(f'@echo off\r\n"{sys.executable}" -B "{SHIM}" "{self.state_path}" {name} %*\r\nexit /b %ERRORLEVEL%\r\n',
                                                      encoding="utf-8", newline="")
            else:
                program = self.bin / name
                command = shlex.join([sys.executable, "-B", str(SHIM), str(self.state_path), name])
                program.write_text('#!/bin/sh\nexec ' + command + ' "$@"\n', encoding="utf-8")
                program.chmod(0o755)

    def make_packet(self):
        """Every file of the packet, from the tools, in the order 8.2 of the packet draft fixes."""
        b = self.bench
        b.copy_a_readback()
        self.cli(b.capture_argv())
        self.cli(b.producer_argv())
        self.run_producer("baseline")
        b.write_durable()
        from core.setup import durable_effects_check

        durable = json.loads(b.durable.read_text(encoding="utf-8"))
        durable["reviewed_by"] = "Synthetic E2E reviewer"
        for effect in durable["effects"]:
            for name, verdict in effect["compat"].items():
                verdict.update(verdict="pass", test=f"synthetic E2E {name} fixture")
        durable["manifest_sha256"] = durable_effects_check.manifest_hash(durable)
        b.durable.write_text(json.dumps(durable, indent=2) + "\n", encoding="utf-8")
        self.apply_dry_run()
        self.cli(b.bind_argv())
        self.cli(b.lock_argv())
        self.cli(b.review_argv())
        b.line.write_text(b.line_text() + chr(10), encoding="utf-8")
        b.quiet_line_file.write_text(b.quiet_text() + chr(10), encoding="utf-8")
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


# Release D on top of the live Release B

D_NOW = dt.datetime(2026, 10, 13, 8, 0, tzinfo=dt.timezone.utc)
D_DAY = dt.date(2026, 10, 12)


def release_b_live(tmp, monkeypatch):
    """Release B walked to AfterJobsUpdate by the real paste, on the image it went live on: its packet is the one Release D is bound to."""
    run = EndToEnd(Path(tmp) / "b", monkeypatch, new_digest=jw.B_LIVE_DIGEST).make_packet()
    for action in ("JobsCandidate", "JobsUpdate"):
        result = run.paste(action)
        assert result.returncode == 0, (action, result.stderr, result.stdout)
    return run


def derive_prior(run_b, out, expect=jw.B_LIVE_DIGEST):
    """The prior jobs release record, derived by the packet command from the lock and the packet Release B's paste and helpers left."""
    out = Path(out)
    run_b.cli(["prior-jobs-release", "--prior-lock", str(run_b.bench.lock_file()), "--prior-packet-dir", str(run_b.bench.packet),
               "--expect-digest", expect, "--out", str(out)])
    return json.loads(out.read_text(encoding="utf-8"))


def release_d(tmp, monkeypatch, run_b, prior_file, **state_over):
    """Release D's packet, made by the same tools on the world Release B left: the fourteen jobs as B's update left them, the services as
    Release A left them, a chain of 12 Oct on B's image, and a clock on 13 Oct. D has a commit, a release id and an image of its own."""
    left = copy.deepcopy(run_b.state()["cloud"].world.jobs)

    def prepare(fixture):
        fixture.world.jobs = left
        fixture.now = D_NOW

    bench_kwargs = {"fixture_kwargs": {"digest": jw.B_LIVE_DIGEST, "day": D_DAY}, "prepare": prepare, "now": D_NOW, "run_date": D_DAY,
                    "salt": "release d", "letter": "D", "prior_file": prior_file}
    return EndToEnd(Path(tmp) / "d", monkeypatch, bench_kwargs=bench_kwargs, new_digest=jw.D_DIGEST, **state_over).make_packet()
