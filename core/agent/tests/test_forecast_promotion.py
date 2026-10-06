import re
from datetime import date, datetime

import pytest
import sqlglot
from sqlglot import exp

from core.agent.forecast_promotion import PROMOTED_SQL, promoted_forecasts, publishable_line
from core.agent.tools.sql_query import HIDDEN_TABLES, MAX_BYTES_BILLED, check_sql

TODAY = date(2026, 10, 2)
FID = {name: name.ljust(64, "0") for name in ("a", "b", "c", "d", "e", "f")}


class DuckWarehouse:
    """Runs the reader's BigQuery SQL, transpiled, on fixture tables."""

    def __init__(self, con):
        self.con = con
        self.calls = []

    def dry_run(self, sql, params):
        raise AssertionError("the reader never dry-runs")

    def run(self, sql, params, max_bytes_billed):
        self.calls.append((sql, params, max_bytes_billed))
        duck = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
        used = {k: v for k, v in (params or {}).items() if re.search(rf"\${k}\b", duck)}
        cur = self.con.execute(duck, used)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def forecast(fid, *, rule="ask_v1", target="reach_rising", horizon=7, resolve=date(2026, 10, 9), observed=None,
             prob=0.7):
    return (fid, "item_x", "ZA", target, date(2026, 10, 2), horizon, rule, prob, prob >= 0.5, False, resolve, observed)


def score(rule, target, horizon, eligible, scored_at):
    return (date(2026, 9, 21), date(2026, 9, 27), f"learn_{scored_at:%d%H}", scored_at, rule, target, horizon,
            eligible)


def item_row(item, market, day, label):
    return (item, market, day, label)


def con_with(forecasts, scores, items=()):
    import duckdb  # test-only

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA intelligence_42_core")
    con.execute("CREATE SCHEMA intelligence_42_agent")
    con.execute("CREATE TABLE intelligence_42_core.v_forecasts_current (forecast_id VARCHAR, item_id VARCHAR, "
                "market VARCHAR, target VARCHAR, issue_date DATE, horizon BIGINT, rule VARCHAR, prob DOUBLE, "
                "predicted_arrival BOOLEAN, persistence_arrival BOOLEAN, resolve_date DATE, observed_arrival BOOLEAN)")
    con.execute("CREATE TABLE intelligence_42_agent.forecast_score (week_start DATE, week_end DATE, run_id VARCHAR, "
                "scored_at TIMESTAMP, rule VARCHAR, target VARCHAR, horizon BIGINT, promotion_eligible BOOLEAN)")
    con.execute("CREATE TABLE intelligence_42_agent.v_items_today (item_id VARCHAR, market VARCHAR, metric_date DATE, "
                "label VARCHAR)")
    if items:
        con.executemany("INSERT INTO intelligence_42_agent.v_items_today VALUES (?, ?, ?, ?)", items)
    if forecasts:
        con.executemany("INSERT INTO intelligence_42_core.v_forecasts_current VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", forecasts)
    if scores:
        con.executemany("INSERT INTO intelligence_42_agent.forecast_score VALUES (?, ?, ?, ?, ?, ?, ?, ?)", scores)
    return con


def test_the_reader_sql_is_fixed_read_only_and_names_only_the_forecast_tables():
    check_sql(PROMOTED_SQL, HIDDEN_TABLES)
    tree = sqlglot.parse_one(PROMOTED_SQL, dialect="bigquery")
    tables = sorted({f"{t.db}.{t.name}" for t in tree.find_all(exp.Table) if t.db})
    assert tables == ["intelligence_42_agent.forecast_score", "intelligence_42_agent.v_items_today",
                      "intelligence_42_core.v_forecasts_current"]
    selected = {c.alias_or_name for c in tree.selects}
    assert "prob" not in selected and "observed_arrival" not in selected


def test_no_ids_reads_nothing():
    wh = DuckWarehouse(con_with([], []))
    assert promoted_forecasts(wh, [], TODAY) == {}
    assert wh.calls == []


def test_only_open_forecasts_of_a_cohort_whose_latest_score_is_eligible_come_back():
    early, late = datetime(2026, 9, 28, 7), datetime(2026, 9, 28, 9)
    con = con_with(
        [forecast(FID["a"]),                                   # ask_v1 reach_rising 7: latest eligible
         forecast(FID["b"], target="cross_market"),            # latest row not eligible, an older one was
         forecast(FID["c"], horizon=14),                       # never scored
         forecast(FID["d"], resolve=date(2026, 10, 1)),        # its window has closed
         forecast(FID["e"], observed=True),                    # already resolved
         forecast(FID["f"], target="persist_50")],             # eligible NULL is not eligible
        [score("ask_v1", "reach_rising", 7, False, early), score("ask_v1", "reach_rising", 7, True, late),
         score("ask_v1", "cross_market", 7, True, early), score("ask_v1", "cross_market", 7, False, late),
         score("ask_v1", "persist_50", 7, None, late),
         score("logistic_v1", "reach_rising", 14, True, late)],
        [item_row("item_x", "ZA", date(2026, 9, 30), "Old name"), item_row("item_x", "ZA", date(2026, 10, 1), "Braai Fridays"),
         item_row("item_x", "NG", date(2026, 10, 2), "Wrong market")])
    wh = DuckWarehouse(con)

    out = promoted_forecasts(wh, list(FID.values()), TODAY)

    assert list(out) == [FID["a"]]
    assert out[FID["a"]]["rule"] == "ask_v1" and out[FID["a"]]["target"] == "reach_rising"
    assert out[FID["a"]]["horizon"] == 7
    assert (out[FID["a"]]["item_id"], out[FID["a"]]["market"], out[FID["a"]]["label"]) == ("item_x", "ZA", "Braai Fridays")
    assert out[FID["a"]]["score_run_id"] == "learn_2809"
    assert len(wh.calls) == 1 and wh.calls[0][0] == PROMOTED_SQL and wh.calls[0][2] == MAX_BYTES_BILLED


def test_with_no_promoted_cohort_nothing_comes_back():
    con = con_with([forecast(FID["a"])], [score("ask_v1", "reach_rising", 7, False, datetime(2026, 9, 28, 7))])
    assert promoted_forecasts(DuckWarehouse(con), [FID["a"]], TODAY) == {}


def test_a_forecast_that_predicts_no_arrival_is_never_promoted():
    # Every template says the item is expected to arrive, so a forecast against arrival has no line to publish.
    con = con_with([forecast(FID["a"]), forecast(FID["b"], prob=0.01)],
                   [score("ask_v1", "reach_rising", 7, True, datetime(2026, 9, 28, 7))],
                   [item_row("item_x", "ZA", date(2026, 10, 1), "Braai Fridays")])
    assert list(promoted_forecasts(DuckWarehouse(con), [FID["a"], FID["b"]], TODAY)) == [FID["a"]]


def test_ids_not_asked_for_are_ignored_even_when_returned():
    class Loose:
        def run(self, sql, params, max_bytes_billed):
            return [{"forecast_id": "zz", "rule": "ask_v1", "target": "reach_rising", "horizon": 7}, {"day": 1}]

    assert promoted_forecasts(Loose(), [FID["a"]], TODAY) == {}


@pytest.mark.parametrize("warehouse", [None, object()])
def test_a_failed_read_promotes_nothing(warehouse):
    assert promoted_forecasts(warehouse, [FID["a"]], TODAY) == {}


ROW = {"forecast_id": FID["a"], "item_id": "item_x", "market": "ZA", "label": "Braai Fridays", "rule": "ask_v1",
       "target": "reach_rising", "horizon": 7}


@pytest.mark.parametrize(("change", "line"), [
    ({}, "Braai Fridays is expected to keep rising in South Africa over the next week."),
    ({"horizon": 14}, "Braai Fridays is expected to keep rising in South Africa over the next fortnight."),
    ({"target": "cross_market", "market": "KE"},
     "Braai Fridays is expected to spread from Kenya to another market over the next week."),
    ({"target": "persist_50", "market": "NG"},
     "Braai Fridays is expected to hold at least half its current daily pace in Nigeria over the next week."),
])
def test_the_publishable_line_is_built_from_the_logged_row(change, line):
    assert publishable_line({**ROW, **change}) == line


@pytest.mark.parametrize("change", [{"label": None}, {"label": "  "}, {"label": "item_x"}, {"target": "other"},
                                    {"horizon": 30}, {"market": "GLOBAL"}])
def test_no_publishable_line_without_a_named_item_or_a_known_target(change):
    assert publishable_line({**ROW, **change}) is None
