"""A bridge capture recorded on the approval ledger is read beside the daily chain route.

The generation, approval, consumption, result, native jobs and pins here are synthetic and
unissued. The ledger records are typed through the real v2 record classes under a test
generation that no packaged catalogue trusts.
"""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.source_estate_bridge_plan import build_bridge_plan

from tests.unit import test_bridge_history_loader as chain_route
from tests.unit.bridge_ledger_fixture import (
    CONSUMED_AT,
    bridge_registry,
    clone_readback,
    creation,
    initial_job_id,
    ledger_capture,
    ledger_manifest,
    manifest_sha256,
)
from tests.unit.test_execution_manifest_origins import IMAGE_URI, manifest
from tests.unit.test_execution_records_v2 import RESOURCE_SHA
from tests.unit.test_protected_context_registry import fixture_document, local
from tests.unit.test_source_estate_bridge_contract import capture_payload, with_artifacts

LEDGER_READS = (
    "bridge_policy",
    "capture_plan",
    "collection_receipt_set",
    "history_completion_set",
    "source_metadata",
    "storage_policy",
    "temporal_rules",
)
AVAILABLE = "2026-09-21T00:26:50.000000Z"
FUNDED_JOB = (
    "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-funded-pilot-staging"
)
FUNDED_EXECUTION_ID = "intelligence-42-funded-pilot-staging-x7k2q"
FUNDED_NAME = f"{FUNDED_JOB}/executions/{FUNDED_EXECUTION_ID}"
FUNDED_IDENTITY = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
FUNDED_APPROVED = datetime(2026, 9, 21, 0, 0, 10, tzinfo=UTC)
FUNDED_CONSUMED = datetime(2026, 9, 21, 0, 1, tzinfo=UTC)
FUNDED_RECORDED = datetime(2026, 9, 21, 0, 14, 30, tzinfo=UTC)
FUNDED_STARTED = "2026-09-21T00:00:45.000000Z"
FUNDED_COMPLETED = "2026-09-21T00:15:30.123456789Z"
COLLECTION_STARTED = "2026-09-21T00:01:30.000000Z"
COLLECTION_COMPLETED = "2026-09-21T00:14:00.000000Z"
# The pipeline run id the pilot stamps into every raw_content and enriched_content row it
# writes and into its GDELT persistence proof, synthetic.
FUNDED_RUN = "5f0c2a9e-3b1d-4c7a-9e2f-8d6b4a1c0e7f"
# A second execution of the funded pilot inside the same capture window, and the pipeline
# run id it stamps, synthetic.
SECOND_EXECUTION_ID = "intelligence-42-funded-pilot-staging-b9m4t"
SECOND_RUN = "2d7e9b41-6a3c-4f58-b0e2-7c1a9d3f5e64"
# The funded close payload the pilot records as its result, synthetic and unissued.
FUNDED_CLOSE = {
    "attribution_state": "complete",
    "calls": 3,
    "budget_debit_credits": "3",
    "source_values_state": "complete",
    "source_values": {},
    "gdelt": {"persistence": {"complete": True, "run_id": FUNDED_RUN}},
}
REF_FIELDS = (
    "operation",
    "consumption_id",
    "manifest_sha256",
    "result_id",
    "result_digest",
    "origin_registry_sha256",
    "resource_manifest_sha256",
)


def funded_chain(registry, *, manifest_changes=None, payload=None, **changes):
    """One Wave 1 pilot execution's approval, consumption and result row, synthetic.

    The pilot records its funded close as a v2 execution result whose reference is the
    execution's name with ``#funded-run-close``; it records no collection receipt.
    """
    value = manifest("wave1_pilot")
    value["expires_at"] = "2026-09-21T12:00:00.000000Z"
    value.update(manifest_changes or {})
    arguments = {
        "approved_at": FUNDED_APPROVED,
        "consumed_at": FUNDED_CONSUMED,
        "completed_at": FUNDED_RECORDED,
        "operation": "wave1_pilot",
        "execution_id": FUNDED_EXECUTION_ID,
        "reference_suffix": "#funded-run-close",
        **changes,
    }
    return ledger_capture(
        registry, value, FUNDED_CLOSE if payload is None else payload, **arguments
    )


def funded_ref(chain):
    """The approval ledger reference of a funded chain's result."""
    result = chain["result"]
    return {name: getattr(result, name) for name in REF_FIELDS}


def receipt_item(chain, **changes):
    """The collection receipt row an approved receipt set carries for a funded execution."""
    return {
        "run_id": chain["consumption"].execution_name.rsplit("/", 1)[1],
        "result_ref": funded_ref(chain),
        "collection_receipt_digest": chain["result"].result_digest,
        "collection_started_at": COLLECTION_STARTED,
        "collection_completed_at": COLLECTION_COMPLETED,
        **changes,
    }


def funded_execution(
    state="succeeded",
    *,
    execution_id=FUNDED_EXECUTION_ID,
    started=FUNDED_STARTED,
    completed=FUNDED_COMPLETED,
    image=IMAGE_URI,
    **changes,
):
    """One execution of the funded pilot job as a Cloud Run v2 GET returns it, synthetic."""
    value = {
        "name": f"{FUNDED_JOB}/executions/{execution_id}",
        "job": "intelligence-42-funded-pilot-staging",
        "taskCount": 1,
        "parallelism": 1,
        "template": {
            "serviceAccount": FUNDED_IDENTITY,
            "maxRetries": 0,
            "timeout": "900s",
            "containers": [
                {
                    "image": image,
                    "command": ["python"],
                    "args": ["scripts/run_rss_now.py"],
                    "env": [
                        {"name": "BIGQUERY_DATASET", "value": "trends_v2_staging"},
                        {"name": "GCP_PROJECT", "value": "ogilvy-trends-v2"},
                        {"name": "SOCIALCRAWL_CREDENTIAL_LANE", "value": "ogilvy_funded"},
                        {"name": "SOCIALCRAWL_FUNDED_STAGE_NAME", "value": "stage_1_wave_1"},
                        {"name": "TRENDS_ENV", "value": "staging"},
                        {
                            "name": "SOCIALCRAWL_OGILVY_API_KEY",
                            "valueSource": {
                                "secretKeyRef": {
                                    "secret": "SOCIALCRAWL_OGILVY_API_KEY",
                                    "version": "1",
                                }
                            },
                        },
                    ],
                }
            ],
        },
        "createTime": "2026-09-21T00:00:40.000000Z",
        "startTime": started,
    }
    if state != "running":
        condition = {"type": "Completed", "lastTransitionTime": completed}
        if state == "succeeded":
            condition["state"] = "CONDITION_SUCCEEDED"
            value["succeededCount"] = 1
        elif state == "failed":
            condition.update(state="CONDITION_FAILED", executionReason="NON_ZERO_EXIT_CODE")
            value["failedCount"] = 1
        else:
            condition.update(state="CONDITION_FAILED", executionReason="CANCELLED")
            value["cancelledCount"] = 1
        value.update(
            completionTime=completed,
            conditions=[{"type": "Started", "state": "CONDITION_SUCCEEDED"}, condition],
        )
    value.update(changes)
    return value


class LedgerEvidence:
    """The chain evidence of the loader world, with the funded executions served natively.

    Each served execution is the raw Cloud Run resource, read through the core native view.
    """

    def __init__(self, evidence, executions):
        self._evidence = evidence
        self.executions = executions
        self.execution_reads = []

    def __getattr__(self, name):
        return getattr(self._evidence, name)

    def read_funded_execution(self, execution_id):
        from src.analysis.open_intelligence.daily_child_execution import (
            native_funded_execution_view,
        )

        self.execution_reads.append(execution_id)
        return native_funded_execution_view(deepcopy(self.executions[execution_id]))


def ledger_world(
    tmp_path,
    *,
    jobs_changes=None,
    payload_changes=None,
    plan_change=None,
    rule_arguments=None,
    recovery_context=None,
    job_id=None,
    funded_changes=None,
    receipt_changes=None,
    second_run=None,
    **capture_changes,
):
    """A sound ledger capture; ``plan_change`` forges the approved plan itself, so the
    approved bytes, the payload digests, the jobs and the clones all agree with the forgery.
    ``funded_changes`` changes the funded pilot chain its one collection receipt names, and
    ``receipt_changes`` that receipt, the same way. ``second_run`` adds a receipt for a
    second funded execution whose close names that pipeline run.
    """
    w = chain_route.world()
    registry, contract = bridge_registry(tmp_path, rule_arguments)
    # The ledger capture's one collection receipt names an execution of the funded pilot,
    # bound to that execution's result row on the approval ledger.
    funded = funded_chain(registry, **(funded_changes or {}))
    inputs = deepcopy(w["inputs"])
    artifacts = inputs["artifacts"]
    artifacts["collection_receipt_set"]["receipts"] = [
        receipt_item(funded, **(receipt_changes or {}))
    ]
    second = None
    if second_run is not None:
        close = deepcopy(FUNDED_CLOSE)
        close["gdelt"]["persistence"]["run_id"] = second_run
        second = funded_chain(registry, payload=close, execution_id=SECOND_EXECUTION_ID)
        artifacts["collection_receipt_set"]["receipts"] = sorted(
            [*artifacts["collection_receipt_set"]["receipts"], receipt_item(second)],
            key=lambda item: item["run_id"],
        )
    inputs["profile"] = with_artifacts(inputs["profile"], artifacts)
    plan = build_bridge_plan(**inputs)
    raw = {name: canonical_bytes(value) for name, value in artifacts.items()}
    raw["capture_plan"] = canonical_bytes(plan)
    if plan_change is not None:
        plan_change(plan)
        raw["capture_plan"] = canonical_bytes(plan)
    raw["source_metadata"] = canonical_bytes(inputs["source_metadata"])
    raw["storage_policy"] = canonical_bytes(inputs["storage_policy"])
    raw["capture_contract"] = canonical_bytes({"contract_version": "capture_contract_v1"})
    # An initial capture recovers nothing: its recovery context is the canonical null.
    raw["recovery_context"] = canonical_bytes(recovery_context)
    arguments = {"cutoff": "2026-09-20", "grant_id": plan["snapshot_plan"]["grant_id"]}
    arguments.update(capture_changes.pop("arguments", {}))
    manifest = ledger_manifest(contract, raw, **arguments)
    # Each creation job is named for the approved manifest, as an initial capture names it.
    owner = manifest_sha256(registry, manifest)
    jobs, records = creation(
        plan,
        job_id=job_id or (lambda lane: initial_job_id(owner, lane)),
        **(jobs_changes or {}),
    )
    readback = clone_readback(plan)
    payload = capture_payload(
        plan["snapshot_plan"],
        creation_records=records,
        capture_receipt_digest=canonical_digest(records),
        snapshot_plan_digest=canonical_digest(plan),
        snapshot_digest=canonical_digest(readback["capture_facts"]),
    )
    payload.update(payload_changes or {})
    ledger = ledger_capture(registry, manifest, payload, **capture_changes)
    clones = {**readback, "native_jobs": jobs}
    executions = {FUNDED_EXECUTION_ID: funded_execution()}
    if second is not None:
        executions[SECOND_EXECUTION_ID] = funded_execution(execution_id=SECOND_EXECUTION_ID)
    return {
        **w,
        "inputs": inputs,
        "funded": funded,
        "funded_second": second,
        "executions": executions,
        "evidence": LedgerEvidence(w["evidence"], executions),
        "plan": plan,
        "ledger_raw": raw,
        "jobs": jobs,
        "clones": clones,
        "ledger": ledger,
    }


def ledger_rows(w):
    chains = {
        w[key]["consumption"].consumption_id: w[key]["rows"]
        for key in ("ledger", "funded", "funded_second")
        if w.get(key) is not None
    }

    def read(consumption_id):
        w.setdefault("ledger_reads", []).append(consumption_id)
        return deepcopy(chains.get(consumption_id, []))

    return read


def job_reader(w, jobs=None):
    served = w["clones"]["native_jobs"] if jobs is None else jobs

    def read(job_id):
        w.setdefault("job_reads", []).append(job_id)
        return deepcopy(served.get(job_id))

    return read


def render(w):
    from scripts.staging.render_protected_context_entry import render_entry

    return json.loads(render_entry(w["ledger"]["rows"], generation_loader=w["ledger"]["catalogue"]))


def register(monkeypatch, tmp_path, w, **changes):
    entry = dict(render(w), **changes)
    value = fixture_document()
    value["entries"].append(entry)
    local(monkeypatch, tmp_path, value)
    return entry


def pin_row(monkeypatch, tmp_path, w):
    """A row rendered from a sound capture of the same day, pinned to ``w``'s ledger result."""
    entry = render(ledger_world(tmp_path / "sound"))
    result = w["ledger"]["result"]
    value = fixture_document()
    value["entries"].append(
        dict(
            entry,
            consumption_id=result.consumption_id,
            manifest_sha256=result.manifest_sha256,
            result_digest=result.result_digest,
            result_id=result.result_id,
        )
    )
    local(monkeypatch, tmp_path, value)


def load(w, **overrides):
    from src.analysis.open_intelligence.general_question_context_admission import (
        load_bridge_history_source,
    )

    arguments = {
        "window_end": "2026-09-20",
        "request_as_of": chain_route.AS_OF,
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ng", "za"],
        "capture_clients": None,
        "read_input": chain_route.reader(w["ledger_raw"]),
        "read_grant": None,
        "evidence": w["evidence"],
        "now": chain_route.NOW,
        "ledger_reader": ledger_rows(w),
        "generation_loader": w["ledger"]["catalogue"],
        **chain_route.clone_readers(w["clones"]),
        "read_native_job": job_reader(w),
    }
    arguments.update(overrides)
    return load_bridge_history_source(**arguments)


def native(w):
    """What the loader reads natively for the validator: facts, jobs and clone tables."""
    return deepcopy(w["clones"])


def refused(w, **overrides):
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w, **overrides)


def test_rendered_ledger_row_is_a_bridge_row_pinned_to_the_ledger_result(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import protected_context_registry as r

    w = ledger_world(tmp_path)
    entry = register(monkeypatch, tmp_path, w)
    result = w["ledger"]["result"]
    assert entry == {
        "consumption_id": result.consumption_id,
        "cutoff_date": "2026-09-20",
        "manifest_sha256": result.manifest_sha256,
        "market_scope": ["ke", "ng", "za"],
        "profile_id": "staging_bridge_v3_20260920",
        "result_contract_version": "open_intelligence_execution_result_v2",
        "result_digest": result.result_digest,
        "result_id": result.result_id,
        "snapshot_tables": [
            relation["destination_table"]
            for relation in w["plan"]["snapshot_plan"]["relation_bindings"]
        ],
        "source_as_of": "2026-09-21T00:00:00+00:00",
    }
    rows = r.bridge_entries()
    assert [row.profile_id for row in rows] == ["staging_bridge_v3_20260920"]
    assert r.bridge_authority(rows[0]) == "execution_ledger"
    assert r.allowed_cutoffs() == ("2026-09-07",)
    assert [item["profile_id"] for item in r._PROTECTED_CONTEXT_PROFILES] == [
        "protected_context_20260907_v1"
    ]


def test_chain_rows_keep_their_own_authority(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import protected_context_registry as r

    w = chain_route.world()
    chain_route.register(monkeypatch, tmp_path, w["capture"])
    assert r.bridge_authority(r.bridge_entries()[0]) == "daily_chain"
    with pytest.raises(ValueError, match=r"^protected_context_registry_invalid$"):
        r.bridge_authority(r.profile_for("2026-09-07"))


def test_loader_admits_a_ledger_capture_through_its_manifest_and_native_jobs(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.source_estate_bridge import REGISTRY_FIELDS

    w = ledger_world(tmp_path)
    entry = register(monkeypatch, tmp_path, w)
    log = []
    loaded = load(w, read_input=chain_route.reader(w["ledger_raw"], log))
    assert sorted(log) == sorted(LEDGER_READS)
    # The receipt's own funded chain is read from the ledger after the capture's.
    assert w["ledger_reads"] == [
        entry["consumption_id"],
        w["funded"]["consumption"].consumption_id,
    ]
    assert sorted(w["job_reads"]) == sorted(w["jobs"])
    assert loaded.profile_id == "staging_bridge_v3_20260920"
    assert loaded.registry_entry == {key: entry[key] for key in REGISTRY_FIELDS}
    binding = loaded.binding
    assert binding["registry_entry_digest"] == canonical_digest(loaded.registry_entry)
    assert binding["result_id"] == w["ledger"]["result"].result_id
    assert binding["result_digest"] == w["ledger"]["result"].result_digest
    assert binding["available_at"] == AVAILABLE
    assert binding["market_scope"] == ["ng", "za"]
    assert [(c["lane"], c["market"], c["product_date"], c["state"]) for c in loaded.cells] == [
        ("trend_analysis", "ng", "2026-09-19", "completed"),
        ("trend_analysis", "za", "2026-09-19", "completed"),
    ]
    assert {item["reason_code"] for item in loaded.limitations} == {
        "product_pending_at_cutoff",
        "native_completion_unrecorded",
    }
    assert loaded.collection_runs == (FUNDED_EXECUTION_ID,)
    assert w["evidence"].execution_reads == [FUNDED_EXECUTION_ID]


def test_a_clone_with_neither_a_chain_nor_an_approval_is_refused(monkeypatch, tmp_path):
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    refused(w, ledger_reader=lambda _consumption_id: [])
    refused(w, ledger_reader=None)
    other = ledger_world(tmp_path / "other", consumed_at=CONSUMED_AT.replace(second=30))
    refused(w, ledger_reader=lambda _consumption_id: deepcopy(other["ledger"]["rows"]))


def test_a_ledger_row_cannot_stand_in_for_a_chain_row(monkeypatch, tmp_path):
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    refused(
        w,
        capture_clients=w["capture_clients"],
        read_grant=chain_route.grant_reader(w["grant"]),
        ledger_reader=None,
        read_native_job=None,
    )
    chain = chain_route.world()
    chain_route.register(monkeypatch, tmp_path, chain["capture"])
    for overrides in (
        {"capture_clients": None},
        {
            "capture_clients": None,
            "read_grant": None,
            "ledger_reader": ledger_rows(w),
            "generation_loader": w["ledger"]["catalogue"],
            "read_native_job": job_reader(w),
        },
    ):
        with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
            chain_route.load(chain, **overrides)


def test_the_generation_must_be_one_the_catalogue_trusts(monkeypatch, tmp_path):
    from tests.unit.test_retained_readers_v2 import FakeCatalogue

    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    registry = w["ledger"]["catalogue"].registry
    refused(w, generation_loader=FakeCatalogue(registry, (registry.sha256, "f" * 64)))
    refused(w, generation_loader=FakeCatalogue(registry, ("f" * 64, RESOURCE_SHA)))


def test_a_failed_execution_admits_nothing(monkeypatch, tmp_path):
    w = ledger_world(tmp_path, status="failed")
    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        render(w)
    pin_row(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize(
    "identity", ["intelligence-42-daily@ogilvy-trends-v2.iam.gserviceaccount.com", None]
)
def test_clones_made_by_another_principal_are_refused(monkeypatch, tmp_path, identity):
    w = ledger_world(tmp_path, jobs_changes={"identity": identity})
    register(monkeypatch, tmp_path, w)
    refused(w)


def test_a_native_job_that_differs_from_its_recorded_digest_is_refused(monkeypatch, tmp_path):
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    jobs = deepcopy(w["jobs"])
    first = sorted(jobs)[0]
    jobs[first]["statistics"]["totalBytesProcessed"] = "1"
    refused(w, read_native_job=job_reader(w, jobs))
    missing = dict(w["jobs"])
    del missing[first]
    refused(w, read_native_job=job_reader(w, missing))
    refused(w, read_native_job=None)


@pytest.mark.parametrize(
    "times",
    [
        {"started": CONSUMED_AT.replace(minute=21, second=30)},
        {"ended": CONSUMED_AT.replace(minute=25, second=30)},
    ],
    ids=["started_before_consumption", "ended_after_capture"],
)
def test_jobs_outside_the_consume_to_capture_window_are_refused(monkeypatch, tmp_path, times):
    w = ledger_world(tmp_path, jobs_changes=times)
    register(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize(
    "change",
    [
        {"status": {"state": "DONE", "errorResult": {"reason": "invalid"}}},
        {"status": {"state": "RUNNING"}},
        {"jobReference": {"projectId": "ogilvy-trends-v2", "location": "EU", "jobId": "x"}},
    ],
    ids=["errored", "running", "other_reference"],
)
def test_jobs_that_did_not_finish_the_recorded_clone_are_refused(monkeypatch, tmp_path, change):
    from tests.unit import bridge_ledger_fixture

    original = bridge_ledger_fixture.native_job

    def changed(*args, **kwargs):
        return dict(original(*args, **kwargs), **deepcopy(change))

    monkeypatch.setattr(bridge_ledger_fixture, "native_job", changed)
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    refused(w)


def test_a_statement_other_than_the_plan_s_is_refused(monkeypatch, tmp_path):
    w = ledger_world(tmp_path)
    plan = deepcopy(w["plan"])
    for statement in plan["creation_statements"]:
        statement["sql"] = statement["sql"].replace("CLONE", "COPY")
    owner = w["ledger"]["approval"].manifest_sha256
    jobs, records = creation(plan, job_id=lambda lane: initial_job_id(owner, lane))
    w = ledger_world(
        tmp_path / "copy",
        payload_changes={
            "creation_records": records,
            "capture_receipt_digest": canonical_digest(records),
        },
    )
    w["clones"]["native_jobs"] = jobs
    register(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize("name", LEDGER_READS)
def test_artifacts_are_the_bytes_the_approved_manifest_digests(monkeypatch, tmp_path, name):
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    raw = dict(w["ledger_raw"])
    raw[name] = raw[name] + b" "
    refused(w, read_input=chain_route.reader(raw))


@pytest.mark.parametrize(
    "arguments",
    [{"grant_id": "another_grant"}, {"cutoff": "2026-09-19"}],
    ids=["grant", "cutoff"],
)
def test_the_approved_vector_names_the_plan_s_cutoff_and_grant(monkeypatch, tmp_path, arguments):
    w = ledger_world(tmp_path, arguments=arguments)
    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        render(w)
    pin_row(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize(
    "payload_changes",
    [
        {"capture_receipt_digest": "0" * 64},
        {"snapshot_plan_digest": "0" * 64},
        {"missing_checks": ["native_job_unread"]},
        {"captured_at": "2026-09-21T00:21:00.000000Z"},
        {"client_scope_id": "fixture_scope"},
        {"grant_id": "another_grant"},
    ],
    ids=["receipt", "plan", "missing", "captured_before_consumed", "scope", "grant"],
)
def test_a_result_that_does_not_bind_the_approved_plan_is_refused(
    monkeypatch, tmp_path, payload_changes
):
    w = ledger_world(tmp_path, payload_changes=payload_changes)
    pin_row(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize(
    "field", ["consumption_id", "manifest_sha256", "result_digest", "result_id"]
)
def test_a_registry_row_that_does_not_pin_the_ledger_result_refuses(monkeypatch, tmp_path, field):
    w = ledger_world(tmp_path)
    prefix = {"consumption_id": "exc_", "result_id": "exr_"}.get(field, "")
    register(monkeypatch, tmp_path, w, **{field: prefix + "0" * 64})
    refused(w)


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_as_of": "2026-09-21T00:26:00.000000Z"},
        {"now": "2026-09-21T00:59:00.000000Z"},
        {"now": "2026-12-20T00:00:00.000000Z"},
        {"market_scope": ["gh"]},
        {"client_scope_id": "fixture_scope"},
    ],
)
def test_request_scope_and_clock_refusals_hold_on_the_ledger_route(
    monkeypatch, tmp_path, overrides
):
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    with pytest.raises(ValueError, match=r"^bridge_source_invalid$"):
        load(w, **overrides)


def test_the_ledger_route_still_checks_every_receipt_and_history_entry(monkeypatch, tmp_path):
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    w["evidence"].rows[0]["term"] = "substituted"
    refused(w)
    w = ledger_world(tmp_path / "run")
    register(monkeypatch, tmp_path, w)
    # On the ledger route a receipt is bound to its funded execution, not a daily chain.
    w["executions"][FUNDED_EXECUTION_ID] = funded_execution(
        execution_id="intelligence-42-funded-pilot-staging-other"
    )
    refused(w)


def test_the_ledger_binding_validator_takes_only_a_capture_the_reader_issued(monkeypatch, tmp_path):
    from src.analysis.open_intelligence.bridge_ledger_binding import (
        read_ledger_capture,
        validate_ledger_read_binding,
    )

    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    loaded = load(w)
    capture = read_ledger_capture(
        consumption_id=w["ledger"]["consumption"].consumption_id,
        ledger_rows=w["ledger"]["rows"],
        generation_loader=w["ledger"]["catalogue"],
        read_input=chain_route.reader(w["ledger_raw"]),
    )
    checked = validate_ledger_read_binding(
        loaded.binding, capture=capture, now=chain_route.NOW, **native(w)
    )
    assert checked == loaded.binding
    with pytest.raises(ValueError):
        deepcopy(capture)
    forged = object.__new__(type(capture))
    with pytest.raises(ValueError, match=r"^source_bridge_ledger_invalid$"):
        validate_ledger_read_binding(
            loaded.binding, capture=forged, now=chain_route.NOW, **native(w)
        )
    with pytest.raises(ValueError):
        validate_ledger_read_binding(
            dict(loaded.binding, available_at="2026-09-21T00:27:00.000000Z"),
            capture=capture,
            now=chain_route.NOW,
            **native(w),
        )


@pytest.mark.parametrize("defect", ["replaced", "missing_clone", "facts_changed", "row_count"])
def test_the_ledger_route_reads_back_every_clone_through_the_same_check(
    monkeypatch, tmp_path, defect
):
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    clones = deepcopy(w["clones"])
    table = w["plan"]["snapshot_plan"]["relation_bindings"][3]["destination_table"]
    if defect == "replaced":
        clones["clone_metadata"][table]["creationTime"] = "1790038800000"
    elif defect == "missing_clone":
        del clones["clone_metadata"][table]
    elif defect == "facts_changed":
        clones["capture_facts"]["captured_at"] = "2026-09-21T00:25:30.000000Z"
    else:
        clones["clone_metadata"][table]["numRows"] = "5"
    refused(
        w,
        read_capture_facts=chain_route.clone_readers(clones)["read_capture_facts"],
        read_clone=chain_route.clone_readers(clones)["read_clone"],
    )
    for name in ("read_capture_facts", "read_clone"):
        refused(w, **{name: None})


def _cheaper_review(raw):
    value = json.loads(raw)
    value["price_review"]["maximum_cycle_cost_micro_usd"] -= 1
    return value


def _substituted_table(raw):
    value = json.loads(raw)
    value["tables"][sorted(value["tables"])[0]]["substituted"] = "yes"
    return value


@pytest.mark.parametrize(
    ("name", "substitute"),
    [("storage_policy", _cheaper_review), ("source_metadata", _substituted_table)],
    ids=["storage_policy", "source_metadata"],
)
def test_canonical_bytes_other_than_the_approved_ones_are_refused(
    monkeypatch, tmp_path, name, substitute
):
    """Bytes that parse canonically still have to be the bytes the manifest digests."""
    w = ledger_world(tmp_path)
    register(monkeypatch, tmp_path, w)
    raw = dict(w["ledger_raw"])
    raw[name] = canonical_bytes(substitute(raw[name]))
    assert raw[name] != w["ledger_raw"][name]
    refused(w, read_input=chain_route.reader(raw))


def _later_as_of(plan):
    for statement in plan["creation_statements"]:
        statement["sql"] = statement["sql"].replace(
            "FOR SYSTEM_TIME AS OF TIMESTAMP '2026", "FOR SYSTEM_TIME AS OF TIMESTAMP '2027"
        )
        statement["sql_digest"] = hashlib.sha256(statement["sql"].encode()).hexdigest()


def _other_source_table(plan):
    for statement in plan["creation_statements"]:
        statement["sql"] = statement["sql"].replace("CLONE `", "CLONE `x")
        statement["sql_digest"] = hashlib.sha256(statement["sql"].encode()).hexdigest()


@pytest.mark.parametrize(
    "plan_change", [_later_as_of, _other_source_table], ids=["later_as_of", "other_source"]
)
def test_an_approved_plan_that_agrees_only_with_itself_is_refused(
    monkeypatch, tmp_path, plan_change
):
    """The approved plan, the payload's digests and the native jobs all name the forgery;
    only the plan rebuilt from the approved inputs tells it apart."""
    w = ledger_world(tmp_path, plan_change=plan_change)
    assert w["plan"] != chain_route.world()["plan"]
    pin_row(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize(
    "rule_arguments",
    [
        {"consume_routine": "sp_consume_open_intelligence_source_snapshot_v2"},
        {"plan_contract_version": "open_intelligence_protected_capture_plan_v2"},
    ],
    ids=["v2_routine", "v2_plan"],
)
def test_a_generation_whose_capture_rule_is_not_the_bridge_rule_admits_nothing(
    monkeypatch, tmp_path, rule_arguments
):
    from src.analysis.open_intelligence.bridge_ledger_binding import is_bridge_ledger_generation

    w = ledger_world(tmp_path, rule_arguments=rule_arguments)
    assert not is_bridge_ledger_generation(w["ledger"]["catalogue"].registry)
    pin_row(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize("suffix", ["#daily-product", "#source-snapshot-v2"])
def test_a_result_recorded_under_another_reference_is_refused(monkeypatch, tmp_path, suffix):
    from tests.unit.test_execution_manifest_origins import manifest

    execution = manifest("source_snapshot_capture")["job_resource"] + "/executions/bridge-capture"
    w = ledger_world(tmp_path, result_reference=execution + suffix)
    assert w["ledger"]["result"].result_reference.endswith(suffix)
    pin_row(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize(("field", "value"), [("projectId", "another-project"), ("location", "EU")])
def test_jobs_of_the_recorded_id_in_another_project_or_location_are_refused(
    monkeypatch, tmp_path, field, value
):
    from tests.unit import bridge_ledger_fixture

    original = bridge_ledger_fixture.native_job

    def changed(*args, **kwargs):
        job = original(*args, **kwargs)
        job["jobReference"][field] = value
        return job

    monkeypatch.setattr(bridge_ledger_fixture, "native_job", changed)
    w = ledger_world(tmp_path)
    assert all(job["jobReference"]["jobId"] == name for name, job in w["jobs"].items())
    register(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize(
    "changes",
    [
        {"arguments": {"mode": "recover"}},
        {"recovery_context": {"contract_version": "open_intelligence_source_capture_recovery_v4"}},
        {"recovery_context": {}},
        {"arguments": {"mode": "recover"}, "recovery_context": {"contract_version": "x"}},
    ],
    ids=["recover_mode", "recovery_context", "empty_context", "recovery"],
)
def test_the_ledger_route_admits_only_an_initial_capture(monkeypatch, tmp_path, changes):
    """Route A is the initial capture: mode initial and a null recovery context, or nothing."""
    from src.analysis.open_intelligence.bridge_ledger_binding import (
        ledger_capture_result,
        read_ledger_capture,
    )

    w = ledger_world(tmp_path, **changes)
    ledger = w["ledger"]
    with pytest.raises(ValueError, match=r"^source_bridge_ledger_not_initial$"):
        ledger_capture_result(
            ledger["rows"],
            consumption_id=ledger["consumption"].consumption_id,
            generation_loader=ledger["catalogue"],
        )
    with pytest.raises(ValueError, match=r"^source_bridge_ledger_not_initial$"):
        read_ledger_capture(
            consumption_id=ledger["consumption"].consumption_id,
            ledger_rows=ledger["rows"],
            generation_loader=ledger["catalogue"],
            read_input=chain_route.reader(w["ledger_raw"]),
        )
    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        render(w)
    pin_row(monkeypatch, tmp_path, w)
    refused(w)


def test_every_creation_job_is_named_for_the_approved_manifest(monkeypatch, tmp_path):
    w = ledger_world(tmp_path)
    owner = w["ledger"]["approval"].manifest_sha256
    records = json.loads(w["ledger"]["result"].canonical_result_json)["creation_records"]
    assert [record["job_id"] for record in records] == [
        f"oi_v3_snapshot_{owner}_{record['lane']}" for record in records
    ]


@pytest.mark.parametrize(
    "job_id",
    [
        lambda lane: initial_job_id("a" * 64, lane),
        lambda lane: "oi_bridge_" + lane,
        lambda lane: initial_job_id("a" * 64, lane) + "_retry",
    ],
    ids=["another_manifest", "unnamed", "suffixed"],
)
def test_jobs_not_named_for_the_approved_manifest_are_refused(monkeypatch, tmp_path, job_id):
    """Another execution's jobs, even under a byte identical plan, cannot be claimed: an
    initial capture names each creation job for the manifest it was approved under."""
    from src.analysis.open_intelligence.bridge_ledger_binding import ledger_capture_result

    w = ledger_world(tmp_path, job_id=job_id)
    ledger = w["ledger"]
    with pytest.raises(ValueError, match=r"^source_bridge_ledger_invalid$"):
        ledger_capture_result(
            ledger["rows"],
            consumption_id=ledger["consumption"].consumption_id,
            generation_loader=ledger["catalogue"],
        )
    with pytest.raises(ValueError, match=r"^capture_receipt_invalid$"):
        render(w)
    pin_row(monkeypatch, tmp_path, w)
    refused(w)


@pytest.mark.parametrize(
    "case",
    [
        test_rendered_ledger_row_is_a_bridge_row_pinned_to_the_ledger_result,
        test_loader_admits_a_ledger_capture_through_its_manifest_and_native_jobs,
    ],
    ids=lambda case: case.__name__,
)
def test_ledger_rows_hold_beside_a_committed_bridge_row(monkeypatch, tmp_path, case):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    case(monkeypatch, tmp_path)


def saved_receipts(directory, receipts):
    """The receipts folder a pin commit adds to: each chain row saved as ``bq`` printed it."""
    directory.mkdir()
    for cutoff, rows in receipts.items():
        (directory / f"{cutoff}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return directory


def committed_with(*entries):
    value = fixture_document()
    value["entries"] = sorted([*value["entries"], *entries], key=lambda entry: entry["cutoff_date"])
    return canonical_bytes(value)


def test_a_committed_ledger_row_is_the_row_its_saved_receipt_renders(tmp_path):
    from scripts.staging.render_protected_context_entry import check_registry_receipts

    w = ledger_world(tmp_path / "world")
    folder = saved_receipts(tmp_path / "receipts", {"2026-09-20": w["ledger"]["rows"]})
    checked = check_registry_receipts(
        committed_with(render(w)), folder, generation_loader=w["ledger"]["catalogue"]
    )
    assert checked == ["2026-09-20"]


def test_a_registry_without_bridge_rows_needs_no_receipt_folder(tmp_path):
    from scripts.staging.render_protected_context_entry import check_registry_receipts

    assert check_registry_receipts(committed_with(), tmp_path / "absent") == []


@pytest.mark.parametrize("field", ["result_digest", "manifest_sha256", "consumption_id"])
def test_a_committed_row_its_receipt_does_not_render_is_refused(tmp_path, field):
    from scripts.staging.render_protected_context_entry import check_registry_receipts

    w = ledger_world(tmp_path / "world")
    entry = render(w)
    entry[field] = entry[field][:-1] + ("0" if entry[field][-1] != "0" else "1")
    if field == "result_digest":
        entry["result_id"] = "exr_" + entry["result_digest"]
    folder = saved_receipts(tmp_path / "receipts", {"2026-09-20": w["ledger"]["rows"]})
    with pytest.raises(ValueError, match=r"^protected_context_receipt_mismatch$"):
        check_registry_receipts(
            committed_with(entry), folder, generation_loader=w["ledger"]["catalogue"]
        )


def test_a_committed_ledger_row_without_its_receipt_is_refused(tmp_path):
    from scripts.staging.render_protected_context_entry import check_registry_receipts

    w = ledger_world(tmp_path / "world")
    folder = saved_receipts(tmp_path / "receipts", {})
    with pytest.raises(ValueError, match=r"^protected_context_receipt_missing$"):
        check_registry_receipts(
            committed_with(render(w)), folder, generation_loader=w["ledger"]["catalogue"]
        )


@pytest.mark.parametrize("name", ["2026-09-21.json", "2026-09-20.txt", "notes.json"])
def test_a_receipt_no_committed_ledger_row_names_is_refused(tmp_path, name):
    from scripts.staging.render_protected_context_entry import check_registry_receipts

    w = ledger_world(tmp_path / "world")
    folder = saved_receipts(tmp_path / "receipts", {"2026-09-20": w["ledger"]["rows"]})
    (folder / name).write_text(json.dumps(w["ledger"]["rows"]), encoding="utf-8")
    with pytest.raises(ValueError, match=r"^protected_context_receipt_unexpected$"):
        check_registry_receipts(
            committed_with(render(w)), folder, generation_loader=w["ledger"]["catalogue"]
        )


def test_a_daily_chain_row_is_proved_by_its_chain_not_a_saved_receipt(tmp_path):
    from scripts.staging.render_protected_context_entry import (
        check_registry_receipts,
        render_bridge_entry,
    )

    chain = chain_route.world()["capture"]
    folder = saved_receipts(tmp_path / "receipts", {})
    assert check_registry_receipts(committed_with(render_bridge_entry(chain)), folder) == []


def test_the_receipt_check_runs_from_the_command_line(tmp_path):
    import subprocess
    import sys

    from tests.unit.test_protected_context_registry import ROOT

    script = ROOT / "scripts/staging/render_protected_context_entry.py"
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(committed_with())
    folder = saved_receipts(tmp_path / "receipts", {})
    checked = subprocess.run(
        [sys.executable, str(script), "--check-receipts", str(folder), str(registry_path)],
        capture_output=True,
        check=True,
    )
    assert json.loads(checked.stdout) == {"checked_cutoffs": []}
    (folder / "2026-09-20.json").write_text("[]", encoding="utf-8")
    refused = subprocess.run(
        [sys.executable, str(script), "--check-receipts", str(folder), str(registry_path)],
        capture_output=True,
    )
    assert refused.returncode != 0
    assert b"protected_context_receipt_unexpected" in refused.stderr


def test_a_trial_pin_row_is_read_beside_every_committed_row(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import protected_context_registry as r

    from tests.unit.test_protected_context_registry import document, trial_cutoff, trial_world

    committed = document()
    route = trial_world(monkeypatch).test_bridge_ledger_route
    monkeypatch.setattr(route, "fixture_document", document)
    w = route.ledger_world(tmp_path / "trial")
    entry = route.register(monkeypatch, tmp_path, w)
    assert entry["cutoff_date"] == trial_cutoff().isoformat()
    rows = r.load_protected_context_registry()
    assert [row.cutoff_date for row in rows] == [
        *(item["cutoff_date"] for item in committed["entries"]),
        entry["cutoff_date"],
    ]
    newest = r.bridge_entries()[0]
    assert newest.profile_id == entry["profile_id"]
    assert r.bridge_authority(newest) == "execution_ledger"
    assert r.bridge_registry_row(newest)["result_id"] == w["ledger"]["result"].result_id
    assert r.allowed_cutoffs() == ("2026-09-07",)


def test_the_loader_admits_a_trial_pin_row_beside_every_committed_row(monkeypatch, tmp_path):
    from src.analysis.open_intelligence import protected_context_registry as r

    from tests.unit.test_protected_context_registry import document, trial_cutoff, trial_world

    route = trial_world(monkeypatch).test_bridge_ledger_route
    # The moved route registers its capture into the real committed document.
    monkeypatch.setattr(route, "fixture_document", document)
    route.test_loader_admits_a_ledger_capture_through_its_manifest_and_native_jobs(
        monkeypatch, tmp_path
    )
    assert r.bridge_entries()[0].cutoff_date == trial_cutoff().isoformat()
    assert len(r.load_protected_context_registry()) == len(document()["entries"]) + 1
