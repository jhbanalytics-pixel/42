import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import types

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import core.api
from core.api import agent_app, app as api_mod, auth, store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ASK_ID = "a_20261001_01234567"
PASS = "finding-save-passcode"
GOOD = {"X-Passcode": PASS}
PRODUCER_MODULE = "core.agent.saved_findings"


def source_record():
    record = json.loads((FIXTURES / "ask_complete.json").read_text(encoding="utf-8"))
    record["ask_id"] = ASK_ID
    return record


def warehouse_posts(record):
    return [{"post_id": evidence["id"], "platform": evidence["platform"], "creator_id": None,
             "url": evidence["url"], "text": evidence["text"], "published_at": evidence["posted_at"],
             "views": evidence.get("engagement", {}).get("views"),
             "likes": evidence.get("engagement", {}).get("likes"),
             "comments": evidence.get("engagement", {}).get("comments"),
             "shares": evidence.get("engagement", {}).get("shares"),
             "thumbnail_url": evidence.get("thumbnail_url"), "duration_s": evidence.get("duration_s"),
             "creator_tier_at_post": evidence.get("creator_tier"),
             "geo_market": (None if "market_assumed" in evidence.get("flags", []) else evidence.get("market")),
             "handle": evidence.get("handle"), "item_ids": [], "langs": [], "formats": []}
            for evidence in record["answer"]["evidence"]]


def producer_row(record):
    as_of = record["answer"]["as_of"]
    return {
        "finding_id": "f_contract_saved_ask",
        "question": record["question"],
        "answer": (f"Answer status: {record['answer']['status']}\n"
                   "Scope: ZA only, 21 to 27 September\nCaveat: Instagram was empty."),
        "as_of": as_of,
        "claims": [
            {"text": "Checked producer claim one", "label": "corroborated", "item_ids": [],
             "evidence_post_ids": ["tt_fixture_1"], "query_ids": ["q_contract_1"],
             "run_ids": ["r_contract"], "result_hashes": ["sha256:" + "1" * 64]},
            {"text": "Checked producer claim two", "label": "inferred", "item_ids": [],
             "evidence_post_ids": ["x_fixture_2"], "query_ids": ["q_contract_2"],
             "run_ids": ["r_contract"], "result_hashes": ["sha256:" + "2" * 64]},
        ],
        "valid_from": record["finished_at"],
        "valid_to": None,
        "status": "current",
    }


def install_producer(monkeypatch):
    calls = []
    producer = types.ModuleType(PRODUCER_MODULE)

    def prepare(ask_id, record):
        calls.append((ask_id, record["ask_id"]))
        return producer_row(record)

    producer.prepare_saved_ask_finding = prepare
    monkeypatch.setitem(sys.modules, PRODUCER_MODULE, producer)
    assert importlib.import_module(PRODUCER_MODULE) is producer
    return calls


def real_producer_spec():
    try:
        return importlib.util.find_spec(PRODUCER_MODULE)
    except (ImportError, ModuleNotFoundError, ValueError):
        return None


class SaveStore:
    def __init__(self, record=None, posts=None):
        self.record = copy.deepcopy(record)
        self.posts = copy.deepcopy(posts or [])
        self.rows = []
        self.read_count = 0
        self.read_error_at = None
        self.read_transform = None
        self.insert_mode = "store"
        self.ask_reads = []
        self.post_reads = []
        self.inserts = []

    def ask_record(self, ask_id):
        self.ask_reads.append(ask_id)
        return copy.deepcopy(self.record) if self.record and self.record.get("ask_id") == ask_id else None

    def posts_by_id(self, post_ids):
        self.post_reads.append(list(post_ids))
        wanted = set(post_ids)
        return [copy.deepcopy(post) for post in self.posts if post["post_id"] in wanted]

    def suppressed_creators(self):
        return set()  # the suppression list exists and is empty

    def suppressions(self):
        return []  # and so is the table behind it

    def hidden_people_rows(self):
        return {"ids": set(), "people": []}  # nobody is hidden

    def finding_rows(self, finding_id):
        self.read_count += 1
        if self.read_count == self.read_error_at:
            raise RuntimeError("readback unavailable")
        rows = [copy.deepcopy(row) for row in self.rows if row["finding_id"] == finding_id]
        if self.read_transform is not None:
            rows = self.read_transform(self.read_count, rows)
        return rows

    def insert_finding(self, row):
        self.inserts.append(copy.deepcopy(row))
        if self.insert_mode in ("store", "accepted_error"):
            self.rows.append(copy.deepcopy(row))
        if self.insert_mode == "accepted_error":
            raise RuntimeError("insert response lost after acceptance")
        if self.insert_mode == "reject":
            return False
        return True


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("F42_DATA", raising=False)
    monkeypatch.setattr(agent_app, "get_ask", lambda _ask_id: (_ for _ in ()).throw(
        AssertionError("save must read the durable Ask record")))
    return TestClient(agent_app.app)


def install_store(monkeypatch, fake):
    monkeypatch.setattr(store, "get_store", lambda: fake)


@pytest.mark.parametrize("body", [
    None,
    {},
    {"ask_id": ASK_ID},
    {"from": {}},
    {"from": {"ask_id": ASK_ID, "claims": [{"text": "injected"}]}},
    {"from": {"ask_id": ASK_ID}, "claims": [{"text": "injected"}]},
    {"from": {"ask_id": "bad/id"}},
])
def test_save_rejects_malformed_or_client_supplied_projection(client, monkeypatch, body):
    fake = SaveStore(source_record(), warehouse_posts(source_record()))
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json=body)

    assert response.status_code == 400
    assert fake.ask_reads == [] and fake.post_reads == [] and fake.inserts == []


def test_save_reads_only_a_durable_source_and_returns_404_when_missing(client, monkeypatch):
    fake = SaveStore()
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 404
    assert fake.ask_reads == [ASK_ID]
    assert fake.post_reads == [] and fake.inserts == []


@pytest.mark.parametrize("change", [
    {"status": "running"},
    {"answer": {"status": "insufficient_evidence", "claims": [], "evidence": []}},
    {"answer": {"status": "complete", "claims": [{"id": "cut", "check": "cut"}], "evidence": []}},
])
def test_save_refuses_running_or_insufficient_answers_before_producer_loading(client, monkeypatch, change):
    record = source_record()
    record.update(change)
    fake = SaveStore(record, warehouse_posts(record))
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 409
    assert fake.inserts == []


def test_save_requires_each_answer_citation_to_resolve_to_a_warehouse_post(client, monkeypatch):
    record = source_record()
    install_producer(monkeypatch)
    fake = SaveStore(record, [])
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 409
    assert fake.inserts == []
    assert fake.post_reads


def test_save_refuses_a_skin_backed_raw_ask_before_post_reads_or_writes(client, monkeypatch):
    record = source_record()
    record["skin_id"] = "s_synthetic"
    record["question"] = "What did Synthetic Reviewer Nova say?"
    record["answer"]["evidence"][0]["text"] = "Synthetic Reviewer Nova said the shared pot was easy to copy."
    record["answer"]["claims"][0]["quotes"][0]["text"] = "Synthetic Reviewer Nova said the shared pot"
    fake = SaveStore(record, warehouse_posts(record))
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 409
    assert fake.ask_reads == [ASK_ID]
    assert fake.post_reads == []
    assert fake.read_count == 0 and fake.inserts == [] and fake.rows == []


def test_missing_saved_findings_producer_returns_503_without_reading_or_appending(client, monkeypatch):
    record = source_record()
    fake = SaveStore(record, warehouse_posts(record))
    install_store(monkeypatch, fake)
    monkeypatch.setitem(sys.modules, PRODUCER_MODULE, None)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 503
    assert response.json()["error"] == "producer_not_ready"
    assert fake.ask_reads == [ASK_ID]
    assert fake.post_reads == [] and fake.read_count == 0
    assert fake.inserts == [] and fake.rows == []


def test_combined_release_uses_real_producer_or_reports_it_missing(client, monkeypatch):
    record = source_record()
    for claim in record["answer"]["claims"]:
        for number in claim.get("numbers", []):
            number["run_id"] = record["run"]["run_id"]
    record["market"] = "ZA"
    record["run"]["window"] = {"from": "2026-09-21", "to": "2026-09-27"}
    record["run"]["notices"] = ["Synthetic run notice retained."]
    record["answer"]["context"] = "Synthetic ZA scope context."
    record["answer"]["gaps"] = [{"what": "Synthetic Instagram limitation retained.",
                                   "searched": "Synthetic Instagram window query.", "why": "empty"}]
    record["answer"]["evidence"].append({
        "id": "x_assumed_fixture_3", "platform": "x", "handle": "@synthetic_assumed",
        "url": "https://example.invalid/x/synthetic_assumed/status/3",
        "posted_at": "2026-09-26T13:00:00+02:00", "market": "ZA", "source_market": "ZA",
        "text": "Synthetic source market evidence with assumed geography.", "engagement": {},
        "flags": ["market_assumed"],
    })
    record["answer"]["claims"][0]["evidence_ids"].append("x_assumed_fixture_3")
    posts = warehouse_posts(record)
    fake = SaveStore(record, posts)
    install_store(monkeypatch, fake)
    model_calls = []
    monkeypatch.setattr(agent_app, "resolve_run_ask", lambda: model_calls.append("called"))

    if real_producer_spec() is None:
        monkeypatch.setitem(sys.modules, PRODUCER_MODULE, None)
        response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})
        assert response.status_code == 503
        assert response.json()["error"] == "producer_not_ready"
        assert fake.inserts == [] and fake.rows == []
        assert fake.post_reads == [] and fake.read_count == 0
        assert model_calls == []
        return

    producer = importlib.import_module(PRODUCER_MODULE)
    expected = producer.prepare_saved_ask_finding(ASK_ID, record)
    assert len(expected["claims"]) == 2
    assert len(fake.posts) == 3
    assert all(not post.get("flags") for post in record["answer"]["evidence"][:2])
    assert [post["geo_market"] for post in fake.posts] == ["ZA", "ZA", None]
    assert record["answer"]["evidence"][2]["flags"] == ["market_assumed"]
    assert record["answer"]["evidence"][2]["source_market"] == "ZA"

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 201
    assert response.json() == {"finding_id": expected["finding_id"], "source_ask_id": ASK_ID}
    assert fake.inserts == [expected]
    assert fake.rows == [expected]
    stored_answer = fake.rows[0]["answer"].casefold()
    for preserved in ("market_assumed", "za", "synthetic za scope context", "2026-09-21", "2026-09-27",
                      "synthetic instagram limitation retained", "synthetic instagram window query",
                      "synthetic run notice retained"):
        assert preserved in stored_answer
    assert fake.read_count == 2
    assert len(fake.post_reads) == 1
    assert set(fake.post_reads[0]) == {"tt_fixture_1", "x_fixture_2", "x_assumed_fixture_3"}
    assert model_calls == []
    from core.api.history import build_history_findings
    fake.findings = lambda *_: copy.deepcopy(fake.rows)
    reopened = build_history_findings(fake)["findings"][0]
    envelope = json.loads(expected["answer"])
    assert reopened["answer"] == envelope["answer_text"]
    assert reopened["valid_from"] == expected["valid_from"]
    assert reopened["valid_to"] == expected["valid_to"]
    assert reopened["evidence"] == envelope["evidence"]
    assert reopened["saved_context"]["window"] == record["run"]["window"]
    assert reopened["saved_context"]["gaps"] == record["answer"]["gaps"]
    assert reopened["saved_context"]["notices"] == record["run"]["notices"]
    assert reopened["evidence"][-1]["flags"] == ["market_assumed"]
    assert reopened["evidence"][-1]["source_market"] == "ZA"
    assert model_calls == []


def test_save_appends_a_whole_verified_answer_and_retry_returns_the_existing_row(client, monkeypatch):
    record = source_record()
    record["answer"]["status"] = "partial"
    producer_calls = install_producer(monkeypatch)
    expected = producer_row(record)
    fake = SaveStore(record, warehouse_posts(record))
    install_store(monkeypatch, fake)
    body = {"from": {"ask_id": ASK_ID}}

    created = client.post("/api/findings", json=body)
    repeated = client.post("/api/findings", json=body)

    assert created.status_code == 201
    assert set(created.json()) == {"finding_id", "source_ask_id"}
    assert created.json()["source_ask_id"] == ASK_ID
    assert repeated.status_code == 200 and repeated.json() == created.json()
    assert len(fake.inserts) == 1
    row = fake.inserts[0]
    assert row == expected
    assert row["finding_id"] == created.json()["finding_id"]
    assert row["question"] == record["question"]
    assert row["claims"] == expected["claims"]
    assert row["claims"][0]["text"] == "Checked producer claim one"
    assert row["claims"][1]["text"] == "Checked producer claim two"
    assert row["answer"] == producer_row(record)["answer"]
    assert row["status"] == "current"
    assert producer_calls and all(call == (ASK_ID, ASK_ID) for call in producer_calls)
    assert {post["post_id"]: post["text"] for post in fake.posts} == {
        evidence["id"]: evidence["text"] for evidence in record["answer"]["evidence"]
    }


def test_partial_answer_is_saved_with_its_partial_status(client, monkeypatch):
    record = source_record()
    record["answer"]["status"] = "partial"
    install_producer(monkeypatch)
    fake = SaveStore(record, warehouse_posts(record))
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 201
    assert fake.inserts[0]["answer"] == producer_row(record)["answer"]
    assert fake.inserts[0]["status"] == "current"


def test_existing_mismatched_deterministic_id_is_a_conflict(client, monkeypatch):
    record = source_record()
    install_producer(monkeypatch)
    fake = SaveStore(record, warehouse_posts(record))
    install_store(monkeypatch, fake)
    body = {"from": {"ask_id": ASK_ID}}
    created = client.post("/api/findings", json=body)
    fake.rows[0]["answer"] = "a conflicting payload"

    response = client.post("/api/findings", json=body)

    assert created.status_code == 201
    assert response.status_code == 409
    assert len(fake.inserts) == 1


def test_save_treats_an_uncertain_insert_as_created_only_after_exact_readback(client, monkeypatch):
    record = source_record()
    install_producer(monkeypatch)
    fake = SaveStore(record, warehouse_posts(record))
    fake.insert_mode = "accepted_error"
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 201
    assert len(fake.inserts) == 1 and fake.read_count == 2


@pytest.mark.parametrize("mode", ["missing", "mismatch", "multiple"])
def test_save_fails_closed_when_insert_readback_is_not_exactly_one_matching_row(client, monkeypatch, mode):
    record = source_record()
    install_producer(monkeypatch)
    fake = SaveStore(record, warehouse_posts(record))
    if mode == "missing":
        fake.insert_mode = "reject"
        fake.read_transform = lambda count, rows: [] if count == 2 else rows
    elif mode == "mismatch":
        fake.read_transform = lambda count, rows: ([{**rows[0], "answer": "different"}]
                                                    if count == 2 and rows else rows)
    else:
        fake.read_transform = lambda count, rows: ([*rows, copy.deepcopy(rows[0])]
                                                    if count == 2 and rows else rows)
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 503
    assert fake.read_count == 2


@pytest.mark.parametrize("read_number", [1, 2])
def test_save_fails_closed_when_a_findings_read_fails(client, monkeypatch, read_number):
    record = source_record()
    install_producer(monkeypatch)
    fake = SaveStore(record, warehouse_posts(record))
    fake.read_error_at = read_number
    install_store(monkeypatch, fake)

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 503
    assert fake.read_count == read_number
    assert len(fake.inserts) == (0 if read_number == 1 else 1)


def test_bigquery_insert_uses_finding_id_as_the_deterministic_row_id():
    class Client:
        def __init__(self):
            self.calls = []

        def insert_rows_json(self, table, rows, row_ids):
            self.calls.append((table, rows, row_ids))
            return []

    client = Client()
    bq = object.__new__(store.BigQueryStore)
    bq.project = "unit-test-project"
    bq.client = client
    bq._catalog = lambda: {"objects": {"intelligence_42_agent.findings"}}
    row = {"finding_id": "f_deterministic", "question": "question"}

    assert bq.insert_finding(row) is True
    assert client.calls == [("unit-test-project.intelligence_42_agent.findings", [row], ["f_deterministic"])]


def test_bigquery_findings_reads_pin_to_agent_table_when_core_has_a_same_named_table():
    bq = object.__new__(store.BigQueryStore)
    bq.project = "unit-test-project"
    bq._catalog = lambda: {
        "objects": {"intelligence_42_core.findings", "intelligence_42_agent.findings"},
        "findings_columns": {"status"},
    }
    calls = []
    bq._query = lambda sql, **params: calls.append((sql, params)) or []

    assert bq.finding_rows("f_read") == []
    assert bq.findings() == []

    assert len(calls) == 2
    assert all("FROM `unit-test-project.intelligence_42_agent.findings` f" in sql for sql, _ in calls)
    assert all("intelligence_42_core.findings" not in sql for sql, _ in calls)
    assert calls[0][1] == {"finding_id": ("STRING", "f_read")}
    assert "LIMIT" not in calls[0][0].upper()
    assert "valid_to IS NULL" not in calls[0][0]


def test_bigquery_findings_fail_closed_when_only_core_findings_exists():
    bq = object.__new__(store.BigQueryStore)
    bq.project = "unit-test-project"
    bq.client = SimpleNamespace(insert_rows_json=lambda *args, **kwargs: [])
    bq._catalog = lambda: {
        "objects": {"intelligence_42_core.findings"},
        "findings_columns": {"status"},
    }
    bq._query = lambda *args, **kwargs: pytest.fail("must not query the core Findings table")

    assert bq.finding_rows("f_missing") is None
    assert bq.findings() is None
    with pytest.raises(RuntimeError, match="findings table is unavailable"):
        bq.insert_finding({"finding_id": "f_missing"})


def test_fixture_store_refuses_ephemeral_save_across_new_store_instances(client, monkeypatch, tmp_path):
    record = source_record()
    posts = warehouse_posts(record)
    install_producer(monkeypatch)
    expected = producer_row(record)
    (tmp_path / "history_findings.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(store.FixtureStore, "ask_record", lambda self, ask_id: copy.deepcopy(record))
    monkeypatch.setattr(store.FixtureStore, "posts_by_id", lambda self, post_ids: copy.deepcopy(posts))
    monkeypatch.setattr(store, "get_store", lambda: store.FixtureStore(root=tmp_path))

    response = client.post("/api/findings", json={"from": {"ask_id": ASK_ID}})

    assert response.status_code == 503
    fresh = store.FixtureStore(root=tmp_path)
    assert fresh.finding_rows(expected["finding_id"]) == []
    assert fresh.findings() == []


def test_save_public_proxy_keeps_passcode_gate_and_forwards_only_the_body(monkeypatch):
    seen = []
    agent = FastAPI()

    @agent.post("/api/findings")
    async def save(request: Request):
        seen.append(await request.json())
        return JSONResponse({"finding_id": "f_saved", "source_ask_id": ASK_ID}, status_code=201)

    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.delenv("F42_AUTH_MODE", raising=False)
    monkeypatch.delenv("IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("IAP_ALLOWED_EMAILS", raising=False)
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.setattr(core.api, "agent_app", SimpleNamespace(app=agent), raising=False)
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    client = TestClient(api_mod.app)
    body = {"from": {"ask_id": ASK_ID}}

    denied = client.post("/api/findings", json=body)
    forwarded = client.post("/api/findings", json=body, headers=GOOD)

    assert denied.status_code == 401
    assert forwarded.status_code == 201
    assert forwarded.json() == {"finding_id": "f_saved", "source_ask_id": ASK_ID}
    assert seen == [body]
