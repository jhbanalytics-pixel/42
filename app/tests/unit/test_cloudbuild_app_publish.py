"""Pins for the reviewed Cloud Build configuration that publishes the app image."""

from __future__ import annotations

import ast
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

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "app"
PUBLISH = ROOT / "cloudbuild.app.publish.yaml"
ENGINE_STAGING = ROOT / "engine" / "cloudbuild.staging.yaml"
ENGINE_TESTS_DOCKERFILE = ROOT / "engine" / "Dockerfile.tests"
APP_DOCKERFILE = APP / "Dockerfile.general-question"
CONTEXT_BUILDER_DOCKERFILE = ROOT / "ops" / "build" / "Dockerfile.context-builder"
BUN_MANIFEST = ROOT / "ops" / "build" / "bun_runtime.json"
BUN_INSTALLER = ROOT / "ops" / "build" / "install_bun_runtime.py"
BOUNDARY_MANIFEST = (
    ROOT / "ops" / "tests" / "fixtures" / "linux_boundary" / "manifest.json"
)
GATES_DOCKERFILE = ROOT / "ops" / "tests" / "Dockerfile.linux-gates"
CONTEXT_SCRIPT = APP / "scripts" / "build_general_question_context.py"
RELEASE = ROOT / "ops" / "deploy" / "release.py"
PDF_RUNTIME = APP / "configs" / "pdf_runtime.json"
SECCOMP_PROFILE = ROOT / "ops" / "tests" / "fixtures" / "pdf" / "seccomp-profile.json"
SECCOMP_PROFILE_IN_WORKSPACE = "/workspace/ops/tests/fixtures/pdf/seccomp-profile.json"
DOCKER_BUILDER = (
    "gcr.io/cloud-builders/docker@sha256:"
    "e036091375b816e40f07620582d1dfaa0e2c2da58ea5731fdcf560534b2a4a57"
)
PYTHON_IMAGE = (
    "python:3.13-slim@sha256:"
    "16f75ad0fbc6c4883a8afd63b2d700c3cf68ccffc1aaeca5304ca0a3a908451f"
)
STEP_IDS = [
    "build-toolchain",
    "build-context-builder",
    "prepare-context-builder-outputs",
    "linux-gate-prebuild",
    "frontend-install",
    "frontend-build",
    "build-context",
    "build-app-image",
    "context-inventory-inside-image",
    "pdf-seccomp-profile-check",
    "pdf-runtime-smoke",
    "build-linux-gates",
    "verify_linux_boundary_tests",
]
IMAGE_TAG = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/"
    "listening-post-question:${COMMIT_SHA}"
)
TOOLCHAIN_TAG = "42-toolchain:${BUILD_ID}"
BUILDER_TAG = "42-context-builder:${BUILD_ID}"
GATES_TAG = "42-r02-linux-gates:${BUILD_ID}"
REVIEWED_VOLUME = [{"name": "reviewed", "path": "/reviewed"}]
REVIEW_SUBSTITUTION = "${_REVIEW_MANIFEST_SHA256}"
BUILDKIT = ["DOCKER_BUILDKIT=1"]
RECEIPT_KEYS = (
    "REVIEW_DIGEST",
    "ENGINE_BUNDLE_DIGEST",
    "LP_COMMIT",
    "APP_TREE_DIGEST",
    "APP_TREE_FILES",
)
SMOKE_RUN_PREFIX = [
    "run",
    "--rm",
    "--init",
    "--network=none",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,size=268435456",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--security-opt",
    f"seccomp={SECCOMP_PROFILE_IN_WORKSPACE}",
    "--user",
    "10001:10001",
    "--workdir",
    "/app",
    "--env",
    "PDF_RENDERER_NATIVE=1",
    "--env",
    "PDF_RENDERER_PID1_MODE=init",
    "--entrypoint",
    "python",
    IMAGE_TAG,
    "-c",
]
INVENTORY_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")
RELEASE_DOC = ROOT / "docs" / "operations" / "release.md"
APP_SUBMIT = re.compile(
    r"^gcloud builds submit projects/\S+ .*--config=cloudbuild\.app\.publish\.yaml.*$",
    re.MULTILINE,
)
# Cloud Build fills these on every build. Every other built in substitution comes from
# the revision a repository trigger fires on, so a `gcloud builds submit` from a local
# checkout leaves it empty and the caller has to name it on the command line.
SUBMIT_POPULATED_SUBSTITUTIONS = frozenset(
    {"BUILD_ID", "PROJECT_ID", "PROJECT_NUMBER", "LOCATION"}
)


def _publish() -> dict:
    return yaml.safe_load(PUBLISH.read_text(encoding="utf-8"))


def _required_substitutions() -> set[str]:
    """The substitution names the config reads that nothing supplies for it."""
    referenced = set(
        re.findall(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", PUBLISH.read_text(encoding="utf-8")
        )
    )
    return (
        referenced - set(_publish()["substitutions"]) - SUBMIT_POPULATED_SUBSTITUTIONS
    )


def _documented_substitutions(command: str) -> dict[str, str]:
    supplied: dict[str, str] = {}
    for token in shlex.split(command):
        if not token.startswith("--substitutions="):
            continue
        for pair in token.removeprefix("--substitutions=").split(","):
            name, _, value = pair.partition("=")
            supplied[name] = value
    return supplied


def _steps() -> dict[str, dict]:
    return {step["id"]: step for step in _publish()["steps"]}


def _manifest() -> dict:
    return json.loads(BOUNDARY_MANIFEST.read_bytes())


def _gate_command(key: str) -> str:
    return (
        _manifest()["commands"][key]
        .replace("<approved-immutable-image>", IMAGE_TAG)
        .replace("<packet-sha>", "${BUILD_ID}")
    )


def _release_constant(name: str) -> str:
    """Evaluate the plain string constants release.py builds from literals and each other."""
    allowed = (
        ast.Constant,
        ast.JoinedStr,
        ast.FormattedValue,
        ast.BinOp,
        ast.Add,
        ast.Name,
        ast.Load,
    )
    namespace: dict[str, object] = {}
    for node in ast.parse(RELEASE.read_text(encoding="utf-8")).body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and all(isinstance(child, allowed) for child in ast.walk(node.value))
            and all(
                child.id in namespace
                for child in ast.walk(node.value)
                if isinstance(child, ast.Name)
            )
        ):
            continue
        expression = compile(ast.Expression(node.value), str(RELEASE), "eval")
        namespace[node.targets[0].id] = eval(expression, {}, dict(namespace))
    value = namespace[name]
    assert isinstance(value, str)
    return value


def _builder_flags() -> dict[str, bool]:
    flags: dict[str, bool] = {}
    for node in ast.walk(ast.parse(CONTEXT_SCRIPT.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            option = node.args[0].value
            assert isinstance(option, str)
            flags[option] = any(
                keyword.arg == "required" and keyword.value.value is True
                for keyword in node.keywords
            )
    return flags


def _run_inline(
    code: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def _preparation_bash(
    *, platform: str = os.name, local_appdata: str | None = None, which=shutil.which
) -> str | None:
    if platform == "nt":
        candidate = (
            Path(local_appdata or os.environ.get("LOCALAPPDATA", ""))
            / "Programs"
            / "Git"
            / "bin"
            / "bash.exe"
        )
        return str(candidate) if candidate.is_file() else None
    return which("bash")


def test_publish_build_has_only_the_reviewed_top_level_keys() -> None:
    value = _publish()
    assert set(value) == {"steps", "images", "substitutions", "options", "timeout"}
    assert value["options"] == {"logging": "CLOUD_LOGGING_ONLY"}
    assert value["timeout"] == "3600s"
    assert value["substitutions"] == {"_REVIEW_MANIFEST_SHA256": ""}
    for forbidden in ("artifacts", "availableSecrets", "logsBucket"):
        assert forbidden not in value
    keys = {step["id"]: set(step) for step in value["steps"]}
    base = {"id", "name", "args"}
    assert keys == {
        "build-toolchain": base | {"env"},
        "build-context-builder": base | {"env"},
        "prepare-context-builder-outputs": base | {"entrypoint", "volumes"},
        "linux-gate-prebuild": base | {"dir"},
        "frontend-install": base | {"dir"},
        "frontend-build": base | {"dir"},
        "build-context": base | {"volumes"},
        "build-app-image": base | {"env", "volumes"},
        "context-inventory-inside-image": base | {"volumes"},
        "pdf-seccomp-profile-check": base | {"entrypoint"},
        "pdf-runtime-smoke": base,
        "build-linux-gates": base | {"env"},
        "verify_linux_boundary_tests": base | {"entrypoint"},
    }


def test_steps_are_the_reviewed_eleven_in_order_on_pinned_images() -> None:
    value = _publish()
    assert [step["id"] for step in value["steps"]] == STEP_IDS
    builder_steps = {
        "linux-gate-prebuild",
        "frontend-install",
        "frontend-build",
        "build-context",
    }
    for step in value["steps"]:
        expected = (
            TOOLCHAIN_TAG
            if step["id"] == "prepare-context-builder-outputs"
            else BUILDER_TAG
            if step["id"] in builder_steps
            else DOCKER_BUILDER
        )
        assert step["name"] == expected, step["id"]
    for step_id in (
        "build-toolchain",
        "build-context-builder",
        "build-app-image",
        "build-linux-gates",
    ):
        assert value["steps"][STEP_IDS.index(step_id)]["env"] == BUILDKIT
    # The toolchain stage is the engine test image's first stage on the pinned python digest,
    # built with the same arguments the engine staging config uses for its unit-tests step.
    tests_dockerfile = ENGINE_TESTS_DOCKERFILE.read_text(encoding="utf-8")
    assert (
        f"FROM --platform=linux/amd64 {PYTHON_IMAGE} AS test-toolchain\n"
        in tests_dockerfile
    )
    staging = yaml.safe_load(ENGINE_STAGING.read_text(encoding="utf-8"))
    unit_tests = next(step for step in staging["steps"] if step["id"] == "unit-tests")
    assert unit_tests["args"] == [
        "build",
        "--file",
        "Dockerfile.tests",
        "--tag",
        "42-engine-tests:${BUILD_ID}",
        ".",
    ]
    assert value["steps"][0]["args"] == [
        "build",
        "--file",
        "engine/Dockerfile.tests",
        "--target",
        "test-toolchain",
        "--tag",
        TOOLCHAIN_TAG,
        "engine",
    ]
    assert value["steps"][1]["args"] == [
        "build",
        "--file",
        "ops/build/Dockerfile.context-builder",
        "--build-arg",
        f"TOOLCHAIN_IMAGE={TOOLCHAIN_TAG}",
        "--tag",
        BUILDER_TAG,
        "ops/build",
    ]


def test_context_builder_image_derives_from_the_toolchain_and_pins_the_bun_release() -> (
    None
):
    dockerfile = CONTEXT_BUILDER_DOCKERFILE.read_text(encoding="utf-8")
    manifest = json.loads(BUN_MANIFEST.read_bytes())
    assert dockerfile.startswith("ARG TOOLCHAIN_IMAGE\nFROM ${TOOLCHAIN_IMAGE}\n")
    assert dockerfile.count("FROM ") == 1
    assert (
        "COPY install_bun_runtime.py /tmp/bun-tools/install_bun_runtime.py\n"
        in dockerfile
    )
    assert "COPY bun_runtime.json /tmp/bun-tools/bun_runtime.json\n" in dockerfile
    assert BUN_INSTALLER.is_file()
    assert BUN_MANIFEST.is_file()
    assert (
        f"--manifest-sha256 {hashlib.sha256(BUN_MANIFEST.read_bytes()).hexdigest()}"
        in dockerfile
    )
    assert "--destination /opt/bun" in dockerfile
    assert (
        f'test "$(/opt/bun/bin/bun --version)" = "{manifest["version"]}"' in dockerfile
    )
    assert 'ENV PATH="/opt/bun/bin:/opt/42-ci/node:${PATH}"' in dockerfile
    assert "ENV NODE_BINARY=/opt/42-ci/node/node" in ENGINE_TESTS_DOCKERFILE.read_text(
        encoding="utf-8"
    )
    assert 'ENV HOME="/home/testuser"' in dockerfile
    assert 'ENV XDG_CACHE_HOME="/home/testuser/.cache"' in dockerfile
    assert 'ENV BUN_INSTALL="/opt/bun"' in dockerfile
    assert "git config --system --add safe.directory /workspace" in dockerfile
    assert dockerfile.rstrip().endswith("USER 10001:10001")
    assert manifest["url"].endswith(f"/bun-v{manifest['version']}/bun-linux-x64.zip")


def test_frontend_steps_build_the_ignored_dist_from_the_locked_dependencies() -> None:
    steps = _steps()
    assert steps["frontend-install"]["dir"] == "app/frontend"
    assert steps["frontend-install"]["args"] == ["bun", "install", "--frozen-lockfile"]
    assert steps["frontend-build"]["dir"] == "app/frontend"
    assert steps["frontend-build"]["args"] == ["bun", "run", "build"]
    assert (APP / "frontend" / "bun.lock").is_file()
    vite = next((APP / "frontend").glob("vite.config.*")).read_text(encoding="utf-8")
    assert "outDir: '../web/dist'" in vite
    assert "web/dist/\n" in (APP / ".gitignore").read_text(encoding="utf-8")
    sys.path.insert(0, str(APP / "scripts"))
    try:
        import build_general_question_context as builder
    finally:
        sys.path.pop(0)
    assert "web/dist/index.html" in builder._REQUIRED["lp"]
    assert "web/dist" in builder._ROOTS["lp"]


def test_root_preparation_is_limited_to_declared_generated_paths() -> None:
    steps = _steps()
    prepare = steps["prepare-context-builder-outputs"]
    assert prepare["name"] == TOOLCHAIN_TAG
    assert prepare["entrypoint"] == "bash"
    assert prepare["volumes"] == REVIEWED_VOLUME
    assert prepare["args"][0] == "-c"
    script = prepare["args"][1]
    assert "set -euo pipefail" in script
    assert "python" not in script
    assert "chown -R" not in script
    assert "safe.directory" not in script
    assert "$$path" in script
    assert '[ -L "$$path" ]' in script
    assert '[ ! -d "$$path" ]' in script
    assert 'parents=("$$workspace"' in script
    assert 'targets=("$$workspace/app/frontend/node_modules"' in script
    assert 'mkdir -p "$$path"' in script
    assert 'chown 10001:10001 "$$path"' in script
    for path in (
        "app/frontend",
        "app/web",
        "app/frontend/node_modules",
        "app/web/dist",
        "/reviewed",
    ):
        assert path in script
    for step_id in (
        "linux-gate-prebuild",
        "frontend-install",
        "frontend-build",
        "build-context",
    ):
        assert STEP_IDS.index("prepare-context-builder-outputs") < STEP_IDS.index(
            step_id
        )


def test_preparation_bash_selects_git_bash_on_windows_and_discovers_bash_elsewhere(
    tmp_path: Path,
) -> None:
    git_bash = tmp_path / "Programs" / "Git" / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.write_bytes(b"")
    assert _preparation_bash(platform="nt", local_appdata=str(tmp_path)) == str(
        git_bash
    )
    assert (
        _preparation_bash(platform="posix", which=lambda name: "/bin/" + name)
        == "/bin/bash"
    )
    assert _preparation_bash(platform="posix", which=lambda _name: None) is None


@pytest.mark.parametrize("fault", ("app_symlink", "late_target_file", "target_symlink"))
def test_root_preparation_prevalidates_the_complete_path_graph_before_mutation(
    tmp_path: Path, fault: str
) -> None:
    bash = _preparation_bash()
    if bash is None:
        pytest.skip("bash is required for the preparation boundary")
    workspace = tmp_path / "workspace"
    reviewed = tmp_path / "reviewed"
    (workspace / "app" / "frontend").mkdir(parents=True)
    (workspace / "app" / "web").mkdir()
    reviewed.mkdir()
    if fault == "app_symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace / "app" / "frontend").rmdir()
        (workspace / "app" / "web").rmdir()
        (workspace / "app").rmdir()
        (workspace / "app").symlink_to(outside, target_is_directory=True)
    elif fault == "late_target_file":
        (workspace / "app" / "web" / "dist").write_text("unsafe", encoding="utf-8")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace / "app" / "frontend" / "node_modules").symlink_to(
            outside, target_is_directory=True
        )
    commands = tmp_path / "commands"
    commands.mkdir()
    for command in ("mkdir", "chown"):
        path = commands / command
        path.write_text(
            '#!/usr/bin/env bash\nprintf "%s %s\\n" "$0" "$*" >> "$CALLS"\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    script = _steps()["prepare-context-builder-outputs"]["args"][1].replace("$$", "$")
    calls = tmp_path / "calls.log"
    result = subprocess.run(
        [bash, "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": commands.as_posix() + ":" + os.environ["PATH"],
            "WORKSPACE_ROOT": workspace.as_posix(),
            "REVIEWED_ROOT": reviewed.as_posix(),
            "CALLS": calls.as_posix(),
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert b"unsafe path" in result.stderr
    assert not calls.exists()


def test_linux_gate_prebuild_runs_the_manifest_prebuild_node_from_the_checkout() -> (
    None
):
    step = _steps()["linux-gate-prebuild"]
    assert step["dir"] == "app"
    assert step["args"] == [
        "/opt/42-engine-test-venv/bin/python",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        _manifest()["prebuild_node_id"],
    ]
    assert step["args"][-1] in _manifest()["commands"]["run_prebuild"]
    assert (
        "python -m venv --copies /opt/42-engine-test-venv"
        in ENGINE_TESTS_DOCKERFILE.read_text(encoding="utf-8")
    )


def test_build_context_invokes_the_builder_with_its_exact_flags_on_the_reviewed_volume() -> (
    None
):
    step = _steps()["build-context"]
    assert step["volumes"] == REVIEWED_VOLUME
    assert step["args"][:2] == ["python", "-c"]
    assert len(step["args"]) == 3
    code = step["args"][2]
    tree = ast.parse(code)
    command = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "command"
    )
    assert isinstance(command, ast.List)
    executable = command.elts[0]
    assert isinstance(executable, ast.Attribute)
    assert isinstance(executable.value, ast.Name)
    assert (executable.value.id, executable.attr) == ("sys", "executable")
    literal = [element.value for element in command.elts[1:]]
    assert literal == [
        "app/scripts/build_general_question_context.py",
        "--engine-root",
        "engine",
        "--lp-root",
        "app",
        "--destination",
        "/reviewed/context",
        "--review-manifest",
        "/reviewed/review.json",
        "--receipt",
        "/reviewed/receipt.json",
    ]
    flags = _builder_flags()
    assert set(literal[1::2]) == set(flags)
    assert all(flags.values())
    assert 'inventory_source(root / "engine", kind="engine")' in code
    assert 'inventory_source(root / "app", kind="lp")' in code
    assert '"allow_reviewed_working_tree": False' in code
    assert '"contract_version": "general_question_context_review_v1"' in code
    assert REVIEW_SUBSTITUTION in code
    assert '"${COMMIT_SHA}"' in code
    assert "subprocess.run(command, check=True)" in code
    for key in RECEIPT_KEYS:
        assert f"{key}=" in code
    assert 'startswith(("src/", "configs/"))' in code


def test_build_app_image_builds_the_reviewed_context_with_the_release_tag() -> None:
    step = _steps()["build-app-image"]
    assert step["volumes"] == REVIEWED_VOLUME
    assert step["args"] == [
        "build",
        "--file",
        "/reviewed/context/Dockerfile.general-question",
        "--tag",
        IMAGE_TAG,
        "/reviewed/context",
    ]
    dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert dockerfile.startswith("# syntax=docker/dockerfile:1.7@sha256:")
    assert "RUN --mount=type=bind,from=build" in dockerfile
    assert dockerfile.count(PYTHON_IMAGE) == 2


def test_image_tag_is_the_release_pin_and_the_only_pushed_image() -> None:
    value = _publish()
    assert value["images"] == [IMAGE_TAG]
    app_image_name = _release_constant("APP_IMAGE_NAME")
    registry = _release_constant("IMAGE_REGISTRY")
    assert app_image_name + ":${COMMIT_SHA}" == IMAGE_TAG
    assert app_image_name.startswith(registry)
    digest_form = f"{app_image_name}@sha256:{'0' * 64}"
    assert re.fullmatch(r"[a-z0-9][a-z0-9./_-]*@sha256:([0-9a-f]{64})", digest_form)


def test_context_inventory_runs_inside_the_image_and_binds_it_to_the_receipt(
    tmp_path: Path,
) -> None:
    step = _steps()["context-inventory-inside-image"]
    assert step["volumes"] == REVIEWED_VOLUME
    assert step["args"][:9] == [
        "run",
        "--rm",
        "--network=none",
        "--env-file",
        "/reviewed/receipt.env",
        "--entrypoint",
        "python",
        IMAGE_TAG,
        "-c",
    ]
    assert len(step["args"]) == 10
    code = step["args"][9]
    assert REVIEW_SUBSTITUTION in code
    assert 'base = pathlib.Path("/app")' in code
    assert '("src", "configs")' in code
    app = tmp_path / "app"
    files = {
        "src/api/main.py": b"print('app')\n",
        "src/api/__init__.py": b"",
        "configs/pdf_runtime.json": b"{}\n",
        "configs/nested/a.json": b"[]\n",
    }
    for name, raw in files.items():
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (app / "web").mkdir()
    (app / "web" / "ignored.txt").write_bytes(b"outside the inventoried roots\n")
    stamp = {"lp_commit": "a" * 40, "engine_bundle_digest": "b" * 64}
    (app / "runtime-build.json").write_text(json.dumps(stamp), encoding="utf-8")
    digests = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    tree = hashlib.sha256()
    for name in sorted(digests):
        tree.update(f"{digests[name]}  {name}\n".encode())
    env = {
        "REVIEW_DIGEST": "c" * 64,
        "ENGINE_BUNDLE_DIGEST": stamp["engine_bundle_digest"],
        "LP_COMMIT": stamp["lp_commit"],
        "APP_TREE_DIGEST": tree.hexdigest(),
        "APP_TREE_FILES": str(len(files)),
    }
    local = code.replace(
        'pathlib.Path("/app")', f"pathlib.Path({json.dumps(app.as_posix())})"
    )
    record_only = _run_inline(local.replace(REVIEW_SUBSTITUTION, ""), env)
    assert record_only.returncode == 0, record_only.stderr
    lines = record_only.stdout.decode("utf-8").splitlines()
    assert lines[0].endswith("expected=record-only")
    summary = lines.pop()
    assert summary.startswith(
        f"context-inventory-inside-image files={len(files)} sha256={tree.hexdigest()}"
    )
    recorded = {}
    for line in lines[1:]:
        match = INVENTORY_LINE.fullmatch(line)
        assert match is not None, line
        recorded[match.group(2)] = match.group(1)
    assert recorded == digests
    reviewed = _run_inline(local.replace(REVIEW_SUBSTITUTION, "c" * 64), env)
    assert reviewed.returncode == 0, reviewed.stderr
    wrong_review = _run_inline(local.replace(REVIEW_SUBSTITUTION, "d" * 64), env)
    assert wrong_review.returncode != 0
    assert b"review manifest digest mismatch" in wrong_review.stderr
    wrong_stamp = _run_inline(
        local.replace(REVIEW_SUBSTITUTION, ""), {**env, "LP_COMMIT": "e" * 40}
    )
    assert wrong_stamp.returncode != 0
    assert b"runtime-build.json does not match" in wrong_stamp.stderr
    (app / "src" / "api" / "main.py").write_bytes(b"print('changed')\n")
    changed = _run_inline(local.replace(REVIEW_SUBSTITUTION, ""), env)
    assert changed.returncode != 0
    assert b"differ from the reviewed inventory" in changed.stderr


def test_pdf_runtime_smoke_renders_inline_html_as_the_non_root_user_and_fails_closed() -> (
    None
):
    step = _steps()["pdf-runtime-smoke"]
    assert step["args"][:25] == SMOKE_RUN_PREFIX
    assert len(step["args"]) == 26
    code = step["args"][25]
    # The run flags are the manifest's run_pdf_init flags, in its order, with the reviewed
    # profile at its workspace path: init as pid 1, the reviewed seccomp profile, no network.
    pdf_run = shlex.split(
        _manifest()["commands"]["run_pdf_init"].replace(
            "<reviewed-seccomp-profile-host-path>", SECCOMP_PROFILE_IN_WORKSPACE
        )
    )
    assert pdf_run[:2] == ["docker", "run"]
    image = pdf_run.index("<immutable-derived-image>")
    assert pdf_run[2:image] == SMOKE_RUN_PREFIX[1:21]
    assert "--init" in SMOKE_RUN_PREFIX
    assert "PDF_RENDERER_PID1_MODE=init" in SMOKE_RUN_PREFIX
    runtime = json.loads(PDF_RUNTIME.read_bytes())
    font = "newsreader-variable-tcB4qQtO.woff2"
    assert runtime["assets"]["fonts"][font]["family"] == "Newsreader"
    assert runtime["assets"]["root"] == "/app/web/dist/assets"
    assert font in code
    assert "font-family:Newsreader" in code
    assert "<p>Bounded Linux renderer proof.</p>" in code
    assert 'config["assets"]["root"]' in code
    assert 'config["browser"]["executable"]' in code
    assert "render_stored_html_pdf(html, asset_root=asset_root)" in code
    assert "PdfExportError" in code
    assert "tests/fixtures" not in code
    assert 'sys.path.insert(0, "/app")' in code
    dockerfile = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert "useradd --create-home --uid 10001 appuser" in dockerfile
    assert "USER appuser\n" in dockerfile
    # Off the image the packaged browser is absent, so the smoke must refuse before rendering.
    local = code.replace(
        'sys.path.insert(0, "/app")',
        f"sys.path.insert(0, {json.dumps(APP.as_posix())})",
    )
    result = _run_inline(local)
    assert result.returncode != 0
    assert b"pdf-runtime-smoke: packaged browser missing" in result.stderr


def test_pdf_seccomp_profile_check_pins_the_reviewed_profile_before_the_smoke(
    tmp_path: Path,
) -> None:
    steps = _steps()
    step = steps["pdf-seccomp-profile-check"]
    assert (
        STEP_IDS.index("pdf-seccomp-profile-check")
        == STEP_IDS.index("pdf-runtime-smoke") - 1
    )
    assert step["entrypoint"] == "bash"
    manifest = _manifest()
    expected = manifest["pdf_security_profile"]["sha256"]
    assert (
        manifest["pdf_security_profile"]["host_path"]
        == "<reviewed-seccomp-profile-host-path>"
    )
    assert hashlib.sha256(SECCOMP_PROFILE.read_bytes()).hexdigest() == expected
    assert isinstance(json.loads(SECCOMP_PROFILE.read_bytes()), dict)
    workspace_path = "/workspace/" + SECCOMP_PROFILE.relative_to(ROOT).as_posix()
    assert workspace_path == SECCOMP_PROFILE_IN_WORKSPACE
    assert step["args"][0] == "-c"
    assert step["args"][2:] == ["pdf-seccomp-profile-check", expected]
    assert len(step["args"]) == 4
    script = step["args"][1]
    lines = script.splitlines()
    assert lines[0] == "set -euo pipefail"
    assert (
        'echo "$$1  ops/tests/fixtures/pdf/seccomp-profile.json" | sha256sum -c -'
        in lines
    )
    assert lines[-1] == 'echo "pdf-seccomp-profile-check sha256=$$1"'
    bash = shutil.which("bash")
    if bash is None or shutil.which("sha256sum") is None:
        pytest.skip("bash and sha256sum are needed to run the check locally")
    # Cloud Build turns $$ into $ before bash sees the script; the check must pass on the
    # checked-in profile and fail closed on any other digest.
    local = script.replace("$$", "$")
    passing = subprocess.run(
        [bash, "-c", local, "pdf-seccomp-profile-check", expected],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert passing.returncode == 0, passing.stderr
    assert expected.encode("ascii") in passing.stdout
    failing = subprocess.run(
        [bash, "-c", local, "pdf-seccomp-profile-check", "0" * 64],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert failing.returncode != 0
    assert b"FAILED" in failing.stdout + failing.stderr


def test_linux_gates_build_and_boundary_gate_run_the_manifest_commands() -> None:
    steps = _steps()
    manifest = _manifest()
    build = shlex.split(_gate_command("build"))
    assert build[0] == "docker"
    assert steps["build-linux-gates"]["args"] == build[1:]
    assert f"APP_RUNTIME_IMAGE={IMAGE_TAG}" in build
    assert GATES_TAG in build
    gates = GATES_DOCKERFILE.read_text(encoding="utf-8")
    assert gates.startswith("ARG APP_RUNTIME_IMAGE\nFROM ${APP_RUNTIME_IMAGE}\n")
    gate = steps["verify_linux_boundary_tests"]
    assert gate["entrypoint"] == "bash"
    manifest_digest = hashlib.sha256(BOUNDARY_MANIFEST.read_bytes()).hexdigest()
    assert gate["args"][0] == "-c"
    assert gate["args"][2:] == ["verify_linux_boundary_tests", manifest_digest]
    assert len(gate["args"]) == 4
    script = gate["args"][1]
    lines = script.splitlines()
    assert lines[0] == "set -euo pipefail"
    assert (
        'echo "$$1  ops/tests/fixtures/linux_boundary/manifest.json" | sha256sum -c -'
        in lines
    )
    for key in ("run_35", "run_combined", "run_engine_observer"):
        assert _gate_command(key) in lines, key
    runs = [line for line in lines if line.startswith("docker run ")]
    assert len(runs) == 3
    for line in runs:
        assert "--network=none" in line
        assert "--user 10001:10001" in line
        assert "--security-opt no-new-privileges" in line
        assert GATES_TAG in line
    assert "seccomp=" not in script
    assert manifest["pdf_security_profile"]["host_path"].startswith("<")
    node_ids = shlex.split(_gate_command("run_35"))
    assert node_ids[-len(manifest["linux_node_ids"]) :] == manifest["linux_node_ids"]
    assert lines[-1] == 'echo "verify_linux_boundary_tests gate_sha256=$$1"'


def test_publish_build_references_only_known_substitutions() -> None:
    text = PUBLISH.read_text(encoding="utf-8")
    assert set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)) == {
        "BUILD_ID",
        "COMMIT_SHA",
        "_REVIEW_MANIFEST_SHA256",
    }
    stray = re.findall(
        r"\$(?!\{(?:BUILD_ID|COMMIT_SHA|_REVIEW_MANIFEST_SHA256)\})", text
    )
    assert text.count("$$1") == 4
    assert text.count("$$path") == 12
    assert len(stray) == 58
    assert IMAGE_TAG in text
    assert "listening-post-staging" not in text


def test_release_doc_app_submit_names_every_substitution_the_build_requires() -> None:
    if not RELEASE_DOC.is_file():
        pytest.skip("docs/operations/release.md is not in this tree")
    commands = APP_SUBMIT.findall(RELEASE_DOC.read_text(encoding="utf-8"))
    assert len(commands) == 1, commands
    command = commands[0]
    assert "--dir=" not in command
    revision = re.search(r"--revision=(\S+)", command)
    assert revision is not None, command
    required = _required_substitutions()
    assert required == {"COMMIT_SHA"}, required
    supplied = _documented_substitutions(command)
    assert required <= set(supplied), supplied
    # The image tag and the build-context receipt check both read COMMIT_SHA, and the
    # receipt comes from the checked out tree, so the documented value is the revision.
    assert supplied["COMMIT_SHA"] == revision.group(1)


@pytest.mark.parametrize("path", [PUBLISH, CONTEXT_BUILDER_DOCKERFILE, BUN_MANIFEST])
def test_new_build_files_use_unix_line_endings(path: Path) -> None:
    raw = path.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
