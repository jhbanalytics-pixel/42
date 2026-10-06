"""Masthead renderer for the PULSE v3 mailer.

Ports the ``.mast`` block from the locked v3 mockup: the lockup line, the issue
date, the wordmark with the blue accent dot, the italic tagline, the Google
four-dot powered-by lockup, and the refresh label. The channel / market / post
counts moved out of the masthead provenance line and into the KPI cards
(``kpi.py``); the count helpers stay here as the single source of truth and are
imported by the KPI renderer.

Email-safe: the masthead is a dark full-width section row. The lockup/issue row
is a presentation table that stacks to one column on a phone (``lp-stack`` +
the MSO ghost so Outlook keeps it two-up). The dark band carries both
``bgcolor`` and inline ``background-color`` so Outlook holds it. The wordmark
keeps the verbatim Outlook overflow fix (exact line-height +
``mso-line-height-rule:exactly`` + padding not margin).
"""

from __future__ import annotations

import datetime

from src.alerts.email_render._table import CLS, FONT_BODY, FONT_SERIF, palette
from src.alerts.email_render._util import esc

# The Google brand four-dot mark. Static brand furniture, hardcoded the same way
# the semantic status colors are.
_GOOGLE_DOTS = ("#4285F4", "#EA4335", "#FBBC05", "#34A853")

# The masthead rides the dark header band, which stays dark in BOTH themes. Its
# text must read light in both, so it uses these fixed on-dark tones rather than
# the page ``on_ink_*`` roles (which now mean dark-ink-on-light-paper) and
# carries no flipping text class. The bright accent is the dark-band accent.
_BAND_INK = "#f4f1ea"
_BAND_MUTE = "#9c958a"
_BAND_DIM = "#776f66"
_BAND_ACCENT = "#5e87f0"
_BAND_RULE = "#37322c"


def _count_posts(briefs: list[dict]) -> int:
    """Sum the per-platform item counts the pipeline stored on each brief.

    ``platform_counts`` entries look like ``'tiktok | 40 items'``. We add the
    integer in each. This is real read volume, not a round number. The sum is
    post-topic assignments (a post under N topic_groups counts 1/N per topic, so
    it lands once per topic), not a distinct-post count, hence the KPI label
    "POST-TOPIC READS".
    """
    total = 0
    for brief in briefs:
        for entry in brief.get("platform_counts") or []:
            parts = [p.strip() for p in str(entry).split("|")]
            if len(parts) < 2:
                continue
            digits = "".join(ch for ch in parts[1] if ch.isdigit())
            if digits:
                total += int(digits)
    return total


def _count_markets(briefs: list[dict]) -> int:
    """Distinct markets actually present in the day's briefs.

    Counting ``brand["markets"]`` overstated on a day a market produced no
    briefs. Counting distinct ``brief["market"]`` values, the same source the
    KPI markets card uses, keeps the count honest.
    """
    seen: set[str] = set()
    for brief in briefs:
        m = str(brief.get("market") or "").strip().lower()
        if m:
            seen.add(m)
    return len(seen)


def _count_channels(briefs: list[dict]) -> int:
    """Distinct channels actually read, counted from ``platform_counts``.

    Counting ``platforms`` (the model-inferred display list) undercounts the
    real ingestion channels, so the KPI "N CHANNELS" would disagree with the
    footer's "read this morning from" list. Counting the same ``platform_counts``
    keys the footer uses keeps them consistent.
    """
    seen: set[str] = set()
    for brief in briefs:
        for entry in brief.get("platform_counts") or []:
            parts = [p.strip() for p in str(entry).split("|")]
            platform = parts[0].lower() if parts and parts[0] else ""
            if platform:
                seen.add(platform)
    return len(seen)


def _four_dot_lockup(brand: dict, pal: dict) -> str:
    """The Google four-dot mark + lockup text + engine credit.

    Each dot is a bgcolor ``<td>`` so Outlook fills it. The lockup and engine
    credit each drop when empty (the standard pattern); the four-dot mark is
    static and always renders.
    """
    engine = esc(brand.get("engine_credit") or "")
    # The Ogilvy lockup already prints top-left in the provenance row, so the
    # four-dot mark carries only the engine credit (the duplicate read as a
    # double wordmark). The lockup still appears in the masthead, just once.
    credit = engine or "Powered by Gemini on Vertex"
    dot_cells = ""
    for i, color in enumerate(_GOOGLE_DOTS):
        if i:
            dot_cells += '<td style="width:5px;font-size:0;">&nbsp;</td>'
        dot_cells += (
            f'<td bgcolor="{color}" style="background-color:{color};width:7px;height:7px;'
            'border-radius:50%;font-size:0;line-height:7px;">&nbsp;</td>'
        )
    return (
        '<table role="presentation" border="0" cellpadding="0" cellspacing="0" '
        'style="margin-top:14px;"><tr>'
        f"{dot_cells}"
        f'<td style="padding-left:10px;font-family:{FONT_BODY};font-size:9px;'
        "letter-spacing:0.12em;text-transform:uppercase;white-space:nowrap;"
        f'color:{_BAND_DIM};font-weight:600;">{credit}</td>'
        "</tr></table>"
    )


def render_masthead(
    briefs: list[dict],
    displays: list[dict],
    brand: dict,
    run_date: datetime.date,
) -> str:
    pal = palette(brand)
    name = esc(brand.get("name") or "PULSE")
    lockup = esc(brand.get("lockup") or "")
    tagline = esc(brand.get("tagline") or "")
    issue = esc(run_date.strftime("%a %d %b %Y"))
    # 00:30 UTC is the primary Cloud Run cron; the day's data refreshes then.
    # Brand-overridable for a white-label market on a different clock.
    refresh = esc(brand.get("refresh_label") or "REFRESHED 00:30 UTC")

    lockup_style = (
        f"font-family:{FONT_BODY};font-size:10px;letter-spacing:0.18em;"
        f"text-transform:uppercase;color:{_BAND_MUTE};font-weight:600;"
    )
    issue_style = (
        f"font-family:{FONT_BODY};font-size:10px;letter-spacing:0.14em;"
        f"text-transform:uppercase;color:{_BAND_DIM};font-weight:600;"
        "white-space:nowrap;"
    )
    # Top row: lockup left, issue + refresh right. The hybrid inline-block +
    # MSO ghost so it is two-up on desktop / Outlook and stacks to one column on
    # a phone (the 320px overflow fix: this row must not stay two-column).
    lockup_left = (
        '<div class="lp-stack" style="display:inline-block;vertical-align:top;width:60%;">'
        f'<span style="{lockup_style}">{lockup}</span></div>'
    )
    issue_right = (
        '<div class="lp-stack" style="display:inline-block;vertical-align:top;width:39%;'
        'text-align:right;">'
        f'<span style="{issue_style}">{esc(issue)} &middot; {refresh}</span></div>'
    )
    top = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td style="font-size:0;">'
        '<!--[if mso]><table role="presentation" width="100%"><tr>'
        '<td width="55%" valign="top"><![endif]-->'
        f"{lockup_left}"
        '<!--[if mso]></td><td width="45%" valign="top" align="right"><![endif]-->'
        f"{issue_right}"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table>"
    )

    # Outlook (the Word engine) renders line-height < 1 by overflowing the glyph
    # upward, which made the wordmark bleed into the lockup row above it. Pin the
    # line box with an exact line-height + mso-line-height-rule:exactly, and use
    # padding (Outlook ignores margin) for the gap above the wordmark.
    wordmark = (
        f'<div class="lp-h1" style="font-family:{FONT_SERIF};font-weight:700;font-size:38px;'
        "line-height:40px;mso-line-height-rule:exactly;letter-spacing:-0.02em;"
        f'color:{_BAND_INK};padding:12px 0 0 0;">'
        f'{name}<span style="color:{_BAND_ACCENT};">.</span></div>'
    )
    tag = ""
    if tagline:
        tag = (
            f'<div style="font-family:{FONT_SERIF};font-style:italic;font-size:15px;'
            f'color:{_BAND_MUTE};padding:7px 0 0 0;line-height:1.4;">{tagline}</div>'
        )
    lockup_mark = _four_dot_lockup(brand, pal)

    inner = f"{top}{wordmark}{tag}{lockup_mark}"
    return (
        f'<tr><td bgcolor="{pal["header_band"]}" class="lp-pd {CLS["band"]}" '
        f'style="background-color:{pal["header_band"]};'
        f'border-bottom:1px solid {_BAND_RULE};padding:22px 28px 20px;">{inner}</td></tr>'
    )
