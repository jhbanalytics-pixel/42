"""Pure display-layer functions for the PULSE v2 mailer.

These compute the per-trend display fields (state badge, phase, act window,
in-market geo share, channel weights, search read, confidence) from data the
engine already stores. No I/O, no new signals. Renderers display these values,
they never compute or invent them.

Thresholds here are starting points to validate against real briefs during
rollout, the same discipline as the D2 embedding gate.
"""

from src.utils.geo_blocklist import (
    has_foreign_latin_density,
    has_non_ssa_script,
    text_matches_geo_blocklist,
)


def trend_state(score: float, prior_score: float | None) -> dict:
    """Per-trend momentum badge + direction, today vs the prior day.

    No "Day N" counter: it was dead (always 1, no caller passed it), and a real
    one would be useless here anyway, every topic is scored every day, so a day
    count prints the topic's age, not a trend signal. The badge states the
    direction plainly; the +/-0.03 deadband stops a flat topic from flapping.
    """
    if prior_score is None:
        return {"badge": "New on the board", "direction": "new"}
    delta = score - prior_score
    if delta > 0.03:
        return {"badge": "Building", "direction": "up"}
    if delta < -0.03:
        return {"badge": "Cooling", "direction": "down"}
    return {"badge": "Holding", "direction": "flat"}


def trend_phase(score: float, velocity: float) -> str:
    if velocity >= 0.30 and score >= 0.45:
        return "Peaking"
    if velocity >= 0.30:
        return "Emerging"
    if velocity <= 0.05 and score < 0.35:
        return "Cooling"
    return "Steady"


def act_window(phase: str) -> str:
    return {
        "Emerging": "wide, 1 to 2 weeks",
        "Peaking": "ride now, ~2 weeks",
        "Steady": "build, 2 to 3 weeks",
        "Cooling": "closing",
    }.get(phase, "build")


def in_market_pct(rows: list[dict], market: str, topic: str) -> int:
    """Share of the sample that passes the foreign-content filter, 0-100.

    NOT a geo-trust or confidence score. It counts rows not flagged foreign
    (non-SSA script, foreign-Latin density, or a per-topic geo-blocklist hit).
    On a topic with no per-topic blocklist only the generic script/density
    filters apply, so it sits near 100; that is why the chip is labelled LOCAL
    (a filter pass-rate), not in-market confidence.
    """
    if not rows:
        return 0
    inm = 0
    for r in rows:
        hay = " ".join(
            [str(r.get("title") or ""), str(r.get("text") or ""), str(r.get("hashtags") or "")]
        )
        foreign = (
            has_non_ssa_script(hay)
            or has_foreign_latin_density(hay)
            or text_matches_geo_blocklist(hay, topic)
        )
        if not foreign:
            inm += 1
    return round(100 * inm / len(rows))


def channel_weights(platform_counts: dict[str, int]) -> list[tuple[str, int]]:
    if not platform_counts:
        return []
    mx = max(platform_counts.values()) or 1
    return sorted(
        [(p, max(1, round(4 * c / mx))) for p, c in platform_counts.items()],
        key=lambda kv: -platform_counts[kv[0]],
    )


def confidence_label(n_platforms: int) -> str:
    """An honest channel-count read, never the word "confirmed".

    Nick's standing point: "Confirmed across N channels" is a client-facing
    liability the engine cannot defend, so the label states the count plainly
    and lets the human make the call. Single source is named as such so a
    one-platform spike never reads as corroborated.
    """
    if n_platforms <= 1:
        return "single-source"
    return f"{n_platforms} channels agree"


def search_read(velocity_7d: float | None) -> str:
    if velocity_7d is None:
        return "no lift"
    return "rising" if velocity_7d > 0.1 else "flat"
