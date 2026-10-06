"""Paired strategist trial gate (E02).

The kernel decides whether three participants completed both flows and
whether the replacement flow improved on the old one. The wrapper validates
each trial row before the kernel sees it.
"""

import math
from statistics import median

FLOWS = frozenset({"old", "replacement"})
COUNT_FIELDS = ("unsupported_selections", "receipt_opens", "clarification_count")
REQUIRED_FIELDS = ("participant", "flow", "complete", "elapsed_seconds", *COUNT_FIELDS)


def paired_trial_result(rows: list[dict]) -> dict:
    pairs = {}
    for row in rows:
        key = (row["participant"], row["flow"])
        if key in pairs:
            raise ValueError("duplicate_trial")
        pairs[key] = row
    people = sorted({row["participant"] for row in rows})
    complete = len(people) == 3 and all(
        (person, flow) in pairs and pairs[(person, flow)]["complete"]
        for person in people
        for flow in ("old", "replacement")
    )
    if not complete:
        return {"trial_complete": False, "improved": False}
    old = [pairs[(person, "old")] for person in people]
    new = [pairs[(person, "replacement")] for person in people]
    improved = median(row["elapsed_seconds"] for row in new) < median(
        row["elapsed_seconds"] for row in old
    ) and sum(row["unsupported_selections"] for row in new) <= sum(
        row["unsupported_selections"] for row in old
    )
    return {"trial_complete": True, "improved": improved}


def validated_trial_result(rows: list[dict]) -> dict:
    """Validate every trial row, then run the kernel.

    Flow must be old or replacement, completion must be a real boolean,
    elapsed time must be a finite nonnegative number and the three counters
    must be nonnegative integers.
    """
    if not isinstance(rows, list):
        raise ValueError("rows_list_required")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("trial_row_invalid")
        if any(field not in row for field in REQUIRED_FIELDS):
            raise ValueError("trial_field_missing")
        if not isinstance(row["participant"], str) or not row["participant"]:
            raise ValueError("participant_invalid")
        if not isinstance(row["flow"], str) or row["flow"] not in FLOWS:
            raise ValueError("flow_invalid")
        if type(row["complete"]) is not bool:
            raise ValueError("complete_bool_required")
        elapsed = row["elapsed_seconds"]
        if (
            type(elapsed) not in (int, float)
            or not math.isfinite(elapsed)
            or elapsed < 0
        ):
            raise ValueError("elapsed_seconds_invalid")
        for field in COUNT_FIELDS:
            if type(row[field]) is not int or row[field] < 0:
                raise ValueError(f"{field}_invalid")
    return paired_trial_result(rows)
