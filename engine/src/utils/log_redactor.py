"""Log redaction filter for credentials and raw request content."""

from __future__ import annotations

import logging
import math
import re
import traceback
from collections.abc import Mapping
from typing import Any

REDACTED = "***REDACTED***"
WITHHELD = "***WITHHELD***"
_UNAVAILABLE = "***UNAVAILABLE***"
_RECURSIVE_REFERENCE = "***RECURSIVE_REFERENCE***"
_LOG_FORMAT_FAILED = "[LOG_FORMAT_FAILED]"
_LOG_REDACTION_FAILED = "[LOG_REDACTION_FAILED]"
_TRACEBACK_REDACTION_FAILED = "[TRACEBACK_REDACTION_FAILED]"
_STACK_REDACTION_FAILED = "[STACK_REDACTION_FAILED]"

_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "apikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "password",
        "passcode",
        "authorization",
        "xapikey",
        "xpasscode",
        "key",
    }
)
_RAW_CONTENT_KEYS = frozenset(
    {
        "q",
        "question",
        "prompt",
        "messages",
        "history",
        "requestbody",
        "responsebody",
        "rawpayload",
    }
)

_SECRET_KEY_PATTERN = (
    r"(?:token|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|passcode|authorization|x[_-]?api[_-]?key|"
    r"x[_-]?passcode|key)"
)
_RAW_KEY_PATTERN = (
    r"(?:q|question|prompt|messages|history|request[_-]?body|response[_-]?body|"
    r"raw[_-]?payload)"
)
_BEARER_PATTERN = re.compile(r"(?P<prefix>\bBearer\s+)(?P<value>[^\s,;\"'}\]]+)", re.IGNORECASE)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rf"(?P<prefix>(?<![\w-])(?P<key>{_SECRET_KEY_PATTERN})(?:[\"'])?\s*[:=]\s*)",
    re.IGNORECASE,
)
_RAW_ASSIGNMENT_PATTERN = re.compile(
    rf"(?P<prefix>(?<![\w-])(?P<key>{_RAW_KEY_PATTERN})(?:[\"'])?\s*[:=]\s*)",
    re.IGNORECASE,
)
_PULSE_PATH_PATTERN = re.compile(r"pulse/\d{4}-\d{2}-\d{2}-[A-Za-z0-9_-]+\.html", re.IGNORECASE)


def _normalise_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _line_end(value: str, start: int) -> int:
    endings = [
        position for position in (value.find("\r", start), value.find("\n", start)) if position >= 0
    ]
    return min(endings, default=len(value))


def _quoted_value_end(value: str, start: int, limit: int) -> int | None:
    quote = value[start]
    position = start + 1
    while position < limit:
        if value[position] == "\\":
            position += 2
            continue
        if value[position] == quote:
            return position + 1
        position += 1
    return None


def _container_value_end(value: str, start: int, limit: int) -> int | None:
    closing = {"[": "]", "{": "}", "(": ")"}
    stack = [closing[value[start]]]
    position = start + 1
    while position < limit:
        char = value[position]
        if char in "\"'":
            quoted_end = _quoted_value_end(value, position, limit)
            if quoted_end is None:
                return None
            position = quoted_end
            continue
        if char in closing:
            stack.append(closing[char])
        elif char == stack[-1]:
            stack.pop()
            if not stack:
                return position + 1
        position += 1
    return None


def _is_url_query_assignment(value: str, start: int) -> bool:
    if start == 0 or value[start - 1] not in "?&":
        return False
    prefix = value[:start]
    token_start = max(prefix.rfind(char) for char in " \t\r\n") + 1
    token_prefix = prefix[token_start:]
    return "://" in token_prefix and "?" in token_prefix


def _assigned_value_span(
    value: str,
    start: int,
    *,
    raw_content: bool,
    authorization: bool,
    url_query: bool,
) -> tuple[int, str]:
    line_limit = _line_end(value, start)
    if start >= line_limit:
        return start, "bare"

    first = value[start]
    if first in "\"'":
        end = _quoted_value_end(value, start, len(value))
        return (end, first) if end is not None else (len(value), "bare")
    if first in "[{(":
        end = _container_value_end(value, start, len(value))
        return (end, "container") if end is not None else (len(value), "bare")

    if raw_content:
        if not url_query:
            return len(value), "bare"
        boundaries = [
            position
            for position in (value.find("&", start), value.find("#", start))
            if position >= 0
        ]
        return min(boundaries, default=len(value)), "bare"

    position = start
    while position < line_limit:
        char = value[position]
        if char in ",;&}]" or (not authorization and char.isspace()):
            break
        position += 1
    return position, "bare"


def _redact_assignments(
    value: str,
    pattern: re.Pattern[str],
    marker: str,
    *,
    raw_content: bool,
) -> str:
    output: list[str] = []
    cursor = 0
    for match in pattern.finditer(value):
        if match.start() < cursor:
            continue
        authorization = _normalise_key(match.group("key")) == "authorization"
        end, style = _assigned_value_span(
            value,
            match.end(),
            raw_content=raw_content,
            authorization=authorization,
            url_query=_is_url_query_assignment(value, match.start()),
        )
        if end == match.end():
            continue
        output.append(value[cursor : match.end()])
        if style in "\"'":
            output.append(f"{style}{marker}{style}")
        elif style == "container":
            output.append(f'"{marker}"')
        else:
            output.append(marker)
        cursor = end
    output.append(value[cursor:])
    return "".join(output)


def redact_text(value: str) -> str:
    """Redact named credentials and raw request fields in diagnostic text."""
    redacted = _redact_assignments(value, _SECRET_ASSIGNMENT_PATTERN, REDACTED, raw_content=False)
    redacted = _redact_assignments(redacted, _RAW_ASSIGNMENT_PATTERN, WITHHELD, raw_content=True)
    redacted = _BEARER_PATTERN.sub(lambda match: f"{match.group('prefix')}{REDACTED}", redacted)
    return _PULSE_PATH_PATTERN.sub("pulse/YYYY-MM-DD-***REDACTED***.html", redacted)


def _sanitize_value(value: Any, ancestors: set[int]) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in ancestors:
            return _RECURSIVE_REFERENCE
        ancestors.add(identity)
        try:
            sanitized = {}
            for key, item in value.items():
                normalized = _normalise_key(key)
                if normalized in _SENSITIVE_KEYS:
                    sanitized[key] = REDACTED
                elif normalized in _RAW_CONTENT_KEYS:
                    sanitized[key] = WITHHELD
                else:
                    sanitized[key] = _sanitize_value(item, ancestors)
            return sanitized
        finally:
            ancestors.remove(identity)
    if isinstance(value, tuple):
        identity = id(value)
        if identity in ancestors:
            return _RECURSIVE_REFERENCE
        ancestors.add(identity)
        try:
            return tuple(_sanitize_value(item, ancestors) for item in value)
        finally:
            ancestors.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in ancestors:
            return _RECURSIVE_REFERENCE
        ancestors.add(identity)
        try:
            return [_sanitize_value(item, ancestors) for item in value]
        finally:
            ancestors.remove(identity)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def sanitize_value(value: Any) -> Any:
    """Return a recursively sanitized copy suitable for logs or JSON."""
    try:
        return _sanitize_value(value, set())
    except Exception:
        return _UNAVAILABLE


class TokenRedactingFilter(logging.Filter):
    """Strips credentials and request content from a complete log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            formatted_message = record.getMessage()
        except Exception:
            try:
                formatted_message = f"{record.msg} {_LOG_FORMAT_FAILED}"
            except Exception:
                formatted_message = _LOG_FORMAT_FAILED
        try:
            record.msg = redact_text(str(formatted_message))
        except Exception:
            record.msg = _LOG_REDACTION_FAILED
        record.args = ()

        if record.exc_text:
            traceback_text = record.exc_text
        elif record.exc_info:
            try:
                traceback_text = "".join(traceback.format_exception(*record.exc_info))
            except Exception:
                traceback_text = None
        else:
            traceback_text = None
        if traceback_text is not None:
            try:
                record.exc_text = redact_text(traceback_text)
            except Exception:
                record.exc_text = _TRACEBACK_REDACTION_FAILED
                record.exc_info = None

        if record.stack_info:
            try:
                record.stack_info = redact_text(record.stack_info)
            except Exception:
                record.stack_info = _STACK_REDACTION_FAILED
        return True


def get_logger(name: str) -> logging.Logger:
    """Create a logger with token redaction enabled."""
    logger = logging.getLogger(name)
    if not any(isinstance(filter_, TokenRedactingFilter) for filter_ in logger.filters):
        logger.addFilter(TokenRedactingFilter())
    return logger
