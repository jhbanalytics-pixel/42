import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

REGISTRY_PATH = (
    Path(__file__).resolve().parents[3]
    / "configs/open_intelligence/protected_context_registry_v1.json"
)
_VERSION = "protected_context_registry_v1"
_PIN_FIELDS = ("consumption_id", "manifest_sha256", "result_digest", "result_id")
_PROFILE_FIELDS = (
    "consumption_id",
    "cutoff_date",
    "manifest_sha256",
    "profile_id",
    "result_digest",
    "result_id",
)
_ENTRY_FIELDS = set(_PROFILE_FIELDS) | {"source_as_of", "market_scope", "snapshot_tables"}
# A v1 entry names a capture recorded in the v1 result ledger and keeps the key set it
# was written with. A v2 entry names a capture recorded in the v2 result ledger; it
# carries its result contract version, its pins are always complete and its snapshot
# tables are the ones the v2 capture plan derives from the cutoff.
RESULT_VERSION_V1 = "open_intelligence_execution_result_v1"
RESULT_VERSION_V2 = "open_intelligence_execution_result_v2"
_ENTRY_FIELDS_V2 = _ENTRY_FIELDS | {"result_contract_version"}
# A bridge entry names a v3 source bridge capture recorded as a daily execution chain. It
# also carries the derivation id the chain is read by; its pins are always complete and
# its snapshot tables are the seven bridge clones the v3 plan derives from the cutoff.
RESULT_VERSION_V3 = "open_intelligence_execution_result_v3"
_ENTRY_FIELDS_V3 = _ENTRY_FIELDS_V2 | {"derivation_id"}
# A bridge ledger entry names a v3 source bridge capture recorded as one consumed approval
# ledger execution. It keeps the v2 key set and result contract version, its profile id is
# the bridge profile id, and its snapshot tables are the seven bridge clones.
_BRIDGE_PROFILE = re.compile(r"staging_bridge_v3_\d{8}")
_cache = None


@dataclass(frozen=True, slots=True)
class ProtectedContextEntry:
    consumption_id: str | None
    cutoff_date: str
    manifest_sha256: str | None
    profile_id: str
    result_digest: str | None
    result_id: str | None
    source_as_of: str
    market_scope: tuple[str, ...]
    snapshot_tables: tuple[str, ...]
    result_contract_version: str = RESULT_VERSION_V1
    derivation_id: str | None = None

    @property
    def pinned(self):
        return self.result_id is not None


def result_version_for(profile_id):
    """The result ledger family a registered profile id names.

    A bridge profile id names the daily chain family here; a bridge entry recorded on the
    approval ledger is told apart by its entry fields, as ``bridge_authority`` reports.
    """
    if type(profile_id) is str and re.fullmatch(r"protected_context_\d{8}_v1", profile_id):
        return RESULT_VERSION_V1
    if type(profile_id) is str and re.fullmatch(r"protected_context_\d{8}_v2", profile_id):
        return RESULT_VERSION_V2
    if type(profile_id) is str and re.fullmatch(r"staging_bridge_v3_\d{8}", profile_id):
        return RESULT_VERSION_V3
    _invalid("profile id")


def v2_snapshot_tables(cutoff):
    """The five snapshot tables a v2 capture plan derives for one cutoff."""
    from src.analysis.open_intelligence.production_snapshot_tables import PROJECT
    from src.analysis.open_intelligence.staging_source_profile import SOURCE_LANES
    from src.analysis.open_intelligence.staging_source_profile import (
        STAGING_SOURCE_DATASET as dataset,
    )

    day = cutoff.strftime("%Y%m%d")
    return [f"{PROJECT}.{dataset}.staging_source_{day}_{lane}" for lane in SOURCE_LANES]


def bridge_snapshot_tables(cutoff):
    """The seven clone tables a v3 bridge plan derives for one cutoff."""
    from src.analysis.open_intelligence.source_estate_bridge import LANES, PROJECT, bridge_policy

    dataset = bridge_policy()["snapshot_dataset"]
    day = cutoff.strftime("%Y%m%d")
    return [f"{PROJECT}.{dataset}.staging_bridge_v3_{day}_{lane}" for lane in LANES]


def _invalid(reason):
    raise ValueError("protected_context_registry_invalid") from ValueError(reason)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid("duplicate key")
        result[key] = value
    return result


def _no_number(value):
    _invalid("noninteger number")


def _entry(value):
    from src.analysis.open_intelligence.production_snapshot_tables import LANES

    families = {
        frozenset(_ENTRY_FIELDS): ("v1", RESULT_VERSION_V1),
        frozenset(_ENTRY_FIELDS_V2): ("v2", RESULT_VERSION_V2),
        frozenset(_ENTRY_FIELDS_V3): ("v3", RESULT_VERSION_V3),
    }
    if type(value) is not dict or frozenset(value) not in families:
        _invalid("entry fields")
    family, version = families[frozenset(value)]
    if (
        family == "v2"
        and type(value["profile_id"]) is str
        and _BRIDGE_PROFILE.fullmatch(value["profile_id"])
    ):
        family = "ledger"
    if value.get("result_contract_version", RESULT_VERSION_V1) != version:
        _invalid("result contract version")
    cutoff = value["cutoff_date"]
    if type(cutoff) is not str or date.fromisoformat(cutoff).isoformat() != cutoff:
        _invalid("cutoff date")
    parsed = date.fromisoformat(cutoff)
    expected_profile = (
        f"staging_bridge_v3_{parsed:%Y%m%d}"
        if family in ("v3", "ledger")
        else f"protected_context_{parsed:%Y%m%d}_{family}"
    )
    if value["profile_id"] != expected_profile:
        _invalid("profile id")
    if (
        value["source_as_of"]
        != datetime.combine(parsed + timedelta(days=1), time.min, UTC).isoformat()
    ):
        _invalid("source as of")
    markets = value["market_scope"]
    if (
        type(markets) is not list
        or not markets
        or any(type(m) is not str for m in markets)
        or markets != sorted(set(markets))
        or not set(markets) <= {"ke", "ng", "za"}
    ):
        _invalid("market scope")
    tables = value["snapshot_tables"]
    expected = (
        bridge_snapshot_tables(parsed)
        if family in ("v3", "ledger")
        else v2_snapshot_tables(parsed)
        if family == "v2"
        else [
            f"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_{parsed:%Y%m%d}_{lane}"
            for lane in LANES
        ]
    )
    if type(tables) is not list or tables != expected:
        _invalid("snapshot tables")
    pins = [value[key] for key in _PIN_FIELDS]
    if family != "v1" or not all(pin is None for pin in pins):
        for key in _PIN_FIELDS:
            prefix = {"consumption_id": "exc_", "result_id": "exr_"}.get(key, "")
            if (
                type(value[key]) is not str
                or re.fullmatch(prefix + r"[0-9a-f]{64}", value[key]) is None
            ):
                _invalid("authority pins must be complete or all null")
    if family == "v3" and (
        type(value["derivation_id"]) is not str
        or re.fullmatch(r"exd_[0-9a-f]{64}", value["derivation_id"]) is None
        or value["result_id"] != "exr_" + value["result_digest"]
    ):
        _invalid("bridge chain pins")
    return ProtectedContextEntry(
        **{**value, "market_scope": tuple(markets), "snapshot_tables": tuple(tables)}
    )


def parse_registry_bytes(raw):
    """Validate registry bytes under the loader's rules; return the document and records."""
    try:
        if type(raw) is not bytes or len(raw) > 1024 * 1024:
            _invalid("registry too large")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_no_number,
            parse_constant=_no_number,
        )
        if type(value) is not dict or set(value) != {"contract_version", "registry_id", "entries"}:
            _invalid("registry fields")
        if value["contract_version"] != _VERSION or value["registry_id"] != _VERSION:
            _invalid("registry version or id")
        if (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            != raw
        ):
            _invalid("noncanonical registry")
        if type(value["entries"]) is not list or not value["entries"]:
            _invalid("entries")
        records = tuple(_entry(item) for item in value["entries"])
        cutoffs = tuple(item.cutoff_date for item in records)
        if cutoffs != tuple(sorted(set(cutoffs))):
            _invalid("duplicate or unordered cutoffs")
        return value, records
    except (
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        RecursionError,
    ) as error:
        if str(error) == "protected_context_registry_invalid":
            raise
        raise ValueError("protected_context_registry_invalid") from error


def load_protected_context_registry():
    global _cache
    try:
        raw = REGISTRY_PATH.read_bytes()
    except OSError as error:
        raise ValueError("protected_context_registry_invalid") from error
    _value, records = parse_registry_bytes(raw)
    if _cache != records:
        _cache = records
    return _cache


def allowed_cutoffs():
    """Cutoffs of pinned v1 entries: the only ones the v1 capture and read gates admit.

    An entry with null pins names no completed capture, so it admits nothing. A v2 entry
    is read through the v2 result ledger and never widens a v1 gate.
    """
    return tuple(
        entry.cutoff_date
        for entry in load_protected_context_registry()
        if entry.pinned and entry.result_contract_version == RESULT_VERSION_V1
    )


def bridge_authority(entry):
    """The record that authorises a bridge entry: its daily chain or its ledger execution."""
    if type(entry) is ProtectedContextEntry and _BRIDGE_PROFILE.fullmatch(entry.profile_id):
        if entry.result_contract_version == RESULT_VERSION_V3 and entry.derivation_id is not None:
            return "daily_chain"
        if entry.result_contract_version == RESULT_VERSION_V2 and entry.derivation_id is None:
            return "execution_ledger"
    _invalid("not a bridge entry")


def bridge_entries():
    """Bridge v3 entries, newest cutoff first.

    Each names one daily capture chain or one consumed approval ledger execution.
    """
    return tuple(
        sorted(
            (
                entry
                for entry in load_protected_context_registry()
                if _BRIDGE_PROFILE.fullmatch(entry.profile_id)
            ),
            key=lambda entry: entry.cutoff_date,
            reverse=True,
        )
    )


def bridge_registry_row(entry):
    """The row a bridge read binding digests: the bridge contract's registry fields."""
    from src.analysis.open_intelligence.source_estate_bridge import REGISTRY_FIELDS

    bridge_authority(entry)
    return {
        name: list(getattr(entry, name))
        if isinstance(getattr(entry, name), tuple)
        else getattr(entry, name)
        for name in REGISTRY_FIELDS
    }


def profile_for(cutoff):
    if type(cutoff) is date:
        cutoff = cutoff.isoformat()
    for entry in load_protected_context_registry():
        if entry.cutoff_date == cutoff:
            return entry
    _invalid("unknown cutoff")


class _ProtectedProfiles(Sequence):
    """Pinned v1 profiles: the v1 context readers read these through the v1 result ledger."""

    def _values(self):
        return tuple(
            {key: getattr(entry, key) for key in _PROFILE_FIELDS}
            for entry in load_protected_context_registry()
            if entry.pinned and entry.result_contract_version == RESULT_VERSION_V1
        )

    def __getitem__(self, index):
        return self._values()[index]

    def __len__(self):
        return len(self._values())

    def __iter__(self):
        return iter(self._values())

    def __eq__(self, other):
        return self._values() == other


_PROTECTED_CONTEXT_PROFILES = _ProtectedProfiles()
