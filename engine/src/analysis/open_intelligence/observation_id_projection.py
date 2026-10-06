"""Project the obs1 observation id over snapshot or raw rows, at read time.

Ask admission and the snapshot identify an observation by (market, platform, id), and the
producer gives every raw row a fresh uuid4, so one post collected in two runs reads as two
unknown observations. This projection derives the versioned obs1 id
(src/ingestion/observation_id.py) from columns raw_content already holds, so nothing is
written by the producer, migrated or granted.

It is an added identity, not a collapse rule. Every input row keeps its own entry, keyed
by its market and id; rows that share an obs1 are listed as members of one observation
beside the existing key and are never merged, dropped or refused here. Admission's
refusal of a byte-different double write happens before any projection and does not
change.

The projection is a pure function over row mappings with the raw_content column names
(id, market, source, platform, native_id, url, title, text, author_handle, author_name,
published_at). Callers pass the rows themselves, not a digest or an excerpt of them: an
excerpt is not the text the kernel keys on. ``key_columns`` records which key columns the
rows carried, so a snapshot captured without native_id says it was keyed by URL or text.
A record written before the projection existed carries no field, and
``stored_observation_id_projection`` reads it as not recorded, which is neither a measured
zero nor a refusal.

Later hooks append ``observation_id_projection`` as the last key of a new admission or
sidecar version; ``PROJECTION_FIELD`` names it.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping

from src.ingestion.observation_id import (
    OBSERVATION_ID_SCHEME,
    content_key,
    observation_id,
    observation_id_scheme,
)

OBSERVATION_ID_PROJECTION_VERSION = "observation_id_projection_v1"
PROJECTION_FIELD = "observation_id_projection"
KEY_COLUMNS = (
    "source",
    "platform",
    "market",
    "native_id",
    "url",
    "title",
    "text",
    "author_handle",
    "author_name",
    "published_at",
)
PROJECTION_FIELDS = (
    "version",
    "scheme",
    "state",
    "key_columns",
    "rows",
    "observations",
    "units",
)
_ROW_FIELDS = ("market", "id", "observation_id", "key_kind")
_KEY_KINDS = ("native", "url", "text")
_UNIT_FIELDS = (
    "collected_records",
    "derived_records",
    "underived_records",
    "distinct_row_ids",
    "distinct_observation_ids",
    "recollected_observation_ids",
)
# What a record written before this projection existed reports for it.
OBSERVATION_ID_PROJECTION_NOT_RECORDED = {
    "version": None,
    "scheme": None,
    "state": "not_recorded",
}


def _invalid() -> None:
    raise ValueError("observation_id_projection_invalid")


def _text(value: object) -> str:
    if type(value) is not str or not value:
        _invalid()
    return value


def _row_entry(row: object) -> dict:
    if not isinstance(row, Mapping):
        _invalid()
    key = content_key(row)
    return {
        "market": _text(row.get("market")),
        "id": _text(row.get("id")),
        "observation_id": observation_id(row),
        "key_kind": None if key is None else key[0],
    }


def _order(entry: dict) -> tuple:
    return (entry["market"], entry["id"], entry["observation_id"] or "", entry["key_kind"] or "")


def _summarise(entries: list[dict], key_columns: list[str]) -> dict:
    members: dict[str, set] = {}
    for entry in entries:
        if entry["observation_id"] is not None:
            members.setdefault(entry["observation_id"], set()).add((entry["market"], entry["id"]))
    observations = [
        {"observation_id": value, "members": [list(member) for member in sorted(members[value])]}
        for value in sorted(members)
    ]
    derived = sum(1 for entry in entries if entry["observation_id"] is not None)
    return {
        "version": OBSERVATION_ID_PROJECTION_VERSION,
        "scheme": OBSERVATION_ID_SCHEME,
        "state": "projected" if derived else "not_projected",
        "key_columns": key_columns,
        "rows": entries,
        "observations": observations,
        "units": {
            "collected_records": len(entries),
            "derived_records": derived,
            "underived_records": len(entries) - derived,
            "distinct_row_ids": len({(entry["market"], entry["id"]) for entry in entries}),
            "distinct_observation_ids": len(observations),
            "recollected_observation_ids": sum(
                1 for item in observations if len(item["members"]) > 1
            ),
        },
    }


def observation_id_projection(rows: object) -> dict:
    """The obs1 identity field set of these rows: one entry per row, nothing collapsed."""
    if type(rows) not in (list, tuple):
        _invalid()
    entries = sorted((_row_entry(row) for row in rows), key=_order)
    present = {column for row in rows for column in KEY_COLUMNS if column in row}
    return _summarise(entries, [column for column in KEY_COLUMNS if column in present])


def validate_observation_id_projection(value: object, *, rows: object = None) -> dict:
    """Validate a stored projection by shape and consistency; rebuild it when rows are given."""
    if type(value) is not dict or set(value) != set(PROJECTION_FIELDS):
        _invalid()
    if (
        value["version"] != OBSERVATION_ID_PROJECTION_VERSION
        or value["scheme"] != OBSERVATION_ID_SCHEME
    ):
        _invalid()
    columns = value["key_columns"]
    if type(columns) is not list or columns != [c for c in KEY_COLUMNS if c in columns]:
        _invalid()
    entries = value["rows"]
    if type(entries) is not list:
        _invalid()
    for entry in entries:
        if type(entry) is not dict or set(entry) != set(_ROW_FIELDS):
            _invalid()
        _text(entry["market"])
        _text(entry["id"])
        derived, kind = entry["observation_id"], entry["key_kind"]
        if derived is None:
            if kind is not None:
                _invalid()
        elif observation_id_scheme(derived) != OBSERVATION_ID_SCHEME or kind not in _KEY_KINDS:
            _invalid()
    if entries != sorted(entries, key=_order):
        _invalid()
    if value != _summarise(copy.deepcopy(entries), list(columns)):
        _invalid()
    if rows is not None and observation_id_projection(rows) != value:
        _invalid()
    return value


def stored_observation_id_projection(record: object) -> dict:
    """The projection a stored record carries, or not recorded for one written before it."""
    if not isinstance(record, Mapping):
        _invalid()
    if PROJECTION_FIELD not in record:
        return copy.deepcopy(OBSERVATION_ID_PROJECTION_NOT_RECORDED)
    return validate_observation_id_projection(record[PROJECTION_FIELD])
