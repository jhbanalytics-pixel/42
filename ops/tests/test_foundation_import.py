import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from ops.deploy.import_snapshot import (
    capture_git_census,
    verify_frozen_files,
)

GIT = os.environ.get("GIT_EXECUTABLE") or shutil.which("git") or "git"


def _entry(source: str, path: str, raw: bytes) -> dict[str, str]:
    return {
        "source": source,
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _manifest(source: Path, *paths: str) -> dict:
    return {
        "included_roots": {"engine": ["."]},
        "files": [_entry("engine", path, (source / path).read_bytes()) for path in paths],
        "exclusions": [],
    }


def test_valid_immutable_source_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/one.py").write_bytes(b"value = 1\n")
    manifest = _manifest(tmp_path, "nested/one.py")

    verify_frozen_files(manifest, {"engine": tmp_path})
    verify_frozen_files(manifest, {"engine": tmp_path})


def test_source_change_aborts_import(tmp_path: Path) -> None:
    source = tmp_path / "one.py"
    source.write_bytes(b"value = 1\n")
    manifest = _manifest(tmp_path, "one.py")
    verify_frozen_files(manifest, {"engine": tmp_path})

    source.write_bytes(b"value = 2\n")

    with pytest.raises(ValueError, match="source_changed"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_deleted_source_aborts_import(tmp_path: Path) -> None:
    source = tmp_path / "one.py"
    source.write_bytes(b"value = 1\n")
    manifest = _manifest(tmp_path, "one.py")
    source.unlink()

    with pytest.raises(ValueError, match="source_missing"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_new_included_file_aborts_import(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_bytes(b"value = 1\n")
    manifest = _manifest(tmp_path, "one.py")
    (tmp_path / "two.py").write_bytes(b"value = 2\n")

    with pytest.raises(ValueError, match="source_unlisted"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_new_top_level_file_aborts_import(tmp_path: Path) -> None:
    included = tmp_path / "included"
    included.mkdir()
    source = tmp_path / "one.py"
    source.write_bytes(b"value = 1\n")
    manifest = {
        "included_roots": {"engine": ["included"]},
        "top_level_inventory": {
            "engine": {
                "path": ".",
                "entries": [
                    {"name": "included", "type": "directory", "disposition": "included"},
                    {"name": "one.py", "type": "file", "disposition": "included"},
                ],
            }
        },
        "files": [_entry("engine", "one.py", source.read_bytes())],
        "exclusions": [],
    }
    verify_frozen_files(manifest, {"engine": tmp_path})
    (tmp_path / "two.py").write_bytes(b"value = 2\n")

    with pytest.raises(ValueError, match="source_unlisted"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_partial_root_requires_inventory_before_file_bytes_are_read(tmp_path: Path) -> None:
    included = tmp_path / "included"
    included.mkdir()
    (included / "one.py").write_bytes(b"value = 1\n")
    (tmp_path / "rogue.py").write_bytes(b"value = 2\n")
    manifest = {
        "included_roots": {"engine": ["included"]},
        "files": [_entry("engine", "included/one.py", b"wrong bytes\n")],
        "exclusions": [],
    }

    with pytest.raises(ValueError, match="source_top_level_inventory_missing"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_top_level_inventory_boundary_must_cover_selected_roots(tmp_path: Path) -> None:
    included = tmp_path / "included"
    included.mkdir()
    source = included / "one.py"
    source.write_bytes(b"value = 1\n")
    (tmp_path / "other").mkdir()
    manifest = {
        "included_roots": {"engine": ["included"]},
        "top_level_inventory": {"engine": {"path": "other", "entries": []}},
        "files": [_entry("engine", "included/one.py", source.read_bytes())],
        "exclusions": [],
    }

    with pytest.raises(ValueError, match="source_top_level_inventory_invalid"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_linked_included_file_aborts_import(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_bytes(b"value = 1\n")
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"links unavailable: {error}")
    manifest = {
        "included_roots": {"engine": ["."]},
        "files": [_entry("engine", "linked.py", target.read_bytes())],
        "exclusions": [_entry("engine", "target.py", target.read_bytes()) | {"reason": "fixture"}],
    }

    with pytest.raises(ValueError, match="source_link"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_listed_directory_aborts_import(tmp_path: Path) -> None:
    directory = tmp_path / "one.py"
    directory.mkdir()
    manifest = {
        "included_roots": {"engine": ["."]},
        "files": [_entry("engine", "one.py", b"")],
        "exclusions": [],
    }

    with pytest.raises(ValueError, match="source_type_changed"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_linked_parent_aborts_import(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "one.py").write_bytes(b"value = 1\n")
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"links unavailable: {error}")
    manifest = _manifest(target, "one.py")

    with pytest.raises(ValueError, match="source_link"):
        verify_frozen_files(manifest, {"engine": linked_parent})


def test_case_colliding_paths_abort_import(tmp_path: Path) -> None:
    source = tmp_path / "one.py"
    source.write_bytes(b"value = 1\n")
    manifest = _manifest(tmp_path, "one.py")
    manifest["files"].append(_entry("engine", "ONE.py", source.read_bytes()))

    with pytest.raises(ValueError, match="source_case_collision"):
        verify_frozen_files(manifest, {"engine": tmp_path})


@pytest.mark.parametrize("path", ["../one.py", "/one.py", "C:/one.py", "nested\\one.py"])
def test_unsafe_paths_abort_import(tmp_path: Path, path: str) -> None:
    manifest = {
        "included_roots": {"engine": ["."]},
        "files": [_entry("engine", path, b"value = 1\n")],
        "exclusions": [],
    }

    with pytest.raises(ValueError, match="source_path_unsafe"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_exclusion_requires_a_reason(tmp_path: Path) -> None:
    (tmp_path / "ignored.txt").write_bytes(b"ignored\n")
    manifest = {
        "included_roots": {"engine": ["."]},
        "files": [],
        "exclusions": [{"source": "engine", "path": "ignored.txt", "reason": ""}],
    }

    with pytest.raises(ValueError, match="source_exclusion_invalid"):
        verify_frozen_files(manifest, {"engine": tmp_path})


def test_corrupted_git_command_result_is_rejected(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    def corrupt(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"\xff", b"")

    monkeypatch.setattr(subprocess, "run", corrupt)

    with pytest.raises(ValueError, match="git_output_not_utf8"):
        capture_git_census("engine", root, tmp_path / "evidence", git_executable=GIT)
