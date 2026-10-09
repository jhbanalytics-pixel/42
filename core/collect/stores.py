"""Where the SocialCrawl client writes and reads: credit_ledger and raw_responses.

Both tables are append-only. The BigQuery stores only append rows and read with parameterised
queries; the memory stores do the same in a list and are what the unit tests use.
"""

import json
import math
from datetime import date, datetime

DATASET = "intelligence_42_core"


def _day(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _instant(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class MemoryLedgerStore:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(dict(row))

    def spent(self, start, end, job=None):
        """Credits charged from start to end (trend_date, both inclusive), for one job (the share) or all.

        Refuses a non-finite charge rather than sum it: one NaN would turn every cap check off.
        """
        total = 0
        for r in self.rows:
            if start <= _day(r["trend_date"]) <= end and (job is None or r.get("job") == job):
                charged = float(r.get("credits_charged") or 0)
                if not math.isfinite(charged):
                    raise ValueError(f"credit_ledger holds a non-finite credits_charged: {charged}")
                total += charged
        return total

    def first_day(self):
        """The earliest trend_date in the ledger, or None when it is empty."""
        days = [_day(r["trend_date"]) for r in self.rows]
        return min(days) if days else None


class MemoryRawStore:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(json.loads(json.dumps(row, default=str)))

    def find(self, route, params_hash, since=None):
        """Body of the latest HTTP 200 response for route and params_hash, fetched at or after since."""
        hits = [
            r
            for r in self.rows
            if r["route"] == route
            and r["params_hash"] == params_hash
            and r.get("http_status") == 200
            and (since is None or _instant(r["fetched_at"]) >= since)
        ]
        if not hits:
            return None
        return max(hits, key=lambda r: _instant(r["fetched_at"]))["body"]


class _BigQueryStore:
    table = ""

    def __init__(self, client, project, dataset=DATASET):
        self.client = client
        self.table_id = f"{project}.{dataset}.{self.table}"

    def append(self, row):
        errors = self.client.insert_rows_json(self.table_id, [self._encode(row)])
        if errors:
            raise RuntimeError(f"append to {self.table_id} failed: {errors}")

    def _encode(self, row):
        return row

    def _query(self, sql, params):
        from google.cloud import bigquery

        config = bigquery.QueryJobConfig(query_parameters=params)
        return list(self.client.query(sql.format(table=self.table_id), job_config=config).result())


class BigQueryLedgerStore(_BigQueryStore):
    table = "credit_ledger"

    def spent(self, start, end, job=None):
        from google.cloud import bigquery

        rows = self._query(
            "SELECT COALESCE(SUM(IF(IS_NAN(c) OR IS_INF(c), 0, c)), 0) AS spent, "
            "COUNTIF(IS_NAN(c) OR IS_INF(c)) AS bad "
            "FROM (SELECT CAST(credits_charged AS FLOAT64) AS c FROM `{table}` "
            "WHERE trend_date BETWEEN @start AND @end AND (@job IS NULL OR job = @job))",
            [
                bigquery.ScalarQueryParameter("start", "DATE", start),
                bigquery.ScalarQueryParameter("end", "DATE", end),
                bigquery.ScalarQueryParameter("job", "STRING", job),
            ],
        )
        if rows[0]["bad"]:
            raise ValueError(f"credit_ledger holds {rows[0]['bad']} non-finite credits_charged values")
        return rows[0]["spent"]

    def first_day(self):
        """The earliest trend_date in the ledger, or None when it is empty."""
        rows = self._query("SELECT MIN(trend_date) AS first_day FROM `{table}`", [])
        return rows[0]["first_day"]


class BigQueryRawStore(_BigQueryStore):
    table = "raw_responses"

    def _encode(self, row):
        out = dict(row)
        out["body"] = None if row.get("body") is None else json.dumps(row["body"])
        return out

    def find(self, route, params_hash, since=None):
        from google.cloud import bigquery

        rows = self._query(
            "SELECT TO_JSON_STRING(body) AS body FROM `{table}` "
            "WHERE route = @route AND params_hash = @params_hash AND http_status = 200 "
            "AND (@since IS NULL OR fetched_at >= @since) "
            "ORDER BY fetched_at DESC LIMIT 1",
            [
                bigquery.ScalarQueryParameter("route", "STRING", route),
                bigquery.ScalarQueryParameter("params_hash", "STRING", params_hash),
                bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
            ],
        )
        if not rows or rows[0]["body"] is None:
            return None
        return json.loads(rows[0]["body"])
