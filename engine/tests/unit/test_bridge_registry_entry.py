"""A bridge v3 registry row pins one daily capture chain and never widens a v1 or v2 gate.

All pins below are synthetic and unissued.
"""

import copy
import json
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest

from tests.unit.test_protected_context_registry import fixture_document, local, registry, v2_entry

BRIDGE_LANES = (
    "enriched_content",
    "event_ledger",
    "raw_content",
    "seed_candidates",
    "seed_graph",
    "trend_analysis",
    "trend_scores",
)


def bridge_entry(cutoff="2026-09-20", digest="5" * 64):
    day = cutoff.replace("-", "")
    source_as_of = datetime.fromisoformat(cutoff).replace(tzinfo=UTC)
    return {
        "consumption_id": "exc_" + "6" * 64,
        "cutoff_date": cutoff,
        "derivation_id": "exd_" + "7" * 64,
        "manifest_sha256": "8" * 64,
        "market_scope": ["ke", "ng", "za"],
        "profile_id": f"staging_bridge_v3_{day}",
        "result_contract_version": "open_intelligence_execution_result_v3",
        "result_digest": digest,
        "result_id": "exr_" + digest,
        "snapshot_tables": [
            f"ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_{day}_{lane}"
            for lane in BRIDGE_LANES
        ],
        "source_as_of": source_as_of.replace(day=source_as_of.day + 1).isoformat(),
    }


def with_entries(monkeypatch, tmp_path, *entries):
    value = fixture_document()
    value["entries"].extend(copy.deepcopy(list(entries)))
    value["entries"].sort(key=lambda item: item["cutoff_date"])
    local(monkeypatch, tmp_path, value)
    return registry()


def test_bridge_row_is_its_own_family_and_carries_its_derivation(monkeypatch, tmp_path):
    r = with_entries(monkeypatch, tmp_path, bridge_entry(), v2_entry())
    entry = r.profile_for("2026-09-20")
    assert entry.result_contract_version == "open_intelligence_execution_result_v3"
    assert entry.derivation_id == "exd_" + "7" * 64
    assert entry.pinned
    assert entry.snapshot_tables == tuple(bridge_entry()["snapshot_tables"])
    assert r.result_version_for(entry.profile_id) == entry.result_contract_version
    assert r.profile_for("2026-09-07").derivation_id is None
    assert r.profile_for("2026-09-21").derivation_id is None


def test_bridge_row_never_widens_a_v1_or_v2_gate(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import general_question_context_admission as admission

    r = with_entries(monkeypatch, tmp_path, bridge_entry())
    assert r.allowed_cutoffs() == ("2026-09-07",)
    assert [item["profile_id"] for item in r._PROTECTED_CONTEXT_PROFILES] == [
        "protected_context_20260907_v1"
    ]
    hint = admission.source_window_hint(datetime(2026, 9, 23, tzinfo=UTC))
    assert hint["profile_id"] == "protected_context_20260907_v1"


def test_bridge_rows_are_listed_newest_first_with_the_read_binding_pins(monkeypatch, tmp_path):
    older = bridge_entry("2026-09-19", "9" * 64)
    newer = bridge_entry()
    r = with_entries(monkeypatch, tmp_path, older, newer)
    rows = r.bridge_entries()
    assert [row.profile_id for row in rows] == [
        "staging_bridge_v3_20260920",
        "staging_bridge_v3_20260919",
    ]
    pins = r.bridge_registry_row(rows[0])
    assert pins == {
        key: newer[key]
        for key in (
            "consumption_id",
            "cutoff_date",
            "manifest_sha256",
            "market_scope",
            "profile_id",
            "result_digest",
            "result_id",
            "snapshot_tables",
            "source_as_of",
        )
    }
    assert canonical_digest(pins) == canonical_digest(dict(pins))


def test_read_binding_pins_are_the_bridge_contract_field_set(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.source_estate_bridge import REGISTRY_FIELDS

    r = with_entries(monkeypatch, tmp_path, bridge_entry())
    assert tuple(r.bridge_registry_row(r.bridge_entries()[0])) == REGISTRY_FIELDS
    with pytest.raises(ValueError, match=r"^protected_context_registry_invalid$"):
        r.bridge_registry_row(r.profile_for("2026-09-07"))


def _defect(entry, defect):
    if defect == "null_pins":
        entry.update(consumption_id=None, manifest_sha256=None, result_id=None, result_digest=None)
    elif defect == "null_derivation":
        entry["derivation_id"] = None
    elif defect == "bad_derivation":
        entry["derivation_id"] = "exa_" + "7" * 64
    elif defect == "missing_derivation":
        del entry["derivation_id"]
    elif defect == "result_id_not_digest":
        entry["result_id"] = "exr_" + "4" * 64
    elif defect == "v1_profile":
        entry["profile_id"] = "protected_context_20260920_v1"
    elif defect == "wrong_day_profile":
        entry["profile_id"] = "staging_bridge_v3_20260919"
    elif defect == "collection_dataset_tables":
        entry["snapshot_tables"] = [
            name.replace("trends_v2_staging", "intelligence_42_sources_staging")
            for name in entry["snapshot_tables"]
        ]
    elif defect == "five_tables":
        entry["snapshot_tables"] = entry["snapshot_tables"][:5]
    elif defect == "v2_version":
        entry["result_contract_version"] = "open_intelligence_execution_result_v2"
    elif defect == "source_as_of":
        entry["source_as_of"] = "2026-09-20T00:00:00+00:00"
    elif defect == "market_scope":
        entry["market_scope"] = ["za", "ke"]
    return entry


@pytest.mark.parametrize(
    "defect",
    [
        "null_pins",
        "null_derivation",
        "bad_derivation",
        "missing_derivation",
        "result_id_not_digest",
        "v1_profile",
        "wrong_day_profile",
        "collection_dataset_tables",
        "five_tables",
        "v2_version",
        "source_as_of",
        "market_scope",
    ],
)
def test_bridge_row_contract_refuses(monkeypatch, tmp_path, defect):
    r = with_entries(monkeypatch, tmp_path, _defect(bridge_entry(), defect))
    with pytest.raises(ValueError, match=r"^protected_context_registry_invalid$"):
        r.load_protected_context_registry()


@pytest.mark.parametrize("family", ["v1", "v2"])
def test_derivation_id_belongs_only_to_the_bridge_family(monkeypatch, tmp_path, family):
    value = fixture_document()
    if family == "v1":
        value["entries"][0]["derivation_id"] = "exd_" + "7" * 64
    else:
        value["entries"].append(dict(v2_entry(), derivation_id="exd_" + "7" * 64))
    local(monkeypatch, tmp_path, value)
    with pytest.raises(ValueError, match=r"^protected_context_registry_invalid$"):
        registry().load_protected_context_registry()


def test_one_cutoff_names_one_registry_row(monkeypatch, tmp_path):
    value = fixture_document()
    value["entries"].extend([v2_entry("2026-09-20"), bridge_entry()])
    local(monkeypatch, tmp_path, value)
    with pytest.raises(ValueError, match=r"^protected_context_registry_invalid$"):
        registry().load_protected_context_registry()


def renderer():
    from scripts.staging import render_protected_context_entry

    return render_protected_context_entry


def shipped_bytes():
    from tests.unit.test_protected_context_registry import ROOT

    return (ROOT / "configs/open_intelligence/protected_context_registry_v1.json").read_bytes()


def test_bridge_row_is_rendered_from_the_issued_capture_chain_alone():
    from tests.unit.test_source_estate_bridge_contract import capture_chain

    chain = capture_chain()
    entry = renderer().render_bridge_entry(chain)
    assert entry == {
        "consumption_id": chain.result["consumption_id"],
        "cutoff_date": "2026-09-20",
        "derivation_id": chain.derivation["derivation_id"],
        "manifest_sha256": chain.result["manifest_sha256"],
        "market_scope": ["ke", "ng", "za"],
        "profile_id": "staging_bridge_v3_20260920",
        "result_contract_version": "open_intelligence_execution_result_v3",
        "result_digest": chain.result["result_digest"],
        "result_id": chain.result["result_id"],
        "snapshot_tables": bridge_entry()["snapshot_tables"],
        "source_as_of": "2026-09-21T00:00:00+00:00",
    }


def test_rendered_bridge_row_merges_into_canonical_registry_bytes(monkeypatch, tmp_path):
    from tests.unit.test_source_estate_bridge_contract import capture_chain

    chain = capture_chain()
    merged = renderer().render_bridge_registry(chain, shipped_bytes())
    path = tmp_path / "registry.json"
    path.write_bytes(merged)
    monkeypatch.setattr(registry(), "REGISTRY_PATH", path)
    # Selected by its cutoff: the shipped registry may already pin a newer bridge capture.
    (row,) = [entry for entry in registry().bridge_entries() if entry.cutoff_date == "2026-09-20"]
    assert row.derivation_id == chain.derivation["derivation_id"]
    assert registry().allowed_cutoffs() == ("2026-09-07",)
    assert renderer().render_bridge_registry(chain, merged) == merged
    document = json.loads(merged)
    for item in document["entries"]:
        if item["cutoff_date"] == "2026-09-20":
            item["market_scope"] = ["za"]
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    registry().parse_registry_bytes(raw)
    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        renderer().render_bridge_registry(chain, raw)


class _NotIssued:
    def __init__(self, chain):
        for name in ("derivation", "result", "operation_context", "operation_payload"):
            setattr(self, name, getattr(chain, name))


def test_bridge_row_refuses_a_chain_the_native_reader_did_not_issue():
    from tests.unit.test_source_estate_bridge_contract import capture_chain

    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        renderer().render_bridge_entry(_NotIssued(capture_chain()))
    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        renderer().render_bridge_entry(None)


def test_bridge_row_refuses_a_chain_of_another_operation():
    from tests.unit.test_source_estate_bridge_contract import capture_chain

    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        renderer().render_bridge_entry(capture_chain(operation="daily_composition_apply"))


def test_bridge_row_cutoff_is_the_chain_context_s_not_the_payload_s():
    from tests.unit.test_source_estate_bridge_contract import capture_chain

    chain = capture_chain(cutoff_utc="2026-09-22T00:00:00+00:00")
    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        renderer().render_bridge_entry(chain)


@pytest.mark.parametrize(
    "change",
    [
        {"missing_checks": ["native_job_readback"]},
        {"cutoff_date": "2026-09-19"},
        {"profile_id": "staging_bridge_v3_20260919"},
        {"contract_version": "open_intelligence_protected_source_snapshot_v2"},
        {"market_scope": []},
    ],
)
def test_bridge_row_refuses_a_payload_that_is_not_a_complete_v3_capture(change):
    from tests.unit.test_source_estate_bridge_contract import capture_chain, capture_payload

    chain = capture_chain(payload=capture_payload(**change))
    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        renderer().render_bridge_entry(chain)


@pytest.mark.parametrize(
    "case",
    [
        test_bridge_row_is_its_own_family_and_carries_its_derivation,
        test_bridge_rows_are_listed_newest_first_with_the_read_binding_pins,
    ],
    ids=lambda case: case.__name__,
)
def test_bridge_row_cases_hold_beside_a_committed_bridge_row(monkeypatch, tmp_path, case):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    case(monkeypatch, tmp_path)


@pytest.fixture
def trial_pinned(monkeypatch, tmp_path):
    """The module's rows are added to the real committed document with a trial pin row."""
    import sys

    from tests.unit.test_protected_context_registry import trial_document

    _w, row, value = trial_document(monkeypatch, tmp_path / "trial")
    monkeypatch.setattr(sys.modules[__name__], "fixture_document", lambda: copy.deepcopy(value))
    return row


@pytest.mark.parametrize(
    "case",
    [
        test_bridge_row_is_its_own_family_and_carries_its_derivation,
        test_bridge_row_never_widens_a_v1_or_v2_gate,
        test_read_binding_pins_are_the_bridge_contract_field_set,
    ],
    ids=lambda case: case.__name__,
)
def test_bridge_row_cases_read_a_trial_pin_row_beside_every_committed_row(
    monkeypatch, tmp_path, trial_pinned, case
):
    case(monkeypatch, tmp_path)
    assert registry().bridge_entries()[0].profile_id == trial_pinned["profile_id"]


def test_ledger_and_chain_rows_are_listed_newest_first_beside_a_trial_pin_row(
    monkeypatch, tmp_path, trial_pinned
):
    older = bridge_entry("2026-09-19", "9" * 64)
    newer = bridge_entry()
    r = with_entries(monkeypatch, tmp_path, older, newer)
    rows = r.bridge_entries()
    assert [row.profile_id for row in rows][:3] == [
        trial_pinned["profile_id"],
        "staging_bridge_v3_20260920",
        "staging_bridge_v3_20260919",
    ]
    assert [r.bridge_authority(row) for row in rows[:3]] == [
        "execution_ledger",
        "daily_chain",
        "daily_chain",
    ]
    assert r.bridge_registry_row(rows[0]) == {
        key: trial_pinned[key] for key in r.bridge_registry_row(rows[0])
    }
    assert r.bridge_registry_row(rows[1])["result_id"] == newer["result_id"]
