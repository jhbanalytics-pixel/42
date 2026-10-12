"""The source commit preconditions of Release B (W8-REL-B v2.1 section 3.1, JS-01 to JS-03, JS-08 to JS-10). Offline: trees are
lists of (path, text) as durable_effects_check.git_tree_files returns them; the a80 tree comes from git, and a missing a80 commit
fails (it never skips)."""
import hashlib
import subprocess
from pathlib import Path

import pytest

from core.schema import apply
from core.setup import durable_effects_check as de
from core.setup.release import jobs_source as js
from core.setup.release import services_only as so

ROOT = Path(__file__).resolve().parents[3]
A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
FIX_BLOB = "1322f3800e4d6ac0bcce82063be3b73538e5d018"
A80_BLOB = "70683db46c4bd732a2e1ca2c95dc228aadae940e"
ITEM_STATE = "`ogilvy-trends-v2.intelligence_42_core.item_state`"
THREE = ("eligible_v1 BOOL", "locality_basis STRING", "locality_status STRING")


def a80_tree():
    if subprocess.run(["git", "cat-file", "-e", A80], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.fail(f"commit {A80} is not available in this checkout")
    return de.git_tree_files(A80, "core", ROOT)


def head_tree():
    return js.tree_from_disk(ROOT)


def replace(files, path, old, new):
    out = []
    for p, text in files:
        if p == path:
            assert text.count(old) == 1, (path, old)
            text = text.replace(old, new)
        out.append((p, text))
    return out


def with_item_state_alters(files):
    alters = "".join(f"\nALTER TABLE {ITEM_STATE}\nADD COLUMN IF NOT EXISTS {col};\n" for col in THREE)
    return [(p, t + alters if p == "core/schema/core.sql" else t) for p, t in files]


# JS-01: the schema files the release applies

def test_js01_the_files_apply_runs_are_read_from_the_apply_module_of_the_tree_and_include_early_signal():
    names = js.sql_file_names(head_tree())
    assert names == tuple(p.name for p in apply.SQL_FILES) == ("agent.sql", "core.sql", "early_signal.sql")
    assert "card_outcome.sql" not in names and "locality_switch.sql" not in names


def test_js01_the_tree_of_this_checkout_has_no_schema_problem():
    assert js.schema_problems(head_tree(), a80_tree()) == []


def test_js01_an_item_state_alter_in_any_sql_files_file_is_a_problem():
    problems = js.schema_problems(with_item_state_alters(head_tree()), a80_tree())
    assert any("item_state ALTER" in p and "core/schema/core.sql" in p for p in problems), problems
    swapped = replace(head_tree(), "core/schema/apply.py", 'HERE / "early_signal.sql")', 'HERE / "early_signal.sql", HERE / "locality_switch.sql")')
    problems = js.schema_problems(swapped, a80_tree())
    assert any("locality_switch.sql" in p and "SQL_FILES" in p for p in problems), problems


def test_js01_a_widened_item_state_create_text_is_a_problem():
    files = head_tree()
    text = dict(files)["core/schema/core.sql"]
    marker = "run_id STRING NOT NULL, rule_version STRING, base_state STRING)"
    assert text.count(marker) == 1
    widened = replace(files, "core/schema/core.sql", marker, "run_id STRING NOT NULL, rule_version STRING, base_state STRING, eligible_v1 BOOL)")
    assert any("item_state CREATE" in p for p in js.schema_problems(widened, a80_tree()))


def test_js01_early_signal_missing_from_sql_files_or_card_outcome_added_is_a_problem():
    dropped = replace(head_tree(), "core/schema/apply.py", ', HERE / "early_signal.sql")', ")")
    assert any("early_signal.sql" in p for p in js.schema_problems(dropped, a80_tree()))
    added = replace(head_tree(), "core/schema/apply.py", 'HERE / "early_signal.sql")', 'HERE / "early_signal.sql", HERE / "card_outcome.sql")')
    assert any("card_outcome.sql" in p for p in js.schema_problems(added, a80_tree()))


def test_js01_the_statements_apply_would_run_are_the_ones_read_from_the_tree_in_run_order():
    files = head_tree()
    run_order = js.execution_order(js.schema_statements(files))
    stmts = apply.load_statements()
    expected = ([s for s in stmts if apply.kind(s) == "schema"] + [s for s in stmts if apply.kind(s) == "table"]
                + [s for s in stmts if apply.kind(s) in ("alter", "view")])
    assert run_order == expected


# JS-03: no positional writer or select-star reader of a table the release alters

def test_js03_the_altered_table_set_is_derived_from_the_alter_statements_the_tree_adds_over_a80():
    assert js.altered_tables(head_tree(), a80_tree()) == ["claim_checks", "post_items", "runs"]
    assert js.altered_tables(with_item_state_alters(head_tree()), a80_tree()) == ["claim_checks", "item_state", "post_items", "runs"]


def test_js03_a80_has_no_positional_writer_and_no_select_star_reader_of_runs_claim_checks_or_post_items():
    assert js.writer_problems(a80_tree(), head_tree()) == []


def test_js03_a_tree_whose_sql_files_alter_item_state_fails_on_the_positional_insert_of_state_sql_with_file_and_line():
    files = a80_tree()
    problems = js.writer_problems(files, with_item_state_alters(head_tree()))
    assert problems, "the a80 state INSERT is positional and must be found"
    hit = [p for p in problems if p.startswith("core/detect/sql/state.sql:")]
    assert hit, problems
    text = dict(files)["core/detect/sql/state.sql"]
    line = int(hit[0].split(":")[1])
    assert "INSERT INTO" in text.splitlines()[line - 1] and "item_state" in text.splitlines()[line - 1]
    assert "positional" in hit[0]


def test_js03_a_select_star_reader_of_an_altered_table_is_found_with_its_line():
    base = [("core/x/read.py", 'import os\n\nSQL = "SELECT * FROM `p.d.runs` WHERE 1"\n')]
    problems = js.writer_problems(base, head_tree())
    assert problems == ["core/x/read.py:3: SELECT * reader of runs"]
    named = [("core/x/read.py", 'SQL = "SELECT run_id FROM `p.d.runs`"\n')]
    assert js.writer_problems(named, head_tree()) == []


def test_js03_a_positional_insert_into_an_altered_table_is_found_and_a_named_insert_is_not():
    positional = [("core/x/w.sql", "-- note\nINSERT INTO `p.d.runs`\nSELECT 1;\n")]
    assert js.writer_problems(positional, head_tree()) == ["core/x/w.sql:2: positional INSERT into runs"]
    named = [("core/x/w.sql", "INSERT INTO `p.d.runs` (run_id)\nSELECT 1;\n")]
    assert js.writer_problems(named, head_tree()) == []


def test_js03_a_comment_that_names_a_positional_insert_or_a_select_star_is_not_a_hit():
    commented = [("core/x/w.sql", "-- INSERT INTO `p.d.runs` SELECT 1\nSELECT 2;\n"),
                 ("core/x/r.py", "# SELECT * FROM `p.d.runs`\nx = 1\n")]
    assert js.writer_problems(commented, head_tree()) == []


def test_js03_a_table_that_only_ends_in_the_same_letters_is_not_the_altered_table():
    other = [("core/x/read.py", 'SQL = "SELECT * FROM `p.d.v_good_runs`"\nQ = "SELECT * FROM prefix_post_items"\n')]
    assert js.writer_problems(other, head_tree()) == []


def test_js03_tests_are_outside_the_scan():
    files = [("core/x/tests/test_w.py", 'SQL = "INSERT INTO `p.d.runs` SELECT 1"\n')]
    assert js.writer_problems(files, head_tree()) == []


# JS-08: the same scan over Release A's frozen commit, a record

def test_js08_the_same_scan_runs_over_any_tree_and_names_the_tables_that_tree_alters():
    tree = head_tree()
    assert js.altered_tables(tree, a80_tree()) == ["claim_checks", "post_items", "runs"]
    assert js.writer_problems(a80_tree(), tree) == []
    assert js.scan_summary(a80_tree(), tree) == {"tables": ["claim_checks", "post_items", "runs"], "problems": []}


# JS-10: the PARSE_JSON fix, pinned by literal blob ids

def test_js10_this_checkout_carries_the_fix_blob_and_not_the_a80_blob():
    assert js.parse_json_fix_problems("HEAD", ROOT) == []


def test_js10_the_pinned_blob_ids_are_the_literals_of_the_contract():
    assert js.PARSE_JSON_FIX_BLOB == FIX_BLOB and js.A80_CLUSTER_CHECKPOINT_BLOB == A80_BLOB


def test_js10_the_a80_tree_has_the_old_blob_and_fails():
    problems = js.parse_json_fix_problems(A80, ROOT)
    assert any("cluster_checkpoint.sql" in p and FIX_BLOB in p for p in problems), problems
    assert any(A80_BLOB in p for p in problems), problems


def test_js10_a_blob_that_is_neither_pinned_value_fails_on_the_first_check(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *a],
                                    capture_output=True, check=True, encoding="utf-8")
    run("init", "-q")
    path = repo / "core/understand/sql"
    path.mkdir(parents=True)
    (path / "cluster_checkpoint.sql").write_text("SELECT 1;\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "x")
    problems = js.parse_json_fix_problems("HEAD", repo)
    assert len(problems) == 1 and FIX_BLOB in problems[0]


def test_js10_the_fix_is_not_read_from_the_tree_under_test(tmp_path):
    # the expected blob is a literal of this module, so a tree that carries a different file with a matching name cannot pass
    assert js.PARSE_JSON_FIX_BLOB != hashlib.sha1(b"").hexdigest()


# JS-09: the apply dry run receipt

VIEWS = None


def views_in_sql_files():
    return [apply.statement_name(s) for s in apply.load_statements() if apply.kind(s) == "view"]


def receipt_text(drift=None, skipped=None, tail="dry run only; nothing created\n", failed=None):
    lines = []
    for statement in apply.load_statements():
        name = apply.statement_name(statement)
        if skipped and name in skipped:
            continue
        if failed and name == failed:
            lines.append(f"dry run failed on {name}: boom")
            continue
        lines.append(f"dry run ok: {name}")
    if skipped:
        lines.append(f"skipped {len(skipped)} dry runs: {', '.join(skipped)} need tables not created yet; --apply creates the tables first")
    for name in drift or []:
        lines.append(f"view differs from the file: {name}. The live view is the older SQL; CREATE VIEW IF NOT EXISTS does not change it.")
    return "\n".join(lines) + "\n" + tail


def bound_for(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_js09_a_clean_receipt_with_the_bound_hash_passes():
    text = receipt_text()
    assert len(views_in_sql_files()) == 4
    assert js.dry_run_receipt_problems(text.encode("utf-8"), bound_for(text), head_tree()) == []


def test_js09_the_receipt_is_refused_when_its_hash_is_not_the_bound_one_or_nothing_is_bound():
    text = receipt_text()
    assert js.dry_run_receipt_problems(text.encode("utf-8"), "0" * 64, head_tree()) == ["the receipt hash is not the bound hash"]
    assert js.dry_run_receipt_problems(text.encode("utf-8"), None, head_tree()) == ["no receipt hash is bound"]
    assert js.dry_run_receipt_problems(None, bound_for(text), head_tree()) == ["no receipt exists"]
    assert js.dry_run_receipt_problems(b"", bound_for(""), head_tree()) != []


def test_js09_a_view_that_differs_from_its_file_is_drift_and_is_refused():
    views = views_in_sql_files()
    text = receipt_text(drift=[views[0]])
    problems = js.dry_run_receipt_problems(text.encode("utf-8"), bound_for(text), head_tree())
    assert any("drift" in p and views[0] in p for p in problems), problems


def test_js09_a_view_that_was_skipped_not_dry_run_is_not_a_clean_check():
    views = views_in_sql_files()
    text = receipt_text(skipped=[views[1]])
    problems = js.dry_run_receipt_problems(text.encode("utf-8"), bound_for(text), head_tree())
    assert any(views[1] in p and "dry run" in p for p in problems), problems


def test_js09_a_failed_statement_or_a_receipt_that_does_not_end_as_a_dry_run_is_refused():
    views = views_in_sql_files()
    text = receipt_text(failed=views[2])
    assert any("failed" in p for p in js.dry_run_receipt_problems(text.encode("utf-8"), bound_for(text), head_tree()))
    text = receipt_text(tail="")
    assert any("dry run only" in p for p in js.dry_run_receipt_problems(text.encode("utf-8"), bound_for(text), head_tree()))
    text = receipt_text(tail="created\n")
    assert any("dry run only" in p for p in js.dry_run_receipt_problems(text.encode("utf-8"), bound_for(text), head_tree()))


def test_js09_the_check_the_acting_path_calls_stops_with_a_code_when_the_receipt_is_missing_or_drifts(tmp_path):
    text = receipt_text()
    path = tmp_path / "apply-dry-run.txt"
    bound = {"dryRunReceiptPath": str(path), "dryRunReceiptSha256": bound_for(text)}
    with pytest.raises(so.Stop) as stop:
        js.require_dry_run_receipt(bound, head_tree())
    assert stop.value.code == "DRY_RUN_RECEIPT"
    path.write_bytes(text.encode("utf-8"))
    js.require_dry_run_receipt(bound, head_tree())
    drifted = receipt_text(drift=[views_in_sql_files()[0]])
    path.write_bytes(drifted.encode("utf-8"))
    with pytest.raises(so.Stop) as stop:
        js.require_dry_run_receipt({**bound, "dryRunReceiptSha256": bound_for(drifted)}, head_tree())
    assert stop.value.code == "DRY_RUN_RECEIPT"
