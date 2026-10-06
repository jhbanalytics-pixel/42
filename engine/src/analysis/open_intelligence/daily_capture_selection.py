"""The capture a closed daily cutoff composes from, chosen by the registry's own rules.

The daily compose stage takes an injected ``capture_entry_for(cutoff)`` and refuses when
it answers None. ``capture_entry_selector`` builds that callable over a read only registry
reader, bound to the source policy digest the approved grant names, the capture scope and
the staging source profile the capture stage registers under. Each call reads the
registry for the one profile the cutoff and policy name, classifies every entry through
``explain_capture_selection`` and answers the latest eligible entry of exactly that
cutoff, policy, scope and profile, or None.

This is the recovery for a stale registry readback. Newer entries that did not succeed,
have expired or are not yet available never hide an older eligible one; an entry of
another source policy is never selected, however new; and when nothing is eligible the
answer is None with the reason of every entry recorded. The callable holds no state
between calls and never writes, so the next call after a fresh eligible entry lands
answers that entry, once per call, and dispatches nothing. A registry read that is
refused (access denied, metadata unavailable, invalid authority) is raised by its own
code rather than read as an empty registry, and is recorded before it is raised.

A source policy change is not recovered here. A capture of another policy belongs to
another profile, so it is reported as ``policy_mismatch`` and the cutoff stays without a
capture until a capture under the approved policy lands; a new policy reaches this seam
only through a new grant carrying its digest, under the approval that already exists.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, time, timedelta

from src.analysis.open_intelligence.capture_registry import (
    _classify,
    explain_capture_selection,
    read_outcome_entries,
)
from src.analysis.open_intelligence.staging_source_profile import (
    NATIVE_IDENTITY_PROFILE_VERSION,
    STAGING_SOURCE_KIND,
    staging_profile_id,
)

CAPTURE_NONE_ELIGIBLE = "capture_none_eligible"
CAPTURE_CUTOFF_INVALID = "capture_cutoff_invalid"
POLICY_MISMATCH = "policy_mismatch"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _invalid(field: str) -> ValueError:
    return ValueError(f"capture_selector_invalid:{field}")


def _cutoff(value: object) -> datetime:
    """A slot cutoff: a timezone aware instant at a UTC midnight, the end of the closed day."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(CAPTURE_CUTOFF_INVALID)
    instant = value.astimezone(UTC)
    if instant.time() != time.min:
        raise ValueError(CAPTURE_CUTOFF_INVALID)
    return instant


def _reference(entry: Mapping[str, object] | None) -> dict | None:
    if entry is None:
        return None
    return {
        "profile_id": entry["profile_id"],
        "result_id": entry["result_id"],
        "operation_id": entry["operation_id"],
        "generation": entry["generation"],
        "capture_available_at": entry["capture_available_at"].isoformat(),
    }


def capture_entry_selector(
    *,
    read_registry: Callable[[str], object],
    source_policy_digest: str,
    scope: str,
    clock: Callable[[], datetime],
    record: Callable[[dict], object] | None = None,
) -> Callable[[datetime], dict | None]:
    """Return ``capture_entry_for(cutoff)`` over the registry, reading only.

    ``read_registry(profile_id)`` answers a registry read outcome in the grammar
    ``read_outcome_entries`` admits: ``{"status": "ok", "entries": [...]}`` or a refused
    status. ``record`` receives the selection explanation of every call, selected or
    refused, so the refusal compose sees as a missing capture is kept with its reasons.
    """
    if not callable(read_registry):
        raise _invalid("read_registry")
    if not isinstance(source_policy_digest, str) or _DIGEST.fullmatch(source_policy_digest) is None:
        raise _invalid("source_policy_digest")
    if not isinstance(scope, str) or not scope or scope != scope.strip():
        raise _invalid("scope")
    if not callable(clock):
        raise _invalid("clock")
    if record is not None and not callable(record):
        raise _invalid("record")

    def _explained(cutoff: datetime, profile_id: str, now: datetime) -> dict:
        entries = read_outcome_entries(read_registry(profile_id))
        diagnostics: list[dict | None] = [None] * len(entries)
        candidates: list[object] = []
        positions: list[int] = []
        for index, entry in enumerate(entries):
            checked, _reason = _classify(entry, now=now, scope=scope)
            if checked is not None and checked["policy_digest"] != source_policy_digest:
                diagnostics[index] = {
                    "index": index,
                    "profile_id": checked["profile_id"],
                    "result_id": checked["result_id"],
                    "eligible": False,
                    "reason": POLICY_MISMATCH,
                }
                continue
            candidates.append(entry)
            positions.append(index)
        explained = explain_capture_selection(
            candidates, now=now, scope=scope, profile_id=profile_id
        )
        for position, item in zip(positions, explained["diagnostics"], strict=True):
            candidate = entries[position]
            result_id = candidate.get("result_id") if isinstance(candidate, Mapping) else None
            diagnostics[position] = {
                "index": position,
                "profile_id": item["profile_id"],
                "result_id": result_id,
                "eligible": item["eligible"],
                "reason": item["reason"],
            }
        selected = explained["selected"]
        if selected is not None and (
            selected["observation_window_end"] != cutoff
            or selected["policy_digest"] != source_policy_digest
            or selected["profile_version"] != NATIVE_IDENTITY_PROFILE_VERSION
            or selected["source_kind"] != STAGING_SOURCE_KIND
        ):
            raise ValueError("capture_selection_inconsistent")
        return {"selected": selected, "diagnostics": diagnostics}

    def capture_entry_for(cutoff: datetime) -> dict | None:
        instant = _cutoff(cutoff)
        now = clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("capture_selector_clock_invalid")
        profile_id = staging_profile_id((instant - timedelta(days=1)).date(), source_policy_digest)
        explanation = {
            "cutoff_utc": instant.isoformat(),
            "evaluated_at": now.astimezone(UTC).isoformat(),
            "source_policy_digest": source_policy_digest,
            "scope": scope,
            "profile_id": profile_id,
            "profile_version": NATIVE_IDENTITY_PROFILE_VERSION,
        }
        try:
            found = _explained(instant, profile_id, now)
        except ValueError as error:
            if record is not None:
                record(
                    {
                        **explanation,
                        "outcome": "refused",
                        "code": str(error),
                        "selected": None,
                        "diagnostics": [],
                    }
                )
            raise
        selected = found["selected"]
        if record is not None:
            record(
                {
                    **explanation,
                    "outcome": "selected" if selected is not None else "refused",
                    "code": None if selected is not None else CAPTURE_NONE_ELIGIBLE,
                    "selected": _reference(selected),
                    "diagnostics": found["diagnostics"],
                }
            )
        return selected

    return capture_entry_for


__all__ = [
    "CAPTURE_CUTOFF_INVALID",
    "CAPTURE_NONE_ELIGIBLE",
    "POLICY_MISMATCH",
    "capture_entry_selector",
]
