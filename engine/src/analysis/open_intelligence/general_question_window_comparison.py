"""Compare the earlier and later halves of one admitted question window.

A comparison question such as how the week to a date compares with the week before
plans one observation window covering both weeks and at least one history requirement.
Nothing retained answers that requirement today, so it stays missing work. Behind the
release switch below, a comparison is answered from the context read the question
already made: the admitted evidence of that one window is split by its source time into
two equal, adjacent halves, and each half is counted. No query, byte, candidate or
model call is added, and no historical evidence reader is consulted. Only the one
pinned capture the snapshot read is used, so its cutoff and source as of bound the
comparison.

The switch is read only when a snapshot is built. A snapshot sealed with a comparison
stays readable after the switch is turned off: the stored record is checked against a
recomputation from the snapshot's own admitted receipts and source binding, never
against the environment.
"""

import copy
import os
from datetime import UTC, date, datetime, time, timedelta

from src.analysis.open_intelligence.brain_contract import canonical_digest

# The deployment turns window comparisons on by setting this variable to exactly
# "enabled". Absent, empty or any other value, including "true" and "Enabled", leaves
# them off, and every snapshot is built exactly as it was before this switch existed.
HISTORY_SOURCES_SWITCH = "GENERAL_QUESTION_HISTORY_SOURCES"

WINDOW_COMPARISON_VERSION = "general_question_window_comparison_v1"
COMPARISON_METHOD = "window_comparison_receipt_count"
# The unit is the admitted evidence item: one receipt of the snapshot, one source row
# with its own content digest. Distinct sources would be the stronger unit, but origin
# authority is often not projected for these records, and a distinct source count read
# from unprojected origins would be an absent projection presented as a measurement.
COMPARISON_UNIT = "admitted_evidence_items"
COMPARATOR = "earlier_window"
# Both reading methods count selected retrieval records. Neither may be the parent of an
# interpretation, and neither travels to a follow-up question as a parent claim.
RETRIEVAL_COUNT_METHODS = frozenset({"selected_receipt_count", COMPARISON_METHOD})
# Each half must hold at least five admitted items before the two are compared. The
# counts are of a selected sample, capped at 200 items and at 24 when a supplement read
# runs, not of a population. Below five, one item more or less moves a half by twenty
# percent or more, so a difference between the halves is indistinguishable from the
# selection itself. Five is kept deliberately high for a first release; lowering it is
# a later decision with evidence, never a default.
MINIMUM_PER_WINDOW = 5
SPLIT_RULE = "equal_halves_start_inclusive_end_exclusive"
_RECORD_FIELDS_V1 = frozenset(
    {
        "contract_version",
        "state",
        "reason",
        "requirement_ids",
        "unit",
        "comparator",
        "method",
        "minimum_per_window",
        "split",
        "capture",
        "windows",
        "excluded",
        "selection",
    }
)
# The rules each record version was sealed under. A stored record is rebuilt under its
# own version's rules, so a later version that changes the minimum, the split, the unit
# or the record's own fields adds an entry here and leaves every record already sealed
# readable. A version absent from this table is refused, never read under the current
# rules. record_fields is the version's record shape and is never written into a record.
WINDOW_COMPARISON_RULES = {
    WINDOW_COMPARISON_VERSION: {
        "record_fields": _RECORD_FIELDS_V1,
        "unit": COMPARISON_UNIT,
        "comparator": COMPARATOR,
        "method": COMPARISON_METHOD,
        "minimum_per_window": MINIMUM_PER_WINDOW,
        "split": SPLIT_RULE,
    },
}
# The admission ranks and caps what it selects, and each context query stops at its LIMIT.
# When either bound, the admitted items are a ranked sample, and the halves of a ranked
# sample measure the ranking as much as the window, so they are never compared as volume.
CAPTURE_LANES = ("candidate", "enriched_index", "enriched", "raw_index", "raw")

HISTORICAL_WINDOW_UNCOVERED = "historical_window_uncovered"
INSUFFICIENT_HISTORY = "insufficient_history"
HISTORICAL_SOURCES_UNAVAILABLE = "historical_sources_unavailable"
COMPARISON_SELECTION_CAPPED = "comparison_selection_capped"
SOURCE_AFTER_CAPTURE = "source_after_capture"
OUTSIDE_WINDOW = "outside_window"
UNCOVERED_LINE_PREFIX = "Historical window uncovered: "


def history_sources_enabled():
    """True only when the switch holds exactly "enabled"."""
    return os.environ.get(HISTORY_SOURCES_SWITCH) == "enabled"


def comparison_requirement_ids(plan):
    """The history requirements a window comparison answers, or none.

    Only a comparison plan is answered this way. A history question is an analogue
    search for earlier signals and needs signal identities no context read carries, so
    it stays unresolved. A challenge requirement searches for disagreement; a count of
    each half is not that search, so a history requirement planned as a challenge is
    never marked answered by one.
    """
    if type(plan) is not dict or plan.get("intent") != "comparison":
        return []
    return sorted(
        item["requirement_id"]
        for item in plan.get("requirements", ())
        if item.get("kind") == "history" and item.get("evidence_purpose") != "challenge"
    )


def _instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("window_comparison_invalid")
    return parsed.astimezone(UTC)


def _text(value):
    return value.isoformat().replace("+00:00", "Z")


def split_window(window):
    """The earlier and later halves of a plan window, as half open instant intervals.

    The plan window runs from the start day at 00:00Z up to, but not including, the day
    after its end day at 00:00Z. The midpoint splits it into two equal, adjacent halves:
    the earlier half holds instants from its start up to but not including the
    midpoint, and the later half holds the midpoint and everything after it. With an
    even day count the midpoint is a day boundary. With an odd day count it is 12:00Z
    of the middle day, so that day is shared by instant and not dropped or assigned
    whole to one side. A window of one day has no earlier week to compare against and
    returns None.
    """
    start = datetime.combine(date.fromisoformat(window["start"]), time(), UTC)
    end = datetime.combine(date.fromisoformat(window["end"]) + timedelta(days=1), time(), UTC)
    if (end - start).days < 2:
        return None
    middle = start + (end - start) / 2
    return start, middle, end


def _source_instant(receipt):
    # The same source time the admission selected the row by: publication, else collection.
    return _instant(receipt["published_at"] or receipt["collected_at"])


def _selection_record(selection):
    """The capped and uncapped numbers of the one read, from the admission's own caps."""
    lanes = {
        key: {
            "kept": selection["lanes"][key]["kept"],
            "matching": selection["lanes"][key]["matching"],
            "capped": selection["lanes"][key]["capped"] is True,
        }
        for key in CAPTURE_LANES
    }
    capped_lanes = [key for key in CAPTURE_LANES if lanes[key]["capped"]]
    selected = selection["selection"]["kept"]
    eligible = selection["selection"]["eligible"]
    return {
        "capped": eligible > selected or bool(capped_lanes),
        "selected_items": selected,
        "eligible_items": eligible,
        "capped_lanes": capped_lanes,
        "lanes": lanes,
    }


def build_window_comparison(plan, receipts, source_binding, selection, *, version=None):
    """Compare the halves of the plan window over the snapshot's admitted receipts.

    The record names the unit, the comparator, both halves' exact bounds, each half's
    count and its receipt ids, and every admitted receipt the comparison refused. A
    receipt dated after the capture's source as of is refused and never counted. A
    window reaching past the pinned cutoff is refused whole and carries no count, so
    the part the capture never read cannot read as zero. A read whose selection or
    query limit bound is refused with its capped and uncapped numbers and no count.
    """
    version = WINDOW_COMPARISON_VERSION if version is None else version
    if version not in WINDOW_COMPARISON_RULES:
        raise ValueError("window_comparison_invalid")
    rules = WINDOW_COMPARISON_RULES[version]
    requirement_ids = comparison_requirement_ids(plan)
    if not requirement_ids:
        raise ValueError("window_comparison_invalid")
    cutoff = source_binding["cutoff_date"]
    source_as_of = source_binding["source_as_of"]
    record = {
        "contract_version": version,
        "state": "refused",
        "reason": None,
        "requirement_ids": requirement_ids,
        **{key: value for key, value in rules.items() if key != "record_fields"},
        "capture": {
            "profile_id": source_binding["profile_id"],
            "cutoff_date": cutoff,
            "source_as_of": source_as_of,
        },
        "windows": None,
        "excluded": [],
        "selection": _selection_record(selection),
    }
    window = plan["window"]
    if window["end"] > cutoff:
        record["reason"] = HISTORICAL_WINDOW_UNCOVERED
        return record
    halves = split_window(window)
    if halves is None:
        record["reason"] = HISTORICAL_SOURCES_UNAVAILABLE
        return record
    if record["selection"]["capped"]:
        record["reason"] = COMPARISON_SELECTION_CAPPED
        return record
    start, middle, end = halves
    bound = _instant(source_as_of)
    earlier, later, excluded = [], [], []
    for receipt in receipts:
        receipt_id = receipt["receipt_id"]
        stamps = [
            _instant(receipt[field])
            for field in ("published_at", "collected_at")
            if receipt[field] is not None
        ]
        if any(stamp > bound for stamp in stamps):
            excluded.append({"receipt_id": receipt_id, "reason": SOURCE_AFTER_CAPTURE})
            continue
        observed = _source_instant(receipt)
        if start <= observed < middle:
            earlier.append(receipt_id)
        elif middle <= observed < end:
            later.append(receipt_id)
        else:
            excluded.append({"receipt_id": receipt_id, "reason": OUTSIDE_WINDOW})
    record["windows"] = {
        "earlier": {
            "start": _text(start),
            "end": _text(middle),
            "count": len(earlier),
            "receipt_ids": earlier,
        },
        "later": {
            "start": _text(middle),
            "end": _text(end),
            "count": len(later),
            "receipt_ids": later,
        },
    }
    record["excluded"] = excluded
    if min(len(earlier), len(later)) < rules["minimum_per_window"]:
        record["reason"] = INSUFFICIENT_HISTORY
        return record
    record["state"] = "compared"
    return record


def validate_stored_window_comparison(record, plan, receipts, source_binding, selection):
    """Refuse a stored comparison that is not exactly the one these receipts produce.

    The record is rebuilt under the rules of the version it names, so it stays readable
    after a later version changes them; a version this table does not know is refused.
    """
    # The version is read first because it names the record shape the fields are held to.
    version = record.get("contract_version") if type(record) is dict else None
    if type(version) is not str or version not in WINDOW_COMPARISON_RULES:
        raise ValueError("window_comparison_invalid")
    if set(record) != WINDOW_COMPARISON_RULES[version]["record_fields"]:
        raise ValueError("window_comparison_invalid")
    rebuilt = build_window_comparison(plan, receipts, source_binding, selection, version=version)
    if record != rebuilt:
        raise ValueError("window_comparison_invalid")
    return copy.deepcopy(record)


def _reading_window(plan, low, high):
    return {
        "start": low.date().isoformat(),
        "end": high.date().isoformat(),
        "closed": plan["window"]["closed"],
    }


def _half_line(role, half, record):
    unit = record["unit"].replace("_", " ")
    named = (
        "earlier window, the named comparator"
        if role == "earlier"
        else "later window, compared against the earlier window"
    )
    return (
        f"Window comparison, {named}: from {half['start']} inclusive to {half['end']} "
        f"exclusive, {half['count']} {unit} counted by source time (publication, else "
        f"collection), from the pinned capture with cutoff {record['capture']['cutoff_date']}. "
        "A selected-record count, not population prevalence."
    )


def _summary_lines(record):
    unit = record["unit"].replace("_", " ")
    windows = record["windows"]
    lines = []
    if record["state"] == "compared":
        lines.append(
            f"Window comparison in {unit}: the later window {windows['later']['start']} to "
            f"{windows['later']['end']} holds {windows['later']['count']}; the earlier window "
            f"{windows['earlier']['start']} to {windows['earlier']['end']}, the comparator, "
            f"holds {windows['earlier']['count']}. Both come from one read of one pinned "
            "capture, so they share units, window rule and source."
        )
    elif record["reason"] == HISTORICAL_WINDOW_UNCOVERED:
        lines.append(
            f"Window comparison refused ({HISTORICAL_WINDOW_UNCOVERED}): the window runs past "
            f"{record['capture']['cutoff_date']}, the pinned capture cutoff. Nothing after "
            "the cutoff was read, and it is not counted as zero."
        )
    elif record["reason"] == INSUFFICIENT_HISTORY:
        lines.append(
            f"Window comparison refused ({INSUFFICIENT_HISTORY}): the earlier window holds "
            f"{windows['earlier']['count']} and the later window {windows['later']['count']} "
            f"{unit}; each needs at least {record['minimum_per_window']}."
        )
    elif record["reason"] == COMPARISON_SELECTION_CAPPED:
        selection = record["selection"]
        bound = ", ".join(selection["capped_lanes"])
        lines.append(
            f"Window comparison refused ({COMPARISON_SELECTION_CAPPED}): the read kept "
            f"{selection['selected_items']} of {selection['eligible_items']} eligible {unit}"
            + (f", and a query limit bound the {bound} read" if bound else "")
            + ". A ranked sample is never compared as volume."
        )
    else:
        lines.append(
            f"Window comparison refused ({HISTORICAL_SOURCES_UNAVAILABLE}): the window is too "
            "short to split into an earlier and a later period."
        )
    after = [row for row in record["excluded"] if row["reason"] == SOURCE_AFTER_CAPTURE]
    if after:
        lines.append(
            f"Window comparison excluded {len(after)} admitted items dated after the capture "
            f"source as of {record['capture']['source_as_of']}; they are not counted."
        )
    return lines


def apply_window_comparison(value, record, plan):
    """Fold a stored comparison into the projected snapshot, in place.

    A compared record adds one reading per half, with the unit, the half's window, its
    count and its receipts, links each counted receipt to its half's reading, and marks
    the comparison's history requirements fulfilled. A refused record leaves them
    unfulfilled and names its refusal code in the missing work. Both add the lines that
    state the comparison in words.
    """
    if record["state"] == "compared":
        requirement_ids = record["requirement_ids"]
        value["receipts"] = copy.deepcopy(value["receipts"])
        readings = []
        split = record["windows"]["earlier"]["end"]
        # With an odd day count the halves meet at noon, which the date windows cannot
        # show, so the unit the reading renders with names the split instant itself.
        units = (
            {"earlier": record["unit"], "later": record["unit"]}
            if _instant(split).time() == time()
            else {
                "earlier": f"{record['unit']} before {split}",
                "later": f"{record['unit']} from {split}",
            }
        )
        for role in ("earlier", "later"):
            half = record["windows"][role]
            reading_id = "gqrd_" + canonical_digest(
                {
                    "snapshot_id": value["snapshot_id"],
                    "method": record["method"],
                    "window_role": role,
                    "requirement_ids": requirement_ids,
                }
            )
            low = _instant(half["start"])
            high = _instant(half["end"]) - timedelta(microseconds=1)
            readings.append(
                {
                    "reading_id": reading_id,
                    "value": half["count"],
                    "unit": units[role],
                    "window": _reading_window(plan, low, high),
                    "method": record["method"],
                    "denominator": None,
                    "source_receipt_ids": list(half["receipt_ids"]),
                    "limitations": [_half_line(role, half, record)],
                }
            )
            members = set(half["receipt_ids"])
            for receipt in value["receipts"]:
                if receipt["receipt_id"] in members:
                    receipt["reading_ids"] = [*receipt["reading_ids"], reading_id]
        value["readings"] = [*value["readings"], *readings]
        value["fulfilled_requirement_ids"] = sorted(
            {*value["fulfilled_requirement_ids"], *requirement_ids}
        )
        value["missing_work"] = [
            item for item in value["missing_work"] if item not in set(requirement_ids)
        ]
    elif record["reason"] not in value["missing_work"]:
        value["missing_work"] = [*value["missing_work"], record["reason"]]
    value["limitations"] = list(dict.fromkeys([*value["limitations"], *_summary_lines(record)]))
    return value


def uncovered_window_line(plan, cutoffs):
    """The line naming the latest pinned cutoff a comparison window runs past."""
    latest = max(cutoffs) if cutoffs else None
    window = plan["window"]
    named = (
        f"{latest}, the latest pinned capture cutoff"
        if latest is not None
        else "the latest pinned capture cutoff, and no capture is pinned"
    )
    return (
        f"{UNCOVERED_LINE_PREFIX}the comparison window {window['start']} to {window['end']} "
        f"runs past {named}. Nothing after the cutoff was read, and it is not counted as zero."
    )


def uncovered_window_missing_work(refusal):
    """The code and cutoff line a builder refusal carries for an uncovered comparison."""
    missing = refusal.get("missing_work") if type(refusal) is dict else None
    if type(missing) is not list or HISTORICAL_WINDOW_UNCOVERED not in missing:
        return []
    lines = [
        item for item in missing if type(item) is str and item.startswith(UNCOVERED_LINE_PREFIX)
    ]
    return [HISTORICAL_WINDOW_UNCOVERED, *lines[:1]]
