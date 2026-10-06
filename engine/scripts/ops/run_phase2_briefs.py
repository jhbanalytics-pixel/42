"""Standalone Phase 2 briefs runner for a Cloud Run job.

Extracted from the inline heredoc in .github/workflows/phase2-briefs.yml.
Reads today's already-populated trend_scores from BigQuery and runs the
trend brief orchestrator against them, persisting results to trend_analysis.
Sends no email.

Inputs come from environment variables (set by the Cloud Run job):
  TREND_DATE_INPUT  ISO date (YYYY-MM-DD). Empty = today UTC.
  TOP_N_INPUT       integer, defaults to 8.
  FORCE_INPUT       "true"/"false", defaults to false.

Credentials come from ADC on Cloud Run, no key file needed. generate_briefs
calls get_client internally.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

# Cloud Run launches this as `python scripts/ops/<name>.py`, so the repo root is
# not on sys.path by default. Put it there before importing src.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.analysis.generate_briefs import generate_briefs


def main() -> int:
    raw = os.environ.get("TREND_DATE_INPUT", "").strip()
    trend_date = date.fromisoformat(raw) if raw else datetime.now(UTC).date()

    top_n = int(os.environ.get("TOP_N_INPUT") or "8")
    force = os.environ.get("FORCE_INPUT", "").lower() == "true"
    model = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash (default)"

    print(
        f"Phase 2 briefs-only run for trend_date={trend_date} "
        f"top_n_per_market={top_n} model={model} force={force}"
    )
    report = generate_briefs(
        trend_date=trend_date,
        top_n_per_market=top_n,
        persist=True,
        force=force,
    )
    print()
    print(f"RESULT: briefs={len(report.briefs)} failures={len(report.failures)}")
    print(
        f"Tokens: prompt={report.total_prompt_tokens} completion={report.total_completion_tokens}"
    )
    print(f"Estimated cost USD: ${report.estimated_cost_usd:.5f}")
    if report.failures:
        print("Failures:")
        for market, topic, err in report.failures:
            print(f"  {market}/{topic}: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
