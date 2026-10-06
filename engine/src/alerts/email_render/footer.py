"""Footer renderer for the PULSE v3 mailer.

Ports the ``.foot`` block from the locked v3 mockup: the source-volume trust
table ("read this morning from News 2,660, TikTok 1,299 ...") whose per-platform
counts sum exactly to the post-topic-reads KPI, an auditable total line, the
static method line, and the brand strip with the Google four-dot lockup.

Email-safe: the footer is a dark section row (bgcolor + inline). The source list
is a two-column presentation grid that stacks to one column on a phone via the
``lp-stack`` cells + an MSO ghost so Outlook keeps it two-up; the brand row is an
inline-block list that wraps. The channel counts are summed from the real
``platform_counts``, so the footer is a true read-volume breakdown. Channels
with no counted volume are dropped. The method line is static, dash-free copy.
"""

from __future__ import annotations

import datetime

from src.alerts.email_render._table import CLS, FONT_BODY, FONT_SERIF, palette
from src.alerts.email_render._util import _platform_label, esc
from src.alerts.email_render.masthead import _count_posts

# The Google four-dot mark for the brand strip (static brand furniture).
_GOOGLE_DOTS = ("#4285F4", "#EA4335", "#FBBC05", "#34A853")

# The static method line. Authored here as renderer furniture, so it is scanned
# dash-free at the source: no em, en, or double hyphen. WINDOW (the 14-day
# lookback) is named here, the only place the word WINDOW appears in the body.
_METHOD_LINE = (
    "Method: rolling 14-day window across the channels read, refreshed 00:30 UTC. "
    "Score is a weighted blend of velocity, cross-source spread, engagement and tone. "
    "Confidence is the count of channels that carried a topic, so a multi-channel item "
    "reads stronger than a single source. Sentiment from GKG tone. Three "
    "markets, never blended. Rank is score, not mention volume; per-trend mention totals "
    "are not collected."
)


def _channel_totals(briefs: list[dict]) -> list[tuple[str, int]]:
    """Sum per-platform item counts across briefs, biggest first."""
    totals: dict[str, int] = {}
    for brief in briefs:
        for entry in brief.get("platform_counts") or []:
            parts = [p.strip() for p in str(entry).split("|")]
            if len(parts) < 2:
                continue
            platform = parts[0].lower()
            digits = "".join(ch for ch in parts[1] if ch.isdigit())
            if not platform or not digits:
                continue
            totals[platform] = totals.get(platform, 0) + int(digits)
    return sorted(totals.items(), key=lambda kv: -kv[1])


def _volume_cell(label: str, value: int, pal: dict, left: bool, last: bool) -> str:
    """One platform row in the two-column source-volume grid."""
    border = "" if last else f"border-bottom:1px solid {pal['card_dark']};"
    pad = "5px 10px 5px 0" if left else "5px 0 5px 10px"
    return (
        f'<td width="50%" valign="top" class="lp-stack {CLS["rule"]}" style="padding:{pad};{border}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td class="{CLS["mute"]}" style="font-family:{FONT_BODY};font-size:11px;color:{pal["on_ink_mute"]};">'
        f"{esc(label)}</td>"
        f'<td align="right" class="px {CLS["ink"]}" style="font-family:{FONT_BODY};font-size:11px;'
        f'color:{pal["on_ink"]};font-weight:600;font-variant-numeric:tabular-nums;">'
        f"{value:,}</td></tr></table></td>"
    )


def _volume_grid(totals: list[tuple[str, int]], pal: dict) -> str:
    """The two-up source-volume grid. Rows stack one-up on a phone."""
    cells: list[str] = []
    n = len(totals)
    for i, (platform, value) in enumerate(totals):
        left = i % 2 == 0
        last = i >= n - 2  # last grid row carries no bottom rule
        cells.append(_volume_cell(_platform_label(platform), value, pal, left, last))
    rows = ""
    for i in range(0, len(cells), 2):
        rows += "<tr>" + "".join(cells[i : i + 2]) + "</tr>"
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin-top:12px;">{rows}</table>'
    )


def _brand_strip(brand: dict, pal: dict) -> str:
    name = esc(brand.get("name") or "PULSE")
    lockup = esc(brand.get("lockup") or "")
    engine = esc(brand.get("engine_credit") or "Powered by Gemini on Vertex")
    credit_bits = [b for b in [lockup, "WPP Open" if lockup else "", engine] if b]
    credit = " &middot; ".join(credit_bits)
    dot_cells = ""
    for i, color in enumerate(_GOOGLE_DOTS):
        if i:
            dot_cells += '<td style="width:4px;font-size:0;">&nbsp;</td>'
        dot_cells += (
            f'<td bgcolor="{color}" style="background-color:{color};width:6px;height:6px;'
            'border-radius:50%;font-size:0;line-height:6px;">&nbsp;</td>'
        )
    wordmark_row = (
        '<table role="presentation" border="0" cellpadding="0" cellspacing="0" '
        'style="margin-top:18px;"><tr>'
        f"{dot_cells}"
        f'<td class="{CLS["ink"]}" style="padding-left:10px;font-family:{FONT_SERIF};font-weight:700;font-size:15px;'
        f'letter-spacing:-0.01em;color:{pal["on_ink"]};">'
        f'{name}<span class="{CLS["accent"]}" style="color:{pal["vermillion"]};">.</span></td></tr></table>'
    )
    credit_line = (
        f'<div class="{CLS["faint"]}" style="font-family:{FONT_BODY};font-size:10.5px;letter-spacing:0.04em;'
        f'color:{pal["on_ink_dim"]};line-height:1.6;margin-top:10px;">{credit}<br>'
        "Reply to brief the desk.</div>"
    )
    return wordmark_row + credit_line


def render_footer(
    briefs: list[dict],
    displays: list[dict],
    brand: dict,
    run_date: datetime.date,
) -> str:
    pal = palette(brand)
    totals = _channel_totals(briefs)
    head = ""
    grid = ""
    total_line = ""
    if totals:
        head = (
            f'<div class="{CLS["faint"]}" style="font-family:{FONT_BODY};font-size:9px;letter-spacing:0.16em;'
            f'color:{pal["on_ink_dim"]};font-weight:700;">'
            "READ THIS MORNING FROM</div>"
        )
        grid = _volume_grid(totals, pal)
        posts = _count_posts(briefs)
        # The grid sums to posts by construction (same platform_counts integers),
        # so the visible total is auditable: the reader can add the list and match.
        total_line = (
            f'<div class="{CLS["mute"]}" style="font-family:{FONT_BODY};font-size:10px;letter-spacing:0.06em;'
            f"text-transform:uppercase;color:{pal['on_ink_mute']};margin-top:12px;"
            f'font-weight:600;">{posts:,} post-topic reads</div>'
        )

    method = (
        f'<div class="{CLS["faint"]} {CLS["rule"]}" style="font-family:{FONT_BODY};font-size:10px;letter-spacing:0.03em;'
        f"color:{pal['on_ink_dim']};line-height:1.6;margin-top:18px;padding-top:16px;"
        f'border-top:1px solid {pal["dark_rule"]};">{esc(_METHOD_LINE)}</div>'
    )
    strip = _brand_strip(brand, pal)
    inner = f"{head}{grid}{total_line}{method}{strip}"
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" style="background-color:{pal["ink"]};'
        f'padding:30px 28px 28px;">{inner}</td></tr>'
    )
