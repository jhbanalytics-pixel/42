import hashlib
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from ops.build import install_pdf_runtime as runtime_installer
from ops.build.install_pdf_runtime import (
    extract_browser_archive,
    load_build_manifest,
    verify_archive,
    verify_debian_archives,
    verify_required_inputs,
)

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "ops/build/pdf_runtime_dependencies.json"
CURRENT_MANIFEST_SHA256 = (
    "2c07e1ddda21cf937259dc05065910a1156dd1cf0492701985b9e06e817f7e01"
)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_shared_build_manifest_contains_only_approved_runtime_pins() -> None:
    manifest = load_build_manifest(MANIFEST, CURRENT_MANIFEST_SHA256)

    assert manifest["contract_version"] == "pdf_runtime_dependencies_v1"
    assert manifest["approval"] == {
        "proposal_sha256": "278b61d22cc3fb8debf7d1ec2dbae3eb8bdf609011c8cdfe6296d1b59ff77351",
        "dependency_manifest_sha256": "c983b80466080683bd0ba8b905dceeac6c36467702584fd5537bfc5c5b75a819",
        "css_safety_decision_sha256": "674364e434ee780effd743a055ccbde67c4ccb108c970a0980a50f28708ca177",
        "css_parser_inventory_sha256": "0d567fa7d1ca3dd32ba9c2ce6e65be1f0fb8ef811a53cbffd19e620d0e139e6f",
    }
    assert manifest["python"] == {
        "playwright": "1.62.0",
        "pypdf": "6.18.1",
        "pyee": "13.0.0",
        "greenlet": "3.5.5",
        "typing-extensions": "4.16.0",
        "tinycss2": "1.5.1",
        "webencodings": "0.6.1",
    }
    assert manifest["browser"]["revision"] == "1234"
    assert manifest["browser"]["archive_sha256"] == (
        "3cfc2bd00d1bafcf8a68dc74c9c92bb7150ddc8d26ade948a776316e1cec4f14"
    )
    assert len(manifest["debian"]["packages"]) == 87
    assert len(manifest["runtime"]["python_license_files"]) == 15
    assert {item["path"] for item in manifest["required_inputs"]} == {
        "app/src/api/pdf_exporter.py",
        "app/src/api/pdf_renderer_worker.py",
        "app/configs/pdf_runtime.json",
        "app/requirements.lock",
        "ops/build/install_pdf_runtime.py",
    }
    for item in manifest["required_inputs"]:
        raw = (ROOT / item["path"]).read_bytes()
        assert len(raw) == item["size"]
        assert digest(raw) == item["sha256"]
    encoded = json.dumps(manifest).casefold()
    assert all(
        word not in encoded
        for word in (
            "albertmeintjes",
            "standing authority",
            "decision_owner",
            "server/.env",
        )
    )


def test_manifest_tampering_and_unknown_fields_are_rejected(tmp_path: Path) -> None:
    value = json.loads(MANIFEST.read_bytes())
    value["browser"]["archive_sha256"] = "f" * 64
    value["private_evidence_path"] = "outside"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest_invalid"):
        load_build_manifest(path, CURRENT_MANIFEST_SHA256)


@pytest.mark.parametrize(
    "mutation",
    ["base", "component", "runtime_cap", "browser_member", "debian_package"],
)
def test_complete_manifest_requires_external_exact_digest(
    tmp_path: Path, mutation: str
) -> None:
    value = json.loads(MANIFEST.read_bytes())
    if mutation == "base":
        value["base_image"]["reference"] = "python:latest"
    elif mutation == "component":
        value["debian"]["component"] = "unapproved"
    elif mutation == "runtime_cap":
        value["runtime"]["download_limit_bytes"] = 10**18
    elif mutation == "browser_member":
        value["browser"]["members"]["extra"] = {
            "size": 1,
            "sha256": "a" * 64,
        }
    else:
        value["debian"]["packages"][0]["package"] = "unapproved"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest_invalid"):
        load_build_manifest(path, CURRENT_MANIFEST_SHA256)


def test_archive_hash_and_size_are_both_required(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    path.write_bytes(b"archive")

    verify_archive(path, size=7, sha256=digest(b"archive"))
    with pytest.raises(ValueError, match="archive_invalid"):
        verify_archive(path, size=8, sha256=digest(b"archive"))
    with pytest.raises(ValueError, match="archive_invalid"):
        verify_archive(path, size=7, sha256="f" * 64)


def browser_zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, raw in entries.items():
            archive.writestr(name, raw)
    return output.getvalue()


def test_browser_extraction_is_bounded_and_verifies_required_members(
    tmp_path: Path,
) -> None:
    prefix = "chrome-headless-shell-linux64"
    files = {
        f"{prefix}/chrome-headless-shell": b"executable",
        f"{prefix}/ABOUT": b"about",
        f"{prefix}/LICENSE.headless_shell": b"license",
    }
    raw = browser_zip(files)
    archive = tmp_path / "browser.zip"
    archive.write_bytes(raw)
    spec = {
        "archive_size": len(raw),
        "archive_sha256": digest(raw),
        "members": {
            name: {"size": len(data), "sha256": digest(data)}
            for name, data in files.items()
        },
    }

    result = extract_browser_archive(archive, tmp_path / "browser", spec)

    assert result == {name: digest(data) for name, data in files.items()}
    assert (tmp_path / "browser" / prefix / "chrome-headless-shell").is_file()


def test_browser_download_uses_archive_size_and_hash(
    tmp_path: Path, monkeypatch
) -> None:
    prefix = "chrome-headless-shell-linux64"
    files = {
        f"{prefix}/chrome-headless-shell": b"executable",
        f"{prefix}/ABOUT": b"about",
        f"{prefix}/LICENSE.headless_shell": b"license",
    }
    raw = browser_zip(files)
    manifest = {
        "browser": {
            "url": "https://cdn.playwright.dev/browser.zip",
            "archive_size": len(raw),
            "archive_sha256": digest(raw),
            "members": {
                name: {"size": len(data), "sha256": digest(data)}
                for name, data in files.items()
            },
        },
        "debian": {"packages": []},
        "runtime": {
            "download_limit_bytes": len(raw) + 1,
            "connect_timeout_seconds": 1,
            "total_timeout_seconds": 1,
        },
    }

    def download(_url, path, expected, _runtime):
        assert expected == {"size": len(raw), "sha256": digest(raw)}
        path.write_bytes(raw)

    monkeypatch.setattr(runtime_installer, "_download", download)
    result = runtime_installer.download_runtime(
        manifest, tmp_path / "downloads", tmp_path / "browser"
    )

    assert result["debian_archives"] == 87


@pytest.mark.parametrize(
    ("final_url", "accepted"),
    [
        (
            "https://storage.googleapis.com/chrome-for-testing-public/archive.zip",
            True,
        ),
        ("https://example.invalid/archive.zip", False),
    ],
)
def test_playwright_download_allows_only_observed_immutable_redirect(
    tmp_path: Path, monkeypatch, final_url: str, accepted: bool
) -> None:
    raw = b"archive"

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def geturl(self):
            return final_url

    monkeypatch.setattr(
        runtime_installer.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(raw),
    )
    path = tmp_path / "archive.zip"
    expected = {"size": len(raw), "sha256": digest(raw)}
    bounds = {
        "download_limit_bytes": 100,
        "connect_timeout_seconds": 1,
        "total_timeout_seconds": 1,
    }
    if accepted:
        runtime_installer._download(
            "https://cdn.playwright.dev/archive.zip", path, expected, bounds
        )
        assert path.read_bytes() == raw
    else:
        with pytest.raises(ValueError, match="download_invalid"):
            runtime_installer._download(
                "https://cdn.playwright.dev/archive.zip", path, expected, bounds
            )
        assert not path.exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "folder/../../escape"])
def test_browser_extraction_rejects_unsafe_members(tmp_path: Path, name: str) -> None:
    raw = browser_zip({name: b"bad"})
    archive = tmp_path / "bad.zip"
    archive.write_bytes(raw)
    spec = {
        "archive_size": len(raw),
        "archive_sha256": digest(raw),
        "members": {name: {"size": 3, "sha256": digest(b"bad")}},
    }

    with pytest.raises(ValueError, match="browser_archive_invalid"):
        extract_browser_archive(archive, tmp_path / "browser", spec)
    assert not (tmp_path / "escape").exists()


def test_all_debian_archives_must_match_exact_names_sizes_and_hashes(
    tmp_path: Path,
) -> None:
    first = b"first"
    second = b"second"
    (tmp_path / "one.deb").write_bytes(first)
    (tmp_path / "two.deb").write_bytes(second)
    manifest = {
        "debian": {
            "packages": [
                {
                    "download_name": "one.deb",
                    "size": len(first),
                    "sha256": digest(first),
                },
                {
                    "download_name": "two.deb",
                    "size": len(second),
                    "sha256": digest(second),
                },
            ]
        }
    }

    assert verify_debian_archives(manifest, tmp_path) == [
        tmp_path / "one.deb",
        tmp_path / "two.deb",
    ]
    (tmp_path / "two.deb").write_bytes(b"changed")
    with pytest.raises(ValueError, match="debian_archive_invalid"):
        verify_debian_archives(manifest, tmp_path)


def test_missing_renderer_source_config_installer_or_lock_refuses(
    tmp_path: Path,
) -> None:
    names = [
        "app/src/api/pdf_exporter.py",
        "app/src/api/pdf_renderer_worker.py",
        "app/configs/pdf_runtime.json",
        "app/requirements.lock",
        "ops/build/install_pdf_runtime.py",
    ]
    required = []
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"present")
        required.append({"path": name, "size": 7, "sha256": digest(b"present")})
    verify_required_inputs(tmp_path, required)

    (tmp_path / names[0]).unlink()
    with pytest.raises(ValueError, match="required_input_missing"):
        verify_required_inputs(tmp_path, required)


def test_required_inputs_bind_size_and_hash(tmp_path: Path) -> None:
    raw = b"approved"
    path = tmp_path / "app/source.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    required = [{"path": "app/source.py", "size": len(raw), "sha256": digest(raw)}]
    verify_required_inputs(tmp_path, required)

    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="required_input_invalid"):
        verify_required_inputs(tmp_path, required)


@pytest.mark.parametrize(
    "changed_name",
    [
        "app/src/api/pdf_exporter.py",
        "app/src/api/pdf_renderer_worker.py",
        "app/configs/pdf_runtime.json",
        "app/requirements.lock",
        "ops/build/install_pdf_runtime.py",
    ],
)
def test_each_real_required_input_is_byte_bound(
    tmp_path: Path, changed_name: str
) -> None:
    manifest = load_build_manifest(MANIFEST, CURRENT_MANIFEST_SHA256)
    for item in manifest["required_inputs"]:
        destination = tmp_path / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / item["path"]).read_bytes())
    verify_required_inputs(tmp_path, manifest["required_inputs"])

    changed = tmp_path / changed_name
    changed.write_bytes(changed.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="required_input_invalid"):
        verify_required_inputs(tmp_path, manifest["required_inputs"])


def test_required_input_swap_is_rejected(tmp_path: Path) -> None:
    manifest = load_build_manifest(MANIFEST, CURRENT_MANIFEST_SHA256)
    for item in manifest["required_inputs"]:
        destination = tmp_path / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / item["path"]).read_bytes())
    first, second = (
        tmp_path / item["path"] for item in manifest["required_inputs"][:2]
    )
    first_raw, second_raw = first.read_bytes(), second.read_bytes()
    first.write_bytes(second_raw)
    second.write_bytes(first_raw)

    with pytest.raises(ValueError, match="required_input_invalid"):
        verify_required_inputs(tmp_path, manifest["required_inputs"])


def test_required_inputs_reject_linked_intermediate_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "source.py").write_bytes(b"approved")
    linked = tmp_path / "ops/build"
    linked.parent.mkdir()
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"links unavailable: {error}")
    required = [
        {
            "path": "ops/build/source.py",
            "size": 8,
            "sha256": digest(b"approved"),
        }
    ]

    with pytest.raises(ValueError, match="required_input_invalid"):
        verify_required_inputs(tmp_path, required)


def test_required_inputs_reject_linked_root_and_file(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    source = real_root / "ops/build/source.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"approved")
    linked_root = tmp_path / "linked-root"
    linked_file = real_root / "ops/build/linked.py"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
        linked_file.symlink_to(source)
    except OSError as error:
        pytest.skip(f"links unavailable: {error}")

    root_spec = [
        {
            "path": "ops/build/source.py",
            "size": 8,
            "sha256": digest(b"approved"),
        }
    ]
    file_spec = [
        {
            "path": "ops/build/linked.py",
            "size": 8,
            "sha256": digest(b"approved"),
        }
    ]
    with pytest.raises(ValueError, match="required_input_invalid"):
        verify_required_inputs(linked_root, root_spec)
    with pytest.raises(ValueError, match="required_input_invalid"):
        verify_required_inputs(real_root, file_spec)


@pytest.mark.skipif(os.name != "nt", reason="junction control requires Windows")
def test_required_inputs_reject_junction_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "source.py").write_bytes(b"approved")
    linked = tmp_path / "ops/build"
    linked.parent.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(linked), str(real)],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        pytest.skip("junction creation unavailable")
    required = [
        {
            "path": "ops/build/source.py",
            "size": 8,
            "sha256": digest(b"approved"),
        }
    ]

    with pytest.raises(ValueError, match="required_input_invalid"):
        verify_required_inputs(tmp_path, required)


def test_docker_recipe_uses_hash_locks_and_no_browser_installer() -> None:
    docker = (ROOT / "app/Dockerfile.general-question").read_text(encoding="utf-8")

    assert "COPY lp/requirements.lock /tmp/lp-requirements.lock" in docker
    assert "python -m pip install --require-hashes" in docker
    assert "ops/build/install_pdf_runtime.py" in docker
    assert "ops/build/pdf_runtime_dependencies.json" in docker
    assert "playwright install" not in docker
    assert "USER appuser" in docker
    assert "--workers 1" in docker
    assert docker.count("FROM ") >= 2
    final_stage = docker.rsplit("FROM ", 1)[1]
    assert "COPY ops/build/install_pdf_runtime.py" not in final_stage
    assert "COPY ops/build/pdf_runtime_dependencies.json" not in final_stage
    assert "--mount=type=bind,from=build" in final_stage
    assert "dpkg -i /tmp/pdf-runtime-debs/*.deb" in final_stage
    assert "COPY --from=build /app/licenses/" not in final_stage


def test_app_locks_contain_only_the_approved_exporter_direct_delta() -> None:
    requirements = (ROOT / "app/requirements.txt").read_text(encoding="utf-8")
    runtime = (ROOT / "app/requirements.lock").read_text(encoding="utf-8")
    dev = (ROOT / "app/requirements-dev.lock").read_text(encoding="utf-8")

    assert "playwright==1.62.0" in requirements
    assert "pypdf==6.18.1" in requirements
    assert "pyee==13.0.0" in requirements
    assert "greenlet==3.5.5" in requirements
    assert "tinycss2==1.5.1" in requirements
    assert "webencodings==0.6.1" in requirements
    for lock in (runtime, dev):
        assert "playwright==1.62.0" in lock
        assert "pypdf==6.18.1" in lock
        assert "pyee==13.0.0" in lock
        assert "greenlet==3.5.5" in lock
        assert "typing-extensions==4.16.0" in lock
        assert "tinycss2==1.5.1" in lock
        assert "webencodings==0.6.1" in lock
        assert (
            "sha256:3415ba0f5839c062696996998176c4a3751d18b7edaaeeb658c9ce21ec150661"
            in lock
        )
        assert (
            "sha256:7fab6269c8bf237c657876b52058ccb182e861518d1c695c1a9aaa8c1c105d5b"
            in lock
        )
        assert (
            "sha256:ba33bae6a13b3d9d354c751cb618af357d20fe1d57767cbcce52079bbef17ad3"
            in lock
        )
        assert (
            "sha256:ee93a2665670ecf57ee81d197a4ca548f3dc15f9cefc56e59b8140866aaa3de5"
            in lock
        )
        assert "playwright==1.62.1" not in lock
