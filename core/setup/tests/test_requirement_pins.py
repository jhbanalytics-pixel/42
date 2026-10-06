"""The API, agent and jobs images run the same core code, so a package they share carries one exact pin."""
import re
from itertools import combinations
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
# The jobs image installs core/understand/requirements.txt with requirements-jobs.txt in one resolve.
FILES = {"api": ROOT / "core/api/requirements.txt", "agent": ROOT / "core/agent/requirements.txt",
         "jobs": ROOT / "core/setup/requirements-jobs.txt", "understand": ROOT / "core/understand/requirements.txt"}


def pins(path):
    """Package name (lower case, extras dropped) to its requirement spec, for every non-comment line."""
    out = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)", ln)
        assert m, ln
        out[m.group(1).lower().replace("_", "-")] = m.group(3).replace(" ", "")
    return out


def test_api_pins_the_web_framework_exactly_at_the_versions_the_tests_run_against():
    api = pins(FILES["api"])
    assert api.get("fastapi") == "==0.142.2"
    assert api.get("starlette") == "==1.7.0"


def test_api_requirements_are_all_exact_pins():
    api = pins(FILES["api"])
    assert all(re.fullmatch(r"==[0-9][0-9A-Za-z.]*", spec) for spec in api.values()), api


@pytest.mark.parametrize("a,b", list(combinations(FILES, 2)))
def test_a_package_shared_between_two_images_has_the_same_exact_pin(a, b):
    pa, pb = pins(FILES[a]), pins(FILES[b])
    shared = sorted(set(pa) & set(pb))
    differ = {n: (pa[n], pb[n]) for n in shared if pa[n] != pb[n] or not pa[n].startswith("==")}
    assert not differ, f"{a} and {b} disagree: {differ}"
