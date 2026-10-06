from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIRECTORY = ROOT / "tests/fixtures/open_intelligence/v2"
CONTRACT_VERSION = "2.0.0"
ENGINE_MANIFEST_SHA256 = "3440cd263099157f43e107ea55a6a56c835bbc1a5e7e86ef8519ce1abd181ebf"
ENGINE_MANIFEST_FILE_SHA256 = "95578fbfc05a15f777ed531da1842cd2ec5fa9b05e33e75aeeb453c0173a2455"
REQUIRED_FIXTURE_IDS = frozenset(
    {
        "cited_answer_election",
        "dynamic_signal_ready",
        "evidence_contradictory",
        "evidence_plan_election",
        "evidence_ready",
        "evidence_thin",
        "evidence_unchecked",
        "golden_01_emerging_without_keyword",
        "golden_02_why_moving",
        "golden_03_cross_market_difference",
        "golden_04_carriers",
        "golden_05_history",
        "golden_06_brand_role",
        "golden_07_audience_lens",
        "golden_08_source_agreement",
        "golden_09_source_gap",
        "golden_10_custom",
        "golden_11_election_brand_role",
        "lineage_merge",
        "lineage_split",
        "pulse_watched_signal_line",
        "source_lab_all_statuses",
    }
)
EXPECTED_FILES = {"manifest.json", *(f"{item}.json" for item in REQUIRED_FIXTURE_IDS)}
MANIFEST_FIELDS = {"contract_version", "fixture_count", "fixtures", "manifest_sha256"}


class ParityError(ValueError):
    pass


def _fail(code: str) -> None:
    raise ParityError(code)


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _manifest_root(entries: list[dict[str, str]]) -> str:
    lines = "".join(
        f"{entry['fixture_id']} {entry['sha256']}\n"
        for entry in sorted(entries, key=lambda entry: entry["fixture_id"])
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _relative_files(directory: Path, scope: str) -> set[str]:
    try:
        root_mode = directory.lstat().st_mode
        if stat.S_ISLNK(root_mode):
            _fail(f"{scope}_symlink_invalid")
        if not stat.S_ISDIR(root_mode):
            _fail(f"{scope}_directory_invalid")

        files = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    _fail(f"{scope}_symlink_invalid")
                if stat.S_ISDIR(mode):
                    _fail(f"{scope}_directory_entry_invalid")
                if not stat.S_ISREG(mode):
                    _fail(f"{scope}_nonregular_entry_invalid")
                files.add(entry.name)
        return files
    except ParityError:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail(f"{scope}_directory_invalid")


def _read_bytes(path: Path, error_code: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        _fail(error_code)


def _parse_json(raw: bytes, error_code: str) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(error_code)
    if not isinstance(value, dict):
        _fail(error_code)
    return value


def _read_json(path: Path, error_code: str) -> tuple[dict, bytes]:
    raw = _read_bytes(path, error_code)
    return _parse_json(raw, error_code), raw


def verify_package(directory: Path, scope: str = "package") -> dict:
    if _relative_files(directory, scope) != EXPECTED_FILES:
        _fail(f"{scope}_file_set_invalid")

    manifest_bytes = _read_bytes(directory / "manifest.json", "manifest_json_invalid")
    if hashlib.sha256(manifest_bytes).hexdigest() != ENGINE_MANIFEST_FILE_SHA256:
        _fail("manifest_file_digest_mismatch")
    manifest = _parse_json(manifest_bytes, "manifest_json_invalid")
    if set(manifest) != MANIFEST_FIELDS:
        _fail("manifest_fields_invalid")
    if manifest_bytes != _canonical_bytes(manifest):
        _fail("manifest_noncanonical")
    if manifest["contract_version"] != CONTRACT_VERSION:
        _fail("manifest_version_invalid")
    if (
        type(manifest["fixture_count"]) is not int
        or manifest["fixture_count"] != len(REQUIRED_FIXTURE_IDS)
    ):
        _fail("manifest_count_invalid")
    entries = manifest["fixtures"]
    if not isinstance(entries, list) or len(entries) != len(REQUIRED_FIXTURE_IDS):
        _fail("manifest_entries_invalid")

    fixture_ids = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"fixture_id", "sha256"}:
            _fail("manifest_entry_invalid")
        fixture_id = entry["fixture_id"]
        digest = entry["sha256"]
        if not isinstance(fixture_id, str) or not isinstance(digest, str):
            _fail("manifest_entry_invalid")
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            _fail("manifest_digest_invalid")
        fixture_ids.append(fixture_id)
    if fixture_ids != sorted(REQUIRED_FIXTURE_IDS):
        _fail("manifest_fixture_ids_invalid")

    for entry in entries:
        fixture_id = entry["fixture_id"]
        fixture, fixture_bytes = _read_json(
            directory / f"{fixture_id}.json", "fixture_json_invalid"
        )
        if fixture_bytes != _canonical_bytes(fixture):
            _fail("fixture_noncanonical")
        if fixture.get("fixture_id") != fixture_id:
            _fail("fixture_id_invalid")
        if fixture.get("contract_version") != CONTRACT_VERSION:
            _fail("fixture_version_invalid")
        if hashlib.sha256(fixture_bytes).hexdigest() != entry["sha256"]:
            _fail("fixture_digest_mismatch")

    manifest_root = _manifest_root(entries)
    if manifest["manifest_sha256"] != manifest_root:
        _fail("manifest_root_mismatch")
    if manifest_root != ENGINE_MANIFEST_SHA256:
        _fail("engine_manifest_root_mismatch")
    return manifest


def verify_engine_parity(
    local_directory: Path, engine_directory: Path, local_manifest: dict
) -> dict:
    local_files = _relative_files(local_directory, "package")
    engine_files = _relative_files(engine_directory, "engine")
    if engine_files != local_files:
        _fail("engine_file_set_mismatch")

    try:
        for relative_path in sorted(local_files):
            local_bytes = (local_directory / relative_path).read_bytes()
            engine_bytes = (engine_directory / relative_path).read_bytes()
            if hashlib.sha256(local_bytes).digest() != hashlib.sha256(engine_bytes).digest():
                _fail("engine_byte_mismatch")
    except ParityError:
        raise
    except OSError:
        _fail("engine_read_invalid")

    engine_manifest = verify_package(engine_directory, "engine")
    if local_manifest["manifest_sha256"] != ENGINE_MANIFEST_SHA256:
        _fail("local_manifest_root_mismatch")
    if engine_manifest["manifest_sha256"] != ENGINE_MANIFEST_SHA256:
        _fail("engine_manifest_root_mismatch")
    return engine_manifest


def _parse_engine_directory(arguments: list[str]) -> Path | None:
    if not arguments:
        return None
    if len(arguments) == 2 and arguments[0] == "--engine-dir" and arguments[1]:
        return Path(arguments[1])
    _fail("cli_invalid")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        engine_directory = _parse_engine_directory(arguments)
        manifest = verify_package(FIXTURE_DIRECTORY)
        engine_manifest = None
        if engine_directory is not None:
            engine_manifest = verify_engine_parity(
                FIXTURE_DIRECTORY, engine_directory, manifest
            )
    except ParityError as error:
        print(f"FAIL {error}")
        return 1
    except Exception:
        print("FAIL unexpected_local_error")
        return 1

    print(f"fixture_count={manifest['fixture_count']}")
    print(f"contract_version={manifest['contract_version']}")
    print(f"manifest_sha256={manifest['manifest_sha256']}")
    if engine_manifest is not None:
        print(f"engine_file_count={len(EXPECTED_FILES)}")
        print(f"engine_manifest_sha256={engine_manifest['manifest_sha256']}")
        print(f"byte_parity={len(EXPECTED_FILES)}/{len(EXPECTED_FILES)}")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
