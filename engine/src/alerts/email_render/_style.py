"""Optional progressive-enhancement ``<style>`` block for the PULSE v3 mailer.

Nothing load-bearing lives here. The whole email is laid out with presentation
tables and inline styles (see the renderers and ``_table.py``), so it reads
correctly with this block deleted, which is exactly what Outlook on Windows
does. This block adds enhancements for the clients that keep it:

1. the display faces (Newsreader serif + Hanken Grotesk sans) via an
   ``@import``. Clients that strip it fall back to the Georgia / Segoe UI /
   Arial stacks declared inline on every element, the intended degrade.
2. the mobile ``@media`` stack: it lets the 600px column shrink to full width,
   reflows the KPI cards two-up then one-up, hides the ticker, lifts the body to
   16px, trims section padding, and pins 44px tap targets. The hybrid
   inline-block layout already reflows without any of this; the media rules only
   polish. Outlook ignores ``@media`` and keeps the desktop layout, which is
   why the MSO ghost tables hold its width.
3. the ADAPTIVE dark override. The inline base is LIGHT (every element carries
   its light color + light ``bgcolor``). This block flips each semantic ``lp-*``
   class to its DARK value via ``@media (prefers-color-scheme: dark)`` with
   ``!important`` (Apple Mail iOS/macOS, and any client that honours the query),
   plus duplicate ``[data-ogsc]`` (foreground) and ``[data-ogsb]`` (background)
   rules for Outlook.com. Clients that strip the block (Gmail, Outlook desktop)
   keep the light inline base, the intended degrade, so nothing dark is
   load-bearing.
4. ``.px`` carries tabular-nums for numeric columns; the same intent is applied
   inline on score / ratio / KPI cells too because Outlook ignores the class.

The inline web-safe stacks intentionally do NOT name the @import faces, so a
client that loads the fonts still has to opt in via this block; that keeps the
inline declarations honest about what renders without it.
"""

# The dark-mode overrides, authored once and reused for the @media block and the
# two Outlook.com prefixes. Each rule names a semantic class and the DARK value
# from ``_DARK_PALETTE`` in _table.py. Backgrounds set ``background-color`` (and
# the @media variant also wins over the inline base via!important); foregrounds
# set ``color``; borders set ``border-color``. Kept in sync with the light base
# by role name.
_DARK_BG = {
    "lp-bg": "#1b1714",  # page / section canvas
    "lp-sf": "#262019",  # lifted panel
    "lp-cd": "#15110e",  # deepest well
    "lp-bd": "#1c1611",  # dark header band (same in both, declared for ogsb)
    # The hero "Trend of the day" band paints white text on this fill. The old
    # #5e87f0 gave white-on-band only 3.39:1, below WCAG AA 4.5:1. #3a63c4 lifts
    # white-on-band to 5.58:1. This is the band FILL only; the text-accent token
    # lp-accent in _DARK_FG keeps the lighter #5e87f0 because the darker blue as
    # accent TEXT on the dark canvas #1b1714 drops to 3.19:1 (below AA), where the
    # lighter blue is 5.26:1.
    "lp-ab": "#3a63c4",  # the hero accent band fill
    "lp-bs": "#222a3d",  # the move-this-week wash
    "lp-pn": "#5e87f0",  # filled confidence pip
    "lp-pf": "#37322c",  # empty confidence pip
    # The chip-line fill is the heatmap bar TRACK (the unfilled gutter behind the
    # vermillion fill). It carries a light bgcolor inline; this entry inverts the
    # track to the midnight chip fill so it never renders light-on-dark.
    "lp-cl": "#2b2622",  # chip-line / heatmap bar track
    # The tone-split segment fills (positive / neutral / negative). These are the
    # BACKGROUND-only variants of the tone classes: the text classes (lp-po /
    # lp-nu / lp-ng in _DARK_FG) must never get a background override, or every
    # tone-coloured text span paints green-on-green in dark mode (the 10 Jul
    # "random highlight" defect). Values match the dark pos / neu / neg tones.
    "lp-pob": "#56c98a",  # tone-split positive segment fill
    "lp-nub": "#9c958a",  # tone-split neutral segment fill
    "lp-ngb": "#e2654a",  # tone-split negative segment fill
}
_DARK_FG = {
    "lp-ink": "#f4f1ea",
    "lp-i2": "#c8c2b7",
    "lp-mu": "#9c958a",
    # lp-ft carries faint normal-size text on the dark canvas #1b1714. The old
    # #776f66 was 3.6:1, below WCAG AA 4.5:1; #8d857a lifts it to 4.89:1.
    "lp-ft": "#8d857a",
    # lp-accent is text-accent, kept at the lighter blue so accent text on the
    # dark canvas #1b1714 stays above AA (5.26:1). The hero band FILL (lp-ab in
    # _DARK_BG) uses the darker #3a63c4 so white-on-band clears AA; the two roles
    # diverge on purpose.
    "lp-accent": "#5e87f0",
    "lp-po": "#56c98a",
    "lp-nu": "#9c958a",
    "lp-ng": "#e2654a",
    "lp-co": "#6aa3d8",
    "lp-bu": "#dfb44a",
}
_DARK_RULE = {
    "lp-rl": "#37322c",
    "lp-r2": "#46403a",
    "lp-ch": "#2b2622",
}

# The inbox CTA fill (lp-cta) is deliberately ABSENT from every dark map. The CTA
# is the one accent surface that must NOT flip in dark mode: leaving lp-cta out of
# _DARK_BG keeps its fill the light accent #2f5fd0, where white text clears WCAG AA
# (5.72:1). Were it to flip to the dark accent #5e87f0 (as the .lp-ab hero band
# does), white text would drop to 3.39:1, below AA. Do not add lp-cta to a map.


def _block(selector_prefix: str, important: str) -> str:
    """Build one override block (bg + fg + rule rules) for a selector prefix.

    ``selector_prefix`` is "" for the @media block (bare ``.lp-...``) or
    ``[data-ogsc] `` / ``[data-ogsb] `` for the Outlook.com prefixes.
    ``important`` is "!important" for @media (must beat the inline base) and ""
    for the Outlook.com rules (the prefix already wins specificity).
    """
    parts: list[str] = []
    for klass, val in _DARK_BG.items():
        parts.append(f"{selector_prefix}.{klass}{{background-color:{val}{important};}}")
    for klass, val in _DARK_FG.items():
        parts.append(f"{selector_prefix}.{klass}{{color:{val}{important};}}")
    for klass, val in _DARK_RULE.items():
        parts.append(f"{selector_prefix}.{klass}{{border-color:{val}{important};}}")
    return "".join(parts)


# Outlook.com strips the <style> media query but honours [data-ogsc] (foreground)
# and [data-ogsb] (background); it injects those attributes on the body in dark
# mode. Build a background-only block for ogsb and a foreground+rule block for
# ogsc so the two halves land on the right Outlook.com hook.
def _ogsc_block() -> str:
    parts: list[str] = []
    for klass, val in _DARK_FG.items():
        parts.append(f"[data-ogsc] .{klass}{{color:{val}!important;}}")
    for klass, val in _DARK_RULE.items():
        parts.append(f"[data-ogsc] .{klass}{{border-color:{val}!important;}}")
    return "".join(parts)


def _ogsb_block() -> str:
    return "".join(
        f"[data-ogsb] .{klass}{{background-color:{val}!important;}}"
        for klass, val in _DARK_BG.items()
    )


_DARK_OVERRIDES = (
    "@media (prefers-color-scheme: dark){"
    + _block("", "!important")
    + "}"
    + _ogsc_block()
    + _ogsb_block()
)

STYLE = (
    "<style>"
    "@import url('https://fonts.googleapis.com/css2?family=Newsreader:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Hanken+Grotesk:wght@400;500;600;700&display=swap');"
    ".px{font-variant-numeric:tabular-nums;}"
    "@media only screen and (max-width:600px){"
    ".lp-container{width:100%!important;max-width:100%!important;}"
    ".pulse-inner{width:100%!important;max-width:100%!important;}"
    ".lp-pd{padding-left:16px!important;padding-right:16px!important;}"
    ".lp-ps{padding-left:14px!important;padding-right:14px!important;}"
    ".lp-kpi{display:inline-block!important;width:50%!important;max-width:50%!important;}"
    ".lp-stack{display:block!important;width:100%!important;max-width:100%!important;text-align:left!important;}"
    ".lp-h1{font-size:31px!important;line-height:1.04!important;}"
    ".lp-body{font-size:16px!important;}"
    ".lp-hide-sm{display:none!important;max-height:0!important;overflow:hidden!important;mso-hide:all!important;}"
    ".lp-tp{padding-top:13px!important;padding-bottom:13px!important;}"
    ".lp-bn{font-size:18px!important;}"
    "}"
    "@media only screen and (max-width:380px){"
    ".lp-kpi{width:100%!important;max-width:100%!important;}"
    "}" + _DARK_OVERRIDES + "</style>"
)
