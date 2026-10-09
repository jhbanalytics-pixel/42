"""Unit tests for core/setup/monitoring.py. No cloud access: a fake Monitoring API records every call, and a
fake session stands in for the REST client's HTTP."""
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from core.collect import chain
from core.setup import monitoring as mon
from core.setup import watchdog as wd

ALBERT = "albert.meintjes@ogilvy.co.za"
SECOND = "someone.else@ogilvy.co.za"
PROJECT = "projects/ogilvy-trends-v2"
ALL = ("collection_missing", "job_failed", "brief_late", "credits_low", "model_spend", "zero_rows", "schema_drift",
       "agent_error_rate", "reconcile", "drift", "seeds_failed", "agent_views_failed", "understand_degraded")
# The two lines core/collect/reconcile.py writes, word for word.
RECONCILE_LINES = {
    "credits_low": "42 ALERT credits_low: 2026-09-30 balance 61234, floor 20000, mean daily spend 1502 over 14 days, "
                   "runway 27.4 days",
    "reconcile": "42 ALERT reconcile: 2026-09-30 difference 312, 4 ledger and 1 vendor rows unmatched",
}


class FakeMonitoring:
    def __init__(self, channels=(), policies=()):
        self.channels = [dict(c) for c in channels]
        self.policies = [dict(p) for p in policies]
        self.calls = []

    def list_channels(self):
        self.calls.append(("list_channels",))
        return [dict(c) for c in self.channels]

    def list_policies(self):
        self.calls.append(("list_policies",))
        return [dict(p) for p in self.policies]

    def create_channel(self, body):
        self.calls.append(("create_channel", body))
        made = {**body, "name": f"{PROJECT}/notificationChannels/{100 + len(self.channels)}"}
        self.channels.append(made)
        return made

    def create_policy(self, body):
        self.calls.append(("create_policy", body))
        made = {**body, "name": f"{PROJECT}/alertPolicies/{500 + len(self.policies)}"}
        self.policies.append(made)
        return made

    def creates(self, kind):
        return [c[1] for c in self.calls if c[0] == f"create_{kind}"]


class NoCalls:
    def __getattr__(self, name):
        raise AssertionError(f"dry run called {name}")


def existing_channel(email, n):
    return {"name": f"{PROJECT}/notificationChannels/{n}", "type": "email", "labels": {"email_address": email}}


def existing_policy(name, n):
    return {"name": f"{PROJECT}/alertPolicies/{n}", "displayName": mon.display_name(name),
            "userLabels": {"f42_alert": name}}


def test_the_eight_alerts_of_setup_md_the_nightly_reconcile_and_the_weekly_drift():
    assert mon.ALERTS == ALL
    assert set(mon.LOG_ALERTS) == set(wd.ALERTS) | {"credits_low", "reconcile", "drift"}
    assert set(mon.LOG_ALERTS) | {"job_failed"} == set(ALL)


def test_dry_run_prints_every_channel_and_policy_and_calls_nothing(capsys):
    assert mon.main(["--second-email", SECOND], monitoring=NoCalls()) == 0
    out = capsys.readouterr().out
    assert ALBERT in out and SECOND in out
    for name in ALL:
        assert mon.display_name(name) in out
    assert "--apply" in out
    assert "300s" in out


def test_dry_run_without_a_second_person_says_so(capsys):
    assert mon.main([], monitoring=NoCalls()) == 0
    out = capsys.readouterr().out
    assert ALBERT in out and "--second-email" in out


def test_a_bad_second_email_is_refused():
    with pytest.raises(SystemExit):
        mon.main(["--second-email", "not-an-address"], monitoring=NoCalls())


@pytest.mark.parametrize("name", ALL)
def test_each_policy_is_log_based_with_auto_close_and_a_five_minute_rate_limit(name):
    channels = [f"{PROJECT}/notificationChannels/1", f"{PROJECT}/notificationChannels/2"]
    body = mon.policy_body(name, channels)
    assert body["displayName"] == mon.display_name(name)
    assert body["userLabels"] == {"managed_by": "f42-monitoring", "f42_alert": name}
    assert body["notificationChannels"] == channels
    assert body["enabled"] is True
    [condition] = body["conditions"]
    log_filter = condition["conditionMatchedLog"]["filter"]
    assert 'resource.type="cloud_run_job"' in log_filter
    strategy = body["alertStrategy"]
    assert strategy["notificationRateLimit"] == {"period": "300s"}
    assert int(strategy["autoClose"].rstrip("s")) >= 1800
    assert body["documentation"]["content"]


def test_job_failed_matches_error_lines_of_any_f42_job_in_cloud_run_system_logs():
    log_filter = mon.policy_body("job_failed", [])["conditions"][0]["conditionMatchedLog"]["filter"]
    assert 'resource.labels.job_name=~"^f42-"' in log_filter
    assert 'log_id("run.googleapis.com/varlog/system")' in log_filter
    assert "severity>=ERROR" in log_filter
    assert "42 ALERT" not in log_filter


def quoted(log_filter, field):
    return re.findall(rf'{re.escape(field)}:"([^"]+)"', log_filter)


def _field(entry, path):
    for part in path.split("."):
        entry = entry.get(part) if isinstance(entry, dict) else None
    return entry


SEVERITIES = ["DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"]


def _term(term, entry):
    term = term.strip()
    if term.startswith("(") and term.endswith(")"):
        return any(_term(t, entry) for t in term[1:-1].split(" OR "))
    if m := re.fullmatch(r'log_id\("([^"]+)"\)', term):
        return entry["logName"].endswith("/logs/" + m.group(1).replace("/", "%2F"))
    if m := re.fullmatch(r"severity>=(\w+)", term):
        return SEVERITIES.index(entry["severity"]) >= SEVERITIES.index(m.group(1))
    if m := re.fullmatch(r'([\w.]+)=~"([^"]+)"', term):
        return re.search(m.group(2), _field(entry, m.group(1)) or "") is not None
    if m := re.fullmatch(r'([\w.]+)="([^"]+)"', term):
        return _field(entry, m.group(1)) == m.group(2)
    if m := re.fullmatch(r'([\w.]+):"([^"]+)"', term):
        return m.group(2) in (_field(entry, m.group(1)) or "")
    raise AssertionError(f"the test's filter reader does not know {term!r}")


def matches(log_filter, entry):
    """Enough of the Cloud Logging filter language to read the filters monitoring.py writes."""
    return all(_term(t, entry) for t in log_filter.split(" AND "))


def stdout_entry(line, as_json=True, job="f42-reconcile"):
    entry = {"resource": {"type": "cloud_run_job", "labels": {"job_name": job}}, "severity": "ERROR",
             "logName": "projects/ogilvy-trends-v2/logs/run.googleapis.com%2Fstdout"}
    if as_json:
        entry["jsonPayload"] = {"message": line}
    else:
        entry["severity"], entry["textPayload"] = "DEFAULT", line
    return entry


def filters():
    return {n: mon.policy_body(n, [])["conditions"][0]["conditionMatchedLog"]["filter"] for n in ALL}


@pytest.mark.parametrize("as_json", [True, False])
@pytest.mark.parametrize("name", sorted(RECONCILE_LINES))
def test_each_exact_reconcile_line_matches_its_own_policy_and_no_other(name, as_json):
    entry = stdout_entry(RECONCILE_LINES[name], as_json)
    assert [n for n, f in filters().items() if matches(f, entry)] == [name]


def drift_line():
    """The exact ERROR line the weekly drift job logs, built by core/collect/drift.py and its JSON formatter."""
    from core.collect import drift
    from core.collect.reconcile import JsonFormatter

    rows = [{"side": "expansion", "dimension": "topic", "market": "ZA", "item": "amapiano", "weight": 90.0},
            {"side": "expansion", "dimension": "topic", "market": "ZA", "item": "braai", "weight": 10.0}]
    line = drift.alert_line(drift.report(rows, markets=("ZA",)), date(2026, 9, 27))
    assert line.startswith("42 ALERT drift: 2026-09-21 to 2026-09-27 ZA topic HHI 0.82")
    record = logging.LogRecord(drift.log.name, logging.ERROR, __file__, 0, line, None, None)
    return json.loads(JsonFormatter().format(record))


@pytest.mark.parametrize("as_json", [True, False])
def test_the_exact_drift_line_matches_the_drift_policy_and_no_other(as_json):
    written = drift_line()
    assert written["severity"] == "ERROR"
    entry = stdout_entry(written["message"], as_json, job="f42-drift")
    assert [n for n, f in filters().items() if matches(f, entry)] == ["drift"]


def test_the_drift_filter_is_exactly_the_shared_alert_shape_and_skips_schema_drift_and_info_lines():
    assert mon.policy_body("drift", [])["conditions"][0]["conditionMatchedLog"]["filter"] == (
        'resource.type="cloud_run_job" AND (jsonPayload.message:"42 ALERT drift:" '
        'OR textPayload:"42 ALERT drift:")')
    for line in ("42 ALERT schema_drift: tiktok/search gained key extra",
                 "drift ZA topic expansion 0.82 feeds none limit 0.25"):
        assert not matches(filters()["drift"], stdout_entry(line, job="f42-drift")), line
    assert mon.DOCS["drift"]


def test_the_old_reconcile_line_shape_would_match_nothing():
    entry = stdout_entry("42 ALERT credits_low 2026-09-30: balance 61234")
    assert [n for n, f in filters().items() if matches(f, entry)] == []


def test_a_failed_execution_in_the_system_log_matches_only_job_failed():
    entry = {"resource": {"type": "cloud_run_job", "labels": {"job_name": "f42-collect"}}, "severity": "ERROR",
             "logName": "projects/ogilvy-trends-v2/logs/run.googleapis.com%2Fvarlog%2Fsystem",
             "textPayload": "Execution f42-collect-abcde has failed to complete, 0/1 tasks were a success."}
    assert [n for n, f in filters().items() if matches(f, entry)] == ["job_failed"]
    entry["resource"]["labels"]["job_name"] = "other-job"
    assert [n for n, f in filters().items() if matches(f, entry)] == []


@pytest.mark.parametrize("name", ALL[:1] + ALL[2:])
def test_each_alert_policy_matches_its_own_line_and_no_other(name):
    log_filter = mon.policy_body(name, [])["conditions"][0]["conditionMatchedLog"]["filter"]
    [needle] = quoted(log_filter, "jsonPayload.message")
    assert quoted(log_filter, "textPayload") == [needle]
    assert needle == f"42 ALERT {name}:"
    for other in ALL:
        if other != name:
            assert needle not in f"42 ALERT {other}: some reason"


class QuietLogOnlyRules:
    """The store answers of a healthy day for the watchdog's log-only rules, so a fake store need only set the
    answers its own alert reads."""

    brief_state = staticmethod(lambda d: {m: {"status": "published", "cards": 3, "held": 0} for m in ("ZA", "NG", "KE")})
    understand_latest = staticmethod(lambda d: None)
    stage_latest = staticmethod(lambda d: [])
    watchdog_last = staticmethod(lambda d: datetime(2099, 1, 1, tzinfo=timezone.utc))


def test_a_forced_zero_rows_run_writes_the_line_the_zero_rows_policy_matches(capsys):
    class ZeroRows(QuietLogOnlyRules):
        collect_ok = staticmethod(lambda d: True)
        observations = staticmethod(lambda d: {"ZA": 0, "NG": 3, "KE": 2})
        published_markets = staticmethod(lambda d: {"ZA", "NG", "KE"})
        model_usd = staticmethod(lambda d: 0.0)
        route_keys = staticmethod(lambda d: [])
        ask_runs = staticmethod(lambda d: (0, 0))
        seeds_latest = staticmethod(lambda d: None)
        detect_latest = staticmethod(lambda d: None)
        fired_today = staticmethod(lambda d: set())

    now = datetime(2026, 10, 1, 4, 30, tzinfo=timezone(timedelta(hours=2)))
    assert wd.main(now=now, store=ZeroRows(), runs=chain.MemoryRunsStore()) == 0
    [line] = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    entry = stdout_entry(line["message"], job="f42-watchdog")
    entry["severity"] = line["severity"]
    assert [n for n, f in filters().items() if matches(f, entry)] == ["zero_rows"]


def test_the_seeds_failed_filter_is_the_shared_alert_shape_and_matches_only_the_watchdog_line(capsys):
    assert mon.policy_body("seeds_failed", [])["conditions"][0]["conditionMatchedLog"]["filter"] == (
        'resource.type="cloud_run_job" AND (jsonPayload.message:"42 ALERT seeds_failed:" '
        'OR textPayload:"42 ALERT seeds_failed:")')
    assert mon.DOCS["seeds_failed"]

    class SeedsFailed(QuietLogOnlyRules):
        collect_ok = staticmethod(lambda d: True)
        observations = staticmethod(lambda d: {"ZA": 3, "NG": 3, "KE": 2})
        published_markets = staticmethod(lambda d: {"ZA", "NG", "KE"})
        model_usd = staticmethod(lambda d: 0.0)
        route_keys = staticmethod(lambda d: [])
        ask_runs = staticmethod(lambda d: (0, 0))
        seeds_latest = staticmethod(lambda d: {"run_id": "seeds-20261001-abc", "status": "failed"})
        detect_latest = staticmethod(lambda d: None)
        fired_today = staticmethod(lambda d: set())

    now = datetime(2026, 10, 1, 3, 0, tzinfo=timezone(timedelta(hours=2)))
    assert wd.main(now=now, store=SeedsFailed(), runs=chain.MemoryRunsStore()) == 0
    [line] = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    for as_json in (True, False):
        entry = stdout_entry(line["message"], as_json, job="f42-watchdog")
        entry["severity"] = line["severity"]
        assert [n for n, f in filters().items() if matches(f, entry)] == ["seeds_failed"]


def test_the_agent_views_failed_filter_is_the_shared_alert_shape_and_matches_only_the_watchdog_line(capsys):
    assert mon.policy_body("agent_views_failed", [])["conditions"][0]["conditionMatchedLog"]["filter"] == (
        'resource.type="cloud_run_job" AND (jsonPayload.message:"42 ALERT agent_views_failed:" '
        'OR textPayload:"42 ALERT agent_views_failed:")')
    assert mon.DOCS["agent_views_failed"]

    class ViewsFailed(QuietLogOnlyRules):
        collect_ok = staticmethod(lambda d: True)
        observations = staticmethod(lambda d: {"ZA": 3, "NG": 3, "KE": 2})
        published_markets = staticmethod(lambda d: {"ZA", "NG", "KE"})
        model_usd = staticmethod(lambda d: 0.0)
        route_keys = staticmethod(lambda d: [])
        ask_runs = staticmethod(lambda d: (0, 0))
        seeds_latest = staticmethod(lambda d: None)
        detect_latest = staticmethod(lambda d: {"run_id": "detect-20261001-abc", "agent_views_status": "failed",
                                                "agent_views_error": "NotFound: 404 v_sensitive_items"})
        fired_today = staticmethod(lambda d: set())

    now = datetime(2026, 10, 1, 3, 0, tzinfo=timezone(timedelta(hours=2)))
    assert wd.main(now=now, store=ViewsFailed(), runs=chain.MemoryRunsStore()) == 0
    [line] = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    for as_json in (True, False):
        entry = stdout_entry(line["message"], as_json, job="f42-watchdog")
        entry["severity"] = line["severity"]
        assert [n for n, f in filters().items() if matches(f, entry)] == ["agent_views_failed"]


def test_the_understand_degraded_filter_is_the_shared_alert_shape_and_matches_only_the_watchdog_line(capsys):
    assert mon.policy_body("understand_degraded", [])["conditions"][0]["conditionMatchedLog"]["filter"] == (
        'resource.type="cloud_run_job" AND (jsonPayload.message:"42 ALERT understand_degraded:" '
        'OR textPayload:"42 ALERT understand_degraded:")')
    assert "partial" in mon.DOCS["understand_degraded"] and "understand" in mon.DOCS["understand_degraded"]

    class Partial(QuietLogOnlyRules):
        collect_ok = staticmethod(lambda d: True)
        observations = staticmethod(lambda d: {"ZA": 3, "NG": 3, "KE": 2})
        published_markets = staticmethod(lambda d: {"ZA", "NG", "KE"})
        model_usd = staticmethod(lambda d: 0.0)
        route_keys = staticmethod(lambda d: [])
        ask_runs = staticmethod(lambda d: (0, 0))
        seeds_latest = staticmethod(lambda d: None)
        detect_latest = staticmethod(lambda d: None)
        understand_latest = staticmethod(lambda d: {
            "run_id": "understand-20261001-abc", "status": "ok", "degraded": [], "embed_error": None, "partial": True,
            "partial_reason": "cluster_stack_failed", "partial_error": "ImportError: numba"})
        fired_today = staticmethod(lambda d: set())

    now = datetime(2026, 10, 1, 3, 0, tzinfo=timezone(timedelta(hours=2)))
    assert wd.main(now=now, store=Partial(), runs=chain.MemoryRunsStore()) == 0
    [line] = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    for as_json in (True, False):
        entry = stdout_entry(line["message"], as_json, job="f42-watchdog")
        entry["severity"] = line["severity"]
        assert [n for n, f in filters().items() if matches(f, entry)] == ["understand_degraded"]


def test_apply_on_an_empty_project_creates_both_channels_then_all_eight_policies(capsys):
    fake = FakeMonitoring()
    assert mon.main(["--apply", "--second-email", SECOND], monitoring=fake) == 0
    channels = fake.creates("channel")
    assert [c["labels"]["email_address"] for c in channels] == [ALBERT, SECOND]
    assert all(c["type"] == "email" for c in channels)
    policies = fake.creates("policy")
    assert [p["userLabels"]["f42_alert"] for p in policies] == list(ALL)
    names = [c["name"] for c in fake.channels]
    assert all(p["notificationChannels"] == names for p in policies)
    first_policy = next(i for i, c in enumerate(fake.calls) if c[0] == "create_policy")
    assert all(c[0] != "create_channel" for c in fake.calls[first_policy:])


def test_apply_creates_only_what_is_missing_and_leaves_the_rest_alone(capsys):
    fake = FakeMonitoring(
        channels=[existing_channel("Albert.Meintjes@Ogilvy.co.za", 7)],
        policies=[existing_policy("zero_rows", 1), existing_policy("brief_late", 2),
                  {"name": f"{PROJECT}/alertPolicies/3", "displayName": mon.display_name("job_failed")}])
    before = [dict(p) for p in fake.policies]
    assert mon.main(["--apply", "--second-email", SECOND], monitoring=fake) == 0
    assert [c["labels"]["email_address"] for c in fake.creates("channel")] == [SECOND]
    made = [p["userLabels"]["f42_alert"] for p in fake.creates("policy")]
    assert made == [n for n in ALL if n not in ("zero_rows", "brief_late", "job_failed")]
    assert fake.policies[:3] == before
    assert all(p["notificationChannels"] == [f"{PROJECT}/notificationChannels/7", f"{PROJECT}/notificationChannels/101"]
               for p in fake.creates("policy"))
    assert {c[0] for c in fake.calls} == {"list_channels", "list_policies", "create_channel", "create_policy"}
    out = capsys.readouterr().out
    assert "left unchanged" in out


def test_a_second_apply_creates_nothing():
    fake = FakeMonitoring()
    mon.main(["--apply", "--second-email", SECOND], monitoring=fake)
    fake.calls.clear()
    assert mon.main(["--apply", "--second-email", SECOND], monitoring=fake) == 0
    assert fake.creates("channel") == [] and fake.creates("policy") == []


def test_the_second_email_equal_to_alberts_makes_one_channel():
    fake = FakeMonitoring()
    mon.main(["--apply", "--second-email", ALBERT.upper()], monitoring=fake)
    assert len(fake.creates("channel")) == 1


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, pages=None, status=200):
        self.pages = pages or {}
        self.status = status
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, dict(params or {})))
        return FakeResponse(self.status, self.pages[(params or {}).get("pageToken", "")])

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return FakeResponse(self.status, {**json, "name": "made"})


API = "https://monitoring.googleapis.com/v3/projects/ogilvy-trends-v2"


def test_the_rest_client_pages_through_lists_and_posts_creates():
    session = FakeSession({"": {"notificationChannels": [{"name": "a"}], "nextPageToken": "p2"},
                           "p2": {"notificationChannels": [{"name": "b"}]}})
    client = mon.Monitoring(session=session)
    assert [c["name"] for c in client.list_channels()] == ["a", "b"]
    assert [c[1] for c in session.calls] == [f"{API}/notificationChannels"] * 2
    session = FakeSession({"": {}})
    client = mon.Monitoring(session=session)
    assert client.list_policies() == []
    assert client.create_policy({"displayName": "x"})["name"] == "made"
    assert client.create_channel({"type": "email"})["name"] == "made"
    assert [(c[0], c[1]) for c in session.calls[1:]] == [("POST", f"{API}/alertPolicies"),
                                                          ("POST", f"{API}/notificationChannels")]


def test_the_rest_client_can_only_list_and_create():
    public = {n for n in dir(mon.Monitoring) if not n.startswith("_")}
    assert public == {"list_channels", "list_policies", "create_channel", "create_policy"}


def test_the_rest_client_raises_on_a_refusal():
    client = mon.Monitoring(session=FakeSession({"": {}}, status=403))
    with pytest.raises(RuntimeError, match="403"):
        client.list_policies()
