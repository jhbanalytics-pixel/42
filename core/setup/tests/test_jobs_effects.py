"""B's durable effects manifest (W8-REL-B v2.1 section 3.3; JS-01, JS-02, JS-04 to JS-06, JS-11; carry item RB-C4). Offline: the
manifest is built from the tree of this checkout and the a80 archive, then judged by the checker's own functions and by
independent derivations from the schema files."""
import copy
import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from core.schema import apply
from core.schema.tests.test_schema import parse_create
from core.setup import durable_effects_check as de
from core.setup.release import jobs_effects as je
from core.setup.release import jobs_source as js
from core.setup.release import services_only as so

ROOT = Path(__file__).resolve().parents[3]
A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
COMMIT = "5e1c0de9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3"
TREE = "9aaee874e0e06e54d3354ae58bf6e576685b5303"
RID = "rel-5e1c0de-01"
OWNER = {"lane": 3, "person": "Albert Meintjes"}

# The final column list of each table the release creates or changes, pinned as literals from core/schema at this checkout.
COLUMNS = {
    "intelligence_42_agent.runs": ['answer', 'calls', 'counts', 'credits', 'error', 'finished_at', 'model_usd', 'outcome', 'plan', 'question', 'record', 'run_date', 'run_id', 'seconds', 'stage', 'stamp', 'started_at', 'status', 'tier', 'tokens'],
    "intelligence_42_agent.claim_checks": ['answer_or_brief_id', 'checker', 'claim_id', 'reason', 'reason_code', 'rule', 'run_id', 'span_sha256', 'verdict'],
    "intelligence_42_core.post_items": ['item_id', 'link_market', 'linked_on', 'post_id', 'via'],
    "intelligence_42_core.early_signal": ['alpha', 'baseline_date', 'baseline_mode', 'baseline_source', 'cusum', 'days_used', 'early', 'h', 'item_id', 'kappa', 'kind', 'lane_class', 'market', 'metric_date', 'mu', 'platform', 'protocol', 'rule_version', 'run_days', 'run_id', 'series', 'series_id', 'window_days', 'y'],
    "intelligence_42_core.item_locality": ['breadth_creators', 'computed_at', 'detect_run_id', 'feed_only_creators', 'feed_only_posts', 'foreign_posts', 'item_id', 'known_creators', 'known_posts', 'local_creators', 'local_posts', 'local_share', 'market', 'metric_version', 'population_cutoff', 'population_digest', 'population_posts', 'run_date', 'schema_version', 'status', 'unknown_posts', 'vetoed_feed_posts'],
    "intelligence_42_core.item_locality_post": ['creator_key', 'detect_run_id', 'feed_obs_date', 'feed_sighted', 'geo_confidence', 'geo_market', 'geo_source', 'item_id', 'locality_class', 'market', 'metric_version', 'platform', 'population_cutoff', 'post_id', 'run_date'],
    "intelligence_42_core.item_locality_verified": ['detect_run_id', 'item_id', 'market', 'member_rows', 'metric_version', 'population_digest', 'run_date', 'verified_at'],
    "intelligence_42_core.post_item_lineage": ['item_id', 'lineage_id', 'link_market', 'linked_on', 'post_id'],
    "intelligence_42_core.post_item_end": ['ended_on', 'item_id', 'lineage_id', 'post_id', 'reason', 'recorded_at'],
}
# effect id -> table of the statement, in apply order
SCHEMA_IDS = [
    ("E-RUNS-DDL", "intelligence_42_agent.runs"), ("E-CLAIM-DDL", "intelligence_42_agent.claim_checks"),
    ("E-POSTITEMS-DDL", "intelligence_42_core.post_items"), ("E-LOC-TABLES-1", "intelligence_42_core.item_locality"),
    ("E-LOC-TABLES-2", "intelligence_42_core.item_locality_post"), ("E-LOC-TABLES-3", "intelligence_42_core.item_locality_verified"),
    ("E-LOC-TABLES-4", "intelligence_42_core.post_item_lineage"), ("E-LOC-TABLES-5", "intelligence_42_core.post_item_end"),
    ("E-EARLY", "intelligence_42_core.early_signal"), ("E-RUNS-STAMP", "intelligence_42_agent.runs"),
    ("E-CLAIM-SPAN", "intelligence_42_agent.claim_checks"), ("E-CLAIM-CODE", "intelligence_42_agent.claim_checks"),
    ("E-POSTITEMS-LINKED", "intelligence_42_core.post_items"), ("E-POSTITEMS-MARKET", "intelligence_42_core.post_items"),
]
# the same five, in the order detect creates them: views.sql in file order, then locality_views.sql in file order
DDL_APPLY_ORDER = ["v_collection_health_current", "v_item_market_scope", "tvf_post_items", "v_item_locality_checked", "v_item_locality_current"]
CHANGED_DDL = ["tvf_post_items", "v_collection_health_current", "v_item_locality_checked", "v_item_locality_current", "v_item_market_scope"]


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def trees():
    if subprocess.run(["git", "cat-file", "-e", A80], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.fail(f"commit {A80} is not available in this checkout")
    return js.tree_from_disk(ROOT), de.git_tree_files(A80, "core", ROOT)


@pytest.fixture(scope="module")
def built(trees):
    head, a80 = trees
    return je.build_jobs_manifest(head, a80, release_id=RID, commit=COMMIT, tree=TREE, owner=OWNER)


@pytest.fixture
def manifest(built):
    return copy.deepcopy(built)


def seal(value):
    value["manifest_sha256"] = de.manifest_hash(value)
    return value


def effect(value, effect_id):
    [found] = [e for e in value["effects"] if e["effect_id"] == effect_id]
    return found


def schema_effects(value):
    return [e for e in value["effects"] if e["kind"] == "schema"]


# JS-05: the checker's allowlists

def test_js05_the_allowlists_gain_exactly_the_job_image_kind_the_six_phases_and_the_chain_manifest_readback():
    assert de.EFFECT_KINDS == ("schema", "view", "receipt", "reservation", "stored_answer", "config", "env", "secret", "cloud_run_tag",
                               "cloud_run_traffic", "job_image")
    assert de.APPLY_KINDS == ("schema", "tag_add", "traffic_pin", "env_set", "code", "job_image")
    assert de.HELPER_PHASES == ("BeforeAnyWrite", "Freeze", "BeforeCandidate", "BeforeSmoke", "AfterSmoke", "BeforePromotion",
                                "AfterAgentPromotion", "AfterPromotion", "BeforeRollback", "AfterRollback", "BeforeRetire", "AfterRetire",
                                "FreezeJobs", "BeforeJobsUpdate", "AfterJobsUpdate", "BeforeJobsRollback", "AfterJobsRollback")
    assert de.READBACK_KINDS == ("information_schema_columns", "row_count_since_marker", "helper_phase", "chain_manifest")
    assert de.CHANGES == ("create_table", "add_column", "create_view", "replace_view", "write_shape", "config_key", "env_set", "tag_add", "traffic_pin")
    assert de.APPLY_PHASES == ("before_candidate", "with_candidate", "after_promotion")


def test_js05_the_helper_phases_are_the_six_of_the_mode_jobs_readback_with_before_any_write_shared():
    assert set(de.HELPER_PHASES) >= {"BeforeAnyWrite", "FreezeJobs", "BeforeJobsUpdate", "AfterJobsUpdate", "BeforeJobsRollback", "AfterJobsRollback"}
    assert len(de.HELPER_PHASES) == 12 + 5  # BeforeAnyWrite is already a services-only phase and is not added twice


def test_js05_the_validator_accepts_b_durable_manifest_as_written(manifest):
    assert de.validate_manifest(manifest) == []


def test_js05_kind_code_for_the_image_effect_and_an_unknown_phase_are_refused(manifest):
    image = effect(manifest, "E-JOBS-IMAGE")
    assert image["kind"] == "job_image" and image["apply_kind"] == "job_image"
    image["kind"] = "code"
    assert any("kind 'code' is not allowed" in p for p in de.validate_manifest(seal(manifest)))
    other = copy.deepcopy(manifest)
    effect(other, "E-JOBS-IMAGE")["kind"] = "job_image"
    effect(other, "E-JOBS-IMAGE")["native_readback"]["target"] = "AfterSomething"
    assert any("names a phase the helper does not have" in p for p in de.validate_manifest(seal(other)))


def test_js05_an_unknown_readback_kind_and_a_job_image_apply_kind_on_a_schema_effect_are_refused(manifest):
    effect(manifest, "E-JOB-VIEWS")["native_readback"]["kind"] = "free_sql"
    assert any("native_readback kind" in p for p in de.validate_manifest(seal(manifest)))
    other = copy.deepcopy(manifest)
    effect(other, "E-JOB-VIEWS")["native_readback"]["kind"] = "chain_manifest"
    effect(other, "E-RUNS-STAMP")["apply_kind"] = "job_image"
    assert any("a schema effect applies through core.schema.apply" in p for p in de.validate_manifest(seal(other)))


class ColumnsBQ:
    """Answers each information_schema read with the pinned column list of the table the query names."""

    def __init__(self, drop=None):
        self.drop = drop

    def dry_run(self, sql, params=None):
        return 1

    def query(self, sql, params=None, max_bytes=None):
        dataset = re.search(r"\.(\w+)\.INFORMATION_SCHEMA", sql).group(1)
        table = re.search(r"table_name = '(\w+)'", sql).group(1)
        columns = COLUMNS[f"{dataset}.{table}"]
        if self.drop == f"{dataset}.{table}":
            columns = columns[1:]
        return [{"column_name": c} for c in columns]


def test_js05_readbacks_pass_when_every_table_shows_its_columns_and_the_deferred_effect_does_not_count(manifest):
    result = de.run_readbacks(manifest, ColumnsBQ())
    assert result["ok"] is True
    assert [r["effect_id"] for r in result["results"] if r.get("deferred")] == ["E-JOB-VIEWS"]
    assert all(r["matches"] is True for r in result["results"] if not r.get("deferred"))


def test_js05_one_table_missing_a_column_fails_the_readbacks_of_every_effect_on_that_table(manifest):
    result = de.run_readbacks(manifest, ColumnsBQ(drop="intelligence_42_agent.runs"))
    assert result["ok"] is False
    failed = sorted(r["effect_id"] for r in result["results"] if r["matches"] is False)
    assert failed == ["E-RUNS-DDL", "E-RUNS-STAMP"]


def test_js05_the_chain_manifest_readback_is_deferred_and_the_other_effects_are_read_back(manifest):
    class Recorder:
        def __init__(self):
            self.calls = []

        def dry_run(self, sql, params=None):
            self.calls.append(("dry_run", sql))
            return 1

        def query(self, sql, params=None, max_bytes=None):
            self.calls.append(("query", sql))
            return []

    bq = Recorder()
    result = de.run_readbacks(manifest, bq)
    by_id = {r["effect_id"]: r for r in result["results"]}
    assert by_id["E-JOB-VIEWS"].get("deferred") is True and by_id["E-JOB-VIEWS"]["matches"] is None
    assert by_id["E-JOBS-IMAGE"]["helper_phase"] == "AfterJobsUpdate"
    assert len([c for c in bq.calls if c[0] == "query"]) == 14  # one information_schema read per schema effect, none for the job effects
    assert all("INFORMATION_SCHEMA.COLUMNS" in c[1] for c in bq.calls)


# JS-01: set equality of the new statements and the schema effects

def head_texts(tree):
    return [dict(tree)[f"core/schema/{name}"] for name in js.sql_file_names(tree)]


def a80_texts(a80):
    return [dict(a80)["core/schema/agent.sql"], dict(a80)["core/schema/core.sql"]]


def test_js01_there_are_fourteen_new_statements_and_fourteen_schema_effects_equal_to_them_by_hash(trees, manifest):
    head, a80 = trees
    new = de.new_statements(head_texts(head), a80_texts(a80))
    assert len(new) == 14 and len(schema_effects(manifest)) == 14
    # independent derivation: apply.py's own loader for the head, and the same splitter over the a80 archive
    old = {sha(s) for text in a80_texts(a80) for s in apply.split_statements(text)}
    fresh = {sha(s) for s in apply.load_statements() if sha(s) not in old}
    assert fresh == {e["statement_sha256"] for e in schema_effects(manifest)} == {sha(s) for s in new}
    assert de.check_schema_effects(manifest, head_texts(head), a80_texts(a80)) == []


def test_js01_every_effect_text_hashes_to_its_sha_and_is_a_form_apply_recognises(manifest):
    for e in schema_effects(manifest):
        assert sha(e["statement_ref"]) == e["statement_sha256"] and apply.kind(e["statement_ref"]) in ("table", "alter")


def test_js01_the_effect_ids_are_the_fourteen_of_the_contract_in_apply_order(manifest):
    assert [e["effect_id"] for e in sorted(schema_effects(manifest), key=lambda e: e["apply_order"])] == [i for i, _ in SCHEMA_IDS]
    assert [e["effect_id"] for e in manifest["effects"] if e["kind"] != "schema"] == ["E-JOBS-IMAGE", "E-JOB-VIEWS"]
    assert "E-ITEMSTATE" not in {e["effect_id"] for e in manifest["effects"]}


def test_js01_a_dropped_effect_and_an_added_item_state_statement_are_both_found(trees, manifest):
    head, a80 = trees
    manifest["effects"] = [e for e in manifest["effects"] if e["effect_id"] != "E-RUNS-STAMP"]
    assert any("not in the manifest" in p and "agent.runs" in p for p in de.check_schema_effects(seal(manifest), head_texts(head), a80_texts(a80)))
    widened = head_texts(head) + ["ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.item_state`\nADD COLUMN IF NOT EXISTS eligible_v1 BOOL;"]
    found = de.check_schema_effects(copy.deepcopy(manifest), widened, a80_texts(a80))
    assert any("item_state" in p for p in found)


def test_js01_the_builder_refuses_a_tree_with_an_item_state_alter_or_a_missing_early_signal(trees):
    head, a80 = trees
    broken = [(p, t + "\nALTER TABLE `ogilvy-trends-v2.intelligence_42_core.item_state`\nADD COLUMN IF NOT EXISTS eligible_v1 BOOL;\n"
               if p == "core/schema/core.sql" else t) for p, t in head]
    with pytest.raises(ValueError, match="item_state ALTER"):
        je.build_jobs_manifest(broken, a80, release_id=RID, commit=COMMIT, tree=TREE, owner=OWNER)


def test_js01_a_statement_the_builder_does_not_know_is_refused_not_dropped(trees):
    head, a80 = trees
    extra = [(p, t + "\nCREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.intelligence_42_core.surprise` (x STRING);\n" if p == "core/schema/core.sql" else t)
             for p, t in head]
    with pytest.raises(ValueError, match="surprise"):
        je.build_jobs_manifest(extra, a80, release_id=RID, commit=COMMIT, tree=TREE, owner=OWNER)


# JS-02: the jobs-owned DDL

def test_js02_the_derived_set_holds_the_seven_objects_the_contract_names(trees):
    head, _ = trees
    owned = set(de.derive_ddl_writers(head))
    assert {"tvf_post_items", "v_item_locality_checked", "v_item_locality_current", "v_collection_health_current",
            "v_item_market_scope", "v_forecasts_current", "v_breaking_signals_current"} <= owned


def test_js02_the_changed_objects_between_a80_and_this_tree_are_the_five_found_by_reading_the_diff(trees):
    head, a80 = trees
    assert je.changed_ddl_objects(a80, head) == CHANGED_DDL


def test_js02_e_job_views_lists_every_changed_object_with_the_hash_of_its_statement_text(manifest):
    view = effect(manifest, "E-JOB-VIEWS")
    assert view["kind"] == "view" and view["change"] == "replace_view" and view["apply_kind"] == "code" and view["apply_phase"] == "with_candidate"
    assert view["target"] == ",".join(DDL_APPLY_ORDER)
    lines = view["statement_ref"].splitlines()
    assert [ln.split(" ")[0] for ln in lines] == DDL_APPLY_ORDER and sorted(DDL_APPLY_ORDER) == CHANGED_DDL
    assert all(len(ln.split(" ")[1]) == 64 for ln in lines)
    assert sha(view["statement_ref"]) == view["statement_sha256"]
    assert view["native_readback"] == {"kind": "chain_manifest", "target": "first_b_chain", "expected_result_sha256": ""}


def test_js02_the_changed_set_is_by_statement_text_a_new_a_changed_and_an_unchanged_object_and_a_layout_only_change():
    a80 = [("core/detect/sql/v.sql", "CREATE OR REPLACE VIEW {core}.v_same AS SELECT 1;\nCREATE OR REPLACE VIEW {core}.v_edit AS SELECT 2;\n"
            "CREATE OR REPLACE VIEW {core}.v_layout AS SELECT a,\n  b FROM t;\nCREATE OR REPLACE VIEW {core}.v_note AS SELECT 5;\n"),
           ("core/detect/tests/test_x.py", "CREATE OR REPLACE VIEW x.v_in_test AS SELECT 1;")]
    head = [("core/detect/sql/v.sql", "CREATE OR REPLACE VIEW {core}.v_same AS SELECT 1;\nCREATE OR REPLACE VIEW {core}.v_edit AS SELECT 3;\n"
             "CREATE OR REPLACE VIEW {core}.v_layout AS SELECT a, b\n   FROM t;\nCREATE OR REPLACE VIEW {core}.v_new AS SELECT 4;\n"
             "CREATE OR REPLACE VIEW {core}.v_note AS -- tidied\nSELECT 5;\n"),
            ("core/detect/tests/test_x.py", "CREATE OR REPLACE VIEW x.v_in_test AS SELECT 99;")]
    assert je.changed_ddl_objects(a80, head) == ["v_edit", "v_new"]


# JS-04: readbacks and order

def test_js04_every_schema_effect_reads_back_the_columns_of_its_table_and_expects_the_pinned_list(manifest):
    for effect_id, table in SCHEMA_IDS:
        e = effect(manifest, effect_id)
        assert e["native_readback"] == {"kind": "information_schema_columns", "target": table,
                                        "expected_result_sha256": de.sha_text(de.canonical([{"column_name": c} for c in COLUMNS[table]]))}, effect_id
        sql, _ = de.readback_sql(e["native_readback"])
        assert f"`ogilvy-trends-v2.{table.split('.')[0]}.INFORMATION_SCHEMA.COLUMNS`" in sql and f"table_name = '{table.split('.')[1]}'" in sql


def test_js04_the_pinned_column_lists_are_what_the_schema_files_declare_by_an_independent_parse():
    cols = {}
    for s in apply.load_statements():
        if apply.kind(s) == "table":
            dataset, table, columns, _ = parse_create(s)
            cols[f"{dataset}.{table}"] = {c[0] for c in columns}
    for s in apply.load_statements():
        if apply.kind(s) == "alter":
            name = s.split("`")[1].split(".")
            added = s.split("ADD COLUMN IF NOT EXISTS")[1].split()[0].strip("`")
            cols[f"{name[1]}.{name[2]}"].add(added)
    for table, expected in COLUMNS.items():
        assert sorted(cols[table]) == expected, table


def test_js04_the_expected_columns_are_the_create_columns_plus_every_added_column_even_one_the_create_text_omits():
    create = "CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2.d.t` (\n  a STRING NOT NULL, b ARRAY<STRUCT<x INT64, y STRING>>, c JSON)\nPARTITION BY a"
    alter = "ALTER TABLE `ogilvy-trends-v2.d.t`\nADD COLUMN IF NOT EXISTS late BOOL"
    other = "ALTER TABLE `ogilvy-trends-v2.d.u`\nADD COLUMN IF NOT EXISTS stray BOOL"
    assert je.final_columns([create, alter, other], "d.t") == ["a", "b", "c", "late"]


def test_js02_the_hash_listed_for_an_object_is_the_hash_of_its_statement_with_comments_dropped_and_whitespace_collapsed(manifest):
    text = (ROOT / "core/detect/sql/locality_views.sql").read_text(encoding="utf-8")
    [statement] = [s for s in apply.split_statements(text) if re.match(r"CREATE OR REPLACE VIEW \{core\}\.v_item_locality_current\b", s)]
    expected = sha(" ".join(statement.split()))
    listed = dict(line.split(" ") for line in effect(manifest, "E-JOB-VIEWS")["statement_ref"].splitlines())
    assert listed["v_item_locality_current"] == expected


def test_js04_the_apply_phase_is_before_the_candidate_for_every_schema_effect_and_with_the_candidate_for_the_job_effects(manifest):
    assert {e["apply_phase"] for e in schema_effects(manifest)} == {"before_candidate"}
    assert effect(manifest, "E-JOBS-IMAGE")["apply_phase"] == "with_candidate" and effect(manifest, "E-JOB-VIEWS")["apply_phase"] == "with_candidate"
    assert de.APPLY_PHASES.index("before_candidate") < de.APPLY_PHASES.index("with_candidate")
    assert max(e["apply_order"] for e in schema_effects(manifest)) < effect(manifest, "E-JOBS-IMAGE")["apply_order"]


def test_js04_the_job_image_effect_is_read_back_by_the_after_jobs_update_phase_and_names_the_fourteen_jobs(manifest):
    image = effect(manifest, "E-JOBS-IMAGE")
    assert image["native_readback"] == {"kind": "helper_phase", "target": "AfterJobsUpdate", "expected_result_sha256": ""}
    assert image["target"].split(",") == sorted(so.JOB_NAMES) and len(so.JOB_NAMES) == 14
    assert image["change"] == "write_shape" and image["destructive"] is False


# JS-06: rollback blockers

class RecordingBQ:
    def __init__(self):
        self.calls = []

    def dry_run(self, sql, params=None):
        self.calls.append("dry_run")
        return 10

    def query(self, sql, params=None, max_bytes=None):
        self.calls.append("query")
        return [{"n": 0}]


def test_js06_with_no_blockers_the_decision_is_offline_blocked_false_and_no_query_is_made(manifest):
    assert manifest["rollback_blockers"] == [] and all(e["authority_or_holds"] is False for e in manifest["effects"])
    bq = RecordingBQ()
    assert de.run_rollback_blockers(manifest, bq) == {"schema_version": 1, "check": "rollback-blockers", "blocked": False, "blockers": 0}
    assert bq.calls == []


def blocker(query):
    return {"condition_query_ref": query, "condition_query_sha256": sha(query), "bytes_cap": 1000, "blocks_when": "count_gt_0"}


def test_js06_a_fixture_with_a_blocker_checks_the_hash_then_scans_the_text_before_the_first_dry_run(manifest):
    good = blocker("SELECT COUNT(*) AS n FROM `ogilvy-trends-v2.intelligence_42_agent.holds`")
    manifest["rollback_blockers"] = [good]
    bq = RecordingBQ()
    result = de.run_rollback_blockers(manifest, bq)
    assert result["blocked"] is False and bq.calls == ["dry_run", "query"]
    for bad, fragment in ((dict(good, condition_query_sha256="0" * 64), "hash"), (blocker("DELETE FROM `p.d.t` WHERE TRUE"), "SELECT"),
                          (blocker("SELECT 1; SELECT 2"), "more than one"), (blocker("SELECT 1 FROM t WHERE x IN (SELECT 1) OR 'DROP' = 'DROP'"), "DROP")):
        manifest["rollback_blockers"] = [bad]
        bq = RecordingBQ()
        with pytest.raises(de.Refusal, match=fragment):
            de.run_rollback_blockers(manifest, bq)
        assert bq.calls == [], "a refused blocker made a BigQuery call"


# JS-11: apply_order is the order apply would run

def test_js11_apply_order_numbers_follow_the_order_apply_load_statements_and_run_would_execute(manifest):
    stmts = apply.load_statements()
    ran = ([s for s in stmts if apply.kind(s) == "schema"] + [s for s in stmts if apply.kind(s) == "table"]
           + [s for s in stmts if apply.kind(s) in ("alter", "view")])
    wanted = {sha(e["statement_ref"]) for e in schema_effects(manifest)}
    order = [sha(s) for s in ran if sha(s) in wanted]
    assert len(order) == 14
    by_hash = {e["statement_sha256"]: e["apply_order"] for e in schema_effects(manifest)}
    assert [by_hash[h] for h in order] == list(range(1, 15))
    assert [e["apply_order"] for e in manifest["effects"] if e["kind"] != "schema"] == [15, 16]


def test_js11_the_three_changed_create_texts_run_in_the_table_pass_before_every_alter(manifest):
    orders = {e["effect_id"]: e["apply_order"] for e in manifest["effects"]}
    for create in ("E-RUNS-DDL", "E-CLAIM-DDL", "E-POSTITEMS-DDL", "E-EARLY"):
        assert all(orders[create] < orders[alter] for alter in ("E-RUNS-STAMP", "E-CLAIM-SPAN", "E-CLAIM-CODE", "E-POSTITEMS-LINKED", "E-POSTITEMS-MARKET"))


# RB-C4: B applies runs.stamp and the claim_checks columns before any B job writes them

def test_rbc4_the_stamp_and_the_two_claim_checks_columns_are_b_own_before_candidate_schema_effects(manifest):
    for effect_id, column, table in (("E-RUNS-STAMP", "stamp JSON", "runs"), ("E-CLAIM-SPAN", "span_sha256 STRING", "claim_checks"),
                                     ("E-CLAIM-CODE", "reason_code STRING", "claim_checks")):
        e = effect(manifest, effect_id)
        assert e["kind"] == "schema" and e["change"] == "add_column" and e["apply_kind"] == "schema" and e["apply_phase"] == "before_candidate"
        assert f"intelligence_42_agent.{table}`" in e["statement_ref"] and f"ADD COLUMN IF NOT EXISTS {column}" in " ".join(e["statement_ref"].split())
        assert e["additive"] is True and e["destructive"] is False
        assert e["apply_order"] < effect(manifest, "E-JOBS-IMAGE")["apply_order"]


def test_rbc4_no_effect_says_release_a_already_applied_them(manifest):
    for e in manifest["effects"]:
        blob = " ".join(str(v) for v in (e["forward_recovery"].get("note", ""), e["evidence"], e["statement_ref"])).lower()
        assert "already applied" not in blob, e["effect_id"]
        if e["effect_id"] in ("E-RUNS-STAMP", "E-CLAIM-SPAN", "E-CLAIM-CODE"):
            assert "no-op" not in blob, e["effect_id"]
    note = effect(manifest, "E-RUNS-STAMP")["forward_recovery"]["note"]
    assert "first reaches staging in B" in note


def test_rbc4_a_row_stamped_by_chain_needs_the_column_so_the_stamp_effect_precedes_every_job_effect(manifest):
    stamp_order = effect(manifest, "E-RUNS-STAMP")["apply_order"]
    assert all(stamp_order < e["apply_order"] for e in manifest["effects"] if e["kind"] in ("job_image", "view"))


def test_the_manifest_binds_its_release_and_source_and_seals_its_hash(manifest):
    assert manifest["release_id"] == RID and manifest["source"] == {"commit": COMMIT, "tree": TREE}
    assert manifest["generated_from"] == {"base": A80, "head": COMMIT}
    assert manifest["no_storage_change"] == {"claimed": False} and manifest["rollback_blockers"] == []
    assert manifest["manifest_sha256"] == de.manifest_hash(manifest)


# RB-C9: the core.sql tables, then views.sql, then locality_views.sql, before the brief's first run

def test_rbc9_the_views_effect_follows_every_core_table_effect_and_lists_views_sql_then_locality_views_sql(manifest):
    views = effect(manifest, "E-JOB-VIEWS")
    core_tables = [e for e in schema_effects(manifest) if e["target"].startswith("intelligence_42_core.")]
    assert core_tables and all(e["apply_order"] < views["apply_order"] for e in core_tables)
    assert all(e["apply_phase"] == "before_candidate" for e in core_tables) and views["apply_phase"] == "with_candidate"
    listed = [ln.split(" ")[0] for ln in views["statement_ref"].splitlines()]
    files = {"v_collection_health_current": 0, "v_item_market_scope": 0, "tvf_post_items": 1, "v_item_locality_checked": 1,
             "v_item_locality_current": 1}      # 0 views.sql, 1 locality_views.sql, read from the two files below
    assert [files[n] for n in listed] == sorted(files[n] for n in listed)
    views_sql = (ROOT / "core/detect/sql/views.sql").read_text(encoding="utf-8")
    locality_sql = (ROOT / "core/detect/sql/locality_views.sql").read_text(encoding="utf-8")
    for name, which in files.items():
        assert (name in (views_sql, locality_sql)[which]) and (name not in (views_sql, locality_sql)[1 - which] or which == 0)


def test_rbc9_detect_creates_views_sql_before_locality_views_and_the_chain_runs_detect_before_brief():
    import ast

    from core.collect import chain

    tree = ast.parse((ROOT / "core/detect/job.py").read_text(encoding="utf-8"))
    [run] = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run"]
    calls = [(n.lineno, ast.unparse(n.func)) for n in ast.walk(run) if isinstance(n, ast.Call)]
    first = min(line for line, name in calls if name.endswith("sqlrun.apply_views"))
    second = min(line for line, name in calls if name.endswith("apply_locality_views_step"))
    assert first < second
    assert chain.STAGES.index("detect") < chain.STAGES.index("brief")
