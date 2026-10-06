import importlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from tests.unit import test_general_question_store as f


def setup():
    module = importlib.import_module("src.analysis.open_intelligence.general_question_queries")
    store, bucket = f.store()
    f.activate_fixture(bucket)
    _, request, intake = f.prepared()
    store.admit(request, intake, scope=f.scope(), now=f.NOW)
    return module, module.GeneralQuestionQueries(store), bucket, request["request_id"]


def reserve(queries, rid, ordinal=1, **overrides):
    values = {
        "scope": f.scope(),
        "ordinal": ordinal,
        "template_id": "released_candidates_v1",
        "sql_digest": "a" * 64,
        "parameters_digest": "b" * 64,
        "maximum_bytes_billed": 100000000,
        "candidate_limit": 100,
        "now": f.NOW,
    }
    values.update(overrides)
    return queries.reserve_query(rid, **values)


def receipt(reservation, **overrides):
    value = {
        key: reservation[key]
        for key in ("job_id", "template_id", "sql_digest", "parameters_digest")
    }
    value.update(
        query_state="succeeded", billed_bytes=10, observed_candidate_count=2, result_digest="c" * 64
    )
    value.update(overrides)
    return value


def test_lost_ack_and_expired_retry_reuse_exact_job_and_binding():
    module, queries, bucket, rid = setup()
    bucket.lose_ack.add(f.PREFIX + f.LEDGER)
    first = reserve(queries, rid)
    uploads = len(bucket.uploads)
    again = reserve(queries, rid, now=f.NOW + timedelta(days=2))
    assert first == again
    assert len(bucket.uploads) == uploads
    assert first["run_id"] == "question_" + rid.replace("-", "")
    with pytest.raises(module.QuestionStoreError, match="query_conflict"):
        reserve(queries, rid, parameters_digest="d" * 64)
    assert queries.read_query(rid, scope=f.scope(), ordinal=1)["reservation"] == first


@pytest.mark.parametrize("cap_version", [1, 2])
def test_context_cap_version_is_reserved_and_bound_to_the_job(cap_version):
    module, queries, _bucket, rid = setup()
    kwargs = {"template_id": "protected_context_candidates_v1", "cap_version": cap_version}
    first = reserve(queries, rid, **kwargs)
    assert first["contract_version"] == "general_question_query_reservation_v2"
    assert first["cap_version"] == cap_version
    assert queries.read_query(rid, scope=f.scope(), ordinal=1)["reservation"] == first
    assert reserve(queries, rid, **kwargs) == first
    with pytest.raises(module.QuestionStoreError, match="query_conflict"):
        reserve(queries, rid, **{**kwargs, "cap_version": 3 - cap_version})
    from src.analysis.open_intelligence.general_question_control import query_binding_digest

    assert query_binding_digest({**first, "cap_version": 3 - cap_version}) != first["query_digest"]


@pytest.mark.parametrize("version", ["missing", None, True, 0, -1])
def test_persisted_control_read_preserves_unknown_cap_version_refusal(version):
    module, queries, bucket, rid = setup()
    reserve(queries, rid, template_id="protected_context_candidates_v1", cap_version=2)
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    reservation = control["requests"][rid]["execution"]["queries"]["1"]
    if version == "missing":
        reservation.pop("cap_version")
    else:
        reservation["cap_version"] = version
    bucket.seed(f.LEDGER, control)
    with pytest.raises(module.QuestionStoreError, match="snapshot_cap_version_unknown"):
        queries.store._control()


@pytest.mark.parametrize(
    "template,ordinal", [("released_review_v1", 3), ("current_source_copy_v1", 8)]
)
def test_metadata_query_uses_existing_shared_reservation_and_recovery(template, ordinal):
    _, queries, bucket, rid = setup()
    first = reserve(queries, rid, ordinal=ordinal, template_id=template, candidate_limit=0)
    uploads = len(bucket.uploads)
    assert reserve(queries, rid, ordinal=ordinal, template_id=template, candidate_limit=0) == first
    assert len(bucket.uploads) == uploads
    recorded = receipt(first, observed_candidate_count=0)
    assert (
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=ordinal, receipt=recorded)
        == recorded
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("ordinal", True),
        ("ordinal", 0),
        ("ordinal", 9),
        ("maximum_bytes_billed", True),
        ("candidate_limit", True),
        ("candidate_limit", -1),
        ("maximum_bytes_billed", -1),
        ("cap_version", 0),
        ("template_id", "arbitrary_sql"),
        ("sql_digest", "not-a-digest"),
    ],
)
def test_invalid_reservation_refuses_without_write(field, value):
    module, queries, bucket, rid = setup()
    uploads = len(bucket.uploads)
    with pytest.raises(module.QuestionStoreError):
        reserve(queries, rid, **{field: value})
    assert len(bucket.uploads) == uploads


def test_zero_cap_version_refuses_as_query_invalid_before_write():
    module, queries, bucket, rid = setup()
    uploads = len(bucket.uploads)
    with pytest.raises(module.QuestionStoreError, match=r"^query_invalid$"):
        reserve(queries, rid, template_id="protected_context_candidates_v1", cap_version=0)
    assert len(bucket.uploads) == uploads


@pytest.mark.parametrize("limit", ["bytes", "candidates", "jobs"])
def test_cumulative_reservations_remain_held(limit):
    module, queries, _, rid = setup()
    if limit == "jobs":
        for ordinal in range(1, 9):
            reserve(queries, rid, ordinal)
        with pytest.raises(module.QuestionStoreError):
            reserve(queries, rid, 9)
    else:
        values = (
            {"maximum_bytes_billed": 1000000000} if limit == "bytes" else {"candidate_limit": 1000}
        )
        first = reserve(queries, rid, **values)
        failed = receipt(
            first,
            query_state="failed",
            billed_bytes=None,
            observed_candidate_count=None,
            result_digest=None,
        )
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=failed)
        with pytest.raises(module.QuestionStoreError, match="query_budget_exhausted"):
            reserve(queries, rid, 2, template_id="released_evidence_v1")


def test_competing_reservations_cannot_overbook_bytes():
    module, queries, bucket, rid = setup()
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    attempts = [0]

    def race(name):
        if name == f.PREFIX + f.LEDGER:
            with guard:
                attempts[0] += 1
                wait = attempts[0] <= 2
            if wait:
                barrier.wait(timeout=3)

    bucket.before_upload = race

    def invoke(ordinal):
        try:
            return reserve(queries, rid, ordinal, maximum_bytes_billed=600000000)
        except module.QuestionStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, (1, 2)))
    assert results.count("query_budget_exhausted") == 1
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    assert len(control["requests"][rid]["execution"]["queries"]) == 1


def test_missing_query_control_and_wrong_scope_never_create_allowance():
    module, queries, bucket, rid = setup()
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    control["requests"][rid]["execution"].pop("queries")
    bucket.seed(f.LEDGER, control)
    uploads = len(bucket.uploads)
    with pytest.raises(module.QuestionStoreError, match="query_unavailable"):
        reserve(queries, rid)
    assert len(bucket.uploads) == uploads
    with pytest.raises(module.QuestionStoreError, match="scope_invalid"):
        reserve(queries, rid, scope={**f.scope(), "client_scope_id": "other"})


def test_late_committed_query_stays_reserved_without_live_grant(monkeypatch):
    module, queries, bucket, rid = setup()
    elapsed = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])

    def slow(name):
        if name == f.PREFIX + f.LEDGER:
            elapsed[0] += 5

    bucket.before_upload = slow
    with pytest.raises(module.QuestionStoreError, match="request_expired"):
        reserve(queries, rid, now=f.NOW + timedelta(seconds=239))
    existing = queries.read_query(rid, scope=f.scope(), ordinal=1)["reservation"]
    assert reserve(queries, rid, now=f.NOW + timedelta(days=2)) == existing


def test_receipts_preserve_unknown_and_failure_without_terminal_flip():
    module, queries, _, rid = setup()
    reservation = reserve(queries, rid)
    running = receipt(
        reservation,
        query_state="running",
        billed_bytes=None,
        observed_candidate_count=None,
        result_digest=None,
    )
    assert queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=running) == running
    failed = {**running, "query_state": "failed"}
    assert (
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=failed)[
            "billed_bytes"
        ]
        is None
    )
    measured = {**failed, "billed_bytes": 0, "observed_candidate_count": 0}
    assert (
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=measured) == measured
    )
    with pytest.raises(module.QuestionStoreError, match="query_receipt_conflict"):
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=receipt(reservation))


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "different"),
        ("sql_digest", "d" * 64),
        ("parameters_digest", "d" * 64),
        ("billed_bytes", None),
        ("billed_bytes", True),
        ("billed_bytes", 100000001),
        ("observed_candidate_count", None),
        ("observed_candidate_count", 101),
        ("result_digest", None),
        ("query_state", "unknown"),
    ],
)
def test_invalid_query_receipt_refuses(field, value):
    module, queries, bucket, rid = setup()
    reservation = reserve(queries, rid)
    uploads = len(bucket.uploads)
    with pytest.raises(module.QuestionStoreError):
        queries.record_query_receipt(
            rid, scope=f.scope(), ordinal=1, receipt=receipt(reservation, **{field: value})
        )
    assert len(bucket.uploads) == uploads


@pytest.mark.parametrize("changed", [False, True])
def test_same_ordinal_race_has_one_stable_job_identity(changed):
    module, queries, bucket, rid = setup()
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    count = [0]

    def race(name):
        if name == f.PREFIX + f.LEDGER:
            with lock:
                count[0] += 1
                wait = count[0] <= 2
            if wait:
                barrier.wait(timeout=3)

    bucket.before_upload = race

    def invoke(index):
        try:
            return reserve(queries, rid, parameters_digest=("d" if index and changed else "b") * 64)
        except module.QuestionStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, (0, 1)))
    if changed:
        assert results.count("query_conflict") == 1
    else:
        assert results[0] == results[1]
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    assert len(control["requests"][rid]["execution"]["queries"]) == 1


def test_expiry_during_reads_prevents_any_new_reservation(monkeypatch):
    module, queries, bucket, rid = setup()
    elapsed = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: elapsed[0])
    original = bucket.get_blob

    def delayed(*args, **kwargs):
        elapsed[0] += 5
        return original(*args, **kwargs)

    monkeypatch.setattr(bucket, "get_blob", delayed)
    uploads = len(bucket.uploads)
    with pytest.raises(module.QuestionStoreError, match="request_expired"):
        reserve(queries, rid, now=f.NOW + timedelta(seconds=239))
    assert len(bucket.uploads) == uploads


def test_receipt_does_not_change_immutable_reservation_or_allow_known_value_rewrite():
    module, queries, bucket, rid = setup()
    original = reserve(queries, rid)
    observed = receipt(original)
    bucket.lose_ack.add(f.PREFIX + f.LEDGER)
    assert (
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=observed) == observed
    )
    uploads = len(bucket.uploads)
    assert reserve(queries, rid, now=f.NOW + timedelta(days=2)) == original
    assert (
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=observed) == observed
    )
    assert len(bucket.uploads) == uploads
    with pytest.raises(module.QuestionStoreError, match="query_receipt_conflict"):
        queries.record_query_receipt(
            rid, scope=f.scope(), ordinal=1, receipt={**observed, "billed_bytes": 11}
        )
    with pytest.raises(TypeError):
        original["job_id"] = "changed"


@pytest.mark.parametrize("overage", ["bytes", "candidates", "both"])
def test_observed_failed_query_overage_is_retained_and_blocks_only_new_reservations(overage):
    module, queries, bucket, rid = setup()
    first = reserve(queries, rid, maximum_bytes_billed=100, candidate_limit=1)
    unknown = receipt(
        first,
        query_state="failed",
        billed_bytes=None,
        observed_candidate_count=None,
        result_digest=None,
    )
    queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=unknown)
    second = reserve(queries, rid, 2, maximum_bytes_billed=100, candidate_limit=1)
    observed = {
        **unknown,
        "billed_bytes": 101 if overage in ("bytes", "both") else None,
        "observed_candidate_count": 2 if overage in ("candidates", "both") else None,
    }
    assert (
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=observed) == observed
    )
    assert queries.read_query(rid, scope=f.scope(), ordinal=1)["receipt"] == observed
    uploads = len(bucket.uploads)
    with pytest.raises(module.QuestionStoreError, match="query_budget_exhausted"):
        reserve(queries, rid, 3, maximum_bytes_billed=0, candidate_limit=0)
    assert (
        reserve(
            queries, rid, maximum_bytes_billed=100, candidate_limit=1, now=f.NOW + timedelta(days=2)
        )
        == first
    )
    assert reserve(queries, rid, 2, maximum_bytes_billed=100, candidate_limit=1) == second
    assert (
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=observed) == observed
    )
    assert len(bucket.uploads) == uploads
    with pytest.raises(module.QuestionStoreError, match="query_receipt_conflict"):
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=unknown)
    control = json.loads(bucket.objects[f.PREFIX + f.LEDGER][1])
    reservations = control["requests"][rid]["execution"]["queries"]
    assert sum(row["maximum_bytes_billed"] for row in reservations.values()) == 200
    assert sum(row["candidate_limit"] for row in reservations.values()) == 2


@pytest.mark.parametrize("field", ["billed_bytes", "observed_candidate_count"])
@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf")])
def test_failed_query_measurements_must_be_nonnegative_exact_integers(field, value):
    module, queries, bucket, rid = setup()
    reservation = reserve(queries, rid)
    observed = receipt(
        reservation,
        query_state="failed",
        billed_bytes=None,
        observed_candidate_count=None,
        result_digest=None,
    )
    observed[field] = value
    uploads = len(bucket.uploads)
    with pytest.raises(module.QuestionStoreError, match="query_receipt_invalid"):
        queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=observed)
    assert len(bucket.uploads) == uploads


def test_overage_observed_after_new_cas_withholds_return_but_keeps_reservation(monkeypatch):
    module, queries, _, rid = setup()
    first = reserve(queries, rid, maximum_bytes_billed=100, candidate_limit=1)
    overage = receipt(
        first,
        query_state="failed",
        billed_bytes=101,
        observed_candidate_count=2,
        result_digest=None,
    )
    original = queries.store._objects.compare_control
    recorded = [False]

    def commit_then_observe(generation, control):
        result = original(generation, control)
        if result and not recorded[0] and "2" in control["requests"][rid]["execution"]["queries"]:
            recorded[0] = True
            queries.record_query_receipt(rid, scope=f.scope(), ordinal=1, receipt=overage)
        return result

    monkeypatch.setattr(queries.store._objects, "compare_control", commit_then_observe)
    with pytest.raises(module.QuestionStoreError, match="query_budget_exhausted"):
        reserve(queries, rid, 2)
    held = queries.read_query(rid, scope=f.scope(), ordinal=2)["reservation"]
    assert reserve(queries, rid, 2) == held
