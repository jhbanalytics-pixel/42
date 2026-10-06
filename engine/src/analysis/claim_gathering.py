"""Brief-claim gathering for the PULSE Intelligence Core reconcile stage.

Pulled out of scripts/run_rss_now.py so a read-only reporting script can
import this small, pure-Python surface without pulling in run_rss_now's full
connector import graph (yt-dlp, praw, google-auth transports, ...), which has
been observed to crash the process (Windows access violation) when combined
with a live BigQuery client in the same process. run_rss_now.py imports these
names from here so its own module namespace and existing call sites/tests are
unchanged.

The claim surface is what the digest RENDERS: every brief headline plus the
brief body sentences (description_rationale / trend_synthesis). Source-ref
titles are deliberately not claims: they are evidence inputs the brief was
built from (often archival social posts), not assertions a reader sees, so
reconciling them checked the wrong text. The ledger-anchor filter plus
reconcile's stopword/anchor matching protect against nonsense binds, so an
unanchored editorial headline is a no-op, never a false correction.
"""

from __future__ import annotations

import re
from typing import Any

# A sentence boundary: terminal punctuation followed by whitespace and an
# upper-case/digit start. Deliberately simple (no NLP dep); an abbreviation
# like "Dr." may over-split, and the token bounds below absorb that.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")

# Body sentences shorter than this carry too little entity signal to anchor
# ("Fans are hopeful."), longer than the max are run-on narrative.
_MIN_SENTENCE_TOKENS = 6
_MAX_SENTENCE_TOKENS = 40

# Bound the claim list per market so the reconcile_actions table and the
# shadow digest stay readable on a verbose day.
_MAX_CLAIMS_PER_MARKET = 40


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_BOUNDARY.split(str(text or "").strip()) if s.strip()]


def _sentence_is_claim_sized(sentence: str) -> bool:
    n = len(sentence.split())
    return _MIN_SENTENCE_TOKENS <= n <= _MAX_SENTENCE_TOKENS


def _body_sentences_for_reconcile(brief: dict[str, Any]) -> list[str]:
    """The brief body sentences a reader sees, deduped, claim-sized."""
    out: list[str] = []
    seen: set[str] = set()
    for field in ("description_rationale", "trend_synthesis"):
        for sentence in _split_sentences(str(brief.get(field) or "")):
            if sentence in seen or not _sentence_is_claim_sized(sentence):
                continue
            seen.add(sentence)
            out.append(sentence)
    return out


def _reconcile_claims_for_market(
    market: str, briefs_by_topic: dict[tuple[str, str], dict[str, Any]]
) -> list[str]:
    """Gather the per-topic brief claims to reconcile for one market.

    Headlines first (the highest-visibility claim), then body sentences in
    brief order, deduped, capped at _MAX_CLAIMS_PER_MARKET.
    """
    claims: list[str] = []
    seen: set[str] = set()
    for brief in briefs_by_topic.values():
        if str(brief.get("market") or "") != market:
            continue
        headline = str(brief.get("headline") or "").strip()
        if headline and headline not in seen:
            seen.add(headline)
            claims.append(headline)
        for sentence in _body_sentences_for_reconcile(brief):
            if sentence in seen:
                continue
            seen.add(sentence)
            claims.append(sentence)
    return claims[:_MAX_CLAIMS_PER_MARKET]


def _filter_claims_to_ledger(claims: list[str], ledger: list[dict[str, Any]]) -> list[str]:
    """Drop claims that cannot anchor to any built ledger event for this market."""
    if not claims or not ledger:
        return list(claims)
    from src.analysis.reconcile import _match_event

    return [claim for claim in claims if _match_event(claim, ledger)[0] is not None]


__all__ = [
    "_body_sentences_for_reconcile",
    "_filter_claims_to_ledger",
    "_reconcile_claims_for_market",
]
