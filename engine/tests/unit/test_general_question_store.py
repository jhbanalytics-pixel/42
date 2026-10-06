import copy
import hashlib
import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.general_question_policy import (
    build_intake_context,
)
from src.analysis.open_intelligence.general_question_policy import (
    build_question_policy as _build_question_policy,
)
from src.analysis.open_intelligence.general_question_request import normalize_question_request

NOW = datetime(2026, 9, 6, 20, 0, tzinfo=UTC)
DEPLOYMENT = "d" * 64
PREFIX = "open-intelligence/v2/staging/general-questions/"
LEDGER = "allowances/general_cultural_question_staging_eval_v1/ledger.json"


def build_question_policy(**kwargs):
    return _build_question_policy(
        approval_contract_digest="519a658ad91b3b4d447298611d259421bbac5b7765d7c0f0049dc8bdd08c5857",
        **kwargs,
    )


def module():
    return importlib.import_module("src.analysis.open_intelligence.general_question_store")


def scope():
    return {
        "client_scope_id": "ogilvy_default",
        "market_scope": ["ke", "ng", "za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
    }


class Bucket:
    name = "listening-post-staging-cache"

    def __init__(self):
        self.objects = {}
        self.versions = {}
        self.counter = 0
        self.lock = threading.Lock()
        self.uploads = []
        self.downloads = []
        self.lose_ack = set()
        self.before_upload = None
        self.fail_reads = False

    def seed(self, key, value):
        with self.lock:
            self.counter += 1
            raw = canonical_bytes(value)
            self.objects[PREFIX + key] = (self.counter, raw)
            self.versions[(PREFIX + key, self.counter)] = raw

    def get_blob(self, name, *, generation=None, **kwargs):
        assert kwargs.get("retry") is None
        if self.fail_reads:
            raise PermissionError("private read failure")
        with self.lock:
            if generation is None:
                item = self.objects.get(name)
                if item is None:
                    return None
                generation, raw = item
            else:
                raw = self.versions.get((name, generation))
                if raw is None:
                    return None
            return Blob(self, name, generation, len(raw))

    def blob(self, name):
        return Blob(self, name)


class Blob:
    def __init__(self, bucket, name, generation=None, size=None):
        self.bucket, self.name, self.generation, self.size = bucket, name, generation, size

    def download_as_bytes(self, *, if_generation_match, **kwargs):
        assert kwargs.get("retry") is None
        assert kwargs.get("raw_download") is True
        assert if_generation_match == self.generation
        with self.bucket.lock:
            self.bucket.downloads.append(self.name)
            raw = self.bucket.versions.get((self.name, self.generation))
            if raw is None:
                raise NotFound("gone")
            return raw

    def upload_from_string(self, data, *, if_generation_match, content_type, **kwargs):
        assert kwargs.get("retry") is None
        assert content_type == "application/json"
        if self.bucket.before_upload:
            self.bucket.before_upload(self.name)
        with self.bucket.lock:
            self.bucket.uploads.append((self.name, if_generation_match))
            current = self.bucket.objects.get(self.name)
            actual = current[0] if current else 0
            if actual != if_generation_match:
                raise PreconditionFailed("generation changed")
            self.bucket.counter += 1
            self.generation = self.bucket.counter
            self.size = len(data)
            self.bucket.objects[self.name] = (self.generation, bytes(data))
            self.bucket.versions[(self.name, self.generation)] = bytes(data)
            if self.name in self.bucket.lose_ack:
                self.bucket.lose_ack.remove(self.name)
                raise TimeoutError("response lost after commit")


def prepared(index=1):
    policy = build_question_policy(pricing_verified_at=NOW)
    request = normalize_question_request(
        {"message": f"Explain mobility question {index}"},
        scope=scope(),
        request_id=str(UUID(int=index)),
        admitted_at=NOW,
        policy_digest=policy["policy_digest"],
    )
    return policy, request, build_intake_context(request, selected_market="za")


def store(bucket=None):
    policy, _, _ = prepared()
    bucket = Bucket() if bucket is None else bucket
    return module().GeneralQuestionStore(
        bucket, policy=policy, deployment_digest=DEPLOYMENT
    ), bucket


def activate_fixture(bucket):
    policy, _, _ = prepared()
    bucket.seed(f"policies/{policy['policy_digest']}/policy.json", policy)
    bucket.seed(
        LEDGER, module().build_initial_question_control(policy, deployment_digest=DEPLOYMENT)
    )


def reseal_policy(policy, *, deadline_seconds):
    policy = copy.deepcopy(policy)
    policy["limits"]["deadline_seconds"] = deadline_seconds
    policy["policy_digest"] = hashlib.sha256(
        canonical_bytes({key: value for key, value in policy.items() if key != "policy_digest"})
    ).hexdigest()
    return policy


def test_missing_allowance_never_bootstraps_or_writes_inputs():
    service, bucket = store()
    _, request, intake = prepared()
    with pytest.raises(module().QuestionStoreError, match="control_unavailable"):
        service.admit(request, intake, scope=scope(), now=NOW)
    assert bucket.objects == {}
    assert bucket.uploads == []


def test_admission_is_durable_and_same_retry_keeps_one_reservation():
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    first = service.admit(request, intake, scope=scope(), now=NOW)
    retried = service.admit(
        copy.deepcopy(request), copy.deepcopy(intake), scope=scope(), now=NOW + timedelta(seconds=1)
    )
    assert first == retried
    value = json.loads(bucket.objects[PREFIX + LEDGER][1])
    assert len(value["requests"]) == 1
    assert value["reserved_microusd"] == 100000
    assert value["requests"][request["request_id"]]["deadline_at"] == "2026-09-06T20:04:00.000000Z"
    loaded = service.read_request(request["request_id"], scope=scope())
    assert loaded["request"] == request
    assert loaded["intake"] == intake
    loaded["request"]["question"] = "mutated caller copy"
    assert service.read_request(request["request_id"], scope=scope())["request"] == request


def test_changed_selection_or_question_cannot_reuse_request_identity():
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    service.admit(request, intake, scope=scope(), now=NOW)
    changed = build_intake_context(request, selected_market="ng")
    with pytest.raises(module().QuestionStoreError, match="run_id_conflict"):
        service.admit(request, changed, scope=scope(), now=NOW)
    assert json.loads(bucket.objects[PREFIX + LEDGER][1])["reserved_microusd"] == 100000


def test_shared_allowance_caps_distinct_requests_at_one_hundred():
    service, bucket = store()
    activate_fixture(bucket)
    for index in range(1, 101):
        _, request, intake = prepared(index)
        service.admit(request, intake, scope=scope(), now=NOW)
    _, request, intake = prepared(101)
    with pytest.raises(module().QuestionStoreError, match="budget_exhausted"):
        service.admit(request, intake, scope=scope(), now=NOW)
    control = json.loads(bucket.objects[PREFIX + LEDGER][1])
    assert len(control["requests"]) == 100
    assert control["reserved_microusd"] == 10000000


def test_two_racing_admissions_preserve_both_reservations():
    service, bucket = store()
    activate_fixture(bucket)
    barrier = threading.Barrier(2)
    count = 0
    guard = threading.Lock()

    def race(name):
        nonlocal count
        if name != PREFIX + LEDGER:
            return
        with guard:
            count += 1
            should_wait = count <= 2
        if should_wait:
            barrier.wait(timeout=5)

    def admit(index):
        _, request, intake = prepared(index)
        return service.admit(request, intake, scope=scope(), now=NOW)

    bucket.before_upload = race
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(admit, (1, 2)))
    assert len(result) == 2
    control = json.loads(bucket.objects[PREFIX + LEDGER][1])
    assert len(control["requests"]) == 2
    assert control["reserved_microusd"] == 200000


@pytest.mark.parametrize("target", ["request", "intake", "ledger"])
def test_lost_write_acknowledgement_reuses_committed_bytes(target):
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    key = LEDGER if target == "ledger" else f"requests/{request['request_id']}/{target}.json"
    bucket.lose_ack.add(PREFIX + key)
    service.admit(request, intake, scope=scope(), now=NOW)
    assert json.loads(bucket.objects[PREFIX + LEDGER][1])["reserved_microusd"] == 100000
    assert sum(name == PREFIX + key for name, _ in bucket.uploads) == 1


def test_control_deleted_after_admission_is_unavailable_not_a_new_allowance():
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    service.admit(request, intake, scope=scope(), now=NOW)
    del bucket.objects[PREFIX + LEDGER]
    uploads = len(bucket.uploads)
    _, next_request, next_intake = prepared(2)
    with pytest.raises(module().QuestionStoreError, match="control_unavailable"):
        service.admit(next_request, next_intake, scope=scope(), now=NOW)
    assert len(bucket.uploads) == uploads


def test_foreign_scope_cannot_read_stored_request():
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    service.admit(request, intake, scope=scope(), now=NOW)
    with pytest.raises(module().QuestionStoreError, match="scope_invalid"):
        service.read_request(request["request_id"], scope={**scope(), "client_scope_id": "other"})


def test_invalid_reserved_total_or_boolean_count_refuses_before_mutation():
    for bad in (True, 1, 10000001):
        service, bucket = store()
        policy, request, intake = prepared()
        value = module().build_initial_question_control(policy, deployment_digest=DEPLOYMENT)
        value["reserved_microusd"] = bad
        bucket.seed(LEDGER, value)
        with pytest.raises(module().QuestionStoreError, match="control_invalid"):
            service.admit(request, intake, scope=scope(), now=NOW)
        assert bucket.uploads == []


def test_expired_new_request_refuses_but_existing_request_can_be_read():
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    service.admit(request, intake, scope=scope(), now=NOW)
    assert service.admit(request, intake, scope=scope(), now=NOW + timedelta(days=2))
    _, new_request, new_intake = prepared(2)
    with pytest.raises(module().QuestionStoreError):
        service.admit(new_request, new_intake, scope=scope(), now=NOW + timedelta(days=2))
    assert json.loads(bucket.objects[PREFIX + LEDGER][1])["reserved_microusd"] == 100000


def test_wrong_bucket_or_unapproved_deployment_cannot_admit():
    bucket = Bucket()
    bucket.name = "other"
    with pytest.raises(module().QuestionStoreError, match="storage_target_invalid"):
        store(bucket)
    service, bucket = store()
    policy, request, intake = prepared()
    bucket.seed(f"policies/{policy['policy_digest']}/policy.json", policy)
    bucket.seed(LEDGER, module().build_initial_question_control(policy, deployment_digest="e" * 64))
    with pytest.raises(module().QuestionStoreError, match="approval_required"):
        service.admit(request, intake, scope=scope(), now=NOW)
    assert bucket.uploads == []


def test_read_failure_is_unavailable_without_empty_state_or_raw_detail():
    service, bucket = store()
    bucket.fail_reads = True
    _, request, intake = prepared()
    with pytest.raises(module().QuestionStoreError) as caught:
        service.admit(request, intake, scope=scope(), now=NOW)
    assert "private" not in str(caught.value)
    assert bucket.uploads == []


def test_policy_refresh_and_redeployment_share_existing_reservations():
    service, bucket = store()
    activate_fixture(bucket)
    _, first, first_intake = prepared()
    service.admit(first, first_intake, scope=scope(), now=NOW)
    refreshed = build_question_policy(pricing_verified_at=NOW + timedelta(hours=12))
    bucket.seed(f"policies/{refreshed['policy_digest']}/policy.json", refreshed)
    control = json.loads(bucket.objects[PREFIX + LEDGER][1])
    control["bindings"]["e" * 64] = refreshed["policy_digest"]
    control["active_deployment_digest"] = "e" * 64
    bucket.seed(LEDGER, control)
    replacement = module().GeneralQuestionStore(
        bucket, policy=refreshed, deployment_digest="e" * 64
    )
    second = normalize_question_request(
        {"message": "A different question"},
        scope=scope(),
        request_id=str(UUID(int=2)),
        admitted_at=NOW + timedelta(hours=12),
        policy_digest=refreshed["policy_digest"],
    )
    replacement.admit(
        second,
        build_intake_context(second, selected_market=None),
        scope=scope(),
        now=NOW + timedelta(hours=12),
    )
    control = json.loads(bucket.objects[PREFIX + LEDGER][1])
    assert control["reserved_microusd"] == 200000
    assert len(control["requests"]) == 2
    assert replacement.read_request(first["request_id"], scope=scope())["request"] == first
    assert [key for key in bucket.objects if key.endswith("ledger.json")] == [PREFIX + LEDGER]


def test_historical_deadline_policies_are_read_once_and_cached_as_safe_copies():
    current, _, _ = prepared()
    historical = reseal_policy(current, deadline_seconds=180)
    request = normalize_question_request(
        {"message": "Historical mobility question"},
        scope=scope(),
        request_id=str(UUID(int=90)),
        admitted_at=NOW,
        policy_digest=historical["policy_digest"],
    )
    intake = build_intake_context(request, selected_market="za")
    bucket = Bucket()
    bucket.seed(f"policies/{current['policy_digest']}/policy.json", current)
    bucket.seed(f"policies/{historical['policy_digest']}/policy.json", historical)
    request_key = f"requests/{request['request_id']}/request.json"
    intake_key = f"requests/{request['request_id']}/intake.json"
    bucket.seed(request_key, request)
    bucket.seed(intake_key, intake)
    control = module().build_initial_question_control(current, deployment_digest=DEPLOYMENT)
    historical_deployment = "e" * 64
    control["bindings"][historical_deployment] = historical["policy_digest"]
    control["requests"][request["request_id"]] = {
        "request_digest": request["request_digest"],
        "intake_digest": intake["intake_digest"],
        "request_generation": str(bucket.objects[PREFIX + request_key][0]),
        "intake_generation": str(bucket.objects[PREFIX + intake_key][0]),
        "client_scope_id": request["client_scope_id"],
        "policy_digest": historical["policy_digest"],
        "deployment_digest": historical_deployment,
        "admitted_at": request["as_of"],
        "deadline_at": "2026-09-06T20:03:00.000000Z",
        "reserved_microusd": 100000,
    }
    control["reserved_microusd"] = 100000
    bucket.seed(LEDGER, control)
    service = module().GeneralQuestionStore(bucket, policy=current, deployment_digest=DEPLOYMENT)
    bucket.downloads.clear()

    assert (
        service.read_request(request["request_id"], scope=scope())["admission"]["deadline_at"]
        == "2026-09-06T20:03:00.000000Z"
    )
    service.read_request(request["request_id"], scope=scope())
    cached = service._stored_policy(historical["policy_digest"])
    cached["limits"]["deadline_seconds"] = 240

    policy_downloads = [name for name in bucket.downloads if "/policies/" in name]
    assert sorted(policy_downloads) == sorted(
        [
            PREFIX + f"policies/{current['policy_digest']}/policy.json",
            PREFIX + f"policies/{historical['policy_digest']}/policy.json",
        ]
    )
    assert service._stored_policy(historical["policy_digest"])["limits"]["deadline_seconds"] == 180


@pytest.mark.parametrize(
    "failure",
    [
        "unknown_policy",
        "altered_body",
        "digest_mismatch",
        "unsupported_deadline",
        "crossed_binding",
    ],
)
def test_policy_bound_control_refuses_untrusted_deadline_authority(failure):
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    service.admit(request, intake, scope=scope(), now=NOW)
    control = json.loads(bucket.objects[PREFIX + LEDGER][1])
    policy_key = f"policies/{service.policy['policy_digest']}/policy.json"
    if failure == "unknown_policy":
        del bucket.objects[PREFIX + policy_key]
    elif failure == "altered_body":
        policy = copy.deepcopy(service.policy)
        policy["limits"]["deadline_seconds"] = 180
        bucket.seed(policy_key, policy)
    elif failure == "digest_mismatch":
        policy = copy.deepcopy(service.policy)
        policy["policy_digest"] = "f" * 64
        bucket.seed(policy_key, policy)
    elif failure == "unsupported_deadline":
        policy = reseal_policy(service.policy, deadline_seconds=300)
        bucket.seed(f"policies/{policy['policy_digest']}/policy.json", policy)
        row = control["requests"][request["request_id"]]
        row["policy_digest"] = policy["policy_digest"]
        control["bindings"][row["deployment_digest"]] = policy["policy_digest"]
        bucket.seed(LEDGER, control)
    else:
        control["requests"][request["request_id"]]["policy_digest"] = "f" * 64
        bucket.seed(LEDGER, control)

    service = module().GeneralQuestionStore(
        bucket, policy=service.policy, deployment_digest=service.deployment_digest
    )
    with pytest.raises(module().QuestionStoreError, match="control_invalid"):
        service.read_request(request["request_id"], scope=scope())
    if failure in {"unknown_policy", "altered_body", "digest_mismatch"}:
        bucket.seed(policy_key, service.policy)
        assert service.read_request(request["request_id"], scope=scope())["request"] == request


def test_validated_policy_cache_is_bounded_and_eviction_rereads_storage():
    service, bucket = store()
    policies = [
        build_question_policy(pricing_verified_at=NOW + timedelta(microseconds=index))
        for index in range(129)
    ]
    for policy in policies:
        bucket.seed(f"policies/{policy['policy_digest']}/policy.json", policy)
        service._stored_policy(policy["policy_digest"])
    first_name = PREFIX + f"policies/{policies[0]['policy_digest']}/policy.json"

    assert len(service._policy_cache) == 128
    assert bucket.downloads.count(first_name) == 1
    service._stored_policy(policies[0]["policy_digest"])
    assert bucket.downloads.count(first_name) == 2


def test_content_addressed_results_require_the_exact_byte_digest():
    _, bucket = store()
    objects = module()._QuestionObjects(bucket)
    request_id = str(UUID(int=1))
    with pytest.raises(module().QuestionStoreError, match="object_digest_mismatch"):
        objects.create(f"requests/{request_id}/results/{'a' * 64}.json", {"value": 1})
    assert bucket.uploads == []
    value = {"value": 1}
    digest = hashlib.sha256(canonical_bytes(value)).hexdigest()
    record = objects.create(f"requests/{request_id}/results/{digest}.json", value)
    assert record.value == value
    bucket.seed(f"requests/{request_id}/results/{digest}.json", {"value": 2})
    with pytest.raises(module().QuestionStoreError, match="object_digest_mismatch"):
        objects.read(f"requests/{request_id}/results/{digest}.json")


def test_control_schema_rejects_unhashable_active_binding_with_typed_error():
    service, bucket = store()
    policy, request, intake = prepared()
    control = module().build_initial_question_control(policy, deployment_digest=DEPLOYMENT)
    control["active_deployment_digest"] = []
    bucket.seed(LEDGER, control)
    with pytest.raises(module().QuestionStoreError, match="control_invalid"):
        service.admit(request, intake, scope=scope(), now=NOW)


def test_known_permission_denial_is_not_retried_as_a_cas_race():
    service, bucket = store()
    activate_fixture(bucket)
    attempts = []

    def deny(name):
        if name == PREFIX + LEDGER:
            attempts.append(name)
            raise PermissionError("private access failure")

    bucket.before_upload = deny
    _, request, intake = prepared()
    with pytest.raises(module().QuestionStoreError, match="storage_access_denied"):
        service.admit(request, intake, scope=scope(), now=NOW)
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "key",
    [
        LEDGER,
        "../other",
        "requests/invalid/request.json",
        f"requests/{UUID(int=1)}/../ledger.json",
        "policies/" + "a" * 64 + "/policy.json",
    ],
)
def test_runtime_object_writer_cannot_bootstrap_or_escape_request_namespace(key):
    _, bucket = store()
    with pytest.raises(module().QuestionStoreError):
        module()._QuestionObjects(bucket).create(key, {})
    assert bucket.uploads == []


def test_noncanonical_or_duplicate_stored_json_is_not_accepted():
    service, bucket = store()
    activate_fixture(bucket)
    generation, _ = bucket.objects[PREFIX + LEDGER]
    bad = b'{"reserved_microusd":0,"reserved_microusd":10000000}'
    bucket.versions[(PREFIX + LEDGER, generation)] = bad
    bucket.objects[PREFIX + LEDGER] = (generation, bad)
    _, request, intake = prepared()
    with pytest.raises(module().QuestionStoreError, match="object_invalid"):
        service.admit(request, intake, scope=scope(), now=NOW)
    assert bucket.uploads == []


@pytest.mark.parametrize("slow_write", ["request", "intake", "ledger"])
def test_elapsed_storage_time_cannot_return_a_new_live_admission(monkeypatch, slow_write):
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    elapsed = [0.0]
    monkeypatch.setattr(module(), "monotonic", lambda: elapsed[0], raising=False)
    suffix = (
        LEDGER if slow_write == "ledger" else f"requests/{request['request_id']}/{slow_write}.json"
    )

    def advance(name):
        if name == PREFIX + suffix:
            elapsed[0] += 5

    bucket.before_upload = advance
    with pytest.raises(module().QuestionStoreError, match="request_expired"):
        service.admit(request, intake, scope=scope(), now=NOW + timedelta(seconds=239))
    control = json.loads(bucket.objects[PREFIX + LEDGER][1])
    if slow_write == "ledger":
        assert control["reserved_microusd"] == 100000
        assert service.read_request(request["request_id"], scope=scope())
        assert service.admit(request, intake, scope=scope(), now=NOW + timedelta(days=1))
    else:
        assert control["reserved_microusd"] == 0
    assert len(bucket.uploads) == {"request": 1, "intake": 2, "ledger": 3}[slow_write]


def test_elapsed_time_rechecks_price_freshness_between_input_writes(monkeypatch):
    service, bucket = store()
    activate_fixture(bucket)
    policy, _, _ = prepared()
    start = NOW + timedelta(days=1, seconds=-1)
    request = normalize_question_request(
        {"message": "What has changed?"},
        scope=scope(),
        request_id=str(UUID(int=1)),
        admitted_at=start,
        policy_digest=policy["policy_digest"],
    )
    intake = build_intake_context(request, selected_market=None)
    elapsed = [0.0]
    monkeypatch.setattr(module(), "monotonic", lambda: elapsed[0], raising=False)

    def advance(name):
        elapsed[0] += 5

    bucket.before_upload = advance
    with pytest.raises(module().QuestionStoreError, match="request_expired"):
        service.admit(request, intake, scope=scope(), now=start)
    assert len(bucket.uploads) == 1
    assert json.loads(bucket.objects[PREFIX + LEDGER][1])["reserved_microusd"] == 0


def test_expiry_during_cas_conflict_never_retries_a_new_reservation(monkeypatch):
    service, bucket = store()
    activate_fixture(bucket)
    _, request, intake = prepared()
    elapsed = [0.0]
    attempts = []
    monkeypatch.setattr(module(), "monotonic", lambda: elapsed[0], raising=False)

    def conflict(name):
        if name == PREFIX + LEDGER:
            attempts.append(name)
            elapsed[0] += 5
            raise PreconditionFailed("racing update")

    bucket.before_upload = conflict
    with pytest.raises(module().QuestionStoreError, match="request_expired"):
        service.admit(request, intake, scope=scope(), now=NOW + timedelta(seconds=239))
    assert len(attempts) == 1
    assert json.loads(bucket.objects[PREFIX + LEDGER][1])["reserved_microusd"] == 0


def test_explicit_generation_cache_keeps_metadata_fresh_and_returns_detached_json(monkeypatch):
    bucket = Bucket()
    key = "requests/00000000-0000-4000-8000-000000000001/request.json"
    bucket.seed(key, {"nested": {"value": 1}})
    generation = bucket.counter
    objects = module()._QuestionObjects(bucket)
    downloads = []
    metadata = []
    old_download = Blob.download_as_bytes
    old_get = bucket.get_blob

    def download(self, **kwargs):
        downloads.append(self.name)
        return old_download(self, **kwargs)

    def get(*args, **kwargs):
        metadata.append(args[0])
        return old_get(*args, **kwargs)

    monkeypatch.setattr(Blob, "download_as_bytes", download)
    monkeypatch.setattr(bucket, "get_blob", get)
    first = objects.read(key, generation=generation)
    first.value["nested"]["value"] = 99
    assert objects.read(key, generation=generation).value == {"nested": {"value": 1}}
    assert len(downloads) == 1
    assert len(metadata) == 2
    objects.read(key)
    objects.read(key)
    assert len(downloads) == 3
    other = module()._QuestionObjects(bucket)
    other.read(key, generation=generation)
    assert len(downloads) == 4


@pytest.mark.parametrize("failure", ["denied", "missing", "generation", "size"])
def test_cached_body_cannot_hide_fresh_metadata_failure(monkeypatch, failure):
    bucket = Bucket()
    key = "requests/00000000-0000-4000-8000-000000000001/request.json"
    bucket.seed(key, {"ok": True})
    generation = bucket.counter
    objects = module()._QuestionObjects(bucket)
    objects.read(key, generation=generation)
    if failure == "denied":
        bucket.fail_reads = True
    elif failure == "missing":
        del bucket.versions[(PREFIX + key, generation)]
    else:
        old = bucket.get_blob

        def changed(*args, **kwargs):
            blob = old(*args, **kwargs)
            if failure == "generation":
                blob.generation += 1
            else:
                blob.size += 1
            return blob

        monkeypatch.setattr(bucket, "get_blob", changed)
    if failure == "missing":
        assert objects.read(key, generation=generation) is None
    else:
        with pytest.raises(module().QuestionStoreError):
            objects.read(key, generation=generation)


def test_generation_cache_is_bounded_and_excludes_control_and_errors(monkeypatch):
    bucket = Bucket()
    prefix = "requests/00000000-0000-4000-8000-000000000001/"
    for suffix in ("request.json", "intake.json"):
        bucket.seed(prefix + suffix, {"value": "x" * 50})
    bucket.seed(LEDGER, {"value": True})
    objects = module()._QuestionObjects(bucket)
    downloads = []
    old = Blob.download_as_bytes

    def download(self, **kwargs):
        downloads.append(self.name)
        return old(self, **kwargs)

    monkeypatch.setattr(Blob, "download_as_bytes", download)
    monkeypatch.setattr(module(), "_MAX_BYTES", 100)
    for generation, suffix in ((1, "request.json"), (2, "intake.json")):
        for _ in range(2):
            objects.read(prefix + suffix, generation=generation)
    assert objects._generation_bytes_size <= 100
    assert len(downloads) == 3
    for _ in range(2):
        objects.read(LEDGER, generation=3)
    assert len(downloads) == 5
    assert all(name != PREFIX + LEDGER for _, name, _ in objects._generation_bytes)
    bad = prefix + "plan.json"
    bucket.seed(bad, {"good": True})
    generation = bucket.counter
    bucket.versions[(PREFIX + bad, generation)] = b"invalid"
    with pytest.raises(module().QuestionStoreError):
        objects.read(bad, generation=generation)
    assert (bucket.name, PREFIX + bad, generation) not in objects._generation_bytes


def test_explicit_generation_cache_never_crosses_object_names():
    bucket = Bucket()
    prefix = "requests/00000000-0000-4000-8000-000000000001/"
    bucket.seed(prefix + "request.json", {"value": "first"})
    bucket.versions[(PREFIX + prefix + "intake.json", 1)] = canonical_bytes({"value": "second"})
    objects = module()._QuestionObjects(bucket)
    assert objects.read(prefix + "request.json", generation=1).value == {"value": "first"}
    assert objects.read(prefix + "intake.json", generation=1).value == {"value": "second"}


def test_concurrent_generation_cache_insertions_share_one_byte_bound(monkeypatch):
    bucket = Bucket()
    objects = module()._QuestionObjects(bucket)
    keys = [f"requests/00000000-0000-4000-8000-{index:012d}/request.json" for index in range(1, 9)]
    for key in keys:
        bucket.seed(key, {"value": "x" * 50})
    monkeypatch.setattr(module(), "_MAX_BYTES", 100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(
            pool.map(lambda item: objects.read(item[1], generation=item[0] + 1), enumerate(keys))
        )
    assert all(value.value == {"value": "x" * 50} for value in values)
    assert objects._generation_bytes_size == sum(map(len, objects._generation_bytes.values()))
    assert objects._generation_bytes_size <= 100


def test_generation_cache_rechecks_content_address(monkeypatch):
    bucket = Bucket()
    raw = canonical_bytes({"value": 1})
    digest = hashlib.sha256(raw).hexdigest()
    key = f"requests/00000000-0000-4000-8000-000000000001/results/{digest}.json"
    bucket.seed(key, {"value": 1})
    objects = module()._QuestionObjects(bucket)
    objects.read(key, generation=1)
    objects._generation_bytes[(bucket.name, PREFIX + key, 1)] = canonical_bytes({"value": 2})
    with pytest.raises(module().QuestionStoreError, match="object_digest_mismatch"):
        objects.read(key, generation=1)


@pytest.mark.parametrize(
    "suffix",
    ["usage/planning.json", "usage/answering.json", "calls/planning.json", "calls/answering.json"],
)
def test_accounting_and_native_response_bodies_are_never_cached(suffix):
    bucket = Bucket()
    key = "requests/00000000-0000-4000-8000-000000000001/" + suffix
    bucket.seed(key, {"value": 1})
    objects = module()._QuestionObjects(bucket)
    assert objects.read(key, generation=1).value == {"value": 1}
    bucket.versions[(PREFIX + key, 1)] = canonical_bytes({"value": 2})
    assert objects.read(key, generation=1).value == {"value": 2}
    assert not objects._generation_bytes
