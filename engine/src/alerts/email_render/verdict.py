"""The desk-verdict renderer for the PULSE v2 mailer.

Surfaces the cross-market daily_summary the engine already computes
(``through_line`` + ``summary_text`` + ``call_to_action``) as a boxed editorial
note above the lede. Without this the v2 email silently dropped the day's
synthesis and led on a single trend's headline instead of the verdict Jo and
Thapelo open the mail for. Every field routes through ``clean_copy`` so the
no-dash editorial rule holds (the v1 path only html-escaped). Renders nothing
when ``summary_text`` is empty, the same guard the v1 block uses.

Email-safe: the box is a presentation-table cell with an inline left accent
border, returned as a section row with horizontal padding. No flex.
"""

from __future__ import annotations

import re

from src.alerts.email_render._table import (
    CLS,
    FONT_BODY,
    FONT_SERIF,
    cell,
    eyebrow_style,
    palette,
    row,
)
from src.alerts.email_render._util import clean_copy, esc, humanize_topic

# The daily_summary call_to_action sometimes echoes a raw "market/topic" key
# (e.g. "za/sports_rugby"); clean_copy's de-slugger skips the slash form on
# purpose (its URL guard), so humanise those refs here before render.
_TOPIC_REF_RE = re.compile(r"\b(?:za|ng|ke)/([a-z][a-z0-9_]+)", re.IGNORECASE)

# The call_to_action also leaks a raw internal metric like "(velocity +0.94)"
# into otherwise-editorial prose; strip it so the verdict reads like a desk
# note, not a dashboard.
_INTERNAL_METRIC_RE = re.compile(r"\s*\((?:velocity|score)\s*[+-]?[0-9.]+\)")


def _humanize_refs(text: str) -> str:
    text = _TOPIC_REF_RE.sub(lambda m: humanize_topic(m.group(1)), text)
    return _INTERNAL_METRIC_RE.sub("", text)


def render_verdict(daily_summary: dict | None, brand: dict | None = None) -> str:
    """Render the cross-market desk verdict block, or "" when there is none.

    ``through_line`` is the verdict headline, ``summary_text`` the read, and
    ``call_to_action`` the accent "move this week" panel. Mirrors the v1
    three-part block (email_digest._render_daily_summary_html) in PULSE skin.
    """
    if not daily_summary:
        return ""
    summary_text = _humanize_refs(clean_copy(str(daily_summary.get("summary_text") or ""))).strip()
    if not summary_text:
        return ""

    pal = palette(brand or {})
    through_line = _humanize_refs(clean_copy(str(daily_summary.get("through_line") or ""))).strip()
    cta = _humanize_refs(clean_copy(str(daily_summary.get("call_to_action") or ""))).strip()
    # Seed score topline (Jo, 22 Jun): the secondary follow-vs-seed call naming
    # the topic best positioned to drive Nanobanana/Lyria usage. Self-hides when
    # the model left it empty (no strongly seed-worthy topic that day).
    seed_rec = _humanize_refs(clean_copy(str(daily_summary.get("seed_recommend") or ""))).strip()

    # The eyebrow reads in the bright blue accent on the dark canvas. A small
    # filled dot precedes the label, matching the v3 mockup's verdict kicker.
    dot = (
        f'<span class="{CLS["accent_bg"]}" style="display:inline-block;width:7px;height:7px;border-radius:50%;'
        f"background-color:{pal['vermillion']};vertical-align:middle;"
        'margin-right:8px;font-size:0;line-height:7px;">&nbsp;</span>'
    )
    parts: list[str] = [
        f'<div style="margin-bottom:12px;">{dot}'
        f'<span class="{CLS["accent"]}" style="{eyebrow_style(pal, color=pal["vermillion"])}">The desk verdict</span></div>'
    ]
    if through_line:
        parts.append(
            f'<div class="{CLS["ink"]}" style="font-family:{FONT_SERIF};font-weight:700;font-size:22px;'
            f"line-height:1.32;letter-spacing:-0.012em;color:{pal['on_ink']};"
            f'margin-bottom:16px;">{esc(through_line)}</div>'
        )
    parts.append(
        f'<div class="lp-body {CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:15px;line-height:1.6;'
        f'color:{pal["on_ink_soft"]};">{esc(summary_text)}</div>'
    )
    if cta:
        parts.append(
            f'<table role="presentation" class="{CLS["accent_soft"]}" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'bgcolor="{pal["accent_soft"]}" style="width:100%;border-collapse:collapse;'
            f'background-color:{pal["accent_soft"]};border-radius:10px;margin-top:16px;">'
            + row(
                cell(
                    f'<div class="{CLS["accent"]}" style="font-family:{FONT_BODY};font-size:9px;font-weight:700;'
                    f"letter-spacing:0.16em;text-transform:uppercase;"
                    f'color:{pal["vermillion"]};">The move this week</div>'
                    f'<div class="lp-body {CLS["ink"]}" style="font-family:{FONT_BODY};font-size:14.5px;'
                    f'line-height:1.55;color:{pal["on_ink"]};padding-top:7px;">{esc(cta)}</div>',
                    style="padding:14px 16px;line-height:1.5;",
                    bgcolor=pal["accent_soft"],
                    cls=CLS["accent_soft"],
                )
            )
            + "</table>"
        )
    if seed_rec:
        parts.append(
            f'<table role="presentation" class="{CLS["accent_soft"]}" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'bgcolor="{pal["accent_soft"]}" style="width:100%;border-collapse:collapse;'
            f'background-color:{pal["accent_soft"]};border-radius:10px;margin-top:10px;">'
            + row(
                cell(
                    f'<div class="{CLS["accent"]}" style="font-family:{FONT_BODY};font-size:9px;font-weight:700;'
                    f"letter-spacing:0.16em;text-transform:uppercase;"
                    f'color:{pal["vermillion"]};">Seed to drive Nanobanana + Lyria</div>'
                    f'<div class="lp-body {CLS["ink"]}" style="font-family:{FONT_BODY};font-size:14.5px;'
                    f'line-height:1.55;color:{pal["on_ink"]};padding-top:7px;">{esc(seed_rec)}</div>',
                    style="padding:14px 16px;line-height:1.5;",
                    bgcolor=pal["accent_soft"],
                    cls=CLS["accent_soft"],
                )
            )
            + "</table>"
        )

    # The blue accent rail is a dedicated 3px cell so the rounded card keeps its
    # corners (a border-left on a rounded box clips oddly in some clients).
    body_cell = cell("".join(parts), style="padding:20px 22px 22px;")
    rail = cell(
        "&nbsp;",
        width="3",
        style="width:3px;font-size:0;line-height:0;",
        bgcolor=pal["vermillion"],
        cls=CLS["accent_bg"],
    )
    box = (
        f'<table role="presentation" class="{CLS["surface"]} {CLS["rule"]}" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{pal["card_dark"]}" style="width:100%;border-collapse:collapse;'
        f"background-color:{pal['card_dark']};border:1px solid {pal['dark_rule']};"
        'border-radius:18px;">' + row(rail + body_cell) + "</table>"
    )
    return row(
        cell(box, style="padding:24px 24px 0 24px;", bgcolor=pal["ink"], cls=CLS["bg"]),
    )
