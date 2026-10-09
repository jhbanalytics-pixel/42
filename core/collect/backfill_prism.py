"""One-off backfill of the prism/profiles posts and the tiktok/song counters that the 28 Sep to 7 Oct 2026 collect
runs fetched but the old parsers read as empty.

Albert's yes (batch 5, BACKFILL-PRISM): one run writes what the fixed parsers recover from the stored raw_responses of
those ten UTC fetch days into posts, post_observations, creators and item_counter_daily, under the :v2 protocol
tokens, with source_market from the call's market, and keeps receipts. Nothing else is read or written.

What it does, in order:

1. Reads raw_responses (http 200, jobs in job.BACKFILL_JOBS, the two routes, fetch days inside WINDOW). The query is
   dry-run first, refused over READ_CAP or when the dry run reports no size, then run with maximum_bytes_billed.
2. Parses each stored body with the branch's parse_with_creators, the way the job would have called it. raw_responses
   keeps params_hash, not params, so a prism/profiles call gets include=posts and since set to the day before its
   market fetch day, and a tiktok/song call gets clipId from the stored seed_key.
3. Gives each prism/profiles call the protocol and series the live job gives it. The panel's members are read from
   the body (data.results[].target) and matched against the market's lists as the job holds them: the culture desk
   (hubs.yaml), the Instagram gossip panel (local_sources) and the curated manifest. A desk or gossip call takes that
   list's own panel_protocol(..., v2) token. A curated call takes the token of the day's whole rotation, which is the
   union of the curated calls of the same stored run and market, because the job hashes the whole rotation and
   reads it in batches. A call that matches none of them is unclassified: it is written under the token of its own
   members and claims no source_market. tiktok/song takes tiktok/song?proto=v2 from the parser.
4. source_market is the call's market on a desk or curated call (the job sets it the same way). A gossip call sets it
   only on posts whose creator is on the market's curated list, as local_sources does, and an unclassified call on
   none, so the backfill never claims more locality than the live job would have.
5. Writes only INSERT ... SELECT ... WHERE NOT EXISTS, so a rerun writes nothing new and no existing row is ever
   changed or removed. Keys: posts by post_id; creators by (platform, creator_id); post_observations by (post_id,
   observed_at, route, market, protocol); item_counter_daily by (obs_date, market, item_id, series, protocol, unit,
   observed_at). The run id is not in any key. Every statement is dry-run first and run with maximum_bytes_billed.
   Every statement of the apply is dry-run before the first one is run.
6. tiktok/song gives counter rows only: a total per call and, as the live writer does, a delta from the day before
   when the same sound has a total on the previous day. Posts, observations and creators the song page might yield
   are counted and not written.

--dry-run (the default) reads, parses and reports counts, how many rows are not yet stored, and the dry-run byte
estimate of every statement. It writes no row and no file. --apply needs --run-id and --receipts-dir, writes the
per-call receipts first, then the inserts batch by batch, a statement receipt after each, and a summary.

    py -3.13 -m core.collect.backfill_prism
    py -3.13 -m core.collect.backfill_prism --apply --run-id <id> --receipts-dir <new directory>
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from core.collect import curated_creators, job, local_sources, writers
from core.collect.parse import (
    COUNTER_COLUMNS, GLOBAL, MARKETS, OBSERVATION_COLUMNS, _local_day, _utc, parse_with_creators)

WINDOW = (date(2026, 9, 28), date(2026, 10, 7))  # UTC fetch days
ROUTES = ("prism/profiles", "tiktok/song")
JOBS = job.BACKFILL_JOBS
SONG_SERIES = "counter_tiktok_sound"
GOSSIP_SERIES = "panel_ig_gossip"
READ_CAP = 512 * 1024 ** 2
WRITE_CAP = 1024 ** 3
BATCH = writers.BATCH
PRINCIPAL = "f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com"
PROJECT = "ogilvy-trends-v2"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,79}")

OBSERVATION_TYPES = {name: {"observed_at": "TIMESTAMP", "observed_date": "DATE", "pull_seq": "INT64", "rank": "INT64",
                            "views": "INT64", "likes": "INT64", "comments": "INT64", "shares": "INT64"}.get(name, "STRING")
                     for name in OBSERVATION_COLUMNS}
COUNTER_TYPES = {name: {"obs_date": "DATE", "is_board": "BOOL", "pull_seq": "INT64", "value": "FLOAT64",
                        "observed_at": "TIMESTAMP", "available_at": "TIMESTAMP"}.get(name, "STRING")
                 for name in COUNTER_COLUMNS}

# table -> (column types, key columns, date column the key is bounded by or None)
SPECS = {
    "posts": (writers.POST_TYPES, ("post_id",), None),
    "creators": (writers.CREATOR_TYPES, ("platform", "creator_id"), None),
    "post_observations": (OBSERVATION_TYPES, ("post_id", "observed_at", "route", "market", "protocol"),
                          "observed_date"),
    "item_counter_daily": (COUNTER_TYPES, ("obs_date", "market", "item_id", "series", "protocol", "unit",
                                           "observed_at"), "obs_date"),
}

READ_SQL = (
    "SELECT r.run_id, r.job, r.market, r.route, r.lane, r.seed_key, r.fetched_at, TO_JSON_STRING(r.body) AS body\n"
    "FROM `{table}` r\n"
    "WHERE DATE(r.fetched_at) BETWEEN @since AND @until AND r.http_status = 200 AND r.body IS NOT NULL\n"
    "  AND r.job IN UNNEST(@jobs) AND r.route IN UNNEST(@routes)\n"
    "ORDER BY r.fetched_at, r.run_id, r.route, r.market, r.seed_key"
)
FORBIDDEN = re.compile(r"\b(UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|REPLACE|EXPORT)\b", re.I)


class Refused(ValueError):
    """The run is stopped before it writes anything it should not."""


def panel_protocol(members):
    return job.panel_protocol(members, job.PRISM_PROTOCOL_VERSION)


def check_window(since, until):
    """(since, until) when both are inside WINDOW and in order, else Refused."""
    if not (WINDOW[0] <= since <= until <= WINDOW[1]):
        raise Refused(f"{since.isoformat()} to {until.isoformat()} is not inside the window "
                      f"{WINDOW[0].isoformat()} to {WINDOW[1].isoformat()}")
    return since, until


def assert_insert_only(sql):
    """Refuse any statement that is not one SELECT or one INSERT, or that names a statement that changes or removes."""
    body = sql.strip().rstrip(";")
    if not re.match(r"(SELECT|INSERT INTO)\b", body, re.I) or ";" in body or FORBIDDEN.search(body):
        raise Refused("only a single SELECT or INSERT statement may run")


# Statements

def _where(table):
    types, keys, bound = SPECS[table]
    clauses = ([f"T.{bound} BETWEEN @lo AND @hi"] if bound else []) + [f"T.{k} = S.{k}" for k in keys]
    return ("WHERE NOT EXISTS (SELECT 1 FROM `{table}` T WHERE " + " AND ".join(clauses) + ")").replace(
        "{table}", writers.table(table))


def insert_sql(table):
    types = SPECS[table][0]
    select = [writers._source(c) if table == "posts" else f"S.{c}" for c in types]
    return (f"INSERT INTO `{writers.table(table)}` ({', '.join(types)})\n"
            f"SELECT {', '.join(select)} FROM (SELECT * FROM UNNEST(@rows)) S\n{_where(table)}")


def count_sql(table):
    return f"SELECT COUNT(*) AS n FROM (SELECT * FROM UNNEST(@rows)) S\n{_where(table)}"


def _params(table, rows):
    from google.cloud import bigquery

    types, _, bound = SPECS[table]
    params = [bigquery.ArrayQueryParameter("rows", "STRUCT", [writers._struct(r, types) for r in rows])]
    if bound:
        days = [writers._day(r[bound]) for r in rows]
        params += [bigquery.ScalarQueryParameter("lo", "DATE", min(days)),
                   bigquery.ScalarQueryParameter("hi", "DATE", max(days))]
    return params


def _dry(client, sql, params, cap):
    from google.cloud import bigquery

    assert_insert_only(sql)
    size = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, dry_run=True, use_query_cache=False,
        maximum_bytes_billed=cap)).total_bytes_processed
    if size is None or size > cap:
        raise Refused(f"dry run reports {size} bytes; the cap is {cap:,}")
    return size


def _run(client, sql, params, cap):
    from google.cloud import bigquery

    assert_insert_only(sql)
    job_ = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=cap,
                                                                 use_query_cache=False))
    return job_, job_.result()


def make_client(principal=PRINCIPAL):
    """The BigQuery client of the read-only held-reasons reader: the builder service account by impersonation."""
    import google.auth
    from google.auth import impersonated_credentials
    from google.cloud import bigquery

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials = impersonated_credentials.Credentials(
        source_credentials=source, target_principal=principal,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"], lifetime=1800)
    return bigquery.Client(project=PROJECT, credentials=credentials, location="US")


def read_raw(client, since, until):
    """The stored bodies of ROUTES fetched from since to until, oldest first, as dicts. Python checks what the SQL
    filtered, so a row outside the window or the routes stops the run."""
    from google.cloud import bigquery

    params = [bigquery.ScalarQueryParameter("since", "DATE", since), bigquery.ScalarQueryParameter("until", "DATE", until),
              bigquery.ArrayQueryParameter("jobs", "STRING", list(JOBS)),
              bigquery.ArrayQueryParameter("routes", "STRING", list(ROUTES))]
    sql = READ_SQL.format(table=writers.table("raw_responses"))
    size = _dry(client, sql, params, READ_CAP)
    _, result = _run(client, sql, params, READ_CAP)
    rows = [dict(r) for r in result]
    for r in rows:
        fetched = _utc(r["fetched_at"]).date()
        if r["route"] not in ROUTES or not since <= fetched <= until:
            raise Refused(f"a stored row of {r['route']} fetched {fetched.isoformat()} is outside the window or routes")
    return rows, size


# Parsing

class Plan:
    def __init__(self):
        self.calls, self.posts, self.observations, self.creators, self.counters, self.skipped = [], [], [], [], [], 0


def _members(body):
    """[(platform, handle)] named by data.results[].target, or None when any row does not name its profile."""
    results = (body.get("data") or {}).get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or not results:
        return None
    out = []
    for row in results:
        target = row.get("target") if isinstance(row, dict) else None
        if not isinstance(target, dict) or not isinstance(target.get("platform"), str) \
                or not isinstance(target.get("handle"), str):
            return None
        out.append((target["platform"], target["handle"]))
    return out


def _fold(members):
    return frozenset((p.casefold(), h.casefold()) for p, h in members)


def _references(manifest, config, day):
    """Per market: the desk and gossip lists with their live protocols, and the curated list in the manifest's own case."""
    desk_calls = {c.market: c for c in (c for m in MARKETS for c in job.panel_calls(m, day, config)
                                         if c.route == "prism/profiles")}
    gossip = {f.market: f for f in local_sources.plan(day) if f.route == "prism/profiles"}
    refs = {}
    for market in MARKETS:
        curated = curated_creators.daily_rotation(manifest, market, day, 10 ** 6)
        refs[market] = {
            "desk": (_fold((i["platform"], i["handle"]) for i in desk_calls[market].params["items"]),
                     desk_calls[market].series_protocol()) if market in desk_calls else (frozenset(), None),
            "gossip": (_fold((i["platform"], i["handle"]) for i in gossip[market].params["items"]),
                       gossip[market].protocol()) if market in gossip else (frozenset(), None),
            "curated": {(r["platform"].casefold(), r["handle"].casefold()): {"platform": r["platform"],
                                                                                "handle": r["handle"]}
                        for r in curated},
            "listed": curated_creators.listed_handles(manifest, market)}
    return refs


def _classify(row, body, refs):
    if row["route"] != "prism/profiles":
        return "song", None
    members = _members(body)
    if row["market"] not in refs or members is None:
        return "unclassified", members
    ref, folded = refs[row["market"]], _fold(members)
    if folded and folded == ref["desk"][0]:
        return "desk", members
    if folded and folded == ref["gossip"][0]:
        return "gossip", members
    if folded and folded <= set(ref["curated"]):
        return "curated", members
    return "unclassified", members


def build(raw_rows, *, run_id, item_id_fn, geo_fn, manifest=None, config=None):
    """The rows the fixed parsers recover from the stored bodies, as a Plan: one entry per stored call in calls, and
    posts, observations, creators and counters (totals and deltas) ready to insert."""
    manifest = curated_creators.load_manifest() if manifest is None else manifest
    config = job.load_config() if config is None else config
    refs = _references(manifest, config, WINDOW[1])
    geo = job.safe_geo(geo_fn)
    rows = sorted(raw_rows, key=lambda r: (_utc(r["fetched_at"]), r["run_id"], r["route"], str(r["market"]),
                                           str(r.get("seed_key"))))
    bodies = [json.loads(r["body"]) if isinstance(r["body"], str) else r["body"] for r in rows]
    classes = [_classify(r, b, refs) for r, b in zip(rows, bodies)]
    rotation = {}
    for r, (klass, members) in zip(rows, classes):
        if klass == "curated":
            rotation.setdefault((r["run_id"], r["market"]), set()).update(_fold(members))
    plan, totals = Plan(), []
    for r, body, (klass, members) in zip(rows, bodies, classes):
        market = str(r["market"]).strip().upper()
        entry = {"run_id": run_id, "raw_run_id": r["run_id"], "job": r.get("job"), "route": r["route"],
                 "market": market, "fetched_at": _utc(r["fetched_at"]).isoformat(), "seed_key": r.get("seed_key"),
                 "class": klass, "protocol": None, "posts": 0, "observations": 0, "creators": 0, "counters": 0,
                 "dropped": 0, "post_ids": []}
        try:
            if klass == "song":
                out = parse_with_creators(r["route"], {"clipId": r.get("seed_key")}, market, body, r["fetched_at"],
                                          run_id, item_id_fn=item_id_fn, geo_fn=geo)
                entry["protocol"] = (out["counters"] or [{}])[0].get("protocol")
                kept = [c for c in out["counters"] if c["series"] == SONG_SERIES and c["unit"] == "total"]
                entry["dropped"] = len(out["counters"]) - len(kept) + len(out["posts"]) + len(out["observations"]) \
                    + len(out["creators"])
                totals += kept
                entry["counters"] = len(kept)
            else:
                protocol = _prism_protocol(klass, members, refs[market] if market in refs else None,
                                           rotation.get((r["run_id"], r["market"])))
                since = (date.fromisoformat(_local_day(_utc(r["fetched_at"]), market)) - timedelta(days=1)).isoformat()
                out = parse_with_creators(r["route"], {"include": "posts", "since": since}, market, body,
                                          r["fetched_at"], run_id, item_id_fn=item_id_fn, geo_fn=geo,
                                          protocol=protocol)
                _scope(out, klass, market, refs.get(market))
                entry["protocol"] = protocol
                plan.posts += out["posts"]
                plan.observations += out["observations"]
                plan.creators += out["creators"]
                entry.update(posts=len(out["posts"]), observations=len(out["observations"]),
                             creators=len(out["creators"]), post_ids=[p["post_id"] for p in out["posts"]])
        except ValueError:
            entry["class"] = "skipped"
            plan.skipped += 1
        plan.calls.append(entry)
    plan.observations = _once(plan.observations, ("post_id", "observed_at", "route", "market", "protocol"))
    plan.counters = _once(totals, ("obs_date", "market", "item_id", "series", "protocol", "unit", "observed_at"))
    plan.counters += _deltas(plan.counters)
    return plan


def _prism_protocol(klass, members, ref, union):
    if klass == "desk":
        return ref["desk"][1]
    if klass == "gossip":
        return ref["gossip"][1]
    if klass == "curated":
        return panel_protocol([ref["curated"][k] for k in sorted(union)])
    return panel_protocol([{"platform": p, "handle": h} for p, h in members or []])


def _scope(out, klass, market, ref):
    """The live job's source_market and series rules on the parsed observations of one prism/profiles call."""
    if klass in ("desk", "curated"):
        for obs in out["observations"]:
            obs["source_market"] = market
    elif klass == "gossip":
        own = {p["post_id"] for p in out["posts"]
               if (p["platform"], str(p.get("creator_id") or "").casefold()) in ref["listed"]}
        for obs in out["observations"]:
            obs["series"] = GOSSIP_SERIES
            if obs["post_id"] in own:
                obs["source_market"] = market


def _once(rows, key):
    seen, out = set(), []
    for row in rows:
        k = tuple(row[c] for c in key)
        if k not in seen:
            seen.add(k)
            out.append(row)
    return out


def _deltas(totals):
    """A delta per sound, market and day with a total on the day before: the day's latest total less the previous
    day's, as writers.deltas computes it."""
    latest = {}
    for row in totals:
        key = (row["obs_date"], row["market"], row["item_id"], row["series"], row["protocol"])
        if key not in latest or row["available_at"] > latest[key]["available_at"]:
            latest[key] = row
    out = []
    for key in sorted(latest):
        day = date.fromisoformat(key[0]) - timedelta(days=1)
        before = latest.get((day.isoformat(),) + key[1:])
        if before is not None:
            out.append(dict(latest[key], unit="delta", value=latest[key]["value"] - before["value"], pull_seq=None,
                            source="live"))
    return out


def rows_by_table(plan):
    return {"posts": writers.dedupe_posts(plan.posts), "creators": writers.dedupe_creators(plan.creators),
            "post_observations": plan.observations, "item_counter_daily": plan.counters}


# Run

def _batches(rows, size):
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def _append(path, record):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def run(client, *, since, until, apply, run_id, receipts_dir, fns):
    """A dry run (apply False) or the apply. Returns the report; Refused stops it before a row is written."""
    since, until = check_window(since, until)
    if apply:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise Refused("--apply needs --run-id of 3 to 80 letters, digits, dots, colons, dashes or underscores")
        if receipts_dir is None:
            raise Refused("--apply needs --receipts-dir")
        receipts_dir = Path(receipts_dir)
        if (receipts_dir / "calls.jsonl").exists():
            raise Refused("the receipts directory already holds a run; use a new one")
    raw, read_bytes = read_raw(client, since, until)
    item_id_fn, geo_fn = fns if fns is not None else job.detect_fns()
    plan = build(raw, run_id=run_id if apply else "dry-run", item_id_fn=item_id_fn, geo_fn=geo_fn)
    tables = rows_by_table(plan)
    batches = [(t, n, b) for t, rows in tables.items() for n, b in enumerate(_batches(rows, BATCH)) if b]
    estimates = {t: 0 for t in tables}
    for table, _, batch in batches:
        estimates[table] += _dry(client, insert_sql(table), _params(table, batch), WRITE_CAP)
    report = {"mode": "apply" if apply else "dry-run", "window": [since.isoformat(), until.isoformat()],
              "run_id": run_id if apply else None,
              "raw_rows": dict(Counter(r["route"] for r in raw)), "jobs": dict(Counter(r["job"] for r in raw)),
              "classes": dict(Counter(c["class"] for c in plan.calls)), "skipped": plan.skipped,
              "rows": {t: len(rows) for t, rows in tables.items()},
              "estimated_bytes": {"read": read_bytes, **estimates},
              "caps": {"read": READ_CAP, "write": WRITE_CAP}}
    if not apply:
        new = {t: 0 for t in tables}
        for table, _, batch in batches:
            sql, params = count_sql(table), _params(table, batch)
            _dry(client, sql, params, WRITE_CAP)
            new[table] += int(list(_run(client, sql, params, WRITE_CAP)[1])[0]["n"])
        report["would_insert"] = new
        return report
    receipts_dir.mkdir(parents=True, exist_ok=True)
    for call in plan.calls:
        _append(receipts_dir / "calls.jsonl", {k: v for k, v in call.items() if k != "post_ids"})
    inserted = {t: 0 for t in tables}
    status, error = "ok", None
    try:
        for table, n, batch in batches:
            sql, params = insert_sql(table), _params(table, batch)
            size = _dry(client, sql, params, WRITE_CAP)
            job_, _ = _run(client, sql, params, WRITE_CAP)
            affected = int(job_.num_dml_affected_rows or 0)
            inserted[table] += affected
            _append(receipts_dir / "statements.jsonl", {
                "run_id": run_id, "table": table, "batch": n, "rows": len(batch), "inserted": affected,
                "job_id": getattr(job_, "job_id", None), "bytes_estimated": size,
                "bytes_billed": getattr(job_, "total_bytes_billed", None)})
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        (receipts_dir / "summary.json").write_text(json.dumps(
            {**report, "status": status, "error": error, "inserted": inserted}, indent=2, default=str),
            encoding="utf-8")
    report["inserted"] = inserted
    return report


def main(argv=None, *, client=None, fns=None):
    args = argparse.ArgumentParser(prog="core.collect.backfill_prism", description=__doc__.split("\n")[0])
    mode = args.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read, parse and report; write nothing (the default)")
    mode.add_argument("--apply", action="store_true", help="insert the recovered rows; needs --run-id and --receipts-dir")
    args.add_argument("--run-id")
    args.add_argument("--since", default=WINDOW[0].isoformat(), help="first UTC fetch day, inside the window")
    args.add_argument("--until", default=WINDOW[1].isoformat(), help="last UTC fetch day, inside the window")
    args.add_argument("--receipts-dir", help="a new directory for calls.jsonl, statements.jsonl and summary.json")
    args.add_argument("--principal", default=PRINCIPAL)
    opts = args.parse_args(argv)
    try:
        since, until = date.fromisoformat(opts.since), date.fromisoformat(opts.until)
        check_window(since, until)
        if opts.apply and (opts.run_id is None or opts.receipts_dir is None):
            raise Refused("--apply needs --run-id and --receipts-dir")
        report = run(client if client is not None else make_client(opts.principal), since=since, until=until,
                     apply=opts.apply, run_id=opts.run_id, receipts_dir=opts.receipts_dir, fns=fns)
    except (Refused, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
