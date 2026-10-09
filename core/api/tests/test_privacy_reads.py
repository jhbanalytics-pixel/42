"""What the suppression readers cost (C5 v2 section 19, row A44): the hidden-people set in one job, one posts lookup, a
list shared across the passes of one request, and a running record's polls on a 30 second list."""
import copy

import pytest

from core.api import privacy
from core.api import store as store_mod
from core.api.store import BigQueryStore
from core.api.tests.test_privacy_ask_routes import ASK, RouteStore, api, hold, remote_agent, world  # noqa: F401
from core.api.tests.test_privacy_projection import (CREATORS, P_HID1, P_REN1, P_VIS1, POSTS, PrivStore, ask_record,
                                                    body_of, ev, handle_row, project)

GOOD = {"X-Passcode": "s3cret-passcode"}


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class CountingClient:
    """A BigQuery client that answers by what the SQL reads and counts the jobs it is given."""

    CATALOG = [{"ds": "intelligence_42_core", "n": n, "what": "table"}
               for n in ("creators", "v_suppressed_creators", "posts")] + [
        {"ds": "intelligence_42_agent", "n": "suppressions", "what": "table"}]

    def __init__(self, hidden_rows, post_rows):
        self.hidden_rows, self.post_rows, self.sql = hidden_rows, post_rows, []

    def query(self, sql, job_config=None):
        if "INFORMATION_SCHEMA" in sql:
            return FakeJob(self.CATALOG)
        self.sql.append(sql)
        return FakeJob(self.hidden_rows if "v_suppressed_creators" in sql else self.post_rows)


@pytest.fixture(autouse=True)
def fresh_catalog(monkeypatch):
    monkeypatch.setattr(store_mod, "_CATALOG", {})


def bigquery_store(hidden_rows=None, post_rows=None):
    hidden = [{"creator_id": "c_hid", "platform": "tiktok", "handle": "hid_handle"}] if hidden_rows is None else hidden_rows
    posts = [{"post_id": p, "creator_id": c} for p, (c, _) in POSTS.items()] if post_rows is None else post_rows
    client = CountingClient(hidden, posts)
    return BigQueryStore(client=client), client


def test_a_finished_record_read_costs_two_jobs_the_hidden_set_and_one_posts_lookup_a44():
    store, client = bigquery_store()
    out = project(ask_record(), store)
    assert len(client.sql) == 2
    assert "v_suppressed_creators" in client.sql[0] and "post_id IN UNNEST" in client.sql[1]
    assert [e["id"] for e in out["answer"]["evidence"]] == [P_VIS1, P_REN1]


def test_the_hidden_set_is_one_read_only_statement_over_the_view_the_table_and_the_creators_a44():
    store, client = bigquery_store()
    project(ask_record(), store)
    sql = client.sql[0].upper()
    for word in ("SUPPRESSIONS", "CREATORS", "LIFTED", "QUALIFY"):
        assert word in sql
    assert not any(w in sql for w in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE ", "DROP "))


def test_a_handle_only_row_and_an_id_with_no_creators_row_both_come_back_from_the_one_job_a44():
    rows = [{"creator_id": "c_gone", "platform": None, "handle": None},
            {"creator_id": None, "platform": "tiktok", "handle": "hid_handle"}]
    store, client = bigquery_store(hidden_rows=rows, post_rows=[])
    keys, ids, _ = privacy.read_hidden(store)
    assert ids == {"c_gone"} and keys == {"tiktok:hid_handle"} and len(client.sql) == 1


def test_a_missing_suppression_view_is_an_unavailable_list():
    store, client = bigquery_store()
    client.CATALOG = [r for r in client.CATALOG if r["n"] != "v_suppressed_creators"]
    assert privacy.read_hidden(store) is None


def test_the_posts_lookup_is_skipped_when_no_creator_id_is_hidden():
    store, client = bigquery_store(hidden_rows=[{"creator_id": None, "platform": "tiktok", "handle": "hid_handle"}])
    out = project(ask_record(), store)
    assert len(client.sql) == 1  # only the hidden set; a handle can be matched without asking who wrote each post
    assert [c["id"] for c in out["answer"]["claims"]] == ["c1", "c4"]


# One list for the passes of a request.
def test_two_passes_inside_one_request_share_one_list_read():
    store = PrivStore(hide={"c_hid"})
    with privacy.one_read():
        first, second = privacy.read_hidden(store), privacy.read_hidden(store)
    assert first == second and store.calls.count("suppressed_creators") == 1
    privacy.read_hidden(store)
    assert store.calls.count("suppressed_creators") == 2  # outside the request nothing is kept


def test_a_dossier_made_from_an_answer_reads_the_list_once(world):  # noqa: F811
    world.store.hide = {"c_hid"}
    hold(ask_record())
    r = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert r.status_code == 201 and world.store.calls.count("suppressed_creators") == 1


def test_an_investigation_page_reads_the_list_once(world, monkeypatch):  # noqa: F811
    from core.api.tests.test_privacy_ask_routes import investigation
    inv = investigation(world, monkeypatch, ask_record())
    assert world.client.get(f"/api/investigations/{inv}").status_code == 200
    assert world.store.calls.count("suppressed_creators") == 1


# Polls of a running record.
def running(record):
    record = copy.deepcopy(record)
    record["status"] = "running"
    return record


def test_polls_of_a_running_record_read_the_list_once_in_thirty_seconds_the_agent_side(world, monkeypatch):  # noqa: F811
    now = [100.0]
    monkeypatch.setattr(privacy, "_clock", lambda: now[0])
    ask = hold(running(ask_record()))
    ask.ended, ask.done = None, None
    for _ in range(3):
        assert world.client.get(f"/api/ask/{ASK}").status_code == 200
    assert world.store.calls.count("suppressed_creators") == 1
    assert world.store.calls.count("post_creators") == 1  # the same posts are not looked up again
    world.store.hide.add("c_hid2")
    now[0] += privacy.SUPPRESSION_MAX_AGE_S + 1
    world.client.get(f"/api/ask/{ASK}")
    assert world.store.calls.count("suppressed_creators") == 2
    ask.stop = True
    ask.finished.set()


def test_a_finished_record_is_read_fresh_every_time_the_agent_side(world):  # noqa: F811
    hold(ask_record())
    world.client.get(f"/api/ask/{ASK}")
    world.client.get(f"/api/ask/{ASK}")
    assert world.store.calls.count("suppressed_creators") == 2


def test_polls_of_a_running_record_read_the_list_once_in_thirty_seconds_the_api_side(api, monkeypatch):  # noqa: F811
    now = [100.0]
    monkeypatch.setattr(privacy, "_clock", lambda: now[0])
    held = running(ask_record())
    client = api.serve(remote_agent({}, held=held))
    for _ in range(3):
        assert client.get(f"/api/ask/{ASK}", headers=GOOD).status_code == 200
    assert api.store.calls.count("suppressed_creators") == 1
    now[0] += privacy.SUPPRESSION_MAX_AGE_S + 1
    client.get(f"/api/ask/{ASK}", headers=GOOD)
    assert api.store.calls.count("suppressed_creators") == 2


# The live stream.
def test_the_stream_looks_up_no_post_while_nobody_is_hidden():
    store = PrivStore(hide=set())
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    for n in range(7):
        event = {"seq": n, "evidence": ev(f"obs1_{n:032x}", "vis_handle", "words")}
        assert privacy.project_event("evidence", event, hidden, store, gone, seen) == event
    assert store.calls.count("post_creators") == 0 and gone == set()


def test_the_stream_looks_each_stored_post_up_once_when_someone_is_hidden():
    store = PrivStore(hide={"c_hid"})
    hidden = privacy.read_hidden(store)
    gone, seen = set(), {}
    event = {"seq": 1, "evidence": ev(P_VIS1, "vis_handle", "words")}
    for _ in range(3):
        assert privacy.project_event("evidence", event, hidden, store, gone, seen) == event
    assert store.calls.count("post_creators") == 1
