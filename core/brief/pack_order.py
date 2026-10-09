"""Pack ordering rules (C4 v2 section 19), kept in one module so a change to the section is one edit.

The ordering itself is the evidence statement in sql/evidence.sql: members of the market's own cluster first,
a creator cap that keeps the member, an outlet cap, and the post id as the last key of every ORDER BY. This module
holds what the statement is given and what is read back from it:

    OUTLET_CAP         the most outlet posts in a pack; 12 changes nothing (W8-DEC-05c-ii is not approved)
    FLOORS             the floors a card needs, pinned here to the numbers job._gate checks
    outlet_keys(path)  the authoritative outlet registry, as platform:handle keys; empty until hubs.yaml is confirmed
    read_stages(rows)  the stage counts the statement returns on every row
    hold_detail(...)   the cause and four counts of a hold the caps may have caused (block_version 1)
    hold_text(...)     the hold's wording, naming the cap when a cap caused it
"""

from pathlib import Path

import yaml

from core.api.store import creator_key
from core.brief.payload import hold_base  # noqa: F401  (re-exported: the wording holds are grouped by)
from core.brief.specificity import MIN_EVIDENCE, local_posts, showable_posts

OUTLET_CAP = 12  # rule 6: a parameter, not a decision. Three is unvalidated, so 12 (the pack size) changes nothing.
PACK_LIMIT = 12  # the statement's LIMIT
OUTLET_KINDS = ("news", "sport")  # rule 5: hubs.yaml entries of these kinds are outlets
HUBS_YAML = Path(__file__).parents[1] / "config" / "hubs.yaml"
CONFIRMED_STATUS = "confirmed"  # hubs.yaml top level `status` once colleagues have confirmed the lists
HELD_BLOCK_VERSION = 1
STAGES_VERSION = 1

# Rule 7: the floors job._gate checks (MIN_EVIDENCE showable posts, 2 local) are not changed by this module.
# tests/test_pack_holds.py pins both numbers against the gate itself.
FLOORS = {"showable": MIN_EVIDENCE, "local": 2}
FLOOR_WORDS = {"showable": "Fewer than 3 posts 42 can show", "local": "Fewer than 2 supported local posts"}

# The stages in the order a post can be lost, and the cause named when the count first falls below the floor there.
CAUSES = (("available", "evidence_absent"), ("after_creator_cap", "capped_by_creator"),
          ("after_outlet_cap", "capped_by_outlet"), ("final", "capped_by_size"))
# Posts taken out of the pack after the query (the suppression mask) are none of those: no cap did it, and the
# wording and counts must not show that they existed.
REMOVED_AFTER_RANKING = "removed_after_ranking"
STAGE_COLUMNS = {  # stage -> (all posts, showable, local) columns of the statement
    "available": ("available_posts", "available_showable", "available_local"),
    "after_creator_cap": ("after_creator_cap", "after_creator_cap_showable", "after_creator_cap_local"),
    "after_outlet_cap": ("after_outlet_cap", "after_outlet_cap_showable", "after_outlet_cap_local")}


def outlet_keys(path=HUBS_YAML):
    """The outlet registry as sorted, de-duplicated platform:handle keys (core.api.store.creator_key, the form the
    statement compares against). Empty unless the file's status is `confirmed` and the market block names who
    confirmed it: a draft list is not authority, so until then only the news platform classifies. A file that
    cannot be read as a registry gives an empty list, never a partial one."""
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(doc, dict) or doc.get("status") != CONFIRMED_STATUS or not isinstance(doc.get("markets"), dict):
        return []
    keys = set()
    for block in doc["markets"].values():
        if not isinstance(block, dict) or not str(block.get("confirmed_by") or "").strip():
            continue
        for entries in block.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("kind") in OUTLET_KINDS:
                    key = creator_key(entry.get("platform"), str(entry.get("handle") or ""))
                    if key:
                        keys.add(key)
    return sorted(keys)


def read_stages(rows):
    """The stage counts of one pack read, or None when the statement returned nothing. Every row of the statement
    repeats them (an empty pack comes back as one row with no post id), so the first row carries them."""
    if not rows:
        return None
    row = rows[0]
    out = {"version": STAGES_VERSION}
    for stage, (posts, showable, local) in STAGE_COLUMNS.items():
        out[stage] = {"posts": row[posts] or 0, "showable": row[showable] or 0, "local": row[local] or 0}
    out["available"]["members"] = row["available_members"] or 0
    return out


def final_counts(evidence, market):
    """The posts, showable posts and local posts of a pack, by the rules the floors are checked by."""
    return {"posts": len(evidence or []), "showable": len(showable_posts(evidence, market)),
            "local": len(local_posts(evidence, market))}


def hold_detail(stages, floor, evidence, market):
    """The held_reason_detail block for a floor hold. floor is "showable" or "local". The four counts are that
    kind of post at each stage: all that were available, after the creator cap, after the outlet cap, and in the
    pack the query left (stages["final"], taken before the suppression mask; the pack the gate read when absent).
    The cause is the first stage at which the count is below the floor; if none is and the gate's pack is still
    short, the posts were taken out after the ranking. A count never reads lower than one after it (a merge or a
    second read can add posts the first read did not have). stages is what evidence.build_pack filled in; a
    candidate without any reads every stage as the final pack, which can only be evidence_absent."""
    actual = final_counts(evidence, market)[floor]
    counts = {name: (stages[name][floor] if stages else actual) for name, _ in CAUSES[:-1]}
    counts["final"] = stages["final"][floor] if stages and "final" in stages else actual
    counts["final"] = max(counts["final"], actual)
    ceiling = 0
    for name, _ in reversed(CAUSES):
        ceiling = counts[name] = max(counts[name], ceiling)
    minimum = FLOORS[floor]
    cause = next((cause for name, cause in CAUSES if counts[name] < minimum), None)  # None: not a hold
    if cause is None and actual < minimum:
        cause = REMOVED_AFTER_RANKING
    return {"block_version": HELD_BLOCK_VERSION, "floor": floor, "minimum": minimum, "cause": cause, "counts": counts}


def hold_text(base, detail):
    """base, with the cap that removed the posts named when one did: "...: the outlet cap left 1 of 4"."""
    counts = detail["counts"]
    names = {"capped_by_creator": ("the creator cap", "available", "after_creator_cap"),
             "capped_by_outlet": ("the outlet cap", "after_creator_cap", "after_outlet_cap"),
             "capped_by_size": (f"the {PACK_LIMIT}-post limit", "after_outlet_cap", "final")}
    if detail["cause"] not in names:
        return base
    what, before, after = names[detail["cause"]]
    return f"{base}: {what} left {counts[after]} of {counts[before]}"
