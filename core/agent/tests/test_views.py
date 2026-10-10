import re
from datetime import datetime
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from core.agent.apply_views import VIEWS_SQL, apply, statements
import pytest

from core.agent.context import Refused, RunContext
from core.agent.tools.sql_query import check_sql
from core.agent.tools.warehouse import recall_findings, rising_topics

# Staging columns as read on 28 Sep 2026 (the conductor's brief).
ITEM_STATE = [
    "metric_date", "market", "item_id", "kind", "state_raw", "state", "untested", "main_series_id", "main_y",
    "main_mu", "main_ratio", "q_min", "sig_days3", "creators3", "posts3", "top_creator_share3", "authenticity",
    "share_flags", "sponsored_share", "geo_status", "local_share", "geo_known_posts7", "spread_platforms",
    "found_platforms", "markets_hot", "lead_market", "diffusion", "novelty", "last_wave", "moment", "eligible",
    "worth_raw", "worth_pct", "run_id", "rule_version",
]
RUNS = [
    "run_id", "stage", "run_date", "status", "started_at", "finished_at", "counts", "error", "question", "tier",
    "plan", "calls", "credits", "tokens", "seconds", "outcome", "answer", "record",
]
FINDINGS = ["finding_id", "question", "answer", "as_of", "claims", "valid_from", "valid_to", "status"]
CULTURAL_MAP = [
    "item_id", "kind", "canonical_key", "label", "aliases", "parent_item_id", "centroid", "first_seen",
    "first_seen_market", "first_seen_platform", "last_seen", "recurrences", "lifecycle", "status", "rejected_until",
    "valid_from", "valid_to",
]
SCHEMA = {
    "ogilvy-trends-v2": {
        "intelligence_42_core": {"item_state": {c: "STRING" for c in ITEM_STATE},
                                 "cultural_map": {c: "STRING" for c in CULTURAL_MAP}},
        "intelligence_42_agent": {"runs": {c: "STRING" for c in RUNS}, "findings": {c: "STRING" for c in FINDINGS}},
    }
}


def views():
    out = {}
    for sql in statements(VIEWS_SQL.read_text(encoding="utf-8")):
        tree = sqlglot.parse_one(sql, dialect="bigquery")
        assert isinstance(tree, exp.Create) and tree.kind == "VIEW"
        out[tree.this.name] = (sql, tree)
    return out


def view_columns(tree) -> list[str]:
    body = qualify(tree.expression.copy(), schema=SCHEMA, dialect="bigquery", catalog="ogilvy-trends-v2")
    return body.named_selects


def referenced(sql: str, alias: str) -> set[str]:
    tree = sqlglot.parse_one(sql, dialect="bigquery")
    return {c.name for c in tree.find_all(exp.Column) if c.table == alias}


class FakeWarehouse:
    def __init__(self):
        self.runs = []

    def dry_run(self, sql, params):
        return {"bytes": 1_000, "tables": []}

    def run(self, sql, params, max_bytes_billed):
        self.runs.append(sql)
        return []


def ctx():
    return RunContext(run_id="run_test", tier="T0", as_of=datetime(2026, 9, 28, 6, 0))


def test_views_sql_defines_exactly_the_two_views_with_if_not_exists():
    found = views()
    assert sorted(found) == ["v_items_today", "v_prior_findings"]
    for sql, tree in found.values():
        assert re.match(r"\s*CREATE\s+VIEW\s+IF\s+NOT\s+EXISTS\s", sql, re.I)
        assert tree.args.get("exists") is True
        assert tree.this.db == "intelligence_42_agent"


def test_views_sql_never_replaces_drops_or_deletes():
    text = VIEWS_SQL.read_text(encoding="utf-8")
    assert not re.search(r"\b(REPLACE|DROP|DELETE|TRUNCATE|MERGE|INSERT|UPDATE)\b", text, re.I)


# The class D objects each view body reads, written out (C5 12.9): v_items_today reads runs for its as_of, and
# v_prior_findings reads findings. A body that reads anything more fails the first check below.
BODY_DEPS = {"v_items_today": ("intelligence_42_agent.runs",), "v_prior_findings": ("intelligence_42_agent.findings",)}


def test_view_bodies_are_read_only_queries_over_allowed_datasets():
    assert set(views()) == set(BODY_DEPS)
    for name, (_, tree) in views().items():
        body = tree.expression.sql(dialect="bigquery")
        check_sql(body, hidden_ok=BODY_DEPS[name])
        with pytest.raises(Refused):
            check_sql(body)  # without the dependency named, the body is a read of a class D object


def test_items_today_exposes_every_item_state_column_plus_label_and_as_of():
    cols = view_columns(views()["v_items_today"][1])
    assert cols[:len(ITEM_STATE)] == ITEM_STATE
    assert set(cols) == set(ITEM_STATE) | {"label", "as_of"}
    assert len(cols) == len(set(cols))


def test_items_today_reads_the_latest_good_detect_run_per_date():
    sql, tree = views()["v_items_today"]
    body = tree.expression
    assert {t.name for t in body.find_all(exp.Table)} == {"item_state", "runs", "cultural_map"}
    literals = {lit.this for lit in body.find_all(exp.Literal) if lit.is_string}
    assert {"detect", "ok"} <= literals
    squashed = " ".join(sql.split())
    assert "PARTITION BY r.run_date ORDER BY r.finished_at DESC" in squashed
    assert "g.run_date = s.metric_date AND g.run_id = s.run_id" in squashed
    assert "g.finished_at AS as_of" in squashed


def _join(body, alias):
    return next(j for j in body.args["joins"] if j.alias_or_name == alias)


def test_items_today_label_is_the_current_cultural_map_label_falling_back_to_item_id():
    body = views()["v_items_today"][1].expression
    label = next(e for e in body.expressions if e.alias_or_name == "label").this
    assert isinstance(label, exp.Coalesce)
    first, fallback = label.this, label.expressions
    assert isinstance(first, exp.Column) and (first.table, first.name) == ("cm", "label")
    assert [(c.table, c.name) for c in fallback] == [("s", "item_id")]
    assert body.args["from_"].this.name == "item_state" and body.args["from_"].this.alias == "s"

    cm = _join(body, "cm")
    assert cm.side == "LEFT"
    assert {(c.table, c.name) for c in cm.args["on"].find_all(exp.Column)} == {("cm", "item_id"), ("s", "item_id")}
    sub = cm.this.this
    assert sub.args["from_"].this.name == "cultural_map"
    where = " ".join(sub.args["where"].sql(dialect="bigquery").split())
    assert re.fullmatch(r"WHERE \w+\.valid_to IS NULL", where)
    window = sub.find(exp.Window)
    assert [p.name for p in window.args["partition_by"]] == ["item_id"]
    order = window.args["order"].expressions
    assert order[0].this.name == "valid_from" and order[0].args.get("desc") is True
    assert "ROW_NUMBER() OVER" in sub.args["qualify"].sql(dialect="bigquery")
    assert sub.args["qualify"].this.expression.name == "1"


def test_items_today_other_joins_stay_inner():
    body = views()["v_items_today"][1].expression
    assert not _join(body, "g").side


def test_prior_findings_exposes_findings_columns_and_current_flag():
    sql, tree = views()["v_prior_findings"]
    cols = view_columns(tree)
    assert set(cols) == set(FINDINGS) | {"current"}
    assert "valid_to IS NULL OR f.valid_to > CURRENT_TIMESTAMP()" in " ".join(sql.split())


def test_rising_topics_reads_only_columns_v_items_today_exposes():
    wh = FakeWarehouse()
    rising_topics(ctx(), wh, market="ZA", kind="hashtag", min_platforms=2)
    sql = wh.runs[0]
    check_sql(sql)
    assert "intelligence_42_agent.v_items_today" in sql
    used = referenced(sql, "t")
    assert used and used <= set(view_columns(views()["v_items_today"][1]))


def test_recall_findings_reads_only_columns_v_prior_findings_exposes():
    wh = FakeWarehouse()
    recall_findings(ctx(), wh, "amapiano braai", since="2026-09-01")
    sql = wh.runs[0]
    check_sql(sql, hidden_ok=("intelligence_42_agent.v_prior_findings", "intelligence_42_agent.findings"))
    with pytest.raises(Refused):
        check_sql(sql)  # the tool's own read; model SQL may not name the view
    assert "intelligence_42_agent.v_prior_findings" in sql
    used = referenced(sql, "f")
    assert used and used <= set(view_columns(views()["v_prior_findings"][1]))


def test_apply_runs_each_statement_once_in_order():
    seen = []
    applied = apply(seen.append)
    expected = statements(VIEWS_SQL.read_text(encoding="utf-8"))
    assert seen == expected == applied
    assert len(seen) == 2
    assert "v_items_today" in seen[0] and "v_prior_findings" in seen[1]


def test_statements_split_on_semicolons_and_skip_comments():
    text = "-- a note; with a semicolon\nCREATE VIEW IF NOT EXISTS a.b AS SELECT 1;\n\n-- tail\nSELECT 2;\n"
    assert statements(text) == ["CREATE VIEW IF NOT EXISTS a.b AS SELECT 1", "SELECT 2"]


def test_views_sql_lives_next_to_the_applier():
    assert VIEWS_SQL == Path(__file__).resolve().parents[1] / "sql" / "views.sql"
