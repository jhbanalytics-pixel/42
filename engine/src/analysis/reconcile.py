"""RECONCILE: the deterministic trust boundary of the PULSE Intelligence Core.

This module is 100% pure deterministic code. It calls NO Gemini and imports
nothing from the event ledger. It consumes a plain list of claim strings (the
sentences a brief or daily summary asserts) and a plain list of event dicts
(the event-ledger contract) and decides, per claim, whether the claim still
holds against the freshest evidence the ledger carries.

Why this lives in deterministic code, not in the model:

A model can be told "do not contradict the ledger" and still drift, because the
instruction is soft. The trust boundary has to be a hard rule. So the only
state-changing powers reconcile holds are narrow and mechanical:

    corroborated  the claim matches a fresh ledger state that agrees with it
    stale         the claim is contradicted by a newer, resolved ledger state
    labelled      the claim is contradicted but the evidence is too thin to
                  correct, so the claim is left as-is with a date-stamp note
    no_match      no ledger event anchors to the claim; the claim is untouched

reconcile NEVER rewrites a claim to free text. The only text it can ever put in
claim_after is the matched event's ``state_text`` verbatim, and only when the
require-a-source hard gate passes. ``state_text`` was itself evidence-cited
upstream when the ledger was built, so a correction is always traceable.

The event-identity hard gate: anchoring proves the claim and the event name the
same ENTITY, not the same EVENT. A person or a country appears in several
unrelated stories on one day, so a correction also requires the claim and the
event's state_text to agree on the kind of event they describe, or to share
real topical vocabulary beyond the anchor. Unknown identity is labelled, never
corrected (see _event_identity_confirmed).

The require-a-source hard gate: a contradicted claim is corrected only when the
matched event carries at least two independent FACTUAL channel families
(news, search, youtube, music) in its corroborating_sources AND its confidence
clears a floor. Anything less is labelled, never corrected. Social chatter
alone (brand24) can flag a claim as worth a second look but can never overturn
the wording of a brief on its own.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

# Channel families that count as factual corroboration. Social-listening
# (brand24) deliberately does NOT count: a viral post is not a verified fact.
_FACTUAL_FAMILIES: frozenset[str] = frozenset(
    {
        "news",
        "search",
        "youtube",
        "music",
        # event_ledger persists gdelt/rss/trends family labels; reconcile must
        # accept them or the factual count against a real ledger stays 0.
        "gdelt",
        "rss",
        "trends",
    }
)

# A correction needs at least this many distinct factual families.
_MIN_FACTUAL_SOURCES: int = 2

# A correction needs the matched event's confidence at or above this floor.
# Below the floor the action is downgraded from a correction to a label, even
# when the factual-source count is met.
_CONFIDENCE_FLOOR: float = 0.55

# Freshness window, in days, before the issue_date. A resolved event whose
# as_of lands inside this window (or on/after issue_date) is fresher truth than
# the claim, which was written against an earlier data pull. An event older
# than the window is stale itself and cannot overturn the claim. Sized to cover
# a brief that reads against roughly the trailing week of data.
_FRESHNESS_WINDOW_DAYS: int = 7

# Markers in a claim that imply a future / scheduled / not-yet-happened event.
# A claim carrying one of these against a ledger event marked resolved is the
# core stale signal: the brief is still talking about something the ledger says
# already happened.
_FUTURE_MARKERS: tuple[str, ...] = (
    "vs",
    "upcoming",
    "to face",
    "this weekend",
    "will",
    "ahead of",
    "preparing for",
    "scheduled",
)

# Words a headline capitalises mid-sentence that then survive proper-noun
# extraction and end up as one-word ledger entity keys. Observed live as
# matched_entity_key values over 2026-07-28..2026-08-03 ('live' 14, 'life' 12,
# 'what' 5, 'daily' 5, 'these' 4, 'free' 3, 'digital' 3), each one binding any
# claim that happens to use the word. They are English filler, never an event
# anchor, so anchor matching must refuse them the same way it refuses "the".
_NON_ANCHOR_TOKENS: frozenset[str] = frozenset(
    {
        "live",
        "life",
        "living",
        "what",
        "when",
        "where",
        "why",
        "how",
        "who",
        "these",
        "those",
        "there",
        "here",
        "daily",
        "weekly",
        "today",
        "tomorrow",
        "yesterday",
        "week",
        "year",
        "day",
        "free",
        "digital",
        "online",
        "watch",
        "full",
        "best",
        "top",
        "more",
        "most",
        "first",
        "last",
        "real",
        "now",
        "good",
        "great",
        "big",
        "story",
        "news",
        "video",
        "people",
        "you",
        "your",
        "they",
        "them",
        "their",
        "our",
        "we",
        "his",
        "her",
        "its",
        "it",
        "he",
        "she",
        "but",
        "not",
        "all",
        "can",
        "has",
        "have",
        "had",
        "been",
        "after",
        "before",
        "over",
        "into",
        "out",
        "up",
        "down",
        "about",
    }
)

# Tokens that carry no entity signal. An anchor match on one of these is not a
# real match: "south" or "the" appearing in a claim must not bind a ledger
# event keyed on a multi-word entity. Anchor matching ignores these.
_STOPWORDS: frozenset[str] = _NON_ANCHOR_TOKENS | frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "and",
        "or",
        "vs",
        "with",
        "against",
        "match",
        "this",
        "that",
        "is",
        "are",
        "was",
        "were",
        "be",
        "as",
        "by",
        "from",
        "turns",
        "attention",
        "upcoming",
        "south",
        "north",
        "new",
        "next",
    }
)

# EVENT IDENTITY. An anchor match proves the claim and the ledger event talk
# about the same ENTITY. It does not prove they talk about the same EVENT: a
# president appears in a commemoration story and an energy story on the same
# day, and correcting one with the other replaces a true claim with an
# unrelated headline (observed live 2026-08-03 za and 2026-07-27 za). Before a
# correction is allowed, the claim and the event's state_text must agree on
# what KIND of event they describe, or share real topical vocabulary.
#
# Frames are deliberately narrow and lexical. A word that could belong to any
# story (rose, hit, top) is left out: an unmatched frame costs a label, never a
# wrong correction.
_EVENT_FRAMES: dict[str, frozenset[str]] = {
    "fixture": frozenset(
        {
            "match",
            "matches",
            "fixture",
            "fixtures",
            "vs",
            "versus",
            "against",
            "tie",
            "clash",
            "derby",
            "qualifier",
            "qualifiers",
            "qualifying",
            "final",
            "finals",
            "semifinal",
            "friendly",
            "kickoff",
            "played",
            "play",
            "plays",
            "playing",
            "face",
            "faces",
            "facing",
            "beat",
            "beats",
            "beaten",
            "won",
            "win",
            "wins",
            "defeat",
            "defeats",
            "defeated",
            "draw",
            "drew",
            "scored",
            "scoreline",
            "goal",
            "goals",
            "squad",
            "lineup",
            "highlights",
            "tournament",
            "league",
            "series",
            "leg",
        }
    ),
    "speech": frozenset(
        {
            "address",
            "addresses",
            "addressed",
            "addressing",
            "speak",
            "speaks",
            "speaking",
            "speech",
            "told",
            "tells",
            "said",
            "says",
            "statement",
            "announce",
            "announces",
            "announced",
            "announcement",
            "briefing",
            "remarks",
            "declared",
            "declares",
            "urged",
            "urges",
            "warned",
            "warns",
            "confirms",
            "confirmed",
            "interview",
            "conference",
            "sona",
        }
    ),
    "election": frozenset(
        {
            "election",
            "elections",
            "elected",
            "vote",
            "votes",
            "voting",
            "voters",
            "poll",
            "polls",
            "ballot",
            "candidate",
            "candidates",
            "campaign",
            "inauguration",
            "sworn",
        }
    ),
    "release": frozenset(
        {
            "release",
            "releases",
            "released",
            "album",
            "single",
            "ep",
            "mixtape",
            "premiere",
            "premieres",
            "launch",
            "launches",
            "launched",
            "debut",
            "unveil",
            "unveils",
            "unveiled",
            "drops",
            "dropped",
        }
    ),
    "legal": frozenset(
        {
            "court",
            "trial",
            "verdict",
            "ruling",
            "ruled",
            "sentenced",
            "sentencing",
            "convicted",
            "conviction",
            "charged",
            "charges",
            "hearing",
            "judgment",
            "judgement",
            "appeal",
            "arrested",
            "arrest",
            "prosecution",
        }
    ),
    "award": frozenset(
        {
            "award",
            "awards",
            "nominated",
            "nomination",
            "nominations",
            "winner",
            "winners",
            "ceremony",
            "crowned",
            "honours",
            "honors",
            "grammy",
            "grammys",
        }
    ),
    "appointment": frozenset(
        {
            "appointed",
            "appoints",
            "appointment",
            "resigned",
            "resigns",
            "resignation",
            "sacked",
            "fired",
            "reshuffle",
            "named",
            "steps",
            "successor",
        }
    ),
}

# Tokens too common in SSA market copy to prove two texts share a topic. They
# are legitimate words, so they stay matchable as part of a multi-word anchor;
# they just cannot COUNT as topical agreement on their own.
_LOW_SIGNAL_TOKENS: frozenset[str] = frozenset(
    {
        "africa",
        "african",
        "africans",
        "nigeria",
        "nigerian",
        "nigerians",
        "kenya",
        "kenyan",
        "kenyans",
        "mzansi",
        "naija",
        "social",
        "media",
        "content",
        "conversation",
        "audience",
        "brand",
        "brands",
        "market",
        "markets",
        "trend",
        "trends",
        "trending",
        "viral",
        "creator",
        "creators",
        "platform",
        "platforms",
        "major",
        "national",
        "country",
        "report",
        "reports",
        "latest",
        "significant",
        "using",
        "such",
        "also",
    }
)

# A shared distinctive token is weak evidence on its own (one name can recur
# across unrelated stories). Two independent shared tokens is the floor for
# calling it the same event on vocabulary alone.
_MIN_SHARED_TOPIC_TOKENS: int = 2

# Frame agreement is only trustworthy when one side has little vocabulary to
# compare. "Attention turns to the upcoming match against South Korea" carries
# nothing distinctive beyond the anchor, so the fact that both texts describe a
# fixture is the only signal available and is worth taking. Two texts that are
# both rich and still share almost nothing are the opposite case: their
# vocabulary is actively disagreeing, and a shared frame is just the observation
# that both are football (observed live 2026-08-17 ke, where a fan-caravan story
# was overwritten by an unrelated video-game match result on the strength of
# both texts naming a fixture). At or below this many distinctive tokens on its
# leaner side, a pair counts as too thin to judge on vocabulary and falls back
# to the frame.
#
# CALIBRATED, not derived. The two live cases it separates sit at 4 (a genuine
# Ramaphosa speech correction, which must still fire) and 9 (the ke fixture
# collision, which must not). 5 is the midpoint with room either side. Re-tune it
# when a third case lands rather than defending the number. Getting it wrong high
# costs a correction that degrades to a label, which is the cheap direction;
# getting it wrong low overwrites a true claim, which is the expensive one.
_THIN_TOPIC_TOKENS: int = 5

# Confidence tier labels surfaced on every reconcile action so a downstream
# renderer / watchdog can sort by how much weight to give the decision.
_TIER_HIGH = "high"
_TIER_MEDIUM = "medium"
_TIER_LOW = "low"
_TIER_NONE = "none"


# ---------------------------------------------------------------------------
# Tokenization + matching helpers (pure)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace. The single normalization entry point."""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _tokens(text: str) -> list[str]:
    """Lowercased alphanumeric token list."""
    return _TOKEN_RE.findall(_normalize(text))


def _content_token_set(text: str) -> set[str]:
    """Tokens with stopwords removed: the set used for anchor matching."""
    return {t for t in _tokens(text) if t not in _STOPWORDS}


def _anchor_phrases(event: dict[str, Any]) -> list[str]:
    """Candidate anchor phrases for an event: its entity_key plus every alias.

    Each phrase is normalized. Empty phrases are dropped.
    """
    phrases: list[str] = []
    key = _normalize(event.get("entity_key", ""))
    if key:
        phrases.append(key)
    for alias in event.get("entity_aliases") or []:
        norm = _normalize(alias)
        if norm:
            phrases.append(norm)
    return phrases


def _phrase_matches_claim(phrase: str, claim_tokens: list[str], claim_token_set: set[str]) -> bool:
    """True when ``phrase`` appears in the claim as specific (non-stopword) tokens.

    A single-token anchor must appear as a content token in the claim (a
    stopword anchor can never match, by construction of the content set). A
    multi-token anchor must appear as a contiguous run in the claim's token
    list, so "south korea" matches only when both words sit next to each other,
    not when "south" appears alone elsewhere.
    """
    phrase_tokens = _tokens(phrase)
    if not phrase_tokens:
        return False

    if len(phrase_tokens) == 1:
        tok = phrase_tokens[0]
        if tok in _STOPWORDS:
            return False
        return tok in claim_token_set

    # Multi-token: require a contiguous subsequence match in the claim tokens.
    n = len(phrase_tokens)
    return any(claim_tokens[i : i + n] == phrase_tokens for i in range(len(claim_tokens) - n + 1))


def _match_event(claim: str, ledger: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Return (best-matching ledger event, the anchor phrase that matched).

    A match requires anchor-entity overlap: the event's entity_key or one of its
    aliases must appear in the claim as a specific (non-stopword) token run.
    When several events match, the one with the longest matched anchor phrase
    wins (most specific), confidence breaks ties. The matched phrase comes back
    with the event so the identity gate knows which tokens are the ANCHOR (and
    therefore prove nothing about event identity) and which are topic.

    Returns (None, "") when nothing anchors.
    """
    claim_tokens = _tokens(claim)
    claim_token_set = _content_token_set(claim)
    if not claim_token_set:
        return None, ""

    best: dict[str, Any] | None = None
    best_phrase = ""
    best_len = -1
    best_conf = -1.0
    for event in ledger:
        matched_len = 0
        matched_phrase = ""
        for phrase in _anchor_phrases(event):
            if _phrase_matches_claim(phrase, claim_tokens, claim_token_set):
                phrase_len = len(_tokens(phrase))
                if phrase_len > matched_len:
                    matched_len = phrase_len
                    matched_phrase = phrase
        if matched_len == 0:
            continue
        conf = _as_float(event.get("confidence"), 0.0)
        if matched_len > best_len or (matched_len == best_len and conf > best_conf):
            best = event
            best_phrase = matched_phrase
            best_len = matched_len
            best_conf = conf
    return best, best_phrase


# ---------------------------------------------------------------------------
# Event identity (pure)
# ---------------------------------------------------------------------------


def _event_frames(text: str) -> set[str]:
    """The event frames a text names (fixture / speech / election / ...).

    Empty when the text uses no frame vocabulary, which is treated as "we do
    not know what kind of event this is", never as agreement.
    """
    tokens = set(_tokens(text))
    return {frame for frame, lexicon in _EVENT_FRAMES.items() if tokens & lexicon}


def _topic_tokens(text: str, anchor_tokens: set[str]) -> set[str]:
    """Distinctive content tokens of a text, excluding the anchor itself.

    The anchor is what made the two texts meet, so it can never be evidence
    that they describe the same event. Stopwords, low-signal market vocabulary
    and short tokens are dropped for the same reason.
    """
    return {
        tok
        for tok in _tokens(text)
        if tok not in _STOPWORDS
        and tok not in _LOW_SIGNAL_TOKENS
        and tok not in anchor_tokens
        and len(tok) >= 4
    }


def _event_identity_confirmed(claim: str, event: dict[str, Any], matched_phrase: str) -> bool:
    """True when the claim and the event's state_text describe the SAME event.

    Two signals, in order:

    topical overlap   at least _MIN_SHARED_TOPIC_TOKENS distinctive words in
                      common beyond the anchor entity itself. Sufficient on its
                      own.
    frame agreement   both texts name the same kind of event (a fixture, a
                      speech, a court ruling). Sufficient ONLY when one side is
                      thin, meaning it carries at most _THIN_TOPIC_TOKENS
                      distinctive words and so offers no vocabulary to compare.
                      A frame is a kind of event, not an identity: two different
                      fixtures involving the same club agree on frame and are
                      still different events.

    Neither signal means event identity is unknown, and an unknown identity is
    never allowed to overwrite a claim.
    """
    state_text = str(event.get("state_text") or "")
    if not state_text.strip():
        return False

    anchor_tokens = set(_tokens(matched_phrase)) | set(_tokens(event.get("entity_key", "")))

    claim_topics = _topic_tokens(claim, anchor_tokens)
    state_topics = _topic_tokens(state_text, anchor_tokens)

    if len(claim_topics & state_topics) >= _MIN_SHARED_TOPIC_TOKENS:
        return True

    if min(len(claim_topics), len(state_topics)) > _THIN_TOPIC_TOKENS:
        return False

    return bool(_event_frames(claim) & _event_frames(state_text))


# ---------------------------------------------------------------------------
# Date + state helpers (pure)
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date | None:
    """Parse an ISO date / datetime string to a date. Returns None on failure."""
    raw = str(value or "").strip()
    if not raw:
        return None
    # Tolerate a trailing Z and full datetimes.
    candidate = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _claim_implies_future(claim: str) -> bool:
    """True when the claim carries a future / scheduled implication.

    Markers are matched on word boundaries so "vs" does not fire inside
    "versatile" and "will" does not fire inside "willing".
    """
    norm = _normalize(claim)
    return any(re.search(rf"\b{re.escape(marker)}\b", norm) for marker in _FUTURE_MARKERS)


def _event_is_fresher(event: dict[str, Any], issue_date: str) -> bool:
    """True when the event's as_of is recent relative to the claim's reference.

    The claim's reference time is the issue_date, and the claim was written
    against a trailing data window ending at issue_date. A resolved event whose
    as_of lands inside that window (issue_date minus the freshness window) or on
    or after issue_date is fresher truth than the claim. An event older than the
    window is itself stale and cannot overturn the claim. When either date is
    unparseable we fail safe: no freshness, so no stale correction fires off a
    bad date.
    """
    event_date = _parse_date(event.get("as_of", ""))
    ref_date = _parse_date(issue_date)
    if event_date is None or ref_date is None:
        return False
    window_start = ref_date - timedelta(days=_FRESHNESS_WINDOW_DAYS)
    return event_date >= window_start


def _factual_source_count(sources: list[str]) -> int:
    """Count of distinct factual channel families in a corroborating-source list."""
    seen = {_normalize(s) for s in (sources or [])}
    return len(seen & _FACTUAL_FAMILIES)


def _confidence_tier(event: dict[str, Any] | None) -> str:
    if event is None:
        return _TIER_NONE
    conf = _as_float(event.get("confidence"), 0.0)
    factual = _factual_source_count(event.get("corroborating_sources") or [])
    if conf >= 0.75 and factual >= _MIN_FACTUAL_SOURCES:
        return _TIER_HIGH
    if conf >= _CONFIDENCE_FLOOR and factual >= 1:
        return _TIER_MEDIUM
    return _TIER_LOW


def _correction_allowed(event: dict[str, Any]) -> bool:
    """The require-a-source hard gate for any correction.

    A contradicted claim is corrected only when the matched event has at least
    two independent FACTUAL channel families AND confidence at/above the floor.
    Otherwise the claim is labelled, never corrected.
    """
    factual = _factual_source_count(event.get("corroborating_sources") or [])
    conf = _as_float(event.get("confidence"), 0.0)
    return factual >= _MIN_FACTUAL_SOURCES and conf >= _CONFIDENCE_FLOOR


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def reconcile_claims(
    claims: list[str],
    ledger: list[dict[str, Any]],
    issue_date: str,
) -> list[dict[str, Any]]:
    """Reconcile each claim against the event ledger. Pure, no side effects.

    Args:
        claims: the sentences a brief / summary asserts.
        ledger: event-ledger contract dicts (entity_key, entity_aliases,
            event_kind, state_label, state_text, as_of, corroborating_sources,
            source_count, confidence).
        issue_date: ISO date the claim was written against (its reference time).

    Returns one dict per claim, in input order, with keys exactly:
        claim_before, action, claim_after, matched_entity_key, receipt_ids,
        confidence_tier.
    """
    ledger = ledger or []
    results: list[dict[str, Any]] = []

    for claim in claims:
        claim_before = str(claim)
        event, matched_phrase = _match_event(claim_before, ledger)

        if event is None:
            results.append(
                {
                    "claim_before": claim_before,
                    "action": "no_match",
                    "claim_after": claim_before,
                    "matched_entity_key": "",
                    "receipt_ids": [],
                    "confidence_tier": _TIER_NONE,
                }
            )
            continue

        entity_key = _normalize(event.get("entity_key", ""))
        sources = [str(s) for s in (event.get("corroborating_sources") or [])]
        tier = _confidence_tier(event)
        state_label = _normalize(event.get("state_label", "")) or "unknown"

        # STALE detection: the claim talks about a future/scheduled thing, the
        # matched event is resolved, and the event is fresher than the claim.
        contradicted = (
            _claim_implies_future(claim_before)
            and state_label == "resolved"
            and _event_is_fresher(event, issue_date)
        )

        if contradicted:
            # The entity anchored, but a shared entity is not a shared event.
            # Without event identity the correction would swap a true claim for
            # an unrelated headline, so an unconfirmed identity is labelled.
            identity_confirmed = _event_identity_confirmed(claim_before, event, matched_phrase)
            if not identity_confirmed:
                logger.info(
                    "reconcile: correction refused for anchor '%s', event identity unconfirmed",
                    entity_key,
                )
            if identity_confirmed and _correction_allowed(event):
                # The require-a-source gate passed: correct to the ledger's
                # state_text verbatim. reconcile never invents the text.
                state_text = str(event.get("state_text") or "").strip()
                claim_after = state_text or claim_before
                action = "stale"
            else:
                # Contradicted but the identity is unconfirmed or the evidence
                # is too thin: label, never correct.
                claim_after = claim_before
                action = "labelled"
            results.append(
                {
                    "claim_before": claim_before,
                    "action": action,
                    "claim_after": claim_after,
                    "matched_entity_key": entity_key,
                    "receipt_ids": sources,
                    "confidence_tier": tier,
                }
            )
            continue

        # Not contradicted. A matched event whose state agrees with the claim
        # and is fresh is positive corroboration; otherwise the claim stands
        # untouched as a plain no-correction match (reported as corroborated
        # only when there is genuine fresh agreement).
        #
        # 'ongoing' is accepted here for forward-compatibility only: the
        # ledger's Gemini schema and deterministic patterns both only ever
        # emit resolved/scheduled/unknown today (see
        # src/analysis/prompts/event_ledger.py RESPONSE_SCHEMA), so this arm
        # of the check is currently dead on live data. Kept so a future
        # 'ongoing' state (e.g. a live match in progress) does not need a
        # reconcile change to be recognised as corroboration.
        if state_label in {"scheduled", "ongoing"} and _factual_source_count(sources) >= 1:
            results.append(
                {
                    "claim_before": claim_before,
                    "action": "corroborated",
                    "claim_after": claim_before,
                    "matched_entity_key": entity_key,
                    "receipt_ids": sources,
                    "confidence_tier": tier,
                }
            )
            continue

        # Matched an event but nothing to assert: leave the claim untouched and
        # report no_match-style neutrality via a label with no correction. We
        # keep the claim text and attach the sources so the receipt survives.
        results.append(
            {
                "claim_before": claim_before,
                "action": "labelled",
                "claim_after": claim_before,
                "matched_entity_key": entity_key,
                "receipt_ids": sources,
                "confidence_tier": tier,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Persistence (the shadow audit trail)
# ---------------------------------------------------------------------------


def persist_reconcile_actions(
    trend_date: date,
    market: str,
    run_id: str,
    actions: list[dict[str, Any]],
    bq_client: Any = None,
    dataset: str | None = None,
) -> int:
    """Persist a market's reconcile action rows to reconcile_actions.

    Maps each action dict from reconcile_claims onto the reconcile_actions
    columns and inserts via the engine's load-job insert (insert_dataframe).
    Idempotent per (trend_date, market): delete today's rows for this market,
    then insert, so a re-run for the same day overwrites cleanly. Because the
    load job (not a streaming insert) lands the rows, the DELETE is safe and we
    never post-INSERT UPDATE (the streaming-buffer gotcha).

    Returns the number of rows inserted. Wrapped in try/except so a BQ failure
    is non-fatal: it logs and returns 0 rather than breaking the caller.
    """
    try:
        import pandas as pd

        from src.utils.bigquery import get_client, get_dataset, insert_dataframe

        bq_client = bq_client or get_client()
        dataset = dataset or get_dataset()

        created_at = datetime.now(UTC).isoformat()
        rows: list[dict[str, Any]] = []
        for action in actions or []:
            rows.append(
                {
                    "action_id": str(uuid.uuid4()),
                    "trend_date": trend_date.isoformat(),
                    "market": market,
                    "run_id": run_id,
                    "source_surface": "brief",
                    "claim_before": action.get("claim_before", ""),
                    "action": action.get("action", ""),
                    "claim_after": action.get("claim_after", ""),
                    "matched_entity_key": action.get("matched_entity_key", ""),
                    "receipt_ids": [str(r) for r in (action.get("receipt_ids") or [])],
                    "confidence_tier": action.get("confidence_tier", ""),
                    "model": "",
                    "created_at": created_at,
                }
            )

        # Idempotent per (trend_date, market): clear today's rows for this
        # market before inserting. Load-job inserts (insert_dataframe) are not
        # streamed, so this DELETE never collides with the streaming buffer.
        delete_sql = (
            f"DELETE FROM `{bq_client.project}.{dataset}.reconcile_actions` "
            "WHERE trend_date = @trend_date AND market = @market"
        )
        from google.cloud import bigquery as _bq

        job_config = _bq.QueryJobConfig(
            query_parameters=[
                _bq.ScalarQueryParameter("trend_date", "DATE", trend_date),
                _bq.ScalarQueryParameter("market", "STRING", market),
            ]
        )
        bq_client.query(delete_sql, job_config=job_config).result()

        if not rows:
            return 0
        df = pd.DataFrame(rows)
        insert_dataframe(df, "reconcile_actions")
        logger.info(
            "reconcile: persisted %d actions for %s/%s",
            len(rows),
            trend_date,
            market,
        )
        return len(rows)
    except Exception as exc:
        logger.error("reconcile: persist_reconcile_actions failed (non-fatal): %s", exc)
        return 0
