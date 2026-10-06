"""Read duplicate observation clusters and one distinct source pair from the staging corpus.

A read only BigQuery tool for the owner. It needs bigquery.jobs.create and table read on
the staging and dev datasets, nothing else. It reads the column names of the enriched and
raw snapshot tables and of their producer tables first and adapts to them: native_id is
used only where the snapshot carries it.

It then finds the two largest duplicate clusters of each kind in the snapshot
enriched_content lane: rows sharing market, platform and id; distinct ids sharing market,
platform and native_id when that column exists; and distinct ids sharing market, platform
and url, which is how a post the producer collected twice appears, because the producer
gives every row a fresh id. It also picks two rows in one market with different source
values, ids, url digests and text digests as the distinct source pair. Source names the
collector, not an origin, and origin is not projected, so that pair's origin independence
is recorded as unknown.

For every chosen id it traces the snapshot enriched row, the matching raw snapshot row and
the producer rows in the dev dataset, each as pipeline_run_id, collected_at, published_at,
platform, market, source and SHA256 digests of the canonical JSON row, of the url and of
the text. The text, title, author and url themselves never reach the receipt, so the
receipt holds no content and can be pasted anywhere. The chosen rows then go through the
identity kernel and each cluster states whether admission would collapse it, refuse it or
admit it as separate observations.

Each traced row also carries its obs1 observation id (src/ingestion/observation_id.py),
derived at read time from the columns the row holds, and the key it was derived from.
Each cluster counts its distinct row ids beside its distinct obs1 ids over the snapshot
enriched rows, so a post collected twice shows two row ids and one obs1. obs1 is a
truncated digest, so it adds no content to the receipt.

Every query is dry run first and refused when the dry run exceeds the byte cap; the real
run carries the same cap as maximum_bytes_billed. The receipt is written once and an
existing path is refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence import pipeline
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_context_identity import (
    evidence_groups,
    origin_authority_projection,
)
from src.ingestion.observation_id import OBSERVATION_ID_SCHEME, content_key, observation_id

PROJECT = "ogilvy-trends-v2"
STAGING_DATASET = "trends_v2_staging"
DEV_DATASET = "trends_v2_dev"
RECEIPT_VERSION = "observation_cluster_read_v2"
DEFAULT_CUTOFF = date(2026, 9, 7)
DEFAULT_WINDOW_DAYS = 7
DEFAULT_MAXIMUM_BYTES_BILLED = 1024**3
CLUSTER_LIMIT = 2
MEMBER_LIMIT = 10
PAIR_CANDIDATE_LIMIT = 200
EVIDENCE_LANES = ("enriched_content", "raw_content")
REQUIRED_COLUMNS = (
    "id",
    "market",
    "platform",
    "source",
    "url",
    "text",
    "published_at",
    "collected_at",
    "pipeline_run_id",
)
TRACE_FIELDS = (
    "table",
    "id",
    "pipeline_run_id",
    "collected_at",
    "published_at",
    "platform",
    "market",
    "source",
    "row_sha256",
    "shared_sha256",
    "url_sha256",
    "text_sha256",
    "observation_id",
    "observation_key_kind",
)


class ReadRefused(Exception):
    pass


def snapshot_table(cutoff, lane):
    return f"{PROJECT}.{STAGING_DATASET}.open_intelligence_v3_source_{cutoff:%Y%m%d}_{lane}"


def producer_table(lane):
    return f"{PROJECT}.{DEV_DATASET}.{lane}"


def _sha256(value):
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _json_safe(value):
    """A value canonical JSON can carry: a non finite float is spelled out, not refused."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _instant(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


class Reader:
    """Runs each statement as a dry run, refuses it above the cap, then runs it capped."""

    def __init__(self, client, *, maximum_bytes_billed):
        if type(maximum_bytes_billed) is not int or maximum_bytes_billed <= 0:
            raise ReadRefused("maximum_bytes_billed must be a positive integer")
        self.client = client
        self.maximum_bytes_billed = maximum_bytes_billed
        self.queries = []

    def _config(self, parameters, *, dry_run):
        from google.cloud import bigquery

        return bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_query_cache=False,
            maximum_bytes_billed=self.maximum_bytes_billed,
            query_parameters=parameters,
        )

    def run(self, name, sql, parameters=()):
        parameters = list(parameters)
        dry = self.client.query(sql, job_config=self._config(parameters, dry_run=True))
        planned = dry.total_bytes_processed
        if type(planned) is not int or planned < 0:
            raise ReadRefused(f"{name}: dry run reported no byte estimate")
        entry = {
            "name": name,
            "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "dry_run_bytes": planned,
        }
        if planned > self.maximum_bytes_billed:
            entry["state"] = "refused_over_cap"
            self.queries.append(entry)
            raise ReadRefused(
                f"{name}: dry run would process {planned} bytes, above the cap of "
                f"{self.maximum_bytes_billed}"
            )
        job = self.client.query(sql, job_config=self._config(parameters, dry_run=False))
        rows = [dict(row.items()) for row in job.result()]
        entry.update(
            state="succeeded",
            job_id=job.job_id,
            bytes_billed=job.total_bytes_billed,
            row_count=len(rows),
        )
        self.queries.append(entry)
        return rows


def _parameters(**values):
    from google.cloud import bigquery

    output = []
    for name, value in values.items():
        if isinstance(value, list):
            output.append(bigquery.ArrayQueryParameter(name, "STRING", value))
        elif isinstance(value, datetime):
            output.append(bigquery.ScalarQueryParameter(name, "TIMESTAMP", value))
        else:
            output.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    return output


def read_columns(reader, dataset, tables):
    """Column names of each named table, read from the dataset's INFORMATION_SCHEMA."""
    sql = (
        f"SELECT table_name, column_name FROM `{PROJECT}.{dataset}.INFORMATION_SCHEMA.COLUMNS`"
        " WHERE table_name IN UNNEST(@tables) ORDER BY table_name, ordinal_position"
    )
    rows = reader.run(f"columns_{dataset}", sql, _parameters(tables=list(tables)))
    columns = {table: [] for table in tables}
    for row in rows:
        if row["table_name"] in columns:
            columns[row["table_name"]].append(row["column_name"])
    for table, names in columns.items():
        missing = [name for name in REQUIRED_COLUMNS if name not in names]
        if missing:
            raise ReadRefused(f"{dataset}.{table}: missing columns {', '.join(missing)}")
    return columns


def selected_columns(lane, present):
    """The admission's evidence columns for one lane, kept only where the table has them."""
    return [name for name in pipeline.EVIDENCE_COLUMNS_BY_TABLE[lane] if name in present]


_WINDOW = "collected_at >= @window_start AND collected_at < @window_end"


# Each cluster kind and the column its members share.
CLUSTER_COLUMNS = {"same_id": "id", "same_native_id": "native_id", "same_url": "url"}


def _cluster_sql(table, kind):
    column = CLUSTER_COLUMNS[kind]
    if column == "id":
        return (
            "SELECT market, platform, id, COUNT(*) AS row_count, 1 AS id_count,"
            " COUNT(DISTINCT DATE(collected_at)) AS day_count, [id] AS ids"
            f" FROM `{table}` WHERE {_WINDOW}"
            " GROUP BY market, platform, id HAVING COUNT(*) > 1"
            f" ORDER BY row_count DESC, market, platform, id LIMIT {CLUSTER_LIMIT}"
        )
    return (
        f"SELECT market, platform, TO_HEX(SHA256({column})) AS key_sha256,"
        " COUNT(*) AS row_count, COUNT(DISTINCT id) AS id_count,"
        " COUNT(DISTINCT DATE(collected_at)) AS day_count,"
        f" ARRAY_AGG(DISTINCT id ORDER BY id LIMIT {MEMBER_LIMIT}) AS ids"
        f" FROM `{table}` WHERE {_WINDOW} AND {column} IS NOT NULL AND TRIM({column}) != ''"
        f" GROUP BY market, platform, {column} HAVING COUNT(DISTINCT id) > 1"
        " ORDER BY id_count DESC, row_count DESC, market, platform, key_sha256"
        f" LIMIT {CLUSTER_LIMIT}"
    )


def find_clusters(reader, table, kinds, window):
    clusters = {}
    for kind in kinds:
        rows = reader.run(f"clusters_{kind}", _cluster_sql(table, kind), _parameters(**window))
        clusters[kind] = [
            {
                "kind": kind,
                "market": row["market"],
                "platform": row["platform"],
                **({"id": row["id"]} if kind == "same_id" else {"key_sha256": row["key_sha256"]}),
                "row_count": row["row_count"],
                "id_count": row["id_count"],
                "day_count": row["day_count"],
                "members": sorted(row["ids"]),
            }
            for row in rows
        ]
    return clusters


ORIGIN_INDEPENDENCE_NOTE = (
    "source names the collector, and a collector is not an origin. Origin is not projected, "
    "so the independence of these two observations stays unknown; distinct ids, url digests "
    "and text digests show only that they are different rows about different content."
)


def find_distinct_source_pair(reader, table, window):
    """Two rows in one market with different source values, ids, url and text digests."""
    sql = (
        "SELECT market, source, pick.id AS id, pick.url_sha256 AS url_sha256,"
        " pick.text_sha256 AS text_sha256 FROM (SELECT market, source,"
        " ARRAY_AGG(STRUCT(id, TO_HEX(SHA256(url)) AS url_sha256,"
        " TO_HEX(SHA256(text)) AS text_sha256) ORDER BY id LIMIT 1)[OFFSET(0)] AS pick"
        f" FROM `{table}` WHERE {_WINDOW} AND source IS NOT NULL AND TRIM(source) != ''"
        " AND url IS NOT NULL AND TRIM(url) != '' AND text IS NOT NULL AND TRIM(text) != ''"
        " GROUP BY market, source)"
        f" ORDER BY market, source LIMIT {PAIR_CANDIDATE_LIMIT}"
    )
    rows = reader.run("distinct_source_candidates", sql, _parameters(**window))
    return choose_distinct_source_pair(rows)


def choose_distinct_source_pair(rows):
    """The first two rows of one market whose source, id, url and text all differ."""
    by_market = {}
    for row in rows:
        by_market.setdefault(row["market"], []).append(row)
    for market in sorted(by_market):
        chosen = []
        for row in sorted(by_market[market], key=lambda item: (item["source"], item["id"])):
            if all(
                row[field] != other[field]
                for other in chosen
                for field in ("source", "id", "url_sha256", "text_sha256")
            ):
                chosen.append(row)
            if len(chosen) == 2:
                return {
                    "market": market,
                    "members": [item["id"] for item in chosen],
                    "origin_independence": "unknown",
                    "origin_independence_note": ORIGIN_INDEPENDENCE_NOTE,
                }
    return None


def read_trace_rows(reader, name, table, columns, shared, ids, window):
    listed = ", ".join(f"`{column}`" for column in columns)
    sql = (
        f"SELECT {listed} FROM `{table}` WHERE {_WINDOW} AND id IN UNNEST(@ids)"
        " ORDER BY id, collected_at, pipeline_run_id"
    )
    rows = reader.run(name, sql, _parameters(ids=sorted(ids), **window))
    return [trace_entry(table, row, shared) for row in rows]


def trace_entry(table, row, shared):
    """One row reduced to its trace fields and digests; no content leaves this function.

    row_sha256 covers every column this table has. shared_sha256 covers only the columns
    the snapshot and producer tables of this lane share, so a producer table that gained
    a column after the clone still matches its snapshot row when the data is the same.
    """
    return {
        "table": table,
        "id": row["id"],
        "pipeline_run_id": row.get("pipeline_run_id"),
        "collected_at": _instant(row.get("collected_at")),
        "published_at": _instant(row.get("published_at")),
        "platform": row.get("platform"),
        "market": row.get("market"),
        "source": row.get("source"),
        "row_sha256": canonical_digest(_json_safe(row)),
        "shared_sha256": canonical_digest(
            _json_safe({column: row.get(column) for column in shared})
        ),
        "url_sha256": _sha256(row.get("url")),
        "text_sha256": _sha256(row.get("text")),
        "observation_id": observation_id(row),
        "observation_key_kind": (content_key(row) or [None])[0],
    }


def identity_result(entries):
    """Feed the snapshot enriched rows of one cluster through the identity kernel."""
    records = [
        {
            "market": entry["market"],
            "platform": entry["platform"],
            "source_row_id": entry["id"],
            "content_digest": entry["row_sha256"],
        }
        for entry in entries
    ]
    projection = origin_authority_projection(records)
    try:
        groups = evidence_groups(records)
    except ValueError as error:
        return {"state": "refused", "reason": str(error), "origin_authority_projection": projection}
    return {
        "state": "grouped",
        "record_count": len(records),
        "observation_count": len(groups["observation_keys"]),
        "independent_origin_count": groups["independent_origin_count"],
        "unknown_origin_count": groups["unknown_origin_count"],
        "origin_authority_projection": projection,
    }


def observation_id_counts(entries):
    """Distinct row ids beside distinct obs1 ids, and the key each obs1 was derived from."""
    derived = [entry for entry in entries if entry["observation_id"] is not None]
    kinds = {}
    for entry in derived:
        # Keyed by_url, not url, so no receipt key names a content column.
        kind = "by_" + entry["observation_key_kind"]
        kinds[kind] = kinds.get(kind, 0) + 1
    return {
        "scheme": OBSERVATION_ID_SCHEME,
        "distinct_row_ids": len({entry["id"] for entry in entries}),
        "distinct_observation_ids": len({entry["observation_id"] for entry in derived}),
        "underived_rows": len(entries) - len(derived),
        "key_kinds": dict(sorted(kinds.items())),
    }


def admission_verdict(entries):
    """What admission does when every row of the cluster reaches one evidence read.

    The evidence query counts rows per market and id, and admission refuses the whole
    read when that count exceeds one, before the identity kernel runs. Distinct ids are
    distinct observations: they are admitted apart, each of unknown origin, and nothing
    collapses them while origin and native identity are not projected.
    """
    per_id = {}
    for entry in entries:
        per_id[entry["id"]] = per_id.get(entry["id"], 0) + 1
    if any(count > 1 for count in per_id.values()):
        return "refuses_whole_admission"
    if len(per_id) > 1:
        return "admits_separate_observations"
    return "admits_one_observation"


def _describe(members, traces, snapshot, producer, *, id_count=None):
    """The trace of one cluster's ids, their identity result and the admission verdict.

    producer_matches_snapshot says, per lane, whether the producer table still holds
    exactly the rows the snapshot cloned for these ids, compared over the columns both
    tables share. The verdict covers the traced members only; a cluster with more ids
    than the member limit says so.
    """
    ids = set(members)
    rows = [entry for entry in traces if entry["id"] in ids]
    digests, shared = {}, {}
    for entry in rows:
        digests.setdefault(entry["table"], set()).add(entry["row_sha256"])
        shared.setdefault(entry["table"], set()).add(entry["shared_sha256"])
    enriched = [entry for entry in rows if entry["table"] == snapshot["enriched_content"]]
    total = len(ids) if id_count is None else id_count
    return {
        "trace": rows,
        "identity": identity_result(enriched),
        "admission": admission_verdict(enriched),
        "admission_scope": "all_members" if len(ids) >= total else "sampled_members",
        "member_count": total,
        "traced_member_count": len(ids),
        "distinct_row_digests_by_table": {
            table: len(values) for table, values in sorted(digests.items())
        },
        "producer_matches_snapshot": {
            lane: shared.get(snapshot[lane], set()) == shared.get(producer[lane], set())
            for lane in EVIDENCE_LANES
        },
        "observation_ids": observation_id_counts(enriched),
    }


def read_clusters(client, *, cutoff, window_days, maximum_bytes_billed):
    reader = Reader(client, maximum_bytes_billed=maximum_bytes_billed)
    start = datetime.combine(cutoff - timedelta(days=window_days - 1), time.min, UTC)
    end = datetime.combine(cutoff + timedelta(days=1), time.min, UTC)
    window = {"window_start": start, "window_end": end}
    snapshot = {lane: snapshot_table(cutoff, lane) for lane in EVIDENCE_LANES}
    producer = {lane: producer_table(lane) for lane in EVIDENCE_LANES}
    staging_columns = read_columns(
        reader, STAGING_DATASET, [name.rsplit(".", 1)[1] for name in snapshot.values()]
    )
    dev_columns = read_columns(reader, DEV_DATASET, list(EVIDENCE_LANES))
    present = {
        snapshot[lane]: staging_columns[snapshot[lane].rsplit(".", 1)[1]] for lane in EVIDENCE_LANES
    }
    present.update({producer[lane]: dev_columns[lane] for lane in EVIDENCE_LANES})
    native = "native_id" in present[snapshot["enriched_content"]]
    kinds = ("same_id", "same_native_id", "same_url") if native else ("same_id", "same_url")
    clusters = find_clusters(reader, snapshot["enriched_content"], kinds, window)
    pair = find_distinct_source_pair(reader, snapshot["enriched_content"], window)
    shared = {
        lane: [
            column
            for column in selected_columns(lane, present[snapshot[lane]])
            if column in present[producer[lane]]
        ]
        for lane in EVIDENCE_LANES
    }
    ids = {member for group in clusters.values() for item in group for member in item["members"]}
    if pair is not None:
        ids.update(pair["members"])
    traces = []
    if ids:
        for label, tables in (("snapshot", snapshot), ("producer", producer)):
            for lane in EVIDENCE_LANES:
                table = tables[lane]
                traces += read_trace_rows(
                    reader,
                    f"trace_{label}_{lane}",
                    table,
                    selected_columns(lane, present[table]),
                    shared[lane],
                    ids,
                    window,
                )
    for group in clusters.values():
        for item in group:
            item.update(
                _describe(item["members"], traces, snapshot, producer, id_count=item["id_count"])
            )
    if pair is not None:
        pair.update(_describe(pair["members"], traces, snapshot, producer))
    return {
        "contract_version": RECEIPT_VERSION,
        "snapshot_tables": snapshot,
        "producer_tables": producer,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "maximum_bytes_billed": maximum_bytes_billed,
        "columns": {
            table: {
                "has_native_id": "native_id" in names,
                "authority_columns": [
                    name for name in pipeline.WAVE1_AUTHORITY_COLUMNS if name in names
                ],
                "column_count": len(names),
            }
            for table, names in sorted(present.items())
        },
        "column_differences": {
            lane: {
                "only_in_snapshot": sorted(
                    set(present[snapshot[lane]]) - set(present[producer[lane]])
                ),
                "only_in_producer": sorted(
                    set(present[producer[lane]]) - set(present[snapshot[lane]])
                ),
                "compared_columns": len(shared[lane]),
            }
            for lane in EVIDENCE_LANES
        },
        "cluster_kinds": list(kinds),
        "clusters": clusters,
        "distinct_source_pair": pair,
        "queries": reader.queries,
        "dry_run_bytes_total": sum(entry["dry_run_bytes"] for entry in reader.queries),
        "bytes_billed_total": sum(entry.get("bytes_billed") or 0 for entry in reader.queries),
    }


def write_receipt(receipt, path):
    with Path(path).open("xb") as handle:
        handle.write(canonical_bytes(receipt))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF.isoformat())
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--maximum-bytes-billed", type=int, default=DEFAULT_MAXIMUM_BYTES_BILLED)
    args = parser.parse_args(argv)
    if Path(args.receipt).exists():
        raise SystemExit(f"receipt path already exists: {args.receipt}")
    if args.window_days < 1:
        raise SystemExit("window days must be at least one")
    from google.cloud import bigquery

    receipt = read_clusters(
        bigquery.Client(project=PROJECT),
        cutoff=date.fromisoformat(args.cutoff),
        window_days=args.window_days,
        maximum_bytes_billed=args.maximum_bytes_billed,
    )
    write_receipt(receipt, args.receipt)
    print(
        json.dumps(
            {
                "clusters": {kind: len(items) for kind, items in receipt["clusters"].items()},
                "distinct_source_pair": receipt["distinct_source_pair"] is not None,
                "dry_run_bytes_total": receipt["dry_run_bytes_total"],
                "bytes_billed_total": receipt["bytes_billed_total"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
