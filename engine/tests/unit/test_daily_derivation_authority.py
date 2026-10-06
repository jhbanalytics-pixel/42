import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from src.analysis.open_intelligence import (
    daily_cycle,
    daily_operation_map,
    daily_store,
    execution_generations,
)
from src.analysis.open_intelligence import daily_derivation_authority as subject
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.daily_cost_policy import (
    MAX_RATES_BYTES,
    RATES_OBJECT,
    build_daily_cost_policy,
)
from src.analysis.open_intelligence.daily_execution_contracts import (
    validate_artifact_set,
    validate_operation_context,
)

from tests.unit.test_daily_store import FakeObjectClient

CUTOFF = datetime(2026, 9, 23, tzinfo=UTC)
NOW = CUTOFF + timedelta(hours=3)
DAILY_JOB = "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
PARENT = DAILY_JOB + "/executions/parent-1"
ORCHESTRATION = "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
IMAGE_DIGEST = "1" * 64
IMAGE = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:" + IMAGE_DIGEST
SOURCE_SHA = "2" * 40
JOB_POLICY = "3" * 64
APPROVAL = "exa_" + "4" * 64


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def grant(**changes):
    value = {
        "allowed_operations": sorted(
            [
                "daily_collection_exposure_issue",
                "daily_quality_proof_issue",
                "daily_source_collection",
                "daily_source_snapshot_capture",
                "daily_staging_release",
            ]
        ),
        "build_provenance_bindings": [
            {
                "build_id": "build-1",
                "image_digest": IMAGE_DIGEST,
                "image_uri": IMAGE,
                "provenance_record_sha256": "5" * 64,
                "receipt_sha256": "6" * 64,
                "source_sha": SOURCE_SHA,
            }
        ],
        "contract_version": "42_recurring_execution_grant_v2",
        "cumulative_ceiling_micro_usd": 1000000,
        "environment": "staging",
        "executing_principals": [ORCHESTRATION],
        "grant_id": "grant-1",
        "issuing_principal": "albert.meintjes@ogilvy.co.za",
        "job_policy_bindings": [{"job_policy_sha256": JOB_POLICY, "job_resource": DAILY_JOB}],
        "monthly_allowance_micro_usd": 10000000,
        "monthly_credit_cap": 20000,
        "permitted_cutoffs": ["2026-09-22"],
        "release_policy_digest": "7" * 64,
        "reserved_micro_usd_per_capture": 500000,
        "resource_manifest_digest": "8" * 64,
        "retry_rules": {"failed_stage": "retry_safe_only"},
        "run_allowance_micro_usd": 2000000,
        "run_credit_cap": 620,
        "schema_version": "42_daily_v2",
        "source_estate_id": "intelligence-42-core",
        "source_policy_digest": "9" * 64,
        "valid_from": (CUTOFF - timedelta(days=1)).isoformat(),
        "valid_until": (CUTOFF + timedelta(days=20)).isoformat(),
    }
    value.update(changes)
    return value


def grant_row(value=None, **changes):
    value = grant() if value is None else value
    row = {
        "grant": value,
        "grant_sha256": digest(value),
        "approval_id": APPROVAL,
        "state": "active",
    }
    row.update(changes)
    return row


def profile(value=None):
    value = grant() if value is None else value
    return {
        "schema_version": "42_daily_v2",
        "source_estate_id": "intelligence-42-core",
        "resource_manifest_digest": value["resource_manifest_digest"],
        "source_policy_digest": value["source_policy_digest"],
        "recurring_grant_digest": digest(value),
        "freshness_target_hours": 30,
        "max_publish_lag_hours": 48,
        "max_catchup_cutoffs": 2,
    }


def parent(**changes):
    value = subject.ParentFacts(
        execution_name=PARENT,
        job_resource=DAILY_JOB,
        principal=ORCHESTRATION,
        image_uri=IMAGE,
        source_sha=SOURCE_SHA,
        job_policy_digest=JOB_POLICY,
    )
    return replace(value, **changes)


def registry():
    return execution_generations.active_generation().registry


STORED = NOW - timedelta(minutes=50)
ADMINISTRATIVE_BYTES = 2**30


def rates(**changes):
    value = {
        "contract_version": "daily_workload_rates_v1",
        "observed_at": (NOW - timedelta(hours=1)).isoformat(),
        "pricing_sources": ["https://cloud.google.com/bigquery/pricing"],
        "query_micro_usd_per_tib": 6250000,
        "vendor_credit_micro_usd": 1000,
        "model_call_micro_usd": 20000,
    }
    value.update(changes)
    return value


class RatesObjects:
    """The rates object as the provider returns it: bytes and storage facts."""

    def __init__(self, record=None, **storage):
        self.raw = canonical_bytes(rates() if record is None else record)
        self.storage = {
            "content_sha256": sha256(self.raw).hexdigest(),
            "created_at": STORED.isoformat(),
            "generation": "3",
            "object_name": RATES_OBJECT,
            "size_bytes": len(self.raw),
        }
        self.storage.update(storage)
        self.calls = []

    def read(self, name, maximum_bytes):
        self.calls.append((name, maximum_bytes))
        return self.raw, dict(self.storage)


def cost_policy(
    operations=("daily_source_snapshot_capture", "daily_staging_release"),
    *,
    bindings=None,
    record=None,
    administrative_bytes_billed=ADMINISTRATIVE_BYTES,
):
    return build_daily_cost_policy(
        rates=rates() if record is None else record,
        bindings=bindings
        or {
            name: daily_operation_map.child_binding(name, registry=registry())
            for name in operations
        },
        administrative_bytes_billed=administrative_bytes_billed,
        job_policy_digest=JOB_POLICY,
        now=NOW,
        stored_at=STORED,
    )


def release_binding_as(operation):
    release = daily_operation_map.child_binding("daily_staging_release", registry=registry())
    stage = {
        "daily_source_collection": "collection",
        "daily_collection_exposure_issue": "exposure",
    }[operation]
    return replace(release, operation=operation, stage=stage)


class Derive:
    def __init__(self, objects):
        self.calls = []
        self.objects = objects
        self.row_changes = {}

    def derive_execution(self, *parameters):
        manifest_json, manifest_sha, context_json, context_sha, _artifact_json, grant_digest = (
            parameters
        )
        assert any(name.startswith("42/daily/artifact-sets/") for name in self.objects.objects)
        self.calls.append(parameters)
        context = json.loads(context_json)
        row = {
            "derivation_id": "exd_" + sha256(manifest_json.encode()).hexdigest(),
            "authorizing_approval_id": APPROVAL,
            "authorizing_grant_digest": grant_digest,
            "operation": context["operation"],
            "manifest_sha256": manifest_sha,
            "canonical_manifest_json": manifest_json,
            "operation_context_sha256": context_sha,
            "canonical_operation_context_json": context_json,
            "business_attempt_id": context["business_attempt_id"],
            "child_job_resource": context["child_job_resource"],
            "child_service_identity": context["child_service_identity"],
            "child_image_uri": context["child_image_uri"],
            "child_job_policy_digest": context["child_job_policy_digest"],
        }
        row.update(self.row_changes)
        return row


def produce(frame, binding):
    return {"release_contract": canonical_bytes({"stage": binding.stage})}


def authority(*, objects=None, store=None, producers=None, policy=None, binding_for=None, **kw):
    objects = objects or FakeObjectClient()
    store = store or daily_store.DailyStore(
        objects, owner=PARENT, now=lambda: NOW, lease_seconds=600
    )
    derive = kw.pop("derive", None) or Derive(objects)
    value = kw.pop("grant_row", None) or grant_row()
    built = subject.DailyDerivationAuthority(
        grant_row=value,
        profile=kw.pop("profile", None) or profile(value["grant"]),
        parent=kw.pop("parent", None) or parent(),
        registry=registry(),
        cost_policy=policy or cost_policy(),
        rates_objects=kw.pop("rates_objects", None) or RatesObjects(),
        administrative_bytes_billed=kw.pop("administrative_bytes_billed", ADMINISTRATIVE_BYTES),
        producers={"daily_staging_release": produce} if producers is None else producers,
        objects=objects,
        store=store,
        write_clients=derive,
        read_clients="read clients",
        now=lambda: NOW,
        **({} if binding_for is None else {"binding_for": binding_for}),
    )
    return built, derive, store, objects


def frame(stage, predecessor=("exr_" + "a" * 64, "b" * 64)):
    slot = daily_store.slot_operation_id_v2(
        environment="staging", source_estate_id="intelligence-42-core", cutoff_utc=CUTOFF
    )
    return {
        "operation_id": slot,
        "stage": stage,
        "cutoff_utc": CUTOFF.isoformat(),
        "profile": profile(),
        "predecessor_result_id": predecessor[0],
        "predecessor_result_digest": predecessor[1],
    }


LEASE = {"owner": PARENT, "epoch": 1, "expires_at": (NOW + timedelta(minutes=5)).isoformat()}


def test_prepare_builds_a_context_intent_manifest_and_artifact_set_that_agree():
    subject_authority, _derive, _store, _objects = authority()
    prepared = subject_authority.prepare("release", frame("release"), lease=LEASE)
    context = validate_operation_context(prepared["canonical_operation_context_json"])
    assert context == prepared["operation_context"]
    assert context["operation"] == "daily_staging_release"
    assert context["child_job_resource"] == DAILY_JOB
    assert context["child_service_identity"] == ORCHESTRATION
    assert context["child_image_uri"] == IMAGE
    assert context["child_job_policy_digest"] == JOB_POLICY
    assert context["parent_execution_name"] == PARENT
    assert context["parent_principal"] == ORCHESTRATION
    assert context["authorizing_approval_id"] == APPROVAL
    assert context["input_digest"] == digest(frame("release"))
    assert context["profile_digest"] == digest(profile())
    assert (
        context["gcs_control_object"]
        == f"42/daily/slots/{frame('release')['operation_id']}/control.json"
    )
    artifacts, raw = validate_artifact_set(prepared["canonical_operation_artifact_set_json"])
    assert sha256(raw).hexdigest() == context["operation_artifact_set_sha256"]
    assert set(artifacts) == {"cost_policy", "daily_profile", "release_contract"}
    assert json.loads(artifacts["cost_policy"]) == cost_policy()
    assert json.loads(artifacts["daily_profile"]) == profile()
    manifest = json.loads(prepared["canonical_manifest_json"])
    assert (
        sha256(prepared["canonical_manifest_json"].encode()).hexdigest()
        == (prepared["manifest_sha256"])
    )
    assert manifest["job_resource"] == DAILY_JOB
    assert manifest["service_identity"] == ORCHESTRATION
    assert manifest["image_uri"] == IMAGE
    assert manifest["source_sha"] == SOURCE_SHA
    assert manifest["registry_operation"] == "r3_release"
    # The bridge policy; tightening the date regex moved it from 95dcb79c.
    assert manifest["contract_sha256"] == (
        "b007a53067e2e4e841421c146eb96bedc3f269b413b253db0d21c0ebd16ce226"
    )
    assert manifest["limits"]["max_rows_written"] == 2
    inputs = {item["name"]: item["sha256"] for item in manifest["input_artifacts"]}
    assert (
        inputs.pop("operation_context")
        == sha256(prepared["canonical_operation_context_json"].encode()).hexdigest()
    )
    assert inputs == {name: sha256(body).hexdigest() for name, body in artifacts.items()}
    intent = prepared["intent"]
    assert daily_store._validate_intent_v2(intent, context["slot_id"]) == intent
    assert intent["operation_context_sha256"] == digest(context)
    assert intent["authorizing_grant_digest"] == digest(grant())


@pytest.mark.parametrize(
    ("stage", "why"),
    [
        ("collection", "unmapped"),
        ("compose", "not granted"),
        ("certify", "no producer"),
        ("capture", "no producer"),
    ],
)
def test_prepare_holds_before_any_effect_when_the_stage_cannot_run(stage, why):
    subject_authority, derive, _store, objects = authority()
    predecessor = (None, None) if stage == "collection" else ("exr_" + "a" * 64, "b" * 64)
    assert subject_authority.prepare(stage, frame(stage, predecessor), lease=LEASE) is None
    assert derive.calls == []
    assert objects.objects == {}


def test_prepare_holds_when_the_cost_policy_does_not_price_the_operation():
    subject_authority, _derive, _store, _objects = authority(
        policy=cost_policy(("daily_source_snapshot_capture",))
    )
    assert subject_authority.prepare("release", frame("release"), lease=LEASE) is None


def test_a_stale_cost_policy_refuses_at_prepare():
    subject_authority, _derive, _store, _objects = authority()
    subject_authority._now = lambda: NOW + timedelta(hours=24)
    with pytest.raises(ValueError, match=r"^daily_cost_policy_expired$"):
        subject_authority.prepare("release", frame("release"), lease=LEASE)


@pytest.mark.parametrize(
    "lease",
    [
        dict(LEASE, owner=DAILY_JOB + "/executions/other"),
        dict(LEASE, epoch=0),
        None,
    ],
)
def test_a_lease_this_parent_does_not_hold_refuses(lease):
    subject_authority, _derive, _store, _objects = authority()
    with pytest.raises(ValueError, match=r"^daily_lease_invalid$"):
        subject_authority.prepare("release", frame("release"), lease=lease)


@pytest.mark.parametrize("name", ["cost_policy", "daily_profile", "operation_context", ""])
def test_a_producer_cannot_supply_a_reserved_artifact(name):
    subject_authority, _derive, _store, _objects = authority(
        producers={"daily_staging_release": lambda frame, binding: {name: b"{}"}}
    )
    with pytest.raises(ValueError, match=r"^daily_artifact_name_reserved$"):
        subject_authority.prepare("release", frame("release"), lease=LEASE)


def test_issue_publishes_the_artifact_set_then_derives_exactly_what_was_prepared():
    subject_authority, derive, _store, objects = authority()
    prepared = subject_authority.prepare("release", frame("release"), lease=LEASE)
    derived = subject_authority.issue("release", prepared)
    context = prepared["operation_context"]
    name = f"42/daily/artifact-sets/{context['operation_artifact_set_sha256']}.json"
    assert objects.objects[name][0] == prepared["canonical_operation_artifact_set_json"].encode()
    assert derive.calls == [
        (
            prepared["canonical_manifest_json"],
            prepared["manifest_sha256"],
            prepared["canonical_operation_context_json"],
            prepared["intent"]["operation_context_sha256"],
            prepared["canonical_operation_artifact_set_json"],
            digest(grant()),
        )
    ]
    assert derived["derivation_id"].startswith("exd_")
    assert derived["operation_context_sha256"] == prepared["intent"]["operation_context_sha256"]
    assert subject_authority.issue("release", prepared)["derivation_id"] == derived["derivation_id"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authorizing_approval_id", "exa_" + "f" * 64),
        ("child_job_resource", DAILY_JOB.replace("daily", "ingest")),
        ("business_attempt_id", "bat_" + "f" * 64),
        ("manifest_sha256", "f" * 64),
        ("derivation_id", "exd_short"),
    ],
)
def test_a_derivation_readback_that_differs_refuses(field, value):
    subject_authority, derive, _store, _objects = authority()
    derive.row_changes = {field: value}
    prepared = subject_authority.prepare("release", frame("release"), lease=LEASE)
    with pytest.raises(ValueError, match=r"^daily_derivation_readback_mismatch$"):
        subject_authority.issue("release", prepared)


def test_issue_refuses_a_preparation_it_did_not_make():
    subject_authority, _derive, _store, _objects = authority()
    prepared = subject_authority.prepare("release", frame("release"), lease=LEASE)
    forged = dict(prepared, canonical_manifest_json=prepared["canonical_manifest_json"] + " ")
    with pytest.raises(ValueError, match=r"^daily_preparation_unknown$"):
        subject_authority.issue("release", forged)
    with pytest.raises(ValueError, match=r"^daily_preparation_unknown$"):
        subject_authority.issue("certify", prepared)


def test_an_artifact_set_object_with_other_bytes_refuses():
    objects = FakeObjectClient()
    subject_authority, _derive, _store, _objects = authority(objects=objects)
    prepared = subject_authority.prepare("release", frame("release"), lease=LEASE)
    name = (
        "42/daily/artifact-sets/"
        f"{prepared['operation_context']['operation_artifact_set_sha256']}.json"
    )
    objects.write(name, b"{}", if_generation_match=0)
    with pytest.raises(ValueError, match=r"^daily_artifact_set_conflict$"):
        subject_authority.issue("release", prepared)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"grant_row": grant_row(state="revoked")}, "daily_grant_inactive"),
        ({"grant_row": grant_row(grant_sha256="f" * 64)}, "daily_grant_invalid"),
        ({"grant_row": grant_row(approval_id="exa_bad")}, "daily_grant_invalid"),
        (
            {"grant_row": grant_row(grant(contract_version="42_recurring_execution_grant_v1"))},
            "daily_grant_invalid",
        ),
        (
            {"profile": dict(profile(), recurring_grant_digest="f" * 64)},
            "daily_grant_profile_mismatch",
        ),
        (
            {"profile": dict(profile(), source_policy_digest="f" * 64)},
            "daily_grant_profile_mismatch",
        ),
        (
            {"parent": parent(principal="other@ogilvy-trends-v2.iam.gserviceaccount.com")},
            "daily_parent_unbound",
        ),
        ({"parent": parent(image_uri=IMAGE.replace("1" * 64, "e" * 64))}, "daily_parent_unbound"),
        ({"parent": parent(source_sha="e" * 40)}, "daily_parent_unbound"),
        ({"parent": parent(job_policy_digest="e" * 64)}, "daily_parent_unbound"),
        (
            {"parent": parent(job_resource=DAILY_JOB.replace("daily", "ingest"))},
            "daily_parent_unbound",
        ),
        (
            {"parent": parent(execution_name=DAILY_JOB + "/executions/other")},
            "daily_parent_unbound",
        ),
    ],
)
def test_construction_refuses_a_grant_parent_or_profile_that_do_not_bind(changes, code):
    with pytest.raises(ValueError, match=rf"^{code}$"):
        authority(**changes)


def test_the_cost_policy_must_name_this_job_policy():
    policy = dict(cost_policy(), job_policy_digest="e" * 64)
    with pytest.raises(ValueError, match=r"^daily_cost_policy_job_mismatch$"):
        authority(policy=policy)


def test_the_kernel_holds_at_collection_while_collection_has_no_registry_row():
    objects = FakeObjectClient()
    store = daily_store.DailyStore(objects, owner=PARENT, now=lambda: NOW, lease_seconds=600)
    subject_authority, derive, _store, _objects = authority(objects=objects, store=store)
    called = []
    stages = {
        stage: (lambda **kwargs: called.append(kwargs) or {"state": "pending"})
        for stage in daily_cycle.STAGE_ORDER_V2
    }
    result = daily_cycle.execute_daily_cycle(
        operation_id=frame("collection")["operation_id"],
        cutoff_utc=CUTOFF,
        profile=profile(),
        authority=subject_authority,
        store=store,
        stages=stages,
    )
    assert result == {
        "state": "authority_missing",
        "operation_id": frame("collection")["operation_id"],
        "stage": "collection",
    }
    assert derive.calls == []
    assert called == []
    control = json.loads(
        objects.objects[f"42/daily/slots/{result['operation_id']}/control.json"][0]
    )
    assert control["dispatch_intent"] is None


def test_the_kernel_publishes_intent_derives_and_dispatches_a_bound_stage():
    objects = FakeObjectClient()
    store = daily_store.DailyStore(objects, owner=PARENT, now=lambda: NOW, lease_seconds=600)
    real = daily_operation_map.child_binding

    def binding_for(operation, *, registry):
        if operation == "daily_source_collection":
            return release_binding_as(operation)
        return real(operation, registry=registry)

    policy = cost_policy(
        bindings={"daily_source_collection": release_binding_as("daily_source_collection")}
    )
    subject_authority, derive, _store, _objects = authority(
        objects=objects,
        store=store,
        producers={"daily_source_collection": produce},
        policy=policy,
        binding_for=binding_for,
    )
    seen = []

    def dispatch(*, manifest, authority_receipt):
        control = json.loads(
            objects.objects[f"42/daily/slots/{manifest['operation_id']}/control.json"][0]
        )
        seen.append((control["dispatch_intent"]["phase"], authority_receipt["derivation_id"]))
        return {"state": "pending"}

    stages = dict.fromkeys(daily_cycle.STAGE_ORDER_V2, dispatch)
    result = daily_cycle.execute_daily_cycle(
        operation_id=frame("collection")["operation_id"],
        cutoff_utc=CUTOFF,
        profile=profile(),
        authority=subject_authority,
        store=store,
        stages=stages,
    )
    assert result["state"] == "pending"
    assert result["stage"] == "collection"
    assert len(derive.calls) == 1
    assert seen == [("dispatch_started", json.loads(json.dumps(seen[0][1])))]
    assert json.loads(derive.calls[0][2])["predecessor_result_id"] is None


def test_the_artifact_set_encodes_each_artifact_as_canonical_base64():
    subject_authority, _derive, _store, _objects = authority()
    prepared = subject_authority.prepare("release", frame("release"), lease=LEASE)
    envelope = json.loads(prepared["canonical_operation_artifact_set_json"])
    assert [item["name"] for item in envelope["artifacts"]] == sorted(
        item["name"] for item in envelope["artifacts"]
    )
    for item in envelope["artifacts"]:
        assert base64.b64encode(base64.b64decode(item["data"])).decode() == item["data"]


# the authority recomputes the cost policy from its own bindings and the stored rates
# (review of dd9fc49, must fix 2)


def test_the_authority_reads_the_rates_object_itself():
    objects = RatesObjects()
    authority(rates_objects=objects)
    assert objects.calls == [(RATES_OBJECT, MAX_RATES_BYTES)]


def test_a_hand_zeroed_policy_refuses():
    """The reviewer's probe: reservations zeroed by hand shipped unchanged."""
    policy = cost_policy()
    policy["operation_reservations_micro_usd"] = dict.fromkeys(
        policy["operation_reservations_micro_usd"], 0
    )
    policy["administrative_reservation_micro_usd"] = 0
    policy["assumptions"] = ["hand written"]
    with pytest.raises(ValueError, match=r"^daily_cost_policy_mismatch$"):
        authority(policy=policy)


@pytest.mark.parametrize(
    "policy",
    [
        lambda: cost_policy(record=rates(query_micro_usd_per_tib=1)),
        lambda: cost_policy(administrative_bytes_billed=1),
        lambda: dict(cost_policy(), assumptions=["other"]),
        lambda: dict(cost_policy(), pricing_sources=["https://example.com/"]),
    ],
)
def test_a_policy_other_than_the_recomputed_one_refuses(policy):
    with pytest.raises(ValueError, match=r"^daily_cost_policy_mismatch$"):
        authority(policy=policy())


def test_a_policy_pricing_an_operation_the_authority_cannot_bind_refuses():
    policy = cost_policy(
        bindings={"daily_source_collection": release_binding_as("daily_source_collection")}
    )
    with pytest.raises(ValueError, match=r"^daily_cost_policy_mismatch$"):
        authority(policy=policy)


@pytest.mark.parametrize(
    ("storage", "code"),
    [
        ({"content_sha256": "0" * 64}, "daily_cost_rates_invalid"),
        ({"object_name": "42/daily/workload-rates/other.json"}, "daily_cost_rates_invalid"),
        ({"size_bytes": 1}, "daily_cost_rates_invalid"),
        ({"created_at": "2026-09-23T02:10:00"}, "daily_cost_rates_invalid"),
        ({"created_at": (NOW - timedelta(hours=2)).isoformat()}, "daily_cost_rates_unwitnessed"),
        ({"created_at": (NOW + timedelta(minutes=1)).isoformat()}, "daily_cost_rates_unwitnessed"),
    ],
)
def test_a_rates_object_its_storage_facts_do_not_bind_refuses(storage, code):
    with pytest.raises(ValueError, match=rf"^{code}$"):
        authority(rates_objects=RatesObjects(**storage))


def test_a_rates_object_that_is_not_canonical_json_refuses():
    objects = RatesObjects()
    objects.raw = b" " + objects.raw
    objects.storage.update(
        content_sha256=sha256(objects.raw).hexdigest(), size_bytes=len(objects.raw)
    )
    with pytest.raises(ValueError, match=r"^daily_cost_rates_invalid$"):
        authority(rates_objects=objects)


def test_a_zero_rate_refuses_at_construction():
    with pytest.raises(ValueError, match=r"^daily_cost_rates_invalid$"):
        authority(rates_objects=RatesObjects(rates(model_call_micro_usd=0)))


# the parent's image must carry the digest its build binding names (surviving mutant)


def test_a_parent_image_whose_digest_is_not_the_build_bindings_refuses():
    other = IMAGE.replace(IMAGE_DIGEST, "0" * 64)
    value = grant(
        build_provenance_bindings=[dict(grant()["build_provenance_bindings"][0], image_uri=other)]
    )
    with pytest.raises(ValueError, match=r"^daily_parent_unbound$"):
        authority(grant_row=grant_row(value), parent=parent(image_uri=other))
