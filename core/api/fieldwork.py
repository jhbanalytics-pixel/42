"""Fieldwork (core/api/contract.md section 17): the sources 42 listens to per market and how each did on one day.

Coverage is the daily scorecard of how much was seen. Fieldwork is the roster: what the 02:00 collect job is set to
read for each market (core/collect/job.py plan, core/collect/local_sources.py, core/public_feeds/catalog.py and the
Google Trends phase, all read from the repo's config, so the roster is real even on a day nothing ran), joined to
collection_health for the day, the collect share of credit_ledger and the collect run's own record. A source with
no health row says so; nothing is filled in.
"""
import datetime as dt
import json
from functools import lru_cache

from core.api.discover import figure
from core.api.today import INVALID_WORDS, LABELS, MARKETS, _series_name, invalid_code

SAST = dt.timezone(dt.timedelta(hours=2))
GLOBAL = "GLOBAL"
COLLECT_SCHEDULE = "f42-collect-0200"

# The roster's sections, in reading order.
GROUPS = (
    ("platform", "Platform feeds and trending boards"),
    ("own", "Own feeds"),
    ("creators", "Kept creator accounts"),
    ("charts", "Music and app charts"),
    ("news", "News sites and RSS"),
    ("search", "Searches, counts and rechecks"),
    ("search_interest", "Google search interest"),
)
GROUP_WORDS = dict(GROUPS)

# series: (group, name). One roster row per market and series, which is how collection_health keys its rows.
SERIES = {
    "feed_tiktok": ("platform", "TikTok local feed"),
    "board_youtube": ("platform", "YouTube trending charts"),
    "board_apple_music": ("platform", "Apple Music charts"),
    "board_tiktok_hashtag": ("platform", "TikTok popular hashtags board"),
    "ig_location": ("platform", "Instagram posts from local places"),
    "board_global_music": ("platform", "Global music boards"),
    "list_reddit": ("own", "Own subreddits"),
    "panel_fb_hub": ("own", "Facebook news pages"),
    "panel_x_hub": ("own", "X accounts"),
    "panel_ig_gossip": ("own", "Gossip and entertainment accounts on Instagram"),
    "panel_culture_desk": ("creators", "Culture desk and kept creator accounts"),
    "board_kworb_spotify": ("charts", "Spotify daily chart (via Kworb)"),
    "board_boomplay": ("charts", "Boomplay trending songs"),
    "board_audiomack": ("charts", "Audiomack trending"),
    "board_shazam": ("charts", "Shazam national chart"),
    "board_shazam_city": ("charts", "Shazam city chart"),
    "board_turntable": ("charts", "TurnTable Top 100"),
    "board_nairaland": ("charts", "Nairaland front page"),
    "board_app_store_iphone": ("charts", "App Store top free apps (iPhone)"),
    "board_google_play": ("charts", "Google Play top free apps"),
    "board_music_country": ("charts", "Country music charts"),
    "radio_playlist": ("charts", "Radio playlists"),
    "news_rss": ("news", "News sites and RSS feeds"),
    "search": ("search", "Searches for the day's candidates and seeds"),
    "counter_post_views": ("search", "Evidence post rechecks"),
    "counter_tiktok_sound": ("search", "TikTok sound counts"),
    "counter_tiktok_hashtag": ("search", "TikTok hashtag counts"),
    "curve_tiktok_sound": ("search", "TikTok sound curves"),
    "x_trends": ("platform", "X trends archive"),
}
# local_sources.FEEDS names its RSS feeds by key; these are the publications.
RSS_NAMES = {"briefly": "Briefly", "zalebs": "ZAlebs", "bellanaija": "BellaNaija", "punch": "Punch",
             "legit": "Legit", "nairametrics": "Nairametrics", "tuko": "Tuko", "kenyans": "Kenyans.co.ke"}
# Public feed names the catalog writes in a brand's own styling that reads as a cut sentence on the page:
# rotation.africa styles itself "rotation." with a stop, so "rotation. Kenya chart" looked like a fragment.
FEED_NAMES = {"ke_rotation_chart": "rotation.africa Kenya chart"}
CITY = {"ZA": "Johannesburg", "NG": "Lagos"}
CHART_DETAIL = {
    "board_kworb_spotify": "The market's daily top songs on Spotify",
    "board_boomplay": "Songs trending in the market on Boomplay",
    "board_audiomack": "Songs trending in the market on Audiomack",
    "board_shazam": "The songs most looked up in the market",
    "board_shazam_city": lambda m: f"The songs most looked up in {CITY.get(m, 'the main city')}",
    "board_turntable": "Nigeria's official music chart",
    "board_nairaland": "The threads on Nigeria's largest forum front page",
    "board_app_store_iphone": "The 50 most downloaded free iPhone apps",
    "board_google_play": "The most downloaded free Android apps",
}
YOUTUBE_CHARTS = {"": "overall", "10": "music", "1": "film and animation", "20": "gaming", "24": "entertainment"}

STATUS_WORDS = {
    "delivered": "Delivered",
    "partial": "Partly delivered",
    "failed": "Failed",
    "no_record": "No record for this day",
    "waiting": "Not recorded yet",
    "unavailable": "Could not be read",
}
TRENDS_STATE_WORDS = {
    "ok": ("delivered", "Trending searches read"),
    "empty": ("delivered", "Read, no trending searches listed"),
    "capped": ("failed", "Not read: the daily credit cap was reached"),
    "not_made": ("failed", "Not read: the collection stopped or ran out of credits first"),
    "unavailable": ("failed", "Not read: the data supplier did not answer"),
    "error": ("failed", "Not read: the read failed"),
    "unknown": ("failed", "Not read: the result was not understood"),
}

# Retired or switched off in config. The config gives the technical reason; these are the same facts in plain words.
DROPPED_SCRAPE_WORDS = {
    ("kworb_spotify", "KE"): ("Spotify daily chart (via Kworb)",
                              "Kworb publishes no Kenya Spotify chart; its Kenya page stopped answering in "
                              "September 2026."),
    ("nairaland", "NG"): ("Nairaland front page",
                          "Not read: the site's rules for automated readers refused us on every morning since "
                          "1 October 2026, so the front page is no longer planned."),
    **{("audiomack", m): ("Audiomack trending",
                          "Not read: since 30 September 2026 the page has come back with no songs we can read, "
                          "in all three markets.") for m in ("ZA", "NG", "KE")},
}
DROPPED_FEED_WORDS = {
    "pulse": (("NG", "KE"), "Pulse RSS feed", "Its feeds stopped answering in July 2026."),
    "tshisalive": (("ZA",), "TshisaLIVE RSS feed", "TimesLIVE's feed addresses stopped answering in July 2026."),
    "citizen_digital": (("KE",), "Citizen Digital RSS feed", "Its feed refused requests in July 2026."),
    "mpasho": (("KE",), "Mpasho RSS feed", "Its feed address serves a web page with no stories."),
    "ewn": (("ZA",), "EWN RSS feed", "Its feed address now leads nowhere and EWN has published no replacement."),
}
CONFIG_UNAVAILABLE = "The collection settings could not be read, so the source list is missing."
HEALTH_ABSENT = "No collection record for this day yet. The roster below is what the 02:00 collection is set to read."
HEALTH_UNAVAILABLE = "The collection record could not be read, so how each source did is unknown."


def _label(market):
    return LABELS.get(market) or ("Across markets" if market == GLOBAL else market)


def _handle(name):
    return "@" + str(name).lstrip("@")


@lru_cache(maxsize=8)
def _plan(day_iso):
    """Everything the collect job plans for the day, as plain rows. Cached per day: the config is in the image."""
    from core.collect import curated_creators, google_trends, job, local_sources
    from core.collect.public_feed_collect import SERIES_BY_KIND
    from core.public_feeds.catalog import FEEDS

    day = dt.date.fromisoformat(day_iso)
    config = job.load_config()
    planned = job.plan(day, config=config)
    rows = []
    for calls in planned["calls"].values():
        for c in calls:
            rows.append({"market": c.health_market(), "series": c.series(), "route": c.route, "params": c.params,
                         "credits": c.hold(), "paid": True, "seeded": c.seed is not None, "row": c.row})
    skipped = list(planned["skipped"])
    for f in local_sources.plan(day):
        rows.append({"market": f.market, "series": f.series, "route": f.route, "params": f.params,
                     "credits": f.hold(), "paid": f.paid, "seeded": False, "row": local_sources.ROW,
                     "source": f.source, "url": f.url, "priced": f.priced()})
    for feed in FEEDS:
        if feed.supported:
            rows.append({"market": feed.market, "series": SERIES_BY_KIND[feed.kind], "route": "public_feed",
                         "params": {}, "credits": 0, "paid": False, "seeded": False, "row": "public",
                         "source": FEED_NAMES.get(feed.feed_id, feed.name), "url": feed.url})
    records = curated_creators.load_manifest()
    kept = {m: len(curated_creators.listed_handles(records, m)) for m in MARKETS}
    caps = job.load_caps()
    return {
        "rows": rows,
        "skipped": skipped,
        "unsupported_feeds": [{"market": f.market, "name": FEED_NAMES.get(f.feed_id, f.name), "reason": f.reason}
                              for f in FEEDS
                              if not f.supported],
        "hubs": config["hubs"]["markets"],
        "markets_config": config["markets"],
        "kept_creators": kept,
        "trends": {"markets": list(google_trends.MARKETS), "credits": google_trends.MAX_SC_CREDITS,
                   "tables": list(google_trends.BQ_MARKETS)},
        "collect_cap": caps["ENGINE_DAILY"]["collect"],
        "local_cap": local_sources.LOCAL_DAILY_CAP,
        "google_play_days": dict(local_sources.GOOGLE_PLAY_DAYS),
    }


def _members(series, market, rows, plan):
    """Who or what a source reads, in words a strategist knows."""
    if series == "list_reddit":
        subs = []
        for r in rows:
            sub = "r/" + str(r["params"].get("subreddit"))
            if sub not in subs:
                subs.append(sub)
        own = {str(s).lower() for s in
               (plan["markets_config"].get(market.lower(), {}).get("subreddits", {}).get("own") or ())}
        theirs = [s for s in subs if s[2:].lower() not in own]
        detail = "Hot and rising posts"
        if theirs:
            detail += "; " + ", ".join(theirs) + " is read as a genre list, not as the market's own"
        return subs, detail
    if series == "panel_x_hub":
        return [_handle(r["params"].get("handle")) for r in rows], "Posts since yesterday"
    if series == "panel_ig_gossip":
        items = [i for r in rows for i in r["params"].get("items") or []]
        return [_handle(i["handle"]) for i in items], "Posts since yesterday"
    if series == "panel_fb_hub":
        return [], f"{len(rows)} news and radio pages, posts since yesterday"
    if series == "panel_culture_desk":
        desk = len((plan["hubs"].get(market.lower()) or {}).get("culture_desk") or [])
        accounts = sum(len(r["params"].get("items") or []) for r in rows)
        kept = plan["kept_creators"].get(market)
        rotation = max(accounts - desk, 0)
        detail = f"{desk} culture desk accounts and {rotation} kept creator accounts in today's rotation"
        if kept:
            detail += f", from {kept} kept for this market"
        return [], detail
    if series == "board_youtube":
        charts = [YOUTUBE_CHARTS.get(str(r["params"].get("category", "")), "other") for r in rows]
        return [], "Charts: " + ", ".join(charts)
    if series == "board_apple_music":
        return [], "Top songs and top music videos"
    if series == "feed_tiktok":
        n = len(rows)
        return [], f"{n} reads of TikTok's own feed for the market" if n != 1 else "One read of TikTok's own feed"
    if series == "board_tiktok_hashtag":
        return [], "The market's popular hashtags over seven days"
    if series == "ig_location":
        return [], "One local place a day, in rotation"
    if series == "news_rss":
        names = []
        for r in rows:
            name = r.get("source")
            if r["route"] == "rss":
                name = RSS_NAMES.get(name, name) + " (RSS)"
            if name and name not in names:
                names.append(name)
        return names, "Headlines guide what 42 looks into; they are never evidence on their own"
    if series == "search":
        tiktok = sum(1 for r in rows if r["route"].startswith("tiktok/"))
        other = len(rows) - tiktok
        return [], f"{tiktok} TikTok searches in the market and {other} searches across other platforms"
    if series in CHART_DETAIL:
        return [], CHART_DETAIL[series](market) if callable(CHART_DETAIL[series]) else CHART_DETAIL[series]
    if series == "board_music_country":
        return [r.get("source") for r in rows if r.get("source")], "The market's own music chart"
    if series == "radio_playlist":
        return [r.get("source") for r in rows if r.get("source")], "What national radio played"
    if series == "counter_post_views":
        return [], "Up to 20 posts behind the day's evidence, reread for their latest counts"
    if series == "board_global_music":
        return [], "YouTube Shorts and Instagram music trending, read once for all markets"
    if series == "counter_tiktok_sound":
        return [], "Post counts for the TikTok sounds 42 is following"
    if series == "counter_tiktok_hashtag":
        return [], "Post counts for the TikTok hashtags 42 is following"
    if series == "curve_tiktok_sound":
        return [], "Daily use of the TikTok sounds 42 is following"
    return [], None


def _rows_by_source(plan):
    out = {}
    for r in plan["rows"]:
        out.setdefault((r["market"], r["series"]), []).append(r)
    return out


def _health_summary(rows):
    calls = sum(r.get("calls") or 0 for r in rows)
    calls_ok = sum(r.get("calls_ok") or 0 for r in rows)
    items = sum(r.get("items") or 0 for r in rows)
    valid = [r for r in rows if r.get("valid")]
    invalid = [r for r in rows if not r.get("valid")]
    shares = [r["located_share"] for r in rows if isinstance(r.get("located_share"), (int, float))]
    reasons = []
    for r in invalid:
        if invalid_code(r) == "calls" and not r.get("calls"):
            words = "no calls were made"
        else:
            words = INVALID_WORDS.get(invalid_code(r), "not usable")
        if words not in reasons:
            reasons.append(words)
    status = "delivered" if not invalid else "failed" if not valid else "partial"
    run_ids = sorted({r["run_id"] for r in rows if r.get("run_id")})
    return {
        "status": status,
        "calls": calls,
        "calls_ok": calls_ok,
        "items": figure(items, "items collected", "q_fieldwork_health", ",".join(run_ids) or None, rows),
        "located_share": shares[0] if len(rows) == 1 and shares else None,
        "located_range": [min(shares), max(shares)] if len(rows) > 1 and shares else None,
        "parts": len(rows),
        "parts_usable": len(valid),
        "reasons": reasons,
    }


def _status_words(status, summary):
    if summary is None:
        return STATUS_WORDS[status]
    if status == "partial":
        return f"Partly delivered: {summary['parts_usable']} of {summary['parts']} parts usable"
    if status == "failed":
        return "Failed: " + "; ".join(summary["reasons"])
    return STATUS_WORDS[status]


def _source(market, series, planned, health_rows, health_state, plan):
    group, name = SERIES.get(series) or ("platform", _series_name({"series": series, "platform": None}))
    members, detail = _members(series, market, planned, plan) if planned else ([], None)
    credits = sum(r["credits"] for r in planned if r.get("priced", True))
    unpriced = [r for r in planned if r.get("priced") is False]
    summary = _health_summary(health_rows) if health_rows else None
    if summary:
        status = summary["status"]
    elif health_state == "recorded":
        status = "no_record"
    elif health_state == "absent":
        status = "waiting"
    else:
        status = "unavailable"
    words = _status_words(status, summary)
    if unpriced and not summary:
        status, words = "off", "Off: the data supplier has no price for it yet, so it is never called"
    return {
        "key": f"{market}:{series}",
        "series": series,
        "group": group,
        "name": name,
        "members": members,
        "detail": detail,
        "planned_calls": len(planned),
        "planned_credits": credits,
        "free": bool(planned) and not any(r["paid"] for r in planned),
        "status": status,
        "status_words": words,
        "health": summary,
    }


def _trends_source(market, plan, collect_run, credits_rows):
    """Google Trends trending searches: search interest only, recorded in the collect run, never in health rows."""
    tables = market in plan["trends"]["tables"]
    detail = "Trending searches of the day guide what 42 looks into. They are never evidence, never a count of " \
             "posts and never proof of place."
    if tables:
        detail += " The free Google Trends tables add top and rising terms."
    state = None
    if collect_run:
        counts = collect_run.get("counts") or {}
        if isinstance(counts, str):
            try:
                counts = json.loads(counts)
            except ValueError:
                counts = {}
        state = (counts.get("search_signal_states") or {}).get(market)
    if state in TRENDS_STATE_WORDS:
        status, words = TRENDS_STATE_WORDS[state]
    elif collect_run:
        status, words = "no_record", "Not reported by this day's collection"
    else:
        status, words = "waiting", STATUS_WORDS["waiting"]
    spent = [r for r in credits_rows or [] if r.get("market") == market and r.get("lane") == "google_trending"]
    return {
        "key": f"{market}:google_trends",
        "series": "google_trends",
        "group": "search_interest",
        "name": "Google Trends trending searches",
        "members": [],
        "detail": detail,
        "planned_calls": 1,
        "planned_credits": plan["trends"]["credits"] // len(plan["trends"]["markets"]),
        "free": False,
        "status": status,
        "status_words": words,
        "health": None,
        "credits_spent": round(sum(r.get("charged") or 0 for r in spent), 4) if spent else None,
    }


def _off(plan, day):
    """Sources the config names but does not read, with the reason in plain words."""
    from core.collect import job, local_sources

    out = []
    for (source, market), (name, why) in DROPPED_SCRAPE_WORDS.items():
        if (source, market) in local_sources.DROPPED_SCRAPES:
            out.append({"market": market, "name": name, "why": why})
    for key, (markets, name, why) in DROPPED_FEED_WORDS.items():
        if key in local_sources.DROPPED_FEEDS:
            out += [{"market": m, "name": name, "why": why} for m in markets]
    for f in plan["unsupported_feeds"]:
        out.append({"market": f["market"], "name": f["name"],
                    "why": "Not read: where its chart comes from is not confirmed yet."})
    for s in plan["skipped"]:
        out.append({"market": s["market"], "name": SERIES.get(s["series"], (None, s["series"]))[1],
                    "why": "Not read: no local places are set for this market."})
    gp_days = plan["google_play_days"]
    weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    for market, weekday in gp_days.items():
        if dt.date.fromisoformat(day).weekday() != weekday:
            out.append({"market": market, "name": "Google Play top free apps",
                        "why": f"Planned for {weekdays[weekday]}s, but the data supplier has no price for it "
                               "yet."})
    out.append({"market": GLOBAL, "name": "X trends archive",
                "why": "Off until legal approves reading it."})
    out.append({"market": GLOBAL, "name": "Google Trends explore and rising searches",
                "why": "Parked until after the 6 October demo; only each market's trending searches are read."})
    missing_tables = [m for m in MARKETS if m not in plan["trends"]["tables"]]
    for m in missing_tables:
        out.append({"market": m, "name": "Google Trends free tables",
                    "why": "The public tables held no rows for this market when last checked."})
    order = {m: i for i, m in enumerate(MARKETS + (GLOBAL,))}
    return sorted(out, key=lambda r: (order.get(r["market"], 9), r["name"]))


def next_collection(now):
    """The next start of the 02:00 SAST collect job (core/setup/schedule.py), as an ISO time in SAST."""
    from core.setup.schedule import SCHEDULES

    cron = next(c for name, c, *_ in SCHEDULES if name == COLLECT_SCHEDULE)
    minute, hour = (int(x) for x in cron.split()[:2])
    local = now.astimezone(SAST)
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start <= local:
        start += dt.timedelta(days=1)
    return {"at": start.isoformat(), "date": start.date().isoformat(), "time": f"{hour:02d}:{minute:02d}",
            "zone": "SAST", "collects_for": start.date().isoformat()}


def _collect_run(store, day):
    runs = store.runs("collect", day) or []
    if not runs:
        return None
    # A run appends rows; the latest finished one carries the status.
    latest = max(runs, key=lambda r: (r.get("finished_at") is not None, str(r.get("finished_at") or ""),
                                      str(r.get("started_at") or "")))
    return latest


def _credits(rows, plan, plan_hold):
    cap = plan["collect_cap"]
    if rows is None:
        return {"state": "unavailable", "cap": cap, "planned": plan_hold, "spent": None, "markets": []}
    collect = [r for r in rows if r.get("job") == "collect"]
    run_ids = ",".join(sorted({i for r in collect for i in (r.get("run_ids") or []) if i})) or None
    total = round(sum(r.get("charged") or 0 for r in collect), 4)
    by_market = []
    for m in MARKETS + (GLOBAL,):
        mine = [r for r in collect if (r.get("market") or GLOBAL) == m]
        if mine:
            value = round(sum(r.get("charged") or 0 for r in mine), 4)
            by_market.append({"market": m, "label": _label(m),
                              "spent": figure(value, "credits charged", "q_fieldwork_credits", run_ids, mine)})
    return {"state": "recorded" if collect else "none", "cap": cap, "planned": plan_hold,
            "spent": figure(total, "credits charged", "q_fieldwork_credits", run_ids, collect) if collect else None,
            "markets": by_market}


def build_fieldwork(store, date=None, *, now=None):
    now = now or dt.datetime.now(SAST)
    today = now.astimezone(SAST).date().isoformat()
    latest = None
    try:
        latest = store.latest_collection_day(today)
    except Exception:  # a missing view reads as no record, never a 500
        latest = None
    day = date or latest or today

    try:
        plan = _plan(day)
    except Exception:
        plan = None

    try:
        health = store.collection_health(day)
        health_state = "recorded" if health else "absent"
    except Exception:
        health, health_state = [], "unavailable"
    try:
        credit_rows = store.credits(day)
    except Exception:
        credit_rows = None
    try:
        collect_run = _collect_run(store, day)
    except Exception:
        collect_run = None

    by_health = {}
    for r in health:
        by_health.setdefault((r["market"], r["series"]), []).append(r)

    markets = []
    if plan is not None:
        planned = _rows_by_source(plan)
        for m in MARKETS + (GLOBAL,):
            keys = [k for k in planned if k[0] == m] + [k for k in by_health if k[0] == m and k not in planned]
            sources = [_source(m, s, planned.get((m, s), []), by_health.get((m, s), []), health_state, plan)
                       for _, s in keys]
            if m != GLOBAL:
                sources.append(_trends_source(m, plan, collect_run, credit_rows))
            rank = {g: i for i, (g, _) in enumerate(GROUPS)}
            sources.sort(key=lambda s: rank.get(s["group"], len(rank)))
            counts = {k: sum(1 for s in sources if s["status"] == k)
                      for k in ("delivered", "partial", "failed", "no_record", "waiting", "off")}
            markets.append({"market": m, "label": _label(m), "sources": sources, "counts": counts,
                            "planned_credits": sum(s["planned_credits"] for s in sources)})
        plan_hold = sum(r["credits"] for r in plan["rows"] if r.get("priced", True)) + plan["trends"]["credits"]
    else:
        plan_hold = None

    run = None
    if collect_run:
        from core.api.coverage import redact

        run = {"run_id": collect_run.get("run_id"), "status": collect_run.get("status"),
               "started_at": collect_run.get("started_at"), "finished_at": collect_run.get("finished_at"),
               "error": redact(collect_run.get("error"))}

    return {
        "date": day,
        "today": today,
        "latest_day": latest,
        "health_state": health_state,
        "health_note": {"absent": HEALTH_ABSENT, "unavailable": HEALTH_UNAVAILABLE}.get(health_state),
        "plan_state": "ready" if plan is not None else "unavailable",
        "plan_note": None if plan is not None else CONFIG_UNAVAILABLE,
        "groups": [{"key": k, "label": v} for k, v in GROUPS],
        "markets": markets,
        "off": _off(plan, day) if plan is not None else [],
        "credits": _credits(credit_rows, plan, plan_hold) if plan is not None else None,
        "collect_run": run,
        "next_collection": next_collection(now),
    }
