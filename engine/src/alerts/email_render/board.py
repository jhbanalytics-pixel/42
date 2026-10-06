"""The ranked board renderer for the PULSE v3 mailer.

The board is the centrepiece: all signals (ranks 01-24) in one scannable
scoreboard. Ranks 01-03 get the rich treatment (market eyebrow, status word, a
real score, a N/D confidence pip strip, optional LOCAL + MOOD). Ranks 04+ are
the one-line compact index: rank, market dot, headline, category, momentum, and
a real two-decimal score on every row (no dash filler). Directly under the board
sits the NOT MEASURED honesty footnote, the strongest statement of the
no-per-trend-mention rule.

Email-safe: every row is a self-contained presentation table with a flexible
headline cell and a fixed rank + score pair, so it reflows in place at 320px
with no wide table and no display:none desktop columns. Binding rule holds: a
row with no headline is skipped; the board is omitted only when zero rows render.
There is NO per-trend mention count, ever: the only per-row numbers are the
score and the channels-agree ratio.
"""

from __future__ import annotations

import re

from src.alerts.email_render._table import CLS, FONT_BODY, FONT_SERIF, palette
from src.alerts.email_render._util import (
    brief_headline,
    brief_topic,
    category_label,
    clean_copy,
    esc,
    market_color,
    market_name,
    mood_glyph,
    mood_label,
)
from src.alerts.email_render.masthead import _count_channels

_category = category_label

# direction -> (palette color key, label) for the rich-row status word.
_STATE = {
    "new": ("vermillion", "New"),
    "up": ("pos", "Building"),
    "flat": ("neu", "Holding"),
    "down": ("cooling", "Cooling"),
}

# direction -> (palette color key, label) for the compact-row momentum. The
# label is preceded by a CSS colored dot (see _compact_row), not a geometric
# glyph: the earlier &#9650;/&#9644;/&#9660; shapes rendered as missing-glyph
# tofu rectangles in Gmail and Outlook (a solid grey/green box that read as a
# redaction bar across most ranked rows). The dot uses the same bulletproof
# inline-block span the market dot uses, so it renders in every client. The flat
# label matches the rich-row _STATE ("Holding") so one flat state reads as one
# word across rich and compact rows.
_MOMENTUM = {
    "new": ("pos", "New"),
    "up": ("pos", "Building"),
    "flat": ("neu", "Holding"),
    "down": ("cooling", "Cooling"),
}

_BOARD_SYNTHESIS_CAP = 112


def _channels_agree(display: dict) -> int | None:
    """The channel-agree count from ``display.confidence``, or None.

    ``confidence`` is "N channels agree" (or "single-source"). Single-source
    resolves to 1 (one filled pip, never hot). A digit scan extracts N; an
    unparseable string yields None so the caller drops the strip.
    """
    confidence = str(display.get("confidence") or "").strip().lower()
    if not confidence:
        return None
    if confidence == "single-source":
        return 1
    m = re.search(r"\d+", confidence)
    return int(m.group(0)) if m else None


def _pip_strip(display: dict, denominator: int, pal: dict) -> str:
    """The N / D confidence pip strip: N filled pips, D-N empty, with a label."""
    n = _channels_agree(display)
    if n is None:
        return ""
    n = max(0, min(denominator, n))
    pips = ""
    for i in range(denominator):
        color = pal["vermillion"] if i < n else pal["dark_rule"]
        cls = CLS["pip_on"] if i < n else CLS["pip_off"]
        sep = "margin-right:2px;" if i < denominator - 1 else ""
        pips += (
            f'<span class="{cls}" style="display:inline-block;width:9px;height:6px;'
            f'background:{color};border-radius:2px;{sep}">&nbsp;</span>'
        )
    label = (
        f'<span class="{CLS["mute"]}" style="font-family:{FONT_BODY};font-size:9px;letter-spacing:0.06em;'
        f"text-transform:uppercase;color:{pal['on_ink_mute']};font-weight:700;"
        f'padding-right:7px;">{n} / {denominator} channels</span>'
    )
    return f'<td valign="middle" style="white-space:nowrap;">{label}{pips}</td>'


def _rich_row(
    rank: int, brief: dict, display: dict, denominator: int, brand: dict, pal: dict
) -> str:
    """A rich board row (ranks 01-03): eyebrow, headline, status, score, pips."""
    headline = brief_headline(brief)
    if not headline:
        return ""
    market = str(brief.get("market") or "")
    color = market_color(market, brand)
    name = market_name(market)
    category = _category(brief_topic(brief))
    direction = str(display.get("state", {}).get("direction") or "flat")
    badge = str(display.get("state", {}).get("badge") or "")
    status_key, status_default = _STATE.get(direction, ("neu", "Holding"))
    status_word = badge or status_default
    status_color = pal[status_key]
    status_cls = {
        "vermillion": CLS["accent"],
        "pos": CLS["pos"],
        "neu": CLS["neu"],
        "neg": CLS["neg"],
        "cooling": CLS["cooling"],
        "building": CLS["building"],
    }.get(status_key, "")
    score = brief.get("trend_score")
    is_hero = rank == 1

    eyebrow = ""
    if is_hero:
        eyebrow = (
            f'<div class="{CLS["accent"]}" style="font-family:{FONT_BODY};font-size:9px;letter-spacing:0.16em;'
            f"text-transform:uppercase;color:{pal['vermillion']};font-weight:700;"
            'padding-bottom:4px;">Trend of the day</div>'
        )
    name_size = "19px" if is_hero else "17px"
    meta = (
        f'<div class="{CLS["faint"]}" style="font-family:{FONT_BODY};font-size:10px;letter-spacing:0.1em;'
        f'text-transform:uppercase;color:{pal["on_ink_dim"]};margin-top:5px;">'
        f'<span style="color:{color};font-weight:700;">{esc(name)}</span>'
    )
    if category:
        meta += f" &middot; {esc(category)}"
    meta += "</div>"
    headline_cell = (
        '<td valign="top" style="padding-right:10px;">'
        f"{eyebrow}"
        f'<div class="lp-bn {CLS["ink"]}" style="font-family:{FONT_SERIF};font-size:{name_size};'
        f'font-weight:600;line-height:1.22;letter-spacing:-0.01em;color:{pal["on_ink"]};">'
        f"{esc(headline)}</div>{meta}</td>"
    )
    score_cell = ""
    if score is not None:
        score_size = "22px" if is_hero else "18px"
        score_cell = (
            f'<td valign="top" align="right" width="58" style="width:58px;white-space:nowrap;">'
            f'<div class="px {CLS["ink"]}" style="font-family:{FONT_SERIF};font-size:{score_size};'
            f"font-weight:600;color:{pal['on_ink']};line-height:1;"
            f'font-variant-numeric:tabular-nums;">{float(score):.2f}</div>'
            f'<div class="{CLS["faint"]}" style="font-family:{FONT_BODY};font-size:8px;letter-spacing:0.12em;'
            f'text-transform:uppercase;color:{pal["on_ink_dim"]};margin-top:4px;">Score</div>'
            "</td>"
        )
    head = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f"<tr>{headline_cell}{score_cell}</tr></table>"
    )

    status_cell = (
        '<td valign="middle" style="padding-right:14px;white-space:nowrap;">'
        f'<span class="{status_cls}" style="display:inline-block;width:7px;height:7px;border-radius:50%;'
        f"background-color:{status_color};vertical-align:middle;font-size:0;line-height:7px;"
        'margin-right:6px;">&nbsp;</span>'
        f'<span class="{status_cls}" style="font-family:{FONT_BODY};font-size:9.5px;letter-spacing:0.1em;'
        f"text-transform:uppercase;font-weight:700;color:{status_color};"
        f'vertical-align:middle;">{esc(status_word)}</span></td>'
    )
    pip_cell = _pip_strip(display, denominator, pal)
    status_row = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin-top:12px;"><tr>{status_cell}{pip_cell}</tr></table>'
    )

    extras = _rich_extras(brief, display, pal)
    pad = "16px 18px" if is_hero else "13px 18px"
    body_cell = f'<td class="lp-tp lp-ps" style="padding:{pad};">{head}{status_row}{extras}</td>'
    if is_hero:
        # The hero rich row carries the blue accent rail on the left.
        rail = (
            f'<td width="3" bgcolor="{pal["vermillion"]}" class="{CLS["accent_bg"]}" '
            f'style="background-color:{pal["vermillion"]};width:3px;font-size:0;'
            'line-height:0;">&nbsp;</td>'
        )
        cells = rail + body_cell
    else:
        cells = body_cell
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f"<tr>{cells}</tr></table>"
    )


def _rich_extras(brief: dict, display: dict, pal: dict) -> str:
    """The optional MOOD + LOCAL line under a rich row. Drops when both absent."""
    bits: list[str] = []
    mood_text = clean_copy(str(brief.get("sentiment_summary") or ""))
    label = mood_label(mood_text)
    glyph = mood_glyph(mood_text)
    if label and glyph:
        bits.append(f"{glyph} Mood {esc(label.lower())}")
    in_market = display.get("in_market_pct")
    if isinstance(in_market, (int, float)) and not isinstance(in_market, bool) and in_market > 0:
        bits.append(f"Local {int(in_market)}%")
    tag = _first_hashtag(brief)
    if tag:
        bits.append(tag)
    if not bits:
        return ""
    body = " &nbsp;&middot;&nbsp; ".join(bits)
    return (
        f'<div class="{CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:10px;letter-spacing:0.05em;'
        f'text-transform:uppercase;color:{pal["on_ink_soft"]};margin-top:9px;">{body}</div>'
    )


def _first_hashtag(brief: dict) -> str:
    """The first driving hashtag + its share, suffixed "of trend hashtags".

    The "of trend hashtags" suffix keeps the percentage from reading as mention
    volume (the trust fix). Drops when there is no tag or no numeric share.
    """
    for t in brief.get("driving_hashtags") or []:
        if not isinstance(t, dict):
            continue
        tag = clean_copy(str(t.get("tag") or "")).strip()
        share = t.get("share_pct")
        if tag and isinstance(share, (int, float)) and not isinstance(share, bool) and share > 0:
            return f"{esc(tag)} {float(share):.0f}% of trend hashtags"
    return ""


def _first_sentence(text: str, cap: int = _BOARD_SYNTHESIS_CAP) -> str:
    """First sentence of a synthesis, hard-capped so a compact row stays a line."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return ""
    dot = flat.find(". ")
    if 0 <= dot < cap:
        return flat[: dot + 1]
    if len(flat) <= cap:
        return flat
    cut = flat[:cap].rsplit(" ", 1)[0].rstrip(",.;:")
    return f"{cut}..."


def _compact_row(rank: int, brief: dict, display: dict, brand: dict, pal: dict) -> str:
    """A one-line index row (ranks 04+): rank, dot, headline, category, score."""
    headline = brief_headline(brief)
    if not headline:
        return ""
    market = str(brief.get("market") or "")
    color = market_color(market, brand)
    name = market_name(market)
    category = _category(brief_topic(brief))
    direction = str(display.get("state", {}).get("direction") or "flat")
    color_key, momentum = _MOMENTUM.get(direction, ("neu", "Holding"))
    color_cls = {
        "vermillion": CLS["accent"],
        "pos": CLS["pos"],
        "neu": CLS["neu"],
        "neg": CLS["neg"],
        "cooling": CLS["cooling"],
        "building": CLS["building"],
    }.get(color_key, "")
    momentum_dot = (
        '<span style="display:inline-block;width:7px;height:7px;border-radius:50%;'
        f"background-color:{pal[color_key]};vertical-align:middle;font-size:0;"
        'line-height:7px;margin-right:5px;">&nbsp;</span>'
    )
    score = brief.get("trend_score")
    tag_label = esc(name)
    if category:
        tag_label += " &middot; " + esc(category)
    synthesis = _first_sentence(clean_copy(str(brief.get("trend_synthesis") or "")))
    meta_bits = [tag_label]
    if synthesis:
        meta_bits.append(esc(synthesis))
    meta = " &middot; ".join(meta_bits)
    score_cell = ""
    if score is not None:
        score_cell = (
            '<td valign="top" align="right" width="40" style="width:40px;">'
            f'<div class="px {CLS["ink2"]}" style="font-family:{FONT_SERIF};font-size:15px;font-weight:600;'
            f'color:{pal["on_ink_soft"]};font-variant-numeric:tabular-nums;">'
            f"{float(score):.2f}</div></td>"
        )
    dot = (
        f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;'
        f'background-color:{color};font-size:0;line-height:8px;vertical-align:middle;">'
        "&nbsp;</span>"
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td class="lp-tp lp-ps" style="padding:12px 18px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td valign="top" width="26" class="px {CLS["faint"]}" style="width:26px;font-family:{FONT_BODY};'
        f"font-size:12px;font-weight:600;color:{pal['on_ink_dim']};padding-top:1px;"
        f'font-variant-numeric:tabular-nums;">{rank:02d}</td>'
        '<td valign="top" style="padding-right:8px;">'
        f'<div class="{CLS["ink"]}" style="font-family:{FONT_SERIF};font-size:15px;font-weight:500;line-height:1.3;'
        f'color:{pal["on_ink"]};">{dot}&nbsp; {esc(headline)}</div>'
        f'<div class="{CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:11px;letter-spacing:0.01em;'
        f'color:{pal["on_ink_soft"]};line-height:1.5;margin-top:5px;padding-left:16px;">'
        f'{meta} &middot; <span class="{color_cls}" style="color:{pal[color_key]};font-weight:700;">'
        f"{momentum_dot}{momentum}</span></div></td>"
        f"{score_cell}</tr></table></td></tr></table>"
    )


def _hairline(pal: dict) -> str:
    # A single bulletproof bgcolor cell, edge to edge across the board container.
    # The earlier two-table inset wrapper cost ~140 bytes per divider; with a
    # divider after every one of up to 24 ranked rows that wrapper alone pushed
    # the busy-day render past the Gmail 102KB clip. A full-bleed rule reads as a
    # cleaner ledger line and the row's own 18px padding keeps the text inset.
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td height="1" bgcolor="{pal["dark_chip_line"]}" class="{CLS["rule2"]}" '
        f'style="background-color:{pal["dark_chip_line"]};height:1px;font-size:0;'
        'line-height:1px;">&nbsp;</td></tr></table>'
    )


def _not_measured_footnote(denominator: int, pal: dict) -> str:
    """The verbatim NOT MEASURED honesty footnote (static, dash-free)."""
    pill = (
        f'<span class="{CLS["rule2"]} {CLS["mute"]}" style="display:inline-block;'
        f"vertical-align:middle;white-space:nowrap;border:1px dashed {pal['dark_rule_2']};"
        f"border-radius:4px;padding:2px 7px;line-height:1.2;"
        f"color:{pal['on_ink_mute']};font-size:9px;font-weight:700;letter-spacing:0.06em;"
        'text-transform:uppercase;">not measured</span>'
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-top:12px;"><tr><td class="lp-ps" style="padding:0 4px;">'
        f'<div class="{CLS["faint"]}" style="font-family:{FONT_BODY};font-size:10.5px;line-height:1.85;'
        f'color:{pal["on_ink_dim"]};">Channels agree counts the sources that carried each '
        f"signal, out of {denominator} checked. A per-trend mention total is {pill}. The "
        "engine has no such number, so it is never shown.</div></td></tr></table>"
    )


def render_board(
    briefs: list[dict],
    displays: list[dict],
    brand: dict,
    start_rank: int = 1,
) -> str:
    """Render the full ranked board (all ranks) + the NOT MEASURED footnote.

    ``start_rank`` is the 1-based rank of ``briefs[0]`` (the dispatch passes the
    whole score-sorted list, so it stays 1). Ranks 01-03 are rich, 04+ compact.
    Returns "" when zero rows render.
    """
    pal = palette(brand)
    denominator = max(12, _count_channels(briefs))
    rows: list[str] = []
    for i, (brief, display) in enumerate(zip(briefs, displays, strict=False)):
        rank = start_rank + i
        if rank <= 3:
            r = _rich_row(rank, brief, display, denominator, brand, pal)
            if r:
                rows.append(r)
                rows.append(_hairline(pal))
        else:
            r = _compact_row(rank, brief, display, brand, pal)
            if r:
                rows.append(r)
                rows.append(_hairline(pal))
    if not rows:
        return ""
    # Drop the trailing hairline.
    if rows and rows[-1] == _hairline(pal):
        rows.pop()

    divider = _section_divider(len(briefs), pal)
    # Plain-language legend so a non-technical reader knows what a signal and a
    # score are without hunting for the method line in the footer.
    legend = (
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_BODY};font-size:11px;line-height:1.55;'
        f'color:{pal["on_ink_mute"]};margin-top:10px;">'
        f'<span class="{CLS["ink2"]}" style="color:{pal["on_ink_soft"]};font-weight:600;">'
        "Each signal is a trend the desk tracked today, ranked by score.</span> "
        "Score runs 0 to 1 and blends how fast a trend is rising, how widely it spreads, and the "
        "mood. Higher means hotter. "
        f'<span class="{CLS["ink2"]}" style="color:{pal["on_ink_soft"]};font-weight:600;">'
        "SEED is a separate read</span>: how well a trend suits a Nanobanana or Lyria activation, "
        "shown as a percentage. Higher is a stronger seed, and a trend can score low yet seed high."
        "</div>"
    )
    container = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{pal["card_dark"]}" class="{CLS["surface"]} {CLS["rule"]}" style="background-color:{pal["card_dark"]};'
        f'border:1px solid {pal["dark_rule"]};border-radius:14px;margin-top:14px;">'
        f'<tr><td style="padding:0;">{"".join(rows)}</td></tr></table>'
    )
    footnote = _not_measured_footnote(denominator, pal)
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" style="background-color:{pal["ink"]};'
        f'padding:32px 24px 30px;">{divider}{legend}{container}{footnote}</td></tr>'
    )


def _section_divider(count: int, pal: dict) -> str:
    """The blue tick + 'The board' label + rule + 'N signals ranked' tail."""
    tail = ""
    if count:
        tail = (
            f'<td valign="middle" class="lp-hide-sm {CLS["faint"]}" style="padding:0 0 0 12px;'
            f"font-family:{FONT_BODY};font-size:10px;letter-spacing:0.1em;"
            f'text-transform:uppercase;color:{pal["on_ink_dim"]};white-space:nowrap;">'
            f"{count} signals &middot; ranked</td>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td valign="middle" width="16" style="width:16px;padding:0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td bgcolor="{pal["vermillion"]}" class="{CLS["accent_bg"]}" style="background-color:{pal["vermillion"]};'
        'width:16px;height:2px;font-size:0;line-height:2px;">&nbsp;</td></tr></table></td>'
        f'<td valign="middle" class="{CLS["ink"]}" style="padding:0 12px;font-family:{FONT_BODY};font-size:11.5px;'
        f"letter-spacing:0.14em;text-transform:uppercase;font-weight:600;color:{pal['on_ink']};"
        'white-space:nowrap;">The board</td>'
        '<td valign="middle" style="padding:0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td height="1" bgcolor="{pal["dark_rule"]}" class="{CLS["rule"]}" style="background-color:{pal["dark_rule"]};'
        'height:1px;font-size:0;line-height:1px;">&nbsp;</td></tr></table></td>'
        f"{tail}</tr></table>"
    )
