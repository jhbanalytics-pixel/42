import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests/build/ci_git_dependencies.json"
INSTALLER = ROOT / "tests/build/install_ci_tools.py"
MANIFEST_SHA256 = "1f596f0f894e8b25e33f8eff9940d07b59227aa8f59082275b7b828f52f79455"


def installer():
    spec = importlib.util.spec_from_file_location("install_ci_tools", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_is_exact_external_test_only_git_closure():
    value = installer().load_ci_manifest(MANIFEST, MANIFEST_SHA256)

    assert value["contract_version"] == "ci_git_dependency_v1"
    assert value["scope"] == "test_only"
    assert value["package_count"] == len(value["packages"]) == 31
    assert len(value["base_packages"]) == value["base_installed_count"] == 87
    assert value["base_package_changes"] == 0
    assert value["tools"]["git"] == {
        "path": "/usr/bin/git",
        "version_output": "git version 2.47.3",
        "sha256": "356db14e102d68a1a37d8a1ac577dfd678d45d46e92f468bef8b7154e7bfdc60",
    }


def test_changed_manifest_and_wrong_external_pin_refuse(tmp_path: Path):
    raw = MANIFEST.read_bytes()
    changed = tmp_path / "changed.json"
    changed.write_bytes(raw + b"\n")

    with pytest.raises(ValueError, match="ci_manifest_invalid"):
        installer().load_ci_manifest(changed, MANIFEST_SHA256)
    with pytest.raises(ValueError, match="ci_manifest_invalid"):
        installer().load_ci_manifest(MANIFEST, "f" * 64)


def test_wrong_git_package_version_refuses():
    value = json.loads(MANIFEST.read_bytes())
    changed = deepcopy(value)
    next(item for item in changed["packages"] if item["name"] == "git")["version"] = "1:2.47.4-1"

    with pytest.raises(ValueError, match="ci_manifest_invalid"):
        installer().validate_ci_manifest(changed)


@pytest.mark.parametrize("raw", [b"truncated", b"changed archive bytes"])
def test_archive_size_and_hash_drift_refuses(tmp_path: Path, raw: bytes):
    path = tmp_path / "package.deb"
    path.write_bytes(raw)

    with pytest.raises(ValueError, match="ci_archive_invalid"):
        installer().verify_archive(path, size=len(raw) + 1, sha256="f" * 64)


def test_incomplete_installed_inventory_refuses(tmp_path: Path, monkeypatch):
    module = installer()
    manifest = module.load_ci_manifest(MANIFEST, MANIFEST_SHA256)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b""),
    )

    with pytest.raises(ValueError, match="ci_installed_inventory_invalid"):
        module.verify_installed(manifest, tmp_path / "licenses", tmp_path / "out")


def test_missing_git_refuses_after_exact_package_inventory(tmp_path: Path, monkeypatch):
    module = installer()
    manifest = module.load_ci_manifest(MANIFEST, MANIFEST_SHA256)
    rows = [*manifest["base_packages"], *manifest["packages"]]
    stdout = "".join(
        f"{item['name']}\t{item['version']}\t{item['architecture']}\n" for item in rows
    ).encode()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout),
    )

    with pytest.raises(ValueError, match="ci_git_invalid"):
        module.verify_installed(manifest, tmp_path / "licenses", tmp_path / "out")


def test_test_image_retains_fixtures_and_enforces_nonroot_empty_repository():
    docker = (ROOT / "Dockerfile.tests").read_text(encoding="utf-8")
    ignore = (ROOT / "Dockerfile.tests.dockerignore").read_text(encoding="utf-8").splitlines()

    assert "USER 10001:10001" in docker
    assert "git init --quiet --template=" in docker
    assert "safe.directory" not in docker
    assert "git remote" in docker
    assert "git rev-parse --verify HEAD" in docker
    assert "git config --local --get user.name" in docker
    assert "git config --local --get user.email" in docker
    assert "tests/fixtures/socialcrawl_catalog_2026_08_26.json" in docker
    assert "git check-ignore --quiet" in docker
    assert "RUN --network=none" in docker
    assert "tests/fixtures" not in ignore
    assert "*.json" not in ignore
    assert "*.html" not in ignore


def test_test_image_requires_git_and_repository_metadata_before_full_suite():
    docker = (ROOT / "Dockerfile.tests").read_text(encoding="utf-8")
    metadata = docker.index("git rev-parse --show-toplevel")
    suite = docker.index("python -m pytest tests/unit/ -q")

    assert docker.index("git version 2.47.3") < metadata < suite
    assert "test -x /usr/bin/git" in docker
    assert "test -d .git" in docker
