from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from scripts.staging import export_dynamic_quality_review as exporter
from src.analysis.open_intelligence import live_quality
from src.contracts.bigquery_ddl import parse_table_ddl

from tests.unit import test_dynamic_signal_persistence as persistence_fixtures

RUN_ID = "run_20260903_dynamic_apply_v2_r16"
ROOT = Path(__file__).resolve().parents[2]


def test_quality_release_schema_is_exact_and_immutable_by_shape():
    sql = (
        (ROOT / "infra/bigquery_schemas/open_intelligence_quality_release_records_v2.sql")
        .read_text(encoding="utf-8")
        .format(project="fixture-project", dataset="fixture_dataset")
    )
    parsed = parse_table_ddl(sql)
    assert tuple(field[0] for field in parsed["fields"]) == live_quality.QUALITY_RELEASE_FIELDS
    assert parsed["partition"] == "DATE(released_at)"
    assert parsed["cluster"] == ("run_id",)


def _batch():
    candidate = persistence_fixtures._candidate()
    candidate["run_id"] = RUN_ID
    evidence = persistence_fixtures._evidence()
    evidence["run_id"] = RUN_ID
    membership = persistence_fixtures._membership()
    membership["run_id"] = RUN_ID
    return persistence_fixtures._batch(
        candidates=(candidate,), evidence=(evidence,), membership=(membership,)
    )


def test_review_packet_contains_every_evidence_row_once_in_contract_order():
    packet = live_quality.build_review_packet(RUN_ID, _batch())
    assert tuple(packet) == (
        "review_contract_version",
        "run_id",
        "review_items",
        "packet_digest",
    )
    assert packet["review_contract_version"] == "dynamic_quality_review_v1"
    assert len(packet["review_items"]) == 1
    item = packet["review_items"][0]
    assert tuple(item) == live_quality.REVIEW_ITEM_FIELDS
    assert item["evidence_id"] == "ev_" + "b" * 64
    preimage = {key: packet[key] for key in tuple(packet)[:-1]}
    assert packet["packet_digest"] == live_quality.canonical_digest(preimage)
    assert packet["packet_digest"] == (
        "1331b4eaf36a448ea68fa64083d323bb624458ca694d39f1fb2cc25779dc54de"
    )


def test_read_only_exporter_renders_the_canonical_packet_without_a_cloud_reader():
    rendered = exporter.render_review_packet(RUN_ID, _batch())
    payload = json.loads(rendered)
    assert payload["run_id"] == RUN_ID
    assert (
        payload["packet_digest"]
        == live_quality.build_review_packet(RUN_ID, _batch())["packet_digest"]
    )
    assert len(payload["review_items"]) == len(_batch().evidence)


def _receipt(packet, **overrides):
    fields = {
        "review_contract_version": "dynamic_quality_review_v1",
        "run_id": RUN_ID,
        "source_window_digest": "a" * 64,
        "candidate_projection_digest": live_quality.candidate_projection_digest(RUN_ID, _batch()),
        "packet_digest": packet["packet_digest"],
        "reviewed_evidence_ids": tuple(item["evidence_id"] for item in packet["review_items"]),
        "foreign_market_evidence_ids": (),
        "factual_conflict_evidence_ids": (),
        "uncertain_evidence_ids": (),
        "reviewed_by": "Albert",
        "reviewed_at": datetime(2026, 8, 30, 12, tzinfo=UTC),
        "decision": "approved",
    }
    fields.update(overrides)
    fields["receipt_digest"] = live_quality.review_receipt_digest(fields)
    return fields


def test_review_receipt_requires_exact_order_complete_review_and_empty_exceptions():
    packet = live_quality.build_review_packet(RUN_ID, _batch())
    receipt = _receipt(packet)
    validated = live_quality.validate_review_receipt(
        receipt,
        packet=packet,
        source_window_digest="a" * 64,
        candidate_projection_digest=live_quality.candidate_projection_digest(RUN_ID, _batch()),
    )
    assert validated.reviewed_by == "Albert"
    assert validated.receipt_digest == receipt["receipt_digest"]
    for mutation in (
        {"reviewed_evidence_ids": ()},
        {"foreign_market_evidence_ids": (packet["review_items"][0]["evidence_id"],)},
        {"factual_conflict_evidence_ids": (packet["review_items"][0]["evidence_id"],)},
        {"uncertain_evidence_ids": (packet["review_items"][0]["evidence_id"],)},
        {"reviewed_by": "agent"},
        {"decision": "rejected"},
    ):
        with pytest.raises(live_quality.QualityRefusal):
            live_quality.validate_review_receipt(
                _receipt(packet, **mutation),
                packet=packet,
                source_window_digest="a" * 64,
                candidate_projection_digest=live_quality.candidate_projection_digest(
                    RUN_ID, _batch()
                ),
            )


def test_review_receipt_digest_excludes_only_itself_and_refuses_nfc_drift():
    packet = live_quality.build_review_packet(RUN_ID, _batch())
    receipt = _receipt(packet)
    assert live_quality.review_receipt_digest(receipt) == receipt["receipt_digest"]
    receipt["reviewed_by"] = "Albe\u0301rt"
    with pytest.raises(live_quality.QualityRefusal, match="noncanonical_unicode"):
        live_quality.review_receipt_digest(receipt)


def test_packet_timestamps_render_the_way_bigquery_formats_them():
    # The register routine and the release recompute the packet digest in SQL with
    # FORMAT_TIMESTAMP('%E*S'), which trims trailing zeros from the fraction and drops it
    # entirely on a whole second. The Python packet must render the same text or the two
    # digests can never agree on a row with a real published_at.
    from datetime import UTC, datetime

    cases = {
        datetime(2026, 8, 25, 0, 54, 57, 696000, tzinfo=UTC): "2026-08-25T00:54:57.696Z",
        datetime(2026, 8, 25, 0, 54, 57, tzinfo=UTC): "2026-08-25T00:54:57Z",
        datetime(2026, 8, 25, 0, 54, 57, 5, tzinfo=UTC): "2026-08-25T00:54:57.000005Z",
        datetime(2026, 8, 25, 0, 54, 57, 120000, tzinfo=UTC): "2026-08-25T00:54:57.12Z",
    }
    for value, text in cases.items():
        assert live_quality.bigquery_timestamp_text(value) == text
    assert live_quality.bigquery_timestamp_text(None) is None
    batch = _batch()
    packet = live_quality.build_review_packet(RUN_ID, batch)
    item = packet["review_items"][0]
    assert item["published_at"] == live_query_text(batch.evidence[0]["published_at"])


def live_query_text(value):
    return live_quality.bigquery_timestamp_text(value)
