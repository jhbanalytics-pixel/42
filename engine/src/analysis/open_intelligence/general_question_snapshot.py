"""Build one immutable admitted general-question evidence snapshot."""

import copy
import json
import re
from datetime import date, datetime, timedelta
from time import monotonic
from types import MappingProxyType

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.general_question_context_identity import (
    support_slots_from_groups,
)
from src.analysis.open_intelligence.general_question_context_queries import (
    SUPPLEMENT_EVIDENCE_LIMIT,
    _context_requirements,
)
from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    control_time,
    utc_time,
)
from src.analysis.open_intelligence.general_question_copy_schema import (
    read_question_copy_schemas,
    validate_current_source_copy_schemas,
)
from src.analysis.open_intelligence.general_question_copy_validation import (
    build_current_source_copy_query,
    validate_current_source_copy_proof,
)
from src.analysis.open_intelligence.general_question_deployment import validate_question_invocation
from src.analysis.open_intelligence.general_question_planning import validate_stored_question_plan
from src.analysis.open_intelligence.general_question_query_execution import (
    execute_current_source_copy_query,
    execute_release_material_query,
    execute_released_candidate_query,
    execute_selected_source_query,
)
from src.analysis.open_intelligence.general_question_release_admission import (
    validate_released_run_admission,
)
from src.analysis.open_intelligence.general_question_release_material import (
    build_release_material_query,
)
from src.analysis.open_intelligence.general_question_retrieval_queries import (
    build_released_candidate_query,
    select_released_candidates_for_inspection,
)
from src.analysis.open_intelligence.general_question_schema import validate_source_schemas
from src.analysis.open_intelligence.general_question_schema_read import (
    read_question_source_schemas,
)
from src.analysis.open_intelligence.general_question_source_query import (
    build_selected_source_query,
    decode_selected_source_rows,
)
from src.analysis.open_intelligence.run_receipts import build_run_receipt

_FAMILIES = {
    "candidates": "signal_candidates_v2",
    "evidence": "signal_evidence_v2",
    "membership": "signal_membership_v2",
    "lineage": "signal_lineage_v2",
    "analysis": "signal_analysis_v2",
    "predictions": "signal_predictions_v2",
    "outcomes": "signal_outcomes_v2",
}
_COPY_MAXIMUM_BYTES = 350_000_000
# Context snapshot profiles. v1 is the retained shape and validates untouched; v2 adds the
# identity sidecar beside the same receipts, readings and provenance, paired with the v2
# admission; v3 pairs with the v3 admission, whose sidecar also carries the origin
# authority projection. No migration rewrites a retained v1 or v2 snapshot: each profile
# is read through the sidecar shape it was written with.
_CONTEXT_PROFILE_V1 = "protected_context_v1"
_CONTEXT_PROFILE_V2 = "protected_context_v2"
_CONTEXT_PROFILE_V3 = "protected_context_v3"
_CURRENT_CONTEXT_PROFILE = _CONTEXT_PROFILE_V3
_CONTEXT_PROFILES = {
    _CONTEXT_PROFILE_V1: "protected_context_admission_v1",
    _CONTEXT_PROFILE_V2: "protected_context_admission_v2",
    _CONTEXT_PROFILE_V3: "protected_context_admission_v3",
}
_IDENTITY_PROFILES = frozenset({_CONTEXT_PROFILE_V2, _CONTEXT_PROFILE_V3})
_SIDECAR_FIELDS_V1 = (
    "identity_policy_digest",
    "native_id_projection",
    "observation_keys",
    "origin_groups",
    "independent_origin_count",
    "unknown_origin_count",
    "units",
)
_SIDECAR_FIELDS_V2 = (*_SIDECAR_FIELDS_V1, "origin_authority_projection")
# The sidecar shape each admission version is read and rebuilt through.
_SIDECAR_VERSIONS = {
    "protected_context_admission_v2": ("general_question_identity_sidecar_v1", _SIDECAR_FIELDS_V1),
    "protected_context_admission_v3": ("general_question_identity_sidecar_v2", _SIDECAR_FIELDS_V2),
}
# What a record written before the origin authority projection existed reports for it: the
# projection was never recorded, which is neither a measured zero nor a captured null.
_PROJECTION_NOT_RECORDED = {
    "state": "not_recorded",
    "projected_records": None,
    "unprojected_records": None,
}
_COORDINATOR_CHECKS = frozenset(
    {
        "physical_schema_validation",
        "source_row_copy_binding",
        "resolved_plan_window_and_scope_admission",
        "release_pointer_and_runtime_authorization",
        "actual_select_job_and_digest_sql_parity",
    }
)
_SAFE_FAILURE_CODES = frozenset(
    {
        "copy_schema_invalid",
        "current_source_copy_invalid",
        "history_resolution_invalid",
        "physical_schema_invalid",
        "parent_source_unavailable",
        "protected_context_registry_invalid",
        "release_admission_invalid",
        "release_material_invalid",
        "snapshot_source_copy_binding_invalid",
        "source_validation_incomplete",
    }
)


def _failure(reason, *missing):
    return {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "coverage_gap"
        if reason in {"coverage_incomplete", "evidence_insufficient"}
        else "refused",
        "snapshot": None,
        "reason": reason,
        "missing_work": list(missing or (reason,)),
    }


def _receipt(value):
    try:
        raw = json.loads(value)
        raw["market_scope"] = tuple(raw["market_scope"])
        for field in ("signal_date", "observation_start", "observation_end"):
            raw[field] = date.fromisoformat(raw[field])
        raw["completed_at"] = datetime.fromisoformat(raw["completed_at"].replace("Z", "+00:00"))
        return build_run_receipt(**raw)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("snapshot_source_invalid") from error


def _source_run(material, run_id):
    metadata = material["run_metadata"][run_id]
    receipt = _receipt(metadata["receipt_json"])
    rows = {
        relation: tuple(row for row in material["records"][family] if row["run_id"] == run_id)
        for family, relation in _FAMILIES.items()
    }
    return receipt, rows


def _receipt_projection(fact, snapshot_id, citation_label, reading_ids):
    limitations = []
    if fact.get("collected_at") is None:
        limitations.append("Source collection time is unavailable in the released relation.")
    value = {
        "receipt_id": "gqr_" + fact["projection_sha256"],
        "citation_label": citation_label,
        "kind": "content",
        "snapshot_id": snapshot_id,
        "market": fact.get("market"),
        "source_label": fact.get("source_label") or fact["source_family"],
        "source_family": fact.get("source_family"),
        "platform": fact.get("platform"),
        "author": fact.get("author"),
        "url": fact.get("url"),
        "source_row_id": fact.get("row_id"),
        "published_at": fact["published_at"].isoformat(),
        "collected_at": None,
        "excerpt": fact.get("excerpt"),
        "reading_ids": list(reading_ids),
        "limitations": limitations,
        "content_digest": fact["projection_sha256"],
    }
    return value


_HISTORY_KIND = "history"
_HISTORY_STATES = frozenset({"analogue", "retained_analogue", "insufficient_history", "unresolved"})
# The history map in a stored snapshot's provenance is versioned. Version 1 is
# every snapshot sealed before the version existed: it carries no version key,
# an optional history map, and only the two historian states. Version 2 is
# what this builder writes: the key, a history map, and all four states. The
# version 1 name is a label for what a reader found and is never written.
_HISTORY_CONTRACT_V1 = "general_question_history_v1"
_HISTORY_CONTRACT_V2 = "general_question_history_v2"
# An unresolved or insufficient history reason is a refusal code or a gap
# name, never prose.
_HISTORY_REASON_CODE = re.compile(r"[a-z][a-z0-9_]*\Z")
_HISTORY_RESULT_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HISTORY_PROVENANCE_INSTANT_FIELDS = (
    "source_created_at",
    "source_first_observed_at",
    "source_last_observed_at",
    "selection_cutoff",
)
_HISTORY_STATES_BY_VERSION = {
    _HISTORY_CONTRACT_V1: frozenset({"analogue", "insufficient_history"}),
    _HISTORY_CONTRACT_V2: _HISTORY_STATES,
}
# The two covering states name their signal on different keys on purpose. The
# historian states carry signal_id, the current signal under investigation. The
# retained state carries matched_signal_id, the earlier signal its record was
# matched to. They are opposite ends of the same comparison, so they never share
# one key and a consumer cannot read one while meaning the other.
_HISTORY_COVERING_STATES = frozenset({"analogue", "retained_analogue"})
_HISTORY_RECORD_INSTANT_FIELDS = (
    "as_of",
    "observed_window_start",
    "observed_window_end",
)
_HISTORY_RECORD_DATE_FIELDS = ("historical_window_start", "historical_window_end")
_HISTORY_RECORD_SEQUENCE_FIELDS = (
    "source_families",
    "evidence_ids",
    "provenance_binding_ids",
    "difference_reasons",
    "limitations",
)
_HISTORY_PROVENANCE_FIELDS = (
    "historical_object_id",
    "source_created_at",
    "source_first_observed_at",
    "source_last_observed_at",
    "selection_cutoff",
    "evidence_ids",
    "signal_id",
    "snapshot_id",
    "result_digest",
)


def _planned_history_ids(plan):
    """The plan's history requirement ids, read the one way the slice reads them.

    There is a single reading of a plan's history requirements and the bridge
    owns it, so the worker that builds the resolver, this builder and the
    stored snapshot validator cannot disagree about which requirements a plan
    asks history for. A planned id that is not text, two requirements sharing
    an id and a requirement with no kind are each refused there; the refusal
    is translated here into this module's own history failure code, so it
    stays inside the safe failure code list rather than publishing a bridge
    internal code or falling through as a generic validation failure.
    """
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        planned_history_requirement_ids,
    )
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    try:
        return set(planned_history_requirement_ids(plan))
    except HistoricalBridgeError as error:
        raise ValueError("history_resolution_invalid") from error


def _unresolved_history_record(requirement_id, reason):
    return {"state": "unresolved", "requirement_id": requirement_id, "reason": reason}


def _history_record(answer):
    from src.analysis.open_intelligence.brain_historian import (
        HistoryRequirementAnswer,
        InsufficientHistory,
    )
    from src.analysis.open_intelligence.historical_evidence_bridge import (
        HistoryRecordAnswer,
        UnresolvedHistory,
    )

    if isinstance(answer, UnresolvedHistory):
        return _unresolved_history_record(answer.requirement_id, answer.reason)
    if isinstance(answer, HistoryRecordAnswer):
        return {
            "state": "retained_analogue",
            "requirement_id": answer.requirement_id,
            "matched_signal_id": answer.matched_signal_id,
            "as_of": answer.as_of.isoformat(),
            "record": _history_record_projection(answer.record),
        }
    record = {
        "requirement_id": answer.requirement_id,
        "signal_id": answer.signal_id,
        "as_of": answer.as_of.isoformat(),
    }
    if isinstance(answer, HistoryRequirementAnswer):
        provenance = {key: answer.provenance[key] for key in _HISTORY_PROVENANCE_FIELDS}
        provenance["evidence_ids"] = list(provenance["evidence_ids"])
        return {"state": "analogue", **record, "provenance": provenance}
    if isinstance(answer, InsufficientHistory):
        return {"state": "insufficient_history", **record, "reason": answer.reason}
    raise ValueError("history_resolution_invalid")


def _history_record_projection(record):
    """Project one admitted history record into the snapshot's plain values."""
    from dataclasses import fields

    value = {}
    for field in fields(record):
        item = getattr(record, field.name)
        if isinstance(item, (datetime, date)):
            value[field.name] = item.isoformat()
        elif isinstance(item, tuple):
            value[field.name] = list(item)
        else:
            value[field.name] = item
    return value


def _history_record_from_projection(value):
    """Rebuild an admitted history record from its stored projection.

    Rebuilding puts the stored values back through the record type, so a
    record edited without rehashing, one rehashed around a value the record
    type does not admit, and one rehashed around a field the historical
    receipt already fixed are each refused on read back.

    What this does not establish. The content digest is over the record's own
    values, so whoever holds the stored snapshot can recompute it; an edit
    that rehashes is not caught by the digest and is only caught by the
    enumerated values and the rederived receipt id. The record version, the
    contract version, the projection read receipt id and the availability are
    covered by neither and are read back as written. And the bridge capability
    that says a record was actually built from a read is not a field, so it is
    not in the stored projection and cannot be restored here; a record rebuilt
    from a snapshot is read, never presented as an admitted answer.
    """
    from dataclasses import fields

    from src.analysis.open_intelligence.historical_evidence_bridge import HistoryRecord

    # The stored snapshot is canonicalized with sorted keys, so the projection
    # is matched by its exact field set rather than by field order.
    names = {field.name for field in fields(HistoryRecord)}
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError("history_resolution_invalid")
    decoded = {}
    for field in fields(HistoryRecord):
        item = value[field.name]
        if field.name in _HISTORY_RECORD_INSTANT_FIELDS:
            decoded[field.name] = datetime.fromisoformat(item)
        elif field.name in _HISTORY_RECORD_DATE_FIELDS:
            decoded[field.name] = date.fromisoformat(item)
        elif field.name in _HISTORY_RECORD_SEQUENCE_FIELDS:
            if not isinstance(item, list) or any(not isinstance(entry, str) for entry in item):
                raise ValueError("history_resolution_invalid")
            decoded[field.name] = tuple(item)
        else:
            decoded[field.name] = item
    return HistoryRecord(**decoded)


def _history_records(plan, history_resolver):
    """Resolve every planned history requirement, never leaving one silent.

    The resolver is built where the admitted historical source is known and
    serves each requirement from retained evidence, without a model call. Its
    typed answers become plain records in the snapshot provenance: a retained
    analogue with its full admitted record, a historian analogue with its exact
    provenance, an explicit insufficient history reason, or an explicit
    unresolved reason.

    A planned requirement the resolver did not answer, and every planned
    requirement when no resolver was wired at all, is recorded as unresolved
    with the reason named. Answering nothing is a state the snapshot carries,
    not an absence a reader has to notice, because that absence was the defect.
    """
    planned = _planned_history_ids(plan)
    if not planned:
        return {}
    if history_resolver is None:
        return {
            requirement_id: _unresolved_history_record(requirement_id, "history_resolver_not_wired")
            for requirement_id in sorted(planned)
        }
    from src.analysis.open_intelligence.historical_provenance import HistoricalBridgeError

    try:
        resolved = history_resolver(plan)
    except HistoricalBridgeError as error:
        # The bridge error carries its own code, which the build reads straight
        # off the exception. Refusing here keeps the snapshot reason inside the
        # safe failure code list instead of publishing a bridge internal code.
        raise ValueError("history_resolution_invalid") from error
    if not isinstance(resolved, dict) or not set(resolved) <= planned:
        raise ValueError("history_resolution_invalid")
    records = {}
    for requirement_id in sorted(planned):
        if requirement_id not in resolved:
            records[requirement_id] = _unresolved_history_record(
                requirement_id, "history_requirement_unanswered"
            )
            continue
        record = _history_record(resolved[requirement_id])
        if record["requirement_id"] != requirement_id:
            raise ValueError("history_resolution_invalid")
        records[requirement_id] = record
    return records


def stored_history_contract_version(snapshot):
    """Name the history contract version a stored snapshot was sealed under.

    No version key is version 1, the shape sealed before the version existed.
    The current version is version 2. Any other value, including the version 1
    name written out, is no shape any writer produced and is refused rather
    than read as either.
    """
    provenance = snapshot["provenance"]
    if "history_contract_version" not in provenance:
        return _HISTORY_CONTRACT_V1
    if provenance["history_contract_version"] == _HISTORY_CONTRACT_V2:
        return _HISTORY_CONTRACT_V2
    raise ValueError("history_contract_version_unknown")


def _is_aware_instant(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _request_instant(request_as_of):
    """The request as of as an aware instant, the bound no history may pass."""
    try:
        bound = datetime.fromisoformat(request_as_of.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("history_resolution_invalid") from error
    if bound.tzinfo is None:
        raise ValueError("history_resolution_invalid")
    return bound


def _refuse_history_after_request(instants, bound):
    """Refuse a covering record that claims any instant later than the request.

    The record's own as of cannot bound it: it comes from the same payload as
    the instants it would bound, so a record dated wholly in the future agrees
    with itself. The request as of is held by the caller independently of the
    record, and a record observed or dated after the question was asked is
    not history the question could have had.

    Every instant here is already aware: the analogue's are checked to be,
    and a retained record's are digested as UTC instants, which the record
    type refuses to do for one without a timezone.
    """
    if any(instant > bound for instant in instants):
        raise ValueError("history_resolution_invalid")


def _validate_history_records(plan, records, version=_HISTORY_CONTRACT_V2, *, request_as_of=None):
    # Every record must answer a requirement this plan actually planned, and
    # nothing else. Under version 1 this is a subset and not an equality on
    # purpose. Every snapshot sealed before a resolver was wired carries an
    # empty history map with the requirement already listed as missing work,
    # and that shape is internally consistent: the coverage recomputation
    # below derives the same missing work from the same empty map and holds
    # the snapshot to it. Demanding a record for every planned requirement
    # there would refuse each of those stored snapshots on read back, which
    # reaches the answering stage and the dossier admission path, so an
    # already retrieved question would become unanswerable. The version 2
    # writer records every planned requirement, unresolved when nothing
    # answered it, so under version 2 a missing record is refused here: an
    # unresolved record dropped from the map changes no recomputed coverage.
    #
    # The request as of is the bound on every covering record. The reader and
    # the writer each pass the request they hold, so a record dated or
    # observed after the question was asked is refused on both paths. It is
    # optional only so the record checks can be exercised on their own.
    if not isinstance(records, dict):
        raise ValueError("history_resolution_invalid")
    bound = None if request_as_of is None else _request_instant(request_as_of)
    planned = _planned_history_ids(plan)
    if version == _HISTORY_CONTRACT_V2:
        answered_as_planned = set(records) == planned
    else:
        answered_as_planned = set(records) <= planned
    if not answered_as_planned:
        raise ValueError("history_resolution_invalid")
    for requirement_id, record in records.items():
        if (
            not isinstance(record, dict)
            or record.get("state") not in _HISTORY_STATES_BY_VERSION[version]
            or record.get("requirement_id") != requirement_id
        ):
            raise ValueError("history_resolution_invalid")
        if record["state"] == "unresolved":
            if set(record) != {"state", "requirement_id", "reason"} or (
                not isinstance(record["reason"], str)
                or _HISTORY_REASON_CODE.match(record["reason"]) is None
            ):
                raise ValueError("history_resolution_invalid")
            continue
        signal_key = "matched_signal_id" if record["state"] == "retained_analogue" else "signal_id"
        if (
            not isinstance(record.get(signal_key), str)
            or not record[signal_key]
            or not isinstance(record.get("as_of"), str)
        ):
            raise ValueError("history_resolution_invalid")
        datetime.fromisoformat(record["as_of"])
        if record["state"] == "retained_analogue":
            if set(record) != {
                "state",
                "requirement_id",
                "matched_signal_id",
                "as_of",
                "record",
            }:
                raise ValueError("history_resolution_invalid")
            try:
                rebuilt = _history_record_from_projection(record["record"])
            except (TypeError, ValueError, KeyError, AttributeError) as error:
                raise ValueError("history_resolution_invalid") from error
            if (
                rebuilt.requirement_id != requirement_id
                or rebuilt.source_object_id != record["matched_signal_id"]
                or rebuilt.as_of.isoformat() != record["as_of"]
            ):
                raise ValueError("history_resolution_invalid")
            if bound is not None:
                # The record type orders the observed window within itself but
                # not against the as of, so each instant is bounded on its own.
                _refuse_history_after_request(
                    (rebuilt.as_of, rebuilt.observed_window_start, rebuilt.observed_window_end),
                    bound,
                )
            continue
        if record["state"] == "analogue":
            provenance = record.get("provenance")
            if (
                set(record) != {"state", "requirement_id", "signal_id", "as_of", "provenance"}
                or not isinstance(provenance, dict)
                # The stored snapshot is canonical JSON with sorted keys, so the
                # provenance is matched by its key set, not the order written.
                or set(provenance) != set(_HISTORY_PROVENANCE_FIELDS)
            ):
                raise ValueError("history_resolution_invalid")
            # The result digest is over the historian result, which the
            # snapshot does not carry, so it cannot be recomputed here; it is
            # held to the digest's form, and each instant must parse.
            if (
                not isinstance(provenance["evidence_ids"], list)
                or any(not isinstance(item, str) for item in provenance["evidence_ids"])
                or any(
                    not isinstance(provenance[key], str) or not provenance[key]
                    for key in _HISTORY_PROVENANCE_FIELDS
                    if key != "evidence_ids"
                )
                or _HISTORY_RESULT_DIGEST.match(provenance["result_digest"]) is None
                or not all(
                    _is_aware_instant(provenance[key]) for key in _HISTORY_PROVENANCE_INSTANT_FIELDS
                )
                or not _is_aware_instant(record["as_of"])
            ):
                raise ValueError("history_resolution_invalid")
            # Two invariants every written analogue satisfies and that the
            # record alone recomputes. The writer copies one signal id into
            # the record and its provenance. And the historian admits an
            # analogue only when nothing it observed is later than the
            # selection cutoff and the cutoff is earlier than the as of
            # (brain_historian.py). Neither binds the record to a real read.
            created, first, last, cutoff = (
                datetime.fromisoformat(provenance[key])
                for key in _HISTORY_PROVENANCE_INSTANT_FIELDS
            )
            if (
                provenance["signal_id"] != record["signal_id"]
                or max(created, first, last) > cutoff
                or cutoff >= datetime.fromisoformat(record["as_of"])
            ):
                raise ValueError("history_resolution_invalid")
            if bound is not None:
                # With the ordering above, the as of bounds the rest; they are
                # named too so the bound does not rest on that ordering alone.
                _refuse_history_after_request(
                    (datetime.fromisoformat(record["as_of"]), created, first, last, cutoff), bound
                )
        elif set(record) != {"state", "requirement_id", "signal_id", "as_of", "reason"} or (
            not isinstance(record["reason"], str)
            or _HISTORY_REASON_CODE.match(record["reason"]) is None
        ):
            raise ValueError("history_resolution_invalid")


def _covered_history_ids(history):
    return sorted(
        requirement_id
        for requirement_id, record in (history or {}).items()
        if record.get("state") in _HISTORY_COVERING_STATES
    )


def _requirement_coverage(requirements, facts, history=None):
    coverage = {}
    missing = []
    covered_history = set(_covered_history_ids(history))
    for requirement in requirements:
        requirement_id = requirement["requirement_id"]
        if requirement["kind"] == _HISTORY_KIND:
            if requirement_id not in covered_history and requirement["mandatory"]:
                missing.append(requirement_id)
            continue
        if requirement["kind"] != "content":
            if requirement["mandatory"]:
                missing.append(requirement_id)
            continue
        terms = tuple(term.strip().casefold() for term in requirement["search_terms"])
        matched = []
        for index, fact in enumerate(facts):
            haystack = " ".join(
                value.casefold()
                for value in (fact.get("source_label"), fact.get("excerpt"))
                if type(value) is str
            )
            if not terms or any(term in haystack for term in terms):
                matched.append(index)
        if matched:
            coverage[requirement_id] = tuple(matched)
        elif requirement["mandatory"]:
            missing.append(requirement_id)
    return coverage, missing


def _covered_material(material, coverage):
    retained = sorted({index for matched in coverage.values() for index in matched})
    positions = {original: current for current, original in enumerate(retained)}
    result = copy.deepcopy(material)
    result["selected_facts"] = [material["selected_facts"][index] for index in retained]
    remapped = {
        requirement_id: tuple(positions[index] for index in matched)
        for requirement_id, matched in coverage.items()
    }
    return result, remapped


def _validate_fact_scope(facts, plan, as_of):
    try:
        start = datetime.combine(date.fromisoformat(plan["window"]["start"]), datetime.min.time())
        end = datetime.combine(
            date.fromisoformat(plan["window"]["end"]) + timedelta(days=1), datetime.min.time()
        )
        cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        start = start.replace(tzinfo=cutoff.tzinfo)
        end = end.replace(tzinfo=cutoff.tzinfo)
        if any(
            fact.get("market") not in plan["markets"]
            or not isinstance(fact.get("published_at"), datetime)
            or fact["published_at"].tzinfo is None
            or not start <= fact["published_at"] < end
            or fact["published_at"] > cutoff
            for fact in facts
        ):
            raise ValueError()
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise ValueError("snapshot_scope_invalid") from error


def _validate_selected_copy_binding(material, copy_proof):
    try:
        copy_run_id = copy_proof["copy_run_id"]
        if type(copy_run_id) is not str or not copy_run_id:
            raise ValueError()
        for metadata in material["run_metadata"].values():
            observed = {
                row["copy_run_id"]
                for field in ("manifest", "copy_receipts")
                for row in metadata[field]
            }
            if observed != {copy_run_id}:
                raise ValueError()
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise ValueError("snapshot_source_copy_binding_invalid") from error


def _coverage_gaps(plan, facts, receipts):
    missing = []
    limitations = []
    observed_markets = {fact["market"] for fact in facts}
    for market in plan["markets"]:
        if market not in observed_markets:
            missing.append(f"market:{market}")
            limitations.append(f"No admitted selected facts cover planned market {market}.")
    if plan["window"]["closed"]:
        start = date.fromisoformat(plan["window"]["start"])
        end = date.fromisoformat(plan["window"]["end"])
        relevant_runs = {fact["source_run_id"] for fact in facts}
        covered = {
            day
            for receipt in receipts
            if receipt.run_id in relevant_runs
            for offset in range((receipt.observation_end - receipt.observation_start).days + 1)
            for day in (receipt.observation_start + timedelta(days=offset),)
        }
        uncovered = [
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if start + timedelta(days=offset) not in covered
        ]
        ranges = []
        for day in uncovered:
            if not ranges or day != ranges[-1][1] + timedelta(days=1):
                ranges.append([day, day])
            else:
                ranges[-1][1] = day
        for first, last in ranges:
            missing.append(f"window:{first.isoformat()}/{last.isoformat()}")
            limitations.append(
                "Admitted run coverage does not cover planned dates "
                f"{first.isoformat()} through {last.isoformat()}."
            )
    return missing, limitations


def _bound_query_capture(capture, query, query_record):
    if type(capture) is not dict or set(capture) != {"rows", "receipt"}:
        raise ValueError("snapshot_provenance_invalid")
    rows, receipt = capture["rows"], capture["receipt"]
    if (
        type(rows) is not list
        or type(receipt) is not dict
        or type(query_record) is not dict
        or set(query_record) != {"reservation", "receipt"}
        or query_record["receipt"] != receipt
    ):
        raise ValueError("snapshot_provenance_invalid")
    reservation = query_record["reservation"]
    expected = {
        "template_id": query.template_id,
        "sql_digest": query.sql_digest,
        "parameters_digest": query.parameters_digest,
        "query_state": "succeeded",
        "result_digest": canonical_digest(rows),
    }
    if any(receipt.get(field) != value for field, value in expected.items()):
        raise ValueError("snapshot_provenance_invalid")
    if any(
        reservation.get(field) != value
        for field, value in (
            ("template_id", query.template_id),
            ("sql_digest", query.sql_digest),
            ("parameters_digest", query.parameters_digest),
            ("candidate_limit", query.candidate_limit),
        )
    ):
        raise ValueError("snapshot_provenance_invalid")
    return rows


def _project_snapshot(
    context,
    plan,
    material,
    schemas,
    admissions,
    provenance,
    coverage,
    missing_work,
    coverage_limitations,
    history=None,
):
    facts = material["selected_facts"]
    seed = {
        "request_digest": context["request"]["request_digest"],
        "plan_digest": plan["plan_digest"],
        "run_ids": [admission.run_id for admission in admissions],
        "fact_digests": [fact["projection_sha256"] for fact in facts],
        "source_schema_digest": schemas["source"]["metadata_digest"],
        "copy_schema_digest": schemas["copy"]["metadata_digest"],
        "discovery_result_digest": provenance["discovery"]["receipt"]["result_digest"],
        "source_result_digest": provenance["source"]["receipt"]["result_digest"],
        "release_result_digests": [
            release["receipt"]["result_digest"] for release in provenance["releases"]
        ],
        "copy_result_digest": provenance["current_copy"]["receipt"]["result_digest"],
    }
    snapshot_id = "gqs_" + canonical_digest(seed)
    reading_ids = {
        requirement_id: "gqrd_"
        + canonical_digest(
            {
                "snapshot_id": snapshot_id,
                "method": "selected_receipt_count",
                "requirement_id": requirement_id,
            }
        )
        for requirement_id in coverage
    }
    fact_readings = {
        index: [
            reading_ids[requirement_id]
            for requirement_id, matched in coverage.items()
            if index in matched
        ]
        for index in range(len(facts))
    }
    receipts = [
        _receipt_projection(fact, snapshot_id, f"R{index + 1}", fact_readings[index])
        for index, fact in enumerate(facts)
    ]
    readings = [
        {
            "reading_id": reading_ids[requirement_id],
            "value": len(matched),
            "unit": "records",
            "window": copy.deepcopy(plan["window"]),
            "method": "selected_receipt_count",
            "denominator": None,
            "source_receipt_ids": [receipts[index]["receipt_id"] for index in matched],
            "limitations": ["This selected-record count is not population prevalence."],
        }
        for requirement_id, matched in coverage.items()
    ]
    snapshot = {
        "contract_version": "general_question_snapshot_v1",
        "request_id": context["request"]["request_id"],
        "request_digest": context["request"]["request_digest"],
        "intake_digest": context["intake"]["intake_digest"],
        "plan_digest": plan["plan_digest"],
        "policy_digest": context["admission"]["policy_digest"],
        "deployment_digest": context["admission"]["deployment_digest"],
        "snapshot_id": snapshot_id,
        "as_of": context["request"]["as_of"],
        "window": copy.deepcopy(plan["window"]),
        "receipts": receipts,
        "readings": readings,
        "limitations": [
            *sorted({limitation for receipt in receipts for limitation in receipt["limitations"]}),
            *coverage_limitations,
        ],
        "missing_work": list(missing_work),
        "fulfilled_requirement_ids": sorted([*coverage, *_covered_history_ids(history)]),
        "provenance": copy.deepcopy(provenance),
    }
    snapshot = json.loads(canonical_bytes(snapshot))
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    return snapshot


def _project_context_snapshot(context, plan, admission, provenance):
    value = {
        "contract_version": "general_question_snapshot_v1",
        "request_id": context["request"]["request_id"],
        "request_digest": context["request"]["request_digest"],
        "intake_digest": context["intake"]["intake_digest"],
        "plan_digest": plan["plan_digest"],
        "policy_digest": context["admission"]["policy_digest"],
        "deployment_digest": context["admission"]["deployment_digest"],
        "snapshot_id": admission["snapshot_id"],
        "as_of": context["request"]["as_of"],
        "window": plan["window"],
        "receipts": admission["receipts"],
        "readings": admission["readings"],
        "limitations": admission["limitations"],
        "missing_work": [
            item["requirement_id"]
            for item in plan["requirements"]
            if item["mandatory"]
            and item["requirement_id"] not in admission["fulfilled_requirement_ids"]
        ],
        "fulfilled_requirement_ids": admission["fulfilled_requirement_ids"],
        "provenance": provenance,
    }
    if provenance.get("parent_context") is not None:
        from src.analysis.open_intelligence.general_question_parent_capsule import (
            select_parent_aliases,
        )

        refs = select_parent_aliases(provenance["parent_context"], plan["parent_receipt_aliases"])
        admitted = {
            (row["market"], row["source_row_id"], row["content_digest"])
            for row in admission["receipts"]
        }
        if any(
            (row["market"], row["source_row_id"], row["content_digest"]) not in admitted
            for row in refs
        ):
            value["missing_work"].append("parent_source_unavailable")
    if "window_comparison" in provenance:
        from src.analysis.open_intelligence.general_question_window_comparison import (
            apply_window_comparison,
        )

        apply_window_comparison(value, provenance["window_comparison"], plan)
    value = json.loads(canonical_bytes(value))
    value["snapshot_digest"] = canonical_digest(value)
    return value


def _identity_sidecar(admission, source_binding):
    """Sidecar metadata: the derived groups bound to the source and the identity policy.

    The admission's own contract version chooses the sidecar shape, so a retained v2
    admission rebuilds the v2 sidecar it was written with and a v3 admission rebuilds the
    v3 one. An admission whose projection says origin authority was never projected while
    its derived groups name origins contradicts itself, and the two cannot both be read,
    so it refuses rather than publishing a count beside a caveat denying it.
    """
    version = admission.get("contract_version") if type(admission) is dict else None
    if version not in _SIDECAR_VERSIONS:
        raise ValueError("context_identity_invalid")
    contract, fields = _SIDECAR_VERSIONS[version]
    if any(field not in admission for field in fields):
        raise ValueError("context_identity_invalid")
    projection = admission.get("origin_authority_projection")
    if (
        type(projection) is dict
        and projection.get("state") != "projected"
        and admission["origin_groups"]
    ):
        raise ValueError("context_identity_invalid")
    value = {
        "contract_version": contract,
        "profile_id": source_binding["profile_id"],
        "source_binding_digest": source_binding["source_binding_digest"],
        "source_snapshot_digest": source_binding["snapshot_digest"],
        "cutoff_date": source_binding["cutoff_date"],
        "snapshot_id": admission["snapshot_id"],
        **{field: copy.deepcopy(admission[field]) for field in fields},
    }
    value["sidecar_digest"] = canonical_digest(value)
    return value


EVIDENCE_SUPPORT_VERSION = "general_question_evidence_support_v1"


def _support_records(receipts, requested):
    """Select the cited receipts and project the producer identity fields they carry."""
    known = {receipt["receipt_id"]: receipt for receipt in receipts}
    selected = list(requested) if type(requested) is list else []
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(value not in known for value in selected)
    ):
        raise ValueError("context_support_request_invalid")
    return [
        {
            "receipt_id": receipt_id,
            "market": known[receipt_id]["market"],
            "platform": known[receipt_id]["platform"],
            "source_row_id": known[receipt_id]["source_row_id"],
            "source_family": known[receipt_id]["source_family"],
        }
        for receipt_id in selected
    ]


def _support_limitations(support, projection):
    units = support["units"]
    lines = [
        f"Cited evidence names its units: {units['collected_records']} collected records, "
        f"{units['unique_observations']} unique observations, {units['source_families']} source "
        f"families, {units['verified_independent_origins']} verified independent origins; "
        f"{units['unknown_origins']} cited observations carry no verified origin. None of these "
        "counts is population prevalence."
    ]
    if units["verified_independent_origins"] == 0 and projection.get("state") != "projected":
        lines.append(
            "Origin authority is not projected for these records, so zero verified "
            "independent origins is an absent projection and not a measured zero."
        )
    shared = [
        slot
        for slot in support["slots"]
        if slot["independence"] == "verified_origin" and len(slot["receipt_ids"]) > 1
    ]
    lines.extend(
        "Records sharing one verified origin fill a single independent support slot: "
        f"{slot['origin_id']} covers {len(slot['receipt_ids'])} cited records."
        for slot in shared
    )
    if support["unknown_support_slots"]:
        lines.append(
            f"Independence is unknown for {support['unknown_support_slots']} cited observations; "
            "an unverified origin is never independent support."
        )
    return lines


def read_context_evidence_support(snapshot, receipt_ids):
    """Read identity, shared origin and independence for the cited receipts of one snapshot.

    The answer cites receipts, so independence has to be decidable for that cited set, and
    the cited set is an argument rather than a default over every retained receipt. The
    derived groups come from the snapshot's own identity sidecar. The sidecar's own digest
    would only prove the sidecar is self consistent, so a caller re-signing it could turn
    unknown observations into a verified origin; the reader instead binds the snapshot to
    its own digest and rebuilds the sidecar from the admission and source binding the
    snapshot carries, so re-signing the sidecar alone no longer buys a verified origin. The
    rebuild's inputs are members of the same payload, so this binds the record together and
    is not on its own a check against anything outside it: replaying the query bindings
    against the receipts held in the store control record is that check, and it is the outer
    gate that stays with the stored snapshot validator. Receipts sharing one verified origin
    fill one support slot, and an observation without established origin proof stays unknown
    rather than independent. A snapshot with no identity record refuses instead of reporting
    independence, and a record written before the origin authority projection existed reads
    that projection as never recorded rather than as a measured zero.
    """
    provenance = snapshot.get("provenance") if type(snapshot) is dict else None
    if (
        type(provenance) is not dict
        or provenance.get("profile") not in _IDENTITY_PROFILES
        or type(provenance.get("identity_sidecar")) is not dict
    ):
        raise ValueError("context_identity_unavailable")
    if snapshot.get("snapshot_digest") != canonical_digest(
        {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
    ):
        raise ValueError("context_identity_invalid")
    sidecar = provenance["identity_sidecar"]
    try:
        rebuilt = json.loads(
            canonical_bytes(
                _identity_sidecar(provenance["admission"], provenance["source_binding"])
            )
        )
    except (TypeError, ValueError, KeyError, IndexError) as error:
        raise ValueError("context_identity_invalid") from error
    if sidecar != rebuilt or sidecar.get("snapshot_id") != snapshot.get("snapshot_id"):
        raise ValueError("context_identity_invalid")
    records = _support_records(snapshot["receipts"], receipt_ids)
    try:
        support = support_slots_from_groups(
            records, sidecar["observation_keys"], sidecar["origin_groups"]
        )
    except ValueError as error:
        raise ValueError("context_identity_invalid") from error
    value = {
        "contract_version": EVIDENCE_SUPPORT_VERSION,
        "snapshot_id": snapshot["snapshot_id"],
        "identity_policy_digest": sidecar["identity_policy_digest"],
        "sidecar_digest": sidecar["sidecar_digest"],
        "origin_authority_projection": copy.deepcopy(
            sidecar.get("origin_authority_projection", _PROJECTION_NOT_RECORDED)
        ),
        "slots": [
            {**slot, "observation_keys": [list(key) for key in slot["observation_keys"]]}
            for slot in support["slots"]
        ],
        "independent_support_slots": support["independent_support_slots"],
        "unknown_support_slots": support["unknown_support_slots"],
        "units": support["units"],
    }
    value["limitations"] = _support_limitations(support, value["origin_authority_projection"])
    return value


_CURRENT_CONTEXT_CAP_VERSION = 2
# Retained activation receipts bind these deployments to unversioned version two caps.
# Reconcile intervening activations before deploying; never add the versioned writer.
_UNVERSIONED_CAP_DEPLOYMENTS = MappingProxyType(
    {
        "a827a412da16f7b9898bb24e1f6ce9db0560d66399cb97804fdc713b5405b744": 2,
        "2bada61bbae78a1156b9c2c051e499500bff2c89a06de99d09e2e484c17476be": 2,
        "437a07c527f15bd3c930b848e6fe4f79bc8e6c57b026544fe0cf221d09f8aac6": 2,
    }
)
_CONTEXT_CAP_SETS = MappingProxyType(
    {
        1: MappingProxyType(
            {
                "keyed": MappingProxyType(
                    {
                        "authority": 35_000_000,
                        "candidate": 35_000_000,
                        "enriched_index": 100_000_000,
                        "enriched": 300_000_000,
                        "raw_index": 180_000_000,
                        "raw": 300_000_000,
                    }
                ),
                "direct": MappingProxyType(
                    {"partitions": 60_000_000, "enriched": 275_000_000, "raw": 275_000_000}
                ),
            }
        ),
        2: MappingProxyType(
            {
                "keyed": MappingProxyType(
                    {
                        "authority": 35_000_000,
                        "candidate": 35_000_000,
                        "enriched_index": 200_000_000,
                        "enriched": 195_000_000,
                        "raw_index": 300_000_000,
                        "raw": 195_000_000,
                    }
                ),
                "direct": MappingProxyType(
                    {"partitions": 60_000_000, "enriched": 170_000_000, "raw": 165_000_000}
                ),
            }
        ),
    }
)


def _context_cap_set(cap_version):
    if type(cap_version) is not int or cap_version not in _CONTEXT_CAP_SETS:
        raise ValueError("snapshot_cap_version_unknown")
    return _CONTEXT_CAP_SETS[cap_version]


def _stored_context_cap_version(value, deployment_digest, *, allow_unversioned):
    if "cap_version" in value:
        version = value["cap_version"]
    elif allow_unversioned:
        version = _UNVERSIONED_CAP_DEPLOYMENTS.get(deployment_digest)
    else:
        version = None
    _context_cap_set(version)
    return version


def _context_query_caps(discovery_bytes, source_bytes, metadata_bytes, *, cap_version=None):
    version = _CURRENT_CONTEXT_CAP_VERSION if cap_version is None else cap_version
    ceilings = _context_cap_set(version)["keyed"]
    allowances = {"authority": metadata_bytes, "candidate": discovery_bytes}
    return {
        lane: min(allowances.get(lane, source_bytes), ceiling) for lane, ceiling in ceilings.items()
    }


def _direct_context_query_caps(query_caps, *, cap_version=None):
    version = _CURRENT_CONTEXT_CAP_VERSION if cap_version is None else cap_version
    ceilings = _context_cap_set(version)["direct"]
    return {
        **query_caps,
        "partitions": ceilings["partitions"],
        "enriched": min(query_caps["enriched"], ceilings["enriched"]),
        "raw": min(query_caps["raw"], ceilings["raw"]),
    }


def _context_ancestors(recovery):
    from scripts.staging.capture_protected_production_snapshot import _validate_recovery

    if recovery is None:
        return ()
    _validate_recovery(recovery, "recover")
    contexts = [recovery]
    if recovery["contract_version"] in {
        "open_intelligence_source_capture_recovery_v3",
        "open_intelligence_source_capture_recovery_v4",
    }:
        contexts.append(recovery["ancestor_recovery_context"])
    if recovery["contract_version"] == "open_intelligence_source_capture_recovery_v4":
        contexts.append(recovery["ancestor_recovery_context"]["ancestor_recovery_context"])
    for field in ("initial_consumption_id", "initial_manifest_sha256", "initial_result_id"):
        if len({context[field] for context in contexts}) != len(contexts):
            raise ValueError("context_authority_invalid")
    return tuple(contexts)


def _build_context_snapshot(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    context,
    candidate_limit,
    evidence_limit,
    query_caps,
    stage_now,
    bridge_clients=None,
):
    cap_version = _CURRENT_CONTEXT_CAP_VERSION
    from google.cloud import bigquery, storage

    from src.analysis.open_intelligence.general_question_context_admission import (
        admit_protected_context,
        load_protected_context_source,
    )
    from src.analysis.open_intelligence.general_question_parent_capsule import select_parent_aliases
    from src.analysis.open_intelligence.general_question_parent_context import read_parent_context
    from src.analysis.open_intelligence.general_question_queries import GeneralQuestionQueries
    from src.analysis.open_intelligence.general_question_query_execution import (
        execute_context_approval_query,
        execute_context_candidate_query,
        execute_context_evidence_query,
        execute_context_index_query,
        execute_context_partition_query,
        execute_context_result_batch_query,
        execute_context_result_query,
    )
    from src.analysis.open_intelligence.production_snapshot_storage import (
        BUCKET_NAME,
        SourceCaptureObjects,
    )
    from src.analysis.open_intelligence.protected_context_registry import (
        _PROTECTED_CONTEXT_PROFILES,
    )

    parent_context = read_parent_context(store, context["request"], context["intake"])
    parent_refs = (
        select_parent_aliases(parent_context, plan["parent_receipt_aliases"])
        if parent_context is not None
        else []
    )
    profiles = sorted(
        _PROTECTED_CONTEXT_PROFILES,
        key=lambda value: (value["cutoff_date"], value["profile_id"]),
        reverse=True,
    )
    end = plan["window"]["end"]
    profiles = [value for value in profiles if end <= value["cutoff_date"]]
    if not profiles:
        # No v1 profile covers the window: the bridge capture is the only other source.
        bridged = _build_bridge_context_snapshot(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            plan=plan,
            context=context,
            candidate_limit=candidate_limit,
            evidence_limit=evidence_limit,
            query_caps=query_caps,
            stage_now=stage_now,
            parent_context=parent_context,
            bridge_clients=bridge_clients,
            cap_version=cap_version,
        )
        from src.analysis.open_intelligence.general_question_window_comparison import (
            HISTORICAL_WINDOW_UNCOVERED,
            comparison_requirement_ids,
            history_sources_enabled,
            uncovered_window_line,
        )
        from src.analysis.open_intelligence.protected_context_registry import bridge_entries

        if (
            bridged == _failure("coverage_incomplete", "bridge_source_uncovered")
            and history_sources_enabled()
            and comparison_requirement_ids(plan)
        ):
            # A comparison whose window runs past every pinned capture names that cutoff;
            # the part no capture read is refused, never counted as zero.
            return _failure(
                "coverage_incomplete",
                "bridge_source_uncovered",
                HISTORICAL_WINDOW_UNCOVERED,
                uncovered_window_line(
                    plan,
                    [
                        *(value["cutoff_date"] for value in _PROTECTED_CONTEXT_PROFILES),
                        *(entry.cutoff_date for entry in bridge_entries() if entry.pinned),
                    ],
                ),
            )
        return bridged
    query_ledger = GeneralQuestionQueries(store)
    ordinal = 0
    authority_captures = []
    approvals = {}
    results_cache = {}
    ancestor_ids = None

    def record(number):
        value = query_ledger.read_query(invocation["request_id"], scope=scope, ordinal=number)
        if value is None:
            raise ValueError("context_query_invalid")
        return value

    def result_reader(consumption_id):
        nonlocal ordinal, ancestor_ids
        if consumption_id in results_cache:
            return (copy.deepcopy(results_cache[consumption_id]),)
        if results_cache:
            if ancestor_ids is None:
                current_approval = approvals[profiles[0]["manifest_sha256"]]
                manifest = json.loads(current_approval.canonical_manifest_json)
                digests = [
                    item["sha256"]
                    for item in manifest["input_artifacts"]
                    if item["name"] == "recovery_context"
                ]
                if len(digests) != 1:
                    raise ValueError("context_authority_invalid")
                raw = objects.read_input(
                    "recovery_context",
                    digests[0],
                    timeout=min(
                        30,
                        (
                            control_time(context["admission"]["deadline_at"]) - stage_now()
                        ).total_seconds(),
                    ),
                )
                recovery = json.loads(raw)
                if canonical_bytes(recovery) != raw or canonical_digest(recovery) != digests[0]:
                    raise ValueError("context_authority_invalid")
                ancestor_ids = tuple(
                    sorted(
                        context["initial_consumption_id"]
                        for context in _context_ancestors(recovery)
                    )
                )
                if profiles[0]["consumption_id"] in ancestor_ids:
                    raise ValueError("context_authority_invalid")
            if consumption_id not in ancestor_ids:
                raise ValueError("context_authority_invalid")
            if len(ancestor_ids) > 1:
                ordinal += 1
                batch = execute_context_result_batch_query(
                    invocation,
                    store=store,
                    scope=scope,
                    runtime_identity=runtime_identity,
                    credentials=credentials,
                    plan=plan,
                    consumption_ids=ancestor_ids,
                    ordinal=ordinal,
                    maximum_bytes_billed=query_caps["authority"],
                    cap_version=cap_version,
                    now=stage_now(),
                )
                if batch.get("application_status") != "accepted" or set(batch["material"]) != set(
                    ancestor_ids
                ):
                    raise ValueError("context_authority_invalid")
                for identifier, material in batch["material"].items():
                    results_cache[identifier] = copy.deepcopy(material["result"])
                    approvals[material["approval"].manifest_sha256] = material["approval"]
                authority_captures.append(
                    {
                        "kind": "results",
                        "value": list(ancestor_ids),
                        "rows": batch["rows"],
                        "receipt": batch["receipt"],
                        "ordinal": ordinal,
                    }
                )
                return (copy.deepcopy(results_cache[consumption_id]),)
        elif consumption_id != profiles[0]["consumption_id"]:
            raise ValueError("context_authority_invalid")
        ordinal += 1
        result = execute_context_result_query(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            plan=plan,
            consumption_id=consumption_id,
            ordinal=ordinal,
            maximum_bytes_billed=query_caps["authority"],
            cap_version=cap_version,
            now=stage_now(),
        )
        if result.get("application_status") != "accepted":
            raise ValueError("context_authority_invalid")
        material = result["material"]
        results_cache[consumption_id] = copy.deepcopy(material["result"])
        approvals[material["approval"].manifest_sha256] = material["approval"]
        authority_captures.append(
            {
                "kind": "result",
                "value": consumption_id,
                "rows": result["rows"],
                "receipt": result["receipt"],
                "ordinal": ordinal,
            }
        )
        return (material["result"],)

    def approval_reader(manifest_sha256):
        nonlocal ordinal
        if manifest_sha256 in approvals:
            return approvals[manifest_sha256]
        ordinal += 1
        result = execute_context_approval_query(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            plan=plan,
            manifest_sha256=manifest_sha256,
            ordinal=ordinal,
            maximum_bytes_billed=query_caps["authority"],
            cap_version=cap_version,
            now=stage_now(),
        )
        if result.get("application_status") != "accepted":
            raise ValueError("context_authority_invalid")
        approvals[manifest_sha256] = result["material"]
        authority_captures.append(
            {
                "kind": "approval",
                "value": manifest_sha256,
                "rows": result["rows"],
                "receipt": result["receipt"],
                "ordinal": ordinal,
            }
        )
        return result["material"]

    native_client = bigquery.Client(
        project="ogilvy-trends-v2", location="US", credentials=credentials
    )
    object_client = None
    try:
        object_client = storage.Client(project="ogilvy-trends-v2", credentials=credentials)
        objects = SourceCaptureObjects(object_client.bucket(BUCKET_NAME))
        source = load_protected_context_source(
            context["request"],
            plan,
            context["intake"],
            objects=objects,
            native_client=native_client,
            result_reader=result_reader,
            approval_reader=approval_reader,
            now=stage_now(),
        )
        ordinal += 1
        candidate = execute_context_candidate_query(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            plan=plan,
            source_binding=source.source_binding,
            ordinal=ordinal,
            candidate_limit=candidate_limit,
            maximum_bytes_billed=query_caps["candidate"],
            cap_version=cap_version,
            now=stage_now(),
        )
        if candidate.get("application_status") != "accepted":
            return _failure(candidate.get("reason") or "context_candidate_unavailable")
        results = {"candidate": candidate}
        terms = [
            term
            for requirement in _context_requirements(plan)
            for term in requirement["search_terms"]
        ]
        direct = bool(terms) or bool(parent_refs)
        supplement = direct and (
            parent_context is not None
            or any(row["sample_row_ids"] for row in candidate["material"]["records"])
        )
        effective_evidence_limit = (
            min(evidence_limit, SUPPLEMENT_EVIDENCE_LIMIT) if supplement else evidence_limit
        )
        if direct:
            query_caps = _direct_context_query_caps(query_caps, cap_version=cap_version)
            ordinal += 1
            partitions = execute_context_partition_query(
                invocation,
                store=store,
                scope=scope,
                runtime_identity=runtime_identity,
                credentials=credentials,
                plan=plan,
                source_binding=source.source_binding,
                candidate_rows=candidate["rows"],
                ordinal=ordinal,
                maximum_bytes_billed=query_caps["partitions"],
                cap_version=cap_version,
                now=stage_now(),
            )
            if partitions.get("application_status") != "accepted":
                return _failure(partitions.get("reason") or "context_partitions_unavailable")
            results["partitions"] = partitions
        raw_parent_reserve = len(
            {
                (row["market"], row["source_row_id"])
                for row in parent_refs
                if row["lane"] == "raw_content"
            }
        )
        enriched_limit = effective_evidence_limit - raw_parent_reserve
        remaining = enriched_limit
        for key, lane in (("enriched", "enriched_content"), ("raw", "raw_content")):
            fallback = (
                {
                    "enriched_rows": results["enriched"]["rows"],
                    "enriched_index_rows": results["enriched_index"]["rows"],
                }
                if key == "raw"
                else {}
            )
            common = dict(
                store=store,
                scope=scope,
                runtime_identity=runtime_identity,
                credentials=credentials,
                plan=plan,
                source_binding=source.source_binding,
                candidate_rows=candidate["rows"],
                **({"partition_rows": results["partitions"]["rows"]} if direct else {}),
                geo_policy=direct,
                parent_context=parent_context,
                continuity_policy=direct,
                lane=lane,
                candidate_limit=remaining,
                **fallback,
            )
            ordinal += 1
            index = execute_context_index_query(
                invocation,
                ordinal=ordinal,
                maximum_bytes_billed=query_caps[key + "_index"],
                cap_version=cap_version,
                now=stage_now(),
                **common,
            )
            if index.get("application_status") != "accepted":
                return _failure(index.get("reason") or "context_index_unavailable")
            results[key + "_index"] = index
            ordinal += 1
            evidence = execute_context_evidence_query(
                invocation,
                ordinal=ordinal,
                maximum_bytes_billed=query_caps[key],
                cap_version=cap_version,
                index_rows=index["rows"],
                now=stage_now(),
                **common,
            )
            if evidence.get("application_status") != "accepted":
                return _failure(evidence.get("reason") or "context_evidence_unavailable")
            results[key] = evidence
            if key == "enriched":
                remaining = effective_evidence_limit - evidence["material"]["candidate_count"]
        captures = {
            key: {"rows": value["rows"], "receipt": value["receipt"]}
            for key, value in results.items()
        }
        queries = {key: value["prepared_query"] for key, value in results.items()}
        query_records = {
            key: record(number)
            for key, number in zip(
                results, range(ordinal - len(results) + 1, ordinal + 1), strict=True
            )
        }
        from src.analysis.open_intelligence.general_question_context_admission import (
            ForeignLocalEvidenceOnly,
            NoMatchingContextEvidence,
        )

        try:
            admission = admit_protected_context(
                context["request"],
                plan,
                context["intake"],
                source=source,
                queries=queries,
                captures=captures,
                query_records=query_records,
            )
        except ForeignLocalEvidenceOnly:
            return _failure("coverage_incomplete", "corroborated_foreign_local_only")
        except NoMatchingContextEvidence:
            return _failure("evidence_insufficient", "no_matching_evidence")
        provenance = {
            "profile": _CURRENT_CONTEXT_PROFILE,
            "cap_version": cap_version,
            "source_binding": copy.deepcopy(source.source_binding),
            "authority_captures": authority_captures,
            "captures": copy.deepcopy(captures),
            "admission": copy.deepcopy(admission),
            "identity_sidecar": _identity_sidecar(admission, source.source_binding),
            "limits": {
                "query_caps": copy.deepcopy(query_caps),
                "candidate_limit": candidate_limit,
                "evidence_limit": evidence_limit,
                **({"effective_evidence_limit": effective_evidence_limit} if supplement else {}),
                "enriched_limit": enriched_limit,
                "raw_limit": remaining,
            },
            **(
                {"parent_context": copy.deepcopy(parent_context)}
                if parent_context is not None
                else {}
            ),
        }
        from src.analysis.open_intelligence.general_question_window_comparison import (
            build_window_comparison,
            comparison_requirement_ids,
            history_sources_enabled,
        )

        if history_sources_enabled() and comparison_requirement_ids(plan):
            from src.analysis.open_intelligence.general_question_context_admission import (
                context_selection_caps,
            )

            # Read from the admission this one context read produced: no query, byte or
            # model call is added for the comparison.
            selection = context_selection_caps(
                admission,
                request=context["request"],
                plan=plan,
                intake=context["intake"],
                queries=queries,
                captures=captures,
                query_records=query_records,
            )
            provenance["window_comparison"] = build_window_comparison(
                plan, admission["receipts"], source.source_binding, selection
            )
        snapshot = _project_context_snapshot(context, plan, admission, provenance)
        key = f"requests/{snapshot['request_id']}/snapshot.json"
        stage_now()
        stored = store._objects.create(key, snapshot)
        readback = store._objects.read(key, generation=stored.generation)
        if readback is None or readback.value != snapshot:
            return _failure("snapshot_readback_invalid")
        return {
            "contract_version": "general_question_snapshot_build_v1",
            "status": "admitted",
            "snapshot": copy.deepcopy(snapshot),
            "reason": None,
            "missing_work": [],
        }
    finally:
        if object_client is not None:
            object_client.close()
        native_client.close()


def _validate_stored_context_snapshot(snapshot, request, intake, plan, query_records):
    from src.analysis.open_intelligence.execution_approval import validate_execution_manifest
    from src.analysis.open_intelligence.general_question_context_admission import (
        validate_stored_context_admission,
    )
    from src.analysis.open_intelligence.general_question_context_queries import (
        _build_context_evidence_query,
        build_context_approval_query,
        build_context_candidate_query,
        build_context_evidence_query,
        build_context_index_query,
        build_context_partition_query,
        build_context_result_batch_query,
        build_context_result_query,
        decode_context_approval_rows,
        decode_context_result_batch_rows,
        decode_context_result_rows,
        decode_context_rows,
    )
    from src.analysis.open_intelligence.protected_context_registry import (
        _PROTECTED_CONTEXT_PROFILES,
    )

    provenance = snapshot["provenance"]
    profile = provenance["profile"]
    cap_version = _stored_context_cap_version(
        provenance, snapshot["deployment_digest"], allow_unversioned=True
    )
    if (
        profile not in _CONTEXT_PROFILES
        or set(provenance)
        != {
            "profile",
            "source_binding",
            "authority_captures",
            "captures",
            "admission",
            "limits",
        }
        | ({"cap_version"} if "cap_version" in provenance else set())
        | ({"parent_context"} if "parent_context_ref" in intake else set())
        | ({"identity_sidecar"} if profile in _IDENTITY_PROFILES else set())
        | ({"window_comparison"} if "window_comparison" in provenance else set())
        or type(provenance["admission"]) is not dict
        or provenance["admission"].get("contract_version") != _CONTEXT_PROFILES[profile]
    ):
        raise ValueError()
    source = provenance["source_binding"]
    parent_context = provenance.get("parent_context")
    if parent_context is not None:
        from src.analysis.open_intelligence.general_question_parent_capsule import (
            select_parent_aliases,
            validate_capsule,
        )

        validate_capsule(parent_context, request=request, reference=intake["parent_context_ref"])
        parent_refs = select_parent_aliases(parent_context, plan["parent_receipt_aliases"])
    else:
        parent_refs = []
    pin = next(
        (
            value
            for value in _PROTECTED_CONTEXT_PROFILES
            if value["profile_id"] == source.get("profile_id")
        ),
        None,
    )
    if pin is None or any(source.get(key) != value for key, value in pin.items()):
        raise ValueError()

    def record(ordinal):
        value = query_records.get(ordinal)
        reservation = value.get("reservation") if type(value) is dict else None
        if reservation is not None:
            reserved_version = _stored_context_cap_version(
                reservation,
                snapshot["deployment_digest"],
                allow_unversioned=reservation.get("contract_version")
                == "general_question_query_reservation_v1",
            )
            if reserved_version != cap_version:
                raise ValueError()
        if (
            reservation is None
            or reservation.get("ordinal") != ordinal
            or reservation.get("request_id") != request["request_id"]
            or reservation.get("request_digest") != request["request_digest"]
            or reservation.get("intake_digest") != intake["intake_digest"]
            or reservation.get("policy_digest") != snapshot["policy_digest"]
            or reservation.get("deployment_digest") != snapshot["deployment_digest"]
        ):
            raise ValueError()
        return value

    caps = provenance["limits"]["query_caps"]
    fixed_caps = _context_query_caps(35_000_000, 300_000_000, 35_000_000, cap_version=cap_version)
    has_partitions = "partitions" in provenance["captures"]
    if has_partitions:
        fixed_caps = _direct_context_query_caps(fixed_caps, cap_version=cap_version)
    if (
        type(caps) is not dict
        or set(caps) != set(fixed_caps)
        or any(
            type(value) is not int or not 0 <= value <= fixed_caps[key]
            for key, value in caps.items()
        )
        or sum(caps.values()) + caps["authority"] > 1_000_000_000
    ):
        raise ValueError()
    authority_ordinals = set()
    materials = {}
    contexts = _context_ancestors(source.get("recovery_context"))
    expected = {context["initial_consumption_id"]: context for context in contexts}
    if source["consumption_id"] in expected:
        raise ValueError()
    for capture in provenance["authority_captures"]:
        if type(capture) is not dict or set(capture) != {
            "kind",
            "value",
            "rows",
            "receipt",
            "ordinal",
        }:
            raise ValueError()
        ordinal = capture["ordinal"]
        if ordinal in authority_ordinals:
            raise ValueError()
        authority_ordinals.add(ordinal)
        if record(ordinal)["reservation"]["maximum_bytes_billed"] != caps["authority"]:
            raise ValueError()
        if capture["kind"] == "result":
            query = build_context_result_query(
                request, plan, intake, consumption_id=capture["value"]
            )
        elif capture["kind"] == "results":
            if type(capture["value"]) is not list or capture["value"] != sorted(expected):
                raise ValueError()
            query = build_context_result_batch_query(
                request, plan, intake, consumption_ids=capture["value"]
            )
        elif capture["kind"] == "approval":
            query = build_context_approval_query(
                request, plan, intake, manifest_sha256=capture["value"]
            )
        else:
            raise ValueError()
        rows = _bound_query_capture(
            {"rows": capture["rows"], "receipt": capture["receipt"]}, query, record(ordinal)
        )
        if capture["kind"] == "approval":
            approval = decode_context_approval_rows(rows, manifest_sha256=capture["value"])
            if approval.approval_id != source["approval_id"]:
                raise ValueError()
            continue
        decoded = (
            decode_context_result_batch_rows(rows, consumption_ids=capture["value"])
            if capture["kind"] == "results"
            else {
                capture["value"]: decode_context_result_rows(rows, consumption_id=capture["value"])
            }
        )
        for identifier, material in decoded.items():
            if identifier in materials:
                raise ValueError()
            result = material["result"]
            fields = (
                "manifest_sha256",
                "consumption_id",
                "execution_name",
                "result_id",
                "result_digest",
            )
            if identifier == source["consumption_id"]:
                if any(result[key] != source[key] for key in (*fields, "approval_id")):
                    raise ValueError()
            elif identifier not in expected or any(
                result[key] != expected[identifier]["initial_" + key] for key in fields
            ):
                raise ValueError()
            materials[identifier] = material
    if set(materials) != {source["consumption_id"], *expected}:
        raise ValueError()
    current_material = materials[source["consumption_id"]]
    current_manifest = validate_execution_manifest(
        json.loads(current_material["approval"].canonical_manifest_json),
        mode="historical_read",
        registry=current_material["registry"],
    )
    current_artifacts = dict(current_manifest.input_artifacts)
    if current_artifacts["recovery_context"] != canonical_digest(source.get("recovery_context")):
        raise ValueError()
    for context in contexts:
        material = materials[context["initial_consumption_id"]]
        manifest = validate_execution_manifest(
            json.loads(material["approval"].canonical_manifest_json),
            mode="historical_replay",
            registry=material["registry"],
        )
        artifacts = dict(manifest.input_artifacts)
        nested = context.get("ancestor_recovery_context")
        expected_mode = "recover" if nested is not None else "initial"
        if (
            manifest.operation != "source_snapshot_capture"
            or manifest.contract_sha256 != current_manifest.contract_sha256
            or manifest.service_identity != current_manifest.service_identity
            or manifest.arguments
            != (
                "scripts/staging/capture_protected_production_snapshot.py",
                "--cutoff-date",
                source["cutoff_date"],
                "--mode",
                expected_mode,
            )
            or any(
                artifacts[name] != current_artifacts[name]
                for name in ("capture_contract", "capture_plan", "source_metadata")
            )
            or (nested is not None and artifacts["recovery_context"] != canonical_digest(nested))
        ):
            raise ValueError()
    data_ordinals = sorted(set(query_records) - authority_ordinals)
    if len(data_ordinals) != (6 if has_partitions else 5) or set(
        query_records
    ) != authority_ordinals | set(data_ordinals):
        raise ValueError()
    limits = provenance["limits"]
    captures = provenance["captures"]
    candidate_query = build_context_candidate_query(
        request,
        plan,
        intake,
        source,
        candidate_limit=limits["candidate_limit"],
    )
    queries = {"candidate": candidate_query}
    legacy_queries = dict(queries)
    candidate_material = decode_context_rows(captures["candidate"]["rows"], query=candidate_query)
    direct = bool(
        [
            term
            for requirement in _context_requirements(plan)
            for term in requirement["search_terms"]
        ]
    ) and not any(row["sample_row_ids"] for row in candidate_material["records"])
    supplement = False
    continuity_versions = [
        captures[key]["receipt"]["template_id"].endswith("_v4")
        for key in ("enriched_index", "enriched", "raw_index", "raw")
    ]
    if any(continuity_versions) and not all(continuity_versions):
        raise ValueError()
    continuity_policy = all(continuity_versions)
    if parent_context is not None and not continuity_policy:
        raise ValueError()
    geo_versions = [
        captures[key]["receipt"]["template_id"].endswith(("_v3", "_v4"))
        for key in ("enriched_index", "enriched", "raw_index", "raw")
    ]
    if any(geo_versions) and not all(geo_versions):
        raise ValueError()
    geo_policy = all(geo_versions)
    if has_partitions:
        supplement = (
            parent_context is not None
            or any(row["sample_row_ids"] for row in candidate_material["records"])
        ) and all(
            captures[key]["receipt"]["template_id"].endswith(("_v2", "_v3", "_v4"))
            for key in ("enriched_index", "enriched", "raw_index", "raw")
        )
        if not direct and not supplement:
            raise ValueError()
        queries["partitions"] = build_context_partition_query(request, plan, intake, source)
        legacy_queries["partitions"] = queries["partitions"]
    if supplement and "effective_evidence_limit" not in limits:
        raise ValueError()
    if "effective_evidence_limit" in limits:
        effective = limits["effective_evidence_limit"]
        if (
            not has_partitions
            or not supplement
            or type(effective) is not int
            or type(limits["evidence_limit"]) is not int
            or not 1 <= limits["evidence_limit"] <= 200
            or effective != min(limits["evidence_limit"], SUPPLEMENT_EVIDENCE_LIMIT)
            or limits["enriched_limit"]
            != effective
            - len(
                {
                    (row["market"], row["source_row_id"])
                    for row in parent_refs
                    if row["lane"] == "raw_content"
                }
            )
            or type(limits["raw_limit"]) is not int
            or not 0 <= limits["raw_limit"] <= effective
        ):
            raise ValueError()
    for key, lane in (("enriched", "enriched_content"), ("raw", "raw_content")):
        common = dict(
            candidate_rows=captures["candidate"]["rows"],
            **({"partition_rows": captures["partitions"]["rows"]} if has_partitions else {}),
            lane=lane,
            candidate_limit=limits[key + "_limit"],
            **({"enriched_rows": captures["enriched"]["rows"]} if key == "raw" else {}),
            geo_policy=geo_policy,
            parent_context=parent_context,
            continuity_policy=continuity_policy,
        )
        queries[key + "_index"] = build_context_index_query(
            request,
            plan,
            intake,
            source,
            **common,
        )
        legacy_queries[key + "_index"] = _build_context_evidence_query(
            request,
            plan,
            intake,
            source,
            _index=True,
            _validation_legacy=not continuity_policy,
            **{**common, "geo_policy": False},
        )
        queries[key] = build_context_evidence_query(
            request,
            plan,
            intake,
            source,
            index_rows=captures[key + "_index"]["rows"],
            **common,
        )
        legacy_queries[key] = _build_context_evidence_query(
            request,
            plan,
            intake,
            source,
            index_rows=captures[key + "_index"]["rows"],
            _validation_legacy=not continuity_policy,
            **{**common, "geo_policy": False},
        )
    records = {key: record(ordinal) for key, ordinal in zip(queries, data_ordinals, strict=True)}
    modes = set()
    selected_queries = {}

    def matches(key, query_record, query):
        expected = {
            "template_id": query.template_id,
            "sql_digest": query.sql_digest,
            "parameters_digest": query.parameters_digest,
        }
        return captures[key]["receipt"] == query_record["receipt"] and all(
            query_record[part].get(field) == value
            for part in ("reservation", "receipt")
            for field, value in expected.items()
        )

    for key, current_query in queries.items():
        legacy_query = (
            current_query
            if current_query.template_id.endswith(("_v2", "_v3", "_v4"))
            else legacy_queries[key]
        )
        query_record = records[key]
        if matches(key, query_record, current_query):
            selected_queries[key] = current_query
            mode = "current"
        elif matches(key, query_record, legacy_query):
            selected_queries[key] = legacy_query
            mode = "legacy"
        else:
            raise ValueError()
        if current_query.sql_digest != legacy_query.sql_digest:
            modes.add(mode)
    if len(modes) > 1:
        raise ValueError()
    queries = selected_queries
    if "effective_evidence_limit" in limits and limits["raw_limit"] != (
        limits["effective_evidence_limit"]
        - decode_context_rows(captures["enriched"]["rows"], query=queries["enriched"])[
            "candidate_count"
        ]
    ):
        raise ValueError()
    if any(records[key]["reservation"]["maximum_bytes_billed"] != caps[key] for key in records):
        raise ValueError()
    admission = validate_stored_context_admission(
        provenance["admission"],
        request=request,
        plan=plan,
        intake=intake,
        queries=queries,
        captures=captures,
        query_records=records,
    )
    if profile in _IDENTITY_PROFILES and provenance["identity_sidecar"] != json.loads(
        canonical_bytes(_identity_sidecar(admission, source))
    ):
        raise ValueError()
    if "window_comparison" in provenance:
        from src.analysis.open_intelligence.general_question_context_admission import (
            context_selection_caps,
        )
        from src.analysis.open_intelligence.general_question_window_comparison import (
            validate_stored_window_comparison,
        )

        # Checked against this snapshot's own receipts, binding and captures, never the
        # switch, so a snapshot sealed while comparisons were on stays readable after.
        selection = context_selection_caps(
            admission,
            request=request,
            plan=plan,
            intake=intake,
            queries=queries,
            captures=captures,
            query_records=records,
        )
        validate_stored_window_comparison(
            provenance["window_comparison"], plan, admission["receipts"], source, selection
        )
    context = {
        "request": request,
        "intake": intake,
        "admission": {
            "policy_digest": snapshot["policy_digest"],
            "deployment_digest": snapshot["deployment_digest"],
        },
    }
    expected = _project_context_snapshot(context, plan, admission, provenance)
    if expected != snapshot:
        raise ValueError()
    return copy.deepcopy(expected)


# Bridge v3 context: the snapshot profile a bridge read is stored under, the loader's native
# clients a provider must supply, and the keyed cap each clone read is reserved under.
BRIDGE_CONTEXT_PROFILE = "protected_bridge_context_v1"
BRIDGE_CLIENT_NAMES = frozenset(
    {
        "capture_clients",
        "read_input",
        "read_grant",
        "evidence",
        "ledger_reader",
        "generation_loader",
        "read_native_job",
        "read_capture_facts",
        "read_clone",
    }
)
_BRIDGE_CAP_KEYS = MappingProxyType(
    {
        "enriched_content": "enriched",
        "raw_content": "raw",
        "seed_candidates": "candidate",
        "trend_analysis": "candidate",
    }
)
_BRIDGE_LOADER_CODES = frozenset({"bridge_source_uncovered", "bridge_source_invalid"})


def production_bridge_clients(credentials):
    """The production native clients the bridge loader reads a capture through.

    Served by the one provider the planner ceiling also consults. While the deployment has
    not turned bridge reads on this refuses, and the snapshot reports a coded coverage gap
    rather than reading a clone through anything weaker.
    """
    from src.analysis.open_intelligence import general_question_context_admission as admission

    provider = admission.bridge_clients_provider()
    if provider is None:
        raise ValueError("bridge_clients_unavailable")
    return provider(credentials)


def _bridge_row_limit(lane, candidate_limit, effective_evidence_limit):
    from src.analysis.open_intelligence.source_estate_bridge import COLLECTION_TABLES

    return effective_evidence_limit if lane in COLLECTION_TABLES else candidate_limit


def _bridge_template(lane):
    from src.analysis.open_intelligence.general_question_context_queries import (
        BRIDGE_COLLECTION_TEMPLATE,
        BRIDGE_HISTORY_TEMPLATE,
    )
    from src.analysis.open_intelligence.source_estate_bridge import COLLECTION_TABLES

    return BRIDGE_COLLECTION_TEMPLATE if lane in COLLECTION_TABLES else BRIDGE_HISTORY_TEMPLATE


def _build_bridge_context_snapshot(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    context,
    candidate_limit,
    evidence_limit,
    query_caps,
    stage_now,
    parent_context,
    bridge_clients,
    cap_version,
):
    """Admit a bridge v3 clone read for a window no v1 profile covers.

    Every refusal of the loader, and every source the stored record cannot re-verify, is a
    coded coverage gap; no row is read before the capture is admitted.
    """
    from src.analysis.open_intelligence.general_question_context_admission import (
        BridgeContextInvalid,
        NoMatchingContextEvidence,
        admit_bridge_context,
        bridge_source_record,
        load_bridge_history_source,
        restore_bridge_source,
    )
    from src.analysis.open_intelligence.general_question_context_queries import (
        bridge_read_lanes,
    )
    from src.analysis.open_intelligence.general_question_queries import GeneralQuestionQueries
    from src.analysis.open_intelligence.general_question_query_execution import (
        execute_context_bridge_query,
    )
    from src.analysis.open_intelligence.protected_context_registry import bridge_entries

    request = context["request"]
    end = plan["window"]["end"]
    if not any(end <= entry.cutoff_date for entry in bridge_entries()):
        return _failure("coverage_incomplete", "bridge_source_uncovered")
    provider = production_bridge_clients if bridge_clients is None else bridge_clients
    try:
        clients = provider(credentials)
        if type(clients) is not dict or set(clients) != BRIDGE_CLIENT_NAMES:
            raise ValueError("bridge_clients_unavailable")
    except Exception:
        return _failure("coverage_incomplete", "bridge_clients_unavailable")
    loader_now = stage_now()
    try:
        loaded = load_bridge_history_source(
            window_end=end,
            request_as_of=request["as_of"],
            client_scope_id=request["client_scope_id"],
            market_scope=sorted(set(plan["markets"])),
            now=loader_now,
            **clients,
        )
    except ValueError as error:
        code = error.args[0] if error.args and error.args[0] in _BRIDGE_LOADER_CODES else None
        return _failure("coverage_incomplete", code or "bridge_source_invalid")
    except QuestionStoreError:
        raise
    except Exception:
        return _failure("coverage_incomplete", "bridge_source_unavailable")
    try:
        record = bridge_source_record(loaded)
        source = restore_bridge_source(record, request=request, plan=plan)
        if source.cells != loaded.cells or source.limitations != loaded.limitations:
            raise BridgeContextInvalid()
        lanes = bridge_read_lanes(source, plan)
    except ValueError:
        return _failure("coverage_incomplete", "bridge_source_invalid")
    effective = min(evidence_limit, SUPPLEMENT_EVIDENCE_LIMIT)
    caps = {lane: query_caps[_BRIDGE_CAP_KEYS[lane]] for lane in lanes}
    if len(lanes) > 8 or sum(caps.values()) > 1_000_000_000:
        return _failure("query_budget_invalid")
    results = {}
    for ordinal, lane in enumerate(lanes, start=1):
        outcome = execute_context_bridge_query(
            invocation,
            bridge_source=source,
            lane=lane,
            template_id=_bridge_template(lane),
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            plan=plan,
            ordinal=ordinal,
            candidate_limit=_bridge_row_limit(lane, candidate_limit, effective),
            maximum_bytes_billed=caps[lane],
            cap_version=cap_version,
            now=stage_now(),
        )
        if outcome.get("application_status") != "accepted":
            return _failure(outcome.get("reason") or "bridge_query_unavailable")
        results[lane] = outcome
    ledger = GeneralQuestionQueries(store)
    query_records = {}
    for ordinal, lane in enumerate(lanes, start=1):
        value = ledger.read_query(invocation["request_id"], scope=scope, ordinal=ordinal)
        if value is None:
            return _failure("bridge_query_unavailable")
        query_records[lane] = value
    captures = {
        lane: {"rows": value["rows"], "receipt": value["receipt"]}
        for lane, value in results.items()
    }
    try:
        admission = admit_bridge_context(
            request,
            plan,
            context["intake"],
            source=source,
            queries={lane: value["prepared_query"] for lane, value in results.items()},
            captures=captures,
            query_records=query_records,
            evidence_limit=effective,
        )
    except NoMatchingContextEvidence:
        return _failure("evidence_insufficient", "no_matching_evidence")
    except BridgeContextInvalid:
        return _failure("coverage_incomplete", "bridge_context_invalid")
    provenance = {
        "profile": BRIDGE_CONTEXT_PROFILE,
        "cap_version": cap_version,
        "bridge_source": record,
        "captures": copy.deepcopy(captures),
        "admission": copy.deepcopy(admission),
        "limits": {
            "query_caps": copy.deepcopy(query_caps),
            "candidate_limit": candidate_limit,
            "evidence_limit": evidence_limit,
            "effective_evidence_limit": effective,
        },
        **({"parent_context": copy.deepcopy(parent_context)} if parent_context is not None else {}),
    }
    snapshot = _project_context_snapshot(context, plan, admission, provenance)
    key = f"requests/{snapshot['request_id']}/snapshot.json"
    stage_now()
    stored = store._objects.create(key, snapshot)
    readback = store._objects.read(key, generation=stored.generation)
    if readback is None or readback.value != snapshot:
        return _failure("snapshot_readback_invalid")
    return {
        "contract_version": "general_question_snapshot_build_v1",
        "status": "admitted",
        "snapshot": copy.deepcopy(snapshot),
        "reason": None,
        "missing_work": [],
    }


def _validate_stored_bridge_snapshot(snapshot, request, intake, plan, query_records):
    """Re-verify a stored bridge snapshot: its source against the committed registry, its
    reads against the owned query records and its admission by rebuilding it."""
    from src.analysis.open_intelligence.general_question_context_admission import (
        restore_bridge_source,
        validate_stored_bridge_admission,
    )
    from src.analysis.open_intelligence.general_question_context_queries import (
        bridge_read_lanes,
        build_bridge_context_query,
    )

    provenance = snapshot["provenance"]
    cap_version = _stored_context_cap_version(
        provenance, snapshot["deployment_digest"], allow_unversioned=False
    )
    if set(provenance) != {
        "profile",
        "cap_version",
        "bridge_source",
        "captures",
        "admission",
        "limits",
    } | ({"parent_context"} if "parent_context_ref" in intake else set()):
        raise ValueError()
    parent_context = provenance.get("parent_context")
    if parent_context is not None:
        from src.analysis.open_intelligence.general_question_parent_capsule import (
            validate_capsule,
        )

        validate_capsule(parent_context, request=request, reference=intake["parent_context_ref"])
    source = restore_bridge_source(provenance["bridge_source"], request=request, plan=plan)
    limits = provenance["limits"]
    if (
        type(limits) is not dict
        or set(limits)
        != {"query_caps", "candidate_limit", "evidence_limit", "effective_evidence_limit"}
        or type(limits["candidate_limit"]) is not int
        or not 1 <= limits["candidate_limit"] <= 1000
        or type(limits["evidence_limit"]) is not int
        or not 1 <= limits["evidence_limit"] <= 200
        or limits["effective_evidence_limit"]
        != min(limits["evidence_limit"], SUPPLEMENT_EVIDENCE_LIMIT)
    ):
        raise ValueError()
    caps = limits["query_caps"]
    fixed_caps = _context_query_caps(35_000_000, 300_000_000, 35_000_000, cap_version=cap_version)
    if (
        type(caps) is not dict
        or set(caps) != set(fixed_caps)
        or any(
            type(value) is not int or not 0 <= value <= fixed_caps[key]
            for key, value in caps.items()
        )
        or sum(caps.values()) + caps["authority"] > 1_000_000_000
    ):
        raise ValueError()
    lanes = bridge_read_lanes(source, plan)
    captures = provenance["captures"]
    if (
        type(captures) is not dict
        or set(captures) != set(lanes)
        or set(query_records) != set(range(1, len(lanes) + 1))
    ):
        raise ValueError()
    queries, records = {}, {}
    for ordinal, lane in enumerate(lanes, start=1):
        value = query_records[ordinal]
        reservation = value.get("reservation") if type(value) is dict else None
        if (
            reservation is None
            or reservation.get("ordinal") != ordinal
            or _stored_context_cap_version(
                reservation, snapshot["deployment_digest"], allow_unversioned=False
            )
            != cap_version
            or reservation.get("request_id") != request["request_id"]
            or reservation.get("request_digest") != request["request_digest"]
            or reservation.get("intake_digest") != intake["intake_digest"]
            or reservation.get("policy_digest") != snapshot["policy_digest"]
            or reservation.get("deployment_digest") != snapshot["deployment_digest"]
            or reservation.get("maximum_bytes_billed") != caps[_BRIDGE_CAP_KEYS[lane]]
        ):
            raise ValueError()
        query = build_bridge_context_query(
            request,
            plan,
            intake,
            source,
            lane=lane,
            row_limit=_bridge_row_limit(
                lane, limits["candidate_limit"], limits["effective_evidence_limit"]
            ),
        )
        _bound_query_capture(captures[lane], query, value)
        queries[lane], records[lane] = query, value
    admission = validate_stored_bridge_admission(
        provenance["admission"],
        request=request,
        plan=plan,
        intake=intake,
        source=source,
        queries=queries,
        captures=captures,
        query_records=records,
        evidence_limit=limits["effective_evidence_limit"],
    )
    context = {
        "request": request,
        "intake": intake,
        "admission": {
            "policy_digest": snapshot["policy_digest"],
            "deployment_digest": snapshot["deployment_digest"],
        },
    }
    expected = _project_context_snapshot(context, plan, admission, provenance)
    if expected != snapshot:
        raise ValueError()
    return copy.deepcopy(expected)


def build_general_question_snapshot(
    invocation,
    *,
    store,
    scope,
    runtime_identity,
    credentials,
    plan,
    candidate_limit,
    evidence_limit,
    discovery_bytes,
    source_bytes,
    release_bytes,
    now,
    history_resolver=None,
    bridge_clients=None,
):
    started = monotonic()
    started_at = utc_time(now)

    def current():
        return started_at + timedelta(seconds=monotonic() - started)

    try:
        context = validate_question_invocation(
            invocation, store=store, scope=scope, runtime_identity=runtime_identity
        )
        deadline = control_time(context["admission"]["deadline_at"])

        def stage_now():
            value = current()
            if value >= deadline:
                raise QuestionStoreError("request_expired")
            return value

        stored_plan = store._objects.read(f"requests/{invocation['request_id']}/plan.json")
        if stored_plan is None:
            return _failure("plan_unavailable")
        expected = validate_stored_question_plan(
            stored_plan.value, request=context["request"], intake=context["intake"]
        )
        if plan != expected:
            return _failure("plan_conflict")
        if (
            type(candidate_limit) is not int
            or not 1 <= candidate_limit <= 1000
            or type(evidence_limit) is not int
            or not 1 <= evidence_limit <= 200
            or any(
                type(value) is not int or value < 0
                for value in (discovery_bytes, source_bytes, release_bytes)
            )
        ):
            return _failure("query_budget_invalid")
        from src.analysis.open_intelligence.protected_context_registry import (
            _PROTECTED_CONTEXT_PROFILES,
            bridge_entries,
        )

        if _PROTECTED_CONTEXT_PROFILES or bridge_entries():
            query_caps = _context_query_caps(discovery_bytes, source_bytes, release_bytes)
            if (
                sum(query_caps.values()) + query_caps["authority"] > 1_000_000_000
                or candidate_limit + 4 * evidence_limit > 1000
            ):
                return _failure("query_budget_invalid")
            return _build_context_snapshot(
                invocation,
                store=store,
                scope=scope,
                runtime_identity=runtime_identity,
                credentials=credentials,
                plan=plan,
                context=context,
                candidate_limit=candidate_limit,
                evidence_limit=evidence_limit,
                query_caps=query_caps,
                stage_now=stage_now,
                bridge_clients=bridge_clients,
            )
        if discovery_bytes + source_bytes + 5 * release_bytes + _COPY_MAXIMUM_BYTES > 1_000_000_000:
            return _failure("query_budget_invalid")
        discovery = execute_released_candidate_query(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            plan=plan,
            candidate_limit=candidate_limit,
            maximum_bytes_billed=discovery_bytes,
            now=stage_now(),
        )
        if discovery.get("application_status") != "accepted":
            return _failure(discovery.get("reason") or "discovery_unavailable")
        selection = select_released_candidates_for_inspection(
            discovery["rows"], remaining_candidate_slots=1000 - candidate_limit
        )
        if not selection["selected_ids"]:
            return _failure(
                "coverage_incomplete", "No complete released run fits the inspection budget."
            )
        if len(selection["selected_run_ids"]) > 5:
            return _failure("query_budget_invalid")
        source = execute_selected_source_query(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            plan=plan,
            selected_ids=selection["selected_ids"],
            candidate_limit=selection["reserved_inspection_slots"],
            evidence_limit=evidence_limit,
            maximum_bytes_billed=source_bytes,
            now=stage_now(),
        )
        if source.get("application_status") != "accepted":
            return _failure(source.get("reason") or "source_unavailable")
        material = source["material"]
        if material.get("overflow_count") or material.get("selection_truncation_reason"):
            return _failure(
                "coverage_incomplete", "Source inspection or evidence selection was truncated."
            )
        schema = None
        admissions = []
        release_receipts = []
        for index, run_id in enumerate(selection["selected_run_ids"], start=3):
            receipt, run_rows = _source_run(material, run_id)
            if schema is None:
                schema = read_question_source_schemas(
                    invocation,
                    store=store,
                    scope=scope,
                    runtime_identity=runtime_identity,
                    credentials=credentials,
                    cluster_build_version=receipt.cluster_build_version,
                    now=stage_now(),
                )
            if schema.get("requested_cluster_build_version") != receipt.cluster_build_version:
                return _failure("physical_schema_invalid")
            release = execute_release_material_query(
                invocation,
                store=store,
                scope=scope,
                runtime_identity=runtime_identity,
                credentials=credentials,
                plan=plan,
                run_id=run_id,
                ordinal=index,
                maximum_bytes_billed=release_bytes,
                now=stage_now(),
            )
            if release.get("application_status") != "accepted":
                return _failure(release.get("reason") or "release_unavailable")
            admission = validate_released_run_admission(
                context["request"],
                plan,
                context["intake"],
                receipt=receipt,
                run_rows=run_rows,
                release_material=release["material"],
                producer_profile_id="legacy_r16_v2",
            )
            admissions.append(admission)
            release_receipts.append(
                {
                    "run_id": run_id,
                    "rows": copy.deepcopy(release["rows"]),
                    "receipt": copy.deepcopy(release["receipt"]),
                }
            )
        copy_result = execute_current_source_copy_query(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            plan=plan,
            ordinal=3 + len(selection["selected_run_ids"]),
            maximum_bytes_billed=_COPY_MAXIMUM_BYTES,
            now=stage_now(),
        )
        if copy_result.get("application_status") != "accepted":
            return _failure(copy_result.get("reason") or "current_source_copy_unavailable")
        copy_schema = read_question_copy_schemas(
            invocation,
            store=store,
            scope=scope,
            runtime_identity=runtime_identity,
            credentials=credentials,
            now=stage_now(),
        )
        if (
            copy_result["material"].get("content_validated") is not True
            or copy_result["material"].get("physical_schema_required") is not True
            or copy_schema.get("source_authority") is not False
        ):
            return _failure("current_source_copy_invalid")
        _validate_selected_copy_binding(material, copy_result["material"])
        if set(material.get("missing_checks", ())) != _COORDINATOR_CHECKS:
            return _failure("source_validation_incomplete")
        facts = material.get("selected_facts")
        if type(facts) is not list or not facts:
            return _failure("coverage_incomplete", "No admitted released evidence was selected.")
        _validate_fact_scope(facts, plan, context["request"]["as_of"])
        history = _history_records(plan, history_resolver)
        # The writer seals exactly what the reader will check, so a record the
        # reader would refuse, one dated after the request among them, is
        # refused here before anything is written.
        _validate_history_records(
            plan, history, _HISTORY_CONTRACT_V2, request_as_of=context["request"]["as_of"]
        )
        coverage, missing_requirements = _requirement_coverage(plan["requirements"], facts, history)
        if not coverage:
            return _failure(
                "coverage_incomplete",
                *(missing_requirements or ["No planned requirement matched admitted evidence."]),
            )
        material, coverage = _covered_material(material, coverage)
        facts = material["selected_facts"]
        coverage_missing, coverage_limitations = _coverage_gaps(
            plan, facts, [admission.receipt for admission in admissions]
        )
        missing_work = [*missing_requirements, *coverage_missing]
        provenance = {
            "candidate_limit": candidate_limit,
            "evidence_limit": evidence_limit,
            "discovery": {
                "rows": copy.deepcopy(discovery["rows"]),
                "receipt": copy.deepcopy(discovery["receipt"]),
            },
            "source": {
                "rows": copy.deepcopy(source["rows"]),
                "receipt": copy.deepcopy(source["receipt"]),
            },
            "releases": release_receipts,
            "current_copy": {
                "rows": copy.deepcopy(copy_result["rows"]),
                "receipt": copy.deepcopy(copy_result["receipt"]),
            },
            "source_schema": copy.deepcopy(schema),
            "copy_schema": copy.deepcopy(copy_schema),
            "producer_profiles": [admission.producer_profile_id for admission in admissions],
            "selection": copy.deepcopy(selection),
            "requirement_coverage": {
                requirement_id: [facts[index]["projection_sha256"] for index in matched]
                for requirement_id, matched in coverage.items()
            },
            "coverage_gaps": copy.deepcopy(coverage_missing),
            "history_requirements": history,
            "history_contract_version": _HISTORY_CONTRACT_V2,
        }
        stage_now()
        snapshot = _project_snapshot(
            context,
            plan,
            material,
            {"source": schema, "copy": copy_schema},
            admissions,
            provenance,
            coverage,
            missing_work,
            coverage_limitations,
            history,
        )
        key = f"requests/{snapshot['request_id']}/snapshot.json"
        stored = store._objects.create(key, snapshot)
        readback = store._objects.read(key, generation=stored.generation)
        if readback is None or readback.value != snapshot:
            return _failure("snapshot_readback_invalid")
        return {
            "contract_version": "general_question_snapshot_build_v1",
            "status": "admitted",
            "snapshot": copy.deepcopy(snapshot),
            "reason": None,
            "missing_work": [],
        }
    except Exception as error:
        code = getattr(error, "code", None)
        if (
            code is None
            and type(error) is ValueError
            and len(error.args) == 1
            and error.args[0] in _SAFE_FAILURE_CODES
        ):
            code = error.args[0]
        return _failure(code or "snapshot_validation_failed")


def validate_stored_general_question_snapshot(snapshot, *, request, intake, plan, query_records):
    """Recompute persisted source bindings against the current query ledger."""
    try:
        if type(snapshot) is not dict or type(query_records) is not dict:
            raise ValueError()
        digest = snapshot.get("snapshot_digest")
        unsigned = {key: value for key, value in snapshot.items() if key != "snapshot_digest"}
        if (
            digest != canonical_digest(unsigned)
            or snapshot.get("contract_version") != "general_question_snapshot_v1"
            or snapshot.get("request_id") != request["request_id"]
            or snapshot.get("request_digest") != request["request_digest"]
            or snapshot.get("intake_digest") != intake["intake_digest"]
            or snapshot.get("plan_digest") != plan["plan_digest"]
            or snapshot.get("policy_digest") != request["policy_digest"]
            or snapshot.get("as_of") != request["as_of"]
            or snapshot.get("window") != plan["window"]
        ):
            raise ValueError()
        validated_plan = validate_stored_question_plan(plan, request=request, intake=intake)
        provenance = snapshot["provenance"]
        if type(provenance) is dict and provenance.get("profile") == BRIDGE_CONTEXT_PROFILE:
            if validated_plan != plan:
                raise ValueError()
            return _validate_stored_bridge_snapshot(snapshot, request, intake, plan, query_records)
        if type(provenance) is dict and provenance.get("profile") in _CONTEXT_PROFILES:
            if validated_plan != plan:
                raise ValueError()
            return _validate_stored_context_snapshot(snapshot, request, intake, plan, query_records)
        history_version = stored_history_contract_version(snapshot)
        if validated_plan != plan or set(provenance) - {
            "history_requirements",
            "history_contract_version",
        } != {
            "candidate_limit",
            "evidence_limit",
            "discovery",
            "source",
            "releases",
            "current_copy",
            "source_schema",
            "copy_schema",
            "producer_profiles",
            "selection",
            "requirement_coverage",
            "coverage_gaps",
        }:
            raise ValueError()
        releases = provenance["releases"]
        if type(releases) is not list or not 1 <= len(releases) <= 5:
            raise ValueError()
        expected_ordinals = set(range(1, 4 + len(releases)))
        if set(query_records) != expected_ordinals:
            raise ValueError()

        def record(ordinal):
            value = query_records[ordinal]
            reservation = value.get("reservation") if type(value) is dict else None
            if (
                reservation is None
                or reservation.get("ordinal") != ordinal
                or reservation.get("request_id") != request["request_id"]
                or reservation.get("request_digest") != request["request_digest"]
                or reservation.get("intake_digest") != intake["intake_digest"]
                or reservation.get("policy_digest") != snapshot["policy_digest"]
                or reservation.get("deployment_digest") != snapshot["deployment_digest"]
            ):
                raise ValueError()
            return value

        discovery_query = build_released_candidate_query(
            request, plan, intake, candidate_limit=provenance["candidate_limit"]
        )
        discovery_rows = _bound_query_capture(provenance["discovery"], discovery_query, record(1))
        selection = select_released_candidates_for_inspection(
            discovery_rows,
            remaining_candidate_slots=1000 - provenance["candidate_limit"],
        )
        if canonical_bytes(selection) != canonical_bytes(provenance["selection"]):
            raise ValueError()
        source_query = build_selected_source_query(
            request,
            selection["selected_ids"],
            plan_window=plan["window"],
            candidate_limit=selection["reserved_inspection_slots"],
            evidence_limit=provenance["evidence_limit"],
        )
        source_rows = _bound_query_capture(provenance["source"], source_query, record(2))
        material = decode_selected_source_rows(source_rows, query=source_query)
        if material["overflow_count"] or material["selection_truncation_reason"]:
            raise ValueError()
        source_runs = {}
        for run_id in selection["selected_run_ids"]:
            source_runs[run_id] = _source_run(material, run_id)
        versions = {receipt.cluster_build_version for receipt, _rows in source_runs.values()}
        if len(versions) != 1:
            raise ValueError()
        source_schema = validate_source_schemas(
            provenance["source_schema"]["table_resources"],
            cluster_build_version=next(iter(versions)),
        )
        if any(
            provenance["source_schema"].get(field) != source_schema[field]
            for field in (
                "table_resources",
                "metadata_digest",
                "schema_digests",
                "declaration_digests",
                "source_authority",
            )
        ):
            raise ValueError()
        copy_schema = validate_current_source_copy_schemas(
            provenance["copy_schema"]["table_resources"]
        )
        if any(provenance["copy_schema"].get(field) != copy_schema[field] for field in copy_schema):
            raise ValueError()
        admissions = []
        for ordinal, release in enumerate(releases, start=3):
            if type(release) is not dict or set(release) != {"run_id", "rows", "receipt"}:
                raise ValueError()
            run_id = release["run_id"]
            if run_id not in source_runs:
                raise ValueError()
            release_query = build_release_material_query(run_id)
            release_rows = _bound_query_capture(
                {"rows": release["rows"], "receipt": release["receipt"]},
                release_query,
                record(ordinal),
            )
            if len(release_rows) != 1 or type(release_rows[0]) is not dict:
                raise ValueError()
            receipt, run_rows = source_runs[run_id]
            admissions.append(
                validate_released_run_admission(
                    request,
                    plan,
                    intake,
                    receipt=receipt,
                    run_rows=run_rows,
                    release_material=release_rows[0],
                    producer_profile_id="legacy_r16_v2",
                )
            )
        if [item.run_id for item in admissions] != list(selection["selected_run_ids"]):
            raise ValueError()
        copy_query = build_current_source_copy_query()
        copy_rows = _bound_query_capture(
            provenance["current_copy"], copy_query, record(3 + len(releases))
        )
        copy_proof = validate_current_source_copy_proof(copy_rows, query=copy_query)
        if (
            copy_proof["content_validated"] is not True
            or set(material.get("missing_checks", ())) != _COORDINATOR_CHECKS
        ):
            raise ValueError()
        _validate_selected_copy_binding(material, copy_proof)
        if history_version == _HISTORY_CONTRACT_V1:
            history = provenance.get("history_requirements", {})
        else:
            history = provenance["history_requirements"]
        _validate_history_records(plan, history, history_version, request_as_of=request["as_of"])
        coverage, missing = _requirement_coverage(
            plan["requirements"], material["selected_facts"], history
        )
        _validate_fact_scope(material["selected_facts"], plan, request["as_of"])
        if not coverage:
            raise ValueError()
        material, coverage = _covered_material(material, coverage)
        coverage_missing, coverage_limitations = _coverage_gaps(
            plan, material["selected_facts"], [admission.receipt for admission in admissions]
        )
        encoded_coverage = {
            requirement_id: [
                material["selected_facts"][index]["projection_sha256"] for index in matched
            ]
            for requirement_id, matched in coverage.items()
        }
        if (
            encoded_coverage != provenance["requirement_coverage"]
            or provenance["coverage_gaps"] != coverage_missing
            or snapshot["missing_work"] != [*missing, *coverage_missing]
        ):
            raise ValueError()
        context = {
            "request": request,
            "intake": intake,
            "admission": {
                "policy_digest": snapshot["policy_digest"],
                "deployment_digest": snapshot["deployment_digest"],
            },
        }
        expected = _project_snapshot(
            context,
            plan,
            material,
            {"source": source_schema, "copy": copy_schema},
            admissions,
            provenance,
            coverage,
            [*missing, *coverage_missing],
            coverage_limitations,
            history,
        )
        if expected != snapshot:
            raise ValueError()
        return copy.deepcopy(snapshot)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError) as error:
        if isinstance(error, ValueError) and str(error) == "snapshot_cap_version_unknown":
            raise
        raise ValueError("snapshot_invalid") from error


__all__ = ["build_general_question_snapshot", "validate_stored_general_question_snapshot"]
