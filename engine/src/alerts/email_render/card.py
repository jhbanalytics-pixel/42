"""Trend-card renderer for the PULSE v2 mailer.

Ports the ``.card`` block from the locked mockup: flagline (market dot + name +
category + state badge), headline, momentum arrow, the read, the chip row
(PHASE / CONFIRMED / WINDOW / MOOD), the Seen-on channel bars, the drivers
(creators), the room (comment slot), and the embedded Kit.

Email-safe: the card is a bordered presentation-table cell; the head, the
drivers, the room and the foot are all tables, not flex. Binding rule: every
element reads a real field. Absent or empty fields drop their element. The room
never fabricates a quote: with no comment data it shows a caged not-live state,
with comment data it shows the real sentiment and theme split but still emits no
quote tag (the pipeline stores no quotes).
"""

from __future__ import annotations

import os
import re

from src.alerts.email_render._guard import assert_sourced
from src.alerts.email_render._table import (
    CLS,
    FONT_BODY,
    FONT_MONO,
    FONT_SERIF,
    mono_label_style,
    palette,
)
from src.alerts.email_render._util import (
    brief_headline,
    brief_topic,
    category_label,
    chip,
    clean_copy,
    esc,
    market_color,
    market_name,
    mood_glyph,
    mood_label,
    parse_creator,
    proof_anchor,
    render_seen,
)
from src.alerts.email_render.kit import render_kit

# Topic-slug prefix -> flagline category label lives in _util now (one shared
# copy with radar). Re-exported as _category so __init__ keeps importing it.
_category = category_label

# direction -> the .state modifier color and the momentum label/arrow.
_STATE_COLOR = {"new": "vermillion", "up": "pos", "flat": "neu", "down": "neg"}
_DIRECTION_MOMENTUM = {
    "new": ("pos", "&#8599;", "New"),
    "up": ("pos", "&#8599;", "Rising"),
    "flat": ("neu", "&rarr;", "Steady"),
    "down": ("neg", "&#8600;", "Cooling"),
}

# 7-day forecast outlook -> the arrow glyph for its chip. The keys are also the
# allow-list: a value outside this set drops the chip (see _outlook_chip).
_OUTLOOK_GLYPH = {"heating": "&#8599;", "steady": "&rarr;", "cooling": "&#8600;"}

# Seed score (Jo, 22 Jun): at or above this the SEED chip renders hot (accent).
# Mirrors configs/scoring.yaml seed_score.chip_hot_threshold; kept as a render
# constant so the card stays config-free like the other badges.
_SEED_CHIP_HOT_THRESHOLD = 0.70

# Wave 1 continuity state -> the short badge label. The keys are the allow-list:
# a value outside this set drops the badge (see _continuity_badge). Values are
# the locked contract set (new, day2, day3plus, rebounding). The label uses the
# stored continuity_day where it adds signal (the running day count), so "day3plus"
# with continuity_day 5 reads "Day 5" not a flat "Day 3+".
_CONTINUITY_LABELS = {
    "new": "New today",
    "day2": "Day 2",
    "day3plus": "Day 3+",
    "rebounding": "Rebounding",
}

# Wave 1 lifecycle phase -> the short badge label. The keys are the allow-list:
# a value outside this set drops the badge (see _lifecycle_badge). Values are
# the locked contract set (birth, growth, maturity, decline) the velocity
# trajectory classifier emits in run_rss_now._lifecycle_phase.
_LIFECYCLE_LABELS = {
    "birth": "Birth",
    "growth": "Growth",
    "maturity": "Maturity",
    "decline": "Decline",
}


def _continuity_enabled() -> bool:
    """Whether the continuity badge is flagged on (CONTINUITY_BADGES_ENABLED).

    Read per call so it flips on the live job and toggles in tests without a
    code change. Dark by default: any value outside the truthy set keeps the
    badge off, so a flag-off render is byte-identical to today.
    """
    return os.environ.get("CONTINUITY_BADGES_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _continuity_badge(brief: dict, pal: dict, dark: bool = False) -> str:
    """The Wave 1 continuity badge (new / Day 2 / Day 3+ / rebounding), or "".

    Defensive: reads the additive ``continuity_state`` field the engine tags on
    a brief once CONTINUITY_BADGES_ENABLED is live. Returns "" when the flag is
    off OR the field is absent or carries an unknown value, so today's render
    (no field, flag off) is unchanged. ``continuity_day`` sharpens the day3plus
    label into the real running count when it is a positive int; everything else
    falls back to the static label. Rebounding wears the accent (a reappearance
    after a gap is the eye-catching state); the rest stay muted.
    """
    if not _continuity_enabled():
        return ""
    state = str(brief.get("continuity_state") or "").strip().lower()
    label = _CONTINUITY_LABELS.get(state)
    if not label:
        return ""
    day = brief.get("continuity_day")
    if state == "day3plus" and isinstance(day, int) and not isinstance(day, bool) and day > 0:
        label = f"Day {day}"
    color = (
        pal["vermillion"]
        if state == "rebounding"
        else (pal["on_ink_mute"] if dark else pal["muted"])
    )
    return f' &middot; <span style="color:{color};font-weight:700;">{esc(label)}</span>'


def _lifecycle_enabled() -> bool:
    """Whether the lifecycle badge is flagged on (LIFECYCLE_ENABLED).

    Read per call so it flips on the live job and toggles in tests without a
    code change. Dark by default: any value outside the truthy set keeps the
    badge off, so a flag-off render is byte-identical to today. Mirrors
    _continuity_enabled exactly.
    """
    return os.environ.get("LIFECYCLE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _lifecycle_badge(brief: dict, pal: dict, dark: bool = False) -> str:
    """The Wave 1 lifecycle badge (Birth / Growth / Maturity / Decline), or "".

    Defensive twin of _continuity_badge: reads the additive ``lifecycle_phase``
    field the engine tags on a brief once LIFECYCLE_ENABLED is live. Returns ""
    when the flag is off OR the field is absent or carries an unknown value, so
    today's render (no field, flag off) is unchanged. Growth wears the accent
    (a trend still climbing is the eye-catching state); the rest stay muted.
    Emits the leading " &middot; " separator so the flagline composes the same
    way it does for the continuity badge.
    """
    if not _lifecycle_enabled():
        return ""
    phase = str(brief.get("lifecycle_phase") or "").strip().lower()
    label = _LIFECYCLE_LABELS.get(phase)
    if not label:
        return ""
    color = (
        pal["vermillion"] if phase == "growth" else (pal["on_ink_mute"] if dark else pal["muted"])
    )
    return f' &middot; <span style="color:{color};font-weight:700;">{esc(label)}</span>'


def _outlook_chip(brief: dict, pal: dict, dark: bool = False) -> str:
    """The 7-day forecast outlook chip (heating / steady / cooling), or "".

    Reads the additive ``forecast_outlook`` field that the Layer 2 forecast tags
    onto a brief. Absent (the default, flag off) or any unknown value drops the
    chip, so a forecast-off render is byte-identical. Heating wears the hot
    accent; steady and cooling stay plain.
    """
    outlook = str(brief.get("forecast_outlook") or "").strip().lower()
    if outlook not in _OUTLOOK_GLYPH:
        return ""
    return chip(
        "OUTLOOK", outlook, pal, hot=outlook == "heating", glyph=_OUTLOOK_GLYPH[outlook], dark=dark
    )


def _flagline(brief: dict, display: dict, brand: dict, pal: dict, dark: bool = False) -> str:
    market = str(brief.get("market") or "")
    color = market_color(market, brand)
    name = market_name(market)
    category = _category(brief_topic(brief))
    direction = str(display.get("state", {}).get("direction") or "flat")
    badge = str(display.get("state", {}).get("badge") or "")
    text_color = pal["on_ink_mute"] if dark else pal["muted"]
    dot = (
        f'<span style="display:inline-block;width:8px;height:8px;background-color:{color};'
        'vertical-align:middle;margin-right:6px;">&nbsp;</span>'
    )
    parts = [f"{dot}{esc(name)}"]
    if category:
        parts.append(esc(category))
    bits = " &middot; ".join(parts)
    badge_html = ""
    if badge:
        state_color = pal[_STATE_COLOR.get(direction, "neu")]
        state_cls = {
            "vermillion": CLS["accent"],
            "pos": CLS["pos"],
            "neu": CLS["neu"],
            "neg": CLS["neg"],
        }.get(_STATE_COLOR.get(direction, "neu"), "")
        badge_html = (
            f' &middot; <span class="{state_cls}" style="color:{state_color};'
            f'font-weight:700;">{esc(badge)}</span>'
        )
    # Wave 1 continuity + lifecycle badges, dark by default. Each helper returns
    # the leading " &middot; " separator and no-ops (returns "") when its flag is
    # off or the field is absent, so the flagline is byte-identical today.
    continuity_html = _continuity_badge(brief, pal, dark=dark)
    lifecycle_html = _lifecycle_badge(brief, pal, dark=dark)
    return (
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:10px;'
        f"letter-spacing:0.1em;text-transform:uppercase;color:{text_color};"
        f'margin-bottom:10px;line-height:1.5;">'
        f"{bits}{badge_html}{continuity_html}{lifecycle_html}</div>"
    )


def _momentum(display: dict, pal: dict) -> str:
    direction = str(display.get("state", {}).get("direction") or "flat")
    color_key, arrow, label = _DIRECTION_MOMENTUM.get(direction, ("neu", "&rarr;", "Steady"))
    color = pal[color_key]
    color_cls = {"pos": CLS["pos"], "neu": CLS["neu"], "neg": CLS["neg"]}.get(color_key, "")
    return (
        f'<div class="{color_cls}" style="font-family:{FONT_MONO};font-size:9.5px;'
        f"font-weight:700;letter-spacing:0.05em;text-transform:uppercase;text-align:right;"
        f'color:{color};white-space:nowrap;">'
        f'<span style="font-size:15px;display:block;">{arrow}</span>{label}</div>'
    )


def _trajectory_label(brief: dict) -> str:
    """A compact 7-day Brand24 sentiment-trajectory chip value, or "".

    This is a market-level signal: computed once per market in generate_briefs
    and stamped on every brief in that market, not measured per card, hence the
    chip reads "MARKET 7-DAY". Empty field drops the chip.

    ``_b24_sentiment_trajectory_for_market`` emits exactly four shapes:
    ``stable``, ``improving (+Npp positive share)``, ``declining (-Npp positive
    share)`` and ``""``. Direction is the entire signal so it leads, and the
    magnitude rides along when the producer computed one.

    Fixed 17 Aug 2026. This function used to hunt for direction words the
    producer never emits ("rising", "holding", "falling"), fall through to a
    tone word, and match the "positive" inside "positive share". A market
    getting WORSE therefore rendered as "Positive", identical to one getting
    better, on every brief every day. Two guards keep that shut: the producer's
    own vocabulary is matched first, and the parenthetical is stripped before
    the tone lookup because "positive share" is the UNIT of the delta, not a
    mood. The legacy vocabulary is still recognised so older stored values and
    any hand-written phrase still render sensibly.
    """
    raw = clean_copy(str(brief.get("b24_sentiment_trajectory") or "")).strip()
    if not raw:
        return ""
    low = raw.lower()
    magnitude = re.search(r"([+-]?\d+)\s*pp\b", low)
    # "positive share" lives inside the parenthetical and is a unit, not a tone.
    outside_parens = re.sub(r"\(.*?\)", " ", low)
    direction = next(
        (
            d
            for d in (
                # Producer contract, generate_briefs._b24_sentiment_trajectory_for_market.
                "improving",
                "declining",
                "stable",
                # Legacy phrasing kept so previously stored values still render.
                "rising",
                "climbing",
                "holding",
                "steady",
                "cooling",
                "falling",
            )
            if d in outside_parens
        ),
        "",
    )
    tone = next(
        (t for t in ("positive", "negative", "mixed", "neutral") if t in outside_parens),
        "",
    )
    parts: list[str] = []
    if direction:
        parts.append(direction.title())
        if magnitude:
            parts.append(f"{magnitude.group(1)}pp")
        elif tone:
            parts.append(tone.title())
    elif tone:
        parts.append(tone.title())
    compact = " ".join(parts).strip()
    return compact if compact else raw[:24].strip().title()


def _chips(brief: dict, display: dict, pal: dict, dark: bool = False) -> str:
    out: list[str] = []
    phase = str(display.get("phase") or "")
    if phase:
        out.append(chip("PHASE", phase, pal, hot=dark, dark=dark))
    confidence = str(display.get("confidence") or "")
    if confidence:
        # Honest channel-count read, never "confirmed". A multi-channel trend
        # shows a hot chip; a single source stays plain so it never reads as
        # corroborated.
        out.append(
            chip("CONFIDENCE", confidence, pal, hot=confidence != "single-source", dark=dark)
        )
    window = str(display.get("window") or "")
    if window:
        # ACT BY is the forward-horizon chip ("ride now ~2wk"); WINDOW is
        # reserved for the 14-day lookback named in the footer method line.
        out.append(chip("ACT BY", window, pal, dark=dark))
    in_market = display.get("in_market_pct")
    if isinstance(in_market, (int, float)) and not isinstance(in_market, bool) and in_market > 0:
        # LOCAL = share of the sample that passed the foreign-content filter
        # (display_layer.in_market_pct), not a geo-trust score; hot under 70%.
        inm = int(in_market)
        out.append(chip("LOCAL", f"{inm}%", pal, hot=inm < 70, dark=dark))
    # Search is a signal only when it says something. "flat" (measured, no
    # movement) and "no lift" (no data) are both noise on the card, so only a
    # real rising read earns the chip (Nick: search supports a trend, it is not
    # wallpaper on every card).
    search = str(display.get("search") or "")
    if search == "rising":
        out.append(chip("SEARCH", "rising", pal, dark=dark))
    mood_text = clean_copy(str(brief.get("sentiment_summary") or ""))
    glyph = mood_glyph(mood_text)
    label = mood_label(mood_text)
    if label and glyph:
        out.append(chip("MOOD", label, pal, glyph=glyph, dark=dark))
    trajectory = _trajectory_label(brief)
    if trajectory:
        out.append(chip("MARKET 7-DAY", trajectory, pal, dark=dark))
    outlook_chip = _outlook_chip(brief, pal, dark=dark)
    if outlook_chip:
        out.append(outlook_chip)
    # SEED = worth-seeding-for-Nanobanana/Lyria signal (Jo, 22 Jun). Hot at or
    # above the threshold; absent/garbage seed_score drops the chip.
    seed = brief.get("seed_score")
    if isinstance(seed, (int, float)) and not isinstance(seed, bool) and seed > 0:
        seed_pct = round(float(seed) * 100)
        out.append(
            chip(
                "SEED", f"{seed_pct}%", pal, hot=float(seed) >= _SEED_CHIP_HOT_THRESHOLD, dark=dark
            )
        )
    if not out:
        return ""
    # Space gap: Outlook ignores the inline-block margin, so the whitespace node
    # is what actually separates the chips there. _minify now preserves a single
    # inter-tag space, so this survives the minifier.
    return f'<div style="line-height:1.9;">{" ".join(out)}</div>'


def _drivers(brief: dict, pal: dict, dark: bool = False) -> str:
    creators = brief.get("top_creators") or []
    rows: list[str] = []
    av_bg = pal["vermillion"] if dark else pal["ink"]
    meta_color = pal["on_ink_soft"] if dark else pal["ink2"]
    stat_color = pal["on_ink_mute"] if dark else pal["muted"]
    for entry in creators:
        c = parse_creator(entry)
        if c is None:
            continue
        meta_bits = [f'<b style="font-family:{FONT_MONO};font-weight:700;">{esc(c["handle"])}</b>']
        if c["platform"]:
            meta_bits.append(esc(c["platform"]))
        line = " &middot; ".join(meta_bits)
        stat = ""
        if c["stat"]:
            stat = (
                f' <span class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:9.5px;'
                f'color:{stat_color};">'
                f"{esc(c['stat'])}</span>"
            )
        rows.append(
            "<tr>"
            f'<td width="34" valign="top" style="width:34px;padding:0 10px 10px 0;">'
            f'<span class="{CLS["accent_bg"]}" style="font-family:{FONT_MONO};font-size:10px;'
            f"font-weight:700;color:#ffffff;"
            f'background-color:{av_bg};padding:3px 6px;display:inline-block;">{esc(c["initials"])}'
            "</span></td>"
            f'<td class="{CLS["ink2"]}" valign="middle" style="font-family:{FONT_BODY};'
            f"font-size:12.5px;line-height:1.45;"
            f'color:{meta_color};padding:0 0 10px 0;">{line}{stat}</td>'
            "</tr>"
        )
    if not rows:
        return ""
    label_color = pal["on_ink_warm"] if dark else pal["muted"]
    label_cls = CLS["accent"] if dark else CLS["mute"]
    return (
        f'<div style="margin-top:22px;"><div class="{label_cls}" '
        f'style="{mono_label_style(pal, color=label_color)}'
        f'margin-bottom:10px;">Who is driving it</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;">{"".join(rows)}</table></div>'
    )


def _room_grid_row(label: str, value: str, pal: dict, dark: bool) -> str:
    """One labeled row in the room card: a mono label cell + a value cell.

    The value reads brighter than the label so the insight is the eye's first
    stop; the label is the dimmer uppercase mono key. A 2-column presentation row
    (email-safe), never display:grid. Mirrors the Kit's dt/dd treatment so the
    room card and the Kit speak one structural language.
    """
    label_color = pal["on_ink_mute"] if dark else pal["muted"]
    value_color = pal["on_ink_soft"] if dark else pal["ink2"]
    return (
        "<tr>"
        f'<td class="{CLS["mute"]}" width="84" valign="top" style="width:84px;'
        f"font-family:{FONT_MONO};font-size:8.5px;"
        f"font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{label_color};"
        f'padding:0 14px 8px 0;white-space:nowrap;">{label}</td>'
        f'<td class="{CLS["ink2"]}" valign="top" style="font-family:{FONT_MONO};'
        f"font-size:10.5px;line-height:1.55;"
        f'color:{value_color};padding:0 0 8px 0;">{value}</td>'
        "</tr>"
    )


def _conversation_block(
    label: str, live: bool, content: str, color: str, pal: dict, dark: bool
) -> str:
    """One Conversation-Intelligence sub-card, shared by the room + hashtag slots.

    A single mono section header that carries the LIVE / NOT LIVE pill, then the
    content in the market-coloured border-left box. Both cards render through this
    one helper so they are structurally identical, one label each (no redundant
    inner sub-label), the pill aligned to the right of the header, the same indent
    and rule. The caller passes pre-built ``content`` HTML.
    """
    head_color = pal["on_ink_mute"] if dark else pal["muted"]
    body_color = pal["on_ink_soft"] if dark else pal["ink2"]
    pp_bg = pal["dark_chip_line"] if dark else pal["paper2"]
    label_color = pal["on_ink_warm"] if dark else pal["muted"]
    label_cls = CLS["accent"] if dark else CLS["mute"]
    pill_cls = CLS["rule2"] if dark else CLS["surface"]
    pill = "LIVE" if live else "NOT LIVE"
    pill_html = (
        f'<span class="{pill_cls} {CLS["mute"]}" style="background-color:{pp_bg};'
        f"color:{head_color};padding:2px 7px;"
        f'font-family:{FONT_MONO};font-size:8px;letter-spacing:0.12em;">{pill}</span>'
    )
    # Header as a 2-column row: label left, pill hard-right (mirrors the Kit
    # header pattern), so every conversation card lines its pill up identically.
    header = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;margin-bottom:10px;"><tr>'
        f'<td class="{label_cls}" valign="middle" '
        f'style="{mono_label_style(pal, color=label_color)}">{label}</td>'
        f'<td valign="middle" align="right" style="white-space:nowrap;">{pill_html}</td>'
        "</tr></table>"
    )
    inner = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;"><tr>'
        f'<td class="{CLS["ink2"]}" style="border-left:3px solid {color};'
        f'padding-left:16px;color:{body_color};">'
        f"{content}</td></tr></table>"
    )
    return f'<div style="margin-top:22px;">{header}{inner}</div>'


def _room(brief: dict, brand: dict, market: str, pal: dict, dark: bool = False) -> str:
    """The comment slot ("what the room says").

    With real ``comment_sentiment`` / ``comment_themes`` it renders the theme
    split and the comment-mood read. With neither the slot drops entirely
    (the binding rule: absent fields render nothing); the earlier caged
    "lands next week" promise went stale once the producer flipped live, and
    on a recovery send it misread as a regression. It never emits a quote
    tag: the pipeline stores no comment quotes, so fabricating one is
    forbidden. Renders through ``_conversation_block``.
    """
    color = market_color(market, brand) if market else pal["vermillion"]
    sentiment = clean_copy(str(brief.get("comment_sentiment") or "")).strip()
    themes = brief.get("comment_themes") or []
    if not (sentiment or themes):
        return ""
    opening = clean_copy(str(brief.get("comment_opening") or "")).strip()
    grid_rows = [_room_grid_row("The read", esc(sentiment), pal, dark)]
    if opening:
        grid_rows.append(_room_grid_row("The opening", esc(opening), pal, dark))
    if themes:
        theme_text = " &middot; ".join(esc(clean_copy(str(t))) for t in themes if str(t).strip())
        if theme_text:
            grid_rows.append(_room_grid_row("Talking", theme_text, pal, dark))
    content = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'border="0" style="width:100%;border-collapse:collapse;">'
        f"{''.join(grid_rows)}</table>"
    )
    return _conversation_block("What the room says", True, content, color, pal, dark)


def _hashtags(brief: dict, brand: dict, market: str, pal: dict, dark: bool = False) -> str:
    """The driving-hashtags slot, paired directly under the room card.

    With a real ``driving_hashtags`` list (top tags, each carrying a
    share-of-tagged-posts and its OWN comment mood) it renders the ranked tags
    LIVE. With none the slot drops entirely, mirroring ``_room`` (absent
    fields render nothing; the caged "lands next week" state is retired).
    The producer hardens every field (no quotes, no dashes, no names), so this
    only escapes + clean_copies on the way out, never fabricates.
    """
    color = market_color(market, brand) if market else pal["vermillion"]
    head_color = pal["on_ink_mute"] if dark else pal["muted"]
    tags = brief.get("driving_hashtags") or []
    rows: list[str] = []
    for t in tags:
        if not isinstance(t, dict):
            continue
        tag = esc(clean_copy(str(t.get("tag") or "")).strip())
        if not tag:
            continue
        bits = [f"<b>{tag}</b>"]
        share = t.get("share_pct")
        if isinstance(share, (int, float)) and not isinstance(share, bool) and share > 0:
            bits.append(f"{float(share):.0f}% of tagged posts")
        mood = esc(clean_copy(str(t.get("mood") or "")).strip())
        if mood:
            bits.append(mood)
        rows.append(" &middot; ".join(bits))
    if not rows:
        return ""
    content = (
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:10px;'
        f"color:{head_color};"
        f'line-height:1.8;">{"<br>".join(rows)}</div>'
    )
    return _conversation_block("Driving the conversation", True, content, color, pal, dark)


def _seed_path(brief: dict, pal: dict, dark: bool = False) -> str:
    """Behaviour path block (A6b). Self-hides when seed_path is absent."""
    sp = brief.get("seed_path") or {}
    channels = sp.get("channels") or []
    if not channels:
        return ""
    term = clean_copy(str(sp.get("term") or "")).strip()
    if term.startswith("@"):
        term = term[1:]
    rows: list[str] = []
    for ch in channels[:5]:
        plat = esc(clean_copy(str(ch.get("platform") or "")))
        fs = esc(str(ch.get("first_seen") or ""))
        if plat:
            rows.append(f"{plat}{(' · ' + fs) if fs else ''}")
    if not rows:
        return ""
    conf = esc(str(sp.get("confidence") or "thin"))
    note = clean_copy(str(sp.get("coverage_note") or "")).strip()
    body = f"<b>{esc(term or 'term')}</b> · {conf}<br>{'<br>'.join(rows)}"
    if note:
        body += f'<br><span style="opacity:0.85">{esc(note)}</span>'
    color = pal["on_ink_warm"] if dark else pal["muted"]
    content = (
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:10px;'
        f'color:{color};line-height:1.8;">{body}</div>'
    )
    return _labeled_block("Seed path", content, pal, dark)


def _labeled_block(label: str, body: str, pal: dict, dark: bool = False) -> str:
    """A card sub-block: a small mono label above a body (Seen on, etc.)."""
    label_color = pal["on_ink_warm"] if dark else pal["muted"]
    label_cls = CLS["accent"] if dark else CLS["mute"]
    return (
        f'<div style="margin-top:22px;"><div class="{label_cls}" '
        f'style="{mono_label_style(pal, color=label_color)}'
        f'margin-bottom:10px;">{label}</div>{body}</div>'
    )


def render_card(brief: dict, display: dict, brand: dict, hero: bool = False) -> str:
    """Render one trend card. ``hero`` selects the dark hero treatment.

    For the hero, the outer wrapper and class set differ (the hero markup uses
    h1 + a band + a hero-foot), so the hero is composed in ``__init__`` via
    ``render_hero``; this function is the standard ``.card`` used for moves.
    """
    pal = palette(brand)
    headline = brief_headline(brief)
    flagline = _flagline(brief, display, brand, pal, dark=True)
    momentum = _momentum(display, pal)
    head_left = flagline
    if headline:
        # guard the truthy path: a headline only prints when it is a real,
        # non-blank field, so a future regression that emits an empty one raises
        head_left += (
            f'<div class="{CLS["ink"]}" style="font-family:{FONT_SERIF};font-weight:bold;'
            f"font-size:22px;"
            f'line-height:1.12;letter-spacing:-0.02em;color:{pal["on_ink"]};">'
            f"{esc(assert_sourced(headline, 'headline'))}</div>"
        )
    read = clean_copy(str(brief.get("trend_synthesis") or "")).strip()
    read_html = ""
    if read:
        read_html = (
            f'<div class="{CLS["ink2"]}" style="font-family:{FONT_BODY};font-size:13.5px;'
            f"line-height:1.55;"
            f'color:{pal["on_ink_soft"]};margin-top:16px;">{esc(read)}</div>'
        )
    chips = _chips(brief, display, pal, dark=True)
    chips_block = f'<div style="margin-top:16px;">{chips}</div>' if chips else ""
    seen = render_seen(display.get("channels") or [], pal, show_label=False, dark=True)
    seen_block = _labeled_block("Seen on", seen, pal, dark=True) if seen else ""
    drivers = _drivers(brief, pal, dark=True)
    room = _room(brief, brand, str(brief.get("market") or ""), pal, dark=True)
    hashtags = _hashtags(brief, brand, str(brief.get("market") or ""), pal, dark=True)
    seed_path_block = _seed_path(brief, pal, dark=True)
    kit = f'<div style="margin-top:22px;">{render_kit(brief, display, brand, compact=True)}</div>'
    foot = _card_foot(brief, display, pal)

    head = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;"><tr>'
        f'<td valign="top">{head_left}</td>'
        f'<td valign="top" align="right" style="padding-left:16px;">{momentum}</td>'
        "</tr></table>"
    )
    inner = f"{head}{read_html}{chips_block}{seen_block}{drivers}{room}{hashtags}{seed_path_block}{kit}{foot}"
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;margin-bottom:16px;"><tr>'
        f'<td class="{CLS["surface"]} {CLS["rule"]}" bgcolor="{pal["card_dark"]}" '
        f'style="background-color:{pal["card_dark"]};'
        f'border:1px solid {pal["dark_rule"]};padding:22px;">{inner}</td>'
        "</tr></table>"
    )


def _card_foot(brief: dict, display: dict, pal: dict) -> str:
    confidence = str(display.get("confidence") or "")
    score = brief.get("trend_score")
    bits = []
    if confidence:
        bits.append(esc(confidence))
    if score is not None:
        # "trend score" not "score" so it reads distinctly from the SEED chip's
        # percentage; the two measure different things (hotness vs activation fit).
        bits.append(f"trend score {float(score):.2f}")
    left = " &middot; ".join(bits)
    proof = proof_anchor(brief, "Proof &rarr;", color=pal["on_ink_warm"])
    return (
        f'<table class="{CLS["rule"]}" role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;margin-top:22px;border-top:1px solid '
        f'{pal["dark_rule"]};"><tr>'
        f'<td class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:10px;'
        f"color:{pal['on_ink_mute']};"
        f'letter-spacing:0.03em;padding-top:16px;">{left}</td>'
        f'<td align="right" style="padding-top:16px;">{proof}</td>'
        "</tr></table>"
    )
