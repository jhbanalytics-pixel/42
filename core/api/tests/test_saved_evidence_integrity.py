import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.agent import ask, checks
from core.agent.context import RunContext, result_hash
from core.api import agent_app, dossiers


def test_deselected_inference_cannot_survive_in_dossier_summary_or_export():
    record = json.loads((Path(__file__).parents[1] / "fixtures/ask_complete.json").read_text(encoding="utf-8"))
    excluded = "The format spreads because it needs no props."
    record["answer"]["claims"][1]["text"] = excluded
    record["answer"]["short_answer"] = excluded
    selection = dossiers.selection({"keep": ["c1"]}, record, None)
    draft = dossiers.build(record, selection, dossier_id="d_integrity", version=1, created_at="2026-10-07",
                           source={"ask_id": record["ask_id"]})
    assert dossiers.needs_tick(draft, {}) == []
    assert not draft["summary"]
    frozen = dossiers.freeze(draft, version=2, created_at="2026-10-07", ticks={})
    assert excluded not in dossiers.render_html(frozen)


def late_receipt(value=499):
    ctx = RunContext(run_id="r_integrity", tier="T1", as_of=datetime(2026, 10, 7, tzinfo=timezone.utc))
    rows = [{"cohort": f"cohort_{i}", "posts": i} for i in range(500)]
    rows[499]["posts"] = value
    qid, digest = ctx.record_query("SELECT cohort, posts FROM counts", {}, rows, "cohort post counts")
    number = {"value": float(value), "query_id": qid, "result_hash": digest, "run_id": ctx.run_id}
    assert checks._number_problem(number, ctx, SimpleNamespace(run=lambda *args: copy.deepcopy(rows)), {}) is None
    return ctx, {"claims": [{"id": "c1", "numbers": [number]}]}, qid


def test_saved_receipt_keeps_the_checked_number_beyond_preview_rows():
    ctx, answer, qid = late_receipt()
    receipt = ask.query_receipts(ctx, answer)[qid]
    assert {"cohort": "cohort_499", "posts": 499} in receipt["rows"]
    assert receipt["row_indexes"][0] == 499
    assert receipt["supporting_rows"] == 1
    assert receipt["row_count"] == 500 and len(receipt["rows"]) <= ask.RECEIPT_ROWS
    assert receipt["result_hash"] == answer["claims"][0]["numbers"][0]["result_hash"]


def test_supporting_decimal_keeps_its_exact_literal_and_type_in_saved_receipt():
    literal = "599.250000000000000000000000000000001"
    ctx, answer, qid = late_receipt(Decimal(literal))
    receipt = ask.query_receipts(ctx, answer)[qid]
    assert receipt["rows"][0] == {"cohort": "cohort_499", "posts": literal}
    assert receipt["cell_types"] == [{"row_index": 499, "path": ["posts"], "type": "decimal"}]
    assert receipt_record(receipt)["rows"][0]["posts"] == literal


@pytest.mark.parametrize("literal", ["12.30", "9007199254740993", "1.234567890123456789123456789"])
def test_a_complete_decimal_receipt_reconstructs_the_original_result_hash(literal):
    ctx = RunContext(run_id="r_integrity", tier="T1", as_of=datetime(2026, 10, 7, tzinfo=timezone.utc))
    qid, digest = ctx.record_query("SELECT n FROM counts", {}, [{"n": Decimal(literal)}], "count")
    number = {"value": float(literal), "query_id": qid, "result_hash": digest, "run_id": ctx.run_id}
    receipt = ask.query_receipts(ctx, {"claims": [{"numbers": [number]}]})[qid]
    saved = receipt_record(receipt)
    assert saved["rows"] == [{"n": literal}]
    assert saved["cell_types"] == [{"row_index": 0, "path": ["n"], "type": "decimal"}]
    assert result_hash(saved["rows"]) == digest
    assert result_hash([{"n": Decimal(saved["rows"][0]["n"])}]) == digest


def test_nested_decimal_cells_keep_their_path_and_exact_literal():
    ctx = RunContext(run_id="r_integrity", tier="T1", as_of=datetime(2026, 10, 7, tzinfo=timezone.utc))
    rows = [{"items": [{"count": Decimal("12.300")}, "12.300"]}]
    qid, digest = ctx.record_query("SELECT items FROM counts", {}, rows, "nested counts")
    receipt = ask.query_receipts(ctx)[qid]
    saved = receipt_record(receipt)
    assert saved["rows"] == [{"items": [{"count": "12.300"}, "12.300"]}]
    assert saved["cell_types"] == [{"row_index": 0, "path": ["items", 0, "count"], "type": "decimal"}]
    assert result_hash(saved["rows"]) == digest


def test_reordered_decimal_support_reconstructs_the_complete_original_query():
    ctx = RunContext(run_id="r_integrity", tier="T1", as_of=datetime(2026, 10, 7, tzinfo=timezone.utc))
    rows = [{"cohort": "preview", "n": Decimal("1.20")}, {"cohort": "cited", "n": Decimal("12.30")}]
    qid, digest = ctx.record_query("SELECT cohort, n FROM counts", {}, rows, "cohort counts")
    number = {"value": 12.3, "query_id": qid, "result_hash": digest, "run_id": ctx.run_id}
    receipt = ask.query_receipts(ctx, {"claims": [{"numbers": [number]}]})[qid]
    saved = receipt_record(receipt)
    assert saved["row_indexes"] == [1, 0]
    restored = [row for _, row in sorted(zip(saved["row_indexes"], saved["rows"]))]
    assert restored == [{"cohort": "preview", "n": "1.20"}, {"cohort": "cited", "n": "12.30"}]
    assert result_hash(restored) == digest


def test_matching_numeric_strings_are_not_used_as_receipt_support():
    ctx, answer, qid = late_receipt()
    ctx.queries[qid]["rows"][0] = {"cohort": "cohort_0", "posts": "499"}
    digest = result_hash(ctx.queries[qid]["rows"])
    ctx.queries[qid]["result_hash"] = answer["claims"][0]["numbers"][0]["result_hash"] = digest
    receipt = ask.query_receipts(ctx, answer)[qid]
    assert receipt["rows"][0] == {"cohort": "cohort_499", "posts": 499}
    assert receipt["supporting_rows"] == 1


def test_more_supporting_rows_than_the_cap_is_explicitly_unavailable():
    ctx, answer, qid = late_receipt()
    ctx.queries[qid]["rows"] = [{"cohort": f"cohort_{i}", "posts": 499} for i in range(60)]
    digest = result_hash(ctx.queries[qid]["rows"])
    ctx.queries[qid]["result_hash"] = answer["claims"][0]["numbers"][0]["result_hash"] = digest
    receipt = ask.query_receipts(ctx, answer)[qid]
    assert receipt["support_unavailable"] == "row_limit"
    assert len(receipt["rows"]) <= ask.RECEIPT_ROWS


def receipt_record(receipt):
    record = {"answer": {"claims": []}, "run": {"run_id": "r_integrity"}}
    row = {"record": json.dumps(record)}
    return json.loads(agent_app.with_receipts(row, record, {"q_1": receipt}))["query_receipts"]["q_1"]


def test_size_pressure_drops_preview_before_numeric_support(monkeypatch):
    monkeypatch.setattr(agent_app, "RECEIPT_BYTES", 1000)
    receipt = {"purpose": "counts", "sql": "SELECT counts", "params": {}, "result_hash": "sha256:pinned",
               "row_count": 500, "rows": [{"cohort": "cited", "posts": 499}, {"text": "x" * 2000}],
               "row_indexes": [499, 0], "supporting_rows": 1}
    saved = receipt_record(receipt)
    assert saved["rows"] == [{"cohort": "cited", "posts": 499}]
    assert saved["row_indexes"] == [499]
    assert saved["supporting_rows"] == 1 and "support_unavailable" not in saved


def test_numeric_support_that_cannot_fit_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(agent_app, "RECEIPT_BYTES", 1000)
    receipt = {"purpose": "counts", "sql": "SELECT counts", "params": {}, "result_hash": "sha256:pinned",
               "row_count": 500, "rows": [{"cohort": "x" * 2000, "posts": 499}],
               "row_indexes": [499], "supporting_rows": 1,
               "cell_types": [{"row_index": 499, "path": ["posts"], "type": "decimal"}]}
    saved = receipt_record(receipt)
    assert saved["rows"] == [] and saved["row_indexes"] == []
    assert saved["supporting_rows"] == 0 and saved["support_unavailable"] == "size"
    assert saved["cell_types"] == []


def test_size_pressure_keeps_only_type_metadata_for_retained_rows(monkeypatch):
    monkeypatch.setattr(agent_app, "RECEIPT_BYTES", 1000)
    receipt = {"purpose": "counts", "sql": "SELECT counts", "params": {}, "result_hash": "sha256:pinned",
               "row_count": 500, "rows": [{"cohort": "cited", "posts": "12.300"}, {"text": "x" * 2000}],
               "row_indexes": [499, 0], "supporting_rows": 1,
               "cell_types": [{"row_index": 499, "path": ["posts"], "type": "decimal"},
                              {"row_index": 0, "path": ["text"], "type": "decimal"}]}
    saved = receipt_record(receipt)
    assert saved["rows"] == [{"cohort": "cited", "posts": "12.300"}]
    assert saved["row_indexes"] == [499]
    assert saved["cell_types"] == [{"row_index": 499, "path": ["posts"], "type": "decimal"}]


@pytest.mark.parametrize("operation", ["freeze", "export"])
def test_existing_deselected_dossier_cannot_reuse_the_source_summary(operation):
    record = json.loads((Path(__file__).parents[1] / "fixtures/ask_complete.json").read_text(encoding="utf-8"))
    excluded = "The format spreads because it needs no props."
    record["answer"]["short_answer"] = excluded
    selection = dossiers.selection({}, record, None)
    draft = dossiers.build(record, selection, dossier_id="d_integrity", version=1, created_at="2026-10-07",
                           source={"ask_id": record["ask_id"]})
    draft["claims"][1]["kept"] = False
    if operation == "freeze":
        frozen = dossiers.freeze(draft, version=2, created_at="2026-10-07", ticks={})
        assert not frozen["summary"]
    else:
        draft["state"] = "frozen"
        assert excluded not in dossiers.render_html(draft)


@pytest.mark.parametrize("state", ["draft", "frozen"])
@pytest.mark.parametrize("suffix", ["", "/versions/1"])
def test_legacy_dossier_get_suppresses_summary_without_mutating_stored_body_or_hash(monkeypatch, state, suffix):
    record = json.loads((Path(__file__).parents[1] / "fixtures/ask_complete.json").read_text(encoding="utf-8"))
    selection = dossiers.selection({}, record, None)
    body = dossiers.build(record, selection, dossier_id="d_integrity", version=1, created_at="2026-10-07",
                          source={"ask_id": record["ask_id"]})
    body["summary"] = "An unticked inference that was removed."
    body["claims"][1]["kept"] = False
    body["state"] = state
    stored = copy.deepcopy(body)
    digest = dossiers.content_hash(body)
    monkeypatch.setattr(agent_app, "dossier_versions", lambda dossier_id: [
        {"body": body, "content_hash": digest, "version": 1}])
    monkeypatch.setattr(agent_app, "dossier_ticks", lambda dossier_id: {})
    shown = dossiers.shown(body)
    assert not shown["summary"]
    response = TestClient(agent_app.app).get(f"/api/dossiers/d_integrity{suffix}")
    assert response.status_code == 200
    assert not response.json()["summary"]
    assert response.json()["content_hash"] == digest
    assert body == stored and dossiers.content_hash(body) == digest


def test_null_support_count_cannot_cost_the_run_under_size_pressure(monkeypatch):
    monkeypatch.setattr(agent_app, "RECEIPT_BYTES", 1000)
    receipt = {"purpose": "counts", "sql": "SELECT counts", "params": {}, "result_hash": "sha256:pinned",
               "row_count": 500, "rows": [{"text": "x" * 2000}], "supporting_rows": None}
    saved = receipt_record(receipt)
    assert saved["rows"] == [] and saved["rows_dropped"] == "size"
    assert saved["support_unavailable"] == "invalid_support"
