"""The bridge history loader admits a capture only through its issued chain and evidence.

Every chain, record and pin here is synthetic and unissued. The chains are read through
the real native chain reader over test clients.
"""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.daily_execution_authority import read_daily_execution_chain
from src.analysis.open_intelligence.daily_product_io import canonical_rows
from src.analysis.open_intelligence.source_estate_bridge_plan import build_bridge_plan

from tests.unit import test_source_estate_bridge_plan as plan_fixture
from tests.unit.source_bridge_chain_fixture import daily_chain, result_ref
from tests.unit.test_daily_product_completion import completion
from tests.unit.test_protected_context_registry import fixture_document, local
from tests.unit.test_source_estate_bridge_contract import (
    bridge_artifacts,
    capture_payload,
    clone_inputs,
    history_entries,
    recurring_grant,
    with_artifacts,
)
from tests.unit.test_staging_source_profile import receipt, run

CUTOFF_UTC = "2026-09-21T00:00:00+00:00"
AS_OF = "2026-09-21T01:00:00.000000Z"
NOW = "2026-09-21T01:05:00.000000Z"
PRODUCT_ROWS = [
    {"market": "za", "trend_date": "2026-09-19", "term": "load shedding"},
    {"market": "ng", "trend_date": "2026-09-19", "term": "fuel"},
    {"market": "za", "trend_date": "2026-09-19", "term": "rugby"},
]
OTHER_ARTIFACTS = (
    "build_provenance",
    "capture_contract",
    "cost_policy",
    "daily_profile",
    "recovery_context",
    "source_metadata",
    "storage_policy",
)
READ_ARTIFACTS = (
    "bridge_policy",
    "capture_plan",
    "collection_receipt_set",
    "history_completion_set",
    "temporal_rules",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Evidence:
    """Resolves which derivation recorded a result and reads the retained evidence."""

    def __init__(self, chains, source_run, record, rows):
        self.chains = chains
        self.source_run = source_run
        self.record = record
        self.rows = rows
        self.reads = []

    def chain_source(self, reference):
        self.reads.append(("chain", reference["operation"]))
        return self.chains[reference["result_id"]]

    def read_source_run(self, run_id):
        self.reads.append(("run", run_id))
        return {"status": "ok", "run": self.source_run}

    def completion_operation(self, reference):
        return "daily-1"

    def read_completion(self, operation_id):
        return self.record if operation_id == "daily-1" else None

    def read_product_rows(self, lane, day):
        self.reads.append(("rows", lane, day))
        return self.rows if (lane, day) == ("trend_analysis", "2026-09-19") else []


def _collection():
    derivation_id, clients, chain = daily_chain(
        operation="daily_source_collection",
        cutoff_utc=CUTOFF_UTC,
        payload={"contract_version": "daily_collection_payload_v1", "value": "measured"},
        started="2026-09-21T00:00:30+00:00",
        completed_at="2026-09-21T00:16:00+00:00",
        native_at="2026-09-21T00:16:00+00:00",
    )
    record = run(
        receipt=receipt(
            cutoff="2026-09-20",
            run_id="run-20260920",
            execution_id=chain.derivation["business_attempt_id"],
            image_uri=chain.operation_context["child_image_uri"],
        ),
        collection_started_at=datetime(2026, 9, 21, 0, 1, tzinfo=UTC),
        collection_completed_at=datetime(2026, 9, 21, 0, 15, tzinfo=UTC),
    )
    item = {
        "run_id": "run-20260920",
        "result_ref": result_ref(chain),
        "collection_receipt_digest": canonical_digest(record["receipt"]),
        "collection_started_at": "2026-09-21T00:01:00.000000Z",
        "collection_completed_at": "2026-09-21T00:15:00.000000Z",
    }
    return (derivation_id, clients, chain), record, item


def _composition():
    derivation_id, clients, chain = daily_chain(
        operation="daily_composition_apply",
        cutoff_utc="2026-09-20T00:00:00+00:00",
        payload={"contract_version": "daily_composition_payload_v1", "value": "measured"},
        started="2026-09-20T01:00:00+00:00",
        completed_at="2026-09-20T02:00:00+00:00",
        native_at="2026-09-20T02:00:01+00:00",
    )
    record = completion()
    record.update(
        business_attempt_id=chain.derivation["business_attempt_id"],
        image_uri=chain.operation_context["child_image_uri"],
    )
    digest = canonical_digest(canonical_rows(PRODUCT_ROWS))
    record["products"]["trend_analysis"] = {
        "state": "completed",
        "row_count": 3,
        "output_digest": digest,
        "readback_digest": digest,
    }
    entries = []
    for market in ("ke", "ng", "za"):
        selected = canonical_rows([row for row in PRODUCT_ROWS if row["market"] == market])
        entries.append(
            {
                "lane": "trend_analysis",
                "market": market,
                "product_date": "2026-09-19",
                "state": "completed" if selected else "empty",
                "result_ref": result_ref(chain),
                "product_receipt_digest": canonical_digest(record),
                "output_digest": canonical_digest(selected),
                "row_count": len(selected),
                "completed_at": "2026-09-20T02:00:00.000000Z",
                "available_at": "2026-09-20T02:00:01.000000Z",
                "reason_code": None,
            }
        )
    return (derivation_id, clients, chain), record, entries


def _bind_manifest(clients, raw_artifacts):
    """Record the capture's manifest and context as the derivation row holds them."""
    parts = clients.parts
    context_json = canonical_bytes(parts["operation_context"]).decode("utf-8")
    manifest = {
        "input_artifacts": [
            {"name": "operation_context", "sha256": sha(context_json.encode("utf-8"))},
            *({"name": name, "sha256": sha(raw_artifacts[name])} for name in sorted(raw_artifacts)),
        ]
    }
    manifest_json = canonical_bytes(manifest).decode("utf-8")
    parts["manifest"] = manifest
    parts["derivation"].update(
        canonical_manifest_json=manifest_json,
        manifest_sha256=sha(manifest_json.encode("utf-8")),
        canonical_operation_context_json=context_json,
        operation_context_sha256=sha(context_json.encode("utf-8")),
    )


def world():
    collection, source_run, receipt_item = _collection()
    composition, record, completed = _composition()
    artifacts = bridge_artifacts()
    artifacts["collection_receipt_set"]["receipts"] = [receipt_item]
    replaced = {(row["lane"], row["market"], row["product_date"]): row for row in completed}
    artifacts["history_completion_set"]["entries"] = [
        replaced.get((row["lane"], row["market"], row["product_date"]), row)
        for row in history_entries()
    ]
    inputs = plan_fixture.inputs()
    inputs["artifacts"] = artifacts
    inputs["profile"] = with_artifacts(inputs["profile"], artifacts)
    plan = build_bridge_plan(**inputs)
    raw = {name: canonical_bytes({"contract_version": name + "_v1"}) for name in OTHER_ARTIFACTS}
    raw["capture_plan"] = canonical_bytes(plan)
    for name in ("bridge_policy", "collection_receipt_set", "history_completion_set"):
        raw[name] = canonical_bytes(artifacts[name])
    raw["temporal_rules"] = canonical_bytes(artifacts["temporal_rules"])
    from src.analysis.open_intelligence.recurring_grant import grant_digest

    grant = recurring_grant(plan["snapshot_plan"])
    derivation_id, capture_clients, _ = daily_chain(
        grant_digest=grant_digest(grant),
        operation="daily_source_snapshot_capture",
        cutoff_utc=CUTOFF_UTC,
        payload=capture_payload(plan["snapshot_plan"]),
        started="2026-09-21T00:05:00+00:00",
        completed_at="2026-09-21T00:26:50+00:00",
        native_at="2026-09-21T00:27:00+00:00",
    )
    _bind_manifest(capture_clients, raw)
    capture = read_daily_execution_chain(derivation_id=derivation_id, clients=capture_clients)
    evidence = Evidence(
        {
            collection[2].result["result_id"]: collection[:2],
            composition[2].result["result_id"]: composition[:2],
        },
        source_run,
        record,
        deepcopy(PRODUCT_ROWS),
    )
    return {
        "capture": capture,
        "capture_clients": capture_clients,
        "raw": raw,
        "evidence": evidence,
        "plan": plan,
        "collection": collection,
        "grant": grant,
        "inputs": inputs,
        "clones": clone_inputs(plan["snapshot_plan"]),
    }


def grant_reader(grant):
    from src.analysis.open_intelligence.recurring_grant import grant_digest

    def read_grant(digest):
        return deepcopy(grant) if digest == grant_digest(grant) else None

    return read_grant


def register(monkeypatch, tmp_path, capture, **changes):
    from scripts.staging.render_protected_context_entry import render_bridge_entry

    entry = dict(render_bridge_entry(capture), **changes)
    value = fixture_document()
    value["entries"].append(entry)
    local(monkeypatch, tmp_path, value)
    return entry


def reader(raw, log=None):
    def read_input(name, digest):
        if log is not None:
            log.append(name)
        value = raw[name]
        return value

    return read_input


def clone_readers(clones, log=None):
    """Native readers for the stored capture facts, the creation jobs and each clone table."""

    def note(item):
        if log is not None:
            log.append(item)

    def read_capture_facts(stored_artifact):
        note(("facts", json.dumps(stored_artifact, sort_keys=True)))
        return deepcopy(clones["capture_facts"])

    def read_native_job(job_id):
        note(("job", job_id))
        return deepcopy(clones["native_jobs"].get(job_id))

    def read_clone(table):
        note(("clone", table))
        return deepcopy(clones["clone_metadata"].get(table))

    return {
        "read_capture_facts": read_capture_facts,
        "read_native_job": read_native_job,
        "read_clone": read_clone,
    }


def load(w, **overrides):
    from src.analysis.open_intelligence.general_question_context_admission import (
        load_bridge_history_source,
    )

    arguments = {
        "window_end": "2026-09-20",
        "request_as_of": AS_OF,
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ng", "za"],
        "capture_clients": w["capture_clients"],
        "read_input": reader(w["raw"]),
        "read_grant": grant_reader(w["grant"]),
        "evidence": w["evidence"],
        "now": NOW,
        **clone_readers(w["clones"]),
    }
    arguments.update(overrides)
    return load_bridge_history_source(**arguments)


def test_loader_admits_the_pinned_capture_through_its_chain_and_evidence(monkeypatch, tmp_path):
    w = world()
    entry = register(monkeypatch, tmp_path, w["capture"])
    log = []
    loaded = load(w, read_input=reader(w["raw"], log))
    assert sorted(log) == sorted(READ_ARTIFACTS)
    assert loaded.profile_id == "staging_bridge_v3_20260920"
    from src.analysis.open_intelligence.source_estate_bridge import REGISTRY_FIELDS

    assert loaded.registry_entry == {key: entry[key] for key in REGISTRY_FIELDS}
    binding = loaded.binding
    assert binding["registry_entry_digest"] == canonical_digest(loaded.registry_entry)
    assert binding["result_id"] == w["capture"].result["result_id"]
    assert binding["market_scope"] == ["ng", "za"]
    assert binding["available_at"] == "2026-09-21T00:27:00.000000Z"
    assert binding["purpose"] == "current_context"
    assert [(c["lane"], c["market"], c["product_date"], c["state"]) for c in loaded.cells] == [
        ("trend_analysis", "ng", "2026-09-19", "completed"),
        ("trend_analysis", "za", "2026-09-19", "completed"),
    ]
    history = json.loads(w["raw"]["history_completion_set"])["entries"]
    za = next(
        row
        for row in history
        if (row["lane"], row["market"], row["product_date"])
        == ("trend_analysis", "za", "2026-09-19")
    )
    assert loaded.cells[1]["completion_entry_digest"] == canonical_digest(za)
    assert loaded.cells[1]["available_at"] == "2026-09-20T02:00:01.000000Z"
    assert loaded.cells[1]["row_count"] == 2
    reasons = {(item["lane"], item["market"], item["product_date"]) for item in loaded.limitations}
    assert ("trend_analysis", "za", "2026-09-20") in reasons
    assert ("seed_candidates", "ng", "2026-09-19") in reasons
    assert not any(item["market"] == "ke" for item in loaded.limitations)
    assert {item["reason_code"] for item in loaded.limitations} == {
        "product_pending_at_cutoff",
        "native_completion_unrecorded",
    }
    assert ("run", "run-20260920") in w["evidence"].reads
    assert loaded.collection_runs == ("run-20260920",)


def test_no_bridge_row_covering_the_window_is_a_coverage_refusal(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    with pytest.raises(ValueError, match=r"^bridge_source_uncovered$"):
        load(w, window_end="2026-09-21")


def test_clones_without_a_recorded_chain_are_refused(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    clients = w["capture_clients"]

    def selected_only(derivation_id):
        value = type(clients).read_derivation(clients, derivation_id)
        return dict(value, lifecycle_state="selected", consumption=None)

    clients.read_derivation = selected_only
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w)


def test_a_hand_written_registry_row_that_names_no_chain_result_refuses(monkeypatch, tmp_path):
    w = world()
    digest = "0" * 64
    register(monkeypatch, tmp_path, w["capture"], result_digest=digest, result_id="exr_" + digest)
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w)


def test_a_row_whose_derivation_reads_another_chain_refuses(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w, capture_clients=w["collection"][1])


def test_the_manifest_is_the_one_the_registry_pins(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"], manifest_sha256="1" * 64)
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w)


@pytest.mark.parametrize("name", READ_ARTIFACTS)
def test_artifacts_are_the_bytes_the_manifest_digests(monkeypatch, tmp_path, name):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    raw = dict(w["raw"])
    raw[name] = raw[name] + b" "
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w, read_input=reader(raw))


def test_a_collection_receipt_the_retained_run_does_not_show_refuses(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    w["evidence"].source_run["receipt"]["execution_id"] = "exec-other"
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w)


def test_a_history_entry_the_product_rows_do_not_show_refuses(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    w["evidence"].rows[0]["term"] = "substituted"
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w)


def test_a_missing_completion_record_refuses(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    w["evidence"].record = None
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w)


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_as_of": "2026-09-21T00:26:00.000000Z"},
        {"now": "2026-09-21T00:59:00.000000Z"},
        {"now": "2026-12-20T00:00:00.000000Z"},
        {"market_scope": ["gh"]},
        {"market_scope": ["za", "ng"]},
        {"market_scope": []},
        {"client_scope_id": "fixture_scope"},
        {"window_end": "20260920"},
        {"now": datetime(2026, 9, 21, 1, 5)},
    ],
)
def test_request_scope_and_clock_refusals(monkeypatch, tmp_path, overrides):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    with pytest.raises(ValueError, match=r"^bridge_source_(invalid|uncovered)$"):
        load(w, **overrides)


@pytest.mark.parametrize(
    "change", [{"source_policy_digest": "0" * 64}, {"grant_id": "another_grant"}]
)
def test_the_recurring_grant_is_the_one_the_chain_was_authorized_under(
    monkeypatch, tmp_path, change
):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    other = dict(w["grant"], **change)
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w, read_grant=lambda _digest: deepcopy(other))
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w, read_grant=lambda _digest: None)


def test_the_evidence_resolver_cannot_substitute_a_chain(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    collection = w["collection"]
    evidence = w["evidence"]
    evidence.chains = dict.fromkeys(evidence.chains, collection[:2])
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w)


def test_the_loader_reads_back_every_clone_the_chain_capture_recorded(monkeypatch, tmp_path):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    log = []
    load(w, **clone_readers(w["clones"], log))
    tables = [row["destination_table"] for row in w["plan"]["snapshot_plan"]["relation_bindings"]]
    assert sorted(item[1] for item in log if item[0] == "clone") == sorted(tables)
    assert sorted(item[1] for item in log if item[0] == "job") == sorted(w["clones"]["native_jobs"])
    assert [item for item in log if item[0] == "facts"] == [("facts", "{}")]


@pytest.mark.parametrize("defect", ["replaced", "missing_clone", "job_changed", "facts_changed"])
def test_a_clone_replaced_or_unread_after_the_chain_capture_refuses(monkeypatch, tmp_path, defect):
    w = world()
    register(monkeypatch, tmp_path, w["capture"])
    clones = deepcopy(w["clones"])
    table = w["plan"]["snapshot_plan"]["relation_bindings"][3]["destination_table"]
    if defect == "replaced":
        clones["clone_metadata"][table]["creationTime"] = "1790038800000"
    elif defect == "missing_clone":
        del clones["clone_metadata"][table]
    elif defect == "job_changed":
        job = sorted(clones["native_jobs"])[0]
        clones["native_jobs"][job]["statistics"]["endTime"] = "1790036000000"
    else:
        clones["capture_facts"]["relation_readbacks"][3]["row_count"] = 5
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w, **clone_readers(clones))
    for name in ("read_capture_facts", "read_native_job", "read_clone"):
        with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
            load(w, **{name: None})


@pytest.mark.parametrize(
    "case",
    [
        test_loader_admits_the_pinned_capture_through_its_chain_and_evidence,
        test_no_bridge_row_covering_the_window_is_a_coverage_refusal,
    ],
    ids=lambda case: case.__name__,
)
def test_chain_rows_hold_beside_a_committed_bridge_row(monkeypatch, tmp_path, case):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    case(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "case",
    [
        "test_loader_admits_the_pinned_capture_through_its_chain_and_evidence",
        "test_no_bridge_row_covering_the_window_is_a_coverage_refusal",
    ],
)
def test_chain_rows_are_read_beside_a_trial_pin_row_and_every_committed_row(
    monkeypatch, tmp_path, case
):
    """A daily chain capture the day after the trial pin, registered into the real
    committed document that also holds the trial ledger row rendered from its receipt."""
    from copy import deepcopy

    from src.analysis.open_intelligence import protected_context_registry as r

    from tests.unit.test_protected_context_registry import trial_document, trial_world

    _w, row, value = trial_document(monkeypatch, tmp_path / "trial")
    loader = trial_world(monkeypatch, days_after=1).test_bridge_history_loader
    monkeypatch.setattr(loader, "fixture_document", lambda: deepcopy(value))
    getattr(loader, case)(monkeypatch, tmp_path)
    ledger, chain = r.bridge_entries()[1], r.bridge_entries()[0]
    assert ledger.profile_id == row["profile_id"]
    assert r.bridge_authority(ledger) == "execution_ledger"
    assert r.bridge_authority(chain) == "daily_chain"
    assert len(r.load_protected_context_registry()) == len(value["entries"]) + 1
