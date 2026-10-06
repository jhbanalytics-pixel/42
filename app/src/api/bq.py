"""BigQuery retrieval for 42.

Read-only access to the Trends Engine dataset. Every query is parameterized
and carries a 30-day window filter. The BigQuery client is created lazily so
the module imports clean with no credentials present.
"""

import json
import math
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

from src.api import geo_blocklist as geo
from src.api import source_lab
from src.api import synth

_client = None

# Per-market channel totals for share-of-voice denominators (see main._CHANNEL_TOTALS_CACHE).
_channel_totals_cache: dict[str, tuple[float, dict]] = {}
_CHANNEL_TOTALS_TTL_SECONDS = 600.0

# Seed Explorer adjacency/path payloads, keyed by (kind, market, keyword).
_seed_graph_cache: dict[str, tuple[float, dict]] = {}
_SEED_GRAPH_CACHE_TTL_SECONDS = 600.0
_SEED_GRAPH_CACHE_MAX = 64

# The latest available graph snapshot is cached separately for each market.
_latest_seed_graph_date_cache: dict[str, tuple[float, object]] = {}

_SEED_PATH_MEASURED_MIN_PLATFORMS = 2
_SEED_PATH_MEASURED_MIN_SPAN_DAYS = 2
_SEED_PATH_MEASURED_MIN_ROWS = 10
_SEED_PATH_NOT_INGESTED = frozenset()

# Free-text queries that collide with a foreign place or brand map to the
# engine topic whose blocklist disambiguates them. "sapa" pulls Vietnam
# tourism, "ankara" pulls the Turkish capital; the keyword layer strips both.
_QUERY_GEO_TOPIC = {
    "sapa": "economy_sapa_hustle",
    "sapa hustle": "economy_sapa_hustle",
    "ankara": "fashion_ankara_asoebi",
}

MARKET_LABELS = {"za": "South Africa", "ng": "Nigeria", "ke": "Kenya"}

# Human display names for the engine's snake_case topic keys. A client should
# read "Sapa hustle", not "economy_sapa_hustle". Unknown keys fall back to a
# prettifier (strip the domain prefix, title-case the rest).
_TOPIC_LABELS = {
    "sports_football": "Football",
    "film_nollywood": "Nollywood",
    "politics_maandamano": "Maandamano protests",
    "fintech_mpesa": "M-Pesa",
    "music_amapiano": "Amapiano",
    "music_gengetone": "Gengetone",
    "diaspora_japa": "Japa migration",
    "economy_sapa_hustle": "Sapa hustle",
    "politics_tinubu": "Tinubu politics",
    "genz_lifestyle": "Lifestyle",
    "education_matric_nsfas": "Matric and NSFAS",
    "economy_hustle": "Hustle economy",
    "transport_matatu": "Matatu culture",
    "music_afrobeats": "Afrobeats",
    "politics_crises": "Politics",
    "food_jollof": "Jollof",
    "genz_sheng": "Sheng",
    "infra_power_eskom": "Eskom and power",
    "culture_owambe": "Owambe",
    "sports_rugby": "Rugby and Springboks",
    "food_rituals_braai": "Braai",
    "fashion_mitumba": "Mitumba fashion",
    "finance_stokvel": "Stokvel",
    "fashion_ankara_asoebi": "Ankara and asoebi",
    "tech_gemini_ai": "Gemini & AI adoption",
}

_TOPIC_PREFIXES = (
    "music_",
    "sports_",
    "politics_",
    "economy_",
    "fintech_",
    "finance_",
    "film_",
    "food_",
    "fashion_",
    "genz_",
    "culture_",
    "diaspora_",
    "education_",
    "transport_",
    "infra_",
)


def topic_label(key: str) -> str:
    """Human display name for a topic key. Falls back to a prettifier."""
    k = (key or "").strip()
    if k in _TOPIC_LABELS:
        return _TOPIC_LABELS[k]
    body = k
    for p in _TOPIC_PREFIXES:
        if body.startswith(p):
            body = body[len(p) :]
            break
    return body.replace("_", " ").strip().title() or k


_PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "threads": "Threads",
    "reddit": "Reddit",
    "twitter": "X",
    "news": "News",
    "rss": "News",
    "web": "Web",
    "google_search": "Google Search",
    "search": "Search",
    "apple_music": "Apple Music",
    "bigquery_trends": "Search Trends",
    "gdelt": "GDELT",
}

_SYNONYMS = {"soccer": "football"}

# Engine topic keys leak into the daily verdict prose ("economy_sapa_hustle",
# "ke/sports_football"). A client must never read snake_case; these rewrite
# every reference to its human label before the text leaves the API.
_MARKET_TOPIC_REF_RE = re.compile(r"\b(za|ng|ke)/([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_BARE_TOPIC_REF_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")


def humanize_topic_refs(text: str) -> str:
    """Replace topic-key references in prose with their human labels.

    "ke/sports_football" becomes "Football in Kenya"; bare "economy_sapa_hustle"
    becomes "Sapa hustle". Only snake_case tokens are touched, so normal prose
    is never rewritten.
    """
    s = text or ""

    def _market_sub(m) -> str:
        label = topic_label(m.group(2))
        market = MARKET_LABELS.get(m.group(1), m.group(1).upper())
        return f"{label} in {market}"

    s = _MARKET_TOPIC_REF_RE.sub(_market_sub, s)
    return _BARE_TOPIC_REF_RE.sub(lambda m: topic_label(m.group(1)), s)


_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_FOREIGN_MARKERS = "ãõçñ¿¡"
_FOREIGN_STOPWORDS = {
    "que",
    "nao",
    "não",
    "voce",
    "você",
    "isso",
    "esse",
    "essa",
    "muito",
    "obrigado",
    "obrigada",
    "também",
    "então",
    "pero",
    "porque",
    "los",
    "las",
    "una",
    "este",
    "esta",
    "están",
    "más",
    "usted",
    "ahora",
}

# Platforms that are NOT a person speaking: aggregator reach rows, news category
# labels, chart and search feeds. These carry generic category text and inflated
# reach numbers (an aggregator "web" row can read 38M), so they must never appear as
# a Gen-Z voice quote or feed the synthesis evidence. Share of voice, mention
# counts and reach are voice-only too now, so an estimate row never inflates them.
_NON_VOICE_PLATFORMS = {
    "web",
    "news",
    "aggregate",
    "aggregator",
    "google_search",
    "search",
    "apple_music",
    "bigquery_trends",
    "gdelt",
    "rss",
    # Retired aggregator trending_hashtag rows arrive on platform "social":
    # estimate rows with inflated synthetic reach, not real posts. Non-voice.
    "social",
}

_ANON_HANDLES = {
    "deleted",
    "removed",
    "[deleted]",
    "[removed]",
    "unknown",
    "anonymous",
    "none",
    "n/a",
}

# News outlets posting on social are not a Gen Z voice. A Singapore outlet's
# TikTok ("asiaone") served as a quote is the failure mode: the handle layer
# drops outlet-shaped names from quotes, genz pairs, and creators lists while
# counts and aggregates keep every row. Conservative on purpose: generic media
# words as substrings, broadcast suffixes anchored to the end, known outlet
# names anchored to the start.
_OUTLET_HANDLE_RE = re.compile(
    r"news|press|media|magazine|journal|broadcast|daily"
    r"|(?:tv|fm|radio)$"
    r"|^(?:asiaone|reuters|bbc|cnn|aljazeera|sabc|citizentv"
    r"|ntvkenya|channelstv|arisetv|punchng|vanguardngr)"
    r"|^(?:enca|ewn|ktn|gma)(?:$|[^a-z])",
    re.IGNORECASE,
)


def is_outlet_handle(handle: str) -> bool:
    """True when a handle reads as a news outlet account, not a person."""
    bare = (handle or "").strip().lstrip("@").strip().lower()
    if not bare:
        return False
    return bool(_OUTLET_HANDLE_RE.search(bare))


_SEARCH_BASE_WHERE = (
    "collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
    "AND (@market = 'all' OR market = @market) "
)

# Function words that carry no retrieval signal. A multi-word ask drops them
# before matching so "world cup opening ceremony" never demands a literal
# "opening ceremony of the" phrase that no post contains.
_QUERY_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "in",
    "on",
    "for",
    "and",
    "to",
    "with",
    "this",
    "that",
}

# Under this many cleaned evidence rows the AND match reads thin and the
# search retries once with a broadened (OR of the two longest tokens) match.
_BROAD_RETRY_FLOOR = 12


def query_tokens(term: str) -> list:
    """Meaningful tokens of the normalized query, stopwords dropped.

    A query that is ALL stopwords keeps its original tokens so the search
    still matches something rather than nothing."""
    tokens = normalize_query(term).split()
    meaningful = [t for t in tokens if t not in _QUERY_STOPWORDS]
    return meaningful or tokens


def rank_by_query(items: list, term: str) -> list:
    """Reorder cleaned items by how many of the query's tokens each one contains.

    The broadened OR retry matches a row on any single token, so a "food brands"
    search pulls generic "food" posts in beside the on-point ones. Ranking by the
    count of distinct query tokens present pushes the rows that match the whole
    ask to the top; the sort is stable, so the recency-then-engagement order that
    clean_items set is kept inside each relevance tier. A one-token query needs no
    reorder and returns unchanged."""
    tokens = list(dict.fromkeys(query_tokens(term)))
    if len(tokens) < 2:
        return items

    def score(it):
        hay = " ".join(
            [
                str(it.get("text") or ""),
                str(it.get("title") or ""),
                str(it.get("hashtags") or ""),
            ]
        ).lower()
        return sum(1 for t in tokens if t in hay)

    return sorted(items, key=score, reverse=True)


def _match_clause(tokens: list, op: str) -> tuple:
    """Per-token match clause plus parameters, tokens in any order.

    Each token rides as its own regex-escaped STRING parameter and must hit
    the row text or a topic_groups entry; op joins the per-token conditions
    (AND for the strict pass, OR for the broadened retry). A single-token
    query produces the same one-parameter clause the search always used."""
    clauses = []
    params = []
    for i, tok in enumerate(tokens):
        name = "term" + str(i)
        clauses.append(
            "(REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(title, ''), ' ', IFNULL(text, ''), ' ', "
            "IFNULL(hashtags, ''))), @" + name + "_wb) "
            "OR EXISTS (SELECT 1 FROM UNNEST(topic_groups) AS tg "
            "WHERE REGEXP_CONTAINS(LOWER(tg), @" + name + ")))"
        )
        # Word-boundary the free-text match so "politics" no longer hits a GDELT
        # theme code like "uspec_politics_general1" (the underscore keeps it a
        # word char under \W). The topic_groups match stays a plain substring so
        # the query still maps onto a controlled topic like "politics_crises".
        params.append((name, "STRING", re.escape(tok)))
        params.append((name + "_wb", "STRING", r"(^|\W)" + re.escape(tok) + r"(\W|$)"))
    return "(" + (" " + op + " ").join(clauses) + ")", params


# Geo-collision exclusion pushed into the SQL WHERE so the aggregate numbers
# (total matches, daily volume, splits) and the creators list count only rows
# that survive the same keyword strip the evidence pool gets. Without it a
# query like "sapa" reports Vietnam tourism inside its match counts.
_GEO_EXCLUDE_CLAUSE = (
    " AND NOT REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(title, ''), ' ', IFNULL(text, ''), ' ', "
    "IFNULL(hashtags, ''))), @geo_pattern)"
)

# Creators must be people speaking, never an aggregator feed or a news domain
# masquerading as an author. Values come from the module constant, not input.
_NON_VOICE_SQL = (
    " AND LOWER(IFNULL(platform, '')) NOT IN ("
    + ", ".join("'" + p + "'" for p in sorted(_NON_VOICE_PLATFORMS))
    + ")"
)

# Mentions count unique conversations, not raw rows: collapse exact-duplicate
# text (reposts, syndicated copies) to one. Very short texts (emoji-only,
# "[r/sub]" stubs) would falsely collapse together, so anything under 20
# alphanumeric characters counts by id instead. COUNT(DISTINCT ...) over this
# key gives the deduped mention count.
_DEDUP_KEY_SQL = (
    "CASE WHEN LENGTH(SUBSTR(REGEXP_REPLACE(LOWER(IFNULL(text, title)), r'[^a-z0-9]', ''), 1, 120)) >= 20 "
    "THEN SUBSTR(REGEXP_REPLACE(LOWER(IFNULL(text, title)), r'[^a-z0-9]', ''), 1, 120) ELSE id END"
)


def _geo_exclusion(term: str):
    """SQL clause plus parameter for the query's geo-collision blocklist.

    Returns ("", []) when the normalized query maps to no geo topic. The
    pattern is one alternation of regex-escaped literal blocklist terms, so it
    rides as a single STRING parameter and can never inject SQL.
    """
    topic = _QUERY_GEO_TOPIC.get(normalize_query(term))
    if not topic:
        return "", []
    terms = geo.TOPIC_GEO_BLOCKLIST.get(topic, [])
    safe = [re.escape(t.strip()) for t in terms if t and len(t.strip()) >= 3]
    if not safe:
        return "", []
    return _GEO_EXCLUDE_CLAUSE, [("geo_pattern", "STRING", "|".join(safe))]


def _project() -> str:
    return os.environ.get("GCP_PROJECT", "ogilvy-trends-v2")


def _dataset() -> str:
    return os.environ.get("BQ_DATASET", "trends_v2_dev")


def _table(name: str) -> str:
    return f"`{_project()}.{_dataset()}.{name}`"


def _get_client():
    global _client
    if _client is None:
        from google.cloud import bigquery

        _client = bigquery.Client(project=_project())
    return _client


# BigQuery answers a rate or quota limit with 403 as well as a refused
# permission. Those two clear on their own; any other 403 does not.
_TRANSIENT_FORBIDDEN_REASONS = frozenset({"rateLimitExceeded", "quotaExceeded"})


def _permanent_forbidden(exc: Exception) -> bool:
    """A 403 that is a refused permission, which no retry will change."""
    from google.api_core import exceptions as gexc

    if not isinstance(exc, gexc.Forbidden):
        return False
    reasons = {
        item.get("reason") for item in (exc.errors or []) if isinstance(item, dict)
    }
    return not reasons & _TRANSIENT_FORBIDDEN_REASONS


def _run_query(sql: str, params=None, *, retries: int = 3) -> list:
    """Run a parameterized query and return rows as plain dicts.

    A list or tuple value becomes an ARRAY parameter; everything else is a
    scalar. Either way the value never touches the SQL string. Retries transient
    BigQuery client failures with exponential backoff."""
    from google.cloud import bigquery

    client = _get_client()
    query_parameters = []
    for name, type_, value in params or []:
        if isinstance(value, (list, tuple)):
            query_parameters.append(
                bigquery.ArrayQueryParameter(name, type_, list(value))
            )
        else:
            query_parameters.append(bigquery.ScalarQueryParameter(name, type_, value))
    job_config = bigquery.QueryJobConfig(
        query_parameters=query_parameters,
        job_timeout_ms=60_000,
    )
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return [
                dict(row) for row in client.query(sql, job_config=job_config).result()
            ]
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= retries or _permanent_forbidden(exc):
                raise
            time.sleep(min(2**attempt, 8))
    if last_exc is not None:
        raise last_exc
    return []


def _select_source_lab_candidate(
    candidates: list[dict[str, object]],
    *,
    client_scope_id: str,
    current_metric_date: date,
) -> dict[str, object] | None:
    matching = [
        row
        for row in candidates
        if row.get("client_scope_id") == client_scope_id
        and row.get("contract_version") == "2.1.0"
        and row.get("vendor") == "socialcrawl"
        and isinstance(row.get("run_id"), str)
        and bool(row["run_id"].strip())
        and isinstance(row.get("metric_date"), date)
        and row["metric_date"] <= current_metric_date
        and isinstance(row.get("snapshot_at"), datetime)
        and row.get("catalog_digest") in source_lab.APPROVED_CATALOGS
        and row.get("inventory_count")
        == source_lab.APPROVED_CATALOGS[row["catalog_digest"]][2]
        and row.get("balance_count") == 1
        and row.get("row_count")
        == source_lab.APPROVED_CATALOGS[row["catalog_digest"]][2] + 1
    ]
    if not matching:
        return None
    return max(
        matching,
        key=lambda row: (row["metric_date"], row["snapshot_at"], row["run_id"]),
    )


def fetch_source_lab_rows(
    client_scope_id: str,
    *,
    current_metric_date: date,
    dataset: str,
) -> list[dict[str, object]]:
    """Read the newest persisted Source Lab run from isolated staging."""
    if dataset != "trends_v2_staging" or not isinstance(current_metric_date, date):
        return []
    table = f"`{_project()}.{dataset}.source_performance_daily_v2`"
    profiles = " OR ".join(
        f"(catalog_digest = '{digest}' AND inventory_count = {profile[2]} "
        f"AND row_count = {profile[2] + 1})"
        for digest, profile in source_lab.APPROVED_CATALOGS.items()
    )
    latest_sql = (
        "SELECT contract_version, run_id, client_scope_id, vendor, metric_date, "
        "COUNTIF(route_role = 'inventory') AS inventory_count, "
        "COUNTIF(route_role = 'utility_balance') AS balance_count, "
        "COUNT(*) AS row_count, MAX(last_checked_at) AS snapshot_at, "
        "MAX(IF(route_role = 'inventory', catalog_digest, NULL)) AS catalog_digest, "
        "COUNT(DISTINCT IF(route_role = 'inventory', catalog_digest, NULL)) AS catalog_count "
        f"FROM {table} "
        "WHERE client_scope_id = @client_scope_id "
        "AND metric_date <= @current_metric_date "
        "AND contract_version = '2.1.0' "
        "AND vendor = 'socialcrawl' "
        "AND route_role IN ('inventory', 'utility_balance') "
        "GROUP BY contract_version, run_id, client_scope_id, vendor, metric_date "
        f"HAVING balance_count = 1 AND catalog_count = 1 AND ({profiles}) "
        "ORDER BY metric_date DESC, snapshot_at DESC, run_id DESC "
        "LIMIT 25"
    )
    candidates = _run_query(
        latest_sql,
        [
            ("client_scope_id", "STRING", client_scope_id),
            ("current_metric_date", "DATE", current_metric_date),
        ],
    )
    latest = _select_source_lab_candidate(
        candidates,
        client_scope_id=client_scope_id,
        current_metric_date=current_metric_date,
    )
    if latest is None:
        return []
    run_id = latest["run_id"]
    metric_date = latest["metric_date"]
    projected_fields = ", ".join(
        f"`{field}`" if field == "rows" else field
        for field in source_lab.SOURCE_PERFORMANCE_FIELDS
    )
    rows_sql = (
        f"SELECT {projected_fields} "
        f"FROM {table} "
        "WHERE client_scope_id = @client_scope_id AND run_id = @run_id "
        "AND metric_date = @metric_date "
        "ORDER BY route_role, http_method, route_path "
        f"LIMIT {source_lab.APPROVED_CATALOGS[latest['catalog_digest']][2] + 2}"
    )
    return _run_query(
        rows_sql,
        [
            ("client_scope_id", "STRING", client_scope_id),
            ("run_id", "STRING", run_id),
            ("metric_date", "DATE", metric_date),
        ],
    )


# The Listen feed shows real posts in their own words, sourced from the engine's
# own enriched_content, which carries the actual ingested post text per market
# and platform. News and web are excluded: they are the SEO-heavy surfaces that
# carry no voice. The "social" platform is excluded too: it only ever carried a
# retired aggregator's stat-summary rows, never a person speaking.
_LISTEN_PLATFORMS = (
    "tiktok",
    "twitter",
    "instagram",
    "reddit",
    "youtube",
    "facebook",
    "threads",
)

# Aggregate rows in enriched_content carry a platform name and synthetic text
# ("Link shared N times in ... trends."), never a person speaking. The pipeline
# tags them on content_type, so the feed excludes them by that column, and
# is_aggregate_text catches any digest that arrives untagged.
_LISTEN_AGGREGATE_TYPES = (
    "top_author",
    "trending_link",
    "trending_hashtag",
)


def _listen_sentiment(value) -> str:
    """A signed tone or lexicon score mapped to the Listen UI's string label."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "neutral"
    if n > 0:
        return "positive"
    if n < 0:
        return "negative"
    return "neutral"


def _listen_host(url, platform) -> str:
    from urllib.parse import urlparse

    try:
        host = urlparse(url or "").netloc.lower()
    except ValueError:
        host = ""
    if host.startswith("www."):
        host = host[4:]
    return host or (platform or "web")


def listen_feed(market: str, sentiment=None, category=None, limit: int = 30) -> dict:
    """Recent real-text posts for the Listen feed, from enriched_content.

    One market, or all three when market is 'all', newest first. Rows carry the
    actual post text, so the feed is never a wall of empty cards. Sentiment is
    derived from the lexicon or GDELT tone score. No cursor: a recency feed over
    a short window is a single page."""
    mk = (market or "").strip().lower()
    where = [
        "text IS NOT NULL",
        "LENGTH(TRIM(text)) > 0",
        "published_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)",
        "LOWER(platform) IN UNNEST(@platforms)",
        "LOWER(IFNULL(content_type, '')) NOT IN UNNEST(@aggregate_types)",
    ]
    params = [
        ("days", "INT64", 7),
        ("platforms", "STRING", list(_LISTEN_PLATFORMS)),
        ("aggregate_types", "STRING", list(_LISTEN_AGGREGATE_TYPES)),
    ]
    if mk in ("za", "ng", "ke"):
        where.append("market = @market")
        params.append(("market", "STRING", mk))
    else:
        where.append("market IN UNNEST(@markets)")
        params.append(("markets", "STRING", ["za", "ng", "ke"]))
    if category:
        where.append("LOWER(platform) = @cat")
        params.append(("cat", "STRING", category.strip().lower()))
    params.append(("lim", "INT64", max(1, min(120, limit * 3))))
    sql = f"""
        SELECT market, platform, title, text, url, published_at,
               COALESCE(sentiment_lexicon_score, tone_polarity, 0) AS sent
        FROM {_table("enriched_content")}
        WHERE {" AND ".join(where)}
        ORDER BY published_at DESC
        LIMIT @lim
    """
    rows = _run_query(sql, params)
    out = []
    want = (sentiment or "").strip().lower()
    for r in rows:
        label = _listen_sentiment(r.get("sent"))
        if want and label != want:
            continue
        if is_aggregate_text(r.get("text") or ""):
            continue
        title = (r.get("title") or "").strip() or None
        body = truncate_words(strip_urls(r.get("text") or ""), 400)
        if not (title or body):
            continue
        pub = r.get("published_at")
        out.append(
            {
                "date": pub.isoformat()
                if hasattr(pub, "isoformat")
                else (str(pub) if pub else None),
                "title": title,
                "content": body,
                "host": _listen_host(r.get("url"), r.get("platform")),
                "url": r.get("url"),
                "category": r.get("platform"),
                "sentiment": label,
                "has_text": True,
                "market": None if mk in ("za", "ng", "ke") else r.get("market"),
            }
        )
        if len(out) >= limit:
            break
    return {"results": out, "cursor": None, "has_more": False}


def normalize_query(q: str) -> str:
    """Lowercase, trim, collapse whitespace, map a few obvious synonyms."""
    words = _WS_RE.split((q or "").strip().lower())
    mapped = [_SYNONYMS.get(w, w) for w in words if w]
    return " ".join(mapped)


def truncate_words(text: str, limit: int) -> str:
    """Truncate on a word boundary near the limit, never mid-word."""
    text = _WS_RE.sub(" ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    else:
        cut = text[:limit]
    return cut.rstrip(" ,;:.") + "..."


def strip_urls(text: str) -> str:
    return _WS_RE.sub(" ", _URL_RE.sub("", text or "")).strip()


_AGGREGATE_TEXT_RE = re.compile(
    r"\d[\d,]*\s+mentions|reach\s+[\d,]+|trending in .+ trends\.|"
    r"mentions,\s*reach|song releases, downloads",
    re.IGNORECASE,
)


def is_aggregate_text(text: str) -> bool:
    """A metric digest or category label, not a person speaking.

    A retired aggregator left stat-summary rows ("#amapiano trending in South
    Africa trends. 7401 mentions, reach 23,424,833") tagged as social, and
    bucket-label rows.
    Their reach number rides in as engagement (tens of millions), so they crowd
    the voice wall with text no human wrote. Drop them from the evidence pool.
    """
    return bool(_AGGREGATE_TEXT_RE.search(text or ""))


def is_foreign(text: str) -> bool:
    """High-confidence Portuguese or Spanish detection only."""
    t = (text or "").lower()
    has_marker = any(ch in t for ch in _FOREIGN_MARKERS)
    words = set(re.findall(r"[^\W\d_]+", t, re.UNICODE))
    hits = len(words & _FOREIGN_STOPWORDS)
    if has_marker and hits >= 1:
        return True
    return hits >= 3


def _platform_label(platform: str) -> str:
    p = (platform or "").strip().lower()
    if not p:
        return "Social"
    return _PLATFORM_LABELS.get(p, p.title())


def mask_handle(handle: str, platform: str) -> str:
    """Mask anon-looking handles: numeric ids, very long strings, bot or deleted patterns."""
    masked = _platform_label(platform) + " creator"
    raw = (handle or "").strip()
    bare = raw.lstrip("@").strip()
    if not bare:
        return masked
    low = bare.lower()
    if bare.isdigit():
        return masked
    if len(bare) > 20:
        return masked
    if low in _ANON_HANDLES:
        return masked
    if low.endswith("bot"):
        return masked
    return raw


def is_real_handle(handle: str, platform: str) -> bool:
    """True only when a handle survives masking as a real name.

    A masked anon id collapses to "<Platform> creator". A list of those adds no
    value, so the caller drops them and keeps only named accounts. Outlet
    handles fail too: a news desk is never "who is driving it".
    """
    if is_outlet_handle(handle):
        return False
    generic = _platform_label(platform) + " creator"
    return mask_handle(handle, platform) != generic


def clean_creator_strings(creators: list) -> list:
    """Keep only named creators from the engine's "@handle | platform | N" strings.

    Drops rows whose handle is a numeric or random anon id (the junk that reads
    like "@ucfvyld7qmnykomnikip0a0q | youtube | 22 mentions").
    """
    out = []
    for entry in creators or []:
        if not isinstance(entry, str):
            continue
        head = entry.split("|", 1)[0].strip()
        bare = head.lstrip("@").strip()
        if (
            not bare
            or bare.isdigit()
            or len(bare) > 20
            or bare.lower() in _ANON_HANDLES
        ):
            continue
        if is_outlet_handle(head):
            continue
        out.append(entry)
    return out


_TRAJ_MOOD = {
    "rising": "Warming up",
    "improving": "Warming up",
    "positive": "Mostly positive",
    "stable": "Steady and balanced",
    "neutral": "Steady and balanced",
    "mixed": "Split opinion",
    "cooling": "Cooling off",
    "declining": "Cooling off",
    "negative": "Running tense",
}


def mood_label(trajectory: str) -> str:
    """Plain-language mood from the market sentiment trajectory.

    The trajectory often carries a qualifier in parentheses, e.g.
    "improving (+6pp positive share)" or "declining (-5pp positive share)", so
    match on the leading word rather than the whole string. Without this an
    exact-match lookup silently defaults every qualified value to neutral.
    """
    head = (trajectory or "").strip().lower().split()
    return (
        _TRAJ_MOOD.get(head[0], "Steady and balanced")
        if head
        else "Steady and balanced"
    )


# Jargon the team should never see in a sentiment read. The score numbers and
# the "tone signal / tone score" phrasing get stripped, leaving the plain
# descriptive sentence the engine wrote around them.
_SENTIMENT_STRIP = (
    re.compile(r"^\s*tone signal.*?(?:\bwith\b|:)\s*", re.IGNORECASE),
    re.compile(
        r"^\s*the (?:average )?tone score of\s*0?\.\d+\s*"
        r"(?:indicates|shows|reflects|suggests)?\s*",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*the sentiment is\s*(?:generally |moderately |highly |quite |fairly )?",
        re.IGNORECASE,
    ),
    re.compile(r"\bwith (?:a|an) 0?\.\d+ average tone score\b", re.IGNORECASE),
    re.compile(
        r"\b(?:a|an|the) (?:average )?tone (?:score|signal) of\s*0?\.\d+\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:at|of|around|near)\s*0?\.\d+\b", re.IGNORECASE),
    re.compile(r"\b0?\.\d+\s*(?:average )?(?:tone (?:score|signal))\b", re.IGNORECASE),
)


def clean_sentiment(summary: str) -> str:
    """Strip score numbers and tone-jargon, keep the plain descriptive prose.

    Returns "" for boilerplate-only text ("Tone signal not available...") so the
    caller can fall back to the plain mood label instead.
    """
    s = (summary or "").strip()
    if not s or "not available" in s.lower():
        return ""
    for rx in _SENTIMENT_STRIP:
        s = rx.sub(" ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" ,.;:")
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    if len(s) < 12:
        return ""
    return s[0].upper() + s[1:]


def _age_hours(stamp_at):
    """Hours since the stamp, None when it is absent or not a datetime."""
    if not isinstance(stamp_at, datetime):
        return None
    stamp = stamp_at if stamp_at.tzinfo else stamp_at.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - stamp).total_seconds() / 3600.0)


def best_stamp(item):
    """The post's own publish stamp when present, else the crawl stamp.

    published_at is when the person posted; collected_at is when the engine
    found it. Age must read from the former wherever the source provides it."""
    published = item.get("published_at")
    if isinstance(published, datetime):
        return published
    return item.get("collected_at")


def _recency_bucket(collected_at) -> int:
    """0 for posts within 72 hours, 1 within 7 days, 2 for older or unstamped."""
    hours = _age_hours(collected_at)
    if hours is None:
        return 2
    if hours <= 72:
        return 0
    if hours <= 7 * 24:
        return 1
    return 2


def age_label(stamp_at) -> str:
    """Compact age string from a timestamp: "3h", "2d", "3w". "" when absent."""
    hours = _age_hours(stamp_at)
    if hours is None:
        return ""
    h = int(hours)
    if h < 24:
        return str(h) + "h"
    d = h // 24
    if d < 7:
        return str(d) + "d"
    return str(d // 7) + "w"


# Voice posts older than this lose their place on the desk: a trends tool shows
# what is moving now, not an evergreen clip re-scraped months after it posted.
# A row with no stamp at all is kept; its age cannot be proven stale.
MAX_VOICE_AGE_DAYS = 45


def clean_items(items: list) -> list:
    """Strip URLs, drop non-voice and foreign junk, mask handles, dedupe near-identical texts.

    Non-voice platforms (aggregator reach rows, news category labels, chart and
    search feeds) are dropped: the voice wall and the synthesis evidence must be
    real social posts, not an aggregator category label with an inflated reach count.
    Outlet handles are dropped too: a news desk's TikTok is not a person.

    The survivors sort recency first (72-hour bucket, then 7-day, then older),
    engagement second. A stale viral post still appears when nothing fresh
    exists, but never above a fresh one.
    """
    out = []
    seen = set()
    for it in items:
        if (it.get("platform") or "").strip().lower() in _NON_VOICE_PLATFORMS:
            continue
        if is_outlet_handle(it.get("handle")):
            continue
        stamp_age = _age_hours(best_stamp(it))
        if stamp_age is not None and stamp_age > MAX_VOICE_AGE_DAYS * 24:
            continue
        text = strip_urls(it.get("text") or it.get("title") or "")
        if not text:
            continue
        if is_aggregate_text(text):
            continue
        if geo.has_foreign_script(text):
            continue
        if is_foreign(text):
            continue
        key = _NON_ALNUM_RE.sub("", text.lower())[:120]
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned = dict(it)
        cleaned["text"] = text
        cleaned["handle"] = mask_handle(it.get("handle"), it.get("platform"))
        cleaned["engagement"] = int(float(it.get("engagement") or 0))
        out.append(cleaned)
    out.sort(key=lambda c: (_recency_bucket(best_stamp(c)), -c["engagement"]))
    return out


# A trailing wall of three or more hashtags is decoration, not speech: it
# moves into a separate tags list. Shorter trailing runs stay in the text.
_TRAILING_TAGS_RE = re.compile(r"((?:\s*#\w+){3,})\s*$")
_HASHTAG_TOKEN_RE = re.compile(r"#\w+")
_MENTION_TOKEN_RE = re.compile(r"@(\w{2,})")


def top_entities(items: list, limit: int = 8) -> dict:
    """Social-native entities across a set of posts: the most frequent hashtags
    and @mentions in the matched posts' text, title and hashtag fields. This is
    real social signal, unlike the news-only GDELT v2persons / v2orgs fields, so
    it powers entity tracking on the Campaign Listening view."""
    tags: Counter = Counter()
    handles: Counter = Counter()
    for it in items:
        blob = " ".join(
            [
                str(it.get("text") or ""),
                str(it.get("title") or ""),
                str(it.get("hashtags") or ""),
            ]
        )
        for tok in _HASHTAG_TOKEN_RE.findall(blob):
            term = tok.lstrip("#").lower()
            if len(term) >= 2:
                tags[term] += 1
        for handle in _MENTION_TOKEN_RE.findall(blob):
            handles[handle.lower()] += 1
    return {
        "hashtags": [{"tag": "#" + t, "n": n} for t, n in tags.most_common(limit)],
        "handles": [{"handle": "@" + h, "n": n} for h, n in handles.most_common(limit)],
    }


def build_quotes(items: list, limit: int = 12) -> list:
    """Voice quotes from the cleaned evidence pool.

    Skips promo junk (undisclosed-ad posts, link-in-bio plugs) and tag-only
    posts; strips a trailing hashtag wall into an optional "tags" list so the
    quote reads as a sentence a person wrote.
    """
    quotes = []
    for it in items:
        if len(quotes) >= limit:
            break
        text = it["text"]
        low = text.lower()
        if "#ad " in low or low.endswith("#ad") or "link in bio" in low:
            continue
        tags: list = []
        m = _TRAILING_TAGS_RE.search(text)
        if m:
            tags = _HASHTAG_TOKEN_RE.findall(m.group(1))[:4]
            text = text[: m.start()].strip()
        if len(text.split()) < 4:
            continue
        quote = {
            "text": truncate_words(text, 240),
            "platform": it.get("platform") or "",
            "market": it.get("market") or "",
            "engagement": int(it.get("engagement") or 0),
            "handle": it.get("handle") or "",
        }
        age = age_label(best_stamp(it))
        if age:
            quote["age"] = age
        if tags:
            quote["tags"] = tags
        quotes.append(quote)
    return quotes


def zero_filled_series(
    daily_counts: dict, days: int = 30, value_key: str = "n"
) -> list:
    """Daily series, zero-filled to the trailing window ending today UTC.

    value_key names the count field ("n" for volume, "reach" for summed
    engagement) so the same fill serves the volume series and the reach series."""
    today = datetime.now(UTC).date()
    series = []
    for offset in range(days - 1, -1, -1):
        d = today - timedelta(days=offset)
        series.append({"date": d.isoformat(), value_key: int(daily_counts.get(d, 0))})
    return series


def parse_render_payload(raw) -> dict:
    """Parse trend_analysis.render_payload. Absent keys become null or empty lists."""
    out = {
        "display": None,
        "comment_sentiment": None,
        "comment_themes": [],
        "driving_hashtags": [],
        "forecast_outlook": None,
    }
    if not raw or not isinstance(raw, str):
        return out
    try:
        payload = json.loads(raw)
    except ValueError:
        return out
    if not isinstance(payload, dict):
        return out

    disp = payload.get("display")
    if isinstance(disp, dict):
        state_raw = disp.get("state")
        state = None
        if isinstance(state_raw, dict):
            state = {
                "badge": state_raw.get("badge")
                if isinstance(state_raw.get("badge"), str)
                else None,
                "direction": state_raw.get("direction")
                if isinstance(state_raw.get("direction"), str)
                else None,
            }
        pct_raw = disp.get("in_market_pct")
        in_market_pct = (
            int(round(float(pct_raw))) if isinstance(pct_raw, (int, float)) else None
        )
        channels = []
        if isinstance(disp.get("channels"), list):
            for entry in disp["channels"]:
                if (
                    isinstance(entry, (list, tuple))
                    and len(entry) >= 2
                    and isinstance(entry[0], str)
                    and isinstance(entry[1], (int, float))
                ):
                    channels.append([entry[0], int(round(float(entry[1])))])
        out["display"] = {
            "state": state,
            "phase": disp.get("phase") if isinstance(disp.get("phase"), str) else None,
            "window": disp.get("window")
            if isinstance(disp.get("window"), str)
            else None,
            "in_market_pct": in_market_pct,
            "channels": channels,
            "confidence": disp.get("confidence")
            if isinstance(disp.get("confidence"), str)
            else None,
            "search": disp.get("search")
            if isinstance(disp.get("search"), str)
            else None,
        }

    if isinstance(payload.get("comment_sentiment"), str):
        out["comment_sentiment"] = payload["comment_sentiment"]
    if isinstance(payload.get("comment_themes"), list):
        out["comment_themes"] = [
            t for t in payload["comment_themes"] if isinstance(t, str)
        ]
    if isinstance(payload.get("driving_hashtags"), list):
        hashtags = []
        for h in payload["driving_hashtags"]:
            if isinstance(h, dict) and isinstance(h.get("tag"), str):
                share = h.get("share_pct")
                hashtags.append(
                    {
                        "tag": h["tag"],
                        "share_pct": float(share)
                        if isinstance(share, (int, float))
                        else 0.0,
                        "mood": h.get("mood") if isinstance(h.get("mood"), str) else "",
                    }
                )
        out["driving_hashtags"] = hashtags
    # The engine's 7-day BQML forecast rides inside render_payload as a plain
    # string (heating/steady/cooling) once FORECAST_ENABLED is on. It is not a
    # column on trend_analysis and is absent on every row today, so anything
    # outside the known set collapses to None and the desk renders no chip.
    outlook = payload.get("forecast_outlook")
    if isinstance(outlook, str) and outlook.strip().lower() in {
        "heating",
        "steady",
        "cooling",
    }:
        out["forecast_outlook"] = outlook.strip().lower()
    return out


def momentum_from_velocity(velocity_score, trend_score) -> str:
    """Rails momentum from the curated view's velocity and score."""
    v = float(velocity_score or 0.0)
    s = float(trend_score or 0.0)
    if s < 0.30 and v > 0.10:
        return "Building"
    if v > 0.05:
        return "Rising"
    if v < -0.05:
        return "Cooling"
    return "Steady"


# Wave 1 (TEV2 roadmap) writes three new STRING columns on trend_scores once the
# combined migration applies and the engine flags flip: momentum_label,
# lifecycle_phase, continuity_state. They are absent on every row today, so each
# reader whitelists against the locked value set and returns None otherwise. The
# desk attaches the field only when it is a known value, so the badge no-ops
# until the engine populates it, byte-identical to today.
_MOMENTUM_LABELS = {"rising", "building", "steady", "cooling"}
_LIFECYCLE_PHASES = {"birth", "growth", "maturity", "decline"}
_CONTINUITY_STATES = {"new", "day2", "day3plus", "rebounding"}


def _clean_enum(value, allowed: set):
    """Lower-cased, trimmed value when it is in the allowed set, else None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    return v if v in allowed else None


def clean_momentum_label(value):
    """The engine's stored momentum_label (rising/building/steady/cooling)."""
    return _clean_enum(value, _MOMENTUM_LABELS)


def momentum_label_from_windows(velocity_7d, velocity_30d):
    """Fallback momentum read from the 7-day vs 30-day velocity windows.

    Used only when the engine has not written momentum_label yet but the two
    velocity windows are present: rising when the short window leads the long by
    a clear band, cooling when it trails, building when positive-but-flat, else
    steady. Returns None when either window is missing so nothing is invented."""
    if not isinstance(velocity_7d, (int, float)) or not isinstance(
        velocity_30d, (int, float)
    ):
        return None
    short = float(velocity_7d)
    long = float(velocity_30d)
    band = 0.05
    if short > long + band:
        return "rising"
    if short < long - band:
        return "cooling"
    if short > band:
        return "building"
    return "steady"


def clean_lifecycle_phase(value):
    """The engine's stored lifecycle_phase (birth/growth/maturity/decline)."""
    return _clean_enum(value, _LIFECYCLE_PHASES)


def clean_continuity_state(value):
    """The engine's stored continuity_state (new/day2/day3plus/rebounding)."""
    return _clean_enum(value, _CONTINUITY_STATES)


# Tags must carry at least one letter; an HTML entity like &#8217; or a bare
# number run is never a hashtag a person typed.
_TAG_HAS_LETTER_RE = re.compile(r"[a-z]")

_HASHTAG_CACHE: dict = {}
_HASHTAG_CACHE_TTL_SECONDS = 900.0


def derive_topic_hashtags(market: str, top_n: int = 6) -> dict:
    """Real driving hashtags per (market, topic), pulled from the posts.

    The engine's render_payload.driving_hashtags is empty on the day a run
    has not yet populated the conversation layer, but the hashtags live in the
    post text. One batched query extracts them with REGEXP_EXTRACT_ALL, groups
    by market and topic, and returns the top tags with their share of that
    group's tagged posts. HTML entities are stripped before extraction so a
    numeric entity can never surface as a tag, geo-collision tags are dropped
    for blocklisted topics, and results cache in-process for 15 minutes.
    Nothing is fabricated: every tag and count traces to real rows.
    """
    now = time.time()
    cached = _HASHTAG_CACHE.get(market)
    if cached is not None and now - cached[0] < _HASHTAG_CACHE_TTL_SECONDS:
        return cached[1]
    sql = (
        "SELECT market, tg AS topic, htag AS tag, COUNT(*) AS n FROM "
        + _table("enriched_content")
        + ", UNNEST(topic_groups) AS tg, "
        "UNNEST(REGEXP_EXTRACT_ALL(REGEXP_REPLACE("
        "LOWER(CONCAT(IFNULL(title,''),' ',IFNULL(text,''),' ',IFNULL(hashtags,''))), "
        r"r'&#?[a-z0-9]{1,10};', ' '), "
        r"r'#[a-z0-9_]{2,30}')) AS htag "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market) "
        "GROUP BY market, topic, tag"
    )
    rows = _run_query(sql, [("market", "STRING", market)])
    by_group: dict[tuple, Counter] = {}
    for r in rows:
        mkt = r.get("market")
        t = r.get("topic")
        tag = r.get("tag")
        if not mkt or not t or not tag:
            continue
        if not _TAG_HAS_LETTER_RE.search(tag):
            continue
        if t in geo.TOPIC_GEO_BLOCKLIST and geo.text_matches_geo_blocklist(tag, t):
            continue
        by_group.setdefault((mkt, t), Counter())[tag] += int(r.get("n") or 0)
    out: dict[tuple, list] = {}
    for group, counter in by_group.items():
        total = sum(counter.values()) or 1
        out[group] = [
            {"tag": tag, "share_pct": round(100.0 * n / total, 1), "mood": ""}
            for tag, n in counter.most_common(top_n)
        ]
    _HASHTAG_CACHE[market] = (now, out)
    return out


def parse_social_refs(refs) -> list:
    """Structure the engine's "url | platform | title" strings.

    Each entry becomes {"url", "platform", "title"} with missing parts None.
    Entries without a valid http url are skipped: a ref the client cannot
    open is not a reference.
    """
    out = []
    for entry in refs or []:
        if not isinstance(entry, str):
            continue
        parts = [p.strip() for p in entry.split(" | ", 2)]
        url = parts[0] if parts else ""
        if not url.lower().startswith(("http://", "https://")):
            continue
        out.append(
            {
                "url": url,
                "platform": parts[1] if len(parts) > 1 and parts[1] else None,
                "title": parts[2] if len(parts) > 2 and parts[2] else None,
            }
        )
    return out


def _build_brief(row: dict) -> dict:
    rp = parse_render_payload(row.get("render_payload"))
    market = row.get("market") or ""
    return {
        "market": market,
        "market_label": MARKET_LABELS.get(market, market.upper()),
        "topic": row.get("query_group"),
        "topic_label": topic_label(row.get("query_group") or ""),
        "headline": row.get("headline"),
        "synthesis": row.get("trend_synthesis"),
        "context": row.get("cultural_context"),
        "status_tag": row.get("status_tag"),
        "trend_score": float(row.get("trend_score") or 0.0),
        "platforms": list(row.get("platforms") or []),
        "top_creators": clean_creator_strings(row.get("top_creators")),
        "campaign_angles": [
            a for a in (row.get("campaign_angles") or []) if isinstance(a, str)
        ],
        "social_refs": parse_social_refs(row.get("social_refs")),
        "visual_anchor": row.get("visual_anchor"),
        "nano_banana_prompt": row.get("nano_banana_prompt"),
        "lyria_prompt": row.get("lyria_prompt"),
        "sentiment_summary": row.get("sentiment_summary"),
        "sentiment_read": clean_sentiment(row.get("sentiment_summary")),
        "b24_sentiment_trajectory": row.get("b24_sentiment_trajectory"),
        "mood_label": mood_label(row.get("b24_sentiment_trajectory")),
        "display": rp["display"],
        "comment_sentiment": rp["comment_sentiment"],
        "comment_themes": rp["comment_themes"],
        "driving_hashtags": rp["driving_hashtags"],
    }


def _topic_chip(token: str) -> dict:
    """Chip object for a verdict topic token.

    "ke/politics_maandamano" becomes {"query": "politics_maandamano",
    "market": "ke", "label": "Maandamano protests in Kenya"}: the chip
    displays the label and searches the bare topic key, which is what
    topic_groups match on.
    """
    raw = (token or "").strip()
    if "/" in raw:
        market, bare = raw.split("/", 1)
        market = market.strip().lower()
    else:
        market = ""
        bare = raw
    chip = {"query": bare, "label": humanize_topic_refs(raw)}
    if market:
        chip["market"] = market
    return chip


FRESHNESS_AMBER_HOURS = 3.0


def refresh_freshness(freshness: dict | None) -> dict:
    """Re-derive age_hours and status from stamp_utc against the clock NOW.

    The stamp is a fact about the day's run and is safe to cache. Age and
    status are not: they are functions of the current time, so a cached copy
    freezes and then lies, and it lies in the reassuring direction. Every serve
    path runs its cached stamp back through here.
    """
    stamp_iso = (freshness or {}).get("stamp_utc")
    if not stamp_iso:
        return {"stamp_utc": None, "age_hours": None, "status": "amber"}
    stamp = datetime.fromisoformat(stamp_iso)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    # Clamp at 0 so clock skew (a stamp slightly in the future) never reports a
    # negative age, the same guard _age_hours uses.
    age_hours = max(0.0, (datetime.now(UTC) - stamp).total_seconds() / 3600.0)
    # The sent age is cut to two decimals, never rounded up, and the status is
    # decided on that same value. The shell reads its stale state back out of
    # this age from the same 3 hours, so a rounded 2.996 sent as 3.0 beside a
    # green status made it call the check stale early. The product is rounded
    # to six places before the cut so an exact hundredth such as 1.15, whose
    # binary product is 114.999..., is not cut to the one below.
    sent_age = math.floor(round(age_hours * 100, 6)) / 100
    return {
        "stamp_utc": stamp.isoformat(),
        "age_hours": sent_age,
        "status": "green" if sent_age < FRESHNESS_AMBER_HOURS else "amber",
    }


def _freshness() -> dict:
    """Latest success stamp for today from pipeline_runs, falling back to
    MAX(collected_at) when the day has no success row (a run that dies
    mid-flight writes no row)."""
    sql = (
        "SELECT MAX(finished_at) AS stamp FROM " + _table("pipeline_runs") + " "
        "WHERE status = 'success' "
        "AND started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND DATE(finished_at) = CURRENT_DATE()"
    )
    rows = _run_query(sql)
    stamp = rows[0].get("stamp") if rows else None
    if stamp is None:
        fallback_sql = (
            "SELECT MAX(collected_at) AS stamp FROM " + _table("enriched_content") + " "
            "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
        )
        rows = _run_query(fallback_sql)
        stamp = rows[0].get("stamp") if rows else None
    if stamp is None:
        return {"stamp_utc": None, "age_hours": None, "status": "amber"}
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return refresh_freshness({"stamp_utc": stamp.isoformat()})


def _voice_platform_count_sql(col: str = "platform") -> str:
    """Distinct voice-platform count expression (matches fetch_growth_metrics)."""
    nonvoice = ", ".join("'" + p + "'" for p in sorted(_NON_VOICE_PLATFORMS))
    return (
        "COUNT(DISTINCT IF(LOWER(IFNULL("
        + col
        + ", '')) NOT IN ("
        + nonvoice
        + ") AND TRIM(IFNULL("
        + col
        + ", '')) != '', LOWER("
        + col
        + "), NULL))"
    )


def fetch_ingest_cache_version() -> str:
    """Fingerprint ingest freshness and the live voice-platform mix.

    Busts server caches when a mid-day backfill lands or a new connector
    (e.g. twitter) appears in enriched_content, without waiting for TTL."""
    sql = (
        "SELECT FORMAT_TIMESTAMP('%s', MAX(collected_at)) AS m, "
        + _voice_platform_count_sql()
        + " AS voice_plat, "
        "COUNTIF(LOWER(IFNULL(platform, '')) = 'twitter' "
        "AND IFNULL(source, '') = 'EnsembleData' "
        "AND DATE(collected_at) = CURRENT_DATE()) AS tw "
        "FROM "
        + _table("enriched_content")
        + " WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
    )
    rows = _run_query(sql)
    if rows:
        row = rows[0]
        return (
            f"{row.get('m') or '0'}:{row.get('voice_plat') or 0}:{row.get('tw') or 0}"
        )
    return "0:0:0"


def fetch_growth_metrics() -> dict:
    """Engine totals for the Today header, each traceable to a row you can reach.

    posts is the cumulative ingested-row count (all connectors, never pruned).
    trends is the count of DISTINCT trends the engine follows, not the old
    SUM(trends_scored) which summed every daily re-score of the same ~50 topics
    into a 1,800+ figure that overstated the tracked count ~60x and drifted from
    its own table. briefs is the real brief count from trend_analysis: the
    pipeline_runs briefs_generated counter was broken (most runs logged 0 while
    still writing briefs, undercounting ~56%). creators/platforms/markets read
    the live 30-day window; platforms excludes the non-voice feed labels (news,
    web, search, aggregate) the rest of this module already strips, so it counts
    real platforms, not source buckets."""
    totals = _run_query(
        "SELECT "
        "(SELECT SUM(total_rows) FROM " + _table("pipeline_runs") + ") AS posts, "
        "(SELECT COUNT(DISTINCT query_group) FROM "
        + _table("trend_scores")
        + ") AS trends, "
        "(SELECT COUNT(DISTINCT CONCAT(market, '|', query_group, '|', CAST(trend_date AS STRING))) "
        "FROM " + _table("trend_analysis") + ") AS briefs"
    )
    t = totals[0] if totals else {}
    live = _run_query(
        "SELECT "
        "COUNT(DISTINCT IF(author_handle IS NOT NULL AND TRIM(author_handle) != '', "
        "author_handle, NULL)) AS creators, "
        + _voice_platform_count_sql()
        + " AS platforms, "
        "COUNT(DISTINCT market) AS markets "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
    )
    r = live[0] if live else {}
    return {
        "posts": int(t.get("posts") or 0),
        "briefs": int(t.get("briefs") or 0),
        "trends": int(t.get("trends") or 0),
        "creators": int(r.get("creators") or 0),
        "platforms": int(r.get("platforms") or 0),
        "markets": int(r.get("markets") or 0),
    }


# Reach is summed engagement_total (views + likes + comments + shares), which is
# views-dominated. A handful of estimated-reach rows (aggregator "aggregate" rows
# average ~50M, a single mega-estimate reaches ~390M) would otherwise dominate
# every total and outweigh thousands of real posts. So every reach figure below
# winsorizes each row at 10_000_000 (~the p99 of engagement_total across markets)
# via a per-row LEAST(..., 10000000) cap: estimate spikes are tamed,
# real posts untouched. Change the cap by find-replacing the 10000000 literal.

# Reddit's automation account is highly active but is not an influencer; drop it
# and any obvious bot from the creator board after the query.
_BOT_AUTHOR_HANDLES = frozenset({"automoderator"})


def fetch_top_authors(market: str, limit: int = 30) -> list:
    """The influencer CRM: the named creators driving the engine's tracked
    conversation in one market, ranked by the reach they command.

    Sourced from the engine's own enriched_content, not a vendor follower list.
    These are the geo-filtered SSA creators the pipeline already collects across
    TikTok, Instagram, Threads, and YouTube, so the board reads as local voices,
    not the global brands a follower-ranked surface returns. A creator needs at
    least two posts in the window to count as active rather than a one-off viral
    hit; anon ids, news desks, and bots are dropped after the query. Each row
    carries the creator's top topics so the board reads as intelligence, not a
    bare leaderboard."""
    mk = (market or "").strip().lower()
    if mk not in MARKET_LABELS:
        return []
    # A handle posts across markets, and a post's market is the seed topic's
    # market, not the creator's home. Filtering posts by market then grouping by
    # handle surfaced foreign creators (a NG handle pulled into a ZA topic on a
    # couple of posts). So first resolve each handle's home market as the one it
    # posts in most over the window, and keep a handle on this board only when
    # this market is its home. Reach and mentions still count this market's slice.
    sql = (
        "WITH home AS ("
        "SELECT author_handle AS h, market, "
        "ROW_NUMBER() OVER (PARTITION BY author_handle ORDER BY COUNT(*) DESC) AS mrank "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND author_handle IS NOT NULL AND TRIM(author_handle) != ''"
        + _NON_VOICE_SQL
        + " "
        "GROUP BY author_handle, market) "
        "SELECT author_handle AS handle, ANY_VALUE(platform) AS platform, "
        "COUNT(*) AS mentions, SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) AS reach, "
        "ARRAY_CONCAT_AGG(IFNULL(topic_groups, [])) AS topic_bag "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market = @market "
        "AND author_handle IS NOT NULL AND TRIM(author_handle) != ''"
        + _NON_VOICE_SQL
        + " "
        "AND author_handle IN (SELECT h FROM home WHERE market = @market AND mrank = 1) "
        "GROUP BY author_handle "
        "HAVING mentions >= 2 "
        "ORDER BY reach DESC "
        "LIMIT @lim"
    )
    rows = _run_query(
        sql, [("market", "STRING", mk), ("lim", "INT64", max(limit * 3, 60))]
    )
    out = []
    for r in rows:
        handle = r.get("handle")
        platform = r.get("platform")
        if not is_real_handle(handle, platform):
            continue
        if (handle or "").strip().lstrip("@").lower() in _BOT_AUTHOR_HANDLES:
            continue
        bag = [t for t in (r.get("topic_bag") or []) if t]
        topics = [topic_label(t) for t, _ in Counter(bag).most_common(2)]
        out.append(
            {
                "name": (handle or "").strip().lstrip("@"),
                "platform": _platform_label(platform),
                "mentions": int(r.get("mentions") or 0),
                "reach": int(r.get("reach") or 0),
                "topics": topics,
            }
        )
        if len(out) >= limit:
            break
    return out


def fetch_today(market: str = "all") -> dict:
    latest_sql = (
        "SELECT MAX(trend_date) AS d FROM " + _table("trend_analysis") + " "
        "WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)"
    )
    rows = _run_query(latest_sql)
    latest = rows[0].get("d") if rows else None
    freshness = _freshness()
    if latest is None:
        return {"date": None, "freshness": freshness, "verdict": None, "briefs": []}

    # The briefs and verdict are static once the day's 00:30 UTC cron lands, so
    # cache the heavy assembly keyed by the data's own date. Freshness stays live
    # (two cheap MAX queries) so the staleness badge never freezes, and keying by
    # latest self-invalidates the moment new data arrives. In-process on the warm
    # instance, GCS-backed for new ones.
    cache_key = ("today", market, latest.isoformat())
    cached = synth.cache_get(cache_key)
    if cached is not None:
        return {
            "date": cached["date"],
            "freshness": freshness,
            "verdict": cached["verdict"],
            "briefs": cached["briefs"],
        }

    briefs_sql = (
        "SELECT market, query_group, headline, trend_synthesis, cultural_context, "
        "status_tag, trend_score, platforms, top_creators, campaign_angles, social_refs, "
        "visual_anchor, nano_banana_prompt, lyria_prompt, sentiment_summary, "
        "b24_sentiment_trajectory, render_payload "
        "FROM " + _table("trend_analysis") + " "
        "WHERE trend_date = @d "
        "AND trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market)"
    )
    brief_rows = _run_query(
        briefs_sql, [("d", "DATE", latest), ("market", "STRING", market)]
    )
    briefs = [_build_brief(r) for r in brief_rows]
    # The conversation layer can be empty on the current day's run; backfill the
    # driving hashtags from the real posts so the card is never blank where the
    # signal exists. Only fills when the engine's own list is empty.
    derived = derive_topic_hashtags(market)
    for b in briefs:
        if not b.get("driving_hashtags"):
            b["driving_hashtags"] = derived.get((b["market"], b["topic"]), [])
    briefs.sort(key=lambda b: b["trend_score"], reverse=True)

    # The verdict must speak to the briefs on the page: prefer the row for the
    # briefs' own trend_date, fall back to the latest earlier non-empty row,
    # and stamp the date so the client can label a stale verdict.
    verdict_sql = (
        "SELECT trend_date, through_line, summary_text, call_to_action, "
        "key_topics, rising_topics "
        "FROM " + _table("daily_summary") + " "
        "WHERE trend_date <= @d "
        "AND trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND summary_text IS NOT NULL AND TRIM(summary_text) != '' "
        "ORDER BY trend_date DESC, generated_at DESC LIMIT 1"
    )
    verdict_rows = _run_query(verdict_sql, [("d", "DATE", latest)])
    verdict = None
    if verdict_rows:
        v = verdict_rows[0]
        v_date = v.get("trend_date")
        verdict = {
            "verdict_date": v_date.isoformat() if v_date is not None else None,
            "through_line": humanize_topic_refs(v.get("through_line")),
            "summary_text": humanize_topic_refs(v.get("summary_text")),
            "call_to_action": humanize_topic_refs(v.get("call_to_action")),
            "key_topics": [_topic_chip(t) for t in (v.get("key_topics") or [])],
            "rising_topics": [_topic_chip(t) for t in (v.get("rising_topics") or [])],
        }

    synth.cache_set(
        cache_key, {"date": latest.isoformat(), "verdict": verdict, "briefs": briefs}
    )
    return {
        "date": latest.isoformat(),
        "freshness": freshness,
        "verdict": verdict,
        "briefs": briefs,
    }


def fetch_rails(market: str = "all") -> dict:
    latest_sql = (
        "SELECT MAX(trend_date) AS d FROM " + _table("trend_analysis") + " "
        "WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)"
    )
    rows = _run_query(latest_sql)
    latest = rows[0].get("d") if rows else None
    if latest is None:
        return {"date": None, "rails": []}

    rails_sql = (
        "SELECT a.query_group AS topic_group, LOWER(a.market) AS market, "
        "CASE WHEN LOWER(a.market) = 'za' THEN 'South Africa' "
        "WHEN LOWER(a.market) = 'ng' THEN 'Nigeria' "
        "WHEN LOWER(a.market) = 'ke' THEN 'Kenya' ELSE UPPER(a.market) END AS market_label, "
        "a.headline, "
        "CASE WHEN a.trend_score >= 0.45 THEN 'Trending' "
        "WHEN a.trend_score >= 0.30 THEN 'Emerging' "
        "WHEN a.trend_score >= 0.18 THEN 'Monitoring' ELSE 'Below threshold' END AS tier, "
        "a.trend_score, s.velocity_score, a.trend_synthesis AS description_rationale "
        "FROM " + _table("trend_analysis") + " a "
        "LEFT JOIN " + _table("trend_scores") + " s "
        "ON s.trend_date = a.trend_date AND LOWER(s.market) = LOWER(a.market) "
        "AND s.query_group = a.query_group "
        "WHERE a.trend_date = @d "
        "AND a.trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR LOWER(a.market) = @market) "
        "ORDER BY a.trend_score DESC, a.query_group ASC"
    )
    rail_rows = _run_query(
        rails_sql, [("d", "DATE", latest), ("market", "STRING", market)]
    )
    rails = []
    for r in rail_rows:
        velocity = float(r.get("velocity_score") or 0.0)
        rails.append(
            {
                "topic": r.get("topic_group"),
                "topic_label": topic_label(r.get("topic_group") or ""),
                "market": r.get("market"),
                "market_label": r.get("market_label"),
                "headline": r.get("headline"),
                "tier": r.get("tier"),
                "momentum": momentum_from_velocity(velocity, r.get("trend_score")),
                "velocity": round(velocity, 4),
                "score": float(r.get("trend_score") or 0.0),
                "read": truncate_words(r.get("description_rationale") or "", 140),
            }
        )
    # "Moving this week" leads with what is accelerating, not the seed list in
    # taxonomy order: sort by velocity, then absolute score as the tie-break.
    rails.sort(key=lambda x: (x["velocity"], x["score"]), reverse=True)
    return {"date": latest.isoformat(), "rails": rails}


def fetch_desk_rows(market: str) -> list:
    """Latest-day score rows joined to their briefs, one query.

    trend_scores carries the numbers (score, velocity, tone, item_count);
    trend_analysis carries the brief prose. The latest day rides as a subselect
    so the desk never spends a separate round trip on MAX(trend_date)."""
    sql = (
        "SELECT s.trend_date, s.market, s.query_group, s.trend_score, "
        "s.velocity_score, s.tone_score, s.tone_rows, s.item_count, s.seed_score, "
        # Wave 1 momentum/lifecycle/continuity columns. They exist on
        # trend_scores after the combined Wave 1 migration and are NULL on every
        # row until the engine flags flip, so SELECTing them changes nothing
        # today and the desk's reads collapse to None.
        "s.momentum_label, s.lifecycle_phase, s.continuity_state, "
        "a.headline, a.trend_synthesis, a.cultural_context, a.sentiment_summary, "
        "a.b24_sentiment_trajectory, "
        "a.campaign_angles, a.top_creators, a.platforms, a.render_payload, "
        "a.nano_banana_prompt, a.lyria_prompt, a.social_refs "
        "FROM " + _table("trend_scores") + " s "
        "LEFT JOIN " + _table("trend_analysis") + " a "
        "ON a.market = s.market AND a.query_group = s.query_group "
        "AND a.trend_date = s.trend_date "
        "WHERE s.trend_date = (SELECT MAX(trend_date) FROM "
        + _table("trend_scores")
        + " "
        "WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)) "
        "AND s.trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR s.market = @market) "
        "ORDER BY s.trend_score DESC"
    )
    return _run_query(sql, [("market", "STRING", market)])


def fetch_seed_insights() -> list:
    """The latest day's seed-intelligence behaviours, ranked.

    seed_insights carries the hidden seedable behaviours a cross-topic Gemini
    pass mined from the briefs. The latest day rides as a subselect so the desk
    never spends a round trip on MAX(trend_date)."""
    sql = (
        "SELECT trend_date, rank, behaviour, the_shift, evidence, why_hidden, "
        "timing, markets, brand_opportunity, activation_tool, activation_angle, "
        "activation_prompt, signal_strength "
        "FROM " + _table("seed_insights") + " "
        "WHERE trend_date = (SELECT MAX(trend_date) FROM "
        + _table("seed_insights")
        + " "
        "WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)) "
        "ORDER BY rank"
    )
    return _run_query(sql)


def fetch_score_history(latest, market: str) -> list:
    """30-day trend_score history for every (market, query_group), one query.

    Feeds the per-topic series (30 points; the client takes the trailing window
    each view needs) and the day-over-day delta, so the desk never issues a
    per-topic history query."""
    sql = (
        "SELECT market, query_group, trend_date, trend_score "
        "FROM " + _table("trend_scores") + " "
        "WHERE trend_date BETWEEN DATE_SUB(@d, INTERVAL 29 DAY) AND @d "
        "AND trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market)"
    )
    return _run_query(sql, [("d", "DATE", latest), ("market", "STRING", market)])


def fetch_digest(latest):
    """The engine's daily digest for the desk: the latest non-empty
    daily_summary row at or before the desk's trend_date, topic keys
    humanized. None when the window has no usable row."""
    sql = (
        "SELECT trend_date, through_line, summary_text, call_to_action, "
        "key_topics, rising_topics "
        "FROM " + _table("daily_summary") + " "
        "WHERE trend_date <= @d "
        "AND trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND summary_text IS NOT NULL AND TRIM(summary_text) != '' "
        "ORDER BY trend_date DESC, generated_at DESC LIMIT 1"
    )
    rows = _run_query(sql, [("d", "DATE", latest)])
    if not rows:
        return None
    row = rows[0]
    row_date = row.get("trend_date")
    return {
        "trend_date": row_date.isoformat() if row_date is not None else None,
        "through_line": humanize_topic_refs(row.get("through_line")),
        "summary_text": humanize_topic_refs(row.get("summary_text")),
        "call_to_action": humanize_topic_refs(row.get("call_to_action")),
        "key_topics": [_topic_chip(t) for t in (row.get("key_topics") or [])],
        "rising_topics": [_topic_chip(t) for t in (row.get("rising_topics") or [])],
    }


def _relevance_sql(topic_fit: str | None = None) -> str:
    """Composite relevance-score expression for an ORDER BY.

    Blends topic fit (when ranking inside a topic), platform-normalized
    (log-compressed) engagement, the engine's regional and slang signals,
    and log recency, then scales by a platform-credibility multiplier so an
    aggregator or news row can never outrank a real voice. This replaces the
    old raw engagement-decay sort, which zeroed near-zero-engagement platforms
    like Instagram regardless of how on-culture the post was. Engagement is
    log-compressed against the 10M cap so one viral post cannot dwarf the
    cultural signals.
    """
    fit = ("0.25 * " + topic_fit + " + ") if topic_fit else ""
    eng_w, reg_w, slang_w = (
        ("0.20", "0.15", "0.12")
        if topic_fit
        else ("0.30", "0.20", "0.15")
    )
    return (
        "("
        + fit
        + eng_w
        + " * (LN(GREATEST(0, LEAST(IFNULL(engagement_total, 0), 10000000)) + 1) / LN(10000001)) + "
        + reg_w
        + " * COALESCE(regional_score, 0) + "
        + slang_w
        + " * COALESCE(slang_score, 0) + "
        "0.10 * (1.0 / (1.0 + LN(GREATEST(1, TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), "
        "COALESCE(published_at, collected_at), HOUR)) + 1)))"
        ") * (CASE LOWER(IFNULL(platform, '')) WHEN 'web' THEN 0.3 WHEN 'social' THEN 0.3 "
        "WHEN 'aggregate' THEN 0.3 WHEN 'news' THEN 0.1 ELSE 1.0 END)"
    )


# TEV2 stores topic_groups alphabetically sorted, so array offset is not
# dominance. Focus is the honest proxy available read-side: a post tagged with
# one topic is wholly about it (fit 1.0); each extra co-tag dilutes how much the
# post is really about any single topic (two -> 0.7, three or more -> 0.4). This
# down-weights a diffuse multi-tag post (an amapiano clip also tagged politics)
# in every topic wall it would otherwise crash, without dropping a genuinely
# cross-cutting post outright.
_TOPIC_FIT_SQL = (
    "(CASE WHEN ARRAY_LENGTH(topic_groups) <= 1 THEN 1.0 "
    "WHEN ARRAY_LENGTH(topic_groups) = 2 THEN 0.7 ELSE 0.4 END)"
)


def fetch_voice_pools(topics: list, market: str, *, per_market: bool = False) -> dict:
    """Relevance-ranked voice posts per topic, one batched query.

    Each topic keeps its top 12 posts by a composite relevance score (topic
    fit, platform-normalized engagement, regional and slang signals,
    recency, platform credibility), not raw engagement, so a relevant
    voice is not buried under a high-engagement aggregator row and a
    near-zero-engagement Instagram post still surfaces on its cultural signal.
    Duplicate text collapses to its highest-relevance instance before the cut.
    Desk requests apply both the deduplication and top-12 limit per market;
    the default keeps a combined pool for the ALL Topic Profile.
    Non-voice platforms are excluded in SQL so an aggregator never reaches the
    wall. topic_groups and url ride along for the cross-tag chip and the
    "view original" link."""
    if not topics:
        return {}
    partition = "market, topic" if per_market else "topic"
    order = (
        "rel DESC, platform ASC, id ASC, url ASC, "
        "TO_JSON_STRING(STRUCT(title, text, market, author_handle, topic_groups, "
        "engagement, published_at, collected_at, slang_terms)) ASC"
    )
    sql = (
        "SELECT topic, id, title, text, platform, market, author_handle, url, "
        "topic_groups, engagement, published_at, collected_at, slang_terms FROM ("
        f"SELECT *, ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {order}) AS rn FROM ("
        "SELECT * EXCEPT(textkey, dup_rn) FROM ("
        f"SELECT *, ROW_NUMBER() OVER (PARTITION BY {partition}, textkey ORDER BY {order}) AS dup_rn FROM ("
        "SELECT tg AS topic, id, title, text, platform, market, author_handle, url, "
        "topic_groups, LEAST(IFNULL(engagement_total, 0), 10000000) AS engagement, "
        "published_at, collected_at, slang_terms, "
        + _relevance_sql(_TOPIC_FIT_SQL)
        + " AS rel, "
        "SUBSTR(REGEXP_REPLACE(LOWER(IFNULL(text, title)), r'[^a-z0-9]', ''), 1, 120) AS textkey "
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market) "
        "AND tg IN UNNEST(@topics)" + _NON_VOICE_SQL + ")"
        ") WHERE dup_rn = 1)"
        ") WHERE rn <= 12 "
        + ("ORDER BY topic, market, rn" if per_market else "ORDER BY topic, rn")
    )
    rows = _run_query(
        sql, [("market", "STRING", market), ("topics", "STRING", sorted(set(topics)))]
    )
    pools: dict = {}
    for r in rows:
        topic = r.get("topic")
        if not topic:
            continue
        pools.setdefault(topic, []).append(
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "text": r.get("text"),
                "platform": r.get("platform"),
                "market": r.get("market"),
                "handle": r.get("author_handle"),
                "url": r.get("url"),
                "topic_groups": list(r.get("topic_groups") or []),
                "engagement": r.get("engagement"),
                "published_at": r.get("published_at"),
                "collected_at": r.get("collected_at"),
                "slang_terms": r.get("slang_terms"),
            }
        )
    return pools


def fetch_bridge_rows(market: str) -> list:
    """Posts by creators active in two or more topic domains, last 14 days.

    One row per (handle, market, post) with the post's full topic list, voice
    platforms only. The SQL keeps only handles whose topics span at least two
    domain prefixes (music_, sports_, ...); the pairing and the exclusivity
    rule run in Python where the topic sets are easy to compare."""
    sql = (
        "WITH per AS ("
        "SELECT author_handle, market, id, ANY_VALUE(platform) AS platform, "
        "MAX(LEAST(IFNULL(engagement_total, 0), 10000000)) AS engagement, "
        "ARRAY_AGG(DISTINCT tg) AS topics "
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY) "
        "AND (@market = 'all' OR market = @market) "
        "AND author_handle IS NOT NULL AND TRIM(author_handle) != ''"
        + _NON_VOICE_SQL
        + " GROUP BY author_handle, market, id), "
        "crossing AS (SELECT author_handle, market FROM per, UNNEST(topics) AS t "
        "GROUP BY author_handle, market "
        "HAVING COUNT(DISTINCT SPLIT(t, '_')[OFFSET(0)]) >= 2) "
        "SELECT p.author_handle AS handle, p.market, p.id, p.platform, "
        "p.engagement, p.topics "
        "FROM per AS p JOIN crossing USING (author_handle, market)"
    )
    return _run_query(sql, [("market", "STRING", market)])


def fetch_lexicon_rows(market: str) -> list:
    """Slang-term frequency per (term, market) over the 30-day archive."""
    mk = (market or "").strip().lower()
    return fetch_lexicon_rows_for_markets([mk] if mk else [])


def fetch_lexicon_rows_for_markets(markets: list) -> list:
    """Batch lexicon fetch across markets in one BQ round-trip."""
    mks = [m.strip().lower() for m in (markets or []) if m]
    if not mks:
        return []
    sql = (
        "SELECT term, market, COUNT(*) AS n, "
        "COUNTIF(collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)) AS n7, "
        "COUNTIF(collected_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY) "
        "AND collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)) AS n_prev "
        "FROM " + _table("enriched_content") + ", "
        "UNNEST(SPLIT(IFNULL(slang_terms, ''), ',')) AS raw_term, "
        "UNNEST([TRIM(LOWER(raw_term))]) AS term "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market IN UNNEST(@markets) "
        "AND TRIM(raw_term) != '' "
        "GROUP BY term, market"
    )
    return _run_query(sql, [("markets", "STRING", mks)])


def _run_search(term: str, market: str, tokens: list, op: str) -> dict:
    """One search pass over enriched_content with the given token match."""
    table = _table("enriched_content")
    match_clause, term_params = _match_clause(tokens, op)
    # Geo-collision exclusion runs inside the WHERE for every served number:
    # counts, splits, creators, and the evidence pool all skip rows the
    # query's blocklist marks as foreign.
    geo_clause, geo_params = _geo_exclusion(term)
    filtered_where = _SEARCH_BASE_WHERE + "AND " + match_clause + geo_clause
    filtered_params = [("market", "STRING", market)] + term_params + geo_params

    agg_sql = (
        "SELECT DATE(collected_at) AS day, platform, market, COUNT(*) AS n "
        "FROM " + table + " WHERE " + filtered_where + " "
        "GROUP BY day, platform, market"
    )
    agg_rows = _run_query(agg_sql, filtered_params)
    daily = Counter()
    platform_split = Counter()
    market_split = Counter()
    total = 0
    for r in agg_rows:
        n = int(r.get("n") or 0)
        total += n
        if r.get("day") is not None:
            daily[r["day"]] += n
        if r.get("platform"):
            platform_split[r["platform"]] += n
        if r.get("market"):
            market_split[r["market"]] += n

    # Creators are voice-only: aggregator and feed platforms never produce a
    # "who is driving it" entry, and the geo strip applies here too.
    creators_sql = (
        "SELECT author_handle, ANY_VALUE(platform) AS platform, COUNT(*) AS n "
        "FROM " + table + " WHERE " + filtered_where + _NON_VOICE_SQL + " "
        "AND author_handle IS NOT NULL AND TRIM(author_handle) != '' "
        # Rank the voices by the reach they command, not raw post count: a
        # prolific low-reach commenter is not a "top voice", a high-reach creator
        # is. Count stays as the tiebreak and the displayed activity figure.
        "GROUP BY author_handle ORDER BY SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) DESC, n DESC LIMIT 12"
    )
    creator_rows = _run_query(creators_sql, filtered_params)
    creators = [
        (r.get("author_handle"), r.get("platform"), int(r.get("n") or 0))
        for r in creator_rows
    ]

    # Pull a wide pool (150) because cleaning (URL strip, foreign drop, near-dup
    # collapse) thins it heavily on viral topics where the same post reposts; the
    # caller cleans then keeps the top survivors, so a rich topic clears the
    # thin-signal floor instead of falsely reading thin on a 40-row pool.
    items_sql = (
        "SELECT id, title, text, platform, market, author_handle, url, "
        "LEAST(IFNULL(engagement_total, 0), 10000000) AS engagement, published_at, collected_at, slang_terms, "
        "hashtags, genz_score "
        "FROM " + table + " WHERE " + filtered_where + " "
        "ORDER BY " + _relevance_sql() + " DESC LIMIT 150"
    )
    item_rows = _run_query(items_sql, filtered_params)
    # Geo-collision strip: a query like "sapa" pulls Vietnam tourism and "ankara"
    # the Turkish capital. The keyword layer needs the query, so it runs here on
    # the raw rows before they reach the voice wall and the synthesis evidence.
    geo_topic = _QUERY_GEO_TOPIC.get(normalize_query(term))
    items = []
    for r in item_rows:
        haystack = " ".join([str(r.get("title") or ""), str(r.get("text") or "")])
        if geo_topic and geo.text_matches_geo_blocklist(haystack, geo_topic):
            continue
        items.append(
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "text": r.get("text"),
                "platform": r.get("platform"),
                "market": r.get("market"),
                "handle": r.get("author_handle"),
                "url": r.get("url"),
                "engagement": r.get("engagement"),
                "published_at": r.get("published_at"),
                "collected_at": r.get("collected_at"),
                "slang_terms": r.get("slang_terms"),
                "hashtags": r.get("hashtags"),
                "genz_score": r.get("genz_score"),
            }
        )

    return {
        "total_matches": total,
        "daily_counts": dict(daily),
        "platform_split": platform_split.most_common(),
        "market_split": market_split.most_common(),
        "creators": creators,
        "items": items,
    }


def search_content(term: str, market: str) -> dict:
    """Parameterized search over enriched_content, last 30 days.

    A multi-word query matches rows containing ALL meaningful tokens in any
    order, never the literal phrase. When that strict pass reads thin, one
    broadened retry ORs the two longest tokens and the payload is marked
    "broad" so the client can caption it. Returns raw aggregates plus the
    top-engagement evidence pool; cleaning happens afterwards in Python via
    clean_items."""
    tokens = query_tokens(term)
    out = _run_search(term, market, tokens, "AND")
    out["broad"] = False
    if len(tokens) >= 2 and len(clean_items(out["items"])) < _BROAD_RETRY_FLOOR:
        widest = sorted(tokens, key=len, reverse=True)[:2]
        broad = _run_search(term, market, widest, "OR")
        broad["broad"] = True
        return broad
    return out


# ---- Creator profile + voices (the influence layer) ----------------------
# One creator's 30-day footprint, and the reach-ranked board of voices per
# market. Reach is the influence signal, never raw post count. Voice platforms
# only, so an aggregator feed never inflates a creator's reach.

_HANDLE_MATCH_SQL = "LOWER(TRIM(REPLACE(author_handle, '@', ''))) = @handle"


def handle_norm(handle: str) -> str:
    """Normalize a creator handle for matching: strip @, lower, trim."""
    return (handle or "").strip().lstrip("@").strip().lower()


def fetch_creator_overview(handle: str) -> dict | None:
    """One creator's 30-day footprint, grouped by market.

    Aggregates the handle's own enriched_content rows: post count, the reach
    (summed engagement) it commands, its peak post, the platforms and markets it
    shows up in, and the bag of topics it drives. Returns None when the handle
    has no rows, or reads as a news outlet or anon id rather than a person."""
    h = handle_norm(handle)
    if not h:
        return None
    sql = (
        "SELECT market, ANY_VALUE(platform) AS platform, COUNT(*) AS posts, "
        "SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) AS reach, "
        "MAX(LEAST(IFNULL(engagement_total, 0), 10000000)) AS top_eng, "
        "ARRAY_CONCAT_AGG(IFNULL(topic_groups, [])) AS topic_bag, "
        "ARRAY_AGG(DISTINCT platform IGNORE NULLS) AS platforms "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND " + _HANDLE_MATCH_SQL + _NON_VOICE_SQL + " "
        "GROUP BY market"
    )
    rows = _run_query(sql, [("handle", "STRING", h)])
    if not rows:
        return None
    # Home market is where the handle commands the most reach.
    rows.sort(key=lambda r: int(r.get("reach") or 0), reverse=True)
    home = rows[0]
    platform = home.get("platform")
    if not is_real_handle(h, platform) or h in _BOT_AUTHOR_HANDLES:
        return None
    posts = sum(int(r.get("posts") or 0) for r in rows)
    reach = sum(int(r.get("reach") or 0) for r in rows)
    top_eng = max((int(r.get("top_eng") or 0) for r in rows), default=0)
    markets = [r.get("market") for r in rows if r.get("market")]
    platform_set: list = []
    for r in rows:
        for p in r.get("platforms") or []:
            lab = _platform_label(p)
            if lab not in platform_set:
                platform_set.append(lab)
    bag: list = []
    for r in rows:
        bag.extend([t for t in (r.get("topic_bag") or []) if t])
    topic_keys = [t for t, _ in Counter(bag).most_common()]
    return {
        "handle": h,
        "platform": _platform_label(platform),
        "market": home.get("market"),
        "markets": markets,
        "posts": posts,
        "reach": reach,
        "top_engagement": top_eng,
        "platforms": platform_set,
        "topic_keys": topic_keys,
    }


def fetch_creator_reach_rank(handle: str, market: str) -> tuple[int | None, int]:
    """Rank one handle by reach inside its home market without scanning every creator."""
    h = handle_norm(handle)
    mk = (market or "").strip().lower()
    if not h or mk not in MARKET_LABELS:
        return None, 0
    sql = (
        "WITH home AS ("
        "SELECT author_handle AS h, market, "
        "ROW_NUMBER() OVER (PARTITION BY author_handle ORDER BY COUNT(*) DESC) AS mrank "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND author_handle IS NOT NULL AND TRIM(author_handle) != ''"
        + _NON_VOICE_SQL
        + " "
        "GROUP BY author_handle, market), "
        "base AS ("
        "SELECT author_handle AS handle, "
        "SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) AS reach "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market = @market "
        "AND author_handle IS NOT NULL AND TRIM(author_handle) != ''"
        + _NON_VOICE_SQL
        + " "
        "AND author_handle IN (SELECT h FROM home WHERE market = @market AND mrank = 1) "
        "GROUP BY author_handle HAVING COUNT(*) >= 2), "
        "ranked AS ("
        "SELECT LOWER(TRIM(REPLACE(handle, '@', ''))) AS handle_norm, "
        "ROW_NUMBER() OVER (ORDER BY reach DESC) AS rank, "
        "COUNT(*) OVER () AS total "
        "FROM base"
        ") "
        "SELECT rank, total FROM ranked WHERE handle_norm = @handle LIMIT 1"
    )
    rows = _run_query(
        sql,
        [
            ("market", "STRING", mk),
            ("handle", "STRING", h),
        ],
    )
    if not rows:
        return None, 0
    row = rows[0]
    return int(row.get("rank") or 0) or None, int(row.get("total") or 0)


def fetch_reach_ranked_handles(market: str, limit: int = 100000) -> list:
    """Every active named handle in a market, ranked by reach.

    The same population and order the voices board uses, but effectively uncapped
    (the ceiling sits far above the ~6.5k handles that clear the mention floor in
    any market), so a creator profile can show an honest "rank N of M" inside its
    market. A 500-row cap here understated the denominator ~13x and left every
    creator past it unranked."""
    mk = (market or "").strip().lower()
    if mk not in MARKET_LABELS:
        return []
    # Same home-market gate as fetch_top_authors so the rank denominator counts
    # the same population the board shows: a creator's "rank N of M" stays
    # consistent with the region-gated voices board.
    sql = (
        "WITH home AS ("
        "SELECT author_handle AS h, market, "
        "ROW_NUMBER() OVER (PARTITION BY author_handle ORDER BY COUNT(*) DESC) AS mrank "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND author_handle IS NOT NULL AND TRIM(author_handle) != ''"
        + _NON_VOICE_SQL
        + " "
        "GROUP BY author_handle, market) "
        "SELECT author_handle AS handle, ANY_VALUE(platform) AS platform, "
        "COUNT(*) AS mentions, SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) AS reach "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market = @market "
        "AND author_handle IS NOT NULL AND TRIM(author_handle) != ''"
        + _NON_VOICE_SQL
        + " "
        "AND author_handle IN (SELECT h FROM home WHERE market = @market AND mrank = 1) "
        "GROUP BY author_handle HAVING mentions >= 2 ORDER BY reach DESC LIMIT @lim"
    )
    rows = _run_query(sql, [("market", "STRING", mk), ("lim", "INT64", limit)])
    out = []
    for r in rows:
        handle = r.get("handle")
        if not is_real_handle(handle, r.get("platform")):
            continue
        if handle_norm(handle) in _BOT_AUTHOR_HANDLES:
            continue
        out.append({"handle": handle_norm(handle), "reach": int(r.get("reach") or 0)})
    return out


def fetch_creator_reach_series(handle: str, days: int = 30) -> list:
    """Per-day reach (summed engagement) for one handle, zero-filled to 30d."""
    h = handle_norm(handle)
    if not h:
        return zero_filled_series({}, days, value_key="reach")
    sql = (
        "SELECT DATE(collected_at) AS day, "
        "SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) AS reach "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND " + _HANDLE_MATCH_SQL + _NON_VOICE_SQL + " "
        "GROUP BY day"
    )
    rows = _run_query(sql, [("handle", "STRING", h)])
    daily = {
        r["day"]: int(r.get("reach") or 0) for r in rows if r.get("day") is not None
    }
    return zero_filled_series(daily, days, value_key="reach")


def fetch_creator_posts(handle: str, limit: int = 60) -> list:
    """One handle's posts over 30 days, newest first, voice platforms only."""
    h = handle_norm(handle)
    if not h:
        return []
    sql = (
        "SELECT id, title, text, platform, market, author_handle, url, "
        "LEAST(IFNULL(engagement_total, 0), 10000000) AS engagement, published_at, collected_at, "
        "slang_terms, hashtags, topic_groups "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND " + _HANDLE_MATCH_SQL + _NON_VOICE_SQL + " "
        # id is the final tiebreak so the LIMIT picks the SAME rows every run:
        # many of a creator's posts share one timestamp, so a date-only order
        # let BigQuery return a different slice (and a different top post) per call.
        "ORDER BY COALESCE(published_at, collected_at) DESC, id DESC LIMIT @lim"
    )
    rows = _run_query(sql, [("handle", "STRING", h), ("lim", "INT64", max(limit, 1))])
    return [
        {
            "id": r.get("id"),
            "title": r.get("title"),
            "text": r.get("text"),
            "platform": r.get("platform"),
            "market": r.get("market"),
            "handle": r.get("author_handle"),
            "url": r.get("url"),
            "engagement": r.get("engagement"),
            "published_at": r.get("published_at"),
            "collected_at": r.get("collected_at"),
            "slang_terms": r.get("slang_terms"),
            "hashtags": r.get("hashtags"),
            "topic_groups": r.get("topic_groups"),
        }
        for r in rows
    ]


def fetch_voices_series(handles: list, market: str, days: int = 30) -> dict:
    """Per-day reach for a set of handles, one batched query.

    Feeds the reach sparkline on every voices-board row without a query per
    creator. Returns {normalized_handle: [reach, ...]} zero-filled to 30d."""
    norm = sorted({handle_norm(h) for h in (handles or []) if handle_norm(h)})
    if not norm:
        return {}
    sql = (
        "SELECT LOWER(TRIM(REPLACE(author_handle, '@', ''))) AS handle, "
        "DATE(collected_at) AS day, "
        "SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) AS reach "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market) "
        "AND LOWER(TRIM(REPLACE(author_handle, '@', ''))) IN UNNEST(@handles)"
        + _NON_VOICE_SQL
        + " "
        "GROUP BY handle, day"
    )
    rows = _run_query(sql, [("market", "STRING", market), ("handles", "STRING", norm)])
    by_handle: dict = {}
    for r in rows:
        h = r.get("handle")
        if not h or r.get("day") is None:
            continue
        by_handle.setdefault(h, {})[r["day"]] = int(r.get("reach") or 0)
    return {
        h: [pt["reach"] for pt in zero_filled_series(daily, days, value_key="reach")]
        for h, daily in by_handle.items()
    }


# ---- Topic story aggregates ----------------------------------------------
# Per-topic reach, observed-volume momentum, and the share-of-voice series.
# Share of voice is a topic's post count over the market's total, per single
# market, never blended; momentum is observed volume, never a forecast.


def fetch_topic_daily(topic_key: str, market: str) -> list:
    """Per-day deduped mention count and reach for one topic, 30-day window.

    Voice-only (estimate and aggregator platforms excluded) and deduped so the
    count is unique conversation, not reposts. Numerator of the topic's share of
    voice; fetch_market_daily is the matching denominator and applies the same
    voice filter and dedup so the percentage stays consistent."""
    key = (topic_key or "").strip()
    if not key:
        return []
    sql = (
        "SELECT DATE(collected_at) AS day, COUNT(DISTINCT "
        + _DEDUP_KEY_SQL
        + ") AS n, "
        "SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) AS reach "
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market) AND tg = @topic"
        + _NON_VOICE_SQL
        + " "
        "GROUP BY day"
    )
    return _run_query(sql, [("market", "STRING", market), ("topic", "STRING", key)])


def fetch_market_daily(market: str) -> list:
    """Per-day deduped voice-post count for a market, the share-of-voice
    denominator. Matches fetch_topic_daily's voice filter and dedup."""
    sql = (
        "SELECT DATE(collected_at) AS day, COUNT(DISTINCT " + _DEDUP_KEY_SQL + ") AS n "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market)" + _NON_VOICE_SQL + " "
        "GROUP BY day"
    )
    return _run_query(sql, [("market", "STRING", market)])


def fetch_topics_reach(market: str) -> dict:
    """Per-(market, topic) reach (winsorized summed engagement) and 30-day mention
    count, one batched query. Keyed by (market, topic_groups key) so the desk
    attaches each market's OWN reach and mentions to every topic row: grouping by
    topic alone blended ZA+NG+KE reach onto every market row of a shared topic and
    double-counted it in the market tiles. The mention count here is the real
    30-day enriched_content COUNT the desk re-bases onto (the engine item_count is
    a single scored-day scalar)."""
    sql = (
        "SELECT market, tg AS topic, COUNT(DISTINCT "
        + _DEDUP_KEY_SQL
        + ") AS mentions, "
        "SUM(LEAST(IFNULL(engagement_total, 0), 10000000)) AS reach "
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market)" + _NON_VOICE_SQL + " "
        "GROUP BY market, tg"
    )
    rows = _run_query(sql, [("market", "STRING", market)])
    return {
        (r["market"], r["topic"]): {
            "reach": int(r.get("reach") or 0),
            "mentions": int(r.get("mentions") or 0),
        }
        for r in rows
        if r.get("topic") and r.get("market")
    }


def _market_channel_totals(market: str) -> dict:
    """Per-platform deduped voice-post count for a market, 30-day window. The
    per-channel share-of-voice denominator, keyed by raw platform string."""
    mk = (market or "all").strip().lower()
    now = time.monotonic()
    cached = _channel_totals_cache.get(mk)
    if cached is not None and now - cached[0] < _CHANNEL_TOTALS_TTL_SECONDS:
        return cached[1]
    sql = (
        "SELECT platform, COUNT(DISTINCT " + _DEDUP_KEY_SQL + ") AS n "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market)" + _NON_VOICE_SQL + " "
        "GROUP BY platform"
    )
    rows = _run_query(sql, [("market", "STRING", mk)])
    totals = {r["platform"]: int(r.get("n") or 0) for r in rows if r.get("platform")}
    _channel_totals_cache[mk] = (now, totals)
    return totals


def _channel_sov(topic_by_platform: dict, market_totals: dict) -> list:
    """Combine per-platform topic counts with the market totals into a per-channel
    share-of-voice list: each platform's share of its own voice that this topic
    holds, plus the raw topic count so the client can also show the channel mix."""
    out = []
    for platform, tn in topic_by_platform.items():
        if not platform:
            continue
        mn = market_totals.get(platform, 0)
        out.append(
            {
                "platform": _platform_label(platform),
                "n": int(tn),
                "sov_pct": round(100.0 * int(tn) / mn, 2) if mn else 0.0,
            }
        )
    out.sort(key=lambda d: d["n"], reverse=True)
    return out


def fetch_topic_channel_sov(topic_key: str, market: str) -> list:
    """Per-platform share of voice for one curated topic, voice-only and deduped.

    Each platform's entry is the topic's deduped voice count on that platform over
    that platform's total market voice, so a strategist sees which channels the
    topic owns. Powers the Compare per-channel toggle and the topic page."""
    key = (topic_key or "").strip()
    if not key:
        return []
    topic_sql = (
        "SELECT platform, COUNT(DISTINCT " + _DEDUP_KEY_SQL + ") AS n "
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market) AND tg = @topic"
        + _NON_VOICE_SQL
        + " "
        "GROUP BY platform"
    )
    t_rows = _run_query(
        topic_sql, [("market", "STRING", market), ("topic", "STRING", key)]
    )
    topic_by_platform = {
        r["platform"]: int(r.get("n") or 0) for r in t_rows if r.get("platform")
    }
    return _channel_sov(topic_by_platform, _market_channel_totals(market))


def fetch_topic_tone_distribution(topic_key: str, market: str) -> dict:
    """Media-tone mix for a topic: how many of its toned posts read positive,
    negative or neutral on the 0..1 GDELT scale (positive > 0.575, negative <
    0.425, matching the pill's signed +/-0.15 bands). The tone comes from GDELT,
    which mostly tags news, web and youtube rows, so this is a media-tone mix,
    not social sentiment. n is the toned-post count behind it."""
    key = (topic_key or "").strip()
    empty = {"positive": 0, "negative": 0, "neutral": 0, "n": 0}
    if not key:
        return empty
    sql = (
        "SELECT COUNTIF(tone_avg > 0.575) AS positive, "
        "COUNTIF(tone_avg < 0.425) AS negative, "
        "COUNTIF(tone_avg >= 0.425 AND tone_avg <= 0.575) AS neutral, "
        "COUNT(*) AS n "
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market) AND tg = @topic "
        "AND IFNULL(v2tone, '') != '' AND tone_avg IS NOT NULL"
    )
    rows = _run_query(sql, [("market", "STRING", market), ("topic", "STRING", key)])
    if not rows:
        return empty
    r = rows[0]
    return {
        "positive": int(r.get("positive") or 0),
        "negative": int(r.get("negative") or 0),
        "neutral": int(r.get("neutral") or 0),
        "n": int(r.get("n") or 0),
    }


def fetch_listen(term: str, market: str) -> dict:
    """Ad-hoc listening signals for a free-text campaign or topic term.

    Per-channel share of voice (voice-only, deduped, matched on the search
    tokens) and a term-level media tone averaged from the matched posts that
    carry GDELT tone, recentered onto the signed scale. No model synthesis: this
    powers the Campaign Listening view, which is listening, not a brief."""
    tokens = query_tokens(term)
    if not tokens:
        return {
            "sov_by_channel": [],
            "media_tone": None,
            "tone_n": 0,
            "total_matches": 0,
        }
    match_clause, params = _match_clause(tokens, "OR")
    geo_clause, geo_params = _geo_exclusion(term)
    where = _SEARCH_BASE_WHERE + "AND " + match_clause + geo_clause + _NON_VOICE_SQL
    # Share of voice is voice-only; media tone is news-derived, so the tone read
    # spans all matched rows (news and web included) to stay consistent with the
    # topic-page media tone, which is also news-derived.
    where_all = _SEARCH_BASE_WHERE + "AND " + match_clause + geo_clause
    qp = [("market", "STRING", market)] + params + geo_params

    chan_sql = (
        "SELECT platform, COUNT(DISTINCT " + _DEDUP_KEY_SQL + ") AS n "
        "FROM " + _table("enriched_content") + " WHERE " + where + " GROUP BY platform"
    )
    chan_rows = _run_query(chan_sql, qp)
    term_by_platform = {
        r["platform"]: int(r.get("n") or 0) for r in chan_rows if r.get("platform")
    }
    sov = _channel_sov(term_by_platform, _market_channel_totals(market))

    tone_sql = (
        "SELECT AVG(tone_avg) AS avg_tone, COUNTIF(IFNULL(v2tone, '') != '') AS n "
        "FROM "
        + _table("enriched_content")
        + " WHERE "
        + where_all
        + " AND IFNULL(v2tone, '') != ''"
    )
    tone_rows = _run_query(tone_sql, qp)
    media_tone, tone_n = None, 0
    if tone_rows:
        n = int(tone_rows[0].get("n") or 0)
        avg = tone_rows[0].get("avg_tone")
        if n > 0 and isinstance(avg, (int, float)):
            media_tone = round(max(-1.0, min(1.0, (float(avg) - 0.5) * 2.0)), 3)
            tone_n = n
    return {
        "sov_by_channel": sov,
        "media_tone": media_tone,
        "tone_n": tone_n,
        "total_matches": sum(term_by_platform.values()),
    }


def fetch_lexicon_term_posts(term: str, market: str, limit: int = 60) -> list:
    """The posts a slang term is carried in, over the 30-day window.

    Matches the term as a delimited token inside the comma-list slang_terms the
    engine's own detector wrote, so a hit always traces to a real detection.
    Ranked by engagement decayed for age."""
    t = (term or "").strip().lower()
    if not t:
        return []
    pattern = r"(^|[,| ])" + re.escape(t) + r"([,| ]|$)"
    sql = (
        "SELECT id, title, text, platform, market, author_handle, "
        "LEAST(IFNULL(engagement_total, 0), 10000000) AS engagement, published_at, collected_at, slang_terms "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND (@market = 'all' OR market = @market) "
        "AND REGEXP_CONTAINS(LOWER(IFNULL(slang_terms, '')), @pat) "
        "ORDER BY LEAST(IFNULL(engagement_total, 0), 10000000) / POW(GREATEST(TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), "
        "COALESCE(published_at, collected_at), DAY) + 2, 1), 1.5) DESC LIMIT @lim"
    )
    rows = _run_query(
        sql,
        [
            ("market", "STRING", market),
            ("pat", "STRING", pattern),
            ("lim", "INT64", max(limit, 1)),
        ],
    )
    return [
        {
            "id": r.get("id"),
            "title": r.get("title"),
            "text": r.get("text"),
            "platform": r.get("platform"),
            "market": r.get("market"),
            "handle": r.get("author_handle"),
            "engagement": r.get("engagement"),
            "published_at": r.get("published_at"),
            "collected_at": r.get("collected_at"),
            "slang_terms": r.get("slang_terms"),
        }
        for r in rows
    ]


def _safe_query(sql: str, params=None) -> list:
    """Run a query that targets a Wave 2 surface that may not exist yet.

    The sentiment_lexicon_score column lands only after the Wave 2 migrations
    apply. Until then a SELECT against it raises a BigQuery NotFound /
    BadRequest; this swallows that to an empty result so the desk read degrades
    to "no data" (and the panel hides) instead of 500-ing. Any other error
    propagates so a real outage is still visible. The pan-African stories read
    no longer comes through here: an absent table must surface as absent, so
    fetch_pan_african_rows probes and raises instead of returning []."""
    try:
        return _run_query(sql, params)
    except Exception as exc:
        from google.api_core import exceptions as gexc

        if isinstance(exc, (gexc.NotFound, gexc.BadRequest)):
            return []
        raise


DYNAMIC_RUN_CONTRACT_VERSION = "open_intelligence_run_receipt_v1"


class DynamicDependencyUnavailable(RuntimeError):
    """The engine-owned dynamic read surface could not be read at all.

    Distinct from "no run qualified". The caller must report a dependency
    failure rather than an empty result, because a missing surface is not
    evidence that the engine found nothing.
    """


def fetch_dynamic_run_receipt(market: str) -> list:
    """The one engine run receipt that may release signals for this market.

    Deliberately not _safe_query wrapped. Every other Wave 2 read degrades a
    missing surface to an empty list, which is exactly the behaviour Open
    Discover must not have: a missing receipt table would then read as a
    completed run that found nothing.

    Ordering is the engine's, latest closed window first, then completion time,
    then run identity. The window must be closed against Johannesburg local
    time, so a run whose observation ends today can never release.
    """
    sql = (
        "SELECT run_id, signal_date, market_scope, observation_start, "
        "observation_end, observation_method, completed_at, row_set_digest, "
        "candidate_count, evidence_count, membership_count, lineage_count, "
        "analysis_count, prediction_count "
        "FROM " + _table("open_intelligence_run_receipts_v1") + " "
        "WHERE run_contract_version = @contract "
        "AND client_scope_id != 'qa_canary' "
        "AND status = 'completed' "
        "AND complete_partitions "
        "AND display_release_state = 'enabled' "
        "AND observation_start <= observation_end "
        "AND signal_date = observation_end "
        "AND observation_end < CURRENT_DATE('Africa/Johannesburg') "
        "AND (@market = 'all' OR @market IN UNNEST(market_scope)) "
        "AND (@market != 'all' OR ("
        "'za' IN UNNEST(market_scope) AND 'ng' IN UNNEST(market_scope) "
        "AND 'ke' IN UNNEST(market_scope))) "
        "ORDER BY observation_end DESC, completed_at DESC, run_id ASC "
        "LIMIT 1"
    )
    params = [
        ("contract", "STRING", DYNAMIC_RUN_CONTRACT_VERSION),
        ("market", "STRING", market),
    ]
    try:
        return _run_query(sql, params)
    except Exception as exc:
        from google.api_core import exceptions as gexc

        if isinstance(exc, (gexc.NotFound, gexc.BadRequest)):
            raise DynamicDependencyUnavailable(
                "the engine dynamic run receipt surface could not be read"
            ) from exc
        raise


def fetch_dynamic_run_row_counts(run_id: str) -> dict:
    """The actual per-family row counts for one run, computed in SQL.

    The receipt claims counts; this measures them, so a receipt whose rows
    were touched after completion cannot pass as intact. One query, one row,
    no client-side aggregation."""
    tables = {
        "candidate_count": "signal_candidates_v2",
        "evidence_count": "signal_evidence_v2",
        "membership_count": "signal_membership_v2",
        "lineage_count": "signal_lineage_v2",
        "analysis_count": "signal_analysis_v2",
        "prediction_count": "signal_predictions_v2",
    }
    selects = ", ".join(
        f"(SELECT COUNT(*) FROM {_table(table)} WHERE run_id = @run) AS {field}"
        for field, table in tables.items()
    )
    try:
        rows = _run_query(f"SELECT {selects}", [("run", "STRING", run_id)])
    except Exception as exc:
        from google.api_core import exceptions as gexc

        if isinstance(exc, (gexc.NotFound, gexc.BadRequest)):
            raise DynamicDependencyUnavailable(
                "the engine run tables could not be read"
            ) from exc
        raise
    return dict(rows[0]) if rows else {}


def fetch_dynamic_run_signals(run_id: str) -> list:
    """The admitted signals of one released run, from the engine-owned view."""
    sql = (
        "SELECT v.contract_version, v.run_id, v.signal_date, v.market, "
        "v.signal_id, v.signal_name, v.discovery_mode, v.evidence_state, "
        "v.why_now, v.possible_response, v.observation_start, "
        "v.observation_end, v.observation_method, v.receipts, "
        "v.velocity_score, v.novelty_score, v.breadth_score, "
        "v.independence_score, v.historical_similarity, v.geo_confidence, "
        "v.topic_tags, v.predicted_at, v.prediction_id, "
        "v.evidence_summary, v.ribbon_series "
        "FROM " + _table("v_desk_dynamic_signals_v2") + " AS v "
        "WHERE v.run_id = @run "
        "ORDER BY v.predicted_at DESC, v.prediction_id ASC"
    )
    try:
        return _run_query(sql, [("run", "STRING", run_id)])
    except Exception as exc:
        from google.api_core import exceptions as gexc

        if isinstance(exc, (gexc.NotFound, gexc.BadRequest)):
            raise DynamicDependencyUnavailable(
                "the engine dynamic signal view could not be read"
            ) from exc
        raise


# Locked Wave 2 tone bands on the [-1.0, 1.0] sentiment_lexicon_score, matching
# the desk's toneOf bands and the engine mailer's tone split so the same score
# reads the same way on every surface.
TONE_POS_FLOOR = 0.15
TONE_NEG_CEIL = -0.15


def fetch_tone_split(market: str) -> dict:
    """Positive / neutral / negative counts from the social sentiment lexicon.

    Bins enriched_content.sentiment_lexicon_score over the last 14 days into the
    three tone buckets by the locked bands. The column is NULL on every row until
    the engine's SENTIMENT_LEXICON_ENABLED flag flips and the Wave 2 migration
    applies, so the NOT NULL filter yields zero rows today and the bar hides. The
    query is _safe_query-wrapped so a pre-migration run (no such column) degrades
    to an empty split rather than erroring."""
    sql = (
        "SELECT "
        "COUNTIF(sentiment_lexicon_score >= @pos) AS positive, "
        "COUNTIF(sentiment_lexicon_score <= @neg) AS negative, "
        "COUNTIF(sentiment_lexicon_score > @neg AND sentiment_lexicon_score < @pos) AS neutral, "
        "COUNTIF(sentiment_lexicon_score IS NOT NULL) AS scored "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY) "
        "AND (@market = 'all' OR market = @market) "
        "AND sentiment_lexicon_score IS NOT NULL"
    )
    rows = _safe_query(
        sql,
        [
            ("market", "STRING", market),
            ("pos", "FLOAT64", TONE_POS_FLOOR),
            ("neg", "FLOAT64", TONE_NEG_CEIL),
        ],
    )
    if not rows:
        return {"positive": 0, "neutral": 0, "negative": 0, "scored": 0}
    r = rows[0]
    return {
        "positive": int(r.get("positive") or 0),
        "neutral": int(r.get("neutral") or 0),
        "negative": int(r.get("negative") or 0),
        "scored": int(r.get("scored") or 0),
    }


class PanAfricanSurfaceMissing(RuntimeError):
    """The pan_african_stories table is not in the dataset.

    Distinct from "no stories": the producer (the engine's pan-African stage)
    has never written here, so the desk must say the surface is absent rather
    than report a day with no cross-market story.
    """


class PanAfricanSurfaceUnavailable(RuntimeError):
    """The pan-African stories surface could not be read at all.

    retryable is False for a BadRequest, which is the query being wrong, and
    for a refused permission; neither fixes itself. A timeout, an outage or a
    rate limit is worth the next request's retry.
    """

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


PAN_AFRICAN_TABLE = "pan_african_stories"


def fetch_pan_african_rows(market: str) -> list:
    """The pan-African stories for the latest date in the pan_african_stories table.

    One row per cross-market story (story_id, story_label, markets, topic_keys,
    total_item_count, momentum_composite), strongest first. The desk passes
    market through; the stories are inherently cross-market, so a single-market
    desk keeps only the stories that include that market.

    The table is created by an engine migration and filled by the engine's
    pan-African stage, neither of which is part of the staging estate, so the
    surface is probed by its pinned name in INFORMATION_SCHEMA before any row is
    read. An absent table raises PanAfricanSurfaceMissing and no story query is
    issued; a probe or read that BigQuery refuses for any reason it reports
    (not found, bad query, permission denied, timeout, outage) raises
    PanAfricanSurfaceUnavailable, so the surface fails on its own and never
    takes the desk down. Anything that is not a BigQuery answer is a bug and
    propagates. Neither exception is an empty result, because an empty result
    means the stage ran and found nothing."""
    from google.api_core import exceptions as gexc

    probe = (
        "SELECT COUNT(*) AS present "
        "FROM " + _table("INFORMATION_SCHEMA.TABLES") + " "
        "WHERE table_name = @name"
    )
    try:
        probed = _run_query(probe, [("name", "STRING", PAN_AFRICAN_TABLE)])
    except gexc.GoogleAPICallError as exc:
        raise PanAfricanSurfaceUnavailable(
            "the pan-African stories surface could not be probed",
            retryable=not (
                isinstance(exc, gexc.BadRequest) or _permanent_forbidden(exc)
            ),
        ) from exc
    present = int((probed[0].get("present") or 0) if probed else 0)
    if present < 1:
        raise PanAfricanSurfaceMissing(
            "the pan-African stories table is not in the dataset"
        )
    sql = (
        "SELECT story_id, story_label, markets, topic_keys, "
        "total_item_count, momentum_composite, trend_date "
        "FROM " + _table(PAN_AFRICAN_TABLE) + " "
        "WHERE trend_date = ("
        "SELECT MAX(trend_date) FROM " + _table(PAN_AFRICAN_TABLE) + ") "
        "AND (@market = 'all' OR @market IN UNNEST(markets)) "
        "ORDER BY momentum_composite DESC, total_item_count DESC"
    )
    try:
        return _run_query(sql, [("market", "STRING", market)])
    except gexc.GoogleAPICallError as exc:
        raise PanAfricanSurfaceUnavailable(
            "the pan-African stories surface could not be read",
            retryable=not (
                isinstance(exc, gexc.BadRequest) or _permanent_forbidden(exc)
            ),
        ) from exc


# --- Console Research retrieval + artifact persist --------------------------------

THIN_FLOOR = 12

_MARKET_COUNTRY = {"za": "ZA", "ng": "NG", "ke": "KE"}


def latest_trend_date():
    sql = (
        "SELECT MAX(trend_date) AS d FROM " + _table("trend_scores") + " "
        "WHERE trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)"
    )
    rows = _run_query(sql)
    return rows[0].get("d") if rows else None


# ---- Research brief pipeline -----------------------------------------------


def fetch_topic_briefs(query_groups: list, market: str, trend_date=None) -> list:
    """trend_analysis + search_velocity for persona allowlisted topics."""
    mk = (market or "").strip().lower()
    return fetch_topic_briefs_for_markets(
        query_groups, [mk] if mk else [], trend_date=trend_date
    )


def fetch_topic_briefs_for_markets(
    query_groups: list, markets: list, trend_date=None
) -> list:
    """Batch brief fetch across markets in one BQ round-trip."""
    groups = [g for g in (query_groups or []) if g]
    mks = [m.strip().lower() for m in (markets or []) if m]
    if not groups or not mks:
        return []
    d = trend_date or latest_trend_date()
    if d is None:
        return []
    sql = (
        "SELECT s.market, s.query_group, s.trend_score, s.velocity_score, "
        "s.search_velocity_score, s.item_count, s.trend_date, "
        "a.headline, a.trend_synthesis, a.cultural_context, a.sentiment_summary "
        "FROM " + _table("trend_scores") + " s "
        "LEFT JOIN " + _table("trend_analysis") + " a "
        "ON a.market = s.market AND a.query_group = s.query_group "
        "AND a.trend_date = s.trend_date "
        "WHERE s.trend_date = @d "
        "AND s.trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND s.query_group IN UNNEST(@groups) "
        "AND s.market IN UNNEST(@markets) "
        "ORDER BY s.trend_score DESC"
    )
    return _run_query(
        sql,
        [
            ("d", "DATE", d),
            ("groups", "STRING", groups),
            ("markets", "STRING", mks),
        ],
    )


def fetch_market_topics(markets: list, per_market: int = 10, trend_date=None) -> list:
    """Top topics per market by trend_score, no persona/query-group filter.

    Feeds the behaviour scan step. One row per (market, query_group) with the
    engine's own clustering, the cultural read, and the item count. QUALIFY keeps
    the top N distinct topics per market in a single round-trip.
    """
    mks = [m.strip().lower() for m in (markets or []) if m]
    if not mks:
        return []
    d = trend_date or latest_trend_date()
    if d is None:
        return []
    per = max(1, min(int(per_market or 10), 25))
    sql = (
        "SELECT s.market, s.query_group, s.trend_score, s.velocity_score, "
        "s.item_count, s.trend_date, "
        "a.headline, a.trend_synthesis, a.cultural_context, a.sentiment_summary "
        "FROM " + _table("trend_scores") + " s "
        "LEFT JOIN " + _table("trend_analysis") + " a "
        "ON a.market = s.market AND a.query_group = s.query_group "
        "AND a.trend_date = s.trend_date "
        "WHERE s.trend_date = @d "
        "AND s.trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND s.market IN UNNEST(@markets) "
        "AND s.query_group IS NOT NULL AND TRIM(s.query_group) != '' "
        "QUALIFY ROW_NUMBER() OVER ("
        "PARTITION BY s.market ORDER BY s.trend_score DESC) <= @per "
        "ORDER BY s.market, s.trend_score DESC"
    )
    return _run_query(
        sql,
        [
            ("d", "DATE", d),
            ("markets", "STRING", mks),
            ("per", "INT64", per),
        ],
    )


def fetch_research_digest(trend_date=None) -> dict | None:
    d = trend_date or latest_trend_date()
    if d is None:
        return None
    sql = (
        "SELECT through_line, summary_text, seed_recommend, trend_date "
        "FROM " + _table("daily_summary") + " "
        "WHERE trend_date <= @d "
        "AND trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND summary_text IS NOT NULL AND TRIM(summary_text) != '' "
        "ORDER BY trend_date DESC, generated_at DESC LIMIT 1"
    )
    rows = _run_query(sql, [("d", "DATE", d)])
    if not rows:
        return None
    row = rows[0]
    return {
        "trend_date": row.get("trend_date"),
        "through_line": humanize_topic_refs(row.get("through_line") or ""),
        "summary_text": humanize_topic_refs(row.get("summary_text") or ""),
        "seed_recommend": humanize_topic_refs(row.get("seed_recommend") or ""),
    }


def fetch_research_posts(markets: list, query_groups: list, *, limit: int = 80) -> list:
    if not query_groups or not markets:
        return []
    sql = (
        "SELECT id, market, platform, text, title, author_handle, url, engagement_total, "
        "views, likes, comments, shares, "
        "published_at, topic_groups, slang_terms "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market IN UNNEST(@markets) "
        + _NON_VOICE_SQL
        + " AND EXISTS (SELECT 1 FROM UNNEST(topic_groups) AS tg "
        "WHERE tg IN UNNEST(@groups)) "
        "ORDER BY engagement_total DESC LIMIT @lim"
    )
    rows = _run_query(
        sql,
        [
            ("markets", "STRING", [m.lower() for m in markets]),
            ("groups", "STRING", list(query_groups)),
            ("lim", "INT64", limit),
        ],
    )
    return clean_items(rows)


# Voice proof pools for behaviour scan (42 phase R2/R2b). Comments rank in their own
# content_type family, never against posts on engagement_total alone.
_VOICE_COMMENT_TYPES = ("tiktok_comment", "instagram_post_comment", "threads_reply")


def _voice_kind(content_type: str | None) -> str:
    ct = (content_type or "").strip().lower()
    return "comment" if ct in _VOICE_COMMENT_TYPES else "post"


def fetch_voice_metrics_for_topics(
    markets: list, query_groups: list
) -> dict[tuple[str, str], dict]:
    """Aggregate post/comment counts and engagement per (market, topic_group)."""
    groups = [g for g in (query_groups or []) if g]
    mks = [m.strip().lower() for m in (markets or []) if m]
    if not groups or not mks:
        return {}
    comment_types = list(_VOICE_COMMENT_TYPES)
    sql = (
        "SELECT market, tg AS topic_group, "
        "COUNTIF(LOWER(IFNULL(content_type, '')) IN UNNEST(@comment_types)) AS comment_count, "
        "COUNTIF(LOWER(IFNULL(content_type, '')) NOT IN UNNEST(@comment_types)) AS post_count, "
        "SUM(IFNULL(engagement_total, 0)) AS engagement_total, "
        "COUNT(DISTINCT LOWER(IFNULL(platform, ''))) AS platform_count "
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market IN UNNEST(@markets) "
        "AND tg IN UNNEST(@groups) " + _NON_VOICE_SQL + " GROUP BY market, tg"
    )
    rows = _run_query(
        sql,
        [
            ("markets", "STRING", mks),
            ("groups", "STRING", groups),
            ("comment_types", "STRING", comment_types),
        ],
    )
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        mk = str(r.get("market") or "").lower()
        tg = str(r.get("topic_group") or "")
        if not mk or not tg:
            continue
        out[(mk, tg)] = {
            "post_count": int(r.get("post_count") or 0),
            "comment_count": int(r.get("comment_count") or 0),
            "engagement_total": int(r.get("engagement_total") or 0),
            "platform_count": int(r.get("platform_count") or 0),
        }
    return out


def fetch_posts_and_comments_for_topics(
    markets: list,
    query_groups: list,
    *,
    per_topic_posts: int = 3,
    per_topic_comments: int = 5,
) -> list:
    """Top posts and comments per (market, topic), ranked within voice_kind."""
    groups = [g for g in (query_groups or []) if g]
    mks = [m.strip().lower() for m in (markets or []) if m]
    if not groups or not mks:
        return []
    per_posts = max(1, min(int(per_topic_posts or 8), 12))
    per_comments = max(1, min(int(per_topic_comments or 5), 10))
    comment_types = list(_VOICE_COMMENT_TYPES)
    base_cols = (
        "id, market, platform, text, title, author_handle, url, engagement_total, "
        "views, likes, comments, shares, "
        "published_at, tg AS topic_group, topic_groups, slang_terms, content_type"
    )
    base_from = (
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market IN UNNEST(@markets) "
        "AND tg IN UNNEST(@groups) " + _NON_VOICE_SQL
    )
    sql = (
        "SELECT id, market, platform, text, title, author_handle, url, engagement_total, "
        "views, likes, comments, shares, "
        "published_at, topic_group, topic_groups, slang_terms, content_type, voice_kind FROM ("
        "SELECT "
        + base_cols
        + ", 'post' AS voice_kind "
        + base_from
        + " AND LOWER(IFNULL(content_type, '')) NOT IN UNNEST(@comment_types) "
        "QUALIFY ROW_NUMBER() OVER ("
        "PARTITION BY market, tg ORDER BY engagement_total DESC) <= @per_posts "
        "UNION ALL "
        "SELECT "
        + base_cols
        + ", 'comment' AS voice_kind "
        + base_from
        + " AND LOWER(IFNULL(content_type, '')) IN UNNEST(@comment_types) "
        "QUALIFY ROW_NUMBER() OVER ("
        "PARTITION BY market, tg ORDER BY engagement_total DESC) <= @per_comments"
        ")"
    )
    rows = _run_query(
        sql,
        [
            ("markets", "STRING", mks),
            ("groups", "STRING", groups),
            ("comment_types", "STRING", comment_types),
            ("per_posts", "INT64", per_posts),
            ("per_comments", "INT64", per_comments),
        ],
    )
    tagged = {r.get("id"): r.get("topic_group") for r in rows}
    cleaned = clean_items(rows)
    for c in cleaned:
        c["topic_group"] = tagged.get(c.get("id")) or c.get("topic_group")
        c["voice_kind"] = _voice_kind(c.get("content_type"))
        c["content_type"] = str(c.get("content_type") or "")
    return cleaned


_TIKTOK_ID_PATTERNS = (
    re.compile(r"/video/([0-9]+)"),
    re.compile(r"/v/([0-9]+)\.html"),
    re.compile(r"[?&]share_item_id=([0-9]+)"),
    re.compile(r"/photo/([0-9]+)"),
)


def _extract_tiktok_video_id(url: str) -> str:
    """Numeric aweme id from any known TikTok URL shape.

    Ensemble stores canonical /video/<id> URLs but share links come through as
    m.tiktok.com/v/<id>.html and query-string share_item_id=<id>, and photo
    posts use /photo/<id>. First pattern that matches wins.
    """
    if not url:
        return ""
    for pat in _TIKTOK_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return ""


def _post_parent_keys(post: dict) -> list[str]:
    """Keys that comment rows store in enriched_content.query_term for this post."""
    keys: list[str] = []
    url = str(post.get("url") or "").strip()
    plat = str(post.get("platform") or "").lower()
    qt = str(post.get("query_term") or "").strip()

    if plat == "tiktok":
        vid = _extract_tiktok_video_id(url)
        if vid:
            keys.append(vid)
    elif plat == "instagram":
        if qt.isdigit() and len(qt) >= 10:
            keys.append(qt)
    elif plat == "threads":
        if qt.isdigit():
            keys.append(qt)

    if url:
        keys.append(url)
        keys.append(url.rstrip("/"))
        if not url.endswith("/"):
            keys.append(url + "/")

    row_id = str(post.get("id") or "").strip()
    if row_id:
        keys.append(row_id)

    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _thread_comment_dict(row: dict) -> dict:
    return {
        "text": str(row.get("text") or ""),
        "handle": str(row.get("author_handle") or row.get("handle") or ""),
        "engagement": int(row.get("engagement_total") or row.get("engagement") or 0),
        "platform": str(row.get("platform") or ""),
        "published_at": row.get("published_at"),
        "content_type": str(row.get("content_type") or ""),
    }


def fetch_comment_threads_for_posts(
    posts: list,
    *,
    per_post: int = 10,
) -> dict[str, list[dict]]:
    """Map enriched_content post row id to up to per_post comment rows.

    Comment types link to parent posts via query_term (TikTok aweme id, IG media id,
    Threads pk, or parent URL where stored).
    """
    post_list = [p for p in (posts or []) if isinstance(p, dict)]
    if not post_list:
        return {}

    per = max(1, min(int(per_post or 10), 10))
    comment_types = list(_VOICE_COMMENT_TYPES)

    post_keys: dict[str, list[str]] = {}
    key_to_posts: dict[str, list[str]] = {}
    markets: set[str] = set()

    for post in post_list:
        pid = str(post.get("id") or "").strip()
        if not pid:
            continue
        mk = str(post.get("market") or "").lower()
        if mk:
            markets.add(mk)
        keys = _post_parent_keys(post)
        post_keys[pid] = keys
        for key in keys:
            key_to_posts.setdefault(key, []).append(pid)

    all_keys = list(key_to_posts.keys())
    if not all_keys or not markets:
        return {pid: [] for pid in post_keys}

    sql = (
        "SELECT id, market, platform, text, author_handle, url, engagement_total, "
        "published_at, content_type, query_term FROM ("
        "SELECT id, market, platform, text, author_handle, url, engagement_total, "
        "published_at, content_type, query_term "
        "FROM " + _table("enriched_content") + " "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market IN UNNEST(@markets) "
        "AND LOWER(IFNULL(content_type, '')) IN UNNEST(@comment_types) "
        "AND query_term IN UNNEST(@parent_keys) "
        + _NON_VOICE_SQL
        + " QUALIFY ROW_NUMBER() OVER ("
        "PARTITION BY market, query_term ORDER BY engagement_total DESC) <= @per_post"
        ")"
    )
    rows = _run_query(
        sql,
        [
            ("markets", "STRING", sorted(markets)),
            ("comment_types", "STRING", comment_types),
            ("parent_keys", "STRING", all_keys),
            ("per_post", "INT64", per),
        ],
    )
    cleaned = clean_items(rows)

    result: dict[str, list[dict]] = {pid: [] for pid in post_keys}
    seen_per_post: dict[str, set[str]] = {pid: set() for pid in post_keys}

    for row in cleaned:
        qt = str(row.get("query_term") or "").strip()
        if not qt:
            continue
        cdict = _thread_comment_dict(row)
        cid = str(row.get("id") or cdict["text"][:80])
        for pid in key_to_posts.get(qt) or []:
            if len(result[pid]) >= per:
                continue
            if cid in seen_per_post[pid]:
                continue
            seen_per_post[pid].add(cid)
            result[pid].append(cdict)

    return result


def fetch_posts_for_topics(
    markets: list, query_groups: list, *, per_topic: int = 3
) -> list:
    """Top posts per (market, topic) so every behaviour keeps its own examples.

    fetch_research_posts orders globally and starves low-engagement topics. This
    partitions by market and topic_group so each behaviour in the scan gets its
    own example posts with links. Returns rows tagged with topic_group.
    """
    groups = [g for g in (query_groups or []) if g]
    mks = [m.strip().lower() for m in (markets or []) if m]
    if not groups or not mks:
        return []
    per = max(1, min(int(per_topic or 8), 12))
    sql = (
        "SELECT id, market, platform, text, title, author_handle, url, engagement_total, "
        "views, likes, comments, shares, "
        "published_at, topic_group, topic_groups, slang_terms FROM ("
        "SELECT id, market, platform, text, title, author_handle, url, engagement_total, "
        "views, likes, comments, shares, "
        "published_at, tg AS topic_group, topic_groups, slang_terms "
        "FROM " + _table("enriched_content") + ", UNNEST(topic_groups) AS tg "
        "WHERE collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND market IN UNNEST(@markets) "
        "AND tg IN UNNEST(@groups) " + _NON_VOICE_SQL + " QUALIFY ROW_NUMBER() OVER ("
        "PARTITION BY market, tg ORDER BY engagement_total DESC) <= @per"
        ")"
    )
    rows = _run_query(
        sql,
        [
            ("markets", "STRING", mks),
            ("groups", "STRING", groups),
            ("per", "INT64", per),
        ],
    )
    tagged = {r.get("id"): r.get("topic_group") for r in rows}
    cleaned = clean_items(rows)
    for c in cleaned:
        c["topic_group"] = tagged.get(c.get("id")) or c.get("topic_group")
    return cleaned


def fetch_research_seeds(markets: list, trend_date=None) -> list:
    d = trend_date or latest_trend_date()
    if d is None:
        return []
    sql = (
        "SELECT trend_date, rank, behaviour, the_shift, evidence, why_hidden, "
        "timing, markets, brand_opportunity, signal_strength "
        "FROM " + _table("seed_insights") + " "
        "WHERE trend_date = @d ORDER BY rank"
    )
    rows = _run_query(sql, [("d", "DATE", d)])
    want = {m.lower() for m in markets or []}
    if not want:
        return rows
    out = []
    for r in rows:
        row_markets = {str(m).lower() for m in (r.get("markets") or [])}
        if row_markets & want:
            out.append(r)
    return out


def _filter_rising_rows(
    rows: list, anchor_tokens: list, *, per_market: int = 20
) -> list:
    anchors = [t.strip().lower() for t in (anchor_tokens or []) if t]
    out = []
    for r in rows:
        blob = " ".join(
            [
                str(r.get("query_group") or ""),
                topic_label(str(r.get("query_group") or "")),
            ]
        ).lower()
        if anchors and not any(a in blob for a in anchors):
            continue
        out.append(r)
    return out[:per_market]


def fetch_rising_search_terms(
    market: str, anchor_tokens: list, trend_date=None
) -> list:
    """Rows with search_velocity_score for the desk day, filtered in Python."""
    mk = (market or "").strip().lower()
    return fetch_rising_search_terms_for_markets(
        [mk] if mk else [], anchor_tokens, trend_date=trend_date
    )


def fetch_rising_search_terms_for_markets(
    markets: list, anchor_tokens: list, trend_date=None
) -> list:
    """Batch rising-term fetch across markets in one BQ round-trip."""
    mks = [m.strip().lower() for m in (markets or []) if m]
    if not mks:
        return []
    d = trend_date or latest_trend_date()
    if d is None:
        return []
    sql = (
        "SELECT market, query_group, search_velocity_score, trend_score, trend_date "
        "FROM " + _table("trend_scores") + " "
        "WHERE trend_date = @d "
        "AND trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND market IN UNNEST(@markets) "
        "AND IFNULL(search_velocity_score, 0) > 0 "
        "ORDER BY search_velocity_score DESC "
        "LIMIT 120"
    )
    rows = _run_query(sql, [("d", "DATE", d), ("markets", "STRING", mks)])
    cap = 20 * max(1, len(mks))
    return _filter_rising_rows(rows, anchor_tokens, per_market=cap)


def _research_index_key():
    return ("research", "_index")


def _research_artifacts_table() -> str:
    return os.environ.get("RESEARCH_ARTIFACTS_TABLE", "research_artifacts")


def _research_dataset() -> str:
    return os.environ.get("RESEARCH_BQ_DATASET", _dataset())


def _research_table_ref() -> str:
    return f"`{_project()}.{_research_dataset()}.{_research_artifacts_table()}`"


def _persist_research_artifact_gcs(artifact_id: str, row: dict) -> None:
    synth.cache_set(("research", "artifact", artifact_id), row)
    idx = synth.cache_get(_research_index_key()) or {"ids": []}
    ids = [i for i in (idx.get("ids") or []) if i != artifact_id]
    ids.insert(0, artifact_id)
    synth.cache_set(_research_index_key(), {"ids": ids[:100]})


def insert_research_artifact(artifact: dict) -> str:
    import uuid

    from src.api import investigation_scopes

    artifact_id = artifact.get("artifact_id") or ("ra_" + uuid.uuid4().hex[:16])
    artifact["artifact_id"] = artifact_id
    # The row binds itself to the server scope it was produced under; readers
    # refuse a row whose stored scope is not the one they serve.
    artifact["client_scope_id"] = investigation_scopes.default_client_scope_id()
    backend = os.environ.get("RESEARCH_PERSIST_BACKEND", "gcs").lower()
    if backend == "gcs":
        _persist_research_artifact_gcs(artifact_id, artifact)
        return artifact_id

    sql = (
        "INSERT INTO " + _research_table_ref() + " "
        "(artifact_id, persona_id, markets, trend_date, status, quality_level, "
        "product_frame, evidence_json, synthesis_json, markdown, degraded_reason, created_at) "
        "VALUES (@id, @persona, @markets, @trend_date, @status, @quality, "
        "@product_frame, @evidence, @synthesis, @markdown, @degraded, CURRENT_TIMESTAMP())"
    )
    try:
        _run_query(
            sql,
            [
                ("id", "STRING", artifact_id),
                ("persona", "STRING", artifact.get("persona_id") or ""),
                ("markets", "STRING", list(artifact.get("markets") or [])),
                ("trend_date", "DATE", artifact.get("trend_date")),
                ("status", "STRING", artifact.get("status") or "completed"),
                (
                    "quality",
                    "STRING",
                    (artifact.get("quality") or {}).get("level") or "",
                ),
                ("product_frame", "STRING", artifact.get("product_frame") or ""),
                (
                    "evidence",
                    "STRING",
                    json.dumps(artifact.get("evidence") or [], default=str),
                ),
                (
                    "synthesis",
                    "STRING",
                    json.dumps(artifact.get("synthesis") or {}, default=str),
                ),
                ("markdown", "STRING", artifact.get("markdown") or ""),
                ("degraded", "STRING", artifact.get("degraded_reason") or ""),
            ],
        )
    except Exception:
        _persist_research_artifact_gcs(artifact_id, artifact)
    else:
        _persist_research_artifact_gcs(artifact_id, artifact)
    return artifact_id


def save_research_artifact(artifact_id: str, row: dict) -> None:
    row = dict(row)
    row["artifact_id"] = artifact_id
    insert_research_artifact(row)


def get_research_artifact(artifact_id: str) -> dict | None:
    hit = synth.cache_get(("research", "artifact", artifact_id))
    if isinstance(hit, dict):
        return hit
    sql = (
        "SELECT artifact_id, persona_id, markets, trend_date, status, quality_level, "
        "product_frame, evidence_json, synthesis_json, markdown, degraded_reason, created_at "
        "FROM " + _research_table_ref() + " WHERE artifact_id = @id LIMIT 1"
    )
    try:
        rows = _run_query(sql, [("id", "STRING", artifact_id)])
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    evidence = row.get("evidence_json")
    synthesis = row.get("synthesis_json")
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except json.JSONDecodeError:
            evidence = []
    if isinstance(synthesis, str):
        try:
            synthesis = json.loads(synthesis)
        except json.JSONDecodeError:
            synthesis = {}
    return {
        "artifact_id": row.get("artifact_id"),
        "persona_id": row.get("persona_id"),
        "persona_label": row.get("persona_label"),
        "markets": list(row.get("markets") or []),
        "trend_date": str(row.get("trend_date") or ""),
        "status": row.get("status"),
        "quality": {"level": row.get("quality_level") or ""},
        "signal_quality": row.get("quality_level") or "",
        "product_frame": row.get("product_frame") or "",
        "evidence": evidence or [],
        "synthesis": synthesis or {},
        "doc_json": synthesis or {},
        "doc_markdown": row.get("markdown") or "",
        "markdown": row.get("markdown") or "",
        "evidence_graph": {"refs": evidence or []},
        "degraded_reason": row.get("degraded_reason") or "",
        "created_at": str(row.get("created_at") or ""),
    }


def delete_research_artifact(artifact_id: str) -> bool:
    sql = "DELETE FROM " + _research_table_ref() + " WHERE artifact_id = @id"
    try:
        _run_query(sql, [("id", "STRING", artifact_id)])
        return True
    except Exception:
        return False


def list_recent_research_artifacts(limit: int = 20) -> list:
    idx = synth.cache_get(_research_index_key()) or {"ids": []}
    out = []
    for aid in (idx.get("ids") or [])[:limit]:
        row = get_research_artifact(aid)
        if not row:
            continue
        out.append(
            {
                "artifact_id": aid,
                "client_scope_id": row.get("client_scope_id"),
                "created_at": row.get("created_at"),
                "persona_id": row.get("persona_id"),
                "persona_label": row.get("persona_label"),
                "markets": row.get("markets"),
                "signal_quality": row.get("signal_quality"),
            }
        )
    return out


# ---- Seed Explorer (V3.8 LP1) ------------------------------------------------


def _seed_graph_cache_key(kind: str, market: str, keyword: str) -> str:
    return (
        kind
        + ":"
        + (market or "za").strip().lower()
        + ":"
        + (keyword or "").strip().lower()
    )


def _seed_graph_cache_get(key: str) -> dict | None:
    now = time.monotonic()
    cached = _seed_graph_cache.get(key)
    if cached is not None and now - cached[0] < _SEED_GRAPH_CACHE_TTL_SECONDS:
        return cached[1]
    return None


def _seed_graph_cache_set(key: str, value: dict) -> None:
    if len(_seed_graph_cache) >= _SEED_GRAPH_CACHE_MAX:
        oldest = next(iter(_seed_graph_cache))
        del _seed_graph_cache[oldest]
    _seed_graph_cache[key] = (time.monotonic(), value)


def _normalize_seed_keyword(keyword: str) -> str:
    kw = normalize_query(keyword)
    if kw.startswith("#"):
        kw = kw[1:].strip()
    return kw


def _seed_keyword_variants(keyword: str) -> list[str]:
    kw = _normalize_seed_keyword(keyword)
    if not kw:
        return []
    out = [kw]
    first = kw.split()[0]
    if first and first not in out:
        out.append(first)
    return out


def _latest_seed_graph_date(market: str):
    mk = market.strip().lower()
    now = time.monotonic()
    cached = _latest_seed_graph_date_cache.get(mk)
    if cached is not None and now - cached[0] < _SEED_GRAPH_CACHE_TTL_SECONDS:
        return cached[1]
    sql = (
        "SELECT MAX(trend_date) AS d FROM " + _table("seed_graph") + " "
        "WHERE market = @market "
        "AND trend_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) "
        "AND trend_date <= CURRENT_DATE()"
    )
    rows = _run_query(sql, [("market", "STRING", mk)])
    d = rows[0].get("d") if rows else None
    # A missing snapshot must be checked again when data arrives.
    if d is not None:
        _latest_seed_graph_date_cache[mk] = (now, d)
    return d


def _fetch_seed_graph_term_rows(market: str, trend_date, terms: list) -> list:
    if not terms or trend_date is None:
        return []
    sql = (
        "SELECT market, term, term_type, platform, trend_date, row_count, "
        "topic_groups, near_topics, co_occur_terms "
        "FROM " + _table("seed_graph") + " "
        "WHERE market = @market AND trend_date = @d "
        "AND LOWER(term) IN UNNEST(@terms)"
    )
    lowered = [t.lower() for t in terms]
    return _run_query(
        sql,
        [
            ("market", "STRING", market.strip().lower()),
            ("d", "DATE", trend_date),
            ("terms", "STRING", lowered),
        ],
    )


def _aggregate_co_occur(rows: list) -> list:
    counts: Counter = Counter()
    types: dict[str, str] = {}
    for r in rows:
        tt = str(r.get("term_type") or "")
        for t in r.get("co_occur_terms") or []:
            term = str(t).strip().lower()
            if not term:
                continue
            counts[term] += 1
        term = str(r.get("term") or "").strip().lower()
        if term and tt:
            types[term] = tt
    out = []
    for term, n in counts.most_common(20):
        out.append({"term": term, "count": int(n), "term_type": types.get(term, "")})
    return out


def _aggregate_topic_overlap(rows: list) -> list:
    topics: Counter = Counter()
    for r in rows:
        for tg in r.get("topic_groups") or []:
            key = str(tg).strip()
            if key:
                topics[key] += int(r.get("row_count") or 1)
    return [
        {"topic": tg, "label": topic_label(tg), "row_count": int(n)}
        for tg, n in topics.most_common(12)
    ]


def _aggregate_near_topics(rows: list) -> list:
    near: Counter = Counter()
    for r in rows:
        for nt in r.get("near_topics") or []:
            key = str(nt).strip()
            if key:
                near[key] += 1
    return [
        {"topic": tg, "label": topic_label(tg), "count": int(n)}
        for tg, n in near.most_common(8)
    ]


def _fetch_bridge_creators_for_keyword(
    keyword: str, market: str, limit: int = 8
) -> list:
    kw = _normalize_seed_keyword(keyword)
    if not kw:
        return []
    tok = kw.split()[0]
    pat = r"(^|\W)" + re.escape(tok) + r"(\W|$)"
    sql = (
        "WITH keyword_posts AS ("
        "SELECT ec.author_handle, ec.platform, ec.market, tg "
        "FROM " + _table("enriched_content") + " ec, UNNEST(ec.topic_groups) AS tg "
        "WHERE ec.collected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY) "
        "AND ec.market = @market "
        "AND REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(ec.title, ''), ' ', IFNULL(ec.text, ''), ' ', "
        "IFNULL(ec.hashtags, ''))), @pat) "
        "AND ec.author_handle IS NOT NULL AND TRIM(ec.author_handle) != ''"
        + _NON_VOICE_SQL
        + "), bridges AS ("
        "SELECT author_handle, market FROM keyword_posts "
        "GROUP BY author_handle, market "
        "HAVING COUNT(DISTINCT SPLIT(tg, '_')[OFFSET(0)]) >= 2) "
        "SELECT kp.author_handle AS handle, ANY_VALUE(kp.platform) AS platform, COUNT(*) AS posts "
        "FROM keyword_posts kp JOIN bridges b USING (author_handle, market) "
        "GROUP BY kp.author_handle "
        "ORDER BY posts DESC LIMIT @lim"
    )
    rows = _run_query(
        sql,
        [
            ("market", "STRING", market.strip().lower()),
            ("pat", "STRING", pat),
            ("lim", "INT64", limit),
        ],
    )
    out = []
    for r in rows:
        handle = r.get("handle")
        platform = r.get("platform") or ""
        if not is_real_handle(handle, platform):
            continue
        out.append(
            {
                "handle": mask_handle(handle, platform),
                "platform": _platform_label(platform),
                "posts": int(r.get("posts") or 0),
            }
        )
    return out[:limit]


def _lexicon_hits_for_terms(market: str, terms: list) -> list:
    want = {t.lower() for t in (terms or []) if t}
    if not want:
        return []
    lex = fetch_lexicon_rows(market)
    out = []
    for row in lex:
        term = str(row.get("term") or "").strip().lower()
        if term in want:
            out.append({"term": term, "n": int(row.get("n") or 0)})
    out.sort(key=lambda d: d["n"], reverse=True)
    return out[:12]


def fetch_seed_graph_adjacency(keyword: str, market: str) -> dict:
    """Co-occurring terms, topic overlap, bridge creators and lexicon hits for a keyword.

    Reads the latest seed_graph day for the market. Results are cached ~10 min per
    (market, keyword) so repeat lookups stay warm."""
    mk = (market or "za").strip().lower()
    variants = _seed_keyword_variants(keyword)
    cache_key = _seed_graph_cache_key("adjacency", mk, variants[0] if variants else "")
    cached = _seed_graph_cache_get(cache_key)
    if cached is not None:
        return cached

    kw = variants[0] if variants else ""
    # Bridge creators is the heaviest read here, a 30-day regex scan of
    # enriched_content, and it needs only the keyword and market. Start it first so
    # it runs alongside the date lookup, the term rows and the lexicon read instead
    # of waiting behind all three.
    with ThreadPoolExecutor(max_workers=1) as pool:
        bridge_future = pool.submit(_fetch_bridge_creators_for_keyword, kw, mk)
        d = _latest_seed_graph_date(mk)
        rows = _fetch_seed_graph_term_rows(mk, d, variants)
        co_occur = _aggregate_co_occur(rows)
        co_terms = [c["term"] for c in co_occur]
        lexicon_hits = _lexicon_hits_for_terms(mk, co_terms)
        bridge_creators = bridge_future.result()
    payload = {
        "keyword": kw,
        "market": mk,
        "trend_date": d.isoformat() if d else None,
        "found": bool(rows),
        "co_occur_terms": co_occur,
        "topic_overlap": _aggregate_topic_overlap(rows),
        "near_topics": _aggregate_near_topics(rows),
        "bridge_creators": bridge_creators,
        "lexicon_hits": lexicon_hits,
    }
    if d is not None and rows:
        _seed_graph_cache_set(cache_key, payload)
    elif _latest_seed_graph_date_cache.get(mk, (None, None))[1] == d:
        _latest_seed_graph_date_cache.pop(mk, None)
    return payload


def _seed_path_coverage_note(platforms_seen: set) -> str:
    missing = sorted(_SEED_PATH_NOT_INGESTED - platforms_seen)
    if not missing:
        return ""
    labels = [_platform_label(p) for p in missing]
    return "Not in our data: " + ", ".join(labels)


def fetch_seed_path(keyword: str, market: str) -> dict:
    """Behaviour path for a keyword from seed_graph and v_seed_first_seen.

    Platforms are ordered by first_seen_event_date so the trail reads as a
    diffusion story. Cached ~10 min per (market, keyword)."""
    mk = (market or "za").strip().lower()
    variants = _seed_keyword_variants(keyword)
    cache_key = _seed_graph_cache_key("path", mk, variants[0] if variants else "")
    cached = _seed_graph_cache_get(cache_key)
    if cached is not None:
        return cached

    d = _latest_seed_graph_date(mk)
    empty = {
        "keyword": variants[0] if variants else "",
        "market": mk,
        "trend_date": d.isoformat() if d else None,
        "term": variants[0] if variants else "",
        "term_type": "",
        "channels": [],
        "span_days": 0,
        "confidence": "thin",
        "coverage_note": _seed_path_coverage_note(set()),
    }
    if not variants or d is None:
        return empty

    sql = (
        "SELECT sg.term, sg.term_type, sg.platform, sg.row_count, "
        "fs.first_seen_event_date, fs.first_seen_ingest_date "
        "FROM " + _table("seed_graph") + " sg "
        "LEFT JOIN " + _table("v_seed_first_seen") + " fs "
        "ON sg.market = fs.market AND sg.term = fs.term AND sg.platform = fs.platform "
        "WHERE sg.market = @market AND sg.trend_date = @d "
        "AND LOWER(sg.term) IN UNNEST(@terms) "
        "ORDER BY sg.row_count DESC"
    )
    rows = _run_query(
        sql,
        [
            ("market", "STRING", mk),
            ("d", "DATE", d),
            ("terms", "STRING", [t.lower() for t in variants]),
        ],
    )
    if not rows:
        if _latest_seed_graph_date_cache.get(mk, (None, None))[1] == d:
            _latest_seed_graph_date_cache.pop(mk, None)
        return empty

    by_term: dict[str, list] = {}
    term_types: dict[str, str] = {}
    for r in rows:
        term = str(r.get("term") or "")
        plat = str(r.get("platform") or "")
        first_seen = r.get("first_seen_event_date") or r.get("first_seen_ingest_date")
        fs_str = first_seen.isoformat() if first_seen else None
        watched_since = fs_str == d.isoformat() if fs_str else False
        term_types[term] = str(r.get("term_type") or "")
        by_term.setdefault(term, []).append(
            {
                "platform": _platform_label(plat),
                "platform_key": plat,
                "first_seen": fs_str,
                "row_count": int(r.get("row_count") or 0),
                "watched_since": watched_since,
            }
        )

    top_term = max(by_term.items(), key=lambda kv: sum(c["row_count"] for c in kv[1]))[
        0
    ]
    channels = sorted(
        by_term[top_term],
        key=lambda c: (c.get("first_seen") or "9999", -c["row_count"]),
    )
    for ch in channels:
        ch.pop("platform_key", None)
    platforms_seen = {str(r.get("platform") or "") for r in rows if r.get("platform")}
    dates = [c["first_seen"] for c in channels if c.get("first_seen")]
    span = 0
    if len(dates) >= 2:
        parsed = sorted(dates)
        span = (date.fromisoformat(parsed[-1]) - date.fromisoformat(parsed[0])).days
    total_rows = sum(c["row_count"] for c in channels)
    n_plat = len({c["platform"] for c in channels if not c.get("watched_since")})
    measured = (
        n_plat >= _SEED_PATH_MEASURED_MIN_PLATFORMS
        and span >= _SEED_PATH_MEASURED_MIN_SPAN_DAYS
        and total_rows >= _SEED_PATH_MEASURED_MIN_ROWS
    )
    payload = {
        "keyword": variants[0],
        "market": mk,
        "trend_date": d.isoformat(),
        "term": top_term,
        "term_type": term_types.get(top_term, ""),
        "channels": channels,
        "span_days": span,
        "confidence": "measured" if measured else "thin",
        "coverage_note": _seed_path_coverage_note(platforms_seen),
    }
    _seed_graph_cache_set(cache_key, payload)
    return payload


# ---- Seed Discover (V3.8 42 Discover) ----------------------------------------


def _monday_on_or_before(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _seed_fit_dict(row: dict) -> dict[str, float]:
    raw = row.get("seed_fit")
    if raw is None:
        return {}
    if hasattr(raw, "items"):
        return {k: float(v or 0) for k, v in raw.items() if v is not None}
    return {}


def fetch_seed_candidates_week(
    *,
    week_start: date,
    week_end: date,
    status_mode: str = "pending",
    market: str | None = None,
) -> list[dict]:
    """Rows from seed_candidates for the Discover desk."""
    clauses = ["proposed_date >= @week_start", "proposed_date <= @week_end"]
    params: list[tuple] = [
        ("week_start", "DATE", week_start),
        ("week_end", "DATE", week_end),
    ]
    mode = (status_mode or "pending").strip().lower()
    if mode == "pending":
        clauses.append("status = 'pending'")
    elif mode == "decided":
        clauses.append("status IN ('approved', 'rejected', 'applied', 'reverted')")
        lookback = week_end - timedelta(days=27)
        clauses[0] = "proposed_date >= @lookback"
        params[0] = ("lookback", "DATE", lookback)

    if market and market.lower() in MARKET_LABELS:
        clauses.append("market = @market")
        params.append(("market", "STRING", market.lower()))

    sql = (
        "SELECT candidate_id, proposed_date, market, candidate_type, candidate_value, "
        "source, lane, score, seed_fit, safety_flags, evidence_topics, "
        "sample_row_ids, status, rationale "
        f"FROM {_table('seed_candidates')} "
        "WHERE " + " AND ".join(clauses) + " "
        "ORDER BY market, lane DESC, score DESC "
        "LIMIT 200"
    )
    return _run_query(sql, params)
