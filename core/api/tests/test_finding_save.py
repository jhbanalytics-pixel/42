import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest

from core.api import dossiers, finding_save

FIXTURE = Path(__file__).resolve().parents[3] / "app" / "frontend" / "src" / "ui" / "__tests__" / "fixtures" / "ask42_complete.json"


def source():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def posts(record=None):
    record = record or source()
    ids = {eid for claim in record["answer"]["claims"] for eid in claim["evidence_ids"]}
    return [{"post_id": eid} for eid in ids]


def producer_row(record, *, citations=None):
    claims = []
    for claim in record["answer"]["claims"]:
        numbers = claim.get("numbers") or []
        claims.append({
            "text": claim["text"],
            "label": claim["label"],
            "item_ids": [],
            "evidence_post_ids": list(citations if citations is not None else claim["evidence_ids"]),
            "query_ids": list(dict.fromkeys(number["query_id"] for number in numbers)),
            "run_ids": list(dict.fromkeys([*(number["run_id"] for number in numbers), record["run"]["run_id"]])),
            "result_hashes": list(dict.fromkeys(number["result_hash"] for number in numbers)),
        })
    as_of = "2026-09-28T04:10:00Z"
    return {
        "finding_id": "f_" + "a" * 64,
        "question": record["question"],
        "answer": "Producer-owned answer with scope and caveats.",
        "as_of": as_of,
        "claims": claims,
        "valid_from": record["finished_at"],
        "valid_to": None,
        "status": "current",
    }


def install_producer(monkeypatch, producer):
    module = ModuleType("core.agent.saved_findings")
    module.prepare_saved_ask_finding = producer
    monkeypatch.setitem(sys.modules, "core.agent.saved_findings", module)


def test_evidence_ids_delegates_and_uses_producer_claim_citations(monkeypatch):
    record = source()
    row = producer_row(record)
    calls = []

    def prepare(ask_id, saved):
        calls.append((ask_id, saved))
        return row

    install_producer(monkeypatch, prepare)

    assert finding_save.evidence_ids(record["ask_id"], record) == ["tt_fixture_1", "x_fixture_2"]
    assert calls == [(record["ask_id"], record)]


def test_build_returns_the_producer_row_unmodified(monkeypatch):
    record = source()
    row = producer_row(record)
    calls = []
    install_producer(monkeypatch, lambda ask_id, saved: calls.append((ask_id, saved)) or row)

    result = finding_save.build(record["ask_id"], record, posts(record))

    assert result is row
    assert result["answer"] == "Producer-owned answer with scope and caveats."
    assert calls == [(record["ask_id"], record)]


@pytest.mark.parametrize("skin_id", ["", "sk_private", []])
def test_build_rejects_skin_backed_source_before_calling_producer(monkeypatch, skin_id):
    record = source()
    record["skin_id"] = skin_id
    record["answer"]["evidence"][0]["handle"] = "Alice Example"
    record["answer"]["evidence"][0]["text"] = "Alice Example said a private phrase."
    record["answer"]["claims"][0]["quotes"][0]["text"] = "Alice Example said a private phrase."
    calls = []
    install_producer(monkeypatch, lambda ask_id, saved: calls.append((ask_id, saved)) or producer_row(record))

    with pytest.raises(dossiers.Refused) as caught:
        finding_save.build(record["ask_id"], record, posts(record))

    assert caught.value.status == 409
    assert calls == []
    assert "Alice Example" not in str(caught.value)


@pytest.mark.parametrize("field", ["investigation_id", "parent_id"])
@pytest.mark.parametrize("investigation_id", ["", "i_private", []])
def test_build_rejects_investigation_backed_source_before_calling_producer(monkeypatch, investigation_id, field):
    record = source()
    record[field] = investigation_id
    calls = []
    install_producer(monkeypatch, lambda ask_id, saved: calls.append((ask_id, saved)) or producer_row(record))

    with pytest.raises(dossiers.Refused) as caught:
        finding_save.build(record["ask_id"], record, posts(record))

    assert caught.value.status == 409
    assert calls == []


@pytest.mark.parametrize("change", [
    {"status": "running"},
    {"status": "stopped"},
    {"status": "failed"},
])
def test_build_rejects_noncomplete_saved_records_before_calling_producer(monkeypatch, change):
    record = {**source(), **change}
    calls = []
    install_producer(monkeypatch, lambda ask_id, saved: calls.append((ask_id, saved)) or producer_row(record))

    with pytest.raises(dossiers.Refused):
        finding_save.build(record["ask_id"], record, posts(record))

    assert calls == []


def test_build_rejects_a_different_ask_id_before_calling_producer(monkeypatch):
    record = source()
    calls = []
    install_producer(monkeypatch, lambda ask_id, saved: calls.append((ask_id, saved)) or producer_row(record))

    with pytest.raises(dossiers.Refused):
        finding_save.build("a_other", record, posts(record))

    assert calls == []


def test_build_rejects_insufficient_answer_before_calling_producer(monkeypatch):
    record = source()
    record["answer"]["status"] = "insufficient_evidence"
    calls = []
    install_producer(monkeypatch, lambda ask_id, saved: calls.append((ask_id, saved)) or producer_row(record))

    with pytest.raises(dossiers.Refused):
        finding_save.build(record["ask_id"], record, posts(record))

    assert calls == []


def test_build_rejects_malformed_saved_answer_before_calling_producer(monkeypatch):
    record = source()
    record["answer"]["claims"][1]["id"] = record["answer"]["claims"][0]["id"]
    calls = []
    install_producer(monkeypatch, lambda ask_id, saved: calls.append((ask_id, saved)) or producer_row(record))

    with pytest.raises(dossiers.Refused):
        finding_save.build(record["ask_id"], record, posts(record))

    assert calls == []


def test_build_maps_producer_value_error_to_not_eligible(monkeypatch):
    record = source()

    def prepare(ask_id, saved):
        raise ValueError("the answer needs more checked claims")

    install_producer(monkeypatch, prepare)

    with pytest.raises(dossiers.Refused) as caught:
        finding_save.build(record["ask_id"], record, posts(record))

    assert caught.value.status == 409
    assert caught.value.code == "not_eligible"


def test_build_reports_producer_missing_as_not_ready_without_fallback(monkeypatch):
    record = source()
    monkeypatch.setitem(sys.modules, "core.agent.saved_findings", None)

    with pytest.raises(dossiers.Refused) as caught:
        finding_save.build(record["ask_id"], record, posts(record))

    assert caught.value.status == 503
    assert caught.value.code == "producer_not_ready"


def test_build_requires_producer_citations_to_resolve_in_source_and_history(monkeypatch):
    record = source()
    row = producer_row(record, citations=["missing"])
    install_producer(monkeypatch, lambda ask_id, saved: row)

    with pytest.raises(dossiers.Refused):
        finding_save.build(record["ask_id"], record, [{"post_id": "missing"}])

    row = producer_row(record)
    install_producer(monkeypatch, lambda ask_id, saved: row)
    with pytest.raises(dossiers.Refused):
        finding_save.build(record["ask_id"], record, [{"post_id": "tt_fixture_1"}])


@pytest.mark.parametrize("finding_id", ["bad id", "_bad", "x" * 129])
def test_build_rejects_producer_finding_id_outside_ui_contract_before_post_read(monkeypatch, finding_id):
    record = source()
    row = producer_row(record)
    row["finding_id"] = finding_id
    install_producer(monkeypatch, lambda ask_id, saved: row)

    class UnreadPosts(list):
        def __iter__(self):
            raise AssertionError("post rows must not be read for an invalid finding ID")

    with pytest.raises(dossiers.Refused) as caught:
        finding_save.build(record["ask_id"], record, UnreadPosts())

    assert caught.value.status == 503
    assert caught.value.code == "producer_output_invalid"


def test_same_row_normalizes_timestamps_and_compares_all_persisted_fields(monkeypatch):
    record = source()
    row = producer_row(record)
    install_producer(monkeypatch, lambda ask_id, saved: row)
    expected = finding_save.build(record["ask_id"], record, posts(record))
    actual = copy.deepcopy(expected)
    actual["as_of"] = datetime(2026, 9, 28, 4, 10, tzinfo=timezone.utc)
    actual["valid_from"] = "2026-09-28T06:10:01+02:00"

    assert finding_save.same_row(expected, actual)

    for field in ("finding_id", "question", "answer", "as_of", "claims", "valid_from", "valid_to", "status"):
        changed = copy.deepcopy(actual)
        if field in ("as_of", "valid_from"):
            changed[field] = "2026-09-28T04:11:00Z"
        elif field == "valid_to":
            changed[field] = "2026-09-28T04:11:00Z"
        elif field == "claims":
            changed[field][0]["text"] += " changed"
        elif field == "answer":
            changed[field] += " changed"
        elif field == "question":
            changed[field] += " changed"
        elif field == "status":
            changed[field] = "stale"
        else:
            changed[field] += "changed"
        assert not finding_save.same_row(expected, changed)


@pytest.mark.parametrize("change", [
    lambda row: row.pop("status"),
    lambda row: row.update(extra="unexpected"),
    lambda row: row.update(as_of="not-a-timestamp"),
])
def test_same_row_rejects_incomplete_or_malformed_readback(monkeypatch, change):
    record = source()
    row = producer_row(record)
    install_producer(monkeypatch, lambda ask_id, saved: row)
    expected = finding_save.build(record["ask_id"], record, posts(record))
    actual = copy.deepcopy(expected)
    change(actual)

    assert not finding_save.same_row(expected, actual)


def test_validity_start_binds_completion_time_not_answer_data_time(monkeypatch):
    record = source()
    row = producer_row(record)
    assert record["finished_at"] != record["answer"]["as_of"]
    install_producer(monkeypatch, lambda *_: row)
    assert finding_save.build(record["ask_id"], record, posts(record))["valid_from"] == record["finished_at"]
    row["valid_from"] = record["answer"]["as_of"]
    with pytest.raises(dossiers.Refused, match="validity start"):
        finding_save.build(record["ask_id"], record, posts(record))
