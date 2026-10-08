"""Read-only recall check: did 42 catch the moments on a colleague's weekly cultural-moments sheet?

    py -3.13 ops/runners/benchmark_moments.py ops/runners/benchmark/moments-2026-09-28.csv [--obs-end YYYY-MM-DD]

The moments file is a CSV with columns id, date (YYYY-MM-DD), market (ZA, NG or KE), moment, any_re and and_re.
A post, topic, news entity or search term matches a moment when it contains any_re and, if and_re is not empty,
also and_re (case-insensitive regular expressions, RE2 syntax).

For each moment it reads staging BigQuery and reports, in this order of how far the moment got:
  posts      posts dated from 2 days before to 3 days after the moment that match, in any market and in the
             moment's market (collected for that market), with distinct creators, posts located in the market
             (geo_confidence 0.7 or more, the G6 rule), posts from the market's own feeds, news posts, lanes and
             platforms, plus up to 3 example URLs from the market's collection
  topics     cultural_map items (any market) seen since 2 days before the moment whose label, key or aliases
             match any_re, and that market's clusters from the day
             before to 4 days after whose label or keywords match
  brief      Today cards and held-back items from the market's briefs dated the moment's day to 3 days after,
             whose headline matches, whose item is one of the market's matching clusters (the item's own label or
             keywords, read from the clusters table), or whose reviewed retained identity matches, with the match
             source and hold reason; incidental post matches are separate diagnostics and never establish coverage
  outside    Google search signals and GDELT news entities for the market in the window
and a verdict: ON TODAY, HELD (reason), TOPIC ONLY, POSTS ONLY, OTHER MARKET ONLY, SEARCH OR NEWS ONLY, or NOT
COLLECTED.

Safety: SELECT statements only, as the caller's application default credentials, in ogilvy-trends-v2, with
maximum_bytes_billed of 3 GB per query. It never calls the bq CLI and writes nothing to BigQuery. The result is
saved as a new file ~/dev/42-readback/moments-benchmark-<file stem>-<UTC stamp>.csv, opened in exclusive mode so
nothing is overwritten, with a -params.json beside it that records the cutoff (--obs-end, else the clock), every
window, the excluded lanes, the located rule, the byte cap, the match rule and a digest of the moments file.
A read that fails leaves its columns empty and marks its moments unconfirmed where it could have changed the
verdict; it is never counted as zero.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

PROJECT = "ogilvy-trends-v2"
CORE, AGENT = f"{PROJECT}.intelligence_42_core", f"{PROJECT}.intelligence_42_agent"
MAX_BYTES = 3 * 1024 ** 3
MARKETS = ("ZA", "NG", "KE")
LOCATED_FLOOR = 8  # TRUST.md G6: under 8 located posts the market is unconfirmed
LOCATED_MIN_CONFIDENCE = 0.7
# Every window and exclusion below is built into the SQL that runs and recorded in the params file saved beside the
# result, from these same names, so the record cannot drift from the query.
POST_BEFORE, POST_AFTER = 2, 3  # posts published this many days before and after the moment's date
BRIEF_AFTER = 3  # briefs dated the moment's day to this many days after
CLUSTER_BEFORE, CLUSTER_AFTER = 1, 4
SEARCH_BEFORE, SEARCH_AFTER = 1, 3
GDELT_BEFORE, GDELT_AFTER = 1, 3
MAP_BEFORE = 2  # cultural_map items last seen on or after the moment's date minus this; no market filter (RC187)
OBS_AFTER = 4  # sightings and briefs are read to this many days past the last post day, unless --obs-end is given
EXCLUDED_LANES = ("placebo", "agent_live")  # the SQL lists them in this order
EXCLUDED_LANE_CLASSES = ("legacy",)
LANE_LIST = ", ".join(f"'{lane}'" for lane in EXCLUDED_LANES)
LANE_CLASS_LIST = ", ".join(f"'{lane_class}'" for lane_class in EXCLUDED_LANE_CLASSES)
MATCH_RULE = ("a post matches when its text, transcript, hashtags and enrichment text contain any_re and, when set, "
              "and_re (case-insensitive RE2); a Today card or held item matches when its title matches both "
              "patterns (headline), when its item id is one of the market's clusters whose own label or keywords "
              "match both patterns (cluster identity), or when a reviewed retained item names it (retained item); "
              "incidental post matches in an item's evidence are diagnostics and never coverage")
# The highest verdict each read could have produced had it not failed, against VERDICT_LEVEL below.
READ_CEILING = {"posts": 3, "map": 4, "clusters": 4, "briefs": 6, "search": 1, "gdelt": 1}
VERDICT_LEVEL = {"NOT COLLECTED": 0, "SEARCH OR NEWS ONLY": 1, "OTHER MARKET ONLY": 2, "POSTS ONLY": 3,
                 "TOPIC ONLY": 4, "HELD": 5, "ON TODAY": 6}
POST_PLATFORMS = {"facebook", "instagram", "news", "reddit", "tiktok", "twitter", "x", "youtube"}
# Reviewed BBNaija hold, retained as "temi, teminators, nkem" on 5 October 2026.
# The key binds all six CSV definition fields, so reused ids cannot inherit this identity.
RETAINED_BRIEF_ITEMS = {
    ("NG1", "2026-10-04", "NG", "BBNaija season finale and N90M prize",
     "bbnaija|big ?brother ?naija|#bbn|bbn ?season", ""): {
        ("2026-10-05", "held", "1b6715ab16faf660a2c440ed13d03d35d6152838b6e83843b2934a06f21684db")}
}

MOMENTS = "WITH m AS (SELECT * FROM UNNEST(@moments))\n"

POSTS_SQL = MOMENTS + f"""
, p AS (
  SELECT p.post_id, p.platform, p.creator_id, p.post_date, p.geo_market, p.geo_confidence, p.url,
    IFNULL(p.engagement, 0) AS engagement,
    LOWER(CONCAT(IFNULL(p.text, ''), ' ', IFNULL(p.transcript, ''), ' ',
      ARRAY_TO_STRING(IFNULL(p.hashtags, []), ' '), ' ', IFNULL(e.screen_text, ''), ' ',
      IFNULL(e.video_notes, ''))) AS t
  FROM `{CORE}.posts` p
  LEFT JOIN (SELECT post_id, ANY_VALUE(screen_text) AS screen_text, ANY_VALUE(video_notes) AS video_notes
             FROM `{CORE}.post_enrichment` GROUP BY post_id) e ON e.post_id = p.post_id
  WHERE p.post_date BETWEEN @start AND @end)
, hit AS (
  SELECT m.id, m.market, p.post_id, p.platform, p.creator_id, p.geo_market, p.geo_confidence, p.url, p.engagement
  FROM m JOIN p
    ON p.post_date BETWEEN DATE_SUB(m.d, INTERVAL {POST_BEFORE} DAY) AND DATE_ADD(m.d, INTERVAL {POST_AFTER} DAY)
   AND REGEXP_CONTAINS(p.t, CONCAT('(?i)', m.any_re))
   AND (m.and_re = '' OR REGEXP_CONTAINS(p.t, CONCAT('(?i)', m.and_re))))
, o AS (
  SELECT DISTINCT post_id, market, lane_class, source_market
  FROM `{CORE}.post_observations`
  WHERE observed_date BETWEEN @start AND @obs_end AND lane_class NOT IN ({LANE_CLASS_LIST})
    AND IFNULL(lane, '') NOT IN ({LANE_LIST}))
, j AS (
  SELECT h.*, o.lane_class, o.source_market, (o.market = h.market) AS mine
  FROM hit h LEFT JOIN o ON o.post_id = h.post_id AND o.market = h.market)
SELECT id,
  COUNT(DISTINCT post_id) AS posts_all,
  COUNT(DISTINCT IF(mine, post_id, NULL)) AS posts_market,
  COUNT(DISTINCT IF(mine, creator_id, NULL)) AS creators_market,
  COUNT(DISTINCT IF(mine AND geo_market = market AND geo_confidence >= {LOCATED_MIN_CONFIDENCE}, post_id, NULL)) AS located,
  COUNT(DISTINCT IF(mine AND source_market = market, post_id, NULL)) AS own_feed,
  COUNT(DISTINCT IF(mine AND platform = 'news', post_id, NULL)) AS news_posts,
  STRING_AGG(DISTINCT IF(mine, lane_class, NULL), ' ') AS lanes,
  STRING_AGG(DISTINCT IF(mine, platform, NULL), ' ') AS platforms,
  ARRAY_AGG(IF(mine, url, NULL) IGNORE NULLS ORDER BY engagement DESC LIMIT 3) AS examples
FROM j GROUP BY id
"""

MAP_SQL = MOMENTS + f"""
SELECT m.id, COUNT(*) AS n,
  ARRAY_AGG(CONCAT(IFNULL(cm.label, cm.canonical_key), ' (', cm.kind, ', first seen ',
    IFNULL(CAST(cm.first_seen AS STRING), '?'), ' ', IFNULL(cm.first_seen_market, '?'), ')')
    IGNORE NULLS ORDER BY cm.first_seen DESC LIMIT 4) AS items
FROM m JOIN `{CORE}.cultural_map` cm
  ON cm.valid_to IS NULL AND (cm.last_seen IS NULL OR cm.last_seen >= DATE_SUB(m.d, INTERVAL {MAP_BEFORE} DAY))
 AND REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(cm.label, ''), ' ', IFNULL(cm.canonical_key, ''), ' ',
       ARRAY_TO_STRING(IFNULL(cm.aliases, []), ' '))), CONCAT('(?i)', m.any_re))
GROUP BY m.id
"""

CLUSTER_TEXT = """LOWER(CONCAT(IFNULL(c.label, ''), ' ', ARRAY_TO_STRING(IFNULL(c.keywords, []), ' '), ' ',
       ARRAY_TO_STRING(IFNULL(c.local_terms, []), ' ')))"""

# item_ids: the items of this market's clusters whose own label or keywords match both patterns. A brief item with
# one of these ids is the moment's topic whatever its title says (T4, 7 October 2026: the held BBNaija topic is titled
# "temi, teminators, nkem" and its keywords name BBNaija). The ids come from the clusters table, never from the brief.
CLUSTERS_SQL = MOMENTS + f"""
SELECT m.id, COUNT(DISTINCT c.cluster_id) AS n, ARRAY_AGG(DISTINCT c.label IGNORE NULLS LIMIT 4) AS labels,
  ARRAY_AGG(DISTINCT IF(m.and_re = '' OR REGEXP_CONTAINS({CLUSTER_TEXT}, CONCAT('(?i)', m.and_re)),
                        c.item_id, NULL) IGNORE NULLS) AS item_ids
FROM m JOIN `{CORE}.clusters` c
  ON c.cluster_date BETWEEN DATE_SUB(m.d, INTERVAL {CLUSTER_BEFORE} DAY) AND DATE_ADD(m.d, INTERVAL {CLUSTER_AFTER} DAY)
 AND UPPER(c.market) = UPPER(m.market)
 AND REGEXP_CONTAINS({CLUSTER_TEXT}, CONCAT('(?i)', m.any_re))
WHERE c.cluster_date BETWEEN @start AND @obs_end
GROUP BY m.id
"""

SEARCH_SQL = MOMENTS + f"""
SELECT m.id, COUNT(DISTINCT s.term) AS n, ARRAY_AGG(DISTINCT s.term LIMIT 4) AS terms
FROM m JOIN `{CORE}.google_search_signals` s
  ON DATE(s.fetched_at) BETWEEN DATE_SUB(m.d, INTERVAL {SEARCH_BEFORE} DAY) AND DATE_ADD(m.d, INTERVAL {SEARCH_AFTER} DAY)
 AND s.market = m.market AND REGEXP_CONTAINS(LOWER(s.term), CONCAT('(?i)', m.any_re))
WHERE DATE(s.fetched_at) BETWEEN @start AND @obs_end
GROUP BY m.id
"""

GDELT_SQL = MOMENTS + f"""
SELECT m.id, SUM(g.mentions) AS mentions, ARRAY_AGG(DISTINCT g.entity LIMIT 4) AS entities
FROM m JOIN `{CORE}.gdelt_daily` g
  ON g.day BETWEEN DATE_SUB(m.d, INTERVAL {GDELT_BEFORE} DAY) AND DATE_ADD(m.d, INTERVAL {GDELT_AFTER} DAY)
 AND g.market = m.market AND REGEXP_CONTAINS(LOWER(g.entity), CONCAT('(?i)', m.any_re))
WHERE g.day BETWEEN @start AND @obs_end
GROUP BY m.id
"""

BRIEFS_SQL = f"""
SELECT brief_date, market, run_id, published_at, status, TO_JSON_STRING(payload) AS payload
FROM `{AGENT}.briefs` WHERE brief_date BETWEEN @start AND @obs_end
"""


def load_moments(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        m = {k: (r.get(k) or "").strip() for k in ("id", "date", "market", "moment", "any_re", "and_re")}
        if m["market"] not in MARKETS or not m["any_re"]:
            raise SystemExit(f"bad moment row: {r}")
        m["d"] = date.fromisoformat(m["date"])
        re.compile(m["any_re"])
        if m["and_re"]:
            re.compile(m["and_re"])
        definition = tuple(m[k] for k in ("id", "date", "market", "moment", "any_re", "and_re"))
        m["retained_items"] = RETAINED_BRIEF_ITEMS.get(definition, set())
        out.append(m)
    return out


def latest_briefs(rows):
    """One payload per (brief_date, market): the latest published row, else the latest row by run_id."""
    best = {}
    for r in rows:
        key = (str(r["brief_date"]), r["market"])
        rank = (r.get("published_at") is not None, str(r.get("published_at") or ""), str(r.get("run_id") or ""))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    out = {}
    for key, (_, r) in best.items():
        try:
            payload = json.loads(r["payload"]) if r.get("payload") else {}
        except ValueError:
            payload = {}
        out[key] = payload if isinstance(payload, dict) else {}
    return out


def brief_hits(moment, briefs, evidence_matches=None):
    """Headline or reviewed identity coverage, with incidental post matches kept as diagnostics only."""
    pat = re.compile(moment["any_re"], re.IGNORECASE)
    required = re.compile(moment["and_re"], re.IGNORECASE) if moment.get("and_re") else None
    cards, held = {}, {}
    diagnostics = {}
    retained = moment.get("retained_items") or set()
    identity_items = moment.get("identity_items") or set()

    def matches(text):
        return pat.search(text) and (required is None or required.search(text))

    for k in range(BRIEF_AFTER + 1):
        day = (moment["d"] + timedelta(days=k)).isoformat()
        payload = briefs.get((day, moment["market"]))
        if not payload:
            continue
        block = payload.get("held_back") if isinstance(payload.get("held_back"), dict) else {}
        for kind, items in (("card", [*(payload.get("cards") or []), *(payload.get("more") or [])]),
                            ("held", block.get("items") or [])):
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "")
                headline = matches(title)
                evidence = list(dict.fromkeys(str(e["id"]) for e in item.get("evidence") or []
                    if isinstance(e, dict) and e.get("id") and e.get("platform") in POST_PLATFORMS
                    and matches(" ".join(str(e.get(field) or "") for field in ("text", "quote_text")))))
                identity = item.get("item_id") or (day, kind, index)
                mapped = (day, kind, item.get("item_id")) in retained
                by_cluster = bool(item.get("item_id")) and item.get("item_id") in identity_items
                if not headline and not mapped and not by_cluster:
                    if evidence:
                        diagnostics.setdefault((kind, identity),
                            f"{day} {title} [{kind}, evidence-only match: {', '.join(evidence)}; not coverage]")
                    continue
                source = ("headline match" if headline else "retained item match; headline not matched" if mapped
                          else "cluster identity match; headline not matched")
                if evidence:
                    source += f"; post matches: {', '.join(evidence)}"
                detail = f"{day} {title} [{source}]"
                if kind == "card":
                    cards.setdefault(identity, detail)
                else:
                    held.setdefault(identity, f"{detail} [{item.get('rule') or ''} {item.get('reason') or ''}]".strip())
    if evidence_matches is not None:
        evidence_matches.extend(diagnostics.values())
    return list(cards.values()), [detail for identity, detail in held.items() if identity not in cards]


def verdict(r):
    if r["cards"]:
        return "ON TODAY"
    if r["held"]:
        return "HELD"
    if r["map_n"] or r["clusters_n"]:
        return "TOPIC ONLY"
    if r["posts_market"]:
        return "POSTS ONLY"
    if r["posts_all"]:
        return "OTHER MARKET ONLY"
    if r["search_n"] or r["gdelt_mentions"]:
        return "SEARCH OR NEWS ONLY"
    return "NOT COLLECTED"


def confidence(verdict_text, failed):
    """"confirmed", or "unconfirmed: <reads>" naming each failed read that could have raised the verdict."""
    risky = sorted(name for name in failed if READ_CEILING[name] > VERDICT_LEVEL[verdict_text])
    return "unconfirmed: " + ", ".join(f"{name} read failed" for name in risky) if risky else "confirmed"


def run_parameters(moments_path, moments, start, end, obs_end, obs_end_source):
    """Everything that decides what a run counts, saved beside its result."""
    with open(moments_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return {
        "runner": "benchmark_moments.py", "moments_file": os.path.abspath(moments_path), "moments_sha256": digest,
        "moments": len(moments), "posts_start": start.isoformat(), "posts_end": end.isoformat(),
        "obs_end": obs_end.isoformat(), "obs_end_source": obs_end_source,
        "post_window_days": {"before": POST_BEFORE, "after": POST_AFTER},
        "brief_window_days": {"before": 0, "after": BRIEF_AFTER},
        "cluster_window_days": {"before": CLUSTER_BEFORE, "after": CLUSTER_AFTER},
        "search_window_days": {"before": SEARCH_BEFORE, "after": SEARCH_AFTER},
        "gdelt_window_days": {"before": GDELT_BEFORE, "after": GDELT_AFTER},
        "map_read": {"last_seen_on_or_after_days_before": MAP_BEFORE, "market_filtered": False},
        "located": {"min_confidence": LOCATED_MIN_CONFIDENCE, "market_floor_posts": LOCATED_FLOOR},
        "excluded_lanes": sorted(EXCLUDED_LANES), "excluded_lane_classes": sorted(EXCLUDED_LANE_CLASSES),
        "max_bytes_billed": MAX_BYTES, "match_rule": MATCH_RULE,
        "retained_items_in_catalog": sum(len(v) for v in RETAINED_BRIEF_ITEMS.values()),
    }


def main(argv):
    parser = argparse.ArgumentParser(prog="benchmark_moments.py", description="Read-only moments recall check.")
    parser.add_argument("moments", help="moments CSV: id, date, market, moment, any_re, and_re")
    parser.add_argument("--obs-end", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="last observation and brief day to read (default: today, capped at the last post "
                             f"day plus {OBS_AFTER} days)")
    args = parser.parse_args(argv[1:])
    from google.cloud import bigquery

    moments = load_moments(args.moments)
    start = min(m["d"] for m in moments) - timedelta(days=POST_BEFORE)
    end = max(m["d"] for m in moments) + timedelta(days=POST_AFTER)
    if args.obs_end:
        obs_end, obs_end_source = args.obs_end, "argument"
    else:
        today = datetime.now(timezone.utc).date()
        obs_end = min(end + timedelta(days=OBS_AFTER), today)
        obs_end_source = f"clock, capped at the moment window end plus {OBS_AFTER} days"
    client = bigquery.Client(project=PROJECT)
    struct = [bigquery.StructQueryParameter(
        None,
        bigquery.ScalarQueryParameter("id", "STRING", m["id"]),
        bigquery.ScalarQueryParameter("d", "DATE", m["d"]),
        bigquery.ScalarQueryParameter("market", "STRING", m["market"]),
        bigquery.ScalarQueryParameter("any_re", "STRING", m["any_re"]),
        bigquery.ScalarQueryParameter("and_re", "STRING", m["and_re"])) for m in moments]
    params = [bigquery.ArrayQueryParameter("moments", "STRUCT", struct),
              bigquery.ScalarQueryParameter("start", "DATE", start),
              bigquery.ScalarQueryParameter("end", "DATE", end),
              bigquery.ScalarQueryParameter("obs_end", "DATE", obs_end)]

    def rows(sql, name):
        if sql.lstrip().split(None, 1)[0].upper() not in ("SELECT", "WITH"):
            raise SystemExit("only SELECT statements are run")
        used = set(re.findall(r"@(\w+)", sql))
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES,
                                      query_parameters=[q for q in params if q.name in used])
        try:
            return [dict(r.items()) for r in client.query(sql, job_config=cfg).result()], None
        except Exception as exc:  # report the read that failed and carry on with the others
            return [], f"{name} read failed: {type(exc).__name__}: {str(exc)[:300]}"

    print(f"42 moments benchmark, {args.moments}, {len(moments)} moments, posts {start} to {end}, "
          f"sightings and briefs to {obs_end} ({obs_end_source}). SELECT only.")
    results, errors, failed = {}, [], set()
    for name, sql in (("posts", POSTS_SQL), ("map", MAP_SQL), ("clusters", CLUSTERS_SQL),
                      ("search", SEARCH_SQL), ("gdelt", GDELT_SQL)):
        got, err = rows(sql, name)
        if err:
            errors.append(err)
            failed.add(name)
        results[name] = {r["id"]: r for r in got}
    brief_rows, err = rows(BRIEFS_SQL, "briefs")
    if err:
        errors.append(err)
        failed.add("briefs")
    briefs = latest_briefs(brief_rows)

    def read(name, value):
        """A failed read has no value: None, never the zero or empty string it would otherwise have become."""
        return None if name in failed else value

    out = []
    for m in moments:
        p = results["posts"].get(m["id"], {})
        mp, cl = results["map"].get(m["id"], {}), results["clusters"].get(m["id"], {})
        se, gd = results["search"].get(m["id"], {}), results["gdelt"].get(m["id"], {})
        evidence_matches = []
        cards, held = brief_hits({**m, "identity_items": set(cl.get("item_ids") or [])}, briefs, evidence_matches)
        r = {"id": m["id"], "date": m["date"], "market": m["market"], "moment": m["moment"],
             "posts_all": read("posts", p.get("posts_all") or 0),
             "posts_market": read("posts", p.get("posts_market") or 0),
             "creators_market": read("posts", p.get("creators_market") or 0),
             "located": read("posts", p.get("located") or 0),
             "own_feed": read("posts", p.get("own_feed") or 0), "news_posts": read("posts", p.get("news_posts") or 0),
             "lanes": read("posts", p.get("lanes") or ""), "platforms": read("posts", p.get("platforms") or ""),
             "examples": read("posts", " ".join(dict.fromkeys(p.get("examples") or []))),
             "map_n": read("map", mp.get("n") or 0), "map_items": read("map", "; ".join(mp.get("items") or [])),
             "clusters_n": read("clusters", cl.get("n") or 0),
             "cluster_labels": read("clusters", "; ".join(cl.get("labels") or [])),
             "cards": read("briefs", "; ".join(cards)), "held": read("briefs", "; ".join(held)),
             "brief_evidence_matches": read("briefs", "; ".join(evidence_matches)),
             "search_n": read("search", se.get("n") or 0),
             "search_terms": read("search", "; ".join(se.get("terms") or [])),
             "gdelt_mentions": read("gdelt", gd.get("mentions") or 0),
             "gdelt_entities": read("gdelt", "; ".join(gd.get("entities") or []))}
        r["verdict"] = verdict(r)
        r["verdict_confidence"] = confidence(r["verdict"], failed)
        r["read_failures"] = ", ".join(sorted(failed))
        r["located_floor"] = ("unknown" if r["located"] is None else "met" if r["located"] >= LOCATED_FLOOR
                              else "under 8")
        out.append(r)

    def shown(value):
        return "?" if value is None else value

    for mk in MARKETS:
        mine = [r for r in out if r["market"] == mk]
        if not mine:
            continue
        caught = sum(r["verdict"] in ("ON TODAY", "HELD", "TOPIC ONLY") for r in mine)
        unconfirmed = sum(r["verdict_confidence"] != "confirmed" for r in mine)
        print(f"\n{mk}: {caught} of {len(mine)} matched a topic label, headline or reviewed retained identity; "
              f"{sum(r['verdict'] == 'ON TODAY' for r in mine)} matched a Today record"
              + (f"; {unconfirmed} unconfirmed because a read failed" if unconfirmed else ""))
        for r in mine:
            note = "" if r["verdict_confidence"] == "confirmed" else f" ({r['verdict_confidence']})"
            print(f"  {r['id']} {r['date']} {r['verdict']}{note}: {r['moment']}")
            print(f"     posts {shown(r['posts_market'])} in {mk} collection ({shown(r['posts_all'])} any market), "
                  f"creators {shown(r['creators_market'])}, located {shown(r['located'])} ({r['located_floor']}), "
                  f"own feeds {shown(r['own_feed'])}, news {shown(r['news_posts'])}, lanes [{shown(r['lanes'])}], "
                  f"platforms [{shown(r['platforms'])}]")
            if r["map_n"] or r["clusters_n"]:
                print(f"     topics: map {r['map_n']} ({r['map_items'][:160]}); clusters {r['clusters_n']} "
                      f"({r['cluster_labels'][:120]})")
            if r["cards"] or r["held"]:
                print(f"     brief: cards [{r['cards'][:160]}] held [{r['held'][:200]}]")
            if r["brief_evidence_matches"]:
                print(f"     brief evidence-only diagnostics (not coverage): {r['brief_evidence_matches'][:200]}")
            if r["search_n"] or r["gdelt_mentions"]:
                print(f"     outside: Google terms {r['search_n']} ({r['search_terms'][:100]}); "
                      f"GDELT mentions {r['gdelt_mentions']} ({r['gdelt_entities'][:100]})")
    for e in errors:
        print("ERROR " + e)

    folder = os.path.expanduser("~/dev/42-readback")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = os.path.splitext(os.path.basename(args.moments))[0]
    path = os.path.join(folder, f"moments-benchmark-{stem}-{stamp}.csv")
    with open(path, "x", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    record = run_parameters(args.moments, moments, start, end, obs_end, obs_end_source)
    record["run_at_utc"] = stamp
    record["read_failures"] = sorted(failed)
    params_path = path[:-len(".csv")] + "-params.json"
    with open(params_path, "x", encoding="utf-8", newline="") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"\nsaved {path}\nparameters {params_path}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
