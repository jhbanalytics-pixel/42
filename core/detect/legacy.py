"""The legacy backfill (BUILD.md 1.7, DATA.md section 2): the old engine's history as memory only.

seed_graph terms (hashtag to hashtag, slang to meme, handle to creator, music to sound; tokens and channels
skipped) and enriched_content authors (creator items) become item_daily rows with lane_class 'legacy', one
aggregate runs row per legacy date so v_item_daily_current and v_item_waves see them, and a MERGE into
cultural_map for first_seen. Legacy rows carry no series, tier or location, so they never form a baseline,
meet a floor or shorten warm-up. When both sources hold the same creator on a day in a market, the source
that saw more posts is used whole, so no post is counted twice.

Reads from the old dataset are SELECT only. Writes are load jobs appending to item_daily, streamed runs rows
and the cultural_map MERGE, which sets status by the aggregate rule (stoplisted hashtags are 'generic').
A date that already has a legacy runs row is skipped, so a re-run adds nothing; the runs rows are written
last, so an interrupted run leaves no date marked done.

Entry point: python -m core.detect.legacy, a one-off backfill as f42-brief or f42-builder.
"""

import datetime
import json
import re
import sys
from pathlib import Path

from google.cloud import bigquery

from . import aggregate, runs, sqlrun
from .items import canonical_key, item_id, items_for_post

LEGACY_SQL = Path(__file__).parent / "sql" / "legacy.sql"
PROJECT = "ogilvy-trends-v2"
LEGACY = "trends_v2_dev"
RULE_VERSION = "legacy-1"
EARLIEST = datetime.date(2000, 1, 1)
MERGE_BATCH = 5000
KINDS = {"hashtag": "hashtag", "slang": "meme", "handle": "creator", "music": "sound"}

_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)


def statements():
    """The named statements of legacy.sql, dataset placeholders left in."""
    out = {}
    for piece in sqlrun.split(LEGACY_SQL.read_text(encoding="utf-8")):
        m = _NAME.search(piece)
        out[m.group(1)] = sqlrun._strip_leading_comments(piece[m.end():])
    return out


def _item(kind, raw, platform):
    """(item_id, kind, canonical_key, label) for one legacy term, or None for a term that folds to nothing."""
    if kind == "hashtag":
        found = items_for_post({"hashtags": [raw]})
        return (found[0]["item_id"], "hashtag", found[0]["canonical_key"], found[0]["label"]) if found else None
    try:
        key = canonical_key(kind, raw, platform)
    except ValueError:
        return None
    return item_id(kind, key), kind, key, str(raw).strip()


def _add(side, iid, market, platform, posts, available_at, creators=None, engagement=None, first_post_at=None):
    per_platform = side.setdefault((market, iid), {})
    row = per_platform.get(platform)
    if row is None:
        per_platform[platform] = {"posts": posts or 0, "creators": creators, "engagement": engagement,
                                  "first_post_at": first_post_at, "available_at": available_at}
        return
    row["posts"] += posts or 0
    if engagement is not None:
        row["engagement"] = (row["engagement"] or 0) + engagement
    row["first_post_at"] = _pick(min, row["first_post_at"], first_post_at)
    row["available_at"] = _pick(max, row["available_at"], available_at)


def _pick(fn, a, b):
    return b if a is None else a if b is None else fn(a, b)


def day_rows(d, seed, handles, run_id, rule_version=RULE_VERSION):
    """item_daily rows for legacy day d from the seed and handles query rows, and the items they name."""
    sides, found = {"seed": {}, "enriched": {}}, {}
    for r in seed:
        item = _item(KINDS[r["term_type"]], r["term"], r["platform"])
        if item:
            found.setdefault(item[0], item)
            _add(sides["seed"], item[0], r["market"], r["platform"], r["posts"], r["available_at"])
    for r in handles:
        item = _item("creator", r["handle"], r["platform"])
        if item:
            found.setdefault(item[0], item)
            _add(sides["enriched"], item[0], r["market"], r["platform"], r["posts"], r["available_at"], creators=1,
                 engagement=r["engagement"], first_post_at=r["first_post_at"])
    rows = []
    for key in sorted(set(sides["seed"]) | set(sides["enriched"])):
        seen = sides["seed"].get(key, {})
        read = sides["enriched"].get(key, {})
        use = read if sum(v["posts"] for v in read.values()) >= sum(v["posts"] for v in seen.values()) else seen
        market, iid = key
        _, kind, ckey, label = found[iid]
        for platform in sorted(use):
            v = use[platform]
            rows.append({
                "metric_date": d, "market": market, "platform": platform, "item_id": iid, "lane_class": "legacy",
                "series": None, "protocol": None, "posts": v["posts"], "creators": v["creators"],
                "unflagged_creators": None, "engagement": v["engagement"], "tier_posts": None,
                "first_post_at": v["first_post_at"], "geo_known_posts": None, "local_posts": None,
                "source_regime": "legacy", "available_at": v["available_at"], "run_id": run_id,
                "rule_version": rule_version,
                "_item": {"item_id": iid, "kind": kind, "canonical_key": ckey, "label": label,
                          "status": aggregate.status(kind, ckey)}})
    return rows


def _json(value):
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return value


def _query(client, sql, params, core, agent, legacy):
    return sqlrun.query(client, sql.replace("{legacy}", legacy), params, core=core, agent=agent)


def run_legacy(client, *, since=None, until=datetime.date(2026, 9, 27), run_id=None,
               core=sqlrun.CORE, agent=sqlrun.AGENT, legacy=LEGACY):
    """Backfill legacy days from since (all history when None) to until, inclusive. Returns counts.
    Raises ValueError when until is on or after the first day 42 collected."""
    sql = statements()
    first = _query(client, sql["first_collect"], {}, core, agent, legacy)[0]["first_day"]
    if first is not None and until >= first:
        raise ValueError(f"until {until} is on or after the first collect day {first}")
    run_id = run_id or runs.new_run_id("legacy", until)
    bounds = {"since": since or EARLIEST, "until": until}
    dates = [r["metric_date"] for r in _query(client, sql["dates"], bounds, core, agent, legacy)]
    done = {r["run_date"] for r in _query(client, sql["done"], bounds, core, agent, legacy)}
    written, items, total = [], {}, 0
    table = None
    for d in dates:
        if d in done:
            continue
        started = runs.now()
        seed = _query(client, sql["seed"], {"d": d}, core, agent, legacy)
        handles = _query(client, sql["handles"], {"d": d}, core, agent, legacy)
        rows = day_rows(d, seed, handles, run_id)
        if not rows:
            continue
        for r in rows:
            it = r["_item"]
            if it["item_id"] not in items:
                items[it["item_id"]] = {**it, "first_seen": d, "first_seen_market": r["market"],
                                        "first_seen_platform": r["platform"], "last_seen": d}
            items[it["item_id"]]["last_seen"] = d
        if table is None:
            table = client.get_table(f"{core}.item_daily")
        config = bigquery.LoadJobConfig(schema=table.schema, source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
                                        write_disposition=bigquery.WriteDisposition.WRITE_APPEND)
        client.load_table_from_json([{k: _json(v) for k, v in r.items() if k != "_item"} for r in rows], table,
                                    job_config=config).result()
        total += len(rows)
        written.append((d, len(rows), started, runs.now()))
    batch = list(items.values())
    for i in range(0, len(batch), MERGE_BATCH):
        aggregate._run(client, sql["merge"],
                       [aggregate._struct_array("items", batch[i:i + MERGE_BATCH], aggregate.ITEM_FIELDS)],
                       core, agent)
    for d, n, started, finished in written:
        runs.append(client, run_id, "aggregate", d, "ok", started, finished, {"legacy_item_daily": n}, agent=agent)
    return {"dates": len(written), "dates_done_before": len(done), "item_daily": total, "items": len(items)}


def main(client=None, core=sqlrun.CORE, agent=sqlrun.AGENT, legacy=LEGACY):
    if client is None:
        client = bigquery.Client(project=PROJECT)
    counts = run_legacy(client, core=core, agent=agent, legacy=legacy)
    print(json.dumps({"legacy": counts}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
