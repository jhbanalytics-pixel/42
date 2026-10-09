"""The suppression SQL, run on fixture rows (work items N1 and R4, finding X-tests-06).

Everything that decides whether a person may be shown reads the suppression list through SQL: the
v_suppressed_creators view, the key check Ask runs before it shows a live search result, the creator and post
checks before watch_video, the brief's list of hidden creators and the two reads f42-api makes. Their tests used
fakes, so the SQL never ran. Here the tables and the view are built in DuckDB from the real CREATE statements
under core/ (harness_bq.DuckWarehouse), the real statements are run on one set of suppression rows, and the people
they return are compared with the people the rule names.

The suppression rows are append-only. The newest row per suppression_id holds, a tie goes to the row that is not
lifted, and a lifted row frees the person. A person can be named by creator_id, or by platform and handle, and a
handle-only suppression need not have a creators row. A handle is matched with X read as twitter, trimmed, lower
case and without a leading @ or u/.
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_bq as hb  # noqa: E402

ROOT = Path(__file__).resolve().parents[4]
CORE = "intelligence_42_core"


def at(day, hour=8):
    return datetime(2026, 9, day, hour, 0, 0, tzinfo=timezone.utc)


def row(suppression_id, status_at, status, creator_id=None, platform=None, handle=None):
    return dict(suppression_id=suppression_id, status_at=status_at, status=status, creator_id=creator_id,
                platform=platform, handle=handle, reason="removal request", who="albert")


CREATORS = [
    dict(creator_id="c1", platform="tiktok", handle="@Thandi.M"),
    dict(creator_id="c2", platform="twitter", handle="kwame"),
    dict(creator_id="c3", platform="reddit", handle="u/Lagos_Eats"),
    dict(creator_id="c4", platform="tiktok", handle="someone_else"),
    dict(creator_id="c5", platform="tiktok", handle="visible_person"),
    dict(creator_id="c6", platform="tiktok", handle="tie_user"),
    dict(creator_id="c7", platform="tiktok", handle="freed_by_id"),
]
SUPPRESSIONS = [
    row("s1", at(29), "suppressed", platform="TikTok", handle=" thandi.m"),            # c1 by platform and handle
    row("s2", at(29), "suppressed", creator_id="c2"),                                   # c2 by id
    row("s3", at(28), "suppressed", platform="reddit", handle="lagos_eats"),            # c3, then freed
    row("s3", at(29), "lifted", platform="reddit", handle="lagos_eats"),
    row("s4", at(29), "suppressed", platform="instagram", handle="Handle_Only"),        # handle only, no creators row
    row("s5", at(26), "suppressed", creator_id="c4"),                                   # c4: hidden, freed, hidden again
    row("s5", at(27), "lifted", creator_id="c4"),
    row("s5", at(28), "suppressed", creator_id="c4"),
    row("s6", at(29), "lifted", creator_id="c6"),                                       # c6: a tie at one instant
    row("s6", at(29), "suppressed", creator_id="c6"),
    row("s7", at(28), "suppressed", creator_id="c7"),                                   # c7: freed by id
    row("s7", at(29), "lifted", creator_id="c7"),
]
HIDDEN_IDS = {"c1", "c2", "c4", "c6"}
POSTS = [
    dict(post_id="p1", platform="tiktok", creator_id="c1", post_date="2026-09-29"),
    dict(post_id="p3", platform="reddit", creator_id="c3", post_date="2026-09-29"),
    dict(post_id="p5", platform="tiktok", creator_id="c5", post_date="2026-09-29"),
]


@pytest.fixture(scope="module")
def registry():
    return hb.load_registry(ROOT)


@pytest.fixture
def warehouse(registry):
    w = hb.DuckWarehouse(registry)
    w.insert(f"{CORE}.creators", CREATORS)
    w.insert(f"{CORE}.suppressions", SUPPRESSIONS)
    w.insert(f"{CORE}.posts", POSTS)
    return w


def ids(rows, key="creator_id"):
    return {r[key] for r in rows}


def test_the_tables_and_the_view_are_built_from_the_real_create_statements(warehouse):
    assert f"{CORE}.v_suppressed_creators" not in warehouse.uncreated
    for name in (f"{CORE}.suppressions", f"{CORE}.creators", f"{CORE}.posts"):
        assert name not in warehouse.uncreated
    assert warehouse.registry.get(CORE, "suppressions").columns[:3] == ["suppression_id", "status_at", "status"]


def test_the_view_hides_who_the_rule_names_and_frees_who_was_lifted(warehouse):
    rows = warehouse.run(f"SELECT creator_id FROM {CORE}.v_suppressed_creators")
    assert ids(rows) == HIDDEN_IDS


def test_the_view_gives_each_person_once(warehouse):
    rows = warehouse.run(f"SELECT creator_id FROM {CORE}.v_suppressed_creators")
    assert len(rows) == len(HIDDEN_IDS)


def test_asks_key_check_returns_the_hidden_keys_only(warehouse):
    from core.agent.tools import socialcrawl

    keys = ["tiktok:thandi.m", "x:kwame", "reddit:lagos_eats", "instagram:handle_only", "tiktok:someone_else",
            "tiktok:visible_person", "tiktok:tie_user", "tiktok:freed_by_id", "x:nobody"]
    rows = warehouse.run(socialcrawl.SUPPRESSED_KEYS_SQL, {"keys": "\n".join(sorted(keys))})
    assert ids(rows, "key") == {"tiktok:thandi.m", "x:kwame", "instagram:handle_only", "tiktok:someone_else",
                                "tiktok:tie_user"}


def test_asks_key_check_catches_a_handle_only_suppression_with_no_creators_row(warehouse):
    from core.agent.tools import socialcrawl

    rows = warehouse.run(socialcrawl.SUPPRESSED_KEYS_SQL, {"keys": "instagram:handle_only"})
    assert ids(rows, "key") == {"instagram:handle_only"}
    assert warehouse.run(f"SELECT 1 FROM {CORE}.creators WHERE platform = 'instagram'") == []


def test_the_creator_check_marks_a_hidden_creator_and_clears_a_freed_one(warehouse):
    from core.agent import skills

    hidden = warehouse.run(skills.CREATOR_SQL, {"key": "tiktok:thandi.m"})
    freed = warehouse.run(skills.CREATOR_SQL, {"key": "reddit:lagos_eats"})
    unknown = warehouse.run(skills.CREATOR_SQL, {"key": "tiktok:nobody"})
    assert [r["suppressed"] for r in hidden] == [True]
    assert [r["suppressed"] for r in freed] == [False]
    assert unknown == []


def test_the_watch_video_check_counts_a_hidden_creator_and_says_unknown_when_it_finds_none(warehouse):
    from core.agent.tools import enrich_tools

    params = lambda post, key: {"post_id": post, "since": "2026-09-01", "key": key}  # noqa: E731
    hidden = warehouse.run(enrich_tools.SUPPRESSED_SQL, params("p1", "tiktok:thandi.m"))[0]
    clear = warehouse.run(enrich_tools.SUPPRESSED_SQL, params("p5", "tiktok:visible_person"))[0]
    freed = warehouse.run(enrich_tools.SUPPRESSED_SQL, params("p3", "reddit:lagos_eats"))[0]
    unknown = warehouse.run(enrich_tools.SUPPRESSED_SQL, params("p0", "tiktok:nobody"))[0]
    assert (hidden["suppressed"], hidden["known"]) == (1, 1)
    assert (clear["suppressed"], clear["known"]) == (0, 1)
    assert (freed["suppressed"], freed["known"]) == (0, 1)
    assert unknown["known"] == 0, "a creator the check cannot find must read as unknown so watch_video refuses"


def named_statements(path):
    text = Path(path).read_text(encoding="utf-8")
    parts = re.split(r"(?m)^-- name: (\w+)\s*$", text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_the_briefs_hidden_list_names_every_hidden_creator_with_its_platform_and_handle(warehouse):
    sql = named_statements(ROOT / "core" / "brief" / "sql" / "evidence.sql")["suppressed"]
    rows = warehouse.run(hb.render(hb.strip_comments(sql)))
    assert ids(rows) == HIDDEN_IDS
    by_id = {r["creator_id"]: (r["platform"], r["handle"]) for r in rows}
    assert by_id["c1"] == ("tiktok", "@Thandi.M")


def capture(registry, method, *args):
    from core.api import store

    store._CATALOG.clear()
    client = hb.RecordingClient(registry, exists=True)
    result = getattr(store.BigQueryStore(project=hb.PROJECT, client=client), method)(*args)
    store._CATALOG.clear()
    return client, result


def test_the_api_reads_the_hidden_ids_from_the_view(warehouse, registry):
    client = hb.RecordingClient(registry, exists=True)
    from core.api import store

    store._CATALOG.clear()
    sql_store = store.BigQueryStore(project=hb.PROJECT, client=client)
    sql_store.suppressed_creators()
    store._CATALOG.clear()
    statement, config = client.statements[-1]
    assert ids(warehouse.run(statement, hb.bound_values(config))) == HIDDEN_IDS


def test_the_apis_sql_picks_the_same_current_row_as_its_fixture_code(warehouse, registry):
    """store.current_suppressions (the fixture store) says it follows the view's order; the SQL it must agree with
    is store.BigQueryStore.suppressions."""
    from core.api import store

    client, _ = capture(registry, "suppressions")
    statement, config = client.statements[-1]
    sql_rows = warehouse.run(statement, hb.bound_values(config))
    python_rows = store.current_suppressions([{**r, "status_at": r["status_at"].isoformat()} for r in SUPPRESSIONS])
    assert [(r["suppression_id"], r["status"]) for r in sql_rows] == [
        (r["suppression_id"], r["status"]) for r in python_rows]
    assert {(r["suppression_id"], r["status"]) for r in sql_rows} == {
        ("s1", "suppressed"), ("s2", "suppressed"), ("s3", "lifted"), ("s4", "suppressed"),
        ("s5", "suppressed"), ("s6", "suppressed"), ("s7", "lifted")}


class TestTheHarnessCanFail:
    def test_a_check_that_ignores_lifting_returns_a_freed_person(self, warehouse):
        from core.agent.tools import socialcrawl

        broken = socialcrawl.SUPPRESSED_KEYS_SQL.replace("s.status != 'lifted'", "TRUE")
        rows = warehouse.run(broken, {"keys": "reddit:lagos_eats"})
        assert ids(rows, "key") == {"reddit:lagos_eats"}, "the harness would not notice a check that ignores lifting"

    def test_a_view_that_ignores_the_newest_row_still_hides_a_person_who_was_freed_by_id(self, registry):
        w = hb.DuckWarehouse(registry)
        w.insert(f"{CORE}.suppressions", SUPPRESSIONS)
        w.con.execute(f"CREATE OR REPLACE VIEW {CORE}.v_suppressed_creators AS SELECT s.creator_id "
                      f"FROM {CORE}.suppressions s WHERE s.status = 'suppressed' AND s.creator_id IS NOT NULL")
        wrong = ids(w.run(f"SELECT creator_id FROM {CORE}.v_suppressed_creators"))
        assert "c7" in wrong and wrong != HIDDEN_IDS

    def test_an_empty_suppression_table_hides_nobody(self, registry):
        w = hb.DuckWarehouse(registry)
        w.insert(f"{CORE}.creators", CREATORS)
        assert w.run(f"SELECT creator_id FROM {CORE}.v_suppressed_creators") == []

    def test_a_statement_duckdb_cannot_run_raises_rather_than_returns_nothing(self, warehouse):
        with pytest.raises(Exception):
            warehouse.run(f"SELECT no_such_column FROM {CORE}.suppressions")
