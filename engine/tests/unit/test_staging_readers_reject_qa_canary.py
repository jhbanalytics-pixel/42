"""Ordinary staging readers refuse or exclude rows carrying the QA canary identity."""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import daily_certification, live_quality, persistence
from src.analysis.open_intelligence.run_receipts import (
    QA_CLIENT_SCOPE_ID,
    RUN_RECEIPT_CONTRACT_VERSION,
    build_run_receipt,
    receipt_qualifies_for_display,
)

ENGINE = Path(__file__).resolve().parents[2]
DESK_VIEW = ENGINE / "infra" / "bigquery_views" / "v_desk_dynamic_signals_v2.sql"
RELEASE_SCRIPT = ENGINE / "scripts" / "staging" / "release_open_intelligence_run.py"
SEMANTIC_CANARY = ENGINE / "scripts" / "staging" / "run_42_semantic_canaries.py"
EXCLUSION = "client_scope_id != 'qa_canary'"
RUN_DATE = date(2026, 9, 14)


def _script(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _release():
    """The release script through the package path daily_certification itself uses."""
    return importlib.import_module("scripts.staging.release_open_intelligence_run")


class _Job:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.statement_type = "SELECT"

    def result(self, max_results=None, **_):
        rows = self._rows if max_results is None else self._rows[:max_results]
        return iter(rows)


class _QaStore:
    """A warehouse fake holding only QA rows; it honours the exclusion when the SQL names it."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    def query(self, sql: str, **_):
        self.sql.append(sql)
        if EXCLUSION in sql:
            return _Job([row for row in self.rows if row["client_scope_id"] != QA_CLIENT_SCOPE_ID])
        return _Job(list(self.rows))


def _qa_rows() -> list[dict]:
    runner = _script(SEMANTIC_CANARY)
    rows = []
    for case in runner.CASES:
        rows.extend(dict(row) for row in runner.case_rows(RUN_DATE, case)["evidence"])
    assert rows
    assert all(row["client_scope_id"] == QA_CLIENT_SCOPE_ID for row in rows)
    return rows


def _qa_receipt():
    return build_run_receipt(
        run_contract_version=RUN_RECEIPT_CONTRACT_VERSION,
        run_id="oi_semantic_canary_v1_2026-09-14_coherent",
        client_scope_id=QA_CLIENT_SCOPE_ID,
        market_scope=("za",),
        signal_date=RUN_DATE,
        observation_start=date(2026, 9, 8),
        observation_end=RUN_DATE,
        observation_method="dynamic_signal_identity_v1",
        source_window_digest="a" * 64,
        cluster_build_version="hybrid_graph_v2",
        source_family_map_version="staging_fixture_source_map_v1",
        rule_version="staging_fixture_canary_rules_v1",
        status="completed",
        complete_partitions=True,
        display_release_state="enabled",
        candidate_count=1,
        evidence_count=2,
        membership_count=1,
        lineage_count=1,
        analysis_count=1,
        prediction_count=1,
        row_set_digest="b" * 64,
        source_sha="769408fc55680ca9d920a1e94dacaddba2c0bb91",
        completed_at=datetime(2026, 9, 15, 6, 30, tzinfo=UTC),
    )


def test_the_display_gate_never_qualifies_a_qa_receipt() -> None:
    receipt = _qa_receipt()
    assert receipt.display_release_state == "enabled"
    assert (
        receipt_qualifies_for_display(receipt, requested_markets=("za",), today=date(2026, 9, 16))
        is False
    )


def test_the_desk_view_excludes_qa_rows_from_every_relation_it_reads() -> None:
    sql = DESK_VIEW.read_text(encoding="utf-8")
    assert sql.count(EXCLUSION) >= 4
    for relation in (
        "open_intelligence_run_receipts_v1",
        "signal_candidates_v2",
        "signal_membership_v2",
    ):
        assert relation in sql


def test_the_desk_producer_and_review_digest_sql_carry_the_exclusion() -> None:
    admission = persistence._completed_provenance_runs_sql(
        project=persistence.TARGET_PROJECT, dataset=persistence.TARGET_DATASET
    )
    assert admission.count(EXCLUSION) >= 3
    digest = live_quality.review_packet_content_digest_sql("run_fixture_001", sql_expression=False)
    assert EXCLUSION in digest


def test_the_release_reader_refuses_a_run_whose_only_rows_are_qa() -> None:
    release = _release()
    store = _QaStore(_qa_rows())
    with pytest.raises(release.ReleaseRefusal, match="review packet authority is unavailable"):
        release._read_review_packet(store)
    assert len(store.sql) == 1
    assert EXCLUSION in store.sql[0]


def test_the_certification_reader_excludes_qa_rows_through_the_same_seam() -> None:
    release = _release()
    store = _QaStore(_qa_rows())
    certifier = SimpleNamespace(_warehouse=store, _release=lambda: release)
    rows = daily_certification.NativeCertifier._rows(
        certifier,
        "signal_evidence_v2",
        ("client_scope_id", "run_id", "evidence_id"),
        "oi_semantic_canary_v1_2026-09-14_coherent",
        "evidence_id",
    )
    assert rows == []
    assert len(store.sql) == 1
    assert EXCLUSION in store.sql[0]


def test_every_run_scoped_row_reader_in_the_release_script_names_the_exclusion() -> None:
    """Row-family readers exclude QA by predicate; the receipt reader is gated by identity.

    A receipt is read by its own run id and a QA receipt never qualifies for display
    (test_the_display_gate_never_qualifies_a_qa_receipt), so the receipt reader needs
    no predicate. Every other run-scoped reader must carry it.
    """
    source = RELEASE_SCRIPT.read_text(encoding="utf-8")
    readers = []
    for match in re.finditer(r"WHERE run_id = @run_id([^\n]*)", source):
        preceding = source[max(0, match.start() - 300) : match.start()]
        tables = re.findall(r"\{(\w+_TABLE)\}`", preceding)
        readers.append((tables[-1] if tables else "unknown", match.group(1)))
    assert readers, "the release script no longer reads run-scoped rows"
    assert {table for table, _ in readers} >= {"EVIDENCE_TABLE", "RECEIPT_TABLE"}
    missing = [
        table for table, clause in readers if table != "RECEIPT_TABLE" and EXCLUSION not in clause
    ]
    assert missing == [], missing
