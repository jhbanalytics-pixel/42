from datetime import timedelta

import pytest

from tests.unit import test_general_question_calls as calls_fixture
from tests.unit import test_general_question_store as fixture


def test_usage_sink_acknowledges_only_exact_persisted_model_event():
    from src.analysis.open_intelligence.general_question_usage import persist_question_usage_event

    _, calls, bucket, rid = calls_fixture.setup()
    permit = calls_fixture.claim(calls, rid)
    calls.record_response(
        permit,
        scope=fixture.scope(),
        response=calls_fixture.native(),
        received_at=fixture.NOW + timedelta(seconds=1),
    )
    events = []

    def persist(event):
        acknowledged = persist_question_usage_event(event, store=calls.store, scope=fixture.scope())
        assert fixture.PREFIX + f"requests/{rid}/usage/planning.json" in bucket.objects
        events.append(acknowledged)
        return acknowledged

    event = calls.persist_usage(rid, stage="planning", scope=fixture.scope(), persist=persist)
    assert events == [event]
    assert event.prompt_tokens == 500
    assert event.completion_tokens == 120
    assert persist_question_usage_event(event, store=calls.store, scope=fixture.scope()) == event


def test_usage_sink_rejects_changed_event_even_when_identity_is_well_formed():
    from src.analysis.open_intelligence.general_question_usage import persist_question_usage_event
    from src.utils.gemini_usage import build_usage_event

    _, calls, bucket, rid = calls_fixture.setup()
    permit = calls_fixture.claim(calls, rid)
    calls.record_response(
        permit,
        scope=fixture.scope(),
        response=calls_fixture.native(),
        received_at=fixture.NOW + timedelta(seconds=1),
    )
    changed = build_usage_event(
        trend_date=fixture.NOW.date(),
        run_id="question_" + rid.replace("-", ""),
        consumer="open_question_answer",
        stage="planning",
        call_index=0,
        market=None,
        gemini_model="gemini-3.5-flash",
        prompt_tokens=999,
        completion_tokens=120,
        recorded_at=fixture.NOW + timedelta(seconds=1),
    )
    uploads = len(bucket.uploads)
    with pytest.raises(ValueError, match="usage_event_invalid"):
        persist_question_usage_event(changed, store=calls.store, scope=fixture.scope())
    assert len(bucket.uploads) == uploads
