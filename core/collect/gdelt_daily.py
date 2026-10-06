"""GDELT daily aggregate, rising entities and the news to social bridge, the rest of BUILD.md task 2.5.

aggregate(day) reads one GKG partition, the UTC news day, and appends per market the mentions of each
person, organisation, theme and place to gdelt_daily, with the entity columns gdelt.py reads and its market
rule, gdelt.MARKET_SQL: a document counts for a market when that country is its dominant location or a local
outlet of the market ran it. Every row carries MARKET_RULE, and every read, the presence check and the
INSERT's guard included, keeps only rows under it: rows written under the earlier any-mention rule have
market_rule NULL, are never read, and their days are written again under this rule. A day already in the
table under the rule is skipped, and the INSERT itself adds nothing if the day appears between the check
and the write. The dropped theme families and any entity holding a whole RULE_ONE word are left
out in the SQL; rising() also runs gdelt.theme_label and gdelt.blocked on every entity, so fused forms the
SQL cannot see never reach a report. backfill(day) fills the day and the 28 before it, one day at a time.

rising(day) reads gdelt_daily for the day and the 28 days before it. The baseline mean divides by the days
actually present in the table, so a day never aggregated is not read as zero, and it needs MIN_BASELINE_DAYS
of them. Score = (mentions + 1) / (baseline mean + 1), with gdelt.py's count floor and ratio; the top TOP_N
per market come back with their numbers, the query and its parameters (rule 5).

bridge(day) looks for those entities in 42's own posts seen 1 to 3 days after the news day in the same
market: the phrase as whole words in the post text, or the words joined as a hashtag. It reports posts,
creators, platforms, the first day seen and up to five evidence post ids. An entity with nothing is news
that did not cross over yet. Read only. GDELT is presence only, never evidence.

Every query is dry-run first through gdelt._checked and refused above gdelt.MAX_BYTES.

    py -3.13 -m core.collect.gdelt_daily --plan --day 2026-09-25        the SQL and parameters, no network
    py -3.13 -m core.collect.gdelt_daily --apply --day 2026-09-25       aggregate the day, then rising and bridge
    py -3.13 -m core.collect.gdelt_daily --apply --backfill --day ...   the same, filling the 28 days before too
"""

import argparse
import hashlib
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from google.cloud import bigquery

from core.collect import gdelt

DATASET = f"{gdelt.PROJECT}.intelligence_42_core"
DAILY_TABLE = f"{DATASET}.gdelt_daily"
POSTS_TABLE = f"{DATASET}.posts"
OBS_TABLE = f"{DATASET}.post_observations"
BASELINE_DAYS = 28
MIN_BASELINE_DAYS = 14
TOP_N = 10
BRIDGE_DAYS = (1, 3)
EVIDENCE_IDS = 5
SETTLED = time(1)
ENTITY_STRUCT = "ARRAY<STRUCT<market STRING, entity_kind STRING, phrase STRING, tag STRING>>"
MARKET_RULE = "dominant_or_outlet_v2"

_MARKET_CASE = " ".join(f"WHEN '{fips}' THEN '{market}'" for market, fips in gdelt.MARKETS.items())

AGGREGATE_SQL = f"""INSERT INTO `{DAILY_TABLE}` (day, market, entity_kind, entity, mentions, computed_at, market_rule)
SELECT @day, CASE fips {_MARKET_CASE} END, e.entity_kind, e.entity, COUNT(*), CURRENT_TIMESTAMP(), @market_rule
FROM (
  SELECT Locations, Persons, Organizations, Themes, SourceCommonName
  FROM `{gdelt.SOURCE_TABLE}`
  WHERE _PARTITIONDATE = @day
) d,
{gdelt.MARKET_SQL},
UNNEST(ARRAY_CONCAT(
  ARRAY(SELECT AS STRUCT 'person' AS entity_kind, TRIM(x) AS entity FROM UNNEST(SPLIT(d.Persons, ';')) x),
  ARRAY(SELECT AS STRUCT 'organisation' AS entity_kind, TRIM(x) AS entity FROM UNNEST(SPLIT(d.Organizations, ';')) x),
  ARRAY(SELECT AS STRUCT 'theme' AS entity_kind, TRIM(x) AS entity FROM UNNEST(SPLIT(d.Themes, ';')) x),
  ARRAY(SELECT AS STRUCT 'place' AS entity_kind,
          TRIM(SPLIT(SPLIT(x, '#')[SAFE_OFFSET(1)], ',')[SAFE_OFFSET(0)]) AS entity
        FROM UNNEST(SPLIT(d.Locations, ';')) x
        WHERE SPLIT(x, '#')[SAFE_OFFSET(0)] IN ('4', '5') AND SPLIT(x, '#')[SAFE_OFFSET(2)] = fips)
)) e
WHERE e.entity != ''
  AND NOT (e.entity_kind = 'theme' AND (
    EXISTS (SELECT 1 FROM UNNEST(@dropped_themes) p WHERE STARTS_WITH(UPPER(e.entity), p))
    OR EXISTS (SELECT 1 FROM UNNEST(SPLIT(UPPER(e.entity), '_')) w WHERE w = 'RELIGION' OR STARTS_WITH(w, 'ETHNIC'))))
  AND NOT EXISTS (
    SELECT 1 FROM UNNEST(REGEXP_EXTRACT_ALL(LOWER(e.entity), r'[\\p{{L}}\\p{{N}}]+')) t WHERE t IN UNNEST(@rule_one))
  AND NOT EXISTS (
    SELECT 1 FROM `{DAILY_TABLE}` g
    WHERE g.day = @day AND g.market_rule = @market_rule)
GROUP BY 2, 3, 4
"""

PRESENT_SQL = f"""SELECT DISTINCT g.day
FROM `{DAILY_TABLE}` g
WHERE g.day BETWEEN @start AND @end AND g.market_rule = @market_rule
ORDER BY g.day
"""

RISING_SQL = f"""WITH w AS (
  SELECT g.day, g.market, g.entity_kind, g.entity, g.mentions
  FROM `{DAILY_TABLE}` g
  WHERE g.day BETWEEN DATE_SUB(@day, INTERVAL {BASELINE_DAYS} DAY) AND @day
    AND g.market_rule = @market_rule),
b AS (SELECT COUNT(DISTINCT w.day) baseline_days FROM w WHERE w.day < @day)
SELECT w.market, w.entity_kind, w.entity,
  SUM(IF(w.day = @day, w.mentions, 0)) today_mentions,
  SUM(IF(w.day < @day, w.mentions, 0)) baseline_mentions,
  ANY_VALUE(b.baseline_days) baseline_days
FROM w CROSS JOIN b
GROUP BY w.market, w.entity_kind, w.entity
HAVING today_mentions >= @min_count
ORDER BY w.market, w.entity_kind, w.entity
"""

BRIDGE_SQL = f"""WITH e AS (
  SELECT x.market, x.entity_kind, x.phrase, x.tag FROM UNNEST(@entities) x),
o AS (
  SELECT po.market, po.post_id, MIN(po.observed_date) first_day
  FROM `{OBS_TABLE}` po
  WHERE po.observed_date BETWEEN DATE_ADD(@day, INTERVAL {BRIDGE_DAYS[0]} DAY) AND DATE_ADD(@day, INTERVAL {BRIDGE_DAYS[1]} DAY)
    AND po.lane_class != 'legacy'
  GROUP BY po.market, po.post_id),
m AS (
  SELECT o.market, o.post_id, o.first_day, ps.platform, ps.creator_id,
    CONCAT(' ', TRIM(REGEXP_REPLACE(LOWER(IFNULL(ps.text, '')), r'[^\\p{{L}}\\p{{N}}]+', ' ')), ' ') words,
    ARRAY(SELECT REGEXP_REPLACE(LOWER(h), r'[^\\p{{L}}\\p{{N}}]+', '') FROM UNNEST(ps.hashtags) h) tags
  FROM o JOIN `{POSTS_TABLE}` ps ON ps.post_id = o.post_id
  WHERE ps.post_date BETWEEN @day AND DATE_ADD(@day, INTERVAL {BRIDGE_DAYS[1]} DAY)),
hits AS (
  SELECT e.market, e.entity_kind, e.phrase, m.post_id, m.first_day, m.platform, m.creator_id
  FROM e JOIN m ON m.market = e.market
  WHERE STRPOS(m.words, CONCAT(' ', e.phrase, ' ')) > 0 OR e.tag IN UNNEST(m.tags))
SELECT e.market, e.entity_kind, e.phrase,
  COUNT(DISTINCT h.post_id) posts, COUNT(DISTINCT h.creator_id) creators, COUNT(DISTINCT h.platform) platforms,
  MIN(h.first_day) first_day,
  ARRAY_AGG(DISTINCT h.post_id IGNORE NULLS ORDER BY h.post_id LIMIT {EVIDENCE_IDS}) evidence_post_ids
FROM e LEFT JOIN hits h ON h.market = e.market AND h.entity_kind = e.entity_kind AND h.phrase = e.phrase
GROUP BY e.market, e.entity_kind, e.phrase
ORDER BY e.market, e.entity_kind, e.phrase
"""


class PartialDay(Exception):
    """The UTC news day has not settled yet, so its GKG partition is still filling."""


def _utc_now():
    return datetime.now(timezone.utc)


def aggregate_params(day):
    return {"day": ("DATE", day), "dropped_themes": ("ARRAY<STRING>", list(gdelt.DROPPED_THEMES)),
            "rule_one": ("ARRAY<STRING>", sorted(gdelt.RULE_ONE)), "market_rule": ("STRING", MARKET_RULE)}


def present_params(start, end):
    return {"start": ("DATE", start), "end": ("DATE", end), "market_rule": ("STRING", MARKET_RULE)}


def rising_params(day):
    return {"day": ("DATE", day), "min_count": ("INT64", gdelt.MIN_COUNT), "market_rule": ("STRING", MARKET_RULE)}


def _bq(params):
    out = []
    for name, (kind, value) in params.items():
        if kind == ENTITY_STRUCT:
            out.append(bigquery.ArrayQueryParameter(name, "STRUCT", [
                bigquery.StructQueryParameter(None, *(bigquery.ScalarQueryParameter(k, "STRING", v)
                                                      for k, v in entity.items()))
                for entity in value]))
        elif kind == "ARRAY<STRING>":
            out.append(bigquery.ArrayQueryParameter(name, "STRING", value))
        else:
            out.append(bigquery.ScalarQueryParameter(name, kind, value))
    return out


def _run(client, sql, params):
    return list(gdelt._checked(client, sql, _bq(params)).result())


def _ready(day, now):
    settled = datetime.combine(day + timedelta(days=1), SETTLED, tzinfo=timezone.utc)
    if (now or _utc_now()) < settled:
        raise PartialDay(f"GKG day {day.isoformat()} is read only after {settled.isoformat()}")


def _present(client, start, end):
    return {r["day"] for r in _run(client, PRESENT_SQL, present_params(start, end))}


def _insert(client, day):
    job = gdelt._checked(client, AGGREGATE_SQL, _bq(aggregate_params(day)))
    job.result()
    return {"day": day, "status": "written", "rows": job.num_dml_affected_rows or 0}


def aggregate(client, day, now=None):
    """Append one news day's per-market mentions to gdelt_daily, unless the day is already there."""
    _ready(day, now)
    if day in _present(client, day, day):
        return {"day": day, "status": "present", "rows": 0}
    return _insert(client, day)


def backfill(client, day, now=None):
    """aggregate() for the day and the BASELINE_DAYS before it, oldest first, skipping days present."""
    _ready(day, now)
    days = [day - timedelta(days=i) for i in range(BASELINE_DAYS, -1, -1)]
    present = _present(client, days[0], day)
    return [{"day": d, "status": "present", "rows": 0} if d in present else _insert(client, d) for d in days]


def phrase(label):
    return " ".join(t for t in gdelt.WORD_SPLIT.split(str(label).casefold()) if t)


def tag(label):
    return phrase(label).replace(" ", "")


def rank(rows, top_n=TOP_N):
    """Per market, the top_n rising entities, best first, after the theme families and rule 1."""
    found = {}
    for r in rows:
        if r["baseline_days"] < MIN_BASELINE_DAYS:
            continue
        raw = r["entity"]
        label = gdelt.theme_label(raw) if r["entity_kind"] == "theme" else (raw or "").strip()
        if not label or gdelt.blocked(raw) or gdelt.blocked(label) or not phrase(label):
            continue
        key = (r["market"], r["entity_kind"], phrase(label))
        entry = found.setdefault(key, {
            "market": r["market"], "entity_kind": r["entity_kind"], "label": label, "phrase": key[2],
            "tag": tag(label), "entities": [], "today": 0, "baseline_mentions": 0,
            "baseline_days": r["baseline_days"]})
        entry["entities"].append(raw)
        entry["today"] += r["today_mentions"]
        entry["baseline_mentions"] += r["baseline_mentions"]
    ranked = defaultdict(list)
    for entry in found.values():
        entry["entities"].sort()
        mean = entry["baseline_mentions"] / entry["baseline_days"]
        score = (entry["today"] + 1) / (mean + 1)
        if entry["today"] >= gdelt.MIN_COUNT and score >= gdelt.MIN_RATIO:
            ranked[entry["market"]].append({**entry, "baseline_mean": mean, "score": score})
    for entries in ranked.values():
        entries.sort(key=lambda e: (-e["score"], -e["today"], e["label"].casefold(), e["entity_kind"]))
        del entries[top_n:]
    return dict(ranked)


def rising(client, day):
    params = rising_params(day)
    rows = _run(client, RISING_SQL, params)
    return {"day": day, "baseline_days": rows[0]["baseline_days"] if rows else 0, "query": RISING_SQL,
            "params": params, "market_rule": MARKET_RULE, "markets": rank(rows)}


def bridge_entities(found):
    return sorted(({"market": e["market"], "entity_kind": e["entity_kind"], "phrase": e["phrase"], "tag": e["tag"]}
                   for entries in found["markets"].values() for e in entries),
                  key=lambda e: (e["market"], e["entity_kind"], e["phrase"]))


def bridge(client, day, found=None):
    """Social follow-through in 42's posts for each rising entity, 1 to 3 days after the news day."""
    if found is None:
        found = rising(client, day)
    entities = bridge_entities(found)
    params = {"day": ("DATE", day), "entities": (ENTITY_STRUCT, entities)}
    result = {"day": day, "query": BRIDGE_SQL, "params": params, "market_rule": found["market_rule"],
              "markets": {}}
    if not entities:
        return result
    counts = {(r["market"], r["entity_kind"], r["phrase"]): r for r in _run(client, BRIDGE_SQL, params)}
    for market, entries in found["markets"].items():
        linked = []
        for e in entries:
            r = counts.get((e["market"], e["entity_kind"], e["phrase"]))
            first = r["first_day"] if r else None
            linked.append({**e, "follow": {
                "posts": r["posts"] if r else 0, "creators": r["creators"] if r else 0,
                "platforms": r["platforms"] if r else 0,
                "lag_days": (first - day).days if first else None,
                "evidence_post_ids": list(r["evidence_post_ids"] or []) if r else []}})
        result["markets"][market] = linked
    return result


def _query_id(sql):
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]


def _show(name, kind, value):
    if isinstance(value, list):
        return f"{name}: {kind} of {len(value)} values"
    return f"{name}: {kind} {value}"


def print_plan(day):
    start = day - timedelta(days=BASELINE_DAYS)
    print(f"GDELT daily for news day {day.isoformat()} (UTC GKG partition), baseline {start.isoformat()} "
          f"to {(day - timedelta(days=1)).isoformat()}, into {DAILY_TABLE}")
    steps = (("presence check", PRESENT_SQL, present_params(day, day)),
             ("aggregate", AGGREGATE_SQL, aggregate_params(day)),
             ("rising", RISING_SQL, rising_params(day)),
             ("bridge", BRIDGE_SQL, {"day": ("DATE", day), "entities": (ENTITY_STRUCT, "the rising entities")}))
    for title, sql, params in steps:
        print(f"\n{title}, query {_query_id(sql)}:")
        print(sql)
        for name, (kind, value) in params.items():
            print(_show(name, kind, value))
    print(f"\ndry-run bytes: not estimated under --plan (no network); every live query dry-runs first and is "
          f"refused above {gdelt.MAX_BYTES:,} bytes")


def print_report(found, linked):
    params = ", ".join(f"{k}={v[1]}" for k, v in found["params"].items())
    print(f"rising {found['day'].isoformat()}: query {_query_id(RISING_SQL)} ({params}), "
          f"market rule {found['market_rule']}, "
          f"baseline {found['baseline_days']} days; bridge query {_query_id(BRIDGE_SQL)}, "
          f"posts seen {BRIDGE_DAYS[0]} to {BRIDGE_DAYS[1]} days later")
    for market in gdelt.MARKETS:
        entries = linked["markets"].get(market, [])
        print(f"{market}: {len(entries)} rising")
        for e in entries:
            f = e["follow"]
            lag = f["lag_days"]
            when = "no follow-through yet" if lag is None else f"lag {lag} day{'' if lag == 1 else 's'}"
            print(f"  {e['entity_kind']} {e['label']}: {e['today']} mentions against a mean of "
                  f"{e['baseline_mean']:.1f} over {e['baseline_days']} days, score {e['score']:.2f}; social "
                  f"{f['posts']} posts, {f['creators']} creators, {f['platforms']} platforms, {when}; "
                  f"evidence {', '.join(f['evidence_post_ids']) or 'none'}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="GDELT daily aggregate, rising entities and the social bridge.")
    parser.add_argument("--day", type=date.fromisoformat,
                        default=(_utc_now() - timedelta(days=1)).date().isoformat(), help="UTC news day")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="print the SQL and parameters; no network")
    mode.add_argument("--apply", action="store_true", help="aggregate the day, then print rising and bridge")
    parser.add_argument("--backfill", action="store_true", help="with --apply, also fill the 28 days before")
    args = parser.parse_args(argv)
    if args.plan:
        print_plan(args.day)
        return 0
    client = bigquery.Client(project=gdelt.PROJECT)
    try:
        results = backfill(client, args.day) if args.backfill else [aggregate(client, args.day)]
        for r in results:
            print(f"gdelt_daily {r['day'].isoformat()}: {r['status']}, {r['rows']} rows")
        found = rising(client, args.day)
        linked = bridge(client, args.day, found)
    except (gdelt.OverCap, PartialDay) as error:
        print(f"refused: {error}")
        return 1
    print_report(found, linked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
