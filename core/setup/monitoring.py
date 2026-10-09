"""Create the 42 alert emails as code (BUILD.md task 2.10, SETUP.md "Monitoring").

    py -3.13 core/setup/monitoring.py                                  dry run: prints every channel and policy, calls nothing
    py -3.13 core/setup/monitoring.py --second-email NAME@ogilvy.co.za  the same, with the second person's channel
    py -3.13 core/setup/monitoring.py --apply --second-email ...       creates each channel and policy that is missing

One email channel per address, then thirteen log-based alert policies sent to every channel: one per
"42 ALERT <name>:" line (the watchdog job core/setup/watchdog.py writes nine, the nightly reconcile job
core/collect/reconcile.py writes credits_low and reconcile, the weekly job core/collect/drift.py writes
drift) and job_failed on Cloud Run's error lines for any f42- job execution. Each policy closes
itself after AUTO_CLOSE and notifies at most once per RATE_LIMIT.

--apply lists what the project already has and creates only what is missing: a channel is found by its
email address, a policy by its f42_alert label or its display name. Nothing here edits or removes a
channel or a policy, so a policy made before the second person's channel keeps its old recipients; add
them in the console. It runs through the Monitoring REST API as whatever application default credentials
act as (f42-builder, which holds monitoring.editor).
"""
import argparse
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.collect import chain  # noqa: E402
from core.setup import watchdog  # noqa: E402

PROJECT = chain.PROJECT
API = f"https://monitoring.googleapis.com/v3/projects/{PROJECT}"
ALBERT = "albert.meintjes@ogilvy.co.za"
ALERTS = ("collection_missing", "job_failed", "brief_late", "credits_low", "model_spend", "zero_rows",
          "schema_drift", "agent_error_rate", "reconcile", "drift", "seeds_failed",
          "agent_views_failed", "understand_degraded")
LOG_ALERTS = watchdog.ALERTS + ("credits_low", "reconcile", "drift")
RATE_LIMIT = "300s"
AUTO_CLOSE = "21600s"  # six hours: one incident per failing morning, a fresh one the next day
LABEL = "f42-monitoring"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JOB_FAILED_FILTER = ('resource.type="cloud_run_job" AND resource.labels.job_name=~"^f42-" '
                     'AND log_id("run.googleapis.com/varlog/system") AND severity>=ERROR')
DOCS = {
    "collection_missing": "No ok collect run by 04:00 SAST. Check the f42-collect execution and its runs rows.",
    "job_failed": "A 42 Cloud Run job execution failed. Open the job's executions for the error.",
    "brief_late": "A market has no published brief by 06:30 SAST. Check f42-detect and f42-brief.",
    "credits_low": "The SocialCrawl balance is running low against BALANCE_FLOOR. See the reconcile job's line.",
    "model_spend": "Model spend today is over 80% of MODEL_DAILY_USD (SETUP.md, cost and credit guards).",
    "zero_rows": "Collect ran ok but a market got no post_observations. Check the routes for that market.",
    "schema_drift": "A SocialCrawl route's top-level response keys changed. Check the parser for that route.",
    "agent_error_rate": "Over 5% of today's Ask runs failed. Check the agent's runs rows and logs.",
    "reconcile": "The credit ledger and SocialCrawl's own transactions disagree for the day, or the day was not "
                 "covered. See the f42-reconcile runs row.",
    "drift": "Last week's seed expansion was more concentrated than the open feeds on a topic, platform or language. "
             "Run py -3.13 -m core.collect.drift --report for the table.",
    "seeds_failed": "Today's latest seeds run in f42-detect failed, so the seed queue did not get new seeds. See the "
                    "error on that seeds runs row.",
    "agent_views_failed": "Today's latest f42-detect run could not apply the agent views L4's API reads "
                          "(v_item_gate_current, v_item_evidence, tvf_item_timeseries, v_sensitive_items), so they may "
                          "be stale. See counts.agent_views.error on that detect runs row.",
    "understand_degraded": "Today's latest understand run finished ok but is partial or could not write a step "
                           "(topic clusters, embeddings or enrichment), so detect ran on thinner data. See "
                           "counts.partial_reason, counts.embed_error, counts.enrich_error and counts.cluster on "
                           "that understand runs row.",
}


def display_name(name):
    return f"42 ALERT {name}"


def log_filter(name):
    if name == "job_failed":
        return JOB_FAILED_FILTER
    needle = f"42 ALERT {name}:"
    return f'resource.type="cloud_run_job" AND (jsonPayload.message:"{needle}" OR textPayload:"{needle}")'


def channel_body(email):
    return {"type": "email", "displayName": f"42 alerts to {email}", "labels": {"email_address": email},
            "userLabels": {"managed_by": LABEL}}


def policy_body(name, channels):
    return {
        "displayName": display_name(name),
        "userLabels": {"managed_by": LABEL, "f42_alert": name},
        "documentation": {"content": DOCS[name], "mimeType": "text/markdown"},
        "combiner": "OR",
        "conditions": [{"displayName": f"log line for {name}", "conditionMatchedLog": {
            "filter": log_filter(name), "labelExtractors": {"job": "EXTRACT(resource.labels.job_name)"}}}],
        "alertStrategy": {"notificationRateLimit": {"period": RATE_LIMIT}, "autoClose": AUTO_CLOSE},
        "notificationChannels": list(channels),
        "severity": "ERROR",
        "enabled": True,
    }


class Monitoring:
    """Cloud Monitoring v3 REST: lists and creates only."""

    def __init__(self, session=None):
        self._session = session

    def _http(self):
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(creds)
        return self._session

    def _list(self, kind):
        items, token = [], ""
        while True:
            resp = self._http().get(f"{API}/{kind}", params={"pageToken": token} if token else {}, timeout=60)
            _ok(resp, f"list {kind}")
            body = resp.json()
            items += body.get(kind, [])
            token = body.get("nextPageToken", "")
            if not token:
                return items

    def _create(self, kind, body):
        resp = self._http().post(f"{API}/{kind}", json=body, timeout=60)
        _ok(resp, f"create {kind} {body.get('displayName', '')}")
        return resp.json()

    def list_channels(self):
        return self._list("notificationChannels")

    def list_policies(self):
        return self._list("alertPolicies")

    def create_channel(self, body):
        return self._create("notificationChannels", body)

    def create_policy(self, body):
        return self._create("alertPolicies", body)


def _ok(resp, what):
    if resp.status_code != 200:
        raise RuntimeError(f"{what} failed: HTTP {resp.status_code} {resp.text}")


def _address(value):
    if not EMAIL.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an email address")
    return value


def _find_channel(channels, email):
    for c in channels:
        if c.get("type") == "email" and (c.get("labels") or {}).get("email_address", "").lower() == email.lower():
            return c
    return None


def _find_policy(policies, name):
    for p in policies:
        if (p.get("userLabels") or {}).get("f42_alert") == name or p.get("displayName") == display_name(name):
            return p
    return None


def main(argv=None, monitoring=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="create each channel and policy that is missing")
    parser.add_argument("--second-email", type=_address, help="the second person who gets every alert")
    args = parser.parse_args(argv)
    emails = [ALBERT]
    if args.second_email and args.second_email.lower() != ALBERT:
        emails.append(args.second_email)

    print(f"Cloud Monitoring in {PROJECT}: {len(emails)} email channel(s) and {len(ALERTS)} log-based alert policies.")
    if not args.second_email:
        print("  The second person is not set: pass --second-email to add their channel.")
    if not args.apply:
        for email in emails:
            print(f"Channel email {email}")
        for name in ALERTS:
            _show(name, [f"<channel for {e}>" for e in emails])
        print("Dry run: nothing listed or created. Run again with --apply to create whatever is missing.")
        return 0

    monitoring = monitoring or Monitoring()
    channels, policies = monitoring.list_channels(), monitoring.list_policies()
    names = []
    for email in emails:
        found = _find_channel(channels, email)
        if found:
            print(f"Channel email {email}: already exists as {found['name']}; left unchanged.")
        else:
            found = monitoring.create_channel(channel_body(email))
            print(f"Channel email {email}: created as {found['name']}.")
        names.append(found["name"])
    for name in ALERTS:
        found = _find_policy(policies, name)
        if found:
            print(f"Policy {display_name(name)}: already exists as {found['name']}; left unchanged.")
            continue
        made = monitoring.create_policy(policy_body(name, names))
        _show(name, names)
        print(f"  created as {made['name']}.")
    return 0


def _show(name, channels):
    body = policy_body(name, channels)
    strategy = body["alertStrategy"]
    print(f"Policy {body['displayName']} ({body['severity']})")
    print(f"  filter      {body['conditions'][0]['conditionMatchedLog']['filter']}")
    print(f"  notifies    {', '.join(channels)}, at most once per {strategy['notificationRateLimit']['period']}")
    print(f"  auto-close  {strategy['autoClose']}")


if __name__ == "__main__":
    sys.exit(main())
