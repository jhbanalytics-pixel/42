"""Watched signal projection: material change, continuity, cap, and an unsent render."""

from __future__ import annotations

import inspect
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.run_receipts import (
    RUN_RECEIPT_CONTRACT_VERSION,
    build_run_receipt,
)
from src.analysis.open_intelligence.watched_signal_delivery import (
    MATERIAL_FIELDS,
    WATCHED_SIGNAL_RECORD_FIELDS,
    render_watched_line,
    render_watched_lines,
    validated_watched_change,
    watched_change,
)

ROOT = Path(__file__).resolve().parents[2]
ROUTER = ROOT.parent / "app" / "frontend" / "src" / "router.js"
FIXTURE = (
    ROOT / "tests" / "fixtures" / "open_intelligence" / "v2" / "pulse_watched_signal_line.json"
)
SIGNAL_A = "sig_1111111111111111111111111111111111111111111111111111111111111111"
SIGNAL_B = "sig_2222222222222222222222222222222222222222222222222222222222222222"
TODAY = date(2026, 8, 27)


def record(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "signal_id": SIGNAL_A,
        "run_id": "run_fixture_001",
        "evidence_state": "ready",
        "velocity_band": "high",
        "breadth_band": "wide",
        "agreement": "two_families",
        "market": "za",
        "lineage_digest": "c" * 64,
    }
    row.update(overrides)
    return row


def receipt(**overrides: object):
    fields: dict[str, object] = {
        "run_contract_version": RUN_RECEIPT_CONTRACT_VERSION,
        "run_id": "run_fixture_001",
        "client_scope_id": "fixture_scope",
        "market_scope": ("za",),
        "signal_date": date(2026, 8, 25),
        "observation_start": date(2026, 8, 19),
        "observation_end": date(2026, 8, 25),
        "observation_method": "dynamic_signal_identity_v1",
        "source_window_digest": "a" * 64,
        "cluster_build_version": "hybrid_graph_v1",
        "source_family_map_version": "family_map_v2",
        "rule_version": "rule_v4",
        "status": "completed",
        "complete_partitions": True,
        "display_release_state": "enabled",
        "candidate_count": 1,
        "evidence_count": 2,
        "membership_count": 1,
        "lineage_count": 1,
        "analysis_count": 1,
        "prediction_count": 1,
        "row_set_digest": "b" * 64,
        "source_sha": "769408fc55680ca9d920a1e94dacaddba2c0bb91",
        "completed_at": datetime(2026, 8, 26, 6, 30, tzinfo=UTC),
    }
    fields.update(overrides)
    return build_run_receipt(**fields)


def pulse_line() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]


def test_material_fields_are_the_approved_projection() -> None:
    assert MATERIAL_FIELDS == (
        "evidence_state",
        "velocity_band",
        "breadth_band",
        "agreement",
        "market",
        "lineage_digest",
    )
    assert ("signal_id", "run_id", *MATERIAL_FIELDS) == WATCHED_SIGNAL_RECORD_FIELDS


def test_unchanged_signal_does_not_create_a_notification():
    row = dict.fromkeys(MATERIAL_FIELDS, "same")
    assert watched_change(row, dict(row)) == ()


def test_material_change_names_changed_fields_in_contract_order() -> None:
    previous = record()
    current = record(run_id="run_fixture_002", agreement="three_families", velocity_band="medium")
    assert watched_change(previous, current) == ("velocity_band", "agreement")
    assert validated_watched_change(previous, current) == ("velocity_band", "agreement")
    assert validated_watched_change(previous, record(run_id="run_fixture_002")) == ()


def test_continuity_requires_the_same_signal() -> None:
    with pytest.raises(ValueError, match="signal_continuity_required"):
        validated_watched_change(record(), record(run_id="run_fixture_002", signal_id=SIGNAL_B))


def test_duplicate_run_yields_no_notification() -> None:
    assert validated_watched_change(record(), record()) == ()
    with pytest.raises(ValueError, match="run_identity_conflict"):
        validated_watched_change(record(), record(agreement="three_families"))


@pytest.mark.parametrize(
    "bad",
    [
        "not a mapping",
        {key: value for key, value in record().items() if key != "lineage_digest"},
        {key: value for key, value in record().items() if key != "run_id"},
        record(velocity_band=3),
        record(agreement=""),
        record(evidence_state=None),
        record(signal_id="sig_short"),
        record(run_id=""),
    ],
)
def test_wrapper_validates_the_signal_record_shape(bad: object) -> None:
    with pytest.raises(ValueError, match="signal_record_invalid"):
        validated_watched_change(bad, record())
    with pytest.raises(ValueError, match="signal_record_invalid"):
        validated_watched_change(record(), bad)


def test_cap_is_explicit_and_overflow_refuses() -> None:
    cap = inspect.signature(render_watched_lines).parameters["cap"]
    assert cap.default is inspect.Parameter.empty
    assert cap.kind is inspect.Parameter.KEYWORD_ONLY
    entries = [(pulse_line(), ("agreement",))] * 3
    with pytest.raises(ValueError, match="watched_cap_exceeded"):
        render_watched_lines(entries, cap=2, release=receipt(), today=TODAY)
    for bad_cap in (0, -1, True, "3", 2.0):
        with pytest.raises(ValueError, match="watched_cap_invalid"):
            render_watched_lines(entries, cap=bad_cap, release=receipt(), today=TODAY)
    assert render_watched_lines([], cap=1, release=receipt(), today=TODAY) == ()


def test_raw_signal_id_without_release_proof_cannot_render() -> None:
    with pytest.raises(ValueError, match="pulse_line_invalid"):
        render_watched_line({"signal_id": SIGNAL_A}, ("agreement",), release=receipt(), today=TODAY)
    with pytest.raises(ValueError, match="pulse_line_invalid"):
        render_watched_line(
            {**pulse_line(), "extra": 1}, ("agreement",), release=receipt(), today=TODAY
        )
    with pytest.raises(ValueError, match="release_proof_required"):
        render_watched_line(
            pulse_line(), ("agreement",), release=receipt(run_id="run_fixture_002"), today=TODAY
        )
    with pytest.raises(ValueError, match="release_proof_required"):
        render_watched_line(
            pulse_line(),
            ("agreement",),
            release=receipt(display_release_state="blocked"),
            today=TODAY,
        )
    with pytest.raises(ValueError, match="release_proof_required"):
        render_watched_line(
            pulse_line(), ("agreement",), release=receipt(), today=date(2026, 8, 25)
        )
    with pytest.raises(ValueError, match="release_proof_required"):
        render_watched_line(pulse_line(), ("agreement",), release={"run_id": "x"}, today=TODAY)


@pytest.mark.parametrize(
    "reasons",
    [
        (),
        ("velocity_band", "velocity_band"),
        ("colour",),
        ("agreement", "velocity_band"),
        "agreement",
    ],
)
def test_render_refuses_reasons_outside_the_projection(reasons: object) -> None:
    expected = "no_material_change" if reasons == () else "reasons_invalid"
    with pytest.raises(ValueError, match=expected):
        render_watched_line(pulse_line(), reasons, release=receipt(), today=TODAY)


def test_render_refuses_because_the_router_defines_no_signal_route() -> None:
    if not ROUTER.is_file():
        pytest.skip("monorepo sibling tree is not present")
    router = ROUTER.read_text(encoding="utf-8")
    assert re.search(r"\bsignals?\b", router) is None, "router now names a signal route"
    with pytest.raises(ValueError, match="deep_link_unavailable"):
        render_watched_line(pulse_line(), ("agreement",), release=receipt(), today=TODAY)
    with pytest.raises(ValueError, match="deep_link_unavailable"):
        render_watched_lines(
            [(pulse_line(), ("agreement",))], cap=1, release=receipt(), today=TODAY
        )
