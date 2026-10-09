"""The simpler explanations the code can find from detect's own numbers (METHOD-GAPS Gap 7, part three, shadow only).

    code_rivals(pack) -> {"record_version", "read", "found", "not_assessed", "inputs", "thresholds"} or None

For each of the seven rivals the critic is asked to name (a paid push, a coordinated or burst push, one creator or a
few carrying the count, a news outlet, a scheduled moment, a collection artefact) the code sets a boolean from the
pack's pinned detect values. found lists the rivals the numbers show, in RIVALS order; not_assessed lists those whose
input is missing, which is never counted as absent: near_duplicates is not assessed until the near duplicate step has
run for the window (the pack then has no near_dup_share). inputs gives each value used with the query id that pinned
it, and thresholds the cut offs the verdicts were judged by, so a stored record can be read after they change.
record_version is 2; a record with no version is the first form, whose near_dup_share was measured over the 7 day
posts that had a size and read 1.0 once the step wrote sizes of 2 or more only. read is the pack's rival_read status (ok,
cutoff_missing or failed), so a failed read is recorded as one rather than as a missing record. None only when the
pack has neither a detect rival value nor a read status, which is a pack built before these values existed.

This is a record, not a gate. explain.py writes it on the critic row and in the critic's audit answer beside the
critic's own ruled_out, and nothing reads it to hold, drop or change a card. Making a code-found rival hold an
explanation would be a new rule (G11) and is Albert's decision.

Thresholds are the ones core/detect/sql/state.sql already uses for share_flags: near_duplicates at near_dup_share 0.30
(line 129), burst at burst_share 0.35 and concentrated at top3_share 0.60, each only with 10 or more posts in 7 days
(lines 130 and 131). A calendar moment is the item's item_state.moment, set by the cal CTE of state.sql (lines 81 to
83: a moment dated from three days back to fourteen days ahead of the brief date). The sponsored share of 0.5 is the
figure METHOD-GAPS gives for G11; state.sql has no sponsored threshold. A news outlet leading is the pack's one
earliest post being a news post (platform news, as views.sql counts news posts). The route regime break has no
detect value at B0 (nothing in core/detect writes one), so it is always not assessed.
"""

SPONSORED_AT = 0.5
BURST_AT = 0.35
CONCENTRATED_AT = 0.60
NEAR_DUPLICATES_AT = 0.30
MIN_POSTS7 = 10
RECORD_VERSION = 2
RIVALS = ("sponsored", "burst", "concentrated", "near_duplicates", "calendar_moment", "news_leading", "regime_break")
USED = ("posts7", "burst_share", "top3_share", "near_dup_share", "sponsored_share", "moment")


def _num(got, field):
    value = got.get(field, {}).get("value")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _share(got, field, at, *, floor):
    """True or False, or None when the share, or the post count it needs, is missing."""
    share = _num(got, field)
    posts = _num(got, "posts7")
    if share is None or (floor and posts is None):
        return None
    return share >= at and (not floor or posts >= MIN_POSTS7)


def _news_leading(pack):
    from core.brief.explain import _earliest_id   # explain imports this module, so the import waits for the call

    first = _earliest_id(pack)
    if first is None:
        return None
    return any(r.get("id") == first and r.get("platform") == "news" for r in pack.get("evidence") or [])


def code_rivals(pack):
    got = {n["rival_field"]: n for n in list(pack.get("numbers") or []) + list(pack.get("pinned") or [])
           if n.get("rival_field")}
    status = pack.get("rival_read")
    if not got and status is None:
        return None
    moment = "moment" in got
    verdicts = {
        "sponsored": _share(got, "sponsored_share", SPONSORED_AT, floor=False),
        "burst": _share(got, "burst_share", BURST_AT, floor=True),
        "concentrated": _share(got, "top3_share", CONCENTRATED_AT, floor=True),
        "near_duplicates": _share(got, "near_dup_share", NEAR_DUPLICATES_AT, floor=False),
        "calendar_moment": bool(got["moment"].get("value")) if moment else None,
        "news_leading": _news_leading(pack),
        "regime_break": None,
    }
    return {
        "record_version": RECORD_VERSION,
        "read": status or "ok",
        "found": [r for r in RIVALS if verdicts[r] is True],
        "not_assessed": [r for r in RIVALS if verdicts[r] is None],
        "inputs": {f: {"value": got[f]["value"], "query_id": got[f]["query_id"]} for f in USED if f in got},
        "thresholds": {"sponsored": SPONSORED_AT, "burst": BURST_AT, "concentrated": CONCENTRATED_AT,
                       "near_duplicates": NEAR_DUPLICATES_AT, "min_posts7": MIN_POSTS7},
    }
