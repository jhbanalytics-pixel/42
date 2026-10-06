import contextlib
import importlib.util
import inspect
import io
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "staging_demo_check.py"
SPEC = importlib.util.spec_from_file_location("staging_demo_check", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("staging demo checker module could not be loaded")
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


ORIGIN = "https://f42-api-590353929363.us-central1.run.app"
SECRET = "never-print-this-value"
SAST = timezone(timedelta(hours=2), "SAST")


def function(name):
    value = getattr(checker, name, None)
    if not callable(value):
        raise AssertionError(f"checker function is missing: {name}")
    return value


class FakeHealthResponse:
    status_code = 200
    history = []
    text = '{"ok":true,"service":"f42-api","version":"84583d04f5f2","auth_mode":"passcode","passcode":true,"checks":{"auth":"ok","bigquery":"ok","agent":"ok"}}'

    def json(self):
        return {
            "ok": True,
            "service": "f42-api",
            "version": "84583d04f5f2",
            "auth_mode": "passcode",
            "passcode": True,
            "checks": {"auth": "ok", "bigquery": "ok", "agent": "ok"},
        }


class FakeHealthClient:
    def __init__(self, response=None):
        self.response = response or FakeHealthResponse()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class StagingDemoCheckerTests(unittest.TestCase):
    def test_base_url_accepts_only_the_allowlisted_staging_origin(self):
        validate = function("validate_base_url")
        self.assertEqual(validate(ORIGIN), ORIGIN)
        self.assertEqual(validate(ORIGIN + "/"), ORIGIN)
        for value in (
            "http://f42-api-590353929363.us-central1.run.app",
            "https://f42-api-prod-590353929363.us-central1.run.app",
            "https://user:secret@f42-api-590353929363.us-central1.run.app",
            ORIGIN + "/path",
            ORIGIN + "?token=hidden",
            ORIGIN + "#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)

    def test_network_policy_allows_only_same_origin_get_and_head(self):
        allows = function("allows_request")
        self.assertTrue(allows("GET", ORIGIN + "/api/today", ORIGIN))
        self.assertTrue(allows("HEAD", ORIGIN + "/", ORIGIN))
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertFalse(allows(method, ORIGIN + "/api/today", ORIGIN))
        self.assertFalse(allows("GET", "https://example.com/image.png", ORIGIN))
        self.assertFalse(allows("GET", ORIGIN + "/api/ask/a_123/events", ORIGIN))
        for suffix in (
            "/api/ask/a_123%2Fevents",
            "/api%2Fask/a_123/events",
            "/api/ask%2Fa_123/events",
            "/api/ask/a_123%5cevents",
            "/api/ask/a_123%255cevents",
            "/%61pi/ask/a_123/events",
            "/api/ask/a_123/%65vents",
        ):
            with self.subTest(suffix=suffix):
                self.assertFalse(allows("GET", ORIGIN + suffix, ORIGIN))
        self.assertFalse(allows("GET", "wss://" + ORIGIN.split("//", 1)[1] + "/socket", ORIGIN))

    def test_health_fetch_is_public_unredirected_and_version_is_hex(self):
        request_health = function("request_health")
        evaluate_health = function("evaluate_health")
        client = FakeHealthClient()
        observation = request_health(ORIGIN, client=client)
        self.assertEqual(len(client.calls), 1)
        url, kwargs = client.calls[0]
        self.assertEqual(url, ORIGIN + "/api/health")
        self.assertIs(kwargs.get("follow_redirects"), False)
        self.assertNotIn("headers", kwargs)
        self.assertEqual(evaluate_health(observation).status, "PASS")
        self.assertEqual(evaluate_health(observation, "84583d04").status, "PASS")
        self.assertEqual(evaluate_health(observation, "11111111").status, "FAIL")

        response = FakeHealthResponse()
        response.status_code = 302
        response.history = []
        redirected = evaluate_health(
            checker.HealthObservation(302, True, response.json(), response.text)
        )
        self.assertEqual(redirected.status, "FAIL")

        invalid = response.json()
        invalid["version"] = "dev"
        self.assertEqual(
            evaluate_health(checker.HealthObservation(200, False, invalid, "")).status,
            "FAIL",
        )

    def test_today_date_checks_sast_warning_and_visible_explicit_date(self):
        evaluate = function("evaluate_today_date")
        today = date(2026, 10, 1)
        self.assertEqual(
            evaluate("2026-10-01", today, False, "Brief for 1 October 2026").status,
            "PASS",
        )
        self.assertEqual(
            evaluate("2026-09-30", today, True, "Brief for 30 September 2026").status,
            "PASS",
        )
        self.assertEqual(evaluate("2026-09-30", today, False, "Brief for 30 September 2026").status, "FAIL")
        self.assertEqual(evaluate("2026-10-02", today, False, "Brief for 2 October 2026").status, "FAIL")

    def test_today_availability_accepts_either_live_empty_or_partial_case(self):
        evaluate = function("evaluate_today_availability")
        expected_banner = function("expected_partial_banner")
        payload = {"status": "partial", "markets": [
            {"market": "ZA", "label": "South Africa", "status": "published"},
            {"market": "NG", "label": "Nigeria", "status": "partial"},
        ]}
        banner = expected_banner(payload)
        self.assertEqual(banner, "Some markets are incomplete: Nigeria.")
        empty_with_link = [{"reason_visible": True, "history_visible": True, "history_href": "#/history"}]
        self.assertEqual(evaluate([], "published", "").status, "FAIL")
        self.assertEqual(evaluate([], "published", "").kind, "missing_case")
        self.assertEqual(evaluate(empty_with_link, "published", "").status, "PASS")
        self.assertEqual(evaluate([], "partial", banner, expected_banner=banner).kind, "missing_case")
        self.assertEqual(evaluate(empty_with_link, "partial", banner, expected_banner=banner).status, "PASS")
        self.assertEqual(evaluate([], "partial", "wrong copy", expected_banner=banner).status, "FAIL")
        self.assertEqual(
            evaluate(empty_with_link, "partial", "wrong copy", expected_banner=banner).status,
            "FAIL",
        )
        self.assertEqual(evaluate(empty_with_link, "partial", banner).status, "FAIL")
        self.assertEqual(
            evaluate([{"reason_visible": False, "history_visible": False}], "published", "").status,
            "FAIL",
        )

    def test_partial_banner_matches_today_page_wording(self):
        expected_banner = function("expected_partial_banner")
        self.assertEqual(
            expected_banner({"markets": [
                {"market": "ZA", "label": "South Africa", "status": "partial"},
                {"market": "NG", "label": "  ", "status": "data_issue"},
                {"market": "KE", "label": "Kenya", "status": "published"},
            ]}),
            "Some markets are incomplete: South Africa, NG.",
        )
        self.assertEqual(
            expected_banner({"markets": [{"market": "ZA", "label": "South Africa", "status": "published"}]}),
            "Some trends are shown without an explanation yet.",
        )
        self.assertIsNone(expected_banner(None))
        self.assertFalse(hasattr(checker, "PARTIAL_BANNER"))

    def test_history_link_is_page_level_when_every_market_is_empty(self):
        evaluate = function("evaluate_today_availability")
        page_link = {"visible": True, "href": "#/history"}
        no_page_link = {"visible": False, "href": None}
        reason_only = [
            {"reason_visible": True, "history_visible": False, "history_href": None},
            {"reason_visible": True, "history_visible": False, "history_href": None},
        ]
        self.assertEqual(
            evaluate(reason_only, "published", "", page_history=page_link, all_markets_empty=True).status,
            "PASS",
        )
        self.assertEqual(
            evaluate(reason_only, "published", "", page_history=no_page_link, all_markets_empty=True).status,
            "FAIL",
        )
        self.assertEqual(
            evaluate(
                reason_only, "published", "",
                page_history={"visible": True, "href": "#/ask"}, all_markets_empty=True,
            ).status,
            "FAIL",
        )
        missing_reason = [{"reason_visible": False, "history_visible": False, "history_href": None}]
        self.assertEqual(
            evaluate(missing_reason, "published", "", page_history=page_link, all_markets_empty=True).status,
            "FAIL",
        )
        doubled = [{"reason_visible": True, "history_visible": True, "history_href": "#/history"}]
        self.assertEqual(
            evaluate(doubled, "published", "", page_history=page_link, all_markets_empty=True).status,
            "FAIL",
        )
        self.assertEqual(evaluate(reason_only[:1], "published", "", page_history=page_link).status, "FAIL")
        self.assertEqual(evaluate(reason_only[:1], "published", "", page_history=no_page_link).status, "FAIL")

    def test_today_wait_is_long_enough_and_load_time_is_reported(self):
        self.assertEqual(checker.TODAY_WAIT_MS, 45000)
        source = inspect.getsource(function("_run_browser_checks"))
        self.assertIn("timeout=TODAY_WAIT_MS", source)
        self.assertIn('metadata["today_load_seconds"]', source)
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 10, 1, 12, 37, tzinfo=SAST)
            result = checker.CheckResult("Health", "PASS", "ok")
            report = function("write_report")(
                directory, [result], now=now,
                metadata={"today_load_seconds": 31.46, "today_loaded": True},
            ).read_text(encoding="utf-8")
            self.assertIn("Today load time: 31.5 s", report)
            slow = function("render_report")(
                [result], now, metadata={"today_load_seconds": 45.02, "today_loaded": False},
            )
            self.assertIn("Today load time: not loaded after 45.0 s", slow)
            unobserved = function("render_report")([result], now, metadata={})
            self.assertIn("Today load time: unobserved", unobserved)

    def test_search_caption_requires_a_live_populated_case(self):
        evaluate = function("evaluate_searching_now")
        missing = evaluate(None, False, [])
        self.assertEqual(missing.status, "FAIL")
        self.assertEqual(missing.kind, "missing_case")
        empty = evaluate(0, False, [])
        self.assertEqual(empty.status, "FAIL")
        self.assertIn("strip was hidden", empty.detail)
        self.assertEqual(evaluate(0, True, ["wrong caption"]).kind, "ui_failure")
        self.assertEqual(evaluate(2, True, ["Google search interest, not posts"]).status, "PASS")
        self.assertEqual(evaluate(2, True, ["Google search interest, not posts", "wrong"]).status, "FAIL")

    def test_saved_answer_requires_one_get_without_ask_post_or_event_stream(self):
        evaluate = function("evaluate_saved_answer")
        requests = [
            {"method": "GET", "path": "/api/ask/a_123"},
            {"method": "GET", "path": "/api/history/asks"},
        ]
        self.assertEqual(evaluate("a_123", True, requests).status, "PASS")
        self.assertEqual(evaluate("a_123", False, requests).status, "FAIL")
        self.assertEqual(evaluate("a_123", True, requests + requests[:1]).status, "FAIL")
        self.assertEqual(
            evaluate("a_123", True, requests + [{"method": "POST", "path": "/api/ask"}]).status,
            "FAIL",
        )
        for event_path in (
            "/api/ask/a_123/events",
            "/api/ask/a_123%2Fevents",
            "/api%2Fask/a_123/events",
            "/api/ask%2Fa_123/events",
            "/api/ask/a_123%5cevents",
            "/api/ask/a_123%255cevents",
        ):
            with self.subTest(event_path=event_path):
                self.assertEqual(
                    evaluate("a_123", True, requests + [{"method": "GET", "path": event_path}]).status,
                    "FAIL",
                )

    def test_inline_citations_and_api_flagged_market_assumption_cases(self):
        evaluate_citations = function("evaluate_citations")
        expected_labels = function("expected_market_assumed_labels")
        evaluate_market = function("evaluate_market_assumed")
        self.assertEqual(evaluate_citations(False, False, False, "", 0, 0).status, "FAIL")
        answer = {"answer": {"evidence": [
            {"flags": ["market_assumed"], "source_market": "KE"},
            {"flags": [], "source_market": "ZA"},
            {"flags": ["market_assumed"], "source_market": "GH"},
        ]}}
        labels = expected_labels(answer)
        self.assertEqual(labels, ["Market assumed: Kenya"])
        self.assertEqual(evaluate_market([], []).kind, "missing_case")
        self.assertEqual(evaluate_market(labels, []).kind, "ui_failure")
        self.assertEqual(evaluate_market(labels, labels).status, "PASS")

    def test_history_follow_id_reads_query_from_fragment(self):
        parse_id = function("ask_follow_id")
        self.assertEqual(parse_id("#/ask?follow=a_123"), "a_123")
        self.assertIsNone(parse_id("#/ask?follow=a_123&other=hidden"))
        self.assertIsNone(parse_id("#/ask"))

    def test_websocket_guard_fails_closed_and_browser_factory_is_injectable(self):
        install = function("install_websocket_guard")

        class SupportsWebSocketRoute:
            def route_web_socket(self, pattern, _handler):
                self.pattern = pattern

        class NoWebSocketRoute:
            pass

        class BrokenWebSocketRoute:
            def route_web_socket(self, _pattern, _handler):
                raise RuntimeError("no route")

        self.assertTrue(install(SupportsWebSocketRoute(), lambda _socket: None))
        self.assertFalse(install(NoWebSocketRoute(), lambda _socket: None))
        self.assertFalse(install(BrokenWebSocketRoute(), lambda _socket: None))
        parameters = inspect.signature(function("_run_browser_checks")).parameters
        self.assertIn("playwright_factory", parameters)
        self.assertEqual(parameters["playwright_factory"].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_expired_auth_stops_without_a_login_fallback(self):
        results = function("_auth_expired_results")()
        self.assertTrue(all(item.status == "FAIL" for item in results.values()))
        self.assertTrue(all(item.kind == "precondition" for item in results.values()))
        self.assertTrue(all("AUTH_EXPIRED" in item.detail for item in results.values()))
        self.assertTrue(all("No login attempt" in item.detail for item in results.values()))
        boundary = function("_auth_expired_results")(boundary_ready=True)
        self.assertEqual(boundary[checker.CHECK_NAMES[-1]].status, "PASS")

    def test_report_redacts_secret_and_uses_exclusive_names(self):
        result_type = getattr(checker, "CheckResult", None)
        self.assertIsNotNone(result_type, "CheckResult is missing")
        result = result_type("Health", "PASS", "version is 84583d04f5f2")
        redact = function("redact_secret")
        self.assertNotIn(SECRET, redact("error: " + SECRET, SECRET))
        now = datetime(2026, 10, 1, 12, 34, tzinfo=SAST)
        with tempfile.TemporaryDirectory() as directory:
            write_report = function("write_report")
            metadata = {
                "brief_date": "2026-10-01",
                "saved_ask_id": "a_123",
                "cards_by_market": {"ZA": 2, "NG": 1, "KE": 0},
            }
            first = write_report(directory, [result], now=now, secret=SECRET, metadata=metadata)
            original = first.read_text(encoding="utf-8")
            self.assertEqual(first.name, "STAGING-CHECK-1234.md")
            self.assertIn("PASS | Health", original)
            self.assertIn("SAST brief date: 2026-10-01", original)
            self.assertIn("Saved Ask id: a_123", original)
            self.assertIn("Cards by market: ZA=2, NG=1, KE=0", original)
            self.assertNotIn(SECRET, original)
            with self.assertRaises(FileExistsError):
                write_report(directory, [result], now=now, secret=SECRET, metadata=metadata)
            self.assertEqual(first.read_text(encoding="utf-8"), original)

    def test_missing_storage_state_writes_auth_expired_without_browser_fallback(self):
        main = function("main")
        with tempfile.TemporaryDirectory() as directory:
            missing_state = Path(directory) / "missing-state.json"
            health_calls = []

            def public_health(url):
                health_calls.append(url)
                body = FakeHealthResponse()
                return checker.HealthObservation(200, False, body.json(), body.text)

            def unexpected_browser_call(*_args):
                self.fail("browser was started without storage state")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(
                    ["--storage-state", str(missing_state), "--output-dir", directory],
                    health_fetcher=public_health,
                    browser_runner=unexpected_browser_call,
                    clock=lambda: datetime(2026, 10, 1, 12, 35, tzinfo=SAST),
                )
            reports = list(Path(directory).glob("STAGING-CHECK-*.md"))
            self.assertEqual(status, 1)
            self.assertEqual(health_calls, [ORIGIN])
            self.assertEqual(len(reports), 1)
            report = reports[0].read_text(encoding="utf-8")
            self.assertIn("PASS | Staging health and version", report)
            self.assertIn("FAIL | Saved answer opens without rerun | precondition | AUTH_EXPIRED", report)
            self.assertIn("human unlock is required", report)
            self.assertNotIn(str(missing_state), report + stdout.getvalue())

    def test_storage_state_path_is_forwarded_without_checker_read(self):
        main = function("main")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "fixture-state.json"
            state_path.touch()
            observed_paths = []

            def public_health(_url):
                body = FakeHealthResponse()
                return checker.HealthObservation(200, False, body.json(), body.text)

            def browser_runner(_url, passed_path, _health, _today):
                observed_paths.append(passed_path)
                return {
                    name: checker.CheckResult(name, "FAIL", "fixture boundary only", "precondition")
                    for name in checker.CHECK_NAMES[1:]
                }

            status = main(
                ["--storage-state", str(state_path), "--output-dir", directory],
                health_fetcher=public_health,
                browser_runner=browser_runner,
                clock=lambda: datetime(2026, 10, 1, 12, 36, tzinfo=SAST),
            )
            self.assertEqual(status, 1)
            self.assertEqual(observed_paths, [state_path])


if __name__ == "__main__":
    unittest.main()
