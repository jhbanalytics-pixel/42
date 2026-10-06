"""Pure materialization helpers for general cultural question claims."""

import math
from collections.abc import Mapping, Sequence


def _declared_reading_ids(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError("declared reading ids are invalid")
    reading_ids = tuple(value)
    if any(not isinstance(reading_id, str) or not reading_id.strip() for reading_id in reading_ids):
        raise ValueError("declared reading ids are invalid")
    if len(reading_ids) != len(set(reading_ids)):
        raise ValueError("declared reading ids are duplicate")
    return reading_ids


def _format_reading(reading_id: str, reading: object) -> str:
    if not isinstance(reading, Mapping):
        raise ValueError("unknown reading")
    if reading.get("reading_id") != reading_id:
        raise ValueError("reading identity is invalid")
    unit = reading.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("reading unit is invalid")

    if "value" not in reading:
        raise ValueError("reading value is missing")
    value = reading["value"]
    if value is None:
        formatted = "Unmeasured"
    elif isinstance(value, bool):
        formatted = "true" if value else "false"
    elif isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        formatted = str(value)
    elif isinstance(value, str):
        formatted = value
    else:
        raise ValueError("reading value is invalid")
    return f"{formatted} {unit}"


def materialize_claim_text(
    segments: object,
    readings_by_id: Mapping[str, Mapping[str, object]],
    *,
    reading_ids: Sequence[str],
) -> str:
    declared = _declared_reading_ids(reading_ids)
    declared_set = set(declared)
    if not isinstance(readings_by_id, Mapping):
        raise ValueError("reading mapping is invalid")
    if not isinstance(segments, list):
        raise ValueError("claim segment list is invalid")

    output = []
    used = set()
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ValueError("claim segment is invalid")
        kind = segment.get("kind")
        if not isinstance(kind, str) or kind not in {"text", "reading"}:
            raise ValueError("claim segment kind is invalid")
        expected_fields = {"kind", "text"} if kind == "text" else {"kind", "reading_id"}
        if set(segment) != expected_fields:
            raise ValueError("claim segment fields are invalid")
        if kind == "text":
            text = segment["text"]
            if not isinstance(text, str):
                raise ValueError("claim segment text is invalid")
            output.append(text)
            continue

        reading_id = segment["reading_id"]
        if not isinstance(reading_id, str) or not reading_id.strip():
            raise ValueError("claim segment reading identity is invalid")
        if reading_id in used:
            raise ValueError("duplicate reading reference")
        if reading_id not in declared_set:
            raise ValueError("undeclared reading reference")
        if reading_id not in readings_by_id:
            raise ValueError("unknown reading reference")
        output.append(_format_reading(reading_id, readings_by_id[reading_id]))
        used.add(reading_id)

    if used != declared_set:
        raise ValueError("unused declared reading reference")
    return "".join(output)


__all__ = ["materialize_claim_text"]
