"""Unit tests for the log redaction filter."""

import copy
import logging
import sys

import pytest
from src.utils.log_redactor import TokenRedactingFilter, get_logger, redact_text


def _apply(msg: str, *args) -> tuple[str, tuple]:
    """Run the redacting filter on a LogRecord and return the mutated fields."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg=msg,
        args=args,
        exc_info=None,
    )
    TokenRedactingFilter().filter(record)
    return record.msg, record.args


def _format(record: logging.LogRecord) -> str:
    TokenRedactingFilter().filter(record)
    return logging.Formatter("%(levelname)s %(message)s").format(record)


def test_redacts_ensembledata_token():
    """A URL query string containing ?token=... has the token value redacted."""
    msg, _ = _apply("GET https://api.example.com/path?token=ABC123XYZ")
    assert "ABC123XYZ" not in msg
    assert "***REDACTED***" in msg


def test_redacts_authorization_bearer():
    """An Authorization: Bearer header string has the bearer token redacted."""
    msg, _ = _apply("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdef")
    assert "eyJhbGciOiJIUzI1NiJ9.abcdef" not in msg
    assert "***REDACTED***" in msg


def test_redacts_x_api_key_header():
    """An X-Api-Key header string has the key value redacted."""
    msg, _ = _apply("X-Api-Key: supersecretkey123")
    assert "supersecretkey123" not in msg
    assert "***REDACTED***" in msg


def test_does_not_redact_plain_text():
    """Normal log messages pass through the filter unchanged."""
    original = "Connector fetched 42 rows for market=za"
    msg, _ = _apply(original)
    assert msg == original


def test_redacts_multiple_tokens_in_one_string():
    """Multiple patterns in a single message all get redacted."""
    msg, _ = _apply("token=ONE and api_key=TWO and Bearer THREE and X-Api-Key: FOUR")
    for leaked in ("ONE", "TWO", "THREE", "FOUR"):
        assert leaked not in msg, f"{leaked!r} was not redacted in {msg!r}"
    assert msg.count("***REDACTED***") >= 4


def test_redacts_tokens_in_log_args():
    """String args are redacted in final output without changing caller values."""
    caller_args = ("token=LEAKED",)
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="fetched %s",
        args=caller_args,
        exc_info=None,
    )

    rendered = _format(record)

    assert "LEAKED" not in rendered
    assert "***REDACTED***" in rendered
    assert caller_args == ("token=LEAKED",)


def test_mapping_arguments_stay_usable_and_do_not_mutate_the_caller():
    caller_args = {"stage": "fetch", "token": "SYNTHETIC_SECRET_MAPPING"}
    original = copy.deepcopy(caller_args)
    record = logging.LogRecord(
        "probe",
        logging.INFO,
        __file__,
        1,
        "stage=%(stage)s token=%(token)s",
        (caller_args,),
        None,
    )

    rendered = _format(record)

    assert "stage=fetch" in rendered
    assert "SYNTHETIC_SECRET_MAPPING" not in rendered
    assert caller_args == original


@pytest.mark.parametrize(
    ("message", "args", "visible", "hidden", "marker"),
    [
        (
            "stage=fetch token=%s",
            ("SYNTHETIC_SECRET_TUPLE",),
            "stage=fetch",
            "SYNTHETIC_SECRET_TUPLE",
            "***REDACTED***",
        ),
        (
            "stage=fetch prompt=%s",
            ("SYNTHETIC_RAW_TUPLE",),
            "stage=fetch",
            "SYNTHETIC_RAW_TUPLE",
            "***WITHHELD***",
        ),
        (
            "Authorization: Bearer %s",
            ("SYNTHETIC_SECRET_BEARER_PLACEHOLDER.a/b+c==",),
            "Authorization:",
            "SYNTHETIC_SECRET_BEARER_PLACEHOLDER",
            "***REDACTED***",
        ),
        (
            'password="%s"',
            ("SYNTHETIC_SECRET_QUOTED_PLACEHOLDER",),
            "password=",
            "SYNTHETIC_SECRET_QUOTED_PLACEHOLDER",
            "***REDACTED***",
        ),
    ],
)
def test_completed_tuple_message_is_sanitized_without_breaking_formatting(
    message, args, visible, hidden, marker
):
    caller_args = copy.deepcopy(args)
    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, message, args, None)

    rendered = _format(record)

    assert visible in rendered
    assert hidden not in rendered
    assert marker in rendered
    assert args == caller_args


def test_completed_mapping_message_redacts_sensitive_alias_without_mutating_caller():
    caller_args = {"stage": "fetch", "value": "SYNTHETIC_SECRET_MAPPING_ALIAS"}
    original = copy.deepcopy(caller_args)
    record = logging.LogRecord(
        "probe",
        logging.ERROR,
        __file__,
        1,
        "stage=%(stage)s token=%(value)s",
        (caller_args,),
        None,
    )

    rendered = _format(record)

    assert "stage=fetch" in rendered
    assert "SYNTHETIC_SECRET_MAPPING_ALIAS" not in rendered
    assert "***REDACTED***" in rendered
    assert caller_args == original


def test_tuple_arguments_keep_numeric_formatting_and_sanitize_nested_values():
    payload = {
        "nested": [{"api_key": "SYNTHETIC_SECRET_NESTED", "rows": 3}],
        "note": "Authorization: Bearer SYNTHETIC_SECRET_NESTED_BEARER.a/b+c==",
    }
    original = copy.deepcopy(payload)
    record = logging.LogRecord(
        "probe",
        logging.INFO,
        __file__,
        1,
        "count=%d payload=%s",
        (7, payload),
        None,
    )

    rendered = _format(record)

    assert "count=7" in rendered
    assert "'rows': 3" in rendered
    assert "SYNTHETIC_SECRET_NESTED" not in rendered
    assert "SYNTHETIC_SECRET_NESTED_BEARER" not in rendered
    assert payload == original
    assert isinstance(record.args, tuple)


def test_redacts_punctuated_credentials_and_withholds_raw_request_fields():
    sentinels = (
        "SYNTHETIC_SECRET_URL.!$%2F",
        "SYNTHETIC_SECRET_JSON:/?=+",
        "SYNTHETIC_SECRET_BEARER.a/b+c==",
        "SYNTHETIC_SECRET_PASSCODE.;:/?=+",
        "SYNTHETIC_RAW_PROMPT_TEXT",
    )
    record = logging.LogRecord(
        "probe",
        logging.ERROR,
        __file__,
        1,
        (
            "connector=synthetic GET https://example.invalid/run?token="
            "SYNTHETIC_SECRET_URL.!$%2F&ok=1 "
            '"api_key": "SYNTHETIC_SECRET_JSON:/?=+", '
            "Authorization: Bearer SYNTHETIC_SECRET_BEARER.a/b+c== "
            "X-Passcode: 'SYNTHETIC_SECRET_PASSCODE.;:/?=+' "
            'prompt="SYNTHETIC_RAW_PROMPT_TEXT"'
        ),
        (),
        None,
    )

    rendered = _format(record)

    assert "connector=synthetic" in rendered
    assert "ok=1" in rendered
    assert all(sentinel not in rendered for sentinel in sentinels)
    assert "***REDACTED***" in rendered
    assert "***WITHHELD***" in rendered


@pytest.mark.parametrize(
    ("message", "hidden", "marker"),
    [
        (
            '{"messages": [{"role": "user", "content": "SYNTHETIC_RAW_CONTAINER"}]}',
            "SYNTHETIC_RAW_CONTAINER",
            "***WITHHELD***",
        ),
        (
            r'{"prompt": "safe \" SYNTHETIC_RAW_ESCAPED"}',
            "SYNTHETIC_RAW_ESCAPED",
            "***WITHHELD***",
        ),
        (
            r'{"password": "safe \" SYNTHETIC_SECRET_ESCAPED"}',
            "SYNTHETIC_SECRET_ESCAPED",
            "***REDACTED***",
        ),
        (
            "Authorization: Basic SYNTHETIC_SECRET_BASIC",
            "SYNTHETIC_SECRET_BASIC",
            "***REDACTED***",
        ),
        (
            '{"messages": [{"content": "first line"\nSYNTHETIC_RAW_UNPARSEABLE',
            "SYNTHETIC_RAW_UNPARSEABLE",
            "***WITHHELD***",
        ),
    ],
)
def test_serialized_protected_values_are_consumed_as_complete_values(message, hidden, marker):
    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, message, (), None)

    rendered = _format(record)

    assert hidden not in rendered
    assert marker in rendered


@pytest.mark.parametrize(
    "message",
    [
        "question=rent, SYNTHETIC_PRIVATE_COMMA_REMAINDER",
        "prompt=hello; SYNTHETIC_PRIVATE_SEMICOLON_REMAINDER",
        "history=first & SYNTHETIC_PRIVATE_AMPERSAND_REMAINDER",
    ],
)
def test_unquoted_raw_fields_withhold_ambiguous_punctuation_tail(message):
    redacted = redact_text(message)

    assert "SYNTHETIC_PRIVATE" not in redacted
    assert "***WITHHELD***" in redacted


def test_url_query_boundary_preserves_following_nonprotected_parameter():
    redacted = redact_text("GET https://example.invalid/search?q=rent&market=za")

    assert redacted == "GET https://example.invalid/search?q=***WITHHELD***&market=za"


def test_recursive_arguments_fail_closed_after_final_message_formatting():
    loop = []
    loop.append(loop)
    record = logging.LogRecord(
        "probe",
        logging.ERROR,
        __file__,
        1,
        "payload=%s secret=%s",
        (loop, "token=SYNTHETIC_SECRET_CYCLE"),
        None,
    )

    rendered = _format(record)

    assert "payload=[[...]]" in rendered
    assert "SYNTHETIC_SECRET_CYCLE" not in rendered
    assert "***REDACTED***" in rendered
    assert loop[0] is loop


def test_formatting_failure_uses_safe_message_and_still_sanitizes_traceback():
    try:
        raise RuntimeError("password=SYNTHETIC_SECRET_FORMAT_TRACE")
    except RuntimeError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "probe",
        logging.ERROR,
        __file__,
        1,
        "count=%d",
        ("token=SYNTHETIC_SECRET_BAD_FORMAT",),
        exc_info,
        sinfo="token=SYNTHETIC_SECRET_FORMAT_STACK",
    )

    rendered = _format(record)

    assert "LOG_FORMAT_FAILED" in rendered
    assert "RuntimeError" in rendered
    assert "SYNTHETIC_SECRET_BAD_FORMAT" not in rendered
    assert "SYNTHETIC_SECRET_FORMAT_TRACE" not in rendered
    assert "SYNTHETIC_SECRET_FORMAT_STACK" not in rendered


def _raise_synthetic_failure() -> None:
    raise RuntimeError("fetch failed password=SYNTHETIC_SECRET_TRACE.!:/?=+")


def test_exception_and_stack_text_are_sanitized_without_losing_diagnostics():
    try:
        _raise_synthetic_failure()
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        "probe",
        logging.ERROR,
        __file__,
        1,
        "connector failure",
        (),
        exc_info,
        func="diagnostic_probe",
        sinfo="Stack source=get_results token=SYNTHETIC_SECRET_STACK.!:/?=+",
    )

    rendered = _format(record)

    assert "connector failure" in rendered
    assert "RuntimeError" in rendered
    assert "_raise_synthetic_failure" in rendered
    assert "source=get_results" in rendered
    assert "SYNTHETIC_SECRET_TRACE" not in rendered
    assert "SYNTHETIC_SECRET_STACK" not in rendered


def test_get_logger_attaches_redacting_filter():
    """get_logger returns a Logger with a TokenRedactingFilter attached."""
    logger = get_logger("trends.test.redactor")
    assert any(isinstance(f, TokenRedactingFilter) for f in logger.filters)


def test_get_logger_does_not_accumulate_equivalent_filters():
    logger = logging.getLogger("trends.test.redactor.duplicates")
    logger.filters.clear()

    get_logger(logger.name)
    get_logger(logger.name)

    assert sum(isinstance(f, TokenRedactingFilter) for f in logger.filters) == 1
