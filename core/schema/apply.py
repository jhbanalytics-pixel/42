"""Create the 42 datasets and tables in BigQuery.

Every statement in agent.sql and core.sql is dry-run before it runs. Without --apply nothing is
created. With --apply the datasets are created first, then every table is dry-run, then the tables
are created. ALTER TABLE ADD COLUMN and CREATE VIEW statements need their tables to exist, so they
are dry-run after the tables are created and run after that; without --apply the ones whose tables
do not exist yet are skipped and named. Finally INFORMATION_SCHEMA.TABLES is listed for both
datasets, tables and views counted apart.

CREATE VIEW IF NOT EXISTS leaves a view that already exists exactly as it is, and BigQuery accepts the statement
without a word. So a view that already exists is never reported as run ok; its live SQL is read back, compared with
the file with whitespace collapsed, and a difference is named and makes the exit code 1, in a dry run too. Nothing
here replaces a view: an edited view needs a reviewed CREATE OR REPLACE VIEW.

Run from the repo root: py -3.13 -m core.schema.apply [--apply]
"""
import argparse
import re
import sys
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

PROJECT = "ogilvy-trends-v2"
LOCATION = "US"
HERE = Path(__file__).resolve().parent
# agent.sql first: a core view (v_breaking_signals_current) reads agent.runs.
SQL_FILES = (HERE / "agent.sql", HERE / "core.sql")
NAME = re.compile(
    r"^(?:CREATE\s+(SCHEMA|TABLE|VIEW)\s+IF\s+NOT\s+EXISTS\s+`([^`]+)`"
    r"|ALTER\s+TABLE\s+`([^`]+)`\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s)", re.I
)
KINDS = ("schema", "table", "view", "alter")
VIEW = re.compile(r"^CREATE\s+VIEW\s+IF\s+NOT\s+EXISTS\s+`[^`]+`\s+AS\s+(.*)$", re.I | re.S)


def split_statements(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"--[^\n]*", "", text)
    return [s.strip() for s in text.split(";") if s.strip()]


def load_statements(paths=SQL_FILES):
    statements = []
    for path in paths:
        statements += split_statements(Path(path).read_text(encoding="utf-8"))
    return statements


def kind(statement):
    m = NAME.search(statement)
    if not m:
        return None
    return m.group(1).lower() if m.group(1) else "alter"


def statement_name(statement):
    m = NAME.search(statement)
    return (m.group(2) or m.group(3)) if m else statement.splitlines()[0]


def view_body(statement):
    """The SELECT of a CREATE VIEW statement with its whitespace collapsed, or None for any other statement."""
    m = VIEW.match(statement)
    return " ".join(m.group(1).split()) if m else None


def is_schema(statement):
    return kind(statement) == "schema"


def needs(statement):
    """Tables an ALTER or VIEW statement reads or changes, which must exist before its dry run."""
    # Table names are dotted; a backticked column name such as `at` is not one
    names = set(re.findall(r"`([^`.]+\.[^`]+)`", statement))
    if kind(statement) == "view":
        names.discard(statement_name(statement))
    return names


def dataset_of(statement):
    return statement_name(statement).split(".")[1]


def execute(client, statements, dry, kept=frozenset()):
    """Dry-run or run each statement. kept names the views that already existed, which BigQuery leaves alone."""
    word = "dry run" if dry else "run"
    for statement in statements:
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False) if dry else None
        try:
            job = client.query(statement, job_config=config, location=LOCATION)
            if not dry:
                job.result()
        except Exception as error:
            print(f"{word} failed on {statement_name(statement)}: {error}")
            return False
        if not dry and statement_name(statement) in kept:
            print(f"{word} left unchanged: {statement_name(statement)} already existed, "
                  "and CREATE VIEW IF NOT EXISTS does not replace it")
        else:
            print(f"{word} ok: {statement_name(statement)}")
    return True


def missing_datasets(client, schemas):
    missing = set()
    for statement in schemas:
        try:
            client.get_dataset(statement_name(statement))
        except NotFound:
            missing.add(dataset_of(statement))
    return missing


def exists(client, name):
    try:
        client.get_table(name)
    except NotFound:
        return False
    return True


def live_view_sql(client, name):
    """The SQL of the view as BigQuery holds it, whitespace collapsed, or None when there is no such view."""
    try:
        table = client.get_table(name)
    except NotFound:
        return None
    query = getattr(table, "view_query", None)
    return " ".join(query.split()) if query else None


def existing_views(client, statements):
    return {statement_name(s) for s in statements if kind(s) == "view" and live_view_sql(client, statement_name(s))}


def report_drift(client, statements):
    """Name every existing view whose live SQL is not the file's. True when there is none."""
    clean = True
    for s in statements:
        if kind(s) != "view":
            continue
        live = live_view_sql(client, statement_name(s))
        if live is not None and live != view_body(s):
            print(f"view differs from the file: {statement_name(s)}. The live view is the older SQL; "
                  "CREATE VIEW IF NOT EXISTS does not change it. Replace it with a reviewed CREATE OR REPLACE VIEW.")
            clean = False
    return clean


def list_tables(client, statements):
    ok = True
    made = [s for s in statements if kind(s) in ("table", "view")]
    for dataset in sorted({dataset_of(s) for s in made}, reverse=True):
        want = {"table": set(), "view": set()}
        for s in made:
            if dataset_of(s) == dataset:
                want[kind(s)].add(statement_name(s).split(".")[2])
        sql = ("SELECT table_name, table_type "
               f"FROM `{PROJECT}.{dataset}.INFORMATION_SCHEMA.TABLES` ORDER BY table_name")
        rows = list(client.query(sql, location=LOCATION).result())
        names = [row["table_name"] for row in rows]
        views = [row["table_name"] for row in rows if row["table_type"] == "VIEW"]
        print(f"{dataset}: {len(names) - len(views)} tables (expected {len(want['table'])}), "
              f"{len(views)} views (expected {len(want['view'])})")
        for name in names:
            print(f"  {name}")
        absent = sorted((want["table"] | want["view"]) - set(names))
        if absent:
            print(f"  missing: {', '.join(absent)}")
            ok = False
    return ok


def run(client, statements, apply=False):
    tables = [s for s in statements if kind(s) == "table"]
    later = [s for s in statements if kind(s) in ("alter", "view")]
    # bootstrap.py creates the datasets; f42-builder cannot, so only missing ones are attempted
    missing = missing_datasets(client, [s for s in statements if is_schema(s)])
    schemas = [s for s in statements if is_schema(s) and dataset_of(s) in missing]
    if not execute(client, schemas, dry=True):
        return 1
    if apply:
        if not execute(client, schemas, dry=False):
            return 1
    else:
        if missing:
            skipped = [t for t in tables if dataset_of(t) in missing]
            tables = [t for t in tables if dataset_of(t) not in missing]
            print(f"skipped {len(skipped)} table dry runs: {', '.join(sorted(missing))} "
                  "not created yet; --apply creates it and dry-runs its tables first")
    if not execute(client, tables, dry=True):
        return 1
    if not apply:
        ready = [s for s in later if all(exists(client, n) for n in needs(s))]
        waiting = [statement_name(s) for s in later if s not in ready]
        if waiting:
            print(f"skipped {len(waiting)} dry runs: {', '.join(waiting)} need tables not created "
                  "yet; --apply creates the tables first, then dry-runs these")
        if not execute(client, ready, dry=True):
            return 1
        unchanged = report_drift(client, ready)
        print("dry run only; nothing created")
        return 0 if unchanged else 1
    if not execute(client, tables, dry=False):
        return 1
    if not execute(client, later, dry=True):
        return 1
    kept = existing_views(client, later)
    if not execute(client, later, dry=False, kept=kept):
        return 1
    unchanged = report_drift(client, later)
    return 0 if list_tables(client, tables + later) and unchanged else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dry-run, then optionally create, the 42 tables.")
    parser.add_argument("--apply", action="store_true", help="create after every dry run passes")
    args = parser.parse_args(argv)
    client = bigquery.Client(project=PROJECT, location=LOCATION)
    return run(client, load_statements(), apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
