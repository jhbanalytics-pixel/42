"""The capped read of the card outcome inputs: dataset names come from the caller, and a deadline bounds every call."""

import re
import time
from datetime import date
from pathlib import Path

import pytest

from core.eval import card_outcome_read as rd

START, END = date(2026, 9, 24), date(2026, 10, 1)
SQL_FILE = Path(rd.__file__).parent / "sql" / "card_outcome.sql"


RESULT_WAITS = []


class Job:
    total_bytes_processed, total_bytes_billed, job_id, cache_hit = 1000, 1000, "j", False

    def result(self, timeout=None):
        RESULT_WAITS.append(timeout)
        return iter([])


class Client:
    def __init__(self):
        self.sql, self.timeouts = [], []

    def query(self, sql, job_config=None, timeout=None):
        self.sql.append(sql)
        self.timeouts.append(timeout)
        return Job()


def test_the_sql_file_names_no_production_dataset_or_project():
    text = SQL_FILE.read_text(encoding="utf-8")
    assert "intelligence_42" not in text and "ogilvy-trends-v2" not in text
    assert set(re.findall(r"`\{(\w+)\}\.", text)) == {"core", "agent"}


def test_every_query_reads_the_datasets_the_caller_names():
    client = Client()
    rd.read_inputs(client, rd.QUERIES, START, END, core="scratch_core", agent="scratch_agent")
    assert len(client.sql) == 6                                   # a dry run and a real run of each of three queries
    for sql in client.sql:
        assert "intelligence_42" not in sql and "ogilvy-trends-v2" not in sql and "{core}" not in sql, sql
        assert "{agent}" not in sql
    blob = "\n".join(client.sql)
    assert "`scratch_agent.v_briefs_current`" in blob and "`scratch_core.v_item_state_current`" in blob
    assert "`scratch_core.v_series_test_current`" in blob and "`scratch_core.v_good_runs`" in blob


def test_with_no_names_given_the_production_datasets_are_read():
    client = Client()
    rd.read_inputs(client, rd.QUERIES, START, END)
    blob = "\n".join(client.sql)
    assert "`intelligence_42_agent.v_briefs_current`" in blob and "`intelligence_42_core.v_good_runs`" in blob


def test_the_queries_stay_unrendered_so_two_callers_cannot_share_a_name():
    client = Client()
    rd.read_inputs(client, rd.QUERIES, START, END, core="a", agent="b")
    assert "{core}" in rd.QUERIES["detect_days"] and "{agent}" in rd.QUERIES["briefs"]


def test_a_deadline_already_passed_stops_before_any_call():
    client = Client()
    with pytest.raises(rd.DeadlineExceeded):
        rd.read_inputs(client, rd.QUERIES, START, END, deadline=time.monotonic() - 1)
    assert client.sql == []


def test_no_call_waits_longer_than_the_time_left_to_the_deadline():
    client = Client()
    rd.read_inputs(client, rd.QUERIES, START, END, deadline=time.monotonic() + 5)
    assert client.timeouts and all(t is not None and 0 < t <= 5 for t in client.timeouts)


def test_without_a_deadline_the_old_call_timeouts_stand():
    client = Client()
    rd.read_inputs(client, rd.QUERIES, START, END)
    assert set(client.timeouts) == {60}


def test_a_deadline_that_runs_out_between_queries_stops_the_next_one():
    class Slow(Client):
        def query(self, sql, job_config=None, timeout=None):
            time.sleep(0.15)
            return super().query(sql, job_config, timeout)

    client = Slow()
    with pytest.raises(rd.DeadlineExceeded):
        rd.read_inputs(client, rd.QUERIES, START, END, deadline=time.monotonic() + 0.2)
    assert 0 < len(client.sql) < 6


def test_no_wait_for_a_result_is_longer_than_the_time_left_to_the_deadline():
    RESULT_WAITS.clear()
    rd.read_inputs(Client(), rd.QUERIES, START, END, deadline=time.monotonic() + 5)
    assert len(RESULT_WAITS) == 3 and all(0 < w <= 5 for w in RESULT_WAITS)
    RESULT_WAITS.clear()
    rd.read_inputs(Client(), rd.QUERIES, START, END)
    assert set(RESULT_WAITS) == {300}
