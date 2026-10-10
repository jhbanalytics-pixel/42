"""Review N4 (C4 v3 section 7.2): the item_state INSERT names its columns, so a table widened by later columns (the
three locality columns of the switch release) does not turn the positional form into a count mismatch, which
BigQuery refuses. DuckDB tolerates a short positional list, so the test counts the values and compares the names with
the table the harness builds from the same DDL."""

from pathlib import Path

from core.detect import sqlrun
from core.detect.tests import duck
from core.detect.tests.duck import strip_leading_comments as _strip

SQL = Path(__file__).resolve().parents[1] / "sql"


def insert_shape(text):
    """(number of values the final SELECT supplies, the column names the INSERT names or None)."""
    insert = [_strip(s) for s in sqlrun.split(sqlrun.render(text, "core", "agent"))][1]
    node = duck._tree(insert)
    select = node.expression
    named = node.this.expressions if getattr(node.this, "expressions", None) else None
    return len(select.expressions), ([c.name for c in named] if named else None)


def test_the_insert_names_every_column_of_the_table_and_supplies_one_value_for_each():
    con = duck.connect()
    table = [r[0] for r in con.execute("DESCRIBE core.item_state").fetchall()]
    values, named = insert_shape((SQL / "state.sql").read_text(encoding="utf-8"))
    assert values == len(table)
    assert named == table
