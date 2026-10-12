"""The trend test's shadow table (contract TREND-TEST-v1): one additive table in its own file, which the default apply does
not run. The column list is a literal here, so the table and its writer are each held to this file, not to each other."""
import re
from pathlib import Path

from core.schema import apply

SCHEMA_DIR = Path(__file__).resolve().parents[1]
FILE = SCHEMA_DIR / "trend_test.sql"
NAME = "ogilvy-trends-v2.intelligence_42_agent.trend_test_private"
COLUMNS = [
    ("run_date", "DATE", True), ("market", "STRING", True), ("item_id", "STRING", True),
    ("brief_run_id", "STRING", True), ("rule_version", "STRING", True), ("schema_version", "INT64", True),
    ("computed_at", "TIMESTAMP", True), ("population_cutoff", "TIMESTAMP", True), ("window_start", "DATE", True),
    ("window_end", "DATE", True), ("placement", "STRING", True), ("held_rule", "STRING", False),
    ("label", "STRING", True), ("reason", "STRING", True), ("news_axis", "BOOL", False), ("trend_axis", "BOOL", False),
    ("registry_status", "STRING", False), ("registry_keys", "INT64", False), ("outlet_hosts", "INT64", False),
    ("posts", "INT64", False), ("keyless_posts", "INT64", False), ("accounts", "INT64", False),
    ("located_posts", "INT64", False), ("feed_posts", "INT64", False), ("registry_posts", "INT64", False),
    ("outlet_posts", "INT64", False), ("outlet_accounts", "INT64", False), ("outlet_share", "FLOAT64", False),
    ("share_posts", "INT64", False), ("rt_posts", "INT64", False), ("outlet_link_posts", "INT64", False),
    ("link_posts", "INT64", False), ("own_accounts", "INT64", False), ("own_share", "FLOAT64", False),
    ("active_days", "INT64", False), ("growth_days", "INT64", False), ("peak_day_share", "FLOAT64", False),
    ("first_post_date", "DATE", False), ("last_post_date", "DATE", False), ("top_sound_accounts", "INT64", False),
    ("challenge_accounts", "INT64", False), ("caption_accounts", "INT64", False), ("format_signal", "BOOL", False),
    ("cause_dated_event", "BOOL", False), ("cause_cue", "STRING", False), ("news_driven", "BOOL", False),
    ("verified_accounts", "INT64", False), ("large_accounts", "INT64", False)]


def statements():
    return apply.load_statements([FILE])


def columns(statement):
    body = statement[statement.index("(") + 1:statement.rindex(")")]
    out = []
    for piece in body.split(","):
        words = piece.split()
        out.append((words[0], words[1], " ".join(words[2:]) == "NOT NULL"))
    return out


def test_the_file_holds_one_additive_create_of_the_private_table():
    [statement] = statements()
    assert apply.kind(statement) == "table" and apply.statement_name(statement) == NAME
    assert statement.startswith(f"CREATE TABLE IF NOT EXISTS `{NAME}` (")
    for word in (r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\b", r"\bREPLACE\b", r"\bALTER\b", r"expir", r"\bOPTIONS\b"):
        assert not re.search(word, statement, re.I), word
    assert statement.endswith("PARTITION BY run_date CLUSTER BY market, item_id")


def test_the_columns_are_the_v1_list_in_order():
    [statement] = statements()
    assert columns(statement[:statement.rindex("PARTITION")]) == COLUMNS


def test_the_default_apply_does_not_run_the_file():
    assert FILE not in apply.SQL_FILES
    assert all(NAME not in s for s in apply.load_statements())


def test_the_name_ends_in_the_suffix_the_ask_fence_always_refuses():
    assert NAME.endswith("_private")
