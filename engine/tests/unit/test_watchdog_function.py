"""Unit tests for the watchdog Cloud Function decision logic.

functions_framework is the Cloud Function's own runtime dependency, not a test
dependency, so stub it in sys.modules before importing the module. The pure
_decide_alert logic is exercised directly; the email-marker counters run against
a fake BigQuery client that answers from the SQL they build. The SMTP path is
validated by a ?dry=1 probe at deploy time.
"""

import importlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

WATCHDOG_REQUIREMENTS = Path("scripts/watchdog_function/requirements.txt")


def test_watchdog_runtime_keeps_patched_dependency_floors():
    requirements = WATCHDOG_REQUIREMENTS.read_text(encoding="utf-8").splitlines()

    assert "idna>=3.15" in requirements
    assert "protobuf>=6.33.5" in requirements


@pytest.fixture
def watchdog(monkeypatch):
    monkeypatch.setitem(sys.modules, "functions_framework", SimpleNamespace(http=lambda fn: fn))
    sys.modules.pop("main", None)  # avoid collision with scripts/trigger_function/main.py
    sys.path.insert(0, "scripts/watchdog_function")
    import main as watchdog_main  # type: ignore

    importlib.reload(watchdog_main)
    return watchdog_main


def test_decide_alert_no_run(watchdog):
    """No success rows is a no_run alert; it dominates even if a stray email
    marker somehow exists."""
    assert watchdog._decide_alert(0, 0) == "no_run"
    assert watchdog._decide_alert(0, 5) == "no_run"


def test_decide_alert_no_email(watchdog):
    """Success rows but no healthy email marker is the blind spot this fix
    closes: the run completed but the digest never went out."""
    assert watchdog._decide_alert(3, 0) == "no_email"


def test_decide_alert_healthy(watchdog):
    """Ran and recorded a healthy email outcome (sent, or a quiet / already-sent
    skip) is healthy: no alert."""
    assert watchdog._decide_alert(3, 1) is None
    assert watchdog._decide_alert(3, 3) is None


def test_decide_alert_market_incomplete(watchdog):
    """A required market with no success row alerts even when the others
    succeeded and the digest went out. This is the 29-30 Jun ZA-partial blind
    spot: success rows > 0 and a healthy email marker still hid a market outage."""
    assert watchdog._decide_alert(2, 1, ("za",)) == "market_incomplete"
    assert watchdog._decide_alert(2, 0, ("za",)) == "market_incomplete"
    assert watchdog._decide_alert(1, 1, ("ng", "ke")) == "market_incomplete"


def test_decide_alert_no_run_dominates_missing_markets(watchdog):
    """No success rows at all is still no_run, not market_incomplete."""
    assert watchdog._decide_alert(0, 0, ("za", "ng", "ke")) == "no_run"


def test_decide_alert_all_markets_complete_is_healthy(watchdog):
    """No missing markets plus a healthy email marker stays healthy."""
    assert watchdog._decide_alert(3, 1, ()) is None


class _FakeBigQuery:
    """Stand-in for bigquery.Client that answers the watchdog's email-marker
    COUNT queries from an in-memory list of email_status values. It reads the
    IN (...) list out of the SQL the function built, so the count depends on the
    allowed set main.py actually assembled rather than on a canned number.
    Added 5 Sep 2026."""

    def __init__(self, email_statuses):
        self.email_statuses = list(email_statuses)
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        match = re.search(r"email_status IN \(([^)]*)\)", sql)
        assert match, "watchdog email query lost its email_status IN (...) filter"
        allowed = {s.strip().strip("'") for s in match.group(1).split(",")}
        n = sum(1 for status in self.email_statuses if status in allowed)
        job = MagicMock()
        job.result.return_value = iter([SimpleNamespace(n=n)])
        return job


def test_count_healthy_email_ignores_quiet_skip_after_send_failed(watchdog, monkeypatch):
    """A fallback quiet skip must not read as healthy when the primary run
    already recorded send_failed on the same day. Runs the real counting
    functions against a fake client so the allowed set main.py builds is what
    decides the count (rewritten 5 Sep 2026, the old version patched both
    counters and asserted on its own fakes)."""
    fake = _FakeBigQuery(["send_failed", "skipped_nothing_notable"])
    monkeypatch.setattr(watchdog.bigquery, "Client", lambda project: fake)

    assert watchdog._count_email_failures_today("p", "d") == 1
    assert watchdog._count_healthy_email_today("p", "d") == 0
    assert "skipped_nothing_notable" not in fake.queries[-1]
    assert watchdog._decide_alert(3, 0, ()) == "no_email"

    # Same quiet skip with no failure marker is the healthy case.
    fake = _FakeBigQuery(["skipped_nothing_notable"])
    monkeypatch.setattr(watchdog.bigquery, "Client", lambda project: fake)
    assert watchdog._count_healthy_email_today("p", "d") == 1
    assert "skipped_nothing_notable" in fake.queries[-1]


class _ProbeClient:
    def __init__(self, *, scan_error=None, error_count=0):
        self.scan_error = scan_error
        self.error_count = error_count

    def query(self, sql):
        if "system_events" in sql:
            if self.scan_error is not None:
                raise self.scan_error
            rows = [
                SimpleNamespace(
                    n=self.error_count, latest="known failure" if self.error_count else None
                )
            ]
        elif "SELECT DISTINCT market" in sql:
            rows = [SimpleNamespace(market=market) for market in ("za", "ng", "ke")]
        elif "COUNT(DISTINCT market)" in sql:
            rows = [SimpleNamespace(n=3, markets=3)]
        elif "'send_failed', 'render_error'" in sql:
            rows = [SimpleNamespace(n=0)]
        else:
            rows = [SimpleNamespace(n=1)]
        return SimpleNamespace(result=lambda: iter(rows))


@pytest.fixture
def watchdog_request(watchdog, monkeypatch):
    for key, value in {
        "GCP_PROJECT": "synthetic",
        "BQ_DATASET": "synthetic",
        "ALERT_EMAIL": "test@example.invalid",
        "GMAIL_USER": "test@example.invalid",
        "GMAIL_APP_PASSWORD": "synthetic-unused",
    }.items():
        monkeypatch.setenv(key, value)
    sender = MagicMock()
    monkeypatch.setattr(watchdog, "_send_alert", sender)
    return SimpleNamespace(args={"dry": "1"}), sender


@pytest.mark.parametrize(
    "error",
    [PermissionError("denied"), TimeoutError("timeout"), FileNotFoundError("table missing")],
)
def test_failed_error_scan_reaches_existing_http_error(
    watchdog, watchdog_request, monkeypatch, error
):
    request, sender = watchdog_request
    client = _ProbeClient(scan_error=error)
    monkeypatch.setattr(watchdog.bigquery, "Client", lambda **kwargs: client)
    body, status = watchdog.check(request)
    assert status == 500
    assert set(body) == {"error", "date"}
    assert body["error"] == "recent_errors_query: query_failed"
    sender.assert_not_called()


@pytest.mark.parametrize("count", [0, 2])
def test_successful_error_scan_preserves_real_count(watchdog, watchdog_request, monkeypatch, count):
    request, sender = watchdog_request
    client = _ProbeClient(error_count=count)
    monkeypatch.setattr(watchdog.bigquery, "Client", lambda **kwargs: client)
    body, status = watchdog.check(request)
    assert status == 200
    assert body["recent_errors_24h"] == count
    assert body["healthy"] is True
    sender.assert_not_called()


def test_failed_error_scan_and_failed_alert_still_return_http_error(
    watchdog, watchdog_request, monkeypatch
):
    request, sender = watchdog_request
    request.args = {}
    sender.side_effect = RuntimeError("synthetic send failed")
    client = _ProbeClient(scan_error=PermissionError("denied"))
    monkeypatch.setattr(watchdog.bigquery, "Client", lambda **kwargs: client)
    body, status = watchdog.check(request)
    assert status == 500
    assert body["error"] == "recent_errors_query: query_failed"
    sender.assert_called_once()


def test_probe_error_payload_logs_and_alert_keep_only_controlled_context(
    watchdog, watchdog_request, monkeypatch, caplog
):
    request, sender = watchdog_request
    request.args = {}
    private = "synthetic-secret-token SELECT private_record FROM private_table"
    client = _ProbeClient(scan_error=PermissionError(private))
    monkeypatch.setattr(watchdog.bigquery, "Client", lambda **kwargs: client)
    body, status = watchdog.check(request)
    assert status == 500
    assert private not in str(body)
    assert private not in caplog.text
    assert private not in str(sender.call_args)
    assert "recent_errors_query" in str(body)
    assert "PermissionError" in caplog.text
