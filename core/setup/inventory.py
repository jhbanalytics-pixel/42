"""Inventory the BigQuery data 42 inherits, read only (task 0.6).

    py -3.13 core/setup/inventory.py --plan    prints every query it would run; builds no client, no network
    py -3.13 core/setup/inventory.py           runs them and writes docs/full-42/reference/data-inventory.md

It uses the default BigQuery client, whose application default credentials impersonate f42-builder.
Every statement is one SELECT. Each is dry-run first and refused if the estimate is over 5 GB, then run
with maximum_bytes_billed at 5 GB. Column names come from INFORMATION_SCHEMA.COLUMNS, then each table is
counted in total and per market with its first and last date. Only counts, dates and market values are
read, never row content. Targets: trends_v2_dev.enriched_content and trend_scores,
trends_v2_staging.enriched_content, seed_graph in whichever of those two datasets holds it, and every
table in intelligence_42_sources_staging if that dataset exists.
"""
import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.api_core.exceptions import GoogleAPIError, NotFound
from google.cloud import bigquery

PROJECT = "ogilvy-trends-v2"
LOCATION = "US"
MAX_BYTES = 5 * 1024 ** 3
OUT = Path(__file__).resolve().parents[2] / "docs" / "full-42" / "reference" / "data-inventory.md"
DEV, STAGING, SOURCES = "trends_v2_dev", "trends_v2_staging", "intelligence_42_sources_staging"
SEED = "seed_graph"
# Dataset and the tables to inventory in it; None means every table in the dataset.
TARGETS = {DEV: ["enriched_content", "trend_scores", SEED], STAGING: ["enriched_content", SEED], SOURCES: None}
COUNTED = {"BASE TABLE"}
MARKET_NAMES = ("market", "country", "geo_market", "market_code", "country_code", "geo")
DATE_NAMES = ("date", "metric_date", "score_date", "published_at", "collected_at", "created_at")
DATE_TYPES = ("DATE", "TIMESTAMP", "DATETIME")
# SALVAGE.md drops this enriched_content column on read, so it is never picked or listed.
DROPPED = "".join(["gen", "z"])
IDENT = re.compile(r"^[A-Za-z0-9_]+$")
MAX_MARKETS = 100


class Refused(Exception):
    pass


def visible(columns):
    return [(n, t) for n, t in columns if DROPPED not in n.lower()]


def pick_market(columns):
    strings = {n.lower(): n for n, t in visible(columns) if t == "STRING"}
    for name in MARKET_NAMES:
        if name in strings:
            return strings[name]
    return next((n for low, n in strings.items() if low.endswith(("_market", "_country"))), None)


def pick_date(columns):
    typed = [(n, t) for n, t in visible(columns) if t in DATE_TYPES]
    by_name = {n.lower(): (n, t) for n, t in typed}
    for name in DATE_NAMES:
        if name in by_name:
            return by_name[name]
    return next((c for c in typed if c[1] == "DATE"), typed[0] if typed else None)


def ref(dataset, table):
    return f"{PROJECT}.{dataset}.{table}"


def tables_sql(dataset, filtered):
    where = " WHERE table_name IN UNNEST(@names)" if filtered else ""
    return (f"SELECT table_name, table_type FROM `{PROJECT}.{dataset}.INFORMATION_SCHEMA.TABLES`{where} "
            "ORDER BY table_name")


def columns_sql(dataset):
    return (f"SELECT table_name, column_name, data_type FROM `{PROJECT}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
            "WHERE table_name IN UNNEST(@names) ORDER BY table_name, ordinal_position")


def date_parts(date):
    return f", CAST(MIN(`{date}`) AS STRING) AS min_date, CAST(MAX(`{date}`) AS STRING) AS max_date"


def total_sql(dataset, table, date=None):
    return f"SELECT COUNT(*) AS total_rows{date_parts(date) if date else ''} FROM `{ref(dataset, table)}`"


def market_sql(dataset, table, market, date=None):
    return (f"SELECT CAST(`{market}` AS STRING) AS market, COUNT(*) AS row_count{date_parts(date) if date else ''} "
            f"FROM `{ref(dataset, table)}` GROUP BY `{market}` ORDER BY row_count DESC LIMIT {MAX_MARKETS + 1}")


def names_param(names):
    return [bigquery.ArrayQueryParameter("names", "STRING", list(names))]


def check_select(sql):
    if not sql.startswith("SELECT ") or ";" in sql:
        raise ValueError(f"refusing a statement that is not a single SELECT: {sql[:80]}")


def run_query(client, sql, params=(), log=None):
    """Dry-run, refuse over the cap, then run capped. Every call is appended to log."""
    check_select(sql)
    entry = {"sql": sql, "params": list(params), "estimate": None, "processed": None, "billed": None,
             "status": "ok"}
    log.append(entry)
    try:
        dry = client.query(sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=list(params)), location=LOCATION)
        entry["estimate"] = dry.total_bytes_processed or 0
        if entry["estimate"] > MAX_BYTES:
            entry["status"] = "refused"
            raise Refused(f"dry run estimate {entry['estimate']:,} bytes is over the 5 GB cap")
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=MAX_BYTES, query_parameters=list(params)), location=LOCATION)
        rows = list(job.result())
    except NotFound:
        entry["status"] = "not found"
        raise
    except GoogleAPIError as error:
        entry["status"] = f"failed: {type(error).__name__}"
        raise
    entry["processed"] = job.total_bytes_processed
    entry["billed"] = job.total_bytes_billed
    return rows


def inventory_table(client, dataset, table, columns, log):
    shown = visible(columns)
    market, date = pick_market(shown), pick_date(shown)
    section = {"dataset": dataset, "table": table, "market": market, "date": date,
               "columns": [n for n, _ in shown], "total": None, "range": None, "markets": None, "problems": []}
    date_name = date[0] if date else None
    try:
        row = run_query(client, total_sql(dataset, table, None if market else date_name), log=log)[0]
        section["total"] = row["total_rows"]
        if date_name and not market:
            section["range"] = (row["min_date"], row["max_date"])
    except (Refused, GoogleAPIError) as error:
        section["problems"].append(f"Total rows query not run: {first_line(error)}")
    if market:
        try:
            section["markets"] = run_query(client, market_sql(dataset, table, market, date_name), log=log)
        except (Refused, GoogleAPIError) as error:
            section["problems"].append(f"Per market query not run: {first_line(error)}")
    return section


def first_line(error):
    text = str(error).strip().splitlines()
    return f"{type(error).__name__}: {text[0][:200] if text else ''}"


def inventory(client):
    log, sections, notes, seed_homes = [], [], [], []
    for dataset, wanted in TARGETS.items():
        try:
            found = {r["table_name"]: r["table_type"] for r in run_query(
                client, tables_sql(dataset, bool(wanted)), names_param(wanted) if wanted else (), log)}
        except NotFound:
            notes.append(f"Dataset {dataset} does not exist, or f42-builder cannot see it.")
            continue
        except (Refused, GoogleAPIError) as error:
            notes.append(f"Dataset {dataset}: table list not read, {first_line(error)}")
            continue
        if SEED in found and wanted:
            seed_homes.append(dataset)
        notes += [f"{dataset}.{name} was not found." for name in wanted or [] if name not in found and name != SEED]
        notes += [f"{dataset}.{n} is a {t.lower()}, not counted." for n, t in sorted(found.items()) if t not in COUNTED]
        notes += [f"{dataset}.{n} skipped: its name needs quoting this script does not do." for n, t in
                  sorted(found.items()) if t in COUNTED and not IDENT.match(n)]
        counted = [n for n, t in sorted(found.items()) if t in COUNTED and IDENT.match(n)]
        if not counted:
            continue
        columns = {}
        try:
            for r in run_query(client, columns_sql(dataset), names_param(counted), log):
                columns.setdefault(r["table_name"], []).append((r["column_name"], r["data_type"]))
        except (Refused, GoogleAPIError) as error:
            notes.append(f"Dataset {dataset}: columns not read, {first_line(error)}")
            continue
        sections += [inventory_table(client, dataset, name, columns.get(name, []), log) for name in counted]
    if not seed_homes:
        notes.append(f"{SEED} was not found in {DEV} or {STAGING}.")
    return sections, notes, log


def render_section(s):
    lines = [f"## {s['dataset']}.{s['table']}", ""]
    total = "not read" if s["total"] is None else f"{s['total']:,}"
    market = f"`{s['market']}`" if s["market"] else "none fits"
    date = f"`{s['date'][0]}` ({s['date'][1]})" if s["date"] else "none fits"
    lines.append(f"Total rows: {total}. Market column: {market}. Date column: {date}.")
    if s["range"]:
        lines.append(f"Dates run from {s['range'][0]} to {s['range'][1]}.")
    if not s["market"] and not s["date"]:
        lines.append(f"Columns: {', '.join(s['columns']) or 'none listed'}.")
    lines += s["problems"]
    rows = s["markets"]
    if rows is not None:
        if len(rows) > MAX_MARKETS:
            lines.append(f"More than {MAX_MARKETS} market values; the {MAX_MARKETS} largest are shown.")
            rows = rows[:MAX_MARKETS]
        rows = sorted(rows, key=lambda r: "" if r["market"] is None else r["market"])
        dated = bool(s["date"])
        lines += ["", "| Market | Rows | Min date | Max date |" if dated else "| Market | Rows |",
                  "|" + ("-" * 3 + "|") * (4 if dated else 2)]
        for r in rows:
            cells = ["(null)" if r["market"] is None else r["market"], f"{r['row_count']:,}"]
            if dated:
                cells += [r["min_date"] or "(null)", r["max_date"] or "(null)"]
            lines.append("| " + " | ".join(cells) + " |")
    return lines + [""]


def bytes_text(value):
    return "not run" if value is None else f"{value:,}"


def render(sections, notes, log, when):
    processed = sum(e["processed"] or 0 for e in log)
    lines = ["# Existing BigQuery data inventory", "",
             f"Run at {when} by core/setup/inventory.py as f42-builder (application default credentials), "
             f"project {PROJECT}, location {LOCATION}. Read only: every query is one SELECT, dry-run first, refused "
             "above 5 GB and run with maximum_bytes_billed at 5 GB. Only counts, dates and market values are read, "
             "never row content. Columns SALVAGE.md drops on read are never picked or listed.", "",
             f"{len(log)} queries, {processed:,} bytes processed in total.", ""]
    for s in sections:
        lines += render_section(s)
    if notes:
        lines += ["## Not found, not counted or not read", ""] + [f"- {n}" for n in notes] + [""]
    lines += ["## Queries", ""]
    for i, e in enumerate(log, 1):
        params = "; ".join(f"@{p.name} = {', '.join(p.values)}" for p in e["params"])
        ran = ("not run" if e["processed"] is None else
               f"{bytes_text(e['processed'])} bytes processed, {bytes_text(e['billed'])} bytes billed")
        lines.append(f"{i}. {e['status']}: estimate {bytes_text(e['estimate'])} bytes, {ran}"
                     + (f", {params}" if params else ""))
        lines += ["", "```sql", e["sql"], "```", ""]
    return "\n".join(lines)


def plan():
    lines = [f"Planned queries, project {PROJECT}, location {LOCATION}. Each is a dry run first, refused if the "
             "estimate is over 5 GB, then run with maximum_bytes_billed at 5 GB.", ""]
    for dataset, wanted in TARGETS.items():
        lines.append(f"{dataset}:")
        lines.append(f"  {tables_sql(dataset, bool(wanted))}" + (f"  with @names = {', '.join(wanted)}" if wanted else ""))
        lines.append(f"  {columns_sql(dataset)}  with @names = the base tables found")
        for table in wanted or ["<table>"]:
            lines.append(f"  {total_sql(dataset, table)}")
            lines.append(f"  {market_sql(dataset, table, '<market_column>', '<date_column>')}")
            lines.append(f"  if no market column fits: {total_sql(dataset, table, '<date_column>')}")
        lines.append("")
    lines.append("Market and date columns are picked from INFORMATION_SCHEMA.COLUMNS at run time; a missing date "
                 "column drops MIN and MAX, a missing market column drops the per market query.")
    return "\n".join(lines)


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description="Read only inventory of the existing BigQuery data.")
    parser.add_argument("--plan", action="store_true", help="print the planned queries; no client, no network")
    parser.add_argument("--out", type=Path, default=OUT, help="markdown file to write")
    args = parser.parse_args(argv)
    if args.plan:
        print(plan())
        return 0
    client = client or bigquery.Client(project=PROJECT, location=LOCATION)
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sections, notes, log = inventory(client)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(sections, notes, log, when), encoding="utf-8")
    print(f"wrote {args.out}: {len(sections)} tables, {len(log)} queries, "
          f"{sum(e['processed'] or 0 for e in log):,} bytes processed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
