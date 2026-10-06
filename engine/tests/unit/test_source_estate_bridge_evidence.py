from copy import deepcopy
from hashlib import sha256

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.execution_approval import result_id_v2
from src.analysis.open_intelligence.source_estate_bridge_evidence import (
    parse_bridge_artifacts,
    resolve_completion,
    resolve_result_ref,
    resolve_source_run,
)
from src.analysis.open_intelligence.staging_source_profile import validate_completed_source_run

from tests.unit.test_daily_product_completion import completion
from tests.unit.test_execution_records_v2 import rebuild
from tests.unit.test_retained_readers_v2 import v2_fixture, v2_rows
from tests.unit.test_source_estate_bridge_contract import bridge_artifacts
from tests.unit.test_staging_source_profile import run


def artifacts():
    return bridge_artifacts()


def test_exact_artifacts_accept_canonical_bytes_without_issuing_authority():
    source = artifacts()
    assert parse_bridge_artifacts({k: canonical_bytes(v) for k, v in source.items()}) == source


@pytest.mark.parametrize("name", list(artifacts()))
def test_artifact_extra_fields_refuse(name):
    source = artifacts()
    source[name]["approved"] = True
    with pytest.raises(ValueError):
        parse_bridge_artifacts(source)


@pytest.mark.parametrize("mutation", ["missing", "extra", "serialized_nested", "wrong_version"])
def test_artifact_envelope_and_nested_shape_refuse(mutation):
    source = artifacts()
    if mutation == "missing":
        del source["bridge_policy"]
    elif mutation == "extra":
        source["extra"] = {}
    elif mutation == "serialized_nested":
        source["temporal_rules"]["rules"] = ["{}"]
    else:
        source["collection_receipt_set"]["contract_version"] = "v0"
    with pytest.raises(ValueError):
        parse_bridge_artifacts(source)


def test_unavailable_history_is_diagnostic_with_null_completion_facts():
    source = artifacts()
    entry = {
        "lane": "seed_graph",
        "market": "za",
        "product_date": "2026-09-19",
        "state": "unavailable",
        "result_ref": None,
        "product_receipt_digest": None,
        "output_digest": None,
        "row_count": None,
        "completed_at": None,
        "available_at": None,
        "reason_code": "predecessor_missing",
    }
    entries = source["history_completion_set"]["entries"]
    index = next(
        i
        for i, row in enumerate(entries)
        if (row["lane"], row["market"], row["product_date"]) == ("seed_graph", "za", "2026-09-19")
    )
    entries[index] = entry
    assert parse_bridge_artifacts(source)["history_completion_set"]["entries"][index] == entry
    entry["row_count"] = 0
    with pytest.raises(ValueError):
        parse_bridge_artifacts(source)


def chain_input():
    _, approval, consumption, result, catalogue = v2_fixture()
    reference = {
        name: getattr(result, name)
        for name in (
            "operation",
            "consumption_id",
            "manifest_sha256",
            "result_id",
            "result_digest",
            "origin_registry_sha256",
            "resource_manifest_sha256",
        )
    }
    return reference, v2_rows(approval, consumption, result), catalogue


def test_result_ref_uses_real_chain_decoder_and_independent_catalogue():
    reference, rows, catalogue = chain_input()
    checked = resolve_result_ref(
        reference, read_chain=lambda ref: rows, generation_loader=catalogue
    )
    assert checked["result"]["result_id"] == reference["result_id"]
    assert catalogue.calls


@pytest.mark.parametrize(
    "field",
    [
        "manifest_sha256",
        "result_id",
        "result_digest",
        "origin_registry_sha256",
        "resource_manifest_sha256",
    ],
)
def test_candidate_ref_cannot_certify_itself(field):
    reference, rows, catalogue = chain_input()
    changed = deepcopy(reference)
    changed[field] = "0" * 64
    with pytest.raises(ValueError):
        resolve_result_ref(changed, read_chain=lambda ref: rows, generation_loader=catalogue)


def test_nonunique_native_chain_refuses():
    reference, rows, catalogue = chain_input()
    rows[0]["result_count"] = 2
    with pytest.raises(ValueError):
        resolve_result_ref(reference, read_chain=lambda ref: rows, generation_loader=catalogue)


def stage_chain(payload):
    selected, approval, consumption, result, catalogue = v2_fixture()
    raw = canonical_bytes(payload).decode()
    digest = sha256(raw.encode()).hexdigest()
    result = rebuild(
        result,
        selected,
        canonical_result_json=raw,
        result_digest=digest,
        result_id=result_id_v2(
            result.consumption_id,
            result.result_reference,
            digest,
            result.status,
            result.completed_at,
            origin_registry_sha256=result.origin_registry_sha256,
            resource_manifest_sha256=result.resource_manifest_sha256,
        ),
    )
    reference = {
        name: getattr(result, name)
        for name in (
            "operation",
            "consumption_id",
            "manifest_sha256",
            "result_id",
            "result_digest",
            "origin_registry_sha256",
            "resource_manifest_sha256",
        )
    }
    return reference, v2_rows(approval, consumption, result), catalogue


@pytest.mark.parametrize("tamper", [False, True])
def test_synthetic_completion_stage_payload_is_not_native_evidence(tamper):
    _, _, consumption, _, _ = v2_fixture()
    record = completion()
    record.update(source_sha=consumption.source_sha, image_uri=consumption.image_uri)
    ref, rows, catalogue = stage_chain(
        {
            "state": "succeeded",
            "result_reference": "products-v1:daily-1",
            "output_digest": canonical_digest(record),
        }
    )
    if tamper:
        record["capture_digest"] = "0" * 64
        with pytest.raises(ValueError):
            resolve_completion(
                ref,
                operation_id="daily-1",
                read_chain=lambda _: rows,
                read_completion=lambda _: record,
                generation_loader=catalogue,
            )
    else:
        with pytest.raises(ValueError, match="bridge_payload_contract_unavailable"):
            resolve_completion(
                ref,
                operation_id="daily-1",
                read_chain=lambda _: rows,
                read_completion=lambda _: record,
                generation_loader=catalogue,
            )


@pytest.mark.parametrize("tamper", [False, True])
def test_synthetic_source_stage_payload_is_not_native_evidence(tamper):
    _, _, consumption, _, _ = v2_fixture()
    record = run()
    record["receipt"].update(
        source_sha=consumption.source_sha,
        image_uri=consumption.image_uri,
        execution_id=consumption.execution_name,
    )
    checked = validate_completed_source_run(record)
    output = {
        "receipt": checked["receipt"],
        **{
            key: checked[key].isoformat()
            for key in (
                "observation_window_end",
                "collection_started_at",
                "collection_completed_at",
            )
        },
    }
    ref, rows, catalogue = stage_chain(
        {
            "state": "succeeded",
            "result_reference": "source_run:" + record["receipt"]["run_id"],
            "output_digest": canonical_digest(output),
        }
    )
    item = {
        "run_id": record["receipt"]["run_id"],
        "result_ref": ref,
        "collection_receipt_digest": canonical_digest(record["receipt"]),
        **{
            key: record[key].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for key in ("collection_started_at", "collection_completed_at")
        },
    }
    if tamper:
        record["receipt"]["raw_rows_persisted"] += 1
        item["collection_receipt_digest"] = canonical_digest(record["receipt"])
        with pytest.raises(ValueError):
            resolve_source_run(
                item,
                read_chain=lambda _: rows,
                read_source_run=lambda _: {"status": "ok", "run": record},
                generation_loader=catalogue,
            )
    else:
        with pytest.raises(ValueError, match="bridge_payload_contract_unavailable"):
            resolve_source_run(
                item,
                read_chain=lambda _: rows,
                read_source_run=lambda _: {"status": "ok", "run": record},
                generation_loader=catalogue,
            )


def test_actual_daily_result_envelope_uses_native_daily_chain_reader():
    from src.analysis.open_intelligence.daily_execution_authority import read_daily_execution_chain
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        resolve_daily_result_ref,
    )

    from tests.unit.test_daily_execution_authority import DERIVATION_ID, ReadClients, _fixture

    _, _, observation, parts = _fixture()
    clients = ReadClients(parts, observation)
    chain = read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=clients)
    ref = {
        name: chain.result[name]
        for name in (
            "operation",
            "consumption_id",
            "manifest_sha256",
            "result_id",
            "result_digest",
            "origin_registry_sha256",
            "resource_manifest_sha256",
        )
    }
    admitted = resolve_daily_result_ref(ref, derivation_id=DERIVATION_ID, clients=clients)
    assert admitted.operation_payload == chain.operation_payload
    ref["result_digest"] = "0" * 64
    with pytest.raises(ValueError):
        resolve_daily_result_ref(ref, derivation_id=DERIVATION_ID, clients=clients)


@pytest.mark.parametrize("defect", [None, "execution_name", "early", "late"])
def test_actual_business_attempt_identity_and_collection_time_bounds(defect):
    from datetime import datetime

    from src.analysis.open_intelligence.daily_execution_authority import read_daily_execution_chain
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        check_daily_source_run_binding,
    )

    from tests.unit.test_daily_execution_authority import DERIVATION_ID, ReadClients, _fixture

    _, _, observation, parts = _fixture()
    clients = ReadClients(parts, observation)
    chain = read_daily_execution_chain(derivation_id=DERIVATION_ID, clients=clients)
    ref = {
        name: chain.result[name]
        for name in (
            "operation",
            "consumption_id",
            "manifest_sha256",
            "result_id",
            "result_digest",
            "origin_registry_sha256",
            "resource_manifest_sha256",
        )
    }
    record = run(
        collection_started_at=datetime.fromisoformat("2026-09-14T00:01:10+00:00"),
        collection_completed_at=datetime.fromisoformat("2026-09-14T00:01:30+00:00"),
    )
    record["receipt"].update(
        cutoff="2026-09-13",
        execution_id=chain.derivation["business_attempt_id"],
        image_uri=chain.operation_context["child_image_uri"],
    )
    if defect == "execution_name":
        record["receipt"]["execution_id"] = chain.consumption["execution_name"]
    elif defect == "early":
        record["collection_started_at"] = datetime.fromisoformat("2026-09-14T00:00:01+00:00")
    elif defect == "late":
        record["collection_completed_at"] = datetime.fromisoformat("2026-09-14T00:03:00+00:00")
    item = {
        "run_id": record["receipt"]["run_id"],
        "result_ref": ref,
        "collection_receipt_digest": canonical_digest(record["receipt"]),
        **{
            key: record[key].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for key in ("collection_started_at", "collection_completed_at")
        },
    }
    kwargs = {
        "derivation_id": DERIVATION_ID,
        "clients": clients,
        "read_source_run": lambda _: {"status": "ok", "run": record},
    }
    if defect:
        with pytest.raises(ValueError):
            check_daily_source_run_binding(item, **kwargs)
    else:
        assert check_daily_source_run_binding(item, **kwargs)["receipt"] == record["receipt"]


@pytest.mark.parametrize(
    "time_basis,event_field",
    [
        ("unknown", "published_at"),
        ("collection_observation", "published_at"),
        ("native_metric_interval", "published_at"),
        ("native_event_instant", None),
    ],
)
def test_time_basis_and_event_field_are_tied(time_basis, event_field):
    source = artifacts()
    source["temporal_rules"]["rules"][0].update(time_basis=time_basis, event_field=event_field)
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        parse_bridge_artifacts(source)


@pytest.mark.parametrize(
    "name,collection",
    [("collection_receipt_set", "receipts"), ("history_completion_set", "entries")],
)
def test_empty_receipt_and_completion_sets_refuse(name, collection):
    source = artifacts()
    source[name][collection] = []
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        parse_bridge_artifacts(source)


@pytest.mark.parametrize("defect", ["absent_entry", "absent_market", "date_gap"])
def test_missing_history_entry_must_be_explicitly_unavailable(defect):
    from tests.unit.test_source_estate_bridge_contract import history_entry

    source = artifacts()
    entries = source["history_completion_set"]["entries"]
    if defect == "absent_entry":
        entries[:] = [
            row
            for row in entries
            if (row["lane"], row["market"], row["product_date"])
            != ("event_ledger", "ng", "2026-09-19")
        ]
    elif defect == "absent_market":
        entries[:] = [row for row in entries if row["market"] != "ke"]
    else:
        entries[:] = sorted(
            [
                *entries,
                *(
                    history_entry(row["lane"], row["market"], "2026-09-17", "unavailable")
                    for row in entries
                    if row["product_date"] == "2026-09-19"
                ),
            ],
            key=lambda row: (row["lane"], row["market"], row["product_date"]),
        )
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        parse_bridge_artifacts(source)


def test_nan_inside_an_otherwise_valid_policy_refuses():
    raw = canonical_bytes(artifacts()["bridge_policy"]).replace(b'"za"', b"NaN")
    source = artifacts()
    source["bridge_policy"] = raw
    with pytest.raises(ValueError):
        parse_bridge_artifacts(source)
    source["bridge_policy"] = policy_with_nan()
    with pytest.raises(ValueError):
        parse_bridge_artifacts(source)


def policy_with_nan():
    value = artifacts()["bridge_policy"]
    value["market_scope"][-1] = float("nan")
    return value


PRODUCT_ROWS = [
    {"market": "za", "trend_date": "2026-09-19", "term": "load shedding"},
    {"market": "ng", "trend_date": "2026-09-19", "term": "fuel"},
    {"market": "za", "trend_date": "2026-09-19", "term": "rugby"},
]


def completion_case(*, operation="daily_composition_apply", cutoff="2026-09-20T00:00:00+00:00"):
    from src.analysis.open_intelligence.daily_product_io import canonical_rows

    from tests.unit.source_bridge_chain_fixture import daily_chain, result_ref

    derivation_id, clients, chain = daily_chain(
        operation=operation,
        cutoff_utc=cutoff,
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
    za = canonical_rows([row for row in PRODUCT_ROWS if row["market"] == "za"])
    entry = {
        "lane": "trend_analysis",
        "market": "za",
        "product_date": "2026-09-19",
        "state": "completed",
        "result_ref": result_ref(chain),
        "product_receipt_digest": canonical_digest(record),
        "output_digest": canonical_digest(za),
        "row_count": 2,
        "completed_at": "2026-09-20T02:00:00.000000Z",
        "available_at": "2026-09-20T02:00:01.000000Z",
        "reason_code": None,
    }
    rows = deepcopy(PRODUCT_ROWS)
    kwargs = {
        "derivation_id": derivation_id,
        "operation_id": "daily-1",
        "clients": clients,
        "read_completion": lambda operation_id: record if operation_id == "daily-1" else None,
        "read_product_rows": lambda lane, day: (
            rows if (lane, day) == ("trend_analysis", "2026-09-19") else []
        ),
    }
    return entry, record, rows, kwargs


def test_history_completion_binds_native_chain_completion_record_and_market_rows():
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        check_daily_completion_binding,
    )

    entry, record, _, kwargs = completion_case()
    checked = check_daily_completion_binding(entry, **kwargs)
    assert checked["completion"] == record
    assert [row["term"] for row in checked["rows"]] == ["load shedding", "rugby"]
    empty = dict(
        entry,
        market="ke",
        state="empty",
        row_count=0,
        output_digest=canonical_digest([]),
    )
    assert check_daily_completion_binding(empty, **kwargs)["rows"] == []


@pytest.mark.parametrize(
    "defect",
    [
        "unavailable_entry",
        "collection_chain",
        "product_date",
        "attempt",
        "image",
        "record_cutoff",
        "receipt_digest",
        "missing_record",
        "tampered_rows",
        "tampered_other_market",
        "market_count",
        "market_digest",
        "claimed_empty",
        "self_admitted_available",
        "completed_at",
        "unrecorded_lane",
        "rows_without_market",
        "forged_ref",
    ],
)
def test_history_completion_refuses_what_native_evidence_does_not_show(defect):
    from src.analysis.open_intelligence.source_estate_bridge_evidence import (
        check_daily_completion_binding,
    )

    case = {}
    if defect == "collection_chain":
        case["operation"] = "daily_source_collection"
    elif defect == "product_date":
        case["cutoff"] = "2026-09-19T00:00:00+00:00"
    entry, record, rows, kwargs = completion_case(**case)
    if defect == "unavailable_entry":
        entry.update(
            state="unavailable",
            result_ref=None,
            product_receipt_digest=None,
            output_digest=None,
            row_count=None,
            completed_at=None,
            available_at=None,
            reason_code="predecessor_missing",
        )
    elif defect == "attempt":
        record["business_attempt_id"] = "bat_" + "0" * 64
        entry["product_receipt_digest"] = canonical_digest(record)
    elif defect == "image":
        record["image_uri"] = "repo/other@sha256:" + "0" * 64
        entry["product_receipt_digest"] = canonical_digest(record)
    elif defect == "record_cutoff":
        record["cutoff_utc"] = "2026-09-21T00:00:00+00:00"
        entry["product_receipt_digest"] = canonical_digest(record)
    elif defect == "receipt_digest":
        entry["product_receipt_digest"] = "0" * 64
    elif defect == "missing_record":
        kwargs["read_completion"] = lambda _: None
    elif defect == "tampered_rows":
        rows[0]["term"] = "substituted"
    elif defect == "tampered_other_market":
        next(row for row in rows if row["market"] == "ng")["term"] = "substituted"
    elif defect == "market_count":
        entry["row_count"] = 3
    elif defect == "market_digest":
        entry["output_digest"] = "0" * 64
    elif defect == "claimed_empty":
        entry.update(state="empty", row_count=0, output_digest=canonical_digest([]))
    elif defect == "self_admitted_available":
        entry["available_at"] = "2026-09-20T02:00:00.000000Z"
    elif defect == "completed_at":
        entry["completed_at"] = "2026-09-20T01:59:00.000000Z"
    elif defect == "unrecorded_lane":
        entry["lane"] = "seed_graph"
        kwargs["read_product_rows"] = lambda lane, day: rows
    elif defect == "rows_without_market":
        from src.analysis.open_intelligence.daily_product_io import canonical_rows

        for row in rows:
            row.pop("market")
        digest = canonical_digest(canonical_rows(rows))
        record["products"]["trend_analysis"].update(output_digest=digest, readback_digest=digest)
        entry["product_receipt_digest"] = canonical_digest(record)
    else:
        entry["result_ref"]["result_digest"] = "0" * 64
    with pytest.raises(ValueError):
        check_daily_completion_binding(entry, **kwargs)


def test_empty_temporal_rule_set_refuses():
    source = artifacts()
    source["temporal_rules"]["rules"] = []
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        parse_bridge_artifacts(source)


@pytest.mark.parametrize("lane", ["event_ledger", "seed_graph", "trend_scores"])
@pytest.mark.parametrize("state", ["completed", "empty"])
def test_lanes_without_a_completion_record_can_only_be_unavailable(lane, state):
    from tests.unit.test_source_estate_bridge_contract import (
        check_profile,
        history_entry,
        profile,
        with_artifacts,
    )

    source = artifacts()
    entries = source["history_completion_set"]["entries"]
    index = next(
        i
        for i, row in enumerate(entries)
        if (row["lane"], row["market"], row["product_date"]) == (lane, "za", "2026-09-19")
    )
    entries[index] = history_entry(lane, "za", "2026-09-19", state)
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        parse_bridge_artifacts(source)
    with pytest.raises(ValueError, match=r"^source_bridge_artifacts_invalid$"):
        check_profile(with_artifacts(profile(), source), source)
