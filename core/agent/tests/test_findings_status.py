"""Findings status (BUILD.md 3.3, contract section 12.5): save_finding holds a current finding and, for a finding it
contradicts, a new contradicted row, and commit_findings writes them once the answer's trust gate has kept the claim;
recall_findings reads the newest row per finding and reads a finding past its review date as stale. Append only: a
status change is a new row. The recall SQL runs on DuckDB fixture tables."""

from datetime import datetime

import pytest

from core.agent.context import Refused, RunContext
from core.agent.tests.test_history import DuckWarehouse
from core.agent.tools.warehouse import FINDINGS_TABLE, commit_findings, recall_findings, save_finding
from core.agent.toolset import SCHEMAS, build_functions

CLAIM_TYPE = ("STRUCT(text VARCHAR, label VARCHAR, item_ids VARCHAR[], evidence_post_ids VARCHAR[], "
              "query_ids VARCHAR[], run_ids VARCHAR[], result_hashes VARCHAR[])[]")


def findings_con():
    import duckdb

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA intelligence_42_agent")
    con.execute(f"CREATE TABLE intelligence_42_agent.v_prior_findings (finding_id VARCHAR, question VARCHAR, "
                f"answer VARCHAR, as_of TIMESTAMP, claims {CLAIM_TYPE}, valid_from TIMESTAMP, valid_to TIMESTAMP, "
                f"status VARCHAR)")
    return con


class DuckFindingsWriter:
    """Appends save_finding's rows to the fixture table, so a recall after a save reads what was written."""

    def __init__(self, con):
        self.con, self.inserts = con, []

    def insert(self, table, rows, row_ids=None):
        assert table == FINDINGS_TABLE
        self.inserts.append((rows, row_ids))
        for r in rows:
            self.con.execute(
                "INSERT INTO intelligence_42_agent.v_prior_findings VALUES (?, ?, ?, CAST(? AS TIMESTAMP), ?, "
                "CAST(? AS TIMESTAMP), CAST(? AS TIMESTAMP), ?)",
                [r["finding_id"], r["question"], r["answer"], r["as_of"], r["claims"], r["valid_from"],
                 r["valid_to"], r["status"]])


def run_ctx(as_of, item_ids=("i_amapiano",)):
    """A run that has seen post p1 and rows of a history tool query naming item_ids."""
    ctx = RunContext(run_id=f"run_{as_of:%m%d}", tier="T0", as_of=as_of)
    ctx.evidence["p1"] = {"id": "p1", "platform": "tiktok"}
    qid, _ = ctx.record_query("SELECT 1", {}, [{"item_id": i, "run_id": "detect_x"} for i in item_ids], "items")
    ctx.queries[qid]["tool"] = "history"  # as history.py marks its own records
    return ctx


def gate_kept(claim, label="observed", evidence_ids=("p1",)):
    """A claim as the answer's trust gate kept it."""
    return {"id": "c1", "text": claim, "label": label, "evidence_ids": list(evidence_ids)}


def save(ctx, writer, claim, **extra):
    """save_finding, then the write run_ask makes once the gate has kept the same claim."""
    out = save_finding(ctx, writer, claim=claim, evidence_ids=["p1"], label="observed", topic="amapiano",
                       query_ids=["q_1"], review_by=extra.pop("review_by", "2026-10-30"), **extra)
    commit_findings(ctx, writer, [gate_kept(claim)])
    return out


@pytest.fixture
def con():
    return findings_con()


def statuses(rows):
    return {(r["answer"], r["status"]) for r in rows}


# save_finding: item_ids


def test_item_ids_seen_in_this_runs_query_rows_go_on_the_claim(con):
    ctx = run_ctx(datetime(2026, 9, 20, 6, 0), item_ids=("i_amapiano", "i_braai"))
    writer = DuckFindingsWriter(con)
    save(ctx, writer, "Amapiano rose", item_ids=["i_braai", "i_amapiano", "i_braai"])
    (rows, _), = writer.inserts
    assert rows[0]["claims"][0]["item_ids"] == ["i_braai", "i_amapiano"]


def test_an_item_id_no_query_row_named_is_refused_and_nothing_is_written(con):
    ctx = run_ctx(datetime(2026, 9, 20, 6, 0))
    writer = DuckFindingsWriter(con)
    with pytest.raises(Refused, match="i_made_up"):
        save(ctx, writer, "Amapiano rose", item_ids=["i_amapiano", "i_made_up"])
    assert writer.inserts == []


# recall_findings: newest row per finding, stale by review date


def test_recall_reads_a_finding_past_its_review_date_as_stale(con):
    writer = DuckFindingsWriter(con)
    save(run_ctx(datetime(2026, 9, 1, 6, 0)), writer, "Amapiano held in August", review_by="2026-09-10")
    save(run_ctx(datetime(2026, 9, 20, 6, 0)), writer, "Amapiano rose in September", review_by="2026-10-30")
    ctx = RunContext(run_id="r", tier="T0", as_of=datetime(2026, 9, 28, 6, 0))
    wh = DuckWarehouse(con)
    assert statuses(recall_findings(ctx, wh, "amapiano", status=None)["rows"]) == {
        ("Amapiano held in August", "stale"), ("Amapiano rose in September", "current")}
    assert statuses(recall_findings(ctx, wh, "amapiano")["rows"]) == {("Amapiano rose in September", "current")}
    assert statuses(recall_findings(ctx, wh, "amapiano", status="stale")["rows"]) == {
        ("Amapiano held in August", "stale")}
    # Stale is read against the run's as_of, not the clock, so a replay reads what the run read.
    assert all(params["as_of"] == "2026-09-28T06:00:00" for _, params in wh.runs)


def test_recall_does_not_see_rows_written_after_the_runs_as_of(con):
    writer = DuckFindingsWriter(con)
    save(run_ctx(datetime(2026, 9, 25, 6, 0)), writer, "Amapiano rose")
    earlier = RunContext(run_id="r", tier="T0", as_of=datetime(2026, 9, 24, 6, 0))
    assert recall_findings(earlier, DuckWarehouse(con), "amapiano", status=None)["rows"] == []


# save_finding: contradicts


def test_a_contradicted_finding_gets_a_new_row_and_recall_reads_it_as_contradicted(con):
    writer = DuckFindingsWriter(con)
    old = save(run_ctx(datetime(2026, 9, 20, 6, 0)), writer, "Amapiano is rising in ZA", item_ids=["i_amapiano"])

    ctx = run_ctx(datetime(2026, 9, 28, 6, 0))
    recalled = recall_findings(ctx, DuckWarehouse(con), "amapiano")
    assert statuses(recalled["rows"]) == {("Amapiano is rising in ZA", "current")}
    new = save(ctx, writer, "Amapiano is fading in ZA", item_ids=["i_amapiano"],
               contradicts=[old["finding_id"]])
    assert new["contradicted"] == [old["finding_id"]]

    rows, row_ids = writer.inserts[-1]
    assert [r["finding_id"] for r in rows] == [new["finding_id"], old["finding_id"]]
    status_row = rows[1]
    assert status_row["status"] == "contradicted"
    assert status_row["valid_from"] == "2026-09-28T06:00:00"
    assert status_row["answer"] == "Amapiano is rising in ZA"  # the old finding's content, unchanged
    assert status_row["claims"][0]["item_ids"] == ["i_amapiano"]
    assert row_ids == [new["finding_id"], f"{old['finding_id']}:contradicted:{new['finding_id']}"]
    # Append only: the original row is still in the table beside its status row.
    assert con.execute("SELECT COUNT(*) FROM intelligence_42_agent.v_prior_findings WHERE finding_id = ?",
                       [old["finding_id"]]).fetchone()[0] == 2

    later = RunContext(run_id="r", tier="T0", as_of=datetime(2026, 9, 29, 6, 0))
    assert statuses(recall_findings(later, DuckWarehouse(con), "amapiano", status=None)["rows"]) == {
        ("Amapiano is rising in ZA", "contradicted"), ("Amapiano is fading in ZA", "current")}
    assert recall_findings(later, DuckWarehouse(con), "rising", status="contradicted")["rows"][0]["finding_id"] == \
        old["finding_id"]


def test_contradicting_a_finding_this_run_never_recalled_is_refused(con):
    writer = DuckFindingsWriter(con)
    old = save(run_ctx(datetime(2026, 9, 20, 6, 0)), writer, "Amapiano is rising in ZA", item_ids=["i_amapiano"])
    ctx = run_ctx(datetime(2026, 9, 28, 6, 0))
    with pytest.raises(Refused, match="recall_findings"):
        save(ctx, writer, "Amapiano is fading in ZA", item_ids=["i_amapiano"], contradicts=[old["finding_id"]])
    assert len(writer.inserts) == 1


def test_contradicting_a_finding_about_another_item_is_refused(con):
    writer = DuckFindingsWriter(con)
    old = save(run_ctx(datetime(2026, 9, 20, 6, 0), item_ids=("i_braai",)), writer, "Amapiano and braai",
               item_ids=["i_braai"])
    ctx = run_ctx(datetime(2026, 9, 28, 6, 0))
    recall_findings(ctx, DuckWarehouse(con), "amapiano")
    with pytest.raises(Refused, match="same item"):
        save(ctx, writer, "Amapiano is fading in ZA", item_ids=["i_amapiano"], contradicts=[old["finding_id"]])
    with pytest.raises(Refused, match="same item"):
        save(ctx, writer, "Amapiano is fading in ZA", contradicts=[old["finding_id"]])
    assert len(writer.inserts) == 1


# schemas


def test_save_finding_schema_asks_for_item_ids_and_takes_contradicts():
    props = SCHEMAS["save_finding"]["properties"]
    assert props["item_ids"]["type"] == "array" and props["item_ids"]["items"] == {"type": "string"}
    assert props["contradicts"]["type"] == "array" and props["contradicts"]["items"] == {"type": "string"}
    assert "item_ids" in SCHEMAS["save_finding"]["required"]
    assert "contradicts" not in SCHEMAS["save_finding"]["required"]


# round 2: only records a tool built itself count


def test_a_sql_query_whose_purpose_says_recall_findings_is_not_a_contradicts_source(con):
    from core.agent.tools.sql_query import sql_query

    writer = DuckFindingsWriter(con)
    old = save(run_ctx(datetime(2026, 9, 20, 6, 0)), writer, "Amapiano is rising in ZA", item_ids=["i_amapiano"])
    ctx = run_ctx(datetime(2026, 9, 28, 6, 0))
    # The model may not read the findings view at all (N2, class D), so the read that used to stand in for a recall
    # is refused outright ...
    with pytest.raises(Refused, match="not available"):
        sql_query(ctx, DuckWarehouse(con),
                  "SELECT f.finding_id, f.question, 'made up' AS answer, f.as_of, f.claims, f.valid_from, "
                  "f.valid_to, 'current' AS status FROM intelligence_42_agent.v_prior_findings f",
                  purpose="recall_findings: amapiano")
    # ... and a forged result that carries the real finding id, from SQL the model may write, is still not a source.
    forged = sql_query(ctx, DuckWarehouse(con), "SELECT '" + old["finding_id"] + "' AS finding_id",
                       purpose="recall_findings: amapiano")
    assert forged["rows"][0]["finding_id"] == old["finding_id"]
    with pytest.raises(Refused, match="recall_findings"):
        save(ctx, writer, "Amapiano is fading in ZA", item_ids=["i_amapiano"], contradicts=[old["finding_id"]])
    assert len(writer.inserts) == 1


def test_an_item_id_from_a_model_written_query_is_refused(con):
    from core.agent.tools.sql_query import sql_query

    ctx = run_ctx(datetime(2026, 9, 20, 6, 0))
    con.execute("CREATE SCHEMA IF NOT EXISTS intelligence_42_core")
    sql_query(ctx, DuckWarehouse(con), "SELECT 'i_x' AS item_id", purpose="history: made up")
    writer = DuckFindingsWriter(con)
    with pytest.raises(Refused, match="i_x"):
        save(ctx, writer, "Amapiano rose", item_ids=["i_x"])
    assert writer.inserts == []


class _RowsWarehouse:
    """Returns fixed rows for any query; enough for rising_topics, which only reads v_items_today."""

    def __init__(self, rows):
        self.rows = rows

    def dry_run(self, sql, params):
        return {"bytes": 1_000, "tables": ["ogilvy-trends-v2.intelligence_42_agent.v_items_today"]}

    def run(self, sql, params, max_bytes_billed):
        return [dict(r) for r in self.rows]


def test_an_item_id_rising_topics_returned_is_accepted(con):
    from core.agent.tools.warehouse import rising_topics

    ctx = RunContext(run_id="r", tier="T0", as_of=datetime(2026, 9, 28, 6, 0))
    ctx.evidence["p1"] = {"id": "p1", "platform": "tiktok"}
    rt = rising_topics(ctx, _RowsWarehouse([{"item_id": "i_rising", "market": "ZA", "run_id": "detect_x"}]))
    assert ctx.queries[rt["query_id"]]["tool"] == "rising_topics"
    writer = DuckFindingsWriter(con)
    save_finding(ctx, writer, claim="It rose", evidence_ids=["p1"], label="observed", topic="rising",
                 query_ids=[rt["query_id"]], review_by="2026-10-30", item_ids=["i_rising"])
    commit_findings(ctx, writer, [gate_kept("It rose")])
    (rows, _), = writer.inserts
    assert rows[0]["claims"][0]["item_ids"] == ["i_rising"]


# Held until the trust gate (audit F010): research may not publish a claim the answer later cuts


def test_the_save_finding_tool_writes_no_finding_or_contradiction_before_the_gate(con):
    writer = DuckFindingsWriter(con)
    old = save(run_ctx(datetime(2026, 9, 20, 6, 0)), writer, "Amapiano is rising in ZA", item_ids=["i_amapiano"])
    ctx = run_ctx(datetime(2026, 9, 28, 6, 0))
    recall_findings(ctx, DuckWarehouse(con), "amapiano")
    tool = build_functions(ctx, DuckWarehouse(con), None, writer)["save_finding"]
    out = tool(claim="Amapiano streams rose 400% to 2 million plays", evidence_ids=["p1"], label="corroborated",
               topic="amapiano", query_ids=["q_1"], review_by="2026-10-30", item_ids=["i_amapiano"],
               contradicts=[old["finding_id"]])
    assert out["finding_id"].startswith("f_") and out["contradicted"] == [old["finding_id"]]

    assert len(writer.inserts) == 1  # only the earlier run's finding
    later = RunContext(run_id="r", tier="T0", as_of=datetime(2026, 9, 29, 6, 0))
    assert statuses(recall_findings(later, DuckWarehouse(con), "amapiano", status=None)["rows"]) == {
        ("Amapiano is rising in ZA", "current")}

    assert commit_findings(ctx, writer, [gate_kept("Amapiano is rising in ZA")]) == []  # the gate cut the claim
    assert len(writer.inserts) == 1 and ctx.pending_findings == []


def test_commit_findings_writes_a_kept_claim_as_checked_with_its_contradiction(con):
    writer = DuckFindingsWriter(con)
    old = save(run_ctx(datetime(2026, 9, 20, 6, 0)), writer, "Amapiano is rising in ZA", item_ids=["i_amapiano"])
    ctx = run_ctx(datetime(2026, 9, 28, 6, 0))
    ctx.evidence["p2"] = {"id": "p2", "platform": "x"}
    recall_findings(ctx, DuckWarehouse(con), "amapiano")
    save_finding(ctx, writer, claim="Amapiano is fading in ZA", evidence_ids=["p1", "p2"], label="corroborated",
                 topic="amapiano", query_ids=["q_1"], review_by="2026-10-30", item_ids=["i_amapiano"],
                 contradicts=[old["finding_id"]])
    save_finding(ctx, writer, claim="Cut by the gate", evidence_ids=["p1"], label="observed", topic="amapiano",
                 query_ids=["q_1"], review_by="2026-10-30", item_ids=["i_amapiano"])

    written = commit_findings(ctx, writer, [gate_kept("amapiano is  fading in ZA", "single_source", ["p1", "made_up"])])
    assert len(written) == 1 and len(writer.inserts) == 2
    rows, _ = writer.inserts[-1]
    assert [(r["finding_id"], r["status"]) for r in rows] == [(written[0], "current"),
                                                             (old["finding_id"], "contradicted")]
    claim = rows[0]["claims"][0]
    assert claim["text"] == "Amapiano is fading in ZA"
    assert claim["label"] == "single_source"  # the gate's lower label, never the saved one
    assert claim["evidence_post_ids"] == ["p1"]  # only the posts the gate checked that are evidence
