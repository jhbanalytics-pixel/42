"""Owner step for the 42 monitoring definitions: dry run, apply and readback.

The reviewed definitions in ``ops/monitoring/definitions.json`` declare one email
notification channel, the log based counter metrics and the alert policies for
project ogilvy-trends-v2. This module is the only writer of those resources.

* The default run is a dry run. It reads the project (list and get calls only),
  prints the planned create, update or no-op per resource and the plan digest,
  and changes nothing. ``--offline`` prints the same plan without any API call.
* ``--apply --plan-sha256 <digest>`` creates or updates the owned resources and
  nothing else. It refuses unless the digest is the one the dry run printed.
* ``--readback`` reads every owned resource, compares it field by field with its
  definition, prints one JSON document with a pass or fail per resource and
  exits non-zero on any mismatch.

A resource is owned when it is named by the definitions: a log metric by its
exact name, a channel or a policy by its ``userLabels`` pair ``managed_by`` and
``monitoring_key``. Nothing else in the project is updated, and nothing is ever
deleted: the client has no delete call.

The native client speaks the Cloud Monitoring v3 and Cloud Logging v2 REST
surfaces through ``google-auth`` with Application Default Credentials, imported
only when the native client is built. No command line tool is invoked and no
token or credential is printed or recorded.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

DEFINITIONS_PATH = Path(__file__).resolve().with_name("definitions.json")
PROJECT = "ogilvy-trends-v2"
OWNER_EMAIL = "jhb.analytics@gmail.com"
OWNER_LABEL = "intelligence-42-monitoring"
CONTRACT_VERSION = "42_monitoring_v1"
MONITORING_ENDPOINT = "https://monitoring.googleapis.com/v3/"
LOGGING_ENDPOINT = "https://logging.googleapis.com/v2/"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
REQUEST_TIMEOUT_SECONDS = 60
PENDING_CHANNEL = "pending:{key}"

_METRIC_NAME = re.compile(r"intelligence_42_[a-z0-9_]{1,80}")
_LABEL_VALUE = re.compile(r"[a-z0-9_-]{1,63}")
_CHANNEL_NAME = re.compile(
    rf"projects/{re.escape(PROJECT)}/notificationChannels/[0-9]{{1,32}}"
)
_POLICY_NAME = re.compile(rf"projects/{re.escape(PROJECT)}/alertPolicies/[0-9]{{1,32}}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Proto3 JSON leaves a field at its default value out of a response, so a
# desired default that the server omits is not a difference.
_DEFAULTS = (0, 0.0, "", False, "0s", None)


class Refusal(ValueError):
    """A named refusal; its message is only the code this module wrote."""


def _refuse(code):
    raise Refusal(code)


def canonical_bytes(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def plan_sha256(definitions) -> str:
    """The digest of the canonical definitions: what a dry run proposes."""
    return hashlib.sha256(canonical_bytes(definitions)).hexdigest()


# Definitions


def _owned_labels(entry, key):
    labels = entry["body"].get("userLabels")
    if labels != {"managed_by": OWNER_LABEL, "monitoring_key": key}:
        _refuse("definition_owner_label_invalid")


def validate_definitions(value) -> dict:
    if type(value) is not dict or set(value) != {
        "contract_version",
        "project",
        "owner_label",
        "notification_channels",
        "log_metrics",
        "alert_policies",
    }:
        _refuse("definitions_invalid")
    if (
        value["contract_version"] != CONTRACT_VERSION
        or value["project"] != PROJECT
        or value["owner_label"] != OWNER_LABEL
    ):
        _refuse("definitions_invalid")
    channels = value["notification_channels"]
    if type(channels) is not list or len(channels) != 1:
        _refuse("definition_channels_invalid")
    for entry in channels:
        body = entry["body"]
        if (
            body.get("type") != "email"
            or body.get("labels") != {"email_address": OWNER_EMAIL}
            or not _LABEL_VALUE.fullmatch(entry["key"])
        ):
            _refuse("definition_channels_invalid")
        _owned_labels(entry, entry["key"])
    emails = set(_EMAIL.findall(json.dumps(value)))
    if emails != {OWNER_EMAIL}:
        _refuse("definition_recipient_invalid")
    names = set()
    for entry in value["log_metrics"]:
        body = entry["body"]
        if (
            entry["key"] != body.get("name")
            or not _METRIC_NAME.fullmatch(body["name"])
            or body["name"] in names
            or not isinstance(body.get("filter"), str)
            or not entry.get("sources")
        ):
            _refuse("definition_metric_invalid")
        names.add(body["name"])
    channel_keys = {entry["key"] for entry in channels}
    policy_keys = set()
    for entry in value["alert_policies"]:
        key = entry["key"]
        if not _LABEL_VALUE.fullmatch(key) or key in policy_keys:
            _refuse("definition_policy_invalid")
        policy_keys.add(key)
        _owned_labels(entry, key)
        if (
            not entry["notification_channel_keys"]
            or set(entry["notification_channel_keys"]) != channel_keys
            or "notificationChannels" in entry["body"]
        ):
            _refuse("definition_policy_invalid")
    return value


def load_definitions(path=DEFINITIONS_PATH) -> dict:
    return validate_definitions(json.loads(Path(path).read_text(encoding="utf-8")))


# Comparison


def differences(desired, observed, path="") -> list:
    """Field paths where ``observed`` does not carry ``desired``.

    Only fields the definition sets are compared, so server fields such as a
    name, a creation record or a generated condition name are not differences.
    """
    if isinstance(desired, dict):
        if not isinstance(observed, dict):
            return [path or "."]
        found = []
        for key in sorted(desired):
            where = f"{path}.{key}" if path else key
            if key not in observed:
                if desired[key] not in _DEFAULTS:
                    found.append(where)
                continue
            found.extend(differences(desired[key], observed[key], where))
        return found
    if isinstance(desired, list):
        if not isinstance(observed, list) or len(observed) != len(desired):
            return [path]
        found = []
        for index, (want, have) in enumerate(zip(desired, observed, strict=True)):
            found.extend(differences(want, have, f"{path}[{index}]"))
        return found
    if isinstance(desired, bool) or isinstance(observed, bool):
        return [] if desired is observed else [path]
    if isinstance(desired, int | float) and isinstance(observed, int | float | str):
        try:
            return [] if float(observed) == float(desired) else [path]
        except ValueError:
            return [path]
    return [] if desired == observed else [path]


# Project state


def _owned(item, key):
    labels = item.get("userLabels") or {}
    return labels.get("managed_by") == OWNER_LABEL and (
        key is None or labels.get("monitoring_key") == key
    )


def _pick(items, key):
    found = [item for item in items if _owned(item, key)]
    if len(found) > 1:
        _refuse(f"owned_duplicate:{key}")
    return found[0] if found else None


def read_state(client, definitions) -> dict:
    """Owned resources as the project holds them, read with list and get only."""
    channels = client.list_channels()
    policies = client.list_policies()
    return {
        "channels": {
            entry["key"]: _pick(channels, entry["key"])
            for entry in definitions["notification_channels"]
        },
        "channel_owned_count": sum(1 for item in channels if _owned(item, None)),
        "metrics": {
            entry["key"]: client.get_log_metric(entry["body"]["name"])
            for entry in definitions["log_metrics"]
        },
        "policies": {
            entry["key"]: _pick(policies, entry["key"])
            for entry in definitions["alert_policies"]
        },
    }


def _policy_body(entry, channel_names):
    body = json.loads(json.dumps(entry["body"]))
    body["notificationChannels"] = [
        channel_names.get(key) or PENDING_CHANNEL.format(key=key)
        for key in entry["notification_channel_keys"]
    ]
    return body


def _resources(definitions, state):
    """(kind, key, desired body, observed or None) in apply order."""
    channel_names = {
        key: (item or {}).get("name") for key, item in state["channels"].items()
    }
    rows = [
        (
            "notification_channel",
            entry["key"],
            entry["body"],
            state["channels"][entry["key"]],
        )
        for entry in definitions["notification_channels"]
    ]
    rows += [
        ("log_metric", entry["key"], entry["body"], state["metrics"][entry["key"]])
        for entry in definitions["log_metrics"]
    ]
    rows += [
        (
            "alert_policy",
            entry["key"],
            _policy_body(entry, channel_names),
            state["policies"][entry["key"]],
        )
        for entry in definitions["alert_policies"]
    ]
    return rows


def _action(desired, observed):
    if observed is None:
        return "create", []
    found = differences(desired, observed)
    return ("update" if found else "no-op"), found


# Commands


def dry_run(definitions, client=None) -> dict:
    """The plan; with no client nothing is read and every action is unread."""
    digest = plan_sha256(definitions)
    if client is None:
        state = {
            "channels": {e["key"]: None for e in definitions["notification_channels"]},
            "metrics": {e["key"]: None for e in definitions["log_metrics"]},
            "policies": {e["key"]: None for e in definitions["alert_policies"]},
        }
    else:
        state = read_state(client, definitions)
    planned = []
    for kind, key, desired, observed in _resources(definitions, state):
        if client is None:
            action, found = "create-or-update-unread", []
        else:
            action, found = _action(desired, observed)
        row = {"kind": kind, "key": key, "action": action}
        if observed is not None and observed.get("name"):
            row["name"] = observed["name"]
        if found:
            row["differences"] = found
        planned.append(row)
    return {
        "mode": "dry-run" if client is not None else "dry-run-offline",
        "project": PROJECT,
        "plan_sha256": digest,
        "resources": planned,
    }


def _top_fields(found):
    """The top level fields named by comparison paths, sorted."""
    return sorted({re.split(r"[.\[]", path, maxsplit=1)[0] for path in found})


def _patch(desired, found):
    """The body and update mask carrying only the top level fields that differ."""
    fields = _top_fields(found)
    return {field: desired[field] for field in fields}, ",".join(fields)


def apply(definitions, client, *, expected_sha256) -> dict:
    digest = plan_sha256(definitions)
    if expected_sha256 != digest:
        _refuse("plan_sha256_mismatch")
    state = read_state(client, definitions)
    applied = []
    for entry in definitions["notification_channels"]:
        key, desired = entry["key"], entry["body"]
        observed = state["channels"][key]
        action, found = _action(desired, observed)
        if action == "create":
            observed = client.create_channel(desired)
        elif action == "update":
            # A changed type or recipient is a different channel, and resending
            # the email label can restart its verification: refuse, never patch.
            if {"type", "labels"} & set(_top_fields(found)):
                _refuse(f"channel_identity_drift:{key}")
            body, mask = _patch(desired, found)
            observed = client.patch_channel(
                _checked(observed["name"], _CHANNEL_NAME), body, mask
            )
        state["channels"][key] = observed
        applied.append(_row("notification_channel", key, action, observed, found))
    for entry in definitions["log_metrics"]:
        key, desired = entry["key"], entry["body"]
        observed = state["metrics"][key]
        action, found = _action(desired, observed)
        if action == "create":
            observed = client.create_log_metric(desired)
        elif action == "update":
            observed = client.update_log_metric(desired["name"], desired)
        applied.append(_row("log_metric", key, action, observed, found))
    channel_names = {k: (v or {}).get("name") for k, v in state["channels"].items()}
    for entry in definitions["alert_policies"]:
        key = entry["key"]
        desired = _policy_body(entry, channel_names)
        if any(not _CHANNEL_NAME.fullmatch(n) for n in desired["notificationChannels"]):
            _refuse("channel_unresolved")
        observed = state["policies"][key]
        action, found = _action(desired, observed)
        if action == "create":
            observed = client.create_policy(desired)
        elif action == "update":
            body, mask = _patch(desired, found)
            observed = client.patch_policy(
                _checked(observed["name"], _POLICY_NAME), body, mask
            )
        applied.append(_row("alert_policy", key, action, observed, found))
    return {
        "mode": "apply",
        "project": PROJECT,
        "plan_sha256": digest,
        "resources": applied,
    }


def _checked(name, grammar):
    if not isinstance(name, str) or not grammar.fullmatch(name):
        _refuse("resource_name_invalid")
    return name


def _row(kind, key, action, observed, found):
    row = {"kind": kind, "key": key, "action": action}
    if observed is not None and observed.get("name"):
        row["name"] = observed["name"]
    if found:
        row["differences"] = found
    return row


def readback(definitions, client) -> dict:
    state = read_state(client, definitions)
    rows = []
    for kind, key, desired, observed in _resources(definitions, state):
        if observed is None:
            rows.append({"kind": kind, "key": key, "status": "fail", "missing": True})
            continue
        found = differences(desired, observed)
        row = {
            "kind": kind,
            "key": key,
            "name": observed.get("name"),
            "status": "fail" if found else "pass",
        }
        if found:
            row["differences"] = found
        rows.append(row)
    channels = {
        "owned_count": state["channel_owned_count"],
        "recipients": sorted(
            (item or {}).get("labels", {}).get("email_address", "")
            for item in state["channels"].values()
        ),
    }
    channels["status"] = (
        "pass"
        if channels["owned_count"] == 1 and channels["recipients"] == [OWNER_EMAIL]
        else "fail"
    )
    passed = channels["status"] == "pass" and all(
        row["status"] == "pass" for row in rows
    )
    return {
        "mode": "readback",
        "project": PROJECT,
        "plan_sha256": plan_sha256(definitions),
        "channels": channels,
        "resources": rows,
        "status": "pass" if passed else "fail",
    }


# Native client


class NativeError(RuntimeError):
    def __init__(self, call, status, detail):
        super().__init__(f"{call}: http {status}: {str(detail)[:300]}")
        self.call = call
        self.status = status


class NativeClient:
    """Cloud Monitoring v3 and Cloud Logging v2 over REST, with ADC.

    The call table is closed: list and create and patch for channels and
    policies, get and create and update for log metrics. There is no delete.
    """

    def __init__(self, session=None):
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _project = google.auth.default(scopes=(CLOUD_PLATFORM_SCOPE,))
            session = AuthorizedSession(credentials)
        self._session = session

    def _call(self, call, method, url, *, params=None, body=None, allow_404=False):
        response = self._session.request(
            method, url, params=params, json=body, timeout=REQUEST_TIMEOUT_SECONDS
        )
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code != 200:
            raise NativeError(call, response.status_code, response.text)
        return response.json() if response.content else {}

    def _list(self, call, url, field):
        items, token = [], None
        while True:
            params = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            page = self._call(call, "GET", url, params=params)
            items.extend(page.get(field, []))
            token = page.get("nextPageToken")
            if not token:
                return items

    def list_channels(self):
        url = f"{MONITORING_ENDPOINT}projects/{PROJECT}/notificationChannels"
        return self._list("list_channels", url, "notificationChannels")

    def create_channel(self, body):
        url = f"{MONITORING_ENDPOINT}projects/{PROJECT}/notificationChannels"
        return self._call("create_channel", "POST", url, body=body)

    def patch_channel(self, name, body, mask):
        url = f"{MONITORING_ENDPOINT}{_checked(name, _CHANNEL_NAME)}"
        return self._call(
            "patch_channel", "PATCH", url, params={"updateMask": mask}, body=body
        )

    def list_policies(self):
        url = f"{MONITORING_ENDPOINT}projects/{PROJECT}/alertPolicies"
        return self._list("list_policies", url, "alertPolicies")

    def create_policy(self, body):
        url = f"{MONITORING_ENDPOINT}projects/{PROJECT}/alertPolicies"
        return self._call("create_policy", "POST", url, body=body)

    def patch_policy(self, name, body, mask):
        url = f"{MONITORING_ENDPOINT}{_checked(name, _POLICY_NAME)}"
        return self._call(
            "patch_policy", "PATCH", url, params={"updateMask": mask}, body=body
        )

    def get_log_metric(self, metric):
        url = f"{LOGGING_ENDPOINT}projects/{PROJECT}/metrics/{_metric(metric)}"
        return self._call("get_log_metric", "GET", url, allow_404=True)

    def create_log_metric(self, body):
        _metric(body["name"])
        url = f"{LOGGING_ENDPOINT}projects/{PROJECT}/metrics"
        return self._call("create_log_metric", "POST", url, body=body)

    def update_log_metric(self, metric, body):
        url = f"{LOGGING_ENDPOINT}projects/{PROJECT}/metrics/{_metric(metric)}"
        return self._call("update_log_metric", "PUT", url, body=body)


def _metric(name):
    return _checked(name, _METRIC_NAME)


# Command line


def _parser():
    parser = argparse.ArgumentParser(
        prog="apply_monitoring.py", description=__doc__.splitlines()[0]
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="plan with no API call")
    mode.add_argument("--apply", action="store_true", help="create or update owned")
    mode.add_argument("--readback", action="store_true", help="compare owned")
    parser.add_argument(
        "--plan-sha256", default=None, help="digest the dry run printed"
    )
    parser.add_argument("--definitions", default=str(DEFINITIONS_PATH))
    return parser


class _StandardOutput:
    """Standard output written as exact UTF-8 bytes. A Windows console or redirect is a text
    stream that turns every \\n into \\r\\n, so the printed plan would differ from a Linux
    run; writing to the byte buffer keeps the two the same."""

    def write(self, text):
        sys.stdout.flush()
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()


def main(argv=None, *, client_factory=NativeClient, out=None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    out = _StandardOutput() if out is None else out
    code = 0
    try:
        definitions = load_definitions(args.definitions)
        if args.offline:
            result = dry_run(definitions)
        elif args.apply:
            if args.plan_sha256 is None:
                _refuse("plan_sha256_required")
            result = apply(
                definitions, client_factory(), expected_sha256=args.plan_sha256
            )
        elif args.readback:
            result = readback(definitions, client_factory())
            code = 0 if result["status"] == "pass" else 1
        else:
            result = dry_run(definitions, client_factory())
    except (Refusal, NativeError) as error:
        result, code = {"status": "refused", "error": str(error)}, 2
    except Exception as error:  # noqa: BLE001 - the class alone, no trace or detail
        out.write(
            json.dumps({"status": "failed", "error": type(error).__name__}) + "\n"
        )
        return 2
    out.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
