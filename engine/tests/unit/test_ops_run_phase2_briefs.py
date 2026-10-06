"""Unit tests for scripts/ops/run_phase2_briefs.py.

The script lives in scripts/ops, so it is imported via a sys.path insert and
importlib, matching the run_rss_now fixture pattern in test_wave1_engine_core.py.
generate_briefs is patched on the script's own namespace (it imports the symbol
at module top), so no BigQuery or Vertex access happens.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest


@pytest.fixture
def briefs_module():
    import importlib
    import sys

    sys.path.insert(0, "scripts/ops")
    import run_phase2_briefs  # type: ignore

    importlib.reload(run_phase2_briefs)
    return run_phase2_briefs


class _FakeReport:
    def __init__(self):
        self.briefs = [1, 2, 3]
        self.failures = []
        self.total_prompt_tokens = 1000
        self.total_completion_tokens = 500
        self.estimated_cost_usd = 0.12345


def test_explicit_trend_date(briefs_module):
    env = {"TREND_DATE_INPUT": "2026-06-20", "TOP_N_INPUT": "5"}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(briefs_module, "generate_briefs", return_value=_FakeReport()) as fake,
    ):
        rc = briefs_module.main()

    assert rc == 0
    fake.assert_called_once()
    kwargs = fake.call_args.kwargs
    assert kwargs["trend_date"] == date(2026, 6, 20)
    assert kwargs["top_n_per_market"] == 5
    assert kwargs["persist"] is True
    assert kwargs["force"] is False


def test_empty_trend_date_defaults_to_today(briefs_module):
    env = {"TREND_DATE_INPUT": ""}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(briefs_module, "generate_briefs", return_value=_FakeReport()) as fake,
    ):
        rc = briefs_module.main()

    assert rc == 0
    fake.assert_called_once()
    passed = fake.call_args.kwargs["trend_date"]
    assert isinstance(passed, date)
    # Defaults to top_n 8 when TOP_N_INPUT is absent.
    assert fake.call_args.kwargs["top_n_per_market"] == 8


def test_force_true(briefs_module):
    env = {"TREND_DATE_INPUT": "2026-06-20", "FORCE_INPUT": "true"}
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(briefs_module, "generate_briefs", return_value=_FakeReport()) as fake,
    ):
        rc = briefs_module.main()

    assert rc == 0
    assert fake.call_args.kwargs["force"] is True
