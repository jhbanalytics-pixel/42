"""The empty-stages check on fixtures: no BigQuery, a fake client that hands back fixed rows."""

import datetime as dt
import re

from ops.runners import check_empty_stages as ces


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return self.rows


class Row(dict):
    __getattr__ = dict.get


class FakeClient:
    def __init__(self, runs, tables):
        self.answers, self.sql, self.configs = [runs, tables], [], []

    def query(self, sql, job_config=None):
        self.sql.append(sql)
        self.configs.append(job_config)
        return Rows(self.answers.pop(0))


RUNS = [
    Row(run_date=dt.date(2026, 10, 3), stage="understand", status="ok",
        started_at=dt.datetime(2026, 10, 3, 1, 5, tzinfo=dt.timezone.utc), error=None,
        detail='{"za":{"skipped":"too_few_posts","posts":12,"today_posts":0}}'),
    Row(run_date=dt.date(2026, 10, 3), stage="coaction", status=None, started_at=None, error="ImportError: x",
        detail=None),
]
TABLES = [Row(t="clusters", n=0, last_day=None), Row(t="coord_signals", n=0, last_day=None),
          Row(t="breakout_signals", n=3, last_day=dt.date(2026, 10, 2))]


def test_prints_each_run_with_missing_fields_and_the_table_counts(capsys):
    ces.main(FakeClient(RUNS, TABLES))
    out = capsys.readouterr().out
    assert "2026-10-03 understand ok       01:05Z  err=- | " in out
    assert '"skipped":"too_few_posts"' in out
    assert "2026-10-03 coaction   -        --:--  err=ImportError: x | -" in out
    assert "runs rows: 2" in out
    assert "breakout_signals  rows=3 last=2026-10-02" in out
    assert "clusters          rows=0 last=None" in out


def test_reads_only_and_caps_every_query():
    client = FakeClient(RUNS, TABLES)
    ces.main(client)
    assert len(client.sql) == 2
    for sql in client.sql:
        assert re.match(r"\s*SELECT\b", sql)
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE)\b", sql, re.I)
    assert all(c.maximum_bytes_billed == 10**9 for c in client.configs)
