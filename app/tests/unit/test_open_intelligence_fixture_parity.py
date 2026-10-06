from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "tests/fixtures/open_intelligence/v2"
SCRIPT = ROOT / "scripts/verify_open_intelligence_fixture_parity.py"
ENGINE_MANIFEST_ROOT = "3440cd263099157f43e107ea55a6a56c835bbc1a5e7e86ef8519ce1abd181ebf"
ENGINE_MANIFEST_FILE_SHA256 = "95578fbfc05a15f777ed531da1842cd2ec5fa9b05e33e75aeeb453c0173a2455"
REQUIRED_FIXTURE_IDS = {
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


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _manifest_root(entries: list[dict[str, str]]) -> str:
    lines = "".join(
        f"{entry['fixture_id']} {entry['sha256']}\n"
        for entry in sorted(entries, key=lambda entry: entry["fixture_id"])
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_bytes(value))


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _restore(directory: Path, snapshot: dict[str, bytes]) -> None:
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    for relative_path, content in snapshot.items():
        path = directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _isolated_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "listening-post"
    script = root / "scripts/verify_open_intelligence_fixture_parity.py"
    package = root / "tests/fixtures/open_intelligence/v2"
    script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, script)
    shutil.copytree(FIXTURE_DIR, package)
    return root, script, package


def _run_verifier(
    root: Path,
    script: Path,
    engine_dir: Path | None = None,
    arguments: list[str] | None = None,
):
    command = [sys.executable, str(script)]
    if engine_dir is not None:
        command.extend(["--engine-dir", str(engine_dir)])
    if arguments is not None:
        command.extend(arguments)
    return subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _success_lines(engine_mode: bool = False) -> list[str]:
    lines = [
        "fixture_count=22",
        "contract_version=2.0.0",
        f"manifest_sha256={ENGINE_MANIFEST_ROOT}",
    ]
    if engine_mode:
        lines.extend(
            [
                "engine_file_count=23",
                f"engine_manifest_sha256={ENGINE_MANIFEST_ROOT}",
                "byte_parity=23/23",
            ]
        )
    return [*lines, "PASS"]


def _assert_success(
    result: subprocess.CompletedProcess[str], *, engine_mode: bool = False
) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == _success_lines(engine_mode)


def _assert_failure(
    result: subprocess.CompletedProcess[str],
    expected_code: str,
    *forbidden_values: str | Path,
) -> None:
    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout == f"FAIL {expected_code}\n"
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined
    assert "{" not in combined
    for value in forbidden_values:
        texts = {str(value)}
        if isinstance(value, Path):
            texts.add(value.as_posix())
        for text in texts:
            assert text not in combined


class _ScandirResult:
    def __init__(self, entries: list[object]):
        self.entries = entries

    def __enter__(self):
        return iter(self.entries)

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _LstatEntry:
    def __init__(self, name: str, mode: int):
        self.name = name
        self.mode = mode

    def stat(self, *, follow_symlinks: bool = True):
        assert follow_symlinks is False
        return os.stat_result((self.mode, 0, 0, 0, 0, 0, 0, 0, 0, 0))


class _SymlinkOSError(OSError):
    def __init__(self, winerror: int | None):
        super().__init__("symlink probe failure")
        self.winerror = winerror


def _allow_mocked_symlink_fallback(error: OSError) -> None:
    if sys.platform == "win32" and getattr(error, "winerror", None) == 1314:
        return
    raise error


def _load_verifier(script: Path):
    spec = importlib.util.spec_from_file_location("fixture_parity_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mock_symlink_entry(monkeypatch, module, directory: Path, filename: str) -> None:
    assert hasattr(module, "os")
    real_scandir = module.os.scandir

    def fake_scandir(path):
        path = Path(path)
        if path != directory:
            return real_scandir(path)
        entries = []
        with real_scandir(path) as actual_entries:
            for entry in actual_entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if entry.name == filename:
                    mode = stat.S_IFLNK | 0o777
                entries.append(_LstatEntry(entry.name, mode))
        return _ScandirResult(entries)

    monkeypatch.setattr(module.os, "scandir", fake_scandir)


def _refresh_manifest(package: Path, fixture_id: str) -> None:
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(item for item in manifest["fixtures"] if item["fixture_id"] == fixture_id)
    entry["sha256"] = hashlib.sha256((package / f"{fixture_id}.json").read_bytes()).hexdigest()
    manifest["manifest_sha256"] = _manifest_root(manifest["fixtures"])
    _write_json(manifest_path, manifest)


def _mutate_package(case: str, package: Path) -> tuple[list[Path], list[str]]:
    fixture_id = "evidence_ready"
    fixture_path = package / f"{fixture_id}.json"
    manifest_path = package / "manifest.json"
    if case == "byte_drift":
        fixture_path.write_bytes(fixture_path.read_bytes() + b" ")
    elif case == "coordinated_manifest_drift":
        fixture = json.loads(fixture_path.read_bytes())
        fixture["sanitized"] = False
        _write_json(fixture_path, fixture)
        _refresh_manifest(package, fixture_id)
    elif case == "missing_file":
        fixture_path.unlink()
    elif case == "extra_file":
        (package / "extra.json").write_bytes(b"{}\n")
    elif case == "renamed_file":
        fixture_path.rename(package / "evidence_ready_renamed.json")
    elif case == "noncanonical_json":
        fixture_path.write_text(
            json.dumps(json.loads(fixture_path.read_bytes()), ensure_ascii=False),
            encoding="utf-8",
        )
    elif case == "wrong_manifest_version":
        manifest = json.loads(manifest_path.read_bytes())
        manifest["contract_version"] = "2.0.1"
        _write_json(manifest_path, manifest)
    elif case == "wrong_fixture_version":
        fixture = json.loads(fixture_path.read_bytes())
        fixture["contract_version"] = "2.0.1"
        _write_json(fixture_path, fixture)
    elif case == "wrong_fixture_id":
        fixture = json.loads(fixture_path.read_bytes())
        fixture["fixture_id"] = "evidence_thin"
        _write_json(fixture_path, fixture)
    elif case == "wrong_count":
        manifest = json.loads(manifest_path.read_bytes())
        manifest["fixture_count"] = 21
        _write_json(manifest_path, manifest)
    elif case == "wrong_id_set":
        manifest = json.loads(manifest_path.read_bytes())
        manifest["fixtures"][0]["fixture_id"] = "unapproved_fixture"
        manifest["manifest_sha256"] = _manifest_root(manifest["fixtures"])
        _write_json(manifest_path, manifest)
    elif case == "canonical_float_count":
        manifest = json.loads(manifest_path.read_bytes())
        manifest["fixture_count"] = 22.0
        _write_json(manifest_path, manifest)
    elif case == "invalid_manifest_json":
        manifest_path.write_bytes(b'{"fixture_count":22.0')
    elif case == "extra_empty_directory":
        path = package / "nested"
        path.mkdir()
        return [path], ["nested"]
    else:
        raise AssertionError(f"unknown mutation case: {case}")
    fragments = {
        "coordinated_manifest_drift": ["sanitized"],
        "wrong_manifest_version": ["2.0.1"],
        "wrong_fixture_version": ["2.0.1"],
        "wrong_fixture_id": ["evidence_thin"],
        "wrong_id_set": ["unapproved_fixture"],
        "canonical_float_count": ["22.0"],
        "invalid_manifest_json": ["fixture_count"],
    }
    mutated_paths = {
        "extra_file": [package / "extra.json"],
        "renamed_file": [fixture_path, package / "evidence_ready_renamed.json"],
        "missing_file": [fixture_path],
    }.get(case, [manifest_path if "manifest" in case or case in {"wrong_count", "wrong_id_set"} else fixture_path])
    return mutated_paths, fragments.get(case, [])


def test_local_package_matches_the_certified_engine_manifest():
    expected_files = {"manifest.json", *(f"{item}.json" for item in REQUIRED_FIXTURE_IDS)}
    assert {path.name for path in FIXTURE_DIR.iterdir() if path.is_file()} == expected_files

    manifest_bytes = (FIXTURE_DIR / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert hashlib.sha256(manifest_bytes).hexdigest() == ENGINE_MANIFEST_FILE_SHA256
    assert manifest_bytes == _canonical_bytes(manifest)
    assert manifest["contract_version"] == "2.0.0"
    assert manifest["fixture_count"] == 22
    assert [entry["fixture_id"] for entry in manifest["fixtures"]] == sorted(
        REQUIRED_FIXTURE_IDS
    )

    for entry in manifest["fixtures"]:
        fixture_path = FIXTURE_DIR / f"{entry['fixture_id']}.json"
        fixture_bytes = fixture_path.read_bytes()
        fixture = json.loads(fixture_bytes)
        assert fixture_bytes == _canonical_bytes(fixture)
        assert hashlib.sha256(fixture_bytes).hexdigest() == entry["sha256"]
        assert fixture["fixture_id"] == entry["fixture_id"]
        assert fixture["contract_version"] == "2.0.0"

    assert _manifest_root(manifest["fixtures"]) == ENGINE_MANIFEST_ROOT
    assert manifest["manifest_sha256"] == ENGINE_MANIFEST_ROOT

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    _assert_success(result)


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("byte_drift", "fixture_noncanonical"),
        ("coordinated_manifest_drift", "manifest_file_digest_mismatch"),
        ("missing_file", "package_file_set_invalid"),
        ("extra_file", "package_file_set_invalid"),
        ("renamed_file", "package_file_set_invalid"),
        ("noncanonical_json", "fixture_noncanonical"),
        ("wrong_manifest_version", "manifest_file_digest_mismatch"),
        ("wrong_fixture_version", "fixture_version_invalid"),
        ("wrong_fixture_id", "fixture_id_invalid"),
        ("wrong_count", "manifest_file_digest_mismatch"),
        ("wrong_id_set", "manifest_file_digest_mismatch"),
        ("canonical_float_count", "manifest_file_digest_mismatch"),
        ("invalid_manifest_json", "manifest_file_digest_mismatch"),
        ("extra_empty_directory", "package_directory_entry_invalid"),
    ],
)
def test_mutation_harness_rejects_local_drift_and_restores_bytes(
    tmp_path: Path, case: str, expected_code: str
):
    root, script, package = _isolated_root(tmp_path)
    original = _snapshot(package)
    _assert_success(_run_verifier(root, script))

    try:
        mutated_paths, payload_fragments = _mutate_package(case, package)
        result = _run_verifier(root, script)
        _assert_failure(
            result,
            expected_code,
            root,
            package,
            *mutated_paths,
            *payload_fragments,
        )
    finally:
        _restore(package, original)

    assert _snapshot(package) == original
    _assert_success(_run_verifier(root, script))


def test_mutation_harness_rejects_a_wrong_pinned_engine_root_and_restores_bytes(
    tmp_path: Path,
):
    root, script, _ = _isolated_root(tmp_path)
    original = script.read_bytes()
    _assert_success(_run_verifier(root, script))

    try:
        changed = original.replace(ENGINE_MANIFEST_ROOT.encode(), b"0" * 64)
        assert changed != original
        script.write_bytes(changed)
        result = _run_verifier(root, script)
        _assert_failure(result, "engine_manifest_root_mismatch", root, script)
    finally:
        script.write_bytes(original)

    assert script.read_bytes() == original
    _assert_success(_run_verifier(root, script))


def test_mutation_harness_rejects_a_wrong_manifest_file_pin_and_restores_bytes(
    tmp_path: Path,
):
    root, script, _ = _isolated_root(tmp_path)
    original = script.read_bytes()
    _assert_success(_run_verifier(root, script))

    try:
        changed = original.replace(ENGINE_MANIFEST_FILE_SHA256.encode(), b"f" * 64)
        assert changed != original
        script.write_bytes(changed)
        result = _run_verifier(root, script)
        _assert_failure(result, "manifest_file_digest_mismatch", root, script)
    finally:
        script.write_bytes(original)

    assert script.read_bytes() == original
    _assert_success(_run_verifier(root, script))


def test_float_count_is_rejected_when_manifest_file_pin_is_coordinated(tmp_path: Path):
    root, script, package = _isolated_root(tmp_path)
    original_script = script.read_bytes()
    original_package = _snapshot(package)
    manifest_path = package / "manifest.json"
    _assert_success(_run_verifier(root, script))

    try:
        manifest = json.loads(manifest_path.read_bytes())
        manifest["fixture_count"] = 22.0
        _write_json(manifest_path, manifest)
        changed_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        changed_script = original_script.replace(
            ENGINE_MANIFEST_FILE_SHA256.encode(), changed_digest.encode()
        )
        assert changed_script != original_script
        script.write_bytes(changed_script)
        result = _run_verifier(root, script)
        _assert_failure(
            result,
            "manifest_count_invalid",
            root,
            package,
            manifest_path,
            script,
            "22.0",
        )
    finally:
        script.write_bytes(original_script)
        _restore(package, original_package)

    assert script.read_bytes() == original_script
    assert _snapshot(package) == original_package
    _assert_success(_run_verifier(root, script))


def test_engine_directory_mode_proves_complete_byte_parity(tmp_path: Path):
    engine_dir = tmp_path / "engine-v2"
    shutil.copytree(FIXTURE_DIR, engine_dir)

    result = _run_verifier(ROOT, SCRIPT, engine_dir)

    _assert_success(result, engine_mode=True)


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("byte_mismatch", "engine_byte_mismatch"),
        ("extra_file", "engine_file_set_mismatch"),
        ("missing_file", "engine_file_set_mismatch"),
        ("extra_empty_directory", "engine_directory_entry_invalid"),
    ],
)
def test_engine_directory_mutations_fail_and_restore_bytes(
    tmp_path: Path, case: str, expected_code: str
):
    engine_dir = tmp_path / "engine-v2"
    shutil.copytree(FIXTURE_DIR, engine_dir)
    original = _snapshot(engine_dir)
    fixture_path = engine_dir / "evidence_ready.json"
    _assert_success(_run_verifier(ROOT, SCRIPT, engine_dir), engine_mode=True)

    try:
        if case == "byte_mismatch":
            fixture_path.write_bytes(fixture_path.read_bytes() + b" ")
        elif case == "extra_file":
            (engine_dir / "extra.json").write_bytes(b"{}\n")
            mutated_paths = [engine_dir / "extra.json"]
        elif case == "missing_file":
            fixture_path.unlink()
            mutated_paths = [fixture_path]
        else:
            path = engine_dir / "nested"
            path.mkdir()
            mutated_paths = [path]
        if case == "byte_mismatch":
            mutated_paths = [fixture_path]
        result = _run_verifier(ROOT, SCRIPT, engine_dir)
        _assert_failure(
            result,
            expected_code,
            ROOT,
            engine_dir,
            *mutated_paths,
            "outside-evidence-ready",
        )
    finally:
        _restore(engine_dir, original)

    assert _snapshot(engine_dir) == original
    _assert_success(_run_verifier(ROOT, SCRIPT, engine_dir), engine_mode=True)


def test_local_file_symlink_is_rejected_before_read(
    tmp_path: Path, monkeypatch, capsys
):
    root, script, package = _isolated_root(tmp_path)
    original = _snapshot(package)
    fixture_path = package / "evidence_ready.json"
    target = package.parent / "outside-evidence-ready.json"
    _assert_success(_run_verifier(root, script))

    target.write_bytes(fixture_path.read_bytes())
    fixture_path.unlink()
    try:
        os.symlink(target, fixture_path)
    except OSError as error:
        _allow_mocked_symlink_fallback(error)
        _restore(package, original)
        module = _load_verifier(script)
        _mock_symlink_entry(monkeypatch, module, package, fixture_path.name)
        monkeypatch.setattr(module, "FIXTURE_DIRECTORY", package)
        return_code = module.main([])
        captured = capsys.readouterr()
        result = subprocess.CompletedProcess([], return_code, captured.out, captured.err)
        _assert_failure(
            result,
            "package_symlink_invalid",
            root,
            package,
            fixture_path,
            target,
            "outside-evidence-ready",
        )
    else:
        result = _run_verifier(root, script)
        _assert_failure(
            result,
            "package_symlink_invalid",
            root,
            package,
            fixture_path,
            target,
            "outside-evidence-ready",
        )
    finally:
        _restore(package, original)

    assert _snapshot(package) == original
    _assert_success(_run_verifier(root, script))


def test_engine_file_symlink_is_rejected_before_read(
    tmp_path: Path, monkeypatch, capsys
):
    engine_dir = tmp_path / "engine-v2"
    shutil.copytree(FIXTURE_DIR, engine_dir)
    original = _snapshot(engine_dir)
    fixture_path = engine_dir / "evidence_ready.json"
    target = engine_dir.parent / "outside-evidence-ready.json"
    _assert_success(_run_verifier(ROOT, SCRIPT, engine_dir), engine_mode=True)

    target.write_bytes(fixture_path.read_bytes())
    fixture_path.unlink()
    try:
        os.symlink(target, fixture_path)
    except OSError as error:
        _allow_mocked_symlink_fallback(error)
        _restore(engine_dir, original)
        module = _load_verifier(SCRIPT)
        _mock_symlink_entry(monkeypatch, module, engine_dir, fixture_path.name)
        return_code = module.main(["--engine-dir", str(engine_dir)])
        captured = capsys.readouterr()
        result = subprocess.CompletedProcess([], return_code, captured.out, captured.err)
        _assert_failure(
            result,
            "engine_symlink_invalid",
            ROOT,
            engine_dir,
            fixture_path,
            target,
            "outside-evidence-ready",
        )
    else:
        result = _run_verifier(ROOT, SCRIPT, engine_dir)
        _assert_failure(
            result,
            "engine_symlink_invalid",
            ROOT,
            engine_dir,
            fixture_path,
            target,
            "outside-evidence-ready",
        )
    finally:
        _restore(engine_dir, original)

    assert _snapshot(engine_dir) == original
    _assert_success(_run_verifier(ROOT, SCRIPT, engine_dir), engine_mode=True)


def test_nonexistent_engine_directory_failure_is_exact_and_sanitized(tmp_path: Path):
    missing = tmp_path / "missing-engine-v2"
    result = _run_verifier(ROOT, SCRIPT, missing)

    _assert_failure(result, "engine_directory_invalid", ROOT, missing)


def test_invalid_cli_failure_is_exact_and_sanitized():
    argument = "client-payload-fragment"
    result = _run_verifier(ROOT, SCRIPT, arguments=[argument])

    _assert_failure(result, "cli_invalid", ROOT, argument)


@pytest.mark.parametrize(
    ("platform", "winerror"),
    [
        ("win32", 5),
        ("win32", None),
        ("linux", 1314),
        ("linux", None),
    ],
)
def test_mocked_symlink_fallback_reraises_every_other_os_error(
    monkeypatch, platform: str, winerror: int | None
):
    monkeypatch.setattr(sys, "platform", platform)
    error = _SymlinkOSError(winerror)

    with pytest.raises(OSError) as caught:
        _allow_mocked_symlink_fallback(error)

    assert caught.value is error


def test_mocked_symlink_fallback_allows_windows_privilege_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    _allow_mocked_symlink_fallback(_SymlinkOSError(1314))
