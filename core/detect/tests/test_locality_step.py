"""The detect locality step (C4 v3 section 8): one transaction of two inserts, then write-time verification in Python
(tests L-09, L-15, L-16 and the ruling's finding F5). The transaction is run statement by statement on DuckDB by a
fake client; the fake can also alter a member row after the insert, refuse streaming inserts, or raise like a bytes
refusal."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.detect import job, locality, sqlrun
from core.detect.tests import duck
from core.detect.tests.test_locality_v2 import CUTOFF, D, DETECT, add, con, key, known  # noqa: F401
from core.trust.locality import METRIC_VERSION

DETECT_RUN = SimpleNamespace(run_id=DETECT, started_at=CUTOFF.isoformat())


class Fake(duck.Client):
    def __init__(self, con, *, tamper=None, refuse_inserts=False, raise_on_script=None):
        super().__init__(con)
        self.configs, self.tamper, self.refuse_inserts, self.raise_on_script = [], tamper, refuse_inserts, raise_on_script
        self.inserted = []

    def query(self, sql, job_config=None):
        self.configs.append(job_config)
        if "BEGIN TRANSACTION" in sql:
            if self.raise_on_script:
                raise self.raise_on_script
            params = {p.name: duck._value(p) for p in job_config.query_parameters}
            for stmt in sqlrun.split(sql):
                if stmt.strip().upper().startswith(("BEGIN", "COMMIT")):
                    continue
                duck.run_duck(self.con, sqlrun._strip_leading_comments(stmt), params)
            if self.tamper:
                self.tamper(self.con)
            return duck._Job([])
        return super().query(sql, job_config)

    def insert_rows_json(self, table, rows):
        if self.refuse_inserts:
            raise RuntimeError("streaming insert refused")
        self.inserted.append((table, rows))
        for r in rows:
            duck.load(self.con, table, [r])
        return []


def world(con):  # noqa: F811
    key(con, "s1")
    known(con, "s1", 5, 3, 2)                       # 10 posts, 8 known, 5 local: local
    key(con, "s2")                                  # a series and no post: a valid zero
    key(con, "s3")
    known(con, "s3", 1, 0, 3)                       # 4 posts, 1 known
    return con


def rows(con, table="item_locality"):  # noqa: F811
    return duck.query(con, f"SELECT * FROM {{core}}.{table}", {})


def run(client, **kw):
    return locality.run_locality_step(client, D, DETECT_RUN, core="core", agent="agent", **kw)


def test_the_step_writes_a_row_and_a_verification_row_for_every_key(con):  # noqa: F811
    world(con)
    counts = run(Fake(con))
    assert (counts["keys"], counts["keys_verified"], counts["keys_refused"], counts["series_keys_without_row"]) == (3, 3, 0, 0)
    assert counts["member_rows"] == 14
    assert counts["status_counts"] == {"local": 1, "market_unconfirmed": 2}
    assert {r["item_id"] for r in rows(con)} == {"s1", "s2", "s3"}
    assert {r["item_id"] for r in rows(con, "item_locality_verified")} == {"s1", "s2", "s3"}
    visible = duck.query(con, "SELECT item_id, checked_status FROM {core}.v_item_locality_checked WHERE detect_run_id = @r", {"r": DETECT})
    assert {r["item_id"]: r["checked_status"] for r in visible} == {"s1": "local", "s2": "market_unconfirmed", "s3": "market_unconfirmed"}


def test_the_cutoff_is_the_detect_runs_recorded_start(con):  # noqa: F811
    key(con, "c1")
    add(con, "c1", geo="NG", conf=0.9, src="ext_region")
    add(con, "c1", geo="NG", conf=0.9, src="ext_region", observed_at=CUTOFF + timedelta(minutes=5), days_ago=0)
    run(Fake(con))
    got = rows(con)[0]
    assert got["population_posts"] == 1 and got["population_cutoff"] == CUTOFF


def test_a_second_call_for_the_same_detect_run_writes_nothing_more(con):  # noqa: F811
    world(con)
    client = Fake(con)
    run(client)
    before = (len(rows(con)), len(rows(con, "item_locality_post")), len(rows(con, "item_locality_verified")))
    second = run(client)
    assert (len(rows(con)), len(rows(con, "item_locality_post")), len(rows(con, "item_locality_verified"))) == before
    assert second["rows_written"] == 0 and second["keys_verified"] == 3


def test_a_member_row_altered_after_the_insert_makes_the_step_raise_and_the_key_stays_invisible(con):  # noqa: F811
    world(con)

    def tamper(c):
        c.execute("UPDATE core.item_locality_post SET locality_class = 'unknown' WHERE item_id = 's1' AND locality_class = 'local'")

    with pytest.raises(locality.LocalityFailed) as caught:
        run(Fake(con, tamper=tamper))
    assert caught.value.counts["keys_refused"] == 1 and caught.value.counts["refused"][0]["item_id"] == "s1"
    assert {r["item_id"] for r in rows(con, "item_locality_verified")} == {"s2", "s3"}
    visible = {r["item_id"] for r in duck.query(con, "SELECT item_id FROM {core}.v_item_locality_checked", {})}
    assert visible == {"s2", "s3"}                    # to every consumer s1 is a missing row


def test_a_crash_after_the_commit_leaves_the_keys_unverified_and_a_second_call_verifies_them(con):  # noqa: F811
    """Ruling F5: the guard must not strand the rows of a run whose verification writes never happened."""
    world(con)
    with pytest.raises(RuntimeError, match="streaming insert refused"):
        run(Fake(con, refuse_inserts=True))
    assert len(rows(con)) == 3 and rows(con, "item_locality_verified") == []
    again = run(Fake(con))
    assert again["rows_written"] == 0 and again["keys_verified"] == 3
    assert len(rows(con, "item_locality")) == 3       # nothing was written twice


def test_the_step_refuses_to_run_without_a_byte_cap(con):  # noqa: F811
    world(con)
    client = Fake(con)
    with pytest.raises(ValueError, match="maximum_bytes_billed"):
        run(client, max_bytes=None)
    assert client.configs == [] and rows(con) == []


def test_the_transaction_carries_the_byte_cap_and_the_timeout(con):  # noqa: F811
    world(con)
    client = Fake(con)
    run(client)
    config = next(c for c in client.configs if c is not None and getattr(c, "maximum_bytes_billed", None))
    assert config.maximum_bytes_billed == locality.MAX_BYTES_BILLED and int(config.job_timeout_ms) == 600_000
    assert {p.name for p in config.query_parameters} >= {"d", "cutoff", "detect_run_id", "metric_version"}


def test_the_metric_version_written_is_the_modules_not_a_parameter_of_the_caller(con):  # noqa: F811
    world(con)
    run(Fake(con))
    assert {r["metric_version"] for r in rows(con)} == {METRIC_VERSION}


# The job wrapper: its own runs row, never an exception.

def runs_rows(con):  # noqa: F811
    return duck.query(con, "SELECT * FROM {agent}.runs r WHERE r.stage = 'locality'", {})


def test_the_wrapper_appends_an_ok_runs_row_with_the_counts_duration_and_bytes(con):  # noqa: F811
    world(con)
    out = job.run_locality_step(Fake(con), D, DETECT_RUN, core="core", agent="agent")
    assert out["keys_verified"] == 3 and "duration_s" in out and "bytes_billed" in out
    (row,) = runs_rows(con)
    assert row["status"] == "ok" and row["error"] is None
    counts = json.loads(row["counts"]) if isinstance(row["counts"], str) else row["counts"]
    assert counts["keys_verified"] == 3 and "duration_s" in counts and "bytes_billed" in counts


def test_the_wrapper_turns_a_verification_failure_into_a_failed_runs_row_and_does_not_raise(con):  # noqa: F811
    world(con)

    def tamper(c):
        c.execute("UPDATE core.item_locality_post SET locality_class = 'unknown' WHERE item_id = 's1' AND locality_class = 'local'")

    out = job.run_locality_step(Fake(con, tamper=tamper), D, DETECT_RUN, core="core", agent="agent")
    assert out["status"] == "failed" and "s1" in out["error"]
    (row,) = runs_rows(con)
    assert row["status"] == "failed" and "s1" in row["error"]


def test_a_bytes_refusal_is_a_failure_of_the_step_and_not_of_detect(con):  # noqa: F811
    world(con)
    out = job.run_locality_step(Fake(con, raise_on_script=RuntimeError("Query exceeded limit for bytes billed")), D,
                                DETECT_RUN, core="core", agent="agent")
    assert out["status"] == "failed" and "bytes billed" in out["error"]
    (row,) = runs_rows(con)
    assert row["status"] == "failed"
