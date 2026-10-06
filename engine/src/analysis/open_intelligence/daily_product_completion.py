"""Versioned product evidence, separate from the dynamic composition receipt."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime

from .brain_contract import canonical_bytes, canonical_digest
from .daily_execution_contracts import validate_result_metering
from .daily_product_io import MODEL_PRODUCTS, WRITE_KEYS

PRODUCT_NAMES = (
    "seed_insights",
    "seed_candidates",
    "v_desk_dynamic_signals_v2",
    "trend_analysis",
    "daily_summary",
    "v_seed_first_seen",
    "creator_briefs",
    "pan_african_stories",
)
VERSION = "42_daily_products_completion_v1"
FIELDS = frozenset(
    {
        "schema_version",
        "operation_id",
        "business_attempt_id",
        "capture_digest",
        "policy_digest",
        "source_sha",
        "image_uri",
        "cutoff_utc",
        "dynamic_receipt_digest",
        "max_model_calls",
        "products",
        "metering",
    }
)
EMPTY_DIGEST = canonical_digest([])
PREFIX = "42/daily/products"
UNFUNDABLE = frozenset(MODEL_PRODUCTS)
WRITE_VERSION = "42_daily_product_write_v1"
_WRITE_TABLES = frozenset(WRITE_KEYS)


def _identifier(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value) is None
    ):
        raise ValueError("product_identifier_invalid")
    return value


def _digest(value, size=64):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{" + str(size) + "}", value) is None:
        raise ValueError("product_digest_invalid")


def validate_completion(value):
    if not isinstance(value, Mapping) or set(value) != FIELDS or value["schema_version"] != VERSION:
        raise ValueError("completion_contract_invalid")
    _identifier(value["operation_id"])
    _identifier(value["business_attempt_id"])
    for name in ("capture_digest", "policy_digest", "dynamic_receipt_digest"):
        _digest(value[name])
    _digest(value["source_sha"], 40)
    if not isinstance(value["image_uri"], str) or "@sha256:" not in value["image_uri"]:
        raise ValueError("completion_image_invalid")
    _digest(value["image_uri"].rsplit("@sha256:", 1)[1])
    try:
        cutoff = datetime.fromisoformat(value["cutoff_utc"])
    except (TypeError, ValueError) as error:
        raise ValueError("completion_cutoff_invalid") from error
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("completion_cutoff_invalid")
    allowance = value["max_model_calls"]
    if type(allowance) is not int or allowance < 0:
        raise ValueError("completion_contract_invalid")
    products = value["products"]
    if not isinstance(products, Mapping) or set(products) != set(PRODUCT_NAMES):
        raise ValueError("product_set_inexact")
    for name, product in products.items():
        if not isinstance(product, Mapping) or set(product) != {
            "state",
            "row_count",
            "output_digest",
            "readback_digest",
        }:
            raise ValueError("product_outcome_invalid")
        count = product["row_count"]
        if type(count) is not int or count < 0:
            raise ValueError("product_count_invalid")
        expected_state = (
            "awaiting_release"
            if name == "v_desk_dynamic_signals_v2"
            else ("completed" if count else "empty")
        )
        if product["state"] == "unfunded":
            # Only a model backed product can be unfunded, only with no rows, and only for
            # an admitted origin that funds no model call at all.
            if name not in UNFUNDABLE or count != 0 or allowance != 0:
                raise ValueError("product_outcome_invalid")
        elif product["state"] != expected_state:
            raise ValueError("product_outcome_invalid")
        _digest(product["output_digest"])
        _digest(product["readback_digest"])
        if name == "v_desk_dynamic_signals_v2":
            # Before release the view is bound to the dynamic receipt and reads back empty.
            if (
                count != 0
                or product["output_digest"] != value["dynamic_receipt_digest"]
                or product["readback_digest"] != EMPTY_DIGEST
            ):
                raise ValueError("product_readback_differs")
        elif product["output_digest"] != product["readback_digest"]:
            raise ValueError("product_readback_differs")
    metering = value["metering"]
    if not isinstance(metering, Mapping):
        raise ValueError("product_metering_incomplete")
    rows_written = metering.get("rows_written")
    calls = metering.get("model_calls")
    if (
        type(rows_written) is not int
        or rows_written < 0
        or type(calls) is not int
        or not 0 <= calls <= allowance
    ):
        raise ValueError("product_metering_incomplete")
    validate_result_metering(
        {name: item for name, item in metering.items() if name != "rows_written"},
        terminal_state="succeeded",
        effect_state="effects_recorded",
        spend_state="measured",
    )
    if value["metering"]["complete"] is not True:
        raise ValueError("product_metering_incomplete")
    return json.loads(canonical_bytes(dict(value)))


class CompletionStore:
    def __init__(self, objects):
        self._objects = objects

    def _name(self, operation_id):
        return f"{PREFIX}/{_identifier(operation_id)}/completion-v1.json"

    def _create(self, name, value, code):
        raw = canonical_bytes(value)
        found = self._objects.read(name)
        if found is None:
            try:
                self._objects.write(name, raw, if_generation_match=0)
            except Exception:
                found = self._objects.read(name)
                if found is None:
                    raise
            else:
                found = self._objects.read(name)
        if found is None or found[0] != raw:
            raise ValueError(code)
        return json.loads(raw)

    def read(self, operation_id):
        found = self._objects.read(self._name(operation_id))
        if found is None:
            return None
        value = validate_completion(json.loads(found[0]))
        if value["operation_id"] != operation_id:
            raise ValueError("completion_binding_differs")
        return value

    def record(self, value):
        checked = validate_completion(value)
        return self._create(self._name(checked["operation_id"]), checked, "completion_conflict")

    def require(self, operation_id, *, expected_digest=None, **bindings):
        value = self.read(operation_id)
        if value is None:
            raise ValueError("completion_missing")
        if expected_digest is not None and canonical_digest(value) != expected_digest:
            raise ValueError("completion_digest_differs")
        if any(value.get(name) != expected for name, expected in bindings.items()):
            raise ValueError("completion_binding_differs")
        return value

    def begin_attempt(self, operation_id, attempt_id, request):
        prefix = f"{PREFIX}/{_identifier(operation_id)}/attempts/{_identifier(attempt_id)}"
        intent = {
            "schema_version": "42_daily_product_attempt_v1",
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "request": request,
        }
        found = self._objects.read(prefix + "/intent.json")
        if found is not None:
            if found[0] != canonical_bytes(intent):
                raise ValueError("paid_attempt_conflict")
            result = self._objects.read(prefix + "/result.json")
            if result is None:
                raise ValueError("paid_attempt_unknown")
            value = json.loads(result[0])
            if value.get("intent_digest") != canonical_digest(intent):
                raise ValueError("paid_attempt_conflict")
            return value["result"]
        raw = canonical_bytes(intent)
        # Losing creation cannot authorize a second dispatch, even with identical bytes.
        self._objects.write(prefix + "/intent.json", raw, if_generation_match=0)
        readback = self._objects.read(prefix + "/intent.json")
        if readback is None or readback[0] != raw:
            raise ValueError("paid_attempt_unknown")
        return None

    def finish_attempt(self, operation_id, attempt_id, result):
        prefix = f"{PREFIX}/{_identifier(operation_id)}/attempts/{_identifier(attempt_id)}"
        found = self._objects.read(prefix + "/intent.json")
        if found is None:
            raise ValueError("paid_attempt_intent_missing")
        value = {
            "schema_version": "42_daily_product_attempt_result_v1",
            "intent_digest": canonical_digest(json.loads(found[0])),
            "result": result,
        }
        return self._create(prefix + "/result.json", value, "paid_attempt_conflict")

    def _write_name(self, operation_id, table, sequence):
        if table not in _WRITE_TABLES or type(sequence) is not int or sequence < 1:
            raise ValueError("product_write_record_invalid")
        return f"{PREFIX}/{_identifier(operation_id)}/writes/{table}/{sequence}.json"

    def record_write(self, operation_id, table, sequence, *, columns, keys, digests):
        """Record, create only, the rows one write is about to put in the shared day."""
        value = {
            "schema_version": WRITE_VERSION,
            "operation_id": operation_id,
            "table": table,
            "sequence": sequence,
            "columns": list(columns),
            "keys": [list(key) for key in keys],
            "digests": list(digests),
        }
        return self._create(
            self._write_name(operation_id, table, sequence), value, "product_write_conflict"
        )

    def prior_writes(self, operation_id, table):
        """Every write record of one table this operation made, in order."""
        records = []
        while True:
            sequence = len(records) + 1
            found = self._objects.read(self._write_name(operation_id, table, sequence))
            if found is None:
                return records
            value = json.loads(found[0])
            if (
                not isinstance(value, Mapping)
                or set(value)
                != {
                    "schema_version",
                    "operation_id",
                    "table",
                    "sequence",
                    "columns",
                    "keys",
                    "digests",
                }
                or value["schema_version"] != WRITE_VERSION
                or (value["operation_id"], value["table"], value["sequence"])
                != (operation_id, table, sequence)
                or not isinstance(value["columns"], list)
                or not isinstance(value["keys"], list)
                or not isinstance(value["digests"], list)
            ):
                raise ValueError("product_write_record_invalid")
            for digest in value["digests"]:
                _digest(digest)
            records.append(value)

    def record_release(self, value):
        checked = self._release_value(value)
        self.require(checked["operation_id"], expected_digest=checked["composition_digest"])
        name = f"{PREFIX}/{_identifier(checked['operation_id'])}/release-v1.json"
        return self._create(name, checked, "products_release_conflict")

    @staticmethod
    def _release_value(value):
        fields = {
            "schema_version",
            "operation_id",
            "composition_digest",
            "release_record_digest",
            "run_id",
            "view_row_count",
            "view_rows_digest",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != fields
            or value["schema_version"] != "42_daily_products_release_v1"
        ):
            raise ValueError("products_release_contract_invalid")
        _identifier(value["operation_id"])
        _identifier(value["run_id"])
        for field in ("composition_digest", "release_record_digest", "view_rows_digest"):
            _digest(value[field])
        if type(value["view_row_count"]) is not int or value["view_row_count"] < 0:
            raise ValueError("products_release_contract_invalid")
        return dict(value)

    def require_release(self, operation_id, *, expected_digest):
        found = self._objects.read(f"{PREFIX}/{_identifier(operation_id)}/release-v1.json")
        if found is None:
            raise ValueError("products_release_missing")
        value = self._release_value(json.loads(found[0]))
        if value["operation_id"] != operation_id or canonical_digest(value) != expected_digest:
            raise ValueError("products_release_digest_differs")
        self.require(operation_id, expected_digest=value["composition_digest"])
        return value
