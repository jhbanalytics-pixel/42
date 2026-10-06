from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any


def project_creator_brief(source_row: Mapping[str, Any]) -> dict[str, Any]:
    for field in ("analysis_id", "market", "query_group", "nano_banana_prompt", "lyria_prompt"):
        value = source_row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"creator brief source field invalid: {field}")
    if type(source_row.get("trend_date")) is not date:
        raise ValueError("creator brief source field invalid: trend_date")
    generated_at = source_row.get("analyzed_at")
    if not isinstance(generated_at, datetime) or generated_at.utcoffset() is None:
        raise ValueError("creator brief source field invalid: analyzed_at")
    angles = source_row.get("campaign_angles")
    if angles is not None and (
        not isinstance(angles, (list, tuple)) or any(not isinstance(value, str) for value in angles)
    ):
        raise ValueError("creator brief source field invalid: campaign_angles")
    return {
        "brief_id": source_row["analysis_id"],
        "trend_date": source_row["trend_date"],
        "cycle_id": source_row.get("cycle_id"),
        "market": source_row["market"],
        "trend_name": source_row["query_group"],
        "trend_score": source_row.get("trend_score"),
        "nano_banana_prompt": source_row["nano_banana_prompt"],
        "nano_banana_trend_trigger": None,
        "lyria_prompt": source_row["lyria_prompt"],
        "campaign_angles": None if angles is None else list(angles),
        "endorsement_disclosure": None,
        "creator_tier": None,
        "status": "generated",
        "generated_at": generated_at,
        "reviewed_at": None,
        "dispatched_at": None,
        "exported_to_sheet": False,
    }


def persist_creator_briefs(
    rows: Sequence[Mapping[str, Any]],
    *,
    row_sink: Callable[[list[dict[str, Any]]], int],
    row_reader: Callable[[tuple[str, ...]], Sequence[Mapping[str, Any]]],
) -> int:
    expected = {}
    for row in rows:
        identifier = row.get("brief_id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("creator brief identity invalid")
        if identifier in expected:
            raise ValueError("creator brief duplicate input")
        expected[identifier] = dict(row)
    if not expected:
        return 0
    identifiers = tuple(sorted(expected))

    def readback(*, complete: bool) -> dict[str, dict[str, Any]]:
        found = {}
        for row in row_reader(identifiers):
            identifier = row.get("brief_id")
            if identifier not in expected or identifier in found:
                raise ValueError("creator brief readback identity differs")
            wanted = expected[identifier]
            if any(key not in row or row[key] != value for key, value in wanted.items()):
                label = "readback" if complete else "conflict"
                raise ValueError(f"creator brief {label} differs")
            found[identifier] = dict(row)
        if complete and set(found) != set(expected):
            raise ValueError("creator brief readback incomplete")
        return found

    existing = readback(complete=False)
    fresh = [expected[identifier] for identifier in identifiers if identifier not in existing]
    if fresh:
        written = row_sink(fresh)
        if type(written) is not int or written != len(fresh):
            raise ValueError("creator brief row sink incomplete")
    readback(complete=True)
    return len(fresh)
