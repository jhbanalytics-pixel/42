"""Lexicon term detail for 42.

A single slang term's living evidence: the real posts the engine detected it in,
newest first. Counts and definitions live elsewhere (the desk lexicon and the
client glossary); this serves the posts that prove the term is alive.
"""

from src.api import bq


def _post_age_key(p):
    hours = bq._age_hours(bq.best_stamp(p))
    return hours if hours is not None else 1e12


def _wall_card(p):
    return {
        "id": p.get("id"),
        "text": p.get("text"),
        "platform": bq._platform_label(p.get("platform")),
        "market": (p.get("market") or "").upper(),
        "engagement": int(p.get("engagement") or 0),
        "age": bq.age_label(bq.best_stamp(p)),
    }


def build_lexicon_term(term: str, region: str) -> dict:
    """One term's post wall over 30 days, cleaned and newest first."""
    market = (region or "all").strip().lower()
    cleaned = bq.clean_items(bq.fetch_lexicon_term_posts(term, market))
    cleaned.sort(key=_post_age_key)
    posts = [_wall_card(p) for p in cleaned]
    return {"term": term, "post_count": len(posts), "posts": posts}
