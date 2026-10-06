from datetime import timedelta

import pytest
from src.analysis.open_intelligence.general_question_control import QuestionStoreError
from src.analysis.open_intelligence.general_question_queries import GeneralQuestionQueries

from tests.unit import test_general_question_calls as calls_fixture
from tests.unit import test_general_question_queries as queries_fixture
from tests.unit import test_general_question_result as result_fixture
from tests.unit import test_general_question_store as fixture


def test_terminal_result_blocks_an_unused_model_slot_before_deadline():
    store, calls, bucket, rid = result_fixture.setup()
    result_fixture.publish(store, rid)
    uploads = len(bucket.uploads)
    with pytest.raises(QuestionStoreError, match="request_terminal"):
        calls_fixture.claim(calls, rid, now=fixture.NOW + timedelta(seconds=1))
    assert len(bucket.uploads) == uploads


def test_terminal_result_blocks_new_queries_but_preserves_existing_readback():
    store, _, bucket, rid = result_fixture.setup()
    queries = GeneralQuestionQueries(store)
    reserved = queries_fixture.reserve(queries, rid)
    result_fixture.publish(store, rid, state="held")
    uploads = len(bucket.uploads)
    assert queries_fixture.reserve(queries, rid) == reserved
    with pytest.raises(QuestionStoreError, match="request_terminal"):
        queries_fixture.reserve(queries, rid, ordinal=2)
    assert len(bucket.uploads) == uploads
    receipt = queries_fixture.receipt(reserved)
    assert (
        queries.record_query_receipt(rid, scope=fixture.scope(), ordinal=1, receipt=receipt)
        == receipt
    )


def test_terminal_result_allows_late_response_and_usage_reconciliation():
    store, calls, _, rid = result_fixture.setup()
    permit = calls_fixture.claim(calls, rid)
    result_fixture.publish(store, rid, state="held")
    raw = calls_fixture.native()
    calls.record_response(
        permit, scope=fixture.scope(), response=raw, received_at=fixture.NOW + timedelta(seconds=1)
    )
    usage = calls.persist_usage(
        rid, stage="planning", scope=fixture.scope(), persist=lambda event: event
    )
    assert usage.prompt_tokens == 500
    assert (
        calls.read_response(rid, stage="planning", scope=fixture.scope())["raw_sdk_response"] == raw
    )
    with pytest.raises(QuestionStoreError, match="request_terminal"):
        calls_fixture.claim(calls, rid, stage="answering")
