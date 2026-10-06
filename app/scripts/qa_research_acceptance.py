#!/usr/bin/env python3
"""QA acceptance for Console Research trust layer.

Golden mode (default): mocked fixture path, no network.
Live mode (--live): hits real BQ; requires credentials.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

env_path = Path(__file__).resolve().parents[1] / ".env"
try:
    from dotenv import load_dotenv

    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import persona_registry, research  # noqa: E402


def _golden_checks() -> list[str]:
    errors = []
    reg = persona_registry.load_registry()
    if list(reg) != ["audience_neutral"]:
        errors.append("audience_neutral is not the only enabled persona")
    if persona_registry.resolve_persona("hustle economy gen z") is not None:
        errors.append("disabled stub persona resolved (should fail-closed)")
    if not (reg.get("audience_neutral") or {}).get("query_groups"):
        errors.append("audience_neutral has no query groups")
    return errors


def _live_checks() -> list[str]:
    errors = []
    try:
        ev = research.build_research_evidence("audience_neutral", ["ke", "ng"])
        if ev.get("ref_count", 0) < 1:
            errors.append("live evidence returned zero refs")
        if not ev.get("query_groups"):
            errors.append("live evidence missing query_groups")
    except Exception as exc:
        errors.append(f"live build_research_evidence failed: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Run live BQ checks")
    args = parser.parse_args()

    errors = _golden_checks()
    if args.live:
        errors.extend(_live_checks())

    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        return 1

    print("OK: research acceptance checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
