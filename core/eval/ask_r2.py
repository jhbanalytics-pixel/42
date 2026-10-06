from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
PROJECT = "ogilvy-trends-v2"
BUILDER_EMAIL = "f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com"
BUILDER_IMPERSONATION_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
    f"{BUILDER_EMAIL}:generateAccessToken"
)
GATE_SCHEMA = "r2-health-gate-v1"
REQUIRED_ANCESTORS = ("9f479c21", "96d10ab6", "16590012")
ORIGINAL_SOURCE_COMMIT = "77c8fcdf5da64ecfbc5816d298a053bda286a520"
GATE_MAX_AGE_SECONDS = 900
SESSION_CAP_USD = 10.0
QUESTION_IDS = ("NOW-01", "RISE-02", "WHY-03", "SPR-03", "CRE-01")
PREFIX_ROOT = "l3-ask-20260930-r2"
RESUME_BASELINE_USD = 1.995613
RESUME_ADDITIONAL_CAP_USD = 8.0
RESUME_TOTAL_CAP_USD = 9.995613
RISE_RETRY_KEY = "RISE-02-retry-1"
R3_NOW_RETRY_KEY = "NOW-01-retry-1"
R3_INITIAL_BOOKED_USD = 1.357150
R3_INITIAL_SOURCE_COMMIT = "15342e3c8faff2a1516443e55ca955403af37f0d"
R3_SOURCE_METADATA_SHA256 = "d70a5f142f98488163e278ac67b9172afce1d13cea0156f17de32cfb51522ab7"
R3_INITIAL_NOW_SHA256 = "c3d4c2d14a089a369e08178a301f93fdb0e8ce73fdf1cd466d5104b742e2d974"
R3_INITIAL_REPORT_SHA256 = "87e407759caa165f66b11dd1289a7174b279ba319c017e421ad2ddc6d9a97639"
CLAIM_TABLE = "intelligence_42_agent.claim_checks"
OUTPUT_PATH = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R2.md")
DATA_DIR = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-R2-data")
MORNING_DIR = Path("C:/Users/AlbertMeintjes/dev/42-inputs/ASK-2026-09-30-data")
REFUSED = 2
FAILED = 1
MICROS = 1_000_000
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_INPUT_BOUND_TOKENS = 2048
EMBEDDING_USD_PER_MILLION_INPUT_TOKENS = Decimal("0.15")
SEMANTIC_EMBEDDING_RESERVE_USD = (
    Decimal(EMBEDDING_INPUT_BOUND_TOKENS) * EMBEDDING_USD_PER_MILLION_INPUT_TOKENS / Decimal(MICROS)
)
_EXECUTION_TOKEN = object()


class OperationRefused(RuntimeError):
    pass


class BudgetRefused(OperationRefused):
    pass


@dataclass
class _Ticket:
    record: dict
    reserve_micros: int
    dispatched: bool = False
    settled: bool = False


def _micros(value) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BudgetRefused("model_price_invalid") from None
    if not amount.is_finite() or amount < 0:
        raise BudgetRefused("model_price_invalid")
    return int((amount * MICROS).to_integral_value(rounding=ROUND_CEILING))


class SessionBudget:
    def __init__(self, cap_usd=SESSION_CAP_USD, price_for=None, provider_500_retries=0):
        self.cap_micros = _micros(cap_usd)
        if self.cap_micros <= 0:
            raise BudgetRefused("session_cap_invalid")
        if type(provider_500_retries) is not int or provider_500_retries not in (0, 1):
            raise BudgetRefused("provider_retry_limit_invalid")
        self.provider_500_retries = provider_500_retries
        if price_for is None:
            from core.llm.provider import price_for as provider_price_for

            price_for = provider_price_for
        self.price_for = price_for
        self.charged_micros = 0
        self.reserved_micros = 0
        self.calls: list[dict] = []
        self.stop_reason: str | None = None
        self.provider_retry_statuses = (500,)
        self.capture_failed_structured_request = False

    @property
    def charged_usd(self) -> float:
        return self.charged_micros / MICROS

    def _refuse(self, record, reason):
        record["status"] = "refused_before_dispatch"
        record["stop_reason"] = reason
        self.calls.append(record)
        if reason != "prior_call_failure":
            self.stop_reason = reason
        raise BudgetRefused(reason)

    def reserve(self, phase, model, input_bound_tokens, output_reserve_tokens):
        record = {
            "phase": str(phase),
            "model": str(model),
            "input_bound_tokens": int(input_bound_tokens),
            "output_reserve_tokens": int(output_reserve_tokens),
            "reserved_usd": 0.0,
            "charged_usd": 0.0,
            "status": "pending",
        }
        if self.stop_reason:
            self._refuse(record, "prior_call_failure")
        if record["input_bound_tokens"] < 0 or record["output_reserve_tokens"] < 0:
            self._refuse(record, "model_price_invalid")
        try:
            price = self.price_for(model)
            input_rate = Decimal(str(price["input"]))
            output_rate = Decimal(str(price["output"]))
            if (not input_rate.is_finite() or not output_rate.is_finite()
                    or input_rate < 0 or output_rate < 0):
                raise InvalidOperation
            reserve_usd = (
                Decimal(record["input_bound_tokens"]) * input_rate
                + Decimal(record["output_reserve_tokens"]) * output_rate
            ) / 1_000_000
            reserve_micros = _micros(reserve_usd)
        except BudgetRefused:
            self._refuse(record, "model_price_invalid")
        except InvalidOperation:
            self._refuse(record, "model_price_invalid")
        except Exception:
            self._refuse(record, "model_price_unavailable")
        if self.charged_micros + self.reserved_micros + reserve_micros > self.cap_micros:
            self._refuse(record, "session_cap_exhausted")
        record["reserved_usd"] = reserve_micros / MICROS
        self.reserved_micros += reserve_micros
        self.calls.append(record)
        return _Ticket(record, reserve_micros)

    def reserve_fixed(self, phase, model, amount_usd, input_bound_tokens):
        record = {
            "phase": str(phase),
            "model": str(model),
            "input_bound_tokens": int(input_bound_tokens),
            "output_reserve_tokens": 0,
            "reserved_usd": 0.0,
            "charged_usd": 0.0,
            "status": "pending",
        }
        if self.stop_reason:
            self._refuse(record, "prior_call_failure")
        try:
            reserve_micros = _micros(amount_usd)
        except BudgetRefused:
            self._refuse(record, "model_price_invalid")
        if self.charged_micros + self.reserved_micros + reserve_micros > self.cap_micros:
            self._refuse(record, "session_cap_exhausted")
        record["reserved_usd"] = reserve_micros / MICROS
        self.reserved_micros += reserve_micros
        self.calls.append(record)
        return _Ticket(record, reserve_micros)

    def mark_dispatched(self, ticket):
        if ticket.settled:
            raise BudgetRefused("ticket_already_settled")
        ticket.dispatched = True
        ticket.record["status"] = "dispatched"

    def settle(self, ticket, actual_usd=None, unknown=False, actual_input_tokens=None, actual_output_tokens=None):
        if ticket.settled:
            raise BudgetRefused("ticket_already_settled")
        ticket.settled = True
        ticket.record["measured_input_tokens"] = (
            int(actual_input_tokens) if type(actual_input_tokens) is int and actual_input_tokens >= 0 else None)
        ticket.record["measured_output_tokens"] = (
            int(actual_output_tokens) if type(actual_output_tokens) is int and actual_output_tokens >= 0 else None)
        self.reserved_micros -= ticket.reserve_micros
        if not ticket.dispatched:
            ticket.record["status"] = "not_dispatched_zero"
            return 0.0
        try:
            if unknown or actual_usd is None:
                raise BudgetRefused("unknown_dispatched_cost")
            charged_micros = _micros(actual_usd)
        except BudgetRefused:
            charged_micros = ticket.reserve_micros
            ticket.record["status"] = "unknown_charged_ceiling"
            self.stop_reason = "unknown_dispatched_cost"
            actual_input_tokens = ticket.record["input_bound_tokens"]
            actual_output_tokens = ticket.record["output_reserve_tokens"]
            ticket.record["charged_input_bound_tokens"] = ticket.record["input_bound_tokens"]
            ticket.record["charged_output_bound_tokens"] = ticket.record["output_reserve_tokens"]
        if charged_micros > ticket.reserve_micros:
            ticket.record["status"] = "actual_cost_above_reserve"
            self.stop_reason = "actual_cost_above_reserve"
        self.charged_micros += charged_micros
        ticket.record["charged_usd"] = charged_micros / MICROS
        ticket.record["actual_input_tokens"] = int(
            ticket.record["input_bound_tokens"] if actual_input_tokens is None else actual_input_tokens)
        ticket.record["actual_output_tokens"] = int(
            ticket.record["output_reserve_tokens"] if actual_output_tokens is None else actual_output_tokens)
        if self.charged_micros + self.reserved_micros > self.cap_micros:
            ticket.record["status"] = "session_cap_breached"
            self.stop_reason = "session_cap_breached"
        elif ticket.record["status"] == "dispatched":
            ticket.record["status"] = "charged_known"
        return charged_micros / MICROS

    def continue_after_settled_server_error(self):
        if self.stop_reason != "unknown_dispatched_cost":
            raise BudgetRefused("settled_failure_not_continuable")
        self.stop_reason = None


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise OperationRefused("prompt_serialization_invalid")
        return value
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump(mode="json", exclude_none=True))
        except TypeError:
            return _jsonable(value.model_dump())
    if hasattr(value, "to_json"):
        try:
            return _jsonable(json.loads(value.to_json()))
        except Exception:
            raise OperationRefused("prompt_serialization_invalid") from None
    raise OperationRefused("prompt_serialization_invalid")


def _serialized_token_bound(value):
    return _serialized_request_stats(value)["request_payload_utf8_bytes"] + 1024


def _serialized_request_stats(value):
    serialized = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    encoded = serialized.encode("utf-8")
    return {
        "request_payload_utf8_bytes": len(encoded),
        "request_payload_characters": len(serialized),
        "request_payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "request_evidence_counts": _payload_collection_counts(json.loads(serialized)),
    }


def _payload_collection_counts(value):
    collection_names = {"evidence", "posts", "rows"}
    identifier_names = {"id", "evidence_id", "post_id", "row_id", "uuid"}
    counts = {name: {"entries": 0, "identified_entries": 0, "unique_ids": set()}
              for name in collection_names}
    found = set()

    def visit(item, *, schema=False):
        if isinstance(item, dict):
            for key, child in item.items():
                key_name = str(key).casefold()
                if schema or key_name in {"schema", "json_schema", "enum"}:
                    continue
                if key_name in collection_names and isinstance(child, list):
                    found.add(key_name)
                    collection = counts[key_name]
                    collection["entries"] += len(child)
                    for entry in child:
                        if isinstance(entry, dict):
                            identifier = next((entry.get(name) for name in identifier_names
                                               if entry.get(name) not in (None, "")), None)
                            if identifier is not None:
                                collection["identified_entries"] += 1
                                collection["unique_ids"].add(str(identifier))
                        visit(entry)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            stripped = item.lstrip()
            if stripped.startswith(("{", "[")):
                try:
                    decoded = json.loads(item)
                except (json.JSONDecodeError, RecursionError):
                    return
                if isinstance(decoded, (dict, list)):
                    visit(decoded)

    visit(value)
    return {
        "method": "payload_collection_scan_v1",
        "status": "measured" if found else "unknown",
        "collections": {
            name: {
                "entries": counts[name]["entries"],
                "identified_entries": counts[name]["identified_entries"],
                "unique_ids": len(counts[name]["unique_ids"]) if counts[name]["identified_entries"] else None,
            }
            for name in sorted(found)
        },
    }


def _known_exception_usd(exc):
    usage = getattr(exc, "usage", None)
    if isinstance(usage, dict) and usage.get("usd") is not None:
        return usage["usd"]
    return getattr(exc, "usd", None)


def _known_exception_usage(exc):
    usage = getattr(exc, "usage", None)
    return usage if isinstance(usage, dict) and usage.get("usd") is not None else None


def _safe_http_error_status(exc):
    for name in ("code", "status_code"):
        try:
            value = getattr(exc, name, None)
        except Exception:
            continue
        if type(value) is int and 400 <= value <= 599:
            return value
    return None


def _has_usage_metadata(response):
    try:
        metadata = getattr(response, "usage_metadata", None)
        if metadata is None:
            return False
        prompt_tokens = getattr(metadata, "prompt_token_count", None)
        return prompt_tokens is not None
    except Exception:
        return False


def _retry_provider_500(budget, ticket, exc, attempt_index):
    status = _safe_http_error_status(exc)
    ticket.record["provider_retry_index"] = attempt_index
    if ticket.dispatched:
        ticket.record["provider_error_type"] = type(exc).__name__
    if status is not None:
        ticket.record["provider_http_status"] = status
    if (not ticket.dispatched or status not in budget.provider_retry_statuses
            or attempt_index >= budget.provider_500_retries):
        ticket.record["retry_authorized"] = False
        return False
    if budget.stop_reason == "unknown_dispatched_cost":
        budget.continue_after_settled_server_error()
    if budget.stop_reason:
        ticket.record["retry_authorized"] = False
        return False
    ticket.record["retry_authorized"] = True
    return True


def _settle_conservative_ceiling(budget, ticket, charge_basis):
    charged = budget.settle(ticket, actual_usd=ticket.reserve_micros / MICROS)
    ticket.record["charge_basis"] = charge_basis
    ticket.record["status"] = "charged_conservative_ceiling"
    ticket.record["charged_input_bound_tokens"] = ticket.record["input_bound_tokens"]
    ticket.record["charged_output_bound_tokens"] = ticket.record["output_reserve_tokens"]
    return charged


def _attach_bounded_usage(exc, ticket, amount_usd):
    existing = _known_exception_usage(exc) or {}
    usage = {
        "input_tokens": existing.get("input_tokens"),
        "output_tokens": existing.get("output_tokens"),
        "usd": existing.get("usd", amount_usd),
        "charged_input_bound_tokens": ticket.record["input_bound_tokens"],
        "charged_output_bound_tokens": ticket.record["output_reserve_tokens"],
    }
    try:
        exc.usage = usage
        exc.usd = amount_usd
    except Exception:
        pass


def _call_totals(calls):
    model_calls = [call for call in calls if call.get("phase") in ("research", "structured")]
    measured = [call for call in model_calls
                if call.get("status") not in ("refused_before_dispatch", "not_dispatched_zero")]

    def total(field, legacy_field):
        values = [call.get(field, call.get(legacy_field)) for call in measured]
        return None if any(value is None for value in values) else sum(int(value) for value in values)

    return {
        "input_tokens": total("measured_input_tokens", "actual_input_tokens"),
        "output_tokens": total("measured_output_tokens", "actual_output_tokens"),
        "usd": sum(float(call.get("charged_usd") or 0) for call in model_calls),
    }


def _prevent_t0_fallback(ask_module):
    if not hasattr(ask_module, "hold_usd") or not hasattr(ask_module, "MODEL"):
        return lambda: None
    original_hold = ask_module.hold_usd
    requested_model = ask_module.MODEL

    def hold_usd(tier, model):
        if tier == "T0":
            return original_hold("T1", requested_model)
        return original_hold(tier, model)

    ask_module.hold_usd = hold_usd
    return lambda: setattr(ask_module, "hold_usd", original_hold)


class _ModelsProxy:
    def __init__(self, models, budget, ticket):
        self._models = models
        self._budget = budget
        self._ticket = ticket

    def generate_content(self, *args, **kwargs):
        self._budget.mark_dispatched(self._ticket)
        response = self._models.generate_content(*args, **kwargs)
        if not _has_usage_metadata(response):
            self._ticket.record["usage_metadata_missing"] = True
        return response

    def __getattr__(self, name):
        return getattr(self._models, name)


class _ClientProxy:
    def __init__(self, client, budget, ticket):
        self._client = client
        self.models = _ModelsProxy(client.models, budget, ticket)

    def __getattr__(self, name):
        return getattr(self._client, name)


class BudgetedGeminiModel:
    def __init__(self, model, budget, reserve_output=None):
        self.model = model
        self.budget = budget
        if reserve_output is None:
            from core.llm.provider import reserve_output as provider_reserve_output

            reserve_output = provider_reserve_output
        self.reserve_output = reserve_output

    def complete_json(self, **kwargs):
        model_name = kwargs["model"]
        request_payload = {
            "system": kwargs["system"],
            "user": kwargs["user"],
            "schema": kwargs["schema"],
        }
        request_stats = _serialized_request_stats(request_payload)
        input_bound = request_stats["request_payload_utf8_bytes"] + 1024
        schema_serialized = json.dumps(_jsonable(kwargs["schema"]), ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"), allow_nan=False).encode("utf-8")
        output_bound = self.reserve_output(model_name, int(kwargs["max_tokens"]))
        retry_source_index = None
        for attempt_index in range(self.budget.provider_500_retries + 1):
            ticket = self.budget.reserve("structured", model_name, input_bound, output_bound)
            ticket.record.update(request_stats)
            ticket.record["structured_schema_serialized_utf8_bytes"] = len(schema_serialized)
            ticket.record["client_timeout_s"] = getattr(self.model, "timeout_s", None)
            ticket.record["requested_max_tokens"] = int(kwargs["max_tokens"])
            ticket.record["effective_output_limit_tokens"] = output_bound
            ticket_index = len(self.budget.calls) - 1
            ticket.record["provider_retry_index"] = attempt_index
            if retry_source_index is not None:
                ticket.record["retry_of_call_index"] = retry_source_index
                self.budget.calls[retry_source_index]["authorized_retry_call_index"] = ticket_index
            original_client = None
            wrapped_client = False
            try:
                if not hasattr(self.model, "_client"):
                    raise BudgetRefused("model_client_unwrappable")
                original_client = self.model.client
                self.model._client = _ClientProxy(original_client, self.budget, ticket)
                wrapped_client = True
                result = self.model.complete_json(**kwargs)
            except BaseException as exc:
                if ticket.dispatched:
                    usage = _known_exception_usage(exc)
                    amount = _known_exception_usd(exc)
                    status = _safe_http_error_status(exc)
                    if (self.budget.capture_failed_structured_request
                            and status is not None and 500 <= status <= 599):
                        ticket.record["failed_structured_request"] = {
                            "system": kwargs["system"],
                            "user": kwargs["user"],
                            "schema": kwargs["schema"],
                            "model": model_name,
                            "max_tokens": int(kwargs["max_tokens"]),
                            "effective_output_limit_tokens": output_bound,
                            "schema_serialized_utf8_bytes": len(schema_serialized),
                        }
                    if ticket.record.get("usage_metadata_missing"):
                        charged = _settle_conservative_ceiling(
                            self.budget, ticket, "bounded_usage_missing")
                    else:
                        charged = self.budget.settle(
                            ticket,
                            actual_usd=amount,
                            unknown=amount is None,
                            actual_input_tokens=usage.get("input_tokens") if usage else None,
                            actual_output_tokens=usage.get("output_tokens") if usage else None,
                        )
                    if usage is None or "input_tokens" not in usage or "output_tokens" not in usage:
                        _attach_bounded_usage(exc, ticket, charged)
                else:
                    self.budget.settle(ticket, actual_usd=0.0)
                if _retry_provider_500(self.budget, ticket, exc, attempt_index):
                    retry_source_index = ticket_index
                    continue
                raise
            finally:
                if wrapped_client:
                    self.model._client = original_client
            if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[1], dict):
                if ticket.record.get("usage_metadata_missing"):
                    charged = _settle_conservative_ceiling(
                        self.budget, ticket, "bounded_usage_missing")
                else:
                    charged = self.budget.settle(ticket, unknown=True)
                exc = BudgetRefused("structured_usage_missing")
                _attach_bounded_usage(exc, ticket, charged)
                raise exc
            output, usage = result
            if ticket.record.get("usage_metadata_missing"):
                _settle_conservative_ceiling(self.budget, ticket, "bounded_usage_missing")
                return output, usage
            if usage.get("usd") is None:
                charged = self.budget.settle(ticket, unknown=True)
                exc = BudgetRefused("structured_usage_missing")
                _attach_bounded_usage(exc, ticket, charged)
                raise exc
            self.budget.settle(ticket, actual_usd=usage["usd"],
                               actual_input_tokens=usage.get("input_tokens"),
                               actual_output_tokens=usage.get("output_tokens"))
            return output, usage


def _install_gemini_guards(modules, budget):
    research = modules.gemini_research
    model = modules.no_retry_model
    previous_factory = research.client_factory
    previous_generate = research._generate
    research.client_factory = lambda: model.client

    def generate_once(client, model_name, history, config):
        serial = {"history": history, "config": config}
        request_stats = _serialized_request_stats(serial)
        input_bound = request_stats["request_payload_utf8_bytes"] + 1024
        config_value = _jsonable(config)
        output_bound = int(config_value.get("max_output_tokens") or 0) if isinstance(config_value, dict) else 0
        if output_bound <= 0:
            raise OperationRefused("research_output_bound_unknown")
        retry_source_index = None
        for attempt_index in range(budget.provider_500_retries + 1):
            ticket = budget.reserve("research", model_name, input_bound, output_bound)
            ticket.record.update(request_stats)
            ticket.record["client_timeout_s"] = getattr(model, "timeout_s", None)
            ticket.record["requested_max_output_tokens"] = output_bound
            ticket.record["effective_output_limit_tokens"] = output_bound
            ticket_index = len(budget.calls) - 1
            ticket.record["provider_retry_index"] = attempt_index
            if retry_source_index is not None:
                ticket.record["retry_of_call_index"] = retry_source_index
                budget.calls[retry_source_index]["authorized_retry_call_index"] = ticket_index
            try:
                budget.mark_dispatched(ticket)
                response = client.models.generate_content(model=model_name, contents=history, config=config)
                usage_metadata_present = _has_usage_metadata(response)
                if not usage_metadata_present:
                    ticket.record["usage_metadata_missing"] = True
                usage = modules.gemini.usage_of(response, model_name)
            except BaseException as exc:
                usage = _known_exception_usage(exc)
                amount = _known_exception_usd(exc)
                if ticket.dispatched:
                    charged = budget.settle(
                        ticket,
                        actual_usd=amount,
                        unknown=amount is None,
                        actual_input_tokens=usage.get("input_tokens") if usage else None,
                        actual_output_tokens=usage.get("output_tokens") if usage else None,
                    )
                    if usage is None or "input_tokens" not in usage or "output_tokens" not in usage:
                        _attach_bounded_usage(exc, ticket, charged)
                else:
                    budget.settle(ticket, actual_usd=0.0)
                if _retry_provider_500(budget, ticket, exc, attempt_index):
                    retry_source_index = ticket_index
                    continue
                raise
            if ticket.record.get("usage_metadata_missing"):
                _settle_conservative_ceiling(budget, ticket, "bounded_usage_missing")
                return response
            if not isinstance(usage, dict) or usage.get("usd") is None:
                charged = budget.settle(ticket, unknown=True)
                exc = OperationRefused("research_usage_missing")
                _attach_bounded_usage(exc, ticket, charged)
                raise exc
            budget.settle(ticket, actual_usd=usage["usd"],
                          actual_input_tokens=usage.get("input_tokens"),
                          actual_output_tokens=usage.get("output_tokens"))
            return response
        raise OperationRefused("provider_retry_limit_exhausted")

    research._generate = generate_once
    return lambda: (setattr(research, "client_factory", previous_factory),
                    setattr(research, "_generate", previous_generate))


def _git_executable():
    local = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local:
        candidates.append(Path(local) / "Programs" / "Git" / "cmd" / "git.exe")
    candidates.extend((Path("C:/Program Files/Git/cmd/git.exe"), Path("C:/Program Files/Git/bin/git.exe")))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return shutil.which("git") or "git"


def _git(repo_root, *args):
    return subprocess.run(
        [str(_git_executable()), "-C", str(repo_root), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _module_origin(repo_root, module):
    root = Path(repo_root).resolve()
    path = Path(module.__file__).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        raise OperationRefused("module_origin_mismatch") from None
    tracked = _git(root, "ls-files", "--error-unmatch", relative)
    if tracked.returncode:
        raise OperationRefused("module_uncommitted")
    return str(path)


def validate_execution_authority(*, repo_root=ROOT, runner_path=None, gate_path=None, now=None):
    repo_root = Path(repo_root).resolve()
    runner_path = Path(runner_path or __file__).resolve()
    gate_path = Path(gate_path or (repo_root / "plans" / "r2-health-gate.json")).resolve()
    try:
        runner_relative = runner_path.relative_to(repo_root).as_posix()
    except ValueError:
        raise OperationRefused("runner_outside_source_root") from None
    status = _git(repo_root, "status", "--porcelain", "--untracked-files=no")
    if status.returncode or status.stdout.strip():
        raise OperationRefused("source_dirty")
    head = _git(repo_root, "rev-parse", "HEAD")
    if head.returncode or not head.stdout.strip():
        raise OperationRefused("source_commit_missing")
    source_commit = head.stdout.strip()
    if len(source_commit) != 40 or any(char not in "0123456789abcdef" for char in source_commit):
        raise OperationRefused("source_commit_invalid")
    tracked = _git(repo_root, "ls-files", "--error-unmatch", runner_relative)
    if tracked.returncode:
        raise OperationRefused("runner_uncommitted")
    original = _git(repo_root, "rev-parse", "--verify", f"{ORIGINAL_SOURCE_COMMIT}^{{commit}}")
    if original.returncode or original.stdout.strip() != ORIGINAL_SOURCE_COMMIT:
        raise OperationRefused("original_source_missing")
    if _git(repo_root, "merge-base", "--is-ancestor", ORIGINAL_SOURCE_COMMIT, source_commit).returncode:
        raise OperationRefused("original_source_not_ancestor")
    ancestor_proof = {}
    for ancestor in REQUIRED_ANCESTORS:
        result = _git(repo_root, "merge-base", "--is-ancestor", ancestor, source_commit)
        ancestor_proof[ancestor] = result.returncode == 0
        if result.returncode != 0:
            raise OperationRefused("source_missing_required_ancestor")
    try:
        gate_bytes = gate_path.read_bytes()
        gate = json.loads(gate_bytes.decode("utf-8"))
    except Exception:
        raise OperationRefused("gate_unavailable") from None
    if gate.get("schema_version") != GATE_SCHEMA or gate.get("project") != PROJECT:
        raise OperationRefused("gate_contract_mismatch")
    if gate.get("verdict") != "READY":
        raise OperationRefused("gate_not_ready")
    source_binding = gate.get("source_binding") or {}
    if (source_binding.get("status") != "PROVEN"
            or source_binding.get("method") != "user_authorized_health_version"):
        raise OperationRefused("gate_source_binding_mismatch")
    health = gate.get("health") or {}
    version = health.get("version")
    health_commit = health.get("git_commit")
    if (type(health.get("http_status")) is not int or health["http_status"] != 200
            or health.get("ok") is not True
            or not isinstance(version, str) or not 7 <= len(version) <= 40
            or any(char not in "0123456789abcdef" for char in version.lower())
            or not isinstance(health_commit, str) or len(health_commit) != 40
            or any(char not in "0123456789abcdef" for char in health_commit.lower())
            or not health_commit.lower().startswith(version.lower())):
        raise OperationRefused("health_binding_invalid")
    resolved_health = _git(repo_root, "rev-parse", "--verify", f"{health_commit}^{{commit}}")
    if resolved_health.returncode or resolved_health.stdout.strip().lower() != health_commit.lower():
        raise OperationRefused("health_commit_unresolved")
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        checked = datetime.fromisoformat(str(gate["checked_at_utc"]).replace("Z", "+00:00"))
        age = (now.astimezone(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
    except Exception:
        raise OperationRefused("gate_timestamp_invalid") from None
    if age < -60 or age > GATE_MAX_AGE_SECONDS:
        raise OperationRefused("gate_stale")
    for ancestor in REQUIRED_ANCESTORS:
        result = _git(repo_root, "merge-base", "--is-ancestor", ancestor, health_commit)
        if result.returncode != 0:
            raise OperationRefused("health_commit_missing_required_ancestor")
    proof_summary = "; ".join(
        f"{ancestor}:source_HEAD=PASS,health=PASS" for ancestor in REQUIRED_ANCESTORS
    )
    return {
        "gate_validated": True,
        "source_commit": source_commit,
        "source_clean": True,
        "original_source_commit": ORIGINAL_SOURCE_COMMIT,
        "original_source_ancestor": True,
        "health_at_start": {
            "http_status": health["http_status"],
            "ok": health["ok"],
            "version": version,
            "git_commit": health_commit,
        },
        "ancestor_proof": proof_summary,
        "ancestor_proof_by_sha": ancestor_proof,
        "gate_sha256": hashlib.sha256(gate_bytes).hexdigest(),
        "gate_checked_at_utc": checked.astimezone(timezone.utc).isoformat(),
        "session_cap_usd": SESSION_CAP_USD,
        "not_run_reasons": {},
    }


def _verify_builder_adc():
    try:
        import google.auth
        from google.auth.impersonated_credentials import Credentials as ImpersonatedCredentials

        credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception:
        raise OperationRefused("builder_adc_unavailable") from None
    principal = getattr(credentials, "_target_principal", None)
    endpoint = getattr(credentials, "_iam_endpoint_override", None)
    if not isinstance(credentials, ImpersonatedCredentials):
        raise OperationRefused("builder_adc_not_impersonated")
    if project != PROJECT:
        raise OperationRefused("builder_adc_project_mismatch")
    if principal != BUILDER_EMAIL:
        raise OperationRefused("builder_adc_principal_mismatch")
    if endpoint and endpoint != BUILDER_IMPERSONATION_URL:
        raise OperationRefused("builder_adc_endpoint_mismatch")
    return {"principal": principal, "project": project, "credential_type": type(credentials).__name__,
            "impersonation_endpoint": endpoint or "iamcredentials.googleapis.com default for f42-builder"}


def jsonable(value):
    return _jsonable(value)


def _rows(result):
    rows = result.get("rows") if isinstance(result, dict) else result
    return [dict(row) for row in rows or []]


def _counts(value):
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value or {})


def _canonical_rows(rows):
    return sorted(json.dumps(_jsonable(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                  for row in rows)


def guard_execute(execute, *, check_sql, spend_sql, prefix, question_id, day, spend_writes):
    expected_what = f"staging_check_ask:{question_id}"

    def guarded(sql, params, max_bytes=None):
        params = dict(params or {})
        if sql == spend_sql:
            if set(params) != {"run_id", "run_date", "counts"}:
                raise OperationRefused("spend_write_shape")
            if params["run_date"] != day or not str(params["run_id"]).startswith(prefix + "-spend-"):
                raise OperationRefused("spend_write_identity")
            try:
                counts = _counts(params["counts"])
                amount = round(float(counts["model_usd"]), 6)
            except Exception:
                raise OperationRefused("spend_write_counts") from None
            if counts.get("what") != expected_what or amount <= 0:
                raise OperationRefused("spend_write_value")
            intent = {"run_id": params["run_id"], "stage": "understand_spend", "run_date": day,
                      "status": "ok", "model_usd": amount, "what": expected_what,
                      "params": _jsonable(params), "submitted": False}
            spend_writes.append(intent)
            try:
                result = execute(sql, params, max_bytes)
                intent["submitted"] = True
                return result
            except BaseException as exc:
                intent["error_type"] = type(exc).__name__
                raise
        try:
            check_sql(sql)
        except Exception:
            raise OperationRefused("query_guard") from None
        return execute(sql, params, max_bytes)

    return guarded


class ReadOnlyWarehouse:
    def __init__(self, backend, check_sql, *, budget=None):
        self.backend = backend
        self.check_sql = check_sql
        self.budget = budget
        self.ctx = None

    def bind_context(self, ctx):
        self.ctx = ctx

    def _semantic_event(self, status, count=0, exc=None, sql=None, rows=None):
        events = getattr(self.ctx, "events", None)
        if not isinstance(events, list):
            return
        event = {"step": "semantic_query_trace", "mode": "semantic", "status": status,
                 "embedding_calls": count}
        if sql is not None:
            event["sql_sha256"] = hashlib.sha256(str(sql).encode("utf-8")).hexdigest()
        if rows is not None:
            event["row_count"] = len(rows)
            event["post_ids"] = [row["post_id"] for row in rows
                                 if isinstance(row, dict) and row.get("post_id") is not None]
        if exc is not None:
            from core.agent.tools.warehouse import retrieval_refusal_category

            event["error_type"] = type(exc).__name__
            event["refusal_category"] = retrieval_refusal_category(exc)
        events.append(event)

    def _check(self, sql, params=None):
        semantic = "tvf_search_posts" in str(sql).lower()
        try:
            self.check_sql(sql)
        except Exception as exc:
            if semantic:
                self._semantic_event("refused", exc=exc)
            raise OperationRefused("query_guard") from None
        try:
            count = _semantic_invocation_count(sql, params)
        except OperationRefused as exc:
            if semantic:
                self._semantic_event("refused", exc=exc)
            raise
        if count and self.budget is None:
            exc = OperationRefused("semantic_budget_unavailable")
            self._semantic_event("refused", count=count, exc=exc)
            raise exc
        return count

    def dry_run(self, sql, params):
        self._check(sql, params)
        return self.backend.dry_run(sql, params)

    def run(self, sql, params, max_bytes_billed):
        count = self._check(sql, params)
        tickets = []
        try:
            for _ in range(count):
                ticket = self.budget.reserve_fixed(
                    "semantic_embedding", EMBEDDING_MODEL, SEMANTIC_EMBEDDING_RESERVE_USD,
                    EMBEDDING_INPUT_BOUND_TOKENS)
                ticket.record["charge_basis"] = "conservative_embedding_ceiling"
                tickets.append(ticket)
        except BaseException as exc:
            for ticket in tickets:
                self.budget.settle(ticket, actual_usd=0.0)
            if count:
                self._semantic_event("refused", count=count, exc=exc)
            raise
        for ticket in tickets:
            self.budget.mark_dispatched(ticket)
        try:
            result = self.backend.run(sql, params, max_bytes_billed)
        except BaseException as exc:
            for ticket in tickets:
                _settle_conservative_ceiling(self.budget, ticket, "conservative_embedding_ceiling")
            if count:
                self._semantic_event("failed", count=count, exc=exc, sql=sql)
            raise
        for ticket in tickets:
            _settle_conservative_ceiling(self.budget, ticket, "conservative_embedding_ceiling")
        if count:
            self._semantic_event("completed", count=count, sql=sql, rows=result)
        return result


def _semantic_invocation_count(sql, params):
    sql = str(sql)
    if "tvf_search_posts" not in sql.casefold():
        return 0
    try:
        import sqlglot
        from sqlglot import exp

        statements = sqlglot.parse(sql, dialect="bigquery")
    except Exception:
        raise OperationRefused("semantic_invocation_unbounded") from None
    scalar = re.compile(
        r"(?:@[A-Za-z_][A-Za-z0-9_]*|NULL|TRUE|FALSE|[-+]?(?:\d+(?:\.\d*)?|\.\d+)|"
        r"'(?:''|[^'])*'|(?:DATE|DATETIME|TIME|TIMESTAMP)\s+'(?:''|[^'])*')",
        re.IGNORECASE,
    )
    count = 0
    for statement in statements:
        if statement is None:
            continue
        for table in statement.find_all(exp.Table):
            function = table.this
            if (not isinstance(function, exp.Func)
                    or function.name.casefold().rsplit(".", 1)[-1] != "tvf_search_posts"):
                continue
            count += 1
            ancestor = table.parent
            while ancestor is not None:
                if isinstance(ancestor, (exp.CTE, exp.Subquery)):
                    raise OperationRefused("semantic_invocation_unbounded")
                ancestor = ancestor.parent
            arguments = list(function.expressions)
            if len(arguments) != 5:
                raise OperationRefused("semantic_invocation_unbounded")
            for position, expression in enumerate(arguments):
                argument = expression.sql(dialect="bigquery").strip()
                if not scalar.fullmatch(argument):
                    raise OperationRefused("semantic_invocation_unbounded")
                parameter = None
                if argument.startswith("@"):
                    name = argument[1:]
                    if not isinstance(params, dict) or name not in params:
                        raise OperationRefused("semantic_invocation_unbounded")
                    parameter = params[name]
                    if parameter is not None and not isinstance(
                            parameter, (str, int, float, bool, Decimal, date, datetime)):
                        raise OperationRefused("semantic_invocation_unbounded")
                if position == 0:
                    if argument.startswith("@"):
                        query_text = parameter
                    elif isinstance(expression, exp.Literal) and expression.is_string:
                        query_text = expression.this
                    else:
                        raise OperationRefused("semantic_invocation_unbounded")
                    if not isinstance(query_text, str):
                        raise OperationRefused("semantic_invocation_unbounded")
                    if len(query_text.encode("utf-8")) > EMBEDDING_INPUT_BOUND_TOKENS:
                        raise OperationRefused("semantic_query_bound_exceeded")
    return count


class ClaimChecksOnlyWriter:
    def __init__(self, backend, *, ask_id, capture_rows):
        self.backend = backend
        self.ask_id = ask_id
        self.capture_rows = capture_rows

    def insert(self, table, rows, row_ids=None):
        if table != CLAIM_TABLE or not isinstance(rows, list):
            raise OperationRefused("table_write_guard")
        copied = _jsonable(copy.deepcopy(rows))
        if any(row.get("answer_or_brief_id") != self.ask_id or not row.get("run_id") for row in copied):
            raise OperationRefused("claim_write_identity")
        ids = list(row_ids or [])
        if ids and len(ids) != len(copied):
            raise OperationRefused("claim_write_row_ids")
        self.capture_rows.extend(copied)
        self.backend.insert(table, rows, row_ids=ids or None)


def _normalize_wiring(wiring):
    if wiring.warehouse is not None:
        return wiring
    deps = wiring.ask_deps()
    warehouse = getattr(deps, "warehouse", None)
    if warehouse is None:
        raise OperationRefused("warehouse_unavailable")
    normalized = copy.copy(wiring)
    normalized.warehouse = warehouse
    normalized.ask_deps = lambda: deps
    return normalized


def _query(wiring, modules, sql, params):
    result = wiring.execute(sql, params, modules.sql.MAX_BYTES_BILLED)
    return _rows(result)


def _prefix_queries(wiring, modules, prefix, day):
    spend_sql = (
        "SELECT run_id, stage, run_date, status, counts "
        "FROM `ogilvy-trends-v2.intelligence_42_agent.runs` "
        "WHERE STARTS_WITH(run_id, @prefix) AND run_date = @day ORDER BY run_id"
    )
    claims_sql = (
        "SELECT answer_or_brief_id, claim_id, rule, verdict, checker, run_id, reason "
        "FROM `ogilvy-trends-v2.intelligence_42_agent.claim_checks` "
        "WHERE answer_or_brief_id = @prefix ORDER BY run_id, claim_id, rule, verdict"
    )
    return (
        _query(wiring, modules, spend_sql, {"prefix": prefix + "-spend-", "day": day}),
        _query(wiring, modules, claims_sql, {"prefix": prefix}),
    )


def _preflight(wiring, modules, prefix, now, session_cap_usd=SESSION_CAP_USD):
    staging, ask = modules.staging, modules.ask
    day = now.astimezone(staging.SAST).date()
    warehouse = ReadOnlyWarehouse(wiring.warehouse, modules.sql.check_sql)
    spent = float(ask.model_spend_today(warehouse, now))
    daily_cap = float(ask.model_daily_usd(now=now))
    if not math.isfinite(spent) or spent < 0 or not math.isfinite(daily_cap) or daily_cap <= 0:
        raise OperationRefused("daily_spend_preflight_invalid")
    if spent >= daily_cap:
        raise OperationRefused("daily_cap_exhausted")
    readonly = copy.copy(wiring)

    def select_only(sql, params, max_bytes=None):
        try:
            modules.sql.check_sql(sql)
        except Exception:
            raise OperationRefused("query_guard") from None
        return wiring.execute(sql, params, max_bytes)

    readonly.execute = select_only
    spend_rows, claim_rows = _prefix_queries(readonly, modules, prefix, day)
    if spend_rows or claim_rows:
        raise OperationRefused("duplicate_prefix")
    return {"run_date": day, "spent_before_usd": spent, "daily_model_cap_usd": daily_cap,
            "session_cap_usd": session_cap_usd}


def _wrap_wiring(wiring, modules, *, prefix, question_id, day, captured, budget):
    guarded_execute = guard_execute(
        wiring.execute,
        check_sql=modules.sql.check_sql,
        spend_sql=modules.embed.SPEND_ROW_SQL,
        prefix=prefix,
        question_id=question_id,
        day=day,
        spend_writes=captured["spend_writes"],
    )
    wrapped = copy.copy(wiring)
    wrapped.execute = guarded_execute
    original_factory = wiring.ask_deps
    model = BudgetedGeminiModel(modules.no_retry_model, budget)

    def ask_deps():
        base = original_factory()
        warehouse = ReadOnlyWarehouse(base.warehouse, modules.sql.check_sql, budget=budget)
        tables = ClaimChecksOnlyWriter(base.tables, ask_id=prefix, capture_rows=captured["claim_rows"])
        return __import__("dataclasses").replace(
            base,
            warehouse=warehouse,
            tables=tables,
            model=model,
            socialcrawl=modules.staging.refusing_socialcrawl,
            spent_today_usd=lambda: modules.ask.model_spend_today(warehouse, wiring.now()),
        )

    wrapped.ask_deps = ask_deps
    wrapped.warehouse = ReadOnlyWarehouse(wiring.warehouse, modules.sql.check_sql, budget=budget)
    wrapped.spent_today = lambda: modules.ask.model_spend_today(wrapped.warehouse, wiring.now())

    def refused_part(*args, **kwargs):
        raise OperationRefused("non_ask_part")

    wrapped.run_embed = refused_part
    wrapped.run_enrich = refused_part
    inner_run_ask = wiring.run_ask

    def run_ask(request, emit, should_stop, *, deps=None):
        if request.get("tier") != "T1" or request.get("mode") != "live":
            raise OperationRefused("ask_request_guard")
        if deps is None or deps.socialcrawl is not modules.staging.refusing_socialcrawl:
            raise OperationRefused("socialcrawl_guard")
        request = dict(request)
        request["ask_id"] = prefix
        inner_research = deps.research

        def research(ctx, *args, **kwargs):
            captured["ctx"] = ctx
            if getattr(ctx, "tier", None) != "T1":
                captured["research_error_type"] = "AskTierMismatch"
                budget.stop_reason = "ask_tier_mismatch"
                return {"note": "", "tokens": {"input": 0, "output": 0}, "usd": 0.0, "stopped": False}
            warehouse = getattr(deps, "warehouse", None)
            for guarded_warehouse in (warehouse, wrapped.warehouse):
                if hasattr(guarded_warehouse, "bind_context"):
                    guarded_warehouse.bind_context(ctx)
            call_start = len(budget.calls)
            try:
                return inner_research(ctx, *args, **kwargs)
            except BaseException as exc:
                captured["research_error_type"] = type(exc).__name__
                captured["research_http_status"] = _safe_http_error_status(exc)
                if not budget.stop_reason:
                    budget.stop_reason = "research_failed"
                usage = _call_totals(budget.calls[call_start:])
                events = getattr(ctx, "events", None)
                if isinstance(events, list):
                    events.append({"step": "r2_research_failure", "reason": type(exc).__name__})
                return {"note": "", "tokens": {"input": usage["input_tokens"],
                                                    "output": usage["output_tokens"]},
                        "usd": usage["usd"], "stopped": False, "r2_failed": True}

        deps = __import__("dataclasses").replace(deps, research=research)
        try:
            result = inner_run_ask(request, emit, should_stop, deps=deps)
            captured["answer"] = result.get("answer")
            captured["run"] = result.get("run") or {}
            return result
        except BaseException as exc:
            captured["run"] = getattr(exc, "run", None) or captured.get("run") or {}
            captured["error_type"] = type(exc).__name__
            raise

    wrapped.run_ask = run_ask
    return wrapped


def _bars_violation(bars):
    if not isinstance(bars, dict) or not bars.get("schema_valid"):
        return "schema_invalid"
    if type(bars.get("pass")) is not bool:
        return "score_invalid"
    if int(bars.get("unknown_ids") or 0):
        return "unknown_evidence_id"
    if int(bars.get("age_claims") or 0):
        return "age_claim"
    if int(bars.get("numbers_reproduced") or 0) != int(bars.get("numbers") or 0):
        return "unreproduced_number"
    insufficient = (int(bars.get("cited_in_window") or 0) < 5 or len(bars.get("platforms") or []) < 2)
    if bars["pass"] == insufficient:
        return "score_inconsistent"
    return None


def _spend_rows_for_report(rows):
    result = []
    for row in rows:
        counts = _counts(row.get("counts"))
        result.append({"run_id": row.get("run_id"), "stage": row.get("stage"),
                       "run_date": _jsonable(row.get("run_date")), "status": row.get("status"),
                       "what": counts.get("what"), "booked_usd": round(float(counts.get("model_usd") or 0), 6)})
    return result


def _spend_readback_matches(writes, rows, day, question_id):
    expected = [{"run_id": item["run_id"], "stage": item["stage"], "run_date": day,
                 "status": item["status"], "counts": {"model_usd": item["model_usd"], "what": item["what"]}}
                for item in writes]
    actual = []
    for row in rows:
        counts = _counts(row.get("counts"))
        actual.append({"run_id": row.get("run_id"), "stage": row.get("stage"),
                       "run_date": row.get("run_date"), "status": row.get("status"),
                       "counts": {"model_usd": round(float(counts.get("model_usd") or 0), 6),
                                  "what": counts.get("what")}})
    if any(item["stage"] != "understand_spend" or item["status"] != "ok"
           or item["counts"]["what"] != f"staging_check_ask:{question_id}" for item in actual):
        return False
    return _canonical_rows(expected) == _canonical_rows(actual)


def _readbacks(wiring, modules, prefix, day, captured):
    spend_rows, claim_rows = _prefix_queries(wiring, modules, prefix, day)
    return {
        "spend_readback": {"match": _spend_readback_matches(
            captured["spend_writes"], spend_rows, day, captured["question_id"]),
            "rows": _spend_rows_for_report(spend_rows)},
        "claim_readback": {"match": _canonical_rows(captured["claim_rows"]) == _canonical_rows(claim_rows),
                           "rows": claim_rows},
    }


def _session_spend_readback(wiring, modules, day, expected_writes):
    sql = (
        "SELECT run_id, stage, run_date, status, counts "
        "FROM `ogilvy-trends-v2.intelligence_42_agent.runs` "
        "WHERE STARTS_WITH(run_id, @prefix) AND run_date = @day ORDER BY run_id"
    )
    rows = _query(wiring, modules, sql, {"prefix": PREFIX_ROOT, "day": day})
    expected = [{"run_id": item["run_id"], "stage": item["stage"], "run_date": day,
                 "status": item["status"], "counts": {"model_usd": item["model_usd"], "what": item["what"]}}
                for item in expected_writes]
    actual = []
    total = 0.0
    for row in rows:
        counts = _counts(row.get("counts"))
        amount = round(float(counts.get("model_usd") or 0), 6)
        total += amount
        actual.append({"run_id": row.get("run_id"), "stage": row.get("stage"),
                       "run_date": row.get("run_date"), "status": row.get("status"),
                       "counts": {"model_usd": amount, "what": counts.get("what")}})
    allowed_attempts = {
        f"{PREFIX_ROOT}-{question_id}-spend-": question_id for question_id in QUESTION_IDS
    }
    allowed_attempts[f"{PREFIX_ROOT}-{RISE_RETRY_KEY}-spend-"] = "RISE-02"
    if PREFIX_ROOT.endswith("-r3"):
        allowed_attempts[f"{PREFIX_ROOT}-{R3_NOW_RETRY_KEY}-spend-"] = "NOW-01"
    allowed = all(
        item["stage"] == "understand_spend" and item["status"] == "ok"
        and any(item["counts"]["what"] == f"staging_check_ask:{question_id}"
                and item["run_id"].startswith(run_prefix)
                for run_prefix, question_id in allowed_attempts.items())
        for item in actual
    )
    return {"match": allowed and _canonical_rows(expected) == _canonical_rows(actual),
            "rows": _spend_rows_for_report(rows), "booked_usd": round(total, 6)}


def _outcome(result, readback, ask_module, spend_intents=()):
    if result.get("error") is not None:
        return "OPERATIONAL STOP", result.get("error_type") or "ask_error"
    if not readback["spend_readback"]["match"] or not readback["claim_readback"]["match"]:
        return "OPERATIONAL STOP", "readback_mismatch"
    run = result.get("run") or {}
    if run.get("tier") != "T1":
        return "OPERATIONAL STOP", "tier_mismatch"
    try:
        reported = round(float(run.get("model_usd")), 6)
        booked = round(float(result.get("booked_usd")), 6)
        guarded_minimum = max((float(row.get("guarded_minimum_usd") or 0) for row in spend_intents), default=0.0)
        expected = round(max(reported, guarded_minimum), 6)
        if booked != expected:
            return "OPERATIONAL STOP", "spend_booking_mismatch"
    except (TypeError, ValueError):
        return "OPERATIONAL STOP", "spend_booking_unknown"
    notices = run.get("notices") or []
    read_refusal_start = "Model spend for today could not be read, so the USD "
    read_refusal_end = " cap is treated as reached and the question was not researched"
    cap_refusal_start = "The model budget for today is spent: the USD "
    cap_refusal_end = " cap has no room left for an answer"
    refused = (getattr(ask_module, "BUDGET_SPENT", None) in notices
               or any(str(notice).startswith(read_refusal_start) and str(notice).endswith(read_refusal_end)
                      or str(notice).startswith(cap_refusal_start) and str(notice).endswith(cap_refusal_end)
                      for notice in notices))
    if refused:
        return "OPERATIONAL STOP", "daily_spend_refused"
    violation = _bars_violation(result.get("bars"))
    if violation:
        return "OPERATIONAL STOP", violation
    if not result["bars"].get("pass"):
        return "VALID INSUFFICIENT", None
    return "PASS", None


def _context_snapshot(ctx):
    if ctx is None:
        return None
    fields = ("run_id", "tier", "as_of", "market", "window_start", "window_end", "evidence",
              "writer_evidence_exclusions", "queries",
              "events", "sc_calls", "forecasts", "native_statuses", "native_languages", "native_review_loaded",
              "native_review_available", "native_language_attempted")
    return {key: _jsonable(getattr(ctx, key)) for key in fields if hasattr(ctx, key)}


def _query_hashes(ctx):
    queries = getattr(ctx, "queries", {}) if ctx is not None else {}
    return {key: value.get("result_hash") for key, value in queries.items() if isinstance(value, dict)}


def _warehouse_bytes(ctx):
    queries = getattr(ctx, "queries", {}) if ctx is not None else {}
    return sum(int(value.get("bytes") or 0) for value in queries.values() if isinstance(value, dict))


def _write_json_exclusive(path, payload):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(_jsonable(payload), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _saved_spend_rows(writes):
    return [{
        "run_id": item["run_id"],
        "stage": item["stage"],
        "run_date": _jsonable(item["run_date"]),
        "status": item["status"],
        "what": item["what"],
        "booked_usd": round(float(item["model_usd"]), 6),
    } for item in writes]


def _reservation_ceiling_micros(call):
    try:
        input_bound = call["input_bound_tokens"]
        output_bound = call["output_reserve_tokens"]
        if (type(input_bound) is not int or input_bound < 0
                or type(output_bound) is not int or output_bound < 0):
            raise ValueError
        from core.llm.provider import price_for

        price = price_for(call["model"])
        input_rate = Decimal(str(price["input"]))
        output_rate = Decimal(str(price["output"]))
        if (not input_rate.is_finite() or not output_rate.is_finite()
                or input_rate < 0 or output_rate < 0):
            raise ValueError
        amount = (Decimal(input_bound) * input_rate + Decimal(output_bound) * output_rate) / MICROS
        return _micros(amount)
    except Exception:
        raise OperationRefused("resume_unknown_ceiling_unverifiable") from None


def _settled_server_error(record):
    status = record.get("research_http_status")
    return (
        record.get("outcome") == "OPERATIONAL STOP"
        and record.get("stop_reason") == "unknown_dispatched_cost"
        and record.get("error_type") == "BudgetRefused"
        and record.get("research_error_type") == "ServerError"
        and record.get("accounting_status") == "unknown_dispatched_cost"
        and record.get("raw_answer") is None
        and record.get("bars") is None
        and (status is None or type(status) is int and 400 <= status <= 599)
    )


def _settled_r3_model_error(record):
    calls = record.get("call_costs")
    model_errors = [call for call in calls or [] if isinstance(call, dict)
                    and call.get("phase") in ("research", "structured")
                    and call.get("status") in ("charged_known", "unknown_charged_ceiling")
                    and (call.get("provider_error_type") is not None
                         or call.get("provider_http_status") is not None)]
    unknown = [call for call in model_errors if call.get("status") == "unknown_charged_ceiling"]
    accounting_statuses = {
        "metered_model_usage", "unknown_dispatched_cost", "conservative_usage_ceiling",
        "conservative_semantic_estimate", "mixed_conservative_estimates",
    }
    return (
        record.get("outcome") == "OPERATIONAL STOP"
        and record.get("stop_reason") not in {
            "readback_error", "session_spend_readback_error", "session_spend_readback_mismatch",
            "session_booked_below_guard", "session_booked_total_over_cap",
            "reported_usage_exceeds_guarded_calls", "actual_cost_above_reserve", "session_cap_breached",
        }
        and record.get("accounting_status") in accounting_statuses
        and record.get("raw_answer") is None
        and record.get("bars") is None
        and not record.get("claim_rows")
        and model_errors
        and all(call.get("phase") in ("research", "structured")
                and (call.get("provider_http_status") is None
                     or type(call.get("provider_http_status")) is int
                     and 400 <= call["provider_http_status"] <= 599) for call in unknown)
    )


def _r3_retry_links_valid(call_costs, call_index_base=0):
    if type(call_index_base) is not int or call_index_base < 0:
        return False
    for index, call in enumerate(call_costs):
        global_index = call_index_base + index
        retry_of = call.get("retry_of_call_index")
        if retry_of is not None:
            source_index = retry_of - call_index_base if type(retry_of) is int else -1
            if (type(retry_of) is not int or not 0 <= source_index < index
                    or call.get("provider_retry_index") != 1
                    or call_costs[source_index].get("status") != "unknown_charged_ceiling"
                    or call_costs[source_index].get("retry_authorized") is not True
                    or call_costs[source_index].get("authorized_retry_call_index") != global_index):
                return False
        if call.get("retry_authorized") is True:
            if (index + 1 >= len(call_costs)
                    or call_costs[index + 1].get("retry_of_call_index") != global_index):
                return False
    return True


def _saved_record_micros(record, question_id, attempt_key, prior_writes, *, evaluation_label="R2",
                         expected_call_index_base=None):
    prefix = f"{PREFIX_ROOT}-{attempt_key}"
    if (not isinstance(record, dict) or record.get("id") != question_id
            or record.get("attempt_key", attempt_key) != attempt_key
            or record.get("prefix") != prefix
            or record.get("readback_error_type") is not None
            or record.get("session_readback_error_type") is not None):
        raise OperationRefused("resume_record_identity_or_status")
    is_r3 = evaluation_label == "R3"
    recoverable = (_settled_r3_model_error(record) if is_r3 else _settled_server_error(record))
    outcome = record.get("outcome")
    allowed_accounting = {"metered_model_usage"}
    if is_r3:
        allowed_accounting.update(("conservative_semantic_estimate", "conservative_usage_ceiling",
                                   "mixed_conservative_estimates", "unknown_dispatched_cost"))
    if not recoverable and (outcome not in {"PASS", "VALID INSUFFICIENT"}
                            or record.get("stop_reason") is not None
                            or record.get("error_type") is not None
                            or record.get("research_error_type") is not None
                            or record.get("accounting_status") not in allowed_accounting):
        raise OperationRefused("resume_record_not_successful")
    run = record.get("run")
    if not isinstance(run, dict) or run.get("tier") != "T1":
        raise OperationRefused("resume_record_tier_invalid")
    bars = record.get("bars")
    if recoverable:
        if bars is not None or record.get("raw_answer") is not None:
            raise OperationRefused("resume_record_trust_bars_invalid")
    else:
        try:
            bars_violation = _bars_violation(bars)
        except (TypeError, ValueError):
            bars_violation = "bars_invalid"
        if bars_violation or (outcome == "PASS") != bool((bars or {}).get("pass")):
            raise OperationRefused("resume_record_trust_bars_invalid")
    claim_rows = record.get("claim_rows")
    claim_readback = record.get("claim_readback") or {}
    if (not isinstance(claim_rows, list)
            or (not recoverable and not claim_rows)
            or (recoverable and claim_rows)
            or any(not isinstance(row, dict) for row in claim_rows)
            or claim_readback.get("match") is not True
            or _canonical_rows(claim_rows) != _canonical_rows(claim_readback.get("rows") or [])
            or any(row.get("answer_or_brief_id") != prefix for row in claim_rows)):
        raise OperationRefused("resume_record_claim_readback_invalid")
    writes = record.get("spend_writes")
    if (not isinstance(writes, list) or not writes
            or any(not isinstance(item, dict)
                   or item.get("stage") != "understand_spend" or item.get("status") != "ok"
                   or item.get("submitted") is not True or item.get("error_type") is not None
                   or item.get("what") != f"staging_check_ask:{question_id}"
                   or not str(item.get("run_id", "")).startswith(f"{prefix}-spend-")
                   for item in writes)):
        raise OperationRefused("resume_record_spend_write_invalid")
    intents = record.get("spend_intents")
    if (not isinstance(intents, list) or len(intents) != 1
            or not isinstance(intents[0], dict)
            or intents[0].get("credit") is not False
            or intents[0].get("error_type") is not None
            or intents[0].get("run_prefix") != prefix
            or intents[0].get("what") != f"staging_check_ask:{question_id}"):
        raise OperationRefused("resume_record_spend_intent_invalid")
    try:
        call_costs = record["call_costs"]
        if not isinstance(call_costs, list) or not call_costs:
            raise ValueError
        unknown_indices, charged = [], 0
        for index, call in enumerate(call_costs):
            if not isinstance(call, dict):
                raise ValueError
            status = call.get("status")
            call_charge = _micros(call["charged_usd"])
            reserved = _micros(call["reserved_usd"])
            if status == "charged_known":
                if call_charge > reserved:
                    raise ValueError
            elif status == "charged_conservative_ceiling":
                if not is_r3:
                    raise ValueError
                basis = call.get("charge_basis")
                if basis == "conservative_embedding_ceiling":
                    ceiling = _micros(SEMANTIC_EMBEDDING_RESERVE_USD)
                    if (call.get("phase") != "semantic_embedding"
                            or call.get("model") != EMBEDDING_MODEL
                            or call.get("input_bound_tokens") != EMBEDDING_INPUT_BOUND_TOKENS
                            or call.get("output_reserve_tokens") != 0
                            or reserved != ceiling or call_charge != ceiling
                            or call.get("actual_input_tokens") != EMBEDDING_INPUT_BOUND_TOKENS
                            or call.get("actual_output_tokens") != 0):
                        raise ValueError
                elif basis == "bounded_usage_missing":
                    if call.get("phase") not in ("research", "structured"):
                        raise ValueError
                    ceiling = _reservation_ceiling_micros(call)
                    if (reserved != ceiling or call_charge != ceiling
                            or call.get("actual_input_tokens") != call.get("input_bound_tokens")
                            or call.get("actual_output_tokens") != call.get("output_reserve_tokens")):
                        raise ValueError
                else:
                    raise ValueError
            elif status == "unknown_charged_ceiling":
                if (not recoverable and not is_r3) or call.get("phase") not in (
                        ("research", "structured") if is_r3 else ("research",)):
                    raise ValueError
                if is_r3:
                    status_code = call.get("provider_http_status")
                    if status_code is not None and (type(status_code) is not int
                            or not 400 <= status_code <= 599):
                        raise ValueError
                ceiling = _reservation_ceiling_micros(call)
                if (reserved != ceiling or call_charge != ceiling
                        or call.get("actual_input_tokens") != call.get("input_bound_tokens")
                        or call.get("actual_output_tokens") != call.get("output_reserve_tokens")):
                    raise ValueError
                unknown_indices.append(index)
            elif status == "refused_before_dispatch":
                if call_charge or reserved:
                    raise ValueError
            else:
                raise ValueError
            charged += call_charge
        if is_r3:
            call_index_base = record.get("call_index_base", 0 if expected_call_index_base in (None, 0) else None)
            if (type(call_index_base) is not int or call_index_base < 0
                    or expected_call_index_base is not None
                    and call_index_base != expected_call_index_base):
                raise ValueError
            if unknown_indices and record.get("accounting_status") != "unknown_dispatched_cost":
                raise ValueError
            if not _r3_retry_links_valid(call_costs, call_index_base):
                raise ValueError
            if recoverable and not any(
                    call.get("provider_error_type") or call.get("provider_http_status") is not None
                    for call in call_costs):
                raise ValueError
            if recoverable and unknown_indices and any(
                    call.get("status") != "refused_before_dispatch"
                    for call in call_costs[unknown_indices[-1] + 1:]):
                raise ValueError
        else:
            if (recoverable and len(unknown_indices) != 1) or (not recoverable and unknown_indices):
                raise ValueError
            if unknown_indices and any(
                    call.get("status") != "refused_before_dispatch"
                    for call in call_costs[unknown_indices[0] + 1:]):
                raise ValueError
        if (charged != _micros(record["charged_usd"])
                or charged != _micros(record["booked_usd"])
                or charged != sum(_micros(item["model_usd"]) for item in writes)
                or _micros(run["model_usd"]) != _micros(record["reported_model_usd"])
                or _micros(record["reported_model_usd"]) > charged):
            raise ValueError
        for key in ("usd", "guarded_minimum_usd", "booked_usd"):
            if _micros(intents[0][key]) != charged:
                raise ValueError
    except (KeyError, TypeError, ValueError, BudgetRefused, OperationRefused):
        raise OperationRefused("resume_record_costs_invalid") from None
    spend_readback = record.get("spend_readback") or {}
    if (spend_readback.get("match") is not True
            or _canonical_rows(_saved_spend_rows(writes))
            != _canonical_rows(spend_readback.get("rows") or [])):
        raise OperationRefused("resume_record_spend_readback_invalid")
    cumulative_writes = prior_writes + writes
    session_readback = record.get("session_spend_readback") or {}
    cumulative_charge = sum(_micros(item["model_usd"]) for item in cumulative_writes)
    if (session_readback.get("match") is not True
            or _canonical_rows(_saved_spend_rows(cumulative_writes))
            != _canonical_rows(session_readback.get("rows") or [])
            or _micros(session_readback.get("booked_usd")) != cumulative_charge):
        raise OperationRefused("resume_record_session_readback_invalid")
    prior_writes.extend(writes)
    return charged, call_costs, recoverable


def _load_resume_prefix(data_dir, questions, *, evaluation_label="R2"):
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise OperationRefused("resume_data_missing")
    ids = [question.get("id", "unknown") for question in questions]
    is_r3 = evaluation_label == "R3"
    retry_key = R3_NOW_RETRY_KEY if is_r3 else RISE_RETRY_KEY
    retry_question_id = "NOW-01" if is_r3 else "RISE-02"
    attempt_keys = set(ids) | {retry_key}
    allowed = {f"{attempt_key}.{suffix}" for attempt_key in attempt_keys
               for suffix in ("json", "attempted")}
    if not is_r3:
        allowed.add("resume-manifest.json")
    entries = list(data_dir.iterdir())
    if any(not entry.is_file() or entry.name not in allowed for entry in entries):
        raise OperationRefused("resume_extra_file")
    markers = {entry.name[:-len(".attempted")] for entry in entries
               if entry.name.endswith(".attempted")}
    records_on_disk = {entry.name[:-len(".json")] for entry in entries
                       if entry.name.endswith(".json") and entry.name != "resume-manifest.json"}
    if markers - records_on_disk:
        raise OperationRefused("resume_pending_marker")
    if records_on_disk - markers:
        raise OperationRefused("resume_missing_marker")
    if markers != records_on_disk:
        raise OperationRefused("resume_marker_record_mismatch")
    base_count = len(records_on_disk & set(ids))
    retry_present = retry_key in records_on_disk
    if base_count == 0:
        raise OperationRefused("resume_prefix_missing")
    if set(ids[:base_count]) != records_on_disk.intersection(ids):
        raise OperationRefused("resume_record_order")
    if retry_present and base_count != len(ids):
        raise OperationRefused("resume_record_order")
    ordered_attempts = [{"id": question_id, "attempt_key": question_id}
                        for question_id in ids[:base_count]]
    if retry_present:
        ordered_attempts.append({"id": retry_question_id, "attempt_key": retry_key})
    records, writes, call_costs, recoverable_keys = [], [], [], []
    record_charges = {}
    charge_micros = 0
    base_charge_micros = 0
    prior_call_count = 0
    for attempt in ordered_attempts:
        question_id, attempt_key = attempt["id"], attempt["attempt_key"]
        marker_path = data_dir / f"{attempt_key}.attempted"
        if marker_path.read_bytes() != b"attempt started\n":
            raise OperationRefused("resume_marker_invalid")
        try:
            record = json.loads((data_dir / f"{attempt_key}.json").read_text(encoding="utf-8"))
        except Exception:
            raise OperationRefused("resume_record_invalid_json") from None
        record.setdefault("attempt_key", attempt_key)
        charged, costs, recoverable = _saved_record_micros(
            record, question_id, attempt_key, writes, evaluation_label=evaluation_label,
            expected_call_index_base=prior_call_count if is_r3 else None)
        prior_call_count += len(costs)
        charge_micros += charged
        record_charges[attempt_key] = charged
        if attempt_key != retry_key:
            base_charge_micros += charged
        if recoverable:
            recoverable_keys.append(attempt_key)
        records.append(record)
        call_costs.extend(copy.deepcopy(costs))
    remaining_attempts = [{"id": question_id, "attempt_key": question_id}
                          for question_id in ids[base_count:]]
    if is_r3 and not retry_present and "NOW-01" in recoverable_keys:
        remaining_attempts.append({"id": "NOW-01", "attempt_key": R3_NOW_RETRY_KEY})
    if not is_r3 and base_count == len(ids) and not retry_present:
        remaining_attempts.append({"id": "RISE-02", "attempt_key": RISE_RETRY_KEY})
    if retry_present and retry_question_id not in recoverable_keys:
        raise OperationRefused("resume_retry_source_not_authorized")
    if not is_r3 and charge_micros > _micros(RESUME_TOTAL_CAP_USD):
        raise OperationRefused("resume_funding_cap_exhausted")
    return {
        "records": records,
        "writes": writes,
        "call_costs": call_costs,
        "charged_micros": charge_micros,
        "base_charged_micros": base_charge_micros,
        "initial_charged_micros": record_charges[ids[0]],
        "record_charges_micros": record_charges,
        "recoverable_attempt_keys": recoverable_keys,
        "remaining_attempts": remaining_attempts,
        "remaining_ids": [attempt["id"] for attempt in remaining_attempts],
        "retry_present": retry_present,
        "evaluation_label": evaluation_label,
    }


def _verify_resume_readbacks(wiring, modules, saved):
    now = wiring.now()
    day = now.astimezone(modules.staging.SAST).date()
    prior_writes = saved["writes"]
    if any(str(item.get("run_date")) != day.isoformat() for item in prior_writes):
        raise OperationRefused("resume_run_date_mismatch")
    for record in saved["records"]:
        question_id = record["id"]
        fresh = _readbacks(
            wiring, modules, record["prefix"], day,
            {"question_id": question_id,
             "claim_rows": record["claim_rows"],
             "spend_writes": record["spend_writes"]},
        )
        if (fresh["claim_readback"]["match"] is not True
                or fresh["spend_readback"]["match"] is not True
                or _canonical_rows(fresh["claim_readback"]["rows"])
                != _canonical_rows(record["claim_readback"]["rows"])
                or _canonical_rows(fresh["spend_readback"]["rows"])
                != _canonical_rows(record["spend_readback"]["rows"])):
            raise OperationRefused("resume_readback_mismatch")
    session = _session_spend_readback(wiring, modules, day, prior_writes)
    last_saved_session = saved["records"][-1]["session_spend_readback"]
    if (session["match"] is not True
            or _canonical_rows(session["rows"]) != _canonical_rows(last_saved_session["rows"])
            or _micros(session["booked_usd"]) != saved["charged_micros"]
            or _micros(last_saved_session["booked_usd"]) != saved["charged_micros"]):
        raise OperationRefused("resume_readback_mismatch")
    return session


def _load_original_run_details(repo_root, saved):
    path = Path(repo_root) / "plans" / "r2-report-recovery-metadata.json"
    try:
        details = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise OperationRefused("original_run_provenance_missing") from None
    try:
        valid = (isinstance(details, dict) and details.get("ready") is True
                 and details.get("gate_validated") is True and details.get("source_clean") is True
                 and details.get("source_commit") == ORIGINAL_SOURCE_COMMIT
                 and details.get("session_booked_verified") is True
                 and _micros(details.get("charged_usd")) == saved["initial_charged_micros"]
                 and _micros(details.get("session_booked_usd")) == saved["initial_charged_micros"])
    except (TypeError, ValueError, BudgetRefused):
        valid = False
    if not valid:
        raise OperationRefused("original_run_provenance_mismatch")
    return details


def _verify_resume_module_sources(repo_root, modules, source_commit=None):
    source_commit = source_commit or ORIGINAL_SOURCE_COMMIT
    names = ("ask", "gemini_research", "writer", "warehouse", "sql", "staging", "gemini", "embed")
    for name in names:
        module = getattr(modules, name, None)
        if module is None:
            raise OperationRefused("resume_module_missing")
        relative = Path(_module_origin(repo_root, module)).resolve().relative_to(
            Path(repo_root).resolve()).as_posix()
        result = _git(repo_root, "diff", "--quiet", source_commit, "HEAD", "--", relative)
        if result.returncode == 1:
            raise OperationRefused("core_ask_code_changed")
        if result.returncode:
            raise OperationRefused("resume_module_source_unverifiable")


def _resume_provenance(metadata, original_details):
    health = metadata.get("health_at_start")
    if (metadata.get("source_clean") is not True
            or not isinstance(metadata.get("source_commit"), str)
            or len(metadata["source_commit"]) != 40
            or not isinstance(health, dict)
            or health.get("http_status") != 200 or health.get("ok") is not True):
        raise OperationRefused("resume_source_provenance_invalid")
    return {
        "resume_source_commit": metadata["source_commit"],
        "source_clean": True,
        "health_at_resume": copy.deepcopy(health),
        "original_source_commit": ORIGINAL_SOURCE_COMMIT,
        "original_source_details": copy.deepcopy(original_details),
    }


def _valid_resume_provenance(provenance, saved):
    try:
        original = provenance["original_source_details"]
        health = provenance["health_at_resume"]
        return (
            provenance.get("original_source_commit") == ORIGINAL_SOURCE_COMMIT
            and provenance.get("source_clean") is True
            and isinstance(provenance.get("resume_source_commit"), str)
            and len(provenance["resume_source_commit"]) == 40
            and isinstance(original, dict)
            and original.get("source_commit") == ORIGINAL_SOURCE_COMMIT
            and original.get("source_clean") is True
            and original.get("session_booked_verified") is True
            and _micros(original.get("charged_usd")) == saved["initial_charged_micros"]
            and _micros(original.get("session_booked_usd")) == saved["initial_charged_micros"]
            and isinstance(health, dict)
            and health.get("http_status") == 200
            and health.get("ok") is True
            and isinstance(health.get("version"), str)
            and isinstance(health.get("git_commit"), str)
        )
    except (KeyError, TypeError, ValueError, BudgetRefused):
        return False


def _r3_resume_provenance(repo_root, data_dir, anchor_report_path, metadata, saved):
    try:
        source_path = Path(repo_root) / "plans" / "r3-source-metadata.json"
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        source = json.loads(source_path.read_text(encoding="utf-8"))
        now_path = Path(data_dir) / "NOW-01.json"
        now_hash = hashlib.sha256(now_path.read_bytes()).hexdigest()
        if anchor_report_path is None:
            raise ValueError
        report_hash = hashlib.sha256(Path(anchor_report_path).read_bytes()).hexdigest()
        health = metadata["health_at_start"]
        source_commit = metadata["source_commit"]
        initial_charge = saved["record_charges_micros"]["NOW-01"]
        if (source_hash != R3_SOURCE_METADATA_SHA256
                or source.get("source_commit") != R3_INITIAL_SOURCE_COMMIT
                or source.get("source_clean") is not True
                or now_hash != R3_INITIAL_NOW_SHA256
                or report_hash != R3_INITIAL_REPORT_SHA256
                or initial_charge != _micros(R3_INITIAL_BOOKED_USD)
                or metadata.get("source_clean") is not True
                or not isinstance(source_commit, str) or len(source_commit) != 40
                or not isinstance(health, dict) or health.get("http_status") != 200
                or health.get("ok") is not True):
            raise ValueError
        return {
            "evaluation_label": "R3",
            "initial_source_commit": source["source_commit"],
            "initial_source_metadata_sha256": source_hash,
            "initial_now_record_sha256": now_hash,
            "initial_report_sha256": report_hash,
            "initial_booked_usd": R3_INITIAL_BOOKED_USD,
            "resume_source_commit": source_commit,
            "resume_source_clean": True,
            "health_at_resume": copy.deepcopy(health),
            "retry_attempt_key": R3_NOW_RETRY_KEY,
            "provider_retry_statuses": [500, 504],
            "now_retry_provider_retry_statuses": [],
        }
    except (OSError, KeyError, TypeError, ValueError, BudgetRefused):
        raise OperationRefused("r3_resume_anchor_or_provenance_invalid") from None


def _valid_r3_resume_provenance(provenance, saved):
    try:
        health = provenance["health_at_resume"]
        return (
            provenance.get("evaluation_label") == "R3"
            and provenance.get("initial_source_commit") == R3_INITIAL_SOURCE_COMMIT
            and provenance.get("initial_source_metadata_sha256") == R3_SOURCE_METADATA_SHA256
            and provenance.get("initial_now_record_sha256") == R3_INITIAL_NOW_SHA256
            and provenance.get("initial_report_sha256") == R3_INITIAL_REPORT_SHA256
            and _micros(provenance.get("initial_booked_usd")) == _micros(R3_INITIAL_BOOKED_USD)
            and saved["record_charges_micros"].get("NOW-01") == _micros(R3_INITIAL_BOOKED_USD)
            and provenance.get("resume_source_clean") is True
            and isinstance(provenance.get("resume_source_commit"), str)
            and len(provenance["resume_source_commit"]) == 40
            and isinstance(health, dict) and health.get("http_status") == 200
            and health.get("ok") is True
            and provenance.get("retry_attempt_key") == R3_NOW_RETRY_KEY
            and provenance.get("provider_retry_statuses") == [500, 504]
        )
    except (KeyError, TypeError, ValueError, BudgetRefused):
        return False


def _ensure_resume_manifest(data_dir, provenance, saved):
    path = Path(data_dir) / "resume-manifest.json"
    if "RISE-02" not in saved["recoverable_attempt_keys"]:
        raise OperationRefused("resume_retry_source_not_authorized")
    funding = {
        "baseline_spend_usd": RESUME_BASELINE_USD,
        "additional_cap_usd": RESUME_ADDITIONAL_CAP_USD,
        "combined_total_cap_usd": RESUME_TOTAL_CAP_USD,
    }
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise OperationRefused("resume_manifest_invalid") from None
        if (not isinstance(existing, dict)
                or existing.get("schema_version") != "r2-resume-manifest-v1"
                or existing.get("original_source_commit") != provenance["original_source_commit"]
                or existing.get("original_source_details") != provenance["original_source_details"]):
            raise OperationRefused("resume_manifest_mismatch")
        existing_funding = existing.get("funding")
        if existing_funding is not None:
            if (existing_funding != funding
                    or existing.get("authorized_retry_attempt_key") != RISE_RETRY_KEY):
                raise OperationRefused("resume_funding_anchor_mismatch")
            manifest = existing
        else:
            manifest = existing
    else:
        manifest = {
            "schema_version": "r2-resume-manifest-v1",
            "original_source_commit": provenance["original_source_commit"],
            "original_source_details": provenance["original_source_details"],
            "first_resume_source_commit": provenance["resume_source_commit"],
            "first_resume_source_clean": provenance["source_clean"],
            "health_at_first_resume": provenance["health_at_resume"],
        }
    if manifest.get("funding") is None:
        if (saved["base_charged_micros"] != _micros(RESUME_BASELINE_USD)
                or saved["charged_micros"] != _micros(RESUME_BASELINE_USD)
                or saved["retry_present"]
                or [record["id"] for record in saved["records"]] != ["NOW-01", "RISE-02"]):
            raise OperationRefused("resume_funding_anchor_unsettable")
        manifest["funding"] = funding
        manifest["authorized_retry_attempt_key"] = RISE_RETRY_KEY
        manifest["retry_authorized_by_attempt_key"] = "RISE-02"
        temporary = path.with_name(path.name + ".pending")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(_jsonable(manifest), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    elif manifest.get("retry_authorized_by_attempt_key") is None:
        manifest["retry_authorized_by_attempt_key"] = "RISE-02"
        temporary = path.with_name(path.name + ".pending")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(_jsonable(manifest), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    elif manifest.get("retry_authorized_by_attempt_key") != "RISE-02":
        raise OperationRefused("resume_retry_source_not_authorized")
    if saved["charged_micros"] > _micros(manifest["funding"]["combined_total_cap_usd"]):
        raise OperationRefused("resume_funding_cap_exhausted")
    return manifest


def _attempt_summaries(records):
    return [{
        "id": record["id"],
        "attempt_key": record.get("attempt_key", record["id"]),
        "prefix": record["prefix"],
        "outcome": record.get("outcome"),
        "stop_reason": record.get("stop_reason"),
        "charged_usd": record.get("charged_usd"),
        "session_cap_usd": record.get("session_cap_usd"),
    } for record in records]


def _render_report(reporter, handle, results, not_run, metadata, morning_records):
    rendered = reporter.render_report(results, not_run, metadata, morning_records)
    handle.seek(0)
    handle.truncate()
    handle.write(rendered)
    handle.flush()
    os.fsync(handle.fileno())


def run_questions(questions, *, wiring, modules, reporter, morning_records, metadata,
                  output_path=OUTPUT_PATH, data_dir=DATA_DIR, session_cap_usd=SESSION_CAP_USD,
                  provider_500_retries=0, _execution_token=None, resume=False):
    output_path, data_dir = Path(output_path), Path(data_dir)
    ids = [question.get("id", "unknown") for question in questions]
    is_r3 = metadata.get("evaluation_label") == "R3"
    metadata = dict(metadata)
    metadata.setdefault("session_cap_usd", session_cap_usd)
    metadata.setdefault("cap_usd", session_cap_usd)
    metadata.setdefault("evaluation_label", "R2")
    metadata.setdefault("charged_usd", 0.0)
    metadata.setdefault("session_booked_usd", None)
    metadata.setdefault("session_booked_verified", False)
    metadata.setdefault("vendor_reconciliation", "unproven")
    metadata["cost_scope"] = (
        "Gemini usage is provider reported when returned. Missing usage is charged at its request ceiling. "
        "Semantic embeddings are charged at the rounded 2,048-token ceiling per TVF call. "
        "Warehouse bytes remain under max_bytes_billed."
    )
    metadata.setdefault("semantic_embedding_cost", {
        "model": EMBEDDING_MODEL,
        "max_input_tokens_per_call": EMBEDDING_INPUT_BOUND_TOKENS,
        "usd_per_million_input_tokens": float(EMBEDDING_USD_PER_MILLION_INPUT_TOKENS),
        "reserved_usd_per_call": _micros(SEMANTIC_EMBEDDING_RESERVE_USD) / MICROS,
        "charge_basis": "conservative_ceiling",
    })
    metadata.setdefault("not_run_reasons", {})
    if _execution_token is not _EXECUTION_TOKEN:
        metadata["not_run_reasons"] = {question_id: "execution_authority_required" for question_id in ids}
        return {"exit_code": REFUSED, "stop_reason": "execution_authority_required", "results": [],
                "not_run": ids, "metadata": metadata}
    if tuple(ids) != QUESTION_IDS:
        return {"exit_code": REFUSED, "stop_reason": "question_set_mismatch", "results": [],
                "not_run": ids, "metadata": metadata}
    if resume:
        try:
            saved = _load_resume_prefix(data_dir, questions, evaluation_label=metadata.get("evaluation_label", "R2"))
        except OperationRefused as exc:
            reason = exc.args[0] if exc.args else "resume_prefix_invalid"
            metadata["not_run_reasons"] = {question_id: reason for question_id in ids}
            return {"exit_code": REFUSED, "stop_reason": reason, "results": [],
                    "not_run": ids, "metadata": metadata}
        prior_results = saved["records"]
        remaining_attempts = saved["remaining_attempts"]
        not_run = [attempt["id"] for attempt in remaining_attempts]
        if not_run and not output_path.is_file():
            metadata["not_run_reasons"] = {question_id: "resume_report_missing" for question_id in not_run}
            return {"exit_code": REFUSED, "stop_reason": "resume_report_missing", "results": prior_results,
                    "not_run": not_run, "metadata": metadata}
        if not not_run:
            metadata["charged_usd"] = saved["charged_micros"] / MICROS
            return {"exit_code": REFUSED, "stop_reason": "no_questions_to_resume", "results": prior_results,
                    "not_run": [], "metadata": metadata, "charged_usd": metadata["charged_usd"],
                    "call_costs": saved["call_costs"]}
        if _micros(session_cap_usd) != _micros(SESSION_CAP_USD):
            metadata["not_run_reasons"] = {question_id: "resume_session_cap_mismatch" for question_id in not_run}
            return {"exit_code": REFUSED, "stop_reason": "resume_session_cap_mismatch",
                    "results": prior_results, "not_run": not_run, "metadata": metadata}
        if is_r3:
            if not _valid_r3_resume_provenance(metadata.get("r3_continuation"), saved):
                metadata["not_run_reasons"] = {question_id: "r3_resume_provenance_invalid" for question_id in not_run}
                return {"exit_code": REFUSED, "stop_reason": "r3_resume_provenance_invalid",
                        "results": prior_results, "not_run": not_run, "metadata": metadata}
            question_by_id = {question["id"]: question for question in questions}
            attempt_questions = []
            for attempt in remaining_attempts:
                question = copy.deepcopy(question_by_id[attempt["id"]])
                question["attempt_key"] = attempt["attempt_key"]
                attempt_questions.append(question)
            not_run = [question["id"] for question in attempt_questions]
            metadata["session_cap_usd"] = session_cap_usd
            metadata["cap_usd"] = session_cap_usd
            metadata["charged_usd"] = saved["charged_micros"] / MICROS
            continuation = copy.deepcopy(metadata["r3_continuation"])
            continuation["attempt_order"] = [
                {"id": question["id"], "attempt_key": question.get("attempt_key", question["id"])}
                for question in attempt_questions
            ]
            metadata["r3_continuation"] = continuation
        else:
            provenance = metadata.get("resume_provenance")
            if not _valid_resume_provenance(provenance, saved):
                metadata["not_run_reasons"] = {
                    question_id: "resume_source_provenance_invalid" for question_id in not_run}
                return {"exit_code": REFUSED, "stop_reason": "resume_source_provenance_invalid",
                        "results": prior_results, "not_run": not_run, "metadata": metadata}
            try:
                resume_manifest = _ensure_resume_manifest(data_dir, provenance, saved)
                funding = resume_manifest["funding"]
                session_cap_usd = funding["combined_total_cap_usd"]
                metadata["funding"] = copy.deepcopy(funding)
                metadata["session_cap_usd"] = session_cap_usd
                metadata["cap_usd"] = session_cap_usd
                question_by_id = {question["id"]: question for question in questions}
                attempt_questions = []
                for attempt in remaining_attempts:
                    question = copy.deepcopy(question_by_id[attempt["id"]])
                    question["attempt_key"] = attempt["attempt_key"]
                    attempt_questions.append(question)
                if (not saved["retry_present"]
                        and not any(question.get("attempt_key") == RISE_RETRY_KEY
                                    for question in attempt_questions)):
                    retry_question = copy.deepcopy(question_by_id["RISE-02"])
                    retry_question["attempt_key"] = RISE_RETRY_KEY
                    attempt_questions.append(retry_question)
                not_run = [question["id"] for question in attempt_questions]
            except (OperationRefused, KeyError, TypeError):
                reason = "resume_funding_manifest_invalid"
                metadata["not_run_reasons"] = {question_id: reason for question_id in not_run}
                return {"exit_code": REFUSED, "stop_reason": reason, "results": prior_results,
                        "not_run": not_run, "metadata": metadata}
    elif output_path.exists() or data_dir.exists():
        metadata["not_run_reasons"] = {question_id: "existing_local_output" for question_id in ids}
        return {"exit_code": REFUSED, "stop_reason": "existing_local_output", "results": [],
                "not_run": ids, "metadata": metadata}
    else:
        not_run = ids
        prior_results = []
        attempt_questions = []
        for question in questions:
            question_copy = copy.deepcopy(question)
            question_copy.setdefault("attempt_key", question_copy["id"])
            attempt_questions.append(question_copy)
    budget = SessionBudget(session_cap_usd, provider_500_retries=provider_500_retries)
    budget.capture_failed_structured_request = is_r3
    if resume:
        budget.charged_micros = saved["charged_micros"]
        budget.calls = copy.deepcopy(saved["call_costs"])
    try:
        wiring = _normalize_wiring(wiring)
        first_question = attempt_questions[0]
        first_prefix = f"{PREFIX_ROOT}-{first_question.get('attempt_key', first_question['id'])}"
        first_preflight = _preflight(
            wiring, modules, first_prefix, wiring.now(), budget.cap_micros / MICROS)
        if resume:
            session_readback = _verify_resume_readbacks(wiring, modules, saved)
            metadata["session_booked_usd"] = session_readback["booked_usd"]
            metadata["session_booked_verified"] = True
            metadata["charged_usd"] = budget.charged_usd
    except BaseException as exc:
        code = exc.args[0] if isinstance(exc, OperationRefused) and exc.args else type(exc).__name__
        refused_not_run = not_run if resume else ids
        metadata["not_run_reasons"] = {question_id: str(code) for question_id in refused_not_run}
        return {"exit_code": REFUSED, "stop_reason": str(code),
                "results": prior_results if resume else [], "not_run": refused_not_run,
                "metadata": metadata, "charged_usd": budget.charged_usd if resume else 0.0,
                "call_costs": budget.calls if resume else []}
    try:
        if resume:
            report = output_path.open("r+", encoding="utf-8", newline="\n")
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=False)
            report = output_path.open("x+", encoding="utf-8", newline="\n")
    except OSError as exc:
        code = "existing_local_output" if isinstance(exc, FileExistsError) else type(exc).__name__
        refused_not_run = not_run if resume else ids
        metadata["not_run_reasons"] = {question_id: str(code) for question_id in refused_not_run}
        return {"exit_code": REFUSED, "stop_reason": str(code),
                "results": prior_results if resume else [], "not_run": refused_not_run,
                "metadata": metadata, "charged_usd": budget.charged_usd if resume else 0.0,
                "call_costs": budget.calls if resume else []}
    restore_tier = _prevent_t0_fallback(modules.ask)
    restore_gemini = _install_gemini_guards(modules, budget)
    results = copy.deepcopy(saved["records"]) if resume else []
    stop_reason = None
    metadata["not_run_reasons"] = {question_id: "execution_pending" for question_id in not_run}
    metadata["attempts"] = _attempt_summaries(results)
    metadata["not_run_attempts"] = [
        {"id": question["id"], "attempt_key": question.get("attempt_key", question["id"])}
        for question in attempt_questions
    ]
    try:
        _render_report(reporter, report, results, not_run, metadata, morning_records)
        for index, question in enumerate(attempt_questions):
            question_id = question["id"]
            attempt_key = question.get("attempt_key", question_id)
            budget.provider_retry_statuses = (
                () if is_r3 and attempt_key == R3_NOW_RETRY_KEY
                else (500, 504) if is_r3 else (500,)
            )
            prefix = f"{PREFIX_ROOT}-{attempt_key}"
            if index:
                try:
                    preflight = _preflight(
                        wiring, modules, prefix, wiring.now(), budget.cap_micros / MICROS)
                except BaseException as exc:
                    stop_reason = exc.args[0] if isinstance(exc, OperationRefused) and exc.args else type(exc).__name__
                    break
            else:
                preflight = first_preflight
            day = wiring.now().astimezone(modules.staging.SAST).date()
            marker = data_dir / f"{attempt_key}.attempted"
            try:
                with marker.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write("attempt started\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                stop_reason = "existing_attempt_marker"
                break
            start_call = len(budget.calls)
            start_charge = budget.charged_micros
            captured = {"question_id": question_id, "ctx": None, "answer": None, "run": {},
                        "error_type": None, "research_error_type": None, "research_http_status": None,
                        "claim_rows": [], "spend_intents": [], "spend_writes": []}
            guarded_wiring = _wrap_wiring(wiring, modules, prefix=prefix, question_id=question_id,
                                          day=day, captured=captured, budget=budget)

            def book(usd, what, credit=False):
                reported_usd = round(float(usd or 0), 6)
                guarded_usd = (budget.charged_micros - start_charge) / MICROS
                amount = round(guarded_usd, 6)
                intent = {"usd": amount, "reported_usd": reported_usd, "guarded_minimum_usd": guarded_usd,
                          "what": str(what), "credit": bool(credit),
                          "run_prefix": prefix, "run_date": day}
                captured["spend_intents"].append(intent)
                if what != f"staging_check_ask:{question_id}" or credit:
                    raise OperationRefused("spend_intent_guard")
                try:
                    value = modules.embed.book_spend(guarded_wiring.execute, run_id=prefix, run_date=day,
                                                     usd=amount, what=what, credit=credit)
                    intent["booked_usd"] = value
                    return value
                except BaseException as exc:
                    intent["error_type"] = type(exc).__name__
                    raise

            result = {"id": question_id, "error": None, "answer": None, "run": captured["run"], "bars": None}
            try:
                check = modules.staging.check_ask(guarded_wiring, book, [question], record_dir=None)
                returned = check.get("questions") or []
                if len(returned) != 1 or returned[0].get("id") != question_id:
                    raise OperationRefused("one_question_result_guard")
                result = returned[0]
            except BaseException as exc:
                captured["error_type"] = type(exc).__name__
                result["error"] = type(exc).__name__
                result["error_type"] = type(exc).__name__
                result["run"] = captured.get("run") or getattr(exc, "run", None) or {}
            if not captured["spend_intents"]:
                run_for_booking = result.get("run") or captured.get("run") or {}
                try:
                    result["booked_usd"] = book(
                        run_for_booking.get("model_usd") or 0.0,
                        f"staging_check_ask:{question_id}",
                    )
                except BaseException as exc:
                    captured["error_type"] = captured.get("error_type") or type(exc).__name__
                    result["error"] = result.get("error") or type(exc).__name__
                    result["error_type"] = captured["error_type"]
            if captured.get("answer") is not None:
                result["answer"] = captured["answer"]
            if captured.get("run"):
                result["run"] = captured["run"]
            readback = {"spend_readback": {"match": False, "rows": []},
                        "claim_readback": {"match": False, "rows": []}}
            readback_error_type = None
            try:
                readback = _readbacks(guarded_wiring, modules, prefix, day, captured)
            except BaseException as exc:
                readback_error_type = type(exc).__name__
            expected_session_writes = [
                write for prior in results for write in prior.get("spend_writes", [])
            ] + captured["spend_writes"]
            session_readback = {"match": False, "rows": [], "booked_usd": None}
            session_readback_error_type = None
            try:
                session_readback = _session_spend_readback(
                    guarded_wiring, modules, day, expected_session_writes)
                metadata["session_booked_usd"] = session_readback["booked_usd"]
                metadata["session_booked_verified"] = (
                    session_readback["match"]
                    and _micros(session_readback["booked_usd"]) >= budget.charged_micros
                )
            except BaseException as exc:
                session_readback_error_type = type(exc).__name__
                metadata["session_booked_usd"] = None
                metadata["session_booked_verified"] = False
            outcome, violation = _outcome(result, readback, modules.ask, captured["spend_intents"])
            if readback_error_type:
                outcome, violation = "OPERATIONAL STOP", "readback_error"
            if session_readback_error_type:
                outcome, violation = "OPERATIONAL STOP", "session_spend_readback_error"
            elif not session_readback["match"]:
                outcome, violation = "OPERATIONAL STOP", "session_spend_readback_mismatch"
            elif not metadata["session_booked_verified"]:
                outcome, violation = "OPERATIONAL STOP", "session_booked_below_guard"
            if captured.get("research_error_type"):
                outcome, violation = "OPERATIONAL STOP", "research_failed"
            if budget.stop_reason:
                outcome, violation = "OPERATIONAL STOP", budget.stop_reason
            run = result.get("run") or {}
            ctx = captured.get("ctx")
            calls = copy.deepcopy(budget.calls[start_call:])
            charged = (budget.charged_micros - start_charge) / MICROS
            metadata["charged_usd"] = budget.charged_usd
            try:
                reported_micros = _micros(run.get("model_usd", 0) or 0)
            except BudgetRefused:
                reported_micros = budget.cap_micros + 1
            if reported_micros > budget.charged_micros - start_charge:
                outcome, violation = "OPERATIONAL STOP", "reported_usage_exceeds_guarded_calls"
            if (metadata.get("session_booked_verified")
                    and (metadata.get("session_booked_usd") or 0) > session_cap_usd):
                outcome, violation = "OPERATIONAL STOP", "session_booked_total_over_cap"
            query_hashes = _query_hashes(ctx)
            record = {
                "id": question_id,
                "attempt_key": attempt_key,
                "question": question.get("question"),
                "markets": question.get("markets") or [],
                "prefix": prefix,
                "run_id": run.get("run_id"),
                "outcome": outcome,
                "stop_reason": violation,
                "error_type": result.get("error_type") or captured.get("error_type"),
                "research_error_type": captured.get("research_error_type"),
                "research_http_status": captured.get("research_http_status"),
                "call_index_base": start_call if is_r3 else None,
                "accounting_status": (
                    "unknown_dispatched_cost" if any(
                        call["status"] == "unknown_charged_ceiling" for call in calls)
                    else "mixed_conservative_estimates" if (
                        any(call.get("charge_basis") == "conservative_embedding_ceiling" for call in calls)
                        and any(call.get("charge_basis") == "bounded_usage_missing" for call in calls))
                    else "conservative_usage_ceiling" if any(
                        call.get("charge_basis") == "bounded_usage_missing" for call in calls)
                    else "conservative_semantic_estimate" if any(
                        call.get("charge_basis") == "conservative_embedding_ceiling" for call in calls)
                    else "metered_model_usage"
                ),
                "reported_model_usd": run.get("model_usd"),
                "booked_usd": result.get("booked_usd"),
                "preflight": preflight,
                "raw_answer": result.get("answer"),
                "run": run,
                "context": _context_snapshot(ctx),
                "query_hashes": query_hashes,
                "claim_rows": captured["claim_rows"],
                "spend_intents": captured["spend_intents"],
                "spend_writes": captured["spend_writes"],
                "spend_readback": readback["spend_readback"],
                "claim_readback": readback["claim_readback"],
                "session_spend_readback": session_readback,
                "session_readback_error_type": session_readback_error_type,
                "call_costs": calls,
                "charged_usd": charged,
                "session_cap_usd": budget.cap_micros / MICROS,
                "bars": result.get("bars"),
                "warehouse_billed_bytes": _warehouse_bytes(ctx),
                "readback_error_type": readback_error_type,
            }
            if resume:
                if is_r3:
                    record["r3_continuation"] = copy.deepcopy(metadata["r3_continuation"])
                else:
                    record["source_provenance"] = copy.deepcopy(metadata["resume_provenance"])
            try:
                _write_json_exclusive(data_dir / f"{attempt_key}.json", record)
            except BaseException as exc:
                stop_reason = "local_persist_" + type(exc).__name__
                record["outcome"], record["stop_reason"] = "OPERATIONAL STOP", stop_reason
                results.append(record)
                break
            results.append(record)
            recoverable_failure = False
            if outcome == "OPERATIONAL STOP":
                prior_writes = [write for prior in results[:-1] for write in prior.get("spend_writes", [])]
                prior_call_count = sum(len(prior.get("call_costs") or []) for prior in results[:-1])
                try:
                    _, _, recoverable_failure = _saved_record_micros(
                        record, question_id, attempt_key, prior_writes,
                        evaluation_label="R3" if is_r3 else "R2",
                        expected_call_index_base=prior_call_count if is_r3 else None)
                except OperationRefused:
                    recoverable_failure = False
            if outcome == "OPERATIONAL STOP" and not recoverable_failure:
                stop_reason = violation or "operational_failure"
                break
            if recoverable_failure:
                if is_r3:
                    if budget.stop_reason not in (None, "unknown_dispatched_cost", "research_failed"):
                        stop_reason = budget.stop_reason
                        break
                    budget.stop_reason = None
                else:
                    budget.continue_after_settled_server_error()
            remaining_attempts = attempt_questions[index + 1:]
            remaining = [attempt["id"] for attempt in remaining_attempts]
            metadata["not_run_reasons"] = {question_id: "execution_pending" for question_id in remaining}
            metadata["attempts"] = _attempt_summaries(results)
            metadata["not_run_attempts"] = [
                {"id": attempt["id"], "attempt_key": attempt.get("attempt_key", attempt["id"])}
                for attempt in remaining_attempts
            ]
            if outcome == "VALID INSUFFICIENT" and not remaining:
                stop_reason = None
            _render_report(reporter, report, results, remaining, metadata, morning_records)
        attempted_keys = {row.get("attempt_key", row["id"]) for row in results}
        remaining_attempts = [
            {"id": question["id"], "attempt_key": question.get("attempt_key", question["id"])}
            for question in attempt_questions
            if question.get("attempt_key", question["id"]) not in attempted_keys
        ]
        not_run = [attempt["id"] for attempt in remaining_attempts]
        cause = stop_reason or "not_started_after_question_stop"
        metadata["not_run_reasons"] = {question_id: cause for question_id in not_run}
        metadata["attempts"] = _attempt_summaries(results)
        metadata["not_run_attempts"] = remaining_attempts
        _render_report(reporter, report, results, not_run, metadata, morning_records)
    finally:
        report.close()
        restore_gemini()
        restore_tier()
    attempted_keys = {row.get("attempt_key", row["id"]) for row in results}
    remaining_attempts = [
        {"id": question["id"], "attempt_key": question.get("attempt_key", question["id"])}
        for question in attempt_questions
        if question.get("attempt_key", question["id"]) not in attempted_keys
    ]
    not_run = [attempt["id"] for attempt in remaining_attempts]
    latest = {row["id"]: row for row in results}
    operational = any(row.get("outcome") == "OPERATIONAL STOP" for row in latest.values())
    code = FAILED if operational or (not_run and results) else REFUSED if not results else 0
    return {"exit_code": code, "stop_reason": stop_reason, "results": results,
            "not_run": not_run, "metadata": metadata, "charged_usd": budget.charged_usd,
            "call_costs": budget.calls}


def _load_modules(repo_root):
    root = Path(repo_root).resolve()
    for name, module in tuple(sys.modules.items()):
        if name == "core" or name.startswith("core."):
            origin = getattr(module, "__file__", None)
            if origin and not Path(origin).resolve().is_relative_to(root):
                raise OperationRefused("foreign_core_loaded")
    sys.path.insert(0, str(root))
    from core.agent import ask, gemini_research, writer
    from core.agent.tools import sql_query, warehouse
    from core.eval import staging_check
    from core.llm import gemini
    from core.understand import embed

    modules = {"ask": ask, "gemini_research": gemini_research, "writer": writer,
               "warehouse": warehouse, "sql": sql_query, "staging": staging_check,
               "gemini": gemini, "embed": embed}
    for module in modules.values():
        _module_origin(root, module)
    for name, module in tuple(sys.modules.items()):
        if name == "core" or name.startswith("core."):
            origin = getattr(module, "__file__", None)
            if origin and not Path(origin).resolve().is_relative_to(root):
                raise OperationRefused("foreign_core_loaded")
            if origin:
                _module_origin(root, module)
    modules["no_retry_model"] = gemini.GeminiModel(retries=0, timeout_s=120)
    return SimpleNamespace(**modules)


def _load_reporter(repo_root):
    sys.path.insert(0, str(Path(repo_root).resolve()))
    from core.eval import ask_r2_report

    _module_origin(repo_root, ask_r2_report)
    return ask_r2_report


def main(argv=None, *, repo_root=ROOT, runner_path=None, gate_path=None, output_path=OUTPUT_PATH,
         data_dir=DATA_DIR, morning_dir=MORNING_DIR, evaluation_label="R2",
         provider_500_retries=0, allow_resume=True, r3_anchor_report_path=None):
    parser = argparse.ArgumentParser(prog="py -3.13 -m core.eval.ask_r2")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if args.resume and not allow_resume:
        print("Refused: resume is disabled for this evaluation.")
        return REFUSED
    if not args.execute:
        print("Refused: pass --execute after the health gate and source checks are ready.")
        return REFUSED
    try:
        metadata = validate_execution_authority(repo_root=repo_root, runner_path=runner_path, gate_path=gate_path)
    except OperationRefused as exc:
        print(f"Refused: {exc.args[0] if exc.args else 'execution_authority_failed'}")
        return REFUSED
    metadata["evaluation_label"] = evaluation_label
    if args.resume:
        try:
            if not Path(output_path).is_file():
                raise OperationRefused("resume_report_missing")
            saved = _load_resume_prefix(
                data_dir, [{"id": question_id} for question_id in QUESTION_IDS],
                evaluation_label=evaluation_label)
            if evaluation_label == "R3":
                metadata["r3_continuation"] = _r3_resume_provenance(
                    repo_root, data_dir, r3_anchor_report_path, metadata, saved)
            else:
                original_details = _load_original_run_details(repo_root, saved)
                metadata["resume_provenance"] = _resume_provenance(metadata, original_details)
        except OperationRefused as exc:
            print(f"Refused: {exc.args[0] if exc.args else 'resume_preflight_failed'}")
            return REFUSED
    elif Path(output_path).exists() or Path(data_dir).exists():
        print("Refused: R2 output or attempt data already exists.")
        return REFUSED
    try:
        identity = _verify_builder_adc()
    except OperationRefused as exc:
        print(f"Refused: {exc.args[0] if exc.args else 'builder_identity_unproven'}")
        return REFUSED
    except Exception:
        print("Refused: builder_identity_unproven")
        return REFUSED
    if identity.get("principal") != BUILDER_EMAIL or identity.get("project") != PROJECT:
        print("Refused: builder_identity_mismatch")
        return REFUSED
    metadata["builder_identity"] = {
        "principal": identity["principal"],
        "project": identity["project"],
        "credential_type": identity.get("credential_type", "injected-proof"),
        "impersonation_endpoint": identity.get("impersonation_endpoint", "verified ADC target"),
    }
    metadata["ready"] = True
    previous_provider = os.environ.get("MODEL_PROVIDER")
    os.environ["MODEL_PROVIDER"] = "gemini"
    try:
        modules = _load_modules(repo_root)
        if args.resume:
            source_commit = (metadata["r3_continuation"]["initial_source_commit"]
                             if evaluation_label == "R3" else ORIGINAL_SOURCE_COMMIT)
            _verify_resume_module_sources(repo_root, modules, source_commit=source_commit)
        metadata["module_origins"] = {
            name: _module_origin(repo_root, module)
            for name, module in vars(modules).items()
            if hasattr(module, "__file__")
        }
        if getattr(modules.no_retry_model, "retries", 0) != 0:
            raise OperationRefused("gemini_retries_enabled")
        wiring = modules.staging.real_wiring()
        questions = modules.staging.choose_questions(None)
        if tuple(question.get("id") for question in questions) != QUESTION_IDS:
            raise OperationRefused("question_set_mismatch")
        if evaluation_label == "R3":
            from core.eval import ask_r3_coverage

            metadata["module_origins"]["ask_r3_coverage"] = _module_origin(repo_root, ask_r3_coverage)
            metadata["embedding_coverage"] = ask_r3_coverage.read_embedding_coverage(wiring, modules)
        reporter = _load_reporter(repo_root)
        if hasattr(reporter, "__file__"):
            metadata["module_origins"]["ask_r2_report"] = _module_origin(repo_root, reporter)
        morning_records, provenance = reporter.load_morning_records(source_dir=morning_dir)
        metadata["morning_source_paths"] = provenance.get("source_paths", [])
        metadata["morning_sha256"] = provenance.get("sha256", {})
        result = run_questions(questions, wiring=wiring, modules=modules, reporter=reporter,
                               morning_records=morning_records, metadata=metadata,
                               output_path=output_path, data_dir=data_dir,
                               provider_500_retries=provider_500_retries,
                               _execution_token=_EXECUTION_TOKEN, resume=args.resume)
        completed_questions = len({row["id"] for row in result["results"]})
        not_run_attempts = result["metadata"].get("not_run_attempts", result["not_run"])
        print(f"{evaluation_label} status={result['exit_code']}; completed_questions={completed_questions}; "
              f"attempt_records={len(result['results'])}; not_run_attempts={len(not_run_attempts)}; "
              f"charged_model_usd={result.get('charged_usd', 0):.6f}; "
              f"stop_reason={result.get('stop_reason') or 'none'}")
        return result["exit_code"]
    except OperationRefused as exc:
        print(f"Refused: {exc.args[0] if exc.args else 'execution_setup_failed'}")
        return REFUSED
    except Exception as exc:
        print(f"{evaluation_label} failed: {type(exc).__name__}")
        return FAILED
    finally:
        if previous_provider is None:
            os.environ.pop("MODEL_PROVIDER", None)
        else:
            os.environ["MODEL_PROVIDER"] = previous_provider


if __name__ == "__main__":
    raise SystemExit(main())
