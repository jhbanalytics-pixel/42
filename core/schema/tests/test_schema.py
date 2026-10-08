import re
import types
from pathlib import Path

import pytest

from core.schema import apply

SCHEMA_DIR = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
DATA_MD = REPO / "docs" / "full-42" / "DATA.md"
SQL_FILES = {
    "intelligence_42_core": SCHEMA_DIR / "core.sql",
    "intelligence_42_agent": SCHEMA_DIR / "agent.sql",
}
PROJECT = "ogilvy-trends-v2"
IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


# DATA.md parsing

def doc_section_1():
    text = DATA_MD.read_text(encoding="utf-8")
    start = text.index("## 1. Tables")
    end = text.index("## 2. Backfill")
    return text[start:end]


def strip_line_comments(sql):
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def split_top(text, seps=",", pairs=("()", "<>")):
    opens = {a: b for a, b in pairs}
    closes = set(opens.values())
    depth, cur, out = 0, [], []
    for ch in text:
        if ch in opens:
            depth += 1
        elif ch in closes:
            depth -= 1
        if depth == 0 and ch in seps:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p.strip() for p in out if p.strip()]


CREATE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?(?:([\w-]+)\.)?(\w+)\.(\w+)`?\s*\(", re.I
)


def create_body(statement):
    """Return (match, column list text, index after its closing paren) for one CREATE TABLE."""
    m = CREATE.search(statement)
    assert m, statement[:80]
    body_start = m.end()
    depth, i = 1, body_start
    while depth:
        depth += {"(": 1, ")": -1}.get(statement[i], 0)
        i += 1
    return m, statement[body_start:i - 1], i


def parse_create(statement):
    """Return (dataset, table, [(name, type, not_null)], tail) for one CREATE TABLE."""
    m, body, i = create_body(statement)
    tail = " ".join(statement[i:].split())
    columns = []
    for piece in split_top(body):
        words = piece.split()
        not_null = [w.upper() for w in words[-2:]] == ["NOT", "NULL"]
        if not_null:
            words = words[:-2]
        columns.append((words[0].strip("`"), " ".join(" ".join(words[1:]).split()), not_null))
    return m.group(2), m.group(3), columns, tail


def doc_ddl():
    section = doc_section_1()
    block = section[section.index("```sql") + len("```sql"):]
    block = strip_line_comments(block[: block.index("```")])
    tables = {}
    for statement in block.split(";"):
        if CREATE.search(statement):
            dataset, table, columns, tail = parse_create(statement)
            tables[table] = (dataset, columns, tail)
    return tables


def first_token_columns(text):
    text = re.split(r"\.\s|\.$", text.strip(), maxsplit=1)[0]
    text = re.sub(r"\([^()]*\)", "", text)
    names = []
    for piece in split_top(text):
        for part in piece.split(" or "):
            token = part.split()[0] if part.split() else ""
            if IDENT.match(token):
                names.append(token)
    return names


def single_ident_columns(text):
    names = []
    for piece in split_top(text, seps=",;"):
        piece = re.sub(r"^and for Ask\s+", "", piece)
        piece = piece.split(":", 1)[0].strip()
        piece = re.sub(r"\s+ARRAY<.*$", "", piece)
        if IDENT.match(piece):
            names.append(piece)
    return names


def item_state_select_columns():
    text = DATA_MD.read_text(encoding="utf-8")
    start = text.index("SELECT @d metric_date")
    end = text.index("FROM wr\nWHERE", start)
    select = strip_line_comments(text[start + len("SELECT"):end])
    return [re.findall(r"\w+", piece)[-1] for piece in split_top(select, pairs=("()",))]


def doc_bullets():
    """Return {table: (dataset, key columns, other columns)} from the bullet list."""
    section = doc_section_1()
    bullets = section[section.index("Other tables"):]
    tables = {}
    for line in bullets.splitlines():
        agent = re.match(r"^- In intelligence_42_agent:\s*(.*)$", line)
        if agent:
            for chunk in split_top(agent.group(1), seps=";"):
                name = re.match(r"(\w+)\s*\((.*)\)", chunk, re.S)
                tables[name.group(1)] = (
                    "intelligence_42_agent", [], single_ident_columns(name.group(2))
                )
            continue
        core = re.match(r"^- (\w+) \(([^)]*)\)\.?(?::\s*(.*))?$", line)
        if not core:
            continue
        table, key, rest = core.group(1), core.group(2), core.group(3)
        if rest is None:
            key, rest = "", key
        if rest.startswith("the columns of the final SELECT"):
            columns = item_state_select_columns()
        else:
            columns = first_token_columns(rest)
        tables[table] = ("intelligence_42_core", first_token_columns(key), columns)
    return tables


def doc_tables():
    tables = {t: v[0] for t, v in doc_ddl().items()}
    for t, v in doc_bullets().items():
        assert t not in tables, f"{t} listed twice in DATA.md"
        tables[t] = v[0]
    return tables


# Schema files

def statements(dataset):
    return apply.split_statements(SQL_FILES[dataset].read_text(encoding="utf-8"))


def our_tables():
    found = {}
    for dataset in SQL_FILES:
        for statement in statements(dataset):
            if CREATE.search(statement):
                project, ds, table, columns, tail = (CREATE.search(statement).group(1),
                                                    *parse_create(statement))
                found.setdefault(table, []).append((project, ds, dataset, columns, tail))
    return found


def column_types(table):
    return {name: typ for name, typ, _ in our_tables()[table][0][3]}


# Tests: the doc parser itself

def test_doc_parser_finds_the_tables():
    ddl = doc_ddl()
    bullets = doc_bullets()
    assert len(ddl) >= 8
    assert "credit_ledger" in bullets and "briefs" in bullets
    agent = {t for t, v in bullets.items() if v[0] == "intelligence_42_agent"}
    assert {"runs", "findings", "forecasts", "feedback", "claim_checks", "watches",
            "watch_matches", "engine_scorecard", "investigations", "dossier_versions",
            "dossier_reviews", "schedules", "skins"} <= agent
    assert bullets["suppressions"][0] == "intelligence_42_core"


# Tests: splitter

def test_splitter_one_statement_per_table():
    expected = doc_tables()
    for dataset, path in SQL_FILES.items():
        stmts = statements(dataset)
        want = sum(1 for d in expected.values() if d == dataset)
        assert re.match(r"CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS", stmts[0], re.I)
        assert [apply.kind(s) for s in stmts].count("schema") == 1, path.name
        assert [apply.kind(s) for s in stmts].count("table") == want, path.name
        for statement in stmts:
            assert apply.kind(statement) in apply.KINDS, statement[:80]
            assert len(re.findall(r"\b(?:CREATE|ALTER)\b", statement, re.I)) == 1, statement[:80]
            assert apply.dataset_of(statement) == dataset, statement[:80]


# Unsafe samples are joined at run time so the guard hook does not read them as commands.
UNSAFE = [
    " ".join(["ALTER TABLE `p.d.t`", "DR" + "OP", "COLUMN y"]),
    " ".join(["CREATE OR", "REP" + "LACE", "VIEW `p.d.v` AS SELECT 1"]),
    "CREATE TABLE `p.d.t` (x INT64)",
    " ".join(["DEL" + "ETE", "FROM `p.d.t` WHERE TRUE"]),
]


def test_every_statement_kind_parses():
    cases = {
        'CREATE SCHEMA IF NOT EXISTS `p.d`\nOPTIONS (location = "US")': ("schema", "p.d"),
        "CREATE TABLE IF NOT EXISTS `p.d.t` (x INT64)": ("table", "p.d.t"),
        "CREATE VIEW IF NOT EXISTS `p.d.v_t_current` AS\nSELECT t.* FROM `p.d.t` t":
            ("view", "p.d.v_t_current"),
        "ALTER TABLE `p.d.t` ADD COLUMN IF NOT EXISTS y FLOAT64": ("alter", "p.d.t"),
        "ALTER TABLE `p.d.t` ADD COLUMN IF NOT EXISTS `at` TIMESTAMP": ("alter", "p.d.t"),
    }
    for statement, (kind, name) in cases.items():
        assert apply.kind(statement) == kind, statement
        assert apply.statement_name(statement) == name, statement
        assert apply.is_schema(statement) == (kind == "schema"), statement
    assert apply.dataset_of("ALTER TABLE `p.d.t` ADD COLUMN IF NOT EXISTS y FLOAT64") == "d"
    assert apply.dataset_of("CREATE VIEW IF NOT EXISTS `p.d.v` AS SELECT 1") == "d"
    assert apply.needs("CREATE VIEW IF NOT EXISTS `p.d.v` AS SELECT t.* FROM `p.d.t` t") == {"p.d.t"}
    assert apply.needs("ALTER TABLE `p.d.t` ADD COLUMN IF NOT EXISTS y FLOAT64") == {"p.d.t"}
    assert apply.needs("ALTER TABLE `p.d.t` ADD COLUMN IF NOT EXISTS `at` TIMESTAMP") == {"p.d.t"}
    for unsafe in UNSAFE:
        assert apply.kind(unsafe) is None, unsafe


def test_splitter_drops_comments_and_blank_statements():
    text = (
        "CREATE TABLE IF NOT EXISTS `p.d.a` (x INT64);\n"
        "/* CREATE VECTOR INDEX i ON `p.d.a`(x); later */\n"
        "-- note; with a semicolon\n"
        "CREATE TABLE IF NOT EXISTS `p.d.b` (y INT64);\n\n"
    )
    stmts = apply.split_statements(text)
    assert stmts == [
        "CREATE TABLE IF NOT EXISTS `p.d.a` (x INT64)",
        "CREATE TABLE IF NOT EXISTS `p.d.b` (y INT64)",
    ]


# Tests: coverage and placement

def test_every_doc_table_created_once_in_its_dataset():
    expected = doc_tables()
    found = our_tables()
    assert set(found) == set(expected)
    for table, dataset in expected.items():
        entries = found[table]
        assert len(entries) == 1, f"{table} created {len(entries)} times"
        project, ds, file_dataset, _, _ = entries[0]
        assert project == PROJECT, table
        assert ds == dataset == file_dataset, table


def test_schemas_created_in_us_only():
    for dataset in SQL_FILES:
        first = statements(dataset)[0]
        assert re.fullmatch(
            rf'CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+`{PROJECT}\.{dataset}`\s*'
            r'OPTIONS\s*\(\s*location\s*=\s*"US"\s*\)',
            first,
        ), first


def test_full_ddl_tables_match_the_doc_column_for_column():
    ddl = doc_ddl()
    found = our_tables()
    for table, (dataset, columns, tail) in ddl.items():
        _, ds, _, our_columns, our_tail = found[table][0]
        assert ds == dataset
        assert our_columns == columns, table
        assert our_tail == tail, table


def test_bullet_tables_carry_the_doc_columns():
    for table, (_, key, columns) in doc_bullets().items():
        ours = [name for name, _, _ in our_tables()[table][0][3]]
        for name in key + columns:
            assert name in ours, f"{table} lacks {name}"
        if table == "item_state":
            assert ours == columns


def test_bullet_tables_keys_not_null_and_partitioned_on_their_date():
    for table, (_, key, _) in doc_bullets().items():
        _, _, _, columns, tail = our_tables()[table][0]
        not_null = {n for n, _, nn in columns if nn}
        line = re.search(rf"^- {table} \(([^)]*)\)", doc_section_1(), re.M)
        either = set()
        if line:
            for piece in line.group(1).split(","):
                if " or " in piece:
                    either |= set(first_token_columns(piece))
        assert set(key) - either <= not_null, table
        assert not either & not_null, table
        dates = [n for n, t, _ in columns if t == "DATE"]
        part = re.search(r"PARTITION BY (\w+)", tail)
        if dates:
            assert part and part.group(1) in dates, table
            key_dates = [k for k in key if k in dates]
            if key_dates:
                assert part.group(1) == key_dates[0], table
        else:
            assert not part, table


def test_credit_ledger_shape():
    cols = column_types("credit_ledger")
    for name in ("job", "route"):
        assert cols[name] == "STRING"
    assert cols["logged_at"] == "TIMESTAMP"
    assert cols["trend_date"] == "DATE"
    _, _, _, columns, tail = our_tables()["credit_ledger"][0]
    assert ("trend_date", "DATE", True) in columns
    assert tail.startswith("PARTITION BY trend_date")
    assert "balance_before" not in cols


def test_breakout_signals_shape():
    _, _, _, columns, tail = our_tables()["breakout_signals"][0]
    assert columns == [
        ("metric_date", "DATE", True), ("market", "STRING", True), ("item_id", "STRING", True),
        ("run_id", "STRING", True), ("creators", "INT64", False), ("posts", "INT64", False),
        ("evidence_post_ids", "ARRAY<STRING>", False), ("top_ratio", "FLOAT64", False),
        ("held_flagged", "INT64", False), ("rule_version", "STRING", False),
    ]
    assert tail == "PARTITION BY metric_date CLUSTER BY market, item_id"


def test_item_hourly_shape():
    _, _, _, columns, tail = our_tables()["item_hourly"][0]
    assert columns == [
        ("market", "STRING", True), ("item_id", "STRING", True), ("platform", "STRING", True),
        ("hour", "TIMESTAMP", True), ("posts", "INT64", False), ("creators", "INT64", False),
        ("lane_class", "STRING", True), ("run_id", "STRING", True),
    ]
    assert tail == "PARTITION BY DATE(hour) CLUSTER BY market, item_id"


def test_breaking_signals_shape():
    # The columns core/detect/breaking.py appends (FIELDS, APPEND_SQL) and L2 Needs 34 asks for.
    from core.detect import breaking

    _, ds, _, columns, tail = our_tables()["breaking_signals"][0]
    assert ds == "intelligence_42_core"
    assert columns == [
        ("hour", "TIMESTAMP", True), ("market", "STRING", True), ("item_id", "STRING", True),
        ("posts6", "INT64", False), ("creators6", "INT64", False), ("expected6", "FLOAT64", False),
        ("ratio", "FLOAT64", False), ("platforms", "INT64", False), ("run_id", "STRING", True),
        ("rule_version", "STRING", False),
    ]
    assert [(n, t) for n, t, _ in columns] == list(breaking.FIELDS)
    assert tail == "PARTITION BY DATE(hour) CLUSTER BY market, item_id"


BREAKING_VIEW = "ogilvy-trends-v2.intelligence_42_core.v_breaking_signals_current"


def test_breaking_current_view_is_the_one_breaking_py_declares():
    from core.detect import breaking, sqlrun

    [view] = [v for v in views_in("intelligence_42_core") if apply.statement_name(v) == BREAKING_VIEW]
    assert apply.needs(view) == {"ogilvy-trends-v2.intelligence_42_core.breaking_signals",
                                 "ogilvy-trends-v2.intelligence_42_agent.runs"}
    ours = " ".join(view.split(" AS\n", 1)[1].replace("`", "").replace("ogilvy-trends-v2.", "").split())
    theirs = sqlrun.render(breaking.CURRENT_VIEW_SQL).strip().split(" AS\n", 1)[1]
    assert ours == " ".join(theirs.split())


def test_inferred_types_follow_the_plain_rules():
    types = {"STRING", "DATE", "TIMESTAMP", "INT64", "FLOAT64", "BOOL", "JSON"}
    for table, entries in our_tables().items():
        for name, typ, _ in entries[0][3]:
            if name.endswith("_date") or name == "day":
                assert typ == "DATE", (table, name)
            if name.endswith("_at"):
                assert typ == "TIMESTAMP", (table, name)
            if name in ("embedding", "centroid"):
                assert typ == "ARRAY<FLOAT64>", (table, name)
            assert typ in types or typ.startswith(("ARRAY<", "STRUCT<")), (table, name, typ)
    claims = column_types("findings")["claims"]
    assert claims == (
        "ARRAY<STRUCT<text STRING, label STRING, item_ids ARRAY<STRING>, "
        "evidence_post_ids ARRAY<STRING>, query_ids ARRAY<STRING>, run_ids ARRAY<STRING>, "
        "result_hashes ARRAY<STRING>>>"
    )


# Tests: rule 7 and rule 1

def test_no_destructive_or_expiring_sql():
    for path in SQL_FILES.values():
        text = path.read_text(encoding="utf-8")
        for pattern in (r"CREATE\s+OR\s+REPLACE", r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\b",
                        r"expir", r"\bREPLACE\b"):
            assert not re.search(pattern, text, re.I), (path.name, pattern)
        for statement in apply.split_statements(text):
            for pattern in (r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\b", r"\bREPLACE\b",
                            r"expir", r"\bRENAME\b", r"\bSET\s+OPTIONS\b"):
                assert not re.search(pattern, statement, re.I), (statement[:80], pattern)
            if re.search(r"\bOPTIONS\b", statement, re.I):
                assert statement.upper().startswith("CREATE SCHEMA"), statement[:80]
            assert apply.kind(statement) is not None, statement[:80]


# Banned strings are joined at run time so neither this file nor its bytecode holds them.
BANNED_TEXT = [
    "".join(["gen", "z"]), "".join(["gen", " z"]), "".join(["gen", "-z"]), "".join(["gen", "_z"]),
    "".join(["google", "_trends"]), "".join(["google", " trends"]), "".join(["google", "trends"]),
]
BANNED_TOKENS = {
    "".join(p) for p in (
        ("a", "ge"), ("a", "ges"), ("a", "ged"), ("bir", "th"), ("bir", "thday"), ("bir", "thdate"),
        ("d", "ob"), ("y", "ob"), ("gen", "der"), ("s", "ex"), ("gen", "eration"),
        ("gen", "erations"), ("demo", "graphic"), ("demo", "graphics"), ("you", "th"),
        ("te", "en"), ("te", "ens"), ("millen", "nial"), ("boom", "er"), ("zoom", "er"),
        ("gen", "z"), ("gen", "x"), ("gen", "y"), ("gen", "alpha"),
    )
}


def test_no_demographic_or_age_columns():
    for table, entries in our_tables().items():
        for name, typ, _ in entries[0][3]:
            fields = [name] + re.findall(r"(\w+)\s+(?:STRING|INT64|FLOAT64|BOOL|DATE|TIMESTAMP|JSON|ARRAY)", typ)
            for field in fields:
                tokens = set(field.lower().split("_")) | {field.lower()}
                assert not tokens & BANNED_TOKENS, (table, field)


def test_no_banned_text_in_schema_package():
    files = list(SCHEMA_DIR.rglob("*.sql")) + list(SCHEMA_DIR.rglob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for word in BANNED_TEXT:
            assert word not in text, path.name
        for dash in (chr(0x2014), chr(0x2013)):
            assert dash not in text, path.name


ALTERS = [
    ("runs", "model_usd", "FLOAT64"),
    ("claim_checks", "reason", "STRING"),
    ("feedback", "at", "TIMESTAMP"),
    ("watches", "status_at", "TIMESTAMP"),
    ("gdelt_daily", "market_rule", "STRING"),
    ("post_observations", "source_market", "STRING"),
    ("post_observations", "source_region", "STRING"),
    ("creators", "display_name", "STRING"),
    ("creators", "verified", "BOOL"),
    ("creators", "profile_location", "STRING"),
    ("creators", "first_seen", "TIMESTAMP"),
    ("creators", "last_seen", "TIMESTAMP"),
    ("post_enrichment", "sensitive", "ARRAY<STRING>"),
]
ADD = re.compile(
    r"^ALTER\s+TABLE\s+`ogilvy-trends-v2\.intelligence_42_(?:agent|core)\.(\w+)`\s+"
    r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+`?(\w+)`?\s+(\w+(?:<\w+>)?)$", re.I
)


def alters():
    found = []
    for dataset in SQL_FILES:
        for statement in statements(dataset):
            if apply.kind(statement) == "alter":
                m = ADD.match(" ".join(statement.split()))
                assert m, statement
                found.append(m.groups())
    return found


def test_added_columns_are_altered_in_and_created_with_their_table():
    found = alters()
    assert sorted(found) == sorted(ALTERS)
    for table, column, typ in found:
        assert (column, typ, False) in our_tables()[table][0][3], (table, column)


def test_alters_and_views_come_after_what_they_need():
    stmts = apply.load_statements()
    created = {}
    for i, statement in enumerate(stmts):
        if apply.kind(statement) in ("table", "view"):
            created[apply.statement_name(statement)] = i
    for i, statement in enumerate(stmts):
        if apply.kind(statement) in ("alter", "view"):
            for name in apply.needs(statement):
                assert created[name] < i, (statement[:60], name)


def test_watch_tables_shape():
    watches = our_tables()["watches"][0]
    assert watches[3] == [
        ("watch_id", "STRING", True), ("created_at", "TIMESTAMP", False),
        ("status_at", "TIMESTAMP", False), ("who", "STRING", False), ("target", "JSON", False), ("market", "STRING", False),
        ("rule", "JSON", False), ("label", "STRING", False), ("status", "STRING", False),
    ]
    assert watches[4] == ""
    matches = our_tables()["watch_matches"][0]
    assert matches[3] == [
        ("watch_id", "STRING", True), ("match_date", "DATE", True), ("item_id", "STRING", False),
        ("market", "STRING", False), ("method", "STRING", False), ("run_id", "STRING", False),
    ]
    assert matches[4] == "PARTITION BY match_date"


def test_watches_current_view_keeps_the_latest_row_per_watch():
    views = [s for s in statements("intelligence_42_agent") if apply.kind(s) == "view"]
    assert [apply.statement_name(v) for v in views] == [
        "ogilvy-trends-v2.intelligence_42_agent.v_watches_current"
    ]
    body = " ".join(views[0].split())
    assert apply.needs(views[0]) == {"ogilvy-trends-v2.intelligence_42_agent.watches"}
    assert "SELECT w.* FROM `ogilvy-trends-v2.intelligence_42_agent.watches` w" in body
    assert ("QUALIFY ROW_NUMBER() OVER (PARTITION BY w.watch_id "
            "ORDER BY COALESCE(w.status_at, w.created_at) DESC NULLS LAST, w.status DESC, "
            "TO_JSON_STRING(w)) = 1" in body)


def test_watches_current_view_lets_a_resume_row_win_over_the_earlier_pause():
    # A resume row keeps the watch's first created_at and carries a later status_at, so the view
    # must order by status time first; ordering by created_at alone ties and hands it to paused.
    view = [s for s in statements("intelligence_42_agent") if apply.kind(s) == "view"][0]
    order = re.search(r"ORDER BY (.*?)\) = 1", " ".join(view.split())).group(1)
    assert order.startswith("COALESCE(w.status_at, w.created_at) DESC NULLS LAST, ")
    assert order.count("created_at") == 1


# GoogleSQL reserved keywords, from the lexical structure page of the BigQuery docs.
RESERVED = set("""
ALL AND ANY ARRAY AS ASC ASSERT_ROWS_MODIFIED AT BETWEEN BY CASE CAST COLLATE CONTAINS CREATE
CROSS CUBE CURRENT DEFAULT DEFINE DESC DISTINCT ELSE END ENUM ESCAPE EXCEPT EXCLUDE EXISTS
EXTRACT FALSE FETCH FOLLOWING FOR FROM FULL GROUP GROUPING GROUPS HASH HAVING IF IGNORE IN INNER
INTERSECT INTERVAL INTO IS JOIN LATERAL LEFT LIKE LIMIT LOOKUP MERGE NATURAL NEW NO NOT NULL
NULLS OF ON OR ORDER OUTER OVER PARTITION PRECEDING PROTO QUALIFY RANGE RECURSIVE RESPECT RIGHT
ROLLUP ROWS SELECT SET SOME STRUCT TABLESAMPLE THEN TO TREAT TRUE UNBOUNDED UNION UNNEST USING
WHEN WHERE WINDOW WITH WITHIN
""".split())
COLUMN = re.compile(
    r"(?<![\w`])(`?)(\w+)(`?)\s+(?:STRING|INT64|FLOAT64|BOOL|DATE|TIMESTAMP|JSON|ARRAY|STRUCT)\b"
)


def test_reserved_word_columns_are_backticked():
    seen = set()
    for dataset in SQL_FILES:
        for statement in statements(dataset):
            if apply.kind(statement) == "table":
                columns = create_body(statement)[1]
            elif apply.kind(statement) == "alter":
                columns = re.split(r"\bEXISTS\b", statement, flags=re.I)[-1]
            else:
                continue
            for left, name, right in COLUMN.findall(columns):
                seen.add(name.lower())
                if name.upper() in RESERVED:
                    assert left == right == "`", (dataset, name, statement[:60])
    assert {"at", "who", "created_at", "claims", "text"} <= seen


def shape(table):
    """(dataset, [(column, type, not_null)], tail) of one created table."""
    _, ds, _, columns, tail = our_tables()[table][0]
    return ds, columns, tail


def test_engine_scorecard_shape():
    # The scorecard DDL, verbatim: one JSON Figure per metric
    ds, columns, tail = shape("engine_scorecard")
    assert ds == "intelligence_42_agent"
    assert columns == [
        ("week_start", "DATE", True), ("week_end", "DATE", True), ("market", "STRING", True),
        ("run_id", "STRING", True), ("rule_version", "STRING", False),
        ("time_to_detect", "JSON", False), ("lead_time", "JSON", False), ("precision", "JSON", False),
        ("recall", "JSON", False), ("breadth_platforms", "JSON", False),
        ("expansion_cluster_share", "JSON", False), ("expansion_platform_share", "JSON", False),
        ("expansion_language_share", "JSON", False), ("cost_per_confirmed", "JSON", False),
    ]
    assert tail == "PARTITION BY week_start CLUSTER BY market"


EVAL_SQL = REPO / "core" / "eval" / "sql"


@pytest.mark.parametrize("table", ["forecast_score", "weekly_quality"])
def test_score_tables_match_their_eval_writer_column_for_column(table):
    # core/eval/forecast_score.py and quality_score.py create these with CREATE TABLE IF NOT EXISTS; the weekly
    # learn job writes them only once they exist, so the schema makes them exactly as the writer would
    writer = apply.split_statements((EVAL_SQL / f"{table}_table.sql").read_text(encoding="utf-8"))
    [statement] = [s for s in writer if CREATE.search(s)]
    project = CREATE.search(statement).group(1)
    w_ds, w_table, w_columns, w_tail = parse_create(statement)
    found = our_tables()[table]
    assert len(found) == 1
    our_project, ds, file_dataset, columns, tail = found[0]
    assert (our_project, ds, file_dataset, w_table) == (project, w_ds, "intelligence_42_agent", table)
    assert columns == w_columns and tail == w_tail == "PARTITION BY week_start"


def test_forecast_score_is_the_table_asks_sql_query_hides():
    # TRUST.md K9: forecast scores stay hidden from the agent like the forecasts themselves
    from core.agent.tools import sql_query
    ds, _, _ = shape("forecast_score")
    assert f"{ds}.forecast_score" in sql_query.HIDDEN_TABLES


def test_investigations_shape():
    # contract.md 13.1: one row per draft, edit, start and finish; the latest version is current
    ds, columns, tail = shape("investigations")
    assert ds == "intelligence_42_agent"
    assert columns == [
        ("investigation_id", "STRING", True), ("version", "INT64", True),
        ("created_at", "TIMESTAMP", False), ("who", "STRING", False), ("status", "STRING", False),
        ("question", "STRING", False), ("market", "STRING", False), ("plan", "JSON", False),
        ("estimate", "JSON", False), ("ask_id", "STRING", False), ("run_id", "STRING", False),
    ]
    assert tail == "CLUSTER BY investigation_id"


def test_dossier_tables_shape():
    # contract.md 13.2: versions are appended, ticks belong to the claim and the latest row counts
    ds, columns, tail = shape("dossier_versions")
    assert ds == "intelligence_42_agent"
    assert columns == [
        ("dossier_id", "STRING", True), ("version", "INT64", True), ("created_at", "TIMESTAMP", False),
        ("who", "STRING", False), ("state", "STRING", False), ("body", "JSON", False),
        ("source_ask_id", "STRING", False), ("content_hash", "STRING", False),
    ]
    assert tail == "CLUSTER BY dossier_id"
    ds, columns, tail = shape("dossier_reviews")
    assert ds == "intelligence_42_agent"
    assert columns == [
        ("dossier_id", "STRING", True), ("claim_id", "STRING", True), ("ticked", "BOOL", False),
        ("note", "STRING", False), ("who", "STRING", False), ("at", "TIMESTAMP", False),
    ]
    assert tail == "CLUSTER BY dossier_id, claim_id"


def test_schedules_and_skins_shape():
    # contract.md 14.2 and 15.1: append-only, current = latest row in v_watches_current's order
    ds, columns, tail = shape("schedules")
    assert ds == "intelligence_42_agent"
    assert columns == [
        ("schedule_id", "STRING", True), ("created_at", "TIMESTAMP", False),
        ("status_at", "TIMESTAMP", False), ("who", "STRING", False), ("question", "STRING", False),
        ("market", "STRING", False), ("tier", "STRING", False), ("cadence", "STRING", False),
        ("deliver", "JSON", False), ("status", "STRING", False),
    ]
    assert tail == "CLUSTER BY schedule_id"
    ds, columns, tail = shape("skins")
    assert ds == "intelligence_42_agent"
    assert columns == [
        ("skin_id", "STRING", True), ("skin_key", "STRING", False), ("created_at", "TIMESTAMP", False),
        ("status_at", "TIMESTAMP", False), ("who", "STRING", False), ("name", "STRING", False),
        ("markets", "JSON", False), ("terms", "JSON", False), ("hashtags", "JSON", False),
        ("accounts", "JSON", False), ("watch_ids", "JSON", False), ("template", "STRING", False),
        ("status", "STRING", False),
    ]
    assert tail == "CLUSTER BY skin_id"


def test_suppressions_shape():
    # SETUP.md data protection: a creator is suppressed by id or by platform and handle, with a reason
    # and who; lifting a suppression appends a row, so nothing is ever removed
    ds, columns, tail = shape("suppressions")
    assert ds == "intelligence_42_core"
    assert columns == [
        ("suppression_id", "STRING", True), ("status_at", "TIMESTAMP", True),
        ("status", "STRING", True), ("creator_id", "STRING", False), ("platform", "STRING", False),
        ("handle", "STRING", False), ("reason", "STRING", False), ("who", "STRING", False),
    ]
    assert tail == "CLUSTER BY suppression_id"


def test_creators_shape():
    # The collect job's creators MERGE (core/collect/writers.py) inserts a (platform, creator_id) once and
    # moves handle, display_name, followers, verified, profile_location and last_seen; first_seen stays.
    # No column holds or infers a person's age (rule 1)
    ds, columns, tail = shape("creators")
    assert ds == "intelligence_42_core"
    assert columns == [
        ("creator_id", "STRING", True), ("platform", "STRING", False), ("handle", "STRING", False),
        ("followers", "INT64", False), ("tier", "STRING", False), ("account_created_at", "TIMESTAMP", False),
        ("home_market", "STRING", False), ("verified_region", "STRING", False), ("coord_score", "INT64", False),
        ("display_name", "STRING", False), ("verified", "BOOL", False), ("profile_location", "STRING", False),
        ("first_seen", "TIMESTAMP", False), ("last_seen", "TIMESTAMP", False),
    ]
    assert tail == ""


NEW_TABLES = ("engine_scorecard", "investigations", "dossier_versions", "dossier_reviews", "schedules",
              "skins", "suppressions", "forecast_score", "weekly_quality", "breaking_signals")


def test_new_tables_are_plain_creates_without_expiry():
    for dataset in SQL_FILES:
        for statement in statements(dataset):
            if apply.kind(statement) != "table" or apply.statement_name(statement).split(".")[2] not in NEW_TABLES:
                continue
            assert statement.startswith("CREATE TABLE IF NOT EXISTS `ogilvy-trends-v2."), statement[:60]
            for pattern in (r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\b", r"\bREPLACE\b", r"expir",
                            r"\bOPTIONS\b"):
                assert not re.search(pattern, statement, re.I), (statement[:60], pattern)


def views_in(dataset):
    return [s for s in statements(dataset) if apply.kind(s) == "view"]


SUPPRESSED_VIEW = "ogilvy-trends-v2.intelligence_42_core.v_suppressed_creators"
SOURCE_MARKETS_VIEW = "ogilvy-trends-v2.intelligence_42_core.v_post_source_markets"


def test_suppressed_creators_view_is_what_the_api_reads():
    # core/api/store.py (L4) finds v_suppressed_creators in core or agent and reads DISTINCT creator_id
    views = views_in("intelligence_42_core")
    assert {apply.statement_name(v) for v in views} == {SUPPRESSED_VIEW, SOURCE_MARKETS_VIEW, BREAKING_VIEW}
    view = next(v for v in views if apply.statement_name(v) == SUPPRESSED_VIEW)
    assert apply.needs(view) == {
        "ogilvy-trends-v2.intelligence_42_core.suppressions",
        "ogilvy-trends-v2.intelligence_42_core.creators",
    }
    body = " ".join(view.split())
    assert ("QUALIFY ROW_NUMBER() OVER (PARTITION BY x.suppression_id "
            "ORDER BY x.status_at DESC, x.status = 'lifted', TO_JSON_STRING(x)) = 1" in body)


def test_source_market_view_publishes_distinct_sorted_markets_and_sightings():
    views = views_in("intelligence_42_core")
    view = next(v for v in views if apply.statement_name(v) == SOURCE_MARKETS_VIEW)
    assert apply.needs(view) == {"ogilvy-trends-v2.intelligence_42_core.post_observations"}
    body = " ".join(view.split())
    assert "source_markets" in body and "ARRAY_AGG(DISTINCT source_market ORDER BY source_market)" in body
    assert "source_sightings" in body and "source_region" in body
    assert "observed_date AS obs_date" in body
    assert "source_market IN ('ZA', 'NG', 'KE')" in body
    assert "observed_at IS NOT NULL" in body and "observed_date IS NOT NULL" in body
    assert "GROUP BY post_id" in body
    assert "ANY_VALUE" not in body


def test_source_market_view_is_backed_by_nullable_additive_observation_columns():
    _, _, _, columns, _ = our_tables()["post_observations"][0]
    assert ("source_market", "STRING", False) in columns
    assert ("source_region", "STRING", False) in columns
    assert {("post_observations", "source_market", "STRING"),
            ("post_observations", "source_region", "STRING")} <= set(alters())


def run_suppressed_view(suppressions, creators):
    duckdb = pytest.importorskip("duckdb")
    sqlglot = pytest.importorskip("sqlglot")
    view = views_in("intelligence_42_core")[0]
    body = view.split(" AS\n", 1)[1].replace("`ogilvy-trends-v2.intelligence_42_core.", "core.")
    body = body.replace("suppressions`", "suppressions").replace("creators`", "creators")
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA core")
    con.execute("CREATE TABLE core.suppressions (suppression_id VARCHAR, status_at TIMESTAMP, status VARCHAR, "
                "creator_id VARCHAR, platform VARCHAR, handle VARCHAR, reason VARCHAR, who VARCHAR)")
    con.execute("CREATE TABLE core.creators (creator_id VARCHAR, platform VARCHAR, handle VARCHAR)")
    con.executemany("INSERT INTO core.suppressions VALUES (?, ?, ?, ?, ?, ?, 'removal request', 'albert')",
                    suppressions)
    con.executemany("INSERT INTO core.creators VALUES (?, ?, ?)", creators)
    sql = sqlglot.transpile(body, read="bigquery", write="duckdb")[0]
    return sorted(r[0] for r in con.execute(sql).fetchall())


def run_source_markets_view(rows):
    duckdb = pytest.importorskip("duckdb")
    sqlglot = pytest.importorskip("sqlglot")
    view = next(v for v in views_in("intelligence_42_core")
                if apply.statement_name(v) == SOURCE_MARKETS_VIEW)
    body = view.split(" AS\n", 1)[1].replace(
        "`ogilvy-trends-v2.intelligence_42_core.post_observations`", "core.post_observations")
    sql = sqlglot.transpile(body, read="bigquery", write="duckdb")[0]
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA core")
    con.execute("CREATE TABLE core.post_observations (post_id VARCHAR, source_market VARCHAR, "
                "source_region VARCHAR, route VARCHAR, protocol VARCHAR, observed_at TIMESTAMP, "
                "observed_date DATE)")
    con.executemany("INSERT INTO core.post_observations VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return con.execute(sql).fetchall()


def test_source_markets_view_keeps_conflicts_deduplicates_and_orders_sightings():
    rows = [
        ("p1", "ZA", "Gauteng", "feed_tiktok", "socialcrawl", "2026-09-29 12:00:00", "2026-09-29"),
        ("p1", "ZA", "Gauteng", "feed_tiktok", "socialcrawl", "2026-09-29 12:00:00", "2026-09-29"),
        ("p1", "ZA", "Western Cape", "search_tiktok", "socialcrawl", "2026-09-28 09:00:00", "2026-09-28"),
        ("p1", "NG", "Lagos", "search_tiktok", "socialcrawl", "2026-09-29 10:00:00", "2026-09-29"),
        ("p1", "ZZ", "Unknown", "search_tiktok", "socialcrawl", "2026-09-29 11:00:00", "2026-09-29"),
        ("p1", None, None, "search_tiktok", "socialcrawl", "2026-09-29 11:30:00", "2026-09-29"),
        ("p1", "KE", "Nairobi", "search_tiktok", "socialcrawl", None, "2026-09-29"),
        ("p1", "KE", "Nairobi", "search_tiktok", "socialcrawl", "2026-09-29 13:00:00", None),
        ("p2", "KE", "Nairobi", "feed_tiktok", "socialcrawl", "2026-09-27 08:00:00", "2026-09-27"),
        ("p3", None, None, "feed_tiktok", "socialcrawl", "2026-09-27 08:00:00", "2026-09-27"),
    ]
    result = run_source_markets_view(rows)
    by_post = {row[0]: row[1:] for row in result}
    assert set(by_post) == {"p1", "p2"}
    p1 = by_post["p1"]
    assert p1[0] == ["NG", "ZA"]
    sightings = p1[1]
    assert len(sightings) == 3
    assert [s["source_market"] for s in sightings] == ["ZA", "NG", "ZA"]
    assert [str(s["obs_date"]) for s in sightings] == ["2026-09-28", "2026-09-29", "2026-09-29"]
    assert sightings[0]["source_region"] == "Western Cape"
    assert p1[0] != [s["source_market"] for s in sightings]


CREATORS = [("c1", "tiktok", "@Thandi.M"), ("c2", "twitter", "kwame"), ("c3", "reddit", "u/lagos_eats"),
            ("c4", "tiktok", "someone_else")]


def test_suppressed_view_matches_by_id_and_by_platform_and_handle():
    rows = [
        ("s1", "2026-09-29 08:00:00", "suppressed", None, "TikTok", " thandi.m"),
        ("s2", "2026-09-29 08:00:00", "suppressed", None, "x", "@KWAME"),
        ("s3", "2026-09-29 08:00:00", "suppressed", None, "reddit", "lagos_eats"),
        ("s4", "2026-09-29 08:00:00", "suppressed", "c9", None, None),
    ]
    assert run_suppressed_view(rows, CREATORS) == ["c1", "c2", "c3", "c9"]


def test_suppressed_view_lifts_on_a_later_row_and_ties_stay_suppressed():
    rows = [
        ("s1", "2026-09-29 08:00:00", "suppressed", "c1", None, None),
        ("s1", "2026-09-30 08:00:00", "lifted", "c1", None, None),
        ("s2", "2026-09-29 08:00:00", "lifted", "c2", None, None),
        ("s2", "2026-09-30 08:00:00", "suppressed", "c2", None, None),
        ("s3", "2026-09-29 08:00:00", "lifted", "c3", None, None),
        ("s3", "2026-09-29 08:00:00", "suppressed", "c3", None, None),
        ("s4", "2026-09-29 08:00:00", "Suppressed ", "c4", None, None),
    ]
    # c1 lifted later; c2 suppressed again; c3 tie goes to suppressed; any status but lifted suppresses
    assert run_suppressed_view(rows, CREATORS) == ["c2", "c3", "c4"]


def test_runs_stages_listed_in_data_md():
    agent_line = re.search(r"^- In intelligence_42_agent:.*$", doc_section_1(), re.M).group(0)
    stages = re.search(r"\(run_id, stage: ([^,]*),", agent_line).group(1).split(" | ")
    for stage in ("learn", "investigation", "scheduled", "ask", "backtest", "pulse", "breaking"):
        assert stage in stages, stage


def test_runs_carries_nullable_ask_payloads():
    _, _, _, columns, _ = our_tables()["runs"][0]
    assert ("answer", "JSON", False) in columns
    assert ("record", "JSON", False) in columns


# Tests: apply.py with a fake client

class FakeJob:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def result(self):
        return self.rows


class FakeClient:
    """live_views maps a view's name to the SQL it already has in BigQuery. A view not named there is taken to
    be identical to the file's, so a test that does not care about drift sees none."""

    def __init__(self, missing=(), fail_on=None, absent=(), live_views=None):
        self.calls = []
        self.missing = set(missing)
        self.absent = set(absent)
        self.fail_on = fail_on
        self.created = {}
        self.live_views = dict(live_views or {})
        self.file_views = {apply.statement_name(st).split(".")[2]: apply.view_body(st)
                           for st in apply.load_statements() if apply.kind(st) == "view"}

    def get_table(self, ref):
        self.calls.append(("get_table", ref, None))
        _, dataset, table = ref.split(".")
        created = table in self.created.get(dataset, {})
        if not created and (dataset in self.missing or table in self.absent):
            raise apply.NotFound(ref)
        return types.SimpleNamespace(view_query=self.live_views.get(table, self.file_views.get(table)))

    def get_dataset(self, ref):
        self.calls.append(("get_dataset", ref, None))
        if ref.split(".")[-1] in self.missing:
            raise apply.NotFound(ref)
        return object()

    def query(self, sql, job_config=None, location=None):
        dry = bool(job_config is not None and job_config.dry_run)
        self.calls.append(("query", sql, dry, location))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("Syntax error: boom")
        m = re.search(r"CREATE (TABLE|VIEW) IF NOT EXISTS `[\w-]+\.(\w+)\.(\w+)`", sql)
        if m and not dry:
            kind = "BASE TABLE" if m.group(1) == "TABLE" else "VIEW"
            self.created.setdefault(m.group(2), {})[m.group(3)] = kind
            if kind == "VIEW":
                # IF NOT EXISTS: BigQuery keeps a view that is already there and accepts the statement.
                self.live_views.setdefault(m.group(3), apply.view_body(sql))
        m = re.search(r"`[\w-]+\.(\w+)\.INFORMATION_SCHEMA\.TABLES`", sql)
        if m:
            made = self.created.get(m.group(1), {})
            return FakeJob({"table_name": t, "table_type": made[t]} for t in sorted(made))
        return FakeJob()


def queries(client):
    return [c for c in client.calls if c[0] == "query"]


def test_default_mode_issues_only_dry_runs():
    client = FakeClient(missing={"intelligence_42_core", "intelligence_42_agent"})
    stmts = apply.load_statements()
    assert apply.run(client, stmts) == 0
    q = queries(client)
    assert len(q) == sum(1 for s in stmts if apply.is_schema(s))
    assert all(dry for _, _, dry, _ in q)
    assert all(loc == "US" for _, _, _, loc in q)


def test_default_mode_skips_tables_of_a_dataset_not_yet_created(capsys):
    client = FakeClient(missing={"intelligence_42_agent"})
    stmts = apply.load_statements()
    assert apply.run(client, stmts) == 0
    q = queries(client)
    assert all(dry for _, _, dry, _ in q)
    assert not any("intelligence_42_agent.runs" in sql for _, sql, _, _ in q)
    assert "intelligence_42_agent" in capsys.readouterr().out


def test_dry_run_stops_on_first_failure_and_names_the_table(capsys):
    client = FakeClient(fail_on="intelligence_42_core.posts`")
    stmts = apply.load_statements()
    assert apply.run(client, stmts, apply=True) != 0
    out = capsys.readouterr().out
    assert "ogilvy-trends-v2.intelligence_42_core.posts" in out and "boom" in out
    q = queries(client)
    assert "intelligence_42_core.posts`" in q[-1][1]
    assert not any(not dry and "CREATE TABLE" in sql for _, sql, dry, _ in q)


def test_existing_datasets_are_never_recreated():
    # bootstrap.py creates the datasets and f42-builder has no datasets.create permission
    client = FakeClient()
    stmts = apply.load_statements()
    assert apply.run(client, stmts, apply=True) == 0
    q = queries(client)
    assert not any("CREATE SCHEMA" in sql for _, sql, _, _ in q)
    tables = [s for s in stmts if apply.kind(s) == "table"]
    assert sum(1 for _, sql, dry, _ in q if not dry and "CREATE TABLE" in sql) == len(tables)
    later = [s for s in stmts if apply.kind(s) in ("alter", "view")]
    assert later
    for statement in later:
        assert [(sql, dry) for _, sql, dry, _ in q].count((statement, False)) == 1


def test_apply_mode_dry_runs_before_creating_and_lists_tables(capsys):
    client = FakeClient(missing={"intelligence_42_core", "intelligence_42_agent"})
    stmts = apply.load_statements()
    assert apply.run(client, stmts, apply=True) == 0
    q = queries(client)
    creates = [(sql, dry) for _, sql, dry, _ in q if "INFORMATION_SCHEMA" not in sql]
    assert len(creates) == 2 * len(stmts)
    for statement in stmts:
        dry_index = creates.index((statement, True))
        real_index = creates.index((statement, False))
        assert dry_index < real_index
    first_table_create = min(i for i, (s, d) in enumerate(creates)
                             if not d and apply.kind(s) == "table")
    last_table_dry = max(i for i, (s, d) in enumerate(creates) if d and apply.kind(s) == "table")
    assert last_table_dry < first_table_create
    # ALTER and VIEW statements need their tables, so they are dry-run once every table exists
    last_table_create = max(i for i, (s, d) in enumerate(creates)
                            if not d and apply.kind(s) == "table")
    later = [(i, d) for i, (s, d) in enumerate(creates) if apply.kind(s) in ("alter", "view")]
    last_later_dry = max(i for i, d in later if d)
    first_later_dry = min(i for i, d in later if d)
    first_later_run = min(i for i, d in later if not d)
    assert last_table_create < first_later_dry <= last_later_dry < first_later_run
    assert all(loc == "US" for _, _, _, loc in q)
    out = capsys.readouterr().out
    expected = doc_tables()
    for dataset in SQL_FILES:
        n = sum(1 for d in expected.values() if d == dataset)
        v = sum(1 for s in stmts if apply.kind(s) == "view" and apply.dataset_of(s) == dataset)
        assert f"{dataset}: {n} tables (expected {n}), {v} views (expected {v})" in out
    assert "missing" not in out


def test_default_mode_dry_runs_alters_and_skips_views_whose_table_is_absent(capsys):
    client = FakeClient(absent={"watches"})
    stmts = apply.load_statements()
    assert apply.run(client, stmts) == 0
    q = queries(client)
    assert all(dry for _, _, dry, _ in q)
    sent = [sql for _, sql, _, _ in q]
    for statement in stmts:
        if apply.kind(statement) == "table" or (
                apply.kind(statement) == "alter" and "intelligence_42_agent.watches`" not in statement):
            assert statement in sent, statement[:60]
    assert not any("intelligence_42_agent.watches`" in sql
                   for sql in sent if apply.kind(sql) != "table")
    assert not any("v_watches_current" in sql for sql in sent)
    out = capsys.readouterr().out
    assert "skipped 2 dry runs" in out and "v_watches_current" in out
    assert "dry run only; nothing created" in out


def test_default_mode_dry_runs_views_once_their_table_exists():
    client = FakeClient()
    stmts = apply.load_statements()
    assert apply.run(client, stmts) == 0
    sent = [sql for _, sql, dry, _ in queries(client) if dry]
    for statement in stmts:
        if not apply.is_schema(statement):
            assert statement in sent, statement[:60]


def test_list_tables_flags_a_missing_view(capsys):
    client = FakeClient()
    stmts = [s for s in apply.load_statements() if not apply.is_schema(s)]
    for s in stmts:
        if apply.kind(s) == "table":
            client.query(s)
    assert not apply.list_tables(client, stmts)
    out = capsys.readouterr().out
    assert "intelligence_42_agent: 16 tables (expected 16), 0 views (expected 1)" in out
    assert "missing: v_watches_current" in out
    assert "intelligence_42_core: 27 tables (expected 27), 0 views (expected 3)" in out
    assert "missing: v_breaking_signals_current, v_post_source_markets, v_suppressed_creators" in out


def test_apply_module_never_touches_credentials():
    text = (SCHEMA_DIR / "apply.py").read_text(encoding="utf-8").lower()
    for word in ("credential", "token", "secret"):
        assert word not in text, word


# N54: CREATE VIEW IF NOT EXISTS leaves an existing view as it is, so an edited view must be reported, not "ok".

def edited_view_client(**kw):
    return FakeClient(live_views={"v_suppressed_creators": "SELECT creator_id FROM old_table"}, **kw)


def test_apply_reports_a_view_whose_live_sql_differs_from_the_file_and_exits_1(capsys):
    client = edited_view_client()
    assert apply.run(client, apply.load_statements(), apply=True) == 1
    out = capsys.readouterr().out
    assert "v_suppressed_creators" in out and "differs" in out
    assert not [ln for ln in out.splitlines() if ln.startswith("run ok: ") and ln.endswith("v_suppressed_creators")]
    assert out.count("view differs") == 1
    assert [sql for _, sql, dry, _ in queries(client) if "CREATE OR REPLACE" in sql.upper()] == []


def test_apply_exits_0_and_names_no_drift_when_every_live_view_matches_the_file(capsys):
    assert apply.run(FakeClient(), apply.load_statements(), apply=True) == 0
    assert "view differs" not in capsys.readouterr().out


def test_a_whitespace_only_difference_is_not_drift(capsys):
    body = next(apply.view_body(st) for st in apply.load_statements()
                if apply.statement_name(st).endswith("v_suppressed_creators"))
    client = FakeClient(live_views={"v_suppressed_creators": body.replace(" ", "\n   ")})
    assert apply.run(client, apply.load_statements(), apply=True) == 0
    assert "view differs" not in capsys.readouterr().out


def test_the_dry_run_also_reports_an_edited_view_that_already_exists(capsys):
    assert apply.run(edited_view_client(), apply.load_statements()) == 1
    out = capsys.readouterr().out
    assert "view differs" in out and "v_suppressed_creators" in out
    assert "dry run only; nothing created" in out


def test_a_view_that_does_not_exist_yet_is_not_drift(capsys):
    client = FakeClient(absent={"v_suppressed_creators"})
    assert apply.run(client, apply.load_statements()) == 0
    assert "view differs" not in capsys.readouterr().out


def test_a_view_that_already_existed_is_not_reported_as_run_ok(capsys):
    client = FakeClient(live_views={"v_suppressed_creators": "SELECT creator_id FROM old_table"},
                        absent={"v_post_source_markets"})
    apply.run(client, apply.load_statements(), apply=True)
    out = capsys.readouterr().out
    assert not [ln for ln in out.splitlines() if ln.startswith("run ok: ") and ln.endswith("v_suppressed_creators")]
    assert "left unchanged" in out
