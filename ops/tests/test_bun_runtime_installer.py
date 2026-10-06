"""Refusals and the happy path of the pinned bun installer, proven on a stub archive."""

import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from ops.build import install_bun_runtime as installer

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "ops" / "build" / "bun_runtime.json"
PINNED = {
    "version": "1.4.0",
    "url": "https://github.com/oven-sh/bun/releases/download/bun-v1.4.0/bun-linux-x64.zip",
    "sha256": "2d03fb5fb83ac8b567aca0a281b2ce1a1a19d488f56c2968d88c3f25e92fe452",
    "size_bytes": 36697619,
}
PAYLOAD = b"#!/bin/sh\necho 1.4.0\n"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stub_archive(directory: Path, member: str = installer.MEMBER) -> Path:
    archive = directory / "bun-linux-x64.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(member, PAYLOAD)
    return archive


def _manifest_for(archive: Path, directory: Path, **overrides) -> tuple[Path, str]:
    raw = archive.read_bytes()
    value = {
        "version": "1.4.0",
        "url": PINNED["url"],
        "sha256": _sha256(raw),
        "size_bytes": len(raw),
        **overrides,
    }
    path = directory / "bun_runtime.json"
    encoded = json.dumps(value).encode("utf-8")
    path.write_bytes(encoded)
    return path, _sha256(encoded)


def _run(manifest: Path, digest: str, archive: Path, destination: Path) -> int:
    return installer.main(
        [
            "--manifest",
            str(manifest),
            "--manifest-sha256",
            digest,
            "--archive",
            str(archive),
            "--destination",
            str(destination),
        ]
    )


def test_repository_manifest_is_the_pinned_release() -> None:
    raw = MANIFEST.read_bytes()
    assert json.loads(raw) == PINNED
    assert installer.load_manifest(MANIFEST, _sha256(raw)) == PINNED
    with pytest.raises(installer.InstallRefused, match="manifest_sha256_mismatch"):
        installer.load_manifest(MANIFEST, "0" * 64)


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"version": "1.4"},
        {"url": "https://example.invalid/bun-linux-x64.zip"},
        {"sha256": "abc"},
        {"size_bytes": 0},
        {"size_bytes": "36697619"},
    ],
)
def test_manifest_shape_drift_refuses(tmp_path: Path, change: dict) -> None:
    archive = _stub_archive(tmp_path)
    manifest, digest = _manifest_for(archive, tmp_path, **change)
    with pytest.raises(installer.InstallRefused, match="manifest_invalid"):
        installer.load_manifest(manifest, digest)
    assert _run(manifest, digest, archive, tmp_path / "opt-bun") == 1
    assert not (tmp_path / "opt-bun").exists()


def test_verified_archive_installs_the_binary_and_records_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _stub_archive(tmp_path)
    manifest, digest = _manifest_for(archive, tmp_path)
    destination = tmp_path / "opt-bun"
    assert _run(manifest, digest, archive, destination) == 0
    binary = destination / "bin" / "bun"
    assert binary.read_bytes() == PAYLOAD
    if os.name == "posix":
        assert binary.stat().st_mode & stat.S_IXUSR
    installed = json.loads((destination / "installed.json").read_bytes())
    assert installed == {
        "contract_version": "bun_runtime_install_v1",
        "version": "1.4.0",
        "archive_sha256": _sha256(archive.read_bytes()),
        "archive_size_bytes": archive.stat().st_size,
        "member": installer.MEMBER,
        "binary_sha256": _sha256(PAYLOAD),
    }
    out = capsys.readouterr()
    assert json.loads(out.out) == {"version": "1.4.0", "binary": str(binary)}
    assert out.err == ""


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"sha256": "0" * 64}, "archive_sha256_mismatch"),
        ({"size_bytes": 1}, "archive_size_mismatch"),
    ],
)
def test_archive_bytes_that_differ_from_the_manifest_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], change: dict, reason: str
) -> None:
    archive = _stub_archive(tmp_path)
    manifest, digest = _manifest_for(archive, tmp_path, **change)
    value = installer.load_manifest(manifest, digest)
    with pytest.raises(installer.InstallRefused, match=reason):
        installer.install_archive(archive, value, tmp_path / "opt-bun")
    assert _run(manifest, digest, archive, tmp_path / "opt-bun") == 1
    assert not (tmp_path / "opt-bun").exists()
    assert capsys.readouterr().err.strip() == f"bun_runtime_refused: {reason}"


def test_archive_without_the_binary_member_refuses(tmp_path: Path) -> None:
    archive = _stub_archive(tmp_path, member="bun-linux-x64/README")
    manifest, digest = _manifest_for(archive, tmp_path)
    with pytest.raises(installer.InstallRefused, match="archive_member_missing"):
        installer.install_archive(
            archive, installer.load_manifest(manifest, digest), tmp_path / "opt-bun"
        )
    assert not (tmp_path / "opt-bun").exists()


def test_existing_destination_refuses(tmp_path: Path) -> None:
    archive = _stub_archive(tmp_path)
    manifest, digest = _manifest_for(archive, tmp_path)
    destination = tmp_path / "opt-bun"
    destination.mkdir()
    with pytest.raises(installer.InstallRefused, match="destination_exists"):
        installer.install_archive(
            archive, installer.load_manifest(manifest, digest), destination
        )
    assert list(destination.iterdir()) == []


def test_download_verifies_the_streamed_bytes(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    archive = _stub_archive(tmp_path / "source")
    raw = archive.read_bytes()
    manifest, digest = _manifest_for(archive, tmp_path)
    value = installer.load_manifest(manifest, digest)
    target = tmp_path / "download" / "bun-linux-x64.zip"

    def opener(url: str, timeout: float) -> io.BytesIO:
        assert url == PINNED["url"]
        assert timeout > 0
        return io.BytesIO(raw)

    assert installer.download_archive(value, target, opener=opener) == target
    assert target.read_bytes() == raw

    for served, reason in (
        (raw + b"x", "archive_size_mismatch"),
        (raw[:-1], "archive_size_mismatch"),
        (raw[:-1] + b"y", "archive_sha256_mismatch"),
    ):
        oversized = tmp_path / "download" / "again.zip"
        with pytest.raises(installer.InstallRefused, match=reason):
            installer.download_archive(
                value, oversized, opener=lambda url, timeout, s=served: io.BytesIO(s)
            )
        assert not oversized.exists()
