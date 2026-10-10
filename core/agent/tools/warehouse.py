"""The agent's warehouse tools: search_posts, fetch_posts, rising_topics, recall_findings and save_finding
(AGENT.md, Tools).

Reads go through sql_query, so each result carries a query_id and result hash. Every value is a named parameter.
save_finding is the agent's only write path (DATA.md section 7); it holds the finding until the answer's trust gate
has run, and commit_findings then writes the ones whose claim passed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import tomllib
from pathlib import Path
from typing import Protocol

from core.agent.checks import LABEL_RANK
from core.agent.context import STORE_TOOL, SWEEP_TOOL, Refused, RunContext
from core.agent.skills import PAGE_TIERS
from core.agent.spread import spread_order
from core.agent.tools.dates import SAST, resolve_dates
from core.agent.tools.socialcrawl import PLATFORM_NAMES, _fence
from core.agent.tools.sql_query import PROJECT, Warehouse, internal_read, sql_query

log = logging.getLogger(__name__)

MAX_POSTS = 100  # breadth (Albert, 4 October): answers drew on about 20 accounts from a store of thousands
DEFAULT_LIMIT = MAX_POSTS  # a search returns the most it may unless the model asks for fewer (Albert, 10 October)
MAX_CREATOR_CANDIDATES = 20
MAX_TERMS = 8
MAX_RISING_TOPIC_ROWS = 50
LOCAL_TERMS_PATH = Path(__file__).resolve().parents[1] / "config" / "local_terms.toml"
RRF_K = 60
MAX_PER_AUTHOR = 3
SORTS = {
    "engagement": "IFNULL(p.engagement, 0) DESC, p.post_date DESC",
    "recent": "COALESCE(p.published_at, TIMESTAMP(p.post_date)) DESC",
}
LABELS = ("observed", "corroborated", "single_source", "inferred")
FINDING_STATUSES = ("current", "stale", "contradicted")
FINDINGS_TABLE = "intelligence_42_agent.findings"
ENGAGEMENT = ("views", "likes", "comments", "shares")
STORED_PLATFORMS = {v: k for k, v in PLATFORM_NAMES.items()}  # a platform filter binds the stored name
MARKETS = ("ZA", "NG", "KE")
MARKET_NAMES = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}
PROFILE_SOURCE = "home_market"
SOURCE_MARKETS_VIEW = "intelligence_42_core.v_post_source_markets"
_BYTE_CAP_ERRORS = (
    "byte cap",
    "bytes billed",
    "maximum bytes billed",
    "query exceeded limit for bytes billed",
)
_MODEL_CONNECTION_ERRORS = (
    "model connection",
    "connection to remote model",
    "connection for remote model",
    "remote model connection",
)
# A post without a creator row falls back to its stable creator_id for the handle.
POST_COLUMNS = ("p.post_id, p.platform, p.url, p.creator_id, COALESCE(c.handle, p.creator_id) AS handle, "
                "p.published_at, p.post_date, p.geo_market, p.text, p.views, p.likes, p.comments, p.shares, "
                "p.engagement, p.geo_source, c.home_market, o.source_sightings")
CREATOR_POST_COLUMNS = ("p.post_id, p.platform, p.url, p.creator_id, COALESCE(c.handle, p.creator_id) AS handle, "
                        "p.published_at, p.post_date, p.geo_market, p.text, p.views, p.likes, p.comments, p.shares, "
                        "p.engagement, p.geo_source, c.home_market, o.source_sightings")
POSTS_JOIN = ("FROM intelligence_42_core.posts p "
              "LEFT JOIN intelligence_42_core.creators c ON c.platform = p.platform AND c.creator_id = p.creator_id ")
CREATOR_INTENT = re.compile(r"\b(?:creators?|djs?|influencers?)\b", re.I)
CREATOR_MARKETS = (("KE", r"\bkenya\w*\b"), ("NG", r"\bnigeria\w*\b"),
                   ("ZA", r"\b(?:south\s+africa\w*|za)\b"))
CREATOR_TOPIC_IGNORES = frozenset(
    "a about and are as at being big behave by can comment comments collaboration collaborations content couple "
    "creator creators dance dj djs do driving formats from how in imported is it just last local localised localized "
    "lyrics makers mid months moves not now of or posting right sections sheng skit sized south spreading still "
    "swahili the their they using what which who with kenya kenyan nigeria nigerian africa african".split()
)
CREATOR_SIZE_WORDS = re.compile(r"\b(?:mid[- ]sized|medium[- ]sized|small|large)\b", re.I)
CREATOR_COMMENTS_WORDS = re.compile(r"\bcomments?\b|\bcomment\s+sections?\b", re.I)


def retrieval_refusal_category(exc: Exception) -> str:
    """Classify known retrieval failures without exposing their messages."""
    error_type = type(exc)
    error_name = error_type.__name__.casefold()
    error_module = error_type.__module__.casefold()
    message = str(exc).casefold()

    if error_module == "core.eval.ask_r2" and error_name in {"operationrefused", "budgetrefused"}:
        return "guard"

    if isinstance(exc, Refused):
        return "byte_cap" if any(marker in message for marker in _BYTE_CAP_ERRORS) else "guard"

    if error_module.startswith("google."):
        code = getattr(exc, "code", None)
        status = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        if callable(code):
            code = code()
        if callable(status):
            status = status()
        if code == 403 or status == 403 or error_name in {"forbidden", "permissiondenied"}:
            return "permission"

    if any(marker in message for marker in _BYTE_CAP_ERRORS):
        return "byte_cap"
    if any(marker in message for marker in _MODEL_CONNECTION_ERRORS):
        return "model_connection"
    return "unknown"


def _emit_retrieval_trace(ctx: RunContext, mode: str, query_id=None, rows=None, error=None) -> None:
    if error is not None:
        category = retrieval_refusal_category(error)
        ctx.emit(
            "retrieval_trace",
            mode=mode,
            query_id=query_id,
            row_count=None,
            post_ids=[],
            status="refused" if category in {"guard", "permission", "byte_cap"} else "error",
            refusal_category=category,
            error_type=type(error).__name__,
        )
        return

    rows = rows or []
    ctx.emit(
        "retrieval_trace",
        mode=mode,
        query_id=query_id,
        row_count=len(rows),
        post_ids=[str(_field(row, "post_id")) for row in rows],
        status="success" if rows else "empty",
        refusal_category=None,
    )


def _source_sightings_join(alias: str, since: str, until: str, scoped: bool = False) -> str:
    scoped_markets = ""
    if scoped:
        scoped_markets = (
            ", ARRAY_AGG(DISTINCT IF(ss.source_market = @market, ss.source_market, NULL) "
            "IGNORE NULLS) AS scoped_markets"
        )
    return (
        "LEFT JOIN (SELECT sm.post_id, ARRAY_AGG(ss) AS source_sightings"
        f"{scoped_markets} FROM {SOURCE_MARKETS_VIEW} sm "
        "CROSS JOIN UNNEST(sm.source_sightings) AS ss "
        f"WHERE ss.obs_date BETWEEN @{since} AND @{until} GROUP BY sm.post_id) o "
        f"ON o.post_id = {alias}.post_id "
    )


def _field(row, name):
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (IndexError, KeyError, TypeError):
        return None


def located(row: dict) -> bool:
    return bool(_field(row, "geo_market")) and _field(row, "geo_source") != PROFILE_SOURCE


def sighting_market(sighting) -> str | None:
    market = _field(sighting, "source_market")
    if market not in MARKETS:
        return None
    return market


def source_market(row: dict, ask_market=None) -> str | None:
    found = {market for sighting in _field(row, "source_sightings") or []
             if (market := sighting_market(sighting))}
    home_market = _field(row, "home_market")
    if home_market in MARKETS:
        found.add(home_market)
    geo_market = _field(row, "geo_market")
    if _field(row, "geo_source") == PROFILE_SOURCE and geo_market in MARKETS:
        found.add(geo_market)
    if ask_market in found:
        return ask_market
    return next(iter(found)) if len(found) == 1 else None


def _day(value, name: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise Refused(f"{name} must be a date as YYYY-MM-DD; got {value!r}.") from None


# Words that join search terms rather than being searched for. Live staging, 4 October: 'sound OR challenge OR
# trend' required the literal word OR in every post, so the keyword search found almost nothing.
_OR, _AND = ("OR", "|"), ("AND", "&")


def _terms(query: str, params: dict, strip_hash: bool = False) -> list[list[str]]:
    """Groups of named parameters, one per term: the terms in a group are ANDed and the groups ORed. OR or | between
    terms starts a new group; AND or & is dropped. strip_hash drops the leading # of a term (a term of only # goes)."""
    groups, count = [[]], 0
    for word in (query or "").split():
        if word in _OR:
            groups.append([])
            continue
        if word in _AND:
            continue
        if strip_hash:
            word = word.lstrip("#")
            if not word:
                continue
        if count >= MAX_TERMS:
            raise Refused(f"Use at most {MAX_TERMS} search terms; got more.")
        params[f"term_{count}"] = word
        groups[-1].append(f"@term_{count}")
        count += 1
    groups = [g for g in groups if g]
    if not groups:
        raise Refused("The search query is empty.")
    return groups


def _match(groups: list[list[str]], test: str) -> str:
    """One SQL condition: test formatted with each term, ANDed within a group and ORed across groups."""
    ands = ["(" + " AND ".join(test.format(name) for name in group) + ")" for group in groups]
    return ands[0] if len(ands) == 1 else "(" + " OR ".join(ands) + ")"


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def is_creator_question(question: str) -> bool:
    return bool(CREATOR_INTENT.search(question or ""))


def _creator_topic_terms(question: str) -> list[str]:
    terms = []
    for term in re.findall(r"[a-z0-9]+", (question or "").lower()):
        if term not in CREATOR_TOPIC_IGNORES and term not in terms:
            terms.append(term)
    return terms[:MAX_TERMS]


def _creator_market(ctx: RunContext, question: str) -> str | None:
    for market, pattern in CREATOR_MARKETS:
        if re.search(pattern, question or "", re.I):
            return market
    return ctx.market if ctx.market in MARKETS else None


def _creator_window(ctx: RunContext) -> tuple[dt.date, dt.date]:
    as_of = _day(ctx.as_of, "as_of")
    since = _day(ctx.window_start, "window_start") if ctx.window_start else as_of - dt.timedelta(days=59)
    until = _day(ctx.window_end, "window_end") if ctx.window_end else as_of
    if since > until:
        raise Refused(f"The ask's window starts after it ends, {since} to {until}.")
    return since, until


def _store_creator_post(ctx: RunContext, row: dict) -> dict:
    return _store(ctx, row, allow_context_market=False)


def discover_creators(ctx: RunContext, warehouse: Warehouse, question: str) -> dict:
    if not is_creator_question(question):
        return {"creator_candidates": [], "discovery_gaps": [], "evidence": [], "query_ids": [], "query_id": None}

    since, until = _creator_window(ctx)
    market = _creator_market(ctx, question)
    topic_terms = _creator_topic_terms(question)
    topic_params = {f"topic_{i}": term for i, term in enumerate(topic_terms)}
    topic_filter = "(" + " OR ".join(
        f"CONTAINS_SUBSTR(p.text, @{name})" for name in topic_params
    ) + ")" if topic_params else ""
    tier_params = {f"page_tier_{i}": tier for i, tier in enumerate(PAGE_TIERS)}
    tier_names = ", ".join(f"@{name}" for name in tier_params)
    creator_where = [
        f"c.tier IN ({tier_names})",
        "NOT EXISTS (SELECT 1 FROM intelligence_42_core.v_suppressed_creators s "
        "WHERE s.creator_id = c.creator_id)",
    ]
    creator_params = {"creator_limit": MAX_CREATOR_CANDIDATES, **tier_params}
    creator_source = "FROM intelligence_42_core.creators c "
    creator_group = ""
    creator_order = "ORDER BY c.handle, c.platform, c.creator_id"
    if topic_params:
        creator_source += (
            "LEFT JOIN intelligence_42_core.posts p ON p.platform = c.platform AND p.creator_id = c.creator_id "
            "AND p.post_date BETWEEN @since AND @until AND "
        ) + topic_filter + " "
        creator_group = "GROUP BY c.platform, c.creator_id, c.handle, c.display_name, c.tier "
        creator_order = "ORDER BY MAX(p.post_date) DESC NULLS LAST, c.handle, c.platform, c.creator_id"
        creator_params.update({**topic_params, "since": since, "until": until})
    if market:
        creator_where.append("(c.home_market = @market OR c.verified_region = @market)")
        creator_params["market"] = market
    creator_sql = (
        "SELECT c.platform, c.creator_id, c.handle, c.display_name, c.tier "
        f"{creator_source}"
        f"WHERE {' AND '.join(creator_where)} "
        f"{creator_group}{creator_order} LIMIT @creator_limit"
    )
    creator_result = internal_read("discover_creators", ctx, warehouse, creator_sql,
                                   purpose="discover_creators: bounded identities", params=creator_params)
    candidates = creator_result["rows"]
    query_ids = [creator_result["query_id"]]
    post_result = None

    if candidates and topic_terms:
        identity_params = {}
        identity_filter = []
        for i, creator in enumerate(candidates):
            platform_key, creator_key = f"creator_platform_{i}", f"creator_id_{i}"
            identity_params[platform_key] = creator["platform"]
            identity_params[creator_key] = creator["creator_id"]
            identity_filter.append(f"(p.platform = @{platform_key} AND p.creator_id = @{creator_key})")
        post_params = {
            **topic_params,
            **identity_params,
            "since": since,
            "until": until,
            "post_limit": MAX_POSTS,
        }
        post_sql = (
            f"SELECT {CREATOR_POST_COLUMNS} {POSTS_JOIN}{_source_sightings_join('p', 'since', 'until')}"
            f"WHERE p.post_date BETWEEN @since AND @until AND {topic_filter} "
            f"AND ({' OR '.join(identity_filter)}) "
            "ORDER BY COALESCE(p.published_at, TIMESTAMP(p.post_date)) DESC, p.platform, p.creator_id, p.post_id "
            "LIMIT @post_limit"
        )
        post_result = sql_query(ctx, warehouse, post_sql, purpose="discover_creator_posts: bounded topic posts",
                                params=post_params)
        query_ids.append(post_result["query_id"])

    evidence = [_store_creator_post(ctx, row) for row in (post_result["rows"] if post_result else [])]
    matched = {(row["platform"], row["creator_id"]) for row in (post_result["rows"] if post_result else [])}
    gaps = [
        {**row, "reason": "no matching stored post returned in the ask window"}
        for row in candidates if (row["platform"], row["creator_id"]) not in matched and topic_terms
    ]
    size_status = "unknown" if CREATOR_SIZE_WORDS.search(question or "") else "not_requested"
    comments_status = "unavailable" if CREATOR_COMMENTS_WORDS.search(question or "") else "not_requested"
    result = {
        "creator_candidates": candidates,
        "discovery_gaps": gaps,
        "evidence": evidence,
        "query_ids": query_ids,
        "query_id": post_result["query_id"] if post_result else creator_result["query_id"],
        "creator_query_id": creator_result["query_id"],
        "posts_query_id": post_result["query_id"] if post_result else None,
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "discovery_market": market,
        "geography_scope": "discovery_only",
        "topic_terms": topic_terms,
        "creator_size_status": size_status,
        "comment_text_status": comments_status,
        "limits": {"creator_candidates": MAX_CREATOR_CANDIDATES, "posts": MAX_POSTS},
    }
    if size_status == "unknown":
        result["creator_size_note"] = (
            "Only macro and mega page tiers pass the naming gate; that filter does not establish a mid-sized category."
        )
    if comments_status == "unavailable":
        result["comment_text_note"] = "Stored posts expose a comment count, not comment text or commenter behavior."
    return result


def load_local_terms(path=None) -> list[dict]:
    """The curated local terms (config/local_terms.toml): topics, each with the lower case words that call for it and
    the single words to search beside them. A missing file, a file that does not parse and an entry of the wrong shape
    each give nothing, so a bad edit costs the extra terms and never a search."""
    path = Path(path) if path else LOCAL_TERMS_PATH
    try:
        topics = tomllib.loads(path.read_text(encoding="utf-8")).get("topics", [])
    except FileNotFoundError:
        return []
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        log.warning("local terms file unreadable: %s: %s", path.name, type(e).__name__)
        return []
    out = []
    for topic in topics if isinstance(topics, list) else []:
        when = topic.get("when") if isinstance(topic, dict) else None
        terms = topic.get("terms") if isinstance(topic, dict) else None
        if not isinstance(when, list) or not isinstance(terms, list):
            continue
        clean = [str(w).strip().lstrip("#").lower() for w in when if isinstance(w, str)]
        words = []
        for term in terms:
            word = term.strip().lstrip("#") if isinstance(term, str) else ""
            if TAG_WORD.fullmatch(word) and word not in words:
                words.append(word)
        if [w for w in clean if w] and words:
            out.append({"when": [w for w in clean if w], "terms": words})
    return out


# A term matches the caption anywhere, or a hashtag of the post exactly, without the # and whatever the case: a tag
# held only in the hashtags column (the parser fills it from the vendor's list) is found as well.
TERM_TEST = ("(CONTAINS_SUBSTR(p.text, {0}) OR EXISTS (SELECT 1 FROM UNNEST(p.hashtags) AS h "
             "WHERE LOWER(TRIM(h)) = LOWER({0})))")
TAG_TEST = "EXISTS (SELECT 1 FROM UNNEST(p.hashtags) AS h WHERE LOWER(TRIM(h)) = LOWER({0}))"  # the column alone
RELATED_TAG_POOL = 30  # rows the co-occurrence read returns; the term cap decides how many are used
MIN_RELATED_CREATORS = 2  # a tag seen with the seed by one creator only is not a family
# A tag joins the family only when at least this share of its own posts also carry the seed. That keeps out the tags
# every post carries (fyp, viral, foryou) without a list of them. A tag also needs this many characters, so a short one
# cannot stand for a letter pair that is in most words.
MIN_RELATED_SHARE = 0.2
MIN_TAG_CHARS = 3
TAG_WORD = re.compile(r"\w{3,50}")


def _reason(exc: Exception) -> str:
    """Why a search leg failed, in one line for the model and the log: the first line of the error, links removed."""
    first = re.sub(r"https?://\S+", "", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    return " ".join(first.split())[:300]


def _check_market(ctx: RunContext, market) -> None:
    """search_posts follows the question's market. A market the model names is accepted only when it is that market, so
    naming one never widens or moves the search."""
    asked = str(market).strip().upper()
    if not ctx.market:
        raise Refused("search_posts follows the question's market and this question has none, so market is not "
                      "accepted. Leave it out and put the place in the query.")
    if asked != ctx.market:
        raise Refused(f"search_posts searches the question's market, {ctx.market}, and cannot search {asked}. "
                      "Leave market out of the call.")


def _has_terms(query) -> bool:
    return any(w not in _OR and w not in _AND and w.lstrip("#") for w in (query or "").split())


def _search_dates(ctx: RunContext, since, until) -> tuple[dt.date, dt.date, tuple | None]:
    default_since, default_until = resolve_dates("last 7 days", ctx.as_of)
    since = default_since if since is None else _day(since, "since")
    until = default_until if until is None else _day(until, "until")
    if since > until:
        raise Refused(f"since {since} is after until {until}.")
    window = (ctx.window_start, ctx.window_end) if ctx.window_start and ctx.window_end else None
    if window:
        since, until = max(since, window[0]), min(until, window[1])
        if since > until:
            raise Refused(f"The dates asked fall outside the ask's window, {window[0]} to {window[1]}.")
    return since, until, window


def _search_filters(platforms, author, min_engagement) -> tuple[list[str], dict]:
    filters, shared = ["IFNULL({a}.engagement, 0) >= @min_engagement"], {"min_engagement": int(min_engagement or 0)}
    wanted = [str(p).strip().lower() for p in (platforms or []) if str(p).strip()]
    if wanted:
        names = []
        for i, platform in enumerate(wanted):
            shared[f"platform_{i}"] = STORED_PLATFORMS.get(platform, platform)
            names.append(f"@platform_{i}")
        filters.append(f"{{a}}.platform IN ({', '.join(names)})")
    if author and str(author).strip().lstrip("@"):
        shared["author"] = str(author).strip().lstrip("@")
        filters.append("(LOWER(c.handle) = LOWER(@author) OR {a}.creator_id = @author)")
    return filters, shared


def _keyword_sql(ctx: RunContext, query: str, since, until, shared: dict, filters: list[str], sort: str,
                 limit: int, test: str = TERM_TEST, tag_terms=()) -> tuple[str, dict]:
    """The keyword leg's SQL and parameters. tag_terms are alternatives to the query that match the hashtags column
    only (the co-occurring tags). Raises Refused for more than MAX_TERMS terms in all."""
    params = {"since": since, "until": until, **shared, "limit": limit}
    where = ["p.post_date BETWEEN @since AND @until"]
    condition = _match(_terms(query, params, strip_hash=True), test)
    if tag_terms:
        used = sum(1 for k in params if k.startswith("term_"))
        if used + len(tag_terms) > MAX_TERMS:
            raise Refused(f"Use at most {MAX_TERMS} search terms; got more.")
        extra = []
        for n, tag in enumerate(tag_terms, start=used):
            params[f"term_{n}"] = tag
            extra.append(TAG_TEST.format(f"@term_{n}"))
        condition = "(" + " OR ".join([condition, *extra]) + ")"
    where.append(condition)
    where += [f.format(a="p") for f in filters]
    if ctx.market:
        params["market"] = ctx.market
        params["profile_source"] = PROFILE_SOURCE
        where.append("(p.geo_market = @market OR ((p.geo_market IS NULL OR p.geo_source = @profile_source) "
                     "AND (c.home_market = @market OR @market IN UNNEST(o.scoped_markets))))")
    sql = (
        f"SELECT {POST_COLUMNS} {POSTS_JOIN}{_source_sightings_join('p', 'since', 'until', scoped=bool(ctx.market))}"
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {SORTS[sort]} LIMIT @limit"
    )
    return sql, params


def _semantic_sql(ctx: RunContext, query: str, since, until, shared: dict, filters: list[str],
                  k: int) -> tuple[str, dict]:
    params = {"q": query, "since": since, "until": until, "k": k, **shared}
    if ctx.market:
        params["market"] = ctx.market
    sql = (
        "SELECT s.post_id, s.platform, s.url, s.creator_id, COALESCE(c.handle, s.creator_id) AS handle, "
        "s.published_at, d.post_date, s.geo_market, d.geo_source, s.text, s.engagement, s.distance, "
        "c.home_market, o.source_sightings "
        f"FROM intelligence_42_agent.tvf_search_posts(@q, {'@market' if ctx.market else 'NULL'}, @since, @until, @k) s "
        "LEFT JOIN intelligence_42_core.creators c ON c.platform = s.platform AND c.creator_id = s.creator_id "
        # the function returns no post_date, the day a post with no published_at falls back to; the read of posts is
        # pruned to the same dates the function searched
        "LEFT JOIN (SELECT post_id, post_date, geo_source FROM intelligence_42_core.posts "
        "WHERE post_date BETWEEN @since AND @until) d ON d.post_id = s.post_id "
        f"{_source_sightings_join('s', 'since', 'until')}"
        f"WHERE {' AND '.join(f.format(a='s') for f in filters)} "
        "ORDER BY s.distance, s.post_id"
    )
    return sql, params


def related_hashtags(ctx: RunContext, warehouse: Warehouse, seeds: list[str], since, until, platforms) -> list[dict]:
    """The hashtags that sit on the same posts as any seed tag, in the ask's market and these dates (the market scope
    of every other stored read). Strongest first: a tag ranks by its posts with the seed squared over all its posts, so a
    tag that mostly appears beside the seed beats a tag every post carries. One recorded query. Rows are tag, co_posts,
    co_creators and all_posts."""
    joins, where, params, _ = _store_scope(ctx, platforms, (since, until))
    params["seed_keys"] = "|" + "|".join(s.replace("|", "") for s in seeds) + "|"
    params["min_creators"] = MIN_RELATED_CREATORS
    params["min_chars"] = MIN_TAG_CHARS
    params["min_share"] = MIN_RELATED_SHARE
    params["pool"] = RELATED_TAG_POOL
    sql = (
        "WITH scoped AS (SELECT p.post_id, p.creator_id, LOWER(TRIM(tag)) AS tag "
        f"{joins}CROSS JOIN UNNEST(p.hashtags) AS tag WHERE {where} AND IFNULL(TRIM(tag), '') != ''), "
        "seeded AS (SELECT DISTINCT post_id FROM scoped WHERE STRPOS(@seed_keys, CONCAT('|', tag, '|')) > 0), "
        "totals AS (SELECT tag, COUNT(DISTINCT post_id) AS all_posts FROM scoped GROUP BY tag), "
        "co AS (SELECT s.tag, COUNT(DISTINCT s.post_id) AS co_posts, COUNT(DISTINCT s.creator_id) AS co_creators "
        "FROM scoped s JOIN seeded d ON d.post_id = s.post_id "
        "WHERE STRPOS(@seed_keys, CONCAT('|', s.tag, '|')) = 0 GROUP BY s.tag) "
        "SELECT co.tag, co.co_posts, co.co_creators, totals.all_posts FROM co JOIN totals ON totals.tag = co.tag "
        "WHERE co.co_creators >= @min_creators AND LENGTH(co.tag) >= @min_chars "
        "AND co.co_posts / totals.all_posts >= @min_share "
        "ORDER BY co.co_posts * co.co_posts / totals.all_posts DESC, co.co_posts DESC, co.tag LIMIT @pool"
    )
    seeds_text = ", ".join(seeds)[:80]
    return sql_query(ctx, warehouse, sql, params=params,
                     purpose=f"search_posts related tags: hashtags posted beside {seeds_text}")["rows"]


def _share_ok(row) -> bool:
    co, total = (row.get("co_posts"), row.get("all_posts")) if isinstance(row, dict) else (None, None)
    return (isinstance(co, (int, float)) and isinstance(total, (int, float)) and total > 0
            and co / total >= MIN_RELATED_SHARE)


def _widen(ctx: RunContext, warehouse: Warehouse, query: str, since, until, platforms) -> tuple[list[str], list[str]]:
    """Words to search beside a query that is only alternatives (one word or hashtag each, joined by OR): the curated
    local terms for its topic (matched in the caption or the tags, as the query's own words are) and, when every
    alternative is a hashtag, the tags that co-occur with them (matched against the hashtags column only, and only
    those that sit mostly with the seed), all within the term cap. Any other query is searched as written. A failed
    co-occurrence read leaves the curated words. Returns (curated, tags)."""
    groups = [[]]
    for word in (query or "").split():
        if word in _OR:
            groups.append([])
        elif word not in _AND and word.lstrip("#"):
            groups[-1].append(word)
    groups = [g for g in groups if g]
    if not groups or any(len(g) != 1 for g in groups):
        return [], []
    room = MAX_TERMS - len(groups)
    seeds = [g[0].lstrip("#").lower() for g in groups]
    extras = []
    for topic in load_local_terms():
        if set(topic["when"]) & set(seeds):
            extras += [w for w in topic["terms"] if w.lower() not in seeds and w.lower() not in
                       [e.lower() for e in extras]]
    extras = extras[:max(room, 0)]
    tags = []
    if all(g[0].startswith("#") for g in groups) and len(extras) < room:
        try:
            rows = related_hashtags(ctx, warehouse, seeds, since, until, platforms)
        except Exception as e:
            log.warning("search_posts related tags failed: run %s: %s", ctx.run_id, _reason(e))
            rows = []
        for row in rows:
            tag = row.get("tag") if isinstance(row, dict) else None
            if (isinstance(tag, str) and TAG_WORD.fullmatch(tag) and _share_ok(row) and tag.lower() not in seeds
                    and tag.lower() not in [e.lower() for e in extras + tags]):
                tags.append(tag)
                if len(extras) + len(tags) == room:
                    break
    return extras, tags


def search_posts(ctx: RunContext, warehouse: Warehouse, query: str, platforms=None, since=None, until=None,
                 min_engagement=0, author=None, sort="engagement", limit=DEFAULT_LIMIT, market=None) -> dict:
    if market not in (None, ""):
        _check_market(ctx, market)
    if sort not in SORTS:
        raise Refused(f"sort must be one of {', '.join(SORTS)}; got {sort!r}.")
    if not _has_terms(query):
        raise Refused("The search query is empty.")
    since, until, window = _search_dates(ctx, since, until)

    limit = max(1, min(int(limit), MAX_POSTS))
    filters, shared = _search_filters(platforms, author, min_engagement)

    lists, query_ids, note, failed = [], [], {}, {}
    curated, tags = _widen(ctx, warehouse, query, since, until, platforms)
    if curated or tags:
        note["also_searched"] = curated + tags
    # Each leg stands alone: a leg that fails (a refused argument, a byte cap, a warehouse error) leaves the other to
    # answer. Both reasons reach the model and the log, and a call where both fail raises with both.
    try:
        sql, params = _keyword_sql(ctx, query + "".join(f" OR #{w}" for w in curated), since, until, shared, filters,
                                   sort, limit, tag_terms=tags)
        result = sql_query(ctx, warehouse, sql, purpose=f"search_posts: {query}", params=params)
    except Exception as e:
        _emit_retrieval_trace(ctx, "keyword", error=e)
        failed["keyword"] = _reason(e)
        note.update({"keyword": "unavailable", "keyword_reason": failed["keyword"]})
        log.warning("search_posts keyword leg failed: run %s: %s", ctx.run_id, failed["keyword"])
    else:
        _emit_retrieval_trace(ctx, "keyword", query_id=result["query_id"], rows=result["rows"])
        lists.append(result["rows"])
        query_ids.append(result["query_id"])

    # Semantic search catches posts that say the same thing in other words or languages (isiZulu, Pidgin, Sheng).
    try:
        sem_sql, sem_params = _semantic_sql(ctx, query, since, until, shared, filters, limit * 2)
        semantic = sql_query(ctx, warehouse, sem_sql, purpose=f"search_posts semantic: {query}", params=sem_params)
    except Exception as e:  # no embeddings yet, the function missing, or the byte cap
        _emit_retrieval_trace(ctx, "semantic", error=e)
        failed["semantic"] = _reason(e)
        note.update({"semantic": "unavailable", "semantic_reason": failed["semantic"]})
        log.warning("search_posts semantic leg failed: run %s: %s", ctx.run_id, failed["semantic"])
    else:
        _emit_retrieval_trace(ctx, "semantic", query_id=semantic["query_id"], rows=semantic["rows"])
        lists.append(semantic["rows"])
        query_ids.append(semantic["query_id"])

    if len(failed) == 2:
        raise Refused(f"Keyword search did not run: {failed['keyword']}. "
                      f"Semantic search did not run: {failed['semantic']}.")

    if window:  # the same rule as fetch_posts: no post published outside the ask's window is stored, and each query
        # record counts and lists what its own rows skipped
        for qid, rows in zip(query_ids, lists):
            skipped = [str(r["post_id"]) for r in rows if not _inside(r, window)]
            ctx.queries[qid]["skipped_outside_window"], ctx.queries[qid]["skipped_ids"] = len(skipped), skipped
        skipped = {str(r["post_id"]) for rows in lists for r in rows if not _inside(r, window)}
        lists = [[r for r in rows if _inside(r, window)] for rows in lists]
        note["skipped_outside_window"] = len(skipped)
    evidence = [_store(ctx, row) for row in _fuse(lists, limit, ctx.market)]
    return {"evidence": evidence, "query_id": query_ids[0], "query_ids": query_ids, **note}


# Fields of a stored post the research model is not sent: the url (the model reads a post by its id, text and
# engagement; the answer's evidence keeps the url) and evidence_id (the same string as id).
MODEL_VIEW_DROPPED = ("url", "evidence_id")
# Fields sent only when they say something, so an empty flag list or an unknown source market costs no tokens.
MODEL_VIEW_WHEN_SET = ("flags", "source_market")


def search_view(result: dict) -> dict:
    """search_posts' result as the research model receives it. The records keep every field the model reads a post by;
    the full records stay in ctx.evidence and the queries in ctx.queries, so receipts, result hashes and cited evidence
    ids are those of the full result. Returns a new dict and leaves result alone; anything that is not a search result
    with a list of records passes through as it is."""
    if not isinstance(result, dict) or not isinstance(result.get("evidence"), list):
        return result
    records = [{k: v for k, v in record.items()
                if k not in MODEL_VIEW_DROPPED and not (k in MODEL_VIEW_WHEN_SET and not v)}
               for record in result.get("evidence") or []]
    return {**result, "evidence": records}


def _store(ctx: RunContext, row: dict, allow_context_market=True) -> dict:
    """Store one posts row as an evidence record; return it as the model sees it, text fenced."""
    eid = str(row["post_id"])
    ask_market = ctx.market if allow_context_market else None
    source = source_market(row, ask_market)
    geo = _field(row, "geo_market") if located(row) else None
    market = geo or source or ask_market
    assumed = not geo and bool(market)
    record = {
        "id": eid,
        "platform": PLATFORM_NAMES.get(row.get("platform"), row.get("platform")),
        "handle": row.get("handle"),
        "url": row.get("url"),
        "posted_at": _iso(row.get("published_at") or row.get("post_date")),
        "market": market,
        "text": row.get("text"),
        "engagement": {k: row[k] for k in ENGAGEMENT if row.get(k) is not None},
        "flags": ["market_assumed"] if assumed else [],
        "source_market": source,
    }
    ctx.evidence[eid] = record
    return {**record, "evidence_id": eid, "text": _fence(record["text"])}


def _posted_day(row) -> dt.date | None:
    """The Johannesburg date a posts row was published, as K3 reads it; post_date when published_at is empty. A naive
    published_at is read as UTC, as BigQuery stores it."""
    published = row.get("published_at")
    if isinstance(published, str):
        try:
            published = dt.datetime.fromisoformat(published)
        except ValueError:
            published = None
    if isinstance(published, dt.datetime):
        return (published if published.tzinfo else published.replace(tzinfo=dt.timezone.utc)).astimezone(SAST).date()
    day = row.get("post_date")
    if isinstance(day, str):
        try:
            day = dt.date.fromisoformat(day[:10])
        except ValueError:
            return None
    if isinstance(day, dt.datetime):
        day = day.date()
    return day if isinstance(day, dt.date) else None


def _inside(row, window) -> bool:
    day = _posted_day(row)
    return day is not None and window[0] <= day <= window[1]


def fetch_posts(ctx: RunContext, warehouse: Warehouse, ids, window: tuple[dt.date, dt.date]) -> dict:
    """Store the posts behind ids the researcher only saw in sql_query rows, so a claim citing them resolves (K1).
    At most MAX_POSTS ids, each once, one named parameter per id. Only posts published inside the ask's window become
    evidence; the query record counts the rest in skipped_outside_window and lists them in skipped_ids."""
    wanted = list(dict.fromkeys(str(i) for i in ids if i))[:MAX_POSTS]
    if not wanted:
        return {"evidence": [], "query_id": None}
    post_params = {f"post_id_{i}": pid for i, pid in enumerate(wanted)}
    params = {**post_params, "source_since": window[0], "source_until": window[1]}
    ids = ", ".join("@" + name for name in post_params)
    sql = (f"SELECT {POST_COLUMNS} {POSTS_JOIN}"
           f"{_source_sightings_join('p', 'source_since', 'source_until')}WHERE p.post_id IN ({ids})")
    result = sql_query(ctx, warehouse, sql, purpose=f"fetch_posts: {len(wanted)} posts listed in query rows",
                       params=params)
    inside, skipped = [], []
    for row in result["rows"]:
        if _inside(row, window):
            inside.append(row)
        else:
            skipped.append(str(row["post_id"]))
    query = ctx.queries[result["query_id"]]
    query["skipped_outside_window"], query["skipped_ids"] = len(skipped), skipped
    return {"evidence": [_store(ctx, row) for row in inside], "query_id": result["query_id"],
            "skipped_outside_window": len(skipped)}


# Whole-store breadth (Albert, 4 October: answers must draw on far more than about 20 accounts). Code counts every
# stored post in the ask's market and window per platform, and per sound and hashtag, before the writer runs, so a
# claim's totals come from the whole store and the posts it cites are samples of them. Each count is one recorded query
# (query_id, result hash, K2 re-run). The market scope is search_posts': located in the market, or with no located
# market and seen in the market's own feeds or posted by a creator based there; located_* count the located posts only.
STORE_SOUNDS_PER_PLATFORM = 15
STORE_TAGS_PER_PLATFORM = 10
STORE_SOUND_SAMPLES = 2  # citable posts per sound, by engagement, beside the sound's earliest post
TIKTOK_MUSIC_URL = "https://www.tiktok.com/music/_-"  # TikTok reads the id after the last hyphen (Wikidata P9290)
# A post the writer can cite carries text and a link (checks.REQUIRED_FIELDS); samples are chosen from those.
_CITABLE = "(IFNULL(TRIM(p.text), '') != '' AND IFNULL(p.url, '') != '')"


def _store_window(ctx: RunContext) -> tuple[dt.date, dt.date]:
    return ((ctx.window_start, ctx.window_end) if ctx.window_start and ctx.window_end
            else resolve_dates("last 7 days", ctx.as_of))


def _store_scope(ctx: RunContext, platforms, window=None) -> tuple[str, str, dict, str]:
    """(FROM clause, WHERE clause, params, located test) for every stored post in the ask's market and window, or in
    window (since, until) when given."""
    since, until = window or _store_window(ctx)
    params = {"since": since, "until": until, "profile_source": PROFILE_SOURCE}
    where = ["p.post_date BETWEEN @since AND @until"]
    names = []
    for platform in platforms or ():
        for stored in dict.fromkeys((platform, STORED_PLATFORMS.get(platform, platform))):
            params[f"platform_{len(names)}"] = stored
            names.append(f"@platform_{len(names)}")
    if names:
        where.append(f"p.platform IN ({', '.join(names)})")
    if ctx.market:
        params["market"] = ctx.market
        where.append("(p.geo_market = @market OR ((p.geo_market IS NULL OR p.geo_source = @profile_source) "
                     "AND (c.home_market = @market OR @market IN UNNEST(o.scoped_markets))))")
        located = "(p.geo_market = @market AND IFNULL(p.geo_source, '') != @profile_source)"
    else:
        located = "(p.geo_market IS NOT NULL AND IFNULL(p.geo_source, '') != @profile_source)"
    joins = POSTS_JOIN + (_source_sightings_join("p", "since", "until", scoped=True) if ctx.market else "")
    return joins, " AND ".join(where), params, located


def _counts(located: str) -> str:
    return ("COUNT(DISTINCT p.post_id) AS posts, COUNT(DISTINCT p.creator_id) AS creators, "
            f"COUNT(DISTINCT IF({located}, p.post_id, NULL)) AS located_posts, "
            f"COUNT(DISTINCT IF({located}, p.creator_id, NULL)) AS located_creators")


def _scoped_counts(name: str) -> str:
    """The same four counts over a CTE that already holds post_id, creator_id and a located flag."""
    return (f"COUNT(DISTINCT {name}.post_id) AS posts, COUNT(DISTINCT {name}.creator_id) AS creators, "
            f"COUNT(DISTINCT IF({name}.located, {name}.post_id, NULL)) AS located_posts, "
            f"COUNT(DISTINCT IF({name}.located, {name}.creator_id, NULL)) AS located_creators")


def store_breadth(ctx: RunContext, warehouse: Warehouse, platforms=None) -> dict:
    """Three whole-store counts for the ask's market and window: per platform, per sound (title when 42 holds one,
    else the earliest creator 42 saw use it, the id and a TikTok music link) and per hashtag. A sound's and a hashtag's
    sample_post_ids are listed in the rows, so the gate fetches them as citable posts. Returns the query ids."""
    joins, where, params, located = _store_scope(ctx, platforms)
    shown = f"{MARKET_NAMES.get(ctx.market, 'every market')} inside the ask's window"
    out = {}

    totals = (f"SELECT p.platform, {_counts(located)} {joins}WHERE {where} "
              "GROUP BY p.platform ORDER BY posts DESC, p.platform")
    out["totals"] = sql_query(ctx, warehouse, totals, params=params,
                              purpose=f"Whole-store totals per platform: every stored post in {shown}")["query_id"]

    # A sound's earliest post in the window names it when 42 holds no title; its handle is listed so the writer can
    # say who used it first, and the post is fetched so that handle is a stored post's.
    sounds = (
        "WITH s AS (SELECT p.platform, p.sound_id, p.post_id, p.creator_id, "
        "COALESCE(c.handle, p.creator_id) AS handle, "
        f"{located} AS located, {_CITABLE} AS citable, "
        "ROW_NUMBER() OVER (PARTITION BY p.platform, p.sound_id "
        "ORDER BY COALESCE(p.published_at, TIMESTAMP(p.post_date)), p.post_id) AS first_rank, "
        f"ROW_NUMBER() OVER (PARTITION BY p.platform, p.sound_id, {_CITABLE} "
        "ORDER BY IFNULL(p.engagement, 0) DESC, p.post_id) AS top_rank "
        f"{joins}WHERE {where} AND IFNULL(p.sound_id, '') != '') "
        "SELECT s.platform, s.sound_id, ANY_VALUE(IF(m.label = s.sound_id, NULL, m.label)) AS sound_title, "
        f"{_scoped_counts('s')}, "
        "MAX(IF(s.first_rank = 1, s.handle, NULL)) AS earliest_creator, "
        "IF(s.platform = 'tiktok' AND REGEXP_CONTAINS(s.sound_id, r'^[0-9]+$'), "
        f"CONCAT('{TIKTOK_MUSIC_URL}', s.sound_id), NULL) AS sound_link, "
        "ARRAY_AGG(DISTINCT IF(s.first_rank = 1 OR (s.citable AND s.top_rank <= @samples), s.post_id, NULL) "
        "IGNORE NULLS) AS sample_post_ids "
        "FROM s LEFT JOIN intelligence_42_core.cultural_map m ON m.kind = 'sound' AND m.valid_to IS NULL "
        "AND m.canonical_key = CONCAT(s.platform, ':', s.sound_id) "
        "GROUP BY s.platform, s.sound_id "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY s.platform ORDER BY COUNT(DISTINCT s.creator_id) DESC, "
        "COUNT(DISTINCT s.post_id) DESC, s.sound_id) "
        "<= @per_platform "
        "ORDER BY creators DESC, posts DESC, s.platform, s.sound_id"
    )
    out["sounds"] = sql_query(
        ctx, warehouse, sounds, params={**params, "samples": STORE_SOUND_SAMPLES,
                                        "per_platform": STORE_SOUNDS_PER_PLATFORM},
        purpose=f"Whole-store sounds per platform, most creators first: every stored post with a sound in {shown}"
    )["query_id"]

    tags = (
        "WITH t AS (SELECT p.platform, LOWER(TRIM(tag)) AS hashtag, p.post_id, p.creator_id, "
        f"{located} AS located, {_CITABLE} AS citable, "
        f"ROW_NUMBER() OVER (PARTITION BY p.platform, LOWER(TRIM(tag)), {_CITABLE} "
        "ORDER BY IFNULL(p.engagement, 0) DESC, p.post_id) AS top_rank "
        f"{joins}CROSS JOIN UNNEST(p.hashtags) AS tag WHERE {where} AND IFNULL(TRIM(tag), '') != '') "
        f"SELECT t.platform, t.hashtag, {_scoped_counts('t')}, "
        "ARRAY_AGG(DISTINCT IF(t.citable AND t.top_rank = 1, t.post_id, NULL) IGNORE NULLS) AS sample_post_ids "
        "FROM t GROUP BY t.platform, t.hashtag "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY t.platform ORDER BY COUNT(DISTINCT t.creator_id) DESC, "
        "COUNT(DISTINCT t.post_id) DESC, t.hashtag) "
        "<= @per_platform "
        "ORDER BY creators DESC, posts DESC, t.platform, t.hashtag"
    )
    out["hashtags"] = sql_query(
        ctx, warehouse, tags, params={**params, "per_platform": STORE_TAGS_PER_PLATFORM},
        purpose=f"Whole-store hashtags per platform, most creators first: every stored post in {shown}"
    )["query_id"]
    before = _store_before(ctx, warehouse, platforms, shown, out)
    if before:
        out["before"] = before
    for qid in out.values():
        ctx.queries[qid]["tool"] = STORE_TOOL
    return out


def _store_before(ctx: RunContext, warehouse: Warehouse, platforms, shown: str, out: dict) -> str | None:
    """The sounds and hashtags the whole-store rows list, counted again over the same number of days just before the
    window, so a list answer can say how each compares with the period before (one row per item that had posts then;
    an item with none has no row). None when the window rows list nothing or the read fails: the window counts stand
    without it."""
    keys = {"sound": [], "hashtag": []}
    for kind, qid, field in (("sound", out.get("sounds"), "sound_id"), ("hashtag", out.get("hashtags"), "hashtag")):
        for row in (ctx.queries.get(qid) or {}).get("rows") or []:
            if row.get("platform") and row.get(field):
                keys[kind].append(f"{row['platform']}:{row[field]}".replace("|", ""))
    if not keys["sound"] and not keys["hashtag"]:
        return None
    since, until = _store_window(ctx)
    days = (until - since).days + 1
    joins, where, params, located = _store_scope(ctx, platforms, (since - dt.timedelta(days=days),
                                                                  since - dt.timedelta(days=1)))
    sql = (
        "WITH b AS (SELECT 'sound' AS kind, p.platform, p.sound_id, CAST(NULL AS STRING) AS hashtag, p.post_id, "
        f"p.creator_id, {located} AS located {joins}WHERE {where} "
        "AND STRPOS(@sound_keys, CONCAT('|', p.platform, ':', p.sound_id, '|')) > 0 "
        "UNION ALL SELECT 'hashtag' AS kind, p.platform, CAST(NULL AS STRING) AS sound_id, "
        "LOWER(TRIM(tag)) AS hashtag, "
        f"p.post_id, p.creator_id, {located} AS located {joins}CROSS JOIN UNNEST(p.hashtags) AS tag WHERE {where} "
        "AND STRPOS(@tag_keys, CONCAT('|', p.platform, ':', LOWER(TRIM(tag)), '|')) > 0) "
        f"SELECT b.kind, b.platform, b.sound_id, b.hashtag, {_scoped_counts('b')} FROM b "
        "GROUP BY b.kind, b.platform, b.sound_id, b.hashtag "
        "ORDER BY b.kind, creators DESC, posts DESC, b.platform, b.sound_id, b.hashtag"
    )
    params = {**params, "sound_keys": "|" + "|".join(keys["sound"]) + "|",
              "tag_keys": "|" + "|".join(keys["hashtag"]) + "|"}
    try:
        return sql_query(ctx, warehouse, sql, params=params,
                         purpose=("Whole-store sounds and hashtags before the window: the sounds and hashtags above, "
                                  f"every stored post in the same number of days just before the window, "
                                  f"{MARKET_NAMES.get(ctx.market, 'every market')}"))["query_id"]
    except Exception as exc:
        log.warning("ask store counts before the window failed: run %s: %s", ctx.run_id, type(exc).__name__)
        return None


# Topic sweep (Albert, 10 October: Ask read 169 posts and rested on 3). Once a question names a topic, code runs up to
# four recorded searches beside the whole-store counts: the topic and its tag family by engagement, the tag column by
# recency, and two wordings of it for the semantic leg. The posts are balanced across platforms and creators and stored
# straight into the run's evidence, so the research model's history does not carry them (about 15,000 tokens per 100
# posts, re-sent every turn). Each search is a recorded query, so a K2 re-run of any cited one works as for any query.
SWEEP_KEEP = 300  # posts a sweep stores, as many as the writer's pack holds (writer.MAX_PACK_POSTS)
MAX_SWEEP_TOPIC = 3
_HASHTAG = re.compile(r"(?<![\w#])#(\w{2,50})")
_QUOTED = re.compile(r'"([^"]{3,60})"')


def sweep_topic(question: str) -> list[str]:
    """What a question names as its topic, for code to search: its hashtags (as #tag, lower case) and any phrase in
    double quotes, at most MAX_SWEEP_TOPIC. A question that names neither has no topic code can read, and no sweep."""
    topic = []
    for m in _HASHTAG.finditer(question or ""):
        tag = "#" + m.group(1).lower()
        if tag not in topic:
            topic.append(tag)
    for m in _QUOTED.finditer(question or ""):
        words = [w.lstrip("#") for w in m.group(1).split() if w not in _OR and w not in _AND]
        phrase = " ".join(w for w in words if w)
        if phrase and phrase not in topic:
            topic.append(phrase)
    return topic[:MAX_SWEEP_TOPIC]


def topic_sweep(ctx: RunContext, warehouse: Warehouse, topic: list[str], platforms=None) -> dict:
    """The sweep for topic (hashtags and phrases from sweep_topic). Stores up to SWEEP_KEEP posts in ctx.evidence and
    returns counts only: posts stored, searches that ran, the searches that failed with their reasons, and the tags
    added to the family. A search that fails leaves the others; the sweep never raises for a warehouse failure."""
    topic = [t for t in topic if isinstance(t, str) and t.strip()][:MAX_SWEEP_TOPIC]
    out = {"posts": 0, "searches": 0, "failed": {}, "family": []}
    if not topic:
        return out
    since, until = _store_window(ctx)
    window = (since, until)
    filters, shared = _search_filters(platforms, None, 0)
    words = " OR ".join(topic)
    base = " ".join(t.lstrip("#") for t in topic)
    curated, tags = _widen(ctx, warehouse, words, since, until, platforms)
    out["family"] = curated + tags
    family_query = words + "".join(f" OR #{w}" for w in curated)
    tags_only = all(t.startswith("#") for t in topic)
    market = MARKET_NAMES.get(ctx.market, "Africa")
    searches = [
        ("keyword", "keyword", lambda: _keyword_sql(ctx, family_query, since, until, shared, filters, "engagement",
                                                     MAX_POSTS, tag_terms=tags)),
        ("tag column" if tags_only else "recent", "keyword",
         lambda: _keyword_sql(ctx, family_query, since, until, shared, filters, "recent", MAX_POSTS,
                              test=TAG_TEST if tags_only else TERM_TEST, tag_terms=tags)),
        ("semantic a", "semantic", lambda: _semantic_sql(ctx, base, since, until, shared, filters, MAX_POSTS)),
        ("semantic b", "semantic", lambda: _semantic_sql(ctx, f"people posting about {base}, {market}", since, until,
                                                         shared, filters, MAX_POSTS)),
    ]
    lists, query_ids = [], []
    for label, mode, build in searches:
        try:
            sql, params = build()
            result = sql_query(ctx, warehouse, sql, params=params, purpose=f"Topic sweep, {label}: {base}")
        except Exception as e:
            _emit_retrieval_trace(ctx, mode, error=e)
            out["failed"][label] = _reason(e)
            log.warning("ask topic sweep %s failed: run %s: %s", label, ctx.run_id, out["failed"][label])
            continue
        _emit_retrieval_trace(ctx, mode, query_id=result["query_id"], rows=result["rows"])
        ctx.queries[result["query_id"]]["tool"] = SWEEP_TOOL
        lists.append(result["rows"])
        query_ids.append(result["query_id"])
    out["searches"] = len(lists)

    inside = [[r for r in rows if _inside(r, window)] for rows in lists]
    fused = _fuse(inside, 10**6, ctx.market)
    best = {id(row): len(fused) - i for i, row in enumerate(fused)}
    chosen = spread_order(fused, platform=lambda r: r.get("platform"),
                          creator=lambda r: r.get("creator_id") or r.get("handle"),
                          strength=lambda r: best[id(r)])[:SWEEP_KEEP]
    for row in chosen:
        _store(ctx, row)
    kept = {str(r["post_id"]) for r in chosen}
    for qid, rows in zip(query_ids, lists):
        ids = [str(r["post_id"]) for r in rows]
        ctx.queries[qid]["skipped_outside_window"] = sum(1 for r in rows if not _inside(r, window))
        ctx.queries[qid]["skipped_ids"] = [pid for pid in dict.fromkeys(ids) if pid not in kept]
    out["posts"] = len(chosen)
    return out


def store_totals(ctx: RunContext, query_id: str | None) -> dict | None:
    """The run's whole-store totals for the Ask meta line, summed from the totals query's rows: posts and creators
    across platforms (a creator on two platforms counts on each), the platforms with posts, and the query_id. None
    when there is no totals query or it found nothing."""
    query = ctx.queries.get(query_id) if query_id else None
    rows = [r for r in (query or {}).get("rows") or [] if isinstance(r, dict) and (r.get("posts") or 0) > 0]
    if not rows:
        return None
    total = {k: sum(int(r.get(k) or 0) for r in rows) for k in ("posts", "creators", "located_posts",
                                                                 "located_creators")}
    return {**total, "platforms": len({PLATFORM_NAMES.get(r.get("platform"), r.get("platform")) for r in rows}),
            "query_id": query_id}


def _fallback_cards(rows, card_count: int) -> list[dict]:
    if len(rows) != card_count:
        raise ValueError("brief card count did not match its cards")
    cards = []
    for row in rows:
        item_id, title = row.get("item_id"), row.get("title")
        if (row.get("item_id_type") != "string" or not isinstance(item_id, str) or not item_id.strip()
                or row.get("title_type") != "string" or not isinstance(title, str) or not title.strip()):
            raise ValueError("brief card item_id and title must be non-empty strings")
        cards.append({key: row.get(key) for key in ("item_id", "title", "rank", "kind", "state")})
    return cards


def get_trending_fallback_snapshot(ctx: RunContext, warehouse: Warehouse, market: str) -> dict:
    """Read bounded Today-card, located-post and prior-card context for one market without promoting it to evidence."""
    today = resolve_dates("today", ctx.as_of)[1]
    start, end = resolve_dates("last 7 days", ctx.as_of)
    result = {
        "complete": False,
        "error": None,
        "market": market,
        "as_of": today,
        "window": {"from": start, "to": end},
        "today": {"complete": False, "available": None, "brief_date": None, "run_id": None,
                  "published_at": None, "status": None, "card_count": None, "query_id": None},
        "located_posts": {"complete": False, "post_ids": [], "limit": MAX_POSTS, "limit_reached": False,
                           "query_id": None},
        "latest_brief": {"complete": False, "brief": None, "query_id": None},
    }

    def fail(stage, exc):
        result["error"] = {"stage": stage, "category": retrieval_refusal_category(exc),
                           "type": type(exc).__name__}
        return result

    if market not in MARKETS:
        return fail("today", Refused("market must be ZA, NG or KE"))

    today_sql = (
        "WITH ranked AS (SELECT b.brief_date, b.market, b.run_id, b.published_at, b.status, "
        "ARRAY_LENGTH(JSON_QUERY_ARRAY(b.payload, '$.cards')) AS card_count, "
        "JSON_TYPE(JSON_QUERY(b.payload, '$.cards')) AS cards_type, "
        "(SELECT COUNTIF(JSON_TYPE(JSON_QUERY(card, '$.item_id')) = 'string' "
        "AND NULLIF(TRIM(JSON_VALUE(card, '$.item_id')), '') IS NOT NULL "
        "AND JSON_TYPE(JSON_QUERY(card, '$.title')) = 'string' "
        "AND NULLIF(TRIM(JSON_VALUE(card, '$.title')), '') IS NOT NULL) "
        "FROM UNNEST(JSON_QUERY_ARRAY(b.payload, '$.cards')) AS card) AS valid_card_count, "
        "ROW_NUMBER() OVER (PARTITION BY b.brief_date, b.market "
        "ORDER BY b.published_at DESC, b.run_id DESC) AS brief_rank "
        "FROM intelligence_42_agent.v_briefs_current b "
        "WHERE b.brief_date = @today AND b.market = @market) "
        "SELECT brief_date, market, run_id, published_at, status, card_count, cards_type, valid_card_count "
        "FROM ranked WHERE brief_rank = 1"
    )
    try:
        today_read = internal_read("get_trending_fallback_snapshot", ctx, warehouse, today_sql,
                                   purpose="trending_fallback: today's published cards",
                                   params={"today": today, "market": market})
        if today_read["truncated"] or len(today_read["rows"]) > 1:
            raise ValueError("today brief read was incomplete")
        rows = today_read["rows"]
        if rows:
            row = rows[0]
            if row.get("market") != market or _day(row.get("brief_date"), "brief_date") != today:
                raise ValueError("today brief read returned a different market or date")
            status = row.get("status")
            result["today"].update(status=status, brief_date=today, run_id=row.get("run_id"),
                                   published_at=row.get("published_at"))
            if status not in {"published", "partial", "data_issue"}:
                raise ValueError("today brief status is unknown")
            cards_type, card_count = row.get("cards_type"), row.get("card_count")
            valid_card_count = row.get("valid_card_count")
            if cards_type != "array" or card_count is None or valid_card_count is None:
                raise ValueError("today brief cards have an invalid shape")
            card_count, valid_card_count = int(card_count), int(valid_card_count)
            if card_count < 0 or valid_card_count != card_count:
                raise ValueError("today brief cards have an invalid count")
            if status == "data_issue" and card_count:
                raise ValueError("data-issue brief contains cards")
            result["today"].update(
                available=status in {"published", "partial"} and card_count > 0,
                card_count=card_count,
            )
        else:
            result["today"]["available"] = False
            result["today"]["card_count"] = 0
        result["today"].update(complete=True, query_id=today_read["query_id"])
    except Exception as exc:
        return fail("today", exc)

    if result["today"]["available"]:
        result["complete"] = True
        return result

    posts_sql = (
        "SELECT p.post_id, p.published_at, p.post_date, p.geo_market, p.geo_source "
        "FROM intelligence_42_core.posts p "
        "WHERE p.geo_market = @market AND COALESCE(p.geo_source, '') != @profile_source "
        "AND COALESCE(DATE(p.published_at, 'Africa/Johannesburg'), p.post_date) BETWEEN @since AND @until "
        "ORDER BY COALESCE(DATE(p.published_at, 'Africa/Johannesburg'), p.post_date) DESC, p.post_id "
        "LIMIT @limit"
    )
    try:
        posts_read = sql_query(
            ctx, warehouse, posts_sql, purpose="trending_fallback: located posts in the last 7 days",
            params={"market": market, "profile_source": PROFILE_SOURCE, "since": start, "until": end,
                    "limit": MAX_POSTS},
        )
        if posts_read["truncated"]:
            raise ValueError("located post read was truncated")
        found = {}
        for row in posts_read["rows"]:
            pid = _field(row, "post_id")
            day = _posted_day(row)
            if (not pid or _field(row, "geo_market") != market or not located(row)
                    or day is None or not start <= day <= end):
                continue
            found.setdefault(str(pid), {"post_id": str(pid), "posted_at": day})
        post_ids = list(found.values())[:MAX_POSTS]
        result["located_posts"].update(
            complete=True,
            post_ids=post_ids,
            limit_reached=len(post_ids) == MAX_POSTS,
            query_id=posts_read["query_id"],
        )
    except Exception as exc:
        return fail("located_posts", exc)

    latest_sql = (
        "WITH candidates AS (SELECT b.brief_date, b.market, b.run_id, b.published_at, b.status, "
        "ARRAY_LENGTH(JSON_QUERY_ARRAY(b.payload, '$.cards')) AS card_count, "
        "JSON_TYPE(JSON_QUERY(b.payload, '$.cards')) AS cards_type, "
        "JSON_QUERY_ARRAY(b.payload, '$.cards') AS card_values "
        "FROM intelligence_42_agent.v_briefs_current b "
        "WHERE b.market = @market AND b.status IN ('published', 'partial') AND b.brief_date <= @as_of), "
        "chosen AS (SELECT * FROM candidates "
        "WHERE cards_type IS NULL OR cards_type != 'array' OR card_count IS NULL OR card_count > 0 "
        "ORDER BY brief_date DESC, published_at DESC, run_id DESC LIMIT 1) "
        "SELECT chosen.brief_date, chosen.market, chosen.run_id, chosen.published_at, chosen.status, "
        "chosen.card_count, chosen.cards_type, JSON_VALUE(card, '$.item_id') AS item_id, "
        "JSON_TYPE(JSON_QUERY(card, '$.item_id')) AS item_id_type, "
        "JSON_VALUE(card, '$.title') AS title, JSON_TYPE(JSON_QUERY(card, '$.title')) AS title_type, "
        "JSON_VALUE(card, '$.rank') AS rank, JSON_VALUE(card, '$.kind') AS kind, "
        "JSON_VALUE(card, '$.state') AS state, card_offset "
        "FROM chosen LEFT JOIN UNNEST(chosen.card_values) AS card WITH OFFSET AS card_offset ON TRUE "
        "ORDER BY card_offset"
    )
    try:
        latest_read = internal_read("get_trending_fallback_snapshot", ctx, warehouse, latest_sql,
                                    purpose="trending_fallback: latest published brief with cards",
                                    params={"market": market, "as_of": today})
        if latest_read["truncated"]:
            raise ValueError("latest brief read was incomplete")
        rows = latest_read["rows"]
        if rows:
            row = rows[0]
            if row.get("market") != market or row.get("status") not in {"published", "partial"}:
                raise ValueError("latest brief read returned an invalid market or status")
            brief_date = _day(row.get("brief_date"), "brief_date")
            if brief_date > today or row.get("cards_type") != "array":
                raise ValueError("latest brief cards have an invalid shape")
            card_count = int(row.get("card_count"))
            if any((other.get("brief_date"), other.get("market"), other.get("run_id"), other.get("status"),
                    other.get("card_count"), other.get("cards_type")) !=
                   (row.get("brief_date"), row.get("market"), row.get("run_id"), row.get("status"),
                    row.get("card_count"), row.get("cards_type")) for other in rows):
                raise ValueError("latest brief card rows do not share one brief")
            cards = _fallback_cards(rows, card_count)
            if not cards:
                raise ValueError("latest brief candidate has no cards")
            result["latest_brief"]["brief"] = {
                "brief_date": brief_date,
                "run_id": row.get("run_id"),
                "status": row.get("status"),
                "published_at": row.get("published_at"),
                "card_count": card_count,
                "cards": cards,
            }
        result["latest_brief"].update(complete=True, query_id=latest_read["query_id"])
    except Exception as exc:
        return fail("latest_brief", exc)

    result["complete"] = True
    return result


def _fuse(lists: list[list[dict]], limit: int, ask_market=None) -> list[dict]:
    """Reciprocal rank fusion, equal weights, 1 / (60 + rank) per list (AGENT.md, Ranking and dedup). One row per
    post_id, the first list's row winning; at most three posts per author; cut to limit. Ties keep first sight."""
    scores, rows = {}, {}
    for ranked in lists:
        for rank, row in enumerate(ranked, start=1):
            pid = str(row["post_id"])
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
            rows.setdefault(pid, row)
    kept, per_author = [], {}
    def group(pid):
        return 0 if located(rows[pid]) else 1 if source_market(rows[pid], ask_market) else 2

    for pid in sorted(scores, key=lambda p: (group(p), -scores[p])):
        row = rows[pid]
        who = row.get("creator_id") or row.get("handle")
        if who:
            if per_author.get(who, 0) >= MAX_PER_AUTHOR:
                continue
            per_author[who] = per_author.get(who, 0) + 1
        kept.append(row)
        if len(kept) == limit:
            break
    return kept


def rising_topics(ctx: RunContext, warehouse: Warehouse, date=None, window_days=7, market=None, kind=None,
                  min_platforms=1) -> dict:
    """Latest state per item and market inside the window, from v_items_today (v_item_state_current plus label, as_of)."""
    window_days = int(window_days)
    if window_days < 1:
        raise Refused("window_days must be at least 1.")
    end = resolve_dates("today", ctx.as_of)[1] if date is None else _day(date, "date")
    params = {"date": end, "since": end - dt.timedelta(days=window_days - 1)}
    where = ["t.metric_date BETWEEN @since AND @date"]
    market = market or ctx.market
    if market:
        params["market"] = market
        where.append("t.market = @market")
    if kind:
        params["kind"] = kind
        where.append("t.kind = @kind")
    if int(min_platforms) > 1:
        params["min_platforms"] = int(min_platforms)
        # found_platforms is NULL until an item beats the placebo base rate, so NULL counts as one platform.
        where.append("IFNULL(t.found_platforms, 1) >= @min_platforms")

    sql = (
        "SELECT t.metric_date, t.market, t.item_id, t.label, t.kind, t.state, t.untested, t.worth_pct, "
        "t.creators3, t.posts3, t.spread_platforms, t.found_platforms, t.markets_hot, t.lead_market, "
        "t.novelty, t.diffusion, t.authenticity, t.share_flags, t.geo_status, t.moment, t.run_id, t.as_of "
        "FROM intelligence_42_agent.v_items_today t "
        f"WHERE {' AND '.join(where)} "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY t.market, t.item_id ORDER BY t.metric_date DESC) = 1 "
        "ORDER BY t.worth_pct DESC NULLS LAST, t.creators3 DESC"
    )
    result = sql_query(ctx, warehouse, sql, purpose="rising_topics", params=params)
    query = ctx.queries[result["query_id"]]
    query["tool"] = "rising_topics"  # save_finding trusts item ids from marked records only
    rows = result["rows"][:MAX_RISING_TOPIC_ROWS]
    return {"rows": rows,
            "truncated": bool(result["truncated"] or len(result["rows"]) > MAX_RISING_TOPIC_ROWS),
            "query_id": result["query_id"], "result_hash": result["result_hash"]}


def recall_findings(ctx: RunContext, warehouse: Warehouse, query: str, since=None, status="current") -> dict:
    """The newest row per finding as of this run (a status change is a new row, never an update). Its status is
    contradicted when that row says so (save_finding writes it), stale once its review date has passed at the run's
    as_of (read here, never written), and current otherwise."""
    params = {"as_of": ctx.as_of.isoformat()}
    where = [_match(_terms(query, params),
                    "(CONTAINS_SUBSTR(r.question, {0}) OR CONTAINS_SUBSTR(r.answer, {0}) "
                    "OR EXISTS (SELECT 1 FROM UNNEST(r.claims) cl WHERE CONTAINS_SUBSTR(cl.text, {0})))")]
    if status is not None:
        if status not in FINDING_STATUSES:
            raise Refused(f"status must be one of {', '.join(FINDING_STATUSES)} or None; got {status!r}.")
        params["status"] = status
        where.append("r.status = @status")
    if since is not None:
        params["since"] = _day(since, "since")
        where.append("DATE(r.as_of) >= @since")

    sql = (
        "SELECT r.finding_id, r.question, r.answer, r.as_of, r.claims, r.valid_from, r.valid_to, r.status FROM ("
        "SELECT f.finding_id, f.question, f.answer, f.as_of, f.claims, f.valid_from, f.valid_to, "
        "CASE WHEN f.status = 'contradicted' THEN 'contradicted' "
        "WHEN f.status = 'stale' OR f.valid_to <= TIMESTAMP(@as_of) THEN 'stale' ELSE 'current' END AS status "
        "FROM intelligence_42_agent.v_prior_findings f "
        "WHERE f.valid_from IS NULL OR f.valid_from <= TIMESTAMP(@as_of) "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY f.finding_id "
        "ORDER BY f.valid_from DESC, IF(f.status = 'contradicted', 0, 1)) = 1) r "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY r.as_of DESC LIMIT 50"
    )
    result = internal_read("recall_findings", ctx, warehouse, sql, purpose=f"recall_findings: {query}", params=params)
    # Marked here, where the model cannot write: save_finding reads contradicts only from records marked so.
    ctx.queries[result["query_id"]]["tool"] = "recall_findings"
    return {"rows": result["rows"], "query_id": result["query_id"]}


class FindingsWriter(Protocol):
    def insert(self, table: str, rows: list[dict], row_ids: list[str] | None = None) -> None: ...


class BigQueryTableWriter:
    """Streaming insert into intelligence_42_agent (findings, claim_checks). Append only. The client is built on
    first use. row_ids, when given, let BigQuery drop a retried row instead of landing it twice."""

    def __init__(self, project: str = PROJECT):
        self.project = project
        self._client = None

    def insert(self, table: str, rows: list[dict], row_ids: list[str] | None = None) -> None:
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project)
        errors = self._client.insert_rows_json(f"{self.project}.{table}", rows, row_ids=row_ids)
        if errors:
            raise RuntimeError(f"Insert into {table} failed: {errors}")


BigQueryFindingsWriter = BigQueryTableWriter


def _row_post_ids(ctx: RunContext) -> set[str]:
    """Every post id a recorded query in this run returned, in any column named like post_id."""
    return {str(pid) for q in list(ctx.queries.values()) for row in q.get("rows") or [] if isinstance(row, dict)
            for column, value in row.items() if "post_id" in str(column).lower()
            for pid in (value if isinstance(value, (list, tuple)) else [value]) if isinstance(pid, str) and pid}


def save_finding(ctx: RunContext, writer: FindingsWriter, claim, evidence_ids, label, topic, query_ids,
                 review_by, item_ids=None, contradicts=None, warehouse: Warehouse | None = None) -> dict:
    """Hold one current finding until the answer's trust gate has run (commit_findings writes it); nothing is written
    here. item_ids must each have come back in a row of a query an item tool ran itself this run (its record marked
    by the tool, never by the model). Each finding in contradicts must have come back from recall_findings this run
    and share an item with this claim; it gets a new row with status contradicted, written with the finding.
    writer is the tool binding's and goes unused here."""
    if not evidence_ids:
        raise Refused("A finding needs at least one evidence id.")
    if isinstance(label, str):  # the skill writes labels as "Observed" and "Single source"; the row keeps the code
        label = re.sub(r"[\s-]+", "_", label.strip()).lower()
    if label not in LABELS:
        raise Refused(f"label must be one of {', '.join(LABELS)}; got {label!r}.")
    unknown = [e for e in evidence_ids if e not in ctx.evidence]
    # Live staging, 4 October: the researcher cited posts it had only seen in query rows, which become evidence at
    # the writer's gate, so saving failed. Those posts are stored now, as the gate would store them, inside the window.
    if (unknown and warehouse is not None and ctx.window_start and ctx.window_end
            and all(str(e) in _row_post_ids(ctx) for e in unknown)):
        fetch_posts(ctx, warehouse, unknown, (ctx.window_start, ctx.window_end))
        unknown = [e for e in evidence_ids if e not in ctx.evidence]
    if unknown:
        raise Refused(f"Evidence ids not seen in this run: {', '.join(map(str, unknown))}.")
    query_ids = list(query_ids or [])
    missing = [q for q in query_ids if q not in ctx.queries]
    if missing:
        raise Refused(f"Query ids not run in this run: {', '.join(map(str, missing))}.")
    item_ids = list(dict.fromkeys(str(i) for i in item_ids or []))
    item_tools = ("history", "analogues", "rising_topics")
    seen_items = {str(row["item_id"]) for q in ctx.queries.values() if q.get("tool") in item_tools
                  for row in q["rows"] if isinstance(row, dict) and row.get("item_id")}
    unseen = [i for i in item_ids if i not in seen_items]
    if unseen:
        raise Refused(f"Item ids no item tool returned in this run: {', '.join(unseen)}. Cite items that history, "
                      f"analogues or rising_topics returned, or save with item_ids [].")
    recalled = {}  # finding_id -> the newest row recall_findings returned for it in this run
    for q in ctx.queries.values():
        if q.get("tool") == "recall_findings":
            recalled.update({row["finding_id"]: row for row in q["rows"] if isinstance(row, dict)
                             and row.get("finding_id")})
    old_rows = []
    for old_id in dict.fromkeys(contradicts or []):
        old = recalled.get(old_id)
        if old is None:
            raise Refused(f"Finding {old_id} was not returned by recall_findings in this run.")
        old_items = {i for c in old.get("claims") or [] for i in c.get("item_ids") or []}
        if not old_items & set(item_ids):
            raise Refused(f"Finding {old_id} is not about the same item as this claim's item_ids, so it cannot be "
                          f"marked contradicted.")
        old_rows.append(old)

    post_ids = sorted(set(evidence_ids))
    key = json.dumps([ctx.run_id, claim, post_ids], ensure_ascii=False)
    finding_id = "f_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    run_ids = [ctx.run_id]
    for q in query_ids:
        for row in ctx.queries[q]["rows"]:
            rid = row.get("run_id") if isinstance(row, dict) else None
            if rid and rid not in run_ids:
                run_ids.append(rid)
    if review_by is None:
        valid_to = None
    elif isinstance(review_by, dt.datetime):
        valid_to = review_by.isoformat()
    else:
        valid_to = f"{_day(review_by, 'review_by').isoformat()}T00:00:00"

    row = {
        "finding_id": finding_id,
        "question": topic,
        "answer": claim,
        "as_of": ctx.as_of.isoformat(),
        "claims": [{
            "text": claim,
            "label": label,
            "item_ids": item_ids,
            "evidence_post_ids": post_ids,
            "query_ids": query_ids,
            "run_ids": run_ids,
            "result_hashes": [ctx.queries[q]["result_hash"] for q in query_ids],
        }],
        "valid_from": ctx.as_of.isoformat(),
        "valid_to": valid_to,
        "status": "current",
    }
    rows, row_ids = [row], [finding_id]
    for old in old_rows:
        rows.append({
            "finding_id": old["finding_id"], "question": old.get("question"), "answer": old.get("answer"),
            "as_of": _iso(old.get("as_of")), "claims": old.get("claims") or [],
            "valid_from": ctx.as_of.isoformat(), "valid_to": _iso(old.get("valid_to")), "status": "contradicted",
        })
        row_ids.append(f"{old['finding_id']}:contradicted:{finding_id}")
    with ctx.lock:  # researchers running at once share the list (RunContext.lane)
        ctx.pending_findings.append({"rows": rows, "row_ids": row_ids})
    ctx.emit("save_finding", finding_id=finding_id)
    out = {"finding_id": finding_id}
    if old_rows:
        out["contradicted"] = [old["finding_id"] for old in old_rows]
    return out


def _claim_words(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def commit_findings(ctx: RunContext, writer: FindingsWriter, claims: list[dict]) -> list[str]:
    """Write the findings save_finding held, once the answer's trust gate has run. claims are the claims that survived
    it. A finding is written only when a surviving claim has its words, and then as that claim was checked: its cited
    posts that are evidence in this run, and the lower of the two labels. Its contradicted rows go in the same
    insert; a finding not written marks nothing contradicted. Every held finding is let go. Returns the ids written."""
    passed = {}
    for claim in claims or []:
        if isinstance(claim, dict):
            passed.setdefault(_claim_words(claim.get("text")), claim)
    with ctx.lock:
        held, ctx.pending_findings[:] = list(ctx.pending_findings), []
    written = []
    for pending in held:
        row, *old_rows = pending["rows"]
        saved = row["claims"][0]
        checked = passed.get(_claim_words(saved["text"]))
        if checked is None or row["finding_id"] in written or checked.get("label") not in LABELS:
            continue
        post_ids = sorted({e for e in checked.get("evidence_ids") or [] if isinstance(e, str) and e in ctx.evidence})
        if not post_ids:
            continue
        label = min(saved["label"], checked["label"], key=lambda name: LABEL_RANK[name])
        row = {**row, "claims": [{**saved, "label": label, "evidence_post_ids": post_ids}]}
        writer.insert(FINDINGS_TABLE, [row, *old_rows], row_ids=pending["row_ids"])
        written.append(row["finding_id"])
    return written
