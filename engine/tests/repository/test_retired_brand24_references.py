"""Tree scan for active references to the retired Brand24 vendor.

Brand24 was retired on 21 Aug 2026 (engine/configs/sources.yaml). A reference
in shipped copy, a prompt, a config binding, a health suggestion or an
operating document that still presents the vendor as a live input is active
and fails this scan. A record carrying a historical label (legacy, retired,
cancelled, contract ended, historical, archive) on the line or within the
twelve lines above it is exempt, as is a stored identifier (a column, content
type or query group name) that must never change, and a comment or docstring,
which records history rather than shipping behaviour.

The scan reads the whole checkout: the app trees, the root ``infra`` bindings,
the ``ops`` deployment package and the operating documents, none of which the
engine build context carries. It therefore lives under ``tests/repository``,
which the publish build runs over the checkout in the engine test image rather
than inside that image's own copy of the engine tree.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import logging
import re
import sys
import tokenize
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[3]
ENGINE = REPOSITORY / "engine"

# The shipped build (engine/Dockerfile.staging copies src, scripts, configs,
# infra and ops) plus the app copy, the runtime bindings and the operating docs.
SCANNED_ROOTS = (
    ENGINE / "src",
    ENGINE / "scripts",
    ENGINE / "configs",
    ENGINE / "infra",
    ENGINE / "Dockerfile.staging",
    ENGINE / "cloudbuild.staging.yaml",
    ENGINE / "cloudbuild.publish.yaml",
    ENGINE / "README.md",
    ENGINE / "DEVELOPMENT.md",
    REPOSITORY / "app" / "backend",
    REPOSITORY / "app" / "frontend" / "src",
    REPOSITORY / "app" / "docs" / "staging-deploy.md",
    REPOSITORY / "app" / "docs" / "staging-qa-checklist.md",
    REPOSITORY / "infra",
    REPOSITORY / "ops" / "deploy",
    REPOSITORY / "docs" / "operations",
    REPOSITORY / "cloudbuild.app.publish.yaml",
)
SCANNED_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".sql", ".md", ".js", ".jsx", ".toml", ""}
# Records, not shipped behaviour: the retired connector module itself (kept for
# stored row readback and asserted unimported below), stored schema migrations
# and test trees.
EXEMPT_PATH_PARTS = ("tests", "__tests__", "migrations", "node_modules", "__pycache__")
EXEMPT_FILES = frozenset({"engine/src/ingestion/connectors/brand24.py"})
REFERENCE = re.compile(r"brand24", re.IGNORECASE)
HISTORICAL_MARKER = re.compile(
    r"legacy|retired|cancelled|contract ended|historical|archive|no longer|"
    r"kept for|stored rows|read ?back",
    re.IGNORECASE,
)
STORED_IDENTIFIER = re.compile(
    r"brand24_[a-z0-9_]+|_brand24\b|brand24\.py|[\"']brand24[\"']|\brow\.brand24\b|^\s*brand24,\s*$",
    re.IGNORECASE,
)
LINE_COMMENT = re.compile(r"^\s*(?:#|--|//|\*|/\*)")
MARKER_WINDOW_ABOVE = 12
MARKER_WINDOW_BELOW = 8


def _files() -> list[Path]:
    found: list[Path] = []
    for root in SCANNED_ROOTS:
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in candidates:
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            relative = path.relative_to(REPOSITORY).as_posix()
            if any(part in EXEMPT_PATH_PARTS for part in path.parts) or relative in EXEMPT_FILES:
                continue
            found.append(path)
    return found


def _record_lines(path: Path, text: str) -> set[int]:
    """Comment and docstring lines, which record history rather than ship it."""
    lines: set[int] = set()
    if path.suffix != ".py":
        for number, line in enumerate(text.splitlines(), start=1):
            if LINE_COMMENT.match(line):
                lines.add(number)
        return lines
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return lines
    previous_significant = None
    for token in tokens:
        if token.type == tokenize.COMMENT:
            lines.add(token.start[0])
        elif token.type == tokenize.STRING and previous_significant in {
            None,
            tokenize.NEWLINE,
            tokenize.INDENT,
            tokenize.DEDENT,
        }:
            lines.update(range(token.start[0], token.end[0] + 1))
        if token.type not in {tokenize.NL, tokenize.COMMENT}:
            previous_significant = token.type
    return lines


def active_references() -> list[str]:
    """Every Brand24 reference the exemptions above do not cover, as path:line: text."""
    active: list[str] = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        records = _record_lines(path, text)
        for number, line in enumerate(lines, start=1):
            if REFERENCE.search(line) is None or number in records:
                continue
            if STORED_IDENTIFIER.search(REFERENCE.sub("brand24", line)):
                continue
            window = lines[max(0, number - 1 - MARKER_WINDOW_ABOVE) : number + MARKER_WINDOW_BELOW]
            if any(HISTORICAL_MARKER.search(item) for item in window):
                continue
            relative = path.relative_to(REPOSITORY).as_posix()
            active.append(f"{relative}:{number}: {line.strip()[:120]}")
    return active


def test_the_scan_covers_the_shipped_build_and_bindings() -> None:
    scanned = {path.relative_to(REPOSITORY).as_posix() for path in _files()}
    assert "engine/src/alerts/email_render/footer.py" in scanned
    assert "engine/src/analysis/prompts/daily_summary.py" in scanned
    assert "engine/configs/vendor_endpoint_catalog.yaml" in scanned
    assert "engine/scripts/engine_pulse.py" in scanned
    assert "engine/infra/bigquery_views/v_pipeline_health.sql" in scanned
    assert "infra/runtime/daily-staging.json" in scanned
    assert "engine/cloudbuild.staging.yaml" in scanned
    assert "docs/operations/release.md" in scanned
    assert not any(item.startswith("engine/tests/") for item in scanned)


def test_no_active_reference_to_the_retired_vendor_remains() -> None:
    assert active_references() == []


def test_daily_entrypoint_has_no_retired_connector_selection() -> None:
    script = ENGINE / "scripts" / "run_rss_now.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    imported = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "src.ingestion.connectors.brand24"
    ]
    connector_keys = [
        item.elts[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "CONNECTORS" for target in node.targets
        )
        for item in node.value.elts
        if isinstance(item, ast.Tuple)
        and item.elts
        and isinstance(item.elts[0], ast.Constant)
        and isinstance(item.elts[0].value, str)
    ]
    assert imported == []
    assert "brand24" not in connector_keys
    assert {"rss", "socialcrawl", "youtube"} <= set(connector_keys)


@pytest.fixture
def verify_live_module(monkeypatch):
    path = ENGINE / "scripts" / "verify_live.py"
    spec = importlib.util.spec_from_file_location("verify_live_retired_control", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    previous_disable = logging.root.manager.disable
    monkeypatch.setattr(sys, "path", sys.path.copy())
    try:
        yield module
    finally:
        logging.disable(previous_disable)


def test_verify_live_refuses_unsupported_targets_before_handlers(
    monkeypatch, capsys, verify_live_module
) -> None:
    module = verify_live_module
    calls = []
    monkeypatch.setattr(module, "_load_env", lambda: None)
    for name in (
        "_verify_gdelt",
        "_reclass_diff",
        "_embed_gate",
        "_ensemble_probe",
        "_ensemble_parent_probe",
        "_verify_semrush",
    ):
        monkeypatch.setattr(module, name, lambda *args, _name=name: calls.append(_name))
    for targets in (("brand24-mentions",), ("gdelt", "brand24-mentions"), ("unknown",)):
        assert module.main(["verify_live.py", *targets]) == 2
        assert calls == []
        assert "unsupported target:" in capsys.readouterr().err


def test_verify_live_keeps_the_default_supported_target(monkeypatch, verify_live_module) -> None:
    module = verify_live_module
    calls = []
    monkeypatch.setattr(module, "_load_env", lambda: None)
    monkeypatch.setattr(module, "_verify_gdelt", lambda: calls.append("gdelt"))
    assert module.main(["verify_live.py"]) == 0
    assert calls == ["gdelt"]


def test_the_retired_connector_is_not_imported_by_shipped_source() -> None:
    importers = []
    for path in sorted((ENGINE / "src").rglob("*.py")):
        if re.search(
            r"^\s*(?:from|import)\s+src\.ingestion\.connectors\.brand24\b|"
            r"^\s*from\s+\.\s*brand24\s+import|^\s*from\s+\.brand24\s+import",
            path.read_text(encoding="utf-8"),
            re.MULTILINE,
        ):
            importers.append(path.relative_to(REPOSITORY).as_posix())
    assert importers == [], importers
