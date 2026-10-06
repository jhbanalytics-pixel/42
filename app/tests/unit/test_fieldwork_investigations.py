import json
from copy import deepcopy
from datetime import UTC, datetime

from src.api import fieldwork, investigation_index, investigations


NOW = datetime(2026, 9, 9, 22, tzinfo=UTC)
STAMP = datetime(2026, 9, 7, 12, tzinfo=UTC)
SCOPE = dict(
    client_scope_id="ogilvy_default",
    market_scope=["za"],
    brand_config_id=None,
    audience_lens_ids=[],
    theme_id=None,
)


def record(question="What should we investigate?", **overrides):
    frame = investigations.InvestigationFrame(
        client_scope_id="ogilvy_default",
        market_scope=("za",),
        brand_config_id=None,
        audience_lens_ids=(),
        theme_id=None,
        run_id="investigation_run_001",
        contract_version="2.1.0",
        decision_question=question,
        time_horizon_days=42,
        brand_context=None,
        known_assumptions=(),
        change_my_mind_if=(),
        research_role_id=None,
        research_role_version=None,
        output_mode="internal_working_paper",
    )
    from dataclasses import replace

    frame = replace(frame, **overrides)
    response = investigations.build_plan_ready(
        frame=frame,
        plan=investigations.default_plan_for_frame(frame),
        investigation_id=investigations.investigation_id_for_frame(frame),
        created_at=STAMP,
        active_role_versions={},
    )
    return investigations.build_investigation_storage_record(frame, response)


class Blob:
    def __init__(self, value):
        self.value = value
        identity = value["response"]["investigation_id"]
        self.name = investigation_index._INVESTIGATIONS_PREFIX + identity + ".json"
        self.generation = "123"
        self.time_created = STAMP
        self.size = len(json.dumps(value).encode())
        self.reads = []

    def download_as_bytes(self, **kwargs):
        self.reads.append(kwargs)
        assert kwargs["if_generation_match"] == 123
        assert kwargs["retry"] is None
        return json.dumps(self.value).encode()


class Bucket:
    name = "listening-post-staging-cache"

    def __init__(self, blobs):
        self.blobs = blobs
        self.list_calls = []

    def list_blobs(self, **kwargs):
        self.list_calls.append(kwargs)
        assert 0 < kwargs["max_results"] <= 201

        class Pages:
            next_page_token = None

            def __iter__(inner):
                return iter(self.blobs[:201])

            @property
            def pages(inner):
                return iter([self.blobs[:201]])

        return Pages()


def observe(bucket):
    return investigation_index.observe_fieldwork_investigations(
        bucket=bucket,
        scope=deepcopy(SCOPE),
        now=NOW,
    )


def test_valid_plan_is_planned_with_original_time_and_generation_pinned_read():
    blob = Blob(record())
    result = observe(Bucket([blob]))
    assert result["coverage"]["state"] == "complete"
    assert result["coverage"]["total_count"] == 1
    assert result["coverage"]["observed_at"] == "2026-09-07T12:00:00Z"
    assert result["rows"][0]["status"] == "planned"
    assert result["rows"][0]["updated_at"] == "2026-09-07T12:00:00Z"
    assert len(blob.reads) == 1
    fieldwork.build_fieldwork_workspace_v2(
        scope=deepcopy(SCOPE),
        general_questions=None,
        investigation_plans=result,
        now=NOW,
    )


def test_empty_complete_read_has_zero_count_without_invented_source_timestamp():
    result = observe(Bucket([]))
    assert result["coverage"]["state"] == "complete"
    assert result["coverage"]["total_count"] == 0
    assert result["coverage"]["observed_at"] is None
    fieldwork.build_fieldwork_workspace_v2(
        scope=deepcopy(SCOPE),
        general_questions=None,
        investigation_plans=result,
        now=NOW,
    )


def test_unreadable_record_preserves_valid_rows_without_complete_scope_count():
    bad = Blob(record("Unknown scope?"))

    def fail(**kwargs):
        raise OSError("private detail")

    bad.download_as_bytes = fail
    result = observe(Bucket([bad, Blob(record())]))
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["total_count"] is None
    assert result["coverage"]["known_count"] == 1
    assert result["coverage"]["unread_count"] is None
    assert "private detail" not in json.dumps(result)


def test_more_than_200_records_cannot_be_reported_as_complete():
    blobs = [Blob(record(f"Question {i}?")) for i in range(201)]
    result = observe(Bucket(blobs))
    assert sum(len(blob.reads) for blob in blobs) == 200
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["total_count"] is None
    assert result["coverage"]["known_count"] == 200
    assert result["coverage"]["returned_count"] == 50


def test_cross_brand_and_tampered_identity_are_not_scoped_operations():
    foreign = Blob(record(brand_config_id="another_brand"))
    bad = Blob(record("Mismatched object?"))
    bad.name = investigation_index._INVESTIGATIONS_PREFIX + "inv_wrong.json"
    result = observe(Bucket([foreign, bad]))
    assert result["rows"] == []
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["invalid_count"] is None
    assert result["coverage"]["observed_at"] is None


def test_listing_failure_is_unavailable_not_empty():
    bucket = Bucket([])

    def fail(**kwargs):
        raise OSError("private detail")

    bucket.list_blobs = fail
    result = observe(bucket)
    assert result["coverage"]["state"] == "unavailable"
    assert result["coverage"]["total_count"] is None
    assert result["coverage"]["reasons"] == ["listing_unavailable"]


def test_lazy_listing_deadline_is_checked_before_consuming_every_item(monkeypatch):
    import time

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    consumed = []
    blobs = [Blob(record(f"Question {i}?")) for i in range(201)]

    def slow_items():
        for blob in blobs:
            clock[0] += 0.1
            consumed.append(blob)
            yield blob

    class SlowListing:
        next_page_token = None

        def __iter__(self):
            return slow_items()

        @property
        def pages(self):
            return iter([slow_items()])

    bucket = Bucket(blobs)
    bucket.list_blobs = lambda **kwargs: SlowListing()
    result = observe(bucket)
    assert len(consumed) < 201
    assert result["coverage"]["state"] == "partial"
    assert "read_deadline_reached" in result["coverage"]["reasons"]
    assert sum(len(blob.reads) for blob in blobs) == 0


def test_each_listing_page_uses_remaining_deadline(monkeypatch):
    import time

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    calls = []

    class Listing:
        next_page_token = None

        def __iter__(self):
            clock[0] += 4
            return iter([])

        @property
        def pages(self):
            clock[0] += 4
            self.next_page_token = "next" if len(calls) == 1 else None
            return iter([[]])

    bucket = Bucket([])

    def list_page(**kwargs):
        calls.append(kwargs)
        return Listing()

    bucket.list_blobs = list_page
    result = observe(bucket)
    assert len(calls) == 2
    assert calls[0]["timeout"] == 10
    assert calls[1]["timeout"] == 6
    assert calls[1]["page_token"] == "next"
    assert result["coverage"]["state"] == "complete"


def test_empty_page_returning_after_deadline_is_not_complete(monkeypatch):
    import time

    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class LatePage:
        next_page_token = None

        @property
        def pages(self):
            clock[0] = 11
            return iter([[]])

    bucket = Bucket([])
    bucket.list_blobs = lambda **kwargs: LatePage()
    result = observe(bucket)
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["total_count"] is None
