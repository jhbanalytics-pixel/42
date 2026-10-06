"""The two reads that give the investigation layer an entrance.

Nothing in the product created or listed an investigation. The Evidence Room,
the Historical workspace and the Client Read preview all read an investigation
by identity, and no screen could produce one, so the last third of the
strategist journey had no way in. That was never a data shortage: releasing
predictions and deploying staging would not have changed it.

Two reads fix it. One says which scopes exist and whether a run has been
released against them, because the creation request is fixed and demands scope
identity the browser has no other way to learn. The other lists the
investigations already in scope, so the dossier landing can be what the
contract says it is.

Both are read only. Both are bounded. Neither guesses.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.api import investigation_scopes

CONTRACT_VERSION = "intelligence_dossier_v1"
SCOPE_RESOURCE_VERSION = "investigation_scope_index_v1"
INDEX_RESOURCE_VERSION = "investigation_index_v1"

# Most recent first, and never more than this many. The surface says so when it
# truncates, because a partial list that reads as complete is worse than a short
# one that admits it.
INDEX_LIMIT = 50

# Human labels for the markets this programme covers. The identifiers travel
# separately because the creation request needs them exactly; these are only for
# a person to read.
_MARKET_LABELS = {
    "za": "South Africa",
    "ng": "Nigeria",
    "ke": "Kenya",
}


class InvestigationIndexError(RuntimeError):
    """The index response violates its contract."""


def _market_labels(markets: Sequence[str]) -> list[str]:
    # An unmapped market shows its identifier rather than being dropped. A
    # market missing from a list of markets is a lie about coverage; an
    # unfamiliar name is only unfamiliar.
    return [_MARKET_LABELS.get(market, market) for market in markets]


def _run_identity(value: object) -> str | None:
    """A run identity, or None when there is not one.

    An empty or non-string value is not an identity that happens to be short.
    The creation request requires this field and validates it as non-empty text
    only, so passing a blank through would satisfy the field while meaning
    nothing, and the investigation would rest on a run that was never named.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def read_scopes(
    *,
    config: Mapping[str, Any] | None = None,
    latest_run_lookup: Callable[[str], object] | None = None,
) -> dict[str, Any]:
    """Which scopes exist, and whether a run has been released against each.

    The scope-resolution addendum fixes the creation request, so the browser
    must send `client_scope_id`, `market_scope`, `brand_config_id`,
    `audience_lens_ids` and `theme_id`, and the server refuses unless the brand
    and theme match the configured values exactly. Nothing told the browser what
    those were. Copying the config into the client would put a contract in the
    one place it cannot be trusted, and drift the moment the config changed.
    """
    raw = (
        config
        if config is not None
        else investigation_scopes._load(investigation_scopes.INVESTIGATION_SCOPES_PATH)
    )
    scopes: list[dict[str, Any]] = []
    for scope_id, scope in (raw.get("scopes") or {}).items():
        if not isinstance(scope, Mapping) or scope.get("enabled") is not True:
            continue
        markets = list(scope.get("market_scope") or ())
        scopes.append(
            {
                "client_scope_id": scope_id,
                "market_labels": _market_labels(markets),
                "market_scope": markets,
                "brand_config_id": scope.get("brand_config_id"),
                "audience_lens_ids": list(scope.get("audience_lens_ids") or ()),
                "theme_id": scope.get("theme_id"),
                "latest_run_id": _run_identity(
                    latest_run_lookup(scope_id) if latest_run_lookup else None
                ),
            }
        )
    payload = {
        "contract_version": CONTRACT_VERSION,
        "resource_version": SCOPE_RESOURCE_VERSION,
        "default_scope_id": raw.get("default_scope_id"),
        "scopes": scopes,
    }
    validate_scope_payload(payload)
    return payload


def validate_scope_payload(payload: Mapping[str, Any]) -> None:
    """The response contract, asserted on the way out."""
    expected = ("contract_version", "resource_version", "default_scope_id", "scopes")
    if tuple(payload) != expected:
        raise InvestigationIndexError("scope response field order is invalid")
    if payload["resource_version"] != SCOPE_RESOURCE_VERSION:
        raise InvestigationIndexError("scope resource version is unsupported")
    scope_fields = (
        "client_scope_id",
        "market_labels",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "latest_run_id",
    )
    for scope in payload["scopes"]:
        if tuple(scope) != scope_fields:
            raise InvestigationIndexError("scope entry field order is invalid")
        if len(scope["market_labels"]) != len(scope["market_scope"]):
            raise InvestigationIndexError("every market must carry a label")


def _created_at(record: Mapping[str, Any]) -> str:
    value = record.get("created_at")
    return value if isinstance(value, str) else ""


def read_index(
    records: Sequence[Mapping[str, Any]],
    *,
    client_scope_id: str | None = None,
) -> dict[str, Any]:
    """The investigations in scope, most recent first and bounded.

    Scope is server owned. A record belonging to another scope is not this
    desk's to show, and a caller cannot widen its own view by asking.
    """
    admitted = []
    for record in records:
        investigation_id = record.get("investigation_id")
        if not isinstance(investigation_id, str) or not investigation_id:
            # Excluded rather than shown blank. A row with no identity cannot be
            # opened, so offering it would be offering a door with no room.
            continue
        if (
            client_scope_id is not None
            and record.get("client_scope_id") != client_scope_id
        ):
            continue
        admitted.append(record)

    admitted.sort(key=_created_at, reverse=True)
    total = len(admitted)
    shown = admitted[:INDEX_LIMIT]
    payload = {
        "contract_version": CONTRACT_VERSION,
        "resource_version": INDEX_RESOURCE_VERSION,
        "investigations": [
            {
                "investigation_id": record["investigation_id"],
                "decision_question": record.get("decision_question"),
                "market_labels": _market_labels(list(record.get("market_scope") or ())),
                "created_at": record.get("created_at"),
                "status": record.get("status"),
                "readiness_state": record.get("readiness_state"),
                "candidate_artifact_id": record.get("candidate_artifact_id"),
            }
            for record in shown
        ],
        "total_count": total,
        # Stated, so a capped view is never mistaken for the whole set.
        "truncated": total > INDEX_LIMIT,
    }
    validate_index_payload(payload)
    return payload


def validate_index_payload(payload: Mapping[str, Any]) -> None:
    expected = (
        "contract_version",
        "resource_version",
        "investigations",
        "total_count",
        "truncated",
    )
    if tuple(payload) != expected:
        raise InvestigationIndexError("index response field order is invalid")
    if payload["resource_version"] != INDEX_RESOURCE_VERSION:
        raise InvestigationIndexError("index resource version is unsupported")
    entry_fields = (
        "investigation_id",
        "decision_question",
        "market_labels",
        "created_at",
        "status",
        "readiness_state",
        "candidate_artifact_id",
    )
    for entry in payload["investigations"]:
        if tuple(entry) != entry_fields:
            raise InvestigationIndexError("index entry field order is invalid")
    if payload["truncated"] and len(payload["investigations"]) != INDEX_LIMIT:
        raise InvestigationIndexError("a truncated index must carry exactly the bound")


# Bounded blob read for the durable listing. Larger than the display bound so
# sorting sees enough of the estate, still finite so a huge prefix cannot make
# one landing request read everything ever written.
STORE_READ_LIMIT = 200

_INVESTIGATIONS_PREFIX = "open-intelligence/v2/staging/investigations/"


def read_stored_investigations(*, bucket: Any) -> list[dict[str, Any]]:
    """The records the creation route wrote, read back for listing.

    Reads only under the exact investigations prefix, bounded by recency, and
    admits only the exact nested shape the creation route stores, revalidated
    here. A blob that is not such a record is skipped rather than shown: the
    store is writable by more hands than this reader, and a shape check is the
    difference between listing what was framed and listing what was planted.
    A storage failure raises: unavailable is a different fact from empty, and
    the route must be able to tell a caller which one happened.
    """
    import json

    from src.api import investigations

    try:
        blobs = list(bucket.list_blobs(prefix=_INVESTIGATIONS_PREFIX))
    except Exception as error:
        raise InvestigationIndexError("investigation storage is unavailable") from error
    # The bound follows recency, not object name. A bound taken in listing
    # order would silently drop the newest records once the estate outgrew it.
    blobs.sort(
        key=lambda blob: (
            getattr(blob, "time_created", None) is not None,
            getattr(blob, "time_created", None),
        ),
        reverse=True,
    )
    records: list[dict[str, Any]] = []
    for blob in blobs[:STORE_READ_LIMIT]:
        if not str(getattr(blob, "name", "")).startswith(_INVESTIGATIONS_PREFIX):
            continue
        try:
            record = json.loads(blob.download_as_bytes().decode("utf-8"))
            frame, response = investigations.validate_investigation_storage_record(
                record
            )
        except Exception:
            # Skipped, not fatal and not invented. One corrupt or foreign
            # object must not take the landing down, and a placeholder row
            # would be a door with no room behind it.
            continue
        records.append(
            {
                "investigation_id": response["investigation_id"],
                "client_scope_id": response["client_scope_id"],
                "decision_question": frame.decision_question,
                "market_scope": list(frame.market_scope),
                "created_at": response["created_at"],
                "status": response["status"],
            }
        )
    return records


def observe_fieldwork_investigations(
    *,
    bucket: Any,
    scope: dict[str, Any],
    now=None,
) -> dict[str, Any]:
    import json
    import time
    from copy import deepcopy
    from datetime import UTC, datetime

    from src.api import fieldwork, investigations

    fieldwork._scope_v2(scope)
    boundary = now or datetime.now(UTC)
    if boundary.utcoffset() is None:
        raise InvestigationIndexError("investigation observation boundary is invalid")
    end = boundary.astimezone(UTC).isoformat().replace("+00:00", "Z")
    deadline = time.monotonic() + 10
    unavailable = fieldwork._unavailable_coverage_v2()
    unavailable["reasons"] = ["listing_unavailable"]
    result = {"scope": deepcopy(scope), "coverage": unavailable, "rows": []}
    if getattr(bucket, "name", None) != "listening-post-staging-cache":
        return result
    reasons = set()
    blobs = []
    page_token = None
    tokens = set()
    while len(blobs) <= STORE_READ_LIMIT:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reasons.add("read_deadline_reached")
            break
        try:
            iterator = bucket.list_blobs(
                prefix=_INVESTIGATIONS_PREFIX,
                max_results=STORE_READ_LIMIT + 1 - len(blobs),
                page_size=STORE_READ_LIMIT + 1 - len(blobs),
                page_token=page_token,
                timeout=remaining,
                retry=None,
            )
            page = next(iterator.pages, ())
            if time.monotonic() >= deadline:
                reasons.add("read_deadline_reached")
                break
            for blob in page:
                if time.monotonic() >= deadline:
                    reasons.add("read_deadline_reached")
                    break
                blobs.append(blob)
                if len(blobs) > STORE_READ_LIMIT:
                    break
            if reasons:
                break
            page_token = iterator.next_page_token
            if page_token is None:
                break
            if (
                not isinstance(page_token, str)
                or not page_token
                or page_token in tokens
            ):
                reasons.add("listing_unavailable")
                break
            tokens.add(page_token)
        except Exception:
            if not blobs:
                return result
            reasons.add("listing_unavailable")
            break
    if len(blobs) > STORE_READ_LIMIT:
        reasons.add("read_limit_reached")
    blobs.sort(key=lambda blob: str(getattr(blob, "time_created", "")), reverse=True)
    rows = []
    timestamps = []
    identities = set()
    for blob in blobs[:STORE_READ_LIMIT]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reasons.add("read_deadline_reached")
            break
        try:
            generation = str(blob.generation)
            if not generation.isdecimal() or int(generation) <= 0:
                raise ValueError("invalid generation")
            if type(blob.size) is not int or not 0 < blob.size <= 1048576:
                raise ValueError("invalid record size")
            name = blob.name
            if not isinstance(name, str) or not name.startswith(_INVESTIGATIONS_PREFIX):
                raise ValueError("invalid record name")
        except (AttributeError, ValueError):
            reasons.add("record_invalid")
            continue
        try:
            raw = blob.download_as_bytes(
                if_generation_match=int(generation),
                timeout=remaining,
                retry=None,
            )
        except Exception:
            reasons.add("record_unavailable")
            continue
        if time.monotonic() >= deadline:
            reasons.add("read_deadline_reached")
            break
        try:
            if len(raw) != blob.size or len(raw) > 1048576:
                raise ValueError("invalid record bytes")
            frame, response = investigations.validate_investigation_storage_record(
                json.loads(raw)
            )
            identity = response["investigation_id"]
            if name != _INVESTIGATIONS_PREFIX + identity + ".json":
                raise ValueError("invalid record identity")
            if identity != investigations.investigation_id_for_frame(frame):
                raise ValueError("invalid frame identity")
            if response["status"] != "plan_ready":
                raise ValueError("invalid plan status")
            if any(
                getattr(frame, key) != scope[key]
                for key in ("client_scope_id", "brand_config_id", "theme_id")
            ):
                continue
            if set(frame.audience_lens_ids) != set(scope["audience_lens_ids"]):
                continue
            if not set(frame.market_scope) <= set(scope["market_scope"]):
                continue
            updated = response["updated_at"]
            fieldwork._timestamp_v2(updated)
            if datetime.fromisoformat(updated.replace("Z", "+00:00")) > boundary:
                raise ValueError("future observation")
            if identity in identities:
                raise ValueError("duplicate identity")
            row = dict(
                operation_id=identity,
                kind="research",
                operation_type="investigation_plan",
                entity_id=identity,
                title=frame.decision_question,
                status="planned",
                market_scope=sorted(frame.market_scope),
                updated_at=updated,
                evidence_readiness="unchecked",
                gaps=[],
                next_operation=dict(
                    action="open_investigation",
                    entity_id=identity,
                    label="Open investigation",
                    available=True,
                    reason=None,
                ),
            )
            fieldwork._operation_v2(row, scope, "investigation_plan")
        except (ValueError, TypeError, KeyError):
            reasons.add("record_invalid")
            continue
        identities.add(identity)
        rows.append(row)
        timestamps.append(updated)
    known = len(rows)
    rows.sort(key=fieldwork._row_sort_v2)
    rows = rows[: fieldwork.RESEARCH_ROW_LIMIT]
    status_counts = dict.fromkeys(fieldwork.OPERATION_STATUSES, 0)
    status_counts["planned"] = known
    coverage = dict(
        state="partial" if reasons else "complete",
        observed_at=max(
            timestamps,
            key=lambda stamp: datetime.fromisoformat(stamp.replace("Z", "+00:00")),
        )
        if timestamps
        else None,
        window=dict(basis="retained_investigation_store", start=None, end=end),
        known_count=known,
        known_by_status=status_counts,
        total_count=None if reasons else known,
        returned_count=len(rows),
        unread_count=None if reasons else 0,
        invalid_count=None if reasons else 0,
        reasons=sorted(reasons),
    )
    fieldwork._coverage_v2(coverage, "investigation_plans")
    return {"scope": deepcopy(scope), "coverage": coverage, "rows": rows}
