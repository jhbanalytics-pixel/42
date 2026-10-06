"""Unit tests for the system observability writer (src/observability/events.py).

Pins the two hard contracts: record_event never raises (a BQ failure degrades to
a logged warning) and the fatal marker is emitted only for fatal ERROR events;
plus the track context manager's ok / error-with-cause behaviour.
"""

import copy
import json
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from unittest import mock

import pytest
from src.observability import events
from src.observability.events import FATAL_MARKER, record_event, track
from src.utils.log_redactor import TokenRedactingFilter


def _structured_lines(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def _record_then_fail(error):
    with events.batch_events():
        record_event("test", "INFO", "first")
        raise error


def test_event_stream_survives_warehouse_failure_with_exact_context(monkeypatch, capsys):
    attempted = []

    def fail(frame, _table):
        attempted.extend(frame.to_dict(orient="records"))
        raise RuntimeError("password=PRIVATE_WAREHOUSE_DETAIL")

    monkeypatch.setattr(events, "insert_dataframe", fail)
    record_event("engine_cron", "WARN", "stage", meta={"run_id": "run-1", "stage": "seed"})
    captured = capsys.readouterr()
    assert captured.out == ""
    stream = _structured_lines(captured.err)
    assert stream[0]["severity"] == "WARNING"
    assert stream[0]["event_id"] == attempted[0]["event_id"]
    assert stream[0]["logging.googleapis.com/insertId"] == attempted[0]["event_id"]
    assert stream[0]["meta"] == {"run_id": "run-1", "stage": "seed"}
    failure = next(row for row in stream if row["event_type"] == "telemetry_delivery_failed")
    assert failure["status"] == "unavailable"
    assert failure["meta"]["event_ids"] == [attempted[0]["event_id"]]
    assert "PRIVATE_WAREHOUSE_DETAIL" not in captured.err


def test_batch_emits_immediately_and_flushes_at_bound_and_exit(monkeypatch, capsys):
    batches = []

    def persist(frame, _table):
        batches.append(frame.to_dict(orient="records"))
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    with events.batch_events():
        for index in range(205):
            record_event("test", "INFO", "stage", meta={"run_id": "run", "index": index})
            if index == 0:
                assert batches == []
                initial = _structured_lines(capsys.readouterr().err)
                assert initial[0]["meta"]["index"] == 0
        assert [len(batch) for batch in batches] == [100, 100]
    assert [len(batch) for batch in batches] == [100, 100, 5]
    rows = [row for batch in batches for row in batch]
    assert [json.loads(row["meta"])["index"] for row in rows] == list(range(205))
    assert len({row["event_id"] for row in rows}) == 205


def test_batch_flush_on_exception_keeps_business_error_and_restores_context(monkeypatch):
    batches = []

    def persist(frame, _table):
        batches.append(frame.to_dict(orient="records"))
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    error = ValueError("business failure")
    with pytest.raises(ValueError) as raised:
        _record_then_fail(error)
    assert raised.value is error
    record_event("test", "INFO", "second")
    assert [[row["event_type"] for row in batch] for batch in batches] == [["first"], ["second"]]


def test_nested_and_parallel_batches_do_not_steal_each_others_rows(monkeypatch):
    batches = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def persist(frame, _table):
        with lock:
            batches.append(frame.to_dict(orient="records"))
        return len(frame)

    def worker(run_id):
        with events.batch_events():
            record_event("test", "INFO", "outer", meta={"run_id": run_id})
            barrier.wait(timeout=5)
            with events.batch_events():
                record_event("test", "INFO", "inner", meta={"run_id": run_id})
            record_event("test", "INFO", "outer", meta={"run_id": run_id})

    monkeypatch.setattr(events, "insert_dataframe", persist)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker, ("run-1", "run-2")))
    assert sorted(len(batch) for batch in batches) == [1, 1, 2, 2]
    assert all(len({json.loads(row["meta"])["run_id"] for row in batch}) == 1 for batch in batches)


def test_batch_preserves_nullable_integer_latency_for_warehouse(monkeypatch):
    frames = []

    def persist(frame, _table):
        frames.append(frame.copy())
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    with events.batch_events():
        record_event("test", "INFO", "first", latency_ms=None)
        record_event("test", "INFO", "second", latency_ms=17)
    assert str(frames[0]["latency_ms"].dtype) == "Int64"
    assert frames[0]["latency_ms"].isna().tolist() == [True, False]
    assert frames[0]["latency_ms"].iloc[1] == 17


def test_oversized_metadata_preserves_exact_correlation_fields(monkeypatch, capsys):
    rows = []

    def persist(frame, _table):
        rows.extend(frame.to_dict(orient="records"))
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    record_event(
        "test",
        "INFO",
        "stage",
        meta={
            "blob": "x" * 30000,
            "run_id": "run-123",
            "request_id": "request-456",
            "stage": "fetch",
        },
    )
    meta = json.loads(rows[0]["meta"])
    assert meta["run_id"] == "run-123"
    assert meta["request_id"] == "request-456"
    assert meta["stage"] == "fetch"
    assert meta["_truncated"] is True
    assert len(rows[0]["meta"]) <= 8000
    assert _structured_lines(capsys.readouterr().err)[0]["meta"] == meta


def test_nonfinite_metadata_is_null_and_keeps_context(monkeypatch, capsys):
    rows = []
    monkeypatch.setattr(
        events,
        "insert_dataframe",
        lambda frame, _table: rows.extend(frame.to_dict(orient="records")) or len(frame),
    )
    record_event(
        "test", "INFO", "stage", meta={"run_id": "run-1", "values": [math.nan, math.inf, -math.inf]}
    )
    meta = json.loads(rows[0]["meta"], parse_constant=lambda _value: pytest.fail("nonfinite JSON"))
    assert meta == {"run_id": "run-1", "values": [None, None, None]}
    assert _structured_lines(capsys.readouterr().err)[0]["meta"] == meta


def test_unconfirmed_write_count_is_reported_without_retry(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        events, "insert_dataframe", lambda frame, _table: calls.append(len(frame)) or 0
    )
    record_event("test", "INFO", "stage")
    assert calls == [1]
    failures = [
        row
        for row in _structured_lines(capsys.readouterr().err)
        if row["event_type"] == "telemetry_delivery_failed"
    ]
    assert failures[0]["meta"]["reason_code"] == "write_count_unconfirmed"


def test_broken_event_stream_does_not_block_warehouse_or_business_error(monkeypatch):
    rows = []

    class BrokenStream:
        def write(self, _text):
            raise OSError("stream unavailable")

        def flush(self):
            pass

    monkeypatch.setattr("sys.stderr", BrokenStream())
    monkeypatch.setattr(
        events,
        "insert_dataframe",
        lambda frame, _table: rows.extend(frame.to_dict(orient="records")) or len(frame),
    )
    error = ValueError("original failure")
    with pytest.raises(ValueError) as raised:
        _record_then_fail(error)
    assert raised.value is error
    assert rows[0]["event_type"] == "first"


def test_invalid_metadata_still_reports_batch_delivery_failure(monkeypatch, capsys):
    class Unprintable:
        def __str__(self):
            raise ValueError("private detail")

    monkeypatch.setattr(
        events, "insert_dataframe", mock.Mock(side_effect=TimeoutError("unknown write"))
    )
    with events.batch_events():
        record_event("test", "INFO", "stage", meta={"extra": Unprintable()})
    stream = _structured_lines(capsys.readouterr().err)
    assert stream[0]["meta"] == {"_serialization_error": True}
    assert stream[1]["event_type"] == "telemetry_delivery_failed"


@pytest.mark.parametrize(
    "latency", [math.nan, True, "bad", -1], ids=["nan", "bool", "text", "negative"]
)
def test_invalid_latency_does_not_drop_event_or_invent_measurement(monkeypatch, capsys, latency):
    rows = []
    monkeypatch.setattr(
        events,
        "insert_dataframe",
        lambda frame, _table: rows.extend(frame.to_dict(orient="records")) or len(frame),
    )
    record_event("test", "INFO", "stage", latency_ms=latency, meta={"run_id": "run-1"})
    assert len(rows) == 1
    stream = _structured_lines(capsys.readouterr().err)[0]
    assert stream["latency_ms"] is None
    assert stream["meta"]["run_id"] == "run-1"
    assert stream["meta"]["_invalid_fields"] == ["latency_ms"]


def test_latency_warning_survives_large_metadata(monkeypatch, capsys):
    monkeypatch.setattr(events, "insert_dataframe", lambda frame, _table: len(frame))
    record_event(
        "test", "INFO", "stage", latency_ms=True, meta={"run_id": "run-1", "blob": "x" * 30000}
    )
    stream = _structured_lines(capsys.readouterr().err)[0]
    assert stream["meta"]["_invalid_fields"] == ["latency_ms"]
    assert stream["meta"]["run_id"] == "run-1"


def test_inherited_context_after_batch_exit_has_a_live_delivery_path(monkeypatch):
    rows = []

    def persist(frame, _table):
        rows.extend(frame.to_dict(orient="records"))
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    with events.batch_events():
        inherited = copy_context()
        record_event("test", "INFO", "parent")
    inherited.run(record_event, "test", "INFO", "late_child")
    assert [row["event_type"] for row in rows] == ["parent", "late_child"]


def test_inherited_context_during_owner_flush_does_not_strand_rows(monkeypatch):
    rows = []
    late_written = False

    def persist(frame, _table):
        nonlocal late_written
        rows.extend(frame.to_dict(orient="records"))
        if not late_written:
            late_written = True
            inherited.run(record_event, "test", "INFO", "child_during_flush")
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    with events.batch_events():
        inherited = copy_context()
        record_event("test", "INFO", "parent")
    assert [row["event_type"] for row in rows] == ["parent", "child_during_flush"]


def test_escaped_preserved_context_cannot_overrun_metadata_cap(monkeypatch):
    rows = []

    def persist(frame, _table):
        rows.extend(frame.to_dict(orient="records"))
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    record_event("test", "INFO", "stage", meta=dict.fromkeys(events._CONTEXT_FIELDS, "\0" * 256))
    assert len(rows[0]["meta"]) <= 8000
    meta = json.loads(rows[0]["meta"])
    assert set(meta["_invalid_context_fields"]) == events._CONTEXT_FIELDS
    assert all(meta[field] == "***UNAVAILABLE***" for field in events._CONTEXT_FIELDS)


def test_metadata_error_fallback_preserves_unicode_context_within_cap(monkeypatch):
    rows = []

    def persist(frame, _table):
        rows.extend(frame.to_dict(orient="records"))
        return len(frame)

    monkeypatch.setattr(events, "insert_dataframe", persist)
    meta = dict.fromkeys(events._CONTEXT_FIELDS, "界" * 256)
    meta[("unsupported", "key")] = "value"
    record_event("test", "INFO", "stage", meta=meta)
    assert len(rows[0]["meta"]) <= 8000
    restored = json.loads(rows[0]["meta"])
    assert restored["_serialization_error"] is True
    assert all(restored[field] == "界" * 256 for field in events._CONTEXT_FIELDS)


def test_record_event_non_fatal_when_insert_raises():
    """A BigQuery failure logs and returns; it never raises into the caller."""
    with mock.patch.object(events, "insert_dataframe", side_effect=RuntimeError("bq down")):
        record_event("engine_cron", "ERROR", "connector_fail", error_detail="boom")
    # No exception is the assertion.


def test_record_event_builds_expected_row():
    captured = {}

    def _capture(df, table):
        captured["table"] = table
        captured["row"] = df.iloc[0].to_dict()
        return 1

    with mock.patch.object(events, "insert_dataframe", side_effect=_capture):
        record_event(
            "lp_chat",
            "INFO",
            "chat_turn",
            status="ok",
            market="za",
            message="answered",
            latency_ms=1234,
            meta={"q": "SYNTHETIC_RAW_QUESTION", "result_count": 4},
        )
    assert captured["table"] == "system_events"
    row = captured["row"]
    assert row["source"] == "lp_chat"
    assert row["severity"] == "INFO"
    assert row["event_type"] == "chat_turn"
    assert row["status"] == "ok"
    assert row["market"] == "za"
    assert row["latency_ms"] == 1234
    parsed_meta = json.loads(row["meta"])
    assert parsed_meta["q"] == "***WITHHELD***"
    assert parsed_meta["result_count"] == 4


def test_record_event_sanitizes_exact_row_without_mutating_inputs():
    captured = {}
    meta = {
        "safe": {"count": 3, "stage": "fetch"},
        "credentials": [
            {"token": "SYNTHETIC_SECRET_TOKEN.!:/?=+"},
            {"api_key": "SYNTHETIC_SECRET_API.!:/?=+"},
            {"access_token": "SYNTHETIC_SECRET_ACCESS.!:/?=+"},
            {"refresh-token": "SYNTHETIC_SECRET_REFRESH.!:/?=+"},
            {"client-secret": "SYNTHETIC_SECRET_CLIENT.!:/?=+"},
            {"password": "SYNTHETIC_SECRET_PASSWORD.!:/?=+"},
            {"passcode": "SYNTHETIC_SECRET_PASSCODE.!:/?=+"},
            {"authorization": "Bearer SYNTHETIC_SECRET_AUTH.a/b+c=="},
            {"X-Api-Key": "SYNTHETIC_SECRET_X_API.!:/?=+"},
            {"x_passcode": "SYNTHETIC_SECRET_X_PASSCODE.!:/?=+"},
        ],
        "q": "SYNTHETIC_RAW_Q",
        "question": "SYNTHETIC_RAW_QUESTION",
        "prompt": "SYNTHETIC_RAW_PROMPT",
        "messages": ["SYNTHETIC_RAW_MESSAGES"],
        "request_body": {"question": "SYNTHETIC_RAW_QUESTION"},
        "history": ["SYNTHETIC_RAW_HISTORY"],
        "response-body": "SYNTHETIC_RAW_RESPONSE",
        "raw_payload": "SYNTHETIC_RAW_PAYLOAD",
        "note": "passcode=SYNTHETIC_SECRET_NOTE.!:/?=+",
        "serialized_prompt": r'{"prompt": "safe \" SYNTHETIC_RAW_META_ESCAPED"}',
    }
    original = copy.deepcopy(meta)

    def _capture(df, table):
        captured["table"] = table
        captured["row"] = df.iloc[0].to_dict()
        return 1

    with mock.patch.object(events, "insert_dataframe", side_effect=_capture):
        record_event(
            "lp_api",
            "ERROR",
            "request_failed",
            message=(
                'request failed {"messages": [{"role": "user", '
                '"content": "SYNTHETIC_RAW_MESSAGE_CONTAINER"}]}'
            ),
            error_detail=(r'RuntimeError {"password": "safe \" SYNTHETIC_SECRET_DETAIL_ESCAPED"}'),
            meta=meta,
        )

    row = captured["row"]
    serialized_row = " ".join(str(value) for value in row.values())
    parsed_meta = json.loads(row["meta"])
    for sentinel in (
        "SYNTHETIC_SECRET_TOKEN",
        "SYNTHETIC_SECRET_API",
        "SYNTHETIC_SECRET_ACCESS",
        "SYNTHETIC_SECRET_REFRESH",
        "SYNTHETIC_SECRET_CLIENT",
        "SYNTHETIC_SECRET_PASSWORD",
        "SYNTHETIC_SECRET_PASSCODE",
        "SYNTHETIC_SECRET_AUTH",
        "SYNTHETIC_SECRET_X_API",
        "SYNTHETIC_SECRET_X_PASSCODE",
        "SYNTHETIC_SECRET_NOTE",
        "SYNTHETIC_SECRET_DETAIL_ESCAPED",
        "SYNTHETIC_RAW_Q",
        "SYNTHETIC_RAW_QUESTION",
        "SYNTHETIC_RAW_PROMPT",
        "SYNTHETIC_RAW_MESSAGES",
        "SYNTHETIC_RAW_HISTORY",
        "SYNTHETIC_RAW_RESPONSE",
        "SYNTHETIC_RAW_PAYLOAD",
        "SYNTHETIC_RAW_META_ESCAPED",
        "SYNTHETIC_RAW_MESSAGE_CONTAINER",
    ):
        assert sentinel not in serialized_row
    assert parsed_meta["safe"] == {"count": 3, "stage": "fetch"}
    assert all(
        next(iter(credential.values())) == "***REDACTED***"
        for credential in parsed_meta["credentials"]
    )
    for field in (
        "q",
        "question",
        "prompt",
        "messages",
        "request_body",
        "history",
        "response-body",
        "raw_payload",
    ):
        assert parsed_meta[field] == "***WITHHELD***"
    assert "RuntimeError" in row["error_detail"]
    assert "***WITHHELD***" in row["message"]
    assert "***REDACTED***" in row["error_detail"]
    assert "***WITHHELD***" in parsed_meta["serialized_prompt"]
    assert meta == original


class _CaptureHandler(logging.Handler):
    def __init__(self, ordered_events):
        super().__init__()
        self.ordered_events = ordered_events
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def emit(self, record):
        self.ordered_events.append(("log", record.levelname, self.format(record)))


def test_fatal_marker_is_sanitized_and_emitted_before_a_failing_insert():
    ordered_events = []
    test_logger = logging.Logger("events.fatal.boundary")
    test_logger.addFilter(TokenRedactingFilter())
    test_logger.addHandler(_CaptureHandler(ordered_events))

    def _fail_insert(df, table):
        ordered_events.append(("insert", table, df.iloc[0].to_dict()))
        raise RuntimeError("bq down token=SYNTHETIC_SECRET_WRITE_FAILURE")

    with (
        mock.patch.object(events, "insert_dataframe", side_effect=_fail_insert),
        mock.patch.object(events, "logger", test_logger),
    ):
        record_event(
            "email",
            "ERROR",
            "digest_failed",
            message="render crash password=SYNTHETIC_SECRET_FATAL.!:/?=+",
            fatal=True,
        )

    assert ordered_events[0][0:2] == ("log", "ERROR")
    assert FATAL_MARKER in ordered_events[0][2]
    assert "SYNTHETIC_SECRET_FATAL" not in ordered_events[0][2]
    assert ordered_events[1][0:2] == ("insert", "system_events")
    assert "SYNTHETIC_SECRET_FATAL" not in str(ordered_events[1][2])
    assert "SYNTHETIC_SECRET_WRITE_FAILURE" not in str(ordered_events)


def test_oversized_unicode_metadata_is_bounded_valid_json_with_context():
    captured = {}

    def _capture(df, table):
        captured["meta"] = df.iloc[0]["meta"]
        return 1

    meta = {"safe_context": "connector=synthetic", "blob": "🔒" * 12000}
    with mock.patch.object(events, "insert_dataframe", side_effect=_capture):
        record_event("engine_cron", "INFO", "large_meta", meta=meta)

    persisted = captured["meta"]
    parsed = json.loads(persisted)
    assert len(persisted) <= events._DETAIL_CAP
    assert parsed["_truncated"] is True
    assert "safe_context" in parsed["preview"]
    assert meta["blob"] == "🔒" * 12000


def test_metadata_serialization_failure_is_nonfatal_and_explicit():
    captured = {}

    def _capture(df, table):
        captured["meta"] = df.iloc[0]["meta"]
        return 1

    with mock.patch.object(events, "insert_dataframe", side_effect=_capture):
        record_event("engine_cron", "INFO", "bad_meta", meta={("bad", "key"): "safe"})

    assert json.loads(captured["meta"]) == {"_serialization_error": True}


def test_logger_failure_does_not_escape_or_prevent_insert_attempt():
    with (
        mock.patch.object(events, "logger") as log,
        mock.patch.object(events, "insert_dataframe", return_value=1) as insert,
    ):
        log.error.side_effect = RuntimeError("logging unavailable")
        record_event("email", "ERROR", "digest_failed", message="boom", fatal=True)

    insert.assert_called_once()


@pytest.mark.parametrize("insert_fails", [False, True])
def test_empty_fatal_detail_keeps_marker_before_insert(insert_fails):
    ordered_events = []
    test_logger = logging.Logger(f"events.empty.fatal.{insert_fails}")
    test_logger.addFilter(TokenRedactingFilter())
    test_logger.addHandler(_CaptureHandler(ordered_events))

    def _insert(df, table):
        ordered_events.append(("insert", table, df.iloc[0].to_dict()))
        if insert_fails:
            raise RuntimeError("bq down")
        return 1

    with (
        mock.patch.object(events, "insert_dataframe", side_effect=_insert),
        mock.patch.object(events, "logger", test_logger),
    ):
        record_event("email", "ERROR", "digest_failed", fatal=True)

    assert ordered_events[0][0:2] == ("log", "ERROR")
    assert FATAL_MARKER in ordered_events[0][2]
    assert ordered_events[1][0:2] == ("insert", "system_events")


def test_record_event_normalises_unknown_severity():
    captured = {}
    with mock.patch.object(
        events, "insert_dataframe", side_effect=lambda df, t: captured.update(row=df.iloc[0])
    ):
        record_event("engine_cron", "LOUD", "cron_run")
    assert captured["row"]["severity"] == "INFO"


def test_fatal_marker_emitted_only_for_fatal_error():
    with (
        mock.patch.object(events, "insert_dataframe", return_value=1),
        mock.patch.object(events, "logger") as log,
    ):
        record_event("email", "ERROR", "digest_failed", message="render crash", fatal=True)
    msgs = " ".join(str(c.args) for c in log.error.call_args_list)
    assert FATAL_MARKER in msgs


def test_no_fatal_marker_for_non_fatal_error():
    with (
        mock.patch.object(events, "insert_dataframe", return_value=1),
        mock.patch.object(events, "logger") as log,
    ):
        record_event("engine_cron", "ERROR", "connector_fail", message="rss blip", fatal=False)
    msgs = " ".join(str(c.args) for c in log.error.call_args_list)
    assert FATAL_MARKER not in msgs


def test_track_records_ok_on_success():
    calls = []
    with (
        mock.patch.object(events, "record_event", side_effect=lambda *a, **k: calls.append((a, k))),
        track("lp_chat", "chat_turn", market="ng"),
    ):
        pass
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "lp_chat"
    assert args[1] == "INFO"
    assert kwargs["status"] == "ok"
    assert "latency_ms" in kwargs


def test_track_records_error_with_cause_and_reraises():
    calls = []
    with (
        mock.patch.object(events, "record_event", side_effect=lambda *a, **k: calls.append((a, k))),
        pytest.raises(ValueError),
        track("lp_chat", "chat_turn"),
    ):
        raise ValueError("nope")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == "ERROR"
    assert kwargs["status"] == "failed"
    assert kwargs["fatal"] is True
    assert "ValueError" in kwargs["error_detail"]
