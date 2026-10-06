"""Workbench URL compatibility smoke (Phase 0)."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The browser driven owner check needs a Chrome or Chromium binary. Its path is
# not the same on every machine, so it is resolved here instead of being
# assumed: an explicit CHROME_PATH or CHROME_BIN first, then anything named
# like Chrome on PATH, then the Playwright browser root this repository already
# installs its rendering browser into. Nothing is skipped when none is found;
# the check runs and reports the missing dependency.
_CHROME_COMMANDS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)
_PLAYWRIGHT_CHROME_GLOBS = (
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-win/chrome.exe",
    "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
)


def _playwright_browser_root() -> Path | None:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured and configured != "0":
        root = Path(configured)
        return root if root.is_dir() else None
    for default in (
        Path.home() / ".cache" / "ms-playwright",
        Path.home() / "AppData" / "Local" / "ms-playwright",
        Path.home() / "Library" / "Caches" / "ms-playwright",
    ):
        if default.is_dir():
            return default
    return None


def _resolve_chrome() -> str | None:
    for name in ("CHROME_PATH", "CHROME_BIN"):
        configured = os.environ.get(name)
        if configured and Path(configured).is_file():
            return configured
    for command in _CHROME_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    root = _playwright_browser_root()
    if root is not None:
        for pattern in _PLAYWRIGHT_CHROME_GLOBS:
            for candidate in sorted(root.glob(pattern), reverse=True):
                if candidate.is_file():
                    return str(candidate)
    return None


def _browser_env() -> dict:
    env = dict(os.environ)
    chrome = _resolve_chrome()
    if chrome:
        env["CHROME_PATH"] = chrome
    return env


def test_seed_to_research_source_boundary_has_no_implicit_audience():
    research_lib = (ROOT / 'frontend' / 'src' / 'researchLib.jsx').read_text(encoding='utf-8')
    seeds = (ROOT / 'frontend' / 'src' / 'seeds.jsx').read_text(encoding='utf-8')
    panel = (ROOT / 'frontend' / 'src' / 'ResearchDocPanel.jsx').read_text(encoding='utf-8')

    assert "digital_architect_genz" not in research_lib
    assert "digital_architect_genz" not in seeds
    assert "list[0].id" not in panel
    assert "resolveResearchRouteUpdate(" in panel
    assert "resolveResearchPersonaSelection(id, personas, markets)" in panel
    subprocess.run(
        ['node', str(ROOT / 'scripts' / 'check_console_boundaries.mjs')],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        ['node', str(ROOT / 'scripts' / 'test_explicit_research_generation.mjs')],
        cwd=ROOT,
        check=True,
        env={**_browser_env(), 'RESEARCH_GENERATION_CASE': 'direct-link'},
    )
    assert "disabled={!personaId || phase === 'running' || !markets.length}" in panel


def test_workbench_route_smoke():
    script = ROOT / 'scripts' / 'test_workbench_routes.mjs'
    subprocess.run(['node', str(script)], cwd=ROOT, check=True)


def test_console_boundary_checks():
    script = ROOT / 'scripts' / 'check_console_boundaries.mjs'
    subprocess.run(['node', str(script)], cwd=ROOT, check=True)
