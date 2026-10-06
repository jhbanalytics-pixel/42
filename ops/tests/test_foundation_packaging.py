import hashlib
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from ops.deploy.import_snapshot import main

GIT = shutil.which("git")
if GIT is None:
    fallback = Path.home() / "AppData/Local/Programs/Git/cmd/git.exe"
    GIT = str(fallback) if fallback.is_file() else None
if GIT is None or not Path(GIT).is_file():
    raise RuntimeError("git executable required for packaging tests")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        [GIT, "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def write(root: Path, name: str, raw: bytes) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def fixture(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Packaging Test")
    git(root, "config", "user.email", "packaging@example.invalid")
    # The manifest below declares git blob digests with LF endings for working
    # tree bytes that carry CRLF, which is what git records when it normalises
    # line endings on the way into the index. Set that normalisation on the
    # fixture repository so the blobs are the same on every platform instead of
    # depending on the global core.autocrlf the host git installation happens
    # to carry.
    git(root, "config", "core.autocrlf", "input")
    working = b"working\r\n"
    blob = b"blob\n"
    before_modified = b"before\n"
    after_modified = b"after\n"
    omitted = b"omit\n"
    added = b"added\n"
    readme = b"existing root\n"
    write(root, "src/working.txt", working)
    write(root, "src/blob.txt", blob)
    write(root, "src/modified.txt", after_modified)
    write(root, "added.txt", added)
    write(root, "README.md", readme)
    write(root, "cache/generated.pyc", b"cache")
    git(root, "add", "src", "added.txt", "README.md")
    git(root, "commit", "-m", "target")
    manifest = {
        "contract_version": "r02_packaging_verification_manifest_v1",
        "source_freeze_sha256": "a" * 64,
        "source_metadata": {"root": "Z:/source-is-unavailable", "head": "b" * 40},
        "files": [
            {
                "source": "engine",
                "path": "working.txt",
                "destination": "src/working.txt",
                "sha256": digest(working),
                "git_blob_sha256": digest(b"working\n"),
                "git_blob_id": "1" * 40,
                "disposition": "selected_import",
                "expected_git_mode": "100644",
            },
            {
                "source": "engine",
                "path": "blob.txt",
                "destination": "src/blob.txt",
                "sha256": digest(b"blob\r\n"),
                "git_blob_sha256": digest(blob),
                "git_blob_id": "2" * 40,
                "disposition": "selected_import",
                "expected_git_mode": "100644",
            },
            {
                "source": "app",
                "path": "modified.txt",
                "destination": "src/modified.txt",
                "sha256": digest(before_modified),
                "git_blob_sha256": digest(before_modified),
                "git_blob_id": "3" * 40,
                "disposition": "selected_import",
                "expected_git_mode": "100644",
            },
            {
                "source": "app",
                "path": "omitted.txt",
                "destination": "src/omitted.txt",
                "sha256": digest(omitted),
                "git_blob_sha256": digest(omitted),
                "git_blob_id": "4" * 40,
                "disposition": "selected_import",
                "expected_git_mode": "100644",
            },
        ],
        "baseline": [
            {
                "path": "README.md",
                "sha256": digest(readme),
                "git_blob_sha256": digest(readme),
                "expected_git_mode": "100644",
                "reason": "pre-existing destination history",
            }
        ],
        "packaging_edits": [
            {
                "destination": "added.txt",
                "action": "added",
                "before_sha256": None,
                "after_sha256": digest(added),
                "after_git_blob_sha256": digest(added),
                "expected_git_mode": "100644",
                "reason": "portable packaging input",
                "evidence_reference": "packaging-review:add",
            },
            {
                "destination": "src/modified.txt",
                "action": "modified",
                "before_sha256": digest(before_modified),
                "after_sha256": digest(after_modified),
                "after_git_blob_sha256": digest(after_modified),
                "expected_git_mode": "100644",
                "reason": "portable packaging adaptation",
                "evidence_reference": "packaging-review:modify",
            },
            {
                "destination": "src/omitted.txt",
                "action": "omitted",
                "before_sha256": digest(omitted),
                "after_sha256": None,
                "after_git_blob_sha256": None,
                "expected_git_mode": "100644",
                "reason": "source-only packaging omission",
                "evidence_reference": "packaging-review:omit",
            },
        ],
        "exclusions": [
            {
                "path": ".git",
                "type": "directory",
                "reason": "target repository metadata",
            },
            {
                "path": "cache",
                "type": "directory",
                "reason": "generated cache directory",
            },
        ],
    }
    return root, manifest


def invoke(tmp_path: Path, root: Path, manifest: dict) -> tuple[int, dict]:
    freeze = tmp_path / "derived-freeze.json"
    output = tmp_path / "receipt.json"
    freeze.write_text(json.dumps(manifest), encoding="utf-8")
    code = main(
        [
            "verify",
            "--freeze",
            str(freeze),
            "--root",
            str(root),
            "--output",
            str(output),
        ]
    )
    return code, json.loads(output.read_bytes())


def invoke_subprocess(
    tmp_path: Path, root: Path, manifest: dict, output: Path
) -> subprocess.CompletedProcess[bytes]:
    freeze = tmp_path / "subprocess-derived-freeze.json"
    freeze.write_text(json.dumps(manifest), encoding="utf-8")
    return run_cli(root, freeze, output)


def reviewed_baseline_modification(root: Path, manifest: dict) -> tuple[bytes, bytes]:
    before = (root / "README.md").read_bytes()
    after = before + b"\n## Portable runtime\n"
    write(root, "README.md", after)
    git(root, "add", "README.md")
    manifest["packaging_edits"].append(
        {
            "destination": "README.md",
            "action": "modified",
            "before_sha256": digest(before),
            "after_sha256": digest(after),
            "after_git_blob_sha256": digest(after),
            "expected_git_mode": "100644",
            "reason": "extend the retained root README",
            "evidence_reference": "packaging-review:readme",
        }
    )
    return before, after


def run_cli(
    root: Path, freeze: Path, output: Path
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "deploy/import_snapshot.py"),
            "verify",
            "--freeze",
            str(freeze),
            "--root",
            str(root),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
    )


def test_cli_verifies_without_source_checkout_and_preserves_version1_fields(
    tmp_path: Path,
) -> None:
    root, manifest = fixture(tmp_path)
    freeze = tmp_path / "derived-freeze.json"
    output = tmp_path / "receipt.json"
    freeze.write_text(json.dumps(manifest), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "deploy/import_snapshot.py"),
            "verify",
            "--freeze",
            str(freeze),
            "--root",
            str(root),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 0
    receipt = json.loads(output.read_bytes())
    assert receipt["result"] == "pass"
    assert receipt["source_freeze_sha256"] == "a" * 64
    assert receipt["counts"] == {
        "baseline": 1,
        "packaging_added": 1,
        "packaging_modified": 1,
        "packaging_omitted": 1,
        "source_git_blob": 1,
        "source_working_bytes": 1,
    }
    assert {item["disposition"] for item in receipt["files"]} == {
        "baseline",
        "packaging_added",
        "packaging_modified",
        "packaging_omitted",
        "source_git_blob",
        "source_working_bytes",
    }
    assert all("root" not in item for item in receipt["files"])


def test_reviewed_baseline_readme_modification_preserves_original_provenance(
    tmp_path: Path,
) -> None:
    root, manifest = fixture(tmp_path)
    before, _ = reviewed_baseline_modification(root, manifest)

    code, receipt = invoke(tmp_path, root, manifest)

    assert code == 0
    assert receipt["result"] == "pass"
    assert receipt["counts"]["baseline_packaging_modified"] == 1
    record = next(item for item in receipt["files"] if item["path"] == "README.md")
    assert record["disposition"] == "baseline_packaging_modified"
    assert record["baseline_before_sha256"] == digest(before)
    assert record["baseline_git_blob_sha256"] == digest(before)
    assert record["baseline_reason"] == "pre-existing destination history"
    assert record["evidence_reference"] == "packaging-review:readme"


@pytest.mark.parametrize(
    ("mutation", "failure"),
    [
        ("wrong_before", "packaging_before_mismatch"),
        ("stale_index", "git_blob_mismatch"),
        ("mode", "git_mode_mismatch"),
        ("missing", "destination_missing"),
        ("omitted", "packaging_edit_invalid"),
    ],
)
def test_invalid_baseline_packaging_edit_is_refused(
    tmp_path: Path, mutation: str, failure: str
) -> None:
    root, manifest = fixture(tmp_path)
    _, after = reviewed_baseline_modification(root, manifest)
    edit = manifest["packaging_edits"][-1]
    if mutation == "wrong_before":
        edit["before_sha256"] = "f" * 64
    elif mutation == "stale_index":
        write(root, "README.md", b"stale staged bytes\n")
        git(root, "add", "README.md")
        write(root, "README.md", after)
    elif mutation == "mode":
        git(root, "update-index", "--chmod=+x", "README.md")
    elif mutation == "missing":
        (root / "README.md").unlink()
    else:
        edit.update(action="omitted", after_sha256=None, after_git_blob_sha256=None)

    code, receipt = invoke(tmp_path, root, manifest)

    assert code == 1
    assert any(
        item == {"path": "README.md", "code": failure} for item in receipt["failures"]
    )


@pytest.mark.parametrize(
    ("mutation", "failure"),
    [
        ("changed", "destination_changed"),
        ("missing", "destination_missing"),
        ("extra", "destination_undeclared"),
        ("mode", "git_mode_mismatch"),
        ("resurrected", "omission_resurrected"),
        ("link", "destination_link"),
    ],
)
def test_destination_drift_writes_refused_receipt(
    tmp_path: Path, mutation: str, failure: str
) -> None:
    root, manifest = fixture(tmp_path)
    if mutation == "changed":
        write(root, "src/working.txt", b"changed\n")
    elif mutation == "missing":
        (root / "src/working.txt").unlink()
    elif mutation == "extra":
        write(root, "src/new-code.py", b"new\n")
    elif mutation == "mode":
        git(root, "update-index", "--chmod=+x", "src/working.txt")
    elif mutation == "resurrected":
        write(root, "src/omitted.txt", b"omit\n")
    else:
        link = root / "src/linked.txt"
        try:
            link.symlink_to(root / "src/working.txt")
        except OSError as error:
            pytest.skip(f"links unavailable: {error}")
        manifest["packaging_edits"].append(
            {
                "destination": "src/linked.txt",
                "action": "added",
                "before_sha256": None,
                "after_sha256": digest((root / "src/working.txt").read_bytes()),
                "after_git_blob_sha256": digest(
                    (root / "src/working.txt").read_bytes()
                ),
                "expected_git_mode": "100644",
                "reason": "link control",
                "evidence_reference": "packaging-review:link",
            }
        )

    code, receipt = invoke(tmp_path, root, manifest)

    assert code == 1
    assert receipt["result"] == "refused"
    assert any(item["code"] == failure for item in receipt["failures"])
    assert all(not Path(item["path"]).is_absolute() for item in receipt["failures"])


@pytest.mark.parametrize(
    ("mutation", "failure"),
    [
        ("added_collision", "packaging_edit_collision"),
        ("added_case_collision", "packaging_edit_collision"),
        ("modified_before", "packaging_before_mismatch"),
        ("omitted_before", "packaging_before_mismatch"),
        ("invalid_mode", "git_mode_invalid"),
        ("conflicting_edit", "packaging_edit_collision"),
    ],
)
def test_invalid_packaging_edits_are_refused(
    tmp_path: Path, mutation: str, failure: str
) -> None:
    root, manifest = fixture(tmp_path)
    if mutation == "added_collision":
        manifest["packaging_edits"][0]["destination"] = "src/working.txt"
    elif mutation == "added_case_collision":
        manifest["packaging_edits"][0]["destination"] = "SRC/WORKING.TXT"
    elif mutation == "modified_before":
        manifest["packaging_edits"][1]["before_sha256"] = "f" * 64
    elif mutation == "omitted_before":
        manifest["packaging_edits"][2]["before_sha256"] = "f" * 64
    elif mutation == "invalid_mode":
        manifest["packaging_edits"][0]["expected_git_mode"] = "777"
    else:
        manifest["packaging_edits"].append(deepcopy(manifest["packaging_edits"][1]))

    code, receipt = invoke(tmp_path, root, manifest)

    assert code == 1
    assert receipt["result"] == "refused"
    assert any(item["code"] == failure for item in receipt["failures"])


def test_untracked_file_cannot_claim_a_verified_git_mode(tmp_path: Path) -> None:
    root, manifest = fixture(tmp_path)
    write(root, "untracked.txt", b"untracked\n")
    manifest["packaging_edits"].append(
        {
            "destination": "untracked.txt",
            "action": "added",
            "before_sha256": None,
            "after_sha256": digest(b"untracked\n"),
            "after_git_blob_sha256": digest(b"untracked\n"),
            "expected_git_mode": "100644",
            "reason": "mode control",
            "evidence_reference": "packaging-review:untracked",
        }
    )

    code, receipt = invoke(tmp_path, root, manifest)

    assert code == 1
    assert any(
        item == {"path": "untracked.txt", "code": "git_mode_unproven"}
        for item in receipt["failures"]
    )


@pytest.mark.parametrize("sink", ["inside", "link", "linked_parent", "directory"])
def test_unsafe_receipt_sink_writes_nothing_and_uses_bounded_stderr(
    tmp_path: Path, sink: str
) -> None:
    root, manifest = fixture(tmp_path)
    victim = tmp_path / "victim.json"
    victim.write_bytes(b"unchanged")
    if sink == "inside":
        output = root / "receipt.json"
    elif sink == "link":
        output = tmp_path / "receipt-link.json"
        try:
            output.symlink_to(victim)
        except OSError as error:
            pytest.skip(f"links unavailable: {error}")
    elif sink == "linked_parent":
        actual = tmp_path / "actual-output"
        actual.mkdir()
        linked = tmp_path / "linked-output"
        try:
            linked.symlink_to(actual, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"links unavailable: {error}")
        output = linked / "receipt.json"
    else:
        output = tmp_path / "output-directory"
        output.mkdir()

    completed = invoke_subprocess(tmp_path, root, manifest, output)

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == (
        b'{"code":"output_path_invalid","failure_count":1,"result":"refused"}\n'
    )
    assert victim.read_bytes() == b"unchanged"
    if sink in {"inside", "linked_parent"}:
        assert not output.exists()


def test_stale_index_blob_is_refused_even_when_working_bytes_and_mode_match(
    tmp_path: Path,
) -> None:
    root, manifest = fixture(tmp_path)
    write(root, "src/working.txt", b"staged wrong bytes\n")
    git(root, "add", "src/working.txt")
    write(root, "src/working.txt", b"working\r\n")

    code, receipt = invoke(tmp_path, root, manifest)

    assert code == 1
    assert any(
        item == {"path": "src/working.txt", "code": "git_blob_mismatch"}
        for item in receipt["failures"]
    )


@pytest.mark.parametrize("transition", ["file_to_directory", "directory_to_file"])
def test_exclusion_type_transition_is_refused(tmp_path: Path, transition: str) -> None:
    root, manifest = fixture(tmp_path)
    if transition == "file_to_directory":
        (root / "excluded.bin").mkdir()
        manifest["exclusions"].append(
            {
                "path": "excluded.bin",
                "type": "file",
                "reason": "generated file",
            }
        )
    else:
        shutil.rmtree(root / "cache")
        write(root, "cache", b"changed type")

    code, receipt = invoke(tmp_path, root, manifest)

    assert code == 1
    assert any(
        item["code"] == "exclusion_type_mismatch" for item in receipt["failures"]
    )


def test_binary_crlf_is_not_guessed_to_be_original_text_blob(tmp_path: Path) -> None:
    root, manifest = fixture(tmp_path)
    raw = b"\xff\r\n"
    write(root, "src/binary.bin", raw)
    git(root, "add", "src/binary.bin")
    git(root, "commit", "-m", "binary fixture")
    manifest["files"].append(
        {
            "source": "engine",
            "path": "binary.bin",
            "destination": "src/binary.bin",
            "sha256": digest(b"different binary"),
            "git_blob_sha256": digest(b"\xff\n"),
            "git_blob_id": "5" * 40,
            "disposition": "selected_import",
            "expected_git_mode": "100644",
        }
    )

    code, receipt = invoke(tmp_path, root, manifest)

    record = next(item for item in receipt["files"] if item["path"] == "src/binary.bin")
    assert code == 1
    assert record["disposition"] == "source_working_bytes"
    assert {
        item["code"] for item in receipt["failures"] if item["path"] == "src/binary.bin"
    } == {"destination_changed"}


@pytest.mark.parametrize("alias", ["equal", "hardlink"])
def test_receipt_cannot_alias_or_overwrite_freeze(tmp_path: Path, alias: str) -> None:
    root, manifest = fixture(tmp_path)
    freeze = tmp_path / "immutable-freeze.json"
    freeze.write_text(json.dumps(manifest), encoding="utf-8")
    output = freeze if alias == "equal" else tmp_path / "freeze-hardlink.json"
    if alias == "hardlink":
        try:
            os.link(freeze, output)
        except OSError as error:
            pytest.skip(f"hardlinks unavailable: {error}")
    before = freeze.read_bytes()

    completed = run_cli(root, freeze, output)

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == (
        b'{"code":"output_path_invalid","failure_count":1,"result":"refused"}\n'
    )
    assert freeze.read_bytes() == before
    assert output.read_bytes() == before


def test_existing_receipt_is_never_overwritten(tmp_path: Path) -> None:
    root, manifest = fixture(tmp_path)
    output = tmp_path / "existing-receipt.json"
    output.write_bytes(b"prior receipt")

    completed = invoke_subprocess(tmp_path, root, manifest, output)

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == (
        b'{"code":"output_path_invalid","failure_count":1,"result":"refused"}\n'
    )
    assert output.read_bytes() == b"prior receipt"


def test_receipt_creation_race_preserves_winner(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root, manifest = fixture(tmp_path)
    freeze = tmp_path / "race-freeze.json"
    output = tmp_path / "race-receipt.json"
    freeze.write_text(json.dumps(manifest), encoding="utf-8")
    original_open = Path.open

    def race(path: Path, mode: str = "r", *args, **kwargs):
        if path == output and mode in {"wb", "xb"} and not output.exists():
            with original_open(output, "wb") as stream:
                stream.write(b"race winner")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", race)

    code = main(
        [
            "verify",
            "--freeze",
            str(freeze),
            "--root",
            str(root),
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == (
        '{"code":"output_path_invalid","failure_count":1,"result":"refused"}\n'
    )
    assert output.read_bytes() == b"race winner"


def test_receipt_is_deterministic_across_two_fresh_outputs(tmp_path: Path) -> None:
    root, manifest = fixture(tmp_path)
    freeze = tmp_path / "deterministic-freeze.json"
    first = tmp_path / "first-receipt.json"
    second = tmp_path / "second-receipt.json"
    freeze.write_text(json.dumps(manifest), encoding="utf-8")

    first_run = run_cli(root, freeze, first)
    second_run = run_cli(root, freeze, second)

    assert (first_run.returncode, second_run.returncode) == (0, 0)
    assert first.read_bytes() == second.read_bytes()
