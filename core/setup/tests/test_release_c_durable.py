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


# The one stored shape this tree adds, evidence_links in the Ask record (plan 3.4d, review of Release C finding I1)

KEY_TARGET = "intelligence_42_agent.runs#record#$.evidence_links"
STORED = "E-STORED-EVIDENCE-LINKS"
FIVE = ["E-TRAFFIC", "E-TAG-A", "E-TAG-W", "E-ENV-AUD", "E-ENV-URL"]


def stored_effect(manifest):
    found = [e for e in manifest["effects"] if e["kind"] == "stored_answer"]
    assert [e["effect_id"] for e in found] == [STORED]
    return found[0]


def test_the_manifest_of_this_tree_declares_the_stored_effect_for_the_key_in_the_run_metadata():
    manifest = built()
    effect = stored_effect(manifest)
    assert effect["target"] == KEY_TARGET and effect["change"] == "write_shape" and effect["apply_kind"] == "code"
    assert effect["apply_phase"] == "with_candidate" and effect["additive"] is True and effect["destructive"] is False
    assert effect["authority_or_holds"] is False and effect["owner"] == {"lane": 3, "person": "Albert Meintjes"}
    assert effect["apply_order"] == 1 + max(e["apply_order"] for e in manifest["effects"] if e is not effect)
    assert effect["statement_ref"].startswith("The Ask record stored in intelligence_42_agent.runs, column record, gains the key evidence_links at $.evidence_links")
    assert effect["statement_sha256"] == dec.sha_text(effect["statement_ref"])
    assert effect["native_readback"] == {"kind": "helper_phase", "target": "AfterSmoke", "expected_result_sha256": ""}
    assert dec.validate_manifest(manifest) == []
    assert [e["effect_id"] for e in manifest["effects"]] == [*FIVE, STORED]


def test_the_stored_effect_recovers_forward_with_no_statement_and_no_sql():
    manifest = built()
    recovery = stored_effect(manifest)["forward_recovery"]
    assert recovery["kind"] == "none_needed" and recovery["statement_ref"] == "" and recovery["statement_sha256"] == ""
    assert "rollback leaves the stored key in place" in recovery["note"]
    claim = manifest["no_storage_change"]
    assert claim["claimed"] is True and claim["proof"]["schema_statements_added"] == 0 and claim["proof"]["changed_sql_or_job_owned"] == []


def blob_lines(path):
    return git(ROOT, "show", f"{head()}:{path}").splitlines()


def test_the_consumers_are_the_non_test_files_that_name_the_key_each_with_a_line_that_does():
    consumers = stored_effect(built())["consumers"]
    files = {c["file"] for c in consumers}
    assert files == {"core/agent/ask.py", "core/agent/checks.py", "core/api/agent_app.py", "core/api/comment_links.py", "core/api/dossiers.py",
                     "core/api/privacy.py"}
    assert {c["access"] for c in consumers} == {"read", "write"} and all(c["image"] == "f42-web" and c["line"] > 0 for c in consumers)
    for consumer in consumers:
        assert any(word in blob_lines(consumer["file"])[consumer["line"] - 1] for word in ("evidence_links", "comment_links")), consumer
    writes = {c["file"] for c in consumers if c["access"] == "write"}
    assert {"core/agent/ask.py", "core/api/agent_app.py"} <= writes and "core/api/dossiers.py" not in writes


def test_every_changed_product_file_that_names_the_key_is_a_consumer_of_the_stored_effect():
    manifest = built()
    only = {**manifest, "effects": [stored_effect(manifest)]}
    changed_paths = set(dp.changed_paths(ROOT, A, head()))
    changed = {path: text for path, text in dec.git_tree_files(head(), "core", ROOT)
               if path in changed_paths and not dec.is_test_path(path) and not path.startswith("core/setup/")}
    assert any("evidence_links" in text for text in changed.values())
    assert dec.check_consumers(only, changed) == []
    assert dec.check_consumers({**only, "effects": [{**only["effects"][0], "consumers": []}]}, changed) != []


def test_without_a_receipt_the_compat_verdicts_of_the_stored_effect_stay_pending_like_the_others():
    assert all(v["verdict"] == "pending" for v in stored_effect(built())["compat"].values())


# the verdicts come from the old-reader receipt, recomputed against this tree

def harness_hash():
    from core.api.tests import old_reader_harness as orh
    return orh.harness_sha256()


def receipt_for(**over):
    return {"schema_version": 1, "base_commit": A, "candidate_commit": head(), "harness_sha256": harness_hash(), "corpus_sha256": "0" * 64,
            "corpus_rows": 30, "legacy_rows": 4, "json_paths": 20, "prior_stores_answer_meta": True, "problems": [], "verdict": "pass", **over}


def write_receipt(tmp_path, **over):
    path = tmp_path / "old-reader-receipt.json"
    path.write_text(json.dumps(receipt_for(**over), indent=2) + "\n", encoding="utf-8")
    return path


def test_the_harness_hash_the_tool_recomputes_from_git_equals_the_one_the_harness_writes():
    assert dp.harness_sha256(ROOT, head()) == harness_hash()


def test_a_passing_receipt_made_for_this_tree_settles_the_five_verdicts_of_the_stored_effect_and_no_other_effect(tmp_path):
    path = write_receipt(tmp_path)
    manifest = dp.build_manifest(ROOT, head(), A, "Albert Meintjes", old_reader_receipt=path)
    effect = stored_effect(manifest)
    assert {k: v["verdict"] for k, v in effect["compat"].items()} == {k: "pass" for k in dec.COMPAT_KEYS}
    digest = dec.sha_text(path.read_text(encoding="utf-8"))[:12]
    assert all(digest in v["test"] for k, v in effect["compat"].items() if k != "a80_ddl_writers")
    assert "no SQL" in effect["compat"]["a80_ddl_writers"]["test"]
    assert all(v["verdict"] == "pending" for e in manifest["effects"] if e["effect_id"] != STORED for v in e["compat"].values())
    assert dec.validate_manifest(manifest) == []


@pytest.mark.parametrize("over", [
    {"verdict": "fail", "problems": ["layer 2: x"]},
    {"verdict": "pass", "problems": ["layer 2: x"]},
    {"verdict": "pending"},
    {"schema_version": 2},
    {"candidate_commit": "0" * 40},
    {"base_commit": "1" * 40},
    {"base_commit": ""},
    {"harness_sha256": "0" * 64},
    {"harness_sha256": 7},
], ids=["fail", "pass with a problem", "no verdict", "other version", "other candidate", "other base", "no base", "other harness", "harness not text"])
def test_a_receipt_that_does_not_belong_to_this_release_or_does_not_pass_is_refused(tmp_path, over):
    path = write_receipt(tmp_path, **over)
    with pytest.raises(dp.Refused, match="old-reader receipt"):
        dp.build_manifest(ROOT, head(), A, "Albert Meintjes", old_reader_receipt=path)


def test_a_receipt_that_is_missing_or_not_a_json_object_is_refused(tmp_path):
    for content in (None, "{", "[]"):
        path = tmp_path / "r.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        with pytest.raises(dp.Refused, match="old-reader receipt"):
            dp.build_manifest(ROOT, head(), A, "Albert Meintjes", old_reader_receipt=path)


def test_a_receipt_that_names_a_short_form_of_the_base_is_accepted_and_a_wrong_one_is_not(tmp_path):
    path = write_receipt(tmp_path, base_commit=A[:7])
    assert stored_effect(dp.build_manifest(ROOT, head(), A, "Albert Meintjes", old_reader_receipt=path))
    with pytest.raises(dp.Refused, match="old-reader receipt"):
        dp.build_manifest(ROOT, head(), A, "Albert Meintjes", old_reader_receipt=write_receipt(tmp_path, base_commit="3710c5f"))


def producer_and_harness(repo):
    add_producer(repo)
    for name in ("old_reader_harness.py", "compat_harness.py"):
        (repo / "core/api/tests").mkdir(parents=True, exist_ok=True)
        (repo / "core/api/tests" / name).write_bytes((ROOT / "core/api/tests" / name).read_bytes())


def test_the_command_takes_the_receipt_and_refuses_a_bad_one_before_it_writes(tmp_path):
    repo, base, tip = make_range(tmp_path, producer_and_harness)
    out = tmp_path / "durable.json"
    argv = ["--repo", str(repo), "--base-commit", base, "--owner", "Albert Meintjes", "--out", str(out)]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(receipt_for(base_commit=base, candidate_commit=tip, verdict="fail")), encoding="utf-8")
    assert dp.main([*argv, "--old-reader-receipt", str(bad)]) == 1 and not out.exists()
    good = tmp_path / "good.json"
    good.write_text(json.dumps(receipt_for(base_commit=base, candidate_commit=tip)), encoding="utf-8")
    assert dp.main([*argv, "--old-reader-receipt", str(good)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert dec.validate_manifest(written) == [] and all(v["verdict"] == "pass" for v in stored_effect(written)["compat"].values())


# the effect follows the tree: it is there when the tree writes the key and not otherwise

def add_producer(repo):
    (repo / "core/agent").mkdir(parents=True)
    (repo / "core/agent/ask.py").write_text("def done(result, links):\n    result['evidence_links'] = links\n", encoding="utf-8")


def add_reader_only(repo):
    (repo / "core/api/view.py").write_text("def links(record):\n    return record.get('evidence_links')\n", encoding="utf-8")


def add_test_only(repo):
    (repo / "core/api/tests").mkdir(parents=True)
    (repo / "core/api/tests/test_x.py").write_text("def test_x():\n    assert 'evidence_links'\n", encoding="utf-8")


def test_a_tree_that_writes_the_key_gets_the_effect_with_the_writer_as_a_consumer(tmp_path):
    repo, base, tip = make_range(tmp_path, add_producer)
    effect = stored_effect(dp.build_manifest(repo, tip, base, "Albert Meintjes"))
    assert effect["consumers"] == [{"access": "write", "component": "ask.py", "file": "core/agent/ask.py", "image": "f42-web", "line": 2}]


@pytest.mark.parametrize("change", [touch_api, add_reader_only, add_test_only])
def test_a_tree_that_does_not_write_the_key_in_a_product_file_gets_no_stored_effect(tmp_path, change):
    repo, base, tip = make_range(tmp_path, change)
    manifest = dp.build_manifest(repo, tip, base, "Albert Meintjes")
    assert [e["effect_id"] for e in manifest["effects"]] == FIVE
    assert dec.validate_manifest(manifest) == []


def test_a_tree_whose_base_already_writes_the_key_adds_no_stored_shape_and_gets_no_stored_effect(tmp_path):
    repo, base, tip = make_range(tmp_path, add_producer)
    assert [e["effect_id"] for e in dp.build_manifest(repo, tip, tip, "Albert Meintjes")["effects"]] == FIVE


def add_producer_and_tooling(repo):
    add_producer(repo)
    (repo / "core/setup").mkdir(parents=True, exist_ok=True)
    (repo / "core/setup/tool.py").write_text("def mark(result):\n    result['evidence_links'] = 1\n", encoding="utf-8")


def test_release_tooling_that_names_the_key_is_not_a_consumer_of_the_stored_ask(tmp_path):
    repo, base, tip = make_range(tmp_path, add_producer_and_tooling)
    assert [c["file"] for c in stored_effect(dp.build_manifest(repo, tip, base, "Albert Meintjes"))["consumers"]] == ["core/agent/ask.py"]
