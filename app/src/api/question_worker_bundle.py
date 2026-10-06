import hashlib
import json
import re
import stat
from pathlib import Path


class BundleVerificationError(ValueError):
    pass


def _json_file(path, limit):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def nonfinite(_value):
        raise ValueError("nonfinite")

    if not stat.S_ISREG(path.lstat().st_mode):
        raise BundleVerificationError("bundle_file_invalid")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise BundleVerificationError("bundle_metadata_too_large")
    return json.loads(
        data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite
    )


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _path(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise BundleVerificationError("bundle_path_invalid")
    parts = name.split("/")
    if any(
        not part
        or part in {".", ".."}
        or part.endswith((".", " "))
        or any(ord(char) < 32 for char in part)
        for part in parts
    ):
        raise BundleVerificationError("bundle_path_invalid")
    for part in parts:
        lower = part.casefold()
        if (
            lower.startswith(".env")
            or lower
            in {
                ".git",
                ".git-credentials",
                ".netrc",
                "_netrc",
                ".ssh",
                "__pycache__",
                "credentials.json",
                "application_default_credentials.json",
                "service-account.json",
                "service_account.json",
            }
            or lower.endswith((".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx"))
        ):
            raise BundleVerificationError("bundle_prohibited_file")
    return parts


def verify_bundle(
    bundle_root, stamp_path, *, expected_lp_commit, expected_bundle_digest
):
    if (
        not isinstance(expected_lp_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", expected_lp_commit) is None
        or not _digest(expected_bundle_digest)
    ):
        raise BundleVerificationError("bundle_binding_invalid")
    root, stamp_path = Path(bundle_root), Path(stamp_path)
    if not root.is_absolute() or not stamp_path.is_absolute():
        raise BundleVerificationError("bundle_path_invalid")
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise BundleVerificationError("bundle_root_invalid")
        stamp = _json_file(stamp_path, 4096)
        if not isinstance(stamp, dict) or stamp != {
            "contract_version": "general_question_runtime_build_v1",
            "lp_commit": expected_lp_commit,
            "engine_bundle_digest": expected_bundle_digest,
        }:
            raise BundleVerificationError("bundle_identity_mismatch")
        manifest = _json_file(root / "bundle-manifest.json", 2 * 1024 * 1024)
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"contract_version", "files"}
            or manifest["contract_version"] != "general_question_engine_bundle_v1"
            or not isinstance(manifest["files"], dict)
        ):
            raise BundleVerificationError("bundle_manifest_invalid")
        encoded = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != expected_bundle_digest:
            raise BundleVerificationError("bundle_manifest_mismatch")
        files = manifest["files"]
        if (
            not {"requirements.lock", "scripts/staging/run_general_question_worker.py"}
            <= set(files)
            or "bundle-manifest.json" in files
        ):
            raise BundleVerificationError("bundle_manifest_incomplete")
        names = {"bundle-manifest.json": "bundle-manifest.json"}

        def register_path(name):
            parts = _path(name)
            for index in range(1, len(parts) + 1):
                prefix = "/".join(parts[:index])
                folded = prefix.casefold()
                if folded in names and names[folded] != prefix:
                    raise BundleVerificationError("bundle_case_collision")
                names[folded] = prefix
            if parts[0] not in {"src", "configs", "infra", "scripts"} and name not in {
                "requirements.lock",
                "bundle-manifest.json",
            }:
                raise BundleVerificationError("bundle_runtime_path_unapproved")

        for name, digest in files.items():
            register_path(name)
            if not _digest(digest):
                raise BundleVerificationError("bundle_manifest_invalid")
        actual = set()
        stack = [root]
        while stack:
            directory = stack.pop()
            for path in directory.iterdir():
                name = path.relative_to(root).as_posix()
                register_path(name)
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    stack.append(path)
                elif stat.S_ISREG(mode):
                    if name == "bundle-manifest.json":
                        continue
                    if name not in files:
                        raise BundleVerificationError("bundle_unlisted_file")
                    with path.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != files[name]:
                        raise BundleVerificationError("bundle_file_mismatch")
                    actual.add(name)
                else:
                    raise BundleVerificationError("bundle_file_invalid")
        if actual != set(files):
            raise BundleVerificationError("bundle_missing_file")
    except (OSError, ValueError, RecursionError) as error:
        if isinstance(error, BundleVerificationError):
            raise
        raise BundleVerificationError("bundle_read_failed") from None
    return {
        "lp_commit": expected_lp_commit,
        "engine_bundle_digest": expected_bundle_digest,
    }
