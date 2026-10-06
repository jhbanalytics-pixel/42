import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops.build import pin_linux_gate_manifest as pin

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / pin.MANIFEST_PATH
TOOL = ROOT / "ops/build/pin_linux_gate_manifest.py"
BUILDER_IMAGE = (
    "gcr.io/cloud-builders/docker@sha256:"
    "e036091375b816e40f07620582d1dfaa0e2c2da58ea5731fdcf560534b2a4a57"
)
STUB_BUILDER = '''"""Stub builder: a bounded engine inventory."""

import hashlib
import json


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def inventory_source(source_root, *, kind):
    assert kind == "engine"
    files = {
        name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        for name in ("requirements.lock", "scripts/staging/run_general_question_worker.py")
    }
    return {"commit": "0" * 40, "dirty": False, "files": files}
'''


def _render(manifest):
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _commit(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "core.autocrlf", "false"], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )


def _bound_copy(tmp_path):
    """Copy the manifest and every file it names into a temp root, then bind it."""
    manifest = json.loads(MANIFEST.read_bytes())
    root = tmp_path / "root"
    for section, base in pin.CLOSURE_BASES.items():
        for name in manifest[section]:
            target = root / base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / base / name).read_bytes())
    manifest_path = root / pin.MANIFEST_PATH
    manifest_path.write_bytes(MANIFEST.read_bytes())
    builder = root / pin.BUILDER_PATH
    builder.parent.mkdir(parents=True, exist_ok=True)
    builder.write_text(STUB_BUILDER, encoding="utf-8")
    entrypoint = root / pin.ENGINE_BUNDLE_ENTRYPOINT
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_bytes(b"print('worker')\n")
    (root / "engine/requirements.lock").write_bytes(b"fixture\n")
    bound = pin.apply_digests(manifest, pin.compute_all(root, manifest))
    manifest_path.write_bytes(_render(bound))
    _commit(root)
    assert pin.main(["--check", "--root", str(root)]) == 0
    return root, manifest_path


def _run(*argv):
    return subprocess.run(
        [sys.executable, str(TOOL), *argv], capture_output=True, timeout=120
    )


def test_check_reports_one_mutated_closure_file(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    name = "tests/unit/test_general_question_runtime.py"
    mutated = root / "engine" / name
    mutated.write_bytes(mutated.read_bytes() + b"\n# changed\n")
    _commit(root)
    pinned = json.loads(manifest_path.read_bytes())["engine_test_closure"][name]
    actual = hashlib.sha256(mutated.read_bytes()).hexdigest()
    capsys.readouterr()

    assert pin.main(["--check", "--root", str(root)]) == 1

    assert capsys.readouterr().out.splitlines() == [
        f"engine_test_closure {name} {pinned[:12]} {actual[:12]}"
    ]


def test_check_reports_a_missing_file_without_crashing(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    name = "app/configs/pdf_runtime.json"
    (root / name).unlink()
    _commit(root)
    pinned = json.loads(manifest_path.read_bytes())["pdf_runtime_closure"][name]
    capsys.readouterr()

    assert pin.main(["--check", "--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"pdf_runtime_closure {name} {pinned[:12]} missing"
    ]

    before = manifest_path.read_bytes()
    assert pin.main(["--write", "--root", str(root)]) == 2
    assert "ClosurePathError closure_file_missing" in capsys.readouterr().err
    assert manifest_path.read_bytes() == before


def test_write_is_idempotent(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    name = "app/requirements-dev.lock"
    old = json.loads(manifest_path.read_bytes())["test_dependency_closure"][name]
    mutated = root / name
    mutated.write_bytes(mutated.read_bytes() + b"# changed\n")
    new = hashlib.sha256(mutated.read_bytes()).hexdigest()
    before_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    capsys.readouterr()

    assert pin.main(["--write", "--root", str(root)]) == 0

    first = manifest_path.read_bytes()
    after_sha = hashlib.sha256(first).hexdigest()
    assert after_sha != before_sha
    assert capsys.readouterr().out.splitlines() == [
        f"pin_tool_modified_repin {name}",
        f"test_dependency_closure {name} {old} {new}",
        f"manifest_sha256 before {before_sha} after {after_sha}",
        "publish config absent cloudbuild.app.publish.yaml",
    ]
    assert json.loads(first)["test_dependency_closure"][name] == new
    _commit(root)

    assert pin.main(["--write", "--root", str(root)]) == 0

    assert manifest_path.read_bytes() == first
    assert capsys.readouterr().out.splitlines() == [
        "no changes",
        f"manifest_sha256 before {after_sha} after {after_sha}",
        "publish config absent cloudbuild.app.publish.yaml",
    ]


def test_write_leaves_every_non_digest_field_byte_identical(
    tmp_path, monkeypatch, capsys
):
    root, manifest_path = _bound_copy(tmp_path)
    before_raw = manifest_path.read_bytes()
    before = json.loads(before_raw)
    for name in (
        "engine/tests/conftest.py",
        "ops/tests/fixtures/linux_boundary/sitecustomize.py",
    ):
        (root / name).write_bytes((root / name).read_bytes() + b"\n")
    entrypoint = root / pin.ENGINE_BUNDLE_ENTRYPOINT
    entrypoint.write_bytes(entrypoint.read_bytes() + b"# changed\n")
    bundle = pin.compute_engine_bundle(root)

    assert pin.main(["--write", "--root", str(root)]) == 0

    after_raw = manifest_path.read_bytes()
    after = json.loads(after_raw)
    assert list(after) == list(before)
    protected = [key for key in before if key not in pin.DIGEST_SECTIONS]
    assert "contract_version" in protected and "commands" in protected
    for key in protected:
        assert _render(after[key]) == _render(before[key]), key
    assert after["engine_bundle"]["entrypoint"] == before["engine_bundle"]["entrypoint"]
    assert list(after["engine_bundle"]) == list(before["engine_bundle"])
    changed = [
        (old, new)
        for old, new in zip(before_raw.splitlines(), after_raw.splitlines())
        if old != new
    ]
    assert len(before_raw.splitlines()) == len(after_raw.splitlines())
    assert len(changed) == 2 + 3
    for old, new in changed:
        assert old.split(b":")[0] == new.split(b":")[0]
    assert set(capsys.readouterr().out.splitlines()) > {
        f"engine_bundle entrypoint_sha256 {before['engine_bundle']['entrypoint_sha256']} {bundle['entrypoint_sha256']}",
        f"engine_bundle files_map_sha256 {before['engine_bundle']['files_map_sha256']} {bundle['files_map_sha256']}",
    }


@pytest.mark.parametrize(
    "section, change",
    [
        ("app_test_closure", ("add", "app/src/api/extra.py")),
        ("engine_test_closure", ("remove", "tests/conftest.py")),
        ("adapter_hashes", ("add", "manifest.json")),
        ("pdf_runtime_closure", ("remove", "app/src/api/pdf_exporter.py")),
    ],
)
def test_refuses_a_manifest_whose_closure_paths_differ(
    tmp_path, capsys, section, change
):
    root, manifest_path = _bound_copy(tmp_path)
    manifest = json.loads(manifest_path.read_bytes())
    action, name = change
    if action == "add":
        manifest[section][name] = "0" * 64
    else:
        del manifest[section][name]
    manifest_path.write_bytes(_render(manifest))
    capsys.readouterr()

    with pytest.raises(pin.ClosurePathError, match=f"closure_paths_mismatch {section}"):
        pin.load_manifest(manifest_path)
    assert pin.main(["--check", "--root", str(root)]) == 2
    assert pin.main(["--write", "--root", str(root)]) == 2

    err = capsys.readouterr().err.splitlines()
    assert (
        err == [f"refused ClosurePathError closure_paths_mismatch {section} {name}"] * 2
    )
    assert manifest_path.read_bytes() == _render(manifest)


def test_refuses_a_changed_entrypoint_path(tmp_path):
    root, manifest_path = _bound_copy(tmp_path)
    manifest = json.loads(manifest_path.read_bytes())
    manifest["engine_bundle"]["entrypoint"] = "engine/other.py"
    manifest_path.write_bytes(_render(manifest))

    with pytest.raises(pin.ClosurePathError, match="entrypoint_mismatch"):
        pin.load_manifest(manifest_path)


def test_refuses_to_change_a_protected_field():
    manifest = json.loads(MANIFEST.read_bytes())

    with pytest.raises(
        pin.ProtectedFieldError, match="protected_field contract_version"
    ):
        pin.apply_digests(manifest, {"contract_version": "other"})
    with pytest.raises(pin.ProtectedFieldError, match="protected_field commands"):
        pin.apply_digests(manifest, {"commands": {}})
    with pytest.raises(
        pin.ProtectedFieldError, match="protected_field engine_bundle.entrypoint"
    ):
        pin.apply_digests(manifest, {"engine_bundle": {"entrypoint": "other"}})


def test_refuses_a_manifest_that_does_not_round_trip(tmp_path):
    root, manifest_path = _bound_copy(tmp_path)
    manifest_path.write_bytes(
        json.dumps(json.loads(manifest_path.read_bytes()), indent=4).encode("utf-8")
    )

    with pytest.raises(pin.ManifestFormatError, match="manifest_format"):
        pin.load_manifest(manifest_path)


def _publish_config(digest, *, extra_step=""):
    """The real publish config's shape: pinned builder images, one gate digest."""

    def docker_step(step_id, *args):
        joined = ", ".join(f'"{arg}"' for arg in args)
        return (
            f"  - id: {step_id}\r\n"
            f"    name: {BUILDER_IMAGE}\r\n"
            f"    args: [{joined}]\r\n"
        )

    return (
        "# Publish build for the question image.\r\n"
        "steps:\r\n"
        + docker_step(
            "build-toolchain", "build", "-f", "ops/build/Dockerfile.toolchain", "."
        )
        + docker_step("build-context-builder", "build", "-t", "context-builder", ".")
        + "  - id: build-context\r\n"
        "    name: context-builder\r\n"
        "    entrypoint: python\r\n"
        "    args:\r\n"
        "      - -c\r\n"
        "      - |\r\n"
        "        import hashlib\r\n"
        "        tree = hashlib.sha256()\r\n"
        + docker_step("build-app-image", "build", "-t", "question:${SHORT_SHA}", ".")
        + docker_step(
            "context-inventory-inside-image", "run", "--rm", "question:${SHORT_SHA}"
        )
        + docker_step(
            "pdf-runtime-smoke", "run", "--rm", "question:${SHORT_SHA}", "pdf"
        )
        + docker_step(
            "build-linux-gates", "build", "-f", "ops/tests/Dockerfile.linux-gates", "."
        )
        + "  - id: verify_linux_boundary_tests\r\n"
        f"    name: {BUILDER_IMAGE}\r\n"
        "    entrypoint: bash\r\n"
        "    args:\r\n"
        "      - -c\r\n"
        "      - |\r\n"
        "        set -euo pipefail\r\n"
        '        echo "$$1  ops/tests/fixtures/linux_boundary/manifest.json"'
        " | sha256sum -c -\r\n"
        '        echo "verify_linux_boundary_tests gate_sha256=$$1"\r\n'
        "      - verify_linux_boundary_tests\r\n"
        f"      - {digest}\r\n"
        f"{extra_step}"
        "\r\n"
        "images:\r\n"
        "  - example.invalid/question:${COMMIT_SHA}\r\n"
        "\r\n"
        "substitutions:\r\n"
        '  _REVIEW_MANIFEST_SHA256: ""\r\n'
        "\r\n"
        "timeout: 3600s\r\n"
    ).encode("utf-8")


def test_publish_config_with_pinned_builder_images_binds_and_moves(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    old_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    config = root / "cloudbuild.app.publish.yaml"
    config.write_bytes(_publish_config(old_sha))
    assert config.read_bytes().count(BUILDER_IMAGE.encode()) == 7
    capsys.readouterr()

    assert pin.publish_config_pin(config.read_bytes(), old_sha) == old_sha
    assert pin.main(["--check", "--root", str(root)]) == 0
    assert capsys.readouterr().out == ""

    mutated = root / "app/requirements-dev.lock"
    mutated.write_bytes(mutated.read_bytes() + b"# changed\n")
    assert pin.main(["--write", "--root", str(root)]) == 0

    new_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert new_sha != old_sha
    assert config.read_bytes() == _publish_config(new_sha)
    assert config.read_bytes().count(BUILDER_IMAGE.encode()) == 7
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"publish_config cloudbuild.app.publish.yaml {old_sha} {new_sha}"
    )
    _commit(root)
    assert pin.main(["--check", "--root", str(root)]) == 0
    assert pin.main(["--write", "--root", str(root)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "no changes",
        f"manifest_sha256 before {new_sha} after {new_sha}",
    ]
    assert config.read_bytes() == _publish_config(new_sha)


def test_check_reports_a_publish_config_carrying_an_older_digest(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    actual = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    config = root / "cloudbuild.app.publish.yaml"
    config.write_bytes(_publish_config("f" * 64))
    capsys.readouterr()

    assert pin.main(["--check", "--root", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"publish_config cloudbuild.app.publish.yaml {'f' * 12} {actual[:12]}"
    ]

    assert pin.main(["--write", "--root", str(root)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "no changes",
        f"manifest_sha256 before {actual} after {actual}",
        f"publish_config cloudbuild.app.publish.yaml {'f' * 64} {actual}",
    ]
    assert config.read_bytes() == _publish_config(actual)
    assert pin.main(["--check", "--root", str(root)]) == 0


def test_refuses_a_publish_config_without_the_literal(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    config = root / "elsewhere.yaml"
    config.write_bytes(_publish_config("<gate-manifest-sha256>"))
    before = config.read_bytes()
    capsys.readouterr()

    with pytest.raises(pin.PublishConfigError, match="publish_config_digest_absent"):
        pin.publish_config_pin(before, sha)
    argv = ["--root", str(root), "--publish-config", str(config)]
    assert pin.main(["--write", *argv]) == 2
    assert pin.main(["--check", *argv]) == 2
    assert (
        capsys.readouterr().err.splitlines()
        == ["refused PublishConfigError publish_config_digest_absent elsewhere.yaml"]
        * 2
    )
    assert config.read_bytes() == before


def test_refuses_a_publish_config_carrying_the_digest_in_two_steps(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    extra = f'  - id: echo-gate\r\n    args: ["echo", "{sha}"]\r\n'
    config = root / "cloudbuild.app.publish.yaml"
    config.write_bytes(_publish_config(sha, extra_step=extra))
    before = config.read_bytes()
    capsys.readouterr()

    with pytest.raises(pin.PublishConfigError, match="publish_config_digest_ambiguous"):
        pin.publish_config_pin(before, sha)
    assert pin.main(["--check", "--root", str(root)]) == 2
    assert pin.main(["--write", "--root", str(root)]) == 2
    assert (
        capsys.readouterr().err.splitlines()
        == [
            "refused PublishConfigError publish_config_digest_ambiguous "
            "cloudbuild.app.publish.yaml"
        ]
        * 2
    )
    assert config.read_bytes() == before


def test_write_skips_a_missing_publish_config(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    capsys.readouterr()

    assert pin.main(["--write", "--root", str(root)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "no changes",
        f"manifest_sha256 before {sha} after {sha}",
        "publish config absent cloudbuild.app.publish.yaml",
    ]
    assert pin.main(["--check", "--root", str(root)]) == 0
    assert capsys.readouterr().out == ""
    assert not (root / "cloudbuild.app.publish.yaml").exists()


def test_exit_codes_from_a_real_process(tmp_path):
    root, manifest_path = _bound_copy(tmp_path)

    clean = _run("--check", "--root", str(root))
    assert (clean.returncode, clean.stdout, clean.stderr) == (0, b"", b"")

    mutated = root / "engine/tests/conftest.py"
    mutated.write_bytes(mutated.read_bytes() + b"\n")
    _commit(root)
    stale = _run("--check", "--root", str(root))
    assert stale.returncode == 1
    assert stale.stdout.startswith(b"engine_test_closure tests/conftest.py ")
    assert stale.stderr == b""

    manifest = json.loads(manifest_path.read_bytes())
    manifest["adapter_hashes"]["extra.py"] = "0" * 64
    manifest_path.write_bytes(_render(manifest))
    refused = _run("--write", "--root", str(root))
    assert refused.returncode == 2
    assert refused.stdout == b""
    assert refused.stderr.splitlines() == [
        b"refused ClosurePathError closure_paths_mismatch adapter_hashes extra.py"
    ]
    assert b"Traceback" not in refused.stderr


def test_engine_bundle_survives_a_colliding_src_package(tmp_path):
    package = tmp_path / "src"
    package.mkdir()
    (package / "__init__.py").write_text("COLLIDING = True\n", encoding="utf-8")
    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "src" or name.startswith("src.")
    }
    sys.path.insert(0, str(tmp_path))
    try:
        import src

        assert Path(src.__file__).resolve().parent == package.resolve()
        bundle = pin.compute_engine_bundle(ROOT)
        assert sys.modules["src"] is src
    finally:
        sys.path.remove(str(tmp_path))
        for name in [n for n in sys.modules if n == "src" or n.startswith("src.")]:
            del sys.modules[name]
        sys.modules.update(saved)

    assert tuple(bundle) == pin.ENGINE_BUNDLE_DIGEST_KEYS
    assert bundle["file_count"] > 0


def test_engine_bundle_computation_is_deterministic_and_pinned():
    first = pin.compute_engine_bundle(ROOT)
    second = pin.compute_engine_bundle(ROOT)

    assert first == second
    assert tuple(first) == pin.ENGINE_BUNDLE_DIGEST_KEYS
    assert first["file_count"] > 0
    assert (
        first["entrypoint_sha256"]
        == hashlib.sha256(
            (ROOT / pin.ENGINE_BUNDLE_ENTRYPOINT).read_bytes()
        ).hexdigest()
    )
    pinned = json.loads(MANIFEST.read_bytes())["engine_bundle"]
    if pin.stale_entries({"engine_bundle": pinned}, {"engine_bundle": first}):
        pytest.skip("manifest stale, run --write")
    assert first == {key: pinned[key] for key in pin.ENGINE_BUNDLE_DIGEST_KEYS}


@pytest.mark.parametrize("mode", ["--check", "--write"])
@pytest.mark.parametrize(
    "name", ["app/requirements-dev.lock", "engine/requirements.lock"]
)
def test_crlf_checkout_refuses_without_output_or_writes(tmp_path, capsys, mode, name):
    root, manifest_path = _bound_copy(tmp_path)
    before = manifest_path.read_bytes()
    target = root / name
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    capsys.readouterr()
    assert pin.main([mode, "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_crlf_checkout" in captured.err
    assert name in captured.err
    assert manifest_path.read_bytes() == before


def test_check_lists_all_head_mismatches(tmp_path, capsys):
    root, _ = _bound_copy(tmp_path)
    names = ["app/requirements-dev.lock", "engine/requirements.lock"]
    for name in names:
        target = root / name
        target.write_bytes(target.read_bytes() + b"changed\n")
    capsys.readouterr()
    assert pin.main(["--check", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_checkout_dirty" in captured.err
    assert "differs_from_HEAD" in captured.err
    assert all(name in captured.err for name in names)


def test_write_repin_requires_commit_before_repeat(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    target = root / "engine/requirements.lock"
    target.write_bytes(target.read_bytes() + b"changed\n")
    capsys.readouterr()
    assert pin.main(["--write", "--root", str(root)]) == 0
    assert "pin_tool_modified_repin engine/requirements.lock" in capsys.readouterr().out
    before = manifest_path.read_bytes()
    assert pin.main(["--write", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_checkout_dirty" in captured.err
    assert "differs_from_HEAD" in captured.err
    assert manifest_path.read_bytes() == before


def test_gitless_checkout_fails_closed(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    (root / ".git").rename(root / ".fixture-git")
    before = manifest_path.read_bytes()
    capsys.readouterr()
    assert pin.main(["--write", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_head_unavailable" in captured.err
    assert manifest_path.read_bytes() == before


def test_binary_crlf_bytes_are_not_text_conversion(tmp_path, capsys):
    root, _ = _bound_copy(tmp_path)
    target = root / "app/tests/fixtures/pdf/newsreader-chromium151.pdf"
    target.write_bytes(target.read_bytes() + b"\x00\r\n")
    capsys.readouterr()
    assert pin.main(["--write", "--root", str(root)]) == 0
    assert (
        "pin_tool_modified_repin app/tests/fixtures/pdf/newsreader-chromium151.pdf"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("mode", ["--check", "--write"])
@pytest.mark.parametrize("restore", [False, True])
def test_digest_bytes_cannot_change_during_computation(
    tmp_path, capsys, monkeypatch, mode, restore
):
    root, manifest_path = _bound_copy(tmp_path)
    before = manifest_path.read_bytes()
    target = root / "app/requirements-dev.lock"
    original = target.read_bytes()
    compute = pin.compute_all

    def changing_compute(root, manifest):
        target.write_bytes(original + b"changed\n")
        result = compute(root, manifest)
        if restore:
            target.write_bytes(original)
        return result

    monkeypatch.setattr(pin, "compute_all", changing_compute)
    capsys.readouterr()
    assert pin.main([mode, "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_" in captured.err
    assert manifest_path.read_bytes() == before


def test_write_checks_crlf_before_digest_computation(tmp_path, capsys, monkeypatch):
    root, manifest_path = _bound_copy(tmp_path)
    before = manifest_path.read_bytes()
    target = root / "app/requirements-dev.lock"
    original = target.read_bytes()
    target.write_bytes(original.replace(b"\n", b"\r\n"))
    compute = pin.compute_all
    calls = []

    def repairing_compute(root, manifest):
        calls.append(True)
        result = compute(root, manifest)
        target.write_bytes(original)
        return result

    monkeypatch.setattr(pin, "compute_all", repairing_compute)
    capsys.readouterr()
    assert pin.main(["--write", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_crlf_checkout" in captured.err
    assert calls == []
    assert manifest_path.read_bytes() == before


@pytest.mark.parametrize("mode", ["--check", "--write"])
def test_untracked_inventory_input_has_its_own_refusal(tmp_path, capsys, mode):
    root, manifest_path = _bound_copy(tmp_path)
    builder = root / pin.BUILDER_PATH
    builder.write_text(
        STUB_BUILDER.replace(
            '("requirements.lock", "scripts/staging/run_general_question_worker.py")',
            '("requirements.lock", "scripts/staging/run_general_question_worker.py", "new_input.py")',
        ),
        encoding="utf-8",
    )
    _commit(root)
    (root / "engine/new_input.py").write_bytes(b"VALUE = 1\n")
    before = manifest_path.read_bytes()
    capsys.readouterr()
    assert pin.main([mode, "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_untracked_input engine/new_input.py" in captured.err
    assert manifest_path.read_bytes() == before


def test_unborn_head_is_not_an_untracked_input(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    (root / ".git").rename(root / ".fixture-git")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    before = manifest_path.read_bytes()
    capsys.readouterr()
    assert pin.main(["--write", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert "pin_tool_head_unavailable" in captured.err
    assert captured.out == ""
    assert manifest_path.read_bytes() == before


def test_stale_input_cannot_change_after_its_digest_was_computed(
    tmp_path, capsys, monkeypatch
):
    root, manifest_path = _bound_copy(tmp_path)
    target = root / "app/requirements-dev.lock"
    original = target.read_bytes()
    target.write_bytes(original + b"first edit\n")
    before = manifest_path.read_bytes()
    compute = pin.compute_all

    def changing_compute(root, manifest):
        computed = compute(root, manifest)
        target.write_bytes(original + b"second edit\n")
        return computed

    monkeypatch.setattr(pin, "compute_all", changing_compute)
    capsys.readouterr()
    assert pin.main(["--write", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_checkout_changed" in captured.err
    assert manifest_path.read_bytes() == before


def test_write_refuses_dirty_input_when_its_pin_does_not_move(tmp_path, capsys):
    root, manifest_path = _bound_copy(tmp_path)
    target = root / "app/requirements-dev.lock"
    original = target.read_bytes()
    target.write_bytes(original + b"committed edit\n")
    _commit(root)
    target.write_bytes(original)
    before = manifest_path.read_bytes()
    capsys.readouterr()
    assert pin.main(["--write", "--root", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pin_tool_checkout_dirty" in captured.err
    assert manifest_path.read_bytes() == before
