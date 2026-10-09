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


def _run_after_user(text):
    """The RUN instructions that come after USER 10001, with continuation lines joined."""
    after = text[text.index("USER 10001") + len("USER 10001"):]
    return [" ".join(m.group(0).replace("\\n", " ").split()) for m in re.finditer(r"^RUN\b(?:.*\\n)*.*$", after, re.M)]


def test_the_build_imports_the_clustering_stack_and_fits_a_tiny_umap_as_the_job_user():
    # f42-understand logged zero clusters from 1 to 4 October with the run finishing ok: first a missing bertopic import,
    # then a numba cache that could not be written. Both only show when the stack is imported and UMAP's numba functions
    # are compiled, so the image build does that as uid 10001 and fails the build, not the job, when it cannot.
    text = DOCKERFILE.read_text(encoding="utf-8")
    runs = [r for r in _run_after_user(text) if "UMAP" in r]
    assert len(runs) == 1, "jobs.Dockerfile has no RUN after USER 10001 that fits UMAP"
    smoke = runs[0]
    assert smoke.startswith("RUN python "), smoke  # it runs the interpreter, and nothing before it can swallow the exit code
    for needle in ("bertopic", "umap", "hdbscan", "numba", ".fit("):
        assert needle in smoke, needle
    assert "|| true" not in smoke and "; true" not in smoke and "exit 0" not in smoke  # a failure must fail the build


def test_the_check_runs_after_the_numba_cache_folder_is_set_and_after_the_code_is_copied():
    text = DOCKERFILE.read_text(encoding="utf-8")
    check = text.index("UMAP(")
    assert text.index("NUMBA_CACHE_DIR=") < check and text.index("COPY core/ /app/core/") < check


def test_the_commit_sha_is_a_build_argument_stamped_into_the_image_for_the_run_stamp():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG GIT_SHA=unknown$", text, re.M)
    assert re.search(r"^ENV F42_GIT_SHA=\$\{?GIT_SHA\}?$", text, re.M)  # core/setup/stamp.py reads F42_GIT_SHA
    # after the pip layers, so a new commit does not rebuild them
    assert text.index("ARG GIT_SHA") > text.index("pip install --no-cache-dir --no-deps bertopic")
