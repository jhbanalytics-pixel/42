"""Pins for the reviewed Cloud Build configuration that publishes the engine image."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / "cloudbuild.publish.yaml"
STAGING = ROOT / "cloudbuild.staging.yaml"
STAGING_DOCKERFILE = ROOT / "Dockerfile.staging"
ORIGINS = ROOT / "configs" / "open_intelligence" / "execution_origins_v1.json"
PACKAGED_MANIFEST = ROOT / "configs" / "open_intelligence" / "resource_manifest_v1.json"
OPS_TREE = ROOT.parent / "ops"
RUNTIME_TREE = ROOT.parent / "infra" / "runtime"
MANAGED_RUNTIME = OPS_TREE / "runners" / "managed_runtime.py"
DOCKER_BUILDER = (
    "gcr.io/cloud-builders/docker@sha256:"
    "e036091375b816e40f07620582d1dfaa0e2c2da58ea5731fdcf560534b2a4a57"
)
STEP_IDS = [
    "unit-tests",
    "repository-tests",
    "stage-ops-package",
    "build-staging-image",
    "manifest-inside-image",
    "ops-tree-inventory",
]
DOCKER_BUILD_STEP_IDS = ["unit-tests", "build-staging-image"]
TEST_IMAGE_TAG = "42-engine-tests:${BUILD_ID}"
REPOSITORY_STEP_ID = "repository-tests"
REPOSITORY_TESTS_ARGS = [
    "/opt/42-engine-test-venv/bin/python",
    "-m",
    "pytest",
    "-q",
    "-p",
    "no:cacheprovider",
    "tests/repository/",
]
# The checks the engine build context cannot carry, and the module each one lives in now.
CHECKOUT_BOUND_TESTS = {
    "tests/repository/test_deployed_daily_job_environment.py": (
        "def test_deployed_daily_job_environment_resolves_the_staging_collection_target("
    ),
    "tests/repository/test_retired_brand24_references.py": (
        "def test_the_scan_covers_the_shipped_build_and_bindings("
    ),
    "tests/repository/test_read_ingest_run_digests.py": (
        "def test_expected_digests_are_the_ingest_job_definition_and_entry_point_profile("
    ),
}
IMAGE_STEP_IDS = ["manifest-inside-image", "ops-tree-inventory"]
STAGE_STEP_ID = "stage-ops-package"
BUILDKIT = ["DOCKER_BUILDKIT=1"]
IMAGE_TAG = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine:${COMMIT_SHA}"
MANIFEST_IN_IMAGE = "/app/configs/open_intelligence/resource_manifest_v1.json"
INVENTORY_ROOTS = ("/app/configs", "/app/infra", "/app/scripts", "/app/ops")
ENGINE_ROOTS = INVENTORY_ROOTS[:3]
STAGED_TREES = ("ops", "infra/runtime")
SOURCE_INVENTORY_IN_IMAGE = "/app/ops-source-inventory.txt"
STAGE_EXCLUSIONS = (
    "-path ops/tests",
    "-name __pycache__",
    "-name .pytest_cache",
    "-name .ruff_cache",
    "-name .mypy_cache",
    "-name .git",
    "-name .venv",
    "-name venv",
    "! -name '*.pyc'",
)
EXCLUDED_DIRECTORY_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".git",
    ".venv",
    "venv",
}
JOB_MODULES = ("ops/runners/managed_runtime.py", "ops/deploy/refresh_question_policy.py")
RUNTIME_FILES = ("infra/runtime/daily-staging.json", "infra/runtime/scheduler-staging.json")
# The three files managed_runtime.main reads under parents[2] of its module, /app in the image.
MANAGED_RUNTIME_READS = (
    "/app/infra/runtime/daily-staging.json",
    "/app/infra/runtime/scheduler-staging.json",
    "/app/ops/deploy/resource_manifest.json",
)
MANIFEST_SUBSTITUTION = "${_RESOURCE_MANIFEST_SHA256}"
SAMPLE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
INVENTORY_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")
SUMMARY_LINE = re.compile(r"^files=(\d+) sha256=([0-9a-f]{64})$")
RELEASE_DOC = ROOT.parent / "docs" / "operations" / "release.md"
ENGINE_SUBMIT = re.compile(
    r"^gcloud builds submit projects/\S+ .*--config=engine/cloudbuild\.publish\.yaml.*$",
    re.MULTILINE,
)
# Cloud Build fills these on every build. Every other built in substitution comes from the
# revision a repository trigger fires on, so a `gcloud builds submit` from a local checkout
# leaves it empty and the caller has to name it on the command line.
SUBMIT_POPULATED_SUBSTITUTIONS = frozenset({"BUILD_ID", "PROJECT_ID", "PROJECT_NUMBER", "LOCATION"})


def _publish() -> dict:
    return yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))


def _required_substitutions() -> set[str]:
    """The substitution names the config reads that nothing supplies for it."""
    referenced = set(
        re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", PUBLISH.read_text(encoding="utf-8"))
    )
    return referenced - set(_publish()["substitutions"]) - SUBMIT_POPULATED_SUBSTITUTIONS


def _documented_substitutions(command: str) -> dict[str, str]:
    supplied: dict[str, str] = {}
    for token in shlex.split(command):
        if not token.startswith("--substitutions="):
            continue
        for pair in token.removeprefix("--substitutions=").split(","):
            name, _, value = pair.partition("=")
            supplied[name] = value
    return supplied


def _staging() -> dict:
    return yaml.safe_load(STAGING.read_text(encoding="utf-8"))


def _single(values: set[str]) -> str:
    assert len(values) == 1, values
    return next(iter(values))


def _v2_origin_field(name: str) -> str:
    rows = json.loads(ORIGINS.read_text(encoding="utf-8"))["rows"]
    v2 = [
        row for row in rows if row["manifest_version"] == "open_intelligence_execution_manifest_v2"
    ]
    assert v2
    return _single({row[name] for row in v2})


def _step(value: dict, step_id: str) -> dict:
    steps = [step for step in value["steps"] if step["id"] == step_id]
    assert len(steps) == 1, step_id
    return steps[0]


def _image_step_code(step: dict) -> str:
    assert step["args"][:6] == ["run", "--rm", "--entrypoint", "python", IMAGE_TAG, "-c"]
    assert len(step["args"]) == 7
    return step["args"][6]


def _stage_script(value: dict) -> str:
    step = _step(value, STAGE_STEP_ID)
    assert step["entrypoint"] == "bash"
    assert step["args"][0] == "-c"
    assert len(step["args"]) == 2
    return step["args"][1]


def _run_inline(code: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)


def _bash() -> str | None:
    found = shutil.which("bash")
    if found is not None and "system32" not in found.lower():
        return found
    candidates = []
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(
            Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe"
        )
    if os.environ.get("PROGRAMFILES"):
        candidates.append(Path(os.environ["PROGRAMFILES"]) / "Git" / "bin" / "bash.exe")
    return next((str(path) for path in candidates if path.is_file()), None)


def _staged_files(engine: Path) -> list[Path]:
    return sorted(
        (path for tree in STAGED_TREES for path in (engine / tree).rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(engine).as_posix(),
    )


def _source_inventory(engine: Path) -> list[str]:
    """The inventory the stage step writes: `sha256  path` lines in byte order, then the summary."""
    lines = []
    digest = hashlib.sha256()
    for path in _staged_files(engine):
        line = f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(engine).as_posix()}"
        lines.append(line)
        digest.update(line.encode("utf-8") + b"\n")
    lines.append(f"files={len(lines)} sha256={digest.hexdigest()}")
    return lines


def _expected_staged_files() -> dict[str, bytes]:
    expected: dict[str, bytes] = {}
    for tree in (OPS_TREE, RUNTIME_TREE):
        for path in tree.rglob("*"):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            relative = path.relative_to(ROOT.parent)
            if relative.parts[:2] == ("ops", "tests"):
                continue
            if EXCLUDED_DIRECTORY_NAMES & set(relative.parts[:-1]):
                continue
            expected[relative.as_posix()] = path.read_bytes()
    return expected


def _stage_locally(value: dict, tmp_path: Path) -> Path:
    """Run the stage step's script through bash with its workspace paths pointed at tmp_path."""
    bash = _bash()
    if bash is None:
        pytest.skip("bash is not available here")
    if not OPS_TREE.is_dir() or not RUNTIME_TREE.is_dir():
        pytest.skip("the repository ops and infra/runtime trees are not in this checkout")
    engine = tmp_path / "engine"
    engine.mkdir()
    script = (
        _stage_script(value)
        .replace("/workspace/engine", engine.as_posix())
        .replace("/workspace", ROOT.parent.as_posix())
        .replace("/tmp/ops-stage", (tmp_path / "scratch").as_posix())
    )
    script_path = tmp_path / "stage.sh"
    script_path.write_text(script, encoding="utf-8", newline="\n")
    result = subprocess.run(
        [bash, script_path.as_posix()], capture_output=True, check=False, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert b"stage-ops-package staged" in result.stdout
    return engine


def _inventory_code_for(value: dict, engine: Path) -> str:
    code = _image_step_code(_step(value, "ops-tree-inventory"))
    for root in ENGINE_ROOTS:
        code = code.replace(f'"{root}"', json.dumps((ROOT / root.removeprefix("/app/")).as_posix()))
    for tree in STAGED_TREES:
        code = code.replace(f'"/app/{tree}"', json.dumps((engine / tree).as_posix()))
    return code.replace(
        f'"{SOURCE_INVENTORY_IN_IMAGE}"',
        json.dumps((engine / "ops-source-inventory.txt").as_posix()),
    )


def _write_fake_staged_trees(engine: Path) -> None:
    (engine / "ops" / "deploy").mkdir(parents=True)
    (engine / "ops" / "runners").mkdir()
    (engine / "infra" / "runtime").mkdir(parents=True)
    (engine / "infra" / "runtime" / "daily-staging.json").write_bytes(b'{"jobs": {}}\n')
    (engine / "infra" / "runtime" / "scheduler-staging.json").write_bytes(b'{"schedulers": {}}\n')
    (engine / "ops" / "deploy" / "resource_guard.py").write_bytes(b"GUARD = True\n")
    (engine / "ops" / "deploy" / "a-b.json").write_bytes(b"{}\n")
    (engine / "ops" / "deploy" / "a").mkdir()
    (engine / "ops" / "deploy" / "a" / "b.json").write_bytes(b"[]\n")
    (engine / "ops" / "runners" / "__init__.py").write_bytes(b"")
    (engine / "ops-source-inventory.txt").write_text(
        "\n".join(_source_inventory(engine)) + "\n", encoding="utf-8", newline="\n"
    )


def test_publish_build_has_only_the_reviewed_top_level_keys() -> None:
    value = _publish()
    assert set(value) == {"steps", "images", "substitutions", "options", "timeout"}
    assert value["options"] == {"logging": "CLOUD_LOGGING_ONLY"}
    assert value["timeout"] == "1800s"
    for forbidden in ("artifacts", "availableSecrets", "logsBucket"):
        assert forbidden not in value
    for step in value["steps"]:
        keys = {"id", "name", "args"}
        if step["id"] in DOCKER_BUILD_STEP_IDS:
            keys.add("env")
        if step["id"] == STAGE_STEP_ID:
            keys.add("entrypoint")
        assert set(step) == keys, step["id"]


def test_publish_build_steps_are_the_reviewed_six_on_the_pinned_builder() -> None:
    value = _publish()
    ids = [step["id"] for step in value["steps"]]
    assert ids == STEP_IDS
    assert ids.index(STAGE_STEP_ID) + 1 == ids.index("build-staging-image")
    # Every step but repository-tests runs on the pinned builder; that one runs in the image
    # unit-tests tagged, which is the only place the engine test virtual environment exists.
    for step in value["steps"]:
        expected = TEST_IMAGE_TAG if step["id"] == REPOSITORY_STEP_ID else DOCKER_BUILDER
        assert step["name"] == expected, step["id"]
    staging_steps = {step["id"]: step for step in _staging()["steps"]}
    assert staging_steps["unit-tests"]["name"] == DOCKER_BUILDER
    assert _step(value, "unit-tests")["args"] == staging_steps["unit-tests"]["args"]
    assert _step(value, "unit-tests")["args"][:3] == ["build", "--file", "Dockerfile.tests"]
    assert _step(value, "unit-tests")["args"][-3:] == ["--tag", TEST_IMAGE_TAG, "."]
    assert _step(value, "build-staging-image")["args"] == [
        "build",
        "--file",
        "Dockerfile.staging",
        "--tag",
        IMAGE_TAG,
        ".",
    ]


def test_repository_tests_run_the_checkout_bound_checks_before_anything_is_staged() -> None:
    """The checks the engine build context cannot carry, three pinned here, run over the checkout.

    unit-tests builds the test image with the engine tree as its docker context, so the suite
    inside that image has no repository above /workspace/engine. The pinned checks read the root
    infra/runtime daily and ingest job definitions and scan the app, docs, infra and ops trees,
    so they live in tests/repository and run in the image over the workspace, before
    stage-ops-package puts anything into the engine tree and before any image is built or pushed.
    """
    value = _publish()
    ids = [step["id"] for step in value["steps"]]
    assert ids.index("unit-tests") + 1 == ids.index(REPOSITORY_STEP_ID)
    assert ids.index(REPOSITORY_STEP_ID) + 1 == ids.index(STAGE_STEP_ID)
    step = _step(value, REPOSITORY_STEP_ID)
    assert step["name"] == TEST_IMAGE_TAG
    assert step["args"] == REPOSITORY_TESTS_ARGS
    assert "dir" not in step
    assert (ROOT / "tests" / "repository" / "__init__.py").is_file()
    unit = sorted((ROOT / "tests" / "unit").glob("*.py"))
    assert unit
    unit_text = "".join(path.read_text(encoding="utf-8") for path in unit)
    # The signature at the start of a line is the definition itself, not this pin's own copy.
    for name, signature in CHECKOUT_BOUND_TESTS.items():
        module = ROOT / name
        assert module.is_file(), name
        assert "\n" + signature in module.read_text(encoding="utf-8"), name
        assert "\n" + signature not in unit_text, name


def test_docker_build_steps_run_under_buildkit_and_no_other_step_carries_an_env() -> None:
    value = _publish()
    steps = {step["id"]: step for step in value["steps"]}
    for step_id in DOCKER_BUILD_STEP_IDS:
        assert steps[step_id]["args"][0] == "build", step_id
        assert steps[step_id]["env"] == BUILDKIT, step_id
    for step_id in IMAGE_STEP_IDS:
        assert steps[step_id]["args"][0] == "run", step_id
        assert "env" not in steps[step_id], step_id
    assert steps[STAGE_STEP_ID]["entrypoint"] == "bash"
    assert "env" not in steps[STAGE_STEP_ID]
    assert "env" not in steps[REPOSITORY_STEP_ID]
    # The test Dockerfile needs BuildKit: its syntax directive and the network flag on
    # its RUN line are what the legacy builder rejected as "Unknown flag: network".
    tests_dockerfile = (ROOT / "Dockerfile.tests").read_text(encoding="utf-8").splitlines()
    assert tests_dockerfile[0].startswith("# syntax=docker/dockerfile:")
    assert any(line.startswith("RUN --network=none ") for line in tests_dockerfile)


def test_stage_step_copies_both_trees_into_the_engine_context_with_the_reviewed_exclusions() -> (
    None
):
    script = _stage_script(_publish())
    assert script.startswith("set -euo pipefail\n")
    # No `$` at all: the bytes in the config are the bytes bash runs, with no
    # substitution escaping between the two.
    assert "$" not in script
    assert "test -d /workspace/ops\n" in script
    assert "test -d /workspace/infra/runtime\n" in script
    for staged in ("ops", "infra/runtime", "ops-source-inventory.txt"):
        assert f"test ! -e /workspace/engine/{staged}\n" in script, staged
    assert "cd /workspace\n" in script
    assert "find ops infra/runtime " in script
    assert "cp --parents --target-directory /workspace/engine" in script
    for exclusion in STAGE_EXCLUSIONS:
        assert exclusion in script, exclusion
    for staged in (*JOB_MODULES, *RUNTIME_FILES, "ops/deploy/resource_manifest.json"):
        assert f"grep -qx {staged} " in script, staged
    assert "LC_ALL=C sort" in script
    assert "sha256sum --text" in script
    assert "> /workspace/engine/ops-source-inventory.txt\n" in script
    assert "rm -rf /workspace" not in script


def test_publish_image_tag_matches_the_v2_origin_and_is_the_only_pushed_image() -> None:
    value = _publish()
    assert value["images"] == [IMAGE_TAG]
    repository = _v2_origin_field("image_repository")
    assert repository + ":${COMMIT_SHA}" == IMAGE_TAG
    tagged = IMAGE_TAG.replace("${COMMIT_SHA}", SAMPLE_COMMIT)
    assert re.fullmatch(_v2_origin_field("image_name"), tagged) is not None
    assert re.fullmatch(_v2_origin_field("image_name"), IMAGE_TAG) is None
    digest_form = f"{repository}@sha256:{'0' * 64}"
    assert re.fullmatch(_v2_origin_field("image_uri_regex"), digest_form) is not None


def test_publish_build_references_only_known_substitutions() -> None:
    text = PUBLISH.read_text(encoding="utf-8")
    assert set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)) == {
        "BUILD_ID",
        "COMMIT_SHA",
        "_RESOURCE_MANIFEST_SHA256",
    }
    assert re.findall(r"\$(?!\{(?:BUILD_ID|COMMIT_SHA|_RESOURCE_MANIFEST_SHA256)\})", text) == []


def test_manifest_check_runs_inside_the_image_against_the_packaged_digest() -> None:
    value = _publish()
    packaged = hashlib.sha256(PACKAGED_MANIFEST.read_bytes()).hexdigest()
    assert value["substitutions"] == {"_RESOURCE_MANIFEST_SHA256": packaged}
    code = _image_step_code(_step(value, "manifest-inside-image"))
    assert MANIFEST_SUBSTITUTION in code
    assert MANIFEST_IN_IMAGE in code
    local = code.replace(MANIFEST_IN_IMAGE, PACKAGED_MANIFEST.as_posix())
    passing = _run_inline(local.replace(MANIFEST_SUBSTITUTION, packaged))
    assert passing.returncode == 0, passing.stderr
    assert packaged.encode("ascii") in passing.stdout
    failing = _run_inline(local.replace(MANIFEST_SUBSTITUTION, "0" * 64))
    assert failing.returncode != 0
    assert b"mismatch" in failing.stderr


def test_ops_tree_inventory_hashes_the_four_roots_and_binds_both_staged_trees(
    tmp_path: Path,
) -> None:
    value = _publish()
    code = _image_step_code(_step(value, "ops-tree-inventory"))
    for root in INVENTORY_ROOTS:
        assert f'"{root}"' in code
    for tree in STAGED_TREES:
        assert f'"/app/{tree}"' in code
    assert f'"{SOURCE_INVENTORY_IN_IMAGE}"' in code
    engine = tmp_path / "engine"
    _write_fake_staged_trees(engine)
    expected: dict[str, str] = {}
    for root in INVENTORY_ROOTS:
        base = ROOT / root.removeprefix("/app/") if root in ENGINE_ROOTS else engine / "ops"
        for path in base.rglob("*"):
            if path.is_file():
                expected[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert expected
    local = _inventory_code_for(value, engine)
    result = _run_inline(local)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.decode("utf-8").splitlines()
    bound = lines.pop()
    assert bound.startswith("ops-tree-inventory ")
    assert " match " in bound
    assert "across 7 lines" in bound
    summary = lines.pop()
    assert summary.startswith(f"ops-tree-inventory files={len(expected)} sha256=")
    recorded = {}
    for line in lines:
        match = INVENTORY_LINE.fullmatch(line)
        assert match is not None, line
        recorded[match.group(2)] = match.group(1)
    assert recorded == expected
    missing_root = _run_inline(
        code.replace('"/app/infra"', json.dumps((ROOT / "no-such-tree").as_posix()))
    )
    assert missing_root.returncode != 0
    # One changed byte, one extra file, a missing staged tree and a missing inventory
    # each fail closed. The runtime files sort first, so the guard is line 5.
    (engine / "ops" / "deploy" / "resource_guard.py").write_bytes(b"GUARD = False\n")
    changed = _run_inline(local)
    assert changed.returncode != 0
    assert b"ops-tree-inventory: line 5 of " in changed.stderr
    (engine / "ops" / "deploy" / "resource_guard.py").write_bytes(b"GUARD = True\n")
    (engine / "ops" / "runners" / "zz.py").write_bytes(b"")
    extra = _run_inline(local)
    assert extra.returncode != 0
    assert b"ops-tree-inventory: line 7 of " in extra.stderr
    (engine / "ops" / "runners" / "zz.py").unlink()
    shutil.move(engine / "infra" / "runtime", tmp_path / "runtime-aside")
    absent_tree = _run_inline(local)
    assert absent_tree.returncode != 0
    assert b"ops-tree-inventory: missing " in absent_tree.stderr
    shutil.move(tmp_path / "runtime-aside", engine / "infra" / "runtime")
    assert _run_inline(local).returncode == 0
    (engine / "ops-source-inventory.txt").unlink()
    absent = _run_inline(local)
    assert absent.returncode != 0
    assert b"ops-tree-inventory: missing " in absent.stderr


def test_stage_script_inventories_the_real_trees_and_the_image_check_accepts_it(
    tmp_path: Path,
) -> None:
    value = _publish()
    engine = _stage_locally(value, tmp_path)
    assert not (ROOT / "ops").exists()
    assert not (ROOT / "infra" / "runtime").exists()
    assert not (ROOT / "ops-source-inventory.txt").exists()
    staged = {
        path.relative_to(engine).as_posix(): path.read_bytes() for path in _staged_files(engine)
    }
    assert staged == _expected_staged_files()
    for name in (*JOB_MODULES, *RUNTIME_FILES):
        assert name in staged, name
    assert not any(
        part in EXCLUDED_DIRECTORY_NAMES or part == "tests"
        for name in staged
        for part in name.split("/")
    )
    assert not any(name.endswith(".pyc") for name in staged)
    lines = (engine / "ops-source-inventory.txt").read_text(encoding="utf-8").splitlines()
    summary = SUMMARY_LINE.fullmatch(lines[-1])
    assert summary is not None, lines[-1]
    assert int(summary.group(1)) == len(lines) - 1 == len(staged)
    digest = hashlib.sha256()
    for line in lines[:-1]:
        match = INVENTORY_LINE.fullmatch(line)
        assert match is not None, line
        assert match.group(2) in staged, line
        assert match.group(1) == hashlib.sha256(staged[match.group(2)]).hexdigest(), line
        digest.update(line.encode("utf-8") + b"\n")
    assert summary.group(2) == digest.hexdigest()
    assert lines == _source_inventory(engine)
    inventoried = {INVENTORY_LINE.fullmatch(line).group(2) for line in lines[:-1]}
    for name in RUNTIME_FILES:
        assert name in inventoried, name
    accepted = _run_inline(_inventory_code_for(value, engine))
    assert accepted.returncode == 0, accepted.stderr
    assert " match " in accepted.stdout.decode("utf-8").splitlines()[-1]
    target = engine / "ops" / "runners" / "managed_runtime.py"
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    rejected = _run_inline(_inventory_code_for(value, engine))
    assert rejected.returncode != 0
    assert b"ops-tree-inventory: line " in rejected.stderr


def test_managed_runtime_reads_only_paths_the_image_copies_carry(tmp_path: Path) -> None:
    if not MANAGED_RUNTIME.is_file():
        pytest.skip("the repository ops tree is not in this checkout")
    source = MANAGED_RUNTIME.read_text(encoding="utf-8")
    assert "root = Path(__file__).resolve().parents[2]" in source
    for read in MANAGED_RUNTIME_READS:
        parts = read.removeprefix("/app/").split("/")
        assert "root / " + " / ".join(json.dumps(part) for part in parts) in source, read
    engine = _stage_locally(_publish(), tmp_path)
    inventoried = {
        INVENTORY_LINE.fullmatch(line).group(2)
        for line in (engine / "ops-source-inventory.txt")
        .read_text(encoding="utf-8")
        .splitlines()[:-1]
    }
    for read in MANAGED_RUNTIME_READS:
        assert read.removeprefix("/app/") in inventoried, read
        assert (engine / read.removeprefix("/app/")).is_file(), read


def test_managed_runtime_accepts_the_tree_runtime_files_and_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not MANAGED_RUNTIME.is_file() or not RUNTIME_TREE.is_dir():
        pytest.skip("the repository ops and infra/runtime trees are not in this checkout")
    monkeypatch.syspath_prepend(str(ROOT.parent))
    for name in [name for name in sys.modules if name == "ops" or name.startswith("ops.")]:
        monkeypatch.delitem(sys.modules, name)
    managed_runtime = __import__("ops.runners.managed_runtime", fromlist=["main"])
    resource_guard = __import__("ops.deploy.resource_guard", fromlist=["load_resource_manifest"])
    config = managed_runtime.load_runtime_configuration(RUNTIME_TREE / "daily-staging.json")
    assert set(config["jobs"]) == {
        "intelligence-42-daily-staging",
        "intelligence-42-price-policy-staging",
    }
    schedulers = managed_runtime.load_scheduler_configuration(
        RUNTIME_TREE / "scheduler-staging.json",
        expected_sha256=config["scheduler_configuration_sha256"],
    )
    assert set(schedulers["schedulers"]) == set(config["jobs"])
    resources = resource_guard.load_resource_manifest(
        OPS_TREE / "deploy" / "resource_manifest.json",
        expected_sha256=config["resource_manifest_sha256"],
    )
    assert resources["project"] == "ogilvy-trends-v2"
    with pytest.raises(ValueError, match=r"^runtime_configuration_invalid$"):
        managed_runtime.load_runtime_configuration(RUNTIME_TREE / "no-such-file.json")


def test_staging_dockerfile_copies_the_staged_ops_package_and_its_inventory() -> None:
    lines = STAGING_DOCKERFILE.read_text(encoding="utf-8").splitlines()
    infra = lines.index("COPY infra/ ./infra/")
    ops = lines.index("COPY ops/ ./ops/")
    inventory = lines.index("COPY ops-source-inventory.txt ./")
    user = lines.index("USER appuser")
    assert infra < ops < inventory < user
    assert lines.count("COPY ops/ ./ops/") == 1
    # The staged infra/runtime rides in on the existing infra copy; no line names it.
    assert not any("runtime" in line for line in lines if line.startswith("COPY "))
    tests_dockerfile = (ROOT / "Dockerfile.tests").read_text(encoding="utf-8")
    assert "COPY ops/" not in tests_dockerfile


def test_staging_cloud_build_is_byte_for_byte_unchanged_at_head() -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not on PATH")
    toplevel = subprocess.run(
        [git, "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True, check=False
    )
    head = subprocess.run(
        [git, "rev-parse", "--verify", "HEAD"], cwd=ROOT, capture_output=True, check=False
    )
    if toplevel.returncode != 0 or head.returncode != 0:
        pytest.skip("git HEAD is unavailable here")
    relative = STAGING.relative_to(
        Path(toplevel.stdout.decode("utf-8").strip()).resolve()
    ).as_posix()
    shown = subprocess.run(
        [git, "show", f"HEAD:{relative}"], cwd=ROOT, capture_output=True, check=False
    )
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout == STAGING.read_bytes()


def test_release_doc_submits_the_engine_directory_of_the_repository_resource() -> None:
    if not RELEASE_DOC.is_file():
        pytest.skip("docs/operations/release.md is not in this tree")
    text = RELEASE_DOC.read_text(encoding="utf-8")
    commands = ENGINE_SUBMIT.findall(text)
    assert len(commands) == 1, commands
    command = commands[0]
    assert " --revision=<sha> " in command
    assert " --dir=engine " in command
    assert " --substitutions=COMMIT_SHA=<sha> " in command
    assert "--git-source-dir" not in command
    assert "`ops-source-inventory.txt`" in text


def test_release_doc_engine_submit_names_every_substitution_the_build_requires() -> None:
    if not RELEASE_DOC.is_file():
        pytest.skip("docs/operations/release.md is not in this tree")
    commands = ENGINE_SUBMIT.findall(RELEASE_DOC.read_text(encoding="utf-8"))
    assert len(commands) == 1, commands
    command = commands[0]
    revision = re.search(r"--revision=(\S+)", command)
    assert revision is not None, command
    required = _required_substitutions()
    assert required == {"COMMIT_SHA"}, required
    supplied = _documented_substitutions(command)
    assert required <= set(supplied), supplied
    # The pushed tag reads COMMIT_SHA and --config is sent from the checkout, so the
    # documented value is the revision the submit names.
    assert supplied["COMMIT_SHA"] == revision.group(1)
