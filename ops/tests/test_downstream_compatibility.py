"""The downstream compatibility matrix names live readers and loadable fixtures."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
MATRIX = ROOT / "docs" / "operations" / "downstream-compatibility.md"
CONSUMERS = ("42", "PULSE render", "Looker", "Daily summary", "Health")
CITATION = re.compile(r"`([^`\s]+):(\d+)` `([^`]+)`")
FIXTURE = re.compile(r"`([^`\s]+\.(?:json|sql|yaml|py))`")
SIGNAL_A = "sig_1111111111111111111111111111111111111111111111111111111111111111"

if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))


def _rows() -> dict[str, dict[str, str]]:
    text = MATRIX.read_text(encoding="utf-8")
    header = [
        "Consumer",
        "Reader",
        "Fixture",
        "Bridge or parity",
        "Stale or missing behaviour",
        "Production boundary",
    ]
    rows: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        if not line.startswith("| ") or line.startswith(("| Consumer", "|---")):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        assert len(cells) == len(header), line
        rows[cells[0]] = dict(zip(header, cells, strict=True))
    return rows


def _lines(path: str) -> list[str]:
    return (ROOT / path).read_text(encoding="utf-8").splitlines()


def test_every_consumer_has_a_row_with_every_column_filled() -> None:
    rows = _rows()
    assert tuple(rows) == CONSUMERS
    for consumer, row in rows.items():
        for column, value in row.items():
            assert value, (consumer, column)


def test_every_reader_citation_names_an_existing_line_carrying_its_symbol() -> None:
    for consumer, row in _rows().items():
        citations = CITATION.findall(row["Reader"])
        assert citations, consumer
        for path, number, symbol in citations:
            lines = _lines(path)
            assert int(number) <= len(lines), (consumer, path, number)
            assert symbol in lines[int(number) - 1], (consumer, path, number, symbol)


def test_every_fixture_named_in_the_matrix_exists() -> None:
    for consumer, row in _rows().items():
        fixtures = FIXTURE.findall(row["Fixture"])
        assert fixtures, consumer
        for path in fixtures:
            assert (ROOT / path).is_file(), (consumer, path)


def test_the_42_fixture_loads_through_the_fixture_package_contract() -> None:
    from src.contracts.open_intelligence_fixtures import load_fixture_package

    directory = ENGINE / "tests" / "fixtures" / "open_intelligence" / "v2"
    package = load_fixture_package(directory)
    ids = {fixture.fixture_id for fixture in package.fixtures}
    assert {"dynamic_signal_ready", "pulse_watched_signal_line"} <= ids
    payload = json.loads(
        (directory / "dynamic_signal_ready.json").read_text(encoding="utf-8")
    )
    assert payload["payload"]["client_scope_id"] == "fixture_scope"


def test_the_pulse_fixture_renders_unsent_only_on_the_route_42_serves() -> None:
    from src.analysis.open_intelligence.run_receipts import (
        RUN_RECEIPT_CONTRACT_VERSION,
        build_run_receipt,
    )
    from src.analysis.open_intelligence.watched_signal_delivery import (
        STAGING_ORIGIN,
        render_watched_line,
    )

    path = (
        ENGINE
        / "tests"
        / "fixtures"
        / "open_intelligence"
        / "v2"
        / "pulse_watched_signal_line.json"
    )
    line = json.loads(path.read_text(encoding="utf-8"))["payload"]
    release = build_run_receipt(
        run_contract_version=RUN_RECEIPT_CONTRACT_VERSION,
        run_id=line["run_id"],
        client_scope_id=line["client_scope_id"],
        market_scope=tuple(line["market_scope"]),
        signal_date=date(2026, 8, 25),
        observation_start=date(2026, 8, 19),
        observation_end=date(2026, 8, 25),
        observation_method="dynamic_signal_identity_v1",
        source_window_digest="a" * 64,
        cluster_build_version="hybrid_graph_v1",
        source_family_map_version="family_map_v2",
        rule_version="rule_v4",
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
        completed_at=datetime(2026, 8, 26, 6, 30, tzinfo=UTC),
    )
    today = date(2026, 8, 27)
    with pytest.raises(ValueError, match="deep_link_unavailable"):
        render_watched_line(line, ("agreement",), release=release, today=today)
    routed = {**line, "deep_link": f"{STAGING_ORIGIN}/#/topic/{SIGNAL_A}?region=za"}
    artifact = render_watched_line(routed, ("agreement",), release=release, today=today)
    assert artifact["delivery"] == "unsent"
    assert artifact["deep_link"] == routed["deep_link"]


def test_pulse_matrix_names_the_local_fixture_renderer_that_calls_the_projection() -> None:
    renderer = ENGINE / "scripts" / "staging" / "render_42_watched_signals.py"
    source = renderer.read_text(encoding="utf-8")
    assert "fixture_mode" in source
    assert "render_watched_lines(entries" in source
    assert not any(entry in source for entry in ("src.alerts", "google.cloud", "smtplib"))


def test_the_looker_views_named_by_the_deployer_exist_and_replace_in_place() -> None:
    source = (ENGINE / "scripts" / "create_looker_views.py").read_text(encoding="utf-8")
    views = re.findall(r'^\s+"(v_[a-z_]+\.sql)",$', source, re.MULTILINE)
    assert len(views) == 7
    for name in views:
        sql = (ENGINE / "infra" / "bigquery_views" / name).read_text(encoding="utf-8")
        assert "CREATE OR REPLACE VIEW" in sql, name
        assert "qa_canary" not in sql, name
        assert "signal_candidates_v2" not in sql, name


def test_the_health_capacity_fixture_declares_the_retired_vendor_at_zero() -> None:
    capacity = yaml.safe_load(
        (ENGINE / "configs" / "engine_capacity.yaml").read_text(encoding="utf-8")
    )
    brand24 = capacity["connectors"]["brand24"]
    assert brand24["expected_rows_per_day"] == 0
    assert set(brand24["expected_rows_per_market_per_day"].values()) == {0}
    catalog = yaml.safe_load(
        (ENGINE / "configs" / "vendor_endpoint_catalog.yaml").read_text(
            encoding="utf-8"
        )
    )
    statuses = {endpoint["status"] for endpoint in catalog["brand24"]["endpoints"]}
    assert statuses == {"permanently_disabled"}
