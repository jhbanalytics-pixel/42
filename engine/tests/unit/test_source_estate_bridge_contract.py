"""Pure contract fixtures. None of these synthetic records grants authority."""

import json
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.recurring_grant import _GRANT_ID
from src.analysis.open_intelligence.source_estate_bridge import (
    bridge_policy,
    validate_bridge_policy,
    validate_bridge_profile,
    validate_capture_facts,
    validate_current_output_set,
    validate_source_read_binding,
)

D = "2026-09-20"
C = "2026-09-21T00:00:00.000000Z"
A = "2026-09-21T00:20:00.000000Z"
CAPTURED = "2026-09-21T00:25:00.000000Z"
AVAILABLE = "2026-09-21T00:27:00.000000Z"
NOW = "2026-09-21T01:00:00.000000Z"
MAP = (
    ("enriched_content", "collection_evidence", "trends_v2_staging", A),
    ("event_ledger", "derived_history", "trends_v2_staging", C),
    ("raw_content", "collection_evidence", "trends_v2_staging", A),
    ("seed_candidates", "derived_history", "trends_v2_staging", C),
    ("seed_graph", "derived_history", "trends_v2_staging", C),
    ("trend_analysis", "derived_history", "trends_v2_staging", C),
    ("trend_scores", "derived_history", "trends_v2_staging", C),
)


def synthetic_digest(label):
    return canonical_digest({"unissued_test_fixture": label})


def policy():
    return {
        "contract_version": "42_source_estate_bridge_v1",
        "collection_dataset": "trends_v2_staging",
        "product_dataset": "trends_v2_staging",
        "snapshot_dataset": "trends_v2_staging",
        "collection_tables": ["enriched_content", "raw_content"],
        "history_tables": [
            "event_ledger",
            "seed_candidates",
            "seed_graph",
            "trend_analysis",
            "trend_scores",
        ],
        "market_scope": ["ke", "ng", "za"],
        "window_rule": "completed_run_availability_with_closed_collection_day_products_v1",
        "temporal_rules_version": "source_observation_semantics_v1",
        "history_read_rule": "prior_completed_products_as_of_cutoff_v1",
    }


ESTATE = synthetic_digest("connector inventory")
REASON_PENDING = "product_pending_at_cutoff"
REASON_UNRECORDED = "native_completion_unrecorded"


def synthetic_ref(label):
    return {
        "operation": "daily_source_collection",
        "consumption_id": "exc_" + synthetic_digest("consumption:" + label),
        "manifest_sha256": synthetic_digest("manifest:" + label),
        "result_id": "exr_" + synthetic_digest("result:" + label),
        "result_digest": synthetic_digest("result:" + label),
        "origin_registry_sha256": synthetic_digest("origin"),
        "resource_manifest_sha256": synthetic_digest("resources"),
    }


def history_entry(lane, market, day, state):
    if state == "unavailable":
        return {
            "lane": lane,
            "market": market,
            "product_date": day,
            "state": "unavailable",
            "result_ref": None,
            "product_receipt_digest": None,
            "output_digest": None,
            "row_count": None,
            "completed_at": None,
            "available_at": None,
            "reason_code": REASON_PENDING if day == D else REASON_UNRECORDED,
        }
    return {
        "lane": lane,
        "market": market,
        "product_date": day,
        "state": state,
        "result_ref": synthetic_ref("products:" + day),
        "product_receipt_digest": synthetic_digest("completion:" + day),
        "output_digest": synthetic_digest(f"rows:{lane}:{market}:{day}"),
        "row_count": 2 if state == "completed" else 0,
        "completed_at": "2026-09-20T02:00:00.000000Z",
        "available_at": "2026-09-20T02:00:01.000000Z",
        "reason_code": None,
    }


def history_entries():
    rows = []
    for lane in ("event_ledger", "seed_candidates", "seed_graph", "trend_analysis", "trend_scores"):
        for market in ("ke", "ng", "za"):
            for day in ("2026-09-19", D):
                state = "unavailable"
                if lane == "trend_analysis" and day == "2026-09-19":
                    state = "empty" if market == "ke" else "completed"
                rows.append(history_entry(lane, market, day, state))
    return rows


def bridge_artifacts():
    return {
        "bridge_policy": policy(),
        "temporal_rules": {
            "contract_version": "source_observation_semantics_v1",
            "rules": [
                {
                    "source_family": "news",
                    "endpoint": "articles",
                    "content_type": "article",
                    "time_basis": "native_event_instant",
                    "event_field": "published_at",
                    "missing_time_action": "withhold_event_time_claim",
                    "future_time_action": "withhold_event_time_claim",
                },
                {
                    "source_family": "social",
                    "endpoint": "mentions",
                    "content_type": "post",
                    "time_basis": "collection_observation",
                    "event_field": None,
                    "missing_time_action": "withhold_event_time_claim",
                    "future_time_action": "withhold_event_time_claim",
                },
            ],
        },
        "collection_receipt_set": {
            "contract_version": "42_bridge_collection_receipt_set_v1",
            "receipts": [
                {
                    "run_id": "run-20260920",
                    "result_ref": synthetic_ref("collection"),
                    "collection_receipt_digest": synthetic_digest("collection receipt"),
                    "collection_started_at": "2026-09-21T00:01:00.000000Z",
                    "collection_completed_at": "2026-09-21T00:15:00.000000Z",
                }
            ],
        },
        "history_completion_set": {
            "contract_version": "42_bridge_history_completion_set_v1",
            "entries": history_entries(),
        },
    }


def with_artifacts(value, artifacts):
    """Rebind a profile to changed artifacts, as an honest producer would."""
    value["temporal_rules_digest"] = canonical_digest(artifacts["temporal_rules"])
    value["collection_receipt_set_digest"] = canonical_digest(artifacts["collection_receipt_set"])
    value["history_completion_set_digest"] = canonical_digest(artifacts["history_completion_set"])
    return value


def profile():
    bindings = [
        {
            "lane": lane,
            "role": role,
            "source_table": f"ogilvy-trends-v2.{dataset}.{lane}",
            "destination_table": f"ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_20260920_{lane}",
            "snapshot_as_of": instant,
            "source_schema_digest": synthetic_digest(lane),
        }
        for lane, role, dataset, instant in MAP
    ]
    schema = [
        {name: row[name] for name in ("lane", "source_table", "source_schema_digest")}
        for row in bindings
    ]
    return {
        "profile_id": "staging_bridge_v3_20260920",
        "profile_version": "42_staging_source_bridge_v3",
        "projection_version": "native_id_bound_v1",
        "observation_window_end": C,
        "source_estate_digest": ESTATE,
        "grant_id": "unissued_test_grant",
        "schema_digest": canonical_digest(schema),
        "bridge_policy_digest": canonical_digest(policy()),
        "temporal_rules_version": "source_observation_semantics_v1",
        "temporal_rules_digest": canonical_digest(bridge_artifacts()["temporal_rules"]),
        "collection_snapshot_as_of": A,
        "history_snapshot_as_of": C,
        "collection_receipt_set_digest": canonical_digest(
            bridge_artifacts()["collection_receipt_set"]
        ),
        "history_completion_set_digest": canonical_digest(
            bridge_artifacts()["history_completion_set"]
        ),
        "relation_bindings": bindings,
    }


def _millis(value):
    return str(int(datetime.fromisoformat(value).timestamp() * 1000))


JOB_CREATED = "2026-09-21T00:21:00+00:00"
CLONE_CREATED = "2026-09-21T00:22:00+00:00"


def clone_resource(relation, **changes):
    """A tables.get resource for one clone, as the capture read it back. Synthetic."""
    project, dataset, table = relation["destination_table"].split(".")
    base_project, base_dataset, base_table = relation["source_table"].split(".")
    stamp = datetime.fromisoformat(relation["snapshot_as_of"])
    value = {
        "kind": "bigquery#table",
        "etag": "unissued-etag",
        "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
        "type": "SNAPSHOT",
        "snapshotDefinition": {
            "baseTableReference": {
                "projectId": base_project,
                "datasetId": base_dataset,
                "tableId": base_table,
            },
            "snapshotTime": stamp.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        },
        "creationTime": _millis(CLONE_CREATED),
        "lastModifiedTime": _millis(CLONE_CREATED),
        "numRows": "0",
        "numBytes": "0",
        "schema": {"fields": [{"name": "fixture_payload", "type": "STRING"}]},
    }
    value.update(changes)
    return value


def clone_fingerprint_digest(resource):
    """The recorded content fingerprint, pinned here independently of the validator."""
    return canonical_digest(
        {
            name: resource[name]
            for name in (
                "tableReference",
                "type",
                "snapshotDefinition",
                "creationTime",
                "numRows",
                "schema",
            )
        }
    )


def facts(p=None):
    p = profile() if p is None else p
    return {
        "contract_version": "open_intelligence_source_bridge_capture_v1",
        "profile_digest": canonical_digest(p),
        "relation_readbacks": [
            {
                "lane": row["lane"],
                "destination_table": row["destination_table"],
                "snapshot_as_of": row["snapshot_as_of"],
                "source_schema_digest": row["source_schema_digest"],
                "row_count": 0,
                "metadata_digest": clone_fingerprint_digest(clone_resource(row)),
            }
            for row in p["relation_bindings"]
        ],
        "collection_receipt_set_digest": p["collection_receipt_set_digest"],
        "history_completion_set_digest": p["history_completion_set_digest"],
        "projection_version": p["projection_version"],
        "temporal_rules_version": p["temporal_rules_version"],
        "temporal_rules_digest": p["temporal_rules_digest"],
        "captured_at": CAPTURED,
    }


def outputs(operation="operation-1"):
    return {
        "contract_version": "42_bridge_current_output_set_v1",
        "operation_id": operation,
        "outputs": [
            {
                "lane": "seed_graph",
                "write_job_reference": "projects/ogilvy-trends-v2/jobs/write-1",
                "write_job_digest": synthetic_digest("native write"),
                "natural_key_set_digest": synthetic_digest("keys"),
                "readback_digest": synthetic_digest("graph readback"),
            }
        ],
    }


def read_job(relation):
    """A jobs.get resource for one creation job. Synthetic and unissued."""
    project, dataset, table = relation["destination_table"].split(".")
    return {
        "jobReference": {
            "projectId": "ogilvy-trends-v2",
            "location": "US",
            "jobId": "capture_" + relation["lane"],
        },
        "status": {"state": "DONE"},
        "statistics": {
            "creationTime": _millis(JOB_CREATED),
            "endTime": _millis(CAPTURED),
            "query": {
                "ddlTargetTable": {"projectId": project, "datasetId": dataset, "tableId": table}
            },
        },
    }


def creation_records(p=None):
    p = profile() if p is None else p
    return [
        {
            **relation,
            "job_id": "capture_" + relation["lane"],
            "native_job_digest": canonical_digest(read_job(relation)),
            "state": "succeeded",
        }
        for relation in p["relation_bindings"]
    ]


def capture_payload(p=None, **changes):
    """A retained v3 capture result as the capture job recorded it, synthetic and unissued."""
    p = profile() if p is None else p
    value = {key: deepcopy(item) for key, item in p.items() if key != "schema_digest"}
    value.update(
        contract_version="open_intelligence_protected_source_snapshot_v3",
        cutoff_date=D,
        client_scope_id="ogilvy_default",
        market_scope=["ke", "ng", "za"],
        captured_at=CAPTURED,
        snapshot_plan_digest=synthetic_digest("plan"),
        snapshot_digest=canonical_digest(facts(p)),
        capture_receipt_digest=canonical_digest(creation_records(p)),
        creation_records=creation_records(p),
        artifact_attempt={},
        stored_artifact={},
        query_count=7,
        total_bytes_billed=0,
        limitations=["historical_predecessor_unavailable"],
        missing_checks=[],
    )
    value.update(changes)
    return value


PINS = ("consumption_id", "manifest_sha256", "result_digest", "result_id")
REGISTRY = {"path": None}


@pytest.fixture(autouse=True)
def _pinned_registry(tmp_path, monkeypatch):
    """Point the real registry loader at a file this test writes, as a pin commit would."""
    from src.analysis.open_intelligence import protected_context_registry

    path = tmp_path / "protected_context_registry_v1.json"
    monkeypatch.setattr(protected_context_registry, "REGISTRY_PATH", path)
    monkeypatch.setattr(protected_context_registry, "_cache", None)
    REGISTRY["path"] = path
    yield
    REGISTRY["path"] = None


def registry_row(cutoff=D, **pins):
    from src.analysis.open_intelligence.production_snapshot_tables import LANES

    day = date.fromisoformat(cutoff)
    return {
        **dict.fromkeys(PINS),
        **pins,
        "cutoff_date": cutoff,
        "market_scope": ["ke", "ng", "za"],
        "profile_id": f"protected_context_{day:%Y%m%d}_v1",
        "snapshot_tables": [
            f"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_{day:%Y%m%d}_{lane}"
            for lane in LANES
        ],
        "source_as_of": datetime.combine(day + timedelta(days=1), time.min, UTC).isoformat(),
    }


def write_registry(*rows):
    value = {
        "contract_version": "protected_context_registry_v1",
        "entries": sorted(rows, key=lambda row: row["cutoff_date"]),
        "registry_id": "protected_context_registry_v1",
    }
    REGISTRY["path"].write_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def pin(chain, **changes):
    """Commit the capture result's pins as the registry row for the cutoff."""
    write_registry(
        registry_row("2026-09-19"),
        registry_row(**{**{k: chain.result[k] for k in PINS}, **changes}),
    )


def pinned_row_digest(cutoff=D):
    """The digest a binding producer computes from the loaded registry row."""
    from src.analysis.open_intelligence.protected_context_registry import profile_for

    entry = profile_for(cutoff)
    return canonical_digest(
        {
            name: list(getattr(entry, name))
            if isinstance(getattr(entry, name), tuple)
            else getattr(entry, name)
            for name in registry_row()
        }
    )


def recurring_grant(p=None, **changes):
    from tests.unit.test_recurring_grant import grant

    p = profile() if p is None else p
    grant_id = p["grant_id"] if _GRANT_ID.fullmatch(p["grant_id"]) else "unissued_test_grant"
    return grant(
        **{"grant_id": grant_id, "source_policy_digest": p["source_estate_digest"], **changes}
    )


def capture_chain(
    p=None, *, payload=None, operation="daily_source_snapshot_capture", grant=None, **times
):
    from src.analysis.open_intelligence.recurring_grant import grant_digest

    from tests.unit.source_bridge_chain_fixture import daily_chain

    return daily_chain(
        grant_digest=grant_digest(recurring_grant(p) if grant is None else grant),
        operation=operation,
        cutoff_utc=times.get("cutoff_utc", "2026-09-21T00:00:00+00:00"),
        payload=capture_payload(p) if payload is None else payload,
        started=times.get("started", "2026-09-21T00:05:00+00:00"),
        completed_at=times.get("completed_at", "2026-09-21T00:26:50+00:00"),
        native_at=times.get("native_at", "2026-09-21T00:27:00+00:00"),
    )[2]


def read_inputs(p=None, **chain_changes):
    chain = capture_chain(p, **chain_changes)
    pin(chain)
    return {
        "profile": profile() if p is None else p,
        "artifacts": bridge_artifacts(),
        "capture_chain": chain,
        "grant": recurring_grant(p),
        "now": "2026-09-21T01:05:00.000000Z",
        **clone_inputs(p),
    }


def clone_inputs(p=None):
    """What the loader reads natively: the capture facts, the jobs and each clone table."""
    p = profile() if p is None else p
    return {
        "capture_facts": facts(p),
        "native_jobs": {"capture_" + row["lane"]: read_job(row) for row in p["relation_bindings"]},
        "clone_metadata": {
            row["destination_table"]: clone_resource(row) for row in p["relation_bindings"]
        },
    }


def binding(operation=None, inputs=None):
    p = profile()
    chain = (inputs or read_inputs())["capture_chain"]
    return {
        "contract_version": "source_bridge_read_binding_v1",
        "registry_entry_digest": pinned_row_digest(),
        "profile_id": p["profile_id"],
        "result_id": chain.result["result_id"],
        "result_digest": chain.result["result_digest"],
        "snapshot_digest": canonical_digest((inputs or {}).get("capture_facts", facts())),
        "request_as_of": NOW,
        "purpose": "current_context" if operation is None else "product_history",
        "product_date": None if operation is None else D,
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ng", "za"],
        "relation_bindings": p["relation_bindings"],
        "collection_receipt_set_digest": p["collection_receipt_set_digest"],
        "history_completion_set_digest": p["history_completion_set_digest"],
        "temporal_rules_version": p["temporal_rules_version"],
        "temporal_rules_digest": p["temporal_rules_digest"],
        "available_at": AVAILABLE,
        "current_operation_id": operation,
        "current_output_set_digest": None
        if operation is None
        else canonical_digest(outputs(operation)),
    }


def check_read(value, inputs=None, **kwargs):
    return validate_source_read_binding(value, **(inputs or read_inputs()), **kwargs)


def check_profile(value, artifacts=None, **kwargs):
    return validate_bridge_profile(
        value,
        cutoff_date=D,
        observed_at=NOW,
        artifacts=bridge_artifacts() if artifacts is None else artifacts,
        source_estate_digest=kwargs.pop("source_estate_digest", ESTATE),
        collection_completed_at=[A],
        **kwargs,
    )


def check_facts(value, *, p=None, artifacts=None, **kwargs):
    return validate_capture_facts(
        value,
        profile=profile() if p is None else p,
        artifacts=bridge_artifacts() if artifacts is None else artifacts,
        source_estate_digest=ESTATE,
        creation_completed_at={row[0]: "2026-09-21T00:24:00.000000Z" for row in MAP},
        **kwargs,
    )


def test_fixed_policy_is_exact_and_returned_data_is_detached():
    assert bridge_policy() == policy()
    assert validate_bridge_policy(canonical_bytes(policy())) == policy()
    copy = bridge_policy()
    copy["market_scope"].clear()
    assert bridge_policy() == policy()


@pytest.mark.parametrize("field", tuple(policy()))
def test_every_policy_field_is_fixed(field):
    value = policy()
    value[field] = [] if isinstance(value[field], list) else "unreviewed"
    with pytest.raises(ValueError):
        validate_bridge_policy(value)


@pytest.mark.parametrize("defect", ["duplicate", "whitespace", "bom"])
def test_noncanonical_json_never_becomes_a_valid_policy(defect):
    value = canonical_bytes(policy())
    if defect == "duplicate":
        value = b'{"contract_version":"42_source_estate_bridge_v1",' + value[1:]
    elif defect == "whitespace":
        value = b" " + value
    else:
        value = b"\xef\xbb\xbf" + value
    with pytest.raises(ValueError):
        validate_bridge_policy(value)


def test_valid_profile_preserves_separate_collection_and_history_instants():
    checked = check_profile(profile())
    assert checked == profile()
    assert {row["snapshot_as_of"] for row in checked["relation_bindings"]} == {A, C}
    checked["relation_bindings"].clear()
    assert len(check_profile(profile())["relation_bindings"]) == 7


@pytest.mark.parametrize(
    "defect",
    [
        "role",
        "source",
        "destination",
        "history_at_a",
        "collection_at_c",
        "five_lanes",
        "order",
        "schema_hash",
        "extra",
        "version",
        "projection",
        "temporal",
        "cutoff",
        "future_a",
        "before_c",
    ],
)
def test_profile_refuses_relation_and_semantic_drift(defect):
    value = profile()
    if defect == "role":
        value["relation_bindings"][0]["role"] = "derived_history"
    elif defect == "source":
        value["relation_bindings"][0]["source_table"] = (
            "ogilvy-trends-v2.intelligence_42_sources_staging.enriched_content"
        )
    elif defect == "destination":
        value["relation_bindings"][0]["destination_table"] += "_retry"
    elif defect == "history_at_a":
        value["relation_bindings"][1]["snapshot_as_of"] = A
    elif defect == "collection_at_c":
        value["relation_bindings"][0]["snapshot_as_of"] = C
    elif defect == "five_lanes":
        value["relation_bindings"] = value["relation_bindings"][:5]
    elif defect == "order":
        value["relation_bindings"].reverse()
    elif defect == "schema_hash":
        value["schema_digest"] = synthetic_digest("wrong schema")
    elif defect == "extra":
        value["approved"] = True
    elif defect == "version":
        value["profile_version"] = "42_staging_source_v2"
    elif defect == "projection":
        value["projection_version"] = "v3_absent_nullable_v1"
    elif defect == "temporal":
        value["temporal_rules_digest"] = "missing"
    elif defect == "cutoff":
        value["observation_window_end"] = "2026-09-22T00:00:00.000000Z"
    elif defect == "future_a":
        value["collection_snapshot_as_of"] = "2026-09-22T00:00:00.000000Z"
    else:
        value["collection_snapshot_as_of"] = "2026-09-20T23:00:00.000000Z"
    with pytest.raises(ValueError):
        check_profile(value)


def test_the_bridge_policy_digest_is_pinned():
    """Pinned literally, so a policy change moves this pin and every artifact that names it."""
    assert (
        canonical_digest(bridge_policy())
        == canonical_digest(policy())
        == "4a48f2ad3d1020090ea02e5adae8712651e4012ecc45849bf7b2d930ac51a807"
    )


@pytest.mark.parametrize("lane", ["enriched_content", "raw_content"])
@pytest.mark.parametrize(
    "dataset", ["intelligence_42_sources_staging", "trends_v2_staging_qa", "trends_v2_dev"]
)
def test_collection_lanes_copy_only_from_the_pilot_product_dataset(lane, dataset):
    """The funded pilot writes raw_content and enriched_content to trends_v2_staging."""
    value = profile()
    relation = next(row for row in value["relation_bindings"] if row["lane"] == lane)
    assert relation["source_table"] == f"ogilvy-trends-v2.trends_v2_staging.{lane}"
    relation["source_table"] = f"ogilvy-trends-v2.{dataset}.{lane}"
    with pytest.raises(ValueError, match=r"^source_bridge_relation_differs$"):
        check_profile(value)


def test_collection_completion_and_observed_clock_are_compared_in_utc():
    validate_bridge_profile(
        profile(),
        cutoff_date=D,
        observed_at=datetime(2026, 9, 21, 3, tzinfo=UTC) - timedelta(hours=2),
        artifacts=bridge_artifacts(),
        source_estate_digest=ESTATE,
        collection_completed_at=["2026-09-21T02:20:00+02:00"],
    )
    with pytest.raises(ValueError):
        validate_bridge_profile(
            profile(),
            cutoff_date=D,
            observed_at=NOW,
            artifacts=bridge_artifacts(),
            source_estate_digest=ESTATE,
            collection_completed_at=[CAPTURED],
        )


def test_r1_pre_storage_facts_and_post_storage_time_binding():
    assert check_facts(facts()) == facts()
    assert (
        check_facts(
            facts(),
            stored_created_at="2026-09-21T00:26:00.000000Z",
            result_captured_at=CAPTURED,
            attempt_captured_at=CAPTURED,
        )
        == facts()
    )


@pytest.mark.parametrize(
    "defect",
    [
        "before_a",
        "before_completion",
        "result_time",
        "attempt_time",
        "storage_time",
        "partial_storage",
        "profile_digest",
        "readback_role_time",
        "missing_lane",
        "bool_count",
        "extra",
    ],
)
def test_r1_refuses_changed_or_impossible_capture_facts(defect):
    value, kwargs = facts(), {}
    if defect == "before_a":
        value["captured_at"] = C
    elif defect == "before_completion":
        value["captured_at"] = "2026-09-21T00:23:00.000000Z"
    elif defect in {"result_time", "attempt_time", "storage_time", "partial_storage"}:
        kwargs = {
            "stored_created_at": AVAILABLE,
            "result_captured_at": CAPTURED,
            "attempt_captured_at": CAPTURED,
        }
        if defect == "result_time":
            kwargs["result_captured_at"] = AVAILABLE
        elif defect == "attempt_time":
            kwargs["attempt_captured_at"] = AVAILABLE
        elif defect == "storage_time":
            kwargs["stored_created_at"] = A
        else:
            del kwargs["result_captured_at"]
    elif defect == "profile_digest":
        value["profile_digest"] = synthetic_digest("another profile")
    elif defect == "readback_role_time":
        value["relation_readbacks"][1]["snapshot_as_of"] = A
    elif defect == "missing_lane":
        value["relation_readbacks"].pop()
    elif defect == "bool_count":
        value["relation_readbacks"][0]["row_count"] = True
    else:
        value["authority"] = "self-issued"
    with pytest.raises(ValueError):
        check_facts(value, **kwargs)


def test_r2_intermediate_graph_can_bind_without_future_composition_result():
    current = outputs()
    assert validate_current_output_set(current, operation_id="operation-1") == current
    checked = check_read(
        binding("operation-1"), operation_id="operation-1", current_output_set=current
    )
    assert checked["current_output_set_digest"] == canonical_digest(current)
    assert "composition_result" not in checked


def test_r2_operation_and_partial_output_changes_change_query_identity():
    first = check_read(
        binding("operation-1"), operation_id="operation-1", current_output_set=outputs()
    )
    second = check_read(
        binding("operation-2"),
        operation_id="operation-2",
        current_output_set=outputs("operation-2"),
    )
    assert canonical_digest(first) != canonical_digest(second)
    changed = outputs()
    changed["outputs"][0]["readback_digest"] = synthetic_digest("new readback")
    updated = binding("operation-1")
    updated["current_output_set_digest"] = canonical_digest(changed)
    third = check_read(updated, operation_id="operation-1", current_output_set=changed)
    assert canonical_digest(third) != canonical_digest(first)


@pytest.mark.parametrize(
    "defect",
    [
        "operation",
        "set_hash",
        "pair",
        "foreign_set",
        "duplicate",
        "collection_lane",
        "extra_output",
        "future_result",
    ],
)
def test_r2_refuses_overlay_substitution(defect):
    value, current = binding("operation-1"), outputs()
    if defect == "operation":
        value["current_operation_id"] = "operation-2"
    elif defect == "set_hash":
        value["current_output_set_digest"] = synthetic_digest("other output set")
    elif defect == "pair":
        value["current_operation_id"] = None
    elif defect == "foreign_set":
        current["operation_id"] = "operation-2"
    elif defect == "duplicate":
        current["outputs"].append(deepcopy(current["outputs"][0]))
    elif defect == "collection_lane":
        current["outputs"][0]["lane"] = "raw_content"
    elif defect == "extra_output":
        current["outputs"][0]["approved"] = True
    else:
        current["composition_result"] = "not-produced-yet"
    with pytest.raises(ValueError):
        check_read(value, operation_id="operation-1", current_output_set=current)


@pytest.mark.parametrize(
    "defect",
    [
        "early_question",
        "before_a_available",
        "scope",
        "date",
        "context_overlay",
        "relation",
        "projection_extra",
        "result_id",
    ],
)
def test_read_binding_refuses_scope_time_and_shape_drift(defect):
    value = binding()
    if defect == "early_question":
        value["request_as_of"] = A
    elif defect == "before_a_available":
        value["available_at"] = C
    elif defect == "scope":
        value["market_scope"] = ["us"]
    elif defect == "date":
        value["product_date"] = D
    elif defect == "context_overlay":
        value.update(
            current_operation_id="operation-1",
            current_output_set_digest=canonical_digest(outputs()),
        )
    elif defect == "relation":
        value["relation_bindings"][1]["snapshot_as_of"] = A
    elif defect == "projection_extra":
        value["projection_version"] = "legacy"
    else:
        value["result_id"] = "invented"
    with pytest.raises(ValueError):
        check_read(value)


def test_clone_only_current_context_has_no_ambient_operation():
    assert check_read(binding()) == binding()
    with pytest.raises(ValueError):
        check_read(binding(), operation_id="operation-1", current_output_set=outputs())


@pytest.mark.parametrize("purpose", [[], {}])
def test_read_purpose_wrong_json_type_is_a_contract_refusal(purpose):
    value = binding()
    value["purpose"] = purpose
    with pytest.raises(ValueError):
        check_read(value)


def _grant_entrypoint(entrypoint, grant_id):
    value = profile()
    value["grant_id"] = grant_id
    if entrypoint == "profile":
        return check_profile(value)
    if entrypoint == "capture":
        capture = facts()
        capture["profile_digest"] = canonical_digest(value)
        return check_facts(capture, p=value)
    inputs = read_inputs(value)
    return check_read(binding(inputs=inputs), inputs)


@pytest.mark.parametrize("entrypoint", ["profile", "capture", "read_binding"])
@pytest.mark.parametrize(
    "grant_id", ["UPPER", "grant-with-hyphen", "grant with space", "grant!name"]
)
def test_every_bridge_entrypoint_rejects_invalid_grant_grammar(entrypoint, grant_id):
    with pytest.raises(ValueError, match=r"^source_bridge_grant_name_invalid$"):
        _grant_entrypoint(entrypoint, grant_id)


@pytest.mark.parametrize("entrypoint", ["profile", "capture", "read_binding"])
def test_every_bridge_entrypoint_retains_lowercase_digit_underscore_grants(entrypoint):
    assert _grant_entrypoint(entrypoint, "unissued_test_grant_42")


def _check_nested_record(kind, value):
    if kind == "profile":
        return check_profile(value)
    if kind == "capture":
        return check_facts(value)
    return validate_current_output_set(value, operation_id="operation-1")


@pytest.mark.parametrize(
    "kind,field",
    [("profile", "relation_bindings"), ("capture", "relation_readbacks"), ("outputs", "outputs")],
)
@pytest.mark.parametrize("encoding", ["string", "bytes"])
def test_nested_bridge_records_require_objects(kind, field, encoding):
    value = {"profile": profile, "capture": facts, "outputs": outputs}[kind]()
    encoded = canonical_bytes(value[field][0])
    value[field][0] = encoded.decode("utf-8") if encoding == "string" else encoded
    with pytest.raises(ValueError):
        _check_nested_record(kind, value)


@pytest.mark.parametrize("kind", ["profile", "capture", "outputs"])
@pytest.mark.parametrize("encoding", ["string", "bytes"])
def test_top_level_bridge_json_remains_supported(kind, encoding):
    value = {"profile": profile, "capture": facts, "outputs": outputs}[kind]()
    encoded = canonical_bytes(value)
    supplied = encoded.decode("utf-8") if encoding == "string" else encoded
    assert _check_nested_record(kind, supplied) == value


def test_read_binding_cannot_retain_matching_serialized_relations():
    p = profile()
    value = binding()
    encoded = canonical_bytes(p["relation_bindings"][0]).decode("utf-8")
    p["relation_bindings"][0] = encoded
    value["relation_bindings"][0] = encoded
    capture = facts()
    capture["profile_digest"] = canonical_digest(p)
    value["snapshot_digest"] = canonical_digest(capture)
    with pytest.raises(ValueError):
        check_read(value, dict(read_inputs(), profile=p))


def test_overlay_cannot_retain_serialized_outputs_with_a_matching_digest():
    current = outputs()
    current["outputs"][0] = canonical_bytes(current["outputs"][0]).decode("utf-8")
    value = binding("operation-1")
    value["current_output_set_digest"] = canonical_digest(current)
    with pytest.raises(ValueError):
        check_read(value, operation_id="operation-1", current_output_set=current)


@pytest.mark.parametrize(
    "artifact", ["temporal_rules", "collection_receipt_set", "history_completion_set"]
)
def test_profile_artifact_digests_are_recomputed_from_the_artifact_bytes(artifact):
    changed = bridge_artifacts()
    if artifact == "temporal_rules":
        changed["temporal_rules"]["rules"][0]["endpoint"] = "unreviewed"
    elif artifact == "collection_receipt_set":
        changed["collection_receipt_set"]["receipts"][0]["run_id"] = "run-substituted"
    else:
        changed["history_completion_set"]["entries"][0]["reason_code"] = "substituted"
    with pytest.raises(ValueError, match=r"^source_bridge_artifact_digest_differs$"):
        check_profile(profile(), changed)
    assert check_profile(with_artifacts(profile(), changed), changed)


def test_profile_source_estate_digest_is_bound_to_the_independent_grant_value():
    with pytest.raises(ValueError, match=r"^source_bridge_estate_differs$"):
        check_profile(profile(), source_estate_digest=synthetic_digest("another estate"))


@pytest.mark.parametrize("entrypoint", ["profile", "capture"])
def test_capture_after_every_receipted_collection_is_mandatory(entrypoint):
    changed = bridge_artifacts()
    changed["collection_receipt_set"]["receipts"][0]["collection_completed_at"] = (
        "2026-09-21T00:21:00.000000Z"
    )
    value = with_artifacts(profile(), changed)
    capture = facts()
    capture.update(
        profile_digest=canonical_digest(value),
        collection_receipt_set_digest=value["collection_receipt_set_digest"],
    )

    def entry():
        if entrypoint == "profile":
            return validate_bridge_profile(
                value,
                cutoff_date=D,
                observed_at=NOW,
                artifacts=changed,
                source_estate_digest=ESTATE,
            )
        return check_facts(capture, p=value, artifacts=changed)

    with pytest.raises(ValueError, match=r"^source_bridge_capture_precedes_collection$"):
        entry()


@pytest.mark.parametrize("defect", ["available_after_clone", "future_date", "stops_early"])
def test_history_completion_set_is_bounded_by_the_history_clone(defect):
    changed = bridge_artifacts()
    entries = changed["history_completion_set"]["entries"]
    if defect == "available_after_clone":
        index = next(i for i, row in enumerate(entries) if row["state"] == "completed")
        entries[index]["available_at"] = "2026-09-21T00:00:00.000001Z"
    elif defect == "future_date":
        entries[:] = [
            history_entry(row["lane"], row["market"], day, "unavailable")
            for row in entries[::2]
            for day in (D, "2026-09-21")
        ]
    else:
        entries[:] = [row for row in entries if row["product_date"] != D]
    with pytest.raises(ValueError, match=r"^source_bridge_history_window_invalid$"):
        check_profile(with_artifacts(profile(), changed), changed)


@pytest.mark.parametrize(
    "defect",
    [
        "result_id_not_digest",
        "result_digest",
        "registry_digest",
        "chain_only_digest",
        "registry_unpinned",
        "registry_other_result",
        "registry_absent",
        "registry_markets",
        "snapshot_digest",
        "self_admitted_available",
        "future_request",
        "client_scope",
        "market_outside_capture",
        "collection_chain",
        "unsealed_chain",
        "chain_cutoff",
        "payload_profile",
        "payload_missing_checks",
        "grant_not_authorizing",
        "grant_estate",
        "grant_id",
        "grant_without_capture",
    ],
)
def test_read_binding_is_bound_to_native_result_registry_and_clock(defect):
    inputs, value = read_inputs(), None
    chain = inputs["capture_chain"]
    if defect == "result_id_not_digest":
        value = binding(inputs=inputs)
        value["result_id"] = "exr_" + synthetic_digest("another result")
    elif defect == "result_digest":
        value = binding(inputs=inputs)
        value["result_digest"] = synthetic_digest("another result")
    elif defect == "registry_digest":
        value = binding(inputs=inputs)
        value["registry_entry_digest"] = synthetic_digest("registry entry")
    elif defect == "chain_only_digest":
        value = binding(inputs=inputs)
        value["registry_entry_digest"] = canonical_digest(
            {"profile_id": "staging_bridge_v3_20260920", "cutoff_date": D}
            | {name: chain.result[name] for name in PINS}
        )
    elif defect == "registry_unpinned":
        write_registry(registry_row())
        value = binding(inputs=inputs)
    elif defect == "registry_other_result":
        pin(chain, result_digest=synthetic_digest("another result"))
        value = binding(inputs=inputs)
    elif defect == "registry_absent":
        value = binding(inputs=inputs)
        write_registry(registry_row("2026-09-19", **{name: chain.result[name] for name in PINS}))
    elif defect == "registry_markets":
        write_registry(
            {**registry_row(**{name: chain.result[name] for name in PINS}), "market_scope": ["za"]}
        )
        value = binding(inputs=inputs)
    elif defect == "snapshot_digest":
        value = binding(inputs=inputs)
        value["snapshot_digest"] = synthetic_digest("another capture")
    elif defect == "self_admitted_available":
        value = binding(inputs=inputs)
        value["available_at"] = "2026-09-21T00:26:00.000000Z"
    elif defect == "future_request":
        inputs["now"] = "2026-09-21T00:59:59.000000Z"
    elif defect == "client_scope":
        value = binding(inputs=inputs)
        value["client_scope_id"] = "foreign_client"
    elif defect == "market_outside_capture":
        inputs["capture_chain"] = capture_chain(payload=capture_payload(market_scope=["za"]))
    elif defect == "collection_chain":
        inputs["capture_chain"] = capture_chain(operation="daily_source_collection")
    elif defect == "unsealed_chain":
        from types import SimpleNamespace

        inputs["capture_chain"] = SimpleNamespace(
            **{
                name: getattr(chain, name)
                for name in ("result", "operation_context", "operation_payload")
            },
            effective_available_at=chain.effective_available_at,
        )
    elif defect == "chain_cutoff":
        inputs["capture_chain"] = capture_chain(cutoff_utc="2026-09-20T00:00:00+00:00")
    elif defect == "payload_profile":
        inputs["capture_chain"] = capture_chain(
            payload=capture_payload(source_estate_digest=synthetic_digest("another estate"))
        )
    elif defect == "payload_missing_checks":
        inputs["capture_chain"] = capture_chain(
            payload=capture_payload(missing_checks=["native_readback"])
        )
    elif defect == "grant_not_authorizing":
        inputs["grant"] = recurring_grant(resource_manifest_digest=synthetic_digest("other"))
    elif defect == "grant_estate":
        inputs["grant"] = recurring_grant(source_policy_digest=synthetic_digest("other estate"))
        inputs["capture_chain"] = capture_chain(grant=inputs["grant"])
    elif defect == "grant_id":
        inputs["grant"] = recurring_grant(grant_id="another_test_grant")
        inputs["capture_chain"] = capture_chain(grant=inputs["grant"])
    else:
        from src.analysis.open_intelligence.recurring_grant import ALLOWED_OPERATIONS

        inputs["grant"] = recurring_grant(
            allowed_operations=[op for op in ALLOWED_OPERATIONS if op != "immutable_capture"]
        )
        inputs["capture_chain"] = capture_chain(grant=inputs["grant"])
    if inputs["capture_chain"] is not chain and defect != "unsealed_chain":
        pin(inputs["capture_chain"])
    if value is None:
        value = binding(inputs=inputs)
    with pytest.raises(ValueError):
        check_read(value, inputs)


def test_read_binding_accepts_the_registry_pin_and_refuses_it_once_removed():
    inputs = read_inputs()
    value = binding(inputs=inputs)
    assert check_read(value, inputs) == value
    write_registry(registry_row("2026-09-19"), registry_row())
    with pytest.raises(ValueError, match=r"^source_bridge_registry_entry_differs$"):
        check_read(value, inputs)


def test_read_binding_availability_is_the_native_completion_instant():
    inputs = read_inputs(completed_at="2026-09-21T00:27:30+00:00")
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_available_time_differs$"):
        check_read(value, inputs)
    value["available_at"] = "2026-09-21T00:27:30.000000Z"
    assert check_read(value, inputs) == value


def _pins(chain):
    return {name: chain.result[name] for name in PINS}


def test_same_result_pinned_at_two_cutoffs_refuses():
    inputs = read_inputs()
    value = binding(inputs=inputs)
    pins = _pins(inputs["capture_chain"])
    write_registry(registry_row("2026-09-19", **pins), registry_row(**pins))
    value["registry_entry_digest"] = pinned_row_digest()
    with pytest.raises(ValueError, match=r"^source_bridge_registry_entry_differs$"):
        check_read(value, inputs)


def test_same_result_pinned_at_its_cutoff_and_a_later_one_refuses():
    inputs = read_inputs()
    value = binding(inputs=inputs)
    pins = _pins(inputs["capture_chain"])
    write_registry(registry_row(**pins), registry_row("2026-09-21", **pins))
    value["registry_entry_digest"] = pinned_row_digest()
    with pytest.raises(ValueError, match=r"^source_bridge_registry_entry_differs$"):
        check_read(value, inputs)


def test_result_pinned_at_another_cutoff_refuses():
    inputs = read_inputs()
    value = binding(inputs=inputs)
    write_registry(registry_row("2026-09-19", **_pins(inputs["capture_chain"])), registry_row())
    value["registry_entry_digest"] = pinned_row_digest("2026-09-19")
    with pytest.raises(ValueError, match=r"^source_bridge_registry_entry_differs$"):
        check_read(value, inputs)


@pytest.mark.parametrize(
    "defect",
    [
        "replaced_after_job",
        "created_before_job",
        "not_snapshot",
        "other_base",
        "other_snapshot_time",
        "other_reference",
        "row_count",
        "fingerprint_only",
        "missing_clone",
        "extra_clone",
        "job_changed",
        "job_missing",
        "facts_changed",
        "facts_readback_changed",
    ],
)
def test_loader_reads_back_every_clone_and_refuses_a_replaced_table(defect):
    inputs = read_inputs()
    value = binding(inputs=inputs)
    relations = profile()["relation_bindings"]
    table = relations[3]["destination_table"]
    clones = inputs["clone_metadata"]
    if defect == "replaced_after_job":
        clones[table]["creationTime"] = _millis("2026-09-21T00:40:00+00:00")
    elif defect == "created_before_job":
        clones[table]["creationTime"] = _millis("2026-09-21T00:20:30+00:00")
    elif defect == "not_snapshot":
        clones[table]["type"] = "TABLE"
    elif defect == "other_base":
        clones[table]["snapshotDefinition"]["baseTableReference"]["datasetId"] = "trends_v2_dev"
    elif defect == "other_snapshot_time":
        clones[table]["snapshotDefinition"]["snapshotTime"] = "2026-09-21T00:00:01.000Z"
    elif defect == "other_reference":
        clones[table]["tableReference"]["tableId"] += "_other"
    elif defect == "row_count":
        clones[table]["numRows"] = "5"
    elif defect == "fingerprint_only":
        clones[table]["schema"] = {"fields": [{"name": "replaced", "type": "STRING"}]}
    elif defect == "missing_clone":
        del clones[table]
    elif defect == "extra_clone":
        clones[table + "_extra"] = deepcopy(clones[table])
    elif defect == "job_changed":
        inputs["native_jobs"]["capture_" + relations[3]["lane"]]["statistics"]["endTime"] = _millis(
            "2026-09-21T00:45:00+00:00"
        )
    elif defect == "job_missing":
        del inputs["native_jobs"]["capture_" + relations[3]["lane"]]
    elif defect == "facts_changed":
        inputs["capture_facts"]["captured_at"] = "2026-09-21T00:26:00.000000Z"
    else:
        inputs["capture_facts"]["relation_readbacks"][3]["row_count"] = 5
    with pytest.raises(ValueError):
        check_read(value, inputs)


def test_clone_readback_accepts_volatile_table_metadata():
    inputs = read_inputs()
    value = binding(inputs=inputs)
    for resource in inputs["clone_metadata"].values():
        resource.update(etag="later-etag", lastModifiedTime=_millis("2026-09-21T01:00:00+00:00"))
    assert check_read(value, inputs) == value


def recorded_inputs(change_clone=None, change_facts=None):
    """Inputs whose capture recorded the changed clone, so only the named check can refuse."""
    inputs = read_inputs()
    p = inputs["profile"]
    clones = inputs["clone_metadata"]
    capture = inputs["capture_facts"]
    table = p["relation_bindings"][3]["destination_table"]
    if change_clone:
        change_clone(clones[table])
    for readback in capture["relation_readbacks"]:
        readback["metadata_digest"] = clone_fingerprint_digest(
            clones[readback["destination_table"]]
        )
    if change_facts:
        change_facts(capture)
    chain = capture_chain(payload=capture_payload(snapshot_digest=canonical_digest(capture)))
    pin(chain)
    inputs["capture_chain"] = chain
    return inputs


def _set(path, value):
    def change(resource):
        target = resource
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return change


@pytest.mark.parametrize(
    "defect",
    [
        "type",
        "base",
        "snapshot_time",
        "created_before_job",
        "created_after_job",
        "row_count",
    ],
)
def test_clone_checks_hold_even_when_the_capture_recorded_the_bad_table(defect):
    change = {
        "type": _set(["type"], "TABLE"),
        "base": _set(["snapshotDefinition", "baseTableReference", "datasetId"], "trends_v2_dev"),
        "snapshot_time": _set(["snapshotDefinition", "snapshotTime"], "2026-09-21T00:00:01.000Z"),
        "created_before_job": _set(["creationTime"], _millis("2026-09-21T00:20:30+00:00")),
        "created_after_job": _set(["creationTime"], _millis("2026-09-21T00:40:00+00:00")),
        "row_count": _set(["numRows"], "5"),
    }[defect]
    inputs = recorded_inputs(change)
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


def test_a_clone_that_carries_an_expiry_is_refused_at_read():
    """The plan never sets an expiry, so a clone with one is not the planned clone."""
    stamp = datetime.fromisoformat(CLONE_CREATED) + timedelta(days=90)
    inputs = recorded_inputs(_set(["expirationTime"], _millis(stamp.isoformat())))
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


def test_the_clone_fingerprint_refuses_a_clone_with_an_expiry():
    from src.analysis.open_intelligence.source_estate_bridge import clone_fingerprint

    relation = profile()["relation_bindings"][3]
    resource = clone_resource(relation, expirationTime=_millis(CLONE_CREATED))
    with pytest.raises(ValueError, match=r"^source_bridge_clone_invalid$"):
        clone_fingerprint(resource)
    assert "expirationTime" not in clone_fingerprint(clone_resource(relation))


def test_recorded_inputs_without_a_change_are_admitted():
    inputs = recorded_inputs()
    value = binding(inputs=inputs)
    assert check_read(value, inputs) == value


def test_a_consistent_clone_and_facts_substitution_is_refused_by_the_retained_digest():
    inputs = read_inputs()
    value = binding(inputs=inputs)
    table = inputs["profile"]["relation_bindings"][3]["destination_table"]
    inputs["clone_metadata"][table]["numRows"] = "5"
    readback = inputs["capture_facts"]["relation_readbacks"][3]
    readback.update(
        row_count=5,
        metadata_digest=clone_fingerprint_digest(inputs["clone_metadata"][table]),
    )
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


def test_a_changed_native_job_is_refused_by_its_recorded_digest():
    inputs = read_inputs()
    value = binding(inputs=inputs)
    job = inputs["native_jobs"]["capture_" + inputs["profile"]["relation_bindings"][3]["lane"]]
    job["status"] = {"state": "DONE", "errorResult": {"reason": "invalidQuery"}}
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


def retained_inputs(change):
    """Inputs whose retained payload carries changed creation records; all else consistent."""
    inputs = read_inputs()
    records = creation_records()
    change(records, inputs)
    chain = capture_chain(payload=capture_payload(creation_records=records))
    pin(chain)
    inputs["capture_chain"] = chain
    return inputs


def _one_record_short(records, inputs):
    records.pop()


def _record_failed(records, inputs):
    records[3]["state"] = "failed"


def _record_other_table(records, inputs):
    records[3]["destination_table"] += "_other"


def _record_other_role(records, inputs):
    roles = {"collection_evidence": "derived_history", "derived_history": "collection_evidence"}
    records[3]["role"] = roles[records[3]["role"]]


def _record_other_source(records, inputs):
    records[3]["source_table"] += "_other"


def _record_other_instant(records, inputs):
    records[3]["snapshot_as_of"] = "2026-09-19T00:00:00.000000Z"


def _record_other_schema(records, inputs):
    records[3]["source_schema_digest"] = synthetic_digest("another source schema")


def _record_other_lane(records, inputs):
    records[3]["lane"] = records[4]["lane"]


def _job_not_an_object(records, inputs):
    forged = ["not", "a", "job"]
    inputs["native_jobs"][records[3]["job_id"]] = forged
    records[3]["native_job_digest"] = canonical_digest(forged)


def _job_without_reference(records, inputs):
    job = inputs["native_jobs"][records[3]["job_id"]]
    del job["jobReference"]
    records[3]["native_job_digest"] = canonical_digest(job)


def _record_names_another_job(records, inputs):
    old = records[3]["job_id"]
    records[3]["job_id"] = "other_job"
    inputs["native_jobs"]["other_job"] = inputs["native_jobs"].pop(old)


@pytest.mark.parametrize(
    "change",
    [
        _one_record_short,
        _record_failed,
        _record_other_table,
        _record_other_role,
        _record_other_source,
        _record_other_instant,
        _record_other_schema,
        _record_other_lane,
        _record_names_another_job,
        _job_not_an_object,
        _job_without_reference,
    ],
)
def test_retained_creation_records_must_match_the_profile_and_their_jobs(change):
    inputs = retained_inputs(change)
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


def test_a_renamed_native_job_key_refuses_with_the_code():
    inputs = read_inputs()
    jobs = inputs["native_jobs"]
    jobs["renamed"] = jobs.pop(sorted(jobs)[0])
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


def test_a_clone_recorded_under_another_reference_refuses():
    inputs = recorded_inputs(_set(["tableReference", "tableId"], "some_other_table"))
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


def test_capture_before_a_job_ended_refuses_at_read():
    inputs = read_inputs()
    for job in inputs["native_jobs"].values():
        job["statistics"]["endTime"] = _millis("2026-09-21T00:26:00+00:00")
    records = creation_records()
    for record in records:
        record["native_job_digest"] = canonical_digest(inputs["native_jobs"][record["job_id"]])
    chain = capture_chain(payload=capture_payload(creation_records=records))
    pin(chain)
    inputs["capture_chain"] = chain
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_capture_precedes_creation$"):
        check_read(value, inputs)


def test_payload_capture_time_must_equal_the_facts():
    inputs = read_inputs()
    chain = capture_chain(payload=capture_payload(captured_at="2026-09-21T00:25:30.000000Z"))
    pin(chain)
    inputs["capture_chain"] = chain
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


def _swap_lanes(records, inputs):
    """Records 3 and 4 trade job ids and digests, so each names the other lane's job."""
    third, fourth = records[3], records[4]
    third["job_id"], fourth["job_id"] = fourth["job_id"], third["job_id"]
    third["native_job_digest"], fourth["native_job_digest"] = (
        fourth["native_job_digest"],
        third["native_job_digest"],
    )


def _job_targets_another_table(records, inputs):
    job = inputs["native_jobs"][records[3]["job_id"]]
    job["statistics"]["query"]["ddlTargetTable"]["tableId"] += "_other"
    records[3]["native_job_digest"] = canonical_digest(job)


def _job_without_target(records, inputs):
    job = inputs["native_jobs"][records[3]["job_id"]]
    del job["statistics"]["query"]
    records[3]["native_job_digest"] = canonical_digest(job)


@pytest.mark.parametrize("change", [_swap_lanes, _job_targets_another_table, _job_without_target])
def test_each_creation_job_must_have_created_its_own_lanes_clone(change):
    inputs = retained_inputs(change)
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)


@pytest.mark.parametrize("field", ["schema", "numRows", "snapshotDefinition"])
def test_a_clone_missing_a_fingerprint_field_refuses_with_the_read_code(field):
    inputs = read_inputs()
    table = inputs["profile"]["relation_bindings"][3]["destination_table"]
    del inputs["clone_metadata"][table][field]
    value = binding(inputs=inputs)
    with pytest.raises(ValueError, match=r"^source_bridge_clone_differs$"):
        check_read(value, inputs)
