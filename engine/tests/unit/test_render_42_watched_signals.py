"""Offline staging renderer tests for the watched PULSE projection."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
SCRIPT = ENGINE / "scripts" / "staging" / "render_42_watched_signals.py"
FIXTURE = (
    ENGINE / "tests" / "fixtures" / "open_intelligence" / "v2" / "pulse_watched_signal_line.json"
)
SIGNAL = "sig_1111111111111111111111111111111111111111111111111111111111111111"
LINK = f"https://listening-post-staging-fibxg5ynpq-uc.a.run.app/#/topic/{SIGNAL}?region=za"


def release_receipt() -> dict[str, object]:
    return {
        "run_contract_version": "open_intelligence_run_receipt_v1",
        "run_id": "run_fixture_001",
        "client_scope_id": "fixture_scope",
        "market_scope": ["za"],
        "signal_date": "2026-08-25",
        "observation_start": "2026-08-19",
        "observation_end": "2026-08-25",
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
        "completed_at": "2026-08-26T06:30:00+00:00",
    }


def render_input() -> dict[str, object]:
    line = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]
    line.update(
        {
            "deep_link": LINK,
            "signal_name": "<signal>",
            "what_changed": "<changed>",
            "material_change": "<material>",
        }
    )
    return {
        "fixture_mode": True,
        "today": "2026-08-27",
        "release_receipt": release_receipt(),
        "lines": [{"line": line, "reasons": ["agreement"]}],
    }


def invoke(input_path: Path, output_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(input_path), "--output", str(output_path)],
        cwd=ENGINE,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def test_offline_renderer_writes_escaped_fixture_html_with_the_exact_staging_link(tmp_path) -> None:
    assert SCRIPT.is_file(), "RED: local watched-signal renderer is missing"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "watched.html"
    input_path.write_text(json.dumps(render_input()), encoding="utf-8")

    result = invoke(input_path, output_path)

    assert result.returncode == 0, result.stderr
    html = output_path.read_text(encoding="utf-8")
    assert "Fixture mode" in html
    assert "&lt;signal&gt;" in html
    assert "&lt;changed&gt;" in html
    assert f'href="{LINK}"' in html
    assert "<signal>" not in html


def test_offline_renderer_refuses_an_invalid_projection_and_never_overwrites_output(
    tmp_path,
) -> None:
    assert SCRIPT.is_file(), "RED: local watched-signal renderer is missing"
    payload = render_input()
    payload["lines"][0]["line"]["deep_link"] = "https://example.invalid/"
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "watched.html"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    output_path.write_text("existing", encoding="utf-8")

    result = invoke(input_path, output_path)

    assert result.returncode != 0
    assert output_path.read_text(encoding="utf-8") == "existing"
    assert "deep_link_unavailable" in result.stderr


def test_offline_renderer_has_no_send_or_cloud_import_path() -> None:
    assert SCRIPT.is_file(), "RED: local watched-signal renderer is missing"
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not any(
        name.startswith(("google", "requests", "smtplib", "src.alerts")) for name in imports
    )
