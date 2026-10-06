import hashlib
import importlib.util
import json
import os
import stat
import struct
import warnings
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "tests/build/install_ci_tools.py"
MANIFEST = ROOT / "tests/build/ci_node_runtime.json"
MANIFEST_SHA256 = "adf631b83e653a953e7a3091ebfacb2178a0ab3c28718fb133480ab88d484462"
NODE_MEMBER = "playwright/driver/node"
LICENSE_MEMBER = "playwright/driver/LICENSE"


def installer():
    spec = importlib.util.spec_from_file_location("install_ci_tools", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def elf(*, elf_class=2, endian=1, machine=62):
    raw = bytearray(64)
    raw[:7] = b"\x7fELF" + bytes([elf_class, endian, 1])
    raw[18:20] = struct.pack("<H", machine)
    return bytes(raw)


def synthetic_manifest(node_raw=None, license_raw=b"license"):
    node_raw = node_raw or elf()
    return {
        "archive": {
            "bytes": 1,
            "filename": "playwright.whl",
            "sha256": "a" * 64,
            "url": "https://files.pythonhosted.org/packages/playwright.whl",
        },
        "base_image": "python:3.13-slim@sha256:" + "1" * 64,
        "contract_version": "ci_node_runtime_v1",
        "license": {
            "bytes": len(license_raw),
            "member": LICENSE_MEMBER,
            "path": "LICENSE",
            "sha256": hashlib.sha256(license_raw).hexdigest(),
        },
        "node": {
            "bytes": len(node_raw),
            "member": NODE_MEMBER,
            "path": "node",
            "sha256": hashlib.sha256(node_raw).hexdigest(),
            "version_output": "v24.18.1",
        },
        "scope": "test_only",
        "source_distribution": "playwright==1.62.0",
    }


def write_wheel(
    path,
    manifest,
    *,
    node_raw=None,
    license_raw=b"license",
    node_mode=0o100755,
    extra=None,
):
    node_raw = node_raw or elf()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            for member, raw, mode in (
                (manifest["node"]["member"], node_raw, node_mode),
                (manifest["license"]["member"], license_raw, 0o100644),
            ):
                info = zipfile.ZipInfo(member)
                info.external_attr = mode << 16
                archive.writestr(info, raw)
            for name, raw, mode in extra or []:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, raw)


def successful_run(args, **_kwargs):
    if args[0] == "ldd":
        return SimpleNamespace(returncode=0, stdout=b"libc.so.6\n", stderr=b"")
    return SimpleNamespace(returncode=0, stdout=b"v24.18.1\n", stderr=b"")


class ControlledResponse:
    def __init__(self, chunks, url):
        self.chunks = list(chunks)
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, _size):
        item = self.chunks.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_exact_node_manifest_is_pinned_and_valid():
    module = installer()
    manifest = module.load_node_manifest(MANIFEST, MANIFEST_SHA256)
    assert hashlib.sha256(MANIFEST.read_bytes()).hexdigest() == MANIFEST_SHA256
    assert manifest["archive"] == {
        "bytes": 47_748_926,
        "filename": "playwright-1.62.0-py3-none-manylinux1_x86_64.whl",
        "sha256": "ba33bae6a13b3d9d354c751cb618af357d20fe1d57767cbcce52079bbef17ad3",
        "url": (
            "https://files.pythonhosted.org/packages/43/6b/"
            "b24aebc2b04bffcb342bccf96e287c78b363e1615bed5cea97500cc0393a/"
            "playwright-1.62.0-py3-none-manylinux1_x86_64.whl"
        ),
    }
    assert manifest["node"]["member"] == NODE_MEMBER
    assert manifest["license"]["member"] == LICENSE_MEMBER


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("extra", True),
        lambda value: value["archive"].__setitem__("bytes", True),
        lambda value: value["archive"].__setitem__("filename", "../playwright.whl"),
        lambda value: value["archive"].__setitem__("sha256", "A" * 64),
        lambda value: value["node"].__setitem__("member", "/node"),
        lambda value: value["node"].__setitem__("path", "../node"),
        lambda value: value["node"].__setitem__("bytes", 0),
        lambda value: value["license"].__setitem__("sha256", "f" * 63),
    ],
)
def test_node_manifest_path_type_and_hash_mutations_refuse(mutate):
    value = json.loads(MANIFEST.read_bytes())
    mutate(value)
    with pytest.raises(ValueError, match="ci_node_manifest_invalid"):
        installer().validate_node_manifest(value)


def test_changed_node_manifest_and_wrong_compiled_digest_refuse(tmp_path):
    changed = tmp_path / "changed.json"
    changed.write_bytes(MANIFEST.read_bytes() + b"\n")
    module = installer()
    with pytest.raises(ValueError, match="ci_node_manifest_invalid"):
        module.load_node_manifest(changed, MANIFEST_SHA256)
    with pytest.raises(ValueError, match="ci_node_manifest_invalid"):
        module.load_node_manifest(MANIFEST, "f" * 64)


def test_synthetic_archive_extracts_only_node_license_and_inventory(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest, extra=[("package/ignored.py", b"ignored", 0o100644)])
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setattr(module.subprocess, "run", successful_run)
    destination = tmp_path / "node"
    output = destination / "installed.json"
    receipt = module.extract_node_archive(manifest, wheel, destination, output)

    assert sorted(path.name for path in destination.iterdir()) == [
        "LICENSE",
        "installed.json",
        "node",
    ]
    assert receipt["contract_version"] == "ci_node_installed_v1"
    assert receipt["node"]["version_output"] == "v24.18.1"
    assert receipt["node"]["sha256"] == manifest["node"]["sha256"]
    assert receipt["license"]["sha256"] == manifest["license"]["sha256"]
    if os.name != "nt":
        assert stat.S_IMODE((destination / "node").stat().st_mode) == 0o755
        assert stat.S_IMODE((destination / "LICENSE").stat().st_mode) == 0o644


def test_target_member_wrong_mode_refuses(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "wrong-mode.whl"
    write_wheel(wheel, manifest, node_mode=0o100644)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setattr(module.subprocess, "run", successful_run)
    with pytest.raises(ValueError, match="ci_node_archive_invalid"):
        module.extract_node_archive(
            manifest,
            wheel,
            tmp_path / "node",
            tmp_path / "node/installed.json",
        )


@pytest.mark.parametrize("field", ["bytes", "sha256"])
def test_archive_size_and_hash_drift_refuse(field, tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if field == "bytes":
        manifest["archive"]["bytes"] += 1
    else:
        manifest["archive"]["sha256"] = "f" * 64
    monkeypatch.setattr(module.subprocess, "run", successful_run)
    with pytest.raises(ValueError, match="ci_node_archive_invalid"):
        module.extract_node_archive(
            manifest,
            wheel,
            tmp_path / "node",
            tmp_path / "node/installed.json",
        )


@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        ("duplicate", [(NODE_MEMBER, elf(), 0o100755)]),
        ("symlink", [("link", b"node", 0o120777)]),
        ("traversal", [("../escape", b"x", 0o100644)]),
    ],
)
def test_duplicate_symlink_and_traversal_members_refuse(kind, extra, tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / f"{kind}.whl"
    write_wheel(wheel, manifest, extra=extra)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setattr(module.subprocess, "run", successful_run)
    with pytest.raises(ValueError, match="ci_node_archive_invalid"):
        module.extract_node_archive(
            manifest,
            wheel,
            tmp_path / "node",
            tmp_path / "node/installed.json",
        )


@pytest.mark.parametrize(
    "node_raw",
    [
        elf(elf_class=1),
        elf(endian=2),
        elf(machine=183),
        b"not an elf",
    ],
)
def test_wrong_node_architecture_refuses(node_raw, tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest(node_raw=node_raw)
    wheel = tmp_path / "wrong.whl"
    write_wheel(wheel, manifest, node_raw=node_raw)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    monkeypatch.setattr(module.subprocess, "run", successful_run)
    with pytest.raises(ValueError, match="ci_node_binary_invalid"):
        module.extract_node_archive(
            manifest,
            wheel,
            tmp_path / "node",
            tmp_path / "node/installed.json",
        )


def test_version_and_linkage_mismatch_refuse(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    destination = tmp_path / "node"
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"v24.18.2\n", stderr=b""),
    )
    with pytest.raises(ValueError, match="ci_node_binary_invalid"):
        module.extract_node_archive(
            manifest,
            wheel,
            destination,
            destination / "installed.json",
        )


def test_missing_dynamic_linkage_refuses(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()

    def missing_linkage(args, **_kwargs):
        if args[0] == "ldd":
            return SimpleNamespace(returncode=1, stdout=b"libc.so.6 => not found\n", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"v24.18.1\n", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", missing_linkage)
    with pytest.raises(ValueError, match="ci_node_binary_invalid"):
        module.extract_node_archive(
            manifest,
            wheel,
            tmp_path / "node",
            tmp_path / "node/installed.json",
        )


@pytest.mark.parametrize(
    "case",
    ["relative", "existing", "linked_parent", "outside", "output_exists", "output_target"],
)
def test_destination_and_output_clobber_controls_refuse(case, tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    destination = tmp_path / "node"
    output = destination / "installed.json"
    if case == "relative":
        destination = Path("node")
        output = destination / "installed.json"
    elif case == "existing":
        destination.mkdir()
    elif case == "linked_parent":
        real = tmp_path / "real"
        real.mkdir()
        linked = tmp_path / "linked"
        linked.symlink_to(real, target_is_directory=True)
        destination = linked / "node"
        output = destination / "installed.json"
    elif case == "outside":
        output = tmp_path / "installed.json"
    elif case == "output_exists":
        destination.mkdir()
        output.write_bytes(b"old")
    elif case == "output_target":
        output = destination / "node"
    monkeypatch.setattr(module.subprocess, "run", successful_run)
    with pytest.raises(ValueError, match="ci_node_destination_invalid"):
        module.extract_node_archive(manifest, wheel, destination, output)


def test_foreign_output_created_after_precheck_survives_cleanup(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    destination = tmp_path / "node"
    output = destination / "installed.json"
    original_open = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == output and mode == "xb":
            with original_open(path, "wb") as foreign:
                foreign.write(b"foreign-output")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    monkeypatch.setattr(module.subprocess, "run", successful_run)
    with pytest.raises(FileExistsError):
        module.extract_node_archive(manifest, wheel, destination, output)

    assert output.read_bytes() == b"foreign-output"


def test_foreign_target_created_after_precheck_survives_cleanup(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    destination = tmp_path / "node"
    target = destination / "node"
    original_open = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "xb":
            with original_open(path, "wb") as foreign:
                foreign.write(b"foreign-target")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    monkeypatch.setattr(module.subprocess, "run", successful_run)
    with pytest.raises(FileExistsError):
        module.extract_node_archive(
            manifest,
            wheel,
            destination,
            destination / "installed.json",
        )

    assert target.read_bytes() == b"foreign-target"


def test_foreign_directory_created_after_precheck_survives_cleanup(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    destination = tmp_path / "node"
    original_mkdir = Path.mkdir

    def racing_mkdir(path, *args, **kwargs):
        if path == destination:
            original_mkdir(path, *args, **kwargs)
            raise FileExistsError(destination)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with pytest.raises(FileExistsError):
        module.extract_node_archive(
            manifest,
            wheel,
            destination,
            destination / "installed.json",
        )

    assert destination.is_dir()


def test_install_node_streams_bytes_manifest_through_real_downloader(tmp_path, monkeypatch):
    module = installer()
    archive_raw = b"controlled-wheel"
    manifest = synthetic_manifest()
    manifest["archive"]["bytes"] = len(archive_raw)
    manifest["archive"]["sha256"] = hashlib.sha256(archive_raw).hexdigest()
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: ControlledResponse(
            [archive_raw, b""], manifest["archive"]["url"]
        ),
    )
    observed = {}

    def extract(_manifest, archive_path, destination, output):
        observed["archive"] = archive_path.read_bytes()
        observed["destination"] = destination
        observed["output"] = output
        return {"contract_version": "controlled"}

    monkeypatch.setattr(module, "extract_node_archive", extract)
    destination = tmp_path / "node"
    output = destination / "installed.json"

    assert module.install_node(manifest, destination, output) == {"contract_version": "controlled"}
    assert observed == {
        "archive": archive_raw,
        "destination": destination,
        "output": output,
    }


def test_real_downloader_preserves_foreign_file_after_exclusive_create_race(tmp_path, monkeypatch):
    module = installer()
    raw = b"archive"
    target = tmp_path / "archive.whl"
    url = "https://files.pythonhosted.org/packages/archive.whl"
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: ControlledResponse([raw, b""], url),
    )
    original_open = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "xb":
            with original_open(path, "wb") as foreign:
                foreign.write(b"foreign-archive")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(ValueError, match="ci_archive_invalid") as refusal:
        module._download(
            url,
            target,
            {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            allowed_hosts=frozenset({"files.pythonhosted.org"}),
        )

    assert isinstance(refusal.value.__cause__, FileExistsError)
    assert target.read_bytes() == b"foreign-archive"


def test_real_downloader_removes_only_its_owned_partial_and_preserves_error(tmp_path, monkeypatch):
    module = installer()
    raw = b"partial"
    target = tmp_path / "archive.whl"
    url = "https://files.pythonhosted.org/packages/archive.whl"
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: ControlledResponse([raw, ValueError("stream-refusal")], url),
    )

    with pytest.raises(ValueError, match="stream-refusal"):
        module._download(
            url,
            target,
            {"size": len(raw) * 2, "sha256": "f" * 64},
            allowed_hosts=frozenset({"files.pythonhosted.org"}),
        )

    assert not target.exists()


@pytest.mark.parametrize("failure", ["overrun", "hash", "redirect"])
def test_real_downloader_rejects_stream_contract_failures(failure, tmp_path, monkeypatch):
    module = installer()
    raw = b"archive"
    target = tmp_path / "archive.whl"
    url = "https://files.pythonhosted.org/packages/archive.whl"
    response_url = "https://example.test/archive.whl" if failure == "redirect" else url
    size = len(raw) - 1 if failure == "overrun" else len(raw)
    digest = "f" * 64 if failure == "hash" else hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: ControlledResponse([raw, b""], response_url),
    )

    with pytest.raises(ValueError, match="ci_archive_invalid"):
        module._download(
            url,
            target,
            {"size": size, "sha256": digest},
            allowed_hosts=frozenset({"files.pythonhosted.org"}),
        )

    assert not target.exists()


def test_owned_partial_target_is_removed_after_extraction_failure(tmp_path):
    module = installer()
    target = tmp_path / "node"

    class FailingSource:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("original-refusal")
            return b"partial"

    class FailingArchive:
        def open(self, _info):
            return FailingSource()

    with pytest.raises(RuntimeError, match="original-refusal"):
        module._extract_member(
            FailingArchive(),
            object(),
            target,
            {"bytes": 20, "sha256": "f" * 64},
        )

    assert not target.exists()


def test_replaced_owned_target_survives_cleanup_with_original_refusal(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    destination = tmp_path / "node"
    target = destination / "node"

    def replace_then_refuse(path, _expected_version):
        path.unlink()
        path.write_bytes(b"foreign-replacement")
        raise ValueError("original-refusal")

    monkeypatch.setattr(module, "_verify_node_binary", replace_then_refuse)
    with pytest.raises(ValueError, match="original-refusal"):
        module.extract_node_archive(
            manifest,
            wheel,
            destination,
            destination / "installed.json",
        )

    assert target.read_bytes() == b"foreign-replacement"


def test_replaced_owned_target_survives_cleanup_when_its_inode_number_is_reused(
    tmp_path, monkeypatch
):
    """The Linux filesystems under the engine image hand a freed inode number to the next
    file created in the same directory, so a foreign replacement carries the device, inode
    and type identity of the owned target it displaced. Collapsing inode numbers reproduces
    that here, where the file identity carries a sequence number that never repeats."""
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    destination = tmp_path / "node"
    target = destination / "node"
    original_identity = module._identity

    def reused_identity(value):
        device, _inode, kind = original_identity(value)
        return device, 0, kind

    def replace_then_refuse(path, _expected_version):
        path.unlink()
        path.write_bytes(b"foreign-replacement")
        raise ValueError("original-refusal")

    monkeypatch.setattr(module, "_identity", reused_identity)
    monkeypatch.setattr(module, "_verify_node_binary", replace_then_refuse)
    with pytest.raises(ValueError, match="original-refusal"):
        module.extract_node_archive(
            manifest,
            wheel,
            destination,
            destination / "installed.json",
        )

    assert target.read_bytes() == b"foreign-replacement"
    assert not (destination / "LICENSE").exists()


def test_foreign_content_in_owned_directory_survives_and_preserves_refusal(tmp_path, monkeypatch):
    module = installer()
    manifest = synthetic_manifest()
    wheel = tmp_path / "playwright.whl"
    write_wheel(wheel, manifest)
    manifest["archive"]["bytes"] = wheel.stat().st_size
    manifest["archive"]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    destination = tmp_path / "node"
    foreign = destination / "foreign"

    def add_foreign_then_refuse(_path, _expected_version):
        foreign.write_bytes(b"caller-data")
        raise ValueError("original-refusal")

    monkeypatch.setattr(module, "_verify_node_binary", add_foreign_then_refuse)
    with pytest.raises(ValueError, match="original-refusal"):
        module.extract_node_archive(
            manifest,
            wheel,
            destination,
            destination / "installed.json",
        )

    assert foreign.read_bytes() == b"caller-data"


@pytest.mark.parametrize("linked_part", ["destination", "output", "ancestor"])
def test_destination_output_and_ancestor_junctions_refuse(linked_part, tmp_path, monkeypatch):
    module = installer()
    destination = tmp_path / "node"
    output = destination / "installed.json"
    linked_path = {
        "destination": destination,
        "output": output,
        "ancestor": tmp_path,
    }[linked_part]
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(Path, "is_junction", lambda path: path == linked_path, raising=False)

    with pytest.raises(ValueError, match="ci_node_destination_invalid"):
        module._safe_node_destination(destination, output)


def test_install_node_cli_requires_all_four_flags():
    with pytest.raises(SystemExit, match="2"):
        installer().main(["install-node"])


def test_dockerfile_has_named_nonroot_node_toolchain_and_retains_full_suite():
    docker = (ROOT / "Dockerfile.tests").read_text(encoding="utf-8")
    source_copy = docker.index("COPY . /workspace/engine")
    assert "AS test-toolchain" in docker[:source_copy]
    assert "install-node" in docker[:source_copy]
    assert "NODE_BINARY=/opt/42-ci/node/node" in docker[:source_copy]
    assert "USER 10001:10001" in docker
    assert "FROM test-toolchain AS test" in docker
    assert docker.count("python -m pytest tests/unit/ -q") == 1
    assert '"/opt/42-engine-test-venv/bin/python", "-m", "pytest", "tests/unit/", "-q"' in docker


def test_existing_git_manifest_and_commands_are_unchanged():
    assert (
        hashlib.sha256((ROOT / "tests/build/ci_git_dependencies.json").read_bytes()).hexdigest()
        == "1f596f0f894e8b25e33f8eff9940d07b59227aa8f59082275b7b828f52f79455"
    )
    docker = (ROOT / "Dockerfile.tests").read_text("utf-8")
    assert "install_ci_tools.py download" in docker
    assert "install_ci_tools.py verify-installed" in docker
    assert 'test "$(git --version)" = "git version 2.47.3"' in docker
