import json

import pytest

from core.eval.ask_timing import main, timing


def step(at, kind, text):
    return {"at": f"2026-10-03T08:{at}+02:00", "kind": kind, "text": text}


RECORD = {
    "created_at": "2026-10-03T08:00:00+02:00",
    "finished_at": "2026-10-03T08:04:00+02:00",
    "steps": [step("00:01", "plan", "Reading the question"),
              step("00:05", "search", "Searching stored posts"),
              step("01:05", "found", "12 posts found"),
              step("02:00", "write", "Writing the answer from 14 posts and 6 counts"),
              step("03:00", "check", "Checking 4 claims against the posts and the numbers")],
}


def test_phases_split_at_the_first_write_and_check_steps():
    out = timing(RECORD)
    assert out["total_s"] == 240 and out["steps"] == 5
    assert out["phases"] == {"research_s": 120, "writing_s": 60, "checks_s": 60}


def test_each_step_owns_the_gap_to_the_next_and_the_last_runs_to_the_finish():
    out = timing(RECORD)
    assert out["by_kind"] == {"plan": 4, "search": 60, "found": 55, "write": 60, "check": 60}
    assert out["slowest"][0] == {"seconds": 60, "kind": "search", "text": "Searching stored posts"}


def test_an_ask_that_never_wrote_is_all_research():
    record = dict(RECORD, steps=RECORD["steps"][:3])
    assert timing(record)["phases"] == {"research_s": 240}


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, sql, job_config=None):
        self.calls.append((sql, {p.name: p.value for p in job_config.query_parameters}))
        rows = self.rows
        return type("Job", (), {"result": lambda self: rows})()


# runs.record is a JSON column: the BigQuery client hands it back already decoded, as a dict.
@pytest.mark.parametrize("record", [RECORD, json.dumps(RECORD)], ids=["json column", "string"])
def test_the_command_reads_the_stored_record_and_prints_its_timing(record, capsys):
    client = FakeClient([{"record": record}])
    assert main(["a_20261003_91b2509f"], client=client) == 0
    assert client.calls[0][1] == {"ask_id": "a_20261003_91b2509f"}
    out = json.loads(capsys.readouterr().out)
    assert out["total_s"] == 240 and out["phases"] == {"research_s": 120, "writing_s": 60, "checks_s": 60}


def test_the_command_says_when_there_is_no_run_row_and_checks_its_arguments(capsys):
    assert main(["a_missing"], client=FakeClient([])) == 1
    assert "no ask run row for a_missing" in capsys.readouterr().out
    assert main([], client=FakeClient([])) == 2
