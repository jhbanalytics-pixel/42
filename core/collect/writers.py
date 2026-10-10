"""BigQuery writes of the collect job (core/collect/job.py), after its last call.

posts: one MERGE on post_id per batch of up to 500 rows, the rows passed as an ARRAY<STRUCT> query
parameter. A new post is inserted with the fields of its first sighting; a known post keeps them and
takes the latest metric reading (a reading with no metric at all changes nothing), and takes the new geo
only while its stored geo_market is NULL. The MERGE has no matched-row removal clause.

creators: one MERGE on (platform, creator_id) per batch: a new creator is inserted; a known one moves handle,
display_name, followers, verified, profile_location and last_seen, never first_seen (CREATORS_MERGE_SQL).
The backfill (job.py --backfill-creators) reads raw_responses with read_raw, merges creators the same way,
and fills NULL geo on stored posts with GEO_FILL_SQL, which has the posts MERGE's geo rule and no INSERT.

post_observations, item_counter_daily and collection_health: load jobs in WRITE_APPEND. The counter rows
gain, after the pulls, one appearances row per item, list and day (the pulls of this run it was on, from
the rank rows) and one delta row per total read (today's latest total minus the previous day's latest,
read with a parameterised SELECT; no row when there is no previous reading).

collection_health: one row per day, market, series and protocol, the day being the market-local day of
the fetches it summarises (the day parse gives their rows), judged as DATA.md section 3.3 does: calls
when under 80% of calls succeeded (a series skipped for want of input has 0 calls and fails here too),
items when the day's rows fall outside half to twice the median of up to 28 prior valid days (only once
3 exist), effort when a panel's k = units_planned / units_ok lies outside 0.5 to 2. The drift check
(TRUST.md A1) is not built yet. A repair run (COLLECT_ONLY_ROUTES) also appends the day's good collect run's
rows for every key it did not read, under its own run_id (repair_base, carry_forward).

last_pulls reads the highest pull_seq stored per market, series and protocol, so each rank list's pull
number keeps running from one run to the next: from item_counter_daily for the SocialCrawl rank lists and
the local sources' charts (task 2.12), and from post_observations (lane_class unbiased_rank) only for the
local lists that write no counters, the Nairaland front page. Both reads are bounded to the LAST_PULL_DAYS
(35) before the run date on their date partitions: the 28 day reference window plus a week of outage. A
list silent for longer restarts at pull 1. That is not fully safe for detection's top10_twice (DATA.md,
the t10 step), which pairs pull k with pull k - 1 of the same list and protocol with no date bound: after
a restart, pull 2 would also pair with the old pull 1 from before the gap, so an item in the top 10 of
both could be flagged without being in the top 10 on consecutive pulls. Bounding that join by date closes
it; until then a gap over 35 days in one list is the only way to reach it.

cultural_map (L2 Needs 11): every item on this run's counter rows (boards, charts, rank lists, counts and the
local charts) goes into cultural_map through L2's own MERGE, core.detect.aggregate.cultural_map_merge_sql(),
with rows shaped like aggregate.items_rows and passed as the @items parameter, so a board or chart item has a
readable name before detect sees it. The MERGE only inserts new items and moves last_seen. An item is left
out, and counted, when its label is one gdelt.blocked refuses (rule 1) or when it has no readable label (a
bare id): an insert with no name would keep a later named one out.

post_items (L2 Needs 34c): the pulse appends its posts' item links with L2's own INSERT,
core.detect.aggregate.POST_ITEMS_INSERT_SQL, which adds only the post and item pairs not already there.

read_watches reads the current watches (intelligence_42_agent.v_watches_current) that are active in one of
the markets or in all of them, with the cultural_map kind and key of an item target, by a parameterised
SELECT. A project without the view yet reads as no watches.

Every statement here appends, merges or reads. None alters or removes a table or a row.
"""

import json
import logging
import math
import re
from collections import Counter
from datetime import date, datetime, timedelta

from core.collect.chain import PROJECT
from core.collect.local_sources import LOCAL_RANK_SERIES, SCRAPE_SOURCES
from core.collect.parse import POST_COLUMNS, ROUTES
from core.collect.socialcrawl_client import PRICED, http_failure
from core.detect.geo import KNOWN as GEO_KNOWN

log = logging.getLogger(__name__)

# L2's profile joins use platform and creator_id, while coordination flags stay grouped by creator_id. Shared
# handles can now be written without multiplying rows in creator-profile readers.
CREATORS_WRITE = True

CORE = "intelligence_42_core"
AGENT = "intelligence_42_agent"
BATCH = 500
PUBLIC_FEED_MERGE_BUDGET_BYTES = 5 * 1024 ** 3
PUBLIC_FEED_WRITE_FAILED = "public_feed_write_failed"
METRICS = ("views", "likes", "comments", "shares", "engagement")
GEO = ("geo_market", "geo_confidence", "geo_source")
KNOWN_GEO = ("ext_region", "home_market", "place_mention")  # language alone is never a known location
TYPES = {"duration_s": "FLOAT64", "geo_confidence": "FLOAT64", "published_at": "TIMESTAMP", "post_date": "DATE",
         "views": "INT64", "likes": "INT64", "comments": "INT64", "shares": "INT64", "engagement": "INT64",
         "hashtags": "ARRAY", "vendor_labels": "JSON"}
POST_TYPES = {name: TYPES.get(name, "STRING") for name in POST_COLUMNS}
HEALTH_COLUMNS = (
    "day", "market", "platform", "route", "series", "protocol", "lane_class", "calls", "calls_ok",
    "units_planned", "units_ok", "items", "ref_items", "ref_days", "k", "valid", "invalid_reason",
    "located_share", "run_id")


class PublicFeedWriteError(RuntimeError):
    category = PUBLIC_FEED_WRITE_FAILED

    def __init__(self, original_exception):
        super().__init__(self.category)
        self._original_exception = original_exception


RANK_SERIES = tuple(sorted({entry[2] for entry in ROUTES.values() if entry[0] in ("rank", "board")}))
# Local rank lists that write no counter rows, only ranked post_observations (the Nairaland front page).
OBSERVATION_SERIES = (SCRAPE_SOURCES["nairaland"].series,)
PUBLIC_FEED_COUNTER_SERIES = ("board_music_country", "radio_playlist")
PUBLIC_FEED_OBSERVATION_SERIES = ("news_rss",)


def table(name, dataset=CORE):
    return f"{PROJECT}.{dataset}.{name}"


def _source(name):
    return f"SAFE.PARSE_JSON(S.{name})" if POST_TYPES[name] == "JSON" else f"S.{name}"


HAS_METRIC = "NOT (S.views IS NULL AND S.likes IS NULL AND S.comments IS NULL AND S.shares IS NULL)"
# Geo is written only at core.detect.geo.KNOWN (0.7, what is_known and the A5 and G6 local_share count), so a
# language guess at 0.3 never takes the empty slot a later known location would fill.
FILLS_GEO = f"T.geo_market IS NULL AND S.geo_market IS NOT NULL AND S.geo_confidence >= {GEO_KNOWN}"
MERGE_SQL = (
    "MERGE `{table}` T\n"
    "USING (SELECT * FROM UNNEST(@rows)) S\n"
    "ON T.post_id = S.post_id\n"
    f"WHEN MATCHED AND (({HAS_METRIC}) OR ({FILLS_GEO})) THEN\n"
    "  UPDATE SET " + ", ".join(f"{m} = IF({HAS_METRIC}, S.{m}, T.{m})" for m in METRICS) + ",\n"
    "  " + ", ".join(f"{g} = IF({FILLS_GEO}, S.{g}, T.{g})" for g in GEO) + "\n"
    "WHEN NOT MATCHED THEN INSERT (" + ", ".join(POST_TYPES) + ")\n"
    "  VALUES (" + ", ".join(_source(n) for n in POST_TYPES) + ")"
)
PUBLIC_FEED_COLUMNS = tuple(name for name in POST_TYPES if name != "post_id")
PUBLIC_FEED_MERGE_SQL = (
    "MERGE `{table}` T\n"
    "USING (SELECT * FROM UNNEST(@rows)) S\n"
    "ON T.post_id = S.post_id\n"
    "WHEN MATCHED AND (" + " OR ".join(f"(T.{name} IS NULL AND S.{name} IS NOT NULL)"
                                         for name in PUBLIC_FEED_COLUMNS) + ") THEN\n"
    "  UPDATE SET " + ", ".join(f"{name} = COALESCE(T.{name}, {_source(name)})"
                                 for name in PUBLIC_FEED_COLUMNS) + "\n"
    "WHEN NOT MATCHED THEN INSERT (" + ", ".join(POST_TYPES) + ")\n"
    "  VALUES (" + ", ".join(_source(name) for name in POST_TYPES) + ")"
)
# The backfill's geo fill: the same rule on posts already stored, and no INSERT, so a post the run left out
# (a panel post before since=, a response that came back on another day) never enters posts this way.
GEO_TYPES = {"post_id": "STRING", "geo_market": "STRING", "geo_confidence": "FLOAT64", "geo_source": "STRING"}
GEO_FILL_SQL = (
    "MERGE `{table}` T\n"
    "USING (SELECT * FROM UNNEST(@rows)) S\n"
    "ON T.post_id = S.post_id\n"
    f"WHEN MATCHED AND {FILLS_GEO} THEN\n"
    "  UPDATE SET " + ", ".join(f"{g} = S.{g}" for g in GEO)
)

# creators: keyed (platform, creator_id). A new creator is inserted; a known one takes each of CREATOR_UPDATES
# from a sighting at or after its last_seen when the sighting has a value, from an older one only where it
# holds none, and last_seen only moves forward. first_seen is never set on a known creator.
CREATOR_TYPES = {"creator_id": "STRING", "platform": "STRING", "handle": "STRING", "display_name": "STRING",
                 "followers": "INT64", "verified": "BOOL", "profile_location": "STRING", "home_market": "STRING", "first_seen": "TIMESTAMP",
                 "last_seen": "TIMESTAMP"}
CREATOR_UPDATES = ("handle", "display_name", "followers", "verified", "profile_location", "home_market")
NEWER = "(T.last_seen IS NULL OR S.last_seen >= T.last_seen)"
CREATORS_MERGE_SQL = (
    "MERGE `{table}` T\n"
    "USING (SELECT * FROM UNNEST(@rows)) S\n"
    "ON T.platform = S.platform AND T.creator_id = S.creator_id\n"
    "WHEN MATCHED THEN\n"
    "  UPDATE SET " + ", ".join(f"{c} = IF(S.{c} IS NOT NULL AND ({NEWER} OR T.{c} IS NULL), S.{c}, T.{c})"
                                for c in CREATOR_UPDATES) + ",\n"
    f"  last_seen = IF({NEWER}, S.last_seen, T.last_seen)\n"
    "WHEN NOT MATCHED THEN INSERT (" + ", ".join(CREATOR_TYPES) + ")\n"
    "  VALUES (" + ", ".join(f"S.{c}" for c in CREATOR_TYPES) + ")"
)

PREVIOUS_SQL = (
    "SELECT c.market, c.item_id, c.series, c.protocol, c.value FROM `{table}` c\n"
    "WHERE c.obs_date = @prev AND c.unit = 'total' AND c.item_id IN UNNEST(@items)\n"
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY c.market, c.item_id, c.series, c.protocol "
    "ORDER BY c.available_at DESC) = 1"
)

LAST_PULL_DAYS = 35
LAST_PULLS_SQL = (
    "SELECT c.market, c.series, c.protocol, MAX(c.pull_seq) AS last_pull FROM (\n"
    "  SELECT market, series, protocol, pull_seq FROM `{counters}`\n"
    f"  WHERE obs_date >= DATE_SUB(@day, INTERVAL {LAST_PULL_DAYS} DAY)\n"
    "    AND pull_seq IS NOT NULL AND (series IN UNNEST(@series)\n"
    "      OR (route = 'public_feed' AND series IN UNNEST(@public_feed_counter_series)))\n"
    "  UNION ALL\n"
    "  SELECT market, series, protocol, pull_seq FROM `{observations}`\n"
    f"  WHERE observed_date >= DATE_SUB(@day, INTERVAL {LAST_PULL_DAYS} DAY) AND pull_seq IS NOT NULL\n"
    "    AND ((lane_class = 'unbiased_rank' AND series IN UNNEST(@observation_series))\n"
    "      OR (route = 'public_feed' AND series IN UNNEST(@public_feed_observation_series)))) c\n"
    "GROUP BY c.market, c.series, c.protocol"
)

REFERENCE_SQL = (
    "WITH good AS (\n"
    "  SELECT r.run_id, MAX(r.finished_at) AS finished_at FROM `{runs}` r\n"
    "  WHERE r.stage = 'collect' AND r.status = 'ok' GROUP BY r.run_id),\n"
    "latest AS (\n"
    "  SELECT x.market, x.series, x.protocol, x.day, x.items, x.valid\n"
    "  FROM `{health}` x JOIN good g ON g.run_id = x.run_id\n"
    "  WHERE x.day BETWEEN DATE_SUB(@d, INTERVAL 28 DAY) AND DATE_SUB(@d, INTERVAL 1 DAY)\n"
    "  QUALIFY ROW_NUMBER() OVER (PARTITION BY x.day, x.market, x.series, x.protocol ORDER BY g.finished_at DESC) = 1)\n"
    "SELECT h.market, h.series, h.protocol, APPROX_QUANTILES(h.items, 2)[OFFSET(1)] AS ref_items, COUNT(*) AS ref_days\n"
    "FROM latest h WHERE h.valid GROUP BY h.market, h.series, h.protocol"
)


WATCHES_SQL = (
    "SELECT w.watch_id, w.market, w.status, w.label, TO_JSON_STRING(w.target) AS target,\n"
    "  m.kind AS item_kind, m.canonical_key AS item_key, m.label AS item_label\n"
    "FROM `{watches}` w\n"
    "LEFT JOIN `{cultural_map}` m ON m.item_id = JSON_VALUE(w.target, '$.item_id') AND m.valid_to IS NULL\n"
    "WHERE w.status = 'active' AND w.market IN UNNEST(@markets)\n"
    "ORDER BY COALESCE(w.created_at, w.status_at), w.watch_id"
)
GLOBAL = "GLOBAL"
ALL_MARKETS = "all"  # a watch's market when it follows every market (contract 10.5)


def _query(bq, sql, params):
    from google.cloud import bigquery

    return list(bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())


# post_items: one row per post and item, never repeated (core/detect/aggregate.py writes the same way)

POST_ITEMS_INSERT_SQL = """INSERT INTO `{table}` (post_id, item_id, via)
SELECT DISTINCT n.post_id, n.item_id, n.via FROM UNNEST(@rows) n
WHERE NOT EXISTS (SELECT 1 FROM `{table}` p WHERE p.post_id = n.post_id AND p.item_id = n.item_id)"""


def insert_post_items(bq, rows, batch=BATCH):
    """Insert post_items rows not already there, batch rows a statement. Returns the number of statements run."""
    from google.cloud import bigquery

    statements = 0
    for start in range(0, len(rows), batch):
        param = bigquery.ArrayQueryParameter("rows", "STRUCT", [
            bigquery.StructQueryParameter(None, *[bigquery.ScalarQueryParameter(f, "STRING", r[f])
                                                  for f in ("post_id", "item_id", "via")])
            for r in rows[start:start + batch]])
        _query(bq, POST_ITEMS_INSERT_SQL.format(table=table("post_items")), [param])
        statements += 1
    return statements


# posts

def dedupe_posts(rows):
    """One row per post_id: the first sighting's fields with the latest reading that has any metric, and the
    first known sighting's geo when the first sighting had none. Geo below GEO_KNOWN is not written."""
    out = {}
    for row in rows:
        if not _known(row):
            row = dict(row, **dict.fromkeys(GEO))
        kept = out.get(row["post_id"])
        if kept is None:
            out[row["post_id"]] = dict(row)
            continue
        if any(row.get(m) is not None for m in METRICS[:4]):
            kept.update({m: row.get(m) for m in METRICS})
        if kept.get("geo_market") is None and row.get("geo_market") is not None:
            kept.update({g: row.get(g) for g in GEO})
    return list(out.values())


def _known(row):
    return row.get("geo_market") is not None and (row.get("geo_confidence") or 0) >= GEO_KNOWN


def dedupe_creators(rows):
    """One row per (platform, creator_id): the earliest first_seen, the latest last_seen, and each updatable
    field from the latest sighting that has it."""
    out = {}
    for row in sorted(rows, key=lambda r: _instant(r["last_seen"])):
        key = (row["platform"], row["creator_id"])
        kept = out.get(key)
        if kept is None:
            out[key] = {c: row.get(c) for c in CREATOR_TYPES}
            continue
        kept.update({c: row[c] for c in CREATOR_UPDATES if row.get(c) is not None})
        kept["first_seen"] = min(kept["first_seen"], row["first_seen"], key=_instant)
        kept["last_seen"] = row["last_seen"]
    return list(out.values())


def _instant(value):
    return value if value is None or isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _day(value):
    return value if value is None or isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _struct(row, types=POST_TYPES):
    from google.cloud import bigquery

    convert = {"INT64": int, "FLOAT64": float, "TIMESTAMP": _instant, "DATE": _day, "STRING": str, "BOOL": bool}
    fields = []
    for name, kind in types.items():
        value = row.get(name)
        if kind == "ARRAY":
            fields.append(bigquery.ArrayQueryParameter(name, "STRING", [str(v) for v in value or []]))
        elif kind == "JSON":
            text = None if value is None else json.dumps(value, ensure_ascii=False, default=str)
            fields.append(bigquery.ScalarQueryParameter(name, "STRING", text))
        else:
            fields.append(bigquery.ScalarQueryParameter(name, kind, None if value is None else convert[kind](value)))
    return bigquery.StructQueryParameter(None, *fields)


def _merge(bq, sql, rows, types, batch):
    from google.cloud import bigquery

    statements = 0
    for start in range(0, len(rows), batch):
        param = bigquery.ArrayQueryParameter("rows", "STRUCT", [_struct(r, types) for r in rows[start:start + batch]])
        _query(bq, sql, [param])
        statements += 1
    return statements


def merge_posts(bq, rows, batch=BATCH):
    """MERGE rows into posts, batch rows a statement. Returns the number of statements run."""
    return _merge(bq, MERGE_SQL.format(table=table("posts")), dedupe_posts(rows), POST_TYPES, batch)


def dedupe_public_feed_posts(rows):
    """One source row per post_id, keeping the first non-null value for each field."""
    out = {}
    for row in rows:
        kept = out.get(row["post_id"])
        if kept is None:
            out[row["post_id"]] = dict(row)
            continue
        for name in PUBLIC_FEED_COLUMNS:
            if kept.get(name) is None and row.get(name) is not None:
                kept[name] = row[name]
    return list(out.values())


def merge_public_feed_posts(bq, rows, batch=BATCH):
    """Add public-feed post values without replacing any stored non-null field."""
    return _merge_public_feed(bq, dedupe_public_feed_posts(rows), batch, PUBLIC_FEED_MERGE_BUDGET_BYTES)


def _merge_public_feed(bq, rows, batch, budget_bytes):
    try:
        return _merge_public_feed_batches(bq, rows, batch, budget_bytes)
    except PublicFeedWriteError:
        raise
    except Exception as exc:
        raise PublicFeedWriteError(exc) from None


def _merge_public_feed_batches(bq, rows, batch, budget_bytes):
    from google.cloud import bigquery

    sql = PUBLIC_FEED_MERGE_SQL.format(table=table("posts"))
    default_config = getattr(bq, "default_query_job_config", None)
    default_cap = getattr(default_config, "maximum_bytes_billed", None)
    remaining = budget_bytes
    statements = 0
    for start in range(0, len(rows), batch):
        if remaining <= 0:
            raise RuntimeError("public-feed MERGE byte budget exhausted")
        cap = remaining if default_cap is None else min(remaining, default_cap)
        if cap <= 0:
            raise RuntimeError("public-feed MERGE byte budget exhausted by default query cap")
        param = bigquery.ArrayQueryParameter(
            "rows", "STRUCT", [_struct(row, POST_TYPES) for row in rows[start:start + batch]])
        config = bigquery.QueryJobConfig(query_parameters=[param], maximum_bytes_billed=cap)
        job = bq.query(sql, job_config=config, retry=None, job_retry=None)
        job.result(retry=None, job_retry=None)
        billed = getattr(job, "total_bytes_billed", None)
        if billed is None or isinstance(billed, bool) or not isinstance(billed, int) or billed < 0:
            raise RuntimeError("public-feed MERGE total_bytes_billed is unavailable")
        if billed > cap or billed > remaining:
            raise RuntimeError("public-feed MERGE exceeded maximum_bytes_billed")
        remaining -= billed
        statements += 1
    return statements


def fill_geo(bq, rows, batch=BATCH):
    """The backfill's geo fill on posts already stored: one row per post_id (the first known one), and only
    posts whose stored geo_market is NULL change. Returns the number of statements run."""
    located = {}
    for row in rows:
        if _known(row):
            located.setdefault(row["post_id"], {g: row.get(g) for g in GEO_TYPES})
    return _merge(bq, GEO_FILL_SQL.format(table=table("posts")), list(located.values()), GEO_TYPES, batch)


def merge_creators(bq, rows, batch=BATCH):
    """MERGE rows into creators, one per (platform, creator_id), batch a statement. Returns the statements run."""
    return _merge(bq, CREATORS_MERGE_SQL.format(table=table("creators")), dedupe_creators(rows), CREATOR_TYPES,
                  batch)


RAW_SQL = (
    "SELECT r.run_id, r.market, r.route, r.lane, r.seed_key, r.fetched_at, TO_JSON_STRING(r.body) AS body\n"
    "FROM `{table}` r\n"
    "WHERE DATE(r.fetched_at) BETWEEN @since AND @until AND r.http_status = 200 AND r.body IS NOT NULL\n"
    "  AND r.job IN UNNEST(@jobs) AND r.route IN UNNEST(@routes)\n"
    "ORDER BY r.fetched_at"
)


def read_raw(bq, since, until, jobs, routes):
    """The stored HTTP 200 bodies of jobs on routes fetched from since to until (UTC dates), oldest first.
    Dry-run first and refused over gdelt.MAX_BYTES (gdelt.OverCap), then run with maximum_bytes_billed."""
    from google.cloud import bigquery

    from core.collect.gdelt import _checked  # gdelt imports job, which imports this module

    return list(_checked(bq, RAW_SQL.format(table=table("raw_responses")), [
        bigquery.ScalarQueryParameter("since", "DATE", since),
        bigquery.ScalarQueryParameter("until", "DATE", until),
        bigquery.ArrayQueryParameter("jobs", "STRING", list(jobs)),
        bigquery.ArrayQueryParameter("routes", "STRING", list(routes))]).result())


# Appends

def append(bq, name, rows):
    """A load job in WRITE_APPEND with the table's own schema. Returns the rows sent."""
    if not rows:
        return 0
    from google.cloud import bigquery

    table_id = table(name)
    config = bigquery.LoadJobConfig(schema=bq.get_table(table_id).schema,
                                    write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
    bq.load_table_from_json(rows, table_id, job_config=config).result()
    return len(rows)


def appearances(rows, run_id):
    """Per day, market, list and item: how many of this run's pulls the item was on."""
    groups = {}
    for r in rows:
        if r["unit"] != "rank":
            continue
        key = (r["obs_date"], r["market"], r["platform"], r["item_id"], r["series"], r["protocol"])
        group = groups.setdefault(key, {"row": r, "pulls": set(), "at": r["observed_at"]})
        group["pulls"].add(r["pull_seq"])
        group["at"] = max(group["at"], r["observed_at"])
    return [dict(g["row"], unit="appearances", pull_seq=None, value=float(len(g["pulls"])), source="live",
                 observed_at=g["at"], available_at=g["at"], run_id=run_id) for g in groups.values()]


def last_pulls(bq, day):
    """(market, series, protocol) -> the highest pull_seq stored for that rank list, local ones included,
    over the LAST_PULL_DAYS before day."""
    from google.cloud import bigquery

    counted = sorted(set(RANK_SERIES + LOCAL_RANK_SERIES) - set(OBSERVATION_SERIES))
    sql = LAST_PULLS_SQL.format(counters=table("item_counter_daily"), observations=table("post_observations"))
    rows = _query(bq, sql, [bigquery.ScalarQueryParameter("day", "DATE", day),
                            bigquery.ArrayQueryParameter("series", "STRING", counted),
                            bigquery.ArrayQueryParameter("observation_series", "STRING", list(OBSERVATION_SERIES)),
                            bigquery.ArrayQueryParameter("public_feed_counter_series", "STRING",
                                                         list(PUBLIC_FEED_COUNTER_SERIES)),
                            bigquery.ArrayQueryParameter("public_feed_observation_series", "STRING",
                                                         list(PUBLIC_FEED_OBSERVATION_SERIES))])
    return {(r["market"], r["series"], r["protocol"]): int(r["last_pull"]) for r in rows if r["last_pull"] is not None}


def previous_totals(bq, prev, items):
    from google.cloud import bigquery

    rows = _query(bq, PREVIOUS_SQL.format(table=table("item_counter_daily")), [
        bigquery.ScalarQueryParameter("prev", "DATE", prev),
        bigquery.ArrayQueryParameter("items", "STRING", items)])
    return {(r["market"], r["item_id"], r["series"], r["protocol"]): float(r["value"])
            for r in rows if r["value"] is not None}


def deltas(bq, rows, run_id):
    """Today's latest total minus the previous day's latest total, per item and series; none without one."""
    latest = {}
    for r in rows:
        if r["unit"] != "total" or r["value"] is None:
            continue
        key = (r["obs_date"], r["market"], r["item_id"], r["series"], r["protocol"])
        if key not in latest or r["available_at"] > latest[key]["available_at"]:
            latest[key] = r
    out = []
    for day in sorted({key[0] for key in latest}):
        today = [r for key, r in latest.items() if key[0] == day]
        prev = previous_totals(bq, date.fromisoformat(day) - timedelta(days=1), sorted({r["item_id"] for r in today}))
        for r in today:
            before = prev.get((r["market"], r["item_id"], r["series"], r["protocol"]))
            if before is not None:
                out.append(dict(r, unit="delta", value=r["value"] - before, pull_seq=None, source="live",
                                run_id=run_id))
    return out


# collection_health

def reference(bq, day):
    """(market, series, protocol) -> (median items, valid days) over the prior 28 days of good collect runs."""
    from google.cloud import bigquery

    rows = _query(bq, REFERENCE_SQL.format(runs=table("runs", AGENT), health=table("collection_health")),
                  [bigquery.ScalarQueryParameter("d", "DATE", day)])
    return {(r["market"], r["series"], r["protocol"]): (float(r["ref_items"]), int(r["ref_days"])) for r in rows}


# W8-DEC-11: a paid route whose calls succeed on two consecutive days and land zero posts and zero counters is
# recorded invalid for the second and later of those days, with reason zero_yield. A day is a zero-yield day when
# every call of its series answered, none landed an observation or a counter, and the route is a SocialCrawl route
# (PRICED) outside the search lanes, which are sparse by design and never decide G1. The first zero day stays
# valid as written; v_collection_health_current (core/detect/sql/views.sql) reads it as invalid once the next
# day's row names zero_yield. The judge's own reasons come first: a day already invalid keeps its reason.
# The day before is found by (market, series, route, lane_class), and not by protocol: the curated panel's
# protocol is a hash of the day's rotation and changes every day, and every route that versions its token
# changes protocol on the switch day, so a protocol key would never see a dead route twice. The series is in the
# key because one route and lane carry several panels (the culture desk, the curated panel and the Instagram
# gossip panel are all prism/profiles in lane panel): a zero day of one is not the day before of another. The
# rows marked are the series rows, one per (market, series, protocol).
ZERO_YIELD = "zero_yield"
# Search lanes are sparse by design, and W8-DEC-11 governs G1's input, which never reads them (the health query in
# core/brief/sql/gatectx.sql takes unbiased_rank, panel and unbiased_counter rows only), so they are never marked.
ZERO_YIELD_EXEMPT_LANES = ("search_presence",)
# Routes whose series output is retired on purpose: wave8/collect bdc1545 stopped writing the tiktok/song/videos
# adoption curve as a series (it is a page sample), so the route lands no counter every day by design and would read
# as a zero-yield route for ever. A route that is dead by accident is not listed here.
ZERO_YIELD_RETIRED_ROUTES = ("tiktok/song/videos",)

ZERO_YIELD_PRIOR_SQL = (
    "WITH good AS (\n"
    "  SELECT r.run_id, MAX(r.finished_at) AS finished_at FROM `{runs}` r\n"
    "  WHERE r.stage = 'collect' AND r.status = 'ok' GROUP BY r.run_id),\n"
    "latest AS (\n"
    "  SELECT x.market, x.series, x.protocol, x.route, x.lane_class, x.calls, x.calls_ok, x.items\n"
    "  FROM `{health}` x JOIN good g ON g.run_id = x.run_id\n"
    "  WHERE x.day = DATE_SUB(@d, INTERVAL 1 DAY)\n"
    "  QUALIFY ROW_NUMBER() OVER (PARTITION BY x.market, x.series, x.protocol ORDER BY g.finished_at DESC) = 1)\n"
    "SELECT DISTINCT l.market, l.series, l.route, l.lane_class FROM latest l\n"
    "WHERE l.calls > 0 AND l.calls_ok = l.calls AND l.items = 0"
)


def zero_yield_route(route, lane_class):
    """True for a route a zero-yield day can be named on: a SocialCrawl route, not retired, not a search lane."""
    return route in PRICED and route not in ZERO_YIELD_RETIRED_ROUTES and lane_class not in ZERO_YIELD_EXEMPT_LANES


def zero_yield_prior(bq, day):
    """The (market, series, route, lane_class) keys with a stored zero-yield row on the day before day: every
    call answered and nothing landed, on a route zero_yield_route names."""
    from google.cloud import bigquery

    rows = _query(bq, ZERO_YIELD_PRIOR_SQL.format(runs=table("runs", AGENT), health=table("collection_health")),
                  [bigquery.ScalarQueryParameter("d", "DATE", day)])
    return {(r["market"], r["series"], r["route"], r["lane_class"]) for r in rows
            if zero_yield_route(r["route"], r["lane_class"])}


# A failed call's class in collection_health.invalid_reason, "calls: <class>[,<class>...]": the client's
# failure class (timeout, connection, http_429, http_5xx, http_4xx, vendor_error, ...) when the call was made,
# else a word for why it was not or what came back (the record's own status word when none is listed). Only
# lower-case words of up to 32 letters, digits and underscores are written, never a URL, a message or a body.
STATUS_FAILURES = {
    "robots_disallowed": "robots", "no_entries": "empty", "no_page": "empty", "unparsable": "unparsable",
    "not_priced": "not_priced", "not_made": "not_made", "over_share": "not_made", "over_local_cap": "not_made",
    "cap_reached": "cap", "balance_floor": "cap", "insufficient_credits": "cap", "day_changed": "day_changed",
    "skipped": "skipped", "forbidden": "forbidden", "not_in_replay": "not_in_replay", "refunded": "http_5xx",
    "error": "error"}
FAILURE_WORD = re.compile(r"[a-z][a-z0-9_]{0,31}")
CALL_FAILURES_SHOWN = 3


def call_failure(record):
    """The class of a failed call record for invalid_reason, or None for a call that came back usable."""
    if record.get("ok"):
        return None
    failure = str(record.get("failure") or "")
    if FAILURE_WORD.fullmatch(failure):
        return failure
    status = str(record.get("status") or "")
    code = re.fullmatch(r"http_(\d{3})", status)
    if code:
        return http_failure(int(code.group(1)))
    return STATUS_FAILURES.get(status) or (status if FAILURE_WORD.fullmatch(status) else "other")


def _calls_reason(failures):
    """"calls" plus the failed calls' classes, most frequent first, at most CALL_FAILURES_SHOWN of them."""
    counted = Counter(f for f in failures if f)
    if not counted:
        return "calls"
    shown = sorted(counted, key=lambda f: (-counted[f], f))[:CALL_FAILURES_SHOWN]
    return "calls: " + ",".join(shown)


def judge(*, calls, calls_ok, items, ref_items, ref_days, lane_class, units_planned, units_ok):
    """(valid, invalid_reason, k) per DATA.md 3.3, without the drift check."""
    k = (units_planned / units_ok if units_ok else None) if lane_class == "panel" else None
    if calls == 0 or calls_ok < 0.8 * calls:
        reason = "calls"
    elif ref_days >= 3 and ref_items is not None and not 0.5 * ref_items <= items <= 2 * ref_items:
        reason = "items"
    elif lane_class == "panel" and not 0.5 <= (99 if k is None else k) <= 2:
        reason = "effort"
    else:
        reason = None
    return reason is None, reason, k


def _zero_yield_day(group):
    """A health group whose calls all answered and landed neither an observation nor a counter, on a route
    zero_yield_route names."""
    first = group["first"]
    return (group["calls"] > 0 and group["calls_ok"] == group["calls"] and group["items"] == 0
            and not group["posts"] and zero_yield_route(first["route"], first["lane_class"]))


def _located(post):
    confidence = post.get("geo_confidence")
    return confidence is not None and confidence >= 0.7 and post.get("geo_source") in KNOWN_GEO


def health_rows(records, posts, refs, run_id, prior=None):
    """One collection_health row per day, market, series and protocol from the job's call records.
    refs maps each day (YYYY-MM-DD) to reference() for that day. prior maps each day to zero_yield_prior() for that
    day, the (market, series, route, lane_class) keys that were zero-yield days on the day before; a series the
    run holds a zero-yield row for on the day before counts as well."""
    located = {}
    for post in posts:
        located.setdefault(post["post_id"], _located(post))
    groups = {}
    for r in records:
        group = groups.setdefault((r["day"], r["market"], r["series"], r["protocol"]), {
            "first": r, "calls": 0, "calls_ok": 0, "units_planned": 0, "units_ok": 0, "items": 0, "posts": set(),
            "failures": []})
        group["calls"] += r["calls"]
        group["calls_ok"] += int(r["ok"])
        group["units_planned"] += r["units_planned"]
        group["units_ok"] += r["units_ok"]
        group["items"] += r["items"]
        group["posts"].update(r["post_ids"])
        group["failures"].append(call_failure(r))
    zero = {(key[0], key[1], key[2], g["first"]["route"], g["first"]["lane_class"])
            for key, g in groups.items() if _zero_yield_day(g)}
    out = []
    for (day, market, series, protocol), g in groups.items():
        first = g["first"]
        ref_items, ref_days = refs.get(day, {}).get((market, series, protocol), (None, 0))
        valid, reason, k = judge(calls=g["calls"], calls_ok=g["calls_ok"], items=g["items"], ref_items=ref_items,
                                 ref_days=ref_days, lane_class=first["lane_class"],
                                 units_planned=g["units_planned"], units_ok=g["units_ok"])
        if reason == "calls":
            reason = _calls_reason(g["failures"])
        route = (market, series, first["route"], first["lane_class"])
        if valid and _zero_yield_day(g):
            yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            if route in (prior or {}).get(day, ()) or (yesterday, *route) in zero:
                valid, reason = False, ZERO_YIELD
        share = sum(located.get(p, False) for p in g["posts"]) / len(g["posts"]) if g["posts"] else None
        out.append({
            "day": day, "market": market, "platform": first["platform"], "route": first["route"],
            "series": series, "protocol": protocol, "lane_class": first["lane_class"], "calls": g["calls"],
            "calls_ok": g["calls_ok"], "units_planned": g["units_planned"], "units_ok": g["units_ok"],
            "items": g["items"], "ref_items": ref_items, "ref_days": ref_days, "k": k, "valid": valid,
            "invalid_reason": reason, "located_share": share, "run_id": run_id,
        })
    return out


# A repair run (COLLECT_ONLY_ROUTES, core/collect/job.py) re-reads a few rank lists on a day that already has an
# ok collect run, then becomes that day's good collect run itself (v_good_runs takes the newest ok run), and
# v_collection_health_current reads only the good run's rows. So the repair appends, under its own run_id, the
# base run's rows for every key it did not read, unchanged; its own rows stand in for the base's for the keys it
# read. Every other collect table is read across all ok collect runs (post_observations, posts and creators
# with no run filter, item_counter_daily newest per key), so the base run's rows there stay as they are.
REPAIR_RUN_SQL = (
    "SELECT r.run_id, TO_JSON_STRING(r.counts) AS counts FROM `{runs}` r\n"
    "WHERE r.stage = 'collect' AND r.status = 'ok' AND r.run_date = @d\n"
    "ORDER BY r.finished_at DESC LIMIT 1"
)
REPAIR_HEALTH_SQL = (
    "SELECT " + ", ".join(f"h.{c}" for c in HEALTH_COLUMNS) + " FROM `{health}` h\n"
    "WHERE h.day = @d AND h.run_id = @run_id"
)


def repair_base(bq, day):
    """The good collect run of day as v_good_runs picks it (the newest ok one by finished_at), with its counts and
    its collection_health rows for day: {"run_id", "counts", "health"}, or None when day has no ok collect run."""
    from google.cloud import bigquery

    runs = _query(bq, REPAIR_RUN_SQL.format(runs=table("runs", AGENT)),
                  [bigquery.ScalarQueryParameter("d", "DATE", day)])
    if not runs:
        return None
    run_id, counts = runs[0]["run_id"], runs[0]["counts"]
    counts = json.loads(counts) if isinstance(counts, str) else dict(counts or {})
    rows = _query(bq, REPAIR_HEALTH_SQL.format(health=table("collection_health")),
                  [bigquery.ScalarQueryParameter("d", "DATE", day),
                   bigquery.ScalarQueryParameter("run_id", "STRING", run_id)])
    health = [{c: (r[c].isoformat() if c == "day" and isinstance(r[c], date) else r[c]) for c in HEALTH_COLUMNS}
              for r in rows]
    return {"run_id": run_id, "counts": counts if isinstance(counts, dict) else {}, "health": health}


def _health_key(row):
    return str(row["day"]), row["market"], row["series"], row["protocol"]


def carry_forward(base, own, run_id):
    """The base run's health rows for every (day, market, series, protocol) own has no row for, each unchanged
    but for run_id."""
    wrote = {_health_key(r) for r in own}
    return [dict(r, run_id=run_id) for r in base if _health_key(r) not in wrote]


# cultural_map

def cultural_map_rows(counters, items):
    """(rows, left out) for the items of counters. items maps item_id to (kind, raw, platform, label) as the
    job made the id and named it. A row is first seen on the item's earliest live counter row (a vendor
    curve's past days only when there is none; on a tie a market before GLOBAL, then by market and platform)
    and last seen on its latest, with status from aggregate.status. left out counts blocked labels
    (gdelt.blocked) and unnamed items (no label, or an id the job cannot resolve)."""
    from core.collect.gdelt import blocked
    from core.detect import aggregate
    from core.detect.items import canonical_key

    rows, left = {}, {"blocked": 0, "unnamed": 0}
    order = sorted(counters, key=lambda c: (c["source"] != "live", c["obs_date"], c["market"] == GLOBAL,
                                            c["market"], c["platform"] or ""))
    for c in order:
        item, day = c["item_id"], _day(c["obs_date"])
        if item in rows:
            if rows[item] is not None:
                rows[item]["last_seen"] = max(rows[item]["last_seen"], day)
            continue
        kind, raw, platform, label = items.get(item) or (None, None, None, None)
        if not label:
            left["unnamed"] += 1
            rows[item] = None
            continue
        if blocked(label):
            left["blocked"] += 1
            rows[item] = None
            continue
        key = canonical_key(kind, raw, platform)
        rows[item] = {"item_id": item, "kind": kind, "canonical_key": key, "label": label, "first_seen": day,
                      "first_seen_market": c["market"], "first_seen_platform": c["platform"], "last_seen": day,
                      "status": aggregate.status(kind, key)}
    return [r for r in rows.values() if r is not None], left


def merge_cultural_map(bq, rows):
    """L2's cultural_map MERGE over rows, aggregate.CHUNK rows a statement. Returns the statements run."""
    from google.cloud import bigquery

    from core.detect import aggregate, sqlrun

    sql = sqlrun.render(aggregate.cultural_map_merge_sql(), CORE, AGENT)
    for start in range(0, len(rows), aggregate.CHUNK):
        param = bigquery.ArrayQueryParameter("items", "STRUCT", [
            bigquery.StructQueryParameter(None, *[bigquery.ScalarQueryParameter(f, t, row[f])
                                                  for f, t in aggregate.ITEM_FIELDS])
            for row in rows[start:start + aggregate.CHUNK]])
        _query(bq, sql, [param])
    return math.ceil(len(rows) / aggregate.CHUNK)


# post_items

def insert_post_items(bq, rows):
    """L2's post_items INSERT (core.detect.aggregate.POST_ITEMS_INSERT_SQL) over rows, aggregate.CHUNK rows a
    statement: it appends only the post and item pairs not already in post_items, so a post the pulse linked is
    not linked again by a later pulse or by detect's aggregate step. Returns the rows sent."""
    from google.cloud import bigquery

    from core.detect import aggregate, sqlrun

    sql = sqlrun.render(aggregate.POST_ITEMS_INSERT_SQL, CORE, AGENT)
    for start in range(0, len(rows), aggregate.CHUNK):
        param = bigquery.ArrayQueryParameter("rows", "STRUCT", [
            bigquery.StructQueryParameter(None, *[bigquery.ScalarQueryParameter(f, t, row[f])
                                                  for f, t in aggregate.POST_ITEM_FIELDS])
            for row in rows[start:start + aggregate.CHUNK]])
        _query(bq, sql, [param])
    return len(rows)


# Watches

KNOWN_POSTS_SQL = "SELECT p.post_id FROM `{table}` p WHERE p.post_id IN UNNEST(@ids)"


def known_posts(bq, ids):
    from google.cloud import bigquery

    ids = sorted(set(ids))
    if not ids:
        return set()
    rows = _query(bq, KNOWN_POSTS_SQL.format(table=table("posts")),
                  [bigquery.ArrayQueryParameter("ids", "STRING", ids)])
    return {row["post_id"] for row in rows}


def read_profile_countries(bq, keys, *, timeout=None):
    import time
    from datetime import datetime, timedelta, timezone
    from google.cloud import bigquery
    from core.collect.location_sources import COUNTRY_ROUTES, _countries
    from core.collect.socialcrawl_client import params_hash

    if not keys:
        return
    max_bytes = 64 * 1024 ** 2
    deadline = time.monotonic() + timeout if timeout is not None else None

    def query(sql, parameters):
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise TimeoutError("account country cache read budget")
        kwargs = {"timeout": remaining, "retry": None, "job_retry": None} if remaining is not None else {}
        result = bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=parameters,
                           maximum_bytes_billed=max_bytes), **kwargs)
        remaining = deadline - time.monotonic() if deadline is not None else None
        kwargs = {"timeout": max(0, remaining), "retry": None, "job_retry": None} if remaining is not None else {}
        return [dict(row) for row in result.result(**kwargs)]

    hashes = [f"{COUNTRY_ROUTES[p]}:{params_hash('GET', {'handle': h.casefold()})}" for p, h in keys]
    inventory = f"""SELECT DISTINCT DATE(fetched_at) AS day FROM `{table('raw_responses')}`
      WHERE http_status = 200 AND (
        CONCAT(route, ':', params_hash) IN UNNEST(@request_hashes)
        OR (route = 'instagram/search/reels' AND @instagram)) ORDER BY day DESC"""
    days = query(inventory, [bigquery.ArrayQueryParameter("request_hashes", "STRING", hashes),
                            bigquery.ScalarQueryParameter("instagram", "BOOL", any(p == "instagram" for p, _ in keys))])
    sql = f"""WITH profiles AS (
      SELECT CASE route WHEN 'tiktok/profile' THEN 'tiktok' ELSE 'instagram' END AS platform,
        JSON_VALUE(body, '$.data.author.username') AS handle,
        route AS country_source,
        CASE route WHEN 'tiktok/profile' THEN JSON_VALUE(body, '$.data.author.location')
          ELSE JSON_VALUE(body, '$.data.author.ext.country') END AS country,
        CASE route WHEN 'tiktok/profile' THEN JSON_TYPE(JSON_QUERY(body, '$.data.author.location'))
          ELSE JSON_TYPE(JSON_QUERY(body, '$.data.author.ext.country')) END AS country_type,
        fetched_at, run_id, params_hash, JSON_VALUE(body, '$.request_id') AS request_id
      FROM `{table('raw_responses')}`
      WHERE route IN ('tiktok/profile', 'instagram/profile/about') AND http_status = 200
        AND fetched_at >= @start AND fetched_at < @end
        AND JSON_VALUE(body, '$.success') = 'true'
        AND CONCAT(route, ':', params_hash, ':', LOWER(JSON_VALUE(body, '$.data.author.username')))
          IN UNNEST(@profile_requests)
      UNION ALL
      SELECT 'instagram', JSON_VALUE(item, '$.post.author.username'), route,
        JSON_VALUE(item, '$.post.ext.author_country'), JSON_TYPE(JSON_QUERY(item, '$.post.ext.author_country')),
        fetched_at, run_id, params_hash,
        JSON_VALUE(body, '$.request_id')
      FROM `{table('raw_responses')}`, UNNEST(JSON_QUERY_ARRAY(body, '$.data.items')) AS item
      WHERE route = 'instagram/search/reels' AND http_status = 200
        AND fetched_at >= @start AND fetched_at < @end
        AND JSON_VALUE(body, '$.success') = 'true'
        AND JSON_VALUE(item, '$.post.ext.author_country') IS NOT NULL
    ), scoped AS (
      SELECT *, CASE WHEN platform = 'tiktok' THEN
          REGEXP_CONTAINS(TRIM(country), r'^[A-Za-z]{{2}}$') AND UPPER(TRIM(country)) IN UNNEST(@codes)
        ELSE UPPER(TRIM(country)) IN UNNEST(@codes)
          OR LOWER(TRIM(REGEXP_REPLACE(country, r'\\s+', ' '))) IN UNNEST(@names)
        END AS recognised
      FROM profiles WHERE CONCAT(platform, ':', LOWER(handle)) IN UNNEST(@profiles)
    ) SELECT * FROM scoped
      QUALIFY ROW_NUMBER() OVER (PARTITION BY platform, LOWER(handle)
        ORDER BY IFNULL(recognised, FALSE) DESC, fetched_at DESC) = 1"""
    codes, names = _countries()
    needed = {(p, h.casefold()) for p, h in keys}
    for day in days:
        if not needed:
            break
        start = datetime.combine(day["day"], datetime.min.time(), timezone.utc)
        parameters = [
            bigquery.ArrayQueryParameter("profiles", "STRING", [f"{p}:{h}" for p, h in sorted(needed)]),
            bigquery.ArrayQueryParameter("profile_requests", "STRING", [
                f"{COUNTRY_ROUTES[p]}:{params_hash('GET', {'handle': h})}:{h}" for p, h in sorted(needed)]),
            bigquery.ArrayQueryParameter("codes", "STRING", sorted(codes)),
            bigquery.ArrayQueryParameter("names", "STRING", sorted(names)),
            bigquery.ScalarQueryParameter("start", "TIMESTAMP", start),
            bigquery.ScalarQueryParameter("end", "TIMESTAMP", start + timedelta(days=1))]
        for row in query(sql, parameters):
            if row.get("recognised"):
                needed.discard((row["platform"], row["handle"].casefold()))
            yield row


def read_watches(bq, markets):
    """The active current watches in any of markets or in all markets, oldest first, target parsed; an item
    target carries its cultural_map item_kind, item_key and item_label (None when the item is not there)."""
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    sql = WATCHES_SQL.format(watches=table("v_watches_current", AGENT), cultural_map=table("cultural_map"))
    try:
        rows = _query(bq, sql, [bigquery.ArrayQueryParameter("markets", "STRING", [*markets, ALL_MARKETS])])
    except NotFound:
        log.warning("intelligence_42_agent.v_watches_current does not exist yet; no watches")
        return []
    out, seen = [], set()
    for r in rows:
        if r["watch_id"] in seen:
            continue
        seen.add(r["watch_id"])
        target = r["target"]
        out.append({"watch_id": r["watch_id"], "market": r["market"], "status": r["status"], "label": r["label"],
                    "target": json.loads(target) if isinstance(target, str) else target or {},
                    "item_kind": r["item_kind"], "item_key": r["item_key"], "item_label": r["item_label"]})
    return out


def write_telegram_raw(bq, rows):
    """The Telegram phase's raw_responses rows (core/collect/telegram_collect.py), on their own append so the public
    feeds' counts stay theirs. {} when there are none, as with core/public_feeds/telegram.py TELEGRAM_READY off; a
    failed append is logged and counted, and never fails the run."""
    if not rows:
        return {}
    try:
        return {"telegram_raw_rows_written": append(bq, "raw_responses", rows), "telegram_raw_error": None}
    except Exception as exc:
        log.error("telegram_raw_write_failed")
        return {"telegram_raw_rows_written": 0, "telegram_raw_error": f"{type(exc).__name__}: {exc}"[:500]}


def write_run(bq, run, run_id, carry=None):
    """Every write of one collect run. Returns the row counts. carry holds a repair run's base health rows
    (repair_base): the ones for keys the repair wrote no row for are appended again under run_id. The reads the health
    rows need (the reference and the zero-yield prior of each day) come first: a read that failed after an append would
    leave the day with observations and counters and no health rows, and the rerun would append them again."""
    refs = {day: reference(bq, date.fromisoformat(day)) for day in sorted({r["day"] for r in run.records})}
    prior = {day: zero_yield_prior(bq, date.fromisoformat(day)) for day in refs}
    public_posts = [post for post in run.posts if post.get("source_regime") == "public_feed"]
    legacy_posts = [post for post in run.posts if post.get("source_regime") != "public_feed"]
    statements = merge_posts(bq, legacy_posts)
    deduped_public_posts = dedupe_public_feed_posts(public_posts)
    public_statements = _merge_public_feed(bq, deduped_public_posts, BATCH, PUBLIC_FEED_MERGE_BUDGET_BYTES)
    counters = run.counters + appearances(run.counters, run_id) + deltas(bq, run.counters, run_id)
    observations = append(bq, "post_observations", run.observations)
    counter_rows = append(bq, "item_counter_daily", counters)
    own = health_rows(run.records, run.posts, refs, run_id, prior)
    carried = carry_forward(carry, own, run_id) if carry is not None else []
    health = append(bq, "collection_health", own + carried)
    mapped, left = cultural_map_rows(run.counters, run.items)
    merge_cultural_map(bq, mapped)
    creators, error = write_creators(bq, run.creators)
    raw_rows = getattr(run, "public_feed_raw_rows", ()) or ()
    raw_count, raw_error = 0, None
    if raw_rows:
        try:
            raw_count = append(bq, "raw_responses", raw_rows)
        except Exception as exc:
            log.error("public_feed_raw_write_failed")
            raw_error = f"{type(exc).__name__}: {exc}"[:500]
    search_rows, search_held = split_search_rows(getattr(run, "search_rows", ()) or ())
    search_count, search_error = _append_search_rows(bq, search_rows)
    telegram = write_telegram_raw(bq, getattr(run, "telegram_raw_rows", ()) or ())
    return {**telegram, "merge_statements": statements + public_statements,
            "public_feed_posts": len(deduped_public_posts),
            "public_feed_merge_statements": public_statements,
            "public_feed_raw_rows": raw_count, "public_feed_raw_error": raw_error,
            "observations": observations, "counters": counter_rows,
            "health": health, "health_own": len(own), "health_carried": len(carried),
            "cultural_map": len(mapped), "labels_blocked": left["blocked"],
            "labels_missing": left["unnamed"], "creators_written": creators, "creators_error": error,
            "google_search_signals": search_count, "google_search_signals_error": search_error,
            "google_search_signals_blocked": len(search_held)}


def split_search_rows(rows):
    """(rows that may be written, rows blocked): rule 1 (gdelt.blocked) applies to every Google search term here,
    whichever phase read it, because the table feeds the Today strip and Ask."""
    from core.collect.gdelt import blocked

    kept, held = [], []
    for row in rows:
        (held if blocked(row.get("term") or "") else kept).append(row)
    return kept, held


def write_search_signals(bq, rows):
    """(rows appended, error) for google_search_signals: Google search interest rows, kept apart from posts.
    A term that fails rule 1 is dropped and counted in the log only. A failing append (the table not yet
    created, say) is logged and returned, never raised."""
    kept, held = split_search_rows(rows)
    if held:
        log.warning("google_search_signals: %d rule 1 terms not written", len(held))
    return _append_search_rows(bq, kept)


def _append_search_rows(bq, rows):
    if not rows:
        return 0, None
    try:
        return append(bq, "google_search_signals", rows), None
    except Exception as exc:
        log.exception("google_search_signals append failed; the run's other writes stand")
        return 0, f"{type(exc).__name__}: {exc}"[:500]


def write_creators(bq, rows):
    """(creators written, error) under CREATORS_WRITE, last and on its own: a failing creators MERGE is logged
    and returned, never raised, so it costs no other write."""
    if not CREATORS_WRITE:
        return 0, None
    try:
        merge_creators(bq, rows)
    except Exception as exc:
        log.exception("creators MERGE failed; the run's other writes stand")
        return 0, f"{type(exc).__name__}: {exc}"[:500]
    return len(dedupe_creators(rows)), None
