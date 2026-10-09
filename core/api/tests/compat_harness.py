"""The compatibility harness (W8-REL 3.2): the previous API with the candidate agent, and the candidate API with the
candidate agent, against the control (the previous API with the previous agent).

Offline, localhost sockets only. Two source trees: a read-only `git archive` of the previous release commit and the
candidate worktree. Each side runs as its own process from its own tree with the fixture agent and fixture data.

The script, the route derivation and the comparison live here so that the tests and the receipt use the same code."""
import ast
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[3]
BASE_COMMIT = "a80be1d"
PASSCODE = "compat-passcode"
HEADERS = {"X-Passcode": PASSCODE}
SELF = Path(__file__).resolve()


# Routes

def _template(node):
    """A path expression as a template: placeholders become {} so names do not matter."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{}" for p in node.values)
    return None


def _normal(path):
    out, depth = [], 0
    for char in path:
        if char == "{":
            depth += 1
            if depth == 1:
                out.append("{}")
        elif char == "}":
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def forwarded_routes(app_source):
    """{(method, path template)} of every call the API makes to the agent: `_forward(method, path)` and `_relay_events(path)`."""
    found = set()
    for node in ast.walk(ast.parse(app_source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "_forward" and len(node.args) >= 2:
            method, path = node.args[0], _template(node.args[1])
            if isinstance(method, ast.Constant) and path:
                found.add((method.value, _normal(path)))
        elif node.func.id == "_relay_events" and node.args:
            path = _template(node.args[0])
            if path:
                found.add(("GET", _normal(path)))
    return found


def agent_routes(agent_source):
    """{(method, path template)} the agent registers with `@app.<method>(path)` or `@app.api_route(path, methods=[...])`."""
    found = set()
    for node in ast.walk(ast.parse(agent_source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.args):
                continue
            path = _template(deco.args[0])
            if path is None:
                continue
            verb = deco.func.attr
            if verb in ("get", "post", "put", "delete", "patch"):
                found.add((verb.upper(), _normal(path)))
            elif verb == "api_route":
                for kw in deco.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        found.update((m.value, _normal(path)) for m in kw.value.elts if isinstance(m, ast.Constant))
    return found


# Shapes

def shape(value):
    """object to {key: shape}; array to the sorted unique element shapes; scalars to a tag."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        unique = {json.dumps(shape(item), sort_keys=True) for item in value}
        return [json.loads(item) for item in sorted(unique)]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def compare_shapes(control, candidate, where="$"):
    """(problems, new keys). A key missing from the candidate fails; a changed non-null type fails; a value that becomes
    null where the control had one fails; new keys pass and are listed."""
    problems, added = [], []
    if isinstance(control, dict):
        if not isinstance(candidate, dict):
            return [f"{where}: control is an object, candidate is {candidate if isinstance(candidate, str) else 'an array'}"], added
        for key, item in control.items():
            if key not in candidate:
                problems.append(f"{where}.{key}: missing from the candidate")
                continue
            sub_problems, sub_added = compare_shapes(item, candidate[key], f"{where}.{key}")
            problems += sub_problems
            added += sub_added
        added += [f"{where}.{key}" for key in candidate if key not in control]
        return problems, added
    if isinstance(control, list):
        if not isinstance(candidate, list):
            return [f"{where}: control is an array, candidate is {candidate if isinstance(candidate, str) else 'an object'}"], added
        for index, item in enumerate(control):
            if not any(not compare_shapes(item, other, where)[0] for other in candidate):
                problems.append(f"{where}[{index}]: no candidate element has this shape")
        for other in candidate:
            if not any(not compare_shapes(item, other, where)[0] for item in control):
                added.append(f"{where}[]")
                break
        return problems, added
    if control == "null":
        return problems, added
    if candidate == "null":
        problems.append(f"{where}: became null where the control had a {control}")
    elif control != candidate:
        problems.append(f"{where}: type changed from {control} to {candidate}")
    return problems, added


def compare_runs(control, candidate):
    """Compare two recorded runs: {"problems": [...], "new_keys": [...]}. Statuses must match for every request."""
    problems, added = [], []
    if [s["label"] for s in control] != [s["label"] for s in candidate]:
        return {"problems": ["the two runs did not cover the same requests"], "new_keys": []}
    for left, right in zip(control, candidate):
        label = left["label"]
        if left["status"] != right["status"]:
            problems.append(f"{label}: status {left['status']} became {right['status']}")
            continue
        if left["kind"] != right["kind"]:
            problems.append(f"{label}: body kind {left['kind']} became {right['kind']}")
        elif left["kind"] == "sse":
            if left["events"] != right["events"]:
                problems.append(f"{label}: events {left['events']} became {right['events']}")
        elif left["kind"] == "json":
            sub_problems, sub_added = compare_shapes(left["shape"], right["shape"], label)
            problems += sub_problems
            added += sub_added
        elif left["content_type"] != right["content_type"]:
            problems.append(f"{label}: content type {left['content_type']} became {right['content_type']}")
    return {"problems": problems, "new_keys": added}


# The fixed script

WATCH = {"target": {"kind": "hashtag", "value": "#amapiano"}, "market": "ZA", "rule": {"state_in": ["rising"]},
         "label": "Amapiano in South Africa"}
SCHEDULE = {"question": "What is behind #fixture in South Africa?", "market": "ZA", "tier": "T1", "cadence": "daily",
            "deliver": ["in_app"]}
SKIN = {"skin_key": "bsa", "name": "Brand South Africa", "markets": ["ZA"], "terms": ["taxi fares"],
        "hashtags": ["#fixture_za_step"], "accounts": [], "watch_ids": [], "template": "weekly_report"}
QUESTION = {"question": "What is behind #fixture in South Africa?", "market": "ZA", "tier": "T1"}
STORED_ASK = "a_20260930_fixture_failed"  # in the API fixture runs, held by no agent: the 404 replay path
DRAFT = {"question": "What is behind #fixture in South Africa?", "market": "ZA"}


def _record(label, response):
    kind = response.headers.get("content-type", "").split(";")[0].strip()
    base = {"label": label, "method": response.request.method, "path": response.request.url.path,
            "status": response.status_code, "content_type": kind}
    if kind == "text/event-stream":
        names = [line[6:].strip() for line in response.text.splitlines() if line.startswith("event:")]
        return {**base, "kind": "sse", "events": names}
    if kind == "application/json":
        try:
            return {**base, "kind": "json", "shape": shape(response.json())}
        except ValueError:
            return {**base, "kind": "text"}
    return {**base, "kind": "text"}


class Script:
    """Every forwarded route, in a fixed order, against one base URL. Ids come from earlier responses."""

    def __init__(self, client):
        self.client = client
        self.records = []
        self.ids = {}

    def call(self, label, method, path, headers=None, **kwargs):
        response = self.client.request(method, path, headers={**HEADERS, **(headers or {})}, **kwargs)
        self.records.append(_record(label, response))
        return response

    def field(self, response, key):
        try:
            return str(response.json().get(key, "missing"))
        except (ValueError, AttributeError):
            return "missing"

    def wait_for_ask(self, ask_id):
        for _ in range(60):
            body = self.client.get(f"/api/ask/{ask_id}", headers=HEADERS)
            if body.status_code == 200 and body.json().get("status") not in (None, "running"):
                return
            time.sleep(0.5)

    def run(self):
        c = self.call
        watch = self.field(c("watches create", "POST", "/api/watches", json=WATCH), "watch_id")
        c("watches list", "GET", "/api/watches")
        c("watches pause", "POST", f"/api/watches/{watch}/pause")
        c("watches resume", "POST", f"/api/watches/{watch}/resume")
        c("watches bad body", "POST", "/api/watches", content=b"not json", headers={"Content-Type": "application/json"})
        schedule = self.field(c("schedules create", "POST", "/api/schedules", json=SCHEDULE), "schedule_id")
        c("schedules list", "GET", "/api/schedules")
        c("schedules pause", "POST", f"/api/schedules/{schedule}/pause")
        c("schedules resume", "POST", f"/api/schedules/{schedule}/resume")
        skin = self.field(c("skins create", "POST", "/api/skins", json=SKIN), "skin_id")
        c("skins list", "GET", "/api/skins")
        c("skins read", "GET", f"/api/skins/{skin}")
        c("skins edit", "PUT", f"/api/skins/{skin}", json={**SKIN, "name": "Brand SA"})
        c("skins read missing", "GET", "/api/skins/sk_missing")
        c("skins archive", "POST", f"/api/skins/{skin}/archive")

        ask = self.field(c("ask create", "POST", "/api/ask", json=QUESTION), "ask_id")
        self.wait_for_ask(ask)
        c("ask read", "GET", f"/api/ask/{ask}")
        c("ask read missing", "GET", "/api/ask/a_20990101_00000000")
        c("ask events", "GET", f"/api/ask/{ask}/events")
        c("ask events resume", "GET", f"/api/ask/{ask}/events", headers={"Last-Event-ID": "1"})
        c("ask events replay of a missing ask", "GET", "/api/ask/a_20990101_00000000/events")
        c("ask read stored", "GET", f"/api/ask/{STORED_ASK}")
        c("ask events replay of a stored ask", "GET", f"/api/ask/{STORED_ASK}/events")
        c("ask events replay of a stored ask resume", "GET", f"/api/ask/{STORED_ASK}/events", headers={"Last-Event-ID": "1"})
        c("ask export", "GET", f"/api/ask/{ask}/export?format=html")
        c("ask export stored", "GET", f"/api/ask/{STORED_ASK}/export?format=html")
        c("ask stop", "POST", f"/api/ask/{ask}/stop")
        c("ask empty body", "POST", "/api/ask", json={})
        c("feedback", "POST", "/api/feedback", json={"target": {"kind": "claim", "answer_id": ask, "claim_id": "c1"}, "value": "useful"})
        c("feedback bad body", "POST", "/api/feedback", json={})

        dossier = self.field(c("dossiers create", "POST", "/api/dossiers", json={"from": {"ask_id": ask}, "title": "Tab A"}), "dossier_id")
        c("dossiers list", "GET", "/api/dossiers?limit=5")
        read = c("dossiers read", "GET", f"/api/dossiers/{dossier}")
        for need in (read.json().get("needs_tick") or []) if read.status_code == 200 else []:
            c("dossiers tick needed claim", "POST", f"/api/dossiers/{dossier}/ticks", json={"claim_id": need["claim_id"], "ticked": True, "note": None})
        c("dossiers edit", "PUT", f"/api/dossiers/{dossier}", json={"title": "Tab B", "from_version": 1})
        c("dossiers edit stale", "PUT", f"/api/dossiers/{dossier}", json={"title": "Tab C", "from_version": 1})
        c("dossiers tick", "POST", f"/api/dossiers/{dossier}/ticks", json={"claim_id": "c1", "ticked": True, "note": "checked"})
        c("dossiers freeze stale", "POST", f"/api/dossiers/{dossier}/freeze", json={"from_version": 1})
        c("dossiers freeze", "POST", f"/api/dossiers/{dossier}/freeze", json={"from_version": 2})
        c("dossiers version", "GET", f"/api/dossiers/{dossier}/versions/3")
        c("dossiers export", "GET", f"/api/dossiers/{dossier}/versions/3/export?format=html")
        c("dossiers read missing", "GET", "/api/dossiers/d_000000000000")
        c("findings save", "POST", "/api/findings", json={"from": {"ask_id": ask}})
        c("suppressions create", "POST", "/api/suppressions", json={"creator_id": "c_ng_macro", "reason": "Asked to be left out.", "who": "Thandi"})

        draft = c("investigations draft", "POST", "/api/investigations", json=DRAFT)
        investigation = self.field(draft, "investigation_id")
        plan = draft.json().get("plan", {}) if draft.status_code == 201 else {}
        c("investigations list", "GET", "/api/investigations")
        c("investigations list by status", "GET", "/api/investigations?status=draft")
        c("investigations read", "GET", f"/api/investigations/{investigation}")
        c("investigations plan", "PUT", f"/api/investigations/{investigation}/plan", json={"plan": plan})
        c("investigations events before start", "GET", f"/api/investigations/{investigation}/events")
        c("investigations start", "POST", f"/api/investigations/{investigation}/start")
        c("investigations events", "GET", f"/api/investigations/{investigation}/events")
        c("investigations events resume", "GET", f"/api/investigations/{investigation}/events", headers={"Last-Event-ID": "2"})
        c("investigations events missing", "GET", "/api/investigations/i_missing/events")
        c("investigations read missing", "GET", "/api/investigations/i_missing")
        c("investigations stop", "POST", f"/api/investigations/{investigation}/stop")
        return self.records


def script_sha256():
    """The hash of the script and the harness together: a change to either changes the receipt."""
    return hashlib.sha256(SELF.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


# Processes

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def archive(commit, dest):
    """A read-only `git archive` of core/ at `commit`, extracted into `dest`."""
    tar = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "core"], capture_output=True, check=True).stdout
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tar)) as bundle:
        bundle.extractall(dest, filter="data")
    return dest


def _guard_directories():
    """The directories on this interpreter's PYTHONPATH that hold a sitecustomize.py: the offline guard it loaded, if any."""
    entries = [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry]
    return [os.path.abspath(entry) for entry in entries if (Path(entry) / "sitecustomize.py").is_file()]


# A child is started with -s (no user site) and never with -I, -E or -S: those would keep the offline guard from loading. Its
# environment is built from a short list, so no other PYTHON setting reaches it, and the guard directory is the one entry of
# PYTHONPATH it keeps, made absolute against this process's working directory because the child runs somewhere else. The guard's
# log and the name of the running test are kept when set, so a refusal inside a child reaches the same log as one in this process.
def _environment(extra):
    keep = {key: os.environ[key] for key in ("SYSTEMROOT", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE", "CORE_OFFLINE_GUARD_LOG", "PYTEST_CURRENT_TEST")
            if key in os.environ}
    if "CORE_OFFLINE_GUARD_LOG" in keep:
        keep["CORE_OFFLINE_GUARD_LOG"] = os.path.abspath(keep["CORE_OFFLINE_GUARD_LOG"])
    guard = _guard_directories()
    return {**keep, **({"PYTHONPATH": os.pathsep.join(guard)} if guard else {}), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", **extra}


class Server:
    """One uvicorn process serving `module:app` from `tree`, started with `python -s` and `--app-dir tree`."""

    def __init__(self, tree, module, env, log_dir, health="/health"):
        self.tree, self.module, self.env, self.health, self.port = Path(tree), module, env, health, free_port()
        self.log = Path(log_dir) / f"{module.split(':')[0].replace('.', '_')}-{self.port}.log"
        self.process = None
        self.any_status = False  # a server whose health reports its dead agent still answers

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        path = self.health
        handle = open(self.log, "wb")
        self.process = subprocess.Popen(
            [sys.executable, "-s", "-m", "uvicorn", "--app-dir", str(self.tree), self.module, "--host", "127.0.0.1",
             "--port", str(self.port), "--log-level", "warning"],
            cwd=self.tree, env=_environment(self.env), stdout=handle, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.module} from {self.tree} exited with {self.process.returncode}: {self.log.read_text(errors='replace')[-600:]}")
            try:
                if httpx.get(self.url + path, timeout=2).status_code == 200 or self.any_status:
                    return self
            except httpx.HTTPError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"{self.module} from {self.tree} did not answer {path}")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def agent_server(tree, log_dir):
    return Server(tree, "core.api.agent_app:app", {"F42_AGENT": "fixture", "F42_DATA": "fixtures"}, log_dir)


def api_server(tree, agent_url, log_dir):
    return Server(tree, "core.api.app:app", {"F42_DATA": "fixtures", "AGENT_URL": agent_url, "UI_PASSCODE": PASSCODE}, log_dir, "/api/health")


def run_pair(api_tree, agent_tree, log_dir):
    """Start the agent, then the API in front of it, run the script, stop both. Returns (records, health)."""
    with agent_server(agent_tree, log_dir) as agent, api_server(api_tree, agent.url, log_dir) as api:
        with httpx.Client(base_url=api.url, timeout=30) as client:
            health = client.get("/api/health").json()
            records = Script(client).run()
    return records, health


def run_api_against_a_dead_agent(api_tree, log_dir):
    """The 502 passthrough: the API with nothing listening at its agent URL."""
    dead = f"http://127.0.0.1:{free_port()}"
    server = api_server(api_tree, dead, log_dir)
    server.any_status, server.health = True, "/api/auth/verify"  # health waits on the dead agent; any answer says the API is up
    with server as api:
        with httpx.Client(base_url=api.url, timeout=30) as client:
            return [_record("watches list, agent down", client.get("/api/watches", headers=HEADERS)),
                    _record("ask create, agent down", client.post("/api/ask", json=QUESTION, headers=HEADERS))]


# Coverage and the receipt

def uncovered(routes, records):
    """The forwarded routes (method, template) no step of the script called: the script must cover every one."""
    called = {(r["method"], r["path"]) for r in records}
    missing = []
    for method, template in sorted(routes):
        pattern = re.compile("^" + "[^/]+".join(re.escape(part) for part in template.split("{}")) + "$")
        if not any(method == m and pattern.match(path) for m, path in called):
            missing.append(f"{method} {template}")
    return missing


def git_head():
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def build_receipt(candidate_commit, routes, missing_routes, uncalled, runs, dead):
    """compat-receipt.json (schema_version 1): both commits, the script and harness hash, the route diff, the new keys and the verdict."""
    problems = [f"{name}: {p}" for name, report in runs.items() for p in report["problems"]]
    problems += [f"route {method} {path} is not in the candidate agent route table" for method, path in sorted(missing_routes)]
    problems += [f"route {path} is not covered by the script" for path in uncalled]
    problems += [f"{p}" for p in dead]
    return {"schema_version": 1, "base_commit": BASE_COMMIT, "candidate_commit": candidate_commit,
            "script_sha256": script_sha256(), "forwarded_routes": len(routes),
            "route_diff": {"missing_from_candidate": sorted(list(m) for m in missing_routes), "not_covered": uncalled},
            "new_keys": {name: sorted(set(report["new_keys"])) for name, report in runs.items()},
            "problems": problems, "verdict": "pass" if not problems else "fail"}


def collect(candidate_tree, log_dir):
    """Run the whole comparison. Returns the data the tests and the receipt both read."""
    base = archive(BASE_COMMIT, Path(log_dir) / "base")
    routes = forwarded_routes((base / "core/api/app.py").read_text(encoding="utf-8"))
    candidate_routes = agent_routes((Path(candidate_tree) / "core/api/agent_app.py").read_text(encoding="utf-8"))
    control, control_health = run_pair(base, base, log_dir)
    old_api, old_api_health = run_pair(base, candidate_tree, log_dir)
    new_api, new_api_health = run_pair(candidate_tree, candidate_tree, log_dir)
    dead = {"previous API": run_api_against_a_dead_agent(base, log_dir), "candidate API": run_api_against_a_dead_agent(candidate_tree, log_dir)}
    return {"base": base, "routes": routes, "candidate_routes": candidate_routes, "control": control, "control_health": control_health,
            "old_api": old_api, "old_api_health": old_api_health, "new_api": new_api, "new_api_health": new_api_health, "dead": dead}


def judge(data, records, agent_source):
    """Every problem one candidate run has against the control: missing routes, routes the script missed, status and shape changes."""
    problems = [f"route {method} {path} is not in the candidate agent route table"
                for method, path in sorted(data["routes"] - agent_routes(agent_source))]
    problems += [f"route {route} is not covered by the script" for route in uncovered(data["routes"], records)]
    return problems + compare_runs(data["control"], records)["problems"]


def dead_agent_problems(dead):
    left, right = dead["previous API"], dead["candidate API"]
    return [] if [(r["status"], r["shape"]) for r in left] == [(r["status"], r["shape"]) for r in right] and all(r["status"] == 502 for r in left)         else ["the 502 passthrough differs between the previous and the candidate API"]


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Write compat-receipt.json for the candidate worktree.")
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        data = collect(ROOT, tmp)
        runs = {"previous API with candidate agent": compare_runs(data["control"], data["old_api"]),
                "candidate API with candidate agent": compare_runs(data["control"], data["new_api"])}
        missing = data["routes"] - data["candidate_routes"]
        receipt = build_receipt(git_head(), data["routes"], missing, uncovered(data["routes"], data["control"]), runs,
                                dead_agent_problems(data["dead"]))
    Path(args.receipt).write_text(json.dumps(receipt, indent=2, sort_keys=True) + chr(10), encoding="utf-8")
    print(f"compat verdict: {receipt['verdict']}")
    return 0 if receipt["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
