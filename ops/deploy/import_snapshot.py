import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROTECTED_NAMES = {
    ".env",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
}
_PROTECTED_SUFFIXES = (".key", ".p12", ".pem", ".pfx")
_CREDENTIAL_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b'"private_key":',
    b'"refresh_token":',
    b'"client_secret":',
)


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _safe_relative(value: object, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("source_path_unsafe")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or (path.parts and ":" in path.parts[0])
        or any(part in {"", ".."} for part in path.parts)
    ):
        raise ValueError("source_path_unsafe")
    normalized = path.as_posix()
    if normalized == "." and not allow_dot:
        raise ValueError("source_path_unsafe")
    if value != normalized:
        raise ValueError("source_path_unsafe")
    return normalized


def _assert_unlinked(path: Path, root: Path) -> None:
    current = path
    while True:
        if _linked(current):
            raise ValueError("source_link")
        if current == root:
            break
        if root not in current.parents:
            raise ValueError("source_path_unsafe")
        current = current.parent


def _assert_root_chain_unlinked(root: Path) -> None:
    current = root
    while True:
        if _linked(current):
            raise ValueError("source_link")
        if current.parent == current:
            break
        current = current.parent


def _case_key(source: str, path: str) -> str:
    return f"{source}/{path}".casefold()


def verify_frozen_files(manifest: dict, source_roots: dict[str, Path]) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("source_manifest_invalid")
    included_roots = manifest.get("included_roots")
    files = manifest.get("files")
    exclusions = manifest.get("exclusions", [])
    if not isinstance(included_roots, dict) or not isinstance(files, list):
        raise ValueError("source_manifest_invalid")
    if not isinstance(exclusions, list):
        raise ValueError("source_manifest_invalid")

    roots = {}
    for source, raw_root in source_roots.items():
        if not isinstance(source, str) or not source:
            raise ValueError("source_manifest_invalid")
        root = Path(raw_root).absolute()
        if not root.is_dir():
            raise ValueError("source_root_missing")
        _assert_root_chain_unlinked(root)
        roots[source] = root

    listed = {}
    excluded = {}
    cases = {}
    for kind, entries in (("file", files), ("exclusion", exclusions)):
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("source_manifest_invalid")
            source = entry.get("source")
            if source not in roots:
                raise ValueError("source_unknown")
            path = _safe_relative(entry.get("path"))
            key = (source, path)
            case_key = _case_key(source, path)
            if case_key in cases and cases[case_key] != key:
                raise ValueError("source_case_collision")
            if key in listed or key in excluded:
                raise ValueError("source_duplicate")
            cases[case_key] = key
            if kind == "file":
                digest = entry.get("sha256")
                if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                    raise ValueError("source_manifest_invalid")
                listed[key] = digest
            else:
                reason = entry.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("source_exclusion_invalid")
                excluded[key] = reason

    normalized_included = {}
    for source, paths in included_roots.items():
        if source not in roots or not isinstance(paths, list) or not paths:
            raise ValueError("source_manifest_invalid")
        normalized_included[source] = [
            _safe_relative(path, allow_dot=True) for path in paths
        ]

    top_level_inventory = manifest.get("top_level_inventory", {})
    if not isinstance(top_level_inventory, dict):
        raise ValueError("source_manifest_invalid")
    for source, paths in normalized_included.items():
        if "." in paths:
            continue
        inventory = top_level_inventory.get(source)
        if not isinstance(inventory, dict):
            raise ValueError(f"source_top_level_inventory_missing:{source}")
        boundary = _safe_relative(inventory.get("path"), allow_dot=True)
        if boundary != "." and any(
            include != boundary and not include.startswith(f"{boundary}/")
            for include in paths
        ):
            raise ValueError(f"source_top_level_inventory_invalid:{source}")
    for source, inventory in top_level_inventory.items():
        if source not in roots or not isinstance(inventory, dict):
            raise ValueError("source_manifest_invalid")
        boundary = _safe_relative(inventory.get("path"), allow_dot=True)
        entries = inventory.get("entries")
        if not isinstance(entries, list):
            raise ValueError("source_manifest_invalid")
        root = roots[source]
        base = (
            root if boundary == "." else root.joinpath(*PurePosixPath(boundary).parts)
        )
        _assert_unlinked(base, root)
        if not base.is_dir():
            raise ValueError("source_included_root_invalid")
        expected_entries = {}
        inventory_cases = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("source_manifest_invalid")
            name = _safe_relative(entry.get("name"))
            if "/" in name:
                raise ValueError("source_path_unsafe")
            case_name = name.casefold()
            if case_name in inventory_cases and inventory_cases[case_name] != name:
                raise ValueError("source_case_collision")
            if name in expected_entries:
                raise ValueError("source_duplicate")
            inventory_cases[case_name] = name
            expected_type = entry.get("type")
            disposition = entry.get("disposition")
            if expected_type not in {"file", "directory"} or disposition not in {
                "included",
                "excluded",
            }:
                raise ValueError("source_manifest_invalid")
            relative = name if boundary == "." else f"{boundary}/{name}"
            if disposition == "excluded":
                reason = entry.get("reason")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("source_exclusion_invalid")
            elif expected_type == "file":
                if (source, relative) not in listed:
                    raise ValueError("source_manifest_invalid")
            elif not any(
                include == relative
                or relative.startswith(f"{include}/")
                or include.startswith(f"{relative}/")
                for include in normalized_included.get(source, [])
            ):
                raise ValueError("source_manifest_invalid")
            expected_entries[name] = expected_type

        actual_entries = {}
        actual_cases = {}
        for path in base.iterdir():
            _assert_unlinked(path, root)
            case_name = path.name.casefold()
            if case_name in actual_cases and actual_cases[case_name] != path.name:
                raise ValueError("source_case_collision")
            actual_cases[case_name] = path.name
            if path.is_file():
                actual_entries[path.name] = "file"
            elif path.is_dir():
                actual_entries[path.name] = "directory"
            else:
                raise ValueError(f"source_type_changed:{source}:{path.name}")
        new_entries = set(actual_entries) - set(expected_entries)
        if new_entries:
            raise ValueError(f"source_unlisted:{source}:{sorted(new_entries)[0]}")
        missing_entries = set(expected_entries) - set(actual_entries)
        if missing_entries:
            raise ValueError(f"source_missing:{source}:{sorted(missing_entries)[0]}")
        for name, expected_type in expected_entries.items():
            if actual_entries[name] != expected_type:
                raise ValueError(f"source_type_changed:{source}:{name}")

    observed = set()
    for source, paths in normalized_included.items():
        root = roots[source]
        for include in paths:
            base = (
                root if include == "." else root.joinpath(*PurePosixPath(include).parts)
            )
            _assert_unlinked(base, root)
            if not base.is_dir():
                raise ValueError("source_included_root_invalid")
            for directory, directory_names, file_names in os.walk(
                base, followlinks=False
            ):
                directory_path = Path(directory)
                _assert_unlinked(directory_path, root)
                for name in directory_names:
                    _assert_unlinked(directory_path / name, root)
                for name in file_names:
                    path = directory_path / name
                    _assert_unlinked(path, root)
                    relative = path.relative_to(root).as_posix()
                    key = (source, relative)
                    if key not in listed and key not in excluded:
                        raise ValueError(f"source_unlisted:{source}:{relative}")
                    observed.add(key)

    for (source, relative), expected in listed.items():
        path = roots[source].joinpath(*PurePosixPath(relative).parts)
        _assert_unlinked(path, roots[source])
        if not path.exists():
            raise ValueError(f"source_missing:{source}:{relative}")
        if not path.is_file():
            raise ValueError(f"source_type_changed:{source}:{relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"source_changed:{source}:{relative}")
    missing_exclusions = set(excluded) - observed
    if missing_exclusions:
        source, relative = sorted(missing_exclusions)[0]
        raise ValueError(f"source_exclusion_missing:{source}:{relative}")


def _run_git(
    root: Path,
    git_executable: str,
    operation: str,
    *args: str,
) -> tuple[bytes, bytes]:
    try:
        result = subprocess.run(
            [git_executable, "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"git_command_failed:{operation}") from error
    try:
        result.stdout.decode("utf-8", errors="strict")
        result.stderr.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"git_output_not_utf8:{operation}") from error
    if result.returncode:
        raise ValueError(f"git_command_failed:{operation}:exit_{result.returncode}")
    return result.stdout, result.stderr


def _read_git_blobs(
    root: Path, git_executable: str, blob_ids: list[str]
) -> dict[str, bytes]:
    try:
        result = subprocess.run(
            [git_executable, "-C", str(root), "cat-file", "--batch"],
            input=("\n".join(blob_ids) + "\n").encode("ascii"),
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("git_command_failed:blobs") from error
    if result.returncode:
        raise ValueError(f"git_command_failed:blobs:exit_{result.returncode}")
    blobs = {}
    position = 0
    for expected in blob_ids:
        header_end = result.stdout.find(b"\n", position)
        if header_end < 0:
            raise ValueError("source_git_invalid")
        header = result.stdout[position:header_end].decode("ascii").split()
        if len(header) != 3 or header[0] != expected or header[1] != "blob":
            raise ValueError("source_git_invalid")
        size = int(header[2])
        start = header_end + 1
        end = start + size
        if end >= len(result.stdout) or result.stdout[end : end + 1] != b"\n":
            raise ValueError("source_git_invalid")
        blobs[expected] = result.stdout[start:end]
        position = end + 1
    if position != len(result.stdout):
        raise ValueError("source_git_invalid")
    return blobs


def _protected(path: str, raw: bytes) -> str | None:
    name = PurePosixPath(path).name.casefold()
    if name in _PROTECTED_NAMES or name.startswith(".env."):
        return "protected_credential_filename"
    if name.endswith(_PROTECTED_SUFFIXES):
        return "protected_credential_suffix"
    if any(marker in raw for marker in _CREDENTIAL_MARKERS):
        return "protected_credential_content"
    return None


def _line_endings(raw: bytes) -> dict[str, int]:
    crlf = raw.count(b"\r\n")
    return {"crlf": crlf, "lf": raw.count(b"\n") - crlf, "cr": raw.count(b"\r") - crlf}


def capture_git_census(
    name: str,
    root: Path,
    evidence_dir: Path,
    *,
    git_executable: str,
) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise ValueError("source_name_invalid")
    root = Path(root).absolute()
    if not root.is_dir() or _linked(root):
        raise ValueError("source_root_invalid")
    destination = Path(evidence_dir).absolute() / "census" / name
    destination.mkdir(parents=True, exist_ok=True)
    commands = {
        "top_level": ("rev-parse", "--show-toplevel"),
        "head": ("rev-parse", "HEAD"),
        "branch": ("symbolic-ref", "--quiet", "--short", "HEAD"),
        "remote": ("remote", "get-url", "origin"),
        "status_porcelain_v2": ("status", "--porcelain=v2", "--untracked-files=all"),
        "binary_diff_head": ("diff", "--binary", "HEAD"),
        "untracked_names": ("ls-files", "--others", "--exclude-standard", "-z"),
        "worktree_inventory": ("worktree", "list", "--porcelain"),
        "tracked_stage": ("ls-files", "--stage", "-z"),
    }
    raw_results = {}
    for operation, args in commands.items():
        stdout, stderr = _run_git(root, git_executable, operation, *args)
        if operation == "remote":
            parsed = urlsplit(stdout.decode("utf-8").strip())
            if parsed.username or parsed.password:
                raise ValueError("source_remote_contains_credentials")
        if operation == "binary_diff_head" and _protected(operation, stdout):
            raise ValueError("source_diff_contains_credentials")
        (destination / f"{operation}.stdout.bin").write_bytes(stdout)
        (destination / f"{operation}.stderr.bin").write_bytes(stderr)
        raw_results[operation] = {
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "argv": list(args),
        }

    top_level = (
        (destination / "top_level.stdout.bin").read_bytes().decode("utf-8").strip()
    )
    head = (destination / "head.stdout.bin").read_bytes().decode("utf-8").strip()
    branch = (destination / "branch.stdout.bin").read_bytes().decode("utf-8").strip()
    remote = (destination / "remote.stdout.bin").read_bytes().decode("utf-8").strip()
    if Path(top_level).resolve() != root.resolve() or not _HEAD_RE.fullmatch(head):
        raise ValueError("source_git_invalid")

    tracked = []
    stage_raw = (destination / "tracked_stage.stdout.bin").read_bytes()
    staged = []
    for item in filter(None, stage_raw.split(b"\0")):
        metadata, raw_path = item.split(b"\t", 1)
        mode, blob, stage = metadata.decode("ascii").split()
        path = _safe_relative(raw_path.decode("utf-8", errors="strict"))
        if stage != "0":
            raise ValueError("source_git_unmerged")
        staged.append((mode, blob, path))
    blob_bytes = _read_git_blobs(
        root, git_executable, list(dict.fromkeys(x[1] for x in staged))
    )
    for mode, blob, path in staged:
        blob_raw = blob_bytes[blob]
        working = root.joinpath(*PurePosixPath(path).parts)
        if working.exists() and working.is_file() and not _linked(working):
            working_raw = working.read_bytes()
            working_sha256 = hashlib.sha256(working_raw).hexdigest()
            working_type = "file"
            line_endings = {
                "git_blob": _line_endings(blob_raw),
                "working": _line_endings(working_raw),
                "bytes_equal": blob_raw == working_raw,
            }
        else:
            working_sha256 = None
            working_type = "missing_or_nonregular"
            line_endings = None
        tracked.append(
            {
                "path": path,
                "mode": mode,
                "git_blob_id": blob,
                "git_blob_sha256": hashlib.sha256(blob_raw).hexdigest(),
                "working_sha256": working_sha256,
                "working_type": working_type,
                "line_endings": line_endings,
            }
        )

    untracked = []
    untracked_raw = (destination / "untracked_names.stdout.bin").read_bytes()
    for item in filter(None, untracked_raw.split(b"\0")):
        path = _safe_relative(item.decode("utf-8", errors="strict"))
        source_path = root.joinpath(*PurePosixPath(path).parts)
        _assert_unlinked(source_path, root)
        if not source_path.is_file():
            raise ValueError(f"source_untracked_type_invalid:{path}")
        raw = source_path.read_bytes()
        reason = _protected(path, raw)
        record = {"path": path, "sha256": hashlib.sha256(raw).hexdigest()}
        if reason:
            record.update({"saved": False, "exclusion_reason": reason})
        else:
            archived = destination / "untracked" / Path(*PurePosixPath(path).parts)
            archived.parent.mkdir(parents=True, exist_ok=True)
            archived.write_bytes(raw)
            record.update(
                {
                    "saved": True,
                    "archive_path": archived.relative_to(evidence_dir).as_posix(),
                }
            )
        untracked.append(record)

    final_head, _ = _run_git(root, git_executable, "final_head", "rev-parse", "HEAD")
    final_status, _ = _run_git(
        root,
        git_executable,
        "final_status",
        "status",
        "--porcelain=v2",
        "--untracked-files=all",
    )
    initial_status = (destination / "status_porcelain_v2.stdout.bin").read_bytes()
    if final_head.decode("utf-8").strip() != head or final_status != initial_status:
        raise ValueError("source_changed")
    record = {
        "name": name,
        "root": str(root),
        "head": head,
        "branch": branch,
        "remote": remote,
        "raw_results": raw_results,
        "tracked": tracked,
        "untracked": untracked,
    }
    (destination / "census.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )
    return record


_PACKAGING_MODES = {"100644", "100755"}
_PACKAGING_MANIFEST_KEYS = {
    "contract_version",
    "source_freeze_sha256",
    "source_metadata",
    "files",
    "baseline",
    "packaging_edits",
    "exclusions",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("packaging_manifest_invalid")
        result[key] = value
    return result


def _packaging_path(value: object) -> str:
    try:
        return _safe_relative(value)
    except ValueError as error:
        raise ValueError("packaging_manifest_invalid") from error


def _packaging_git(root: Path) -> dict[str, dict[str, str]]:
    executable = shutil.which("git")
    if executable is None:
        fallback = Path.home() / "AppData/Local/Programs/Git/cmd/git.exe"
        executable = str(fallback) if fallback.is_file() else None
    if executable is None:
        raise ValueError("git_unavailable")
    top, _ = _run_git(root, executable, "packaging_top", "rev-parse", "--show-toplevel")
    if Path(top.decode("utf-8").strip()).resolve() != root.resolve():
        raise ValueError("target_repository_invalid")
    raw, _ = _run_git(root, executable, "packaging_index", "ls-files", "--stage", "-z")
    staged = []
    for item in filter(None, raw.split(b"\0")):
        metadata, raw_path = item.split(b"\t", 1)
        mode_parts = metadata.decode("ascii").split()
        mode, blob, stage = mode_parts
        path = _packaging_path(raw_path.decode("utf-8", errors="strict"))
        folded = path.casefold()
        if stage != "0" or any(item[0] == folded for item in staged):
            raise ValueError("target_repository_invalid")
        staged.append((folded, mode, blob))
    blobs = _read_git_blobs(
        root, executable, list(dict.fromkeys(item[2] for item in staged))
    )
    return {
        folded: {
            "mode": mode,
            "blob_sha256": hashlib.sha256(blobs[blob]).hexdigest(),
        }
        for folded, mode, blob in staged
    }


def _text_lf_sha256(raw: bytes) -> str | None:
    if b"\0" in raw:
        return None
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if b"\r" in raw.replace(b"\r\n", b""):
        return None
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _safe_packaging_output(root: Path, freeze: Path, output: Path) -> bool:
    root = root.absolute()
    freeze = freeze.absolute()
    output = output.absolute()
    if (
        os.path.normcase(os.path.abspath(output))
        == os.path.normcase(os.path.abspath(freeze))
        or output == root
        or output.is_relative_to(root)
        or output.is_symlink()
        or not output.parent.is_dir()
    ):
        return False
    if output.exists():
        if freeze.exists():
            try:
                if os.path.samefile(freeze, output):
                    return False
            except OSError:
                return False
        return False
    current = output
    while True:
        if _linked(current) or (current == output and current.is_dir()):
            return False
        if current.parent == current:
            return True
        current = current.parent


def _write_unsafe_output_refusal() -> None:
    raw = (
        _canonical(
            {"code": "output_path_invalid", "failure_count": 1, "result": "refused"}
        )
        + b"\n"
    )
    stream = getattr(sys.stderr, "buffer", sys.stderr)
    stream.write(raw if hasattr(sys.stderr, "buffer") else raw.decode("utf-8"))
    stream.flush()


def _packaging_failure(path: str, code: str) -> dict[str, str]:
    return {"path": path, "code": code}


def _packaging_receipt(
    manifest: dict,
    manifest_sha256: str,
    root: Path,
) -> dict:
    failures = []
    records = []
    if type(manifest) is not dict or set(manifest) != _PACKAGING_MANIFEST_KEYS:
        raise ValueError("packaging_manifest_invalid")
    if (
        manifest.get("contract_version") != "r02_packaging_verification_manifest_v1"
        or not isinstance(manifest.get("source_freeze_sha256"), str)
        or _SHA256_RE.fullmatch(manifest["source_freeze_sha256"]) is None
        or type(manifest.get("source_metadata")) is not dict
        or any(
            type(manifest.get(name)) is not list
            for name in ("files", "baseline", "packaging_edits", "exclusions")
        )
        or len(manifest["files"]) > 10000
        or len(manifest["packaging_edits"]) > 1000
        or len(manifest["exclusions"]) > 10000
    ):
        raise ValueError("packaging_manifest_invalid")

    source_files = {}
    cases = {}
    required_source_keys = {
        "source",
        "path",
        "destination",
        "sha256",
        "git_blob_sha256",
        "git_blob_id",
        "disposition",
        "expected_git_mode",
    }
    for entry in manifest["files"]:
        if type(entry) is not dict or set(entry) != required_source_keys:
            raise ValueError("packaging_manifest_invalid")
        destination = _packaging_path(entry.get("destination"))
        if (
            not isinstance(entry.get("source"), str)
            or not entry["source"]
            or _packaging_path(entry.get("path")) != entry["path"]
            or entry.get("disposition") != "selected_import"
            or not isinstance(entry.get("git_blob_id"), str)
            or _HEAD_RE.fullmatch(entry["git_blob_id"]) is None
            or any(
                not isinstance(entry.get(name), str)
                or _SHA256_RE.fullmatch(entry[name]) is None
                for name in ("sha256", "git_blob_sha256")
            )
            or entry.get("expected_git_mode") not in _PACKAGING_MODES
        ):
            raise ValueError("packaging_manifest_invalid")
        folded = destination.casefold()
        if folded in cases:
            raise ValueError("packaging_manifest_invalid")
        cases[folded] = destination
        source_files[destination] = entry

    baseline = {}
    for entry in manifest["baseline"]:
        if type(entry) is not dict or set(entry) != {
            "path",
            "sha256",
            "git_blob_sha256",
            "expected_git_mode",
            "reason",
        }:
            raise ValueError("packaging_manifest_invalid")
        path = _packaging_path(entry.get("path"))
        if (
            not isinstance(entry.get("sha256"), str)
            or _SHA256_RE.fullmatch(entry["sha256"]) is None
            or not isinstance(entry.get("git_blob_sha256"), str)
            or _SHA256_RE.fullmatch(entry["git_blob_sha256"]) is None
            or entry.get("expected_git_mode") not in _PACKAGING_MODES
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"].strip()
            or path.casefold() in cases
        ):
            raise ValueError("packaging_manifest_invalid")
        cases[path.casefold()] = path
        baseline[path] = entry

    edits = {}
    for entry in manifest["packaging_edits"]:
        if type(entry) is not dict or set(entry) != {
            "destination",
            "action",
            "before_sha256",
            "after_sha256",
            "after_git_blob_sha256",
            "expected_git_mode",
            "reason",
            "evidence_reference",
        }:
            raise ValueError("packaging_manifest_invalid")
        path = _packaging_path(entry.get("destination"))
        action = entry.get("action")
        folded = path.casefold()
        if folded in {name.casefold() for name in edits}:
            failures.append(_packaging_failure(path, "packaging_edit_collision"))
            continue
        if (
            action not in {"added", "modified", "omitted"}
            or entry.get("expected_git_mode") not in _PACKAGING_MODES
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"].strip()
            or not isinstance(entry.get("evidence_reference"), str)
            or not entry["evidence_reference"].strip()
        ):
            failures.append(
                _packaging_failure(
                    path,
                    "git_mode_invalid"
                    if entry.get("expected_git_mode") not in _PACKAGING_MODES
                    else "packaging_edit_invalid",
                )
            )
            continue
        before = entry.get("before_sha256")
        after = entry.get("after_sha256")
        after_blob = entry.get("after_git_blob_sha256")
        original = source_files.get(path)
        baseline_original = baseline.get(path)
        if action == "added":
            if (
                folded in cases
                or original is not None
                or path in baseline
                or before is not None
            ):
                failures.append(_packaging_failure(path, "packaging_edit_collision"))
            if not isinstance(after, str) or _SHA256_RE.fullmatch(after) is None:
                failures.append(_packaging_failure(path, "packaging_edit_invalid"))
            if (
                not isinstance(after_blob, str)
                or _SHA256_RE.fullmatch(after_blob) is None
            ):
                failures.append(_packaging_failure(path, "packaging_edit_invalid"))
        else:
            expected_before = (
                original["sha256"]
                if original is not None
                else baseline_original["sha256"]
                if baseline_original is not None and action == "modified"
                else None
            )
            if expected_before is None or before != expected_before:
                failures.append(_packaging_failure(path, "packaging_before_mismatch"))
            if baseline_original is not None and action != "modified":
                failures.append(_packaging_failure(path, "packaging_edit_invalid"))
            if action == "modified" and (
                not isinstance(after, str) or _SHA256_RE.fullmatch(after) is None
            ):
                failures.append(_packaging_failure(path, "packaging_edit_invalid"))
            if action == "modified" and (
                not isinstance(after_blob, str)
                or _SHA256_RE.fullmatch(after_blob) is None
            ):
                failures.append(_packaging_failure(path, "packaging_edit_invalid"))
            if action == "omitted" and (after is not None or after_blob is not None):
                failures.append(_packaging_failure(path, "packaging_edit_invalid"))
        edits[path] = entry

    exclusions = {}
    for entry in manifest["exclusions"]:
        if type(entry) is not dict or set(entry) != {"path", "type", "reason"}:
            raise ValueError("packaging_manifest_invalid")
        path = _packaging_path(entry.get("path"))
        if (
            entry.get("type") not in {"file", "directory"}
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"].strip()
            or path.casefold() in cases
            or path.casefold() in {name.casefold() for name in exclusions}
        ):
            raise ValueError("packaging_manifest_invalid")
        exclusions[path] = entry

    root = Path(root).absolute()
    if not root.is_dir():
        raise ValueError("target_repository_invalid")
    _assert_root_chain_unlinked(root)
    modes = _packaging_git(root)
    for path, entry in exclusions.items():
        target = root.joinpath(*PurePosixPath(path).parts)
        try:
            _assert_unlinked(target, root)
        except ValueError:
            failures.append(_packaging_failure(path, "destination_link"))
            continue
        if not target.exists():
            continue
        actual_type = (
            "file" if target.is_file() else "directory" if target.is_dir() else "other"
        )
        if actual_type != entry["type"]:
            failures.append(_packaging_failure(path, "exclusion_type_mismatch"))
    excluded_directories = {
        path for path, entry in exclusions.items() if entry["type"] == "directory"
    }
    observed_files = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(directory)
        relative_directory = current.relative_to(root).as_posix()
        if relative_directory == ".":
            relative_directory = ""
        for name in list(directory_names):
            path = current / name
            relative = f"{relative_directory}/{name}".lstrip("/")
            if _linked(path):
                failures.append(_packaging_failure(relative, "destination_link"))
                directory_names.remove(name)
            elif relative in excluded_directories:
                if exclusions[relative]["type"] != "directory":
                    failures.append(
                        _packaging_failure(relative, "exclusion_type_mismatch")
                    )
                directory_names.remove(name)
        for name in file_names:
            path = current / name
            relative = f"{relative_directory}/{name}".lstrip("/")
            if _linked(path):
                failures.append(_packaging_failure(relative, "destination_link"))
                continue
            if relative in exclusions:
                if exclusions[relative]["type"] != "file":
                    failures.append(
                        _packaging_failure(relative, "exclusion_type_mismatch")
                    )
                continue
            observed_files.add(relative)

    expected_present = set(source_files) | set(baseline)
    expected_present -= {
        path for path, edit in edits.items() if edit["action"] == "omitted"
    }
    expected_present |= {
        path for path, edit in edits.items() if edit["action"] == "added"
    }
    for path in sorted(observed_files - expected_present):
        failures.append(_packaging_failure(path, "destination_undeclared"))

    def record_file(
        path: str,
        disposition: str,
        expected_sha256: str | None,
        expected_blob_sha256: str | None,
        expected_mode: str,
        *,
        omitted: bool = False,
        baseline_entry: dict | None = None,
        evidence_reference: str | None = None,
    ) -> None:
        target = root.joinpath(*PurePosixPath(path).parts)
        folded = path.casefold()
        observed_index = modes.get(folded)
        observed_mode = observed_index["mode"] if observed_index else None
        observed_blob = observed_index["blob_sha256"] if observed_index else None
        if omitted:
            if target.exists() or target.is_symlink():
                failures.append(_packaging_failure(path, "omission_resurrected"))
            if observed_mode is not None:
                failures.append(_packaging_failure(path, "omission_indexed"))
            records.append(
                {
                    "path": path,
                    "disposition": disposition,
                    "sha256": None,
                    "expected_git_mode": expected_mode,
                    "observed_git_mode": None,
                    "expected_git_blob_sha256": None,
                    "observed_git_blob_sha256": observed_blob,
                    "mode_result": "not_present_as_declared",
                }
            )
            return
        if not target.exists():
            failures.append(_packaging_failure(path, "destination_missing"))
            actual = None
        elif _linked(target) or not target.is_file():
            failures.append(
                _packaging_failure(
                    path,
                    "destination_link"
                    if _linked(target)
                    else "destination_type_changed",
                )
            )
            actual = None
        else:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected_sha256:
                failures.append(_packaging_failure(path, "destination_changed"))
        if observed_mode is None:
            failures.append(_packaging_failure(path, "git_mode_unproven"))
            mode_result = "unproven"
        elif observed_mode != expected_mode:
            failures.append(_packaging_failure(path, "git_mode_mismatch"))
            mode_result = "mismatch"
        else:
            mode_result = "verified"
        if observed_blob is not None and observed_blob != expected_blob_sha256:
            failures.append(_packaging_failure(path, "git_blob_mismatch"))
        record = {
            "path": path,
            "disposition": disposition,
            "sha256": actual,
            "expected_git_mode": expected_mode,
            "observed_git_mode": observed_mode,
            "expected_git_blob_sha256": expected_blob_sha256,
            "observed_git_blob_sha256": observed_blob,
            "mode_result": mode_result,
        }
        if baseline_entry is not None:
            record.update(
                baseline_before_sha256=baseline_entry["sha256"],
                baseline_git_blob_sha256=baseline_entry["git_blob_sha256"],
                baseline_reason=baseline_entry["reason"],
                evidence_reference=evidence_reference,
            )
        records.append(record)

    for path, entry in source_files.items():
        edit = edits.get(path)
        if edit and edit["action"] == "modified":
            record_file(
                path,
                "packaging_modified",
                edit["after_sha256"],
                edit["after_git_blob_sha256"],
                edit["expected_git_mode"],
            )
        elif edit and edit["action"] == "omitted":
            record_file(
                path,
                "packaging_omitted",
                None,
                None,
                edit["expected_git_mode"],
                omitted=True,
            )
        else:
            target = root.joinpath(*PurePosixPath(path).parts)
            if target.is_file() and not _linked(target):
                raw = target.read_bytes()
                if hashlib.sha256(raw).hexdigest() == entry["sha256"]:
                    disposition = "source_working_bytes"
                    expected = entry["sha256"]
                elif (
                    hashlib.sha256(raw).hexdigest() == entry["git_blob_sha256"]
                    or _text_lf_sha256(raw) == entry["git_blob_sha256"]
                ):
                    disposition = "source_git_blob"
                    expected = hashlib.sha256(raw).hexdigest()
                else:
                    disposition = "source_working_bytes"
                    expected = entry["sha256"]
            else:
                disposition = "source_working_bytes"
                expected = entry["sha256"]
            record_file(
                path,
                disposition,
                expected,
                entry["git_blob_sha256"],
                entry["expected_git_mode"],
            )
    for path, entry in baseline.items():
        edit = edits.get(path)
        if edit and edit["action"] == "modified":
            record_file(
                path,
                "baseline_packaging_modified",
                edit["after_sha256"],
                edit["after_git_blob_sha256"],
                edit["expected_git_mode"],
                baseline_entry=entry,
                evidence_reference=edit["evidence_reference"],
            )
        else:
            record_file(
                path,
                "baseline",
                entry["sha256"],
                entry["git_blob_sha256"],
                entry["expected_git_mode"],
            )
    for path, edit in edits.items():
        if (
            edit["action"] == "added"
            and path not in source_files
            and path not in baseline
        ):
            record_file(
                path,
                "packaging_added",
                edit["after_sha256"],
                edit["after_git_blob_sha256"],
                edit["expected_git_mode"],
            )

    failures = sorted(
        {(item["path"], item["code"]) for item in failures},
        key=lambda item: (item[0].casefold(), item[1]),
    )
    counts = {}
    for record in records:
        counts[record["disposition"]] = counts.get(record["disposition"], 0) + 1
    return {
        "contract_version": "r02_packaging_verification_receipt_v1",
        "manifest_sha256": manifest_sha256,
        "source_freeze_sha256": manifest["source_freeze_sha256"],
        "result": "refused" if failures else "pass",
        "counts": dict(sorted(counts.items())),
        "files": sorted(records, key=lambda item: item["path"].casefold()),
        "failures": [{"path": path, "code": code} for path, code in failures],
    }


def _refused_packaging_receipt(manifest_sha256: str, code: str) -> dict:
    return {
        "contract_version": "r02_packaging_verification_receipt_v1",
        "manifest_sha256": manifest_sha256,
        "source_freeze_sha256": None,
        "result": "refused",
        "counts": {},
        "files": [],
        "failures": [{"path": "", "code": code}],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--freeze", required=True)
    verify.add_argument("--root", required=True)
    verify.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    freeze_path = Path(args.freeze)
    output_path = Path(args.output)
    root = Path(args.root).absolute()
    if not _safe_packaging_output(root, freeze_path, output_path):
        _write_unsafe_output_refusal()
        return 1
    try:
        raw = freeze_path.read_bytes()
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError("packaging_manifest_invalid")
        manifest_sha256 = hashlib.sha256(raw).hexdigest()
        manifest = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("packaging_manifest_invalid")
            ),
        )
        receipt = _packaging_receipt(manifest, manifest_sha256, root)
        code = 0 if receipt["result"] == "pass" else 1
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as error:
        manifest_sha256 = locals().get(
            "manifest_sha256", hashlib.sha256(b"").hexdigest()
        )
        message = str(error).split(":", 1)[0]
        allowed = {
            "git_unavailable",
            "output_path_invalid",
            "packaging_manifest_invalid",
            "target_repository_invalid",
        }
        receipt = _refused_packaging_receipt(
            manifest_sha256,
            message if message in allowed else "packaging_verification_failed",
        )
        code = 1
    if not _safe_packaging_output(root, freeze_path, output_path):
        _write_unsafe_output_refusal()
        return 1
    try:
        with output_path.open("xb") as stream:
            stream.write(_canonical(receipt))
    except (FileExistsError, IsADirectoryError, NotADirectoryError):
        _write_unsafe_output_refusal()
        return 1
    print(
        _canonical(
            {"result": receipt["result"], "failure_count": len(receipt["failures"])}
        ).decode("utf-8")
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
