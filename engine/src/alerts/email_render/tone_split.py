"""Tone-split section for the PULSE v2 mailer (Wave 2).

A daily positive / neutral / negative read of the day's social conversation,
built from the social sentiment lexicon. The engine's Wave 2 lexicon scorer
writes ``sentiment_lexicon_score`` on each ``enriched_content`` row (a float in
[-1.0, 1.0], NULL until SENTIMENT_LEXICON_ENABLED is on). Those per-row scores
are carried onto the day's briefs as a ``sentiment_lexicon_scores`` list; this
section bins them into the three tone buckets by the locked thresholds and
renders a single stacked share bar so the desk sees the mood balance at a glance.

Dark on ship. The whole section is gated behind TONE_SPLIT_ENABLED and renders
nothing when the flag is off, so today's email is byte-identical. Even with the
flag on it returns "" when no brief carries a usable score (the column is NULL
across the feed until the lexicon flag flips on the engine), so a quiet day never
prints empty furniture. Every value is derived from the stored scores, nothing is
modelled.

Email-safe: the stacked bar is a presentation-table row whose three coloured
segments are ``bgcolor`` cells with percentage widths (Outlook ignores CSS
background on a cell, so each segment is an attribute-coloured ``<td>``), never a
flex or grid track.
"""

from __future__ import annotations

import os

from src.alerts.email_render._table import CLS, FONT_MONO, palette
from src.alerts.email_render._util import esc

# Locked polarity thresholds on the [-1.0, 1.0] lexicon score. A score at or
# above POS_FLOOR reads positive, at or below NEG_CEIL reads negative, the band
# between is neutral. These match the Pulse desk's toneOf bands so the two
# surfaces label the same score the same way.
_POS_FLOOR = 0.15
_NEG_CEIL = -0.15

# Brief field carrying the per-row lexicon scores rolled up for that topic. The
# engine attaches it post-scoring; absent on every brief today.
_SCORES_KEY = "sentiment_lexicon_scores"


def _tone_enabled() -> bool:
    """Whether the tone split is flagged on (TONE_SPLIT_ENABLED).

    Read per call so it flips on the live job and toggles in tests without a
    code change. Dark by default: any value outside the truthy set keeps the
    section off, so a flag-off render is byte-identical to today.
    """
    return os.environ.get("TONE_SPLIT_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _collect_scores(briefs: list[dict]) -> list[float]:
    """Pull every usable ``sentiment_lexicon_score`` off the day's briefs.

    Each brief may carry a ``sentiment_lexicon_scores`` list (the per-row scores
    for that topic). A non-numeric entry, or one outside [-1.0, 1.0], is dropped
    so a malformed value never skews the split. Returns an empty list when no
    brief carries a usable score, which is the state today.
    """
    out: list[float] = []
    for brief in briefs:
        for raw in brief.get(_SCORES_KEY) or []:
            if isinstance(raw, bool):
                continue
            if not isinstance(raw, (int, float)):
                continue
            score = float(raw)
            if score < -1.0 or score > 1.0:
                continue
            out.append(score)
    return out


def _split_counts(scores: list[float]) -> tuple[int, int, int]:
    """Bin the scores into (positive, neutral, negative) counts."""
    pos = sum(1 for s in scores if s >= _POS_FLOOR)
    neg = sum(1 for s in scores if s <= _NEG_CEIL)
    neu = len(scores) - pos - neg
    return pos, neu, neg


def _segment(pct: int, color: str, cls: str) -> str:
    """One coloured segment of the stacked bar, a bgcolor cell at pct width."""
    if pct <= 0:
        return ""
    return (
        f'<td bgcolor="{color}" class="{cls}" style="background-color:{color};width:{pct}%;'
        'font-size:0;line-height:14px;height:14px;">&nbsp;</td>'
    )


def _legend_cell(label: str, pct: int, dot: str, text_color: str, dot_cls: str) -> str:
    """One legend entry: a tone dot, the label, and the share percent."""
    return (
        f'<td valign="middle" class="{CLS["ink2"]}" style="font-family:{FONT_MONO};font-size:9.5px;'
        f'letter-spacing:0.04em;color:{text_color};padding:0 14px 0 0;white-space:nowrap;">'
        f'<span class="{dot_cls}" style="display:inline-block;width:8px;height:8px;'
        f'background-color:{dot};vertical-align:middle;margin-right:6px;">&nbsp;</span>'
        f"{esc(label)} {pct}%</td>"
    )


def render_tone_split(briefs: list[dict], brand: dict) -> str:
    """Render the tone-split section row, or "" when off/empty.

    Returns "" when TONE_SPLIT_ENABLED is off (dark on ship) OR when no brief
    carries a usable ``sentiment_lexicon_score`` (the column is NULL across the
    feed today, so the section prints no empty furniture). Otherwise a dark
    full-width section ``<tr>`` matching the moves block: a mono eyebrow over a
    stacked positive / neutral / negative bar with the three shares, computed
    from the stored scores. Defensive: every value derives from the lexicon
    scores, nothing is invented.
    """
    if not _tone_enabled():
        return ""
    scores = _collect_scores(briefs)
    if not scores:
        return ""
    total = len(scores)
    pos, _neu, neg = _split_counts(scores)
    pal = palette(brand)
    # Round the shares so they read cleanly; the bar widths use the same rounded
    # values so the segments and the legend always agree.
    pos_pct = round(100 * pos / total)
    neg_pct = round(100 * neg / total)
    neu_pct = max(0, 100 - pos_pct - neg_pct)
    segments = (
        _segment(pos_pct, pal["pos"], CLS["pos_bg"])
        + _segment(neu_pct, pal["neu"], CLS["neu_bg"])
        + _segment(neg_pct, pal["neg"], CLS["neg_bg"])
    )
    bar = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;border-radius:3px;overflow:hidden;">'
        f"<tr>{segments}</tr></table>"
    )
    soft = pal["on_ink_soft"]
    legend = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;margin-top:12px;"><tr>'
        + _legend_cell("Positive", pos_pct, pal["pos"], soft, CLS["pos_bg"])
        + _legend_cell("Neutral", neu_pct, pal["neu"], soft, CLS["neu_bg"])
        + _legend_cell("Negative", neg_pct, pal["neg"], soft, CLS["neg_bg"])
        + "</tr></table>"
    )
    eyebrow = (
        f'<div class="{CLS["accent"]}" style="font-family:{FONT_MONO};font-size:10px;font-weight:700;'
        f"letter-spacing:0.2em;text-transform:uppercase;color:{pal['vermillion']};"
        f'margin-bottom:16px;">How the day felt &middot; tone split</div>'
    )
    count_note = (
        f'<div class="{CLS["mute"]}" style="font-family:{FONT_MONO};font-size:9px;color:{pal["on_ink_mute"]};'
        f'letter-spacing:0.03em;margin-top:10px;">{esc(f"{total:,}")} scored posts</div>'
    )
    return (
        f'<tr><td bgcolor="{pal["ink"]}" class="lp-pd {CLS["bg"]}" '
        f'style="background-color:{pal["ink"]};padding:30px 26px;">{eyebrow}{bar}{legend}{count_note}</td></tr>'
    )
