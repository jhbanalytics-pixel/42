import copy
import datetime as dt
import json
import logging
import re
import sys
import threading
import time
import types
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from core.api import agent_app
from core.config import caps as caps_config

# Arithmetic against the cap as it stood until 3 October 2026 (conftest.py).
pytestmark = pytest.mark.usefixtures("old_model_cap_schedule")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORD_KEYS = {"ask_id", "question", "parent_id", "market", "status", "created_at",
               "finished_at", "answer", "run", "steps", "error"}
RUN_KEYS = {"run_id", "tier", "mode", "credits", "tokens", "seconds", "model_usd", "window",
            "posts", "platforms", "source_status", "followups", "notices"}
ROW_KEYS = {"run_id", "stage", "run_date", "status", "started_at", "finished_at", "question",
            "tier", "credits", "seconds", "outcome", "answer", "record"}
ASK_ID = re.compile(r"^a_\d{8}_[0-9a-f]{8}$")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("F42_AGENT", "fixture")
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0.01")
    monkeypatch.delenv("F42_DATA", raising=False)
    monkeypatch.delenv("F42_PROJECT", raising=False)
    agent_app.SINK.clear()
    agent_app.ASKS.clear()
    yield
    # Let background asks from this test finish, so none writes SINK during the next.
    for held in list(agent_app.ASKS.values()):
        held.stop = True
        held.finished.wait(5)
    agent_app.SINK.clear()
    agent_app.ASKS.clear()


@pytest.fixture(autouse=True)
def nobody_hidden(monkeypatch):
    """Readers now ask who is hidden. These tests are about other things and their stores hold no suppression list;
    the readers' own behaviour is pinned in test_privacy_*.py."""
    from core.api import privacy
    monkeypatch.setattr(privacy, "read_hidden", lambda store: (set(), set(), set()))


@pytest.fixture
def client():
    return TestClient(agent_app.app)


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def parse_sse(text):
    events, comments = [], []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            comments.append(block)
            continue
        lines = block.split("\n")
        assert [line.split(":", 1)[0] for line in lines] == ["id", "event", "data"], block
        fields = dict(line.split(": ", 1) for line in lines)
        events.append({"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"])})
    return events, comments


def ask(client, question="What is behind #fixture in South Africa this week?", **body):
    return client.post("/api/ask", json={"question": question, "market": "ZA", **body})


def test_start_returns_202_then_events_end_in_done(client):
    r = ask(client)
    assert r.status_code == 202
    body = r.json()
    assert ASK_ID.match(body["ask_id"])
    assert body["status"] == "running"
    assert body["events_url"] == f"/api/ask/{body['ask_id']}/events"
    assert body["url"] == f"/api/ask/{body['ask_id']}"

    stream = client.get(body["events_url"])
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    events, _ = parse_sse(stream.text)
    assert [e["id"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["data"]["seq"] == e["id"] for e in events)
    assert events[-1]["event"] == "done"
    assert events[-1]["data"] == {"seq": events[-1]["id"], "status": "complete", "url": body["url"]}
    kinds = [e["event"] for e in events[:-1]]
    steps = [e["data"] for e in events if e["event"] == "step"]
    assert 4 <= len(steps) <= 6
    assert all(s["text"] and isinstance(s["count"], int) and s["at"] for s in steps)
    assert kinds.count("evidence") == 2
    claims = [e["data"] for e in events if e["event"] == "claim"]
    assert [c["check"] for c in claims] == ["checking", "verified"]
    assert all(e["data"]["at"] for e in events[:-1])

    record = client.get(body["url"]).json()
    assert record["status"] == "complete"
    assert record["steps"] == steps


def test_wait_true_returns_complete_record(client):
    r = ask(client, wait=True, tier="T1")
    assert r.status_code == 200
    record = r.json()
    assert set(record) == RECORD_KEYS
    assert ASK_ID.match(record["ask_id"])
    assert record["status"] == "complete"
    assert record["market"] == "ZA"
    assert record["parent_id"] is None
    assert record["error"] is None
    assert record["created_at"] and record["finished_at"]
    assert record["created_at"].endswith("+02:00")
    assert record["answer"] == load("ask_complete.json")["answer"]
    assert RUN_KEYS <= set(record["run"])
    assert len(record["run"]["followups"]) == 3
    assert 4 <= len(record["steps"]) <= 6


def test_source_market_and_market_assumed_survive_fixture_agent_readback(client, monkeypatch):
    fixture_agent = agent_app.fixture_agent

    def source_market_fixture(request, emit, should_stop):
        result = fixture_agent(request, emit, should_stop)
        evidence = result["answer"]["evidence"][0]
        evidence["source_market"] = "KE"
        evidence["flags"] = ["market_assumed"]
        return result

    monkeypatch.setattr(agent_app, "fixture_agent", source_market_fixture)
    finished = ask(client, wait=True).json()
    readback = client.get(f"/api/ask/{finished['ask_id']}").json()
    evidence = readback["answer"]["evidence"][0]

    assert readback["market"] == "ZA"
    assert evidence["market"] == "ZA"
    assert evidence["source_market"] == "KE"
    assert evidence["flags"] == ["market_assumed"]
    assert readback["answer"]["claims"][0]["label"] == finished["answer"]["claims"][0]["label"]


@pytest.mark.parametrize("kind", ["sounds", "hashtags"])
def test_code_owned_ranked_metadata_survives_fixture_agent_readback(client, monkeypatch, kind):
    from core.agent.ranked import ranked_list
    from core.agent.tests.test_ranked_list import QUESTION, fixture

    fixture_agent = agent_app.fixture_agent
    question = QUESTION if kind == "sounds" else "Which hashtags are rising on TikTok this week?"
    ctx, answer, _ = fixture(kind=kind)
    ranked, _ = ranked_list(question, answer, ctx)
    assert ranked is not None

    def with_ranking(request, emit, should_stop):
        result = fixture_agent(request, emit, should_stop)
        result["answer"] = answer
        result["run"]["run_id"] = ctx.run_id
        result["run"]["window"] = ranked["window"]
        result["run"]["ranked_list"] = ranked
        return result

    monkeypatch.setattr(agent_app, "fixture_agent", with_ranking)
    finished = ask(client, question=question, wait=True).json()
    readback = client.get(f"/api/ask/{finished['ask_id']}").json()
    assert readback["run"]["ranked_list"] == ranked
    assert readback["answer"] == finished["answer"] == answer
    assert "query_receipts" not in readback


def test_masked_skin_reader_omits_ranked_metadata_without_mutating_the_source(monkeypatch):
    record = load("ask_complete.json")
    record["skin_id"] = "skin_fixture"
    record["run"]["ranked_list"] = {"items": [{"usage_handle": "private_handle"}]}
    monkeypatch.setattr(agent_app, "skin_people", lambda *a: {"approved": [], "allowed": []})
    shown = agent_app.shown_record(record)
    assert "ranked_list" not in shown["run"]
    assert record["run"]["ranked_list"]["items"][0]["usage_handle"] == "private_handle"


def test_a_skin_record_without_run_metadata_keeps_its_existing_null_shape(monkeypatch):
    record = load("ask_complete.json")
    record["skin_id"] = "skin_fixture"
    record["run"] = None
    monkeypatch.setattr(agent_app, "skin_people", lambda *a: {"approved": [], "allowed": []})
    assert agent_app.shown_record(record)["run"] is None
    assert record["run"] is None


@pytest.mark.parametrize("kind", ["platforms", "topics", "creators"])
def test_entity_lists_survive_the_fixture_agent_reader(client, monkeypatch, kind):
    from core.agent.tests.test_remaining_lists import QUESTIONS, entity_lists, fixture

    ctx, answer, discovered, _ = fixture(kind)
    groups, _ = entity_lists(QUESTIONS[kind], answer, ctx, creator_discovery=discovered)
    assert groups
    fixture_agent = agent_app.fixture_agent

    def with_lists(request, emit, should_stop):
        result = fixture_agent(request, emit, should_stop)
        result["answer"] = answer
        result["run"].update(run_id=ctx.run_id, entity_lists=groups,
                             window={"from": ctx.window_start.isoformat(), "to": ctx.window_end.isoformat()})
        return result

    monkeypatch.setattr(agent_app, "fixture_agent", with_lists)
    finished = ask(client, question=QUESTIONS[kind], wait=True).json()
    readback = client.get(f"/api/ask/{finished['ask_id']}").json()
    assert readback["run"]["entity_lists"] == groups
    assert readback["answer"] == finished["answer"] == answer
    assert "query_receipts" not in readback


def test_masked_skin_reader_omits_entity_lists_without_mutating_stored_metadata(monkeypatch):
    record = load("ask_complete.json")
    record["skin_id"] = "skin_fixture"
    record["run"]["entity_lists"] = [{"items": [{"name": "private_name"}]}]
    monkeypatch.setattr(agent_app, "skin_people", lambda *a: {"approved": [], "allowed": []})
    shown = agent_app.shown_record(record)
    assert "entity_lists" not in shown["run"]
    assert record["run"]["entity_lists"][0]["items"][0]["name"] == "private_name"


@pytest.mark.parametrize("body", [
    {"question": "What is behind #fixture?", "tier": "T2"},
    {"question": "What is behind #fixture?", "tier": "T3"},
    {"question": "What is behind #fixture?", "tier": "T9"},
    {"question": "hi"},
    {"question": "x" * 2001},
    {"question": "What is behind #fixture?", "market": "GH"},
    {"question": "What is behind #fixture?", "mode": "cached"},
    {"question": "What is behind #fixture?", "wait": "yes"},
    {"market": "ZA"},
])
def test_bad_requests_give_400(client, body):
    r = client.post("/api/ask", json=body)
    assert r.status_code == 400
    assert set(r.json()) == {"error", "message"}
    assert r.json()["error"] == "bad_request"


def test_tier_t2_message_names_the_stage(client):
    r = client.post("/api/ask", json={"question": "What is behind #fixture?", "tier": "T2"})
    assert r.status_code == 400
    assert "T2" in r.json()["message"]


def test_followup_passes_parent(client, monkeypatch):
    seen = []

    def spy(request, emit, should_stop):
        seen.append(request)
        return agent_app.fixture_agent(request, emit, should_stop)

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: spy)
    first = ask(client, wait=True).json()
    second = ask(client, "And on X?", wait=True, parent_id=first["ask_id"]).json()
    assert second["parent_id"] == first["ask_id"]
    # The parent's window rides along so a follow-up that names none reads the same dates (visual QA A02).
    assert seen[1]["parent"] == {"question": first["question"], "answer": first["answer"],
                                 "window": first["run"]["window"]}
    assert seen[1]["parent"]["window"]["from"] and seen[1]["parent"]["window"]["to"]
    assert seen[1]["ask_id"] == second["ask_id"]
    assert "parent" not in seen[0]

    r = ask(client, "And on X?", parent_id="a_20260928_deadbeef")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_request_carries_body_and_defaults(client, monkeypatch):
    seen = []

    def spy(request, emit, should_stop):
        seen.append(request)
        return agent_app.fixture_agent(request, emit, should_stop)

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: spy)
    card = {"item_id": "abc", "market": "ZA", "date": "2026-09-28"}
    record = ask(client, wait=True, from_card=card, tier="T0").json()
    req = seen[0]
    assert req["ask_id"] == record["ask_id"]
    assert req["from_card"] == card
    assert req["tier"] == "T0"
    assert req["mode"] == "live"
    assert req["market"] == "ZA"


def test_stop_gives_stopped(client, monkeypatch):
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0.2")
    body = ask(client).json()
    r = client.post(f"/api/ask/{body['ask_id']}/stop")
    assert r.status_code == 202
    assert r.json() == {"ask_id": body["ask_id"], "status": "stopping"}
    events, _ = parse_sse(client.get(body["events_url"]).text)
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["status"] == "stopped"
    record = client.get(body["url"]).json()
    assert record["status"] == "stopped"
    assert record["answer"] is not None
    assert record["finished_at"]
    assert len(record["steps"]) < 4


def test_stop_unknown_ask_gives_404(client):
    r = client.post("/api/ask/a_20260928_deadbeef/stop")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_fail_gives_failed_with_error(client):
    r = ask(client, "Why does this fail for #fixture?", wait=True)
    assert r.status_code == 200
    record = r.json()
    assert record["status"] == "failed"
    assert record["answer"] is None
    assert record["error"] == load("ask_failed.json")["error"]
    assert "Traceback" not in record["error"]["message"]
    assert record["finished_at"]
    events, _ = parse_sse(client.get(f"/api/ask/{record['ask_id']}/events").text)
    assert events[-1]["data"]["status"] == "failed"


def test_thin_question_gives_partial_answer(client):
    record = ask(client, "Is there anything thin on #fixture in Kenya?", wait=True).json()
    assert record["status"] == "complete"
    assert record["answer"] == load("ask_partial.json")["answer"]
    assert record["answer"]["status"] == "partial"
    assert record["answer"]["gaps"]


def test_last_event_id_resume_skips_earlier_events(client):
    record = ask(client, wait=True).json()
    url = f"/api/ask/{record['ask_id']}/events"
    full, _ = parse_sse(client.get(url).text)
    resumed, _ = parse_sse(client.get(url, headers={"Last-Event-ID": "3"}).text)
    assert resumed[0]["id"] == 4
    assert resumed == full[3:]
    assert resumed[-1]["event"] == "done"


def test_keepalive_is_sent_while_waiting(client, monkeypatch):
    monkeypatch.setattr(agent_app, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0.1")
    body = ask(client).json()
    events, comments = parse_sse(client.get(body["events_url"]).text)
    assert ": keepalive" in comments
    assert events[-1]["event"] == "done"


def test_unknown_ask_gives_404(client):
    for path in ("/api/ask/a_20260928_deadbeef", "/api/ask/a_20260928_deadbeef/events"):
        r = client.get(path)
        assert r.status_code == 404
        assert r.json() == {"error": "not_found", "message": r.json()["message"]}


def test_sink_receives_one_row_per_finished_ask(client):
    first = ask(client, wait=True).json()
    second = ask(client, "Why does this fail for #fixture?", wait=True).json()
    assert len(agent_app.SINK) == 2
    rows = {row["status"]: row for row in agent_app.SINK}
    assert set(rows) == {"complete", "failed"}
    for row in agent_app.SINK:
        assert set(row) == ROW_KEYS
        assert row["stage"] == "ask"
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", row["run_date"])
    ok = rows["complete"]
    assert json.loads(ok["answer"]) == first["answer"]
    assert json.loads(ok["record"]) == first
    assert ok["run_id"] == first["run"]["run_id"]
    assert ok["outcome"] == "complete"
    assert ok["question"] == first["question"]
    assert json.loads(rows["failed"]["record"]) == second
    assert rows["failed"]["answer"] is None
    assert rows["failed"]["outcome"] == "internal"


def test_bigquery_sink_calls_insert_rows_json(client, monkeypatch):
    monkeypatch.setenv("F42_DATA", "bigquery")
    monkeypatch.setenv("F42_PROJECT", "test-project")
    fake = mock.MagicMock()
    fake.return_value.insert_rows_json.return_value = []
    monkeypatch.setattr("google.cloud.bigquery.Client", fake)
    record = ask(client, wait=True).json()
    fake.assert_called_once_with(project="test-project")
    fake.return_value.insert_rows_json.assert_called_once()
    table, rows = fake.return_value.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.runs"
    assert len(rows) == 1
    assert set(rows[0]) == ROW_KEYS
    assert rows[0]["stage"] == "ask"
    assert json.loads(rows[0]["record"]) == record
    assert agent_app.SINK == []


# A finished ask's model spend stays on today's cap until its runs row is written (F002): the wrapper releases it only
# once the insert is confirmed, and an ask whose row is not written says so rather than reading as a plain completion.
NOT_RECORDED = "could not be recorded"


def harnessed(monkeypatch, insert):
    """A real run_ask on the core.agent test fakes, run through execute with the runs insert doing `insert`."""
    from core.agent import ask as agent_ask
    from core.agent.tests.test_ask import Harness

    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.setenv("F42_DATA", "bigquery")
    bq = mock.MagicMock()
    bq.insert_rows_json.side_effect = insert
    monkeypatch.setattr(agent_app, "bigquery_client", lambda: bq)
    h = Harness(spent=lambda: 0.0)
    held = agent_app.Ask({"ask_id": "a_20260928_test", "question": "What is behind amapiano in South Africa this week?",
                          "tier": "T1", "mode": "live", "market": None, "parent_id": None})
    agent_app.execute(held, lambda request, emit, should_stop: agent_ask.run_ask(request, emit, should_stop,
                                                                                deps=h.deps))
    return held, bq


def next_tier():
    """The tier the next ask gets on a spend read that leaves out the first ask: one T1 hold fits only without it."""
    from core.agent.tests.test_ask import Harness, current_model_cap, hold

    return Harness(spent=lambda: current_model_cap() - hold() - 0.001).run(ask_id="a_20260928_next")["run"]["tier"]


def insert_errors(table, rows):
    return [{"index": 0, "errors": ["no such field"]}]


def insert_raises(table, rows):
    raise ConnectionError("streaming insert dropped")


def test_a_written_runs_row_releases_the_asks_unrecorded_spend(monkeypatch):
    held, bq = harnessed(monkeypatch, lambda table, rows: [])
    record = held.snapshot()
    assert record["status"] == "complete" and bq.insert_rows_json.call_count == 1
    assert not any(NOT_RECORDED in n for n in record["run"]["notices"])
    assert next_tier() == "T1"


@pytest.mark.parametrize("insert", [insert_errors, insert_raises], ids=["row_errors", "raises"])
def test_a_runs_row_not_written_keeps_the_spend_on_the_cap_and_says_so(monkeypatch, insert):
    held, bq = harnessed(monkeypatch, insert)
    record = held.snapshot()
    assert bq.insert_rows_json.call_count == 1
    assert any(NOT_RECORDED in n for n in record["run"]["notices"])
    assert held.done["status"] == record["status"]
    assert next_tier() == "T0"


# The queries behind an answer's numbers (purpose, sql, params, result_hash, row_count, rows) go on the stored runs
# row so a reviewer can check a number (contract section 7). They are never in an Ask body, an event, or a record read
# back for a viewer: their rows can hold names the app hides.
RECEIPTS = {"q_1": {"purpose": "posts per creator", "sql": "SELECT p.creator_id, COUNT(*) AS n FROM posts p",
                    "params": {"term": "amapiano"}, "result_hash": "sha256:" + "0" * 64, "row_count": 2,
                    "rows": [{"creator_id": "x:hiddenperson", "n": 4}, {"creator_id": "x:other", "n": 2}]}}


def receipts_agent(receipts):
    def agent(request, emit, should_stop):
        record = load("ask_complete.json")
        emit({"event": "step", "kind": "plan", "text": "Planning", "count": 0})
        return {"answer": record["answer"], "run": {**record["run"], "run_id": "r_" + request["ask_id"][2:]},
                "query_receipts": copy.deepcopy(receipts)}
    return agent


def test_query_receipts_go_on_the_runs_row_and_never_in_what_a_viewer_reads(client, monkeypatch, tmp_path):
    from core.api import store
    use_agent(monkeypatch, receipts_agent(RECEIPTS))
    body = ask(client, wait=True)
    record = body.json()
    ask_id = record["ask_id"]
    reads = [body, client.get(f"/api/ask/{ask_id}"), client.get(f"/api/ask/{ask_id}/events")]
    (row,) = agent_app.SINK
    stored = json.loads(row["record"])
    assert stored["query_receipts"] == RECEIPTS
    assert {k: v for k, v in stored.items() if k != "query_receipts"} == record
    assert "query_receipts" not in json.loads(row["answer"])

    agent_app.ASKS.clear()  # past the hour: the record is read back from the runs rows
    local = agent_app.investigation_record(ask_id)
    (tmp_path / "runs.json").write_text(json.dumps([{**row, "record": stored}]), encoding="utf-8")
    fixture_store = store.FixtureStore(tmp_path).ask_record(ask_id)

    class Job:
        def result(self):
            return [{"record": json.dumps(stored)}]

    class Client:
        def query(self, sql, job_config=None):
            return Job()

    bq_store = store.BigQueryStore(project="p", client=Client()).ask_record(ask_id)
    for read in (local, fixture_store, bq_store):
        assert read == record
    for text in [r.text for r in reads] + [json.dumps(r) for r in (local, fixture_store, bq_store)]:
        assert "query_receipts" not in text and "hiddenperson" not in text


def test_bigquery_runs_row_carries_the_query_receipts(client, monkeypatch):
    monkeypatch.setenv("F42_DATA", "bigquery")
    fake = mock.MagicMock()
    fake.return_value.insert_rows_json.return_value = []
    monkeypatch.setattr("google.cloud.bigquery.Client", fake)
    use_agent(monkeypatch, receipts_agent(RECEIPTS))
    record = ask(client, wait=True).json()
    (row,) = fake.return_value.insert_rows_json.call_args.args[1]
    assert set(row) == ROW_KEYS
    assert json.loads(row["record"]) == {**record, "query_receipts": RECEIPTS}


def test_large_query_receipts_drop_rows_before_the_runs_row_is_lost(client, monkeypatch):
    # BigQuery refuses a streamed row over 10 MB, and the whole row with it. Receipts keep at most RECEIPT_ROWS rows
    # each and their share of the row; past it the largest queries lose their rows first, then the receipts go.
    wide = {f"q_{i}": {"purpose": f"query {i}", "sql": f"SELECT {i}", "params": {}, "result_hash": f"sha256:{i}",
                       "row_count": 80, "rows": [{"text": "x" * (2_000 * i)} for _ in range(80)]} for i in (1, 5, 9)}
    use_agent(monkeypatch, receipts_agent(wide))
    record = ask(client, wait=True).json()
    (row,) = agent_app.SINK
    assert len(json.dumps(row)) <= agent_app.ROW_BYTES
    kept = json.loads(row["record"])["query_receipts"]
    assert len(json.dumps(json.dumps(kept))) <= agent_app.RECEIPT_BYTES
    assert set(kept) == set(wide)
    assert all(len(r["rows"]) <= agent_app.RECEIPT_ROWS for r in kept.values())
    assert kept["q_9"]["rows"] == [] and kept["q_9"]["rows_dropped"] == "size"
    assert kept["q_1"]["rows"] == wide["q_1"]["rows"][:agent_app.RECEIPT_ROWS] and "rows_dropped" not in kept["q_1"]
    for query_id, receipt in kept.items():
        assert {k: receipt[k] for k in ("purpose", "sql", "params", "result_hash", "row_count")} == {
            k: wide[query_id][k] for k in ("purpose", "sql", "params", "result_hash", "row_count")}

    agent_app.SINK.clear()
    huge = {"q_1": {**RECEIPTS["q_1"], "sql": "SELECT 1 -- " + "y" * agent_app.RECEIPT_BYTES}}
    use_agent(monkeypatch, receipts_agent(huge))
    record = ask(client, wait=True).json()
    (row,) = agent_app.SINK
    stored = json.loads(row["record"])
    assert stored["query_receipts"] is None
    assert {k: v for k, v in stored.items() if k != "query_receipts"} == record and record["status"] == "complete"


def test_receipts_that_are_not_plain_json_never_cost_the_runs_row(client, monkeypatch):
    odd = {"q_1": {**RECEIPTS["q_1"], "params": {"day": dt.date(2026, 10, 2)}, "rows": [{"at": dt.datetime(2026, 10, 2)}]}}
    use_agent(monkeypatch, receipts_agent(odd))
    record = ask(client, wait=True).json()
    (row,) = agent_app.SINK
    kept = json.loads(row["record"])["query_receipts"]["q_1"]
    assert kept["params"] == {"day": "2026-10-02"} and kept["rows"] == [{"at": "2026-10-02 00:00:00"}]
    assert record["status"] == "complete"


def test_resolve_uses_core_agent_when_present(monkeypatch):
    monkeypatch.delenv("F42_AGENT", raising=False)
    fake = types.ModuleType("core.agent")
    fake.run_ask = lambda request, emit, should_stop: {"answer": None, "run": None}
    monkeypatch.setitem(sys.modules, "core.agent", fake)
    # lane L3's tests import the real core.agent.ask first in a full run
    monkeypatch.delitem(sys.modules, "core.agent.ask", raising=False)
    assert agent_app.resolve_run_ask() is fake.run_ask


def test_resolve_prefers_core_agent_ask_module(monkeypatch):
    monkeypatch.delenv("F42_AGENT", raising=False)
    package = types.ModuleType("core.agent")
    package.__path__ = []
    package.run_ask = lambda request, emit, should_stop: {"answer": None, "run": None}
    module = types.ModuleType("core.agent.ask")
    module.run_ask = lambda request, emit, should_stop: {"answer": None, "run": None}
    monkeypatch.setitem(sys.modules, "core.agent", package)
    monkeypatch.setitem(sys.modules, "core.agent.ask", module)
    assert agent_app.resolve_run_ask() is module.run_ask


def test_resolve_gives_none_when_agent_missing(monkeypatch):
    monkeypatch.delenv("F42_AGENT", raising=False)
    monkeypatch.setitem(sys.modules, "core.agent", None)
    monkeypatch.setitem(sys.modules, "core.agent.ask", None)
    assert agent_app.resolve_run_ask() is None
    monkeypatch.setenv("F42_AGENT", "fixture")
    assert agent_app.resolve_run_ask() is agent_app.fixture_agent


def test_missing_agent_gives_503_and_health_says_missing(client, monkeypatch):
    monkeypatch.delenv("F42_AGENT", raising=False)
    monkeypatch.setitem(sys.modules, "core.agent", None)
    monkeypatch.setitem(sys.modules, "core.agent.ask", None)
    r = ask(client)
    assert r.status_code == 503
    assert r.json() == {"error": "agent_unavailable",
                        "message": "The Ask agent is not installed on this service."}
    assert agent_app.ASKS == {}
    assert client.get("/health").json()["agent"] == "missing"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["service"] == "f42-agent"
    assert r.json()["agent"] == "fixture"


def spy_on_requests(monkeypatch):
    seen = []

    def spy(request, emit, should_stop):
        seen.append(request)
        return agent_app.fixture_agent(request, emit, should_stop)

    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: spy)
    return seen


def drain(client, ask_id):
    events, _ = parse_sse(client.get(f"/api/ask/{ask_id}/events").text)
    assert events[-1]["event"] == "done"


def test_finished_asks_past_max_are_evicted_oldest_first(client, monkeypatch):
    monkeypatch.setattr(agent_app, "MAX_FINISHED", 3)
    ids = [ask(client, wait=True).json()["ask_id"] for _ in range(5)]
    assert len(agent_app.ASKS) == 3
    assert [client.get(f"/api/ask/{i}").status_code for i in ids] == [404, 404, 200, 200, 200]


def test_finished_asks_older_than_ttl_are_evicted(client, monkeypatch):
    monkeypatch.setattr(agent_app, "FINISHED_TTL_S", 3600)
    old = ask(client, wait=True).json()["ask_id"]
    agent_app.ASKS[old].ended -= 3601
    new = ask(client, wait=True).json()["ask_id"]
    assert client.get(f"/api/ask/{old}").status_code == 404
    assert client.get(f"/api/ask/{new}").status_code == 200


def test_running_asks_are_never_evicted(client, monkeypatch):
    monkeypatch.setattr(agent_app, "MAX_FINISHED", 0)
    monkeypatch.setattr(agent_app, "FINISHED_TTL_S", 0)
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0")
    held = "What keeps running in South Africa this week?"

    def agent(request, emit, should_stop):
        # The held ask runs until it is asked to stop, however long the two asks after it take, rather than for a
        # fixed delay a slow machine could outlast; the deadline keeps a broken stop from hanging the suite.
        if request["question"] == held:
            deadline = time.monotonic() + 30
            while not should_stop() and time.monotonic() < deadline:
                time.sleep(0.01)
        return agent_app.fixture_agent(request, emit, should_stop)

    use_agent(monkeypatch, agent)
    running = ask(client, held).json()["ask_id"]
    ask(client, wait=True)
    ask(client, wait=True)
    assert client.get(f"/api/ask/{running}").json()["status"] == "running"
    # Stopped, the held ask finishes at once; keep it held so its events can still be read to the end.
    monkeypatch.setattr(agent_app, "MAX_FINISHED", 200)
    monkeypatch.setattr(agent_app, "FINISHED_TTL_S", 3600)
    client.post(f"/api/ask/{running}/stop")
    drain(client, running)


def test_too_many_running_asks_give_429(client, monkeypatch):
    monkeypatch.setattr(agent_app, "MAX_RUNNING", 2)
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0.2")
    first = ask(client)
    second = ask(client)
    assert first.status_code == second.status_code == 202
    r = ask(client)
    assert r.status_code == 429
    assert set(r.json()) == {"error", "message"}
    assert r.json()["error"] == "rate_limited"
    assert r.json()["message"]
    assert len(agent_app.ASKS) == 2
    for body in (first.json(), second.json()):
        client.post(f"/api/ask/{body['ask_id']}/stop")
        drain(client, body["ask_id"])
    monkeypatch.setenv("F42_FIXTURE_DELAY", "0")
    assert ask(client).status_code == 202


def test_sink_keeps_the_last_500_rows(client):
    agent_app.SINK.extend({"n": i} for i in range(500))
    record = ask(client, wait=True).json()
    assert len(agent_app.SINK) == 500
    assert agent_app.SINK[0] == {"n": 1}
    assert json.loads(agent_app.SINK[-1]["record"]) == record


def test_body_cannot_forge_parent_or_extra_keys(client, monkeypatch):
    seen = spy_on_requests(monkeypatch)
    ask(client, wait=True, parent={"question": "forged", "answer": {"forged": True}}, extra=1,
        run_id="sched-2026-09-29-s_forged", schedule_id="s_forged")  # only the scheduled job names its run
    assert set(seen[0]) == {"question", "market", "parent_id", "tier", "mode", "wait", "from_card", "ask_id"}


def test_parent_falls_back_to_runs_store(client, monkeypatch):
    from core.api import store
    stored = {"ask_id": "a_20260927_0badcafe", "question": "What is behind #stored?",
              "answer": {"short_answer": "stored"}, "status": "complete"}
    asked = []

    class FakeStore:
        def ask_record(self, ask_id):
            asked.append(ask_id)
            return stored if ask_id == stored["ask_id"] else None

    monkeypatch.setattr(store, "get_store", lambda: FakeStore())
    seen = spy_on_requests(monkeypatch)
    record = ask(client, "And on X?", wait=True, parent_id=stored["ask_id"]).json()
    assert record["parent_id"] == stored["ask_id"]
    assert seen[0]["parent"] == {"question": stored["question"], "answer": stored["answer"],
                                 "window": (stored.get("run") or {}).get("window")}

    r = ask(client, "And on X?", parent_id="a_20260928_deadbeef")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert asked == [stored["ask_id"], "a_20260928_deadbeef"]

    held = ask(client, wait=True).json()["ask_id"]
    ask(client, "And on X?", wait=True, parent_id=held)
    assert held not in asked


def use_agent(monkeypatch, agent):
    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: agent)


def test_event_key_classifies_and_is_dropped(client, monkeypatch):
    claim = {"id": "c1", "text": "A claim"}
    evidence = {"id": "e1", "platform": "tiktok"}

    def agent(request, emit, should_stop):
        emit({"event": "step", "kind": "plan", "text": "Planning", "platform": None, "count": 0})
        emit({"event": "evidence", "evidence": evidence})
        emit({"event": "claim", "claim": claim, "check": "verified", "reason": None})
        emit({"event": "step", "kind": "note", "text": "Reading the claim again", "claim": claim, "count": 1})
        emit({"kind": "write", "text": "Writing", "count": 2})
        return {"answer": None, "run": {"run_id": "r_x"}}

    use_agent(monkeypatch, agent)
    record = ask(client, wait=True).json()
    events, _ = parse_sse(client.get(f"/api/ask/{record['ask_id']}/events").text)
    assert [e["event"] for e in events] == ["step", "evidence", "claim", "step", "step", "done"]
    assert [e["id"] for e in events] == [1, 2, 3, 4, 5, 6]
    assert all("event" not in e["data"] for e in events)
    assert events[2]["data"]["claim"] == claim and events[2]["data"]["check"] == "verified"
    assert events[1]["data"]["evidence"] == evidence
    assert [s["text"] for s in record["steps"]] == ["Planning", "Reading the claim again", "Writing"]
    assert all("event" not in s for s in record["steps"])


class PartialRunError(RuntimeError):
    def __init__(self, message, run):
        super().__init__(message)
        self.run = run


def test_failure_after_model_calls_keeps_the_partial_run(client, monkeypatch):
    partial = {"run_id": "r_partial", "tier": "T1", "mode": "live", "model_usd": 0.42,
               "tokens": {"input": 1200, "output": 300}, "credits": 7, "seconds": 12.5}

    def agent(request, emit, should_stop):
        emit({"event": "step", "kind": "plan", "text": "Planning", "count": 0})
        raise PartialRunError("model gave up", dict(partial))

    use_agent(monkeypatch, agent)
    record = ask(client, wait=True).json()
    assert record["status"] == "failed"
    assert record["answer"] is None
    assert record["error"] == {"error": "internal",
                               "message": "The agent hit an error and could not finish (PartialRunError)."}
    assert record["run"] == partial
    assert len(agent_app.SINK) == 1
    row = agent_app.SINK[0]
    assert set(row) == ROW_KEYS
    assert row["status"] == "failed"
    assert row["run_id"] == "r_partial"
    assert row["credits"] == 7
    assert row["seconds"] == 12.5
    assert row["tier"] == "T1"
    assert row["answer"] is None
    assert row["outcome"] == "internal"
    stored = json.loads(row["record"])
    assert stored == record
    assert stored["run"]["model_usd"] == 0.42


def test_plain_failure_keeps_run_null(client, monkeypatch):
    def agent(request, emit, should_stop):
        raise ValueError("no run here")

    use_agent(monkeypatch, agent)
    record = ask(client, wait=True).json()
    assert record["status"] == "failed"
    assert record["run"] is None
    assert json.loads(agent_app.SINK[0]["record"])["run"] is None
    assert agent_app.SINK[0]["run_id"] == record["ask_id"]


# Every daily cap counts the Johannesburg day: the spend reads (spent_today, ask.model_spend_today) filter runs on
# run_date = the SAST date. An ask's created_at is on its market's clock, so its run_date must still be the SAST day,
# or a Kenyan ask after 23:00 SAST, or a Nigerian one before 01:00 SAST, lands on a day nobody is reading.
@pytest.mark.parametrize("market, at, day", [
    ("KE", dt.datetime(2026, 10, 2, 23, 30), "2026-10-02"),  # 00:30 on 3 October in Nairobi
    ("NG", dt.datetime(2026, 10, 3, 0, 30), "2026-10-03"),   # 23:30 on 2 October in Lagos
    ("ZA", dt.datetime(2026, 10, 2, 23, 59, 59), "2026-10-02"),
])
def test_an_ask_row_is_dated_on_the_sast_day_the_spend_read_counts(client, monkeypatch, market, at, day):
    sast = dt.timezone(dt.timedelta(hours=2))

    def app_now(m=None):
        zone = dt.timezone(dt.timedelta(hours=agent_app.OFFSETS.get(m, 2)))
        return at.replace(tzinfo=sast).astimezone(zone)

    def agent(request, emit, should_stop):
        return {"answer": None, "run": {"run_id": "r_late", "tier": "T1", "model_usd": 1.5, "credits": 4}}

    monkeypatch.setattr(agent_app, "now", app_now)
    use_agent(monkeypatch, agent)
    record = client.post("/api/ask", json={"question": "What is new in this market?", "market": market,
                                           "wait": True}).json()
    (row,) = agent_app.SINK
    assert row["run_date"] == day
    assert row["started_at"] == record["created_at"] == app_now(market).isoformat()  # still the market's clock
    assert agent_app.spent_today() == {"credits": 4, "model_usd": 1.5}


def test_non_dict_run_attribute_is_ignored(client, monkeypatch):
    def agent(request, emit, should_stop):
        raise PartialRunError("odd", "not a run")

    use_agent(monkeypatch, agent)
    record = ask(client, wait=True).json()
    assert record["status"] == "failed"
    assert record["run"] is None


@pytest.mark.parametrize("name", ["ask_complete.json", "ask_partial.json", "ask_failed.json"])
def test_fixture_records_follow_contract_and_rubric(name):
    record = load(name)
    assert set(record) == RECORD_KEYS
    assert ASK_ID.match(record["ask_id"])
    assert 4 <= len(record["steps"]) <= 6 or record["status"] == "failed"
    lowered = json.dumps(record).lower()
    for word in ("gen z", "genz", "millennial", "boomer", "gen alpha", "youth", "young", "teen",
                 "years old", "age group", "google trends", "google_trends", "\u2014", "\u2013"):
        assert word not in lowered, word
    if record["status"] == "failed":
        assert record["answer"] is None and record["run"] is None
        assert record["error"]["error"] == "internal"
        return
    answer, run = record["answer"], record["run"]
    assert set(answer) >= {"status", "as_of", "short_answer", "claims", "evidence", "so_what",
                           "watch_next", "gaps", "context"}
    assert RUN_KEYS <= set(run) and len(run["followups"]) == 3
    evidence = {e["id"]: e for e in answer["evidence"]}
    assert len(evidence) >= 2
    for e in evidence.values():
        assert e["platform"] in {"tiktok", "instagram", "youtube", "reddit", "x", "facebook"}
        assert e["handle"].startswith("@fixture_") and e["url"].startswith("https://example.invalid/")
        assert {"posted_at", "market", "text", "thumbnail_url", "creator_tier"} <= set(e)
        # Media fields are optional (contract section 4) and never null (L3's answer contract).
        if "duration_s" in e:
            assert isinstance(e["duration_s"], (int, float)) and e["duration_s"] >= 0
        if "transcript_span" in e:
            assert {"start_s", "end_s", "text"} <= set(e["transcript_span"])
        assert run["window"]["from"] <= e["posted_at"][:10] <= run["window"]["to"]
    for claim in answer["claims"]:
        assert claim["label"] in {"observed", "corroborated", "single_source", "inferred"}
        assert claim["kind"] in {"observation", "interpretation", "recommendation", "proposal"}
        assert claim["evidence_ids"] and all(i in evidence for i in claim["evidence_ids"])
        for quote in claim.get("quotes", []):
            assert quote["text"] in evidence[quote["evidence_id"]]["text"]
        for number in claim.get("numbers", []):
            assert number["query_id"] and number["run_id"]
            assert re.match(r"^sha256:[0-9a-f]{64}$", number["result_hash"])
    claim_ids = {c["id"] for c in answer["claims"]}
    for item in answer["so_what"] + answer["watch_next"]:
        assert set(item["claim_ids"]) <= claim_ids


def test_failed_run_keeps_only_contract_keys_and_survives_bad_values(client, monkeypatch):
    import threading as _threading

    partial = {"run_id": "r_x", "model_usd": 0.5, "tokens": {"input": 10, "output": 2}, "credits": 3,
               "error": "secret-ish exception text", "client": _threading.Lock()}

    def agent(request, emit, should_stop):
        raise PartialRunError("boom", partial)

    use_agent(monkeypatch, agent)
    record = ask(client, wait=True).json()
    assert record["status"] == "failed"
    assert record["run"] == {"run_id": "r_x", "model_usd": 0.5, "tokens": {"input": 10, "output": 2}, "credits": 3}


def test_oversized_failed_run_falls_back_to_spend_only(client, monkeypatch):
    partial = {"model_usd": 0.25, "credits": 4, "tokens": {"input": 1, "output": 1},
               "source_status": [{"platform": "x" * 100, "items": i} for i in range(5000)]}

    def agent(request, emit, should_stop):
        raise PartialRunError("boom", partial)

    use_agent(monkeypatch, agent)
    record = ask(client, wait=True).json()
    assert record["run"] == {"model_usd": 0.25, "credits": 4, "tokens": {"input": 1, "output": 1}}


def test_agent_cannot_set_seq_or_at(client, monkeypatch):
    def agent(request, emit, should_stop):
        emit({"event": "step", "seq": 99, "at": "never", "type": "claim", "kind": "plan", "text": "a", "count": 0})
        emit({"event": "step", "kind": "plan", "text": "b", "count": 0})
        return {"answer": None, "run": None}

    use_agent(monkeypatch, agent)
    record = ask(client, wait=True).json()
    assert [s["seq"] for s in record["steps"]] == [1, 2]
    assert record["steps"][0]["at"] != "never"
    assert "type" not in record["steps"][0]


WATCH_KEYS = {"watch_id", "created_at", "status_at", "who", "target", "market", "rule", "label", "status"}
WATCH_ID = re.compile(r"^w_[0-9a-f]{12}$")


@pytest.fixture
def held():
    agent_app.WATCHES.clear()
    agent_app.FEEDBACK.clear()
    yield
    agent_app.WATCHES.clear()
    agent_app.FEEDBACK.clear()


def new_watch(client, **body):
    payload = {"target": {"kind": "hashtag", "value": "#amapiano"}, "market": "ZA",
               "rule": {"state_in": ["rising"]}, "label": "Amapiano in South Africa", **body}
    return client.post("/api/watches", json=payload)


def test_create_watch_appends_a_row_and_returns_201(client, held):
    r = new_watch(client)
    assert r.status_code == 201
    watch = r.json()
    assert set(watch) == WATCH_KEYS
    assert WATCH_ID.match(watch["watch_id"])
    assert watch["who"] == "passcode"
    assert watch["status"] == "active"
    assert watch["target"] == {"kind": "hashtag", "value": "#amapiano"}
    assert watch["rule"] == {"state_in": ["rising"]}
    assert watch["market"] == "ZA"
    assert watch["label"] == "Amapiano in South Africa"
    assert re.search(r"[+-]\d{2}:\d{2}$", watch["created_at"])
    assert watch["status_at"] == watch["created_at"]
    assert len(agent_app.WATCHES) == 1
    row = agent_app.WATCHES[0]
    assert set(row) == WATCH_KEYS
    assert json.loads(row["target"]) == watch["target"]
    assert json.loads(row["rule"]) == watch["rule"]


@pytest.mark.parametrize("target, rule, market", [
    ({"kind": "item", "item_id": "abc"}, {"ratio_over": 2.5}, "all"),
    ({"kind": "sound", "value": "water dance"}, {"reach_over": 50}, "NG"),
    ({"kind": "creator", "value": "@someone"}, {"creator_surge": True}, "KE"),
    ({"kind": "brand", "value": "Nando's"}, {"tone_flip": True}, "ZA"),
    ({"kind": "query", "value": "load shedding memes"}, {"breakout": True}, "all"),
    ({"kind": "item", "item_id": "abc"}, {"state_in": ["emerging", "rising", "new_to_42"]}, "ZA"),
])
def test_every_target_kind_and_rule_is_accepted(client, held, target, rule, market):
    r = new_watch(client, target=target, rule=rule, market=market)
    assert r.status_code == 201, r.json()
    assert r.json()["target"] == target and r.json()["rule"] == rule


@pytest.mark.parametrize("body", [
    {"target": {"kind": "topic", "value": "x"}},
    {"target": {"kind": "item"}},
    {"target": {"kind": "item", "item_id": ""}},
    {"target": {"kind": "hashtag"}},
    {"target": {"kind": "hashtag", "value": "   "}},
    {"target": "amapiano"},
    {"market": "GH"},
    {"market": None},
    {"rule": {}},
    {"rule": {"state_in": ["rising"], "ratio_over": 2}},
    {"rule": {"state_in": []}},
    {"rule": {"state_in": ["hot"]}},
    {"rule": {"state_in": "rising"}},
    {"rule": {"ratio_over": 0}},
    {"rule": {"ratio_over": -1}},
    {"rule": {"ratio_over": True}},
    {"rule": {"ratio_over": "2"}},
    {"rule": {"reach_over": 2.5}},
    {"rule": {"reach_over": 0}},
    {"rule": {"breakout": False}},
    {"rule": {"tone_flip": 1}},
    {"rule": {"unknown": True}},
    {"label": "x" * 121},
    {"label": 5},
])
def test_bad_watches_give_400(client, held, body):
    r = new_watch(client, **body)
    assert r.status_code == 400
    assert set(r.json()) == {"error", "message"}
    assert r.json()["error"] == "bad_request"
    assert agent_app.WATCHES == []


def test_watch_body_must_be_json_object(client, held):
    assert client.post("/api/watches", content=b"not json").status_code == 400
    assert client.post("/api/watches", json=[1, 2]).json()["error"] == "bad_request"


def test_missing_label_falls_back_to_the_target_within_120(client, held):
    body = new_watch(client).json()
    assert body["label"] == "Amapiano in South Africa"
    payload = {"target": {"kind": "query", "value": "y" * 200}, "market": "all", "rule": {"breakout": True}}
    assert client.post("/api/watches", json=payload).json()["label"] == "y" * 120
    payload["target"] = {"kind": "item", "item_id": "abc"}
    assert client.post("/api/watches", json=payload).json()["label"] == "abc"


def test_watch_keeps_only_contract_target_keys(client, held):
    r = new_watch(client, target={"kind": "hashtag", "value": "#amapiano", "extra": 1}, who="someone")
    assert r.json()["target"] == {"kind": "hashtag", "value": "#amapiano"}
    assert r.json()["who"] == "passcode"


def test_pause_and_resume_append_rows_and_list_shows_latest(client, held):
    first = new_watch(client).json()
    second = new_watch(client, label="Second").json()
    r = client.post(f"/api/watches/{first['watch_id']}/pause")
    assert r.status_code == 200
    assert r.json()["status"] == "paused"
    assert r.json()["watch_id"] == first["watch_id"]
    assert r.json()["target"] == first["target"] and r.json()["label"] == first["label"]
    assert len(agent_app.WATCHES) == 3

    listed = client.get("/api/watches").json()["watches"]
    assert {w["watch_id"]: w["status"] for w in listed} == {first["watch_id"]: "paused",
                                                            second["watch_id"]: "active"}
    assert all(set(w) == WATCH_KEYS and isinstance(w["rule"], dict) for w in listed)

    r = client.post(f"/api/watches/{first['watch_id']}/resume")
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert len(agent_app.WATCHES) == 4
    assert [row["status"] for row in agent_app.WATCHES if row["watch_id"] == first["watch_id"]] == [
        "active", "paused", "active"]
    listed = client.get("/api/watches").json()["watches"]
    assert {w["status"] for w in listed} == {"active"}


def test_pause_and_resume_keep_created_at_and_stamp_status_at(client, held, monkeypatch):
    stamps = iter(["2026-10-01T09:00:00+02:00", "2026-10-02T10:30:00+02:00"])
    first = new_watch(client).json()
    monkeypatch.setattr(agent_app, "status_time", lambda market: next(stamps))
    paused = client.post(f"/api/watches/{first['watch_id']}/pause").json()
    assert paused["created_at"] == first["created_at"]
    assert paused["status_at"] == "2026-10-01T09:00:00+02:00"
    resumed = client.post(f"/api/watches/{first['watch_id']}/resume").json()
    assert resumed["created_at"] == first["created_at"]
    assert resumed["status_at"] == "2026-10-02T10:30:00+02:00"
    assert [(row["created_at"], row["status_at"]) for row in agent_app.WATCHES] == [
        (first["created_at"], first["created_at"]),
        (first["created_at"], "2026-10-01T09:00:00+02:00"),
        (first["created_at"], "2026-10-02T10:30:00+02:00")]
    (listed,) = client.get("/api/watches").json()["watches"]
    assert listed["created_at"] == first["created_at"] and listed["status_at"] == "2026-10-02T10:30:00+02:00"


def test_pause_twice_appends_once(client, held):
    watch = new_watch(client).json()
    client.post(f"/api/watches/{watch['watch_id']}/pause")
    r = client.post(f"/api/watches/{watch['watch_id']}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert len(agent_app.WATCHES) == 2


def test_pause_or_resume_unknown_watch_gives_404(client, held):
    for action in ("pause", "resume"):
        r = client.post(f"/api/watches/w_000000000000/{action}")
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"
    assert agent_app.WATCHES == []


def test_watch_rows_are_capped_like_the_sink(client, held, monkeypatch):
    monkeypatch.setattr(agent_app, "MAX_SINK", 3)
    for _ in range(5):
        new_watch(client)
    assert len(agent_app.WATCHES) == 3


def fake_bigquery(monkeypatch, rows=()):
    monkeypatch.setenv("F42_DATA", "bigquery")
    monkeypatch.setenv("F42_PROJECT", "test-project")
    fake = mock.MagicMock()
    fake.return_value.insert_rows_json.return_value = []
    fake.return_value.query.return_value.result.return_value = list(rows)
    monkeypatch.setattr("google.cloud.bigquery.Client", fake)
    return fake.return_value


def test_bigquery_watch_insert(client, held, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    watch = new_watch(client).json()
    table, rows = client_bq.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.watches"
    assert len(rows) == 1 and set(rows[0]) == WATCH_KEYS
    assert rows[0]["watch_id"] == watch["watch_id"]
    assert json.loads(rows[0]["target"]) == watch["target"]
    assert agent_app.WATCHES == []


def test_bigquery_insert_errors_give_500(client, held, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    client_bq.insert_rows_json.return_value = [{"index": 0, "errors": ["no such field"]}]
    r = new_watch(client)
    assert r.status_code == 500
    assert r.json()["error"] == "internal"
    assert "no such field" not in r.json()["message"]


def stored_row(watch_id="w_0123456789ab", status="active", as_json=True, status_at=None):
    target = {"kind": "item", "item_id": "abc"}
    rule = {"ratio_over": 3}
    return {"watch_id": watch_id, "created_at": agent_app.now(), "status_at": status_at, "who": "passcode",
            "target": target if as_json else json.dumps(target), "market": "ZA",
            "rule": rule if as_json else json.dumps(rule), "label": "Stored", "status": status}


def test_bigquery_list_reads_latest_rows(client, held, monkeypatch):
    client_bq = fake_bigquery(monkeypatch, [stored_row(), stored_row("w_ba9876543210", "paused", as_json=False,
                                                                    status_at=agent_app.now())])
    listed = client.get("/api/watches").json()["watches"]
    assert [w["watch_id"] for w in listed] == ["w_0123456789ab", "w_ba9876543210"]
    assert all(w["target"] == {"kind": "item", "item_id": "abc"} and w["rule"] == {"ratio_over": 3}
               for w in listed)
    assert all(isinstance(w["created_at"], str) for w in listed)
    assert listed[0]["status_at"] is None and isinstance(listed[1]["status_at"], str)
    sql = client_bq.query.call_args.args[0]
    assert "test-project.intelligence_42_agent.watches" in sql
    assert "status_at" in sql.split("FROM")[0]
    # The same tie-break as L1's v_watches_current (core/schema/agent.sql on full-42-l1).
    assert ("PARTITION BY w.watch_id ORDER BY COALESCE(w.status_at, w.created_at) DESC NULLS LAST, "
            "w.status DESC, TO_JSON_STRING(w)) = 1") in sql
    assert sql.rstrip().endswith("ORDER BY COALESCE(status_at, created_at) DESC")


def test_bigquery_pause_reads_the_table_then_appends(client, held, monkeypatch):
    stored = stored_row()
    stored["created_at"] = stored["created_at"].replace(year=2026, month=9, day=20)
    client_bq = fake_bigquery(monkeypatch, [dict(stored)])
    r = client.post("/api/watches/w_0123456789ab/pause")
    assert r.status_code == 200
    assert r.json()["status"] == "paused" and r.json()["label"] == "Stored"
    table, rows = client_bq.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.watches"
    assert rows[0]["status"] == "paused" and rows[0]["watch_id"] == "w_0123456789ab"
    assert json.loads(rows[0]["target"]) == {"kind": "item", "item_id": "abc"}
    assert rows[0]["created_at"] == r.json()["created_at"] == stored["created_at"].isoformat()
    assert rows[0]["status_at"] == r.json()["status_at"] and rows[0]["status_at"] != rows[0]["created_at"]


def test_bigquery_missing_watches_table_lists_nothing(client, held, monkeypatch):
    from google.api_core.exceptions import NotFound

    client_bq = fake_bigquery(monkeypatch)
    client_bq.query.side_effect = NotFound("Not found: Table test-project:intelligence_42_agent.watches")
    r = client.get("/api/watches")
    assert r.status_code == 200 and r.json() == {"watches": []}
    assert client.post("/api/watches/w_0123456789ab/pause").status_code == 404
    client_bq.insert_rows_json.assert_not_called()


def test_bigquery_pause_unknown_gives_404(client, held, monkeypatch):
    client_bq = fake_bigquery(monkeypatch, [])
    assert client.post("/api/watches/w_0123456789ab/pause").status_code == 404
    client_bq.insert_rows_json.assert_not_called()


FEEDBACK_KEYS = {"who", "what", "reason", "at"}


@pytest.mark.parametrize("target", [
    {"kind": "card", "item_id": "abc", "market": "ZA", "date": "2026-10-20"},
    {"kind": "claim", "answer_id": "a_20261020_0badcafe", "claim_id": "c1"},
    {"kind": "claim", "brief_id": "2026-10-20_ZA", "claim_id": "c2"},
])
def test_feedback_appends_a_row_and_returns_202(client, held, target):
    r = client.post("/api/feedback", json={"target": target, "value": "not_real", "reason": "A campaign"})
    assert r.status_code == 202
    assert len(agent_app.FEEDBACK) == 1
    row = agent_app.FEEDBACK[0]
    assert set(row) == FEEDBACK_KEYS
    assert row["who"] == "passcode"
    assert json.loads(row["what"]) == {"target": target, "value": "not_real"}
    assert row["reason"] == "A campaign"
    assert re.search(r"[+-]\d{2}:\d{2}$", row["at"])


def test_feedback_reason_is_optional(client, held):
    target = {"kind": "card", "item_id": "abc", "market": "NG", "date": "2026-10-20"}
    r = client.post("/api/feedback", json={"target": target, "value": "useful"})
    assert r.status_code == 202
    assert agent_app.FEEDBACK[0]["reason"] is None


@pytest.mark.parametrize("body", [
    {"target": {"kind": "card", "item_id": "abc", "market": "ZA", "date": "2026-10-20"}, "value": "great"},
    {"target": {"kind": "card", "item_id": "abc", "market": "GH", "date": "2026-10-20"}, "value": "real"},
    {"target": {"kind": "card", "item_id": "abc", "market": "ZA", "date": "20 Oct"}, "value": "real"},
    {"target": {"kind": "card", "market": "ZA", "date": "2026-10-20"}, "value": "real"},
    {"target": {"kind": "claim", "claim_id": "c1"}, "value": "wrong"},
    {"target": {"kind": "claim", "answer_id": "a", "brief_id": "b", "claim_id": "c1"}, "value": "wrong"},
    {"target": {"kind": "claim", "answer_id": "a"}, "value": "wrong"},
    {"target": {"kind": "post", "item_id": "abc"}, "value": "real"},
    {"target": {"kind": "card", "item_id": "abc", "market": "ZA", "date": "2026-10-20"}, "value": "real",
     "reason": 5},
    {"target": {"kind": "card", "item_id": "abc", "market": "ZA", "date": "2026-10-20"}, "value": "real",
     "reason": "x" * 1001},
    {"value": "real"},
])
def test_bad_feedback_gives_400(client, held, body):
    r = client.post("/api/feedback", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"
    assert agent_app.FEEDBACK == []


def test_bigquery_feedback_insert(client, held, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    target = {"kind": "card", "item_id": "abc", "market": "ZA", "date": "2026-10-20"}
    r = client.post("/api/feedback", json={"target": target, "value": "real"})
    assert r.status_code == 202
    table, rows = client_bq.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.feedback"
    assert set(rows[0]) == FEEDBACK_KEYS
    assert agent_app.FEEDBACK == []


# Spike asks (contract section 14.1) and scheduled questions (section 14.2).

SPIKE = {"item_id": "abc", "market": "ZA", "date": "2026-09-28", "series": "feed_tiktok"}


def test_spike_reaches_run_ask_as_sent(client, monkeypatch):
    seen = spy_on_requests(monkeypatch)
    ask(client, wait=True, tier="T1", from_card={k: SPIKE[k] for k in ("item_id", "market", "date")}, spike=SPIKE)
    assert seen[0]["spike"] == SPIKE and seen[0]["tier"] == "T1"


def test_a_spike_ask_record_carries_its_spike_on_the_read_and_the_runs_row(client):
    ask_id = ask(client, tier="T1", spike=SPIKE).json()["ask_id"]
    agent_app.ASKS[ask_id].finished.wait(5)
    record = client.get(f"/api/ask/{ask_id}").json()
    assert set(record) == RECORD_KEYS | {"spike"} and record["spike"] == SPIKE
    (row,) = agent_app.SINK
    assert json.loads(row["record"])["spike"] == SPIKE


def test_an_ask_without_a_spike_has_no_spike_on_its_record(client):
    assert "spike" not in ask(client, wait=True, spike=None).json()


@pytest.mark.parametrize("spike", ["abc", [1], 5])
def test_spike_must_be_an_object(client, spike):
    r = ask(client, spike=spike)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


@pytest.mark.parametrize("spike, words", [
    ({**SPIKE, "note": "x" * 50_000}, "spike takes only item_id, market, date and series, not note."),
    ({**SPIKE, "market": "GH"}, "spike market must be ZA, NG or KE."),
    ({**SPIKE, "date": "2026-02-30"}, "spike date must be a real date written YYYY-MM-DD."),
    ({**SPIKE, "date": "28 September"}, "spike date must be a real date written YYYY-MM-DD."),
    ({**SPIKE, "series": "s" * 81}, "spike series must be 1 to 80 letters, digits or _, or left out."),
    ({**SPIKE, "item_id": "bad id!"}, "spike item_id must be an item id."),
    ({"market": "ZA", "date": "2026-09-28"}, "spike needs item_id, market and date."),
])
def test_a_bad_spike_gives_400_in_plain_words_and_starts_nothing(client, spike, words):
    r = ask(client, tier="T1", spike=spike)
    assert r.status_code == 400
    assert r.json() == {"error": "bad_request", "message": words}
    assert agent_app.ASKS == {} and agent_app.SINK == []


@pytest.mark.parametrize("spike", [SPIKE, {k: SPIKE[k] for k in ("item_id", "market", "date")},
                                   {**SPIKE, "series": None}, {**SPIKE, "series": "s" * 80}])
def test_a_good_spike_is_accepted_and_stored_as_sent(client, spike):
    r = ask(client, tier="T1", spike=spike)
    assert r.status_code == 202, r.text
    ask_id = r.json()["ask_id"]
    agent_app.ASKS[ask_id].finished.wait(5)
    assert client.get(f"/api/ask/{ask_id}").json()["spike"] == spike
    (row,) = agent_app.SINK
    assert json.loads(row["record"])["spike"] == spike


SCHEDULE_KEYS = {"schedule_id", "created_at", "status_at", "who", "question", "market", "tier", "cadence", "deliver",
                 "status"}
SCHEDULE_ID = re.compile(r"^s_[0-9a-f]{12}$")


@pytest.fixture
def sched():
    agent_app.SCHEDULES.clear()
    yield
    agent_app.SCHEDULES.clear()


def new_schedule(client, **body):
    payload = {"question": "What is behind #amapiano in South Africa this week?", "market": "ZA", "tier": "T1",
               "cadence": "weekly_monday", "deliver": ["in_app", "channel"], **body}
    return client.post("/api/schedules", json=payload)


def test_create_schedule_appends_a_row_and_returns_201(client, sched):
    r = new_schedule(client)
    assert r.status_code == 201, r.text
    s = r.json()
    assert set(s) == SCHEDULE_KEYS
    assert SCHEDULE_ID.match(s["schedule_id"])
    assert s["who"] == "passcode" and s["status"] == "active"
    assert s["question"] == "What is behind #amapiano in South Africa this week?"
    assert (s["market"], s["tier"], s["cadence"]) == ("ZA", "T1", "weekly_monday")
    assert s["deliver"] == ["in_app", "channel"]
    assert re.search(r"[+-]\d{2}:\d{2}$", s["created_at"]) and s["status_at"] == s["created_at"]
    (row,) = agent_app.SCHEDULES
    assert set(row) == SCHEDULE_KEYS and json.loads(row["deliver"]) == ["in_app", "channel"]


@pytest.mark.parametrize("body", [
    {"question": "   "}, {"question": "ab"}, {"question": "x" * 501}, {"question": 5}, {"question": None},
    {"market": "all"}, {"market": None}, {"market": "GH"},
    {"tier": "T2"}, {"tier": "T3"}, {"tier": None}, {"tier": "t1"},
    {"cadence": "weekly"}, {"cadence": None}, {"cadence": "monthly"},
    {"deliver": []}, {"deliver": ["email"]}, {"deliver": "in_app"}, {"deliver": ["in_app", 5]}, {"deliver": None},
])
def test_bad_schedules_give_400(client, sched, body):
    r = new_schedule(client, **body)
    assert r.status_code == 400
    assert set(r.json()) == {"error", "message"} and r.json()["error"] == "bad_request"
    assert agent_app.SCHEDULES == []


def test_schedule_body_must_be_json_object(client, sched):
    assert client.post("/api/schedules", content=b"not json").status_code == 400
    assert client.post("/api/schedules", json=[1]).json()["error"] == "bad_request"


def test_schedule_keeps_only_its_own_keys_and_one_of_each_channel(client, sched):
    r = new_schedule(client, deliver=["channel", "in_app", "channel"], who="someone", status="paused", extra=1)
    s = r.json()
    assert set(s) == SCHEDULE_KEYS and s["who"] == "passcode" and s["status"] == "active"
    assert s["deliver"] == ["channel", "in_app"]
    assert new_schedule(client, question="  What is new in Kenya?  ", market="KE", tier="T0",
                        cadence="daily", deliver=["in_app"]).json()["question"] == "What is new in Kenya?"


def test_pause_and_resume_schedule_append_rows_and_list_shows_latest(client, sched, monkeypatch):
    first = new_schedule(client).json()
    second = new_schedule(client, cadence="daily").json()
    monkeypatch.setattr(agent_app, "status_time", lambda market: "2026-10-01T09:00:00+02:00")
    r = client.post(f"/api/schedules/{first['schedule_id']}/pause")
    assert r.status_code == 200
    paused = r.json()
    assert paused["status"] == "paused" and paused["created_at"] == first["created_at"]
    assert paused["status_at"] == "2026-10-01T09:00:00+02:00" and paused["question"] == first["question"]
    assert len(agent_app.SCHEDULES) == 3
    listed = client.get("/api/schedules").json()["schedules"]
    assert {s["schedule_id"]: s["status"] for s in listed} == {first["schedule_id"]: "paused",
                                                               second["schedule_id"]: "active"}
    assert all(isinstance(s["deliver"], list) for s in listed)
    assert client.post(f"/api/schedules/{first['schedule_id']}/pause").status_code == 200
    assert len(agent_app.SCHEDULES) == 3  # pausing twice appends once
    r = client.post(f"/api/schedules/{first['schedule_id']}/resume")
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert [row["status"] for row in agent_app.SCHEDULES if row["schedule_id"] == first["schedule_id"]] == [
        "active", "paused", "active"]


def test_pause_or_resume_unknown_schedule_gives_404(client, sched):
    for action in ("pause", "resume"):
        r = client.post(f"/api/schedules/s_000000000000/{action}")
        assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert agent_app.SCHEDULES == []


def sched_run(run_id, stage, status, outcome, finished_at, ask_id=None):
    record = {"ask_id": ask_id, "schedule_id": run_id[17:]} if ask_id else {"schedule_id": run_id[17:],
                                                                           "reason": outcome}
    return {"run_id": run_id, "stage": stage, "run_date": run_id[6:16], "status": status, "started_at": finished_at,
            "finished_at": finished_at, "question": "q", "tier": "T1", "credits": 0, "seconds": 1,
            "outcome": outcome, "answer": None, "record": json.dumps(record)}


def test_schedule_list_shows_last_run_and_skip_reason(client, sched):
    a = new_schedule(client).json()["schedule_id"]
    b = new_schedule(client, cadence="daily").json()["schedule_id"]
    c = new_schedule(client, cadence="daily").json()["schedule_id"]
    agent_app.SINK.extend([
        sched_run(f"sched-2026-09-28-{a}", "ask", "complete", "complete", "2026-09-28T07:02:00+02:00",
                  "a_20260928_0000aaaa"),
        sched_run(f"sched-2026-09-29-{a}", "scheduled", "skipped", "Scheduled share spent",
                  "2026-09-29T07:01:00+02:00"),
        sched_run(f"sched-2026-09-29-{b}", "ask", "complete", "insufficient_evidence", "2026-09-29T07:03:00+02:00",
                  "a_20260929_0000bbbb"),
        sched_run("r_20260929_user", "ask", "complete", "complete", "2026-09-29T08:00:00+02:00", "a_user"),
    ])
    listed = {s["schedule_id"]: s for s in client.get("/api/schedules").json()["schedules"]}
    assert listed[a]["skip_reason"] == "Scheduled share spent"
    assert listed[a]["last_run"] == {"run_id": f"sched-2026-09-29-{a}", "date": "2026-09-29", "status": "skipped",
                                     "ask_id": None, "outcome": "Scheduled share spent"}
    assert listed[b]["skip_reason"] is None
    assert listed[b]["last_run"] == {"run_id": f"sched-2026-09-29-{b}", "date": "2026-09-29", "status": "complete",
                                     "ask_id": "a_20260929_0000bbbb", "outcome": "insufficient_evidence"}
    assert listed[c]["last_run"] is None and listed[c]["skip_reason"] is None


def stored_schedule(schedule_id="s_0123456789ab", status="active", status_at=None):
    return {"schedule_id": schedule_id, "created_at": agent_app.now(), "status_at": status_at, "who": "passcode",
            "question": "What is behind #stored?", "market": "ZA", "tier": "T0", "cadence": "daily",
            "deliver": json.dumps(["in_app"]), "status": status}


def fake_tables(client_bq, schedules=(), runs=()):
    """Answer the schedules query and the runs query each with their own rows."""
    def query(sql, job_config=None):
        result = mock.MagicMock()
        result.result.return_value = list(schedules if "intelligence_42_agent.schedules" in sql else runs)
        return result

    client_bq.query.side_effect = query


def test_bigquery_schedule_insert(client, sched, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    s = new_schedule(client).json()
    table, rows = client_bq.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.schedules"
    assert set(rows[0]) == SCHEDULE_KEYS and rows[0]["schedule_id"] == s["schedule_id"]
    assert json.loads(rows[0]["deliver"]) == ["in_app", "channel"]
    assert agent_app.SCHEDULES == []


def test_bigquery_schedule_insert_errors_give_500(client, sched, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    client_bq.insert_rows_json.return_value = [{"index": 0, "errors": ["no such field"]}]
    r = new_schedule(client)
    assert r.status_code == 500 and r.json()["error"] == "internal"


def test_bigquery_schedule_list_reads_latest_rows_and_runs(client, sched, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    sid = "s_0123456789ab"
    fake_tables(client_bq, [stored_schedule(), stored_schedule("s_ba9876543210", "paused", agent_app.now())],
                [{"run_id": f"sched-2026-09-29-{sid}", "stage": "scheduled", "run_date": "2026-09-29",
                  "status": "skipped", "outcome": "Model budget spent", "finished_at": agent_app.now(),
                  "ask_id": None}])
    listed = client.get("/api/schedules").json()["schedules"]
    assert [s["schedule_id"] for s in listed] == [sid, "s_ba9876543210"]
    assert listed[0]["deliver"] == ["in_app"] and isinstance(listed[0]["created_at"], str)
    assert listed[0]["skip_reason"] == "Model budget spent" and listed[1]["last_run"] is None
    sqls = [c.args[0] for c in client_bq.query.call_args_list]
    sql = next(s for s in sqls if "intelligence_42_agent.schedules" in s)
    assert "test-project.intelligence_42_agent.schedules" in sql
    # The same order as the watches and L1's v_watches_current.
    assert ("PARTITION BY s.schedule_id ORDER BY COALESCE(s.status_at, s.created_at) DESC NULLS LAST, "
            "s.status DESC, TO_JSON_STRING(s)) = 1") in sql
    assert sql.rstrip().endswith("ORDER BY COALESCE(status_at, created_at) DESC")
    runs_sql = next(s for s in sqls if "intelligence_42_agent.runs" in s)
    assert "STARTS_WITH(r.run_id, 'sched-')" in runs_sql
    for call in client_bq.query.call_args_list:
        assert call.kwargs["job_config"].maximum_bytes_billed == 2 * 1024 ** 3


def test_bigquery_pause_schedule_reads_the_table_then_appends(client, sched, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    stored = stored_schedule()
    fake_tables(client_bq, [stored])
    r = client.post("/api/schedules/s_0123456789ab/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused"
    table, rows = client_bq.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.schedules"
    assert rows[0]["status"] == "paused" and rows[0]["created_at"] == stored["created_at"].isoformat()
    assert json.loads(rows[0]["deliver"]) == ["in_app"]


def test_bigquery_missing_schedules_table_lists_nothing(client, sched, monkeypatch):
    from google.api_core.exceptions import NotFound

    client_bq = fake_bigquery(monkeypatch)
    client_bq.query.side_effect = NotFound("Not found: Table test-project:intelligence_42_agent.schedules")
    r = client.get("/api/schedules")
    assert r.status_code == 200 and r.json() == {"schedules": []}
    assert client.post("/api/schedules/s_0123456789ab/pause").status_code == 404
    client_bq.insert_rows_json.assert_not_called()


# Dossiers (contract section 13.2).

DOSSIER_ROW_KEYS = {"dossier_id", "version", "created_at", "who", "state", "body", "source_ask_id", "content_hash"}
REVIEW_ROW_KEYS = {"dossier_id", "claim_id", "ticked", "note", "who", "at"}
DOSSIER_ID = re.compile(r"^d_[0-9a-f]{12}$")


def dossier_source():
    from core.api.tests.test_dossiers import source
    return source()


@pytest.fixture
def dossier_store(monkeypatch):
    """The source Ask record, read back from the runs store as f42-agent does once the ask is evicted."""
    from core.api import store
    held = {"record": dossier_source(), "asked": []}

    class FakeStore:
        def ask_record(self, ask_id):
            held["asked"].append(ask_id)
            return copy.deepcopy(held["record"]) if ask_id == held["record"]["ask_id"] else None

    monkeypatch.setattr(store, "get_store", lambda: FakeStore())
    agent_app.DOSSIER_VERSIONS.clear()
    agent_app.DOSSIER_REVIEWS.clear()
    yield held
    agent_app.DOSSIER_VERSIONS.clear()
    agent_app.DOSSIER_REVIEWS.clear()


def new_dossier(client, held, **body):
    return client.post("/api/dossiers", json={"from": {"ask_id": held["record"]["ask_id"]}, **body})


def tick(client, dossier_id, claim_id, ticked=True, note=None):
    return client.post(f"/api/dossiers/{dossier_id}/ticks", json={"claim_id": claim_id, "ticked": ticked, "note": note})


def test_create_dossier_from_a_stored_ask(client, dossier_store):
    r = new_dossier(client, dossier_store, title="Pot dance")
    assert r.status_code == 201
    body = r.json()
    assert DOSSIER_ID.match(body["dossier_id"])
    assert body["version"] == 1 and body["state"] == "draft" and body["title"] == "Pot dance"
    assert [c["claim_id"] for c in body["claims"]] == ["c1", "c2", "c3"]
    assert "CUT CLAIM TEXT" not in json.dumps(body)
    assert body["ticks"] == {}
    assert [n["claim_id"] for n in body["needs_tick"]] == ["c2", "c3"]
    assert body["content_hash"].startswith("sha256:")
    (row,) = agent_app.DOSSIER_VERSIONS
    assert set(row) == DOSSIER_ROW_KEYS
    assert row["state"] == "draft" and row["version"] == 1 and row["who"] == "passcode"
    assert row["source_ask_id"] == dossier_store["record"]["ask_id"]
    assert json.loads(row["body"])["claims"] == body["claims"]
    assert row["content_hash"] == body["content_hash"]


def test_create_dossier_ignores_claims_sent_by_the_client(client, dossier_store):
    r = new_dossier(client, dossier_store, claims=[{"id": "c1", "text": "FORGED", "label": "observed"}],
                    keep=["c1"], summary="FORGED", evidence=[{"id": "fake"}])
    assert r.status_code == 201
    assert "FORGED" not in json.dumps(agent_app.DOSSIER_VERSIONS)
    assert [c["claim_id"] for c in r.json()["claims"] if c["kept"]] == ["c1", "c2", "c3"]


def test_create_dossier_from_a_held_ask(client, dossier_store):
    record = ask(client, wait=True).json()
    r = client.post("/api/dossiers", json={"from": {"ask_id": record["ask_id"]}})
    assert r.status_code == 201
    assert r.json()["source_ask_id"] == record["ask_id"]
    assert r.json()["title"] == record["question"]
    assert record["ask_id"] not in dossier_store["asked"]


@pytest.mark.parametrize("body, status", [
    ({"from": {"ask_id": "a_20260928_deadbeef"}}, 404),
    ({"from": {"investigation_id": "i_0123456789ab"}}, 404),
    ({"from": {}}, 400),
    ({"from": {"ask_id": "a", "investigation_id": "i"}}, 400),
    ({"from": "a_20260928_0000000a"}, 400),
    ({}, 400),
    ({"from": {"ask_id": "a_20260928_0000000a"}, "title": "x" * 201}, 400),
])
def test_bad_or_unknown_dossier_sources(client, dossier_store, body, status):
    r = client.post("/api/dossiers", json=body)
    assert r.status_code == status
    assert agent_app.DOSSIER_VERSIONS == []


def test_dossier_from_an_unfinished_ask_gives_409(client, dossier_store):
    dossier_store["record"]["status"] = "failed"
    dossier_store["record"]["answer"] = None
    r = new_dossier(client, dossier_store)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"


def test_put_appends_a_new_draft_rebuilt_from_the_source(client, dossier_store):
    first = new_dossier(client, dossier_store).json()
    r = client.put(f"/api/dossiers/{first['dossier_id']}",
                   json={"keep": ["c1", "c3"], "order": ["c3", "c1"], "title": "Pot dance",
                         "notes": {"c3": "One poster only"},
                         "claims": [{"claim_id": "c1", "text": "FORGED", "label": "observed", "kept": True}]})
    assert r.status_code == 200
    second = r.json()
    assert second["version"] == 2 and second["state"] == "draft"
    assert [(c["claim_id"], c["kept"]) for c in second["claims"]] == [("c3", True), ("c1", True), ("c2", False)]
    assert second["claims"][0]["note"] == "One poster only"
    assert "FORGED" not in json.dumps(agent_app.DOSSIER_VERSIONS)
    assert [row["version"] for row in agent_app.DOSSIER_VERSIONS] == [1, 2]
    assert client.get(f"/api/dossiers/{first['dossier_id']}/versions/1").json()["claims"] == first["claims"]
    assert client.put(f"/api/dossiers/{first['dossier_id']}", json={"keep": ["c9"]}).status_code == 400
    assert client.put("/api/dossiers/d_000000000000", json={"keep": ["c1"]}).status_code == 404


def test_ticks_append_rows_and_carry_across_versions(client, dossier_store):
    dossier_id = new_dossier(client, dossier_store).json()["dossier_id"]
    r = tick(client, dossier_id, "c2", note="Checked the X post")
    assert r.status_code == 201
    assert set(r.json()) == REVIEW_ROW_KEYS
    assert set(agent_app.DOSSIER_REVIEWS[0]) == REVIEW_ROW_KEYS
    client.put(f"/api/dossiers/{dossier_id}", json={"title": "Edited"})
    current = client.get(f"/api/dossiers/{dossier_id}").json()
    assert current["version"] == 2
    assert current["ticks"]["c2"]["ticked"] is True and current["ticks"]["c2"]["note"] == "Checked the X post"
    assert [n["claim_id"] for n in current["needs_tick"]] == ["c3"]
    tick(client, dossier_id, "c2", ticked=False)
    current = client.get(f"/api/dossiers/{dossier_id}").json()
    assert current["ticks"]["c2"]["ticked"] is False
    assert [n["claim_id"] for n in current["needs_tick"]] == ["c2", "c3"]
    assert len(agent_app.DOSSIER_REVIEWS) == 2


@pytest.mark.parametrize("body", [
    {"claim_id": "c9", "ticked": True},
    {"claim_id": "c4", "ticked": True},
    {"claim_id": "c1", "ticked": "yes"},
    {"claim_id": "c1"},
    {"claim_id": "c1", "ticked": True, "note": "x" * 1001},
])
def test_bad_ticks_give_400(client, dossier_store, body):
    dossier_id = new_dossier(client, dossier_store).json()["dossier_id"]
    r = client.post(f"/api/dossiers/{dossier_id}/ticks", json=body)
    assert r.status_code == 400
    assert agent_app.DOSSIER_REVIEWS == []


def test_tick_on_unknown_dossier_gives_404(client, dossier_store):
    assert tick(client, "d_000000000000", "c1").status_code == 404


def test_freeze_refused_without_ticks_on_single_source_and_inferred(client, dossier_store):
    dossier_id = new_dossier(client, dossier_store).json()["dossier_id"]
    r = client.post(f"/api/dossiers/{dossier_id}/freeze")
    assert r.status_code == 409
    assert r.json()["error"] == "not_ready"
    assert [c["claim_id"] for c in r.json()["claims"]] == ["c2", "c3"]
    assert "c2" in r.json()["message"] and "c3" in r.json()["message"]
    tick(client, dossier_id, "c2")
    r = client.post(f"/api/dossiers/{dossier_id}/freeze")
    assert r.status_code == 409 and [c["claim_id"] for c in r.json()["claims"]] == ["c3"]
    assert len(agent_app.DOSSIER_VERSIONS) == 1


def test_freeze_refused_when_the_source_no_longer_holds_a_quote_or_hash(client, dossier_store):
    dossier_id = new_dossier(client, dossier_store).json()["dossier_id"]
    tick(client, dossier_id, "c2")
    tick(client, dossier_id, "c3")
    evidence = dossier_store["record"]["answer"]["evidence"][1]
    original = evidence["text"]
    evidence["text"] = "Tried the #fixture thing with my cousins, everybody dances"
    r = client.post(f"/api/dossiers/{dossier_id}/freeze")
    assert r.status_code == 409
    assert sorted(c["claim_id"] for c in r.json()["claims"]) == ["c2", "c3"]
    evidence["text"] = original
    dossier_store["record"]["answer"]["claims"][0]["numbers"][0]["result_hash"] = "sha256:" + "f" * 64
    r = client.post(f"/api/dossiers/{dossier_id}/freeze")
    assert r.status_code == 409
    assert [c["claim_id"] for c in r.json()["claims"]] == ["c1"]
    assert "result_hash" in r.json()["claims"][0]["reason"]
    assert len(agent_app.DOSSIER_VERSIONS) == 1


def freeze_ready(client, held):
    dossier_id = new_dossier(client, held, title="Pot dance").json()["dossier_id"]
    tick(client, dossier_id, "c2")
    tick(client, dossier_id, "c3", note="One poster, flagged in the note")
    return dossier_id


def test_freeze_appends_a_frozen_version_that_never_changes(client, dossier_store):
    dossier_id = freeze_ready(client, dossier_store)
    r = client.post(f"/api/dossiers/{dossier_id}/freeze")
    assert r.status_code == 201
    frozen = r.json()
    assert frozen["version"] == 2 and frozen["state"] == "frozen" and frozen["frozen_from"] == 1
    assert frozen["reviews"]["c3"] == {"ticked": True, "note": "One poster, flagged in the note",
                                       "at": frozen["reviews"]["c3"]["at"]}
    again = client.post(f"/api/dossiers/{dossier_id}/freeze")
    assert again.status_code == 200 and again.json()["version"] == 2
    assert len(agent_app.DOSSIER_VERSIONS) == 2

    r = client.put(f"/api/dossiers/{dossier_id}", json={"keep": ["c1"]})
    assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["state"] == "draft"
    stored = client.get(f"/api/dossiers/{dossier_id}/versions/2").json()
    assert stored["state"] == "frozen"
    assert [c["claim_id"] for c in stored["claims"] if c["kept"]] == ["c1", "c2", "c3"]
    assert stored["content_hash"] == frozen["content_hash"]
    assert [row["state"] for row in agent_app.DOSSIER_VERSIONS] == ["draft", "frozen", "draft"]
    assert client.get(f"/api/dossiers/{dossier_id}").json()["version"] == 3
    assert client.get(f"/api/dossiers/{dossier_id}/versions/9").status_code == 404
    assert client.get("/api/dossiers/d_000000000000").status_code == 404


STALE = "This dossier changed since you opened it. Reload to see the latest version."


def test_edit_again_from_a_frozen_version_another_tab_has_moved_past_gives_409_and_appends_nothing(
        client, dossier_store):
    dossier_id = freeze_ready(client, dossier_store)
    assert client.post(f"/api/dossiers/{dossier_id}/freeze", json={"from_version": 1}).status_code == 201
    # Tab B makes draft 3 from frozen 2; tab A still shows frozen 2 and presses Edit again.
    b = client.put(f"/api/dossiers/{dossier_id}", json={"keep": ["c1"], "from_version": 2})
    assert b.status_code == 200 and b.json()["version"] == 3
    a = client.put(f"/api/dossiers/{dossier_id}", json={"title": "Tab A", "from_version": 2})
    assert a.status_code == 409
    assert a.json() == {"error": "stale_version", "message": STALE}
    assert [row["version"] for row in agent_app.DOSSIER_VERSIONS] == [1, 2, 3]
    latest = client.get(f"/api/dossiers/{dossier_id}").json()
    assert latest["version"] == 3 and latest["title"] == "Pot dance"
    assert [c["claim_id"] for c in latest["claims"] if c["kept"]] == ["c1"]
    ok = client.put(f"/api/dossiers/{dossier_id}", json={"title": "Tab A", "from_version": 3})
    assert ok.status_code == 200 and ok.json()["version"] == 4


def test_a_freeze_from_a_draft_another_tab_has_edited_gives_409_and_appends_nothing(client, dossier_store):
    dossier_id = freeze_ready(client, dossier_store)
    assert client.put(f"/api/dossiers/{dossier_id}", json={"title": "Tab B", "from_version": 1}).status_code == 200
    r = client.post(f"/api/dossiers/{dossier_id}/freeze", json={"from_version": 1})
    assert r.status_code == 409
    assert r.json() == {"error": "stale_version", "message": STALE}
    assert [row["state"] for row in agent_app.DOSSIER_VERSIONS] == ["draft", "draft"]
    r = client.post(f"/api/dossiers/{dossier_id}/freeze", json={"from_version": 2})
    assert r.status_code == 201 and r.json()["version"] == 3 and r.json()["title"] == "Tab B"


def test_a_stale_freeze_of_an_already_frozen_dossier_gives_409(client, dossier_store):
    dossier_id = freeze_ready(client, dossier_store)
    client.post(f"/api/dossiers/{dossier_id}/freeze")
    client.put(f"/api/dossiers/{dossier_id}", json={"keep": ["c1"]})
    r = client.post(f"/api/dossiers/{dossier_id}/freeze", json={"from_version": 2})
    assert r.status_code == 409 and r.json()["error"] == "stale_version"
    assert len(agent_app.DOSSIER_VERSIONS) == 3


@pytest.mark.parametrize("given", ["2", 0, True])
def test_a_bad_from_version_gives_400_and_appends_nothing(client, dossier_store, given):
    dossier_id = new_dossier(client, dossier_store).json()["dossier_id"]
    assert client.put(f"/api/dossiers/{dossier_id}", json={"title": "x", "from_version": given}).status_code == 400
    assert client.post(f"/api/dossiers/{dossier_id}/freeze", json={"from_version": given}).status_code == 400
    assert len(agent_app.DOSSIER_VERSIONS) == 1


def test_a_null_from_version_is_an_older_client_and_keeps_todays_behaviour(client, dossier_store):
    dossier_id = new_dossier(client, dossier_store).json()["dossier_id"]
    assert client.put(f"/api/dossiers/{dossier_id}", json={"title": "x", "from_version": None}).status_code == 200
    assert client.put(f"/api/dossiers/{dossier_id}", json={"title": "y"}).status_code == 200
    assert [row["version"] for row in agent_app.DOSSIER_VERSIONS] == [1, 2, 3]


def test_a_freeze_body_that_is_not_json_gives_400(client, dossier_store):
    dossier_id = freeze_ready(client, dossier_store)
    r = client.post(f"/api/dossiers/{dossier_id}/freeze", content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert len(agent_app.DOSSIER_VERSIONS) == 1


def test_freeze_refuses_a_draft_with_no_kept_claims(client, dossier_store):
    dossier_id = new_dossier(client, dossier_store).json()["dossier_id"]
    client.put(f"/api/dossiers/{dossier_id}", json={"keep": []})
    r = client.post(f"/api/dossiers/{dossier_id}/freeze")
    assert r.status_code == 409


def test_dossier_list_newest_first_with_limit_and_before(client, dossier_store):
    ids = [new_dossier(client, dossier_store, title=f"Dossier {n}").json()["dossier_id"] for n in range(3)]
    client.put(f"/api/dossiers/{ids[0]}", json={"title": "Dossier 0 edited"})
    listed = client.get("/api/dossiers").json()["dossiers"]
    assert [d["dossier_id"] for d in listed] == [ids[0], ids[2], ids[1]]
    assert listed[0]["version"] == 2 and listed[0]["title"] == "Dossier 0 edited"
    assert {"dossier_id", "version", "state", "title", "created_at", "source_ask_id", "question", "market",
            "kept", "frozen_version"} <= set(listed[0])
    assert listed[0]["kept"] == 3 and listed[0]["frozen_version"] is None
    page = client.get("/api/dossiers", params={"limit": 2}).json()
    assert [d["dossier_id"] for d in page["dossiers"]] == [ids[0], ids[2]]
    assert page["next_before"] == page["dossiers"][-1]["created_at"]
    rest = client.get("/api/dossiers", params={"limit": 2, "before": page["next_before"]}).json()["dossiers"]
    assert [d["dossier_id"] for d in rest] == [ids[1]]
    assert client.get("/api/dossiers", params={"limit": 0}).status_code == 400
    assert client.get("/api/dossiers", params={"before": "yesterday"}).status_code == 400


def test_export_refuses_drafts_and_serves_frozen_html(client, dossier_store):
    dossier_id = freeze_ready(client, dossier_store)
    r = client.get(f"/api/dossiers/{dossier_id}/versions/1/export", params={"format": "html"})
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    client.post(f"/api/dossiers/{dossier_id}/freeze")
    r = client.get(f"/api/dossiers/{dossier_id}/versions/2/export", params={"format": "html"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-disposition"] == f'attachment; filename="42-dossier-{dossier_id}-v2.html"'
    assert "https://example.invalid/tiktok/@fixture_za_1/video/7431" in r.text
    assert 'href="#source-1"' in r.text
    assert client.get(f"/api/dossiers/{dossier_id}/versions/2/export", params={"format": "doc"}).status_code == 400
    assert client.get(f"/api/dossiers/{dossier_id}/versions/7/export").status_code == 404


def test_export_pdf_renders_the_frozen_html_in_memory(client, dossier_store, monkeypatch):
    from core.api import pdf
    seen = []
    monkeypatch.setattr(pdf, "render_pdf", lambda html: seen.append(html) or b"%PDF-1.4 fake")
    dossier_id = freeze_ready(client, dossier_store)
    assert client.get(f"/api/dossiers/{dossier_id}/versions/1/export", params={"format": "pdf"}).status_code == 409
    assert seen == []
    client.post(f"/api/dossiers/{dossier_id}/freeze")
    r = client.get(f"/api/dossiers/{dossier_id}/versions/2/export", params={"format": "pdf"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="42-dossier-{dossier_id}-v2.pdf"'
    assert r.content == b"%PDF-1.4 fake"
    assert len(seen) == 1 and seen[0] == client.get(
        f"/api/dossiers/{dossier_id}/versions/2/export", params={"format": "html"}).text


def test_export_pdf_failure_is_named(client, dossier_store, monkeypatch):
    from core.api import pdf

    def busy(html):
        raise pdf.PdfError("pdf_render_busy")

    def broken(html):
        raise pdf.PdfError("pdf_output_invalid")

    dossier_id = freeze_ready(client, dossier_store)
    client.post(f"/api/dossiers/{dossier_id}/freeze")
    url = f"/api/dossiers/{dossier_id}/versions/2/export"
    monkeypatch.setattr(pdf, "render_pdf", busy)
    r = client.get(url, params={"format": "pdf"})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    monkeypatch.setattr(pdf, "render_pdf", broken)
    r = client.get(url, params={"format": "pdf"})
    assert r.status_code == 500 and r.json()["error"] == "internal"


def test_dossier_rows_are_capped_like_the_sink(client, dossier_store, monkeypatch):
    monkeypatch.setattr(agent_app, "MAX_SINK", 3)
    for _ in range(5):
        new_dossier(client, dossier_store)
    assert len(agent_app.DOSSIER_VERSIONS) == 3


def test_bigquery_dossier_writes_and_reads(client, dossier_store, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    r = new_dossier(client, dossier_store)
    assert r.status_code == 201
    table, rows = client_bq.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.dossier_versions"
    assert set(rows[0]) == DOSSIER_ROW_KEYS and json.loads(rows[0]["body"])["version"] == 1
    assert agent_app.DOSSIER_VERSIONS == []
    stored = dict(rows[0], body=json.loads(rows[0]["body"]))
    client_bq.query.return_value.result.return_value = [stored]
    dossier_id = r.json()["dossier_id"]
    r = tick(client, dossier_id, "c2")
    assert r.status_code == 201
    table, rows = client_bq.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.dossier_reviews"
    assert set(rows[0]) == REVIEW_ROW_KEYS
    sql = [call.args[0] for call in client_bq.query.call_args_list]
    assert any("test-project.intelligence_42_agent.dossier_versions" in s for s in sql)


def test_bigquery_missing_dossier_tables_read_as_empty(client, dossier_store, monkeypatch):
    from google.api_core.exceptions import NotFound

    client_bq = fake_bigquery(monkeypatch)
    client_bq.query.side_effect = NotFound("Not found: Table test-project:intelligence_42_agent.dossier_versions")
    assert client.get("/api/dossiers").json() == {"dossiers": [], "next_before": None}
    assert client.get("/api/dossiers/d_000000000000").status_code == 404
    assert tick(client, "d_000000000000", "c1").status_code == 404
    client_bq.insert_rows_json.assert_not_called()


def test_bigquery_dossier_insert_errors_give_500(client, dossier_store, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    client_bq.insert_rows_json.return_value = [{"index": 0, "errors": ["no such field"]}]
    r = new_dossier(client, dossier_store)
    assert r.status_code == 500 and r.json()["error"] == "internal"


# Investigations (contract section 13.1).

INV_KEYS = {"investigation_id", "version", "created_at", "who", "status", "question", "market",
            "plan", "estimate", "ask_id", "run_id"}
MODEL_SPENT = "Today's model budget is spent; try tomorrow or ask Albert to raise MODEL_DAILY_USD"
NEED_T3 = "Investigations need the T3 agent"


@pytest.fixture
def inv(monkeypatch):
    from core.api import investigations
    monkeypatch.delenv("ASK_DAILY", raising=False)
    monkeypatch.delenv("MODEL_DAILY_USD", raising=False)

    class Clock:
        value = dt.datetime(2026, 10, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    monkeypatch.setattr(caps_config, "datetime", Clock)
    monkeypatch.setattr(agent_app, "_runs_have_model_usd", False, raising=False)
    agent_app.INVESTIGATIONS.clear()
    investigations.RESERVED.clear()
    agent_app._PLANNER_READBACK_HOLDS.clear()
    yield investigations
    agent_app.INVESTIGATIONS.clear()
    agent_app.RUNNING_INVESTIGATIONS.clear()
    investigations.RESERVED.clear()
    agent_app._PLANNER_READBACK_HOLDS.clear()


def today():
    return agent_app.now().date().isoformat()


def spend(model_usd=None, credits=None, run_id="r_spent_today", stage="brief"):
    agent_app.SINK.append({"run_id": run_id, "stage": stage, "run_date": today(), "credits": credits,
                           "model_usd": model_usd, "record": None})


def use_persistent_runs(monkeypatch, inv, rows=None):
    rows = list(rows or [])
    append = agent_app.append

    def persist(table, held, row):
        if table == "runs":
            rows.append(dict(row))
            return True
        return append(table, held, row)

    def read_spend():
        return inv.spent_in_rows([*agent_app.SINK, *rows], today())

    def read_planner_snapshot(run_id, run_date):
        all_rows = [*agent_app.SINK, *rows]
        run_rows = [row for row in all_rows if row.get("run_id") == run_id and row.get("run_date") == run_date]
        total = inv.spent_in_rows(all_rows, run_date)["model_usd"]
        planner = inv.spent_in_rows(run_rows, run_date)["model_usd"] if run_rows else None
        return {"model_usd": total, "planner_model_usd": planner}

    monkeypatch.setattr(agent_app, "append", persist)
    monkeypatch.setattr(agent_app, "spent_today", read_spend)
    monkeypatch.setattr(agent_app, "planner_spend_snapshot", read_planner_snapshot)
    return rows, persist


def draft(client, question="What is behind #fixture in South Africa this week?", **body):
    return client.post("/api/investigations", json={"question": question, "market": "ZA", **body})


class Gate:
    """A T3 run_ask that holds until opened, so investigations can overlap."""

    def __init__(self):
        self.opened = threading.Event()
        self.requests = []

    def run_ask(self, request, emit, should_stop):
        self.requests.append(request)
        emit({"kind": "plan", "text": "Splitting the question into three", "count": 3})
        while not self.opened.is_set() and not should_stop():
            time.sleep(0.005)
        if "fail" in request["question"]:
            raise RuntimeError("gate failed on purpose")
        return {"answer": load("ask_complete.json")["answer"],
                "run": {"run_id": "r_" + request["ask_id"][2:], "tier": "T3", "credits": 40, "model_usd": 0.5}}


def use_gate(monkeypatch, inv):
    gate = Gate()
    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (inv.fixture_planner, inv.fixture_estimate, gate.run_ask))
    return gate


def wait_finished(inv_id, seconds=5):
    """The finish row, once the run has ended; the reservation is released after that row is written."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        rows = [r for r in agent_app.INVESTIGATIONS if r["investigation_id"] == inv_id]
        if rows and rows[-1]["status"] in ("complete", "stopped", "failed"):
            ask = agent_app.get_ask(rows[-1]["ask_id"])
            if ask is None or ask.finished.wait(max(0.0, deadline - time.monotonic())):
                return rows[-1]
        time.sleep(0.01)
    raise AssertionError(f"{inv_id} did not finish")


def test_draft_returns_201_with_plan_estimate_and_budget_left(client, inv):
    r = draft(client)
    assert r.status_code == 201
    body = r.json()
    assert re.match(r"^i_[0-9a-f]{12}$", body["investigation_id"])
    assert body["status"] == "draft" and body["version"] == 1
    assert len(body["plan"]["sub_questions"]) == 3
    assert body["estimate"] == {"credits": 300, "model_usd": 1.5, "minutes": 6}
    planner_usd = inv.fixture_planner({"question": "q", "market": "ZA"})["run"]["model_usd"]
    assert body["budget_left"] == {"credits": 600, "model_usd": round(20 - planner_usd, 4)}
    assert body["plan"]["max_credits"] <= 600 and body["plan"]["max_model_usd"] <= 20 - planner_usd
    assert len(agent_app.INVESTIGATIONS) == 1
    stored = agent_app.INVESTIGATIONS[0]
    assert set(stored) == INV_KEYS
    assert stored["status"] == "draft" and stored["version"] == 1 and stored["ask_id"] is None
    assert json.loads(stored["plan"]) == body["plan"]
    assert json.loads(stored["estimate"]) == body["estimate"]


def test_draft_writes_the_planner_spend_to_a_runs_row_that_counts(client, inv, monkeypatch):
    spend(model_usd=19.0)
    budget_seen = []

    def planner(request):
        budget_seen.append(request["budget_left"]["model_usd"])
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    body = draft(client).json()
    rows = [r for r in agent_app.SINK if r["stage"] == "investigation"]
    assert len(rows) == 2
    ceiling, planner_row_saved = rows
    assert ceiling["run_id"] == planner_row_saved["run_id"]
    assert planner_row_saved["run_id"].startswith("r_plan_")
    assert ceiling["status"] == "running" and ceiling["model_usd"] == inv.PLANNER_USD
    assert planner_row_saved["status"] == "ok" and planner_row_saved["model_usd"] is None
    assert planner_row_saved["run_date"] == today()
    record = json.loads(planner_row_saved["record"])
    assert record["investigation_id"] == body["investigation_id"]
    assert record["actual_model_usd"] == 0.04 and "model_usd" not in record["run"]
    corrections = [r for r in agent_app.SINK if r["stage"] == "investigation_spend"]
    assert len(corrections) == 1 and corrections[0]["run_id"] != planner_row_saved["run_id"]
    assert corrections[0]["model_usd"] == pytest.approx(0.04 - inv.PLANNER_USD)
    assert agent_app.INVESTIGATIONS[0]["run_id"] == planner_row_saved["run_id"]
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    assert inv.spent_in_rows(agent_app.SINK, today())["model_usd"] == pytest.approx(19.04)
    raw_spend = sum((json.loads(r["record"]).get("run") or {}).get("model_usd", 0) for r in agent_app.SINK
                    if r["run_date"] == today() and r.get("record"))
    assert raw_spend == pytest.approx(0.04)
    # The next draft sees the first one's planner spend.
    second_response = draft(client)
    assert second_response.status_code == 201
    assert budget_seen == [1.0, 0.96]
    assert second_response.json()["budget_left"]["model_usd"] == pytest.approx(0.92)
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}


@pytest.mark.parametrize("append_failure", [False, RuntimeError("SYNTHETIC_SECRET_MARKER")])
@pytest.mark.parametrize("failure_point", ["planner", "correction"])
def test_final_planner_booking_failure_keeps_durable_ceiling_across_reload_and_denies_retry(
        client, inv, monkeypatch, caplog, append_failure, failure_point):
    called = []
    rows, persist = use_persistent_runs(monkeypatch, inv, [
        {"run_id": "r_prior_spend", "stage": "brief", "run_date": today(), "model_usd": 19.1,
         "credits": None, "record": None}
    ])

    def planner(request):
        called.append(request)
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))

    def fail_actual_booking(table, held, row):
        fails = table == "runs" and (
            (failure_point == "planner" and row["stage"] == "investigation" and row["status"] == "ok") or
            (failure_point == "correction" and row["stage"] == "investigation_spend"))
        if fails:
            if isinstance(append_failure, Exception):
                raise append_failure
            return append_failure
        return persist(table, held, row)

    monkeypatch.setattr(agent_app, "append", fail_actual_booking)

    first = draft(client)
    assert first.status_code == 500
    assert first.json() == {"error": "internal", "message": "The planner spend could not be saved; try again."}
    assert len(called) == 1 and agent_app.INVESTIGATIONS == []
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    holds = [r for r in rows if r["stage"] == "investigation" and r["status"] == "running"]
    assert len(holds) == 1 and holds[0]["model_usd"] == inv.PLANNER_USD

    # Simulate a service restart: process globals clear, while the fake runs ledger survives.
    agent_app.SINK.clear()
    agent_app.INVESTIGATIONS.clear()
    inv.RESERVED.clear()
    second = draft(client)
    assert second.status_code == 429 and len(called) == 1
    assert inv.spent_in_rows(rows, today())["model_usd"] == pytest.approx(19.1 + inv.PLANNER_USD)
    if isinstance(append_failure, Exception):
        assert "SYNTHETIC_SECRET_MARKER" not in caplog.text


@pytest.mark.parametrize("append_failure", [False, RuntimeError("SYNTHETIC_SECRET_MARKER")])
def test_unconfirmed_planner_ceiling_booking_never_calls_planner(client, inv, monkeypatch, caplog, append_failure):
    called = []

    def planner(request):
        called.append(request)
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    original_append = agent_app.append

    def fail_ceiling(table, held, row):
        if table == "runs":
            if isinstance(append_failure, Exception):
                raise append_failure
            return append_failure
        return original_append(table, held, row)

    monkeypatch.setattr(agent_app, "append", fail_ceiling)
    first = draft(client)
    assert first.status_code == 500
    assert first.json() == {"error": "internal", "message": "The planner spend could not be saved; try again."}
    assert called == [] and agent_app.INVESTIGATIONS == []
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    if isinstance(append_failure, Exception):
        assert "SYNTHETIC_SECRET_MARKER" not in caplog.text


def test_delayed_planner_ceiling_readback_holds_budget_until_visible_without_double_count(
        client, inv, monkeypatch):
    monkeypatch.setenv("MODEL_DAILY_USD", "1.00")
    rows = []
    visibility = {"visible": False}
    calls = []
    original_append = agent_app.append

    def append(table, held, row):
        if table == "runs":
            rows.append(dict(row))
            return True
        return original_append(table, held, row)

    def read_spend():
        return inv.spent_in_rows([*agent_app.SINK, *(rows if visibility["visible"] else [])], today())

    def read_planner_snapshot(run_id, run_date):
        all_rows = [*agent_app.SINK, *(rows if visibility["visible"] else [])]
        run_rows = [row for row in all_rows if row.get("run_id") == run_id and row.get("run_date") == run_date]
        total = inv.spent_in_rows(all_rows, run_date)["model_usd"]
        planner_usd = inv.spent_in_rows(run_rows, run_date)["model_usd"] if run_rows else None
        return {"model_usd": total, "planner_model_usd": planner_usd}

    def planner(request):
        calls.append(request)
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "append", append)
    monkeypatch.setattr(agent_app, "spent_today", read_spend)
    monkeypatch.setattr(agent_app, "planner_spend_snapshot", read_planner_snapshot)
    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))

    first = draft(client)
    second = draft(client)
    assert first.status_code == second.status_code == 500
    assert calls == [] and len(rows) == 1
    assert rows[0]["status"] == "running" and rows[0]["model_usd"] == inv.PLANNER_USD
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": inv.PLANNER_USD}

    visibility["visible"] = True
    assert agent_app.money_left()["model_usd"] == pytest.approx(0.50)
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    third = draft(client)
    assert third.status_code == 201 and len(calls) == 1
    assert len([r for r in rows if r["status"] == "running"]) == 2
    assert inv.spent_in_rows(rows, today())["model_usd"] == pytest.approx(0.54)
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}


def test_planner_booking_and_dispatch_fail_closed_across_the_sast_cap_cutoff(client, inv, monkeypatch):
    class Clock:
        value = dt.datetime(2026, 10, 2, 23, 59, 59, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    def app_now(market=None):
        zone = dt.timezone(dt.timedelta(hours=agent_app.OFFSETS.get(market, 2)))
        return Clock.value.astimezone(zone).replace(microsecond=0)

    monkeypatch.setattr(caps_config, "datetime", Clock)
    monkeypatch.setattr(agent_app, "now", app_now)
    assert agent_app.money_left()["model_usd"] == 80.0
    Clock.value = dt.datetime(2026, 10, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert agent_app.money_left()["model_usd"] == 20.0

    Clock.value = dt.datetime(2026, 10, 2, 23, 59, 59, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    rows = []
    calls = []
    original_append = agent_app.append

    def append(table, held, row):
        if table == "runs":
            rows.append(dict(row))
            return True
        return original_append(table, held, row)

    def read_spend():
        return inv.spent_in_rows([*agent_app.SINK, *rows], today())

    def read_planner_snapshot(run_id, run_date):
        day_rows = [row for row in [*agent_app.SINK, *rows] if row.get("run_date") == run_date]
        run_rows = [row for row in day_rows if row.get("run_id") == run_id]
        snapshot = {"model_usd": inv.spent_in_rows(day_rows, run_date)["model_usd"],
                    "planner_model_usd": inv.spent_in_rows(run_rows, run_date)["model_usd"] if run_rows else None}
        Clock.value = dt.datetime(2026, 10, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        return snapshot

    def planner(request):
        calls.append(request)
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "append", append)
    monkeypatch.setattr(agent_app, "spent_today", read_spend)
    monkeypatch.setattr(agent_app, "planner_spend_snapshot", read_planner_snapshot)
    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    response = draft(client)
    assert response.status_code == 409
    assert response.json()["message"] == "The model budget day changed; retry the draft."
    assert calls == [] and len(rows) == 1
    assert rows[0]["run_date"] == "2026-10-02" and rows[0]["status"] == "running"
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    assert agent_app.money_left()["model_usd"] == 20.0


@pytest.mark.parametrize("readback_failure", ["missing", "raises", "unrelated", "nan_total", "infinite_total",
                                               "nan_booking", "infinite_booking"])
def test_missing_raised_or_unrelated_planner_ceiling_readback_never_calls_planner(
        client, inv, monkeypatch, caplog, readback_failure):
    monkeypatch.setenv("MODEL_DAILY_USD", "1.00")
    rows = []
    planner_reads = []
    calls = []
    original_append = agent_app.append

    def append(table, held, row):
        if table == "runs":
            rows.append(dict(row))
            return True
        return original_append(table, held, row)

    def read_spend():
        return {"credits": 0.0, "model_usd": 0.0}

    def read_planner_snapshot(run_id, run_date):
        planner_reads.append((run_id, run_date))
        if readback_failure == "raises":
            raise RuntimeError("SYNTHETIC_SECRET_MARKER")
        if readback_failure == "unrelated":
            return {"model_usd": 0.5, "planner_model_usd": None}
        if readback_failure == "nan_total":
            return {"model_usd": float("nan"), "planner_model_usd": 0.5}
        if readback_failure == "infinite_total":
            return {"model_usd": float("inf"), "planner_model_usd": 0.5}
        if readback_failure == "nan_booking":
            return {"model_usd": 0.5, "planner_model_usd": float("nan")}
        if readback_failure == "infinite_booking":
            return {"model_usd": 0.5, "planner_model_usd": float("inf")}
        return {"model_usd": None, "planner_model_usd": None}

    def planner(request):
        calls.append(request)
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "append", append)
    monkeypatch.setattr(agent_app, "spent_today", read_spend)
    monkeypatch.setattr(agent_app, "planner_spend_snapshot", read_planner_snapshot)
    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    first = draft(client)
    second = draft(client)
    assert first.status_code == second.status_code == 500
    assert calls == [] and len(rows) == 1
    assert len(planner_reads) == 2 and planner_reads[0] == planner_reads[1]
    assert planner_reads[0] == (rows[0]["run_id"], today())
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": inv.PLANNER_USD}
    if readback_failure == "raises":
        assert "SYNTHETIC_SECRET_MARKER" not in caplog.text


def test_unknown_planner_usage_keeps_the_durable_ceiling_without_a_correction(client, inv, monkeypatch):
    rows, _ = use_persistent_runs(monkeypatch, inv)

    def planner(request):
        result = inv.fixture_planner(request)
        result["run"] = {key: value for key, value in result["run"].items() if key != "model_usd"}
        return result

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    response = draft(client)
    assert response.status_code == 201
    ceiling, final = [r for r in rows if r["stage"] == "investigation"]
    assert ceiling["model_usd"] == inv.PLANNER_USD
    assert final["model_usd"] is None and "actual_model_usd" not in json.loads(final["record"])
    assert not [r for r in rows if r["stage"] == "investigation_spend"]
    assert inv.spent_in_rows(rows, today())["model_usd"] == inv.PLANNER_USD
    assert response.json()["budget_left"]["model_usd"] == 20 - inv.PLANNER_USD


def test_planner_receives_its_own_ceiling_and_the_budget_left(client, inv, monkeypatch):
    seen = []
    angles = [f"angle {i}" for i in range(1, 9)]

    def planner(request):
        seen.append(request)
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    response = draft(client, angles=angles)
    assert response.status_code == 201
    assert seen[0]["max_model_usd"] == inv.PLANNER_USD
    assert seen[0]["angles"] == angles
    assert seen[0]["budget_left"] == {"credits": 600, "model_usd": 20.0}
    assert seen[0]["investigation_id"].startswith("i_")


def test_nine_angles_are_refused_before_planner_dispatch_or_booking(client, inv, monkeypatch):
    calls = []

    def planner(request):
        calls.append(request)
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    response = client.post("/api/investigations", json={
        "question": "What is behind #fixture?", "market": "ZA",
        "angles": [f"angle {i}" for i in range(1, 10)],
    })

    assert response.status_code == 400 and response.json()["error"] == "bad_request"
    assert calls == []
    assert agent_app.SINK == [] and agent_app.INVESTIGATIONS == []
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}


def test_draft_refused_under_half_a_dollar_left(client, inv, monkeypatch):
    called = []
    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (lambda request: called.append(request), inv.fixture_estimate,
                                 agent_app.fixture_agent))
    spend(model_usd=19.51)
    r = draft(client)
    assert r.status_code == 429
    assert r.json() == {"error": "rate_limited", "message": MODEL_SPENT}
    assert called == []
    assert agent_app.INVESTIGATIONS == []
    assert [r["stage"] for r in agent_app.SINK] == ["brief"]


def test_draft_allowed_at_exactly_half_a_dollar_left(client, inv):
    spend(model_usd=19.5)
    assert draft(client).status_code == 201


def test_draft_refused_when_running_reservations_leave_under_half_a_dollar(client, inv):
    inv.RESERVED.add("i_running", {"credits": 0, "model_usd": 19.8})
    r = draft(client)
    assert r.status_code == 429 and r.json()["message"] == MODEL_SPENT


@pytest.mark.parametrize("body", [
    {"question": "hi", "market": "ZA"},
    {"question": "What is behind #fixture?", "market": "UK"},
    {"question": "What is behind #fixture?", "market": "ZA", "angles": "music"},
    {"question": "What is behind #fixture?", "market": "ZA", "angles": [""]},
])
def test_bad_drafts_give_400(client, inv, body):
    r = client.post("/api/investigations", json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert agent_app.INVESTIGATIONS == []


def missing_t3(monkeypatch):
    monkeypatch.delenv("F42_AGENT", raising=False)
    for name in ("core.agent", "core.agent.ask", "core.agent.investigate"):
        monkeypatch.setitem(sys.modules, name, None)


def test_missing_t3_agent_gives_503(client, inv, monkeypatch):
    held = draft(client).json()
    missing_t3(monkeypatch)
    assert agent_app.resolve_investigator() is None
    rows = len(agent_app.INVESTIGATIONS)
    for r in (draft(client),
              client.put(f"/api/investigations/{held['investigation_id']}/plan", json={"plan": held["plan"]}),
              client.post(f"/api/investigations/{held['investigation_id']}/start")):
        assert r.status_code == 503
        assert r.json() == {"error": "agent_unavailable", "message": NEED_T3}
    assert len(agent_app.INVESTIGATIONS) == rows


def test_resolve_investigator_uses_core_agent_investigate(monkeypatch):
    monkeypatch.delenv("F42_AGENT", raising=False)
    package = types.ModuleType("core.agent")
    package.__path__ = []
    package.run_ask = lambda request, emit, should_stop: {"answer": None, "run": None}
    module = types.ModuleType("core.agent.investigate")
    module.plan_investigation = lambda request: None
    module.estimate_plan = lambda plan: None
    monkeypatch.setitem(sys.modules, "core.agent", package)
    monkeypatch.setitem(sys.modules, "core.agent.ask", None)
    monkeypatch.setitem(sys.modules, "core.agent.investigate", module)
    assert agent_app.resolve_investigator() == (module.plan_investigation, module.estimate_plan, package.run_ask)
    monkeypatch.setitem(sys.modules, "core.agent.investigate", None)
    assert agent_app.resolve_investigator() is None
    package.plan_investigation = module.plan_investigation
    package.estimate_plan = module.estimate_plan
    assert agent_app.resolve_investigator() == (module.plan_investigation, module.estimate_plan, package.run_ask)


def test_put_plan_appends_a_draft_with_a_fresh_estimate_and_clamps(client, inv):
    held = draft(client).json()
    plan = copy.deepcopy(held["plan"])
    plan["sub_questions"] = plan["sub_questions"][:2]
    plan["max_credits"] = 5000
    r = client.put(f"/api/investigations/{held['investigation_id']}/plan", json={"plan": plan})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2 and body["status"] == "draft"
    assert body["estimate"] == inv.fixture_estimate(body["plan"])
    assert body["plan"]["max_credits"] == 600
    assert len(body["plan"]["sub_questions"]) == 2
    assert [r["version"] for r in agent_app.INVESTIGATIONS] == [1, 2]
    assert client.get(f"/api/investigations/{held['investigation_id']}").json()["version"] == 2


def test_put_bad_plan_gives_400_and_unknown_gives_404(client, inv):
    held = draft(client).json()
    bad_plan = {**held["plan"], "sub_questions": []}
    bad = client.put(f"/api/investigations/{held['investigation_id']}/plan",
                     json={"plan": bad_plan})
    assert bad.status_code == 400
    assert client.put(f"/api/investigations/{held['investigation_id']}/plan", json={}).status_code == 400
    assert client.put("/api/investigations/i_000000000000/plan", json={"plan": held["plan"]}).status_code == 404
    assert len(agent_app.INVESTIGATIONS) == 1


def test_start_refused_over_the_credit_budget_with_its_words(client, inv):
    held = draft(client).json()
    spend(credits=450, stage="ask", run_id="r_ask_today")
    r = client.post(f"/api/investigations/{held['investigation_id']}/start")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "not_ready"
    assert "300 credits" in body["message"] and "150" in body["message"] and "ASK_DAILY" in body["message"]
    assert "MODEL_DAILY_USD" not in body["message"]
    assert [r["status"] for r in agent_app.INVESTIGATIONS] == ["draft"]
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}


def test_start_refused_over_the_model_budget_with_its_words(client, inv):
    held = draft(client).json()
    spend(model_usd=19.0)
    r = client.post(f"/api/investigations/{held['investigation_id']}/start")
    assert r.status_code == 409
    message = r.json()["message"]
    assert "USD 1.50" in message and "MODEL_DAILY_USD" in message and "ASK_DAILY" not in message
    assert [r["status"] for r in agent_app.INVESTIGATIONS] == ["draft"]
    assert agent_app.ASKS == {}


def test_unknown_budget_refuses_to_start_and_to_draft(client, inv, monkeypatch):
    held = draft(client).json()
    monkeypatch.setattr(agent_app, "spent_today", lambda: {"credits": None, "model_usd": 2.0})
    r = client.post(f"/api/investigations/{held['investigation_id']}/start")
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    assert "could not be read" in r.json()["message"]
    assert agent_app.ASKS == {} and inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    assert draft(client).status_code == 409
    assert client.put(f"/api/investigations/{held['investigation_id']}/plan",
                      json={"plan": held["plan"]}).status_code == 409
    assert [r["status"] for r in agent_app.INVESTIGATIONS] == ["draft"]


def test_reading_a_draft_again_carries_the_budget_left_now(client, inv):
    held = draft(client).json()
    url = f"/api/investigations/{held['investigation_id']}"
    assert client.get(url).json()["budget_left"] == held["budget_left"]
    spend(credits=150, stage="ask", run_id="r_ask_today")
    inv.RESERVED.add("i_running", {"credits": 100, "model_usd": 3.0})
    body = client.get(url).json()
    assert body["budget_left"] == {"credits": 350, "model_usd": round(held["budget_left"]["model_usd"] - 3.0, 4)}
    assert body["budget_left"] == agent_app.money_left()


def test_reading_a_draft_whose_spend_cannot_be_read_gives_a_null_budget_left(client, inv, monkeypatch):
    held = draft(client).json()
    monkeypatch.setattr(agent_app, "spent_today", lambda: {"credits": None, "model_usd": 2.0})
    r = client.get(f"/api/investigations/{held['investigation_id']}")
    assert r.status_code == 200
    body = r.json()
    assert "budget_left" in body and body["budget_left"] is None
    assert body["status"] == "draft" and body["plan"] == held["plan"]


@pytest.mark.parametrize("status", ["running", "complete", "stopped", "failed"])
def test_reading_an_investigation_that_is_not_a_draft_carries_no_budget_left(client, inv, monkeypatch, status):
    plan = inv.fixture_planner({"question": "q", "market": "ZA"})["plan"]
    agent_app.INVESTIGATIONS.append(inv.storage_row(
        "i_0123456789ab", 2, "2026-09-29T10:00:00+02:00", status, "What is behind #fixture?", "ZA", plan,
        {"credits": 300, "model_usd": 1.5, "minutes": 6}, None, "r_plan_x"))
    reads = []
    monkeypatch.setattr(agent_app, "spent_today", lambda: reads.append(1) or {"credits": 0.0, "model_usd": 0.0})
    body = client.get("/api/investigations/i_0123456789ab").json()
    assert body["status"] == status and "budget_left" not in body
    assert reads == []


def test_two_started_back_to_back_share_the_budget_left(client, inv, monkeypatch):
    monkeypatch.setenv("ASK_DAILY", "700")
    monkeypatch.setenv("MODEL_DAILY_USD", "5")
    gate = use_gate(monkeypatch, inv)
    first, second = draft(client).json(), draft(client).json()
    planner_usd = 2 * inv.fixture_planner({"question": "q", "market": "ZA"})["run"]["model_usd"]
    one = client.post(f"/api/investigations/{first['investigation_id']}/start")
    two = client.post(f"/api/investigations/{second['investigation_id']}/start")
    assert one.status_code == 202 and two.status_code == 202
    assert one.json()["ceilings"] == {"max_credits": 400, "max_model_usd": 3.0}
    assert two.json()["ceilings"] == {"max_credits": 300, "max_model_usd": round(5 - planner_usd - 3.0, 4)}
    assert inv.RESERVED.total() == {"credits": 700, "model_usd": round(3.0 + 5 - planner_usd - 3.0, 4)}
    requests = {r["investigation_id"]: r for r in gate.requests}
    assert requests[first["investigation_id"]]["max_credits"] == 400
    assert requests[second["investigation_id"]]["max_credits"] == 300
    assert all(r["tier"] == "T3" and r["plan"]["sub_questions"] for r in gate.requests)
    # A third finds nothing left while both are running.
    third = draft(client, "What is behind #another in South Africa?")
    assert third.status_code == 429
    gate.opened.set()
    wait_finished(first["investigation_id"])
    wait_finished(second["investigation_id"])
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}


@pytest.mark.parametrize("how, status", [("finish", "complete"), ("stop", "stopped"), ("fail", "failed")])
def test_reservation_is_released_when_the_run_ends(client, inv, monkeypatch, how, status):
    gate = use_gate(monkeypatch, inv)
    question = "Why does this fail for #fixture?" if how == "fail" else "What is behind #fixture this week?"
    held = draft(client, question).json()
    started = client.post(f"/api/investigations/{held['investigation_id']}/start")
    assert started.status_code == 202
    assert inv.RESERVED.total() == {"credits": 400, "model_usd": 3.0}
    if how == "stop":
        r = client.post(f"/api/investigations/{held['investigation_id']}/stop")
        assert r.status_code == 202
        assert r.json() == {"investigation_id": held["investigation_id"], "status": "stopping"}
    else:
        gate.opened.set()
    last = wait_finished(held["investigation_id"])
    assert last["status"] == status
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    assert held["investigation_id"] not in agent_app.RUNNING_INVESTIGATIONS


def test_every_draft_edit_start_and_finish_appends_a_row(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    held = draft(client).json()
    inv_id = held["investigation_id"]
    client.put(f"/api/investigations/{inv_id}/plan", json={"plan": held["plan"]})
    before = copy.deepcopy(agent_app.INVESTIGATIONS)
    started = client.post(f"/api/investigations/{inv_id}/start").json()
    gate.opened.set()
    wait_finished(inv_id)
    rows = agent_app.INVESTIGATIONS
    assert [(r["version"], r["status"]) for r in rows] == [(1, "draft"), (2, "draft"), (3, "running"), (4, "complete")]
    assert rows[:2] == before
    assert all(set(r) == INV_KEYS and r["investigation_id"] == inv_id for r in rows)
    assert rows[2]["ask_id"] == started["ask_id"] == rows[3]["ask_id"]
    assert rows[3]["run_id"] == "r_" + started["ask_id"][2:]
    assert all(r["plan"] == rows[1]["plan"] for r in rows[2:])


def test_finished_run_is_an_ask_record_with_the_investigation_id_and_notice(client, inv, monkeypatch):
    fixture_agent = agent_app.fixture_agent
    notice_texts = {
        "complete": "The investigation has finished and its answer is ready to open",
        "partial": "The investigation has finished with a partial answer",
        "insufficient_evidence": "The investigation has finished without enough evidence for an answer",
        "refused": "The investigation has finished and its question was refused",
        "stopped": "The investigation was stopped before it finished",
        "failed": "The investigation failed before it finished",
    }
    notice = {}

    def agent_with_notice(request, emit, should_stop):
        result = fixture_agent(request, emit, should_stop)
        notice.update({"investigation_id": request["investigation_id"], "status": result["answer"]["status"],
                       "text": notice_texts[result["answer"]["status"]],
                       "at": agent_app.now(request.get("market")).isoformat()})
        result["run"]["notice"] = dict(notice)
        return result

    monkeypatch.setattr(agent_app, "fixture_agent", agent_with_notice)
    held = draft(client).json()
    inv_id = held["investigation_id"]
    started = client.post(f"/api/investigations/{inv_id}/start")
    assert started.status_code == 202
    body = started.json()
    assert ASK_ID.match(body["ask_id"])
    assert body["events_url"] == f"/api/investigations/{inv_id}/events"
    assert body["url"] == f"/api/investigations/{inv_id}"
    events, _ = parse_sse(client.get(body["events_url"]).text)
    assert events[-1]["event"] == "done" and events[-1]["data"]["status"] == "complete"
    read = client.get(body["url"]).json()
    assert read["status"] == "complete" and read["version"] == 3
    record = read["record"]
    assert set(record) == RECORD_KEYS | {"investigation_id"}
    assert record["investigation_id"] == inv_id and record["ask_id"] == body["ask_id"]
    assert record["answer"] == load("ask_complete.json")["answer"]
    assert record["run"]["notice"] == notice
    assert set(record["run"]["notice"]) == {"investigation_id", "status", "text", "at"}
    ask_rows = [r for r in agent_app.SINK if r["stage"] == "ask"]
    assert len(ask_rows) == 1 and json.loads(ask_rows[0]["record"]) == record
    # Evicted from memory: the table and the runs rows still hold everything.
    agent_app.ASKS.clear()
    again = client.get(body["url"]).json()
    assert again["record"] == record
    assert again["record"]["run"]["notice"] == notice
    replay, _ = parse_sse(client.get(body["events_url"]).text)
    assert [e["data"] for e in replay if e["event"] == "step"] == record["steps"]
    assert replay[-1]["event"] == "done" and replay[-1]["data"]["status"] == "complete"


def test_failed_investigation_keeps_its_notice_in_safe_record_storage(client, inv, monkeypatch):
    notice = {}

    def failing_agent(request, emit, should_stop):
        notice.update({"investigation_id": request["investigation_id"], "status": "failed",
                       "text": "The investigation failed before it finished",
                       "at": agent_app.now(request.get("market")).isoformat()})
        failure = RuntimeError("research failed")
        failure.run = {"run_id": "r_failed", "model_usd": 0.1, "notice": dict(notice),
                       "provider_response": "must not be exposed"}
        raise failure

    monkeypatch.setattr(agent_app, "fixture_agent", failing_agent)
    held = draft(client).json()
    inv_id = held["investigation_id"]
    started = client.post(f"/api/investigations/{inv_id}/start").json()
    assert started["status"] == "running"
    wait_finished(inv_id)

    record = client.get(f"/api/investigations/{inv_id}").json()["record"]
    assert record["run"] == {"run_id": "r_failed", "model_usd": 0.1, "notice": notice}
    assert set(record["run"]["notice"]) == {"investigation_id", "status", "text", "at"}
    assert record["run"]["notice"]["investigation_id"] == inv_id
    assert record["run"]["notice"]["status"] == "failed"
    assert record["run"]["notice"]["text"] == "The investigation failed before it finished"
    ask_row = next(r for r in agent_app.SINK if r["stage"] == "ask")
    stored_record = json.loads(ask_row["record"])
    assert stored_record == record and stored_record["run"]["notice"]["investigation_id"] == inv_id
    agent_app.ASKS.clear()
    again = client.get(f"/api/investigations/{inv_id}").json()["record"]
    assert again == record and again["run"]["notice"] == notice


def test_start_twice_gives_409_and_stop_needs_a_running_one(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    held = draft(client).json()
    inv_id = held["investigation_id"]
    assert client.post(f"/api/investigations/{inv_id}/stop").status_code == 409
    assert client.post(f"/api/investigations/{inv_id}/start").status_code == 202
    assert client.post(f"/api/investigations/{inv_id}/start").status_code == 409
    assert client.put(f"/api/investigations/{inv_id}/plan", json={"plan": held["plan"]}).status_code == 409
    gate.opened.set()
    wait_finished(inv_id)
    assert client.post("/api/investigations/i_000000000000/stop").status_code == 404
    assert client.post("/api/investigations/i_000000000000/start").status_code == 404
    assert client.get("/api/investigations/i_000000000000").status_code == 404
    assert client.get("/api/investigations/i_000000000000/events").status_code == 404
    assert client.get(f"/api/investigations/{draft(client).json()['investigation_id']}/events").status_code == 409


def test_list_is_newest_first_with_a_status_filter(client, inv, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    first = draft(client, "What is behind #first in South Africa?").json()["investigation_id"]
    time.sleep(0.02)
    second = draft(client, "What is behind #second in South Africa?").json()["investigation_id"]
    client.post(f"/api/investigations/{first}/start")
    listed = client.get("/api/investigations").json()["investigations"]
    assert [i["investigation_id"] for i in listed] == [second, first]
    assert [i["status"] for i in listed] == ["draft", "running"]
    assert [i["investigation_id"] for i in
            client.get("/api/investigations?status=running").json()["investigations"]] == [first]
    assert client.get("/api/investigations?status=paused").status_code == 400
    gate.opened.set()
    wait_finished(first)


def test_planner_failure_keeps_its_spend_and_gives_502(client, inv, monkeypatch):
    class PlannerError(RuntimeError):
        run = {"run_id": "r_plan_failed", "model_usd": 0.2}

    def planner(request):
        raise PlannerError("planner broke")

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    r = draft(client)
    assert r.status_code == 502 and r.json()["error"] == "agent_unavailable"
    rows = [r for r in agent_app.SINK if r["stage"] == "investigation"]
    assert len(rows) == 2 and rows[0]["model_usd"] == inv.PLANNER_USD
    assert rows[1]["model_usd"] is None and rows[1]["status"] == "failed"
    assert json.loads(rows[1]["record"])["actual_model_usd"] == 0.2
    correction, = [r for r in agent_app.SINK if r["stage"] == "investigation_spend"]
    assert correction["model_usd"] == pytest.approx(0.2 - inv.PLANNER_USD)
    assert inv.spent_in_rows(agent_app.SINK, today())["model_usd"] == pytest.approx(0.2)
    assert agent_app.INVESTIGATIONS == []
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}


def test_failed_planner_booking_after_exception_keeps_durable_ceiling_and_returns_persistence_error(client, inv,
                                                                                                    monkeypatch):
    class PlannerError(RuntimeError):
        run = {"run_id": "r_plan_unbooked_failure", "model_usd": 0.2}

    def planner(request):
        raise PlannerError("planner broke after charging")

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    original_append = agent_app.append

    def fail_planner_row(table, held, row):
        return False if table == "runs" and row["status"] == "failed" else original_append(table, held, row)

    monkeypatch.setattr(agent_app, "append", fail_planner_row)
    r = draft(client)
    assert r.status_code == 500
    assert r.json() == {"error": "internal", "message": "The planner spend could not be saved; try again."}
    assert agent_app.INVESTIGATIONS == []
    (hold,) = [row for row in agent_app.SINK if row["stage"] == "investigation"]
    assert hold["status"] == "running" and hold["model_usd"] == inv.PLANNER_USD
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}


def test_bigquery_investigation_rows_go_to_their_tables(client, inv, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    # The runs table already has L1's model_usd column.
    client_bq.query.return_value.result.return_value = [{"column_name": "model_usd"}, {"column_name": "record"}]
    monkeypatch.setattr(agent_app, "spent_today", lambda: {"credits": 0.0, "model_usd": 0.0})

    def planner_spend_snapshot(run_id, run_date):
        rows = [call.args[1][0] for call in client_bq.insert_rows_json.call_args_list
                if call.args[0].endswith(".runs")]
        exact = [row for row in rows if row["run_id"] == run_id and row["run_date"] == run_date]
        return {"model_usd": inv.spent_in_rows(rows, run_date)["model_usd"],
                "planner_model_usd": inv.spent_in_rows(exact, run_date)["model_usd"] if exact else None}

    monkeypatch.setattr(agent_app, "planner_spend_snapshot", planner_spend_snapshot)
    r = draft(client)
    assert r.status_code == 201
    inserts = client_bq.insert_rows_json.call_args_list
    tables = [call.args[0] for call in inserts]
    assert tables == ["test-project.intelligence_42_agent.runs"] * 3 + [
        "test-project.intelligence_42_agent.investigations"]
    ceiling, planner, correction, stored = (call.args[1][0] for call in inserts)
    assert ceiling["stage"] == planner["stage"] == "investigation"
    assert ceiling["model_usd"] == inv.PLANNER_USD and ceiling["status"] == "running"
    assert planner["run_id"] == ceiling["run_id"] and planner["model_usd"] is None
    assert json.loads(planner["record"])["actual_model_usd"] == 0.04
    assert correction["stage"] == "investigation_spend" and correction["run_id"] != planner["run_id"]
    assert correction["model_usd"] == pytest.approx(0.04 - inv.PLANNER_USD)
    assert set(stored) == INV_KEYS and stored["version"] == 1
    assert agent_app.INVESTIGATIONS == [] and agent_app.SINK == []


def test_bigquery_reads_investigations_latest_row_with_parameters(client, inv, monkeypatch):
    from core.api import investigations
    plan = investigations.fixture_planner({"question": "q", "market": "ZA"})["plan"]
    rows = [investigations.storage_row("i_0123456789ab", v, f"2026-09-29T10:0{v}:00+02:00", "draft",
                                       "What is behind #fixture?", "ZA", plan,
                                       {"credits": 300, "model_usd": 1.5, "minutes": 6}, None, "r_plan_x")
            for v in (1, 2)]
    client_bq = fake_bigquery(monkeypatch, rows)
    monkeypatch.setattr(agent_app, "spent_today", lambda: {"credits": 0.0, "model_usd": 0.0})
    read = client.get("/api/investigations/i_0123456789ab").json()
    assert read["version"] == 2 and read["plan"] == plan
    sql, = [call.args[0] for call in client_bq.query.call_args_list]
    assert "test-project.intelligence_42_agent.investigations" in sql and "@investigation_id" in sql
    config = client_bq.query.call_args.kwargs["job_config"]
    assert config.maximum_bytes_billed == 2 * 1024 ** 3


def test_bigquery_draft_read_gives_a_null_budget_left_when_the_spend_tables_fail(client, inv, monkeypatch):
    from core.api import investigations
    plan = investigations.fixture_planner({"question": "q", "market": "ZA"})["plan"]
    row = investigations.storage_row("i_0123456789ab", 1, "2026-09-29T10:01:00+02:00", "draft",
                                     "What is behind #fixture?", "ZA", plan,
                                     {"credits": 300, "model_usd": 1.5, "minutes": 6}, None, "r_plan_x")
    client_bq = fake_bigquery(monkeypatch)

    def query(sql, job_config=None):
        if "credit_ledger" in sql or "INFORMATION_SCHEMA" in sql or "intelligence_42_agent.runs" in sql:
            raise RuntimeError("BigQuery unreachable")
        result = mock.MagicMock()
        result.result.return_value = [row]
        return result

    client_bq.query.side_effect = query
    r = client.get("/api/investigations/i_0123456789ab")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "draft" and "budget_left" in body and body["budget_left"] is None


def test_bigquery_missing_investigations_table_reads_as_empty(client, inv, monkeypatch):
    from google.api_core.exceptions import NotFound

    client_bq = fake_bigquery(monkeypatch)
    client_bq.query.side_effect = NotFound("Not found: Table test-project:intelligence_42_agent.investigations")
    assert client.get("/api/investigations").json() == {"investigations": []}
    assert client.get("/api/investigations/i_0123456789ab").status_code == 404


class RunsWithoutModelUsd:
    """BigQuery before L1 adds runs.model_usd: an insert naming that field is rejected whole, as insert_rows_json does.

    The runs spend read sums each run's highest record.run.model_usd for the day, as investigations._runs_usd does."""

    def __init__(self, catalog_breaks_on=None):
        self.catalog_breaks_on, self.catalog_calls = catalog_breaks_on, 0
        self.tables = {}
        self.planner_reads = []

    def insert_rows_json(self, table, rows):
        if table.endswith(".runs") and any("model_usd" in row for row in rows):
            return [{"index": 0, "errors": [{"reason": "invalid", "location": "model_usd",
                                             "message": "no such field: model_usd."}]}]
        self.tables.setdefault(table.rsplit(".", 1)[1], []).extend(rows)
        return []

    def query(self, sql, job_config=None):
        params = {p.name: getattr(p, "value", None) for p in job_config.query_parameters}
        if "INFORMATION_SCHEMA" in sql:
            self.catalog_calls += 1
            if self.catalog_calls == self.catalog_breaks_on:
                raise RuntimeError("catalog unreachable")
            out = [{"column_name": c} for c in sorted(ROW_KEYS)]
        elif "credit_ledger" in sql:
            out = [{"credits": None}]
        elif "intelligence_42_agent.runs" in sql:
            assert "r.model_usd" not in sql
            if "planner_model_usd" in sql:
                assert job_config.use_query_cache is False
                assert "MAX(IF(run_id = @run_id, usd, NULL))" in sql
                self.planner_reads.append(sql)
            usd = {}
            for row in self.tables.get("runs", []):
                value = (json.loads(row["record"]).get("run") or {}).get("model_usd")
                if row["run_date"] == str(params["d"]) and value is not None:
                    if row["run_id"] not in usd or value > usd[row["run_id"]]:
                        usd[row["run_id"]] = value
            if "planner_model_usd" in sql:
                out = [{"model_usd": sum(usd.values()) if usd else 0.0,
                        "planner_model_usd": usd.get(params["run_id"])}]
            else:
                out = [{"usd": sum(usd.values()) if usd else None}]
        else:
            raise AssertionError(f"unexpected query: {sql}")
        result = mock.MagicMock()
        result.result.return_value = out
        return result


# The draft reads the catalog for spend and planner writes; call 3 fails closed while recording actual usage.
@pytest.mark.parametrize("catalog_breaks_on", [None, 3])
def test_bigquery_planner_spend_counts_before_runs_has_model_usd(client, inv, monkeypatch, catalog_breaks_on):
    fake = RunsWithoutModelUsd(catalog_breaks_on)
    monkeypatch.setenv("F42_DATA", "bigquery")
    monkeypatch.setenv("F42_PROJECT", "test-project")
    monkeypatch.setattr(agent_app, "bigquery_client", lambda: fake)
    assert agent_app.money_left() == {"credits": 600, "model_usd": 20.0}
    r = draft(client)
    assert r.status_code == 201
    ceiling, planner, correction = fake.tables["runs"]
    assert ceiling["stage"] == planner["stage"] == "investigation"
    assert "model_usd" not in ceiling and "model_usd" not in planner
    usd = json.loads(planner["record"])["actual_model_usd"]
    assert json.loads(ceiling["record"])["run"]["model_usd"] == inv.PLANNER_USD
    assert correction["stage"] == "investigation_spend"
    assert json.loads(correction["record"])["run"]["model_usd"] == pytest.approx(usd - inv.PLANNER_USD)
    assert len(fake.planner_reads) == 1
    assert fake.catalog_calls >= 3
    assert agent_app.money_left() == {"credits": 600, "model_usd": round(20 - usd, 4)}


def running_investigation(inv):
    inv_id, ask_id = "i_0123456789ab", "a_20260929_0badf00d"
    plan = inv.fixture_planner({"question": "q", "market": "ZA"})["plan"]
    agent_app.INVESTIGATIONS.append(inv.storage_row(
        inv_id, 1, "2026-09-29T10:00:00+02:00", "running", "What is behind #fixture?", "ZA", plan,
        {"credits": 300, "model_usd": 1.5, "minutes": 6}, ask_id, "r_plan_x"))
    inv.RESERVED.add(inv_id, {"credits": 400, "model_usd": 3.0})
    ask = mock.MagicMock()
    ask.snapshot.return_value = {"ask_id": ask_id, "status": "complete", "run": {"run_id": "r_20260929_0badf00d"}}
    agent_app.RUNNING_INVESTIGATIONS[inv_id] = ask
    return inv_id, ask


def test_finish_row_is_written_before_the_reservation_is_released(inv, monkeypatch):
    inv_id, ask = running_investigation(inv)
    seen, real = [], agent_app.append

    def append(table, held, row):
        seen.append((table, row["status"], inv.RESERVED.total(), inv_id in agent_app.RUNNING_INVESTIGATIONS))
        return real(table, held, row)

    monkeypatch.setattr(agent_app, "append", append)
    agent_app.finish_investigation(inv_id, ask)
    assert seen == [("investigations", "complete", {"credits": 400, "model_usd": 3.0}, True)]
    last = agent_app.INVESTIGATIONS[-1]
    assert (last["version"], last["status"], last["run_id"]) == (2, "complete", "r_20260929_0badf00d")
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    assert inv_id not in agent_app.RUNNING_INVESTIGATIONS


@pytest.mark.parametrize("fails", ["read", "write refused", "write raises"])
def test_reservation_is_released_even_when_the_finish_row_fails(inv, monkeypatch, caplog, fails):
    inv_id, ask = running_investigation(inv)
    if fails == "read":
        monkeypatch.setattr(agent_app, "investigation_rows", mock.Mock(side_effect=RuntimeError("store down")))
    elif fails == "write refused":
        monkeypatch.setattr(agent_app, "append", lambda table, held, row: False)
    else:
        monkeypatch.setattr(agent_app, "append", mock.Mock(side_effect=RuntimeError("store down")))
    with caplog.at_level(logging.ERROR, logger="f42-agent"):
        agent_app.finish_investigation(inv_id, ask)
    assert inv.RESERVED.total() == {"credits": 0, "model_usd": 0}
    assert inv_id not in agent_app.RUNNING_INVESTIGATIONS
    assert any(r.levelno >= logging.ERROR and inv_id in r.getMessage() for r in caplog.records)
    assert agent_app.INVESTIGATIONS[-1]["status"] == "running"


# Dossiers from a finished investigation (contract sections 13.1 and 13.2).


def inv_dossier(client, inv_id):
    return client.post("/api/dossiers", json={"from": {"investigation_id": inv_id}})


def test_dossier_from_a_finished_investigation_builds_from_its_answer(client, inv, dossier_store):
    held = draft(client).json()
    inv_id = held["investigation_id"]
    started = client.post(f"/api/investigations/{inv_id}/start").json()
    wait_finished(inv_id)
    r = inv_dossier(client, inv_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert DOSSIER_ID.match(body["dossier_id"]) and body["version"] == 1 and body["state"] == "draft"
    assert body["source"] == {"investigation_id": inv_id}
    assert body["source_ask_id"] == started["ask_id"]
    assert body["title"] == held["question"]
    answer = load("ask_complete.json")["answer"]
    assert body["summary"] == answer["short_answer"]
    (row,) = agent_app.DOSSIER_VERSIONS
    assert row["source_ask_id"] == started["ask_id"]
    assert started["ask_id"] not in dossier_store["asked"]
    edited = client.put(f"/api/dossiers/{body['dossier_id']}", json={"title": "Edited"})
    assert edited.status_code == 200 and edited.json()["source"] == {"investigation_id": inv_id}


def test_dossier_from_an_investigation_evicted_from_memory_reads_the_table_and_runs(client, inv, dossier_store):
    inv_id = draft(client).json()["investigation_id"]
    ask_id = client.post(f"/api/investigations/{inv_id}/start").json()["ask_id"]
    wait_finished(inv_id)
    agent_app.ASKS.clear()
    agent_app.RUNNING_INVESTIGATIONS.clear()
    r = inv_dossier(client, inv_id)
    assert r.status_code == 201, r.text
    assert r.json()["source_ask_id"] == ask_id
    assert r.json()["source"] == {"investigation_id": inv_id}


def test_dossier_from_a_draft_or_running_investigation_gives_409(client, inv, dossier_store, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    inv_id = draft(client).json()["investigation_id"]
    r = inv_dossier(client, inv_id)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    assert "not finished" in r.json()["message"]
    client.post(f"/api/investigations/{inv_id}/start")
    # Running: the ask_id comes from memory, so the table is never read.
    monkeypatch.setattr(agent_app, "investigation_rows", mock.Mock(side_effect=AssertionError("table read")))
    r = inv_dossier(client, inv_id)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    monkeypatch.undo()
    gate.opened.set()
    wait_finished(inv_id)
    assert agent_app.DOSSIER_VERSIONS == []


def test_dossier_from_a_failed_investigation_gives_409(client, inv, dossier_store, monkeypatch):
    gate = use_gate(monkeypatch, inv)
    inv_id = draft(client, "Why does this fail for #fixture in South Africa?").json()["investigation_id"]
    client.post(f"/api/investigations/{inv_id}/start")
    gate.opened.set()
    assert wait_finished(inv_id)["status"] == "failed"
    r = inv_dossier(client, inv_id)
    assert r.status_code == 409 and r.json()["error"] == "not_ready"
    assert agent_app.DOSSIER_VERSIONS == []


def test_dossier_from_an_investigation_whose_answer_is_gone_gives_404(client, inv, dossier_store):
    plan = inv.fixture_planner({"question": "q", "market": "ZA"})["plan"]
    agent_app.INVESTIGATIONS.append(inv.storage_row(
        "i_0123456789ab", 1, "2026-09-29T10:00:00+02:00", "complete", "What is behind #fixture?", "ZA", plan,
        {"credits": 300, "model_usd": 1.5, "minutes": 6}, "a_20260929_0badf00d", "r_x"))
    r = inv_dossier(client, "i_0123456789ab")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert "a_20260929_0badf00d" in dossier_store["asked"]
    assert inv_dossier(client, "i_ffffffffffff").status_code == 404
    assert agent_app.DOSSIER_VERSIONS == []


# Ask skills (contract section 15.2).


@pytest.mark.parametrize("skill", ["brand-implication", "creator-read", "context-pack"])
def test_a_known_skill_reaches_run_ask_as_sent(client, monkeypatch, skill):
    seen = spy_on_requests(monkeypatch)
    assert ask(client, wait=True, skill=skill).status_code == 200
    assert seen[0]["skill"] == skill


def test_no_skill_adds_no_key(client, monkeypatch):
    seen = spy_on_requests(monkeypatch)
    ask(client, wait=True)
    assert "skill" not in seen[0]


@pytest.mark.parametrize("skill", ["genai-visibility", "Brand-Implication", "", 5, ["creator-read"]])
def test_an_unknown_skill_gives_400(client, monkeypatch, skill):
    seen = spy_on_requests(monkeypatch)
    r = ask(client, skill=skill)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert seen == []


# T2 for the brand lens and the context pack, behind F42_T2_READY (contract sections 6 and 15.2).

T2_SKILLS = ("brand-implication", "context-pack")


@pytest.mark.parametrize("skill", [None, "brand-implication", "context-pack", "creator-read"])
def test_t2_is_refused_while_the_flag_is_off(client, monkeypatch, skill):
    monkeypatch.delenv("F42_T2_READY", raising=False)
    seen = spy_on_requests(monkeypatch)
    body = {"tier": "T2"} if skill is None else {"tier": "T2", "skill": skill}
    r = ask(client, **body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "T2" in r.json()["message"]
    assert seen == []


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "on", " 1"])
def test_only_the_value_1_opens_t2(client, monkeypatch, value):
    monkeypatch.setenv("F42_T2_READY", value)
    seen = spy_on_requests(monkeypatch)
    r = ask(client, tier="T2", skill="brand-implication")
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert seen == []


@pytest.mark.parametrize("skill", T2_SKILLS)
def test_t2_is_accepted_for_the_brand_lens_and_context_pack_with_the_flag_on(client, monkeypatch, skill):
    monkeypatch.setenv("F42_T2_READY", "1")
    seen = spy_on_requests(monkeypatch)
    r = ask(client, wait=True, tier="T2", skill=skill)
    assert r.status_code == 200
    assert seen[0]["tier"] == "T2" and seen[0]["skill"] == skill


@pytest.mark.parametrize("extra", [{}, {"skill": "creator-read"}, {"spike": SPIKE}])
def test_t2_stays_refused_for_any_other_ask_with_the_flag_on(client, monkeypatch, extra):
    monkeypatch.setenv("F42_T2_READY", "1")
    seen = spy_on_requests(monkeypatch)
    r = ask(client, tier="T2", **extra)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "T2" in r.json()["message"]
    assert seen == []


@pytest.mark.parametrize("flag", [None, "1"])
@pytest.mark.parametrize("skill", [None, *T2_SKILLS])
def test_t3_is_always_refused_on_ask(client, monkeypatch, flag, skill):
    if flag is None:
        monkeypatch.delenv("F42_T2_READY", raising=False)
    else:
        monkeypatch.setenv("F42_T2_READY", flag)
    seen = spy_on_requests(monkeypatch)
    body = {"tier": "T3"} if skill is None else {"tier": "T3", "skill": skill}
    r = ask(client, **body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert "T3" in r.json()["message"]
    assert seen == []


def test_health_says_whether_t2_is_open(client, monkeypatch):
    monkeypatch.delenv("F42_T2_READY", raising=False)
    assert client.get("/health").json()["t2_ready"] is False
    monkeypatch.setenv("F42_T2_READY", "0")
    assert client.get("/health").json()["t2_ready"] is False
    monkeypatch.setenv("F42_T2_READY", "1")
    assert client.get("/health").json()["t2_ready"] is True


# Client skins (contract section 15.1).

SKIN_KEYS = {"skin_id", "skin_key", "created_at", "status_at", "who", "name", "markets", "terms", "hashtags",
             "accounts", "watch_ids", "template", "status"}
SKIN_ID = re.compile(r"^sk_[0-9a-f]{12}$")
NOT_ACCEPTED = "Not accepted (rule 1, rule 2 or mixed script)"


@pytest.fixture
def skin_env(monkeypatch, tmp_path):
    """L1's word check stood in for, the approved accounts from a test file, empty skin and watch lists."""
    from core.api import skins
    from core.api.tests.test_skins import ACCOUNTS

    seen = []

    def blocked(text):
        seen.append(text)
        return "blockedword" in str(text).casefold()

    monkeypatch.setitem(sys.modules, "core.collect.gdelt", types.SimpleNamespace(blocked=blocked))
    path = tmp_path / "skin_accounts.yaml"
    path.write_text(json.dumps(ACCOUNTS), encoding="utf-8")
    monkeypatch.setattr(skins, "ACCOUNTS_FILE", path)
    agent_app.SKINS.clear()
    agent_app.WATCHES.clear()
    yield seen
    agent_app.SKINS.clear()
    agent_app.WATCHES.clear()


def new_skin(client, **body):
    payload = {"skin_key": "bsa", "name": "Brand South Africa", "markets": ["ZA"], "terms": ["taxi fares"],
               "hashtags": ["#fixture_za_step"], "accounts": [{"platform": "x", "handle": "@BrandSA"}],
               "watch_ids": [], "template": "weekly_report", **body}
    return client.post("/api/skins", json=payload)


def test_create_skin_appends_a_row_and_returns_201(client, skin_env):
    r = new_skin(client)
    assert r.status_code == 201, r.text
    skin = r.json()
    assert set(skin) == SKIN_KEYS and SKIN_ID.match(skin["skin_id"])
    assert skin["who"] == "passcode" and skin["status"] == "active"
    assert skin["accounts"] == [{"platform": "x", "handle": "@BrandSA", "org": "Brand South Africa",
                                 "role": "client"}]
    assert re.search(r"[+-]\d{2}:\d{2}$", skin["created_at"]) and skin["status_at"] == skin["created_at"]
    (row,) = agent_app.SKINS
    assert set(row) == SKIN_KEYS
    for key in ("markets", "terms", "hashtags", "accounts", "watch_ids"):
        assert json.loads(row[key]) == skin[key]
    assert set(skin_env) >= {"bsa", "Brand South Africa", "taxi fares", "#fixture_za_step"}


@pytest.mark.parametrize("body", [
    {"name": "ab"}, {"markets": ["GH"]}, {"terms": ["x" * 61]}, {"template": "daily"}, {"skin_key": None},
])
def test_bad_skins_give_400(client, skin_env, body):
    r = new_skin(client, **body)
    assert r.status_code == 400 and set(r.json()) == {"error", "message"}
    assert agent_app.SKINS == []


def test_skin_body_must_be_a_json_object(client, skin_env):
    assert client.post("/api/skins", content=b"not json").status_code == 400
    assert client.post("/api/skins", json=[1]).json()["error"] == "bad_request"


def test_an_account_off_the_approved_list_is_refused(client, skin_env):
    r = new_skin(client, accounts=[{"platform": "x", "handle": "@a_private_person"}])
    assert r.status_code == 400
    assert r.json() == {"error": "bad_request", "message": "Only approved organisation accounts"}
    assert agent_app.SKINS == []


@pytest.mark.parametrize("body", [
    {"name": "BlockedWord campaign"}, {"terms": ["fine", "blockedword"]}, {"hashtags": ["#BlockedWord"]},
    {"terms": ["Google Trends"]}, {"name": "Brand g.o.o.g.l.e-trends"},
])
def test_refused_words_give_400_without_the_word(client, skin_env, body):
    r = new_skin(client, **body)
    assert r.status_code == 400
    assert r.json() == {"error": "bad_request", "message": NOT_ACCEPTED}
    assert agent_app.SKINS == []


def test_the_labels_of_linked_watches_pass_the_word_check(client, skin_env):
    fine = client.post("/api/watches", json={"target": {"kind": "hashtag", "value": "#fixture_za_step"},
                                             "market": "ZA", "rule": {"state_in": ["emerging"]},
                                             "label": "Step dance"}).json()
    bad = client.post("/api/watches", json={"target": {"kind": "query", "value": "anything"}, "market": "ZA",
                                            "rule": {"state_in": ["rising"]},
                                            "label": "a blockedword search"}).json()
    r = new_skin(client, watch_ids=[fine["watch_id"], bad["watch_id"]])
    assert r.status_code == 400 and r.json()["message"] == NOT_ACCEPTED
    assert "Step dance" in skin_env
    r = new_skin(client, watch_ids=[fine["watch_id"]])
    assert r.status_code == 201 and r.json()["watch_ids"] == [fine["watch_id"]]


def test_an_unknown_watch_is_refused(client, skin_env):
    r = new_skin(client, watch_ids=["w_000000000000"])
    assert r.status_code == 400 and "w_000000000000" in r.json()["message"]


def test_skin_writes_fail_closed_without_the_word_check(client, skin_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "core.collect.gdelt", None)
    r = new_skin(client)
    assert r.status_code == 503
    assert r.json()["message"] == "The word check is not available yet"
    assert agent_app.SKINS == []


def test_put_appends_the_full_body_and_archive_appends_an_archived_row(client, skin_env, monkeypatch):
    first = new_skin(client).json()
    second = new_skin(client, name="Second skin").json()
    monkeypatch.setattr(agent_app, "status_time", lambda market: "2026-10-01T09:00:00+02:00")
    r = client.put(f"/api/skins/{first['skin_id']}", json={
        "skin_key": "bsa", "name": "Brand South Africa, edited", "markets": ["ZA", "KE"], "terms": ["braai"],
        "hashtags": [], "accounts": [], "watch_ids": [], "template": "weekly_report"})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["skin_id"] == first["skin_id"] and edited["created_at"] == first["created_at"]
    assert edited["status_at"] == "2026-10-01T09:00:00+02:00"
    assert (edited["name"], edited["markets"], edited["terms"], edited["accounts"]) == (
        "Brand South Africa, edited", ["ZA", "KE"], ["braai"], [])
    assert client.put(f"/api/skins/{first['skin_id']}", json={"name": "ab"}).status_code == 400
    r = client.post(f"/api/skins/{second['skin_id']}/archive")
    assert r.status_code == 200 and r.json()["status"] == "archived" and r.json()["name"] == "Second skin"
    assert len(agent_app.SKINS) == 4
    assert client.post(f"/api/skins/{second['skin_id']}/archive").status_code == 200
    assert len(agent_app.SKINS) == 4  # archiving twice appends once
    listed = {s["skin_id"]: s for s in client.get("/api/skins").json()["skins"]}
    assert listed[first["skin_id"]]["name"] == "Brand South Africa, edited"
    assert listed[second["skin_id"]]["status"] == "archived"
    one = client.get(f"/api/skins/{first['skin_id']}")
    assert one.status_code == 200 and one.json() == listed[first["skin_id"]]


def test_unknown_skins_give_404(client, skin_env):
    for r in (client.get("/api/skins/sk_000000000000"), client.post("/api/skins/sk_000000000000/archive"),
              client.put("/api/skins/sk_000000000000", json={"skin_key": "bsa", "name": "Brand South Africa",
                                                             "markets": ["ZA"], "template": "weekly_report"})):
        assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert agent_app.SKINS == []


def stored_skin(skin_id="sk_0123456789ab", status="active", status_at=None):
    return {"skin_id": skin_id, "skin_key": "bsa", "created_at": agent_app.now(), "status_at": status_at,
            "who": "passcode", "name": "Stored", "markets": json.dumps(["ZA"]), "terms": json.dumps(["braai"]),
            "hashtags": json.dumps([]), "accounts": json.dumps([]), "watch_ids": json.dumps([]),
            "template": "weekly_report", "status": status}


def test_bigquery_skin_insert_and_list(client, skin_env, monkeypatch):
    client_bq = fake_bigquery(monkeypatch, [stored_skin(), stored_skin("sk_ba9876543210", "archived",
                                                                       agent_app.now())])
    skin = new_skin(client).json()
    table, rows = client_bq.insert_rows_json.call_args.args
    assert table == "test-project.intelligence_42_agent.skins"
    assert set(rows[0]) == SKIN_KEYS and rows[0]["skin_id"] == skin["skin_id"]
    assert agent_app.SKINS == []
    listed = client.get("/api/skins").json()["skins"]
    assert [s["skin_id"] for s in listed] == ["sk_0123456789ab", "sk_ba9876543210"]
    assert listed[0]["terms"] == ["braai"] and isinstance(listed[0]["created_at"], str)
    sql = client_bq.query.call_args.args[0]
    assert "test-project.intelligence_42_agent.skins" in sql
    # The same order as the watches and L1's v_watches_current.
    assert ("PARTITION BY k.skin_id ORDER BY COALESCE(k.status_at, k.created_at) DESC NULLS LAST, "
            "k.status DESC, TO_JSON_STRING(k)) = 1") in sql
    assert sql.rstrip().endswith("ORDER BY COALESCE(status_at, created_at) DESC")
    assert client_bq.query.call_args.kwargs["job_config"].maximum_bytes_billed == 2 * 1024 ** 3


def test_bigquery_missing_skins_table_lists_nothing(client, skin_env, monkeypatch):
    from google.api_core.exceptions import NotFound

    client_bq = fake_bigquery(monkeypatch)
    client_bq.query.side_effect = NotFound("Not found: Table test-project:intelligence_42_agent.skins")
    assert client.get("/api/skins").json() == {"skins": []}
    assert client.get("/api/skins/sk_0123456789ab").status_code == 404
    assert client.post("/api/skins/sk_0123456789ab/archive").status_code == 404
    client_bq.insert_rows_json.assert_not_called()


def test_bigquery_skin_insert_errors_give_500(client, skin_env, monkeypatch):
    client_bq = fake_bigquery(monkeypatch)
    client_bq.insert_rows_json.return_value = [{"index": 0, "errors": ["no such field"]}]
    r = new_skin(client)
    assert r.status_code == 500 and r.json()["error"] == "internal"


# A skin's weekly report: an investigation scoped to the skin, and its dossier names no one it may not.

def no_leaks(text):
    from core.api.tests.test_skins import leaks
    return leaks(text)


def skin_answer():
    from core.api.tests.test_skins import skin_record

    return skin_record()["answer"]


def test_skin_report_draft_carries_the_skin_to_the_planner_and_keeps_it_in_the_plan(client, inv, skin_env,
                                                                                    monkeypatch):
    from core.api import skins

    seen = []

    def planner(request):
        seen.append(request)
        return inv.fixture_planner(request)

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (planner, inv.fixture_estimate, agent_app.fixture_agent))
    skin = new_skin(client).json()
    r = client.post("/api/investigations", json=skins.report_plan(skin))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["plan"]["skin_id"] == skin["skin_id"]
    assert seen[0]["skin"]["skin_id"] == skin["skin_id"] and seen[0]["skin"]["terms"] == ["taxi fares"]
    assert json.loads(agent_app.INVESTIGATIONS[0]["plan"])["skin_id"] == skin["skin_id"]
    plan = {k: v for k, v in body["plan"].items() if k != "skin_id"}
    r = client.put(f"/api/investigations/{body['investigation_id']}/plan", json={"plan": plan})
    assert r.status_code == 200 and r.json()["plan"]["skin_id"] == skin["skin_id"]
    forged = client.put(f"/api/investigations/{body['investigation_id']}/plan",
                        json={"plan": {**plan, "skin_id": "sk_ffffffffffff"}})
    assert forged.json()["plan"]["skin_id"] == skin["skin_id"]


@pytest.mark.parametrize("skin_id, status", [("sk_000000000000", 404), (5, 400), ("bad id!", 400)])
def test_a_report_on_an_unknown_skin_is_refused(client, inv, skin_env, skin_id, status):
    r = client.post("/api/investigations", json={"question": "Weekly report", "market": "ZA", "skin_id": skin_id})
    assert r.status_code == status
    assert agent_app.INVESTIGATIONS == []


def test_skin_report_dossier_names_no_sub_tier_author(client, inv, skin_env, monkeypatch):
    from core.api import pdf, skins

    answer = skin_answer()

    def run_ask(request, emit, should_stop):
        return {"answer": copy.deepcopy(answer),
                "run": {"run_id": "r_" + request["ask_id"][2:], "tier": "T3", "credits": 10, "model_usd": 0.1}}

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (inv.fixture_planner, inv.fixture_estimate, run_ask))
    skin = new_skin(client).json()
    inv_id = client.post("/api/investigations", json=skins.report_plan(skin)).json()["investigation_id"]
    assert client.post(f"/api/investigations/{inv_id}/start").status_code == 202
    wait_finished(inv_id)

    read = client.get(f"/api/investigations/{inv_id}")
    assert read.status_code == 200 and no_leaks(read.text) == []
    assert "@BrandSA" in read.text

    r = client.post("/api/dossiers", json={"from": {"investigation_id": inv_id}})
    assert r.status_code == 201, r.text
    assert no_leaks(r.text) == []
    shown = r.json()
    assert shown["claims"][0]["quotes"][0]["text"] == f"with {skins.MASK} and @BrandSA"
    evidence = {e["id"]: e for e in shown["evidence"]}
    assert "handle" not in evidence["x_micro"] and "url" not in evidence["x_micro"]
    assert evidence["x_org"]["handle"] == "@BrandSA"

    stored = json.loads(agent_app.DOSSIER_VERSIONS[-1]["body"])
    assert stored["claims"][0]["quotes"] == answer["claims"][0]["quotes"]
    assert stored["claims"][0]["quotes"][0]["text"] == "with @pal_of_micro and @BrandSA"

    dossier_id = shown["dossier_id"]
    edited = client.put(f"/api/dossiers/{dossier_id}", json={"title": "Weekly report"})
    assert edited.status_code == 200 and no_leaks(edited.text) == []
    frozen = client.post(f"/api/dossiers/{dossier_id}/freeze")
    assert frozen.status_code == 201, frozen.text
    assert no_leaks(frozen.text) == []
    version = frozen.json()["version"]
    assert json.loads(agent_app.DOSSIER_VERSIONS[-1]["body"])["claims"][0]["quotes"] == answer["claims"][0]["quotes"]
    assert no_leaks(client.get(f"/api/dossiers/{dossier_id}").text) == []
    assert no_leaks(client.get(f"/api/dossiers/{dossier_id}/versions/{version}").text) == []
    page = client.get(f"/api/dossiers/{dossier_id}/versions/{version}/export", params={"format": "html"})
    assert page.status_code == 200 and no_leaks(page.text) == []
    assert "@BrandSA" in page.text
    rendered = []
    monkeypatch.setattr(pdf, "render_pdf", lambda html: rendered.append(html) or b"%PDF-1.4 x")
    assert client.get(f"/api/dossiers/{dossier_id}/versions/{version}/export",
                      params={"format": "pdf"}).status_code == 200
    assert rendered and no_leaks(rendered[0]) == []


# Every path that shows a skin run's record masks it the same way (contract 15.1): the Ask read, both event
# streams, the investigations list, and a dossier made from the run's ask_id.


def skin_run_ask(answer):
    """A T3 run_ask that streams the skin fixture's step, evidence and claim before it answers."""
    from core.api.tests.test_skins import skin_record

    steps = skin_record()["steps"]

    def run_ask(request, emit, should_stop):
        emit({"event": "step", "kind": "read", "text": steps[0]["text"]})
        for item in answer["evidence"]:
            emit({"event": "evidence", "evidence": copy.deepcopy(item)})
        for claim in answer["claims"]:
            emit({"event": "claim", "claim": copy.deepcopy(claim), "check": "verified", "reason": None})
        return {"answer": copy.deepcopy(answer),
                "run": {"run_id": "r_" + request["ask_id"][2:], "tier": "T3", "credits": 10, "model_usd": 0.1}}

    return run_ask


def finished_skin_run(client, inv, monkeypatch):
    from core.api import skins

    monkeypatch.setattr(agent_app, "resolve_investigator",
                        lambda: (inv.fixture_planner, inv.fixture_estimate, skin_run_ask(skin_answer())))
    skin = new_skin(client).json()
    inv_id = client.post("/api/investigations", json=skins.report_plan(skin)).json()["investigation_id"]
    started = client.post(f"/api/investigations/{inv_id}/start").json()
    wait_finished(inv_id)
    return inv_id, started["ask_id"]


def test_the_ask_read_of_a_skin_run_is_masked(client, inv, skin_env, monkeypatch):
    inv_id, ask_id = finished_skin_run(client, inv, monkeypatch)
    r = client.get(f"/api/ask/{ask_id}")
    assert r.status_code == 200 and r.json()["skin_id"].startswith("sk_")
    assert no_leaks(r.text) == []
    assert "@BrandSA" in r.text
    # The run's own stored record keeps the answer verbatim.
    (row,) = [row for row in agent_app.SINK if row["stage"] == "ask"]
    assert "pal_of_micro" in row["record"]


def test_an_ask_that_is_not_a_skin_run_is_read_unchanged(client):
    record = ask(client, wait=True).json()
    assert client.get(f"/api/ask/{record['ask_id']}").json() == record


def test_both_event_streams_of_a_skin_run_are_masked(client, inv, skin_env, monkeypatch):
    inv_id, ask_id = finished_skin_run(client, inv, monkeypatch)
    for path in (f"/api/ask/{ask_id}/events", f"/api/investigations/{inv_id}/events"):
        r = client.get(path)
        events, _ = parse_sse(r.text)
        assert {e["event"] for e in events} == {"step", "evidence", "claim", "done"}, path
        assert no_leaks(r.text) == [], path
        claim = next(e["data"]["claim"] for e in events if e["event"] == "claim")
        assert claim["quotes"][0]["text"] == "with @*** and @BrandSA"
    # Evicted from memory: the investigation stream replays the stored steps, masked too.
    agent_app.ASKS.clear()
    r = client.get(f"/api/investigations/{inv_id}/events")
    assert r.status_code == 200 and "event: step" in r.text and no_leaks(r.text) == []


def test_the_investigations_list_carries_no_evidence_or_claim_text(client, inv, skin_env, monkeypatch):
    finished_skin_run(client, inv, monkeypatch)
    r = client.get("/api/investigations")
    assert r.status_code == 200 and no_leaks(r.text) == []
    assert all("record" not in i and "evidence" not in i for i in r.json()["investigations"])


def test_a_dossier_from_a_skin_run_ask_id_is_masked(client, inv, skin_env, monkeypatch):
    inv_id, ask_id = finished_skin_run(client, inv, monkeypatch)
    r = client.post("/api/dossiers", json={"from": {"ask_id": ask_id}})
    assert r.status_code == 201, r.text
    assert no_leaks(r.text) == [] and r.json()["people"]["skin_id"].startswith("sk_")
    stored = json.loads(agent_app.DOSSIER_VERSIONS[-1]["body"])
    assert stored["claims"][0]["quotes"][0]["text"] == "with @pal_of_micro and @BrandSA"


def test_a_creator_suppressed_after_the_dossier_was_made_is_no_longer_named(client, inv, skin_env, monkeypatch):
    from core.api import pdf, store

    inv_id, _ = finished_skin_run(client, inv, monkeypatch)
    dossier = client.post("/api/dossiers", json={"from": {"investigation_id": inv_id}}).json()
    assert dossier["people"]["allowed"] == ["tiktok:fixture_ng_macro"]
    assert "@fixture_ng_macro" in json.dumps(dossier)
    version = client.post(f"/api/dossiers/{dossier['dossier_id']}/freeze").json()["version"]

    class Suppressed(store.FixtureStore):
        def suppressed_creators(self):
            return {"c_ng_macro"}

    monkeypatch.setattr(store, "get_store", Suppressed)
    monkeypatch.setattr(pdf, "render_pdf", lambda html: b"%PDF-1.4 " + html.encode())
    base = f"/api/dossiers/{dossier['dossier_id']}"
    for r in (client.get(base), client.get(f"{base}/versions/{version}"),
              client.get(f"{base}/versions/{version}/export", params={"format": "html"}),
              client.get(f"{base}/versions/{version}/export", params={"format": "pdf"})):
        assert r.status_code == 200
        assert "fixture_ng_macro" not in r.text and no_leaks(r.text) == []
        assert "@BrandSA" in r.text
    stored = json.loads(agent_app.DOSSIER_VERSIONS[-1]["body"])
    assert stored["people"]["allowed"] == ["tiktok:fixture_ng_macro"]  # the stored version is never changed


def test_a_resumed_skin_stream_still_masks_an_author_it_skipped(skin_env):
    """follow(after=2) skips the evidence that named privperson; later events name them bare and in a URL."""
    from core.api.tests.test_skins import leaks, skin_record

    priv = next(e for e in skin_record()["answer"]["evidence"] if e["id"] == "x_priv")
    held = agent_app.Ask({"ask_id": "a_20260929_0000beef", "question": "Weekly report", "market": "ZA"})
    held.record["skin_id"] = "sk_0123456789ab"
    held.emit({"event": "evidence", "evidence": priv})
    held.emit({"event": "step", "kind": "read", "text": "Reading the launch posts"})
    held.emit({"event": "step", "kind": "read", "text": "privperson moved to web.facebook.com/privperson/posts/1"})
    held.emit({"event": "claim", "claim": {"id": "c1", "text": "As privperson put it, the launch was fine.",
                                           "label": "observed", "evidence_ids": ["x_priv"], "quotes": []},
               "check": "verified", "reason": None})
    held.done = {"seq": 5, "status": "complete", "url": "/api/ask/a_20260929_0000beef"}
    text = "".join(agent_app.follow(held, 2))
    events, _ = parse_sse(text)
    assert [e["id"] for e in events] == [3, 4, 5]
    assert events[0]["data"]["text"] == "*** moved to web.facebook.com/***/posts/1"
    assert events[1]["data"]["claim"]["text"] == "As *** put it, the launch was fine."
    assert leaks(text) == []


def test_planted_handles_under_structural_keys_never_reach_a_reader(client, inv, skin_env, monkeypatch):
    """A hidden handle under creator_id, author_id, item_ids, a so_what label and a step label is masked in the
    Ask JSON view, the dossier JSON and the live events; market and labels stay intact."""
    from core.api import skins

    answer = skin_answer()
    answer["claims"][0]["numbers"] = [{"value": 3, "unit": "posts", "query_id": "q_1", "run_id": "r_1",
                                       "result_hash": "sha256:ab", "creator_id": "x:privperson",
                                       "author_id": "x_privperson"}]
    answer["so_what"][0]["label"] = "@privperson"
    answer["gaps"][0]["label"] = "@privperson"
    next(e for e in answer["evidence"] if e["id"] == "x_org")["item_ids"] = ["tiktok:privperson"]

    def run_ask(request, emit, should_stop):
        emit({"event": "step", "kind": "read", "text": "Reading", "label": "@privperson on x:privperson"})
        for item in answer["evidence"]:
            emit({"event": "evidence", "evidence": copy.deepcopy(item)})
        emit({"event": "claim", "claim": copy.deepcopy(answer["claims"][0]), "check": "verified", "reason": None})
        return {"answer": copy.deepcopy(answer),
                "run": {"run_id": "r_" + request["ask_id"][2:], "tier": "T3", "credits": 10, "model_usd": 0.1}}

    monkeypatch.setattr(agent_app, "resolve_investigator", lambda: (inv.fixture_planner, inv.fixture_estimate, run_ask))
    skin = new_skin(client).json()
    inv_id = client.post("/api/investigations", json=skins.report_plan(skin)).json()["investigation_id"]
    ask_id = client.post(f"/api/investigations/{inv_id}/start").json()["ask_id"]
    wait_finished(inv_id)
    view = client.get(f"/api/ask/{ask_id}")
    stream = client.get(f"/api/ask/{ask_id}/events")
    dossier = client.post("/api/dossiers", json={"from": {"investigation_id": inv_id}})
    assert dossier.status_code == 201, dossier.text
    for r in (view, stream, dossier):
        assert "privperson" not in r.text.lower() and no_leaks(r.text) == []
    record = view.json()
    assert record["market"] == "ZA" and record["answer"]["claims"][1]["label"] == "observed"
    assert record["answer"]["claims"][0]["numbers"][0]["creator_id"] == "x:***"
    assert record["steps"][0]["label"] == "@*** on x:***"
    body = dossier.json()
    assert body["market"] == "ZA" and body["claims"][0]["numbers"][0]["author_id"] == "x_***"
    assert next(e for e in body["evidence"] if e["id"] == "x_org")["item_ids"] == ["tiktok:***"]
    assert body["gaps"][0]["label"] == skins.MASK


# A model that is out of quota or not enabled is "model_unavailable", in plain words, not a raw internal error
# (contract section 1). The wrapper reads the HTTP status the model SDK's error carries, or its cause's.
def sdk_error(name, module, **attrs):
    """An error class shaped like one the model SDK raises, with its module name."""
    return type(name, (Exception,), {"__module__": module, **attrs})


MODEL_SDK = "google.genai"
RateLimitError = sdk_error("ClientError", MODEL_SDK + ".errors", code=429)


class Wrapped(Exception):
    pass


def test_the_only_model_sdk_is_google_genai():
    assert agent_app.MODEL_SDKS == (MODEL_SDK,)


@pytest.mark.parametrize("make, status, module", [
    (lambda: RateLimitError("Vertex quota"), 429, MODEL_SDK + ".errors"),
    (lambda: sdk_error("ClientError", MODEL_SDK, code=429)("429 quota exceeded"), 429, MODEL_SDK),
    (lambda: sdk_error("ClientError", MODEL_SDK + ".errors", code=403)("model not enabled"), 403,
     MODEL_SDK + ".errors"),
    (lambda: sdk_error("ClientError", MODEL_SDK + ".errors", code=404)("publisher model not found"), 404,
     MODEL_SDK + ".errors"),
])
def test_a_model_quota_or_access_failure_is_model_unavailable(client, monkeypatch, make, status, module):
    def agent(request, emit, should_stop):
        raise make()

    use_agent(monkeypatch, agent)
    record = ask(client, wait=True).json()
    assert record["status"] == "failed"
    # The status and the SDK module say why the model was unavailable; the SDK's own message text is never kept.
    assert record["error"] == {"error": "model_unavailable", "message": agent_app.MODEL_UNAVAILABLE,
                               "status": status, "sdk_module": module}
    assert agent_app.SINK[0]["outcome"] == "model_unavailable"
    stored = json.loads(agent_app.SINK[0]["record"])
    assert stored["error"] == record["error"]
    raw = make()
    assert str(raw) not in agent_app.SINK[0]["record"] and str(raw) not in json.dumps(record)


def test_a_wrapped_quota_failure_is_model_unavailable(client, monkeypatch):
    def agent(request, emit, should_stop):
        try:
            raise RateLimitError("Vertex quota")
        except RateLimitError as exc:
            raise Wrapped("research turn failed") from exc

    use_agent(monkeypatch, agent)
    error = ask(client, wait=True).json()["error"]
    # The cause that carried the status is the one recorded, not the wrapper around it.
    assert error == {"error": "model_unavailable", "message": agent_app.MODEL_UNAVAILABLE, "status": 429,
                     "sdk_module": RateLimitError.__module__}


def test_other_failures_stay_internal(client, monkeypatch):
    def agent(request, emit, should_stop):
        raise sdk_error("ServerError", MODEL_SDK + ".errors", code=500)("boom")

    use_agent(monkeypatch, agent)
    error = ask(client, wait=True).json()["error"]
    assert error["error"] == "internal" and "status" not in error and "sdk_module" not in error


@pytest.mark.parametrize("wrap", [False, True])
@pytest.mark.parametrize("fault", ["NotFound", "Forbidden", "TooManyRequests"])
def test_a_bigquery_404_403_or_429_is_never_called_a_model_failure(client, monkeypatch, wrap, fault):
    from google.api_core import exceptions

    def agent(request, emit, should_stop):
        error = getattr(exceptions, fault)("claim_checks")
        if not wrap:
            raise error
        try:
            raise error
        except Exception as exc:
            raise Wrapped("writing checks failed") from exc

    use_agent(monkeypatch, agent)
    error = ask(client, wait=True).json()["error"]
    assert error["error"] == "internal"
    assert error["message"].endswith("(Wrapped)." if wrap else f"({fault}).")


def test_dossier_ticks_quote_the_reserved_word_at(monkeypatch):
    # `at` is reserved in BigQuery and dossier_reviews names its time column at (L1's table on staging), so every
    # use must be in backticks or BigQuery refuses the query.
    import re as _re
    seen = []
    monkeypatch.setenv("F42_DATA", "bigquery")
    monkeypatch.setattr(agent_app, "_stored", lambda table, sql, params: seen.append(sql) or [])
    agent_app.dossier_ticks("d_0123456789ab")
    sql = seen[0]
    assert "`at`" in sql
    assert not _re.search(r"(?<![`\w.])at(?![`\w])", sql.replace("`at`", "")), sql


def test_receipt_rows_with_nan_or_infinity_still_give_strict_json_for_the_runs_row():
    # IEEE_DIVIDE in an Ask query can return NaN or Infinity; the runs record is a JSON column, so a bare NaN would
    # lose the whole row and its spend.
    receipts = {"q_1": {"purpose": "share", "sql": "SELECT 1", "params": {}, "result_hash": "h", "row_count": 1,
                        "rows": [{"share": float("nan"), "lift": float("inf"), "drop": float("-inf"), "n": 3}]}}
    text = agent_app.with_receipts({"record": "{}"}, {"ask_id": "a"}, receipts, "a")
    stored = json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert stored["query_receipts"]["q_1"]["rows"] == [{"share": None, "lift": None, "drop": None, "n": 3}]


# F2b: ask_record named no partition, so every read of an Ask scanned the whole runs table (RCU0013).
def test_bigquery_ask_record_reads_only_the_partitions_around_the_ask_id_date():
    from core.api import store

    class Job:
        def result(self):
            return []

    class Client:
        calls = []

        def query(self, sql, job_config=None):
            self.calls.append((sql, {p.name: p.value for p in job_config.query_parameters}))
            return Job()

    client = Client()
    bq = store.BigQueryStore(project="p", client=client)
    assert bq.ask_record("a_20261008_b1d10398") is None
    sql, params = client.calls[-1]
    assert "r.run_date BETWEEN @lo AND @hi" in sql
    assert params["lo"].isoformat() == "2026-10-07" and params["hi"].isoformat() == "2026-10-09"
    assert params["ask_id"] == "a_20261008_b1d10398"
    # An id that does not carry a date is still read, whole table as before.
    assert bq.ask_record("a_x") is None
    sql, params = client.calls[-1]
    assert "run_date" not in sql and set(params) == {"ask_id"}
