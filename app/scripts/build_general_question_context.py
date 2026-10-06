"""Prepare a hash-reviewed, isolated Docker context without building an image."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.api.question_worker_bundle import _path, verify_bundle  # noqa: E402

_ROOTS = {
    "engine": ("src", "configs", "infra", "scripts"),
    "lp": ("src", "configs", "web/dist"),
}
_FILES = {
    "engine": ("requirements.lock",),
    "lp": ("requirements.txt", "Dockerfile.general-question"),
}
_REQUIRED = {
    "engine": {"requirements.lock", "scripts/staging/run_general_question_worker.py"},
    "lp": {
        "requirements.txt",
        "Dockerfile.general-question",
        "src/api/main.py",
        "web/dist/index.html",
    },
}
_EXCLUDED = {"tests", "__pycache__", "node_modules"}
_SUBTREE = {"engine": "engine", "lp": "app"}
_MONOREPO_LP_FILES = (
    "ops/build/install_pdf_runtime.py",
    "ops/build/pdf_runtime_dependencies.json",
)
_MONOREPO_LP_REQUIRED = {
    "requirements.lock",
    "src/api/pdf_exporter.py",
    "src/api/pdf_renderer_worker.py",
    "configs/pdf_runtime.json",
    *_MONOREPO_LP_FILES,
}
_PDF_BUILD_MANIFEST_SHA256 = (
    "2c07e1ddda21cf937259dc05065910a1156dd1cf0492701985b9e06e817f7e01"
)


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _git_state(root, *, kind):
    executable = shutil.which("git")
    if executable is None:
        candidate = Path.home() / "AppData/Local/Programs/Git/cmd/git.exe"
        executable = str(candidate) if candidate.is_file() else None
    if executable is None:
        raise ValueError("git_unavailable")

    source = Path(root)
    if ".." in source.parts:
        raise ValueError("source_root_invalid")
    source = source.absolute()
    current = source
    while True:
        if _linked(current):
            raise ValueError("source_link_invalid")
        if current.parent == current:
            break
        current = current.parent

    def run(directory, *args):
        result = subprocess.run(
            [executable, "-C", str(directory), *args], capture_output=True, timeout=15
        )
        if result.returncode:
            raise ValueError("source_git_invalid")
        try:
            result.stdout.decode("utf-8", errors="strict")
            result.stderr.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("source_git_invalid") from error
        return result.stdout

    top = Path(run(source, "rev-parse", "--show-toplevel").decode("utf-8").strip())
    top = top.resolve()
    resolved = source.resolve()
    if resolved != top and (resolved.parent != top or resolved.name != _SUBTREE[kind]):
        raise ValueError("source_root_invalid")
    commit = run(top, "rev-parse", "HEAD").decode("utf-8").strip()
    unmerged = run(top, "ls-files", "--unmerged")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None or unmerged:
        raise ValueError("source_conflict")
    status = run(top, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    diff = run(top, "diff", "--binary", "HEAD")
    return {
        "commit": commit,
        "dirty": bool(status),
        "_repository": str(top),
        "_status_digest": hashlib.sha256(status).hexdigest(),
        "_diff_digest": hashlib.sha256(diff).hexdigest(),
    }


def _excluded(name):
    return any(
        part in _EXCLUDED
        or part.startswith(".")
        or part.lower().endswith((".pyc", ".pyo", ".env", ".md", ".markdown"))
        for part in name.split("/")
    )


def _linked(path):
    return path.is_symlink() or path.is_junction()


def _file_bytes(path, name):
    _path(name)
    if _linked(path) or not path.is_file():
        raise ValueError("source_file_invalid")
    raw = path.read_bytes()
    if (
        b"-----BEGIN PRIVATE KEY-----" in raw
        or b"-----BEGIN RSA PRIVATE KEY-----" in raw
    ):
        raise ValueError("source_credential_invalid")
    if path.suffix.lower() == ".json":
        pending = [json.loads(raw)]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if item.get("type") in (
                    "service_account",
                    "authorized_user",
                    "external_account",
                ):
                    raise ValueError("source_credential_invalid")
                if any(
                    key
                    in {"private_key", "client_secret", "refresh_token", "access_token"}
                    and isinstance(value, str)
                    and value
                    for key, value in item.items()
                ):
                    raise ValueError("source_credential_invalid")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
    return raw


def _monorepo_app(root):
    return root.name == "app" and (root.parent / ".git").exists()


def _unlinked_descendant(base, name):
    base = Path(base).absolute()
    if _linked(base) or not base.is_dir():
        raise ValueError("source_link_invalid")
    path = base
    for part in _path(name):
        path = path / part
        if _linked(path):
            raise ValueError("source_link_invalid")
    try:
        if not path.resolve().is_relative_to(base.resolve()):
            raise ValueError("source_link_invalid")
    except (OSError, RuntimeError) as error:
        raise ValueError("source_link_invalid") from error
    return path


def _context_source_path(root, kind, name):
    if kind == "lp" and name in _MONOREPO_LP_FILES and _monorepo_app(root):
        return _unlinked_descendant(root.parent, name)
    return root / name


def _validate_pdf_build_inputs(root):
    manifest_path = _unlinked_descendant(
        root.parent, "ops/build/pdf_runtime_dependencies.json"
    )
    if not manifest_path.is_file():
        raise ValueError("source_assets_missing")
    raw = _file_bytes(manifest_path, "ops/build/pdf_runtime_dependencies.json")
    _unlinked_descendant(root.parent, "ops/build/pdf_runtime_dependencies.json")
    if hashlib.sha256(raw).hexdigest() != _PDF_BUILD_MANIFEST_SHA256:
        raise ValueError("source_assets_invalid")
    try:
        manifest = json.loads(raw)
        required = manifest["required_inputs"]
    except (KeyError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("source_assets_invalid") from error
    if not isinstance(required, list):
        raise ValueError("source_assets_invalid")
    for item in required:
        if (
            type(item) is not dict
            or set(item) != {"path", "size", "sha256"}
            or not isinstance(item["size"], int)
            or item["size"] <= 0
        ):
            raise ValueError("source_assets_invalid")
        path = _unlinked_descendant(root.parent, item["path"])
        if not path.is_file():
            raise ValueError("source_assets_missing")
        content = _file_bytes(path, item["path"])
        _unlinked_descendant(root.parent, item["path"])
        if (
            len(content) != item["size"]
            or hashlib.sha256(content).hexdigest() != item["sha256"]
        ):
            raise ValueError("source_assets_invalid")


def _inventory_files(root, kind):
    if kind == "lp" and _monorepo_app(root):
        _validate_pdf_build_inputs(root)
        for name in _MONOREPO_LP_REQUIRED:
            path = _context_source_path(root, kind, name)
            if _linked(path) or not path.is_file():
                raise ValueError("source_assets_missing")
    paths = []
    for folder in _ROOTS[kind]:
        base = root / folder
        if not base.is_dir() or _linked(base):
            raise ValueError("source_assets_missing")
        for directory, directories, filenames in os.walk(base, followlinks=False):
            for name in list(directories):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if _excluded(relative):
                    directories.remove(name)
                elif _linked(path):
                    raise ValueError("source_link_invalid")
            paths.extend(Path(directory) / name for name in filenames)
    paths.extend(root / name for name in _FILES[kind])
    named_paths = [(path.relative_to(root).as_posix(), path) for path in paths]
    if kind == "lp" and _monorepo_app(root):
        named_paths.append(("requirements.lock", root / "requirements.lock"))
        named_paths.extend(
            (name, _context_source_path(root, kind, name))
            for name in _MONOREPO_LP_FILES
        )
    files, cases = {}, {}
    for name, path in sorted(named_paths):
        if _excluded(name):
            continue
        _path(name)
        for index in range(1, len(name.split("/")) + 1):
            prefix = "/".join(name.split("/")[:index])
            if cases.get(prefix.casefold(), prefix) != prefix:
                raise ValueError("source_case_collision")
            cases[prefix.casefold()] = prefix
        raw = _file_bytes(path, name)
        _context_source_path(root, kind, name)
        files[name] = hashlib.sha256(raw).hexdigest()
    required = _REQUIRED[kind]
    if kind == "lp" and _monorepo_app(root):
        required = required | _MONOREPO_LP_REQUIRED
    if not required <= files.keys():
        raise ValueError("source_assets_missing")
    return files


def inventory_source(source_root, *, kind):
    if kind not in _ROOTS:
        raise ValueError("source_kind_invalid")
    source = Path(source_root)
    if ".." in source.parts:
        raise ValueError("source_root_invalid")
    if _linked(source) or not source.is_dir():
        raise ValueError("source_root_invalid")
    root = source.resolve()
    state = _git_state(source, kind=kind)
    files = _inventory_files(root, kind)
    if _inventory_files(root, kind) != files or _git_state(source, kind=kind) != state:
        raise ValueError("source_changed")
    return {"commit": state["commit"], "dirty": state["dirty"], "files": files}


def build_context(*, engine_root, lp_root, destination, review_manifest):
    if (
        type(review_manifest) is not dict
        or set(review_manifest)
        != {"contract_version", "allow_reviewed_working_tree", "engine", "lp"}
        or review_manifest["contract_version"] != "general_question_context_review_v1"
        or type(review_manifest["allow_reviewed_working_tree"]) is not bool
    ):
        raise ValueError("review_invalid")
    for kind in ("engine", "lp"):
        reviewed = review_manifest[kind]
        if (
            type(reviewed) is not dict
            or set(reviewed) != {"commit", "dirty", "files"}
            or type(reviewed["dirty"]) is not bool
            or type(reviewed["files"]) is not dict
        ):
            raise ValueError("review_invalid")
    bindings = {
        kind: _git_state(Path(root), kind=kind)
        for kind, root in (("engine", engine_root), ("lp", lp_root))
    }
    if (
        bindings["engine"].get("_repository") == bindings["lp"].get("_repository")
        and bindings["engine"]["commit"] != bindings["lp"]["commit"]
    ):
        raise ValueError("source_changed")
    observed = {
        kind: inventory_source(root, kind=kind)
        for kind, root in (("engine", engine_root), ("lp", lp_root))
    }
    if any(observed[kind] != review_manifest[kind] for kind in observed):
        raise ValueError("unreviewed_source")
    dirty = any(value["dirty"] for value in observed.values())
    if dirty and not review_manifest["allow_reviewed_working_tree"]:
        raise ValueError("unreviewed_source")
    target = Path(destination).resolve()
    if (
        target.exists()
        or target.is_symlink()
        or any(
            target.is_relative_to(Path(root).resolve())
            for root in (engine_root, lp_root)
        )
    ):
        raise ValueError("destination_invalid")
    prepared = []
    for kind, root in (("engine", Path(engine_root)), ("lp", Path(lp_root))):
        for name, digest in observed[kind]["files"].items():
            raw = _file_bytes(_context_source_path(root, kind, name), name)
            _context_source_path(root, kind, name)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("source_changed")
            output = (
                name
                if kind == "lp"
                and (
                    name == "Dockerfile.general-question" or name in _MONOREPO_LP_FILES
                )
                else f"{kind}/{name}"
            )
            prepared.append((output, raw))
    manifest = {
        "contract_version": "general_question_engine_bundle_v1",
        "files": observed["engine"]["files"],
    }
    bundle_digest = hashlib.sha256(canonical(manifest)).hexdigest()
    stamp = {
        "contract_version": "general_question_runtime_build_v1",
        "lp_commit": observed["lp"]["commit"],
        "engine_bundle_digest": bundle_digest,
    }
    prepared.extend(
        (
            ("engine/bundle-manifest.json", canonical(manifest)),
            ("runtime-build.json", canonical(stamp)),
        )
    )
    target.mkdir(parents=True)
    for name, raw in prepared:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    verify_bundle(
        (target / "engine").resolve(),
        (target / "runtime-build.json").resolve(),
        expected_lp_commit=stamp["lp_commit"],
        expected_bundle_digest=bundle_digest,
    )
    sources = (("engine", engine_root), ("lp", lp_root))
    if any(
        _git_state(Path(root), kind=kind) != bindings[kind] for kind, root in sources
    ) or any(
        inventory_source(root, kind=kind) != observed[kind] for kind, root in sources
    ):
        raise ValueError("source_changed")
    return {
        "contract_version": "general_question_context_receipt_v1",
        **observed,
        "engine_bundle_digest": bundle_digest,
        "lp_files_digest": hashlib.sha256(
            canonical(observed["lp"]["files"])
        ).hexdigest(),
        "review_digest": hashlib.sha256(canonical(review_manifest)).hexdigest(),
        "deployment_ready": not dirty,
        "commit_role": "base_revision_with_reviewed_working_bytes"
        if dirty
        else "exact_clean_revision",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-root", required=True)
    parser.add_argument("--lp-root", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--review-manifest", required=True)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    try:
        receipt_path = Path(args.receipt).absolute()
        if receipt_path.exists() or receipt_path.is_relative_to(
            Path(args.destination).absolute()
        ):
            raise ValueError("receipt_destination_invalid")
        review = json.loads(Path(args.review_manifest).read_bytes())
        receipt = build_context(
            engine_root=args.engine_root,
            lp_root=args.lp_root,
            destination=args.destination,
            review_manifest=review,
        )
        receipt_path.write_bytes(canonical(receipt))
        print(
            json.dumps(
                {
                    "engine_bundle_digest": receipt["engine_bundle_digest"],
                    "deployment_ready": receipt["deployment_ready"],
                },
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print("build_context_refused", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
