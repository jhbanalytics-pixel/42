"""Create the agent's views from core/agent/sql/views.sql (DATA.md section 7). Every statement is
CREATE VIEW IF NOT EXISTS, so a second run changes nothing.

    py -3.13 -m core.agent.apply_views
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.agent.tools.sql_query import PROJECT

VIEWS_SQL = Path(__file__).resolve().parent / "sql" / "views.sql"


def statements(text: str) -> list[str]:
    """Split on semicolons after dropping whole-line comments."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def apply(execute: Callable[[str], object]) -> list[str]:
    done = []
    for sql in statements(VIEWS_SQL.read_text(encoding="utf-8")):
        execute(sql)
        done.append(sql)
    return done


def main() -> None:
    from google.cloud import bigquery

    client = bigquery.Client(project=PROJECT)
    for sql in apply(lambda sql: client.query(sql).result()):
        print("applied:", sql.split(" AS", 1)[0])


if __name__ == "__main__":
    main()
