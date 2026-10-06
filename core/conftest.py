"""Shared fixtures for the core tests."""
import sys

import pytest

from core.config import caps as caps_config

# MODEL_DAILY_USD as it stood until 3 October 2026: USD 20, with USD 80 on 1 and 2 October SAST. The investigation,
# Coverage, health, Ask cutover and staging-check cutover tests do their arithmetic against this schedule, including
# the SAST expiry of the window, so they read it from here rather than from the live core/config/caps.yaml.
OLD_MODEL_CAP_SCHEDULE = ('MODEL_DAILY_USD:\n  default: 20\n  temporary:\n    amount: 80\n'
                          '    starts_on: "2026-10-01"\n    ends_on: "2026-10-02"\n')


@pytest.fixture
def old_model_cap_schedule(monkeypatch, tmp_path):
    path = tmp_path / "caps.yaml"
    path.write_text(OLD_MODEL_CAP_SCHEDULE, encoding="utf-8")
    real = caps_config.model_daily_usd

    def read(_path=None, *, now=None):
        return real(path, now=now)

    monkeypatch.setattr(caps_config, "model_daily_usd", read)
    return path


@pytest.fixture(autouse=True)
def no_unrecorded_ask_spend():
    """Tests run asks without writing their runs rows, so each starts with no finished ask's spend still held."""
    yield
    ask = sys.modules.get("core.agent.ask")
    if ask is not None and hasattr(ask, "_UNRECORDED"):
        with ask._SPEND_LOCK:
            ask._UNRECORDED.clear()
