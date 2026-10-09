"""Old readers against candidate records (W8-REL 4.4, DE-05 to DE-07): the previous release's readers must still read what the
candidate writers store, and the candidate readers must not turn a record without the typed state into a stated outcome.

Real processes on localhost, so the deploy partition's rules apply: nothing here needs a network beyond the loopback."""
import copy
import os
from pathlib import Path

import pytest

from core.api.tests import compat_harness as ch
from core.api.tests import old_reader_harness as h

ROOT = h.ROOT
STORE = "core/api/store.py"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("oldreaders")
    base = ch.archive(ch.BASE_COMMIT, tmp / "base")
    legacy = h.written_rows(base)
    rows, twin_of = h.corpus(h.written_rows(ROOT))
    with h.OldReader(base, tmp) as reader:
        yield {"tmp": tmp, "base": base, "legacy": legacy, "rows": rows, "twin_of": twin_of, "reader": reader,
               "paths": h.a80_record_paths(base)}


def check(world, rows):
    """Layers 2 and 3 on a corpus: the previous readers' problems."""
    world["reader"].load(rows)
    reads = world["reader"].read([h.ask_id_of(row) for row in rows])
    return h.layer2_problems(reads, world["twin_of"]) + h.layer3_problems(world["legacy"], rows, world["paths"])


def clone(world, number=1):
    return f"a_20261009_meta{number:02d}"


def altered(world, change):
    """The corpus with `change(record)` applied to the first clone."""
    rows = copy.deepcopy(world["rows"])
    target = next(row for row in rows if h.ask_id_of(row) == clone(world))
    change(target["record"])
    return rows


# Layer 1: the corpus

def test_de05_the_corpus_holds_every_execution_and_summary_state_and_the_key_present_and_absent(world):
    metas = [row["record"]["answer_meta"] for row in world["rows"] if "answer_meta" in row["record"]]
    assert {m["execution"]["state"] for m in metas} == {"completed", "stopped_on_request", "stopped_on_budget", "refused_budget_spent", "failed"}
    assert {m["summary"]["state"] for m in metas} == {"shown", "shown_rewritten", "fixed_text", "removed", "blank_unexplained", "no_answer"}
    assert any("answer_meta" not in row["record"] for row in world["rows"])
    assert {h.kind_of(row) for row in world["rows"]} == {"complete", "partial", "failed", "stopped"}
    assert all(m["digest"].startswith("sha256:") and len(m["digest"]) == 71 for m in metas)


# Layers 2 and 3

def test_de05_the_previous_readers_give_no_5xx_declared_keys_and_legacy_compatible_types_on_the_corpus(world):
    assert check(world, world["rows"]) == []
    reads = world["reader"].read([h.ask_id_of(row) for row in world["rows"]])
    clones = [reads[a] for a in world["twin_of"] if a.startswith("a_20261009_meta")]  # the synthetic states, not the writers' own rows
    assert len(clones) == len(h.STATES)
    assert all(r["ask read"]["status"] == 200 for r in clones)
    assert all("answer_meta" in r["ask read"]["shape"] for r in clones)
    assert all(r["ask events"]["events"][-1] == "done" for r in clones)


def test_de05_the_json_paths_the_previous_release_reads_are_found_and_cover_the_ask_record(world):
    assert {"ask_id", "market", "question", "status", "answer.status", "run.model_usd"} <= world["paths"]
    assert h.layer3_problems(world["legacy"], world["rows"], world["paths"]) == []


def test_de05_a_key_the_consumers_do_not_declare_fails(world):
    problems = check(world, altered(world, lambda record: record.update(surprise=1)))
    assert any("ask read.surprise: undeclared key" in p for p in problems), problems


def test_de05_the_type_rules_of_layer_3():
    legacy = [{"record": {"status": "complete", "answer": {"status": "complete"}, "ask_id": "a", "run": {"model_usd": 0.5}}},
              {"record": {"status": "failed", "answer": None, "ask_id": "b", "run": {"model_usd": None}}}]
    paths = {"status", "answer.status", "run.model_usd"}
    assert h.layer3_problems(legacy, legacy, paths) == []
    wrong = [{"record": {"ask_id": "c", "status": 3, "answer": {"status": "complete"}, "run": {"model_usd": "0.5"}}}]
    assert len(h.layer3_problems(legacy, wrong, paths)) == 2
    gone = [{"record": {"ask_id": "d", "status": "complete"}}]
    assert h.layer3_problems(legacy, gone, paths) == ["d $.run.model_usd: missing is not one of ['null', 'number']"]


# Layer 4

@pytest.fixture(scope="module")
def reverse(world, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("reverse")
    tree = h.copy_tree(ROOT, tmp / "candidate")
    with h.OldReader(tree, tmp) as reader:
        reader.load(world["legacy"])
        yield h.read_with_meta(reader, [h.ask_id_of(row) for row in world["legacy"]])


def test_de06_the_candidate_readers_read_records_the_previous_writers_wrote_as_no_stated_outcome(world, reverse):
    assert len(reverse) == 5 and "today" in reverse
    assert [reverse[h.ask_id_of(row)]["ask read"]["status"] for row in world["legacy"]] == [200] * 4
    assert h.reverse_problems(reverse) == []


def test_de06_a_stated_outcome_on_a_legacy_record_is_a_problem():
    def read(meta):
        return {"a_1": {"ask read": {"kind": "json", "meta": meta}}}

    for allowed in ("absent", "null", {"check": "legacy_unknown"}):
        assert h.reverse_problems(read(allowed)) == []
    for stated in ({"check": "verified", "execution": {"state": "completed"}}, {"check": "unverified", "problem": "x"}, {"v": 1}, {}):
        assert h.reverse_problems(read(stated)) == [f"a_1: a record with no typed state reads as {stated}"]


def test_de06_a_candidate_reader_that_states_an_outcome_for_every_record_is_found(world, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("reverse_mutant")
    tree = h.copy_tree(ROOT, tmp / "candidate")
    path = tree / STORE
    source = path.read_text(encoding="utf-8")
    old = "    return record\n\n\nclass FixtureStore"
    assert source.count(old) == 1
    path.write_text(source.replace(old, '    return {**record, "answer_meta": {"check": "verified", "execution": {"state": "completed"}}} if isinstance(record, dict) else record\n\n\nclass FixtureStore'), encoding="utf-8")
    with h.OldReader(tree, tmp) as reader:
        reader.load(world["legacy"])
        reads = h.read_with_meta(reader, [h.ask_id_of(row) for row in world["legacy"]])
    assert len(h.reverse_problems(reads)) == 4


# DE-07: the corpus loses a legacy key and DE-05 fails

def test_de07_a_record_that_drops_a_required_legacy_key_fails_the_check(world):
    problems = check(world, altered(world, lambda record: record.pop("status")))
    assert any("ask read.status: missing from the candidate" in p for p in problems), problems
    assert any("$.status: missing is not one of" in p for p in problems), problems


def test_de07_a_record_that_drops_the_answer_fails_the_check(world):
    problems = check(world, altered(world, lambda record: record.pop("answer")))
    assert any("ask read.answer: missing from the candidate" in p for p in problems), problems


def test_de07_a_record_whose_list_becomes_a_string_fails_the_check(world):
    problems = check(world, altered(world, lambda record: record.update(steps="none")))
    assert any("ask read.steps" in p for p in problems), problems


def test_de07_a_record_whose_answer_status_changes_type_fails_the_check(world):
    problems = check(world, altered(world, lambda record: record["answer"].update(status=7)))
    assert any("$.answer.status: number is not one of" in p for p in problems), problems
    assert any("ask read.answer.status: type changed" in p for p in problems), problems


def test_de07_the_unaltered_corpus_still_passes_after_the_failing_ones(world):
    assert check(world, world["rows"]) == []


# The pieces the process runs do not reach

def read_of(status, shape=None, kind="json", events=None):
    return {"label": "r", "status": status, "kind": kind, "shape": shape or {}, "events": events or [], "content_type": ""}


def test_layer_2_rules_on_hand_made_reads():
    twin = {"ask read": read_of(200, {"a": "string", "b": "number"}), "ask events": read_of(200, kind="sse", events=["step", "done"])}
    ok = {"ask read": read_of(200, {"a": "string", "b": "number", "answer_meta": {"v": "number"}}), "ask events": twin["ask events"]}
    assert h.layer2_problems({"t": twin, "c": ok}, {"c": "t"}) == []
    assert h.layer2_problems({"t": twin, "c": {**ok, "ask read": read_of(500)}}, {"c": "t"}) == [
        "c ask read: status 500", "c ask read: status 200 became 500"]
    assert h.layer2_problems({"t": twin, "c": {**ok, "ask read": read_of(404)}}, {"c": "t"}) == ["c ask read: status 200 became 404"]
    lost = {**ok, "ask read": read_of(200, {"a": "string"})}
    assert h.layer2_problems({"t": twin, "c": lost}, {"c": "t"}) == ["c ask read.b: missing from the candidate"]
    retyped = {**ok, "ask read": read_of(200, {"a": "number", "b": "number"})}
    assert h.layer2_problems({"t": twin, "c": retyped}, {"c": "t"}) == ["c ask read.a: type changed from string to number"]
    extra = {**ok, "ask read": read_of(200, {"a": "string", "b": "number", "x": "string"})}
    assert h.layer2_problems({"t": twin, "c": extra}, {"c": "t"}) == ["c ask read.x: undeclared key"]
    nested = {**ok, "ask read": read_of(200, {"a": "string", "b": "number", "answer_meta": {}, "x": {"y": "string"}})}
    assert h.layer2_problems({"t": twin, "c": nested}, {"c": "t"}) == ["c ask read.x: undeclared key"]
    reordered = {**ok, "ask events": read_of(200, kind="sse", events=["done"])}
    assert h.layer2_problems({"t": twin, "c": reordered}, {"c": "t"}) == ["c ask events: events ['step', 'done'] became ['done']"]
    assert h.layer2_problems({"t": twin, "c": ok}, {}) == []


def test_the_path_pattern_reads_aliased_and_bare_record_columns_only():
    sample = ("JSON_VALUE(r.record, '$.ask_id') JSON_VALUE(record, '$.run.model_usd') JSON_VALUE(r.counts, '$.model_usd') "
              "JSON_QUERY(r.record, '$.x') JSON_VALUE( x.record , '$.a_b.c1' )")
    assert set(h.PATH.findall(sample)) == {"ask_id", "run.model_usd", "a_b.c1"}


def test_the_type_tags_and_path_reads():
    assert [h.type_tag(v) for v in (None, True, 1, 1.5, "s", {}, [])] == ["null", "boolean", "number", "number", "string", "object", "array"]
    assert [h.at_path({"a": {"b": 1}}, p) for p in ("a.b", "a.c", "x", "a.b.c")] == ["number", "missing", "missing", "missing"]


def test_the_corpus_clones_each_kind_once_per_state_and_keeps_the_written_rows_unchanged(world):
    rows, twin_of = world["rows"], world["twin_of"]
    written = [row for row in rows if "answer_meta" not in row["record"]]
    stored = len(twin_of) - len(h.STATES)  # written rows that already carry answer_meta (C1 lane 2), each with a key-absent twin
    assert stored in (0, 4) and len(written) == 4 and len(rows) == 4 + stored + len(h.STATES)
    by_id = {h.ask_id_of(row): row for row in rows}
    for clone_id, twin_id in twin_of.items():
        clone, twin = by_id[clone_id]["record"], by_id[twin_id]["record"]
        assert h.kind_of(by_id[clone_id]) == h.kind_of(by_id[twin_id])
        assert {k: v for k, v in clone.items() if k not in ("ask_id", "answer_meta")} == {k: v for k, v in twin.items() if k != "ask_id"}
        assert clone["answer_meta"]["ask_id"] == clone_id
    assert h.kind_of({"record": {"status": "complete", "answer": {"status": "partial"}}}) == "partial"
    assert h.kind_of({"record": {"status": "complete", "answer": {"status": "complete"}}}) == "complete"
    assert h.kind_of({"record": {"status": "failed"}}) == "failed" and h.kind_of({"record": {"status": "stopped"}}) == "stopped"


def test_make_meta_binds_the_facts_of_its_record_and_a_digest_of_the_rest():
    record = {"ask_id": "a_1", "run": {"run_id": "r_1"}, "answer": {"status": "partial", "short_answer": "  ", "claims": [1, 2]}}
    meta = h.make_meta(record, "completed", None, "removed", [{"stage": "first_check", "cause": "K6"}], "not_attempted")
    assert meta["bound"] == {"answer_status": "partial", "summary_blank": True, "claims": 2} and meta["check_run_id"] == "r_1"
    body = {k: v for k, v in meta.items() if k != "digest"}
    assert meta["digest"] == "sha256:" + h.hashlib.sha256(h.canonical(body).encode("utf-8")).hexdigest()
    none = h.make_meta({"ask_id": "a_2"}, "failed", None, "no_answer", [], "not_attempted")
    assert none["bound"] == {"answer_status": None, "summary_blank": None, "claims": None} and none["check_run_id"] is None


# The candidate writers store answer_meta themselves once C1 lane 2 is merged, so a written row may already carry the key.
# The previous readers are then judged on the row against the same row with the key absent, never against another state.

def fake_row(ask_id, status, answer_status, meta=None):
    record = {"ask_id": ask_id, "status": status, "answer": {"status": answer_status}, "run": {"run_id": "r_" + ask_id}}
    if meta is not None:
        record["answer_meta"] = meta
    return {"run_id": ask_id, "record": record}


WRITTEN_META = {"check": "verified", "v": 1, "ask_id": "x", "execution": {"state": "completed", "stop_reason": None},
                "summary": {"state": "removed", "removals": [{"stage": "first_check", "cause": "K6"}], "rewrite": "not_attempted"},
                "bound": {"answer_status": "complete", "summary_blank": False, "claims": 1}, "digest": "sha256:" + "0" * 64}


def written(with_meta):
    meta = WRITTEN_META if with_meta else None
    return [fake_row("a_1", "complete", "complete", meta), fake_row("a_2", "complete", "partial", meta),
            fake_row("a_3", "failed", None, meta), fake_row("a_4", "stopped", "insufficient_evidence", meta)]


def test_a_corpus_of_rows_without_the_key_is_as_before_the_written_rows_are_their_own_twins():
    rows, twin_of = h.corpus(written(False))
    assert len(rows) == 4 + len(h.STATES) and len(twin_of) == len(h.STATES)
    assert {twin_of[a] for a in twin_of} <= {"a_1", "a_2", "a_3", "a_4"}


def test_rows_the_writers_stored_with_the_key_get_a_key_absent_twin_and_the_old_readers_are_judged_against_it():
    rows, twin_of = h.corpus(written(True))
    by_id = {h.ask_id_of(row): row for row in rows}
    stored = [a for a in ("a_1", "a_2", "a_3", "a_4")]
    assert all(by_id[a]["record"]["answer_meta"] == WRITTEN_META for a in stored)  # the producer's rows are kept as written
    assert len(rows) == 4 + 4 + len(h.STATES) and len(twin_of) == 4 + len(h.STATES)
    for a in stored:
        twin = by_id[twin_of[a]]["record"]
        assert "answer_meta" not in twin and {k: v for k, v in twin.items() if k != "ask_id"} == {k: v for k, v in by_id[a]["record"].items() if k not in ("ask_id", "answer_meta")}
        assert h.kind_of(by_id[twin_of[a]]) == h.kind_of(by_id[a])
    for clone_id in (c for c in twin_of if c.startswith("a_20261009_meta")):
        assert "answer_meta" not in by_id[twin_of[clone_id]]["record"]  # a clone is never judged against another state's meta
    assert len({h.ask_id_of(row) for row in rows}) == len(rows)


def test_the_declared_key_is_answer_meta_alone_whatever_a_stored_state_holds_inside_it():
    twin = {"ask read": read_of(200, {"a": "string"})}
    produced = {"ask read": read_of(200, {"a": "string", "answer_meta": {"v": "number", "summary": {"removals": ["object"]}}})}
    assert h.layer2_problems({"t": twin, "p": produced}, {"p": "t"}) == []
    against_another_state = {"ask read": read_of(200, {"a": "string", "answer_meta": {"summary": {"removals": []}}})}
    problems = h.layer2_problems({"t": against_another_state, "p": produced}, {"p": "t"})
    assert "p ask read.answer_meta.summary.removals[]: undeclared key" in problems  # why the twin must be the key-absent row


# CC-9: a child the harness starts loads the offline guard (no -I, -E or -S, and the guard directory kept on PYTHONPATH) and stays
# isolated in the ways that matter here: no user site, and an environment built from a short list rather than inherited.

class Started(Exception):
    pass


def captured_child(monkeypatch, tmp_path, start):
    seen = {}

    def record(argv, **kwargs):
        seen["argv"], seen["env"] = list(argv), kwargs["env"]
        raise Started

    monkeypatch.setattr(ch.subprocess, "Popen", record)
    monkeypatch.setattr(h.subprocess, "run", record)
    with pytest.raises(Started):
        start()
    return seen


def start_server(tmp_path):
    ch.Server(tmp_path, "app:app", {}, tmp_path).start()


@pytest.mark.parametrize("start", [start_server, lambda tmp_path: h.written_rows(tmp_path)], ids=["compat server", "corpus driver"])
def test_cc9_the_children_are_not_started_in_a_mode_that_skips_the_guard_and_still_skip_the_user_site(monkeypatch, tmp_path, start):
    seen = captured_child(monkeypatch, tmp_path, lambda: start(tmp_path))
    options = [a for a in seen["argv"][1:] if a.startswith("-") and not a.startswith("--")][:1]
    assert not set("IES") & set("".join(options).lstrip("-")), seen["argv"][:3]
    assert seen["argv"][1] == "-s"


def test_cc9_the_child_environment_keeps_the_guard_directory_and_nothing_else_from_python_settings(monkeypatch, tmp_path):
    guard, other = tmp_path / "guard", tmp_path / "other"
    guard.mkdir()
    other.mkdir()
    (guard / "sitecustomize.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(other), str(guard)]))
    for stray in ("PYTHONSTARTUP", "PYTHONHOME", "PYTHONINSPECT", "PYTHONUSERBASE"):
        monkeypatch.setenv(stray, str(other))
    env = ch._environment({})
    assert env["PYTHONPATH"] == str(guard)
    assert not [k for k in env if k.startswith("PYTHON") and k not in ("PYTHONPATH", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE")]
    monkeypatch.delenv("PYTHONPATH")
    assert "PYTHONPATH" not in ch._environment({})


def options_before_the_program(argv):
    """Every interpreter option before -m or -c: what decides how the child starts."""
    options = []
    for argument in argv[1:]:
        if argument in ("-m", "-c"):
            break
        options.append(argument)
    return options


@pytest.mark.parametrize("start", [start_server, lambda tmp_path: h.written_rows(tmp_path)], ids=["compat server", "corpus driver"])
def test_rv3_no_interpreter_option_before_the_program_isolates_the_child_from_the_guard(monkeypatch, tmp_path, start):
    seen = captured_child(monkeypatch, tmp_path, lambda: start(tmp_path))
    options = options_before_the_program(seen["argv"])
    assert options and options[0] == "-s", seen["argv"][:5]
    assert not set("IES") & set("".join(option.lstrip("-") for option in options if not option.startswith("--"))), seen["argv"][:5]


@pytest.mark.parametrize("extra", [["-I"], ["-E"], ["-S"], ["-sI"], ["-IE"]])
def test_rv3_the_option_check_sees_an_isolating_flag_after_the_first_option(extra):
    options = options_before_the_program(["python", "-s", *extra, "-m", "uvicorn"])
    assert set("IES") & set("".join(option.lstrip("-") for option in options[1:]))


def test_rv2_the_child_environment_keeps_the_guard_log_and_the_current_test_when_they_are_set(monkeypatch, tmp_path):
    log = str(tmp_path / "guard.jsonl")
    monkeypatch.setenv("CORE_OFFLINE_GUARD_LOG", log)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "core/api/tests/test_x.py::test_y (call)")
    env = ch._environment({})
    assert env["CORE_OFFLINE_GUARD_LOG"] == log and env["PYTEST_CURRENT_TEST"] == "core/api/tests/test_x.py::test_y (call)"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CORE_OFFLINE_GUARD_LOG", "relative.jsonl")
    assert Path(ch._environment({})["CORE_OFFLINE_GUARD_LOG"]) == tmp_path / "relative.jsonl"
    monkeypatch.delenv("CORE_OFFLINE_GUARD_LOG")
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    env = ch._environment({})
    assert "CORE_OFFLINE_GUARD_LOG" not in env and "PYTEST_CURRENT_TEST" not in env


def test_rv2_a_refusal_inside_a_child_reaches_the_log_the_parent_was_given(monkeypatch, tmp_path):
    import subprocess
    import sys

    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import os\nwith open(os.environ['CORE_OFFLINE_GUARD_LOG'], 'a', encoding='utf-8') as s:\n    s.write(os.environ.get('PYTEST_CURRENT_TEST', 'none') + '\\n')\n",
        encoding="utf-8")
    log = tmp_path / "guard.jsonl"
    monkeypatch.setenv("PYTHONPATH", str(guard))
    monkeypatch.setenv("CORE_OFFLINE_GUARD_LOG", str(log))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "a::b (call)")
    subprocess.run([sys.executable, "-s", "-c", "pass"], env=ch._environment({}), check=True, cwd=tmp_path)
    assert log.read_text(encoding="utf-8").splitlines() == ["a::b (call)"]


def test_rv4_a_relative_guard_directory_is_made_absolute_against_the_parents_working_directory(monkeypatch, tmp_path):
    import subprocess
    import sys

    guard = tmp_path / "rel_guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text("import os\nopen(os.environ['MARKER'], 'w').close()\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    marker = tmp_path / "loaded.txt"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", "rel_guard")
    env = ch._environment({"MARKER": str(marker)})
    assert os.path.isabs(env["PYTHONPATH"]) and Path(env["PYTHONPATH"]) == guard
    subprocess.run([sys.executable, "-s", "-c", "pass"], env=env, check=True, cwd=elsewhere)
    assert marker.exists()
