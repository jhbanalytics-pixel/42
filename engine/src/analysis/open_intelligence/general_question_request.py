"""Normalize general questions after server scope resolution, before persistence."""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime
from uuid import UUID

from src.analysis.open_intelligence.brain_contract import APPROVED_MARKETS, canonical_digest

_VERSION = "general_cultural_question_v1"
_SCOPE_FIELDS = frozenset(
    {"client_scope_id", "market_scope", "brand_config_id", "audience_lens_ids", "theme_id"}
)
_REQUEST_FIELDS = _SCOPE_FIELDS | {
    "contract_version",
    "request_id",
    "request_digest",
    "run_id",
    "question",
    "decision",
    "history",
    "history_omitted_turns",
    "requested_window",
    "as_of",
    "output_form",
    "policy_digest",
}
# The optional client lens the admitted request was bound to. It is present only
# when the request named one, so a general 42 request keeps the record and the
# digest it had before lenses existed, and a lensed one is never byte identical
# to the same wording admitted without it.
_LENS_FIELDS = {"lens_binding_version", "client_lens_id", "configuration_digest"}
_LENS_BINDING_VERSION = "client_lens_binding_v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class QuestionRequestInvalid(ValueError):
    def __init__(self, code: str = "request_invalid") -> None:
        self.code = code
        super().__init__(code)


def _text(value: object, *, code: str = "request_invalid") -> str:
    if not isinstance(value, str):
        raise QuestionRequestInvalid(code)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise QuestionRequestInvalid(code) from exc
    return value


def _identifier(value: object, *, code: str = "scope_invalid") -> str:
    text = _text(value, code=code)
    if not text or text != text.strip():
        raise QuestionRequestInvalid(code)
    return text


def _scope(scope: object) -> dict:
    if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
        raise QuestionRequestInvalid("scope_invalid")
    _identifier(scope["client_scope_id"])
    for field in ("brand_config_id", "theme_id"):
        if scope[field] is not None:
            _identifier(scope[field])
    for field in ("market_scope", "audience_lens_ids"):
        values = scope[field]
        if not isinstance(values, list):
            raise QuestionRequestInvalid("scope_invalid")
        for value in values:
            _identifier(value)
        if len(values) != len(set(values)):
            raise QuestionRequestInvalid("scope_invalid")
    markets = scope["market_scope"]
    if not markets or markets != sorted(markets) or not set(markets) <= APPROVED_MARKETS:
        raise QuestionRequestInvalid("scope_invalid")
    return copy.deepcopy(scope)


def _history(value: object, *, transport: bool) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise QuestionRequestInvalid()
    text_field = "content" if transport else "text"
    result = []
    for turn in value:
        if not isinstance(turn, dict) or set(turn) != {"role", text_field}:
            raise QuestionRequestInvalid()
        if turn["role"] not in ("user", "assistant"):
            raise QuestionRequestInvalid()
        result.append({"role": turn["role"], "text": _text(turn[text_field])})
    return result


def _window(value: object) -> object:
    if value is None:
        return None
    code = "window_invalid"
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise QuestionRequestInvalid(code)
    bounds = []
    for field in ("start", "end"):
        text = _text(value[field], code=code)
        if _DATE.fullmatch(text) is None:
            raise QuestionRequestInvalid(code)
        try:
            bounds.append(date.fromisoformat(text))
        except ValueError as exc:
            raise QuestionRequestInvalid(code) from exc
    if not 0 <= (bounds[1] - bounds[0]).days <= 365:
        raise QuestionRequestInvalid(code)
    return dict(value)


def _uuid(value: object) -> UUID:
    try:
        result = UUID(_text(value))
    except (ValueError, AttributeError) as exc:
        raise QuestionRequestInvalid() from exc
    if str(result) != value:
        raise QuestionRequestInvalid()
    return result


def _policy(value: object) -> str:
    text = _text(value)
    if _DIGEST.fullmatch(text) is None:
        raise QuestionRequestInvalid()
    return text


def _client_lens(value: object) -> dict | None:
    """The three lens identity values, or nothing when the request names no lens."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _LENS_FIELDS:
        raise QuestionRequestInvalid("scope_invalid")
    if value["lens_binding_version"] != _LENS_BINDING_VERSION:
        raise QuestionRequestInvalid("scope_invalid")
    if not _identifier(value["client_lens_id"], code="scope_invalid"):
        raise QuestionRequestInvalid("scope_invalid")
    digest = _text(value["configuration_digest"], code="scope_invalid")
    if _DIGEST.fullmatch(digest) is None:
        raise QuestionRequestInvalid("scope_invalid")
    return {key: value[key] for key in sorted(_LENS_FIELDS)}


def normalize_question_request(
    transport: dict,
    *,
    scope: dict,
    request_id: str,
    admitted_at: datetime,
    policy_digest: str,
    requested_window: dict | None = None,
    decision: str | None = None,
    output_form: str = "answer",
    client_lens: dict | None = None,
) -> dict:
    """Consume message/history only; the caller has already resolved transport market."""
    if not isinstance(transport, dict) or not {"message"} <= set(transport) <= {
        "message",
        "history",
    }:
        raise QuestionRequestInvalid()
    resolved_scope = _scope(scope)
    request_uuid = _uuid(request_id)
    if not isinstance(admitted_at, datetime) or admitted_at.utcoffset() is None:
        raise QuestionRequestInvalid()
    question = _text(transport["message"]).strip()
    if not question or len(question) > 4000:
        raise QuestionRequestInvalid()
    history = _history(transport.get("history", []), transport=True)
    if history and len(history[-1]["text"]) > 16000:
        raise QuestionRequestInvalid()
    retained = history[-8:]
    total = sum(len(turn["text"]) for turn in retained)
    while total > 16000:
        total -= len(retained.pop(0)["text"])
    request = {
        "contract_version": _VERSION,
        "request_id": request_id,
        "run_id": f"question_{request_uuid.hex}",
        **resolved_scope,
        "question": question,
        "decision": decision,
        "history": retained,
        "history_omitted_turns": len(history) - len(retained),
        "requested_window": _window(requested_window),
        "as_of": admitted_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "output_form": output_form,
        "policy_digest": _policy(policy_digest),
    }
    lens = _client_lens(client_lens)
    if lens is not None:
        request["client_lens"] = lens
    # Validate scalars before canonical encoding, so refusals never expose user text.
    if decision is not None and (not _text(decision).strip() or len(decision) > 2000):
        raise QuestionRequestInvalid()
    if output_form not in ("answer", "comparison", "briefing"):
        raise QuestionRequestInvalid()
    request["request_digest"] = canonical_digest(request)
    return validate_question_request(request, scope=resolved_scope, policy_digest=policy_digest)


def validate_question_request(value: dict, *, scope: dict, policy_digest: str) -> dict:
    """Validate stored data without granting policy authority or re-trimming history."""
    if not isinstance(value, dict) or not _REQUEST_FIELDS <= set(value) <= _REQUEST_FIELDS | {
        "client_lens"
    }:
        raise QuestionRequestInvalid()
    _client_lens(value.get("client_lens"))
    actual_scope = _scope({key: value[key] for key in _SCOPE_FIELDS})
    if actual_scope != _scope(scope):
        raise QuestionRequestInvalid("scope_invalid")
    if value["contract_version"] != _VERSION or _policy(value["policy_digest"]) != _policy(
        policy_digest
    ):
        raise QuestionRequestInvalid()
    request_uuid = _uuid(value["request_id"])
    if value["run_id"] != f"question_{request_uuid.hex}":
        raise QuestionRequestInvalid()
    question = _text(value["question"])
    if not question or question != question.strip() or len(question) > 4000:
        raise QuestionRequestInvalid()
    decision = value["decision"]
    if decision is not None and (not _text(decision).strip() or len(decision) > 2000):
        raise QuestionRequestInvalid()
    history = _history(value["history"], transport=False)
    if len(history) > 8 or sum(len(turn["text"]) for turn in history) > 16000:
        raise QuestionRequestInvalid()
    omissions = value["history_omitted_turns"]
    if type(omissions) is not int or omissions < 0:
        raise QuestionRequestInvalid()
    _window(value["requested_window"])
    timestamp = _text(value["as_of"])
    if _TIMESTAMP.fullmatch(timestamp) is None:
        raise QuestionRequestInvalid()
    try:
        datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise QuestionRequestInvalid() from exc
    if value["output_form"] not in ("answer", "comparison", "briefing"):
        raise QuestionRequestInvalid()
    digest = _policy(value["request_digest"])
    if digest != canonical_digest(
        {key: item for key, item in value.items() if key != "request_digest"}
    ):
        raise QuestionRequestInvalid()
    return copy.deepcopy(value)


def validate_question_retry(
    existing: dict, requested: dict, *, scope: dict, policy_digest: str
) -> dict:
    stored = validate_question_request(existing, scope=scope, policy_digest=policy_digest)
    incoming = validate_question_request(requested, scope=scope, policy_digest=policy_digest)
    if stored != incoming:
        raise QuestionRequestInvalid("run_id_conflict")
    return stored


def history_limitation(request: dict) -> str | None:
    omitted = request["history_omitted_turns"]
    if type(omitted) is not int or omitted < 0:
        raise QuestionRequestInvalid()
    if not omitted:
        return None
    noun = "turn was" if omitted == 1 else "turns were"
    return f"{omitted} earlier conversation {noun} omitted to fit the context limit."
