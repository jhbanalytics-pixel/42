import copy
import hashlib
import importlib
import json
import re
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def registry():
    return importlib.import_module("src.analysis.open_intelligence.protected_context_registry")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


COMMITTED = ROOT / "configs/open_intelligence/protected_context_registry_v1.json"
RECEIPTS = ROOT / "configs/open_intelligence/protected_context_receipts"
BRIDGE_PROFILE = re.compile(r"staging_bridge_v3_\d{8}")


def document():
    return json.loads(COMMITTED.read_bytes())


def fixture_document():
    """The committed document without its bridge rows: the base a test adds its own rows to.

    A committed bridge row names a real capture. A test that adds a synthetic row for an
    earlier cutoff would otherwise append it out of cutoff order, or meet the committed row
    as the newest one covering its window.
    """
    value = document()
    value["entries"] = [
        entry for entry in value["entries"] if not BRIDGE_PROFILE.fullmatch(entry["profile_id"])
    ]
    return value


def rehearsal_row(cutoff="2026-09-25"):
    """A synthetic, unissued execution ledger bridge row, shaped as a pin commit adds one."""
    day = date.fromisoformat(cutoff)
    return {
        "consumption_id": "exc_" + "5" * 64,
        "cutoff_date": cutoff,
        "manifest_sha256": "6" * 64,
        "market_scope": ["ke", "ng", "za"],
        "profile_id": f"staging_bridge_v3_{day:%Y%m%d}",
        "result_contract_version": "open_intelligence_execution_result_v2",
        "result_digest": "7" * 64,
        "result_id": "exr_" + "8" * 64,
        "snapshot_tables": registry().bridge_snapshot_tables(day),
        "source_as_of": datetime.combine(
            day + timedelta(days=1), datetime.min.time(), UTC
        ).isoformat(),
    }


def rehearse_pin(monkeypatch, tmp_path):
    """Stand in for the next pin commit: the committed registry gains one bridge row.

    The row's cutoff is 2026-09-25, or the day after the newest committed cutoff once a pin
    commit has landed, so the rehearsal holds at every later commit. Returns the row.
    """
    value = document()
    newest = max(date.fromisoformat(entry["cutoff_date"]) for entry in value["entries"])
    row = rehearsal_row(max(date(2026, 9, 25), newest + timedelta(days=1)).isoformat())
    value["entries"].append(row)
    raw = canonical(value)
    registry().parse_registry_bytes(raw)
    path = tmp_path / "committed_registry.json"
    path.write_bytes(raw)
    monkeypatch.setattr(sys.modules[__name__], "COMMITTED", path)
    monkeypatch.setattr(registry(), "REGISTRY_PATH", path)
    return row


def trial_cutoff():
    """The cutoff the next pin commit adds: 2026-09-25, or the day after the newest one."""
    newest = max(date.fromisoformat(entry["cutoff_date"]) for entry in document()["entries"])
    return max(date(2026, 9, 25), newest + timedelta(days=1))


def trial_world(monkeypatch, days_after=0):
    """The bridge loader fixtures moved whole to the trial cutoff, plus ``days_after`` days.

    Their synthetic chains carry the moved dates, so the clock the chain reader checks
    stored observations against reads a time after all of them.
    """
    from src.analysis.open_intelligence import daily_execution_authority

    from tests.unit.bridge_shift_fixture import shifted_world

    cutoff = trial_cutoff() + timedelta(days=days_after)
    read_at = datetime.combine(cutoff + timedelta(days=3), datetime.min.time(), UTC)
    monkeypatch.setattr(
        daily_execution_authority, "_utc_now", lambda: max(datetime.now(UTC), read_at)
    )
    return shifted_world((cutoff - date(2026, 9, 20)).days)


def trial_capture(monkeypatch, tmp_path):
    """A well formed synthetic ledger capture at the trial cutoff and the row it renders.

    The row is derived by the renderer from the capture's chain receipt, as the pin commit
    derives its row. It is never written to the committed registry.
    """
    route = trial_world(monkeypatch).test_bridge_ledger_route
    w = route.ledger_world(tmp_path / "trial_capture")
    return w, route.render(w)


def trial_document(monkeypatch, tmp_path, *entries):
    """The real committed document with the trial row left in, and a test's own rows."""
    w, row = trial_capture(monkeypatch, tmp_path)
    value = document()
    value["entries"] = sorted(
        [*value["entries"], *entries, row], key=lambda entry: entry["cutoff_date"]
    )
    return w, row, value


def trial_pin(monkeypatch, tmp_path, *entries):
    """Stand in for the pin commit: every committed row, the trial row and a test's rows."""
    w, row, value = trial_document(monkeypatch, tmp_path, *entries)
    local(monkeypatch, tmp_path, value)
    assert registry().bridge_entries()[0] == registry()._entry(row)
    return w, row


def local(monkeypatch, tmp_path, value):
    path = tmp_path / "registry.json"
    path.write_bytes(canonical(value))
    monkeypatch.setattr(registry(), "REGISTRY_PATH", path)
    return path


def third_entry():
    entry = copy.deepcopy(document()["entries"][0])
    entry["cutoff_date"] = "2026-09-14"
    entry["source_as_of"] = "2026-09-15T00:00:00+00:00"
    entry["profile_id"] = "protected_context_20260914_v1"
    entry["snapshot_tables"] = [
        name.replace("20260907", "20260914") for name in entry["snapshot_tables"]
    ]
    entry["consumption_id"] = "exc_" + "a" * 64
    entry["result_id"] = "exr_" + "b" * 64
    entry["manifest_sha256"] = "c" * 64
    entry["result_digest"] = "d" * 64
    return entry


def test_shipped_registry_and_immutable_records():
    r = registry()
    records = r.load_protected_context_registry()
    assert r.allowed_cutoffs() == ("2026-09-07",)
    assert records[1].cutoff_date == "2026-09-08"
    assert records[0].profile_id == "protected_context_20260907_v1"
    assert records[1].result_id is None
    assert isinstance(records[0].snapshot_tables, tuple)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        records[0].cutoff_date = "2026-09-14"
    assert r.profile_for(date(2026, 9, 7)) == records[0]
    with pytest.raises(ValueError, match="protected_context_registry_invalid"):
        r.profile_for("2026-09-21")


@pytest.mark.parametrize(
    "defect",
    ["duplicate", "missing", "extra", "table", "float", "partial", "order", "as_of", "market"],
)
def test_registry_defects_refuse(monkeypatch, tmp_path, defect):
    value = document()
    entry = value["entries"][0]
    if defect == "duplicate":
        value["entries"].append(copy.deepcopy(entry))
    elif defect == "missing":
        del entry["result_id"]
    elif defect == "extra":
        entry["extra"] = True
    elif defect == "table":
        entry["snapshot_tables"][0] += "_wrong"
    elif defect == "float":
        entry["cutoff_date"] = 1.0
    elif defect == "partial":
        entry["result_id"] = None
    elif defect == "order":
        value["entries"].reverse()
    elif defect == "as_of":
        entry["source_as_of"] = "2026-09-07T00:00:00+00:00"
    else:
        entry["market_scope"] = ["za", "za"]
    local(monkeypatch, tmp_path, value)
    with pytest.raises(ValueError, match="protected_context_registry_invalid"):
        registry().load_protected_context_registry()


def test_every_call_revalidates(monkeypatch, tmp_path):
    path = local(monkeypatch, tmp_path, document())
    registry().load_protected_context_registry()
    path.write_text('{"registry_id":"x","registry_id":"y"}', encoding="utf-8")
    for call in (
        registry().load_protected_context_registry,
        registry().allowed_cutoffs,
        lambda: registry().profile_for("2026-09-07"),
    ):
        with pytest.raises(ValueError, match="protected_context_registry_invalid"):
            call()


def test_admission_preserves_capture_only_cutoff():
    from src.analysis.open_intelligence import general_question_context_admission as admission

    assert (
        admission.source_window_hint(datetime(2026, 9, 10, tzinfo=UTC))["cutoff_date"]
        == "2026-09-07"
    )


def test_admission_third_cutoff(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import general_question_context_admission as admission

    value = fixture_document()
    value["entries"].append(third_entry())
    path = local(monkeypatch, tmp_path, value)
    hint = admission.source_window_hint(datetime(2026, 9, 22, tzinfo=UTC))
    assert hint["cutoff_date"] == "2026-09-14"
    assert hint["cutoff_date"] != "2026-09-21"
    path.write_bytes(b"{}")
    with pytest.raises(ValueError, match="protected_context_registry_invalid"):
        admission.source_window_hint(datetime(2026, 9, 22, tzinfo=UTC))


def test_v1_context_reader_keeps_its_own_profile_beside_a_newer_v2_entry(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import general_question_context_admission as admission

    value = fixture_document()
    value["entries"].append(v2_entry())
    local(monkeypatch, tmp_path, value)
    hint = admission.source_window_hint(datetime(2026, 9, 23, tzinfo=UTC))
    assert hint["profile_id"] == "protected_context_20260907_v1"


def source_for(cutoff):
    from tests.unit.test_general_question_context_queries import inputs

    request, plan, _, source = inputs()
    request["as_of"] = "2026-09-23T00:00:00+00:00"
    source["cutoff_date"] = cutoff
    source["profile_id"] = "protected_context_" + cutoff.replace("-", "") + "_v1"
    source["source_as_of"] = (
        datetime.combine(date.fromisoformat(cutoff), datetime.min.time(), UTC) + timedelta(days=1)
    ).isoformat()
    source["captured_at"] = source["source_as_of"]
    for table in source["snapshot_tables"]:
        table["snapshot_table"] = table["snapshot_table"].replace(
            "20260907", cutoff.replace("-", "")
        )
        table["snapshot_time"] = source["source_as_of"]
    return request, plan, source


def test_queries_third_cutoff(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.general_question_context_queries import _source

    value = fixture_document()
    value["entries"].append(third_entry())
    local(monkeypatch, tmp_path, value)
    for cutoff in ("2026-09-07", "2026-09-14"):
        assert _source(*source_for(cutoff))[0].isoformat() == cutoff
    for cutoff in ("2026-09-08", "2026-09-21"):
        with pytest.raises(ValueError, match="context_query_invalid"):
            _source(*source_for(cutoff))


def test_policy_third_cutoff(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.staging_source_profile import (
        LEGACY_CAPTURE_POLICY_VERSION,
        accepted_capture_policy,
    )

    value = fixture_document()
    value["entries"].append(third_entry())
    path = local(monkeypatch, tmp_path, value)
    assert accepted_capture_policy(LEGACY_CAPTURE_POLICY_VERSION)["allowed_cutoffs"] == (
        "2026-09-07",
        "2026-09-14",
    )
    path.write_bytes(b"{}")
    with pytest.raises(ValueError, match="protected_context_registry_invalid"):
        accepted_capture_policy(LEGACY_CAPTURE_POLICY_VERSION)


def test_execution_third_cutoff(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.execution_approval import _validate_manifest_invocation

    value = fixture_document()
    value["entries"].append(third_entry())
    local(monkeypatch, tmp_path, value)
    contract = {
        "dynamic_arguments": "source_snapshot_capture",
        "command": ("python",),
        "environment": (("TARGET", "staging"),),
        "secrets": (),
    }
    payload = {
        "command": ["python"],
        "arguments": [],
        "environment": [{"name": "TARGET", "value": "staging"}],
        "secrets": [],
        "max_retries": 0,
        "timeout_seconds": 600,
    }
    for cutoff in ("2026-09-07", "2026-09-08", "2026-09-14", "2026-09-21"):
        payload["arguments"] = [
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            cutoff,
            "--mode",
            "initial",
        ]
        if cutoff in ("2026-09-08", "2026-09-21"):
            with pytest.raises(ValueError):
                _validate_manifest_invocation(payload, "source_snapshot_capture", contract)
        else:
            assert (
                _validate_manifest_invocation(payload, "source_snapshot_capture", contract)[1][2]
                == cutoff
            )


def test_renderer_uses_retained_capture_receipt(tmp_path):
    row = retained_result_row()
    path = tmp_path / "receipt.json"
    path.write_bytes(canonical(row))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/staging/render_protected_context_entry.py"),
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    assert result.stdout.rstrip(b"\r\n") == canonical(document()["entries"][0])


def test_registry_digest_pin():
    assert (
        hashlib.sha256(
            (ROOT / "configs/open_intelligence/protected_context_registry_v1.json").read_bytes()
        ).hexdigest()
        == "b3fec23840ba1b4e06565bb73da3713dd64d5e59dc2baf8a03a25d570807f162"
    )


@pytest.mark.parametrize(
    "defect", ["status", "digest", "extra", "missing_checks", "table", "payload"]
)
def test_renderer_refuses_defective_receipt(defect):
    from scripts.staging.render_protected_context_entry import render_entry

    row = retained_result_row()
    payload = json.loads(row["canonical_result_json"])
    if defect == "status":
        row["status"] = "failed"
    elif defect == "digest":
        row["result_digest"] = "f" * 64
    elif defect == "extra":
        row["extra"] = True
    elif defect == "missing_checks":
        payload["missing_checks"] = ["unproven"]
        row = resealed_result(row, payload)
    elif defect == "table":
        payload["creation_records"][0]["destination"] += "_wrong"
        row = resealed_result(row, payload)
    else:
        payload["query_count"] += 1
        row["canonical_result_json"] = canonical(payload).decode()
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(row)


@pytest.mark.parametrize(
    "raw", [b"{}\n", b'{"registry_id":"x","registry_id":"y"}', b"[]", b'{"entries":NaN}']
)
def test_registry_rejects_invalid_serialization(monkeypatch, tmp_path, raw):
    path = local(monkeypatch, tmp_path, document())
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="protected_context_registry_invalid"):
        registry().load_protected_context_registry()


def test_registry_returns_defensive_profile_copies():
    profiles = registry()._PROTECTED_CONTEXT_PROFILES
    before = profiles[0]
    profiles[0]["result_digest"] = "f" * 64
    assert profiles[0] == before


@pytest.mark.parametrize("target", ["staging_source_profile", "daily_stages", "recurring_grant"])
@pytest.mark.parametrize("missing", [False, True])
def test_registry_defect_does_not_break_unrelated_import(target, missing, tmp_path):
    path = tmp_path / "registry.json"
    if not missing:
        path.write_bytes(b"{}")
    program = """
import importlib
import sys
from pathlib import Path
from src.analysis.open_intelligence import protected_context_registry as registry
registry.REGISTRY_PATH = Path(sys.argv[1])
importlib.import_module("src.analysis.open_intelligence." + sys.argv[2])
from src.analysis.open_intelligence.staging_source_profile import accepted_capture_policy, LEGACY_CAPTURE_POLICY_VERSION
try:
    accepted_capture_policy(LEGACY_CAPTURE_POLICY_VERSION)
except ValueError as error:
    assert str(error) == "protected_context_registry_invalid"
else:
    raise AssertionError("acting path accepted an invalid registry")
print("import succeeded; acting path refused")
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), target], cwd=ROOT, capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stdout.strip() == b"import succeeded; acting path refused"


@pytest.mark.parametrize("field", ["profile_id", "contract_version", "registry_id"])
def test_registry_version_and_profile_contract(monkeypatch, tmp_path, field):
    value = document()
    if field == "profile_id":
        value["entries"][0][field] = "protected_context_20260907_v2"
    else:
        value[field] = "unreviewed_version"
    local(monkeypatch, tmp_path, value)
    with pytest.raises(ValueError, match="protected_context_registry_invalid"):
        registry().load_protected_context_registry()


def test_noncanonical_registry_bytes_refuse(monkeypatch, tmp_path):
    path = local(monkeypatch, tmp_path, document())
    path.write_text(json.dumps(document(), indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="protected_context_registry_invalid"):
        registry().load_protected_context_registry()


def test_identity_refuses_unregistered_profile():
    from src.analysis.open_intelligence import general_question_context_admission as admission

    allowed, _ = admission._identity(
        [], "protected_context_20260907_v1", "protected_context_admission_v2"
    )
    assert allowed["units"]["collected_records"] == 0
    with pytest.raises(ValueError, match="protected_context_invalid"):
        admission._identity([], "protected_context_20260921_v1", "protected_context_admission_v2")


@pytest.mark.parametrize(
    "defect",
    ["contract", "failed_lane", "missing_creation", "extra_payload", "wrong_lane", "payload_type"],
)
def test_renderer_refuses_structurally_invalid_capture(defect):
    from scripts.staging.render_protected_context_entry import render_entry

    row = retained_result_row()
    payload = json.loads(row["canonical_result_json"])
    if defect == "contract":
        payload["contract_version"] = "foreign_contract"
    elif defect == "failed_lane":
        payload["creation_records"][0]["state"] = "failed"
    elif defect == "missing_creation":
        del payload["creation_records"]
    elif defect == "extra_payload":
        payload["unversioned_field"] = True
    elif defect == "wrong_lane":
        payload["creation_records"][0]["lane"] = "raw_content"
    else:
        payload = []
    row = resealed_result(row, payload)
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(row)


def test_renderer_missing_argument_refuses_with_named_code():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/staging/render_protected_context_entry.py")],
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"capture_receipt_invalid" in result.stderr


def test_renderer_payload_contract_matches_capture_writer():
    from scripts.staging import capture_protected_production_snapshot as capture
    from scripts.staging import render_protected_context_entry as renderer

    assert renderer._PAYLOAD_FIELDS == capture._COMPACT_RESULT_FIELDS


def retained_result_row():
    from tests.unit.test_general_question_context_admission import actual_capture_result

    row = actual_capture_result()
    row["completed_at"] = row["completed_at"].isoformat()
    return row


def resealed_result(row, payload):
    from src.analysis.open_intelligence.execution_approval import result_id

    row = copy.deepcopy(row)
    row["canonical_result_json"] = canonical(payload).decode()
    row["result_digest"] = hashlib.sha256(row["canonical_result_json"].encode()).hexdigest()
    completed = row["completed_at"]
    if isinstance(completed, str):
        completed = datetime.fromisoformat(completed)
    row["result_id"] = result_id(
        row["consumption_id"],
        row["result_reference"],
        row["result_digest"],
        row["status"],
        completed,
    )
    return row


@pytest.mark.parametrize("native_timestamp", [False, True])
def test_renderer_accepts_exact_retained_row_without_mutating_it(native_timestamp):
    from scripts.staging.render_protected_context_entry import render_entry

    row = retained_result_row()
    if native_timestamp:
        row["completed_at"] = datetime.fromisoformat(row["completed_at"])
    before = copy.deepcopy(row)
    assert render_entry(row).encode() == canonical(document()["entries"][0])
    assert row == before


@pytest.mark.parametrize(
    "defect",
    [
        "result_id",
        "version",
        "missing",
        "extra",
        "reference",
        "timestamp",
        "naive_timestamp",
        "malformed_timestamp",
        "noncanonical_payload",
        "approval_id",
        "execution_name",
    ],
)
def test_renderer_refuses_invalid_retained_result_row(defect):
    from scripts.staging.render_protected_context_entry import render_entry

    row = retained_result_row()
    if defect == "result_id":
        row["result_id"] = "exr_" + "f" * 64
    elif defect == "version":
        row["result_contract_version"] = "open_intelligence_execution_result_v2"
    elif defect == "missing":
        del row["approval_id"]
    elif defect == "extra":
        row["origin_registry_sha256"] = "a" * 64
    elif defect == "reference":
        row["result_reference"] += "wrong"
    elif defect == "timestamp":
        row["completed_at"] = "2026-09-08T15:46:33.936000+00:00"
    elif defect == "naive_timestamp":
        row["completed_at"] = "2026-09-08T15:46:32.936000"
    elif defect == "malformed_timestamp":
        row["completed_at"] = "not-a-time"
    elif defect == "noncanonical_payload":
        row["canonical_result_json"] = json.dumps(
            json.loads(row["canonical_result_json"]), indent=2
        )
    elif defect == "approval_id":
        row["approval_id"] = "invalid"
    else:
        row["execution_name"] = "invalid"
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(row)


def test_renderer_rejects_compact_capture_stdout_envelope():
    from scripts.staging.render_protected_context_entry import render_entry

    row = retained_result_row()
    envelope = {
        "payload": json.loads(row["canonical_result_json"]),
        "execution_result": {
            key: row[key]
            for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest", "status")
        },
    }
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(envelope)


def test_renderer_rejects_resealed_foreign_operation():
    from scripts.staging.render_protected_context_entry import render_entry
    from src.analysis.open_intelligence import execution_approval as authority

    row = retained_result_row()
    row["operation"] = "r3_apply"
    job = authority._OPERATION_CONTRACTS["r3_apply"]["job"]
    row["execution_name"] = (
        f"projects/ogilvy-trends-v2/locations/us-central1/jobs/{job}/executions/{job}-example"
    )
    row["result_reference"] = row["execution_name"] + "#source-snapshot"
    row = resealed_result(row, json.loads(row["canonical_result_json"]))
    native = {**row, "completed_at": datetime.fromisoformat(row["completed_at"])}
    authority._result_from_value(native, "capture_receipt_invalid")
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(row)


def test_renderer_rejects_resealed_wrong_capture_reference():
    from scripts.staging.render_protected_context_entry import render_entry
    from src.analysis.open_intelligence import execution_approval as authority

    row = retained_result_row()
    row["result_reference"] = row["execution_name"] + "#unrelated"
    row = resealed_result(row, json.loads(row["canonical_result_json"]))
    authority._result_from_value(
        {**row, "completed_at": datetime.fromisoformat(row["completed_at"])},
        "capture_receipt_invalid",
    )
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(row)


def test_renderer_rejects_resealed_failed_result():
    from scripts.staging.render_protected_context_entry import render_entry
    from src.analysis.open_intelligence import execution_approval as authority

    row = retained_result_row()
    row["status"] = "failed"
    row = resealed_result(row, json.loads(row["canonical_result_json"]))
    authority._result_from_value(
        {**row, "completed_at": datetime.fromisoformat(row["completed_at"])},
        "capture_receipt_invalid",
    )
    with pytest.raises(ValueError, match="capture_receipt_invalid"):
        render_entry(row)


def test_renderer_cli_rejects_duplicate_result_columns(tmp_path):
    row = retained_result_row()
    path = tmp_path / "duplicate.json"
    path.write_bytes(canonical(row)[:-1] + b',"status":"succeeded"}')
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/staging/render_protected_context_entry.py"),
            str(path),
        ],
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"capture_receipt_invalid" in result.stderr


def v2_entry(cutoff="2026-09-21"):
    from src.analysis.open_intelligence.staging_source_profile import SOURCE_LANES

    day = cutoff.replace("-", "")
    return {
        "consumption_id": "exc_" + "1" * 64,
        "cutoff_date": cutoff,
        "manifest_sha256": "2" * 64,
        "market_scope": ["ke", "ng", "za"],
        "profile_id": f"protected_context_{day}_v2",
        "result_contract_version": "open_intelligence_execution_result_v2",
        "result_digest": "3" * 64,
        "result_id": "exr_" + "4" * 64,
        "snapshot_tables": [
            f"ogilvy-trends-v2.intelligence_42_sources_staging.staging_source_{day}_{lane}"
            for lane in SOURCE_LANES
        ],
        "source_as_of": (
            datetime.combine(date.fromisoformat(cutoff), datetime.min.time(), UTC)
            + timedelta(days=1)
        ).isoformat(),
    }


def v1_source_manifest(cutoff):
    contract = {
        "dynamic_arguments": "source_snapshot_capture",
        "command": ("python",),
        "environment": (("TARGET", "staging"),),
        "secrets": (),
    }
    payload = {
        "command": ["python"],
        "arguments": [
            "scripts/staging/capture_protected_production_snapshot.py",
            "--cutoff-date",
            cutoff,
            "--mode",
            "initial",
        ],
        "environment": [{"name": "TARGET", "value": "staging"}],
        "secrets": [],
        "max_retries": 0,
        "timeout_seconds": 600,
    }
    return payload, contract


def test_unpinned_entry_admits_nothing_at_any_capture_gate():
    from src.analysis.open_intelligence.execution_approval import _validate_manifest_invocation
    from src.analysis.open_intelligence.general_question_context_queries import _source
    from src.analysis.open_intelligence.staging_source_profile import (
        LEGACY_CAPTURE_POLICY_VERSION,
        accepted_capture_policy,
    )

    r = registry()
    assert r.profile_for("2026-09-08").result_id is None
    assert r.allowed_cutoffs() == ("2026-09-07",)
    assert accepted_capture_policy(LEGACY_CAPTURE_POLICY_VERSION)["allowed_cutoffs"] == (
        "2026-09-07",
    )
    payload, contract = v1_source_manifest("2026-09-08")
    with pytest.raises(ValueError, match="execution_approval_manifest_invalid"):
        _validate_manifest_invocation(payload, "source_snapshot_capture", contract)
    with pytest.raises(ValueError, match="context_query_invalid"):
        _source(*source_for("2026-09-08"))
    payload, contract = v1_source_manifest("2026-09-07")
    assert _validate_manifest_invocation(payload, "source_snapshot_capture", contract)[1][2] == (
        "2026-09-07"
    )


def test_v2_entry_is_read_by_its_own_family_and_never_widens_a_v1_gate(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.execution_approval import _validate_manifest_invocation
    from src.analysis.open_intelligence.general_question_context_queries import _source
    from src.analysis.open_intelligence.staging_source_profile import (
        LEGACY_CAPTURE_POLICY_VERSION,
        accepted_capture_policy,
    )

    value = fixture_document()
    value["entries"].append(v2_entry())
    local(monkeypatch, tmp_path, value)
    r = registry()
    entry = r.profile_for("2026-09-21")
    assert entry.result_contract_version == "open_intelligence_execution_result_v2"
    assert r.profile_for("2026-09-07").result_contract_version == (
        "open_intelligence_execution_result_v1"
    )
    assert r.result_version_for(entry.profile_id) == entry.result_contract_version
    # The v1 context readers read only v1 results, so a v2 entry is never one of their profiles.
    assert [item["profile_id"] for item in r._PROTECTED_CONTEXT_PROFILES] == [
        "protected_context_20260907_v1",
    ]
    assert r.allowed_cutoffs() == ("2026-09-07",)
    assert accepted_capture_policy(LEGACY_CAPTURE_POLICY_VERSION)["allowed_cutoffs"] == (
        "2026-09-07",
    )
    payload, contract = v1_source_manifest("2026-09-21")
    with pytest.raises(ValueError):
        _validate_manifest_invocation(payload, "source_snapshot_capture", contract)
    with pytest.raises(ValueError, match="context_query_invalid"):
        _source(*source_for("2026-09-21"))


@pytest.mark.parametrize(
    "defect", ["null_pins", "v1_tables", "v1_profile", "version", "v1_with_version"]
)
def test_v2_entry_contract_refuses(monkeypatch, tmp_path, defect):
    value = fixture_document()
    entry = v2_entry()
    if defect == "null_pins":
        for key in ("consumption_id", "manifest_sha256", "result_digest", "result_id"):
            entry[key] = None
    elif defect == "v1_tables":
        entry["snapshot_tables"] = [
            name.replace("20260907", "20260921")
            for name in document()["entries"][0]["snapshot_tables"]
        ]
    elif defect == "v1_profile":
        entry["profile_id"] = "protected_context_20260921_v1"
    elif defect == "version":
        entry["result_contract_version"] = "open_intelligence_execution_result_v1"
    else:
        entry = copy.deepcopy(third_entry())
        entry["result_contract_version"] = "open_intelligence_execution_result_v1"
    value["entries"].append(entry)
    local(monkeypatch, tmp_path, value)
    with pytest.raises(ValueError, match="protected_context_registry_invalid"):
        registry().load_protected_context_registry()


def test_fixture_document_is_the_committed_registry_without_its_bridge_rows(monkeypatch, tmp_path):
    before = document()
    row = rehearse_pin(monkeypatch, tmp_path)
    assert document() == {**before, "entries": [*before["entries"], row]}
    assert registry().bridge_entries()[0].cutoff_date == row["cutoff_date"]
    assert fixture_document() == {
        **before,
        "entries": [
            entry
            for entry in before["entries"]
            if not entry["profile_id"].startswith("staging_bridge_v3_")
        ],
    }
    assert [entry["profile_id"] for entry in fixture_document()["entries"]][:2] == [
        "protected_context_20260907_v1",
        "protected_context_20260908_v1",
    ]
    assert registry().allowed_cutoffs() == ("2026-09-07",)


@pytest.mark.parametrize(
    "case",
    [
        test_admission_third_cutoff,
        test_queries_third_cutoff,
        test_policy_third_cutoff,
        test_execution_third_cutoff,
        test_v1_context_reader_keeps_its_own_profile_beside_a_newer_v2_entry,
        test_v2_entry_is_read_by_its_own_family_and_never_widens_a_v1_gate,
    ],
    ids=lambda case: case.__name__,
)
def test_cutoff_cases_hold_beside_a_committed_bridge_row(monkeypatch, tmp_path, case):
    rehearse_pin(monkeypatch, tmp_path)
    case(monkeypatch, tmp_path)


def test_committed_ledger_bridge_rows_are_the_rows_their_saved_receipts_render():
    from scripts.staging.render_protected_context_entry import check_registry_receipts

    checked = check_registry_receipts(COMMITTED.read_bytes(), RECEIPTS)
    assert checked == [
        entry.cutoff_date
        for entry in reversed(registry().bridge_entries())
        if registry().bridge_authority(entry) == "execution_ledger"
    ]


def test_the_trial_row_is_its_receipt_row_left_beside_every_committed_row(monkeypatch, tmp_path):
    committed = document()
    _w, row = trial_pin(monkeypatch, tmp_path)
    r = registry()
    assert row["cutoff_date"] == trial_cutoff().isoformat()
    assert [item.cutoff_date for item in r.load_protected_context_registry()] == sorted(
        [*(entry["cutoff_date"] for entry in committed["entries"]), row["cutoff_date"]]
    )
    entry = r.profile_for(row["cutoff_date"])
    assert r.bridge_authority(entry) == "execution_ledger"
    assert r.bridge_registry_row(entry)["result_id"] == row["result_id"]
    # The v1 readers' own profiles and gates are the committed ones.
    assert r.allowed_cutoffs() == ("2026-09-07",)
    assert [item["profile_id"] for item in r._PROTECTED_CONTEXT_PROFILES] == [
        "protected_context_20260907_v1"
    ]


def test_admission_reads_the_committed_document_beside_a_trial_bridge_row(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import general_question_context_admission as admission

    _w, row = trial_pin(monkeypatch, tmp_path, third_entry())
    after = datetime.combine(
        date.fromisoformat(row["cutoff_date"]) + timedelta(days=2), datetime.min.time(), UTC
    )
    assert admission.source_window_hint(datetime(2026, 9, 10, tzinfo=UTC))["cutoff_date"] == (
        "2026-09-07"
    )
    assert admission.source_window_hint(datetime(2026, 9, 22, tzinfo=UTC))["cutoff_date"] == (
        "2026-09-14"
    )
    # No bridge provider is served, so the bridge row offers no ceiling yet.
    assert admission.source_window_hint(after)["profile_id"] == "protected_context_20260914_v1"
    monkeypatch.setattr(admission, "PRODUCTION_BRIDGE_CLIENTS", lambda credentials: {})
    hint = admission.source_window_hint(after)
    assert {key: hint[key] for key in ("profile_id", "cutoff_date", "result_id")} == {
        key: row[key] for key in ("profile_id", "cutoff_date", "result_id")
    }
    assert admission.source_window_hint(datetime(2026, 9, 22, tzinfo=UTC))["cutoff_date"] == (
        "2026-09-14"
    )


def test_queries_read_the_committed_document_beside_a_trial_bridge_row(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.general_question_context_queries import _source

    _w, row = trial_pin(monkeypatch, tmp_path, third_entry())
    for cutoff in ("2026-09-07", "2026-09-14"):
        assert _source(*source_for(cutoff))[0].isoformat() == cutoff
    for cutoff in ("2026-09-08", "2026-09-21", row["cutoff_date"]):
        with pytest.raises(ValueError, match="context_query_invalid"):
            _source(*source_for(cutoff))


def test_policy_reads_the_committed_document_beside_a_trial_bridge_row(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.staging_source_profile import (
        LEGACY_CAPTURE_POLICY_VERSION,
        accepted_capture_policy,
    )

    trial_pin(monkeypatch, tmp_path, third_entry())
    assert accepted_capture_policy(LEGACY_CAPTURE_POLICY_VERSION)["allowed_cutoffs"] == (
        "2026-09-07",
        "2026-09-14",
    )


def test_execution_reads_the_committed_document_beside_a_trial_bridge_row(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.execution_approval import _validate_manifest_invocation

    _w, row = trial_pin(monkeypatch, tmp_path, third_entry())
    for cutoff in ("2026-09-07", "2026-09-14"):
        payload, contract = v1_source_manifest(cutoff)
        checked = _validate_manifest_invocation(payload, "source_snapshot_capture", contract)
        assert checked[1][2] == cutoff
    for cutoff in ("2026-09-08", "2026-09-21", row["cutoff_date"]):
        payload, contract = v1_source_manifest(cutoff)
        with pytest.raises(ValueError):
            _validate_manifest_invocation(payload, "source_snapshot_capture", contract)


def test_a_v2_entry_keeps_its_family_beside_a_trial_bridge_row(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import general_question_context_admission as admission
    from src.analysis.open_intelligence.staging_source_profile import (
        LEGACY_CAPTURE_POLICY_VERSION,
        accepted_capture_policy,
    )

    _w, row = trial_pin(monkeypatch, tmp_path, v2_entry())
    r = registry()
    assert r.profile_for("2026-09-21").result_contract_version == (
        "open_intelligence_execution_result_v2"
    )
    assert r.bridge_authority(r.profile_for(row["cutoff_date"])) == "execution_ledger"
    assert r.bridge_entries()[0].profile_id == row["profile_id"]
    assert r.allowed_cutoffs() == ("2026-09-07",)
    assert accepted_capture_policy(LEGACY_CAPTURE_POLICY_VERSION)["allowed_cutoffs"] == (
        "2026-09-07",
    )
    hint = admission.source_window_hint(datetime(2026, 9, 23, tzinfo=UTC))
    assert hint["profile_id"] == "protected_context_20260907_v1"


def test_a_trial_ledger_row_is_the_row_its_saved_receipt_renders(monkeypatch, tmp_path):
    from scripts.staging.render_protected_context_entry import check_registry_receipts
    from src.analysis.open_intelligence.execution_generations import load_trusted_generation

    w, row = trial_pin(monkeypatch, tmp_path)
    # The committed receipts, and the trial capture's receipt as the pin commit saves it.
    folder = tmp_path / "receipts"
    folder.mkdir()
    if RECEIPTS.exists():
        for path in RECEIPTS.iterdir():
            (folder / path.name).write_bytes(path.read_bytes())
    (folder / f"{row['cutoff_date']}.json").write_text(
        json.dumps(w["ledger"]["rows"], indent=2), encoding="utf-8"
    )
    catalogue = w["ledger"]["catalogue"]

    def generations(origin_registry_sha256, resource_manifest_sha256):
        # The trial generation is the fixture's; every committed receipt keeps its own.
        if (origin_registry_sha256, resource_manifest_sha256) == catalogue.pair:
            return catalogue(origin_registry_sha256, resource_manifest_sha256)
        return load_trusted_generation(origin_registry_sha256, resource_manifest_sha256)

    checked = check_registry_receipts(
        registry().REGISTRY_PATH.read_bytes(), folder, generation_loader=generations
    )
    ledger = [
        entry.cutoff_date
        for entry in reversed(registry().bridge_entries())
        if registry().bridge_authority(entry) == "execution_ledger"
    ]
    assert row["cutoff_date"] in checked
    assert checked == ledger


def test_a_pin_leaves_every_trusted_generation_pair_unmoved(monkeypatch, tmp_path):
    """The registry is pinned by tests only. No origin registry, resource manifest or
    registered policy of any trusted generation reads or names it, so a pin commit that
    adds a row leaves the active pair and every retained pair loading unchanged."""
    import shutil

    from src.analysis.open_intelligence import execution_generations as generations

    relative = COMMITTED.relative_to(ROOT).as_posix()
    pairs = [entry[:2] for entry in generations._TRUSTED_GENERATIONS]
    assert generations.ACTIVE_GENERATION_PAIR in pairs
    reads = []
    original = Path.read_bytes

    def recorded(path):
        reads.append(Path(path).resolve())
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", recorded)
    before = {pair: generations.load_trusted_generation(*pair) for pair in pairs}
    monkeypatch.setattr(Path, "read_bytes", original)
    assert reads
    assert COMMITTED.resolve() not in reads
    for path in set(reads):
        raw = path.read_bytes()
        assert relative.encode() not in raw, path
        assert COMMITTED.name.encode() not in raw, path
    # A pin commit's registry, with the trial row in it, beside the same reviewed files.
    _w, _row, value = trial_document(monkeypatch, tmp_path)
    copied = tmp_path / "package"
    shutil.copytree(ROOT / "configs", copied / "configs")
    (copied / relative).write_bytes(canonical(value))
    assert (copied / relative).read_bytes() != COMMITTED.read_bytes()
    monkeypatch.setattr(generations, "_PACKAGE_ROOT", copied)
    for pair in pairs:
        after = generations.load_trusted_generation(*pair)
        assert (after.origin_registry_sha256, after.resource_manifest_sha256) == pair
        assert after.registry.sha256 == before[pair].registry.sha256
        assert after.resource_manifest == before[pair].resource_manifest
        assert hashlib.sha256(after.registry_path.read_bytes()).hexdigest() == pair[0]
        assert hashlib.sha256(after.resource_manifest_path.read_bytes()).hexdigest() == pair[1]
