"""Platform heat-map section for the PULSE v2 mailer (Wave 1).

A daily "which platform drove the day" rollup. Every brief already carries a
per-brief ``platform_counts`` list (entries like ``'tiktok | 40 items'``); the
masthead sums them into one POST-TOPIC READS total but never breaks them down by
platform. This section aggregates those same counts across all the day's briefs
into a per-platform share, then renders a ranked bar so the desk sees at a glance
where the day's volume actually came from.

Dark on ship. The whole section is gated behind PLATFORM_HEATMAP_ENABLED and
renders nothing when the flag is off, so today's email is byte-identical. Even
with the flag on it returns "" when no brief carries usable ``platform_counts``,
so a degraded day never prints empty furniture. No new schema: it reads the
existing additive field, the same parse the masthead uses.

Email-safe: the bars are presentation-table rows with a fixed-width fill cell
carrying a ``bgcolor`` (Outlook ignores CSS background on a cell, so the bar is
an attribute-coloured ``<td>``), never a flex or grid track.
"""

from __future__ import annotations

import os

from src.alerts.email_render._table import CLS, FONT_MONO, palette
from src.alerts.email_render._util import _platform_label, esc

# Cap the rollup so the section stays a scannable summary, not a full ledger.
#
# Raised 6 -> 10 on 27 Jul 2026. Six no longer covered the live spread: the
# 27 Jul briefs ranked tiktok, youtube, web, reddit, google_search, threads in
# the top six, which already pushed instagram out, and the facebook phase
# shipping the same day would have landed below it. A channel that is ingested
# but never rendered is dead ingestion, and this section is its only reader.
_MAX_ROWS = 10


def _platform_enabled() -> bool:
    """Whether the platform heat-map is flagged on (PLATFORM_HEATMAP_ENABLED).

    Read per call so it flips on the live job and toggles in tests without a
    code change. Dark by default: any value outside the truthy set keeps the
    section off, so a flag-off render is byte-identical to today.
    """
    return os.environ.get("PLATFORM_HEATMAP_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _aggregate_platform_counts(briefs: list[dict]) -> list[tuple[str, int]]:
    """Sum the per-brief ``platform_counts`` into a per-platform total.

    ``platform_counts`` entries look like ``'tiktok | 40 items'``; this is the
    same parse the masthead's ``_count_posts`` uses (split on the pipe, pull the
    integer out of the second field), but kept per-platform instead of summed
    flat. Returns ``(platform_slug, total)`` pairs sorted by total descending,
    ties broken by slug for a stable order. An empty or malformed field
    contributes nothing, so the result is empty when no brief carries counts.
    """
    totals: dict[str, int] = {}
    for brief in briefs:
        for entry in brief.get("platform_counts") or []:
            parts = [p.strip() for p in str(entry).split("|")]
            if len(parts) < 2:
                continue
            platform = parts[0].lower()
            if not platform:
                continue
            digits = "".join(ch for ch in parts[1] if ch.isdigit())
            if not digits:
                continue
            totals[platform] = totals.get(platform, 0) + int(digits)
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))


def _bar_row(name: str, count: int, share: int, top: int, pal: dict) -> str:
    """One ranked platform row: the name, a proportional fill bar, the share.

    The fill width is the platform's share of the leading platform's count (so
    the top platform fills the track and the rest read relative to it), floored
    at a thin sliver so even a tiny contributor stays visible. The bar is a
    bgcolor cell inside a fixed-width track cell, both Outlook-safe.
    """
    pct_of_top = round(100 * count / top) if top else 0
    fill = max(4, min(100, pct_of_top))
    label_color = pal["on_ink_soft"]
    stat_color = pal["on_ink_mute"]
    track = pal["dark_chip_line"]
    bar = pal["vermillion"]
    name_cell = (
        f'<td width="108" valign="middle" class="{CLS["ink2"]}" style="width:108px;font-family:{FONT_MONO};'
        f"font-size:10px;font-weight:700;letter-spacing:0.04em;color:{label_color};"
        f'padding:0 12px 8px 0;white-space:nowrap;">{esc(name)}</td>'
    )
    fill_bar = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="{CLS["chip_line"]}" style="width:100%;border-collapse:collapse;background-color:{track};"><tr>'
        f'<td bgcolor="{bar}" class="{CLS["accent_bg"]}" style="background-color:{bar};width:{fill}%;font-size:0;'
        'line-height:8px;height:8px;">&nbsp;</td>'
        '<td style="font-size:0;line-height:8px;">&nbsp;</td>'
        "</tr></table>"
    )
    bar_cell = f'<td valign="middle" style="padding:0 12px 8px 0;">{fill_bar}</td>'
    stat_cell = (
        f'<td width="92" valign="middle" align="right" class="{CLS["mute"]}" style="width:92px;'
        f"font-family:{FONT_MONO};font-size:9.5px;color:{stat_color};"
        f'padding:0 0 8px 0;white-space:nowrap;">{count:,} &middot; {share}%</td>'
    )
    return f"<tr>{name_cell}{bar_cell}{stat_cell}</tr>"


def render_heatmap(briefs: list[dict], brand: dict) -> str:
    """Render the platform heat-map section row, or "" when off/empty.

    Returns "" when PLATFORM_HEATMAP_ENABLED is off (dark on ship) OR when no
    brief carries usable ``platform_counts`` (a degraded day prints no empty
    furniture). Otherwise a dark full-width section ``<tr>`` matching the moves
    block: a mono eyebrow over a ranked bar per platform, share computed against
    the day's total read volume. Defensive: every value is derived from the
    stored field, nothing is invented.
    """
    if not _platform_enabled():
        return ""
    pairs = _aggregate_platform_counts(briefs)
    if not pairs:
        return ""
    total = sum(count for _, count in pairs)
    if total <= 0:
        return ""
    top = pairs[0][1]
    pal = palette(brand)
    rows = "".join(
        _bar_row(_platform_label(slug), count, round(100 * count / total), top, pal)
        for slug, count in pairs[:_MAX_ROWS]
    )
    eyebrow = (
        f'<div class="{CLS["accent"]}" style="font-family:{FONT_MONO};font-size:10px;font-weight:700;'
        f"letter-spacing:0.2em;text-transform:uppercase;color:{pal['vermillion']};"
        f'margin-bottom:16px;">What drove the day &middot; platform mix</div>'
    )
    table = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;">{rows}</table>'
    )
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" '
        f'style="background-color:{pal["ink"]};padding:30px 26px;">{eyebrow}{table}</td></tr>'
    )
