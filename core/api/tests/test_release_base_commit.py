"""The compatibility and old-reader harnesses compare the candidate with the prior release's commit, and that commit is a parameter.

Release A compared with a80be1d, so a80be1d stays the default. Release C's rollback target is Release A (3710c5e), so the commit the
harnesses archive and write into their receipts comes from the prior release record, through --base-commit. The two real runs at
the end are the evidence the lead asked for: the readers of A read what the current tree writes, and the tree reads what A wrote."""
import json
import subprocess

import httpx
import pytest

from core.api.tests import compat_harness as ch
from core.api.tests import old_reader_harness as h

A = "3710c5e"
ROOT = ch.ROOT


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout


EMPTY_COMPAT = {"routes": set(), "candidate_routes": set(), "control": [], "old_api": [], "new_api": [],
                "dead": {"previous API": [], "candidate API": []}}
EMPTY_READERS = {"rows": [], "legacy": [], "paths": [], "reads": {}, "reverse": {}, "twin_of": {}}


def test_the_default_base_is_still_a80be1d():
    assert ch.BASE_COMMIT == "a80be1d"
    assert ch.build_receipt("c" * 40, set(), set(), [], {}, [])["base_commit"] == "a80be1d"


def test_the_compat_receipt_names_the_base_it_was_given():
    receipt = ch.build_receipt("c" * 40, set(), set(), [], {}, [], base_commit=A)
    assert receipt["base_commit"] == A and receipt["candidate_commit"] == "c" * 40


def test_the_old_reader_receipt_names_the_base_it_was_given(monkeypatch):
    monkeypatch.setattr(h, "layer_problems", lambda data: {})
    assert h.build_receipt("c" * 40, EMPTY_READERS)["base_commit"] == "a80be1d"
    assert h.build_receipt("c" * 40, EMPTY_READERS, base_commit=A)["base_commit"] == A


@pytest.mark.parametrize("text", ["a80be1", "ZZZZZZZ", "HEAD", "3710c5e;rm", "3710c5e" + "0" * 34, ""])
@pytest.mark.parametrize("module", [ch, h])
def test_a_base_that_is_not_a_commit_is_refused_before_anything_runs(module, text, tmp_path):
    with pytest.raises(SystemExit) as stopped:
        module.main(["--receipt", str(tmp_path / "r.json"), "--base-commit", text])
    assert stopped.value.code == 2 and not (tmp_path / "r.json").exists()


def test_the_compat_command_archives_and_records_the_base_it_is_given(monkeypatch, tmp_path):
    seen = []

    def collect(tree, log_dir, base_commit=ch.BASE_COMMIT):
        seen.append(base_commit)
        return EMPTY_COMPAT

    monkeypatch.setattr(ch, "collect", collect)
    target = tmp_path / "compat-receipt.json"
    assert ch.main(["--receipt", str(target), "--base-commit", A]) in (0, 1)
    assert seen == [A] and json.loads(target.read_text(encoding="utf-8"))["base_commit"] == A
    target2 = tmp_path / "default.json"
    ch.main(["--receipt", str(target2)])
    assert seen[-1] == "a80be1d" and json.loads(target2.read_text(encoding="utf-8"))["base_commit"] == "a80be1d"


def test_the_old_reader_command_archives_and_records_the_base_it_is_given(monkeypatch, tmp_path):
    seen = []

    def collect(tree, log_dir, base_commit=ch.BASE_COMMIT):
        seen.append(base_commit)
        return EMPTY_READERS

    monkeypatch.setattr(h, "collect", collect)
    monkeypatch.setattr(h, "changed_files", lambda root: [])
    monkeypatch.setattr(h, "layer_problems", lambda data: {})
    target = tmp_path / "old-reader-receipt.json"
    assert h.main(["--receipt", str(target), "--base-commit", A]) == 0
    assert seen == [A] and json.loads(target.read_text(encoding="utf-8"))["base_commit"] == A


def test_collect_archives_the_commit_it_is_given_and_never_the_default(monkeypatch, tmp_path):
    archived = []

    def archive(commit, dest):
        archived.append(commit)
        raise RuntimeError("stop after the archive")

    monkeypatch.setattr(ch, "archive", archive)
    for collect in (ch.collect, h.collect):
        with pytest.raises(RuntimeError):
            collect(ROOT, tmp_path, base_commit=A)
    assert archived == [A, A]


def test_the_tree_the_harness_archives_at_the_prior_commit_is_that_commit_and_not_a80(tmp_path):
    differing = [line for line in git("diff", "--name-only", ch.BASE_COMMIT, A, "--", "core").decode("utf-8").splitlines() if line.endswith(".py")]
    assert differing, "the prior release must differ from a80 in some file, or this test says nothing"
    tree = ch.archive(A, tmp_path / "base")
    for path in differing[:5]:
        shown = subprocess.run(["git", "-C", str(ROOT), "show", f"{A}:{path}"], capture_output=True).stdout
        if shown:
            assert (tree / path).read_bytes() == shown, path


# The evidence at Release A. Both harnesses run against the archive of 3710c5e. Release A's writers store answer_meta, so the old-reader
# harness derives that from them and judges layers 2 and 4 by the rules for such a previous release (test_old_readers_prior_meta.py holds
# the verdict); here the mechanics: the harness archives A, runs A's writers and A's API on the candidate's records, and records A as its base.

@pytest.fixture(scope="module")
def readers_at_a(tmp_path_factory):
    return h.collect(ROOT, tmp_path_factory.mktemp("readers-at-a"), base_commit=A)


def test_the_old_reader_harness_runs_release_a_writers_and_readers_and_records_a_as_its_base(readers_at_a):
    receipt = h.build_receipt(ch.git_head(), readers_at_a, base_commit=A)
    assert receipt["base_commit"] == A and receipt["candidate_commit"] == ch.git_head()
    assert receipt["legacy_rows"] > 0 and receipt["json_paths"] > 0 and receipt["corpus_rows"] > 0
    assert all(h.ask_id_of(row) in readers_at_a["reads"] for row in readers_at_a["layer2_rows"])
    assert all(h.ask_id_of(row) in readers_at_a["reverse"] for row in readers_at_a["legacy"])


def test_release_a_writers_store_the_typed_state_so_the_harness_judges_it_by_the_rules_for_a_release_that_does(readers_at_a):
    assert readers_at_a["legacy"] and all("answer_meta" in row["record"] for row in readers_at_a["legacy"])
    assert readers_at_a["prior_stores_answer_meta"] is True


@pytest.fixture(scope="module")
def compat_at_a(tmp_path_factory):
    return ch.collect(ROOT, tmp_path_factory.mktemp("compat-at-a"), base_commit=A)


def test_the_api_of_release_a_with_this_agent_and_this_api_with_this_agent_answer_as_release_a_with_its_own_agent(compat_at_a):
    data = compat_at_a
    runs = {"previous API with candidate agent": ch.compare_runs(data["control"], data["old_api"]),
            "candidate API with candidate agent": ch.compare_runs(data["control"], data["new_api"])}
    receipt = ch.build_receipt(ch.git_head(), data["routes"], data["routes"] - data["candidate_routes"],
                               ch.uncovered(data["routes"], data["control"]), runs, ch.dead_agent_problems(data["dead"]), base_commit=A)
    assert receipt["base_commit"] == A and receipt["problems"] == [] and receipt["verdict"] == "pass"
    assert receipt["forwarded_routes"] > 0


# Contract: the API of Release A in front of this tree's agent, when the agent answers with the reader view (merged claims, and the
# run.technical_gaps key the view adds). A's API forwards what the agent says and reads the answer for nothing but its status, so the
# new view must pass through it with every key A's API returns today still there and the one new key declared.

PRESENTED = "run.technical_gaps"


def through_release_a(api_tree, agent_tree, log_dir):
    """A's API in front of the agent of agent_tree, which answers every Ask with the repeat-comments record. Returns the recorded calls
    and the agent's own answer to the read."""
    with ch.agent_server(agent_tree, log_dir) as agent, ch.api_server(api_tree, agent.url, log_dir) as api:
        with httpx.Client(base_url=api.url, timeout=30) as through, httpx.Client(base_url=agent.url, timeout=30) as direct:
            script = ch.Script(through)
            ask = script.field(script.call("ask create", "POST", "/api/ask", json=ch.QUESTION), "ask_id")
            script.wait_for_ask(ask)
            read = script.call("ask read", "GET", f"/api/ask/{ask}")
            script.call("ask events", "GET", f"/api/ask/{ask}/events")
            script.call("ask export", "GET", f"/api/ask/{ask}/export?format=html")
            on_agent = direct.get(f"/api/ask/{ask}", headers=ch.HEADERS)
    return {"records": script.records, "read": read, "agent_read": on_agent}


def repeat_comments_tree(source, dest):
    """A copy of a tree whose fixture agent answers with the repeat-comments record (the fixture the reader view was built on)."""
    tree = h.copy_tree(source, dest)
    fixtures = tree / "core/api/fixtures"
    (fixtures / "ask_complete.json").write_bytes((ROOT / "core/api/fixtures/ask_comments_repeat.json").read_bytes())
    return tree


@pytest.fixture(scope="module")
def presented_through_release_a(tmp_path_factory, compat_at_a):
    tmp = tmp_path_factory.mktemp("a-api-presented")
    this_agent = repeat_comments_tree(ROOT, tmp / "agent-c")
    a_agent = repeat_comments_tree(compat_at_a["base"], tmp / "agent-a")
    return {"this": through_release_a(compat_at_a["base"], this_agent, tmp),
            "control": through_release_a(compat_at_a["base"], a_agent, tmp)}


def test_release_a_api_in_front_of_this_agent_returns_the_reader_view_unchanged(presented_through_release_a):
    read, agent_read = presented_through_release_a["this"]["read"], presented_through_release_a["this"]["agent_read"]
    assert read.status_code == 200 and agent_read.status_code == 200
    body = read.json()
    assert [claim["id"] for claim in body["answer"]["claims"]] == [claim["id"] for claim in agent_read.json()["answer"]["claims"]]
    assert body["run"]["technical_gaps"]
    assert body["answer"] == agent_read.json()["answer"] and body["run"]["technical_gaps"] == agent_read.json()["run"]["technical_gaps"]


def test_release_a_agent_over_the_same_record_shows_the_raw_claims_so_the_view_is_what_changed(presented_through_release_a):
    body = presented_through_release_a["control"]["read"].json()
    assert len(body["answer"]["claims"]) == 7 and "technical_gaps" not in body["run"]


def test_release_a_api_in_front_of_this_agent_keeps_every_key_it_returns_in_front_of_its_own_agent_and_gains_only_the_declared_one(presented_through_release_a):
    control = {r["label"]: r for r in presented_through_release_a["control"]["records"]}
    seen = {r["label"]: r for r in presented_through_release_a["this"]["records"]}
    for label in ("ask create", "ask read", "ask events", "ask export"):
        assert seen[label]["status"] == control[label]["status"] == (202 if label == "ask create" else 200), label
    assert seen["ask events"]["events"] == control["ask events"]["events"]
    missing, added = ch.compare_shapes(control["ask read"]["shape"], seen["ask read"]["shape"], "ask read")
    assert missing == [] and [key for key in added if key != f"ask read.{PRESENTED}"] == []


# The stored-shape proof for the durable-effects manifest of a release on top of A: the records this tree's writers leave on the fixture
# path (complete, partial, failed, stopped) hold exactly the key paths the records of Release A's writers hold, nested keys included.

def key_paths(value, prefix=""):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(f"{prefix}.{key}")
            found |= key_paths(item, f"{prefix}.{key}")
    elif isinstance(value, list):
        for item in value:
            found |= key_paths(item, prefix + "[]")
    return found


def test_the_records_this_tree_writes_hold_no_key_path_that_release_a_records_do_not_and_lack_none(readers_at_a):
    ours = h.written_rows(ROOT)
    theirs = readers_at_a["legacy"]
    assert len(ours) == len(theirs) > 0
    ours_paths = set().union(*(key_paths(row) for row in ours))
    theirs_paths = set().union(*(key_paths(row) for row in theirs))
    assert len(theirs_paths) > 100
    assert sorted(ours_paths - theirs_paths) == [] and sorted(theirs_paths - ours_paths) == []


def test_key_paths_name_nested_and_listed_keys():
    assert key_paths({"a": {"b": 1}, "c": [{"d": 2}, {"e": 3}]}) == {".a", ".a.b", ".c", ".c[].d", ".c[].e"}
