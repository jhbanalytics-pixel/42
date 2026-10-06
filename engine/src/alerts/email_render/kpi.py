"""KPI metric cards + the strongest-confirmation line for the PULSE v3 mailer.

Four inline-block metric cards in a row (channels, markets, post-topic reads,
trends tracked) followed by a slim line naming the strongest-confirmation trend.
Every number is a real audited count: the three count helpers live in
``masthead.py`` (the tests pin them) and are imported here so the KPI cards and
the masthead read one source of truth, plus ``len(briefs)`` for the trend count.
There is NO per-trend mention count anywhere (the hard contract rule).

Email-safe + mobile: the cards are the hybrid inline-block + MSO ghost pattern
the mockup verified. Outlook lays them four-across via the ghost table; other
clients reflow two-up (<=600px) then one-up (<=380px) via ``lp-kpi``. With no
briefs the whole KPI block is omitted; a single zero count drops its own card.
"""

from __future__ import annotations

from src.alerts.email_render._table import CLS, FONT_BODY, FONT_SERIF, palette
from src.alerts.email_render._util import brief_headline, esc
from src.alerts.email_render.masthead import (
    _count_channels,
    _count_markets,
    _count_posts,
)


def channel_denominator(briefs: list[dict]) -> int:
    """The shared D for the pip strip, the KPI channels card, and the footnote.

    ``max(12, channels read)`` so the ratio is never N over less-than-N and the
    three places that print the denominator (the board pips, the channels KPI,
    the NOT MEASURED footnote) all state the same number.
    """
    return max(12, _count_channels(briefs))


def _card(value: str, label: str, pal: dict) -> str:
    """One hybrid inline-block KPI card. Empty value drops the card upstream."""
    return (
        '<div class="lp-kpi" style="display:inline-block;width:138px;max-width:138px;'
        'vertical-align:top;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td style="padding:0 5px 10px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="{CLS["surface"]} {CLS["rule"]}" '
        f'bgcolor="{pal["card_dark"]}" style="background-color:{pal["card_dark"]};'
        f'border:1px solid {pal["dark_rule"]};border-radius:12px;">'
        '<tr><td style="padding:15px 16px;">'
        f'<div class="px {CLS["ink"]}" style="font-family:{FONT_SERIF};font-size:28px;font-weight:600;'
        f"color:{pal['on_ink']};line-height:1;letter-spacing:-0.02em;"
        f'font-variant-numeric:tabular-nums;">{esc(value)}</div>'
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_BODY};font-size:9.5px;letter-spacing:0.1em;'
        f"text-transform:uppercase;color:{pal['on_ink_mute']};margin-top:7px;"
        f'font-weight:500;line-height:1.35;min-height:26px;">{esc(label)}</div>'
        "</td></tr></table></td></tr></table></div>"
    )


def render_kpi(briefs: list[dict], displays: list[dict], brand: dict) -> str:
    """Render the four KPI cards as a section row, or "" with no briefs."""
    if not briefs:
        return ""
    pal = palette(brand)
    cards_data: list[tuple[str, str]] = []
    channels = channel_denominator(briefs)
    if channels:
        cards_data.append((str(channels), "Channels"))
    markets = _count_markets(briefs)
    if markets:
        cards_data.append((str(markets), "Markets"))
    posts = _count_posts(briefs)
    if posts:
        cards_data.append((f"{posts:,}", "Post-topic reads"))
    cards_data.append((str(len(briefs)), "Trends tracked"))

    pieces: list[str] = []
    for i, (value, label) in enumerate(cards_data):
        if i == 0:
            pieces.append(
                '<!--[if mso]><table role="presentation" width="552" cellpadding="0" '
                'cellspacing="0" border="0"><tr><td width="138" valign="top"><![endif]-->'
            )
        else:
            pieces.append('<!--[if mso]></td><td width="138" valign="top"><![endif]-->')
        pieces.append(_card(value, label, pal))
    pieces.append("<!--[if mso]></td></tr></table><![endif]-->")
    cards = "".join(pieces)

    cards_block = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td align="center" style="font-size:0;">{cards}</td></tr></table>'
    )

    strongest = _strongest_line(briefs, displays, pal)
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" '
        f'style="background-color:{pal["ink"]};padding:18px 24px 0;">'
        f"{cards_block}{strongest}</td></tr>"
    )


def _strongest_line(briefs: list[dict], displays: list[dict], pal: dict) -> str:
    """A slim line naming the hero brief and its channel-agree confidence, or ""."""
    headline = brief_headline(briefs[0])
    confidence = str((displays[0] if displays else {}).get("confidence") or "").strip()
    if not headline or not confidence or confidence == "single-source":
        return ""
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-top:2px;"><tr>'
        f'<td class="lp-ps {CLS["card"]} {CLS["rule"]}" bgcolor="{pal["well"]}" style="padding:11px 14px;'
        f"background-color:{pal['well']};border:1px solid {pal['dark_rule']};"
        'border-radius:10px;">'
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_BODY};font-size:11px;letter-spacing:0.03em;'
        f'color:{pal["on_ink_mute"]};line-height:1.6;">'
        f'<span class="{CLS["faint"]}" style="color:{pal["on_ink_dim"]};text-transform:uppercase;'
        'letter-spacing:0.12em;font-size:9px;font-weight:700;">Strongest confirmation</span><br>'
        f'<span class="{CLS["ink"]}" style="color:{pal["on_ink"]};font-weight:600;">{esc(headline)}</span>&#160;'
        f'<span class="{CLS["pos"]}" style="color:{pal["pos"]};font-weight:600;">{esc(confidence)}</span>.'
        "</div></td></tr></table>"
    )
