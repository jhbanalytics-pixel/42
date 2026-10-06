"""Install the pinned bun release into the context builder image from a reviewed manifest."""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

MEMBER = "bun-linux-x64/bun"
_KEYS = {"version", "url", "sha256", "size_bytes"}
_URL = (
    "https://github.com/oven-sh/bun/releases/download/bun-v{version}/bun-linux-x64.zip"
)
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_BINARY_BYTES = 512 * 1024 * 1024
_CHUNK = 1024 * 1024
_DOWNLOAD_SECONDS = 180


class InstallRefused(ValueError):
    pass


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_manifest(value) -> dict:
    if (
        type(value) is not dict
        or set(value) != _KEYS
        or not isinstance(value["version"], str)
        or _VERSION.fullmatch(value["version"]) is None
        or value["url"] != _URL.format(version=value["version"])
        or not isinstance(value["sha256"], str)
        or _SHA256.fullmatch(value["sha256"]) is None
        or type(value["size_bytes"]) is not int
        or value["size_bytes"] <= 0
    ):
        raise InstallRefused("manifest_invalid")
    return value


def load_manifest(path: Path, expected_sha256: str) -> dict:
    raw = Path(path).read_bytes()
    if _digest(raw) != expected_sha256:
        raise InstallRefused("manifest_sha256_mismatch")
    try:
        value = json.loads(raw)
    except ValueError as error:
        raise InstallRefused("manifest_invalid") from error
    return validate_manifest(value)


def verify_archive(path: Path, manifest: dict) -> None:
    if not path.is_file():
        raise InstallRefused("archive_missing")
    if path.stat().st_size != manifest["size_bytes"]:
        raise InstallRefused("archive_size_mismatch")
    if _digest(path.read_bytes()) != manifest["sha256"]:
        raise InstallRefused("archive_sha256_mismatch")


def download_archive(
    manifest: dict, destination: Path, *, opener=urllib.request.urlopen
) -> Path:
    destination = Path(destination)
    if destination.exists():
        raise InstallRefused("archive_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    count = 0
    try:
        with (
            opener(manifest["url"], timeout=60) as response,
            destination.open("wb") as output,
        ):
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                count += len(chunk)
                if count > manifest["size_bytes"]:
                    raise InstallRefused("archive_size_mismatch")
                if time.monotonic() - started > _DOWNLOAD_SECONDS:
                    raise InstallRefused("download_timeout")
                output.write(chunk)
        verify_archive(destination, manifest)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def install_archive(archive: Path, manifest: dict, destination: Path) -> Path:
    archive, destination = Path(archive), Path(destination)
    verify_archive(archive, manifest)
    if destination.exists() or destination.is_symlink():
        raise InstallRefused("destination_exists")
    try:
        with zipfile.ZipFile(archive) as bundle:
            try:
                info = bundle.getinfo(MEMBER)
            except KeyError as error:
                raise InstallRefused("archive_member_missing") from error
            if (
                info.is_dir()
                or info.file_size <= 0
                or info.file_size > _MAX_BINARY_BYTES
            ):
                raise InstallRefused("archive_member_invalid")
            binary = destination / "bin" / "bun"
            binary.parent.mkdir(parents=True)
            digest = hashlib.sha256()
            written = 0
            with bundle.open(info) as source, binary.open("wb") as output:
                while True:
                    chunk = source.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > info.file_size:
                        raise InstallRefused("archive_member_invalid")
                    digest.update(chunk)
                    output.write(chunk)
            if written != info.file_size:
                raise InstallRefused("archive_member_invalid")
    except zipfile.BadZipFile as error:
        raise InstallRefused("archive_invalid") from error
    binary.chmod(0o755)
    record = {
        "contract_version": "bun_runtime_install_v1",
        "version": manifest["version"],
        "archive_sha256": manifest["sha256"],
        "archive_size_bytes": manifest["size_bytes"],
        "member": MEMBER,
        "binary_sha256": digest.hexdigest(),
    }
    (destination / "installed.json").write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return binary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--download-root")
    parser.add_argument("--archive")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(Path(args.manifest), args.manifest_sha256)
        if args.archive:
            archive = Path(args.archive)
        elif args.download_root:
            archive = download_archive(
                manifest, Path(args.download_root) / "bun-linux-x64.zip"
            )
        else:
            raise InstallRefused("archive_source_missing")
        binary = install_archive(archive, manifest, Path(args.destination))
    except InstallRefused as error:
        print(f"bun_runtime_refused: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("bun_runtime_refused: io_error", file=sys.stderr)
        return 1
    print(json.dumps({"version": manifest["version"], "binary": str(binary)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
