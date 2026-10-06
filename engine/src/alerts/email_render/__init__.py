"""PULSE v2 mailer renderers (package).

``render_pulse_html`` composes the full editorial email from the section
renderers: masthead, the desk verdict, the big-thing lede, the hero (the lead
brief), the move cards, the radar, and the footer.

Email-safe by construction. Outlook on Windows renders with the Word engine: it
deletes the head ``<style>`` block and supports neither flexbox nor grid. So the
whole email is built from presentation ``<table>`` rows with inline styles on
every element, and the body is a fixed 680px column centered by the standard
outer-table pattern. The optional ``<style>`` block (``_style.py``) only carries
the web-font @import and the mobile @media stack as progressive enhancement, so
the email still reads correctly with it stripped.

``render_inbox_summary`` is the bulletproof table-layout version for the inbox
itself: the lede plus the top headlines and a link to the full read, inline
styles only, no ``<style>`` block.

Binding rule holds end to end: every value comes from a real brief or display
field. Absent fields drop their element. No quote is ever fabricated.
"""

from __future__ import annotations

import datetime
import re

from src.alerts.email_render._style import STYLE
from src.alerts.email_render._table import (
    CLS,
    FONT_BODY,
    FONT_MONO,
    FONT_SERIF,
    palette,
)
from src.alerts.email_render._util import (
    brief_headline,
    chip,
    clean_copy,
    esc,
    market_color,
    market_name,
    mood_glyph,
    mood_label,
    proof_anchor,
    render_seen,
)
from src.alerts.email_render.board import render_board
from src.alerts.email_render.card import (
    _category,
    _continuity_badge,
    _drivers,
    _hashtags,
    _lifecycle_badge,
    _outlook_chip,
    _room,
    _trajectory_label,
    render_card,
)
from src.alerts.email_render.footer import render_footer
from src.alerts.email_render.heatmap import render_heatmap
from src.alerts.email_render.kit import render_kit
from src.alerts.email_render.kpi import channel_denominator, render_kpi
from src.alerts.email_render.masthead import _count_markets, render_masthead
from src.alerts.email_render.micro_briefs import render_micro_briefs
from src.alerts.email_render.pan_african import render_pan_african
from src.alerts.email_render.ticker import render_ticker
from src.alerts.email_render.tone_split import render_tone_split
from src.alerts.email_render.verdict import _humanize_refs, render_verdict

__all__ = ["render_inbox_summary", "render_pulse_html"]

# Zero-width non-joiner (U+200C), built by codepoint so this source stays clean.
# A short run trails the hidden preheader text to stop the body copy bleeding into
# the inbox preview snippet; the literal char is 3 bytes in UTF-8 vs 7 for the
# ``&#8204;`` entity, which keeps the busy-day render under the Gmail clip.
_ZWNJ = chr(0x200C)


def render_hero(brief: dict, display: dict, brand: dict) -> str:
    """Render the lead brief as the dark hero block.

    Same fields as a move card, but the hero markup (a vermillion band, the
    dark body, dark chips, a hero foot). Reuses the card's drivers / room / kit
    helpers (with the dark flag) so the binding and graceful-degradation logic
    is identical. Returned as a full-width section row.
    """
    pal = palette(brand)
    market = str(brief.get("market") or "")
    color = market_color(market, brand)
    name = market_name(market)
    category = _category(brief.get("topic") or brief.get("query_group") or "")
    badge = str(display.get("state", {}).get("badge") or "")
    direction = str(display.get("state", {}).get("direction") or "flat")
    state_color = {
        "new": pal["vermillion"],
        "up": pal["pos"],
        "flat": pal["neu"],
        "down": pal["neg"],
    }.get(direction, pal["neu"])
    dot = (
        f'<span style="display:inline-block;width:8px;height:8px;background-color:{color};'
        'vertical-align:middle;margin-right:6px;">&nbsp;</span>'
    )
    flag_parts = [f"{dot}{esc(name)}"]
    if category:
        flag_parts.append(esc(category))
    flag_body = " &middot; ".join(flag_parts)
    if badge:
        flag_body += (
            f' &middot; <span style="color:{state_color};font-weight:700;">{esc(badge)}</span>'
        )
    # Wave 1 continuity + lifecycle badges on the lead brief too, so the hero and
    # the move cards carry the same signal. Each no-ops (returns "") when its flag
    # is off or the field is absent, so the hero flagline is byte-identical today.
    flag_body += _continuity_badge(brief, pal, dark=True)
    flag_body += _lifecycle_badge(brief, pal, dark=True)
    flagline = (
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:10px;'
        f"letter-spacing:0.1em;text-transform:uppercase;color:{pal['on_ink_mute']};"
        f'margin-bottom:16px;line-height:1.5;">{flag_body}</div>'
    )

    # No h1 here: the headline is the lede standfirst directly above this block,
    # so the hero leads with the read instead of reprinting the same line (the
    # repeat flagged 30 May 2026).
    read = clean_copy(str(brief.get("trend_synthesis") or "")).strip()
    read_html = ""
    if read:
        read_html = (
            f'<div class="{CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:15px;'
            f'line-height:1.55;color:{pal["on_ink_soft"]};margin-bottom:22px;">{esc(read)}</div>'
        )

    chips = _hero_chips(brief, display, pal)
    chips_block = f'<div style="margin-bottom:22px;">{chips}</div>' if chips else ""
    seen = render_seen(display.get("channels") or [], pal, show_label=True, dark=True)
    seen_block = f'<div style="margin-bottom:22px;">{seen}</div>' if seen else ""
    drivers = _drivers(brief, pal, dark=True)
    room = _room(brief, brand, market, pal, dark=True)
    hashtags = _hashtags(brief, brand, market, pal, dark=True)
    kit = f'<div style="margin-top:22px;">{render_kit(brief, display, brand)}</div>'
    foot = _hero_foot(brief, display, pal)

    band_r = ""
    if name:
        band_r = (
            f'<td align="right" style="font-family:{FONT_MONO};font-size:10.5px;font-weight:700;'
            f'letter-spacing:0.05em;color:#ffffff;">{esc(name)} &middot; top trend today</td>'
        )
    band = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;"><tr>'
        f'<td bgcolor="{pal["vermillion"]}" class="lp-pd {CLS["accent_bg"]}" '
        f'style="background-color:{pal["vermillion"]};padding:12px 24px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;"><tr>'
        f'<td style="font-family:{FONT_MONO};font-size:10.5px;font-weight:700;'
        'letter-spacing:0.2em;text-transform:uppercase;color:#ffffff;">'
        "● Trend of the day</td>"
        f"{band_r}</tr></table></td></tr></table>"
    )

    body = (
        f'<td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" '
        f'style="background-color:{pal["ink"]};padding:22px 24px 30px;">'
        f"{flagline}{read_html}{chips_block}{seen_block}{drivers}{room}{hashtags}{kit}{foot}</td>"
    )
    inner = (
        f"{band}"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;"><tr>{body}</tr></table>'
    )
    return f'<tr><td style="padding:0;">{inner}</td></tr>'


def _hero_chips(brief: dict, display: dict, pal: dict) -> str:
    out: list[str] = []
    phase = str(display.get("phase") or "")
    if phase:
        out.append(chip("PHASE", phase, pal, hot=True, dark=True))
    confidence = str(display.get("confidence") or "")
    if confidence:
        out.append(
            chip("CONFIDENCE", confidence, pal, hot=confidence != "single-source", dark=True)
        )
    window = str(display.get("window") or "")
    if window:
        # ACT BY (forward horizon); WINDOW is the 14-day lookback in the footer.
        out.append(chip("ACT BY", window, pal, dark=True))
    in_market = display.get("in_market_pct")
    if isinstance(in_market, (int, float)) and not isinstance(in_market, bool) and in_market > 0:
        # LOCAL = foreign-content-filter pass-rate, not a geo-trust score.
        inm = int(in_market)
        out.append(chip("LOCAL", f"{inm}%", pal, hot=inm < 70, dark=True))
    search = str(display.get("search") or "")
    if search == "rising":
        out.append(chip("SEARCH", "rising", pal, dark=True))

    mood_text = clean_copy(str(brief.get("sentiment_summary") or ""))
    glyph = mood_glyph(mood_text)
    label = mood_label(mood_text)
    if label and glyph:
        out.append(chip("MOOD", label, pal, glyph=glyph, dark=True))
    trajectory = _trajectory_label(brief)
    if trajectory:
        out.append(chip("MARKET 7-DAY", trajectory, pal, dark=True))
    outlook_chip = _outlook_chip(brief, pal, dark=True)
    if outlook_chip:
        out.append(outlook_chip)
    if not out:
        return ""
    # Join with a space: Outlook ignores the inline-block margin, so a breakable
    # whitespace text node is what actually separates the chips there. _minify
    # now preserves a single inter-tag space, so this survives the minifier.
    return f'<div style="line-height:1.9;">{" ".join(out)}</div>'


def _hero_foot(brief: dict, display: dict, pal: dict) -> str:
    confidence = str(display.get("confidence") or "")
    score = brief.get("trend_score")
    bits = []
    if confidence:
        bits.append(esc(confidence))
    if score is not None:
        bits.append(f"score {float(score):.2f}")
    left = " &middot; ".join(bits)
    proof = proof_anchor(brief, "Open the evidence &rarr;", color=pal["on_ink_warm"])
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="{CLS["rule"]}" style="width:100%;border-collapse:collapse;margin-top:22px;'
        f'border-top:1px solid {pal["dark_rule"]};"><tr>'
        f'<td class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:10px;'
        f'color:{pal["on_ink_mute"]};letter-spacing:0.03em;padding-top:16px;">{left}</td>'
        f'<td align="right" style="padding-top:16px;">{proof}</td>'
        "</tr></table>"
    )


def _trust_strip(briefs: list[dict], brand: dict) -> str:
    """A slim plain-language trust line under the masthead so the reader sees the
    scope and the score scale before the first number. The full method breakdown
    still lives in the footer for auditing."""
    pal = palette(brand)
    d = channel_denominator(briefs)
    m = _count_markets(briefs)
    label = (
        f'<span class="{CLS["accent"]}" style="font-family:{FONT_BODY};font-size:9px;font-weight:700;'
        f"letter-spacing:0.14em;text-transform:uppercase;color:{pal['vermillion']};"
        'white-space:nowrap;">How to read this</span>'
    )
    body = (
        f"{d} channels across {m} markets, rolling 14-day window, refreshed 00:30 UTC. "
        "Score runs 0 to 1 and ranks every signal. Higher is hotter."
    )
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]} {CLS["rule"]}" '
        f'style="background-color:{pal["ink"]};padding:14px 24px;border-bottom:1px solid {pal["dark_rule"]};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td valign="top" style="padding-right:12px;white-space:nowrap;">{label}</td>'
        f'<td valign="top" class="{CLS["mute"]}" style="font-family:{FONT_BODY};font-size:11px;'
        f'line-height:1.55;color:{pal["on_ink_mute"]};">{body}</td>'
        "</tr></table></td></tr>"
    )


def render_pulse_html(
    briefs: list[dict],
    displays: list[dict],
    brand: dict,
    run_date: datetime.date,
    daily_summary: dict | None = None,
    pan_african_stories: list[dict] | None = None,
    micro_briefs_by_market: dict[str, list[dict]] | None = None,
) -> str:
    """Compose the full PULSE v3 email (the midnight signal-desk look).

    Order: ticker, masthead (with the Google four-dot powered-by lockup), the
    desk verdict + move-this-week panel, four KPI metric cards + the strongest-
    confirmation line, the 24-signal ranked board (rich 01-03 with pip strips,
    04+ a one-line index) + the NOT MEASURED footnote, the full hero brief, two
    compact move cards, the source-volume trust footer with the method line.
    ``displays`` is positionally aligned to ``briefs``; index 0 is the hero, 1-2
    the moves, 3+ the board long tail. With no briefs the masthead and verdict
    still render and every data-bound section omits cleanly.

    Each section renderer returns a presentation-table row (``<tr>``); they are
    stacked inside one 600px inner table wrapped in an MSO ghost table so Outlook
    holds the width while other clients shrink to full width.
    """
    pal = palette(brand)
    ticker = render_ticker(briefs, displays, brand)
    masthead = render_masthead(briefs, displays, brand, run_date)
    verdict = render_verdict(daily_summary, brand)
    trust = _trust_strip(briefs, brand) if briefs else ""
    kpi = render_kpi(briefs, displays, brand)

    board = ""
    hero = ""
    moves = ""
    if briefs:
        # The board carries ALL ranks (the at-a-glance scoreboard); the hero and
        # move cards are the deep dive of the same top briefs.
        board = render_board(briefs, displays, brand, start_rank=1)
        hero = render_hero(briefs[0], displays[0], brand)
        move_briefs = briefs[1:3]
        move_displays = displays[1:3]
        cards = "".join(
            render_card(b, d, brand) for b, d in zip(move_briefs, move_displays, strict=False)
        )
        if cards:
            total_moves = max(0, len(briefs) - 1)
            label = f"Today's moves &middot; {len(move_briefs)} of {total_moves}"
            moves = _section_row(label, cards, pal)

    # Wave 1 platform heat-map (live: PLATFORM_HEATMAP_ENABLED): which platform
    # drove the day, rolled up from each brief's platform_counts. Returns "" when
    # the flag is off or no brief carries counts.
    heatmap = render_heatmap(briefs, brand)
    # Wave 2 tone split (dark until TONE_SPLIT_ENABLED): the day's
    # positive/neutral/negative mood balance from the social sentiment lexicon.
    # Returns "" when off or no brief carries a lexicon score.
    tone = render_tone_split(briefs, brand)
    # Wave 2 pan-African (dark until PAN_AFRICAN_ENABLED): stories rising in two
    # or more SSA markets at once. Returns "" when off or no row spans two markets.
    pan_african = render_pan_african(pan_african_stories or [], brand)
    # Wave 3 micro-briefs (dark until MICRO_BRIEFS_ENABLED): small-N early
    # signals that never earn a full brief, one line each below the main
    # briefs. Returns "" when off or no market supplies any items.
    micro_briefs = render_micro_briefs(micro_briefs_by_market, brand)
    footer = render_footer(briefs, displays, brand, run_date)
    title = esc(brand.get("name") or "PULSE")

    inner = (
        '<table role="presentation" class="pulse-inner lp-container '
        f'{CLS["bg"]}" width="600" cellpadding="0" '
        'cellspacing="0" border="0" align="center" '
        f'bgcolor="{pal["ink"]}" '
        f'style="width:100%;max-width:600px;border-collapse:collapse;background-color:'
        f'{pal["ink"]};">'
        f"{ticker}{masthead}{trust}{verdict}{kpi}{board}{hero}{moves}"
        f"{heatmap}{tone}{pan_african}{micro_briefs}{footer}"
        "</table>"
    )
    outer = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="{CLS["bg"]}" bgcolor="{pal["ink"]}" style="width:100%;border-collapse:collapse;'
        f'background-color:{pal["ink"]};">'
        '<tr><td align="center" style="padding:0;">'
        f'<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'border="0" bgcolor="{pal["ink"]}" style="background-color:{pal["ink"]};">'
        "<tr><td><![endif]-->"
        f"{inner}"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table>"
    )
    doc = (
        "<!DOCTYPE html>"
        '<html lang="en" xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{title}</title>"
        f"{STYLE}"
        "</head>"
        f'<body class="{CLS["bg"]}" bgcolor="{pal["ink"]}" '
        f'style="margin:0;padding:0;background-color:{pal["ink"]};">'
        f"{_preheader(daily_summary)}"
        f"{outer}"
        "</body></html>"
    )
    return _minify(doc)


def _preheader(daily_summary: dict | None) -> str:
    """The hidden inbox preview text, carried from the daily summary through_line.

    Gmail and Apple Mail scrape the first visible text for the inbox snippet; with
    no preheader that is the issue date. This hidden span feeds the through_line
    into that snippet instead, cleaned exactly as the verdict cleans it (raw
    market/topic refs humanised, internal velocity/score metrics stripped). It is
    esc-wrapped because the through_line is Gemini-generated text. A short run of
    zero-width non-joiners after the text stops body copy bleeding into the
    preview. Returns "" when there is no through_line, so no empty span ships.

    The text is capped near the inbox-snippet length (Gmail and Apple Mail show
    roughly 100 characters of preview); anything past that never renders and only
    eats into the Gmail 102KB clip, so a longer line is trimmed on a word boundary
    with a trailing ellipsis.
    """
    if not daily_summary:
        return ""
    through_line = _humanize_refs(clean_copy(str(daily_summary.get("through_line") or ""))).strip()
    if not through_line:
        return ""
    if len(through_line) > 100:
        cut = through_line[:100].rsplit(" ", 1)[0].rstrip(",.;:")
        through_line = f"{cut}..."
    return (
        '<span style="display:none;font-size:0;line-height:0;max-height:0;mso-hide:all;'
        'overflow:hidden;opacity:0;">'
        f"{esc(through_line)}{_ZWNJ * 6}</span>"
    )


def _minify(html: str) -> str:
    """Collapse whitespace between tags to a single space, to claw back margin
    under the Gmail 102KB clip on a busy day.

    Collapses to one space rather than nothing so the single whitespace node the
    chip rows rely on survives (Outlook desktop ignores the inline-block chip
    margin, so that space is the only separator there). The regex matches only
    whitespace wholly between a ``>`` and a ``<``, so no rendered copy changes.
    Conditional comments and content are left intact.

    Also drops the redundant trailing ``;`` before a closing attribute quote
    (``;"`` to ``"``), which is the optional CSS statement terminator at the end
    of an inline ``style`` value. Escaped copy never contains a literal ``"`` (it
    is rendered as ``&quot;``), so ``;"`` only ever closes a style attribute; this
    is a safe byte claw-back under the Gmail 102KB clip.

    Drops the same redundant ``;`` before a closing CSS rule brace (``;}`` to
    ``}``) too. ``;}`` only ever occurs inside the head ``<style>`` block (an
    inline style ends at ``;"``, never ``;}``), so this trims the optional
    terminator on each @media / dark-override rule with no rendered change.
    """
    html = re.sub(r">\s+<", "> <", html)
    return html.replace(';"', '"').replace(";}", "}")


def _section_row(label: str, body: str, pal: dict) -> str:
    """A dark section row: a mono eyebrow above the body (the moves block)."""
    eyebrow = (
        f'<div class="{CLS["accent"]}" style="font-family:{FONT_MONO};font-size:10px;'
        f"font-weight:700;letter-spacing:0.2em;text-transform:uppercase;"
        f'color:{pal["vermillion"]};margin-bottom:16px;">{label}</div>'
    )
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" '
        f'style="background-color:{pal["ink"]};padding:30px 24px;">{eyebrow}{body}</td></tr>'
    )


def render_inbox_summary(
    briefs: list[dict],
    displays: list[dict],
    brand: dict,
    run_date: datetime.date,
    full_url: str,
    daily_summary: dict | None = None,
) -> str:
    """Bulletproof full-document inbox teaser for Path A, adaptive light base.

    A self-contained ``<html>`` document built for Outlook's Word engine. The
    inline base is the LIGHT paper skin (matching the rich body), so Gmail,
    Outlook on Windows and any light-mode reader see paper. There is no
    ``<style>`` block here (the bulletproof contract), so the dark switch is left
    to the client: ``color-scheme: light dark`` meta declares both schemes, and
    the semantic ``lp-*`` classes are attached so a host that injects the shared
    overrides (or Apple Mail's automatic dark treatment) flips it cleanly. The
    light base is the safe default everywhere the block is absent. Gmail in dark
    mode is one such place: it strips the classes and the color-scheme meta, so
    the teaser stays the light paper card against the app's dark chrome. That
    degrade is accepted (readable dark ink on paper, just a light box in a dark
    inbox); do not chase it with a mid-tone base without checking the result in
    both schemes, since a token that reads in dark can fail contrast in light.

    The Word-engine rules still hold: every coloured cell carries a ``bgcolor``
    attribute (Outlook ignores CSS background on cells), the CTA is a ``bgcolor``
    ``<td>`` with the link inside (Outlook does not treat ``<a>`` as
    block-level), fonts are web-safe inline on every element (no web font, no
    mono, so the Times-New-Roman override never triggers), spacing is padding and
    a fixed-line-height spacer not margin, and there is no flex and no grid.

    Binds to real fields, omits absent.
    """
    pal = palette(brand)
    accent = pal["vermillion"]
    bg = pal["ink"]  # the light page
    cream = pal["on_ink"]  # primary ink on paper
    soft = pal["on_ink_soft"]
    dim = pal["on_ink_dim"]
    body_txt = pal["on_ink_soft"]
    rule = pal["dark_rule"]  # the light hairline
    name = esc(brand.get("name") or "PULSE")
    tagline = esc(brand.get("tagline") or "")
    issue = esc(run_date.strftime("%a %d %b %Y").upper())

    # The desk verdict, cleaned exactly as the rich block cleans it (raw
    # market/topic refs humanised, internal velocity/score metrics stripped),
    # rendered web-safe on the dark sheet: a serif headline + a body read.
    verdict_html = ""
    if daily_summary:
        through_line = _humanize_refs(
            clean_copy(str(daily_summary.get("through_line") or ""))
        ).strip()
        summary_text = _humanize_refs(
            clean_copy(str(daily_summary.get("summary_text") or ""))
        ).strip()
        call_to_action = _humanize_refs(
            clean_copy(str(daily_summary.get("call_to_action") or ""))
        ).strip()
        vparts = []
        if through_line:
            vparts.append(
                f'<div class="{CLS["ink"]}" style="font-family:{FONT_SERIF};font-weight:bold;'
                f'font-size:21px;line-height:1.28;color:{cream};padding:0 0 10px 0;">'
                f"{esc(through_line)}</div>"
            )
        if summary_text:
            vparts.append(
                f'<div class="{CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:14px;'
                f'line-height:1.55;color:{body_txt};padding:0 0 20px 0;">{esc(summary_text)}</div>'
            )
        if call_to_action:
            vparts.append(
                f'<div class="{CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:13px;'
                f'font-weight:bold;line-height:1.45;color:{cream};padding:0 0 16px 0;">'
                f"The move this week: {esc(call_to_action)}</div>"
            )
        verdict_html = "".join(vparts)

    rows = []
    for brief, display in list(zip(briefs, displays, strict=False))[:3]:
        headline = brief_headline(brief)
        if not headline:
            continue
        market = esc(market_name(str(brief.get("market") or "")))
        badge = esc(str(display.get("state", {}).get("badge") or ""))
        meta = " &middot; ".join(b for b in [market, badge] if b)
        rows.append(
            f'<tr><td class="{CLS["ink"]} {CLS["rule"]}" '
            f'style="padding:12px 0;border-bottom:1px solid {rule};'
            f'font-family:{FONT_SERIF};font-size:17px;line-height:1.3;color:{cream};">'
            f"{esc(headline)}"
            f'<div class="{CLS["faint"]}" style="font-family:{FONT_BODY};font-size:11px;'
            f'color:{dim};padding-top:4px;">{meta}</div>'
            "</td></tr>"
        )
    rows_html = "".join(rows)

    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{name}</title></head>"
        f'<body class="{CLS["bg"]}" style="margin:0;padding:0;background-color:{bg};" '
        f'bgcolor="{bg}">'
        f"{_preheader(daily_summary)}"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="{CLS["bg"]}" bgcolor="{bg}" '
        f'style="width:100%;border-collapse:collapse;background-color:{bg};">'
        '<tr><td align="center" style="padding:26px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="{CLS["bg"]}" bgcolor="{bg}" '
        f'style="width:100%;max-width:600px;border-collapse:collapse;background-color:{bg};">'
        # Masthead wordmark on the light sheet; bgcolor on the cell keeps it filled.
        f'<tr><td class="{CLS["bg"]}" bgcolor="{bg}" '
        f'style="background-color:{bg};padding:8px 28px 0 28px;">'
        f'<div class="{CLS["ink"]}" style="font-family:{FONT_SERIF};font-weight:bold;'
        f'font-size:34px;line-height:1.1;color:{cream};">{name}'
        f'<span class="{CLS["accent"]}" style="color:{accent};">.</span></div>'
        f'<div class="{CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:12px;color:{soft};'
        f'padding-top:5px;">{tagline}</div>'
        f'<div class="{CLS["faint"]}" style="font-family:{FONT_BODY};font-size:10px;'
        f'letter-spacing:0.08em;color:{dim};padding-top:7px;">{issue}</div>'
        "</td></tr>"
        # Content on the same light sheet.
        f'<tr><td class="{CLS["bg"]}" bgcolor="{bg}" '
        f'style="background-color:{bg};padding:18px 28px 28px 28px;">'
        f"{verdict_html}"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;">'
        f"{rows_html}"
        "</table>"
        # Fixed-line-height spacer (margin is ignored by Outlook).
        '<div style="font-size:0;line-height:24px;">&nbsp;</div>'
        # Bulletproof CTA: bgcolor on the td, link inline-block inside it.
        # The CTA fill carries lp-cta (NOT lp-ab): lp-cta has no dark override, so
        # the fill stays the light accent #2f5fd0 in dark mode instead of flipping
        # to #5e87f0. White text on #2f5fd0 is 5.72:1 (AA), where white on #5e87f0
        # was only 3.39:1. The hero band keeps lp-ab and still flips, untouched.
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;"><tr>'
        f'<td class="lp-cta" bgcolor="{accent}" '
        f'style="background-color:{accent};">'
        f'<a href="{esc(full_url)}" style="display:inline-block;padding:13px 22px;'
        f"font-family:{FONT_BODY};font-size:13px;font-weight:bold;letter-spacing:0.02em;"
        f'color:#ffffff;text-decoration:none;">Open the full read &rarr;</a>'
        "</td></tr></table>"
        "</td></tr>"
        "</table></td></tr></table>"
        "</body></html>"
    )


def render_inbox_summary_text(
    briefs: list[dict],
    displays: list[dict],
    run_date: datetime.date,
    full_url: str,
    daily_summary: dict | None = None,
) -> str:
    """Plain-text counterpart to render_inbox_summary for Path A multipart sends."""
    from src.alerts.email_render.verdict import _humanize_refs

    lines = [f"PULSE, {run_date.isoformat()}", ""]
    if daily_summary:
        through = _humanize_refs(clean_copy(str(daily_summary.get("through_line") or ""))).strip()
        summary = _humanize_refs(clean_copy(str(daily_summary.get("summary_text") or ""))).strip()
        cta = _humanize_refs(clean_copy(str(daily_summary.get("call_to_action") or ""))).strip()
        if through:
            lines.append(through)
        if summary:
            if through:
                lines.append("")
            lines.append(summary)
        if cta:
            if through or summary:
                lines.append("")
            lines.append(f"The move this week: {cta}")
    lines.append("")
    lines.append("Top trends today:")
    for brief, display in list(zip(briefs, displays, strict=False))[:3]:
        headline = brief_headline(brief)
        if not headline:
            continue
        market = market_name(str(brief.get("market") or ""))
        badge = str(display.get("state", {}).get("badge") or "").strip()
        meta = " · ".join(p for p in (market, badge) if p)
        suffix = f" ({meta})" if meta else ""
        lines.append(f"- {headline}{suffix}")
    if full_url:
        lines.append("")
        lines.append(f"Open the full read: {full_url}")
    return "\n".join(lines)
