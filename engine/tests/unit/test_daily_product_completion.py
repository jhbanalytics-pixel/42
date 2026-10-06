import copy
import json

import pytest
from src.analysis.open_intelligence.daily_product_completion import (
    PRODUCT_NAMES,
    CompletionStore,
    validate_completion,
)


class Objects:
    def __init__(self):
        self.values = {}

    def read(self, name):
        return self.values.get(name)

    def write(self, name, payload, *, if_generation_match):
        assert if_generation_match == 0
        if name in self.values:
            raise ValueError("exists")
        self.values[name] = (payload, 1)
        return 1


EMPTY_DIGEST = __import__(
    "src.analysis.open_intelligence.brain_contract", fromlist=["canonical_digest"]
).canonical_digest([])


def completion():
    return {
        "schema_version": "42_daily_products_completion_v1",
        "operation_id": "daily-1",
        "business_attempt_id": "attempt-1",
        "capture_digest": "a" * 64,
        "policy_digest": "b" * 64,
        "source_sha": "c" * 40,
        "image_uri": "image@sha256:" + "d" * 64,
        "cutoff_utc": "2026-09-20T00:00:00+00:00",
        "dynamic_receipt_digest": "e" * 64,
        "max_model_calls": 100,
        "products": {
            name: {
                "state": "empty",
                "row_count": 0,
                "output_digest": "f" * 64,
                "readback_digest": "f" * 64,
            }
            for name in PRODUCT_NAMES
        }
        | {
            # Awaiting release: bound to the dynamic receipt, and read back empty before it.
            "v_desk_dynamic_signals_v2": {
                "state": "awaiting_release",
                "row_count": 0,
                "output_digest": "e" * 64,
                "readback_digest": EMPTY_DIGEST,
            }
        },
        "metering": {
            "query_count": 12,
            "total_bytes_billed": 100,
            "storage_write_count": 8,
            "storage_write_bytes": 1000,
            "rows_written": 0,
            "model_calls": 6,
            "vendor_credits": "0",
            "complete": True,
        },
    }


def test_empty_completion_requires_all_eight_executed_product_outcomes():
    record = completion()
    assert len(validate_completion(record)["products"]) == 8
    del record["products"]["seed_candidates"]
    with pytest.raises(ValueError, match="product_set_inexact"):
        validate_completion(record)


@pytest.mark.parametrize(
    "mutation", ["unknown", "partial", "missing_usage", "digest", "extra", "count"]
)
def test_completion_refuses_false_success(mutation):
    record = completion()
    outcome = record["products"]["trend_analysis"]
    if mutation in {"unknown", "partial"}:
        outcome["state"] = mutation
    elif mutation == "missing_usage":
        record["metering"]["complete"] = False
    elif mutation == "digest":
        outcome["readback_digest"] = "0" * 64
    elif mutation == "count":
        outcome["row_count"] = 1
    else:
        record["self_approved"] = True
    with pytest.raises(ValueError):
        validate_completion(record)


def test_completion_create_readback_and_conflicting_retry():
    objects = Objects()
    store = CompletionStore(objects)
    record = completion()
    assert store.record(record) == record
    assert store.record(record) == record
    changed = copy.deepcopy(record)
    changed["capture_digest"] = "0" * 64
    with pytest.raises(ValueError, match="completion_conflict"):
        store.record(changed)
    assert store.read("daily-1") == record


def test_missing_dynamic_only_or_stale_record_never_counts_as_completion():
    store = CompletionStore(Objects())
    with pytest.raises(ValueError, match="completion_missing"):
        store.require("daily-1", dynamic_receipt_digest="e" * 64, capture_digest="a" * 64)
    store.record(completion())
    with pytest.raises(ValueError, match="completion_binding_differs"):
        store.require("daily-1", dynamic_receipt_digest="0" * 64, capture_digest="a" * 64)


def test_completion_tampering_cannot_recertify_itself():
    from src.analysis.open_intelligence.brain_contract import canonical_digest

    objects = Objects()
    store = CompletionStore(objects)
    original = completion()
    store.record(original)
    expected = canonical_digest(original)
    name = next(iter(objects.values))
    changed = copy.deepcopy(original)
    changed["metering"]["model_calls"] = 0
    objects.values[name] = (json.dumps(changed).encode(), 2)
    assert canonical_digest(changed) != expected
    with pytest.raises(ValueError, match="completion_digest_differs"):
        store.require("daily-1", expected_digest=expected)


def test_unmatched_paid_intent_is_unknown_and_cannot_replay():
    objects = Objects()
    store = CompletionStore(objects)
    assert store.begin_attempt("daily-1", "brief-za-1", {"prompt_digest": "a" * 64}) is None
    with pytest.raises(ValueError, match="paid_attempt_unknown"):
        store.begin_attempt("daily-1", "brief-za-1", {"prompt_digest": "a" * 64})
    store.finish_attempt("daily-1", "brief-za-1", {"response": "answer", "usage": {"tokens": 5}})
    assert (
        store.begin_attempt("daily-1", "brief-za-1", {"prompt_digest": "a" * 64})["response"]
        == "answer"
    )
    with pytest.raises(ValueError, match="paid_attempt_conflict"):
        store.begin_attempt("daily-1", "brief-za-1", {"prompt_digest": "b" * 64})


def test_completion_refuses_corrupt_storage_and_path_escape():
    objects = Objects()
    store = CompletionStore(objects)
    for value in ("../escape", "x/y", "", " padded "):
        with pytest.raises(ValueError):
            store.read(value)
    store.record(completion())
    name = next(iter(objects.values))
    record = json.loads(objects.values[name][0])
    record["operation_id"] = "other"
    objects.values[name] = (json.dumps(record).encode(), 2)
    with pytest.raises(ValueError, match="completion_binding_differs"):
        store.read("daily-1")


def test_release_proof_binds_composition_and_view_readback():
    from src.analysis.open_intelligence.brain_contract import canonical_digest

    objects = Objects()
    store = CompletionStore(objects)
    record = completion()
    store.record(record)
    release = {
        "schema_version": "42_daily_products_release_v1",
        "operation_id": "daily-1",
        "composition_digest": canonical_digest(record),
        "release_record_digest": "0" * 64,
        "run_id": "run-1",
        "view_row_count": 2,
        "view_rows_digest": "1" * 64,
    }
    assert store.record_release(release) == release
    assert store.require_release("daily-1", expected_digest=canonical_digest(release)) == release
    name = next(key for key in objects.values if key.endswith("release-v1.json"))
    changed = {**release, "view_row_count": 3}
    objects.values[name] = (json.dumps(changed).encode(), 2)
    with pytest.raises(ValueError, match="products_release_digest_differs"):
        store.require_release("daily-1", expected_digest=canonical_digest(release))


def _unfunded(record):
    for name in ("trend_analysis", "daily_summary", "seed_insights", "creator_briefs"):
        record["products"][name] = {
            "state": "unfunded",
            "row_count": 0,
            "output_digest": EMPTY_DIGEST,
            "readback_digest": EMPTY_DIGEST,
        }
    record["metering"]["model_calls"] = 0
    return record


def test_unfunded_products_are_accepted_only_for_an_origin_that_funds_no_call():
    record = _unfunded(completion())
    record["max_model_calls"] = 0
    assert validate_completion(record)["products"]["trend_analysis"]["state"] == "unfunded"
    funded = _unfunded(completion())
    with pytest.raises(ValueError, match="product_outcome_invalid"):
        validate_completion(funded)


def test_metered_model_calls_cannot_exceed_the_admitted_allowance():
    record = completion()
    record["max_model_calls"] = 5
    with pytest.raises(ValueError, match="product_metering_incomplete"):
        validate_completion(record)


@pytest.mark.parametrize(
    "field,value",
    [("output_digest", "f" * 64), ("readback_digest", "e" * 64), ("row_count", 1)],
)
def test_dynamic_view_outcome_is_bound_to_its_receipt_and_an_empty_read(field, value):
    record = completion()
    record["products"]["v_desk_dynamic_signals_v2"][field] = value
    with pytest.raises(ValueError):
        validate_completion(record)
