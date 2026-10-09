"""Shared fixtures for the core tests."""
import os
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


def set_locality_authority(monkeypatch, value):
    """Patch the locality authority everywhere it has been read into a name: the constant itself, detect's copy of it
    and the rule version derived from it, including the default of detect's run() and the brief's copy of that version.
    The brief's version is detect's version followed by the brief's own rule tokens, so only the detect prefix is
    swapped and the tokens after it stay. Modules not imported yet read the patched constant when they are."""
    from core.trust import locality

    monkeypatch.setattr(locality, "LOCALITY_AUTHORITY", value)
    detect = sys.modules.get("core.detect.job")
    if detect is not None:
        version = detect.rule_version_for(value)
        monkeypatch.setattr(detect, "LOCALITY_AUTHORITY", value)
        monkeypatch.setattr(detect, "RULE_VERSION", version)
        monkeypatch.setitem(detect.run.__kwdefaults__, "rule_version", version)
        brief = sys.modules.get("core.brief.job")
        if brief is not None:
            # The prefix is whichever authority's version the brief read when it was imported, which is not always the
            # current one: a module first imported inside a patched test keeps the patched value after the test.
            prefix = next((p for p in map(detect.rule_version_for, ("v1", "v2")) if brief.RULE_VERSION.startswith(p)), None)
            assert prefix is not None, "the brief rule version no longer begins with a detect rule version"
            monkeypatch.setattr(brief, "RULE_VERSION", version + brief.RULE_VERSION[len(prefix):])


@pytest.fixture(autouse=True)
def locality_authority_from_environment(monkeypatch):
    """F42_TEST_LOCALITY_AUTHORITY=v2 runs the whole suite as if the switch had shipped, without touching the
    constant: the proof that no test depends on the shadow default. Unset, the constant is what it is."""
    value = os.environ.get("F42_TEST_LOCALITY_AUTHORITY")
    if value:
        set_locality_authority(monkeypatch, value)
