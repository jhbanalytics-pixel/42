import hashlib
from datetime import datetime

import pytest
import sqlglot
from sqlglot import exp

from core.agent.context import Refused, RunContext
from core.agent.tools.forecast import FORECASTS_TABLE, RULE, log_forecast

# 23:30 UTC on 27 September is 01:30 SAST on 28 September, so the issue date is the 28th.
AS_OF = datetime.fromisoformat("2026-09-27T23:30:00+00:00")
STATE_ROW = {"metric_date": "2026-09-28", "state": "rising", "markets_hot": 1}


def tables_in(sql):
    tree = sqlglot.parse_one(sql, dialect="bigquery")
    return sorted({f"{t.db}.{t.name}" for t in tree.find_all(exp.Table) if t.db})


class FakeWarehouse:
    """v_items_today answers with rows; the forecasts table with existing, or raises fail_forecasts."""

    def __init__(self, rows=None, existing=(), fail_forecasts=None):
        self.rows = [STATE_ROW] if rows is None else rows
        self.existing = list(existing)
        self.fail_forecasts = fail_forecasts
        self.runs = []

    def dry_run(self, sql, params):
        return {"bytes": 1_000, "tables": [f"ogilvy-trends-v2.{t}" for t in tables_in(sql)]}

    def run(self, sql, params, max_bytes_billed):
        self.runs.append((sql, params))
        if tables_in(sql) == [FORECASTS_TABLE]:
            if self.fail_forecasts:
                raise self.fail_forecasts
            return [dict(r) for r in self.existing]
        return [dict(r) for r in self.rows]

    def run_on(self, table):
        return [(sql, params) for sql, params in self.runs if tables_in(sql) == [table]]


class FakeWriter:
    def __init__(self):
        self.inserts = []

    def insert(self, table, rows, row_ids=None):
        self.inserts.append((table, rows, row_ids))


def make_ctx():
    c = RunContext(run_id="run_1", tier="T1", as_of=AS_OF, market="ZA")
    c.evidence["p1"] = {"id": "p1", "platform": "tiktok", "market": "ZA", "text": "braai"}
    c.evidence["p2"] = {"id": "p2", "platform": "x", "market": "ZA", "text": "shisa nyama"}
    return c


GOOD = dict(item_id="item_amapiano", market="ZA", target="reach_rising", horizon_days=7, probability=0.7,
            statement="Amapiano braai clips keep rising in Durban for the next week.", evidence_ids=["p1", "p2"])


def expected_id(item="item_amapiano", market="ZA", target="reach_rising", issue="2026-09-28", horizon=7):
    return hashlib.sha256(f"{item}|{market}|{target}|{issue}|{horizon}|ask_v1".encode("utf-8")).hexdigest()


def test_log_forecast_appends_one_row():
    c, wh, writer = make_ctx(), FakeWarehouse(), FakeWriter()
    out = log_forecast(c, wh, writer, **GOOD)

    fid = expected_id()
    assert out == {"forecast_id": fid}
    assert RULE == "ask_v1"
    assert len(writer.inserts) == 1
    table, rows, row_ids = writer.inserts[0]
    assert table == FORECASTS_TABLE == "intelligence_42_agent.forecasts"
    assert row_ids == [fid]
    assert rows == [{
        "forecast_id": fid, "item_id": "item_amapiano", "market": "ZA", "target": "reach_rising",
        "issue_date": "2026-09-28", "horizon": 7, "rule": "ask_v1", "prob": 0.7, "predicted_arrival": True,
        "persistence_arrival": True, "resolve_date": "2026-10-05", "observed_arrival": None,
    }]


def test_log_forecast_keeps_the_run_record_and_emits():
    c = make_ctx()
    fid = log_forecast(c, FakeWarehouse(), FakeWriter(), **GOOD)["forecast_id"]
    assert c.forecasts[fid] == {"forecast_id": fid, "statement": GOOD["statement"], "evidence_ids": ["p1", "p2"],
                                "target": "reach_rising", "horizon": 7, "probability": 0.7}
    assert any(e["step"] == "log_forecast" and e["forecast_id"] == fid for e in c.events)


def test_log_forecast_event_keeps_the_trail():
    c = make_ctx()
    fid = log_forecast(c, FakeWarehouse(), FakeWriter(), **GOOD)["forecast_id"]
    event = next(e for e in c.events if e["step"] == "log_forecast")
    assert event == {"step": "log_forecast", "forecast_id": fid, "item_id": "item_amapiano", "target": "reach_rising",
                     "horizon": 7, "evidence_ids": ["p1", "p2"]}


def test_log_forecast_reads_v_items_today_with_named_parameters():
    c, wh = make_ctx(), FakeWarehouse()
    log_forecast(c, wh, FakeWriter(), **GOOD)
    (sql, params), = wh.run_on("intelligence_42_agent.v_items_today")
    assert "item_amapiano" not in sql and "ZA" not in sql
    assert params["item_id"] == "item_amapiano" and params["market"] == "ZA"
    assert str(params["issue_date"]) == "2026-09-28"
    assert str(params["earliest"]) == "2026-09-26" and "@earliest" in sql  # the baseline is at most 2 days old
    assert len(c.queries) == 2  # the forecasts dedup read and the baseline read


def test_log_forecast_checks_the_table_for_the_id_with_a_parameter():
    c, wh = make_ctx(), FakeWarehouse()
    log_forecast(c, wh, FakeWriter(), **GOOD)
    (sql, params), = wh.run_on(FORECASTS_TABLE)
    assert params == {"forecast_id": expected_id()} and expected_id() not in sql
    assert wh.runs[0] == (sql, params)  # read before anything else


def test_log_forecast_already_in_the_table_returns_the_id_and_writes_nothing():
    c, writer = make_ctx(), FakeWriter()
    wh = FakeWarehouse(existing=[{"forecast_id": expected_id()}])
    assert log_forecast(c, wh, writer, **GOOD) == {"forecast_id": expected_id()}
    assert writer.inserts == []
    assert wh.run_on("intelligence_42_agent.v_items_today") == []


# DATA.md section 6 keeps stored forecasts hidden from the agent until they beat persistence, so the dedup read selects
# only forecast_id and an id already in the table is recorded in the run as already_logged, with nothing it holds.

def test_log_forecast_already_in_the_table_is_recorded_in_the_run_as_already_logged_only():
    c, writer = make_ctx(), FakeWriter()
    wh = FakeWarehouse(existing=[{"forecast_id": expected_id(), "prob": 0.6, "observed_arrival": True}])
    fid = log_forecast(c, wh, writer, **GOOD)["forecast_id"]
    assert writer.inserts == []
    assert c.forecasts[fid] == {"forecast_id": fid, "already_logged": True}
    assert log_forecast(c, wh, writer, **GOOD) == {"forecast_id": fid} and len(wh.runs) == 1  # found in the run now


def test_log_forecast_dedup_read_selects_only_the_forecast_id():
    c = make_ctx()
    wh = FakeWarehouse(existing=[{"forecast_id": expected_id()}])
    log_forecast(c, wh, FakeWriter(), **GOOD)
    ((sql, params),) = wh.run_on(FORECASTS_TABLE)
    assert sql.startswith("SELECT f.forecast_id FROM ")
    assert "prob" not in sql and "observed_arrival" not in sql
    assert all("prob" not in q["sql"] and "observed_arrival" not in q["sql"] for q in c.queries.values())


def test_log_forecast_refuses_when_the_dedup_read_fails():
    c, writer = make_ctx(), FakeWriter()
    with pytest.raises(Refused, match="duplicate"):
        log_forecast(c, FakeWarehouse(fail_forecasts=RuntimeError("backend down")), writer, **GOOD)
    assert writer.inserts == [] and c.forecasts == {}


@pytest.mark.parametrize("metric_date", ["2026-09-25", "2026-09-20"])
def test_log_forecast_refuses_a_baseline_older_than_two_days(metric_date):
    writer = FakeWriter()
    wh = FakeWarehouse(rows=[{**STATE_ROW, "metric_date": metric_date}])
    with pytest.raises(Refused, match="2 days"):
        log_forecast(make_ctx(), wh, writer, **GOOD)
    assert writer.inserts == []


def test_log_forecast_accepts_a_baseline_two_days_old():
    writer = FakeWriter()
    log_forecast(make_ctx(), FakeWarehouse(rows=[{**STATE_ROW, "metric_date": "2026-09-26"}]), writer, **GOOD)
    assert len(writer.inserts) == 1


@pytest.mark.parametrize("statement, horizon", [
    ("A 70% chance amapiano braai clips keep rising in Durban over 7 days.", 7),
    ("Probability 0.7 that braai clips keep rising within 14 days.", 14),
    ("Braai clips keep rising, .7 likely, over the next 7-day window.", 7),
])
def test_log_forecast_allows_the_horizon_and_probability_as_numerals(statement, horizon):
    writer = FakeWriter()
    log_forecast(make_ctx(), FakeWarehouse(), writer, **{**GOOD, "statement": statement, "horizon_days": horizon})
    assert len(writer.inserts) == 1


@pytest.mark.parametrize("statement", [
    "Amapiano reaches 3 markets within 7 days.",
    "A 20% chance braai clips keep rising over 7 days.",
    "Braai clips keep rising over 14 days.",
    "Braai clips pass 1,000 posts this week.",
    "Braai clips keep rising, 70 likely.",
    "Braai clips double to 2x this week.",
    "Braai clips reach 7k views within 7 days.",
    "Braai clips grow 7x over 7 days.",
    "Braai clips reach 0.7m views, 0.7 likely, within 7 days.",
    "Braai clips pass R7 in ad spend within 7 days.",
    "Braai clips keep rising, 70%x likely.",
    "Braai clips keep rising for 7days.",
])
def test_log_forecast_refuses_any_other_numeral(statement):
    c, wh, writer = make_ctx(), FakeWarehouse(), FakeWriter()
    with pytest.raises(Refused, match="numeral"):
        log_forecast(c, wh, writer, **{**GOOD, "statement": statement})
    assert writer.inserts == [] and wh.runs == []


def test_log_forecast_is_idempotent_within_a_question():
    c, wh, writer = make_ctx(), FakeWarehouse(), FakeWriter()
    a = log_forecast(c, wh, writer, **GOOD)
    b = log_forecast(c, wh, writer, **{**GOOD, "probability": 0.4})
    assert a == b
    assert len(writer.inserts) == 1 and len(wh.runs) == 2  # the second call reads nothing
    assert c.forecasts[a["forecast_id"]]["probability"] == 0.7


def test_log_forecast_id_changes_with_each_key_part():
    c = make_ctx()
    base = log_forecast(c, FakeWarehouse(), FakeWriter(), **GOOD)["forecast_id"]
    other = log_forecast(c, FakeWarehouse(), FakeWriter(), **{**GOOD, "horizon_days": 14})["forecast_id"]
    assert other == expected_id(horizon=14) != base


@pytest.mark.parametrize("probability, predicted", [(0.5, True), (0.49, False), (0.99, True), (0.01, False)])
def test_predicted_arrival_is_probability_at_least_half(probability, predicted):
    writer = FakeWriter()
    log_forecast(make_ctx(), FakeWarehouse(), writer, **{**GOOD, "probability": probability})
    row = writer.inserts[0][1][0]
    assert row["prob"] == probability and row["predicted_arrival"] is predicted


@pytest.mark.parametrize("target, row, expected", [
    ("reach_rising", {"state": "rising", "markets_hot": 1}, True),
    ("reach_rising", {"state": "Peaking", "markets_hot": 1}, True),
    ("reach_rising", {"state": "emerging", "markets_hot": 3}, False),
    ("reach_rising", {"state": None, "markets_hot": 1}, None),
    ("cross_market", {"state": "emerging", "markets_hot": 2}, True),
    ("cross_market", {"state": "rising", "markets_hot": 1}, False),
    ("cross_market", {"state": "rising", "markets_hot": None}, None),
    ("persist_50", {"state": "fading", "markets_hot": 0}, True),
])
def test_persistence_arrival_follows_the_data_md_baseline(target, row, expected):
    writer = FakeWriter()
    wh = FakeWarehouse(rows=[{"metric_date": "2026-09-28", **row}])
    log_forecast(make_ctx(), wh, writer, **{**GOOD, "target": target})
    assert writer.inserts[0][1][0]["persistence_arrival"] is expected


def test_resolve_date_for_fourteen_days():
    writer = FakeWriter()
    log_forecast(make_ctx(), FakeWarehouse(), writer, **{**GOOD, "horizon_days": 14})
    assert writer.inserts[0][1][0]["resolve_date"] == "2026-10-12"


def test_log_forecast_refuses_an_item_not_in_todays_items():
    writer = FakeWriter()
    with pytest.raises(Refused, match="v_items_today"):
        log_forecast(make_ctx(), FakeWarehouse(rows=[]), writer, **GOOD)
    assert writer.inserts == []


@pytest.mark.parametrize("change", [
    {"target": "reach_peak"},
    {"target": None},
    {"horizon_days": 30},
    {"horizon_days": True},
    {"horizon_days": "7"},
    {"horizon_days": 7.5},
    {"horizon_days": 30.0},
    {"horizon_days": float("nan")},
    {"horizon_days": float("inf")},
    {"probability": 0},
    {"probability": 1},
    {"probability": 1.2},
    {"probability": -0.1},
    {"probability": True},
    {"probability": "0.7"},
    {"market": "GH"},
    {"market": "za"},
    {"evidence_ids": []},
    {"evidence_ids": ["p1", "ghost"]},
    {"statement": ""},
    {"item_id": ""},
])
def test_log_forecast_refuses_bad_input_before_touching_anything(change):
    c, wh, writer = make_ctx(), FakeWarehouse(), FakeWriter()
    with pytest.raises(Refused):
        log_forecast(c, wh, writer, **{**GOOD, **change})
    assert writer.inserts == [] and wh.runs == [] and c.forecasts == {}


@pytest.mark.parametrize("statement", [
    "Gen Z in Durban will keep this rising.",
    "Teens will carry it to Nairobi.",
    "The 18-24s will push it across markets.",
    "It will spread among middle-class households.",
    "Google Trends shows it rising.",
])
def test_log_forecast_refuses_age_demographic_and_search_volume_statements(statement):
    c, writer = make_ctx(), FakeWriter()
    with pytest.raises(Refused) as e:
        log_forecast(c, FakeWarehouse(), writer, **{**GOOD, "statement": statement})
    assert writer.inserts == []
    for word in ("Gen Z", "Teens", "18-24", "middle-class"):
        assert word not in str(e.value)  # the reason names the kind of breach, never the words


# Second review (item 8): a spelled figure in the statement is a stray numeral too, read by the checks' own reader.

@pytest.mark.parametrize("statement", [
    "Amapiano braai clips reach a million views in the next 7 days.",
    "Amapiano braai clips reach thousands of views in the next 7 days.",
    "Forty creators post amapiano braai clips in the next 7 days.",
    "Fifty percent of braai clips carry amapiano in the next 7 days.",
    "One in five braai clips carries amapiano in the next 7 days.",
    "A quarter of braai clips carry amapiano in the next 7 days.",
    "Amapiano braai clips double their reach in the next 7 days.",
])
def test_log_forecast_refuses_a_spelled_figure_in_the_statement(statement):
    c, writer = make_ctx(), FakeWriter()
    with pytest.raises(Refused, match="numeral"):
        log_forecast(c, FakeWarehouse(), writer, **dict(GOOD, statement=statement))
    assert writer.inserts == []


def test_log_forecast_keeps_small_number_words():
    c, writer = make_ctx(), FakeWriter()
    log_forecast(c, FakeWarehouse(), writer, **dict(GOOD, statement="One creator's amapiano braai clips keep rising "
                                                                     "on two platforms for the next week."))
    assert len(writer.inserts) == 1


@pytest.mark.parametrize("statement", [
    "Amapiano braai clips reach a few thousand posts in the next 7 days.",
    "Amapiano braai clips reach several million views in the next 7 days.",
    "Nine out of ten braai clips carry amapiano in the next 7 days.",
])
def test_log_forecast_refuses_a_vague_cardinal_or_out_of_share(statement):
    c, writer = make_ctx(), FakeWriter()
    with pytest.raises(Refused, match="numeral"):
        log_forecast(c, FakeWarehouse(), writer, **dict(GOOD, statement=statement))
    assert writer.inserts == []


@pytest.mark.parametrize("given, horizon", [(7.0, 7), (14.0, 14)])
def test_a_whole_number_float_horizon_is_taken_as_its_integer(given, horizon):
    c, writer = make_ctx(), FakeWriter()
    fid = log_forecast(c, FakeWarehouse(), writer, **{**GOOD, "horizon_days": given})["forecast_id"]
    row = writer.inserts[0][1][0]
    assert fid == expected_id(horizon=horizon)
    assert row["horizon"] == horizon and type(row["horizon"]) is int


def test_log_forecast_dedup_still_reads_the_table_the_agent_cannot():
    from core.agent.tools.sql_query import check_sql

    c, wh, writer = make_ctx(), FakeWarehouse(), FakeWriter()
    log_forecast(c, wh, writer, **GOOD)
    sql, _ = wh.run_on(FORECASTS_TABLE)[0]
    with pytest.raises(Refused):
        check_sql(sql)  # the agent's own sql_query may not run the read log_forecast makes
    assert len(writer.inserts) == 1
