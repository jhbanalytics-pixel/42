import copy
import io
import json
import re
import subprocess
from pathlib import Path

import pytest

from ops.monitoring import apply_monitoring as monitoring

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "ops" / "deploy" / "resource_manifest.json"
READS = {"list_channels", "list_policies", "get_log_metric"}
FOREIGN_CHANNEL = "projects/ogilvy-trends-v2/notificationChannels/900"
FOREIGN_POLICY = "projects/ogilvy-trends-v2/alertPolicies/901"


class FakeClient:
    """An in memory project that answers the way the REST surfaces do.

    Responses carry server fields the definitions never set and leave out
    fields at their proto default, so the comparison is exercised as it would
    be against the native project.
    """

    def __init__(self):
        self.calls = []
        self.channels = {
            FOREIGN_CHANNEL: {
                "name": FOREIGN_CHANNEL,
                "type": "email",
                "labels": {"email_address": "someone@example.org"},
                "userLabels": {"team": "other"},
            }
        }
        self.policies = {
            FOREIGN_POLICY: {
                "name": FOREIGN_POLICY,
                "displayName": "42 daily job execution failed",
                "userLabels": {"monitoring_key": "daily-job-failure"},
            }
        }
        self.metrics = {"foreign_metric": {"name": "foreign_metric", "filter": "x"}}
        self.foreign = copy.deepcopy(
            (self.channels, self.policies, self.metrics["foreign_metric"])
        )
        self._next = 1000
        self.masks = []

    def _record(self, call, target=None):
        self.calls.append((call, target))

    def _served(self, body, name):
        served = copy.deepcopy(body)
        served["name"] = name
        served["creationRecord"] = {"mutateTime": "2026-09-25T00:00:00Z"}
        for index, condition in enumerate(served.get("conditions", [])):
            condition["name"] = f"{name}/conditions/{index}"
            threshold = condition.get("conditionThreshold")
            if threshold is not None:
                for field in ("thresholdValue", "duration"):
                    if threshold.get(field) in (0, "0s"):
                        del threshold[field]
        return served

    def _id(self):
        self._next += 1
        return str(self._next)

    def list_channels(self):
        self._record("list_channels")
        return copy.deepcopy(list(self.channels.values()))

    def create_channel(self, body):
        self._record("create_channel")
        name = f"projects/ogilvy-trends-v2/notificationChannels/{self._id()}"
        self.channels[name] = self._served(body, name)
        self.channels[name]["verificationStatus"] = "VERIFICATION_STATUS_UNSPECIFIED"
        return copy.deepcopy(self.channels[name])

    def patch_channel(self, name, body, mask):
        self._record("patch_channel", name)
        self.masks.append(("patch_channel", mask, sorted(body)))
        assert set(mask.split(",")) == set(body)
        self.channels[name].update(copy.deepcopy(body))
        return copy.deepcopy(self.channels[name])

    def list_policies(self):
        self._record("list_policies")
        return copy.deepcopy(list(self.policies.values()))

    def create_policy(self, body):
        self._record("create_policy")
        name = f"projects/ogilvy-trends-v2/alertPolicies/{self._id()}"
        self.policies[name] = self._served(body, name)
        return copy.deepcopy(self.policies[name])

    def patch_policy(self, name, body, mask):
        self._record("patch_policy", name)
        self.masks.append(("patch_policy", mask, sorted(body)))
        assert set(mask.split(",")) == set(body)
        merged = copy.deepcopy(self.policies[name])
        merged.update(copy.deepcopy(body))
        self.policies[name] = self._served(merged, name)
        return copy.deepcopy(self.policies[name])

    def get_log_metric(self, metric):
        self._record("get_log_metric", metric)
        found = self.metrics.get(metric)
        return copy.deepcopy(found) if found is not None else None

    def create_log_metric(self, body):
        self._record("create_log_metric", body["name"])
        served = copy.deepcopy(body)
        served["metricDescriptor"].update(
            name=f"projects/ogilvy-trends-v2/metricDescriptors/{body['name']}",
            type=f"logging.googleapis.com/user/{body['name']}",
        )
        served["createTime"] = "2026-09-25T00:00:00Z"
        self.metrics[body["name"]] = served
        return copy.deepcopy(served)

    def update_log_metric(self, metric, body):
        self._record("update_log_metric", metric)
        self.metrics[metric].update(copy.deepcopy(body))
        return copy.deepcopy(self.metrics[metric])

    def mutations(self):
        return [call for call in self.calls if call[0] not in READS]


@pytest.fixture
def definitions():
    return monitoring.load_definitions()


def _applied(definitions, client):
    return monitoring.apply(
        definitions,
        client,
        expected_sha256=monitoring.plan_sha256(definitions),
    )


def test_offline_dry_run_makes_no_api_call():
    built = []
    out = io.StringIO()

    def factory():
        built.append(True)
        return FakeClient()

    assert monitoring.main(["--offline"], client_factory=factory, out=out) == 0
    assert built == []
    result = json.loads(out.getvalue())
    assert result["mode"] == "dry-run-offline"
    assert {row["action"] for row in result["resources"]} == {"create-or-update-unread"}


def test_dry_run_only_reads_and_prints_plan_and_digest(definitions):
    client = FakeClient()
    out = io.StringIO()
    assert monitoring.main([], client_factory=lambda: client, out=out) == 0
    assert client.mutations() == []
    result = json.loads(out.getvalue())
    assert result["plan_sha256"] == monitoring.plan_sha256(definitions)
    assert len(result["resources"]) == 1 + 7 + 7
    assert {row["action"] for row in result["resources"]} == {"create"}


def test_plan_digest_is_sha256_of_canonical_definitions(definitions):
    raw = json.loads(monitoring.DEFINITIONS_PATH.read_text(encoding="utf-8"))
    import hashlib

    expected = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert monitoring.plan_sha256(definitions) == expected


def test_apply_refuses_without_the_printed_digest(definitions):
    client = FakeClient()
    with pytest.raises(ValueError, match="plan_sha256_mismatch"):
        monitoring.apply(definitions, client, expected_sha256="0" * 64)
    assert client.mutations() == []
    out = io.StringIO()
    assert monitoring.main(["--apply"], client_factory=lambda: client, out=out) == 2
    assert client.mutations() == []


def test_apply_is_idempotent(definitions):
    client = FakeClient()
    first = _applied(definitions, client)
    assert {row["action"] for row in first["resources"]} == {"create"}
    before = len(client.mutations())
    second = _applied(definitions, client)
    assert {row["action"] for row in second["resources"]} == {"no-op"}
    assert len(client.mutations()) == before
    plan = monitoring.dry_run(definitions, client)
    assert {row["action"] for row in plan["resources"]} == {"no-op"}


def test_readback_passes_after_apply(definitions):
    client = FakeClient()
    _applied(definitions, client)
    out = io.StringIO()
    code = monitoring.main(["--readback"], client_factory=lambda: client, out=out)
    result = json.loads(out.getvalue())
    assert code == 0, result
    assert result["status"] == "pass"
    assert result["channels"] == {
        "owned_count": 1,
        "recipients": ["jhb.analytics@gmail.com"],
        "status": "pass",
    }
    assert all(row["status"] == "pass" for row in result["resources"])


def test_readback_detects_drifted_fields(definitions):
    client = FakeClient()
    _applied(definitions, client)
    policy = next(
        item
        for item in client.policies.values()
        if item.get("userLabels", {}).get("monitoring_key")
        == "daily-success-absent-48h"
    )
    policy["severity"] = "WARNING"
    client.metrics["intelligence_42_daily_success"]["filter"] += " AND x"
    out = io.StringIO()
    code = monitoring.main(["--readback"], client_factory=lambda: client, out=out)
    result = json.loads(out.getvalue())
    assert code == 1
    assert result["status"] == "fail"
    failed = {
        row["key"]: row["differences"]
        for row in result["resources"]
        if row["status"] == "fail"
    }
    assert failed == {
        "intelligence_42_daily_success": ["filter"],
        "daily-success-absent-48h": ["severity"],
    }
    plan = monitoring.dry_run(definitions, client)
    updates = {r["key"] for r in plan["resources"] if r["action"] == "update"}
    assert updates == set(failed)
    repaired = _applied(definitions, client)
    assert {r["key"] for r in repaired["resources"] if r["action"] == "update"} == set(
        failed
    )
    assert monitoring.readback(definitions, client)["status"] == "pass"


def test_readback_fails_when_an_owned_resource_is_missing(definitions):
    client = FakeClient()
    _applied(definitions, client)
    del client.metrics["intelligence_42_vendor_failure"]
    result = monitoring.readback(definitions, client)
    assert result["status"] == "fail"
    assert [r["key"] for r in result["resources"] if r["status"] == "fail"] == [
        "intelligence_42_vendor_failure"
    ]


def test_channel_list_is_exactly_the_owner_email(definitions):
    channels = definitions["notification_channels"]
    assert [c["body"]["labels"] for c in channels] == [
        {"email_address": "jhb.analytics@gmail.com"}
    ]
    assert [c["body"]["type"] for c in channels] == ["email"]
    text = monitoring.DEFINITIONS_PATH.read_text(encoding="utf-8")
    assert set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)) == {
        "jhb.analytics@gmail.com"
    }
    client = FakeClient()
    _applied(definitions, client)
    owned = [
        name
        for name, item in client.channels.items()
        if item.get("userLabels", {}).get("managed_by") == monitoring.OWNER_LABEL
    ]
    assert len(owned) == 1
    for item in client.policies.values():
        if item["name"] != FOREIGN_POLICY:
            assert item["notificationChannels"] == owned


def test_a_second_recipient_is_refused(definitions):
    extra = copy.deepcopy(definitions)
    extra["notification_channels"][0]["body"]["labels"]["email_address"] = (
        "other@example.org"
    )
    with pytest.raises(ValueError, match="definition_channels_invalid"):
        monitoring.validate_definitions(extra)
    extra = copy.deepcopy(definitions)
    extra["notification_channels"].append(
        copy.deepcopy(extra["notification_channels"][0])
    )
    with pytest.raises(ValueError, match="definition_channels_invalid"):
        monitoring.validate_definitions(extra)


def test_only_owned_resources_are_touched(definitions):
    client = FakeClient()
    _applied(definitions, client)
    _applied(definitions, client)
    monitoring.readback(definitions, client)
    touched = [target for _call, target in client.mutations()]
    assert FOREIGN_CHANNEL not in touched
    assert FOREIGN_POLICY not in touched
    assert "foreign_metric" not in touched
    assert all(
        target is None
        or "intelligence_42_" in target
        or target.startswith("projects/ogilvy-trends-v2/")
        for target in touched
    )
    reads = {target for call, target in client.calls if call == "get_log_metric"}
    assert reads == {entry["key"] for entry in definitions["log_metrics"]}
    assert (client.channels[FOREIGN_CHANNEL], client.policies[FOREIGN_POLICY]) == (
        client.foreign[0][FOREIGN_CHANNEL],
        client.foreign[1][FOREIGN_POLICY],
    )
    assert client.metrics["foreign_metric"] == client.foreign[2]


def test_duplicate_owned_policy_refuses_before_any_write(definitions):
    client = FakeClient()
    _applied(definitions, client)
    for name in ("projects/ogilvy-trends-v2/alertPolicies/7",):
        client.policies[name] = {
            "name": name,
            "userLabels": {
                "managed_by": monitoring.OWNER_LABEL,
                "monitoring_key": "vendor-failure",
            },
        }
    before = len(client.mutations())
    with pytest.raises(ValueError, match="owned_duplicate:vendor-failure"):
        _applied(definitions, client)
    assert len(client.mutations()) == before


def test_native_client_has_no_delete_call():
    names = [name for name in dir(monitoring.NativeClient) if not name.startswith("__")]
    assert not [name for name in names if "delete" in name.lower()]
    source = Path(monitoring.__file__).read_text(encoding="utf-8")
    assert '"DELETE"' not in source
    assert "subprocess" not in source


# Every filter literal is one the code emits


def _source_line(source):
    path = ROOT / source["path"]
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    line = lines[source["line"] - 1] if len(lines) >= source["line"] else ""
    if source["literal"] in line or "ref" not in source:
        return line
    shown = subprocess.run(
        ["git", "show", f"{source['ref']}:{source['path']}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert shown.returncode == 0, (
        f"{source['path']} does not carry the alarm line yet and commit "
        f"{source['ref']} is not available"
    )
    return shown.stdout.splitlines()[source["line"] - 1]


def _manifest_names():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {row["name"] for row in manifest["resources"]}


def test_every_cited_source_line_carries_its_literal(definitions):
    for metric in definitions["log_metrics"]:
        for source in metric["sources"]:
            assert source["literal"] in _source_line(source), (metric["key"], source)


def test_every_filter_literal_is_emitted_by_the_cited_code(definitions):
    names = _manifest_names()
    location = "//run.googleapis.com/projects/ogilvy-trends-v2/locations/us-central1"
    log_names = {
        f"projects/ogilvy-trends-v2/logs/run.googleapis.com%2F{stream}"
        for stream in ("stdout", "stderr")
    }
    for metric in definitions["log_metrics"]:
        text = metric["body"]["filter"]
        literals = " ".join(source["literal"] for source in metric["sources"])
        for field in re.findall(r"jsonPayload\.(\w+)", text):
            assert f'"{field}"' in literals, (metric["key"], field)
        clauses = re.findall(
            r'([\w.]+)\s*[=:]\s*\(?((?:"[^"]*"(?:\s+OR\s+)?)+)\)?', text
        )
        covered = sum(len(re.findall(r'"[^"]*"', values)) for _f, values in clauses)
        assert covered == len(re.findall(r'"[^"]*"', text)), metric["key"]
        for field, values in clauses:
            for value in re.findall(r'"([^"]*)"', values):
                if field == "resource.type":
                    assert value in {"cloud_run_job", "cloud_run_revision"}
                elif field == "resource.labels.job_name":
                    assert f"{location}/jobs/{value}" in names, value
                elif field == "resource.labels.service_name":
                    assert f"{location}/services/{value}" in names, value
                elif field == "logName":
                    assert value in log_names, value
                else:
                    assert value in literals, (metric["key"], field, value)


def test_policies_reference_defined_metrics(definitions):
    defined = {entry["body"]["name"] for entry in definitions["log_metrics"]}
    used = set()
    for policy in definitions["alert_policies"]:
        for condition in policy["body"]["conditions"]:
            text = json.dumps(condition)
            used |= set(re.findall(r"logging\.googleapis\.com/user/(\w+)", text))
            used |= set(re.findall(r"logging_googleapis_com:user_(\w+)", text))
    assert used == defined


def test_absence_policies_hold_to_thirty_and_forty_eight_hours(definitions):
    held = {}
    for policy in definitions["alert_policies"]:
        for condition in policy["body"]["conditions"]:
            promql = condition.get("conditionPrometheusQueryLanguage")
            if promql is None:
                continue
            assert promql["query"].startswith("absent_over_time(")
            assert promql["query"].endswith("[24h])")
            hours = 24 + int(promql["duration"].rstrip("s")) // 3600
            held[policy["key"]] = (hours, policy["body"]["severity"])
    assert held == {
        "daily-success-absent-30h": (30, "WARNING"),
        "daily-success-absent-48h": (48, "CRITICAL"),
        "price-renewal-success-absent-30h": (30, "ERROR"),
    }


def test_absence_policies_ship_disabled_until_activation(definitions):
    enabled = {p["key"]: p["body"]["enabled"] for p in definitions["alert_policies"]}
    assert {key for key, value in enabled.items() if value is False} == {
        "daily-success-absent-30h",
        "daily-success-absent-48h",
        "price-renewal-success-absent-30h",
    }
    assert sum(value is True for value in enabled.values()) == 4
    client = FakeClient()
    _applied(definitions, client)
    policy = next(
        item
        for item in client.policies.values()
        if item.get("userLabels", {}).get("monitoring_key")
        == "daily-success-absent-30h"
    )
    assert policy["enabled"] is False
    policy["enabled"] = True
    result = monitoring.readback(definitions, client)
    assert result["status"] == "fail"
    assert [
        (row["key"], row["differences"])
        for row in result["resources"]
        if row["status"] == "fail"
    ] == [("daily-success-absent-30h", ["enabled"])]


def _owned_policy(client, key):
    return next(
        item
        for item in client.policies.values()
        if item.get("userLabels", {}).get("monitoring_key") == key
    )


def test_policy_patch_mask_names_only_the_drifted_field(definitions):
    client = FakeClient()
    _applied(definitions, client)
    _owned_policy(client, "vendor-failure")["severity"] = "CRITICAL"
    result = _applied(definitions, client)
    assert [r["key"] for r in result["resources"] if r["action"] == "update"] == [
        "vendor-failure"
    ]
    assert client.masks == [("patch_policy", "severity", ["severity"])]
    assert monitoring.readback(definitions, client)["status"] == "pass"


def test_channel_patch_never_resends_type_or_email(definitions):
    client = FakeClient()
    _applied(definitions, client)
    channel = next(
        item
        for item in client.channels.values()
        if item.get("userLabels", {}).get("managed_by") == monitoring.OWNER_LABEL
    )
    channel["displayName"] = "renamed"
    _applied(definitions, client)
    assert client.masks == [("patch_channel", "displayName", ["displayName"])]
    for field, value in (
        ("type", "sms"),
        ("labels", {"email_address": "other@example.org"}),
    ):
        drifted = copy.deepcopy(channel)
        drifted[field] = value
        client.channels[channel["name"]] = drifted
        before = len(client.mutations())
        with pytest.raises(ValueError, match="channel_identity_drift:owner_email"):
            _applied(definitions, client)
        assert len(client.mutations()) == before
    assert [mask for mask in client.masks if mask[0] == "patch_channel"] == [
        ("patch_channel", "displayName", ["displayName"])
    ]


@pytest.mark.parametrize(
    "error",
    [
        KeyError("body"),
        OSError("reset"),
        type("DefaultCredentialsError", (Exception,), {})(),
        type("MalformedError", (ValueError,), {})("token ya29.SECRET"),
    ],
)
def test_unexpected_errors_exit_2_naming_the_class_only(error):
    def factory():
        raise error

    for argv in ([], ["--readback"], ["--apply", "--plan-sha256", "0" * 64]):
        out = io.StringIO()
        assert monitoring.main(argv, client_factory=factory, out=out) == 2
        lines = out.getvalue().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {
            "error": type(error).__name__,
            "status": "failed",
        }
        assert "Traceback" not in out.getvalue()
        assert "SECRET" not in out.getvalue()


def test_a_definitions_key_error_exits_2_naming_the_class(tmp_path, definitions):
    broken = copy.deepcopy(definitions)
    del broken["notification_channels"][0]["body"]
    path = tmp_path / "definitions.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    out = io.StringIO()
    code = monitoring.main(["--offline", "--definitions", str(path)], out=out)
    assert code == 2
    assert out.getvalue() == '{"status": "failed", "error": "KeyError"}\n'


def test_standard_output_is_the_same_bytes_on_windows(monkeypatch):
    """A Windows console or redirect is a text stream that turns every \\n into \\r\\n; the
    printed plan, whose digest is read back by hand, is written as the Linux bytes."""
    import sys

    raw = io.BytesIO()
    windows = io.TextIOWrapper(raw, encoding="utf-8", newline="\r\n")
    monkeypatch.setattr(sys, "stdout", windows)
    assert monitoring.main(["--offline"], client_factory=FakeClient) == 0
    windows.flush()
    printed = raw.getvalue()
    result = json.loads(printed)
    assert printed == (json.dumps(result, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    assert b"\r" not in printed
