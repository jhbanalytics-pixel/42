"""The Kit renderer for the PULSE v2 mailer.

Ports the ``.kit`` block from the locked mockup. The Kit always renders the
activation as the Move (from ``cultural_context``). Make it (the Nano Banana
prompt) and Sound it (the Lyria prompt) render only when those fields exist.
Cast it renders from the real top creators. The image-prompt line states the
exclusions are hard rules the model must not render; it never uses the word
verbatim.

Email-safe: the Kit is a dark block (bgcolor + inline) and the dt/dd grid is a
2-column presentation table (a label cell + a value cell per row), never
``display:grid``. Binding rule holds: a row whose field is absent is dropped,
never invented.
"""

from __future__ import annotations

from src.alerts.email_render._table import (
    CLS,
    FONT_BODY,
    FONT_MONO,
    FONT_SERIF,
    palette,
)
from src.alerts.email_render._util import clean_copy, esc, parse_creator
from src.analysis.prompts.trend_brief import IMAGE_EXCLUSION_CLAUSE

# Short strategist reminder. The full brand-safe exclusion clause rides inside
# the Nano Banana prompt itself (a creator pastes the prompt), so this note does
# NOT re-list the exclusions, it just flags the clause is mandatory. Re-listing
# them here printed the whole list twice in the Make it cell. Dash-free, and the
# word verbatim never appears.
_EXCLUSION_NOTE = (
    "Brand-safe exclusions are baked into the prompt above. Keep every word when "
    "you paste, do not let them be edited out."
)

# Marker that tells us the model already embedded the exclusion clause in its
# prompt. If a generated prompt is missing it (model variance), _make_row
# appends the canonical clause so the pasted prompt is never brand-unsafe and
# the exclusion still appears exactly once.
_EXCLUSION_MARKER = "no brand logos"


def _kit_row(
    pal: dict,
    dt: str,
    dd_body: str,
    *,
    dd_color: str = "",
    dd_cls: str = "",
    risk: bool = False,
) -> str:
    """One Kit row: a mono label cell and a value cell, in a 2-column table row.

    ``dt`` is the short uppercase label, ``dd_body`` the pre-built value HTML.
    ``dd_color`` overrides the value text color (the Move reads accent, the Check
    reads warning-red); ``dd_cls`` is the matching semantic class so the override
    flips in dark mode. ``risk`` also tints the label. The resting label + value
    read in the muted / secondary roles so they sit under the brighter overrides.
    """
    dt_color = pal["on_ink_warm"] if risk else pal["muted"]
    dt_cls = CLS["accent"] if risk else CLS["mute"]
    value_color = dd_color or pal["on_ink_soft"]
    value_cls = dd_cls or CLS["ink2"]
    weight = ";font-weight:bold" if risk else ""
    return (
        "<tr>"
        f'<td width="74" valign="top" class="{dt_cls}" style="width:74px;font-family:{FONT_MONO};'
        f"font-size:9.5px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;"
        f'color:{dt_color};padding:2px 16px 10px 0;">{dt}</td>'
        f'<td valign="top" class="{value_cls}" style="font-family:{FONT_BODY};font-size:13px;'
        f'line-height:1.5;color:{value_color}{weight};padding:0 0 10px 0;">{dd_body}</td>'
        "</tr>"
    )


def _move_row(brief: dict, pal: dict) -> str:
    move = clean_copy(str(brief.get("cultural_context") or "")).strip()
    if not move:
        return ""
    return _kit_row(pal, "Move", esc(move), dd_color=pal["vermillion"], dd_cls=CLS["accent"])


def _make_row(brief: dict, pal: dict) -> str:
    prompt = clean_copy(str(brief.get("nano_banana_prompt") or "")).strip()
    if not prompt:
        return ""
    # The exclusion clause must travel inside the pasted prompt. The model is
    # told to include it, but if a given prompt is missing it, append the
    # canonical clause so it is never absent and never doubled.
    if _EXCLUSION_MARKER not in prompt.lower():
        prompt = prompt.rstrip(".") + "." + IMAGE_EXCLUSION_CLAUSE
    # The strategist reminder sits on its OWN line below the prompt, set off by a
    # hairline rule, so it reads as a separate brand-safety note rather than
    # trailing inline off the end of the prompt sentence.
    body = (
        f'<b class="{CLS["ink"]}" style="color:{pal["on_ink"]};font-weight:bold;">Nano Banana:</b> '
        f"{esc(prompt)}"
        f'<div class="{CLS["mute"]} {CLS["rule"]}" style="font-family:{FONT_MONO};font-size:9px;'
        f"color:{pal['on_ink_mute']};letter-spacing:0.03em;line-height:1.55;margin-top:9px;"
        f'padding-top:8px;border-top:1px solid {pal["dark_rule"]};">{esc(_EXCLUSION_NOTE)}</div>'
    )
    return _kit_row(pal, "Make it", body)


def _sound_row(brief: dict, pal: dict) -> str:
    prompt = clean_copy(str(brief.get("lyria_prompt") or "")).strip()
    if not prompt:
        return ""
    body = (
        f'<b class="{CLS["ink"]}" style="color:{pal["on_ink"]};font-weight:bold;">Lyria:</b> '
        f"{esc(prompt)}"
    )
    return _kit_row(pal, "Sound it", body)


def _cast_row(brief: dict, pal: dict) -> str:
    creators = brief.get("top_creators") or []
    handles: list[str] = []
    for entry in creators:
        c = parse_creator(entry)
        if c is None:
            continue
        handles.append(esc(c["handle"]))
    if not handles:
        return ""
    return _kit_row(pal, "Cast it", ", ".join(handles))


# Platform slug -> the display label for the Where row. Long-form YouTube and
# YouTube Shorts are distinct surfaces in the schema, so plain "youtube" stays
# "YouTube" and only "youtube shorts" reads as Shorts; relabelling plain YouTube
# as Shorts told the strategist to run on the wrong surface.
_WHERE_LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
    "instagram reels": "Instagram Reels",
    "youtube": "YouTube",
    "youtube shorts": "YouTube Shorts",
    "threads": "Threads",
    "twitter": "Twitter",
    "facebook": "Facebook",
}


def _where_row(brief: dict, pal: dict) -> str:
    """Where to run it: the brief's primary platforms, display-cased.

    Reads ``platforms`` (the model's descending-share platform list) and names
    the top few surfaces. Absent or empty platforms drop the row.
    """
    platforms = [str(p).strip() for p in (brief.get("platforms") or []) if str(p).strip()]
    if not platforms:
        return ""
    seen: list[str] = []
    for p in platforms[:4]:
        label = _WHERE_LABELS.get(p.lower(), p)
        if label not in seen:
            seen.append(label)
    return _kit_row(pal, "Where", esc(", ".join(seen)))


def _say_row(brief: dict, pal: dict) -> str:
    """Say it: the on-feed hook line, derived from the visual_anchor.

    The pipeline has no dedicated caption field, so per the binding rule this
    reuses the ``visual_anchor`` fingerprint (the "On The Feed:" line) as the
    creative hook a strategist says out loud. Strips the literal prefix so the
    Kit reads as a directive. Omitted when there is no anchor.
    """
    anchor = clean_copy(str(brief.get("visual_anchor") or "")).strip()
    if not anchor:
        return ""
    for prefix in ("On The Feed:", "On the feed:", "On The Feed", "On the feed"):
        if anchor.startswith(prefix):
            anchor = anchor[len(prefix) :].strip(" :")
            break
    if not anchor:
        return ""
    return _kit_row(pal, "Say it", esc(anchor))


def _tags_row(brief: dict, pal: dict) -> str:
    """Tag it: the hashtags driving this trend's conversation.

    Reads the top ``driving_hashtags`` (the same producer that lights the
    conversation card) so the kit hands a creator the exact tags to ride. A caged
    or unlit topic carries no tags, so the row simply drops, never invented.
    """
    tags = brief.get("driving_hashtags") or []
    out: list[str] = []
    for t in tags:
        if not isinstance(t, dict):
            continue
        tag = clean_copy(str(t.get("tag") or "")).strip()
        if tag and tag not in out:
            out.append(tag)
    if not out:
        return ""
    return _kit_row(pal, "Tag it", esc("  ".join(out[:5])))


def _risk_row(brief: dict, pal: dict) -> str:
    """Per-trend brand-safety caution from the brief's ``risk_flags``.

    Brand-safety reviews asked for risk findings to surface as a visible Check
    line: when the brief carries them (a competitor-category overlap, a
    sensitive-context flag), the desk can vet before brief. Distinct from the
    fixed exclusion note on the image prompt, which is the prompt-side safety
    belt. Absent or empty flags drop the row, never a blank caution.
    """
    flags = [str(f).strip() for f in (brief.get("risk_flags") or []) if str(f).strip()]
    if not flags:
        return ""
    body = "; ".join(esc(clean_copy(f)) for f in flags)
    return _kit_row(pal, "Check", body, dd_color=pal["neg"], dd_cls=CLS["neg"], risk=True)


def render_kit(brief: dict, display: dict, brand: dict, compact: bool = False) -> str:
    """Render the Kit block. Returns an empty string only if nothing binds.

    ``compact`` (the move cards) renders only the Move row (the one-line kit the
    v3 mockup shows on a move card); the full eight-row kit stays on the hero and
    the hosted full read, so nothing bound elsewhere is dropped. The brand-safety
    exclusion still rides the hero's full Make it row.
    """
    pal = palette(brand)
    if compact:
        rows = _move_row(brief, pal)
    else:
        rows = "".join(
            [
                _move_row(brief, pal),
                _make_row(brief, pal),
                _sound_row(brief, pal),
                _say_row(brief, pal),
                _tags_row(brief, pal),
                _where_row(brief, pal),
                _cast_row(brief, pal),
                _risk_row(brief, pal),
            ]
        )
    if not rows:
        return ""
    header = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="{CLS["rule"]}" style="width:100%;border-collapse:collapse;'
        f'border-bottom:1px solid {pal["dark_rule"]};margin-bottom:16px;">'
        "<tr>"
        f'<td valign="bottom" class="{CLS["ink"]}" style="font-family:{FONT_SERIF};'
        f'font-weight:bold;font-size:19px;color:{pal["on_ink"]};padding:0 0 10px 0;">The kit</td>'
        f'<td align="right" valign="bottom" class="{CLS["accent"]}" '
        f'style="font-family:{FONT_MONO};font-size:8.5px;letter-spacing:0.16em;'
        f"text-transform:uppercase;color:{pal['vermillion']};"
        'padding:0 0 10px 0;">ready to brief</td>'
        "</tr></table>"
    )
    grid = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;">{rows}</table>'
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;border-collapse:collapse;">'
        "<tr>"
        f'<td bgcolor="{pal["ink"]}" class="{CLS["bg"]}" '
        f'style="background-color:{pal["ink"]};padding:22px;">'
        f"{header}{grid}</td>"
        "</tr></table>"
    )
