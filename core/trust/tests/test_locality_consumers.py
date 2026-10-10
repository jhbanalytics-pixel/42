"""L-14 (C4 v3 section 11.3): no v1 locality value on an admission path once locality_v2 is authoritative.

The v1 values are detect's geo_status and local_share, the brief's pack scope (market_scope, market_posts7) and the
kept observation eligible_v1. This scan lists every place under core/ (tests excluded) whose code or SQL reads one of
them, as the function or named statement that reads it, and fails when a read is not on the list below. A new read in
gate.py, in the brief job's admission functions or in the candidate ordering therefore fails here until someone has
looked at it and put it on the list with a reason.

The list has two parts. ALLOWED names a reader that is a v1 producer, an observation, a display of the pack, or a site
whose own text keeps v2 in front (the basis test is checked below). PENDING names readers that the switch must still
change and that belong to other lanes (Discover, Radar, Ask, ops). PENDING is not a licence: each entry says what the
switch needs there, and the test fails when an entry stops matching so the list cannot rot.

Python is read with the ast module (names, attributes and string constants, docstrings left out); SQL with comments
stripped. A read is attributed to the innermost function, or to the named statement of the SQL file."""

import ast
import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[2]
V1_NAMES = ("geo_status", "local_share", "market_scope", "market_posts7", "eligible_v1")
WORD = re.compile(r"\b(" + "|".join(V1_NAMES) + r")\b")
NAME_LINE = re.compile(r"^--\s*name:\s*(\w+)", re.MULTILINE)

ALLOWED = {
    # The pack scope, V1-B: computed here, kept as an observation (pack_scope_v1), never an admission input after the switch.
    "brief/market_scope.py::read_market_scope": "V1-B producer",
    "brief/market_scope.py::<module>": "V1-B producer, its SQL text",
    "brief/sql/market_scope.sql::market_scope": "V1-B producer",
    "brief/whatif_g1.py::judge": "what-if report of the G1 hold, read only",
    "detect/sql/views.sql::<file>": "v_item_market_scope, the V1-B view",
    # The brief job: market_scope on the candidate row is the v2-derived scope for a row on the v2 basis (_prepare), and
    # the pack scope for any other row; the pack figures are kept as pack_scope_v1.
    "brief/job.py::_prepare": "derives market_scope from the retained row for the v2 basis and keeps the pack as pack_scope_v1",
    "brief/job.py::_for_today": "reads the candidate's market_scope, v2-derived on the v2 basis",
    "brief/job.py::_floor_held": "reads the candidate's market_scope, v2-derived on the v2 basis",
    "brief/job.py::_market_payload": "reads the candidate's market_scope, v2-derived on the v2 basis",
    "brief/job.py::_brief": "the locality_shadow observation list: pack scope beside the v2 status",
    "brief/job.py::_selection_snapshot": "audit of the scope the candidate statement derived",
    "brief/job.py::_selection_result": "audit of the scope the candidate statement derived",
    "brief/job.py::<module>": "the QUERIES text of brief.sql statements and the locality audit constants",
    "brief/payload.py::_card": "card fields; market_scope_basis says which rule wrote market_scope",
    "brief/payload.py::_flag": "geo_status flags a card only when its scope did not come from locality_v2.1",
    "brief/locality_audit.py::build_locality_audit": "local_share of the retained v2 row",
    "brief/sql/brief.sql::candidates": "ordering: the pack scope and local_first only for a row not on the v2 basis",
    "brief/sql/brief.sql::not_local_audit": "local_share of the retained v2 row",
    "trust/gate.py::_pack_scope": "G6 as written for v1, used only for a card not on the v2 basis",
    "trust/locality.py::read_locality": "local_share of the retained v2 row",
    "trust/locality.py::locality_block": "local_share of the retained v2 row",
    # W8-DEC-17 on the v1 basis (trust-tighten): the label of a row whose item_state geo_status is the card's source.
    # brief/payload.py _flag calls it only for a row whose scope did not come from locality_v2.1.
    "trust/locality.py::geo_status": "the v1 basis label: reads geo_status and local_share of a row on the v1 basis",
    "trust/locality.py::local_posts": "the v1 basis label: the whole count of local posts a v1 row's share gives back",
    # The rival window (intel-rivals) shows item_state's local_share as one descriptor number of the trend, beside the
    # card and never as an admission input.
    "brief/evidence.py::<module>": "rival_state descriptor: item_state local_share shown as an observation",
    "brief/sql/evidence.sql::rival_state": "rival_state descriptor: item_state local_share shown as an observation",
    # Detect: the producer of geo_status and local_share, the shadow comparison, and the one place a row's basis decides.
    "detect/sql/state.sql::<file>": "producer of geo_status and local_share; eligible follows @authority; eligible_v1 kept",
    "detect/sql/watches.sql::items": "geo_status line applies only to a row not on the v2 basis",
    "detect/locality.py::<module>": "shadow cross-tabulation text and the v2 reader's SQL",
    "detect/sql/locality_summary.sql::<file>": "local_share of the retained v2 row",
    "detect/sql/locality_views.sql::<file>": "local_share of the retained v2 row",
    "schema/core.sql::<file>": "column definitions",
    "schema/locality_switch.sql::<file>": "the column definitions of the switch release only",
    "detect/job.py::<module>": "state_script: the text that drops the switch columns from the INSERT under v1",
    "detect/sqlrun.py::<module>": "the v1 substitution (for_authority): names eligible_v1 among the three switch columns to replace its reads with a typed NULL, and reads no value",
    "collect/probe_report.py::main": "a probe report, read only",
}

PENDING = {
    "api/discover.py::_market_posts_all_news": "lane 2 (L-23): W8-DEC-12 reads the pack scope's news count; the switch needs the retained row",
    "api/store.py::_item_states": "lane 2: the pack scope and its news column for a v1 row; the v2 basis reads the retained row",
    "api/discover.py::_build_card": "lane 2 (L-23): scope from the retained row, v1 fields kept and clamped",
    "api/discover.py::_flag": "lane 2 (L-23): flag from the retained row for a v2-basis row",
    "api/discover.py::_held": "lane 2 (L-23): held reason from the retained row for a v2-basis row",
    "api/store.py::detect_states": "lane 2 (L-24): Radar reach from the retained row",
    "api/store.py::item_states": "lane 2: scope from the retained row for a v2-basis row",
    "api/today.py::_not_assessed": "audit entries carry the candidate's scope; the locality_audit block is separate",
    "agent/tools/sql_query.py::<module>": "lane 1: Ask allow-list, v_item_locality_current under its own name",
    "agent/tools/warehouse.py::rising_topics": "lane 1: Ask names the v1 figure under its v1 label",
}


def _strings(tree):
    """Docstrings of modules, classes and functions, so they can be left out of the scan."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
    return skip


def python_reads(source):
    """{function name or <module>: names read} for one Python source. A read is a Name, an Attribute or a string
    constant that contains the name as a word, outside docstrings."""
    tree = ast.parse(source)
    skip = _strings(tree)
    spans = [(n.lineno, n.end_lineno, n.name) for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def where(line):
        inner = [s for s in spans if s[0] <= line <= s[1]]
        return min(inner, key=lambda s: s[1] - s[0])[2] if inner else "<module>"

    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            texts = [node.id]
        elif isinstance(node, ast.Attribute):
            texts = [node.attr]
        elif isinstance(node, ast.keyword) and node.arg:
            texts = [node.arg]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            texts = [node.value]
        else:
            continue
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        for text in texts:
            for m in WORD.finditer(text):
                found.setdefault(where(line), set()).add(m.group(1))
    return found


def sql_reads(source):
    """{named statement or <file>: names read} for one SQL file, comments stripped."""
    stripped, names = [], []
    for line in source.splitlines():
        m = NAME_LINE.match(line)
        if m:
            names.append((len(stripped), m.group(1)))
        stripped.append(re.sub(r"--.*$", "", line))
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), "\n".join(stripped), flags=re.S)
    found = {}
    for i, line in enumerate(text.splitlines()):
        for m in WORD.finditer(line):
            current = "<file>"
            for start, name in names:
                if start <= i:
                    current = name
            found.setdefault(current, set()).add(m.group(1))
    return found


def repository_reads():
    sites = {}
    for path in sorted(CORE.rglob("*")):
        rel = path.relative_to(CORE)
        if path.suffix not in (".py", ".sql") or "tests" in rel.parts or rel.parts[0] == "eval":
            continue
        source = path.read_text(encoding="utf-8")
        for where, names in (python_reads(source) if path.suffix == ".py" else sql_reads(source)).items():
            sites[f"{rel.as_posix()}::{where}"] = names
    return sites


def test_every_read_of_a_v1_locality_value_is_on_the_list():
    unlisted = sorted(set(repository_reads()) - set(ALLOWED) - set(PENDING))
    assert not unlisted, f"a v1 locality value is read at a site that is not on the L-14 list: {unlisted}"


def test_no_entry_of_the_list_is_stale():
    seen = set(repository_reads())
    stale = sorted((set(ALLOWED) | set(PENDING)) - seen)
    assert not stale, f"entries of the L-14 list that read none of the names any more: {stale}"


def test_an_entry_is_either_allowed_or_pending_never_both():
    assert not set(ALLOWED) & set(PENDING)


def test_the_gate_reads_the_pack_only_in_the_function_a_v1_card_is_sent_to():
    gate = {k for k in repository_reads() if k.startswith("trust/gate.py::")}
    assert gate == {"trust/gate.py::_pack_scope"}


def test_the_state_statement_reads_geo_status_for_admission_only_in_the_branch_that_is_not_v2():
    text = (CORE / "detect/sql/state.sql").read_text(encoding="utf-8")
    code = "\n".join(re.sub(r"--.*$", "", line) for line in text.splitlines())
    admissions = re.findall(r"[^\n]*\bgeo_status\s*!=\s*'not_local'[^\n]*", code)
    assert len(admissions) == 2
    first, second = admissions
    assert first.strip().startswith("AND IF(@authority = 'v2'") and first.rstrip().endswith("s.geo_status != 'not_local')")
    assert "eligible_v1" in code[code.index(second):code.index(second) + 200]


def test_the_watch_statement_reads_geo_status_only_behind_the_basis_test():
    text = (CORE / "detect/sql/watches.sql").read_text(encoding="utf-8")
    code = "\n".join(re.sub(r"--.*$", "", line) for line in text.splitlines())
    lines = [line for line in code.splitlines() if "geo_status" in line]
    assert len(lines) == 1 and "IF(s.locality_basis = 'locality_v2.1', TRUE," in lines[0]


# The scanner itself: a scan that finds nothing is no evidence, so it is run on text it must flag and text it must not.


def test_the_python_scan_flags_a_read_in_a_function_and_a_string_key_and_ignores_a_docstring_and_a_comment():
    source = '''
"""geo_status in a module docstring"""
def admit(row):
    """local_share in a docstring"""
    # market_scope in a comment
    return row.get("geo_status") == "local" and row["market_posts7"] > 1

def other(row):
    return row.eligible_v1
'''
    assert python_reads(source) == {"admit": {"geo_status", "market_posts7"}, "other": {"eligible_v1"}}


def test_the_sql_scan_attributes_a_read_to_its_named_statement_and_skips_comments():
    source = ("-- name: a\nSELECT 1 -- geo_status in a comment\n;\n-- name: b\nSELECT s.local_share FROM t s;\n"
              "/* market_scope in a block */\nSELECT 2;\n")
    assert sql_reads(source) == {"b": {"local_share"}}
