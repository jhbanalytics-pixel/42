"""Publish gate for morning trend cards (TRUST.md section 2, Stage 1A rules of section 4).

Public API:

    gate_card(card, ctx) -> Decision
    market_banner(decisions) -> str | None

card: one item_state row for one market (DATA.md section 3.7), read for state, source market
    counts, sponsored_share, hashtags and canonical_key.
ctx: a dict with these keys (political and corroborated_unbiased may be missing):
    valid_days             the last three market-days on the item's main platform, each True (valid),
                           False (invalid) or None (no history yet, which is warm-up, not invalid)
    lane_classes           the lane classes the item was seen in (DATA.md section 3.2)
    campaign_hashtags      the Ogilvy and client campaign hashtag list
    political              False when the item is classified as not political; anything else,
                           including a missing key or None, is treated as political
    corroborated_unbiased  True when the item is Corroborated in an unbiased lane; anything else is not
    explanation_passed     True when its explanation passed claim checks after one repair round, or, when the
                           critic held the morning explanation only because its why-now was not shown, after the
                           one second draft with the critic's reason, every check run again in full and no further
                           repair (TRUST.md section 3 step 8; G10 in section 2)

Rules, in the order they are applied (the first hold wins):
    G1  any invalid day: held, flag "Data issue"
    G3  seen in no measured lane (unbiased_rank, unbiased_counter, panel): held, "Found by search"
    G6  source market share at or under 0.5: held as global; missing or inconsistent evidence is held,
        flag "Market unconfirmed". A card whose locality_basis is locality_v2.1 is read by the retained locality
        row instead (C4 v3 section 11.2): not_local is held as global with its counts, local and market_unconfirmed
        pass (flag "Market unconfirmed" unless the W8-DEC-17 label is local), and a row that cannot be read is held
        by G1 as a data issue, never as global
    G5  sponsored or brand-owned share 0.5 or more: held, flag "Paid-led"
    G5b tag on the campaign hashtag list: held, flag "Paid-led"
    G4b political without Corroborated in an unbiased lane: held, flag "Not assessed"
    G8  state seasonal: published to moments instead of today
    G10 explanation failed claim checks (after the repair, and the second draft where step 8 allows one):
        published with numbers_only true

market_banner takes the decisions for one market's candidates and returns "Data issue" when
more than 30% of them were held by G1, else None.
"""

import math
import unicodedata
from dataclasses import dataclass

from core.trust.locality import V2_BASIS, label, read_locality, row_from_prefixed

MEASURED_LANES = {"unbiased_rank", "unbiased_counter", "panel"}


@dataclass(frozen=True)
class Decision:
    publish: bool
    where: str  # today | held_back | moments
    flag: str | None  # an EXPERIENCE.md flag word, or None
    reason: str | None  # plain words for the Held back row
    rule: str | None  # the rule that decided, or None for a clean card
    numbers_only: bool


def _held(rule, reason, flag=None):
    return Decision(
        publish=False, where="held_back", flag=flag, reason=reason, rule=rule, numbers_only=False
    )


def _tag(value):
    return unicodedata.normalize("NFKC", str(value)).strip().lstrip("#").casefold()


def gate_card(card, ctx):
    """Decide where one trend card goes. See the module docstring."""
    invalid = [i for i, ok in enumerate(ctx["valid_days"]) if ok is False]
    if invalid:
        return _held(
            "G1",
            f"Data issue: {len(invalid)} of the last 3 market-days invalid on the main platform",
            "Data issue",
        )

    lanes = set(ctx["lane_classes"])
    if not lanes & MEASURED_LANES:
        return _held("G3", "Found by search only: not yet in the feeds 42 measures every day")

    locality_flag = None
    if card.get("locality_basis") == V2_BASIS:
        verdict = _locality(card)
        if isinstance(verdict, Decision):
            return verdict
        locality_flag = verdict
    else:
        scope_hold = _pack_scope(card)
        if scope_hold is not None:
            return scope_hold

    sponsored = card.get("sponsored_share") or 0
    if sponsored >= 0.5:
        return _held("G5", f"Paid-led: sponsored or brand-owned share {sponsored:.2f}", "Paid-led")

    campaign = {_tag(t) for t in ctx["campaign_hashtags"]}
    tags = {_tag(t) for t in card.get("hashtags") or []}
    if card.get("canonical_key"):
        tags.add(_tag(card["canonical_key"]))
    on_list = sorted(tags & campaign)
    if on_list:
        return _held("G5b", f"Paid-led: #{on_list[0]} is on the campaign hashtag list", "Paid-led")

    if ctx.get("political") is not False and ctx.get("corroborated_unbiased") is not True:
        return _held(
            "G4b",
            "Not assessed: political topic that no neutral source has confirmed yet",
            "Not assessed",
        )

    rules, notes, flag, where = [], [], locality_flag, "today"
    if card.get("state") == "seasonal":
        rules.append("G8")
        where = "moments"
        notes.append(f"Seasonal: {card.get('moment') or 'last-year match'}")
    numbers_only = ctx["explanation_passed"] is not True
    if numbers_only:
        rules.append("G10")
        notes.append("Explanation failed claim checks: numbers and posts only")
    return Decision(
        publish=True,
        where=where,
        flag=flag,
        reason="; ".join(notes) or None,
        rule=rules[0] if rules else None,
        numbers_only=numbers_only,
    )


def _locality(card):
    """G6 for a card admitted under locality_v2.1: a Decision that holds it, or the flag it carries (None, or
    "Market unconfirmed" when the W8-DEC-17 label is not local). The retained row is read by the one reader from its
    counts; nothing it says about itself is taken, and the pack fields of the card decide nothing here."""
    got = read_locality(row_from_prefixed(card))
    if got.status == "unreadable":
        return _held("G1", "Evidence could not be read", "Data issue")
    if got.status == "not_local":
        return _held(
            "G6",
            f"Not local: {got.local} of {got.known} located posts in the last 7 days were in this market",
        )
    return None if label(got.status, got.known, got.local) == "local" else "Market unconfirmed"


def _pack_scope(card):
    """G6 as written for v1: the pack scope of the card's own source posts. A Decision that holds the card, or None."""
    market_scope = card.get("market_scope")
    market_posts7 = card.get("market_posts7")
    total_posts7 = card.get("total_posts7")
    market_share7 = card.get("market_share7")
    valid_counts = (
        type(market_posts7) is int
        and type(total_posts7) is int
        and total_posts7 > 0
        and 0 <= market_posts7 <= total_posts7
    )
    valid_share = (
        type(market_share7) in (int, float)
        and 0 <= market_share7 <= 1
        and math.isfinite(market_share7)
    )
    if not valid_counts or not valid_share or market_scope not in ("market", "global"):
        return _held(
            "G6",
            "Market unconfirmed: source market evidence is missing or invalid",
            "Market unconfirmed",
        )
    expected_share = market_posts7 / total_posts7
    expected_scope = "market" if 2 * market_posts7 > total_posts7 else "global"
    if market_scope != expected_scope or not math.isclose(
        market_share7, expected_share, rel_tol=0.0, abs_tol=1e-9
    ):
        return _held(
            "G6",
            "Market unconfirmed: source market evidence is inconsistent",
            "Market unconfirmed",
        )
    if market_scope == "global":
        return _held(
            "G6",
            f"Global: {market_posts7} of {total_posts7} card source posts in the last 7 days were located in this market or came from its feeds",
        )
    return None


def market_banner(decisions):
    """Return "Data issue" when over 30% of one market's candidates were held for data reasons."""
    decisions = list(decisions)
    held = sum(1 for d in decisions if d.rule == "G1")
    return "Data issue" if decisions and held > 0.3 * len(decisions) else None
