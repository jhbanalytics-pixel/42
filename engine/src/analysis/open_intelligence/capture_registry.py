"""Daily capture admission and latest-capture selection.

``admit_capture`` is the admission kernel. It compares a candidate entry with
the values verified from the native terminal result and object generation
readback, and with any existing admission for the same profile. Field grammar
validation is a separate wrapper so the kernel stays a plain equality check.
Selection never treats a refused registry read as an empty registry: a read
outcome is converted to entries first, and an access denial, unavailable
metadata or invalid authority is raised as its own refusal.

Registry writes go through ``create_capture_entry``, a create only seam: the
writer exposes conditional creation and an exact read back, never a replace.
An ambiguous acknowledgement is resolved by reading the exact object back
before any retry, so a write that landed is recognised rather than repeated.

``admit_capture_coverage`` is the registration precondition: an entry names the source
lanes and markets a capture stands for, and only the native result can say which of them
the capture reached, so the two are compared before anything is written.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

COMPLETION_STATES = frozenset({"pending", "unknown", "succeeded", "failed", "partial", "cancelled"})
CAPTURE_MARKETS = ("za", "ng", "ke")
READ_STATUSES = frozenset({"ok", "metadata_unavailable", "access_denied", "invalid_authority"})
WRITE_ACKNOWLEDGEMENTS = frozenset({"created", "exists", "ambiguous"})
WRITE_ATTEMPTS = 2
CAPTURE_COVERAGE_FIELDS = frozenset({"market_scope", "creation_records"})
CAPTURE_CREATION_RECORD_FIELDS = frozenset(
    {"lane", "destination", "job_id", "native_job_digest", "state"}
)
CAPTURE_CREATION_ADMITTED = "succeeded"
# The producer names every creation job for the manifest its execution ran under and
# the lane it created.
CAPTURE_JOB_PREFIX = "oi_v3_snapshot_"
PROFILE_BINDING_FIELDS = (
    "profile_id",
    "profile_version",
    "source_kind",
    "source_tables",
    "snapshot_tables",
    "scope",
    "markets",
    "observation_window_end",
    "schema_digest",
    "policy_digest",
    "source_digest",
)
_IDENTIFIER_FIELDS = (
    "profile_id",
    "profile_version",
    "operation_id",
    "result_id",
    "source_kind",
    "scope",
)
_TABLE_SET_FIELDS = ("source_tables", "snapshot_tables")
_DIGEST_FIELDS = (
    "content_digest",
    "schema_digest",
    "policy_digest",
    "source_digest",
    "image_digest",
)
_INSTANT_FIELDS = (
    "observation_window_end",
    "collection_started_at",
    "collection_completed_at",
    "snapshot_as_of",
    "capture_available_at",
)
REQUIRED_FIELDS = (
    *_IDENTIFIER_FIELDS,
    *_TABLE_SET_FIELDS,
    "markets",
    *_INSTANT_FIELDS,
    *_DIGEST_FIELDS,
    "generation",
    "completion_state",
)
OPTIONAL_FIELDS = ("expires_at",)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION = re.compile(r"[1-9][0-9]*\Z")
_TABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROJECT = re.compile(r"[a-z][a-z0-9-]*[a-z0-9]\Z")


def admit_capture(entry, verified, *, existing=None):
    if entry != verified or entry.get("completion_state") != "succeeded":
        raise ValueError("authority_mismatch")
    if existing is not None and existing != entry:
        raise ValueError("profile_conflict")
    return dict(entry)


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"capture_field_invalid:{field}")
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"capture_field_invalid:{field}")
    return value


def _table_set(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, str | bytes | Mapping) or not isinstance(value, Iterable):
        raise ValueError(f"capture_field_invalid:{field}")
    tables = tuple(value)
    if (
        not tables
        or len(tables) != len(set(tables))
        or any(not isinstance(table, str) or _TABLE.fullmatch(table) is None for table in tables)
    ):
        raise ValueError(f"capture_field_invalid:{field}")
    return tuple(sorted(tables))


def validate_capture_entry(entry: object) -> dict:
    """Validate the capture entry grammar and return a normalized copy."""
    if not isinstance(entry, Mapping):
        raise ValueError("capture_entry_invalid")
    for field in REQUIRED_FIELDS:
        if field not in entry:
            raise ValueError(f"capture_field_missing:{field}")
    unknown = set(entry) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
    if unknown:
        raise ValueError(f"capture_field_unknown:{sorted(unknown)[0]}")
    normalized: dict = {}
    for field in _IDENTIFIER_FIELDS:
        normalized[field] = _identifier(entry[field], field)
    for field in _TABLE_SET_FIELDS:
        normalized[field] = _table_set(entry[field], field)
    markets = _table_set(entry["markets"], "markets")
    if any(market not in CAPTURE_MARKETS for market in markets):
        raise ValueError("capture_field_invalid:markets")
    normalized["markets"] = markets
    for field in _INSTANT_FIELDS:
        normalized[field] = _instant(entry[field], field)
    for field in _DIGEST_FIELDS:
        value = entry[field]
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            raise ValueError(f"capture_field_invalid:{field}")
        normalized[field] = value
    generation = entry["generation"]
    if not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None:
        raise ValueError("capture_field_invalid:generation")
    normalized["generation"] = generation
    state = entry["completion_state"]
    if state not in COMPLETION_STATES:
        raise ValueError("capture_field_invalid:completion_state")
    normalized["completion_state"] = state
    expires_at = entry.get("expires_at")
    normalized["expires_at"] = None if expires_at is None else _instant(expires_at, "expires_at")
    ordered = [normalized[field] for field in _INSTANT_FIELDS]
    if any(earlier > later for earlier, later in pairwise(ordered)):
        raise ValueError("capture_instants_unordered")
    return normalized


def admit_validated_capture(entry, verified, *, existing=None) -> dict:
    """Validate every side of an admission before the equality kernel runs."""
    checked_existing = None if existing is None else validate_capture_entry(existing)
    return admit_capture(
        validate_capture_entry(entry), validate_capture_entry(verified), existing=checked_existing
    )


def _coverage_sequence(value: object) -> tuple:
    if isinstance(value, str | bytes | Mapping) or not isinstance(value, Iterable):
        raise ValueError("capture_coverage_invalid")
    return tuple(value)


def _capture_estate() -> tuple[str, str]:
    """The project and dataset every daily capture writes into, from the module that owns them."""
    from src.analysis.open_intelligence.production_snapshot_tables import (
        DESTINATION_DATASET,
        PROJECT,
    )

    return PROJECT, DESTINATION_DATASET


def _observation_day(checked: Mapping[str, object]) -> date:
    """The observation day this entry closes, as the plan that names its tables reads it."""
    window_end = checked["observation_window_end"]
    return (window_end.astimezone(UTC) - timedelta(days=1)).date()


def _capture_table(day: date, lane: str) -> str:
    """The name the capture the entry stands for gives this lane's table of that day."""
    from src.analysis.open_intelligence.production_snapshot_tables import (
        snapshot_destination_table,
    )

    return snapshot_destination_table(day, lane)


def _creation_job(value: object, lane: str) -> str:
    """The native job identity of one creation record, read whole rather than by its tail.

    The capture producer names a creation job for the execution manifest it ran under and
    the lane it created: ``oi_v3_snapshot_<manifest digest>_<lane>``. Both parts are read,
    so a job of another lane, a job whose name merely mentions this lane, and a job
    carrying no manifest digest where the producer puts one are each refused.
    """
    suffix = f"_{lane}"
    if (
        not isinstance(value, str)
        or not value.startswith(CAPTURE_JOB_PREFIX)
        or not value.endswith(suffix)
        or _DIGEST.fullmatch(value[len(CAPTURE_JOB_PREFIX) : -len(suffix)]) is None
    ):
        raise ValueError(f"capture_job_unobserved:{lane}")
    return value


def _creation_destination(value: object, lane: str, table: str) -> tuple[str, str]:
    """The project and dataset of one creation record, whose table must be the given one.

    A destination is the whole name of the table a native job wrote, not its last dotted
    segment: the project and the dataset travel with it, so a table of the same name in
    another estate is a different table. The table must be the name the plan the capture
    producer runs gives this record's own lane on this entry's own observation day, so a
    swapped lane pairing, a record of another day's capture and a table of a naming that
    nothing produces are each refused rather than admitted for their shape.
    """
    if not isinstance(value, str):
        raise ValueError("capture_coverage_invalid")
    parts = value.split(".")
    if len(parts) != 3:
        raise ValueError("capture_coverage_invalid")
    project, dataset, found = parts
    if (
        _PROJECT.fullmatch(project) is None
        or _TABLE.fullmatch(dataset) is None
        or _TABLE.fullmatch(found) is None
    ):
        raise ValueError("capture_coverage_invalid")
    if found != table:
        raise ValueError(f"capture_snapshot_tables_unadmitted:{lane}")
    return project, dataset


def admit_capture_coverage(entry: object, coverage: object, *, declared_datasets=None) -> None:
    """Refuse to register a capture whose native result did not admit its whole table set.

    An admitted entry names the source lanes and the markets the capture stands for, but it
    cannot tell on its own what the native capture reached. ``coverage`` carries the facts
    only the native result holds: the market scope the capture ran for and the creation
    record of every snapshot table it attempted. The entry is admitted only when the market
    scope is exactly the entry's markets and every one of its lanes has its own succeeded
    creation record, carrying a native job named for that lane and the manifest its
    execution ran under, at the whole destination the capture producer's own plan gives
    that lane on this entry's own observation day.

    Every destination is compared against the name the module that owns the capture's
    naming produces, not against a shape: the table is that lane's table of that day under
    the producer's own prefix, and the project and dataset are the one estate that producer
    writes into. The tables so admitted must also be exactly the ``snapshot_tables`` the
    entry declares, so the field the registry stores names the tables the capture created
    rather than the tables of a plan that cannot run. ``declared_datasets``, when the
    caller holds the approved capture origin, is the set of datasets that origin binds the
    capture operation to; a producer writing outside them is refused even if it writes
    consistently, so a drift between the producer and the approval cannot register.

    A missing record, an unfinished one, a repeated lane, a record whose native job was
    never observed and a record naming another lane's table, another day's table, a table
    the entry does not declare or another estate are each refused by name, and an empty
    record list is a capture that admitted nothing rather than one that admitted
    everything.

    The two ways a coverage can be missing are named apart. A result object written before
    the coverage travelled with the readback carries neither field and is refused as
    unrecorded; a result that carries both fields and reports nothing in them is a runner
    that answered no coverage today and is refused as unreported.

    Two things this cannot establish. The native job digest is read as a digest and is not
    compared with the job it was taken from: that comparison is against the stored creation
    evidence, which this seam never opens. And the market scope compared is the entry's own,
    while the profile that builds a daily entry always claims all three bootstrap markets,
    so a capture legitimately run for a market subset is refused here until that profile
    reads its markets from the run it closes.
    """
    checked = validate_capture_entry(entry)
    if not isinstance(coverage, Mapping):
        raise ValueError("capture_coverage_invalid")
    if not coverage:
        raise ValueError("capture_coverage_unrecorded")
    if set(coverage) != CAPTURE_COVERAGE_FIELDS:
        raise ValueError("capture_coverage_invalid")
    if all(coverage[field] is None for field in CAPTURE_COVERAGE_FIELDS):
        raise ValueError("capture_coverage_unreported")
    markets = _coverage_sequence(coverage["market_scope"])
    if tuple(sorted(markets)) != checked["markets"]:
        raise ValueError("capture_markets_unadmitted")
    records = coverage["creation_records"]
    if not isinstance(records, list):
        raise ValueError("capture_coverage_invalid")
    day = _observation_day(checked)
    lanes: set[str] = set()
    tables: set[str] = set()
    estates: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, Mapping) or set(record) != CAPTURE_CREATION_RECORD_FIELDS:
            raise ValueError("capture_coverage_invalid")
        lane, state = record["lane"], record["state"]
        if not isinstance(lane, str) or not lane or not isinstance(state, str):
            raise ValueError("capture_coverage_invalid")
        if lane in lanes:
            raise ValueError("capture_coverage_lane_repeated")
        if state != CAPTURE_CREATION_ADMITTED:
            raise ValueError(f"capture_table_unadmitted:{state}")
        # The two identity fields a succeeded record only has once its native job was read
        # back: the job the producer names for this lane, and the digest over that job's
        # own resource. The digest is not compared with the job it is taken from here: that
        # comparison needs the stored creation evidence, which this seam never opens.
        _creation_job(record["job_id"], lane)
        digest = record["native_job_digest"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError(f"capture_native_job_unobserved:{lane}")
        table = _capture_table(day, lane)
        estates.add(_creation_destination(record["destination"], lane, table))
        lanes.add(lane)
        tables.add(table)
    if tuple(sorted(lanes)) != checked["source_tables"]:
        raise ValueError("capture_tables_unadmitted")
    if tuple(sorted(tables)) != checked["snapshot_tables"]:
        raise ValueError("capture_snapshot_tables_undeclared")
    if len(estates) != 1 or next(iter(estates)) != _capture_estate():
        raise ValueError("capture_estate_unadmitted")
    if declared_datasets is not None and next(iter(estates))[1] not in declared_datasets:
        raise ValueError("capture_dataset_undeclared")


def capture_registry_key(entry: object) -> str:
    """The registry object key of one capture attempt: profile and result identity."""
    checked = validate_capture_entry(entry)
    return f"{checked['profile_id']}/{checked['result_id']}"


def _acknowledgement(ack: object) -> str:
    if not isinstance(ack, Mapping) or ack.get("status") not in WRITE_ACKNOWLEDGEMENTS:
        raise ValueError("capture_write_ack_invalid")
    return ack["status"]


def _read_back(writer, key: str, admitted: dict) -> dict | None:
    found = writer.read(key)
    if found is None:
        return None
    if validate_capture_entry(found) != admitted:
        raise ValueError("profile_conflict")
    return admitted


def create_capture_entry(entry, verified, *, writer, existing=None) -> dict:
    """Admit an entry, then create its registry object conditionally, never replacing one.

    The writer must expose ``create(key, entry)`` returning an acknowledgement whose
    status is created, exists or ambiguous, and ``read(key)`` returning the stored entry
    or None. Admission runs before any write. An existing object equal to the admitted
    entry is idempotent; a different one is a profile conflict. An ambiguous
    acknowledgement is read back before any retry, and retries are bounded.
    """
    if not callable(getattr(writer, "create", None)) or not callable(getattr(writer, "read", None)):
        raise ValueError("capture_writer_invalid")
    admitted = admit_validated_capture(entry, verified, existing=existing)
    key = capture_registry_key(admitted)
    for _attempt in range(WRITE_ATTEMPTS):
        status = _acknowledgement(writer.create(key, dict(admitted)))
        if status == "created":
            return admitted
        found = _read_back(writer, key, admitted)
        if found is not None:
            return found
        if status == "exists":
            raise ValueError("capture_write_unresolved")
    raise ValueError("capture_write_unresolved")


def read_outcome_entries(outcome: object) -> list[dict]:
    """Convert a registry read outcome into entries, refusing every non-ok read."""
    if not isinstance(outcome, Mapping) or outcome.get("status") not in READ_STATUSES:
        raise ValueError("capture_read_invalid")
    status = outcome["status"]
    if status == "access_denied":
        raise ValueError("capture_read_access_denied")
    if status == "metadata_unavailable":
        raise ValueError("capture_read_metadata_unavailable")
    if status == "invalid_authority":
        raise ValueError("capture_read_authority_invalid")
    entries = outcome.get("entries")
    if not isinstance(entries, list):
        raise ValueError("capture_read_entries_missing")
    return list(entries)


def _classify(entry: object, *, now: datetime, scope: str) -> tuple[dict | None, str]:
    if entry is None:
        return None, "missing"
    try:
        checked = validate_capture_entry(entry)
    except ValueError as error:
        return None, str(error)
    if checked["scope"] != scope:
        return checked, "scope_mismatch"
    if checked["completion_state"] != "succeeded":
        return checked, f"not_succeeded:{checked['completion_state']}"
    if checked["expires_at"] is not None and checked["expires_at"] <= now:
        return checked, "expired"
    if checked["capture_available_at"] > now:
        return checked, "not_yet_available"
    return checked, "eligible"


def explain_capture_selection(
    entries: object, *, now: datetime, scope: str, profile_id: str | None = None
) -> dict:
    """Select the latest eligible capture and report why every entry qualified or not."""
    if entries is None or isinstance(entries, str | bytes | Mapping):
        raise ValueError("capture_entries_missing")
    _instant(now, "now")
    _identifier(scope, "scope")
    if profile_id is not None:
        _identifier(profile_id, "profile_id")
    bindings: dict[str, dict] = {}
    diagnostics: list[dict] = []
    eligible: list[tuple[tuple, dict]] = []
    for index, entry in enumerate(entries):
        checked, reason = _classify(entry, now=now, scope=scope)
        if checked is not None:
            binding = {field: checked[field] for field in PROFILE_BINDING_FIELDS}
            known = bindings.setdefault(checked["profile_id"], binding)
            if known != binding:
                raise ValueError("profile_conflict")
        if checked is not None and profile_id is not None and checked["profile_id"] != profile_id:
            reason = "profile_mismatch"
        diagnostics.append(
            {
                "index": index,
                "profile_id": None if checked is None else checked["profile_id"],
                "eligible": reason == "eligible",
                "reason": reason,
            }
        )
        if reason == "eligible" and checked is not None:
            order = (
                checked["observation_window_end"],
                checked["capture_available_at"],
                int(checked["generation"]),
                checked["result_id"],
            )
            eligible.append((order, checked))
    selected = max(eligible, key=lambda item: item[0])[1] if eligible else None
    return {"selected": selected, "diagnostics": tuple(diagnostics)}


def select_latest_capture(
    entries: list[dict], *, now: datetime, scope: str, profile_id: str | None = None
) -> dict | None:
    return explain_capture_selection(entries, now=now, scope=scope, profile_id=profile_id)[
        "selected"
    ]
