"""Watched signal projection: the kernel, the approved contract, and the unsent render."""

from __future__ import annotations

import ast
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence import watched_signal_delivery as module
from src.analysis.open_intelligence.general_question_deployment import _AUDIENCE
from src.analysis.open_intelligence.run_receipts import (
    QA_CLIENT_SCOPE_ID,
    RUN_RECEIPT_CONTRACT_VERSION,
    build_run_receipt,
)
from src.analysis.open_intelligence.watched_signal_delivery import (
    MATERIAL_FIELDS,
    STAGING_ORIGIN,
    render_watched_line,
    render_watched_lines,
    signal_deep_link,
    validated_watched_change,
    watched_change,
)

ENGINE = Path(__file__).resolve().parents[2]
MODULE = ENGINE / "src" / "analysis" / "open_intelligence" / "watched_signal_delivery.py"
ROUTER = ENGINE.parent / "app" / "frontend" / "src" / "router.js"
EXPLORE = ENGINE.parent / "app" / "frontend" / "src" / "explore.jsx"
FIXTURE = (
    ENGINE / "tests" / "fixtures" / "open_intelligence" / "v2" / "pulse_watched_signal_line.json"
)
SIGNAL_A = "sig_1111111111111111111111111111111111111111111111111111111111111111"
SIGNAL_B = "sig_2222222222222222222222222222222222222222222222222222222222222222"
DEEP_LINK = f"{STAGING_ORIGIN}/#/topic/{SIGNAL_A}?region=za"
TODAY = date(2026, 8, 27)
SEND_ENTRY_POINTS = (
    "src.alerts.email_digest",
    "src.alerts.detector",
    "smtplib",
    "src.alerts.self_heal",
)


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


def pulse_line(**overrides: object) -> dict[str, object]:
    """The frozen PULSE fixture line, carrying the one deep link 42 serves."""
    line = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]
    line["deep_link"] = DEEP_LINK
    line.update(overrides)
    return line


def test_unchanged_signal_does_not_create_a_notification():
    row = dict.fromkeys(MATERIAL_FIELDS, "same")
    assert watched_change(row, dict(row)) == ()


def test_kernel_is_the_pure_projection_with_no_threshold() -> None:
    source = ast.get_source_segment(
        MODULE.read_text(encoding="utf-8"),
        next(
            node
            for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef) and node.name == "watched_change"
        ),
    )
    assert source is not None
    assert "previous[key] != current[key]" in source
    assert not re.search(r"[<>]=?\s*\d", source)


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
        record(evidence_state="same"),
        record(evidence_state="READY"),
        record(market="ZA"),
        record(market="us"),
        record(lineage_digest="c" * 63),
        record(lineage_digest="C" * 64),
        record(velocity_band="0.82"),
        record(breadth_band="3"),
        record(agreement="75%"),
    ],
)
def test_records_outside_the_approved_signal_contract_never_compare(bad: dict) -> None:
    good = record(run_id="run_fixture_002")
    with pytest.raises(ValueError, match="signal_contract_invalid"):
        validated_watched_change(bad, good)
    with pytest.raises(ValueError, match="signal_contract_invalid"):
        validated_watched_change(record(), {**bad, "run_id": "run_fixture_002"})


def test_deep_link_reproduces_the_router_topic_grammar_on_the_staging_origin() -> None:
    assert STAGING_ORIGIN == _AUDIENCE
    if ROUTER.is_file():
        router = ROUTER.read_text(encoding="utf-8")
        assert re.search(r"\bsignals?\b", router) is None
        assert "return '#/topic/' + encodeURIComponent(id) + '?region=' + scope;" in router
        explore = EXPLORE.read_text(encoding="utf-8")
        assert "buildTopicHash(selected.id" in explore
    assert signal_deep_link(SIGNAL_A, ("za",)) == DEEP_LINK
    assert (
        signal_deep_link(SIGNAL_A, ("za", "ng"))
        == f"{STAGING_ORIGIN}/#/topic/{SIGNAL_A}?region=all"
    )
    for bad_scope in ((), ("ZA",), ("us",), ("za", "us")):
        with pytest.raises(ValueError, match="deep_link_unavailable"):
            signal_deep_link(SIGNAL_A, bad_scope)
    with pytest.raises(ValueError, match="deep_link_unavailable"):
        signal_deep_link("sig_short", ("za",))


def test_render_returns_an_unsent_artifact_with_the_exact_deep_link() -> None:
    artifact = render_watched_line(pulse_line(), ("agreement",), release=receipt(), today=TODAY)
    assert artifact["delivery"] == "unsent"
    assert artifact["deep_link"] == DEEP_LINK
    assert artifact["reasons"] == ("agreement",)
    assert artifact["release_run_id"] == "run_fixture_001"
    assert artifact["line"] == pulse_line()
    assert set(artifact) == {"delivery", "deep_link", "reasons", "line", "release_run_id"}


def test_render_enforces_source_run_and_deep_link_continuity() -> None:
    with pytest.raises(ValueError, match="release_proof_required"):
        render_watched_line(
            pulse_line(), ("agreement",), release=receipt(run_id="run_fixture_002"), today=TODAY
        )
    with pytest.raises(ValueError, match="release_proof_required"):
        render_watched_line(
            pulse_line(),
            ("agreement",),
            release=receipt(client_scope_id="other_scope"),
            today=TODAY,
        )
    for stale in (
        pulse_line(deep_link=f"/signals/{SIGNAL_A}?market=za"),
        pulse_line(deep_link=f"{STAGING_ORIGIN}/#/topic/{SIGNAL_B}?region=za"),
        pulse_line(deep_link=f"{STAGING_ORIGIN}/#/topic/{SIGNAL_A}?region=ng"),
        pulse_line(deep_link=f"#/topic/{SIGNAL_A}?region=za"),
    ):
        with pytest.raises(ValueError, match="deep_link_unavailable"):
            render_watched_line(stale, ("agreement",), release=receipt(), today=TODAY)


def test_raw_signal_id_without_release_proof_cannot_render() -> None:
    with pytest.raises(ValueError, match="pulse_line_invalid"):
        render_watched_line({"signal_id": SIGNAL_A}, ("agreement",), release=receipt(), today=TODAY)
    with pytest.raises(ValueError, match="release_proof_required"):
        render_watched_line(
            pulse_line(),
            ("agreement",),
            release=receipt(display_release_state="blocked"),
            today=TODAY,
        )
    with pytest.raises(ValueError, match="release_proof_required"):
        render_watched_line(pulse_line(), ("agreement",), release=None, today=TODAY)


def test_qa_identity_never_renders() -> None:
    qa_line = pulse_line(client_scope_id=QA_CLIENT_SCOPE_ID, brand_config_id="qa", theme_id="qa")
    with pytest.raises(ValueError, match="qa_identity_refused"):
        render_watched_line(
            qa_line,
            ("agreement",),
            release=receipt(client_scope_id=QA_CLIENT_SCOPE_ID),
            today=TODAY,
        )


def test_cap_overflow_refuses_before_any_line_renders() -> None:
    entries = [(pulse_line(), ("agreement",))] * 3
    with pytest.raises(ValueError, match="watched_cap_exceeded"):
        render_watched_lines(entries, cap=2, release=receipt(), today=TODAY)
    rendered = render_watched_lines(entries, cap=3, release=receipt(), today=TODAY)
    assert len(rendered) == 3
    assert all(item["delivery"] == "unsent" and item["deep_link"] == DEEP_LINK for item in rendered)


def test_render_never_imports_or_calls_a_send_path() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for entry_point in SEND_ENTRY_POINTS:
        assert not any(
            name == entry_point or name.startswith(entry_point + ".") for name in imported
        )
    assert not any(name.startswith("src.alerts") for name in imported)
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not any(name.startswith("send") for name in calls)
