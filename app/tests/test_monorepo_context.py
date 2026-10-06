import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import build_general_question_context as context
from src.api.question_worker_bundle import verify_bundle

GIT = shutil.which("git")
if GIT is None:
    fallback = Path.home() / "AppData/Local/Programs/Git/cmd/git.exe"
    GIT = str(fallback) if fallback.is_file() else None
if GIT is None or not Path(GIT).is_file():
    raise RuntimeError("git executable required for monorepo context tests")
SOURCE_ROOT = Path(__file__).resolve().parents[2]


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        [GIT, "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def write(root: Path, name: str, data: bytes = b"pass\n") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def monorepo(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Context Test")
    git(root, "config", "user.email", "context@example.invalid")
    engine = root / "engine"
    app = root / "app"
    for name in (
        "src/entry.py",
        "src/prompts/lazy-template.txt",
        "scripts/staging/run_general_question_worker.py",
        "configs/rules.yaml",
        "infra/bigquery_schemas/example.sql",
        "requirements.lock",
    ):
        write(engine, name)
    for name in (
        "src/api/main.py",
        "src/api/pdf_exporter.py",
        "src/api/pdf_renderer_worker.py",
        "configs/scope.yaml",
        "configs/pdf_runtime.json",
        "requirements.txt",
        "requirements.lock",
        "web/dist/index.html",
    ):
        write(app, name)
    for name in (
        "app/src/api/pdf_exporter.py",
        "app/src/api/pdf_renderer_worker.py",
        "app/configs/pdf_runtime.json",
        "app/requirements.lock",
        "ops/build/install_pdf_runtime.py",
        "ops/build/pdf_runtime_dependencies.json",
    ):
        write(root, name, (SOURCE_ROOT / name).read_bytes())
    write(
        app,
        "Dockerfile.general-question",
        (
            Path(__file__).resolve().parents[1] / "Dockerfile.general-question"
        ).read_bytes(),
    )
    git(root, "add", "engine", "app", "ops")
    git(root, "commit", "-m", "fixture")
    return root, engine, app


def review_for(engine: Path, app: Path) -> dict:
    return {
        "contract_version": "general_question_context_review_v1",
        "allow_reviewed_working_tree": False,
        "engine": context.inventory_source(engine, kind="engine"),
        "lp": context.inventory_source(app, kind="lp"),
    }


def test_real_monorepo_subtrees_build_and_verify_version1_context(
    tmp_path: Path,
) -> None:
    _, engine, app = monorepo(tmp_path)
    review = review_for(engine, app)

    receipt = context.build_context(
        engine_root=engine,
        lp_root=app,
        destination=tmp_path / "context",
        review_manifest=review,
    )

    assert set(review["engine"]) == {"commit", "dirty", "files"}
    assert set(review["lp"]) == {"commit", "dirty", "files"}
    assert set(receipt) == {
        "contract_version",
        "engine",
        "lp",
        "engine_bundle_digest",
        "lp_files_digest",
        "review_digest",
        "deployment_ready",
        "commit_role",
    }
    assert (
        tmp_path / "context/engine/src/prompts/lazy-template.txt"
    ).read_bytes() == b"pass\n"
    assert (tmp_path / "context/lp/requirements.lock").read_bytes() == (
        SOURCE_ROOT / "app/requirements.lock"
    ).read_bytes()
    assert (tmp_path / "context/ops/build/install_pdf_runtime.py").read_bytes() == (
        SOURCE_ROOT / "ops/build/install_pdf_runtime.py"
    ).read_bytes()
    assert "ops/build/pdf_runtime_dependencies.json" in review["lp"]["files"]
    stamp = json.loads((tmp_path / "context/runtime-build.json").read_bytes())
    assert verify_bundle(
        (tmp_path / "context/engine").resolve(),
        (tmp_path / "context/runtime-build.json").resolve(),
        expected_lp_commit=stamp["lp_commit"],
        expected_bundle_digest=stamp["engine_bundle_digest"],
    ) == {
        "lp_commit": stamp["lp_commit"],
        "engine_bundle_digest": stamp["engine_bundle_digest"],
    }


@pytest.mark.parametrize("case", ["swapped", "arbitrary", "traversal"])
def test_monorepo_source_must_be_exact_declared_subtree(
    tmp_path: Path, case: str
) -> None:
    root, engine, app = monorepo(tmp_path)
    if case == "swapped":
        source, kind = app, "engine"
    elif case == "arbitrary":
        source, kind = root / "other", "engine"
        source.mkdir()
    else:
        source, kind = root / "engine/../app", "lp"

    with pytest.raises(ValueError, match="source_root_invalid"):
        context.inventory_source(source, kind=kind)


def test_linked_monorepo_parent_is_rejected(tmp_path: Path) -> None:
    root, _, _ = monorepo(tmp_path)
    linked = tmp_path / "linked-repo"
    try:
        linked.symlink_to(root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"links unavailable: {error}")

    with pytest.raises(ValueError, match="source_link_invalid"):
        context.inventory_source(linked / "engine", kind="engine")


@pytest.mark.parametrize(
    "linked_name", ["ops", "ops/build", "ops/build/install_pdf_runtime.py"]
)
def test_linked_monorepo_build_input_ancestor_is_rejected(
    tmp_path: Path, linked_name: str
) -> None:
    root, _, app = monorepo(tmp_path)
    selected = root / linked_name
    moved = root / f"real-{selected.name}"
    selected.rename(moved)
    try:
        selected.symlink_to(moved, target_is_directory=moved.is_dir())
    except OSError as error:
        pytest.skip(f"links unavailable: {error}")

    with pytest.raises(ValueError, match="source_link_invalid"):
        context.inventory_source(app, kind="lp")


def test_admitted_file_change_during_inventory_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    _, engine, _ = monorepo(tmp_path)
    selected = engine / "src/entry.py"
    original = context._file_bytes
    reads = 0

    def changing_file(path: Path, name: str) -> bytes:
        nonlocal reads
        raw = original(path, name)
        if path == selected and reads == 0:
            reads += 1
            selected.write_bytes(b"changed\n")
        return raw

    monkeypatch.setattr(context, "_file_bytes", changing_file)

    with pytest.raises(ValueError, match="source_changed"):
        context.inventory_source(engine, kind="engine")


def test_status_change_during_copy_is_rejected_when_tree_was_already_dirty(
    tmp_path: Path, monkeypatch
) -> None:
    root, engine, app = monorepo(tmp_path)
    write(root, "existing-notes.md")
    review = review_for(engine, app)
    review["allow_reviewed_working_tree"] = True
    assert review["engine"]["dirty"] is True
    original = context._file_bytes
    selected = engine / "src/entry.py"
    reads = 0

    def changing_status(path: Path, name: str) -> bytes:
        nonlocal reads
        raw = original(path, name)
        if path == selected:
            reads += 1
            if reads == 3:
                write(root, "new-notes.md")
        return raw

    monkeypatch.setattr(context, "_file_bytes", changing_status)

    with pytest.raises(ValueError, match="source_changed"):
        context.build_context(
            engine_root=engine,
            lp_root=app,
            destination=tmp_path / "context",
            review_manifest=review,
        )


@pytest.mark.parametrize(
    "path",
    [
        "app/src/api/pdf_exporter.py",
        "app/src/api/pdf_renderer_worker.py",
        "app/configs/pdf_runtime.json",
        "app/requirements.lock",
        "ops/build/install_pdf_runtime.py",
        "ops/build/pdf_runtime_dependencies.json",
    ],
)
def test_monorepo_missing_pdf_packaging_input_is_refused(
    tmp_path: Path, path: str
) -> None:
    _, _, app = monorepo(tmp_path)
    (app.parent / path).unlink()

    with pytest.raises(ValueError, match="source_assets_missing"):
        context.inventory_source(app, kind="lp")
