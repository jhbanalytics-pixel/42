"""B's durable effects manifest (W8-REL-B v2.1 section 3.3, JS-01, JS-02, JS-04, JS-05, JS-11; carry item RB-C4).

    build_jobs_manifest(head_files, a80_files, release_id=..., commit=..., tree=..., owner=...) -> the sealed manifest
    changed_ddl_objects(a80_files, head_files) -> the jobs-owned DDL objects whose statement text changed

The schema effects are derived from the statements of the head tree's SQL_FILES that a80's did not carry (whole-statement hash,
as durable_effects_check.new_statements computes it), in the order apply.run executes them: every table in file order, then
the ALTERs. Each statement must be one this module knows by table and column, so a statement it does not know is refused and
never dropped. Release A applies no schema (integration/RELEASE-A-SCHEMA.md), so runs.stamp and the claim_checks columns first
reach staging here, as before_candidate effects ordered before the job image effect.

The expected readback column lists are computed from the head tree's CREATE and ALTER statements; the tests pin them as literals.
"""
from __future__ import annotations

import re

from core.setup import durable_effects_check as de
from core.setup.release import jobs_source as js
from core.setup.release import services_only as so

A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
CORE = "intelligence_42_core"
AGENT = "intelligence_42_agent"

TABLE_EFFECTS = {
    f"{AGENT}.runs": "E-RUNS-DDL", f"{AGENT}.claim_checks": "E-CLAIM-DDL", f"{CORE}.post_items": "E-POSTITEMS-DDL",
    f"{CORE}.early_signal": "E-EARLY", f"{CORE}.item_locality": "E-LOC-TABLES-1", f"{CORE}.item_locality_post": "E-LOC-TABLES-2",
    f"{CORE}.item_locality_verified": "E-LOC-TABLES-3", f"{CORE}.post_item_lineage": "E-LOC-TABLES-4",
    f"{CORE}.post_item_end": "E-LOC-TABLES-5",
}
COLUMN_EFFECTS = {
    (f"{AGENT}.runs", "stamp"): "E-RUNS-STAMP", (f"{AGENT}.claim_checks", "span_sha256"): "E-CLAIM-SPAN",
    (f"{AGENT}.claim_checks", "reason_code"): "E-CLAIM-CODE", (f"{CORE}.post_items", "linked_on"): "E-POSTITEMS-LINKED",
    (f"{CORE}.post_items", "link_market"): "E-POSTITEMS-MARKET",
}
# The file in the jobs image that writes (or, for the two tables nothing writes yet, reads) each table.
CONSUMERS = {
    f"{AGENT}.runs": ("core/collect/chain.py", "write"), f"{AGENT}.claim_checks": ("core/brief/job.py", "write"),
    f"{CORE}.post_items": ("core/detect/aggregate.py", "write"), f"{CORE}.early_signal": ("core/detect/early_signal.py", "write"),
    f"{CORE}.item_locality": ("core/detect/locality.py", "write"), f"{CORE}.item_locality_post": ("core/detect/locality.py", "write"),
    f"{CORE}.item_locality_verified": ("core/detect/locality.py", "write"),
    f"{CORE}.post_item_lineage": ("core/detect/sql/locality_views.sql", "read"), f"{CORE}.post_item_end": ("core/detect/sql/locality_views.sql", "read"),
}
COMPAT_TESTS = {"a80_readers": "JX-08", "a80_writers": "JS-03", "a80_ddl_writers": "JS-02", "candidate_readers_old_records": "JX-06",
                "candidate_writers_old_readers": "JX-08"}
NAME = re.compile(r"`ogilvy-trends-v2\.(\w+)\.(\w+)`")
ADD_COLUMN = re.compile(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+`?(\w+)`?", re.I)


def split_top(text):
    depth, current, out = 0, "", []
    for ch in text:
        depth += ch in "(<"
        depth -= ch in ")>"
        if ch == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += ch
    out.append(current)
    return [p.strip() for p in out if p.strip()]


def create_columns(statement):
    start = statement.index("(")
    depth, i = 1, start + 1
    while depth:
        depth += {"(": 1, ")": -1}.get(statement[i], 0)
        i += 1
    return [piece.split()[0].strip("`") for piece in split_top(statement[start + 1:i - 1])]


def final_columns(statements, table):
    """The column names of table once every CREATE and ADD COLUMN in statements has run, sorted."""
    columns = set()
    for statement in statements:
        m = NAME.search(statement)
        if not m or f"{m.group(1)}.{m.group(2)}" != table:
            continue
        kind = de.statement_kind(statement)
        if kind == "table":
            columns |= set(create_columns(statement))
        elif kind == "alter":
            columns.add(ADD_COLUMN.search(statement).group(1))
    return sorted(columns)


def classify(statement):
    """(effect id, change, table, kind) for a new schema statement, or ValueError for one this builder does not know."""
    m = NAME.search(statement)
    kind = de.statement_kind(statement)
    if m and kind == "table" and f"{m.group(1)}.{m.group(2)}" in TABLE_EFFECTS:
        table = f"{m.group(1)}.{m.group(2)}"
        return TABLE_EFFECTS[table], "create_table", table
    if m and kind == "alter":
        added = ADD_COLUMN.search(statement)
        table = f"{m.group(1)}.{m.group(2)}"
        if added and (table, added.group(1)) in COLUMN_EFFECTS:
            return COLUMN_EFFECTS[(table, added.group(1))], "add_column", table
    raise ValueError(f"a new schema statement the manifest has no effect for: {' '.join(statement.split())[:110]}")


# the jobs-owned DDL

# The order the detect job creates its views: views.sql first, then locality_views.sql, which reads v_good_runs from the first
# (core/detect/job.py run: apply_views, then apply_locality_views_step). Objects in other files follow, in path order.
DETECT_VIEW_FILES = ("core/detect/sql/views.sql", "core/detect/sql/locality_views.sql")

def ddl_statements(files):
    """{object name: the CREATE OR REPLACE statement text, whitespace collapsed} for the non-test files under core/ outside
    core/schema. The text runs from the CREATE to the first semicolon or closing triple quote."""
    out = {}
    for path, text in files:
        if de.is_test_path(path) or path.startswith("core/schema/") or not path.endswith((".py", ".sql")):
            continue
        for m in de.DDL_REPLACE.finditer(text):
            name = m.group(1).rstrip(";").rsplit(".", 1)[-1].strip("`{}")
            ends = [i for i in (text.find(";", m.start()), text.find('"""', m.start()), text.find("'''", m.start())) if i != -1]
            body = re.sub(r"--[^\n]*", "", text[m.start():min(ends) if ends else len(text)])
            out.setdefault(name, []).append(" ".join(body.split()))
    return {name: "\n".join(sorted(bodies)) for name, bodies in out.items()}


def ddl_apply_order(files, names):
    """names in the order detect creates them: by file (DETECT_VIEW_FILES first), then by position in the file."""
    place = {}
    for path, text in files:
        if de.is_test_path(path) or path.startswith("core/schema/") or not path.endswith((".py", ".sql")):
            continue
        rank = DETECT_VIEW_FILES.index(path) if path in DETECT_VIEW_FILES else len(DETECT_VIEW_FILES)
        for m in de.DDL_REPLACE.finditer(text):
            name = m.group(1).rstrip(";").rsplit(".", 1)[-1].strip("`{}")
            place.setdefault(name, (rank, path, m.start()))
    return sorted(names, key=lambda n: place[n])


def changed_ddl_objects(a80_files, head_files):
    old, new = ddl_statements(a80_files), ddl_statements(head_files)
    return sorted(name for name, text in new.items() if old.get(name) != text)


# the manifest

def consumer(file, access, head_files, table=None):
    text = js.text_of(head_files, file)
    if text is None or (table and table.split(".")[1] not in text):
        raise ValueError(f"{file} is not in the tree or does not name {table}")
    return {"component": "jobs image", "image": "jobs", "file": file, "line": 0, "access": access}


def compat():
    return {key: {"verdict": "pending", "test": test} for key, test in COMPAT_TESTS.items()}


def base_effect(effect_id, kind, target, change, order, phase, owner, statement, apply_kind, consumers, readback, note):
    return {"effect_id": effect_id, "kind": kind, "target": target, "change": change, "apply_order": order, "apply_phase": phase,
            "owner": dict(owner), "statement_ref": statement, "statement_sha256": de.sha_text(statement), "apply_kind": apply_kind,
            "additive": True, "destructive": False, "consumers": consumers, "native_readback": readback, "compat": compat(),
            "forward_recovery": {"kind": "none_needed", "statement_ref": "", "statement_sha256": "", "note": note},
            "authority_or_holds": False, "evidence": {}}


def build_jobs_manifest(head_files, a80_files, *, release_id, commit, tree, owner):
    problems = js.schema_problems(head_files, a80_files)
    if problems:
        raise ValueError("the head tree fails the schema preconditions: " + "; ".join(problems))
    head_statements = js.schema_statements(head_files)
    old = {de.sha_text(s) for s in js.schema_statements(a80_files)}
    new = {de.sha_text(s): s for s in head_statements if de.sha_text(s) not in old}
    effects, order = [], 0
    for statement in js.execution_order(head_statements):
        if de.sha_text(statement) not in new:
            continue
        effect_id, change, table = classify(statement)
        order += 1
        file, access = CONSUMERS[table]
        columns = final_columns(head_statements, table)
        readback = {"kind": "information_schema_columns", "target": table,
                    "expected_result_sha256": de.sha_text(de.canonical([{"column_name": c} for c in columns]))}
        if change == "add_column":
            note = "additive column; it first reaches staging in B, because Release A applies no schema, and is applied before any B job writes it"
        else:
            note = "additive; a CREATE ... IF NOT EXISTS leaves an existing table as it is"
        effects.append(base_effect(effect_id, "schema", table, change, order, "before_candidate", owner, statement, "schema",
                                   [consumer(file, access, head_files, table)], readback, note))
    if len(effects) != len(new) or len({e["effect_id"] for e in effects}) != len(effects):
        raise ValueError("the schema effects are not one for one with the new statements")
    changed = ddl_apply_order(head_files, changed_ddl_objects(a80_files, head_files))
    listing = "\n".join(f"{name} {de.sha_text(ddl_statements(head_files)[name])}" for name in changed)
    writers = de.derive_ddl_writers(head_files)
    view_files = sorted({path for name in changed for path in writers.get(name, [])})
    effects.append(base_effect("E-JOBS-IMAGE", "job_image", ",".join(sorted(so.JOB_NAMES)), "write_shape", order + 1, "with_candidate", owner,
                               "the 14 jobs move to the frozen jobs image digest through jobs update --image and no other flag",
                               "job_image", [{"component": "jobs image", "image": "jobs", "file": "core/setup/deploy_jobs.py", "line": 0, "access": "write"}],
                               {"kind": "helper_phase", "target": "AfterJobsUpdate", "expected_result_sha256": ""},
                               "JobsRollback restores the bound rollback digest; the objects B created stay behind"))
    effects.append(base_effect("E-JOB-VIEWS", "view", ",".join(changed), "replace_view", order + 2, "with_candidate", owner, listing, "code",
                               [{"component": "jobs image", "image": "jobs", "file": f, "line": 0, "access": "write"} for f in view_files],
                               {"kind": "chain_manifest", "target": "first_b_chain", "expected_result_sha256": ""},
                               "applied by the detect job on its first run, views.sql objects then locality_views.sql objects and before the brief's first run, "
                               "after the core.sql tables; read back from the first B chain manifest, a deferred readback"))
    manifest = {"schema_version": de.SCHEMA_VERSION, "release_id": release_id, "source": {"commit": commit, "tree": tree}, "effects": effects,
                "no_storage_change": {"claimed": False}, "rollback_blockers": [], "a80_ddl_writers": sorted(de.derive_ddl_writers(a80_files)),
                "generated_from": {"base": A80, "head": commit}, "reviewed_by": ""}
    manifest["manifest_sha256"] = de.manifest_hash(manifest)
    return manifest
