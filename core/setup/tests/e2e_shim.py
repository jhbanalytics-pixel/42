"""The stand-in for every command the jobs paste starts, for the end to end test (core/setup/tests/test_jobs_final.py).

    py -3.13 -B e2e_shim.py <state.pkl> py -3.13 <script or -m module> <its arguments>
    py -3.13 -B e2e_shim.py <state.pkl> gcloud config list ...

The real paste runs as a real pwsh process with its own Read-Native and Run-Logged. The `py` and `gcloud` it starts are two small .cmd files
first on PATH (EndToEnd.install_programs), which hand the command line to this program. The program loads the fake world the test pickled,
runs the real main() of the named script against that world, saves the world again and exits with the code the real script returned, so the
paste's own retry, stop and readback rules act on real exit codes. git is the real git, over a real throwaway repository at the release
commit. What is faked is only what reaches a cloud: the Cloud Run readers and adapter, the BigQuery clients (the durable column readbacks and
core.schema.apply), the Cloud Build session and the gcloud configuration read.
"""
from __future__ import annotations

import contextlib
import io
import os
import pickle
import re
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


BUILD_ID = "6f1d0c2e-7b3a-4d58-9e41-a2b3c4d5e6f7"


class ColumnsBq:
    """BigQuery as the durable readbacks see it: only the INFORMATION_SCHEMA.COLUMNS read, answered from the pinned column lists, and
    only after the schema apply has run. Before it, a table that exists today shows its columns without the ones B adds."""

    def __init__(self, state):
        self.state = state

    def dry_run(self, sql, params=None):
        return 0

    def query(self, sql, params=None, max_bytes=None):
        from core.setup.tests.test_jobs_effects import COLUMNS

        match = re.search(r"\.(\w+)\.INFORMATION_SCHEMA\.COLUMNS` WHERE table_name = '(\w+)'", sql)
        assert match, sql
        table = f"{match.group(1)}.{match.group(2)}"
        final = COLUMNS.get(table, [])
        if self.state["schema_applied"]:
            columns = final
        else:
            added = {"stamp", "span_sha256", "reason_code", "linked_on", "link_market"}
            existing = table in ("intelligence_42_agent.runs", "intelligence_42_agent.claim_checks", "intelligence_42_core.post_items")
            columns = [c for c in final if c not in added] if existing else []
        return [{"column_name": c} for c in columns]


class _Job:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return self.rows


class ApplyBq:
    """BigQuery as core.schema.apply sees it: datasets, tables and views that exist (`live`, a plain dict the test and the shim pass around),
    dry runs that change nothing, and statements that create what they name. `live` starts as the a80 schema, which is what is deployed
    today (jobs_e2e_world.a80_live)."""

    def __init__(self, live):
        self.live = live

    def get_dataset(self, name):
        from google.api_core.exceptions import NotFound

        if name not in self.live["datasets"]:
            raise NotFound(name)
        return object()

    def get_table(self, name):
        from google.api_core.exceptions import NotFound

        if name not in self.live["tables"]:
            raise NotFound(name)
        return types.SimpleNamespace(view_query=self.live["tables"][name])

    def query(self, sql, job_config=None, location=None):
        from core.schema import apply

        listing = re.search(r"`[\w-]+\.(\w+)\.INFORMATION_SCHEMA\.TABLES`", sql)
        if listing:
            prefix = f"ogilvy-trends-v2.{listing.group(1)}."
            return _Job([{"table_name": name[len(prefix):], "table_type": "VIEW" if body else "BASE TABLE"}
                         for name, body in sorted(self.live["tables"].items()) if name.startswith(prefix)])
        if not (job_config is not None and job_config.dry_run):
            kind, name = apply.kind(sql), apply.statement_name(sql)
            if kind == "table":
                self.live["tables"].setdefault(name, None)
            elif kind == "view":
                self.live["tables"].setdefault(name, apply.view_body(sql))
            elif kind == "schema":
                self.live["datasets"].add(name)
        return _Job([])


class BuildSession:
    """Cloud Build's REST API: a create returns the build, a get returns it finished, and the build leaves its record and the tag."""

    def __init__(self, state):
        self.state = state
        self.posts, self.gets = [], []

    def _record(self, body):
        tag = body["images"][0]
        return {"id": BUILD_ID, "status": "SUCCESS", "serviceAccount": body["serviceAccount"], "source": body["source"], "steps": body["steps"],
                "results": {"images": [{"name": tag, "digest": self.state["new_digest"]}]}}

    def post(self, url, json=None, timeout=None):
        from core.setup.tests.test_jobs_build import FakeResponse

        self.posts.append((url, json))
        record = self._record(json)
        self.state["fixture"].world.builds[BUILD_ID] = record
        self.state["fixture"].world.registry[json["images"][0]] = self.state.get("registry_digest", self.state["new_digest"])
        return FakeResponse({"metadata": {"build": {"id": BUILD_ID, "status": "QUEUED"}}})

    def get(self, url, timeout=None):
        from core.setup.tests.test_jobs_build import FakeResponse

        self.gets.append(url)
        return FakeResponse(self.state["fixture"].world.builds[BUILD_ID])


def run_command(state, argv):
    """argv is the command the paste would have started: py -3.13 <script> <args> or py -3.13 -m <module> <args>."""
    fixture, cloud, clock = state["fixture"], state["cloud"], state["clock"]
    if argv[0] == "gcloud":
        assert argv[1:3] == ["config", "list"], argv
        import json

        print(json.dumps(cloud.config()))
        return 0
    assert argv[:2] == ["py", "-3.13"], argv
    rest = argv[2:]
    if rest[0] == "-m":
        from core.schema import apply

        assert rest[1] == "core.schema.apply" and rest[2:] == ["--apply"], rest
        files = tuple(Path(os.getcwd()) / "core" / "schema" / path.name for path in apply.SQL_FILES)
        real_load = apply.load_statements
        apply.bigquery.Client = lambda **kw: ApplyBq(state["live"])
        apply.load_statements = lambda paths=files: real_load(paths)
        code = apply.main(["--apply"])
        if code == 0 and not state.get("apply_noop"):
            state["schema_applied"] = True
        state["applied_at_call"] = state["calls"]
        return code
    script, args = rest[0].replace("\\", "/"), rest[1:]
    name = script.rsplit("/", 1)[-1]
    if name == "bound_readback.py":
        from core.setup.release import bound_readback

        from core.setup import durable_effects_check as de

        # The files of the bound commit are read by the real git archive, from the repository the paste was started in.
        return bound_readback.main(args, reader_factory=lambda timeouts: cloud, bq_factory=lambda bound: fixture.bq_client(), now=clock.now,
                                   head_files_factory=lambda bound: de.git_tree_files(bound["target"], "core", os.getcwd()))
    if name == "jobs_run.py":
        from core.setup.release import jobs_run

        return jobs_run.main(args, adapter_factory=lambda timeouts: cloud, bq_factory=lambda bound: fixture.bq_client(), now=clock.now,
                             console=lambda: True, wall=time.time)
    if name == "durable_effects_check.py":
        from core.setup import durable_effects_check

        return durable_effects_check.main(args, bq_factory=lambda: ColumnsBq(state))
    if name == "deploy_jobs.py":
        from core.setup import deploy_jobs
        from core.setup.tests.test_jobs_build import FakeGcloud, FakeGit

        commit = state["target"]
        try:
            return deploy_jobs.main(args, gcloud=FakeGcloud(), git=FakeGit(head=commit), exists=lambda module: True, workdir=str(ROOT / ".nothing"),
                                    session=BuildSession(state), sleep=lambda seconds: None)
        except SystemExit as stop:
            print(stop.code, file=sys.stderr)
            return 1
    raise AssertionError(f"the shim does not play {argv}")


def main():
    state_path, argv = Path(sys.argv[1]), sys.argv[2:]
    state = pickle.loads(state_path.read_bytes())
    state["calls"] += 1
    state["log"].append(argv)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = run_command(state, argv)
    state_path.write_bytes(pickle.dumps(state))
    sys.stdout.write(out.getvalue())
    sys.stderr.write(err.getvalue())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
