import json
import subprocess
import sys
from pathlib import Path

from ops.certification import fixture_inventory
from ops.certification.fixture_inventory import (
    INVENTORY_NAME,
    build_inventory,
    contract_versions,
    family_of,
    write_inventory,
)

ROOT = Path(__file__).resolve().parents[2]
UI_VERIFY_SCRIPT = "app/frontend/scripts/verify-instrument-package.mjs"
GATE_MANIFEST_CONTRACT = "r02_linux_gate_manifest_v1"


def inventory():
    return build_inventory(ROOT)


def test_every_tracked_fixture_file_belongs_to_exactly_one_family():
    tracked = fixture_inventory.tracked_files(ROOT)
    expected = sorted(p for p in tracked if family_of(p) is not None)
    listed = sorted(f["path"] for fam in inventory()["families"] for f in fam["files"])
    assert listed == expected
    assert len(listed) == len(set(listed))
    assert len(expected) >= 140
    assert {fam["path"].split("/")[0] for fam in inventory()["families"]} == {
        "app",
        "engine",
        "ops",
    }


def test_every_inventoried_file_exists_with_its_recorded_digest():
    for family in inventory()["families"]:
        assert family["files"], family["path"]
        for entry in family["files"]:
            path = ROOT / entry["path"]
            assert path.is_file(), entry["path"]
            assert fixture_inventory.sha256_file(path) == entry["sha256"], entry["path"]
            assert path.stat().st_size == entry["bytes"]


def test_every_family_reports_lexical_reader_candidates_separately_from_verified_readers():
    for family in inventory()["families"]:
        assert "reader_candidates" in family
        assert family["verified_readers"] == []
        for reader in family["reader_candidates"]:
            assert (ROOT / reader).is_file(), reader
            assert family_of(reader) is None, reader


def test_every_contract_version_remains_unproven_without_a_path_bound_reader_check():
    found = inventory()
    unread = {
        (row["family"], row["contract_version"])
        for row in found["unread_contract_versions"]
    }
    declared = {
        (family["path"], literal)
        for family in found["families"]
        for literal in family["contract_versions"]
    }
    assert unread == declared
    for family in found["families"]:
        for literal, verified_readers in family["contract_versions"].items():
            assert verified_readers == [], (family["path"], literal)


def test_the_ui_package_pin_agrees_across_package_lock_sidecar_and_verify_script():
    ui = inventory()["ui_package"]
    assert (
        ui["dependency"]
        == f"file:vendor/{fixture_inventory.UI_PACKAGE_NAME}-{ui['version']}.tgz"
    )
    assert (ROOT / ui["tgz"]).is_file()
    assert ui["tgz_sha256"] == fixture_inventory.sha256_file(ROOT / ui["tgz"])
    assert ui["sidecar_matches"] is True
    assert ui["lock_resolution_present"] is True
    verify = (ROOT / UI_VERIFY_SCRIPT).read_text(encoding="utf-8")
    assert ui["dependency"] in verify
    for unreferenced in ui["unreferenced_vendor_files"]:
        assert ui["version"] not in unreferenced


def test_the_gate_manifest_digest_is_the_pin_the_publish_config_carries():
    gate = inventory()["linux_gate_manifest"]
    manifest = ROOT / gate["path"]
    assert gate["sha256"] == fixture_inventory.sha256_file(manifest)
    assert gate["contract_version"] == GATE_MANIFEST_CONTRACT
    assert (
        json.loads(manifest.read_bytes())["contract_version"] == GATE_MANIFEST_CONTRACT
    )
    assert gate["pinned_in_publish_config"] is True
    publish = (ROOT / gate["publish_config"]).read_text(encoding="utf-8")
    assert publish.count(gate["sha256"]) == 1


def test_family_of_groups_by_the_directory_under_fixtures():
    assert family_of("app/tests/fixtures/pdf/x.pdf") == "app/tests/fixtures/pdf"
    assert family_of("app/tests/fixtures/x.json") == "app/tests/fixtures"
    assert family_of("engine/tests/fixtures/a/b/c.json") == "engine/tests/fixtures/a"
    assert family_of("app/src/api/main.py") is None


def test_contract_versions_are_read_from_json_and_from_text_literals(tmp_path):
    nested = tmp_path / "nested.json"
    nested.write_bytes(
        json.dumps(
            {"contract_version": "outer_v1", "rows": [{"contract_version": "inner_v2"}]}
        ).encode()
    )
    assert contract_versions(nested) == ["inner_v2", "outer_v1"]
    script = tmp_path / "fixture.js"
    script.write_text(
        "export const x = {contract_version: 'js_v3'};\n", encoding="utf-8"
    )
    assert contract_versions(script) == ["js_v3"]
    binary = tmp_path / "blob.pdf"
    binary.write_bytes(b"%PDF\x00\xff\xfe")
    assert contract_versions(binary) == []


def synthetic_tree(root):
    fixture = root / "app/tests/fixtures/family"
    fixture.mkdir(parents=True)
    (fixture / "record.json").write_bytes(b'{"contract_version": "family_v1"}')
    reader = root / "app/src/reader.py"
    reader.parent.mkdir(parents=True)
    reader.write_text(
        'VERSION = "family_v1"\nPATH = "tests/fixtures/family/record.json"\n',
        encoding="utf-8",
    )
    history = root / "docs/history.md"
    history.parent.mkdir(parents=True)
    history.write_text(
        "record.json once declared family_v1.\n",
        encoding="utf-8",
    )
    other = root / "app/tests/fixtures/other"
    other.mkdir(parents=True)
    (other / "record.json").write_bytes(b'{"contract_version": "family_v1"}')
    orphan = root / "engine/tests/fixtures/orphan"
    orphan.mkdir(parents=True)
    (orphan / "lonely.json").write_bytes(b'{"contract_version": "lonely_v9"}')
    return fixture


def test_inventory_keeps_lexical_candidates_and_versions_unproven(tmp_path):
    synthetic_tree(tmp_path)
    found = build_inventory(tmp_path)
    by_path = {fam["path"]: fam for fam in found["families"]}
    assert by_path["app/tests/fixtures/family"]["reader_candidates"] == [
        "app/src/reader.py",
        "docs/history.md",
    ]
    assert by_path["app/tests/fixtures/family"]["verified_readers"] == []
    assert by_path["app/tests/fixtures/family"]["contract_versions"] == {
        "family_v1": []
    }
    assert by_path["engine/tests/fixtures/orphan"]["reader_candidates"] == []
    assert {
        (row["family"], row["contract_version"])
        for row in found["unread_contract_versions"]
    } == {
        ("app/tests/fixtures/family", "family_v1"),
        ("app/tests/fixtures/other", "family_v1"),
        ("engine/tests/fixtures/orphan", "lonely_v9"),
    }
    assert found["ui_package"]["dependency"] is None
    assert found["linux_gate_manifest"]["sha256"] is None


def test_inventory_digest_changes_when_a_fixture_changes(tmp_path):
    fixture = synthetic_tree(tmp_path)
    before = build_inventory(tmp_path)["inventory_digest"]
    (fixture / "record.json").write_bytes(b'{"contract_version": "family_v2"}')
    assert build_inventory(tmp_path)["inventory_digest"] != before


def test_cli_writes_the_inventory_file(tmp_path):
    tree = tmp_path / "tree"
    synthetic_tree(tree)
    output = tmp_path / "out"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ops.certification.fixture_inventory",
            "--root",
            str(tree),
            "--output",
            str(output),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    written = json.loads((output / INVENTORY_NAME).read_bytes())
    assert written["schema"] == "fixture_inventory_v2"
    assert written == write_inventory(tree, output)
    assert "unproven contract versions" in completed.stdout
    assert (
        fixture_inventory.main(
            ["--root", str(tmp_path / "missing"), "--output", str(output)]
        )
        == 1
    )
