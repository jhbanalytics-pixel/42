"""Old readers against candidate records (W8-REL 4.4): the corpus, the previous release's readers run on it, the JSON path
check and the reverse direction.

Layers
1  the corpus: each candidate writer on its fixture path (the fixture agent, its in-memory sink), every typed state, the new
   keys present and absent
2  the previous release's API, started from a read-only `git archive`, serving the corpus as its fixture data: no 5xx,
   required fields present with unchanged types, every extra key declared
3  every `JSON_VALUE(r.record, '$.<path>')` path the previous release reads: the type each corpus record gives is a type a
   legacy record gives
4  the candidate readers on records the previous writers wrote: a record without the typed state is never a stated outcome"""
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from core.api.tests import compat_harness as ch

ROOT = ch.ROOT
RECORD_KEYS = {"answer_meta"}  # the one top-level key the record gains (C1 2.2); the dossier keys are not reachable offline

DRIVER = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ.update(F42_AGENT="fixture", F42_DATA="fixtures", F42_FIXTURE_DELAY="0.01")
for name in ("ASK_DAILY", "MODEL_DAILY_USD"):
    os.environ.pop(name, None)
from fastapi.testclient import TestClient
from core.api import agent_app

client = TestClient(agent_app.app)


def ask(question):
    return client.post("/api/ask", json={"question": question, "market": "ZA", "tier": "T1"}).json()["ask_id"]


def settle(ask_id):
    for _ in range(100):
        if client.get(f"/api/ask/{ask_id}").json().get("status") != "running":
            return
        time.sleep(0.1)


ids = []
for question in ("What is behind #fixture in South Africa?", "What is the thin story on #fixture in South Africa?",
                 "Why did #fixture fail in South Africa?"):
    ids.append(ask(question))
    settle(ids[-1])
os.environ["F42_FIXTURE_DELAY"] = "0.3"
ids.append(ask("What is behind #fixture in South Africa?"))
time.sleep(0.5)
client.post(f"/api/ask/{ids[-1]}/stop")
settle(ids[-1])
for _ in range(50):
    if len(agent_app.SINK) >= len(ids):
        break
    time.sleep(0.1)
sys.stdout.write("CORPUS " + json.dumps(agent_app.SINK) + "\n")
'''


# Layer 1: the corpus

def written_rows(tree):
    """The runs rows a writer tree leaves behind on its fixture path: complete, partial, failed and stopped Asks."""
    done = subprocess.run([sys.executable, "-I", "-c", DRIVER, str(tree)], cwd=tree, env=ch._environment({}),
                          capture_output=True, encoding="utf-8", timeout=120)
    lines = [line for line in done.stdout.splitlines() if line.startswith("CORPUS ")]
    if done.returncode != 0 or len(lines) != 1:
        raise RuntimeError(f"the corpus driver failed in {tree}: {done.stderr[-600:]}")
    return [as_fixture_row(row) for row in json.loads(lines[0][7:])]


def as_fixture_row(row):
    """A sink row as the fixture store holds it: the JSON columns decoded."""
    out = dict(row)
    for key in ("record", "answer"):
        if isinstance(out.get(key), str):
            out[key] = json.loads(out[key])
    return out


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_meta(record, execution, stop_reason, summary, removals, rewrite):
    """A stored answer_meta object (C1 2.2) for a record: the version, the ids, the states, the bound facts and the digest."""
    answer = record.get("answer")
    meta = {"v": 1, "ask_id": record["ask_id"], "check_run_id": (record.get("run") or {}).get("run_id"),
            "execution": {"state": execution, "stop_reason": stop_reason},
            "summary": {"state": summary, "removals": removals, "rewrite": rewrite},
            "bound": {"answer_status": answer.get("status") if answer else None,
                      "summary_blank": (not str(answer.get("short_answer") or "").strip()) if answer else None,
                      "claims": len(answer.get("claims") or []) if answer else None}}
    meta["digest"] = "sha256:" + hashlib.sha256(canonical(meta).encode("utf-8")).hexdigest()
    return meta


# (base row, execution state, stop reason, summary state, removals, rewrite): every execution state and every summary state
STATES = [
    ("complete", "completed", None, "shown", [], "not_attempted"),
    ("complete", "completed", None, "shown_rewritten", [], "kept"),
    ("complete", "completed", None, "fixed_text", [], "not_attempted"),
    ("partial", "completed", None, "removed", [{"stage": "first_check", "cause": "K6"}], "not_attempted"),
    ("partial", "completed", None, "blank_unexplained", [], "not_attempted"),
    ("failed", "failed", None, "no_answer", [], "not_attempted"),
    ("stopped", "stopped_on_request", None, "no_answer", [], "not_attempted"),
    ("stopped", "stopped_on_budget", "budget_full", "no_answer", [], "not_attempted"),
    ("stopped", "refused_budget_spent", None, "no_answer", [], "not_attempted"),
]


def kind_of(row):
    record = row["record"]
    if record["status"] == "failed":
        return "failed"
    if record["status"] == "stopped":
        return "stopped"
    return "partial" if (record.get("answer") or {}).get("status") == "partial" else "complete"


def corpus(written):
    """(rows, twin_of): the written rows (the key absent), then one clone per state with answer_meta present. twin_of maps
    each clone's ask id to the ask id of the written row it was cloned from."""
    base = {kind_of(row): row for row in written}
    assert set(base) == {"complete", "partial", "failed", "stopped"}, sorted(base)
    rows, twin_of = [copy.deepcopy(row) for row in written], {}
    for number, (kind, execution, stop_reason, summary, removals, rewrite) in enumerate(STATES, 1):
        clone = copy.deepcopy(base[kind])
        ask_id = f"a_20261009_meta{number:02d}"
        twin_of[ask_id] = clone["record"]["ask_id"]
        clone["record"]["ask_id"] = ask_id
        clone["run_id"] = ask_id
        clone["record"]["answer_meta"] = make_meta(clone["record"], execution, stop_reason, summary, removals, rewrite)
        rows.append(clone)
    return rows, twin_of


def ask_id_of(row):
    return row["record"]["ask_id"]


# Layer 2: the previous release's readers

class OldReader:
    """The previous API and the previous agent from a git archive, the corpus rows written into its fixture runs table."""

    def __init__(self, tree, log_dir):
        self.tree, self.log_dir = Path(tree), log_dir
        self.runs = self.tree / "core/api/fixtures/runs.json"
        self.original = json.loads(self.runs.read_text(encoding="utf-8"))
        self.agent = self.api = None

    def __enter__(self):
        self.agent = ch.agent_server(self.tree, self.log_dir).start()
        self.api = ch.api_server(self.tree, self.agent.url, self.log_dir).start()
        return self

    def __exit__(self, *exc):
        for server in (self.api, self.agent):
            if server:
                server.stop()

    def load(self, rows):
        self.runs.write_text(json.dumps(self.original + rows), encoding="utf-8")

    def read(self, ask_ids):
        """{ask_id: {route: record}} for the three routes that read a stored Ask, plus Today."""
        out = {}
        with httpx.Client(base_url=self.api.url, timeout=30) as client:
            for ask_id in ask_ids:
                out[ask_id] = {}
                for route, path in (("ask read", f"/api/ask/{ask_id}"), ("ask events", f"/api/ask/{ask_id}/events"),
                                   ("ask export", f"/api/ask/{ask_id}/export?format=html")):
                    out[ask_id][route] = ch._record(route, client.get(path, headers=ch.HEADERS))
            out["today"] = {"today": ch._record("today", client.get("/api/today", headers=ch.HEADERS))}
        return out


def layer2_problems(reads, twin_of, declared=RECORD_KEYS):
    """No 5xx; per route the status and shape of a clone equal its written twin's, new keys only declared ones."""
    problems = []
    for ask_id, routes in reads.items():
        for route, got in routes.items():
            if got["status"] >= 500:
                problems.append(f"{ask_id} {route}: status {got['status']}")
            twin = reads.get(twin_of.get(ask_id), {}).get(route)
            if twin is None:
                continue
            if got["status"] != twin["status"]:
                problems.append(f"{ask_id} {route}: status {twin['status']} became {got['status']}")
            elif got["kind"] == "json":
                missing, added = ch.compare_shapes(twin["shape"], got["shape"], route)
                problems += [f"{ask_id} {p}" for p in missing]
                problems += [f"{ask_id} {key}: undeclared key" for key in added if key not in {f"{route}.{name}" for name in declared}]
            elif got["kind"] == "sse" and got["events"] != twin["events"]:
                problems.append(f"{ask_id} {route}: events {twin['events']} became {got['events']}")
    return problems


# Layer 3: JSON paths

PATH = re.compile(r"JSON_VALUE\(\s*(?:\w+\.)?record\s*,\s*'\$\.([A-Za-z0-9_.]+)'\s*\)")


def a80_record_paths(tree):
    """Every `$.<path>` read from a stored Ask record by JSON_VALUE in the previous release's non-test sources."""
    found = set()
    for path in sorted(Path(tree, "core").rglob("*.py")):
        if "tests" in path.parts:
            continue
        found.update(PATH.findall(path.read_text(encoding="utf-8")))
    return found


def type_tag(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string" if isinstance(value, str) else "object" if isinstance(value, dict) else "array"


def at_path(record, path):
    for part in path.split("."):
        if not isinstance(record, dict) or part not in record:
            return "missing"
        record = record[part]
    return type_tag(record)


def layer3_problems(legacy_rows, rows, paths):
    """Each path gives, on every corpus record, a type some legacy record gives there (null only where a legacy one is null)."""
    allowed = {path: {at_path(row["record"], path) for row in legacy_rows} for path in paths}
    return [f"{ask_id_of(row)} ${'.' + path}: {at_path(row['record'], path)} is not one of {sorted(allowed[path])}"
            for row in rows for path in sorted(paths) if at_path(row["record"], path) not in allowed[path]]


# Layer 4: the candidate readers on the previous writers' records

def copy_tree(source, dest):
    shutil.copytree(Path(source) / "core", Path(dest) / "core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"))
    return Path(dest)


def reverse_problems(reads):
    """A record the previous writers wrote has no typed state; a reader gives none, null, or legacy_unknown, never an outcome."""
    problems = []
    for ask_id, routes in reads.items():
        got = routes.get("ask read")
        if got is None or got["kind"] != "json":
            continue
        meta = got.get("meta", "absent")
        if meta not in ("absent", "null") and meta != {"check": "legacy_unknown"}:
            problems.append(f"{ask_id}: a record with no typed state reads as {meta}")
    return problems


def read_with_meta(reader, ask_ids):
    """reader.read plus, for the Ask read, the answer_meta the API gave (the shape alone cannot tell the values apart)."""
    reads = reader.read(ask_ids)
    with httpx.Client(base_url=reader.api.url, timeout=30) as client:
        for ask_id in ask_ids:
            body = client.get(f"/api/ask/{ask_id}", headers=ch.HEADERS).json()
            reads[ask_id]["ask read"]["meta"] = "absent" if "answer_meta" not in body else "null" if body["answer_meta"] is None else body["answer_meta"]
    return reads
