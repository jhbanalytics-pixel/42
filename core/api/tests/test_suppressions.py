"""Hiding a person from the app (contract.md section 16): POST /api/suppressions on f42-agent, forwarded by f42-api,
and GET /api/suppressions read by f42-api itself. Outside BigQuery a hide lands in store.SUPPRESSIONS, which the
fixture store's suppression view reads too, so the next people read leaves the person out."""

import ast
import re
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

import core.api
from core.api import agent_app, auth
from core.api import app as api_mod
from core.api import store as store_mod
from core.api.store import FIXTURES, BigQueryStore, FixtureStore

PASS = "s3cret-passcode"
GOOD = {"X-Passcode": PASS}
ROW_KEYS = {"suppression_id", "status_at", "status", "creator_id", "platform", "handle", "reason", "who"}
SUP_ID = re.compile(r"^sup_app_[0-9a-f]{12}$")
REASON = "Asked through Ogilvy legal to be left out of 42"
UNAVAILABLE = {"error": "suppression_unavailable", "message": "Hiding people is not switched on yet."}
BUSY = {"error": "suppression_unavailable", "message": "Hiding people is busy; try again in a minute."}
MAY_NOT_BE_SAVED = "The hide may not have been saved; check the hidden list before trying again."
NOTHING_SAVED = "The person could not be hidden; nothing was saved. Try again."
API_DIR = Path(agent_app.__file__).resolve().parent


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("F42_DATA", raising=False)
    monkeypatch.delenv("F42_PROJECT", raising=False)
    monkeypatch.delenv("AGENT_URL", raising=False)
    monkeypatch.setenv("UI_PASSCODE", PASS)
    monkeypatch.setattr(store_mod, "_CATALOG", {})
    monkeypatch.setattr(core.api, "agent_app", agent_app, raising=False)
    monkeypatch.setattr(core.api, "store", store_mod, raising=False)
    auth.auth_limiter.hits.clear()
    auth.ask_limiter.hits.clear()
    store_mod.SUPPRESSIONS.clear()
    yield
    store_mod.SUPPRESSIONS.clear()


@pytest.fixture
def agent():
    return TestClient(agent_app.app)


@pytest.fixture
def api():
    return TestClient(api_mod.app)


def hide(client, **body):
    payload = {"reason": REASON, "who": "Thandi", **body}
    return client.post("/api/suppressions", json=payload)


# The route on f42-agent.


def test_hide_by_creator_id_appends_one_row_and_returns_201_with_matched(agent):
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 201
    body = r.json()
    assert set(body) == ROW_KEYS | {"matched"}
    assert SUP_ID.match(body["suppression_id"])
    assert body["status"] == "suppressed"
    assert body["creator_id"] == "c_ng_macro" and body["platform"] is None and body["handle"] is None
    assert body["reason"] == REASON and body["who"] == "Thandi"
    assert re.search(r"[+-]\d{2}:\d{2}$", body["status_at"])
    assert body["matched"] == 1
    assert len(store_mod.SUPPRESSIONS) == 1
    row = store_mod.SUPPRESSIONS[0]
    assert set(row) == ROW_KEYS
    assert {k: row[k] for k in ROW_KEYS} == {k: body[k] for k in ROW_KEYS}


def test_each_hide_gets_its_own_id(agent):
    first = hide(agent, creator_id="c_ng_macro").json()["suppression_id"]
    second = hide(agent, creator_id="c_ng_macro").json()["suppression_id"]
    assert first != second and len(store_mod.SUPPRESSIONS) == 2


@pytest.mark.parametrize("platform, handle, stored_platform, matched", [
    ("instagram", "@Fixture_NG_Mega", "instagram", 1),  # the fixture writes it without the @
    ("tiktok", "fixture_ng_macro", "tiktok", 1),  # the fixture writes it with the @
    ("twitter", "fixture_ng_k1", "x", 1),  # twitter is written as x
    ("x", "@fixture_ng_k1", "x", 1),
    ("TikTok", " @fixture_ng_m2 ", "tiktok", 1),
    ("tiktok", "@nobody_42_holds", "tiktok", 0),  # hidden if they appear later; not a failure
    ("reddit", "u/someone_new", "reddit", 0),
])
def test_hide_by_platform_and_handle_counts_the_creators_it_matches_now(agent, platform, handle, stored_platform,
                                                                         matched):
    r = hide(agent, platform=platform, handle=handle)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["creator_id"] is None
    assert body["platform"] == stored_platform and body["handle"] == handle.strip()
    assert body["matched"] == matched
    assert len(store_mod.SUPPRESSIONS) == 1


@pytest.mark.parametrize("reason", [
    "Removal request received 2026-09-29 14:30, reference 4471",
    "Logged 2026-09-29T14:30:05 and again 2026-09-30 09:00",
    "Asked on 2026-09-29 4471 times over",
    "Received by post on 29/09/2026",
    "Call at 14:30 or 09:15:30 tomorrow",
])
def test_a_reason_with_a_date_and_time_is_not_a_phone_number(agent, reason):
    r = hide(agent, creator_id="c_ng_macro", reason=reason)
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("field, text", [
    ("reason", "Call 082/123/4567 about this"),
    ("reason", "She phoned from 0821-23-4567 to ask"),
    ("reason", "Nigeria line 0803-12-12345"),
    ("reason", "Received 2026-09-29 then called 0821-23-4567"),
    ("who", "jane.doe@example.com"),
    ("who", "Thandi 082 123 4567"),
    ("who", "Thandi 082/123/4567"),
    ("who", "0821-23-4567"),
])
def test_contact_details_in_reason_or_who_are_refused(agent, field, text):
    r = hide(agent, creator_id="c_ng_macro", **{field: text})
    assert r.status_code == 400, r.text
    assert r.json() == {"error": "bad_request",
                        "message": f"{field} must not hold an email address or a phone number."}
    assert store_mod.SUPPRESSIONS == []


@pytest.mark.parametrize("body", [
    {"creator_id": "c_ng_macro", "reason": "Asked by jane.doe@example.com to be removed"},
    {"creator_id": "c_ng_macro", "reason": "Write to JANE@EXAMPLE.CO.ZA about this"},
    {"creator_id": "c_ng_macro", "reason": "Call +27 82 123 4567 about this"},
    {"creator_id": "c_ng_macro", "reason": "She phoned from 0821234567 to ask"},
    {"creator_id": "c_ng_macro", "reason": "Reach him on (011) 555-1234 please"},
    {"creator_id": "c_ng_macro", "reason": "Nigeria line 0803 123 4567"},
    {"creator_id": "c_ng_macro", "reason": "ok"},
    {"creator_id": "c_ng_macro", "reason": "  x  "},
    {"creator_id": "c_ng_macro", "reason": "a" * 301},
    {"creator_id": "c_ng_macro", "reason": None},
    {"creator_id": "c_ng_macro", "reason": 12345},
    {"creator_id": "c_ng_macro", "who": "A"},
    {"creator_id": "c_ng_macro", "who": "a" * 61},
    {"creator_id": "c_ng_macro", "who": None},
    {"creator_id": "c_ng_macro", "who": ["Thandi"]},
    {"platform": "tiktok", "handle": "https://www.tiktok.com/@someone"},
    {"platform": "tiktok", "handle": "tiktok.com/@someone"},
    {"platform": "tiktok", "handle": "www.tiktok.com"},
    {"platform": "instagram", "handle": "instagram.com"},
    {"platform": "tiktok", "handle": "some one"},
    {"platform": "tiktok", "handle": "@some\tone"},
    {"platform": "tiktok", "handle": "a/b"},
    {"platform": "tiktok", "handle": "a\\b"},
    {"platform": "tiktok", "handle": "@"},
    {"platform": "tiktok", "handle": ""},
    {"platform": "tiktok", "handle": "@@someone"},
    {"platform": "tiktok", "handle": "a" * 101},
    {"platform": "myspace", "handle": "someone"},
    {"platform": "google_trends", "handle": "someone"},
    {"platform": None, "handle": "someone"},
    {"handle": "someone"},
    {"platform": "tiktok"},
    {"platform": "tiktok", "handle": 42},
    {"creator_id": "c_ng_macro", "platform": "tiktok", "handle": "someone"},
    {"creator_id": "c_ng_macro", "handle": "someone"},
    {},
    {"creator_id": ""},
    {"creator_id": "bad id!"},
    {"creator_id": 7},
])
def test_invalid_bodies_give_400_in_plain_words_and_write_nothing(agent, body):
    r = hide(agent, **body)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "bad_request"
    assert isinstance(r.json()["message"], str) and r.json()["message"]
    assert "Traceback" not in r.text
    assert store_mod.SUPPRESSIONS == []


def test_the_body_must_be_a_json_object(agent):
    assert agent.post("/api/suppressions", content=b"not json").status_code == 400
    assert agent.post("/api/suppressions", json=["c_ng_macro"]).status_code == 400
    assert store_mod.SUPPRESSIONS == []


def test_an_unknown_creator_id_gives_404_and_writes_nothing(agent):
    r = hide(agent, creator_id="c_nobody")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert store_mod.SUPPRESSIONS == []


def test_a_creators_read_that_fails_gives_500_and_writes_nothing(agent, monkeypatch):
    class Broken(FixtureStore):
        def creators_by_handle(self, keys):
            raise RuntimeError("creators unreadable")

    monkeypatch.setattr(store_mod, "get_store", lambda: Broken())
    r = hide(agent, platform="tiktok", handle="@fixture_ng_macro")
    assert r.status_code == 500 and r.json()["error"] == "internal"
    assert "creators unreadable" not in r.text
    assert store_mod.SUPPRESSIONS == []


def test_there_is_no_lift_route(agent, api):
    sid = hide(agent, creator_id="c_ng_macro").json()["suppression_id"]
    for client in (agent, api):
        headers = GOOD if client is api else {}
        for method, path in (("POST", f"/api/suppressions/{sid}/lift"), ("DELETE", f"/api/suppressions/{sid}"),
                             ("PUT", f"/api/suppressions/{sid}"), ("POST", f"/api/suppressions/{sid}")):
            assert client.request(method, path, headers=headers).status_code in (404, 405)
    assert len(store_mod.SUPPRESSIONS) == 1
    routes = {getattr(r, "path", "") for r in agent_app.app.routes} | {getattr(r, "path", "") for r in api_mod.app.routes}
    assert not [p for p in routes if "suppression" in p and p != "/api/suppressions"]


# BigQuery: one parameterised INSERT; 503 on an access refusal, a quota refusal or a missing table; 500 on anything
# else, saying whether the row may exist.


def bigquery_agent(monkeypatch, query):
    monkeypatch.setenv("F42_DATA", "bigquery")
    monkeypatch.setenv("F42_PROJECT", "test-project")
    fake = mock.MagicMock()
    fake.query.side_effect = query
    monkeypatch.setattr(agent_app, "bigquery_client", lambda: fake)
    monkeypatch.setattr(store_mod, "get_store", lambda: FixtureStore())
    return fake


def test_bigquery_hide_is_one_parameterised_insert(agent, monkeypatch):
    job = mock.MagicMock()
    fake = bigquery_agent(monkeypatch, lambda sql, job_config=None: job)
    reason = "Removal request under POPIA, logged by Thandi"
    r = hide(agent, platform="twitter", handle="@Fixture_NG_K1", reason=reason, who="Thandi M")
    assert r.status_code == 201, r.text
    body = r.json()
    assert fake.query.call_count == 1 and job.result.call_count == 1
    assert not fake.insert_rows_json.called
    sql = fake.query.call_args.args[0]
    config = fake.query.call_args.kwargs["job_config"]
    assert re.fullmatch(
        r"INSERT INTO `test-project\.intelligence_42_core\.suppressions` "
        r"\(suppression_id, status_at, status, creator_id, platform, handle, reason, who\) "
        r"VALUES \(@suppression_id, @status_at, @status, @creator_id, @platform, @handle, @reason, @who\)", sql)
    assert not re.search(r"\b(DELETE|UPDATE|MERGE|TRUNCATE|DROP|SELECT)\b", sql, re.IGNORECASE)
    for literal in (reason, "Thandi", "Fixture_NG_K1", body["suppression_id"]):
        assert literal not in sql
    params = {p.name: (p.type_, p.value) for p in config.query_parameters}
    assert params["suppression_id"] == ("STRING", body["suppression_id"])
    assert params["status"] == ("STRING", "suppressed")
    assert params["creator_id"] == ("STRING", None)
    assert params["platform"] == ("STRING", "x") and params["handle"] == ("STRING", "@Fixture_NG_K1")
    assert params["reason"] == ("STRING", reason) and params["who"] == ("STRING", "Thandi M")
    assert params["status_at"][0] == "TIMESTAMP" and isinstance(params["status_at"][1], datetime)
    assert params["status_at"][1] == datetime.fromisoformat(body["status_at"])
    assert body["matched"] == 1
    assert store_mod.SUPPRESSIONS == []


@pytest.mark.parametrize("where", ["query", "result"])
def test_bigquery_access_refused_gives_503_suppression_unavailable(agent, monkeypatch, where):
    from google.api_core.exceptions import Forbidden

    refusal = Forbidden("Access Denied: Table test-project:intelligence_42_core.suppressions")
    if where == "query":
        def query(sql, job_config=None):
            raise refusal
    else:
        job = mock.MagicMock()
        job.result.side_effect = refusal

        def query(sql, job_config=None):
            return job
    bigquery_agent(monkeypatch, query)
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 503
    assert r.json() == UNAVAILABLE
    assert store_mod.SUPPRESSIONS == []


def failing_at(where, exc):
    """A fake client.query that raises exc when the query is sent, or from the job's result()."""
    if where == "query":
        def query(sql, job_config=None):
            raise exc
        return query
    job = mock.MagicMock()
    job.result.side_effect = exc
    return lambda sql, job_config=None: job


@pytest.mark.parametrize("where", ["query", "result"])
def test_bigquery_access_denied_by_reason_gives_503_suppression_unavailable(agent, monkeypatch, where):
    from google.api_core.exceptions import Forbidden

    refusal = Forbidden("Access Denied: Table test-project:intelligence_42_core.suppressions",
                        errors=[{"reason": "accessDenied", "message": "Access Denied"}])
    bigquery_agent(monkeypatch, failing_at(where, refusal))
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 503
    assert r.json() == UNAVAILABLE


@pytest.mark.parametrize("where", ["query", "result"])
@pytest.mark.parametrize("reason", ["rateLimitExceeded", "quotaExceeded"])
def test_bigquery_quota_403_gives_503_busy_not_switched_off(agent, monkeypatch, where, reason):
    from google.api_core.exceptions import Forbidden

    refusal = Forbidden("Exceeded rate limits: too many table update operations for this table",
                        errors=[{"reason": reason, "message": "Exceeded rate limits"}])
    bigquery_agent(monkeypatch, failing_at(where, refusal))
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 503
    assert r.json() == BUSY
    assert "not switched on" not in r.text and "Exceeded" not in r.text
    assert store_mod.SUPPRESSIONS == []


@pytest.mark.parametrize("where", ["query", "result"])
def test_bigquery_a_403_that_is_not_an_access_refusal_never_reads_not_switched_on(agent, monkeypatch, where):
    from google.api_core.exceptions import Forbidden

    refusal = Forbidden("Billing has not been enabled for this project",
                        errors=[{"reason": "billingNotEnabled", "message": "Billing"}])
    bigquery_agent(monkeypatch, failing_at(where, refusal))
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 500 and r.json()["error"] == "internal"
    assert "not switched on" not in r.text and "Billing" not in r.text


@pytest.mark.parametrize("where", ["query", "result"])
def test_bigquery_a_missing_suppressions_table_gives_503_suppression_unavailable(agent, monkeypatch, where):
    from google.api_core.exceptions import NotFound

    missing = NotFound("Not found: Table test-project:intelligence_42_core.suppressions was not found")
    bigquery_agent(monkeypatch, failing_at(where, missing))
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 503
    assert r.json() == UNAVAILABLE
    assert store_mod.SUPPRESSIONS == []


@pytest.mark.parametrize("where", ["query", "result"])
@pytest.mark.parametrize("fault", ["BadRequest", "InternalServerError", "RuntimeError", "TimeoutError"])
def test_bigquery_a_failure_after_the_query_was_sent_says_it_may_not_have_been_saved(agent, monkeypatch, where,
                                                                                    fault):
    import builtins

    from google.api_core import exceptions

    cls = getattr(exceptions, fault, None) or getattr(builtins, fault)
    bigquery_agent(monkeypatch, failing_at(where, cls("the insert failed")))
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 500
    assert r.json() == {"error": "internal", "message": MAY_NOT_BE_SAVED}
    assert "nothing was saved" not in r.text


def test_bigquery_a_failure_before_the_query_is_sent_says_nothing_was_saved(agent, monkeypatch):
    fake = bigquery_agent(monkeypatch, lambda sql, job_config=None: mock.MagicMock())

    def no_client():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(agent_app, "bigquery_client", no_client)
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 500
    assert r.json() == {"error": "internal", "message": NOTHING_SAVED}
    assert "no credentials" not in r.text
    assert not fake.query.called


class Tripwire(list):
    """A list that fails the test if anything reads or writes it."""

    def _trip(self, *args, **kwargs):
        raise AssertionError("the in-memory suppression list was touched in bigquery mode")

    append = extend = insert = __iter__ = __len__ = __getitem__ = __setitem__ = __iadd__ = copy = _trip


@pytest.mark.parametrize("outcome", ["committed", "refused", "failed"])
def test_bigquery_mode_never_touches_the_in_memory_list(agent, monkeypatch, outcome):
    from google.api_core.exceptions import Forbidden

    monkeypatch.setattr(agent_app, "SUPPRESSIONS", Tripwire())
    if outcome == "committed":
        query = lambda sql, job_config=None: mock.MagicMock()  # noqa: E731
    elif outcome == "refused":
        query = failing_at("result", Forbidden("Access Denied", errors=[{"reason": "accessDenied"}]))
    else:
        query = failing_at("result", RuntimeError("the insert failed"))
    fake = bigquery_agent(monkeypatch, query)
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == {"committed": 201, "refused": 503, "failed": 500}[outcome], r.text
    assert fake.query.call_count == 1
    assert store_mod.SUPPRESSIONS == []


@pytest.mark.parametrize("fault", ["BadRequest", "InternalServerError", "RuntimeError"])
def test_bigquery_any_other_failure_gives_500_and_is_never_reported_done(agent, monkeypatch, fault):
    from google.api_core import exceptions

    cls = getattr(exceptions, fault, None) or RuntimeError

    def query(sql, job_config=None):
        raise cls("the insert failed")

    bigquery_agent(monkeypatch, query)
    r = hide(agent, creator_id="c_ng_macro")
    assert r.status_code == 500
    assert r.json()["error"] == "internal"
    assert "suppression_id" not in r.text and "the insert failed" not in r.text
    assert store_mod.SUPPRESSIONS == []


def _strings(tree):
    """Every string literal in a module, f-strings joined from their literal parts."""
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            yield "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def test_nothing_in_core_api_deletes_updates_merges_or_truncates_suppressions():
    inserts = []
    for path in sorted(API_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for text in _strings(tree):
            if "suppressions" not in text.lower():
                continue
            assert not re.search(r"\b(DELETE|UPDATE|MERGE|TRUNCATE|DROP|REPLACE|ALTER)\b", text, re.IGNORECASE), \
                (path.name, text)
            if re.search(r"\bINSERT\b", text, re.IGNORECASE):
                inserts.append((path.name, text))
        # The streaming append helper writes intelligence_42_agent tables only; it never names suppressions.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) in (
                    "append", "insert_rows_json", "insert_rows", "load_table_from_json"):
                for arg in node.args:
                    assert not (isinstance(arg, ast.Constant) and arg.value == "suppressions"), path.name
        assert "insert_rows_json" not in source or "suppressions" not in _near(source, "insert_rows_json")
    assert [name for name, _ in inserts] == ["agent_app.py"]
    assert inserts[0][1].startswith("INSERT INTO `")


def _near(source, word, span=300):
    return " ".join(source[max(0, m.start() - span):m.end() + span] for m in re.finditer(word, source))


# End to end through f42-api: forwarded to f42-agent, then gone from the next people read.


def community_members(api):
    r = api.get("/api/communities", params={"market": "NG"}, headers=GOOD)
    assert r.status_code == 200, r.text
    return [m["creator_id"] for c in r.json()["communities"] for m in c["members"] or []]


def test_post_needs_the_passcode(api):
    assert api.post("/api/suppressions", json={"creator_id": "c_ng_macro", "reason": REASON, "who": "Thandi"}
                    ).status_code == 401
    assert api.get("/api/suppressions").status_code == 401
    assert store_mod.SUPPRESSIONS == []


def test_after_a_hide_by_id_the_creator_is_gone_from_the_next_read(api):
    assert api.get("/api/creators/c_ng_macro", params={"market": "NG"}, headers=GOOD).status_code == 200
    assert "c_ng_macro" in community_members(api)
    r = api.post("/api/suppressions", json={"creator_id": "c_ng_macro", "reason": REASON, "who": "Thandi"},
                 headers=GOOD)
    assert r.status_code == 201 and r.json()["matched"] == 1
    r = api.get("/api/creators/c_ng_macro", params={"market": "NG"}, headers=GOOD)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert "c_ng_macro" not in community_members(api)
    communities = api.get("/api/communities", params={"market": "NG"}, headers=GOOD).json()["communities"]
    one = api.get(f"/api/communities/{communities[0]['community_id']}", headers=GOOD)
    assert one.status_code == 200 and "fixture_ng_macro" not in one.text


def test_after_a_hide_by_handle_the_creator_is_gone_from_the_next_read(api):
    assert "c_ng_mega" in community_members(api)
    r = api.post("/api/suppressions", json={"platform": "instagram", "handle": "@FIXTURE_NG_MEGA",
                                            "reason": REASON, "who": "Thandi"}, headers=GOOD)
    assert r.status_code == 201 and r.json()["matched"] == 1
    assert api.get("/api/creators/c_ng_mega", params={"market": "NG"}, headers=GOOD).status_code == 404
    assert "c_ng_mega" not in community_members(api)
    assert "c_ng_macro" in community_members(api)


def test_a_refused_hide_through_f42_api_passes_the_words_through(api):
    r = api.post("/api/suppressions", json={"creator_id": "c_ng_macro", "reason": "mail me at a@b.co", "who": "Th"},
                 headers=GOOD)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    r = api.post("/api/suppressions", json={"creator_id": "c_nobody", "reason": REASON, "who": "Thandi"},
                 headers=GOOD)
    assert r.status_code == 404
    assert store_mod.SUPPRESSIONS == []


# The fixture view reads the in-memory rows the way L1's view reads the table.


def test_fixture_view_adds_in_memory_rows_by_id_and_by_handle():
    base = FixtureStore().suppressed_creators()
    assert base == {"c_ng_hidden"}
    store_mod.SUPPRESSIONS.extend([
        {"suppression_id": "sup_app_000000000001", "status_at": "2026-09-29T10:00:00+02:00", "status": "suppressed",
         "creator_id": "c_ng_mid", "platform": None, "handle": None, "reason": REASON, "who": "Thandi"},
        {"suppression_id": "sup_app_000000000002", "status_at": "2026-09-29T10:00:00+02:00", "status": "suppressed",
         "creator_id": None, "platform": "x", "handle": "@FIXTURE_NG_K1", "reason": REASON, "who": "Thandi"},
        {"suppression_id": "sup_app_000000000003", "status_at": "2026-09-29T10:00:00+02:00", "status": "suppressed",
         "creator_id": None, "platform": "tiktok", "handle": "@nobody_42_holds", "reason": REASON, "who": "Thandi"},
    ])
    assert FixtureStore().suppressed_creators() == {"c_ng_hidden", "c_ng_mid", "c_ng_k1"}


def test_fixture_view_takes_the_newest_row_per_id_and_skips_lifted():
    row = {"suppression_id": "sup_x", "creator_id": "c_ng_mid", "platform": None, "handle": None, "reason": REASON,
           "who": "Albert"}
    store_mod.SUPPRESSIONS.extend([
        {**row, "status_at": "2026-09-29T10:00:00+02:00", "status": "suppressed"},
        {**row, "status_at": "2026-09-29T11:00:00+02:00", "status": "lifted"},
    ])
    assert FixtureStore().suppressed_creators() == {"c_ng_hidden"}
    store_mod.SUPPRESSIONS.append({**row, "status_at": "2026-09-29T11:00:00+02:00", "status": "suppressed"})
    # A tie on status_at goes to the row that is not lifted, as in L1's view.
    assert FixtureStore().suppressed_creators() == {"c_ng_hidden", "c_ng_mid"}


def test_fixture_view_still_fails_closed_without_the_view(tmp_path):
    for f in FIXTURES.glob("*.json"):
        if f.stem != "people_suppressed":
            (tmp_path / f.name).write_bytes(f.read_bytes())
    store_mod.SUPPRESSIONS.append(
        {"suppression_id": "sup_app_000000000001", "status_at": "2026-09-29T10:00:00+02:00", "status": "suppressed",
         "creator_id": "c_ng_mid", "platform": None, "handle": None, "reason": REASON, "who": "Thandi"})
    held = FixtureStore(root=tmp_path)
    assert held.suppressed_creators() is None
    assert held.suppressions() is None


# The list on f42-api.


def test_the_list_shows_the_current_row_per_id_newest_first(api):
    first = api.post("/api/suppressions", json={"creator_id": "c_ng_macro", "reason": REASON, "who": "Thandi"},
                     headers=GOOD).json()
    second = api.post("/api/suppressions", json={"platform": "tiktok", "handle": "@someone_new",
                                                 "reason": "Removal request", "who": "Jo"}, headers=GOOD).json()
    r = api.get("/api/suppressions", headers=GOOD)
    assert r.status_code == 200
    listed = r.json()["suppressions"]
    assert [s["suppression_id"] for s in listed] == [second["suppression_id"], first["suppression_id"]]
    assert all(set(s) == ROW_KEYS for s in listed)
    assert listed[0]["handle"] == "@someone_new" and listed[0]["who"] == "Jo" and listed[0]["status"] == "suppressed"
    assert listed[1]["creator_id"] == "c_ng_macro" and listed[1]["reason"] == REASON


def test_fixture_list_order_matches_the_view():
    row = {"creator_id": "c_ng_mid", "platform": None, "handle": None, "reason": REASON, "who": "Albert"}
    store_mod.SUPPRESSIONS.extend([
        {**row, "suppression_id": "sup_a", "status_at": "2026-09-29T10:00:00+02:00", "status": "suppressed"},
        {**row, "suppression_id": "sup_b", "status_at": "2026-09-29T09:00:00+02:00", "status": "suppressed"},
        {**row, "suppression_id": "sup_b", "status_at": "2026-09-29T12:00:00+02:00", "status": "lifted"},
        {**row, "suppression_id": "sup_c", "status_at": "2026-09-29T11:00:00+02:00", "status": "lifted"},
        {**row, "suppression_id": "sup_c", "status_at": "2026-09-29T11:00:00+02:00", "status": "suppressed"},
    ])
    listed = FixtureStore().suppressions()
    assert [(s["suppression_id"], s["status"]) for s in listed] == [
        ("sup_b", "lifted"), ("sup_c", "suppressed"), ("sup_a", "suppressed")]


@pytest.mark.parametrize("how", ["missing", "unreadable"])
def test_the_list_fails_closed_with_people_unavailable(api, monkeypatch, how):
    class Store(FixtureStore):
        def suppressions(self):
            if how == "missing":
                return None
            raise RuntimeError("Access Denied: suppressions")

    monkeypatch.setattr(store_mod, "get_store", lambda: Store())
    r = api.get("/api/suppressions", headers=GOOD)
    assert r.status_code == 503
    assert r.json() == {"error": "people_unavailable", "message": "People view unavailable."}
    assert "Access Denied" not in r.text


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    def __init__(self, objects, rows=None):
        self.objects, self.rows, self.calls = objects, rows or [], []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        if "INFORMATION_SCHEMA" in sql:
            return FakeJob([{"ds": "intelligence_42_core", "n": n, "what": "table"} for n in self.objects])
        return FakeJob(self.rows)


def test_bigquery_list_reads_the_table_in_the_views_order():
    stamp = datetime.fromisoformat("2026-09-29T08:00:00+00:00")
    client = FakeClient(("suppressions", "v_suppressed_creators", "creators"), rows=[
        {"suppression_id": "sup_app_0123456789ab", "status_at": stamp, "status": "suppressed",
         "creator_id": "c_ng_macro", "platform": None, "handle": None, "reason": REASON, "who": "Thandi"}])
    listed = BigQueryStore(client=client).suppressions()
    assert listed == [{"suppression_id": "sup_app_0123456789ab", "status_at": "2026-09-29T10:00:00+02:00",
                       "status": "suppressed", "creator_id": "c_ng_macro", "platform": None, "handle": None,
                       "reason": REASON, "who": "Thandi"}]
    sql, config = client.calls[-1]
    assert "`intelligence_42_core.suppressions`" not in sql and ".intelligence_42_core.suppressions` s" in sql
    assert re.search(r"QUALIFY ROW_NUMBER\(\) OVER \(PARTITION BY s\.suppression_id\s+ORDER BY s\.status_at DESC, "
                     r"s\.status = 'lifted', TO_JSON_STRING\(s\)\) = 1", sql)
    assert re.search(r"ORDER BY s\.status_at DESC", sql.split("= 1", 1)[1])
    assert not re.search(r"\b(INSERT|UPDATE|MERGE|CREATE|DELETE|DROP|TRUNCATE)\b", sql)
    assert config.maximum_bytes_billed == 2_000_000_000


def test_bigquery_list_is_none_while_the_table_does_not_exist():
    client = FakeClient(("creators",))
    assert BigQueryStore(client=client).suppressions() is None
    assert all("INFORMATION_SCHEMA" in sql for sql, _ in client.calls)
