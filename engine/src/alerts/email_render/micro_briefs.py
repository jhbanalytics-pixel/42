"""Early-signals micro-brief section for the PULSE v2 mailer (Wave 3).

Small-N but real cultural moments never earn a full Gemini brief and today
just dissolve into a broad topic bucket. ``src.analysis.micro_briefs`` selects
up to a handful of these per market from the day's seed_graph rows (pure, no
Gemini, no BQ); this module renders them as one compact line each under the
existing brief cards.

Dark on ship. The whole section is gated behind MICRO_BRIEFS_ENABLED and
renders nothing when the flag is off, so today's email is byte-identical.
Even with the flag on it returns "" when no market supplies any items, so a
quiet day never prints empty furniture. Every value is read off the selected
item dicts, nothing is invented.

Email-safe: a presentation-table row per market, inline styles only, no flex
and no grid.
"""

from __future__ import annotations

import os

from src.alerts.email_render._table import CLS, FONT_BODY, FONT_MONO, palette
from src.alerts.email_render._util import esc, humanize_topic, market_color, market_name

# Cap per market so the section stays a scannable strip, not a second board.
_MAX_ITEMS_PER_MARKET = 3


def _micro_briefs_enabled() -> bool:
    """Whether the early-signals section is flagged on (MICRO_BRIEFS_ENABLED).

    Read per call so it flips on the live job and toggles in tests without a
    code change. Dark by default: any value outside the truthy set keeps the
    section off, so a flag-off render is byte-identical to today.
    """
    # Truthy set matches _micro_briefs_enabled in scripts/run_rss_now.py; the
    # two readers of this flag must never drift.
    return os.environ.get("MICRO_BRIEFS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _item_line(item: dict, pal: dict) -> str:
    """One early-signal line: the term, platform hint, topic hint, row count."""
    term = str(item.get("term") or "")
    freq = int(item.get("frequency") or 0)
    platforms = [str(p) for p in (item.get("platforms") or []) if p]
    topics = [str(t) for t in (item.get("topics") or []) if t]

    hint_parts = []
    if platforms:
        hint_parts.append(" / ".join(platforms))
    if topics:
        hint_parts.append(" / ".join(humanize_topic(t) for t in topics))
    hint = " &middot; ".join(hint_parts)

    count_label = f"{freq} rows" if freq != 1 else "1 row"

    return (
        f'<tr><td style="padding:0 0 8px 0;font-family:{FONT_BODY};font-size:12px;'
        f'line-height:1.5;color:{pal["on_ink_soft"]};">'
        f'<span style="font-weight:700;">{esc(term)}</span>'
        f'<span style="color:{pal["on_ink_mute"]};"> &middot; {esc(hint)} &middot; '
        f"{esc(count_label)}</span></td></tr>"
    )


def _market_block(market: str, items: list[dict], brand: dict, pal: dict) -> str:
    name = market_name(market)
    color = market_color(market, brand)
    dot = (
        f'<span style="display:inline-block;width:7px;height:7px;background-color:{color};'
        'vertical-align:middle;margin-right:6px;">&nbsp;</span>'
    )
    label = (
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:9px;font-weight:700;'
        f"letter-spacing:0.08em;text-transform:uppercase;color:{pal['on_ink_mute']};"
        f'margin-bottom:8px;">{dot}{esc(name)}</div>'
    )
    rows = "".join(_item_line(item, pal) for item in items[:_MAX_ITEMS_PER_MARKET])
    body = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;">{rows}</table>'
    )
    return f'<tr><td style="padding:0 0 16px 0;">{label}{body}</td></tr>'


def render_micro_briefs(micro_briefs_by_market: dict[str, list[dict]] | None, brand: dict) -> str:
    """Render the early-signals section row, or "" when off/empty.

    ``micro_briefs_by_market`` maps market slug to the list of items
    ``select_micro_briefs`` returned for that market. Returns "" when
    MICRO_BRIEFS_ENABLED is off (dark on ship) OR when every market's list is
    empty. Otherwise a dark full-width section matching the pan-African block:
    a mono eyebrow over one compact block per market with items.
    """
    if not _micro_briefs_enabled():
        return ""
    data = micro_briefs_by_market or {}
    populated = {mk: items for mk, items in data.items() if items}
    if not populated:
        return ""
    pal = palette(brand)
    eyebrow = (
        f'<div class="{CLS["accent"]}" style="font-family:{FONT_MONO};font-size:10px;font-weight:700;'
        f"letter-spacing:0.2em;text-transform:uppercase;color:{pal['vermillion']};"
        f'margin-bottom:16px;">Early signals</div>'
    )
    blocks = "".join(
        _market_block(mk, items, brand, pal) for mk, items in populated.items() if items
    )
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;">{blocks}</table>'
    )
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" '
        f'style="background-color:{pal["ink"]};padding:30px 26px;">{eyebrow}{table}</td></tr>'
    )


__all__ = ["render_micro_briefs"]
