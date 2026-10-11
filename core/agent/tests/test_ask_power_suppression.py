"""Ask power review, Critical 1 (agent side): a follow-up never reads a suppressed creator's post back in.

The reuse path turns a parent's cited ids into a store read. The parent's answer is projected before its ids are read,
but a creator hidden after that answer was stored, or a span whose post was hidden, must not come back through the ids.
The reuse read carries the suppression filter; the gate's own listed-id read is unchanged. The warehouse is DuckDB, so
the SQL that runs is the SQL that ships."""

from datetime import date, datetime

import pytest

from core.agent import ask
from core.agent.context import RunContext
from core.agent.tests.test_ask_depth_replay import ReplayWarehouse, posts_table
from core.agent.tools import sql_query as sq
from core.agent.tools.warehouse import fetch_posts

DAY = date(2026, 9, 25)
WINDOW = (date(2026, 9, 22), date(2026, 9, 28))


def store():
    import duckdb

    con = duckdb.connect()
    posts_table(con)
    con.execute("CREATE VIEW intelligence_42_core.v_suppressed_creators AS SELECT 'c_hidden' AS creator_id")
    for pid, creator in (("p_ok", "c_ok"), ("p_hidden", "c_hidden"), ("p_anon", None)):
        con.execute("INSERT INTO intelligence_42_core.posts VALUES (?, 'tiktok', ?, ?, NULL, ?, 'ZA', 'geotag', "
                    "'amapiano words', 10, 1, NULL, NULL, 11, ['amapiano'])", [pid, f"https://p.example/{pid}", creator, DAY])
    return con


@pytest.fixture
def ctx():
    return RunContext(run_id="r_x", tier="T1", as_of=datetime(2026, 9, 28, 8, 0), market="ZA",
                      window_start=WINDOW[0], window_end=WINDOW[1])


def test_the_gates_own_fetch_is_unchanged(ctx):
    wh = ReplayWarehouse(store())
    out = fetch_posts(ctx, wh, ["p_ok", "p_hidden", "p_anon"], WINDOW)
    assert {e["id"] for e in out["evidence"]} == {"p_ok", "p_hidden", "p_anon"}
    assert "v_suppressed_creators" not in wh.runs[0][0]


def test_the_reuse_fetch_leaves_out_a_suppressed_creators_post(ctx):
    wh = ReplayWarehouse(store())
    out = fetch_posts(ctx, wh, ["p_ok", "p_hidden", "p_anon"], WINDOW, exclude_suppressed=True)
    assert {e["id"] for e in out["evidence"]} == {"p_ok", "p_anon"}
    assert "p_hidden" not in ctx.evidence and "v_suppressed_creators" in wh.runs[0][0]


def test_only_the_registered_reuse_read_may_touch_the_suppression_view(ctx):
    assert set(sq.INTERNAL_ALLOW["fetch_posts_reuse"]) == {sq._SUPPRESSED_VIEW, sq._SUPPRESSIONS}
    with pytest.raises(Exception):
        sq.check_sql("SELECT creator_id FROM intelligence_42_core.v_suppressed_creators")


def test_a_follow_up_reads_its_parents_posts_without_the_suppressed_one(ctx):
    from core.agent.tests.test_ask import Harness
    from core.agent.tests.test_ask_power_followup import WINDOW as PARENT_WINDOW
    from core.agent.tests.test_ask_power_followup import parent

    seen = {}
    wh = ReplayWarehouse(store())

    def research(c, prompt, options, emit, should_stop):
        seen.update(evidence=set(c.evidence), prompt=prompt)
        return {"note": "", "tokens": {"input": 1, "output": 1}, "usd": 0.0}

    h = Harness(research=research)
    h.warehouse = h.deps.warehouse = wh
    h.run(question="Which creators are driving it?", market="ZA",
          parent=parent("p_ok", "p_hidden_span_1", "p_hidden", window=PARENT_WINDOW))
    assert seen["evidence"] == {"p_ok"} and "p_hidden" not in seen["prompt"]
