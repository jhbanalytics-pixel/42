"""Bridge evidence decoding and independently resolved retained-record bindings."""

import copy
import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta

from .brain_contract import canonical_digest
from .daily_child_execution import FUNDED_EXECUTION_PREFIX
from .daily_execution_authority import native_instant_key, read_daily_execution_chain
from .daily_execution_contracts import DERIVATION_ID, canonical_json_object
from .daily_product_completion import PRODUCT_NAMES, validate_completion
from .daily_product_io import canonical_rows
from .execution_approval import _parse_timestamp
from .general_question_context_queries import decode_context_result_rows_v2
from .source_estate_bridge import HISTORY_TABLES, MARKETS, validate_bridge_policy
from .staging_source_profile import read_completed_source_run

REF = frozenset(
    (
        "operation",
        "consumption_id",
        "manifest_sha256",
        "result_id",
        "result_digest",
        "origin_registry_sha256",
        "resource_manifest_sha256",
    )
)
COLLECTION = frozenset(
    (
        "run_id",
        "result_ref",
        "collection_receipt_digest",
        "collection_started_at",
        "collection_completed_at",
    )
)
HISTORY = frozenset(
    (
        "lane",
        "market",
        "product_date",
        "state",
        "result_ref",
        "product_receipt_digest",
        "output_digest",
        "row_count",
        "completed_at",
        "available_at",
        "reason_code",
    )
)
RULE = frozenset(
    (
        "source_family",
        "endpoint",
        "content_type",
        "time_basis",
        "event_field",
        "missing_time_action",
        "future_time_action",
    )
)
# Only the event instant basis names an event field; every other basis withholds one.
EVENT_FIELDS = {
    "native_event_instant": "published_at",
    "native_metric_interval": None,
    "collection_observation": None,
    "unknown": None,
}
# History lanes whose daily outcome the retained products completion record carries.
RECORDED_HISTORY_LANES = tuple(lane for lane in HISTORY_TABLES if lane in PRODUCT_NAMES)
VERSIONS = {
    "temporal_rules": ("source_observation_semantics_v1", "rules", RULE),
    "collection_receipt_set": ("42_bridge_collection_receipt_set_v1", "receipts", COLLECTION),
    "history_completion_set": ("42_bridge_history_completion_set_v1", "entries", HISTORY),
}


def _require(condition):
    if not condition:
        raise ValueError("bridge_evidence_invalid")


def _text(value):
    _require(type(value) is str and bool(value.strip()) and value == value.strip())


def _digest(value):
    _require(
        type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _ref(value):
    _require(type(value) is dict and set(value) == REF)
    for name, field in value.items():
        _text(field)
        if name not in ("operation", "consumption_id", "result_id"):
            _digest(field)


def parse_bridge_artifacts(values):
    _require(type(values) is dict and set(values) == {"bridge_policy", *VERSIONS})
    result = {"bridge_policy": validate_bridge_policy(values["bridge_policy"])}
    for name, (version, collection, fields) in VERSIONS.items():
        _, raw = canonical_json_object(values[name], "bridge_evidence_invalid")
        value = json.loads(raw)
        _require(
            set(value) == {"contract_version", collection} and value["contract_version"] == version
        )
        rows = value[collection]
        _require(type(rows) is list)
        keys = []
        for row in rows:
            _require(type(row) is dict and set(row) == fields)
            if name == "temporal_rules":
                for field in ("source_family", "endpoint", "content_type"):
                    _text(row[field])
                _require(type(row["time_basis"]) is str and row["time_basis"] in EVENT_FIELDS)
                _require(row["event_field"] is None or type(row["event_field"]) is str)
                _require(row["event_field"] == EVENT_FIELDS[row["time_basis"]])
                _require(
                    row["missing_time_action"]
                    == row["future_time_action"]
                    == "withhold_event_time_claim"
                )
                keys.append(tuple(row[k] for k in ("source_family", "endpoint", "content_type")))
            elif name == "collection_receipt_set":
                _text(row["run_id"])
                _ref(row["result_ref"])
                _digest(row["collection_receipt_digest"])
                _require(
                    _parse_timestamp(row["collection_started_at"], "bridge_evidence_invalid")
                    <= _parse_timestamp(row["collection_completed_at"], "bridge_evidence_invalid")
                )
                keys.append(row["run_id"])
            else:
                _require(row["lane"] in HISTORY_TABLES and row["market"] in MARKETS)
                _require(
                    type(row["product_date"]) is str
                    and date.fromisoformat(row["product_date"]).isoformat() == row["product_date"]
                )
                _require(row["state"] in ("completed", "empty", "unavailable"))
                if row["state"] == "unavailable":
                    _text(row["reason_code"])
                    _require(
                        all(
                            row[k] is None
                            for k in (
                                "result_ref",
                                "product_receipt_digest",
                                "output_digest",
                                "row_count",
                                "completed_at",
                                "available_at",
                            )
                        )
                    )
                else:
                    # A lane the completion record does not carry has no native evidence.
                    _require(row["lane"] in RECORDED_HISTORY_LANES)
                    _ref(row["result_ref"])
                    _digest(row["product_receipt_digest"])
                    _digest(row["output_digest"])
                    _require(
                        type(row["row_count"]) is int
                        and (
                            row["row_count"] > 0
                            if row["state"] == "completed"
                            else row["row_count"] == 0
                        )
                    )
                    _require(row["reason_code"] is None)
                    _require(
                        _parse_timestamp(row["completed_at"], "bridge_evidence_invalid")
                        <= _parse_timestamp(row["available_at"], "bridge_evidence_invalid")
                    )
                keys.append(tuple(row[k] for k in ("lane", "market", "product_date")))
        _require(keys == sorted(set(keys)))
        _require(bool(rows))
        if name == "history_completion_set":
            _require(_history_grid(keys))
        result[name] = value
    return result


def _history_grid(keys):
    """A missing (lane, market, date) must be an explicit unavailable entry, never absent."""
    days = sorted({date.fromisoformat(day) for _, _, day in keys})
    span = [days[0] + timedelta(days=offset) for offset in range((days[-1] - days[0]).days + 1)]
    expected = {
        (lane, market, day.isoformat())
        for lane in HISTORY_TABLES
        for market in MARKETS
        for day in span
    }
    return set(keys) == expected


def resolve_result_ref(reference, *, read_chain, generation_loader=None):
    _ref(reference)
    checked = decode_context_result_rows_v2(
        read_chain(dict(reference)),
        consumption_id=reference["consumption_id"],
        operation=reference["operation"],
        generation_loader=generation_loader,
    )
    _require(all(checked["result"][name] == value for name, value in reference.items()))
    _require(checked["result"]["status"] == "succeeded")
    return checked


def resolve_daily_result_ref(reference, *, derivation_id, clients):
    _ref(reference)
    chain = read_daily_execution_chain(derivation_id=derivation_id, clients=clients)
    _require(all(chain.result[name] == value for name, value in reference.items()))
    _require(chain.result["status"] == "succeeded")
    return chain


def resolve_source_run(item, *, read_chain, read_source_run, generation_loader=None):
    raise ValueError("bridge_payload_contract_unavailable")


# The funded Wave 1 pilot records its funded close as an approval ledger execution result
# under this operation and reference suffix, run as this identity; the approval ledger
# route binds each collection receipt to one such result and its execution.
FUNDED_OPERATION = "wave1_pilot"
FUNDED_RESULT_SUFFIX = "#funded-run-close"
FUNDED_IDENTITY = "intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"
# The pilot stamps one pipeline run id into every raw_content and enriched_content row it
# writes and into the GDELT persistence proof its funded close carries; the grammar is the
# one that proof admits for its run id.
FUNDED_RUN_ID = re.compile(r"[a-z0-9_][a-z0-9_-]{0,127}")


def funded_pipeline_run(result_json, digest):
    """The pipeline run id one funded close names, from bytes that hash to ``digest``.

    The funded close must name its GDELT persistence proof as complete, and that proof the
    run id; a collected row is the funded execution's own only when it carries that id.
    """
    _refuse(
        type(result_json) is str and hashlib.sha256(result_json.encode()).hexdigest() == digest,
        "bridge_funded_run_differs",
    )
    code = "bridge_funded_run_invalid"
    value, _ = canonical_json_object(result_json, code)
    gdelt = value.get("gdelt")
    persistence = gdelt.get("persistence") if type(gdelt) is dict else None
    _refuse(type(persistence) is dict and persistence.get("complete") is True, code)
    run = persistence.get("run_id")
    _refuse(type(run) is str and FUNDED_RUN_ID.fullmatch(run) is not None, code)
    return run


def _aware(value):
    _require(isinstance(value, datetime) and value.utcoffset() is not None)
    return native_instant_key(value.astimezone(UTC).isoformat())


def resolve_funded_collection(
    item, *, read_chain, read_funded_execution, window_start, window_end, generation_loader=None
):
    """Bind one collection receipt of an approval ledger capture to its funded execution.

    ``read_chain(consumption_id)`` returns the approval ledger rows of the receipt's result
    reference, which must decode as one sound Wave 1 pilot chain under a trusted generation
    whose result is the reference's, every field, and succeeded. The result must be that
    execution's funded close, its digest the receipt's, and the receipt's run id that
    execution's id. ``read_funded_execution`` reads the execution natively; it must be that
    execution of the funded job, run as the funded identity from the approved image, and
    must have succeeded by the strict terminal read inside the capture window
    [``window_start``, ``window_end``], started before ``window_start`` plus one day, with
    the approval's consumption, the collection and the recorded result inside it. Returns the execution id, the pipeline run id the funded
    close names and that close's bytes.
    """
    _require(type(item) is dict and set(item) == COLLECTION)
    start, end = _aware(window_start), _aware(window_end)
    reference = item["result_ref"]
    _ref(reference)
    _refuse(reference["operation"] == FUNDED_OPERATION, "bridge_funded_result_not_funded")
    try:
        rows = read_chain(reference["consumption_id"])
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("bridge_funded_result_unavailable") from error
    try:
        checked = decode_context_result_rows_v2(
            rows,
            consumption_id=reference["consumption_id"],
            operation=FUNDED_OPERATION,
            generation_loader=generation_loader,
        )
    except ValueError as error:
        raise ValueError("bridge_funded_result_invalid") from error
    result, consumption = checked["result"], checked["consumption"]
    _refuse(
        all(result[name] == value for name, value in reference.items()),
        "bridge_funded_result_differs",
    )
    _refuse(result["status"] == "succeeded", "bridge_funded_result_not_succeeded")
    execution_name = consumption.execution_name
    _refuse(
        execution_name == FUNDED_EXECUTION_PREFIX + item["run_id"]
        and result["result_reference"] == execution_name + FUNDED_RESULT_SUFFIX
        and item["collection_receipt_digest"] == result["result_digest"],
        "bridge_funded_receipt_differs",
    )
    view = read_funded_execution(item["run_id"])
    _refuse(type(view) is dict, "bridge_funded_execution_invalid")
    _refuse(view.get("execution_name") == execution_name, "bridge_funded_execution_differs")
    _refuse("terminal_state" in view, "bridge_funded_execution_running")
    _refuse(view["terminal_state"] == "succeeded", "bridge_funded_execution_not_succeeded")
    _refuse(
        view.get("service_identity") == FUNDED_IDENTITY
        and view.get("image_uri") == consumption.image_uri,
        "bridge_funded_execution_differs",
    )
    started = native_instant_key(view["execution_started_at"])
    completed = native_instant_key(view["completed_at"])
    # The execution proves the one collection day whose window closed at ``window_start``:
    # it started before the next day's window closed, so it cannot date a later day earlier.
    _refuse(
        start <= started < _aware(window_start + timedelta(days=1)) and completed <= end,
        "bridge_funded_execution_outside_window",
    )
    code = "bridge_funded_collection_outside_execution"
    _refuse(
        started <= _aware(consumption.consumed_at)
        and started <= _aware(_parse_timestamp(item["collection_started_at"], code))
        and _aware(_parse_timestamp(item["collection_completed_at"], code))
        <= _aware(result["completed_at"])
        <= completed,
        code,
    )
    return {
        "execution_id": item["run_id"],
        "pipeline_run_id": funded_pipeline_run(
            result["canonical_result_json"], item["collection_receipt_digest"]
        ),
        "result_json": result["canonical_result_json"],
    }


def resolve_capture_bridge_evidence(
    artifacts, *, profile, read_chain, read_funded_execution, generation_loader=None
):
    """Bind a route A capture's approved evidence natively before its approval is spent.

    ``artifacts`` are the approved bytes of the four bridge artifacts. Every collection
    receipt must bind, by ``resolve_funded_collection``, to the funded pilot's own result
    row on the approval ledger (``read_chain(consumption_id)``) and to that execution read
    from Cloud Run (``read_funded_execution(execution_id)``), inside the capture window the
    approved profile names: from its observation window end to its collection snapshot
    instant. The approval ledger route has no native read of a daily completion, so any
    history entry other than ``unavailable`` refuses before any read. Returns the bindings.
    """
    _require(type(artifacts) is dict and set(artifacts) == {"bridge_policy", *VERSIONS})
    values = {
        name: canonical_json_object(raw, "bridge_evidence_invalid")[0]
        for name, raw in artifacts.items()
    }
    parsed = parse_bridge_artifacts(values)
    _refuse(
        all(
            entry["state"] == "unavailable" for entry in parsed["history_completion_set"]["entries"]
        ),
        "bridge_history_evidence_unavailable",
    )
    _require(type(profile) is dict)
    window_start = _parse_timestamp(
        profile.get("observation_window_end"), "bridge_evidence_invalid"
    )
    window_end = _parse_timestamp(
        profile.get("collection_snapshot_as_of"), "bridge_evidence_invalid"
    )
    return tuple(
        resolve_funded_collection(
            item,
            read_chain=read_chain,
            read_funded_execution=read_funded_execution,
            window_start=window_start,
            window_end=window_end,
            generation_loader=generation_loader,
        )
        for item in parsed["collection_receipt_set"]["receipts"]
    )


def resolve_completion(
    reference, *, operation_id, read_chain, read_completion, generation_loader=None
):
    raise ValueError("bridge_payload_contract_unavailable")


def check_daily_completion_binding(
    entry, *, derivation_id, operation_id, clients, read_completion, read_product_rows
):
    """Check one completed or empty history entry against native evidence.

    The daily chain names the composition attempt, the retained completion record names the
    product outcome, and ``read_product_rows`` is a native readback of that product's rows for
    the entry's date, digested exactly as the completion readback digested them. Only lanes the
    completion record carries can be admitted; every other lane stays unavailable.
    """
    _require(type(entry) is dict and set(entry) == HISTORY)
    _require(entry["state"] in ("completed", "empty"))
    _require(entry["lane"] in RECORDED_HISTORY_LANES and entry["market"] in MARKETS)
    _require(type(entry["product_date"]) is str)
    for field in ("completed_at", "available_at"):
        _parse_timestamp(entry[field], "bridge_evidence_invalid")
    chain = resolve_daily_result_ref(
        entry["result_ref"], derivation_id=derivation_id, clients=clients
    )
    context = chain.operation_context
    _require(context["operation"] == "daily_composition_apply")
    cutoff = native_instant_key(context["cutoff_utc"])
    day = date.fromisoformat(entry["product_date"])
    _require(context["cutoff_utc"] == f"{day + timedelta(days=1)}T00:00:00+00:00")
    found = read_completion(operation_id)
    _require(found is not None)
    record = validate_completion(found)
    _require(
        record["operation_id"] == operation_id
        and record["business_attempt_id"] == chain.derivation["business_attempt_id"]
        and record["image_uri"] == context["child_image_uri"]
        and native_instant_key(record["cutoff_utc"]) == cutoff
    )
    _require(canonical_digest(record) == entry["product_receipt_digest"])
    product = record["products"][entry["lane"]]
    rows = canonical_rows(read_product_rows(entry["lane"], entry["product_date"]))
    _require(canonical_digest(rows) == product["output_digest"])
    _require(len(rows) == product["row_count"])
    _require(all(row.get("market") in MARKETS for row in rows))
    selected = [row for row in rows if row["market"] == entry["market"]]
    _require(entry["row_count"] == len(selected))
    _require(entry["output_digest"] == canonical_digest(selected))
    _require(entry["state"] == ("completed" if selected else "empty"))
    completed = native_instant_key(chain.result["completed_at"])
    _require(native_instant_key(entry["completed_at"]) == completed)
    available = native_instant_key(chain.effective_available_at)
    _require(native_instant_key(entry["available_at"]) == available)
    _require(native_instant_key(chain.consumption["consumed_at"]) <= completed)
    return {"chain": chain, "completion": record, "rows": selected}


def check_daily_source_run_binding(item, *, derivation_id, clients, read_source_run):
    """Check native identity and time bounds; this does not admit a payload contract."""
    chain = resolve_daily_result_ref(
        item["result_ref"], derivation_id=derivation_id, clients=clients
    )
    _require(chain.operation_context["operation"] == "daily_source_collection")
    run = read_completed_source_run(read_source_run, item["run_id"])
    _require(run["receipt"]["execution_id"] == chain.derivation["business_attempt_id"])
    _require(run["receipt"]["image_uri"] == chain.operation_context["child_image_uri"])
    _require(canonical_digest(run["receipt"]) == item["collection_receipt_digest"])
    for field in ("collection_started_at", "collection_completed_at"):
        _require(run[field] == _parse_timestamp(item[field], "bridge_evidence_invalid"))
    _require(run["observation_window_end"].isoformat() == chain.operation_context["cutoff_utc"])
    _require(
        native_instant_key(chain.consumption["consumed_at"])
        <= native_instant_key(run["collection_started_at"].isoformat())
        <= native_instant_key(run["collection_completed_at"].isoformat())
        <= native_instant_key(chain.native_execution["completed_at"])
    )
    return run


# The manifest inputs the bridge loader reads on either route: the daily chain route reads
# the four bridge artifacts and the plan, the ledger route also the metadata and policy.
BRIDGE_INPUT_NAMES = frozenset(
    (
        "bridge_policy",
        "capture_plan",
        "collection_receipt_set",
        "history_completion_set",
        "source_metadata",
        "storage_policy",
        "temporal_rules",
    )
)


def _refuse(condition, code):
    if not condition:
        raise ValueError(code)


def _hex64(value):
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _unique_json(raw, code):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _refuse(key not in value, code)
            value[key] = item
        return value

    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(code) from error


def bridge_input_reader(read_object):
    """``read_input(name, digest)`` for the bridge loader over one native object reader.

    Only the inputs the loader reads are served, and bytes are returned only when they hash
    to the digest the approved manifest names; the loader checks that digest again.
    """

    def read_input(name, digest):
        _refuse(type(name) is str and name in BRIDGE_INPUT_NAMES, "bridge_input_invalid")
        _refuse(_hex64(digest), "bridge_input_invalid")
        try:
            raw = read_object(name, digest)
        except Exception as error:
            raise ValueError("bridge_input_unavailable") from error
        _refuse(
            type(raw) is bytes and hashlib.sha256(raw).hexdigest() == digest,
            "bridge_input_invalid",
        )
        return raw

    return read_input


def bridge_grant_reader(read_approval_rows):
    """``read_grant(digest)`` for the daily chain route over the approval ledger.

    The daily chain names its recurring grant by the digest of the grant's immutable terms,
    and the chain statement joins the approval whose manifest is that grant. The approval
    rows are read by that digest, and the grant is returned only when its own terms hash to
    it; the read binding validator checks the digest against the chain again.
    """
    from .recurring_grant import grant_digest

    code = "bridge_grant_invalid"

    def read_grant(digest):
        _refuse(_hex64(digest), code)
        try:
            rows = read_approval_rows(digest)
        except Exception as error:
            raise ValueError("bridge_grant_unavailable") from error
        _refuse(type(rows) is list and len(rows) == 1 and type(rows[0]) is dict, code)
        row = rows[0]
        _refuse(set(row) == {"approval_count", "approval_json"}, code)
        _refuse(type(row["approval_count"]) is int and row["approval_count"] == 1, code)
        _refuse(type(row["approval_json"]) is str, code)
        approval = _unique_json(row["approval_json"], code)
        _refuse(type(approval) is dict and approval.get("manifest_sha256") == digest, code)
        grant, _ = canonical_json_object(approval.get("canonical_manifest_json"), code)
        try:
            recomputed = grant_digest(grant)
        except (TypeError, ValueError) as error:
            raise ValueError(code) from error
        _refuse(recomputed == digest, code)
        return grant

    return read_grant


class BridgeEvidence:
    """The evidence object the bridge loader takes, resolved over native readers.

    ``locate_chain(result_ref)`` names the daily derivation that recorded a result and the
    chain read clients it is read through; the loader's checks read that chain and compare
    every reference field with it, so the locator is trusted for nothing but the lookup.
    The completion operation is the composition chain's own slot, never a value taken from
    the history entry it checks. Product rows are read once per lane and day.
    """

    def __init__(
        self,
        *,
        locate_chain,
        read_source_run,
        read_completion,
        read_product_rows,
        read_funded_execution=None,
    ):
        self._locate = locate_chain
        self._read_source_run = read_source_run
        self._read_completion = read_completion
        self._read_product_rows = read_product_rows
        self._read_funded_execution = read_funded_execution
        self._rows = {}

    def read_funded_execution(self, execution_id):
        """The native view of one funded pilot execution, for the ledger route's receipts.

        A coded refusal of the reader is kept; any other failure, an absent reader
        included, is unavailable.
        """
        _text(execution_id)
        try:
            return self._read_funded_execution(execution_id)
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("bridge_funded_execution_unavailable") from error

    def chain_source(self, reference):
        _ref(reference)
        located = self._locate(dict(reference))
        _require(type(located) is tuple and len(located) == 2)
        derivation_id, clients = located
        _require(type(derivation_id) is str and DERIVATION_ID.fullmatch(derivation_id) is not None)
        _require(clients is not None)
        return derivation_id, clients

    def read_source_run(self, run_id):
        _text(run_id)
        try:
            return self._read_source_run(run_id)
        except Exception as error:
            raise ValueError("bridge_source_run_unavailable") from error

    def completion_operation(self, reference):
        derivation_id, clients = self.chain_source(reference)
        chain = resolve_daily_result_ref(reference, derivation_id=derivation_id, clients=clients)
        context = chain.operation_context
        _require(context["operation"] == "daily_composition_apply")
        _digest(context["slot_id"])
        return context["slot_id"]

    def read_completion(self, operation_id):
        _text(operation_id)
        try:
            return self._read_completion(operation_id)
        except Exception as error:
            raise ValueError("bridge_completion_unavailable") from error

    def read_product_rows(self, lane, day):
        _require(type(lane) is str and lane in RECORDED_HISTORY_LANES)
        _require(type(day) is str and len(day) == 10)
        try:
            _require(date.fromisoformat(day).isoformat() == day)
        except ValueError as error:
            raise ValueError("bridge_evidence_invalid") from error
        key = (lane, day)
        if key not in self._rows:
            rows = self._read_product_rows(lane, day)
            _require(type(rows) is list and all(type(row) is dict for row in rows))
            self._rows[key] = rows
        return copy.deepcopy(self._rows[key])
