"""Enrichment layer: adds scoring signals to raw content DataFrames.

Ported from trends-mvp/trends-free-mvp/pipeline.py prepare_common_df, split
into per-signal functions for testability. Reads per-market configs via
src/utils/config_loader.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime

import pandas as pd

from src.enrichment.embedding_classifier import EmbeddingClassifier
from src.enrichment.embedding_classifier import is_enabled as embedding_is_enabled
from src.enrichment.sentiment_lexicon import load_lexicon
from src.enrichment.topic_classifier import (
    LAYER_EMBEDDING,
    classify_topics,
    classify_topics_with_layer,
)
from src.utils.config_loader import load_market_creators, load_market_keywords, load_scoring
from src.utils.geo_blocklist import TOPIC_GEO_BLOCKLIST, text_matches_geo_blocklist
from src.utils.language_guard import should_skip_content_type
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

_TIER_WEIGHTS: dict[str, float] = {
    "tier_1": 1.0,
    "tier_2": 0.7,
    "tier_3": 0.4,
}

# Lazily-built embedding classifier (D2). One instance per process so the
# per-topic anchor vectors and the content-hash cache are built once and
# reused across all markets in a run. None until first use.
_EMBEDDING_CLF: EmbeddingClassifier | None = None


def _get_embedding_classifier() -> EmbeddingClassifier:
    global _EMBEDDING_CLF
    if _EMBEDDING_CLF is None:
        _EMBEDDING_CLF = EmbeddingClassifier()
    return _EMBEDDING_CLF


def _search_velocity_on() -> bool:
    """True when the search-velocity revive is enabled.

    Dark by default. Set SEARCH_VELOCITY_ENABLED=true to carry a real
    search_velocity_score (from BigQuery Trends rows) through enrichment
    instead of hard-zeroing every row. Read per call so the flag can flip
    on the live job without a code change and toggle in tests + shadow-runs.
    Mirrors _language_guard_on in src/enrichment/topic_classifier.py.
    """
    return os.environ.get("SEARCH_VELOCITY_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _sentiment_lexicon_on() -> bool:
    """True when the Wave 2 social sentiment lexicon scorer is enabled.

    Dark by default. Set SENTIMENT_LEXICON_ENABLED=true to compute
    sentiment_lexicon_score (-1.0..1.0) on each social (non-GDELT) row via the
    lexicon scorer in src/enrichment/sentiment_lexicon.py. When off, the column
    is filled with pd.NA on every row and enrichment is byte-identical to today.
    Read per call so the flag can flip on the live job without a code change and
    toggle in tests + shadow-runs. Mirrors _search_velocity_on above.
    """
    return os.environ.get("SENTIMENT_LEXICON_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _classification_instrumentation_on() -> bool:
    """True when Wave 1 classification instrumentation is enabled.

    Dark by default. Set CLASSIFICATION_INSTRUMENTATION_ENABLED=true to compute
    the per-row winning classification_layer (and let log_pipeline_run roll up
    per-layer counts + labelling_rate_percent). When off, enrichment never
    asks for the layer and classification_layer stays empty, byte-identical to
    today. Read per call so it can flip on the live job without a code change.
    """
    return os.environ.get("CLASSIFICATION_INSTRUMENTATION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def engagement_weight_for(content_type: str, weights_map: dict[str, float]) -> float:
    """Look up the engagement weight for a content_type with default fallback."""
    if not content_type:
        return float(weights_map.get("default", 1.0))
    w = weights_map.get(content_type)
    if w is None:
        w = weights_map.get("default", 1.0)
    return float(w)


def _parse_v2tone(raw: object) -> tuple[float, float]:
    """Parse GDELT V2Tone comma-delimited string into (tone_avg_norm, polarity_norm).

    Format: avg_tone, positive, negative, polarity, activity, self_ref, word_count.
    Normalises avg_tone (-100..+100) to 0..1 via (tone + 100) / 200.
    Normalises polarity (0..100) to 0..1 via / 100.
    Missing / unparseable / empty returns (NaN, NaN) so aggregation can exclude.
    """
    import math

    if raw is None or not isinstance(raw, str) or not raw.strip():
        return (math.nan, math.nan)
    parts = raw.split(",")
    if len(parts) < 4:
        return (math.nan, math.nan)
    try:
        avg_tone = float(parts[0])
        polarity = float(parts[3])
    except (ValueError, TypeError):
        return (math.nan, math.nan)
    tone_norm = max(0.0, min(1.0, (avg_tone + 100.0) / 200.0))
    polarity_norm = max(0.0, min(1.0, polarity / 100.0))
    return (tone_norm, polarity_norm)


# GCAM emotional-arousal intensity normalisation. The signal answers "how
# emotional is the coverage of this topic", regardless of valence, so both
# positive and negative emotion lift it. Built from the two Lexicoder emotion
# dims persisted on v2gcam by the GDELT connector: v19.1 (positive emotion) and
# v19.9 (negative emotion). Each is min-max normalised to 0..1 against its
# observed [min, p95] range and winsorised at p95 so outliers do not saturate.
# The Hedonometer dims (v10.1 happiness, v10.2 its secondary) and SentiWordNet
# polarity (v20.1) are excluded: they are valence, which overlaps the existing
# v2tone signal. Bounds calibrated 2026-06-25 against the first full post-flip
# cron (4545 GKG rows). RECALIBRATE against a multi-day distribution before
# raising the gcam_score weight above 0.00 in scoring.yaml.
_GCAM_INTENSITY_BOUNDS = {
    "v19.1": (3.713, 6.536),
    "v19.9": (3.686, 5.796),
}


def _parse_gcam_intensity(raw: object) -> float:
    """Parse the v2gcam code:value string into a 0..1 emotional-intensity float.

    Reads only the dims in _GCAM_INTENSITY_BOUNDS, min-max normalises each to
    0..1 (clamped), and returns the mean of whatever dims are present so a row
    missing one dim is not penalised. Missing / empty / unparseable, or no
    target dim present, returns NaN so aggregation excludes the row rather than
    biasing it toward 0 (same convention as _parse_v2tone).
    """
    import math

    if raw is None or not isinstance(raw, str) or not raw.strip():
        return math.nan
    present: list[float] = []
    for tok in raw.split(","):
        if ":" not in tok:
            continue
        code, _, val = tok.partition(":")
        bounds = _GCAM_INTENSITY_BOUNDS.get(code.strip())
        if bounds is None:
            continue
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        lo, hi = bounds
        rng = hi - lo
        norm = 0.0 if rng <= 0 else (v - lo) / rng
        present.append(max(0.0, min(1.0, norm)))
    if not present:
        return math.nan
    return round(sum(present) / len(present), 4)


def engagement_per_day(engagement_total: float, published_at, now=None) -> float:
    """Divide raw engagement by days since published so an old evergreen viral
    video does not contribute the same as a fresh vlog. Unknown published_at
    returns the raw total (treated as today's content).
    """
    if not engagement_total or engagement_total <= 0:
        return 0.0
    if published_at is None:
        return float(engagement_total)
    try:
        pub = pd.to_datetime(published_at, utc=True, errors="coerce")
    except Exception:
        return float(engagement_total)
    if pub is None or pd.isna(pub):
        return float(engagement_total)
    ref = now or datetime.now(UTC)
    days = max((ref - pub.to_pydatetime()).days, 1)
    return float(engagement_total) / float(days)


def extract_hashtags(text: str) -> list[str]:
    """Return lowercase hashtag tokens from text (without the leading '#')."""
    if not text:
        return []
    return re.findall(r"#(\w+)", str(text).lower())


# Saturation constant for keyword relevance. Robertson/BM25 term-frequency
# form hits/(hits+K): the first marker hit counts most, each additional hit
# gives diminishing returns, no hard cap at 3. K=1.5 -> 1 hit 0.40, 2 -> 0.57,
# 3 -> 0.67, 5 -> 0.77. Replaces the old min(1.0, hits/3.0) cliff.
RELEVANCE_SATURATION_K = 1.5


def relevance_score(text: str, markers: list) -> float:
    """Saturated count of word-boundary marker hits, on 0..1.

    Markers match on word boundaries (not bare substrings) so a marker does
    not fire inside an unrelated longer word (the classic 'died' inside
    'studied' false positive). Repeated hits saturate via hits/(hits+K)
    rather than the old hits/3 hard cap, so a strongly on-topic text outscores
    a single-word match without a cliff at three hits.

    Accepts ints/floats in markers (configs may carry numeric entries like
    254 for the Kenya dialling code); they are str()'d before matching.
    """
    if not text or not markers:
        return 0.0
    text_l = str(text).lower()
    hits = 0
    for m in markers:
        m_l = str(m).lower().strip()
        if not m_l:
            continue
        # Boundary on both sides so the marker is not counted inside a larger
        # word. Lookarounds (not \b) so multi-word and punctuated markers such
        # as "gen z" or "+254" anchor correctly.
        if re.search(rf"(?<!\w){re.escape(m_l)}(?!\w)", text_l):
            hits += 1
    return round(hits / (hits + RELEVANCE_SATURATION_K), 4)


def detect_topic(text: str, topic_groups: dict[str, list]) -> str:
    """First-match topic group. Returns 'other' when nothing hits."""
    if not text or not topic_groups:
        return "other"
    text_l = str(text).lower()
    for group, terms in topic_groups.items():
        for term in terms:
            if str(term).lower() in text_l:
                return group
    return "other"


def normalize_handle(handle: str) -> str:
    """Strip whitespace, lowercase, drop all '@' characters."""
    if handle is None:
        return ""
    return str(handle).strip().lower().replace("@", "")


def build_watchlist_lookup(creators_config: dict) -> dict[tuple[str, str], str]:
    """(platform, normalized_handle) -> tier_name from creators yaml."""
    lookup: dict[tuple[str, str], str] = {}
    watchlists = creators_config.get("watchlists", {}) if creators_config else {}
    for platform, tiers in watchlists.items():
        for tier_name, handles in (tiers or {}).items():
            for handle in handles or []:
                norm = normalize_handle(handle)
                if norm:
                    lookup[(str(platform).lower(), norm)] = tier_name
    return lookup


def build_handle_to_tier_fallback(creators_config: dict) -> dict[str, str]:
    """normalized_handle -> best tier across ALL platforms.

    Cross-platform fallback for the watchlist join. The strict
    (platform, handle) lookup misses every creator row whose source
    connector emits a platform (youtube, reddit, web, social) that the
    creators yaml does not list, even when the handle itself is a
    known anchor on a different surface (e.g. Tyla listed under tiktok
    but ingested as a YouTube video).

    Tier precedence is tier_1 > tier_2 > tier_3 so a handle that sits
    in tier_1 on one platform and tier_3 on another gets the higher
    tier here.
    """
    fallback: dict[str, str] = {}
    if not creators_config:
        return fallback
    tier_rank = {"tier_1": 3, "tier_2": 2, "tier_3": 1}
    for _platform, tiers in (creators_config.get("watchlists", {}) or {}).items():
        for tier_name, handles in (tiers or {}).items():
            new_rank = tier_rank.get(tier_name, 0)
            if new_rank == 0:
                continue
            for handle in handles or []:
                norm = normalize_handle(handle)
                if not norm:
                    continue
                current = fallback.get(norm)
                if current is None or tier_rank.get(current, 0) < new_rank:
                    fallback[norm] = tier_name
    return fallback


def watchlist_score_for_tier(tier_name: str) -> float:
    return _TIER_WEIGHTS.get(tier_name or "", 0.0)


def _term_matches(term: str, text_l: str) -> bool:
    """True when term appears in text_l on word boundaries, not as a substring.

    Mirrors the boundary approach used by relevance_score and the topic
    classifier: lookarounds (not bare membership) so a short or embedded term
    does not false-match inside a longer word ("up" inside "setup", "da"
    inside "data", "veo" inside "video"). Lookarounds rather than \\b so
    multi-word and punctuated terms anchor correctly. Internal whitespace is
    relaxed to \\s+ so "soft life" matches "soft  life". term is str()'d and
    lowercased; text_l is expected already lowercased.
    """
    t = str(term).lower().strip()
    if not t:
        return False
    escaped = re.escape(t).replace(r"\ ", r"\s+")
    return re.search(rf"(?<!\w){escaped}(?!\w)", text_l) is not None


def extract_slang_terms(text: str, slang_config: dict) -> list[str]:
    """Return sorted unique slang terms present in text.

    V2 slang_config shape: {level_name: {terms: [...], weight: float}}.
    """
    if not text or not slang_config:
        return []
    text_l = str(text).lower()
    found: set[str] = set()
    for _level, cfg in slang_config.items():
        for term in (cfg or {}).get("terms", []) or []:
            if _term_matches(term, text_l):
                found.add(str(term).lower())
    return sorted(found)


def score_slang(text: str, slang_config: dict) -> float:
    """Weighted slang score, capped at 1.0.

    Score = sum(weight per matched term) / 3, capped at 1.0. The /3 gives
    parity with the MVP's max-saturates-at-3-hits convention.
    """
    if not text or not slang_config:
        return 0.0
    text_l = str(text).lower()
    score = 0.0
    for _level, cfg in slang_config.items():
        weight = float((cfg or {}).get("weight", 0.0))
        for term in (cfg or {}).get("terms", []) or []:
            if _term_matches(term, text_l):
                score += weight
    return min(1.0, score / 3.0)


def enrich_dataframe(df: pd.DataFrame, market: str) -> pd.DataFrame:
    """Apply all enrichment steps to a raw content DataFrame.

    Required input columns: title, text, author_handle, platform. Others are
    tolerated. Returns a new DataFrame with enrichment columns added.
    """
    if df is None or df.empty:
        logger.warning("enrich_dataframe called with empty input for market=%s", market)
        return pd.DataFrame([])

    keywords = load_market_keywords(market)
    creators = load_market_creators(market)

    topic_groups = keywords.get("topic_groups", {}) or {}
    regional_markers = keywords.get("regional_markers", []) or []
    genz_markers = keywords.get("genz_markers", []) or []
    slang_config = keywords.get("slang", {}) or {}
    watchlist_lookup = build_watchlist_lookup(creators)
    # Cross-platform fallback: same handle, any platform. Catches Tyla
    # under tiktok being matched on a YouTube row, Reddit profile
    # matched on Brand24 mention, etc. Without it, every creator on a
    # platform absent from the yaml (youtube, reddit, web, social) scores 0
    # even when the handle is a known anchor elsewhere.
    handle_fallback_lookup = build_handle_to_tier_fallback(creators)

    out = df.copy()
    for col in ["title", "text", "author_handle", "platform", "query_group"]:
        if col not in out.columns:
            out[col] = ""
        out[col] = out[col].fillna("").astype(str)

    out["full_text"] = (out["title"] + " " + out["text"]).str.strip()

    auto_topic = out["full_text"].apply(lambda t: detect_topic(t, topic_groups))
    out["query_group"] = out.apply(
        lambda r: r["query_group"] if r["query_group"].strip() else auto_topic.loc[r.name],
        axis=1,
    )

    out["regional_score"] = out["full_text"].apply(lambda t: relevance_score(t, regional_markers))
    out["genz_score"] = out["full_text"].apply(lambda t: relevance_score(t, genz_markers))
    out["slang_terms"] = out["full_text"].apply(
        lambda t: ",".join(extract_slang_terms(t, slang_config))
    )
    out["slang_score"] = out["full_text"].apply(lambda t: score_slang(t, slang_config))

    out["platform_norm"] = out["platform"].str.lower()
    out["author_handle_norm"] = out["author_handle"].apply(normalize_handle)

    def _resolve_watchlist_tier(row) -> str:
        handle = row["author_handle_norm"]
        if not handle:
            return ""
        # Strict (platform, handle) match first so a handle that sits on
        # multiple platforms with different tiers gets the per-platform
        # tier, not the cross-platform best.
        tier = watchlist_lookup.get((row["platform_norm"], handle), "")
        if tier:
            return tier
        # Cross-platform fallback. Catches creator content arriving on a
        # platform absent from the yaml (youtube, reddit, web, social).
        return handle_fallback_lookup.get(handle, "")

    out["creator_watchlist_tier"] = out.apply(_resolve_watchlist_tier, axis=1)
    out["creator_watchlist_score"] = out["creator_watchlist_tier"].apply(watchlist_score_for_tier)
    # search_velocity_score is the only signal sourced upstream of enrichment:
    # the bigquery_trends connector carries it on its rows (the other connectors
    # have no search velocity, so their rows have none). Dark behind
    # SEARCH_VELOCITY_ENABLED. When ON, keep an incoming value and zero only the
    # rows that arrived without one; when OFF, hard-zero every row exactly as
    # before so the live cron is byte-identical. KE rows stay 0.0 naturally:
    # the public Google Trends dataset has zero KE coverage, so KE never carries
    # a value to preserve (no special-casing needed).
    if _search_velocity_on() and "search_velocity_score" in out.columns:
        out["search_velocity_score"] = (
            pd.to_numeric(out["search_velocity_score"], errors="coerce").fillna(0.0).astype(float)
        )
    else:
        out["search_velocity_score"] = 0.0

    # Damp per-row engagement: apply content-type weight + views-per-day cap.
    # Saves run_rss_now aggregation from seeing a 1.86M-view Afrobeats track
    # saturate the engagement_score for a whole query_group.
    scoring = load_scoring()
    weights_map = scoring.get("engagement_weights", {}) or {}
    per_day_cap = float(scoring.get("engagement_per_day_cap") or 0) or None

    if "engagement_total" in out.columns and "content_type" in out.columns:
        now = datetime.now(UTC)

        def _dampen(row):
            raw = float(row.get("engagement_total") or 0.0)
            if raw <= 0:
                return 0.0
            per_day = engagement_per_day(raw, row.get("published_at"), now=now)
            if per_day_cap is not None and per_day > per_day_cap:
                per_day = per_day_cap
            return per_day * engagement_weight_for(str(row.get("content_type") or ""), weights_map)

        out["engagement_weighted"] = out.apply(_dampen, axis=1)
    else:
        out["engagement_weighted"] = 0.0

    # Step 4: parse GDELT V2Tone into normalised per-row floats. NaN for
    # non-GDELT rows so aggregation can exclude them rather than biasing
    # negative with a 0. Format: comma-delimited
    #   avg_tone, positive_score, negative_score, polarity, activity_density, ...
    if "v2tone" in out.columns:
        out[["tone_avg", "tone_polarity"]] = out["v2tone"].apply(_parse_v2tone).apply(pd.Series)
    else:
        out["tone_avg"] = pd.NA
        out["tone_polarity"] = pd.NA

    # GCAM-reader: parse v2gcam into a per-row emotional-intensity float. NaN for
    # non-GDELT rows (and GDELT rows with the flag dark / no target dim) so
    # aggregation excludes them. Ephemeral like tone_avg: consumed by
    # _aggregate_by_topic, never written to enriched_content.
    if "v2gcam" in out.columns:
        out["gcam_intensity"] = out["v2gcam"].apply(_parse_gcam_intensity)
    else:
        out["gcam_intensity"] = pd.NA

    # Wave 2: social sentiment lexicon. GDELT rows already carry tone_avg from
    # V2Tone; the ~95 percent that are social (source != 'gdelt') carry no
    # per-row sentiment. When SENTIMENT_LEXICON_ENABLED is on, score each social
    # row's full_text on -1.0..1.0 via the per-market lexicon; GDELT rows stay
    # NA (their tone is the V2Tone signal, not the slang lexicon). When off,
    # every row is NA and this block adds one NA column only, so the OFF path is
    # byte-identical to today aside from the additive column.
    if _sentiment_lexicon_on():
        lexicon = load_lexicon(market)
        source_l = (
            out["source"].fillna("").astype(str).str.lower()
            if "source" in out.columns
            else pd.Series([""] * len(out), index=out.index)
        )
        out["sentiment_lexicon_score"] = [
            (lexicon.score(text) if src != "gdelt" else pd.NA)
            for text, src in zip(out["full_text"], source_l, strict=False)
        ]
    else:
        out["sentiment_lexicon_score"] = pd.NA

    # Topic clustering refactor: three-layer classification per row against
    # configs/topic_groups/{market}.yaml. Empty list means unclassified;
    # scoring aggregation in Task 5 will skip those rows and bump the
    # unclassified counter in pipeline_runs.
    # 26 May 2026: extended with Brand24 aggregated-label direct mapping
    # (query_term arg) and slang_terms fallback. See
    # src/enrichment/topic_classifier.py docstring for the layer order.
    # Wave 1 instrumentation (gated). When on, the layer-aware classifier
    # reports the winning layer per row into classification_layer; when off,
    # the original classify_topics call runs unchanged and classification_layer
    # stays empty, so the OFF path is byte-identical to today.
    instrument = _classification_instrumentation_on()
    if instrument:
        _topic_groups: list[list[str]] = []
        _layers: list[str] = []
        for row in out.to_dict(orient="records"):
            topics, layer = classify_topics_with_layer(
                title=str(row.get("title") or ""),
                text=str(row.get("text") or ""),
                hashtags=str(row.get("hashtags") or ""),
                market=market,
                query_term=str(row.get("query_term") or ""),
                slang_terms=(
                    row.get("slang_terms").split(",")
                    if isinstance(row.get("slang_terms"), str) and row.get("slang_terms")
                    else []
                ),
                content_type=str(row.get("content_type") or ""),
            )
            _topic_groups.append(topics)
            _layers.append(layer)
        out["topic_groups"] = _topic_groups
        out["classification_layer"] = _layers
    else:
        out["topic_groups"] = [
            classify_topics(
                title=str(row.get("title") or ""),
                text=str(row.get("text") or ""),
                hashtags=str(row.get("hashtags") or ""),
                market=market,
                query_term=str(row.get("query_term") or ""),
                slang_terms=(
                    row.get("slang_terms").split(",")
                    if isinstance(row.get("slang_terms"), str) and row.get("slang_terms")
                    else []
                ),
                content_type=str(row.get("content_type") or ""),
            )
            for row in out.to_dict(orient="records")
        ]

    # D2: embedding rescue over the keyword-unclassified residual. Dark by
    # default (EMBEDDING_CLASSIFIER_ENABLED off). Batch-embed the residual
    # once, cosine-match against per-topic anchors, fill topic_groups.
    # Embedding-assigned topics still pass the geo-collision strip. Failures
    # are non-fatal: the keyword result stands and enrichment continues.
    if embedding_is_enabled():
        topic_lists = list(out["topic_groups"])
        records = out.to_dict(orient="records")
        # Exclude the same non-conversational rows the keyword layer already
        # skips via should_skip_content_type (gdelt_gkg machine theme codes,
        # video/* and chart_track titles) plus Brand24 platform=='aggregate'
        # rollup rows. Their text is codes / titles / metric blobs, not natural
        # language, so embedding them wastes the batch and risks a spurious
        # cosine match. They stay unclassified by the rescue, exactly as they
        # already are by the keyword layer, so this narrows embedding coverage
        # to the conversational residual only.
        residual = [
            (
                i,
                " ".join(
                    [
                        str(records[i].get("title") or ""),
                        str(records[i].get("text") or ""),
                        str(records[i].get("hashtags") or ""),
                    ]
                ).strip(),
            )
            for i, topics in enumerate(topic_lists)
            if not topics
            and not should_skip_content_type(str(records[i].get("content_type") or ""))
            and str(records[i].get("platform") or "").lower() != "aggregate"
        ]
        # Skip blank-text residual rows (empty social pings) - nothing to embed.
        residual = [(i, txt) for i, txt in residual if txt]
        if residual:
            try:
                clf = _get_embedding_classifier()
                near_miss_on = os.environ.get("NEAR_MISS_CAPTURE_ENABLED", "false").lower() in (
                    "1",
                    "true",
                    "yes",
                )
                near_topics: list[str | None] = [None] * len(out)
                near_cosines: list[float | None] = [None] * len(out)
                if near_miss_on:
                    matched, scored = clf.classify_batch_scored(
                        [txt for _, txt in residual], market
                    )
                else:
                    matched = clf.classify_batch([txt for _, txt in residual], market)
                    scored = None
                rescued = 0
                # When instrumenting, rescued rows flip from 'unclassified' to
                # 'embedding'. Mutate a copy of the column so an absent column
                # (instrumentation off) is never indexed.
                layers = list(out["classification_layer"]) if instrument else None
                for idx, ((i, txt), topics) in enumerate(zip(residual, matched, strict=False)):
                    if scored is not None:
                        pairs = scored[idx] if idx < len(scored) else []
                        if pairs and not topics:
                            near_topics[i] = pairs[0][0]
                            near_cosines[i] = pairs[0][1]
                        elif pairs and len(pairs) >= 2:
                            near_topics[i] = pairs[1][0]
                            near_cosines[i] = pairs[1][1]
                    if not topics:
                        continue
                    kept = [
                        tp
                        for tp in topics
                        if not (tp in TOPIC_GEO_BLOCKLIST and text_matches_geo_blocklist(txt, tp))
                    ]
                    if kept:
                        topic_lists[i] = sorted(kept)
                        rescued += 1
                        if layers is not None:
                            layers[i] = LAYER_EMBEDDING
                out["topic_groups"] = topic_lists
                if layers is not None:
                    out["classification_layer"] = layers
                if near_miss_on:
                    out["near_topic"] = near_topics
                    out["near_cosine"] = near_cosines
                logger.info(
                    "Embedding rescue: %d/%d residual rows classified for market=%s",
                    rescued,
                    len(residual),
                    market,
                )
            except Exception as exc:
                logger.warning("embedding rescue failed for market=%s: %s", market, exc)

    logger.info(
        "Enriched %d rows for market=%s (topic_groups=%d regional_markers=%d genz_markers=%d slang_terms=%d watchlist=%d)",
        len(out),
        market,
        len(topic_groups),
        len(regional_markers),
        len(genz_markers),
        sum(len((c or {}).get("terms") or []) for c in slang_config.values()),
        len(watchlist_lookup),
    )

    # Watchlist match summary. CI logs use this to verify the creator
    # join actually fired today. Pre-28 May this signal was 0 across
    # ~58/60 topics because cross-platform fallback did not exist;
    # surfacing per-tier counts makes a future regression obvious in
    # one line. unique_handles is the distinct count of normalised
    # handles that matched, regardless of how many rows each appears in.
    tier_counts = {"tier_1": 0, "tier_2": 0, "tier_3": 0}
    matched_handles: set[str] = set()
    for tier, handle in zip(out["creator_watchlist_tier"], out["author_handle_norm"], strict=False):
        if tier in tier_counts:
            tier_counts[tier] += 1
            if handle:
                matched_handles.add(handle)
    logger.info(
        "Watchlist matches today: market=%s tier_1=%d tier_2=%d tier_3=%d unique_handles=%d",
        market,
        tier_counts["tier_1"],
        tier_counts["tier_2"],
        tier_counts["tier_3"],
        len(matched_handles),
    )

    return out
