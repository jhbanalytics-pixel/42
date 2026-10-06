import hashlib
import json
import os

import pytest

from src.api.question_worker_bundle import BundleVerificationError, verify_bundle


LP_COMMIT = "1" * 40
ENTRYPOINT = "scripts/staging/run_general_question_worker.py"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def make_bundle(tmp_path, extra=None):
    root = tmp_path / "engine"
    files = {
        "requirements.lock": b"# locked dependencies\n",
        ENTRYPOINT: b"print('worker')\n",
    }
    files.update(extra or {})
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {
        "contract_version": "general_question_engine_bundle_v1",
        "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
    }
    (root / "bundle-manifest.json").write_bytes(canonical(manifest))
    digest = hashlib.sha256(canonical(manifest)).hexdigest()
    stamp = tmp_path / "runtime-build.json"
    stamp.write_bytes(
        canonical(
            {
                "contract_version": "general_question_runtime_build_v1",
                "lp_commit": LP_COMMIT,
                "engine_bundle_digest": digest,
            }
        )
    )
    return root, stamp, digest


def verify(fixture, **overrides):
    root, stamp, digest = fixture
    return verify_bundle(
        root,
        stamp,
        expected_lp_commit=overrides.get("commit", LP_COMMIT),
        expected_bundle_digest=overrides.get("digest", digest),
    )


def test_exact_bundle_bytes_and_pinned_identity_are_accepted(tmp_path):
    fixture = make_bundle(tmp_path, {"configs/markets/café.json": b"{}"})
    assert verify(fixture) == {
        "lp_commit": LP_COMMIT,
        "engine_bundle_digest": fixture[2],
    }


@pytest.mark.parametrize("change", ["changed", "missing", "extra", "renamed"])
def test_manifest_cannot_hide_changed_missing_or_unlisted_bytes(tmp_path, change):
    fixture = make_bundle(tmp_path)
    root = fixture[0]
    if change == "changed":
        (root / ENTRYPOINT).write_bytes(b"print('different')")
    elif change == "missing":
        (root / ENTRYPOINT).unlink()
    elif change == "extra":
        (root / "src").mkdir()
        (root / "src/unlisted.py").write_bytes(b"pass")
    else:
        (root / ENTRYPOINT).rename(root / "scripts/staging/other.py")
    with pytest.raises(BundleVerificationError):
        verify(fixture)


@pytest.mark.parametrize(
    "override",
    [
        {"commit": "2" * 40},
        {"digest": "f" * 64},
        {"commit": "bad"},
        {"digest": "F" * 64},
    ],
)
def test_external_release_binding_must_match_the_embedded_stamp(tmp_path, override):
    with pytest.raises(BundleVerificationError):
        verify(make_bundle(tmp_path), **override)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "src/.env.local",
        "configs/service-account.json",
        "src/module.pyc",
        "src/__pycache__/x.py",
        "credentials.json",
        ".git-credentials",
        "configs/.netrc",
    ],
)
def test_manifest_does_not_authorize_credentials_or_bytecode(tmp_path, name):
    with pytest.raises(BundleVerificationError):
        verify(make_bundle(tmp_path, {name: b"sensitive placeholder"}))


@pytest.mark.parametrize(
    "name",
    [
        "../escape.py",
        "/absolute.py",
        "src//double.py",
        "src/./relative.py",
        "src\\backslash.py",
        "C:/drive.py",
        "src/trailing. ",
    ],
)
def test_noncanonical_manifest_paths_are_rejected_even_when_digest_is_pinned(
    tmp_path, name
):
    fixture = make_bundle(tmp_path)
    root, stamp, _ = fixture
    manifest = json.loads((root / "bundle-manifest.json").read_bytes())
    manifest["files"][name] = "a" * 64
    encoded = canonical(manifest)
    (root / "bundle-manifest.json").write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    value = json.loads(stamp.read_bytes())
    value["engine_bundle_digest"] = digest
    stamp.write_bytes(canonical(value))
    with pytest.raises(BundleVerificationError):
        verify((root, stamp, digest))


@pytest.mark.parametrize("target", ["manifest", "stamp"])
def test_duplicate_json_keys_are_rejected(tmp_path, target):
    fixture = make_bundle(tmp_path)
    path = fixture[0] / "bundle-manifest.json" if target == "manifest" else fixture[1]
    data = path.read_bytes()
    path.write_bytes(b'{"contract_version":"ignored",' + data[1:])
    with pytest.raises(BundleVerificationError):
        verify(fixture)


@pytest.mark.skipif(os.name != "posix", reason="Target Linux filesystem symlink check")
@pytest.mark.parametrize("target", ["file", "directory", "manifest", "stamp"])
def test_symlinks_cannot_supply_verified_runtime_bytes(tmp_path, target):
    fixture = make_bundle(tmp_path)
    root, stamp, _ = fixture
    path = {
        "file": root / ENTRYPOINT,
        "directory": root / "scripts",
        "manifest": root / "bundle-manifest.json",
        "stamp": stamp,
    }[target]
    saved = tmp_path / "outside"
    path.rename(saved)
    path.symlink_to(saved, target_is_directory=target == "directory")
    with pytest.raises(BundleVerificationError):
        verify(fixture)


@pytest.mark.skipif(os.name != "posix", reason="Target case-sensitive Linux filesystem")
def test_case_collisions_are_rejected(tmp_path):
    fixture = make_bundle(
        tmp_path, {"src/worker.py": b"pass", "src/Worker.py": b"pass"}
    )
    with pytest.raises(BundleVerificationError):
        verify(fixture)


@pytest.mark.skipif(os.name != "posix", reason="Target case-sensitive Linux filesystem")
@pytest.mark.parametrize(
    "files",
    [{"src/a.py": b"pass", "Src/b.py": b"pass"}, {"BUNDLE-MANIFEST.JSON": b"{}"}],
)
def test_directory_and_reserved_manifest_case_collisions_are_rejected(tmp_path, files):
    with pytest.raises(BundleVerificationError):
        verify(make_bundle(tmp_path, files))


@pytest.mark.parametrize(
    "name", ["tests/check.py", "docs/design.md", "README.md", "requirements.txt"]
)
def test_bundle_accepts_only_the_backend_agreed_runtime_roots(tmp_path, name):
    with pytest.raises(BundleVerificationError):
        verify(make_bundle(tmp_path, {name: b"not admitted to runtime"}))
