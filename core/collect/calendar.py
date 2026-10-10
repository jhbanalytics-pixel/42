"""Build the moments calendar for ZA, NG and KE and MERGE it into BigQuery (task 1.4).

Rows come from date.nager.at public holidays (the recorded fixtures, or a live fetch with --fetch)
and core/config/moments.yaml recurring and explicitly dated seeds. Each market's window runs from the load date in its
own timezone to 90 days later, or to known_until in moments.yaml if that is later.

Without --apply nothing touches the cloud: the rows are printed sorted by date and market with a
count per market. With --apply the MERGE is dry-run first, then run. The MERGE inserts new rows only
and leaves existing rows unchanged.

With --analogues (task 3.6) each row gets last year's analogue: the rule's date in the same month last
year for a rule moment, else last year's date.nager.at row of the same name, else the fixed day and month
for a holiday on the fixed_dates list, else no analogue with the reason. The window runs ANALOGUE_DAYS
either side. One read-only SELECT counts the hashtags in every window for that market in the social
rows of the legacy enriched_content history and in 42's posts; rule 1 labels are held back with a
count. With --fetch each market and year is fetched once per run; a failed fetch is printed as not
loaded and carried as the reason on the rows it affects. Each row then gets one record appended
to calendar_analogues, keyed like the calendar row, carrying the analogue, the window, the labels, the SQL and its parameters; a row with nothing found says why. Reruns
append again, so readers take the newest computed_at per row. Without --apply this prints the
matches, the SQL and its parameters with no network; with --apply every query is dry-run first
and refused above MAX_BYTES.

The weekly f42-drift job (core/collect/drift.py) runs the same --analogues --apply step on Monday after
its report, through weekly_analogues, so calendar rows loaded since the last run get their analogue.

Run from the repo root: py -3.13 -m core.collect.calendar [--start YYYY-MM-DD] [--fetch] [--analogues] [--apply]
"""
import argparse
import http.client
import json
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from google.cloud import bigquery

from core.collect.gdelt import MAX_BYTES, OverCap, _checked, blocked

HERE = Path(__file__).resolve().parent
CONFIG = HERE.parent / "config"
FIXTURES = HERE / "tests" / "fixtures"
NAGER_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{market}"
NAGER_SOURCE = "date.nager.at /api/v3/PublicHolidays/{year}/{market}"
NAGER_CACHE = {}
WINDOW_DAYS = 90
LOCATION = "US"
TABLE = "ogilvy-trends-v2.intelligence_42_core.calendar"
VALID_KINDS = {
    "commerce", "culture", "election", "fuel", "holiday", "payday", "results", "season", "sport"
}

MERGE_SQL = f"""MERGE `{TABLE}` t
USING (SELECT * FROM UNNEST(@rows)) s
ON t.moment_date = s.moment_date AND t.market = s.market AND t.name = s.name
WHEN NOT MATCHED THEN
  INSERT (moment_date, market, name, kind, source, item_ids)
  VALUES (s.moment_date, s.market, s.name, s.kind, s.source, ARRAY<STRING>[])
"""

ANALOGUE_DAYS = 7
TOP_READ = 50
TOP_KEEP = 10
ANALOGUE_TABLE = "ogilvy-trends-v2.intelligence_42_core.calendar_analogues"
LEGACY_TABLE = "ogilvy-trends-v2.trends_v2_dev.enriched_content"
POSTS_TABLE = "ogilvy-trends-v2.intelligence_42_core.posts"
LEGACY_SOURCE = "trends_v2_dev.enriched_content"
POSTS_SOURCE = "intelligence_42_core.posts"
WINDOW_FIELDS = (("moment_date", "DATE"), ("market", "STRING"), ("name", "STRING"), ("timezone", "STRING"),
                 ("window_start", "DATE"), ("window_end", "DATE"))
RECORD_FIELDS = (("moment_date", "DATE"), ("market", "STRING"), ("name", "STRING"), ("analogue_date", "DATE"),
                 ("match_kind", "STRING"), ("window_start", "DATE"), ("window_end", "DATE"), ("status", "STRING"),
                 ("reason", "STRING"), ("evidence", "STRING"), ("query_text", "STRING"), ("query_params", "STRING"))

# Legacy rows are read from social connectors only (rule 2). Each source is the literal a social
# connector in engine/src/ingestion/connectors/ writes (socialcrawl.py, ensemble.py "EnsembleData",
# reddit.py "Reddit", bluesky.py, youtube_scrape.py), lowercased; each platform is a social platform
# those connectors write. Search, news, web and chart connectors are left out, and a row must match both.
LEGACY_SOCIAL_SOURCES = ("socialcrawl", "ensembledata", "reddit", "bluesky", "youtube_scrape")
LEGACY_SOCIAL_PLATFORMS = ("tiktok", "instagram", "threads", "twitter", "youtube", "reddit", "facebook", "bluesky")
# A tag starts after a space or punctuation, not inside a word, URL path or HTML entity, and holds a
# letter; three, six or eight hex digits are a colour code, not a tag.
TAG_PATTERN = "(?:^|[^a-z0-9_/&#])#([a-z0-9_]*[a-z][a-z0-9_]*)"
HEX_COLOUR = "^([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$"
# SocialCrawl drops posts older than max_age_days (21, engine/configs/sources.yaml) when it collects,
# so a social post from a window is collected within 21 days of it; one more day covers timezones.
LEGACY_COLLECT_DAYS = 22


def sql_list(values):
    return ", ".join(f"'{v}'" for v in values)


# Hashtags per window from the legacy history (the hashtags column there is empty, so they are read
# from title and text) and from 42's own posts. The legacy table is partitioned on collected_at, which
# never comes before published_at, so the lower bound prunes partitions without losing a row; the
# upper bound stops the scan LEGACY_COLLECT_DAYS after the last window.
EVIDENCE_SQL = f"""WITH w AS (
  SELECT * FROM UNNEST(@windows)
),
found AS (
  SELECT w.moment_date, w.market, w.name, '{LEGACY_SOURCE}' AS source, CONCAT('#', tag) AS label,
    e.id AS post_key, e.published_at AS seen_at
  FROM `{LEGACY_TABLE}` e
  JOIN w ON e.market = LOWER(w.market)
    AND DATE(e.published_at, w.timezone) BETWEEN w.window_start AND w.window_end
  CROSS JOIN UNNEST(REGEXP_EXTRACT_ALL(LOWER(CONCAT(IFNULL(e.title, ''), ' ', IFNULL(e.text, ''))), r'{TAG_PATTERN}')) tag
  WHERE e.market IN UNNEST(@legacy_markets)
    AND LOWER(e.source) IN ({sql_list(LEGACY_SOCIAL_SOURCES)})
    AND LOWER(e.platform) IN ({sql_list(LEGACY_SOCIAL_PLATFORMS)})
    AND NOT REGEXP_CONTAINS(tag, r'{HEX_COLOUR}')
    AND DATE(e.collected_at) >= DATE_SUB(@min_start, INTERVAL 1 DAY)
    AND DATE(e.collected_at) <= DATE_ADD(@max_end, INTERVAL {LEGACY_COLLECT_DAYS} DAY)
    AND e.published_at >= TIMESTAMP(DATE_SUB(@min_start, INTERVAL 1 DAY))
    AND e.published_at < TIMESTAMP(DATE_ADD(@max_end, INTERVAL 2 DAY))
  UNION ALL
  SELECT w.moment_date, w.market, w.name, '{POSTS_SOURCE}', CONCAT('#', LTRIM(LOWER(tag), '#')),
    p.post_id, p.published_at
  FROM `{POSTS_TABLE}` p
  JOIN w ON p.geo_market = w.market AND p.post_date BETWEEN w.window_start AND w.window_end
  CROSS JOIN UNNEST(p.hashtags) tag
  WHERE p.geo_market IN UNNEST(@markets)
    AND p.post_date BETWEEN @min_start AND @max_end
)
SELECT moment_date, market, name, source, label, COUNT(DISTINCT post_key) AS posts, MIN(seen_at) AS first_seen
FROM found
GROUP BY moment_date, market, name, source, label
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY moment_date, market, name, source ORDER BY COUNT(DISTINCT post_key) DESC, label) <= @top_read
"""

INSERT_ANALOGUES_SQL = f"""INSERT INTO `{ANALOGUE_TABLE}` (moment_date, market, name, analogue_date, match_kind,
  window_start, window_end, status, reason, evidence, query_text, query_params, computed_at)
SELECT moment_date, market, name, analogue_date, match_kind, window_start, window_end, status, reason,
  PARSE_JSON(evidence, wide_number_mode => 'round'), query_text,
  PARSE_JSON(query_params, wide_number_mode => 'round'), CURRENT_TIMESTAMP()
FROM UNNEST(@rows)
"""


def load_yaml(name):
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8"))


def markets():
    return {m["country_code"]: m["timezone"]["value"] for m in load_yaml("markets.yaml")["markets"].values()}


def as_list(market):
    return market if isinstance(market, list) else [market]


def row(moment_date, market, name, kind, source):
    return {"moment_date": moment_date, "market": market, "name": name, "kind": kind,
            "source": source, "item_ids": []}


def parse_nager(records, market, year):
    source = NAGER_SOURCE.format(year=year, market=market)
    return [row(date.fromisoformat(h["date"]), market, h["name"], "holiday", source)
            for h in records if h.get("global", True)]


class NagerUnavailable(Exception):
    pass


def nager_records(market, year, fetch=False):
    """The year's records from the fixtures, or with fetch from date.nager.at at most once per market and
    year per run; a failed fetch is kept as its reason and raised as NagerUnavailable on every read."""
    if not fetch:
        return json.loads((FIXTURES / f"nager_{market}_{year}.json").read_text(encoding="utf-8"))
    key = (market, year)
    if key not in NAGER_CACHE:
        try:
            with urllib.request.urlopen(NAGER_URL.format(year=year, market=market), timeout=30) as response:
                NAGER_CACHE[key] = json.load(response)
        except (OSError, ValueError, http.client.HTTPException) as error:
            NAGER_CACHE[key] = f"date.nager.at {year} {market} could not be fetched ({error})"
    if isinstance(NAGER_CACHE[key], str):
        raise NagerUnavailable(NAGER_CACHE[key])
    return NAGER_CACHE[key]


def nager_failures():
    return sorted(v for v in NAGER_CACHE.values() if isinstance(v, str))


def months(start, end):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def nth_weekday(year, month, nth, weekday):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))


def safe_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def rule_dates(rule, start, end):
    kind = rule["rule"]
    if kind == "monthly_day":
        found = [safe_date(y, m, rule["day"]) for y, m in months(start, end)]
    elif kind == "monthly_nth_weekday":
        found = [nth_weekday(y, m, rule["nth"], rule["weekday"]) for y, m in months(start, end)]
    elif kind == "yearly_date":
        found = [safe_date(y, rule["month"], rule["day"]) for y in range(start.year, end.year + 1)]
    elif kind == "yearly_nth_weekday":
        found = [nth_weekday(y, rule["month"], rule["nth"], rule["weekday"]) + timedelta(days=rule.get("offset_days", 0))
                 for y in range(start.year, end.year + 1)]
    else:
        raise ValueError(f"unknown calendar rule: {kind}")
    return sorted(d for d in found if d and start <= d <= end)


def renames(moments):
    return {(r["market"], r["nager_name"]): r for r in moments["nager_renames"]}


def nager_rows(market, year, fetch=False, renames=None):
    """The year's date.nager.at rows for a market, with the hand list names and kinds applied."""
    rows = parse_nager(nager_records(market, year, fetch), market, year)
    for r in rows:
        hand = (renames or {}).get((market, r["name"]))
        if hand:
            r.update(name=hand["name"], kind=hand["kind"], source=f"{r['source']}; {hand['source']}")
    return rows


def build_rows(start=None, fetch=False, moments=None):
    moments = moments or load_yaml("moments.yaml")
    known_until = date.fromisoformat(str(moments["known_until"]))
    hand = renames(moments)
    market_timezones = markets()
    dated = moments.get("dated", [])
    if not isinstance(dated, list):
        raise ValueError("dated moments must be a list")
    dated_rows = []
    for entry in dated:
        if not isinstance(entry, dict):
            raise ValueError("dated moment entries must be mappings")
        active = entry.get("active")
        if not isinstance(active, bool):
            raise ValueError("dated moment active status must be boolean")
        if not active:
            continue
        date_text = entry.get("date")
        if not isinstance(date_text, str):
            raise ValueError("dated moment date must be ISO text")
        try:
            moment_date = date.fromisoformat(date_text)
        except ValueError as error:
            raise ValueError("dated moment date must be ISO text") from error
        if moment_date.isoformat() != date_text:
            raise ValueError("dated moment date must be ISO text")
        market = entry.get("market")
        if not isinstance(market, str) or market not in market_timezones:
            raise ValueError("dated moment market is unknown")
        name = entry.get("name")
        kind = entry.get("kind")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("dated moment name is required")
        if not isinstance(kind, str) or kind not in VALID_KINDS:
            raise ValueError("dated moment kind is unknown")
        if entry.get("source") != "research_confirmed":
            raise ValueError("dated moment source must be research_confirmed")
        source_file = entry.get("source_file")
        source_line = entry.get("source_line")
        if not isinstance(source_file, str) or not source_file.strip() or Path(source_file).name != source_file:
            raise ValueError("dated moment source file must be a file name")
        if not isinstance(source_line, int) or isinstance(source_line, bool) or source_line < 1:
            raise ValueError("dated moment source line must be positive")
        source_urls = entry.get("source_urls")
        if not isinstance(source_urls, list) or not source_urls:
            raise ValueError("dated moment source URLs are required")
        for url in source_urls:
            if not isinstance(url, str):
                raise ValueError("dated moment source URLs must be text")
            parsed_url = urlsplit(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                raise ValueError("dated moment source URL is invalid")
            if parsed_url.username or parsed_url.password:
                raise ValueError("dated moment source URL cannot contain credentials")
        supplied_date_text = entry.get("supplied_date_text")
        if not isinstance(supplied_date_text, str) or not supplied_date_text.strip():
            raise ValueError("dated moment supplied date text is required")
        source = "; ".join(("research_confirmed", f"{source_file}:{source_line}", " | ".join(source_urls)))
        dated_rows.append((moment_date, market, name, kind, source))

    rows = []
    for market, timezone in market_timezones.items():
        first = start or datetime.now(ZoneInfo(timezone)).date()
        last = max(first + timedelta(days=WINDOW_DAYS), known_until)
        for year in range(first.year, last.year + 1):
            try:
                rows += nager_rows(market, year, fetch, hand)
            except NagerUnavailable:
                continue  # the reason stays in NAGER_CACHE and main prints it as not loaded
        for rule in moments["rules"]:
            if market in as_list(rule["market"]):
                rows += [row(d, market, rule["name"], rule["kind"], rule["source"])
                         for d in rule_dates(rule, first, last)]
        rows += [row(moment_date, dated_market, name, kind, source)
                 for moment_date, dated_market, name, kind, source in dated_rows
                 if dated_market == market and first <= moment_date <= last]
        rows = [r for r in rows if r["market"] != market or first <= r["moment_date"] <= last]
    keys = Counter((r["moment_date"], r["market"], r["name"]) for r in rows)
    duplicates = [k for k, n in keys.items() if n > 1]
    if duplicates:
        raise ValueError(f"duplicate calendar keys: {duplicates}")
    missing = [r for r in rows if not r["source"]]
    if missing:
        raise ValueError(f"calendar rows without a source: {missing}")
    return sorted(rows, key=lambda r: (r["moment_date"], r["market"], r["name"]))


def rows_parameter(rows):
    return bigquery.ArrayQueryParameter("rows", "STRUCT", [
        bigquery.StructQueryParameter(
            None,
            bigquery.ScalarQueryParameter("moment_date", "DATE", r["moment_date"]),
            bigquery.ScalarQueryParameter("market", "STRING", r["market"]),
            bigquery.ScalarQueryParameter("name", "STRING", r["name"]),
            bigquery.ScalarQueryParameter("kind", "STRING", r["kind"]),
            bigquery.ScalarQueryParameter("source", "STRING", r["source"]),
        ) for r in rows])


def apply(client, rows):
    parameters = [rows_parameter(rows)]
    dry = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=parameters)
    try:
        client.query(MERGE_SQL, job_config=dry, location=LOCATION)
    except Exception as error:
        print(f"dry run failed on the calendar MERGE: {error}")
        return 1
    print("dry run ok: calendar MERGE")
    job = client.query(MERGE_SQL, job_config=bigquery.QueryJobConfig(query_parameters=parameters),
                       location=LOCATION)
    job.result()
    print(f"MERGE ok: {job.num_dml_affected_rows} rows inserted in {TABLE}")
    return 0


# Last year's analogues (task 3.6) -----------------------------------------------------


def month_bounds(year, month):
    return date(year, month, 1), date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def analogue_window(analogue_date):
    return analogue_date - timedelta(days=ANALOGUE_DAYS), analogue_date + timedelta(days=ANALOGUE_DAYS)


def match_analogue(row, moments, fetch=False):
    """Last year's date for a calendar row: the rule's date that month, else last year's dated source row
    of the same name, else the fixed day and month from the fixed list, else none with the reason. A failed
    date.nager.at fetch is carried as the reason."""
    moment, market, name = row["moment_date"], row["market"], row["name"]
    year = moment.year - 1

    def found(analogue_date, kind, reason=None):
        return {"analogue_date": analogue_date, "match_kind": kind, "reason": reason}

    rule = next((r for r in moments["rules"] if r["name"] == name and market in as_list(r["market"])), None)
    if rule:
        first, last = month_bounds(year, moment.month)
        dates = rule_dates(rule, first, last)
        return found(dates[0], "rule") if dates else found(
            None, "none", f"the rule for {name} gives no date in {first:%B %Y}")
    failed = None
    try:
        held = nager_rows(market, year, fetch, renames(moments))
    except FileNotFoundError:
        held = None
    except NagerUnavailable as error:
        held, failed = None, str(error)
    same = [r["moment_date"] for r in held or [] if r["name"] == name]
    if same:
        target = safe_date(year, moment.month, moment.day) or moment - timedelta(days=365)
        return found(min(same, key=lambda d: abs((d - target).days)), "source_row")
    fixed = moments["fixed_dates"].get(market, {}).get(name)
    if fixed:
        month, day = (int(x) for x in fixed.split("-"))
        return found(date(year, month, day), "fixed_date", failed and f"{failed}; dated from the fixed list")
    if failed:
        return found(None, "none", f"{failed}, and {name} has no fixed date")
    if held is None:
        return found(None, "none", f"{name} moves from year to year, no date.nager.at {year} {market} rows "
                                   f"are held and it has no fixed date")
    return found(None, "none", f"no {name} row in date.nager.at {year} {market}")


def analogue_plan(rows, moments, fetch=False):
    plan = []
    for r in rows:
        match = match_analogue(r, moments, fetch)
        start, end = analogue_window(match["analogue_date"]) if match["analogue_date"] else (None, None)
        plan.append({"row": r, **match, "window_start": start, "window_end": end})
    return plan


def evidence_params(plan):
    """The evidence query's parameters as plain values, or None when no row has an analogue."""
    timezones = markets()
    dated = [p for p in plan if p["analogue_date"]]
    if not dated:
        return None
    market_codes = sorted({p["row"]["market"] for p in dated})
    return {
        "windows": [{"moment_date": p["row"]["moment_date"].isoformat(), "market": p["row"]["market"],
                     "name": p["row"]["name"], "timezone": timezones[p["row"]["market"]],
                     "window_start": p["window_start"].isoformat(), "window_end": p["window_end"].isoformat()}
                    for p in dated],
        "markets": market_codes,
        "legacy_markets": [m.lower() for m in market_codes],
        "min_start": min(p["window_start"] for p in dated).isoformat(),
        "max_end": max(p["window_end"] for p in dated).isoformat(),
        "top_read": TOP_READ,
    }


def query_params(params):
    def scalar(name, kind, value):
        return bigquery.ScalarQueryParameter(name, kind, date.fromisoformat(value) if kind == "DATE" else value)
    return [
        bigquery.ArrayQueryParameter("windows", "STRUCT", [
            bigquery.StructQueryParameter(None, *(scalar(n, k, w[n]) for n, k in WINDOW_FIELDS))
            for w in params["windows"]]),
        bigquery.ArrayQueryParameter("markets", "STRING", params["markets"]),
        bigquery.ArrayQueryParameter("legacy_markets", "STRING", params["legacy_markets"]),
        scalar("min_start", "DATE", params["min_start"]),
        scalar("max_end", "DATE", params["max_end"]),
        bigquery.ScalarQueryParameter("top_read", "INT64", params["top_read"]),
    ]


def stamp(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def analogue_records(plan, found):
    """One record per calendar row: its analogue, window, the labels found there and the query that
    found them. Rule 1 labels are held back and counted in the reason; an empty window says so."""
    params = evidence_params(plan)
    hits = defaultdict(list)
    for f in found:
        hits[(f["moment_date"], f["market"], f["name"])].append(f)
    records = []
    for p in plan:
        r = p["row"]
        rec = {"moment_date": r["moment_date"], "market": r["market"], "name": r["name"],
               "analogue_date": p["analogue_date"], "match_kind": p["match_kind"],
               "window_start": p["window_start"], "window_end": p["window_end"]}
        if not p["analogue_date"]:
            records.append(dict(rec, status="no_analogue", reason=p["reason"], evidence=None, query_text=None,
                                query_params=None))
            continue
        got = hits[(r["moment_date"], r["market"], r["name"])]
        held = sum(1 for h in got if blocked(h["label"]))
        evidence = []
        for source in (LEGACY_SOURCE, POSTS_SOURCE):
            ranked = sorted((h for h in got if h["source"] == source and not blocked(h["label"])),
                            key=lambda h: (-h["posts"], h["label"]))
            evidence += [{"source": source, "label": h["label"], "posts": int(h["posts"]),
                          "first_seen": stamp(h["first_seen"])} for h in ranked[:TOP_KEEP]]
        note = f"{held} label{'' if held == 1 else 's'} held back by rule 1" if held else None
        if evidence:
            status, reason = "evidence", "; ".join(x for x in (note, p["reason"]) if x) or None
        else:
            status = "no_evidence"
            reason = "; ".join(x for x in (
                f"no hashtags in {r['market']} posts from {p['window_start']} to {p['window_end']} "
                f"in {LEGACY_SOURCE} or {POSTS_SOURCE}", note, p["reason"]) if x)
        records.append(dict(rec, status=status, reason=reason, evidence=json.dumps(evidence),
                            query_text=EVIDENCE_SQL, query_params=json.dumps(params)))
    return records


def records_parameter(records):
    return bigquery.ArrayQueryParameter("rows", "STRUCT", [
        bigquery.StructQueryParameter(None, *(bigquery.ScalarQueryParameter(n, k, rec[n]) for n, k in RECORD_FIELDS))
        for rec in records])


def run_analogues(client, plan):
    """Dry-run and cap the evidence read, run it, then dry-run and append one record per calendar row."""
    params = evidence_params(plan)
    try:
        found = list(_checked(client, EVIDENCE_SQL, query_params(params)).result()) if params else []
        records = analogue_records(plan, found)
        job = _checked(client, INSERT_ANALOGUES_SQL, [records_parameter(records)])
        job.result()
    except OverCap as error:
        print(f"refused: {error}")
        return 1
    counts = Counter(rec["status"] for rec in records)
    print(f"{ANALOGUE_TABLE}: {job.num_dml_affected_rows} records appended "
          f"({', '.join(f'{s} {n}' for s, n in sorted(counts.items()))})")
    return 0


def weekly_analogues(client, start=None):
    """The weekly step the f42-drift job runs after its report: today's calendar rows from the fixtures
    and moments.yaml (no date.nager.at fetch), last year's analogue for each, then run_analogues, which
    dry-runs every query and refuses one above MAX_BYTES. Returns run_analogues' exit code."""
    moments = load_yaml("moments.yaml")
    plan = analogue_plan(build_rows(start=start, moments=moments), moments)
    print(f"calendar analogues: {len(plan)} calendar rows from {start or 'today'}")
    return run_analogues(client, plan)


def print_analogue_plan(plan, fetch=False):
    for p in plan:
        r = p["row"]
        head = f"{r['moment_date'].isoformat()}  {r['market']}  {r['name']}"
        if p["analogue_date"]:
            print(f"{head}  analogue {p['analogue_date'].isoformat()} ({p['match_kind']}), "
                  f"window {p['window_start'].isoformat()} to {p['window_end'].isoformat()}"
                  + (f"; {p['reason']}" if p["reason"] else ""))
        else:
            print(f"{head}  no analogue: {p['reason']}")
    params = evidence_params(plan)
    if params:
        print("evidence query, one read-only SELECT:")
        print(EVIDENCE_SQL)
        print("parameters:")
        print(json.dumps(params, indent=1))
    print(f"dry-run bytes: not estimated on a plan; a live run dry-runs first and refuses above {MAX_BYTES:,} bytes")
    print(f"a live run then appends {len(plan)} records to {ANALOGUE_TABLE}")
    print("plan only; no BigQuery call (date.nager.at fetched live)" if fetch else "plan only; no network")


def print_rows(rows, pending):
    for r in rows:
        print(f"{r['moment_date'].isoformat()}  {r['market']}  {r['name']}  [{r['kind']}]  {r['source']}")
    counts = Counter(r["market"] for r in rows)
    for market in sorted(counts):
        print(f"{market}: {counts[market]} rows")
    print(f"total: {len(rows)} rows")
    for p in pending:
        print(f"not loaded (pending): {', '.join(as_list(p['market']))}  {p['name']}: {p['missing']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the moments calendar and optionally MERGE it.")
    parser.add_argument("--start", type=date.fromisoformat, help="load date, default today per market")
    parser.add_argument("--fetch", action="store_true", help="fetch date.nager.at live instead of the fixtures")
    parser.add_argument("--apply", action="store_true", help="dry-run, then run the MERGE into BigQuery")
    parser.add_argument("--analogues", action="store_true",
                        help="last year's analogue and evidence per row; with --apply, read and append them")
    args = parser.parse_args(argv)
    moments = load_yaml("moments.yaml")
    rows = build_rows(start=args.start, fetch=args.fetch, moments=moments)
    if args.analogues:
        plan = analogue_plan(rows, moments, args.fetch)
        for reason in nager_failures():
            print(f"not loaded: {reason}")
        if not args.apply:
            print_analogue_plan(plan, args.fetch)
            return 0
        return run_analogues(bigquery.Client(project="ogilvy-trends-v2"), plan)
    print_rows(rows, moments["pending"])
    for reason in nager_failures():
        print(f"not loaded: {reason}")
    if not args.apply:
        print("dry run only; nothing loaded")
        return 0
    return apply(bigquery.Client(project="ogilvy-trends-v2"), rows)


if __name__ == "__main__":
    sys.exit(main())
