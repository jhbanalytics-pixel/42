import copy
import importlib
import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_snapshot")


def query_receipt(m, template_id, rows, marker):
    return {
        "template_id": template_id,
        "sql_digest": marker * 64,
        "parameters_digest": marker.upper() * 64,
        "query_state": "succeeded",
        "result_digest": m.canonical_digest(rows),
    }


def fact():
    return {
        "row_id": "row",
        "market": "za",
        "excerpt": "Synthetic excerpt",
        "source_label": "Synthetic source",
        "author": None,
        "url": None,
        "platform": "forum",
        "published_at": datetime(2026, 9, 3, tzinfo=UTC),
        "collected_at": None,
        "source_family": "forum",
        "source_run_id": "run",
        "projection_sha256": "7" * 64,
    }


def test_snapshot_module_exposes_fixed_builder():
    assert callable(module().build_general_question_snapshot)


class Objects:
    def __init__(self, plan):
        self.values = {"requests/request/snapshot.json": None}
        self.plan = plan
        self.writes = 0

    def read(self, key, generation=None):
        if key.endswith("plan.json"):
            return SimpleNamespace(value=self.plan, generation=1)
        value = self.values.get(key)
        return None if value is None else SimpleNamespace(value=value, generation=2)

    def create(self, key, value):
        existing = self.values.get(key)
        if existing is not None and existing != value:
            raise ValueError("conflict")
        if existing is None:
            self.values[key] = value
            self.writes += 1
        return SimpleNamespace(value=value, generation=2)


def fixture(monkeypatch, discovery_status="accepted"):
    from src.analysis.open_intelligence import general_question_context_admission

    monkeypatch.setattr(general_question_context_admission, "_PROTECTED_CONTEXT_PROFILES", ())
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setattr(protected_context_registry, "_PROTECTED_CONTEXT_PROFILES", ())
    # No registered context at all: a committed bridge row would open the context route too.
    # A caller that stubbed its own bridge rows before building this fixture keeps them.
    if protected_context_registry.bridge_entries.__module__ == protected_context_registry.__name__:
        monkeypatch.setattr(protected_context_registry, "bridge_entries", lambda: ())
    m = module()
    plan = {
        "plan_digest": "1" * 64,
        "markets": ["za"],
        "window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True},
        "requirements": [
            {
                "requirement_id": "content",
                "question": "What do the synthetic records show?",
                "kind": "content",
                "mandatory": True,
                "search_terms": ["synthetic"],
            }
        ],
    }
    request = {
        "request_id": "request",
        "request_digest": "2" * 64,
        "as_of": "2026-09-06T20:00:00.000000Z",
    }
    context = {
        "request": request,
        "intake": {"intake_digest": "3" * 64},
        "admission": {
            "policy_digest": "4" * 64,
            "deployment_digest": "5" * 64,
            "deadline_at": "2026-09-06T20:04:00.000000Z",
        },
    }
    store = SimpleNamespace(_objects=Objects(plan))
    monkeypatch.setattr(m, "validate_question_invocation", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(m, "validate_stored_question_plan", lambda value, **_kwargs: value)
    discovery_rows = [{"run_id": "run", "signal_id": "signal", "market": "za"}]
    monkeypatch.setattr(
        m,
        "execute_released_candidate_query",
        lambda *_args, **_kwargs: {
            "application_status": discovery_status,
            "reason": "discovery_unavailable" if discovery_status != "accepted" else None,
            "rows": discovery_rows,
            "receipt": query_receipt(m, "released_candidates_v1", discovery_rows, "6"),
        },
    )
    selection = {
        "selected_ids": ({"run_id": "run", "signal_id": "signal", "market": "za"},),
        "selected_run_ids": ("run",),
        "reserved_inspection_slots": 10,
        "omitted_runs": (),
        "discovery_truncated": False,
        "source_authority": False,
    }
    monkeypatch.setattr(
        m, "select_released_candidates_for_inspection", lambda *_args, **_kwargs: selection
    )
    selected_fact = fact()
    material = {
        "overflow_count": 0,
        "selection_truncation_reason": None,
        "selected_facts": [selected_fact],
        "missing_checks": [
            "physical_schema_validation",
            "source_row_copy_binding",
            "resolved_plan_window_and_scope_admission",
            "release_pointer_and_runtime_authorization",
            "actual_select_job_and_digest_sql_parity",
        ],
        "run_metadata": {
            "run": {
                "manifest": [{"copy_run_id": "copy"}],
                "copy_receipts": [{"copy_run_id": "copy"}],
            }
        },
    }
    source_rows = [{"kind": "synthetic"}]
    monkeypatch.setattr(
        m,
        "execute_selected_source_query",
        lambda *_args, **_kwargs: {
            "application_status": "accepted",
            "material": material,
            "rows": source_rows,
            "receipt": query_receipt(m, "released_evidence_v1", source_rows, "8"),
        },
    )
    receipt = SimpleNamespace(
        run_id="run",
        cluster_build_version="hybrid_graph_v1",
        observation_start=datetime(2026, 8, 23, tzinfo=UTC).date(),
        observation_end=datetime(2026, 9, 5, tzinfo=UTC).date(),
    )
    monkeypatch.setattr(m, "_source_run", lambda *_args: (receipt, {}))
    monkeypatch.setattr(
        m,
        "read_question_source_schemas",
        lambda *_args, **_kwargs: {
            "requested_cluster_build_version": "hybrid_graph_v1",
            "metadata_digest": "9" * 64,
            "table_resources": [],
            "schema_digests": {},
            "declaration_digests": {},
            "source_authority": False,
            "observations": [],
            "source_receipt_version_bound": False,
        },
    )
    release_rows = [{"release": True}]
    monkeypatch.setattr(
        m,
        "execute_release_material_query",
        lambda *_args, **_kwargs: {
            "application_status": "accepted",
            "material": {"release": True},
            "rows": release_rows,
            "receipt": query_receipt(m, "released_review_v1", release_rows, "a"),
        },
    )
    monkeypatch.setattr(
        m,
        "validate_released_run_admission",
        lambda *_args, **_kwargs: SimpleNamespace(
            run_id="run", producer_profile_id="legacy_r16_v2", receipt=receipt
        ),
    )
    copy_rows = [{"copy": True}]
    monkeypatch.setattr(
        m,
        "execute_current_source_copy_query",
        lambda *_args, **_kwargs: {
            "application_status": "accepted",
            "material": {
                "content_validated": True,
                "physical_schema_required": True,
                "copy_run_id": "copy",
            },
            "rows": copy_rows,
            "receipt": query_receipt(m, "current_source_copy_v1", copy_rows, "b"),
        },
    )
    monkeypatch.setattr(
        m,
        "read_question_copy_schemas",
        lambda *_args, **_kwargs: {
            "source_authority": False,
            "metadata_digest": "c" * 64,
            "table_resources": [],
            "schema_digests": {},
            "declaration_digests": {},
            "observations": [],
        },
    )
    return m, store, plan


def build(m, store, plan):
    return m.build_general_question_snapshot(
        {"request_id": "request"},
        store=store,
        scope={},
        runtime_identity={},
        credentials=object(),
        plan=plan,
        candidate_limit=100,
        evidence_limit=200,
        discovery_bytes=100,
        source_bytes=100,
        release_bytes=100,
        now=datetime(2026, 9, 6, 20, tzinfo=UTC),
    )


def test_fixed_pipeline_persists_exact_snapshot_and_idempotent_readback(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    first = build(m, store, plan)
    second = build(m, store, plan)
    assert first == second
    assert first["status"] == "admitted"
    snapshot = first["snapshot"]
    digest = snapshot.pop("snapshot_digest")
    assert digest == m.canonical_digest(snapshot)
    assert snapshot["receipts"][0]["citation_label"] == "R1"
    assert snapshot["readings"][0]["value"] == 1
    assert snapshot["readings"][0]["method"] == "selected_receipt_count"
    assert snapshot["fulfilled_requirement_ids"] == ["content"]
    assert store._objects.writes == 1


def test_failed_discovery_returns_typed_failure_without_snapshot(monkeypatch):
    m, store, plan = fixture(monkeypatch, discovery_status="unavailable")
    result = build(m, store, plan)
    assert result == {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "refused",
        "snapshot": None,
        "reason": "discovery_unavailable",
        "missing_work": ["discovery_unavailable"],
    }
    assert store._objects.writes == 0


def test_current_copy_failure_and_unsupported_requirement_never_create_snapshot(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    monkeypatch.setattr(
        m,
        "execute_current_source_copy_query",
        lambda *_args, **_kwargs: {
            "application_status": "refused",
            "reason": "current_source_copy_invalid",
        },
    )
    assert build(m, store, plan)["reason"] == "current_source_copy_invalid"
    assert store._objects.writes == 0


@pytest.mark.parametrize(
    "boundary,expected",
    [
        ("copy_schema", "copy_schema_invalid"),
        ("release", "release_admission_invalid"),
        ("copy_binding", "snapshot_source_copy_binding_invalid"),
    ],
)
def test_actual_validation_boundary_retains_only_known_safe_failure_code(
    monkeypatch, boundary, expected
):
    result, store = actual_validation_failure(monkeypatch, boundary)
    assert result["reason"] == expected
    assert result["missing_work"] == [expected]
    assert store._objects.writes == 0


def actual_validation_failure(monkeypatch, boundary):
    m, store, plan = fixture(monkeypatch)
    if boundary == "copy_schema":
        from src.analysis.open_intelligence.general_question_copy_schema import (
            validate_current_source_copy_schemas,
        )

        monkeypatch.setattr(
            m,
            "read_question_copy_schemas",
            lambda *_args, **_kwargs: validate_current_source_copy_schemas([]),
        )
    elif boundary == "release":
        from src.analysis.open_intelligence.general_question_release_admission import (
            validate_released_run_admission,
        )

        monkeypatch.setattr(m, "validate_released_run_admission", validate_released_run_admission)
    else:
        monkeypatch.setattr(
            m,
            "execute_current_source_copy_query",
            lambda *_args, **_kwargs: {
                "application_status": "accepted",
                "material": {
                    "content_validated": True,
                    "physical_schema_required": True,
                    "copy_run_id": "different_copy",
                },
                "rows": [{"copy": True}],
                "receipt": query_receipt(m, "current_source_copy_v1", [{"copy": True}], "b"),
            },
        )
    return build(m, store, plan), store


def test_arbitrary_value_error_text_never_enters_snapshot_failure(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    monkeypatch.setattr(
        m,
        "read_question_copy_schemas",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("RAW_SENTINEL")),
    )
    result = build(m, store, plan)
    assert result["reason"] == "snapshot_validation_failed"
    assert "RAW_SENTINEL" not in str(result)
    assert store._objects.writes == 0


@pytest.mark.parametrize(
    "error,expected",
    [
        (ValueError("protected_context_registry_invalid"), "protected_context_registry_invalid"),
        (ValueError("protected_context_registry_invalid PRIVATE"), "snapshot_validation_failed"),
        (RuntimeError("protected_context_registry_invalid"), "snapshot_validation_failed"),
    ],
)
def test_context_registry_failure_preserves_only_the_named_safe_code(monkeypatch, error, expected):
    from src.analysis.open_intelligence import protected_context_registry as registry

    m, store, plan = fixture(monkeypatch)
    monkeypatch.setattr(registry, "_PROTECTED_CONTEXT_PROFILES", ({"profile_id": "synthetic"},))
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise error

    monkeypatch.setattr(m, "_build_context_snapshot", fail)
    result = build(m, store, plan)
    assert calls == [True]
    assert result["reason"] == expected
    assert result["missing_work"] == [expected]
    assert "PRIVATE" not in str(result)
    assert store._objects.writes == 0


def test_unsupported_requirement_without_grounded_portion_never_creates_snapshot(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"] = [
        {
            "requirement_id": "aggregate",
            "question": "What share is represented?",
            "kind": "aggregate",
            "mandatory": True,
            "search_terms": [],
        }
    ]
    result = build(m, store, plan)
    assert result["status"] == "coverage_gap"
    assert result["missing_work"] == ["aggregate"]
    assert store._objects.writes == 0


def test_unmatched_content_requirement_stays_missing_in_partial_snapshot(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(
        {
            "requirement_id": "unmatched",
            "question": "What does a different subject show?",
            "kind": "content",
            "mandatory": True,
            "search_terms": ["absent phrase"],
        }
    )
    result = build(m, store, plan)
    assert result["status"] == "admitted"
    assert result["snapshot"]["fulfilled_requirement_ids"] == ["content"]
    assert result["snapshot"]["missing_work"] == ["unmatched"]
    assert store._objects.writes == 1


def test_grounded_content_snapshot_retains_unsupported_mandatory_work_as_partial(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(
        {
            "requirement_id": "history",
            "question": "What is the earlier history?",
            "kind": "history",
            "mandatory": True,
            "search_terms": [],
        }
    )
    result = build(m, store, plan)
    assert result["status"] == "admitted"
    assert result["snapshot"]["fulfilled_requirement_ids"] == ["content"]
    assert result["snapshot"]["missing_work"] == ["history"]
    assert store._objects.writes == 1


@pytest.mark.parametrize(
    "field,value", [("market", "ng"), ("published_at", datetime(2026, 8, 1, tzinfo=UTC))]
)
def test_fact_outside_resolved_plan_scope_or_window_refuses(field, value):
    selected = fact()
    selected[field] = value
    with pytest.raises(ValueError, match="snapshot_scope_invalid"):
        module()._validate_fact_scope(
            [selected],
            {
                "markets": ["za"],
                "window": {"start": "2026-08-23", "end": "2026-09-05", "closed": True},
            },
            "2026-09-06T20:00:00.000000Z",
        )


def test_planned_market_without_admitted_fact_is_explicit_partial_coverage():
    selected = fact()
    receipt = SimpleNamespace(
        run_id="run",
        observation_start=datetime(2026, 9, 3, tzinfo=UTC).date(),
        observation_end=datetime(2026, 9, 3, tzinfo=UTC).date(),
    )
    missing, limitations = module()._coverage_gaps(
        {
            "markets": ["ng", "za"],
            "window": {"start": "2026-09-03", "end": "2026-09-03", "closed": True},
        },
        [selected],
        [receipt],
    )
    assert missing == ["market:ng"]
    assert limitations == ["No admitted selected facts cover planned market ng."]


def test_unmatched_selected_fact_is_not_projected_into_public_receipts():
    m = module()
    matched = fact()
    unrelated = {**fact(), "row_id": "other", "projection_sha256": "d" * 64}
    unrelated["excerpt"] = "A different subject"
    unrelated["source_label"] = "Different source"
    requirements = [
        {
            "requirement_id": "content",
            "question": "What is synthetic?",
            "kind": "content",
            "mandatory": True,
            "search_terms": ["synthetic"],
        }
    ]
    coverage, missing = m._requirement_coverage(requirements, [matched, unrelated])
    material, coverage = m._covered_material({"selected_facts": [matched, unrelated]}, coverage)
    assert missing == []
    assert material["selected_facts"] == [matched]
    assert coverage == {"content": (0,)}


def test_elapsed_time_is_carried_to_later_stages_and_blocks_snapshot(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(m, "monotonic", lambda: clock[0])

    def discovery(*_args, **_kwargs):
        clock[0] = 241.0
        return {
            "application_status": "accepted",
            "reason": None,
            "rows": [{"run_id": "run", "signal_id": "signal", "market": "za"}],
            "receipt": {"result_digest": "6" * 64},
        }

    monkeypatch.setattr(m, "execute_released_candidate_query", discovery)
    monkeypatch.setattr(
        m,
        "execute_selected_source_query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("expired source read")),
    )
    result = build(m, store, plan)
    assert result["reason"] == "request_expired"
    assert store._objects.writes == 0


def validator_fixture(monkeypatch, *, with_history=False, resolver=None):
    m, store, plan = fixture(monkeypatch)
    if with_history:
        plan["requirements"].append(history_requirement())
    snapshot = (
        build(m, store, plan) if resolver is None else build_with_history(m, store, plan, resolver)
    )["snapshot"]
    provenance = snapshot["provenance"]
    selection = provenance["selection"]
    material = {
        "selected_facts": [fact()],
        "overflow_count": 0,
        "selection_truncation_reason": None,
        "missing_checks": list(m._COORDINATOR_CHECKS),
        "run_metadata": {
            "run": {
                "manifest": [{"copy_run_id": "copy"}],
                "copy_receipts": [{"copy_run_id": "copy"}],
            }
        },
    }

    def descriptor(capture, candidate_limit):
        receipt = capture["receipt"]
        return SimpleNamespace(
            template_id=receipt["template_id"],
            sql_digest=receipt["sql_digest"],
            parameters_digest=receipt["parameters_digest"],
            candidate_limit=candidate_limit,
        )

    monkeypatch.setattr(
        m,
        "build_released_candidate_query",
        lambda *_args, **_kwargs: descriptor(
            provenance["discovery"], provenance["candidate_limit"]
        ),
    )
    monkeypatch.setattr(
        m,
        "build_selected_source_query",
        lambda *_args, **_kwargs: descriptor(
            provenance["source"], selection["reserved_inspection_slots"]
        ),
    )
    monkeypatch.setattr(
        m,
        "build_release_material_query",
        lambda *_args, **_kwargs: descriptor(provenance["releases"][0], 0),
    )
    monkeypatch.setattr(
        m,
        "build_current_source_copy_query",
        lambda *_args, **_kwargs: descriptor(provenance["current_copy"], 0),
    )
    monkeypatch.setattr(
        m,
        "select_released_candidates_for_inspection",
        lambda *_args, **_kwargs: copy.deepcopy(selection),
    )
    monkeypatch.setattr(
        m, "decode_selected_source_rows", lambda *_args, **_kwargs: copy.deepcopy(material)
    )
    source_schema = {
        field: copy.deepcopy(provenance["source_schema"][field])
        for field in (
            "table_resources",
            "metadata_digest",
            "schema_digests",
            "declaration_digests",
            "source_authority",
        )
    }
    copy_schema = {
        field: copy.deepcopy(provenance["copy_schema"][field])
        for field in (
            "table_resources",
            "metadata_digest",
            "schema_digests",
            "declaration_digests",
            "source_authority",
        )
    }
    monkeypatch.setattr(m, "validate_source_schemas", lambda *_args, **_kwargs: source_schema)
    monkeypatch.setattr(
        m, "validate_current_source_copy_schemas", lambda *_args, **_kwargs: copy_schema
    )
    monkeypatch.setattr(
        m,
        "validate_current_source_copy_proof",
        lambda *_args, **_kwargs: {"content_validated": True, "copy_run_id": "copy"},
    )
    request = {
        "request_id": "request",
        "request_digest": "2" * 64,
        "policy_digest": "4" * 64,
        "as_of": "2026-09-06T20:00:00.000000Z",
    }
    intake = {"intake_digest": "3" * 64}
    captures = [
        provenance["discovery"],
        provenance["source"],
        provenance["releases"][0],
        provenance["current_copy"],
    ]
    query_records = {}
    for ordinal, capture in enumerate(captures, start=1):
        query_records[ordinal] = {
            "reservation": {
                "ordinal": ordinal,
                "request_id": request["request_id"],
                "request_digest": request["request_digest"],
                "intake_digest": intake["intake_digest"],
                "policy_digest": snapshot["policy_digest"],
                "deployment_digest": snapshot["deployment_digest"],
                "template_id": capture["receipt"]["template_id"],
                "sql_digest": capture["receipt"]["sql_digest"],
                "parameters_digest": capture["receipt"]["parameters_digest"],
                "candidate_limit": (
                    provenance["candidate_limit"]
                    if ordinal == 1
                    else selection["reserved_inspection_slots"]
                    if ordinal == 2
                    else 0
                ),
            },
            "receipt": copy.deepcopy(capture["receipt"]),
        }
    return m, snapshot, request, intake, plan, query_records


def test_stored_snapshot_revalidates_against_actual_query_records(monkeypatch):
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch)
    result = m.validate_stored_general_question_snapshot(
        snapshot, request=request, intake=intake, plan=plan, query_records=records
    )
    assert result == snapshot
    result["receipts"][0]["excerpt"] = "detached"
    assert snapshot["receipts"][0]["excerpt"] == "Synthetic excerpt"


@pytest.mark.parametrize("mutation", ["ledger", "public", "coverage"])
def test_stored_snapshot_refuses_resealed_or_unowned_mutations(monkeypatch, mutation):
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch)
    changed = copy.deepcopy(snapshot)
    if mutation == "ledger":
        records[2]["receipt"]["result_digest"] = "f" * 64
    elif mutation == "public":
        changed["receipts"][0]["excerpt"] = "Resealed changed prose"
        changed["snapshot_digest"] = m.canonical_digest(
            {key: value for key, value in changed.items() if key != "snapshot_digest"}
        )
    else:
        changed["provenance"]["requirement_coverage"]["content"] = []
        changed["snapshot_digest"] = m.canonical_digest(
            {key: value for key, value in changed.items() if key != "snapshot_digest"}
        )
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            changed, request=request, intake=intake, plan=plan, query_records=records
        )


def history_requirement():
    return {
        "requirement_id": "history",
        "question": "What is the earlier history?",
        "kind": "history",
        "mandatory": True,
        "search_terms": [],
    }


def build_with_history(m, store, plan, resolver):
    return m.build_general_question_snapshot(
        {"request_id": "request"},
        store=store,
        scope={},
        runtime_identity={},
        credentials=object(),
        plan=plan,
        candidate_limit=100,
        evidence_limit=200,
        discovery_bytes=100,
        source_bytes=100,
        release_bytes=100,
        now=datetime(2026, 9, 6, 20, tzinfo=UTC),
        history_resolver=resolver,
    )


def brain_history_resolver(tmp_path, monkeypatch, **changes):
    import json

    from src.analysis.open_intelligence import brain_authority
    from src.analysis.open_intelligence.brain import history_requirement_resolver

    from tests.unit.test_intelligence_brain_observer import context as observer_context

    if changes:
        payload = json.loads(brain_authority._FIXTURE_PATH.read_text(encoding="utf-8"))
        payload.update(changes)
        fixture_path = tmp_path / "rewritten_snapshot.json"
        fixture_path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(brain_authority, "_FIXTURE_PATH", fixture_path)
    runtime, evidence = observer_context()
    return history_requirement_resolver(runtime, evidence), evidence


def test_history_requirement_is_covered_with_exact_provenance_through_the_historian(
    tmp_path, monkeypatch
):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    before = build(m, store, plan)
    assert before["status"] == "admitted"
    assert before["snapshot"]["missing_work"] == ["history"]
    assert before["snapshot"]["fulfilled_requirement_ids"] == ["content"]

    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    resolver, evidence = brain_history_resolver(tmp_path, monkeypatch)
    after = build_with_history(m, store, plan, resolver)

    assert after["status"] == "admitted"
    snapshot = after["snapshot"]
    assert snapshot["missing_work"] == []
    assert snapshot["fulfilled_requirement_ids"] == ["content", "history"]
    record = snapshot["provenance"]["history_requirements"]["history"]
    assert record["state"] == "analogue"
    assert record["signal_id"] == evidence.signal_id
    assert record["as_of"] == evidence.as_of.isoformat()
    provenance = record["provenance"]
    assert provenance["historical_object_id"] == "hist_fixture_1"
    assert provenance["source_created_at"] == "2026-08-01T00:00:00+00:00"
    assert provenance["source_first_observed_at"] == "2026-08-02T00:00:00+00:00"
    assert provenance["source_last_observed_at"] == "2026-08-20T00:00:00+00:00"
    assert provenance["selection_cutoff"] == "2026-08-27T00:00:00+00:00"
    assert provenance["evidence_ids"] == list(evidence.evidence_ids)
    assert provenance["snapshot_id"] == evidence.snapshot_id
    assert len(provenance["result_digest"]) == 64
    assert snapshot["readings"][0]["method"] == "selected_receipt_count"
    assert store._objects.writes == 1


def test_history_requirement_without_comparable_history_records_the_typed_reason(
    tmp_path, monkeypatch
):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    resolver, evidence = brain_history_resolver(tmp_path, monkeypatch, historical_object_ids=[])
    result = build_with_history(m, store, plan, resolver)

    assert result["status"] == "admitted"
    snapshot = result["snapshot"]
    assert snapshot["missing_work"] == ["history"]
    assert snapshot["fulfilled_requirement_ids"] == ["content"]
    assert snapshot["provenance"]["history_requirements"] == {
        "history": {
            "state": "insufficient_history",
            "requirement_id": "history",
            "signal_id": evidence.signal_id,
            "as_of": evidence.as_of.isoformat(),
            "reason": "no_comparable_history",
        }
    }


def test_history_resolver_output_for_an_unplanned_requirement_is_refused(tmp_path, monkeypatch):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)

    def foreign(plan):
        answer = resolver(plan)["history"]
        return {"other": answer}

    result = build_with_history(m, store, plan, foreign)
    assert result["status"] == "refused"
    assert result["reason"] == "history_resolution_invalid"
    assert store._objects.writes == 0


def retained_history_resolver(monkeypatch, requirement_id="history"):
    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        RetainedHistorySource,
        build_question_history_resolver,
    )

    from tests.unit import test_historical_provenance_v2 as bridge_fixture

    current, candidate, rows = bridge_fixture.retained_analogue_material()
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: bridge_fixture.FakeClient(rows))
    resolver = build_question_history_resolver(
        frame=bridge_fixture.frame(),
        sources=(RetainedHistorySource(requirement_id, current, (candidate,)),),
        rules=bridge_fixture.analogue_rules(),
        limit=1,
    )
    return resolver, candidate


def test_a_planned_history_requirement_without_a_resolver_is_recorded_not_omitted(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    result = build(m, store, plan)

    assert result["status"] == "admitted"
    snapshot = result["snapshot"]
    assert snapshot["provenance"]["history_requirements"] == {
        "history": {
            "state": "unresolved",
            "requirement_id": "history",
            "reason": "history_resolver_not_wired",
        }
    }
    assert snapshot["missing_work"] == ["history"]
    assert snapshot["fulfilled_requirement_ids"] == ["content"]


def test_a_resolver_that_answers_nothing_is_recorded_not_omitted(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    result = build_with_history(m, store, plan, lambda _plan: {})

    assert result["status"] == "admitted"
    assert result["snapshot"]["provenance"]["history_requirements"] == {
        "history": {
            "state": "unresolved",
            "requirement_id": "history",
            "reason": "history_requirement_unanswered",
        }
    }
    assert result["snapshot"]["missing_work"] == ["history"]


def test_a_resolver_unresolved_answer_keeps_its_exact_reason(monkeypatch):
    from src.analysis.open_intelligence.historical_evidence_bridge import UnresolvedHistory

    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    result = build_with_history(
        m,
        store,
        plan,
        lambda _plan: {
            "history": UnresolvedHistory("history", "historical_evidence_reader_incomplete")
        },
    )

    assert result["snapshot"]["provenance"]["history_requirements"]["history"] == {
        "state": "unresolved",
        "requirement_id": "history",
        "reason": "historical_evidence_reader_incomplete",
    }


def test_retained_history_record_covers_its_requirement_with_its_full_provenance(monkeypatch):
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    resolver, candidate = retained_history_resolver(monkeypatch)
    result = build_with_history(m, store, plan, resolver)

    assert result["status"] == "admitted"
    snapshot = result["snapshot"]
    assert snapshot["missing_work"] == []
    assert snapshot["fulfilled_requirement_ids"] == ["content", "history"]
    record = snapshot["provenance"]["history_requirements"]["history"]
    assert record["state"] == "retained_analogue"
    assert record["requirement_id"] == "history"
    assert record["matched_signal_id"] == candidate.signal_id
    assert "signal_id" not in record
    assert record["record"]["requirement_id"] == "history"
    assert record["record"]["record_version"] == "history_record_v2"
    assert record["record"]["reader_mode"] == "runtime_view"
    assert record["record"]["kind"] == "analogue"
    assert record["record"]["history_record_id"].startswith("hrc_")
    assert record["record"]["source_object_id"] == candidate.signal_id
    assert store._objects.writes == 1


def test_stored_snapshot_revalidates_its_retained_history_record(monkeypatch):
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    assert snapshot["fulfilled_requirement_ids"] == ["content", "history"]
    assert (
        m.validate_stored_general_question_snapshot(
            snapshot, request=request, intake=intake, plan=plan, query_records=records
        )
        == snapshot
    )


def test_stored_snapshot_with_an_edited_history_record_is_refused(monkeypatch):
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    changed = copy.deepcopy(snapshot)
    stored = changed["provenance"]["history_requirements"]["history"]["record"]
    stored["source_market"] = "ng"
    changed["snapshot_digest"] = m.canonical_digest(
        {key: value for key, value in changed.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            changed, request=request, intake=intake, plan=plan, query_records=records
        )


def test_stored_snapshot_missing_a_planned_history_record_is_refused(monkeypatch):
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    changed = copy.deepcopy(snapshot)
    changed["provenance"]["history_requirements"] = {}
    changed["snapshot_digest"] = m.canonical_digest(
        {key: value for key, value in changed.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            changed, request=request, intake=intake, plan=plan, query_records=records
        )


def resign_stored_history_record(
    m, snapshot, *, requirement_id="history", changes=None, extra=None
):
    """Rewrite one stored history record and rehash it and the snapshot.

    The record id is recomputed over the decoded values in declaration order,
    exactly as the bridge computes it, so the rewritten record is one an
    attacker holding the stored snapshot could actually produce.
    """
    from dataclasses import fields

    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecord,
        _history_record_id,
    )

    changed = copy.deepcopy(snapshot)
    stored = changed["provenance"]["history_requirements"][requirement_id]["record"]
    stored.update(changes or {})
    decoded = {}
    for field in fields(HistoryRecord):
        if field.name == "history_record_id":
            continue
        item = stored[field.name]
        if field.name in m._HISTORY_RECORD_INSTANT_FIELDS:
            decoded[field.name] = datetime.fromisoformat(item)
        elif field.name in m._HISTORY_RECORD_DATE_FIELDS:
            decoded[field.name] = date.fromisoformat(item)
        elif field.name in m._HISTORY_RECORD_SEQUENCE_FIELDS:
            decoded[field.name] = tuple(item)
        else:
            decoded[field.name] = item
    stored["history_record_id"] = _history_record_id(decoded)
    stored.update(extra or {})
    changed["snapshot_digest"] = m.canonical_digest(
        {key: value for key, value in changed.items() if key != "snapshot_digest"}
    )
    return changed


def test_stored_history_record_cannot_claim_a_fixture_read_as_a_runtime_read(monkeypatch):
    """A rehashed stored record that renames the reader is refused on read back."""
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    for change in (
        {"reader_mode": "fixture"},
        {"reader_version": "totally_made_up"},
        {"kind": "not_a_kind"},
    ):
        changed = resign_stored_history_record(m, snapshot, changes=change)
        assert (
            changed["provenance"]["history_requirements"]["history"]["record"]
            != snapshot["provenance"]["history_requirements"]["history"]["record"]
        )
        with pytest.raises(ValueError, match="snapshot_invalid"):
            m.validate_stored_general_question_snapshot(
                changed, request=request, intake=intake, plan=plan, query_records=records
            )


def test_stored_history_record_cannot_be_moved_onto_another_requirement(monkeypatch):
    """The record's own requirement id is digested and matched to its key."""
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    assert (
        snapshot["provenance"]["history_requirements"]["history"]["record"]["requirement_id"]
        == "history"
    )
    changed = resign_stored_history_record(m, snapshot, changes={"requirement_id": "history_two"})
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            changed, request=request, intake=intake, plan=plan, query_records=records
        )


def test_stored_history_record_with_a_field_it_never_carried_is_refused(monkeypatch):
    """The stored projection is matched by its exact field set, not a subset."""
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    changed = resign_stored_history_record(m, snapshot, extra={"reader_note": "added later"})
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            changed, request=request, intake=intake, plan=plan, query_records=records
        )


def test_stored_history_record_times_keep_the_iso_8601_wire_format(monkeypatch):
    """The stored instants and dates are ISO 8601, not a repr of the value."""
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    resolver, _candidate = retained_history_resolver(monkeypatch)
    snapshot = build_with_history(m, store, plan, resolver)["snapshot"]
    stored = snapshot["provenance"]["history_requirements"]["history"]["record"]
    for field in m._HISTORY_RECORD_INSTANT_FIELDS:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00", stored[field]), (
            field,
            stored[field],
        )
    for field in m._HISTORY_RECORD_DATE_FIELDS:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", stored[field]), (field, stored[field])
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00",
        snapshot["provenance"]["history_requirements"]["history"]["as_of"],
    )


def test_a_history_requirement_id_that_is_not_text_is_refused_before_admission(monkeypatch):
    """A non-string planned id used to build a snapshot that never read back."""
    for identity in (7, "", "   "):
        m, store, plan = fixture(monkeypatch)
        plan["requirements"].append(dict(history_requirement(), requirement_id=identity))
        result = build(m, store, plan)
        assert result["status"] == "refused"
        assert result["reason"] == "history_resolution_invalid"
        assert store._objects.writes == 0


def test_a_resolver_bridge_refusal_does_not_surface_its_own_code(monkeypatch):
    """A bridge refusal outside the per requirement guard stays a safe code."""
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    def failing(_plan):
        raise HistoricalBridgeError("historical_scope_invalid", "market scope")

    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    result = build_with_history(m, store, plan, failing)
    assert result["status"] == "refused"
    assert result["reason"] == "history_resolution_invalid"
    assert store._objects.writes == 0


def test_a_snapshot_stored_before_any_resolver_still_reads_back(monkeypatch):
    """Every snapshot already in the store must still validate.

    Before the resolver was wired the builder was called with none, so a plan
    that asked for history was sealed with an empty history map and the
    requirement already listed as missing work. That shape is internally
    consistent: the coverage recomputation derives the same missing work from
    the same empty map, which is why it validated before. The validator is
    reached through the answering stage and through the dossier admission
    path, so refusing it would make an already retrieved question
    unanswerable and refuse its dossier admission.
    """
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch, with_history=True)
    assert snapshot["missing_work"] == ["history"]
    assert snapshot["fulfilled_requirement_ids"] == ["content"]
    # Snapshots sealed before this branch carry no history contract version,
    # so the stand in drops the key the head builder writes.
    stored = as_prior_history_shape(m, snapshot, {})
    assert (
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )
        == stored
    )


def test_a_stored_snapshot_dropping_a_covered_history_record_is_refused(monkeypatch):
    """A record this code wrote, deleted from the sealed snapshot, is refused.

    The dropped record covered its requirement, so the recomputed missing work
    names that requirement and the recomputed fulfilled ids do not; both
    disagree with what the sealed snapshot carries. What remains is still a
    subset of what the plan planned, so the subset comparison lets it through
    and the coverage recomputation is what refuses it.
    """
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    assert snapshot["fulfilled_requirement_ids"] == ["content", "history"]
    assert snapshot["missing_work"] == []
    stored = copy.deepcopy(snapshot)
    del stored["provenance"]["history_requirements"]["history"]
    assert set(stored["provenance"]["history_requirements"]) <= {"history"}
    stored["snapshot_digest"] = m.canonical_digest(
        {key: value for key, value in stored.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )


def test_a_stored_history_record_for_an_unplanned_requirement_is_refused(monkeypatch):
    """A record may only answer a requirement the plan actually planned.

    The added record is unresolved, so it covers nothing and changes neither
    the recomputed missing work nor the recomputed fulfilled ids. The subset
    comparison is the only thing that refuses it.
    """
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch, with_history=True)
    stored = copy.deepcopy(snapshot)
    stored["provenance"]["history_requirements"]["never_planned"] = {
        "state": "unresolved",
        "requirement_id": "never_planned",
        "reason": "history_resolver_not_wired",
    }
    stored["snapshot_digest"] = m.canonical_digest(
        {key: value for key, value in stored.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )


def test_two_history_requirements_sharing_an_identifier_are_refused(monkeypatch):
    """Two planned requirements with one id collapse into a single answer."""
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    plan["requirements"].append(dict(history_requirement(), question="Read it again."))
    result = build(m, store, plan)

    assert result["status"] == "refused"
    assert result["reason"] == "history_resolution_invalid"
    assert store._objects.writes == 0


def test_a_requirement_with_no_kind_is_refused_under_the_history_code(monkeypatch):
    """A requirement with no kind is refused, not quietly read as not history.

    It used to escape the safe failure code list and refuse the build under a
    generic code, because the two readings of a plan disagreed: one raised on
    the absent key and the other skipped the requirement.
    """
    m, store, plan = fixture(monkeypatch)
    requirement = history_requirement()
    del requirement["kind"]
    plan["requirements"].append(requirement)
    result = build(m, store, plan)

    assert result["status"] == "refused"
    assert result["reason"] == "history_resolution_invalid"
    assert store._objects.writes == 0


@pytest.mark.parametrize(
    "change",
    [
        {"display_eligible": True},
        {"source_object_id": "sig_" + "c" * 64},
        {"investigation_id": "inv_never_ran"},
        {"scope_digest": "sco_" + "0" * 64},
        {"evidence_refs_digest": "erd_" + "0" * 64},
        {"evidence_projection_receipt_id": "hep_" + "0" * 64},
        {"source_projection_digest": "spd_" + "0" * 64},
        {"provenance_bindings_digest": "pbd_" + "0" * 64},
    ],
)
def test_a_stored_history_record_cannot_rename_what_its_receipt_fixed(monkeypatch, change):
    """A rehashed stored record is held to the receipt id it names.

    Each change here is one the slice's own re-signing helper makes cleanly:
    the record digest is recomputed over the edited values and matches. What
    refuses the snapshot is the historical receipt id, rederived from those
    same values on read back.
    """
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    changed = resign_stored_history_record(m, snapshot, changes=change)
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            changed, request=request, intake=intake, plan=plan, query_records=records
        )


# Saved evidence for request c6b9bc0e against the protected 7 September snapshot,
# observed 20 September 2026. The first three values are native totalBytesBilled.
_RECORDED_BILLED_BYTES = {
    "authority": 31_457_280,
    "candidate": 31_457_280,
    "partitions": 49_283_072,
}
# The enriched index refusal names its required ceiling. The other values are
# recorded dry run billing estimates, not bills from executed evidence queries.
_REQUIRED_OR_ESTIMATED_BYTES = {
    "enriched_index": 157_286_400,
    "enriched": 130_023_424,
    "raw_index": 236_978_176,
    "raw": 125_829_120,
}


def test_enriched_index_cap_clears_the_recorded_required_ceiling():
    caps = module()._direct_context_query_caps(
        module()._context_query_caps(35_000_000, 300_000_000, 35_000_000)
    )
    assert caps["enriched_index"] >= 157_286_400


def test_every_lane_cap_clears_recorded_billing_evidence_inside_the_total():
    m = module()
    caps = m._context_query_caps(35_000_000, 300_000_000, 35_000_000)
    direct = m._direct_context_query_caps(caps)
    evidence = {**_RECORDED_BILLED_BYTES, **_REQUIRED_OR_ESTIMATED_BYTES}
    assert set(direct) == set(evidence)
    for lane, billed_or_estimated in evidence.items():
        assert direct[lane] >= billed_or_estimated, lane
        if lane != "partitions":
            assert caps[lane] >= billed_or_estimated, lane
    assert sum(direct.values()) + direct["authority"] <= 1_000_000_000
    assert sum(caps.values()) + caps["authority"] <= 1_000_000_000


def reseal(m, snapshot):
    snapshot["snapshot_digest"] = m.canonical_digest(
        {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
    )
    return snapshot


def as_prior_history_shape(m, snapshot, history=...):
    """Rewrite a head built snapshot into the shape stored before versioning.

    Snapshots sealed before the history shape was versioned carry no history
    contract version at all, so the prior shape is the head shape with that
    key removed and, where given, the history map replaced.
    """
    stored = copy.deepcopy(snapshot)
    del stored["provenance"]["history_contract_version"]
    if history is None:
        del stored["provenance"]["history_requirements"]
    elif history is not ...:
        stored["provenance"]["history_requirements"] = history
    return reseal(m, stored)


def test_a_snapshot_built_now_carries_the_current_history_contract_version(monkeypatch):
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    assert snapshot["provenance"]["history_contract_version"] == "general_question_history_v2"
    assert (
        m.validate_stored_general_question_snapshot(
            snapshot, request=request, intake=intake, plan=plan, query_records=records
        )
        == snapshot
    )
    assert m.stored_history_contract_version(snapshot) == "general_question_history_v2"


def test_a_snapshot_built_now_without_history_still_carries_the_version(monkeypatch):
    _m, snapshot, *_rest = validator_fixture(monkeypatch)
    assert snapshot["provenance"]["history_requirements"] == {}
    assert snapshot["provenance"]["history_contract_version"] == "general_question_history_v2"


@pytest.mark.parametrize("history", ["empty", "absent"])
def test_a_snapshot_stored_in_the_prior_history_shape_reads_back_as_the_prior_version(
    monkeypatch, history
):
    """A snapshot sealed before the version existed still reads, labelled v1.

    Before the resolver was wired a plan asking for history was sealed with an
    empty history map, and before history existed at all with no map. Neither
    carries a history contract version.
    """
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch, with_history=True)
    stored = as_prior_history_shape(m, snapshot, {} if history == "empty" else None)
    assert "history_contract_version" not in stored["provenance"]
    assert stored["missing_work"] == ["history"]
    assert (
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )
        == stored
    )
    assert m.stored_history_contract_version(stored) == "general_question_history_v1"


@pytest.mark.parametrize(
    ("changes", "state", "missing_work"),
    [
        ({"historical_object_ids": []}, "insufficient_history", ["history"]),
        ({}, "analogue", []),
    ],
)
def test_a_prior_version_historian_record_reads_back_as_the_prior_version(
    tmp_path, monkeypatch, changes, state, missing_work
):
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch, **changes)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    record = snapshot["provenance"]["history_requirements"]["history"]
    assert record["state"] == state
    stored = as_prior_history_shape(m, snapshot)
    assert stored["missing_work"] == missing_work
    assert (
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )
        == stored
    )
    assert m.stored_history_contract_version(stored) == "general_question_history_v1"


def test_a_historian_analogue_record_round_trips_through_sorted_key_json(tmp_path, monkeypatch):
    """A stored analogue is read by its provenance key set, not its key order.

    The builder writes the provenance in the historian's field order, but the
    stored snapshot is canonical JSON with sorted keys, so the order that comes
    back is not the order that was written.
    """
    import json

    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    stored = json.loads(json.dumps(snapshot, sort_keys=True))
    provenance = stored["provenance"]["history_requirements"]["history"]["provenance"]
    assert tuple(provenance) == tuple(sorted(m._HISTORY_PROVENANCE_FIELDS))
    assert tuple(provenance) != m._HISTORY_PROVENANCE_FIELDS
    assert (
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )
        == stored
    )


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_a_stored_analogue_provenance_with_a_different_key_set_is_refused(
    tmp_path, monkeypatch, change
):
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    stored = copy.deepcopy(snapshot)
    provenance = stored["provenance"]["history_requirements"]["history"]["provenance"]
    if change == "missing":
        del provenance["snapshot_id"]
    else:
        provenance["unexpected"] = "value"
    reseal(m, stored)
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )


@pytest.mark.parametrize("resolved", [False, True])
def test_a_prior_version_snapshot_cannot_carry_a_state_only_the_new_version_writes(
    monkeypatch, resolved
):
    """An unresolved or retained record under the prior version is refused.

    Neither state existed before the version moved, so a snapshot claiming the
    prior version while carrying one is not reinterpreted as either version.
    """
    resolver = retained_history_resolver(monkeypatch)[0] if resolved else None
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    state = snapshot["provenance"]["history_requirements"]["history"]["state"]
    assert state == ("retained_analogue" if resolved else "unresolved")
    stored = as_prior_history_shape(m, snapshot)
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )


@pytest.mark.parametrize(
    "version", ["general_question_history_v3", "general_question_history_v1", None, 2]
)
def test_a_snapshot_with_an_unknown_history_contract_version_is_refused(monkeypatch, version):
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch, with_history=True)
    stored = copy.deepcopy(snapshot)
    stored["provenance"]["history_contract_version"] = version
    reseal(m, stored)
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )
    with pytest.raises(ValueError, match="history_contract_version_unknown"):
        m.stored_history_contract_version(stored)


def stored_history_map(tmp_path, monkeypatch, *, analogue=True):
    """A plan with one history requirement and the history map sealed for it."""
    if analogue:
        resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)
    else:
        resolver = None
    m, snapshot, *_rest, plan, _records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    return m, plan, copy.deepcopy(snapshot["provenance"]["history_requirements"])


def test_a_current_version_snapshot_missing_its_unresolved_record_is_refused(monkeypatch):
    """The current writer records every planned requirement, so the reader does.

    A snapshot built with no resolver records the requirement as unresolved.
    Deleting that record leaves the same missing work and fulfilled ids, so
    only an equality between the planned ids and the records refuses it.
    """
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch, with_history=True)
    history = snapshot["provenance"]["history_requirements"]
    assert history["history"]["reason"] == "history_resolver_not_wired"
    stored = copy.deepcopy(snapshot)
    stored["provenance"]["history_requirements"] = {}
    reseal(m, stored)
    assert m.stored_history_contract_version(stored) == "general_question_history_v2"
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )


def test_only_the_prior_version_may_carry_fewer_records_than_planned(tmp_path, monkeypatch):
    m, plan, _history = stored_history_map(tmp_path, monkeypatch, analogue=False)
    m._validate_history_records(plan, {}, "general_question_history_v1")
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, {}, "general_question_history_v2")


@pytest.mark.parametrize(
    "key",
    [
        "historical_object_id",
        "source_created_at",
        "source_first_observed_at",
        "source_last_observed_at",
        "selection_cutoff",
        "evidence_ids",
        "signal_id",
        "snapshot_id",
        "result_digest",
    ],
)
def test_an_analogue_provenance_missing_a_key_is_refused_by_its_key_set(tmp_path, monkeypatch, key):
    """The refusal is the history code, not a KeyError from a later read."""
    m, plan, history = stored_history_map(tmp_path, monkeypatch)
    assert set(history["history"]["provenance"]) == set(m._HISTORY_PROVENANCE_FIELDS)
    m._validate_history_records(plan, history)
    del history["history"]["provenance"][key]
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


@pytest.mark.parametrize("digest", ["g" * 64, "A" * 64, " " * 64, "a" * 63 + "\n"])
def test_an_analogue_result_digest_that_is_not_a_sha256_hex_digest_is_refused(
    tmp_path, monkeypatch, digest
):
    m, plan, history = stored_history_map(tmp_path, monkeypatch)
    history["history"]["provenance"]["result_digest"] = digest
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


@pytest.mark.parametrize(
    "key",
    [
        "source_created_at",
        "source_first_observed_at",
        "source_last_observed_at",
        "selection_cutoff",
    ],
)
@pytest.mark.parametrize(
    "value", ["not a time", "2026-08-01T00:00:00", "2026-13-01T00:00:00+00:00"]
)
def test_an_analogue_timestamp_that_is_not_an_aware_instant_is_refused(
    tmp_path, monkeypatch, key, value
):
    m, plan, history = stored_history_map(tmp_path, monkeypatch)
    history["history"]["provenance"][key] = value
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


@pytest.mark.parametrize(
    "reason", ["History resolver not wired", "history resolver", "_history", "history-gap", "1st"]
)
def test_an_unresolved_reason_outside_the_code_grammar_is_refused(tmp_path, monkeypatch, reason):
    m, plan, history = stored_history_map(tmp_path, monkeypatch, analogue=False)
    m._validate_history_records(plan, history)
    history["history"]["reason"] = reason
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


def test_an_analogue_provenance_naming_another_signal_is_refused(tmp_path, monkeypatch):
    """The writer copies one signal id into the record and its provenance."""
    m, plan, history = stored_history_map(tmp_path, monkeypatch)
    record = history["history"]
    assert record["provenance"]["signal_id"] == record["signal_id"]
    m._validate_history_records(plan, history)
    record["provenance"]["signal_id"] = "other"
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


@pytest.mark.parametrize(
    "key", ["source_created_at", "source_first_observed_at", "source_last_observed_at"]
)
def test_an_analogue_observed_after_its_selection_cutoff_is_refused(tmp_path, monkeypatch, key):
    """Mirrors the historian's leak check: nothing is later than the cutoff."""
    m, plan, history = stored_history_map(tmp_path, monkeypatch)
    provenance = history["history"]["provenance"]
    cutoff = datetime.fromisoformat(provenance["selection_cutoff"])
    provenance[key] = (cutoff + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


@pytest.mark.parametrize("offset", [0, 1])
def test_an_analogue_cutoff_not_earlier_than_the_record_as_of_is_refused(
    tmp_path, monkeypatch, offset
):
    """Mirrors the historian's leak check: the cutoff is before the as of."""
    m, plan, history = stored_history_map(tmp_path, monkeypatch)
    record = history["history"]
    as_of = datetime.fromisoformat(record["as_of"])
    record["provenance"]["selection_cutoff"] = (as_of + timedelta(seconds=offset)).isoformat()
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


def test_an_analogue_record_as_of_without_a_timezone_is_refused(tmp_path, monkeypatch):
    m, plan, history = stored_history_map(tmp_path, monkeypatch)
    record = history["history"]
    record["as_of"] = datetime.fromisoformat(record["as_of"]).replace(tzinfo=None).isoformat()
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


def test_a_forged_future_analogue_does_not_fulfil_history_on_read_back(tmp_path, monkeypatch):
    """The reviewer's probe: a resealed analogue from the future is refused.

    A snapshot built with no resolver has its unresolved record replaced by an
    analogue whose provenance names another signal and instants in 2099, with
    the missing work emptied and the fulfilled ids extended to match, then
    resealed. The digest cannot be recomputed, but the two invariants every
    written analogue satisfies can be, and this record breaks both.
    """
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)
    _m, genuine, *_rest = validator_fixture(monkeypatch, with_history=True, resolver=resolver)
    analogue = copy.deepcopy(genuine["provenance"]["history_requirements"]["history"])
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch, with_history=True)
    assert snapshot["missing_work"] == ["history"]
    analogue["signal_id"] = "forged"
    analogue["as_of"] = "2000-01-01T00:00:00+00:00"
    analogue["provenance"].update(
        {
            "signal_id": "other",
            "source_created_at": "2099-01-01T00:00:00+00:00",
            "source_first_observed_at": "2099-01-02T00:00:00+00:00",
            "source_last_observed_at": "2099-01-03T00:00:00+00:00",
            "selection_cutoff": "2099-01-04T00:00:00+00:00",
            "result_digest": "0" * 64,
        }
    )
    stored = copy.deepcopy(snapshot)
    stored["provenance"]["history_requirements"] = {"history": analogue}
    stored["missing_work"] = []
    stored["fulfilled_requirement_ids"] = ["content", "history"]
    reseal(m, stored)
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )


@pytest.mark.parametrize("reason", ["No comparable history", "no comparable", "-", "_gap"])
def test_an_insufficient_history_reason_outside_the_code_grammar_is_refused(
    tmp_path, monkeypatch, reason
):
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch, historical_object_ids=[])
    m, snapshot, *_rest, plan, _records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    history = copy.deepcopy(snapshot["provenance"]["history_requirements"])
    assert history["history"]["reason"] == "no_comparable_history"
    m._validate_history_records(plan, history)
    history["history"]["reason"] = reason
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history)


def request_instant(request):
    return datetime.fromisoformat(request["as_of"].replace("Z", "+00:00"))


def redate_analogue(record, *, shift):
    """Move a stored analogue and every instant it names by one shift.

    The shift keeps every invariant the record alone recomputes: the signal
    ids still agree, nothing observed is later than the cutoff and the cutoff
    is still earlier than the as of.
    """
    moved = copy.deepcopy(record)
    moved["as_of"] = (datetime.fromisoformat(moved["as_of"]) + shift).isoformat()
    for key in (
        "source_created_at",
        "source_first_observed_at",
        "source_last_observed_at",
        "selection_cutoff",
    ):
        value = datetime.fromisoformat(moved["provenance"][key])
        moved["provenance"][key] = (value + shift).isoformat()
    return moved


def test_a_self_consistent_analogue_dated_after_the_request_does_not_fulfil_history(
    tmp_path, monkeypatch
):
    """The reviewer's round three probe: every instant agrees, all in 2099.

    The record passes every check it can make against itself, so only the
    request as of, which the reader holds independently of the snapshot, can
    show that it was observed after the question was asked.
    """
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)
    _m, genuine, *_rest = validator_fixture(monkeypatch, with_history=True, resolver=resolver)
    m, snapshot, request, intake, plan, records = validator_fixture(monkeypatch, with_history=True)
    assert snapshot["missing_work"] == ["history"]
    analogue = redate_analogue(
        genuine["provenance"]["history_requirements"]["history"],
        shift=datetime(2099, 1, 1, tzinfo=UTC) - datetime(2026, 8, 1, tzinfo=UTC),
    )
    assert datetime.fromisoformat(analogue["as_of"]).year == 2099
    history = {"history": analogue}
    m._validate_history_records(plan, history)
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history, request_as_of=request["as_of"])
    stored = copy.deepcopy(snapshot)
    stored["provenance"]["history_requirements"] = history
    stored["missing_work"] = []
    stored["fulfilled_requirement_ids"] = ["content", "history"]
    reseal(m, stored)
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            stored, request=request, intake=intake, plan=plan, query_records=records
        )


@pytest.mark.parametrize("offset", [0, 1])
def test_an_analogue_is_held_to_the_request_as_of_to_the_second(tmp_path, monkeypatch, offset):
    """An analogue dated at the request as of is admitted, one second later is not."""
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)
    m, snapshot, request, *_rest, plan, _records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    record = snapshot["provenance"]["history_requirements"]["history"]
    assert datetime.fromisoformat(record["as_of"]) < request_instant(request)
    shift = request_instant(request) - datetime.fromisoformat(record["as_of"])
    history = {"history": redate_analogue(record, shift=shift + timedelta(seconds=offset))}
    m._validate_history_records(plan, history)
    if offset:
        with pytest.raises(ValueError, match="history_resolution_invalid"):
            m._validate_history_records(plan, history, request_as_of=request["as_of"])
    else:
        m._validate_history_records(plan, history, request_as_of=request["as_of"])


def test_a_genuine_analogue_is_dated_before_the_request(tmp_path, monkeypatch):
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)
    m, snapshot, request, *_rest, plan, _records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    history = snapshot["provenance"]["history_requirements"]
    m._validate_history_records(plan, history, request_as_of=request["as_of"])


@pytest.mark.parametrize("bound", ["2026-09-06T20:00:00", "not a time", 20260906])
def test_a_request_as_of_that_is_not_an_aware_instant_bounds_nothing(tmp_path, monkeypatch, bound):
    """A bound that cannot be ordered against an instant refuses under the history code."""
    resolver, _evidence = brain_history_resolver(tmp_path, monkeypatch)
    m, snapshot, *_rest, plan, _records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    history = snapshot["provenance"]["history_requirements"]
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history, request_as_of=bound)


def redate_stored_history_record(m, snapshot, changes):
    """Rewrite one stored retained record's instants and rederive both its ids.

    The historical receipt id and the record id are each recomputed from the
    record's own values, exactly as the bridge computes them, and the entry's
    as of follows the record's, so the rewritten record is one an attacker
    holding the stored snapshot could actually produce.
    """
    from dataclasses import asdict

    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecord,
        _history_record_id,
        _rederived_historical_receipt_id,
    )

    changed = copy.deepcopy(snapshot)
    entry = changed["provenance"]["history_requirements"]["history"]
    values = asdict(m._history_record_from_projection(entry["record"]))
    del values["history_record_id"]
    values.update(changes)
    values["historical_receipt_id"] = _rederived_historical_receipt_id(values)
    record = HistoryRecord(history_record_id=_history_record_id(values), **values)
    entry["record"] = m._history_record_projection(record)
    entry["as_of"] = record.as_of.isoformat()
    return reseal(m, changed)


def test_a_retained_record_moved_after_the_request_does_not_fulfil_history(monkeypatch):
    """The reviewer's probe on the retained state: 2099, with both ids rederived."""
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    assert snapshot["missing_work"] == []
    changed = redate_stored_history_record(
        m,
        snapshot,
        {
            "as_of": datetime(2099, 2, 1, tzinfo=UTC),
            "observed_window_start": datetime(2099, 1, 1, tzinfo=UTC),
            "observed_window_end": datetime(2099, 1, 20, tzinfo=UTC),
        },
    )
    history = changed["provenance"]["history_requirements"]
    m._validate_history_records(plan, history)
    with pytest.raises(ValueError, match="history_resolution_invalid"):
        m._validate_history_records(plan, history, request_as_of=request["as_of"])
    with pytest.raises(ValueError, match="snapshot_invalid"):
        m.validate_stored_general_question_snapshot(
            changed, request=request, intake=intake, plan=plan, query_records=records
        )


@pytest.mark.parametrize("fields", [("as_of",), ("observed_window_end",)])
@pytest.mark.parametrize("offset", [0, 1])
def test_each_retained_record_instant_is_held_to_the_request_as_of(monkeypatch, fields, offset):
    """The record type does not order its as of against its observed window.

    So the as of and the observed window end are each held to the request as
    of on their own: at it is admitted, one second later is not.
    """
    resolver, _candidate = retained_history_resolver(monkeypatch)
    m, snapshot, request, intake, plan, records = validator_fixture(
        monkeypatch, with_history=True, resolver=resolver
    )
    moved = request_instant(request) + timedelta(seconds=offset)
    changed = redate_stored_history_record(m, snapshot, dict.fromkeys(fields, moved))
    history = changed["provenance"]["history_requirements"]
    m._validate_history_records(plan, history)
    if offset:
        with pytest.raises(ValueError, match="history_resolution_invalid"):
            m._validate_history_records(plan, history, request_as_of=request["as_of"])
        with pytest.raises(ValueError, match="snapshot_invalid"):
            m.validate_stored_general_question_snapshot(
                changed, request=request, intake=intake, plan=plan, query_records=records
            )
    else:
        m._validate_history_records(plan, history, request_as_of=request["as_of"])


def test_the_writer_refuses_a_historian_analogue_dated_after_the_request(tmp_path, monkeypatch):
    """The builder seals history itself, so it is held to the reader's bound."""
    resolver, _evidence = brain_history_resolver(
        tmp_path, monkeypatch, as_of="2099-02-01T10:00:00.000000Z"
    )
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    result = build_with_history(m, store, plan, resolver)
    assert result["status"] == "refused"
    assert result["reason"] == "history_resolution_invalid"
    assert result["snapshot"] is None
    assert store._objects.writes == 0


def test_the_writer_refuses_a_retained_record_dated_after_the_request(monkeypatch):
    """A bridge whose frame is dated after the request builds a genuine record.

    The record carries the bridge capability and passes every check the
    record type makes, so only the request as of refuses it at the write.
    """
    import dataclasses

    from src.analysis.open_intelligence import historical_evidence_reader as reader
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        RetainedHistorySource,
        build_question_history_resolver,
    )

    from tests.unit import test_historical_provenance_v2 as bridge_fixture

    later = datetime(2099, 2, 1, tzinfo=UTC)
    current, candidate, rows = bridge_fixture.retained_analogue_material()
    current = dataclasses.replace(current, as_of=later)
    monkeypatch.setattr(reader, "_create_runtime_client", lambda: bridge_fixture.FakeClient(rows))
    resolver = build_question_history_resolver(
        frame=bridge_fixture.frame(as_of=later),
        sources=(RetainedHistorySource("history", current, (candidate,)),),
        rules=bridge_fixture.analogue_rules(),
        limit=1,
    )
    m, store, plan = fixture(monkeypatch)
    plan["requirements"].append(history_requirement())
    result = build_with_history(m, store, plan, resolver)
    assert result["status"] == "refused"
    assert result["reason"] == "history_resolution_invalid"
    assert result["snapshot"] is None
    assert store._objects.writes == 0


def test_the_legacy_pipeline_holds_beside_a_committed_bridge_row(monkeypatch, tmp_path):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    test_fixed_pipeline_persists_exact_snapshot_and_idempotent_readback(monkeypatch)


def test_a_trial_pin_row_left_in_the_registry_opens_the_bridge_route(monkeypatch, tmp_path):
    """The legacy pipeline runs only while nothing is registered: over the real committed
    document with a trial pin row left in, the bridge row is read and the pipeline takes
    the bridge context route instead, which refuses until a bridge provider is served."""
    from src.analysis.open_intelligence import protected_context_registry

    from tests.unit.test_protected_context_registry import trial_pin

    read_bridge_rows = protected_context_registry.bridge_entries
    m, store, plan = fixture(monkeypatch)
    monkeypatch.setattr(protected_context_registry, "bridge_entries", read_bridge_rows)
    _w, row = trial_pin(monkeypatch, tmp_path)
    assert plan["window"]["end"] <= row["cutoff_date"]
    assert build(m, store, plan) == {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "coverage_gap",
        "snapshot": None,
        "reason": "coverage_incomplete",
        "missing_work": ["bridge_clients_unavailable"],
    }
    # A window past the trial row's cutoff is not covered by it.
    later = date.fromisoformat(row["cutoff_date"]) + timedelta(days=1)
    plan["window"]["end"] = later.isoformat()
    result = build(m, store, plan)
    assert (result["status"], result["missing_work"]) == (
        "coverage_gap",
        ["bridge_source_uncovered"],
    )
    assert store._objects.writes == 0
