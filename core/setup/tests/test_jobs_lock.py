"""The lock of Release B lists every tooling and job code file the jobs paste or its children execute (W8-REL-B v2.1 JP-01, the lock gap F13).
The lists are held to what the source says: the first party import closure of every script the paste starts, the modules it starts with -m,
the schema files core.schema.apply runs, and the test sources that carry a Release B node. Nothing here is typed from memory."""


from core.setup.release import jobs_source as js
from core.setup.release import lock as locklib
from core.setup.tests import lock_closure as lc

ROOT = lc.ROOT
PASTE = ROOT / "core/setup/release/JOBS-PASTE.ps1"
# The scripts the lead starts to produce and judge the packet, beside the ones the paste starts.
LEAD_TOOLS = ("core/setup/release/packet.py", "core/setup/release/chain_evidence.py")
B_PREFIXES = ("jm", "jb", "js", "ju", "jr", "jx", "jp")


def entries():
    scripts, modules = lc.script_paths(PASTE.read_text(encoding="utf-8"))
    return sorted(scripts | set(modules) | set(LEAD_TOOLS)), scripts, modules


def test_lock_the_entries_are_read_from_the_paste_and_include_the_helper_the_runner_the_build_and_the_checker_and_the_apply():
    found, scripts, modules = entries()
    assert {"core/setup/release/bound_readback.py", "core/setup/release/jobs_run.py", "core/setup/deploy_jobs.py",
            "core/setup/durable_effects_check.py"} <= scripts
    assert modules == {"core.schema.apply"}
    assert len(found) >= 6


def test_lock_every_first_party_module_the_paste_and_its_children_import_is_in_repo_files():
    found, _, _ = entries()
    reached = lc.closure(found)
    missing = [p for p in reached if p not in locklib.REPO_FILES]
    assert missing == [], missing
    assert len(reached) > 30


def test_lock_the_files_core_schema_apply_runs_are_in_repo_files():
    names = js.sql_file_names(js.tree_from_disk(ROOT))
    assert names, "apply.py names no SQL file"
    for name in names:
        assert f"core/schema/{name}" in locklib.REPO_FILES, name
    assert "core/schema/apply.py" in locklib.REPO_FILES


def test_lock_every_release_tooling_file_that_belongs_to_jobs_is_in_repo_files():
    folder = ROOT / "core/setup/release"
    jobs = sorted(p.name for p in folder.glob("*.py") if p.name.startswith("jobs_")) + ["env_scan.py", "chain_evidence.py", "JOBS-PASTE.ps1"]
    assert len(jobs) >= 9
    for name in jobs:
        assert f"core/setup/release/{name}" in locklib.REPO_FILES, name


def test_lock_every_job_code_file_release_b_changes_for_the_stamp_and_the_chain_record_is_in_repo_files():
    for rel in ("core/collect/chain.py", "core/setup/stamp.py", "core/setup/deploy_jobs.py", "core/setup/cloudbuild.jobs.yaml",
                "core/setup/jobs.Dockerfile"):
        assert rel in locklib.REPO_FILES, rel


def test_lock_every_file_of_repo_files_and_test_files_exists_and_none_is_listed_twice():
    for group in (locklib.REPO_FILES, locklib.TEST_FILES):
        assert len(set(group)) == len(group)
        for rel in group:
            assert (ROOT / rel).is_file(), rel


def test_lock_every_test_source_that_carries_a_release_b_node_is_in_test_files():
    carriers = lc.test_files_with_prefix_nodes(B_PREFIXES)
    assert len(carriers) >= 15
    missing = [p for p in carriers if p not in locklib.TEST_FILES]
    assert missing == [], missing


def test_lock_every_test_support_module_those_sources_import_is_in_test_files():
    carriers = lc.test_files_with_prefix_nodes(B_PREFIXES)
    support = lc.tests_support_closure(carriers)
    assert "core/setup/tests/jobs_world.py" in support
    missing = [p for p in support if p not in locklib.TEST_FILES]
    assert missing == [], missing


def test_lock_every_test_file_and_world_named_for_the_jobs_release_is_in_test_files():
    folder = ROOT / "core/setup/tests"
    named = sorted(p.relative_to(ROOT).as_posix() for pattern in ("test_jobs_*.py", "jobs_*.py", "e2e_*.py", "test_chain_evidence.py") for p in folder.glob(pattern))
    assert len(named) >= 14
    missing = [p for p in named if p not in locklib.TEST_FILES]
    assert missing == [], missing


def test_lock_a_lock_built_for_a_jobs_release_holds_all_of_them(tmp_path):
    bound = {n: tmp_path / "x" for n in locklib.JOBS_BOUND_NAMES}
    lock = locklib.build_lock(ROOT, "rel-d666ef6-01", bound, names=locklib.JOBS_BOUND_NAMES)
    found, _, _ = entries()
    assert set(lc.closure(found)) <= set(lock["repo_files"])
    assert set(lc.test_files_with_prefix_nodes(B_PREFIXES)) <= set(lock["tests"])
    assert locklib.lock_problems(ROOT, lock) == []


def test_lock_the_closure_finder_sees_an_import_inside_a_function_and_a_relative_import(tmp_path):
    (tmp_path / "core" / "pkg").mkdir(parents=True)
    (tmp_path / "core" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "core" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "core" / "pkg" / "entry.py").write_text("def f():\n    from core.pkg import lazy\n    from . import sibling\n", encoding="utf-8")
    (tmp_path / "core" / "pkg" / "lazy.py").write_text("import core.other\n", encoding="utf-8")
    (tmp_path / "core" / "pkg" / "sibling.py").write_text("", encoding="utf-8")
    (tmp_path / "core" / "other.py").write_text("import json\n", encoding="utf-8")
    assert lc.closure(["core/pkg/entry.py"], root=tmp_path) == ["core/__init__.py", "core/other.py", "core/pkg/__init__.py", "core/pkg/entry.py",
                                                                  "core/pkg/lazy.py", "core/pkg/sibling.py"]
