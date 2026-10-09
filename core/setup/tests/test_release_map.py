"""The release test map, the packet lock and the environment (W8-REL 5.7, TM-01 to TM-05). These nodes collect in the core
partition. A missing prerequisite fails them with a message; nothing here skips."""
import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from core.setup.release import lock as locklib
from core.setup.tests import release_prereq

ROOT = Path(__file__).resolve().parents[3]
MAP = ROOT / "core/setup/tests/release_test_ids.json"
COUNTS = {"DS": 22, "RT": 5, "PS": 23, "HR": 54, "AU": 9, "CT": 7, "DE": 15, "TM": 5, "SM": 1,
          "JM": 11, "JB": 8, "JS": 11, "JU": 10, "JR": 6, "JX": 8, "JP": 6}
PARTITION_OF_PREFIX = {"HR": "core", "AU": "core", "DE": "core", "TM": "core", "SM": "core", "DS": "deploy", "RT": "deploy", "PS": "deploy", "CT": "deploy",
                       "JM": "core", "JB": "core", "JS": "core", "JR": "core", "JX": "core", "JU": "deploy", "JP": "deploy"}
NODE = re.compile(r"^test_(hr|au|ds|rt|ps|ct|de|tm|sm|jm|jb|js|ju|jr|jx|jp)(\d\d)(?:_|$)")
# The folders the nodes are collected from. core/collect/tests and core/detect/tests hold the three Release B nodes the lane 6 and lane 5 files own.
COLLECTION_ROOTS = ("core/setup/tests", "core/api/tests", "core/schema/tests", "core/collect/tests", "core/detect/tests")


def load_map():
    return json.loads(MAP.read_text(encoding="utf-8"))


def declared_ids():
    return {f"{prefix}-{n:02d}" for prefix, count in COUNTS.items() for n in range(1, count + 1)}


def collected():
    """{id: {file, ...}} from every test file under the three test folders, read from the source (the names pytest collects)."""
    found = {}
    for folder in COLLECTION_ROOTS:
        for path in sorted((ROOT / folder).glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    m = NODE.match(node.name)
                    if m:
                        found.setdefault(f"{m.group(1).upper()}-{m.group(2)}", set()).add(path.relative_to(ROOT).as_posix())
    return found


def test_tm01_the_map_lists_every_id_of_section_5_and_no_more():
    data = load_map()
    assert data["schema_version"] == 1
    assert set(data["ids"]) == declared_ids()


def test_tm01_every_built_id_has_a_test_in_the_partition_named_for_its_prefix_and_no_test_carries_an_unlisted_id():
    data = load_map()
    files_of = {name: set(files) for name, files in data["partitions"].items()}
    found = collected()
    assert set(found) <= set(data["ids"]), sorted(set(found) - set(data["ids"]))
    for ident, entry in data["ids"].items():
        assert entry["partition"] == PARTITION_OF_PREFIX[ident[:2]], ident
        if entry["status"] == "built":
            assert ident in found, f"{ident} is marked built and has no test whose name carries it"
            assert found[ident] <= files_of[entry["partition"]], (ident, found[ident], entry["partition"])
        else:
            assert ident not in found, f"{ident} is {entry['status']} yet a test carries its id"
            assert entry["status"] in ("pending", "not_applicable") and entry["reason"]


def test_tm01_every_file_of_a_partition_exists_and_no_file_is_in_both():
    data = load_map()
    core, deploy = set(data["partitions"]["core"]), set(data["partitions"]["deploy"])
    assert not core & deploy
    for rel in core | deploy:
        assert (ROOT / rel).is_file(), rel


def test_tm01_the_pending_and_not_applicable_ids_are_exactly_the_ones_this_branch_does_not_build():
    data = load_map()
    states = {ident: e["status"] for ident, e in data["ids"].items() if e["status"] != "built"}
    assert {i for i, s in states.items() if s == "not_applicable"} == {"HR-03", "HR-48"}
    pending = {f"AU-0{n}" for n in range(1, 10)} | {f"JB-{n:02d}" for n in range(1, 9)} | {f"JS-{n:02d}" for n in range(1, 12)} | {"JX-06", "JX-08"}
    assert {i for i, s in states.items() if s == "pending"} == pending


# TM-02: the lock

def test_tm02_a_lock_built_from_the_repository_has_no_problems_and_a_changed_file_is_found(tmp_path):
    bound = {}
    for name in locklib.BOUND_NAMES:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"schema_version": 1, "name": name}), encoding="utf-8")
        bound[name] = path
    lock = locklib.build_lock(ROOT, "rel-d666ef6-01", bound)
    assert lock["schema_version"] == 1 and set(lock["bound_files"]) == set(locklib.BOUND_NAMES)
    assert locklib.lock_problems(ROOT, lock) == []
    assert "core/setup/release/SERVICES-PASTE.ps1" in lock["repo_files"] and "core/api/deploy_candidate.sh" in lock["repo_files"]
    for rel in ("core/setup/release/bound_readback.py", "core/api/smoke.py", "core/setup/durable_effects_check.py"):
        broken = json.loads(json.dumps(lock))
        broken["repo_files"][rel] = "0" * 64
        assert any(rel in p for p in locklib.lock_problems(ROOT, broken)), rel
    broken = json.loads(json.dumps(lock))
    broken["tests"]["core/setup/tests/test_release_plan.py"] = "0" * 64
    assert any("test_release_plan" in p for p in locklib.lock_problems(ROOT, broken))
    assert locklib.lock_problems(ROOT, {**lock, "schema_version": 2}) == ["unknown lock schema_version"]


def test_tm02_a_file_the_repository_gained_that_the_lock_does_not_list_is_found(tmp_path):
    lock = locklib.build_lock(ROOT, "rel-d666ef6-01", {n: tmp_path / "x" for n in locklib.BOUND_NAMES})
    del lock["repo_files"]["core/setup/release/plan.py"]
    assert any("plan.py is not in the lock" in p for p in locklib.lock_problems(ROOT, lock))


def test_tm02_line_endings_do_not_change_a_hash(tmp_path):
    lf, crlf = tmp_path / "lf.txt", tmp_path / "crlf.txt"
    lf.write_bytes(b"a\nb\n")
    crlf.write_bytes(b"a\r\nb\r\n")
    assert locklib.sha_file(lf) == locklib.sha_file(crlf)
    assert locklib.sha_file(lf, text=False) != locklib.sha_file(crlf, text=False)


def test_tm02_the_committed_lock_of_the_release_being_prepared_binds_the_repository_sources():
    locks = sorted((ROOT / "core/setup/release/locks").glob("PACKET-LOCK-*.json")) if (ROOT / "core/setup/release/locks").is_dir() else []
    # No lock is committed until Albert prepares Release A: the lock needs the bound baseline, the bindings and the receipts,
    # which are captured outside the repository. When one is committed, the newest must hold for every source it lists.
    for path in locks[-1:]:
        assert locklib.lock_problems(ROOT, json.loads(path.read_text(encoding="utf-8"))) == []


# TM-03: prerequisites

def test_tm03_pwsh_bash_and_the_a80_commit_are_present_or_the_test_fails_naming_what_is_missing():
    missing = release_prereq.prerequisites(ROOT)
    if missing:
        pytest.fail("the release tests need " + ", ".join(missing))


def test_tm03_a_missing_prerequisite_is_named_not_skipped(tmp_path):
    missing = release_prereq.prerequisites(tmp_path, env={"F42_BASH": str(tmp_path / "nope")}, which=lambda name: None)
    assert missing[0] == "pwsh" and missing[1] == "bash" and missing[2].startswith("commit a80be1d")


def test_tm03_the_new_test_files_carry_no_skip_marker_but_the_one_that_names_its_reason():
    allowed = {"core/setup/tests/test_declared_env_removals.py": "kept out of the repository"}
    for rel in list(locklib.TEST_FILES) + ["core/setup/tests/paste_world.py", "core/setup/tests/release_world.py", "core/api/tests/smoke_support.py"]:
        path = ROOT / rel
        if not path.is_file() or path.suffix != ".py":
            continue
        text = path.read_text(encoding="utf-8")
        hits = [n.attr for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Attribute) and n.attr in ("skip", "skipif", "xfail", "importorskip")]
        if rel in allowed:
            assert hits == ["skipif"] and allowed[rel] in text, rel
        else:
            assert hits == [], (rel, hits)


# TM-04: bash does not come from git's directory

def test_tm04_bash_is_resolved_by_the_explicit_order_and_a_shim_git_first_on_path_does_not_change_it(tmp_path, monkeypatch):
    real = release_prereq.resolve_bash()
    assert real.is_file()
    shim = tmp_path / "shim" / "mingw64" / "bin"
    shim.mkdir(parents=True)
    (shim / ("git.exe" if release_prereq.sys.platform.startswith("win") else "git")).write_bytes(b"")
    monkeypatch.setenv("PATH", str(shim) + ";" + __import__("os").environ["PATH"])
    assert release_prereq.resolve_bash() == real


def test_tm04_the_order_is_f42_bash_then_path_on_linux_then_local_app_data_on_windows(tmp_path):
    explicit = tmp_path / "bash"
    explicit.write_bytes(b"")
    assert release_prereq.resolve_bash({"F42_BASH": str(explicit)}, "linux") == explicit
    assert release_prereq.resolve_bash({}, "linux", which=lambda name: "/usr/bin/bash") == Path("/usr/bin/bash")
    with pytest.raises(FileNotFoundError):
        release_prereq.resolve_bash({}, "linux", which=lambda name: None)
    git_bash = tmp_path / "Programs" / "Git" / "bin"
    git_bash.mkdir(parents=True)
    (git_bash / "bash.exe").write_bytes(b"")
    assert release_prereq.resolve_bash({"LOCALAPPDATA": str(tmp_path)}, "win32") == git_bash / "bash.exe"
    with pytest.raises(FileNotFoundError):
        release_prereq.resolve_bash({"LOCALAPPDATA": str(tmp_path / "none")}, "win32")
    with pytest.raises(FileNotFoundError):
        release_prereq.resolve_bash({"F42_BASH": str(tmp_path / "missing")}, "linux")


def test_tm04_the_deploy_tests_resolve_bash_through_that_function_and_not_through_git():
    for rel in ("core/api/tests/test_deploy_script.py", "core/api/tests/test_deploy_candidate.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "resolve_bash()" in text and 'shutil.which("git")' not in text, rel


# TM-05: the smoke count

def test_tm05_the_expected_smoke_check_count_in_a_lock_is_the_count_recorded_in_smoke_py_by_ast(tmp_path):
    lock = locklib.build_lock(ROOT, "rel-d666ef6-01", {n: tmp_path / "x" for n in locklib.BOUND_NAMES})
    names = [c.args[0].value for c in ast.walk(next(n for n in ast.walk(ast.parse((ROOT / "core/api/smoke.py").read_text(encoding="utf-8")))
                                                    if isinstance(n, ast.FunctionDef) and n.name == "run"))
             if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "record" and c.args and isinstance(c.args[0], ast.Constant)]
    assert lock["expected_smoke_checks"] == locklib.smoke_check_count(ROOT / "core/api/smoke.py") == len(names) == 6
    assert names == ["health", "gate", "today", "ask", "events", "export"]
    broken = {**lock, "expected_smoke_checks": 5}
    assert any("expected_smoke_checks" in p for p in locklib.lock_problems(ROOT, broken))
