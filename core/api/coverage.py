"""Coverage (core/api/contract.md section 10.4): what 42 collected, spent and ran on one day."""
import json
import os
import re
from collections import Counter
from urllib.parse import parse_qs

from core.api.discover import figure
from core.api.auth import sast_date
from core.api.today import (INVALID_WORDS, LABELS, MARKETS, NEWS_SERIES, _coverage, _news_feed_name, _series_name,
                            invalid_code)
from core.config import caps as caps_config
from core.public_feeds.catalog import FEEDS

STAGE_ORDER = ("collect", "aggregate", "stats", "coaction", "understand", "detect", "brief", "publish", "ask", "learn")
OTHER_LABELS = {"GLOBAL": "Global", None: "No market"}
NO_MODEL_USD = "Model spend is not recorded per run yet"
# Stages whose runs always record their model spend, 0.0 when no model was called (detect and learn write 0.0;
# understand_batch is left out because its spend is booked in its understand_spend row). A finished or running run
# of one of these with no model_usd leaves the day's spend unknown, never a measured zero.
MODEL_STAGES = ("understand", "understand_spend", "detect", "brief", "ask", "learn", "investigation")
NO_SCORECARD = ("No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, "
                "for the week before")  # f42-learn-mon-0730, core/setup/schedule.py
# ENGINE.md section 5, said plainly on every screen that could imply otherwise.
NOT_SEEN = [
    "WhatsApp and other private groups",
    "Official X location trends (the public archive is used only as a seed)",
    "Pinterest Trends (our data supplier does not cover South Africa, Nigeria or Kenya)",
    # True once the Google Trends collect (feat/collect-google-trends) is live: it reads each market's trending
    # searches (google_trends/trending); search interest only guides what 42 looks into, never evidence (RULES.md
    # rule 2).
    "Google Trends beyond each market's trending searches of the day, which only guide what 42 looks into",
]
# key in the response: engine_scorecard column (ENGINE.md section 6). Each column is a JSON Figure written by L2's
# core/detect/scorecard.py ({value, unit, query_id, run_id, result_hash, n, reason}), passed through as it is.
SCORECARD = {
    "time_to_detect": "time_to_detect",
    "lead_time": "lead_time",
    "precision": "precision",
    "recall": "recall",
    "breadth": "breadth_platforms",
    "cost_per_confirmed_trend": "cost_per_confirmed",
}
MARKET_ORDER = ("ZA", "NG", "KE")
# What a run that finished ok could not write (store.degraded_writes, from the understand row's counts).
DEGRADED_WORDS = {"enrich": "Enrichment"}
CLUSTER_WORDS = {"pan": "the three markets pooled"}


MAX_ERROR = 300
_URL_USER = re.compile(r"(?i)\b(https?://)[^\s/?#@]+@")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
_SCHEME = re.compile(r"(?i)\b(Bearer|Basic)\s+\S+")
# A name ending in one of these words (api_key, x-api-key, X-Passcode, access_token_v2), optionally quoted, then
# : or = and the value. The word must end the name or be followed by - _ or . so keyboard or KeyError stay.
_SECRET_PAIR = re.compile(
    r"(?i)((?<![\w.-])[\w.-]*?(?:key|token|secret|password|passcode|authorization)(?:[_.-][\w.-]*)?"
    r"[\"']?\s*[:=]\s*[\"']?)(?!(?:bearer|basic)\s)[^\s&,;\"'}]+")
# 32 or more base64 or hex characters holding both a digit and a letter: a key, a token or a signature.
_LONG_RUN = re.compile(r"(?<![\w+/-])(?=[\w+/-]*\d)(?=[\w+/-]*[A-Za-z])[\w+/-]{32,}={0,2}")
# core/collect/chain.py's begin and _duplicate reasons, which name a stage, the day and a run id: the run log reads
# them in words with no run id (the day is the page's own).
_UPSTREAM = re.compile(r"upstream (\w+) for \d{4}-\d\d-\d\d is not ok \((no runs row|latest status (\w+))\)")
_ALREADY_OK = re.compile(r"\w+ for \d{4}-\d\d-\d\d already ran ok \(run [^)]*\)")
_STILL_RUNNING = re.compile(r"\w+ for \d{4}-\d\d-\d\d is still running \(run [^)]*\)")
STALE_RUNNING = "No finish was recorded for this run; a later run of this step finished"


def redact(text):
    """Error text safe to show in the app: no URL query strings, keys or tokens, at most 300 characters."""
    if text is None:
        return None
    text = _URL_USER.sub(r"\1", str(text))
    text = _URL_QUERY.sub(r"\1", text)
    text = _SCHEME.sub(r"\1 [redacted]", text)
    text = _SECRET_PAIR.sub(r"\1[redacted]", text)
    text = _LONG_RUN.sub("[redacted]", text)
    return text[:MAX_ERROR]


def _label(market):
    return LABELS.get(market) or OTHER_LABELS.get(market) or market


def _run_ids(rows):
    ids = sorted({i for r in rows for i in (r.get("run_ids") or [r.get("run_id")]) if i})
    return ",".join(ids) or None


# The sources table, display only (owner, 5 October 2026: "can we not clean this up and display this better?"). Each
# source sits in one part of the table, carries a name no other row of its market shares, and, when its posts carry no
# place to measure, says what kind of source it is in the Located column. Nothing here changes what is counted, judged
# or called local: the summary above each market is still _summary's.
GROUPS = (("charts", "Charts and app stores"), ("boards", "Trending boards and feeds"),
          ("posts", "Posts from platforms and followed accounts"), ("news", "News feeds"), ("other", "Other sources"))
GROUP_WORDS = dict(GROUPS)
CHART_SERIES = frozenset({"board_apple_music", "board_kworb_spotify", "board_boomplay", "board_audiomack", "board_shazam",
                          "board_shazam_city", "board_turntable", "board_app_store_iphone", "board_google_play",
                          "board_music_country", "radio_playlist", "board_global_music"})
BOARD_SERIES = frozenset({"feed_tiktok", "board_tiktok_hashtag", "board_youtube", "list_reddit", "board_nairaland",
                          "x_trends"})
POST_SERIES = frozenset({"ig_location", "search", "placebo", "watch", "agent_live"})
POST_PREFIXES = ("panel_", "counter_", "curve_")
POST_LANES = frozenset({"panel", "search_presence", "watchlist", "unbiased_counter"})
# What a chart or board is, said in the Located column when its rows hold no post with a place to measure (a chart
# lists songs or apps, a hashtag board lists hashtags). Each says only what the source's own URL or parameters fix.
LOCATED_WORDS = {
    "board_apple_music": "National chart", "board_kworb_spotify": "National chart", "board_shazam": "National chart",
    "board_turntable": "National chart", "board_app_store_iphone": "National chart",
    "board_google_play": "National chart", "board_music_country": "National chart", "board_shazam_city": "City chart",
    # One trending page for every market, read with the market as the reader's country (core/collect/local_sources.py
    # by_country).
    "board_boomplay": "Seen from the market", "board_audiomack": "Seen from the market",
    "radio_playlist": "Radio station playlist", "board_tiktok_hashtag": "Market board", "board_youtube": "Market board",
    "x_trends": "Market board", "board_nairaland": "Market forum", "board_global_music": "Global board",
}
# YouTube's own public category ids as core/collect/job.py YOUTUBE_CATEGORIES reads them ("" is the overall chart).
YOUTUBE_CATEGORY_WORDS = {"": "all categories", "10": "Music", "1": "Film and animation", "20": "Gaming",
                          "24": "Entertainment"}
APPLE_TYPE_WORDS = {"songs": "songs", "music-videos": "music videos", "albums": "albums"}
SEARCH_ROUTE_WORDS = {"tiktok/search/top": "top posts", "tiktok/search/hashtag": "hashtags",
                      "search/multi": "several platforms", "tiktok/profile/videos": "creator videos",
                      "tiktok/song/videos": "song videos", "instagram/audio/reels": "audio reels",
                      "twitter/user/tweets": "account posts", "facebook/profile/posts": "page posts"}
# A platform panel's series words are the platform alone ("Facebook", "X"); the table says whose posts they are.
PANEL_WORDS = {"panel_fb_hub": "pages we follow", "panel_x_hub": "accounts we follow"}
FEED_SERIES = frozenset({NEWS_SERIES, "board_music_country", "radio_playlist"})
CATALOG_NAMES = frozenset(f.name for f in FEEDS)


def source_group(row):
    """The part of the sources table a health row sits in, from its series and lane class."""
    series, lane = row.get("series") or "", row.get("lane_class")
    if series in CHART_SERIES:
        return "charts"
    if series == NEWS_SERIES or lane == "context":
        return "news"
    if series in BOARD_SERIES:
        return "boards"
    if series in POST_SERIES or series.startswith(POST_PREFIXES) or lane in POST_LANES:
        return "posts"
    if series.startswith("board_") or lane == "unbiased_rank":
        return "boards"
    return "other"


def _protocol_parts(row):
    """(route, params) of a health row's protocol, params None when the protocol holds no query to read."""
    protocol = str(row.get("protocol") or "")
    route, mark, query = protocol.partition("?")
    params = {k: v[0] for k, v in parse_qs(query).items()} if mark else None
    return row.get("route") or route, params


def _source_detail(row):
    """What tells this row apart from another row of its series: the chart type, the board's period and industry,
    the YouTube category, the subreddit and sort, the publication or channel, or the kind of search."""
    series = row.get("series")
    route, params = _protocol_parts(row)
    if series in ("search", "placebo"):
        return SEARCH_ROUTE_WORDS.get(route)
    if series in PANEL_WORDS:
        return PANEL_WORDS[series]
    if params is None:
        return None
    if series == "feed_tiktok" and params.get("feed"):
        return f"{params['feed']} feed"
    if series == "board_apple_music" and params.get("type"):
        return APPLE_TYPE_WORDS.get(params["type"], params["type"].replace("-", " "))
    if series == "board_tiktok_hashtag":
        industry, period = params.get("industry"), params.get("period")
        parts = ["all industries" if industry == "all" else f"{industry} industry" if industry else None,
                 None if not period else "last day" if period == "1" else f"last {period} days"]
        return ", ".join(p for p in parts if p) or None
    if series == "board_youtube":
        category = params.get("category", "")
        return YOUTUBE_CATEGORY_WORDS.get(category, f"category {category}")
    if series == "list_reddit" and params.get("subreddit"):
        return ", ".join(p for p in (f"r/{params['subreddit']}", params.get("sort")) if p)
    if series in FEED_SERIES:
        name = _news_feed_name(row)
        return None if name == "News RSS" else name
    if series == "panel_telegram" and params.get("channel"):
        return params["channel"]
    return None


def _source_name(row):
    detail = _source_detail(row)
    series = row.get("series")
    if series == NEWS_SERIES:
        return f"News feed, {detail}" if detail else _series_name(row)
    if series in FEED_SERIES and detail and detail in CATALOG_NAMES:
        return detail  # a catalog chart or playlist, whose own name says what it is
    base = _series_name(row)
    return f"{base}, {detail}" if detail else base


def source_names(rows):
    """One readable name per health row of a market, none shared: the series name with what tells the row apart,
    and where two rows still read alike (unnamed Instagram places, two panels of one series), a number in protocol
    order."""
    names = [_source_name(r) for r in rows]
    counts = Counter(names)
    out = list(names)
    for name in [n for n, c in counts.items() if c > 1]:
        same = sorted((i for i, n in enumerate(names) if n == name),
                      key=lambda i: (str(rows[i].get("protocol") or ""), str(rows[i].get("platform") or ""), i))
        for n, i in enumerate(same, 1):
            r = rows[i]
            word = "place" if r.get("series") == "ig_location" else "list" if r.get("lane_class") == "panel" \
                else "source"
            out[i] = f"{name}, {word} {n}"
    return out


def located_words(row):
    """The Located column's words when a row's posts give no share to measure, else None."""
    if row.get("located_share") is not None:
        return None
    return LOCATED_WORDS.get(row.get("series"))


def _markets(health):
    extra = sorted({r["market"] for r in health if r["market"] not in MARKETS}, key=lambda m: m or "")
    out = []
    for m in list(MARKETS) + extra:
        rows = [r for r in health if r["market"] == m]
        names = source_names(rows)
        series = [{"platform": r.get("platform"), "series": r["series"], "series_words": name,
                   "group": source_group(r), "group_words": GROUP_WORDS[source_group(r)],
                   "located_words": located_words(r),
                   "calls": r.get("calls"), "calls_ok": r.get("calls_ok"), "items": r.get("items"),
                   "valid": r.get("valid"), "invalid_reason": r.get("invalid_reason"),
                   "invalid_words": None if r.get("valid") else (
                       "no calls recorded" if invalid_code(r) == "calls" and r.get("calls") == 0 else
                       INVALID_WORDS.get(invalid_code(r), "not usable")),
                   "located_share": r.get("located_share")}
                  for r, name in zip(rows, names)]
        out.append({"market": m, "label": _label(m), "series": series,
                    "summary": _summary([r for r in health if r["market"] == m])})
    return out


def _summary(rows):
    """One market's day at a glance, counted as Today counts it: posts and platforms from usable sources, the
    located share weighted by posts. None when nothing was recorded, so an empty day never reads as zero."""
    if not rows:
        return None
    day = _coverage(rows)
    return {"posts": day["posts"], "platforms": len(day["platforms"]), "located_share": day["located_share"],
            "sources": len(rows), "sources_usable": len(rows) - day["invalid_series"]}


def collection_state(health, runs):
    """What happened to collection on the day, from the store's own rows: collected (sources recorded), running,
    failed, empty (a collect run finished but recorded no source) or not_recorded (no collect run at all). A run
    still marked running after a later collect run finished ok (_superseded) does not make the day running."""
    if health:
        return "collected"
    stale = _superseded(runs)
    statuses = {r.get("status") for r in runs if r.get("stage") == "collect" and r.get("run_id") not in stale}
    if "running" in statuses:
        return "running"
    if "ok" in statuses:
        return "empty"
    if "failed" in statuses:
        return "failed"
    return "not_recorded"


def _credits(rows):
    if rows is None:
        return None
    total = round(sum(r.get("charged") or 0 for r in rows), 4)
    return {"total": figure(total, "credits charged", "q_coverage_credits", _run_ids(rows), rows),
            "rows": [{"market": r["market"], "market_label": _label(r["market"]), "job": r["job"], "lane": r["lane"],
                      "credits": figure(round(r.get("charged") or 0, 4), "credits charged", "q_coverage_credits",
                                        _run_ids([r]), [r]),
                      "calls": r.get("calls")} for r in rows]}


def _model_spend(runs, has_column, cap):
    if not has_column:
        return {"usd": None, "cap_usd": cap, "stages": [], "text": NO_MODEL_USD}
    spent = [{"run_id": r["run_id"], "stage": r["stage"], "model_usd": r["model_usd"]}
             for r in runs if r.get("model_usd") is not None]
    uncosted = sorted(r["run_id"] for r in runs if r.get("stage") in MODEL_STAGES and r.get("model_usd") is None)
    total = round(sum(r["model_usd"] for r in spent), 6)
    stages = []
    for stage in sorted({r["stage"] for r in spent}, key=_stage_rank):
        rows = [r for r in spent if r["stage"] == stage]
        stages.append({"stage": stage, "usd": figure(round(sum(r["model_usd"] for r in rows), 6), "USD",
                                                     "q_coverage_runs", _run_ids(rows), rows)})
    known = figure(total, "USD", "q_coverage_runs", _run_ids(spent), spent)
    if uncosted:
        # The recorded subtotal stays apart as known_usd, so it never reads as the day's spend against the cap.
        n = len(uncosted)
        runs_words = "1 model run has" if n == 1 else f"{n} model runs have"
        text = f"Model spend is incomplete: {runs_words} no recorded cost"
        if spent:
            text += f"; USD {total:.2f} is recorded against the USD {cap:.2f} daily model cap"
        return {"usd": None, "known_usd": known if spent else None, "uncosted_run_ids": uncosted, "cap_usd": cap,
                "stages": stages, "text": text}
    return {"usd": known, "known_usd": known, "uncosted_run_ids": [], "cap_usd": cap,
            "stages": stages, "text": f"USD {total:.2f} of the USD {cap:.2f} daily model cap"}


def _degraded(codes):
    """counts.degraded in words ("Enrichment", "Clustering for Nigeria"), or None when nothing failed."""
    out = []
    for code in codes if isinstance(codes, (list, tuple)) else []:
        step, _, market = str(code).partition(":")
        if step == "cluster" and market:
            out.append(f"Clustering for {CLUSTER_WORDS.get(market) or _label(market.upper())}")
        else:
            out.append(DEGRADED_WORDS.get(code) or str(code))
    return out or None


def run_note(error):
    """A chain reason in the reader's words, with no run id; any other error text as redact leaves it."""
    text = str(error) if error is not None else None
    upstream = _UPSTREAM.fullmatch(text.strip()) if text else None
    if upstream:
        stage, seen, status = upstream.groups()
        stage = _status_words(stage)
        if seen == "no runs row":
            return f"Did not start: the {stage} step had not run for this day yet"
        if status == "running":
            return f"Did not start: the {stage} step had not recorded a finish for this day yet"
        return f"Did not start: the {stage} step did not finish for this day ({_status_words(status)})"
    if text and _ALREADY_OK.fullmatch(text.strip()):
        return "Not run again: this step had already finished for this day"
    if text and _STILL_RUNNING.fullmatch(text.strip()):
        return "Not run again: another run of this step was still going"
    return redact(error)


def _status_words(status):
    return str(status or "").replace("_", " ")


def _run(r, superseded=False):
    """One run of the day; a run that finished ok but could not make some writes also carries degraded, what it
    could not write. A run whose last row still says running while a later run of its stage finished ok is
    superseded: its row stays as recorded, with a note saying so."""
    out = {**{k: r.get(k) for k in ("run_id", "stage", "status", "started_at", "finished_at")},
           "error": run_note(r.get("error"))}
    if superseded and r.get("status") == "running" and not out["error"]:
        out["error"] = STALE_RUNNING
    degraded = _degraded(r.get("degraded")) if r.get("status") == "ok" else None
    return {**out, "degraded": degraded} if degraded else out


def _superseded(runs):
    """run_ids whose last row says running while a run of the same stage that started later finished ok."""
    done = {}
    for r in runs:
        if r.get("status") == "ok":
            done.setdefault(r.get("stage"), []).append(str(r.get("started_at") or ""))
    return {r.get("run_id") for r in runs if r.get("status") == "running"
            and any(s > str(r.get("started_at") or "") for s in done.get(r.get("stage"), []))}


def _stage_rank(stage):
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else len(STAGE_ORDER)


FIGURE_KEYS = ("value", "unit", "query_id", "run_id", "result_hash")


def _stored(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) and "value" in value else None


def _stored_figure(value):
    """A contract Figure (section 1) from L2's stored one, which also carries n and reason."""
    stored = _stored(value)
    return {k: stored.get(k) for k in FIGURE_KEYS} if stored else None


def _scorecard(rows):
    """The latest week, one entry per market in ZA, NG, KE order; a metric missing from a row is None."""
    if not rows:
        return None
    first = rows[0]
    order = {m: i for i, m in enumerate(MARKET_ORDER)}
    markets = []
    for r in sorted(rows, key=lambda r: order.get(r.get("market"), len(order))):
        entry = {"market": r.get("market"), "reasons": {}}
        for key, column in SCORECARD.items():
            entry[key] = _stored_figure(r.get(column))
            reason = (_stored(r.get(column)) or {}).get("reason")
            if reason:
                entry["reasons"][key] = reason  # why a metric has no value this week, in L2's words
        markets.append(entry)
    return {"week": first.get("week_start"), "week_end": first.get("week_end"), "markets": markets}


def build_coverage(store, date):
    shared_cap = caps_config.model_daily_usd()
    override = os.environ.get("MODEL_DAILY_USD")
    cap = min(shared_cap, float(override)) if override else shared_cap
    runs = store.runs_of_day(date)
    health = store.collection_health(date)
    card = _scorecard(store.scorecard())
    stale = _superseded(runs)
    return {
        "date": date,
        "today": sast_date(),
        "collection": collection_state(health, runs),
        "latest_day_with_data": store.latest_collection_day(),
        "markets": _markets(health),
        "credits": _credits(store.credits(date)),
        "model_spend": _model_spend(runs, store.has_model_usd(), cap),
        "runs": [_run(r, r["run_id"] in stale)
                 for r in sorted(runs, key=lambda r: (_stage_rank(r["stage"]), r.get("started_at") or ""))],
        "not_seen": list(NOT_SEEN),
        "scorecard": card,
        "scorecard_text": None if card else NO_SCORECARD,
    }
