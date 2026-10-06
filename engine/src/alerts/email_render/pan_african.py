"""Pan-African section for the PULSE v2 mailer (Wave 2).

The stories that are rising in two or more SSA markets at once. The engine's
Wave 2 post-scoring stage normalises topic keys to a shared family across
za/ng/ke, finds the topics moving in step, and writes one row per story to the
``pan_african_stories`` table (trend_date, story_id, story_label, markets,
topic_keys, total_item_count, momentum_composite). This section lists the top
few so the desk sees what is breaking continent-wide, not just in one market.

Dark on ship. The whole section is gated behind PAN_AFRICAN_ENABLED and renders
nothing when the flag is off, so today's email is byte-identical. Even with the
flag on it returns "" when no story row is supplied (the table is empty until the
pan-African stage runs), so a single-market day never prints empty furniture.
Every value is read straight off the stored story rows, nothing is invented.

Email-safe: each story is a presentation-table row, the market chips are inline
``bgcolor`` spans, no flex and no grid.
"""

from __future__ import annotations

import os

from src.alerts.email_render._table import CLS, FONT_BODY, FONT_MONO, palette
from src.alerts.email_render._util import esc, market_color, market_name

# Cap the list so the section stays a scannable summary of the strongest
# cross-market stories, not the full table.
_MAX_STORIES = 4


def _pan_african_enabled() -> bool:
    """Whether the pan-African section is flagged on (PAN_AFRICAN_ENABLED).

    Read per call so it flips on the live job and toggles in tests without a
    code change. Dark by default: any value outside the truthy set keeps the
    section off, so a flag-off render is byte-identical to today.
    """
    return os.environ.get("PAN_AFRICAN_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _clean_markets(raw) -> list[str]:
    """Distinct, ordered market slugs off a story's ``markets`` field.

    Accepts the stored ARRAY<STRING>. Drops blanks and duplicates while keeping
    first-seen order so the chips read za, ng, ke as written, not re-sorted.
    """
    out: list[str] = []
    # raw may be a list OR a numpy array (a BQ ARRAY<STRING> read via
    # to_dataframe), so test for None explicitly: `raw or []` raises
    # "truth value of an array is ambiguous" on a multi-element ndarray.
    if raw is None:
        return out
    for entry in raw:
        slug = str(entry or "").strip().lower()
        if slug and slug not in out:
            out.append(slug)
    return out


def _clean_stories(stories: list[dict]) -> list[dict]:
    """Keep only stories that genuinely span two or more markets.

    The pan-African contract is a 2-plus-market rule, so a row that arrives with
    fewer than two distinct markets, or no label, is dropped rather than shown as
    a single-market story. Sorted by ``momentum_composite`` descending (the
    engine's cross-market strength), ties broken by item count then label for a
    stable order.
    """
    cleaned: list[dict] = []
    for story in stories or []:
        markets = _clean_markets(story.get("markets"))
        label = str(story.get("story_label") or "").strip()
        if len(markets) < 2 or not label:
            continue
        try:
            momentum = float(story.get("momentum_composite") or 0.0)
        except (TypeError, ValueError):
            momentum = 0.0
        try:
            items = int(story.get("total_item_count") or 0)
        except (TypeError, ValueError):
            items = 0
        cleaned.append({"label": label, "markets": markets, "momentum": momentum, "items": items})
    cleaned.sort(key=lambda s: (-s["momentum"], -s["items"], s["label"]))
    return cleaned


def _market_chip(market: str, brand: dict, pal: dict) -> str:
    """One market chip: a coloured dot and the market name, dark-section safe."""
    color = market_color(market, brand)
    name = market_name(market)
    return (
        f'<span class="{CLS["ink2"]}" style="display:inline-block;font-family:{FONT_MONO};font-size:9px;'
        f"font-weight:700;letter-spacing:0.06em;text-transform:uppercase;"
        f'color:{pal["on_ink_soft"]};margin-right:10px;white-space:nowrap;">'
        f'<span style="display:inline-block;width:7px;height:7px;background-color:{color};'
        f'vertical-align:middle;margin-right:5px;">&nbsp;</span>{esc(name)}</span>'
    )


def _story_row(story: dict, brand: dict, pal: dict) -> str:
    """One story row: the label, the market chips, the cross-market item count."""
    chips = "".join(_market_chip(m, brand, pal) for m in story["markets"])
    count = story["items"]
    count_html = ""
    if count > 0:
        count_html = (
            f'<span style="font-family:{FONT_MONO};font-size:9px;color:{pal["on_ink_mute"]};'
            f'letter-spacing:0.03em;white-space:nowrap;">{esc(f"{count:,}")} items</span>'
        )
    span = len(story["markets"])
    eyebrow = (
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:9px;font-weight:700;'
        f"letter-spacing:0.1em;text-transform:uppercase;color:{pal['on_ink_mute']};"
        f'margin-bottom:6px;">Moving in {span} markets</div>'
    )
    label = (
        f'<div class="{CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:14px;font-weight:700;'
        f'line-height:1.35;color:{pal["on_ink_soft"]};margin-bottom:10px;">'
        f"{esc(story['label'])}</div>"
    )
    foot = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;"><tr>'
        f'<td valign="middle" style="padding:0;">{chips}</td>'
        f'<td align="right" valign="middle" style="padding:0;">{count_html}</td>'
        "</tr></table>"
    )
    return (
        f'<tr><td class="{CLS["rule"]}" style="padding:0 0 18px 0;border-bottom:1px solid {pal["dark_rule"]};">'
        f"{eyebrow}{label}{foot}</td></tr>"
        '<tr><td style="font-size:0;line-height:18px;">&nbsp;</td></tr>'
    )


def render_pan_african(stories: list[dict], brand: dict) -> str:
    """Render the pan-African section row, or "" when off/empty.

    Returns "" when PAN_AFRICAN_ENABLED is off (dark on ship) OR when no supplied
    story spans two or more markets (the table is empty until the pan-African
    stage runs, so the section prints no empty furniture). Otherwise a dark
    full-width section ``<tr>`` matching the moves block: a mono eyebrow over a
    list of the strongest cross-market stories, each with its markets and item
    count. Defensive: every value is read off the stored story rows.
    """
    if not _pan_african_enabled():
        return ""
    cleaned = _clean_stories(stories)
    if not cleaned:
        return ""
    pal = palette(brand)
    rows = "".join(_story_row(s, brand, pal) for s in cleaned[:_MAX_STORIES])
    eyebrow = (
        f'<div class="{CLS["accent"]}" style="font-family:{FONT_MONO};font-size:10px;font-weight:700;'
        f"letter-spacing:0.2em;text-transform:uppercase;color:{pal['vermillion']};"
        f'margin-bottom:16px;">Across the continent &middot; pan-African</div>'
    )
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;">{rows}</table>'
    )
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" '
        f'style="background-color:{pal["ink"]};padding:30px 26px;">{eyebrow}{table}</td></tr>'
    )
