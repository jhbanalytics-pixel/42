"""Ask's depth levels: what each step of the depth slider runs, the most it may spend and whether it is open.

The slider in the app picks one level and sends its tier. Every number here comes from the tier's own caps in
context.TIERS and the day's shared ask credits (ASK_DAILY), so the app never shows a cost the agent would not hold to.
A level is a ceiling, not a price: an ask stops when its answer is ready and usually spends less.

Deep sends three to five researchers in parallel (news first on a figures question), then the critic. It is open to a
plain ask only while f42-agent has both F42_T2_READY and F42_ASK_DEEP set to "1"; the brand lens and the context pack
keep their own T2 rule (contract section 15.2). Investigations (T3) stay on their own plan screen.
"""

from __future__ import annotations

import os

from core.agent.context import TIERS

DEFAULT_ASK_DAILY = 600  # credits, all asks and investigations together (SETUP.md caps table)

# cost: how the app flags the level. "highest" is the level the slider sets alight.
LEVELS = (
    {"key": "quick", "tier": "T0", "label": "Quick", "cost": "low", "max_researchers": 1,
     "reads": "42's stored posts, with a few small live checks"},
    {"key": "standard", "tier": "T1", "label": "Standard", "cost": "normal", "max_researchers": 1,
     "reads": "Stored posts plus live searches on the platforms the question names"},
    {"key": "deep", "tier": "T2", "label": "Deep", "cost": "highest", "max_researchers": 5,
     "reads": "Live news and every platform group in parallel, full news articles, then a critic"},
)


def ask_daily() -> float:
    try:
        return float(os.environ.get("ASK_DAILY") or DEFAULT_ASK_DAILY)
    except ValueError:
        return float(DEFAULT_ASK_DAILY)


def deep_open() -> bool:
    """Deep is open to a plain ask only with both switches on; anything but exactly "1" keeps it closed."""
    return os.environ.get("F42_T2_READY") == "1" and os.environ.get("F42_ASK_DEEP") == "1"


def max_model_usd(tier: str, model: str) -> float:
    """The model dollars an ask at this tier holds before it starts: research, writing, checks and review, plus the
    figure-listing call a figures question adds (ask.held)."""
    from core.agent import ask

    return round(ask.hold_usd(tier, model) + ask.figures_usd(model), 2)


def levels(deep: bool, daily: float | None = None, model: str | None = None) -> list[dict]:
    """The slider's steps, lowest first, each with its ceilings, its share of the day's ask credits and whether the
    service takes it now."""
    from core.agent import ask

    daily = ask_daily() if daily is None else daily
    model = model or ask.MODEL
    out = []
    for level in LEVELS:
        tier = TIERS[level["tier"]]
        is_open = level["tier"] != "T2" or deep
        out.append({
            **level,
            "max_credits": tier["credits"],
            "max_live_calls": tier["calls"],
            "max_model_usd": max_model_usd(level["tier"], model),
            "share_of_daily": round(tier["credits"] / daily, 3) if daily > 0 else None,
            "open": is_open,
            "closed_reason": None if is_open else "Deep is switched off on this service.",
        })
    return out
