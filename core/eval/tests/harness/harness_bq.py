"""An offline stand-in for a BigQuery dry run.

A dry run asks BigQuery to parse a statement, find every table and column it names, and bind its parameters,
without running it. With no cloud access that check can still be made against what the repository itself says
exists: the CREATE statements under core/ name every table, view and table function and list the columns of the
tables. This module reads those, reads a statement with sqlglot, and reports what a dry run would refuse.

Nothing here skips. A table the schema files do not create is a problem, not a reason to leave the statement out.
The live counterpart (live_dry_run) sends the same statements to BigQuery with dry_run=True when F42_BQ=1 and also
treats a missing table as a failure.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.schema import MappingSchema

PROJECT = "ogilvy-trends-v2"
DATASETS = {"core": "intelligence_42_core", "agent": "intelligence_42_agent"}
HEADER = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(TABLE\s+FUNCTION|MATERIALIZED\s+VIEW|TABLE|VIEW|FUNCTION|MODEL)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?`?([\w.\-]+)`?", re.IGNORECASE)
PARAMETER = re.compile(r"(?<![@\w])@([A-Za-z_]\w*)")


@dataclass
class Obj:
    kind: str
    columns: list = None
    source: str = ""
    arity: int = None
    ddl: str = ""


@dataclass
class Registry:
    objects: dict = field(default_factory=dict)

    def has(self, dataset, name):
        return f"{dataset}.{name}" in self.objects

    def get(self, dataset, name):
        return self.objects.get(f"{dataset}.{name}")

    def names(self, kinds=None):
        return sorted(k for k, o in self.objects.items() if kinds is None or o.kind in kinds)


def strip_comments(text):
    """The text with its block comments and line comments removed, quotes respected."""
    out, i, n, quote = [], 0, len(text), None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            out.append(ch)
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end < 0 else end - 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def split_statements(text):
    """Statements of a script: comments dropped, split on semicolons outside quotes."""
    text = strip_comments(text)
    out, buf, quote, i = [], [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            buf.append(ch)
        elif ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


def render(text):
    return text.replace("{core}", DATASETS["core"]).replace("{agent}", DATASETS["agent"])


def dotted(name):
    """dataset.name from project.dataset.name or dataset.name."""
    parts = name.strip("`").split(".")
    return ".".join(parts[-2:])


def table_columns(statement):
    try:
        tree = sqlglot.parse_one(statement, read="bigquery")
    except SqlglotError:
        return None
    schema = tree.this if isinstance(tree, exp.Create) else None
    if not isinstance(schema, exp.Schema):
        return None
    return [c.name for c in schema.expressions if isinstance(c, exp.ColumnDef)]


def function_arity(statement):
    match = re.search(r"\(", statement[statement.upper().index("FUNCTION"):])
    if not match:
        return None
    start = statement.upper().index("FUNCTION") + match.start()
    depth, i, args, current = 0, start, [], ""
    while i < len(statement):
        ch = statement[i]
        if ch == "(":
            depth += 1
            if depth == 1:
                i += 1
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                args.append(current)
                break
        elif ch == "," and depth == 1:
            args.append(current)
            current = ""
            i += 1
            continue
        current += ch
        i += 1
    return len([a for a in args if a.strip()])


def load_registry(root):
    """Every table, view, table function and function the repository's SQL creates, under core/ (tests excluded)."""
    registry = Registry()
    views = []
    for path in sorted(Path(root, "core").rglob("*.sql")):
        if "tests" in path.relative_to(root).parts:
            continue
        for statement in split_statements(render(path.read_text(encoding="utf-8"))):
            match = HEADER.match(statement)
            if not match:
                continue
            kind = re.sub(r"\s+", " ", match.group(1).upper())
            name = dotted(match.group(2))
            source = path.relative_to(root).as_posix()
            if kind == "TABLE":
                registry.objects[name] = Obj("table", table_columns(statement), source, ddl=statement)
            elif kind in ("VIEW", "MATERIALIZED VIEW"):
                registry.objects[name] = Obj("view", None, source, ddl=statement)
                views.append((name, statement))
            elif kind == "TABLE FUNCTION":
                registry.objects[name] = Obj("table function", None, source, function_arity(statement))
            else:
                registry.objects[name] = Obj(kind.lower(), None, source)
    derive_view_columns(registry, views)
    return registry


def schema_of(registry):
    mapping = {}
    for name, obj in registry.objects.items():
        if obj.columns:
            dataset, table = name.split(".")
            mapping.setdefault(PROJECT, {}).setdefault(dataset, {})[table] = {c: "UNKNOWN" for c in obj.columns}
    return MappingSchema(mapping, dialect="bigquery")


def derive_view_columns(registry, views):
    """Output columns of each view, found by qualifying its SELECT against the tables and the views already done.
    A view that cannot be read keeps columns=None, so a query on it is checked for the name and not the columns."""
    pending = list(views)
    for _ in range(4):
        left = []
        for name, statement in pending:
            columns = view_columns(statement, registry)
            if columns:
                registry.objects[name].columns = columns
            else:
                left.append((name, statement))
        pending = left


def view_columns(statement, registry):
    try:
        tree = sqlglot.parse_one(statement, read="bigquery")
        body = tree.expression
        if body is None:
            return None
        qualified = qualify(body.copy(), schema=schema_of(registry), dialect="bigquery", validate_qualify_columns=False,
                            infer_schema=False)
        names = [s for s in qualified.named_selects if s and s != "*"]
        return names or None
    except Exception:  # a view the reader cannot follow is checked by name only
        return None


@dataclass
class Problem:
    kind: str
    detail: str

    def __str__(self):
        return f"{self.kind}: {self.detail}"


def referenced_tables(tree):
    """[(dataset, name, arity)] for every table or table function the statement names, CTE names left out.
    A name BigQuery lets you quote as one piece (`project.dataset.name`) is split on its dots. dataset is empty for
    a name with no dataset. INFORMATION_SCHEMA views are left out: they are the service's own."""
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    found = []
    for table in tree.find_all(exp.Table):
        arity = None
        this = table.this
        if isinstance(this, exp.Func):
            arity = len(this.expressions)
            name = this.this.name if isinstance(this.this, exp.Identifier) else this.name
            parts = [p for p in name.split(".") if p]
        else:
            parts = []
            for piece in (table.catalog, table.db, table.name):
                parts += [p for p in piece.split(".") if p]
        if not parts:
            continue
        if any(p.upper() == "INFORMATION_SCHEMA" for p in parts):
            continue
        if len(parts) == 1:
            if parts[0].lower() not in ctes:
                found.append(("", parts[0], arity))
            continue
        found.append((parts[-2], parts[-1], arity))
    return found


def check_statement(sql, bound, registry):
    """What a dry run would refuse in this statement. bound is the set of parameter names the caller bound."""
    problems = []
    try:
        trees = sqlglot.parse(sql, read="bigquery")
    except SqlglotError as error:
        return [Problem("parse", str(error).splitlines()[0][:200])]
    if not trees or any(t is None for t in trees):
        return [Problem("parse", "no statement")]
    for tree in trees:
        for dataset, name, arity in referenced_tables(tree):
            if not dataset:
                problems.append(Problem("table", f"{name} is not qualified with a dataset"))
                continue
            obj = registry.get(dataset, name)
            if obj is None:
                problems.append(Problem("table", f"{dataset}.{name} is not created by any SQL file under core/"))
            elif obj.kind == "table function" and arity is not None and obj.arity is not None and arity != obj.arity:
                problems.append(Problem("arity", f"{dataset}.{name} takes {obj.arity} arguments, called with {arity}"))
        if not problems:
            problems += column_problems(tree, registry)
    named = set(PARAMETER.findall(re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)))
    for name in sorted(named - set(bound)):
        problems.append(Problem("parameter", f"@{name} is used and not bound"))
    return problems


def column_problems(tree, registry):
    try:
        qualify(tree.copy(), schema=schema_of(registry), dialect="bigquery", validate_qualify_columns=True,
                infer_schema=False)
    except SqlglotError as error:
        message = str(error).splitlines()[0]
        return [Problem("column", message[:240])]
    except Exception as error:  # sqlglot could not follow a construct; the name checks above still stand
        return [] if "unsupported" in str(error).lower() else [Problem("column", f"{type(error).__name__}: {error}"[:240])]
    return []


def live_dry_run(client, statements, job_config_for):
    """Send each statement to BigQuery with dry_run=True. A statement the service refuses, a missing table
    included, is a problem. Nothing is skipped."""
    problems = []
    for label, sql in statements:
        try:
            client.query(sql, job_config=job_config_for(label))
        except Exception as error:
            problems.append((label, f"{type(error).__name__}: {str(error).splitlines()[0][:200]}"))
    return problems


class Row(dict):
    """A result row as BigQuery's client hands it back: dict(row.items())."""


class RecordingJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return self.rows


class RecordingClient:
    """A stand-in for google.cloud.bigquery.Client that records every statement and its configuration and answers
    from the registry. The catalog read BigQueryStore makes first (INFORMATION_SCHEMA over both datasets) lists
    every object the repository creates when `exists` is true and nothing when it is false, so both branches of
    each method run. Every other query answers with no rows."""

    def __init__(self, registry, exists=True):
        self.registry = registry
        self.exists = exists
        self.statements = []
        self.inserts = []

    def catalog(self):
        rows = []
        if not self.exists:
            return rows
        for name, obj in self.registry.objects.items():
            dataset, table = name.split(".")
            if dataset not in DATASETS.values():
                continue
            if obj.kind in ("table", "view"):
                rows.append(Row(ds=dataset, n=table, what="table"))
            elif obj.kind in ("table function", "function"):
                rows.append(Row(ds=dataset, n=table, what="routine"))
        for what, name in (("runs_column", f"{DATASETS['agent']}.runs"), ("map_column", f"{DATASETS['core']}.cultural_map"),
                           ("findings_column", f"{DATASETS['agent']}.findings")):
            obj = self.registry.objects.get(name)
            dataset = name.split(".")[0]
            rows += [Row(ds=dataset, n=column, what=what) for column in (obj.columns or [])] if obj else []
        return rows

    def query(self, sql, job_config=None, **kwargs):
        self.statements.append((sql, job_config))
        if "INFORMATION_SCHEMA" in sql and "UNION ALL" in sql and "what" in sql:
            return RecordingJob(self.catalog())
        return RecordingJob([])

    def insert_rows_json(self, table, rows, row_ids=None, **kwargs):
        self.inserts.append((table, rows, row_ids))
        return []


def bound_names(job_config):
    return {p.name for p in (getattr(job_config, "query_parameters", None) or [])}


def bound_values(job_config):
    """{name: value} of the parameters a job configuration binds (scalar and array)."""
    out = {}
    for p in getattr(job_config, "query_parameters", None) or []:
        out[p.name] = p.values if hasattr(p, "values") else p.value
    return out


class DuckWarehouse:
    """The repository's tables and views, built in DuckDB from the same CREATE statements, so a BigQuery statement
    can be run on fixture rows. Tables come from their real DDL (columns and types as core/schema says); views come
    from their real definitions, so a view such as v_suppressed_creators is the one the warehouse would run.
    A statement is moved from BigQuery SQL to DuckDB SQL by sqlglot. If sqlglot cannot translate or DuckDB cannot
    run it, run() raises: nothing is skipped."""

    def __init__(self, registry):
        import logging

        import duckdb

        logging.getLogger("sqlglot").setLevel(logging.ERROR)  # sqlglot warns for each construct it translates loosely
        self.registry = registry
        self.con = duckdb.connect(":memory:")
        self.con.execute(f'ATTACH \':memory:\' AS "{PROJECT}"')
        self.con.execute(f'USE "{PROJECT}"')
        for dataset in DATASETS.values():
            self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {dataset}")
        self.uncreated = {}
        for name, obj in registry.objects.items():
            if obj.kind == "table" and name.split(".")[0] in DATASETS.values():
                self._create(name, obj)
        pending = [(n, o) for n, o in registry.objects.items() if o.kind == "view"]
        for _ in range(5):
            pending = [(n, o) for n, o in pending if not self._create(n, o)]

    def _create(self, name, obj):
        try:
            tree = sqlglot.parse_one(obj.ddl, read="bigquery")
            tree.set("properties", None)
            if obj.kind == "table":
                self.con.execute(tree.sql("duckdb").replace("IF NOT EXISTS ", ""))
            else:
                body = tree.expression.sql("duckdb")
                self.con.execute(f"CREATE OR REPLACE VIEW {name} AS {body}")
            self.uncreated.pop(name, None)
            return True
        except Exception as error:
            self.uncreated[name] = f"{type(error).__name__}: {str(error).splitlines()[0][:160]}"
            return False

    def insert(self, name, rows):
        for row in rows:
            columns = ", ".join(f'"{c}"' for c in row)
            marks = ", ".join("?" for _ in row)
            self.con.execute(f"INSERT INTO {name} ({columns}) VALUES ({marks})", list(row.values()))

    def run(self, sql, params=None):
        params = dict(params or {})
        rows = []
        for tree in sqlglot.parse(sql, read="bigquery"):
            duck = tree.sql("duckdb")
            used = {k: v for k, v in params.items() if f"${k}" in duck}
            result = self.con.execute(duck, used)
            if result.description:
                names = [d[0] for d in result.description]
                rows = [dict(zip(names, r)) for r in result.fetchall()]
        return rows
