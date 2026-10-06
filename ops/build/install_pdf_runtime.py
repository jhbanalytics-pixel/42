import argparse
import hashlib
import json
import shutil
import stat
import subprocess
import sysconfig
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

_APPROVAL = {
    "proposal_sha256": "278b61d22cc3fb8debf7d1ec2dbae3eb8bdf609011c8cdfe6296d1b59ff77351",
    "dependency_manifest_sha256": "c983b80466080683bd0ba8b905dceeac6c36467702584fd5537bfc5c5b75a819",
    "css_safety_decision_sha256": "674364e434ee780effd743a055ccbde67c4ccb108c970a0980a50f28708ca177",
    "css_parser_inventory_sha256": "0d567fa7d1ca3dd32ba9c2ce6e65be1f0fb8ef811a53cbffd19e620d0e139e6f",
}
_PYTHON = {
    "playwright": "1.62.0",
    "pypdf": "6.18.1",
    "pyee": "13.0.0",
    "greenlet": "3.5.5",
    "typing-extensions": "4.16.0",
    "tinycss2": "1.5.1",
    "webencodings": "0.6.1",
}
_KEYS = {
    "contract_version",
    "approval",
    "base_image",
    "python",
    "browser",
    "debian",
    "runtime",
    "required_inputs",
}


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("manifest_invalid")
        result[key] = value
    return result


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("manifest_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError("manifest_invalid")
    return value


def load_build_manifest(path: Path, expected_sha256: str) -> dict:
    try:
        raw = Path(path).read_bytes()
        if not _sha(expected_sha256) or _digest(raw) != expected_sha256:
            raise ValueError("manifest_invalid")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("manifest_invalid") from error
    if (
        len(raw) > 2 * 1024 * 1024
        or type(value) is not dict
        or set(value) != _KEYS
        or value.get("contract_version") != "pdf_runtime_dependencies_v1"
        or value.get("approval") != _APPROVAL
        or value.get("python") != _PYTHON
        or value.get("browser", {}).get("url")
        != "https://cdn.playwright.dev/builds/cft/151.0.7922.34/linux64/chrome-headless-shell-linux64.zip"
        or value["browser"].get("revision") != "1234"
        or value["browser"].get("version") != "151.0.7922.34"
        or value["browser"].get("archive_size") != 120231126
        or value["browser"].get("archive_sha256")
        != "3cfc2bd00d1bafcf8a68dc74c9c92bb7150ddc8d26ade948a776316e1cec4f14"
        or value.get("debian", {}).get("snapshot") != "20260824T000000Z"
        or len(value["debian"].get("packages", [])) != 87
        or len(value["debian"].get("base_packages", [])) != 87
    ):
        raise ValueError("manifest_invalid")
    seen = set()
    for package in value["debian"]["packages"]:
        if (
            type(package) is not dict
            or package.get("download_name")
            != PurePosixPath(package.get("filename", "")).name
            or urlsplit(package.get("url", "")).hostname != "snapshot.debian.org"
            or not package["url"].endswith(package["filename"])
            or not isinstance(package.get("size"), int)
            or package["size"] <= 0
            or not _sha(package.get("sha256"))
            or package["download_name"].casefold() in seen
        ):
            raise ValueError("manifest_invalid")
        seen.add(package["download_name"].casefold())
        license_item = package.get("license")
        if (
            type(license_item) is not dict
            or not license_item.get("installed_path", "").startswith("/usr/share/doc/")
            or not isinstance(license_item.get("size"), int)
            or license_item["size"] < 0
            or not _sha(license_item.get("sha256"))
        ):
            raise ValueError("manifest_invalid")
    for name, member in value["browser"].get("members", {}).items():
        _relative(name)
        if (
            type(member) is not dict
            or not isinstance(member.get("size"), int)
            or member["size"] <= 0
            or not _sha(member.get("sha256"))
        ):
            raise ValueError("manifest_invalid")
    input_paths = set()
    for item in value["required_inputs"]:
        if type(item) is not dict or set(item) != {"path", "size", "sha256"}:
            raise ValueError("manifest_invalid")
        name = _relative(item["path"])
        if (
            name.casefold() in input_paths
            or not isinstance(item["size"], int)
            or item["size"] <= 0
            or not _sha(item["sha256"])
        ):
            raise ValueError("manifest_invalid")
        input_paths.add(name.casefold())
    license_paths = set()
    for item in value["runtime"].get("python_license_files", []):
        name = _relative(item.get("path"))
        if (
            type(item) is not dict
            or set(item) != {"path", "size", "sha256"}
            or name.casefold() in license_paths
            or not isinstance(item.get("size"), int)
            or item["size"] <= 0
            or not _sha(item.get("sha256"))
        ):
            raise ValueError("manifest_invalid")
        license_paths.add(name.casefold())
    if len(license_paths) != 15:
        raise ValueError("manifest_invalid")
    return value


def verify_archive(path: Path, *, size: int, sha256: str) -> None:
    path = Path(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size != size
        or _digest(path.read_bytes()) != sha256
    ):
        raise ValueError("archive_invalid")


def extract_browser_archive(
    archive: Path, destination: Path, spec: dict
) -> dict[str, str]:
    verify_archive(
        archive,
        size=spec["archive_size"],
        sha256=spec["archive_sha256"],
    )
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("browser_destination_invalid")
    observed = {}
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            names = set()
            if len(members) > 400:
                raise ValueError("browser_archive_invalid")
            destination.mkdir(parents=True)
            for member in members:
                name = _relative(member.filename.rstrip("/"))
                if name.casefold() in names or stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("browser_archive_invalid")
                names.add(name.casefold())
                target = destination.joinpath(*PurePosixPath(name).parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                raw = source.read(member)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(raw)
                observed[name] = _digest(raw)
            for name, expected in spec["members"].items():
                verify_archive(
                    destination.joinpath(*PurePosixPath(name).parts),
                    size=expected["size"],
                    sha256=expected["sha256"],
                )
            executable = (
                destination / "chrome-headless-shell-linux64/chrome-headless-shell"
            )
            executable.chmod(0o555)
            return {name: observed[name] for name in spec["members"]}
    except (OSError, KeyError, zipfile.BadZipFile, ValueError) as error:
        if destination.exists() and not destination.is_symlink():
            shutil.rmtree(destination)
        if isinstance(error, ValueError) and str(error) == "browser_archive_invalid":
            raise
        raise ValueError("browser_archive_invalid") from error


def verify_debian_archives(manifest: dict, root: Path) -> list[Path]:
    root = Path(root)
    expected = []
    for package in manifest["debian"]["packages"]:
        path = root / package["download_name"]
        try:
            verify_archive(path, size=package["size"], sha256=package["sha256"])
        except ValueError as error:
            raise ValueError("debian_archive_invalid") from error
        expected.append(path)
    if {path.name for path in root.glob("*.deb")} != {path.name for path in expected}:
        raise ValueError("debian_archive_invalid")
    return expected


def _linked_path(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _required_path(root: Path, name: str) -> Path:
    root = Path(root).absolute()
    if _linked_path(root) or not root.is_dir():
        raise ValueError("required_input_invalid")
    path = root
    for part in PurePosixPath(_relative(name)).parts:
        path = path / part
        if _linked_path(path):
            raise ValueError("required_input_invalid")
    try:
        if not path.is_file():
            raise ValueError(f"required_input_missing:{name}")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("required_input_invalid")
    except (OSError, RuntimeError) as error:
        raise ValueError("required_input_invalid") from error
    return path


def verify_required_inputs(root: Path, required: list[dict]) -> None:
    for item in required:
        if type(item) is not dict or set(item) != {"path", "size", "sha256"}:
            raise ValueError("required_input_invalid")
        path = _required_path(root, item["path"])
        raw = path.read_bytes()
        if len(raw) != item["size"] or _digest(raw) != item["sha256"]:
            raise ValueError("required_input_invalid")
        _required_path(root, item["path"])


def _download(url: str, path: Path, expected: dict, runtime: dict) -> None:
    if (
        path.exists()
        or path.is_symlink()
        or expected["size"] > runtime["download_limit_bytes"]
    ):
        raise ValueError("download_invalid")
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "42-runtime-build/1"})
    initial_host = urlsplit(url).hostname
    allowed_final_hosts = {initial_host}
    if initial_host == "cdn.playwright.dev":
        allowed_final_hosts.add("storage.googleapis.com")
    try:
        with (
            urllib.request.urlopen(
                request, timeout=runtime["connect_timeout_seconds"]
            ) as response,
            path.open("xb") as output,
        ):
            if urlsplit(response.geturl()).hostname not in allowed_final_hosts:
                raise ValueError("download_invalid")
            digest = hashlib.sha256()
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if (
                    count > runtime["download_limit_bytes"]
                    or time.monotonic() - started > runtime["total_timeout_seconds"]
                ):
                    raise ValueError("download_invalid")
                digest.update(chunk)
                output.write(chunk)
        if count != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError("download_invalid")
    except Exception as error:
        if path.exists() and not path.is_symlink():
            path.unlink()
        if isinstance(error, ValueError):
            raise
        raise ValueError("download_invalid") from error


def download_runtime(manifest: dict, download_root: Path, browser_root: Path) -> dict:
    download_root = Path(download_root)
    browser_root = Path(browser_root)
    if download_root.exists() or browser_root.exists():
        raise ValueError("download_destination_invalid")
    debian = download_root / "debian"
    debian.mkdir(parents=True)
    runtime = manifest["runtime"]
    browser = manifest["browser"]
    archive = download_root / "browser.zip"
    _download(
        browser["url"],
        archive,
        {"size": browser["archive_size"], "sha256": browser["archive_sha256"]},
        runtime,
    )
    for package in manifest["debian"]["packages"]:
        _download(package["url"], debian / package["download_name"], package, runtime)
    verify_debian_archives(manifest, debian)
    members = extract_browser_archive(archive, browser_root, browser)
    return {"browser_members": members, "debian_archives": 87}


def verify_installed_system(
    manifest: dict,
    browser_root: Path,
    license_root: Path,
    output: Path,
) -> dict:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\n"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("installed_inventory_invalid")
    actual = {}
    for line in result.stdout.decode("utf-8", errors="strict").splitlines():
        package, version, architecture = line.split("\t")
        actual[package] = {"version": version, "architecture": architecture}
    expected = {
        item["package"]: {
            "version": item["version"],
            "architecture": item["architecture"],
        }
        for item in [
            *manifest["debian"]["base_packages"],
            *manifest["debian"]["packages"],
        ]
    }
    if actual != expected:
        raise ValueError("installed_inventory_invalid")
    browser_root = Path(browser_root)
    browser_hashes = {}
    for name, item in manifest["browser"]["members"].items():
        path = browser_root.joinpath(*PurePosixPath(name).parts)
        verify_archive(path, size=item["size"], sha256=item["sha256"])
        browser_hashes[name] = item["sha256"]
    license_root = Path(license_root)
    if license_root.exists() or license_root.is_symlink():
        raise ValueError("license_destination_invalid")
    debian_root = license_root / "debian"
    browser_license_root = license_root / "chrome-headless-shell"
    debian_root.mkdir(parents=True)
    browser_license_root.mkdir(parents=True)
    license_hashes = {}
    for package in manifest["debian"]["packages"]:
        item = package["license"]
        source = Path(item["installed_path"])
        verify_archive(source, size=item["size"], sha256=item["sha256"])
        destination = debian_root / f"{package['package']}.copyright"
        destination.write_bytes(source.read_bytes())
        license_hashes[destination.relative_to(license_root).as_posix()] = item[
            "sha256"
        ]
    for name in ("ABOUT", "LICENSE.headless_shell"):
        source_name = f"chrome-headless-shell-linux64/{name}"
        source = browser_root / source_name
        destination = browser_license_root / name
        destination.write_bytes(source.read_bytes())
        license_hashes[destination.relative_to(license_root).as_posix()] = _digest(
            destination.read_bytes()
        )
    site_roots = {
        Path(sysconfig.get_paths()["purelib"]),
        Path(sysconfig.get_paths()["platlib"]),
    }
    python_root = license_root / "python"
    for item in manifest["runtime"]["python_license_files"]:
        relative = PurePosixPath(_relative(item["path"]))
        matches = [root.joinpath(*relative.parts) for root in site_roots]
        source = next((path for path in matches if path.is_file()), None)
        if source is None:
            raise ValueError("python_license_invalid")
        verify_archive(source, size=item["size"], sha256=item["sha256"])
        destination = python_root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        license_hashes[destination.relative_to(license_root).as_posix()] = item[
            "sha256"
        ]
    inventory = {
        "contract_version": "pdf_runtime_installed_inventory_v1",
        "packages": actual,
        "browser_members": browser_hashes,
        "licenses": license_hashes,
    }
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("inventory_destination_invalid")
    with output.open("xb") as stream:
        stream.write(
            json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
    return inventory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    download = subparsers.add_parser("download")
    download.add_argument("--manifest", required=True)
    download.add_argument("--manifest-sha256", required=True)
    download.add_argument("--download-root", required=True)
    download.add_argument("--browser-root", required=True)
    verify = subparsers.add_parser("verify-inputs")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--manifest-sha256", required=True)
    verify.add_argument("--root", required=True)
    installed = subparsers.add_parser("verify-installed")
    installed.add_argument("--manifest", required=True)
    installed.add_argument("--manifest-sha256", required=True)
    installed.add_argument("--browser-root", required=True)
    installed.add_argument("--license-root", required=True)
    installed.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = load_build_manifest(Path(args.manifest), args.manifest_sha256)
        if args.command == "download":
            result = download_runtime(
                manifest, Path(args.download_root), Path(args.browser_root)
            )
        elif args.command == "verify-inputs":
            verify_required_inputs(Path(args.root), manifest["required_inputs"])
            result = {"required_inputs": len(manifest["required_inputs"])}
        else:
            result = verify_installed_system(
                manifest,
                Path(args.browser_root),
                Path(args.license_root),
                Path(args.output),
            )
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError):
        print("pdf_runtime_install_refused")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
