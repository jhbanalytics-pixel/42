"""The Listening Post reads the completed run only.

The retired vendor client, its routes and every hard-coded keyword set are gone
from the tree a reader or a colleague can see: the API, the frontend, the
scripts, the configs, the deploy files and the living docs. Dated planning
documents and audit records under docs/ are historical and stay as written.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SHIPPING_PREFIXES = (
    "src/",
    "frontend/src/",
    "scripts/",
    "configs/",
    "infra/",
    ".github/",
    "web/",
    "tests/",
)
LIVING_DOCS = (
    "README.md",
    "DESIGN_CONTEXT.md",
    "docs/staging-deploy.md",
    "docs/jo-research-acceptance.md",
    "docs/V3-WOW-DEMO.md",
)
HISTORICAL_PREFIXES = (
    "docs/2026-",
    "docs/audits/",
    "docs/plans/",
    "docs/prompts/",
    "docs/reviews/",
    "docs/v3-visual-redesign-notes",
    "docs/staging-full-qa-report-",
)
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".tgz", ".pyc"}

RETIRED_VENDOR = re.compile(r"brand\s*24", re.IGNORECASE)
RETIRED_METHOD = re.compile(r"base keywords?|seed keywords?|base 25|keyword set", re.IGNORECASE)
# The two routes that were deleted with the vendor. A living doc that still
# names them points a colleague at a dead endpoint.
RETIRED_ROUTES = ("/api/intel/overview", "/api/intel/authors")
# The old chip rows and market seed rows, as literal arrays in source.
LITERAL_KEYWORD_ARRAYS = (
    re.compile(r"\[\s*'fifa'\s*,\s*'rugby'"),
    re.compile(r"SUGGEST(?:IONS)?\s*=\s*\["),
    re.compile(r"EXAMPLES\s*=\s*\{"),
    re.compile(r"\b(?:za|ng|ke)\s*:\s*\[\s*'[a-z ]+'\s*,\s*'[a-z ]+'"),
)
# Names that only ever appeared in the vendor client's author noise list.
NOISE_LIST_MARKERS = ("kondzilla", "abs-cbn", "worldstar", "startimes", "_GLOBAL_BRAND_AUTHORS")


def _tracked_files() -> list[Path]:
    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    names = [n for n in out.decode("utf-8").split("\0") if n]
    picked = []
    for name in names:
        if name.startswith(HISTORICAL_PREFIXES):
            continue
        if not (name.startswith(SHIPPING_PREFIXES) or name in LIVING_DOCS):
            continue
        if name == "tests/unit/test_retired_sources.py":
            continue
        if Path(name).suffix.lower() in BINARY_SUFFIXES:
            continue
        if "frontend/vendor" in name or "node_modules" in name:
            continue
        picked.append(ROOT / name)
    assert picked, "git ls-files returned nothing under the shipping prefixes"
    return picked


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_retired_vendor_modules_are_gone():
    assert not (ROOT / "src" / "api" / "brand24.py").exists()
    assert not (ROOT / "src" / "api" / "naturalize.py").exists()
    assert not (ROOT / "tests" / "unit" / "test_brand24_citation_refs.py").exists()


def test_shipping_tree_names_no_retired_vendor():
    hits = []
    for path in _tracked_files():
        text = _read(path)
        for i, line in enumerate(text.splitlines(), start=1):
            if RETIRED_VENDOR.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:120]}")
    assert hits == [], "\n".join(hits)


def test_shipping_tree_names_no_keyword_method():
    hits = []
    for path in _tracked_files():
        text = _read(path)
        for i, line in enumerate(text.splitlines(), start=1):
            if RETIRED_METHOD.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:120]}")
            for pattern in LITERAL_KEYWORD_ARRAYS:
                if pattern.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:120]}")
            lowered = line.lower()
            for marker in NOISE_LIST_MARKERS:
                if marker.lower() in lowered:
                    hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:120]}")
    assert hits == [], "\n".join(hits)


def test_shipping_tree_names_no_retired_route():
    hits = []
    for path in _tracked_files():
        text = _read(path)
        for i, line in enumerate(text.splitlines(), start=1):
            if any(route in line for route in RETIRED_ROUTES):
                hits.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:120]}")
    assert hits == [], "\n".join(hits)


def test_api_exposes_no_retired_vendor_routes():
    from src.api import main

    paths = {getattr(route, "path", "") for route in main.app.routes}
    for retired in RETIRED_ROUTES:
        assert retired not in paths
    # The live feed and the desk stay: both read the pipeline tables.
    assert "/api/intel/mentions" in paths
    assert "/api/desk" in paths


def test_frontend_suggestions_come_from_the_completed_run():
    source = _read(ROOT / "frontend" / "src" / "suggestions.js")
    assert "dynamic_discovery" in source
    assert "signal_name" in source
    for consumer in ("views.jsx", "chat.jsx", "seedpath.jsx"):
        text = _read(ROOT / "frontend" / "src" / consumer)
        assert "runSuggestions" in text, consumer
