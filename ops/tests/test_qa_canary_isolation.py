"""Synthetic QA canary identities never reach ordinary readers or artifact fixtures."""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
RUNNER = ENGINE / "scripts" / "staging" / "run_42_semantic_canaries.py"
QA_DATASET = "trends_v2_staging_qa"
QA_DATA_MARKERS = ("qa_canary", "oi_semantic_canary")
ORDINARY_READER_ROOTS = (
    "app/src",
    "app/frontend/src",
    "engine/src",
    "engine/infra/bigquery_views",
    "engine/infra/bigquery_routines",
)
IDENTITY_ROLES = (
    ("sig", "signal"),
    ("sig", "prior_signal"),
    ("ev", "evidence"),
    ("ev", "first"),
    ("ev", "second"),
    ("mem", "member"),
    ("pred", "prediction"),
    ("out", "outcome"),
)
SCANNED_SUFFIXES = {
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".csv",
    ".sql",
    ".txt",
    ".html",
    ".js",
    ".mjs",
}
_IDENTIFIER = re.compile(r"\b(?:sig|ev|mem|pred|out)_[0-9a-f]{64}\b")

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))


def _runner():
    spec = importlib.util.spec_from_file_location("semantic_canaries_isolation", RUNNER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_files() -> list[Path]:
    directories = [
        path
        for path in ROOT.rglob("fixtures")
        if path.is_dir()
        and "node_modules" not in path.parts
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
    ]
    assert {path.relative_to(ROOT).as_posix() for path in directories} >= {
        "engine/tests/fixtures",
        "app/tests/fixtures",
        "ops/tests/fixtures",
    }
    return sorted(
        path
        for directory in directories
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in SCANNED_SUFFIXES
    )


def _qa_identifiers(module, first: date, last: date) -> set[str]:
    identifiers = set()
    day = first
    while day <= last:
        for case in module.CASES:
            for prefix, role in IDENTITY_ROLES:
                identifiers.add(module.qa_identifier(prefix, case, role, day))
        day += timedelta(days=1)
    return identifiers


def test_the_identity_roles_cover_every_identifier_a_case_row_carries() -> None:
    module = _runner()
    run_date = date(2026, 9, 14)
    expected = _qa_identifiers(module, run_date, run_date)
    carried = {
        value
        for case in module.CASES
        for rows in module.case_rows(run_date, case).values()
        for row in rows
        for value in row.values()
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value)
    }
    assert carried
    assert carried <= expected


def test_synthetic_qa_identities_are_absent_from_every_artifact_fixture() -> None:
    module = _runner()
    identifiers = _qa_identifiers(module, date(2026, 1, 1), date(2027, 12, 31))
    hits = []
    for path in _fixture_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in QA_DATA_MARKERS:
            if marker in text:
                hits.append((path.relative_to(ROOT).as_posix(), marker))
        for token in set(_IDENTIFIER.findall(text)) & identifiers:
            hits.append((path.relative_to(ROOT).as_posix(), token))
    assert hits == []


def test_no_ordinary_reader_names_the_qa_dataset() -> None:
    hits = []
    for root in ORDINARY_READER_ROOTS:
        for path in (ROOT / root).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or "node_modules" in path.parts:
                continue
            if path.suffix not in {".py", ".sql", ".js", ".jsx", ".mjs"}:
                continue
            if QA_DATASET in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(path.relative_to(ROOT).as_posix())
    assert hits == []


def test_every_ordinary_view_over_scoped_rows_excludes_the_qa_scope() -> None:
    views = sorted((ENGINE / "infra" / "bigquery_views").glob("*.sql"))
    scoped = [path for path in views if "client_scope_id" in path.read_text(encoding="utf-8")]
    assert scoped
    for path in scoped:
        assert "client_scope_id != 'qa_canary'" in path.read_text(encoding="utf-8"), path.name


def test_the_qa_results_table_is_created_only_in_the_qa_dataset() -> None:
    module = _runner()
    assert module.QA_DATASET == QA_DATASET
    assert module.QA_RESULTS_TABLE == "canary_results_v2"
    ddl = (ENGINE / "infra" / "bigquery_schemas" / "canary_results_v2.sql").read_text(
        encoding="utf-8"
    )
    assert "partition_expiration_days = 90" in ddl
    assert module.QA_RETENTION_DAYS == 90
