"""One child execution consumes at most one derivation, in the consume routine itself.

The write-once observation object already refuses a second observation for an execution,
but that is storage, not the protected record (review of dd9fc49, should fix). The consume
routine refuses a new consumption for an execution name any consumption already carries;
the replay of the same derivation by the same execution keeps its own earlier branch.
"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSUME = ROOT / "infra/bigquery_routines/sp_consume_open_intelligence_daily_derivation_v1.sql"
MARK = "AS 'daily_execution_already_consumed';"
EXECUTION = "projects/p/locations/l/jobs/intelligence-42-daily-staging/executions/child1"


def predicate():
    text = CONSUME.read_text(encoding="utf-8")
    end = text.index(MARK)
    start = text.rindex("  ASSERT ", 0, end)
    body = text[start + len("  ASSERT ") : end]
    return re.sub(r"`\{project\}\.\{dataset\}\.([a-z0-9_]+)`", r"\1", body)


def evaluate(consumed):
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(
            """
            CREATE TABLE variables(v_execution_name, v_derivation_id);
            CREATE TABLE open_intelligence_execution_consumptions_v2(approval_id, execution_name);
            """
        )
        connection.execute("INSERT INTO variables VALUES (?, ?)", (EXECUTION, "exd_new"))
        connection.executemany(
            "INSERT INTO open_intelligence_execution_consumptions_v2 VALUES (?, ?)", consumed
        )
        return connection.execute(f"SELECT ({predicate()}) FROM variables").fetchone()[0]


def test_an_execution_that_consumed_nothing_may_consume():
    assert evaluate([("exd_other", EXECUTION + "x")]) == 1


def test_an_execution_that_consumed_another_derivation_refuses():
    assert evaluate([("exd_other", EXECUTION)]) == 0


def test_the_check_is_inside_the_new_consumption_path_before_its_insert():
    text = CONSUME.read_text(encoding="utf-8")
    replay_end = text.index("    RETURN;\n  END IF;")
    lock = text.index("ASSERT @@row_count=1 AS 'daily_consumption_concurrent_conflict';")
    check = text.index(MARK)
    insert = text.index(
        "INSERT INTO `{project}.{dataset}.open_intelligence_execution_consumptions_v2`"
    )
    assert replay_end < lock < check < insert
