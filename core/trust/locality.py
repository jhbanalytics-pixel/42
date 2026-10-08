"""Locality for the Local label (W8-DEC-17).

G6's removal is not here and is not changed: item_state sets geo_status not_local when an item has 8 or more
located posts and a local_share under 0.6 (core/detect/sql/state.sql), and that item is out of the market's list.
What this adds is the label. An item item_state calls local is confirmed Local only when the lower end of the 95%
Wilson interval of local_share is 0.5 or more; otherwise the card carries Market unconfirmed. The interval is
computed from the counts the row holds, local posts and located posts, so a share that is not a whole number of
posts cannot be confirmed.
"""

import math

Z95 = 1.959963984540054  # the 97.5th percentile of the standard normal: a two-sided 95% interval
MIN_LOCAL_LOWER = 0.5
REMOVE_BELOW = 0.6  # G6's share, kept as part of Local so a label never outruns the removal rule


def wilson_lower(local, known):
    """The lower end of the 95% Wilson score interval for local of known, or 0.0 with no sample."""
    if known <= 0:
        return 0.0
    p = local / known
    z2 = Z95 * Z95
    centre = p + z2 / (2 * known)
    spread = Z95 * math.sqrt(p * (1 - p) / known + z2 / (4 * known * known))
    return max(0.0, (centre - spread) / (1 + z2 / known))


def is_local(local, known):
    """True when local of known located posts is at least G6's share and its interval's lower end is 0.5 or more."""
    return known > 0 and local / known >= REMOVE_BELOW and wilson_lower(local, known) >= MIN_LOCAL_LOWER


def _count(value):
    return type(value) is int and value > 0


def local_posts(local_share, known):
    """The number of local posts a row's share and located count give back, or None when they give no whole number
    (a missing or non-finite share, a share outside 0 to 1, a located count that is not a positive integer)."""
    if not _count(known) or type(local_share) not in (int, float) or not math.isfinite(local_share):
        return None
    if not 0 <= local_share <= 1:
        return None
    local = local_share * known
    nearest = round(local)
    return nearest if abs(local - nearest) < 1e-6 else None


def geo_status(row):
    """The row's geo_status as the card should read it. Only a row item_state calls local can change, to
    market_unconfirmed, when its counts do not confirm Local. A row with no counts at all is left as it is."""
    status = row.get("geo_status")
    if status != "local":
        return status
    share, known = row.get("local_share"), row.get("geo_known_posts7")
    if share is None and known is None:
        return status
    local = local_posts(share, known)
    return "local" if local is not None and is_local(local, known) else "market_unconfirmed"
