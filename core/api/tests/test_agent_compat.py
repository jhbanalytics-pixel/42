"""The compatibility contract test (W8-REL 3.2, 5.5, CT-01 to CT-07): the previous API with the candidate agent must still
work for every call the API forwards, and the candidate API must be a superset of the previous one for an old browser tab.

These tests start real processes on localhost, so they run in the deploy partition, without the offline guard."""
import shutil
from pathlib import Path

import pytest

from core.api.tests import compat_harness as h

ROOT = h.ROOT
AGENT = "core/api/agent_app.py"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return h.collect(ROOT, tmp_path_factory.mktemp("compat"))


def agent_source():
    return (ROOT / AGENT).read_text(encoding="utf-8")


def by_label(records):
    return {r["label"]: r for r in records}


def mutant_run(world, tmp_path_factory, old, new):
    """The candidate agent with one edit, behind the previous API: the records of the run."""
    tree = tmp_path_factory.mktemp("mutant")
    shutil.copytree(ROOT / "core", tree / "core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"))
    path = tree / AGENT
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, old
    path.write_text(source.replace(old, new), encoding="utf-8")
    records, _ = h.run_pair(world["base"], tree, tree)
    return records, path.read_text(encoding="utf-8")


# CT-01

def test_ct01_every_route_the_previous_api_forwards_is_in_the_candidate_agent_route_table(world):
    routes = world["routes"]
    assert len(routes) >= 35
    for expected in (("GET", "/api/watches"), ("POST", "/api/ask"), ("GET", "/api/ask/{}/events"), ("POST", "/api/investigations/{}/start"),
                     ("GET", "/api/dossiers/{}/versions/{}/export"), ("GET", "/api/investigations/{}/events"), ("POST", "/api/suppressions")):
        assert expected in routes, expected
    assert routes <= world["candidate_routes"], sorted(routes - world["candidate_routes"])


def test_ct01_the_script_calls_every_forwarded_route(world):
    assert h.uncovered(world["routes"], world["control"]) == []


def test_ct01_the_route_derivation_reads_both_call_forms_and_both_registration_forms():
    source = '''
@gated.get("/api/a/{x}")
async def a(x):
    return _passthrough(await _forward("POST", f"/api/a/{x}/b", json_body={}))

async def b(request):
    return await _relay_events(f"/api/c/{request}/events", request)
'''
    assert h.forwarded_routes(source) == {("POST", "/api/a/{}/b"), ("GET", "/api/c/{}/events")}
    agent = '''
@app.get("/api/one/{id}")
def one(id): ...
@app.api_route("/api/{path:path}", methods=["GET", "PUT"])
def two(path): ...
@app.put("/api/p/{id}")
def p(id): ...
@app.delete("/api/d")
def d(): ...
@app.patch("/api/q")
def q(): ...
@app.post("/api/o")
def o(): ...
'''
    assert h.agent_routes(agent) == {("GET", "/api/one/{}"), ("GET", "/api/{}"), ("PUT", "/api/{}"), ("PUT", "/api/p/{}"),
                                     ("DELETE", "/api/d"), ("PATCH", "/api/q"), ("POST", "/api/o")}


# CT-02

def test_ct02_the_previous_api_with_the_candidate_agent_gives_the_control_statuses_and_shapes(world):
    report = h.compare_runs(world["control"], world["old_api"])
    assert report["problems"] == []
    assert len(world["control"]) >= 50
    assert [r["status"] for r in world["control"]] == [r["status"] for r in world["old_api"]]


def test_ct02_the_control_exercises_success_and_failure_paths_not_only_errors(world):
    statuses = [r["status"] for r in world["control"]]
    assert sum(1 for s in statuses if s in (200, 201, 202)) >= 35
    assert {400, 404, 409} <= set(statuses)


def test_ct02_shape_rules():
    control = h.shape({"a": 1, "b": "x", "c": None, "d": [{"k": 1}, {"k": 2}], "e": {"f": True}})
    assert h.compare_shapes(control, h.shape({"a": 2, "b": "y", "c": 5, "d": [{"k": 3}], "e": {"f": False}, "new": 1})) == ([], ["$.new"])
    assert h.compare_shapes(control, h.shape({"a": 2, "c": None, "d": [], "e": {"f": True}}))[0] == [
        "$.b: missing from the candidate", "$.d[0]: no candidate element has this shape"]
    assert "$.a: type changed from number to string" in h.compare_shapes(control, h.shape({"a": "1", "b": "x", "c": None, "d": [{"k": 1}], "e": {"f": True}}))[0]
    assert "$.b: became null where the control had a string" in h.compare_shapes(control, h.shape({"a": 1, "b": None, "c": None, "d": [{"k": 1}], "e": {"f": True}}))[0]
    assert h.compare_shapes(control, h.shape({"a": 1, "b": "x", "c": None, "d": [{"k": "1"}], "e": {"f": True}}))[0]


# CT-03

def test_ct03_the_event_streams_deliver_the_same_event_names_in_the_same_order(world):
    labels = ["ask events", "ask events resume", "ask events replay of a stored ask", "ask events replay of a stored ask resume",
              "investigations events", "investigations events resume"]
    control = by_label(world["control"])
    for run in (world["old_api"], world["new_api"]):
        got = by_label(run)
        for label in labels:
            assert control[label]["kind"] == "sse" and control[label]["events"], label
            assert got[label]["events"] == control[label]["events"], label
    assert control["ask events"]["events"][-1] == "done"
    assert control["ask events replay of a stored ask"]["events"] == ["done"]


# CT-04

def test_ct04_errors_pass_through_with_the_same_status_and_keys(world):
    control = by_label(world["control"])
    for run in (world["old_api"], world["new_api"]):
        got = by_label(run)
        for label, status in (("watches bad body", 400), ("ask empty body", 400), ("ask read missing", 404),
                              ("dossiers edit stale", 409), ("investigations events before start", 409)):
            assert control[label]["status"] == status and got[label]["status"] == status, label
            assert set(control[label]["shape"]) == set(got[label]["shape"]) and "error" in got[label]["shape"], label


def test_ct04_a_502_is_the_same_from_the_previous_and_the_candidate_api(world):
    for records in world["dead"].values():
        assert [r["status"] for r in records] == [502, 502]
        assert "error" in records[0]["shape"]
    assert h.dead_agent_problems(world["dead"]) == []


# CT-05

def test_ct05_the_previous_api_health_is_ok_against_the_candidate_agent(world):
    health = world["old_api_health"]
    assert health["ok"] is True and health["checks"]["agent"] == "ok"
    assert world["control_health"]["checks"]["agent"] == "ok"


# CT-06

def test_ct06_the_candidate_api_with_the_candidate_agent_is_a_superset_of_the_control(world):
    assert h.compare_runs(world["control"], world["new_api"])["problems"] == []
    assert world["new_api_health"]["ok"] is True and world["new_api_health"]["checks"]["agent"] == "ok"
    assert h.judge(world, world["new_api"], agent_source()) == []
    assert h.judge(world, world["old_api"], agent_source()) == []


# The receipt

def test_the_receipt_says_pass_for_this_tree_carries_both_commits_and_a_version(world):
    runs = {"previous API with candidate agent": h.compare_runs(world["control"], world["old_api"]),
            "candidate API with candidate agent": h.compare_runs(world["control"], world["new_api"])}
    receipt = h.build_receipt("c" * 40, world["routes"], world["routes"] - world["candidate_routes"],
                              h.uncovered(world["routes"], world["control"]), runs, h.dead_agent_problems(world["dead"]))
    assert receipt["schema_version"] == 1 and receipt["verdict"] == "pass" and receipt["problems"] == []
    assert receipt["base_commit"] == h.BASE_COMMIT and receipt["candidate_commit"] == "c" * 40
    assert receipt["script_sha256"] == h.script_sha256() and receipt["forwarded_routes"] == len(world["routes"])


def test_the_receipt_says_fail_when_a_route_is_missing_or_a_run_has_a_problem():
    runs = {"run": {"problems": ["x: status 200 became 404"], "new_keys": ["$.n"]}}
    receipt = h.build_receipt("c" * 40, {("GET", "/api/a")}, {("GET", "/api/a")}, ["GET /api/b"], runs, ["dead differs"])
    assert receipt["verdict"] == "fail"
    assert len(receipt["problems"]) == 4
    assert receipt["new_keys"] == {"run": ["$.n"]}


# CT-07

MUTANTS = {
    "a route removed": ('@app.post("/api/watches/{watch_id}/pause")', '@app.post("/api/watches/{watch_id}/pause_gone")',
                        ["is not in the candidate agent route table", "watches pause: status 200 became 404"]),
    "a key renamed": ('return {"watches": current_watches()}', 'return {"watch_rows": current_watches()}',
                      ["watches list.watches: missing from the candidate"]),
    "a type changed": ('"content_hash": version["content_hash"], "ticks"', '"content_hash": len(version["content_hash"]), "ticks"',
                       ["content_hash: type changed from string to number"]),
    "an event renamed": (r'event: {kind}\ndata: {json.dumps(data)}', r'event: {kind}_x\ndata: {json.dumps(data)}',
                         ["ask events: events"]),
}


@pytest.mark.parametrize("name", list(MUTANTS))
def test_ct07_the_harness_fails_when_the_candidate_agent_is_mutated(world, tmp_path_factory, name):
    old, new, expected = MUTANTS[name]
    records, source = mutant_run(world, tmp_path_factory, old, new)
    problems = h.judge(world, records, source)
    assert problems, name
    for text in expected:
        assert any(text in p for p in problems), (text, problems)


def test_the_unmutated_candidate_has_no_problem_the_mutants_are_judged_against(world):
    assert h.judge(world, world["old_api"], agent_source()) == []


# The pieces the world run does not reach

def test_shapes_name_every_scalar_kind():
    assert [h.shape(v) for v in (True, 1, 2.5, None, "x")] == ["boolean", "number", "number", "null", "string"]
    assert h.shape([{"a": 1}, {"a": 2}, {"a": "x"}]) == [{"a": "number"}, {"a": "string"}]


def test_a_forwarded_route_is_covered_only_by_a_call_that_matches_its_whole_path_and_method():
    routes = {("GET", "/api/ask/{}"), ("POST", "/api/ask/{}/stop")}
    calls = [{"method": "GET", "path": "/api/ask/a_1/events"}, {"method": "GET", "path": "/api/ask/a_1/stop"}]
    assert h.uncovered(routes, calls) == ["GET /api/ask/{}", "POST /api/ask/{}/stop"]
    calls += [{"method": "GET", "path": "/api/ask/a_1"}, {"method": "POST", "path": "/api/ask/a_1/stop"}]
    assert h.uncovered(routes, calls) == []


def test_runs_over_different_requests_or_statuses_or_event_lists_do_not_compare_equal():
    one = {"label": "x", "method": "GET", "path": "/x", "status": 200, "content_type": "text/event-stream", "kind": "sse", "events": ["step", "done"]}
    assert h.compare_runs([one], [one]) == {"problems": [], "new_keys": []}
    assert h.compare_runs([one], [{**one, "label": "y"}])["problems"] == ["the two runs did not cover the same requests"]
    assert h.compare_runs([one], [{**one, "status": 404}])["problems"] == ["x: status 200 became 404"]
    assert h.compare_runs([one], [{**one, "events": ["done", "step"]}])["problems"]
    assert h.compare_runs([one], [{**one, "kind": "json", "shape": {}}])["problems"]


def test_a_502_that_differs_between_the_two_apis_is_a_problem():
    same = [{"status": 502, "shape": {"error": "string"}}] * 2
    assert h.dead_agent_problems({"previous API": same, "candidate API": same}) == []
    other = [{"status": 502, "shape": {"error": "string", "x": "string"}}] * 2
    assert h.dead_agent_problems({"previous API": same, "candidate API": other})
    wrong = [{"status": 500, "shape": {"error": "string"}}] * 2
    assert h.dead_agent_problems({"previous API": wrong, "candidate API": wrong})


def test_judge_names_a_missing_route_an_uncovered_route_and_a_changed_status():
    record = {"label": "x", "method": "GET", "path": "/x", "status": 200, "content_type": "application/json", "kind": "json", "shape": {}}
    data = {"routes": {("GET", "/x"), ("GET", "/y")}, "control": [record]}
    problems = h.judge(data, [{**record, "status": 500}], '@app.get("/x")\ndef x(): ...\n')
    assert problems == ["route GET /y is not in the candidate agent route table", "route GET /y is not covered by the script", "x: status 200 became 500"]
