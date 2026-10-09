"""Source commit preconditions of Release B (W8-REL-B v2.1 section 3.1, JS-01 to JS-03, JS-08 to JS-10).

Each check takes a tree, a list of (path, text) for core/ as durable_effects_check.git_tree_files returns it, so the same check
runs over the working tree, a git archive of the release commit, or the a80 archive. Nothing here reads a cloud.

    sql_file_names(files)          the schema files apply.py runs at that tree, read from its apply.py
    schema_problems(head, a80)     item_state ALTER or widened CREATE, the SQL_FILES membership rules (JS-01)
    writer_problems(a80, head)     positional INSERT and SELECT * readers of every table the head tree alters (JS-03, JS-08)
    parse_json_fix_problems(rev)   the PARSE_JSON fix pinned by literal git blob ids (JS-10)
    dry_run_receipt_problems(...)  the logged apply.py dry run reports no view drift (JS-09)

The expected values are literals of this module or come from the tree under test only where the tree is what is being checked
(the SQL_FILES of its own apply.py). A hash or blob id is compared with a recomputation or with a literal, never with a value
read from the thing it vouches for.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from core.setup import durable_effects_check as de
from core.setup.release import services_only as so
from core.setup.release.services_only import Stop

SCHEMA_DIR = "core/schema/"
NOT_SQL_FILES = ("card_outcome.sql", "locality_switch.sql")
PARSE_JSON_FIX_BLOB = "1322f3800e4d6ac0bcce82063be3b73538e5d018"
A80_CLUSTER_CHECKPOINT_BLOB = "70683db46c4bd732a2e1ca2c95dc228aadae940e"
CLUSTER_CHECKPOINT = "core/understand/sql/cluster_checkpoint.sql"
SQL_FILES_NAME = re.compile(r'HERE\s*/\s*"([\w.]+\.sql)"')
SQL_FILES_ASSIGNMENT = re.compile(r"^SQL_FILES\s*=\s*\((.*?)\)\s*$", re.M | re.S)
ALTER_TABLE = re.compile(r"^ALTER\s+TABLE\s+`([^`]+)`", re.I)
CREATE_TABLE = re.compile(r"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`([^`]+)`", re.I)
CREATE_VIEW = re.compile(r"^CREATE\s+VIEW\s+IF\s+NOT\s+EXISTS\s+`([^`]+)`", re.I)


def tree_from_disk(root):
    """(path, text) of every text file under root/core, relative to root, the form git_tree_files returns."""
    root = Path(root)
    files = []
    for path in sorted((root / "core").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            files.append((path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return files


def text_of(files, path):
    for name, text in files:
        if name == path:
            return text
    return None


def sql_file_names(files):
    """The schema files apply.py runs, in its order, read from core/schema/apply.py of this tree."""
    text = text_of(files, SCHEMA_DIR + "apply.py")
    if text is None:
        return ()
    found = SQL_FILES_ASSIGNMENT.search(text)
    return tuple(SQL_FILES_NAME.findall(found.group(1))) if found else ()


def schema_statements(files):
    """The statements of the SQL_FILES in file order, each as apply.py splits them."""
    out = []
    for name in sql_file_names(files):
        text = text_of(files, SCHEMA_DIR + name)
        if text is not None:
            out += de.split_statements(text)
    return out


def execution_order(statements):
    """The order apply.run executes them: datasets, then every table, then ALTERs and views in file order."""
    kinds = [de.statement_kind(s) for s in statements]
    return ([s for s, k in zip(statements, kinds) if k == "schema"] + [s for s, k in zip(statements, kinds) if k == "table"]
            + [s for s, k in zip(statements, kinds) if k in ("alter", "view")])


def item_state_create(files, names=None):
    out = []
    for name in names if names is not None else sql_file_names(files):
        for statement in de.split_statements(text_of(files, SCHEMA_DIR + name) or ""):
            m = CREATE_TABLE.match(statement)
            if m and m.group(1).endswith(".item_state"):
                out.append(statement)
    return out


def schema_problems(head_files, a80_files):
    problems = []
    names = sql_file_names(head_files)
    if "early_signal.sql" not in names:
        problems.append("early_signal.sql is not in SQL_FILES")
    for name in NOT_SQL_FILES:
        if name in names:
            problems.append(f"{name} is in SQL_FILES")
    for name in names:
        for statement in de.split_statements(text_of(head_files, SCHEMA_DIR + name) or ""):
            m = ALTER_TABLE.match(statement)
            if m and m.group(1).endswith(".item_state"):
                problems.append(f"item_state ALTER in {SCHEMA_DIR}{name}: {' '.join(statement.split())[:100]}")
    head_create, a80_create = item_state_create(head_files), item_state_create(a80_files, sql_file_names(a80_files) or ("agent.sql", "core.sql"))
    if len(head_create) != 1 or len(a80_create) != 1:
        problems.append("item_state CREATE statement not found exactly once")
    elif de.sha_text(head_create[0]) != de.sha_text(a80_create[0]):
        problems.append("item_state CREATE text differs from a80's")
    return problems


def altered_tables(head_files, a80_files):
    """The short names of the tables the ALTER TABLE statements of the head SQL_FILES change that a80's did not carry, by whole
    statement hash, sorted. An ALTER a80 already had is a no-op IF NOT EXISTS and changes nothing now."""
    old = {de.sha_text(s) for s in schema_statements(a80_files)}
    tables = set()
    for statement in schema_statements(head_files):
        m = ALTER_TABLE.match(statement)
        if m and de.sha_text(statement) not in old:
            tables.add(m.group(1).rsplit(".", 1)[-1])
    return sorted(tables)


def in_code(text, position):
    """False when the line holding position has a comment marker before it (SQL --, Python #)."""
    start = text.rfind("\n", 0, position) + 1
    prefix = text[start:position]
    return "--" not in prefix and "#" not in prefix


def line_of(text, position):
    return text.count("\n", 0, position) + 1


def scannable(path):
    return path.endswith((".py", ".sql")) and not de.is_test_path(path)


def star_reader(table):
    return re.compile(r"SELECT\s+(?:\w+\.)?\*\s+FROM\s+`?(?:[\w{}-]+\.)*" + re.escape(table) + r"\b", re.I)


def writer_problems(a80_files, head_files):
    """For every table the head tree alters: a positional INSERT or a SELECT * reader in the a80 archive. 'path:line: what'."""
    problems = set()
    for table in altered_tables(head_files, a80_files):
        pattern = star_reader(table)
        for path, text in a80_files:
            if not scannable(path):
                continue
            for m in de.INSERT_NO_COLUMNS.finditer(text):
                if m.group(1).rsplit(".", 1)[-1] == table and in_code(text, m.start()):
                    problems.add((path, line_of(text, m.start()), f"positional INSERT into {table}"))
            for m in pattern.finditer(text):
                if in_code(text, m.start()):
                    problems.add((path, line_of(text, m.start()), f"SELECT * reader of {table}"))
    return [f"{path}:{line}: {what}" for path, line, what in sorted(problems)]


def scan_summary(a80_files, head_files):
    return {"tables": altered_tables(head_files, a80_files), "problems": writer_problems(a80_files, head_files)}


# JS-10

def git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", timeout=120)
    return proc.returncode, proc.stdout.strip()


def parse_json_fix_problems(rev, cwd):
    problems = []
    code, blob = git(["rev-parse", f"{rev}:{CLUSTER_CHECKPOINT}"], cwd)
    if code != 0 or blob != PARSE_JSON_FIX_BLOB:
        problems.append(f"{CLUSTER_CHECKPOINT} at {rev[:12]} is not the PARSE_JSON fix blob {PARSE_JSON_FIX_BLOB}")
    code, listing = git(["ls-tree", "-r", rev], cwd)
    if code != 0:
        problems.append(f"the tree of {rev[:12]} could not be listed")
    elif A80_CLUSTER_CHECKPOINT_BLOB in {line.split()[2] for line in listing.splitlines() if len(line.split()) >= 3}:
        problems.append(f"the a80 cluster_checkpoint blob {A80_CLUSTER_CHECKPOINT_BLOB} is in the tree of {rev[:12]}")
    return problems


# JS-09

def view_names(files):
    out = []
    for statement in schema_statements(files):
        m = CREATE_VIEW.match(statement)
        if m:
            out.append(m.group(1))
    return out


def dry_run_receipt_problems(data, bound_sha, head_files):
    """The logged `core.schema.apply` dry run: bound by hash, no failed statement, no view drift, every view of the SQL_FILES
    dry run ok, and ending as a dry run that created nothing. The log cannot show which project it ran against."""
    if data is None:
        return ["no receipt exists"]
    if not bound_sha:
        return ["no receipt hash is bound"]
    if hashlib.sha256(data).hexdigest() != bound_sha:
        return ["the receipt hash is not the bound hash"]
    lines = [ln.rstrip() for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    problems = [f"a statement failed its dry run: {ln[:90]}" for ln in lines if ln.startswith("dry run failed on ")]
    drift = "view differs from the file: "
    problems += [f"view drift: {ln[len(drift):].split(' ')[0].rstrip('.')}" for ln in lines if ln.startswith(drift)]
    if not lines or lines[-1] != "dry run only; nothing created":
        problems.append("the receipt does not end with 'dry run only; nothing created', so it is not a dry run log")
    for view in view_names(head_files):
        if f"dry run ok: {view}" not in lines:
            problems.append(f"{view} has no 'dry run ok' line, so its drift was not checked")
    return problems


def require_dry_run_receipt(bound, head_files):
    path = Path(bound.get("dryRunReceiptPath", ""))
    data = path.read_bytes() if path.is_file() else None
    problems = dry_run_receipt_problems(data, bound.get("dryRunReceiptSha256"), head_files)
    if problems:
        raise Stop("DRY_RUN_RECEIPT", "The apply dry run receipt is not clean: " + "; ".join(problems))
