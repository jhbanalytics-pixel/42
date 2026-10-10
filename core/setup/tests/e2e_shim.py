"""The stand-in for every command the jobs paste starts, for the end to end test (core/setup/tests/test_jobs_final.py).

    py -3.13 -B e2e_shim.py <state.pkl> py -3.13 <script or -m module> <its arguments>

The paste, run as a real pwsh process with its commands doubled, hands each command it would have started to this program. The program
loads the fake world the test pickled, runs the real main() of the named script against that world, saves the world again and exits
with the code the real script returned, so the paste's own retry, stop and readback rules act on real exit codes. Nothing here reaches
a cloud: the readers, the Cloud Run adapter, the BigQuery client, the Cloud Build session and git are all doubles over the pickled world.
"""
from __future__ import annotations

import contextlib
import io
import pickle
import re
import sys
import time
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
    assert argv[:2] == ["py", "-3.13"], argv
    rest = argv[2:]
    fixture, cloud, clock = state["fixture"], state["cloud"], state["clock"]
    if rest[0] == "-m":
        assert rest[1] == "core.schema.apply" and rest[2:] == ["--apply"], rest
        if not state.get("apply_noop"):
            state["schema_applied"] = True
        state["applied_at_call"] = state["calls"]
        print("applied")
        return 0
    script, args = rest[0].replace("\\", "/"), rest[1:]
    name = script.rsplit("/", 1)[-1]
    if name == "bound_readback.py":
        from core.setup.release import bound_readback

        return bound_readback.main(args, reader_factory=lambda timeouts: cloud, bq_factory=lambda bound: fixture.bq_client(), now=clock.now,
                                   head_files_factory=lambda bound: fixture.head_files())
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
