"""Read side for Today, trends, finished Ask records, the Stage 2 screens (Discover, topics, Coverage) and Compare.

F42_DATA=fixtures (the default) reads core/api/fixtures/; F42_DATA=bigquery reads the v_*_current views
named in core/api/contract.md sections 8 and 10, with a 2 GB bytes-billed cap on every query.

Stage 2 reads return None when their table, view or column does not exist yet (a missing fixture file, or a
name absent from INFORMATION_SCHEMA, read at most every 10 minutes), and a list, possibly empty, when it does.
"""
import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAX_BYTES = 2_000_000_000
log = logging.getLogger("f42.api.store")
MARKET_TZ = {"ZA": dt.timezone(dt.timedelta(hours=2)), "NG": dt.timezone(dt.timedelta(hours=1)),
             "KE": dt.timezone(dt.timedelta(hours=3))}
PUBLISHED = ("published", "partial")
CORE, AGENT = "intelligence_42_core", "intelligence_42_agent"
_CATALOG = {}  # project -> {"at", "objects": {"dataset.name"}, "runs_columns": {...}}, read again after CATALOG_TTL
CATALOG_TTL = 600  # seconds, so objects L2 ships appear without a restart
_CATALOG_LOCK = threading.Lock()  # one catalog read when a page's reads start together
_SHARED = {}  # (project, client class) -> the process's BigQueryStore
_SHARED_LOCK = threading.Lock()


def get_store():
    """A FixtureStore a call, or the process's one BigQueryStore for its project, so every request reuses one
    client and its open connections instead of building a client (and fetching a token) per request."""
    kind = os.environ.get("F42_DATA") or "fixtures"
    if kind == "fixtures":
        return FixtureStore()
    if kind == "bigquery":
        from google.cloud import bigquery

        key = (os.environ.get("F42_PROJECT") or "ogilvy-trends-v2", bigquery.Client)
        with _SHARED_LOCK:
            held = _SHARED.get(key)
            if held is None:
                held = _SHARED[key] = BigQueryStore()
            return held
    raise ValueError(f"F42_DATA must be fixtures or bigquery, not {kind}")



def canon_platform(platform):
    """Collection writes X as "twitter"; 42 names it "x" everywhere it shows or filters platforms."""
    return "x" if platform == "twitter" else platform


def _known(post):
    """A post whose place is known as v_item_market_scope reads it: geo_confidence 0.7 or more from its region,
    home market or a place it names."""
    return ((post.get("geo_confidence") or 0) >= 0.7
            and post.get("geo_source") in ("ext_region", "home_market", "place_mention"))


def creator_key(platform, handle):
    """"platform:handle" as 42 matches an Evidence author to a creators row: X as x, the handle trimmed, lower case,
    without a leading @ or u/. None without a handle."""
    h = (handle or "").strip().lstrip("@")
    if h.startswith("u/"):
        h = h[2:]
    return f"{canon_platform(platform)}:{h.lower()}" if h else None


# Rows f42-agent appends to the suppression list outside BigQuery (contract.md section 16). The fixture store reads
# them as L1's table, so a hide made in tests shows on the next people read. Never rebound, only appended to.
SUPPRESSIONS = []
SUPPRESSION_COLUMNS = ("suppression_id", "status_at", "status", "creator_id", "platform", "handle", "reason", "who")


def current_suppressions(rows):
    """The current row per suppression_id, newest first, in the order of L1's v_suppressed_creators: the latest
    status_at, a tie going to the row that is not lifted, then to the row's own JSON text."""
    def rank(r):
        return (-dt.datetime.fromisoformat(r["status_at"]).timestamp(), r["status"] == "lifted",
                json.dumps(r, sort_keys=True))

    best = {}
    for r in rows:
        r = {k: r.get(k) for k in SUPPRESSION_COLUMNS}
        if r["suppression_id"] not in best or rank(r) < rank(best[r["suppression_id"]]):
            best[r["suppression_id"]] = r
    return sorted(best.values(), key=lambda r: (rank(r)[0], r["suppression_id"]))


HTTP_POOL = 32  # connections the BigQuery client keeps open: fast.WORKERS reads at once, plus pages reading alone


def _widen_pool(client):
    """Keep HTTP_POOL connections open instead of requests' 10, so reads started together each reuse a connection
    rather than opening one and dropping it. Only a plain HTTPS adapter is replaced (never a mutual TLS one)."""
    import requests

    try:
        session = client._http
        if type(session.get_adapter("https://")) is requests.adapters.HTTPAdapter:
            session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=HTTP_POOL, pool_maxsize=HTTP_POOL))
    except Exception as e:  # the default pool still works
        log.warning("BigQuery connection pool left as it is: %s", type(e).__name__)


def _counts(run):
    """A runs row's counts as a dict; the fixtures hold it as an object or as JSON text."""
    counts = run.get("counts") or {}
    return json.loads(counts) if isinstance(counts, str) else counts


CLUSTER_MARKETS = ("za", "ng", "ke", "pan")  # core.understand.job.CLUSTER_MARKETS, kept here so no BERTopic loads


def degraded_writes(counts):
    """The writes an understand run failed soft on, from its counts: "enrich" when it has enrich_error, then
    "cluster:<market>" for each market whose cluster counts carry an error. Empty for a clean run."""
    counts = counts if isinstance(counts, dict) else {}
    cluster = counts.get("cluster") if isinstance(counts.get("cluster"), dict) else {}
    return (["enrich"] if counts.get("enrich_error") else []) + [
        f"cluster:{m}" for m in CLUSTER_MARKETS if isinstance(cluster.get(m), dict) and cluster[m].get("error")]


# degraded_writes in SQL, over the latest runs row's counts; WITH OFFSET keeps the order above.
DEGRADED_SQL = "ARRAY(SELECT w FROM UNNEST([{}]) AS w WITH OFFSET o WHERE w IS NOT NULL ORDER BY o)".format(", ".join(
    ["IF(JSON_VALUE(r.counts, '$.enrich_error') IS NOT NULL, 'enrich', NULL)"]
    + [f"IF(JSON_VALUE(r.counts, '$.cluster.{m}.error') IS NOT NULL, 'cluster:{m}', NULL)" for m in CLUSTER_MARKETS]))


def viewer_record(record):
    """A stored Ask record as any reader gets it: without the query receipts the runs row keeps for reviewers
    (contract section 7), whose rows can hold names and fields the app hides."""
    if isinstance(record, dict) and "query_receipts" in record:
        record = {k: v for k, v in record.items() if k != "query_receipts"}
    return record


class FixtureStore:
    def __init__(self, root=FIXTURES):
        self.root = Path(root)

    def _load(self, name):
        return json.loads((self.root / f"{name}.json").read_text(encoding="utf-8"))

    def latest_brief_date(self):
        return max((r["brief_date"] for r in self._load("briefs")), default=None)

    def briefs(self, date):
        return [r for r in self._load("briefs") if r["brief_date"] == date]

    def previous_brief(self, market, before):
        rows = [r for r in self._load("briefs")
                if r["market"] == market and r["brief_date"] < before and r["status"] in PUBLISHED]
        return max(rows, key=lambda r: r["brief_date"], default=None)

    def collection_health(self, date):
        return [r for r in self._load("collection_health") if r["day"] == date]

    def latest_collection_day(self, until=None):
        """The latest day collection_health has rows for, on or before until when given, or None (Fieldwork's
default day and Coverage's way back from an empty day)."""
        return max((r["day"] for r in self._load("collection_health") if until is None or r["day"] <= until),
                   default=None)

    def calendar(self, start, end):
        rows = [r for r in self._load("calendar") if start <= r["moment_date"] <= end]
        return sorted(rows, key=lambda r: (r["moment_date"], r["market"], r["name"]))

    def runs(self, stage, run_date):
        return [r for r in self._load("runs") if r["stage"] == stage and r["run_date"] == run_date]

    def first_ok_collect_date(self):
        return min((r["run_date"] for r in self._load("runs") if r["stage"] == "collect" and r["status"] == "ok"),
                   default=None)

    def ask_record(self, ask_id):
        for r in self._load("runs"):
            if r["stage"] == "ask" and (r.get("record") or {}).get("ask_id") == ask_id:
                return viewer_record(r["record"])
        return None

    def health(self):
        try:
            self._load("briefs")
            return "ok"
        except (OSError, ValueError) as e:
            return f"fixtures unreadable: {type(e).__name__}"

    # Stage 2. A missing fixture file stands for a table or view that does not exist yet.

    def _optional(self, name):
        path = self.root / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _runs_all(self):
        return (self._optional("runs") or []) + (self._optional("runs_v2") or [])

    def latest_detect_run(self, until=None):
        ok = [r for r in self._runs_all() if r["stage"] == "detect" and r["status"] == "ok"
              and (until is None or r["run_date"] <= until)]
        if not ok or self._optional("item_state") is None:
            return None
        best = max(ok, key=lambda r: (r["run_date"], r.get("finished_at") or ""))
        return {"run_id": best["run_id"], "run_date": best["run_date"]}

    def item_states(self, run, market):
        breakouts = {}
        for b in self._optional("breakout_signals") or []:
            key = (b["item_id"], b["market"])
            if b["metric_date"] == run["run_date"] and b.get("creators") is not None:
                breakouts[key] = max(breakouts.get(key, b["creators"]), b["creators"])
        before = (dt.date.fromisoformat(run["run_date"]) - dt.timedelta(days=1)).isoformat()
        tones = {}
        for t in self._optional("item_tone_daily") or []:
            if t["metric_date"] in (run["run_date"], before):
                tones[(t["item_id"], t["market"], t["metric_date"])] = t.get("tone")
        rows = [dict(r, breakout_creators=breakouts.get((r["item_id"], r["market"])),
                     tone_today=tones.get((r["item_id"], r["market"], run["run_date"])),
                     tone_before=tones.get((r["item_id"], r["market"], before)))
                for r in self._optional("item_state") or []
                if r["metric_date"] == run["run_date"] and r["run_id"] == run["run_id"]
                and market in ("all", r["market"])]
        scope_rows = self._optional("item_market_scope")
        if scope_rows is None:
            return [dict(r, market_scope=None, market_posts7=None, market_news_posts7=None, total_posts7=None,
                         market_share7=None) for r in rows]
        scopes = {(r.get("metric_date"), r.get("item_id"), r.get("market")): r for r in scope_rows}
        out = []
        for r in rows:
            scope_row = scopes.get((r["metric_date"], r["item_id"], r["market"]))
            out.append(dict(r, market_scope="market" if (scope_row or {}).get("market_scope") == "market"
                            else "global", market_posts7=(scope_row or {}).get("market_posts7"),
                            market_news_posts7=(scope_row or {}).get("market_news_posts7"),
                            total_posts7=(scope_row or {}).get("total_posts7"),
                            market_share7=(scope_row or {}).get("market_share7")))
        return out

    def watch_matches(self, run):
        rows = self._optional("watch_matches")
        if rows is None:
            return None
        out = {}
        for r in rows:
            if r["match_date"] == run["run_date"]:
                out.setdefault(r["watch_id"], []).append(r["item_id"])
        return out

    def item_gate(self, market):
        rows = self._optional("item_gate")
        return None if rows is None else [r for r in rows if market in ("all", r["market"])]

    def item_history(self, item_id, market, start, end):
        rows = [{"metric_date": r["metric_date"], "state": r["state"], "creators3": r.get("creators3")}
                for r in self._optional("item_state") or []
                if r["item_id"] == item_id and r["market"] == market and start <= r["metric_date"] <= end]
        return sorted(rows, key=lambda r: r["metric_date"])

    def _item_rows(self, name, item_id, market):
        rows = self._optional(name)
        return None if rows is None else [r for r in rows if r["item_id"] == item_id and r["market"] == market]

    def item_series(self, item_id, market, days):
        return self._item_rows("item_timeseries", item_id, market)

    def item_waves(self, item_id, market):
        rows = self._item_rows("item_waves", item_id, market)
        return None if rows is None else sorted(rows, key=lambda r: r["peak_date"], reverse=True)

    def coord_signals(self, item_id, market, date):
        rows = self._item_rows("coord_signals", item_id, market)
        return None if rows is None else [r for r in rows if r["metric_date"] == date]

    def item_spread(self, date, market, item_id=None):
        rows = self._optional("item_spread")
        return None if rows is None else [r for r in rows if r["metric_date"] == date
                                          and market in ("all", r["market"]) and item_id in (None, r["item_id"])]

    def item_reach(self, date, market, item_ids):
        obs, items, posts = (self._optional(n) for n in ("post_observations", "post_items", "posts"))
        if obs is None or items is None or posts is None or not item_ids:
            return None
        end = dt.date.fromisoformat(date)
        scan, start = (end - dt.timedelta(days=27)).isoformat(), (end - dt.timedelta(days=6)).isoformat()
        seen = {}
        for o in obs:
            if (o["market"] == market and scan <= o["observed_date"] <= date and o.get("lane_class") != "legacy"
                    and (o.get("lane") or "") not in ("placebo", "agent_live")):
                s = seen.setdefault(o["post_id"], {"first": o["observed_date"], "own_feed": False, "measured": False,
                                                   "views": None})
                s["first"] = min(s["first"], o["observed_date"])
                s["own_feed"] = s["own_feed"] or o.get("source_market") == market
                s["measured"] = s["measured"] or o.get("lane_class") in ("unbiased_rank", "panel")
                if o.get("views") is not None:
                    s["views"] = max(s["views"] or 0, o["views"])
        flagged = {c["creator_id"] for c in self._optional("creators") or [] if (c.get("coord_score") or 0) >= 1}
        by_post = {p["post_id"]: p for p in posts}
        groups = {}
        for pi in items:
            post = pi["post_id"]
            if pi["item_id"] in item_ids and post in seen and seen[post]["first"] >= start and post in by_post:
                groups.setdefault(pi["item_id"], set()).add(post)
        out = []
        for item_id, post_ids in sorted(groups.items()):
            ps = [by_post[p] for p in sorted(post_ids)]
            own = [seen[p["post_id"]]["own_feed"] for p in ps]
            measured = [p for p in ps if seen[p["post_id"]]["measured"]]
            views = [v for v in (p.get("views") if p.get("views") is not None else seen[p["post_id"]]["views"]
                                 for p in ps) if v is not None]
            engagement = [p["engagement"] for p in ps if p.get("engagement") is not None]
            out.append({"item_id": item_id, "posts7": len(ps),
                        "creators7": len({p["creator_id"] for p in ps if p.get("creator_id")
                                          and p["creator_id"] not in flagged}),
                        "measured_posts7": len(measured),
                        "measured_creators7": len({p["creator_id"] for p in measured if p.get("creator_id")
                                                   and p["creator_id"] not in flagged}),
                        "views7": sum(views) if views else None, "views_posts7": len(views),
                        "engagement7": sum(engagement) if engagement else None, "engagement_posts7": len(engagement),
                        "platforms": sorted({p["platform"] for p in ps if p.get("platform")}),
                        "located7": sum(1 for p, f in zip(ps, own) if _known(p) or f),
                        "local7": sum(1 for p, f in zip(ps, own)
                                      if (_known(p) and str(p.get("geo_market") or "").upper() == market) or f),
                        "own_feed7": sum(own)})
        return out

    def health_days(self, market, start, end):
        rows = self._optional("collection_health")
        if rows is None:
            return None
        days = {}
        for r in rows:
            if (r["market"] == market and start <= r["day"] <= end and r.get("platform")
                    and r.get("lane_class") in ("unbiased_rank", "panel", "unbiased_counter")):
                key = (r["platform"], r["day"])
                days[key] = days.get(key, True) and bool(r.get("valid"))
        out = {}
        for (platform, _), ok in days.items():
            o = out.setdefault(platform, {"platform": platform, "days": 0, "days_ok": 0})
            o["days"] += 1
            o["days_ok"] += int(ok)
        return [out[p] for p in sorted(out)]

    def creator_names(self, keys):
        rows = self._optional("creators")
        if rows is None or not keys:
            return None
        out = {}
        for r in sorted(rows, key=lambda r: (r.get("display_name") is None, -(r.get("followers") or 0))):
            key = f"{(r.get('platform') or '').strip().lower()}:{(r.get('creator_id') or '').strip().lower()}"
            if key in keys and key not in out:
                out[key] = {k: r.get(k) for k in ("key", "creator_id", "platform", "handle", "display_name")}
                out[key]["key"] = key
        return out

    def item_origin(self, item_id, market):
        return self._item_rows("item_origin", item_id, market)

    def news_followthrough(self, item_id, market, until):
        rows = self._optional("news_followthrough")
        return None if rows is None else [
            r for r in rows if r["market"] == market and r["seed_date"] <= until
            and (r["item_id"] == item_id or any(m.get("item_id") == item_id for m in r.get("matched_items") or []))]

    def item_evidence(self, item_id, market):
        rows = self._optional("item_evidence")
        return None if rows is None else _in_market(
            [r for r in rows if r["item_id"] == item_id], market)

    def credits(self, date):
        groups = {}
        for r in self._optional("credit_ledger") or []:
            if r["trend_date"] != date:
                continue
            g = groups.setdefault((r["market"], r["job"], r["lane"]),
                                  {"market": r["market"], "job": r["job"], "lane": r["lane"], "charged": 0.0,
                                   "calls": 0, "run_ids": set()})
            g["charged"] += r.get("credits_charged") or 0.0
            g["calls"] += r.get("calls") or 0
            if r.get("run_id"):
                g["run_ids"].add(r["run_id"])
        rows = [dict(g, run_ids=sorted(g["run_ids"])) for g in groups.values()]
        return sorted(rows, key=lambda r: (r["market"] or "", r["job"] or "", r["lane"] or ""))

    def runs_of_day(self, date):
        latest, usd = {}, {}
        for r in self._runs_all():
            if r["run_date"] != date:
                continue
            key = (r.get("finished_at") is not None, r.get("finished_at") or "", r.get("started_at") or "")
            if r["run_id"] not in latest or key >= latest[r["run_id"]][0]:
                latest[r["run_id"]] = (key, r)
            value = r.get("model_usd")
            if value is None:
                record = r.get("record") or {}
                if isinstance(record, str):
                    record = json.loads(record)
                value = (record.get("run") or {}).get("model_usd")
            if value is None:
                value = _counts(r).get("model_usd")
            if value is not None:
                run_id = r["run_id"]
                if run_id not in usd or value > usd[run_id]:
                    usd[run_id] = value
        return [{"run_id": r["run_id"], "stage": r["stage"], "status": r["status"], "started_at": r.get("started_at"),
                 "finished_at": r.get("finished_at"), "error": r.get("error"), "model_usd": usd.get(r["run_id"]),
                 "degraded": degraded_writes(_counts(r))}
                for _, r in sorted(latest.values(), key=lambda kr: kr[1].get("started_at") or "")]

    def has_model_usd(self):
        return any(any(k in r for k in ("model_usd", "record", "counts")) for r in self._runs_all())

    def scorecard(self):
        """The latest week's rows, one per market (engine_scorecard as L1 created it: week_start, market, JSON Figures)."""
        rows = self._optional("engine_scorecard")
        if not rows:
            return None
        latest = max(r["week_start"] for r in rows)
        return [r for r in rows if r["week_start"] == latest]

    # Compare (contract.md section 11). runs_compare holds the aggregate runs the other fixtures do not.

    def latest_aggregate_run(self):
        ok = [r for r in self._runs_all() + (self._optional("runs_compare") or [])
              if r["stage"] == "aggregate" and r["status"] == "ok"]
        if not ok:
            return None
        best = max(ok, key=lambda r: (r["run_date"], r.get("finished_at") or ""))
        return {"run_id": best["run_id"], "run_date": best["run_date"]}

    def map_items(self, item_ids):
        rows = self._map_rows()
        return None if rows is None else [r for r in rows if r["item_id"] in item_ids]

    def breaking_signals(self, since):
        """Rows of v_breaking_signals_current whose hour is at or after since (ISO with offset), newest first. None
        while the fixture file is missing, which stands for the view not existing yet."""
        rows = self._optional("breaking_signals")
        if rows is None:
            return None
        start = dt.datetime.fromisoformat(since)
        rows = [r for r in rows if dt.datetime.fromisoformat(r["hour"]) >= start]
        return sorted(rows, key=lambda r: dt.datetime.fromisoformat(r["hour"]), reverse=True)

    def search_signals(self, start, end, markets):
        """google_search_signals rows of the given markets fetched on SAST days start to end, each with its SAST
        fetch_day. None while the fixture file is missing, which stands for the table not existing yet."""
        rows = self._optional("google_search_signals")
        if rows is None:
            return None
        out = []
        for r in rows:
            day = dt.datetime.fromisoformat(r["fetched_at"]).astimezone(MARKET_TZ["ZA"]).date().isoformat()
            if r.get("market") in markets and start <= day <= end:
                out.append({k: r.get(k) for k in ("market", "term", "source", "rank", "refreshed_at")} |
                           {"fetch_day": day})
        return out

    def health_range(self, start, end, markets):
        return [r for r in self._optional("collection_health") or []
                if start <= r["day"] <= end and r["market"] in markets]

    def compare_counts(self, subjects, start, end):
        """The rows BigQueryStore.compare_counts returns, computed the same way over the fixture posts."""
        posts, links, sightings = (self._optional(n) for n in ("posts", "post_items", "post_observations"))
        if posts is None or links is None or sightings is None:
            return None
        by_id = {p["post_id"]: p for p in posts}
        first = {}
        for o in sightings:
            if o["lane_class"] == "legacy" or o.get("lane") in EXCLUDED_LANES:
                continue
            key = (o["post_id"], o["market"])
            first[key] = min(first.get(key, o["observed_date"]), o["observed_date"])
        out = []
        for s in subjects:
            ids = {r["post_id"] for r in links if r["item_id"] == s["item_id"]}
            if s["terms"] is not None:
                pattern = _keyword_regex(s["terms"])
                ids |= {pid for pid, p in by_id.items()
                        if pattern.search(p.get("text") or "") or pattern.search(p.get("transcript") or "")}
            hits = [(by_id[pid], first[(pid, s["market"])]) for pid in sorted(ids)
                    if pid in by_id and (pid, s["market"]) in first
                    and (s["platform"] is None or canon_platform(by_id[pid]["platform"]) == s["platform"])]
            win = [(p, day) for p, day in hits if start <= day <= end]
            counted = [p for p, _ in win if any(p.get(k) is not None for k in ("likes", "comments", "shares"))]
            out.append({"grain": "total", "key": s["key"], "day": None, "posts": len(win),
                        "creators": len({p["creator_id"] for p, _ in win if p.get("creator_id") is not None}),
                        "engagement": None if win and not counted else sum(
                            (p.get("likes") or 0) + (p.get("comments") or 0) + (p.get("shares") or 0)
                            for p in counted),
                        "engagement_posts": len(counted),
                        "platforms": len({canon_platform(p["platform"]) for p, _ in win if p.get("platform") is not None}),
                        "first_seen": min((day for _, day in hits), default=None)})
            per_day = {}
            for _, day in win:
                per_day[day] = per_day.get(day, 0) + 1
            out += [{"grain": "day", "key": s["key"], "day": day, "posts": n, "creators": None, "engagement": None,
                     "engagement_posts": None, "platforms": None, "first_seen": None}
                    for day, n in sorted(per_day.items())]
        return out

    # Creators, communities and history (contract.md section 12). The people_ and history_ files add creators,
    # posts, map rows, waves and runs to the Stage 2 fixtures; people_sensitive stands for v_sensitive_items,
    # people_sensitive_complete for v_sensitive_items_complete,
    # people_boards for item_counter_daily board rows, people_suppressed for the suppression view.

    def _both(self, name, extra):
        base = self._optional(name)
        return None if base is None else base + (self._optional(extra) or [])

    def _map_rows(self):
        rows = self._both("cultural_map", "people_cultural_map")
        return None if rows is None else [r for r in rows if r.get("valid_to") is None]

    def creator(self, creator_id):
        return next(iter(self.creators_by_id([creator_id]) or []), None)

    def creators_by_id(self, creator_ids):
        rows = self._optional("people_creators")
        if rows is None:
            return None
        return [{k: r.get(k) for k in CREATOR_COLUMNS} for r in rows if r["creator_id"] in creator_ids]

    def creators_by_handle(self, keys):
        rows = self._optional("people_creators")
        if rows is None:
            return None
        return [{k: r.get(k) for k in CREATOR_COLUMNS} for r in rows
                if creator_key(r.get("platform"), r.get("handle")) in set(keys)]

    def detect_states(self, start, end):
        """item_state rows of every good detect run dated inside the window."""
        rows = self._optional("item_state")
        if rows is None:
            return None
        runs = {(r["run_id"], r["run_date"]) for r in self._runs_all()
                if r["stage"] == "detect" and r["status"] == "ok" and start <= r["run_date"] <= end}
        return [r for r in rows if (r["run_id"], r["metric_date"]) in runs]

    def sensitive_items(self):
        sources = [self._optional(n) for n in ("people_sensitive_complete", "people_sensitive")]
        found = [rows for rows in sources if rows is not None]
        return None if not found else {r["item_id"] for rows in found for r in rows}

    def sensitive_complete(self):
        return self._optional("people_sensitive_complete") is not None

    def generic_items(self):
        return {r["item_id"] for r in self._map_rows() or [] if r.get("status") == "generic"}

    def board_items(self, market, start, end):
        return {r["item_id"] for r in self._optional("people_boards") or []
                if r["market"] == market and start <= r["obs_date"] <= end}

    def suppressed_creators(self):
        """The view's creator ids, plus those the in-memory rows name by creator_id or by platform and handle."""
        rows = self._optional("people_suppressed")
        if rows is None:
            return None
        held = [r for r in current_suppressions(list(SUPPRESSIONS)) if r["status"] != "lifted"]
        keys = {creator_key(r["platform"], r["handle"]) for r in held if r["platform"]} - {None}
        by_handle = (self.creators_by_handle(keys) or []) if keys else []
        return ({r["creator_id"] for r in rows} | {r["creator_id"] for r in held if r["creator_id"]}
                | {c["creator_id"] for c in by_handle})

    def suppressions(self):
        """The suppression list for GET /api/suppressions: the in-memory rows, current row per id. None while the
        view fixture is missing, which stands for L1's list not existing yet."""
        if self._optional("people_suppressed") is None:
            return None
        return current_suppressions(list(SUPPRESSIONS))

    def _posts(self):
        posts = self._both("posts", "people_posts")
        if posts is None:
            return None
        items = {}
        for link in self._both("post_items", "people_post_items") or []:
            items.setdefault(link["post_id"], set()).add(link["item_id"])
        handles = {c["creator_id"]: c.get("handle") for c in self._optional("people_creators") or []}
        enrich = {e["post_id"]: e for e in self._optional("people_post_enrichment") or []}
        return [dict({k: p.get(k) for k in POST_COLUMNS}, handle=handles.get(p.get("creator_id")),
                     item_ids=sorted(items.get(p["post_id"], ())),
                     langs=(enrich.get(p["post_id"]) or {}).get("langs"),
                     formats=(enrich.get(p["post_id"]) or {}).get("formats")) for p in posts]

    def creator_posts(self, creator_id, excluded, limit):
        posts = self._posts()
        if posts is None:
            return None
        mine = [p for p in posts if p["creator_id"] == creator_id and not set(p["item_ids"]) & set(excluded)]
        return sorted(mine, key=lambda p: (p["published_at"] or "", p["post_id"]), reverse=True)[:limit]

    def window_posts(self, creator_ids, market, start, end):
        """Posts of these creators first sighted in the market inside the window (legacy and agent_live aside)."""
        posts, sightings = self._posts(), self._both("post_observations", "people_post_observations")
        if posts is None or sightings is None:
            return None
        first = {}
        for o in sightings:
            if o["market"] != market or o["lane_class"] == "legacy" or o.get("lane") in EXCLUDED_LANES:
                continue
            first[o["post_id"]] = min(first.get(o["post_id"], o["observed_date"]), o["observed_date"])
        return [dict(p, first_day=first[p["post_id"]]) for p in posts
                if (creator_ids is None or p["creator_id"] in creator_ids) and p["post_id"] in first
                and start <= first[p["post_id"]] <= end]

    def community_edges(self, market, start, end, excluded):
        """Pairs of creators sharing three or more items outside `excluded` in the window, as BigQuery returns."""
        posts = self.window_posts(None, market, start, end)
        if posts is None:
            return None
        by_creator = {}
        for p in posts:
            if p["creator_id"] is not None:
                by_creator.setdefault(p["creator_id"], set()).update(set(p["item_ids"]) - set(excluded))
        ids = sorted(by_creator)
        out = []
        for n, a in enumerate(ids):
            for b in ids[n + 1:]:
                shared = by_creator[a] & by_creator[b]
                if len(shared) >= 3:
                    out.append({"creator_a": a, "creator_b": b, "items": sorted(shared)})
        return out

    def posts_by_id(self, post_ids):
        return [p for p in self._posts() or [] if p["post_id"] in post_ids]

    def seed_path_posts(self, market, terms, start, end, limit):
        """The rows BigQueryStore.seed_path_posts returns, computed the same way over the fixture posts. The
        seedpath_ files add posts, sightings and links only this read sees."""
        names = ("posts", "post_observations", "post_items")
        base = [self._optional(n) for n in names]
        if any(b is None for b in base):
            return None
        posts, obs, links = (b + (self._optional("people_" + n) or []) + (self._optional("seedpath_" + n) or [])
                             for b, n in zip(base, names))
        first = {}
        for o in obs:
            if o["market"] != market or o["lane_class"] == "legacy" or o.get("lane") in EXCLUDED_LANES:
                continue
            first[o["post_id"]] = min(first.get(o["post_id"], o["observed_date"]), o["observed_date"])
        items = {}
        for link in links:
            items.setdefault(link["post_id"], set()).add(link["item_id"])
        pattern, tags = _keyword_regex(terms), {t.lower() for t in terms}
        hits = {}
        for p in posts:
            if p["post_id"] not in first or p["post_id"] in hits:
                continue
            if (pattern.search(p.get("text") or "") or pattern.search(p.get("transcript") or "")
                    or any((h or "").lstrip("#").lower() in tags for h in p.get("hashtags") or [])):
                hits[p["post_id"]] = p
        out = []
        for platform in sorted({canon_platform(p["platform"]) for p in hits.values()}):
            mine = [pid for pid, p in hits.items() if canon_platform(p["platform"]) == platform]
            out.append({"grain": "first", "platform": platform, "day": min(first[pid] for pid in mine),
                        "posts": len(mine), "post_id": None, "text": None, "transcript": None, "hashtags": None,
                        "item_ids": None})
        win = sorted((p for pid, p in hits.items() if start <= first[pid] <= end),
                     key=lambda p: (first[p["post_id"]], p["post_id"]))
        out += [{"grain": "post", "platform": canon_platform(p["platform"]), "day": first[p["post_id"]],
                 "posts": None, "post_id": p["post_id"], "text": p.get("text"), "transcript": p.get("transcript"),
                 "hashtags": list(p.get("hashtags") or []), "item_ids": sorted(items.get(p["post_id"], ()))}
                for p in win[:limit]]
        return out

    def seed_queue(self, market, since, until, limit):
        """The rows BigQueryStore.seed_queue returns, over the fixture seed_queue. The fixture rows are dated, so
        since and until (which bound the BigQuery partitions read) are not applied here."""
        rows = self._optional("seed_queue")
        if rows is None:
            return None
        return _seed_queue_rows([r for r in rows if r["market"] == market and r.get("lane") != "placebo"], limit)

    def waves(self, item_ids, market):
        rows = self._both("item_waves", "history_waves")
        if rows is None:
            return None
        rows = [r for r in rows if r["item_id"] in item_ids and market in (None, r["market"])]
        return sorted(rows, key=lambda r: (r["item_id"], r["peak_date"]), reverse=True)

    def item_days(self, item_ids, market, start, end):
        rows = self._optional("history_item_days")
        if rows is None:
            return None
        return sorted((r for r in rows if r["item_id"] in item_ids and r["market"] == market
                       and start <= r["metric_date"] <= end), key=lambda r: (r["item_id"], r["metric_date"]))

    def nearest_items(self, item_id, k):
        """Nearest stored centroids by cosine distance, or None while the item has none."""
        rows = {r["item_id"]: r["centroid"] for r in self._optional("history_centroids") or [] if r.get("centroid")}
        if item_id not in rows:
            return None
        here = rows[item_id]

        def distance(v):
            dot = sum(x * y for x, y in zip(here, v))
            norm = (sum(x * x for x in here) * sum(y * y for y in v)) ** 0.5
            return 1 - dot / norm if norm else 1.0

        out = [{"item_id": i, "distance": distance(v)} for i, v in rows.items() if i != item_id]
        return sorted(out, key=lambda r: (r["distance"], r["item_id"]))[:k]

    def search_items(self, q, limit):
        rows = self._map_rows()
        if rows is None:
            return None
        q = q.lower()
        hits = [{k: r.get(k) for k in ("item_id", "kind", "label", "canonical_key", "aliases")} for r in rows
                if any(q in (t or "").lower()
                       for t in [r.get("label"), r.get("canonical_key")] + (r.get("aliases") or []))]
        return sorted(hits, key=lambda r: (r["label"] or "", r["item_id"]))[:limit]

    def lexicon_terms(self, market, kinds, start, end, week_from, prior_from, limit):
        """Page port, 3 October 2026: the rows BigQueryStore.lexicon_terms returns, counted the same way over
        lexicon_item_days (v_item_daily_current's columns) and the open cultural_map rows, lexicon_cultural_map
        adding items only this read sees. None while lexicon_item_days is missing."""
        days = self._optional("lexicon_item_days")
        extra = self._optional("lexicon_cultural_map")
        rows = self._map_rows()
        if days is None or (rows is None and extra is None):
            return None
        cmap = {r["item_id"]: r for r in (rows or []) + [r for r in extra or [] if r.get("valid_to") is None]}
        placebo = {(q["item_id"], q["market"], q["seed_date"]) for q in self._optional("lexicon_seed_queue") or []
                   if q.get("lane") == "placebo"}
        lanes = {}
        for r in days:
            if (r["market"] != market or not start <= r["metric_date"] <= end or r["lane_class"] == "_any"
                    or (r["lane_class"] == "search_presence" and (r["item_id"], market, r["metric_date"]) in placebo)):
                continue
            key = (r["item_id"], r["metric_date"], r["lane_class"])
            lanes[key] = lanes.get(key, 0) + (r["posts"] or 0)
        per_day = {}
        for (item, day, _lane), posts in lanes.items():
            per_day[(item, day)] = max(per_day.get((item, day), 0), posts)
        out = {}
        for (item, day), posts in per_day.items():
            m = cmap.get(item)
            if not posts or m is None or m["kind"] not in kinds or m.get("status") != "active":
                continue
            row = out.setdefault(item, {"item_id": item, "kind": m["kind"], "label": m.get("label"),
                                        "canonical_key": m.get("canonical_key"), "status": m.get("status"),
                                        "first_seen": m.get("first_seen"), "posts_window": 0, "posts_week": 0,
                                        "posts_prior_week": 0})
            row["posts_window"] += posts
            row["posts_week"] += posts if day >= week_from else 0
            row["posts_prior_week"] += posts if prior_from <= day < week_from else 0
        return sorted(out.values(), key=lambda r: (-r["posts_window"], r["label"] or "", r["item_id"]))[:limit]

    def ask_history(self, limit, before, market=None):
        rows = []
        for r in self._runs_all() + (self._optional("history_runs") or []):
            record = r.get("record") or {}
            if r["stage"] != "ask" or not record.get("ask_id"):
                continue
            if market and record.get("market") != market:
                continue
            if before and dt.datetime.fromisoformat(r["started_at"]) >= dt.datetime.fromisoformat(before):
                continue
            rows.append({"ask_id": record["ask_id"], "question": record.get("question") or r.get("question"),
                         "asked_at": r["started_at"], "status": record.get("status"),
                         "answer_status": (record.get("answer") or {}).get("status"), "market": record.get("market")})
        return sorted(rows, key=lambda r: dt.datetime.fromisoformat(r["asked_at"]), reverse=True)[:limit]

    def brief_history(self, limit, before, market=None):
        rows = [r for r in self._load("briefs") if (before is None or r["brief_date"] < before)
                and (market is None or r["market"] == market)]
        dates = sorted({r["brief_date"] for r in rows}, reverse=True)[:limit]
        return [{k: r.get(k) for k in ("brief_date", "market", "run_id", "status", "published_at", "payload")}
                for r in rows if r["brief_date"] in dates]

    def finding_rows(self, finding_id):
        rows = self._optional("history_findings")
        return None if rows is None else [dict(r) for r in rows if r.get("finding_id") == finding_id]

    def insert_finding(self, row):
        raise RuntimeError("Findings writes are unavailable in fixture mode")

    def findings(self, item_id=None):
        rows = self._optional("history_findings")
        if rows is None:
            return None
        rows = [r for r in rows if r.get("valid_to") is None
                and (item_id is None or any(item_id in (c.get("item_ids") or []) for c in r.get("claims") or []))]
        return sorted(rows, key=lambda r: dt.datetime.fromisoformat(r["as_of"]), reverse=True)


class BigQueryStore:
    def __init__(self, project=None, client=None):
        from google.cloud import bigquery

        self.bq = bigquery
        self.project = project or os.environ.get("F42_PROJECT") or "ogilvy-trends-v2"
        self.client = client or bigquery.Client(project=self.project)
        # A real client reads through jobs.query, which answers a short read in one round trip; stand-in clients in
        # tests keep the query().result() path they implement.
        from google.cloud.bigquery.client import Client as _Client

        self._one_trip = isinstance(self.client, _Client)
        if self._one_trip:
            _widen_pool(self.client)

    def _t(self, name):
        return f"`{self.project}.{name}`"

    def _query(self, sql, **params):
        qp = []
        for name, (typ, value) in params.items():
            if typ.startswith("ARRAY<"):
                qp.append(self.bq.ArrayQueryParameter(name, typ[6:-1], list(value)))
                continue
            if typ == "DATE" and value is not None:
                value = dt.date.fromisoformat(value)
            if typ == "TIMESTAMP" and isinstance(value, str):
                value = dt.datetime.fromisoformat(value)
            qp.append(self.bq.ScalarQueryParameter(name, typ, value))
        cfg = self.bq.QueryJobConfig(maximum_bytes_billed=MAX_BYTES, query_parameters=qp)
        if getattr(self, "_one_trip", False):
            rows = self.client.query_and_wait(sql, job_config=cfg)
        else:
            rows = self.client.query(sql, job_config=cfg).result()
        return [_normalise(dict(r.items())) for r in rows]

    def latest_brief_date(self):
        rows = self._query(f"SELECT MAX(b.brief_date) AS d FROM {self._t('intelligence_42_agent.v_briefs_current')} b")
        return rows[0]["d"] if rows else None

    def briefs(self, date):
        return self._query(
            "SELECT b.brief_date, b.market, b.run_id, b.published_at, b.status, b.payload, b.rule_version "
            f"FROM {self._t('intelligence_42_agent.v_briefs_current')} b WHERE b.brief_date = @d ORDER BY b.market",
            d=("DATE", date))

    def previous_brief(self, market, before):
        rows = self._query(
            "SELECT b.brief_date, b.market, b.run_id, b.published_at, b.status, b.payload, b.rule_version "
            f"FROM {self._t('intelligence_42_agent.v_briefs_current')} b "
            "WHERE b.market = @market AND b.brief_date < @before AND b.status IN ('published', 'partial') "
            "ORDER BY b.brief_date DESC LIMIT 1",
            market=("STRING", market), before=("DATE", before))
        return rows[0] if rows else None

    def collection_health(self, date):
        return self._query(
            "SELECT h.day, h.market, h.platform, h.route, h.series, h.protocol, h.lane_class, h.calls, h.calls_ok, "
            "h.units_planned, h.units_ok, h.items, h.ref_items, h.ref_days, h.k, h.valid, h.invalid_reason, "
            f"h.located_share, h.run_id FROM {self._t('intelligence_42_core.v_collection_health_current')} h "
            "WHERE h.day = @d",
            d=("DATE", date))

    def latest_collection_day(self, until=None):
        """The latest day on or before until (default today in SAST) with collection_health rows, within 60 days,
or None."""
        until = until or dt.datetime.now(MARKET_TZ["ZA"]).date().isoformat()
        rows = self._query(
            f"SELECT MAX(h.day) AS d FROM {self._t('intelligence_42_core.v_collection_health_current')} h "
            "WHERE h.day <= @until AND h.day >= DATE_SUB(@until, INTERVAL 60 DAY)",
            until=("DATE", until))
        return rows[0]["d"] if rows else None

    def calendar(self, start, end):
        return self._query(
            "SELECT c.moment_date, c.market, c.name, c.kind, c.source, c.item_ids "
            f"FROM {self._t('intelligence_42_core.calendar')} c WHERE c.moment_date BETWEEN @start AND @end "
            "ORDER BY c.moment_date, c.market, c.name",
            start=("DATE", start), end=("DATE", end))

    def runs(self, stage, run_date):
        return self._query(
            "SELECT r.run_id, r.stage, r.run_date, r.status, r.started_at, r.finished_at, r.counts, r.error "
            f"FROM {self._t('intelligence_42_agent.runs')} r WHERE r.stage = @stage AND r.run_date = @d",
            stage=("STRING", stage), d=("DATE", run_date))

    def first_ok_collect_date(self):
        rows = self._query(
            f"SELECT MIN(r.run_date) AS d FROM {self._t('intelligence_42_agent.runs')} r "
            "WHERE r.stage = 'collect' AND r.status = 'ok'")
        return rows[0]["d"] if rows else None

    def ask_record(self, ask_id):
        rows = self._query(
            f"SELECT r.record FROM {self._t('intelligence_42_agent.runs')} r "
            "WHERE r.stage = 'ask' AND JSON_VALUE(r.record, '$.ask_id') = @ask_id "
            "ORDER BY r.finished_at DESC LIMIT 1",
            ask_id=("STRING", ask_id))
        return viewer_record(rows[0]["record"]) if rows else None

    def health(self):
        try:
            self._query("SELECT 1 AS ok")
            return "ok"
        except Exception as e:
            return f"bigquery unreachable: {type(e).__name__}"

    # Stage 2 (contract.md section 10). Objects L2 has not built yet read as None.

    def _catalog(self):
        cat = _CATALOG.get(self.project)
        if cat is None or time.monotonic() - cat["at"] > CATALOG_TTL:
            with _CATALOG_LOCK:
                return self._read_catalog()
        return cat

    def _read_catalog(self):
        cat = _CATALOG.get(self.project)
        if cat is None or time.monotonic() - cat["at"] > CATALOG_TTL:
            parts = []
            for ds in (CORE, AGENT):
                parts.append(f"SELECT '{ds}' AS ds, t.table_name AS n, 'table' AS what "
                             f"FROM `{self.project}.{ds}.INFORMATION_SCHEMA.TABLES` t")
                parts.append(f"SELECT '{ds}' AS ds, r.routine_name AS n, 'routine' AS what "
                             f"FROM `{self.project}.{ds}.INFORMATION_SCHEMA.ROUTINES` r")
            parts.append(f"SELECT '{AGENT}' AS ds, c.column_name AS n, 'runs_column' AS what "
                         f"FROM `{self.project}.{AGENT}.INFORMATION_SCHEMA.COLUMNS` c WHERE c.table_name = 'runs'")
            parts.append(f"SELECT '{CORE}' AS ds, c.column_name AS n, 'map_column' AS what "
                         f"FROM `{self.project}.{CORE}.INFORMATION_SCHEMA.COLUMNS` c "
                         "WHERE c.table_name = 'cultural_map'")
            parts.append(f"SELECT '{AGENT}' AS ds, c.column_name AS n, 'findings_column' AS what "
                         f"FROM `{self.project}.{AGENT}.INFORMATION_SCHEMA.COLUMNS` c "
                         "WHERE c.table_name = 'findings'")
            parts.append(f"SELECT '{CORE}' AS ds, c.column_name AS n, 'scope_column' AS what "
                         f"FROM `{self.project}.{CORE}.INFORMATION_SCHEMA.COLUMNS` c "
                         "WHERE c.table_name = 'v_item_market_scope'")
            rows = self._query(" UNION ALL ".join(parts))
            cat = {"at": time.monotonic(),
                   "objects": {f"{r['ds']}.{r['n']}" for r in rows if r["what"] in ("table", "routine")},
                   "runs_columns": {r["n"] for r in rows if r["what"] == "runs_column"},
                   "map_columns": {r["n"] for r in rows if r["what"] == "map_column"},
                   "findings_columns": {r["n"] for r in rows if r["what"] == "findings_column"},
                   "scope_columns": {r["n"] for r in rows if r["what"] == "scope_column"}}
            _CATALOG[self.project] = cat
        return cat

    def _find(self, name):
        """The dataset-qualified table name, looked up in core then agent, or None if it does not exist yet."""
        objects = self._catalog()["objects"]
        for ds in (CORE, AGENT):
            if f"{ds}.{name}" in objects:
                return self._t(f"{ds}.{name}")
        return None

    def latest_detect_run(self, until=None):
        good, state = self._find("v_good_runs"), self._find("v_item_state_current")
        if good is None or state is None:
            return None
        since = " AND g.run_date <= @until" if until else ""
        params = {"until": ("DATE", until)} if until else {}
        rows = self._query(f"SELECT g.run_date, g.run_id FROM {good} g WHERE g.stage = 'detect'{since} "
                           "ORDER BY g.run_date DESC LIMIT 1", **params)
        return {"run_id": rows[0]["run_id"], "run_date": rows[0]["run_date"]} if rows else None

    def item_states(self, run, market):
        """item_state rows of one run; a missing map, series-test view or breakout table leaves its columns null,
        never fails. breakout_creators is the item's largest creator count in breakout_signals for the run's date
        in that market, over any run that day. tone_today and tone_before are the item's tone in that market on
        the run's date and the day before, from L2's v_item_tone_daily, read by _tones in a query of its own so a
        tone failure never fails the page: NULL under 5 enriched posts, until the view exists, or when that read
        fails. After a rollback the view can lose market_news_posts7 while this process's catalog still lists it:
        the read then fails with "Unrecognized name" or, for the qualified column this query selects, "Name
        market_news_posts7 not found inside t", so the catalog is dropped and the read made once more."""
        try:
            return self._item_states(run, market)
        except Exception as e:
            text = str(e)
            if "market_news_posts7" not in text or not ("Unrecognized name" in text or "not found inside" in text):
                raise
            with _CATALOG_LOCK:
                _CATALOG.pop(self.project, None)
            return self._item_states(run, market)

    def _item_states(self, run, market):
        tests, cmap = self._find("v_series_test_current"), self._find("cultural_map")
        breakouts, tones = self._find("breakout_signals"), self._find("v_item_tone_daily")
        objects = self._catalog()["objects"]
        scope_view = self._t(CORE + ".v_item_market_scope") if CORE + ".v_item_market_scope" in objects else None
        ctes, joins = [], []
        if tests:
            ctes.extend(("pl AS (SELECT t.item_id, t.market, ARRAY_AGG(DISTINCT t.platform IGNORE NULLS) AS platforms "
                         f"FROM {tests} t WHERE t.metric_date = @d GROUP BY t.item_id, t.market)",
                         f"mv AS (SELECT t.series_id, t.vel, t.platform FROM {tests} t WHERE t.metric_date = @d)"))
            joins.extend(("LEFT JOIN mv ON mv.series_id = s.main_series_id",
                          "LEFT JOIN pl ON pl.item_id = s.item_id AND pl.market = s.market"))
            series = "mv.vel AS main_vel, mv.platform AS main_platform, pl.platforms"
        else:
            series = ("CAST(NULL AS FLOAT64) AS main_vel, CAST(NULL AS STRING) AS main_platform, "
                      "CAST([] AS ARRAY<STRING>) AS platforms")
        if cmap:
            labels = "cm.label, cm.canonical_key, cm.aliases, cm.status AS map_status, cm.first_seen"
            joins.append(f"LEFT JOIN {cmap} cm ON cm.item_id = s.item_id AND cm.valid_to IS NULL")
        else:
            labels = ("s.item_id AS label, CAST(NULL AS STRING) AS canonical_key, CAST([] AS ARRAY<STRING>) AS aliases, "
                      "'unknown' AS map_status, CAST(NULL AS DATE) AS first_seen")
        if scope_view:
            # The view gains market_news_posts7 when detect next re-creates it; until then the count is unknown.
            news = "market_news_posts7" in self._catalog().get("scope_columns", ())
            ctes.append(f"ms AS (SELECT t.item_id, t.market, t.metric_date, t.market_scope, t.market_posts7, "
                        f"{'t.market_news_posts7, ' if news else ''}t.total_posts7, t.market_share7 "
                        f"FROM {scope_view} t WHERE t.metric_date = @d)")
            joins.append("LEFT JOIN ms ON ms.item_id = s.item_id AND ms.market = s.market "
                         "AND ms.metric_date = s.metric_date")
            scope = "IF(ms.market_scope = 'market', 'market', 'global')"
            scope_basis = ("ms.market_posts7, " + ("ms.market_news_posts7" if news
                           else "CAST(NULL AS INT64) AS market_news_posts7") + ", ms.total_posts7, ms.market_share7")
        else:
            scope = "CAST(NULL AS STRING)"
            scope_basis = ("CAST(NULL AS INT64) AS market_posts7, CAST(NULL AS INT64) AS market_news_posts7, "
                           "CAST(NULL AS INT64) AS total_posts7, CAST(NULL AS FLOAT64) AS market_share7")
        if breakouts:
            ctes.append(f"bo AS (SELECT b.item_id, b.market, MAX(b.creators) AS breakout_creators FROM {breakouts} b "
                        "WHERE b.metric_date = @d GROUP BY b.item_id, b.market)")
            joins.append("LEFT JOIN bo ON bo.item_id = s.item_id AND bo.market = s.market")
            breakout = "bo.breakout_creators"
        else:
            breakout = "CAST(NULL AS INT64) AS breakout_creators"
        withs = "WITH " + ", ".join(ctes) + " " if ctes else ""
        rows = self._query(
            f"{withs}SELECT s.*, {labels}, {series}, {scope} AS market_scope, {scope_basis}, {breakout} "
            f"FROM {self._t(CORE + '.v_item_state_current')} s {' '.join(joins)} "
            "WHERE s.metric_date = @d AND s.run_id = @run_id AND (@market = 'all' OR s.market = @market)",
            d=("DATE", run["run_date"]), run_id=("STRING", run["run_id"]), market=("STRING", market))
        found = self._tones(tones, run["run_date"]) if tones and rows else {}
        before = (dt.date.fromisoformat(run["run_date"]) - dt.timedelta(days=1)).isoformat()
        return [dict(r, tone_today=found.get((r.get("item_id"), r.get("market"), run["run_date"])),
                     tone_before=found.get((r.get("item_id"), r.get("market"), before))) for r in rows]

    def _tones(self, view, run_date):
        """{(item_id, market, metric_date): tone} from v_item_tone_daily for the run's date and the day before, or
        {} with a warning when the read fails: Discover and alerts never fail because of tone."""
        before = (dt.date.fromisoformat(run_date) - dt.timedelta(days=1)).isoformat()
        try:
            rows = self._query(f"SELECT t.item_id, t.market, t.metric_date, t.tone FROM {view} t "
                               "WHERE t.metric_date IN (@d, @d_before)",
                               d=("DATE", run_date), d_before=("DATE", before))
        except Exception as e:
            log.warning("tone read failed, tones left empty: %s: %s", type(e).__name__, e)
            return {}
        return {(r["item_id"], r["market"], str(r["metric_date"])): r["tone"] for r in rows}

    def watch_matches(self, run):
        """{watch_id: [item_id]} from L2's detect run for the run's date, or None until the table exists."""
        matches = self._find("watch_matches")
        if matches is None:
            return None
        out = {}
        for r in self._query(f"SELECT m.watch_id, m.item_id FROM {matches} m WHERE m.match_date = @d "
                             "ORDER BY m.watch_id, m.item_id", d=("DATE", run["run_date"])):
            out.setdefault(r["watch_id"], []).append(r["item_id"])
        return out

    def item_gate(self, market):
        gate = self._find("v_item_gate_current")
        if gate is None:
            return None
        return self._query(f"SELECT g.item_id, g.market, g.brief_date, g.place, g.rule, g.reason, g.reason_text "
                           f"FROM {gate} g "
                           "WHERE @market = 'all' OR g.market = @market", market=("STRING", market))

    def item_history(self, item_id, market, start, end):
        return self._query(
            f"SELECT s.metric_date, s.state, s.creators3 FROM {self._t(CORE + '.v_item_state_current')} s "
            "WHERE s.item_id = @item_id AND s.market = @market AND s.metric_date BETWEEN @start AND @end "
            "ORDER BY s.metric_date",
            item_id=("STRING", item_id), market=("STRING", market), start=("DATE", start), end=("DATE", end))

    def item_series(self, item_id, market, days):
        tvf = self._find("tvf_item_timeseries")
        if tvf is None:
            return None
        return self._query(f"SELECT t.* FROM {tvf}(@item_id, @market, @days) t",
                           item_id=("STRING", item_id), market=("STRING", market), days=("INT64", days))

    def item_waves(self, item_id, market):
        waves = self._find("v_item_waves")
        if waves is None:
            return None
        return self._query(f"SELECT w.wave_start, w.wave_end, w.peak_date, w.peak_posts FROM {waves} w "
                           "WHERE w.item_id = @item_id AND w.market = @market ORDER BY w.peak_date DESC",
                           item_id=("STRING", item_id), market=("STRING", market))

    def coord_signals(self, item_id, market, date):
        signals = self._find("v_coord_signals_current")
        if signals is None:
            return None
        return self._query(
            f"SELECT c.signal, c.component_id, c.accounts, c.item_posts_share, c.run_id FROM {signals} c "
            "WHERE c.item_id = @item_id AND c.market = @market AND c.metric_date = @d ORDER BY c.signal",
            item_id=("STRING", item_id), market=("STRING", market), d=("DATE", date))

    def item_spread(self, date, market, item_id=None):
        """v_item_spread rows for the date, one market (or all) and one item (or every item), or None until the
        view exists or when its read fails (logged). Each platform's first-seen date comes back as an ISO string."""
        spread = self._find("v_item_spread")
        if spread is None:
            return None
        try:
            rows = self._query(
                f"SELECT s.item_id, s.market, s.metric_date, s.tiers, s.platform_first_seen, s.spread_line "
                f"FROM {spread} s WHERE s.metric_date = @d AND (@market = 'all' OR s.market = @market) "
                "AND (@item_id IS NULL OR s.item_id = @item_id)",
                d=("DATE", date), market=("STRING", market), item_id=("STRING", item_id))
        except Exception as e:  # noqa: BLE001 - spread is optional; Discover, alerts and topics must not fail on it
            print(f"store: v_item_spread read failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None
        return [dict(r, platform_first_seen=[_value(p, MARKET_TZ["ZA"])
                                             for p in r.get("platform_first_seen") or []]) for r in rows]

    def item_reach(self, date, market, item_ids):
        """Each item's reach in the market over the 7 days ending date, each post dated by its first sighting there
        (as tvf_item_window and item_daily date it, scanning 28 days) and counted once, every collection lane counted
        (legacy, placebo and agent_live aside): posts7, creators7 (unflagged, coord_score under 1, as creators3),
        measured_posts7 and measured_creators7 (only the rank and panel lanes, which no search of ours steers),
        views7 and engagement7 with views_posts7 and engagement_posts7 (how many posts carried the value), platforms,
        located7 (posts whose place is known: geo_confidence 0.7 or more from ext_region, home_market or
        place_mention, or sighted on one of the market's own feeds), local7 (those located in the market or sighted on
        its own feeds, the rule v_item_market_scope uses) and own_feed7. None without the tables or when the read
        fails (logged): Radar then shows no measures."""
        obs, items, posts = self._find("post_observations"), self._find("post_items"), self._find("posts")
        creators = self._find("creators")
        if obs is None or items is None or posts is None or creators is None or not item_ids:
            return None
        try:
            return self._query(
                "WITH o AS (SELECT pi.item_id, po.post_id, MIN(po.observed_date) first_day, "
                "IFNULL(LOGICAL_OR(po.source_market = @market), FALSE) own_feed, "
                "LOGICAL_OR(po.lane_class IN ('unbiased_rank', 'panel')) measured, MAX(po.views) obs_views "
                f"FROM {obs} po JOIN {items} pi ON pi.post_id = po.post_id "
                "WHERE po.market = @market AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 27 DAY) AND @d "
                "AND po.lane_class != 'legacy' AND IFNULL(po.lane, '') NOT IN ('placebo', 'agent_live') "
                "AND pi.item_id IN UNNEST(@item_ids) GROUP BY pi.item_id, po.post_id "
                "HAVING MIN(po.observed_date) > DATE_SUB(@d, INTERVAL 7 DAY)), "
                "p AS (SELECT ps.post_id, ANY_VALUE(ps.platform) platform, ANY_VALUE(ps.creator_id) creator_id, "
                "MAX(ps.views) views, MAX(ps.engagement) engagement, "
                "LOGICAL_OR(IFNULL(ps.geo_confidence >= 0.7 "
                "AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)) known, "
                "LOGICAL_OR(IFNULL(ps.geo_confidence >= 0.7 AND UPPER(ps.geo_market) = @market "
                "AND ps.geo_source IN ('ext_region', 'home_market', 'place_mention'), FALSE)) in_market "
                f"FROM {posts} ps WHERE ps.post_id IN (SELECT o.post_id FROM o) GROUP BY ps.post_id), "
                "fl AS (SELECT c.creator_id, MAX(IFNULL(c.coord_score, 0)) >= 1 flagged "
                f"FROM {creators} c WHERE c.creator_id IN (SELECT p.creator_id FROM p) GROUP BY c.creator_id), "
                "j AS (SELECT o.*, p.platform, p.creator_id, p.engagement, p.known, p.in_market, "
                "COALESCE(p.views, o.obs_views) post_views, IFNULL(fl.flagged, FALSE) flagged "
                "FROM o JOIN p ON p.post_id = o.post_id LEFT JOIN fl ON fl.creator_id = p.creator_id) "
                "SELECT j.item_id, COUNT(DISTINCT j.post_id) posts7, "
                "COUNT(DISTINCT IF(j.flagged, NULL, j.creator_id)) creators7, "
                "COUNT(DISTINCT IF(j.measured, j.post_id, NULL)) measured_posts7, "
                "COUNT(DISTINCT IF(j.measured AND NOT j.flagged, j.creator_id, NULL)) measured_creators7, "
                "SUM(j.post_views) views7, COUNTIF(j.post_views IS NOT NULL) views_posts7, "
                "SUM(j.engagement) engagement7, COUNTIF(j.engagement IS NOT NULL) engagement_posts7, "
                "ARRAY_AGG(DISTINCT j.platform IGNORE NULLS) platforms, COUNTIF(j.known OR j.own_feed) located7, "
                "COUNTIF(j.in_market OR j.own_feed) local7, COUNTIF(j.own_feed) own_feed7 "
                "FROM j GROUP BY j.item_id",
                d=("DATE", date), market=("STRING", market), item_ids=("ARRAY<STRING>", sorted(item_ids)))
        except Exception as e:  # noqa: BLE001 - reach is optional; Discover and Radar must not fail on it
            print(f"store: item reach read failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None

    def health_days(self, market, start, end):
        """Per platform, the days from start to end that have baseline health rows in the market and how many of
        them were valid on every baseline series (unbiased_rank, panel, unbiased_counter), as the gate reads a day.
        None without the view or when the read fails (logged)."""
        health = self._find("v_collection_health_current")
        if health is None:
            return None
        try:
            return self._query(
                "SELECT d.platform, COUNT(*) AS days, COUNTIF(d.ok) AS days_ok FROM (SELECT h.platform, h.day, "
                f"LOGICAL_AND(IFNULL(h.valid, FALSE)) ok FROM {health} h WHERE h.market = @market "
                "AND h.day BETWEEN @start AND @end AND h.platform IS NOT NULL "
                "AND h.lane_class IN ('unbiased_rank', 'panel', 'unbiased_counter') GROUP BY h.platform, h.day) d "
                "GROUP BY d.platform ORDER BY d.platform",
                market=("STRING", market), start=("DATE", start), end=("DATE", end))
        except Exception as e:  # noqa: BLE001 - health is optional here; Radar must not fail on it
            print(f"store: health days read failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None

    def creator_names(self, keys):
        """{creator item canonical_key: creators row} for "platform:creator id" keys as cultural_map folds them
        (lower case), the row with the most followers, carrying creator_id, platform, handle and display_name.
        None without the table or when the read fails (logged)."""
        creators = self._find("creators")
        if creators is None or not keys:
            return None
        try:
            rows = self._query(
                "SELECT CONCAT(LOWER(TRIM(c.platform)), ':', LOWER(TRIM(c.creator_id))) AS key, c.creator_id, "
                f"c.platform, c.handle, c.display_name FROM {creators} c "
                "WHERE CONCAT(LOWER(TRIM(c.platform)), ':', LOWER(TRIM(c.creator_id))) IN UNNEST(@keys) "
                "QUALIFY ROW_NUMBER() OVER (PARTITION BY LOWER(TRIM(c.platform)), LOWER(TRIM(c.creator_id)) "
                "ORDER BY c.display_name IS NULL, c.followers DESC) = 1",
                keys=("ARRAY<STRING>", sorted(keys)))
        except Exception as e:  # noqa: BLE001 - names are optional; Discover must not fail on them
            print(f"store: creator names read failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None
        return {r["key"]: r for r in rows}

    def item_origin(self, item_id, market):
        """v_item_origin's row for the item and market (the view holds one), or None until the view exists or when
        its read fails (logged)."""
        origin = self._find("v_item_origin")
        if origin is None:
            return None
        try:
            return self._query(
                "SELECT o.item_id, o.market, o.origin, o.first_measured, o.after_collection_began, o.first_state_day, "
                f"o.lead_news_day, o.lag_days FROM {origin} o WHERE o.item_id = @item_id AND o.market = @market",
                item_id=("STRING", item_id), market=("STRING", market))
        except Exception as e:  # noqa: BLE001 - origin is optional; a topic page must not fail on it
            print(f"store: v_item_origin read failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None

    def news_followthrough(self, item_id, market, until):
        """v_news_followthrough rows in the market whose seed is the item or matched it (matched_items), queued on or
        before `until`, newest first and at most 20; None until the view exists or when its read fails (logged)."""
        news = self._find("v_news_followthrough")
        if news is None:
            return None
        try:
            return self._query(
                "SELECT n.market, n.news_day, n.news_day_from, n.seed_date, n.item_id, n.label, n.kind, "
                "n.matched_items, n.collect_ran, n.reached_state, n.first_state_day, n.first_measured, n.lag_days "
                f"FROM {news} n WHERE n.market = @market AND n.seed_date <= @d AND (n.item_id = @item_id "
                "OR EXISTS (SELECT 1 FROM UNNEST(n.matched_items) m WHERE m.item_id = @item_id)) "
                "ORDER BY n.seed_date DESC, n.item_id LIMIT 20",
                item_id=("STRING", item_id), market=("STRING", market), d=("DATE", until))
        except Exception as e:  # noqa: BLE001 - news is optional; a topic page must not fail on it
            print(f"store: v_news_followthrough read failed: {type(e).__name__}: {e}", file=sys.stderr)
            return None

    def item_evidence(self, item_id, market):
        evidence = self._find("v_item_evidence")
        if evidence is None:
            return None
        return _in_market(self._query(f"SELECT e.* FROM {evidence} e WHERE e.item_id = @item_id LIMIT 200",
                                      item_id=("STRING", item_id)), market)

    def credits(self, date):
        ledger = self._find("credit_ledger")
        if ledger is None:
            return None
        return self._query(
            "SELECT l.market, l.job, l.lane, SUM(l.credits_charged) AS charged, SUM(l.calls) AS calls, "
            "ARRAY_AGG(DISTINCT l.run_id IGNORE NULLS ORDER BY l.run_id) AS run_ids "
            f"FROM {ledger} l WHERE l.trend_date = @d GROUP BY l.market, l.job, l.lane "
            "ORDER BY l.market, l.job, l.lane",
            d=("DATE", date))

    def runs_of_day(self, date):
        columns = self._catalog()["runs_columns"]
        parts = []
        if "model_usd" in columns:
            parts.append("r.model_usd")
        if "record" in columns:
            parts.append("SAFE_CAST(JSON_VALUE(r.record, '$.run.model_usd') AS FLOAT64)")
        if "counts" in columns:
            parts.append("SAFE_CAST(JSON_VALUE(r.counts, '$.model_usd') AS FLOAT64)")
        usd = f"MAX(COALESCE({', '.join(parts)})) OVER (PARTITION BY r.run_id)" if parts else "CAST(NULL AS FLOAT64)"
        # The writes a run that finished ok could not make (core/understand/job.py), from its latest row's counts.
        degraded = f", {DEGRADED_SQL} AS degraded" if "counts" in columns else ""
        # A run appends several rows; the latest one carries its status.
        return self._query(
            f"SELECT r.run_id, r.stage, r.status, r.started_at, r.finished_at, r.error, {usd} AS model_usd{degraded} "
            f"FROM {self._t(AGENT + '.runs')} r WHERE r.run_date = @d "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY r.run_id "
            "ORDER BY r.finished_at IS NULL, r.finished_at DESC, r.started_at DESC) = 1 "
            "ORDER BY r.started_at",
            d=("DATE", date))

    def has_model_usd(self):
        return bool(self._catalog()["runs_columns"] & {"model_usd", "record", "counts"})

    def scorecard(self):
        card = self._find("engine_scorecard")
        if card is None:
            return None
        # One row per market per week; the latest week_start holds the current scorecard.
        rows = self._query(f"SELECT s.* FROM {card} s "
                           f"WHERE s.week_start = (SELECT MAX(w.week_start) FROM {card} w) ORDER BY s.market")
        return rows or None

    # Compare (contract.md section 11).

    def latest_aggregate_run(self):
        good = self._find("v_good_runs")
        if good is None:
            return None
        rows = self._query(f"SELECT g.run_date, g.run_id FROM {good} g WHERE g.stage = 'aggregate' "
                           "ORDER BY g.run_date DESC LIMIT 1")
        return {"run_id": rows[0]["run_id"], "run_date": rows[0]["run_date"]} if rows else None

    def map_items(self, item_ids):
        cmap = self._find("cultural_map")
        if cmap is None:
            return None
        return self._query(
            "SELECT cm.item_id, cm.kind, cm.canonical_key, cm.label, cm.aliases, cm.status "
            f"FROM {cmap} cm WHERE cm.valid_to IS NULL AND cm.item_id IN UNNEST(@item_ids)",
            item_ids=("ARRAY<STRING>", item_ids))

    def breaking_signals(self, since):
        """The hourly Breaking rule's rows of ok runs (v_breaking_signals_current) whose hour is at or after since,
        newest first. None while the view does not exist."""
        view = self._find("v_breaking_signals_current")
        if view is None:
            return None
        return self._query(
            "SELECT s.hour, s.market, s.item_id, s.posts6, s.creators6, s.expected6, s.ratio, s.platforms, s.run_id "
            f"FROM {view} s WHERE s.hour >= @since ORDER BY s.hour DESC, s.market, s.item_id LIMIT 200",
            since=("TIMESTAMP", since))

    def search_signals(self, start, end, markets):
        """google_search_signals rows (Google search interest collect wrote) of the given markets fetched on SAST
        days start to end, each with its SAST fetch_day, newest first. None while the table does not exist."""
        table = self._find("google_search_signals")
        if table is None:
            return None
        return self._query(
            "SELECT s.market, s.term, s.source, s.rank, s.refreshed_at, "
            "DATE(s.fetched_at, 'Africa/Johannesburg') AS fetch_day "
            f"FROM {table} s WHERE s.fetched_at >= TIMESTAMP(@start, 'Africa/Johannesburg') "
            "AND s.fetched_at < TIMESTAMP(DATE_ADD(@end, INTERVAL 1 DAY), 'Africa/Johannesburg') "
            "AND s.market IN UNNEST(@markets) ORDER BY s.fetched_at DESC, s.market, s.source, s.term LIMIT 5000",
            start=("DATE", start), end=("DATE", end), markets=("ARRAY<STRING>", list(markets)))

    def health_range(self, start, end, markets):
        health = self._find("v_collection_health_current")
        if health is None:
            return None
        return self._query(
            f"SELECT h.day, h.market, h.platform, h.series, h.protocol, h.valid FROM {health} h "
            "WHERE h.day BETWEEN @start AND @end AND h.market IN UNNEST(@markets)",
            start=("DATE", start), end=("DATE", end), markets=("ARRAY<STRING>", markets))

    def compare_counts(self, subjects, start, end):
        """Per subject one 'total' row (posts, creators, engagement, platforms in the window; first_seen all time)
        and one 'day' row per first-sighting day in the window with posts. None until the three tables exist.

        Each post's first sighting per market is taken over all of post_observations, legacy and agent_live
        sightings aside, and only then clipped to the window; posts is joined by post_id with no post_date
        filter. Linked subjects (terms None) are found through post_items, the rest by whole-word match of any
        term in posts.text or posts.transcript as well as through post_items, so a topic counts the posts detect linked
        to it (the population its card's reach and state read) plus the posts that name it. engagement sums likes, comments and shares over the posts that report
        any of them, counted in engagement_posts; it is null when the subject has posts and none reports one."""
        posts, obs, links = self._find("posts"), self._find("post_observations"), self._find("post_items")
        if posts is None or obs is None or links is None:
            return None
        params = {"start": ("DATE", start), "end": ("DATE", end),
                  "markets": ("ARRAY<STRING>", sorted({s["market"] for s in subjects}))}
        selects = []
        for n, s in enumerate(subjects):
            selects.append(f"SELECT @k{n} AS key, @i{n} AS item_id, @m{n} AS market, @p{n} AS platform, "
                           f"@r{n} AS pattern")
            params.update({f"k{n}": ("STRING", s["key"]), f"i{n}": ("STRING", s["item_id"]),
                           f"m{n}": ("STRING", s["market"]), f"p{n}": ("STRING", s["platform"]),
                           f"r{n}": ("STRING", None if s["terms"] is None else _keyword_re2(s["terms"]))})
        subj = " UNION ALL ".join(selects)
        return self._query(
            f"WITH subj AS ({subj}),\n"
            "sight AS (\n"
            "  SELECT po.post_id, po.market, MIN(po.observed_date) AS first_day\n"
            f"  FROM {obs} po\n"
            "  WHERE po.market IN UNNEST(@markets) AND po.lane_class != 'legacy'\n"
            "    AND IFNULL(po.lane, '') NOT IN ('legacy', 'agent_live')\n"
            "  GROUP BY po.post_id, po.market),\n"
            "linked AS (\n"
            f"  SELECT DISTINCT s.key, pit.post_id FROM subj s JOIN {links} pit ON pit.item_id = s.item_id),\n"
            "named AS (\n"
            f"  SELECT DISTINCT s.key, p.post_id FROM subj s CROSS JOIN {posts} p\n"
            "  WHERE s.pattern IS NOT NULL\n"
            "    AND (REGEXP_CONTAINS(IFNULL(p.text, ''), s.pattern)\n"
            "      OR REGEXP_CONTAINS(IFNULL(p.transcript, ''), s.pattern))),\n"
            "hits AS (\n"
            "  SELECT s.key, p.post_id, p.creator_id, IF(p.platform = 'twitter', 'x', p.platform) AS platform, sg.first_day,\n"
            "    IF(COALESCE(p.likes, p.comments, p.shares) IS NULL, NULL,\n"
            "      IFNULL(p.likes, 0) + IFNULL(p.comments, 0) + IFNULL(p.shares, 0)) AS engagement\n"
            "  FROM (SELECT l.key, l.post_id FROM linked l UNION DISTINCT SELECT n.key, n.post_id FROM named n) m\n"
            "  JOIN subj s ON s.key = m.key\n"
            "  JOIN sight sg ON sg.post_id = m.post_id AND sg.market = s.market\n"
            f"  JOIN {posts} p ON p.post_id = m.post_id\n"
            "  WHERE s.platform IS NULL OR IF(p.platform = 'twitter', 'x', p.platform) = s.platform),\n"
            "win AS (SELECT h.* FROM hits h WHERE h.first_day BETWEEN @start AND @end),\n"
            "totals AS (\n"
            "  SELECT w.key, COUNT(DISTINCT w.post_id) AS posts, COUNT(DISTINCT w.creator_id) AS creators,\n"
            "    SUM(w.engagement) AS engagement,\n"
            "    COUNT(DISTINCT IF(w.engagement IS NULL, NULL, w.post_id)) AS engagement_posts,\n"
            "    COUNT(DISTINCT w.platform) AS platforms\n"
            "  FROM win w GROUP BY w.key),\n"
            "firsts AS (SELECT h.key, MIN(h.first_day) AS first_seen FROM hits h GROUP BY h.key),\n"
            "daily AS (SELECT w.key, w.first_day AS day, COUNT(DISTINCT w.post_id) AS posts\n"
            "  FROM win w GROUP BY w.key, w.first_day)\n"
            "SELECT 'total' AS grain, s.key, CAST(NULL AS DATE) AS day, IFNULL(t.posts, 0) AS posts,\n"
            "  IFNULL(t.creators, 0) AS creators,\n"
            "  IF(IFNULL(t.posts, 0) > 0 AND t.engagement_posts = 0, NULL, IFNULL(t.engagement, 0)) AS engagement,\n"
            "  IFNULL(t.engagement_posts, 0) AS engagement_posts, IFNULL(t.platforms, 0) AS platforms, f.first_seen\n"
            "FROM subj s LEFT JOIN totals t ON t.key = s.key LEFT JOIN firsts f ON f.key = s.key\n"
            "UNION ALL\n"
            "SELECT 'day', d.key, d.day, d.posts, CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64),\n"
            "  CAST(NULL AS INT64), CAST(NULL AS DATE)\n"
            "FROM daily d\n"
            "ORDER BY grain DESC, key, day",
            **params)

    # Creators, communities and history (contract.md section 12). creators.coord_score is never selected.

    def creator(self, creator_id):
        return next(iter(self.creators_by_id([creator_id]) or []), None)

    def creators_by_id(self, creator_ids):
        creators = self._find("creators")
        if creators is None:
            return None
        return self._query(
            f"SELECT {', '.join('c.' + k for k in CREATOR_COLUMNS)} FROM {creators} c "
            "WHERE c.creator_id IN UNNEST(@creator_ids) "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY c.creator_id ORDER BY c.followers DESC) = 1",
            creator_ids=("ARRAY<STRING>", list(creator_ids)))

    def creators_by_handle(self, keys):
        """Creators rows whose creator_key (store.creator_key, in SQL) is one of keys, or None without the table."""
        creators = self._find("creators")
        if creators is None:
            return None
        return self._query(
            f"SELECT {', '.join('c.' + k for k in CREATOR_COLUMNS)} FROM {creators} c\n"
            # Platform read the way L1's v_suppressed_creators reads it, so a count here agrees with who is hidden.
            "WHERE CONCAT(IF(LOWER(TRIM(c.platform)) = 'twitter', 'x', LOWER(TRIM(c.platform))), ':',\n"
            "  LOWER(REGEXP_REPLACE(TRIM(c.handle), r'^@*(u/)?', ''))) IN UNNEST(@keys)",
            keys=("ARRAY<STRING>", sorted(keys)))

    def detect_states(self, start, end):
        """The gate's item_state columns for every good detect run dated inside the window, or None."""
        good, state = self._find("v_good_runs"), self._find("v_item_state_current")
        if good is None or state is None:
            return None
        return self._query(
            "SELECT s.item_id, s.market, s.run_id, s.metric_date, s.eligible, s.authenticity, s.sponsored_share,\n"
            f"  s.geo_status\nFROM {state} s\nJOIN {good} g ON g.run_id = s.run_id AND g.run_date = s.metric_date\n"
            "WHERE g.stage = 'detect' AND g.run_date BETWEEN @start AND @end",
            start=("DATE", start), end=("DATE", end))

    def sensitive_complete(self):
        """Whether L2's complete sensitive set (keyword list plus model classification) exists; only it switches
        creator item lists, recent posts and named communities on (contract.md section 12.1 rule 2)."""
        return f"{CORE}.v_sensitive_items_complete" in self._catalog()["objects"]

    def sensitive_items(self):
        """The union of every sensitive source that exists: v_sensitive_items_complete, v_sensitive_items and
        cultural_map.sensitive, so an item on any list is excluded; None until any exists."""
        views = [self._t(f"{CORE}.v_sensitive_items_complete")] if self.sensitive_complete() else []
        views.append(self._find("v_sensitive_items"))
        reads = [f"SELECT DISTINCT s.item_id FROM {view} s" for view in views if view is not None]
        if "sensitive" in self._catalog()["map_columns"] and self._find("cultural_map"):
            reads.append(f"SELECT DISTINCT cm.item_id FROM {self._find('cultural_map')} cm "
                         "WHERE cm.valid_to IS NULL AND cm.sensitive")
        if not reads:
            return None
        # The sources are read together (they used to be read one after another); the set is their union as before.
        with ThreadPoolExecutor(max_workers=len(reads), thread_name_prefix="f42-sensitive") as pool:
            return {r["item_id"] for rows in pool.map(self._query, reads) for r in rows}

    def generic_items(self):
        cmap = self._find("cultural_map")
        if cmap is None or "status" not in self._catalog()["map_columns"]:
            return set()
        return {r["item_id"] for r in self._query(
            f"SELECT DISTINCT cm.item_id FROM {cmap} cm WHERE cm.valid_to IS NULL AND cm.status = 'generic'")}

    def board_items(self, market, start, end):
        counters = self._find("item_counter_daily")
        if counters is None:
            return set()
        return {r["item_id"] for r in self._query(
            f"SELECT DISTINCT i.item_id FROM {counters} i WHERE i.is_board AND i.market = @market "
            "AND i.obs_date BETWEEN @start AND @end",
            market=("STRING", market), start=("DATE", start), end=("DATE", end))}

    def suppressed_creators(self):
        view = self._find("v_suppressed_creators")
        if view is None:
            return None
        return {r["creator_id"] for r in self._query(f"SELECT DISTINCT s.creator_id FROM {view} s")}

    def suppressions(self):
        """L1's suppression list as f42-web reads it (contract.md section 16): the current row per suppression_id,
        chosen as v_suppressed_creators chooses it, newest first. None while the table does not exist."""
        table = self._find("suppressions")
        if table is None:
            return None
        return self._query(
            f"SELECT {', '.join('s.' + k for k in SUPPRESSION_COLUMNS)} FROM {table} s\n"
            "WHERE TRUE\n"
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY s.suppression_id\n"
            "  ORDER BY s.status_at DESC, s.status = 'lifted', TO_JSON_STRING(s)) = 1\n"
            "ORDER BY s.status_at DESC, s.suppression_id")

    def _post_select(self, posts, links, where, extra_with="", extra_join="", extra_cols="", tail=""):
        """Post rows in one shape: post columns, the creator's handle, linked item_ids, langs and formats."""
        creators, enrich = self._find("creators"), self._find("post_enrichment")
        handle = "c.handle" if creators else "CAST(NULL AS STRING)"
        joins = extra_join + (f"LEFT JOIN (SELECT cr.creator_id, ANY_VALUE(cr.handle) AS handle FROM {creators} cr "
                              "GROUP BY cr.creator_id) c ON c.creator_id = p.creator_id\n" if creators else "")
        if enrich:
            joins += f"LEFT JOIN {enrich} e ON e.post_id = p.post_id\n"
            langs = "e.langs, e.formats"
        else:
            langs = "CAST(NULL AS ARRAY<STRING>) AS langs, CAST(NULL AS ARRAY<STRING>) AS formats"
        return (
            f"WITH li AS (SELECT pit.post_id, ARRAY_AGG(DISTINCT pit.item_id ORDER BY pit.item_id) AS item_ids\n"
            f"  FROM {links} pit GROUP BY pit.post_id){extra_with}\n"
            f"SELECT {', '.join('p.' + k for k in POST_COLUMNS)}, {handle} AS handle,\n"
            f"  IFNULL(li.item_ids, []) AS item_ids, {langs}{extra_cols}\n"
            f"FROM {posts} p\nLEFT JOIN li ON li.post_id = p.post_id\n{joins}WHERE {where}{tail}")

    def creator_posts(self, creator_id, excluded, limit):
        posts, links = self._find("posts"), self._find("post_items")
        if posts is None or links is None:
            return None
        sql = self._post_select(
            posts, links,
            "p.creator_id = @creator_id\n  AND NOT EXISTS (SELECT 1 FROM UNNEST(IFNULL(li.item_ids, [])) i "
            "WHERE i IN UNNEST(@excluded))",
            tail="\nORDER BY p.published_at DESC, p.post_id DESC LIMIT @limit")
        return self._query(sql, creator_id=("STRING", creator_id), excluded=("ARRAY<STRING>", sorted(excluded)),
                           limit=("INT64", limit))

    def _sight(self, obs, only=None):
        """Each post's first sighting day in the market; with only (a CTE with post_id), just those posts' days."""
        among = f"    AND po.post_id IN (SELECT o.post_id FROM {only} o)\n" if only else ""
        return ("SELECT po.post_id, MIN(po.observed_date) AS first_day\n"
                f"  FROM {obs} po\n"
                "  WHERE po.market = @market AND po.lane_class != 'legacy'\n"
                "    AND IFNULL(po.lane, '') NOT IN ('legacy', 'agent_live')\n"
                f"{among}"
                "  GROUP BY po.post_id")

    def window_posts(self, creator_ids, market, start, end):
        """Posts of these creators first sighted in the market inside the window, the first sighting taken over
        all of post_observations (legacy and agent_live aside), as Compare counts."""
        posts, obs, links = self._find("posts"), self._find("post_observations"), self._find("post_items")
        if posts is None or obs is None or links is None:
            return None
        sql = self._post_select(
            posts, links, "p.creator_id IN UNNEST(@creator_ids) AND sg.first_day BETWEEN @start AND @end",
            extra_with=f",\nsight AS ({self._sight(obs)})",
            extra_join="JOIN sight sg ON sg.post_id = p.post_id\n", extra_cols=", sg.first_day")
        return self._query(sql, creator_ids=("ARRAY<STRING>", list(creator_ids)), market=("STRING", market),
                           start=("DATE", start), end=("DATE", end))

    def community_edges(self, market, start, end, excluded):
        """Pairs of creators (a < b) sharing three or more items outside `excluded`, each over posts first sighted
        in the market inside the window. Only creators with three or more such items can have an edge."""
        posts, obs, links = self._find("posts"), self._find("post_observations"), self._find("post_items")
        if posts is None or obs is None or links is None:
            return None
        return self._query(
            f"WITH sight AS ({self._sight(obs)}),\n"
            "ci AS (\n"
            "  SELECT DISTINCT p.creator_id, pit.item_id\n"
            "  FROM sight sg\n"
            f"  JOIN {posts} p ON p.post_id = sg.post_id\n"
            f"  JOIN {links} pit ON pit.post_id = sg.post_id\n"
            "  WHERE sg.first_day BETWEEN @start AND @end AND p.creator_id IS NOT NULL\n"
            "    AND pit.item_id NOT IN UNNEST(@excluded)),\n"
            "many AS (SELECT ci.creator_id FROM ci GROUP BY ci.creator_id HAVING COUNT(*) >= 3),\n"
            "k AS (SELECT ci.creator_id, ci.item_id FROM ci JOIN many ON many.creator_id = ci.creator_id)\n"
            "SELECT a.creator_id AS creator_a, b.creator_id AS creator_b,\n"
            "  ARRAY_AGG(a.item_id ORDER BY a.item_id) AS items\n"
            "FROM k a JOIN k b ON b.item_id = a.item_id AND a.creator_id < b.creator_id\n"
            "GROUP BY a.creator_id, b.creator_id HAVING COUNT(*) >= 3\n"
            "ORDER BY creator_a, creator_b",
            market=("STRING", market), start=("DATE", start), end=("DATE", end),
            excluded=("ARRAY<STRING>", sorted(excluded)))

    def posts_by_id(self, post_ids):
        posts, links = self._find("posts"), self._find("post_items")
        if posts is None or links is None or not post_ids:
            return []
        return self._query(self._post_select(posts, links, "p.post_id IN UNNEST(@post_ids)"),
                           post_ids=("ARRAY<STRING>", list(post_ids)))

    def seed_path_posts(self, market, terms, start, end, limit):
        """Seed path (contract.md section 17): posts first sighted in the market (over all of post_observations,
        legacy and agent_live sightings aside, as Compare counts) whose text or transcript holds any term as a
        whole word, case-insensitive, or whose hashtags hold it. One 'first' row per platform (its first sighting
        day and its matching posts, all time) and one 'post' row per match first sighted inside the window, oldest
        first, at most `limit` of them. None until the three tables exist."""
        posts, obs, links = self._find("posts"), self._find("post_observations"), self._find("post_items")
        if posts is None or obs is None or links is None:
            return None
        # The matching posts first, then the first sightings of those posts only: the same rows as taking every
        # post's first sighting in the market and joining, without grouping the market's whole sightings table.
        return self._query(
            "WITH matched AS (\n"
            "  SELECT p.post_id, p.platform, p.text, p.transcript, p.hashtags\n"
            f"  FROM {posts} p\n"
            "  WHERE REGEXP_CONTAINS(IFNULL(p.text, ''), @pattern)\n"
            "    OR REGEXP_CONTAINS(IFNULL(p.transcript, ''), @pattern)\n"
            "    OR EXISTS (SELECT 1 FROM UNNEST(IFNULL(p.hashtags, [])) h\n"
            "      WHERE LOWER(LTRIM(h, '#')) IN UNNEST(@tags))),\n"
            f"sight AS ({self._sight(obs, only='matched')}),\n"
            "hits AS (\n"
            "  SELECT p.post_id, IF(p.platform = 'twitter', 'x', p.platform) AS platform, sg.first_day AS day,\n"
            "    p.text, p.transcript, p.hashtags\n"
            "  FROM sight sg\n"
            "  JOIN matched p ON p.post_id = sg.post_id),\n"
            "win AS (\n"
            "  SELECT h.* FROM hits h WHERE h.day BETWEEN @start AND @end\n"
            "  ORDER BY h.day, h.post_id LIMIT @limit),\n"
            "li AS (\n"
            "  SELECT pit.post_id, ARRAY_AGG(DISTINCT pit.item_id ORDER BY pit.item_id) AS item_ids\n"
            f"  FROM {links} pit JOIN win w ON w.post_id = pit.post_id GROUP BY pit.post_id)\n"
            "SELECT 'first' AS grain, h.platform, MIN(h.day) AS day, COUNT(DISTINCT h.post_id) AS posts,\n"
            "  CAST(NULL AS STRING) AS post_id, CAST(NULL AS STRING) AS text, CAST(NULL AS STRING) AS transcript,\n"
            "  CAST(NULL AS ARRAY<STRING>) AS hashtags, CAST(NULL AS ARRAY<STRING>) AS item_ids\n"
            "FROM hits h GROUP BY h.platform\n"
            "UNION ALL\n"
            "SELECT 'post', w.platform, w.day, CAST(NULL AS INT64), w.post_id, w.text, w.transcript,\n"
            "  IFNULL(w.hashtags, []), IFNULL(li.item_ids, [])\n"
            "FROM win w LEFT JOIN li ON li.post_id = w.post_id\n"
            "ORDER BY grain, day, platform, post_id",
            market=("STRING", market), pattern=("STRING", _keyword_re2(terms)),
            tags=("ARRAY<STRING>", sorted({t.lower() for t in terms})), start=("DATE", start),
            end=("DATE", end), limit=("INT64", limit))

    def seed_queue(self, market, since, until, limit):
        """Seeds (contract.md section 19): the market's queued rows (credits_estimate set, both yield columns null)
        of the latest seed_date that has any, and its yield rows of the latest seed_date that has any, one row per
        seed with MAX of each yield column as read_trials reads them. Placebo rows are never read. Seed dates
        between since and until only, and at most `limit` rows of each grain. None until the table exists."""
        table = self._find("seed_queue")
        if table is None:
            return None
        return self._query(
            "WITH q AS (\n"
            "  SELECT q.seed_date, q.item_id, q.query, q.kind, q.lane, q.template, q.priority, q.credits_estimate,\n"
            "    q.yield_posts, q.yield_new_creators,\n"
            "    q.credits_estimate IS NOT NULL AND q.yield_posts IS NULL AND q.yield_new_creators IS NULL AS queued,\n"
            "    q.yield_posts IS NOT NULL OR q.yield_new_creators IS NOT NULL AS yielded\n"
            f"  FROM {table} q\n"
            "  WHERE q.market = @market AND q.seed_date BETWEEN @since AND @until\n"
            "    AND IFNULL(q.lane, '') != 'placebo'),\n"
            "d AS (SELECT MAX(IF(q.queued, q.seed_date, NULL)) AS queued_date,\n"
            "  MAX(IF(q.yielded, q.seed_date, NULL)) AS yield_date FROM q),\n"
            "qd AS (\n"
            "  SELECT 'queued' AS grain, q.seed_date, q.item_id, q.query, q.kind, q.lane, q.template, q.priority,\n"
            "    q.credits_estimate, CAST(NULL AS INT64) AS yield_posts, CAST(NULL AS INT64) AS yield_new_creators\n"
            "  FROM q JOIN d ON q.seed_date = d.queued_date WHERE q.queued\n"
            "  ORDER BY q.priority DESC, q.lane, q.item_id, q.query LIMIT @limit),\n"
            "yd AS (\n"
            "  SELECT 'yield' AS grain, q.seed_date, q.item_id, q.query, q.kind, q.lane, q.template,\n"
            "    CAST(NULL AS FLOAT64) AS priority, CAST(NULL AS FLOAT64) AS credits_estimate,\n"
            "    MAX(q.yield_posts) AS yield_posts, MAX(q.yield_new_creators) AS yield_new_creators\n"
            "  FROM q JOIN d ON q.seed_date = d.yield_date WHERE q.yielded\n"
            "  GROUP BY q.seed_date, q.item_id, q.query, q.kind, q.lane, q.template\n"
            "  ORDER BY yield_posts DESC, q.lane, q.item_id, q.query LIMIT @limit)\n"
            "SELECT * FROM qd UNION ALL SELECT * FROM yd\n"
            "ORDER BY grain, seed_date",
            market=("STRING", market), since=("DATE", since), until=("DATE", until), limit=("INT64", limit))

    def waves(self, item_ids, market):
        """v_item_waves rows (every column, so above_half_days comes through once L2 adds it), or None."""
        waves = self._find("v_item_waves")
        if waves is None:
            return None
        return self._query(
            f"SELECT w.* FROM {waves} w WHERE w.item_id IN UNNEST(@item_ids) "
            "AND (@market IS NULL OR w.market = @market) ORDER BY w.item_id DESC, w.peak_date DESC",
            item_ids=("ARRAY<STRING>", list(item_ids)), market=("STRING", market))

    def item_days(self, item_ids, market, start, end):
        """Posts per day as v_item_waves counts them: the largest lane class total that day, '_any' and placebo
        search_presence sightings aside. None until v_item_daily_current exists."""
        daily, seeds = self._find("v_item_daily_current"), self._find("seed_queue")
        if daily is None:
            return None
        if seeds:
            placebo = (f"WITH pl AS (SELECT DISTINCT q.item_id, q.market, q.seed_date FROM {seeds} q "
                       "WHERE q.lane = 'placebo' AND q.item_id IN UNNEST(@item_ids) AND q.market = @market)\n")
            join = "LEFT JOIN pl ON pl.item_id = i.item_id AND pl.market = i.market AND pl.seed_date = i.metric_date\n"
            drop = " AND NOT (i.lane_class = 'search_presence' AND pl.item_id IS NOT NULL)"
        else:
            placebo, join, drop = "", "", ""
        return self._query(
            f"{placebo}SELECT lc.item_id, lc.market, lc.metric_date, MAX(lc.posts) AS posts FROM (\n"
            "  SELECT i.item_id, i.market, i.metric_date, i.lane_class, SUM(i.posts) AS posts\n"
            f"  FROM {daily} i\n  {join}"
            "  WHERE i.item_id IN UNNEST(@item_ids) AND i.market = @market AND i.metric_date BETWEEN @start AND @end\n"
            f"    AND i.lane_class != '_any'{drop}\n"
            "  GROUP BY i.item_id, i.market, i.metric_date, i.lane_class) lc\n"
            "GROUP BY lc.item_id, lc.market, lc.metric_date HAVING MAX(lc.posts) > 0\n"
            "ORDER BY lc.item_id, lc.metric_date",
            item_ids=("ARRAY<STRING>", list(item_ids)), market=("STRING", market), start=("DATE", start),
            end=("DATE", end))

    def nearest_items(self, item_id, k):
        """Up to k items whose stored cultural_map centroid is nearest by cosine distance (VECTOR_SEARCH over
        stored vectors, no embedding call), or None while the item has no stored centroid."""
        cmap = self._find("cultural_map")
        if cmap is None or "centroid" not in self._catalog()["map_columns"]:
            return None
        has = self._query(f"SELECT COUNT(*) AS n FROM {cmap} cm WHERE cm.valid_to IS NULL "
                          "AND cm.item_id = @item_id AND ARRAY_LENGTH(cm.centroid) > 0", item_id=("STRING", item_id))
        if not has or not has[0].get("n"):
            return None
        return self._query(
            "SELECT v.base.item_id AS item_id, v.distance FROM VECTOR_SEARCH(\n"
            f"  (SELECT cm.item_id, cm.centroid FROM {cmap} cm WHERE cm.valid_to IS NULL\n"
            "     AND ARRAY_LENGTH(cm.centroid) > 0 AND cm.item_id != @item_id),\n"
            "  'centroid',\n"
            f"  (SELECT cm.centroid FROM {cmap} cm WHERE cm.valid_to IS NULL AND cm.item_id = @item_id\n"
            "     AND ARRAY_LENGTH(cm.centroid) > 0 LIMIT 1),\n"
            f"  top_k => {int(k)}, distance_type => 'COSINE') v\n"
            "ORDER BY v.distance, item_id",
            item_id=("STRING", item_id))

    def search_items(self, q, limit):
        """Labels, canonical keys and, once cultural_map has the column, aliases matching q."""
        cmap = self._find("cultural_map")
        if cmap is None:
            return None
        if "aliases" in self._catalog()["map_columns"]:
            aliases = "cm.aliases"
            alias_match = "\n  OR EXISTS (SELECT 1 FROM UNNEST(cm.aliases) a WHERE STRPOS(LOWER(a), LOWER(@q)) > 0)"
        else:
            aliases, alias_match = "CAST([] AS ARRAY<STRING>) AS aliases", ""
        return self._query(
            f"SELECT cm.item_id, cm.kind, cm.label, cm.canonical_key, {aliases} FROM {cmap} cm\n"
            "WHERE cm.valid_to IS NULL AND (STRPOS(LOWER(IFNULL(cm.label, '')), LOWER(@q)) > 0\n"
            f"  OR STRPOS(LOWER(IFNULL(cm.canonical_key, '')), LOWER(@q)) > 0{alias_match})\n"
            "ORDER BY cm.label, cm.item_id LIMIT @limit",
            q=("STRING", q), limit=("INT64", limit))

    def lexicon_terms(self, market, kinds, start, end, week_from, prior_from, limit):
        """Page port, 3 October 2026: the open cultural_map items of the given kinds (status active, so generic and
        rejected rows aside) with posts in the market between start and end, counted per day as item_days counts
        them (the largest lane class total, '_any' and placebo search_presence sightings aside), summed over the
        window, the week from week_from and the week from prior_from up to week_from. Most posts first, at most
        `limit` rows. None until cultural_map and v_item_daily_current exist."""
        daily, cmap, seeds = self._find("v_item_daily_current"), self._find("cultural_map"), self._find("seed_queue")
        if daily is None or cmap is None:
            return None
        if seeds:
            placebo = (f"pl AS (SELECT DISTINCT q.item_id, q.market, q.seed_date FROM {seeds} q\n"
                       "  WHERE q.lane = 'placebo' AND q.market = @market AND q.seed_date BETWEEN @start AND @end),\n")
            join = "  LEFT JOIN pl ON pl.item_id = i.item_id AND pl.market = i.market AND pl.seed_date = i.metric_date\n"
            drop = " AND NOT (i.lane_class = 'search_presence' AND pl.item_id IS NOT NULL)"
        else:
            placebo, join, drop = "", "", ""
        return self._query(
            f"WITH {placebo}lc AS (\n"
            "  SELECT i.item_id, i.metric_date, i.lane_class, SUM(i.posts) AS posts\n"
            f"  FROM {daily} i\n{join}"
            "  WHERE i.market = @market AND i.metric_date BETWEEN @start AND @end\n"
            f"    AND i.lane_class != '_any'{drop}\n"
            "  GROUP BY i.item_id, i.metric_date, i.lane_class),\n"
            "d AS (SELECT lc.item_id, lc.metric_date, MAX(lc.posts) AS posts FROM lc\n"
            "  GROUP BY lc.item_id, lc.metric_date HAVING MAX(lc.posts) > 0),\n"
            "t AS (SELECT d.item_id, SUM(d.posts) AS posts_window,\n"
            "  SUM(IF(d.metric_date >= @week_from, d.posts, 0)) AS posts_week,\n"
            "  SUM(IF(d.metric_date >= @prior_from AND d.metric_date < @week_from, d.posts, 0)) AS posts_prior_week\n"
            "  FROM d GROUP BY d.item_id)\n"
            "SELECT t.item_id, cm.kind, cm.label, cm.canonical_key, cm.status, cm.first_seen,\n"
            "  t.posts_window, t.posts_week, t.posts_prior_week\n"
            f"FROM t JOIN {cmap} cm ON cm.item_id = t.item_id AND cm.valid_to IS NULL\n"
            "WHERE cm.kind IN UNNEST(@kinds) AND cm.status = 'active'\n"
            "ORDER BY t.posts_window DESC, cm.label, t.item_id LIMIT @limit",
            market=("STRING", market), kinds=("ARRAY<STRING>", list(kinds)), start=("DATE", start),
            end=("DATE", end), week_from=("DATE", week_from), prior_from=("DATE", prior_from),
            limit=("INT64", limit))

    def ask_history(self, limit, before, market=None):
        return self._query(
            "SELECT JSON_VALUE(r.record, '$.ask_id') AS ask_id, JSON_VALUE(r.record, '$.market') AS market,\n"
            "  IFNULL(JSON_VALUE(r.record, '$.question'), r.question) AS question, r.started_at AS asked_at,\n"
            "  JSON_VALUE(r.record, '$.status') AS status, JSON_VALUE(r.record, '$.answer.status') AS answer_status\n"
            f"FROM {self._t(AGENT + '.runs')} r\n"
            "WHERE r.stage = 'ask' AND JSON_VALUE(r.record, '$.ask_id') IS NOT NULL\n"
            "  AND (@before IS NULL OR r.started_at < @before)\n"
            "  AND (@market IS NULL OR JSON_VALUE(r.record, '$.market') = @market)\n"
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY JSON_VALUE(r.record, '$.ask_id')\n"
            "  ORDER BY r.finished_at DESC) = 1\n"
            "ORDER BY r.started_at DESC LIMIT @limit",
            before=("TIMESTAMP", before), limit=("INT64", limit), market=("STRING", market))

    def brief_history(self, limit, before, market=None):
        """Every market row of the newest `limit` brief dates, whatever its status (a brief that held every item
        included), with only the parts of the payload History counts from: cards, more and held_back.items."""
        briefs = self._t(AGENT + ".v_briefs_current")
        return self._query(
            "SELECT b.brief_date, b.market, b.run_id, b.status, b.published_at,\n"
            "  JSON_OBJECT('cards', JSON_QUERY(b.payload, '$.cards'), 'more', JSON_QUERY(b.payload, '$.more'),\n"
            "    'held_back', JSON_OBJECT('items', JSON_QUERY(b.payload, '$.held_back.items'))) AS payload\n"
            f"FROM {briefs} b\n"
            "WHERE (@market IS NULL OR b.market = @market) AND b.brief_date IN (SELECT DISTINCT d.brief_date FROM "
            f"{briefs} d WHERE (@before IS NULL OR d.brief_date < @before) AND (@market IS NULL OR d.market = @market)"
            " ORDER BY d.brief_date DESC LIMIT @limit)\n"
            "ORDER BY b.brief_date DESC, b.market",
            before=("DATE", before), limit=("INT64", limit), market=("STRING", market))

    def findings(self, item_id=None):
        """Current findings; status reads null until L3 adds the column, so the route gives its fallback note."""
        catalog = self._catalog()
        if f"{AGENT}.findings" not in catalog["objects"]:
            return None
        table = self._t(f"{AGENT}.findings")
        status = "f.status" if "status" in catalog["findings_columns"] else "CAST(NULL AS STRING) AS status"
        return self._query(
            f"SELECT f.finding_id, f.question, f.answer, f.as_of, f.claims, f.valid_from, f.valid_to, {status}\n"
            f"FROM {table} f\n"
            "WHERE f.valid_to IS NULL AND (@item_id IS NULL OR EXISTS (\n"
            "  SELECT 1 FROM UNNEST(f.claims) c, UNNEST(c.item_ids) i WHERE i = @item_id))\n"
            "ORDER BY f.as_of DESC",
            item_id=("STRING", item_id))

    def finding_rows(self, finding_id):
        """Every existing row with this deterministic finding ID, including rows no longer current."""
        if f"{AGENT}.findings" not in self._catalog()["objects"]:
            return None
        table = self._t(f"{AGENT}.findings")
        return self._query(
            "SELECT f.finding_id, f.question, f.answer, f.as_of, f.claims, f.valid_from, f.valid_to, f.status\n"
            f"FROM {table} f WHERE f.finding_id = @finding_id",
            finding_id=("STRING", finding_id))

    def insert_finding(self, row):
        """Append one Findings row with BigQuery's deterministic insert ID for retry deduplication."""
        if f"{AGENT}.findings" not in self._catalog()["objects"]:
            raise RuntimeError("findings table is unavailable")
        errors = self.client.insert_rows_json(
            f"{self.project}.{AGENT}.findings", [row], row_ids=[row["finding_id"]])
        return not errors


CREATOR_COLUMNS = ("creator_id", "platform", "handle", "followers", "tier", "home_market")  # never coord_score
POST_COLUMNS = ("post_id", "platform", "creator_id", "url", "text", "published_at", "views", "likes", "comments",
                "shares", "thumbnail_url", "duration_s", "creator_tier_at_post", "geo_market")
EXCLUDED_LANES = ("legacy", "agent_live")  # never counted in a market (item_daily's '_any', DATA.md section 1)
WORD = r"[^\p{L}\p{N}_]"


def _keyword_regex(terms):
    """Whole-word, case-insensitive match of any term, in Python's re (the fixture store)."""
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", re.IGNORECASE)


def _keyword_re2(terms):
    """The same match in RE2, which BigQuery's REGEXP_CONTAINS uses and which has no lookarounds."""
    return f"(?i)(?:^|{WORD})(?:" + "|".join(re.escape(t) for t in terms) + f")(?:$|{WORD})"


SEED_KEY = ("seed_date", "item_id", "query", "kind", "lane", "template")


def _seed_queue_rows(rows, limit):
    """BigQueryStore.seed_queue's rows from plain seed_queue rows (placebo already left out), in Python."""
    queued = [r for r in rows if r.get("credits_estimate") is not None and r.get("yield_posts") is None
              and r.get("yield_new_creators") is None]
    yielded = [r for r in rows if r.get("yield_posts") is not None or r.get("yield_new_creators") is not None]
    out = []
    if queued:
        day = max(r["seed_date"] for r in queued)
        mine = sorted((r for r in queued if r["seed_date"] == day),
                      key=lambda r: (-(r.get("priority") or 0), r.get("lane") or "", r.get("item_id") or "",
                                     r.get("query") or ""))
        out += [dict({k: r.get(k) for k in SEED_KEY}, grain="queued", priority=r.get("priority"),
                     credits_estimate=r["credits_estimate"], yield_posts=None, yield_new_creators=None)
                for r in mine[:limit]]
    if yielded:
        day = max(r["seed_date"] for r in yielded)
        best = {}
        for r in yielded:
            if r["seed_date"] != day:
                continue
            key = tuple(r.get(k) for k in SEED_KEY)
            row = best.setdefault(key, dict(zip(SEED_KEY, key), grain="yield", priority=None, credits_estimate=None,
                                            yield_posts=None, yield_new_creators=None))
            for col in ("yield_posts", "yield_new_creators"):
                if r.get(col) is not None:
                    row[col] = max(row[col], r[col]) if row[col] is not None else r[col]
        out += sorted(best.values(), key=lambda r: (-(r["yield_posts"] or 0), r["lane"] or "", r["item_id"] or "",
                                                    r["query"] or ""))[:limit]
    return out


def cap_refused(exc):
    """True when BigQuery refused a query for reading more than MAX_BYTES."""
    reasons = {e.get("reason") for e in getattr(exc, "errors", None) or [] if isinstance(e, dict)}
    return "bytesBilledLimitExceeded" in reasons or "exceeded limit for bytes billed" in str(exc).lower()


def _in_market(rows, market):
    return [r for r in rows if r.get("market") in (None, market)]


def _value(v, tz):
    if isinstance(v, dt.datetime):
        return v.astimezone(tz).isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _value(x, tz) for k, x in v.items()}
    return v


def _normalise(row):
    tz = MARKET_TZ.get(row.get("market"), MARKET_TZ["ZA"])
    out = {}
    for k, v in row.items():
        if k in ("payload", "record", "counts") and isinstance(v, str):
            v = json.loads(v)
        out[k] = _value(v, tz)
    return out
