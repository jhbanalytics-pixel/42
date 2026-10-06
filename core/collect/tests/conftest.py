import copy

import pytest

from core.collect import job, sources_sweep
from core.collect.socialcrawl_client import load_caps


@pytest.fixture
def collect_share_2000(monkeypatch):
    """Tests that pin the plan's size describe it under the 2,000 collect share it was measured at, whatever
    caps.yaml sets for the week (job.volume grows the plan with the share)."""
    caps = copy.deepcopy(load_caps())
    caps["ENGINE_DAILY"]["collect"] = 2000
    monkeypatch.setattr(job, "load_caps", lambda *a, **k: copy.deepcopy(caps))
    monkeypatch.setattr(sources_sweep, "load_caps", lambda *a, **k: copy.deepcopy(caps))
