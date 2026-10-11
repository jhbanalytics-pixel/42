"""The durable-effects manifest of Release C (a release on top of Release A) states no storage change, and the proof is recomputed from git
(plan 3.4d; review of Release C, finding m10). Offline: git only, no cloud."""
import ast
import functools
import json
import subprocess
from pathlib import Path

import pytest

from core.setup import durable_effects_check as dec
from core.setup.release import durable_prior as dp
from core.setup.release import packet
from core.setup.tests import release_world as rw

ROOT = Path(__file__).resolve().parents[3]
A = rw.A_COMMIT


def git(repo, *args):
    done = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=repo,
                          capture_output=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def head():
    return git(ROOT, "rev-parse", "HEAD")


@functools.lru_cache(maxsize=None)
def built():
    """The manifest of this tree on top of A, built once: it reads two archives of core/ from git."""
    return dp.build_manifest(ROOT, head(), A, "Albert Meintjes")


# the real range: Release A to this tree

def test_the_manifest_of_this_tree_on_top_of_a_is_valid_and_states_no_storage_change():
    manifest = built()
    assert dec.validate_manifest(manifest) == []
    assert manifest["generated_from"] == {"base": A, "head": head()} and manifest["release_id"] == f"rel-{head()[:7]}-01"
    claim = manifest["no_storage_change"]
    assert claim["claimed"] is True and claim["proof"]["schema_statements_added"] == 0 and claim["proof"]["changed_sql_or_job_owned"] == []
    assert claim["proof"]["schema_sha256"]["base"] == claim["proof"]["schema_sha256"]["head"]
    assert "runs" in {r["table"] for r in claim["references"]}


def test_the_claim_holds_against_the_checker_with_the_schema_files_of_both_commits():
    claim = built()["no_storage_change"]
    base_texts = dp.schema_texts(dec.git_tree_files(A, "core", ROOT))
    head_texts = dp.schema_texts(dec.git_tree_files(head(), "core", ROOT))
    assert head_texts and dec.check_no_storage_change(claim, head_texts, base_texts) == []
    assert dec.new_statements(head_texts, base_texts) == []


def test_bind_accepts_the_manifest_for_the_release_it_names_and_no_other():
    manifest = built()
    tree = git(ROOT, "rev-parse", "HEAD^{tree}")
    assert packet.durable_binding_problem(manifest, manifest["release_id"], head(), tree) is None
    assert packet.durable_binding_problem(manifest, "rel-1234567-01", head(), tree) == "release_id"


def test_the_effects_name_the_release_tags_and_say_nothing_about_a80_or_the_canonical_url():
    manifest = built()
    targets = {e["effect_id"]: e["target"] for e in manifest["effects"]}
    assert targets["E-TAG-A"] == f"f42-agent:{manifest['release_id']}" and targets["E-TAG-W"] == f"f42-api:{manifest['release_id']}"
    for effect in manifest["effects"]:
        assert effect["owner"]["person"] == "Albert Meintjes"
        if effect["effect_id"] in dp.WORDING:
            assert "a80" not in effect["statement_ref"] and "canonical URL to" not in effect["statement_ref"]
    assert all(v["verdict"] == "pending" for e in manifest["effects"] for v in e["compat"].values())


def test_the_manifest_is_written_once_and_never_replaced(tmp_path):
    repo, base, tip = make_range(tmp_path, touch_api)
    out = tmp_path / "durable.json"
    argv = ["--repo", str(repo), "--base-commit", base, "--owner", "Albert Meintjes", "--out", str(out)]
    assert dp.main(argv) == 0
    first = out.read_bytes()
    written = json.loads(first)
    assert written["no_storage_change"]["claimed"] is True and written["source"]["commit"] == tip and dec.validate_manifest(written) == []
    assert dp.main(argv) == 1
    assert out.read_bytes() == first


def test_the_command_refuses_a_checkout_that_is_not_clean(tmp_path):
    repo, base, tip = make_range(tmp_path, touch_api)
    (repo / "core/api/store.py").write_text("changed\n", encoding="utf-8")
    out = tmp_path / "durable.json"
    assert dp.main(["--repo", str(repo), "--base-commit", base, "--owner", "Albert Meintjes", "--out", str(out)]) == 1 and not out.exists()


@pytest.mark.parametrize("base", ["3710c5e", "main", "", "z" * 40])
def test_the_command_refuses_a_base_that_is_not_a_full_commit(tmp_path, base):
    out = tmp_path / "x.json"
    assert dp.main(["--repo", str(ROOT), "--base-commit", base, "--owner", "Albert Meintjes", "--out", str(out)]) == 1 and not out.exists()


# the claim is false when the range adds a statement or changes a file the schema or the jobs own

def make_range(tmp_path, change):
    repo = tmp_path / "range"
    (repo / "core/schema").mkdir(parents=True)
    (repo / "core/api").mkdir(parents=True)
    (repo / "core/schema/agent.sql").write_text("CREATE TABLE IF NOT EXISTS `p.intelligence_42_agent.runs` (record STRING);\n", encoding="utf-8")
    (repo / "core/schema/core.sql").write_text("CREATE TABLE IF NOT EXISTS `p.intelligence_42_core.posts` (id STRING);\n", encoding="utf-8")
    (repo / "core/api/store.py").write_text("SQL = 'SELECT 1 FROM intelligence_42_agent.runs'\n", encoding="utf-8")
    template = repo / dp.TEMPLATE
    template.parent.mkdir(parents=True)
    template.write_bytes((ROOT / dp.TEMPLATE).read_bytes())
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    change(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "head")
    return repo, base, git(repo, "rev-parse", "HEAD")


def touch_api(repo):
    (repo / "core/api/store.py").write_text("SQL = 'SELECT 2 FROM intelligence_42_agent.runs'\n", encoding="utf-8")


def add_statement(repo):
    touch_api(repo)
    with (repo / "core/schema/core.sql").open("a", encoding="utf-8") as out:
        out.write("CREATE TABLE IF NOT EXISTS `p.intelligence_42_core.fresh` (x INT64);\n")


def add_column(repo):
    with (repo / "core/schema/agent.sql").open("a", encoding="utf-8") as out:
        out.write("ALTER TABLE `p.intelligence_42_agent.runs` ADD COLUMN IF NOT EXISTS extra STRING;\n")


def change_job_owned(repo):
    (repo / "core/detect/sql").mkdir(parents=True)
    (repo / "core/detect/sql/views.sql").write_text("CREATE OR REPLACE VIEW `p.d.v` AS SELECT 1;\n", encoding="utf-8")


def a_new_sql_file(repo):
    (repo / "core/api/extra.sql").write_text("SELECT 1;\n", encoding="utf-8")


def test_a_range_that_touches_only_application_code_is_a_claim_that_holds(tmp_path):
    repo, base, tip = make_range(tmp_path, touch_api)
    manifest = dp.build_manifest(repo, tip, base, "Albert Meintjes")
    assert manifest["no_storage_change"]["references"] == [{"table": "runs", "column": ""}]


@pytest.mark.parametrize("change", [add_statement, add_column, change_job_owned, a_new_sql_file])
def test_a_range_that_adds_a_schema_statement_or_changes_a_sql_or_job_owned_file_makes_the_claim_false(tmp_path, change):
    repo, base, tip = make_range(tmp_path, change)
    with pytest.raises(dp.Refused, match="no storage change is false|claim of no storage change|schema|file the jobs"):
        dp.build_manifest(repo, tip, base, "Albert Meintjes")


# Release A's readers and the one new tool mark

def tool_reads_and_writes(files):
    reads, writes = [], set()
    for path, text in files:
        if dec.is_test_path(path) or not path.endswith(".py"):
            continue
        tree = ast.parse(text)
        parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            is_get = (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args
                      and isinstance(node.args[0], ast.Constant) and node.args[0].value == "tool")
            is_item = isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "tool"
            if is_item and isinstance(node.ctx, ast.Store):
                writes.add((path, ast.unparse(parent[node])))
            elif is_get or is_item:
                reads.append((path, type(parent[node]).__name__))
    return reads, writes


def test_release_a_reads_the_tool_mark_of_a_query_only_by_comparison_and_this_tree_adds_one_mark():
    reads_a, writes_a = tool_reads_and_writes(dec.git_tree_files(A, "core", ROOT))
    reads_c, writes_c = tool_reads_and_writes(dec.git_tree_files(head(), "core", ROOT))
    assert reads_a and {kind for _, kind in reads_a} == {"Compare"}, "a reader that does not compare could fail on an unknown mark"
    assert {kind for _, kind in reads_c} == {"Compare"}
    assert sorted(writes_c - writes_a) == [("core/agent/tools/warehouse.py", "ctx.queries[result['query_id']]['tool'] = SWEEP_TOOL")]
    assert writes_a <= writes_c
