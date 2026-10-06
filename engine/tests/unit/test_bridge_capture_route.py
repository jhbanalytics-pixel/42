"""Route A end to end over a fake provider: producer, approval, runner, then the reader.

Every record is synthetic and unissued. The packaged bridge candidate is made the active
pair inside the test only, the temporal rules are a fixture list, the capture evidence
resolver the bridge fix lane still owes is a recording stand in, and BigQuery, Cloud
Storage and the approval ledger are fakes behind the real client and adapter seams.
"""

import hashlib
import io
import json
import socket
from copy import deepcopy
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

import pytest
from google.cloud import bigquery
from src.analysis.open_intelligence import execution_approval
from src.analysis.open_intelligence import source_estate_bridge_evidence as evidence
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

from tests.unit import bridge_route_fixture as fx

RULE_PATH = "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network prohibited"))
    for name in ("GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)


def pin_private_registry(monkeypatch, tmp_path):
    """A private copy of the committed registry without its bridge rows, so a test's row
    never leaks and no committed bridge row stands in for it."""
    from src.analysis.open_intelligence import protected_context_registry

    from tests.unit.test_protected_context_registry import fixture_document, local

    monkeypatch.setattr(protected_context_registry, "_cache", None, raising=False)
    tmp_path.mkdir(parents=True, exist_ok=True)
    local(monkeypatch, tmp_path, fixture_document())


@pytest.fixture(autouse=True)
def _pinned_registry(tmp_path, monkeypatch):
    pin_private_registry(monkeypatch, tmp_path)


def capture():
    from scripts.staging import capture_protected_production_snapshot

    return capture_protected_production_snapshot


def rule():
    return json.loads((fx.ENGINE_ROOT / RULE_PATH).read_bytes())["operation_validation"][
        "source_snapshot_capture"
    ]


def world(
    tmp_path,
    monkeypatch,
    *,
    provider=None,
    resolver=True,
    identity=fx.IDENTITY,
    recovery=None,
    manifest_change=None,
    **produced,
):
    _, output = fx.produced(tmp_path, monkeypatch, **produced)
    inputs, manifest_raw = fx.read_outputs(output)
    if recovery is not None:
        # Another recovery context approved under its own digest.
        inputs["recovery_context"] = recovery
        manifest = json.loads(manifest_raw)
        for item in manifest["input_artifacts"]:
            if item["name"] == "recovery_context":
                item["sha256"] = hashlib.sha256(recovery).hexdigest()
        manifest_raw = canonical_bytes(manifest)
    if manifest_change is not None:
        # Another manifest approved under its own digest.
        manifest = json.loads(manifest_raw)
        manifest_change(manifest)
        manifest_raw = canonical_bytes(manifest)
    plan = json.loads(inputs["capture_plan"])
    clock = fx.Clock(fx.START_AT)
    http = fx.BridgeBigQueryHTTP(plan, clock, **(provider or {}))
    client = bigquery.Client(
        project="ogilvy-trends-v2",
        location="US",
        credentials=fx.CreationCredentials(identity),
        _http=http,
    )
    objects, storage_http = fx.storage(inputs)
    record = fx.approval(manifest_raw)
    ledger = fx.Ledger(record)
    stand_in = fx.EvidenceResolver()
    if resolver:
        monkeypatch.setattr(evidence, "resolve_capture_bridge_evidence", stand_in, raising=False)
    fx.freeze_preparation(monkeypatch, fx.START_AT)
    return SimpleNamespace(
        inputs=inputs,
        manifest_raw=manifest_raw,
        manifest=json.loads(manifest_raw),
        plan=plan,
        clock=clock,
        http=http,
        client=client,
        objects=objects,
        storage_http=storage_http,
        approval=record,
        ledger=ledger,
        resolver=stand_in,
        stdout=io.StringIO(),
    )


def runner(w, **overrides):
    seams = {
        "now": w.clock,
        "execution_reader": lambda: fx.runtime_pair(w.manifest_raw),
        "approval_reader": lambda digest: w.approval,
        "build_reader": lambda name: fx.build_describe(),
        "runtime_clients": lambda identity: (w.client, w.objects),
        "bridge_evidence_readers": fx.evidence_readers(),
        "consume_query": w.ledger.consume,
        "result_writer": w.ledger.write_result,
        "result_reader": w.ledger.read_result,
        "stdout": w.stdout,
        "diagnostics": lambda event: None,
    }
    seams.update(overrides)
    return partial(capture()._run_bridge_capture, **seams)


def run(w, **overrides):
    untouched = lambda *args, **kwargs: pytest.fail("legacy seam used")  # noqa: E731
    return capture()._main_impl(
        w.manifest["arguments"][1:],
        now=untouched,
        execution_reader=untouched,
        approval_reader=untouched,
        build_reader=untouched,
        runtime_clients=untouched,
        operation_runner=untouched,
        source_preflight=untouched,
        stdout=w.stdout,
        diagnostics=untouched,
        bridge_runner=runner(w, **overrides),
    )


def typed(w):
    """The ledger rows the runner wrote, typed by the real v2 record classes."""
    from tests.unit.test_retained_readers_v2 import v2_rows

    context = {
        "mode": "new_consume",
        "registry": fx.bridge_generation().registry,
        "expected_resource_manifest_sha256": fx.BRIDGE_PAIR[1],
    }
    row = w.ledger.consume(*_replay(w))
    consumption = execution_approval.ExecutionConsumptionV2(
        **row,
        job_resource=w.manifest["job_resource"],
        source_sha=w.manifest["source_sha"],
        image_uri=w.manifest["image_uri"],
        **context,
    )
    result_row = dict(w.ledger.results[-1])
    result = execution_approval.ExecutionResultV2(**result_row, **context)
    return consumption, result, v2_rows(w.approval, consumption, result)


def _replay(w):
    """Re-derive the consumption row of the recorded call without spending anything."""
    routine, values = w.ledger.consume_calls[-1]
    w.ledger.consumed.discard(w.approval.approval_id)
    return routine, [bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in values.items()]


def stored_facts(w):
    raws = [
        item["raw"]
        for name, item in w.storage_http.objects.items()
        if isinstance(name, str) and name.startswith("captures/")
    ]
    assert len(raws) == 1
    return json.loads(raws[0])["capture"]


def register(monkeypatch, tmp_path, rows):
    from scripts.staging.render_protected_context_entry import render_entry

    from tests.unit.test_protected_context_registry import fixture_document, local

    entry = json.loads(render_entry(rows))
    value = fixture_document()
    value["entries"].append(entry)
    local(monkeypatch, tmp_path, value)
    return entry


def binding(w, entry, result):
    from src.analysis.open_intelligence.source_estate_bridge import REGISTRY_FIELDS

    payload = json.loads(result.canonical_result_json)
    profile = w.plan["snapshot_plan"]
    return {
        "contract_version": "source_bridge_read_binding_v1",
        "registry_entry_digest": canonical_digest({key: entry[key] for key in REGISTRY_FIELDS}),
        "profile_id": profile["profile_id"],
        "result_id": result.result_id,
        "result_digest": result.result_digest,
        "snapshot_digest": payload["snapshot_digest"],
        "request_as_of": fx.stamp(fx.REQUEST_AT),
        "purpose": "current_context",
        "product_date": None,
        "client_scope_id": payload["client_scope_id"],
        "market_scope": ["za"],
        "relation_bindings": profile["relation_bindings"],
        "collection_receipt_set_digest": profile["collection_receipt_set_digest"],
        "history_completion_set_digest": profile["history_completion_set_digest"],
        "temporal_rules_version": profile["temporal_rules_version"],
        "temporal_rules_digest": profile["temporal_rules_digest"],
        "available_at": fx.stamp(fx.RESULT_COMPLETED_AT),
        "current_operation_id": None,
        "current_output_set_digest": None,
    }


def read(w, rows, consumption, *, inputs=None):
    from src.analysis.open_intelligence.bridge_ledger_binding import read_ledger_capture

    served = w.inputs if inputs is None else inputs
    return read_ledger_capture(
        consumption_id=consumption.consumption_id,
        ledger_rows=deepcopy(rows),
        read_input=lambda name, digest: served[name],
    )


def admit(w, capture_record, value, *, native_jobs=None, clone_metadata=None, facts=None):
    from src.analysis.open_intelligence.bridge_ledger_binding import validate_ledger_read_binding

    return validate_ledger_read_binding(
        value,
        capture=capture_record,
        now=fx.REQUEST_AT,
        capture_facts=stored_facts(w) if facts is None else facts,
        native_jobs=deepcopy(w.http.jobs) if native_jobs is None else native_jobs,
        clone_metadata=deepcopy(w.http.tables) if clone_metadata is None else clone_metadata,
    )


def succeeded(tmp_path, monkeypatch, **changes):
    w = world(tmp_path, monkeypatch, **changes)
    assert run(w) == 0, w.stdout.getvalue()
    consumption, result, rows = typed(w)
    return w, consumption, result, rows


# The route stays closed unless the active generation takes the bridge plan.


def test_the_fresh_route_stays_closed_for_every_other_plan(tmp_path, monkeypatch):
    fx.activate_amendment_e(monkeypatch)
    touched = []

    def bridge(*args, **kwargs):
        touched.append("bridge")

    with pytest.raises(ValueError, match=r"^snapshot_fresh_route_unavailable$"):
        capture()._main_impl(
            ["--cutoff-date", "2026-09-22", "--mode", "initial", "--grant", fx.GRANT_ID],
            now=lambda: touched.append("clock"),
            execution_reader=lambda: touched.append("execution"),
            approval_reader=lambda _value: touched.append("approval"),
            build_reader=lambda _value: touched.append("build"),
            runtime_clients=lambda: touched.append("runtime"),
            operation_runner=lambda *a, **k: touched.append("operation"),
            source_preflight=lambda *a: touched.append("preflight"),
            stdout=io.StringIO(),
            diagnostics=lambda *a: touched.append("diagnostic"),
            bridge_runner=bridge,
        )
    assert touched == []


def test_the_bridge_route_opens_only_under_the_active_bridge_generation(monkeypatch):
    fx.activate_bridge(monkeypatch)
    seen = []
    argv = ["--cutoff-date", "2026-09-22", "--mode", "initial", "--grant", fx.GRANT_ID]
    assert (
        capture()._main_impl(
            argv,
            now=None,
            execution_reader=None,
            approval_reader=None,
            build_reader=None,
            runtime_clients=None,
            operation_runner=None,
            source_preflight=None,
            stdout=io.StringIO(),
            diagnostics=None,
            bridge_runner=lambda values, *, rule: seen.append((tuple(values), rule)) or 7,
        )
        == 7
    )
    assert seen == [(tuple(argv), rule())]
    with pytest.raises(ValueError, match=r"^snapshot_fresh_route_unavailable$"):
        capture()._main_impl(
            argv,
            now=None,
            execution_reader=None,
            approval_reader=None,
            build_reader=None,
            runtime_clients=None,
            operation_runner=None,
            source_preflight=None,
            stdout=io.StringIO(),
            diagnostics=None,
        )


def test_the_packaged_active_pair_opens_the_bridge_route():
    """No monkeypatched pair: the packaged active generation is the bridge generation, so
    route A opens and the fresh route refuses only without a bridge runner."""
    from src.analysis.open_intelligence import execution_generations

    assert execution_generations.ACTIVE_GENERATION_PAIR == fx.BRIDGE_PAIR
    assert capture()._bridge_capture_rule() == rule()
    seen = []
    flag = "-" * 2
    argv = [
        flag + "cutoff-date",
        "2026-09-22",
        flag + "mode",
        "initial",
        flag + "grant",
        fx.GRANT_ID,
    ]
    assert (
        capture()._main_impl(
            argv,
            now=None,
            execution_reader=None,
            approval_reader=None,
            build_reader=None,
            runtime_clients=None,
            operation_runner=None,
            source_preflight=None,
            stdout=io.StringIO(),
            diagnostics=None,
            bridge_runner=lambda values, *, rule: seen.append(tuple(values)) or 7,
        )
        == 7
    )
    assert seen == [tuple(argv)]


# The end to end route.


def test_producer_approval_runner_then_the_reader_admits_the_clone(tmp_path, monkeypatch):
    w, consumption, result, rows = succeeded(tmp_path, monkeypatch)
    # The runner consumed through the v3 routine once, with the approved plan bytes.
    assert [call[0] for call in w.ledger.consume_calls[:1]] == [
        "sp_consume_open_intelligence_source_snapshot_v3"
    ]
    assert w.ledger.consume_calls[0][1]["capture_plan_json"] == w.inputs["capture_plan"].decode()
    # An initial capture: the approved vector's mode and a canonical null recovery context.
    assert w.manifest["arguments"][3:5] == ["--mode", "initial"]
    assert w.ledger.consume_calls[0][1]["recovery_context_json"] == "null"
    # Each clone job is named for the approved manifest and its lane, as the capture
    # contract and the capture registry name it.
    digest = hashlib.sha256(w.manifest_raw).hexdigest()
    assert [call[2]["jobReference"]["jobId"] for call in w.http.calls if call[0] == "POST"] == [
        f"oi_v3_snapshot_{digest}_{row['lane']}"
        for row in w.plan["snapshot_plan"]["relation_bindings"]
    ]
    assert len(w.resolver.calls) == 1
    # The seven creation statements ran byte for byte, one job each.
    posts = [call for call in w.http.calls if call[0] == "POST"]
    assert [call[2]["configuration"]["query"]["query"] for call in posts] == [
        statement["sql"] for statement in w.plan["creation_statements"]
    ]
    assert result.status == "succeeded"
    assert result.result_reference == fx.EXECUTION_NAME + "#source-snapshot"
    payload = json.loads(result.canonical_result_json)
    assert payload["contract_version"] == "open_intelligence_protected_source_snapshot_v3"
    assert payload["missing_checks"] == []
    assert payload["cutoff_date"] == "2026-09-22"
    assert payload["grant_id"] == fx.GRANT_ID
    assert payload["captured_at"] == fx.stamp(fx.CAPTURED_AT)
    assert payload["snapshot_plan_digest"] == canonical_digest(w.plan)
    assert payload["capture_receipt_digest"] == canonical_digest(payload["creation_records"])
    assert payload["snapshot_digest"] == canonical_digest(stored_facts(w))
    output = json.loads(w.stdout.getvalue())
    assert output["execution_result"]["result_id"] == result.result_id
    # The reader on the approval route admits the clone.
    entry = register(monkeypatch, tmp_path, rows)
    assert entry["profile_id"] == "staging_bridge_v3_20260922"
    assert entry["consumption_id"] == consumption.consumption_id
    record = read(w, rows, consumption)
    value = binding(w, entry, result)
    assert admit(w, record, value) == value


def _forge(w, rows, consumption, result, payload):
    """A ledger result the runner never wrote, typed and pinned like a real one."""
    from tests.unit.test_retained_readers_v2 import v2_rows

    raw = canonical_bytes(payload).decode()
    digest = execution_approval.hashlib.sha256(raw.encode()).hexdigest()
    forged = execution_approval.ExecutionResultV2(
        result_contract_version=result.result_contract_version,
        result_id=execution_approval.result_id_v2(
            result.consumption_id,
            result.result_reference,
            digest,
            result.status,
            result.completed_at,
            origin_registry_sha256=result.origin_registry_sha256,
            resource_manifest_sha256=result.resource_manifest_sha256,
        ),
        consumption_id=result.consumption_id,
        approval_id=result.approval_id,
        manifest_sha256=result.manifest_sha256,
        operation=result.operation,
        execution_name=result.execution_name,
        result_reference=result.result_reference,
        canonical_result_json=raw,
        result_digest=digest,
        status=result.status,
        completed_at=result.completed_at,
        origin_registry_sha256=result.origin_registry_sha256,
        resource_manifest_sha256=result.resource_manifest_sha256,
        mode="new_consume",
        registry=fx.bridge_generation().registry,
        expected_resource_manifest_sha256=fx.BRIDGE_PAIR[1],
    )
    return forged, v2_rows(w.approval, consumption, forged)


def _refused_read(monkeypatch, tmp_path, w, rows, consumption, result, *, code, **native):
    """The forged rows render and read, and the request time binding refuses them."""
    entry = register(monkeypatch, tmp_path, rows)
    record = read(w, rows, consumption)
    value = binding(w, entry, result)
    with pytest.raises(ValueError, match=rf"^{code}$"):
        admit(w, record, value, **native)


def test_a_hand_made_clone_is_refused(tmp_path, monkeypatch):
    w, consumption, result, rows = succeeded(tmp_path, monkeypatch)
    # No ledger execution behind the clone at all.
    with pytest.raises(ValueError):
        read(w, [], consumption)
    # The clone recreated by hand after capture, under the same name.
    tables = deepcopy(w.http.tables)
    name = w.plan["snapshot_plan"]["relation_bindings"][2]["destination_table"]
    tables[name]["creationTime"] = fx.millis(fx.REQUEST_AT - timedelta(minutes=5))
    _refused_read(
        monkeypatch,
        tmp_path,
        w,
        rows,
        consumption,
        result,
        code="source_bridge_clone_differs",
        clone_metadata=tables,
    )


def _rejob(w, payload, change):
    """Re-digest every creation record over jobs changed by ``change``: a forged capture."""
    jobs = deepcopy(w.http.jobs)
    for job in jobs.values():
        change(job)
    for record in payload["creation_records"]:
        record["native_job_digest"] = canonical_digest(jobs[record["job_id"]])
    payload["capture_receipt_digest"] = canonical_digest(payload["creation_records"])
    return jobs


OTHER = "intelligence-42-daily@ogilvy-trends-v2.iam.gserviceaccount.com"


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda job: job.update(user_email=OTHER), "source_bridge_ledger_invalid"),
        (
            lambda job: job["statistics"].update(
                creationTime=fx.millis(fx.CONSUMED_AT - timedelta(seconds=1))
            ),
            "source_bridge_ledger_invalid",
        ),
        # A creation that ends after the capture is refused by the clone readback first.
        (
            lambda job: job["statistics"].update(
                endTime=fx.millis(fx.CAPTURED_AT + timedelta(seconds=1))
            ),
            "source_bridge_capture_precedes_creation",
        ),
    ],
    ids=["wrong_identity", "job_before_consumption", "job_after_capture"],
)
def test_a_forged_capture_whose_jobs_are_not_the_execution_s_is_refused(
    tmp_path, monkeypatch, change, code
):
    w, consumption, result, rows = succeeded(tmp_path, monkeypatch)
    payload = json.loads(result.canonical_result_json)
    jobs = _rejob(w, payload, change)
    forged, forged_rows = _forge(w, rows, consumption, result, payload)
    _refused_read(
        monkeypatch,
        tmp_path,
        w,
        forged_rows,
        consumption,
        forged,
        code=code,
        native_jobs=jobs,
    )


def test_a_reused_execution_is_refused(tmp_path, monkeypatch):
    w, consumption, result, _rows = succeeded(tmp_path, monkeypatch)
    # The same approval run again: the ledger refuses the second consumption, no job runs.
    posts = len([call for call in w.http.calls if call[0] == "POST"])
    with pytest.raises(ValueError, match=r"^execution_approval_concurrent_conflict$"):
        run(w)
    assert len([call for call in w.http.calls if call[0] == "POST"]) == posts
    # A result carried over to another execution of the same approval is refused.
    fields = execution_approval._CONSUMPTION_ROW_FIELDS + execution_approval._GENERATION_FIELDS
    other = execution_approval.ExecutionConsumptionV2(
        mode="new_consume",
        registry=fx.bridge_generation().registry,
        expected_resource_manifest_sha256=fx.BRIDGE_PAIR[1],
        **{
            **{name: getattr(consumption, name) for name in fields},
            "execution_name": fx.EXECUTION_NAME.replace("route-a-1", "route-a-2"),
            "consumption_id": execution_approval.consumption_id_v2(
                consumption.approval_id,
                fx.EXECUTION_NAME.replace("route-a-1", "route-a-2"),
                consumption.consumed_at,
                origin_registry_sha256=consumption.origin_registry_sha256,
                resource_manifest_sha256=consumption.resource_manifest_sha256,
            ),
        },
    )
    from tests.unit.test_retained_readers_v2 import v2_rows

    with pytest.raises(ValueError):
        read(w, v2_rows(w.approval, other, result), other)


def test_a_capture_that_did_not_succeed_is_recorded_failed_and_refused(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, provider={"failed_lane": "seed_graph"})
    assert run(w) == 1
    consumption, result, rows = typed(w)
    assert result.status == "failed"
    payload = json.loads(result.canonical_result_json)
    assert payload["missing_checks"] == ["snapshot_creation_failed"]
    with pytest.raises(ValueError):
        register(monkeypatch, tmp_path, rows)
    with pytest.raises(ValueError):
        read(w, rows, consumption)


def test_the_runner_refuses_jobs_run_by_another_principal(tmp_path, monkeypatch):
    w = world(
        tmp_path,
        monkeypatch,
        provider={"identity": "intelligence-42-daily@ogilvy-trends-v2.iam.gserviceaccount.com"},
    )
    assert run(w) == 1
    _, result, _ = typed(w)
    assert result.status == "failed"
    payload = json.loads(result.canonical_result_json)
    assert payload["missing_checks"] == ["snapshot_creation_job_invalid"]
    # The first foreign job stops the run: no further clone is created.
    assert len([call for call in w.http.calls if call[0] == "POST"]) == 1
    assert payload["creation_records"][0]["state"] == "unresolved"


def test_the_runner_refuses_a_job_created_before_the_consumption(tmp_path, monkeypatch):
    # The provider reports the first job created 10 seconds before the approval was spent.
    w = world(tmp_path, monkeypatch, provider={"shift": timedelta(seconds=-40)})
    assert run(w) == 1
    _, result, _ = typed(w)
    payload = json.loads(result.canonical_result_json)
    assert result.status == "failed"
    assert payload["missing_checks"] == ["snapshot_creation_job_invalid"]
    # The early job stops the run at its own lane.
    assert len([call for call in w.http.calls if call[0] == "POST"]) == 1


def test_a_creation_that_bills_bytes_is_recorded_failed(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, provider={"billed": "1024"})
    assert run(w) == 1
    _, result, _ = typed(w)
    payload = json.loads(result.canonical_result_json)
    assert result.status == "failed"
    assert payload["missing_checks"] == ["snapshot_creation_billing_unresolved"]
    assert len([call for call in w.http.calls if call[0] == "POST"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda manifest: manifest.update(timeout_seconds=1200),
        lambda manifest: manifest["limits"].update(max_bytes_billed=500_000_000),
    ],
    ids=["timeout_1200", "bytes_below_profile"],
)
def test_an_approved_manifest_outside_the_route_a_profile_refuses_before_consumption(
    tmp_path, monkeypatch, change
):
    w = world(tmp_path, monkeypatch, manifest_change=change)
    with pytest.raises(ValueError, match=r"^snapshot_runtime_profile_invalid$"):
        run(w)
    assert w.ledger.consume_calls == []
    assert w.http.calls == []


def test_the_capture_declares_its_unrecorded_history_days(tmp_path, monkeypatch):
    w, _, result, _ = succeeded(tmp_path, monkeypatch)
    entries = json.loads(w.inputs["history_completion_set"])["entries"]
    assert any(row["reason_code"] == "native_completion_unrecorded" for row in entries)
    assert json.loads(result.canonical_result_json)["limitations"] == [
        "native_completion_unrecorded",
        "upstream_collection_completeness_unproven",
    ]


@pytest.mark.parametrize(
    "entries,declared",
    [
        ([{"state": "completed", "reason_code": None}], False),
        ([{"state": "unavailable", "reason_code": "native_completion_withheld"}], False),
        (
            [
                {"state": "completed", "reason_code": None},
                {"state": "unavailable", "reason_code": "native_completion_unrecorded"},
            ],
            True,
        ),
    ],
    ids=["all_recorded", "other_reason", "one_unrecorded"],
)
def test_the_unrecorded_limitation_follows_the_completion_set(entries, declared):
    # A lane the completion record does not carry is always unrecorded, so no complete
    # capture of the fixture's estate lacks the limitation; the rule itself is pinned here.
    limitations = capture()._bridge_limitations({"history_completion_set": {"entries": entries}})
    assert limitations == sorted(
        {"upstream_collection_completeness_unproven"}
        | ({"native_completion_unrecorded"} if declared else set())
    )


def test_the_runner_credentials_must_be_the_manifest_identity(tmp_path, monkeypatch):
    # Credentials of another principal refuse before the approval is spent.
    w = world(
        tmp_path,
        monkeypatch,
        identity="intelligence-42-daily@ogilvy-trends-v2.iam.gserviceaccount.com",
    )
    with pytest.raises(ValueError, match=r"^snapshot_runtime_identity_invalid$"):
        run(w)
    assert w.ledger.consume_calls == []
    assert w.http.calls == []


def test_the_runner_asks_for_clients_of_the_policy_identity(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch)
    asked = []

    def clients(identity):
        asked.append(identity)
        return w.client, w.objects

    assert run(w, runtime_clients=clients) == 0
    assert asked == [fx.IDENTITY]


@pytest.mark.parametrize("recovery", [b"{}", b'"null"', b"[]"])
def test_an_initial_capture_must_carry_a_null_recovery_context(tmp_path, monkeypatch, recovery):
    w = world(tmp_path, monkeypatch, recovery=recovery)
    with pytest.raises(ValueError, match=r"^snapshot_recovery_invalid$"):
        run(w)
    assert w.ledger.consume_calls == []
    assert w.http.calls == []


def test_without_the_evidence_resolver_nothing_is_consumed(tmp_path, monkeypatch):
    w = world(tmp_path, monkeypatch, resolver=False)
    monkeypatch.delattr(evidence, "resolve_capture_bridge_evidence", raising=False)
    with pytest.raises(ValueError, match=r"^bridge_payload_contract_unavailable$"):
        run(w)
    assert w.ledger.consume_calls == []
    assert w.http.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--cutoff-date", "2026-09-22", "--mode", "recover", "--grant", fx.GRANT_ID],
        ["--cutoff-date", "2026-09-21", "--mode", "initial", "--grant", fx.GRANT_ID],
        ["--cutoff-date", "2026-09-22", "--mode", "initial", "--grant", "another_grant"],
    ],
    ids=["recover", "other_cutoff", "other_grant"],
)
def test_the_command_line_must_be_the_approved_vector(tmp_path, monkeypatch, argv):
    w = world(tmp_path, monkeypatch)
    asked = []

    def clients(identity):
        asked.append(identity)
        return w.client, w.objects

    with pytest.raises(ValueError, match=r"^snapshot_cli_invalid$"):
        capture()._main_impl(
            argv,
            now=None,
            execution_reader=None,
            approval_reader=None,
            build_reader=None,
            runtime_clients=None,
            operation_runner=None,
            source_preflight=None,
            stdout=w.stdout,
            diagnostics=None,
            bridge_runner=runner(w, runtime_clients=clients),
        )
    assert w.ledger.consume_calls == []
    assert w.http.calls == []
    # A recovery vector is refused before any client is asked for.
    assert asked == ([] if "recover" in argv else [fx.IDENTITY])


# The approval-route result check is never weaker than the daily check.


def _redigest(value, kwargs, change):
    """Change every native job and re-digest the records and the result over the change."""
    value, kwargs = deepcopy(value), dict(kwargs)
    jobs = deepcopy(kwargs["native_jobs"])
    for job in jobs.values():
        change(job)
    records = deepcopy(kwargs["creation_records"])
    for record in records:
        record["native_job_digest"] = canonical_digest(jobs[record["job_id"]])
    value["creation_records"] = deepcopy(records)
    value["capture_receipt_digest"] = canonical_digest(records)
    kwargs.update(native_jobs=jobs, creation_records=records)
    return value, kwargs


def _changed_inputs(name, change):
    def mutate(value, kwargs):
        kwargs = dict(kwargs)
        plan_inputs = deepcopy(kwargs["plan_inputs"])
        change(plan_inputs[name])
        kwargs["plan_inputs"] = plan_inputs
        return value, kwargs

    return mutate


def _other_consumption(value, kwargs):
    return value, {**kwargs, "capture_consumption": SimpleNamespace(**vars_of(kwargs))}


def vars_of(kwargs):
    consumption = kwargs["capture_consumption"]
    fields = execution_approval._CONSUMPTION_ROW_FIELDS + execution_approval._GENERATION_FIELDS
    return {name: getattr(consumption, name) for name in fields}


LEDGER_MUTATIONS = {
    # The daily check binds these too, under its own principal.
    "other_identity": lambda v, k: _redigest(v, k, lambda job: job.update(user_email=OTHER)),
    "metering": lambda v, k: (
        v,
        {**k, "native_metering": {"query_count": 6, "total_bytes_billed": 0}},
    ),
    # Only the approval route binds these: the job window and the approved inputs.
    "job_before_consumption": lambda v, k: _redigest(
        v,
        k,
        lambda job: job["statistics"].update(
            creationTime=fx.millis(fx.CONSUMED_AT - timedelta(milliseconds=1))
        ),
    ),
    # Each job's own time order, with every time still inside the window.
    "start_before_creation": lambda v, k: _redigest(
        v,
        k,
        lambda job: job["statistics"].update(
            startTime=str(int(job["statistics"]["creationTime"]) - 1)
        ),
    ),
    "start_after_end": lambda v, k: _redigest(
        v,
        k,
        lambda job: job["statistics"].update(startTime=str(int(job["statistics"]["endTime"]) + 1)),
    ),
    "created_after_end": lambda v, k: _redigest(
        v,
        k,
        lambda job: job["statistics"].update(
            creationTime=str(int(job["statistics"]["endTime"]) + 1),
            startTime=str(int(job["statistics"]["endTime"]) + 1),
        ),
    ),
    "consumption_copy": _other_consumption,
    "no_authority": lambda v, k: (v, {**k, "authority": None}),
    "storage_policy": _changed_inputs(
        "storage_policy", lambda value: value["price_review"].update(assumptions=["other"])
    ),
    # Bytes the plan does not read: only the manifest digest binds them.
    "source_metadata": _changed_inputs(
        "source_metadata",
        lambda value: next(iter(value["tables"].values())).update(etag="other"),
    ),
}


def test_the_runner_checks_its_result_against_the_clones_it_read_back(tmp_path, monkeypatch):
    """The runner hands the result check the tables.get resource of every clone, and the
    check refuses a clone that differs from the recorded fingerprint."""
    from src.analysis.open_intelligence import source_estate_bridge_ledger_result as module

    original = module.validate_successful_ledger_bridge_result
    seen = {}

    def spy(value, **kwargs):
        seen["clone_metadata"] = deepcopy(kwargs.get("clone_metadata"))
        clones = deepcopy(kwargs["clone_metadata"])
        table = w.plan["snapshot_plan"]["relation_bindings"][0]["destination_table"]
        clones[table]["numRows"] = str(int(clones[table]["numRows"]) + 1)
        with pytest.raises(ValueError, match=r"^source_bridge_result_invalid$"):
            original(value, **{**kwargs, "clone_metadata": clones})
        return original(value, **kwargs)

    monkeypatch.setattr(module, "validate_successful_ledger_bridge_result", spy)
    w = world(tmp_path, monkeypatch)
    assert run(w) == 0, w.stdout.getvalue()
    assert seen["clone_metadata"] == w.http.tables
    assert set(seen["clone_metadata"]) == {
        relation["destination_table"] for relation in w.plan["snapshot_plan"]["relation_bindings"]
    }


def test_the_approval_route_result_check_refuses_what_the_daily_check_would_admit(
    tmp_path, monkeypatch
):
    from src.analysis.open_intelligence import source_estate_bridge_ledger_result as module

    original = module.validate_successful_ledger_bridge_result
    refused = {}

    def spy(value, **kwargs):
        # Run while the authority is consumed, exactly as the runner calls it.
        for name, mutation in LEDGER_MUTATIONS.items():
            changed, changed_kwargs = mutation(value, kwargs)
            try:
                original(changed, **changed_kwargs)
            except ValueError as error:
                refused[name] = str(error)
        assert original(value, **kwargs) == json.loads(canonical_bytes(value))
        return original(value, **kwargs)

    monkeypatch.setattr(module, "validate_successful_ledger_bridge_result", spy)
    w = world(tmp_path, monkeypatch)
    assert run(w) == 0, w.stdout.getvalue()
    assert set(refused) == set(LEDGER_MUTATIONS)
    assert set(refused.values()) == {"source_bridge_result_invalid"}


# The consumer resolves the capture evidence through the funded readers the runner builds.


def test_the_consumer_passes_the_funded_readers_to_the_resolver(monkeypatch, tmp_path):
    w = world(tmp_path, monkeypatch)
    assert run(w) == 0
    assert w.resolver.calls == [
        (
            ["bridge_policy", "collection_receipt_set", "history_completion_set", "temporal_rules"],
            ["generation_loader", "profile", "read_chain", "read_funded_execution"],
        )
    ]


def test_the_runner_builds_the_readers_from_its_own_client(monkeypatch, tmp_path):
    w = world(tmp_path, monkeypatch)
    clients = []

    def factory(client):
        clients.append(client)
        return fx.evidence_readers()

    assert run(w, bridge_evidence_readers=factory) == 0
    assert clients == [w.client]


# The real resolver: the funded run's ledger row and its execution, read before spending.


def _funded_world(tmp_path, monkeypatch, *, history=None):
    chain = fx.funded_chain()
    facts = dict(fx.history_facts(), entries=[]) if history is None else history
    w = world(tmp_path, monkeypatch, resolver=False, chain=chain, history_completions=facts)
    return w, chain


def test_the_real_resolver_binds_the_funded_run_and_the_capture_succeeds(tmp_path, monkeypatch):
    w, chain = _funded_world(tmp_path, monkeypatch)
    readers = fx.funded_readers(chain)
    assert run(w, bridge_evidence_readers=lambda client: readers) == 0, w.stdout.getvalue()
    assert readers.reads == [
        ("chain", chain["consumption"].consumption_id),
        ("execution", fx.FUNDED_EXECUTION_ID),
    ]
    assert len(w.ledger.consume_calls) == 1
    assert json.loads(w.stdout.getvalue())["execution_result"]["status"] == "succeeded"


def test_a_denied_funded_execution_read_spends_nothing(tmp_path, monkeypatch):
    w, chain = _funded_world(tmp_path, monkeypatch)
    readers = fx.funded_readers(chain)

    def denied(execution_id):
        raise ValueError("bridge_funded_execution_forbidden")

    readers.read_funded_execution = denied
    with pytest.raises(ValueError, match=r"^bridge_evidence_unavailable$"):
        run(w, bridge_evidence_readers=lambda client: readers)
    assert w.ledger.consume_calls == []
    assert w.http.calls == []


def test_a_failed_funded_execution_spends_nothing(tmp_path, monkeypatch):
    w, chain = _funded_world(tmp_path, monkeypatch)
    readers = fx.funded_readers(chain, fx.funded_execution(chain, "failed"))
    with pytest.raises(ValueError, match=r"^bridge_evidence_unavailable$"):
        run(w, bridge_evidence_readers=lambda client: readers)
    assert w.ledger.consume_calls == []


def test_a_recorded_history_completion_spends_nothing(tmp_path, monkeypatch):
    # The approval ledger route has no native completion read, so a supplied completion
    # fact refuses before the approval is spent, and before any read.
    w, chain = _funded_world(tmp_path, monkeypatch, history=fx.history_facts())
    readers = fx.funded_readers(chain)
    with pytest.raises(ValueError, match=r"^bridge_evidence_unavailable$"):
        run(w, bridge_evidence_readers=lambda client: readers)
    assert w.ledger.consume_calls == []
    assert readers.reads == []


def test_the_route_holds_beside_a_committed_bridge_row(tmp_path, monkeypatch):
    from tests.unit.test_protected_context_registry import rehearse_pin

    rehearse_pin(monkeypatch, tmp_path)
    pin_private_registry(monkeypatch, tmp_path / "private")
    test_producer_approval_runner_then_the_reader_admits_the_clone(tmp_path, monkeypatch)
