import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

_MANIFEST_SHA256 = "1f596f0f894e8b25e33f8eff9940d07b59227aa8f59082275b7b828f52f79455"
_NODE_MANIFEST_SHA256 = "adf631b83e653a953e7a3091ebfacb2178a0ab3c28718fb133480ab88d484462"
_BASE_IMAGE = (
    "python:3.13-slim@sha256:16f75ad0fbc6c4883a8afd63b2d700c3cf68ccffc1aaeca5304ca0a3a908451f"
)
_SNAPSHOT = "https://snapshot.debian.org/archive/debian/20260824T000000Z/"
_GIT = {
    "path": "/usr/bin/git",
    "version_output": "git version 2.47.3",
    "sha256": "356db14e102d68a1a37d8a1ac577dfd678d45d46e92f468bef8b7154e7bfdc60",
}
_KEYS = {
    "contract_version",
    "scope",
    "base_image",
    "base_installed_count",
    "base_dpkg_status_sha256",
    "package_index_sha256",
    "snapshot",
    "apt_simulation_sha256",
    "packages",
    "package_count",
    "base_package_changes",
    "archive_bytes",
    "tools",
    "base_packages",
}
_NODE_KEYS = {
    "archive",
    "base_image",
    "contract_version",
    "license",
    "node",
    "scope",
    "source_distribution",
}
_NODE_ARCHIVE = {
    "bytes": 47_748_926,
    "filename": "playwright-1.62.0-py3-none-manylinux1_x86_64.whl",
    "sha256": "ba33bae6a13b3d9d354c751cb618af357d20fe1d57767cbcce52079bbef17ad3",
    "url": (
        "https://files.pythonhosted.org/packages/43/6b/"
        "b24aebc2b04bffcb342bccf96e287c78b363e1615bed5cea97500cc0393a/"
        "playwright-1.62.0-py3-none-manylinux1_x86_64.whl"
    ),
}
_NODE = {
    "bytes": 123_656_816,
    "member": "playwright/driver/node",
    "path": "node",
    "sha256": "f3432a45b03b2da0d270095fdd8813dc34cbea73f5fc8b18c7a384b7cf9b333a",
    "version_output": "v24.18.1",
}
_NODE_LICENSE = {
    "bytes": 157_606,
    "member": "playwright/driver/LICENSE",
    "path": "LICENSE",
    "sha256": "148eacf7863ef4329224a29398623077200a27194aa075569faf4a0a85566ca5",
}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _relative(value) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("ci_manifest_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError("ci_manifest_invalid")
    return value


def validate_ci_manifest(value: dict) -> dict:
    if (
        type(value) is not dict
        or set(value) != _KEYS
        or value.get("contract_version") != "ci_git_dependency_v1"
        or value.get("scope") != "test_only"
        or value.get("base_image") != _BASE_IMAGE
        or value.get("snapshot") != _SNAPSHOT
        or value.get("base_installed_count") != 87
        or value.get("package_count") != 31
        or value.get("base_package_changes") != 0
        or value.get("archive_bytes") != 23651012
        or value.get("tools") != {"git": _GIT}
        or not _sha(value.get("base_dpkg_status_sha256"))
        or not _sha(value.get("package_index_sha256"))
        or not _sha(value.get("apt_simulation_sha256"))
        or type(value.get("packages")) is not list
        or type(value.get("base_packages")) is not list
        or len(value["packages"]) != 31
        or len(value["base_packages"]) != 87
    ):
        raise ValueError("ci_manifest_invalid")
    names = set()
    archive_names = set()
    total = 0
    for item in value["packages"]:
        if type(item) is not dict or set(item) != {
            "name",
            "version",
            "architecture",
            "filename",
            "size",
            "sha256",
            "depends",
            "pre_depends",
            "copyright",
        }:
            raise ValueError("ci_manifest_invalid")
        name = item.get("name")
        filename = _relative(item.get("filename"))
        copyright_item = item.get("copyright")
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or PurePosixPath(filename).name in archive_names
            or item.get("architecture") not in {"all", "amd64"}
            or not isinstance(item.get("version"), str)
            or not item["version"]
            or not isinstance(item.get("size"), int)
            or item["size"] <= 0
            or not _sha(item.get("sha256"))
            or type(copyright_item) is not dict
            or set(copyright_item) != {"path", "resolved_path", "sha256", "bytes"}
            or not _sha(copyright_item.get("sha256"))
            or not isinstance(copyright_item.get("bytes"), int)
            or copyright_item["bytes"] <= 0
        ):
            raise ValueError("ci_manifest_invalid")
        _relative(copyright_item["path"])
        _relative(copyright_item["resolved_path"])
        names.add(name)
        archive_names.add(PurePosixPath(filename).name)
        total += item["size"]
    base_names = set()
    for item in value["base_packages"]:
        if (
            type(item) is not dict
            or set(item) != {"name", "version", "architecture"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or item["name"] in base_names
            or not isinstance(item.get("version"), str)
            or not item["version"]
            or item.get("architecture") not in {"all", "amd64"}
        ):
            raise ValueError("ci_manifest_invalid")
        base_names.add(item["name"])
    git_package = next((item for item in value["packages"] if item["name"] == "git"), None)
    if (
        names & base_names
        or total != value["archive_bytes"]
        or git_package is None
        or git_package["version"] != "1:2.47.3-0+deb13u1"
        or git_package["sha256"]
        != "3e35662fd5c46add561703e54031a1d8ad9df45811927689f0a51122b13be722"
    ):
        raise ValueError("ci_manifest_invalid")
    return value


def load_ci_manifest(path: Path, expected_sha256: str) -> dict:
    try:
        raw = Path(path).read_bytes()
        if expected_sha256 != _MANIFEST_SHA256 or _digest(raw) != _MANIFEST_SHA256:
            raise ValueError("ci_manifest_invalid")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("ci_manifest_invalid") from error
    return validate_ci_manifest(value)


def validate_node_manifest(value: dict) -> dict:
    if (
        type(value) is not dict
        or set(value) != _NODE_KEYS
        or value.get("contract_version") != "ci_node_runtime_v1"
        or value.get("scope") != "test_only"
        or value.get("base_image") != _BASE_IMAGE
        or value.get("source_distribution") != "playwright==1.62.0"
        or value.get("archive") != _NODE_ARCHIVE
        or value.get("node") != _NODE
        or value.get("license") != _NODE_LICENSE
    ):
        raise ValueError("ci_node_manifest_invalid")
    for section in ("archive", "node", "license"):
        item = value[section]
        if any(type(item[field]) is not int or item[field] <= 0 for field in ("bytes",)):
            raise ValueError("ci_node_manifest_invalid")
        if not _sha(item["sha256"]):
            raise ValueError("ci_node_manifest_invalid")
    for field in ("filename",):
        try:
            _relative(value["archive"][field])
        except ValueError as error:
            raise ValueError("ci_node_manifest_invalid") from error
    for section in ("node", "license"):
        try:
            _relative(value[section]["member"])
            _relative(value[section]["path"])
        except ValueError as error:
            raise ValueError("ci_node_manifest_invalid") from error
    return value


def load_node_manifest(path: Path, expected_sha256: str) -> dict:
    try:
        raw = Path(path).read_bytes()
        if expected_sha256 != _NODE_MANIFEST_SHA256 or _digest(raw) != _NODE_MANIFEST_SHA256:
            raise ValueError("ci_node_manifest_invalid")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("ci_node_manifest_invalid") from error
    return validate_node_manifest(value)


def verify_archive(path: Path, *, size: int, sha256: str) -> None:
    path = Path(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size != size
        or _digest(path.read_bytes()) != sha256
    ):
        raise ValueError("ci_archive_invalid")


def _download(
    url: str,
    path: Path,
    item: dict,
    *,
    allowed_hosts=frozenset({"snapshot.debian.org", "snapshot-cloudflare.debian.org"}),
) -> None:
    if path.exists() or _linked(path):
        raise ValueError("ci_archive_invalid")
    started = time.monotonic()
    owned_identity = None
    digest = hashlib.sha256()
    count = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "42-ci-build/1"})
        with urllib.request.urlopen(request, timeout=20) as response, path.open("xb") as output:
            owned_identity = _identity(os.fstat(output.fileno()))
            if urlsplit(response.geturl()).hostname not in allowed_hosts:
                raise ValueError("ci_archive_invalid")
            while chunk := response.read(1024 * 1024):
                if count + len(chunk) > item["size"] or time.monotonic() - started > 180:
                    raise ValueError("ci_archive_invalid")
                output.write(chunk)
                count += len(chunk)
                digest.update(chunk)
        if count != item["size"] or digest.hexdigest() != item["sha256"]:
            raise ValueError("ci_archive_invalid")
    except Exception as error:
        if owned_identity is not None:
            _remove_owned_file(path, owned_identity, count, digest.hexdigest())
        if isinstance(error, ValueError):
            raise
        raise ValueError("ci_archive_invalid") from error


def download_archives(manifest: dict, destination: Path) -> list[Path]:
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("ci_download_destination_invalid")
    destination.mkdir(parents=True)
    paths = []
    for item in manifest["packages"]:
        path = destination / PurePosixPath(item["filename"]).name
        _download(manifest["snapshot"] + item["filename"], path, item)
        paths.append(path)
    if {path.name for path in destination.iterdir()} != {path.name for path in paths}:
        raise ValueError("ci_archive_invalid")
    return paths


def _safe_node_destination(destination: Path, output: Path) -> tuple[Path, Path]:
    destination = Path(destination)
    output = Path(output)
    if not destination.is_absolute() or not output.is_absolute() or output.parent != destination:
        raise ValueError("ci_node_destination_invalid")
    paths = (destination, output, *destination.parents, *output.parents)
    if any(_linked(path) for path in paths) or destination.exists() or output.exists():
        raise ValueError("ci_node_destination_invalid")
    return destination, output


def _linked(path: Path) -> bool:
    try:
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or (junction is not None and junction())
    except OSError:
        return True


def _identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def _path_identity(path: Path) -> tuple[int, int, int]:
    return _identity(path.stat(follow_symlinks=False))


def _same_owned_path(path: Path, identity: tuple[int, int, int]) -> bool:
    try:
        return not _linked(path) and _path_identity(path) == identity
    except OSError:
        return False


def _remove_owned_file(path: Path, identity: tuple[int, int, int], size: int, sha256: str) -> None:
    """Delete only the file this invocation created, proven by identity and by content.

    Identity alone stops proving ownership once the file is closed: the Linux filesystems
    under the engine image hand a freed inode number to the next file created in the same
    directory, so a foreign replacement can carry the identity of the owned file it
    displaced. Only the bytes this invocation wrote settle it.
    """
    if not _same_owned_path(path, identity):
        return
    digest = hashlib.sha256()
    count = 0
    try:
        with path.open("rb") as stream:
            if _identity(os.fstat(stream.fileno())) != identity:
                return
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                if count > size:
                    return
                digest.update(chunk)
    except OSError:
        return
    if count != size or digest.hexdigest() != sha256:
        return
    with suppress(OSError):
        path.unlink()


def _node_archive_members(archive: zipfile.ZipFile, manifest: dict) -> dict[str, zipfile.ZipInfo]:
    names = set()
    targets = {}
    expected = {
        manifest["node"]["member"]: (manifest["node"], 0o755),
        manifest["license"]["member"]: (manifest["license"], 0o644),
    }
    for info in archive.infolist():
        try:
            _relative(info.filename.rstrip("/"))
        except ValueError as error:
            raise ValueError("ci_node_archive_invalid") from error
        if info.filename in names:
            raise ValueError("ci_node_archive_invalid")
        names.add(info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError("ci_node_archive_invalid")
        if info.filename in expected:
            spec, permissions = expected[info.filename]
            if (
                not stat.S_ISREG(mode)
                or stat.S_IMODE(mode) != permissions
                or info.file_size != spec["bytes"]
            ):
                raise ValueError("ci_node_archive_invalid")
            targets[info.filename] = info
    if set(targets) != set(expected):
        raise ValueError("ci_node_archive_invalid")
    return targets


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    spec: dict,
) -> tuple[int, int, int]:
    digest = hashlib.sha256()
    count = 0
    owned_identity = None
    try:
        with archive.open(info) as source, target.open("xb") as output:
            owned_identity = _identity(os.fstat(output.fileno()))
            while chunk := source.read(1024 * 1024):
                if count + len(chunk) > spec["bytes"]:
                    raise ValueError("ci_node_archive_invalid")
                output.write(chunk)
                count += len(chunk)
                digest.update(chunk)
        if count != spec["bytes"] or digest.hexdigest() != spec["sha256"]:
            raise ValueError("ci_node_archive_invalid")
        return owned_identity
    except Exception:
        if owned_identity is not None:
            _remove_owned_file(target, owned_identity, count, digest.hexdigest())
        raise


def _verify_node_binary(path: Path, expected_version: str) -> str:
    header = path.read_bytes()[:20]
    if (
        len(header) < 20
        or header[:4] != b"\x7fELF"
        or header[4] != 2
        or header[5] != 1
        or int.from_bytes(header[18:20], "little") != 62
    ):
        raise ValueError("ci_node_binary_invalid")
    version = subprocess.run(
        [str(path), "--version"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    linkage = subprocess.run(
        ["ldd", str(path)],
        check=False,
        capture_output=True,
        timeout=30,
    )
    observed = version.stdout.decode("utf-8", errors="strict").strip()
    if (
        version.returncode
        or observed != expected_version
        or linkage.returncode
        or b"not found" in linkage.stdout
        or b"not found" in linkage.stderr
    ):
        raise ValueError("ci_node_binary_invalid")
    return observed


def extract_node_archive(
    manifest: dict,
    archive_path: Path,
    destination: Path,
    output: Path,
) -> dict:
    destination, output = _safe_node_destination(destination, output)
    if output in {
        destination / manifest["node"]["path"],
        destination / manifest["license"]["path"],
    }:
        raise ValueError("ci_node_destination_invalid")
    archive_path = Path(archive_path)
    try:
        verify_archive(
            archive_path,
            size=manifest["archive"]["bytes"],
            sha256=manifest["archive"]["sha256"],
        )
    except ValueError as error:
        raise ValueError("ci_node_archive_invalid") from error
    destination_identity = None
    owned_files = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _node_archive_members(archive, manifest)
            destination.mkdir()
            destination_identity = _path_identity(destination)
            for name in (manifest["node"]["member"], manifest["license"]["member"]):
                spec = (
                    manifest["node"] if name == manifest["node"]["member"] else manifest["license"]
                )
                target = destination / spec["path"]
                if not _same_owned_path(destination, destination_identity):
                    raise ValueError("ci_node_destination_invalid")
                target_identity = _extract_member(archive, members[name], target, spec)
                owned_files.append((target, target_identity, spec["bytes"], spec["sha256"]))
                target.chmod(0o755 if spec is manifest["node"] else 0o644)
        observed = _verify_node_binary(
            destination / manifest["node"]["path"], manifest["node"]["version_output"]
        )
        receipt = {
            "contract_version": "ci_node_installed_v1",
            "archive": {
                "bytes": manifest["archive"]["bytes"],
                "filename": manifest["archive"]["filename"],
                "sha256": manifest["archive"]["sha256"],
            },
            "node": {
                "bytes": manifest["node"]["bytes"],
                "path": manifest["node"]["path"],
                "sha256": manifest["node"]["sha256"],
                "version_output": observed,
            },
            "license": {
                "bytes": manifest["license"]["bytes"],
                "path": manifest["license"]["path"],
                "sha256": manifest["license"]["sha256"],
            },
        }
        payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        with output.open("xb") as stream:
            output_identity = _identity(os.fstat(stream.fileno()))
            owned_files.append((output, output_identity, len(payload), _digest(payload)))
            stream.write(payload)
        return receipt
    except Exception:
        for path, identity, size, sha256 in reversed(owned_files):
            _remove_owned_file(path, identity, size, sha256)
        if destination_identity is not None and _same_owned_path(destination, destination_identity):
            with suppress(OSError):
                destination.rmdir()
        raise


def install_node(manifest: dict, destination: Path, output: Path) -> dict:
    destination, output = _safe_node_destination(destination, output)
    with tempfile.TemporaryDirectory(prefix="42-ci-node-") as temporary:
        archive_path = Path(temporary) / manifest["archive"]["filename"]
        _download(
            manifest["archive"]["url"],
            archive_path,
            {
                "size": manifest["archive"]["bytes"],
                "sha256": manifest["archive"]["sha256"],
            },
            allowed_hosts=frozenset({"files.pythonhosted.org"}),
        )
        return extract_node_archive(manifest, archive_path, destination, output)


def verify_installed(manifest: dict, license_root: Path, output: Path) -> dict:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\n"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("ci_installed_inventory_invalid")
    actual = {}
    for line in result.stdout.decode("utf-8", errors="strict").splitlines():
        name, version, architecture = line.split("\t")
        actual[name] = {"version": version, "architecture": architecture}
    expected = {
        item["name"]: {
            "version": item["version"],
            "architecture": item["architecture"],
        }
        for item in [*manifest["base_packages"], *manifest["packages"]]
    }
    if actual != expected:
        raise ValueError("ci_installed_inventory_invalid")
    git = Path(_GIT["path"])
    if not git.is_file() or _digest(git.read_bytes()) != _GIT["sha256"]:
        raise ValueError("ci_git_invalid")
    version = subprocess.run([str(git), "--version"], check=False, capture_output=True, timeout=10)
    linkage = subprocess.run(["ldd", str(git)], check=False, capture_output=True, timeout=10)
    if (
        version.returncode
        or version.stdout.decode("utf-8").strip() != _GIT["version_output"]
        or linkage.returncode
        or b"not found" in linkage.stdout
    ):
        raise ValueError("ci_git_invalid")
    license_root = Path(license_root)
    if license_root.exists() or license_root.is_symlink():
        raise ValueError("ci_license_destination_invalid")
    license_root.mkdir(parents=True)
    licenses = {}
    for item in manifest["packages"]:
        spec = item["copyright"]
        source = Path("/").joinpath(*PurePosixPath(spec["resolved_path"]).parts)
        verify_archive(source, size=spec["bytes"], sha256=spec["sha256"])
        target = license_root / f"{item['name']}.copyright"
        target.write_bytes(source.read_bytes())
        licenses[target.name] = spec["sha256"]
    receipt = {
        "contract_version": "ci_git_installed_inventory_v1",
        "packages": actual,
        "git": _GIT,
        "licenses": licenses,
    }
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("ci_inventory_destination_invalid")
    with output.open("xb") as stream:
        stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode())
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download")
    download.add_argument("--manifest", required=True)
    download.add_argument("--manifest-sha256", required=True)
    download.add_argument("--destination", required=True)
    installed = commands.add_parser("verify-installed")
    installed.add_argument("--manifest", required=True)
    installed.add_argument("--manifest-sha256", required=True)
    installed.add_argument("--license-root", required=True)
    installed.add_argument("--output", required=True)
    node = commands.add_parser("install-node")
    node.add_argument("--manifest", required=True)
    node.add_argument("--manifest-sha256", required=True)
    node.add_argument("--destination", required=True)
    node.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "install-node":
            manifest = load_node_manifest(Path(args.manifest), args.manifest_sha256)
            result = install_node(manifest, Path(args.destination), Path(args.output))
        else:
            manifest = load_ci_manifest(Path(args.manifest), args.manifest_sha256)
        if args.command == "download":
            paths = download_archives(manifest, Path(args.destination))
            result = {"archive_count": len(paths)}
        elif args.command == "verify-installed":
            result = verify_installed(manifest, Path(args.license_root), Path(args.output))
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile):
        print("ci_tools_install_refused")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
