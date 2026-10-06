import os

import pytest

# Some modules pick their model when imported (ask.MODEL, critic.DEFAULT_MODEL, enrich.MODEL), before any fixture
# runs, so the shell's MODEL_PROVIDER is cleared here, as this file loads ahead of the test modules beside it.
os.environ.pop("MODEL_PROVIDER", None)


@pytest.fixture(autouse=True)
def default_provider(monkeypatch):
    """Every test runs on the default provider (gemini) with the shell's MODEL_PROVIDER cleared."""
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)


@pytest.fixture(autouse=True)
def video_reading_off(monkeypatch):
    """VIDEO_DAILY in caps.yaml is above zero, so a job test that is not about video would read clips through its
    fake execute. Every test starts with video reading off; test_video.py sets the caps it needs itself."""
    from core.understand import job

    monkeypatch.setattr(job, "video_daily", lambda: {"clips": 0, "credits": 0})
