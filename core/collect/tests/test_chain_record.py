"""What the chain records about the run itself (W8-REL-B v2.1, JB-07 and JB-08): the Cloud Run job name and task attempt in the
running row's counts, and the run stamp on every row that passes through chain._row. No cloud access: a memory runs store."""
import json
from datetime import date

import pytest

from core.collect import chain
from core.setup import stamp

DAY = date(2026, 10, 12)
SHA = "5e1c0de9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3"
DIGEST = "sha256:" + "ab" * 32


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("RUN_DATE", "FORCE_RERUN", "CLOUD_RUN_EXECUTION", "CLOUD_RUN_TASK_ATTEMPT", "CLOUD_RUN_JOB", "CHAIN_UNDERSTAND",
                 "F42_GIT_SHA", "F42_VERSION", "F42_IMAGE_DIGEST"):
        monkeypatch.delenv(name, raising=False)


def running_row(runs, stage="collect"):
    rows = [r for r in runs.rows if r["stage"] == stage and r["status"] == "running"]
    assert len(rows) == 1, rows
    return rows[0]


# JB-07

def test_jb07_the_running_row_records_the_execution_the_job_and_the_task_attempt_the_platform_injects(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-x7k2p")
    monkeypatch.setenv("CLOUD_RUN_JOB", "f42-collect")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "0")
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs)
    assert running_row(runs)["counts"] == {"execution": "f42-collect-x7k2p", "job": "f42-collect", "task_attempt": 0}


def test_jb07_a_retried_task_records_its_attempt_number(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-x7k2p")
    monkeypatch.setenv("CLOUD_RUN_JOB", "f42-collect")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "2")
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs)
    assert running_row(runs)["counts"]["task_attempt"] == 2


def test_jb07_an_env_without_the_names_records_nothing_and_the_row_is_as_it_was():
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs)
    assert running_row(runs)["counts"] is None


def test_jb07_each_name_is_recorded_only_when_the_env_carries_it(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-x7k2p")
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs)
    assert running_row(runs)["counts"] == {"execution": "f42-collect-x7k2p"}
    monkeypatch.delenv("CLOUD_RUN_EXECUTION")
    monkeypatch.setenv("CLOUD_RUN_JOB", "f42-detect")
    other = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=other)
    assert running_row(other)["counts"] == {"job": "f42-detect"}


@pytest.mark.parametrize("attempt", ["", "x", "-1", "1.5", " 1", "٣", "9" * 12])
def test_jb07_a_task_attempt_that_is_not_a_small_non_negative_integer_is_not_recorded(monkeypatch, attempt):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-x7k2p")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", attempt)
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs)
    assert running_row(runs)["counts"] == {"execution": "f42-collect-x7k2p"}


@pytest.mark.parametrize("job", ["", "F42 COLLECT", "f42-collect\nx", "a" * 70, "../etc"])
def test_jb07_a_job_name_that_is_not_a_cloud_run_resource_name_is_not_recorded(monkeypatch, job):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-x7k2p")
    monkeypatch.setenv("CLOUD_RUN_JOB", job)
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs)
    assert running_row(runs)["counts"] == {"execution": "f42-collect-x7k2p"}


def test_jb07_the_terminal_row_still_carries_the_stage_counts_alone(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-x7k2p")
    monkeypatch.setenv("CLOUD_RUN_JOB", "f42-collect")
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "0")
    runs = chain.MemoryRunsStore()
    run = chain.begin("collect", DAY, runs=runs)
    chain.finish(run, "ok", {"posts": 12}, runs=runs)
    assert runs.rows[-1]["counts"] == {"posts": 12}


def test_jb07_a_cloud_run_retry_of_the_same_execution_is_still_let_through_with_the_new_keys_in_the_running_row(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-x7k2p")
    monkeypatch.setenv("CLOUD_RUN_JOB", "f42-collect")
    runs = chain.MemoryRunsStore()
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "0")
    chain.begin("collect", DAY, runs=runs)  # the first attempt dies without a terminal row
    monkeypatch.setenv("CLOUD_RUN_TASK_ATTEMPT", "1")
    chain.begin("collect", DAY, runs=runs)  # the platform retries the task inside the same execution
    attempts = [r["counts"]["task_attempt"] for r in runs.rows if r["status"] == "running"]
    assert attempts == [0, 1]
    assert not [r for r in runs.rows if r["status"] == chain.SKIPPED]


def test_jb07_a_skipped_duplicate_row_still_carries_no_counts(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "f42-collect-x7k2p")
    monkeypatch.setenv("CLOUD_RUN_JOB", "f42-collect")
    runs = chain.MemoryRunsStore()
    first = chain.begin("collect", DAY, runs=runs)
    chain.finish(first, "ok", {"posts": 1}, runs=runs)
    with pytest.raises(chain.AlreadyDone):
        chain.begin("collect", DAY, runs=runs)
    assert runs.rows[-1]["status"] == chain.SKIPPED and runs.rows[-1]["counts"] is None


# JB-08

def stamped(row):
    assert isinstance(row.get("stamp"), str), row
    return json.loads(row["stamp"])


def test_jb08_every_row_chain_writes_carries_the_stamp_of_the_running_process(monkeypatch):
    monkeypatch.setenv("F42_GIT_SHA", SHA)
    monkeypatch.setenv("F42_IMAGE_DIGEST", DIGEST)
    runs = chain.MemoryRunsStore()
    run = chain.begin("collect", DAY, runs=runs)
    chain.finish(run, "ok", {"posts": 1}, runs=runs)
    expected = stamp.build()
    assert expected["git_sha"] == SHA and expected["image_digest"] == DIGEST
    assert [r["status"] for r in runs.rows] == ["running", "ok"]
    for row in runs.rows:
        assert stamped(row) == expected


def test_jb08_a_blocked_row_and_a_skipped_duplicate_row_are_stamped_too(monkeypatch):
    monkeypatch.setenv("F42_GIT_SHA", SHA)
    runs = chain.MemoryRunsStore()
    with pytest.raises(chain.UpstreamNotReady):
        chain.begin("detect", DAY, runs=runs)  # understand is out of the chain, so collect is the upstream, and it has no row
    done = chain.begin("collect", DAY, runs=runs)
    chain.finish(done, "ok", None, runs=runs)
    with pytest.raises(chain.AlreadyDone):
        chain.begin("collect", DAY, runs=runs)
    assert {r["status"] for r in runs.rows} == {"running", "blocked", "ok", chain.SKIPPED}
    for row in runs.rows:
        assert stamped(row)["git_sha"] == SHA, row["status"]


def test_jb08_reconcile_rows_are_stamped_because_they_pass_through_the_same_row_builder(monkeypatch):
    monkeypatch.setenv("F42_GIT_SHA", SHA)
    runs = chain.MemoryRunsStore()
    run = chain.begin("reconcile", DAY, runs=runs)
    chain.finish(run, "ok", {"days": 1}, runs=runs)
    assert len(runs.rows) == 2
    for row in runs.rows:
        assert stamped(row)["git_sha"] == SHA


def test_jb08_a_row_that_already_carries_a_stamp_raises_when_it_goes_through_the_stamper_again(monkeypatch):
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs)
    with pytest.raises(ValueError, match="already carries a stamp"):
        stamp.stamp_row(runs.rows[0])


def test_jb08_the_stamp_is_built_by_the_code_that_ran_not_taken_from_anything_the_caller_passes(monkeypatch):
    # chain.begin and finish take no stamp argument; the only inputs are the process env and tree, so the recorded git_sha
    # is the env's and a counts dict that tries to carry a stamp does not become one.
    monkeypatch.setenv("F42_GIT_SHA", SHA)
    runs = chain.MemoryRunsStore()
    run = chain.begin("collect", DAY, runs=runs)
    forged = {"stamp": {"git_sha": "0" * 40}}
    chain.finish(run, "ok", forged, runs=runs)
    row = runs.rows[-1]
    assert stamped(row)["git_sha"] == SHA and row["counts"] == forged


def test_jb08_with_no_env_the_stamp_is_still_written_with_its_declared_fields_null():
    runs = chain.MemoryRunsStore()
    chain.begin("collect", DAY, runs=runs)
    got = stamped(runs.rows[0])
    assert got["git_sha"] is None and got["image_digest"] is None and got["v"] == stamp.STAMP_VERSION


def test_jb08_the_bigquery_store_sends_the_stamp_as_a_text_column_beside_the_json_counts(monkeypatch):
    monkeypatch.setenv("F42_GIT_SHA", SHA)
    sent = []

    class Client:
        def insert_rows_json(self, table, rows):
            sent.append((table, rows))
            return []

    store = chain.BigQueryRunsStore(Client())
    row = chain._row(chain.Run("collect-20261012-aaaaaaaaaaaa", "collect", DAY, "2026-10-12T00:00:00+00:00"), "running", None, None, None)
    store.append(row)
    table, rows = sent[0]
    assert table == "ogilvy-trends-v2.intelligence_42_agent.runs"
    assert json.loads(rows[0]["stamp"])["git_sha"] == SHA
