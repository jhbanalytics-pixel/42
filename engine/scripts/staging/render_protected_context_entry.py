"""Derive a protected context registry entry from a completed capture's ledger receipt.

Two receipts are read, both exactly as the ledger returns them, and neither is written:

* an execution-result-v1 ledger row, whose result_contract_version names the contract.
  JSON completed_at values must include an ISO timezone. Its manifest, consumption and
  approval authority still require independent ledger review.
* the one row of the read-only v2 chain statement (``--chain-query`` prints it with its
  parameter), either as the row object or as the one element list ``bq --format=json``
  prints. The approval, consumption and result are typed under the generation pair they
  record, admitted only when the packaged catalogue of reviewed generations holds it; the
  chain is validated end to end, the cutoff is read from the approved manifest and the
  result must be a completed v2 capture at that cutoff.

A v2 chain row whose generation's capture policy is the v3 bridge policy renders a bridge
entry under the v2 result family instead: the cutoff and grant are the approved vector's,
the payload must be a complete v3 capture at that cutoff and the tables are the seven bridge
clones. The bridge reader still checks its inputs, native jobs and evidence before any read.

``render_bridge_entry`` derives a bridge v3 entry from the daily capture chain the native
chain reader issued for its derivation id; it takes no receipt file and no value from the
caller besides that chain.

Compact capture stdout envelopes are not accepted. ``--registry PATH`` prints the whole
registry with the derived entry merged in cutoff order as canonical bytes, for the operator
to review and commit; an entry already present for that cutoff must be byte identical.

``--check-receipts FOLDER REGISTRY`` proves that every execution ledger bridge row of a
registry is the row its saved chain receipt renders. The pin commit that adds such a row
also adds, as ``FOLDER/<cutoff>.json``, the chain statement's one row exactly as ``bq``
printed it, so the row can be derived again from the receipt at any later commit.

Usage:
  render_protected_context_entry.py RECEIPT.json
  render_protected_context_entry.py RECEIPT.json --registry REGISTRY.json
  render_protected_context_entry.py --chain-query CONSUMPTION_ID
  render_protected_context_entry.py --check-receipts FOLDER REGISTRY.json
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.analysis.open_intelligence.execution_approval import _RESULT_ROW_FIELDS, _result_from_value
from src.analysis.open_intelligence.protected_context_registry import _entry

_PAYLOAD_FIELDS = frozenset(
    {
        "artifact_attempt",
        "capture_receipt_digest",
        "captured_at",
        "client_scope_id",
        "contract_version",
        "creation_records",
        "cutoff_date",
        "limitations",
        "market_scope",
        "missing_checks",
        "query_count",
        "snapshot_digest",
        "snapshot_plan_digest",
        "source_as_of",
        "stored_artifact",
        "total_bytes_billed",
    }
)


_CHAIN_FIELDS = frozenset(
    {
        "approval_count",
        "consumption_count",
        "result_count",
        "approval_json",
        "consumption_json",
        "result_json",
    }
)


def chain_query(consumption_id):
    """The read-only v2 chain statement and its one parameter, for the operator to run."""
    from src.analysis.open_intelligence.general_question_context_queries import _RESULT_SQL_V2

    if type(consumption_id) is not str or re.fullmatch(r"exc_[0-9a-f]{64}", consumption_id) is None:
        raise ValueError("capture_receipt_invalid")
    return {
        "sql": _RESULT_SQL_V2,
        "parameters": [
            {
                "name": "consumption_id",
                "parameterType": {"type": "STRING"},
                "parameterValue": {"value": consumption_id},
            }
        ],
    }


def _chain_row(receipt):
    if type(receipt) is list and len(receipt) == 1:
        receipt = receipt[0]
    if type(receipt) is not dict or set(receipt) != _CHAIN_FIELDS:
        raise ValueError("capture_receipt_invalid")
    row = dict(receipt)
    for key in ("approval_count", "consumption_count", "result_count"):
        value = row[key]
        if type(value) is str and re.fullmatch(r"0|[1-9][0-9]*", value):
            row[key] = int(value)
        elif type(value) is not int:
            raise ValueError("capture_receipt_invalid")
    return row


def _render_v2(receipt, generation_loader):
    from src.analysis.open_intelligence import execution_approval
    from src.analysis.open_intelligence.bridge_ledger_binding import is_bridge_ledger_generation
    from src.analysis.open_intelligence.general_question_context_queries import (
        decode_context_result_rows_v2,
    )
    from src.analysis.open_intelligence.production_snapshot_tables import (
        retained_v2_capture_result,
    )
    from src.analysis.open_intelligence.protected_context_registry import RESULT_VERSION_V2

    code = "capture_receipt_invalid"
    row = _chain_row(receipt)
    try:
        consumption_id = json.loads(row["consumption_json"])["consumption_id"]
        checked = decode_context_result_rows_v2(
            [row],
            consumption_id=consumption_id,
            operation="source_snapshot_capture",
            generation_loader=generation_loader,
        )
        registry = checked["registry"]
        manifest = execution_approval.validate_execution_manifest(
            json.loads(checked["approval"].canonical_manifest_json),
            mode="historical_read",
            registry=registry,
        )
        if is_bridge_ledger_generation(registry):
            return _render_bridge_ledger([row], consumption_id, generation_loader)
        origin = execution_approval._v2_origin_for_operation(
            "source_snapshot_capture", mode="historical_read", registry=registry
        )
        policy = json.loads(
            execution_approval._policy_bytes_for_origin(registry=registry, origin=origin)
        )
        vector = execution_approval.read_source_snapshot_capture_arguments_v2(
            manifest.arguments,
            rule=policy["operation_validation"]["source_snapshot_capture"]["arguments"],
        )
        result, payload = retained_v2_capture_result(
            checked["result"],
            code,
            cutoff=vector.cutoff_date,
            generation_loader=generation_loader,
        )
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError(code) from error
    cutoff = vector.cutoff_date.isoformat()
    entry = {key: getattr(result, key) for key in _PINS}
    entry.update(
        cutoff_date=cutoff,
        profile_id="protected_context_" + cutoff.replace("-", "") + "_v2",
        source_as_of=payload["observation_window_end"],
        market_scope=payload["market_scope"],
        snapshot_tables=[record["destination"] for record in payload["creation_records"]],
        result_contract_version=RESULT_VERSION_V2,
    )
    return entry


_PINS = ("consumption_id", "manifest_sha256", "result_digest", "result_id")


def _render_bridge_ledger(rows, consumption_id, generation_loader):
    """A bridge entry for one consumed capture under a generation with the bridge policy.

    The cutoff and grant are the approved vector's; the payload must be a complete v3
    capture at that cutoff whose clones are the seven bridge tables. The pins are the
    ledger result's own. The reader still checks every input, native job and evidence.
    """
    from datetime import UTC, time, timedelta

    from src.analysis.open_intelligence.bridge_ledger_binding import ledger_capture_result
    from src.analysis.open_intelligence.protected_context_registry import RESULT_VERSION_V2

    checked = ledger_capture_result(
        rows, consumption_id=consumption_id, generation_loader=generation_loader
    )
    day = checked["arguments"].cutoff_date
    payload = checked["payload"]
    entry = {key: checked["result"][key] for key in _PINS}
    entry.update(
        cutoff_date=day.isoformat(),
        market_scope=list(payload["market_scope"]),
        profile_id=payload["profile_id"],
        result_contract_version=RESULT_VERSION_V2,
        snapshot_tables=[record["destination_table"] for record in payload["creation_records"]],
        source_as_of=datetime.combine(day + timedelta(days=1), time.min, UTC).isoformat(),
    )
    return entry


def render_entry(receipt, *, generation_loader=None):
    """Render the entry a completed capture's ledger receipt derives, as canonical text."""
    if (type(receipt) is dict and set(receipt) == _CHAIN_FIELDS) or type(receipt) is list:
        entry = _render_v2(receipt, generation_loader)
    else:
        entry = _render_v1(receipt)
    try:
        _entry(entry)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        raise ValueError("capture_receipt_invalid") from error
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def render_registry(receipt, registry_bytes, *, generation_loader=None):
    """The registry bytes with the derived entry merged in cutoff order, canonical."""
    entry = json.loads(render_entry(receipt, generation_loader=generation_loader))
    return _merge(entry, registry_bytes)


def render_bridge_entry(chain):
    """A bridge v3 entry derived from one issued daily capture chain and nothing else.

    ``chain`` is what ``read_daily_execution_chain`` issued for the capture's derivation.
    The pins and the derivation id are the chain's own records, the cutoff is the one its
    operation context names, and the payload must be a complete v3 capture at that cutoff.
    """
    from datetime import UTC, timedelta

    from src.analysis.open_intelligence.protected_context_registry import (
        RESULT_VERSION_V3,
        bridge_snapshot_tables,
    )
    from src.analysis.open_intelligence.source_estate_bridge import _capture_result

    try:
        payload = _capture_result(chain)
        cutoff = datetime.fromisoformat(chain.operation_context["cutoff_utc"])
        if cutoff.utcoffset() != timedelta(0):
            raise ValueError("capture_receipt_invalid")
        cutoff = cutoff.astimezone(UTC)
        day = cutoff.date() - timedelta(days=1)
        tables = bridge_snapshot_tables(day)
        if (
            payload["cutoff_date"] != day.isoformat()
            or payload["profile_id"] != f"staging_bridge_v3_{day:%Y%m%d}"
            or datetime.fromisoformat(payload["observation_window_end"]) != cutoff
            or [row["destination_table"] for row in payload["relation_bindings"]] != tables
        ):
            raise ValueError("capture_receipt_invalid")
        entry = {key: chain.result[key] for key in _PINS}
        entry.update(
            cutoff_date=day.isoformat(),
            derivation_id=chain.derivation["derivation_id"],
            market_scope=list(payload["market_scope"]),
            profile_id=payload["profile_id"],
            result_contract_version=RESULT_VERSION_V3,
            snapshot_tables=tables,
            source_as_of=cutoff.isoformat(),
        )
        _entry(entry)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("capture_receipt_invalid") from error
    return entry


def render_bridge_registry(chain, registry_bytes):
    """The registry bytes with the chain's bridge entry merged in cutoff order, canonical."""
    return _merge(render_bridge_entry(chain), registry_bytes)


def _merge(entry, registry_bytes):
    from src.analysis.open_intelligence.protected_context_registry import parse_registry_bytes

    try:
        document, _records = parse_registry_bytes(registry_bytes)
    except ValueError as error:
        raise ValueError("capture_receipt_invalid") from error
    present = [item for item in document["entries"] if item["cutoff_date"] == entry["cutoff_date"]]
    if present:
        if present != [entry]:
            raise ValueError("capture_receipt_invalid")
        return registry_bytes
    document["entries"] = sorted(
        [*document["entries"], entry], key=lambda item: item["cutoff_date"]
    )
    merged = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    try:
        parse_registry_bytes(merged)
    except ValueError as error:
        raise ValueError("capture_receipt_invalid") from error
    return merged


_RECEIPT_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})\.json")


def check_registry_receipts(registry_bytes, receipts, *, generation_loader=None):
    """The cutoffs of a registry's execution ledger bridge rows, each proved by its receipt.

    ``receipts`` is the folder of saved chain receipts, one ``<cutoff>.json`` per ledger
    row. Every ledger row needs its receipt and the receipt must render exactly that row; a
    file that names no ledger row is refused. A daily chain row is proved by its own chain
    and needs no receipt here. A missing folder holds no receipts.
    """
    from src.analysis.open_intelligence.protected_context_registry import (
        bridge_authority,
        parse_registry_bytes,
    )

    document, records = parse_registry_bytes(registry_bytes)
    ledger = {
        record.cutoff_date: item
        for record, item in zip(records, document["entries"], strict=True)
        if re.fullmatch(r"staging_bridge_v3_\d{8}", record.profile_id)
        and bridge_authority(record) == "execution_ledger"
    }
    folder = Path(receipts)
    saved = {}
    if folder.exists():
        for path in sorted(folder.iterdir()):
            match = _RECEIPT_NAME.fullmatch(path.name)
            if not path.is_file() or match is None or match.group(1) not in ledger:
                raise ValueError("protected_context_receipt_unexpected")
            saved[match.group(1)] = path
    if set(saved) != set(ledger):
        raise ValueError("protected_context_receipt_missing")
    for cutoff, item in ledger.items():
        try:
            rendered = render_entry(_read_json(saved[cutoff]), generation_loader=generation_loader)
        except ValueError as error:
            raise ValueError("protected_context_receipt_mismatch") from error
        if rendered != json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False):
            raise ValueError("protected_context_receipt_mismatch")
    return sorted(ledger)


def _render_v1(receipt):
    """A v1 proposal; copied authority pins require independent ledger review."""
    from src.analysis.open_intelligence.production_snapshot_tables import LANES

    if type(receipt) is not dict or set(receipt) != set(_RESULT_ROW_FIELDS):
        raise ValueError("capture_receipt_invalid")
    values = dict(receipt)
    try:
        if isinstance(values["completed_at"], str):
            values["completed_at"] = datetime.fromisoformat(values["completed_at"])
        result = _result_from_value(values, "capture_receipt_invalid")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("capture_receipt_invalid") from error
    if (
        result.operation != "source_snapshot_capture"
        or result.status != "succeeded"
        or result.result_reference != result.execution_name + "#source-snapshot"
    ):
        raise ValueError("capture_receipt_invalid")
    payload = json.loads(result.canonical_result_json)
    if (
        type(payload) is not dict
        or set(payload) != _PAYLOAD_FIELDS
        or payload["contract_version"] != "open_intelligence_protected_source_snapshot_v1"
        or type(payload["cutoff_date"]) is not str
        or type(payload["creation_records"]) is not list
        or len(payload["creation_records"]) != len(LANES)
    ):
        raise ValueError("capture_receipt_invalid")
    for lane, record in zip(LANES, payload["creation_records"], strict=True):
        if (
            type(record) is not dict
            or record.get("lane") != lane
            or record.get("state") != "succeeded"
            or type(record.get("destination")) is not str
        ):
            raise ValueError("capture_receipt_invalid")
    if payload["missing_checks"]:
        raise ValueError("capture_receipt_invalid")
    entry = {key: getattr(result, key) for key in _PINS}
    cutoff = payload["cutoff_date"]
    entry.update(
        cutoff_date=cutoff,
        profile_id="protected_context_" + cutoff.replace("-", "") + "_v1",
        source_as_of=payload["source_as_of"],
        market_scope=payload["market_scope"],
        snapshot_tables=[record["destination"] for record in payload["creation_records"]],
    )
    return entry


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("capture_receipt_invalid")
    return result


def _read_json(path):
    try:
        return json.loads(Path(path).read_bytes(), object_pairs_hook=_unique_fields)
    except (OSError, ValueError) as error:
        raise ValueError("capture_receipt_invalid") from error


def main(argv):
    if len(argv) == 2 and argv[0] == "--chain-query":
        sys.stdout.write(json.dumps(chain_query(argv[1]), sort_keys=True, indent=1) + "\n")
    elif len(argv) == 3 and argv[0] == "--check-receipts":
        try:
            registry_bytes = Path(argv[2]).read_bytes()
        except OSError as error:
            raise ValueError("capture_receipt_invalid") from error
        checked = check_registry_receipts(registry_bytes, Path(argv[1]))
        sys.stdout.write(json.dumps({"checked_cutoffs": checked}, sort_keys=True) + "\n")
    elif len(argv) == 1:
        print(render_entry(_read_json(argv[0])))
    elif len(argv) == 3 and argv[1] == "--registry":
        try:
            registry_bytes = Path(argv[2]).read_bytes()
        except OSError as error:
            raise ValueError("capture_receipt_invalid") from error
        sys.stdout.buffer.write(render_registry(_read_json(argv[0]), registry_bytes))
    else:
        raise ValueError("capture_receipt_invalid")


if __name__ == "__main__":
    main(sys.argv[1:])
