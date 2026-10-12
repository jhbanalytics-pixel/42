"""The two production reads every reader learns hidden people's names from, run with their exact SQL text.

BigQueryStore.hidden_people_rows (History, Ask, dossiers, investigations, the watch list) and
BigQueryStore.hidden_display_names (Today, Discover, Compare and the people pages) are built here as in production
and run on DuckDB through the detect tests' harness (core/detect/tests/duck.py transpiles BigQuery SQL with
sqlglot), over tables shaped as core/schema/core.sql makes them, so a break in either statement fails a test.
Only the dataset names differ: core.creators and core.suppressions for the project's tables."""
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("sqlglot")

from core.api.store import BigQueryStore  # noqa: E402
from core.detect.tests import duck  # noqa: E402

OBJECTS = {"creators": "core.creators", "suppressions": "core.suppressions",
           "v_suppressed_creators": "core.v_suppressed_creators"}


class DuckStore(BigQueryStore):
    """BigQueryStore with its statements run on DuckDB: the same SQL text, the same parameters."""

    def __init__(self, con, objects=OBJECTS):
        self.con, self.objects, self.sql = con, objects, []

    def _find(self, name):
        return self.objects.get(name)

    def _query(self, sql, **params):
        self.sql.append(sql)
        return duck.query(self.con, sql, {k: list(v) if t.startswith("ARRAY<") else v for k, (t, v) in params.items()})


def world(creators, suppressions):
    con = duck.connect(views=False)
    con.execute("DROP VIEW core.v_suppressed_creators")
    con.execute("DROP TABLE core.creators")
    con.execute("CREATE TABLE core.creators (creator_id VARCHAR NOT NULL, platform VARCHAR, handle VARCHAR, "
                "followers BIGINT, tier VARCHAR, display_name VARCHAR)")
    con.execute("CREATE TABLE core.suppressions (suppression_id VARCHAR NOT NULL, status_at TIMESTAMPTZ NOT NULL, "
                "status VARCHAR NOT NULL, creator_id VARCHAR, platform VARCHAR, handle VARCHAR, reason VARCHAR, "
                "who VARCHAR)")
    # v_suppressed_creators as core/schema/core.sql defines it, written for DuckDB.
    con.execute("CREATE VIEW core.v_suppressed_creators AS WITH s AS (SELECT * FROM core.suppressions "
                "QUALIFY ROW_NUMBER() OVER (PARTITION BY suppression_id ORDER BY status_at DESC, status = 'lifted') = 1) "
                "SELECT creator_id FROM s WHERE status != 'lifted' AND creator_id IS NOT NULL")
    duck.load(con, "core.creators", creators)
    duck.load(con, "core.suppressions", suppressions)
    return DuckStore(con)


CREATORS = [
    # Hidden by id, written under two names across two creators rows.
    {"creator_id": "c_two", "platform": "tiktok", "handle": "two_names", "followers": 900, "display_name": "Thabo Mokoena"},
    {"creator_id": "c_two", "platform": "instagram", "handle": "two_names_ig", "followers": 50,
     "display_name": "Thabo M Dances"},
    # Hidden by id; the row with the most followers has no display name, the other one has.
    {"creator_id": "c_top_blank", "platform": "tiktok", "handle": "blank_top", "followers": 5000, "display_name": None},
    {"creator_id": "c_top_blank", "platform": "x", "handle": "blank_top_x", "followers": 10,
     "display_name": "Zinhle Dube"},
    # Hidden by handle alone (the suppression row names a platform and a handle, no creator id).
    {"creator_id": "c_by_handle", "platform": "twitter", "handle": "@Lerato_K", "followers": 300,
     "display_name": "Lerato Khumalo"},
    # Lifted: no longer hidden.
    {"creator_id": "c_lifted", "platform": "tiktok", "handle": "lifted_one", "followers": 70, "display_name": "Sipho Lifted"},
    # Never hidden.
    {"creator_id": "c_other", "platform": "tiktok", "handle": "other_one", "followers": 70, "display_name": "Ayanda Seen"},
]
SUPPRESSIONS = [
    {"suppression_id": "s1", "status_at": "2026-10-01T08:00:00+00:00", "status": "active", "creator_id": "c_two"},
    {"suppression_id": "s2", "status_at": "2026-10-01T08:00:00+00:00", "status": "active", "creator_id": "c_top_blank"},
    {"suppression_id": "s3", "status_at": "2026-10-01T08:00:00+00:00", "status": "active", "platform": "x",
     "handle": "lerato_k"},
    {"suppression_id": "s4", "status_at": "2026-10-01T08:00:00+00:00", "status": "active", "creator_id": "c_lifted"},
    {"suppression_id": "s4", "status_at": "2026-10-02T08:00:00+00:00", "status": "lifted", "creator_id": "c_lifted"},
]
HIDDEN_NAMES = {"Thabo Mokoena", "Thabo M Dances", "Zinhle Dube", "Lerato Khumalo"}


def test_hidden_people_rows_reads_every_hidden_persons_names_in_one_job():
    store = world(CREATORS, SUPPRESSIONS)
    rows = store.hidden_people_rows()
    assert rows["ids"] == {"c_two", "c_top_blank"}
    assert ("x", "lerato_k") in rows["people"] and ("tiktok", "two_names") in rows["people"]
    assert rows["names"] == HIDDEN_NAMES
    assert len(store.sql) == 1


def test_hidden_display_names_by_id_and_by_handle():
    store = world(CREATORS, SUPPRESSIONS)
    assert store.hidden_display_names(["c_two", "c_top_blank"], []) == {"Thabo Mokoena", "Thabo M Dances",
                                                                         "Zinhle Dube"}
    assert store.hidden_display_names([], ["x:lerato_k"]) == {"Lerato Khumalo"}
    assert store.hidden_display_names(["c_other"], ["tiktok:other_one"]) == {"Ayanda Seen"}
    assert store.hidden_display_names(["nobody"], ["tiktok:nobody"]) == set()


def test_nobody_hidden_reads_no_name():
    store = world(CREATORS, [])
    assert store.hidden_people_rows() == {"ids": set(), "people": [], "names": set()}
    jobs = len(store.sql)
    assert store.hidden_display_names([], []) == set() and len(store.sql) == jobs


def test_without_a_creators_table_the_list_still_reads():
    store = world(CREATORS, SUPPRESSIONS)
    store.objects = {k: v for k, v in OBJECTS.items() if k != "creators"}
    rows = store.hidden_people_rows()
    assert rows["ids"] == {"c_two", "c_top_blank"} and rows["names"] == set()
    assert store.hidden_display_names(["c_two"], []) == set()
