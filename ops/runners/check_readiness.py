"""Read-only morning readiness report for 42 staging: why the brief held what it held, and what is not pulling.

    py -3.13 ops/runners/check_readiness.py [YYYY-MM-DD] [brief_run_id]

Run it from the root of a checkout (a fresh worktree is fine). Needs google-cloud-bigquery, pyyaml and gcloud ADC
on this PC. SELECT only, every query capped at 1 GB billed; a query that fails or would pass the cap prints one
line naming it and its part of the report shows ? instead. The date defaults to today in SAST and the run to the
day's latest brief run.

What it prints, summary first:
  SUMMARY      one line per market (cards, holds by rule, the main platform of the G1 holds, invalid baseline feeds
               over d-2..d, planned sources not pulling on d) and one line each for detect, understand and Google
               Trends.
  BRIEF        per market the brief's status, cards, holds by rule and reason code, the G1 holds by main platform
               (the gate's own rule, core/brief/gatectx.py: the main series' platform, else the platform with most
               sightings in 14 days) and the invalid baseline feeds for d, d-1 and d-2 with invalid_reason.
  SOURCES      every source in the collect plan (core/collect/job.py plan, local_sources.plan and the confirmed
               public feeds) per market with seven days of collection_health: calls ok/total, units ok/planned,
               valid protocols, and the invalid reasons, with the protocols planned but never called (0 calls,
               which writers.judge marks invalid on calls; c counts only the calls made). NOT PULLING flags a
               source planned on d with no health row, no successful call or a row on d invalid on calls (or
               for no stated reason); INVALID ON <reason> flags one that pulled but whose rows on d were judged
               invalid only on items, effort or drift, which the summary lists apart.
  DETECT       per day the latest detect run's status, the view builds that failed (spread, agent_views, news,
               item_centroids: they have no runs row of their own, so a failure there leaves the pages reading
               those views empty or stale while detect still reads ok) and the other steps that failed or were
               skipped, each with its error type only.
  UNDERSTAND   posts sighted per market and day against posts with an enrichment row, the understand runs' video
               counts, transcript calls in the credit ledger and how the clips were read (linked video or a
               thumbnail frame).
  TRENDS       the Google Trends rows (google_search_signals) per day, market and source, and the collect runs'
               own states for the SocialCrawl trending read and the BigQuery public tables.

Reads intelligence_42_core and intelligence_42_agent in ogilvy-trends-v2 (the staging datasets the jobs write).
Prints item titles, counts, series names, rule codes and fixed reasons only: no handles, post text, model text or
error text.
"""
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.collect.chain import TIMEOUTS  # noqa: E402

P = "ogilvy-trends-v2"
CORE, AGENT = f"`{P}.intelligence_42_core`", f"`{P}.intelligence_42_agent`"
CAP = 10**9
MARKETS = ("ZA", "NG", "KE")
BASELINE = ("unbiased_rank", "panel", "unbiased_counter")
# writers.judge reasons for a day that pulled but whose volume or effort was out of band (DATA.md 3.3).
JUDGED = ("items", "effort", "drift")
DAYS = 7
SAST = dt.timezone(dt.timedelta(hours=2))
# The detect counts keys of core/detect/job.py: the view builds with no runs row of their own (job.VIEW_STEPS),
# then the steps that also append their own runs row.
DETECT_VIEW_STEPS = ("spread", "agent_views", "news", "item_centroids")
DETECT_OTHER_STEPS = ("coaction", "breakout", "watch", "seeds", "forecast")
_STEP_COLUMNS = ",\n  ".join(
    f"JSON_VALUE(counts, '$.{k}.status') {k}_status, "
    f"REGEXP_EXTRACT(JSON_VALUE(counts, '$.{k}.error'), r'^([A-Za-z_][A-Za-z0-9_.]*):') {k}_type"
    for k in DETECT_VIEW_STEPS + DETECT_OTHER_STEPS)

SQL = {
    "brief runs": f"""
SELECT b.run_id, MIN(b.published_at) published_at, ANY_VALUE(r.status) run_status
FROM {AGENT}.briefs b
LEFT JOIN {AGENT}.runs r ON r.run_id = b.run_id AND r.run_date = @d AND r.stage = 'brief'
WHERE b.brief_date = @d
GROUP BY b.run_id ORDER BY published_at""",
    "brief payloads": f"""
SELECT market, status, TO_JSON_STRING(payload) p FROM {AGENT}.briefs
WHERE brief_date = @d AND run_id = @rid""",
    # The gate's main platform (core/brief/sql/gatectx.sql series_platform and sightings), batched over the G1 holds.
    "main series": f"""
WITH s AS (
  SELECT item_id, market, main_series_id FROM {CORE}.v_item_state_current
  WHERE metric_date = @d AND item_id IN UNNEST(@ids)
), p AS (
  SELECT st.series_id, ARRAY_AGG(st.platform ORDER BY st.metric_date DESC LIMIT 1)[OFFSET(0)] platform
  FROM {CORE}.v_series_test_current st
  WHERE st.series_id IN (SELECT main_series_id FROM s) AND st.platform IS NOT NULL
    AND st.metric_date BETWEEN DATE_SUB(@d, INTERVAL 60 DAY) AND @d
  GROUP BY st.series_id
)
SELECT s.item_id, s.market, s.main_series_id, p.platform FROM s LEFT JOIN p ON p.series_id = s.main_series_id""",
    "sightings": f"""
SELECT pi.item_id, po.market, po.platform, COUNT(*) n
FROM {CORE}.post_observations po
JOIN {CORE}.post_items pi ON pi.post_id = po.post_id
WHERE pi.item_id IN UNNEST(@ids) AND po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 13 DAY) AND @d
GROUP BY 1, 2, 3""",
    "collection health": f"""
SELECT h.day, h.market, h.platform, h.series, h.lane_class, COUNT(*) protocols, COUNTIF(h.valid) valid_protocols,
  SUM(IFNULL(h.calls, 0)) calls, SUM(IFNULL(h.calls_ok, 0)) calls_ok,
  SUM(IFNULL(h.units_planned, 0)) units_planned, SUM(IFNULL(h.units_ok, 0)) units_ok,
  COUNTIF(IFNULL(h.calls, 0) = 0) not_called,
  STRING_AGG(DISTINCT IF(h.valid IS NOT TRUE, IFNULL(h.invalid_reason, 'no reason'), NULL), '; ') reasons
FROM {CORE}.v_collection_health_current h
WHERE h.day BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
GROUP BY 1, 2, 3, 4, 5""",
    "collect runs": f"""
SELECT run_date, status, started_at, finished_at, COUNT(DISTINCT run_id) OVER (PARTITION BY run_date) runs_that_day,
  TO_JSON_STRING(JSON_QUERY(counts, '$.search_signal_states')) sc_states,
  TO_JSON_STRING(JSON_QUERY(counts, '$.google_bq_states')) bq_states,
  SAFE_CAST(JSON_VALUE(counts, '$.search_signals') AS INT64) sc_rows,
  SAFE_CAST(JSON_VALUE(counts, '$.google_bq_signals') AS INT64) bq_rows,
  JSON_VALUE(counts, '$.trends_error') IS NOT NULL sc_error,
  JSON_VALUE(counts, '$.google_bq_error') IS NOT NULL bq_error,
  JSON_VALUE(counts, '$.local_error') IS NOT NULL local_error,
  JSON_VALUE(counts, '$.public_feed_error') IS NOT NULL public_feed_error
FROM {AGENT}.runs
WHERE stage = 'collect' AND status != 'skipped_duplicate' AND run_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
QUALIFY ROW_NUMBER() OVER (PARTITION BY run_date
  ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC) = 1""",
    # Only the error's type is read, never its text. The begin and finish rows share started_at, so the finished
    # row of the newest run wins; a skipped_duplicate row (a second start after an ok run) is left out, as
    # chain.latest leaves it out.
    "detect runs": f"""
SELECT run_date, status, started_at, finished_at, COUNT(DISTINCT run_id) OVER (PARTITION BY run_date) runs_that_day,
  {_STEP_COLUMNS}
FROM {AGENT}.runs
WHERE stage = 'detect' AND status != 'skipped_duplicate' AND run_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
QUALIFY ROW_NUMBER() OVER (PARTITION BY run_date
  ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC) = 1""",
    "search signals": f"""
SELECT DATE(fetched_at, 'Africa/Johannesburg') day, market, source, COUNT(*) n
FROM {CORE}.google_search_signals
WHERE DATE(fetched_at) BETWEEN DATE_SUB(@d, INTERVAL 7 DAY) AND DATE_ADD(@d, INTERVAL 1 DAY)
GROUP BY 1, 2, 3""",
    # The enrich step's own sighting rule (core/understand/sql/enrich_select.sql), split by market.
    "enrichment": f"""
WITH s AS (
  SELECT DISTINCT po.observed_date day, po.market, po.post_id FROM {CORE}.post_observations po
  WHERE po.observed_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d AND po.market IN UNNEST(@markets)
), e AS (SELECT DISTINCT post_id FROM {CORE}.post_enrichment WHERE tone IS NOT NULL)
SELECT s.day, s.market, COUNT(*) sighted, COUNTIF(e.post_id IS NOT NULL) enriched
FROM s LEFT JOIN e ON e.post_id = s.post_id GROUP BY 1, 2""",
    "understand runs": f"""
SELECT run_date, status, started_at, finished_at, SAFE_CAST(JSON_VALUE(counts, '$.enriched') AS INT64) enriched,
  JSON_VALUE(counts, '$.enrich_error') IS NOT NULL enrich_error, TO_JSON_STRING(JSON_QUERY(counts, '$.video')) video
FROM {AGENT}.runs
WHERE stage = 'understand' AND status != 'skipped_duplicate' AND run_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d
QUALIFY ROW_NUMBER() OVER (PARTITION BY run_date
  ORDER BY COALESCE(finished_at, started_at) DESC, finished_at IS NOT NULL DESC) = 1""",
    "transcript calls": f"""
SELECT trend_date day, SUM(IFNULL(calls, 0)) calls, ROUND(SUM(IFNULL(credits_charged, 0)), 1) credits
FROM {CORE}.credit_ledger
WHERE trend_date BETWEEN DATE_SUB(@d, INTERVAL 6 DAY) AND @d AND ENDS_WITH(IFNULL(route, ''), '/transcript')
GROUP BY 1""",
    "clip reads": f"""
SELECT IFNULL(JSON_VALUE(video_notes, '$.read_from'), '?') read_from, COUNT(*) n
FROM {CORE}.post_enrichment WHERE video_notes IS NOT NULL GROUP BY 1""",
}
READ_FROM = {"video": "linked clip (frames and sound)", "thumbnail": "thumbnail frame only", "text": "text only"}


class Reader:
    """Runs the named SELECTs with the 1 GB cap. rows() returns dicts, or None after printing one line."""

    def __init__(self, client, out=print):
        self.client, self.out = client, out

    def config(self, params):
        from google.cloud import bigquery
        return bigquery.QueryJobConfig(maximum_bytes_billed=CAP, use_query_cache=True, query_parameters=params)

    def rows(self, label, params):
        try:
            job = self.client.query(SQL[label], job_config=self.config(params))
            return [dict(r.items()) for r in job.result()]
        except Exception as exc:
            self.out(f"[{label}: query failed ({type(exc).__name__}); shown as ?]")
            return None


def params(d, **extra):
    from google.cloud import bigquery
    out = [bigquery.ScalarQueryParameter("d", "DATE", d)]
    for name, value in extra.items():
        if isinstance(value, (list, tuple)):
            out.append(bigquery.ArrayQueryParameter(name, "STRING", list(value)))
        else:
            out.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    return out


def planned_sources(days):
    """{(market, series): {"platform", "lane_class", "days": {day}}} for every source the collect job plans on
    the days: the SocialCrawl calls (health market, so counters sit in GLOBAL), the local sources and the
    confirmed public feeds. Search and placebo calls are one source each per market."""
    from core.collect import job, local_sources
    from core.public_feeds.catalog import confirmed_feeds
    from core.collect.public_feed_collect import SERIES_BY_KIND

    out = {}

    def add(market, series, platform, lane_class, day):
        s = out.setdefault((market, series), {"platform": platform, "lane_class": lane_class, "days": set()})
        s["days"].add(day)
        if s["platform"] != platform:
            s["platform"] = "mixed" if s["platform"] and platform else (s["platform"] or platform)

    for day in days:
        for calls in job.plan(day)["calls"].values():
            for c in calls:
                add(c.health_market(), c.series(), job.ROUTES[c.route][1], c.lane_class(), day)
        for f in local_sources.plan(day):
            add(f.market, f.series, f.platform, f.lane_class, day)
        for f in confirmed_feeds():
            add(f.market, SERIES_BY_KIND[f.kind], f.kind, "unbiased_rank" if f.kind == "chart" else "context", day)
    return out


# Pure parts: everything below works on rows already read, so the tests run it on fixtures.

def brief_holds(payload):
    """(cards, held items, Counter of rule, Counter of 'rule reason') for one market's payload."""
    payload = payload or {}
    cards = len(payload.get("cards") or []) + len(payload.get("more") or [])
    held = (payload.get("held_back") or {}).get("items") or []
    rules = Counter(str(x.get("rule") or "?") for x in held)
    reasons = Counter(f"{x.get('rule') or '?'} {x.get('reason') or '?'}" for x in held)
    return cards, held, rules, reasons


def main_platforms(g1, series_rows, sightings):
    """{(market, item_id): platform} for the G1 holds by the gate's rule: the main series' platform, else the
    platform with most sightings in the market over 14 days (ties alphabetical), else None."""
    main = {(r["market"], r["item_id"]): r.get("platform") for r in series_rows or []}
    seen = defaultdict(Counter)
    for r in sightings or []:
        if r.get("platform"):
            seen[(r["market"], r["item_id"])][r["platform"]] += r["n"]
    out = {}
    for key in g1:
        platform = main.get(key)
        if platform is None and seen[key]:
            platform = min(seen[key], key=lambda p: (-seen[key][p], p))
        out[key] = platform
    return out


def cell(h):
    if h is None:
        return "no row"
    mark = "ok" if h["valid_protocols"] == h["protocols"] else "BAD"
    return f"c{h['calls_ok']}/{h['calls']} u{h['units_ok']}/{h['units_planned']} {mark}"


def merge_health(rows):
    """{(market, series, day): totals} over platforms and lane classes."""
    out = {}
    for r in rows or []:
        key = (r["market"], r["series"], r["day"])
        t = out.setdefault(key, {"protocols": 0, "valid_protocols": 0, "calls": 0, "calls_ok": 0,
                                 "units_planned": 0, "units_ok": 0, "not_called": 0, "reasons": set()})
        for k in ("protocols", "valid_protocols", "calls", "calls_ok", "units_planned", "units_ok", "not_called"):
            t[k] += r.get(k) or 0
        t["reasons"].update(x.strip() for x in (r.get("reasons") or "").split(";") if x.strip())
    return out


def not_pulling(h):
    return h is None or h["calls_ok"] == 0 or (h["valid_protocols"] < h["protocols"] and not judged_only(h))


def judged_only(h):
    """The judge's reasons when every invalid row of h was judged on items, effort or drift, else ()."""
    reasons = sorted(h["reasons"])
    return tuple(reasons) if reasons and all(x in JUDGED for x in reasons) else ()


def source_lines(sources, health, days):
    """(lines, {market: [series not pulling on days[0]]}, {market: [(series, reasons) pulled but judged invalid
    on days[0]]}) for every planned source, newest day first."""
    merged = merge_health(health)
    lines, flags, judged = [], defaultdict(list), defaultdict(list)
    d = days[0]
    for market in ("GLOBAL",) + MARKETS:
        keys = sorted(s for (m, s) in sources if m == market)
        if not keys:
            continue
        lines.append(f"{market}  ({', '.join(f'{x:%m-%d}' for x in days)})")
        for series in keys:
            src = sources[(market, series)]
            cells, why = [], []
            for day in days:
                if day not in src["days"]:
                    cells.append("not planned")
                    continue
                h = merged.get((market, series, day))
                cells.append(cell(h))
                if h and h["reasons"]:
                    unmade = (f" ({h['not_called']} of {h['protocols']} protocols not called)"
                              if h["not_called"] else "")
                    why.append(f"{day:%m-%d} {', '.join(sorted(h['reasons']))}{unmade}")
            planned_d = d in src["days"]
            h = merged.get((market, series, d))
            bad = planned_d and not_pulling(h)
            only = () if bad or not planned_d or h["valid_protocols"] == h["protocols"] else judged_only(h)
            if bad:
                flags[market].append(series)
            if only:
                judged[market].append((series, only))
            mark = "NOT PULLING " if bad else f"INVALID ON {', '.join(only)} " if only else ""
            base = "baseline" if src["lane_class"] in BASELINE else src["lane_class"]
            lines.append(f"  {mark}{series} [{src['platform'] or '-'}, {base}]: " + " | ".join(cells))
            if why:
                lines.append(f"    invalid: {'; '.join(why)}")
    unplanned = sorted({(m, s) for (m, s, _) in merged} - set(sources))
    if unplanned:
        lines.append("health rows for series not in the plan: "
                     + ", ".join(f"{m} {s}" for m, s in unplanned))
    return lines, flags, judged


def invalid_baseline(health, market, days3):
    """Invalid baseline health rows for the market on the three days, newest first."""
    rows = [r for r in health or [] if r["market"] == market and r["day"] in days3
            and r["lane_class"] in BASELINE and r["valid_protocols"] < r["protocols"]]
    return sorted(rows, key=lambda r: (-r["day"].toordinal(), r["series"]))


def platform_bad_days(health, market, platform, days3):
    """The gate's valid_days for a platform: the days among days3 where any baseline row was invalid."""
    return [day for day in days3 if any(r["market"] == market and r["platform"] == platform and r["day"] == day
                                        and r["lane_class"] in BASELINE and r["valid_protocols"] < r["protocols"]
                                        for r in health or [])]


def step_failures(row, steps):
    """'step (Type)' for each step the detect row marks failed, 'step skipped (Type)' for skipped, in order."""
    out = []
    for k in steps:
        status = row.get(f"{k}_status")
        if status in ("failed", "skipped"):
            out.append(f"{k}{' skipped' if status == 'skipped' else ''} ({row.get(f'{k}_type') or '?'})")
    return out


def views_text(row, views):
    """The view builds' state: failed ones named, 'not run' when the row carries none of the view statuses (a run
    that stopped before them, or one still running), else 'ok'. item_centroids reads no status when ok."""
    if views:
        return "failed " + ", ".join(views)
    if all(row.get(f"{k}_status") is None for k in DETECT_VIEW_STEPS):
        return "not run"
    return "ok"


def detect_summary(data):
    rows = data.get("detect")
    if rows is None:
        return ["detect: ?"]
    r = next((x for x in rows if x["run_date"] == data["d"]), None)
    if r is None:
        return [f"detect {data['d']:%m-%d}: no run"]
    views, other = step_failures(r, DETECT_VIEW_STEPS), step_failures(r, DETECT_OTHER_STEPS)
    several = f" (latest of {r['runs_that_day']})" if (r.get("runs_that_day") or 1) > 1 else ""
    text = f"detect {data['d']:%m-%d}: run {r['status']}{several}; "
    text += (f"FLAG view builds failed: {', '.join(views)}, so the pages reading those views can be empty or stale"
             if views else f"view builds {views_text(r, views)}")
    return [text + (f"; other steps failed: {', '.join(other)}" if other else "")]


def detect_lines(data):
    rows = data.get("detect")
    if rows is None:
        return ["  detect runs: ?"]
    runs = {r["run_date"]: r for r in rows}
    out = []
    for day in data["days"]:
        r = runs.get(day)
        if not r:
            out.append(f"  {day:%m-%d} detect run: none")
            continue
        views, other = step_failures(r, DETECT_VIEW_STEPS), step_failures(r, DETECT_OTHER_STEPS)
        several = f" (latest of {r['runs_that_day']})" if (r.get("runs_that_day") or 1) > 1 else ""
        out.append(f"  {day:%m-%d} detect run {r['status']}{several}: "
                   f"view builds {views_text(r, views)}; "
                   f"steps failed {', '.join(other) or 'none'}")
    return out


def counts_text(counter):
    return ", ".join(f"{k} {v}" for k, v in sorted(counter.items())) or "none"


def report(data):
    """Every line of the report from the rows read (see fetch). Values a failed query left out show as ?."""
    d, days = data["d"], data["days"]
    days3 = days[:3]
    health = data.get("health")
    out = [f"42 readiness for {d} (brief run {data.get('rid') or 'none'}), read {data['now']:%Y-%m-%d %H:%M} SAST"]
    if data.get("plan_error"):
        out.append(f"[collect plan: could not be built ({data['plan_error']}); sources come from health rows only]")
    src_lines, flags, judged = ([], {}, {}) if health is None else source_lines(data["sources"], health, days)

    per_market, summary = [], ["", "== SUMMARY"]
    payloads = data.get("payloads")
    platforms = data.get("platforms") or {}
    for m in MARKETS:
        if payloads is None:
            summary.append(f"{m}: brief ?")
            continue
        p = payloads.get(m)
        cards, held, rules, reasons = brief_holds((p or {}).get("payload"))
        g1 = Counter(platforms.get((m, x.get("item_id"))) or "unknown" for x in held if x.get("rule") == "G1")
        bad = None if health is None else invalid_baseline(health, m, days3)
        status = (p or {}).get("status") or "no brief"
        summary.append(
            f"{m}: {status}, {cards} cards, {len(held)} held ({counts_text(rules)}); G1 by main platform "
            f"{counts_text(g1)}; invalid baseline feeds d-2..d {'?' if bad is None else len(bad)}; "
            f"not pulling on {d:%m-%d} {'?' if health is None else len(flags.get(m, []))}")
        per_market += ["", f"== {m} brief: {status}, {cards} cards, {len(held)} held"]
        per_market.append(f"  holds by rule and reason: {counts_text(reasons)}")
        for platform, n in sorted(g1.items()):
            bad_days = [] if health is None or platform == "unknown" else platform_bad_days(health, m, platform, days3)
            per_market.append(f"  G1 main platform {platform}: {n} items; invalid baseline days on it: "
                              f"{', '.join(f'{x:%m-%d}' for x in bad_days) or ('?' if health is None else 'none')}")
        if bad is None:
            per_market.append("  invalid baseline feeds d-2..d: ?")
        else:
            per_market.append(f"  invalid baseline feeds d-2..d: {len(bad) or 'none'}")
            per_market += [f"    {r['day']:%m-%d} {r['series']} [{r['platform'] or '-'}] calls {r['calls_ok']}/"
                           f"{r['calls']} units {r['units_ok']}/{r['units_planned']} valid {r['valid_protocols']}/"
                           f"{r['protocols']}: {r['reasons'] or 'no reason'}" for r in bad]
    if "GLOBAL" in flags:
        summary.append(f"GLOBAL counters not pulling on {d:%m-%d}: {len(flags['GLOBAL'])}")
    summary += detect_summary(data) + understand_summary(data) + trends_summary(data)
    if flags:
        summary.append("not pulling on d: " + "; ".join(f"{m} {', '.join(v)}" for m, v in flags.items() if v))
    if judged:
        summary.append("pulled but judged invalid on d: " + "; ".join(
            f"{m} " + ", ".join(f"{s} ({', '.join(r)})" for s, r in v) for m, v in judged.items() if v))

    out += summary + per_market
    out += ["", "== SOURCES (collect plan x collection_health; c calls ok/total, u units ok/planned, ok or BAD "
                "when any protocol was invalid)"]
    out += src_lines or ["  ?"]
    out += ["", "== DETECT (view builds with no runs row of their own, then the other steps)"] + detect_lines(data)
    out += ["", "== UNDERSTAND"] + understand_lines(data)
    out += ["", "== GOOGLE TRENDS"] + trends_lines(data)
    return out


def video_counts(run):
    """The run's counts.video as a dict. TO_JSON_STRING of a missing $.video is the string "null", so a run with
    no video step (or an older run that stored something else there) reads as empty, never as None."""
    raw = (run or {}).get("video")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def understand_summary(data):
    rows = data.get("enrichment")
    if rows is None:
        return ["understand: ?"]
    today = [r for r in rows if r["day"] == data["d"]]
    seen, done = sum(r["sighted"] for r in today), sum(r["enriched"] for r in today)
    run = next((r for r in data.get("understand") or [] if r["run_date"] == data["d"]), None)
    video = video_counts(run)
    tr = next((r for r in data.get("transcripts") or [] if r["day"] == data["d"]), None)
    return [f"understand {data['d']:%m-%d}: run {run['status'] if run else 'none'}, enriched {done} of {seen} "
            f"sighted posts; clips read {video.get('read', 'off' if video.get('off') else 0)}; transcript calls "
            f"{tr['calls'] if tr else 0}{' (FLAG: no transcripts, frames only)' if not tr else ''}"]


def understand_lines(data):
    out = []
    rows = data.get("enrichment")
    if rows is None:
        out.append("  enrichment: ?")
    else:
        by = {(r["day"], r["market"]): r for r in rows}
        for day in data["days"]:
            cells = []
            for m in MARKETS:
                r = by.get((day, m))
                share = f" {round(100 * r['enriched'] / r['sighted'])}%" if r and r["sighted"] else ""
                cells.append(f"{m} {r['enriched'] if r else 0}/{r['sighted'] if r else 0}{share}")
            out.append(f"  {day:%m-%d} enriched/sighted: {' | '.join(cells)}")
    runs = {r["run_date"]: r for r in data.get("understand") or []}
    trs = {r["day"]: r for r in data.get("transcripts") or []}
    for day in data["days"]:
        r = runs.get(day)
        if not r:
            out.append(f"  {day:%m-%d} understand run: none")
            continue
        v = video_counts(r)
        vtxt = ("off" if v.get("off") else f"skipped {v['skipped']}" if v.get("skipped") else
                f"read {v.get('read', 0)}, failed {v.get('failed', 0)}, vendor credits {v.get('credits', 0)}"
                + (", error" if v.get("error") else "")) if v else "not run"
        t = trs.get(day)
        out.append(f"  {day:%m-%d} understand run {r['status']}: enriched {r.get('enriched')}"
                   f"{', enrich error' if r.get('enrich_error') else ''}; video {vtxt}; transcript calls "
                   f"{t['calls'] if t else 0} ({t['credits'] if t else 0} credits)")
    reads = data.get("clip_reads")
    out.append("  clips read so far: ?" if reads is None else "  clips read so far: " + (", ".join(
        f"{READ_FROM.get(r['read_from'], r['read_from'])} {r['n']}" for r in reads) or "none"))
    return out


def trends_summary(data):
    sig = data.get("signals")
    if sig is None:
        return ["google trends: ?"]
    today = Counter()
    for r in sig:
        if r["day"] == data["d"]:
            today[f"{r['market']} {r['source']}"] += r["n"]
    from core.collect import google_trends
    want = sorted([f"{m} google_bq" for m in google_trends.BQ_MARKETS]
                  + [f"{m} google_trending" for m in google_trends.MARKETS])
    missing = [k for k in want if not today.get(k)]
    return [f"google trends {data['d']:%m-%d}: {counts_text(today)}"
            + (f"; FLAG no rows from {', '.join(missing)}" if missing else "")]


def trends_lines(data):
    out = []
    sig = data.get("signals")
    if sig is None:
        out.append("  google_search_signals: ?")
    else:
        by = defaultdict(Counter)
        for r in sig:
            by[r["day"]][f"{r['market']} {r['source']}"] += r["n"]
        for day in data["days"]:
            out.append(f"  {day:%m-%d} rows: {counts_text(by.get(day, Counter()))}")
    runs = {r["run_date"]: r for r in data.get("collect") or []}
    for day in data["days"]:
        r = runs.get(day)
        if not r:
            out.append(f"  {day:%m-%d} collect run: none")
            continue
        errs = [k for k in ("sc_error", "bq_error", "local_error", "public_feed_error") if r.get(k)]
        several = f" (latest of {r['runs_that_day']})" if (r.get("runs_that_day") or 1) > 1 else ""
        out.append(f"  {day:%m-%d} collect run {r['status']}{several}: "
                   f"trending read {r.get('sc_rows')} rows {r.get('sc_states') or '-'}; BigQuery tables "
                   f"{r.get('bq_rows')} rows {r.get('bq_states') or '-'}"
                   + (f"; errors set: {', '.join(errs)}" if errs else ""))
    return out


def fetch(reader, d, rid=None, sources_fn=planned_sources, now=None):
    """Every row the report needs, read with the capped reader."""
    days = [d - dt.timedelta(days=i) for i in range(DAYS)]
    data = {"d": d, "days": days, "now": now or dt.datetime.now(SAST)}
    runs = reader.rows("brief runs", params(d))
    if runs and not rid:
        rid = runs[-1]["run_id"]
    data["rid"] = rid
    payloads = None
    if runs is not None:
        rows = reader.rows("brief payloads", params(d, rid=rid or "")) if rid else []
        payloads = None if rows is None else {
            r["market"]: {"status": r["status"], "payload": json.loads(r["p"]) if r["p"] else {}} for r in rows}
    data["payloads"] = payloads
    g1 = sorted({(m, x.get("item_id")) for m, p in (payloads or {}).items()
                 for x in brief_holds(p["payload"])[1] if x.get("rule") == "G1" and x.get("item_id")})
    if g1:
        ids = sorted({i for _, i in g1})
        series = reader.rows("main series", params(d, ids=ids))
        sightings = reader.rows("sightings", params(d, ids=ids))
        data["platforms"] = main_platforms(g1, series, sightings)
    data["health"] = reader.rows("collection health", params(d))
    try:
        data["sources"] = sources_fn(days)
    except Exception as exc:
        data["plan_error"] = type(exc).__name__
        data["sources"] = {(r["market"], r["series"]): {"platform": r["platform"], "lane_class": r["lane_class"],
                                                         "days": set(days)} for r in data["health"] or []}
    data["collect"] = reader.rows("collect runs", params(d))
    data["signals"] = reader.rows("search signals", params(d))
    data["detect"] = reader.rows("detect runs", params(d))
    data["enrichment"] = reader.rows("enrichment", params(d, markets=list(MARKETS)))
    data["understand"] = reader.rows("understand runs", params(d))
    for stage in ("collect", "understand", "detect"):
        for row in data[stage] or []:
            if row["status"] == "running" and row.get("started_at") is not None:
                started = dt.datetime.fromisoformat(str(row["started_at"]))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=dt.timezone.utc)
                if data["now"] - started >= TIMEOUTS[stage]:
                    row["status"] = "dead"
    data["transcripts"] = reader.rows("transcript calls", params(d))
    data["clip_reads"] = reader.rows("clip reads", params(d))
    return data


def main(argv=None, client=None, out=print):
    if out is print and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a title a narrow console cannot show never stops the report
    argv = sys.argv[1:] if argv is None else argv
    d = dt.date.fromisoformat(argv[0]) if argv else dt.datetime.now(SAST).date()
    rid = argv[1] if len(argv) > 1 else None
    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=P)
    for line in report(fetch(Reader(client, out), d, rid)):
        out(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
