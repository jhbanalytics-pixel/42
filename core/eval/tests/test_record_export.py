"""Hermetic checks for the BUILD1.16 local frozen research record seam."""

import copy
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.agent.checks import check_answer
from core.agent.context import RunContext, result_hash
from core.eval import blind_test

AS_OF = datetime(2026, 9, 29, 10, 30, tzinfo=timezone(timedelta(hours=2)))
WINDOW = (date(2026, 9, 22), date(2026, 9, 28))
RUN_ID = "r_20260929_103000_abcd1234"
SQL = "SELECT posts, day FROM counts WHERE market = @market"
PARAMS = {"market": "ZA"}
ROWS = [{"posts": 41, "ratio": Decimal("2.8"), "day": date(2026, 9, 27)}]


def make_context():
    ctx = RunContext(run_id=RUN_ID, tier="T1", as_of=AS_OF, market="ZA",
                     window_start=WINDOW[0], window_end=WINDOW[1])
    ctx.evidence["tt_1"] = {
        "id": "tt_1", "platform": "tiktok", "handle": "@someone",
        "url": "https://example.test/tt_1", "posted_at": "2026-09-27T09:00:00+02:00",
        "market": "ZA", "text": "My count is 41 posts this week.",
        "engagement": {"views": 10}, "flags": [],
    }
    ctx.record_query(SQL, PARAMS, copy.deepcopy(ROWS), "count of posts by market")
    ctx.forecasts = {"forecast-control-sentinel": {
        "forecast_id": "forecast-control-sentinel", "statement": "Forecast gate snapshot",
        "evidence_ids": ["tt_1"],
    }}
    ctx.native_statuses = {"zu": "native-status-control-sentinel"}
    ctx.native_languages = {"tt_1": ["native-language-control-sentinel"]}
    ctx.native_review_loaded = True
    ctx.native_review_available = False
    ctx.native_language_attempted = {"tt_1"}
    ctx.access_token = "credential-control-sentinel"
    ctx.client = object()
    return ctx


def make_run(ctx):
    return {
        "run_id": ctx.run_id,
        "tier": ctx.tier,
        "window": {"from": WINDOW[0].isoformat(), "to": WINDOW[1].isoformat()},
    }


def make_saved_attempt(ctx):
    run = make_run(ctx)
    run["source_status"] = [{"route": "reddit/search", "status": "timeout"}]
    run["booked_usd"] = "financial-wrapper-sentinel"
    captured_ctx = {
        "run_id": ctx.run_id,
        "tier": ctx.tier,
        "as_of": ctx.as_of.isoformat(),
        "market": ctx.market,
        "window_start": WINDOW[0].isoformat(),
        "window_end": WINDOW[1].isoformat(),
        "evidence": ctx.evidence,
        "queries": ctx.queries,
        "forecasts": ctx.forecasts,
        "native_statuses": ctx.native_statuses,
        "native_languages": ctx.native_languages,
        "native_review_loaded": ctx.native_review_loaded,
        "native_review_available": ctx.native_review_available,
        "native_language_attempted": sorted(ctx.native_language_attempted),
        "events": [{"step": "get_comments", "route": "tiktok/comments", "status": "partial",
                    "payload": "event-wrapper-sentinel"}],
        "sc_calls": [{"route": "reddit/search", "params": {"query": "7colours"}, "status": "timeout",
                      "backend_token": "call-wrapper-sentinel"}],
        "secret": "context-wrapper-sentinel",
    }
    attempt = {
        "id": "NOW-01",
        "question": "How many posts were recorded?",
        "markets": ["ZA"],
        "run_id": ctx.run_id,
        "run": run,
        "ctx": captured_ctx,
        "query_hashes": {query_id: query["result_hash"] for query_id, query in ctx.queries.items()},
        "raw_answer": "answer-wrapper-sentinel",
        "reported_model_usd": "model-spend-wrapper-sentinel",
    }
    def jsonable(value):
        return value.isoformat() if isinstance(value, (date, datetime)) else str(value)

    return json.loads(json.dumps(attempt, default=jsonable))


def export_record(ctx, **overrides):
    from core.eval.record_export import record_from_ask

    values = {
        "id": "NOW-01",
        "question": "How many posts were recorded?",
        "markets": ["ZA"],
        "run": make_run(ctx),
        "gaps": [{"what": "A source gap from the research context"}],
        "failed_sources": [{"route": "x/search", "status": "timeout"}],
        "note": "The research returned this note.",
    }
    values.update(overrides)
    return record_from_ask(ctx, **values)


def numbered_draft(ctx, record):
    evidence = copy.deepcopy(record["evidence"][0])
    query = ctx.queries["q_1"]
    return {
        "status": "complete",
        "as_of": AS_OF.isoformat(),
        "short_answer": "The recorded count was 41 posts.",
        "claims": [{
            "id": "c1", "text": "The count was 41 posts.", "label": "single_source",
            "kind": "observation", "evidence_ids": [evidence["id"]],
            "quotes": [{"evidence_id": evidence["id"], "text": "41 posts this week"}],
            "numbers": [{"value": 41, "unit": "posts", "query_id": "q_1", "run_id": ctx.run_id,
                         "result_hash": query["result_hash"]}],
        }],
        "evidence": [evidence], "so_what": [], "watch_next": [], "gaps": [],
    }


def test_exported_record_replays_the_pinned_number_and_restores_controls(tmp_path):
    from core.eval.record_export import write_record

    original = make_context()
    record = export_record(original)
    path = write_record(tmp_path, record)
    loaded = blind_test.load_replay(path)
    replay = loaded[0]

    assert replay["window"] == {"from": "2026-09-22", "to": "2026-09-28"}
    assert replay["as_of"] == AS_OF.isoformat()
    assert replay["evidence"] == [original.evidence["tt_1"]]
    assert replay["queries"]["q_1"]["sql"] == SQL
    assert replay["queries"]["q_1"]["params"] == PARAMS
    assert replay["queries"]["q_1"]["rows"] == [{"posts": 41, "ratio": "2.8", "day": "2026-09-27"}]
    assert replay["queries"]["q_1"]["result_hash"] == original.queries["q_1"]["result_hash"]
    assert result_hash(replay["queries"]["q_1"]["rows"]) == replay["queries"]["q_1"]["result_hash"]
    assert replay["gaps"] == [{"what": "A source gap from the research context"}]
    assert replay["failed_sources"] == [{"route": "x/search", "status": "timeout"}]
    assert replay["note"] == "The research returned this note."

    ctx = blind_test.context_for(replay)
    assert ctx.run_id == RUN_ID and ctx.market == "ZA" and ctx.as_of == AS_OF
    assert ctx.window_start == WINDOW[0] and ctx.window_end == WINDOW[1]
    assert ctx.forecasts == original.forecasts
    assert ctx.native_statuses == original.native_statuses
    assert ctx.native_languages == original.native_languages
    assert ctx.native_review_loaded is True and ctx.native_review_available is False
    assert ctx.native_language_attempted == {"tt_1"}
    packed = blind_test._pack(replay)
    for sentinel in ("forecast-control-sentinel", "native-status-control-sentinel",
                     "native-language-control-sentinel"):
        assert sentinel not in packed

    answer, verdicts = check_answer(numbered_draft(ctx, replay), ctx, blind_test.ReplayWarehouse(replay),
                                    window=WINDOW, markets=["ZA"])
    assert [n["value"] for c in answer["claims"] for n in c.get("numbers", [])] == [41]
    assert not [v for v in verdicts if v["rule"] == "K2" and v["verdict"] == "cut"]

    ctx.queries["q_1"]["sql"] = "SELECT 42 AS posts"
    missed, miss_verdicts = check_answer(numbered_draft(ctx, replay), ctx, blind_test.ReplayWarehouse(replay),
                                         window=WINDOW, markets=["ZA"])
    assert missed["claims"] == []
    assert any(v["rule"] == "K2" and v["verdict"] == "cut" and "LookupError" in v["reason"]
               for v in miss_verdicts)


def test_record_is_detached_from_context_and_exclusive_file_write_never_overwrites(tmp_path):
    from core.eval.record_export import write_record

    ctx = make_context()
    ctx.sc_calls = [{"route": "reddit/search", "params": {"query": "source-control-sentinel"}, "status": "ok"}]
    record = export_record(ctx)
    assert "credential-control-sentinel" not in repr(record)
    assert "client" not in record and "lock" not in record
    ctx.evidence["tt_1"]["text"] = "changed after export"
    ctx.queries["q_1"]["rows"][0]["posts"] = 99
    ctx.forecasts.clear()
    ctx.sc_calls[0]["params"]["query"] = "changed after export"
    assert record["evidence"][0]["text"] == "My count is 41 posts this week."
    assert record["queries"]["q_1"]["rows"][0]["posts"] == 41
    assert record["gate_controls"]["forecasts"]
    assert record["gate_controls"]["sc_calls"][0]["params"]["query"] == "source-control-sentinel"

    path = write_record(tmp_path, record)
    before = path.read_bytes()
    record["note"] = "a later value"
    with pytest.raises(FileExistsError):
        write_record(tmp_path, record)
    assert path.read_bytes() == before


def test_record_filename_cannot_escape_the_selected_directory(tmp_path):
    from core.eval.record_export import write_record

    record = export_record(make_context(), id="../../escape")
    path = write_record(tmp_path, record)
    assert path.parent == tmp_path
    assert path.name == "escape.json"


def test_saved_attempt_adapts_only_replay_inputs_and_preserves_serialized_hashes():
    from core.agent.context import result_hash
    from core.eval.record_export import record_from_saved_attempt

    ctx = make_context()
    attempt = make_saved_attempt(ctx)
    record = record_from_saved_attempt(attempt)

    assert record["id"] == attempt["id"]
    assert record["question"] == attempt["question"]
    assert record["window"] == {"from": WINDOW[0].isoformat(), "to": WINDOW[1].isoformat()}
    assert record["as_of"] == attempt["ctx"]["as_of"]
    assert record["evidence"] == list(attempt["ctx"]["evidence"].values())
    assert record["queries"] == attempt["ctx"]["queries"]
    assert all(result_hash(q["rows"]) == q["result_hash"] == attempt["query_hashes"][qid]
               for qid, q in record["queries"].items())
    assert record["failed_sources"] == attempt["run"]["source_status"]
    assert record["gaps"] == [] and record["note"] == ""
    assert record["replay_metadata"] == {
        "snapshot_phase": "after_answer", "research_note_available": False,
        "declared_markets": ["ZA"], "query_hash_recovery": [],
    }
    assert record["gate_controls"]["forecasts"] == attempt["ctx"]["forecasts"]
    assert record["gate_controls"]["native_statuses"] == attempt["ctx"]["native_statuses"]
    assert record["gate_controls"]["native_languages"] == attempt["ctx"]["native_languages"]
    assert record["gate_controls"]["source_events"] == [
        {"step": "get_comments", "route": "tiktok/comments", "status": "partial"},
    ]
    assert record["gate_controls"]["sc_calls"] == [
        {"route": "reddit/search", "params": {"query": "7colours"}, "status": "timeout"},
    ]

    replay = blind_test.context_for(record)
    assert replay.run_id == RUN_ID
    assert replay.window_start == WINDOW[0] and replay.window_end == WINDOW[1]
    assert replay.queries["q_1"]["rows"] == [
        {"posts": 41, "ratio": "2.8", "day": "2026-09-27"},
    ]
    assert blind_test.ReplayWarehouse(record).run(SQL, PARAMS, None) == replay.queries["q_1"]["rows"]
    packed = blind_test._pack(record)
    assert "after_answer" not in packed and "wrapper-sentinel" not in packed
    for sentinel in ("financial-wrapper-sentinel", "model-spend-wrapper-sentinel",
                     "answer-wrapper-sentinel", "context-wrapper-sentinel", "call-wrapper-sentinel"):
        assert sentinel not in repr(record)
    attempt["ctx"]["evidence"]["tt_1"]["text"] = "changed after adaptation"
    attempt["ctx"]["queries"]["q_1"]["rows"][0]["posts"] = 99
    assert record["evidence"][0]["text"] == "My count is 41 posts this week."
    assert record["queries"]["q_1"]["rows"][0]["posts"] == 41


def test_saved_attempt_recovers_only_runner_datetime_encoding_for_hashes():
    from core.agent.context import result_hash
    from core.eval.record_export import record_from_saved_attempt

    ctx = make_context()
    ctx.queries["q_1"]["rows"][0]["captured_at"] = datetime(
        2026, 9, 27, 9, 0, tzinfo=timezone(timedelta(hours=2)))
    ctx.queries["q_1"]["result_hash"] = result_hash(ctx.queries["q_1"]["rows"])
    attempt = make_saved_attempt(ctx)
    saved_row = attempt["ctx"]["queries"]["q_1"]["rows"][0]
    record = record_from_saved_attempt(attempt)
    replay_row = record["queries"]["q_1"]["rows"][0]

    assert saved_row["captured_at"] == "2026-09-27T09:00:00+02:00"
    assert replay_row["captured_at"] == "2026-09-27 09:00:00+02:00"
    assert result_hash([replay_row]) == attempt["ctx"]["queries"]["q_1"]["result_hash"]
    assert record["replay_metadata"]["query_hash_recovery"] == [
        {"query_id": "q_1", "method": "iso_datetime_to_datetime_string"},
    ]
    assert saved_row["captured_at"] == "2026-09-27T09:00:00+02:00"


@pytest.mark.parametrize(("effective_market", "expected_markets", "expected_context_market"), [
    ("KE", ["KE"], "KE"),
    (None, ["ZA", "KE"], None),
])
def test_saved_attempt_preserves_effective_and_declared_markets(
        effective_market, expected_markets, expected_context_market):
    from core.eval.record_export import record_from_saved_attempt

    attempt = make_saved_attempt(make_context())
    attempt["markets"] = ["ZA", "KE"]
    attempt["ctx"]["market"] = effective_market
    record = record_from_saved_attempt(attempt)

    assert record["markets"] == expected_markets
    assert record["replay_metadata"]["declared_markets"] == ["ZA", "KE"]
    assert blind_test.context_for(record).market == expected_context_market


@pytest.mark.parametrize("corruption", [
    "run_id", "window", "as_of", "market", "evidence_id", "query_hash", "hash_index",
])
def test_saved_attempt_rejects_inconsistent_capture(corruption):
    from core.eval.record_export import record_from_saved_attempt

    attempt = make_saved_attempt(make_context())
    if corruption == "run_id":
        attempt["ctx"]["run_id"] = "different-run"
    elif corruption == "window":
        attempt["ctx"]["window_end"] = "2026-09-29"
    elif corruption == "as_of":
        attempt["ctx"]["as_of"] = "2026-09-29T10:31:00+02:00"
    elif corruption == "market":
        attempt["markets"] = ["KE"]
    elif corruption == "evidence_id":
        first = next(iter(attempt["ctx"]["evidence"].values()))
        first["id"] = "different-evidence"
    elif corruption == "query_hash":
        attempt["ctx"]["queries"]["q_1"]["rows"][0]["posts"] = 42
    elif corruption == "hash_index":
        attempt["query_hashes"]["q_1"] = "sha256:wrong"

    with pytest.raises(ValueError):
        record_from_saved_attempt(attempt)


def test_serialized_socialcrawl_params_preserve_live_k2_gap_and_hashtag_pins(tmp_path):
    from core.eval.record_export import write_record

    ctx = make_context()
    ctx.sc_calls = [
        {"route": "reddit/search", "params": {"query": "7colours"}, "status": "ok",
         "backend_token": "backend-token-sentinel", "client_state": {"session": "client-state-sentinel"}},
        {"route": "tiktok/search/hashtag", "params": {"hashtag": "amapiano2026"}, "status": "ok",
         "auth_headers": {"authorization": "auth-header-sentinel"}},
    ]
    record = export_record(ctx, gaps=[])
    path = write_record(tmp_path, record)
    replay = blind_test.load_replay(path)[0]
    replay_ctx = blind_test.context_for(replay)
    gaps = [
        {"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"},
        {"what": "Posts under #amapiano2026 not split by city", "searched": "stored posts", "why": "no city"},
    ]

    live_draft = numbered_draft(ctx, record)
    live_draft["gaps"] = copy.deepcopy(gaps)
    live_answer, _ = check_answer(live_draft, ctx, blind_test.ReplayWarehouse(record),
                                  window=WINDOW, markets=["ZA"])
    replay_draft = numbered_draft(replay_ctx, replay)
    replay_draft["gaps"] = copy.deepcopy(gaps)
    replay_answer, _ = check_answer(replay_draft, replay_ctx, blind_test.ReplayWarehouse(replay),
                                    window=WINDOW, markets=["ZA"])

    assert all(gap in live_answer["gaps"] for gap in gaps)
    assert all(gap in replay_answer["gaps"] for gap in gaps)
    assert replay["gate_controls"]["sc_calls"] == [
        {"route": "reddit/search", "params": {"query": "7colours"}, "status": "ok"},
        {"route": "tiktok/search/hashtag", "params": {"hashtag": "amapiano2026"}, "status": "ok"},
    ]
    assert "backend-token-sentinel" not in repr(replay)
    assert "client-state-sentinel" not in repr(replay)
    assert "auth-header-sentinel" not in repr(replay)
    packed = blind_test._pack(replay)
    assert "7colours" not in packed and "amapiano2026" not in packed
