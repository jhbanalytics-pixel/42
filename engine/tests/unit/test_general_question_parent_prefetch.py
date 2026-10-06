import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.analysis.open_intelligence import general_question_parent_context as parent
from src.analysis.open_intelligence.general_question_control import QuestionStoreError

from tests.unit import test_general_question_store as fixture


def prepared():
    policy, request, _ = fixture.prepared()
    bucket = fixture.Bucket()
    from src.analysis.open_intelligence.general_question_store import GeneralQuestionStore

    store = GeneralQuestionStore(bucket, policy=policy, deployment_digest="b" * 64)
    manifest = []
    for index in range(4):
        key = (
            f"requests/{request['request_id']}/"
            + ("request.json", "intake.json", "plan.json", "snapshot.json")[index]
        )
        bucket.seed(key, {"value": index})
        manifest.append((key, bucket.objects[fixture.PREFIX + key][0]))
    return store, bucket, manifest


def test_same_twenty_second_read_budget_serial_fails_prefetch_completes(monkeypatch):
    store, bucket, manifest = prepared()
    get = bucket.get_blob
    lock = threading.Lock()
    elapsed = 0
    active = peak = 0
    parallel = False

    def clock():
        with lock:
            return elapsed

    def advance_read():
        nonlocal elapsed
        with lock:
            elapsed += 7

    monkeypatch.setattr(parent, "monotonic", clock)
    wave = threading.Barrier(4, action=advance_read)

    def slow(name, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            if parallel:
                wave.wait(timeout=5)
            else:
                advance_read()
            return get(name, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(bucket, "get_blob", slow)

    def serial():
        for key, generation in manifest:
            store._objects.read(key, generation=generation)

    with (
        parent.parent_read_attempt(store, deadline=parent.monotonic() + 20),
        pytest.raises(QuestionStoreError, match="parent_context_deadline"),
    ):
        serial()
    assert clock() == 21
    parallel = True
    parallel_start = clock()
    with parent.parent_read_attempt(store, deadline=parent.monotonic() + 20) as attempt:
        attempt.prefetch(manifest)
        assert attempt.read_attempts == 4
        assert [
            store._objects.read(key, generation=generation).value for key, generation in manifest
        ] == [{"value": i} for i in range(4)]
    assert clock() - parallel_start == 7
    assert peak == 4


def test_attempt_limit_is_shared_under_contention():
    store, _, _ = prepared()
    attempt = parent.ParentReadAttempt(store._objects, time.monotonic() + 20)
    barrier = threading.Barrier(8)

    def run(_):
        barrier.wait()
        outcomes = []
        for _ in range(8):
            try:
                attempt.before_read()
                outcomes.append(True)
            except QuestionStoreError as error:
                outcomes.append(error.code)
        return outcomes

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(run, range(8)))
    flattened = [value for row in outcomes for value in row]
    assert flattened.count(True) == 40
    assert flattened.count("parent_read_budget_exhausted") == 24
    assert attempt.read_attempts == attempt.metadata_attempts == 40


def test_prefetch_deduplicates_exact_pins_but_keeps_distinct_generations():
    store, bucket, manifest = prepared()
    key, old = manifest[0]
    bucket.seed(key, {"value": "new"})
    new = bucket.objects[fixture.PREFIX + key][0]
    with parent.parent_read_attempt(store, deadline=time.monotonic() + 20) as attempt:
        attempt.prefetch([(key, old), (key, old), (key, new)])
        assert attempt.read_attempts == 2
        assert store._objects.read(key, generation=old).value == {"value": 0}
        assert store._objects.read(key, generation=new).value == {"value": "new"}
        attempt.prefetch([(key, new)])
        assert attempt.read_attempts == 2


def test_prefetch_failure_cancels_queued_reads_and_never_writes(monkeypatch):
    store, bucket, manifest = prepared()
    for number in range(4):
        key = f"requests/00000000-0000-4000-8000-{number + 100:012d}/request.json"
        bucket.seed(key, {"value": number})
        manifest.append((key, bucket.objects[fixture.PREFIX + key][0]))
    before = copy.deepcopy(bucket.objects)
    barrier = threading.Barrier(4)
    calls = []
    original = bucket.get_blob

    def fail(name, **kwargs):
        calls.append(name)
        barrier.wait(timeout=2)
        if name == fixture.PREFIX + manifest[0][0]:
            raise PermissionError("private failure")
        time.sleep(0.03)
        return original(name, **kwargs)

    monkeypatch.setattr(bucket, "get_blob", fail)
    with parent.parent_read_attempt(store, deadline=time.monotonic() + 20) as attempt:
        with pytest.raises(QuestionStoreError):
            attempt.prefetch(manifest)
        assert attempt.failed
        with pytest.raises(QuestionStoreError):
            attempt.prefetch(manifest)
    assert len(calls) == 4
    assert bucket.objects == before
    assert not bucket.uploads


@pytest.mark.parametrize("bad", ["scope", "generation", "digest"])
def test_request_scope_and_binding_validate_before_later_prefetch(monkeypatch, bad):
    store, bucket = fixture.store()
    fixture.activate_fixture(bucket)
    _, request, intake = fixture.prepared()
    store.admit(request, intake, scope=fixture.scope(), now=fixture.NOW)
    control_object, control = store._control()
    rid = request["request_id"]
    if bad == "generation":
        control["requests"][rid]["intake_generation"] = "999999"
    if bad == "digest":
        name = fixture.PREFIX + f"requests/{rid}/request.json"
        generation = int(control["requests"][rid]["request_generation"])
        altered = {**request, "question": "tampered"}
        from src.analysis.open_intelligence.brain_contract import canonical_bytes

        bucket.versions[(name, generation)] = canonical_bytes(altered)
    scope = fixture.scope()
    if bad == "scope":
        scope["brand_config_id"] = "foreign_brand"
    reads = []
    get = bucket.get_blob
    monkeypatch.setattr(
        bucket, "get_blob", lambda name, **kw: reads.append(name) or get(name, **kw)
    )
    before = copy.deepcopy(bucket.objects)
    with parent.parent_read_attempt(store, deadline=time.monotonic() + 20) as attempt:
        from dataclasses import replace

        with (
            attempt.freeze_ledger(replace(control_object, value=control)),
            pytest.raises(QuestionStoreError),
        ):
            parent._prefetch_parent_contexts(store, control, [rid], scope)
    assert not any("/results/" in name or name.endswith("parent_context.json") for name in reads)
    assert bucket.objects == before


def test_expired_attempt_dispatches_nothing():
    store, bucket, manifest = prepared()
    with (
        parent.parent_read_attempt(store, deadline=time.monotonic() - 1) as attempt,
        pytest.raises(QuestionStoreError, match="parent_context_deadline"),
    ):
        attempt.prefetch(manifest)
    assert attempt.read_attempts == 0
    assert bucket.downloads == []
