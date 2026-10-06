"""Inventory of every fixture family and pinned asset a candidate tree ships (E03).

A fixture family is one directory directly under a tests fixtures tree, or the
fixtures directory itself for the files that sit at its top level. For each
family the inventory records the tracked files with their sha256, the contract
version literals the fixture bytes declare, the tracked code files that name
the family or one of its files, and for each literal the code files that carry
it. Two pinned assets sit beside the families: the vendored UI package the
frontend depends on, with its tgz digest checked against the sidecar and the
lockfile resolution, and the Linux gate manifest, with its digest checked
against the pin the publish configuration carries.

The inventory is data about the tree; it passes nothing. A literal no code
names is listed under unread_contract_versions so the gap stays visible.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "fixture_inventory_v2"
INVENTORY_NAME = "fixture_inventory_v2.json"
FIXTURE_SEGMENT = "fixtures"
FRONTEND_PACKAGE = "app/frontend/package.json"
FRONTEND_LOCK = "app/frontend/bun.lock"
FRONTEND_VENDOR = "app/frontend/vendor"
UI_PACKAGE_NAME = "ogilvy-intelligence-design-system"
GATE_MANIFEST = "ops/tests/fixtures/linux_boundary/manifest.json"
PUBLISH_CONFIG = "cloudbuild.app.publish.yaml"
CODE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".mjs",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".md",
    ".sql",
    ".txt",
    ".cfg",
    ".ini",
}
CONTRACT_KEY = "contract_version"
CONTRACT_LITERAL = re.compile(
    r"""["']?contract_version["']?\s*[:=]\s*["']([^"'\n]+)["']"""
)
FILE_DEPENDENCY = re.compile(
    r"^file:(?P<path>.+/(?P<name>[^/]+)-(?P<version>\d+\.\d+\.\d+)\.tgz)$"
)
WALK_EXCLUDES = {".git", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tracked_files(root: Path) -> list[str]:
    """Tracked paths relative to root, from git, or a filesystem walk without git."""
    root = Path(root)
    if (root / ".git").exists():
        try:
            listing = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--full-name"],
                capture_output=True,
                check=True,
                timeout=120,
            ).stdout.decode("utf-8")
        except (OSError, subprocess.SubprocessError):
            listing = None
        if listing is not None:
            return sorted(p for p in listing.split("\0") if p and (root / p).is_file())
    paths = []
    for path in root.rglob("*"):
        if path.is_file() and not any(part in WALK_EXCLUDES for part in path.parts):
            paths.append(path.relative_to(root).as_posix())
    return sorted(paths)


def family_of(relative: str) -> str | None:
    parts = relative.split("/")
    if FIXTURE_SEGMENT not in parts:
        return None
    index = parts.index(FIXTURE_SEGMENT)
    depth = index + 2 if len(parts) > index + 2 else index + 1
    return "/".join(parts[:depth])


def _contract_values(value, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == CONTRACT_KEY and isinstance(item, str):
                found.add(item)
            _contract_values(item, found)
    elif isinstance(value, list):
        for item in value:
            _contract_values(item, found)


def contract_versions(path: Path) -> list[str]:
    """Every contract_version literal a fixture file declares, by parse or by pattern."""
    data = path.read_bytes()
    found: set[str] = set()
    if path.suffix.lower() == ".json":
        try:
            _contract_values(json.loads(data), found)
            return sorted(found)
        except ValueError:
            pass
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    found.update(match.group(1) for match in CONTRACT_LITERAL.finditer(text))
    return sorted(found)


def _is_text_code(relative: str) -> bool:
    return Path(relative).suffix.lower() in CODE_SUFFIXES


def _read_text(root: Path, relative: str) -> str | None:
    data = (root / relative).read_bytes()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def build_inventory(root: Path) -> dict:
    root = Path(root)
    paths = tracked_files(root)
    fixture_paths = [p for p in paths if family_of(p) is not None]
    code_texts = {
        p: text
        for p in paths
        if _is_text_code(p)
        for text in [_read_text(root, p)]
        if text is not None
    }
    outside_fixtures = {p: t for p, t in code_texts.items() if family_of(p) is None}
    families: dict[str, dict] = {}
    for relative in fixture_paths:
        family = family_of(relative)
        entry = families.setdefault(
            family,
            {
                "path": family,
                "files": [],
                "contract_versions": {},
                "reader_candidates": [],
                "verified_readers": [],
            },
        )
        entry["files"].append(
            {
                "path": relative,
                "sha256": sha256_file(root / relative),
                "bytes": (root / relative).stat().st_size,
                "contract_versions": contract_versions(root / relative),
            }
        )
    unread = []
    for family, entry in families.items():
        names = {Path(f["path"]).name for f in entry["files"]}
        family_tail = family.split("/", 1)[1] if "/" in family else family
        readers = sorted(
            p
            for p, text in outside_fixtures.items()
            if family in text
            or family_tail in text
            or any(name in text for name in names)
        )
        entry["reader_candidates"] = readers
        literals = sorted({v for f in entry["files"] for v in f["contract_versions"]})
        for literal in literals:
            entry["contract_versions"][literal] = []
            unread.append({"family": family, "contract_version": literal})
    ui = ui_package_pin(root)
    gate = gate_manifest_pin(root)
    inventory = {
        "schema": SCHEMA,
        "root_files": len(paths),
        "fixture_files": len(fixture_paths),
        "families": [families[key] for key in sorted(families)],
        "unread_contract_versions": unread,
        "ui_package": ui,
        "linux_gate_manifest": gate,
    }
    canonical = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
    inventory["inventory_digest"] = sha256_bytes(canonical.encode("utf-8"))
    return inventory


def ui_package_pin(root: Path) -> dict:
    """The vendored UI package: dependency string, lock resolution, tgz digest and sidecar."""
    package_path = root / FRONTEND_PACKAGE
    result = {
        "package_json": FRONTEND_PACKAGE,
        "dependency": None,
        "version": None,
        "tgz": None,
        "tgz_sha256": None,
        "sidecar": None,
        "sidecar_sha256": None,
        "sidecar_matches": None,
        "lock_resolution_present": None,
        "unreferenced_vendor_files": [],
    }
    if not package_path.is_file():
        return result
    package = json.loads(package_path.read_bytes())
    dependency = (package.get("dependencies") or {}).get(UI_PACKAGE_NAME)
    result["dependency"] = dependency
    match = FILE_DEPENDENCY.match(dependency or "")
    if match is None:
        return result
    tgz = Path(FRONTEND_PACKAGE).parent / match.group("path")
    result["version"] = match.group("version")
    result["tgz"] = tgz.as_posix()
    if (root / tgz).is_file():
        result["tgz_sha256"] = sha256_file(root / tgz)
    sidecar = tgz.with_suffix(".sha256")
    if (root / sidecar).is_file():
        result["sidecar"] = sidecar.as_posix()
        declared = (root / sidecar).read_text(encoding="utf-8").split()[0].lower()
        result["sidecar_sha256"] = declared
        result["sidecar_matches"] = declared == result["tgz_sha256"]
    lock = root / FRONTEND_LOCK
    if lock.is_file():
        result["lock_resolution_present"] = (
            f"{UI_PACKAGE_NAME}@{match.group('path')}"
            in lock.read_text(encoding="utf-8")
        )
    vendor = root / FRONTEND_VENDOR
    if vendor.is_dir():
        referenced = {tgz.name, sidecar.name}
        result["unreferenced_vendor_files"] = sorted(
            (Path(FRONTEND_VENDOR) / p.name).as_posix()
            for p in vendor.iterdir()
            if p.is_file() and p.name not in referenced
        )
    return result


def gate_manifest_pin(root: Path) -> dict:
    """The Linux gate manifest digest and the pin the publish configuration carries."""
    manifest = root / GATE_MANIFEST
    result = {
        "path": GATE_MANIFEST,
        "sha256": None,
        "contract_version": None,
        "publish_config": PUBLISH_CONFIG,
        "pinned_in_publish_config": None,
    }
    if not manifest.is_file():
        return result
    result["sha256"] = sha256_file(manifest)
    try:
        result["contract_version"] = json.loads(manifest.read_bytes()).get(CONTRACT_KEY)
    except ValueError:
        result["contract_version"] = None
    publish = root / PUBLISH_CONFIG
    if publish.is_file():
        result["pinned_in_publish_config"] = result["sha256"] in publish.read_text(
            encoding="utf-8"
        )
    return result


def write_inventory(root: Path, output_dir: Path) -> dict:
    inventory = build_inventory(root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / INVENTORY_NAME).write_bytes(
        json.dumps(inventory, indent=2, sort_keys=True).encode("utf-8")
    )
    return inventory


def summary_lines(inventory: dict) -> list[str]:
    lines = [
        f"families: {len(inventory['families'])} fixture_files: {inventory['fixture_files']}"
    ]
    for family in inventory["families"]:
        lines.append(
            f"{family['path']}: files={len(family['files'])} "
            f"contract_versions={sorted(family['contract_versions'])} "
            f"reader_candidates={len(family['reader_candidates'])} "
            f"verified_readers={len(family['verified_readers'])}"
        )
    lines.append(f"unproven contract versions: {inventory['unread_contract_versions']}")
    ui = inventory["ui_package"]
    lines.append(
        f"ui package: {ui['version']} tgz_sha256={ui['tgz_sha256']} "
        f"sidecar_matches={ui['sidecar_matches']} lock={ui['lock_resolution_present']}"
    )
    gate = inventory["linux_gate_manifest"]
    lines.append(
        f"gate manifest: sha256={gate['sha256']} pinned={gate['pinned_in_publish_config']}"
    )
    lines.append(f"inventory digest: {inventory['inventory_digest']}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ops.certification.fixture_inventory",
        description="Inventory every fixture family and pinned asset of a candidate tree.",
    )
    parser.add_argument("--root", required=True, help="candidate repository root")
    parser.add_argument("--output", required=True, help="directory for the inventory")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"root is not a directory: {root}", file=sys.stderr)
        return 1
    inventory = write_inventory(root, Path(args.output))
    print("\n".join(summary_lines(inventory)))
    print(f"inventory: {(Path(args.output) / INVENTORY_NAME).resolve().as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
