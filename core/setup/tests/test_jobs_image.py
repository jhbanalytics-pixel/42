"""The jobs image runs as uid 10001 with no home and a root-owned site-packages, so a library that caches compiled
code must be pointed at a folder that user can write."""
import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "jobs.Dockerfile"


def test_numba_caches_under_tmp_because_site_packages_and_home_are_not_writable():
    # UMAP's numba functions use cache=True. Without a writable cache folder f42-understand's clustering fails in
    # every market with "cannot cache function 'rdist': no locator available" (runs 2026-10-03 and 2026-10-04).
    text = DOCKERFILE.read_text(encoding="utf-8")
    env = re.search(r"^ENV\b.*\bNUMBA_CACHE_DIR=(\S+)", text, re.M)
    assert env, "jobs.Dockerfile sets no NUMBA_CACHE_DIR"
    assert env.group(1).startswith("/tmp/"), env.group(1)
    assert text.index(env.group(0)) < text.index("USER 10001")
