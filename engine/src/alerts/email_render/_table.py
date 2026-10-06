"""Email-safe layout helpers for the PULSE v2 mailer.

Outlook on Windows renders with the Word engine: it strips the head ``<style>``
block and does not support flexbox or grid. So every aligned row in PULSE has to
be a ``<table>`` and every style has to be inline on the element. This module is
the one place those table and inline-style fragments live, so the renderers stay
DRY and the look is consistent.

The palette is read from the brand config (``brand["palette"]`` plus the locked
editorial tokens) via ``palette()``, so a white-label client skin still flows
through. ``FONT_SERIF`` / ``FONT_BODY`` / ``FONT_MONO`` are the web-safe stacks
the design degrades to; the Fraunces / Archivo display faces ride only in the
optional ``<style>`` @import (progressive enhancement, see ``_style.py``).
"""

from __future__ import annotations

# Web-safe font stacks, declared inline on every text element so the email reads
# correctly with the head <style> deleted (Outlook deletes it). The editorial
# Fraunces / Archivo / JetBrains Mono faces are an enhancement layered on top via
# the @import in _style.py; clients that keep it upgrade, clients that strip it
# fall back to exactly these stacks.
# The v3 "midnight signal-desk" stacks. Newsreader serif leads the headline /
# wordmark / score stack, Hanken Grotesk leads the body and the wide-tracked
# uppercase eyebrows. Both ride only in the optional @import (see _style.py);
# the inline web-safe fallbacks (Georgia / Segoe UI / Arial) are first-class so
# the email reads when Outlook deletes the head <style>. FONT_MONO keeps its
# name for the readers across the package but now points at the Hanken stack;
# numeric alignment is done with inline font-variant-numeric:tabular-nums on the
# score / ratio / KPI cells, not a monospace face (the mockup has no mono).
FONT_SERIF = "'Newsreader',Georgia,serif"
FONT_BODY = "'Hanken Grotesk','Segoe UI',Arial,sans-serif"
FONT_MONO = "'Hanken Grotesk','Segoe UI',Arial,sans-serif"

# ADAPTIVE THEMING. The inline base is now LIGHT (the concept-3 "paper terminal"
# daylight skin). Every renderer reads ``pal[...]`` and writes that LIGHT value
# inline + as the cell ``bgcolor``, so Gmail, Outlook on Windows, and any
# light-mode reader see the paper look with the head <style> stripped. DARK is an
# override layer: ``_style.py`` carries an ``@media (prefers-color-scheme: dark)``
# block (+ Outlook.com ``[data-ogsc]`` / ``[data-ogsb]`` rules) that flips each
# semantic ``lp-*`` class to its midnight value with !important. Apple Mail and
# Outlook.com follow the reader's theme; Gmail and Outlook desktop stay on the
# light inline base. ``CLS`` maps each palette role to the class hook a renderer
# attaches next to its inline color; ``DARK`` is the value the override sets.
#
# The brand config still overrides ink / paper / accent and the per-market dots;
# the rest of the tokens (status colors, rules, surfaces) stay fixed so a second
# brand keeps the same signal-desk furniture in both themes.
_DEFAULT_PALETTE = {
    # In the light base ``ink`` is the page CANVAS (paper), not a dark fill. The
    # renderers use ``ink`` as the section/canvas background and ``on_ink*`` as
    # the text on it; mapped to the light page + light ink tones below so the
    # existing role usage stays correct without renaming every call site.
    "ink": "#f3efe6",
    "paper": "#f3efe6",
    "paper2": "#ece7df",
    "card": "#fbf9f3",
    # The lifted surface a card / panel reads off the page.
    "card_dark": "#ece7df",
    # The masthead band and the ticker strip stay a dark band on the light page
    # (concept-3 keeps a dark header lockup), with light text on it.
    "header_band": "#1c1611",
    # The SEEN-ON / pip wells and the board-row hairline base.
    "well": "#ece7df",
    "vermillion": "#2f5fd0",
    "vermillion_ink": "#2f5fd0",
    "pos": "#2e9e63",
    "neu": "#7c766b",
    "neg": "#c0432b",
    # Cooling is a distinct direction from negative (a cooling trend is not bad).
    "cooling": "#2d76a3",
    # Building badge.
    "building": "#9a6a12",
    "line": "#e3ddd0",
    "line_soft": "#e3ddd0",
    "muted": "#6f6a60",
    "ink2": "#544f48",
    # Section "on canvas" text tones (now on the light page).
    "on_ink": "#2b2722",
    "on_ink_soft": "#544f48",
    "on_ink_mute": "#6f6a60",
    # on_ink_dim carries normal-size text (footer credit / method line, board
    # score labels, teaser headline meta, masthead issue date). On the light
    # paper #f3efe6 the old #9a9488 was 2.63:1, below WCAG AA 4.5:1; Gmail and
    # Outlook desktop strip the dark overrides so this light skin is what most
    # recipients see. #6f6a60 clears 4.5:1 on the paper (4.68:1).
    "on_ink_dim": "#6f6a60",
    "on_ink_warm": "#2f5fd0",
    "dark_rule": "#e3ddd0",
    # A second, lighter rule for the dashed NOT MEASURED border + empty pip cells.
    "dark_rule_2": "#d2ccc0",
    "dark_chip_line": "#d2ccc0",
    # The move-this-week panel fill (a soft blue wash, distinct from card_dark).
    "accent_soft": "#e7ecfb",
}

# The DARK override values, keyed by the SAME role names as the light base. The
# ``@media (prefers-color-scheme: dark)`` block in _style.py sets each role's
# ``lp-*`` class to this value with !important. These are the midnight "signal
# desk" tones the v3 design shipped with before the adaptive conversion. The
# header_band stays a dark band in both themes (it is already dark on light).
_DARK_PALETTE = {
    "ink": "#1b1714",
    "paper": "#1b1714",
    "paper2": "#262019",
    "card": "#15110e",
    "card_dark": "#262019",
    "header_band": "#1c1611",
    "well": "#15110e",
    # The dark accent splits by role: the hero band FILL (lp-ab) is the darker
    # #3a63c4 so white-on-band clears AA, while text-accent (lp-accent /
    # vermillion_ink) keeps the lighter #5e87f0 so accent text on the dark canvas
    # stays above AA. The live override values are in _style.py; this dict is
    # reference only.
    "vermillion": "#3a63c4",
    "vermillion_ink": "#5e87f0",
    "pos": "#56c98a",
    "neu": "#9c958a",
    "neg": "#e2654a",
    "cooling": "#6aa3d8",
    "building": "#dfb44a",
    "line": "#37322c",
    "line_soft": "#46403a",
    "muted": "#9c958a",
    "ink2": "#c8c2b7",
    "on_ink": "#f4f1ea",
    "on_ink_soft": "#c8c2b7",
    "on_ink_mute": "#9c958a",
    "on_ink_dim": "#8d857a",
    "on_ink_warm": "#5e87f0",
    "dark_rule": "#37322c",
    "dark_rule_2": "#46403a",
    "dark_chip_line": "#2b2622",
    "accent_soft": "#222a3d",
}

# Palette role -> the semantic CSS class a renderer attaches next to the inline
# LIGHT color so the dark @media block can override it. One class per role; a few
# roles share a class where the dark override is the same (e.g. every canvas-bg
# element is ``lp-bg``). The renderers reference these via ``CLS["bg"]`` so the
# class strings live in one place and stay in sync with the _style.py overrides.
CLS = {
    "bg": "lp-bg",  # the page / section canvas (ink / paper)
    "surface": "lp-sf",  # a lifted panel fill (card_dark / well)
    "card": "lp-cd",  # the deepest well fill (card / well)
    "band": "lp-bd",  # the dark header band (header_band)
    "ink": "lp-ink",  # primary text (on_ink)
    "ink2": "lp-i2",  # secondary text (on_ink_soft / ink2)
    "mute": "lp-mu",  # muted text (on_ink_mute / muted)
    "faint": "lp-ft",  # faint text (on_ink_dim)
    "rule": "lp-rl",  # hairline rule (dark_rule / line)
    "rule2": "lp-r2",  # lighter rule (dark_rule_2 / dark_chip_line)
    "accent": "lp-accent",  # accent text (vermillion / vermillion_ink)
    "accent_bg": "lp-ab",  # accent fill (the hero band)
    "accent_soft": "lp-bs",  # the move-this-week wash (accent_soft)
    "pos": "lp-po",
    "neu": "lp-nu",
    "neg": "lp-ng",
    # Background-fill variants of the tone classes. The text classes above get a
    # dark COLOR override only; these get a dark BACKGROUND override only. Sharing
    # one class for both roles painted text spans green-on-green in dark mode.
    "pos_bg": "lp-pob",
    "neu_bg": "lp-nub",
    "neg_bg": "lp-ngb",
    "cooling": "lp-co",
    "building": "lp-bu",
    "pip_on": "lp-pn",  # a filled confidence pip (accent fill)
    "pip_off": "lp-pf",  # an empty confidence pip (rule fill)
    "chip": "lp-ch",  # a chip resting border / text
    "chip_line": "lp-cl",  # the chip-line fill (heatmap bar track)
}


def klass(role: str) -> str:
    """The ``class="lp-..."`` attribute fragment for a palette role, or "".

    A tiny convenience so a renderer writes ``klass("ink")`` instead of the
    literal ``' class="lp-ink"'``. Unknown roles return "" so a typo degrades to
    no class (the inline light color still renders) rather than raising.
    """
    name = CLS.get(role)
    return f' class="{name}"' if name else ""


def palette(brand: dict) -> dict:
    """Resolve the full token set for a brand, falling back to the locked skin.

    Reads ``brand["palette"]`` (ink / paper / accent) and ``market_colors`` and
    merges them over the editorial defaults, so the renderers pull every color
    from here and a white-label brand only has to declare the three brand colors.
    ``accent`` maps onto vermillion (the headline accent) and a darker
    ``vermillion_ink`` is derived for on-paper text use.
    """
    pal = dict(_DEFAULT_PALETTE)
    brand_pal = brand.get("palette") or {}
    if brand_pal.get("ink"):
        pal["ink"] = brand_pal["ink"]
    if brand_pal.get("paper"):
        pal["paper"] = brand_pal["paper"]
        pal["on_ink"] = brand_pal["paper"]
    if brand_pal.get("accent"):
        pal["vermillion"] = brand_pal["accent"]
        # A second brand only declares one accent; keep the on-paper ink-accent
        # the same so the text-accent reads against the paper without a second
        # config knob. The locked Ogilvy skin keeps its hand-tuned darker ink.
        if not _is_ogilvy(brand_pal):
            pal["vermillion_ink"] = brand_pal["accent"]
    return pal


def _is_ogilvy(brand_pal: dict) -> bool:
    return str(brand_pal.get("accent") or "").lower() == "#2f5fd0"


def market_dot_color(market: str, brand: dict) -> str:
    """Per-market dot color from the brand config, neutral fallback."""
    m = str(market or "").strip().lower()
    colors = brand.get("market_colors") or {}
    return colors.get(m, _DEFAULT_PALETTE["muted"])


# --- table builders ------------------------------------------------------
#
# Every multi-column or aligned block is a presentation table. role=presentation
# tells screen readers it is layout, not data. cellpadding/cellspacing/border are
# zeroed on the attribute (Outlook reads attributes, not the reset stylesheet it
# stripped). Width is set both as an attribute and inline for the same reason.


def table_open(width: str = "100%", extra_style: str = "") -> str:
    """Open a presentation table. ``width`` is "100%" or a pixel number string."""
    w = width
    style = f"border-collapse:collapse;{('width:' + w + 'px;') if w.isdigit() else 'width:100%;'}"
    if w.isdigit():
        style += f"max-width:{w}px;"
    if extra_style:
        style += extra_style
    return (
        f'<table role="presentation" width="{w}" cellpadding="0" cellspacing="0" '
        f'border="0" style="{style}">'
    )


TABLE_CLOSE = "</table>"


def row(cells: str, extra_style: str = "") -> str:
    """A single ``<tr>`` wrapping pre-built ``<td>`` cells."""
    style = f' style="{extra_style}"' if extra_style else ""
    return f"<tr{style}>{cells}</tr>"


def cell(
    body: str,
    *,
    style: str = "",
    width: str = "",
    align: str = "",
    valign: str = "",
    bgcolor: str = "",
    colspan: int = 0,
    cls: str = "",
) -> str:
    """A ``<td>`` with inline style plus the attributes Outlook needs.

    ``bgcolor`` is emitted as BOTH the attribute and an inline ``background-color``
    because Outlook honours the attribute and other clients the inline rule.
    ``width`` / ``align`` / ``valign`` are emitted as attributes for the same
    Outlook reason. ``cls`` is a space-separated class list (the semantic ``lp-*``
    hook(s) the dark @media override targets); the light ``bgcolor`` stays the
    inline base. Padding and the rest of the look go in ``style``.
    """
    attrs = ""
    if cls:
        attrs += f' class="{cls}"'
    if width:
        attrs += f' width="{width}"'
    if align:
        attrs += f' align="{align}"'
    if valign:
        attrs += f' valign="{valign}"'
    if colspan:
        attrs += f' colspan="{colspan}"'
    full_style = style
    if bgcolor:
        attrs += f' bgcolor="{bgcolor}"'
        full_style = f"background-color:{bgcolor};{full_style}"
    style_attr = f' style="{full_style}"' if full_style else ""
    return f"<td{attrs}{style_attr}>{body}</td>"


def spacer_row(height: int, bgcolor: str = "") -> str:
    """A fixed-height spacer row. Outlook ignores margin, so vertical rhythm

    between blocks is done with an empty cell of a set line-height + height.
    """
    bg = f' bgcolor="{bgcolor}"' if bgcolor else ""
    bg_style = f"background-color:{bgcolor};" if bgcolor else ""
    return (
        f'<tr><td{bg} style="{bg_style}font-size:0;line-height:{height}px;'
        f'height:{height}px;">&nbsp;</td></tr>'
    )


def hr_row(color: str, *, pad_x: int = 0) -> str:
    """A 1px horizontal rule as a bordered cell (Outlook-safe divider)."""
    pad = f"padding:0 {pad_x}px;" if pad_x else ""
    return (
        f'<tr><td style="{pad}font-size:0;line-height:0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:100%;border-collapse:collapse;">'
        f'<tr><td style="border-top:1px solid {color};font-size:0;line-height:1px;">'
        "&nbsp;</td></tr></table></td></tr>"
    )


# --- inline text-style fragments -----------------------------------------
#
# Common label / body styles assembled from the font stacks + palette so a
# renderer writes label_style(pal) rather than repeating the whole declaration.


def eyebrow_style(pal: dict, color: str | None = None) -> str:
    """The mono kicker label above a section (tracked, uppercase).

    ``color`` overrides the default on-paper accent so a dark section passes the
    bright accent without a second ``color`` declaration (a duplicate inline
    property Outlook is better off not seeing).
    """
    return (
        f"font-family:{FONT_MONO};font-size:10px;font-weight:700;"
        f"letter-spacing:0.16em;text-transform:uppercase;color:{color or pal['vermillion_ink']};"
    )


def mono_label_style(pal: dict, size: str = "9px", color: str | None = None) -> str:
    """A small mono row label (the labels inside cards and the kit dt).

    ``color`` overrides the default muted tone so a dark section passes the
    on-dark tone without a duplicate ``color`` declaration.
    """
    return (
        f"font-family:{FONT_MONO};font-size:{size};font-weight:700;"
        f"letter-spacing:0.14em;text-transform:uppercase;color:{color or pal['muted']};"
    )
