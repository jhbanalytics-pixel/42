"""The stand-in for every command the jobs paste starts, for the end to end test (core/setup/tests/test_jobs_final.py).

    py -3.13 -B e2e_shim.py <state.pkl> py -3.13 <script or -m module> <its arguments>
    py -3.13 -B e2e_shim.py <state.pkl> gcloud config list ...

The real paste runs as a real pwsh process with its own Read-Native and Run-Logged. The `py` and `gcloud` it starts are two small .cmd files
first on PATH (EndToEnd.install_programs), which hand the command line to this program. The program loads the fake world the test pickled,
runs the real main() of the named script against that world, saves the world again and exits with the code the real script returned, so the
paste's own retry, stop and readback rules act on real exit codes. git is the real git for the paste, over a real throwaway repository at
the release commit, but deploy_jobs.py runs with a fake git (rev-parse, status and archive) and a module check that always passes, so the
build's own HEAD and clean checks and the git archive tarball never run here; the paste's Assert-Source still checks the checkout with real
git. The rest of what is faked reaches a cloud: the Cloud Run readers and adapter, the BigQuery clients (the durable column readbacks and
core.schema.apply), the Cloud Build session and the gcloud configuration read.
"""
from __future__ import annotations

import contextlib
import io
import os
import pickle
import re
import subprocess
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

    def definition(self, target):
        from core.schema import release_install

        return release_install.definition(WarehouseBq(self.state), target)[0]

    def query(self, sql, params=None, max_bytes=None):
        match = re.search(r"\.(\w+)\.INFORMATION_SCHEMA\.COLUMNS` WHERE table_name = '(\w+)'", sql)
        assert match, sql
        table = f"{match.group(1)}.{match.group(2)}"
        columns = self.state["live"]["columns"].get("ogilvy-trends-v2." + table, [])
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
                from core.setup.release import jobs_effects

                if name not in self.live["tables"]:
                    self.live["tables"][name] = None
                    self.live.setdefault("columns", {})[name] = sorted(jobs_effects.create_columns(sql))
            elif kind == "alter":
                from core.setup.release import jobs_effects

                added = jobs_effects.ADD_COLUMN.search(sql)
                columns = self.live.setdefault("columns", {}).setdefault(name, [])
                if added and added.group(1) not in columns:
                    columns.append(added.group(1))
                    columns.sort()
            elif kind == "view":
                self.live["tables"].setdefault(name, apply.view_body(sql))
            elif kind == "schema":
                self.live["datasets"].add(name)
        return _Job([])


class WarehouseBq(ApplyBq):
    def __init__(self, state):
        super().__init__(state["live"])
        self.state = state
        self.objects = state.setdefault("warehouse_objects", {})

    def get_table(self, target):
        from google.api_core.exceptions import NotFound

        api = self.objects.get(target)
        if api is None:
            if target not in self.live["tables"]:
                raise NotFound(target)
            project, dataset, name = target.split(".")
            body = self.live["tables"][target]
            api = {"type": "VIEW" if body else "TABLE", "view": {"query": body},
                   "tableReference": {"projectId": project, "datasetId": dataset, "tableId": name}, "etag": "synthetic"}
        return types.SimpleNamespace(to_api_repr=lambda: api, view_query=api.get("view", {}).get("query"))

    def get_routine(self, target):
        return self.get_table(target)

    def query(self, sql, job_config=None, location=None):
        from core.schema import release_install

        dry = bool(job_config and job_config.dry_run)
        replace = sql.startswith("CREATE OR REPLACE")
        if not dry and not sql.lstrip().startswith("SELECT"):
            self.state.setdefault("ddl_calls", []).append(("warehouse" if replace else "schema", sql))
        if not dry and self.state.get("apply_noop"):
            return _Job([])
        if replace:
            if not dry:
                definition = release_install.expected_definition(sql)
                dataset, name = definition["target"].split(".")
                reference = {"projectId": release_install.PROJECT, "datasetId": dataset}
                if definition["kind"] == "view":
                    api = {"type": "VIEW", "view": {"query": definition["body"]},
                           "tableReference": {**reference, "tableId": name}, "etag": "synthetic"}
                else:
                    api = {"routineType": definition["routineType"], "language": "SQL", "arguments": definition["arguments"],
                           "definitionBody": definition["body"], "routineReference": {**reference, "routineId": name}, "etag": "synthetic"}
                self.objects[release_install.PROJECT + "." + definition["target"]] = api
            return _Job([])
        result = super().query(sql, job_config=job_config, location=location)
        if not dry and not sql.lstrip().startswith("SELECT"):
            self.state["schema_applied"] = True
            self.state["applied_at_call"] = self.state["calls"]
        return result


class DefinitionBq:
    def __init__(self, delegate, state):
        self.delegate, self.state = delegate, state

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def definition(self, target):
        from core.schema import release_install

        return release_install.definition(WarehouseBq(self.state), target)[0]


def fixture_source(bound):
    from core.setup import durable_effects_check
    from core.setup.release import jobs_run

    root = Path.cwd()
    read = lambda *args: subprocess.check_output(["git", "-C", str(root), *args]).decode("utf-8").strip()
    jobs_run.require(read("rev-parse", "HEAD") == bound["target"] and read("rev-parse", "HEAD^{tree}") == bound["tree"]
                     and not read("status", "--porcelain=v1"), "SOURCE", "Synthetic fixture source differs from its bound commit")
    return durable_effects_check.git_tree_files(bound["target"], cwd=root)


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
        return bound_readback.main(args, reader_factory=lambda timeouts: cloud,
                                   bq_factory=lambda bound: DefinitionBq(fixture.bq_client(), state), now=clock.now,
                                   head_files_factory=lambda bound: de.git_tree_files(bound["target"], "core", os.getcwd()))
    if name == "jobs_run.py":
        from core.setup.release import jobs_run

        return jobs_run.main(args, adapter_factory=lambda timeouts: cloud,
                             bq_factory=lambda bound: DefinitionBq(fixture.bq_client(), state), now=clock.now,
                             console=lambda: True, wall=time.time, source_factory=fixture_source,
                             warehouse_factory=lambda: WarehouseBq(state))
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
