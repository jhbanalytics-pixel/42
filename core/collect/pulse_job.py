"""The intraday pulse job f42-pulse (FEATURES.md row 30a): core/collect/pulse.py on a schedule, with its writes.

Scheduler starts it every 3 hours from 09:00 to 21:00 SAST (core/setup/schedule.py). It is not a chain stage:
nothing starts it and it starts nothing. A run

- exits 0 with one line outside 06:30 to 22:30 SAST (pulse.window_reason), before it reads anything;
- exits 0 with one line unless the latest collect runs row for today's SAST date is ok, so it never runs
  beside the morning chain's collect or on a day collect has not read;
- reads the hot items (pulse.read_hot) and the last stored pull of each rank list (writers.last_pulls), and
  runs pulse.run on a SocialCrawl client on the pulse share, whose PULSE_DAILY cap and ledger bind every call;
- writes through core/collect/writers.py only: posts by the MERGE, then post_observations, post_items,
  item_counter_daily (the pulse's counters with their appearances and deltas, as collect writes them) and
  item_hourly by append;
- appends runs rows with stage pulse, a running row first and an ok or failed row with the counts at the end,
  not through chain.begin, which would refuse a second pulse on the same day.

item_hourly (L2 Needs 23, the table in core/schema/core.sql) gets one row per market, item, platform, SAST
hour start and lane class from this run's observations: posts is the distinct posts observed, creators their
distinct creators. The items an observation names are those of its post, as parse's rank counters name them
(each hashtag, the sound and the creator), and, on a counter read, the hot item the read was for. A hashtag
or key that gdelt.blocked flags (rule 1) or that item ids refuse gives no row.

post_items (L2 Needs 34c) links each post of the run to its items exactly as detect's aggregate step does
(core.detect.items.items_for_post: its hashtags, sound and creator, via hashtag, sound or creator), appended
through writers.insert_post_items, which skips a pair already linked. Without it a post first found by a pulse
has no item until detect itemises it, and the hourly Breaking rule (core/detect/breaking.py) cannot see it.

    python -m core.collect.pulse_job     the Cloud Run job f42-pulse
"""

import argparse
import json
import logging
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone

from core.collect import chain, pulse, writers
from core.collect.gdelt import blocked
from core.collect.parse import _item

log = logging.getLogger(__name__)

STAGE = "pulse"
# The counter route a hot item is read on, back to the item's kind.
COUNTED = {route: kind for kind, (route, _) in pulse.COUNTER_ROUTES.items()}


def _hour(observed_at):
    """The SAST hour start of an observation, as a UTC ISO timestamp like parse's observed_at."""
    moment = datetime.fromisoformat(str(observed_at)).astimezone(chain.SAST)
    return moment.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def _named(obs, post, item_id_fn):
    """Item ids an observation names: its post's hashtags, sound and creator, and a counter read's item."""
    ctx = {"item_id_fn": item_id_fn}
    named = []
    if post is not None:
        named += [("hashtag", h) for h in post.get("hashtags") or []]
        named += [("sound", post.get("sound_id")), ("creator", post.get("creator_id"))]
    if obs.get("route") in COUNTED:
        named.append((COUNTED[obs["route"]], obs.get("seed_key")))
    ids = set()
    for kind, raw in named:
        if raw in (None, "") or blocked(raw):
            continue
        item = _item(ctx, kind, raw, obs["platform"])
        if item is not None:
            ids.add(item)
    return ids


def hourly_rows(out, item_id_fn):
    """item_hourly rows from pulse.run's output."""
    posts = {}
    for p in out["posts"]:
        posts.setdefault(p["post_id"], p)
    groups = {}
    for o in out["observations"]:
        post = posts.get(o["post_id"])
        for item in sorted(_named(o, post, item_id_fn)):
            key = (o["market"], item, o["platform"], _hour(o["observed_at"]), o["lane_class"])
            group = groups.setdefault(key, (set(), set()))
            group[0].add(o["post_id"])
            if post is not None and post.get("creator_id") is not None:
                group[1].add(post["creator_id"])
    return [{"market": market, "item_id": item, "platform": platform, "hour": hour, "posts": len(ids),
             "creators": len(creators), "lane_class": lane_class, "run_id": out["run_id"]}
            for (market, item, platform, hour, lane_class), (ids, creators) in groups.items()]


def post_items_rows(out):
    """post_items rows for the posts of pulse.run's output: one per post and item, as the daily aggregate makes."""
    from core.detect.items import items_for_post

    rows, seen = [], set()
    for p in out["posts"]:
        for it in items_for_post(p):
            if (p["post_id"], it["item_id"]) not in seen:
                seen.add((p["post_id"], it["item_id"]))
                rows.append({"post_id": p["post_id"], "item_id": it["item_id"], "via": it["via"]})
    return rows


def write(bq, out, item_id_fn):
    """Every write of one pulse, through the writers. Returns the row counts."""
    run_id = out["run_id"]
    writers.merge_posts(bq, out["posts"])
    counters = out["counters"] + writers.appearances(out["counters"], run_id) + writers.deltas(bq, out["counters"], run_id)
    observations = writers.append(bq, "post_observations", out["observations"])
    links = writers.insert_post_items(bq, post_items_rows(out))
    counter_rows = writers.append(bq, "item_counter_daily", counters)
    hourly = writers.append(bq, "item_hourly", hourly_rows(out, item_id_fn))
    return {"observations": observations, "post_items": links, "counters": counter_rows, "item_hourly": hourly}


def counts(out, hot):
    return {"hot_rows": len(hot), "calls": sum(r["route"] is not None for r in out["records"]),
            "statuses": dict(Counter(r["status"] for r in out["records"])), "credits": out["credits"],
            "stopped": out["stopped"], "posts": len({p["post_id"] for p in out["posts"]}),
            "observations": len(out["observations"]), "counters": len(out["counters"])}


def live_client(run_id, bq, clock):
    from core.collect.socialcrawl_client import SocialCrawlClient, requests_http
    from core.collect.stores import BigQueryLedgerStore, BigQueryRawStore

    return SocialCrawlClient(share=pulse.PULSE_SHARE, run_id=run_id, mode="live",
                             ledger=BigQueryLedgerStore(bq, chain.PROJECT), raw=BigQueryRawStore(bq, chain.PROJECT),
                             http=requests_http, clock=clock)


def main(argv=None, *, runs=None, bq=None, make_client=None, fns=None, clock=None):
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args(argv)
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    why = pulse.window_reason(now)
    if why:
        print(f"pulse: not run, {why}")
        return 0
    day = now.astimezone(chain.SAST).date()
    if bq is None:
        from google.cloud import bigquery

        bq = bigquery.Client(project=chain.PROJECT)
    runs = runs or chain.BigQueryRunsStore(bq)
    collect = runs.latest("collect", day)
    if collect is None or collect["status"] != "ok":
        seen = "no runs row" if collect is None else f"latest status {collect['status']}"
        print(f"pulse: not run, collect for {day.isoformat()} has not finished ok ({seen})")
        return 0
    run = chain.Run(f"{STAGE}-{day:%Y%m%d}-{uuid.uuid4().hex[:12]}", STAGE, day, now.isoformat(), runs)
    runs.append(chain._row(run, "running", None, None, None))
    make_client = make_client or (lambda run_id: live_client(run_id, bq, clock))
    out, hot = None, []
    try:
        if fns is None:
            from core.collect.job import detect_fns

            fns = detect_fns()
        item_id_fn, geo_fn = fns
        hot = pulse.read_hot(bq, day)
        last = writers.last_pulls(bq, day)
        out = pulse.run(run.run_id, rows=hot, client=make_client(run.run_id), clock=clock, item_id_fn=item_id_fn,
                        geo_fn=geo_fn, last_pulls=last)
        written = write(bq, out, item_id_fn)
    except Exception as exc:
        log.exception("pulse %s failed", run.run_id)
        chain.finish(run, "failed", counts(out, hot) if out else {"hot_rows": len(hot)},
                     f"{type(exc).__name__}: {exc}"[:1000], runs=runs)
        return 1
    done = {**counts(out, hot), **written}
    chain.finish(run, "ok", done, runs=runs)
    print(json.dumps({"run_id": run.run_id, **done}))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
