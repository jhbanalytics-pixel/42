"""Sampled source binding for the explicitly versioned provenance path."""

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time, timedelta
from types import MappingProxyType

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.candidates import (
    Observation,
    resolve_source_identity,
    validate_source_identity,
)
from src.analysis.open_intelligence.pipeline import (
    EVIDENCE_COLUMNS_BY_TABLE,
    MAX_REQUESTED_SAMPLE_KEYS,
    TARGET_DATASET,
    TARGET_PROJECT,
    EvidenceProjection,
    _unique_evidence_rows,
)

PROVENANCE_VERSION = "candidate_source_provenance_v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset(
    {
        "contract_version",
        "coverage_basis",
        "source_snapshot_digest",
        "resolution_state",
        "pairs",
        "unresolved_sample_refs",
    }
)


def _error(reason="conflict"):
    return ValueError(f"source_provenance_{reason}")


def _digest(value):
    from scripts.staging.replay_open_intelligence import _digest as replay_digest

    return replay_digest(value)


def _sequence(value):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _error()
    return tuple(value)


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise _error("snapshot_mismatch") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _error("snapshot_mismatch")
    return value.astimezone(UTC)


def encode_source_provenance(envelope):
    if not isinstance(envelope, Mapping) or set(envelope) != _FIELDS:
        raise _error()
    if envelope["contract_version"] != PROVENANCE_VERSION:
        raise _error("version_invalid")
    digest = envelope["source_snapshot_digest"]
    if (
        envelope["coverage_basis"] != "sampled_record_support"
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
    ):
        raise _error()
    pair_keys = []
    locators = set()
    for pair in _sequence(envelope["pairs"]):
        if not isinstance(pair, Mapping) or set(pair) != {
            "vendor_family",
            "channel_family",
            "source_table",
            "sample_refs",
        }:
            raise _error()
        try:
            vendor, channel = validate_source_identity(
                pair["vendor_family"], pair["channel_family"]
            )
        except ValueError as error:
            raise _error() from error
        if (vendor, channel) != (pair["vendor_family"], pair["channel_family"]):
            raise _error()
        table = pair["source_table"]
        if table not in EVIDENCE_COLUMNS_BY_TABLE:
            raise _error()
        pair_keys.append((vendor, channel, table))
        refs = _sequence(pair["sample_refs"])
        ids = []
        if not refs:
            raise _error()
        for ref in refs:
            if not isinstance(ref, Mapping) or set(ref) != {"row_id", "row_digest"}:
                raise _error()
            row_id, row_digest = ref["row_id"], ref["row_digest"]
            if (
                not isinstance(row_id, str)
                or not row_id.strip()
                or not isinstance(row_digest, str)
                or not _DIGEST.fullmatch(row_digest)
            ):
                raise _error()
            if row_id in locators:
                raise _error()
            locators.add(row_id)
            ids.append(row_id)
        if ids != sorted(set(ids)):
            raise _error()
    if pair_keys != sorted(set(pair_keys)):
        raise _error()
    missing = []
    for ref in _sequence(envelope["unresolved_sample_refs"]):
        if not isinstance(ref, Mapping) or set(ref) != {"row_id", "reason"}:
            raise _error()
        row_id = ref["row_id"]
        if (
            not isinstance(row_id, str)
            or not row_id.strip()
            or row_id in locators
            or ref["reason"] not in {"missing", "ambiguous", "unsupported_identity"}
        ):
            raise _error()
        missing.append(row_id)
    if missing != sorted(set(missing)):
        raise _error()
    expected_state = (
        "partial" if pair_keys and missing else "resolved" if pair_keys else "unavailable"
    )
    if envelope["resolution_state"] != expected_state:
        raise _error()
    if len(locators) + len(missing) > MAX_REQUESTED_SAMPLE_KEYS:
        raise _error("limit_exceeded")
    return canonical_bytes(_plain(envelope)).decode("utf-8")


def bind_source_provenance(*, observations, member_samples, projection, source_snapshot):
    if not isinstance(projection, EvidenceProjection) or not isinstance(source_snapshot, Mapping):
        raise _error()
    snapshot = dict(source_snapshot)
    snapshot_digest = snapshot.pop("source_digest", None)
    if (
        not isinstance(snapshot_digest, str)
        or not _DIGEST.fullmatch(snapshot_digest)
        or _digest(snapshot) != snapshot_digest
    ):
        raise _error("snapshot_mismatch")
    captured_at = _timestamp(snapshot.get("captured_at"))
    visibility_at = captured_at
    if "snapshot_contract_version" in snapshot:
        from src.analysis.open_intelligence.production_snapshot import SNAPSHOT_CONTRACT_VERSION

        if snapshot["snapshot_contract_version"] != SNAPSHOT_CONTRACT_VERSION:
            raise _error("version_invalid")
        visibility_at = _timestamp(snapshot.get("source_as_of"))
        cutoff = projection.source_window.end_date
        if (
            snapshot.get("cutoff_date") != cutoff.isoformat()
            or visibility_at != datetime.combine(cutoff + timedelta(days=1), time.min, UTC)
            or captured_at < visibility_at
        ):
            raise _error("snapshot_mismatch")
    rows_by_table = snapshot.get("rows_by_table")
    if not isinstance(rows_by_table, Mapping):
        raise _error("snapshot_mismatch")
    index = {}
    for table, fields in EVIDENCE_COLUMNS_BY_TABLE.items():
        groups = defaultdict(list)
        for row in _sequence(rows_by_table.get(table)):
            if not isinstance(row, Mapping) or set(row) != set(fields):
                raise _error("snapshot_mismatch")
            key = (row["market"], row["id"])
            if any(not isinstance(item, str) or not item.strip() for item in key):
                raise _error("snapshot_mismatch")
            collected_at = _timestamp(row["collected_at"])
            if (
                not projection.source_window.start_date
                <= collected_at.date()
                <= projection.source_window.end_date
                or collected_at > visibility_at
            ):
                continue
            groups[key].append(row)
        index[table] = groups
    by_member = {}
    for observation in _sequence(observations):
        if not isinstance(observation, Observation):
            raise _error()
        identity = f"{observation.market}|{observation.candidate_type}|{observation.term}"
        if identity in by_member:
            raise _error()
        if observation.market not in projection.source_window.markets:
            raise _error()
        by_member[identity] = observation
    if (
        not isinstance(member_samples, Mapping)
        or set(member_samples) != set(by_member)
        or set(projection.receipts_by_member) != set(by_member)
    ):
        raise _error()
    requested = {}
    for identity in by_member:
        ids = _sequence(member_samples[identity])
        if any(not isinstance(item, str) or not item.strip() for item in ids) or len(
            set(ids)
        ) != len(ids):
            raise _error()
        requested[identity] = tuple(sorted(ids))
    if (
        len(
            {
                (by_member[identity].market, row_id)
                for identity, ids in requested.items()
                for row_id in ids
            }
        )
        > MAX_REQUESTED_SAMPLE_KEYS
    ):
        raise _error("limit_exceeded")
    result = {}
    for identity in sorted(by_member):
        market = by_member[identity].market
        projected_rows = _sequence(projection.receipts_by_member[identity])
        if len({row.row_id for row in projected_rows}) != len(projected_rows):
            raise _error()
        projected = {row.row_id: row for row in projected_rows}
        if set(projected) - set(requested[identity]) or any(
            row.market != market for row in projected_rows
        ):
            raise _error()
        pairs = defaultdict(list)
        missing = []
        for row_id in requested[identity]:
            resolved = None
            reason = "missing"
            for table in ("enriched_content", "raw_content"):
                matches = index[table].get((market, row_id), ())
                if len(matches) > 1:
                    reason = "ambiguous"
                    break
                if not matches:
                    continue
                original = matches[0]
                typed = dict(original)
                typed["match_count"] = len(matches)
                typed["collected_at"] = _timestamp(typed["collected_at"])
                if typed["published_at"] is not None:
                    typed["published_at"] = _timestamp(typed["published_at"])
                vendor, channel = typed["vendor_family"], typed["channel_family"]
                if (vendor is None) != (channel is None) or any(
                    value is not None and (not isinstance(value, str) or not value.strip())
                    for value in (vendor, channel)
                ):
                    raise _error()
                try:
                    resolve_source_identity(
                        source=typed["source"],
                        platform=typed["platform"],
                        content_type=typed["content_type"],
                        endpoint=typed["endpoint"],
                        vendor_family=vendor,
                        channel_family=channel,
                    )
                except ValueError:
                    reason = "unsupported_identity"
                    break
                try:
                    found, _ = _unique_evidence_rows((typed,), table)
                except ValueError as error:
                    raise _error() from error
                resolved = found.get((market, row_id))
                if resolved is not None:
                    if (
                        f"{TARGET_PROJECT}.{TARGET_DATASET}.{table}"
                        not in projection.source_window.source_tables
                    ):
                        raise _error()
                    if projected.get(row_id) != resolved:
                        raise _error()
                    key = (resolved.vendor_family, resolved.channel_family, table)
                    pairs[key].append({"row_id": row_id, "row_digest": _digest(original)})
                    break
            if resolved is None:
                if row_id in projected:
                    raise _error()
                missing.append({"row_id": row_id, "reason": reason})
        envelope = {
            "contract_version": PROVENANCE_VERSION,
            "coverage_basis": "sampled_record_support",
            "source_snapshot_digest": snapshot_digest,
            "resolution_state": "partial"
            if pairs and missing
            else "resolved"
            if pairs
            else "unavailable",
            "pairs": tuple(
                {
                    "vendor_family": vendor,
                    "channel_family": channel,
                    "source_table": table,
                    "sample_refs": tuple(refs),
                }
                for (vendor, channel, table), refs in sorted(pairs.items())
            ),
            "unresolved_sample_refs": tuple(missing),
        }
        encode_source_provenance(envelope)
        result[identity] = _freeze(envelope)
    return MappingProxyType(result)


def provenance_member_id(legacy_member_id, envelope):
    if (
        not isinstance(legacy_member_id, str)
        or re.fullmatch(r"mem_[0-9a-f]{64}", legacy_member_id) is None
    ):
        raise _error()
    encode_source_provenance(envelope)
    return "mem_" + canonical_digest(
        {"legacy_member_id": legacy_member_id, "source_provenance": _plain(envelope)}
    )


def validate_source_provenance(value, *, observations, member_samples, projection, source_snapshot):
    expected = bind_source_provenance(
        observations=observations,
        member_samples=member_samples,
        projection=projection,
        source_snapshot=source_snapshot,
    )
    if not isinstance(value, Mapping) or set(value) != set(expected):
        raise _error()
    if any(
        encode_source_provenance(value[key]) != encode_source_provenance(expected[key])
        for key in expected
    ):
        raise _error()
