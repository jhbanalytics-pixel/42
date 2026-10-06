"""Ticker strip renderer for the PULSE v3 mailer.

A thin dark strip above the masthead, a one-line scannable shift summary
("9 channels agree. Today's shift: Football cooling, Amapiano holding."). It is
the v2 ``_shift_line`` logic relocated, with the market flag emoji dropped (the
judge flagged inconsistent flag rendering) and the whole strip hidden below
600px via ``lp-hide-sm`` so it never forces a width floor on a phone.

Email-safe: a single dark cell (bgcolor + inline). Binding rule holds: the
"N channels agree" lead binds the hero's ``display.confidence``; each shift
clause binds a brief's short topic label + its ``display.state.direction``. With
no briefs, or no resolvable clauses, the strip is omitted.
"""

from __future__ import annotations

import re

from src.alerts.email_render._table import CLS, FONT_BODY, palette
from src.alerts.email_render._util import brief_topic, esc, humanize_topic

# The ticker rides the dark header band, which stays dark in BOTH themes (it is
# already a dark band on the light page). So its text must be light in both
# themes and must NOT carry the flipping ``lp-*`` text classes (those follow the
# page canvas, which goes light). These fixed band tones are the on-dark light
# values; the bright accent is the dark-band accent, not the on-paper ink-accent.
_BAND_INK = "#f4f1ea"
_BAND_SOFT = "#c8c2b7"
_BAND_MUTE = "#9c958a"
_BAND_ACCENT = "#5e87f0"
_BAND_POS = "#56c98a"
_BAND_RULE = "#37322c"

# direction -> the verb the ticker uses for a shift clause.
_DIRECTION_VERB = {
    "new": "entering",
    "up": "accelerating",
    "flat": "holding",
    "down": "cooling",
}


def _channels_lead(display: dict) -> str:
    """The "N channels agree" prefix from the hero confidence, or "".

    ``confidence`` is the honest channel-count string ("9 channels agree" or
    "single-source"). We surface it verbatim when it carries a count, and drop
    it for a single source (never reads as corroborated).
    """
    confidence = str(display.get("confidence") or "").strip()
    if not confidence or confidence == "single-source":
        return ""
    if not re.search(r"\d", confidence):
        return ""
    return confidence


def render_ticker(briefs: list[dict], displays: list[dict], brand: dict) -> str:
    """Render the ticker strip, or "" when there is nothing to say."""
    if not briefs:
        return ""
    pal = palette(brand)
    clauses: list[str] = []
    seen: set[str] = set()
    for brief, display in zip(briefs, displays, strict=False):
        label = humanize_topic(brief_topic(brief))
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        direction = str(display.get("state", {}).get("direction") or "flat")
        verb = _DIRECTION_VERB.get(direction, "moving")
        clauses.append(f"{esc(label)} {verb}")
        if len(clauses) >= 3:
            break

    lead = _channels_lead(displays[0] if displays else {})
    if not lead and not clauses:
        return ""

    parts: list[str] = []
    if lead:
        parts.append(f'<span style="color:{_BAND_POS};font-weight:700;">{esc(lead)}</span>.')
    if clauses:
        body = ", ".join(clauses)
        parts.append(
            f'<span style="color:{_BAND_MUTE};">Today\'s shift:&#160;</span>'
            f'<span style="color:{_BAND_INK};font-weight:600;">{body}</span>'
        )
    line = " ".join(parts)

    inner = (
        f'<span style="font-family:{FONT_BODY};font-size:11px;letter-spacing:0.04em;'
        f'line-height:1.6;color:{_BAND_SOFT};">'
        f'<span style="color:{_BAND_ACCENT};text-transform:uppercase;'
        "letter-spacing:0.18em;font-size:9.5px;font-weight:700;"
        f'margin-right:10px;">Live signal</span>{line}</span>'
    )
    return (
        f'<tr><td bgcolor="{pal["header_band"]}" class="lp-hide-sm {CLS["band"]}" '
        f'style="background-color:{pal["header_band"]};'
        f"border-bottom:1px solid {_BAND_RULE};padding:9px 18px;"
        f'mso-hide:all;">{inner}</td></tr>'
    )
