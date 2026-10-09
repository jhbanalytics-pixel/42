"""Constructs the locality SQL must not use because they have not run on BigQuery (C4 v3 section 27, review F11).

The DuckDB harness accepts several forms BigQuery may refuse. Until the F42_BQ dry runs of test_locality_bigquery.py have
been run on staging, the statements stay within forms the repository already runs there: a STRING_AGG delimiter is
a quoted literal or the statement joins an array with ARRAY_TO_STRING, whose delimiter is any expression."""

from pathlib import Path

import pytest

from core.brief import job as brief_job

SQL = Path(__file__).resolve().parents[1] / "sql"
FILES = ("locality_members.sql", "locality_summary.sql", "locality_views.sql", "label_shared_members.sql")


def texts():
    out = {name: (SQL / name).read_text(encoding="utf-8") for name in FILES}
    out.update({name: brief_job.QUERIES[name] for name in ("not_local_audit", "unreadable_audit")})
    return out


@pytest.mark.parametrize("name", sorted(texts()))
def test_no_locality_statement_uses_string_agg(name):
    text = texts()[name]
    assert "STRING_AGG" not in text


def test_the_population_digest_joins_an_array_with_a_newline():
    text = texts()["locality_summary.sql"]
    assert "STRING_AGG" not in text
    assert "ARRAY_TO_STRING(ARRAY_AGG(" in text and "ORDER BY m.post_id), CHR(10))" in text
