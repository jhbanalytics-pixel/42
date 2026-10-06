"""Contract tests for server-owned Gemini canary plan approvals."""

from __future__ import annotations

import runpy
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

MANIFEST_SHA = "0f90586306fae9932b88424a16699182e4fc3ecbda47015a63bb3491dbb833c7"
ACTOR_ID = "user_fixture_001"
APPROVED_AT = datetime(2026, 8, 28, 12, tzinfo=UTC)


def approval_module():
    from src.analysis.open_intelligence import canary_plan_approvals

    return canary_plan_approvals


def planning_outputs():
    from src.analysis.open_intelligence.canary_manifests import load_canonical_manifest

    build = runpy.run_path("tests/unit/test_gemini_canary_callers.py")["planning_output"]
    manifest = load_canonical_manifest()
    intents = {item["task_id"]: item["expected_intent"] for item in manifest["tasks"]}
    output = {}
    for stage in manifest["stages"]:
        if stage["stage"] == "planning":
            plan = build(stage["envelope"])
            plan["intent"] = intents[stage["task_id"]]
            plan["human_review_required"] = stage["envelope"]["context_index"][
                "human_review_required"
            ]
            if plan["human_review_required"]:
                for requirement in plan["claim_requirements"]:
                    requirement["human_review_required"] = True
            output[stage["task_id"]] = plan
    return output


def request(module, **overrides):
    values = {
        "manifest_sha256": MANIFEST_SHA,
        "task_ids": module.TASK_IDS,
        "approval_phrase": module.APPROVAL_PHRASE,
    }
    values.update(overrides)
    return module.ApprovalActionRequest(**values)


def actor(module, **overrides):
    values = {
        "stable_user_id": ACTOR_ID,
        "source": "authenticated_application_user_id",
    }
    values.update(overrides)
    return module.AuthenticatedServerActor(**values)


def execute(module, *, plans=None, store=None, action_request=None, actor_value=None, clock=None):
    plan_store = module.FixtureCanaryPlanStore(plans or planning_outputs())
    approval_store = store or module.MemoryCanaryPlanApprovalStore()
    companion = module.create_approval_companion(
        action_request or request(module),
        plan_store=plan_store,
        approval_store=approval_store,
        resolve_actor=lambda: actor_value or actor(module),
        utc_now=clock or (lambda: APPROVED_AT),
    )
    return companion, plan_store, approval_store


def test_exact_eleven_plan_batch_reproduces_record_and_companion_ids():
    module = approval_module()

    companion, _plans, store = execute(module)

    assert companion.task_count == 11
    assert companion.task_ids == module.TASK_IDS
    assert tuple(record.plan_id for record in companion.approval_records) == module.PLAN_IDS
    assert {record.approved_by for record in companion.approval_records} == {ACTOR_ID}
    assert {record.approved_at for record in companion.approval_records} == {
        "2026-08-28T12:00:00.000000Z"
    }
    assert companion.approval_records[0].plan_digest == (
        "de4f25e22c975b89bfa5f888383bb6758e97e64b98b61a503b1319d6595f0020"
    )
    assert companion.approval_records[0].approval_id == (
        "apr_255a091243df65db2f7820d169fcbf14007dbd4d6d88cd11eadfdb4f5e340c6b"
    )
    assert companion.approval_records[-1].approval_id == (
        "apr_b4da9e7eb3966967dd9025e2562f69897c0e6ae9b18e32e1deb64ed538748469"
    )
    assert companion.companion_sha256 == (
        "ebc3ab97fac8abe0d3e71beb974218b54a19b36abbb92a1487cf3f59ac7bb720"
    )
    stored = store.read_manifest(MANIFEST_SHA)
    assert stored.companion_sha256 == companion.companion_sha256
    assert stored.canonical_companion_json == module.canonical_companion_json(companion)
    assert all(store.read_approval(item.approval_id) == item for item in companion.approval_records)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "duplicate", "reordered", "renamed", "wrong_manifest"],
)
def test_batch_scope_and_manifest_mutations_fail_without_storage(mutation):
    module = approval_module()
    task_ids = list(module.TASK_IDS)
    manifest = MANIFEST_SHA
    if mutation == "missing":
        task_ids.pop()
    elif mutation == "extra":
        task_ids.append("golden_12_extra")
    elif mutation == "duplicate":
        task_ids[-1] = task_ids[0]
    elif mutation == "reordered":
        task_ids.reverse()
    elif mutation == "renamed":
        task_ids[0] = "golden_01_renamed"
    else:
        manifest = "f" * 64
    store = module.MemoryCanaryPlanApprovalStore()

    with pytest.raises(module.CanaryApprovalFailure):
        execute(
            module,
            store=store,
            action_request=request(module, task_ids=tuple(task_ids), manifest_sha256=manifest),
        )
    assert store.count == 0


def test_action_request_has_no_actor_time_plan_or_requirement_inputs():
    module = approval_module()

    for field in (
        "approved_by",
        "email",
        "display_name",
        "approved_at",
        "timestamp",
        "approval_id",
        "plan_digest",
        "plan",
        "claim_requirements",
    ):
        with pytest.raises(TypeError):
            module.ApprovalActionRequest(
                manifest_sha256=MANIFEST_SHA,
                task_ids=module.TASK_IDS,
                approval_phrase=module.APPROVAL_PHRASE,
                **{field: "forged"},
            )


def test_phrase_actor_and_server_clock_are_exact_and_server_owned():
    module = approval_module()
    store = module.MemoryCanaryPlanApprovalStore()
    with pytest.raises(module.CanaryApprovalFailure, match="phrase"):
        execute(module, store=store, action_request=request(module, approval_phrase="approve"))
    assert store.count == 0

    for bad_actor in (
        actor(module, stable_user_id="person@example.com"),
        actor(module, source="request_body"),
        actor(module, stable_user_id=""),
    ):
        with pytest.raises(module.CanaryApprovalFailure, match="actor"):
            execute(module, actor_value=bad_actor)

    for bad_time in (
        datetime(2026, 8, 28, 12),
        datetime(2026, 8, 28, 14, tzinfo=timezone(timedelta(hours=2))),
    ):
        with pytest.raises(module.CanaryApprovalFailure, match="timestamp"):
            execute(module, clock=lambda value=bad_time: value)


@pytest.mark.parametrize(
    "mutation",
    ["missing_plan", "plan_id", "claim_order", "stale_plan"],
)
def test_planning_outputs_and_claim_requirements_are_exact(mutation):
    module = approval_module()
    plans = planning_outputs()
    first = module.TASK_IDS[0]
    if mutation == "missing_plan":
        plans.pop(first)
    elif mutation == "plan_id":
        plans[first]["plan_id"] = "different_plan"
    elif mutation == "claim_order":
        plans[first]["claim_requirements"] = list(reversed(plans[first]["claim_requirements"]))
        plans[first]["claim_requirements"].append(plans[first]["claim_requirements"][0])
    else:
        plans[first]["decision"] = "Changed stored plan"

    with pytest.raises(module.CanaryApprovalFailure):
        execute(module, plans=plans)


def test_store_append_is_atomic_insert_only_and_duplicate_safe():
    module = approval_module()
    companion, _plans, store = execute(module)

    with pytest.raises(module.CanaryApprovalFailure, match="conflict"):
        store.append_batch(module.canonical_companion_json(companion), companion.companion_sha256)
    assert store.count == 1
    assert not hasattr(store, "update")
    assert not hasattr(store, "delete")

    broken_json = module.canonical_companion_json(companion).replace(
        companion.approval_records[0].approval_id,
        companion.approval_records[1].approval_id,
        1,
    )
    fresh = module.MemoryCanaryPlanApprovalStore()
    with pytest.raises(module.CanaryApprovalFailure):
        fresh.append_batch(broken_json, companion.companion_sha256)
    assert fresh.count == 0


def test_companion_and_readback_tamper_fail_before_answer_enablement():
    module = approval_module()
    companion, plans, store = execute(module)
    first = module.TASK_IDS[0]

    loaded = module.load_approved_plan(
        manifest_sha256=MANIFEST_SHA,
        task_id=first,
        plan_store=plans,
        approval_store=store,
    )
    assert loaded.record == companion.approval_records[0]
    assert loaded.approved_claim_ids == tuple(
        item["claim_id"] for item in planning_outputs()[first]["claim_requirements"]
    )

    changed = planning_outputs()
    changed[first]["claim_requirements"][0]["question"] = "Changed question"
    with pytest.raises(module.CanaryApprovalFailure, match="plan_digest"):
        module.load_approved_plan(
            manifest_sha256=MANIFEST_SHA,
            task_id=first,
            plan_store=module.FixtureCanaryPlanStore(changed),
            approval_store=store,
        )

    stored = store.read_manifest(MANIFEST_SHA)
    store._companions[MANIFEST_SHA] = replace(stored, companion_sha256="f" * 64)
    with pytest.raises(module.CanaryApprovalFailure, match="readback"):
        module.load_approved_plan(
            manifest_sha256=MANIFEST_SHA,
            task_id=first,
            plan_store=plans,
            approval_store=store,
        )


def test_import_and_proposal_approval_create_no_records():
    module = approval_module()

    assert module.MemoryCanaryPlanApprovalStore().count == 0
    assert not hasattr(module, "DEFAULT_COMPANION")


@pytest.mark.parametrize("mutation", ["manifest", "actor", "timestamp"])
def test_store_rejects_rehashed_record_scope_or_batch_identity_drift(mutation):
    module = approval_module()
    companion, _plans, _store = execute(module)
    records = list(companion.approval_records)
    changes = {
        "manifest": {"manifest_sha256": "e" * 64},
        "actor": {"approved_by": "different_user"},
        "timestamp": {"approved_at": "2026-08-28T12:00:01.000000Z"},
    }[mutation]
    changed = replace(records[0], **changes, approval_id="")
    records[0] = replace(changed, approval_id=module._record_id(changed))
    changed_companion = replace(companion, approval_records=tuple(records), companion_sha256="")
    changed_companion = replace(
        changed_companion,
        companion_sha256=module._companion_digest(changed_companion),
    )
    store = module.MemoryCanaryPlanApprovalStore()

    with pytest.raises(module.CanaryApprovalFailure):
        store.append_batch(
            module.canonical_companion_json(changed_companion),
            changed_companion.companion_sha256,
        )
    assert store.count == 0


def test_correction_appends_new_manifest_without_overwriting_old_companion():
    module = approval_module()
    original, _plans, store = execute(module)
    new_manifest = "e" * 64
    records = []
    for record in original.approval_records:
        changed = replace(record, manifest_sha256=new_manifest, approval_id="")
        records.append(replace(changed, approval_id=module._record_id(changed)))
    correction = replace(
        original,
        manifest_sha256=new_manifest,
        approval_records=tuple(records),
        companion_sha256="",
    )
    correction = replace(correction, companion_sha256=module._companion_digest(correction))

    store.append_batch(module.canonical_companion_json(correction), correction.companion_sha256)

    assert store.count == 2
    assert store.read_manifest(MANIFEST_SHA).companion_sha256 == original.companion_sha256
    assert store.read_manifest(new_manifest).companion_sha256 == correction.companion_sha256


def test_answer_enables_only_with_exact_companion_readback(monkeypatch):
    from datetime import date

    from src.analysis.open_intelligence import canary_runtime
    from src.analysis.open_intelligence.canary_manifests import stage_manifest
    from src.analysis.open_intelligence.canary_policy import resolve_canary_policy
    from src.analysis.open_intelligence.canary_receipts import MemoryCanaryReceiptSink
    from src.analysis.open_intelligence.open_question_answer import answer_open_question
    from src.contracts.open_intelligence_budget import OpenIntelligenceRunBudget

    module = approval_module()
    _companion, plans, store = execute(module)
    task_id = module.TASK_IDS[0]
    approved = module.load_approved_plan(
        manifest_sha256=MANIFEST_SHA,
        task_id=task_id,
        plan_store=plans,
        approval_store=store,
    )
    stage = stage_manifest(f"open_question_answer:answering:{task_id}")
    ns = runpy.run_path("tests/unit/test_gemini_canary_callers.py")
    client = ns["FakeClient"](ns["answering_output"](stage["envelope"]))
    policy = resolve_canary_policy("open_question_answer", "answering", "canary")
    monkeypatch.setattr(canary_runtime, "_utc_now", lambda: APPROVED_AT)

    result = answer_open_question(
        stage["envelope"],
        approved_plan=approved,
        lane="canary",
        model=policy.model,
        policy=policy,
        sdk_client=client,
        budget=OpenIntelligenceRunBudget(
            run_id=task_id,
            consumer="open_question_answer",
            trend_date=date(2026, 8, 25),
            market="za",
        ),
        persist_usage=lambda event: event,
        receipt_sink=MemoryCanaryReceiptSink(),
    )

    assert result.parsed["plan_id"] == approved.plan_id
    assert [name for name, _kwargs in client.models.calls] == ["count", "generate"]
