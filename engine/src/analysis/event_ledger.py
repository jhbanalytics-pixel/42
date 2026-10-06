"""Event-state ledger builder for the PULSE Intelligence Core.

Reads today's FACTUAL enriched_content rows per market (GDELT GKG entities,
RSS headlines, BigQuery Trends search-velocity spikes), clusters them by a
normalized entity_key, resolves each event's current state, and persists one
row per (trend_date, market, entity_key) into the event_ledger table.

State resolution runs in two layers:

- A deterministic headline-pattern guess (won / beat / results -> resolved,
  vs / fixture / upcoming -> scheduled, else unknown). This is the fallback
  that ships when Gemini is skipped or fails.
- A single Gemini pass per market over the clusters. Anti-hallucination is
  enforced IN PYTHON, not the model: a returned state must cite a supplied
  evidence row index or it is dropped, and a 'resolved' label with no citing
  row falls back to the deterministic guess. resolved_by records which path
  produced the row. confidence is always computed in Python, never by the
  model.

The module is NOT wired into the cron. The caller decides when to invoke it,
flag-gated. build_and_persist_ledger wraps the whole run in try/except and
returns {} on failure so it can never break a pipeline that calls it.

TODO(validity_window / supersession): a ledger row is currently a fresh
snapshot per trend_date with no link back to yesterday's row for the same
entity_key. reconcile's freshness check (_event_is_fresher, a fixed
trailing-window date compare) is a workaround, not a real validity window.
The full design (an explicit valid_from/valid_until per state, with a newer
'resolved' row explicitly superseding an older 'scheduled' row for the same
entity) is out of scope for this pass; see docs/trends-engine-v3-blueprint.md
for where that belongs when it is picked up.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any

from google.cloud import bigquery as bq

from src.analysis.gemini_client import GeminiClient
from src.analysis.prompts.event_ledger import (
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    build_event_ledger_prompt,
)
from src.utils.bigquery import get_client, get_dataset, insert_dataframe
from src.utils.config_loader import load_entity_aliases
from src.utils.gemini_usage import (
    UsageSink,
    persist_gemini_usage,
    record_usage,
    usage_rows,
)
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

MARKETS = ("za", "ng", "ke")

# The reconcile ledger runs one Gemini resolution call per market per day and
# renders nothing (Intelligence Core shadow mode). It does not need the premium
# brief model, so it runs on gemini-2.5-flash by default (about 5x cheaper on
# input, 3.6x on output than gemini-3.5-flash), which keeps the shadow cost
# under the activation runbook's per-month ceiling while the promotion window
# accrues. The never-invent trust guard (drop any state whose citation does not
# point at its own evidence) is pure Python and model-agnostic, so a cheaper
# model only affects resolution recall, not the anti-hallucination boundary.
# gemini-2.5-flash is served on the global endpoint, so it works with the same
# GEMINI_LOCATION=global the cron sets for the 3.x briefs. Override with
# RECONCILE_GEMINI_MODEL to pin a different model (e.g. match the brief model).
RECONCILE_GEMINI_MODEL = os.environ.get("RECONCILE_GEMINI_MODEL") or "gemini-2.5-flash"

# This stage's name in the shared gemini_usage ledger. The reconcile shadow's
# spend used to be ESTIMATED by the cost watchdog from a hardcoded per-call
# token guess, because event_ledger records gemini_model but no token columns.
# It is measured now, off the response, per market. It resolves on a cheaper
# model than the briefs, so the model column is what keeps it priced right.
USAGE_CONSUMER = "reconcile"

# Channel families. enriched_content.source / content_type map to one of these
# so the noise floor (>=2 distinct families) measures real cross-channel
# corroboration, not two rows from the same feed.
_GDELT_CONTENT_TYPE = "gdelt_gkg"
_RSS_SOURCE = "rss"
# Live enriched_content RSS rows carry the outlet name as source and
# content_type='article'; the bare source='rss' shape is kept for synthetic
# rows and any legacy data.
_RSS_CONTENT_TYPE = "article"
_TRENDS_SOURCE = "bigquery_trends"
_RSS_CONTENT_TYPES = frozenset({"article"})
# trending_search rows come from the google_trends_rss connector: the only
# search-trend source with KE coverage (the BigQuery public trends dataset
# has no Kenya rows, re-probed 13 Jul 2026).
_TRENDS_CONTENT_TYPES = frozenset({"search_term", "top_term", "trending_search"})
# YouTube search-result videos (content_type 'video', or 'video/{categoryId}'
# once the comments-enrichment pass tags a category) and Apple Music chart
# entries are their own factual families: a chart placement or a video title
# is independent corroboration from the GDELT/RSS/trends news channels.
_YOUTUBE_CONTENT_TYPE = "video"
_APPLE_MUSIC_SOURCE = "apple_music"
_APPLE_MUSIC_CONTENT_TYPE = "chart_track"

# A search-velocity spike alone clears the noise floor (a real surge of search
# interest is corroboration even from a single channel).
_SEARCH_SPIKE_THRESHOLD = 0.5

# Deterministic state patterns. Word-boundary matched against the lowercased
# headline / entity string. Sport-fixture verbs (thrash, take on, kick off)
# widen coverage beyond generic news language so a Springboks-vs-England-style
# headline resolves without needing Gemini.
_RESOLVED_PATTERNS = re.compile(
    r"\b(win|wins|won|beat|beats|defeated|defeats|results?|elected|"
    r"announced|announces|released|releases|crowned|claimed|thrash(?:ed|es)?|"
    r"hammer(?:ed)?|triumph(?:ed|s)?|clinch(?:ed|es)?|romp(?:ed|s)?|"
    r"told|endorses?|endorsed|has called for|resign(?:ed|s)?|appoint(?:ed|s)?|"
    r"pass(?:ed|es)?|sign(?:ed|s)?|launch(?:ed|es)?|score(?:d|s)?|"
    r"sentenc(?:ed|es)?|convict(?:ed|s)?|confirm(?:ed|s)?|reject(?:ed|s)?|"
    r"approve(?:d|s)?|ban(?:ned|s)?|qualif(?:ied|ies|y)|eliminate(?:d|s)?|"
    r"draw(?:s|n)?|tie(?:d|s)?)\b"
)
_SCHEDULED_PATTERNS = re.compile(
    r"\b(vs\.?|versus|fixture|to face|to play|upcoming|scheduled|"
    r"set to|will face|preview|ahead of|kick[- ]?off|take on|takes on|"
    r"clash with|to address)\b"
)

# GDELT GKG rows with no article title synthesise `text` from theme codes
# (ALL_CAPS_WITH_UNDERSCORES, e.g. 'TAX_FNCACT_LEADER WB_1249_ECONOMIC_GROWTH')
# plus persons/orgs/source (see gdelt.py _normalise_row). A real headline
# essentially never contains a multi-segment ALL-CAPS underscore token, so one
# match is enough to flag the text as a taxonomy blob rather than prose.
_THEME_BLOB_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,}\b")

# Channel families whose candidates are headline-shaped (a real quote from a
# title field), as opposed to gdelt (entity names) or trends (search terms).
# Used to grant partial citation credit to a deterministic 'resolved' verdict
# that is grounded in an actual clean headline, not just a keyword hit.
_HEADLINE_FAMILIES = frozenset({"rss", "youtube", "music"})

# Phase 3: a small curated entity_key -> [alias, ...] map per market
# (configs/entity_aliases.yaml), so reconcile can anchor a claim that names an
# entity differently from how today's data happened to spell it (e.g.
# "Ramaphosa" when the ledger clustered under "cyril ramaphosa"). A single-
# word alias that is a generic role/institution term is dropped even if it
# slipped into the config: a bare "President" or "Team" alias would anchor
# almost any political/sport claim, defeating the anchor-match's specificity.
_GENERIC_ALIAS_BLOCKLIST = frozenset(
    {
        "president",
        "government",
        "team",
        "minister",
        "party",
        "news",
        "official",
        "national",
        "leader",
        "state",
    }
)

# Single-token entity keys that carry no event identity on their own. A ledger
# event keyed on one of these anchors almost any claim mentioning the word
# (observed live: '2026', 'young', 'netflix', 'facebook' as entity keys), so a
# candidate whose whole normalized key is such a token is dropped before
# clustering. Multi-word keys are unaffected.
_GENERIC_ENTITY_TOKENS = _GENERIC_ALIAS_BLOCKLIST | frozenset(
    {
        "young",
        "netflix",
        "facebook",
        "whatsapp",
        "tiktok",
        "twitter",
        "instagram",
        "youtube",
        "google",
        "gemini",
        "africa",
        "kenya",
        "nigeria",
        "nairobi",
        "lagos",
        "money",
        "video",
        "music",
        "trending",
        "breaking",
        "viral",
    }
    # Headline filler that survives proper-noun extraction because a headline
    # capitalises it mid-sentence ("Watch Live", "The Best Free Apps"). Each of
    # these was a real matched_entity_key over 2026-07-28..2026-08-03 ('live'
    # 14 actions, 'life' 12, 'what' 5, 'daily' 5, 'these' 4, 'free' 3,
    # 'digital' 3), anchoring any claim that used the word. A one-word key made
    # of English filler is never an event anchor.
    | frozenset(
        {
            "live",
            "life",
            "living",
            "what",
            "when",
            "where",
            "these",
            "those",
            "there",
            "here",
            "daily",
            "weekly",
            "today",
            "tomorrow",
            "week",
            "year",
            "free",
            "digital",
            "online",
            "watch",
            "full",
            "best",
            "more",
            "most",
            "first",
            "last",
            "real",
            "good",
            "great",
            "story",
            "people",
            "your",
            "they",
            "their",
            "about",
            "after",
            "before",
            "over",
            "into",
            "with",
            "from",
            "this",
            "that",
            "have",
            "been",
        }
    )
)


# Demonym / market self-reference tokens. Capitalized and frequent in both
# headlines and claims, so a phrase made ONLY of these (plus generic tokens)
# would bind almost any in-market claim ("Young Nigerians", "South Africa").
# A phrase carrying at least one real name token still passes.
_DEMONYM_TOKENS = frozenset(
    {
        "south",
        "africa",
        "africans",
        "african",
        "nigerians",
        "nigerian",
        "kenyans",
        "kenyan",
        "mzansi",
        "naija",
        "kanairo",
        "saharan",
        "gen",
        "z",
    }
)
_NON_ENTITY_TOKENS = _GENERIC_ENTITY_TOKENS | _DEMONYM_TOKENS

# A run of capitalized tokens ("Ellis Park", "Peter Obi"). \w covers accented
# letters under re.UNICODE; apostrophes and hyphens stay inside a token.
_CAPITALIZED_RUN = re.compile(
    "\\b[A-Z][\\w'’-]*(?:\\s+[A-Z][\\w'’-]*)*"  # noqa: RUF001 (curly apostrophe in names)
)

# Leading role titles are stripped from an extracted phrase so "President
# Cyril Ramaphosa" and GDELT's "Cyril Ramaphosa" converge on one entity_key.
# Without this the two anchors fragment one event into two clusters that each
# die at the noise floor.
_ROLE_PREFIX_TOKENS = frozenset(
    {
        "president",
        "deputy",
        "vice",
        "minister",
        "mp",
        "dr",
        "prof",
        "professor",
        "chief",
        "coach",
        "governor",
        "senator",
        "pastor",
        "reverend",
        "general",
        "captain",
        "king",
        "queen",
    }
)

# Max extracted name anchors per headline; first-seen order.
_MAX_NAMES_PER_HEADLINE = 5


def _proper_noun_phrases(text: str) -> list[str]:
    """Extract proper-noun phrases from a headline as anchor candidates.

    Deterministic, no NLP dep: contiguous runs of capitalized tokens. A
    single-token run at the very start of the text is sentence-initial
    capitalization noise and is dropped; a 2+ token run there is kept
    ('Super Eagles beat...'). Each phrase must survive the junk and demonym
    filters after normalization. Returns display-form phrases, deduped,
    capped at _MAX_NAMES_PER_HEADLINE.
    """
    raw = str(text or "")
    out: list[str] = []
    seen: set[str] = set()
    for match in _CAPITALIZED_RUN.finditer(raw):
        phrase = match.group(0).strip()
        tokens = phrase.split()
        if match.start() == 0 and len(tokens) == 1:
            continue
        # A 3+ token all-caps run is a shouting headline fragment
        # ("LISTEN TO ME BRO"), not an entity name.
        if phrase.isupper() and len(tokens) >= 3:
            continue
        while tokens and tokens[0].lower().strip(".") in _ROLE_PREFIX_TOKENS:
            tokens = tokens[1:]
        if len(tokens) < 1:
            continue
        phrase = " ".join(tokens)
        key = _normalize_entity_key(phrase)
        if not key or key in seen:
            continue
        key_tokens = key.split()
        if _entity_key_is_junk(key) or all(t in _NON_ENTITY_TOKENS for t in key_tokens):
            continue
        seen.add(key)
        out.append(phrase)
        if len(out) >= _MAX_NAMES_PER_HEADLINE:
            break
    return out


def _entity_key_is_junk(key: str) -> bool:
    """True when a normalized entity_key cannot identify a real event anchor.

    Rejects bare numbers/years ('2026'), single tokens shorter than 4 chars,
    and single generic tokens (_GENERIC_ENTITY_TOKENS). Multi-word keys pass:
    they are the real anchors ('nelson mandela', 'super eagles').
    """
    if not key:
        return True
    if " " in key:
        return False
    if key.isdigit():
        return True
    if len(key) < 4:
        return True
    return key in _GENERIC_ENTITY_TOKENS


def _entity_alias_map(market: str) -> dict[str, list[str]]:
    """The curated entity_key -> [alias, ...] map for one market.

    Empty-safe: a missing/malformed config or an unknown market both degrade
    to {} so this can never break ledger construction.
    """
    try:
        raw = load_entity_aliases().get(market) or {}
    except Exception:
        return {}
    return {str(k).strip().lower(): list(v or []) for k, v in raw.items() if isinstance(v, list)}


def _alias_canonical_map(alias_map: dict[str, list[str]]) -> dict[str, str]:
    """Reverse the curated alias map: normalized name -> canonical entity_key.

    Without this the curated aliases only ever WIDEN an existing cluster's
    matchable names; they never MERGE the clusters themselves. A headline
    naming "Ramaphosa" extracts the anchor 'ramaphosa' while GDELT supplies
    'cyril ramaphosa', and the two ran as separate ledger events keyed on the
    same human being (observed live: 'ramaphosa' and 'cyril ramaphosa' both
    appearing as matched_entity_key values in the same week). Mapping every
    curated alias back to its canonical key collapses them into one event.

    A blocklisted generic alias is skipped: "President" must never canonicalize
    to a specific person.
    """
    canonical: dict[str, str] = {}
    for key, aliases in alias_map.items():
        canonical_key = _normalize_entity_key(key)
        if not canonical_key:
            continue
        canonical[canonical_key] = canonical_key
        for alias in aliases or []:
            norm = _normalize_entity_key(str(alias))
            if not norm or norm in _GENERIC_ALIAS_BLOCKLIST:
                continue
            # Never let an alias steal a key that is itself canonical.
            canonical.setdefault(norm, canonical_key)
    return canonical


def _canonicalize_candidates(
    candidates: list[dict[str, Any]], canonical: dict[str, str]
) -> list[dict[str, Any]]:
    """Rewrite each candidate's entity_key to its canonical form, in place.

    Only anchor-shaped candidates (entity / search_term) are rewritten. A
    headline candidate's entity_key is the whole normalized headline, which is
    matched as text during clustering, so rewriting it would break attachment.
    """
    if not canonical:
        return candidates
    for cand in candidates:
        if cand.get("event_kind") not in {"entity", "search_term"}:
            continue
        target = canonical.get(cand["entity_key"])
        if target and target != cand["entity_key"]:
            cand["entity_key"] = target
    return candidates


def _anchor_phrase_index(
    candidates: list[dict[str, Any]], alias_map: dict[str, list[str]]
) -> dict[str, list[str]]:
    """canonical entity_key -> extra normalized phrases that name it.

    Clustering attaches a free-text headline to an anchor by looking for the
    anchor's key inside the headline. Once 'ramaphosa' canonicalizes to 'cyril
    ramaphosa' the literal key no longer appears in a "Ramaphosa ..." headline,
    so the curated aliases have to be searchable too or canonicalization would
    silently drop evidence.
    """
    keys = {c["entity_key"] for c in candidates if c.get("event_kind") in {"entity", "search_term"}}
    index: dict[str, list[str]] = {}
    for key, aliases in alias_map.items():
        canonical_key = _normalize_entity_key(key)
        if canonical_key not in keys:
            continue
        phrases = []
        for alias in aliases or []:
            norm = _normalize_entity_key(str(alias))
            if norm and norm != canonical_key and norm not in _GENERIC_ALIAS_BLOCKLIST:
                phrases.append(norm)
        if phrases:
            index[canonical_key] = phrases
    return index


def _merge_aliases(observed: list[str], curated: list[str]) -> list[str]:
    """Merge today's observed raw_names with the curated alias list.

    Drops a curated alias that is a single generic blocklisted token; keeps
    every observed alias as-is (those came from real data, not a hand-edited
    config, so they carry no blocklist risk).
    """
    extra = [
        alias
        for alias in curated
        if str(alias).strip() and str(alias).strip().lower() not in _GENERIC_ALIAS_BLOCKLIST
    ]
    return sorted({*observed, *extra})


# A headline naming two sides of a fixture ("Springboks vs England") should
# corroborate BOTH anchors it names, not just whichever anchor string is
# longest. Non-fixture headlines keep the original longest-anchor-wins
# behaviour so a headline never fans out to every unrelated substring match.
_FIXTURE_HEADLINE_PATTERN = re.compile(r"\bvs\.?\b|\bversus\b")

# A headline carrying a head-to-head marker names two sides of a fixture, not
# one entity qualifying another, so it should corroborate both anchors it
# matches rather than only the longest.
_HEAD_TO_HEAD_MARKER = re.compile(r"\bvs\.?\b|\bversus\b")


# ---------------------------------------------------------------------------
# Step A: BQ read
# ---------------------------------------------------------------------------


def _fetch_factual_rows(
    client: bq.Client, dataset: str, trend_date: date, market: str
) -> list[dict[str, Any]]:
    """Pull today's FACTUAL enriched_content rows for one market.

    Three channel families:
    - GDELT GKG (content_type='gdelt_gkg'): v2persons / v2orgs / v2locations
      semicolon-delimited entity strings + v2tone + published_at.
    - RSS/article rows: live enriched_content stores these as content_type
      'article' with source carrying the publication name, but we still accept
      the legacy source='rss' shape too.
    - BigQuery Trends: live rows land as content_type in ('search_term',
      'top_term'), with a legacy source='bigquery_trends' fallback. The
      google_trends_rss connector's 'trending_search' rows join the same
      trends family (KE's only search-trend source).
    - YouTube search-result videos (content_type 'video' or 'video/N'): the
      video title is treated like an RSS headline.
    - Apple Music charts (source='apple_music', content_type='chart_track'):
      the track/artist title is treated like an RSS headline for the 'music'
      family.

    Modelled on _fetch_brand24_insights: one parameterised read, returns an
    empty list on failure so the caller degrades rather than blocking.
    """
    sql = f"""
    SELECT
      source,
      content_type,
      title,
      text,
      query_term,
      v2persons,
      v2orgs,
      v2locations,
      v2tone,
      search_velocity_score,
      published_at
    FROM `{client.project}.{dataset}.enriched_content`
    WHERE DATE(collected_at) = @trend_date
      AND market = @market
      AND (
        content_type = 'gdelt_gkg'
        OR content_type = 'article'
        OR content_type IN ('search_term', 'top_term', 'trending_search')
        OR content_type = 'video'
        OR content_type LIKE 'video/%'
        OR (source = 'apple_music' AND content_type = 'chart_track')
        OR source = 'rss'
        OR source = 'bigquery_trends'
      )
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[
            bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
            bq.ScalarQueryParameter("market", "STRING", market),
        ]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
    except Exception:
        logger.warning(
            "event_ledger: factual-row fetch failed for %s/%s, skipping market",
            trend_date,
            market,
        )
        return []
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Step B: candidate extraction + clustering
# ---------------------------------------------------------------------------


def _channel_family(row: dict[str, Any]) -> str:
    """Map a row to its channel family for the noise floor."""
    content_type = str(row.get("content_type") or "")
    source = str(row.get("source") or "")
    if content_type == _GDELT_CONTENT_TYPE:
        return "gdelt"
    if content_type in _RSS_CONTENT_TYPES or source == _RSS_SOURCE:
        return "rss"
    if content_type in _TRENDS_CONTENT_TYPES or source == _TRENDS_SOURCE:
        return "trends"
    if content_type == _YOUTUBE_CONTENT_TYPE or content_type.startswith(
        f"{_YOUTUBE_CONTENT_TYPE}/"
    ):
        return "youtube"
    if source == _APPLE_MUSIC_SOURCE and content_type == _APPLE_MUSIC_CONTENT_TYPE:
        return "music"
    return source or "other"


def _looks_like_theme_blob(text: str) -> bool:
    """True when text is a GDELT thematic-taxonomy blob, not real prose.

    Guards evidence_quote / state_text: reconcile can put state_text verbatim
    into a corrected claim, so a raw theme-code string must never surface as
    if it were a citable quote.
    """
    return bool(_THEME_BLOB_TOKEN.search(text or ""))


def _normalize_entity_key(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for clustering."""
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _split_gdelt_entities(value: str) -> list[str]:
    """Split a GDELT v2persons/v2orgs string into clean entity names.

    GDELT delimits entities with ';' and may carry an offset after a ','
    (e.g. 'Cyril Ramaphosa,123'). Keep the name, drop the offset.
    """
    out: list[str] = []
    for chunk in (value or "").split(";"):
        name = chunk.split(",", 1)[0].strip()
        # A pandas round-trip can serialise a missing column as the literal
        # string 'nan'; it is not an entity.
        if len(name) >= 3 and name.lower() != "nan":
            out.append(name)
    return out


def _parse_published_at(value: Any) -> datetime | None:
    """Coerce a BQ published_at value to a tz-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _extract_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn factual rows into per-candidate mention records.

    Each candidate carries: raw_name (display), entity_key (normalized),
    family, headline (the freshest text seen), published_at, event_kind,
    search_velocity, is_search_spike.
    """
    candidates: list[dict[str, Any]] = []
    for row in rows:
        family = _channel_family(row)
        published_at = _parse_published_at(row.get("published_at"))
        try:
            velocity = float(row.get("search_velocity_score") or 0.0)
        except (TypeError, ValueError):
            velocity = 0.0

        if family == "gdelt":
            names = _split_gdelt_entities(str(row.get("v2persons") or ""))
            names += _split_gdelt_entities(str(row.get("v2orgs") or ""))
            # GDELT GKG rows carry an empty title and a raw theme dump in
            # text; the dump must never become a headline, so fall back to
            # the entity name instead.
            headline = str(row.get("title") or "")
            event_kind = "entity"
            for name in names:
                candidates.append(
                    {
                        "raw_name": name,
                        "entity_key": _normalize_entity_key(name),
                        "family": family,
                        "headline": headline or name,
                        "published_at": published_at,
                        "event_kind": event_kind,
                        "search_velocity": 0.0,
                        "is_search_spike": False,
                    }
                )
        elif family in {"rss", "youtube", "music"}:
            headline = str(row.get("title") or "")
            if not headline.strip():
                continue
            candidates.append(
                {
                    "raw_name": headline,
                    "entity_key": _normalize_entity_key(headline),
                    "family": family,
                    "headline": headline,
                    "published_at": published_at,
                    "event_kind": "headline",
                    "search_velocity": 0.0,
                    "is_search_spike": False,
                }
            )
            # Proper-noun phrases from the headline become first-class
            # anchors, so headline-only evidence can seed and join clusters
            # on real names (and give reconcile short, matchable aliases)
            # instead of dying unmatched at clustering.
            for name in _proper_noun_phrases(headline):
                candidates.append(
                    {
                        "raw_name": name,
                        "entity_key": _normalize_entity_key(name),
                        "family": family,
                        "headline": headline,
                        "published_at": published_at,
                        "event_kind": "entity",
                        "search_velocity": 0.0,
                        "is_search_spike": False,
                    }
                )
        elif family == "trends":
            term = str(row.get("query_term") or "")
            if not term.strip():
                continue
            candidates.append(
                {
                    "raw_name": term,
                    "entity_key": _normalize_entity_key(term),
                    "family": family,
                    "headline": term,
                    "published_at": published_at,
                    "event_kind": "search_term",
                    "search_velocity": velocity,
                    "is_search_spike": velocity >= _SEARCH_SPIKE_THRESHOLD,
                }
            )
    return [c for c in candidates if not _entity_key_is_junk(c["entity_key"])]


def _pick_evidence_quote(members: list[dict[str, Any]]) -> str:
    """Pick the evidence_quote text: the freshest headline, skipping a raw
    GDELT theme-code blob when a cleaner alternative exists in the cluster,
    and preferring a news headline over a video title when both are present.

    A member may carry a precomputed 'is_blob' flag; when absent, blob-ness
    is computed from the headline text directly so this stays a standalone,
    directly-testable helper. as_of/event_kind still track the true freshest
    member regardless (computed separately in _cluster_candidates); only the
    quoted text skips the blob.
    """

    def _sort_key(m: dict[str, Any]) -> datetime:
        return m.get("published_at") or datetime.min.replace(tzinfo=UTC)

    def _is_blob(m: dict[str, Any]) -> bool:
        if "is_blob" in m:
            return bool(m["is_blob"])
        return _looks_like_theme_blob(str(m.get("headline") or ""))

    ranked = sorted(members, key=_sort_key, reverse=True)
    clean = [m for m in ranked if not _is_blob(m)]
    if clean:
        # Prefer readable prose over a GDELT entity-name echo (headline ==
        # raw_name), and prefer either over a video title. A video title names
        # a piece of content, not a state of the world, so a game-simulation
        # upload reads as a real result once the title stands alone. On
        # 2026-08-25 the ng/chelsea cluster held a Vanguard report of the real
        # match at 21:13 and two eFootball simulation videos at 21:27 and
        # 22:02; freshest-wins handed the quote to a simulation, and reconcile
        # puts state_text verbatim into a corrected claim. Demoted, not
        # excluded: a youtube-only cluster still keeps its quote.
        def _quote_rank(m: dict[str, Any]) -> tuple[int, datetime]:
            headline = str(m.get("headline") or "").strip()
            raw = str(m.get("raw_name") or "").strip()
            entity_echo = (
                m.get("event_kind") == "entity"
                and m.get("family") == "gdelt"
                and bool(raw and headline.lower() == raw.lower())
            )
            if entity_echo:
                tier = 0
            elif m.get("family") == "youtube":
                tier = 1
            else:
                tier = 2
            return (tier, _sort_key(m))

        return max(clean, key=_quote_rank)["headline"]
    # Every member is a blob: nothing clean to fall back to, so surface the
    # freshest blob rather than nothing.
    return ranked[0]["headline"]


def _cluster_candidates(
    candidates: list[dict[str, Any]],
    anchor_phrases: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Cluster candidates by entity_key, applying the noise floor.

    Anchor-based clustering: GDELT entities and trends terms are anchors whose
    normalized entity_key names a candidate event. A free-text headline (RSS,
    or a multi-word trends term) is attributed to an anchor when the anchor's
    entity_key appears as a whole-word run inside the headline, so a GDELT
    person 'bafana bafana' and an RSS headline 'Bafana Bafana win the cup'
    collapse into one cluster across two channel families. A headline that
    matches no anchor clusters on its own normalized key (so a lone headline
    never corroborates itself). A "X vs Y" fixture headline names two sides,
    so it attaches to every anchor it matches; anything else keeps
    longest-anchor-wins so a headline does not fan out to every substring
    match.

    NOISE FLOOR: keep a cluster only if it has >=2 distinct channel families
    OR a real search-velocity spike. Each kept cluster carries:
    entity_key, entity_aliases, families (set), source_count, as_of (max
    published_at), evidence_quote (freshest headline), event_kind,
    deterministic state_label, max_search_velocity.
    """
    # Anchors are the short, entity-shaped candidates (named entities + search
    # terms). A multi-word headline is not itself an anchor; it attaches to one.
    anchors = [c for c in candidates if c["event_kind"] in {"entity", "search_term"}]
    anchor_keys = sorted({a["entity_key"] for a in anchors}, key=len, reverse=True)
    # (searchable phrase, canonical key) pairs, longest phrase first. A curated
    # alias is searchable alongside the key itself so a headline naming the
    # entity by its short name still attaches to the canonical cluster.
    phrase_pairs: list[tuple[str, str]] = [(key, key) for key in anchor_keys]
    for key, phrases in (anchor_phrases or {}).items():
        if key in anchor_keys:
            phrase_pairs.extend((phrase, key) for phrase in phrases)
    phrase_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    by_key: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        if cand["event_kind"] in {"entity", "search_term"}:
            by_key.setdefault(cand["entity_key"], []).append(cand)
            continue
        # Free-text headline: attach to the anchor(s) whose key appears as a
        # whole-word run inside the headline. A "X vs Y" fixture headline
        # names two sides, not one entity qualifying another, so it attaches
        # to every matching anchor. Anything else keeps the original
        # longest-anchor-wins behaviour so a headline does not fan out to
        # every substring match.
        haystack = f" {cand['entity_key']} "
        matched_keys: list[str] = []
        for phrase, key in phrase_pairs:
            if f" {phrase} " in haystack and key not in matched_keys:
                matched_keys.append(key)
        if not matched_keys:
            continue
        if _HEAD_TO_HEAD_MARKER.search(str(cand.get("headline") or "")):
            for key in matched_keys:
                by_key.setdefault(key, []).append(cand)
        else:
            by_key.setdefault(matched_keys[0], []).append(cand)

    clusters: list[dict[str, Any]] = []
    for entity_key, members in by_key.items():
        families = {m["family"] for m in members}
        has_spike = any(m["is_search_spike"] for m in members)
        if len(families) < 2 and not has_spike:
            logger.info(
                "event_ledger: dropped '%s' at the noise floor (families=%s, spike=%s)",
                entity_key,
                sorted(families),
                has_spike,
            )
            continue

        # Freshest member by published_at (None sorts oldest) gives as_of +
        # the evidence_quote headline.
        def _sort_key(m: dict[str, Any]) -> datetime:
            return m["published_at"] or datetime.min.replace(tzinfo=UTC)

        freshest = max(members, key=_sort_key)
        aliases = sorted({m["raw_name"] for m in members if m["raw_name"]})
        max_velocity = max((m["search_velocity"] for m in members), default=0.0)
        evidence_quote = _pick_evidence_quote(members)

        clusters.append(
            {
                "entity_key": entity_key,
                "entity_aliases": aliases,
                "families": sorted(families),
                "source_count": len(members),
                "as_of": freshest["published_at"],
                "evidence_quote": evidence_quote,
                "event_kind": freshest["event_kind"],
                "state_label": _deterministic_state(members),
                "max_search_velocity": max_velocity,
                # Flat evidence rows (one per member) for the prompt + the
                # Python citation post-validator.
                "members": members,
            }
        )
    return clusters


# ---------------------------------------------------------------------------
# Step C: deterministic state
# ---------------------------------------------------------------------------


def _deterministic_state(members: list[dict[str, Any]]) -> str:
    """Provisional state from headline patterns across a cluster's members.

    resolved beats scheduled beats unknown. This is the fallback that ships
    when Gemini is skipped or its answer fails post-validation.
    """
    blob = " ".join(str(m.get("headline") or "") for m in members).lower()
    if _RESOLVED_PATTERNS.search(blob):
        return "resolved"
    if _SCHEDULED_PATTERNS.search(blob):
        return "scheduled"
    return "unknown"


# ---------------------------------------------------------------------------
# Step D: Gemini resolution + Python post-validation
# ---------------------------------------------------------------------------


def _recency_factor(as_of: datetime | None, trend_date: date) -> float:
    """1.0 when the freshest evidence is from trend_date, decaying to 0.0.

    Same-day -> 1.0, one day stale -> 0.5, two+ days -> 0.0. None -> 0.0.
    """
    if as_of is None:
        return 0.0
    # Anchor on the END of trend_date so a same-day timestamp (any hour) scores
    # full recency and a prior-day timestamp scores one day stale.
    end_of_day = datetime.combine(trend_date, datetime.max.time(), tzinfo=UTC)
    age_days = (end_of_day - as_of).days
    if age_days <= 0:
        return 1.0
    if age_days == 1:
        return 0.5
    return 0.0


def _compute_confidence(source_count: int, recency_factor: float, has_citation: bool) -> float:
    """Confidence in [0, 1], computed in Python, never by the model.

    clamp(0.4*min(source_count/3, 1) + 0.3*recency_factor + 0.3*citation_factor)
    """
    citation_factor = 1.0 if has_citation else 0.0
    raw = 0.4 * min(source_count / 3.0, 1.0) + 0.3 * recency_factor + 0.3 * citation_factor
    return max(0.0, min(1.0, raw))


def _resolve_with_gemini(
    clusters: list[dict[str, Any]],
    client: GeminiClient,
    trend_date: date,
    market: str | None = None,
    usage_tally: dict | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """One Gemini call over the clusters. Returns (by_entity_key, model).

    ``usage_tally``, when passed, collects this call's token counts for the
    gemini_usage ledger under ``market``. The call bills even when every
    cluster ends up keeping its deterministic state.

    Builds a FLAT evidence list (global indices across all clusters) so the
    model's evidence_index points at a single row list the post-validator can
    check. Drops any returned state whose evidence_index does not point at a
    supplied row. Returns a map entity_key -> {state_label, state_text} for the
    states that survive validation.
    """
    # Build the flat evidence list + per-cluster prompt input. Each member gets
    # a global index; we remember which cluster (entity_key) each index belongs
    # to so a citation can be checked against the right entity.
    flat_index_to_key: dict[int, str] = {}
    prompt_clusters: list[dict[str, Any]] = []
    idx = 0
    for cluster in clusters:
        evidence_rows: list[dict[str, Any]] = []
        for member in cluster["members"]:
            flat_index_to_key[idx] = cluster["entity_key"]
            evidence_rows.append({"index": idx, "text": member.get("headline") or ""})
            idx += 1
        prompt_clusters.append({"entity_key": cluster["entity_key"], "evidence": evidence_rows})

    prompt = build_event_ledger_prompt(prompt_clusters)
    response = client.generate_brief(
        prompt,
        RESPONSE_SCHEMA,
        model=RECONCILE_GEMINI_MODEL,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    if usage_tally is not None:
        record_usage(usage_tally, market, response)
    model = response.model

    # The schema is an ARRAY; the SDK wrapper normalises the top level to a
    # dict, so the array may land under a key or as result.parsed itself.
    raw = response.parsed
    items: list[Any] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        # google-genai sometimes wraps a bare array; pull the first list value.
        for value in raw.values():
            if isinstance(value, list):
                items = value
                break

    validated: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        entity_key = str(item.get("entity_key") or "").strip()
        state_label = str(item.get("state_label") or "").strip().lower()
        state_text = str(item.get("state_text") or "").strip()
        try:
            evidence_index = int(item.get("evidence_index"))
        except (TypeError, ValueError):
            # No usable citation: this state cannot be trusted, drop it so the
            # deterministic guess wins.
            continue

        # Anti-hallucination: the cited index must exist AND point at THIS
        # entity's evidence. A citation to a row belonging to a different
        # entity is treated as no citation.
        cited_key = flat_index_to_key.get(evidence_index)
        if cited_key != entity_key:
            continue

        validated[entity_key] = {
            "state_label": state_label,
            "state_text": state_text,
            "has_citation": True,
        }
    return validated, model


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def build_event_ledger(
    trend_date: date,
    market: str,
    client: GeminiClient | None = None,
    bq_client: bq.Client | None = None,
    dataset: str | None = None,
    usage_tally: dict | None = None,
) -> list[dict[str, Any]]:
    """Build the event ledger for one market.

    Returns a list of dicts, each EXACTLY:
        {entity_key, entity_aliases, event_kind, state_label, state_text,
         as_of (ISO str or None), evidence_quote,
         corroborating_sources (list of channel-family names),
         source_count (int), confidence (float 0-1),
         resolved_by ('deterministic' | 'gemini')}

    Step A reads factual rows, Step B clusters with the noise floor, Step C
    assigns a deterministic provisional state, Step D runs one Gemini call and
    post-validates citations in Python. The deterministic state is the fallback
    when Gemini is skipped, fails, or returns an uncitable state.
    """
    bq_client = bq_client or get_client()
    dataset = dataset or get_dataset()

    alias_map = _entity_alias_map(market)

    rows = _fetch_factual_rows(bq_client, dataset, trend_date, market)
    candidates = _extract_candidates(rows)
    # Fold every curated alias onto its canonical key BEFORE clustering, so one
    # real-world entity is one event, not one per spelling the day happened to
    # use ('ramaphosa' + 'cyril ramaphosa').
    candidates = _canonicalize_candidates(candidates, _alias_canonical_map(alias_map))
    clusters = _cluster_candidates(candidates, _anchor_phrase_index(candidates, alias_map))
    # Cluster count feeds the Gemini prompt size; log it so drift from the
    # extracted-name anchors is visible across cron days.
    logger.info(
        "event_ledger: %s clusters=%d candidates=%d rows=%d",
        market,
        len(clusters),
        len(candidates),
        len(rows),
    )
    if not clusters:
        return []

    # Step D: one Gemini call over the clusters (best-effort). On any failure
    # every cluster keeps its deterministic state.
    gemini_states: dict[str, dict[str, Any]] = {}
    gemini_model: str | None = None
    if client is not None:
        try:
            gemini_states, gemini_model = _resolve_with_gemini(
                clusters, client, trend_date, market=market, usage_tally=usage_tally
            )
        except Exception as exc:
            logger.warning(
                "event_ledger: Gemini resolution failed for %s/%s, using deterministic states: %s",
                trend_date,
                market,
                exc,
            )

    ledger: list[dict[str, Any]] = []
    for cluster in clusters:
        entity_key = cluster["entity_key"]
        deterministic_label = cluster["state_label"]
        resolved_by = "deterministic"
        state_label = deterministic_label
        state_text = cluster["evidence_quote"]
        has_citation = False

        gem = gemini_states.get(entity_key)
        if gem is not None:
            candidate_label = gem["state_label"]
            # A 'resolved' label must have a citing row; the validator already
            # guarantees a citation for every entry in gemini_states, so an
            # entry here is citable. Accept the Gemini state.
            if candidate_label in {"resolved", "scheduled", "unknown"}:
                state_label = candidate_label
                state_text = gem["state_text"] or state_text
                resolved_by = "gemini"
                has_citation = gem["has_citation"]
        elif state_label == "resolved" and _HEADLINE_FAMILIES & set(cluster["families"]):
            # Partial citation credit: a deterministic 'resolved' verdict
            # grounded in a real headline-shaped quote (rss/youtube/music,
            # not a GDELT theme blob) is as citable as a Gemini answer would
            # be, so it earns the same confidence credit.
            has_citation = not _looks_like_theme_blob(cluster["evidence_quote"])
        # A deterministic 'resolved' with no headline-family backing carries
        # no citation, so has_citation stays False and the confidence
        # citation term is 0.

        # Safety net: state_text can only ever be the ledger's own
        # evidence_quote or a Gemini citation drawn from supplied evidence,
        # never a raw GDELT theme-code blob, even if the model echoes one.
        if _looks_like_theme_blob(state_text):
            state_text = cluster["evidence_quote"]

        recency = _recency_factor(cluster["as_of"], trend_date)
        confidence = _compute_confidence(cluster["source_count"], recency, has_citation)

        as_of = cluster["as_of"]
        entity_aliases = _merge_aliases(cluster["entity_aliases"], alias_map.get(entity_key, []))
        ledger.append(
            {
                "entity_key": entity_key,
                "entity_aliases": entity_aliases,
                "event_kind": cluster["event_kind"],
                "state_label": state_label,
                "state_text": state_text,
                "as_of": as_of.isoformat() if as_of else None,
                "evidence_quote": cluster["evidence_quote"],
                "corroborating_sources": cluster["families"],
                "source_count": int(cluster["source_count"]),
                "confidence": round(confidence, 4),
                "resolved_by": resolved_by,
                "_gemini_model": gemini_model,
            }
        )
    return ledger


def _delete_today_ledger(client: bq.Client, dataset: str, trend_date: date) -> None:
    """Clear today's event_ledger rows so the insert overwrites cleanly.

    Delete-then-insert avoids the BigQuery streaming-buffer gotcha: a row that
    just landed via a load job cannot be UPDATEd for a while, so we never
    post-INSERT UPDATE. insert_dataframe uses a load job (not streaming
    inserts), so DELETE here is safe.
    """
    sql = f"""
    DELETE FROM `{client.project}.{dataset}.event_ledger`
    WHERE trend_date = @trend_date
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    client.query(sql, job_config=job_config).result()


def build_and_persist_ledger(
    trend_date: date,
    client: GeminiClient | None = None,
    bq_client: bq.Client | None = None,
    dataset: str | None = None,
    usage_sink: UsageSink | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build all 3 markets and write them to event_ledger idempotently.

    Returns the in-run dict {market: [ledger rows]}. Wrapped in try/except so a
    BQ or Gemini failure is non-fatal: logs and returns {} rather than breaking
    a pipeline that calls it.

    There is no MERGE. Persistence deletes every row for the trend_date and then
    inserts the fresh set as a load job, not a streaming write. The outcome is
    still idempotent, because a re-run for the same trend_date replaces the day
    wholesale, and going through a load job avoids the streaming-buffer UPDATE
    restriction that a MERGE would hit.
    """
    usage_tally: dict = {}
    sink = usage_sink or persist_gemini_usage
    try:
        import pandas as pd

        bq_client = bq_client or get_client()
        dataset = dataset or get_dataset()

        result: dict[str, list[dict[str, Any]]] = {}
        for market in MARKETS:
            result[market] = build_event_ledger(
                trend_date,
                market,
                client=client,
                bq_client=bq_client,
                dataset=dataset,
                usage_tally=usage_tally,
            )

        bq_rows: list[dict[str, Any]] = []
        generated_at = datetime.now(UTC)
        for market, entries in result.items():
            for entry in entries:
                bq_rows.append(
                    {
                        "ledger_id": str(uuid.uuid4()),
                        "trend_date": trend_date.isoformat(),
                        "market": market,
                        "entity_key": entry["entity_key"],
                        "entity_aliases": entry["entity_aliases"],
                        "event_kind": entry["event_kind"],
                        "state_label": entry["state_label"],
                        "state_text": entry["state_text"],
                        "as_of": entry["as_of"],
                        "evidence_quote": entry["evidence_quote"],
                        "corroborating_sources": entry["corroborating_sources"],
                        "source_count": entry["source_count"],
                        "confidence": entry["confidence"],
                        "resolved_by": entry["resolved_by"],
                        "gemini_model": entry.get("_gemini_model"),
                        "generated_at": generated_at.isoformat(),
                    }
                )

        _delete_today_ledger(bq_client, dataset, trend_date)
        if bq_rows:
            df = pd.DataFrame(bq_rows)
            insert_dataframe(df, "event_ledger")
        logger.info(
            "event_ledger: persisted %d rows for %s across %d markets",
            len(bq_rows),
            trend_date,
            len(result),
        )
        return result
    except Exception as exc:
        logger.error("event_ledger: build_and_persist failed (non-fatal): %s", exc)
        return {}
    finally:
        # A market resolved before the failure still billed.
        sink(usage_rows(trend_date, USAGE_CONSUMER, usage_tally))


__all__ = ["build_and_persist_ledger", "build_event_ledger"]
