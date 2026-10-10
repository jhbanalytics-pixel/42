"""Durable effects (W8-REL 4, DE-01 to DE-15): the manifest validator, the schema set equality, the jobs-owned DDL derived
from the a80 archive, and the three read-only checks. No BigQuery client is built: a double records every call, and a
refused manifest is shown to have made none."""
import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from core.schema import apply
from core.setup import durable_effects_check as de

ROOT = Path(__file__).resolve().parents[3]
A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
OWNER = {"lane": 3, "person": "Albert Meintjes"}
GOOD_BLOCKER_SQL = "SELECT COUNT(*) AS n FROM `ogilvy-trends-v2.intelligence_42_agent.holds` WHERE created_at >= '2026-10-09'"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def a80_files():
    if subprocess.run(["git", "cat-file", "-e", A80], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.fail(f"commit {A80} is not available in this checkout; a shallow clone cannot run the release tests")
    return de.git_tree_files(A80, "core", ROOT)


def head_texts():
    return [(ROOT / "core" / "schema" / "agent.sql").read_text(encoding="utf-8"), (ROOT / "core" / "schema" / "core.sql").read_text(encoding="utf-8")]


def effect(effect_id="E01", **over):
    statement = "CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_agent.notes` (id STRING)"
    base = {
        "effect_id": effect_id, "kind": "schema", "target": "intelligence_42_agent.notes", "change": "create_table", "apply_order": 1,
        "apply_phase": "before_candidate", "owner": dict(OWNER), "statement_ref": statement, "statement_sha256": sha(statement),
        "apply_kind": "schema", "additive": True, "destructive": False,
        "consumers": [{"component": "f42-api", "image": "f42-web", "file": "core/api/store.py", "line": 10, "access": "read"}],
        "native_readback": {"kind": "information_schema_columns", "target": "intelligence_42_agent.notes", "expected_result_sha256": "ab" * 32},
        "compat": {k: {"verdict": "pass", "test": "DE-05"} for k in de.COMPAT_KEYS},
        "forward_recovery": {"kind": "none_needed", "statement_ref": "", "statement_sha256": "", "note": "additive"},
        "authority_or_holds": False, "evidence": {},
    }
    base.update(over)
    return base


def seal(manifest):
    manifest["manifest_sha256"] = de.manifest_hash(manifest)
    return manifest


def manifest(*effects, **over):
    value = {"schema_version": 1, "release_id": "rel-d666ef6-01", "source": {"commit": "d6" * 20, "tree": "9a" * 20},
             "effects": list(effects), "no_storage_change": {}, "rollback_blockers": [], "a80_ddl_writers": [],
             "generated_from": {"base": A80, "head": "d6" * 20}, "reviewed_by": ""}
    value.update(over)
    return seal(value)


def blocker(query=GOOD_BLOCKER_SQL, cap=10_000_000):
    return {"condition_query_ref": query, "condition_query_sha256": sha(query), "bytes_cap": cap, "blocks_when": "count_gt_0"}


class FakeBQ:
    def __init__(self, dry=1000, rows=None, fail=False):
        self.dry, self.rows, self.fail, self.calls = dry, rows if rows is not None else [{"n": 0}], fail, []

    def dry_run(self, sql, params=None):
        self.calls.append(("dry_run", sql))
        return self.dry

    def query(self, sql, params=None, max_bytes=None):
        self.calls.append(("query", sql, max_bytes))
        if self.fail:
            raise RuntimeError("cannot run")
        return self.rows


def problems(value):
    return de.validate_manifest(value)


def has(found, fragment):
    return any(fragment in p for p in found)


# DE-01: the validator

def test_de01_a_complete_manifest_is_valid_and_a_release_with_no_storage_change_still_has_the_file():
    assert problems(manifest(effect())) == []
    assert problems(manifest(no_storage_change={"claimed": True})) == []
    assert has(problems(manifest()), "no effects and no no_storage_change claim")


@pytest.mark.parametrize("field", de.RELEASE_FIELDS)
def test_de01_every_release_field_is_required(field):
    value = manifest(effect())
    value.pop(field)
    assert has(problems(value), f"missing {field}") or field == "schema_version" and has(problems(value), "schema_version")


@pytest.mark.parametrize("field", de.EFFECT_FIELDS)
def test_de01_every_effect_field_is_required(field):
    e = effect()
    e.pop(field)
    assert has(problems(manifest(e)), f"missing {field}")


def test_de01_an_unknown_schema_version_is_refused():
    assert has(problems(seal({**manifest(effect()), "schema_version": 2})), "schema_version")


@pytest.mark.parametrize("over, fragment", [
    ({"destructive": True}, "destructive"), ({"additive": False}, "additive"), ({"kind": "iam"}, "not allowed"),
    ({"apply_kind": "gcloud run services update"}, "fixed allowlist"), ({"apply_kind": "bq query drop"}, "fixed allowlist"),
    ({"change": "drop_table"}, "unknown change"), ({"apply_phase": "whenever"}, "apply_phase"),
    ({"apply_order": "1"}, "apply_order"), ({"owner": "the builder"}, "owner"), ({"owner": {"lane": 3, "person": ""}}, "owner"),
    ({"owner": {"lane": 3, "person": "TO BE NAMED"}}, "owner"), ({"owner": {"lane": "3", "person": "Albert Meintjes"}}, "owner"),
    ({"consumers": [{"component": "x", "image": "other", "file": "f"}]}, "consumers"),
    ({"compat": {"a80_readers": {}}}, "compat"),
])
def test_de01_unsafe_or_incomplete_effects_are_refused(over, fragment):
    assert has(problems(manifest(effect(**over))), fragment)


def test_de01_a_schema_effect_must_apply_through_the_schema_allowlist_entry():
    assert has(problems(manifest(effect(apply_kind="code"))), "applies through core.schema.apply")


def test_de01_a_tie_in_apply_order_and_a_repeated_id_are_errors():
    assert has(problems(manifest(effect("E01"), effect("E02"))), "apply_order has a tie")
    assert has(problems(manifest(effect("E01", apply_order=1), effect("E01", apply_order=2))), "effect_id is repeated")


@pytest.mark.parametrize("readback", [
    {"kind": "information_schema_columns", "target": "a.b", "expected_result_sha256": "ab" * 32, "command": "bq query"},
    {"kind": "information_schema_columns", "target": "a.b"},
    {"kind": "free_sql", "target": "a.b", "expected_result_sha256": "ab" * 32},
    {"command": "bq query select 1"},
])
def test_de01_a_native_readback_is_a_typed_read_and_never_free_command_text(readback):
    assert has(problems(manifest(effect(native_readback=readback))), "native_readback")


def test_de01_the_recorded_hash_must_equal_a_recomputation():
    value = manifest(effect())
    value["effects"][0]["note"] = "edited after sealing"
    assert has(problems(value), "manifest_sha256")


# DE-02, DE-03, DE-11: the schema files

def test_de02_the_statements_apply_would_run_minus_a80_are_the_manifest_schema_effects():
    a80 = [t for p, t in a80_files() if p in ("core/schema/agent.sql", "core/schema/core.sql")]
    assert [p for p, _ in a80_files() if p.startswith("core/schema/")]
    old = [dict(path=p, text=t) for p, t in a80_files() if p in ("core/schema/agent.sql", "core/schema/core.sql")]
    ordered = [next(o["text"] for o in old if o["path"] == name) for name in ("core/schema/agent.sql", "core/schema/core.sql")]
    assert a80 and de.new_statements(ordered, ordered) == []
    same = de.check_schema_effects(manifest(), head_texts(), ordered)
    # whatever this branch added since a80 must be listed; with no effects listed every new statement is reported
    assert all("not in the manifest" in p or "skip silently" in p for p in same)


def test_de02_an_added_statement_is_listed_by_hash_or_reported():
    old = ["CREATE TABLE IF NOT EXISTS `p.d.a` (x INT64);"]
    new = old[0] + "\nCREATE TABLE IF NOT EXISTS `p.d.b` (y INT64);"
    statement = "CREATE TABLE IF NOT EXISTS `p.d.b` (y INT64)"
    listed = manifest(effect(statement_ref=statement, statement_sha256=sha(statement)))
    assert de.check_schema_effects(listed, [new], old) == []
    assert has(de.check_schema_effects(manifest(), [new], old), "not in the manifest")


def test_de02_a_listed_statement_that_apply_would_not_run_fails():
    old = ["CREATE TABLE IF NOT EXISTS `p.d.a` (x INT64);"]
    statement = "CREATE TABLE IF NOT EXISTS `p.d.ghost` (y INT64)"
    found = de.check_schema_effects(manifest(effect(statement_ref=statement, statement_sha256=sha(statement))), old, old)
    assert has(found, "would not run")


def test_de02_a_statement_text_that_does_not_hash_to_its_sha_fails():
    old = ["CREATE TABLE IF NOT EXISTS `p.d.a` (x INT64);"]
    statement = "CREATE TABLE IF NOT EXISTS `p.d.b` (y INT64)"
    found = de.check_schema_effects(manifest(effect(statement_ref=statement, statement_sha256="0" * 64)), [old[0] + statement + ";"], old)
    assert has(found, "does not match statement_ref")


def test_de03_a_statement_apply_does_not_recognise_is_found_not_skipped_silently():
    old = ["CREATE TABLE IF NOT EXISTS `p.d.a` (x INT64);"]
    new = old[0] + "\nCREATE OR REPLACE VIEW `p.d.v` AS SELECT 1;"
    assert has(de.check_schema_effects(manifest(), [new], old), "skip silently")
    statement = "CREATE OR REPLACE VIEW `p.d.v` AS SELECT 1"
    found = de.check_schema_effects(manifest(effect(statement_ref=statement, statement_sha256=sha(statement))), [new], old)
    assert has(found, "not a statement apply.py recognises")


def test_de03_the_restated_kind_function_agrees_with_core_schema_apply_on_every_current_statement():
    for statement in de.schema_statements(head_texts()):
        assert de.statement_kind(statement) == apply.kind(statement), statement[:60]
    assert de.split_statements(head_texts()[0]) == apply.split_statements(head_texts()[0])


def test_de11_an_unlisted_statement_added_to_core_sql_fails_set_equality():
    texts = head_texts()
    extra = texts[:1] + [texts[1] + "\nCREATE TABLE IF NOT EXISTS `p.intelligence_42_core.unlisted_t` (x INT64);"]
    found = de.check_schema_effects(manifest(), extra, texts)
    assert has(found, "unlisted_t")


# DE-04: no storage change

def test_de04_a_claim_of_no_change_passes_when_every_name_is_in_the_schema():
    texts = head_texts()
    claim = {"claimed": True, "references": [{"table": "runs", "column": "answer"}, {"table": "posts", "column": ""}]}
    assert de.check_no_storage_change(claim, texts, texts) == []


def test_de04_a_column_or_table_the_schema_does_not_define_fails():
    texts = head_texts()
    assert has(de.check_no_storage_change({"references": [{"table": "runs", "column": "no_such_column_xyz"}]}, texts, texts), "column runs.no_such_column_xyz")
    assert has(de.check_no_storage_change({"references": [{"table": "no_such_table_xyz", "column": ""}]}, texts, texts), "table no_such_table_xyz")


def test_de04_a_claim_with_a_diff_that_adds_a_schema_statement_fails():
    texts = head_texts()
    added = [texts[0], texts[1] + "\nCREATE TABLE IF NOT EXISTS `p.d.fresh_t` (x INT64);"]
    assert has(de.check_no_storage_change({"claimed": True, "references": []}, added, texts), "adds a schema statement")


# DE-08: consumers

def test_de08_every_changed_file_that_mentions_a_target_is_a_listed_consumer():
    value = manifest(effect())
    changed = {"core/api/store.py": "reads notes", "core/api/other.py": "also reads notes", "core/api/unrelated.py": "nothing"}
    found = de.check_consumers(value, changed)
    assert found == ["effect E01: core/api/other.py mentions notes but is not in consumers"]


# DE-09: unsafe recovery and blocker queries

@pytest.mark.parametrize("statement", ["DROP TABLE x", "DELETE FROM x WHERE 1=1", "TRUNCATE TABLE x", "CREATE OR REPLACE VIEW v AS SELECT 1", "drop table x"])
def test_de09_a_forward_recovery_never_drops_deletes_truncates_or_replaces(statement):
    recovery = {"kind": "compensating_statement", "statement_ref": statement, "statement_sha256": sha(statement), "note": ""}
    assert has(problems(manifest(effect(forward_recovery=recovery))), "drop, delete, truncate or replace")


def test_de09_a_safe_compensating_statement_is_valid():
    statement = "UPDATE `p.d.t` SET flag = FALSE WHERE flag"
    recovery = {"kind": "compensating_statement", "statement_ref": statement, "statement_sha256": sha(statement), "note": ""}
    assert problems(manifest(effect(forward_recovery=recovery))) == []


@pytest.mark.parametrize("query, fragment", [
    (GOOD_BLOCKER_SQL + "; SELECT 2", "more than one statement"), ("INSERT INTO t SELECT 1", "not a SELECT"),
    ("SELECT 1 FROM t WHERE x = (SELECT 1) AND DROP", "DROP"), ("select count(*) from t where merge", "MERGE"),
    ("WITH a AS (SELECT 1) SELECT * FROM a, delete", "DELETE"), ("SELECT 1 /* update */", "UPDATE"),
    ("SELECT 1 FROM t -- truncate", "TRUNCATE"), ("SELECT create_time FROM t", None),
])
def test_de09_a_blocker_query_is_one_select_with_none_of_the_eight_write_words(query, fragment):
    assert (de.scan_blocker_query(query) is None) == (fragment is None)
    if fragment:
        assert fragment in de.scan_blocker_query(query)


def test_de09_a_blocker_whose_hash_does_not_match_its_text_is_invalid():
    bad = {**blocker(), "condition_query_sha256": "0" * 64}
    e = effect(authority_or_holds=True, rollback_blocker=bad)
    assert has(problems(manifest(e, rollback_blockers=[bad])), "hash does not match")


# DE-10: rollback blockers

def blocked_manifest():
    b = blocker()
    return manifest(effect(authority_or_holds=True, rollback_blocker=b), rollback_blockers=[b])


def test_de10_with_no_blockers_listed_the_answer_is_not_blocked_and_no_query_runs():
    bq = FakeBQ()
    assert de.run_rollback_blockers(manifest(effect()), bq) == {"schema_version": 1, "check": "rollback-blockers", "blocked": False, "blockers": 0}
    assert bq.calls == []


def test_de10_rows_created_since_the_marker_block_and_none_do_not():
    assert de.run_rollback_blockers(blocked_manifest(), FakeBQ(rows=[{"n": 3}]))["blocked"] is True
    assert de.run_rollback_blockers(blocked_manifest(), FakeBQ(rows=[{"n": 0}]))["blocked"] is False


def test_de10_a_blocker_query_that_cannot_run_blocks_when_blockers_are_listed():
    assert de.run_rollback_blockers(blocked_manifest(), FakeBQ(fail=True))["blocked"] is True
    assert de.run_rollback_blockers(blocked_manifest(), FakeBQ(dry=10**12))["blocked"] is True


def test_de10_the_dry_run_comes_before_the_query_and_the_cap_is_passed_to_it():
    bq = FakeBQ()
    de.run_rollback_blockers(blocked_manifest(), bq)
    assert [c[0] for c in bq.calls] == ["dry_run", "query"] and bq.calls[1][2] == 10_000_000


# DE-12, DE-13: the jobs image and the a80 readers and writers

def test_de12_the_jobs_owned_ddl_is_derived_from_the_a80_archive_and_holds_the_views_the_review_found():
    owned = de.derive_ddl_writers(a80_files())
    for name in ("v_forecasts_current", "v_breaking_signals_current", "v_item_waves"):
        assert name in owned, name
    assert any(path.startswith("core/detect/") for path in owned["v_forecasts_current"])


def test_de12_the_derived_set_is_a_superset_of_every_statement_set_the_detect_job_applies():
    from core.detect import sqlrun

    owned = set(de.derive_ddl_writers(a80_files()))
    named = {sqlrun.object_name(s).rsplit(".", 1)[-1] for s in sqlrun.statements() + sqlrun.agent_statements() + sqlrun.news_statements() + sqlrun.spread_statements()}
    assert named and named <= owned, sorted(named - owned)


def test_de12_a_create_or_replace_added_to_any_non_test_file_appears_with_no_other_edit():
    files = list(a80_files())
    assert "v_fresh_view" not in de.derive_ddl_writers(files)
    added = files + [("core/api/new_module.py", 'SQL = "CREATE OR REPLACE VIEW `{core}.v_fresh_view` AS SELECT 1"')]
    assert de.derive_ddl_writers(added)["v_fresh_view"] == ["core/api/new_module.py"]
    ignored = files + [("core/api/tests/test_x.py", 'SQL = "CREATE OR REPLACE VIEW x.v_in_a_test AS SELECT 1"'),
                       ("core/schema/extra.sql", "CREATE OR REPLACE VIEW x.v_schema_owned AS SELECT 1")]
    derived = de.derive_ddl_writers(ignored)
    assert "v_in_a_test" not in derived and "v_schema_owned" not in derived


def test_de12_the_derivation_holds_no_literal_list_of_object_names():
    text = Path(de.__file__).read_text(encoding="utf-8")
    for name in ("v_forecasts_current", "v_breaking_signals_current", "v_item_waves", "v_good_runs", "v_item_state_current"):
        assert name not in text


def test_de12_an_effect_or_a_referenced_object_inside_the_derived_set_fails_unless_the_columns_are_proved():
    files = a80_files()
    view = effect(kind="view", change="create_view", target="intelligence_42_core.v_forecasts_current")
    assert has(de.check_services_only_isolation([view], [], files, files), "v_forecasts_current")
    assert de.check_services_only_isolation([view], [], files, files, column_proofs={"E01": True}) == []
    assert has(de.check_services_only_isolation([], ["intelligence_42_agent.v_breaking_signals_current"], files, files), "v_breaking_signals_current")
    assert de.check_services_only_isolation([effect()], ["intelligence_42_agent.notes"], files, files) == []


def test_de13_a_positional_insert_or_a_select_star_reader_of_a_table_is_found():
    files = [("core/a.py", "INSERT INTO `p.d.posts` SELECT * FROM staging"), ("core/b.py", "INSERT INTO `p.d.posts` (id, text) SELECT id, text FROM s"),
             ("core/c.py", "q = 'SELECT * FROM `p.d.posts` WHERE x'"), ("core/tests/t.py", "INSERT INTO `p.d.posts` VALUES (1)"),
             ("core/d.py", "INSERT INTO `p.d.runs` VALUES (1, 2)")]
    assert de.positional_writers("d.posts", files) == ["core/a.py"]
    assert de.positional_writers("d.runs", files) == ["core/d.py"]
    assert de.select_star_readers("d.posts", files) == ["core/c.py"]
    assert de.positional_writers("d.nothing", files) == [] == de.select_star_readers("d.nothing", files)


def test_de13_the_a80_scan_runs_over_the_real_archive():
    files = a80_files()
    assert isinstance(de.positional_writers("intelligence_42_agent.runs", files), list)
    assert isinstance(de.select_star_readers("intelligence_42_agent.runs", files), list)


# DE-14 and DE-15: versions, and refusals that make no BigQuery call

def run_main(tmp_path, check, manifest_value=None, bindings=None, bq=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    args = ["--check", check, "--evidence", str(tmp_path / "evidence")]
    if manifest_value is not None:
        (tmp_path / "m.json").write_text(json.dumps(manifest_value), encoding="utf-8")
        args += ["--manifest", str(tmp_path / "m.json")]
    if bindings is not None:
        (tmp_path / "b.json").write_text(json.dumps(bindings), encoding="utf-8")
        args += ["--bindings", str(tmp_path / "b.json")]
    code = de.main(args, bq_factory=lambda: bq)
    return code


def test_de14_the_checker_output_carries_a_schema_version_and_unknown_inputs_are_refused(tmp_path, capsys):
    assert run_main(tmp_path, "rollback-blockers", manifest(effect()), bq=FakeBQ()) == 0
    assert json.loads((tmp_path / "evidence" / "rollback-blockers.json").read_text(encoding="utf-8"))["schema_version"] == 1
    assert run_main(tmp_path / "v2", "rollback-blockers", {**manifest(effect()), "schema_version": 2}, bq=FakeBQ()) == 1
    bad = {"schema_version": 2, "inflightAsksSqlSha256": de.INFLIGHT_ASKS_SQL_SHA256, "inflightAsksBytesCap": 10**6}
    assert run_main(tmp_path / "v3", "inflight-asks", bindings=bad, bq=FakeBQ()) == 1


@pytest.mark.parametrize("name", ["command_key", "unknown_kind", "forbidden_word", "two_statements", "hash_mismatch"])
def test_de15_a_refused_manifest_makes_no_bigquery_call(tmp_path, name):
    bq = FakeBQ()
    if name == "command_key":
        e = effect(native_readback={"kind": "information_schema_columns", "target": "a.b", "expected_result_sha256": "ab" * 32, "command": "bq query"})
        value, check = manifest(e), "readbacks"
    elif name == "unknown_kind":
        value, check = manifest(effect(native_readback={"kind": "free_sql", "target": "a.b", "expected_result_sha256": "ab" * 32})), "readbacks"
    else:
        query = {"forbidden_word": GOOD_BLOCKER_SQL + " AND drop", "two_statements": GOOD_BLOCKER_SQL + "; SELECT 2", "hash_mismatch": GOOD_BLOCKER_SQL}[name]
        b = {**blocker(query), "condition_query_sha256": "0" * 64 if name == "hash_mismatch" else sha(query)}
        value, check = manifest(rollback_blockers=[b], no_storage_change={"claimed": True}), "rollback-blockers"
    assert run_main(tmp_path, check, value, bq=bq) == 1
    assert bq.calls == []


def bindings(**over):
    value = {"schema_version": 1, "inflightAsksSqlSha256": de.INFLIGHT_ASKS_SQL_SHA256, "inflightAsksBytesCap": 5_000_000}
    value.update(over)
    return value


def test_de15_an_inflight_template_whose_hash_differs_from_the_bound_one_makes_no_bigquery_call(tmp_path):
    bq = FakeBQ()
    assert run_main(tmp_path, "inflight-asks", bindings=bindings(inflightAsksSqlSha256="0" * 64), bq=bq) == 1
    assert bq.calls == []


def test_the_inflight_count_dry_runs_first_under_the_bound_cap_and_reports_the_running_asks(tmp_path, capsys):
    bq = FakeBQ(rows=[{"running": 2}])
    assert run_main(tmp_path, "inflight-asks", bindings=bindings(), bq=bq) == 0
    assert [c[0] for c in bq.calls] == ["dry_run", "query"] and bq.calls[1][2] == 5_000_000
    assert json.loads(capsys.readouterr().out)["running"] == 2
    over = FakeBQ(dry=10**9)
    assert run_main(tmp_path / "over", "inflight-asks", bindings=bindings(), bq=over) == 1
    assert [c[0] for c in over.calls] == ["dry_run"]
    assert run_main(tmp_path / "cap", "inflight-asks", bindings=bindings(inflightAsksBytesCap=0), bq=FakeBQ()) == 1


def test_the_inflight_template_is_read_only_and_its_hash_is_the_constant():
    assert de.INFLIGHT_ASKS_SQL_SHA256 == sha(de.INFLIGHT_ASKS_SQL)
    assert de.scan_blocker_query(de.INFLIGHT_ASKS_SQL) is None
    assert "stage = 'ask'" in de.INFLIGHT_ASKS_SQL and "'running'" in de.INFLIGHT_ASKS_SQL


def test_the_readbacks_check_compares_each_typed_read_with_its_expected_hash(tmp_path):
    rows = [{"column_name": "id"}]
    good = effect(native_readback={"kind": "information_schema_columns", "target": "intelligence_42_agent.notes",
                                   "expected_result_sha256": sha(de.canonical(rows))})
    bq = FakeBQ(rows=rows)
    assert run_main(tmp_path, "readbacks", manifest(good), bq=bq) == 0
    assert [c[0] for c in bq.calls] == ["dry_run", "query"] and "INFORMATION_SCHEMA.COLUMNS" in bq.calls[0][1]
    assert run_main(tmp_path / "bad", "readbacks", manifest(effect()), bq=FakeBQ(rows=rows)) == 1


@pytest.mark.parametrize("target", ["a", "a.b.c", "a.b; DROP", "`a`.b", "a.b#c#"])
def test_a_native_readback_target_that_is_not_a_plain_identifier_never_becomes_sql(target):
    with pytest.raises(de.Refusal):
        de.readback_sql({"kind": "information_schema_columns", "target": target, "expected_result_sha256": "ab" * 32})


def test_the_since_marker_readback_validates_every_part_of_its_target():
    sql, _ = de.readback_sql({"kind": "row_count_since_marker", "target": "intelligence_42_agent.holds#created_at#2026-10-09", "expected_result_sha256": "ab" * 32})
    assert "COUNT(*)" in sql and "holds" in sql
    with pytest.raises(de.Refusal):
        de.readback_sql({"kind": "row_count_since_marker", "target": "d.t#c#x' OR '1'='1", "expected_result_sha256": "ab" * 32})


def test_the_checker_names_no_secret_and_makes_no_call_of_its_own_beyond_git_archive_and_bigquery():
    text = Path(de.__file__).read_text(encoding="utf-8").lower()
    for word in ("requests", "urllib", "socket", "token", "password", "gcloud"):
        assert word not in text, word


# the Release A template: the five Cloud Run effects, pre-filled, owners left to be named

TEMPLATE = ROOT / "core" / "setup" / "release" / "durable-effects-manifest.template.json"


def test_the_release_a_template_holds_the_five_cloud_run_effects_all_non_destructive():
    value = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert [e["effect_id"] for e in value["effects"]] == ["E-TRAFFIC", "E-TAG-A", "E-TAG-W", "E-ENV-AUD", "E-ENV-URL"]
    assert all(e["destructive"] is False and e["kind"] != "iam" for e in value["effects"])
    assert {e["kind"] for e in value["effects"]} == {"cloud_run_traffic", "cloud_run_tag", "env"}


def test_the_template_is_not_a_valid_manifest_until_owners_are_named_a_reviewer_fills_in_and_it_is_sealed():
    found = problems(json.loads(TEMPLATE.read_text(encoding="utf-8")))
    assert has(found, "owner is not a lane and a named person") and has(found, "manifest_sha256")
    assert not [p for p in found if "owner" not in p and "manifest_sha256" not in p], found


def test_the_template_with_named_owners_sealed_is_valid_and_the_builder_never_fills_reviewed_by():
    value = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert value["reviewed_by"] == ""
    for e in value["effects"]:
        e["owner"] = dict(OWNER)
    assert problems(seal(value)) == []


def test_a_helper_phase_readback_names_a_phase_the_helper_has_and_is_not_run_against_bigquery(tmp_path):
    value = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    for e in value["effects"]:
        e["owner"] = dict(OWNER)
    bq = FakeBQ()
    assert run_main(tmp_path, "readbacks", seal(value), bq=bq) == 0 and bq.calls == []
    value["effects"][0]["native_readback"]["target"] = "AfterEverything"
    assert has(problems(seal(value)), "phase the helper does not have")


# edges the first mutant pass found nothing pinned

def test_an_owner_needs_a_lane_and_a_full_name():
    assert has(problems(manifest(effect(owner={"lane": 3, "person": "Albert"}))), "owner")


def test_a_forward_recovery_kind_outside_the_three_is_refused_and_its_statement_must_hash():
    assert has(problems(manifest(effect(forward_recovery={"kind": "drop_table", "statement_ref": "", "statement_sha256": ""}))), "forward_recovery kind")
    statement = "UPDATE `p.d.t` SET flag = FALSE WHERE flag"
    wrong = {"kind": "compensating_statement", "statement_ref": statement, "statement_sha256": "0" * 64, "note": ""}
    assert has(problems(manifest(effect(forward_recovery=wrong))), "forward_recovery hash")


def test_an_effect_that_creates_authority_needs_a_blocker_and_the_release_lists_one_per_such_effect():
    assert has(problems(manifest(effect(authority_or_holds=True))), "needs a rollback_blocker")
    b = blocker()
    assert has(problems(manifest(effect(authority_or_holds=True, rollback_blocker=b), rollback_blockers=[])), "does not list one blocker")
    assert has(problems(manifest(effect(), rollback_blockers=[b])), "does not list one blocker")


@pytest.mark.parametrize("cap", [0, -1, True, "5", None])
def test_a_blocker_bytes_cap_is_a_positive_integer(cap):
    b = {**blocker(), "bytes_cap": cap}
    assert has(problems(manifest(effect(authority_or_holds=True, rollback_blocker=b), rollback_blockers=[b])), "bytes_cap")


def test_a_jobs_owned_object_is_found_whether_it_exists_at_a80_only_or_at_the_release_commit_only():
    old = [("core/detect/old.py", 'SQL = "CREATE OR REPLACE VIEW `x.v_old_only` AS SELECT 1"')]
    new = [("core/detect/new.py", 'SQL = "CREATE OR REPLACE VIEW `x.v_new_only` AS SELECT 1"')]
    for target in ("x.v_old_only", "x.v_new_only"):
        view = effect(kind="view", change="create_view", target=target)
        assert has(de.check_services_only_isolation([view], [], old, new), target.split(".")[1])
    assert has(de.check_services_only_isolation([], ["x.v_old_only"], old, new), "v_old_only")
    assert has(de.check_services_only_isolation([], ["x.v_new_only"], old, new), "v_new_only")


@pytest.mark.parametrize("cap", [True, False, "5000000", None, 0, -1])
def test_an_inflight_cap_that_is_not_a_positive_integer_makes_no_bigquery_call(tmp_path, cap):
    bq = FakeBQ(dry=0, rows=[{"running": 0}])
    assert run_main(tmp_path, "inflight-asks", bindings=bindings(inflightAsksBytesCap=cap), bq=bq) == 1
    assert bq.calls == []


def test_an_inflight_bindings_file_of_an_unknown_version_is_refused_even_when_everything_else_is_right(tmp_path):
    bq = FakeBQ(rows=[{"running": 0}])
    assert run_main(tmp_path, "inflight-asks", bindings=bindings(schema_version=2), bq=bq) == 1
    assert bq.calls == []


def test_a_readback_with_a_command_key_never_becomes_sql():
    with pytest.raises(de.Refusal):
        de.readback_sql({"kind": "information_schema_columns", "target": "a.b", "expected_result_sha256": "ab" * 32, "command": "bq query"})
    with pytest.raises(de.Refusal):
        de.readback_sql({"kind": "free_sql", "target": "a.b", "expected_result_sha256": "ab" * 32})


def test_the_readbacks_check_refuses_an_invalid_manifest_before_any_bigquery_call():
    bq = FakeBQ()
    with pytest.raises(de.Refusal):
        de.run_readbacks(manifest(effect(destructive=True)), bq)
    assert bq.calls == []


def test_the_rollback_blockers_output_carries_a_schema_version_when_blockers_exist():
    assert de.run_rollback_blockers(blocked_manifest(), FakeBQ())["schema_version"] == 1


# RJ-3: a schema effect is read back from information_schema, never by a phase of the helper or a deferred read

DEFERRED_READBACKS = [
    {"kind": "helper_phase", "target": "BeforeCandidate", "expected_result_sha256": "ab" * 32},
    {"kind": "row_count_since_marker", "target": "intelligence_42_agent.notes#created_at#2026-10-09", "expected_result_sha256": "ab" * 32},
]


@pytest.mark.parametrize("readback", DEFERRED_READBACKS, ids=lambda r: r["kind"])
def test_rj3_a_schema_effect_with_a_helper_phase_or_deferred_readback_is_refused_by_the_effect_and_the_manifest(readback):
    assert has(de.validate_effect(effect(native_readback=readback), "effect E01"), "a schema effect must carry an information_schema_columns readback")
    assert has(problems(manifest(effect(native_readback=readback))), "a schema effect must carry an information_schema_columns readback")


def test_rj3_a_schema_effect_with_the_information_schema_readback_and_an_effect_of_another_kind_with_a_helper_phase_are_still_accepted():
    tag = effect("E02", kind="cloud_run_tag", target="f42-api", change="tag_add", apply_order=2, apply_kind="tag_add",
                 native_readback={"kind": "helper_phase", "target": "AfterSmoke", "expected_result_sha256": "ab" * 32})
    assert problems(manifest(effect(), tag)) == []
