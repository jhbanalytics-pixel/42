import copy
from dataclasses import replace
from datetime import timedelta

import pytest
from src.analysis.open_intelligence import brain_live_reader as reader
from src.analysis.open_intelligence.persistence import TABLE_BINDINGS, OpenIntelligenceRowBatch
from src.analysis.open_intelligence.run_receipts import build_run_receipt

from tests.unit import test_intelligence_brain_live_reader as fixture


def material(relations):
    return {name: tuple(relations[name]) for name in reader.RECEIPT_RELATIONS}


@pytest.mark.parametrize("version", ["hybrid_graph_v2", "hybrid_graph_v3"])
def test_complete_run_returns_validated_batch_without_capability(version):
    receipt, relations = fixture._receipt_and_rows()
    if version == "hybrid_graph_v3":
        relations = fixture._v3_rows()
        receipt = build_run_receipt(**relations["open_intelligence_run_receipts_v1"][0])
    original = reader._plain_authority_value(relations)
    batch = reader._validate_complete_run_rows(receipt, material(relations))
    assert isinstance(batch, OpenIntelligenceRowBatch)
    assert batch.cluster_build_version == version
    assert reader._plain_authority_value(relations) == original
    with pytest.raises(reader.LiveBrainReadRefusal):
        reader.validate_live_brain_read(batch)


@pytest.mark.parametrize("relation", reader.RECEIPT_RELATIONS)
def test_future_timestamp_in_every_family_refuses_helper_and_reader(relation):
    receipt, relations = fixture._receipt_and_rows()
    target = relations[relation][0]
    field = next(k for k, value in target.items() if hasattr(value, "tzinfo"))
    target[field] = fixture.COMPLETED_AT + timedelta(seconds=1)
    with pytest.raises(reader.LiveBrainReadRefusal):
        reader._validate_complete_run_rows(receipt, material(relations))
    with pytest.raises(reader.LiveBrainReadRefusal):
        reader.read_live_brain_authority(
            client=fixture._Client(relations), run_id=fixture.RUN_ID, signal_id=fixture.SIGNAL_ID
        )


@pytest.mark.parametrize("relation", tuple(TABLE_BINDINGS.values()))
def test_duplicate_natural_key_in_each_batch_family_refuses(relation):
    receipt, relations = fixture._receipt_and_rows()
    relations[relation].append(dict(relations[relation][0]))
    with pytest.raises(reader.LiveBrainReadRefusal):
        reader._validate_complete_run_rows(receipt, material(relations))


@pytest.mark.parametrize(
    "mutation",
    [
        "unselected_future",
        "foreign_member",
        "missing_candidate",
        "duplicate_candidate",
        "candidate_version",
        "prediction_version",
        "prediction_source_map",
        "prediction_rule",
        "evidence_count",
        "analysis_count",
        "invalid_number",
    ],
)
def test_whole_run_semantic_mutations_refuse(mutation):
    receipt, relations = fixture._receipt_and_rows()
    if mutation == "unselected_future":
        extra = copy.deepcopy(relations["signal_evidence_v2"][0])
        extra.update(
            signal_id="unselected",
            evidence_id="other_evidence",
            row_id="other_row",
            published_at=fixture.COMPLETED_AT + timedelta(seconds=1),
        )
        relations["signal_evidence_v2"].append(extra)
        receipt = replace(receipt, evidence_count=2)
    elif mutation == "foreign_member":
        relations["signal_membership_v2"][0]["client_scope_id"] = "foreign"
    elif mutation == "missing_candidate":
        relations["signal_candidates_v2"] = []
        receipt = replace(receipt, candidate_count=0)
    elif mutation == "duplicate_candidate":
        relations["signal_candidates_v2"] *= 2
        receipt = replace(receipt, candidate_count=2)
    elif mutation == "candidate_version":
        relations["signal_candidates_v2"][0]["cluster_build_version"] = "unknown"
    elif mutation.startswith("prediction_"):
        field = {
            "prediction_version": "cluster_build_version",
            "prediction_source_map": "source_family_map_version",
            "prediction_rule": "rule_version",
        }[mutation]
        relations["signal_predictions_v2"][0][field] = "wrong"
    elif mutation.endswith("_count"):
        receipt = replace(receipt, **{mutation: 2})
    else:
        relations["signal_evidence_v2"][0]["published_at"] = None
        relations["signal_candidates_v2"][0]["breadth_score"] = float("nan")
    with pytest.raises(reader.LiveBrainReadRefusal):
        reader._validate_complete_run_rows(receipt, material(relations))


def test_more_than_200_evidence_rows_can_be_inspected_without_admitting_them():
    receipt, relations = fixture._receipt_and_rows()
    original = relations["signal_evidence_v2"][0]
    relations["signal_evidence_v2"] = [
        {**original, "evidence_id": f"evidence_{index}", "row_id": f"row_{index}"}
        for index in range(201)
    ]
    receipt = replace(receipt, evidence_count=201)
    batch = reader._validate_complete_run_rows(receipt, material(relations))
    assert len(batch.evidence) == 201
    assert not hasattr(batch, "selected_facts")


def test_live_reader_invokes_shared_helper_once_and_keeps_digest_query(monkeypatch):
    original = reader._validate_complete_run_rows
    calls = []

    def validate(receipt, run_rows):
        calls.append(receipt.run_id)
        return original(receipt, run_rows)

    monkeypatch.setattr(reader, "_validate_complete_run_rows", validate)
    client = fixture._Client(fixture._receipt_and_rows()[1])
    read = reader.read_live_brain_authority(
        client=client, run_id=fixture.RUN_ID, signal_id=fixture.SIGNAL_ID
    )
    assert calls == [fixture.RUN_ID]
    assert reader.validate_live_brain_read(read) is read
    assert [item[0] for item in client.queries] == [
        "open_intelligence_run_receipts_v1",
        *reader.RECEIPT_RELATIONS,
        "row_set_digest",
        "signal_outcomes_v2",
        "open_intelligence_source_copy_manifest_v1",
        "open_intelligence_source_copy_receipts_v1",
    ]
