import copy
import json
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import general_question_snapshot as snapshot

from tests.fixtures.general_question_cap_history_v1 import RETAINED_DIRECT_CAP_RECORDS
from tests.unit import test_general_question_snapshot as released_fixture

SYNTHETIC_BINDING = {
    "source_binding_digest": "5" * 64,
    "snapshot_digest": "2" * 64,
    "cutoff_date": "2026-09-07",
}

UNVERSIONED_V2_DEPLOYMENTS = (
    "a827a412da16f7b9898bb24e1f6ce9db0560d66399cb97804fdc713b5405b744",
    "2bada61bbae78a1156b9c2c051e499500bff2c89a06de99d09e2e484c17476be",
    "437a07c527f15bd3c930b848e6fe4f79bc8e6c57b026544fe0cf221d09f8aac6",
)


def unversioned_context_record(
    stored, records, context, deployment_digest, *, legacy_reservations=True
):
    from src.analysis.open_intelligence.general_question_control import (
        query_binding_digest,
        query_job_id,
        validate_query_reservation,
    )

    converted, replacements = {}, {}
    for ordinal, value in records.items():
        reservation = dict(value["reservation"])
        if legacy_reservations:
            reservation.pop("cap_version")
            reservation["contract_version"] = "general_question_query_reservation_v1"
        reservation["deployment_digest"] = deployment_digest
        reservation["query_digest"] = query_binding_digest(reservation)
        job = query_job_id(reservation["request_id"], ordinal, reservation["query_digest"])
        replacements[reservation["job_id"]] = job
        reservation["job_id"] = job
        receipt = {**value["receipt"], "job_id": job}
        validate_query_reservation(
            {**reservation, "receipt": receipt},
            {**context["admission"], "deployment_digest": deployment_digest},
            context["request"]["request_id"],
        )
        converted[ordinal] = {"reservation": reservation, "receipt": receipt}
    encoded = json.dumps(stored)
    for old, new in replacements.items():
        encoded = encoded.replace(json.dumps(old), json.dumps(new))
    retained = json.loads(encoded)
    retained["deployment_digest"] = deployment_digest
    retained["provenance"].pop("cap_version")
    admission = retained["provenance"]["admission"]
    admission["admission_digest"] = snapshot.canonical_digest(
        {key: value for key, value in admission.items() if key != "admission_digest"}
    )
    resign(retained)
    return retained, converted


def synthetic_admission(**fields):
    """A v3 admission shape with empty derived groups, for builds that stub the admission."""
    from src.analysis.open_intelligence.general_question_context_admission import (
        identity_policy_digest,
    )

    return {
        "contract_version": "protected_context_admission_v3",
        "snapshot_id": "gqs_" + "f" * 64,
        "receipts": [],
        "readings": [],
        "fulfilled_requirement_ids": ["content"],
        "limitations": [],
        "observation_keys": [],
        "origin_groups": {},
        "independent_origin_count": 0,
        "unknown_origin_count": 0,
        "identity_policy_digest": identity_policy_digest(),
        "native_id_projection": {"version": "v3_absent_nullable_v1", "identity_states": {}},
        "origin_authority_projection": {
            "state": "not_projected",
            "projected_records": 0,
            "unprojected_records": 0,
        },
        "units": {
            "collected_records": 0,
            "unique_observations": 0,
            "source_families": 0,
            "verified_independent_origins": 0,
            "unknown_origins": 0,
        },
        "admission_digest": "6" * 64,
        **fields,
    }


def test_context_projection_preserves_source_times_and_partial_coverage():
    context = {
        "request": {
            "request_id": "request",
            "request_digest": "a" * 64,
            "as_of": "2026-09-08T12:00:00Z",
        },
        "intake": {"intake_digest": "b" * 64},
        "admission": {"policy_digest": "c" * 64, "deployment_digest": "d" * 64},
    }
    plan = {
        "plan_digest": "e" * 64,
        "window": {"start": "2026-08-25", "end": "2026-09-07", "closed": True},
        "requirements": [
            {"requirement_id": "observed", "mandatory": True},
            {"requirement_id": "market_share", "mandatory": True},
            {"requirement_id": "optional", "mandatory": False},
        ],
    }
    receipt = {
        "receipt_id": "receipt",
        "published_at": None,
        "collected_at": "2026-09-07T08:00:00Z",
        "author": "Source author",
        "url": None,
        "excerpt": "Synthetic source observation",
    }
    admission = {
        "snapshot_id": "gqs_" + "f" * 64,
        "receipts": [receipt],
        "readings": [],
        "fulfilled_requirement_ids": ["observed"],
        "limitations": ["Selected observations do not measure market share."],
    }
    provenance = {"profile": "protected_context_v1", "admission": copy.deepcopy(admission)}
    result = snapshot._project_context_snapshot(context, plan, admission, provenance)
    assert result["receipts"] == [receipt]
    assert result["missing_work"] == ["market_share"]
    assert result["fulfilled_requirement_ids"] == ["observed"]
    assert result["policy_digest"] == context["admission"]["policy_digest"]
    assert result["deployment_digest"] == context["admission"]["deployment_digest"]
    assert result["snapshot_digest"] == snapshot.canonical_digest(
        {key: value for key, value in result.items() if key != "snapshot_digest"}
    )
    receipt["excerpt"] = "Mutated after projection"
    provenance["profile"] = "mutated"
    assert result["receipts"][0]["excerpt"] == "Synthetic source observation"
    assert result["provenance"]["profile"] == "protected_context_v1"


def test_nonempty_context_profile_routes_without_legacy_fallback(monkeypatch):
    from src.analysis.open_intelligence import general_question_context_admission as admission
    from src.analysis.open_intelligence import general_question_queries as query_ledger
    from src.analysis.open_intelligence import general_question_query_execution as execution
    from src.analysis.open_intelligence import production_snapshot_storage as storage_boundary

    module, store, plan = released_fixture.fixture(monkeypatch)
    pin = {
        "profile_id": "protected_context_20260907_v1",
        "cutoff_date": "2026-09-07",
        "manifest_sha256": "a" * 64,
        "consumption_id": "exc_" + "b" * 64,
        "result_id": "exr_" + "c" * 64,
        "result_digest": "d" * 64,
    }
    monkeypatch.setattr(admission, "_PROTECTED_CONTEXT_PROFILES", (pin,))
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setattr(protected_context_registry, "_PROTECTED_CONTEXT_PROFILES", (pin,))
    monkeypatch.setattr(
        module,
        "execute_released_candidate_query",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy fallback")),
    )
    records = {}

    class Ledger:
        def __init__(self, _store):
            pass

        def read_query(self, _request_id, *, scope, ordinal):
            return records[ordinal]

    monkeypatch.setattr(query_ledger, "GeneralQuestionQueries", Ledger)
    monkeypatch.setattr(storage_boundary, "SourceCaptureObjects", lambda bucket: object())
    monkeypatch.setattr(
        "google.cloud.bigquery.Client", lambda **kwargs: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(
        "google.cloud.storage.Client",
        lambda **kwargs: SimpleNamespace(bucket=lambda name: object(), close=lambda: None),
    )

    def result_query(*args, ordinal, **kwargs):
        approval = SimpleNamespace(manifest_sha256=pin["manifest_sha256"])
        rows = [{"authority": True}]
        receipt = {"result_digest": module.canonical_digest(rows)}
        records[ordinal] = {
            "reservation": {"ordinal": ordinal},
            "receipt": receipt,
        }
        return {
            "application_status": "accepted",
            "material": {"approval": approval, "result": {"result_id": pin["result_id"]}},
            "rows": rows,
            "receipt": receipt,
        }

    monkeypatch.setattr(execution, "execute_context_result_query", result_query)
    loaded = SimpleNamespace(source_binding={"profile_id": pin["profile_id"], **SYNTHETIC_BINDING})

    def load(*args, result_reader, approval_reader, **kwargs):
        assert result_reader(pin["consumption_id"])[0]["result_id"] == pin["result_id"]
        assert approval_reader(pin["manifest_sha256"]).manifest_sha256 == pin["manifest_sha256"]
        return loaded

    monkeypatch.setattr(admission, "load_protected_context_source", load)

    def data_result(template, ordinal, count):
        rows = [{"template": template}]
        receipt = {"result_digest": module.canonical_digest(rows)}
        records[ordinal] = {"reservation": {"ordinal": ordinal}, "receipt": receipt}
        return {
            "application_status": "accepted",
            "rows": rows,
            "receipt": receipt,
            "material": {"candidate_count": count, "records": [{"sample_row_ids": ["synthetic"]}]},
            "prepared_query": SimpleNamespace(template_id=template),
        }

    monkeypatch.setattr(
        execution,
        "execute_context_candidate_query",
        lambda *args, ordinal, **kwargs: data_result("candidate", ordinal, 1),
    )
    monkeypatch.setattr(
        execution,
        "execute_context_partition_query",
        lambda *args, ordinal, **kwargs: data_result("partitions", ordinal, 0),
    )
    monkeypatch.setattr(
        execution,
        "execute_context_evidence_query",
        lambda *args, lane, ordinal, **kwargs: data_result(
            lane, ordinal, 1 if lane == "enriched_content" else 0
        ),
    )
    monkeypatch.setattr(
        execution,
        "execute_context_index_query",
        lambda *args, lane, ordinal, **kwargs: data_result(
            lane + "_index", ordinal, 1 if lane == "enriched_content" else 0
        ),
    )
    contextual = synthetic_admission(limitations=["Contextual evidence only."])
    monkeypatch.setattr(admission, "admit_protected_context", lambda *args, **kwargs: contextual)

    result = released_fixture.build(module, store, plan)

    assert result["status"] == "admitted"
    assert result["snapshot"]["provenance"]["profile"] == "protected_context_v3"
    assert result["snapshot"]["snapshot_id"] == contextual["snapshot_id"]
    sidecar = result["snapshot"]["provenance"]["identity_sidecar"]
    assert sidecar == module._identity_sidecar(contextual, loaded.source_binding)
    assert sidecar["source_binding_digest"] == SYNTHETIC_BINDING["source_binding_digest"]
    assert sidecar["identity_policy_digest"] == contextual["identity_policy_digest"]
    assert set(records) == {1, 2, 3, 4, 5, 6, 7}


@pytest.mark.parametrize("expire_before_write", [False, True])
@pytest.mark.parametrize("collection_before_window", [False, True])
@pytest.mark.parametrize("supplement", [False, True])
@pytest.mark.parametrize("cap_version", [1, 2])
def test_actual_protected_capture_owned_queries_admission_and_restore_join(
    monkeypatch, expire_before_write, collection_before_window, supplement, cap_version
):
    """The consumption row is the retained v1 capture record read under historical_read:
    successor 1 excludes protected capture from fresh execution authority. The joined
    capture this test replays is produced by the validated operation body under the
    retained view (replay suite completed_capture), never by a live capture authority."""
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime

    from google.cloud import bigquery
    from src.analysis.open_intelligence import execution_approval as approval
    from src.analysis.open_intelligence import general_question_context_admission as admission
    from src.analysis.open_intelligence import production_snapshot_storage as storage_boundary
    from src.analysis.open_intelligence.general_question_queries import GeneralQuestionQueries

    from tests.unit import test_general_question_context_queries as wires
    from tests.unit import test_general_question_query_execution as native
    from tests.unit import test_protected_snapshot_replay_integration as protected
    from tests.unit.test_open_intelligence_execution_approval_runtime import (
        retained_source_snapshot_records,
    )

    monkeypatch.setattr(snapshot, "_CURRENT_CONTEXT_CAP_VERSION", cap_version, raising=False)
    # Reproduce historical keyed captures for exact v1 replay checks below.
    if not supplement:
        monkeypatch.setattr(snapshot, "_context_requirements", lambda plan: [])

    capture_http, _, ledger, protected_inputs = protected.completed_capture(monkeypatch)
    monkeypatch.setattr(bigquery, "Client", protected._REAL_CLIENT)
    monkeypatch.setattr(native.f, "NOW", datetime(2026, 9, 9, 12, 1, tzinfo=UTC))
    original_draft = native.runtime.plan_draft()
    original_draft["window"] = {"start": "2026-09-01", "end": "2026-09-07", "closed": True}
    monkeypatch.setattr(native.runtime, "plan_draft", lambda: copy.deepcopy(original_draft))
    values = native.fixture()
    store, bucket, invocation, identity, credentials, plan = values
    pin = {
        key: protected_inputs[key]
        for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
    }
    pin.update(profile_id="protected_context_20260907_v1", cutoff_date="2026-09-07")
    monkeypatch.setattr(admission, "_PROTECTED_CONTEXT_PROFILES", (pin,))
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setattr(protected_context_registry, "_PROTECTED_CONTEXT_PROFILES", (pin,))
    closed = []
    monkeypatch.setattr(
        "google.cloud.storage.Client",
        lambda **_: SimpleNamespace(
            bucket=lambda _: protected_inputs["objects"].bucket,
            close=lambda: closed.append("storage"),
        ),
    )
    from src.analysis.open_intelligence.production_snapshot_capture import _read_protected_capture

    completed = _read_protected_capture(
        **{
            key: protected_inputs[key]
            for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
        },
        client_scope_id="ogilvy_default",
        market_scope=["ke", "ng", "za"],
        objects=protected_inputs["objects"],
        result_reader=lambda _: (ledger.result,),
        approval_reader=lambda _: approval.ExecutionApproval(**ledger.approval),
    )
    binding = admission._source_binding(pin, completed)
    artifacts = protected.capture_fixture.operation_fixture.artifacts()
    # Successor 1: protected capture is excluded from fresh execution authority, so the
    # consumption row is the retained v1 record read under historical_read.
    _, consumption = retained_source_snapshot_records(artifacts=artifacts)
    authority_rows = [
        {
            "approval_count": 1,
            "consumption_count": 1,
            "result_count": 1,
            "approval_json": snapshot.canonical_bytes(ledger.approval).decode(),
            "consumption_json": snapshot.canonical_bytes(asdict(consumption)).decode(),
            "result_json": snapshot.canonical_bytes(ledger.result).decode(),
        }
    ]
    candidates = wires.candidate_rows(binding)
    candidates[-1]["sample_row_ids"] = ["fresh_native_row"]
    payload = json.loads(candidates[-1]["payload"])
    payload["sample_row_ids"] = ["fresh_native_row"]
    candidates[-1]["payload"] = json.dumps(payload)
    enriched = [
        wires.metadata(binding, 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "fresh_native_row",
            "match_count": 1,
            "payload": json.dumps(
                {
                    "id": "fresh_native_row",
                    "market": "za",
                    "source": "news",
                    "source_family": None,
                    "platform": "web",
                    "author_handle": None,
                    "url": None,
                    "title": "Mobility observations",
                    "text": "Mobility observations from a full native snapshot row.",
                    "published_at": None,
                    "collected_at": "2026-09-07T11:00:00+00:00",
                }
            ),
        },
    ]

    enriched_index = [
        wires.metadata(binding, 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "fresh_native_row",
            "match_count": 1,
            "payload": json.dumps(
                {"market": "za", "id": "fresh_native_row", "collected_date": "2026-09-07"}
            ),
        },
    ]
    if collection_before_window:
        evidence_payload = json.loads(enriched[-1]["payload"])
        evidence_payload.update(
            published_at="2026-09-07T11:00:00+00:00", collected_at="2026-08-31T11:00:00+00:00"
        )
        enriched[-1]["payload"] = json.dumps(evidence_payload)
        index_payload = json.loads(enriched_index[-1]["payload"])
        index_payload["collected_date"] = "2026-08-31"
        enriched_index[-1]["payload"] = json.dumps(index_payload)

    class JoinedHTTP(native.HTTP):
        def request(self, method, url, **kwargs):
            if "/tables/" in url:
                return capture_http.request(method, url, **kwargs)
            if method == "POST":
                sql = json.loads(kwargs["data"])["configuration"]["query"]["query"]
                rows = (
                    authority_rows
                    if "result_json" in sql
                    else [
                        {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
                        {
                            "lane": "enriched_content",
                            "partition_date": "2026-08-31"
                            if collection_before_window
                            else "2026-09-07",
                            "partition_count": 1,
                        },
                    ]
                    if "AS partition_count" in sql
                    else candidates
                    if "WITH candidates" in sql
                    else enriched_index
                    if "CAST(DATE(evidence.collected_at) AS STRING)" in sql
                    and "enriched_content" in sql
                    else enriched
                    if "enriched_content" in sql
                    else [wires.metadata(binding, 0, 0, evidence=True)]
                )
                native._context_wire(self, rows)
            return super().request(method, url, **kwargs)

    http = JoinedHTTP(identity, bucket=bucket)
    native.install(monkeypatch, http)
    if expire_before_write:
        project = snapshot._project_context_snapshot

        def expire(*args):
            result = project(*args)
            monkeypatch.setattr(snapshot, "monotonic", lambda: 1000000000000)
            return result

        monkeypatch.setattr(snapshot, "_project_context_snapshot", expire)
    result = snapshot.build_general_question_snapshot(
        invocation,
        store=store,
        scope=native.f.scope(),
        runtime_identity=identity,
        credentials=credentials,
        plan=plan,
        candidate_limit=10,
        evidence_limit=10,
        discovery_bytes=100000000,
        source_bytes=300000000,
        release_bytes=50000000,
        now=native.f.NOW,
    )
    if expire_before_write:
        assert result["status"] == "refused", result
        assert store._objects.read(f"requests/{invocation['request_id']}/snapshot.json") is None
        assert closed == ["storage"]
        return
    assert result["status"] == "admitted", result
    context = store.read_request(invocation["request_id"], scope=native.f.scope())
    queries = GeneralQuestionQueries(store)
    records = {
        ordinal: queries.read_query(
            invocation["request_id"], scope=native.f.scope(), ordinal=ordinal
        )
        for ordinal in range(1, 8 if supplement else 7)
    }
    assert result["snapshot"]["provenance"]["cap_version"] == cap_version
    assert all(value["reservation"]["cap_version"] == cap_version for value in records.values())
    monkeypatch.setattr(snapshot, "_CURRENT_CONTEXT_CAP_VERSION", 2)
    restored = snapshot.validate_stored_general_question_snapshot(
        result["snapshot"],
        request=context["request"],
        intake=context["intake"],
        plan=plan,
        query_records=records,
    )
    assert restored == result["snapshot"]
    different_version = {
        ordinal: {"reservation": dict(value["reservation"]), "receipt": value["receipt"]}
        for ordinal, value in records.items()
    }
    different_version[1]["reservation"]["cap_version"] = 3 - cap_version
    with pytest.raises(ValueError, match=r"^snapshot_invalid$"):
        snapshot.validate_stored_general_question_snapshot(
            restored,
            request=context["request"],
            intake=context["intake"],
            plan=plan,
            query_records=different_version,
        )
    if cap_version == 2:
        own_deployment = restored["deployment_digest"]
        assert own_deployment not in UNVERSIONED_V2_DEPLOYMENTS
        for deployment in (*UNVERSIONED_V2_DEPLOYMENTS, own_deployment, "0" * 64):
            retained, retained_records = unversioned_context_record(
                restored, records, context, deployment
            )
            arguments = {
                "request": context["request"],
                "intake": context["intake"],
                "plan": plan,
                "query_records": retained_records,
            }
            if deployment not in UNVERSIONED_V2_DEPLOYMENTS:
                with pytest.raises(ValueError, match=r"^snapshot_cap_version_unknown$"):
                    snapshot.validate_stored_general_question_snapshot(retained, **arguments)
                continue
            before = snapshot.canonical_bytes(retained)
            assert (
                snapshot.validate_stored_general_question_snapshot(retained, **arguments)
                == retained
            )
            assert snapshot.canonical_bytes(retained) == before
            retained["provenance"]["cap_version"] = 2
            resign(retained)
            assert (
                snapshot.validate_stored_general_question_snapshot(retained, **arguments)
                == retained
            )
            retained["provenance"].pop("cap_version")
            resign(retained)
            unmarked_profile, marked_records = unversioned_context_record(
                restored, records, context, deployment, legacy_reservations=False
            )
            assert (
                snapshot.validate_stored_general_question_snapshot(
                    unmarked_profile, **{**arguments, "query_records": marked_records}
                )
                == unmarked_profile
            )
            for location in (retained["provenance"], retained_records[1]["reservation"]):
                location["cap_version"] = None
                resign(retained)
                with pytest.raises(ValueError, match=r"^snapshot_cap_version_unknown$"):
                    snapshot.validate_stored_general_question_snapshot(retained, **arguments)
                location.pop("cap_version")
                resign(retained)
            retained_records[1]["reservation"]["contract_version"] = (
                "general_question_query_reservation_v2"
            )
            with pytest.raises(ValueError, match=r"^snapshot_cap_version_unknown$"):
                snapshot.validate_stored_general_question_snapshot(retained, **arguments)
    for location in ("profile", "reservation"):
        for unknown in (None, 999, True):
            invalid = copy.deepcopy(restored)
            invalid_records = {
                ordinal: {"reservation": dict(value["reservation"]), "receipt": value["receipt"]}
                for ordinal, value in records.items()
            }
            target = (
                invalid["provenance"]
                if location == "profile"
                else invalid_records[1]["reservation"]
            )
            if unknown is None:
                target.pop("cap_version")
            else:
                target["cap_version"] = unknown
            invalid["snapshot_digest"] = snapshot.canonical_digest(
                {key: value for key, value in invalid.items() if key != "snapshot_digest"}
            )
            with pytest.raises(ValueError, match=r"^snapshot_cap_version_unknown$"):
                snapshot.validate_stored_general_question_snapshot(
                    invalid,
                    request=context["request"],
                    intake=context["intake"],
                    plan=plan,
                    query_records=invalid_records,
                )
    assert restored["receipts"][0]["source_row_id"] == "fresh_native_row"
    sidecar = restored["provenance"]["identity_sidecar"]
    assert restored["provenance"]["profile"] == "protected_context_v3"
    assert sidecar["observation_keys"] == sorted(
        [row["market"], row["platform"], row["source_row_id"]] for row in restored["receipts"]
    )
    assert sidecar["observation_keys"][0][2] == "fresh_native_row"
    assert sidecar["unknown_origin_count"] == 1
    assert sidecar["independent_origin_count"] == 0
    assert (
        sidecar["source_binding_digest"]
        == (restored["provenance"]["source_binding"]["source_binding_digest"])
    )
    assert sidecar["native_id_projection"]["version"] == "v3_absent_nullable_v1"
    # The reader runs against this real replayed capture in the four combinations that
    # produce a snapshot; the other four refuse before the write, so no snapshot exists to
    # read. The capture's physical rows carry no origin column, so its zero verified
    # origins is an absent projection rather than a measured zero, and it says so.
    cited_receipts = [row["receipt_id"] for row in restored["receipts"]]
    support = snapshot.read_context_evidence_support(restored, cited_receipts)
    assert support["snapshot_id"] == restored["snapshot_id"]
    assert support["units"]["collected_records"] == len(restored["receipts"])
    assert support["independent_support_slots"] == 0
    assert support["unknown_support_slots"] == len(restored["receipts"])
    assert support["limitations"][-1].startswith("Independence is unknown for")
    assert support["origin_authority_projection"] == {
        "state": "not_projected",
        "projected_records": 0,
        "unprojected_records": len(restored["receipts"]),
    }
    assert any(
        line.startswith("Origin authority is not projected") for line in support["limitations"]
    )
    with pytest.raises(TypeError):
        snapshot.read_context_evidence_support(restored)
    with pytest.raises(ValueError, match="context_support_request_invalid"):
        snapshot.read_context_evidence_support(restored, [*cited_receipts, "gqctx_absent"])
    forged = copy.deepcopy(restored)
    forged_sidecar = forged["provenance"]["identity_sidecar"]
    forged_sidecar["origin_groups"] = {"wire-story-7": forged_sidecar["observation_keys"]}
    forged_sidecar["independent_origin_count"] = 1
    forged_sidecar["unknown_origin_count"] = 0
    forged_sidecar["sidecar_digest"] = snapshot.canonical_digest(
        {key: item for key, item in forged_sidecar.items() if key != "sidecar_digest"}
    )
    forged["snapshot_digest"] = snapshot.canonical_digest(
        {key: item for key, item in forged.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(forged, cited_receipts)
    for field, value in (("unknown_origin_count", 0), ("identity_policy_digest", "0" * 64)):
        tampered = copy.deepcopy(result["snapshot"])
        tampered["provenance"]["identity_sidecar"][field] = value
        tampered["snapshot_digest"] = snapshot.canonical_digest(
            {key: item for key, item in tampered.items() if key != "snapshot_digest"}
        )
        with pytest.raises(ValueError):
            snapshot.validate_stored_general_question_snapshot(
                tampered,
                request=context["request"],
                intake=context["intake"],
                plan=plan,
                query_records=records,
            )
    if collection_before_window:
        assert restored["receipts"][0]["collected_at"].startswith("2026-08-31")
        assert restored["receipts"][0]["published_at"].startswith("2026-09-07")
    else:
        assert restored["receipts"][0]["published_at"] is None
    assert closed == ["storage"]
    posts = [call for call in http.calls if call[0] == "POST"]
    assert len(posts) == (7 if supplement else 6)
    expected_caps = (
        [35000000, 35000000, 60000000, 200000000, 170000000, 300000000, 165000000]
        if supplement
        else [
            35000000,
            35000000,
            200000000,
            195000000,
            300000000,
            195000000,
        ]
    )
    if cap_version == 1:
        expected_caps = (
            [35000000, 35000000, 60000000, 100000000, 275000000, 180000000, 275000000]
            if supplement
            else [35000000, 35000000, 100000000, 300000000, 180000000, 300000000]
        )
    assert [
        int(call[3]["configuration"]["query"]["maximumBytesBilled"]) for call in posts
    ] == expected_caps
    if supplement:
        assert restored["provenance"]["limits"]["effective_evidence_limit"] == 10
        for field in ("effective_evidence_limit", "raw_limit", "missing_effective_limit"):
            invalid = copy.deepcopy(restored)
            if field == "missing_effective_limit":
                invalid["provenance"]["limits"].pop("effective_evidence_limit")
            else:
                invalid["provenance"]["limits"][field] += 1
            invalid["snapshot_digest"] = snapshot.canonical_digest(
                {k: v for k, v in invalid.items() if k != "snapshot_digest"}
            )
            with pytest.raises(ValueError, match="snapshot_invalid"):
                snapshot.validate_stored_general_question_snapshot(
                    invalid,
                    request=context["request"],
                    intake=context["intake"],
                    plan=plan,
                    query_records=records,
                )

    if not collection_before_window and not supplement:
        context_queries = wires.subject()
        legacy_snapshot = copy.deepcopy(restored)
        legacy_records = {
            ordinal: {
                "reservation": dict(value["reservation"]),
                "receipt": dict(value["receipt"]),
            }
            for ordinal, value in records.items()
        }
        captures = legacy_snapshot["provenance"]["captures"]
        limits = legacy_snapshot["provenance"]["limits"]
        legacy_queries = {
            "candidate": context_queries.build_context_candidate_query(
                context["request"],
                plan,
                context["intake"],
                binding,
                candidate_limit=limits["candidate_limit"],
            )
        }
        for key, lane in (("enriched", "enriched_content"), ("raw", "raw_content")):
            common = {
                "candidate_rows": captures["candidate"]["rows"],
                "lane": lane,
                "candidate_limit": limits[key + "_limit"],
                **({"enriched_rows": captures["enriched"]["rows"]} if key == "raw" else {}),
            }
            legacy_queries[key + "_index"] = context_queries._build_context_evidence_query(
                context["request"],
                plan,
                context["intake"],
                binding,
                _index=True,
                _validation_legacy=True,
                **common,
            )
            legacy_queries[key] = context_queries._build_context_evidence_query(
                context["request"],
                plan,
                context["intake"],
                binding,
                index_rows=captures[key + "_index"]["rows"],
                _validation_legacy=True,
                **common,
            )
        admission = legacy_snapshot["provenance"]["admission"]
        for key, ordinal in zip(legacy_queries, range(2, 7), strict=True):
            digest = legacy_queries[key].sql_digest
            legacy_records[ordinal]["reservation"]["sql_digest"] = digest
            legacy_records[ordinal]["receipt"]["sql_digest"] = digest
            captures[key]["receipt"]["sql_digest"] = digest
            admission["provenance"]["query_bindings"][key]["sql_digest"] = digest
        admission["admission_digest"] = snapshot.canonical_digest(
            {key: value for key, value in admission.items() if key != "admission_digest"}
        )
        legacy_snapshot["snapshot_digest"] = snapshot.canonical_digest(
            {key: value for key, value in legacy_snapshot.items() if key != "snapshot_digest"}
        )
        assert (
            snapshot.validate_stored_general_question_snapshot(
                legacy_snapshot,
                request=context["request"],
                intake=context["intake"],
                plan=plan,
                query_records=legacy_records,
            )
            == legacy_snapshot
        )

        unknown_snapshot = copy.deepcopy(legacy_snapshot)
        unknown_records = copy.deepcopy(legacy_records)
        unknown_digest = "0" * 64
        for value in (
            unknown_records[3]["reservation"],
            unknown_records[3]["receipt"],
            unknown_snapshot["provenance"]["captures"]["enriched_index"]["receipt"],
            unknown_snapshot["provenance"]["admission"]["provenance"]["query_bindings"][
                "enriched_index"
            ],
        ):
            value["sql_digest"] = unknown_digest
        unknown_admission = unknown_snapshot["provenance"]["admission"]
        unknown_admission["admission_digest"] = snapshot.canonical_digest(
            {key: value for key, value in unknown_admission.items() if key != "admission_digest"}
        )
        unknown_snapshot["snapshot_digest"] = snapshot.canonical_digest(
            {key: value for key, value in unknown_snapshot.items() if key != "snapshot_digest"}
        )
        with pytest.raises(ValueError, match="snapshot_invalid"):
            snapshot.validate_stored_general_question_snapshot(
                unknown_snapshot,
                request=context["request"],
                intake=context["intake"],
                plan=plan,
                query_records=unknown_records,
            )

    altered = copy.deepcopy(restored)
    altered["provenance"]["authority_captures"] = []
    altered["snapshot_digest"] = snapshot.canonical_digest(
        {key: value for key, value in altered.items() if key != "snapshot_digest"}
    )
    with pytest.raises(ValueError, match="snapshot_invalid"):
        snapshot.validate_stored_general_question_snapshot(
            altered,
            request=context["request"],
            intake=context["intake"],
            plan=plan,
            query_records=records,
        )

    for ordinal in (3, 5):
        unowned = dict(records)
        unowned[ordinal] = {**records[ordinal], "receipt": dict(records[ordinal]["receipt"])}
        unowned[ordinal]["receipt"]["result_digest"] = "0" * 64
        with pytest.raises(ValueError, match="snapshot_invalid"):
            snapshot.validate_stored_general_question_snapshot(
                restored,
                request=context["request"],
                intake=context["intake"],
                plan=plan,
                query_records=unowned,
            )


@pytest.mark.parametrize(
    "direct,lane,expected",
    [
        (False, "authority", 35_000_000),
        (False, "candidate", 35_000_000),
        (False, "enriched_index", 100_000_000),
        (False, "enriched", 300_000_000),
        (False, "raw_index", 180_000_000),
        (False, "raw", 300_000_000),
        (True, "partitions", 60_000_000),
        (True, "enriched", 275_000_000),
        (True, "raw", 275_000_000),
    ],
)
def test_version_one_cap_literal_preserves_retained_allowance(direct, lane, expected):
    caps = snapshot._context_query_caps(1_000_000_000, 1_000_000_000, 1_000_000_000, cap_version=1)
    if direct:
        caps = snapshot._direct_context_query_caps(caps, cap_version=1)
    assert caps[lane] == expected


def test_unversioned_cap_deployment_mapping_is_exact_and_immutable():
    assert dict(snapshot._UNVERSIONED_CAP_DEPLOYMENTS) == dict.fromkeys(
        UNVERSIONED_V2_DEPLOYMENTS, 2
    )
    with pytest.raises(TypeError):
        snapshot._UNVERSIONED_CAP_DEPLOYMENTS["0" * 64] = 2


@pytest.mark.parametrize(
    "record", RETAINED_DIRECT_CAP_RECORDS, ids=lambda record: record["request_id"]
)
def test_version_one_direct_caps_match_retained_records(record):
    assert record["has_partitions"]
    assert not record["cap_version_present"]
    caps = snapshot._context_query_caps(35_000_000, 300_000_000, 35_000_000, cap_version=1)
    assert snapshot._direct_context_query_caps(caps, cap_version=1) == record["query_caps"]


def test_context_caps_preserve_caller_allowances_and_recovery_ceiling():
    caps = snapshot._context_query_caps(100000000, 300000000, 50000000)
    assert sum(caps.values()) + caps["authority"] == 995000000
    assert "partitions" not in caps
    direct = snapshot._direct_context_query_caps(caps)
    assert direct["partitions"] == 60000000
    assert direct["enriched"] == 170000000
    assert direct["raw"] == 165000000
    assert sum(direct.values()) + direct["authority"] == 1000000000
    assert sum(direct.values()) + direct["authority"] <= 1000000000
    assert sum(caps.values()) + caps["authority"] == 995000000
    smaller = snapshot._context_query_caps(12000000, 200000000, 9000000)
    assert smaller["candidate"] == 12000000
    assert smaller["authority"] == 9000000
    assert smaller["enriched"] == smaller["raw"] == 195000000
    assert smaller["raw_index"] == 200000000
    assert max(smaller["enriched_index"], smaller["raw_index"]) <= 200000000


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("enriched_count", [0, 200])
@pytest.mark.parametrize("raw_parent", [False, True])
def test_v4_lazy_batch_keeps_full_capture_and_both_raw_stages(
    monkeypatch, enriched_count, direct, raw_parent
):
    import copy
    import json
    from datetime import UTC, datetime

    from src.analysis.open_intelligence import general_question_context_admission as admission
    from src.analysis.open_intelligence import general_question_queries as ledger
    from src.analysis.open_intelligence import general_question_query_execution as execution

    from tests.unit.test_open_intelligence_execution_approval_migration import (
        _SOURCE_METADATA_CONTEXT_JSON,
    )

    module, store, plan = released_fixture.fixture(monkeypatch)
    recovery = json.loads(_SOURCE_METADATA_CONTEXT_JSON)
    contexts = [
        recovery,
        recovery["ancestor_recovery_context"],
        recovery["ancestor_recovery_context"]["ancestor_recovery_context"],
    ]
    identifiers = tuple(sorted(context["initial_consumption_id"] for context in contexts))
    pin = {
        "profile_id": "protected_context_20260907_v1",
        "cutoff_date": "2026-09-07",
        "manifest_sha256": "a" * 64,
        "consumption_id": "exc_" + "b" * 64,
        "result_id": "exr_" + "c" * 64,
        "result_digest": "d" * 64,
    }
    monkeypatch.setattr(admission, "_PROTECTED_CONTEXT_PROFILES", (pin,))
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setattr(protected_context_registry, "_PROTECTED_CONTEXT_PROFILES", (pin,))
    trace, records, input_reads = [], {}, []
    current_approval = SimpleNamespace(
        manifest_sha256=pin["manifest_sha256"],
        canonical_manifest_json=json.dumps(
            {
                "input_artifacts": [
                    {"name": "recovery_context", "sha256": module.canonical_digest(recovery)}
                ]
            }
        ),
    )

    class Objects:
        def read_input(self, name, digest, *, timeout):
            assert (name, digest) == ("recovery_context", module.canonical_digest(recovery))
            input_reads.append(name)
            return module.canonical_bytes(recovery)

    monkeypatch.setattr(
        "src.analysis.open_intelligence.production_snapshot_storage.SourceCaptureObjects",
        lambda _: Objects(),
    )
    monkeypatch.setattr(
        "google.cloud.bigquery.Client", lambda **_: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(
        "google.cloud.storage.Client",
        lambda **_: SimpleNamespace(bucket=lambda _: None, close=lambda: None),
    )
    monkeypatch.setattr(
        ledger,
        "GeneralQuestionQueries",
        lambda _: SimpleNamespace(read_query=lambda *args, ordinal, **kwargs: records[ordinal]),
    )

    def captured(kind, ordinal, maximum_bytes_billed, material, count=0):
        ledger._ordinal(ordinal)
        trace.append((kind, ordinal, maximum_bytes_billed, count))
        rows = [{"kind": kind}]
        receipt = {"result_digest": module.canonical_digest(rows)}
        records[ordinal] = {"reservation": {"ordinal": ordinal}, "receipt": receipt}
        return {
            "application_status": "accepted",
            "rows": rows,
            "receipt": receipt,
            "material": material,
            "prepared_query": SimpleNamespace(template_id=kind),
        }

    def single(*args, consumption_id, ordinal, maximum_bytes_billed, **kwargs):
        assert consumption_id == pin["consumption_id"]
        return captured(
            "result",
            ordinal,
            maximum_bytes_billed,
            {"approval": current_approval, "result": {"result_id": pin["result_id"]}},
        )

    def batch(*args, consumption_ids, ordinal, maximum_bytes_billed, **kwargs):
        assert tuple(consumption_ids) == identifiers
        return captured(
            "results",
            ordinal,
            maximum_bytes_billed,
            {
                context["initial_consumption_id"]: {
                    "approval": SimpleNamespace(manifest_sha256=context["initial_manifest_sha256"]),
                    "result": {"result_id": context["initial_result_id"]},
                }
                for context in contexts
            },
        )

    monkeypatch.setattr(execution, "execute_context_result_query", single)
    monkeypatch.setattr(execution, "execute_context_result_batch_query", batch)
    monkeypatch.setattr(
        execution,
        "execute_context_approval_query",
        lambda *args, **kwargs: pytest.fail("approval cache miss"),
    )

    def load(*args, result_reader, approval_reader, **kwargs):
        assert result_reader(pin["consumption_id"])[0]["result_id"] == pin["result_id"]
        for context in contexts:
            assert (
                result_reader(context["initial_consumption_id"])[0]["result_id"]
                == context["initial_result_id"]
            )
            assert (
                result_reader(context["initial_consumption_id"])[0]["result_id"]
                == context["initial_result_id"]
            )
            assert (
                approval_reader(context["initial_manifest_sha256"]).manifest_sha256
                == context["initial_manifest_sha256"]
            )
        return SimpleNamespace(
            source_binding={
                "profile_id": pin["profile_id"],
                "market_scope": ["ke", "ng", "za"],
                **SYNTHETIC_BINDING,
            }
        )

    monkeypatch.setattr(admission, "load_protected_context_source", load)
    monkeypatch.setattr(
        execution,
        "execute_context_candidate_query",
        lambda *args, ordinal, maximum_bytes_billed, candidate_limit, **kwargs: captured(
            "candidate",
            ordinal,
            maximum_bytes_billed,
            {
                "candidate_count": 1,
                "records": [{"sample_row_ids": [] if direct else ["synthetic"]}],
            },
            candidate_limit,
        ),
    )
    monkeypatch.setattr(
        execution,
        "execute_context_partition_query",
        lambda *args, ordinal, maximum_bytes_billed, **kwargs: captured(
            "partitions", ordinal, maximum_bytes_billed, {}, 0
        ),
    )
    for name, index in [
        ("execute_context_index_query", True),
        ("execute_context_evidence_query", False),
    ]:

        def data(
            *args, lane, ordinal, maximum_bytes_billed, candidate_limit, _index=index, **kwargs
        ):
            return captured(
                lane + ("_index" if _index else ""),
                ordinal,
                maximum_bytes_billed,
                {
                    "candidate_count": min(enriched_count, candidate_limit)
                    if lane == "enriched_content" and not _index
                    else 0
                },
                candidate_limit,
            )

        monkeypatch.setattr(execution, name, data)
    monkeypatch.setattr(
        admission, "admit_protected_context", lambda *args, **kwargs: synthetic_admission()
    )
    if raw_parent:
        from src.analysis.open_intelligence import general_question_parent_context

        plan["parent_receipt_aliases"] = ["s01"]
        parent = {
            "parent_receipt_refs": [
                {
                    "alias": "s01",
                    "source_preview": {"text": "source"},
                    "market": "za",
                    "source_row_id": "raw_parent",
                    "content_digest": "a" * 64,
                    "lane": "raw_content",
                }
            ]
        }
        monkeypatch.setattr(
            general_question_parent_context, "read_parent_context", lambda *args: parent
        )
    result = module.build_general_question_snapshot(
        {"request_id": "request"},
        store=store,
        scope={},
        runtime_identity={},
        credentials=object(),
        plan=plan,
        candidate_limit=100,
        evidence_limit=200,
        discovery_bytes=100000000,
        source_bytes=300000000,
        release_bytes=50000000,
        now=datetime(2026, 9, 6, 20, tzinfo=UTC),
    )
    assert result["status"] == "admitted"
    assert plan["markets"] == ["za"]
    assert result["snapshot"]["provenance"]["source_binding"]["market_scope"] == ["ke", "ng", "za"]
    assert len(trace) == 8
    assert sum(row[2] for row in trace) == 1000000000
    assert next(row[2] for row in trace if row[0] == "partitions") == 60000000
    assert sum(row[3] for row in trace) <= 900
    limits = result["snapshot"]["provenance"]["limits"]
    assert limits["evidence_limit"] == 200
    effective = 200 if direct and not raw_parent else 24
    assert limits.get("effective_evidence_limit", 200) == effective
    assert limits["enriched_limit"] == effective - int(raw_parent)
    assert limits["raw_limit"] == effective - min(enriched_count, effective - int(raw_parent))
    if raw_parent:
        assert limits["raw_limit"] >= 1
        assert next(row[3] for row in trace if row[0] == "enriched_content") == 23
    assert [row[0] for row in trace][-2:] == ["raw_content_index", "raw_content"]
    assert input_reads == ["recovery_context"]
    assert [value["kind"] for value in result["snapshot"]["provenance"]["authority_captures"]] == [
        "result",
        "results",
    ]


# Evidence support reader: identity, shared origin and independence read from the record.


def support_context():
    return {
        "request": {
            "request_id": "request",
            "request_digest": "a" * 64,
            "as_of": "2026-09-08T12:00:00Z",
        },
        "intake": {"intake_digest": "b" * 64},
        "admission": {"policy_digest": "c" * 64, "deployment_digest": "d" * 64},
    }


def support_plan():
    return {
        "plan_digest": "e" * 64,
        "window": {"start": "2026-08-25", "end": "2026-09-07", "closed": True},
        "requirements": [{"requirement_id": "content", "mandatory": True}],
    }


def support_receipt(receipt_id, *, platform, source_row_id, source_family="news", label="R1"):
    return {
        "receipt_id": receipt_id,
        "citation_label": label,
        "kind": "content",
        "snapshot_id": "gqs_" + "f" * 64,
        "market": "za",
        "source_label": "publisher",
        "source_family": source_family,
        "platform": platform,
        "author": None,
        "url": None,
        "source_row_id": source_row_id,
        "published_at": "2026-09-01T00:00:00+00:00",
        "collected_at": "2026-09-02T00:00:00+00:00",
        "excerpt": "one story",
        "reading_ids": [],
        "limitations": [],
        "content_digest": "1" * 64,
    }


SYNDICATED = (
    ("gqctx_web", "web", "row-a"),
    ("gqctx_facebook", "facebook", "row-b"),
    ("gqctx_x", "x", "row-c"),
)


def support_snapshot(*, origin_id="wire-story-7", unknown=(), profile=None):
    """A stored context snapshot whose sidecar carries the derived groups of its receipts."""
    receipts = [
        support_receipt(receipt_id, platform=platform, source_row_id=row_id, label=f"R{index + 1}")
        for index, (receipt_id, platform, row_id) in enumerate([*SYNDICATED, *unknown])
    ]
    keys = [[row["market"], row["platform"], row["source_row_id"]] for row in receipts]
    shared = sorted(keys[: len(SYNDICATED)])
    admission = synthetic_admission(
        receipts=receipts,
        observation_keys=sorted(keys),
        origin_groups={origin_id: shared} if origin_id is not None else {},
        independent_origin_count=0 if origin_id is None else 1,
        unknown_origin_count=len(keys) - (0 if origin_id is None else len(shared)),
        origin_authority_projection={
            "state": "not_projected" if origin_id is None else "projected",
            "projected_records": 0 if origin_id is None else len(keys),
            "unprojected_records": len(keys) if origin_id is None else 0,
        },
    )
    binding = {"profile_id": "protected_context_20260907_v1", **SYNTHETIC_BINDING}
    provenance = {
        "profile": profile or snapshot._CURRENT_CONTEXT_PROFILE,
        "source_binding": binding,
        "admission": copy.deepcopy(admission),
        "identity_sidecar": snapshot._identity_sidecar(admission, binding),
    }
    if profile == snapshot._CONTEXT_PROFILE_V1:
        provenance.pop("identity_sidecar")
    return snapshot._project_context_snapshot(
        support_context(), support_plan(), admission, provenance
    )


def cited(stored):
    """Every receipt the stored snapshot carries, as the cited set the reader requires."""
    return [row["receipt_id"] for row in stored["receipts"]]


def resign(stored):
    """Re-sign the snapshot over its own bytes, the way the projection does."""
    stored["snapshot_digest"] = snapshot.canonical_digest(
        {key: value for key, value in stored.items() if key != "snapshot_digest"}
    )
    return stored


def resign_sidecar(stored):
    """Re-sign only the identity sidecar, the way a tamperer holding the record would."""
    sidecar = stored["provenance"]["identity_sidecar"]
    sidecar["sidecar_digest"] = snapshot.canonical_digest(
        {key: value for key, value in sidecar.items() if key != "sidecar_digest"}
    )
    return stored


def rederive_sidecar(stored):
    """Rebuild the sidecar from the admission the snapshot carries, then re-sign the bytes."""
    provenance = stored["provenance"]
    provenance["identity_sidecar"] = json.loads(
        snapshot.canonical_bytes(
            snapshot._identity_sidecar(provenance["admission"], provenance["source_binding"])
        )
    )
    return resign(stored)


def test_syndicated_receipts_fill_one_independent_support_slot():
    stored = support_snapshot()
    result = snapshot.read_context_evidence_support(stored, cited(stored))
    assert result["contract_version"] == "general_question_evidence_support_v1"
    assert result["independent_support_slots"] == 1
    assert result["unknown_support_slots"] == 0
    assert [slot["receipt_ids"] for slot in result["slots"]] == [
        ["gqctx_facebook", "gqctx_web", "gqctx_x"]
    ]
    assert result["slots"][0]["origin_id"] == "wire-story-7"
    assert result["slots"][0]["independence"] == "verified_origin"
    assert result["units"] == {
        "collected_records": 3,
        "unique_observations": 3,
        "source_families": 1,
        "verified_independent_origins": 1,
        "unknown_origins": 0,
    }
    assert result["limitations"][0] == (
        "Cited evidence names its units: 3 collected records, 3 unique observations, "
        "1 source families, 1 verified independent origins; 0 cited observations carry "
        "no verified origin. None of these counts is population prevalence."
    )
    assert any(
        line.startswith("Records sharing one verified origin fill a single independent support")
        for line in result["limitations"]
    )
    assert (
        result["identity_policy_digest"]
        == support_snapshot()["provenance"]["identity_sidecar"]["identity_policy_digest"]
    )


def test_unknown_origin_receipts_stay_unknown_and_never_independent():
    unknown = (("gqctx_unknown", "web", "row-z"),)
    stored = support_snapshot(unknown=unknown)
    result = snapshot.read_context_evidence_support(stored, cited(stored))
    assert result["independent_support_slots"] == 1
    assert result["unknown_support_slots"] == 1
    assert result["slots"][-1] == {
        "origin_id": None,
        "independence": "unknown",
        "observation_keys": [["za", "web", "row-z"]],
        "receipt_ids": ["gqctx_unknown"],
    }
    assert result["units"]["unknown_origins"] == 1
    assert any(
        line
        == (
            "Independence is unknown for 1 cited observations; an unverified origin is "
            "never independent support."
        )
        for line in result["limitations"]
    )
    bare = support_snapshot(origin_id=None)
    without_origin = snapshot.read_context_evidence_support(bare, cited(bare))
    assert without_origin["independent_support_slots"] == 0
    assert without_origin["unknown_support_slots"] == 3


def test_support_reads_only_the_cited_receipts():
    unknown = (("gqctx_unknown", "web", "row-z"),)
    stored = support_snapshot(unknown=unknown)
    result = snapshot.read_context_evidence_support(stored, ["gqctx_web", "gqctx_unknown"])
    assert result["units"]["collected_records"] == 2
    assert result["independent_support_slots"] == 1
    assert result["unknown_support_slots"] == 1
    assert [slot["receipt_ids"] for slot in result["slots"]] == [["gqctx_web"], ["gqctx_unknown"]]


def test_support_is_unavailable_without_a_version_two_identity_record():
    stored = support_snapshot(profile=snapshot._CONTEXT_PROFILE_V1)
    with pytest.raises(ValueError, match="context_identity_unavailable"):
        snapshot.read_context_evidence_support(stored, cited(stored))
    released = copy.deepcopy(stored)
    released["provenance"] = {"profile": "released_v1"}
    with pytest.raises(ValueError, match="context_identity_unavailable"):
        snapshot.read_context_evidence_support(released, cited(released))
    # A retained profile that carries a sidecar anyway is still read under its own version.
    relabelled = support_snapshot()
    relabelled["provenance"]["profile"] = snapshot._CONTEXT_PROFILE_V1
    with pytest.raises(ValueError, match="context_identity_unavailable"):
        snapshot.read_context_evidence_support(relabelled, cited(relabelled))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("independent_origin_count", 3),
        ("unknown_origin_count", 5),
        ("identity_policy_digest", "0" * 64),
        ("snapshot_id", "gqs_" + "0" * 64),
    ],
)
def test_tampered_sidecar_refuses_before_any_support_is_read(field, value):
    stored = support_snapshot()
    stored["provenance"]["identity_sidecar"][field] = value
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))


def test_sidecar_bound_to_another_snapshot_refuses():
    stored = support_snapshot()
    sidecar = stored["provenance"]["identity_sidecar"]
    sidecar["snapshot_id"] = "gqs_" + "9" * 64
    resign(resign_sidecar(stored))
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))


def test_sidecar_of_another_contract_version_or_without_its_digest_refuses():
    stored = support_snapshot()
    sidecar = stored["provenance"]["identity_sidecar"]
    sidecar["contract_version"] = "general_question_identity_sidecar_v9"
    resign(resign_sidecar(stored))
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))
    bare = support_snapshot()
    bare["provenance"]["identity_sidecar"].pop("sidecar_digest")
    resign(bare)
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(bare, cited(bare))


def test_receipt_outside_the_derived_observation_keys_refuses():
    stored = support_snapshot()
    stored["receipts"][0]["source_row_id"] = "row-not-derived"
    resign(stored)
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))


def test_one_observation_claimed_by_two_origins_refuses():
    stored = support_snapshot()
    stored["provenance"]["admission"]["origin_groups"]["statement-19"] = [["za", "web", "row-a"]]
    rederive_sidecar(stored)
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))


@pytest.mark.parametrize(
    "receipt_ids",
    [
        ["gqctx_absent"],
        ["gqctx_web", "gqctx_web"],
        [],
        "gqctx_web",
        [7],
        7,
        {"gqctx_web": 1},
        None,
    ],
)
def test_support_refuses_receipt_ids_the_record_does_not_carry(receipt_ids):
    with pytest.raises(ValueError, match="context_support_request_invalid"):
        snapshot.read_context_evidence_support(support_snapshot(), receipt_ids)


def test_the_public_reader_requires_the_cited_set():
    """Independence is a property of the cited set, so the reader never invents one.

    A default of every retained receipt reads the wrong set for any caller but the answer
    helper, and reports uncited receipts as support. The cited set is an argument.
    """
    stored = support_snapshot()
    with pytest.raises(TypeError):
        snapshot.read_context_evidence_support(stored)
    every = snapshot.read_context_evidence_support(stored, cited(stored))
    one = snapshot.read_context_evidence_support(stored, ["gqctx_web"])
    assert every["units"]["collected_records"] == 3
    assert one["units"]["collected_records"] == 1
    assert one["slots"][0]["receipt_ids"] == ["gqctx_web"]


def test_re_signing_the_sidecar_cannot_turn_unknown_observations_into_a_verified_origin():
    """The reader binds the record it reads instead of taking the sidecar's word for it.

    A tamperer holding the retained snapshot can re-sign the sidecar over whatever groups
    it likes, so the sidecar's own digest proves only that the sidecar is self consistent.
    The reader rebuilds the sidecar from the admission and the source binding the snapshot
    carries and refuses when the two differ, and it holds whether or not the caller checked
    the snapshot digest first.
    """
    stored = support_snapshot(origin_id=None)
    honest = snapshot.read_context_evidence_support(stored, cited(stored))
    assert (honest["independent_support_slots"], honest["unknown_support_slots"]) == (0, 3)
    sidecar = stored["provenance"]["identity_sidecar"]
    sidecar["origin_groups"] = {"wire-story-7": sidecar["observation_keys"]}
    sidecar["independent_origin_count"] = 1
    sidecar["unknown_origin_count"] = 0
    resign_sidecar(stored)
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))
    resign(stored)
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))


def test_the_reader_refuses_a_snapshot_whose_bytes_no_longer_match_its_digest():
    stored = support_snapshot()
    stored["as_of"] = "2026-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))


def test_the_reader_refuses_a_sidecar_bound_to_another_snapshot_id():
    stored = support_snapshot()
    stored["snapshot_id"] = "gqs_" + "9" * 64
    resign(stored)
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot.read_context_evidence_support(stored, cited(stored))


def test_support_says_when_origin_authority_was_never_projected():
    """A zero that means the column does not exist must not read as a measured zero."""
    stored = support_snapshot(origin_id=None)
    result = snapshot.read_context_evidence_support(stored, cited(stored))
    assert result["origin_authority_projection"] == {
        "state": "not_projected",
        "projected_records": 0,
        "unprojected_records": 3,
    }
    assert result["units"]["verified_independent_origins"] == 0
    assert result["limitations"][0].startswith("Cited evidence names its units")
    assert result["limitations"][-1].startswith("Independence is unknown for")
    assert any(
        line.startswith("Origin authority is not projected") for line in result["limitations"]
    )
    measured = support_snapshot()
    reading = snapshot.read_context_evidence_support(measured, cited(measured))
    assert reading["origin_authority_projection"]["state"] == "projected"
    assert not any(
        line.startswith("Origin authority is not projected") for line in reading["limitations"]
    )


def duplicate_receipt_snapshot():
    """One observation cited twice under one verified origin: two records, one observation."""
    receipts = [
        support_receipt("gqctx_raw", platform="web", source_row_id="row-a", label="R1"),
        support_receipt("gqctx_enriched", platform="web", source_row_id="row-a", label="R2"),
    ]
    keys = [["za", "web", "row-a"]]
    admission = synthetic_admission(
        receipts=receipts,
        observation_keys=keys,
        origin_groups={"wire-story-7": keys},
        independent_origin_count=1,
        unknown_origin_count=0,
        origin_authority_projection={
            "state": "projected",
            "projected_records": 2,
            "unprojected_records": 0,
        },
    )
    binding = {"profile_id": "protected_context_20260907_v1", **SYNTHETIC_BINDING}
    provenance = {
        "profile": snapshot._CURRENT_CONTEXT_PROFILE,
        "source_binding": binding,
        "admission": copy.deepcopy(admission),
        "identity_sidecar": snapshot._identity_sidecar(admission, binding),
    }
    return snapshot._project_context_snapshot(
        support_context(), support_plan(), admission, provenance
    )


def test_the_shared_origin_line_counts_cited_records_and_not_observations():
    """The line says how many cited records one origin covers, which is the slot's point."""
    stored = duplicate_receipt_snapshot()
    result = snapshot.read_context_evidence_support(stored, cited(stored))
    assert result["units"]["collected_records"] == 2
    assert result["units"]["unique_observations"] == 1
    assert result["slots"][0]["receipt_ids"] == ["gqctx_enriched", "gqctx_raw"]
    assert result["slots"][0]["observation_keys"] == [["za", "web", "row-a"]]
    assert (
        "Records sharing one verified origin fill a single independent support slot: "
        "wire-story-7 covers 2 cited records."
    ) in result["limitations"]


# The cited units reach the answer a reader sees.


def answer_values_with_shared_origin(origin_id="wire-story-7"):
    """The answer fixture, with two syndicated copies of one story cited by the answer."""
    from tests.unit import test_general_question_answer as answer_fixture

    request, intake, plan, stored, usage, output, policy = answer_fixture.fixture()
    first = stored["receipts"][0]
    first["platform"] = "web"
    first["source_row_id"] = "row-a"
    second = copy.deepcopy(first)
    second.update(
        receipt_id="receipt_copy",
        citation_label="R10",
        platform="facebook",
        source_row_id="row-b",
        content_digest="b" * 64,
        excerpt="Neighbours share tools at the repair cafe.",
        reading_ids=[],
    )
    stored["receipts"].append(second)
    observed = copy.deepcopy(output["claims"][2])
    observed.update(
        claim_id="observed_copy",
        segments=[{"kind": "quote", "receipt_id": "receipt_copy", "text": second["excerpt"]}],
        receipt_ids=["receipt_copy"],
    )
    output["claims"].append(observed)
    output["sections"][0]["claim_ids"].append("observed_copy")
    keys = [[row["market"], row["platform"], row["source_row_id"]] for row in stored["receipts"]]
    admission = synthetic_admission(
        snapshot_id=stored["snapshot_id"],
        receipts=copy.deepcopy(stored["receipts"]),
        observation_keys=sorted(keys),
        origin_groups={origin_id: sorted(keys)} if origin_id is not None else {},
        independent_origin_count=0 if origin_id is None else 1,
        unknown_origin_count=0 if origin_id is not None else len(keys),
        origin_authority_projection={
            "state": "not_projected" if origin_id is None else "projected",
            "projected_records": 0 if origin_id is None else len(keys),
            "unprojected_records": len(keys) if origin_id is None else 0,
        },
    )
    binding = {"profile_id": "protected_context_20260907_v1", **SYNTHETIC_BINDING}
    stored["provenance"] = {
        "profile": snapshot._CURRENT_CONTEXT_PROFILE,
        "source_binding": binding,
        "admission": copy.deepcopy(admission),
        "identity_sidecar": snapshot._identity_sidecar(admission, binding),
    }
    answer_fixture.rehash(stored)
    return (request, intake, plan, stored, usage, output, policy)


def test_two_copies_of_one_story_fill_one_independent_support_slot_in_the_answer():
    from tests.unit import test_general_question_answer as answer_fixture

    result = answer_fixture.project(answer_values_with_shared_origin())
    limitations = result["intelligence"]["limitations"]
    assert [row["citation_label"] for row in result["intelligence"]["receipts"]] == ["R1", "R2"]
    assert (
        "Cited evidence names its units: 2 collected records, 2 unique observations, "
        "1 source families, 1 verified independent origins; 0 cited observations carry "
        "no verified origin. None of these counts is population prevalence."
    ) in limitations
    assert (
        "Records sharing one verified origin fill a single independent support slot: "
        "wire-story-7 covers 2 cited records."
    ) in limitations
    assert all("Limitation: " + line in result["answer"] for line in limitations)


def test_unknown_independence_stays_unknown_in_the_projected_answer():
    from tests.unit import test_general_question_answer as answer_fixture

    result = answer_fixture.project(answer_values_with_shared_origin(origin_id=None))
    limitations = result["intelligence"]["limitations"]
    assert (
        "Independence is unknown for 2 cited observations; an unverified origin is never "
        "independent support."
    ) in limitations
    assert (
        "Cited evidence names its units: 2 collected records, 2 unique observations, "
        "1 source families, 0 verified independent origins; 2 cited observations carry "
        "no verified origin. None of these counts is population prevalence."
    ) in limitations
    assert not any("verified independent origins; 0 cited" in line for line in limitations)


def test_a_retained_snapshot_without_the_identity_record_gains_no_support_lines():
    """A retained v1 snapshot does reach an answer; it says nothing about independence.

    The reader refuses a snapshot with no version two identity record, but the answer does
    not: it projects the same answer with no unit line, no shared origin line and no
    unknown independence line, so a retained snapshot claims no independence it cannot
    establish rather than being unable to reach an answer at all.
    """
    from tests.unit import test_general_question_answer as answer_fixture

    values = list(answer_values_with_shared_origin())
    values[3]["provenance"]["profile"] = snapshot._CONTEXT_PROFILE_V1
    values[3]["provenance"].pop("identity_sidecar")
    answer_fixture.rehash(values[3])
    result = answer_fixture.project(tuple(values))
    assert not any(
        line.startswith("Cited evidence names its units")
        for line in result["intelligence"]["limitations"]
    )


def test_a_snapshot_with_no_provenance_record_answers_without_support_lines():
    """The guard that keeps the reader out of a snapshot with no record is pinned on both
    of its arms: a provenance that is not a record at all, and a record with no sidecar."""
    from tests.unit import test_general_question_answer as answer_fixture

    for provenance in (None, [], "none"):
        values = list(answer_values_with_shared_origin())
        values[3]["provenance"] = provenance
        answer_fixture.rehash(values[3])
        result = answer_fixture.project(tuple(values))
        assert not any(
            line.startswith("Cited evidence names its units")
            for line in result["intelligence"]["limitations"]
        )
    missing = list(answer_values_with_shared_origin())
    del missing[3]["provenance"]
    answer_fixture.rehash(missing[3])
    assert not any(
        line.startswith("Cited evidence names its units")
        for line in answer_fixture.project(tuple(missing))["intelligence"]["limitations"]
    )


def test_an_unusable_identity_record_refuses_the_answer_by_name():
    """A record that is present but cannot be read is a refusal, not an exception escaping.

    A sidecar that is not a record at all, and a retained profile carrying a sidecar it
    never declared, both used to raise the context reader's own error out of the answer
    projection. The answer names its own refusal instead.
    """
    from tests.unit import test_general_question_answer as answer_fixture

    values = list(answer_values_with_shared_origin())
    values[3]["provenance"]["identity_sidecar"] = "not a record"
    answer_fixture.rehash(values[3])
    with pytest.raises(ValueError, match="answer_identity_record_invalid"):
        answer_fixture.project(tuple(values))
    retained = list(answer_values_with_shared_origin())
    retained[3]["provenance"]["profile"] = snapshot._CONTEXT_PROFILE_V1
    answer_fixture.rehash(retained[3])
    with pytest.raises(ValueError, match="answer_identity_record_invalid"):
        answer_fixture.project(tuple(retained))
    tampered = list(answer_values_with_shared_origin())
    tampered[3]["provenance"]["identity_sidecar"]["origin_groups"] = {"forged": []}
    answer_fixture.rehash(tampered[3])
    with pytest.raises(ValueError, match="answer_identity_record_invalid"):
        answer_fixture.project(tuple(tampered))


def test_only_the_cited_receipts_reach_the_answer_units():
    from tests.unit import test_general_question_answer as answer_fixture

    values = list(answer_values_with_shared_origin())
    stored = values[3]
    spare = copy.deepcopy(stored["receipts"][1])
    spare.update(
        receipt_id="receipt_spare",
        citation_label="R11",
        platform="x",
        source_row_id="row-c",
        content_digest="c" * 64,
    )
    stored["receipts"].append(spare)
    admission = copy.deepcopy(stored["provenance"]["admission"])
    keys = [[row["market"], row["platform"], row["source_row_id"]] for row in stored["receipts"]]
    admission.update(
        receipts=copy.deepcopy(stored["receipts"]),
        observation_keys=sorted(keys),
        origin_groups={"wire-story-7": sorted(keys)},
    )
    stored["provenance"]["admission"] = admission
    stored["provenance"]["identity_sidecar"] = snapshot._identity_sidecar(
        admission, stored["provenance"]["source_binding"]
    )
    answer_fixture.rehash(stored)
    result = answer_fixture.project(tuple(values))
    assert len(result["intelligence"]["receipts"]) == 2
    assert any(
        line.startswith("Cited evidence names its units: 2 collected records")
        for line in result["intelligence"]["limitations"]
    )


def one_record_per_origin_snapshot():
    """Three cited records, three verified origins, each covering exactly one record."""
    receipts = [
        support_receipt(receipt_id, platform=platform, source_row_id=row_id, label=f"R{index + 1}")
        for index, (receipt_id, platform, row_id) in enumerate(SYNDICATED)
    ]
    keys = [[row["market"], row["platform"], row["source_row_id"]] for row in receipts]
    admission = synthetic_admission(
        receipts=receipts,
        observation_keys=sorted(keys),
        origin_groups={f"publisher-{index}": [key] for index, key in enumerate(sorted(keys))},
        independent_origin_count=3,
        unknown_origin_count=0,
        origin_authority_projection={
            "state": "projected",
            "projected_records": 3,
            "unprojected_records": 0,
        },
    )
    binding = {"profile_id": "protected_context_20260907_v1", **SYNTHETIC_BINDING}
    provenance = {
        "profile": snapshot._CURRENT_CONTEXT_PROFILE,
        "source_binding": binding,
        "admission": copy.deepcopy(admission),
        "identity_sidecar": snapshot._identity_sidecar(admission, binding),
    }
    return snapshot._project_context_snapshot(
        support_context(), support_plan(), admission, provenance
    )


def test_no_shared_origin_line_when_every_verified_origin_covers_one_record():
    """The shared origin line says records share an origin, so one record never shares one.

    A slot covering a single record is ordinary independent support. Printing the shared
    origin line for it tells a reader that one record shares an origin with itself and fills
    a single slot, which reads as a warning about evidence that carries none.
    """
    stored = one_record_per_origin_snapshot()
    result = snapshot.read_context_evidence_support(stored, cited(stored))
    assert result["independent_support_slots"] == 3
    assert result["units"]["verified_independent_origins"] == 3
    assert all(len(slot["receipt_ids"]) == 1 for slot in result["slots"])
    assert not any(
        line.startswith("Records sharing one verified origin") for line in result["limitations"]
    )
    shared = support_snapshot()
    lines = snapshot.read_context_evidence_support(shared, cited(shared))["limitations"]
    assert any(line.startswith("Records sharing one verified origin") for line in lines)


def test_an_identity_profile_without_a_sidecar_is_unavailable_rather_than_unreadable():
    """The named path for no identity record, with a snapshot digest that is correct.

    A snapshot whose digest is wrong is invalid; a snapshot whose digest is right and which
    simply carries no identity record is unavailable. The two are different answers and the
    profile gate is what separates them.
    """
    for profile in sorted(snapshot._IDENTITY_PROFILES):
        stored = support_snapshot()
        stored["provenance"]["profile"] = profile
        stored["provenance"].pop("identity_sidecar")
        resign(stored)
        assert stored["snapshot_digest"] == snapshot.canonical_digest(
            {key: value for key, value in stored.items() if key != "snapshot_digest"}
        )
        with pytest.raises(ValueError, match="context_identity_unavailable"):
            snapshot.read_context_evidence_support(stored, cited(stored))


def test_the_absent_projection_caveat_is_published_only_beside_a_zero_count():
    """The caveat explains a zero, so it cannot stand beside a count that is not zero."""
    support = {
        "slots": [],
        "unknown_support_slots": 0,
        "units": {
            "collected_records": 2,
            "unique_observations": 2,
            "source_families": 1,
            "verified_independent_origins": 1,
            "unknown_origins": 0,
        },
    }
    for state in ("not_projected", "not_recorded", "projected"):
        lines = snapshot._support_limitations(support, {"state": state})
        assert not any(line.startswith("Origin authority is not projected") for line in lines)
    zero = copy.deepcopy(support)
    zero["units"]["verified_independent_origins"] = 0
    for state in ("not_projected", "not_recorded"):
        lines = snapshot._support_limitations(zero, {"state": state})
        assert any(line.startswith("Origin authority is not projected") for line in lines)
    projected = snapshot._support_limitations(zero, {"state": "projected"})
    assert not any(line.startswith("Origin authority is not projected") for line in projected)


def test_the_projected_answer_never_prints_the_caveat_beside_its_own_count():
    """The lane fixture printed one verified independent origin and denied it in the next line."""
    from tests.unit import test_general_question_answer as answer_fixture

    result = answer_fixture.project(answer_values_with_shared_origin())
    limitations = result["intelligence"]["limitations"]
    assert any("1 verified independent origins" in line for line in limitations)
    assert not any(line.startswith("Origin authority is not projected") for line in limitations)
    unknown = answer_fixture.project(answer_values_with_shared_origin(origin_id=None))
    unknown_lines = unknown["intelligence"]["limitations"]
    assert any("0 verified independent origins" in line for line in unknown_lines)
    assert any(line.startswith("Origin authority is not projected") for line in unknown_lines)
